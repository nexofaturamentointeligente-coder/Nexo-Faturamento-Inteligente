"""
NEXO - Raio-X público do lead (captação pelo site; decisões do dono em 15/11/2026)
==================================================================================
Rotas (DESLIGADAS por padrão: com AVALIACAO_PUBLICA_ATIVA falso, todas respondem 404; o POST sem token CSRF
recebe antes o 400 do CSRF, que é global e roda primeiro):
  GET  /avaliacao-inicial            → formulário: contato, plano de interesse, relatórios e autorização
  POST /avaliacao-inicial            → lê os relatórios no processo isolado e grava o lead (origem "site")
  GET  /avaliacao-inicial/resultado  → a vitrine do Raio-X do lead desta sessão
  POST /avaliacao-inicial/sair       → esquece o resultado nesta sessão

Regras:
  - O lead nasce pelo mesmo caminho do consultor (avaliacao_cenario.processar e registrar), com
    registrado_por nulo, origem "site", a autorização marcada pelo próprio visitante (instante e versão do
    texto) e o que ele informou; o consultor o vê em /admin/leads, com o protocolo. Nenhum disparo de
    WhatsApp ou e-mail: o "Desbloquear Relatório Completo" é um link wa.me que o visitante abre.
  - Ordem do POST: CSRF (global), campo isca, tempo mínimo desde o formulário, campos, autorização e
    arquivos (até 12 e 8 MB), e só então o banco (esquema), a vaga única, o teto diário e o processo isolado:
    pedido inválido não toca nas tabelas de lead (tests/test_isolamento_rotas.py).
  - Limites: 3 envios por hora e 5 por dia por IP (chave HMAC, extensions.chave_limite_ip); teto diário do
    site (padrão 20, dia de Brasília) contando os leads gravados E as tentativas que chegaram ao processo
    isolado, com sucesso ou não (estas, na memória do processo); a vaga única só se estiver livre, sem espera
    (o cliente pagante nunca fica atrás de uma fila de visitantes); até 8 MB pelo tamanho REAL dos arquivos
    recebidos (o Content-Length do cliente não é autoridade, e corpo chunked não o traz: ele só recusa acima do
    teto mais a folga do envelope do formulário); processo isolado com prazo de 60 s.
  - WhatsApp do visitante: as regras do envio pela Evolution (whatsapp_service.higienizar_telefone), mais
    estritas no que é ambíguo, como a máscara dos scripts; gravado só com DDD e número.
  - O resultado fica preso à sessão assinada (o id do lead e o instante, nunca na URL), vale até 8 horas e
    sai com Cache-Control: no-store. A vitrine é lista branca (diagnostico_cenario.visao_publica): os produtos
    bloqueados não saem do servidor, e a tela desenha no lugar deles um texto fixo desfocado.
  - A vitrine é calculada UMA vez, no envio, ainda dentro da vaga, e guardada por lead neste processo: o GET
    do resultado só desenha, sem banco. Sem a entrada (processo reiniciado ou que dormiu), o GET refaz uma
    vez, um pedido por vez (os outros recebem 503 com Retry-After), com a conexão devolvida ao pool antes da
    triagem, e guarda de novo. O reenvio, a retriagem e o expurgo feitos pelas telas do consultor a descartam
    (com geração, para um cálculo em andamento não guardar o dado de antes); as vencidas saem a cada pedido ao
    processo, e "Encerrar" tira a do visitante.
  - A autorização usa o texto aprovado pelo dono em 15/11/2026, que ainda precisa de revisão jurídica antes
    de ligar a rota (Docs/PRE_CADASTRO_LANDING.md).
"""

from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, time as horario_do_dia, timezone
import os
import re
import threading
import time
import uuid

from flask import (Blueprint, abort, current_app, flash, make_response, redirect, render_template, request,
                   session, url_for)
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

import avaliacao_cenario
from blueprints.leads import CODIGO_OCUPADO, ESPERA_SEGUNDOS, MENSAGEM_OCUPADO, _lead_para_payload, esquema_disponivel
from configuracoes_conta import DadoInvalido
from contas_email import normalizar_email
import diagnostico_cenario
from emails import email_valido
import entitlements
from etl_inputs import ETLInputError
from etl_limits import get_limits
import etl_runtime
from etl_uploads import vaga_de_entrada
from extensions import chave_limite_ip, db, limiter
from horario import BRASILIA, agora_utc
from leads_avaliacao import RETENCAO_DIAS
from models import (DIFICULDADE_MAXIMO, LeadAvaliacao, ROTULOS_FAIXAS_DE_FATURAMENTO,
                    ROTULOS_PLANOS_DE_INTERESSE)
from raio_x_servico import EXTENSOES_ACEITAS
from werkzeug.utils import secure_filename
from whatsapp_service import higienizar_telefone

avaliacao_publica_bp = Blueprint("avaliacao_publica", __name__)

# Texto da caixa obrigatória (o da tela do consultor, em 1ª pessoa; aprovado pelo dono em 15/11/2026) e a
# versão gravada no lead. Texto novo exige versão nova.
TEXTO_AUTORIZACAO = "Autorizo o uso destes relatórios nesta avaliação."
VERSAO_AUTORIZACAO = "autorizacao-relatorios-2026-11-15"
# Padrões ajustáveis pelo config (AVALIACAO_PUBLICA_<NOME>), sempre inteiros positivos.
PADROES = {"MAX_ARQUIVOS": 12, "MAX_MB": 8, "TETO_DIARIO": 20, "TEMPO_MINIMO_SEGUNDOS": 5,
           "RESULTADO_HORAS": 8}
# O run_avaliacao_cenario dobra o prazo configurado (até 120 s): 30 vira 60 s no público.
PRAZO_DO_PROCESSO_SEGUNDOS = 30
CHAVE_DA_SESSAO = "avaliacao_publica"
CHAVE_DO_FORMULARIO = "avaliacao_publica_aberto_em"
NOME_MAXIMO = 120
# Limites por IP aprovados pelo dono (constantes: mudar exige editar aqui e os testes que os fixam).
LIMITE_DE_ENVIOS = "3 per hour;5 per day"
LIMITE_DE_LEITURAS = "60 per hour"
# Vitrines guardadas neste processo (gunicorn -w 1), por lead: até este número, cada uma até RESULTADO_HORAS
# (as vencidas saem a cada pedido ao processo, sem esperar a leitura da mesma chave).
VITRINES_MAXIMO = 256
# O Content-Length conta também o envelope do formulário (fronteiras, cabeçalhos das partes, token e campos):
# ele só recusa acima do teto mais esta folga; quem decide é o tamanho real dos arquivos.
FOLGA_DO_ENVELOPE = 64 * 1024
MENSAGEM_WHATSAPP = "Informe um WhatsApp do Brasil com DDD: 10 ou 11 números."
WHATSAPP_MAXIMO = 60

limite_envio = limiter.limit(LIMITE_DE_ENVIOS, key_func=chave_limite_ip)
# A leitura do resultado vem da vitrine guardada; o teto por IP limita o recálculo depois de um reinício.
limite_resultado = limiter.limit(LIMITE_DE_LEITURAS, key_func=chave_limite_ip)

_vitrines: "OrderedDict" = OrderedDict()
# A geração muda a cada descarte (reenvio, retriagem ou expurgo do consultor): o cálculo que começou antes dele
# não guarda a vitrine de antes (como a geração do cache do diagnóstico, em blueprints/leads.py).
_geracao_das_vitrines = {"valor": 0}
_vitrines_trava = threading.Lock()
_recalculo_trava = threading.Lock()
_tentativas = {"dia": None, "quantas": 0}
_tentativas_trava = threading.Lock()


@avaliacao_publica_bp.before_request
def _somente_ligada():
    # Roda depois dos before_request da aplicação: o POST sem token CSRF recebe o 400 do CSRF antes deste 404.
    if current_app.config.get("AVALIACAO_PUBLICA_ATIVA") is not True:
        abort(404)


@avaliacao_publica_bp.before_app_request
def _varrer_vitrines() -> None:
    """A cada pedido ao processo (de qualquer tela), as vitrines vencidas saem da memória: o contato e os
    produtos de quem não voltou não ficam guardados além de RESULTADO_HORAS. Um erro aqui nunca derruba o
    pedido."""
    try:
        with _vitrines_trava:
            _varrer_vitrines_vencidas(time.monotonic())
    except Exception as erro:  # noqa: BLE001 - a limpeza é auxiliar; o pedido segue
        current_app.logger.warning("Varredura das vitrines do Raio-X público não rodou (%s).", type(erro).__name__)


def _config(nome: str) -> int:
    valor = current_app.config.get(f"AVALIACAO_PUBLICA_{nome}", PADROES[nome])
    return valor if type(valor) is int and valor >= 0 else PADROES[nome]


# =====================================================================
# Respostas
# =====================================================================
def _pagina(template: str, status: int = 200, **contexto):
    """A página pública: sem cache (o formulário tem token; o resultado, dado do lead) e fora da busca."""
    resposta = make_response(render_template(template, **contexto), status)
    resposta.headers["Cache-Control"] = "no-store"
    resposta.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resposta


def _contexto_do_formulario(formulario) -> dict:
    return {"formulario": formulario, "planos": ROTULOS_PLANOS_DE_INTERESSE, "faixas": ROTULOS_FAIXAS_DE_FATURAMENTO,
            "texto_autorizacao": TEXTO_AUTORIZACAO, "max_arquivos": _config("MAX_ARQUIVOS"),
            "max_mb": _config("MAX_MB"), "retencao_dias": RETENCAO_DIAS, "dificuldade_maximo": DIFICULDADE_MAXIMO}


def _formulario_de_volta(formulario) -> dict:
    """Os campos digitados, para a tela não apagá-los; nunca a autorização (marca-se a cada envio) nem o
    campo isca."""
    return {nome: formulario.get(nome, "") for nome in
            ("nome", "empresa", "email", "whatsapp", "plano_interesse", "faixa_faturamento", "dificuldade")}


def _recusar(mensagem: str, formulario, status: int = 422, espera: int | None = None):
    flash(mensagem, "danger")
    resposta = _pagina("avaliacao_publica/formulario.html", status,
                       **_contexto_do_formulario(_formulario_de_volta(formulario)))
    if espera:
        resposta.headers["Retry-After"] = str(espera)
    return resposta


def _ocupado(formulario):
    """A vaga única está com outro envio ou processamento: 503 com Retry-After, nada foi lido."""
    return _recusar(MENSAGEM_OCUPADO, formulario, 503, ESPERA_SEGUNDOS)


# =====================================================================
# Formulário
# =====================================================================
def _uma_linha(valor) -> str:
    return " ".join(valor.split()) if isinstance(valor, str) else ""


def _texto_livre(valor) -> str:
    """Até DIFICULDADE_MAXIMO caracteres, com quebras simples: sem espaços sobrando nem linhas vazias
    em série (o guarda de NUL da factory já recusa o caractere nulo)."""
    if not isinstance(valor, str):
        return ""
    linhas = [" ".join(linha.split()) for linha in valor.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    texto = "\n".join(linhas).strip()
    while "\n\n\n" in texto:
        texto = texto.replace("\n\n\n", "\n\n")
    return texto


# Antes da regra, como nos scripts: os caracteres invisíveis de formatação saem (o Contatos do Mac envolve o
# número copiado em marcas de direção), os traços tipográficos viram hífen e o "0xx" vira o zero de tronco.
_INVISIVEIS = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_TRACOS = re.compile("[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]")
_ZERO_XX = re.compile(r"0\s*[xX]{2}")
# Depois do DDD, fixo de 8 dígitos (começa por 2 a 5) ou celular de 9 (começa por 9): a regra da máscara dos
# scripts (static/js/landing-precadastro.js e avaliacao-publica.js).
_NUMERO_BRASILEIRO = re.compile(r"[1-9][1-9](?:[2-5][0-9]{7}|9[0-9]{8})")


def _whatsapp(valor) -> str:
    """O WhatsApp do visitante só com DDD e número (10 ou 11 dígitos), ou DadoInvalido.

    As regras de whatsapp_service.higienizar_telefone, as do envio pela Evolution: DDD em uso, celular de 9
    dígitos começando por 9, sem ramal, letra, número de exemplo nem DDI que não seja o 55. Mais estrito no que
    é ambíguo, como a máscara dos scripts: sem "+" ou "00", o 55 do começo não vira DDI (12 ou 13 dígitos são
    recusados em vez de lidos como outro número), e o fixo de 8 dígitos começa por 2 a 5. Caracteres invisíveis
    de formatação, traços tipográficos e o "0xx" são normalizados antes, como nos scripts."""
    texto = valor if isinstance(valor, str) else ""
    texto = " ".join(_ZERO_XX.sub("0", _TRACOS.sub("-", _INVISIVEIS.sub("", texto))).split())
    if not texto:
        raise DadoInvalido("Informe o WhatsApp com DDD.")
    numero = higienizar_telefone(texto) if len(texto) <= WHATSAPP_MAXIMO else None
    nacional = numero[2:] if numero else ""
    digitos = "".join(c for c in texto if c.isdigit())
    internacional = texto.startswith("+") or digitos.startswith("00")
    if not _NUMERO_BRASILEIRO.fullmatch(nacional) or (
            not internacional and len(digitos.removeprefix("0")) != len(nacional)):
        raise DadoInvalido(MENSAGEM_WHATSAPP)
    return nacional


def _contato(formulario) -> dict:
    """O contato e as respostas do visitante, prontos para avaliacao_cenario.registrar (DadoInvalido com a
    mensagem para a tela)."""
    nome, empresa = _uma_linha(formulario.get("nome")), _uma_linha(formulario.get("empresa"))
    if not nome or not empresa:
        raise DadoInvalido("Informe o nome do responsável e o nome da empresa.")
    if len(nome) > NOME_MAXIMO or len(empresa) > NOME_MAXIMO:
        raise DadoInvalido(f"O nome do responsável e o da empresa têm até {NOME_MAXIMO} caracteres.")
    email = normalizar_email(formulario.get("email"))
    if not email_valido(email):
        raise DadoInvalido("Informe um e-mail válido.")
    whatsapp = _whatsapp(formulario.get("whatsapp"))
    plano = formulario.get("plano_interesse")
    if plano not in ROTULOS_PLANOS_DE_INTERESSE:
        raise DadoInvalido("Escolha o plano de interesse.")
    faixa = formulario.get("faixa_faturamento") or None
    if faixa is not None and faixa not in ROTULOS_FAIXAS_DE_FATURAMENTO:
        raise DadoInvalido("Escolha a faixa de faturamento na lista ou deixe em branco.")
    dificuldade = _texto_livre(formulario.get("dificuldade"))
    if len(dificuldade) > DIFICULDADE_MAXIMO:
        raise DadoInvalido(f"A principal dificuldade tem até {DIFICULDADE_MAXIMO} caracteres.")
    if formulario.get("autorizacao") != "1":
        raise DadoInvalido("Para fazer o Raio-X, marque a autorização de uso dos relatórios.")
    return {"nome_contato": nome, "email": email, "whatsapp": whatsapp, "nome_loja": empresa, "origem": "site",
            "plano_interesse": plano, "faixa_faturamento": faixa, "dificuldade": dificuldade or None,
            "versao_autorizacao": VERSAO_AUTORIZACAO}


def _avaliacoes_de_hoje() -> int:
    """Avaliações do site gravadas hoje (dia de Brasília)."""
    hoje = agora_utc().astimezone(BRASILIA).date()
    inicio = datetime.combine(hoje, horario_do_dia.min, tzinfo=BRASILIA).astimezone(timezone.utc)
    return db.session.scalar(select(func.count()).select_from(LeadAvaliacao).where(
        LeadAvaliacao.origem == "site", LeadAvaliacao.criado_em >= inicio)) or 0


class _TetoDoDiaAtingido(Exception):
    """O teto de tentativas do dia foi atingido na hora de chamar o processo isolado."""


def _contar_tentativa_de_hoje() -> bool:
    """Conta (neste processo) a tentativa que vai ao processo isolado, com sucesso ou não, no dia de Brasília;
    False quando o teto do dia já foi atingido. Envio que falha não grava lead e ficaria fora da contagem do
    banco, ocupando a vaga única sem gastar o teto."""
    hoje = agora_utc().astimezone(BRASILIA).date()
    with _tentativas_trava:
        if _tentativas["dia"] != hoje:
            _tentativas.update(dia=hoje, quantas=0)
        if _tentativas["quantas"] >= _config("TETO_DIARIO"):
            return False
        _tentativas["quantas"] += 1
        return True


def _tentativas_esgotadas() -> bool:
    """Se as tentativas de hoje (dia de Brasília) já chegaram ao teto, sem contar nenhuma."""
    hoje = agora_utc().astimezone(BRASILIA).date()
    with _tentativas_trava:
        return _tentativas["dia"] == hoje and _tentativas["quantas"] >= _config("TETO_DIARIO")


def _executar_contando(caminhos, limits=None):
    """O executor da avaliação com o teto do dia: a tentativa só conta aqui, quando o processo isolado vai mesmo
    ser chamado. As recusas do processo web (formato, Baia, arquivo vazio ou repetido) não gastam o teto."""
    if not _contar_tentativa_de_hoje():
        raise _TetoDoDiaAtingido()
    return etl_runtime.run_avaliacao_cenario(caminhos, limits)


def _formato_recusado(arquivos):
    """A mensagem do primeiro arquivo com extensão que o Raio-X não lê (a mesma do serviço de envio), ou None:
    recusado antes da vaga, sem ocupá-la."""
    for numero, arquivo in enumerate(arquivos, 1):
        nome = secure_filename(arquivo.filename or "") or ""
        if os.path.splitext(nome)[1].lower().lstrip(".") not in EXTENSOES_ACEITAS:
            return (f"Arquivo {numero}: formato não suportado. Envie CSV, XLSX, XLS ou Planilha XML 2003 "
                    "(.xml).")
    return None


def descartar_vitrine(lead_id=None) -> None:
    """Esquece a vitrine guardada de um lead (ou todas) e muda a geração: o reenvio, a retriagem e o expurgo do
    consultor, feitos pelas telas deste processo, trocam os dados de que ela saiu (blueprints/leads.
    _descartar_do_cache), e o cálculo que estava em andamento não a guarda de novo com o dado de antes. A
    retriagem pela linha de comando roda em outro processo e não chega aqui (Docs/PRE_CADASTRO_LANDING.md)."""
    with _vitrines_trava:
        _geracao_das_vitrines["valor"] += 1
        if lead_id is None:
            _vitrines.clear()
        else:
            _vitrines.pop(lead_id, None)


def zerar_contadores() -> None:
    """Esquece as vitrines guardadas e as tentativas do dia deste processo (para os testes)."""
    with _vitrines_trava:
        _vitrines.clear()
    with _tentativas_trava:
        _tentativas.update(dia=None, quantas=0)


def _tamanho_declarado():
    """O Content-Length do pedido (None quando o corpo vem chunked ou sem o cabeçalho)."""
    return request.content_length


def _tamanho_dos_arquivos(arquivos):
    """A soma dos tamanhos REAIS dos arquivos recebidos (o Content-Length do cliente não é autoridade), ou
    None quando algum não dá para medir."""
    total = 0
    for arquivo in arquivos:
        fluxo = arquivo.stream
        try:
            posicao = fluxo.tell()
            fluxo.seek(0, os.SEEK_END)
            total += fluxo.tell()
            fluxo.seek(posicao)
        except (AttributeError, OSError, ValueError):
            return None
    return total


def mensagem_de_tamanho() -> str:
    return (f"Os relatórios passam de {_config('MAX_MB')} MB somados. Nada foi gravado: envie menos meses "
            "por vez.")


def _avaliar(arquivos, contato: dict) -> LeadAvaliacao:
    """Lê os relatórios no processo isolado (prazo e teto por arquivo do público) e grava o lead (com commit)."""
    prata = entitlements.catalogo().nivel("PRATA")
    meses_prata = prata.limite_historico_meses if prata is not None else None
    base = get_limits()
    limites = replace(base, timeout_seconds=min(PRAZO_DO_PROCESSO_SEGUNDOS, base.timeout_seconds),
                      max_file_bytes=min(base.max_file_bytes, _config("MAX_MB") * 1024 * 1024))
    # Até 1 minuto no processo isolado: a transação de leitura termina antes, como no consultor.
    db.session.rollback()
    resultado = avaliacao_cenario.processar(arquivos, limits=limites, executar=_executar_contando)
    lead = avaliacao_cenario.registrar(resultado, contato=contato, registrado_por=None, meses_prata=meses_prata)
    db.session.commit()
    return lead


# =====================================================================
# Resultado
# =====================================================================
def _lead_da_sessao():
    """O id do lead que esta sessão enviou, se ainda valer; senão None (e a chave sai da sessão)."""
    dados = session.get(CHAVE_DA_SESSAO)
    if not isinstance(dados, dict):
        return None
    instante = dados.get("em")
    try:
        lead_id = uuid.UUID(str(dados.get("lead")))
    except ValueError:
        lead_id = None
    if lead_id is None or type(instante) is not int or not 0 <= time.time() - instante <= _config("RESULTADO_HORAS") * 3600:
        session.pop(CHAVE_DA_SESSAO, None)
        return None
    return lead_id


def _whatsapp_legivel(whatsapp) -> str:
    digitos = "".join(c for c in whatsapp or "" if c.isdigit())
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        digitos = digitos[2:]
    if len(digitos) in (10, 11):
        return f"({digitos[:2]}) {digitos[2:-4]}-{digitos[-4:]}"
    return whatsapp or ""


def mensagem_de_desbloqueio(lead: LeadAvaliacao) -> str:
    """A mensagem do "Desbloquear Relatório Completo": o formato do pré-cadastro da landing
    (static/js/landing-precadastro.js), com os dados que o próprio visitante informou, o Raio-X realizado e o
    protocolo que o consultor acha em /admin/leads (exceção registrada em Docs/WHATSAPP_EVOLUTION.md)."""
    linhas = ["*NOVO CONTATO COMERCIAL | NEXO*", "",
              f"Nome: {lead.nome_contato}", f"Empresa: {lead.nome_loja}",
              f"WhatsApp: {_whatsapp_legivel(lead.whatsapp)}",
              f"Plano de interesse: {ROTULOS_PLANOS_DE_INTERESSE.get(lead.plano_interesse, '—')}"]
    if lead.email:
        linhas.append(f"E-mail: {lead.email}")
    if lead.faixa_faturamento in ROTULOS_FAIXAS_DE_FATURAMENTO:
        linhas.append(f"Faixa de faturamento: {ROTULOS_FAIXAS_DE_FATURAMENTO[lead.faixa_faturamento]}")
    if lead.dificuldade:
        linhas.append(f"Principal necessidade: {lead.dificuldade}")
    linhas += ["", f"Situação do Raio-X: Realizado (protocolo {lead.protocolo})", "Solicitou auxílio: Não",
               "Origem: Landing Page da NEXO"]
    return "\n".join(linhas)


def _contexto_do_resultado(lead: LeadAvaliacao) -> dict:
    """Tudo o que a tela do resultado desenha, montado de uma vez: a vitrine (lista branca) e o que o próprio
    visitante digitou. Lê o que precisa do lead, as linhas de PDV e devolve a conexão ao pool ANTES da triagem,
    que é só CPU."""
    dados_do_lead = _lead_para_payload(lead)
    contexto = {"empresa": lead.nome_loja, "protocolo": lead.protocolo, "mensagem": mensagem_de_desbloqueio(lead)}
    linhas, _assinatura = avaliacao_cenario.linhas_do_lead(lead.id)
    db.session.rollback()
    contexto["vitrine"] = diagnostico_cenario.visao_publica(dados_do_lead, linhas, agora=agora_utc(),
                                                            retencao_dias=RETENCAO_DIAS)
    return contexto


def _varrer_vitrines_vencidas(agora: float) -> None:
    """Com a trava: tira as vitrines vencidas, da mais antiga para a mais nova (todas valem RESULTADO_HORAS e
    entram no fim)."""
    while _vitrines:
        validade, _contexto = next(iter(_vitrines.values()))
        if validade > agora:
            break
        _vitrines.popitem(last=False)


def _geracao_atual() -> int:
    """Lida ANTES de ler o lead: se um descarte acontecer durante o cálculo, a vitrine não é guardada."""
    with _vitrines_trava:
        return _geracao_das_vitrines["valor"]


def _guardar_vitrine(lead_id, contexto: dict, geracao: int) -> bool:
    """Guarda a vitrine do lead, salvo se houve descarte desde `geracao` (ela seria de antes dele)."""
    with _vitrines_trava:
        agora = time.monotonic()
        _varrer_vitrines_vencidas(agora)
        if geracao != _geracao_das_vitrines["valor"]:
            return False
        _vitrines[lead_id] = (agora + _config("RESULTADO_HORAS") * 3600, contexto)
        _vitrines.move_to_end(lead_id)
        while len(_vitrines) > VITRINES_MAXIMO:
            _vitrines.popitem(last=False)
        return True


def _vitrine_guardada(lead_id):
    with _vitrines_trava:
        _varrer_vitrines_vencidas(time.monotonic())
        entrada = _vitrines.get(lead_id)
        return None if entrada is None else entrada[1]


def _esquecer_vitrine(lead_id) -> None:
    """Tira a vitrine do lead sem mudar a geração: o visitante encerrou, o dado não mudou."""
    with _vitrines_trava:
        _vitrines.pop(lead_id, None)


# =====================================================================
# Rotas
# =====================================================================
@avaliacao_publica_bp.get("/avaliacao-inicial")
def formulario():
    # Marca a abertura do formulário: o envio feito antes do tempo mínimo é de robô.
    session[CHAVE_DO_FORMULARIO] = int(time.time())
    return _pagina("avaliacao_publica/formulario.html", **_contexto_do_formulario({}))


@avaliacao_publica_bp.post("/avaliacao-inicial")
@limite_envio
def enviar():
    formulario = request.form
    # Campo isca: invisível para pessoas; robô que preenche tudo é recusado sem ler arquivo.
    if formulario.get("campo_isca"):
        current_app.logger.info("Raio-X público recusado pelo campo isca.")
        return _recusar("Não foi possível enviar. Atualize a página e tente de novo.", {})
    aberto_em = session.get(CHAVE_DO_FORMULARIO)
    agora = int(time.time())
    if type(aberto_em) is not int or agora - aberto_em < _config("TEMPO_MINIMO_SEGUNDOS"):
        # A marca volta para a sessão: o reenvio feito depois do tempo mínimo passa (sem ela, a página devolvida
        # recusaria para sempre e gastaria a cota do IP); o robô continua tendo de esperar.
        session[CHAVE_DO_FORMULARIO] = agora
        mensagem = ("Confira os dados e envie de novo." if type(aberto_em) is int
                    else "O formulário expirou. Confira os dados e envie de novo.")
        return _recusar(mensagem, formulario)
    try:
        contato = _contato(formulario)
    except DadoInvalido as erro:
        return _recusar(str(erro), formulario)
    arquivos = [arquivo for arquivo in request.files.getlist("arquivos") if arquivo is not None and arquivo.filename]
    maximo = _config("MAX_ARQUIVOS")
    if not arquivos:
        return _recusar("Selecione ao menos um relatório de vendas.", formulario)
    if len(arquivos) > maximo:
        return _recusar(f"Envie no máximo {maximo} relatórios por vez.", formulario)
    teto = _config("MAX_MB") * 1024 * 1024
    tamanho = _tamanho_dos_arquivos(arquivos)
    if (_tamanho_declarado() or 0) > teto + FOLGA_DO_ENVELOPE or tamanho is None or tamanho > teto:
        return _recusar(mensagem_de_tamanho(), formulario, 413)
    formato = _formato_recusado(arquivos)
    if formato:
        return _recusar(formato, formulario)
    # Daqui em diante, o banco: só com o pedido válido.
    if not esquema_disponivel():
        return _recusar("O Raio-X pelo site está indisponível no momento. Tente mais tarde.", formulario, 503,
                        ESPERA_SEGUNDOS)
    teto_do_dia = "O limite de avaliações pelo site por hoje foi atingido. Tente de novo amanhã."
    with vaga_de_entrada() as livre:
        if not livre:
            return _ocupado(formulario)
        # Dentro da vaga, o teto diário não deixa dois envios simultâneos passarem juntos do limite. Conta os
        # leads gravados hoje e, neste processo, as tentativas que chegaram ao processo isolado (estas, contadas
        # pelo próprio executor, na hora de chamá-lo).
        if _avaliacoes_de_hoje() >= _config("TETO_DIARIO") or _tentativas_esgotadas():
            return _recusar(teto_do_dia, formulario, 429)
        try:
            lead = _avaliar(arquivos, contato)
        except _TetoDoDiaAtingido:
            db.session.rollback()
            return _recusar(teto_do_dia, formulario, 429)
        except ETLInputError as erro:
            db.session.rollback()
            if erro.code == CODIGO_OCUPADO:
                return _ocupado(formulario)
            return _recusar(erro.safe_message, formulario)
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Raio-X público não gravado (%s).", type(erro).__name__)
            return _recusar("Não foi possível gravar a avaliação. Tente novamente.", formulario, 503, ESPERA_SEGUNDOS)
        lead_id = lead.id
        # A vitrine sai aqui, uma vez, ainda dentro da vaga: o GET do resultado só a desenha. Se falhar, o lead
        # já está gravado; o GET a refaz.
        try:
            geracao = _geracao_atual()
            _guardar_vitrine(lead_id, _contexto_do_resultado(lead), geracao)
        except (SQLAlchemyError, ValueError, KeyError, TypeError, ArithmeticError) as erro:
            db.session.rollback()
            current_app.logger.warning("Vitrine do Raio-X público não montada no envio (%s).", type(erro).__name__)
    session.pop(CHAVE_DO_FORMULARIO, None)
    session[CHAVE_DA_SESSAO] = {"lead": str(lead_id), "em": agora}
    current_app.logger.info("Raio-X público gravado: lead %s.", lead_id)
    return redirect(url_for(".resultado"), 303)


@avaliacao_publica_bp.get("/avaliacao-inicial/resultado")
@limite_resultado
def resultado():
    lead_id = _lead_da_sessao()
    if lead_id is None:
        flash("Envie os relatórios para ver o resultado do Raio-X.", "info")
        return redirect(url_for(".formulario"), 303)
    contexto = _vitrine_guardada(lead_id)
    if contexto is None:
        # Sem a vitrine guardada (processo reiniciado ou que dormiu): refaz uma vez, um pedido por vez.
        if not _recalculo_trava.acquire(blocking=False):
            resposta = _pagina("avaliacao_publica/aguarde.html", 503)
            resposta.headers["Retry-After"] = "5"
            return resposta
        try:
            if not esquema_disponivel():
                flash("O Raio-X pelo site está indisponível no momento. Tente mais tarde.", "warning")
                return redirect(url_for(".formulario"), 303)
            geracao = _geracao_atual()
            lead = db.session.get(LeadAvaliacao, lead_id)
            if lead is None or lead.origem != "site":
                session.pop(CHAVE_DA_SESSAO, None)
                return redirect(url_for(".formulario"), 303)
            contexto = _contexto_do_resultado(lead)
            _guardar_vitrine(lead_id, contexto, geracao)
        finally:
            _recalculo_trava.release()
    # O template recebe só a vitrine (lista branca) e o que o próprio visitante digitou; nunca o lead.
    return _pagina("avaliacao_publica/resultado.html", **contexto)


@avaliacao_publica_bp.post("/avaliacao-inicial/sair")
def sair():
    lead_id = _lead_da_sessao()
    if lead_id is not None:
        _esquecer_vitrine(lead_id)
    session.pop(CHAVE_DA_SESSAO, None)
    session.pop(CHAVE_DO_FORMULARIO, None)
    return redirect(url_for("index"), 303)
