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
  - Cobertura de cada ano (meta): relatórios, dias cobertos pelos períodos e se todos os
    relatórios do ano são mensais. É a base do ritmo de venda da triagem, que compara anos com
    durações diferentes (um ano inteiro com o semestre corrente, por exemplo).

Saída (JSON; o worker tem teto rígido de 4 MiB, conferido aqui antes de devolver):
  - "raio_x": o resultado de raio_x.diagnosticar para os mesmos arquivos (o resumo do lead sai
    dele: índice de saúde e recuperável; o plano é recomendado no processo pai);
  - "linhas": {"colunas": COLUNAS, "valores": [[...], ...]}, uma lista por produto e ano;
  - "meta": contagens do envio (CHAVES_META), sem nome de produto nem texto livre.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
import math

from etl_inputs import ETLInputError
from etl_limits import get_limits
import raio_x

VERSAO_REGRAS_LEAD = "lead-2026-11.2"
ANO_MINIMO = 2000
ANO_MAXIMO_DO_BANCO = 2100  # ck_brainstorming_dados_pdv_ano
CHAVES_RESULTADO = frozenset({"versao", "raio_x", "linhas", "meta"})
# A meta fica no lead depois do expurgo das linhas: só contagens, com chaves fixas, e nenhum texto
# livre. O processo pai confere exatamente estas chaves (etl_runtime.validar_avaliacao).
CHAVES_META = frozenset({
    "versao_regras", "versao_raio_x", "arquivos", "arquivos_por_ano", "dias_por_ano",
    "relatorios_mensais_por_ano", "arquivos_com_custo", "linhas_lidas", "qualidade", "situacao",
    "celulas_recodificadas",
})
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
)
# Folga para o envelope do worker ({"ok": true, "result": ..., "runtime": {...}}).
_FOLGA_ENVELOPE = 4096
# Maior valor absoluto que cada coluna numérica da staging guarda (NUMERIC(p, s) do banco:
# 10 ** (p - s), exclusivo). O SQLite não impõe a precisão; o PostgreSQL derrubaria a gravação.
TETOS = {
    "quantidade": 10 ** 11, "quantidade_com_custo": 10 ** 11,
    "valor_venda_bruto": 10 ** 12, "faturamento_com_custo": 10 ** 12, "custo_total": 10 ** 12,
    "perda": 10 ** 12, "vazamento_preco": 10 ** 12, "recuperavel": 10 ** 12,
    # Derivadas no processo pai: custo médio NUMERIC(14,4) e vazamento estimado NUMERIC(12,2).
    "custo_medio_registrado": 10 ** 10, "vazamento_estimado_r$": 10 ** 10,
}


def _data_br(valor: date) -> str:
    return f"{valor:%d/%m/%Y}"


def _ano_do_arquivo(resumo: dict, ano_maximo: int, hoje: date) -> int:
    """Ano civil do relatório; recusa sem período, período que cruza o ano, fora da faixa ou que
    começa depois de hoje."""
    numero = resumo["indice"] + 1
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


def _recusar_sobreposicao(resumos: list) -> None:
    """Dois relatórios com o mesmo mês somariam a venda duas vezes na matriz do lead."""
    anterior = None
    for atual in sorted(resumos, key=lambda r: (r["inicio"], r["indice"])):
        if anterior is not None and atual["inicio"] <= anterior["fim"]:
            raise ETLInputError("avaliacao_sobreposicao", (
                f"Os arquivos {anterior['indice'] + 1} e {atual['indice'] + 1} cobrem períodos que se sobrepõem. "
                "Envie cada período uma vez só."))
        if anterior is None or atual["fim"] > anterior["fim"]:
            anterior = atual


def chave_produto(produto_normalizado: str) -> str:
    """Chave estável do produto entre os anos: o nome normalizado do ETL (sem acento, pontuação
    nem caixa), resumido em 16 hexadecimais para caber numa coluna de tamanho fixo."""
    return hashlib.sha256(produto_normalizado.encode("utf-8")).hexdigest()[:16]


def _finito(valor: float, casas: int) -> float:
    numero = float(valor)
    if not math.isfinite(numero):
        raise ETLInputError("avaliacao_valor", "Há quantidade ou valor fora do intervalo aceito nos relatórios.")
    return round(numero, casas)


LIMITE_DESCRICAO = 200  # brainstorming_dados_pdv.descricao_produto


def _descricao(nome, produto: str) -> str:
    """Nome para a staging: espaços colapsados (a coluna exige texto além de espaços) e, se o nome
    do relatório ficar vazio, a chave normalizada do produto."""
    return (" ".join(str(nome).split()) or produto)[:LIMITE_DESCRICAO]


def _linhas_produto_ano(por_arquivo, resumos: list, anos: dict) -> list:
    """Uma lista de valores (na ordem de COLUNAS) por produto e ano de exercício."""
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
    # por_arquivo já vem em ordem cronológica estável: "last" é o mês mais recente do ano.
    grupos = base.groupby(["ano_exercicio", "produto"], sort=True)
    somas = grupos[["linhas", "quantidade", "faturamento", "quantidade_com_custo", "faturamento_com_custo",
                    "custo_total", "perda", "vazamento", "recuperavel"]].sum()
    coluna = {nome: somas[nome].to_numpy() for nome in somas.columns}
    nomes = grupos["nome"].last().reindex(somas.index).to_numpy()
    codigos = grupos["codigo"].last().reindex(somas.index).to_numpy()
    variacao = grupos["variacao"].any().reindex(somas.index).to_numpy()
    conflitos = grupos["conflito_cadastral"].any().reindex(somas.index).to_numpy()
    negativos = grupos["estoque_negativo"].any().reindex(somas.index).to_numpy()
    meses = grupos["arquivo"].nunique().reindex(somas.index).to_numpy()
    linhas = []
    for i, (ano, produto) in enumerate(somas.index):
        codigo = codigos[i]
        codigo = " ".join(codigo.split()) if isinstance(codigo, str) else None
        linhas.append([
            int(ano), chave_produto(produto), codigo or None,
            _descricao(nomes[i], produto), _finito(coluna["quantidade"][i], 3), _finito(coluna["faturamento"][i], 2),
            _finito(coluna["quantidade_com_custo"][i], 3), _finito(coluna["faturamento_com_custo"][i], 2),
            _finito(coluna["custo_total"][i], 2), _finito(coluna["perda"][i], 2), _finito(coluna["vazamento"][i], 2),
            _finito(coluna["recuperavel"][i], 2), int(coluna["linhas"][i]), int(meses[i]),
            bool(variacao[i]), bool(conflitos[i]), bool(negativos[i]),
        ])
    return linhas


def conferir_tetos(linhas: list) -> None:
    """Recusa (com mensagem) valor que a coluna do banco não guarda, em vez de derrubar a gravação."""
    posicoes = {nome: COLUNAS.index(nome) for nome in TETOS if nome in COLUNAS}
    for linha in linhas:
        for nome, posicao in posicoes.items():
            if abs(linha[posicao]) >= TETOS[nome]:
                raise ETLInputError("avaliacao_valor", (
                    "Há quantidade ou valor acima do que a avaliação aceita nos relatórios. Confira as colunas "
                    "de quantidade, valor e custo."))


def _meta(resumos: list, anos: dict, hoje: date) -> dict:
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
    for resumo in resumos:
        ano = str(anos[resumo["indice"]])
        dias[ano] += dias_cobertos(resumo, hoje)
        mensais[ano] = mensais.get(ano, True) and resumo["inicio"].month == resumo["fim"].month
    return {
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
    }


def avaliar_cenario(caminhos: list, *, ano_maximo: int | None = None, hoje: date | None = None) -> dict:
    """Raio-X e matriz produto x ano dos relatórios de vendas do lead (na ordem recebida)."""
    por_arquivo, resumos, envolvidos = raio_x._ler_envio(caminhos)
    hoje = date.today() if hoje is None else hoje
    ano_maximo = hoje.year if ano_maximo is None else ano_maximo
    anos = {r["indice"]: _ano_do_arquivo(r, ano_maximo, hoje) for r in resumos}
    _recusar_sobreposicao(resumos)
    linhas = _linhas_produto_ano(por_arquivo, resumos, anos)
    conferir_tetos(linhas)
    resultado = {
        "versao": VERSAO_REGRAS_LEAD,
        "raio_x": raio_x._combinar(por_arquivo, resumos, envolvidos),
        "linhas": {"colunas": list(COLUNAS), "valores": linhas},
        "meta": _meta(resumos, anos, hoje),
    }
    # O worker troca resultado acima do teto por um erro genérico; aqui a recusa explica o motivo.
    tamanho = len(json.dumps(resultado, ensure_ascii=True, allow_nan=False))
    if tamanho > get_limits().max_output_bytes - _FOLGA_ENVELOPE:
        raise ETLInputError("avaliacao_grande", (
            f"A avaliação passa do tamanho que o processamento devolve ({len(linhas)} produtos somados entre os "
            "anos). Envie menos meses por vez ou separe os relatórios por grupo de produtos."))
    return resultado
