"""Planilhas XML 2003 do Excel (SpreadsheetML) fictícias, para os testes do leitor etl_planilha_xml.

Não há amostra real desse formato. Os documentos daqui seguem os elementos e os atributos da especificação
(Workbook, Worksheet, Table, Row, Cell e Data; ss:Index, ss:MergeAcross, ss:MergeDown e ss:Type) e o desenho
do relatório paginado de ERP que a auditoria de cobertura mediu nas planilhas
(Docs/AUDITORIA_COBERTURA_MOTOR_ETL.md, seção 2): título, período, linhas de filtro, cabeçalho em duas
linhas repetido a cada página, células espalhadas por colunas, mesclas, linhas em branco, rodapés e totais.

Os DADOS são inventados: produtos, loja, fabricante do software, documentos e valores não vêm de nenhum
arquivo real. Sem import do app, de config nem do leitor.
"""

from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ESPACO = "urn:schemas-microsoft-com:office:spreadsheet"
DECLARACAO = '<?xml version="1.0"?>'
INSTRUCAO = '<?mso-application progid="Excel.Sheet"?>'
# A abertura que o Excel grava: o espaço de nomes da planilha é o padrão e também o do prefixo ss.
ABRE_LIVRO = (f'<Workbook xmlns="{ESPACO}"\n xmlns:o="urn:schemas-microsoft-com:office:office"\n'
              f' xmlns:x="urn:schemas-microsoft-com:office:excel"\n xmlns:ss="{ESPACO}"\n'
              ' xmlns:html="http://www.w3.org/TR/REC-html40">')
_ATRIBUTOS = {"indice": "ss:Index", "atravessa": "ss:MergeAcross", "desce": "ss:MergeDown", "faixa": "ss:Span",
              "formula": "ss:Formula", "estilo": "ss:StyleID", "vinculo": "ss:HRef"}


def _atributos(opcoes) -> str:
    return "".join(f" {_ATRIBUTOS[nome]}={quoteattr(str(valor))}" for nome, valor in opcoes.items()
                   if valor is not None)


def dado(valor, tipo=None) -> str:
    """O elemento Data de um valor do Python; com `tipo`, o texto vai como está (para os casos de recusa)."""
    if tipo is not None:
        texto = str(valor)
    elif isinstance(valor, bool):
        tipo, texto = "Boolean", "1" if valor else "0"
    elif isinstance(valor, (int, float)):
        tipo, texto = "Number", repr(valor)
    elif isinstance(valor, datetime):
        tipo, texto = "DateTime", valor.isoformat(timespec="milliseconds")
    else:
        # O Excel grava a quebra de linha de uma célula como referência de caractere.
        tipo, texto = "String", escape(str(valor), {"\n": "&#10;", "\r": "&#13;"})
    return f'<Data ss:Type="{tipo}">{texto}</Data>'


def celula(valor=None, *, tipo=None, **opcoes) -> str:
    """Uma Cell. Opções: indice, atravessa, desce, formula, estilo e vinculo (os atributos ss)."""
    if valor is None and tipo is None:
        return f"<Cell{_atributos(opcoes)}/>"
    return f"<Cell{_atributos(opcoes)}>{dado(valor, tipo)}</Cell>"


def linha(*celulas, **opcoes) -> str:
    """Uma Row com as células dadas (texto XML). Opções: indice, faixa e estilo."""
    if not celulas:
        return f"<Row{_atributos(opcoes)}/>"
    return f"<Row{_atributos(opcoes)}>{''.join(celulas)}</Row>"


def aba(*linhas, nome="Report", depois="") -> str:
    """Uma Worksheet com uma Table e as linhas dadas (texto XML); `depois` vai depois da Table."""
    return (f' <Worksheet ss:Name="{nome}">\n  <Table>\n' + "".join(f"   {texto}\n" for texto in linhas)
            + f"  </Table>\n{depois} </Worksheet>")


def livro(*abas, declaracao=DECLARACAO, instrucao=INSTRUCAO, abertura=ABRE_LIVRO, antes="") -> str:
    """O documento inteiro: declaração, instrução do Excel, Workbook e as abas (texto XML)."""
    partes = [declaracao, instrucao, abertura, antes, *abas, "</Workbook>"]
    return "\n".join(parte for parte in partes if parte) + "\n"


def tabela(*linhas, **opcoes) -> str:
    """Atalho: um livro de uma aba só com as linhas dadas."""
    return livro(aba(*linhas), **opcoes)


def matriz_esperada(linhas) -> list:
    """A matriz que o leitor deve devolver para um relatório dado como lista de linhas, cada uma um
    dicionário {coluna (base zero): valor}: da primeira linha até a última com valor, e da primeira coluna
    até a última com valor, com None no que está vazio."""
    com_valor = [{coluna: valor for coluna, valor in registro.items() if valor is not None} for registro in linhas]
    while com_valor and not com_valor[-1]:
        com_valor.pop()
    largura = max((max(registro) for registro in com_valor if registro), default=-1) + 1
    return [[registro.get(coluna) for coluna in range(largura)] for registro in com_valor]


def texto_do_relatorio(linhas, mesclas=(), *, indices="necessarios", linhas_em_branco="omitidas",
                       declaracao=DECLARACAO) -> str:
    """Um relatório (lista de linhas {coluna base zero: valor}; mesclas (linha, coluna, linha, coluna), base
    zero, como em tests/leiautes_erp_support.py) como Planilha XML 2003, com cada mescla na primeira célula
    da área.

    `indices`: "necessarios" põe ss:Index só onde a posição pula, como o Excel grava; "sempre" põe em toda
    Row e em toda Cell, como os geradores de relatório. `linhas_em_branco`: "omitidas" (a linha seguinte leva
    ss:Index) ou "vazias" (uma Row sem célula, só com a altura)."""
    origens = {(inicio, coluna): (ultima_coluna - coluna, fim - inicio)
               for inicio, coluna, fim, ultima_coluna in mesclas}
    partes, anterior = [], 0
    for numero, registro in enumerate(linhas, 1):
        valores = {coluna: valor for coluna, valor in registro.items() if valor is not None}
        colunas = sorted(set(valores) | {coluna for origem, coluna in origens if origem == numero - 1})
        if not colunas and linhas_em_branco == "omitidas":
            continue
        indice = numero if indices == "sempre" or numero != anterior + 1 else None
        anterior = numero
        if not colunas:
            partes.append(f'<Row{_atributos({"indice": indice})} ss:AutoFitHeight="0" ss:Height="6"/>')
            continue
        celulas, proxima = [], 1
        for coluna in colunas:
            atravessa, desce = origens.get((numero - 1, coluna), (0, 0))
            posicao = coluna + 1 if indices == "sempre" or coluna + 1 != proxima else None
            celulas.append(celula(valores.get(coluna), indice=posicao, atravessa=atravessa or None,
                                  desce=desce or None))
            proxima = coluna + atravessa + 2
        partes.append(linha(*celulas, indice=indice))
    return tabela(*partes, declaracao=declaracao)


def gravar(destino, texto, codificacao="utf-8") -> str:
    """Grava o texto (ou os bytes, como vierem) e devolve o caminho."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(texto if isinstance(texto, bytes) else texto.encode(codificacao))
    return str(destino)


# -----------------------------------------------------------------------------------------------------
# O relatório fictício completo, como o Excel o gravaria: "Giro de Vendas por Produto" em duas páginas.
# Tem o que o leitor precisa atravessar sem contar: propriedades, estilos, nomes definidos, larguras de
# coluna, células em branco só com formatação, quebra de página (com um elemento Row que não é o da
# planilha), fórmula com o valor guardado, uma linha formatada bem abaixo do fim e duas abas menores.
# A conferência célula a célula está em tests/test_etl_planilha_xml.py (RELATORIO_ESPERADO).
# -----------------------------------------------------------------------------------------------------
_CABECALHO_DO_RELATORIO = """\
    <Cell ss:MergeDown="1" ss:StyleID="s65"><Data ss:Type="String">Codigo</Data><NamedCell
      ss:Name="Print_Titles"/></Cell>
    <Cell ss:MergeAcross="6" ss:MergeDown="1" ss:StyleID="s65"><Data ss:Type="String">Nome do produto</Data><NamedCell
      ss:Name="Print_Titles"/></Cell>
    <Cell ss:Index="10" ss:StyleID="s65"><Data ss:Type="String">Qtde</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s65"><Data ss:Type="String">Total</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s65"><Data ss:Type="String">Total</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s65"><Data ss:Type="String">Marg. de</Data></Cell>
    <Cell ss:StyleID="s65"><Data ss:Type="String">Lucro</Data></Cell>
    <Cell ss:StyleID="s65"><Data ss:Type="String">Estoque</Data></Cell>
   </Row>
   <Row ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell ss:Index="10" ss:StyleID="s65"><Data ss:Type="String">Vend</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s65"><Data ss:Type="String">Venda</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s65"><Data ss:Type="String">Custo</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s65"><Data ss:Type="String">Lucro %</Data></Cell>
    <Cell ss:Index="18" ss:StyleID="s65"><Data ss:Type="String">Atual</Data></Cell>
   </Row>
"""

RELATORIO_DO_ERP = f"""\
<?xml version="1.0"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:x="urn:schemas-microsoft-com:office:excel"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:html="http://www.w3.org/TR/REC-html40">
 <DocumentProperties xmlns="urn:schemas-microsoft-com:office:office">
  <Author>ERP Ficticio</Author>
  <Created>2026-11-09T20:03:38Z</Created>
  <Version>12.00</Version>
 </DocumentProperties>
 <ExcelWorkbook xmlns="urn:schemas-microsoft-com:office:excel">
  <WindowHeight>9000</WindowHeight>
  <WindowWidth>18000</WindowWidth>
  <ProtectStructure>False</ProtectStructure>
  <ProtectWindows>False</ProtectWindows>
 </ExcelWorkbook>
 <Styles>
  <Style ss:ID="Default" ss:Name="Normal">
   <Alignment ss:Vertical="Bottom"/>
   <Borders/>
   <Font ss:FontName="Arial" ss:Size="8"/>
   <Interior/>
   <NumberFormat/>
   <Protection/>
  </Style>
  <Style ss:ID="s62">
   <NumberFormat ss:Format="dd/mm/yy\\ hh:mm"/>
  </Style>
  <Style ss:ID="s63">
   <NumberFormat ss:Format="#,##0.00;\\(#,##0.00\\)"/>
  </Style>
  <Style ss:ID="s64">
   <NumberFormat ss:Format="0.0"/>
  </Style>
  <Style ss:ID="s65">
   <Font ss:FontName="Arial" ss:Size="8" ss:Bold="1"/>
  </Style>
  <Style ss:ID="s66">
   <Borders>
    <Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1"/>
   </Borders>
  </Style>
 </Styles>
 <Names>
  <NamedRange ss:Name="Print_Titles" ss:RefersTo="=Report!R14:R15"/>
 </Names>
 <Worksheet ss:Name="Report">
  <Names>
   <NamedRange ss:Name="Print_Titles" ss:RefersTo="=Report!R14:R15"/>
  </Names>
  <Table ss:ExpandedColumnCount="19" ss:ExpandedRowCount="60" x:FullColumns="1"
   x:FullRows="1" ss:DefaultRowHeight="11.25">
   <Column ss:AutoFitWidth="0" ss:Width="45"/>
   <Column ss:AutoFitWidth="0" ss:Width="30" ss:Span="6"/>
   <Column ss:Index="9" ss:AutoFitWidth="0" ss:Width="12"/>
   <Row ss:Index="2" ss:AutoFitHeight="0" ss:Height="15">
    <Cell ss:Index="6" ss:MergeAcross="7" ss:StyleID="s65"><Data
      ss:Type="String">Giro de Vendas por Produto</Data></Cell>
    <Cell ss:Index="15"><Data ss:Type="String">Página ...:</Data></Cell>
    <Cell ss:StyleID="s62"><Data ss:Type="DateTime">2026-11-09T17:03:38.000</Data></Cell>
    <Cell><Data ss:Type="String">1 de 2</Data></Cell>
   </Row>
   <Row ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell ss:Index="6" ss:MergeAcross="9"><Data
      ss:Type="String">Período de  : 09/11/23 á 09/11/26 - Andamento: Todas vendas</Data></Cell>
   </Row>
   <Row>
    <Cell ss:MergeAcross="17"><Data ss:Type="String">Produtos ....:  -Todos  até  -Todos</Data></Cell>
   </Row>
   <Row>
    <Cell ss:MergeAcross="17"><Data ss:Type="String">Clientes ....:  -Todos  até  -Todos</Data></Cell>
   </Row>
   <Row>
    <Cell ss:MergeAcross="17"><Data ss:Type="String">Vendedores ..:  -Todos  até  -Todos</Data></Cell>
   </Row>
   <Row>
    <Cell ss:MergeAcross="17"><Data ss:Type="String">Gr.Clientes .:  -Todos  até  -Todos</Data></Cell>
   </Row>
   <Row>
    <Cell ss:MergeAcross="17"><Data ss:Type="String">Gr.Produtos .:  -Todos  até  -Todos</Data></Cell>
   </Row>
   <Row>
    <Cell ss:MergeAcross="17"><Data ss:Type="String">Sub-Gr.Prods :  -Todos  até  -Todos</Data></Cell>
   </Row>
   <Row>
    <Cell ss:MergeAcross="17"><Data ss:Type="String">Fornecedores :  -Todos  até  -Todos</Data></Cell>
   </Row>
   <Row>
    <Cell ss:MergeAcross="17"><Data
      ss:Type="String">Filiais .....:  9-LOJA FICTICIA  até  9-LOJA FICTICIA</Data></Cell>
   </Row>
   <Row>
    <Cell ss:MergeAcross="17"><Data
      ss:Type="String">L_FiltroProdutoXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX</Data></Cell>
   </Row>
   <Row ss:AutoFitHeight="0" ss:Height="6">
    <Cell ss:StyleID="s66"/>
    <Cell ss:Index="19" ss:StyleID="s66"/>
   </Row>
   <Row ss:AutoFitHeight="0" ss:Height="11.25">
{_CABECALHO_DO_RELATORIO}\
   <Row ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell><Data ss:Type="String">0700019</Data></Cell>
    <Cell ss:MergeAcross="6"><Data ss:Type="String">MASSA DE REPARO FICTICIA 1KG</Data></Cell>
    <Cell ss:Index="10" ss:StyleID="s64"><Data ss:Type="Number">12</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">480</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">300</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">60</Data></Cell>
    <Cell><Data ss:Type="String">%</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">180</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">3</Data></Cell>
   </Row>
   <Row ss:Index="18" ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell><Data ss:Type="String">0600027</Data></Cell>
    <Cell ss:MergeAcross="6"><Data ss:Type="String">ADAPTADOR FICTICIO 25MM X 3/4</Data></Cell>
    <Cell ss:Index="10" ss:StyleID="s64"><Data ss:Type="Number">40</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">220</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">120</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">83.33</Data></Cell>
    <Cell><Data ss:Type="String">%</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">100</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">-4</Data></Cell>
   </Row>
   <Row ss:Index="20" ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell><Data ss:Type="String">0300035</Data></Cell>
    <Cell ss:MergeAcross="6"><Data ss:Type="String">ADESIVO FICTICIO P/ LAMINAÇÃO 880G &amp; CIA</Data></Cell>
    <Cell ss:Index="10" ss:StyleID="s64"><Data ss:Type="Number">332</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">14408.63</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">8993.42</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">60.21</Data></Cell>
    <Cell><Data ss:Type="String">%</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">5415.21</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">29</Data></Cell>
   </Row>
   <Row ss:Index="22" ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell><Data ss:Type="String">0300043</Data></Cell>
    <Cell ss:MergeAcross="6"><Data ss:Type="String">SOLVENTE FICTICIO 0,9L</Data></Cell>
    <Cell ss:Index="10" ss:StyleID="s64"><Data ss:Type="Number">16</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">270.4</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">179.96</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">50.25</Data></Cell>
    <Cell><Data ss:Type="String">%</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">90.44</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">41</Data></Cell>
   </Row>
   <Row ss:AutoFitHeight="0" ss:Height="6"/>
   <Row ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell ss:Index="3" ss:MergeAcross="14"><Data
      ss:Type="String">Software desenvolvido por Fabricante Ficticio Informatica - www.example.invalid</Data></Cell>
   </Row>
   <Row ss:Index="26" ss:AutoFitHeight="0" ss:Height="11.25">
{_CABECALHO_DO_RELATORIO}\
   <Row ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell><Data ss:Type="String">0100051</Data></Cell>
    <Cell ss:MergeAcross="6"><Data ss:Type="String">ESMALTE FICTICIO BRANCO 3,6L</Data></Cell>
    <Cell ss:Index="10" ss:StyleID="s64"><Data ss:Type="Number">90</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">8100</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">5400</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">50</Data></Cell>
    <Cell><Data ss:Type="String">%</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">2700</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">12.5</Data></Cell>
   </Row>
   <Row ss:Index="30" ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell><Data ss:Type="String">0500069</Data></Cell>
    <Cell ss:MergeAcross="6"><Data ss:Type="String">LIXA FICTICIA GRAO 80</Data></Cell>
    <Cell ss:Index="10" ss:StyleID="s64"><Data ss:Type="Number">165</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">2710.22</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">1704.45</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">59</Data></Cell>
    <Cell><Data ss:Type="String">%</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">1005.77</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">-2</Data></Cell>
   </Row>
   <Row ss:Index="32" ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell><Data ss:Type="String">0600077</Data></Cell>
    <Cell ss:MergeAcross="6"><Data ss:Type="String">VERNIZ FICTICIO 3,0L</Data></Cell>
    <Cell ss:Index="10" ss:StyleID="s64"><Data ss:Type="Number">8</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">703.2</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"/>
    <Cell><Data ss:Type="String">%</Data></Cell>
    <Cell ss:Index="17" ss:StyleID="s63"><Data ss:Type="Number">0</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">0</Data></Cell>
   </Row>
   <Row ss:Index="34">
    <Cell ss:MergeAcross="17"><Data
     ss:Type="String">Movimentação produto: "PRODUTO GENERICO" - Qtde Vend.: 0,0 - Total Liq.: 0,00</Data></Cell>
   </Row>
   <Row ss:StyleID="s65">
    <Cell><Data ss:Type="String">Totais:</Data></Cell>
    <Cell><Data ss:Type="String">Descontos:</Data></Cell>
    <Cell ss:Index="4" ss:StyleID="s63"><Data ss:Type="Number">512.34</Data></Cell>
    <Cell ss:Index="9" ss:StyleID="s64" ss:Formula="=SUM(R[-19]C[1]:R[-3]C[1])"><Data ss:Type="Number">663</Data></Cell>
    <Cell ss:Index="11" ss:MergeAcross="1" ss:StyleID="s63"
     ss:Formula="=SUM(R[-19]C:R[-3]C)"><Data ss:Type="Number">26892.45</Data></Cell>
    <Cell ss:MergeAcross="1" ss:StyleID="s63"><Data ss:Type="Number">16697.83</Data></Cell>
    <Cell><Data ss:Type="String">45,00%</Data></Cell>
    <Cell ss:Index="17" ss:StyleID="s63"><Data ss:Type="Number">9491.42</Data></Cell>
   </Row>
   <Row ss:Index="37">
    <Cell ss:MergeAcross="3"><Data ss:Type="String">Valor total dos fretes ...:</Data></Cell>
    <Cell ss:StyleID="s63"><Data ss:Type="Number">8.5</Data></Cell>
   </Row>
   <Row ss:Index="39">
    <Cell ss:MergeAcross="17"><Data
      ss:Type="String">Atenção .: Total das vendas não esta considerando &quot;produto genérico&quot;.</Data></Cell>
   </Row>
   <Row ss:AutoFitHeight="0" ss:Height="6">
    <Cell ss:StyleID="s66"/>
    <Cell ss:Index="19" ss:StyleID="s66"/>
   </Row>
   <Row ss:AutoFitHeight="0" ss:Height="11.25">
    <Cell ss:Index="3" ss:MergeAcross="14"><Data
      ss:Type="String">Software desenvolvido por Fabricante Ficticio Informatica - www.example.invalid</Data></Cell>
   </Row>
   <Row ss:Index="60" ss:AutoFitHeight="0" ss:Height="30" ss:StyleID="s66"/>
  </Table>
  <WorksheetOptions xmlns="urn:schemas-microsoft-com:office:excel">
   <PageSetup>
    <Header x:Margin="0.3"/>
    <Footer x:Margin="0.3"/>
   </PageSetup>
   <Selected/>
   <ProtectObjects>False</ProtectObjects>
   <ProtectScenarios>False</ProtectScenarios>
  </WorksheetOptions>
  <PageBreaks xmlns="urn:schemas-microsoft-com:office:excel">
   <RowBreaks>
    <RowBreak>
     <Row>25</Row>
    </RowBreak>
   </RowBreaks>
  </PageBreaks>
 </Worksheet>
 <Worksheet ss:Name="Plan2">
  <Table ss:ExpandedColumnCount="2" ss:ExpandedRowCount="2" x:FullColumns="1" x:FullRows="1">
   <Row>
    <Cell><Data ss:Type="String">Anotação ficticia</Data></Cell>
    <Cell><Data ss:Type="Number">1</Data></Cell>
   </Row>
   <Row>
    <Cell ss:Index="2"><Data ss:Type="Number">2</Data></Cell>
   </Row>
  </Table>
 </Worksheet>
 <Worksheet ss:Name="Plan3">
  <WorksheetOptions xmlns="urn:schemas-microsoft-com:office:excel">
   <ProtectObjects>False</ProtectObjects>
  </WorksheetOptions>
 </Worksheet>
</Workbook>
"""
