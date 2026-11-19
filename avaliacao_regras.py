"""O contrato da Avaliação de Cenário que o processo pai usa, só com a biblioteca padrão.

avaliacao_motor.py (o cálculo, com pandas) roda no processo isolado do ETL. O processo pai confere o
resultado (etl_runtime.validar_avaliacao) e grava as linhas (avaliacao_cenario) com estas versões, chaves,
colunas e tetos, sem carregar pandas. avaliacao_motor importa estes nomes daqui (uma só fonte) e continua a
expô-los.
"""

VERSAO_REGRAS_LEAD = "lead-2026-11.4"
ANO_MINIMO = 2000
ANO_MAXIMO_DO_BANCO = 2100  # ck_brainstorming_dados_pdv_ano
CHAVES_RESULTADO = frozenset({"versao", "raio_x", "linhas", "meta"})
# A meta fica no lead depois do expurgo das linhas: só contagens, com chaves fixas, e nenhum texto
# livre. O processo pai confere exatamente estas chaves (etl_runtime.validar_avaliacao).
CHAVES_META = frozenset({
    "versao_regras", "versao_raio_x", "arquivos", "arquivos_por_ano", "dias_por_ano",
    "relatorios_mensais_por_ano", "arquivos_com_custo", "linhas_lidas", "qualidade", "situacao",
    "celulas_recodificadas",
    # Etapa 2: cobertura de cada verificação por ano e os ajustes de produto sem venda no ano.
    "relatorios_com_custo_por_ano", "relatorios_com_ncm_por_ano", "relatorios_com_categoria_por_ano",
    "relatorios_com_estoque_por_ano", "relatorios_completos_por_ano", "dias_mesmos_meses_por_ano",
    "relatorios_cabecalho_ambiguo", "ajustes_sem_venda_por_ano",
    # Etapa 3: os períodos cobertos do envio (datas, sem texto) e os relatórios com várias lojas.
    "periodos_cobertos", "relatorios_varias_unidades",
})
# Contagens de relatórios por ano (inteiros de 0 ao número de relatórios do ano).
CHAVES_RELATORIOS_POR_ANO = (
    "relatorios_com_custo_por_ano", "relatorios_com_ncm_por_ano", "relatorios_com_categoria_por_ano",
    "relatorios_com_estoque_por_ano", "relatorios_completos_por_ano",
)
CHAVES_AJUSTES = ("custo_bonificacoes", "custo_devolucoes", "devolucoes_com_custo")
# Contadores de qualidade dos valores em dinheiro (etl_processor._converter_serie_monetaria).
CHAVES_QUALIDADE = frozenset({
    "invalidos", "lidos_como_milhar", "lidos_como_decimal", "virgula_como_decimal", "virgula_como_milhar",
})
# Situação das vendas (etl_processor._aplicar_situacao_das_vendas): contagens e valores.
CHAVES_SITUACAO_CONTAGENS = frozenset({"linhas_canceladas", "linhas_devolucao"})
CHAVES_SITUACAO_VALORES = frozenset({"valor_cancelado", "valor_devolucoes"})
COLUNAS = (
    "ano_exercicio", "chave_produto", "codigo_produto", "descricao_produto", "quantidade",
    "valor_venda_bruto", "quantidade_com_custo", "faturamento_com_custo", "custo_total", "perda",
    "vazamento_preco", "recuperavel", "linhas", "meses", "variacao", "conflito_cadastral",
    "estoque_negativo",
    # Etapa 2 (lead-2026-11.3).
    "faturamento_em_relatorio_com_custo", "meses_com_custo", "meses_margem_negativa", "ultimo_mes_negativo",
    "meses_completos", "quantidade_mesmos_meses", "estoque_final", "categoria", "ncm_ausente",
    "categoria_ausente", "custo_bonificacoes", "custo_devolucoes", "devolucoes_com_custo",
    "relatorios_desde_primeira_venda",
    # Etapa 3 (lead-2026-11.4).
    "fim_ultimo_relatorio_com_venda",
)
# Tipo de cada coluna, para a conferência do contrato (etl_runtime.validar_avaliacao) e a gravação.
COLUNAS_DINHEIRO = ("valor_venda_bruto", "faturamento_com_custo", "custo_total", "perda", "vazamento_preco",
                    "recuperavel", "faturamento_em_relatorio_com_custo", "custo_bonificacoes", "custo_devolucoes",
                    "devolucoes_com_custo")
COLUNAS_QUANTIDADE = ("quantidade", "quantidade_com_custo", "quantidade_mesmos_meses")
COLUNAS_CONTAGEM = ("linhas", "meses", "meses_com_custo", "meses_margem_negativa", "meses_completos",
                    "relatorios_desde_primeira_venda")
COLUNAS_MARCA = ("variacao", "conflito_cadastral", "estoque_negativo", "ultimo_mes_negativo", "ncm_ausente",
                 "categoria_ausente")
COLUNAS_DATA = ("fim_ultimo_relatorio_com_venda",)
# Maior valor absoluto que cada coluna numérica da staging guarda (NUMERIC(p, s) do banco:
# 10 ** (p - s), exclusivo). O SQLite não impõe a precisão; o PostgreSQL derrubaria a gravação.
TETOS = {
    "quantidade": 10 ** 11, "quantidade_com_custo": 10 ** 11, "quantidade_mesmos_meses": 10 ** 11,
    "estoque_final": 10 ** 11,
    "valor_venda_bruto": 10 ** 12, "faturamento_com_custo": 10 ** 12, "custo_total": 10 ** 12,
    "perda": 10 ** 12, "vazamento_preco": 10 ** 12, "recuperavel": 10 ** 12,
    "faturamento_em_relatorio_com_custo": 10 ** 12, "custo_bonificacoes": 10 ** 12, "custo_devolucoes": 10 ** 12,
    "devolucoes_com_custo": 10 ** 12,
    # Derivadas no processo pai: custo médio NUMERIC(14,4) e vazamento estimado NUMERIC(12,2).
    "custo_medio_registrado": 10 ** 10, "vazamento_estimado_r$": 10 ** 10,
}
# Valores em dinheiro da meta (ajustes sem venda e situação): o mesmo teto do dinheiro das linhas.
TETO_DINHEIRO_DA_META = 10 ** 12
LIMITE_DESCRICAO = 200  # brainstorming_dados_pdv.descricao_produto
