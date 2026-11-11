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
