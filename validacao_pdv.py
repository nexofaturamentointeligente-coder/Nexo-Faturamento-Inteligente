"""Trava do total do PDV: o valor informado no envio e o resultado da conferência.

A regra (comparar, tolerância, status) é pura e fica em etl_processor.validar_total_pdv;
aqui fica o que toca o banco e a leitura do que a pessoa digitou. O processo isolado do
ETL não carrega este módulo, porque ele importa os modelos.

Implantação expand -> migrate -> deploy, como as Configurações: antes da migração
e2f7a94c1d35, envio, processamento e painéis seguem funcionando, sem campo e sem card.
Só a publicação recusa (motivo_bloqueio_publicacao), porque publicar sem a tabela
desligaria a trava sem ninguém saber.
"""

from decimal import Decimal
import re
import time
from weakref import WeakKeyDictionary

from sqlalchemy import inspect, select
from sqlalchemy.orm import selectinload

from etl_inputs import ETLInputError
from etl_processor import STATUS_PDV_DIVERGENTE, conferir_total_pdv, converter_valor_monetario, em_centavos
from extensions import db
from horario import agora_utc
from models import Analise, Empresa, Usuario, ValidacaoPdv

STATUS_PDV_PENDENTE = "PENDENTE"
TAMANHO_MAXIMO_DIGITADO = 32
# O teto de Numeric(14, 2).
VALOR_MAXIMO = 999_999_999_999.99
_REVERIFICAR_ESQUEMA_SEGUNDOS = 30.0
_esquema_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()

# O critério de contract_admin.valor_monetario, para digitação humana. O conversor de
# planilha não interrompe o laço: leria "abc" como 0 e "150,000" como 150.
_FORMATO_BR = re.compile(r"(?:[1-9][0-9]{0,2}(?:\.[0-9]{3})+|[0-9]+)(?:,[0-9]{1,2})?")
_FORMATO_PONTO = re.compile(r"[0-9]+\.[0-9]{1,2}")
_PREFIXO_REAIS = re.compile(r"r\$\s*", re.IGNORECASE)


class ValorPdvInvalido(ETLInputError):
    """As rotas de envio já tratam ETLInputError: desfazem, avisam e voltam ao formulário."""

    def __init__(self):
        super().__init__("pdv_valor", "Informe o total de vendas do PDV em reais, no formato 150.000,00.")


def esquema_validacao_pdv_disponivel(sessao=None) -> bool:
    """True quando a tabela da revisão e2f7a94c1d35 existe neste banco.

    Confirmada a presença, o resultado fica fixo para o engine; a ausência é reavaliada
    a cada 30 s, para a migração aplicada valer sem reiniciar o processo. Vem sempre antes
    da consulta: no PostgreSQL, ler tabela inexistente aborta a transação inteira.
    """
    sessao = sessao or db.session
    engine = sessao.get_bind()
    estado = _esquema_por_engine.get(engine)
    if estado is True:
        return True
    if estado is not None and time.monotonic() - estado < _REVERIFICAR_ESQUEMA_SEGUNDOS:
        return False
    # A conexão da própria sessão: sem segunda conexão nem reset de pool.
    if inspect(sessao.connection()).has_table(ValidacaoPdv.__tablename__):
        _esquema_por_engine[engine] = True
        return True
    _esquema_por_engine[engine] = time.monotonic()
    return False


def interpretar_valor_digitado(digitado) -> Decimal:
    """Total digitado no formulário, em centavos. Recusa o que uma pessoa não quis dizer.
    Zero vale: um período sem nenhuma venda válida tem bruto zero."""
    texto = (digitado or "").replace("\xa0", " ").strip() if isinstance(digitado, str) or digitado is None else ""
    if len(texto) > TAMANHO_MAXIMO_DIGITADO:
        raise ValorPdvInvalido()
    prefixo = _PREFIXO_REAIS.match(texto)
    corpo = texto[prefixo.end():] if prefixo else texto
    if not (_FORMATO_BR.fullmatch(corpo) or _FORMATO_PONTO.fullmatch(corpo)):
        raise ValorPdvInvalido()
    valor = converter_valor_monetario(corpo)
    if not 0 <= valor <= VALOR_MAXIMO:
        raise ValorPdvInvalido()
    return em_centavos(valor)


def referencia(analise) -> str:
    """Mês e ano, com a quinzena quando houver: duas quinzenas não podem ter o mesmo rótulo."""
    mes_ano = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
    return f"{mes_ano} ({analise.quinzena_referencia}ª quinzena)" if analise.quinzena_referencia else mes_ano


def registrar_valor_informado(id_analise: int, valor: Decimal, id_usuario: int) -> None:
    """Grava (sem commit) um total novo. O arquivo de Vendas ou o total mudou: a
    conferência anterior deixa de valer até o próximo processamento."""
    linha = db.session.get(ValidacaoPdv, id_analise, populate_existing=True)
    if linha is None:
        linha = ValidacaoPdv(id_analise=id_analise)
        db.session.add(linha)
    linha.valor_pdv_informado = valor
    linha.status_validacao = STATUS_PDV_PENDENTE
    linha.faturamento_bruto_calculado = None
    linha.diferenca = None
    linha.valor_devolucoes = None
    linha.valor_cancelado = None
    linha.id_usuario_informante = id_usuario
    linha.informado_em = agora_utc()
    linha.validado_em = None


def conferir_e_registrar(id_analise: int, resultado):
    """Compara e grava (sem commit), na transação do processamento e sob o lock da
    análise. Devolve o resultado com a conferência na telemetria. Sem a tabela ou sem
    valor informado (análise anterior à trava), não há o que conferir."""
    if not esquema_validacao_pdv_disponivel():
        return resultado
    linha = db.session.get(ValidacaoPdv, id_analise, populate_existing=True)
    if linha is None:
        return resultado
    resultado = conferir_total_pdv(resultado, linha.valor_pdv_informado)
    conferencia = resultado.telemetria["conferencia_pdv"]
    devolucoes = resultado.telemetria.get("valor_devolucoes")
    cancelado = resultado.telemetria.get("valor_cancelado")
    linha.status_validacao = conferencia["status"]
    linha.faturamento_bruto_calculado = conferencia["faturamento_bruto_calculado"]
    linha.diferenca = conferencia["diferenca"]
    linha.valor_devolucoes = None if devolucoes is None else em_centavos(devolucoes)
    linha.valor_cancelado = None if cancelado is None else em_centavos(cancelado)
    linha.validado_em = agora_utc()
    return resultado


def obter(id_analise: int):
    """A linha da análise, sem cache de sessão; None sem a tabela ou sem conferência."""
    if not esquema_validacao_pdv_disponivel():
        return None
    return db.session.get(ValidacaoPdv, id_analise, populate_existing=True)


def por_analise(ids) -> dict:
    """{id_analise: linha} numa consulta só. Vazio sem ids ou sem a tabela."""
    ids = list(ids)
    if not ids or not esquema_validacao_pdv_disponivel():
        return {}
    return {linha.id_analise: linha for linha in db.session.scalars(
        select(ValidacaoPdv).where(ValidacaoPdv.id_analise.in_(ids))
    )}


def divergentes() -> list:
    """Análises divergentes, as mais recentes primeiro, para o painel do consultor."""
    if not esquema_validacao_pdv_disponivel():
        return []
    linhas = db.session.execute(
        select(ValidacaoPdv, Analise)
        .join(Analise, Analise.id_analise == ValidacaoPdv.id_analise)
        .join(Empresa, Empresa.id_empresa == Analise.id_empresa)
        .where(ValidacaoPdv.status_validacao == STATUS_PDV_DIVERGENTE, Empresa.deletado_em.is_(None))
        .options(selectinload(Analise.empresa))
        .order_by(ValidacaoPdv.validado_em.desc(), ValidacaoPdv.id_analise.desc())
    ).all()
    return [{"validacao": validacao, "analise": analise, "referencia": referencia(analise)}
            for validacao, analise in linhas]


def informado_pelo_cliente(linha, analise) -> bool:
    """O total foi digitado por um usuário da própria empresa. O consultor também digita, e
    pode ter digitado justamente o calculado: fora da cobertura, esse número não sai."""
    if linha.id_usuario_informante is None:
        return False
    usuario = db.session.get(Usuario, linha.id_usuario_informante)
    return usuario is not None and usuario.is_cliente and usuario.id_empresa == analise.id_empresa


def conferencia_em_aberto(id_analise: int) -> bool:
    """Os KPIs gravados estão sob suspeita: divergiram, ou o arquivo de Vendas ou o total
    mudou depois de calculados. Sem a tabela ou sem linha, não há o que dizer."""
    linha = obter(id_analise)
    return linha is not None and linha.status_validacao in (STATUS_PDV_DIVERGENTE, STATUS_PDV_PENDENTE)


def motivo_bloqueio_publicacao(id_analise: int):
    """Por que esta análise não pode ser publicada agora; None se puder.

    Sem linha é análise anterior à trava: a especificação só barra a divergência, e
    PENDENTE entra porque o arquivo de Vendas ou o total mudou depois do último processamento.
    Ao publicar, chamar sob o lock da análise, para não correr com um processamento.
    """
    if not esquema_validacao_pdv_disponivel():
        return ("Publicação suspensa: a conferência do total do PDV ainda não foi instalada neste "
                "banco. Aplique a migração e2f7a94c1d35.")
    linha = db.session.get(ValidacaoPdv, id_analise, populate_existing=True)
    if linha is None:
        return None
    if linha.status_validacao == STATUS_PDV_DIVERGENTE:
        return ("Publicação bloqueada: o faturamento calculado diverge do total informado no PDV. "
                "Corrija o total ou reenvie o relatório de Vendas e processe de novo.")
    if linha.status_validacao == STATUS_PDV_PENDENTE:
        return ("Publicação bloqueada: o total do PDV ainda não foi conferido. "
                "Processe a análise com o relatório de Vendas e o total atuais.")
    return None
