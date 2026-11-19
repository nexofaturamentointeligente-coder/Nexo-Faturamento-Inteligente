"""A forma do nó de indicadores executivos e a conferência do processo pai, só com a biblioteca padrão.

indicadores_executivos.py (o cálculo, com pandas e numpy) roda no processo isolado do ETL, dentro do
Raio-X. O processo pai confere o nó inteiro antes de gravar (valido) e as telas leem a margem mínima sem
carregar pandas. indicadores_executivos importa estes nomes daqui (uma só fonte) e continua a expô-los.
"""

from __future__ import annotations

import math
import re

VERSAO = "indicadores-2026-11.2"
CHAVES = (
    "faturamento", "ticket_medio", "margem", "giro_estoque", "mix", "itens_por_venda", "recompra", "saving",
    "evolucao_custo", "cmv", "curva_abc_faturamento", "curva_abc_volume", "curva_abc_lucro", "margem_negativa",
    "margem_abaixo_minimo", "indice_descontos", "ruptura", "encalhados", "cobertura_estoque", "gmroi", "mapa_calor",
)
STATUS = frozenset({
    "calculado", "sem_dados", "requer_item_a_item", "requer_custo", "requer_estoque", "requer_cpf_cnpj",
    "requer_parametro", "requer_relatorio_de_compras", "requer_mais_de_um_mes", "requer_periodo",
    "requer_coluna_de_desconto", "requer_cadastro_de_produtos",
})
MODOS = frozenset({"analitico", "acumulado"})
MARGEM_MINIMA = 0.15
LIMITE_NOME = 120
LIMITE_CODIGO = 60


# ---------------------------------------------------------------------------
# Conferência no processo pai (contrato)
# ---------------------------------------------------------------------------
_RE_CHAVE = re.compile(r"[a-z0-9_]{1,40}")
_RE_ROTULO = re.compile(r"[A-Za-z0-9_.-]{1,40}")
# Só estes campos levam texto do relatório (nome e código do produto); os demais textos são rótulos fixos.
_TEXTO_DO_RELATORIO = {"produto": LIMITE_NOME, "codigo": LIMITE_CODIGO}
_MAXIMO_DE_NOS = 250_000
_MAXIMO_DA_LISTA = 3_000
_MAIOR_NUMERO = 1e15


def valido(no) -> bool:
    """O nó tem a forma que calcular devolve: chaves fixas, os 21 indicadores com status conhecido,
    números finitos, rótulos curtos e texto livre só no nome e no código do produto. Um processo isolado
    com defeito (ou comprometido) não grava texto qualquer. Qualquer exceção na conferência também é
    recusa (False), e não erro 500."""
    try:
        return _valido(no)
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        return False


def _valido(no) -> bool:
    if not isinstance(no, dict) or set(no) != {"versao", "modo", "parametros", "indicadores"}:
        return False
    if (no["versao"] != VERSAO or not isinstance(no["modo"], str) or no["modo"] not in MODOS
            or not isinstance(no["parametros"], dict)):
        return False
    indicadores = no["indicadores"]
    if not isinstance(indicadores, dict) or set(indicadores) != set(CHAVES):
        return False
    if not all(isinstance(valor, dict) and isinstance(valor.get("status"), str) and valor["status"] in STATUS
               for valor in indicadores.values()):
        return False
    return _seguro(no, 0, [0], None)


def _seguro(valor, profundidade: int, contador: list, chave) -> bool:
    contador[0] += 1
    if contador[0] > _MAXIMO_DE_NOS or profundidade > 8:
        return False
    if valor is None or type(valor) is bool:
        return True
    if type(valor) is int:
        return abs(valor) <= 2 ** 53
    if type(valor) is float:
        return math.isfinite(valor) and abs(valor) < _MAIOR_NUMERO
    if isinstance(valor, str):
        if chave in _TEXTO_DO_RELATORIO:
            return (0 < len(valor) <= _TEXTO_DO_RELATORIO[chave] and bool(valor.strip())
                    and all(caractere.isprintable() for caractere in valor))
        return _RE_ROTULO.fullmatch(valor) is not None
    if isinstance(valor, list):
        return len(valor) <= _MAXIMO_DA_LISTA and all(_seguro(item, profundidade + 1, contador, chave) for item in valor)
    if isinstance(valor, dict):
        return all(isinstance(nome, str) and _RE_CHAVE.fullmatch(nome) is not None
                   and _seguro(item, profundidade + 1, contador, nome) for nome, item in valor.items())
    return False
