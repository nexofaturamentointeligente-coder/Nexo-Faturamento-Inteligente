"""Planos por nível de inteligência: quem pode usar cada recurso do portal.

A fonte da verdade são as tabelas plano_nivel, funcionalidade e plano_funcionalidade
(revisão b3e9d6c1f7a2). O plano da empresa continua sendo empresa.id_plano_atual, ligado
ao nível pelo código (plano.nome_plano = plano_nivel.codigo), e a situação da assinatura
continua vindo de contract_policy (status_conta, contrato, suspensão): não há coluna nova
em empresa nem em plano, e nada aqui grava no banco.

Regras:
  - empresa_tem_acesso() só é True com a assinatura ATIVA (contract_policy.ESTADO_ATIVA).
    Empresa suspensa, cancelada, na lixeira ou sem contrato não usa recurso de plano.
  - Falha fechada: sem o esquema, com erro de banco ou com chave desconhecida, o acesso é
    negado. Os recursos que já existiam antes dos planos (Curva ABC, semáforos, histórico)
    não passam por aqui: nenhuma rota existente foi restringida.
  - @requer_funcionalidade responde 403 com o plano necessário e a URL de upgrade (JSON em
    /api/ ou quando o cliente pede JSON; página do portal nos demais casos) e 503 enquanto
    a migração não existir no banco.
  - O menu do cliente sai de MENU_CLIENTE. Um item de plano superior aparece com cadeado e
    o selo UPGRADE e leva à página de planos; sem o esquema ou com a assinatura inativa,
    os itens de plano não aparecem.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import logging
import time
from weakref import WeakKeyDictionary

from flask import current_app, g, has_request_context, jsonify, make_response, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError

from contract_policy import ESTADO_ATIVA, obter_situacao_contratual
from extensions import db
from models import Empresa, Funcionalidade, PlanoFuncionalidade, PlanoNivel

logger = logging.getLogger(__name__)

REVISAO_PLANOS = "b3e9d6c1f7a2"
TABELAS_PLANOS = ("plano_nivel", "funcionalidade", "plano_funcionalidade", "diagnostico_entrada")
_REVERIFICAR_ESQUEMA_SEGUNDOS = 30.0
_esquema_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()

# Funcionalidades que o portal já entrega, e onde. As demais do catálogo aparecem na página
# de planos como "Em implantação" e nunca viram item de menu.
FUNCIONALIDADES_NO_PORTAL = {
    "curva_abc_vendas": "Análises publicadas: tabela e gráfico da Curva ABC.",
    "produtos_parados_ruptura": "Análises publicadas: saldo estimado parado e semáforos de estoque.",
    "checklist_acoes": "Análises publicadas: semáforos com o que fazer e por quê.",
    "raio_x_entrada": "Raio-X do PDV.",
    "auditoria_abaixo_custo": "Auditoria de custos.",
    "margem_bruta": "Raio-X do PDV e Auditoria de custos.",
    "diagnostico_cmv": "Auditoria de custos.",
    "alerta_precificacao": "Auditoria de custos.",
}


# ---------------------------------------------------------------------------
# Disponibilidade do esquema (implantação expand -> migrate -> deploy)
# ---------------------------------------------------------------------------
def esquema_planos_disponivel(sessao=None) -> bool:
    """True quando as quatro tabelas da revisão b3e9d6c1f7a2 existem e o catálogo de níveis
    foi gravado.

    Confirmada a presença, o resultado fica fixo para o engine; a ausência é reavaliada a
    cada 30 s, para a migração aplicada valer sem reiniciar o processo. Esquema parcial ou
    catálogo vazio (tabelas criadas sem a migração, por create_all) contam como ausentes
    (falha fechada) e ficam registrados no log.
    """
    sessao = sessao or db.session
    engine = sessao.get_bind()
    estado = _esquema_por_engine.get(engine)
    if estado is True:
        return True
    if estado is not None and time.monotonic() - estado < _REVERIFICAR_ESQUEMA_SEGUNDOS:
        return False
    # A conexão da própria sessão: sem segunda conexão nem reset de pool.
    inspetor = inspect(sessao.connection())
    presentes = {tabela for tabela in TABELAS_PLANOS if inspetor.has_table(tabela)}
    if presentes == set(TABELAS_PLANOS):
        if sessao.scalar(select(PlanoNivel.codigo).limit(1)) is not None:
            _esquema_por_engine[engine] = True
            return True
        logger.warning("Catálogo dos planos vazio: as tabelas existem sem os dados da migração %s.",
                       REVISAO_PLANOS)
    elif presentes:
        logger.warning("Esquema de planos parcial (%s): confira a migração %s.", ", ".join(sorted(presentes)),
                       REVISAO_PLANOS)
    _esquema_por_engine[engine] = time.monotonic()
    return False


def esquecer_esquema_planos(sessao=None) -> None:
    """Descarta a presença memorizada (o banco pode ter voltado a uma revisão anterior)."""
    sessao = sessao or db.session
    _esquema_por_engine.pop(sessao.get_bind(), None)


def registrar_falha_de_banco(erro) -> None:
    """Consulta falhou (tabela removida, conexão perdida): desfaz e volta a conferir o esquema."""
    logger.warning("Planos indisponíveis (%s): recursos de plano negados nesta requisição.", type(erro).__name__)
    try:
        db.session.rollback()
        esquecer_esquema_planos()
    except Exception:  # noqa: BLE001 - a página precisa sair mesmo com o banco fora
        pass
    if has_request_context():
        for chave in ("_nexo_catalogo_planos", "_nexo_funcionalidades", "_nexo_assinaturas"):
            g.pop(chave, None)


# ---------------------------------------------------------------------------
# Catálogo: níveis, funcionalidades e matriz
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NivelPlano:
    codigo: str
    nome: str
    modelo_atendimento: str
    atendimento: str
    nivel_hierarquico: int
    limite_historico_meses: int | None

    @property
    def ancora(self) -> str:
        return self.codigo.lower()

    @property
    def historico(self) -> str:
        if self.limite_historico_meses is None:
            return "Histórico completo"
        return f"Últimos {self.limite_historico_meses} meses"


@dataclass(frozen=True)
class FuncionalidadeInfo:
    chave: str
    nome: str
    descricao: str

    @property
    def no_portal(self) -> bool:
        return self.chave in FUNCIONALIDADES_NO_PORTAL


@dataclass(frozen=True)
class Catalogo:
    niveis: tuple            # NivelPlano, do menor para o maior
    funcionalidades: dict    # chave -> FuncionalidadeInfo, na ordem do catálogo
    liberadas: dict          # código do nível -> frozenset de chaves

    def nivel(self, codigo) -> NivelPlano | None:
        return next((nivel for nivel in self.niveis if nivel.codigo == codigo), None)

    def plano_necessario(self, chave) -> NivelPlano | None:
        """O menor nível que libera a funcionalidade."""
        return next((nivel for nivel in self.niveis if chave in self.liberadas.get(nivel.codigo, ())), None)


def _cache_requisicao(nome: str) -> dict:
    if not has_request_context():
        return {}
    if nome not in g:
        setattr(g, nome, {})
    return getattr(g, nome)


def catalogo() -> Catalogo:
    """Níveis, funcionalidades e matriz, lidos uma vez por requisição. Exige o esquema."""
    cache = _cache_requisicao("_nexo_catalogo_planos")
    if "catalogo" in cache:
        return cache["catalogo"]
    niveis = tuple(
        NivelPlano(n.codigo, n.nome, n.modelo_atendimento, n.atendimento, n.nivel_hierarquico,
                   n.limite_historico_meses)
        for n in db.session.scalars(select(PlanoNivel).order_by(PlanoNivel.nivel_hierarquico))
    )
    funcionalidades = {
        f.chave: FuncionalidadeInfo(f.chave, f.nome, f.descricao)
        for f in db.session.scalars(select(Funcionalidade).order_by(Funcionalidade.id_funcionalidade))
    }
    liberadas: dict = {}
    for codigo, chave in db.session.execute(
        select(PlanoFuncionalidade.codigo_plano, Funcionalidade.chave)
        .join(Funcionalidade, Funcionalidade.id_funcionalidade == PlanoFuncionalidade.id_funcionalidade)
    ):
        liberadas.setdefault(codigo, set()).add(chave)
    resultado = Catalogo(niveis, funcionalidades, {codigo: frozenset(chaves) for codigo, chaves in liberadas.items()})
    cache["catalogo"] = resultado
    return resultado


def plano_necessario(funcionalidade_chave: str) -> NivelPlano | None:
    if not esquema_planos_disponivel():
        return None
    return catalogo().plano_necessario(funcionalidade_chave)


def catalogo_planos() -> list:
    """Para a página de planos: cada nível com as funcionalidades que ele acrescenta."""
    dados = catalogo()
    linhas, anteriores = [], frozenset()
    for nivel in dados.niveis:
        liberadas = dados.liberadas.get(nivel.codigo, frozenset())
        linhas.append({
            "nivel": nivel,
            "novas": [info for chave, info in dados.funcionalidades.items()
                      if chave in liberadas and chave not in anteriores],
            "herda": next((n for n in reversed(dados.niveis)
                           if n.nivel_hierarquico < nivel.nivel_hierarquico), None),
        })
        anteriores = liberadas
    return linhas


# ---------------------------------------------------------------------------
# Assinatura da empresa e acesso
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Assinatura:
    """O que o pedido chama de plano_id, status_assinatura e data_expiracao_plano, derivado
    das fontes que já existem (empresa.id_plano_atual e contract_policy), sem duplicá-las."""
    id_empresa: int
    codigo_plano: str | None
    situacao: str
    data_expiracao: object = None   # date do fim do contrato vigente, quando houver

    @property
    def ativa(self) -> bool:
        return self.situacao == ESTADO_ATIVA


def assinatura_da_empresa(empresa) -> Assinatura:
    cache = _cache_requisicao("_nexo_assinaturas")
    if empresa.id_empresa in cache:
        return cache[empresa.id_empresa]
    situacao = obter_situacao_contratual(empresa, carregar_coberturas=False)
    plano = empresa.plano_atual
    assinatura = Assinatura(
        id_empresa=empresa.id_empresa,
        codigo_plano=plano.nome_plano if plano is not None else None,
        situacao=situacao.estado,
        data_expiracao=situacao.contrato.data_fim if situacao.contrato is not None else None,
    )
    cache[empresa.id_empresa] = assinatura
    return assinatura


def funcionalidades_da_empresa(id_empresa: int) -> frozenset:
    """Chaves liberadas para a empresa agora; vazio sem esquema ou com assinatura inativa.
    Erros de banco propagam: quem chama decide a resposta (empresa_tem_acesso nega)."""
    cache = _cache_requisicao("_nexo_funcionalidades")
    if id_empresa in cache:
        return cache[id_empresa]
    liberadas = frozenset()
    if esquema_planos_disponivel():
        empresa = db.session.get(Empresa, id_empresa)
        if empresa is not None:
            assinatura = assinatura_da_empresa(empresa)
            if assinatura.ativa and assinatura.codigo_plano:
                liberadas = catalogo().liberadas.get(assinatura.codigo_plano, frozenset())
    cache[id_empresa] = liberadas
    return liberadas


def empresa_tem_acesso(empresa_id, funcionalidade_chave) -> bool:
    """True se o plano atual da empresa libera a funcionalidade e a assinatura está ativa."""
    if not isinstance(funcionalidade_chave, str) or not funcionalidade_chave:
        return False
    try:
        id_empresa = int(empresa_id)
    except (TypeError, ValueError):
        return False
    try:
        return funcionalidade_chave in funcionalidades_da_empresa(id_empresa)
    except SQLAlchemyError as erro:
        registrar_falha_de_banco(erro)
        return False


def nivel_da_empresa(empresa) -> NivelPlano | None:
    """Nível do plano atual (exige o esquema); None se o plano não tiver nível cadastrado."""
    codigo = assinatura_da_empresa(empresa).codigo_plano
    return catalogo().nivel(codigo) if codigo else None


def limite_historico_meses(empresa) -> int | None:
    """Meses de histórico do plano; None = sem limite. Sem nível conhecido, o do menor nível."""
    nivel = nivel_da_empresa(empresa)
    if nivel is None:
        niveis = catalogo().niveis
        nivel = niveis[0] if niveis else None
    return nivel.limite_historico_meses if nivel is not None else None


# ---------------------------------------------------------------------------
# Decorator e respostas de acesso negado
# ---------------------------------------------------------------------------
def _quer_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    return request.accept_mimetypes.best_match(("text/html", "application/json")) == "application/json"


def url_upgrade(nivel: NivelPlano | None = None) -> str:
    """Página de planos, na âncora do nível sugerido. Relativa: a origem pública não é confiável."""
    if nivel is None:
        return url_for("onboarding.planos")
    return url_for("onboarding.planos", _anchor=nivel.ancora)


MENSAGEM_VERIFICACAO = "Não foi possível conferir o seu plano agora. Tente novamente em instantes."


def _motivo_negado(chave: str, id_empresa, *, falha_banco: bool = False) -> tuple[int, dict]:
    """Status e contexto da recusa, sem nenhum dado do recurso pedido."""
    rotulo = next((item.rotulo for item in MENU_CLIENTE if item.funcionalidade == chave), "Recurso do plano")
    base = {"codigo": "plano_insuficiente", "funcionalidade": {"chave": chave, "nome": rotulo},
            "plano_atual": None, "plano_necessario": None, "url_upgrade": None}
    if not esquema_planos_disponivel():
        return 503, {**base, "codigo": "recurso_indisponivel",
                     "mensagem": "Este recurso ainda não foi ativado no portal. Tente novamente mais tarde."}
    if falha_banco:
        # A conferência falhou por erro de banco: não é recusa por plano, é "tente de novo".
        return 503, {**base, "codigo": "verificacao_indisponivel", "mensagem": MENSAGEM_VERIFICACAO}
    dados = catalogo()
    info = dados.funcionalidades.get(chave)
    if info is not None:
        base["funcionalidade"] = {"chave": chave, "nome": info.nome}
    nome = base["funcionalidade"]["nome"]
    if id_empresa is None:
        return 403, {**base, "codigo": "acesso_restrito",
                     "mensagem": f"{nome} é um recurso do portal do cliente."}
    empresa = db.session.get(Empresa, id_empresa)
    assinatura = assinatura_da_empresa(empresa) if empresa is not None else None
    atual = dados.nivel(assinatura.codigo_plano) if assinatura is not None else None
    base["plano_atual"] = ({"codigo": atual.codigo, "nome": atual.nome} if atual is not None
                           else ({"codigo": assinatura.codigo_plano, "nome": assinatura.codigo_plano}
                                 if assinatura is not None and assinatura.codigo_plano else None))
    if assinatura is None or not assinatura.ativa:
        return 403, {**base, "codigo": "assinatura_inativa", "url_upgrade": url_for("cliente.tickets"),
                     "mensagem": (f"{nome} fica disponível com a assinatura ativa. "
                                  "Fale com a equipe NEXO pelo Suporte.")}
    necessario = dados.plano_necessario(chave)
    if necessario is None:
        return 403, {**base, "url_upgrade": url_upgrade(),
                     "mensagem": f"{nome} ainda não faz parte de nenhum plano disponível."}
    return 403, {**base, "plano_necessario": {"codigo": necessario.codigo, "nome": necessario.nome},
                 "url_upgrade": url_upgrade(necessario),
                 "mensagem": f"{nome} está disponível a partir do plano {necessario.nome}."}


def resposta_acesso_negado(chave: str, id_empresa=None, *, falha_banco: bool = False):
    """403 (plano ou assinatura) ou 503 (sem esquema ou erro de banco) com o plano necessário
    e a URL de upgrade."""
    try:
        status, contexto = _motivo_negado(chave, id_empresa, falha_banco=falha_banco)
    except SQLAlchemyError as erro:
        registrar_falha_de_banco(erro)
        status, contexto = 503, {"codigo": "verificacao_indisponivel",
                                 "funcionalidade": {"chave": chave, "nome": "Recurso do plano"},
                                 "plano_atual": None, "plano_necessario": None, "url_upgrade": None,
                                 "mensagem": MENSAGEM_VERIFICACAO}
    mensagem = contexto.pop("mensagem")
    if _quer_json():
        resposta = jsonify(erro=mensagem, resposta=mensagem, fonte="local", acao=None, destino=None, **contexto)
        resposta.status_code = status
    else:
        resposta = make_response(render_template("onboarding/upgrade.html", mensagem=mensagem, status=status,
                                                 **contexto), status)
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


def requer_funcionalidade(chave: str):
    """Libera a view só para o CLIENTE cuja empresa tem a funcionalidade no plano atual."""
    def decorador(view):
        @wraps(view)
        def protegida(*args, **kwargs):
            if not current_user.is_authenticated:
                return current_app.login_manager.unauthorized()
            id_empresa = current_user.id_empresa if getattr(current_user, "is_cliente", False) else None
            if id_empresa is None:
                return resposta_acesso_negado(chave, None)
            try:
                liberado = chave in funcionalidades_da_empresa(id_empresa)
            except SQLAlchemyError as erro:
                # Falha fechada, mas sem convite falso de upgrade a quem talvez já tenha o plano.
                registrar_falha_de_banco(erro)
                return resposta_acesso_negado(chave, id_empresa, falha_banco=True)
            if liberado:
                return view(*args, **kwargs)
            return resposta_acesso_negado(chave, id_empresa)
        protegida.funcionalidade_requerida = chave
        return protegida
    return decorador


# ---------------------------------------------------------------------------
# Menu lateral do cliente
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ItemMenu:
    rotulo: str
    icone: str
    endpoint: str
    ativos: tuple
    funcionalidade: str | None = None


MENU_CLIENTE = (
    ItemMenu("Visão Geral", "bi-speedometer2", "cliente.dashboard", ("cliente.dashboard",)),
    ItemMenu("Enviar relatórios", "bi-cloud-arrow-up", "cliente.upload", ("cliente.upload",)),
    ItemMenu("Histórico", "bi-clock-history", "cliente.historico", ("cliente.historico", "cliente.analise")),
    ItemMenu("Raio-X do PDV", "bi-activity", "onboarding.diagnostico",
             ("onboarding.diagnostico", "onboarding.diagnostico_enviar"), "raio_x_entrada"),
    ItemMenu("Auditoria de custos", "bi-clipboard2-pulse", "onboarding.auditoria",
             ("onboarding.auditoria",), "auditoria_abaixo_custo"),
    ItemMenu("Guia", "bi-book", "cliente.guia", ("cliente.guia",)),
    ItemMenu("Suporte", "bi-life-preserver", "cliente.tickets", ("cliente.tickets", "cliente.ticket_detalhe")),
    ItemMenu("Configurações", "bi-gear", "cliente.configuracoes", ("cliente.configuracoes",)),
)


def _item(item: ItemMenu) -> dict:
    return {"rotulo": item.rotulo, "icone": item.icone, "href": url_for(item.endpoint),
            "ativos": item.ativos, "bloqueado": False, "plano_necessario": None}


def _menu_basico() -> list:
    return [_item(item) for item in MENU_CLIENTE if item.funcionalidade is None]


def menu_cliente(id_empresa: int) -> list:
    """Itens do menu do CLIENTE: os de sempre, mais os de plano (livres ou com cadeado)."""
    if not esquema_planos_disponivel():
        return _menu_basico()
    empresa = db.session.get(Empresa, id_empresa)
    if empresa is None or not assinatura_da_empresa(empresa).ativa:
        return _menu_basico()
    liberadas = funcionalidades_da_empresa(id_empresa)
    dados = catalogo()
    itens = []
    for item in MENU_CLIENTE:
        if item.funcionalidade is None or item.funcionalidade in liberadas:
            itens.append(_item(item))
            continue
        necessario = dados.plano_necessario(item.funcionalidade)
        if necessario is None:
            continue
        itens.append({"rotulo": item.rotulo, "icone": "bi-lock-fill", "href": url_upgrade(necessario),
                      "ativos": (), "bloqueado": True, "plano_necessario": necessario.nome})
    return itens


def contexto_menu(usuario) -> dict:
    """Context processor: {'menu_cliente': [...]} para o CLIENTE; nada para os demais.

    Roda em toda página, inclusive nas de erro e no modal dos termos: qualquer falha
    devolve o menu de sempre, sem os itens de plano, em vez de derrubar a página.
    """
    try:
        if not getattr(usuario, "is_authenticated", False) or not getattr(usuario, "is_cliente", False):
            return {}
        id_empresa = usuario.id_empresa
    except Exception:  # noqa: BLE001 - usuário expirado após rollback e banco fora
        return {}
    try:
        return {"menu_cliente": menu_cliente(id_empresa)}
    except Exception as erro:  # noqa: BLE001
        registrar_falha_de_banco(erro)
        try:
            return {"menu_cliente": _menu_basico()}
        except Exception:  # noqa: BLE001 - endpoints do cliente ausentes num app reduzido
            return {"menu_cliente": []}
