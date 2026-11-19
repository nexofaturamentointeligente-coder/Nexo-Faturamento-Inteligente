"""
NEXO - Teste isolado do motor de ETL
====================================
Valida o pipeline de ETL SEM Flask e SEM banco de dados: lê os arquivos reais
do PDV, calcula os KPIs em memória e imprime tudo que o ADMIN precisa conferir.

Uso (PowerShell):
    python test_etl.py .\\dados\\Vendas_2025.xlsx .\\dados\\Compras_2025.xlsx 1

O 3º argumento (id_analise) é opcional e meramente informativo aqui — este
teste NÃO persiste nada no banco, apenas exercita parsing, sanitização e cálculo.
"""

import sys
from pathlib import Path

# Console do Windows costuma usar cp1252; força UTF-8 para exibir acentos corretamente.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etl_processor import calcular_kpis_em_memoria, ETLValidationError


def _fmt_moeda(v) -> str:
    return "-" if v is None else f"R$ {v:,.2f}"


def _fmt_num(v) -> str:
    return "-" if v is None else f"{v:g}"


def rodar_teste() -> int:
    if len(sys.argv) < 3:
        print("Uso: python test_etl.py <caminho_vendas> <caminho_compras> [id_analise]")
        return 1

    caminho_vendas = sys.argv[1]
    caminho_compras = sys.argv[2]
    id_analise = sys.argv[3] if len(sys.argv) > 3 else "(não informado)"

    print("=" * 64)
    print("NEXO - Teste isolado do motor de ETL (sem Flask / sem banco)")
    print("=" * 64)
    print(f"Arquivo de VENDAS  recebido : {caminho_vendas}")
    print(f"Arquivo de COMPRAS recebido : {caminho_compras}")
    print(f"id_analise (informativo)    : {id_analise}")
    print("-" * 64)

    try:
        r = calcular_kpis_em_memoria(caminho_vendas, caminho_compras)
    except ETLValidationError as e:
        print("\n[X] FALHA DE VALIDACAO (dados/arquivo):")
        print(f"    {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        print("\n[X] ERRO INESPERADO no motor de ETL:")
        print(f"    {type(e).__name__}: {e}")
        return 3

    print("\nColunas encontradas em VENDAS (apos sanitizacao):")
    print(f"    {r.telemetria['colunas_vendas']}")
    print("Colunas encontradas em COMPRAS (apos sanitizacao):")
    print(f"    {r.telemetria['colunas_compras']}")
    print(f"Coluna de valor usada em COMPRAS: {r.telemetria['coluna_valor_compras']}")
    print(f"Linhas validas -> vendas: {r.telemetria['linhas_vendas']} | compras: {r.telemetria['linhas_compras']}")

    k = r.kpis
    print("\n" + "-" * 64)
    print("KPIs OFICIAIS (persistidos em indicador_analise):")
    print("-" * 64)
    print(f"  Faturamento total ................... {_fmt_moeda(k['faturamento_total'])}")
    print(f"  Total comprado (notas) .............. {_fmt_moeda(k['total_comprado'])}")
    print(f"  Indicador de Pressao de Estoque ..... {_fmt_moeda(k['saldo_estimado_compras_vendas'])}")
    print( "     (saldo estimado = total comprado - faturamento; descasamento compras x vendas)")
    print(f"  Produto mais vendido (qtd) .......... {k['produto_mais_vendido_nome']} ({_fmt_num(k['produto_mais_vendido_quantidade'])} un)")
    print(f"  Produto maior faturamento ........... {k['produto_maior_faturamento_nome']} ({_fmt_moeda(k['produto_maior_faturamento_valor'])})")
    print(f"  Produto maior saldo estimado parado . {k['produto_maior_saldo_parado_nome'] or '- (nao calculado)'}")
    print(f"  Saldo estimado parado (unidades) .... {_fmt_num(k['saldo_estimado_parado'])}")

    print("\n" + "-" * 64)
    print("ALERTAS / TELEMETRIA (em memoria, NAO persistidos):")
    print("-" * 64)
    if r.alertas:
        for a in r.alertas:
            print(f"  - {a}")
    else:
        print("  (nenhum alerta)")

    print("\n[OK] Calculo concluido. Nenhuma linha foi persistida (teste isolado).")
    return 0


if __name__ == "__main__":
    sys.exit(rodar_teste())
