"""Avaliação de Cenário (lead): o motor do Raio-X numa passada única, mais a matriz produto x ano.

Roda no processo isolado do ETL (etl_worker, operação "avaliacao_cenario"), como o Raio-X: não
importa banco, Flask nem config. A leitura, a limpeza e as regras por linha são as do Raio-X
(raio_x._ler_envio e raio_x._combinar), sem cópia: o que é só do lead mora aqui.

Regras próprias do lead:
  - Ano de exercício: cada relatório precisa do período no cabeçalho, dentro de um mesmo ano civil
    entre 2000 e o ano corrente ("99" no cabeçalho vira 2099 e é recusado). Relatório sem período,
    que cruza o ano ou fora da faixa é recusado com o número do arquivo: a matriz é por ano.
  - Períodos sobrepostos são recusados: no Raio-X do cliente eles só geram aviso, mas aqui as
    linhas somariam o mesmo mês duas vezes.
  - Matriz produto x ano: as somas que as regras do Raio-X usam (_SOMAS), por produto e ano, com o
    nome e o código do mês mais recente, os relatórios com venda (coluna `meses`: são meses quando
    os relatórios são mensais) e as marcas de variação de preço, conflito de cadastro e estoque
    negativo do ano. A triagem e as marcas do lead são refeitas no processo pai a partir delas
    (triagem_cenario), sem novo envio.
  - Etapa 2 (lead-2026-11.3), também por produto e ano:
      - a venda nos relatórios que têm a coluna de custo (faturamento_em_relatorio_com_custo): sem
        ela, um relatório exportado sem a coluna faria o produto parecer "sem custo";
      - os relatórios com custo, os com margem negativa (comparados em centavos inteiros), se o mais
        recente com custo foi negativo e os relatórios completos (o período já terminou antes de hoje);
      - a quantidade nos mesmos meses (_janela_mesmos_meses): os meses do calendário que os dois últimos
        anos do envio cobrem, só com relatórios mensais e, no último ano, só com os completos (o mês
        corrente, parcial, fica de fora nos dois lados); é a comparação de ritmo sem a sazonalidade;
      - os relatórios do ano desde a primeira venda do produto no ano (relatorios_desde_primeira_venda):
        o giro de um lançamento conta desde que ele começou a vender;
      - o último estoque informado (quando o relatório tem a coluna);
      - a categoria e se faltaram NCM ou categoria, lidas do mesmo relatório de vendas quando ele tem
        essas colunas (etl_processor._vendas_do_bruto(..., cadastro=True)); relatório com cabeçalho
        ambíguo ("Cód. NCM" lido como código do produto) não alimenta NCM nem categoria;
      - os ajustes de bonificação e devolução do produto (os do CMV líquido do Raio-X); os de produto
        sem venda no ano vão para a meta, por ano, para os totais fecharem com o Raio-X.
  - Etapa 3 (lead-2026-11.4): o fim (até hoje) do último relatório do ano em que o produto vendeu
    (fim_ultimo_relatorio_com_venda), base dos dias sem venda da inteligência de estoque.
  - Cobertura de cada ano (meta): relatórios, dias cobertos pelos períodos, se todos os relatórios
    do ano são mensais e, por ano, quantos trazem custo, NCM, categoria e estoque e quantos estão
    completos; e os períodos cobertos do envio (periodos_cobertos: [início, fim até hoje], contíguos
    unidos), que dizem quantos dias cobertos há depois de uma data. É a base do ritmo de venda e de cada
    verificação da triagem: sem a coluna, a verificação não se aplica.

Saída (JSON; o worker tem teto rígido de 4 MiB, conferido aqui antes de devolver):
  - "raio_x": o resultado de raio_x.diagnosticar para os mesmos arquivos (o resumo do lead sai
    dele: índice de saúde e recuperável; o plano é recomendado no processo pai);
  - "linhas": {"colunas": COLUNAS, "valores": [[...], ...], "categorias": [...]}, uma lista por
    produto e ano; a coluna "categoria" é o índice em "categorias" (cada nome vai uma vez só);
    números inteiros saem sem ".0";
  - "meta": contagens do envio (CHAVES_META), sem nome de produto nem texto livre.

Indicadores executivos (raio-x-2026-11.6): o "raio_x" traz o nó indicadores_executivos (21 indicadores,
indicadores_executivos.py), montado por raio_x._combinar com as mesmas somas por produto e relatório daqui.
Nele está a matriz da venda com margem negativa por produto e mês (AAAA-MM), com impacto_q4 nos meses 10, 11
e 12 e sem os itens de custo a confirmar (refatoração 5). O processo pai grava o nó em
lead_avaliacao.indicadores_executivos_json, que sai no expurgo de 30 dias com as linhas.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from bisect import bisect_left
import hashlib
import json
import math

import pandas as pd

from etl_inputs import ETLInputError
from etl_limits import get_limits
from etl_processor import LIMITE_CATEGORIA
import raio_x
from security_gatekeeper import neutralizar_celula

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
# Folga para o envelope do worker ({"ok": true, "result": ..., "runtime": {...}}).
_FOLGA_ENVELOPE = 4096
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
_MENSAGEM_TETO = ("Há quantidade ou valor acima do que a avaliação aceita nos relatórios. Confira as colunas de "
                  "quantidade, valor e custo.")


def _data_br(valor: date) -> str:
    return f"{valor:%d/%m/%Y}"


def _ano_do_arquivo(resumo: dict, ano_maximo: int, hoje: date) -> int:
    """Ano civil do relatório; recusa sem período, período que cruza o ano, fora da faixa ou que
    começa depois de hoje."""
    numero = raio_x._numero_do_arquivo(resumo)
    inicio, fim = resumo["inicio"], resumo["fim"]
    if inicio is None or fim is None:
        raise ETLInputError("avaliacao_sem_periodo", (
            f"Arquivo {numero}: o período não está no cabeçalho do relatório. A avaliação separa os dados "
            "por ano: envie o relatório com a linha \"Período: dd/mm/aaaa a dd/mm/aaaa\"."))
    if inicio.year != fim.year:
        raise ETLInputError("avaliacao_periodo_entre_anos", (
            f"Arquivo {numero}: o período ({_data_br(inicio)} a {_data_br(fim)}) passa de um ano para o "
            "outro. Envie um relatório por mês ou por ano civil."))
    if not ANO_MINIMO <= inicio.year <= ano_maximo:
        raise ETLInputError("avaliacao_ano", (
            f"Arquivo {numero}: o ano do período ({inicio.year}) está fora do intervalo aceito "
            f"({ANO_MINIMO} a {ano_maximo}). Confira a data no cabeçalho do relatório."))
    if inicio > hoje:
        raise ETLInputError("avaliacao_periodo_futuro", (
            f"Arquivo {numero}: o período começa em {_data_br(inicio)}, depois de hoje. Confira a data no "
            "cabeçalho do relatório."))
    return inicio.year


def dias_cobertos(resumo: dict, hoje: date) -> int:
    """Dias do período do relatório até hoje: o mês corrente exportado com o mês inteiro no
    cabeçalho ("01/11 a 30/11" em 08/11) cobre só os dias que já passaram."""
    return max(1, (min(resumo["fim"], hoje) - resumo["inicio"]).days + 1)


def _janela_mesmos_meses(resumos: list, anos: dict, hoje: date) -> tuple[frozenset, dict]:
    """(índices dos relatórios na janela dos mesmos meses, dias deles por ano).

    A janela são os meses do calendário cobertos pelos dois últimos anos do envio: os relatórios
    completos do último ano (fim antes de hoje; o mês corrente, parcial, fica de fora) e os do ano
    anterior. Só vale quando os relatórios desses dois anos são todos mensais; senão, e nos demais
    anos, fica vazia (a triagem compara os períodos inteiros). Sem mês em comum, também fica vazia:
    a triagem não compara ritmo (12 meses de nov a out comparariam meses diferentes)."""
    por_ano: dict = {}
    for resumo in resumos:
        por_ano.setdefault(anos[resumo["indice"]], []).append(resumo)
    dias = {str(ano): 0 for ano in por_ano}
    if len(por_ano) < 2:
        return frozenset(), dias
    ultimo = max(por_ano)
    anterior = max(ano for ano in por_ano if ano < ultimo)
    if not all(r["inicio"].month == r["fim"].month for r in por_ano[ultimo] + por_ano[anterior]):
        return frozenset(), dias
    comuns = ({r["inicio"].month for r in por_ano[ultimo] if r["fim"] < hoje}
              & {r["inicio"].month for r in por_ano[anterior]})
    dentro = set()
    for ano, so_completos in ((ultimo, True), (anterior, False)):
        for resumo in por_ano[ano]:
            if resumo["inicio"].month in comuns and (not so_completos or resumo["fim"] < hoje):
                dentro.add(resumo["indice"])
                dias[str(ano)] += dias_cobertos(resumo, hoje)
    return frozenset(dentro), dias


def _periodos_cobertos(resumos: list, hoje: date) -> list:
    """[[início, fim], ...] em ordem, com o fim cortado em hoje e os períodos contíguos unidos (os
    sobrepostos já foram recusados)."""
    unidos: list = []
    for resumo in sorted(resumos, key=lambda r: r["inicio"]):
        inicio, fim = resumo["inicio"], min(resumo["fim"], hoje)
        if unidos and (inicio - unidos[-1][1]).days <= 1:
            unidos[-1][1] = max(unidos[-1][1], fim)
        else:
            unidos.append([inicio, fim])
    return [[inicio.isoformat(), fim.isoformat()] for inicio, fim in unidos]


def _recusar_sobreposicao(resumos: list) -> None:
    """Dois relatórios com o mesmo mês somariam a venda duas vezes na matriz do lead."""
    anterior = None
    for atual in sorted(resumos, key=lambda r: (r["inicio"], r["indice"])):
        if anterior is not None and atual["inicio"] <= anterior["fim"]:
            raise ETLInputError("avaliacao_sobreposicao", (
                f"Os arquivos {raio_x._numero_do_arquivo(anterior)} e {raio_x._numero_do_arquivo(atual)} cobrem "
                "períodos que se sobrepõem. "
                "Envie cada período uma vez só."))
        if anterior is None or atual["fim"] > anterior["fim"]:
            anterior = atual


def chave_produto(produto_normalizado: str) -> str:
    """Chave estável do produto entre os anos: o nome normalizado do ETL (sem acento, pontuação
    nem caixa), resumido em 16 hexadecimais para caber numa coluna de tamanho fixo."""
    return hashlib.sha256(produto_normalizado.encode("utf-8")).hexdigest()[:16]


def _finito(valor: float, casas: int):
    """O número arredondado; inteiro sai como int ("10" em vez de "10.0" no JSON, que tem teto)."""
    numero = float(valor)
    if not math.isfinite(numero):
        raise ETLInputError("avaliacao_valor", "Há quantidade ou valor fora do intervalo aceito nos relatórios.")
    numero = round(numero, casas)
    return int(numero) if numero.is_integer() else numero


LIMITE_DESCRICAO = 200  # brainstorming_dados_pdv.descricao_produto


def _texto_limpo(valor) -> str:
    """Só caracteres imprimíveis, com espaço especial (NBSP, TAB) virando espaço e os espaços colapsados:
    o contrato do processo pai recusa caractere de controle, e o PostgreSQL recusa NUL."""
    texto = "".join(" " if c.isspace() else c for c in str(valor) if c.isprintable() or c.isspace())
    return " ".join(texto.split())


def _sem_formula(texto: str) -> str:
    """O texto já limpo, neutralizado de novo contra fórmula, pela regra da leitura (o que já saiu
    neutralizado não muda). A leitura julga a célula como ela chega; a limpeza daqui tira do começo o que a
    leitura não trata como começo de célula (NBSP, espaço de largura zero, caractere de controle), e sem
    isto o texto gravado na staging voltaria a começar por "="."""
    return neutralizar_celula(texto)


def _descricao(nome, produto: str) -> str:
    """Nome para a staging: limpo (a coluna exige texto além de espaços), sem fórmula e, se o nome do
    relatório ficar vazio, a chave normalizada do produto."""
    return _sem_formula(_texto_limpo(nome) or _texto_limpo(produto) or "PRODUTO")[:LIMITE_DESCRICAO].strip()


def _cadastro(resumo: dict) -> dict:
    """O que o relatório trouxe de cadastro; o de cabeçalho ambíguo não conta NCM nem categoria."""
    info = resumo.get("_cadastro") or {}
    confiavel = not info.get("ambiguo")
    return {"ncm": bool(info.get("ncm")) and confiavel, "categoria": bool(info.get("categoria")) and confiavel,
            "estoque": bool(info.get("estoque")), "ambiguo": bool(info.get("ambiguo")),
            "varias_unidades": bool(info.get("varias_unidades"))}


def _centavos(valor) -> int:
    return int(round(float(valor) * 100))


def _ultimo_valor(serie):
    """O último valor não nulo (a série vem em ordem cronológica) ou None."""
    validos = serie.dropna()
    return validos.iloc[-1] if not validos.empty else None


def _linhas_produto_ano(por_arquivo, resumos: list, anos: dict, hoje: date) -> tuple[list, list, dict]:
    """(linhas na ordem de COLUNAS, categorias, ajustes de produto sem venda por ano)."""
    por_indice = {r["indice"]: r for r in resumos}
    cadastro = {indice: _cadastro(r) for indice, r in por_indice.items()}
    janela, _dias_janela = _janela_mesmos_meses(resumos, anos, hoje)
    # Inícios dos relatórios de cada ano, em ordem: quantos relatórios do ano vieram desde a primeira
    # venda do produto (o giro de um lançamento conta a partir dela).
    inicios_por_ano: dict = {}
    for resumo in resumos:
        inicios_por_ano.setdefault(anos[resumo["indice"]], []).append(resumo["inicio"])
    for inicios in inicios_por_ano.values():
        inicios.sort()

    base = por_arquivo.assign(ano_exercicio=por_arquivo["arquivo"].map(anos))
    # Conflito de cadastro e estoque negativo valem para o produto no ANO: o arquivo em que eles
    # aparecem pode ter só bonificação ou devolução do produto, que não entram nas somas.
    conflito = {(anos[r["indice"]], produto) for r in resumos for produto in r["_envolvidos"]}
    negativo = {(anos[r["indice"]], produto) for r in resumos for produto in r["_estoque_negativo"]}
    pares = list(zip(base["ano_exercicio"], base["produto"]))
    com_linha = set(pares)
    # A marca de um ano sem venda do produto (lá ele só teve bonificação ou devolução) vai para as
    # linhas do mesmo produto nos outros anos: o Raio-X do mesmo envio conta o produto, e a triagem
    # também tem de contar.
    conflito_sem_linha = {produto for _ano, produto in conflito - com_linha}
    negativo_sem_linha = {produto for _ano, produto in negativo - com_linha}
    base["conflito_cadastral"] = [par in conflito or par[1] in conflito_sem_linha for par in pares]
    base["estoque_negativo"] = [par in negativo or par[1] in negativo_sem_linha for par in pares]
    # Etapa 2, por arquivo e produto.
    arquivo = base["arquivo"]
    tem_custo = arquivo.map({indice: bool(r["tem_custo"]) for indice, r in por_indice.items()})
    base["_fat_rel_custo"] = base["faturamento"].where(tem_custo, 0.0)
    base["_com_custo"] = (base["quantidade_com_custo"] > 0) & tem_custo
    margem_negativa = pd.Series([_centavos(custo) > _centavos(venda) for custo, venda
                                 in zip(base["custo_total"], base["faturamento_com_custo"])], index=base.index, dtype=bool)
    base["_negativo"] = base["_com_custo"] & margem_negativa
    base["_completo"] = arquivo.map({indice: r["fim"] < hoje for indice, r in por_indice.items()})
    base["_qtd_janela"] = base["quantidade"].where(arquivo.isin(janela), 0.0)
    for chave in ("ncm", "categoria", "estoque"):
        if chave not in base.columns:
            base[chave] = None
    col_ncm = arquivo.map({indice: c["ncm"] for indice, c in cadastro.items()})
    col_categoria = arquivo.map({indice: c["categoria"] for indice, c in cadastro.items()})
    base["_ncm_avaliado"] = col_ncm
    base["_ncm_ok"] = col_ncm & base["ncm"].notna()
    base["_cat_avaliada"] = col_categoria
    base["_cat_ok"] = col_categoria & base["categoria"].notna()
    base["categoria"] = base["categoria"].where(col_categoria, None)

    # por_arquivo já vem em ordem cronológica estável: "last" é o mês mais recente do ano.
    grupos = base.groupby(["ano_exercicio", "produto"], sort=True)
    somas = grupos[["linhas", "quantidade", "faturamento", "quantidade_com_custo", "faturamento_com_custo",
                    "custo_total", "perda", "vazamento", "recuperavel", "_fat_rel_custo", "_com_custo",
                    "_negativo", "_completo", "_qtd_janela"]].sum()
    indice = somas.index
    coluna = {nome: somas[nome].to_numpy() for nome in somas.columns}
    nomes = grupos["nome"].last().reindex(indice).to_numpy()
    codigos = grupos["codigo"].last().reindex(indice).to_numpy()
    variacao = grupos["variacao"].any().reindex(indice).to_numpy()
    conflitos = grupos["conflito_cadastral"].any().reindex(indice).to_numpy()
    negativos = grupos["estoque_negativo"].any().reindex(indice).to_numpy()
    meses = grupos["arquivo"].nunique().reindex(indice).to_numpy()
    primeiros = grupos["arquivo"].first().reindex(indice).to_numpy()
    ultimos = grupos["arquivo"].last().reindex(indice).to_numpy()
    com_custo = base[base["_com_custo"]].groupby(["ano_exercicio", "produto"], sort=True)
    ultimo_negativo = com_custo["_negativo"].last().reindex(indice).fillna(False).to_numpy()
    # Último saldo informado de cada produto no ano, em ordem cronológica dos relatórios do ano, inclusive
    # dos relatórios em que ele não vendeu (só aparece com quantidade 0 e o saldo).
    saldo_no_ano: dict = {}
    for resumo in sorted(resumos, key=lambda r: (r["inicio"], r["indice"])):
        tabela = resumo.get("_estoques")
        if tabela is None:
            continue
        ano = anos[resumo["indice"]]
        for produto, valor in tabela.items():
            if valor is not None and not (isinstance(valor, float) and math.isnan(valor)):
                saldo_no_ano[(ano, produto)] = valor
    estoques = [saldo_no_ano.get(par) for par in indice]
    categorias_do_ano = grupos["categoria"].agg(_ultimo_valor).reindex(indice).to_numpy()
    ncm_avaliado = grupos["_ncm_avaliado"].any().reindex(indice).to_numpy()
    ncm_ok = grupos["_ncm_ok"].any().reindex(indice).to_numpy()
    cat_avaliada = grupos["_cat_avaliada"].any().reindex(indice).to_numpy()
    cat_ok = grupos["_cat_ok"].any().reindex(indice).to_numpy()

    # Ajustes de bonificação e devolução: do produto no ano quando ele tem linha; senão, do ano.
    ajustes = {}
    for resumo in resumos:
        tabela = resumo.get("_ajustes")
        if tabela is None:
            continue
        ano = anos[resumo["indice"]]
        for produto, valores in tabela.iterrows():
            soma = ajustes.setdefault((ano, produto), [0.0, 0.0, 0.0])
            for posicao, chave in enumerate(CHAVES_AJUSTES):
                soma[posicao] += float(valores[chave])
    sem_venda = {str(ano): [0.0, 0.0, 0.0] for ano in sorted(set(anos.values()))}
    for (ano, produto), valores in ajustes.items():
        if (ano, produto) not in com_linha:
            for posicao, valor in enumerate(valores):
                sem_venda[str(ano)][posicao] += valor

    nomes_de_categoria, posicao_da_categoria = [], {}
    linhas = []
    for i, (ano, produto) in enumerate(indice):
        codigo = codigos[i]
        codigo = _sem_formula(_texto_limpo(codigo))[:60].strip() if isinstance(codigo, str) else None
        categoria = categorias_do_ano[i]
        if isinstance(categoria, str) and categoria.strip():
            categoria = _sem_formula(categoria)[:LIMITE_CATEGORIA].strip()
            if categoria not in posicao_da_categoria:
                posicao_da_categoria[categoria] = len(nomes_de_categoria)
                nomes_de_categoria.append(categoria)
            categoria = posicao_da_categoria[categoria]
        else:
            categoria = None
        estoque = estoques[i]
        estoque = _finito(estoque, 3) if estoque is not None and not (isinstance(estoque, float) and math.isnan(estoque)) else None
        bonificacao, devolucao, receita_devolvida = ajustes.get((ano, produto), (0.0, 0.0, 0.0))
        inicios = inicios_por_ano[ano]
        desde_a_primeira = len(inicios) - bisect_left(inicios, por_indice[int(primeiros[i])]["inicio"])
        linhas.append([
            int(ano), chave_produto(produto), codigo or None,
            _descricao(nomes[i], produto), _finito(coluna["quantidade"][i], 3), _finito(coluna["faturamento"][i], 2),
            _finito(coluna["quantidade_com_custo"][i], 3), _finito(coluna["faturamento_com_custo"][i], 2),
            _finito(coluna["custo_total"][i], 2), _finito(coluna["perda"][i], 2), _finito(coluna["vazamento"][i], 2),
            _finito(coluna["recuperavel"][i], 2), int(coluna["linhas"][i]), int(meses[i]),
            bool(variacao[i]), bool(conflitos[i]), bool(negativos[i]),
            _finito(coluna["_fat_rel_custo"][i], 2), int(coluna["_com_custo"][i]), int(coluna["_negativo"][i]),
            bool(ultimo_negativo[i]), int(coluna["_completo"][i]), _finito(coluna["_qtd_janela"][i], 3), estoque,
            categoria, bool(ncm_avaliado[i] and not ncm_ok[i]), bool(cat_avaliada[i] and not cat_ok[i]),
            _finito(bonificacao, 2), _finito(devolucao, 2), _finito(receita_devolvida, 2), int(desde_a_primeira),
            min(por_indice[int(ultimos[i])]["fim"], hoje).isoformat(),
        ])
    ajustes_sem_venda = {ano: {chave: _finito(valores[posicao], 2) for posicao, chave in enumerate(CHAVES_AJUSTES)}
                         for ano, valores in sem_venda.items()}
    return linhas, nomes_de_categoria, ajustes_sem_venda


def conferir_tetos(linhas: list) -> None:
    """Recusa (com mensagem) valor que a coluna do banco não guarda, em vez de derrubar a gravação."""
    posicoes = {nome: COLUNAS.index(nome) for nome in TETOS if nome in COLUNAS}
    for linha in linhas:
        for nome, posicao in posicoes.items():
            if linha[posicao] is not None and abs(linha[posicao]) >= TETOS[nome]:
                raise ETLInputError("avaliacao_valor", _MENSAGEM_TETO)


def conferir_tetos_da_meta(meta: dict) -> None:
    """O dinheiro da meta (ajustes de produto sem venda e situação) tem o teto do dinheiro das linhas:
    uma bonificação absurda de produto sem venda não pode inflar o CMV líquido do lead."""
    valores = [valor for ajustes in meta["ajustes_sem_venda_por_ano"].values() for valor in ajustes.values()]
    valores += [valor for chave, valor in meta["situacao"].items() if chave in CHAVES_SITUACAO_VALORES]
    if any(abs(valor) >= TETO_DINHEIRO_DA_META for valor in valores):
        raise ETLInputError("avaliacao_valor", _MENSAGEM_TETO)


def _meta(resumos: list, anos: dict, hoje: date, ajustes_sem_venda: dict) -> dict:
    qualidade, situacao = Counter(), Counter()
    for resumo in resumos:
        qualidade.update({chave: valor for chave, valor in resumo["_qualidade"].items() if chave in CHAVES_QUALIDADE})
        situacao.update({chave: valor for chave, valor in resumo["_situacao"].items()
                         if chave in CHAVES_SITUACAO_CONTAGENS | CHAVES_SITUACAO_VALORES})
    por_ano = Counter(str(anos[r["indice"]]) for r in resumos)
    # Os períodos não se sobrepõem (_recusar_sobreposicao): somar os dias de cada relatório dá os
    # dias cobertos do ano, contados até hoje. Mensal: o relatório cabe num mesmo mês (o mês
    # corrente pode ser parcial).
    dias, mensais = Counter(), {}
    contagens = {chave: Counter({ano: 0 for ano in por_ano}) for chave in CHAVES_RELATORIOS_POR_ANO}
    _janela, dias_janela = _janela_mesmos_meses(resumos, anos, hoje)
    for resumo in resumos:
        ano = str(anos[resumo["indice"]])
        dias[ano] += dias_cobertos(resumo, hoje)
        mensais[ano] = mensais.get(ano, True) and resumo["inicio"].month == resumo["fim"].month
        cadastro = _cadastro(resumo)
        contagens["relatorios_com_custo_por_ano"][ano] += bool(resumo["tem_custo"])
        contagens["relatorios_com_ncm_por_ano"][ano] += cadastro["ncm"]
        contagens["relatorios_com_categoria_por_ano"][ano] += cadastro["categoria"]
        contagens["relatorios_com_estoque_por_ano"][ano] += cadastro["estoque"]
        contagens["relatorios_completos_por_ano"][ano] += resumo["fim"] < hoje
    meta = {
        "versao_regras": VERSAO_REGRAS_LEAD,
        "versao_raio_x": raio_x.VERSAO_REGRAS,
        "arquivos": len(resumos),
        "arquivos_por_ano": dict(sorted(por_ano.items())),
        "dias_por_ano": dict(sorted(dias.items())),
        "relatorios_mensais_por_ano": dict(sorted(mensais.items())),
        "arquivos_com_custo": sum(1 for r in resumos if r["tem_custo"]),
        "linhas_lidas": sum(r["linhas"] for r in resumos),
        "qualidade": dict(qualidade),
        "situacao": {chave: (round(valor, 2) if isinstance(valor, float) else valor)
                     for chave, valor in situacao.items()},
        "celulas_recodificadas": sum(r["celulas_recodificadas"] for r in resumos),
        "dias_mesmos_meses_por_ano": dict(sorted(dias_janela.items())),
        "relatorios_cabecalho_ambiguo": sum(1 for r in resumos if _cadastro(r)["ambiguo"]),
        "ajustes_sem_venda_por_ano": dict(sorted(ajustes_sem_venda.items())),
        "periodos_cobertos": _periodos_cobertos(resumos, hoje),
        "relatorios_varias_unidades": sum(1 for r in resumos if _cadastro(r)["varias_unidades"]),
    }
    meta.update({chave: dict(sorted(contagens[chave].items())) for chave in CHAVES_RELATORIOS_POR_ANO})
    return meta


def avaliar_cenario(caminhos: list, *, ano_maximo: int | None = None, hoje: date | None = None) -> dict:
    """Raio-X e matriz produto x ano dos relatórios de vendas do lead (na ordem recebida)."""
    hoje = date.today() if hoje is None else hoje
    por_arquivo, resumos, envolvidos = raio_x._ler_envio(caminhos, cadastro=True, ate=hoje)
    ano_maximo = hoje.year if ano_maximo is None else ano_maximo
    anos = {r["indice"]: _ano_do_arquivo(r, ano_maximo, hoje) for r in resumos}
    for resumo in resumos:
        if (resumo.get("_cadastro") or {}).get("produto_de_loja"):
            raise ETLInputError("avaliacao_cabecalho_loja", (
                f"Arquivo {raio_x._numero_do_arquivo(resumo)}: a coluna lida como código ou descrição do produto é de loja ou "
                "filial (por exemplo, \"Descrição Loja\" antes da descrição do produto). Exporte o relatório com as "
                "colunas de loja depois das do produto, ou sem elas."))
    _recusar_sobreposicao(resumos)
    linhas, categorias, ajustes_sem_venda = _linhas_produto_ano(por_arquivo, resumos, anos, hoje)
    conferir_tetos(linhas)
    meta = _meta(resumos, anos, hoje, ajustes_sem_venda)
    conferir_tetos_da_meta(meta)
    resultado = {
        "versao": VERSAO_REGRAS_LEAD,
        "raio_x": raio_x._combinar(por_arquivo, resumos, envolvidos),
        "linhas": {"colunas": list(COLUNAS), "valores": linhas, "categorias": categorias},
        "meta": meta,
    }
    # O worker troca resultado acima do teto por um erro genérico; aqui a recusa explica o motivo.
    tamanho = len(json.dumps(resultado, ensure_ascii=True, allow_nan=False))
    if tamanho > get_limits().max_output_bytes - _FOLGA_ENVELOPE:
        raise ETLInputError("avaliacao_grande", (
            f"A avaliação passa do tamanho que o processamento devolve ({len(linhas)} produtos somados entre os "
            "anos). Envie menos meses por vez ou separe os relatórios por grupo de produtos."))
    return resultado
