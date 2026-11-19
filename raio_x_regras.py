"""Regras do Raio-X que o processo web usa, só com a biblioteca padrão.

raio_x.py (o cálculo, com pandas) roda no processo isolado do ETL (etl_worker). O processo web lê daqui a
versão das regras, os tetos do envio, a razão do custo implausível, os limiares das telas, as chaves do
resultado e a recomendação de plano, sem carregar pandas, numpy nem openpyxl. raio_x.py importa estes nomes
daqui (uma só fonte) e continua a expô-los.
"""

from __future__ import annotations

from etl_regras import _reais as _brl

# 11.4: saneamento de 11/2026 (rodapés, quantidades, títulos de reserva, codificação e os campos de
# CMV líquido). Raio-X gravado com a 11.3 não tem os campos líquidos (raio_x_servico.visao).
# 11.5: unificação de 11/2026. O item de custo implausível sai da perda e do recuperável (a regra que a
# triagem do lead já tinha), a leitura passa a aceitar os formatos e o leiaute paginado do ERP, e o total
# informado com fração de centavo fecha com a soma do relatório (_fechar_centavos).
# 11.6: indicadores executivos de 11/2026. O resultado ganha o nó indicadores_executivos (21 indicadores,
# indicadores_executivos.py), e o Raio-X do cliente também fatia por mês a venda item a item com a data de cada
# venda (sem recusar a data inconsistente: o arquivo continua um relatório só).
# 11.7: Plano de Ação de 11/2026 (Etapa 2). O scanner ganha faturamento_variacao_ou_sem_custo, a venda dos produtos
# com variação de preço ou sem custo (a base do ganho de margem do cenário otimizado, plano_acao.py). Nenhum valor
# antigo muda; o Raio-X gravado antes continua valendo, sem esse campo.
VERSAO_REGRAS = "raio-x-2026-11.7"
MAX_ARQUIVOS = 36
# Relatórios do envio depois de fatiar por mês o arquivo que traz a data de cada venda: 10 anos de meses. O
# teto de arquivos enviados continua sendo MAX_ARQUIVOS.
MAX_FATIAS = 120
# Custo a partir de 3 vezes a venda com custo é erro de cadastro (embalagem cadastrada como unidade, item
# genérico vendido por valor), não prejuízo: é a razão padrão da triagem do lead (há teste que mantém as duas iguais).
RAZAO_CUSTO_IMPLAUSIVEL = 3
LIMITE_ITENS_LISTA = 50
TOLERANCIA_PRECO_MINIMA = 0.01
TOLERANCIA_PRECO_RELATIVA = 0.005
LIMIAR_SEM_CUSTO = 0.10
LIMIAR_RECUPERAVEL = 0.01
LIMIAR_ALTA_CUSTO = 0.05
CHAVES_RESULTADO = frozenset({
    "versao", "arquivos", "fatias", "saude", "scanner", "auditoria", "recuperavel", "periodo", "avisos",
    "indicadores_executivos",
})
# O Raio-X gravado antes da 11.6 não tem os indicadores: a leitura do banco (raio_x_servico.ultimo) exige só estas.
CHAVES_OBRIGATORIAS = CHAVES_RESULTADO - {"indicadores_executivos"}


# ---------------------------------------------------------------------------
# Recomendação de plano (fora do worker: depende do catálogo do banco)
# ---------------------------------------------------------------------------
def recomendar(resultado: dict, meses_prata: int | None) -> dict:
    """Plano sugerido e os motivos, em regras fixas e explicáveis.

    PRATA quando o cadastro precisa de auditoria de custo: custo ausente em ao menos 10% do
    faturamento (ou nenhum arquivo com a coluna), item com margem negativa ou potencial
    recuperável de ao menos 1% do faturamento. OURO quando, além disso, o histórico enviado
    passa do que o Prata mantém. BRONZE nos demais casos.
    """
    saude, recuperavel = resultado["saude"], resultado["recuperavel"]
    motivos = []
    if not saude["arquivos_com_custo"]:
        motivos.append("Os relatórios enviados não trazem o custo dos produtos: sem ele, não dá para calcular "
                       "a margem.")
    elif (saude["pct_faturamento_sem_custo"] or 0) >= LIMIAR_SEM_CUSTO:
        motivos.append(f"{_brl(100 * saude['pct_faturamento_sem_custo'])}% do faturamento saiu sem custo "
                       "informado.")
    # O item de custo a confirmar também tem margem negativa (custo de RAZAO_CUSTO_IMPLAUSIVEL vezes a venda ou
    # mais), mas é erro de cadastro: desde 14/11/2026 fica num motivo à parte, e o número de produtos com margem
    # negativa bate com o da Auditoria de custos e o dos pontos do cadastro. O plano sugerido não muda: os dois
    # motivos levam ao Prata. Raio-X gravado antes da 11.5 não tem o bloco: tudo continua margem negativa.
    bloco = resultado.get("auditoria", {}).get("custo_a_confirmar")
    confirmar = bloco.get("itens", 0) if isinstance(bloco, dict) else 0
    negativos = max(0, saude["inconsistencias"]["itens_margem_negativa"] - confirmar)
    if negativos:
        motivos.append(f"{negativos} {'produto' if negativos == 1 else 'produtos'} com margem negativa: "
                       f"{'vendido' if negativos == 1 else 'vendidos'}, no período, abaixo do custo informado.")
    if confirmar:
        motivos.append(f"{confirmar} {'produto' if confirmar == 1 else 'produtos'} com custo a confirmar: nas "
                       f"vendas com custo, o custo é {RAZAO_CUSTO_IMPLAUSIVEL} vezes o valor vendido ou mais, sinal de "
                       "erro no cadastro.")
    faturamento = saude["faturamento_total"]
    if faturamento > 0 and recuperavel["total"] / faturamento >= LIMIAR_RECUPERAVEL:
        motivos.append(f"Dinheiro recuperável de R$ {_brl(recuperavel['total'])} em vendas abaixo do custo ou "
                       "abaixo do preço normal.")
    if not motivos:
        return {"plano": "BRONZE", "motivos": [
            "Cadastro com custo informado e sem perdas relevantes: o acompanhamento do Bronze cobre o que o "
            "Raio-X encontrou."]}
    meses = resultado["periodo"]["meses"]
    if meses_prata and meses and meses > meses_prata:
        motivos.append(f"O histórico enviado tem {meses} meses, acima dos {meses_prata} que o Prata mantém.")
        return {"plano": "OURO", "motivos": motivos}
    return {"plano": "PRATA", "motivos": motivos}
