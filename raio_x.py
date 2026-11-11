"""Raio-X de entrada: saúde do cadastro e da precificação a partir dos relatórios de vendas.

Roda no processo isolado do ETL (etl_worker, operação "raio_x"): não importa banco, Flask
nem config, lê cada arquivo uma vez e devolve só um dict serializável em JSON (números
finitos, textos e datas ISO). Os arquivos são o mesmo "relatório de Vendas por produto"
das análises mensais, um por mês (até MAX_ARQUIVOS), lidos pelas mesmas funções do ETL:
canceladas saem, devoluções e trocas ficam fora das contas de preço e custo.

Definições, as mesmas que a tela explica:
  - Venda analisada: linha válida com quantidade > 0 e valor > 0. Relatório sem nenhuma é
    recusado: não há o que diagnosticar.
  - Custo informado: a coluna de custo do relatório; vazio ou zero conta como "sem custo".
    Coluna de custo por unidade (custo unitário, unid., und., custo médio ou preço de custo
    sem "total" no título) é multiplicada pela quantidade; as demais, e as que já trazem a
    multiplicação no título ("qtde x custo"), valem como custo total da linha. Valor e custo
    de cada venda ficam em centavos; quando o relatório informa o total da linha com fração de
    centavo (o ERP rateia o desconto do pedido entre os itens), a diferença entre a soma das
    linhas arredondadas e a soma do relatório é distribuída, um centavo por linha, às linhas de
    maior resto: o faturamento fecha com o total do relatório e com o da análise mensal.
  - Índice de saúde cadastral (0-100): parte do faturamento analisado que tem custo
    informado e cujo produto não aparece com código ou nome conflitante.
  - Inconsistências: códigos com mais de um nome, nomes com mais de um código e produtos com
    estoque negativo, cada um contado uma vez no envio inteiro; vendas sem valor (quantidade
    sem faturamento), contadas por linha.
  - Preço de referência de cada item, em cada arquivo: o preço de tabela (coluna de preço
    unitário), quando o relatório traz; senão, o preço mais praticado (por quantidade; no
    empate, o menor).
  - Scanner de omissão: itens vendidos mais de uma vez (quantidade > 1) abaixo do preço de
    referência ou com preços diferentes no mesmo arquivo, o perfil que relatórios de margem
    de ERP costumam descartar ou diluir. Só diferenças acima de max(R$ 0,01; 0,5% da
    referência) contam, medidas no preço praticado sem arredondar. Com uma linha por
    produto e sem preço de tabela (ou com o total calculado a partir dele), a variação não é
    visível, e o resultado diz isso.
  - Potencial recuperável, por linha: o maior entre (preço de referência x quantidade -
    valor vendido) e (custo informado - valor vendido). É quanto a receita teria sido maior
    se a venda saísse ao menos pelo preço de referência e nunca abaixo do custo, sem contar
    a mesma diferença duas vezes.
  - Margem bruta estimada: faturamento - custo informado, só nas vendas com custo. Não é
    lucro (não desconta despesas, impostos nem taxas) nem CMV contábil (que depende de
    inventário). A regra "sem margem" do etl_processor continua valendo para os KPIs das
    análises, para o PDF e para a devolutiva; aqui a margem diagnostica o custo que o próprio
    PDV informa, e só chega à tela de quem tem a funcionalidade no plano (entitlements).
  - Vendas abaixo do custo: por item, só as vendas abaixo do custo (quantidade, valor
    vendido, custo dessas vendas e a perda).
  - Precificação desatualizada: entre o primeiro e o último mês com custo, o custo unitário
    subiu ao menos 5% e o preço médio subiu menos que o custo menos 2 pontos.
"""

from __future__ import annotations

from collections import Counter
from calendar import monthrange
from datetime import date
import math
import re

import numpy as np
import pandas as pd

from etl_inputs import ETLInputError
from etl_processor import (
    ETLLeiauteAmbiguo, ETLValidationError, _converter_serie_monetaria, _extrair_periodo_base, _ler_bruto,
    _nome_exibivel, _reais as _brl, _vendas_do_bruto, data_da_linha, normalizar_categoria, normalizar_ncm,
)

# 11.4: saneamento de 11/2026 (rodapés, quantidades, títulos de reserva, codificação e os campos de
# CMV líquido). Raio-X gravado com a 11.3 não tem os campos líquidos (raio_x_servico.visao).
# 11.5: unificação de 11/2026. O item de custo implausível sai da perda e do recuperável (a regra que a
# triagem do lead já tinha), a leitura passa a aceitar os formatos e o leiaute paginado do ERP, e o total
# informado com fração de centavo fecha com a soma do relatório (_fechar_centavos).
VERSAO_REGRAS = "raio-x-2026-11.5"
MAX_ARQUIVOS = 36
# Relatórios do envio depois de fatiar por mês o arquivo que traz a data de cada venda (só a Avaliação de
# Cenário fatia): 5 anos de meses. O teto de arquivos enviados continua sendo MAX_ARQUIVOS.
MAX_FATIAS = 60
_MAIS_QUE_O_TETO_DE_FATIAS = (f"Os relatórios enviados somam mais de {MAX_FATIAS} meses. Envie no máximo 5 anos "
                              "por vez.")
_SEM_VENDAS = ("Nenhuma venda com quantidade e valor maiores que zero nos relatórios enviados. Confira se são "
               "relatórios de vendas por produto.")
# Custo a partir de 3 vezes a venda com custo é erro de cadastro (embalagem cadastrada como unidade, item
# genérico vendido por valor), não prejuízo: é a razão padrão da triagem do lead (há teste que mantém as duas iguais).
RAZAO_CUSTO_IMPLAUSIVEL = 3
LIMITE_ITENS_LISTA = 50
TOLERANCIA_PRECO_MINIMA = 0.01
TOLERANCIA_PRECO_RELATIVA = 0.005
# Folga de ponto flutuante: 1,00 - 0,99 dá 0,010000000000000009, que não pode passar da tolerância.
_EPSILON = 1e-9
LIMIAR_SEM_CUSTO = 0.10
LIMIAR_RECUPERAVEL = 0.01
LIMIAR_ALTA_CUSTO = 0.05
FOLGA_REPASSE = 0.02
CHAVES_RESULTADO = frozenset({
    "versao", "arquivos", "fatias", "saude", "scanner", "auditoria", "recuperavel", "periodo", "avisos",
})
# Título (normalizado pelo ETL) de coluna de custo por unidade, e não do custo total da linha.
# Sinal forte: unit, unid, und, un, "custo por". Sinal fraco (médio, preço de custo) só vale sem
# "total" no título. Multiplicação explícita (x, qtde, quantidade) é sempre o custo da linha.
_RE_UNITARIO_FORTE = re.compile(r"\bunit|\bunid|\bund?\b|custo por")
_RE_UNITARIO_FRACO = re.compile(r"\bmedio\b|\bmedia\b|preco de custo|preco custo")
_RE_MULTIPLICADO = re.compile(r"\bx\b|\bqtde?\b|\bquant")
_RE_TOTAL = re.compile(r"\btot(?:al|ais)?\b")


def custo_e_unitario(titulo: str) -> bool:
    """O título da coluna de custo indica custo por unidade (a multiplicar pela quantidade)."""
    if _RE_MULTIPLICADO.search(titulo):
        return False
    if _RE_UNITARIO_FORTE.search(titulo):
        return True
    return bool(_RE_UNITARIO_FRACO.search(titulo)) and not _RE_TOTAL.search(titulo)

_SOMAS = ("linhas", "quantidade", "faturamento", "quantidade_com_custo", "faturamento_com_custo", "custo_total",
          "perda", "vazamento", "recuperavel", "quantidade_abaixo", "faturamento_abaixo", "custo_abaixo")


# ---------------------------------------------------------------------------
# Números de saída: finitos, arredondados e nativos (o worker usa allow_nan=False)
# ---------------------------------------------------------------------------
def _dinheiro(valor) -> float:
    numero = float(valor)
    return round(numero, 2) if math.isfinite(numero) else 0.0


def _fracao(parte, todo):
    parte, todo = float(parte), float(todo)
    if not (math.isfinite(parte) and math.isfinite(todo)) or todo <= 0:
        return None
    return round(parte / todo, 4)


def _variacao(final, inicial):
    final, inicial = float(final), float(inicial)
    if not (math.isfinite(final) and math.isfinite(inicial)) or inicial <= 0:
        return None
    return round(final / inicial - 1, 4)


def _texto_curto(valor, limite=120):
    texto = " ".join(str(valor or "").split())
    return texto[:limite] or None


# ---------------------------------------------------------------------------
# Um arquivo
# ---------------------------------------------------------------------------
def _codigos(df: pd.DataFrame) -> pd.Series | None:
    if "codigo" not in df.columns:
        return None
    codigos = df["codigo"].map(lambda valor: _texto_curto(valor, 60))
    return codigos.where(~codigos.fillna("").str.lower().isin(("", "nan", "none", "null")))


def _conflitos(df: pd.DataFrame, codigos: pd.Series | None) -> tuple[set, set, set]:
    """(códigos com mais de um nome, nomes com mais de um código, produtos envolvidos)."""
    if codigos is None:
        return set(), set(), set()
    pares = pd.DataFrame({"codigo": codigos, "produto": df["_prod_norm"]}).dropna().drop_duplicates()
    if pares.empty:
        return set(), set(), set()
    nomes_por_codigo = pares.groupby("codigo")["produto"].nunique()
    codigos_por_nome = pares.groupby("produto")["codigo"].nunique()
    codigos_ruins = set(nomes_por_codigo[nomes_por_codigo > 1].index)
    nomes_ruins = set(codigos_por_nome[codigos_por_nome > 1].index)
    envolvidos = set(pares.loc[pares["codigo"].isin(codigos_ruins), "produto"]) | nomes_ruins
    return codigos_ruins, nomes_ruins, envolvidos


def _ler_arquivo(caminho: str, indice: int, *, cadastro: bool = False):
    """(produtos, resumo do arquivo, produtos em conflito) de um relatório de vendas.

    `cadastro=True` (só a Avaliação de Cenário pede) acrescenta, sem mudar nada do Raio-X: em
    `produtos`, o NCM e a categoria de cada produto vendido (quando o relatório tem as colunas) e o
    último estoque informado; no resumo, os ajustes de bonificação e devolução por produto
    (`_ajustes`) e o que o relatório trouxe de cadastro (`_cadastro`)."""
    return _resumir(*_ler_vendas(caminho, indice, cadastro), indice, cadastro)


def _ler_vendas(caminho: str, numero: int, cadastro: bool):
    """(vendas do relatório, formato do arquivo, período do cabeçalho, rótulo "Arquivo N")."""
    rotulo = f"Arquivo {numero + 1}"
    try:
        bruto = _ler_bruto(caminho)
        periodo = _extrair_periodo_base(bruto) if not bruto.empty else None
        df = _vendas_do_bruto(bruto, rotulo.lower(), cadastro=cadastro)
    except ETLLeiauteAmbiguo as erro:
        raise ETLInputError("raio_x_dados", f"{rotulo}: {erro.orientacao}") from None
    except ETLValidationError as erro:
        if erro.code != "data":
            raise  # limite de tamanho, linhas ou células: a mensagem do ETL já é segura
        raise ETLInputError("raio_x_dados", f"{rotulo}: não foi possível identificar produto, quantidade e "
                            "valor de venda. Envie o relatório de vendas por produto (CSV, XLSX ou XLS).") from None
    _fechar_centavos(df)
    return df, bruto.attrs.get("format"), periodo, rotulo


def _vendas_analisadas(df: pd.DataFrame) -> pd.Series:
    """As linhas que o Raio-X analisa: quantidade e valor maiores que zero, fora devolução e troca."""
    validas = ~df["_devolucao"] if "_devolucao" in df.columns else pd.Series(True, index=df.index)
    return validas & (df["quantidade"] > 0) & (df["valor_venda"] > 0)


# O fechamento dos centavos é feito em milionésimos de real, em inteiros: a soma é exata.
_MICROS_POR_REAL = 1_000_000
_MICROS_POR_CENTAVO = _MICROS_POR_REAL // 100
# Acima disto por linha o float já não guarda o milionésimo: a linha fica só arredondada, como sempre.
_TETO_DO_FECHAMENTO = 1e9
# Valor positivo que arredonda para zero centavo: a linha que fica com 0,00 continua sendo venda analisada.
_ABAIXO_DE_MEIO_CENTAVO = 0.004


def _fechar_centavos(df: pd.DataFrame) -> None:
    """Total da linha INFORMADO pelo relatório com fração de centavo (o ERP rateia o desconto do pedido
    entre os itens e imprime "3,8081"): cada linha continua em centavos, e a diferença entre a soma das
    linhas arredondadas e a soma do relatório é distribuída, um centavo por linha. Assim o faturamento
    fecha com o total do relatório e com o da análise mensal, que soma sem arredondar a linha.

    Quem recebe (ou perde) o centavo: só linha que o arredondamento já tinha levado para o outro lado, de
    modo que nenhuma se afasta um centavo do valor informado. Entre elas, primeiro as linhas em que um
    centavo não muda o preço por unidade além da tolerância do scanner (quantidade de ao menos 1, ou valor
    de ao menos R$ 2,00), para o ajuste não parecer variação de preço; depois, a de maior resto; no
    empate, a linha de cima. A linha de um centavo só devolve o centavo quando não há outra: com zero
    centavo ela continua sendo venda analisada, mas passaria a contar também como bonificação.

    Valor CALCULADO pelo motor (quantidade x preço unitário) fica como sempre: cada venda em centavos, como
    o PDV registra. Sem fração de centavo, ou sem diferença, nada muda."""
    if "valor_venda" not in df.attrs.get("cabecalhos", {}):
        return
    venda = _vendas_analisadas(df)
    valor = df.loc[venda, "valor_venda"].to_numpy(float)
    if not valor.size or not np.isfinite(valor).all() or valor.max() >= _TETO_DO_FECHAMENTO:
        return
    micros = np.rint(valor * _MICROS_POR_REAL).astype(np.int64)
    centavos = np.rint(np.round(valor, 2) * 100).astype(np.int64)
    alvo = (sum(micros.tolist()) + _MICROS_POR_CENTAVO // 2) // _MICROS_POR_CENTAVO
    diferenca = alvo - sum(centavos.tolist())
    if not diferenca:
        return
    sinal = 1 if diferenca > 0 else -1
    restos = (micros - centavos * _MICROS_POR_CENTAVO) * sinal  # positivo: arredondada contra a diferença
    quantidade = df.loc[venda, "quantidade"].to_numpy(float)
    sensivel = (quantidade < 1) & (valor < TOLERANCIA_PRECO_MINIMA / TOLERANCIA_PRECO_RELATIVA)
    zeraria = (centavos == 1) & (sinal < 0)
    # Por último as que ficariam com zero centavo; antes delas as sensíveis; entre iguais, a de maior resto.
    ordem = np.lexsort((-restos, sensivel, zeraria))
    escolhidas = ordem[restos[ordem] > 0][:abs(diferenca)]
    if len(escolhidas) < abs(diferenca):
        return  # não acontece (a soma dos restos garante as linhas); sem elas, vale o arredondamento
    centavos[escolhidas] += sinal
    df.loc[venda, "valor_venda"] = np.where(centavos > 0, centavos / 100.0,
                                            np.minimum(valor, _ABAIXO_DE_MEIO_CENTAVO))


def _codigo_do_produto(df: pd.DataFrame):
    """O código de cada linha, quando a coluna lida como código é a do produto: menos de um quinto dos
    códigos tem mais de um nome. A coluna pode ser a do pedido ou a do cliente ("Cód. Venda" à esquerda do
    código do produto): aí cada código tem vários produtos, e ele não diz se o produto se repete. Sem
    coluna de código, None."""
    codigos = _codigos(df)
    if codigos is None:
        return None
    pares = pd.DataFrame({"codigo": codigos, "produto": df["_prod_norm"]}).dropna().drop_duplicates()
    nomes_por_codigo = pares.groupby("codigo")["produto"].nunique()
    if nomes_por_codigo.empty or 5 * int((nomes_por_codigo > 1).sum()) >= len(nomes_por_codigo):
        return None
    return codigos


def _e_venda_item_a_item(df: pd.DataFrame) -> bool:
    """Ao menos um quinto das linhas de venda repete o produto: o mesmo nome, com o mesmo código (quando o
    relatório traz o código do produto) e na mesma loja (quando traz a loja). Num resumo, a coluna "Data" é
    outra coisa (a última venda, o cadastro) e quase nada se repete: nem com dois códigos de mesmo nome (a
    grade de tamanhos de uma confecção), nem com uma linha por produto e loja. A linha de devolução do
    produto não entra na conta."""
    vendas = df[~df["_devolucao"]] if "_devolucao" in df.columns else df
    chave = vendas[[coluna for coluna in ("_prod_norm", "_unidade") if coluna in vendas.columns]]
    codigos = _codigo_do_produto(vendas)
    if codigos is not None:
        chave = chave.assign(_codigo=codigos)
    return 5 * int(chave.duplicated(keep=False).sum()) >= len(vendas)


def _fatias_mensais(df: pd.DataFrame, periodo, rotulo: str, ate=None):
    """[(vendas do mês, (início, fim))] quando o relatório traz a data de cada venda e o período do
    cabeçalho passa de um mês; senão None (o arquivo é um relatório só, como sempre).

    Cada mês do calendário do período do cabeçalho vira um relatório, cortado pelo período, COM venda ou
    sem: o mês sem venda continua sendo período coberto (os dias dele contam no ritmo, e outro arquivo do
    mesmo mês continua sendo sobreposição). `ate` (hoje, na Avaliação de Cenário) dispensa os meses sem
    venda que ainda não começaram.

    Data ilegível em parte das linhas, fora do período do cabeçalho ou em mês que ainda não começou: o
    arquivo de um ano só continua um relatório, como era lido antes de existirem as fatias; o que cruza o
    ano, que a Avaliação de Cenário recusaria de qualquer jeito, é recusado aqui, com o motivo."""
    if "_data" not in df.columns or periodo is None:
        return None
    inicio, fim = periodo
    if (inicio.year, inicio.month) == (fim.year, fim.month):
        return None
    # Só a venda item a item é fatiada: num resumo por produto, a coluna "Data" é outra coisa, e o arquivo
    # continua uma fatia só.
    if not _e_venda_item_a_item(df):
        return None
    datas = df["_data"].map({texto: data_da_linha(texto) for texto in df["_data"].unique()})
    if datas.isna().all():
        return None  # a coluna não traz data nenhuma: não é a data da venda
    motivo = None
    if datas.isna().any():
        motivo = (f"{rotulo}: o relatório cobre mais de um mês e há linhas de venda sem data legível. Envie a "
                  "venda item a item com a data em todas as linhas, ou um relatório por mês.")
    elif (datas < inicio).any() or (datas > fim).any():
        motivo = (f"{rotulo}: há vendas com data fora do período do cabeçalho ({inicio:%d/%m/%Y} a "
                  f"{fim:%d/%m/%Y}). Confira o relatório exportado.")
    elif ate is not None and (datas.map(lambda dia: dia.replace(day=1)) > ate).any():
        motivo = (f"{rotulo}: há vendas com data em um mês que ainda não começou (hoje é {ate:%d/%m/%Y}). "
                  "Confira a data das vendas no relatório exportado.")
    if motivo is not None:
        if inicio.year == fim.year:
            return None
        raise ETLInputError("raio_x_datas", motivo)
    meses = datas.map(lambda dia: dia.year * 100 + dia.month)
    # Os meses do período que já começaram (o mês com venda já começou: conferido acima).
    do_periodo, (ano, mes) = [], (inicio.year, inicio.month)
    while (ano, mes) <= (fim.year, fim.month):
        if ate is None or date(ano, mes, 1) <= ate:
            do_periodo.append((ano, mes))
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    if len(do_periodo) > MAX_FATIAS:
        raise ETLInputError("raio_x_arquivos", _MAIS_QUE_O_TETO_DE_FATIAS)
    fatias = []
    for ano, mes in do_periodo:
        vendas = df[meses == ano * 100 + mes].reset_index(drop=True)
        vendas.attrs = dict(df.attrs)
        if fatias:
            # Os contadores do arquivo (qualidade da leitura, situação das linhas) entram uma vez só.
            vendas.attrs.update({"qualidade": {}, "situacao": {}, "celulas_recodificadas": 0})
        fatias.append((vendas, (max(inicio, date(ano, mes, 1)), min(fim, date(ano, mes, monthrange(ano, mes)[1])))))
    return fatias


def _resumir(df: pd.DataFrame, formato, periodo, rotulo: str, indice: int, cadastro: bool):
    """(produtos, resumo, produtos em conflito) de um relatório já lido, ou de uma fatia mensal dele."""
    cabecalhos = df.attrs.get("cabecalhos", {})
    validas = ~df["_devolucao"] if "_devolucao" in df.columns else pd.Series(True, index=df.index)
    quantidade, valor = df["quantidade"], df["valor_venda"]
    venda = _vendas_analisadas(df)
    tem_custo = "valor_custo" in df.columns
    custo = df["valor_custo"] if tem_custo else pd.Series(0.0, index=df.index)
    if tem_custo and custo_e_unitario(cabecalhos.get("valor_custo", "")):
        custo = custo * quantidade  # custo por unidade: o da linha é unitário x quantidade
    tem_preco_unitario = "preco_unitario" in df.columns
    # Sem coluna de total, o ETL calcula o valor da linha pelo preço unitário: o preço praticado
    # é o próprio preço de tabela, e a diferença entre os dois não existe.
    valor_derivado = "valor_venda" not in cabecalhos
    preco_tabela = (_converter_serie_monetaria(df["preco_unitario"], Counter(), formato == "csv") if tem_preco_unitario
                    else pd.Series(np.nan, index=df.index))
    numeros = np.concatenate([quantidade[venda].to_numpy(float), valor[venda].to_numpy(float),
                              custo[venda].to_numpy(float)])
    if not np.isfinite(numeros).all():
        raise ETLInputError("raio_x_dados", f"{rotulo}: há quantidade, valor ou custo fora do intervalo aceito.")
    codigos = _codigos(df)
    codigos_ruins, nomes_ruins, envolvidos = _conflitos(df, codigos)
    estoque_negativo = (set(df.groupby("_prod_norm")["estoque"].min().loc[lambda s: s < 0].index)
                        if "estoque" in df.columns else set())

    # Valor e custo de cada venda em centavos, como o PDV registra: total calculado por preço x
    # quantidade fracionada (granel) não deixa fração de centavo, e a perda de cada linha da
    # auditoria fecha com "custo dessas vendas - vendido por".
    v = pd.DataFrame({
        "produto": df.loc[venda, "_prod_norm"],
        "nome": df.loc[venda, "produto"].map(_nome_exibivel),
        "codigo": codigos[venda] if codigos is not None else None,
        "quantidade": quantidade[venda],
        "faturamento": valor[venda].round(2),
        "custo": custo[venda].where(custo[venda] > 0, 0.0).round(2),
        "tabela": preco_tabela[venda].where(preco_tabela[venda] > 0),
    })
    v["preco_real"] = v["faturamento"] / v["quantidade"]
    v["preco"] = v["preco_real"].round(2)  # só para agrupar os preços e mostrar mínimo e máximo
    v["com_custo"] = v["custo"] > 0
    # Referência: o preço de tabela quando o produto tem; senão, o mais praticado.
    tem_tabela = v["tabela"].notna().groupby(v["produto"]).transform("any").astype(bool)
    v["candidato"] = np.where(tem_tabela, v["tabela"].round(2), v["preco"])
    # Soma arredondada: 1,1 + 2,2 kg empata com 3,3 kg, e o desempate fica com o menor preço.
    pesos = (v.dropna(subset=["candidato"]).groupby(["produto", "candidato"])["quantidade"].sum().round(6)
             .reset_index().sort_values(["produto", "quantidade", "candidato"], ascending=[True, False, True]))
    referencia = pesos.drop_duplicates("produto").set_index("produto")["candidato"]
    v["referencia"] = v["produto"].map(referencia)
    v["referencia_tabela"] = tem_tabela
    limite = np.maximum(TOLERANCIA_PRECO_MINIMA, TOLERANCIA_PRECO_RELATIVA * v["referencia"].fillna(0)) + _EPSILON
    abaixo_referencia = (v["referencia"] - v["preco_real"]) > limite
    fora_da_referencia = (v["preco_real"] - v["referencia"]).abs() > limite
    grupos = v.groupby("produto")
    amplitude = grupos["preco_real"].transform("max") - grupos["preco_real"].transform("min")
    variacao = ((amplitude > limite) | fora_da_referencia.groupby(v["produto"]).transform("any")) \
        & (grupos["quantidade"].transform("sum") > 1)
    v["vazamento"] = np.where(variacao & abaixo_referencia,
                              v["referencia"] * v["quantidade"] - v["faturamento"], 0.0)
    v["perda"] = np.where(v["com_custo"], np.maximum(v["custo"] - v["faturamento"], 0.0), 0.0)
    v["recuperavel"] = np.maximum(v["vazamento"], v["perda"])
    abaixo_custo = v["perda"] > 0
    v["quantidade_abaixo"] = np.where(abaixo_custo, v["quantidade"], 0.0)
    v["faturamento_abaixo"] = np.where(abaixo_custo, v["faturamento"], 0.0)
    v["custo_abaixo"] = np.where(abaixo_custo, v["custo"], 0.0)
    v["variacao"] = variacao
    v["linhas"] = 1
    v["quantidade_com_custo"] = np.where(v["com_custo"], v["quantidade"], 0.0)
    v["faturamento_com_custo"] = np.where(v["com_custo"], v["faturamento"], 0.0)
    v["custo_total"] = v["custo"]

    grupos = v.groupby("produto", sort=False)
    produtos = grupos[list(_SOMAS)].sum()
    produtos["nome"] = grupos["nome"].first()
    produtos["codigo"] = grupos["codigo"].first() if codigos is not None else None
    produtos["preco_min"] = grupos["preco"].min()
    produtos["preco_max"] = grupos["preco"].max()
    produtos["referencia"] = grupos["referencia"].first()
    produtos["referencia_tabela"] = grupos["referencia_tabela"].any()
    produtos["variacao"] = grupos["variacao"].any()
    produtos["arquivo"] = indice

    faturamento = float(v["faturamento"].sum())
    devolucoes = float(-valor[~validas].sum()) if "_devolucao" in df.columns else 0.0
    # CMV líquido: a bonificação (item entregue sem cobrança: quantidade com valor zero informado,
    # "0,00", e não célula em branco) saiu do estoque e soma o custo; a devolução voltou ao estoque
    # e desconta o custo e a receita dela.
    custo_linha = custo.abs().round(2)
    informado = df["_valor_informado"] if "_valor_informado" in df.columns else pd.Series(True, index=df.index)
    bonificadas = validas & informado & (quantidade > 0) & (valor.round(2) == 0) & (custo_linha > 0)
    devolvidas_com_custo = ~validas & (custo_linha > 0)
    resumo = {
        "indice": indice,
        "inicio": periodo[0] if periodo else None,
        "fim": periodo[1] if periodo else None,
        "linhas": int(len(df)),
        "tem_custo": tem_custo,
        "tem_preco_unitario": tem_preco_unitario,
        "faturamento": faturamento,
        "devolucoes": devolucoes,
        "itens": int(produtos.shape[0]),
        "faturamento_com_custo": float(v["faturamento_com_custo"].sum()),
        "custo_total": float(v["custo_total"].sum()),
        "perda": float(v["perda"].sum()),
        "vazamento": float(v["vazamento"].sum()),
        "recuperavel": float(v["recuperavel"].sum()),
        "venda_sem_valor": int((validas & (quantidade > 0) & (valor <= 0)).sum()),
        "variacao_visivel": bool((tem_preco_unitario and not valor_derivado)
                                 or (grupos["linhas"].sum() > 1).any()),
        "custo_bonificacoes": float(custo_linha[bonificadas].sum()),
        "custo_devolucoes": float(custo_linha[devolvidas_com_custo].sum()),
        "devolucoes_com_custo": float(valor[devolvidas_com_custo].abs().round(2).sum()),
        "celulas_recodificadas": int(df.attrs.get("celulas_recodificadas", 0)),
        # Do arquivo, para a Avaliação de Cenário (avaliacao_motor): conflito por arquivo e o que o
        # ETL contou na limpeza. Também não são serializados.
        "_envolvidos": envolvidos,
        "_qualidade": dict(df.attrs.get("qualidade") or {}),
        "_situacao": dict(df.attrs.get("situacao") or {}),
        # Conjuntos, não serializados: as inconsistências contam cada código ou produto uma vez.
        "_codigos_conflito": codigos_ruins,
        "_nomes_conflito": nomes_ruins,
        "_estoque_negativo": estoque_negativo,
    }
    if cadastro:
        _cadastro_do_arquivo(df, venda, produtos, resumo, custo_linha, bonificadas, devolvidas_com_custo, valor)
    return produtos, resumo, envolvidos


def _primeiro_valor(serie: pd.Series):
    """O primeiro valor não nulo da série (ou None)."""
    validos = serie.dropna()
    return validos.iloc[0] if not validos.empty else None


def _texto_da_unidade(valor) -> str:
    """A loja ou filial da linha como texto comparável ("" quando a célula está vazia): sem diferença de
    espaços nem de maiúsculas, e o código 1 igual a 1.0 (a célula numérica do XLSX)."""
    if valor is None or (isinstance(valor, float) and valor != valor):
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    texto = " ".join(str(valor).split()).casefold()
    if texto.endswith(".0") and texto[:-2].isdigit():
        texto = texto[:-2]
    return texto


def _cadastro_do_arquivo(df, venda, produtos, resumo, custo_linha, bonificadas, devolvidas_com_custo, valor) -> None:
    """Só para a Avaliação de Cenário (avaliacao_motor): o cadastro e os ajustes do arquivo por produto."""
    info = df.attrs.get("cadastro") or {}
    estoque_informado = df["_estoque_informado"] if "_estoque_informado" in df.columns else None
    unidades_das_vendas = None
    if "_unidade" in df.columns:
        unidades_das_vendas = df.loc[venda, "_unidade"].map(_texto_da_unidade)
    varias_unidades = unidades_das_vendas is not None and unidades_das_vendas[unidades_das_vendas != ""].nunique() >= 2
    resumo["_cadastro"] = {
        "ncm": bool(info.get("ncm")), "categoria": bool(info.get("categoria")),
        "ambiguo": bool(info.get("ambiguo")), "estoque": estoque_informado is not None,
        "varias_unidades": bool(varias_unidades), "produto_de_loja": bool(info.get("produto_de_loja")),
    }
    vendidos = df.loc[venda, "_prod_norm"]
    if info.get("ncm"):
        ncm = df.loc[venda, "_ncm"].map(normalizar_ncm)
        produtos["ncm"] = ncm.groupby(vendidos).agg(_primeiro_valor).reindex(produtos.index)
    if info.get("categoria"):
        categoria = df.loc[venda, "_categoria"].map(normalizar_categoria)
        produtos["categoria"] = categoria.groupby(vendidos).agg(_primeiro_valor).reindex(produtos.index)
    if estoque_informado is not None:
        # O último estoque informado do produto no arquivo (a linha mais de baixo), em qualquer linha do
        # produto: o relatório "vendas x estoque" lista o item com quantidade 0 e o saldo atual. A marca
        # de estoque negativo já olha todas as linhas (_estoque_negativo).
        informado = estoque_informado
        if varias_unidades:
            # Relatório com várias lojas: o estoque da rede é a soma do último estoque de cada loja (o da
            # última linha seria o de uma loja só). Linha sem loja conta como uma loja "sem nome".
            unidade = df.loc[informado, "_unidade"].map(_texto_da_unidade)
            por_loja = df.loc[informado, "estoque"].groupby([df.loc[informado, "_prod_norm"], unidade]).last()
            estoques = por_loja.groupby(level=0).sum()
        else:
            estoques = df.loc[informado, "estoque"].groupby(df.loc[informado, "_prod_norm"]).last()
        # Só os produtos com venda: com a tabela vazia (arquivo ou mês sem venda válida), a atribuição direta
        # adotaria o índice da série e criaria linhas de produto sem venda, com as somas vazias.
        produtos["estoque"] = estoques.reindex(produtos.index)
        # Todos os produtos com saldo informado no arquivo, inclusive os que não venderam nele: o motor do
        # lead usa o saldo mais recente do ano (o relatório "vendas x estoque" lista o item parado).
        resumo["_estoques"] = estoques
    # Bonificação e devolução por produto, inclusive de produto sem venda no arquivo: as mesmas máscaras
    # dos totais do resumo (custo_bonificacoes, custo_devolucoes e devolucoes_com_custo).
    ajustes = pd.DataFrame({
        "custo_bonificacoes": custo_linha.where(bonificadas, 0.0),
        "custo_devolucoes": custo_linha.where(devolvidas_com_custo, 0.0),
        "devolucoes_com_custo": valor.abs().round(2).where(devolvidas_com_custo, 0.0),
    }).groupby(df["_prod_norm"]).sum()
    resumo["_ajustes"] = ajustes[(ajustes != 0).any(axis=1)]


# ---------------------------------------------------------------------------
# Combinação dos arquivos
# ---------------------------------------------------------------------------
def _numero_do_arquivo(resumo) -> int:
    """O número do arquivo enviado (as fatias mensais de um mesmo arquivo têm o mesmo número)."""
    return resumo.get("arquivo", resumo["indice"]) + 1


def _rotulo(resumo) -> str:
    inicio, fim = resumo["inicio"], resumo["fim"]
    if inicio is None:
        return f"Arquivo {_numero_do_arquivo(resumo)}"
    if (inicio.year, inicio.month) == (fim.year, fim.month):
        return f"{inicio.month:02d}/{inicio.year}"
    return f"{inicio:%d/%m/%Y} a {fim:%d/%m/%Y}"


def _fatia(resumo, fora=None) -> dict:
    """`fora`: (perda, recuperável) dos itens de custo implausível do arquivo, que saem dos dois totais."""
    perda_fora, recuperavel_fora = fora or (0.0, 0.0)
    com_custo = resumo["faturamento_com_custo"]
    margem = com_custo - resumo["custo_total"]
    return {
        "indice": resumo["indice"],
        "rotulo": _rotulo(resumo),
        "inicio": resumo["inicio"].isoformat() if resumo["inicio"] else None,
        "fim": resumo["fim"].isoformat() if resumo["fim"] else None,
        "faturamento": _dinheiro(resumo["faturamento"]),
        "itens": resumo["itens"],
        "faturamento_sem_custo": _dinheiro(resumo["faturamento"] - com_custo),
        "pct_faturamento_sem_custo": _fracao(resumo["faturamento"] - com_custo, resumo["faturamento"]),
        "custo_total": _dinheiro(resumo["custo_total"]) if com_custo > 0 else None,
        "margem_valor": _dinheiro(margem) if com_custo > 0 else None,
        "margem_percentual": _fracao(margem, com_custo) if com_custo > 0 else None,
        "perda_abaixo_custo": _dinheiro(max(0.0, resumo["perda"] - perda_fora)),
        "vazamento_preco": _dinheiro(resumo["vazamento"]),
        "recuperavel": _dinheiro(max(0.0, resumo["recuperavel"] - recuperavel_fora)),
        "devolucoes": _dinheiro(resumo["devolucoes"]),
    }


def _item(linha, campos) -> dict:
    item = {"produto": linha["nome"], "codigo": linha["codigo"] if isinstance(linha["codigo"], str) else None}
    custo, com_custo = float(linha["custo_total"]), float(linha["faturamento_com_custo"])
    margem = com_custo - custo
    referencia = float(linha["referencia"]) if pd.notna(linha["referencia"]) else None
    valores = {
        "linhas": int(linha["linhas"]),
        "quantidade": round(float(linha["quantidade"]), 3),
        "faturamento": _dinheiro(linha["faturamento"]),
        "preco_min": _dinheiro(linha["preco_min"]),
        "preco_max": _dinheiro(linha["preco_max"]),
        "preco_referencia": _dinheiro(referencia) if referencia is not None else None,
        "referencia_tabela": bool(linha["referencia_tabela"]),
        "vazamento": _dinheiro(linha["vazamento"]),
        "quantidade_com_custo": round(float(linha["quantidade_com_custo"]), 3),
        "faturamento_com_custo": _dinheiro(com_custo),
        "custo_total": _dinheiro(custo) if custo > 0 else None,
        "custo_medio": (_dinheiro(custo / float(linha["quantidade_com_custo"]))
                        if custo > 0 and float(linha["quantidade_com_custo"]) > 0 else None),
        "margem_valor": _dinheiro(margem) if custo > 0 else None,
        "margem_percentual": _fracao(margem, com_custo) if custo > 0 else None,
        "quantidade_abaixo": round(float(linha["quantidade_abaixo"]), 3),
        "faturamento_abaixo": _dinheiro(linha["faturamento_abaixo"]),
        "custo_abaixo": _dinheiro(linha["custo_abaixo"]),
        "perda": _dinheiro(linha["perda"]),
    }
    item.update({campo: valores[campo] for campo in campos})
    return item


def _precificacao(por_arquivo: pd.DataFrame, resumos: list) -> tuple[bool, list]:
    """Custo unitário que subiu mais que o preço entre o primeiro e o último mês com custo."""
    ordem = {r["indice"]: r["inicio"] for r in resumos if r["inicio"] is not None and r["tem_custo"]}
    if len(ordem) < 2:
        return False, []
    base = por_arquivo[por_arquivo["arquivo"].isin(list(ordem)) & (por_arquivo["quantidade_com_custo"] > 0)
                       & (por_arquivo["custo_total"] > 0)].copy()
    if base.empty:
        return True, []
    base["inicio"] = base["arquivo"].map(ordem)
    base = base.sort_values(["produto", "inicio", "arquivo"])
    primeiro = base.groupby("produto").head(1).set_index("produto")
    ultimo = base.groupby("produto").tail(1).set_index("produto")
    alertas = []
    for produto, fim in ultimo.iterrows():
        comeco = primeiro.loc[produto]
        if comeco["arquivo"] == fim["arquivo"]:
            continue
        custo_inicial = comeco["custo_total"] / comeco["quantidade_com_custo"]
        custo_final = fim["custo_total"] / fim["quantidade_com_custo"]
        preco_inicial = comeco["faturamento_com_custo"] / comeco["quantidade_com_custo"]
        preco_final = fim["faturamento_com_custo"] / fim["quantidade_com_custo"]
        alta_custo, alta_preco = _variacao(custo_final, custo_inicial), _variacao(preco_final, preco_inicial)
        if alta_custo is None or alta_preco is None:
            continue
        if alta_custo >= LIMIAR_ALTA_CUSTO and alta_preco < alta_custo - FOLGA_REPASSE:
            impacto = max(0.0, (custo_final - custo_inicial) - (preco_final - preco_inicial)) * fim["quantidade_com_custo"]
            alertas.append({
                "produto": fim["nome"],
                "custo_unitario_inicial": _dinheiro(custo_inicial), "custo_unitario_final": _dinheiro(custo_final),
                "preco_inicial": _dinheiro(preco_inicial), "preco_final": _dinheiro(preco_final),
                "variacao_custo": alta_custo, "variacao_preco": alta_preco, "impacto": _dinheiro(impacto),
            })
    alertas.sort(key=lambda alerta: (-alerta["impacto"], alerta["produto"]))
    return True, alertas[:LIMITE_ITENS_LISTA]


def _avisos(resumos: list) -> list:
    avisos = []
    # Compara cada arquivo com o que chega mais longe entre os anteriores: um relatório anual
    # e três mensais do mesmo ano geram um aviso para cada mensal, e não só para o vizinho.
    datados = sorted((r for r in resumos if r["inicio"] is not None), key=lambda r: (r["inicio"], r["indice"]))
    dono = None
    for atual in datados:
        if dono is not None and atual["inicio"] <= dono["fim"]:
            avisos.append(f"Os arquivos {_numero_do_arquivo(dono)} e {_numero_do_arquivo(atual)} cobrem períodos que se "
                          "sobrepõem: os totais somam os dois.")
        if dono is None or atual["fim"] > dono["fim"]:
            dono = atual
    for r in resumos:
        if r["inicio"] is None:
            avisos.append(f"O arquivo {_numero_do_arquivo(r)} não traz o período no cabeçalho: entra nos totais, "
                          "mas fica fora da linha do tempo.")
        if not r["tem_custo"]:
            avisos.append(f"O arquivo {_numero_do_arquivo(r)} não traz a coluna de custo: as vendas dele contam como "
                          "sem custo informado.")
        if r.get("celulas_recodificadas"):
            avisos.append(f"O arquivo {_numero_do_arquivo(r)} misturava codificações de texto: os acentos de "
                          f"{r['celulas_recodificadas']} célula(s) foram corrigidos, para o mesmo produto não "
                          "virar dois entre os meses.")
    # As fatias mensais de um mesmo arquivo repetem o aviso: fica um por arquivo.
    return list(dict.fromkeys(avisos))


def diagnosticar(caminhos: list) -> dict:
    """Lê os relatórios de vendas (na ordem recebida) e devolve o Raio-X combinado."""
    return _combinar(*_ler_envio(caminhos))


def _ler_envio(caminhos: list, *, cadastro: bool = False, ate=None):
    """(por_arquivo, resumos, envolvidos): cada relatório lido uma vez por _ler_arquivo e os
    agregados por produto e arquivo em ordem cronológica estável (arquivo sem período conta como
    o mais antigo). A Avaliação de Cenário (avaliacao_motor) parte daqui, como o Raio-X, com
    `cadastro=True` (ver _ler_arquivo) e `ate` igual a hoje (ver _fatias_mensais)."""
    if not 1 <= len(caminhos) <= MAX_ARQUIVOS:
        raise ETLInputError("raio_x_arquivos", f"Envie de 1 a {MAX_ARQUIVOS} relatórios de vendas.")
    partes, resumos, envolvidos = [], [], set()
    for numero, caminho in enumerate(caminhos):
        df, formato, periodo, rotulo = _ler_vendas(caminho, numero, cadastro)
        # Só a Avaliação de Cenário (cadastro=True) lê a data de cada venda: um arquivo de vários meses
        # vira um relatório por mês, em memória. No Raio-X do cliente o arquivo continua uma fatia só.
        fatias = _fatias_mensais(df, periodo, rotulo, ate) if cadastro else None
        if len(resumos) + len(fatias or ()) > MAX_FATIAS:
            raise ETLInputError("raio_x_arquivos", _MAIS_QUE_O_TETO_DE_FATIAS)
        for vendas, intervalo in fatias or [(df, periodo)]:
            produtos, resumo, conflitantes = _resumir(vendas, formato, intervalo, rotulo, len(resumos), cadastro)
            resumo["arquivo"] = numero
            # O mês sem venda de um arquivo fatiado é período coberto (entra nos resumos), sem produto a somar.
            if fatias is None or not produtos.empty:
                partes.append(produtos.reset_index())
            resumos.append(resumo)
            envolvidos |= conflitantes
        if len(resumos) > MAX_FATIAS:
            raise ETLInputError("raio_x_arquivos", _MAIS_QUE_O_TETO_DE_FATIAS)
    if not partes:
        raise ETLInputError("raio_x_sem_vendas", _SEM_VENDAS)
    # Em ordem cronológica: nome, código e preço de referência de cada item ficam os do mês mais recente.
    datas = {r["indice"]: r["inicio"] or date.min for r in resumos}
    por_arquivo = pd.concat(partes, ignore_index=True)
    por_arquivo["_data"] = por_arquivo["arquivo"].map(datas)
    por_arquivo = por_arquivo.sort_values(["_data", "arquivo"], kind="stable")
    return por_arquivo, resumos, envolvidos


def _combinar(por_arquivo: pd.DataFrame, resumos: list, envolvidos: set) -> dict:
    """O Raio-X do envio a partir do que _ler_envio leu: totais, saúde, scanner, auditoria,
    recuperável, período e avisos."""
    grupos = por_arquivo.groupby("produto", sort=False)
    produtos = grupos[list(_SOMAS)].sum()
    produtos["nome"] = grupos["nome"].last()
    produtos["codigo"] = grupos["codigo"].last()
    produtos["preco_min"] = grupos["preco_min"].min()
    produtos["preco_max"] = grupos["preco_max"].max()
    produtos["referencia"] = grupos["referencia"].last()
    produtos["referencia_tabela"] = grupos["referencia_tabela"].last()
    produtos["variacao"] = grupos["variacao"].any()

    faturamento = float(produtos["faturamento"].sum())
    if faturamento <= 0:
        raise ETLInputError("raio_x_sem_vendas", _SEM_VENDAS)
    com_custo = float(produtos["faturamento_com_custo"].sum())
    custo_total = float(produtos["custo_total"].sum())
    sem_custo = produtos["custo_total"] <= 0
    auditavel = float(produtos.loc[~produtos.index.isin(envolvidos), "faturamento_com_custo"].sum())
    margem = com_custo - custo_total
    margem_negativa = (produtos["custo_total"] > 0) & (produtos["faturamento_com_custo"] < produtos["custo_total"])

    # Custo implausível: o item sai da perda abaixo do custo e do potencial recuperável (fica só o que ele tem
    # de variação de preço) e é listado "a confirmar". O custo total e a margem do envio não mudam.
    # Em centavos inteiros, como a triagem do lead compara (em Decimal): em ponto flutuante, três vendas de
    # 1,10 com custo de 3,30 cada somam 9,899999999999999 de custo, abaixo de 3 vezes 3,30.
    custo_em_centavos = (produtos["custo_total"] * 100).round()
    venda_em_centavos = (produtos["faturamento_com_custo"] * 100).round()
    a_confirmar = (venda_em_centavos > 0) & (custo_em_centavos >= RAZAO_CUSTO_IMPLAUSIVEL * venda_em_centavos)
    implausiveis = produtos[a_confirmar].sort_values(["custo_total", "faturamento"], ascending=False)
    perda_fora = float(implausiveis["perda"].sum())
    recuperavel_fora = float((implausiveis["recuperavel"] - implausiveis["vazamento"]).sum())
    fora_por_arquivo = {}
    if not implausiveis.empty:
        parte = por_arquivo[por_arquivo["produto"].isin(implausiveis.index)]
        somas = (parte.assign(_excesso=parte["recuperavel"] - parte["vazamento"])
                 .groupby("arquivo")[["perda", "_excesso"]].sum())
        fora_por_arquivo = {int(indice): (float(linha["perda"]), float(linha["_excesso"]))
                            for indice, linha in somas.iterrows()}

    com_variacao = produtos[produtos["variacao"]].sort_values(["vazamento", "faturamento"], ascending=False)
    abaixo = produtos[(produtos["perda"] > 0) & ~a_confirmar].sort_values(["perda", "faturamento"], ascending=False)
    com_margem = produtos[produtos["custo_total"] > 0].sort_values("faturamento_com_custo", ascending=False)
    precificacao_visivel, precificacao = _precificacao(por_arquivo, resumos)
    variacao_visivel = any(r["variacao_visivel"] for r in resumos)

    inicios = [r["inicio"] for r in resumos if r["inicio"] is not None]
    fins = [r["fim"] for r in resumos if r["fim"] is not None]
    inicio, fim = (min(inicios), max(fins)) if inicios else (None, None)
    # Composição líquida do CMV e da margem: + custo das bonificações, - custo e receita das
    # devoluções que trazem custo. Os campos de sempre (custo_total, margem_valor) não mudam.
    custo_bonificacoes = sum(r["custo_bonificacoes"] for r in resumos)
    custo_devolucoes = sum(r["custo_devolucoes"] for r in resumos)
    cmv_liquido = custo_total + custo_bonificacoes - custo_devolucoes
    receita_liquida_com_custo = com_custo - sum(r["devolucoes_com_custo"] for r in resumos)
    margem_liquida = receita_liquida_com_custo - cmv_liquido
    avisos = _avisos(resumos)
    if not implausiveis.empty:
        avisos.append(f"{implausiveis.shape[0]} produto(s) com custo a partir de {RAZAO_CUSTO_IMPLAUSIVEL} vezes a "
                      "venda ficaram fora da perda e do potencial recuperável: confira no cadastro a unidade ou "
                      "a embalagem do custo.")
    return {
        "versao": VERSAO_REGRAS,
        "arquivos": [{
            "indice": r["indice"], "rotulo": _rotulo(r), "linhas": r["linhas"],
            "tem_custo": r["tem_custo"], "tem_preco_unitario": r["tem_preco_unitario"],
        } for r in resumos],
        "fatias": [_fatia(r, fora_por_arquivo.get(r["indice"]))
                   for r in sorted(resumos, key=lambda r: (r["inicio"] is None, r["inicio"] or date.min, r["indice"]))],
        "saude": {
            "indice": round(100 * auditavel / faturamento, 2),
            "faturamento_total": _dinheiro(faturamento),
            "faturamento_sem_custo": _dinheiro(faturamento - com_custo),
            "pct_faturamento_sem_custo": _fracao(faturamento - com_custo, faturamento),
            "itens_vendidos": int(produtos.shape[0]),
            "itens_sem_custo": int(sem_custo.sum()),
            "pct_itens_sem_custo": _fracao(int(sem_custo.sum()), int(produtos.shape[0])),
            "arquivos_com_custo": sum(1 for r in resumos if r["tem_custo"]),
            "devolucoes": _dinheiro(sum(r["devolucoes"] for r in resumos)),
            "inconsistencias": {
                "codigo_com_varios_nomes": len(set().union(*(r["_codigos_conflito"] for r in resumos))),
                "nome_com_varios_codigos": len(set().union(*(r["_nomes_conflito"] for r in resumos))),
                "estoque_negativo": len(set().union(*(r["_estoque_negativo"] for r in resumos))),
                "venda_sem_valor": sum(r["venda_sem_valor"] for r in resumos),
                "itens_margem_negativa": int(margem_negativa.sum()),
            },
        },
        "scanner": {
            "variacao_visivel": variacao_visivel,
            "itens_com_variacao": int(com_variacao.shape[0]),
            "faturamento_envolvido": _dinheiro(com_variacao["faturamento"].sum()),
            "vazamento_preco": _dinheiro(com_variacao["vazamento"].sum()),
            "itens": [_item(linha, ("linhas", "quantidade", "faturamento", "preco_min", "preco_max",
                                    "preco_referencia", "referencia_tabela", "vazamento", "custo_medio",
                                    "margem_valor", "margem_percentual"))
                      for _, linha in com_variacao.head(LIMITE_ITENS_LISTA).iterrows()],
        },
        "auditoria": {
            "faturamento_com_custo": _dinheiro(com_custo),
            "custo_total": _dinheiro(custo_total) if custo_total > 0 else None,
            "margem_valor": _dinheiro(margem) if custo_total > 0 else None,
            "margem_percentual": _fracao(margem, com_custo) if custo_total > 0 else None,
            "perda_abaixo_custo": _dinheiro(max(0.0, produtos["perda"].sum() - perda_fora)),
            "itens_venda_abaixo_custo": int(abaixo.shape[0]),
            "custo_a_confirmar": {
                "itens": int(implausiveis.shape[0]),
                "faturamento_com_custo": _dinheiro(implausiveis["faturamento_com_custo"].sum()),
                "custo_total": _dinheiro(implausiveis["custo_total"].sum()),
                "perda_desconsiderada": _dinheiro(perda_fora),
                "lista": [_item(linha, ("quantidade_com_custo", "faturamento_com_custo", "custo_total", "custo_medio"))
                          for _, linha in implausiveis.head(LIMITE_ITENS_LISTA).iterrows()],
            },
            "itens_margem_negativa": int(margem_negativa.sum()),
            "itens_abaixo_custo": [_item(linha, ("quantidade_abaixo", "faturamento_abaixo", "custo_abaixo", "perda"))
                                   for _, linha in abaixo.head(LIMITE_ITENS_LISTA).iterrows()],
            "margem_por_item": [_item(linha, ("quantidade_com_custo", "faturamento_com_custo", "custo_total",
                                              "custo_medio", "margem_valor", "margem_percentual"))
                                for _, linha in com_margem.head(LIMITE_ITENS_LISTA).iterrows()],
            "precificacao_visivel": precificacao_visivel,
            "precificacao": precificacao,
            "custo_bonificacoes": _dinheiro(custo_bonificacoes),
            "custo_devolucoes": _dinheiro(custo_devolucoes),
            "cmv_liquido": _dinheiro(cmv_liquido) if custo_total > 0 else None,
            "margem_liquida_valor": _dinheiro(margem_liquida) if custo_total > 0 else None,
            "margem_liquida_percentual": _fracao(margem_liquida, receita_liquida_com_custo) if custo_total > 0 else None,
        },
        "recuperavel": {
            "total": _dinheiro(max(0.0, produtos["recuperavel"].sum() - recuperavel_fora)),
            "abaixo_custo": _dinheiro(max(0.0, produtos["perda"].sum() - perda_fora)),
            "variacao_preco": _dinheiro(produtos["vazamento"].sum()),
        },
        "periodo": {
            "inicio": inicio.isoformat() if inicio else None,
            "fim": fim.isoformat() if fim else None,
            "meses": ((fim.year - inicio.year) * 12 + fim.month - inicio.month + 1) if inicio else None,
        },
        "avisos": avisos,
    }


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
        motivos.append("Os relatórios enviados não trazem o custo dos produtos: sem ele, a margem não pode "
                       "ser auditada.")
    elif (saude["pct_faturamento_sem_custo"] or 0) >= LIMIAR_SEM_CUSTO:
        motivos.append(f"{_brl(100 * saude['pct_faturamento_sem_custo'])}% do faturamento saiu sem custo "
                       "informado no cadastro.")
    negativos = saude["inconsistencias"]["itens_margem_negativa"]
    if negativos:
        motivos.append(f"{negativos} item(ns) com margem negativa: vendidos, no período, abaixo do custo "
                       "informado.")
    faturamento = saude["faturamento_total"]
    if faturamento > 0 and recuperavel["total"] / faturamento >= LIMIAR_RECUPERAVEL:
        motivos.append(f"Potencial recuperável de R$ {_brl(recuperavel['total'])} em vendas abaixo do custo ou "
                       "abaixo do preço de referência.")
    if not motivos:
        return {"plano": "BRONZE", "motivos": [
            "Cadastro com custo informado e sem perdas relevantes: o acompanhamento do Bronze cobre o que o "
            "Raio-X encontrou."]}
    meses = resultado["periodo"]["meses"]
    if meses_prata and meses and meses > meses_prata:
        motivos.append(f"O histórico enviado tem {meses} meses, acima dos {meses_prata} que o Prata mantém.")
        return {"plano": "OURO", "motivos": motivos}
    return {"plano": "PRATA", "motivos": motivos}
