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

from collections import Counter, namedtuple
from calendar import monthrange
from datetime import date, timedelta
import math
import re

import numpy as np
import pandas as pd

from etl_inputs import ETLInputError
import indicadores_executivos
from etl_processor import (
    ETLLeiauteAmbiguo, ETLRelatorioDeCompras, ETLValidationError, _converter_serie_monetaria,
    _extrair_periodo_base, _ler_bruto, _nome_exibivel, _normalizar_produto, _reais as _brl, _vendas_do_bruto,
    data_da_linha, ler_tabela_auxiliar, ler_tabela_pelo_titulo, normalizar_categoria, normalizar_codigo_do_produto,
    normalizar_ncm, normalizar_numero_do_documento, papel_anunciado_pelo_titulo, resumo_de_vendas_sem_produto,
)

# 11.4: saneamento de 11/2026 (rodapés, quantidades, títulos de reserva, codificação e os campos de
# CMV líquido). Raio-X gravado com a 11.3 não tem os campos líquidos (raio_x_servico.visao).
# 11.5: unificação de 11/2026. O item de custo implausível sai da perda e do recuperável (a regra que a
# triagem do lead já tinha), a leitura passa a aceitar os formatos e o leiaute paginado do ERP, e o total
# informado com fração de centavo fecha com a soma do relatório (_fechar_centavos).
# 11.6: indicadores executivos de 11/2026. O resultado ganha o nó indicadores_executivos (21 indicadores,
# indicadores_executivos.py), e o Raio-X do cliente também fatia por mês a venda item a item com a data de cada
# venda (sem recusar a data inconsistente: o arquivo continua um relatório só).
VERSAO_REGRAS = "raio-x-2026-11.6"
MAX_ARQUIVOS = 36
# Relatórios do envio depois de fatiar por mês o arquivo que traz a data de cada venda: 10 anos de meses. O
# teto de arquivos enviados continua sendo MAX_ARQUIVOS.
MAX_FATIAS = 120
_MAIS_QUE_O_TETO_DE_FATIAS = (f"Os relatórios enviados somam mais de {MAX_FATIAS} meses. Envie no máximo 10 anos "
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
    "indicadores_executivos",
})
# O Raio-X gravado antes da 11.6 não tem os indicadores: a leitura do banco (raio_x_servico.ultimo) exige só estas.
CHAVES_OBRIGATORIAS = CHAVES_RESULTADO - {"indicadores_executivos"}
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
    lido = _ler_arquivo_do_envio(caminho, numero, cadastro)
    return lido.df, lido.formato, lido.periodo, lido.rotulo


# Um arquivo do envio: o relatório de vendas (df) ou, na leitura de cadastro, o que ele é quando não é venda (a tabela
# auxiliar de etl_processor.ler_tabela_pelo_titulo ou ler_tabela_auxiliar, o relatório de compras, o resumo de vendas
# sem produto ou o relatório de vendas de leiaute ambíguo), com a recusa que vale quando o envio não o aceita
# (_ler_envio_uma_vez).
_LidoDoEnvio = namedtuple("_LidoDoEnvio", "df formato periodo rotulo tabela recusa")
_SEM_PRODUTO = ("{rotulo}: não foi possível identificar produto, quantidade e valor de venda. Envie o relatório de "
                "vendas por produto (CSV, XLSX ou XLS).")


def _recusa_da_tabela(rotulo: str, tabela: dict) -> ETLInputError:
    """A recusa da tabela auxiliar num envio sem relatório de vendas: ela nunca passa pela trava de produto, quantidade
    e valor de venda, e diz o que o arquivo é."""
    return ETLInputError("raio_x_dados", f"{rotulo}: o arquivo é {tabela['artigo']} {tabela['rotulo']}, uma tabela "
                         "auxiliar que só completa a venda item a item com a data de cada venda (a Opção 27 do ERP), e "
                         "não um relatório de vendas. Envie as duas juntas, ou envie o relatório de vendas por produto "
                         "(CSV, XLSX ou XLS).")


def _ler_arquivo_do_envio(caminho: str, numero: int, cadastro: bool, *, auxiliar: bool = False) -> _LidoDoEnvio:
    """O arquivo lido. Com `auxiliar` (a leitura de cadastro), a classificação vem antes da leitura de vendas: o título
    do relatório que anuncia uma tabela auxiliar (posição de estoque, inventário, lista de preços) a lê como tal
    (etl_processor.ler_tabela_pelo_titulo). Depois, o arquivo que não é relatório de vendas e é reconhecido volta com a
    tabela e a recusa dele, em vez de recusar já: a tabela auxiliar pelas colunas (o resumo por documento), o relatório
    de compras, o resumo de vendas sem produto e o relatório de vendas de leiaute ambíguo; o que fazer com cada um
    decide _ler_envio_uma_vez. O arquivo que não é nada disso é recusado na hora, como antes: o relatório por produto
    sem uma das colunas (corrompido) continua com a recusa de produto, quantidade e valor de venda."""
    rotulo = f"Arquivo {numero + 1}"
    bruto, periodo = None, None
    try:
        bruto = _ler_bruto(caminho)
        periodo = _extrair_periodo_base(bruto) if not bruto.empty else None
        if auxiliar and not bruto.empty:
            tabela = ler_tabela_pelo_titulo(bruto, rotulo.lower())
            if tabela is not None:
                return _LidoDoEnvio(None, None, periodo, rotulo, tabela, _recusa_da_tabela(rotulo, tabela))
        df = _vendas_do_bruto(bruto, rotulo.lower(), cadastro=cadastro)
    except ETLLeiauteAmbiguo as erro:
        recusa = ETLInputError("raio_x_dados", f"{rotulo}: {erro.orientacao}")
        if auxiliar:
            papel = "compras" if isinstance(erro, ETLRelatorioDeCompras) else "ambiguo"
            tabela = {"papel": papel, "produtos": None, "documentos": None,
                      "pelo_titulo": bool(getattr(erro, "pelo_titulo", False))}
            return _LidoDoEnvio(None, None, periodo, rotulo, tabela, recusa)
        raise recusa from None
    except ETLValidationError as erro:
        if erro.code != "data":
            raise  # limite de tamanho, linhas ou células: a mensagem do ETL já é segura
        recusa = ETLInputError("raio_x_dados", _SEM_PRODUTO.format(rotulo=rotulo))
        tabela = None
        if auxiliar and bruto is not None:
            try:
                tabela = ler_tabela_auxiliar(bruto, rotulo.lower())
            except ETLValidationError:
                tabela = None
            if tabela is None and resumo_de_vendas_sem_produto(bruto):
                tabela = {"papel": "resumo_de_vendas", "produtos": None, "documentos": None}
            if tabela is None:
                # Nada leu o arquivo. Com o título de uma tabela auxiliar (a posição de estoque só com os valores), ele
                # é recusado dizendo o que falta, e o lote com venda o deixa fora das contas. Sem esse título, é o
                # relatório de vendas que não foi lido: o lote só o deixa fora das contas com o período dentro do da
                # âncora, que já traz a venda dele (_ler_envio_uma_vez); senão, a recusa de sempre.
                anunciado = papel_anunciado_pelo_titulo(bruto)
                if anunciado is not None:
                    rotulo_do_aviso, texto_da_recusa = _ILEGIVEL[anunciado]
                    tabela = {"papel": "ilegivel", "rotulo": rotulo_do_aviso, "produtos": None, "documentos": None}
                    recusa = ETLInputError("raio_x_dados", texto_da_recusa.format(rotulo=rotulo))
                else:
                    tabela = {"papel": "nao_reconhecido", "produtos": None, "documentos": None}
                return _LidoDoEnvio(None, None, periodo, rotulo, tabela, recusa)
        if tabela is None:
            raise recusa from None
        if tabela["papel"] != "resumo_de_vendas":
            recusa = _recusa_da_tabela(rotulo, tabela)
        return _LidoDoEnvio(None, None, periodo, rotulo, tabela, recusa)
    _marcar_devolucoes_sem_situacao(df)
    _fechar_centavos(df)
    return _LidoDoEnvio(df, bruto.attrs.get("format"), periodo, rotulo, None, None)


def _marcar_devolucoes_sem_situacao(df: pd.DataFrame) -> None:
    """Sem a coluna de situação, a linha com quantidade e valor negativos é a devolução (ou a troca): ela entra nas
    devoluções, no CMV líquido e no faturamento líquido de devolução, em vez de sumir das contas (auditoria matemática
    de 11/2026). Com a coluna, vale o que ela diz (etl_processor)."""
    if df.empty or "_devolucao" in df.columns:
        return
    negativas = (df["quantidade"] < 0) & (df["valor_venda"] < 0)
    if negativas.any():
        df["_devolucao"] = negativas.to_numpy(bool)


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


def _fatias_mensais(df: pd.DataFrame, periodo, rotulo: str, ate=None, *, recusar: bool = True, item_a_item=None):
    """[(vendas do mês, (início, fim))] quando o relatório traz a data de cada venda e o período do
    cabeçalho passa de um mês; senão None (o arquivo é um relatório só, como sempre).

    Cada mês do calendário do período do cabeçalho vira um relatório, cortado pelo período, COM venda ou
    sem: o mês sem venda continua sendo período coberto (os dias dele contam no ritmo, e outro arquivo do
    mesmo mês continua sendo sobreposição). `ate` (hoje, na Avaliação de Cenário) dispensa os meses sem
    venda que ainda não começaram.

    Data ilegível em parte das linhas, fora do período do cabeçalho ou em mês que ainda não começou: o
    arquivo de um ano só continua um relatório, como era lido antes de existirem as fatias; o que cruza o
    ano, que a Avaliação de Cenário recusaria de qualquer jeito, é recusado aqui, com o motivo. Com
    `recusar=False` (o Raio-X do cliente, que sempre aceitou esses arquivos), ele continua um relatório só, e
    o mesmo vale para o arquivo de mais de MAX_FATIAS meses. `item_a_item`: o resultado já calculado de
    _e_venda_item_a_item para o arquivo."""
    if "_data" not in df.columns or periodo is None:
        return None
    inicio, fim = periodo
    if (inicio.year, inicio.month) == (fim.year, fim.month):
        return None
    # Só a venda item a item é fatiada: num resumo por produto, a coluna "Data" é outra coisa, e o arquivo
    # continua uma fatia só.
    if not (_e_venda_item_a_item(df) if item_a_item is None else item_a_item):
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
        if inicio.year == fim.year or not recusar:
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
        if not recusar:
            return None
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


def _periodo_das_datas(df: pd.DataFrame):
    """(primeira, última) data das linhas, quando toda linha tem a data legível, ou None: o período da venda item a
    item que não traz a linha do período no cabeçalho, para a cobertura da âncora (_ler_envio_uma_vez)."""
    if "_data" not in df.columns or df.empty:
        return None
    datas = df["_data"].map({texto: data_da_linha(texto) for texto in df["_data"].unique()})
    if datas.isna().any():
        return None
    return datas.min(), datas.max()


def _datas_validas(df: pd.DataFrame, periodo, ate=None) -> bool:
    """A coluna de data é a data de cada venda, para os indicadores (última venda, mapa por dia da semana,
    recompra por dia): toda linha com data legível, dentro do período do cabeçalho e em mês que já começou. Quem
    chama confere também que o relatório é venda item a item: num resumo, "Data" é outra coisa."""
    if "_data" not in df.columns or periodo is None:
        return False
    datas = df["_data"].map({texto: data_da_linha(texto) for texto in df["_data"].unique()})
    if datas.isna().any():
        return False
    inicio, fim = periodo
    if (datas < inicio).any() or (datas > fim).any():
        return False
    return ate is None or not (datas.map(lambda dia: dia.replace(day=1)) > ate).any()


def _resumir(df: pd.DataFrame, formato, periodo, rotulo: str, indice: int, cadastro: bool, *,
             datas_validas: bool = False):
    """(produtos, resumo, produtos em conflito) de um relatório já lido, ou de uma fatia mensal dele.
    `datas_validas`: a data de cada linha é a da venda (_datas_validas), para os indicadores."""
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
        # As linhas do relatório: as que a ingestão em lote acrescentou (saldo e cadastro) não contam.
        "linhas": int(len(df) - (df["_auxiliar"].astype(bool).sum() if "_auxiliar" in df.columns else 0)),
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
    # Indicadores executivos: o que só existe linha a linha (número da venda, cliente, desconto, data e hora).
    resumo["_linhas_venda"] = int(venda.sum())
    # O custo unitário que a ingestão em lote trouxe para os produtos sem venda (raio_x._completar_ancora).
    resumo["_custo_unitario_auxiliar"] = None
    if "_custo_auxiliar" in df.columns:
        com_custo = df["_custo_auxiliar"].notna().to_numpy()
        resumo["_custo_unitario_auxiliar"] = dict(zip(df.loc[com_custo, "_prod_norm"],
                                                      df.loc[com_custo, "_custo_auxiliar"].astype(float))) or None
    resumo.update(indicadores_executivos.resumo_das_linhas(df, venda, v["faturamento"], csv=formato == "csv",
                                                           datas_validas=datas_validas))
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


def _varias_lojas(df: pd.DataFrame, linhas=None) -> bool:
    """O relatório (nas `linhas`, uma máscara, quando dadas) traz duas lojas ou mais com nome. A célula da loja
    em branco não é loja: com uma loja só, ela é dessa loja."""
    if "_unidade" not in df.columns:
        return False
    unidades = df["_unidade"] if linhas is None else df.loc[linhas, "_unidade"]
    lojas = unidades.map(_texto_da_unidade)
    return lojas[lojas != ""].nunique() >= 2


def _varias_lojas_do_cupom(df: pd.DataFrame) -> bool:
    """A loja do cupom (etl_processor._loja_do_cupom) traz duas lojas ou mais com nome: a loja que só aparecia no
    item cancelado do cupom também conta para a numeração das vendas."""
    if "_loja_do_cupom" not in df.columns:
        return False
    lojas = df["_loja_do_cupom"].astype(str)
    return lojas[lojas != ""].nunique() >= 2


def _marcar_as_lojas(df: pd.DataFrame) -> None:
    """Decide no arquivo inteiro, antes das fatias mensais (que copiam os attrs), se ele tem duas lojas ou mais
    com nome: em todas as linhas (a numeração das vendas, nos indicadores) e nas linhas com saldo (o estoque).
    Decidida na fatia, valeria só o mês: no mês em que uma loja só vendeu, o saldo da outra se perderia."""
    df.attrs["varias_lojas"] = _varias_lojas(df) or _varias_lojas_do_cupom(df)
    df.attrs["varias_lojas_com_saldo"] = ("_estoque_informado" in df.columns
                                          and _varias_lojas(df, df["_estoque_informado"].to_numpy(bool)))


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
        # Por produto e loja, quando as linhas com saldo do ARQUIVO trazem duas lojas ou mais com nome (decidido
        # no arquivo inteiro, antes das fatias mensais: _marcar_as_lojas): o estoque da rede é a soma do último
        # estoque de cada loja (o da última linha seria o de uma loja só). A loja vem das linhas com saldo, e não
        # só das de venda: o depósito que só traz o saldo, com quantidade 0, também soma; nessa soma, a linha sem
        # loja conta como uma loja "sem nome". Sem a coluna de loja, ou com uma loja só, é o último saldo do
        # produto, como sempre: a linha com a célula da loja em branco (mesclada, ou preenchida só na primeira
        # linha do grupo ou do cupom) é dessa loja, e não uma segunda.
        varias_lojas = df.attrs.get("varias_lojas_com_saldo")
        if varias_lojas is None:
            varias_lojas = _varias_lojas(df, informado.to_numpy(bool))
        if varias_lojas:
            unidade = df.loc[informado, "_unidade"].map(_texto_da_unidade)
        else:
            unidade = pd.Series("", index=df.index[informado.to_numpy(bool)], dtype=object)
        por_loja = df.loc[informado, "estoque"].groupby([df.loc[informado, "_prod_norm"], unidade]).last()
        estoques = por_loja.groupby(level=0).sum()
        # O saldo de cada produto e loja, para _ler_envio acumular nas fatias mensais do arquivo.
        resumo["_estoques_por_loja"] = por_loja
        # Só os produtos com venda: com a tabela vazia (arquivo ou mês sem venda válida), a atribuição direta
        # adotaria o índice da série e criaria linhas de produto sem venda, com as somas vazias.
        produtos["estoque"] = estoques.reindex(produtos.index)
        # Todos os produtos com saldo informado no arquivo, inclusive os que não venderam nele: o motor do
        # lead usa o saldo mais recente do ano (o relatório "vendas x estoque" lista o item parado).
        resumo["_estoques"] = estoques
        # O nome de cada um, para os indicadores de estoque (o item parado não está entre os vendidos).
        resumo["_nomes_estoque"] = (df.loc[informado, "produto"].groupby(df.loc[informado, "_prod_norm"]).last()
                                    .map(_nome_exibivel).to_dict())
    # Bonificação e devolução por produto, inclusive de produto sem venda no arquivo: as mesmas máscaras
    # dos totais do resumo (custo_bonificacoes, custo_devolucoes e devolucoes_com_custo). O valor e a quantidade de
    # toda devolução vão também para as curvas ABC líquidas dos indicadores (indicadores_executivos.calcular).
    devolvidas = (df["_devolucao"].astype(bool) if "_devolucao" in df.columns
                  else pd.Series(False, index=df.index))
    quantidade = pd.to_numeric(df["quantidade"], errors="coerce").fillna(0.0)
    ajustes = pd.DataFrame({
        "custo_bonificacoes": custo_linha.where(bonificadas, 0.0),
        "custo_devolucoes": custo_linha.where(devolvidas_com_custo, 0.0),
        "devolucoes_com_custo": valor.abs().round(2).where(devolvidas_com_custo, 0.0),
        "devolucoes": valor.abs().round(2).where(devolvidas, 0.0),
        "quantidade_devolvida": quantidade.abs().where(devolvidas, 0.0),
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
    # Os da ingestão em lote (o papel de cada arquivo auxiliar e o que ele completou na âncora).
    for r in resumos:
        avisos.extend(r.get("_avisos_do_lote") or ())
    # As fatias mensais de um mesmo arquivo repetem o aviso: fica um por arquivo.
    return list(dict.fromkeys(avisos))


def diagnosticar(caminhos: list) -> dict:
    """Lê os relatórios de vendas (na ordem recebida) e devolve o Raio-X combinado. Desde a 11.6, com a
    leitura de cadastro (a data de cada venda, o estoque informado) e as fatias mensais da venda item a item
    (refatoração 4 dos indicadores), sem recusar a data inconsistente."""
    return _combinar(*_ler_envio(caminhos, cadastro=True, ate=date.today(), recusar_datas=False))


class _AcimaDoTetoDeFatias(Exception):
    """O envio do Raio-X do cliente passou do teto de fatias: _ler_envio lê de novo, sem fatiar arquivo nenhum."""


def _ler_envio(caminhos: list, *, cadastro: bool = False, ate=None, recusar_datas: bool = True):
    """(por_arquivo, resumos, envolvidos): cada relatório lido uma vez por _ler_arquivo e os
    agregados por produto e arquivo em ordem cronológica estável (arquivo sem período conta como
    o mais antigo). O Raio-X do cliente e a Avaliação de Cenário (avaliacao_motor) partem daqui com
    `cadastro=True` (ver _ler_arquivo) e `ate` igual a hoje (ver _fatias_mensais); só a avaliação recusa a
    data inconsistente e o envio acima de MAX_FATIAS meses (`recusar_datas`). No Raio-X do cliente, o envio
    que passaria do teto é lido de novo sem fatiar nenhum arquivo, como antes das fatias: o resultado não
    depende da ordem dos arquivos (é o caso raro de mais de 10 anos de venda item a item, e só ele lê duas
    vezes). No teto, o arquivo que não é fatiado conta como um relatório."""
    if not 1 <= len(caminhos) <= MAX_ARQUIVOS:
        raise ETLInputError("raio_x_arquivos", f"Envie de 1 a {MAX_ARQUIVOS} relatórios de vendas.")
    try:
        return _ler_envio_uma_vez(caminhos, cadastro=cadastro, ate=ate, recusar_datas=recusar_datas, fatiar=True)
    except _AcimaDoTetoDeFatias:
        pass  # a segunda leitura fica fora do except: o quadro da primeira (as fatias já lidas) é liberado antes
    return _ler_envio_uma_vez(caminhos, cadastro=cadastro, ate=ate, recusar_datas=recusar_datas, fatiar=False)


# ---------------------------------------------------------------------------
# Ingestão em lote: a âncora temporal e as tabelas auxiliares
# ---------------------------------------------------------------------------
# A âncora é a venda item a item com a data de cada venda (_datas_validas; a Opção 27 do ERP, "Vendas por Cliente
# Detalhada por Produto", com a "Data Ven."). Com ela no envio, os outros arquivos deixam de ser vendas e a completam:
#   - o relatório de vendas por produto sem data por linha, dentro do período da âncora (ou sem período), dá o
#     custo unitário do período (custo das vendas / quantidade com custo), o último saldo e o cadastro, e não soma
#     no faturamento (o resumido do ERP conta o pedido e a nota em dobro: o custo unitário não muda com isso);
#   - a posição de estoque e o inventário dão o saldo e o custo de cada produto; a lista de preços, o cadastro (o
#     preço dela não entra nas contas);
#   - o resumo por pedido ou documento dá o desconto (o percentual vira reais), o CPF/CNPJ do cliente e a hora de
#     cada venda;
#   - o relatório de compras, o resumo de vendas sem produto (por cliente, fornecedor, grupo ou forma de pagamento)
#     e o relatório de vendas de leiaute ambíguo do período da âncora entram no envio, mas ficam fora das contas.
# O cruzamento é um left join: a âncora é a tabela primária. O produto casa pelo código quando a âncora e as tabelas
# o trazem (só letras e algarismos, sem os zeros à esquerda) e, sem ele, pelo nome normalizado, a chave de produto do
# motor inteiro (a Opção 27 real não tem a coluna de código). A venda casa pelo número (numero_da_venda, sem os zeros
# à esquerda de cada grupo). O faturamento depois do cruzamento tem de ser o da âncora, no centavo: senão, o envio é
# recusado com _FALHA_DE_CONTENCAO. O custo e o estoque só completam a âncora que não os traz, e o desconto, o cliente
# e a hora, a âncora sem a coluna. Sem a âncora no envio, cada relatório de vendas soma, como antes; a tabela
# auxiliar e o resumo de vendas sem produto ficam fora das contas quando o envio tem relatório de vendas, e recusam o
# envio quando não tem (a tabela auxiliar com a recusa dela, nunca com a de produto, quantidade e valor de venda). O
# relatório de compras pelo título também fica fora das contas (diretriz de 11/2026, "jogue tudo e processe"); o de
# compras só pelas colunas e o de leiaute ambíguo continuam recusados, como antes.
_FALHA_DE_CONTENCAO = "Falha de Contenção de Dados: Duplicação de Faturamento Detectada"
# O resumo por documento só vale quando casa com quase toda a venda da âncora.
_CASAMENTO_MINIMO_DOS_DOCUMENTOS = 0.9
# O aviso do papel de cada arquivo do lote com a âncora: o que ele completou, ou que ficou fora das contas.
_PAPEIS_DO_LOTE = {
    "vendas": "entrou como tabela auxiliar (custo, estoque e cadastro por produto) da venda item a item e não soma "
              "no faturamento",
    "estoque": "entrou como {rotulo} (tabela auxiliar: o saldo e o custo de cada produto)",
    "precos": "entrou como lista de preços (o cadastro dos produtos; o preço não entra nas contas)",
    "documentos": "entrou como resumo por pedido ou documento (tabela auxiliar)",
    "compras": "é um relatório de compras: entrou no envio, mas fica fora das contas (o resumo por nota não traz o "
               "produto)",
    "resumo_de_vendas": "é um resumo de vendas sem produto (por cliente, fornecedor, grupo ou forma de pagamento): "
                        "entrou no envio, mas fica fora das contas",
    "ambiguo": "é um relatório de vendas de leiaute ambíguo (dois títulos de coluna iguais para um valor, ou o "
               "separador duvidoso), do período da venda item a item: entrou no envio, mas fica fora das contas",
    "ilegivel": "tem o título de {rotulo}: entrou no envio, mas fica fora das contas",
    "nao_reconhecido": "não foi reconhecido como relatório de vendas por produto (não foi possível identificar "
                       "produto, quantidade e valor de venda) nem como tabela auxiliar, e o período dele está no da "
                       "venda item a item com a data de cada venda, que já traz a venda dele: entrou no envio, mas fica "
                       "fora das contas",
}
# O arquivo com o título de uma tabela auxiliar que não pôde ser lido como ela: o rótulo do aviso do lote e a recusa.
_ILEGIVEL = {
    "estoque": ("posição de estoque, mas não foi possível ler o saldo de cada produto (a coluna da quantidade em "
                "estoque)",
                "{rotulo}: o arquivo tem o título de posição de estoque ou de inventário, mas não foi possível ler o "
                "saldo de cada produto: falta a coluna do saldo (a quantidade em estoque), ou ela vem misturada com a "
                "venda. Envie a posição de estoque com o produto e o saldo."),
    "precos": ("lista de preços, mas não foi possível ler o preço de cada produto",
               "{rotulo}: o arquivo tem o título de lista de preços, mas não foi possível ler o preço de cada produto. "
               "Envie a lista com o produto e o preço."),
}
_PAPEIS_QUE_COMPLETAM = frozenset({"vendas", "estoque", "precos", "documentos"})
# Sem a âncora, com relatório de vendas no envio, a tabela auxiliar fica fora das contas.
_TABELA_FORA_SEM_ANCORA = ("é {artigo} {rotulo}, uma tabela auxiliar que só completa a venda item a item com a data de "
                           "cada venda (a Opção 27 do ERP): entrou no envio, mas fica fora das contas")


# O relatório por produto com a loja de cada venda, absorvido pela âncora que não traz a loja: ele é tomado como da
# mesma venda (a Opção 27 da rede toda), e a venda das lojas que a âncora não tem ficaria de fora sem ninguém saber.
_AVISO_DAS_LOJAS = ("O arquivo {numero} traz a loja de cada venda, e a venda item a item com a data de cada venda não "
                    "traz: ele foi tomado como da mesma venda, e não soma no faturamento. Se a venda item a item é de "
                    "uma loja só, a das outras lojas ficou de fora: envie a venda item a item de todas as lojas.")


def _aviso_do_papel(numero: int, tabela: dict, *, com_ancora: bool) -> str:
    """O aviso do arquivo `numero` (do envio, a partir de 0) que não é venda somada."""
    if com_ancora or tabela["papel"] not in ("estoque", "precos", "documentos"):
        texto = _PAPEIS_DO_LOTE[tabela["papel"]].format(rotulo=tabela.get("rotulo", ""))
    else:
        texto = _TABELA_FORA_SEM_ANCORA.format(artigo=tabela["artigo"], rotulo=tabela["rotulo"])
    return f"O arquivo {numero + 1} {texto}."


# A venda item a item que, num período longo, vendeu uma parte pequena dos produtos com saldo no estoque enviado (o da
# loja inteira) parece filtrada, por poucos clientes ou por um vendedor (diretriz de 11/2026): o giro, a cobertura, o
# GMROI e os encalhados comparariam a venda parcial com o estoque inteiro.
_PREFIXO_DA_AMOSTRA_PARCIAL = "Amostra parcial:"
_AMOSTRA_PARCIAL_FRACAO = 0.15
# Em um ano ou mais, a loja inteira vende bem mais do que isso do que tem em estoque: o limite sobe para 30%. No lote
# real de 11/2026, a venda de 20 clientes em 1.411 dias vendeu 113 dos 636 produtos com saldo (17,8%).
_AMOSTRA_PARCIAL_FRACAO_ANUAL = 0.30
_AMOSTRA_PARCIAL_DIAS = 90
_AMOSTRA_PARCIAL_DIAS_ANUAL = 365
_AMOSTRA_PARCIAL_MINIMO_DE_PRODUTOS = 20


def _aviso_de_amostra_parcial(saldos: pd.Series, chaves_vendidas: pd.Series, periodo, numero: str):
    """O aviso de que a venda é parcial diante do estoque inteiro, ou None: com 20 produtos ou mais com saldo positivo
    nas tabelas de estoque, a âncora de 90 dias ou mais vendeu menos de 15% deles (a de um ano ou mais, menos de 30%)."""
    com_saldo = set(saldos[saldos > 0].index)
    if periodo is None or len(com_saldo) < _AMOSTRA_PARCIAL_MINIMO_DE_PRODUTOS:
        return None
    dias = (periodo[1] - periodo[0]).days + 1
    if dias < _AMOSTRA_PARCIAL_DIAS:
        return None
    vendidos = len(com_saldo & set(chaves_vendidas.dropna()))
    limite = _AMOSTRA_PARCIAL_FRACAO_ANUAL if dias >= _AMOSTRA_PARCIAL_DIAS_ANUAL else _AMOSTRA_PARCIAL_FRACAO
    if vendidos >= limite * len(com_saldo):
        return None
    def milhar(numero_inteiro: int) -> str:
        return f"{numero_inteiro:,}".replace(",", ".")

    return (f"{_PREFIXO_DA_AMOSTRA_PARCIAL} a venda do arquivo {numero} tem {milhar(vendidos)} dos "
            f"{milhar(len(com_saldo))} produtos com saldo no estoque enviado ({vendidos / len(com_saldo):.0%}), em "
            f"{milhar(dias)} dias. Ela parece filtrada (por "
            "cliente, vendedor ou loja), e o estoque é o da loja inteira: o giro, a cobertura, o GMROI, a ruptura e os "
            "encalhados comparam essa venda parcial com o estoque inteiro e não representam a loja.")


def _aviso_do_saldo(fonte: dict):
    """O aviso de onde veio o saldo quando o envio tem mais de uma tabela de estoque (_saldo_das_tabelas_de_estoque),
    ou None. Os números dos arquivos saem em ordem."""
    principais, completaram = list(fonte.get("principais") or []), sorted(fonte.get("completaram") or [])
    fora = sorted(fonte.get("fora") or [])
    if len(principais) + len(completaram) + len(fora) < 2:
        return None
    if fonte.get("lojas", 0) >= 2:
        partes = [f"O saldo de estoque soma as tabelas de {fonte['lojas']} lojas diferentes "
                  f"({_lista_de_arquivos(sorted(principais))})"]
        sujeito = "elas"
    else:
        partes = [f"O saldo de estoque veio d{_lista_de_arquivos(principais[:1])}"]
        sujeito = "ele"
    if completaram:
        partes.append(f"{_lista_de_arquivos(completaram)} só {'completaram' if len(completaram) > 1 else 'completou'} "
                      f"os produtos que {sujeito} não {'trazem' if sujeito == 'elas' else 'traz'}")
    if fora:
        partes.append(f"{_lista_de_arquivos(fora)} {'trazem' if len(fora) > 1 else 'traz'} o saldo dos mesmos produtos")
    final = (": as tabelas de estoque da mesma loja (ou sem a loja no topo) não somam."
             if completaram or fora else ".")
    return "; ".join(partes) + final


def _lista_de_arquivos(ordens: list) -> str:
    """"o arquivo 2" ou "os arquivos 2 e 3" (as ordens a partir de 0)."""
    numeros = [str(ordem + 1) for ordem in ordens]
    if len(numeros) == 1:
        return f"o arquivo {numeros[0]}"
    return f"os arquivos {', '.join(numeros[:-1])} e {numeros[-1]}"


def _lojas_com_nome(df: pd.DataFrame) -> set:
    """As lojas com nome do relatório (a célula em branco não conta)."""
    if "_unidade" not in df.columns:
        return set()
    return {loja for loja in df["_unidade"].map(_texto_da_unidade).unique() if loja}


def _auxiliar_das_vendas(df: pd.DataFrame) -> dict:
    """A tabela auxiliar que um relatório de vendas por produto dá à âncora: o custo e a quantidade com custo de cada
    produto (o custo unitário do período sai da divisão), o último saldo informado, o nome e o código."""
    cabecalhos = df.attrs.get("cabecalhos", {})
    validas = ~df["_devolucao"] if "_devolucao" in df.columns else pd.Series(True, index=df.index)
    quantidade = df["quantidade"]
    if "valor_custo" in df.columns:
        custo = df["valor_custo"] * quantidade if custo_e_unitario(cabecalhos.get("valor_custo", "")) else df["valor_custo"]
    else:
        custo = pd.Series(0.0, index=df.index)
    com_custo = validas & (quantidade > 0) & (custo > 0)
    base = pd.DataFrame({"chave_nome": df["_prod_norm"], "quantidade_com_custo": quantidade.where(com_custo, 0.0),
                         "custo": custo.where(com_custo, 0.0)})
    produtos = base.groupby("chave_nome", sort=False)[["quantidade_com_custo", "custo"]].sum()
    produtos["nome"] = df["produto"].map(_nome_exibivel).groupby(df["_prod_norm"]).last()
    codigos = _codigos(df)
    produtos["codigo"] = (codigos.map(normalizar_codigo_do_produto).groupby(df["_prod_norm"]).first()
                          if codigos is not None else None)
    produtos["estoque"] = np.nan
    if "_estoque_informado" in df.columns and df["_estoque_informado"].any():
        informado = df["_estoque_informado"].to_numpy(bool)
        produtos["estoque"] = df.loc[informado, "estoque"].groupby(df.loc[informado, "_prod_norm"]).last()
    return {"papel": "vendas", "produtos": produtos.reset_index(), "documentos": None}


def _ordem_das_tabelas(ordens: list, meta: dict) -> list:
    """As tabelas de estoque da mais recente para a mais antiga, pela data (o fim do período do cabeçalho, a data
    rotulada ou a emissão); a sem data conta como a mais antiga, como o relatório sem período; no empate, a posição de
    estoque antes do inventário, e depois o primeiro arquivo."""
    def chave(ordem):
        informacao = meta.get(ordem, {})
        return (-(informacao.get("data") or date.min).toordinal(), informacao.get("prioridade", 0), ordem)
    return sorted(ordens, key=chave)


def _saldo_das_tabelas_de_estoque(estoque: pd.DataFrame, chave: str, meta: dict):
    """(saldo por produto, pares (arquivo, produto) que deram o saldo, fonte para o aviso) das tabelas de estoque.
    Dentro de uma tabela, as linhas do mesmo produto somam (os depósitos, os códigos com o mesmo nome). As tabelas se
    agrupam pela loja do topo (etl_processor._loja_do_topo: a filial antes da empresa): com duas lojas ou mais com
    nome, cada loja dá o saldo dela e os saldos das lojas somam, e a tabela sem loja só completa o produto que nenhuma
    traz. Com uma loja só (ou nenhuma com nome), as tabelas são da mesma loja, e somar dobraria o estoque (a posição
    de estoque e o inventário trazem o saldo dos mesmos produtos). Dentro da loja vale a tabela mais recente
    (_ordem_das_tabelas), e as outras só completam o produto que ela não traz."""
    vazio = pd.Series(dtype=float)
    if estoque.empty:
        return vazio, set(), {}
    por_tabela = estoque.groupby(["ordem", chave], sort=False)["estoque"].sum(min_count=1)
    ordens = list(dict.fromkeys(int(ordem) for ordem in estoque["ordem"]))
    por_loja: dict = {}
    for ordem in ordens:
        loja = meta.get(ordem, {}).get("loja")
        if loja:
            por_loja.setdefault(loja, []).append(ordem)
    varias_lojas = len(por_loja) >= 2
    grupos = list(por_loja.values()) if varias_lojas else [ordens]
    sem_loja = [ordem for ordem in ordens if not meta.get(ordem, {}).get("loja")] if varias_lojas else []
    pares, principais, completaram, fora = set(), [], [], []

    def completar(saldo, ordem):
        parte = por_tabela.xs(ordem, level=0)
        novos = parte if saldo is None else parte[~parte.index.isin(saldo.index)]
        if novos.empty:
            fora.append(ordem)
            return saldo
        (principais if saldo is None else completaram).append(ordem)
        pares.update((ordem, produto) for produto in novos.index)
        return novos if saldo is None else pd.concat([saldo, novos])

    saldos = []
    for grupo in grupos:
        saldo_da_loja = None
        for ordem in _ordem_das_tabelas(grupo, meta):
            saldo_da_loja = completar(saldo_da_loja, ordem)
        if saldo_da_loja is not None:
            saldos.append(saldo_da_loja)
    saldo = pd.concat(saldos).groupby(level=0, sort=False).sum(min_count=1) if saldos else vazio
    for ordem in _ordem_das_tabelas(sem_loja, meta):
        saldo = completar(saldo, ordem)
    return saldo, pares, {"principais": principais, "completaram": completaram, "fora": fora,
                          "lojas": len(por_loja) if varias_lojas else 0}


def _custo_ponderado(estoque: pd.DataFrame, chave: str) -> pd.Series:
    """O custo unitário de cada produto nas linhas de estoque, ponderado pelo saldo (o negativo pesa zero)."""
    peso = estoque["estoque"].clip(lower=0).where(estoque["custo_unitario"].notna(), 0.0)
    custo = ((estoque["custo_unitario"].fillna(0.0) * peso).groupby(estoque[chave]).sum()
             / peso.groupby(estoque[chave]).sum())
    return custo.where(np.isfinite(custo) & (custo > 0))


def _combinar_auxiliares(tabelas: list, chave: str) -> tuple[pd.DataFrame, int]:
    """(por produto, pela `chave` "codigo" ou "chave_nome": custo_unitario, estoque, nome e codigo; nomes com mais de
    um código). O custo é o do relatório por produto (o custo médio do período, somado entre os arquivos), senão o das
    tabelas de estoque que deram o saldo do produto (ponderado pelo saldo; as que ficaram fora do saldo só entram para o
    produto sem custo nelas), senão o da lista de preços. O saldo é o das tabelas de estoque
    (_saldo_das_tabelas_de_estoque), senão o último do relatório por produto de período mais recente (no empate, o
    último enviado). attrs["fonte_do_saldo"]: de onde veio o saldo, para o aviso."""
    quadros = []
    for tabela in tabelas:
        produtos = tabela.get("produtos")
        if produtos is not None and not produtos.empty:
            quadros.append(produtos.assign(papel=tabela["papel"], ordem=tabela["ordem"]))
    if not quadros:
        return pd.DataFrame(columns=["custo_unitario", "estoque", "nome", "codigo"]), 0
    todas = pd.concat(quadros, ignore_index=True)
    for coluna in ("quantidade_com_custo", "custo", "custo_unitario", "estoque"):
        if coluna not in todas.columns:
            todas[coluna] = np.nan
    todas = todas[todas[chave].notna()]
    vendas, estoque, precos = (todas[todas["papel"] == papel] for papel in ("vendas", "estoque", "precos"))
    custo_das_vendas = (vendas.groupby(chave)["custo"].sum() / vendas.groupby(chave)["quantidade_com_custo"].sum())
    custo_das_vendas = custo_das_vendas.where(np.isfinite(custo_das_vendas) & (custo_das_vendas > 0))
    saldo_da_posicao, pares, fonte = _saldo_das_tabelas_de_estoque(
        estoque, chave, {tabela["ordem"]: tabela for tabela in tabelas if tabela["papel"] == "estoque"})
    no_saldo = estoque.loc[np.array([(int(ordem), produto) in pares
                                     for ordem, produto in zip(estoque["ordem"], estoque[chave])], dtype=bool)]
    custo_do_estoque = (_custo_ponderado(no_saldo, chave)
                        .combine_first(no_saldo.groupby(chave)["custo_unitario"].mean())
                        .combine_first(_custo_ponderado(estoque, chave))
                        .combine_first(estoque.groupby(chave)["custo_unitario"].mean()))
    custo_dos_precos = precos.groupby(chave)["custo_unitario"].mean()
    # O saldo do relatório por produto é o do de período mais recente (no empate, o último enviado); o relatório sem
    # período conta como o mais antigo, como a tabela de estoque sem data.
    fim_das_vendas = {tabela["ordem"]: tabela.get("data") or date.min for tabela in tabelas
                      if tabela["papel"] == "vendas"}
    com_saldo = vendas.dropna(subset=["estoque"])
    saldo_das_vendas = (com_saldo.assign(_fim=com_saldo["ordem"].map(lambda ordem: fim_das_vendas.get(ordem, date.min)))
                        .sort_values(["_fim", "ordem"], kind="stable").groupby(chave)["estoque"].last())
    combinados = pd.DataFrame({
        "custo_unitario": custo_das_vendas.combine_first(custo_do_estoque).combine_first(custo_dos_precos),
        "estoque": saldo_da_posicao.combine_first(saldo_das_vendas),
        "nome": todas.groupby(chave)["nome"].first(),
        "codigo": todas.groupby(chave)["codigo"].first(),
    })
    combinados.attrs["fonte_do_saldo"] = fonte
    ambiguos = int((todas.groupby("chave_nome")["codigo"].nunique() > 1).sum()) if chave == "chave_nome" else 0
    return combinados, ambiguos


def _conferir_contencao(df: pd.DataFrame, linhas: int, faturamento: float) -> None:
    """A trava contra a duplicação: depois do cruzamento, as linhas da âncora e o faturamento são os de antes."""
    originais = ~df["_auxiliar"].astype(bool) if "_auxiliar" in df.columns else pd.Series(True, index=df.index)
    if int(originais.sum()) != linhas or abs(round(float(df["valor_venda"].sum()), 2) - faturamento) > 0.01:
        raise ETLInputError("raio_x_dados", _FALHA_DE_CONTENCAO)


def _completar_ancora(df: pd.DataFrame, tabelas: list, periodo, rotulo: str, ate=None) -> tuple[pd.DataFrame, list]:
    """(a âncora completada pelas tabelas auxiliares, avisos). Ver o texto acima. `ate` (hoje): as linhas de saldo e de
    cadastro ficam na data do fim do período da âncora, ou na de hoje quando o fim ainda não chegou (um mês que ainda
    não começou faria a fatia mensal recusar ou deixar de fatiar a âncora)."""
    atributos = dict(df.attrs)
    linhas, faturamento = len(df), round(float(df["valor_venda"].sum()), 2)
    avisos = []
    numero = rotulo.split()[-1]
    dia_do_saldo = min(periodo[1], ate) if ate is not None else periodo[1]
    produtos = [tabela for tabela in tabelas if tabela.get("produtos") is not None]
    if produtos:
        codigos = _codigos(df)
        codigos = codigos.map(normalizar_codigo_do_produto) if codigos is not None else None
        # O código só é a chave quando toda tabela de produtos o traz: a tabela sem a coluna ficaria de fora inteira.
        tem_codigo = all("codigo" in tabela["produtos"].columns and tabela["produtos"]["codigo"].notna().any()
                         for tabela in produtos)
        usar_codigo = bool(codigos is not None and tem_codigo and codigos.notna().mean() >= 0.9)
        chave = "codigo" if usar_codigo else "chave_nome"
        combinados, ambiguos = _combinar_auxiliares(produtos, chave)
        chave_da_linha = codigos if usar_codigo else df["_prod_norm"]
        vendidos = _vendas_analisadas(df)
        # Custo: a âncora sem custo recebe o custo unitário de cada produto vezes a quantidade da linha.
        sem_custo = "valor_custo" not in df.columns or not (df["valor_custo"].abs() > 0).any()
        custos = combinados["custo_unitario"].dropna()
        if sem_custo and not custos.empty:
            tabela_de_custos = pd.DataFrame({"_chave": custos.index, "_custo_unitario": custos.to_numpy(float)})
            try:
                unido = df.assign(_chave=chave_da_linha.to_numpy()).merge(
                    tabela_de_custos, on="_chave", how="left", validate="many_to_one")
            except pd.errors.MergeError:
                # A chave repetida do lado das tabelas multiplicaria as linhas da venda.
                raise ETLInputError("raio_x_dados", _FALHA_DE_CONTENCAO) from None
            _conferir_contencao(unido, linhas, faturamento)
            unido["valor_custo"] = (unido["_custo_unitario"] * unido["quantidade"].abs()).round(2).fillna(0.0)
            df = unido.drop(columns=["_chave", "_custo_unitario"])
            cabecalhos = dict(atributos.get("cabecalhos") or {})
            cabecalhos["valor_custo"] = "total custo tabela auxiliar"
            atributos["cabecalhos"] = cabecalhos
            casados = int(chave_da_linha[vendidos].isin(custos.index).groupby(df.loc[vendidos, "_prod_norm"]).any().sum())
            total = int(df.loc[vendidos, "_prod_norm"].nunique())
            avisos.append(f"O custo das vendas do arquivo {numero} veio das tabelas auxiliares, para {casados} de "
                          f"{total} produto(s) vendido(s): é o custo médio do período do relatório por produto ou o "
                          "da posição de estoque, aplicado às vendas do período inteiro. As margens são estimadas.")
        # Estoque e cadastro: linhas sem venda (quantidade 0), na data do fim da âncora, como o relatório "vendas x
        # estoque" lista o saldo. A âncora que já traz o estoque fica com o dela.
        tem_estoque = "_estoque_informado" in df.columns and bool(df["_estoque_informado"].any())
        saldos = combinados["estoque"].dropna() if not tem_estoque else pd.Series(dtype=float)
        da_ancora = set(chave_da_linha.dropna())
        catalogo = [c for c in combinados.index if c not in da_ancora and c not in saldos.index]
        chaves_novas = list(saldos.index) + catalogo
        if chaves_novas:
            nome_na_ancora = df["produto"].groupby(chave_da_linha.to_numpy()).last()
            norma_na_ancora = df["_prod_norm"].groupby(chave_da_linha.to_numpy()).last()
            codigo_bruto = df["codigo"].groupby(chave_da_linha.to_numpy()).last() if "codigo" in df.columns else None
            novas = pd.DataFrame(index=range(len(chaves_novas)))
            novas["produto"] = [nome_na_ancora.get(c, combinados.at[c, "nome"]) for c in chaves_novas]
            novas["_prod_norm"] = [norma_na_ancora.get(c) or _normalizar_produto(combinados.at[c, "nome"])
                                   for c in chaves_novas]
            if "codigo" in df.columns:
                novas["codigo"] = [(codigo_bruto.get(c) if codigo_bruto is not None else None)
                                   or combinados.at[c, "codigo"] for c in chaves_novas]
            novas["quantidade"] = 0.0
            novas["valor_venda"] = 0.0
            if "_valor_informado" in df.columns:
                novas["_valor_informado"] = False
            if "valor_custo" in df.columns:
                novas["valor_custo"] = 0.0
            if "_devolucao" in df.columns:
                novas["_devolucao"] = False
            if "_data" in df.columns:
                novas["_data"] = f"{dia_do_saldo:%d/%m/%Y}"
            if "_unidade" in df.columns:
                novas["_unidade"] = ""
            if "_loja_do_cupom" in df.columns:
                novas["_loja_do_cupom"] = ""
            if len(saldos) or "estoque" in df.columns:
                novas["estoque"] = [float(saldos.get(c, 0.0)) for c in chaves_novas]
                novas["_estoque_informado"] = [c in saldos.index for c in chaves_novas]
            novas["_auxiliar"] = True
            # O custo unitário dos produtos sem venda na âncora, para os indicadores valorizarem o saldo deles (o
            # estoque parado): o da venda continua vindo das linhas. Fica numa coluna das linhas acrescentadas, e não
            # em df.attrs: o pandas copia os attrs inteiros a cada operação, e com os milhares de produtos de uma
            # posição de estoque real o lote ficava várias vezes mais lento.
            novas["_custo_auxiliar"] = [float(combinados.at[c, "custo_unitario"])
                                        if pd.notna(combinados.at[c, "custo_unitario"]) else np.nan
                                        for c in chaves_novas]
            # Com a chave pelo código, dois códigos com o mesmo nome dariam duas linhas do mesmo produto, e a leitura do
            # saldo (o último de cada produto) ficaria só com uma: os saldos dos códigos de mesmo nome somam.
            if "estoque" in novas.columns and novas["_prod_norm"].duplicated().any():
                regras = {coluna: "first" for coluna in novas.columns if coluna != "_prod_norm"}
                regras["estoque"] = "sum"
                if "_estoque_informado" in novas.columns:
                    regras["_estoque_informado"] = "max"
                novas = novas.groupby("_prod_norm", sort=False, as_index=False).agg(regras)
            if len(saldos) and not tem_estoque:
                df = df.assign(estoque=0.0, _estoque_informado=False)
            df = pd.concat([df.assign(_auxiliar=False), novas], ignore_index=True)
            if len(saldos):
                avisos.append(f"O estoque do arquivo {numero} é o das tabelas auxiliares ({len(saldos)} produto(s)), "
                              f"na data de {dia_do_saldo:%d/%m/%Y}.")
                aviso_do_saldo = _aviso_do_saldo(combinados.attrs.get("fonte_do_saldo") or {})
                if aviso_do_saldo:
                    avisos.append(aviso_do_saldo)
                aviso_da_amostra = _aviso_de_amostra_parcial(saldos, chave_da_linha[vendidos], periodo, numero)
                if aviso_da_amostra:
                    avisos.append(aviso_da_amostra)
            if catalogo:
                avisos.append(f"{len(catalogo)} produto(s) das tabelas auxiliares sem venda nem saldo entraram no "
                              "cadastro (o mix).")
        if ambiguos:
            avisos.append(f"{ambiguos} nome(s) de produto com mais de um código nas tabelas auxiliares: o custo e o "
                          "estoque saíram juntos pelo nome.")
    # O resumo por pedido ou documento: o desconto (na primeira linha de venda de cada pedido), o cliente e a hora.
    documentos = [tabela["documentos"] for tabela in tabelas if tabela.get("documentos") is not None]
    if documentos and "_documento" in df.columns:
        todos = pd.concat(documentos)
        todos = todos[~todos.index.duplicated(keep="first")]
        numeros = df["_documento"].map(indicadores_executivos.numero_da_venda).map(normalizar_numero_do_documento)
        venda = _vendas_analisadas(df)
        if "_auxiliar" in df.columns:
            venda &= ~df["_auxiliar"].astype(bool)
        casou = numeros.isin(todos.index)
        # O casamento é medido pelo valor das vendas, e não pelas linhas: com 90% das linhas casadas, a venda grande
        # sem documento deixaria o desconto desconhecido de quase todo o faturamento (auditoria matemática de 11/2026).
        valor_da_venda = df["valor_venda"].where(venda, 0.0).abs()
        total_da_venda = float(valor_da_venda.sum())
        taxa = float(valor_da_venda[casou].sum()) / total_da_venda if total_da_venda > 0 else 0.0
        if taxa >= _CASAMENTO_MINIMO_DOS_DOCUMENTOS:
            vieram = []
            if "_desconto" not in df.columns and "desconto" in todos.columns:
                primeiras = set(numeros[venda & casou].drop_duplicates().index)
                valores = numeros.map(todos["desconto"])
                df["_desconto"] = [f"{valores[i]:.2f}".replace(".", ",") if i in primeiras and pd.notna(valores[i])
                                   else "" for i in df.index]
                vieram.append("o desconto")
            for coluna, nome in (("cliente", "o cliente"), ("hora", "a hora")):
                if "_" + coluna not in df.columns and coluna in todos.columns:
                    df["_" + coluna] = numeros.map(todos[coluna])
                    vieram.append(nome)
            if not vieram:
                texto = "ele não trouxe nada que a venda já não tivesse"
            elif len(vieram) == 1:
                texto = f"{vieram[0]} veio dele"
            else:
                texto = f"{', '.join(vieram[:-1])} e {vieram[-1]} vieram dele"
            avisos.append(f"O resumo por documento casou com {taxa:.0%} do valor das vendas do arquivo {numero}: "
                          f"{texto}.")
        else:
            avisos.append(f"O resumo por documento casou com só {taxa:.0%} do valor das vendas do arquivo {numero} (o "
                          f"mínimo é {_CASAMENTO_MINIMO_DOS_DOCUMENTOS:.0%}): ficou fora.")
    df.attrs = atributos
    _conferir_contencao(df, linhas, faturamento)
    return df, avisos


def _resumir_arquivo(df, formato, periodo, rotulo: str, numero: int, item_a_item: bool, datas_validas: bool, *,
                     cadastro: bool, ate, recusar_datas: bool, fatiar: bool, partes: list, resumos: list) -> set:
    """Um relatório de vendas do envio em `resumos` e `partes` (fatiado por mês quando é a venda item a item com a
    data de cada venda); devolve os produtos em conflito de cadastro."""
    envolvidos = set()
    # Com a leitura de cadastro (o Raio-X do cliente e a Avaliação de Cenário), a data de cada venda faz de um
    # arquivo de vários meses um relatório por mês, em memória.
    fatias = (_fatias_mensais(df, periodo, rotulo, ate, recusar=recusar_datas, item_a_item=item_a_item)
              if cadastro and fatiar else None)
    # O arquivo que não é fatiado conta como um relatório: com 120 meses já lidos, o mês a mais passa do teto em
    # qualquer ordem.
    if len(resumos) + (len(fatias) if fatias else 1) > MAX_FATIAS:
        if recusar_datas:
            raise ETLInputError("raio_x_arquivos", _MAIS_QUE_O_TETO_DE_FATIAS)
        # O Raio-X do cliente sempre aceitou até MAX_ARQUIVOS arquivos: lê de novo sem fatiar nenhum (sem as
        # fatias, cada arquivo é um relatório, e MAX_ARQUIVOS fica abaixo do teto).
        if fatiar:
            raise _AcimaDoTetoDeFatias()
    # O estoque de cada produto e loja acumulado nas fatias do arquivo, em ordem: a fatia mensal só traz as lojas e
    # os produtos que tiveram linha no mês, e o saldo de quem não apareceu nela continua valendo.
    acumulado: dict = {}
    for vendas, intervalo in fatias or [(df, periodo)]:
        produtos, resumo, conflitantes = _resumir(vendas, formato, intervalo, rotulo, len(resumos), cadastro,
                                                  datas_validas=datas_validas)
        resumo["arquivo"] = numero
        resumo["_item_a_item"] = item_a_item
        por_loja = resumo.pop("_estoques_por_loja", None)
        if por_loja is not None:
            acumulado.update(por_loja.to_dict())
            resumo["_estoques"] = (pd.Series(list(acumulado.values()),
                                             index=pd.MultiIndex.from_tuples(list(acumulado)), dtype=float)
                                   .groupby(level=0).sum() if acumulado else pd.Series(dtype=float))
        # O mês sem venda de um arquivo fatiado é período coberto (entra nos resumos), sem produto a somar.
        if fatias is None or not produtos.empty:
            partes.append(produtos.reset_index())
        resumos.append(resumo)
        envolvidos |= conflitantes
    return envolvidos


def _renumerar(resumos: list, partes: list) -> None:
    """Os resumos em ordem de arquivo (a âncora é resumida no fim, e o arquivo absorvido sai), com os índices de novo
    em sequência. Sem âncora, a ordem já é essa, e nada muda."""
    ordem = sorted(range(len(resumos)), key=lambda i: (resumos[i]["arquivo"], resumos[i]["indice"]))
    novos = {resumos[i]["indice"]: posicao for posicao, i in enumerate(ordem)}
    if all(antigo == novo for antigo, novo in novos.items()):
        return
    resumos[:] = [resumos[i] for i in ordem]
    for resumo in resumos:
        resumo["indice"] = novos[resumo["indice"]]
    for posicao, parte in enumerate(partes):
        partes[posicao] = parte.assign(arquivo=parte["arquivo"].map(novos))


def _cobertura(periodos: list) -> list:
    """Os intervalos (início, fim) que os períodos cobrem, juntos quando se sobrepõem ou se tocam (o dia seguinte ao fim
    de um é contínuo)."""
    juntos = []
    for inicio, fim in sorted(periodos):
        if juntos and inicio <= juntos[-1][1] + timedelta(days=1):
            juntos[-1] = (juntos[-1][0], max(juntos[-1][1], fim))
        else:
            juntos.append((inicio, fim))
    return juntos


def _coberto(periodo, intervalos: list) -> bool:
    """O período (ou a falta dele) cabe num dos intervalos cobertos pelas âncoras."""
    return periodo is None or any(inicio <= periodo[0] and periodo[1] <= fim for inicio, fim in intervalos)


def _ler_envio_uma_vez(caminhos: list, *, cadastro: bool, ate, recusar_datas: bool, fatiar: bool):
    partes, resumos, envolvidos_por_arquivo = [], [], {}
    ancoras, auxiliares, de_vendas = [], [], {}
    opcoes = {"cadastro": cadastro, "ate": ate, "recusar_datas": recusar_datas, "fatiar": fatiar, "partes": partes,
              "resumos": resumos}
    for numero, caminho in enumerate(caminhos):
        lido = _ler_arquivo_do_envio(caminho, numero, cadastro, auxiliar=cadastro)
        if lido.tabela is not None:
            auxiliares.append((numero, lido))
            continue
        df = lido.df
        _marcar_as_lojas(df)
        item_a_item = _e_venda_item_a_item(df)
        datas_validas = cadastro and item_a_item and _datas_validas(df, lido.periodo, ate)
        if datas_validas:
            # A âncora temporal é resumida no fim: os outros arquivos do envio podem completá-la.
            ancoras.append((numero, lido, item_a_item))
            continue
        if cadastro:
            # Sem a linha do período no cabeçalho, a venda item a item vale pelas datas das linhas: a de outro mês não é
            # absorvida pela âncora (a venda dela sumiria do faturamento). Num resumo, "Data" é outra coisa.
            periodo = lido.periodo or (_periodo_das_datas(df) if item_a_item else None)
            de_vendas[numero] = (periodo, _auxiliar_das_vendas(df), _lojas_com_nome(df))
        envolvidos_por_arquivo[numero] = _resumir_arquivo(df, lido.formato, lido.periodo, lido.rotulo, numero,
                                                          item_a_item, datas_validas, **opcoes)
    tabelas, avisos_do_lote = [], []
    if ancoras:
        # O período que as âncoras cobrem é a união dos períodos de cada uma: com duas âncoras, o mês entre elas não é
        # coberto, e o relatório desse mês continua sendo venda.
        intervalos = _cobertura([lido.periodo for _numero, lido, _item in ancoras])
        # O relatório de vendas sem data por linha é absorvido quando cabe no período coberto (ou não tem período) e as
        # lojas com nome dele estão na âncora. Só de outras lojas, continua sendo venda. Com lojas da âncora e de fora, é
        # recusado: somar duplicaria a venda das primeiras, e absorver perderia a das outras. Sem a loja na âncora, ele
        # é absorvido (a âncora pode ser a da rede toda), com aviso quando traz a loja de cada venda.
        lojas_das_ancoras = set().union(*(_lojas_com_nome(lido.df) for _numero, lido, _item in ancoras))
        absorvidos, avisos_das_lojas = set(), {}
        for numero, (periodo, _tabela, lojas) in de_vendas.items():
            if not _coberto(periodo, intervalos):
                continue
            if lojas and lojas_das_ancoras and not lojas <= lojas_das_ancoras:
                if lojas & lojas_das_ancoras:
                    raise ETLInputError("raio_x_dados", f"Arquivo {numero + 1}: o relatório traz vendas de lojas que a "
                                        "venda item a item com a data de cada venda tem e de lojas que ela não tem. "
                                        "Envie o relatório de cada loja separado, ou a venda item a item de todas as "
                                        "lojas.")
                continue
            absorvidos.add(numero)
            if lojas and not lojas_das_ancoras:
                avisos_das_lojas[numero] = _AVISO_DAS_LOJAS.format(numero=numero + 1)
        if absorvidos:
            removidos = {resumo["indice"] for resumo in resumos if resumo["arquivo"] in absorvidos}
            resumos[:] = [resumo for resumo in resumos if resumo["arquivo"] not in absorvidos]
            partes[:] = [parte for parte in partes if not parte["arquivo"].isin(removidos).any()]
            for numero in absorvidos:
                envolvidos_por_arquivo.pop(numero, None)
        # O relatório de vendas de leiaute ambíguo só fica fora das contas quando o período dele está no da âncora (ou
        # ele não tem período), como o arquivo absorvido: senão, a venda dele faltaria, e a recusa de sempre vale.
        for _numero, lido in auxiliares:
            periodo = lido.periodo
            if lido.tabela["papel"] == "ambiguo" and not _coberto(periodo, intervalos):
                raise lido.recusa
            # O arquivo que nada leu, sem o título de tabela auxiliar, só fica fora das contas com o período dentro do
            # da âncora: sem período, ou de outro período, a venda dele poderia faltar, e a recusa de sempre vale.
            if lido.tabela["papel"] == "nao_reconhecido" and (periodo is None or not _coberto(periodo, intervalos)):
                raise lido.recusa
        # O relatório por produto leva o fim do período, para o saldo vir do mais recente (_combinar_auxiliares).
        papeis = sorted([(numero, {**de_vendas[numero][1],
                                   "data": de_vendas[numero][0][1] if de_vendas[numero][0] else None})
                         for numero in absorvidos]
                        + [(numero, lido.tabela) for numero, lido in auxiliares], key=lambda par: par[0])
        for numero, tabela in papeis:
            if tabela["papel"] in _PAPEIS_QUE_COMPLETAM:
                tabelas.append({**tabela, "ordem": numero})
            avisos_do_lote.append(_aviso_do_papel(numero, tabela, com_ancora=True))
            if numero in avisos_das_lojas:
                avisos_do_lote.append(avisos_das_lojas[numero])
    elif auxiliares:
        # Sem a âncora: a tabela auxiliar, o resumo de vendas sem produto, o arquivo com título de tabela auxiliar que
        # não foi lido e o relatório de compras pelo título ficam fora das contas quando o envio tem relatório de
        # vendas, e recusam o envio (pela ordem dos arquivos) quando não tem. O relatório de compras só pelas colunas, o
        # de leiaute ambíguo e o arquivo que nada leu continuam recusados: a venda deles poderia faltar.
        for _numero, lido in auxiliares:
            papel = lido.tabela["papel"]
            if (not envolvidos_por_arquivo or papel in ("ambiguo", "nao_reconhecido")
                    or (papel == "compras" and not lido.tabela.get("pelo_titulo"))):
                raise lido.recusa
        avisos_do_lote = [_aviso_do_papel(numero, lido.tabela, com_ancora=False) for numero, lido in auxiliares]
    for numero, lido, item_a_item in ancoras:
        df, avisos = (_completar_ancora(lido.df, tabelas, lido.periodo, lido.rotulo, ate) if tabelas
                      else (lido.df, []))
        antes = len(resumos)
        envolvidos_por_arquivo[numero] = _resumir_arquivo(df, lido.formato, lido.periodo, lido.rotulo, numero,
                                                          item_a_item, True, **opcoes)
        if (avisos_do_lote or avisos) and len(resumos) > antes:
            resumos[antes]["_avisos_do_lote"] = avisos_do_lote + avisos
            avisos_do_lote = []
        # A amostra parcial diante do estoque inteiro marca os indicadores de estoque (indicadores_executivos.calcular).
        if any(aviso.startswith(_PREFIXO_DA_AMOSTRA_PARCIAL) for aviso in avisos):
            for resumo in resumos[antes:]:
                resumo["_amostra_parcial"] = True
    _renumerar(resumos, partes)
    if not partes:
        raise ETLInputError("raio_x_sem_vendas", _SEM_VENDAS)
    if avisos_do_lote and resumos:
        # Sem a âncora (ou com a âncora sem fatia), os avisos do lote vão no primeiro relatório.
        resumos[0]["_avisos_do_lote"] = avisos_do_lote + list(resumos[0].get("_avisos_do_lote") or [])
    envolvidos = set().union(*envolvidos_por_arquivo.values()) if envolvidos_por_arquivo else set()
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
    indicadores = indicadores_executivos.calcular(produtos, a_confirmar, por_arquivo, resumos, inicio, fim)
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
        "indicadores_executivos": indicadores,
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
