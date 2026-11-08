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
def _parse_valor(bruto):
    """
    Aceita 'R$ 1.234,56', '1.234,56', '1234,56', '1,234.56', '1234.56',
    '(123,45)', ' - ', '', None — retorna float ou None se irrecuperável.

    Só para quantidades e estoque: aqui o ponto único é decimal, porque "1.500" numa
    coluna de quantidade costuma ser 1,5 kg. Dinheiro usa converter_valor_monetario.
    """
    if bruto is None:
        return None
    s = str(bruto).strip()
    # O apóstrofo da neutralização de fórmula (security_gatekeeper) não é sinal nem dígito:
    # sem tirá-lo, "'-2 un(s)" seria lido como +2 e a devolução viraria venda. O dinheiro já
    # o ignora (_limpar_monetario).
    if s.startswith("'"):
        s = s[1:].strip()
    if s.lower() in ("", "nan", "none", "-", "--"):
        return None

    s = s.replace("\xa0", "").replace(" ", "")
    s = re.sub(r"(?i)r\$", "", s)
    negativo = s.startswith("-") or s.startswith("(")
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
_RE_DECIMAL = re.compile(r"[0-9]+[.,]?[0-9]*|[.,][0-9]+")
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


def _normalizar_separadores(corpo: str, ponto_decimal: bool):
    """Texto aceito por float(), ou None se os separadores não formam um número."""
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
    return corpo.replace(",", ".") if _RE_DECIMAL.fullmatch(corpo) else None


def _interpretar_monetario(valor, ponto_decimal: bool = False):
    """Float finito, ou None quando o valor é nulo ou inválido."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (numbers.Real, Decimal)):
        numero = float(valor)
        return numero if math.isfinite(numero) else None
    negativo, corpo = _separar_sinal(_limpar_monetario(valor))
    texto = _normalizar_separadores(corpo, ponto_decimal)
    if texto is None:
        return None
    numero = float(texto)
    if not math.isfinite(numero):
        return None
    return -numero if (negativo and numero) else numero


def converter_valor_monetario(valor, ponto_decimal: bool = False) -> float:
    """
    Converte um valor em dinheiro para float, sem nunca interromper o processamento:
    nulo ou inválido vira 0.0.

      "R$ 12.500,50" -> 12500.5     "12.500" -> 12500.0     "1.234.567" -> 1234567.0
      "1,234.56"     -> 1234.56     "12,5"   -> 12.5        "0.125"     -> 0.125
      "(10,00)"      -> -10.0       "10,00-" -> -10.0       "abc"       -> 0.0

    `ponto_decimal=True` lê "12.500" como 12.5, para colunas que mostram usar o ponto
    como decimal (ver _coluna_usa_ponto_decimal). Um valor sozinho não tem como revelar isso.
    """
    numero = _interpretar_monetario(valor, ponto_decimal)
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


def _converter_serie_monetaria(serie: pd.Series, qualidade: Counter) -> pd.Series:
    """Converte uma coluna em dinheiro e anota em `qualidade` o que pede revisão humana."""
    corpos = [_separar_sinal(_limpar_monetario(v))[1] for v in serie]
    ponto_decimal = _coluna_usa_ponto_decimal(corpos)
    valores = []
    for bruto, corpo in zip(serie, corpos):
        numero = _interpretar_monetario(bruto, ponto_decimal)
        if numero is None:
            if corpo.lower() not in _VALORES_EM_BRANCO:
                qualidade["invalidos"] += 1
            numero = 0.0
        elif corpo.count(".") == 1 and _RE_MILHAR_PONTO.fullmatch(corpo):
            qualidade["lidos_como_decimal" if ponto_decimal else "lidos_como_milhar"] += 1
        valores.append(numero)
    return pd.Series(valores, index=serie.index, dtype="float64")


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


def _mapear_cabecalho(linha_norm: list, aliases: dict) -> dict:
    """
    Para cada COLUNA da linha (já normalizada), encontra o 1º canônico cujo
    fragmento aparece como substring. Cada canônico é claimed no máximo 1x
    (1ª coluna vence — proteção contra duplicatas no relatório).
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
    """
    limite = min(janela, len(df_bruto))
    melhor = None
    melhor_parcial: dict = {}   # melhor mapeamento visto, mesmo sem passar na validação

    # 1) cabeçalho de 1 linha
    for i in range(limite):
        linha = [_norm_chave(v) for v in df_bruto.iloc[i].tolist()]
        mapa = _mapear_cabecalho(linha, aliases)
        if len(mapa) > len(melhor_parcial):
            melhor_parcial = mapa
        if valida_fn(mapa) and (melhor is None or len(mapa) > len(melhor[1])):
            melhor = (i, mapa, 1)
    if melhor is not None:
        return melhor

    # 2) cabeçalho composto (2 linhas)
    for i in range(limite - 1):
        l1 = [_norm_chave(v) for v in df_bruto.iloc[i].tolist()]
        l2 = [_norm_chave(v) for v in df_bruto.iloc[i + 1].tolist()]
        # zip por menor comprimento — colunas extras ficam de fora
        comb = [(a + " " + b).strip() for a, b in zip(l1, l2)]
        mapa = _mapear_cabecalho(comb, aliases)
        if len(mapa) > len(melhor_parcial):
            melhor_parcial = mapa
        if valida_fn(mapa) and (melhor is None or len(mapa) > len(melhor[1])):
            melhor = (i, mapa, 2)
    if melhor is not None:
        return melhor

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
# Linhas-rodapé / marca d'água típicas de PDV (regex case-insensitive).
_PADRAO_LIXO = re.compile(
    r"\b(?:total geral|totais|subtotal|movimenta|labsofti|software|"
    r"resumo|periodo|usuario|filtro|impresso|pagina)\b",
    re.IGNORECASE,
)

# Strings que, se aparecerem como valor da coluna-chave, indicam que aquela
# linha é um cabeçalho REPETIDO (quebra de página do PDV) — devem ser puladas.
_CABECALHOS_REPETIDOS = {
    "codigo", "produto", "nome do produto", "descricao",
    "data", "data emissao", "data nf", "fornecedor",
    "valor nf", "vr total", "total nf",
}


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


def _filtrar_linhas_validas(df: pd.DataFrame, chave: str) -> pd.DataFrame:
    """
    Descarta linhas onde a coluna-chave está vazia, é rodapé/marca d'água, OU
    é um cabeçalho reimpresso (quebra de página típica do PDV).
    """
    if chave not in df.columns:
        raise ETLValidationError(f"Coluna-chave '{chave}' ausente após o mapeamento.")
    bruto = df[chave].map(lambda v: "" if v is None else str(v).strip())
    valido = bruto.map(lambda v: v.lower() not in ("", "nan", "none"))
    nao_rodape = ~bruto.str.contains(_PADRAO_LIXO, na=False)
    nao_cabec = ~bruto.map(_norm_chave).isin(_CABECALHOS_REPETIDOS)
    return df[valido & nao_rodape & nao_cabec].reset_index(drop=True)


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


def _situacao_das_linhas(df_bruto: pd.DataFrame, idx_cab: int, mapa: dict, linhas_cab: int):
    """Texto normalizado das colunas de status/operação de cada linha; None se não houver."""
    titulos = [[_norm_chave(v) for v in df_bruto.iloc[i].tolist()]
               for i in range(idx_cab, idx_cab + linhas_cab)]
    usadas = set(mapa.values())
    colunas = []
    for indice, partes in enumerate(zip(*titulos)):
        chave = " ".join(partes).strip()
        if (indice not in usadas and _RE_COLUNA_SITUACAO.search(chave)
                and not _RE_COLUNA_DE_OUTRO_ASSUNTO.search(chave)):
            colunas.append(indice)
    if not colunas:
        return None
    bloco = df_bruto.iloc[idx_cab + linhas_cab:, colunas]
    return [" ".join(_norm_chave(v).upper() for v in linha if not pd.isna(v))
            for linha in bloco.itertuples(index=False, name=None)]


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


def _vendas_do_bruto(bruto: pd.DataFrame, nome: str) -> pd.DataFrame:
    """carregar_vendas a partir da matriz já lida: o Raio-X lê cada arquivo uma vez só e
    tira dela também o período do cabeçalho (_extrair_periodo_base)."""
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
    df = _filtrar_linhas_validas(df, "produto")

    # Conversões numéricas — NaN vira 0 (linha sem qtd/valor não impacta KPI)
    qualidade = Counter()
    df["quantidade"] = _converter_serie(df["quantidade"]).fillna(0.0)
    if "valor_venda" in df.columns:
        df["valor_venda"] = _converter_serie_monetaria(df["valor_venda"], qualidade)
    elif "preco_unitario" in df.columns:
        # Sem coluna de total: deriva o faturamento da linha (qtd x preço unitário).
        # Cálculo honesto a partir de colunas reais — não é dado inventado.
        df["valor_venda"] = _converter_serie_monetaria(df["preco_unitario"], qualidade) * df["quantidade"]
        logger.info("VENDAS '%s': faturamento derivado de quantidade x preço unitário.", nome)
    else:
        raise ETLValidationError(
            f"Relatório de VENDAS '{nome}' sem coluna de valor (nem total, nem preço unitário)."
        )
    if "valor_custo" in df.columns:
        df["valor_custo"] = _converter_serie_monetaria(df["valor_custo"], qualidade)
    if "estoque" in df.columns:
        df["estoque"] = _converter_serie(df["estoque"]).fillna(0.0)

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
    df.attrs["qualidade"] = dict(qualidade)
    df.attrs["cabecalhos"] = cabecalhos
    return df


def carregar_compras(caminho: str):
    """
    Retorna (df_compras, coluna_valor_usada).
    `coluna_valor_usada` ∈ {"produtos_total", "valor_nf"} — produtos_total
    tem preferência (valor "puro" dos itens; valor_nf inclui frete/imposto).
    """
    nome = os.path.basename(caminho)
    bruto = _ler_bruto(caminho)
    if bruto.empty:
        raise ETLValidationError(f"O arquivo de compras '{nome}' está totalmente vazio.")

    idx, mapa, linhas_cab = _detectar_cabecalho(bruto, ALIAS_COMPRAS, _validar_compras)
    df = _construir_df_canonico(bruto, idx, mapa, linhas_cab)
    # Chave de validade de linha: fornecedor (NF-level) ou, na ausência dele,
    # produto (item-level) — alinhado ao validador.
    chave_linha = "fornecedor" if "fornecedor" in df.columns else "produto"
    df = _filtrar_linhas_validas(df, chave_linha)

    qualidade = Counter()
    if "produtos_total" in df.columns:
        coluna_valor = "produtos_total"
        df[coluna_valor] = _converter_serie_monetaria(df[coluna_valor], qualidade)
    elif "valor_nf" in df.columns:
        coluna_valor = "valor_nf"
        df[coluna_valor] = _converter_serie_monetaria(df[coluna_valor], qualidade)
    elif "custo_unitario" in df.columns and "qtde_comprada" in df.columns:
        # Sem coluna de total: deriva o total da linha (qtd x custo unitário).
        # Cálculo honesto a partir de colunas reais — não é dado inventado.
        qtd = _converter_serie(df["qtde_comprada"]).fillna(0.0)
        custo = _converter_serie_monetaria(df["custo_unitario"], qualidade)
        coluna_valor = "valor_total_compra"
        df[coluna_valor] = qtd * custo
        logger.info("COMPRAS '%s': total derivado de quantidade x custo unitário.", nome)
    else:
        raise ETLValidationError(
            f"Relatório de COMPRAS '{nome}' sem coluna de valor reconhecida "
            "(total de NF/compra/custo, nem custo unitário + quantidade)."
        )
    df = df[df[coluna_valor] > 0].reset_index(drop=True)

    if "qtde_comprada" in df.columns:
        df["qtde_comprada"] = _converter_serie(df["qtde_comprada"]).fillna(0.0)
    if "produto" in df.columns:
        df["_prod_norm"] = df["produto"].map(_normalizar_produto)
    df.attrs["qualidade"] = dict(qualidade)
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
