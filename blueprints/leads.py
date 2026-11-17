"""
NEXO - Avaliação de Cenário (blueprint `leads`, só consultores)
================================================================
Rotas (todas admin_required: aqui o consultor envia em nome do lead. A captação pelo site é o Raio-X
público de blueprints/avaliacao_publica.py, atrás do interruptor AVALIACAO_PUBLICA_ATIVA, desligado por
padrão; os leads dele aparecem aqui com a origem "site". Ver Docs/PRE_CADASTRO_LANDING.md):
  GET  /admin/leads                  → a tabela dos leads (empresa, contato, e-mail, WhatsApp, data, aceite dos
                                       termos, situação e caixa resgatável) com o "Ver Avaliação de Cenário" de
                                       cada um, e o formulário de nova avaliação; ?ver=<id> abre o painel daquele
                                       lead ao carregar (a volta do processamento)
  POST /admin/leads                  → lê os relatórios do lead e grava a avaliação
  GET  /admin/leads/<id>             → resumo executivo do caixa resgatável, matriz de triagem, marcas,
                                       matriz por categoria, inteligência de estoque e de preço (com o
                                       simulador pelos mesmos parâmetros da rota JSON), os itens de cada bloco,
                                       o aceite dos termos que o visitante deu na landing (revisão
                                       c8e2a4f6b0d1, gravado pelo microsserviço do Raio-X) e as planilhas que ele
                                       enviou por lá
  GET  /admin/leads/<id>/painel      → o mesmo diagnóstico, sem base.html: o fragmento do painel lateral da
                                       lista (static/js/leads-painel.js), com o contato, o aceite e as planilhas
  GET  /admin/leads/<id>/diagnostico.json → o mesmo diagnóstico em JSON (diagnostico_cenario.montar_payload),
                                       com as séries para gráficos em "visualizacoes"
  GET  /admin/leads/<id>/arquivos/<n> → uma planilha original do lead, do bucket privado do Storage do Supabase
  GET  /admin/leads/<id>/arquivos.zip → todas elas num .zip (armazenamento_leads, só leitura)
  POST /admin/leads/<id>/processar   → baixa as planilhas do lead da landing e grava a avaliação com elas
  POST /admin/leads/<id>/reenviar    → troca os dados do lead pelos de um novo envio
  POST /admin/leads/<id>/retriar     → refaz marcas e triagem a partir dos dados gravados

Regras:
  - Mesma vaga única de upload/processamento das análises e do Raio-X (etl_uploads): ocupada,
    a resposta é 503 com Retry-After e a orientação de enviar de novo; nenhum arquivo é lido.
    O processo isolado tem vaga própria (etl_runtime): ocupada, a resposta é a mesma.
  - Antes do processo isolado (até 2 minutos), a transação da requisição é encerrada: nada fica
    travado nem segura conexão do pooler enquanto os arquivos são lidos.
  - 20 envios por hora por consultor, somando novo envio, reenvio e processamento das planilhas do site; 60
    downloads de planilha por hora por consultor, somando os avulsos e os .zip.
  - Planilhas do lead da landing (armazenamento_leads): o JSON arquivos_storage é conferido antes de qualquer
    pedido ao Storage (só os caminhos deste lead viram pedido), o conteúdo baixado tem de bater com o tamanho e o
    SHA-256 registrados, e nenhuma resposta ou log leva a chave, o endereço ou o caminho do objeto. Sem
    SUPABASE_KEY (ou fora do banco do Supabase), os botões ficam desabilitados com a orientação.
  - As cinco colunas do microsserviço (revisão f2b6d8a4c1e9) ficam fora do mapeamento do ORM: são lidas pela
    tabela, numa consulta só para a lista inteira, e o aceite dos termos também (nunca uma por lead). Banco sem a
    revisão: a tela segue sem o dado.
  - Diagnóstico (tela e JSON): as linhas do lead numa consulta só, sem objetos do ORM, e um cache por
    processo (_CacheDoDiagnostico) cuja chave leva o conteúdo das linhas e os campos do lead: a mesma
    leitura não refaz a triagem, a matriz e a inteligência, e qualquer mudança gera outra chave. A
    entrada vale LEADS_DIAGNOSTICO_CACHE_SEGUNDOS (padrão 600; 0 desliga) e nunca além da retenção; as
    vencidas saem da memória a cada pedido ao processo (de qualquer tela), e o reenvio, a retriagem e o
    expurgo pela tela tiram as entradas antigas na hora, inclusive as que uma leitura concorrente
    terminaria de calcular depois (a geração do lead no cache).
"""

from collections import OrderedDict, namedtuple
from datetime import timedelta
from decimal import Decimal
import io
import json
import re
import threading
import time
from urllib.parse import quote
import uuid
from weakref import WeakKeyDictionary

from flask import (Blueprint, abort, current_app, flash, jsonify, make_response, redirect, render_template,
                   request, url_for)
from flask_login import current_user
from sqlalchemy import String, cast, func, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import FileStorage

import armazenamento_leads
import avaliacao_cenario
import avaliacao_motor
from blueprints.admin import admin_required
import diagnostico_cenario
from configuracoes_conta import TIPOS_DOCUMENTO, DadoInvalido, limpar_telefone, rotulo_versao
from contas_email import normalizar_email
from emails import email_valido
import entitlements
from etl_inputs import ETLInputError
from etl_uploads import vaga_de_entrada
from extensions import chave_limite_usuario, db, limiter
from horario import agora_utc
import inteligencia_estoque
import leads_avaliacao
from leads_avaliacao import RETENCAO_DIAS
import whatsapp_service
from models import (AceiteDocumentoLead, BrainstormingDadosPdv, COLUNAS_DO_MICROSSERVICO, DocumentoLegal,
                    LeadAvaliacao, ROTULOS_FAIXAS_DE_FATURAMENTO, ROTULOS_PLANOS_DE_INTERESSE)
import raio_x
import triagem_cenario

leads_bp = Blueprint("leads", __name__, url_prefix="/admin/leads")

limite_envio = limiter.shared_limit("20 per hour", scope="avaliacao-lead", key_func=chave_limite_usuario)
# Downloads das planilhas originais do lead da landing (avulsas e .zip, somados), por consultor.
limite_download = limiter.shared_limit("60 per hour", scope="planilhas-lead", key_func=chave_limite_usuario)

# O que o visitante escolheu na Etapa 3 do pré-cadastro da landing (models.INTERESSES_DO_LEAD: os botões 1, 2 e 3)
# e a situação dos relatórios na triagem do microsserviço (models.SITUACOES_DOS_ARQUIVOS), em texto curto.
ROTULOS_INTERESSE = {"raio_x": "Raio-X com relatórios", "ajuda_relatorios": "Pediu ajuda com os relatórios",
                     "so_contato": "Só contato, sem Raio-X"}
ROTULOS_STATUS_ARQUIVO = {"sem_arquivo": "sem relatórios", "aprovado": "relatórios aprovados",
                          "parcial": "parte dos relatórios recusada", "recusado": "relatórios recusados"}
# Os ids do fragmento do painel levam este prefixo: não colidem com os da página da lista.
PREFIXO_DO_PAINEL = "painel-"
# Cabeçalho que marca o fragmento do painel: o script só põe no <dialog> a resposta que o traz.
CABECALHO_DO_PAINEL = "X-Nexo-Painel"
MENSAGEM_STORAGE_FALHOU = ("Não foi possível baixar as planilhas do lead no Storage agora. Tente de novo em "
                           "instantes.")

MENSAGEM_OCUPADO = ("Há outro envio ou processamento em andamento. Aguarde alguns segundos e envie de novo: "
                    "nenhum arquivo foi lido.")
MENSAGEM_SEM_ESQUEMA = ("A Avaliação de Cenário depende das migrações 5e1b9c3a7d20, 6a8f2d4c0e13, 7c4e2a9b5d31, "
                        "9b3e5d7f1a28, c5f8a1d3e7b9 e e7a1c3f5b9d2, ainda não aplicadas neste banco: nenhum "
                        "arquivo foi lido.")
# Colunas das revisões mais novas de que a tela depende: a da staging (9b3e5d7f1a28, Etapa 3), a dos
# indicadores executivos do lead (c5f8a1d3e7b9) e a da origem do lead (e7a1c3f5b9d2, captação pelo site),
# que o modelo LeadAvaliacao já lê em toda consulta.
_COLUNA_DO_ESQUEMA = "fim_ultimo_relatorio_com_venda"
_COLUNAS_DO_LEAD = ("indicadores_executivos_json", "origem")
ESPERA_SEGUNDOS = 30
# Código do etl_runtime quando a vaga do processo isolado (_ADMISSION) está ocupada.
CODIGO_OCUPADO = "busy"
ITENS_POR_BLOCO = 30
LEADS_NA_LISTA = 100
_REVERIFICAR_ESQUEMA_SEGUNDOS = 30.0
_esquema_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()


def esquema_disponivel() -> bool:
    """True quando a staging da Etapa 3 (revisão 9b3e5d7f1a28, sobre a 7c4e2a9b5d31) e as colunas do lead
    das revisões c5f8a1d3e7b9 (indicadores executivos) e e7a1c3f5b9d2 (origem) existem neste banco.

    O mesmo critério de validacao_pdv: a presença fica fixa para o engine, e a ausência é
    reavaliada a cada 30 s, para a migração aplicada valer sem reiniciar o processo. Vem antes de
    qualquer consulta às tabelas de lead: código publicado antes da migração mostra um aviso, e não
    erro 500.
    """
    engine = db.session.get_bind()
    estado = _esquema_por_engine.get(engine)
    if estado is True:
        return True
    if estado is not None and time.monotonic() - estado < _REVERIFICAR_ESQUEMA_SEGUNDOS:
        return False
    inspetor = inspect(db.session.connection())
    disponivel = (inspetor.has_table(BrainstormingDadosPdv.__tablename__) and _COLUNA_DO_ESQUEMA in
                  {coluna["name"] for coluna in inspetor.get_columns(BrainstormingDadosPdv.__tablename__)}
                  and inspetor.has_table(LeadAvaliacao.__tablename__) and set(_COLUNAS_DO_LEAD) <=
                  {coluna["name"] for coluna in inspetor.get_columns(LeadAvaliacao.__tablename__)})
    _esquema_por_engine[engine] = True if disponivel else time.monotonic()
    return disponivel


def _sem_esquema():
    flash(MENSAGEM_SEM_ESQUEMA, "warning")
    return redirect(url_for("leads.lista"), 303)


# O expurgo de 30 dias também roda pela própria tela: sem o pg_cron (revisão 6a8f2d4c0e13), ou com
# o servidor dormindo na hora do job, a lista e o detalhe não mostram dado de PDV vencido. No
# máximo uma vez por hora por processo, a mesma regra de `flask leads expurgar-pdv --aplicar`; erro
# de banco nunca derruba a página.
EXPURGO_A_CADA_SEGUNDOS = 3600.0
_expurgo_trava = threading.Lock()
_expurgo_ultimo = {"instante": None}


def expurgo_oportunista() -> None:
    if not _expurgo_trava.acquire(blocking=False):
        return
    try:
        agora = time.monotonic()
        ultimo = _expurgo_ultimo["instante"]
        if ultimo is not None and agora - ultimo < EXPURGO_A_CADA_SEGUNDOS:
            return
        _expurgo_ultimo["instante"] = agora
        try:
            resultado = leads_avaliacao.expurgar_dados_pdv(db.session)
            db.session.commit()
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Expurgo dos dados de PDV dos leads não rodou (%s).", type(erro).__name__)
            return
        if resultado.linhas_removidas or resultado.leads_expirados:
            # O dado de PDV que saiu do banco também sai do cache do diagnóstico deste processo.
            _descartar_do_cache()
            current_app.logger.info("Expurgo dos leads: %s linha(s) de PDV removidas, %s lead(s) expirados.",
                                    resultado.linhas_removidas, resultado.leads_expirados)
    finally:
        _expurgo_trava.release()


# =====================================================================
# Contexto
# =====================================================================
PROTOCOLO_VALIDO = re.compile(r"[0-9A-Fa-f]{8}")


def _contexto_lista(formulario=None, protocolo=None, ver=None) -> dict:
    """A tabela dos leads: os mais recentes (ou os do protocolo), com o aceite dos termos e o que o microsserviço
    gravou lidos numa consulta só para a lista inteira. `ver`: o lead cujo painel abre ao carregar."""
    # Ordem: a última avaliação (a autorização, renovada a cada reenvio e processamento) ou a criação.
    avaliado_em = func.coalesce(LeadAvaliacao.consentimento_em, LeadAvaliacao.criado_em)
    consulta = select(LeadAvaliacao)
    if protocolo:
        # O protocolo do Raio-X público (LeadAvaliacao.protocolo) é o começo do id: acha o lead mesmo fora dos
        # LEADS_NA_LISTA mais recentes (o id em texto: com hífens no PostgreSQL, 32 hexadecimais no SQLite).
        consulta = consulta.where(cast(LeadAvaliacao.id, String).like(f"{protocolo.lower()}%"))
    leads = db.session.scalars(consulta.order_by(avaliado_em.desc(), LeadAvaliacao.criado_em.desc())
                               .limit(LEADS_NA_LISTA)).all()
    ids = [lead.id for lead in leads]
    aceites = _aceites_dos_leads(ids)
    sites = _dados_do_site(ids)
    conformidades = {lead.id: _conformidade(lead, None if aceites is None else aceites.get(lead.id, []),
                                            (sites or {}).get(lead.id), sites is not None) for lead in leads}
    return {"leads": leads, "formulario": formulario or {}, "max_arquivos": raio_x.MAX_ARQUIVOS,
            "protocolo": protocolo or "",
            "rotulos_blocos": triagem_cenario.ROTULOS_DOS_BLOCOS, "retencao_dias": RETENCAO_DIAS,
            "disponivel": True, "parados": {lead.id: _parado_da_lista(lead) for lead in leads},
            "sites": sites or {}, "conformidades": conformidades,
            "rotulos_interesse": ROTULOS_INTERESSE, "rotulos_status_arquivo": ROTULOS_STATUS_ARQUIVO,
            "painel_abrir": url_for("leads.painel", lead_id=ver) if ver else "",
            "painel_pagina": url_for("leads.detalhe", lead_id=ver) if ver else "",
            "total_leads": db.session.scalar(select(func.count()).select_from(LeadAvaliacao))}


def _parado_da_lista(lead: LeadAvaliacao):
    """O capital parado que a coluna "Caixa resgatável" mostra junto do potencial recuperável: a menor faixa do
    resumo gravado da inteligência (60 dias ou mais sem venda), sem refazer nada. None sem estoque informado. A
    triagem, as marcas, a saúde e o plano sugerido de cada lead estão no painel (o diagnóstico completo)."""
    try:
        inteligencia = _resumo_inteligencia(lead)
        if not (inteligencia and inteligencia["com_estoque"] and inteligencia.get("capital_parado")):
            return None
        dias = min(inteligencia["capital_parado"], key=int)
        # Parcial só quando algum produto com custo e sem saldo informado já está nessa faixa sem venda: o
        # capital dele falta na soma (quem ainda vende, ou não tem custo, não entraria nela com saldo nenhum).
        return {"dias": int(dias), "capital": inteligencia["capital_parado"][dias],
                "parcial": bool(inteligencia.get("itens_parados_sem_estoque_informado"))}
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
        # Um lead com gravação que nenhuma validação previu não derruba a lista dos outros.
        return None


def _objeto_json(texto):
    """O JSON gravado se for um objeto com números finitos; qualquer outra coisa (lista, texto, JSON
    inválido, NaN, aninhamento sem fim) vale nada (diagnostico_cenario.ler_objeto)."""
    return diagnostico_cenario.ler_objeto(texto)


def _resumo(lead: LeadAvaliacao, bloco: str):
    """O resumo gravado de um bloco, só se tiver o formato que a tela usa (itens inteiro, totais em
    texto decimal, motivos {código: contagem}; diagnostico_cenario.resumo_de_bloco_valido); gravação fora
    do formato vale nada, sem erro 500."""
    return diagnostico_cenario.resumo_de_bloco_valido(_objeto_json(getattr(lead, f"resumo_{bloco}_json")))


def _resumo_inteligencia(lead: LeadAvaliacao):
    """O resumo numérico da inteligência gravado no lead, só se for da versão atual e tiver o formato
    que as telas usam (diagnostico_cenario.resumo_inteligencia_valido); senão, nada."""
    return diagnostico_cenario.resumo_inteligencia_valido(_objeto_json(lead.resumo_inteligencia_json))


def _lead_para_payload(lead: LeadAvaliacao) -> dict:
    """Os campos do lead que o diagnóstico usa. Nunca o contato (nome, e-mail, WhatsApp, loja) nem
    quem registrou."""
    return {
        "id": lead.id, "status": lead.status, "versao_regras": lead.versao_regras,
        "versao_triagem": lead.versao_triagem, "periodo_inicio": lead.periodo_inicio,
        "periodo_fim": lead.periodo_fim, "arquivos": lead.arquivos,
        "score_saude_cadastral": lead.score_saude_cadastral,
        "plano_recomendado_codigo": lead.plano_recomendado_codigo,
        "vazamento_total_reais": lead.vazamento_total_reais,
        "itens_venda_sem_custo": lead.itens_venda_sem_custo,
        "itens_venda_abaixo_custo": lead.itens_venda_abaixo_custo,
        "itens_omissao_erp": lead.itens_omissao_erp,
        "resumos_json": {bloco: getattr(lead, f"resumo_{bloco}_json") for bloco in triagem_cenario.BLOCOS},
        "meta_json": lead.meta_json, "limiares_triagem_json": lead.limiares_triagem_json,
        "resumo_inteligencia_json": lead.resumo_inteligencia_json,
    }


# O diagnóstico de um lead, sem gravar nada: o payload, quantas linhas de PDV há, quando expiram, se são
# da versão atual das regras e, para a tela de um lead de outra versão, os itens no bloco gravado.
Diagnostico = namedtuple("Diagnostico", "payload linhas_pdv expira_em linhas_atuais itens_gravados")
CACHE_PADRAO_SEGUNDOS = 600
CACHE_MAXIMO = 8


class _CacheDoDiagnostico:
    """Os diagnósticos mais recentes da aplicação, neste processo (até CACHE_MAXIMO; o menos usado sai
    primeiro). A chave leva o lead, a assinatura do conteúdo das linhas (avaliacao_cenario.linhas_do_lead:
    muda com qualquer valor de qualquer linha), os campos do lead que o payload usa, o limite, os
    parâmetros e a versão do diagnóstico: o reenvio, a retriagem, o expurgo e qualquer mudança de dado
    geram outra chave. A entrada vale o TTL e nunca além da retenção das linhas. Fica na memória do
    processo, como a resposta que ela evita refazer, e só os consultores a leem (admin_required)."""

    # Gerações guardadas por lead (cada descarte soma 1); passado o teto, todas recomeçam pela global.
    GERACOES_MAXIMO = 1000

    def __init__(self, maximo: int = CACHE_MAXIMO):
        self._itens = OrderedDict()
        self._trava = threading.Lock()
        self.maximo = maximo
        self._geracao_global = 0
        self._geracoes = {}

    def geracao(self, lead_id) -> tuple:
        """A geração do lead no cache: muda a cada descarte dele e a cada limpeza. Lida antes da carga,
        impede que uma leitura concorrente guarde o dado de antes de um descarte."""
        with self._trava:
            return self._geracao_global, self._geracoes.get(str(lead_id), 0)

    def varrer(self, agora) -> None:
        with self._trava:
            self._varrer(agora)

    def _varrer(self, agora) -> None:
        """Tira da memória toda entrada vencida (não só a pedida): o dado de PDV não fica guardado à espera
        de uma leitura da mesma chave, que depois do expurgo ou do reenvio nunca mais vem."""
        for chave in [chave for chave, (_diagnostico, validade) in self._itens.items() if agora >= validade]:
            del self._itens[chave]

    def obter(self, chave, agora):
        with self._trava:
            self._varrer(agora)
            item = self._itens.get(chave)
            if item is None:
                return None
            self._itens.move_to_end(chave)
            return item[0]

    def guardar(self, chave, diagnostico, validade, agora, geracao=None) -> None:
        with self._trava:
            self._varrer(agora)
            if geracao is not None and geracao != (self._geracao_global, self._geracoes.get(chave[0], 0)):
                # Um descarte aconteceu durante o cálculo: o diagnóstico é de antes dele e não é guardado.
                return
            self._itens[chave] = (diagnostico, validade)
            self._itens.move_to_end(chave)
            while len(self._itens) > self.maximo:
                self._itens.popitem(last=False)

    def descartar_lead(self, lead_id) -> int:
        """Tira as entradas de um lead (a chave começa pelo id dele); devolve quantas saíram."""
        with self._trava:
            if len(self._geracoes) >= self.GERACOES_MAXIMO:
                self._geracoes.clear()
                self._geracao_global += 1
            self._geracoes[str(lead_id)] = self._geracoes.get(str(lead_id), 0) + 1
            chaves = [chave for chave in self._itens if chave[0] == str(lead_id)]
            for chave in chaves:
                del self._itens[chave]
            return len(chaves)

    def limpar(self) -> None:
        with self._trava:
            self._geracao_global += 1
            self._itens.clear()

    def __len__(self) -> int:
        with self._trava:
            return len(self._itens)


def _descartar_do_cache(lead_id=None) -> None:
    """Tira do cache deste processo os diagnósticos de um lead (reenvio e retriagem) ou todos (expurgo):
    o dado antigo não fica na memória à espera do TTL. Os outros processos só o servem até a validade.
    Leva junto a vitrine guardada do Raio-X público do mesmo lead (ou todas), que o visitante veria."""
    # Import tardio: blueprints.avaliacao_publica importa deste módulo.
    from blueprints import avaliacao_publica
    avaliacao_publica.descartar_vitrine(lead_id)
    cache = current_app.extensions.get("nexo_diagnostico_leads")
    if cache is None:
        return
    if lead_id is None:
        cache.limpar()
    else:
        cache.descartar_lead(lead_id)


@leads_bp.before_app_request
def _varrer_cache_do_diagnostico() -> None:
    """A cada pedido ao processo (de qualquer tela, não só das de leads), as entradas vencidas do cache
    saem da memória: depois de um expurgo feito pelo pg_cron ou pelo comando, o dado de PDV não fica
    guardado num processo com tráfego. No máximo 8 comparações; um erro aqui nunca derruba o pedido."""
    cache = current_app.extensions.get("nexo_diagnostico_leads")
    if cache is None:
        return
    try:
        cache.varrer(agora_utc())
    except Exception as erro:  # noqa: BLE001 - a limpeza é auxiliar; o pedido segue
        current_app.logger.warning("Varredura do cache do diagnóstico não rodou (%s).", type(erro).__name__)


def _cache_do_diagnostico():
    """(cache, segundos) da aplicação atual; LEADS_DIAGNOSTICO_CACHE_SEGUNDOS = 0 desliga o cache."""
    segundos = current_app.config.get("LEADS_DIAGNOSTICO_CACHE_SEGUNDOS", CACHE_PADRAO_SEGUNDOS)
    if type(segundos) is not int or segundos <= 0:
        return None, 0
    return current_app.extensions.setdefault("nexo_diagnostico_leads", _CacheDoDiagnostico()), segundos


def _diagnostico(lead: LeadAvaliacao, limite: int, parametros=None) -> Diagnostico:
    """O diagnóstico do lead (Diagnostico), sem gravar nada; do cache quando nada mudou."""
    cache, segundos = _cache_do_diagnostico()
    # A geração antes da carga: um descarte do lead durante o cálculo impede guardar o dado de antes dele.
    geracao = cache.geracao(lead.id) if cache is not None else None
    linhas, assinatura = avaliacao_cenario.linhas_do_lead(lead.id)
    agora = agora_utc()
    dados_do_lead = _lead_para_payload(lead)
    chave = None
    if cache is not None:
        chave = (str(lead.id), assinatura, len(linhas), json.dumps(dados_do_lead, default=str, sort_keys=True),
                 max(1, min(int(limite), diagnostico_cenario.ITENS_POR_BLOCO_MAXIMO)),
                 json.dumps(parametros.para_dict(), sort_keys=True) if parametros is not None else None,
                 diagnostico_cenario.VERSAO_DIAGNOSTICO)
        guardado = cache.obter(chave, agora)
        if guardado is not None:
            return guardado
    payload = diagnostico_cenario.montar_payload(dados_do_lead, linhas, agora=agora, retencao_dias=RETENCAO_DIAS,
                                                 limite=limite, parametros_inteligencia=parametros)
    expira_em = min((l["criado_em"] for l in linhas), default=None)
    expira_em = expira_em + timedelta(days=RETENCAO_DIAS) if expira_em is not None else None
    linhas_atuais = bool(linhas) and {l["versao_regras"] for l in linhas} == {avaliacao_motor.VERSAO_REGRAS_LEAD}
    itens = _itens_gravados(linhas) if payload["situacao"] == "desatualizado" else None
    diagnostico = Diagnostico(payload, len(linhas), expira_em, linhas_atuais, itens)
    if cache is not None:
        validade = agora + timedelta(seconds=segundos)
        if expira_em is not None and payload["situacao"] != "expirado":
            # Com dado de PDV no payload, a entrada nunca passa da retenção das linhas.
            validade = min(validade, expira_em)
        cache.guardar(chave, diagnostico, validade, agora, geracao)
    return diagnostico


def _itens_gravados(linhas: list) -> dict:
    """Os itens no bloco gravado (bloco_triagem), sem motivos nem impacto: a tela de um lead com a
    triagem ou as regras de outra versão, enquanto as linhas existem. Ordem: potencial recuperável
    gravado, faturamento e nome. O nome e o código são os do ano mais recente."""
    itens = {bloco: [] for bloco in triagem_cenario.BLOCOS}
    por_produto: dict = {}
    for linha in linhas:  # em ordem de ano
        item = por_produto.setdefault(linha["chave_produto"], {
            "produto": linha["descricao_produto"], "codigo": linha["codigo_produto"], "categoria": None, "anos": [],
            "faturamento": Decimal("0"), "potencial_recuperavel": Decimal("0"), "impacto": None,
            "a_confirmar": False, "motivos": [], "bloco": linha["bloco_triagem"],
        })
        item["produto"], item["codigo"] = linha["descricao_produto"], linha["codigo_produto"] or item["codigo"]
        item["anos"].append(linha["ano_exercicio"])
        item["faturamento"] += Decimal(linha["valor_venda_bruto"])
        item["potencial_recuperavel"] += Decimal(linha["vazamento_estimado_reais"])
    for item in por_produto.values():
        if item["bloco"] in itens:
            itens[item["bloco"]].append(item)
    for bloco, lista in itens.items():
        lista.sort(key=lambda item: (-item["potencial_recuperavel"], -item["faturamento"], item["produto"]))
        itens[bloco] = lista[:ITENS_POR_BLOCO]
    return itens


# Parâmetros do simulador e da auditoria de preço que o consultor pode trocar pela consulta
# (?descontos=0.1,0.25&fracao_liquidada=0.5&markup_informado=0.35&cobertura_alvo_dias=45).
_PARAMETROS_DA_CONSULTA = ("descontos", "fracao_liquidada", "markup_informado", "cobertura_alvo_dias")


def _parametros_da_consulta(args):
    """Os parâmetros da inteligência pedidos na consulta, validados (ValueError com a mensagem)."""
    ajustes = {nome: args.get(nome) for nome in _PARAMETROS_DA_CONSULTA if args.get(nome) not in (None, "")}
    if not ajustes:
        return None
    return inteligencia_estoque.PARAMETROS_PADRAO.com_ajustes(ajustes)


def _limiares_legiveis(lead: LeadAvaliacao):
    try:
        return avaliacao_cenario.limiares_do_lead(lead)
    except ValueError:
        return None


# A tabela do aceite do lead (revisão c8e2a4f6b0d1) pode não existir ainda: o mesmo critério de esquema_disponivel,
# com a presença fixa para o engine e a ausência reavaliada a cada 30 s.
_aceite_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()


def _aceite_dos_termos_disponivel() -> bool:
    engine = db.session.get_bind()
    estado = _aceite_por_engine.get(engine)
    if estado is True:
        return True
    if estado is not None and time.monotonic() - estado < _REVERIFICAR_ESQUEMA_SEGUNDOS:
        return False
    presente = inspect(db.session.connection()).has_table(AceiteDocumentoLead.__tablename__)
    _aceite_por_engine[engine] = True if presente else time.monotonic()
    return presente


def _aceites_dos_leads(ids):
    """{id do lead: aceites} dos leads `ids`, numa consulta só (a lista inteira nunca pede um por lead): os aceites
    dos Termos de Uso e da Política de Privacidade que o visitante deu na landing (gravados pelo microsserviço do
    Raio-X), cada um com o documento, o rótulo da versão aceita, o instante e o IP, na ordem dos documentos. O lead
    sem aceite fica fora do dicionário. None quando a tabela ainda não existe neste banco ou a leitura falha (num
    SAVEPOINT: no PostgreSQL, a consulta que falha não aborta a transação da página, que segue sem o dado)."""
    ids = list(ids)
    if not ids:
        return {}
    ordem = {tipo: indice for indice, tipo in enumerate(TIPOS_DOCUMENTO)}
    try:
        with db.session.begin_nested():
            if not _aceite_dos_termos_disponivel():
                return None
            linhas = db.session.execute(
                select(AceiteDocumentoLead.id_lead, DocumentoLegal.tipo, DocumentoLegal.versao_maior,
                       DocumentoLegal.versao_menor, AceiteDocumentoLead.aceito_em, AceiteDocumentoLead.ip)
                .join(DocumentoLegal, DocumentoLegal.id_documento == AceiteDocumentoLead.id_documento)
                .where(AceiteDocumentoLead.id_lead.in_(ids))
            ).all()
    except SQLAlchemyError as erro:
        _aceite_por_engine.pop(db.session.get_bind(), None)
        current_app.logger.warning("Aceite dos termos do lead não lido (%s).", type(erro).__name__)
        return None
    por_lead = {}
    for linha in sorted(linhas, key=lambda linha: (ordem.get(linha.tipo, len(ordem)), linha.aceito_em)):
        por_lead.setdefault(linha.id_lead, []).append({
            "documento": TIPOS_DOCUMENTO.get(linha.tipo, linha.tipo),
            "versao": rotulo_versao(linha.versao_maior, linha.versao_menor),
            "aceito_em": linha.aceito_em, "ip": linha.ip})
    return por_lead


def _aceites_dos_termos(lead: LeadAvaliacao):
    """Os aceites dos termos de um lead (_aceites_dos_leads): lista vazia sem aceite; None sem a tabela ou com
    falha na leitura."""
    aceites = _aceites_dos_leads([lead.id])
    return None if aceites is None else aceites.get(lead.id, [])


# As cinco colunas que só o microsserviço do Raio-X grava (revisão f2b6d8a4c1e9) estão na tabela, mas fora do
# mapeamento do ORM (models.LeadAvaliacao): são lidas pela tabela, com o mesmo critério de esquema do aceite.
DadosDoSite = namedtuple("DadosDoSite", COLUNAS_DO_MICROSSERVICO)
_site_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()


def _colunas_do_site_disponiveis() -> bool:
    engine = db.session.get_bind()
    estado = _site_por_engine.get(engine)
    if estado is True:
        return True
    if estado is not None and time.monotonic() - estado < _REVERIFICAR_ESQUEMA_SEGUNDOS:
        return False
    colunas = {coluna["name"] for coluna in inspect(db.session.connection()).get_columns(LeadAvaliacao.__tablename__)}
    presentes = set(COLUNAS_DO_MICROSSERVICO) <= colunas
    _site_por_engine[engine] = True if presentes else time.monotonic()
    return presentes


def _dados_do_site(ids):
    """{id do lead: DadosDoSite} dos leads `ids`, numa consulta só: o interesse, a situação dos arquivos, o JSON dos
    caminhos no Storage e o resumo do aceite (termos_aceitos_em e termos_versao), nulos nos leads que não vieram do
    microsserviço. None quando as colunas ainda não existem neste banco ou a leitura falha (num SAVEPOINT, como o
    aceite): a tela segue sem o dado, nunca com erro 500."""
    ids = list(ids)
    if not ids:
        return {}
    tabela = LeadAvaliacao.__table__
    try:
        with db.session.begin_nested():
            if not _colunas_do_site_disponiveis():
                return None
            linhas = db.session.execute(
                select(tabela.c.id, *(tabela.c[nome] for nome in COLUNAS_DO_MICROSSERVICO))
                .where(tabela.c.id.in_(ids))
            ).all()
    except SQLAlchemyError as erro:
        _site_por_engine.pop(db.session.get_bind(), None)
        current_app.logger.warning("Dados do microsserviço dos leads não lidos (%s).", type(erro).__name__)
        return None
    return {linha[0]: DadosDoSite(*linha[1:]) for linha in linhas}


def _planilhas_do_lead(lead: LeadAvaliacao) -> list:
    """As planilhas válidas que o lead enviou pela landing (armazenamento_leads.planilhas_do_lead); vazio sem elas,
    sem as colunas no banco ou com o JSON fora do formato."""
    sites = _dados_do_site([lead.id])
    site = sites.get(lead.id) if sites else None
    return armazenamento_leads.planilhas_do_lead(lead.id, site.arquivos_storage) if site else []


def _conformidade(lead: LeadAvaliacao, aceites, site, colunas_lidas: bool) -> dict:
    """O indicador do aceite dos termos de um lead, na lista e no painel, pelo "tipo":
      - "aceite": as versões que o visitante aceitou de cada documento (aceite_documento_lead), com o instante e o
        IP de cada uma;
      - "termos": sem linha de aceite, o resumo gravado no lead pelo microsserviço (termos_versao e o instante);
      - "consultor": lead do consultor sem aceite: quem confirmou a autorização do uso dos relatórios foi ele;
      - "visitante": lead do Raio-X público sem aceite dos termos, com a versão do texto da autorização que o
        visitante marcou;
      - "sem_aceite": lead do site sem nenhum registro;
      - "indisponivel": banco sem a tabela do aceite e sem as colunas do microsserviço (a tela mostra "—")."""
    if aceites:
        return {"tipo": "aceite", "aceites": aceites, "instante": max(aceite["aceito_em"] for aceite in aceites),
                "ips": list(dict.fromkeys(aceite["ip"] for aceite in aceites if aceite["ip"]))}
    if site is not None and (site.termos_versao or site.termos_aceitos_em):
        return {"tipo": "termos", "versao": site.termos_versao, "instante": site.termos_aceitos_em}
    # O reenvio do consultor troca a autorização do visitante pela dele (avaliacao_cenario._gravar).
    if lead.origem == "consultor" or (lead.registrado_por is not None and not lead.versao_autorizacao):
        return {"tipo": "consultor", "instante": lead.consentimento_em}
    if aceites is None and not colunas_lidas:
        return {"tipo": "indisponivel"}
    if lead.versao_autorizacao:
        return {"tipo": "visitante", "versao": lead.versao_autorizacao, "instante": lead.consentimento_em}
    return {"tipo": "sem_aceite"}


def _contexto_detalhe(lead: LeadAvaliacao, parametros=None, erro_parametros=None) -> dict:
    """Resumo gravado e, enquanto os dados de PDV existirem e forem da versão atual, a matriz por
    categoria, a inteligência de estoque e de preço e os itens de cada bloco com os motivos (o mesmo
    diagnóstico da rota JSON, com os mesmos parâmetros do simulador). Também o aceite dos termos, o que o
    microsserviço gravou (interesse, situação e planilhas no Storage) e se o Storage está configurado. Serve à
    página do lead e ao fragmento do painel da lista, que mostram o mesmo diagnóstico."""
    diagnostico = _diagnostico(lead, ITENS_POR_BLOCO, parametros)
    payload = diagnostico.payload
    meta = _objeto_json(lead.meta_json) or {}
    atual = payload["situacao"] == "atual"
    if atual:
        itens = {bloco: payload["blocos"][bloco]["itens"] for bloco in triagem_cenario.BLOCOS}
    elif payload["situacao"] == "desatualizado":
        # Linhas dentro da retenção, mas de outra versão: o bloco gravado, sem os motivos refeitos.
        itens = diagnostico.itens_gravados
    else:
        itens = {bloco: [] for bloco in triagem_cenario.BLOCOS}
    limiares = _limiares_legiveis(lead)
    inteligencia = payload["inteligencia"] if atual else None
    efetivos = parametros or inteligencia_estoque.PARAMETROS_PADRAO
    resumos = {bloco: _resumo(lead, bloco) for bloco in triagem_cenario.BLOCOS}
    resumo_inteligencia = _resumo_inteligencia(lead)
    # Como no caixa resgatável: com o cálculo atual, só ele vale; sem ele, o resumo gravado. A nota de soma
    # parcial conta só os produtos sem saldo e com custo: sem custo, ficariam fora das parcelas com saldo ou sem.
    if inteligencia is not None:
        itens_sem_estoque_com_custo = (inteligencia["estoque"]["itens_sem_estoque_com_custo"]
                                       if inteligencia["estoque"] is not None else None)
    else:
        itens_sem_estoque_com_custo = (resumo_inteligencia or {}).get("itens_sem_estoque_com_custo")
    # A tela mostra o diagnóstico inteiro (e o contato): a leitura fica no log, como na rota JSON, só com os
    # identificadores, inclusive quando vem do cache.
    current_app.logger.info("Diagnóstico do lead %s lido pelo consultor %s.", lead.id, current_user.id_usuario)
    aceites_termos = _aceites_dos_termos(lead)
    sites = _dados_do_site([lead.id])
    site = sites.get(lead.id) if sites else None
    planilhas = armazenamento_leads.planilhas_do_lead(lead.id, site.arquivos_storage) if site else []
    return {
        "itens_sem_estoque_com_custo": itens_sem_estoque_com_custo,
        "caixa_resgatavel": _caixa_resgatavel(payload["visualizacoes"] or {}),
        "matriz_triagem": _matriz_triagem(resumos, limiares),
        "inteligencia": inteligencia, "resumo_inteligencia": resumo_inteligencia,
        "visualizacoes": payload["visualizacoes"] or {}, "naturezas_do_caixa": diagnostico_cenario.NATUREZAS_DO_CAIXA,
        "erro_parametros": erro_parametros,
        # Os valores do formulário do simulador: os parâmetros em uso, no formato da consulta.
        "simulador_descontos": ",".join(str(d) for d in efetivos.descontos),
        "simulador_fracao": str(efetivos.fracao_liquidada),
        "lead": lead, "meta": meta, "itens": itens, "linhas_pdv": diagnostico.linhas_pdv,
        "expira_em": diagnostico.expira_em,
        "triagem_atual": atual or payload["situacao"] == "expirado",
        "linhas_atuais": diagnostico.linhas_atuais, "situacao": payload["situacao"], "avisos": payload["avisos"],
        "versao_triagem_atual": triagem_cenario.VERSAO_TRIAGEM, "triagem_da_versao_atual":
            lead.versao_triagem == triagem_cenario.VERSAO_TRIAGEM,
        # Os rótulos dos KPIs pela semântica da versão gravada (a troca de versão não volta aos de antes da Etapa 2).
        "triagem_com_marcas_da_etapa_2": lead.versao_triagem in triagem_cenario.VERSOES_COM_MARCAS_DA_ETAPA_2,
        "custo_a_confirmar_por_ano": lead.versao_triagem in triagem_cenario.VERSOES_COM_CUSTO_A_CONFIRMAR_POR_ANO,
        "resumos": resumos,
        "rotulo_do_motivo": lambda codigo: triagem_cenario.rotulo_do_motivo(codigo, limiares),
        "rotulos_blocos": triagem_cenario.ROTULOS_DOS_BLOCOS,
        "totais": payload["totais"], "categorias": payload["categorias"],
        "categorias_publicadas": payload["categorias_publicadas"],
        "max_arquivos": raio_x.MAX_ARQUIVOS, "itens_por_bloco": ITENS_POR_BLOCO, "retencao_dias": RETENCAO_DIAS,
        # O que o visitante escolheu no Raio-X público (lead com origem "site").
        "rotulos_planos_interesse": ROTULOS_PLANOS_DE_INTERESSE, "rotulos_faixas_faturamento": ROTULOS_FAIXAS_DE_FATURAMENTO,
        # O aceite dos termos na landing (o bloco "Aceite dos termos"); None sem a tabela.
        "aceites_termos": aceites_termos,
        "conformidade": _conformidade(lead, aceites_termos, site, sites is not None),
        # O que o microsserviço gravou (None no lead do consultor e no banco sem a revisão f2b6d8a4c1e9) e as
        # planilhas válidas no Storage, com o estado da configuração para os botões de baixar e processar.
        "site": site, "planilhas": planilhas,
        "armazenamento_pronto": armazenamento_leads.do_app(current_app.config) is not None,
        "mensagem_sem_armazenamento": armazenamento_leads.MENSAGEM_SEM_CONFIGURACAO,
        "rotulos_interesse": ROTULOS_INTERESSE, "rotulos_status_arquivo": ROTULOS_STATUS_ARQUIVO,
        # Lead que ainda não foi processado (o da landing, pendente): o painel mostra isso no lugar dos números.
        "sem_diagnostico": (lead.arquivos is None and lead.score_saude_cadastral is None
                            and not diagnostico.linhas_pdv),
    }


def _caixa_resgatavel(visualizacoes: dict) -> list:
    """As parcelas do caixa resgatável (visualizacoes["caixa_resgatavel"]) para a tela: rótulo, valor,
    o texto da natureza e a proporção da barra (0 a 100, contra a maior parcela). Nenhum valor: vazio."""
    caixa = visualizacoes.get("caixa_resgatavel")
    if not caixa:
        return []
    valores = caixa["series"][0]["valores"]
    if all(valor is None for valor in valores):
        return []
    maior = max((valor for valor in valores if valor), default=0)
    return [{"rotulo": rotulo, "valor": valor, "natureza": diagnostico_cenario.NATUREZAS_DO_CAIXA[natureza],
             "proporcao": round(100 * valor / maior, 1) if valor and maior else 0}
            for rotulo, valor, natureza in zip(caixa["rotulos"], valores, caixa["naturezas"])]


def _matriz_triagem(resumos: dict, limiares) -> list:
    """Uma linha por bloco (Urgência, Atenção, Eletiva) do resumo gravado: produtos, faturamento,
    potencial recuperável e os três motivos mais frequentes. Resumo fora do formato: a linha sem números."""
    linhas = []
    for bloco in triagem_cenario.BLOCOS:
        resumo = resumos.get(bloco)
        motivos = sorted((resumo or {}).get("motivos", {}).items(), key=lambda par: (-par[1], par[0]))[:3]
        linhas.append({
            "bloco": bloco, "rotulo": triagem_cenario.ROTULOS_DOS_BLOCOS[bloco],
            "itens": resumo["itens"] if resumo else None,
            "faturamento": resumo.get("faturamento") if resumo else None,
            "recuperavel": resumo.get("recuperavel") if resumo else None,
            "motivos": [(triagem_cenario.rotulo_do_motivo(codigo, limiares), quantos) for codigo, quantos in motivos],
        })
    return linhas


def _ocupado(template: str, contexto: dict):
    """503 com Retry-After: a vaga única está com outro envio ou processamento."""
    flash(MENSAGEM_OCUPADO, "warning")
    resposta = make_response(render_template(template, **contexto), 503)
    resposta.headers["Retry-After"] = str(ESPERA_SEGUNDOS)
    return resposta


# =====================================================================
# Contato do lead nas telas
# =====================================================================
@leads_bp.app_template_filter("numero_whatsapp")
def numero_whatsapp(valor) -> str:
    """O WhatsApp do lead como o wa.me o aceita (só dígitos, com o DDI 55), pela mesma higienização dos avisos pela
    Evolution (whatsapp_service.higienizar_telefone); vazio quando o número é ambíguo ou inválido. A macro
    link_whatsapp de _macros.html confere de novo antes de desenhar o link."""
    return whatsapp_service.higienizar_telefone(valor) or ""


@leads_bp.app_template_filter("endereco_mailto")
def endereco_mailto(email) -> str:
    """O href mailto: do e-mail do lead, com os caracteres que mudariam o sentido do endereço (?, #, %, &, espaço)
    codificados; a arroba e o ponto ficam."""
    return "mailto:" + quote(str(email or ""), safe="@.+-_")


# =====================================================================
# Respostas das planilhas e do painel
# =====================================================================
def _do_painel(resposta):
    """Marca a resposta como fragmento do painel (static/js/leads-painel.js só põe no <dialog> o que vier
    marcado) e sem cache: o fragmento leva o contato e o diagnóstico do lead."""
    resposta.headers[CABECALHO_DO_PAINEL] = "1"
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


def _resposta_curta(texto: str, status: int):
    """Texto puro e curto, sem detalhe interno: o que um download que falhou mostra."""
    resposta = make_response(texto, status)
    resposta.headers["Content-Type"] = "text/plain; charset=utf-8"
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


def _anexo(conteudo: bytes, nome: str, tipo: str):
    """O arquivo para baixar: Content-Disposition attachment com o nome em ASCII e em UTF-8 (RFC 5987), o tipo do
    formato, sem farejar o tipo nem guardar em cache."""
    seguro = nome.encode("ascii", "ignore").decode("ascii").replace('"', "").replace("\\", "") or "arquivo"
    resposta = make_response(conteudo)
    resposta.headers["Content-Type"] = tipo
    resposta.headers["Content-Disposition"] = (f'attachment; filename="{seguro}"; '
                                               f"filename*=UTF-8''{quote(nome, safe='')}")
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


def _preparar_download(lead_id):
    """(lead, planilhas, None) para as rotas de download, ou (None, None, resposta curta): banco sem as migrações
    (503), lead inexistente, com os dados expirados ou sem planilha válida (404). Passada a retenção, a planilha
    original também não sai (LGPD): o mesmo que a tela mostra, sem link de baixar."""
    if not esquema_disponivel():
        resposta = _resposta_curta(MENSAGEM_SEM_ESQUEMA, 503)
        resposta.headers["Retry-After"] = str(ESPERA_SEGUNDOS)
        return None, None, resposta
    lead = db.session.get(LeadAvaliacao, lead_id)
    if lead is None or lead.status == "expirado":
        abort(404)
    planilhas = _planilhas_do_lead(lead)
    if not planilhas:
        abort(404)
    return lead, planilhas, None


def _armazenamento_ou_resposta():
    """(cliente do Storage, None) ou (None, 503 com a orientação de configurar SUPABASE_KEY)."""
    armazenamento = armazenamento_leads.do_app(current_app.config)
    if armazenamento is None:
        return None, _resposta_curta(armazenamento_leads.MENSAGEM_SEM_CONFIGURACAO + ".", 503)
    return armazenamento, None


def _uuid_da_consulta(valor):
    """O uuid de um parâmetro da consulta (?ver=), ou None se não for um."""
    if not isinstance(valor, str) or not valor or len(valor) > 64:
        return None
    try:
        return uuid.UUID(valor.strip())
    except ValueError:
        return None


# =====================================================================
# Formulário
# =====================================================================
def _contato_do_formulario(formulario) -> dict:
    nome = " ".join((formulario.get("nome_contato") or "").split())
    loja = " ".join((formulario.get("nome_loja") or "").split())
    if not nome or not loja:
        raise DadoInvalido("Informe o nome do contato e o nome da loja.")
    if len(nome) > 120 or len(loja) > 120:
        raise DadoInvalido("O nome do contato e o da loja têm até 120 caracteres.")
    email = normalizar_email(formulario.get("email"))
    if not email_valido(email):
        raise DadoInvalido("Informe um e-mail válido.")
    telefone = limpar_telefone(formulario.get("whatsapp") or "")
    whatsapp = None
    if telefone:
        whatsapp = ("+" if telefone.startswith("+") else "") + "".join(c for c in telefone if c.isdigit())
    _exigir_autorizacao(formulario)
    return {"nome_contato": nome, "email": email, "whatsapp": whatsapp, "nome_loja": loja}


def _exigir_autorizacao(formulario) -> None:
    if formulario.get("autorizacao") != "1":
        raise DadoInvalido("Confirme que o lead autorizou o uso destes relatórios na avaliação.")


def _avaliar(arquivos, *, contato=None, lead_id=None) -> LeadAvaliacao:
    """Lê os relatórios no processo isolado e grava a avaliação (com commit)."""
    prata = entitlements.catalogo().nivel("PRATA")
    meses_prata = prata.limite_historico_meses if prata is not None else None
    consultor = current_user.id_usuario
    # Até 2 minutos no processo isolado: a transação de leitura termina antes, sem trava nem
    # conexão parada no pooler. A gravação abre outra, curta, depois.
    db.session.rollback()
    resultado = avaliacao_cenario.processar(arquivos)
    lead = avaliacao_cenario.registrar(resultado, contato=contato, lead_id=lead_id, registrado_por=consultor,
                                       meses_prata=meses_prata)
    db.session.commit()
    # No reenvio, os dados que o envio trocou saem do cache deste processo.
    _descartar_do_cache(lead.id)
    return lead


def _avisar_por_whatsapp(lead: LeadAvaliacao, formulario) -> None:
    """Relatório pronto pelo WhatsApp do lead, quando cadastrado E quando o consultor confirmou que o
    lead aceitou o aviso (caixa "aviso_whatsapp", desmarcada por padrão: a autorização de uso dos
    relatórios não cobre o contato por WhatsApp). Depois do commit e fora da vaga de entrada, com a
    fila de fundo do notificador fazendo o HTTP. O valor é o do KPI "Potencial recuperável" da tela do
    lead (o total retroativo); o lead não tem acesso ao portal, então a mensagem convida para a
    apresentação da equipe. Nada aqui muda a resposta da rota."""
    if formulario.get("aviso_whatsapp") != "1":
        return
    try:
        if lead.whatsapp:
            whatsapp_service.notificador().notificar_relatorio_pronto(
                lead.whatsapp, lead.nome_contato, lead.nome_loja, lead.vazamento_total_reais,
                acesso_ao_portal=False)
    except Exception as erro:  # noqa: BLE001 - aviso auxiliar: a avaliação já está gravada
        db.session.rollback()
        current_app.logger.warning("Aviso por WhatsApp do lead não disparado (%s).", type(erro).__name__)


# =====================================================================
# Rotas
# =====================================================================
@leads_bp.route("")
@admin_required
def lista():
    if not esquema_disponivel():
        return render_template("admin/leads.html", disponivel=False, retencao_dias=RETENCAO_DIAS,
                               max_arquivos=raio_x.MAX_ARQUIVOS, leads=[], formulario={},
                               mensagem_sem_esquema=MENSAGEM_SEM_ESQUEMA)
    expurgo_oportunista()
    protocolo = (request.args.get("protocolo") or "").strip()
    if protocolo and not PROTOCOLO_VALIDO.fullmatch(protocolo):
        flash("Protocolo inválido: use os 8 caracteres que vêm na mensagem do Raio-X público.", "warning")
        protocolo = ""
    # ?ver=<id>: o painel daquele lead abre ao carregar (a volta do processamento); outro valor é ignorado.
    return render_template("admin/leads.html", **_contexto_lista(protocolo=protocolo or None,
                                                                  ver=_uuid_da_consulta(request.args.get("ver"))))


@leads_bp.route("", methods=["POST"])
@admin_required
@limite_envio
def avaliar():
    if not esquema_disponivel():
        return _sem_esquema()
    try:
        contato = _contato_do_formulario(request.form)
    except DadoInvalido as erro:
        flash(str(erro), "danger")
        return render_template("admin/leads.html", **_contexto_lista(request.form)), 422
    with vaga_de_entrada() as livre:
        if not livre:
            return _ocupado("admin/leads.html", _contexto_lista(request.form))
        try:
            lead = _avaliar(request.files.getlist("arquivos"), contato=contato)
        except ETLInputError as erro:
            db.session.rollback()
            if erro.code == CODIGO_OCUPADO:
                return _ocupado("admin/leads.html", _contexto_lista(request.form))
            flash(erro.safe_message, "danger")
            return render_template("admin/leads.html", **_contexto_lista(request.form)), 422
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Avaliação de lead não gravada (%s).", type(erro).__name__)
            flash("Não foi possível gravar a avaliação. Tente novamente.", "danger")
            return render_template("admin/leads.html", **_contexto_lista(request.form)), 503
    _avisar_por_whatsapp(lead, request.form)
    flash(f"Avaliação concluída: {lead.arquivos} relatório(s) de vendas.", "success")
    return redirect(url_for("leads.detalhe", lead_id=lead.id), 303)


@leads_bp.route("/<uuid:lead_id>")
@admin_required
def detalhe(lead_id):
    if not esquema_disponivel():
        return _sem_esquema()
    expurgo_oportunista()
    lead = db.session.get(LeadAvaliacao, lead_id)
    if lead is None:
        abort(404)
    # O simulador na tela: os mesmos parâmetros da rota JSON; inválidos, a tela avisa e usa os padrão.
    try:
        parametros, erro_parametros = _parametros_da_consulta(request.args), None
    except ValueError as erro:
        parametros, erro_parametros = None, str(erro)
    return render_template("admin/lead.html", **_contexto_detalhe(lead, parametros, erro_parametros))


@leads_bp.route("/<uuid:lead_id>/painel")
@admin_required
def painel(lead_id):
    """O fragmento HTML do painel lateral da lista ("Ver Avaliação de Cenário"), sem base.html: o cabeçalho do lead
    (empresa, contato, e-mail, WhatsApp, data e o aceite dos termos), as planilhas que ele enviou pela landing e o
    diagnóstico completo, o mesmo da página do lead (_contexto_detalhe, com os ids prefixados). Sem desfoque nem
    bloqueio: é a tela do consultor. As mesmas checagens do detalhe, sem cache, e a leitura no log só com os ids."""
    if not esquema_disponivel():
        resposta = make_response(render_template("admin/_lead_painel.html", disponivel=False,
                                                 mensagem_sem_esquema=MENSAGEM_SEM_ESQUEMA), 503)
        resposta.headers["Retry-After"] = str(ESPERA_SEGUNDOS)
        return _do_painel(resposta)
    expurgo_oportunista()
    lead = db.session.get(LeadAvaliacao, lead_id)
    if lead is None:
        abort(404)
    html = render_template("admin/_lead_painel.html", disponivel=True, prefixo_ids=PREFIXO_DO_PAINEL,
                           **_contexto_detalhe(lead))
    return _do_painel(make_response(html))


@leads_bp.route("/<uuid:lead_id>/diagnostico.json")
@admin_required
def diagnostico_json(lead_id):
    """O diagnóstico do lead em JSON, para o consultor (audiência "consultor"; nunca o contato do
    lead). Itens por bloco até `?limite=` (padrão 30, máximo 200)."""
    if not esquema_disponivel():
        resposta = jsonify({"erro": "esquema_pendente", "mensagem": MENSAGEM_SEM_ESQUEMA})
        resposta.status_code = 503
        resposta.headers["Retry-After"] = str(ESPERA_SEGUNDOS)
    else:
        expurgo_oportunista()
        lead = db.session.get(LeadAvaliacao, lead_id)
        if lead is None:
            resposta = jsonify({"erro": "lead_inexistente"})
            resposta.status_code = 404
        else:
            limite = request.args.get("limite", default=diagnostico_cenario.ITENS_POR_BLOCO_PADRAO, type=int)
            try:
                parametros = _parametros_da_consulta(request.args)
            except ValueError as erro:
                resposta = jsonify({"erro": "parametros_invalidos", "mensagem": str(erro)})
                resposta.status_code = 400
                resposta.headers["Cache-Control"] = "no-store"
                return resposta
            payload = _diagnostico(lead, limite, parametros).payload
            # Registro da leitura só com identificadores (sem nome de produto nem contato).
            current_app.logger.info("Diagnóstico do lead %s lido pelo consultor %s.", lead.id, current_user.id_usuario)
            resposta = jsonify(payload)
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@leads_bp.route("/<uuid:lead_id>/reenviar", methods=["POST"])
@admin_required
@limite_envio
def reenviar(lead_id):
    if not esquema_disponivel():
        return _sem_esquema()
    lead = db.session.get(LeadAvaliacao, lead_id)
    if lead is None:
        abort(404)
    try:
        _exigir_autorizacao(request.form)
    except DadoInvalido as erro:
        flash(str(erro), "danger")
        return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)
    with vaga_de_entrada() as livre:
        if not livre:
            return _ocupado("admin/lead.html", _contexto_detalhe(lead))
        try:
            lead = _avaliar(request.files.getlist("arquivos"), lead_id=lead_id)
        except (ETLInputError, avaliacao_cenario.LeadIndisponivel) as erro:
            db.session.rollback()
            if getattr(erro, "code", None) == CODIGO_OCUPADO:
                return _ocupado("admin/lead.html", _contexto_detalhe(lead))
            flash(getattr(erro, "safe_message", None) or str(erro), "danger")
            return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Reenvio de lead não gravado (%s).", type(erro).__name__)
            flash("Não foi possível gravar a avaliação. Tente novamente.", "danger")
            return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)
    _avisar_por_whatsapp(lead, request.form)
    flash(f"Avaliação refeita com {lead.arquivos} relatório(s) de vendas.", "success")
    if getattr(lead, "aviso_limiares", None):
        flash(lead.aviso_limiares, "warning")
    return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)


@leads_bp.route("/<uuid:lead_id>/retriar", methods=["POST"])
@admin_required
def retriar(lead_id):
    if not esquema_disponivel():
        return _sem_esquema()
    try:
        lead = avaliacao_cenario.retriar(lead_id)
        aviso_limiares = getattr(lead, "aviso_limiares", None)
        db.session.commit()
    except avaliacao_cenario.LeadIndisponivel as erro:
        db.session.rollback()
        flash(str(erro), "warning")
    except ETLInputError as erro:
        db.session.rollback()
        flash(erro.safe_message, "warning")
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Triagem de lead não refeita (%s).", type(erro).__name__)
        flash("Não foi possível refazer a triagem. Tente novamente.", "danger")
    else:
        _descartar_do_cache(lead_id)
        flash("Triagem refeita com as regras atuais.", "success")
        if aviso_limiares:
            flash(aviso_limiares, "warning")
    return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)


# =====================================================================
# Planilhas do lead da landing (Storage do Supabase, só leitura)
# =====================================================================
@leads_bp.route("/<uuid:lead_id>/arquivos/<int:indice>")
@admin_required
@limite_download
def arquivo(lead_id, indice):
    """Uma planilha original do lead (o NN do caminho no Storage), conferida com o tamanho e o SHA-256 registrados
    pelo microsserviço. Índice que não é de uma planilha válida: 404; Storage sem configuração: 503; falha do
    Storage: 502, sempre sem detalhe interno."""
    _lead, planilhas, resposta = _preparar_download(lead_id)
    if resposta is not None:
        return resposta
    planilha = next((planilha for planilha in planilhas if planilha.indice == indice), None)
    if planilha is None:
        abort(404)
    armazenamento, resposta = _armazenamento_ou_resposta()
    if resposta is not None:
        return resposta
    consultor = current_user.id_usuario
    # A conexão do banco volta ao pool antes da rede (até 30 s pelo arquivo).
    db.session.rollback()
    try:
        conteudo = armazenamento.baixar(planilha)
    except armazenamento_leads.ErroDeArmazenamento as erro:
        current_app.logger.warning("Planilha %s do lead %s não baixada do Storage (%s).", indice, lead_id, erro)
        return _resposta_curta(MENSAGEM_STORAGE_FALHOU, 502)
    current_app.logger.info("Planilha %s do lead %s baixada pelo consultor %s.", indice, lead_id, consultor)
    return _anexo(conteudo, planilha.nome_para_baixar, planilha.tipo)


@leads_bp.route("/<uuid:lead_id>/arquivos.zip")
@admin_required
@limite_download
def arquivos_zip(lead_id):
    """Todas as planilhas válidas do lead num .zip (nomes únicos, na ordem do envio), com as mesmas conferências e
    respostas da planilha avulsa."""
    lead, planilhas, resposta = _preparar_download(lead_id)
    if resposta is not None:
        return resposta
    armazenamento, resposta = _armazenamento_ou_resposta()
    if resposta is not None:
        return resposta
    nome, consultor = f"planilhas-lead-{lead.protocolo}.zip", current_user.id_usuario
    # A conexão do banco volta ao pool antes da rede (até 30 s por arquivo).
    db.session.rollback()
    try:
        conteudos = [armazenamento.baixar(planilha) for planilha in planilhas]
    except armazenamento_leads.ErroDeArmazenamento as erro:
        current_app.logger.warning("Planilhas do lead %s não baixadas do Storage (%s).", lead_id, erro)
        return _resposta_curta(MENSAGEM_STORAGE_FALHOU, 502)
    pacote = armazenamento_leads.montar_zip(zip(armazenamento_leads.nomes_unicos(planilhas), conteudos))
    current_app.logger.info("Planilhas do lead %s (%s arquivo(s)) baixadas em .zip pelo consultor %s.", lead_id,
                            len(planilhas), consultor)
    return _anexo(pacote, nome, "application/zip")


@leads_bp.route("/<uuid:lead_id>/processar", methods=["POST"])
@admin_required
@limite_envio
def processar(lead_id):
    """Processa as planilhas que o lead enviou pela landing: dentro da vaga única de entrada, baixa cada uma do
    Storage (conferida com o registrado) e grava a avaliação no próprio lead pelo mesmo caminho do reenvio (_avaliar:
    processo isolado e avaliacao_cenario.registrar). Lead convertido ou com os dados expirados, sem planilha válida
    ou com o Storage sem configuração: nada é baixado. Nenhuma mensagem é enviada ao lead. Volta para a lista com o
    painel do lead aberto (?ver=), já com os números."""
    if not esquema_disponivel():
        return _sem_esquema()
    lead = db.session.get(LeadAvaliacao, lead_id)
    if lead is None:
        abort(404)
    destino = url_for("leads.lista", ver=lead_id)
    if lead.status == "convertido":
        flash("Lead já convertido em cliente: as planilhas não são processadas de novo.", "warning")
        return redirect(destino, 303)
    if lead.status == "expirado":
        flash(f"Os dados deste lead passaram dos {RETENCAO_DIAS} dias de retenção: as planilhas não são processadas "
              "de novo.", "warning")
        return redirect(destino, 303)
    planilhas = _planilhas_do_lead(lead)
    if not planilhas:
        flash("Este lead não tem planilhas válidas no Storage para processar.", "warning")
        return redirect(destino, 303)
    armazenamento = armazenamento_leads.do_app(current_app.config)
    if armazenamento is None:
        flash(armazenamento_leads.MENSAGEM_SEM_CONFIGURACAO + ".", "warning")
        return redirect(destino, 303)
    # O mesmo relatório enviado duas vezes vai uma só: o processamento recusa arquivo repetido.
    vistos, unicas = set(), []
    for planilha in planilhas:
        if planilha.sha256 not in vistos:
            vistos.add(planilha.sha256)
            unicas.append(planilha)
    planilhas = unicas
    with vaga_de_entrada() as livre:
        if not livre:
            return _ocupado("admin/leads.html", _contexto_lista(ver=lead_id))
        # A transação de leitura termina antes da rede (até 30 s por planilha) e do processo isolado.
        db.session.rollback()
        try:
            arquivos = [FileStorage(io.BytesIO(armazenamento.baixar(planilha)), content_type=planilha.tipo,
                                    filename=f"relatorio-{planilha.indice:02d}.{planilha.formato}")
                        for planilha in planilhas]
            lead = _avaliar(arquivos, lead_id=lead_id)
        except armazenamento_leads.ErroDeArmazenamento as erro:
            db.session.rollback()
            current_app.logger.warning("Planilhas do lead %s não baixadas para processar (%s).", lead_id, erro)
            flash(MENSAGEM_STORAGE_FALHOU, "danger")
            return redirect(destino, 303)
        except (ETLInputError, avaliacao_cenario.LeadIndisponivel) as erro:
            db.session.rollback()
            if getattr(erro, "code", None) == CODIGO_OCUPADO:
                return _ocupado("admin/leads.html", _contexto_lista(ver=lead_id))
            flash(getattr(erro, "safe_message", None) or str(erro), "danger")
            return redirect(destino, 303)
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Processamento das planilhas do lead não gravado (%s).", type(erro).__name__)
            flash("Não foi possível gravar a avaliação. Tente novamente.", "danger")
            return redirect(destino, 303)
    current_app.logger.info("Planilhas do lead %s (%s arquivo(s)) processadas pelo consultor %s.", lead_id,
                            len(planilhas), current_user.id_usuario)
    flash(f"Avaliação concluída: {lead.arquivos} relatório(s) de vendas.", "success")
    if getattr(lead, "aviso_limiares", None):
        flash(lead.aviso_limiares, "warning")
    return redirect(destino, 303)
