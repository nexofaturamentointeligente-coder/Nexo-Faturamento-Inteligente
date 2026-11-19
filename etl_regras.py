"""Regras do ETL que o processo web usa, só com a biblioteca padrão.

etl_processor.py (o motor, com pandas e numpy) roda no processo isolado do ETL (etl_worker). O processo
web usa daqui, sem carregar pandas, numpy nem openpyxl:
  - o resultado do cálculo (ResultadoETL) e o orquestrador da análise mensal (processar_arquivos_analise),
    que despacha o cálculo para o processo isolado e grava só os KPIs finais;
  - a trava do total do PDV (regra pura; o banco fica em validacao_pdv.py) e a leitura de um valor em
    dinheiro, a mesma do motor;
  - o teto do nome da categoria, que o processo pai também confere na Avaliação de Cenário.
etl_processor importa estes nomes daqui (uma só fonte) e continua a expô-los.
"""

from collections import namedtuple
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
import logging
import math
import numbers
import re
import unicodedata

from etl_erros import ETLInputError, ETLValidationError
from etl_limits import get_limits

# O canal de log de sempre da análise mensal.
logger = logging.getLogger("etl_processor")

ResultadoETL = namedtuple(
    "ResultadoETL", ["sucesso", "mensagem", "kpis", "telemetria", "alertas"]
)

# Trava do total do PDV: diferença maior que a tolerância bloqueia a publicação.
TOLERANCIA_PDV = Decimal("1.00")
STATUS_PDV_CONCLUIDO = "CONCLUIDO"
STATUS_PDV_DIVERGENTE = "DIVERGENTE_PDV"

# Tamanho máximo do nome da categoria (etl_processor.normalizar_categoria), que o processo pai também
# confere na Avaliação de Cenário (etl_runtime).
LIMITE_CATEGORIA = 80


# =====================================================================
# Leitura de um valor em dinheiro (a do motor e a do total digitado no PDV)
# =====================================================================
# Milhar só com grupos de 3 dígitos depois de um início sem zero: "0.125" nunca é milhar.
_RE_MILHAR_PONTO = re.compile(r"[1-9][0-9]{0,2}(?:\.[0-9]{3})+")
_RE_MILHAR_VIRGULA = re.compile(r"[1-9][0-9]{0,2}(?:,[0-9]{3})+")
# A mesma linguagem de sempre ("[0-9]+[.,]?[0-9]*|[.,][0-9]+"), escrita sem retrocesso quadrático:
# uma célula com 16 mil dígitos levava 3 s.
_RE_DECIMAL = re.compile(r"[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+")
_RE_CASAS_DECIMAIS = re.compile(r"[0-9]+")
_RE_CIENTIFICO = re.compile(r"[0-9]+(?:[.,][0-9]+)?[eE][+-]?[0-9]+")
_RE_MOEDA = re.compile(r"r\$|us\$|brl|usd|\$", re.IGNORECASE)
_SINAIS_DE_MENOS = str.maketrans({"−": "-", "–": "-", "﹣": "-", "－": "-"})


def _limpar_monetario(valor) -> str:
    """Tira espaços, caracteres invisíveis, aspas de texto do Excel e símbolo de moeda."""
    texto = str(valor).translate(_SINAIS_DE_MENOS)
    texto = "".join(c for c in texto if unicodedata.category(c)[0] not in "CZ")
    return _RE_MOEDA.sub("", texto.strip("'\""))


def _separar_sinal(corpo: str) -> tuple:
    """(negativo, corpo sem sinal): aceita "(10)", "-10" e "10-", formatos de PDV."""
    negativo = False
    if corpo.startswith("(") and corpo.endswith(")"):
        negativo, corpo = True, corpo[1:-1]
    if corpo.startswith("-"):
        negativo, corpo = True, corpo[1:]
    elif corpo.endswith("-"):
        negativo, corpo = True, corpo[:-1]
    return negativo, corpo.removeprefix("+")


def _normalizar_separadores(corpo: str, ponto_decimal: bool, virgula_milhar: bool = False):
    """Texto aceito por float(), ou None se os separadores não formam um número.
    `virgula_milhar`: a coluna é en-US pelo texto das células (etl_processor._coluna_usa_virgula_de_milhar), então
    "12,500" é doze mil e quinhentos; sem isso, como sempre, é doze e meio."""
    if _RE_CIENTIFICO.fullmatch(corpo):
        return corpo.replace(",", ".")
    virgula, ponto = corpo.rfind(","), corpo.rfind(".")
    if virgula >= 0 and ponto >= 0:
        # O separador mais à direita é o decimal; o outro só pode agrupar milhares.
        decimal_br = virgula > ponto
        inteiro, _, casas = corpo.rpartition("," if decimal_br else ".")
        agrupado = _RE_MILHAR_PONTO if decimal_br else _RE_MILHAR_VIRGULA
        if not (agrupado.fullmatch(inteiro) and _RE_CASAS_DECIMAIS.fullmatch(casas)):
            return None
        return inteiro.replace("." if decimal_br else ",", "") + "." + casas
    if corpo.count(",") > 1:
        return corpo.replace(",", "") if _RE_MILHAR_VIRGULA.fullmatch(corpo) else None
    if corpo.count(".") > 1:
        return corpo.replace(".", "") if _RE_MILHAR_PONTO.fullmatch(corpo) else None
    if _RE_MILHAR_PONTO.fullmatch(corpo) and not ponto_decimal:
        return corpo.replace(".", "")
    # Numa coluna en-US pelo texto das células ("1,234.56"), "12,500" é milhar: doze mil e quinhentos.
    if virgula_milhar and _RE_MILHAR_VIRGULA.fullmatch(corpo):
        return corpo.replace(",", "")
    return corpo.replace(",", ".") if _RE_DECIMAL.fullmatch(corpo) else None


def _interpretar_monetario(valor, ponto_decimal: bool = False, virgula_milhar: bool = False):
    """Float finito, ou None quando o valor é nulo ou inválido."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (numbers.Real, Decimal)):
        numero = float(valor)
        return numero if math.isfinite(numero) else None
    negativo, corpo = _separar_sinal(_limpar_monetario(valor))
    texto = _normalizar_separadores(corpo, ponto_decimal, virgula_milhar)
    if texto is None:
        return None
    numero = float(texto)
    if not math.isfinite(numero):
        return None
    return -numero if (negativo and numero) else numero


def converter_valor_monetario(valor, ponto_decimal: bool = False, virgula_milhar: bool = False) -> float:
    """
    Converte um valor em dinheiro para float, sem nunca interromper o processamento:
    nulo ou inválido vira 0.0.

      "R$ 12.500,50" -> 12500.5     "12.500" -> 12500.0     "1.234.567" -> 1234567.0
      "1,234.56"     -> 1234.56     "12,5"   -> 12.5        "0.125"     -> 0.125
      "(10,00)"      -> -10.0       "10,00-" -> -10.0       "abc"       -> 0.0

    `ponto_decimal=True` lê "12.500" como 12.5, para colunas que mostram usar o ponto
    como decimal (ver etl_processor._coluna_usa_ponto_decimal); `virgula_milhar=True` lê "12,500"
    como 12500.0, para colunas en-US pelo texto (ver etl_processor._coluna_usa_virgula_de_milhar). Um
    valor sozinho não tem como revelar nenhum dos dois.
    """
    numero = _interpretar_monetario(valor, ponto_decimal, virgula_milhar)
    return 0.0 if numero is None else numero


def _reais(valor: float) -> str:
    inteiro, _, centavos = f"{abs(valor):,.2f}".partition(".")
    return ("-" if valor < 0 else "") + inteiro.replace(",", ".") + "," + centavos


# =====================================================================
# Cálculo da análise mensal, no processo isolado (etl_runtime.run_calculation)
# =====================================================================
def calcular_kpis_em_memoria(caminho_vendas: str, caminho_compras: str) -> ResultadoETL:
    """Calcula sem banco em processo próprio; limites não dependem de threads."""
    from etl_runtime import run_calculation
    try:
        return run_calculation(caminho_vendas, caminho_compras, get_limits())
    except ETLInputError as erro:
        raise ETLValidationError(erro.safe_message, erro.code) from None


# =====================================================================
# Trava do total do PDV (regra pura; o banco fica em validacao_pdv.py)
# =====================================================================
def em_centavos(valor) -> Decimal:
    """Valor arredondado ao centavo. Comparar em float erraria na fronteira de R$ 1,00:
    131072.70 - 131071.70 dá 1.0000000000145519, e uma diferença de 1,00 viraria divergência."""
    with localcontext() as contexto:
        contexto.prec = 400  # cabe qualquer float finito: o quantize não pode estourar a precisão
        return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def validar_total_pdv(faturamento_bruto_calculado, valor_pdv_informado) -> dict:
    """
    Compara o total informado no PDV com o faturamento bruto calculado pelo ETL.

    `status` é o resultado da CONFERÊNCIA, e não o da análise: CONCLUIDO aqui só diz que
    os totais batem. A análise segue em EM_ANALISE até a publicação humana.
    """
    calculado = em_centavos(faturamento_bruto_calculado)
    informado = em_centavos(valor_pdv_informado)
    diferenca = abs(calculado - informado)
    return {
        "status": STATUS_PDV_DIVERGENTE if diferenca > TOLERANCIA_PDV else STATUS_PDV_CONCLUIDO,
        "valor_pdv_informado": informado,
        "faturamento_bruto_calculado": calculado,
        "diferenca": diferenca,
    }


def faturamento_bruto(resultado: ResultadoETL) -> float:
    """Soma das vendas válidas: sem as canceladas e antes de deduzir devoluções e trocas."""
    return resultado.kpis["faturamento_total"] + resultado.telemetria.get("valor_devolucoes", 0.0)


def conferir_total_pdv(resultado: ResultadoETL, valor_pdv_informado) -> ResultadoETL:
    """
    Acrescenta a conferência à telemetria do retorno, para auditoria. Roda no processo
    pai, depois do cálculo: valor digitado não entra no processo isolado do ETL, e a
    assinatura de processar_arquivos_analise continua a mesma.
    """
    conferencia = validar_total_pdv(faturamento_bruto(resultado), valor_pdv_informado)
    return resultado._replace(telemetria={**resultado.telemetria, "conferencia_pdv": conferencia})


# =====================================================================
# Persistência (somente KPIs finais) + orquestrador oficial
# =====================================================================
def _persistir_indicadores(db_session, id_analise: int, kpis: dict) -> None:
    from sqlalchemy import select
    from models import IndicadorAnalise
    indicador = db_session.execute(
        select(IndicadorAnalise).where(IndicadorAnalise.id_analise == id_analise)
    ).scalar_one_or_none()

    if indicador is None:
        indicador = IndicadorAnalise(id_analise=id_analise, versao_processamento=1)
        db_session.add(indicador)
    else:
        indicador.versao_processamento = (indicador.versao_processamento or 0) + 1

    indicador.faturamento_total = kpis["faturamento_total"]
    indicador.total_comprado = kpis["total_comprado"]
    indicador.saldo_estimado_compras_vendas = kpis["saldo_estimado_compras_vendas"]
    indicador.produto_mais_vendido_nome = kpis["produto_mais_vendido_nome"]
    indicador.produto_mais_vendido_quantidade = kpis["produto_mais_vendido_quantidade"]
    indicador.produto_maior_faturamento_nome = kpis["produto_maior_faturamento_nome"]
    indicador.produto_maior_faturamento_valor = kpis["produto_maior_faturamento_valor"]
    indicador.produto_maior_saldo_parado_nome = kpis["produto_maior_saldo_parado_nome"]
    indicador.saldo_estimado_parado = kpis["saldo_estimado_parado"]
    indicador.periodo_base_inicio = kpis.get("periodo_base_inicio")
    indicador.periodo_base_fim = kpis.get("periodo_base_fim")
    indicador.data_geracao = datetime.now(timezone.utc)


def _persistir_curva_abc(db_session, id_analise: int, kpis: dict) -> None:
    """
    Regrava (idempotente) a Curva ABC completa em produto_curva_abc: apaga as linhas
    anteriores desta análise e insere o ranking novo. Reflete o reprocessamento — cada
    execução do ETL substitui a Curva ABC anterior. Em lote, porque agora são todos os
    produtos do período, e não só os primeiros.
    """
    from sqlalchemy import delete, insert
    from models import ProdutoCurvaABC
    # Limpa o ranking anterior desta análise (reprocessamento limpo).
    db_session.execute(delete(ProdutoCurvaABC).where(ProdutoCurvaABC.id_analise == id_analise))

    linhas = [
        {
            "id_analise": id_analise,
            "posicao_ranking": p["posicao"],
            "produto_nome": p["nome"],
            "faturamento": p["faturamento"],
            "quantidade": p.get("quantidade"),
            "percentual_individual": p["perc_individual"],
            "percentual_acumulado": p["perc_acumulado"],
            "classe_abc": p["classe"],
        }
        for p in kpis.get("curva_abc", {}).get("produtos", [])
    ]
    if linhas:
        db_session.execute(insert(ProdutoCurvaABC), linhas)


def processar_arquivos_analise(caminho_vendas: str, caminho_compras: str,
                               id_analise: int, db_session) -> ResultadoETL:
    """
    Orquestrador oficial (assinatura imutável). Calcula em memória e persiste
    APENAS os KPIs finais. NÃO comita (caller controla transação) e NÃO
    persiste linhas processadas. Em falha, retorna sucesso=False sem gravar.
    """
    try:
        resultado = calcular_kpis_em_memoria(caminho_vendas, caminho_compras)
        # Somente o processo pai recebe a Session. O SAVEPOINT mantém KPIs/ABC
        # anteriores intactos em falha, sem soltar o lock externo da análise.
        with db_session.begin_nested():
            _persistir_indicadores(db_session, id_analise, resultado.kpis)
            _persistir_curva_abc(db_session, id_analise, resultado.kpis)
            db_session.flush()
        return resultado._replace(
            mensagem="Processamento concluído. KPIs consolidados + Curva ABC persistidos."
        )
    except ETLInputError as erro:
        logger.warning("ETL recusado (%s).", erro.code)
        return ResultadoETL(sucesso=False, mensagem=erro.safe_message,
                            kpis={}, telemetria={}, alertas=[])
    except Exception as erro:  # não registrar SQL, caminhos, conteúdo ou credenciais
        logger.warning("Processamento ETL indisponível (%s).", type(erro).__name__)
        return ResultadoETL(sucesso=False, mensagem="Não foi possível concluir o processamento com segurança. Tente novamente ou revise os arquivos.",
                            kpis={}, telemetria={}, alertas=[])
