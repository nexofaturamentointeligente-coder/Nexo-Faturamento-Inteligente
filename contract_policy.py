"""Gestão contratual: política única de acesso do CLIENTE (somente leitura).

Todas as decisões comerciais do portal partem daqui:
  - estado efetivo da empresa (regime LEGADO x CONTRATUAL);
  - acesso ao portal, também como cláusula SQL usada por login, sessão,
    tokens de senha e o polling do sininho (session_security);
  - consulta de análises pelos períodos efetivamente pagos;
  - exportação de PDF dentro do prazo contado da suspensão;
  - envio de arquivos e abertura de novas análises.
Nada aqui grava no banco; as mutações ficam em contract_admin.py.

Regime LEGADO (sem linha em regime_contratual): comportamento anterior
preservado, inclusive a NEXO-AJ-06.1 (CANCELADA bloqueia). Regime CONTRATUAL:
o vínculo é o contrato ATIVO/SUSPENSO e o direito a cada análise nasce da
cobertura paga. O prazo da suspensão vale pelo instante gravado no contrato,
mesmo antes da reconciliação atualizar o banco. Enquanto a migração
81287c74387a não existir no banco, tudo opera no regime legado sem consultar
as tabelas novas (ver esquema_contratual_disponivel).

Prazos (proposta pendente de homologação comercial): meses de calendário e
dias corridos contados no fuso configurado, a partir do instante da suspensão;
o prazo termina ao atingir o instante-limite (limite exclusivo).
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from flask import current_app, g, has_app_context, has_request_context
from sqlalchemy import and_, inspect, or_, select, true
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import CoberturaPaga, Contrato, Empresa, RegimeContratual

REGIME_LEGADO = "LEGADO"
REGIME_CONTRATUAL = "CONTRATUAL"

ESTADO_ATIVA = "ATIVA"
ESTADO_SUSPENSA = "SUSPENSA"
ESTADO_SUSPENSAO_EXPIRADA = "SUSPENSAO_EXPIRADA"
ESTADO_SEM_CONTRATO = "SEM_CONTRATO"
ESTADO_CANCELADA = "CANCELADA"
ESTADO_EXCLUIDA = "EXCLUIDA"

# Estados em que o portal mostra "Conta encerrada" após a senha correta.
ESTADOS_CONTA_ENCERRADA = (ESTADO_CANCELADA, ESTADO_SUSPENSAO_EXPIRADA, ESTADO_SEM_CONTRATO)

_PADRAO_MESES_SUSPENSAO = 3
_PADRAO_DIAS_PDF = 14
_PADRAO_FUSO = "America/Sao_Paulo"

TABELAS_CONTRATUAIS = (
    "regime_contratual", "contrato", "cobertura_paga", "evento_contratual", "historico_preco_plano",
)
_REVERIFICAR_ESQUEMA_SEGUNDOS = 30.0
_esquema_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()


class EsquemaContratualIndisponivel(RuntimeError):
    """A migração da gestão contratual ainda não foi aplicada neste banco."""


class EsquemaContratualInconsistente(SQLAlchemyError):
    """Só parte das tabelas contratuais existe. É erro de banco: guarda de
    sessão (503) e o polling do sininho falham fechado."""


# ---------------------------------------------------------------------------
# Disponibilidade do esquema (implantação expand -> migrate -> deploy)
# ---------------------------------------------------------------------------
def esquema_contratual_disponivel(sessao=None) -> bool:
    """True quando as tabelas da revisão 81287c74387a existem neste banco.

    Antes da migração o portal segue no regime legado sem tocar nas tabelas
    novas. Só a AUSÊNCIA confirmada de todas desliga a gestão; erro de
    consulta propaga e esquema parcial falha fechado. Confirmada a presença,
    o resultado fica fixo para o engine; a ausência é reavaliada a cada 30 s,
    para que a migração aplicada passe a valer sem reiniciar o processo.
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
    presentes = {tabela for tabela in TABELAS_CONTRATUAIS if inspetor.has_table(tabela)}
    if presentes == set(TABELAS_CONTRATUAIS):
        _esquema_por_engine[engine] = True
        return True
    if presentes:
        raise EsquemaContratualInconsistente("Esquema contratual parcial: confira a migração 81287c74387a.")
    _esquema_por_engine[engine] = time.monotonic()
    return False


def exigir_esquema_contratual(sessao=None) -> None:
    if not esquema_contratual_disponivel(sessao):
        raise EsquemaContratualIndisponivel(
            "A gestão contratual depende da migração 81287c74387a, ainda não aplicada neste banco."
        )


def regime_contratual_ativo(id_empresa: int, *, sessao=None) -> bool:
    sessao = sessao or db.session
    if not esquema_contratual_disponivel(sessao):
        return False
    with sessao.no_autoflush:
        return sessao.scalar(select(RegimeContratual.id_empresa).where(
            RegimeContratual.id_empresa == id_empresa,
        )) is not None


# ---------------------------------------------------------------------------
# Relógio e parâmetros
# ---------------------------------------------------------------------------
def utc(instante: datetime) -> datetime:
    """Normaliza para UTC; instante sem fuso é erro de programação."""
    if not isinstance(instante, datetime) or instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError("Instante contratual exige fuso horário explícito.")
    return instante.astimezone(timezone.utc)


def agora(*, nova_leitura: bool = False) -> datetime:
    """Relógio único da política. Testes injetam NEXO_RELOGIO (callable).

    Dentro de uma requisição o instante é fixado na primeira leitura: guarda
    de sessão, rota e template avaliam o MESMO instante. `nova_leitura` lê o
    relógio de novo (reverificação após uma etapa demorada, como o upload).
    """
    if not nova_leitura and has_request_context() and "_nexo_agora_contratual" in g:
        return g._nexo_agora_contratual
    relogio = current_app.config.get("NEXO_RELOGIO") if has_app_context() else None
    instante = utc(relogio() if relogio is not None else datetime.now(timezone.utc))
    if has_request_context() and "_nexo_agora_contratual" not in g:
        g._nexo_agora_contratual = instante
    return instante


def _inteiro(valor, nome: str, minimo: int, maximo: int) -> int:
    if type(valor) is not int or not minimo <= valor <= maximo:
        raise ValueError(f"{nome} deve ser um inteiro entre {minimo} e {maximo}.")
    return valor


@dataclass(frozen=True)
class PoliticaPrazos:
    meses_suspensao: int
    dias_exportacao_pdf: int
    fuso: ZoneInfo
    dias_alerta_vencimento: int = 30
    dias_alerta_suspensao: int = 15


def politica_prazos(config=None) -> PoliticaPrazos:
    """Parâmetros validados; valor inválido falha alto em vez de liberar acesso."""
    if config is None:
        config = current_app.config if has_app_context() else {}
    return PoliticaPrazos(
        meses_suspensao=_inteiro(config.get("CONTRATO_SUSPENSAO_MESES", _PADRAO_MESES_SUSPENSAO),
                                 "CONTRATO_SUSPENSAO_MESES", 1, 12),
        dias_exportacao_pdf=_inteiro(config.get("CONTRATO_EXPORTACAO_PDF_DIAS", _PADRAO_DIAS_PDF),
                                     "CONTRATO_EXPORTACAO_PDF_DIAS", 1, 366),
        fuso=ZoneInfo(config.get("CONTRATO_FUSO_HORARIO", _PADRAO_FUSO)),
        dias_alerta_vencimento=_inteiro(config.get("CONTRATO_ALERTA_VENCIMENTO_DIAS", 30),
                                        "CONTRATO_ALERTA_VENCIMENTO_DIAS", 0, 366),
        dias_alerta_suspensao=_inteiro(config.get("CONTRATO_ALERTA_SUSPENSAO_DIAS", 15),
                                       "CONTRATO_ALERTA_SUSPENSAO_DIAS", 0, 366),
    )


# ---------------------------------------------------------------------------
# Aritmética de prazos (calendário local, resultado em UTC)
# ---------------------------------------------------------------------------
def _localizar(ingenuo: datetime, fuso: ZoneInfo) -> datetime:
    # Horário local ambíguo usa a primeira ocorrência; inexistente (salto de
    # horário de verão) é lido com o deslocamento anterior, avançando o limite.
    return ingenuo.replace(tzinfo=fuso, fold=0).astimezone(timezone.utc)


def somar_meses_calendario(instante: datetime, meses: int, fuso: ZoneInfo) -> datetime:
    """Mesmo dia e hora locais N meses depois; dia inexistente vira o último do mês
    (31/01 + 1 mês = 28/02 ou 29/02 em ano bissexto)."""
    local = utc(instante).astimezone(fuso)
    indice = local.month - 1 + meses
    ano, mes = local.year + indice // 12, indice % 12 + 1
    dia = min(local.day, calendar.monthrange(ano, mes)[1])
    return _localizar(local.replace(tzinfo=None, year=ano, month=mes, day=dia), fuso)


def somar_dias_corridos(instante: datetime, dias: int, fuso: ZoneInfo) -> datetime:
    """Mesma hora local N dias corridos depois."""
    local = utc(instante).astimezone(fuso)
    return _localizar(local.replace(tzinfo=None) + timedelta(days=dias), fuso)


def calcular_limite_suspensao(suspenso_em: datetime, meses: int | None = None,
                              politica: PoliticaPrazos | None = None) -> datetime:
    politica = politica or politica_prazos()
    meses = politica.meses_suspensao if meses is None else _inteiro(meses, "meses", 1, 12)
    return somar_meses_calendario(suspenso_em, meses, politica.fuso)


def calcular_limite_exportacao_pdf(suspenso_em: datetime, limite_suspensao: datetime,
                                   politica: PoliticaPrazos | None = None) -> datetime:
    """Nunca ultrapassa o fim da própria suspensão."""
    politica = politica or politica_prazos()
    return min(somar_dias_corridos(suspenso_em, politica.dias_exportacao_pdf, politica.fuso),
               utc(limite_suspensao))


def data_local(instante: datetime, fuso: ZoneInfo | None = None) -> date:
    return utc(instante).astimezone(fuso or politica_prazos().fuso).date()


def formatar_instante(instante: datetime | None, fuso: ZoneInfo | None = None) -> str:
    """Exibição no fuso da política (nunca no relógio do navegador)."""
    if instante is None:
        return "—"
    local = utc(instante).astimezone(fuso or politica_prazos().fuso)
    return local.strftime("%d/%m/%Y às %H:%M")


# ---------------------------------------------------------------------------
# Coberturas pagas e períodos de análise
# ---------------------------------------------------------------------------
def unir_intervalos(intervalos) -> tuple[tuple[date, date], ...]:
    """Une intervalos inclusivos sobrepostos ou consecutivos (fim + 1 dia)."""
    unidos: list[tuple[date, date]] = []
    for inicio, fim in sorted((i, f) for i, f in intervalos if i is not None and f is not None and i <= f):
        if unidos and (inicio - unidos[-1][1]).days <= 1:
            if fim > unidos[-1][1]:
                unidos[-1] = (unidos[-1][0], fim)
        else:
            unidos.append((inicio, fim))
    return tuple(unidos)


def periodo_coberto(inicio: date | None, fim: date | None, unidos) -> bool:
    """True só se TODO o período [inicio, fim] estiver dentro de um trecho pago."""
    if inicio is None or fim is None or inicio > fim:
        return False
    return any(i <= inicio and fim <= f for i, f in unidos)


def periodo_analise(analise) -> tuple[date | None, date | None]:
    """Período declarado da análise, ampliado pelo período real lido da base.

    O ETL grava em indicador_analise o período encontrado no relatório do PDV.
    Se ele exceder o declarado, a cobertura precisa abranger os dois: nenhum
    dado de período não pago chega ao cliente por agregados da análise.
    """
    inicio, fim = analise.periodo_inicio, analise.periodo_fim
    indicador = analise.indicador
    if indicador is not None:
        if indicador.periodo_base_inicio is not None and inicio is not None:
            inicio = min(inicio, indicador.periodo_base_inicio)
        if indicador.periodo_base_fim is not None and fim is not None:
            fim = max(fim, indicador.periodo_base_fim)
    return inicio, fim


def obter_coberturas_pagas(id_empresa: int, *, instante: datetime | None = None,
                           sessao=None, incluir_revogadas: bool = False):
    """Coberturas confirmadas até o instante e não revogadas antes dele."""
    sessao = sessao or db.session
    instante = agora() if instante is None else utc(instante)
    consulta = select(CoberturaPaga).where(CoberturaPaga.id_empresa == id_empresa)
    if not incluir_revogadas:
        consulta = consulta.where(
            CoberturaPaga.confirmado_em <= instante,
            or_(CoberturaPaga.revogada_em.is_(None), CoberturaPaga.revogada_em > instante),
        )
    return tuple(sessao.scalars(consulta.order_by(
        CoberturaPaga.periodo_inicio, CoberturaPaga.id_cobertura,
    )))


def intervalos_pagos(id_empresa: int, *, instante: datetime | None = None, sessao=None):
    return unir_intervalos(
        (c.periodo_inicio, c.periodo_fim)
        for c in obter_coberturas_pagas(id_empresa, instante=instante, sessao=sessao)
    )


# ---------------------------------------------------------------------------
# Estado efetivo
# ---------------------------------------------------------------------------
def obter_contrato_aplicavel(id_empresa: int, *, sessao=None, atualizar: bool = False) -> Contrato | None:
    """Contrato ATIVO ou SUSPENSO (índice único parcial garante no máximo um).

    `atualizar` relê os atributos do banco em vez do mapa de identidade.
    """
    sessao = sessao or db.session
    consulta = select(Contrato).where(
        Contrato.id_empresa == id_empresa,
        Contrato.situacao.in_(("ATIVO", "SUSPENSO")),
    ).order_by(Contrato.id_contrato.desc()).limit(1)
    if atualizar:
        consulta = consulta.execution_options(populate_existing=True)
    return sessao.scalars(consulta).first()


@dataclass(frozen=True)
class SituacaoContratual:
    id_empresa: int | None
    regime: str
    estado: str
    status_conta: str | None
    instante: datetime
    contrato: Contrato | None = None
    suspenso_em: datetime | None = None
    suspensao_limite: datetime | None = None
    suspensao_limite_original: datetime | None = None
    pdf_limite: datetime | None = None
    vigencia_encerrada: bool = False
    intervalos_pagos: tuple = field(default=())

    @property
    def contratual(self) -> bool:
        return self.regime == REGIME_CONTRATUAL

    @property
    def acesso_portal(self) -> bool:
        return self.estado in (ESTADO_ATIVA, ESTADO_SUSPENSA)

    @property
    def conta_encerrada(self) -> bool:
        return self.estado in ESTADOS_CONTA_ENCERRADA

    @property
    def suspensa(self) -> bool:
        return self.estado == ESTADO_SUSPENSA

    @property
    def suspensao_prorrogada(self) -> bool:
        return (self.suspensao_limite is not None and self.suspensao_limite_original is not None
                and self.suspensao_limite > self.suspensao_limite_original)

    @property
    def exportacao_pdf_aberta(self) -> bool:
        if not self.acesso_portal:
            return False
        if not self.contratual or self.estado == ESTADO_ATIVA:
            return True
        return self.pdf_limite is not None and self.instante < self.pdf_limite


def obter_situacao_contratual(empresa: Empresa | None, *, instante: datetime | None = None,
                              sessao=None, carregar_coberturas: bool = True,
                              atualizar: bool = False) -> SituacaoContratual:
    """Situação da empresa no instante; mesma regra da cláusula SQL de acesso.

    `atualizar` relê empresa e contrato do banco e o relógio, para reverificar
    uma decisão depois de uma etapa longa da mesma requisição.
    """
    instante = agora(nova_leitura=atualizar) if instante is None else utc(instante)
    if atualizar and empresa is not None:
        (sessao or db.session).refresh(empresa)
    if empresa is None:
        return SituacaoContratual(None, REGIME_LEGADO, ESTADO_EXCLUIDA, None, instante)
    base = dict(id_empresa=empresa.id_empresa, status_conta=empresa.status_conta, instante=instante)
    regime = (REGIME_CONTRATUAL if regime_contratual_ativo(empresa.id_empresa, sessao=sessao)
              else REGIME_LEGADO)
    if empresa.deletado_em is not None:
        return SituacaoContratual(regime=regime, estado=ESTADO_EXCLUIDA, **base)
    if empresa.status_conta == ESTADO_CANCELADA:
        return SituacaoContratual(regime=regime, estado=ESTADO_CANCELADA, **base)
    if regime == REGIME_LEGADO:
        estado = ESTADO_SUSPENSA if empresa.status_conta == "SUSPENSA" else ESTADO_ATIVA
        return SituacaoContratual(regime=regime, estado=estado, **base)

    contrato = obter_contrato_aplicavel(empresa.id_empresa, sessao=sessao, atualizar=atualizar)
    if contrato is None:
        return SituacaoContratual(regime=regime, estado=ESTADO_SEM_CONTRATO, **base)
    intervalos = (intervalos_pagos(empresa.id_empresa, instante=instante, sessao=sessao)
                  if carregar_coberturas else ())
    if contrato.situacao == "SUSPENSO":
        estado = (ESTADO_SUSPENSA if instante < contrato.suspensao_limite
                  else ESTADO_SUSPENSAO_EXPIRADA)
        return SituacaoContratual(
            regime=regime, estado=estado, contrato=contrato,
            suspenso_em=contrato.suspenso_em, suspensao_limite=contrato.suspensao_limite,
            suspensao_limite_original=contrato.suspensao_limite_original,
            pdf_limite=contrato.pdf_limite, intervalos_pagos=intervalos, **base,
        )
    # Contrato ATIVO. Status SUSPENSA sem suspensão registrada (alteração
    # externa) é tratado como suspensão sem prazos: consulta paga, sem PDF.
    estado = ESTADO_SUSPENSA if empresa.status_conta == "SUSPENSA" else ESTADO_ATIVA
    encerrada = (contrato.data_fim is not None
                 and data_local(instante) > contrato.data_fim)
    return SituacaoContratual(
        regime=regime, estado=estado, contrato=contrato, vigencia_encerrada=encerrada,
        intervalos_pagos=intervalos, **base,
    )


def condicao_sql_vinculo_contratual(instante: datetime | None = None, sessao=None):
    """Cláusula SQL equivalente a acesso_portal para empresas não canceladas.

    Regime legado passa direto; regime contratual exige contrato ATIVO ou
    SUSPENSO com limite ainda não atingido no instante avaliado. Sem a
    migração aplicada, não há regime contratual possível: cláusula neutra.
    """
    if not esquema_contratual_disponivel(sessao):
        return true()
    instante = agora() if instante is None else utc(instante)
    return or_(
        ~select(RegimeContratual.id_empresa).where(
            RegimeContratual.id_empresa == Empresa.id_empresa,
        ).exists(),
        select(Contrato.id_contrato).where(
            Contrato.id_empresa == Empresa.id_empresa,
            or_(
                Contrato.situacao == "ATIVO",
                and_(Contrato.situacao == "SUSPENSO", Contrato.suspensao_limite > instante),
            ),
        ).exists(),
    )


# ---------------------------------------------------------------------------
# Decisões por recurso
# ---------------------------------------------------------------------------
def analise_publicada(analise) -> bool:
    return (analise is not None and analise.status_analise == "CONCLUIDO"
            and analise.relatorio is not None and bool(analise.relatorio.publicado))


def analise_coberta(situacao: SituacaoContratual, analise) -> bool:
    """Regime legado não restringe por pagamento; o contratual exige cobertura total."""
    if not situacao.acesso_portal or analise is None or analise.id_empresa != situacao.id_empresa:
        return False
    if not situacao.contratual:
        return True
    return periodo_coberto(*periodo_analise(analise), situacao.intervalos_pagos)


def filtrar_analises_consultaveis(situacao: SituacaoContratual, analises) -> list:
    return [a for a in analises if analise_coberta(situacao, a)]


def pode_consultar_analise(situacao: SituacaoContratual, analise) -> bool:
    return analise_publicada(analise) and analise_coberta(situacao, analise)


def pode_exportar_pdf(situacao: SituacaoContratual, analise) -> bool:
    return pode_consultar_analise(situacao, analise) and situacao.exportacao_pdf_aberta


def pode_enviar_relatorio(situacao: SituacaoContratual, analise=None) -> bool:
    """ATIVA envia; SUSPENSA só para análise aberta de período já pago."""
    if not situacao.acesso_portal:
        return False
    if not situacao.contratual or situacao.estado == ESTADO_ATIVA:
        return True
    if situacao.suspensao_limite is None or analise is None:
        return False
    return analise_coberta(situacao, analise)


def vigencia_efetiva(contrato: Contrato, *, sessao=None) -> tuple[date, date | None]:
    """Intervalo em que o contrato realmente regeu a relação (fim None = aberto).

    ENCERRADO termina na véspera do início do sucessor; CANCELADO, na data
    local do cancelamento; sempre limitado ao término previsto.
    """
    sessao = sessao or db.session
    fim = contrato.data_fim
    limite = None
    if contrato.situacao == "ENCERRADO":
        inicio_sucessor = sessao.scalar(select(Contrato.data_inicio).where(
            Contrato.id_contrato_anterior == contrato.id_contrato,
            Contrato.id_empresa == contrato.id_empresa,
        ))
        if inicio_sucessor is not None:
            limite = inicio_sucessor - timedelta(days=1)
    elif contrato.situacao == "CANCELADO" and contrato.cancelado_em is not None:
        limite = data_local(contrato.cancelado_em)
    if limite is not None:
        fim = limite if fim is None else min(fim, limite)
    return contrato.data_inicio, fim


def contrato_da_vigencia(id_empresa: int, periodo_inicio: date, periodo_fim: date, *,
                         sessao=None) -> Contrato | None:
    """Contrato não cancelado cuja vigência efetiva contém todo o período.

    Se mais de um servir (renovação com períodos sobrepostos), vale o de
    início mais recente: é ele que rege o período.
    """
    sessao = sessao or db.session
    if periodo_inicio is None or periodo_fim is None or periodo_inicio > periodo_fim:
        return None
    candidatos = []
    for contrato in sessao.scalars(select(Contrato).where(
            Contrato.id_empresa == id_empresa, Contrato.situacao != "CANCELADO")):
        inicio, fim = vigencia_efetiva(contrato, sessao=sessao)
        if inicio <= periodo_inicio and (fim is None or periodo_fim <= fim):
            candidatos.append(contrato)
    return max(candidatos, key=lambda c: (c.data_inicio, c.id_contrato), default=None)


def motivo_bloqueio_nova_analise(situacao: SituacaoContratual, periodo_inicio: date,
                                 periodo_fim: date, *, sessao=None) -> str | None:
    """Regime contratual: empresa ATIVA e período inteiro na vigência efetiva de
    um contrato não cancelado (o vigente ou um anterior encerrado)."""
    if not situacao.contratual:
        return None
    if situacao.estado != ESTADO_ATIVA or situacao.contrato is None:
        return "A empresa não possui contrato ativo: novas análises estão bloqueadas."
    if contrato_da_vigencia(situacao.id_empresa, periodo_inicio, periodo_fim, sessao=sessao) is None:
        return "O período da análise está fora da vigência dos contratos da empresa."
    return None
