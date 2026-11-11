"""Diagnóstico da Avaliação de Cenário: a matriz por categoria e o payload do consultor (Etapa 2).

Função pura (sem banco, Flask nem config), no processo pai, sobre as linhas da staging de um lead
(brainstorming_dados_pdv) e o resumo gravado no lead. Não lê arquivo nenhum: o fluxo do lead recebe
só relatórios de VENDAS. Por isso:
  - "Compras" não entra (o lead não envia relatório de compras);
  - "Perdas operacionais" (quebra, validade, avaria, furto) fica nulo: exige relatório de perdas ou
    inventário. O que existe são as vendas abaixo do custo (a perda por linha do Raio-X);
  - margem e CMV são os do Raio-X, sobre as vendas com custo informado pelo PDV.

Matriz por categoria (matriz_por_categoria): a categoria do produto é a do ano mais recente que tiver
uma (triagem_cenario.chave_da_categoria agrupa grafias). Para cada categoria e para o total: itens,
faturamento (do envio e do último ano), participação no último ano, faturamento de itens de venda
contínua (último ano mensal, com ao menos 2 relatórios completos: venderam em ao menos
FRACAO_VENDA_CONTINUA dos relatórios completos, os de período já terminado), venda com custo, custo das
vendas, custo das bonificações e das devoluções, CMV líquido, margem bruta estimada, margem com
bonificações e devoluções, vendas abaixo do custo, vazamento de preço, potencial recuperável
(retroativo), estoque parado (estoque x custo médio dos itens com estagnação crítica e estoque > 0, em
qualquer bloco; nulo sem a coluna de estoque), itens por bloco, marcas, cauda e itens a confirmar
(custo implausível). O total soma também os ajustes de bonificação e devolução de produto sem venda no
ano (meta), e fecha com a auditoria do Raio-X. A matriz só é publicada com categoria em ao menos
COBERTURA_MINIMA_DA_CATEGORIA do faturamento; mostra até CATEGORIAS_PUBLICADAS_MAXIMO categorias (as
demais somadas numa linha só), e "Sem categoria" vai por último, fora da ordenação.

Impacto de cada item (impacto_do_produto): por ano, o maior entre o potencial recuperável, a venda sem
custo exposta (venda em relatório com a coluna de custo e sem custo), a oportunidade de margem (só com
motivo de margem: distância até a margem de referência x venda com custo) e, no último ano, o estoque
parado, também do item que não vendeu no último ano; item de custo implausível tem impacto zero e
aparece como "a confirmar". A ordenação é pelo impacto do último ano, depois o acumulado e o nome.

Inteligência de estoque e de preço (Etapa 3, inteligencia_estoque.calcular): estoque parado por
idade (60, 90 e 180 dias sem venda), estoque excedente, simulador de liquidação da Urgência e auditoria
de preço pelo markup de referência, na seção "inteligencia" do payload. Sem estoque informado (a coluna
falta em todos os relatórios ou vem vazia em todas as linhas), o estoque e o simulador ficam nulos, e o
estoque parado da matriz também; sem os períodos cobertos na meta (envio de antes da Etapa 3), a seção
inteira fica nula.

Visualizações (Etapa 4, visualizacoes): séries prontas para gráficos (rótulos e valores numéricos),
tiradas do próprio payload, sem cálculo novo: a triagem por bloco, as três marcas, o caixa resgatável
(cada parcela com a natureza dela: retroativo, capital parado, capital liberável ou projeção, que não
se somam), o estoque por idade, as categorias por impacto, o simulador e a auditoria de preço.

Payload (montar_payload, audiência: consultor): só sai pela rota admin_required
`/admin/leads/<id>/diagnostico.json`, nunca leva o contato do lead e só leva texto do PDV (nome,
código, categoria) enquanto as linhas existem e estão dentro da retenção de 30 dias. Linhas de outra
versão das regras, triagem de outra versão ou dado gravado ilegível: "desatualizado", só o resumo
gravado. Toda linha precisa ser do próprio lead (lead_id): linha de outro lead é erro do chamador.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import math

import avaliacao_motor
import inteligencia_estoque
import triagem_cenario

VERSAO_DIAGNOSTICO = "diagnostico-lead-2026-11.3"
FRACAO_VENDA_CONTINUA = Fraction(3, 4)
COBERTURA_MINIMA_DA_CATEGORIA = Fraction(1, 2)
ITENS_POR_BLOCO_PADRAO = 30
ITENS_POR_BLOCO_MAXIMO = 200
# Categorias publicadas na matriz: as de maior impacto; as demais saem somadas numa linha só (um
# catálogo com milhares de grupos faria a tela e o JSON passarem de megabytes).
CATEGORIAS_PUBLICADAS_MAXIMO = 60
SEM_CATEGORIA = "Sem categoria"
DEMAIS_CATEGORIAS = "Demais categorias"
_ZERO = Decimal("0")
_MOTIVOS_DE_MARGEM = frozenset({"margem_baixa_relativa", "margem_abaixo_do_mix"})
# Exceções de dado gravado fora do formato (meta, limiares ou linhas alteradas no banco): o lead fica
# "desatualizado", nunca erro 500.
_DADO_ILEGIVEL = (ValueError, KeyError, TypeError, AttributeError, ArithmeticError, RecursionError, InvalidOperation)

DEFINICOES = {
    "faturamento": "Venda bruta das linhas com quantidade e valor maiores que zero, sem descontar devoluções.",
    "faturamento_ultimo_ano": "Faturamento só do último ano do envio.",
    "participacao_ultimo_ano": "Faturamento do último ano da categoria sobre o do lead.",
    "venda_com_custo": "Venda das linhas que trazem custo informado pelo PDV.",
    "custo_das_vendas": "Custo dessas vendas, informado pelo PDV (não é o CMV contábil).",
    "custo_bonificacoes": "Custo dos itens entregues sem cobrança ao cliente (valor zero informado).",
    "custo_devolucoes": "Custo das devoluções com custo, que voltaram ao estoque.",
    "cmv_liquido": "Custo das vendas + custo das bonificações - custo das devoluções.",
    "margem_bruta_estimada": "Venda com custo - custo das vendas, sobre a venda com custo.",
    "margem_com_bonificacoes_e_devolucoes": ("(Venda com custo - receita devolvida) - CMV líquido, sobre a "
                                             "venda com custo menos a receita devolvida (percentual nulo quando "
                                             "essa receita não é positiva)."),
    "vendas_abaixo_do_custo": ("Soma, linha a linha, do custo menos o valor vendido quando a venda ficou abaixo do custo, "
                               "fora o ano de custo a confirmar (custo implausível)."),
    "vazamento_de_preco": "Venda abaixo do preço de referência do próprio relatório (tabela ou o mais praticado).",
    "potencial_recuperavel": ("Retroativo: o maior, por linha, entre a venda abaixo do preço de referência e a "
                              "venda abaixo do custo; no ano de custo a confirmar (custo implausível), só a venda "
                              "abaixo do preço de referência. Inclui promoção intencional."),
    "estoque_parado": ("Estoque informado x custo médio dos itens com venda parada, em queda forte ou com giro "
                       "crítico e estoque positivo, em qualquer bloco."),
    "faturamento_venda_continua": ("Venda do último ano dos itens que venderam em ao menos 75% dos relatórios "
                                   "mensais completos (período já terminado); nulo sem relatórios mensais no último "
                                   "ano ou com menos de 2 completos."),
    "perdas_operacionais": "Não calculado: exige relatório de perdas ou inventário (quebra, validade, avaria).",
    "impacto": ("Por item e ano, o maior entre o potencial recuperável, a venda sem custo exposta, a oportunidade de "
                "margem e o estoque parado (este, no último ano)."),
    "impacto_acumulado": "Soma do impacto de todos os anos do envio.",
    "itens_por_bloco": "Produtos da categoria em cada bloco da triagem.",
    "marcas": ("Produtos com venda sem custo, com alguma venda abaixo do custo (fora o ano de custo a confirmar) e "
               "com omissão no ERP."),
    "cauda": "Produtos fora dos 95% do faturamento do último ano e a participação deles.",
    "a_confirmar": "Produtos com custo implausível (unidade ou embalagem trocada), fora do impacto.",
}
# Os três textos de antes da triagem-2026-11.4, em que o ano de custo a confirmar ainda contava na venda abaixo do
# custo, no recuperável e na marca: o lead triado por outra versão mostra a regra com que foi triado, como a tela.
_DEFINICOES_ANTES_DO_CUSTO_A_CONFIRMAR_POR_ANO = {
    "vendas_abaixo_do_custo": "Soma, linha a linha, do custo menos o valor vendido quando a venda ficou abaixo do custo.",
    "potencial_recuperavel": ("Retroativo: o maior, por linha, entre a venda abaixo do preço de referência e a "
                              "venda abaixo do custo. Inclui promoção intencional."),
    "marcas": "Produtos com venda sem custo, com alguma venda abaixo do custo e com omissão no ERP.",
}


# Antes da Etapa 2 (triagem-2026-11.2 ou lead sem versão), a marca era o preço médio abaixo do custo médio, e a
# omissão no ERP não existia: a tela mostra essa regra, e o JSON também.
_DEFINICOES_ANTES_DA_ETAPA_2 = {
    "marcas": ("Produtos com venda sem custo em algum ano e com preço médio abaixo do custo médio registrado (a versão "
               "não marcava a omissão no ERP)."),
}
_ROTULOS_DAS_MARCAS = {
    True: (("Venda sem custo", "itens_venda_sem_custo"), ("Venda abaixo do custo", "itens_venda_abaixo_custo"),
           ("Omissão no ERP", "itens_omissao_erp")),
    False: (("Venda sem custo", "itens_venda_sem_custo"), ("Preço médio abaixo do custo", "itens_venda_abaixo_custo")),
}


def definicoes_da_versao(versao_triagem) -> dict:
    """As definições do diagnóstico para a versão da triagem gravada no lead (uma cópia; DEFINICOES não muda): a
    definição das marcas segue VERSOES_COM_MARCAS_DA_ETAPA_2, e a ressalva do custo a confirmar segue
    VERSOES_COM_CUSTO_A_CONFIRMAR_POR_ANO, como a tela."""
    if versao_triagem in triagem_cenario.VERSOES_COM_CUSTO_A_CONFIRMAR_POR_ANO:
        return dict(DEFINICOES)
    definicoes = {**DEFINICOES, **_DEFINICOES_ANTES_DO_CUSTO_A_CONFIRMAR_POR_ANO}
    if versao_triagem not in triagem_cenario.VERSOES_COM_MARCAS_DA_ETAPA_2:
        definicoes.update(_DEFINICOES_ANTES_DA_ETAPA_2)
    return definicoes


def _dinheiro(valor) -> float:
    numero = float(valor)
    return round(numero, 2) if math.isfinite(numero) else 0.0


def _fracao(parte, todo):
    parte, todo = Decimal(parte), Decimal(todo)
    if todo <= 0:
        return None
    numero = float(parte / todo)
    return round(numero, 4) if math.isfinite(numero) else None


def _margem(venda, custo):
    """Margem em valor e percentual; nula quando a base (venda) não é positiva."""
    venda, custo = Decimal(venda), Decimal(custo)
    if venda <= 0:
        return None
    return {"valor": _dinheiro(venda - custo), "percentual": _fracao(venda - custo, venda)}


def _margem_liquida(receita, cmv):
    """Como o Raio-X (raio_x._combinar): o valor sai sempre; o percentual, só com receita positiva."""
    receita, cmv = Decimal(receita), Decimal(cmv)
    return {"valor": _dinheiro(receita - cmv), "percentual": _fracao(receita - cmv, receita)}


def _por_produto(linhas: list) -> dict:
    produtos: dict = {}
    for linha in linhas:
        produtos.setdefault(linha["chave_produto"], {})[int(linha["ano_exercicio"])] = linha
    return produtos


def _rotulos_de_categoria(produtos: dict) -> dict:
    """Chave normalizada -> o nome mais recente (o de maior ano; empate: o menor texto)."""
    escolhido: dict = {}
    for por_ano in produtos.values():
        for ano, linha in por_ano.items():
            nome = linha.get("categoria_produto")
            if isinstance(nome, str) and nome.strip():
                chave = triagem_cenario.chave_da_categoria(nome)
                candidato = (ano, nome)
                atual = escolhido.get(chave)
                if atual is None or candidato[0] > atual[0] or (candidato[0] == atual[0] and candidato[1] < atual[1]):
                    escolhido[chave] = candidato
    return {chave: nome for chave, (_ano, nome) in escolhido.items()}


def _custo_medio(por_ano: dict):
    for ano in sorted(por_ano, reverse=True):
        linha = por_ano[ano]
        if linha["quantidade_com_custo"] > 0 and linha["custo_total"] > 0:
            return linha["custo_total"] / linha["quantidade_com_custo"]
    return None


def estoque_parado(por_ano: dict, chave: str, triagem):
    """Estoque informado x custo médio do item com estagnação crítica (venda parada, queda forte ou
    giro crítico) e estoque positivo, em qualquer bloco (triagem.estagnados)."""
    if chave not in triagem.estagnados or chave in triagem.a_confirmar:
        return None
    estoque = triagem_cenario._estoque(por_ano)
    custo_medio = _custo_medio(por_ano)
    if estoque is None or estoque <= 0 or custo_medio is None:
        return None
    return Decimal(estoque) * custo_medio


def impacto_do_produto(por_ano: dict, chave: str, triagem, ultimo: int) -> tuple:
    """(impacto do último ano, impacto acumulado, origem do impacto no último ano)."""
    if chave in triagem.a_confirmar:
        return _ZERO, _ZERO, "a_confirmar"
    motivos = triagem.motivos.get(chave, ())
    referencia = (triagem.margem_referencia or {}).get(chave)
    total, ultimo_ano, origem = _ZERO, _ZERO, None
    for ano, linha in sorted(por_ano.items()):
        componentes = {
            "potencial_recuperavel": linha["recuperavel"],
            "venda_sem_custo": max(triagem_cenario._valor(linha, "faturamento_em_relatorio_com_custo",
                                                          linha["valor_venda_bruto"]) - linha["faturamento_com_custo"], _ZERO),
        }
        if motivos and _MOTIVOS_DE_MARGEM & set(motivos) and referencia is not None and linha["faturamento_com_custo"] > 0:
            margem = Fraction(linha["faturamento_com_custo"] - linha["custo_total"]) / Fraction(linha["faturamento_com_custo"])
            if referencia > margem:
                distancia = referencia - margem
                componentes["oportunidade_de_margem"] = (Decimal(distancia.numerator) / Decimal(distancia.denominator)
                                                          * linha["faturamento_com_custo"])
        if ano == ultimo:
            parado = estoque_parado(por_ano, chave, triagem)
            if parado is not None:
                componentes["estoque_parado"] = parado
        nome, valor = max(componentes.items(), key=lambda item: (item[1], item[0]))
        total += valor
        if ano == ultimo:
            ultimo_ano, origem = valor, (nome if valor > 0 else None)
    if ultimo not in por_ano:
        # O item que parou de vender não tem linha no último ano: o estoque parado dele é o impacto
        # desse ano (é o caso típico da Urgência por estagnação).
        parado = estoque_parado(por_ano, chave, triagem)
        if parado is not None and parado > 0:
            ultimo_ano, origem = parado, "estoque_parado"
            total += parado
    return ultimo_ano, total, origem


def _vazio() -> dict:
    return {
        "itens": 0, "faturamento": _ZERO, "faturamento_ultimo_ano": _ZERO, "venda_continua": _ZERO,
        "venda_com_custo": _ZERO, "custo_das_vendas": _ZERO, "custo_bonificacoes": _ZERO, "custo_devolucoes": _ZERO,
        "receita_devolvida_com_custo": _ZERO, "vendas_abaixo_do_custo": _ZERO, "vazamento_de_preco": _ZERO,
        "potencial_recuperavel": _ZERO, "estoque_parado": _ZERO, "impacto": _ZERO, "impacto_acumulado": _ZERO,
        "blocos": Counter(), "marcas": Counter(), "cauda_itens": 0, "cauda_faturamento": _ZERO,
        "a_confirmar_itens": 0, "a_confirmar_faturamento": _ZERO,
    }


def _somar(destino: dict, origem: dict) -> None:
    for nome, valor in origem.items():
        destino[nome] = destino[nome] + valor


def _fechar(soma: dict, *, total_ultimo_ano: Decimal, venda_continua_mensuravel: bool, tem_estoque: bool) -> dict:
    com_custo, custo = soma["venda_com_custo"], soma["custo_das_vendas"]
    cmv_liquido = custo + soma["custo_bonificacoes"] - soma["custo_devolucoes"]
    receita_liquida = com_custo - soma["receita_devolvida_com_custo"]
    return {
        "itens": soma["itens"],
        "faturamento": _dinheiro(soma["faturamento"]),
        "faturamento_ultimo_ano": _dinheiro(soma["faturamento_ultimo_ano"]),
        "participacao_ultimo_ano": _fracao(soma["faturamento_ultimo_ano"], total_ultimo_ano),
        "faturamento_venda_continua": _dinheiro(soma["venda_continua"]) if venda_continua_mensuravel else None,
        "venda_com_custo": _dinheiro(com_custo),
        "custo_das_vendas": _dinheiro(custo) if com_custo > 0 else None,
        "custo_bonificacoes": _dinheiro(soma["custo_bonificacoes"]),
        "custo_devolucoes": _dinheiro(soma["custo_devolucoes"]),
        "cmv_liquido": _dinheiro(cmv_liquido) if com_custo > 0 else None,
        "margem_bruta_estimada": _margem(com_custo, custo),
        "margem_com_bonificacoes_e_devolucoes": _margem_liquida(receita_liquida, cmv_liquido) if com_custo > 0 else None,
        "vendas_abaixo_do_custo": _dinheiro(soma["vendas_abaixo_do_custo"]),
        "vazamento_de_preco": _dinheiro(soma["vazamento_de_preco"]),
        "potencial_recuperavel": _dinheiro(soma["potencial_recuperavel"]),
        "estoque_parado": _dinheiro(soma["estoque_parado"]) if tem_estoque else None,
        "perdas_operacionais": None,
        "itens_por_bloco": {bloco: soma["blocos"][bloco] for bloco in triagem_cenario.BLOCOS},
        "marcas": {nome: soma["marcas"][nome] for nome in ("sem_custo", "abaixo_do_custo", "omissao_erp")},
        "cauda": {"itens": soma["cauda_itens"], "participacao": _fracao(soma["cauda_faturamento"], total_ultimo_ano)},
        "a_confirmar": {"itens": soma["a_confirmar_itens"], "faturamento": _dinheiro(soma["a_confirmar_faturamento"])},
        "impacto": _dinheiro(soma["impacto"]),
        "impacto_acumulado": _dinheiro(soma["impacto_acumulado"]),
    }


def _ajustes_da_meta(meta: dict, anos) -> list:
    """Os ajustes de produto sem venda da meta gravada, com o formato e o teto do contrato do processo
    isolado (etl_runtime._meta_valida): um por ano do envio, as três somas como número (nunca texto),
    finitas, não negativas e abaixo do teto do dinheiro. Fora disso: ValueError ("desatualizado")."""
    ajustes = meta.get("ajustes_sem_venda_por_ano")
    if not isinstance(ajustes, dict) or set(ajustes) != {str(ano) for ano in anos}:
        raise ValueError("Ajustes da meta fora do formato.")
    saida = []
    for valores in ajustes.values():
        if not isinstance(valores, dict) or set(valores) != set(avaliacao_motor.CHAVES_AJUSTES):
            raise ValueError("Ajustes da meta fora do formato.")
        convertidos = {}
        for chave, valor in valores.items():
            if type(valor) not in (int, float) or not math.isfinite(valor) or not 0 <= valor < avaliacao_motor.TETO_DINHEIRO_DA_META:
                raise ValueError("Ajuste da meta fora do formato.")
            convertidos[chave] = Decimal(str(valor))
        saida.append(convertidos)
    return saida


def matriz_por_categoria(linhas: list, cobertura, triagem, meta: dict) -> dict:
    """{"totais", "categorias" (lista ordenada por impacto, "Sem categoria" por último) ou None,
    "categorias_publicadas", "cobertura_categoria"}."""
    produtos = _por_produto(linhas)
    ultimo = max(cobertura.relatorios)
    rotulos = _rotulos_de_categoria(produtos)
    mensal = bool(cobertura.mensais.get(ultimo))
    completos = (cobertura.completos or {}).get(ultimo) if cobertura.completos is not None else cobertura.relatorios[ultimo]
    venda_continua_mensuravel = mensal and completos is not None and completos >= 2
    # O critério da inteligência: a coluna vazia em todas as linhas é estoque não informado (nulo), não zero.
    tem_estoque = inteligencia_estoque.estoque_informado(linhas, cobertura)
    somas: dict = defaultdict(_vazio)
    total = _vazio()
    total_ultimo_ano = sum((anos[ultimo]["valor_venda_bruto"] for anos in produtos.values() if ultimo in anos), _ZERO)
    for chave, por_ano in produtos.items():
        categoria = triagem_cenario._categoria(por_ano)
        impacto_ultimo, impacto_total, _origem = impacto_do_produto(por_ano, chave, triagem, ultimo)
        # O ano de custo a confirmar é inconsistência de custo (a_confirmar_itens), não venda abaixo do custo; o
        # outro ano do mesmo produto conta normalmente (a regra da triagem).
        implausivel = {ano: triagem_cenario.custo_implausivel(l, triagem.limiares) for ano, l in por_ano.items()}
        marcas = {
            "sem_custo": any(triagem_cenario.marcas(l, triagem.limiares)["flag_venda_sem_custo"] for l in por_ano.values()),
            "abaixo_do_custo": any(l["perda"] > 0 and not implausivel[ano] for ano, l in por_ano.items()),
            "omissao_erp": bool((triagem.omissoes or {}).get(chave)),
        }
        parado = estoque_parado(por_ano, chave, triagem) or _ZERO
        venda_ultimo = por_ano[ultimo]["valor_venda_bruto"] if ultimo in por_ano else _ZERO
        continua = _ZERO
        if venda_continua_mensuravel and ultimo in por_ano:
            meses_completos = triagem_cenario._valor(por_ano[ultimo], "meses_completos", por_ano[ultimo]["meses"])
            if Fraction(meses_completos, completos) >= FRACAO_VENDA_CONTINUA:
                continua = venda_ultimo
        for soma in (somas[categoria], total):
            soma["itens"] += 1
            soma["faturamento"] += sum((l["valor_venda_bruto"] for l in por_ano.values()), _ZERO)
            soma["faturamento_ultimo_ano"] += venda_ultimo
            soma["venda_continua"] += continua
            # No ano de custo a confirmar, a perda contra o custo implausível fica fora da venda abaixo do custo e do
            # recuperável (que fica só com a variação de preço), como na triagem.
            for nome, coluna in (("venda_com_custo", "faturamento_com_custo"), ("custo_das_vendas", "custo_total"),
                                 ("vazamento_de_preco", "vazamento_preco")):
                soma[nome] += sum((l[coluna] for l in por_ano.values()), _ZERO)
            soma["vendas_abaixo_do_custo"] += sum((triagem_cenario.perda_real_do_ano(l, triagem.limiares)
                                                   for l in por_ano.values()), _ZERO)
            soma["potencial_recuperavel"] += sum((triagem_cenario.recuperavel_do_ano(l, triagem.limiares)
                                                  for l in por_ano.values()), _ZERO)
            for nome, coluna in (("custo_bonificacoes", "custo_bonificacoes"), ("custo_devolucoes", "custo_devolucoes"),
                                 ("receita_devolvida_com_custo", "devolucoes_com_custo")):
                soma[nome] += sum((triagem_cenario._valor(l, coluna, _ZERO) for l in por_ano.values()), _ZERO)
            soma["estoque_parado"] += parado
            soma["impacto"] += impacto_ultimo
            soma["impacto_acumulado"] += impacto_total
            bloco = triagem.blocos.get(chave)
            if bloco:
                soma["blocos"][bloco] += 1
            for nome, marcado in marcas.items():
                soma["marcas"][nome] += marcado
            if chave in triagem.cauda:
                soma["cauda_itens"] += 1
                soma["cauda_faturamento"] += venda_ultimo
            if chave in triagem.a_confirmar:
                soma["a_confirmar_itens"] += 1
                soma["a_confirmar_faturamento"] += sum((l["valor_venda_bruto"] for l in por_ano.values()), _ZERO)
    # Bonificação e devolução de produto sem venda no ano: entram só no total (não têm categoria).
    for ajustes in _ajustes_da_meta(meta, cobertura.relatorios):
        total["custo_bonificacoes"] += ajustes["custo_bonificacoes"]
        total["custo_devolucoes"] += ajustes["custo_devolucoes"]
        total["receita_devolvida_com_custo"] += ajustes["devolucoes_com_custo"]
    opcoes = {"total_ultimo_ano": total_ultimo_ano, "venda_continua_mensuravel": venda_continua_mensuravel,
              "tem_estoque": tem_estoque}
    faturamento_total = total["faturamento"]
    com_categoria = sum((soma["faturamento"] for chave, soma in somas.items() if chave is not None), _ZERO)
    cobertura_categoria = _fracao(com_categoria, faturamento_total)
    publicadas = faturamento_total > 0 and Fraction(com_categoria) >= COBERTURA_MINIMA_DA_CATEGORIA * Fraction(faturamento_total)
    categorias = None
    if publicadas:
        ordenadas = sorted((chave for chave in somas if chave is not None),
                           key=lambda c: (-somas[c]["impacto"], -somas[c]["impacto_acumulado"], -somas[c]["faturamento"],
                                          rotulos.get(c, c)))
        mostradas, demais = ordenadas[:CATEGORIAS_PUBLICADAS_MAXIMO], ordenadas[CATEGORIAS_PUBLICADAS_MAXIMO:]
        categorias = [{"categoria": rotulos.get(chave, chave), **_fechar(somas[chave], **opcoes)} for chave in mostradas]
        if demais:
            soma_demais = _vazio()
            for chave in demais:
                _somar(soma_demais, somas[chave])
            categorias.append({"categoria": f"{DEMAIS_CATEGORIAS} ({len(demais)})", **_fechar(soma_demais, **opcoes)})
        if None in somas:
            categorias.append({"categoria": SEM_CATEGORIA, **_fechar(somas[None], **opcoes)})
    return {"totais": _fechar(total, **opcoes), "categorias": categorias, "categorias_publicadas": publicadas,
            "cobertura_categoria": cobertura_categoria}


def itens_dos_blocos(linhas: list, cobertura, triagem, limite: int = ITENS_POR_BLOCO_PADRAO) -> dict:
    """Os itens de cada bloco, ordenados por impacto (último ano, acumulado, nome), até `limite`. O
    impacto de todos os itens decide a ordem; o resto (rótulos, omissões, categoria) só é montado para
    os itens mostrados."""
    produtos = _por_produto(linhas)
    ultimo = max(cobertura.relatorios)
    rotulos = _rotulos_de_categoria(produtos)
    por_bloco = {bloco: [] for bloco in triagem_cenario.BLOCOS}
    for chave, por_ano in produtos.items():
        bloco = triagem.blocos.get(chave)
        if bloco not in por_bloco:
            continue
        impacto_ultimo, impacto_total, origem = impacto_do_produto(por_ano, chave, triagem, ultimo)
        recente = por_ano[max(por_ano)]
        por_bloco[bloco].append(((-impacto_ultimo, -impacto_total, recente["descricao_produto"], chave),
                                 chave, impacto_ultimo, impacto_total, origem))
    rotulo_do_motivo: dict = {}

    def rotulo(codigo):
        if codigo not in rotulo_do_motivo:
            rotulo_do_motivo[codigo] = triagem_cenario.rotulo_do_motivo(codigo, triagem.limiares)
        return rotulo_do_motivo[codigo]

    saida = {}
    for bloco, candidatos in por_bloco.items():
        candidatos.sort(key=lambda item: item[0])
        itens = []
        for _ordem, chave, impacto_ultimo, impacto_total, origem in candidatos[:limite]:
            por_ano = produtos[chave]
            anos = sorted(por_ano)
            recente = por_ano[anos[-1]]
            codigo = next((por_ano[a].get("codigo_produto") for a in reversed(anos) if por_ano[a].get("codigo_produto")), None)
            categoria = triagem_cenario._categoria(por_ano)
            itens.append({
                "produto": recente["descricao_produto"],
                "codigo": codigo,
                "categoria": rotulos.get(categoria) if categoria is not None else None,
                "anos": anos,
                "faturamento": _dinheiro(sum((l["valor_venda_bruto"] for l in por_ano.values()), _ZERO)),
                "faturamento_ultimo_ano": _dinheiro(por_ano[ultimo]["valor_venda_bruto"]) if ultimo in por_ano else 0.0,
                # A regra por ano da triagem: no ano de custo a confirmar, a perda contra o custo implausível não
                # é recuperável nem venda abaixo do custo (a soma dos itens fecha com o resumo do bloco).
                "potencial_recuperavel": _dinheiro(sum((triagem_cenario.recuperavel_do_ano(l, triagem.limiares)
                                                        for l in por_ano.values()), _ZERO)),
                "vendas_abaixo_do_custo": _dinheiro(sum((triagem_cenario.perda_real_do_ano(l, triagem.limiares)
                                                         for l in por_ano.values()), _ZERO)),
                "impacto": _dinheiro(impacto_ultimo),
                "impacto_acumulado": _dinheiro(impacto_total),
                "origem_do_impacto": origem,
                "motivos": [{"codigo": m, "rotulo": rotulo(m)} for m in triagem.motivos.get(chave, ())],
                "omissoes": [{"codigo": m, "rotulo": triagem_cenario.ROTULOS_DAS_OMISSOES[m]}
                             for m in (triagem.omissoes or {}).get(chave, ())],
                "a_confirmar": chave in triagem.a_confirmar,
            })
        saida[bloco] = {"total": len(candidatos), "mostrando": len(itens), "itens": itens}
    return saida


def _cobertura_publica(cobertura) -> dict:
    def contagem(valores, ano):
        return None if valores is None else valores.get(ano)
    return {str(ano): {
        "relatorios": cobertura.relatorios[ano], "dias": cobertura.dias[ano], "mensais": cobertura.mensais[ano],
        "com_custo": contagem(cobertura.com_custo, ano), "com_ncm": contagem(cobertura.com_ncm, ano),
        "com_categoria": contagem(cobertura.com_categoria, ano), "com_estoque": contagem(cobertura.com_estoque, ano),
        "completos": contagem(cobertura.completos, ano),
    } for ano in sorted(cobertura.relatorios)}


def _float_finito(texto: str) -> float:
    numero = float(texto)
    if not math.isfinite(numero):
        raise ValueError("Número não finito no JSON gravado.")
    return numero


def _constante_recusada(nome):
    raise ValueError(f"Constante {nome} fora do JSON.")


# Maior inteiro (em dígitos) que um JSON gravado no lead pode trazer: nenhum valor gravado chega perto
# (contagens e dinheiro abaixo de 10¹²), e um inteiro gigante derrubaria float() e math.isfinite().
_DIGITOS_DO_MAIOR_INTEIRO = 18


def _inteiro_limitado(texto: str) -> int:
    if len(texto.lstrip("-")) > _DIGITOS_DO_MAIOR_INTEIRO:
        raise ValueError("Inteiro grande demais no JSON gravado.")
    return int(texto)


def ler_objeto(texto):
    """O JSON gravado se for um objeto com números finitos; senão (inválido, NaN, Infinity, 1e999,
    inteiro com mais de 18 dígitos ou aninhado demais), None. O jsonify escreveria um número não finito
    como JSON inválido."""
    import json
    try:
        valor = (json.loads(texto, parse_constant=_constante_recusada, parse_float=_float_finito,
                            parse_int=_inteiro_limitado) if texto else None)
    except (ValueError, RecursionError):
        return None
    return valor if isinstance(valor, dict) else None


# Resumos gravados no lead (ficam depois do expurgo): validados aqui, num lugar só, para o payload, as
# visualizações e as telas. Número fora do formato (texto onde vai número, negativo, não finito ou acima
# do teto do dinheiro) faz o resumo inteiro valer nada, sem erro 500.
_TETO_DO_RESUMO = avaliacao_motor.TETO_DINHEIRO_DA_META
_VALORES_DO_RESUMO_DA_INTELIGENCIA = (
    "capital_em_estoque", "capital_excedente_liberavel", "urgencia_receita_a_preco_medio",
    "urgencia_capital_ao_custo", "urgencia_itens_com_estoque", "diferenca_de_preco_adicional",
    "itens_abaixo_do_preco_de_referencia")
# Contagens do resumo: com estoque informado, inteiros obrigatórios (a falta de uma diria "soma completa", o
# que não se sabe); sem ele, nulas.
_CONTAGENS_DO_RESUMO = ("itens_sem_estoque_informado", "itens_sem_estoque_com_custo",
                        "itens_parados_sem_estoque_informado")


def _numero_do_resumo(valor, *, inteiro: bool = False, texto: bool = True):
    """O número de um resumo gravado se for finito, não negativo e abaixo do teto; senão None. Com
    `inteiro`, só int (nunca bool, float ou texto); sem `texto`, texto decimal não vale."""
    if isinstance(valor, bool):
        return None
    if inteiro:
        return valor if type(valor) is int and 0 <= valor < _TETO_DO_RESUMO else None
    if not isinstance(valor, (int, float)) and not (texto and isinstance(valor, str)):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError, OverflowError):
        return None
    return numero if math.isfinite(numero) and 0 <= numero < _TETO_DO_RESUMO else None


def resumo_de_bloco_valido(resumo):
    """O resumo gravado de um bloco da triagem (triagem_cenario: itens, faturamento e recuperável em texto
    decimal, motivos {código: contagem}) só com esse formato e as duas somas presentes; senão None."""
    if not isinstance(resumo, dict):
        return None
    motivos = resumo.get("motivos", {})
    if (_numero_do_resumo(resumo.get("itens"), inteiro=True) is None or not isinstance(motivos, dict)
            or not all(isinstance(codigo, str) and _numero_do_resumo(quantos, inteiro=True) is not None
                       for codigo, quantos in motivos.items())
            or any(_numero_do_resumo(resumo.get(chave)) is None for chave in ("faturamento", "recuperavel"))):
        return None
    return resumo


def resumo_inteligencia_valido(resumo):
    """O resumo numérico da inteligência gravado no lead (inteligencia_estoque.resumo) só se for da
    versão atual e tiver o formato: com_estoque booleano, valores numéricos (nunca texto) finitos, não
    negativos e abaixo do teto, ou nulos; faixas {dias em dígitos: valor}, até 5; com estoque informado, as
    contagens de produtos sem saldo (inteiros, obrigatórias) e, sem ele, nulas. Senão, None: um resumo
    de outra versão pode dar a um campo outro sentido."""
    if not isinstance(resumo, dict) or resumo.get("versao") != inteligencia_estoque.VERSAO_INTELIGENCIA:
        return None

    def valido(valor):
        return valor is None or _numero_do_resumo(valor, texto=False) is not None

    if type(resumo.get("com_estoque")) is not bool or not all(valido(resumo.get(c)) for c in _VALORES_DO_RESUMO_DA_INTELIGENCIA):
        return None
    if resumo["com_estoque"]:
        if not all(_numero_do_resumo(resumo.get(c), inteiro=True) is not None for c in _CONTAGENS_DO_RESUMO):
            return None
    elif not all(resumo.get(c) is None for c in _CONTAGENS_DO_RESUMO):
        return None
    for chave in ("capital_parado", "itens_parados"):
        faixas = resumo.get(chave)
        if faixas is not None and not (isinstance(faixas, dict) and len(faixas) <= 5 and all(
                isinstance(dias, str) and dias.isascii() and dias.isdigit() and len(dias) <= 4 and valido(valor)
                for dias, valor in faixas.items())):
            return None
    return resumo


def montar_payload(lead: dict, registros: list, *, agora, retencao_dias: int,
                   limite: int = ITENS_POR_BLOCO_PADRAO, parametros_inteligencia=None) -> dict:
    """O payload do consultor. `lead`: os campos do lead (id, status, versões, período, arquivos,
    score_saude_cadastral, plano_recomendado_codigo, vazamento_total_reais, itens_*, resumos_json
    {bloco: texto}, meta_json, limiares_triagem_json); `registros`: as linhas da staging no formato de
    avaliacao_cenario.linha_da_staging, cada uma com `lead_id`, `versao_regras` e `criado_em`. Sem o
    contato do lead. Linha de outro lead (ou sem lead_id) é recusada com ValueError: o payload nunca
    mistura leads, nem por um filtro esquecido no chamador."""
    if any(r.get("lead_id") != lead["id"] for r in registros):
        raise ValueError("Há linhas de PDV de outro lead no diagnóstico.")
    limite = max(1, min(int(limite), ITENS_POR_BLOCO_MAXIMO))
    payload = {
        "versao": VERSAO_DIAGNOSTICO,
        "audiencia": "consultor",
        "gerado_em": agora.isoformat(),
        "lead": {
            "id": str(lead["id"]), "status": lead["status"], "versao_regras": lead.get("versao_regras"),
            "versao_triagem": lead.get("versao_triagem"),
            "periodo": {"inicio": lead["periodo_inicio"].isoformat() if lead.get("periodo_inicio") else None,
                        "fim": lead["periodo_fim"].isoformat() if lead.get("periodo_fim") else None},
            "arquivos": lead.get("arquivos"),
            "score_saude_cadastral": float(lead["score_saude_cadastral"]) if lead.get("score_saude_cadastral") is not None else None,
            "plano_recomendado": lead.get("plano_recomendado_codigo"),
            "potencial_recuperavel_total": (_dinheiro(lead["vazamento_total_reais"])
                                            if lead.get("vazamento_total_reais") is not None else None),
        },
        "contagens": {nome: lead.get(nome) for nome in ("itens_venda_sem_custo", "itens_venda_abaixo_custo",
                                                         "itens_omissao_erp")},
        "resumos": {bloco: ler_objeto(lead.get("resumos_json", {}).get(bloco)) for bloco in triagem_cenario.BLOCOS},
        "limiares": ler_objeto(lead.get("limiares_triagem_json")),
        "situacao": "atual", "dados_pdv_ate": None, "cobertura": None, "totais": None, "categorias": None,
        "categorias_publicadas": False, "cobertura_categoria": None, "blocos": None, "inteligencia": None,
        # O resumo numérico gravado no lead (fica depois do expurgo), só se for da versão atual e válido.
        "resumo_inteligencia": resumo_inteligencia_valido(ler_objeto(lead.get("resumo_inteligencia_json"))),
        "definicoes": definicoes_da_versao(lead.get("versao_triagem")), "avisos": [], "visualizacoes": None,
    }
    expira_em = min((r["criado_em"] for r in registros), default=None)
    expira_em = expira_em + timedelta(days=retencao_dias) if expira_em is not None else None
    payload["dados_pdv_ate"] = expira_em.isoformat() if expira_em is not None else None
    if not registros or lead["status"] == "expirado" or expira_em <= agora:
        payload["situacao"] = "expirado"
        payload["avisos"].append(f"Os dados de PDV deste lead saem {retencao_dias} dias depois do envio: "
                                 "fica só o resumo. Envie os relatórios de novo para ver itens e categorias.")
        payload["visualizacoes"] = visualizacoes(payload)
        return payload
    if {r.get("versao_regras") for r in registros} != {avaliacao_motor.VERSAO_REGRAS_LEAD}:
        payload["situacao"] = "desatualizado"
        payload["avisos"].append("Os dados deste lead foram gravados por outra versão das regras: envie os "
                                 "relatórios de novo para ver itens e categorias.")
        payload["visualizacoes"] = visualizacoes(payload)
        return payload
    try:
        meta = ler_objeto(lead.get("meta_json")) or {}
        cobertura = triagem_cenario.Cobertura.do_envio(meta)
        # Como avaliacao_cenario.limiares_do_lead: sem gravação, os padrão; gravação ilegível ou de
        # outra versão não cai nos padrão em silêncio (a triagem mostrada seria outra que a gravada).
        if lead.get("limiares_triagem_json"):
            if payload["limiares"] is None:
                raise ValueError("Limiares gravados ilegíveis.")
            limiares = triagem_cenario.Limiares.de_dict(payload["limiares"])
        else:
            limiares = triagem_cenario.LIMIARES_PADRAO
        if lead.get("versao_triagem") != triagem_cenario.VERSAO_TRIAGEM:
            raise ValueError("Triagem de outra versão.")
        triagem = triagem_cenario.triar(registros, cobertura, limiares)
        matriz = matriz_por_categoria(registros, cobertura, triagem, meta)
        blocos = itens_dos_blocos(registros, cobertura, triagem, limite)
    except _DADO_ILEGIVEL:
        payload["situacao"] = "desatualizado"
        payload["avisos"].append("A triagem deste lead foi feita com regras anteriores ou o resumo gravado está "
                                 "fora do formato: use \"Refazer a triagem\" ou envie os relatórios de novo.")
        payload["visualizacoes"] = visualizacoes(payload)
        return payload
    payload.update({
        "limiares": limiares.para_dict(),
        "cobertura": _cobertura_publica(cobertura),
        "totais": matriz["totais"], "categorias": matriz["categorias"],
        "categorias_publicadas": matriz["categorias_publicadas"],
        "cobertura_categoria": matriz["cobertura_categoria"],
        "blocos": blocos,
    })
    # A inteligência (Etapa 3) à parte: um dado que ela não consegue ler tira só a seção dela, nunca a
    # matriz e os blocos.
    try:
        inteligencia = inteligencia_estoque.calcular(
            registros, cobertura, triagem, meta, parametros_inteligencia or inteligencia_estoque.PARAMETROS_PADRAO)
    except _DADO_ILEGIVEL:
        inteligencia = None
        payload["avisos"].append("A inteligência de estoque e de preço não pôde ser calculada com os dados gravados "
                                 "deste lead: envie os relatórios de novo.")
    else:
        if inteligencia["estoque"] is None:
            payload["avisos"].append("Os relatórios não trazem estoque informado (a coluna falta ou vem vazia): o "
                                     "estoque parado por idade, o excedente e o simulador de liquidação não foram "
                                     "calculados.")
    payload["inteligencia"] = inteligencia
    if not matriz["categorias_publicadas"]:
        payload["avisos"].append("Os relatórios não trazem categoria em ao menos metade do faturamento: a matriz "
                                 "por categoria não é publicada; os totais valem.")
    if cobertura.cabecalho_ambiguo:
        payload["avisos"].append(f"{cobertura.cabecalho_ambiguo} relatório(s) com cabeçalho ambíguo (por exemplo, "
                                 "\"Cód. NCM\" lido como código do produto): NCM e categoria desses relatórios não foram "
                                 "avaliados, e o conflito de cadastro não foi avaliado no envio inteiro.")
    varias_unidades = meta.get("relatorios_varias_unidades") or 0
    if varias_unidades:
        payload["avisos"].append(f"{varias_unidades} relatório(s) com várias lojas (coluna de loja ou filial): o "
                                 "estoque é a soma das lojas, e a variação de preço entre lojas pode ser preço "
                                 "regional; confira antes de tratar como vazamento. A matriz não separa as lojas.")
    if triagem.sem_mes_em_comum:
        payload["avisos"].append("Os dois últimos anos do envio não têm mês em comum (por exemplo, novembro a "
                                 "outubro): a queda do ritmo de venda não foi avaliada, para a sazonalidade não "
                                 "parecer parada. Envie os mesmos meses dos dois anos para comparar.")
    payload["visualizacoes"] = visualizacoes(payload)
    return payload


# ---------------------------------------------------------------------------
# Visualizações (Etapa 4)
# ---------------------------------------------------------------------------
CATEGORIAS_NO_GRAFICO = 10
NATUREZAS_DO_CAIXA = {
    "retroativo": "Já aconteceu: o que se recupera no preço daqui para a frente, se a causa for corrigida.",
    "capital_parado": ("Capital em estoque sem venda, ao custo, pelo último saldo informado nos relatórios "
                       "(não é inventário)."),
    "capital_liberavel": "Capital acima da cobertura-alvo em itens que ainda vendem (ao custo).",
    "projecao": "Projeção: receita do estoque da Urgência ao preço médio praticado, sem desconto.",
}


def _valor_do_resumo(valor):
    """Número de um resumo gravado (texto decimal ou número) como float finito; o resto vale None."""
    if isinstance(valor, bool) or valor is None:
        return None
    if isinstance(valor, int):
        return valor
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def _serie(nome, unidade, valores) -> dict:
    return {"nome": nome, "unidade": unidade, "valores": list(valores)}


def visualizacoes(payload: dict) -> dict:
    """Séries para gráficos, a partir do payload já montado (nada é recalculado). Cada visualização:
    {"titulo", "tipo": "barras" ou "linhas", "rotulos": [...], "series": [{"nome", "unidade" ("BRL",
    "itens" ou "fracao"), "valores": [...]}]} e, no caixa resgatável, "naturezas" (uma por rótulo).
    Só rótulos fixos, nomes de categoria (enquanto as linhas existem) e números: nunca o contato."""
    saida = {}
    blocos = triagem_cenario.BLOCOS
    resumos = payload.get("resumos") or {}

    def do_bloco(bloco, chave):
        resumo = resumo_de_bloco_valido(resumos.get(bloco))
        return _valor_do_resumo(resumo.get(chave)) if resumo is not None else None

    saida["triagem"] = {
        "titulo": "Matriz de triagem", "tipo": "barras",
        "rotulos": [triagem_cenario.ROTULOS_DOS_BLOCOS[b] for b in blocos],
        "series": [_serie("Produtos", "itens", (do_bloco(b, "itens") for b in blocos)),
                   _serie("Faturamento", "BRL", (do_bloco(b, "faturamento") for b in blocos)),
                   _serie("Potencial recuperável", "BRL", (do_bloco(b, "recuperavel") for b in blocos))],
    }
    contagens = payload.get("contagens") or {}
    # Os rótulos das marcas pela versão gravada, como a tela: antes da Etapa 2, sem a omissão no ERP.
    versao = (payload.get("lead") or {}).get("versao_triagem")
    marcas = _ROTULOS_DAS_MARCAS[versao in triagem_cenario.VERSOES_COM_MARCAS_DA_ETAPA_2]
    saida["marcas"] = {
        "titulo": "Marcas do cadastro", "tipo": "barras",
        "rotulos": [rotulo for rotulo, _nome in marcas],
        "series": [_serie("Produtos", "itens", (contagens.get(nome) for _rotulo, nome in marcas))],
    }
    # Caixa resgatável: do cálculo atual ou, sem ele (linhas expiradas, de outra versão ou ilegíveis), do
    # resumo gravado no lead. Com o cálculo atual, só ele vale: sem estoque informado, as parcelas de
    # estoque ficam nulas, diga o resumo gravado o que disser.
    calculada = payload.get("inteligencia") is not None
    inteligencia = payload.get("inteligencia") or {}
    estoque, simulador = inteligencia.get("estoque"), inteligencia.get("simulador")
    gravado = {} if calculada else (payload.get("resumo_inteligencia") or {})
    # A primeira faixa (a menor, como na lista de leads), do cálculo atual ou do resumo gravado válido.
    if estoque is not None:
        acima_de = estoque["parado"]["acima_de"]
        primeira = min(acima_de, key=int) if acima_de else None
        parado = acima_de[primeira]["capital"] if primeira else None
        excedente = estoque["excedente"]["capital_liberavel"]
    else:
        acima = gravado.get("capital_parado") or {}
        primeira = min(acima, key=int) if acima else None
        parado = _valor_do_resumo(acima.get(primeira)) if primeira else None
        excedente = _valor_do_resumo(gravado.get("capital_excedente_liberavel"))
    urgencia = (simulador["receita_a_preco_medio"] if simulador is not None
                else _valor_do_resumo(gravado.get("urgencia_receita_a_preco_medio")))
    rotulo_parado = f"Estoque parado há {primeira} dias ou mais" if primeira else "Estoque parado"
    saida["caixa_resgatavel"] = {
        "titulo": "Caixa resgatável (as parcelas não se somam)", "tipo": "barras",
        "rotulos": ["Potencial recuperável", rotulo_parado, "Estoque excedente", "Estoque da Urgência a preço médio"],
        "naturezas": ["retroativo", "capital_parado", "capital_liberavel", "projecao"],
        "series": [_serie("Valor", "BRL", (payload["lead"].get("potencial_recuperavel_total"), parado, excedente, urgencia))],
    }
    if estoque is not None:
        faixas = estoque["parado"]["faixas"]
        saida["estoque_por_idade"] = {
            "titulo": "Estoque parado por idade", "tipo": "barras", "rotulos": [f["faixa"] for f in faixas],
            "series": [_serie("Capital", "BRL", (f["capital"] for f in faixas)),
                       _serie("Produtos", "itens", (f["itens"] for f in faixas))],
        }
    categorias = [c for c in (payload.get("categorias") or []) if c.get("categoria") is not None]
    if payload.get("categorias_publicadas") and categorias:
        topo = categorias[:CATEGORIAS_NO_GRAFICO]
        saida["categorias_por_impacto"] = {
            "titulo": "Categorias por impacto no último ano", "tipo": "barras", "rotulos": [c["categoria"] for c in topo],
            "series": [_serie("Impacto", "BRL", (c.get("impacto") for c in topo)),
                       _serie("Potencial recuperável", "BRL", (c.get("potencial_recuperavel") for c in topo)),
                       _serie("Estoque parado", "BRL", (c.get("estoque_parado") for c in topo))],
        }
    if simulador is not None and simulador["cenarios"]:
        cenarios = simulador["cenarios"]
        saida["simulador"] = {
            "titulo": "Simulador de liberação de caixa (Urgência)", "tipo": "linhas",
            "rotulos": [f"{c['desconto'] * 100:g}%".replace(".", ",") for c in cenarios],
            "series": [_serie("Caixa", "BRL", (c["caixa"] for c in cenarios)),
                       _serie("Resultado contra o custo", "BRL", (c["resultado_contra_o_custo"] for c in cenarios))],
        }
    auditoria = inteligencia.get("auditoria_de_preco")
    if auditoria is not None and auditoria["categorias"]:
        topo = auditoria["categorias"][:CATEGORIAS_NO_GRAFICO]
        saida["auditoria_de_preco"] = {
            "titulo": "Diferença para o preço de referência", "tipo": "barras",
            "rotulos": [c["categoria"] if c["categoria"] is not None else "Sem categoria" for c in topo],
            "series": [_serie("Diferença", "BRL", (c["diferenca"] for c in topo)),
                       _serie("Produtos abaixo", "itens", (c["itens"] for c in topo))],
        }
    return saida
