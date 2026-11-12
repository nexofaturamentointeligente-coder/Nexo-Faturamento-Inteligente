"""Relatórios fictícios no leiaute real de um ERP de varejo ("Giro de Vendas"), para os testes.

A FORMA é a que a auditoria de cobertura mediu nos arquivos reais (Docs/AUDITORIA_COBERTURA_MOTOR_ETL.md):
a planilha é a imagem de um relatório paginado, com título, período, linhas de filtro, cabeçalho em uma ou
duas linhas repetido a cada página, linha em branco entre os dados, rodapés e avisos, e as células
espalhadas por 19 colunas unidas por mesclas. Em 7 dos 8 relatórios o título de uma coluna não fica na
mesma coluna do dado.

Os DADOS são inventados: produtos, clientes, fornecedores, loja, documentos e valores não vêm de nenhum
arquivo real. Os números são coerentes entre si, para os testes poderem conferir somas.

Cada relatório é uma lista de linhas; cada linha é um dicionário {índice da coluna (base zero): valor}.
Sem import do app nem de config.
"""

import csv
from datetime import datetime
from pathlib import Path

PERIODO = "Período de  : 09/11/23 á 09/11/26 - Andamento: Todas vendas"
_TODOS = "           -Todos                              até             -Todos                             "
FILTROS = [rotulo + _TODOS for rotulo in ("Produtos ....: ", "Clientes ....: ", "Vendedores ..: ", "Gr.Clientes .: ",
                                          "Gr.Produtos .: ", "Sub-Gr.Prods : ", "Fornecedores : ")]
FILTROS.append("Filiais .....:           9-LOJA FICTICIA               até            9-LOJA FICTICIA              ")
RODAPE_DO_SOFTWARE = ("Software desenvolvido por LabSofti Informática - www.LabSofti.com.br - "
                      "email: contato@example.invalid")
MOVIMENTACAO = 'Movimentação produto: "PRODUTO GENERICO"   -   Qtde Vend.: 0,0   -   Total Liq. Vend.: 0,00'
FRETES = "Valor total dos fretes ...:"
ATENCAO = 'Atenção .: Total das vendas não esta considerando "produto genérico".'
MARCADOR_DE_MODELO = "L_FiltroProduto" + "X" * 100
DINHEIRO = "#,##0.00;(#,##0.00)"
QUANTIDADE = "0.0"
DATA = "dd/mm/yy"
EMISSAO = datetime(2026, 11, 9, 17, 3, 38)

# Um contêiner OLE2 só com a assinatura (não é uma planilha) e o PDF que a tela do ERP imprime: recusados.
XLS_MINIMO = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 504
PDF_MINIMO = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n"

# (código, nome, quantidade, total de venda, total de custo, markup %, lucro, estoque atual)
PRODUTOS = [
    ("0700019", "MASSA DE REPARO FICTICIA 1KG", 12.0, 480.0, 300.0, 60.0, 180.0, 3.0),
    ("0600027", "ADAPTADOR FICTICIO 25MM X 3/4", 40.0, 220.0, 120.0, 83.33, 100.0, -4.0),
    ("0300035", "ADESIVO FICTICIO P/ LAMINAÇÃO 880G", 332.0, 14408.63, 8993.42, 60.21, 5415.21, 29.0),
    ("0300043", "SOLVENTE FICTICIO 0,9L", 16.0, 270.4, 179.96, 50.25, 90.44, 41.0),
    ("0100051", "ESMALTE FICTICIO BRANCO 3,6L", 90.0, 8100.0, 5400.0, 50.0, 2700.0, 12.5),
    ("0500069", "LIXA FICTICIA GRAO 80", 165.0, 2710.22, 1704.45, 59.0, 1005.77, -2.0),
]
SEM_CUSTO = ("0600077", "VERNIZ FICTICIO 3,0L", 8.0, 703.2, 0.0)
# Item genérico vendido por valor, com custo de cadastro absurdo (custo muito acima da venda).
GENERICO = ("0200085", "DIVERSOS FICTICIOS", 1400.0, 7000.0, 595000.0, -98.82, -588000.0, 0.0)


def _filtros(linhas):
    linhas.extend({0: texto} for texto in FILTROS)


def relatorio_por_produto(*, paginas=2, generico=False):
    """Resumido por produto: cabeçalho em duas linhas repetido a cada página e linha em branco entre os
    dados. É o único dos oito em que o título fica na mesma coluna do dado."""
    produtos = PRODUTOS + ([GENERICO] if generico else [])
    linhas, mesclas = [{}], []
    linhas.append({5: "Giro de Vendas por Produto", 14: "Página ...:", 15: EMISSAO, 16: "1 de 1"})
    linhas.append({5: PERIODO})
    _filtros(linhas)
    linhas.append({0: MARCADOR_DE_MODELO})

    def cabecalho():
        mesclas.append((len(linhas), 1, len(linhas), 3))
        linhas.append({0: "Codigo", 1: "Nome do produto", 9: "Qtde", 10: "Total", 12: "Total", 13: "Marg. de",
                       16: "Lucro", 17: "Estoque"})
        linhas.append({9: "Vend", 10: "Venda", 12: "Custo", 13: "Lucro %", 16: "Atual"})

    cabecalho()
    por_pagina = max(1, len(produtos) // paginas)
    for indice, produto in enumerate(produtos):
        if indice and indice % por_pagina == 0:
            linhas.append({2: RODAPE_DO_SOFTWARE})
            linhas.append({})
            cabecalho()
        linha = len(linhas)
        mesclas.extend([(linha, 1, linha, 7), (linha, 10, linha, 11), (linha, 12, linha, 13)])
        linhas.append({0: produto[0], 1: produto[1], 9: produto[2], 10: produto[3], 12: produto[4], 14: produto[5],
                       15: "%", 16: produto[6], 17: produto[7]})
        linhas.append({})
    # Produto sem custo: a célula do custo fica vazia e o "%" vai para a coluna da margem.
    linha = len(linhas)
    mesclas.extend([(linha, 1, linha, 7), (linha, 10, linha, 11)])
    linhas.append({0: SEM_CUSTO[0], 1: SEM_CUSTO[1], 9: SEM_CUSTO[2], 10: SEM_CUSTO[3], 14: "%", 16: 0.0, 17: 0.0})
    linhas.append({})
    quantidade = sum(p[2] for p in produtos) + SEM_CUSTO[2]
    venda = round(sum(p[3] for p in produtos) + SEM_CUSTO[3], 2)
    custo = round(sum(p[4] for p in produtos), 2)
    lucro = round(sum(p[6] for p in produtos), 2)
    linhas.append({0: MOVIMENTACAO})
    # A quantidade do total fica na coluna 8 (nas linhas de dado, na 9), e "Descontos:" na coluna do nome.
    linhas.append({0: "Totais:", 1: "Descontos:", 3: 512.34, 8: quantidade, 10: venda, 12: custo, 14: "45,00%", 16: lucro})
    linhas.append({})
    linhas.append({0: FRETES, 4: 8.5})
    linhas.append({})
    linhas.append({0: ATENCAO})
    linhas.append({})
    linhas.append({2: RODAPE_DO_SOFTWARE})
    formatos = {3: DINHEIRO, 4: DINHEIRO, 8: DINHEIRO, 9: QUANTIDADE, 10: DINHEIRO, 12: DINHEIRO,
                14: "#,##0.00;-#,##0.00", 16: DINHEIRO, 17: DINHEIRO}
    verdade = {"produtos": len(produtos) + 1, "quantidade": quantidade, "venda": venda, "custo": custo,
               "sem_custo": 1, "estoque_negativo": sum(1 for p in produtos if p[7] < 0)}
    return linhas, mesclas, formatos, verdade


# (vendedor, pedido e nota na mesma célula, produto, unitário truncado, quantidade, total com até 4 casas)
ITENS = [
    (38.0, "P9-00001  NFC9-00001", "LIXA FICTICIA GRAO 80", 3.8, 1.0, 3.8081),
    (38.0, "P9-00001  NFC9-00001", "ADESIVO FICTICIO P/ LAMINAÇÃO 880G", 19.43, 1.0, 19.431),
    (38.0, "P9-00002  NFC9-00002", "SOLVENTE FICTICIO 0,9L", 5.76, 1.0, 5.7609),
    (12.0, "P9-00003 ", "MASSA DE REPARO FICTICIA 1KG", 26.99, 2.0, 53.996),
    (12.0, "P9-00004  NFC9-00003", "ESMALTE FICTICIO BRANCO 3,6L", 69.9, 1.0, 69.9),
    (7.0, "P9-00005  NFC9-00004", "LIXA FICTICIA GRAO 80", 3.46, 2.0, 6.93),
]
CLIENTES = ("CONSUMIDOR", "CLIENTE FICTICIO A", "CLIENTE FICTICIO B")


def relatorio_detalhado(*, clientes=3):
    """Venda item a item em blocos por cliente. Título e dado em colunas diferentes: "Produto" na coluna 8
    e o nome na 4 (mescla de 4 a 12); "Valor Total" na 16 e o total na 17; a quantidade na 16."""
    linhas, mesclas = [{}], []
    linhas.append({2: "Giro de vendas por cliente detalhada por produto", 14: "Página ...:", 15: EMISSAO, 16: "1 de 1"})
    linhas.append({})
    linhas.append({4: PERIODO})
    _filtros(linhas)
    linhas.extend([{}, {}])
    total_geral, quantidade_geral, itens = 0.0, 0.0, 0
    for cliente in range(clientes):
        linhas.append({0: "*** Cliente :", 2: CLIENTES[cliente % len(CLIENTES)]})
        linhas.append({0: "Data", 1: "Ven.", 3: "Nr. Doc.", 8: "Produto", 13: "Valor", 15: "Qtde", 16: "Valor"})
        linhas.append({13: "Unit.", 16: "Total"})
        soma, quantidade = 0.0, 0.0
        for indice, item in enumerate(ITENS):
            linha = len(linhas)
            mesclas.extend([(linha, 2, linha, 3), (linha, 4, linha, 12)])
            # Sempre dentro do período do cabeçalho (09/11/23 a 09/11/26): o dia vai de 1 a 9.
            dia = datetime(2024 + (cliente + indice) % 3, 1 + (indice * 2 + cliente) % 12, 1 + (indice * 3 + cliente) % 9)
            linhas.append({0: dia, 1: item[0], 2: item[1], 4: item[2], 14: item[3], 16: item[4], 17: item[5]})
            soma += item[5]
            quantidade += item[4]
            itens += 1
        linhas.append({0: "Descontos >>> ", 3: 1.5 * cliente, 9: " Valor Total para este cliente  >>>",
                       15: quantidade, 17: round(soma, 4)})
        linhas.append({})
        if cliente == 0:
            linhas.append({2: RODAPE_DO_SOFTWARE})
            linhas.append({})
        total_geral += soma
        quantidade_geral += quantidade
    linhas.append({0: MOVIMENTACAO})
    linhas.append({})
    linhas.append({1: "Total Geral de Todas as Vendas do período", 14: round(total_geral, 4)})
    linhas.append({})
    linhas.append({0: FRETES, 4: 8.5})
    linhas.append({0: ATENCAO})
    linhas.append({})
    linhas.append({2: RODAPE_DO_SOFTWARE})
    formatos = {0: DATA, 3: DINHEIRO, 4: DINHEIRO, 14: DINHEIRO, 15: DINHEIRO, 16: QUANTIDADE, 17: DINHEIRO}
    verdade = {"itens": itens, "venda": round(total_geral, 4), "quantidade": quantidade_geral,
               "produtos": len({item[2] for item in ITENS})}
    return linhas, mesclas, formatos, verdade


# (código com a máscara do ERP, descrição, quantidade, "unitário de compra", "unitário vendido", diferença)
ABAIXO_DO_CUSTO = [
    ("000007000.19", "MASSA DE REPARO FICTICIA 1KG", 2.0, 31.7, 20.02, 11.68),
    ("000001000.51", "ESMALTE FICTICIO BRANCO 3,6L", 2.0, 63.0282, 60.0, 3.0282),
    ("000003000.43", "SOLVENTE FICTICIO 0,9L", 4.0, 47.64, 28.9, 18.74),
]
# Colunas do dado e do título no relatório de vendas abaixo do custo (dado: título).
ALINHAMENTO_ABAIXO_DO_CUSTO = {2: 5, 12: 11, 15: 13, 17: 16}


def relatorio_abaixo_do_custo():
    """Vendas abaixo do custo: dois títulos "Valor Unitário" iguais na primeira linha do cabeçalho; só a
    segunda linha ("Compra" e "Vendido") os distingue."""
    linhas, mesclas = [{}], []
    linhas.append({1: "Giro de vendas por Produto(s) Vendido(s) Abaixo do Pr. Custo"})
    linhas.append({4: PERIODO, 14: EMISSAO})
    linhas.append({14: "Página ..:", 15: 1.0, 17: 1.0})
    _filtros(linhas)
    linhas.append({})
    mesclas.extend([(len(linhas), 11, len(linhas), 12), (len(linhas), 13, len(linhas), 14)])
    linhas.append({0: "Cód.", 5: "Descrição", 10: "Qtde", 11: "Valor Unitário", 13: "Valor Unitário", 16: "Diferença"})
    linhas.append({0: "Produto", 5: "Produto", 10: "Vendida", 12: "Compra", 14: "Vendido"})
    linhas.append({})
    for item in ABAIXO_DO_CUSTO:
        linha = len(linhas)
        mesclas.extend([(linha, 0, linha, 1), (linha, 2, linha, 9), (linha, 10, linha, 11), (linha, 12, linha, 13),
                        (linha, 15, linha, 16)])
        linhas.append({0: item[0], 2: item[1], 10: item[2], 12: item[3], 15: item[4], 17: item[5]})
        linhas.append({})
    linhas.append({0: RODAPE_DO_SOFTWARE})
    linhas.append({0: "Totais Gerais >>>", 10: sum(i[2] for i in ABAIXO_DO_CUSTO), 12: sum(i[3] for i in ABAIXO_DO_CUSTO),
                   15: sum(i[4] for i in ABAIXO_DO_CUSTO), 17: sum(i[5] for i in ABAIXO_DO_CUSTO)})
    linhas.append({})
    linhas.append({0: MOVIMENTACAO})
    linhas.append({})
    linhas.append({0: FRETES, 4: 0.0})
    linhas.append({0: ATENCAO})
    formatos = {4: DINHEIRO, 10: QUANTIDADE, 12: DINHEIRO, 15: DINHEIRO, 17: DINHEIRO}
    verdade = {"itens": len(ABAIXO_DO_CUSTO),
               "quantidade_x_compra": round(sum(i[2] * i[3] for i in ABAIXO_DO_CUSTO), 2),
               "quantidade_x_vendido": round(sum(i[2] * i[4] for i in ABAIXO_DO_CUSTO), 2)}
    return linhas, mesclas, formatos, verdade


def _resumido(titulo, coluna_titulo, cabecalho, mesclas_do_cabecalho, dados, mesclas_do_dado, total, formatos, *,
              marcador=None, periodo_na_coluna=4):
    linhas, mesclas = [{}], []
    linhas.append({coluna_titulo: titulo, 14: "Página ...:", 15: EMISSAO, 16: "1 de 1"})
    linhas.append({})
    linhas.append({periodo_na_coluna: PERIODO})
    if marcador:
        linhas.append({7: marcador})
    _filtros(linhas)
    linhas.extend([{}, {}])
    linha = len(linhas)
    mesclas.extend((linha, inicio, linha, fim) for inicio, fim in mesclas_do_cabecalho)
    linhas.append(cabecalho)
    linhas.append({})
    for dado in dados:
        linha = len(linhas)
        mesclas.extend((linha, inicio, linha, fim) for inicio, fim in mesclas_do_dado)
        linhas.append(dado)
        linhas.append({})
    linhas.append({0: MOVIMENTACAO})
    linhas.append({})
    linhas.append(total)
    linhas.append({})
    linhas.append({0: FRETES, 4: 8.5})
    linhas.append({0: ATENCAO})
    linhas.append({2: RODAPE_DO_SOFTWARE})
    return linhas, mesclas, formatos, {"itens": len(dados)}


def relatorio_por_grupo_de_produto():
    return _resumido("GIRO DE VENDAS POR GRUPO DE PRODUTOS", 1,
                     {2: "Nome do grupo ", 10: "Qtde venda", 13: "Valor venda", 16: "Lucro"},
                     [(2, 3), (10, 11), (13, 14)],
                     [{0: "GRUPO FICTICIO A", 10: 40.0, 13: 1460.5, 15: 570.25},
                      {0: "GRUPO FICTICIO B", 10: 332.0, 13: 14408.6342, 15: 5415.2142},
                      {0: "GRUPO FICTICIO C", 10: 1400.0, 13: 7000.0, 15: -588000.0}],
                     [(0, 8), (10, 11), (13, 14), (15, 16)],
                     {0: "TOTAL GERAL DAS VENDAS", 10: 1772.0, 13: 22869.1342, 15: -582014.5358},
                     {4: DINHEIRO, 10: QUANTIDADE, 13: DINHEIRO, 15: DINHEIRO}, marcador="Label60", periodo_na_coluna=3)


def relatorio_por_fornecedor():
    return _resumido("Giro de vendas por fornecedor", 4, {3: "Fornecedor", 13: "Valor Total"}, [(3, 4), (13, 14)],
                     [{0: "FORNECEDOR FICTICIO A LTDA", 15: 1242.44}, {0: "FORNECEDOR FICTICIO B LTDA", 15: 14290.76},
                      {0: "OUTROS", 15: 7000.0}],
                     [(0, 11), (15, 16)], {1: "Total ...", 13: 22533.2}, {4: DINHEIRO, 13: DINHEIRO, 15: DINHEIRO},
                     marcador="Label78")


def relatorio_por_forma_de_pagamento():
    """Por forma de pagamento: o relatório fecha com três avisos e o total de fretes, sem linha em branco
    entre os dois últimos. A Baia de Segurança lê esse par como um cabeçalho de compras."""
    linhas, mesclas = [{}], []
    linhas.append({2: "Giro de vendas por forma de pagamento", 14: EMISSAO, 16: "Página :", 17: 1.0, 18: 1.0})
    linhas.append({4: PERIODO})
    _filtros(linhas)
    linhas.append({})
    linha = len(linhas)
    mesclas.extend([(linha, 1, linha, 3), (linha, 13, linha, 14)])
    linhas.append({1: "Forma de pagamento", 13: "Valor Venda"})
    linhas.append({})
    formas = [("DINHEIRO", 2114.4), ("CARTAO DE CREDITO 1X", 11986.83), ("PIX", 4732.56)]
    for forma, valor in formas:
        linha = len(linhas)
        mesclas.extend([(linha, 0, linha, 6), (linha, 12, linha, 14)])
        linhas.append({0: forma, 12: valor})
        linhas.append({})
    linhas.append({0: MOVIMENTACAO})
    linhas.append({})
    linhas.append({7: "Total ", 11: round(sum(valor for _forma, valor in formas), 2)})
    linhas.append({0: "Atenção .: Total das vendas é liquido (Abatendo, Descontos e Fretes)"})
    linhas.append({0: "Atenção .: Pode haver pequena diferença referente a dizima períodica"})
    linhas.append({0: ATENCAO})
    linhas.append({})
    linhas.append({7: FRETES, 14: 8.5})
    linhas.append({2: RODAPE_DO_SOFTWARE})
    return linhas, mesclas, {11: DINHEIRO, 12: DINHEIRO, 14: DINHEIRO}, {"itens": len(formas)}


def relatorio_por_grupo_de_cliente():
    return _resumido("GIRO DE VENDAS POR GRUPO DE CLIENTES", 3,
                     {2: "Grupo de cliente", 11: "Valor  vendido", 14: "Lucro valor"}, [(2, 4), (11, 12), (14, 15)],
                     [{0: "GRUPO DE CLIENTE A", 10: 22000.5, 13: -581000.25}, {0: "GRUPO DE CLIENTE B", 10: 868.6342, 13: -1014.2858}],
                     [(10, 12), (13, 15)], {0: "TOTAL GERAL DAS VENDAS", 10: 22869.1342, 13: -582014.5358},
                     {4: DINHEIRO, 10: DINHEIRO, 13: DINHEIRO}, marcador="Label60", periodo_na_coluna=3)


def relatorio_ranking_de_clientes():
    """Ranking de clientes: no arquivo real há dado pessoal nestas colunas; aqui, tudo inventado."""
    linhas, mesclas = [{}], []
    linhas.append({4: "Giro de Vendas Por cliente - Ranking", 15: "Página:", 16: EMISSAO, 17: 1.0, 18: 1.0})
    linhas.append({})
    linhas.append({2: "Período de  : 09/11/26 á 09/11/26 - Andamento: Todas vendasValor Decrescente"})
    _filtros(linhas)
    linhas.append({0: "Entregadores.: " + _TODOS})
    linhas.append({})
    linhas.append({0: "Cod.", 1: "Cliente", 7: "CPF/CNPJ", 9: "Fone 1", 11: "Fone 2", 14: "E-Mail", 16: "Total"})
    linhas.append({16: "Vr Liq"})
    for codigo, nome, documento, telefone, total in (("0001", "CONSUMIDOR", None, "00000000", 716.0),
                                                     ("9999", "CLIENTE FICTICIO A", "000.000.000-00", "00 000000000", 319.6)):
        linha = len(linhas)
        mesclas.extend([(linha, 1, linha, 5), (linha, 8, linha, 9), (linha, 16, linha, 17)])
        registro = {0: codigo, 1: nome, 8: telefone, 16: total}
        if documento:
            registro[6] = documento
            mesclas.append((linha, 6, linha, 7))
        linhas.append(registro)
    linhas.append({0: MOVIMENTACAO})
    linhas.append({3: "Totais:", 4: "Vr Fretes:", 7: 0.0, 8: "Vr Acrésc.:", 10: 0.0, 11: "Vr desctos:", 13: 1.8,
                   14: "Vr Liq Ven:", 16: 1035.6})
    linhas.append({0: "Quantidade de Pedidos:"})
    linhas.append({})
    linhas.append({0: 'Atenção .: Total das vendas não esta considerando "produto genérico código 2".'})
    linhas.append({2: RODAPE_DO_SOFTWARE})
    return linhas, mesclas, {7: DINHEIRO, 10: DINHEIRO, 13: DINHEIRO, 16: DINHEIRO}, {"itens": 2}


# ---------------------------------------------------------------------------------------------------------------
# Relatórios auxiliares do lote (11/2026): a FORMA dos arquivos reais do mesmo ERP que acompanham a Opção 27 (posição
# de estoque, inventário, lista de preços, resumo por documento e posição das compras), medida neles; os DADOS são
# inventados. Os produtos são os de PRODUTOS e ITENS; os documentos, os de relatorio_detalhado(clientes=1).
# ---------------------------------------------------------------------------------------------------------------
EMISSAO_DO_LOTE = datetime(2026, 11, 9, 15, 13, 5)
DIA_DO_ESTOQUE = "09/11/2026"
LOJA_DO_TOPO = "009 - LOJA FICTICIA"
# (grupo, código com a máscara do ERP, fornecedor, nome, estoque atual, mínimo, máximo, preço de custo, NCM)
ESTOQUE = [
    ("GRUPO FICTICIO A", "000007000.19", 212.0, "MASSA DE REPARO FICTICIA 1KG", 3.0, 2.0, 10.0, 25.0, 32149000.0),
    ("GRUPO FICTICIO A", "000003000.35", 128.0, "ADESIVO FICTICIO P/ LAMINAÇÃO 880G", 29.0, 3.0, 12.0, 27.0, 35061090.0),
    ("GRUPO FICTICIO A", "000003000.43", 212.0, "SOLVENTE FICTICIO 0,9L", 41.0, 40.0, 160.0, 11.25, 38140000.0),
    ("GRUPO FICTICIO B", "000001000.51", 34.0, "ESMALTE FICTICIO BRANCO 3,6L", 12.5, 2.0, 4.0, 60.0, 32081010.0),
    ("GRUPO FICTICIO B", "000005000.69", 114.0, "LIXA FICTICIA GRAO 80", -2.0, 0.0, 0.0, 1.5, 68052000.0),
    ("GRUPO FICTICIO C", "000008000.10", 11.0, "PINCEL FICTICIO 2 POL", 10.0, 0.0, 0.0, 4.0, 96034010.0),
    ("GRUPO FICTICIO C", "000008000.28", 11.0, "ROLO FICTICIO 23CM", 0.0, 0.0, 0.0, 9.0, 96034090.0),
]
# (código, nome, preço do grupo de preço 1, preço do grupo 2): o ERP lista o produto uma vez por grupo de preço.
PRECOS = [(item[1], item[3], preco_1, preco_2) for item, (preco_1, preco_2) in zip(
    ESTOQUE, [(29.9, 31.9), (45.0, 47.0), (7.5, 7.9), (89.9, 94.9), (3.9, 4.2), (8.9, 9.5), (19.9, 21.0)])]
PRECOS.append(("000008000.36", "FITA FICTICIA CREPE", 5.5, 5.9))  # só no cadastro: sem venda nem estoque
# Os documentos da venda item a item com um cliente (relatorio_detalhado(clientes=1)): (vendedor, número, data,
# cliente, valor líquido, desconto em percentual sobre o bruto). O desconto em reais é valor x p / (100 - p): 5,81 no
# P9-00001 e 69,90 no P9-00004. O cliente que começa por "TOTAL" é uma linha de documento, e não um total.
DOCUMENTOS = [
    ("VENDEDOR FICTICIO A", "P9-00001", datetime(2024, 1, 1), "CONSUMIDOR", 23.24, 20.0),
    ("VENDEDOR FICTICIO A", "P9-00002", datetime(2025, 5, 7), "CLIENTE FICTICIO A", 5.76, 0.0),
    ("VENDEDOR FICTICIO A", "P9-00003", datetime(2024, 7, 1), "TOTAL CLIENTE FICTICIO", 54.0, 0.0),
    ("VENDEDOR FICTICIO B", "P9-00004", datetime(2025, 9, 4), "CLIENTE FICTICIO B", 69.9, 50.0),
    ("VENDEDOR FICTICIO B", "P9-00005", datetime(2026, 11, 7), "CONSUMIDOR", 6.93, 0.0),
]
DESCONTO_DOS_DOCUMENTOS = 75.71
# (data, número da NF, série, fornecedor, total dos produtos, total da nota)
COMPRAS = [
    (datetime(2024, 1, 5), 46.279, 1.0, "FORNECEDOR FICTICIO A LTDA", 1476.3, 1518.16),
    (datetime(2024, 2, 8), 524.038, 1.0, "FORNECEDOR FICTICIO B LTDA", 7102.1, 8576.13),
    (datetime(2025, 3, 1), 1562415.0, 1.0, "FORNECEDOR FICTICIO A LTDA", 1958.54, 1958.54),
    (datetime(2026, 10, 16), None, 1.0, "FORNECEDOR FICTICIO C LTDA", 0.0, 0.0),
]


def relatorio_posicao_de_estoque(*, estoque=None, loja=LOJA_DO_TOPO, emissao=EMISSAO_DO_LOTE, emitido_em=None,
                                 empresa=None, fornecedor_so_no_primeiro=False, loja_em_duas_celulas=False):
    """Posição de estoque atual de produtos: blocos por grupo ("Grupo :" e o nome) com o cabeçalho em duas linhas
    repetido, o nome do produto na coluna 3 (título na 5), o preço de custo na 12 (título na 11) e o valor na 13
    (título na 12), o total de cada grupo e o total geral. `loja=None` tira a linha da filial do topo; `emissao=None`, a
    data do canto; `emitido_em` põe a data num rótulo ("Emitido em: ..."); `empresa`, a linha da empresa acima da
    filial; `fornecedor_so_no_primeiro` deixa o código do fornecedor em branco em todo produto menos o primeiro."""
    estoque = ESTOQUE if estoque is None else estoque
    linhas, mesclas = [{10: emissao} if emissao else {}, {4: "Posição de estoque atual de produtos"}], []
    if emitido_em:
        linhas.append({0: f"Emitido em: {emitido_em}"})
    linhas.append({0: "Selecionados por : Todos produtos"})
    if empresa:
        linhas.append({0: f"Empresa ........: {empresa}"})
    if loja:
        linhas.append({0: "Filial .........:", 2: loja} if loja_em_duas_celulas else {0: f"Filial .........: {loja}"})
    total_estoque, total_valor = 0.0, 0.0
    for grupo in dict.fromkeys(item[0] for item in estoque):
        linhas.append({0: "Grupo :", 2: grupo})
        linhas.append({0: "Código", 1: "Forn.", 5: "Nome produto", 8: "Est.", 9: "Est.", 10: "Est.", 11: "Preço",
                       12: "Valor"})
        linhas.append({8: "Atual", 9: "Mín.", 10: "Máx.", 11: "Custo", 12: "Estoque"})
        soma_estoque, soma_valor = 0.0, 0.0
        for item in (item for item in estoque if item[0] == grupo):
            valor = round(item[4] * item[7], 4)
            linha = len(linhas)
            mesclas.append((linha, 3, linha, 7))
            registro = {0: item[1], 2: item[2], 3: item[3], 8: item[4], 9: item[5], 10: item[6], 12: item[7], 13: valor}
            if fornecedor_so_no_primeiro and item is not estoque[0]:
                del registro[2]
            linhas.append(registro)
            soma_estoque += item[4]
            soma_valor += valor
        linhas.append({1: "Total deste grupo :", 7: soma_estoque, 10: round(soma_valor, 4)})
        total_estoque += soma_estoque
        total_valor += soma_valor
    linhas.append({0: "Nr de itens listados:", 5: "Total geral:", 6: total_estoque, 10: round(total_valor, 4)})
    formatos = {6: QUANTIDADE, 7: QUANTIDADE, 8: QUANTIDADE, 9: QUANTIDADE, 10: DINHEIRO, 12: DINHEIRO, 13: DINHEIRO}
    return linhas, mesclas, formatos, {"produtos": len(estoque), "estoque": total_estoque}


def relatorio_inventario(*, estoque=None, dia=DIA_DO_ESTOQUE, emissao=EMISSAO_DO_LOTE, por_pagina=4, saldos=None):
    """Inventário de estoque: o cabeçalho da página e o das colunas repetidos a cada página, o título da descrição com
    as letras separadas por espaço, o código na coluna 1 (título na 0), o nome na 3 (título na 4) e o custo na 9
    (título na 7); o NCM na mesma coluna do título. `saldos` ({nome: saldo}) troca o saldo de algum produto."""
    estoque = ESTOQUE if estoque is None else estoque
    linhas, mesclas, total = [], [], 0.0
    for indice, item in enumerate(estoque):
        if indice % por_pagina == 0:
            linhas.append({5: "Inventário de Estoque", 8: emissao, 9: "Página:", 11: float(indice // por_pagina + 1)})
            linhas.append({6: f"Período : {dia}"})
            linhas.append({6: "Label60"})
            linhas.append({0: "Cód.", 2: "NCM", 4: "D e s c r i ç ã o   d o   p r o d u t o", 7: "Custo",
                           10: "Saldo de", 11: "Valor do"})
            linhas.append({7: "Unitário", 10: "Estoque", 11: "Estoque"})
        saldo = (saldos or {}).get(item[3], item[4])
        valor = round(saldo * item[7], 4)
        linha = len(linhas)
        mesclas.append((linha, 3, linha, 8))
        linhas.append({1: item[1], 2: item[8], 3: item[3], 9: item[7], 10: saldo, 11: valor})
        total += valor
    linhas.append({0: 'Total geral do "Valor do Estoque"', 10: round(total, 4)})
    linhas.append({0: "Nr de Itens listados:"})
    return linhas, mesclas, {9: DINHEIRO, 10: QUANTIDADE, 11: DINHEIRO}, {"produtos": len(estoque)}


def relatorio_lista_de_precos(*, precos=None, emissao=EMISSAO_DO_LOTE, parametros_juntos=False):
    """Lista de preços: os parâmetros do topo (o "Grupo Preço inicial" sobre o "Produto inicial" parecia um cabeçalho
    com o produto e o preço), blocos por grupo com o cabeçalho repetido, uma linha por grupo de preço (o mesmo produto
    em duas linhas), o código na coluna 1 (título na 0) e o preço na 6 (título na 5)."""
    precos = PRECOS if precos is None else precos
    grupos = {item[3]: item[0] for item in ESTOQUE}
    linhas = [{3: "Lista de preços"}, {3: emissao},
              {0: "Endereço inicial ....:", 4: "Endereço final ......:"},
              {0: "Pesquisa 1 inicial ..:", 4: "Pesquisa 1 final ....:"},
              {0: "Fornecedor inicial ..: 1", 4: "Fornecedor final ....: 225"},
              {0: "Grupo inicial .......: 1", 4: "Grupo final .........: 277"},
              {0: "Grupo Preço inicial .: 1", 4: "Grupo Preço final ...: 4"},
              {0: "Produto inicial .....: 000001000.51", 4: "Produto final .......: 000008000.36"},
              {0: f"Filial Corrente .....: {LOJA_DO_TOPO}"}]
    if parametros_juntos:
        # O produto e o grupo de preço na mesma linha de parâmetros: ela casa o produto e o preço sem reserva.
        linhas[6:8] = [{0: "Produto inicial .....: 000001000.51", 4: "Grupo Preço inicial .: 1"}]
    for grupo in dict.fromkeys(grupos.get(nome, "GRUPO FICTICIO D") for _codigo, nome, _p1, _p2 in precos):
        linhas.append({0: "Grupo :", 2: grupo})
        linhas.append({0: "Cód. Prod.", 2: "Nome", 5: "Preço"})
        for codigo, nome, preco_1, preco_2 in precos:
            if grupos.get(nome, "GRUPO FICTICIO D") == grupo:
                linhas.extend({1: codigo, 2: nome, 6: preco} for preco in (preco_1, preco_2))
    linhas.append({0: "Nr de Itens listados:"})
    return linhas, [], {6: DINHEIRO}, {"produtos": len(precos), "linhas": 2 * len(precos)}


def relatorio_por_documento(*, documentos=None):
    """Giro de vendas por cliente / documento: blocos por vendedor, o cabeçalho em duas linhas ("Total" sobre "Desc.
    %", "% Méd" sobre "Comi."), o número do documento na coluna 0 (título na 2), o cliente na 5 (título na 7), o
    desconto em percentual sobre o valor bruto e o valor do documento já líquido, os subtotais de cada vendedor e o
    total geral com o "Vr. desctos"."""
    documentos = DOCUMENTOS if documentos is None else documentos
    linhas, mesclas = [{}], []
    linhas.append({3: "Giro de vendas por Cliente / documento", 12: "Página:", 13: EMISSAO, 14: 1.0, 15: 1.0})
    linhas.append({3: PERIODO})
    _filtros(linhas)
    linhas.append({0: "Entregadores.: " + _TODOS})
    total, total_desconto = 0.0, 0.0
    for vendedor in dict.fromkeys(documento[0] for documento in documentos):
        linhas.append({0: "Vendedor :", 2: vendedor})
        linhas.append({2: "Nr. Doc.", 4: "Data", 7: "Cliente", 9: "Valor", 11: "Total", 12: "Acres.", 13: "Frete",
                       14: "% Méd"})
        linhas.append({11: "Desc. %", 14: "Comi."})
        soma, desconto = 0.0, 0.0
        for _vendedor, numero, dia, cliente, valor, percentual in (d for d in documentos if d[0] == vendedor):
            linha = len(linhas)
            mesclas.extend([(linha, 0, linha, 3), (linha, 5, linha, 8)])
            linhas.append({0: numero, 4: dia, 5: cliente, 9: valor, 11: percentual, 12: 0.0, 13: 0.0, 14: 1.0})
            soma += valor
            desconto += valor * percentual / (100 - percentual)
        linhas.append({1: "Sub-tot Documentos = R$", 3: round(soma, 2), 6: "Sub-tot descontos = R$",
                       8: round(desconto, 4), 9: "Sub-tot comissao plano pgto = R$", 14: round(soma / 100, 4)})
        linhas.append({12: "Frete = R$", 14: 0.0})
        total += soma
        total_desconto += desconto
    linhas.append({0: MOVIMENTACAO})
    linhas.append({0: "Total:", 1: "Vr vendas:", 2: round(total, 2), 3: "Vr fretes:", 5: 0.0, 6: "Vr Acrésc.:", 7: 0.0,
                   8: "Vr. desctos:", 9: round(total_desconto, 4), 10: "Vr. Comissões:", 13: round(total / 100, 4)})
    linhas.append({0: "Quantidade de Pedidos:"})
    linhas.append({0: ATENCAO})
    linhas.append({2: RODAPE_DO_SOFTWARE})
    formatos = {2: DINHEIRO, 3: DINHEIRO, 4: DATA, 8: DINHEIRO, 9: DINHEIRO, 11: DINHEIRO, 12: DINHEIRO, 13: DINHEIRO,
                14: DINHEIRO}
    return linhas, mesclas, formatos, {"documentos": len(documentos), "desconto": round(total_desconto, 2)}


def relatorio_posicao_de_compras(*, compras=None, por_pagina=2):
    """Posição das compras de produtos: o resumo por nota do ERP (sem o produto), com o cabeçalho da página e o das
    colunas, em duas linhas, repetidos a cada página."""
    compras = COMPRAS if compras is None else compras
    linhas, mesclas = [], []
    for indice, (dia, nota, serie, fornecedor, produtos, total) in enumerate(compras):
        if indice % por_pagina == 0:
            linhas.append({7: EMISSAO_DO_LOTE})
            linhas.append({3: "Posição das compras de produtos"})
            linhas.append({4: "Periodo : 01/01/23 á 09/11/2026"})
            linhas.append({5: "Label60"})
            linhas.append({0: "Data da", 1: "Número", 2: "Série", 3: "Fornecedor", 6: "Ajuste de", 7: "Vr Total",
                           8: "Despesas", 9: "Valor"})
            linhas.append({0: "Compra", 1: "NF", 6: "Pagto.", 7: "Produtos", 9: "Nf"})
        linha = len(linhas)
        mesclas.append((linha, 3, linha, 5))
        registro = {0: dia, 2: serie, 3: fornecedor, 6: "X", 7: produtos, 8: 0.0, 9: total}
        if nota is not None:
            registro[1] = nota
        linhas.append(registro)
    linhas.append({0: "Totais geral", 7: round(sum(c[4] for c in compras), 2), 9: round(sum(c[5] for c in compras), 2)})
    return linhas, mesclas, {0: DATA, 7: DINHEIRO, 8: DINHEIRO, 9: DINHEIRO}, {"notas": len(compras)}


AUXILIARES = {
    "posicao_de_estoque": relatorio_posicao_de_estoque,
    "inventario": relatorio_inventario,
    "lista_de_precos": relatorio_lista_de_precos,
    "por_documento": relatorio_por_documento,
    "posicao_de_compras": relatorio_posicao_de_compras,
}


RELATORIOS = {
    "por_produto": relatorio_por_produto,
    "detalhado": relatorio_detalhado,
    "abaixo_do_custo": relatorio_abaixo_do_custo,
    "por_grupo_de_produto": relatorio_por_grupo_de_produto,
    "por_fornecedor": relatorio_por_fornecedor,
    "por_forma_de_pagamento": relatorio_por_forma_de_pagamento,
    "por_grupo_de_cliente": relatorio_por_grupo_de_cliente,
    "ranking_de_clientes": relatorio_ranking_de_clientes,
}


def gravar_xlsx(linhas, mesclas, formatos, destino, *, branco_com_estilo_na_coluna=None):
    """Grava o relatório em .xlsx com as células nas mesmas colunas do real, as mesclas e os formatos.
    `branco_com_estilo_na_coluna` põe células vazias formatadas nessa coluna, como a conversão do .xls
    deixa na última coluna do relatório detalhado (elas contam na área conferida pelo motor)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    livro = Workbook()
    folha = livro.active
    folha.title = "Report"
    for numero, linha in enumerate(linhas, 1):
        for coluna, valor in linha.items():
            if valor is None:
                continue
            celula = folha.cell(row=numero, column=coluna + 1, value=valor)
            if isinstance(valor, datetime):
                celula.number_format = formatos.get(coluna, DATA) if valor.hour == 0 else "dd/mm/yyyy hh:mm"
            elif isinstance(valor, (int, float)) and coluna in formatos:
                celula.number_format = formatos[coluna]
    for linha_1, coluna_1, linha_2, coluna_2 in mesclas:
        folha.merge_cells(start_row=linha_1 + 1, start_column=coluna_1 + 1, end_row=linha_2 + 1, end_column=coluna_2 + 1)
    if branco_com_estilo_na_coluna is not None:
        for numero in (1, 2, len(linhas)):
            folha.cell(row=numero, column=branco_com_estilo_na_coluna + 1).font = Font(bold=True)
    Path(destino).parent.mkdir(parents=True, exist_ok=True)
    livro.save(destino)
    livro.close()
    return str(destino)


def gravar_xls(linhas, mesclas, formatos, destino, *, outros=()):
    """Grava o relatório em .xls do Excel 97-2003 (BIFF8), o formato em que o ERP exporta: as mesmas células,
    com o tipo e o formato numérico de cada uma, e as mesclas. `outros`: fluxos a mais no contêiner, como
    (nome, bytes): o projeto de macros ou o objeto embutido de uma planilha de verdade."""
    from tests import xls_support
    pasta = xls_support.Pasta()
    folha = pasta.aba("Report")
    for numero, linha in enumerate(linhas):
        for coluna in sorted(linha):
            valor = linha[coluna]
            if valor is None:
                continue
            formato = None
            if isinstance(valor, datetime):
                formato = formatos.get(coluna, DATA) if valor.hour == 0 else "dd/mm/yyyy hh:mm"
            elif isinstance(valor, (int, float)) and not isinstance(valor, bool):
                formato = formatos.get(coluna)
            folha.por(numero, coluna, valor, formato)
    if mesclas:
        folha.bruto(xls_support.mesclas([(linha_1, linha_2, coluna_1, coluna_2)
                                         for linha_1, coluna_1, linha_2, coluna_2 in mesclas]))
    Path(destino).parent.mkdir(parents=True, exist_ok=True)
    Path(destino).write_bytes(bytes(pasta.arquivo(outros=outros)))
    return str(destino)


def gravar_planilha_xml(linhas, mesclas, destino):
    """Grava o relatório como Planilha XML 2003 do Excel (.xml), com cada mescla na primeira célula."""
    from tests import planilha_xml_support
    return planilha_xml_support.gravar(destino, planilha_xml_support.texto_do_relatorio(linhas, mesclas))


def texto_exibido(valor, formato=None):
    """O valor como a tela do ERP mostra: "1.460.035,77", "(396.747,48)", "1434,0", "09/11/23"."""
    if isinstance(valor, datetime):
        return valor.strftime("%d/%m/%y") if valor.hour == 0 else valor.strftime("%d/%m/%Y %H:%M")
    if isinstance(valor, (int, float)):
        if formato == QUANTIDADE:
            return f"{valor:.1f}".replace(".", ",")
        texto = f"{abs(valor):,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
        if valor < 0:
            return f"({texto})" if formato == DINHEIRO else f"-{texto}"
        return texto
    return str(valor)


_ROTULOS_QUE_NAO_SAO_DADO = ("Totais", "TOTAL", "Total", "Movimenta", "Valor total", "Aten", "Software", "Descontos", "***",
                             "Giro", "GIRO", "Período", "Página")


def gravar_csv(linhas, formatos, destino, *, separador=";", codificacao="cp1252", alinhar=None):
    """Grava o relatório em .csv com os valores como a tela mostra. `alinhar` ({coluna do dado: coluna do
    título}) põe cada dado embaixo do título dele; sem isso, o .csv mantém o desalinhamento da planilha."""
    largura = max((max(linha) for linha in linhas if linha), default=0) + 1
    Path(destino).parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "w", encoding=codificacao, newline="") as saida:
        escritor = csv.writer(saida, delimiter=separador, lineterminator="\r\n")
        for linha in linhas:
            celulas = [""] * largura
            primeiro = next(iter(linha.values()), None) if linha else None
            e_dado = (not (isinstance(primeiro, str) and primeiro.startswith(_ROTULOS_QUE_NAO_SAO_DADO))
                      and any(isinstance(valor, (int, float, datetime)) for valor in linha.values()))
            for coluna, valor in linha.items():
                destino_da_coluna = alinhar.get(coluna, coluna) if (alinhar and e_dado) else coluna
                celulas[destino_da_coluna] = texto_exibido(valor, formatos.get(coluna))
            escritor.writerow(celulas)
    return str(destino)
