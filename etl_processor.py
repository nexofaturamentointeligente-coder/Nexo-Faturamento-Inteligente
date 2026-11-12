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


class ETLLeiauteAmbiguo(ETLValidationError):
    """O relatório foi reconhecido, mas o leiaute não deixa saber qual coluna é a da venda. Na análise
    mensal é uma recusa de dados como as outras (código "data"); o Raio-X e a Avaliação de Cenário mostram
    `orientacao`, um texto fixo, depois do número do arquivo."""

    def __init__(self, mensagem, orientacao):
        super().__init__(mensagem)
        self.orientacao = orientacao


# Texto fixo do relatório de compras ou de entradas enviado como vendas (_recusar_relatorio_de_compras): o
# Raio-X e a Avaliação de Cenário mostram "Arquivo N: " antes dele.
ORIENTACAO_RELATORIO_DE_COMPRAS = ("o relatório enviado é de compras/entradas. Por favor, envie o relatório de "
                                   "vendas por produto.")


class ETLRelatorioDeCompras(ETLLeiauteAmbiguo):
    """Relatório de compras ou de entradas de mercadoria enviado no lugar do relatório de vendas."""

    def __init__(self, nome):
        super().__init__(f"O arquivo de VENDAS '{nome}' é um relatório de COMPRAS ou de entradas de mercadoria. "
                         "Envie o relatório de vendas por produto.", ORIENTACAO_RELATORIO_DE_COMPRAS)


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
    if s.lower() in ("", "nan", "none", "-", "--") or s in _TRACOS_SOZINHOS:
        return None
    # Percentual não é quantidade nem estoque: "-99,97 %" é a margem que o ERP imprime ao lado do custo.
    if "%" in s:
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
# O travessão ("—"), o traço de algarismo ("‒") e a barra horizontal ("―") sozinhos são a célula vazia
# (o zero do relatório formatado), como o "-": sem o alerta de valor inválido. Antes do número, não são sinal: o
# número fica como sempre foi lido.
_TRACOS_SOZINHOS = ("—", "‒", "―")
_VALORES_EM_BRANCO = {"", "-", "--", "nan", "none", "null", "na", "n/a", "nd", "n/d", *_TRACOS_SOZINHOS}


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


# Título que casa um fragmento do dicionário e é outra coisa:
#   - coluna de data, a que tem numa das linhas do cabeçalho um título que começa por "Data" ou "Dt." ("Data
#     Faturamento", "Dt. Últ. Custo"), não é quantidade, valor, custo nem estoque;
#   - coluna de percentual, a do título com um "%" que não vem depois de um número ("% Valor Total",
#     "Faturamento (%)", "Marg. %"), não fica com campo nenhum. O "%" depois de um número é parâmetro do
#     título ("Valor Total (-5% desc.)", "Preço c/ ICMS 18%"), e a coluna continua sendo a do valor;
#   - nas vendas, título de compra ou de custo ("Preço Compra", "Valor Unitário Compra", um segundo título
#     de custo) não é o preço nem o total da venda. Sem isto, o preço de compra à esquerda do de venda
#     ficava com o preço unitário, e o faturamento saía de quantidade x preço de compra, sem aviso. A
#     coluna de compra não é lida como custo: fica sem campo.
_RE_TITULO_DE_DATA = re.compile(r"(?:data|dt)\b")
_RE_TITULO_DE_COMPRA_OU_CUSTO = re.compile(r"\b(?:compras?|comprad[oa]s?|custos?|cmv)\b")
_RE_PERCENTUAL_DE_PARAMETRO = re.compile(r"\d\s*%")
_CAMPOS_DO_VALOR_DA_VENDA = frozenset({"valor_venda", "preco_unitario"})
_CAMPOS_NUMERICOS = frozenset({"quantidade", "valor_venda", "preco_unitario", "valor_custo", "estoque",
                               "qtde_comprada", "valor_nf", "produtos_total", "custo_unitario"})


def _titulo_de_percentual(celula) -> bool:
    """A célula de título, como veio do arquivo, é de coluna de percentual."""
    return "%" in _RE_PERCENTUAL_DE_PARAMETRO.sub("", str(celula))


_RE_DATA_OU_HORA = re.compile(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}:\d{2}")
_MAXIMO_DO_VALOR_DO_ROTULO = 24


def _rotulo_com_valor(linha: list, coluna: int) -> bool:
    """A célula é um rótulo do topo do relatório com o valor dele, e não título de coluna: traz uma data ou
    uma hora nela mesma ("Data: 05/02/2026"), ou a próxima célula preenchida da linha é uma data ou uma
    hora ("Data de emissão" | "05/02/2026"; "Data Inicial" | "01/01/2026"). É a emissão ou o período do
    relatório. Um dígito ou dois-pontos qualquer não basta: "Data Faturamento:" e "Dt. Últ. Custo 2025"
    são títulos de coluna."""
    if _RE_DATA_OU_HORA.search(str(linha[coluna])):
        return True
    for proxima in linha[coluna + 1:]:
        if not _celula_vazia(proxima):
            texto = str(proxima).strip()
            return len(texto) <= _MAXIMO_DO_VALOR_DO_ROTULO and _RE_DATA_OU_HORA.match(texto) is not None
    return False


def _titulo_de_outra_coisa(canonico: str, chave: str, de_data: bool = False) -> bool:
    """O título (normalizado) não pode ficar com o campo, mesmo casando um fragmento dele. `de_data`: a
    coluna tem título de data numa das linhas do cabeçalho."""
    if de_data and canonico in _CAMPOS_NUMERICOS:
        return True
    return canonico in _CAMPOS_DO_VALOR_DA_VENDA and _RE_TITULO_DE_COMPRA_OU_CUSTO.search(chave) is not None


def _mapear_cabecalho(linha_norm: list, aliases: dict, reserva: dict = None,
                      percentuais: frozenset = frozenset(), de_data: frozenset = frozenset()) -> dict:
    """
    Para cada COLUNA da linha (já normalizada), encontra o 1º canônico cujo
    fragmento aparece como substring. Cada canônico é claimed no máximo 1x
    (1ª coluna vence — proteção contra duplicatas no relatório). A coluna de data (`de_data`)
    não fica com campo numérico, e o título de compra ou de custo não fica com os campos de valor
    da venda (_titulo_de_outra_coisa).

    Depois, os títulos de reserva (RESERVA_VENDAS/RESERVA_COMPRAS) completam só os
    canônicos que ficaram sem coluna, e só com coluna que o dicionário não reconhece.
    `percentuais`: colunas de percentual ("% Total", "Total (%)", "% CMV"; ver
    _titulo_de_percentual), cujo título a normalização transforma em "total" ou "cmv": não
    ficam com campo nenhum, nem pelo dicionário
    (o "% Valor Total" à esquerda do "Valor Total" dava a soma dos percentuais como faturamento)
    nem pela reserva. Título de reserva repetido
    na mesma linha ("Quant." sobre "Pedida" e "Quant." sobre "Vendida") é ambíguo e também não vale:
    o cabeçalho em duas linhas, se houver, desfaz a dúvida.
    """
    mapa: dict = {}
    for idx, chave in enumerate(linha_norm):
        if not chave or idx in percentuais:
            continue
        for canonico, fragmentos in aliases.items():
            if canonico in mapa or _titulo_de_outra_coisa(canonico, chave, idx in de_data):
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


# Uma frase inteira que contém "produto" e "valor total" é aviso ou rodapé do relatório ("Atenção .: Total
# das vendas não esta considerando "produto genérico"."), não cabeçalho: sem esta conferência, um aviso e
# a linha de baixo passavam por cabeçalho de compras. A frase é comprida e fica SOZINHA na linha; o título
# comprido de uma coluna ("Valor Total de Vendas no Período de ...") vem ao lado dos outros títulos. No
# cabeçalho em duas linhas, a frase sozinha vale quando a outra linha do par (em cima ou embaixo dela) tem
# três células ou mais: é o título do relatório sobre a linha de títulos ("Relatório de vendas por produto,
# período de ... a ..." sobre "Item;Qtde;Valor Total"), que já era lido assim. A conferência só conta as
# células: um aviso com a palavra "produto" colado a uma linha de três células com títulos de quantidade e
# de valor continua passando por cabeçalho, como antes (Docs/UNIFICACAO_MOTOR_ETL.md, seção 10).
_MAXIMO_DE_PALAVRAS_DO_TITULO = 8


def _titulos_plausiveis(linhas: list, indice: int, linhas_cab: int, mapa: dict) -> bool:
    """Nenhuma célula que deu o mapeamento é uma frase: mais de 8 palavras, sozinha na linha dela, sem uma
    linha de títulos (três células ou mais) na outra linha do cabeçalho."""
    preenchidas = [sum(1 for celula in linhas[indice + deslocamento] if not _celula_vazia(celula))
                   for deslocamento in range(linhas_cab)]
    for deslocamento in range(linhas_cab):
        linha = linhas[indice + deslocamento]
        if preenchidas[deslocamento] != 1 or any(
                preenchidas[outra] >= 3 for outra in range(linhas_cab) if outra != deslocamento):
            continue
        for coluna in mapa.values():
            celula = linha[coluna] if coluna < len(linha) else None
            if not _celula_vazia(celula) and len(str(celula).split()) > _MAXIMO_DE_PALAVRAS_DO_TITULO:
                return False
    return True


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
    com_percentual = [frozenset(j for j, v in enumerate(linha) if _titulo_de_percentual(v)) for linha in linhas]
    percentuais = {1: com_percentual, 2: [com_percentual[i] | com_percentual[i + 1] for i in range(limite - 1)]}
    # O título de data é conferido em cada linha: no cabeçalho de duas linhas, o texto junto pode começar
    # pela linha de cima ("Período: ..." sobre "Data Faturamento"). Na linha de cima, o rótulo com o valor
    # dele ("Data: 05/02/2026", ou "Data de emissão" com a data na célula ao lado) não é título de coluna.
    com_data = [frozenset(j for j, titulo in enumerate(linha) if _RE_TITULO_DE_DATA.match(titulo)) for linha in simples]
    data_em_cima = [frozenset(j for j in colunas if not _rotulo_com_valor(linhas[i], j)) for i, colunas in enumerate(com_data)]
    de_data = {1: com_data, 2: [data_em_cima[i] | com_data[i + 1] for i in range(limite - 1)]}
    reserva = _reserva_de(aliases)
    for com_reserva in ((False, True) if reserva else (False,)):
        # 1) cabeçalho de 1 linha; 2) cabeçalho composto (2 linhas)
        for linhas_cab, candidatas in ((1, simples), (2, compostas)):
            melhor = None
            for i, titulos in enumerate(candidatas):
                mapa = _mapear_cabecalho(titulos, aliases, reserva if com_reserva else {},
                                         percentuais[linhas_cab][i], de_data[linhas_cab][i])
                if len(mapa) > len(melhor_parcial):
                    melhor_parcial = mapa
                if (valida_fn(mapa) and (melhor is None or len(mapa) > len(melhor[1]))
                        and _titulos_plausiveis(linhas, i, linhas_cab, mapa)):
                    melhor = (i, mapa)
            if melhor is not None:
                i = melhor[0]
                return i, _mapear_cabecalho(candidatas[i], aliases, reserva, percentuais[linhas_cab][i],
                                            de_data[linhas_cab][i]), linhas_cab

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
_RE_TITULO_DE_CADASTRO = re.compile(
    r"\b(?:ncm|grupo|categoria|departamento|depto|secao|familia|subgrupo|loja|filial|estabelecimento|empresa|cnpj)\b")
# Coluna de loja ou filial (só no modo cadastro, para o lead): "Unidade" sozinha fica de fora, porque no
# PDV ela é unidade de medida (UN, KG, CX); caixa, PDV e terminal são divisões da mesma loja.
_RE_TITULO_UNIDADE = re.compile(r"\b(?:loja|filial|estabelecimento|unidade de negocio|unid negocio)\b")
# Título de número da loja ("Estoque Loja", "Preço Filial", "Qtd. Loja"): é medida, não a loja; a coluna
# segue as regras de antes (NCM, categoria ou nada).
_RE_TITULO_MEDIDA = re.compile(
    r"\b(?:preco|precos|valor|valores|vlr|vl|custo|custos|qtd|qtde|quant\w*|estoque|estoques|saldo|saldos|"
    r"total|totais|margem|venda|vendas|faturamento|desconto|descontos|perc\w*|markup|lucro|media|giro|"
    r"cobertura|minimo|maximo|pedido|pedidos|compra|compras|comissao|meta|data|dt|dia|mes|ano)\b")
# Título de produto: "Produto Loja" ou "Descrição do Produto (Filial)" ainda é o produto.
_RE_TITULO_DE_PRODUTO = re.compile(r"\b(?:produto|produtos|mercadoria|mercadorias|item|itens|sku)\b")
_RE_NUMERO_INTEIRO_DO_XLSX = re.compile(r"\d+\.0+")
# Data de cada venda num relatório item a item ("Data", "Data venda", "Dt. Emissão", "Data/Hora"): com ela, um
# arquivo de vários meses vira fatias mensais no Raio-X e na Avaliação de Cenário (raio_x._fatias_mensais).
_RE_TITULO_DATA_DA_VENDA = re.compile(
    r"(?:data|dt)(?: (?:da |de |do )?(?:venda|vendas|emissao|movimento|pedido|documento|doc|nf|nota|cupom|"
    r"lancamento|faturamento))?(?:(?: e)? hora)?|emissao")
# Dados de cada venda para os indicadores executivos (indicadores_executivos.resumo_das_linhas), todos pelo
# título inteiro, como a data:
#   - o número da venda (pedido, cupom, nota ou documento): "Nr. Doc.", "Pedido", "Cupom", "Nº NF-e", "Nota
#     Fiscal", "Documento", "COO". "Venda" e "Transação" só com o marcador de número ("Nº Venda", "ID
#     Transação"): "Venda" sozinho costuma ser o valor;
#   - o CPF ou CNPJ do cliente, por lista de permissão: o título é só o documento ("CPF", "CNPJ", "CPF/CNPJ",
#     "Nº do CPF", "CPF na Nota Fiscal", "CPF ou CNPJ", "Doc. CPF/CNPJ", "Identificação (CPF/CNPJ)", "CPF/CNPJ/ID
#     Estrangeiro": tiradas as palavras que só marcam ou completam o documento, sobra só "CPF" ou "CNPJ"; "Fiscal"
#     só completa "Nota", "Cupom" e "Documento") ou diz de quem é (cliente, consumidor, comprador, destinatário,
#     tomador, "Cli.", "Dest."). O CPF do fiscal de caixa ("CPF Fiscal", "CPF do Fiscal"), o
#     do operador, do caixa, do vendedor ou do representante e o CNPJ da loja, da matriz ou da unidade de
#     negócio não são o cliente (o da unidade de negócio continua sendo a loja). Com o título do cliente e o do
#     documento sozinho no mesmo relatório, vale o do cliente. Só é contado dentro do processo isolado
#     (recompra), nunca guardado;
#   - o desconto da linha, em reais ("Desconto", "Vlr. Desconto", "Desconto R$"); o de percentual ("Desc. %")
#     fica de fora, e "Desc." sozinho não conta (é também a abreviatura de descrição);
#   - a hora da venda, quando vem em coluna própria ("Hora", "Hora da Venda").
_MARCADOR_DE_NUMERO = r"(?:nr|n|no|num|nro|numero|id)(?: (?:do|da|de))?"
_RE_TITULO_DOCUMENTO_DA_VENDA = re.compile(
    rf"(?:{_MARCADOR_DE_NUMERO} )?(?:doc|docto|documento|cupom|cupom fiscal|nota|nota fiscal|nf|nfe|nf e|nfce|"
    rf"nfc e|nfc|pedido|coo)|{_MARCADOR_DE_NUMERO} (?:venda|transacao)")
_RE_TITULO_CPF_CNPJ = re.compile(r"\b(?:cpf|cnpj)\b")
# As palavras que só marcam ou completam o documento do cliente no título: tiradas elas, o título que sobra só
# com "cpf" ou "cnpj" é o do cliente. "Operador", "caixa", "vendedor", "loja", "matriz", "unidade" e semelhantes não
# estão aqui: o título com elas não é o do cliente.
_PALAVRAS_DO_DOCUMENTO_DO_CLIENTE = frozenset({
    "nr", "n", "no", "num", "nro", "numero", "do", "da", "de", "na", "ou", "e", "doc", "documento", "identificacao",
    "informado", "nota", "nf", "nfe", "nfc", "nfce", "cupom", "venda", "id", "estrangeiro"})
# "Fiscal" só completa a nota, o cupom ou o documento ("CPF na Nota Fiscal"); sozinho, é o fiscal de caixa.
_RE_DOCUMENTO_FISCAL = re.compile(r"\b(nota|cupom|documento|doc) fiscal\b")
_RE_TITULO_DE_QUEM_COMPROU = re.compile(
    r"\b(?:cliente|clientes|cli|consumidor|consumidores|comprador|destinatario|dest|tomador)\b")
_RE_TITULO_DESCONTO = re.compile(
    r"(?:(?:vlr|vl|vr|valor|total|tot)(?: (?:do|de|da))? )?(?:desconto|descontos|desc)(?: (?:r|rs|concedido))?")
_RE_TITULO_HORA_DA_VENDA = re.compile(
    r"(?:hora|hr|horario)(?: (?:da |de |do )?(?:venda|emissao|movimento|pedido|cupom|nf|nota|documento|lancamento))?")


def _dado_da_venda(texto: str, celulas: list):
    """"documento", "cliente", "desconto", "hora" ou None: o dado de cada venda que o título (inteiro,
    normalizado) indica. `celulas`: o título como veio do arquivo, para recusar a coluna de percentual."""
    if _RE_TITULO_HORA_DA_VENDA.fullmatch(texto):
        return "hora"
    if _RE_TITULO_DOCUMENTO_DA_VENDA.fullmatch(texto):
        return "documento"
    if _RE_TITULO_CPF_CNPJ.search(texto):
        if _RE_TITULO_DE_QUEM_COMPROU.search(texto):
            return "cliente"
        resto = [palavra for palavra in _RE_DOCUMENTO_FISCAL.sub(r"\1", texto).split()
                 if palavra not in _PALAVRAS_DO_DOCUMENTO_DO_CLIENTE]
        if all(palavra in ("cpf", "cnpj") for palavra in resto):
            return "cliente_sem_titulo"
        return None
    if (_RE_TITULO_DESCONTO.fullmatch(texto) and texto != "desc"
            and not any(_titulo_de_percentual(celula) for celula in celulas)):
        return "desconto"
    return None


def _colunas_de_cadastro(df_bruto: pd.DataFrame, idx_cab: int, mapa: dict, linhas_cab: int) -> dict:
    """{"ncm": índice, "categoria": índice} das colunas de cadastro fora do mapa (as ausentes ficam fora)."""
    titulos = [[_norm_chave(v) for v in df_bruto.iloc[i].tolist()]
               for i in range(idx_cab, idx_cab + linhas_cab)]
    usadas = set(mapa.values())
    ncm, niveis, unidade, data = None, {}, None, None
    da_venda: dict = {}
    for indice, partes in enumerate(zip(*titulos)):
        chave = " ".join(partes).strip()
        if not chave or indice in usadas:
            continue
        # A célula vazia da outra linha do cabeçalho chega como "nan" (XLSX): não faz parte do título. A data
        # vem antes da situação: "Data Movimento" é a data da venda, embora "movimento" também seja título de
        # coluna de situação.
        texto = " ".join(parte for parte in partes if parte and parte != "nan")
        if _RE_TITULO_DATA_DA_VENDA.fullmatch(texto):
            if data is None:
                data = indice
            continue
        if _RE_COLUNA_SITUACAO.search(chave):
            continue
        dado = _dado_da_venda(texto, [df_bruto.iat[i, indice] for i in range(idx_cab, idx_cab + linhas_cab)])
        if dado is not None:
            da_venda.setdefault(dado, indice)
            continue
        if (_RE_TITULO_UNIDADE.search(chave) and not _RE_TITULO_NCM.search(chave)
                and not _RE_TITULO_MEDIDA.search(chave)
                and not any(nivel in chave.split() for nivel in _NIVEIS_DE_CATEGORIA)):
            if unidade is None:
                unidade = indice
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
    if data is not None:
        colunas["data"] = data
    if unidade is not None:
        colunas["unidade"] = unidade
    for nivel in _NIVEIS_DE_CATEGORIA:
        if nivel in niveis:
            colunas["categoria"] = niveis[nivel]
            break
    # O CPF/CNPJ só com o título do documento vale quando não há o do cliente com o nome.
    sem_titulo = da_venda.pop("cliente_sem_titulo", None)
    if "cliente" not in da_venda and sem_titulo is not None:
        da_venda["cliente"] = sem_titulo
    colunas.update(da_venda)
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


def _loja_do_cupom(df: pd.DataFrame) -> pd.Series:
    """A loja de cada linha para o número da venda (indicadores_executivos.resumo_das_linhas), no quadro inteiro e
    antes de a linha cancelada sair: a célula da loja em branco logo abaixo do mesmo número (a loja só no primeiro
    item do cupom) é a do cupom, também quando esse primeiro item é cancelado, devolvido, bonificado ou tem
    quantidade 0. O trecho de cada cupom são as linhas de venda com o mesmo número em sequência; a linha fora da
    venda entra nele só com o mesmo número da próxima venda (é item daquele cupom), e uma linha de outro número no
    meio (a devolução de outro cupom) não parte o cupom. O estoque continua com a coluna da loja."""
    from indicadores_executivos import _loja, numero_da_venda  # o módulo importa este: aqui, na hora da chamada
    numeros = df["_documento"].map(numero_da_venda)
    lojas = df["_unidade"].map(_loja)
    fora = pd.Series(False, index=df.index)
    if "_situacao" in df.columns:
        fora = df["_situacao"].map(lambda s: bool(_RE_LINHA_CANCELADA.search(s) or _RE_LINHA_DEVOLUCAO.search(s)))
    venda = ~fora.astype(bool) & (df["quantidade"] > 0) & (df["valor_venda"] > 0)
    proxima_venda = numeros.where(venda).bfill()
    entra = numeros.notna() & (venda | numeros.eq(proxima_venda))
    trecho = (~numeros[entra].eq(numeros[entra].shift())).cumsum()
    herdada = lojas[entra].mask(lojas[entra] == "").groupby(trecho).ffill().fillna("")
    return herdada.reindex(df.index).fillna(lojas).astype(str)


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


# O .csv lido pela vírgula em que a linha de títulos tem ponto e vírgula, tabulação ou barra vertical dentro
# de uma célula: o separador do arquivo é esse outro, e a vírgula é a decimal (o relatório com ";" em que os
# títulos têm tantas vírgulas quanto as linhas de dado, e nenhum registro que o ";" organize melhor). Lido
# pela vírgula, o cabeçalho partido ainda pode dar produto, quantidade e valor, e os números sairiam de
# pedaços das células, sem aviso. Num CSV separado por vírgula de verdade, o outro separador fica nos dados,
# e não nos títulos. A leitura recusa, com a orientação de salvar como .xlsx.
_OUTROS_SEPARADORES = (";", "\t", "|")


def _recusar_separador_duvidoso(bruto: pd.DataFrame, idx: int, linhas_cab: int, nome: str) -> None:
    if bruto.attrs.get("format") != "csv" or bruto.attrs.get("separator") != ",":
        return
    titulos = [str(celula) for celula in bruto.iloc[idx + linhas_cab - 1].tolist() if not _celula_vazia(celula)]
    if any(separador in titulo for titulo in titulos for separador in _OUTROS_SEPARADORES):
        raise ETLLeiauteAmbiguo(
            f"O arquivo '{nome}' foi lido como separado por vírgula, mas a linha de títulos tem ponto e vírgula, "
            "tabulação ou barra vertical dentro de uma célula: não dá para decidir o separador do arquivo. Abra "
            "o arquivo no Excel, salve como .xlsx e envie novamente.",
            "a linha de títulos tem ponto e vírgula, tabulação ou barra vertical dentro de uma célula, e não dá "
            "para decidir o separador do arquivo .csv. Abra o arquivo no Excel, salve como .xlsx e envie "
            "novamente.")


# Dois títulos iguais para um valor em dinheiro ("Valor Unitário" de compra e "Valor Unitário" vendido,
# que só a segunda linha do cabeçalho distingue): a primeira coluna ficaria com o campo e o faturamento
# sairia do valor errado, sem aviso (o relatório de vendas abaixo do custo do ERP). A leitura recusa.
_CAMPOS_SEM_TITULO_REPETIDO = ("preco_unitario", "valor_venda", "valor_custo")


def _titulos_do_cabecalho(bruto: pd.DataFrame, idx: int, linhas_cab: int) -> list:
    """O título normalizado de cada coluna, com as linhas do cabeçalho juntas."""
    return [" ".join(_norm_chave(bruto.iat[idx + k, coluna]) for k in range(linhas_cab)).strip()
            for coluna in range(bruto.shape[1])]


def _recusar_titulos_repetidos(bruto: pd.DataFrame, idx: int, linhas_cab: int, mapa: dict, nome: str) -> None:
    titulos = _titulos_do_cabecalho(bruto, idx, linhas_cab)
    # A coluna de percentual ("% Valor Total", "Faturamento (%)", da curva ABC) perde o "%" na normalização
    # e ficaria com o título da coluna do valor: não conta como repetição (e nunca fica com o campo).
    percentuais = {coluna for coluna in range(bruto.shape[1])
                   if any(_titulo_de_percentual(bruto.iat[idx + k, coluna]) for k in range(linhas_cab)
                          if not _celula_vazia(bruto.iat[idx + k, coluna]))}
    repetidos = Counter(titulo for coluna, titulo in enumerate(titulos) if titulo and coluna not in percentuais)
    for campo in _CAMPOS_SEM_TITULO_REPETIDO:
        if campo in mapa and repetidos[titulos[mapa[campo]]] > 1:
            raise ETLLeiauteAmbiguo(
                f"Relatório de VENDAS '{nome}' com dois títulos de coluna iguais para um valor (por exemplo, "
                "dois \"Valor Unitário\"): não dá para saber qual é o da venda. Envie o relatório de vendas "
                "por produto.",
                "o relatório tem dois títulos de coluna iguais para um valor (por exemplo, dois \"Valor "
                "Unitário\", o de compra e o vendido): não dá para saber qual é o da venda. Envie o relatório "
                "de vendas por produto ou a venda item a item.")


# Relatório de VENDAS enviado no campo de COMPRAS. Sem a recusa, o "total comprado" saía do custo ou da
# venda do relatório, sem aviso. A recusa só vale com sinal inequívoco, nesta ordem:
#   1. o relatório que se diz de compras ou de entradas, em qualquer célula do topo, é de compras;
#   2. o título do relatório é de vendas ("Relatório de Vendas...", "Giro de vendas por fornecedor",
#      "Vendas por produto"): recusa, com qualquer coluna (vendas por fornecedor tem a coluna Fornecedor);
#   3. sem título que decida, o cabeçalho tem coluna que só existe em vendas ("Qtde Vend", "Total Venda")
#      e nenhuma de compras, de entrada, de fornecedor ou de nota: recusa.
# Uma palavra solta no topo que só comece por "Venda" (a cidade "Venda Nova do Imigrante") não é título de
# relatório, e "Dt. Faturamento" não é coluna só de vendas.
_RE_TITULO_DE_RELATORIO_DE_VENDAS = re.compile(
    r"^(?:(?:relatorio|giro|resumo|analise|listagem|ranking|mapa|curva)(?: geral)? (?:d[eao]s? )?vendas?\b"
    r"|vendas? por\b)")
_RE_TITULO_DE_COMPRAS = re.compile(r"\b(?:compras?|comprad\w*|entradas?)\b")
_RE_COLUNA_SO_DE_VENDAS = re.compile(
    r"\b(?:qtde?|qtd|quantidade) vend\w*|\btotal vendas?\b|\bvalor total venda\b")
_RE_COLUNA_DE_COMPRAS = re.compile(r"\b(?:compras?|comprad\w*|entradas?|fornecedor(?:es)?|nfe?|notas?)\b")


# O espelho, na rota de VENDAS (análise mensal, Raio-X e Avaliação de Cenário): relatório de compras ou de
# entradas de mercadoria enviado como vendas. Sem a recusa, o "Valor Total" da nota de entrada virava
# faturamento, sem aviso. A recusa vale também na análise mensal: só com sinal inequívoco, e nunca quando o
# topo traz título de relatório de vendas ("Relatório de Vendas por Cliente", "Giro de Vendas", "Vendas x
# Compras", "Produtos Mais Vendidos", "Vendas de Produtos", "Vendas Diárias", "Faturamento por Produto",
# "Pedidos de Venda", "Relação de Vendas") ou da compra do cliente ("Compras por Cliente"; "Histórico de
# Compras" com o rótulo "Cliente:" no topo), que é a venda da loja. Fora isso, qualquer um:
#   1. o TÍTULO do relatório, no topo (as linhas acima do cabeçalho), é de compras ou de entradas: a célula
#      começa por "Relatório de Compras", "Entradas de Mercadorias", "Compras por Fornecedor", "Nota Fiscal de
#      Entrada", "Curva ABC de Compras", "Recebimento de Mercadorias", "Compras do Mês", "Entradas no Período",
#      "Pedidos de Compra", "Ordens de Compra", "Mercadorias Recebidas", "Compras Realizadas", "Compras/
#      Entradas", "Entrada de NF-e", "Consulta de Notas de Entrada", "Demonstrativo de Compras", "Posição das
#      Compras de Produtos" (o resumo por nota do ERP da Opção 27).
#      Não é título:
#        - a célula que fala do cliente ou do consumidor ("Compras por Cliente", "Histórico de Compras do
#          Cliente"): a compra do cliente é a venda da loja;
#        - o valor de um filtro: a célula logo depois de um rótulo com dois-pontos que não é o do título
#          ("Grupo:" | "ENTRADAS", "Usuário:" | "COMPRAS", "Setor:" | "Compras por Fornecedor"), ou o filtro
#          numa célula só ("Comprador: TODOS"). Depois do rótulo do título ("Relatório:", "Rel.:", "Título:",
#          "Tipo de Relatório:", "Nome do Relatório:"), a célula É o título ("Relatório:" | "Compras por
#          Fornecedor"), como na célula só ("Relatório: Compras por Fornecedor");
#        - a palavra "Compras" ou "Entradas" sozinha fora da primeira célula do arquivo e do lugar do título (um
#          grupo de produtos, uma seção);
#        - palavra solta no nome da loja ("Supermercado Boa Compra", "Compra Por Menos Ltda", "Compra Na Hora")
#          ou no endereço ("Av. Entrada da Cidade"): "Compras por" e "Compras no/na" só com o complemento de
#          compras ("por Fornecedor", "por Produto", "no Período"); "Ranking", "Curva ABC" e "Giro" só de
#          "Compras" (no restaurante, "Entradas" é grupo de pratos). "(Produtos|Itens) Comprados" fica de fora:
#          no comércio eletrônico, costuma ser o do cliente.
#      A célula do título que fala também de venda ou saída ("Movimento de Entradas e Saídas") não recusa;
#      "Venda Nova" no endereço, em outra célula, não salva o "Relatório de Compras";
#   2. o único total do relatório é o da compra, dito com todas as letras ("Valor Total da Compra", "Total
#      Compra", "Compra Total"), e nenhuma coluna ficou com o total da venda. "Vlr. Compra" e "Valor de Compra"
#      ao lado do preço de venda podem ser o custo da unidade: a venda com custo não é recusada;
#   3. a exportação de compras sem título: a coluna lida como quantidade da venda é a quantidade comprada
#      ("Qtde Comprada", "Qtd. Compra"), há uma coluna própria da compra (fornecedor, custo, data ou nota de
#      entrada) e nenhuma coluna que só existe em vendas ("Qtde Vendida", "Quant. Vendida", "Total Venda",
#      "Valor Total de Venda"). Com outra coluna de quantidade lida ("Qtde" ao lado de "Qtde Compra"), é a venda
#      com a compra informada. Sem a coluna da compra, "Quantidade Comprada" é a do cliente (exportação de
#      pedidos); o custo do envio, do frete ou da entrega e a "Entrada" do crediário não são coluna da compra;
#   4. a exportação de compras sem título, pelo fornecedor e pela entrada: o cabeçalho tem o fornecedor e a
#      data ou a nota de entrada ("Data Entrada", "Dt. de Entrada", "NF Entrada", "Nota Fiscal de Entrada"), e
#      nenhuma coluna que só existe em vendas.
# Rótulo do topo não é título de vendas: "Vendas por:" | "Todas", "Venda por: Todas", "Vendas x Compras:",
# "Vendas no Período: R$ 1.234,00" e "Histórico de vendas considerado: 90 dias" são parâmetros (o relatório de
# compras continua recusado). Conta o rótulo do título ("Relatório: Vendas por produto") e o título com
# dois-pontos que começa pelo nome do relatório ("Relatório de Vendas: Outubro"), ou por "Vendas por" e
# "Faturamento por" com o que vem depois ("Vendas por Produto: Outubro"). A palavra "Venda" solta ("Venda Nova
# Supermercado", "Venda do Zé") não é título de vendas.
# Quando nem o cabeçalho de vendas é achado, o título (1), ou o total da compra sem nada de venda nas
# primeiras linhas (2), troca a mensagem genérica ("colunas essenciais") pela de compras.
_RE_TOPO_DE_VENDAS = re.compile(r"\b(?:vendas?|vendid[oa]s?|faturamento|faturad[oa]s?|saidas?)\b")
# O qualificador entre o nome do relatório e o assunto ("Relatório Analítico de Vendas", "Relatório Sintético de
# Compras", "Resumo Diário de Vendas").
_QUALIFICADOR_DO_RELATORIO = (r"(?: (?:geral|analitic[oa]|sintetic[oa]|detalhad[oa]|resumid[oa]|consolidad[oa]|"
                              r"gerencial|mensal|diari[oa]))?")
_RE_TITULO_DE_VENDAS_NO_TOPO = re.compile(
    r"(?:(?:relatorio|rel|giro|resumo|analise|listagem|ranking|mapa|curva abc|curva|movimento|historico|relacao|"
    r"demonstrativo|consulta|extrato|estatisticas?|total)" + _QUALIFICADOR_DO_RELATORIO + r" (?:d[eao]s? )?vendas?\b"
    r"|vendas? (?:por|x|e)\b"
    r"|vendas? (?:de|dos|das) (?:produtos?|itens|mercadorias?)\b"
    r"|vendas? (?:d[oe]|no) (?:periodo|mes|dia)\b"
    r"|vendas? (?:diarias?|mensal|mensais)\b"
    r"|(?:produtos?|itens|mercadorias?|pecas) (?:mais )?vendid[oa]s?\b"
    r"|acertos? (?:de |da |do )?consignac\w*\b"
    r"|curva abc (?:d[eao]s? )?(?:produtos?|itens|mercadorias?) vendid[oa]s?\b"
    r"|pedidos? de vendas?\b"
    r"|faturamento (?:por|diario|mensal)\b"
    r"|(?:relatorio|rel|relacao|listagem|resumo|consulta|demonstrativo|ranking|curva abc|mapa)"
    + _QUALIFICADOR_DO_RELATORIO + r" (?:d[eao]s? )?"
    r"(?:pedidos? de vendas?|(?:produtos?|itens|mercadorias?|pecas) (?:mais )?vendid[oa]s?|faturamento|"
    r"acertos? (?:de |da |do )?consignac\w*)\b)")
# A compra do cliente é a venda da loja: "Compras por Cliente", "Ranking de Compras por Cliente", "Histórico de
# Compras do Cliente"; e "Histórico" ou "Extrato de Compras" com o rótulo do cliente no topo ("Cliente:" | "JOAO").
_RE_COMPRA_DO_CLIENTE = re.compile(r"\bcompras? (?:por|d[oa]s?|de) (?:clientes?|consumidor(?:es)?)\b")
_RE_HISTORICO_DE_COMPRAS = re.compile(r"(?:historico|extrato)(?: d[eao]s?)? compras?\b")
_RE_ROTULO_DO_CLIENTE = re.compile(r"(?:(?:nome|cod|codigo)(?: d[oa])? )?(?:clientes?|consumidor(?:es)?)")
# O rótulo do título: a célula de depois dele (ou o resto da célula) é o título do relatório.
_RE_ROTULO_DE_TITULO = re.compile(r"(?:(?:tipo|nome|modelo) d[eo] )?(?:relatorio|rel|titulo)")
# O título com dois-pontos ("Relatório de Vendas:" | "Outubro") começa pelo nome do relatório, ou por "Vendas por"
# e "Faturamento por" com o que vem depois ("Vendas por Produto:" | "Outubro"), e não termina pendurado.
# "Histórico" fica de fora ("Histórico de vendas considerado:" é parâmetro), e "Vendas x Compras:" também.
_NOMES_DE_RELATORIO = frozenset({"relatorio", "rel", "resumo", "analise", "listagem", "ranking", "mapa", "curva",
                                 "giro", "relacao", "demonstrativo", "consulta", "extrato", "estatistica",
                                 "estatisticas", "movimento"})
_FIM_DE_PARAMETRO = frozenset({"por", "x", "e", "de", "do", "da", "dos", "das", "em", "no", "na", "com",
                               "considerado", "considerada", "considerados", "consideradas"})
_RE_TITULO_DE_RELATORIO_DE_COMPRAS = re.compile(
    r"(?:(?:relatorio|rel|resumo|analise|listagem|relacao|mapa|movimento|movimentacao|historico|consulta|"
    r"demonstrativo|extrato|posicao)" + _QUALIFICADOR_DO_RELATORIO + r"(?: d[eao]s?)? (?:compras?|entradas?)\b"
    r"|(?:curva abc|curva|ranking|giro)" + _QUALIFICADOR_DO_RELATORIO + r"(?: d[eao]s?)? compras?\b"
    r"|(?:compras?|entradas?)(?: d[eao]s?)? (?:mercadorias?|produtos?|notas?|fornecedor(?:es)?|estoque|mes|periodo)\b"
    r"|(?:compras?|entradas?) por (?:fornecedor(?:es)?|produtos?|itens|grupos?|subgrupos?|secao|secoes|"
    r"departamentos?|categorias?|marcas?|familias?|periodo|data|datas|notas?|nf|nfe|comprador(?:es)?|lojas?|"
    r"filial|filiais|mes|meses)\b"
    r"|(?:compras?|entradas?) n[oa] (?:periodo|mes|data)\b"
    r"|(?:compras?|entradas?) (?:realizadas|efetuadas)\b"
    r"|compras? entradas?\b"
    r"|(?:(?:relatorio|rel|relacao|listagem|consulta|demonstrativo|extrato|resumo|mapa|analise)"
    + _QUALIFICADOR_DO_RELATORIO + r" (?:d[eao]s? )?)?"
    r"(?:(?:pedidos?|ordens?|ordem) de compras?|mercadorias? recebidas|recebimento (?:de |das )?mercadorias?)\b"
    r"|entradas? de (?:nf|nfe|nf e|notas?)\b"
    r"|(?:(?:relatorio|rel|relacao|listagem|resumo|consulta|demonstrativo|extrato)" + _QUALIFICADOR_DO_RELATORIO
    + r" (?:de |das |dos )?)?"
    r"(?:notas?|nf|nfe|nf e) (?:fisca(?:l|is) )?(?:eletronicas? )?de (?:compras?|entradas?)\b"
    r"|(?:compras?|entradas?)$)")
_RE_TITULO_DO_CLIENTE = re.compile(r"\b(?:clientes?|consumidor(?:es)?)\b")
_PALAVRA_DE_COMPRAS = frozenset({"compra", "compras", "entrada", "entradas"})
_RE_TITULO_DE_TOTAL_DA_COMPRA = re.compile(
    r"\b(?:total|tot)\b(?: \w+){0,2} (?:da |de |do |das |dos )?compras?\b|\bcompras? total\b")
# O total da venda pelo nome, mesmo fora do dicionário ("Total da Venda", "Valor de Venda", "Venda Total", "Total
# Vendido"): com ele, o total da compra ao lado não é o único total. "Preço Venda" é o unitário e não conta.
_RE_TITULO_DE_TOTAL_DA_VENDA = re.compile(r"\b(?:total|tot|valor|vlr|vl|vr)\b(?: d[aeo]s?)? vend\w*|\bvendas? total\b")
_RE_COLUNA_QUANTIDADE_COMPRADA = re.compile(r"\b(?:qtde?|qtd|quantidade|quant) (?:comprad\w*|compras?)\b")
# A data ou a nota da entrada da mercadoria ("Data Entrada", "Dt. de Entrada", "NF Entrada", "Nota Fiscal de
# Entrada"). "Entrada" sozinha é também o sinal do crediário.
_RE_COLUNA_DE_ENTRADA = re.compile(
    r"\b(?:data|dt)(?: d[ae])? entrada\b|\b(?:nota|nf|nfe|nf e)(?: fiscal)?(?: d[ae])? entrada\b"
    r"|\bentradas? de mercadorias?\b")
_RE_COLUNA_FORNECEDOR = re.compile(r"\bfornecedor(?:es)?\b")
# Coluna própria da compra: o fornecedor, o custo (fora o do envio, do frete ou da entrega) e a entrada.
_RE_COLUNA_DA_COMPRA = re.compile(
    r"\bfornecedor(?:es)?\b|\bcustos?\b(?! (?:d[aeo] )?(?:envio|frete|entrega)\b)|" + _RE_COLUNA_DE_ENTRADA.pattern)
# Coluna que só existe em vendas, para a recusa de compras (a de _recusar_relatorio_de_vendas_nas_compras, a
# do campo de compras da análise mensal, continua como estava).
_RE_COLUNA_SO_DA_VENDA = re.compile(
    r"\b(?:qtde?|qtd|quantidade|quant) vend\w*|\btotal (?:d[aeo]s? )?vendas?\b|\bvalor total (?:d[aeo] )?venda\b"
    r"|\b(?:data|dt)(?: d[ae])? vendas?\b")
_LINHAS_DO_TOPO = 30

# Uma célula preenchida do topo: o texto normalizado; se é a primeira preenchida do arquivo; se vem logo depois de
# um rótulo com dois-pontos que não é o do título (o valor de um filtro) ou que é (o lugar do título); e, quando a
# própria célula é um rótulo ("Vendas por:") ou começa por um ("Comprador: TODOS"), o rótulo e o valor, normalizados
# (o rótulo é None na célula sem dois-pontos), e os trechos do rótulo antes do separador (" - ", " | ").
_CelulaDoTopo = namedtuple("_CelulaDoTopo", "texto primeira valor_de_filtro depois_do_titulo rotulo valor trechos",
                           defaults=((),))
# O dois-pontos do rótulo: o da hora ("10:35") não é.
_RE_DOIS_PONTOS_DO_ROTULO = re.compile(r"(?<!\d):|:(?!\d)")
# O separador entre o título e o resto da célula ("Vendas x Compras - Período: ...").
_RE_SEPARADOR_DO_TITULO = re.compile(r"\s+[-–—|]+\s+")


def _celulas_do_topo(bruto: pd.DataFrame, ate: int) -> list:
    """[_CelulaDoTopo] das primeiras `ate` linhas."""
    celulas, primeira = [], True
    for linha in range(min(ate, len(bruto))):
        rotulo_anterior = None  # o rótulo da célula anterior da linha, quando ela termina em dois-pontos
        preenchidas = [celula for celula in bruto.iloc[linha].tolist() if not _celula_vazia(celula)]
        for posicao, celula in enumerate(preenchidas):
            texto = str(celula).strip()
            rotulo, valor, trechos, com_valor_ao_lado = None, "", (), False
            dois_pontos = _RE_DOIS_PONTOS_DO_ROTULO.search(texto)
            if dois_pontos is not None:
                antes, depois = texto[:dois_pontos.start()], texto[dois_pontos.end():]
                rotulo, valor = _norm_chave(antes), _norm_chave(depois)
                trechos = tuple(_norm_chave(trecho) for trecho in _RE_SEPARADOR_DO_TITULO.split(antes))[:-1]
            elif posicao + 1 < len(preenchidas) and _valor_de_parametro(preenchidas[posicao + 1]):
                # O rótulo sem dois-pontos, com o valor na célula ao lado ("Vendas no Período" | "R$ 1.234,00").
                rotulo, valor, com_valor_ao_lado = _norm_chave(texto), _norm_chave(preenchidas[posicao + 1]), True
            do_titulo = rotulo_anterior is not None and _RE_ROTULO_DE_TITULO.fullmatch(rotulo_anterior) is not None
            celulas.append(_CelulaDoTopo(_norm_chave(celula), primeira, rotulo_anterior is not None and not do_titulo,
                                         do_titulo, rotulo, valor, trechos))
            primeira = False
            rotulo_anterior = rotulo if (texto.endswith(":") or com_valor_ao_lado) else None
    return celulas


def _valor_de_parametro(celula) -> bool:
    """A célula é um valor em reais ou com casas decimais ("1.234,00", "R$ 1.234,00", 1234.5): o valor de um
    parâmetro. O número inteiro ("2025", "12") fica de fora: pode ser o ano ou a página ao lado do título."""
    texto = str(celula).strip()
    if re.fullmatch(r"(?i)r\$\s*-?[\d.,]*\d", texto):
        return True
    return _so_numero(celula) and re.search(r"[.,]\d", texto) is not None and not re.fullmatch(r"\d+\.0", texto)


def _rotulo_e_titulo_de_vendas(rotulo: str) -> bool:
    """O rótulo é ele mesmo o título de vendas ("Relatório de Vendas:" | "Outubro", "Vendas por Produto:" |
    "Outubro"): começa pelo nome do relatório, ou por "Vendas por" e "Faturamento por" com o que vem depois, e não
    termina pendurado. "Vendas por:", "Vendas no Período:", "Vendas x Compras:" e "Histórico de vendas
    considerado:" não são."""
    palavras = rotulo.split()
    if len(palavras) < 2 or _RE_TITULO_DE_VENDAS_NO_TOPO.match(rotulo) is None:
        return False
    if palavras[0] in _NOMES_DE_RELATORIO:
        return True  # também "Relatório de Vendas De:" e "Relatório de Vendas por:" | "Produto"
    return (palavras[-1] not in _FIM_DE_PARAMETRO and palavras[0] in ("venda", "vendas", "faturamento")
            and palavras[1] == "por")


def _titulo_de_vendas(celulas: list) -> bool:
    """Alguma célula é título de relatório de vendas, ou da compra do cliente (a venda da loja). O rótulo de
    filtro e o valor dele não contam (ver o texto acima)."""
    com_rotulo_do_cliente = any(celula.rotulo is not None and _RE_ROTULO_DO_CLIENTE.fullmatch(celula.rotulo)
                                for celula in celulas)
    for celula in celulas:
        if celula.valor_de_filtro:
            continue
        if celula.rotulo is None:
            texto = celula.texto
        elif _RE_ROTULO_DE_TITULO.fullmatch(celula.rotulo):
            texto = celula.valor
        elif _rotulo_e_titulo_de_vendas(celula.rotulo):
            texto = celula.rotulo
        elif any(_RE_TITULO_DE_VENDAS_NO_TOPO.match(trecho) or _RE_COMPRA_DO_CLIENTE.search(trecho)
                 for trecho in celula.trechos):
            return True  # "Vendas x Compras - Período: ...", "Produtos Vendidos - Emitido em: ..."
        else:
            continue
        if (_RE_TITULO_DE_VENDAS_NO_TOPO.match(texto) or _RE_COMPRA_DO_CLIENTE.search(texto)
                or (com_rotulo_do_cliente and _RE_HISTORICO_DE_COMPRAS.match(texto))):
            return True
    return False


def _titulo_de_compras(celulas: list) -> bool:
    """Alguma célula é título de relatório de compras ou de entradas (pelo começo; ver o texto acima)."""
    for celula in celulas:
        if celula.valor_de_filtro:
            continue
        if celula.rotulo is not None and _RE_ROTULO_DE_TITULO.fullmatch(celula.rotulo):
            texto, lugar_do_titulo = celula.valor, True
        else:
            texto, lugar_do_titulo = celula.texto, celula.primeira or celula.depois_do_titulo
        if not texto or _RE_TITULO_DO_CLIENTE.search(texto) or _RE_TOPO_DE_VENDAS.search(texto):
            continue
        if texto in _PALAVRA_DE_COMPRAS and not lugar_do_titulo:
            continue
        if _RE_TITULO_DE_RELATORIO_DE_COMPRAS.match(texto):
            return True
    return False


def _recusar_relatorio_de_compras(bruto: pd.DataFrame, idx: int, linhas_cab: int, mapa: dict, nome: str) -> None:
    """Recusa (ETLRelatorioDeCompras) o relatório de compras ou de entradas lido como vendas."""
    # No cabeçalho de duas linhas, a de cima pode ser o título do relatório ("Relatório de Compras por Produto"
    # sobre "Item;Qtde;Valor Total"): ela também é topo.
    topo = _celulas_do_topo(bruto, min(idx + (1 if linhas_cab == 2 else 0), _LINHAS_DO_TOPO))
    if _titulo_de_vendas(topo):
        return
    if _titulo_de_compras(topo):
        raise ETLRelatorioDeCompras(nome)
    todos = _titulos_do_cabecalho(bruto, idx, linhas_cab)
    titulos = [titulo for titulo in todos if titulo]
    # A coluna que só existe em vendas vem antes da regra do total da compra: com ela, a quantidade lida é a
    # vendida, e o faturamento sai da venda, nunca do total da compra.
    if any(_RE_COLUNA_SO_DA_VENDA.search(titulo) for titulo in titulos):
        return
    if ("valor_venda" not in mapa and any(_RE_TITULO_DE_TOTAL_DA_COMPRA.search(titulo) for titulo in titulos)
            and not any(_RE_TITULO_DE_TOTAL_DA_VENDA.search(titulo) for titulo in titulos)):
        raise ETLRelatorioDeCompras(nome)
    quantidade = todos[mapa["quantidade"]] if "quantidade" in mapa else ""
    if (_RE_COLUNA_QUANTIDADE_COMPRADA.search(quantidade)
            and any(_RE_COLUNA_DA_COMPRA.search(titulo) for titulo in titulos)):
        raise ETLRelatorioDeCompras(nome)
    if (any(_RE_COLUNA_FORNECEDOR.search(titulo) for titulo in titulos)
            and any(_RE_COLUNA_DE_ENTRADA.search(titulo) for titulo in titulos)):
        raise ETLRelatorioDeCompras(nome)


def _recusar_compras_sem_cabecalho_de_vendas(bruto: pd.DataFrame, nome: str) -> None:
    """O cabeçalho de vendas não foi achado: recusa como compras quando as primeiras linhas trazem o título de
    compras ou de entradas, ou o total da compra sem nada de venda."""
    celulas = _celulas_do_topo(bruto, _LINHAS_DO_TOPO)
    if _titulo_de_vendas(celulas):
        return
    if _titulo_de_compras(celulas):
        raise ETLRelatorioDeCompras(nome)
    textos = [celula.texto for celula in celulas]
    if (any(_RE_TITULO_DE_TOTAL_DA_COMPRA.search(texto) for texto in textos)
            and not any(_RE_TOPO_DE_VENDAS.search(texto) for texto in textos)):
        raise ETLRelatorioDeCompras(nome)


def _recusar_relatorio_de_vendas_nas_compras(bruto: pd.DataFrame, idx: int, linhas_cab: int, nome: str) -> None:
    topo = [_norm_chave(celula) for linha in range(min(idx, 30)) for celula in bruto.iloc[linha].tolist()
            if not _celula_vazia(celula)]
    if any(_RE_TITULO_DE_COMPRAS.search(texto) for texto in topo):
        return
    titulos = [titulo for titulo in _titulos_do_cabecalho(bruto, idx, linhas_cab) if titulo]
    so_de_vendas = (any(_RE_COLUNA_SO_DE_VENDAS.search(titulo) for titulo in titulos)
                    and not any(_RE_COLUNA_DE_COMPRAS.search(titulo) for titulo in titulos))
    titulo_de_vendas = any(_RE_TITULO_DE_RELATORIO_DE_VENDAS.match(texto) for texto in topo)
    if so_de_vendas or titulo_de_vendas:
        raise ETLValidationError(
            f"O arquivo de COMPRAS '{nome}' é um relatório de VENDAS (o título ou as colunas falam em venda, "
            "e não em compra). Envie o relatório de compras ou de entradas de mercadoria.")


# Prova de leitura do alinhamento: parte das linhas de item em que o produto tem de ser texto e a quantidade
# e o valor têm de ser números.
_PROVA_DO_ALINHAMENTO = 0.9


def _so_numero(valor) -> bool:
    """A célula é só um número (sem letra nenhuma): não é nome de produto."""
    return not any(caractere.isalpha() for caractere in str(valor)) and _parse_valor(valor) is not None


def _comeca_por_numero(valor) -> bool:
    """A célula é um número, no máximo com a moeda antes e a unidade depois. O texto que começa por letra
    ("CX 12", "LOTE 7") não é, embora o conversor de quantidade tire dele um número."""
    texto = _RE_MOEDA.sub("", str(valor)).strip()
    return bool(texto) and not texto[0].isalpha() and _parse_valor(valor) is not None


def _alinhar_relatorio_paginado(bruto: pd.DataFrame, idx: int, linhas_cab: int, mapa: dict) -> pd.DataFrame:
    """Relatório paginado de ERP exportado para planilha: as células mescladas deixam o título de cada
    coluna numa coluna e o dado em outra, na mesma ordem (o título "Produto" na coluna I e os nomes na E).
    Cada dado passa para a coluna do título correspondente, pela ordem, quando TUDO isto vale:
      - a coluna do título do produto não tem dado nenhum no corpo (só o próprio título, repetido a cada
        página);
      - há tantas colunas com título quanto colunas com dado nas linhas de item (a forma mais comum das
        linhas com três células ou mais, somada às formas que só diferem dela por células em branco);
      - cada título fica entre a coluna de dado anterior e a seguinte (é a mesma coluna visual);
      - depois de alinhar, o produto é texto e a quantidade e o valor são números em ao menos 90% das
        linhas de item (a prova de leitura: pega a coluna a menos compensada por outra a mais quando isso
        põe um número no lugar do nome, ou um texto que começa por letra no lugar da quantidade ou do
        valor).
    As linhas a que falta o produto, a quantidade ou o valor ficam vazias: o cliente do bloco, os rodapés,
    os totais e o frete, e também a linha de item com a quantidade ou o valor em branco. Em qualquer outro
    caso a matriz volta como veio: o relatório que já era lido não muda, e o que não era lido continua
    recusado.

    Limite conhecido: uma coluna com título e sem dado em quase todas as linhas, compensada por uma coluna
    sem título, passa pelas conferências quando o que cai em cada lugar tem o tipo esperado. Um TEXTO ao
    lado do nome do produto (a unidade, a marca) vira o nome do produto; um NÚMERO ao lado da quantidade ou
    do valor vira a quantidade ou o valor. Os títulos e os dados ficam em mesmo número e na mesma ordem: a
    matriz não traz as mesclas, e só elas diriam qual dado é de qual título (Docs/UNIFICACAO_MOTOR_ETL.md,
    seções 3 e 10)."""
    inicio = idx + linhas_cab
    if inicio >= len(bruto) or "produto" not in mapa:
        return bruto
    corpo = bruto.iloc[inicio:]
    ocupadas = ~corpo.apply(lambda serie: serie.map(_celula_vazia)).to_numpy(dtype=bool)
    # A forma (colunas preenchidas) mais comum entre as linhas com três células ou mais é a das linhas de item.
    formas = Counter(tuple(int(coluna) for coluna in np.flatnonzero(linha)) for linha in ocupadas if linha.sum() >= 3)
    if not formas:
        return bruto
    principal, vezes = formas.most_common(1)[0]
    # A mesma linha de item com uma célula em branco (o vendedor, o documento) tem uma forma contida na
    # principal, ou a contém: entra na conta das colunas de dado quando é frequente (um quinto da principal).
    dados = set(principal)
    for forma, quantas in formas.items():
        if quantas * 5 >= vezes and (dados.issuperset(forma) or set(principal).issubset(forma)):
            dados |= set(forma)
    dados = sorted(dados)
    titulos = sorted({coluna for k in range(linhas_cab) for coluna in range(bruto.shape[1])
                      if not _celula_vazia(bruto.iat[idx + k, coluna])})
    if mapa["produto"] in dados or len(titulos) != len(dados) or titulos == dados:
        return bruto
    # Geometria: o título de cada coluna fica entre a coluna de dado anterior e a seguinte.
    for posicao, titulo in enumerate(titulos):
        anterior = dados[posicao - 1] if posicao else -1
        seguinte = dados[posicao + 1] if posicao + 1 < len(dados) else bruto.shape[1]
        if not anterior <= titulo <= seguinte:
            return bruto
    # A coluna do título do produto só pode ter, no corpo, o próprio título (o cabeçalho repetido): com
    # qualquer outro valor ali, o relatório não é o paginado que esta função conhece.
    do_titulo = {_norm_chave(bruto.iat[idx + k, mapa["produto"]]) for k in range(linhas_cab)}
    if any(not _celula_vazia(valor) and _norm_chave(valor) not in do_titulo
           for valor in corpo.iloc[:, mapa["produto"]].tolist()):
        return bruto
    alinhado = pd.DataFrame(None, index=corpo.index, columns=corpo.columns, dtype=object)
    for origem, alvo in zip(dados, titulos):
        alinhado.iloc[:, alvo] = corpo.iloc[:, origem].to_numpy()
    essenciais = [mapa[campo] for campo in ("produto", "quantidade") if campo in mapa]
    essenciais.append(mapa["valor_venda"] if "valor_venda" in mapa else mapa["preco_unitario"])
    incompletas = alinhado.iloc[:, essenciais].apply(lambda serie: serie.map(_celula_vazia)).any(axis=1)
    # Prova de leitura, nas linhas de item: o nome do produto não é um número, e a quantidade e o valor são.
    itens = alinhado.loc[~incompletas.to_numpy()]
    if itens.empty or (~itens.iloc[:, essenciais[0]].map(_so_numero).astype(bool)).mean() < _PROVA_DO_ALINHAMENTO:
        return bruto
    for coluna in essenciais[1:]:
        if itens.iloc[:, coluna].map(_comeca_por_numero).mean() < _PROVA_DO_ALINHAMENTO:
            return bruto
    alinhado.loc[incompletas.to_numpy(), :] = None
    resultado = pd.concat([bruto.iloc[:inicio], alinhado])
    resultado.attrs = dict(bruto.attrs)
    return resultado


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

    try:
        idx, mapa, linhas_cab = _detectar_cabecalho(bruto, ALIAS_VENDAS, _validar_vendas)
    except ETLValidationError:
        _recusar_compras_sem_cabecalho_de_vendas(bruto, nome)
        raise
    _recusar_separador_duvidoso(bruto, idx, linhas_cab, nome)
    _recusar_titulos_repetidos(bruto, idx, linhas_cab, mapa, nome)
    _recusar_relatorio_de_compras(bruto, idx, linhas_cab, mapa, nome)
    bruto = _alinhar_relatorio_paginado(bruto, idx, linhas_cab, mapa)
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
            "ncm": "ncm" in colunas, "categoria": "categoria" in colunas, "unidade": "unidade" in colunas,
            "data": "data" in colunas,
            "ambiguo": any(_RE_TITULO_DE_CADASTRO.search(cabecalhos.get(c, "")) for c in ("codigo", "produto")),
            # A coluna lida como código ou como produto é de loja ou filial ("Cód. Loja" antes do código,
            # "Descrição Loja" antes da descrição do produto): os "produtos" seriam as lojas; o lead recusa.
            "produto_de_loja": any(_RE_TITULO_UNIDADE.search(cabecalhos.get(c, ""))
                                   and not _RE_TITULO_DE_PRODUTO.search(cabecalhos.get(c, ""))
                                   for c in ("codigo", "produto")),
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
    if cadastro and "_unidade" in df.columns and "_documento" in df.columns:
        df["_loja_do_cupom"] = _loja_do_cupom(df)
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


# =====================================================================
# 6b) Tabelas auxiliares do lote (ingestão de vários arquivos)
# =====================================================================
# Num envio com a venda item a item com a data de cada venda (a "âncora temporal", como a Opção 27 do ERP, "Vendas
# por Cliente Detalhada por Produto"), os outros arquivos deixam de ser vendas e completam a âncora
# (raio_x._ler_envio_uma_vez). A classificação vem ANTES da leitura de vendas quando o título do relatório anuncia a
# tabela auxiliar (ler_tabela_pelo_titulo: posição de estoque, inventário, lista de preços); senão, depois de a
# leitura de vendas falhar, pelas colunas (ler_tabela_auxiliar). Os leitores reconhecem:
#   - posição de estoque e inventário: produto e saldo, com o custo unitário ou o valor em estoque;
#   - lista de preços: produto e preço (o cadastro dos produtos), com o custo quando houver;
#   - resumo por pedido ou documento: o número da venda e o valor, com o desconto (em reais, ou em percentual
#     convertido em reais), o CPF/CNPJ do cliente e a hora pelos mesmos títulos da venda item a item (_dado_da_venda);
#   - o resumo de vendas sem produto (por cliente, fornecedor, grupo ou forma de pagamento), pelo título
#     (resumo_de_vendas_sem_produto).
# O relatório de compras é reconhecido pela recusa de sempre (ETLRelatorioDeCompras) e entra no lote fora das contas: o
# resumo por nota não traz o produto.
# Dois cuidados, da revisão independente de 11/2026, para nenhuma venda sumir do faturamento:
#   - a classificação pelo título lê só o TÍTULO do relatório (_titulo_do_relatorio: a primeira célula com letras do
#     topo), comparado pelo começo, e nunca um parâmetro com dois-pontos ("Tabela de Preço: 1 - VAREJO", "Considerar
#     estoque atual: Sim"), comum em relatório de vendas; o título de vendas ou de compras no topo decide contra ela;
#   - o relatório de vendas que não foi lido (sem uma das colunas) não vira tabela auxiliar pelas colunas e continua
#     recusado: o resumo por documento não tem a coluna do produto, a lista de preços não tem a quantidade nem o total
#     da venda, a posição de estoque lida pelas colunas também não, e o resumo sem produto não tem título de produto.
#
# Os leiautes reais (11/2026: a Opção 27 e os relatórios auxiliares do mesmo ERP) são relatórios paginados exportados
# para .xls, e os leitores tratam o que eles têm:
#   - o título da coluna em duas linhas ("Est." sobre "Atual", "Total" sobre "Desc. %"): a linha de baixo só tem
#     texto, e cada célula dela fica debaixo de um título da de cima (_linha_continua_o_cabecalho). Sem isto, o
#     "Total" de "Total Desc. %" ficava com o valor do documento;
#   - o título numa coluna e o dado em outra, na mesma ordem, por causa das mesclas ("Cód." na coluna A e o código na
#     B): _alinhar_tabela_auxiliar, a mesma ideia de _alinhar_relatorio_paginado;
#   - os blocos por grupo de produto ("Grupo :" e o nome) e por vendedor, com o cabeçalho repetido, o total do bloco,
#     o cabeçalho de página e o rodapé: a linha sem o produto (ou o documento) ou sem o número fica de fora;
#   - o título com as letras separadas por espaço ("D e s c r i ç ã o   d o   p r o d u t o", no inventário):
#     _desespacar;
#   - os títulos curtos ("Cód.", "Nome", "Valor"), pelos títulos de reserva, que comparam o título inteiro.
# O que cada leiaute real dá:
#   - "Posição de estoque atual de produtos": Código, Nome produto, Est. Atual, Preço Custo e Valor Estoque (Forn.,
#     Est. Mín. e Est. Máx. ficam de fora), em blocos por grupo;
#   - "Inventário de Estoque": Cód., Descrição do produto, Custo Unitário, Saldo de Estoque e Valor do Estoque (o NCM
#     fica de fora), com o cabeçalho a cada página;
#   - "Lista de preços": Cód. Prod., Nome e Preço, em blocos por grupo, com uma linha por grupo de preço do ERP (o
#     mesmo produto em duas linhas, sem dizer de que grupo é cada preço): o preço não entra nas contas;
#   - "Giro de vendas por Cliente / documento": Nr. Doc., Valor e Total Desc. % (Data, Cliente, Acres., Frete e % Méd
#     Comi. ficam de fora), em blocos por vendedor. O desconto em reais é valor x percentual / (100 - percentual): o
#     valor do documento já vem líquido do desconto (a soma bate com o "Vr. desctos" do rodapé do relatório);
#   - "Posição das compras de produtos": o resumo por nota (Data da Compra, Número NF, Fornecedor, Vr Total Produtos,
#     Valor Nf), reconhecido como relatório de compras.
ALIAS_ESTOQUE = {
    "codigo": ["id produto", "id do produto", "codigo produto", "codigo", "cod produto", "cod barras", "ean", "sku",
               "ref", "cod "],
    "produto": ["nome do produto", "descricao do produto", "descricao produto", "nome produto", "produto",
                "mercadoria", "descricao do item", "descricao"],
    # O valor em estoque antes do saldo: "Valor em Estoque" também tem a palavra "estoque".
    "valor_estoque": ["valor em estoque", "valor do estoque", "valor estoque", "total em estoque", "custo total",
                      "total custo", "valor total"],
    "custo_unitario": ["custo unitario", "custo unit", "custo medio", "custo de reposicao", "custo reposicao",
                       "preco de custo", "preco custo", "vlr custo", "vr custo", "valor custo", "custo"],
    # "Est. Atual" é o saldo; "Est. Mín." e "Est. Máx." (os limites do cadastro) não casam fragmento nenhum.
    "estoque": ["estoque atual", "est atual", "estq atual", "saldo estoque", "saldo atual", "qtde estoque",
                "qtd estoque", "qtde em estoque", "quantidade em estoque", "quantidade estoque", "estoque fisico",
                "qtde atual", "qtd atual", "quantidade atual", "qtde disponivel", "qtd disponivel",
                "quantidade disponivel", "saldo disponivel", "disponivel", "estoque", "saldo"],
}
ALIAS_PRECOS = {
    "codigo": ALIAS_ESTOQUE["codigo"],
    "produto": ALIAS_ESTOQUE["produto"],
    # O custo antes do preço: "Preço de Custo" também tem a palavra "preço".
    "custo_unitario": ["custo unitario", "custo unit", "custo medio", "preco de custo", "preco custo", "custo"],
    "preco": ["preco de venda", "preco venda", "preco unitario", "preco unit", "valor unitario", "vlr unitario",
              "valor de venda", "preco"],
}
ALIAS_DOCUMENTOS = {
    "documento": ["nr pedido", "nro pedido", "numero do pedido", "numero pedido", "n pedido", "no pedido", "pedido",
                  "nr doc", "nr documento", "numero do documento", "documento", "nr cupom", "cupom", "nr nota",
                  "nota fiscal", "nr nf", "coo"],
    "valor": ["valor total", "vlr total", "vl total", "total liquido", "valor liquido", "total do pedido",
              "total pedido", "valor do pedido", "total"],
}
# Títulos de reserva das tabelas auxiliares: comparam o título inteiro e só completam o campo que o dicionário deixou
# sem coluna (_mapear_cabecalho). "Qtde" e "Quantidade" só são o saldo quando o título do relatório é de estoque
# (ler_tabela_pelo_titulo): fora dele, a planilha com o produto e a quantidade é a venda sem a coluna do valor, que
# continua recusada.
_RESERVA_DO_CODIGO = ("cod", "cod prod", "cod produto", "cod do produto", "cod item", "cod int", "cod interno")
_RESERVA_DO_PRODUTO = ("nome", "descricao", "desc", "item", "mercadoria", "nome do item")
RESERVA_ESTOQUE = {"codigo": _RESERVA_DO_CODIGO, "produto": _RESERVA_DO_PRODUTO}
RESERVA_ESTOQUE_PELO_TITULO = {**RESERVA_ESTOQUE, "estoque": ("qtde", "qtd", "quant", "quantidade", "est")}
RESERVA_PRECOS = {"codigo": _RESERVA_DO_CODIGO, "produto": _RESERVA_DO_PRODUTO}
RESERVA_DOCUMENTOS = {"valor": ("valor", "vlr", "vl", "vr", "valor r", "vlr r", "vl r", "vr r", "valor doc",
                                "valor documento", "valor do documento", "total r")}
_RE_TITULO_DE_ESTOQUE = re.compile(r"\b(?:posicao|saldos?|inventario|balanco)(?: \w+){0,2} (?:de |do |dos )?estoques?\b"
                                   r"|\bestoques? (?:atual|atuais|fisico|a custo)\b|\binventario\b")
_RE_TITULO_DE_INVENTARIO = re.compile(r"\binventario\b")
_RE_TITULO_DE_PRECOS = re.compile(r"\b(?:lista|tabela|relacao|cadastro)s? (?:de |dos )?(?:precos?|produtos?)\b")
# A classificação prévia (_papel_pelo_titulo) compara só o TÍTULO do relatório (_titulo_do_relatorio), pelo começo:
# "Posição de estoque atual de produtos", "Inventário de Estoque", "Relatório de Estoque", "Lista de preços". O título
# de preços pede a palavra "preço": "Relação de Produtos" e "Cadastro de Produtos" podem ser venda.
_PREFIXO_DO_TITULO = (r"(?:(?:relatorio|rel|consulta|listagem|relacao|demonstrativo|resumo|mapa|impressao)"
                      + _QUALIFICADOR_DO_RELATORIO + r"(?: d[eao]s?)? )?")
# "Estoque" sozinho no começo não basta: a razão social ("ESTOQUE DE FÁBRICA CALÇADOS LTDA") e o módulo ("Estoque - Curva
# ABC de Produtos") começam assim e são de relatório de vendas.
_RE_TITULO_DE_ESTOQUE_NO_TOPO = re.compile(
    r"(?:(?:relatorio|rel|consulta|listagem|demonstrativo|resumo|mapa|impressao)" + _QUALIFICADOR_DO_RELATORIO
    + r"(?: d[eao]s?)? estoques?\b"
    r"|" + _PREFIXO_DO_TITULO + r"(?:(?:posicao|saldos?|inventario|balanco)(?: \w+){0,2} (?:de |do |dos |em )?estoques?\b"
    r"|inventario\b|estoques? (?:atual|atuais|fisico|a custo|de produtos?|dos produtos?)\b))")
# A venda ou a saída numa linha de texto do topo que não é o título (_papel_pelo_titulo). "Venda" sozinha fica de fora:
# é nome de loja ("Venda Nova Supermercado").
_RE_VENDA_NO_TOPO = re.compile(r"\b(?:vendas|vendid[oa]s?|faturamento|faturad[oa]s?|saidas?)\b")
# O preço de venda da unidade numa tabela de estoque ("Preço Venda", "Vlr. Venda", "P. Venda").
_RE_PRECO_DE_VENDA = re.compile(r"\b(?:precos?|valor|vlr|vl|vr|p)\b.*\bvenda\b")
_RE_TITULO_DE_PRECOS_NO_TOPO = re.compile(_PREFIXO_DO_TITULO + r"(?:lista|tabela|relacao)s? (?:de |dos )?precos?\b")
# O rótulo da loja no topo da tabela de estoque ("Filial ....: 002 - LOJA", ou o rótulo numa célula e o nome na
# seguinte), com o depósito junto. A empresa não identifica a loja (é a mesma nas filiais, e a tabela só com ela pode ser
# a de uma filial ou o total), nem a filial "Todas" ou "Consolidado".
_RE_ROTULO_DA_FILIAL = re.compile(r"(?:filial|filiais|loja|lojas|unidade|unidades)\b")
_RE_ROTULO_DO_DEPOSITO = re.compile(r"(?:deposito|depositos|almoxarifado)\b")
# O total no lugar da filial ("Todas", "Todas as filiais", "Geral", "000 - Consolidado", "Total geral") só quando o
# valor começa assim, depois do código: "003 - LOJA GERAL" e "ARMAZÉM GERAL DO POVO" são lojas.
_RE_LOJA_CONSOLIDADA = re.compile(r"^(?:\d+\s+)?(?:todas?|todos|total|geral|consolidad\w*)\b")
# O título de coluna do produto, para as guardas: os do dicionário de vendas e os curtos ("Item", "Mercadoria"), sem o
# total e o agrupamento ("Total Produtos", "Grupo de Produtos"). "Nome" e "Desc." ficam de fora: são também o cliente e o
# desconto.
_TITULOS_DE_PRODUTO_SOZINHOS = frozenset({"descricao", "item", "itens", "mercadoria", "mercadorias", "nome do item",
                                          "produto", "produtos"})
_RE_TITULO_DE_AGREGADO = re.compile(r"\b(?:total|totais|valor|vlr|vl|vr|qtde?|qtd|quantidade|quant|grupos?|subgrupos?|"
                                    r"categorias?|departamentos?|secao|secoes|familias?|marcas?)\b")
# O valor sem dizer de quê ("Valor", "Vlr", "Total"): na tabela auxiliar lida pelas colunas, é o total da venda do
# relatório de vendas que não foi lido, e não o preço nem o valor do estoque.
_TITULOS_DE_VALOR_SOZINHO = frozenset({"valor", "vlr", "vl", "vr", "total", "valor r", "vlr r", "vl r", "vr r", "total r",
                                       "valor rs", "vlr rs", "valor r s"})
# O rótulo de uma data no topo ("Data:", "Emitido em:", "Emissão:", "Posição em:").
_RE_ROTULO_DE_DATA = re.compile(r"\b(?:data|dt|emissao|emitido|emitida|impresso|impressao|gerado|posicao|referencia|em)\b")
# O resumo de vendas por outra coisa que não o produto, pelo título ("Giro de vendas por fornecedor", "por grupo de
# clientes", "por grupo de produtos", "por forma de pagamento", "Por cliente - Ranking"). O título que fala do produto
# ("por produto", "detalhada por produto") é o relatório por produto: lido ou, se não deu, recusado.
_RE_TITULO_DE_RESUMO_SEM_PRODUTO = re.compile(
    r"\bvendas? por (?:clientes?|fornecedor(?:es)?|grupos?|subgrupos?|formas? de pagamento|planos? de pagamento|"
    r"condic\w* de pagamento|vendedor(?:es|as)?|representantes?|lojas?|filial|filiais|cidades?|regiao|regioes|"
    r"estados?|uf|dias?|mes|meses|hora|horas|caixas?|operador(?:es)?|marcas?|fabricantes?|departamentos?|"
    r"secao|secoes|categorias?|familias?)\b")
_RE_TITULO_POR_PRODUTO = re.compile(r"\bpor (?:produtos?|itens?|mercadorias?|referencias?)\b|\bdetalhad")
# O título com as letras separadas por um espaço e as palavras por dois ou mais.
_RE_LETRAS_SEPARADAS = re.compile(r"\S(?: \S){3,}(?: {2,}\S(?: \S)*)*")
# O que a leitura de uma tabela auxiliar devolve: o quadro canônico, a matriz (alinhada), o cabeçalho e o topo acima
# dele (o título do relatório e os parâmetros).
_TabelaLida = namedtuple("_TabelaLida", "df bruto idx linhas_cab mapa topo")
# A chave de ligação pelo código do produto: só letras e algarismos, sem os zeros à esquerda ("0700019" e
# "000007000.19", a máscara do outro relatório do mesmo ERP, são o mesmo código).
_RE_FORA_DO_CODIGO = re.compile(r"[^0-9A-Z]")


def normalizar_codigo_do_produto(valor):
    """O código do produto como chave de ligação entre relatórios, ou None."""
    if valor is None or (isinstance(valor, float) and not math.isfinite(valor)):
        return None
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    texto = _RE_FORA_DO_CODIGO.sub("", _strip_acentos(str(valor)).upper())
    if texto in ("", "NAN", "NONE", "NULL"):
        return None
    return texto.lstrip("0") or "0"


def normalizar_numero_do_documento(numero):
    """O número da venda (indicadores_executivos.numero_da_venda) como chave de ligação, sem os zeros à esquerda de
    cada grupo de algarismos ("P9-00001" e "P9-1" são o mesmo pedido), ou None."""
    if numero is None:
        return None
    return re.sub(r"\d+", lambda grupo: str(int(grupo.group())), str(numero).upper())


def _desespacar(valor):
    """O título com as letras separadas por espaço ("D e s c r i ç ã o   d o   p r o d u t o") com as palavras
    juntas ("Descrição do produto"); qualquer outro valor volta como veio."""
    if not isinstance(valor, str):
        return valor
    texto = valor.strip()
    if not _RE_LETRAS_SEPARADAS.fullmatch(texto):
        return valor
    return " ".join(parte.replace(" ", "") for parte in re.split(r" {2,}", texto))


def _sem_titulos_espacados(bruto: pd.DataFrame, ate: int = 100) -> pd.DataFrame:
    """A matriz com os títulos de letras separadas juntos nas primeiras `ate` linhas (o topo e o cabeçalho)."""
    topo = bruto.iloc[:ate]
    novo = topo.apply(lambda serie: serie.map(_desespacar))
    if novo.equals(topo):
        return bruto
    resultado = pd.concat([novo, bruto.iloc[ate:]])
    resultado.attrs = dict(bruto.attrs)
    return resultado


def _celula_de_rotulo(valor) -> bool:
    """A célula é um rótulo com dois-pontos ("Produto inicial ....: 0001", "Grupo :", "Total:"): parâmetro, bloco ou
    total do relatório, nunca título de coluna. O dois-pontos da hora ("10:35") não conta."""
    return not _celula_vazia(valor) and _RE_DOIS_PONTOS_DO_ROTULO.search(str(valor)) is not None


def _linha_continua_o_cabecalho(linhas: list, i: int) -> bool:
    """A linha seguinte à `i` é a segunda linha dos títulos de coluna ("Est." sobre "Atual", "Total" sobre "Desc. %"):
    só tem texto (nenhum número, data ou rótulo com dois-pontos), e cada célula dela fica debaixo de uma célula
    preenchida da linha `i`, que tem dois títulos ou mais e nenhum rótulo."""
    if i + 1 >= len(linhas):
        return False
    de_cima = {j for j, valor in enumerate(linhas[i]) if not _celula_vazia(valor)}
    de_baixo = [(j, valor) for j, valor in enumerate(linhas[i + 1]) if not _celula_vazia(valor)]
    return (len(de_cima) >= 2 and bool(de_baixo) and not any(_celula_de_rotulo(valor) for valor in linhas[i])
            and all(j in de_cima and not _so_numero(valor) and data_da_linha(valor) is None
                    and not _celula_de_rotulo(valor) for j, valor in de_baixo))


def _titulos_auxiliares(partes: list) -> list:
    """O título normalizado de cada coluna, com as linhas do cabeçalho juntas (a célula vazia não entra)."""
    largura = max((len(parte) for parte in partes), default=0)
    return [" ".join(_norm_chave(parte[j]) for parte in partes if j < len(parte) and not _celula_vazia(parte[j]))
            .strip() for j in range(largura)]


def _detectar_cabecalho_auxiliar(bruto: pd.DataFrame, aliases: dict, reserva: dict, valida_fn, janela: int = 100):
    """(índice, mapa, linhas do cabeçalho) da tabela auxiliar: a linha (com a segunda linha dos títulos, quando há:
    _linha_continua_o_cabecalho) que mapeia mais campos e passa por `valida_fn`, primeiro só com o dicionário e depois
    com os títulos de reserva. A linha com rótulo de dois-pontos (os parâmetros do topo, "Grupo :", os totais) não é
    título de coluna: os parâmetros "Grupo Preço inicial: 1" e "Produto inicial: ..." da lista de preços pareciam um
    cabeçalho com o produto e o preço. Como em _detectar_cabecalho, a coluna de percentual não fica com campo nenhum, e
    a de data não fica com campo numérico. Levanta ETLValidationError quando nenhuma linha passa."""
    limite = min(janela, len(bruto))
    linhas = [bruto.iloc[i].tolist() for i in range(limite)]
    candidatos = []
    for i in range(limite):
        if any(_celula_de_rotulo(valor) for valor in linhas[i]):
            continue
        linhas_cab = 2 if _linha_continua_o_cabecalho(linhas, i) else 1
        partes = linhas[i:i + linhas_cab]
        titulos = _titulos_auxiliares(partes)
        percentuais = frozenset(j for j in range(len(titulos)) if any(
            j < len(parte) and not _celula_vazia(parte[j]) and _titulo_de_percentual(parte[j]) for parte in partes))
        de_data = frozenset(j for j in range(len(titulos)) if any(
            j < len(parte) and _RE_TITULO_DE_DATA.match(_norm_chave(parte[j])) for parte in partes))
        candidatos.append((i, linhas_cab, titulos, percentuais, de_data))
    for com_reserva in (False, True):
        melhor = None
        for i, linhas_cab, titulos, percentuais, de_data in candidatos:
            mapa = _mapear_cabecalho(titulos, aliases, reserva if com_reserva else {}, percentuais, de_data)
            if valida_fn(mapa) and (melhor is None or len(mapa) > len(melhor[1])):
                melhor = (i, mapa, linhas_cab, titulos, percentuais, de_data)
        if melhor is not None:
            i, _mapa, linhas_cab, titulos, percentuais, de_data = melhor
            return i, _mapear_cabecalho(titulos, aliases, reserva, percentuais, de_data), linhas_cab
    raise ETLValidationError("Os títulos de coluna da tabela auxiliar não foram achados.")


def _alinhar_tabela_auxiliar(bruto: pd.DataFrame, idx: int, linhas_cab: int, mapa: dict, *, textos=(), numeros=(),
                             preenchidos=()) -> pd.DataFrame:
    """O alinhamento do relatório paginado (_alinhar_relatorio_paginado) nas tabelas auxiliares: as mesclas deixam o
    título de cada coluna numa coluna e o dado em outra, na mesma ordem ("Cód." na A e o código na B). Cada dado passa
    para a coluna do título correspondente, pela ordem, quando tudo isto vale:
      - há tantas colunas com título quanto colunas com dado nas linhas de item (a forma mais comum das linhas com
        três células ou mais, somada às que só diferem dela por células em branco);
      - cada título fica entre a coluna de dado anterior e a seguinte (é a mesma coluna visual);
      - depois de alinhar, nas linhas com todos os campos essenciais, os de `textos` não são só número, os de `numeros`
        começam por número e os de `preenchidos` têm valor, em ao menos 90% delas.
    Diferente da leitura de vendas, a coluna do título do produto pode ter dado no corpo (na lista de preços o nome
    fica na mesma coluna, e só o código e o preço mudam). As linhas a que falta um campo essencial ficam vazias (o
    grupo, o vendedor, o cabeçalho repetido, os totais e o rodapé). Em qualquer outro caso a matriz volta como veio."""
    inicio = idx + linhas_cab
    if inicio >= len(bruto):
        return bruto
    corpo = bruto.iloc[inicio:]
    ocupadas = ~corpo.apply(lambda serie: serie.map(_celula_vazia)).to_numpy(dtype=bool)
    formas = Counter(tuple(int(coluna) for coluna in np.flatnonzero(linha)) for linha in ocupadas if linha.sum() >= 3)
    if not formas:
        return bruto
    principal, vezes = formas.most_common(1)[0]
    dados = set(principal)
    for forma, quantas in formas.items():
        if quantas * 5 >= vezes and (dados.issuperset(forma) or set(principal).issubset(forma)):
            dados |= set(forma)
    dados = sorted(dados)
    titulos = sorted({coluna for k in range(linhas_cab) for coluna in range(bruto.shape[1])
                      if not _celula_vazia(bruto.iat[idx + k, coluna])})
    if len(titulos) > len(dados):
        # A coluna opcional em branco na maior parte das linhas (o fornecedor, o NCM): a forma que contém a principal e
        # tem tantas colunas quanto os títulos, mesmo rara, diz onde ela fica. A geometria e a prova continuam valendo.
        maiores = [forma for forma, _quantas in formas.most_common()
                   if len(forma) == len(titulos) and set(forma).issuperset(dados)]
        if maiores:
            dados = sorted(maiores[0])
    if len(titulos) != len(dados) or titulos == dados:
        return bruto
    for posicao, titulo in enumerate(titulos):
        anterior = dados[posicao - 1] if posicao else -1
        seguinte = dados[posicao + 1] if posicao + 1 < len(dados) else bruto.shape[1]
        if not anterior <= titulo <= seguinte:
            return bruto
    alinhado = pd.DataFrame(None, index=corpo.index, columns=corpo.columns, dtype=object)
    for origem, alvo in zip(dados, titulos):
        alinhado.iloc[:, alvo] = corpo.iloc[:, origem].to_numpy()
    de_texto = [mapa[campo] for campo in textos if campo in mapa]
    de_numero = [mapa[campo] for campo in numeros if campo in mapa]
    essenciais = de_texto + de_numero + [mapa[campo] for campo in preenchidos if campo in mapa]
    if not essenciais:
        return bruto
    incompletas = alinhado.iloc[:, essenciais].apply(lambda serie: serie.map(_celula_vazia)).any(axis=1).to_numpy()
    itens = alinhado.loc[~incompletas]
    if itens.empty:
        return bruto
    for coluna in de_texto:
        if (~itens.iloc[:, coluna].map(_so_numero).astype(bool)).mean() < _PROVA_DO_ALINHAMENTO:
            return bruto
    for coluna in de_numero:
        if itens.iloc[:, coluna].map(_comeca_por_numero).mean() < _PROVA_DO_ALINHAMENTO:
            return bruto
    alinhado.loc[incompletas, :] = None
    resultado = pd.concat([bruto.iloc[:inicio], alinhado])
    resultado.attrs = dict(bruto.attrs)
    return resultado


def _titulo_do_relatorio(topo: list):
    """O título do relatório, normalizado, ou None. É a primeira célula com letras do topo (o título vem antes dos
    parâmetros; a data e a página não têm letras), e nela: o texto inteiro, sem rótulo; o valor depois do rótulo do
    título ("Relatório: Posição de Estoque"); o trecho antes do separador ("Posição de estoque - Emitido em: ..."); ou
    o rótulo seguido só de uma data ("Posição de estoque em: 09/11/2026"). A primeira célula que é parâmetro ("Tabela de
    Preço: 1 - VAREJO", "Empresa: ...") dá None: o parâmetro nunca é o título, e os seguintes também não."""
    for celula in topo:
        if celula.valor_de_filtro or not re.search(r"[a-z]", celula.texto):
            continue
        if celula.rotulo is None:
            return celula.texto
        if _RE_ROTULO_DE_TITULO.fullmatch(celula.rotulo):
            return celula.valor or None
        if celula.trechos:
            return celula.trechos[0]
        if celula.valor and re.fullmatch(r"[\d ]+", celula.valor) and len(re.sub(r"\D", "", celula.valor)) >= 6:
            return celula.rotulo
        return None
    return None


def _papel_pelo_titulo(topo: list, titulos: list):
    """O papel ("estoque" ou "precos") que o título do relatório (_titulo_do_relatorio), comparado pelo começo, anuncia,
    ou None. O título de relatório de vendas ou de compras em qualquer célula do topo, a palavra de venda no título
    ("Posição de estoque e vendas") e a coluna que só existe em vendas ("Qtde Vendida", "Total Venda") decidem contra a
    tabela auxiliar: o arquivo segue para a leitura de vendas, como antes. Um filtro do topo ("Tabela de Preço:
    VAREJO", "Considerar estoque atual: Sim") num relatório de vendas não muda nada (revisão de 11/2026)."""
    if (_titulo_de_vendas(topo) or _titulo_de_compras(topo)
            or any(_RE_COLUNA_SO_DA_VENDA.search(titulo) for titulo in titulos if titulo)):
        return None
    titulo = _titulo_do_relatorio(topo)
    # A palavra de venda ou de compra no título ("Posição de estoque e vendas", "Estoque - Entradas de Mercadorias"):
    # a quantidade dele é a vendida ou a comprada, e não o saldo.
    if not titulo or _RE_TOPO_DE_VENDAS.search(titulo) or _RE_TITULO_DE_COMPRAS.search(titulo):
        return None
    # Uma linha de texto do topo, fora do título e dos parâmetros, que fala de venda ou de saída ("Relatório de
    # Estoque" com "Saídas por Produto" embaixo): o relatório é do movimento, e a quantidade dele é a vendida.
    if any(celula.rotulo is None and not celula.valor_de_filtro and _RE_VENDA_NO_TOPO.search(celula.texto)
           for celula in topo):
        return None
    if _RE_TITULO_DE_ESTOQUE_NO_TOPO.match(titulo):
        return "estoque"
    if _RE_TITULO_DE_PRECOS_NO_TOPO.match(titulo):
        return "precos"
    return None


def _colunas_de_venda(titulos: list) -> dict:
    """O que os títulos de coluna dariam na leitura de vendas (ALIAS_VENDAS com os títulos de reserva, como em
    _detectar_cabecalho): a quantidade, o total da venda, o produto."""
    return _mapear_cabecalho(titulos, ALIAS_VENDAS)


def _e_titulo_de_produto(titulo: str) -> bool:
    """O título de coluna (normalizado) é o do produto ("Produto", "Descrição", "Nome do Produto", "Item",
    "Mercadoria"), e não um total ou um agrupamento de produtos ("Total Produtos", "Grupo de Produtos")."""
    if not titulo:
        return False
    if titulo in _TITULOS_DE_PRODUTO_SOZINHOS:
        return True
    return (any(fragmento in titulo for fragmento in ALIAS_VENDAS["produto"])
            and not _RE_TITULO_DE_AGREGADO.search(titulo))


def _valor_sozinho_fora_do_mapa(titulos: list, mapa: dict) -> bool:
    """Alguma coluna que a tabela não leu tem o título do valor sem dizer de quê ("Valor", "Vlr", "Total")."""
    usadas = set(mapa.values())
    return any(titulo in _TITULOS_DE_VALOR_SOZINHO for coluna, titulo in enumerate(titulos) if coluna not in usadas)


def _documento_com_produto(titulos: list, mapa: dict) -> bool:
    """O resumo por documento não tem a coluna do produto: com ela ("Data;Nr. Pedido;Produto;Valor", "Cupom;Item;Vlr"),
    é a venda item a item que não foi lida, e a trava de produto, quantidade e valor de venda continua."""
    return any(_e_titulo_de_produto(titulo) for titulo in titulos)


def _estoque_pelo_titulo_com_quantidade_vendida(titulos: list, mapa: dict) -> bool:
    """Com o título de estoque, a coluna da quantidade que não é a do saldo ("Estoque Atual;Qtde;Valor Total") é a
    vendida: o relatório é a venda com o estoque ("Posição de Estoque e Giro"), e segue para a leitura de vendas, como
    antes. O preço da unidade e o total não decidem nada: são os da posição ou do inventário ("Qtde;Preço Venda",
    "Quantidade;Preço Unitário;Valor Total")."""
    vendas = _colunas_de_venda(titulos)
    return "quantidade" in vendas and vendas["quantidade"] != mapa.get("estoque")


def _valor_a_preco_de_venda(titulos: list, mapa: dict) -> bool:
    """O valor do estoque da tabela é a preço de venda, e não a custo: o título dele diz venda, ou a tabela traz o preço
    de venda da unidade e o título do valor não diz custo ("Qtde;Preço Venda;Valor Total"). Aí o custo não sai da
    divisão do valor pelo saldo: daria o preço de venda, e a margem zero."""
    coluna = mapa.get("valor_estoque")
    titulo_do_valor = titulos[coluna] if coluna is not None and coluna < len(titulos) else ""
    if "venda" in titulo_do_valor:
        return True
    return "custo" not in titulo_do_valor and any(_RE_PRECO_DE_VENDA.search(titulo)
                                                  for indice, titulo in enumerate(titulos)
                                                  if titulo and indice != coluna)


def _ler_tabela(bruto: pd.DataFrame, aliases: dict, reserva: dict, obrigatorios: tuple, *, chave: str, textos=(),
                numeros=(), preenchidos=(), numericas=(), titulo=None, sem_vendas=False, recusar_colunas=None,
                extras=None):
    """A tabela auxiliar lida (_TabelaLida), ou None quando o leiaute não casa. `titulo`: o papel que o título do
    relatório tem de anunciar (_papel_pelo_titulo). `sem_vendas`: o título de relatório de vendas no topo ou a coluna
    que só existe em vendas recusam a tabela (o relatório por produto sem uma das colunas não vira posição de estoque
    pelo "Estoque Atual": continua recusado). `recusar_colunas(titulos, mapa)` recusa a tabela pelos títulos de coluna
    (_colunas_de_venda). `extras(df, bruto, idx, linhas_cab, mapa)` acrescenta colunas antes do filtro das linhas."""
    try:
        bruto = _sem_titulos_espacados(bruto)
        idx, mapa, linhas_cab = _detectar_cabecalho_auxiliar(bruto, aliases, reserva,
                                                            lambda m: all(campo in m for campo in obrigatorios))
        topo = _celulas_do_topo(bruto, min(idx, _LINHAS_DO_TOPO))
        titulos = _titulos_auxiliares([bruto.iloc[idx + k].tolist() for k in range(linhas_cab)])
        if titulo is not None and _papel_pelo_titulo(topo, titulos) != titulo:
            return None
        if sem_vendas and (_titulo_de_vendas(topo)
                           or any(_RE_COLUNA_SO_DA_VENDA.search(texto) for texto in titulos if texto)):
            return None
        if recusar_colunas is not None and recusar_colunas(titulos, mapa):
            return None
        bruto = _alinhar_tabela_auxiliar(bruto, idx, linhas_cab, mapa, textos=textos, numeros=numeros,
                                         preenchidos=preenchidos)
        df = _construir_df_canonico(bruto, idx, mapa, linhas_cab)
        if extras is not None:
            extras(df, bruto, idx, linhas_cab, mapa)
        df = _filtrar_linhas_validas(df, chave, tuple(campo for campo in numericas if campo in df.columns))
    except ETLValidationError:
        return None
    return _TabelaLida(df, bruto, idx, linhas_cab, mapa, topo)


def _tabela_de_produtos(bruto: pd.DataFrame, aliases: dict, reserva: dict, obrigatorios: tuple, numericas: tuple,
                        titulo=None, recusar_colunas=None):
    """A tabela de produtos lida (_TabelaLida, com chave_nome, codigo e nome no quadro), ou None."""
    lida = _ler_tabela(bruto, aliases, reserva, obrigatorios, chave="produto", textos=("produto",),
                       numeros=tuple(campo for campo in obrigatorios if campo != "produto"), numericas=numericas,
                       titulo=titulo, sem_vendas=True, recusar_colunas=recusar_colunas)
    if lida is None:
        return None
    df = lida.df
    df["chave_nome"] = df["produto"].map(_normalizar_produto)
    df = df[(df["chave_nome"] != "") & (~df["chave_nome"].isin(_NAO_PRODUTOS))].reset_index(drop=True)
    if df.empty:
        return None
    df["codigo"] = df["codigo"].map(normalizar_codigo_do_produto) if "codigo" in df.columns else None
    df["nome"] = df["produto"].map(_nome_exibivel)
    return lida._replace(df=df)


def _loja_do_topo(topo: list):
    """A loja que o topo da tabela diz, normalizada, ou None: a filial (ou a loja, a unidade), com o depósito junto
    quando o topo traz ("002 loja / 01"). O valor vem na mesma célula do rótulo ("Filial: 001 - LOJA A") ou na seguinte
    ("Filial:" | "001 - LOJA A"). Sem a filial, ou com a filial "Todas" ou "Consolidado", a loja não é conhecida, e a
    tabela não soma com as outras (raio_x._saldo_das_tabelas_de_estoque): a empresa sozinha pode ser a de uma filial ou
    o total que a contém."""
    achados = {}
    for posicao, celula in enumerate(topo):
        if celula.rotulo is None:
            continue
        valor = celula.valor
        if not valor and posicao + 1 < len(topo) and topo[posicao + 1].valor_de_filtro:
            valor = topo[posicao + 1].texto
        if not valor:
            continue
        for tipo, padrao in (("filial", _RE_ROTULO_DA_FILIAL), ("deposito", _RE_ROTULO_DO_DEPOSITO)):
            if tipo not in achados and padrao.match(celula.rotulo):
                achados[tipo] = valor
    filial = achados.get("filial")
    if not filial or _RE_LOJA_CONSOLIDADA.search(filial):
        return None
    return " / ".join(parte for parte in (filial, achados.get("deposito")) if parte)


def _primeira_data(texto: str):
    """A primeira data dd/mm/aa(aa) válida do texto, ou None."""
    for grupos in _RE_DATA_BR.findall(texto):
        dia = _parse_data_br(grupos)
        if dia is not None:
            return dia
    return None


def _data_de_referencia(bruto: pd.DataFrame, idx: int):
    """A data da tabela de estoque, ou None, nesta ordem: o fim do período do cabeçalho ("Período : 11/11/2026"); a
    data de um rótulo de data no topo ("Data:", "Emitido em:", "Emissão:", "Posição em:"); a data sozinha numa célula
    (a emissão que o ERP imprime no canto); a data dentro de um texto ("Posição de estoque em 09/11/2026")."""
    periodo = _extrair_periodo_base(bruto)
    if periodo is not None:
        return periodo[1]
    rotuladas, soltas, em_texto = [], [], []
    for i in range(min(idx, _LINHAS_DO_TOPO)):
        for valor in bruto.iloc[i].tolist():
            if _celula_vazia(valor):
                continue
            texto = str(valor).strip()
            dois_pontos = _RE_DOIS_PONTOS_DO_ROTULO.search(texto)
            if dois_pontos is not None:
                if _RE_ROTULO_DE_DATA.search(_norm_chave(texto[:dois_pontos.start()])):
                    dia = _primeira_data(texto[dois_pontos.end():])
                    if dia is not None:
                        rotuladas.append(dia)
                continue
            dia = data_da_linha(texto)
            if dia is not None:
                soltas.append(dia)
            elif re.search(r"[A-Za-z]", texto):
                dia = _primeira_data(texto)
                if dia is not None:
                    em_texto.append(dia)
    for datas in (rotuladas, soltas, em_texto):
        if datas:
            return datas[0]
    return None


def _estoque_com_colunas_de_venda(titulos: list, mapa: dict) -> bool:
    """Sem o título de estoque (a leitura pelas colunas), a tabela com a quantidade ou o total da venda é o relatório de
    vendas que não foi lido, e não a posição de estoque: "Produto;Valor Total;Estoque" sem a quantidade continua
    recusado. O total com "estoque" no título ("Valor em Estoque") é o valor do estoque, e o total ao lado do custo
    unitário ("Saldo Atual;Custo Médio;Valor Total") também."""
    vendas = _colunas_de_venda(titulos)
    if "quantidade" in vendas and vendas["quantidade"] != mapa.get("estoque"):
        return True
    coluna = vendas.get("valor_venda")
    if coluna is not None and "estoque" not in titulos[coluna] and "custo_unitario" not in mapa:
        return True
    return _valor_sozinho_fora_do_mapa(titulos, mapa)


def _precos_com_colunas_de_venda(titulos: list, mapa: dict) -> bool:
    """A lista de preços não tem a quantidade nem o total da venda: com eles, é o relatório de vendas que não foi lido
    ("Codigo;Produto;Preço Unit.;Valor Total", "Codigo;Descricao;Preco Medio;Valor" sem a quantidade continuam
    recusados), mesmo com o título de preços."""
    vendas = _colunas_de_venda(titulos)
    return "quantidade" in vendas or "valor_venda" in vendas or _valor_sozinho_fora_do_mapa(titulos, mapa)


def _ler_posicao_de_estoque(bruto: pd.DataFrame, nome: str, *, pelo_titulo: bool = False):
    lida = _tabela_de_produtos(bruto, ALIAS_ESTOQUE, RESERVA_ESTOQUE_PELO_TITULO if pelo_titulo else RESERVA_ESTOQUE,
                               ("produto", "estoque"), ("estoque", "custo_unitario", "valor_estoque"),
                               titulo="estoque" if pelo_titulo else None,
                               # Com o título de estoque, só a quantidade vendida recusa a tabela: o preço da
                               # unidade e o total são os da posição ou do inventário.
                               recusar_colunas=(_estoque_pelo_titulo_com_quantidade_vendida if pelo_titulo
                                                else _estoque_com_colunas_de_venda))
    if lida is None:
        return None
    df = lida.df
    qualidade, csv = Counter(), bruto.attrs.get("format") == "csv"
    estoque = _converter_serie(df["estoque"])
    custo = (_converter_serie_monetaria(df["custo_unitario"], qualidade, csv) if "custo_unitario" in df.columns
             else pd.Series(np.nan, index=df.index))
    titulos = _titulos_auxiliares([lida.bruto.iloc[lida.idx + k].tolist() for k in range(lida.linhas_cab)])
    if "valor_estoque" in df.columns and not _valor_a_preco_de_venda(titulos, lida.mapa):
        valor = _converter_serie_monetaria(df["valor_estoque"], qualidade, csv)
        derivado = (valor / estoque).where(estoque > 0)
        custo = custo.where(custo > 0, derivado)
    produtos = pd.DataFrame({"chave_nome": df["chave_nome"], "codigo": df["codigo"], "nome": df["nome"],
                             "estoque": estoque, "custo_unitario": custo.where(custo > 0)})
    produtos = produtos[produtos["estoque"].notna()].reset_index(drop=True)
    if produtos.empty:
        return None  # nenhum saldo lido: a tabela não entra calada como se tivesse dado o estoque
    titulo = _titulo_do_relatorio(lida.topo) or ""
    inventario = bool(_RE_TITULO_DE_INVENTARIO.search(titulo))
    # A loja e a data dizem se duas tabelas de estoque somam (lojas diferentes) ou se uma vale (raio_x._combinar_auxiliares).
    return {"papel": "estoque", "rotulo": "inventário de estoque" if inventario else "posição de estoque",
            "artigo": "um" if inventario else "uma", "produtos": produtos, "documentos": None,
            "loja": _loja_do_topo(lida.topo), "data": _data_de_referencia(lida.bruto, lida.idx),
            "prioridade": 1 if inventario else 0}


def _ler_lista_de_precos(bruto: pd.DataFrame, nome: str, *, pelo_titulo: bool = False):
    lida = _tabela_de_produtos(bruto, ALIAS_PRECOS, RESERVA_PRECOS, ("produto", "preco"), ("preco", "custo_unitario"),
                               titulo="precos" if pelo_titulo else None,
                               recusar_colunas=_precos_com_colunas_de_venda)
    if lida is None:
        return None
    df = lida.df
    qualidade, csv = Counter(), bruto.attrs.get("format") == "csv"
    custo = (_converter_serie_monetaria(df["custo_unitario"], qualidade, csv) if "custo_unitario" in df.columns
             else pd.Series(np.nan, index=df.index))
    produtos = pd.DataFrame({"chave_nome": df["chave_nome"], "codigo": df["codigo"], "nome": df["nome"],
                             "estoque": np.nan, "custo_unitario": custo.where(custo > 0),
                             "preco": _converter_serie_monetaria(df["preco"], qualidade, csv)})
    return {"papel": "precos", "rotulo": "lista de preços", "artigo": "uma", "produtos": produtos, "documentos": None}


def _desconto_em_percentual(titulo: str, celulas: list) -> bool:
    """A coluna é o desconto em percentual ("Total Desc. %", "Desc. %", "% Desconto"): o título de desconto com o "%"
    numa das linhas do cabeçalho."""
    return (_RE_TITULO_DESCONTO.fullmatch(titulo) is not None
            and any(_titulo_de_percentual(celula) for celula in celulas if not _celula_vazia(celula)))


def _extras_do_documento(df: pd.DataFrame, bruto: pd.DataFrame, idx: int, linhas_cab: int, mapa: dict) -> None:
    """O desconto (em reais, ou o percentual), o cliente e a hora de cada documento, pelos títulos da venda item a
    item (_dado_da_venda), em colunas com "_"."""
    usadas = set(mapa.values())
    partes = [bruto.iloc[idx + k].tolist() for k in range(linhas_cab)]
    titulos = _titulos_auxiliares(partes)
    bloco = bruto.iloc[idx + linhas_cab:].reset_index(drop=True)
    extras = {}
    for coluna, titulo in enumerate(titulos):
        if coluna in usadas or not titulo:
            continue
        celulas = [parte[coluna] for parte in partes if coluna < len(parte)]
        dado = _dado_da_venda(titulo, celulas)
        if dado is None and _desconto_em_percentual(titulo, celulas):
            dado = "desconto_percentual"
        if dado in ("desconto", "desconto_percentual", "hora", "cliente", "cliente_sem_titulo"):
            extras.setdefault("cliente" if dado.startswith("cliente") else dado, coluna)
    for chave, coluna in extras.items():
        df["_" + chave] = bloco.iloc[:, coluna].tolist()


def _ler_resumo_por_documento(bruto: pd.DataFrame, nome: str):
    from indicadores_executivos import numero_da_venda  # o módulo importa este: aqui, na hora da chamada
    # O resumo por documento não tem a coluna do produto (_documento_com_produto): com ela, é a venda item a item que
    # não foi lida, e a trava de produto, quantidade e valor de venda continua.
    lida = _ler_tabela(bruto, ALIAS_DOCUMENTOS, RESERVA_DOCUMENTOS, ("documento", "valor"), chave="documento",
                       numeros=("valor",), preenchidos=("documento",), numericas=("valor",),
                       recusar_colunas=_documento_com_produto, extras=_extras_do_documento)
    if lida is None:
        return None
    df = lida.df
    numeros = df["documento"].map(numero_da_venda).map(normalizar_numero_do_documento)
    documentos = pd.DataFrame({"numero": numeros})
    csv = bruto.attrs.get("format") == "csv"
    # O desconto que não dá para ler fica sem coluna (o índice de descontos pede a coluna), e não zerado.
    if "_desconto" in df.columns:
        qualidade = Counter()
        desconto = _converter_serie_monetaria(df["_desconto"], qualidade, csv)
        if not qualidade.get("invalidos"):
            documentos["desconto"] = desconto.abs()
    elif "_desconto_percentual" in df.columns:
        # O percentual sobre o valor bruto, com o valor do documento já líquido: desconto = valor x p / (100 - p). Com
        # todo percentual até 1, ele pode estar em fração (o 20% gravado como 0,2 numa célula de porcentagem do XLSX):
        # sem como saber, o desconto fica de fora, e não 100 vezes menor.
        qualidade = Counter()
        percentual = _converter_serie_monetaria(df["_desconto_percentual"], qualidade, csv)
        valor = _converter_serie_monetaria(df["valor"], qualidade, csv).abs()
        valido = (percentual >= 0) & (percentual < 100)
        em_fracao = bool((percentual > 0).any()) and float(percentual.max()) <= 1.0
        if not qualidade.get("invalidos") and not em_fracao:
            documentos["desconto"] = (valor * percentual / (100 - percentual)).where(valido)
    for chave in ("cliente", "hora"):
        if "_" + chave in df.columns:
            documentos[chave] = df["_" + chave].tolist()
    documentos = documentos.dropna(subset=["numero"])
    if documentos.empty:
        return None
    # Um pedido em mais de uma linha: soma o desconto e fica com o primeiro cliente e a primeira hora.
    agregados = {coluna: ("sum" if coluna == "desconto" else "first") for coluna in documentos.columns if coluna != "numero"}
    documentos = documentos.groupby("numero", sort=False).agg(agregados) if agregados else (
        documentos.drop_duplicates("numero").set_index("numero"))
    return {"papel": "documentos", "rotulo": "resumo por pedido ou documento", "artigo": "um", "produtos": None,
            "documentos": documentos}


def ler_tabela_pelo_titulo(bruto: pd.DataFrame, nome: str):
    """A classificação prévia do lote: a tabela auxiliar que o TÍTULO do relatório anuncia (posição de estoque,
    inventário ou lista de preços), lida antes da leitura de vendas, ou None. O título é o do topo, acima dos títulos
    de coluna; o relatório de vendas no topo e a coluna que só existe em vendas decidem por vendas
    (_papel_pelo_titulo). Com o título de estoque, "Qtde" e "Quantidade" são o saldo (RESERVA_ESTOQUE_PELO_TITULO):
    a posição de estoque com "Qtde" e "Valor Total" não é mais lida como venda."""
    if bruto.empty:
        return None
    for leitor in (_ler_posicao_de_estoque, _ler_lista_de_precos):
        tabela = leitor(bruto, nome, pelo_titulo=True)
        if tabela is not None:
            return tabela
    return None


def ler_tabela_auxiliar(bruto: pd.DataFrame, nome: str):
    """A tabela auxiliar do lote que o arquivo traz pelas colunas ({"papel", "rotulo", "artigo", "produtos",
    "documentos"}; a de estoque também com "loja", "data" e "prioridade"), ou None quando ele não é nenhuma delas: é a
    leitura depois de a de vendas falhar. A ordem segue o título do relatório (posição de estoque, lista de preços) e,
    sem título, tenta o estoque, os preços e o resumo por documento."""
    if bruto.empty:
        return None
    topo = " | ".join(celula.texto for celula in _celulas_do_topo(bruto, _LINHAS_DO_TOPO))
    leitores = [_ler_posicao_de_estoque, _ler_lista_de_precos, _ler_resumo_por_documento]
    if _RE_TITULO_DE_PRECOS.search(topo) and not _RE_TITULO_DE_ESTOQUE.search(topo):
        leitores = [_ler_lista_de_precos, _ler_posicao_de_estoque, _ler_resumo_por_documento]
    for leitor in leitores:
        tabela = leitor(bruto, nome)
        if tabela is not None:
            return tabela
    return None


def _linha_de_titulos(linha: list, minimo: int = 2) -> bool:
    """A linha pode ser de títulos de coluna: `minimo` células ou mais, nenhuma só número e nenhuma com rótulo de
    dois-pontos (a linha de dado e a de parâmetro não são)."""
    preenchidas = [valor for valor in linha if not _celula_vazia(valor)]
    return (len(preenchidas) >= minimo and not any(_so_numero(valor) for valor in preenchidas)
            and not any(_celula_de_rotulo(valor) for valor in preenchidas))


def _tem_coluna_de_produto(bruto: pd.DataFrame, janela: int = 100) -> bool:
    """Alguma linha de títulos (_linha_de_titulos, duas células ou mais), sozinha ou com a de baixo quando ela também só
    tem texto, traz o título do produto (_e_titulo_de_produto): é o relatório por produto, mesmo que a leitura não tenha
    dado certo, com qualquer título do valor ("Valor", "Total") e também com só duas colunas ("Produto;Valor Total")."""
    limite = min(janela, len(bruto))
    linhas = [bruto.iloc[i].tolist() for i in range(limite)]
    for i in range(limite):
        if not _linha_de_titulos(linhas[i]):
            continue
        partes = [linhas[i]]
        if i + 1 < limite and _linha_de_titulos(linhas[i + 1], minimo=1):
            partes.append(linhas[i + 1])
        titulos = _titulos_auxiliares(partes) + [_norm_chave(valor) for valor in linhas[i] if not _celula_vazia(valor)]
        if any(_e_titulo_de_produto(titulo) for titulo in titulos):
            return True
    return False


def resumo_de_vendas_sem_produto(bruto: pd.DataFrame) -> bool:
    """O relatório é um resumo de vendas por outra coisa que não o produto ("Giro de vendas por fornecedor", "por grupo
    de clientes", "por grupo de produtos", "por forma de pagamento", "Por cliente - Ranking"): o título de vendas do
    topo diz pelo quê, e nenhuma linha de títulos traz a coluna do produto (_tem_coluna_de_produto). O relatório por
    produto que não foi lido (sem uma das colunas, corrompido) não é: continua recusado."""
    if bruto.empty:
        return False
    topo = _celulas_do_topo(bruto, _LINHAS_DO_TOPO)
    if not any(_RE_TITULO_DE_RESUMO_SEM_PRODUTO.search(celula.texto) and not _RE_TITULO_POR_PRODUTO.search(celula.texto)
               for celula in topo if not celula.valor_de_filtro):
        return False
    return not _tem_coluna_de_produto(bruto)


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
    _recusar_separador_duvidoso(bruto, idx, linhas_cab, nome)
    _recusar_relatorio_de_vendas_nas_compras(bruto, idx, linhas_cab, nome)
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


def data_da_linha(valor) -> "datetime.date | None":
    """A data de uma linha de venda: a data da planilha ("2023-11-09 00:00:00") ou o texto dd/mm/aa ou
    dd/mm/aaaa, com ou sem hora depois. Qualquer outra coisa não é data (None)."""
    if valor is None:
        return None
    texto = str(valor).strip()
    if len(texto) >= 10 and texto[4] == "-" and texto[7] == "-":
        try:
            return datetime.strptime(texto[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    achado = _RE_DATA_BR.match(texto)
    if achado is None or (achado.end() < len(texto) and not texto[achado.end()].isspace()):
        return None
    return _parse_data_br(achado.groups())


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
