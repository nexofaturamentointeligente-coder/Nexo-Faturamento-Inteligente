"""
NEXO - Avaliação de Cenário (blueprint `leads`, só consultores)
================================================================
Rotas (todas admin_required: na fase 1 o consultor envia em nome do lead; a captação pública
fica para depois da LGPD do lead, do anti-abuso e da capacidade, ver Docs/AVALIACAO_DE_CENARIO.md):
  GET  /admin/leads                  → leads avaliados e o formulário de nova avaliação
  POST /admin/leads                  → lê os relatórios do lead e grava a avaliação
  GET  /admin/leads/<id>             → resumo executivo do caixa resgatável, matriz de triagem, marcas,
                                       matriz por categoria, inteligência de estoque e de preço (com o
                                       simulador pelos mesmos parâmetros da rota JSON) e os itens de cada bloco
  GET  /admin/leads/<id>/diagnostico.json → o mesmo diagnóstico em JSON (diagnostico_cenario.montar_payload),
                                       com as séries para gráficos em "visualizacoes"
  POST /admin/leads/<id>/reenviar    → troca os dados do lead pelos de um novo envio
  POST /admin/leads/<id>/retriar     → refaz marcas e triagem a partir dos dados gravados

Regras:
  - Mesma vaga única de upload/processamento das análises e do Raio-X (etl_uploads): ocupada,
    a resposta é 503 com Retry-After e a orientação de enviar de novo; nenhum arquivo é lido.
    O processo isolado tem vaga própria (etl_runtime): ocupada, a resposta é a mesma.
  - Antes do processo isolado (até 2 minutos), a transação da requisição é encerrada: nada fica
    travado nem segura conexão do pooler enquanto os arquivos são lidos.
  - 20 envios por hora por consultor, somando novo envio e reenvio.
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
import json
import threading
import time
from weakref import WeakKeyDictionary

from flask import (Blueprint, abort, current_app, flash, jsonify, make_response, redirect, render_template,
                   request, url_for)
from flask_login import current_user
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import SQLAlchemyError

import avaliacao_cenario
import avaliacao_motor
from blueprints.admin import admin_required
import diagnostico_cenario
from configuracoes_conta import DadoInvalido, limpar_telefone
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
from models import BrainstormingDadosPdv, LeadAvaliacao
import raio_x
import triagem_cenario

leads_bp = Blueprint("leads", __name__, url_prefix="/admin/leads")

limite_envio = limiter.shared_limit("20 per hour", scope="avaliacao-lead", key_func=chave_limite_usuario)

MENSAGEM_OCUPADO = ("Há outro envio ou processamento em andamento. Aguarde alguns segundos e envie de novo: "
                    "nenhum arquivo foi lido.")
MENSAGEM_SEM_ESQUEMA = ("A Avaliação de Cenário depende das migrações 5e1b9c3a7d20, 6a8f2d4c0e13, 7c4e2a9b5d31, "
                        "9b3e5d7f1a28 e c5f8a1d3e7b9, ainda não aplicadas neste banco: nenhum arquivo foi lido.")
# Colunas das revisões mais novas de que a tela depende: a da staging (9b3e5d7f1a28, Etapa 3) e a dos
# indicadores executivos do lead (c5f8a1d3e7b9), que o modelo LeadAvaliacao já lê em toda consulta.
_COLUNA_DO_ESQUEMA = "fim_ultimo_relatorio_com_venda"
_COLUNA_DOS_INDICADORES = "indicadores_executivos_json"
ESPERA_SEGUNDOS = 30
# Código do etl_runtime quando a vaga do processo isolado (_ADMISSION) está ocupada.
CODIGO_OCUPADO = "busy"
ITENS_POR_BLOCO = 30
LEADS_NA_LISTA = 100
_REVERIFICAR_ESQUEMA_SEGUNDOS = 30.0
_esquema_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()


def esquema_disponivel() -> bool:
    """True quando a staging da Etapa 3 (revisão 9b3e5d7f1a28, sobre a 7c4e2a9b5d31) e a coluna dos
    indicadores executivos do lead (c5f8a1d3e7b9) existem neste banco.

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
                  and inspetor.has_table(LeadAvaliacao.__tablename__) and _COLUNA_DOS_INDICADORES in
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
def _contexto_lista(formulario=None) -> dict:
    # Ordem da coluna "Avaliado em": a última autorização (renovada a cada reenvio) ou a criação.
    avaliado_em = func.coalesce(LeadAvaliacao.consentimento_em, LeadAvaliacao.criado_em)
    leads = db.session.scalars(select(LeadAvaliacao).order_by(avaliado_em.desc(), LeadAvaliacao.criado_em.desc())
                               .limit(LEADS_NA_LISTA)).all()
    return {"leads": leads, "formulario": formulario or {}, "max_arquivos": raio_x.MAX_ARQUIVOS,
            "rotulos_blocos": triagem_cenario.ROTULOS_DOS_BLOCOS, "retencao_dias": RETENCAO_DIAS,
            "disponivel": True, "paineis": {lead.id: _painel_da_lista(lead) for lead in leads},
            "total_leads": db.session.scalar(select(func.count()).select_from(LeadAvaliacao))}


def _painel_da_lista(lead: LeadAvaliacao) -> dict:
    """O que a lista mostra de cada lead além das colunas: os produtos de cada bloco da triagem (do
    resumo gravado) e o resumo gravado da inteligência (capital parado, excedente), sem refazer nada."""
    try:
        resumos = {bloco: _resumo(lead, bloco) for bloco in triagem_cenario.BLOCOS}
        inteligencia = _resumo_inteligencia(lead)
        parado = None
        if inteligencia and inteligencia["com_estoque"] and inteligencia.get("capital_parado"):
            dias = min(inteligencia["capital_parado"], key=int)
            # Parcial só quando algum produto com custo e sem saldo informado já está nessa faixa sem venda: o
            # capital dele falta na soma (quem ainda vende, ou não tem custo, não entraria nela com saldo nenhum).
            parado = {"dias": int(dias), "capital": inteligencia["capital_parado"][dias],
                      "parcial": bool(inteligencia.get("itens_parados_sem_estoque_informado"))}
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
        # Um lead com gravação que nenhuma validação previu não derruba a lista dos outros.
        return {"triagem": {bloco: None for bloco in triagem_cenario.BLOCOS}, "inteligencia": None, "parado": None}
    return {"triagem": {bloco: (resumo["itens"] if resumo else None) for bloco, resumo in resumos.items()},
            "inteligencia": inteligencia, "parado": parado}


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
    o dado antigo não fica na memória à espera do TTL. Os outros processos só o servem até a validade."""
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


def _contexto_detalhe(lead: LeadAvaliacao, parametros=None, erro_parametros=None) -> dict:
    """Resumo gravado e, enquanto os dados de PDV existirem e forem da versão atual, a matriz por
    categoria, a inteligência de estoque e de preço e os itens de cada bloco com os motivos (o mesmo
    diagnóstico da rota JSON, com os mesmos parâmetros do simulador)."""
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
    return render_template("admin/leads.html", **_contexto_lista())


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
