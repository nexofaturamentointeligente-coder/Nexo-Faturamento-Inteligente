"""Leitor da "Planilha XML 2003" do Excel (SpreadsheetML), só com a biblioteca padrão.

É o formato que os geradores de relatório de ERP gravam como "Excel XML" (.xml): raiz Workbook no espaço de
nomes urn:schemas-microsoft-com:office:spreadsheet, com Worksheet, Table, Row, Cell e Data. O módulo só lê
as células, em fluxo (xml.parsers.expat, blocos de 64 KiB, uma passada), e as entrega com o tipo que o
arquivo declara. Não conhece banco, Flask nem config; não grava em disco, não executa nada do arquivo e não
segue referência nenhuma: fórmula (ss:Formula), vínculo (ss:HRef), estilo, nome definido, comentário e as
opções do Excel são ignorados, e de uma célula com fórmula vale o valor que o arquivo traz em Data.

Contrato (o mesmo do leitor de .xls, para o etl_inputs integrar os dois com uma função só):

    ler(caminho, *, max_abas, max_linhas, max_colunas, max_celulas, max_caracteres, max_area=None,
        verificar=None, materializar=True) -> dict

  devolve {"abas", "aba", "linhas", "colunas", "celulas", "celulas_no_arquivo", "valores"}:
    abas ................ quantas Worksheet o arquivo tem;
    aba ................. índice (base zero) da aba escolhida: a de mais células com valor; no empate, a
                          primeira;
    linhas, colunas ..... da aba escolhida, da primeira linha e coluna até a última célula com valor;
    celulas ............. células com valor na aba escolhida;
    celulas_no_arquivo .. células com valor em todas as abas;
    valores ............. com materializar=True, a matriz da aba escolhida: `linhas` listas de `colunas`
                          posições, com None nas células vazias; senão, None.

  Cada valor é str (String, com o texto como veio, sem aparar), int ou float (Number: int quando o arquivo
  escreve um inteiro, float nos demais casos), datetime.datetime sem fuso (DateTime, com ou sem hora) ou
  bool (Boolean). Error e Data vazio deixam a célula vazia. Célula em branco só com formatação, linha
  vazia e coluna vazia depois da última célula com valor não contam em nada.

  Toda recusa é LeituraRecusada(codigo, mensagem), com mensagem fixa (nenhum dado do arquivo):
    invalid ....... XML malformado ou truncado; DOCTYPE, entidade, notação ou instrução de processamento
                    que não seja "mso-application"; codificação declarada que o conteúdo não segue; elemento
                    da planilha fora do lugar; índice, faixa ou mescla inválidos; valor dentro da área de uma
                    mescla; célula sem tipo; número, data ou valor lógico ilegível; texto fora de Data;
                    aninhamento acima de 64 níveis; endereço de espaço de nomes acima de 256 caracteres;
    unsupported ... raiz que não é Workbook no espaço de nomes da planilha (HTML, documento fiscal, SVG,
                    parte de .xlsx, outro XML); codificação fora da lista aceita; data com fuso horário;
    sheets, rows, columns, cells, field_chars ... limites do chamador, aplicados durante a leitura
                    (`cells` também quando a aba passa do orçamento de elementos, ver _NOS_POR_CELULA, e
                    quando a área da aba escolhida, linhas x colunas, passa de `max_area`);
    empty ......... arquivo sem byte nenhum ou planilha sem célula com valor.
  MemoryError e o que `verificar` levantar passam sem conversão; erro de disco (OSError) também.

Posição das células, pela especificação do formato (XML Spreadsheet Reference, da Microsoft): Row e Cell sem
ss:Index ficam uma posição depois do elemento anterior, e depois de uma célula mesclada (ss:MergeAcross) a
célula sem ss:Index fica na primeira coluna depois da mescla. Índice repetido, fora de ordem ou dentro da
faixa de linhas vazias que a linha anterior declarou (ss:Span) é recusa; linha com ss:Span não tem célula.
De uma mescla (ss:MergeAcross e ss:MergeDown) só a primeira célula tem valor: valor em outra posição da
área é recusa, e não um número a mais (célula vazia ali dentro é tolerada). Linha oculta e coluna oculta são
lidas como as outras. Os tamanhos declarados (ss:ExpandedRowCount e ss:ExpandedColumnCount) e a posição da
tabela na aba (ss:TopCell e ss:LeftCell, que só aparecem ao salvar um intervalo) são ignorados: a matriz
começa na primeira linha e na primeira coluna da tabela.

Memória: durante a leitura fica guardado só o que tem valor (a aba em leitura e a melhor até ali). A matriz
de saída tem linhas x colunas posições: `max_area`, quando dado, é conferido ANTES de ela ser montada (duas
células distantes num arquivo de poucos bytes não ocupam a área inteira); sem ele, a área fica limitada só
por max_linhas x max_colunas.

O que protege a leitura, além dos limites: o DOCTYPE é recusado no primeiro sinal, então não existe entidade
própria, externa nem bomba de entidades; a codificação declarada é conferida numa lista fixa antes de o
Python consultar um codec; o endereço de um espaço de nomes tem tamanho máximo (o expat o repete no nome de
cada elemento, e um endereço enorme faria o tempo crescer com o quadrado do arquivo). O tamanho do arquivo é
conferido por quem chama.
"""

from __future__ import annotations

from datetime import datetime
import re
from xml.parsers import expat

__all__ = ["LeituraRecusada", "ler"]

_ESPACO = "urn:schemas-microsoft-com:office:spreadsheet"
_SEPARADOR = "}"
_BLOCO = 65536
_PROFUNDIDADE_MAXIMA = 64
# Orçamento de elementos de cada aba (e do que fica fora das abas): 12 por célula do limite, mais uma
# parte fixa. Além dos elementos, contam as instruções de processamento, as declarações de espaço de nomes
# e as colunas cobertas por cada mescla para baixo. É a mesma conta do etl_inputs para as partes do XLSX.
_NOS_POR_CELULA = 12
_NOS_FIXOS = 16_384
_ENDERECO_MAXIMO = 256
_NUMERO_MAXIMO = 64
_DIGITOS_DE_INDICE = 9
_LINHAS_POR_VERIFICACAO = 4096
_BRANCOS = " \t\r\n"

# As codificações que o leitor aceita na declaração XML (sem declaração, vale UTF-8 ou UTF-16 pelo BOM).
# UTF-8, UTF-16, US-ASCII e ISO-8859-1 o expat lê sozinho; ISO-8859-15 e windows-1252 passam pelo codec do
# Python. A lista é fixa de propósito: fora dela o Python aceitaria nomes que mudam de uma máquina para
# outra ("mbcs", no Windows) e leria "utf8", sem hífen, como uma codificação de um byte que não tem acento.
_CODIFICACOES = frozenset({"utf-8", "utf-16", "utf-16le", "utf-16be", "us-ascii", "iso-8859-1", "iso-8859-15",
                           "windows-1252"})
_BOM_UTF8 = b"\xef\xbb\xbf"
_BOMS_UTF32 = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")
# As instruções de processamento aceitas, além da declaração XML: só a que o Excel grava. O leitor não
# executa nem segue instrução nenhuma; a lista existe para recusar o que não é desta planilha
# (<?xml-stylesheet?>, por exemplo). Um gerador que grave uma instrução própria entra aqui pelo nome.
_INSTRUCOES_ACEITAS = frozenset({"mso-application"})

_OUTRO, _LIVRO, _ABA, _TABELA, _LINHA, _CELULA, _DADO, _COMENTARIO = range(8)
_ELEMENTOS = {_ESPACO + _SEPARADOR + local: tipo for local, tipo in (
    ("Workbook", _LIVRO), ("Worksheet", _ABA), ("Table", _TABELA), ("Row", _LINHA), ("Cell", _CELULA),
    ("Data", _DADO), ("Comment", _COMENTARIO))}
_A_INDICE = _ESPACO + _SEPARADOR + "Index"
_A_FAIXA = _ESPACO + _SEPARADOR + "Span"
_A_ATRAVESSA = _ESPACO + _SEPARADOR + "MergeAcross"
_A_DESCE = _ESPACO + _SEPARADOR + "MergeDown"
_A_TIPO = _ESPACO + _SEPARADOR + "Type"

_TEXTO, _NUMERO, _DATA, _LOGICO, _ERRO = range(5)
_TIPOS_DE_DADO = {"String": _TEXTO, "Number": _NUMERO, "DateTime": _DATA, "Boolean": _LOGICO, "Error": _ERRO}
_LOGICOS = {"1": True, "0": False, "true": True, "false": False}
# O número como o XML Schema o escreve (xsd:double), só com algarismos ASCII: sem separador de milhar, sem
# vírgula decimal, sem NaN nem infinito. Os grupos só casam quando há casa decimal ou expoente.
_RE_NUMERO = re.compile(r"[+-]?(?:[0-9]+(\.[0-9]*)?|(\.[0-9]+))([eE][+-]?[0-9]+)?")
_RE_DATA = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})"
                      r"(?:T([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?)?"
                      r"(Z|[+-][0-9]{2}(?::?[0-9]{2})?)?")
_INFINITOS = (float("inf"), float("-inf"))

_ORIENTACAO = "Envie a planilha em .xlsx, .xls ou .csv, ou salve o relatório como Planilha XML 2003 no Excel."
_M_VAZIO = "O arquivo está vazio."
_M_SEM_VALOR = "A Planilha XML não contém células com valor."
_M_MALFORMADO = "O arquivo XML está malformado ou incompleto."
_M_CODIFICACAO_INCOERENTE = "A codificação declarada no arquivo XML não corresponde ao conteúdo."
_M_PROIBIDO = ("O arquivo XML contém DOCTYPE, entidade ou notação, que não são aceitos. A Planilha XML 2003 do "
               "Excel não usa esses recursos. " + _ORIENTACAO)
_M_INSTRUCAO = ("O arquivo XML contém uma instrução de processamento que a Planilha XML 2003 do Excel não usa. "
                + _ORIENTACAO)
_M_PROFUNDIDADE = "A estrutura XML do arquivo tem aninhamento excessivo."
_M_ENDERECO = "O arquivo XML declara um espaço de nomes fora do padrão."
_M_ESTRUTURA = "A Planilha XML tem elemento fora do lugar (Workbook, Worksheet, Table, Row, Cell ou Data)."
_M_INDICES = "A Planilha XML tem índice de linha ou de coluna, faixa ou mescla inválidos ou fora de ordem."
_M_ATRIBUTO_SOLTO = "A Planilha XML tem atributo de posição fora do espaço de nomes da planilha."
_M_MESCLA = "A Planilha XML tem valor dentro da área de uma mescla, fora da primeira célula."
_M_TIPO = "A Planilha XML tem célula sem tipo ou com tipo desconhecido."
_M_NUMERO = "A Planilha XML tem célula numérica cujo conteúdo não é um número."
_M_DATA = "A Planilha XML tem célula de data cujo conteúdo não é uma data válida (aaaa-mm-ddThh:mm:ss)."
_M_LOGICO = "A Planilha XML tem célula lógica cujo conteúdo não é verdadeiro nem falso."
_M_TEXTO_SOLTO = "A Planilha XML tem texto fora das células."
_M_ABAS = "A Planilha XML excede o limite de abas permitido."
_M_LINHAS = "A Planilha XML excede o limite de linhas permitido."
_M_COLUNAS = "A Planilha XML excede o limite de colunas permitido."
_M_CELULAS = "Uma aba da Planilha XML excede o limite de células com valor permitido."
_M_AREA = "Uma aba da Planilha XML excede a área de células permitida."
_M_NOS = "A Planilha XML excede o orçamento de elementos permitido."
_M_CARACTERES = "A Planilha XML contém uma célula acima do limite de caracteres permitido."
_M_CODIFICACAO = ("O arquivo XML usa uma codificação de texto que o leitor não lê (as aceitas são UTF-8, UTF-16, "
                  "ISO-8859-1, ISO-8859-15 e windows-1252). Envie a planilha em .xlsx, .xls ou .csv.")
_M_FUSO = ("A Planilha XML traz data com fuso horário, que o leitor não lê. Envie a planilha em .xlsx, .xls ou "
           ".csv.")
_M_RAIZ_HTML = "O arquivo é uma página HTML, e não uma Planilha XML 2003 do Excel. " + _ORIENTACAO
_M_RAIZ_FISCAL = ("O arquivo é o XML de um documento fiscal (NF-e, NFC-e, CF-e ou CT-e), e não um relatório em "
                  "planilha. " + _ORIENTACAO)
_M_RAIZ_SVG = "O arquivo é uma imagem SVG, e não uma planilha. " + _ORIENTACAO
_M_RAIZ_XLSX = ("O arquivo é uma parte interna de um .xlsx desmontado, e não uma Planilha XML 2003 do Excel. "
                "Envie o arquivo .xlsx inteiro, ou a planilha em .xls ou .csv.")
_M_RAIZ_OUTRA = ("O arquivo XML não é uma Planilha XML 2003 do Excel: a raiz não é Workbook no espaço de nomes "
                 "da planilha. " + _ORIENTACAO)

_RAIZES_FISCAIS = frozenset({"nfe", "nfeproc", "procnfe", "cfe", "cfecanc", "cte", "cteproc", "mdfe", "mdfeproc",
                             "nfse", "compnfse"})
_SEM_MEMORIA = expat.errors.codes[expat.errors.XML_ERROR_NO_MEMORY]
_ERRO_DE_CODIFICACAO = expat.errors.codes[expat.errors.XML_ERROR_INCORRECT_ENCODING]


class LeituraRecusada(Exception):
    """Recusa do leitor: `codigo` (ver o contrato no topo do módulo) e `mensagem` fixa, em português."""

    def __init__(self, codigo: str, mensagem: str):
        self.codigo = codigo
        self.mensagem = mensagem
        super().__init__(mensagem)

    def __reduce__(self):
        # Para a recusa atravessar um processo (pickle) com o código, e não só com a mensagem.
        return type(self), (self.codigo, self.mensagem)


def _recusar(codigo, mensagem):
    raise LeituraRecusada(codigo, mensagem)


def _proibido(*_):
    _recusar("invalid", _M_PROIBIDO)


def _mensagem_da_raiz(nome) -> str:
    """Diz que arquivo é, pela raiz, para a recusa orientar o que enviar."""
    endereco, _, local = nome.rpartition(_SEPARADOR)
    local = local.lower()
    if local == "html" or endereco == "http://www.w3.org/1999/xhtml":
        return _M_RAIZ_HTML
    if "portalfiscal.inf.br" in endereco or local in _RAIZES_FISCAIS:
        return _M_RAIZ_FISCAL
    if local == "svg" or endereco == "http://www.w3.org/2000/svg":
        return _M_RAIZ_SVG
    if endereco.startswith(("http://schemas.openxmlformats.org/", "http://purl.oclc.org/ooxml/")):
        return _M_RAIZ_XLSX
    return _M_RAIZ_OUTRA


def _inteiro(bruto) -> int:
    """O valor de ss:Index, ss:Span, ss:MergeAcross ou ss:MergeDown: só algarismos ASCII, até 9."""
    if not bruto.isascii() or not bruto.isdecimal() or len(bruto) > _DIGITOS_DE_INDICE:
        _recusar("invalid", _M_INDICES)
    return int(bruto)


def _numero(texto):
    texto = texto.strip(_BRANCOS)
    if not texto:
        return None
    encontrado = _RE_NUMERO.fullmatch(texto) if len(texto) <= _NUMERO_MAXIMO else None
    if encontrado is None:
        _recusar("invalid", _M_NUMERO)
    if encontrado.lastindex is None:
        return int(texto)
    valor = float(texto)
    if valor in _INFINITOS:
        _recusar("invalid", _M_NUMERO)
    return valor


def _data(texto):
    texto = texto.strip(_BRANCOS)
    if not texto:
        return None
    encontrado = _RE_DATA.fullmatch(texto)
    if encontrado is None:
        _recusar("invalid", _M_DATA)
    ano, mes, dia, hora, minuto, segundo, fracao, fuso = encontrado.groups()
    if fuso:
        # Com fuso há duas leituras possíveis do mesmo dia (a hora escrita ou a hora local), e a planilha
        # não diz qual vale: na dúvida, recusa.
        _recusar("unsupported", _M_FUSO)
    try:
        return datetime(int(ano), int(mes), int(dia), int(hora or 0), int(minuto or 0), int(segundo or 0),
                        int((fracao or "0").ljust(6, "0")[:6]))
    except ValueError:
        _recusar("invalid", _M_DATA)


def _logico(texto):
    texto = texto.strip(_BRANCOS)
    if not texto:
        return None
    valor = _LOGICOS.get(texto.lower())
    if valor is None:
        _recusar("invalid", _M_LOGICO)
    return valor


class _Leitor:
    """O estado de uma leitura: a pilha de elementos, a posição corrente e o que tem valor em cada aba."""

    def __init__(self, max_abas, max_linhas, max_colunas, max_celulas, max_caracteres, materializar):
        self._max_abas, self._max_linhas, self._max_colunas = max_abas, max_linhas, max_colunas
        self._max_celulas, self._max_caracteres = max_celulas, max_caracteres
        self._max_nos = max_celulas * _NOS_POR_CELULA + _NOS_FIXOS
        self._materializar = materializar
        self._pilha = []
        self._nos = self._nos_do_livro = 0
        self._abas = self._total = 0
        self._melhor = None
        self._melhor_celulas = 0
        # A aba em leitura.
        self._tabela_vista = False
        self._celulas = self._linhas_usadas = self._colunas_usadas = 0
        self._indices = self._registros = None
        # A linha e a célula em leitura. `_faixa` é o ss:Span da linha; `_atravessa`, o ss:MergeAcross da
        # célula; `_fim_da_mescla`, a última coluna coberta por mescla nesta linha; `_descidas`, por coluna,
        # a última linha coberta por uma mescla que veio de cima.
        self._linha = self._faixa = 0
        self._coluna = self._atravessa = self._fim_da_mescla = 0
        self._descidas = {}
        self._registro = None
        self._coberta = self._tem_dado = False
        # O Data em leitura.
        self._em_dado = False
        self._tipo = _TEXTO
        self._texto = None
        self._tamanho = 0
        # Os quatro primeiros bytes do arquivo (o BOM, quando há), retidos até chegarem todos.
        self._inicio = None
        self._retido = b""
        # intern=None: o expat não guarda os nomes que vê (um arquivo com milhões de nomes diferentes
        # encheria esse dicionário).
        analisador = expat.ParserCreate(namespace_separator=_SEPARADOR, intern=None)
        analisador.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        analisador.buffer_text = True  # o texto chega em pedaços de até 8 KiB, e não a cada entidade ou quebra
        analisador.XmlDeclHandler = self._declaracao
        analisador.StartElementHandler = self._abrir_raiz
        analisador.EndElementHandler = self._fechar
        analisador.CharacterDataHandler = self._caracteres
        analisador.StartNamespaceDeclHandler = self._espaco_de_nomes
        analisador.ProcessingInstructionHandler = self._instrucao
        analisador.StartDoctypeDeclHandler = _proibido
        analisador.EntityDeclHandler = _proibido
        analisador.UnparsedEntityDeclHandler = _proibido
        analisador.NotationDeclHandler = _proibido
        analisador.ExternalEntityRefHandler = _proibido
        analisador.SkippedEntityHandler = _proibido
        self._analisador = analisador

    # -- entrada e saída ------------------------------------------------------------------------------
    def alimentar(self, bloco, fim=False):
        """Entrega um bloco ao expat. Quando um tratador recusa, o expat para no mesmo ponto."""
        if self._inicio is None:
            bloco = self._retido + bloco
            if len(bloco) < 4 and not fim:
                self._retido = bloco
                return
            self._inicio, self._retido = bloco[:4], b""
            if self._inicio in _BOMS_UTF32:
                _recusar("unsupported", _M_CODIFICACAO)  # o expat não lê UTF-32
        try:
            self._analisador.Parse(bloco, fim)
        except (LeituraRecusada, MemoryError):
            raise
        except expat.ExpatError as erro:
            if erro.code == _SEM_MEMORIA:
                raise MemoryError from None
            mensagem = _M_CODIFICACAO_INCOERENTE if erro.code == _ERRO_DE_CODIFICACAO else _M_MALFORMADO
            raise LeituraRecusada("invalid", mensagem) from None
        except Exception:
            # O que mais o expat ou a conversão do texto levantarem diante de um arquivo malformado.
            raise LeituraRecusada("invalid", _M_MALFORMADO) from None

    def soltar(self):
        """Desfaz o ciclo entre o leitor e o analisador do expat."""
        self._analisador = None

    def resultado(self, verificar, max_area=None) -> dict:
        if self._melhor is None:
            _recusar("empty", _M_SEM_VALOR)
        aba, linhas, colunas, celulas, indices, registros = self._melhor
        self._melhor = None
        # A área é conferida antes de a matriz existir: é a matriz, e não a leitura, que custa linhas x colunas.
        if max_area is not None and linhas * colunas > max_area:
            _recusar("cells", _M_AREA)
        valores = _matriz(indices, registros, linhas, colunas, verificar) if self._materializar else None
        return {"abas": self._abas, "aba": aba, "linhas": linhas, "colunas": colunas, "celulas": celulas,
                "celulas_no_arquivo": self._total, "valores": valores}

    # -- o que não é elemento -------------------------------------------------------------------------
    def _declaracao(self, _versao, codificacao, _autonomo):
        if codificacao is None:
            return
        codificacao = codificacao.lower()
        if codificacao not in _CODIFICACOES:
            _recusar("unsupported", _M_CODIFICACAO)
        if self._inicio.startswith(_BOM_UTF8) and codificacao != "utf-8":
            # O expat seguiria a declaração e leria o UTF-8 como texto de um byte: acentos trocados em silêncio.
            _recusar("invalid", _M_CODIFICACAO_INCOERENTE)

    def _espaco_de_nomes(self, _prefixo, endereco):
        self._contar(1)
        if endereco is not None and len(endereco) > _ENDERECO_MAXIMO:
            _recusar("invalid", _M_ENDERECO)

    def _instrucao(self, alvo, _dado):
        self._contar(1)
        if alvo not in _INSTRUCOES_ACEITAS:
            _recusar("invalid", _M_INSTRUCAO)

    def _contar(self, quantos):
        self._nos += quantos
        if self._nos > self._max_nos:
            _recusar("cells", _M_NOS)

    def _caracteres(self, texto):
        partes = self._texto
        if partes is not None:
            self._tamanho += len(texto)
            if self._tamanho > self._max_caracteres:
                _recusar("field_chars", _M_CARACTERES)
            partes.append(texto)
        elif not texto.isspace() and not self._em_dado and self._pilha and 0 < self._pilha[-1] <= _CELULA:
            # Valor escrito direto em Cell, Row ou Table, sem Data: ignorar seria perder dado em silêncio.
            _recusar("invalid", _M_TEXTO_SOLTO)

    # -- elementos ------------------------------------------------------------------------------------
    def _abrir_raiz(self, nome, _atributos):
        if _ELEMENTOS.get(nome) != _LIVRO:
            _recusar("unsupported", _mensagem_da_raiz(nome))
        self._contar(1)
        self._pilha.append(_LIVRO)
        self._analisador.StartElementHandler = self._abrir

    def _abrir(self, nome, atributos):
        self._nos += 1
        if self._nos > self._max_nos:
            _recusar("cells", _M_NOS)
        pilha = self._pilha
        if len(pilha) >= _PROFUNDIDADE_MAXIMA:
            _recusar("invalid", _M_PROFUNDIDADE)
        tipo = _ELEMENTOS.get(nome, _OUTRO)
        if tipo == _OUTRO:
            pilha.append(_OUTRO)
            return
        if self._em_dado:
            # Dentro de Data só cabe a marcação do texto rico (B, I, Font...), nunca um elemento da planilha.
            _recusar("invalid", _M_ESTRUTURA)
        pai = pilha[-1]
        if tipo == _CELULA:
            if pai != _LINHA:
                _recusar("invalid", _M_ESTRUTURA)
            self._abrir_celula(atributos)
        elif tipo == _DADO:
            if pai == _CELULA:
                self._abrir_dado(atributos)
            elif pai == _COMENTARIO:
                tipo = _OUTRO  # o texto de um comentário não é valor da célula
            else:
                _recusar("invalid", _M_ESTRUTURA)
        elif tipo == _LINHA:
            if pai != _TABELA:
                _recusar("invalid", _M_ESTRUTURA)
            self._abrir_linha(atributos)
        elif tipo == _TABELA:
            if pai != _ABA or self._tabela_vista:
                _recusar("invalid", _M_ESTRUTURA)
            self._tabela_vista = True
        elif tipo == _ABA:
            if pai != _LIVRO:
                _recusar("invalid", _M_ESTRUTURA)
            self._abrir_aba()
        elif tipo == _LIVRO:
            _recusar("invalid", _M_ESTRUTURA)
        pilha.append(tipo)

    def _fechar(self, _nome):
        tipo = self._pilha.pop()
        if tipo == _DADO:
            self._fechar_dado()
        elif tipo == _ABA:
            self._fechar_aba()

    def _abrir_aba(self):
        self._abas += 1
        if self._abas > self._max_abas:
            _recusar("sheets", _M_ABAS)
        self._nos_do_livro, self._nos = self._nos, 0
        self._tabela_vista = False
        self._celulas = self._linhas_usadas = self._colunas_usadas = 0
        self._linha = self._faixa = 0
        self._descidas = {}
        if self._materializar:
            self._indices, self._registros = [], []

    def _fechar_aba(self):
        self._total += self._celulas
        if self._celulas > self._melhor_celulas:  # no empate fica a primeira
            self._melhor_celulas = self._celulas
            self._melhor = (self._abas - 1, self._linhas_usadas, self._colunas_usadas, self._celulas,
                            self._indices, self._registros)
        self._indices = self._registros = self._registro = None
        self._nos = self._nos_do_livro

    def _abrir_linha(self, atributos):
        linha = self._linha + 1
        faixa = 0
        bruto = atributos.get(_A_INDICE) if atributos else None
        if bruto is not None:
            linha = _inteiro(bruto)
            if linha <= self._linha + self._faixa:
                _recusar("invalid", _M_INDICES)
        elif self._faixa:
            # Sem índice, a linha fica uma depois da anterior, e a anterior declarou uma faixa (ss:Span) que
            # ocupa essa posição: pela especificação os índices se sobrepõem (o Excel grava o índice aqui).
            _recusar("invalid", _M_INDICES)
        if atributos:
            bruto = atributos.get(_A_FAIXA)
            if bruto is not None:
                faixa = _inteiro(bruto)
            if "Index" in atributos or "Span" in atributos:
                _recusar("invalid", _M_ATRIBUTO_SOLTO)
        self._linha, self._faixa = linha, faixa
        self._coluna = self._atravessa = self._fim_da_mescla = 0
        self._registro = None

    def _abrir_celula(self, atributos):
        if self._faixa:
            _recusar("invalid", _M_INDICES)  # linha com ss:Span é só formatação: não tem célula
        coluna = self._coluna + self._atravessa + 1
        atravessa = desce = 0
        if atributos:
            bruto = atributos.get(_A_INDICE)
            if bruto is not None:
                coluna = _inteiro(bruto)
                if coluna <= self._coluna:
                    _recusar("invalid", _M_INDICES)
            bruto = atributos.get(_A_ATRAVESSA)
            if bruto is not None:
                atravessa = _inteiro(bruto)
            bruto = atributos.get(_A_DESCE)
            if bruto is not None:
                desce = _inteiro(bruto)
            if "Index" in atributos or "MergeAcross" in atributos or "MergeDown" in atributos:
                _recusar("invalid", _M_ATRIBUTO_SOLTO)
        linha, descidas = self._linha, self._descidas
        self._coberta = coluna <= self._fim_da_mescla or (bool(descidas) and descidas.get(coluna, 0) >= linha)
        self._coluna, self._atravessa = coluna, atravessa
        self._tem_dado = False
        if atravessa or desce:
            fim = coluna + atravessa
            if fim > self._fim_da_mescla:
                self._fim_da_mescla = fim
            if desce:
                # Só interessam as colunas em que ainda cabe valor; o trabalho entra no orçamento da aba.
                fim = min(fim, self._max_colunas)
                if fim >= coluna:
                    self._contar(fim - coluna + 1)
                    ultima = linha + desce
                    for coberta in range(coluna, fim + 1):
                        if descidas.get(coberta, 0) < ultima:
                            descidas[coberta] = ultima

    def _abrir_dado(self, atributos):
        if self._tem_dado:
            _recusar("invalid", _M_ESTRUTURA)  # uma célula tem no máximo um Data
        self._tem_dado = True
        tipo = _TIPOS_DE_DADO.get(atributos.get(_A_TIPO))
        if tipo is None:
            _recusar("invalid", _M_TIPO)
        self._tipo = tipo
        self._em_dado = True
        if tipo != _ERRO:
            self._texto = []
            self._tamanho = 0

    def _fechar_dado(self):
        self._em_dado = False
        partes = self._texto
        if partes is None:
            return  # ss:Type="Error": a célula fica vazia
        self._texto = None
        texto = partes[0] if len(partes) == 1 else "".join(partes)
        tipo = self._tipo
        if tipo == _TEXTO:
            valor = texto or None
        elif tipo == _NUMERO:
            valor = _numero(texto)
        elif tipo == _DATA:
            valor = _data(texto)
        else:
            valor = _logico(texto)
        if valor is None:
            return
        if self._coberta:
            _recusar("invalid", _M_MESCLA)
        linha, coluna = self._linha, self._coluna
        if linha > self._max_linhas:
            _recusar("rows", _M_LINHAS)
        if coluna > self._max_colunas:
            _recusar("columns", _M_COLUNAS)
        self._celulas += 1
        if self._celulas > self._max_celulas:
            _recusar("cells", _M_CELULAS)
        self._linhas_usadas = linha
        if coluna > self._colunas_usadas:
            self._colunas_usadas = coluna
        if self._materializar:
            registro = self._registro
            if registro is None:
                registro = self._registro = []
                self._indices.append(linha)
                self._registros.append(registro)
            registro.append(coluna)
            registro.append(valor)


def _matriz(indices, registros, linhas, colunas, verificar) -> list:
    """A matriz da aba escolhida, a partir do que tem valor: `indices` são as linhas com valor e cada
    registro alterna coluna e valor. Os registros são soltos à medida que viram linha."""
    valores = []
    posicao, proxima = 0, indices[0]
    for numero in range(1, linhas + 1):
        if verificar is not None and not numero % _LINHAS_POR_VERIFICACAO:
            verificar()
        nova = [None] * colunas
        if numero == proxima:
            registro, registros[posicao] = registros[posicao], None
            for item in range(0, len(registro), 2):
                nova[registro[item] - 1] = registro[item + 1]
            posicao += 1
            proxima = indices[posicao] if posicao < len(indices) else 0
        valores.append(nova)
    return valores


def ler(caminho, *, max_abas, max_linhas, max_colunas, max_celulas, max_caracteres, max_area=None,
        verificar=None, materializar=True) -> dict:
    """Lê a Planilha XML 2003 em `caminho` e devolve a aba de mais células com valor (ver o contrato no
    topo do módulo). `max_area`, se dado, é o teto de linhas x colunas da aba escolhida, conferido antes
    de a matriz ser montada (com ou sem `materializar`). `verificar`, sem argumentos, é chamada antes de
    cada bloco de 64 KiB e a cada 4.096 linhas da matriz; o que ela levantar passa adiante."""
    for nome, valor in (("max_abas", max_abas), ("max_linhas", max_linhas), ("max_colunas", max_colunas),
                        ("max_celulas", max_celulas), ("max_caracteres", max_caracteres)):
        if type(valor) is not int or valor < 1:
            raise ValueError("Limite inválido do leitor de Planilha XML: " + nome)
    if max_area is not None and (type(max_area) is not int or max_area < 1):
        raise ValueError("Limite inválido do leitor de Planilha XML: max_area")
    if verificar is not None and not callable(verificar):
        raise TypeError("O parâmetro verificar precisa ser uma função sem argumentos.")
    leitor = _Leitor(max_abas, max_linhas, max_colunas, max_celulas, max_caracteres, bool(materializar))
    try:
        with open(caminho, "rb") as arquivo:
            vazio = True
            while True:
                if verificar is not None:
                    verificar()
                bloco = arquivo.read(_BLOCO)
                if not bloco:
                    break
                vazio = False
                leitor.alimentar(bloco)
        if vazio:
            _recusar("empty", _M_VAZIO)
        leitor.alimentar(b"", True)
    finally:
        leitor.soltar()
    return leitor.resultado(verificar, max_area)
