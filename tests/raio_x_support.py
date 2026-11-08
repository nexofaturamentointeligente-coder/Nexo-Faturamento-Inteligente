"""Relatórios fictícios e o executor do Raio-X no próprio processo, para os testes.

Sem import do app nem de config: o ensaio PostgreSQL importa este módulo antes de montar
o app com o ambiente filtrado. O executor faz o que etl_worker faz na operação "raio_x"
(validação de cada arquivo, cálculo e ida e volta em JSON), porque o worker real exige o
módulo `resource`, que só existe em Linux.
"""

import json
from pathlib import Path

JANEIRO = """Relatorio de vendas por produto
Periodo: 01/01/2026 a 31/01/2026
Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque
1;Cafe 500g;10;20,00;180,00;150,00;5
2;Acucar 1kg;5;6,00;30,00;;3
3;Leite 1L;20;5,00;100,00;110,00;-2
3;Leite Integral;2;5,00;10,00;9,00;1
""".encode("utf-8")

FEVEREIRO = """Relatorio de vendas por produto
Periodo: 01/02/2026 a 28/02/2026
Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque
1;Cafe 500g;10;20,00;200,00;180,00;5
2;Acucar 1kg;5;6,00;30,00;20,00;3
3;Leite 1L;20;5,00;100,00;90,00;4
""".encode("utf-8")


def relatorio_do_mes(ano, mes, linhas):
    """Relatório de um mês, com o período no cabeçalho como o PDV exporta."""
    from calendar import monthrange
    fim = monthrange(ano, mes)[1]
    cabecalho = (f"Relatorio de vendas por produto\nPeriodo: 01/{mes:02d}/{ano} a {fim:02d}/{mes:02d}/{ano}\n"
                 "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n")
    return (cabecalho + "".join(linha + "\n" for linha in linhas)).encode("utf-8")


def executar_como_o_worker(caminhos, limits=None):
    """etl_runtime.run_raio_x sem o processo filho: mesma validação, cálculo e serialização."""
    import raio_x
    from etl_inputs import validate_input
    from etl_limits import get_limits, using_limits
    politica = get_limits() if limits is None else limits
    with using_limits(politica):
        for caminho in caminhos:
            validate_input(caminho, Path(caminho).suffix.lower(), politica)
        resultado = raio_x.diagnosticar(caminhos)
    return json.loads(json.dumps(resultado, ensure_ascii=True, allow_nan=False))


def avaliar_como_o_worker(caminhos, limits=None, *, hoje=None):
    """etl_runtime.run_avaliacao_cenario sem o processo filho: a mesma validação, o cálculo, a
    serialização e a conferência do contrato que o processo pai faz (etl_runtime.validar_avaliacao).
    `hoje` fixa a data da regra do ano e da cobertura (o worker usa a data do dia)."""
    import avaliacao_motor
    import etl_runtime
    from etl_inputs import validate_input
    from etl_limits import get_limits, using_limits
    politica = get_limits() if limits is None else limits
    with using_limits(politica):
        for caminho in caminhos:
            validate_input(caminho, Path(caminho).suffix.lower(), politica)
        resultado = avaliacao_motor.avaliar_cenario(caminhos, hoje=hoje)
    return etl_runtime.validar_avaliacao(json.loads(json.dumps(resultado, ensure_ascii=True, allow_nan=False)))
