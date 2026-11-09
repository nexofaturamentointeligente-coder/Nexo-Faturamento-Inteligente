"""
NEXO - Faturamento Inteligente | Motor ETL UNIVERSAL (v3)
==========================================================
Processa relatórios de PDV (Vendas + Compras) com mapeamento DINÂMICO de
cabeçalhos: independe da posição das colunas, do nome exato, do tamanho da
janela temporal (15 dias, mês, trimestre, ano ou histórico inteiro) e
tolera sujeira (linhas decorativas, quebras de página, totalizações,
células e linhas em branco entre os registros).

Estratégia (4 camadas):
  1) Leitura crua: lê o arquivo SEM assumir cabeçalho (header=None,
     dtype=str), preservando a matriz como string para a varredura.
  2) Detecção de cabeçalho: varre as primeiras 60 linhas e procura a
     linha (ou 2 linhas adjacentes, no caso de cabeçalho composto) que
     MELHOR mapeia os aliases canônicos via substring normalizada.
  3) DataFrame canônico: a partir da 1ª linha de dados, monta um DF só
     com as colunas reconhecidas, renomeadas para nomes canônicos.
  4) Filtro de lixo: descarta linhas onde a coluna-chave é nula ou
     contém marcadores de rodapé/cabeçalho repetido (totais, "Movimenta",
     marca d'água "LabSofti" etc.).

Regras inegociáveis (MVP/PI2):
  - DataFrames vivem APENAS em memória; só os KPIs finais são persistidos.
  - O indicador de descasamento é "Indicador de Pressão de Estoque"
    (saldo estimado entre compras e vendas). NUNCA expor lucro, margem,
    CMV, prejuízo, rentabilidade ou estoque real como KPI.
  - 'Lucro' e 'Total Custo' do PDV são DETECTADOS para mapeamento
    defensivo (não confundem com outras colunas) e DESCARTADOS para fins
    de KPI. 'Total Custo' pode gerar APENAS o alerta "venda abaixo do
    custo informado pelo PDV" — observação do dado bruto, NÃO margem.
    A margem bruta estimada do Raio-X do PDV (raio_x.py, liberada por plano)
    é outro produto e não entra nos KPIs, no PDF nem na devolutiva.
  - Falha de leitura/validação nunca é mascarada com dados falsos.
  - Status CONCLUIDO da análise nunca é setado aqui — é decisão humana via admin.
    (O CONCLUIDO de validar_total_pdv é só o resultado da conferência com o PDV.)

Assinatura imutável:
  processar_arquivos_analise(caminho_vendas, caminho_compras,
                             id_analise, db_session) -> ResultadoETL
"""

import os
import re
import csv
import io
import logging
import math
import numbers
import unicodedata
from datetime import datetime, timezone
from collections import Counter, namedtuple
from decimal import Decimal, ROUND_HALF_UP, localcontext

import numpy as np
import pandas as pd
from sqlalchemy import delete, insert, select

from etl_inputs import ETLInputError, read_raw
from etl_limits import get_limits

logger = logging.getLogger(__name__)

ResultadoETL = namedtuple(
    "ResultadoETL", ["sucesso", "mensagem", "kpis", "telemetria", "alertas"]
)

# Limiares do Princípio de Pareto (Curva ABC) sobre o faturamento ACUMULADO:
#   Classe A (Estratégicos): até 80%
#   Classe B (Táticos):      acima de 80% até 95%
#   Classe C (Operacionais): acima de 95%
ABC_LIMITE_A = 80.0
ABC_LIMITE_B = 95.0

# Trava do total do PDV: diferença maior que a tolerância bloqueia a publicação.
TOLERANCIA_PDV = Decimal("1.00")
STATUS_PDV_CONCLUIDO = "CONCLUIDO"
STATUS_PDV_DIVERGENTE = "DIVERGENTE_PDV"


class ETLValidationError(ETLInputError):
    """Mantém o tipo público anterior e a identificação segura de limites."""

    def __init__(self, mensagem, code="data"):
        super().__init__(code, mensagem)


# Encodings e separadores tentados em sequência para arquivos texto/CSV
ENCODINGS = ("utf-8-sig", "cp1252", "utf-8", "latin1")
SEPARADORES = (";", ",", "\t", "|")


# =====================================================================
# 1) Sanitização de strings
# =====================================================================
def _strip_acentos(texto) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(texto))
        if not unicodedata.combining(c)
    )


def _norm_chave(valor) -> str:
    """
    Normalização AGRESSIVA para casar fragmentos do dicionário de aliases:
      - remove BOM/NBSP/quebras de linha;
      - tira acentos; vira minúsculas;
      - substitui pontuação por espaço; colapsa múltiplos espaços.
    Resultado é seguro para `fragmento in chave_normalizada`.
    """
    if valor is None:
        return ""
    s = str(valor).replace("﻿", "").replace("\xa0", " ")
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    s = _strip_acentos(s).lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalizar_produto(valor) -> str:
    """Chave de agrupamento por produto (uppercase, sem acentos, sem pontuação)."""
    s = _strip_acentos(str(valor or "")).upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Limite do nome EXIBÍVEL do produto: um valor vindo da planilha não deve
# estourar as colunas do banco, o layout do PDF nem os rótulos dos gráficos.
LIMITE_NOME_PRODUTO = 200


def _nome_exibivel(valor) -> str:
    """Nome de produto com teto de tamanho (proteção de armazenamento/layout) e
    sem NUL. Preserva o restante do conteúdo: os números financeiros e os nomes
    permanecem equivalentes ao motor original; a sanitização para uma linha é
    responsabilidade da camada de exibição (templates/PDF). Não é a chave de
    agrupamento (essa é _normalizar_produto)."""
    return str(valor or "").replace("\x00", "")[:LIMITE_NOME_PRODUTO]


# Linhas que NÃO são produto e contaminam rankings/Curva ABC quando o PDV as
# exporta como itens (ex.: "Descontos:" aparecendo como 50% do faturamento).
# Comparadas contra o nome JÁ normalizado por _normalizar_produto (uppercase,
# sem acento/pontuação). Match exato para não derrubar produtos reais.
_NAO_PRODUTOS = {
    "DESCONTO", "DESCONTOS", "ACRESCIMO", "ACRESCIMOS", "FRETE", "TROCO",
    "TAXA", "TAXAS", "JUROS", "TOTAL", "SUBTOTAL", "TOTAL GERAL",
    "ARREDONDAMENTO", "ENTRADA", "SANGRIA", "SUPRIMENTO",
}


# =====================================================================
# 2) Conversão numérica robusta (pt-BR e en-US)
# =====================================================================
# Quantidade fracionada como o PDV escreve: "1/2", "3/4 kg", "1 1/2 un(s)", "(1/2)", "(1/2) KG",
# "1/2-" e "1/2 KG-". Grupos: sinal no começo, inteiro, numerador, denominador, sinal logo depois
# do número e o resto (unidade, parêntese e o sinal no fim). Só texto curto (_TETO_DA_FRACAO): é
# quantidade, e nada de célula longa passa por regex.
_RE_FRACAO = re.compile(r"\(?\s*([+-])?\s*(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)(-)?([^\d/+]*)")
_TETO_DA_FRACAO = 64
# Acima do teto, só a forma simples ("1000...0/1", "-9999/1", "(1/2)"), sem espaços, conferida sem
# retrocesso: o valor gigante vira infinito, como os dígitos colados viravam antes.
_RE_FRACAO_LONGA = re.compile(r"(\(?)([+-]?)(\d+)/(\d+)(-?)\)?")
# Data numa coluna de quantidade não é quantidade: "2026-01-02" viraria 20260102.
_RE_DATA_COMO_TEXTO = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?")
_RE_CIENTIFICO_QUANTIDADE = re.compile(r"[+-]?\d+(?:[.,]\d+)?[eE][+-]?\d+")


# Número seguido da unidade, numa unidade com barra ("2 UN/CX", "1,250 KG/UN", "18,350/KG",
# "(2) UN/CX", "1 P/ KG"): a quantidade é o número do começo, com o parêntese, como sempre foi.
_RE_NUMERO_ANTES_DA_UNIDADE = re.compile(r"(\(?\s*[+-]?\s*\d[\d.,]*\s*\)?)\s*-?\s*/?\s*[^\W\d_]")


def _parse_fracao(texto: str):
    """Valor da fração ("1 1/2" = 1,5), com o sinal; None se não for fração ou se dividir por zero.
    Fração grande demais para um float vira infinito, como os dígitos colados viravam antes, e a
    leitura recusa o arquivo com a mensagem de valor fora do intervalo."""
    if len(texto) > _TETO_DA_FRACAO:
        longa = _RE_FRACAO_LONGA.fullmatch(texto.replace(" ", ""))
        if longa is None:
            return None
        parentese, sinal, numerador, denominador, sinal_no_fim = longa.groups()
        inteiro = None
        negativo = sinal == "-" or sinal_no_fim == "-" or (parentese == "(" and texto.rstrip().endswith(")"))
    else:
        encontrado = _RE_FRACAO.fullmatch(texto)
        if not encontrado:
            return None
        sinal, inteiro, numerador, denominador, sinal_logo_depois, resto = encontrado.groups()
        # O resto só pode ter um sinal, e no fim ("1/2 KG-"); "1/2-3" não é fração.
        if "-" in resto.rstrip().rstrip("-"):
            return None
        negativo = (sinal == "-" or sinal_logo_depois == "-" or resto.rstrip().endswith("-")
                    or (texto.lstrip().startswith("(") and ")" in texto))
    # Zeros à esquerda fora antes da conta: o int() do Python recusa texto com mais de 4.300 dígitos,
    # e "000…0/1" vale 0, e não infinito; "1/000…0" divide por zero.
    inteiro, numerador, denominador = ((parte or "0").lstrip("0") or "0" for parte in (inteiro, numerador, denominador))
    if denominador == "0":
        return None
    try:
        valor = int(inteiro) + int(numerador) / int(denominador)
    except (OverflowError, ValueError):
        valor = math.inf
    return -valor if (negativo and valor > 0) else valor


def _parse_valor(bruto):
    """
    Aceita 'R$ 1.234,56', '1.234,56', '1234,56', '1,234.56', '1234.56',
    '(123,45)', ' - ', '', None — retorna float ou None se irrecuperável.

    Só para quantidades e estoque: aqui o ponto único é decimal, porque "1.500" numa
    coluna de quantidade costuma ser 1,5 kg. Dinheiro usa converter_valor_monetario.

    Como no dinheiro, o sinal contábil depois do número ("2-", "150,00-") e o menos
    tipográfico ("−2") são negativos, e "1E+03" é notação científica. Fração ("1/2",
    "1 1/2 kg") vira o valor dela. Data, faixa ("1-2") e barra solta não são quantidade:
    None, em vez de um número feito com os dígitos ("1/2" já deu 12).
    """
    if bruto is None:
        return None
    s = str(bruto).strip()
    # O apóstrofo da neutralização de fórmula (security_gatekeeper) não é sinal nem dígito:
    # sem tirá-lo, "'-2 un(s)" seria lido como +2 e a devolução viraria venda. O dinheiro já
    # o ignora (_limpar_monetario). Sai também o de dentro da célula ("1,'-5"), que o
    # núcleo abaixo sempre descartou: original e neutralizado têm de dar o mesmo número.
    s = s.replace("'", "").strip()
    if s.lower() in ("", "nan", "none", "-", "--"):
        return None

    s = s.translate(_SINAIS_DE_MENOS).replace("\xa0", " ").strip()
    if _RE_DATA_COMO_TEXTO.fullmatch(s):
        return None
    if "/" in s:
        # Espaços colapsados antes das regex da barra: com um espaço só entre as partes, os \s*
        # seguidos de _RE_NUMERO_ANTES_DA_UNIDADE não retrocedem (400 espaços levavam 40 s). As regex
        # aceitam qualquer quantidade de espaço ali; o resultado só muda acima do teto da fração
        # (64 caracteres), em que a fração com unidade e espaços demais passa a caber e ser lida.
        s = re.sub(r"\s+", " ", s)
        fracao = _parse_fracao(s)
        if fracao is not None:
            return fracao
        # A barra da unidade ("2 UN/CX") não é fração: vale o número do começo, como sempre valeu.
        antes_da_unidade = _RE_NUMERO_ANTES_DA_UNIDADE.match(s)
        if antes_da_unidade is None:
            return None
        s = antes_da_unidade.group(1).strip()
    s = s.replace(" ", "")
    s = re.sub(r"(?i)r\$", "", s)
    if _RE_CIENTIFICO_QUANTIDADE.fullmatch(s):
        numero = float(s.replace(",", "."))
        return numero if math.isfinite(numero) else None
    negativo = s.startswith("-") or s.startswith("(")
    if s.endswith("-") and not s.startswith("-"):
        negativo, s = True, s[:-1]
    # Hífen entre dígitos não é sinal: "1-2" é faixa ou código, não quantidade.
    if re.search(r"[0-9.,]-+[0-9]", s):
        return None
    s2 = re.sub(r"[^0-9.,]", "", s)
    if s2 == "":
        return None

    tem_virg = "," in s2
    tem_pto = "." in s2
    if tem_virg and tem_pto:
        # o separador mais à direita é o decimal
        if s2.rfind(",") > s2.rfind("."):
            s2 = s2.replace(".", "").replace(",", ".")
        else:
            s2 = s2.replace(",", "")
    elif tem_virg:
        # padrão BR: vírgula é decimal
        s2 = s2.replace(".", "").replace(",", ".")
    elif tem_pto and s2.count(".") > 1:
        # múltiplos pontos => separadores de milhar
        s2 = s2.replace(".", "")
    # único ponto = decimal => mantém

    try:
        val = float(s2)
    except ValueError:
        return None
    return -val if (negativo and val > 0) else val


def _converter_serie(serie: pd.Series) -> pd.Series:
    return pd.Series([_parse_valor(v) for v in serie], index=serie.index, dtype="float64")


# Milhar só com grupos de 3 dígitos depois de um início sem zero: "0.125" nunca é milhar.
_RE_MILHAR_PONTO = re.compile(r"[1-9][0-9]{0,2}(?:\.[0-9]{3})+")
_RE_MILHAR_VIRGULA = re.compile(r"[1-9][0-9]{0,2}(?:,[0-9]{3})+")
# A mesma linguagem de sempre ("[0-9]+[.,]?[0-9]*|[.,][0-9]+"), escrita sem retrocesso quadrático:
# uma célula com 16 mil dígitos levava 3 s.
_RE_DECIMAL = re.compile(r"[0-9]+(?:[.,][0-9]*)?|[.,][0-9]+")
_RE_CASAS_DECIMAIS = re.compile(r"[0-9]+")
_RE_CIENTIFICO = re.compile(r"[0-9]+(?:[.,][0-9]+)?[eE][+-]?[0-9]+")
_RE_PONTO_DECIMAL = re.compile(r"[0-9,]*\.[0-9]+")
_RE_VIRGULA_DECIMAL = re.compile(r"[0-9.]*,[0-9]+")
_RE_MOEDA = re.compile(r"r\$|us\$|brl|usd|\$", re.IGNORECASE)
_SINAIS_DE_MENOS = str.maketrans({"−": "-", "–": "-", "﹣": "-", "－": "-"})
_VALORES_EM_BRANCO = {"", "-", "--", "nan", "none", "null", "na", "n/a", "nd", "n/d"}


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
    `virgula_milhar`: a coluna é en-US pelo texto das células (_coluna_usa_virgula_de_milhar), então
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
    como decimal (ver _coluna_usa_ponto_decimal); `virgula_milhar=True` lê "12,500" como
    12500.0, para colunas en-US pelo texto (ver _coluna_usa_virgula_de_milhar). Um valor
    sozinho não tem como revelar nenhum dos dois.
    """
    numero = _interpretar_monetario(valor, ponto_decimal, virgula_milhar)
    return 0.0 if numero is None else numero


def _coluna_usa_ponto_decimal(corpos: list) -> bool:
    """A coluna tem valor com ponto que não pode ser milhar ("12.50", "1,234.56") e
    nenhum com vírgula decimal ("1.234,50"): nela, "12.500" é 12,5, e não 12.500,00."""
    com_ponto = com_virgula = False
    for corpo in corpos:
        if _RE_VIRGULA_DECIMAL.fullmatch(corpo):
            com_virgula = True
        elif _RE_PONTO_DECIMAL.fullmatch(corpo) and not _RE_MILHAR_PONTO.fullmatch(corpo):
            com_ponto = True
    return com_ponto and not com_virgula


def _coluna_usa_virgula_de_milhar(brutos, corpos: list) -> bool:
    """A coluna é en-US pelo TEXTO das células: alguma tem ponto decimal ("12.50", "1,234.56")
    e nenhuma tem vírgula decimal que não possa ser milhar ("12,50", "1.234,56"). Só texto conta:
    número de verdade (célula numérica do XLSX) não diz como o relatório escreve os valores, e
    "12,500" e "12.500" são ambíguos."""
    com_ponto = com_virgula = False
    for bruto, corpo in zip(brutos, corpos):
        if not isinstance(bruto, str):
            continue
        if _RE_VIRGULA_DECIMAL.fullmatch(corpo) and not _RE_MILHAR_VIRGULA.fullmatch(corpo):
            com_virgula = True
        elif _RE_PONTO_DECIMAL.fullmatch(corpo) and not _RE_MILHAR_PONTO.fullmatch(corpo):
            com_ponto = True
    return com_ponto and not com_virgula


# Marca interna em `qualidade`: alguma coluna em dinheiro do arquivo é en-US pelo texto. Sai antes de
# chegar à telemetria (_fechar_qualidade).
_EN_US = "_en_us"


def _converter_serie_monetaria(serie: pd.Series, qualidade: Counter, texto_do_csv: bool = False) -> pd.Series:
    """Converte uma coluna em dinheiro e anota em `qualidade` o que pede revisão humana.

    `texto_do_csv`: a coluna veio de um CSV, em que toda célula é o texto que o relatório escreveu.
    Só aí o formato en-US ("1,234.56") pode ser provado e "12,500" lido como milhar. No XLSX as
    células chegam como texto também (dtype=str), mas o número de verdade vira "32.9" e não diz
    como o relatório escreve: lá vale a leitura de sempre."""
    brutos = list(serie)
    corpos = [_separar_sinal(_limpar_monetario(v))[1] for v in brutos]
    ponto_decimal = _coluna_usa_ponto_decimal(corpos)
    virgula_milhar = texto_do_csv and _coluna_usa_virgula_de_milhar(brutos, corpos)
    if virgula_milhar:
        qualidade[_EN_US] += 1
    valores = []
    for bruto, corpo in zip(brutos, corpos):
        numero = _interpretar_monetario(bruto, ponto_decimal, virgula_milhar)
        if numero is None:
            if corpo.lower() not in _VALORES_EM_BRANCO:
                qualidade["invalidos"] += 1
            numero = 0.0
        elif corpo.count(".") == 1 and _RE_MILHAR_PONTO.fullmatch(corpo):
            qualidade["lidos_como_decimal" if ponto_decimal else "lidos_como_milhar"] += 1
        elif corpo.count(",") == 1 and _RE_MILHAR_VIRGULA.fullmatch(corpo):
            qualidade["virgula_como_milhar" if virgula_milhar else "virgula_como_decimal"] += 1
        valores.append(numero)
    return pd.Series(valores, index=serie.index, dtype="float64")


def _valor_informado(serie: pd.Series) -> pd.Series:
    """A célula traz um número, mesmo que zero ("0,00"), e não vazio, "-" ou texto inválido. Separa
    a venda de valor zero informado (brinde, bonificação) da célula em branco."""
    return pd.Series([_interpretar_monetario(valor) is not None for valor in serie], index=serie.index, dtype=bool)


def _fechar_qualidade(qualidade: Counter) -> dict:
    """O que vai para a telemetria: "virgula_como_decimal" só quando o arquivo tem alguma coluna
    en-US pelo texto. Num relatório todo brasileiro, "31,125" com 3 casas é decimal e não pede
    revisão, como sempre foi."""
    en_us = qualidade.pop(_EN_US, 0)
    if not en_us:
        qualidade.pop("virgula_como_decimal", None)
    return dict(qualidade)


# =====================================================================
# 3) Leitura defensiva (.xlsx / .csv) sem assumir cabeçalho
# =====================================================================
def _trim_vazios(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove linhas e colunas COMPLETAMENTE vazias da matriz crua. Resolve dois
    tipos de "sujeira" comuns em exportações de PDV sem mascarar dado real:
      - linhas em branco decorativas no topo (empurram o cabeçalho para baixo);
      - colunas vazias à esquerda/entre dados (deslocamento de colunas).
    `how="all"` só descarta o que é integralmente nulo — nenhum dado é perdido.
    """
    if df is None or df.empty:
        return df
    limpo = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    return limpo.reset_index(drop=True) if not limpo.empty else df


def _ler_bruto(caminho_arquivo: str) -> pd.DataFrame:
    """Leitura limitada; conversões e seleção de aba preservam o contrato anterior."""
    try:
        return read_raw(caminho_arquivo, get_limits())
    except ETLInputError as erro:
        raise ETLValidationError(erro.safe_message, erro.code) from None


# =====================================================================
# 4) Dicionário de aliases — mapeamento DINÂMICO por substring
# =====================================================================
# IMPORTANTE: a ORDEM dos canônicos importa. Fragmentos mais ESPECÍFICOS
# devem vir ANTES de fragmentos mais GENÉRICOS, para evitar colisões
# (ex.: "Qtde Estoque" cair em quantidade, "Vr Total Produtos" cair em
# valor_nf). Todos os fragmentos estão escritos JÁ na forma normalizada
# (sem acento, minúsculas, sem pontuação, sem espaços extras).

ALIAS_VENDAS = {
    # codigo PRECISA vir antes de produto: assim a coluna "id_produto" (ex.: P001)
    # é reivindicada como código e NÃO rouba o canônico 'produto' do nome real
    # ("descricao_produto"/"nome_produto"). A ordem do dicionário é a inteligência.
    "codigo":      ["id produto", "id do produto", "codigo produto", "codigo",
                    "cod produto", "cod barras", "ean", "sku", "ref", "cod "],
    "produto":     ["nome do produto", "descricao do produto", "descricao produto",
                    "nome produto", "produto", "mercadoria", "descricao do item", "descricao"],
    # estoque ANTES de quantidade — "Qtde Estoque" não pode cair em qtd
    "estoque":     ["estoque atual", "saldo estoque", "saldo atual", "estoque"],
    # custo ANTES de venda — só por consistência
    "valor_custo": ["total custo", "vr custo", "vlr custo", "valor custo",
                    "custo total", "custo"],
    # 'faturamento' é o TOTAL por linha (qtd x preço) → é o valor de venda real.
    # Mantemos 'preco/preco_unitario' FORA daqui de propósito: somar preço
    # unitário inflaria o faturamento. Casamos o total, nunca o unitário.
    "valor_venda": ["faturamento", "arrecadado", "total venda", "total vendas",
                    "vr venda", "vlr venda", "valor total venda", "total liquido",
                    "valor venda", "vr total", "valor total"],
    # preço UNITÁRIO (vem DEPOIS de valor_venda: o total explícito sempre vence).
    # Só é usado para DERIVAR o total (qtd x unitário) quando não há coluna de total.
    "preco_unitario": ["preco unitario", "preco unit", "valor unitario", "vlr unitario",
                       "preco venda", "preco"],
    "quantidade":  ["qtde vend", "qtde vendida", "quantidade vendida", "qtd vendida",
                    "qtde total", "qtd vend", "qtde", "quantidade", "qtd"],
    # 'lucro' é DETECTADO defensivamente (reservar a coluna) e DESCARTADO
    # no cálculo. NEXO/PI2 NÃO usa lucro/CMV como KPI — terminologia oficial.
    "lucro":       ["lucro"],
}

ALIAS_COMPRAS = {
    # codigo no topo, mesmo motivo do dicionário de vendas (absorve "id_produto").
    "codigo":         ["id produto", "id do produto", "codigo produto", "codigo",
                       "cod produto", "cod barras"],
    "data":           ["data emissao", "data nf", "data entrada", "data compra", "data"],
    "fornecedor":     ["razao social fornecedor", "nome fornecedor", "fornecedor",
                       "razao social", "emitente"],
    # produtos_total ANTES de valor_nf — "Vr Total Produtos" é mais específico
    "produtos_total": ["total produtos", "vr total produtos", "vr produtos",
                       "total mercadorias", "valor produtos"],
    # valor da compra a nível de NF OU de item: 'total_compra' (C1) e
    # 'total_custo' (C4 item-level) são o total da linha — nunca o unitário.
    "valor_nf":       ["valor nf", "total compra", "total custo", "vr total", "total nf",
                       "total da nota", "valor total", "valor nota", "vr nf", "vr nota"],
    # custo UNITÁRIO (depois de valor_nf: o total explícito sempre vence). Usado
    # para DERIVAR o total (qtd x unitário) quando não há coluna de total.
    "custo_unitario": ["custo unitario", "custo unit", "vr unitario", "vlr unitario",
                       "preco unitario", "valor unitario"],
    # Colunas item-a-item (relatórios de compra por produto, não por NF):
    "produto":        ["nome do produto", "descricao do produto", "descricao produto",
                       "nome produto", "descricao", "mercadoria", "produto"],
    "qtde_comprada":  ["qtde comprada", "quantidade comprada", "qtd comprada",
                       "qtde compra", "qtd compra", "qtde"],
}

# Títulos de reserva: rótulos comuns de ERP que os dicionários acima não reconhecem ("Quant.",
# "Vlr. Total", "CMV"). Valem só como reserva, depois do dicionário principal, e por isso um
# relatório que já era lido mantém o mesmo mapeamento:
#   - comparam o título INTEIRO normalizado, nunca fragmento: "Quantia" não vira quantidade e
#     "Vlr Total Desconto" não vira venda;
#   - só preenchem o canônico que o dicionário principal deixou sem coluna;
#   - só usam coluna que nenhum fragmento do dicionário principal reconhece, nem como duplicata.
# Ficam de fora, por ambiguidade: "Valor" e "Valor Bruto" (unitário? sem desconto?), "Receita",
# "Vendas" e "Un", que costuma ser a unidade de medida ("UN", "KG"), e não a quantidade.
# "Total" sozinho é o total da linha num relatório por item.
RESERVA_VENDAS = {
    "valor_custo":    ("cmv", "cmv total"),
    "valor_venda":    ("vlr total", "vl total", "valor liquido", "vlr liquido", "vl liquido",
                       "total", "total r"),
    "preco_unitario": ("vr unit", "vlr unit", "vl unit", "vr unitario", "vl unitario"),
    "quantidade":     ("quant", "quant vendida", "quant vend"),
}
RESERVA_COMPRAS = {
    "valor_nf":       ("custo total", "vlr total", "vl total", "total", "total r"),
    "custo_unitario": ("vr unit", "vlr unit", "vl unit", "vl unitario"),
    "qtde_comprada":  ("quantidade", "qtd", "quant"),
}


def _reserva_de(aliases: dict) -> dict:
    """Títulos de reserva do dicionário principal; outro dicionário não tem reserva."""
    if aliases is ALIAS_VENDAS:
        return RESERVA_VENDAS
    if aliases is ALIAS_COMPRAS:
        return RESERVA_COMPRAS
    return {}


def _mapear_cabecalho(linha_norm: list, aliases: dict, reserva: dict = None,
                      percentuais: frozenset = frozenset()) -> dict:
    """
    Para cada COLUNA da linha (já normalizada), encontra o 1º canônico cujo
    fragmento aparece como substring. Cada canônico é claimed no máximo 1x
    (1ª coluna vence — proteção contra duplicatas no relatório).

    Depois, os títulos de reserva (RESERVA_VENDAS/RESERVA_COMPRAS) completam só os
    canônicos que ficaram sem coluna, e só com coluna que o dicionário não reconhece.
    `percentuais`: colunas cujo título tem "%" ("% Total", "Total (%)", "% CMV"), que a
    normalização transforma em "total" ou "cmv" e a reserva nunca usa. Título de reserva repetido
    na mesma linha ("Quant." sobre "Pedida" e "Quant." sobre "Vendida") é ambíguo e também não vale:
    o cabeçalho em duas linhas, se houver, desfaz a dúvida.
    """
    mapa: dict = {}
    for idx, chave in enumerate(linha_norm):
        if not chave:
            continue
        for canonico, fragmentos in aliases.items():
            if canonico in mapa:
                continue
            for frag in fragmentos:
                if frag in chave:
                    mapa[canonico] = idx
                    break
            if canonico in mapa and mapa[canonico] == idx:
                break

    reserva = _reserva_de(aliases) if reserva is None else reserva
    faltantes = [canonico for canonico in reserva if canonico not in mapa]
    if not faltantes:
        return mapa
    fragmentos = [frag for lista in aliases.values() for frag in lista]
    repeticoes = Counter(chave for chave in linha_norm if chave)
    livres = {idx: chave for idx, chave in enumerate(linha_norm)
              if chave and idx not in percentuais and repeticoes[chave] == 1
              and not any(frag in chave for frag in fragmentos)}
    for canonico in faltantes:
        for idx, chave in livres.items():
            if chave in reserva[canonico] and idx not in mapa.values():
                mapa[canonico] = idx
                break
    return mapa


def _detectar_cabecalho(df_bruto: pd.DataFrame, aliases: dict, valida_fn,
                        janela: int = 100):
    """
    Localiza a linha de cabeçalho na janela inicial:
      1) Cabeçalho de UMA linha — escolhe a que mapeia MAIS canônicos
         entre as que passam por `valida_fn` (cobertura mínima).
      2) Cabeçalho COMPOSTO (2 linhas adjacentes concatenadas) — fallback
         para PDVs que quebram títulos em duas linhas.
    Retorna (idx_cabecalho, mapa_canonico, linhas_cabecalho).

    Em caso de falha, levanta ETLValidationError com DIAGNÓSTICO acionável
    (quais colunas foram reconhecidas) — nunca fabrica colunas: um relatório
    sem as colunas essenciais não vira KPI inventado (integridade do dado).

    A busca usa primeiro só o dicionário principal, como sempre: os títulos de reserva
    (RESERVA_VENDAS/RESERVA_COMPRAS) completam a linha escolhida e só entram na busca quando
    nenhuma linha passa sem eles. Assim um cabeçalho em duas linhas que o dicionário já lia
    ("Total" sobre "Líquido") não é trocado pela primeira linha sozinha.
    """
    limite = min(janela, len(df_bruto))
    melhor_parcial: dict = {}   # melhor mapeamento visto, mesmo sem passar na validação
    linhas = [df_bruto.iloc[i].tolist() for i in range(limite)]
    simples = [[_norm_chave(v) for v in linha] for linha in linhas]
    # zip por menor comprimento — colunas extras ficam de fora
    compostas = [[(a + " " + b).strip() for a, b in zip(simples[i], simples[i + 1])]
                 for i in range(limite - 1)]
    com_percentual = [frozenset(j for j, v in enumerate(linha) if v is not None and "%" in str(v)) for linha in linhas]
    percentuais = {1: com_percentual, 2: [com_percentual[i] | com_percentual[i + 1] for i in range(limite - 1)]}
    reserva = _reserva_de(aliases)
    for com_reserva in ((False, True) if reserva else (False,)):
        # 1) cabeçalho de 1 linha; 2) cabeçalho composto (2 linhas)
        for linhas_cab, candidatas in ((1, simples), (2, compostas)):
            melhor = None
            for i, titulos in enumerate(candidatas):
                mapa = _mapear_cabecalho(titulos, aliases, reserva if com_reserva else {},
                                         percentuais[linhas_cab][i])
                if len(mapa) > len(melhor_parcial):
                    melhor_parcial = mapa
                if valida_fn(mapa) and (melhor is None or len(mapa) > len(melhor[1])):
                    melhor = (i, mapa)
            if melhor is not None:
                i = melhor[0]
                return i, _mapear_cabecalho(candidatas[i], aliases, reserva, percentuais[linhas_cab][i]), linhas_cab

    reconhecidas = ", ".join(sorted(melhor_parcial.keys())) if melhor_parcial else "nenhuma"
    raise ETLValidationError(
        f"Não consegui identificar as colunas essenciais nas primeiras {limite} "
        f"linhas do relatório. Colunas reconhecidas: {reconhecidas}. "
        "Confirme que o arquivo é o relatório tabular do PDV — VENDAS precisa de "
        "produto, quantidade e valor; COMPRAS precisa de fornecedor e valor — e reenvie."
    )


# =====================================================================
# 5) DataFrame canônico + limpeza de linhas-lixo
# =====================================================================
# Rodapé, título de seção e controle de impressão que o PDV exporta no meio dos itens. Duas partes:
#
# 1. O filtro de sempre (_PADRAO_LIXO_ANTIGO, até 11/2026) é o piso: a linha que ele tirava continua
#    saindo. Ele casa palavra inteira sem acento no texto cru ("filtro", "software", "impresso",
#    "resumo", "periodo", "pagina", "usuario"...) e com isso também apagava produtos. A exceção são
#    as formas de nome de produto com "filtro", "software" ou "impresso" (_forma_de_produto):
#    "Filtro de Café 103", "Software Antivírus", "Papel Impresso", "Cigarro sem Filtro". Com palavra
#    de rodapé por perto, dois-pontos, data ou hora ("Filtro Loja 01", "FILTRO TODOS", "Relatório
#    impresso às 10:35"), a linha continua rodapé; com "resumo", "periodo", "pagina" e "usuario" sem
#    acento, sempre, como no filtro antigo.
# 2. Os rodapés que o filtro de sempre deixava passar e a diretriz manda tirar, só nas formas
#    inequívocas (_RE_RODAPE_EXPLICITO): "Total do Dia", "Subtotal", "Total Geral", "Página 2 de 5",
#    "Movimentação do Dia" e os totais por agrupamento e por forma de pagamento. Produto que o filtro
#    de sempre mantinha ("Transporte", "Venda Caixa Fechada", "Marcador de Página 3M") continua.
#
# A parte 2 e as exceções comparam o texto sem acento e em minúsculas, com a pontuação de dentro
# preservada, porque é ela que separa "Filtro: Todos" (rodapé) de "Filtro de Café" (produto).
_PADRAO_LIXO_ANTIGO = re.compile(
    r"\b(?:total geral|totais|subtotal|movimenta|labsofti|software|resumo|periodo|"
    r"usuario|filtro|impresso|pagina)\b",
    re.IGNORECASE,
)
_MESES = r"janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro"
_AGRUPAMENTOS = (
    r"dia|diari[oa]|data|semana|semanal|quinzena|mes|mensal|trimestre|ano|anual|periodo|pagina|pag|"
    r"folha|turno|grupo|subgrupo|categoria|departamento|depto|secao|setor|familia|marca|laboratorio|"
    r"fabricante|vendedor(?:a|es|as)?|frentista|bico|bomba|loja|caixa|pdv|ecf|operador|usuario|filial|"
    r"empresa|cliente|fornecedor|relatorio|movimento|movimentacao|vendas?|compras?|comprado|entradas?|"
    rf"itens|produtos|documento|cupom|cupons|nfe?|notas?|anterior|descontos?|acrescimos?|{_MESES}|"
    # Totais por forma de pagamento no fechamento do caixa ("Total Dinheiro", "Total em Cartão").
    r"dinheiro|cartao|cartoes|credito|debito|pix|cheques?|crediario|convenio|vale|ticket|"
    r"formas? de pagamento|a prazo|a vista"
)
# O que vem antes de "total" num rodapé ("Valor Total do Dia", "Qtde Total Período").
_ANTES_DO_TOTAL = r"(?:(?:valor|vlr|vl|vr|qtde?|qtd|quantidade) )?"
_TOTAL = r"(?:total|tot\.?)"
_PREPOSICAO = r"(?: (?:d[oa]s?|de|por|no|na|nos|nas|em|neste|nesta|deste|desta))?"
# Parte 2, casada no início do texto (re.match). Sem quantificador aninhado: nada aqui retrocede de
# forma exponencial (a linha que é só data vai por _linha_de_data).
_RE_RODAPE_EXPLICITO = re.compile(
    rf"(?:sub ?-? ?total(?:is)?|totais|totalizador(?:es)?|{_TOTAL} geral)\b"
    rf"|{_ANTES_DO_TOTAL}{_TOTAL}{_PREPOSICAO} (?:{_AGRUPAMENTOS})\b"
    rf"|{_ANTES_DO_TOTAL}{_TOTAL} (?:liquido|bruto|vendido|faturado|recebido|parcial|acumulado|final)\b"
    # "Total", "Total:", "Total R$ 1.234,56", "Total 1.234,56": só número depois. "Total 12 Clean
    # Mint" e "Total 100% Integral" são produtos.
    rf"|{_ANTES_DO_TOTAL}{_TOTAL}\s*(?::.*|(?:r\$)?\s*-?[\d.,]*\s*-?)$"
    rf"|{_ANTES_DO_TOTAL}{_TOTAL} em\s*\d{{1,2}}/\d{{1,2}}"
    r"|soma\s*(?::.*|-?[\d.,]*\s*)$"
    r"|movimenta(?:cao|coes)\b|movimento (?:d[oa]s?|de|no|na) (?:dia|mes|periodo|semana|ano|turno|caixa)\b"
    r"|(?:vendas?|faturamento|receita) (?:d[oa]s?|no|na|neste|nesta) (?:dia|mes|periodo|semana|ano|turno|caixa)\b"
    r"|saldo (?:anterior|final|inicial)\b|saldo (?:d[oa]|no|na) (?:dia|mes|periodo|caixa)\b"
    # "Transporte: 1.234,56" e "Transporte 1.234,56" são o acumulado da página; "Transporte",
    # "Transporte 2" e "Transporte 01" são o serviço vendido (verificação adversarial de 11/2026).
    r"|a transportar\b|transportado\b"
    r"|transporte\s*(?::\s*(?:r\$)?\s*-?[\d.,]*\d[\d.,]*|(?:r\$)?\s*-?\d+(?:\.\d{3})*,\d{2})\s*$"
    r"|resumo\s*(?::.*)?$"
    r"|periodo[.\s]*(?::|de\s+\d|\d{1,2}/)"
    r"|(?:usuario|operador|filtros?|software|sistema)[.\s]*:"
    r"|filtros? (?:aplicad|utilizad|selecionad|usad)"
    r"|(?:impresso|emitido|gerado)\s*(?::|em\s*:?\s*\d{1,2}[/.-]\d{1,2}|por\b|pelo\b|pela\b|as\b|dia\b)"
    r"|(?:data|hora|data e hora) (?:de |da )?(?:impressao|emissao)\b"
    # Página com o total ("Folha 1/3", "Pág. 2 de 5") ou com marcador ("Página: 4", "Página nº 1").
    # "Folha 40" e "FL 2" sozinhos podem ser produto e seguem a regra de sempre.
    r"|(?:pag(?:ina)?|folha|fl)[.\s]*(?:(?:no?[.\s]*|:\s*)\d+\s*(?:(?:de|/)\s*\d+\s*)?|\d+\s*(?:de|/)\s*\d+\s*)$"
    r"|(?:data|dia)\s*:?\s*\d{1,2}/\d{1,2}/\d{2,4}\b"
)
# Parte 2, em qualquer ponto do texto (re.search): marcas inequívocas de agregação e de impressão.
_RE_RODAPE_EXPLICITO_EM_QUALQUER_PONTO = re.compile(
    r"\b(?:total geral|sub ?-? ?total(?:is)?|totais|totalizador(?:es)?|labsofti)\b"
    r"|\bpagina\s*\d+\s*(?:de|/)\s*\d+\b"
    r"|\b(?:impresso|emitido|gerado)\s*(?:em|pelo|pela|dia|as)?\s*:?\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}"
    r"|\b(?:usuario|operador)[.\s]*:"
    r"|\bdesenvolvido por\b|\bwww\.|\bhttps?:"
)
# Linha que é só data, com hora, intervalo ou dia da semana ("01/03/2026 - Segunda-feira",
# "01/03/2026 a 31/03/2026"), conferida por partes, sem regex de retrocesso. "10/12/14 Conjunto
# Infantil" é produto.
_RE_DATA_COMPLETA = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
_RE_PARTE_DE_DATA = re.compile(
    r"\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}:\d{2}(?::\d{2})?|[-–]+|a|ate|as|hs?|"
    r"(?:segunda|terca|quarta|quinta|sexta)(?:-feira)?|sabado|domingo|feira"
)


def _linha_de_data(texto: str) -> bool:
    partes = [parte.rstrip(":.") for parte in re.split(r"[\s,;]+", texto) if parte.rstrip(":.")]
    return (bool(partes) and _RE_DATA_COMPLETA.fullmatch(partes[0]) is not None
            and all(_RE_PARTE_DE_DATA.fullmatch(parte) for parte in partes[1:]))


# Parte 1: as palavras do filtro antigo que nunca são nome de produto.
_RE_SEMPRE_RODAPE = re.compile(r"\b(?:total geral|totais|subtotal|movimenta|labsofti)\b")
_GATILHOS_DO_FILTRO_ANTIGO = frozenset({"software", "resumo", "periodo", "usuario", "filtro", "impresso", "pagina"})
# Só estas três têm resgate de produto ("Filtro de Café 103", "Software Antivírus", "Papel Impresso",
# os exemplos da diretriz). "resumo", "periodo", "pagina" e "usuario" sem acento começam rodapés e
# títulos de fechamento demais ("RESUMO DOS CAIXAS", "RESUMO TEF", "PERIODO ANALISADO", "NOME DO
# USUARIO", "QUEBRA DE PAGINA") para um vocabulário fechado separar: com elas, vale o filtro antigo.
_GATILHOS_COM_RESGATE = frozenset({"software", "filtro", "impresso"})
_PREPOSICOES = frozenset({"de", "da", "do", "das", "dos", "no", "na", "nos", "nas", "em", "a", "o", "e", "p", "c",
                          "com", "sem", "para", "por", "pelo", "pela", "pelos", "pelas", "ao", "aos", "ate", "entre",
                          "num", "numa", "neste", "nesta", "deste", "desta", "nesse", "nessa", "desse", "dessa",
                          "naquele", "naquela", "daquele", "daquela"})
# Vocabulário de rodapé (sem acento, minúsculo; o plural e o singular também valem): com uma palavra
# do filtro antigo e qualquer uma destas no mesmo texto, a linha continua rodapé, como sempre foi
# ("Filtro Loja 01", "FILTRO TODOS"). A lista é fechada de propósito: a exceção de produto só vale
# para o nome que não tem nada de rodapé.
_VOCABULARIO_DE_RODAPE = frozenset("""
    dia diario diaria data semana semanal quinzena mes mensal trimestre ano anual periodo pagina pag folha fl
    turno grupo subgrupo categoria departamento depto secao setor familia marca laboratorio fabricante
    vendedor vendedora vendedores vendedoras frentista bico bomba loja caixa pdv ecf operador filial
    empresa cliente clientes fornecedor fornecedores relatorio movimento movimentacao venda vendas vendido
    vendidos compra compras comprado entrada entradas saida saidas itens produtos documento cupom cupons
    nf nfe nota notas anterior descontos desconto acrescimos acrescimo janeiro fevereiro marco abril maio
    junho julho agosto setembro outubro novembro dezembro dinheiro cartao cartoes credito debito pix cheque
    cheques crediario convenio vale ticket forma formas pagamento pagamentos recebimento recebimentos prazo
    vista total tot subtotal soma saldo acumulado transporte transportar transportado valor vlr vl vr
    faturamento faturado receita media bruto bruta liquido liquida qtde qtd quantidade geral parcial final
    atual corrente seguinte referencia ref inicial fim proxima ultima continua continuacao emitido gerado
    impressao emissao terminal sistema versao ltda eireli cnpj hora horas fiscal contabil gerencial
    financeiro tributario impostos tributos operacoes cfop aplicado aplicados utilizado utilizados
    selecionado selecionados usados admin administrador supervisor gerente fiscalizacao todos todas nenhum
    nenhuma padrao ativo ativos criterio criterios faixa intervalo produto item codigo nome usuario resumo
    fechamento abertura sangria suprimento reducao tef sat analitico sintetico consolidado apuracao analisado
""".split())


def _no_vocabulario_de_rodape(palavra: str) -> bool:
    """A palavra, no singular ou no plural, está no vocabulário de rodapé ("caixas", "dias", "produto")."""
    return (palavra in _VOCABULARIO_DE_RODAPE or palavra + "s" in _VOCABULARIO_DE_RODAPE
            or (palavra.endswith("s") and palavra[:-1] in _VOCABULARIO_DE_RODAPE)
            or (palavra.endswith("es") and palavra[:-2] in _VOCABULARIO_DE_RODAPE))
_RE_FILTRO_RODAPE = re.compile(r"\bfiltros?\s*:|\bfiltros?\s+(?:aplicad|utilizad|selecionad|usad)")
_RE_SOFTWARE_RODAPE = re.compile(
    r"\bsoftware\s*:|\b(?:pelo|por|com|via|do|no)\s+software\b"
    r"|\b(?:versao|ltda|eireli|desenvolvid\w*|licenciad\w*)\b|\bv\s?\d+[.,]\d|\(\d{2}\)|\bwww\.|\bhttps?:"
)
_RE_IMPRESSO_RODAPE = re.compile(
    r"\bimpresso\s*(?::|por\b|pelo\b|pela\b|as\b|dia\b)|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\b\d{1,2}:\d{2}\b"
)


def _forma_de_produto(texto: str) -> bool:
    """O texto (sem acento, minúsculo) usa "filtro", "software" ou "impresso" como parte de um nome
    de produto, e não de rodapé: uma palavra só do filtro antigo, entre as que têm resgate, nenhuma
    palavra do vocabulário de rodapé ao lado e a palavra na forma de produto. Na dúvida, rodapé, como
    o filtro antigo."""
    if _RE_SEMPRE_RODAPE.search(texto):
        return False
    palavras = re.findall(r"[a-z]+", texto)
    ocorrencias = [palavra for palavra in palavras if palavra in _GATILHOS_DO_FILTRO_ANTIGO]
    gatilhos = set(ocorrencias)
    demais = [palavra for palavra in palavras if palavra not in gatilhos and palavra not in _PREPOSICOES]
    if not ocorrencias or not demais or len(ocorrencias) > 1 or not gatilhos <= _GATILHOS_COM_RESGATE:
        # A palavra sozinha (ou só com preposição), duas do filtro antigo, mesmo repetidas ("RESUMO
        # NESTE RESUMO"), ou uma das que não têm resgate ("RESUMO DOS CAIXAS"): rodapé.
        return False
    if any(_no_vocabulario_de_rodape(palavra) for palavra in demais):
        return False
    if "filtro" in gatilhos and _RE_FILTRO_RODAPE.search(texto):
        return False
    if "software" in gatilhos and _RE_SOFTWARE_RODAPE.search(texto):
        return False
    if "impresso" in gatilhos and _RE_IMPRESSO_RODAPE.search(texto):
        return False
    return True


# Decoração que o PDV põe em volta do rodapé ("*** TOTAL GERAL ***") e o apóstrofo da
# neutralização de fórmula ("'=== TOTAL ===").
_DECORACAO_DAS_PONTAS = " *=-_#'\".>:|[](){}~"

# Strings que, se aparecerem como valor da coluna-chave, indicam que aquela
# linha é um cabeçalho REPETIDO (quebra de página do PDV) — devem ser puladas.
_CABECALHOS_REPETIDOS = {
    "codigo", "produto", "nome do produto", "descricao",
    "data", "data emissao", "data nf", "fornecedor",
    "valor nf", "vr total", "total nf",
}

# Colunas numéricas de cada relatório. Linha com todas elas vazias e chave sem nenhum dígito não é
# item: é título de seção ("BEBIDAS") ou marca d'água. Produto sem venda ("Açúcar 1kg;-;-") fica.
_NUMERICAS_VENDAS = ("quantidade", "valor_venda", "preco_unitario", "valor_custo", "estoque")
_NUMERICAS_COMPRAS = ("qtde_comprada", "valor_nf", "produtos_total", "custo_unitario")


def _celula_vazia(valor) -> bool:
    return valor is None or (isinstance(valor, float) and math.isnan(valor)) or not str(valor).strip()


def _texto_de_rodape(valor) -> str:
    """Texto sem acento, minúsculo, com espaços colapsados e sem a decoração das pontas."""
    texto = _strip_acentos(str(valor).replace("\ufeff", "").replace("\xa0", " ")).lower()
    return re.sub(r"\s+", " ", texto).strip(_DECORACAO_DAS_PONTAS)


# Rodapé é linha curta: o texto comparado pelas regras novas tem teto, e nenhuma célula grande custa
# mais do que isso. O filtro antigo (linear) continua lendo o texto inteiro, como sempre.
_TETO_DO_TEXTO_DE_RODAPE = 400


def _e_rodape_explicito(valor) -> bool:
    """Parte 2: os rodapés inequívocos que a diretriz manda tirar ("Total do Dia", "Subtotal")."""
    texto = _texto_de_rodape(valor)[:_TETO_DO_TEXTO_DE_RODAPE]
    return bool(_RE_RODAPE_EXPLICITO.match(texto) or _RE_RODAPE_EXPLICITO_EM_QUALQUER_PONTO.search(texto)
                or _linha_de_data(texto))


def _e_rodape(valor) -> bool:
    """A célula-chave é rodapé, total, título de impressão ou marca d'água do PDV: o que o filtro de
    sempre tirava, salvo nome de produto (parte 1), e os rodapés inequívocos da diretriz (parte 2)."""
    if _e_rodape_explicito(valor):
        return True
    if not _PADRAO_LIXO_ANTIGO.search(str(valor)):
        return False
    return not _forma_de_produto(_texto_de_rodape(valor)[:_TETO_DO_TEXTO_DE_RODAPE])


def _construir_df_canonico(df_bruto: pd.DataFrame, idx_cab: int,
                           mapa: dict, linhas_cab: int) -> pd.DataFrame:
    """Recorta a partir da 1ª linha de dados e renomeia para nomes canônicos."""
    inicio = idx_cab + linhas_cab
    if inicio >= len(df_bruto):
        raise ETLValidationError("Cabeçalho localizado, mas sem linhas de dados após ele.")
    bloco = df_bruto.iloc[inicio:].reset_index(drop=True)
    out = pd.DataFrame(index=bloco.index)
    for nome_canonico, col_idx in mapa.items():
        if col_idx < bloco.shape[1]:
            out[nome_canonico] = bloco.iloc[:, col_idx].values
        else:
            out[nome_canonico] = pd.Series([None] * len(bloco))
    return out


def _filtrar_linhas_validas(df: pd.DataFrame, chave: str, numericas: tuple = ()) -> pd.DataFrame:
    """
    Descarta linhas onde a coluna-chave está vazia, é rodapé/total/marca d'água
    (_e_rodape), OU é um cabeçalho reimpresso (quebra de página típica do PDV), e os
    títulos de seção: chave sem dígito e todas as colunas `numericas` presentes vazias.
    """
    if chave not in df.columns:
        raise ETLValidationError(f"Coluna-chave '{chave}' ausente após o mapeamento.")
    bruto = df[chave].map(lambda v: "" if v is None else str(v).strip())
    valido = bruto.map(lambda v: v.lower() not in ("", "nan", "none"))
    # Uma classificação por texto distinto: num relatório por cupom o mesmo nome se repete.
    rodape = {texto: _e_rodape(texto) for texto in bruto.unique()}
    nao_rodape = ~bruto.map(rodape).astype(bool)
    nao_cabec = ~bruto.map(_norm_chave).isin(_CABECALHOS_REPETIDOS)
    filtro = valido & nao_rodape & nao_cabec
    colunas = [coluna for coluna in numericas if coluna in df.columns]
    if colunas:
        vazias = df[colunas].apply(lambda serie: serie.map(_celula_vazia)).all(axis=1)
        titulo = vazias & ~bruto.str.contains(r"[0-9]", regex=True)
        filtro &= ~titulo
    return df[filtro].reset_index(drop=True)


# =====================================================================
# 5a) Situação da linha: cancelamentos, devoluções e trocas
# =====================================================================
# Lidas à parte de ALIAS_VENDAS de propósito: essas colunas não disputam lugar com os
# canônicos nem mudam a escolha da linha de cabeçalho. A exclusão existe porque
# "Status do Pagamento: Cancelado" não significa que a venda foi cancelada.
_RE_COLUNA_SITUACAO = re.compile(
    r"\b(?:status|situacao|operacao|natureza|movimento|movimentacao)\b"
    r"|^tipo$|^tipo (?:de )?(?:venda|nota|documento|doc|registro|lancamento)\b"
)
_RE_COLUNA_DE_OUTRO_ASSUNTO = re.compile(
    r"\b(?:pagamento|entrega|cliente|produto|estoque|financeiro|financeira|titulo|cadastro)\b"
)
# Início de palavra, para "MERCANCIA" não contar como "CANC"; e sem negação, porque
# "NAO CANCELADA" é uma venda válida.
_RE_LINHA_CANCELADA = re.compile(r"(?<![A-Z0-9])(?<!NAO )(?<!SEM )(?:CANC|ESTORN|INUTILIZ)")
_RE_LINHA_DEVOLUCAO = re.compile(r"(?<![A-Z0-9])(?<!NAO )(?<!SEM )(?:DEVOL|TROCA)")


def _situacao_das_linhas(df_bruto: pd.DataFrame, idx_cab: int, mapa: dict, linhas_cab: int,
                         outro_assunto=None):
    """Texto normalizado das colunas de status/operação de cada linha; None se não houver.
    `outro_assunto`: títulos a mais que não são da linha (nas compras, o status do pedido)."""
    titulos = [[_norm_chave(v) for v in df_bruto.iloc[i].tolist()]
               for i in range(idx_cab, idx_cab + linhas_cab)]
    usadas = set(mapa.values())
    colunas = []
    for indice, partes in enumerate(zip(*titulos)):
        chave = " ".join(partes).strip()
        if (indice not in usadas and _RE_COLUNA_SITUACAO.search(chave)
                and not _RE_COLUNA_DE_OUTRO_ASSUNTO.search(chave)
                and not (outro_assunto is not None and outro_assunto.search(chave))):
            colunas.append(indice)
    if not colunas:
        return None
    bloco = df_bruto.iloc[idx_cab + linhas_cab:, colunas]
    return [" ".join(_norm_chave(v).upper() for v in linha if not pd.isna(v))
            for linha in bloco.itertuples(index=False, name=None)]


# =====================================================================
# Colunas de cadastro (NCM e categoria), só para a Avaliação de Cenário
# =====================================================================
# Lidas DEPOIS da escolha do cabeçalho, só nas colunas fora do mapa e que não são de situação, e só
# quando quem chama pede (_vendas_do_bruto(..., cadastro=True)): a análise mensal e o Raio-X do
# cliente não mudam (golden tests/test_cadastro_regression.py). Nada disto entra no ALIAS_VENDAS.
_RE_TITULO_NCM = re.compile(r"\bncm\b|\bclassificacao fiscal\b")
# Categoria do produto, na ordem de preferência: o título "categoria" e, depois, do nível mais amplo ao
# mais específico. Título de código, fiscal ou de outro assunto não é categoria ("Cód. Grupo", "Grupo
# Tributário", "Grupo de Clientes").
_NIVEIS_DE_CATEGORIA = ("categoria", "departamento", "depto", "secao", "grupo", "familia", "subgrupo")
_RE_TITULO_FORA_DE_CATEGORIA = re.compile(
    r"\b(?:cod|codigo|id|ncm|tribut\w*|fiscal|icms|ipi|pis|cofins|cst|cfop|preco|cliente|clientes|"
    r"fornecedor|fornecedores|pagamento|situacao|status)\b"
)
# Marcadores de categoria não configurada no ERP (comparados como _norm_chave). "Diversos" e "Outros"
# podem ser departamentos reais e ficam como categoria.
_CATEGORIAS_GENERICAS = frozenset({
    "sem grupo", "sem categoria", "sem departamento", "sem secao", "sem familia", "sem subgrupo",
    "nao definido", "nao definida", "nao informado", "nao informada", "nao classificado", "nao classificada",
    "a classificar", "indefinido", "indefinida", "padrao", "geral", "nenhum", "nenhuma", "null", "none", "nan",
    "n a", "na", "nd", "0", "00", "000", "999", "9999",
})
LIMITE_CATEGORIA = 80
# Palavra de cadastro no título da coluna lida como código ou como produto: o dicionário de vendas pegou
# a coluna errada ("Cód. NCM" antes de "Código", "Grupo do Produto" antes da descrição).
_RE_TITULO_DE_CADASTRO = re.compile(r"\b(?:ncm|grupo|categoria|departamento|depto|secao|familia|subgrupo)\b")
_RE_NUMERO_INTEIRO_DO_XLSX = re.compile(r"\d+\.0+")


def _colunas_de_cadastro(df_bruto: pd.DataFrame, idx_cab: int, mapa: dict, linhas_cab: int) -> dict:
    """{"ncm": índice, "categoria": índice} das colunas de cadastro fora do mapa (as ausentes ficam fora)."""
    titulos = [[_norm_chave(v) for v in df_bruto.iloc[i].tolist()]
               for i in range(idx_cab, idx_cab + linhas_cab)]
    usadas = set(mapa.values())
    ncm, niveis = None, {}
    for indice, partes in enumerate(zip(*titulos)):
        chave = " ".join(partes).strip()
        if not chave or indice in usadas or _RE_COLUNA_SITUACAO.search(chave):
            continue
        if _RE_TITULO_NCM.search(chave):
            if ncm is None:
                ncm = indice
            continue
        if _RE_TITULO_FORA_DE_CATEGORIA.search(chave):
            continue
        palavras = chave.split()
        for nivel in _NIVEIS_DE_CATEGORIA:
            if nivel in palavras and nivel not in niveis:
                niveis[nivel] = indice
                break
    colunas = {} if ncm is None else {"ncm": ncm}
    for nivel in _NIVEIS_DE_CATEGORIA:
        if nivel in niveis:
            colunas["categoria"] = niveis[nivel]
            break
    return colunas


def normalizar_ncm(valor):
    """NCM com 8 dígitos ou None: só os dígitos ("0402.10.10"), sem o ".0" da célula numérica do XLSX,
    com o zero à esquerda que o número perdeu (7 dígitos). Vazio, só zeros ou outro tamanho: None."""
    if _celula_vazia(valor):
        return None
    texto = str(valor).strip()
    if _RE_NUMERO_INTEIRO_DO_XLSX.fullmatch(texto):
        texto = texto.split(".", 1)[0]
    digitos = "".join(c for c in texto if c.isdigit())
    if len(digitos) == 7:
        digitos = "0" + digitos
    if len(digitos) != 8 or set(digitos) == {"0"}:
        return None
    return digitos


def normalizar_categoria(valor):
    """Categoria do produto como o relatório escreve (espaços colapsados, até 80 caracteres, sem
    caractere de controle) ou None, se vazia ou marcador de categoria não configurada."""
    if _celula_vazia(valor):
        return None
    # Espaço especial (NBSP, TAB, espaço fino) vira espaço antes de tirar os não imprimíveis: senão
    # "Frios e Laticinios" grudaria em "FrioseLaticinios" e a mesma categoria viraria várias.
    texto = "".join(" " if c.isspace() else c for c in str(valor) if c.isprintable() or c.isspace())
    texto = " ".join(texto.split())
    if _RE_NUMERO_INTEIRO_DO_XLSX.fullmatch(texto):
        texto = texto.split(".", 1)[0]
    chave = _norm_chave(texto)
    if not chave or chave in _CATEGORIAS_GENERICAS:
        return None
    return texto[:LIMITE_CATEGORIA].strip() or None


def _aplicar_situacao_das_vendas(df: pd.DataFrame, nome: str) -> pd.DataFrame:
    """
    Descarta as linhas canceladas e transforma devoluções e trocas em dedução: valor e
    quantidade ficam negativos, tenham vindo positivos ou não na planilha. Assim, somar
    o faturamento já dá o Líquido Real (vendas válidas - devoluções/trocas), sem subtrair
    duas vezes. "DEVOLUCAO CANCELADA" não aconteceu: conta como cancelada.
    """
    canceladas = df["_situacao"].map(lambda s: bool(_RE_LINHA_CANCELADA.search(s))).astype(bool)
    devolucoes = df["_situacao"].map(lambda s: bool(_RE_LINHA_DEVOLUCAO.search(s))).astype(bool)
    valor_cancelado = float(df.loc[canceladas, "valor_venda"].abs().sum())
    linhas_canceladas = int(canceladas.sum())
    devolucoes = devolucoes[~canceladas].reset_index(drop=True)
    df = df[~canceladas].drop(columns="_situacao").reset_index(drop=True)
    if df.empty:
        raise ETLValidationError(
            f"Relatório de VENDAS '{nome}' sem vendas válidas: todas as linhas estão canceladas."
        )
    df["_devolucao"] = devolucoes
    df.loc[devolucoes, "valor_venda"] = -df.loc[devolucoes, "valor_venda"].abs()
    df.loc[devolucoes, "quantidade"] = -df.loc[devolucoes, "quantidade"].abs()
    df.attrs["situacao"] = {
        "linhas_canceladas": linhas_canceladas,
        "valor_cancelado": valor_cancelado,
        "linhas_devolucao": int(devolucoes.sum()),
        "valor_devolucoes": float(-df.loc[devolucoes, "valor_venda"].sum()),
    }
    return df


# =====================================================================
# 6) Carregadores específicos (Vendas / Compras)
# =====================================================================
def _validar_vendas(mapa: dict) -> bool:
    # Precisa de produto + quantidade + um valor: total explícito (valor_venda)
    # OU preço unitário (o total da linha é derivado de qtd x unitário).
    if not ("produto" in mapa and "quantidade" in mapa):
        return False
    return ("valor_venda" in mapa) or ("preco_unitario" in mapa)


def _validar_compras(mapa: dict) -> bool:
    # Aceita relatório NF-level (chave = fornecedor) ou item-level (chave = produto).
    # Valor: total explícito (valor_nf/produtos_total) OU custo unitário + quantidade
    # (o total da linha é derivado de qtd_comprada x custo_unitario).
    tem_chave = ("fornecedor" in mapa) or ("produto" in mapa)
    tem_total = ("valor_nf" in mapa) or ("produtos_total" in mapa)
    tem_derivavel = ("custo_unitario" in mapa) and ("qtde_comprada" in mapa)
    return tem_chave and (tem_total or tem_derivavel)


def carregar_vendas(caminho: str) -> pd.DataFrame:
    return _vendas_do_bruto(_ler_bruto(caminho), os.path.basename(caminho))


def _vendas_do_bruto(bruto: pd.DataFrame, nome: str, *, cadastro: bool = False) -> pd.DataFrame:
    """carregar_vendas a partir da matriz já lida: o Raio-X lê cada arquivo uma vez só e
    tira dela também o período do cabeçalho (_extrair_periodo_base).

    `cadastro=True` (só a Avaliação de Cenário pede) traz também, em colunas com "_", o NCM e a
    categoria do relatório, quando ele tem essas colunas, e se o estoque de cada linha foi informado;
    attrs["cadastro"] diz quais colunas existem e se o código ou o produto foram lidos de uma coluna
    de cadastro (cabeçalho ambíguo). Sem o parâmetro, nada muda."""
    if bruto.empty:
        raise ETLValidationError(f"O arquivo de vendas '{nome}' está totalmente vazio.")

    idx, mapa, linhas_cab = _detectar_cabecalho(bruto, ALIAS_VENDAS, _validar_vendas)
    # Título normalizado de cada coluna reconhecida. Os KPIs não o usam; o Raio-X precisa
    # saber se o custo é unitário e se o total da linha veio do preço unitário.
    cabecalhos = {canonico: " ".join(_norm_chave(bruto.iat[idx + k, coluna]) for k in range(linhas_cab)).strip()
                  for canonico, coluna in mapa.items()}
    df = _construir_df_canonico(bruto, idx, mapa, linhas_cab)
    situacao = _situacao_das_linhas(bruto, idx, mapa, linhas_cab)
    if situacao is not None:
        df["_situacao"] = situacao
    info_cadastro = None
    if cadastro:
        colunas = _colunas_de_cadastro(bruto, idx, mapa, linhas_cab)
        bloco = bruto.iloc[idx + linhas_cab:]
        for chave, coluna in colunas.items():
            df["_" + chave] = bloco.iloc[:, coluna].tolist()
        info_cadastro = {
            "ncm": "ncm" in colunas, "categoria": "categoria" in colunas,
            "ambiguo": any(_RE_TITULO_DE_CADASTRO.search(cabecalhos.get(c, "")) for c in ("codigo", "produto")),
        }
    df = _filtrar_linhas_validas(df, "produto", _NUMERICAS_VENDAS)

    # Conversões numéricas — NaN vira 0 (linha sem qtd/valor não impacta KPI)
    qualidade = Counter()
    csv = bruto.attrs.get("format") == "csv"  # só o texto do CSV prova o formato en-US
    df["quantidade"] = _converter_serie(df["quantidade"]).fillna(0.0)
    if "valor_venda" in df.columns:
        df["_valor_informado"] = _valor_informado(df["valor_venda"])
        df["valor_venda"] = _converter_serie_monetaria(df["valor_venda"], qualidade, csv)
    elif "preco_unitario" in df.columns:
        # Sem coluna de total: deriva o faturamento da linha (qtd x preço unitário).
        # Cálculo honesto a partir de colunas reais — não é dado inventado.
        df["_valor_informado"] = _valor_informado(df["preco_unitario"])
        df["valor_venda"] = _converter_serie_monetaria(df["preco_unitario"], qualidade, csv) * df["quantidade"]
        logger.info("VENDAS '%s': faturamento derivado de quantidade x preço unitário.", nome)
    else:
        raise ETLValidationError(
            f"Relatório de VENDAS '{nome}' sem coluna de valor (nem total, nem preço unitário)."
        )
    if "valor_custo" in df.columns:
        df["valor_custo"] = _converter_serie_monetaria(df["valor_custo"], qualidade, csv)
    if "estoque" in df.columns:
        estoque = _converter_serie(df["estoque"])
        if cadastro:
            df["_estoque_informado"] = estoque.notna()
        df["estoque"] = estoque.fillna(0.0)

    # 'lucro' é DETECTADO e DESCARTADO — terminologia NEXO proíbe usar.
    if "lucro" in df.columns:
        df = df.drop(columns=["lucro"])

    df["_prod_norm"] = df["produto"].map(_normalizar_produto)
    # Remove vazios e linhas que não são produto (descontos, frete, troco...),
    # que distorceriam o ranking de faturamento e a Curva ABC.
    df = df[(df["_prod_norm"] != "") & (~df["_prod_norm"].isin(_NAO_PRODUTOS))].reset_index(drop=True)
    if df.empty:
        raise ETLValidationError(
            f"Relatório de VENDAS '{nome}' sem nomes de produto válidos após a limpeza."
        )
    if "_situacao" in df.columns:
        df = _aplicar_situacao_das_vendas(df, nome)
    df.attrs["qualidade"] = _fechar_qualidade(qualidade)
    df.attrs["cabecalhos"] = cabecalhos
    df.attrs["celulas_recodificadas"] = int(bruto.attrs.get("celulas_recodificadas", 0))
    if info_cadastro is not None:
        df.attrs["cadastro"] = info_cadastro
    return df


# Bonificação: mercadoria recebida sem pagamento. Mesma forma das marcas de cancelamento e
# devolução: início de palavra e sem negação logo antes ("SEM BONIFICACAO" não é bonificação).
_RE_LINHA_BONIFICACAO = re.compile(r"(?<![A-Z0-9])(?<!NAO )(?<!SEM )(?<!COM )(?<!C )BONIFIC")
# A operação é uma compra ou, na natureza escrita pelo fornecedor, uma venda ("Compra c/ bonificação",
# "Compra bonificada", "Venda e bonificação"): a NF foi paga.
_RE_LINHA_COMPRA = re.compile(r"(?<![A-Z0-9])(?:COMPRA|VENDA)")
# Nas compras, a marca só vale como fato da própria nota. Com negação em qualquer ponto antes dela
# ("NAO HOUVE DEVOLUCAO AO FORNECEDOR", "NOTA NAO FOI CANCELADA"), desfeita nas duas palavras seguintes
# ("CANCELAMENTO REJEITADO", "DEVOLUCAO PENDENTE") ou, no cancelamento, com outro sujeito logo antes
# ("PAGAMENTO ESTORNADO", "SALDO CANCELADO" do pedido), vale a regra de sempre.
_RE_NEGACAO = re.compile(r"(?<![A-Z0-9])(?:NAO|SEM|NENHUMA?|NUNCA)(?![A-Z0-9])")
_RE_MARCA_DESFEITA = re.compile(r"REJEITAD|NEGAD|RECUSAD|INDEFERID|PENDENTE|SOLICITAD|AGUARDAND|ANALISE|"
                                r"ANDAMENTO|PARCIAL|NAO$")
_RE_OUTRO_SUJEITO_DO_CANCELAMENTO = re.compile(
    r"PAGAMENTO|PAGTO|PGTO|TITULOS?|BOLETOS?|PARCELAS?|DUPLICATAS?|COBRANCA|SALDO|PEDIDOS?|ITEM|ITENS|FRETE|"
    r"ENTREGA|RESERVA|ORCAMENTO|COTACAO")


def _marca_da_nota(padrao, texto: str, *, sujeito: bool = False) -> bool:
    """A marca (cancelamento, devolução ou bonificação) está no texto como fato da própria nota."""
    encontrado = padrao.search(texto)
    if encontrado is None or _RE_NEGACAO.search(texto, 0, encontrado.start()):
        return False
    antes = texto[:encontrado.start()].split()
    if sujeito and antes and _RE_OUTRO_SUJEITO_DO_CANCELAMENTO.fullmatch(antes[-1]):
        return False
    depois = texto[encontrado.end():]
    palavras = depois.split()
    if depois[:1] and not depois[:1].isspace():
        palavras = palavras[1:]  # o resto da palavra marcada ("ELAMENTO" de "CANCELAMENTO")
    return not any(_RE_MARCA_DESFEITA.match(palavra) for palavra in palavras[:2])
# Devolução AO FORNECEDOR, nas compras: só o documento de devolução ("Devolução de compra",
# "Devolução ao fornecedor") ou o CFOP de devolução de compra escrito como CFOP ("CFOP 5.202"). Não
# contam "devolução de venda" (o cliente devolveu à loja), o status da NF original ("Devolvida",
# "Devolvida ao fornecedor"), "troca" nem um número solto de operação ("5202"), e a linha segue a
# regra de sempre.
_RE_DEVOLUCAO_DE_COMPRA = re.compile(
    r"(?<![A-Z0-9])(?<!NAO )(?<!SEM )DEVOLUC(?:AO|OES)\s+(?:DE\s+COMPRAS?|(?:AO|AOS|A|AS|PARA(?:\s+O|\s+OS)?)\s+FORNEC)"
    r"|\bCFOP\s*[56]\s?(?:201|202|410|411)\b"
)
# Coluna de situação que não é da nota: o status do pedido ("Atendido - saldo cancelado") não diz
# que a NF foi cancelada.
_RE_COLUNA_DE_OUTRO_ASSUNTO_NAS_COMPRAS = re.compile(r"\b(?:pedido|pedidos|orcamento|cotacao)\b")


def _consolidar_compras(df: pd.DataFrame, coluna_valor: str):
    """
    Compras líquidas. Retorna (df, resumo da consolidação). Só as marcas explícitas da coluna de
    situação mudam alguma coisa; o resto segue a regra de sempre (entra a linha com valor positivo):
      - nota fiscal cancelada, estornada ou inutilizada sai do cálculo;
      - devolução ao fornecedor (_RE_DEVOLUCAO_DE_COMPRA) desconta o valor e a quantidade;
      - bonificação (situação BONIFIC, numa linha que não diz compra nem venda) entra só na
        quantidade: a mercadoria chegou sem pagamento, então não soma no total comprado.
    Marca com negação antes dela, em qualquer ponto do texto ("NAO HOUVE DEVOLUCAO"), não conta.
    Sem coluna de situação, o resultado é o de antes.
    """
    situacao = df["_situacao"] if "_situacao" in df.columns else pd.Series("", index=df.index)

    def marcadas(padrao):
        return situacao.map(lambda texto: bool(padrao.search(texto))).astype(bool)

    def marcas_da_nota(padrao, sujeito=False):
        return situacao.map(lambda texto: _marca_da_nota(padrao, texto, sujeito=sujeito)).astype(bool)

    valor = df[coluna_valor]
    qtde = df["qtde_comprada"] if "qtde_comprada" in df.columns else pd.Series(0.0, index=df.index)
    canceladas = marcas_da_nota(_RE_LINHA_CANCELADA, sujeito=True)
    devolucao = ~canceladas & marcas_da_nota(_RE_DEVOLUCAO_DE_COMPRA) & ((valor != 0) | (qtde != 0))
    bonificacao = ~canceladas & ~devolucao & marcas_da_nota(_RE_LINHA_BONIFICACAO) & ~marcadas(_RE_LINHA_COMPRA)
    compra = ~canceladas & ~devolucao & ~bonificacao & (valor > 0)
    resumo = {
        "linhas_canceladas": int(canceladas.sum()),
        "valor_cancelado": float(valor[canceladas].abs().sum()),
        "linhas_devolucao": int(devolucao.sum()),
        "valor_devolucoes": float(valor[devolucao].abs().sum()),
        "linhas_bonificacao": int(bonificacao.sum()),
        "quantidade_bonificada": float(qtde[bonificacao].abs().sum()),
    }
    df = df.copy()
    df.loc[devolucao, coluna_valor] = -valor[devolucao].abs()
    df.loc[bonificacao, coluna_valor] = 0.0
    if "qtde_comprada" in df.columns:
        df.loc[devolucao, "qtde_comprada"] = -qtde[devolucao].abs()
        df.loc[bonificacao, "qtde_comprada"] = qtde[bonificacao].abs()
    df = df[compra | devolucao | bonificacao].drop(columns="_situacao", errors="ignore").reset_index(drop=True)
    return df, resumo


def carregar_compras(caminho: str):
    """
    Retorna (df_compras, coluna_valor_usada).
    `coluna_valor_usada` ∈ {"produtos_total", "valor_nf"} — produtos_total
    tem preferência (valor "puro" dos itens; valor_nf inclui frete/imposto).
    O total já sai líquido de NF cancelada e de devolução ao fornecedor (_consolidar_compras).
    """
    nome = os.path.basename(caminho)
    bruto = _ler_bruto(caminho)
    if bruto.empty:
        raise ETLValidationError(f"O arquivo de compras '{nome}' está totalmente vazio.")

    idx, mapa, linhas_cab = _detectar_cabecalho(bruto, ALIAS_COMPRAS, _validar_compras)
    df = _construir_df_canonico(bruto, idx, mapa, linhas_cab)
    situacao = _situacao_das_linhas(bruto, idx, mapa, linhas_cab,
                                    outro_assunto=_RE_COLUNA_DE_OUTRO_ASSUNTO_NAS_COMPRAS)
    if situacao is not None:
        df["_situacao"] = situacao
    # Chave de validade de linha: fornecedor (NF-level) ou, na ausência dele,
    # produto (item-level) — alinhado ao validador.
    chave_linha = "fornecedor" if "fornecedor" in df.columns else "produto"
    df = _filtrar_linhas_validas(df, chave_linha, _NUMERICAS_COMPRAS)
    if chave_linha == "fornecedor" and "produto" in df.columns:
        # Relatório por item com fornecedor e produto: o total inequívoco pode vir na coluna do
        # produto ("Distribuidora A;SUBTOTAL;15;170,00") e somaria a nota duas vezes. Só os totais da
        # diretriz: essa coluna nunca passou pelo filtro antigo, e "Transporte", "Filtro" e
        # "Impresso em Papel Kraft" são itens comprados.
        rodape_no_produto = df["produto"].map(lambda v: not _celula_vazia(v) and _e_rodape_explicito(v))
        df = df[~rodape_no_produto.astype(bool)].reset_index(drop=True)

    qualidade = Counter()
    csv = bruto.attrs.get("format") == "csv"  # só o texto do CSV prova o formato en-US
    if "qtde_comprada" in df.columns:
        df["qtde_comprada"] = _converter_serie(df["qtde_comprada"]).fillna(0.0)
    if "produtos_total" in df.columns:
        coluna_valor = "produtos_total"
        df[coluna_valor] = _converter_serie_monetaria(df[coluna_valor], qualidade, csv)
    elif "valor_nf" in df.columns:
        coluna_valor = "valor_nf"
        df[coluna_valor] = _converter_serie_monetaria(df[coluna_valor], qualidade, csv)
    elif "custo_unitario" in df.columns and "qtde_comprada" in df.columns:
        # Sem coluna de total: deriva o total da linha (qtd x custo unitário).
        # Cálculo honesto a partir de colunas reais — não é dado inventado.
        custo = _converter_serie_monetaria(df["custo_unitario"], qualidade, csv)
        coluna_valor = "valor_total_compra"
        df[coluna_valor] = df["qtde_comprada"] * custo
        logger.info("COMPRAS '%s': total derivado de quantidade x custo unitário.", nome)
    else:
        raise ETLValidationError(
            f"Relatório de COMPRAS '{nome}' sem coluna de valor reconhecida "
            "(total de NF/compra/custo, nem custo unitário + quantidade)."
        )
    df, consolidacao = _consolidar_compras(df, coluna_valor)

    if "produto" in df.columns:
        df["_prod_norm"] = df["produto"].map(_normalizar_produto)
    df.attrs["qualidade"] = _fechar_qualidade(qualidade)
    df.attrs["celulas_recodificadas"] = int(bruto.attrs.get("celulas_recodificadas", 0))
    if consolidacao["linhas_canceladas"] or consolidacao["linhas_devolucao"] or consolidacao["linhas_bonificacao"]:
        df.attrs["consolidacao"] = consolidacao
    return df, coluna_valor


# =====================================================================
# 6a) Detecção do PERÍODO DA BASE (auditoria de origem dos dados)
# =====================================================================
# Relatórios de PDV trazem o período no cabeçalho textual (ex.:
# "Período de : 01/01/26 á 31/03/26"). Extraímos isso via pandas para exibir
# no dashboard COM QUE BASE/PERÍODO os dados foram gerados — sem digitar à mão.
_RE_DATA_BR = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")


def _parse_data_br(grupos) -> "datetime.date | None":
    d, m, y = (int(x) for x in grupos)
    if y < 100:
        y += 2000  # ano de 2 dígitos: 26 -> 2026
    try:
        return datetime(y, m, d).date()
    except ValueError:
        return None


def _extrair_periodo_base(df_bruto: pd.DataFrame, janela: int = 30):
    """
    Varre as primeiras linhas procurando um cabeçalho de 'período' e extrai as
    duas datas (início, fim). Retorna (date, date) ou None se não encontrar.
    """
    limite = min(janela, len(df_bruto))
    for i in range(limite):
        for cell in df_bruto.iloc[i].tolist():
            if cell is None:
                continue
            txt = _strip_acentos(str(cell)).lower()
            if "periodo" not in txt:
                continue
            datas = [d for d in (_parse_data_br(g) for g in _RE_DATA_BR.findall(str(cell))) if d]
            if len(datas) >= 2:
                ini, fim = datas[0], datas[1]
                return (ini, fim) if ini <= fim else (fim, ini)
            if len(datas) == 1:
                return datas[0], datas[0]
    return None


def _detectar_periodo_arquivo(caminho: str):
    """Lê o cru do arquivo e tenta extrair o período do cabeçalho. Falha → None."""
    try:
        return _extrair_periodo_base(_ler_bruto(caminho))
    except ETLValidationError:
        return None


# =====================================================================
# 6b) Curva ABC (Princípio de Pareto) sobre o faturamento por produto
# =====================================================================
def _classificar_curva_abc(grupo: pd.DataFrame) -> dict:
    """
    Recebe o DataFrame já agregado por produto (colunas: _prod_norm, qtde, fat,
    nome) e devolve um dict com:
      - "produtos": TODOS os produtos, da maior para a menor receita líquida, com
        posicao, nome, faturamento, quantidade, perc_individual, perc_acumulado e classe.
      - "resumo": agregados por classe (qtd de produtos, faturamento e % do total).

    A classe segue o acumulado: A (<=80%), B (<=95%), C (acima). O produto líder é sempre
    Classe A, para um único produto dominante não cair em B ou C ao ultrapassar o limiar
    sozinho; os demais seguem as faixas.

    O total é a receita líquida de todos os itens, igual ao Faturamento Total. Por isso,
    zerados e negativos (devoluções acima das vendas) entram no fim da lista, e o
    acumulado pode passar de 100% antes de fechar.
    """
    base = grupo.sort_values(["fat", "_prod_norm"], ascending=[False, True], kind="stable").reset_index(drop=True)
    acumulado = base["fat"].cumsum()
    total = float(acumulado.iloc[-1]) if not base.empty else 0.0
    # Menos de meio centavo é ruído de ponto flutuante, não receita.
    if round(total, 2) <= 0:
        return {"produtos": [], "resumo": []}

    # Em centésimos de ponto percentual, inteiros: cada individual é a diferença entre
    # acumulados arredondados, então a soma fecha em exatamente 100,00. Arredondar os
    # individuais um a um deixaria 99,99 ou 100,01; repartir essa sobra entre eles
    # deslocaria o acumulado, e com ele as classes, em catálogos com muitos itens pequenos.
    acum_centesimos = (acumulado / total * 10000).round().astype("int64")
    ind_centesimos = acum_centesimos.diff().fillna(acum_centesimos.iloc[0]).astype("int64")
    perc_acum = acum_centesimos / 100
    classes = np.select([perc_acum <= ABC_LIMITE_A, perc_acum <= ABC_LIMITE_B], ["A", "B"], default="C")
    classes[0] = "A"

    tem_qtde = "qtde" in base.columns
    quantidades = base["qtde"].tolist() if tem_qtde else [None] * len(base)
    produtos = [
        {
            "posicao": posicao,
            "nome": str(nome),
            "faturamento": float(faturamento),
            "quantidade": float(quantidade) if tem_qtde else None,
            "perc_individual": individual / 100,
            "perc_acumulado": acum / 100,
            "classe": str(classe),
        }
        for posicao, (nome, faturamento, quantidade, individual, acum, classe) in enumerate(
            zip(base["nome"].tolist(), base["fat"].tolist(), quantidades,
                ind_centesimos.tolist(), acum_centesimos.tolist(), classes.tolist()),
            start=1,
        )
    ]

    resumo = []
    for c in ("A", "B", "C"):
        da_classe = classes == c
        resumo.append({
            "classe": c,
            "qtd_produtos": int(da_classe.sum()),
            "faturamento": round(float(base.loc[da_classe, "fat"].sum()), 2),
            "perc_faturamento": int(ind_centesimos[da_classe].sum()) / 100,
        })
    return {"produtos": produtos, "resumo": resumo}


def _alertas_de_valores(qualidade: Counter, situacao, produtos_abc: list) -> list:
    """Observações para o administrador conferir antes de publicar a devolutiva."""
    alertas = []
    if qualidade["invalidos"]:
        alertas.append(
            f"Qualidade dos dados: {qualidade['invalidos']} valor(es) em dinheiro não puderam ser "
            "interpretados e entraram como R$ 0,00. Revise o relatório antes de publicar."
        )
    if qualidade["lidos_como_milhar"]:
        alertas.append(
            f"Confira os valores: {qualidade['lidos_como_milhar']} valor(es) com ponto e 3 casas, como "
            "12.500, foram lidos como milhar (doze mil e quinhentos). Se o relatório usa ponto como "
            "decimal, esses valores ficaram mil vezes maiores."
        )
    if qualidade["lidos_como_decimal"]:
        alertas.append(
            f"Confira os valores: {qualidade['lidos_como_decimal']} valor(es) com ponto e 3 casas, como "
            "12.500, foram lidos como decimal (doze e meio), porque a mesma coluna usa ponto decimal "
            "em outras linhas."
        )
    if qualidade["virgula_como_decimal"]:
        alertas.append(
            f"Confira os valores: {qualidade['virgula_como_decimal']} valor(es) com vírgula e 3 casas, como "
            "12,500, foram lidos como decimal (doze e meio). Se o relatório usa vírgula como separador de "
            "milhar, esses valores ficaram mil vezes menores."
        )
    if qualidade["virgula_como_milhar"]:
        alertas.append(
            f"Confira os valores: {qualidade['virgula_como_milhar']} valor(es) com vírgula e 3 casas, como "
            "12,500, foram lidos como milhar (doze mil e quinhentos), porque a mesma coluna usa ponto decimal "
            "em outras linhas."
        )
    if situacao and (situacao["linhas_canceladas"] or situacao["linhas_devolucao"]):
        alertas.append(
            f"Faturamento líquido: {situacao['linhas_canceladas']} linha(s) canceladas ou estornadas "
            f"ficaram fora do cálculo e {situacao['linhas_devolucao']} devolução(ões) ou troca(s) foram deduzidas."
        )
    negativos = sum(1 for p in produtos_abc if p["faturamento"] < 0)
    if negativos:
        alertas.append(
            f"Curva ABC: {negativos} produto(s) têm receita líquida negativa e aparecem no fim da lista "
            "com percentual negativo; por isso, o acumulado pode passar de 100% antes de fechar em 100%."
        )
    if not produtos_abc:
        alertas.append("Curva ABC não calculada: a receita líquida do período não é positiva.")
    return alertas


def _alerta_de_compras(consolidacao: dict) -> str:
    """Resumo da consolidação das compras (_consolidar_compras), só com o que aconteceu."""
    partes = []
    if consolidacao["linhas_canceladas"]:
        partes.append(f"{consolidacao['linhas_canceladas']} nota(s) cancelada(s), estornada(s) ou inutilizada(s) "
                      "ficaram fora do total comprado")
    if consolidacao["linhas_devolucao"]:
        partes.append(f"{consolidacao['linhas_devolucao']} devolução(ões) ao fornecedor descontaram "
                      f"R$ {_reais(consolidacao['valor_devolucoes'])}")
    if consolidacao["linhas_bonificacao"]:
        partes.append(f"{consolidacao['linhas_bonificacao']} bonificação(ões) entraram só na quantidade comprada, "
                      "sem valor")
    return "Compras líquidas: " + "; ".join(partes) + "."


def _reais(valor: float) -> str:
    inteiro, _, centavos = f"{abs(valor):,.2f}".partition(".")
    return ("-" if valor < 0 else "") + inteiro.replace(",", ".") + "," + centavos


# =====================================================================
# 7) Cálculo dos KPIs em memória (sem tocar no banco)
# =====================================================================
def calcular_kpis_em_memoria(caminho_vendas: str, caminho_compras: str) -> ResultadoETL:
    """Calcula sem banco em processo próprio; limites não dependem de threads."""
    from etl_runtime import run_calculation
    try:
        return run_calculation(caminho_vendas, caminho_compras, get_limits())
    except ETLInputError as erro:
        raise ETLValidationError(erro.safe_message, erro.code) from None


def _calcular_kpis_local(caminho_vendas: str, caminho_compras: str) -> ResultadoETL:
    nome_v = os.path.basename(caminho_vendas)

    df_v = carregar_vendas(caminho_vendas)
    df_c, col_valor_c = carregar_compras(caminho_compras)

    # ---- VENDAS: agregação por produto normalizado ----
    grupo = (
        df_v.groupby("_prod_norm")
        .agg(qtde=("quantidade", "sum"),
             fat=("valor_venda", "sum"),
             nome=("produto", "first"))
        .reset_index()
    )
    if not grupo.empty:
        grupo["nome"] = grupo["nome"].map(_nome_exibivel)
    if grupo.empty:
        raise ETLValidationError(f"Sem produtos para agregar em '{nome_v}'.")
    faturamento_total = float(grupo["fat"].sum())

    i_qtd = grupo["qtde"].idxmax()
    prod_norm_mais_vendido = grupo.loc[i_qtd, "_prod_norm"]
    produto_mais_vendido_nome = str(grupo.loc[i_qtd, "nome"])
    produto_mais_vendido_quantidade = float(grupo.loc[i_qtd, "qtde"])

    # Maior faturamento: em varejos pequenos qtde e faturamento são fortemente
    # correlacionados, então o topo dos dois rankings costuma coincidir. Para
    # evitar a redundância visual ("mesmo produto em dois cards") sem violar a
    # regra NEXO (lucro/CMV proibidos como KPI), se o produto de maior
    # faturamento coincidir com o mais vendido mostramos o 2º colocado do
    # ranking de faturamento (exigindo fat > 0 para não cair em zero falso).
    # Se sobrar só 1 candidato (relatório com produto único, ou todos os outros
    # com fat=0), aceitamos a coincidência sem mascarar.
    ranking_fat = grupo.sort_values("fat", ascending=False)
    candidatos_fat = ranking_fat[
        (ranking_fat["_prod_norm"] != prod_norm_mais_vendido) & (ranking_fat["fat"] > 0)
    ]
    linha_fat = candidatos_fat.iloc[0] if not candidatos_fat.empty else ranking_fat.iloc[0]
    produto_maior_faturamento_nome = str(linha_fat["nome"])
    produto_maior_faturamento_valor = float(linha_fat["fat"])

    # ---- Curva ABC (Princípio de Pareto) sobre o faturamento por produto ----
    curva_abc = _classificar_curva_abc(grupo)

    # ---- Período da base (cabeçalho do relatório): vendas tem prioridade ----
    periodo_base = _detectar_periodo_arquivo(caminho_vendas) or _detectar_periodo_arquivo(caminho_compras)
    periodo_base_inicio = periodo_base[0] if periodo_base else None
    periodo_base_fim = periodo_base[1] if periodo_base else None

    # ---- COMPRAS: total (nível NF) ----
    total_comprado = float(df_c[col_valor_c].sum())

    # ---- Indicador de Pressão de Estoque ----
    saldo_estimado_compras_vendas = total_comprado - faturamento_total

    alertas: list = []

    # ---- Produto com maior saldo estimado parado (só se COMPRAS tem produto+qtde) ----
    produto_maior_saldo_parado_nome = None
    saldo_estimado_parado = None
    tem_prod_c = "produto" in df_c.columns and "_prod_norm" in df_c.columns
    tem_qtd_c = "qtde_comprada" in df_c.columns
    if tem_prod_c and tem_qtd_c:
        compras_prod = (
            df_c[df_c["_prod_norm"] != ""]
            .groupby("_prod_norm")
            .agg(qtde_comprada=("qtde_comprada", "sum"),
                 nome=("produto", "first"))
            .reset_index()
        )
        if not compras_prod.empty:
            compras_prod["nome"] = compras_prod["nome"].map(_nome_exibivel)
        vendas_prod = grupo[["_prod_norm", "qtde"]].rename(columns={"qtde": "qtde_vendida"})
        cruz = compras_prod.merge(vendas_prod, on="_prod_norm", how="left")
        cruz["qtde_vendida"] = cruz["qtde_vendida"].fillna(0.0)
        cruz["saldo"] = cruz["qtde_comprada"] - cruz["qtde_vendida"]
        positivos = cruz[cruz["saldo"] > 0]
        if not positivos.empty:
            i_saldo = positivos["saldo"].idxmax()
            produto_maior_saldo_parado_nome = str(positivos.loc[i_saldo, "nome"])
            saldo_estimado_parado = float(positivos.loc[i_saldo, "saldo"])
    else:
        alertas.append(
            "Limitação técnica: o relatório de COMPRAS está em nível de nota "
            "fiscal (fornecedor/valor) e não traz produto + quantidade comprada. "
            "O KPI 'produto com maior saldo estimado parado' não pôde ser "
            "calculado e foi persistido como nulo (sem gerar dado enganoso)."
        )

    # ---- Telemetria (em memória, NÃO persistida — sem campo no DER V4) ----
    colunas_v = [c for c in df_v.columns if not c.startswith("_")]
    colunas_c = [c for c in df_c.columns if not c.startswith("_")]
    telemetria = {
        "coluna_valor_compras": col_valor_c,
        "colunas_vendas": colunas_v,
        "colunas_compras": colunas_c,
        "linhas_vendas": int(len(df_v)),
        "linhas_compras": int(len(df_c)),
    }

    # Alerta: saldo de estoque NEGATIVO informado pelo PDV (não é KPI, é sinal)
    if "estoque" in df_v.columns:
        n_neg = int((df_v["estoque"] < 0).sum())
        taxa = n_neg / len(df_v) if len(df_v) else 0.0
        telemetria["produtos_estoque_pdv_negativo"] = n_neg
        telemetria["taxa_estoque_pdv_negativo"] = taxa
        if taxa > 0.10:
            alertas.append(
                f"Atenção: {n_neg} produto(s) ({taxa * 100:.1f}%) apresentam saldo "
                f"de estoque NEGATIVO informado pelo PDV — possível inconsistência "
                f"de registro no sistema do cliente."
            )

    # Alerta: venda abaixo do custo informado pelo PDV (observação, NÃO margem)
    if "valor_custo" in df_v.columns:
        abaixo = (df_v["valor_custo"] > 0) & (df_v["valor_venda"] < df_v["valor_custo"])
        if "_devolucao" in df_v.columns:
            abaixo &= ~df_v["_devolucao"]
        n_abaixo = int(abaixo.sum())
        telemetria["itens_venda_abaixo_custo_pdv"] = n_abaixo
        if n_abaixo > 0:
            alertas.append(
                f"Alerta de custo informado pelo PDV: {n_abaixo} item(ns) tiveram "
                f"valor total de venda inferior ao custo total informado pelo PDV no "
                f"período. É um sinal extraído da coluna bruta de custo do PDV, NÃO "
                f"um cálculo contábil de margem."
            )

    # Faturamento Líquido Real = vendas válidas - devoluções/trocas. Só há o que somar
    # aqui quando o relatório traz status ou operação; as devoluções já vêm negativas.
    situacao = df_v.attrs.get("situacao")
    if situacao:
        telemetria.update(situacao)
        telemetria["vendas_validas"] = faturamento_total + situacao["valor_devolucoes"]
    qualidade = Counter(df_v.attrs.get("qualidade")) + Counter(df_c.attrs.get("qualidade"))
    if qualidade:
        telemetria["valores_monetarios"] = dict(qualidade)
    alertas.extend(_alertas_de_valores(qualidade, situacao, curva_abc["produtos"]))

    # Só aparecem quando aconteceram: relatório sem esses casos sai igual ao de antes.
    consolidacao = df_c.attrs.get("consolidacao")
    if consolidacao:
        telemetria["compras_consolidacao"] = consolidacao
        alertas.append(_alerta_de_compras(consolidacao))
    recodificadas = {nome: quantidade for nome, quantidade in (
        ("vendas", df_v.attrs.get("celulas_recodificadas", 0)),
        ("compras", df_c.attrs.get("celulas_recodificadas", 0))) if quantidade}
    if recodificadas:
        telemetria["celulas_recodificadas"] = recodificadas
        alertas.append(
            f"Codificação do texto: {sum(recodificadas.values())} célula(s) misturavam acentos de UTF-8 e de "
            "Windows-1252 (como \"CafÃ©\") e foram corrigidas antes do cálculo, para o mesmo produto não "
            "virar dois. Confira os nomes no relatório."
        )

    kpis = {
        "faturamento_total": faturamento_total,
        "total_comprado": total_comprado,
        "saldo_estimado_compras_vendas": saldo_estimado_compras_vendas,
        "produto_mais_vendido_nome": produto_mais_vendido_nome,
        "produto_mais_vendido_quantidade": produto_mais_vendido_quantidade,
        "produto_maior_faturamento_nome": produto_maior_faturamento_nome,
        "produto_maior_faturamento_valor": produto_maior_faturamento_valor,
        "produto_maior_saldo_parado_nome": produto_maior_saldo_parado_nome,
        "saldo_estimado_parado": saldo_estimado_parado,
        # Curva ABC: NÃO é um único KPI escalar; é o ranking + resumo por classe.
        # Persistido em produto_curva_abc (1:N), não em indicador_analise.
        "curva_abc": curva_abc,
        # Período real coberto pela base (lido do cabeçalho do relatório).
        "periodo_base_inicio": periodo_base_inicio,
        "periodo_base_fim": periodo_base_fim,
    }
    return ResultadoETL(
        sucesso=True,
        mensagem="Cálculo concluído em memória.",
        kpis=kpis,
        telemetria=telemetria,
        alertas=alertas,
    )


# =====================================================================
# 7a) Trava do total do PDV (regra pura; o banco fica em validacao_pdv.py)
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
# 8) Persistência (somente KPIs finais) + orquestrador oficial
# =====================================================================
def _persistir_indicadores(db_session, id_analise: int, kpis: dict) -> None:
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
