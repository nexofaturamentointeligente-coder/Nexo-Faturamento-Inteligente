"""Triagem dos relatórios enviados pela landing, antes de qualquer gravação.

Só .csv e .xlsx, até o teto de tamanho. A extensão sai do nome ORIGINAL do arquivo (o nome exibido é limpo à
parte), o tipo declarado pelo navegador tem de ser um dos esperados para ela, e o conteúdo tem de confirmar
os dois, porque extensão e tipo vêm do cliente.

.xlsx: lido como pacote OPC, com o expat em blocos de 64 KB (sem expressão regular sobre o XML cru):
  - o ZIP: só entradas sem cifra, comprimidas por deflate ou guardadas, com nomes válidos e sem repetição,
    até 2000 partes e 100 MB descompactados, sem taxa de compressão de bomba;
  - a estrutura de cada parte XML: até 256 elementos abertos ao mesmo tempo, 2000 nomes distintos e 1 MB numa
    só marcação ou comentário (a memória do expat cresce com cada um deles, e não com o tamanho da parte);
  - cada parte tem um tipo de conteúdo no [Content_Types].xml, comparado por igualdade com uma lista branca de
    partes de dados (pasta de trabalho, abas, textos, estilos, tema, tabelas, desenhos, gráficos, imagens,
    propriedades); macro, ActiveX, objeto, vínculo, conexão, modelo, VML ou qualquer outro tipo é recusado,
    em qualquer caminho;
  - a pasta de trabalho é a do tipo de planilha comum (nem modelo nem com macro);
  - toda relação é de dados e interna (TargetMode External é recusado);
  - toda parte XML é lida inteira: DOCTYPE e ENTITY recusados; objeto, controle, vínculo DDE e publicação na
    web recusados em qualquer namespace; forma com macro atribuída recusada; e o texto decodificado (inclusive o
    escape _xHHHH_) de toda fórmula (célula, nome definido, formatação condicional e o valor do cfvo, validação,
    coluna calculada de tabela, gráfico, texto de forma ligado a célula) não pode chamar endereço externo,
    programa ou DDE, nem apontar para outro arquivo.
.csv: texto (sem byte nulo nem controle), não um binário, página ou XML renomeado, com separador de colunas,
  e sem célula que comece como fórmula (=, +, -, @) e chame endereço externo, programa ou DDE, ou aponte para
  outro arquivo. O texto é decodificado como o Excel o lê: com a marca do UTF-8 (BOM), UTF-8; sem ela, UTF-8
  ou, não sendo, cp1252 (o Excel em português). As células são lidas como o Excel poderia lê-las: por todo
  separador que ele poderia usar (vírgula, ponto e vírgula, tabulação, barra vertical e o da linha sep=), pela
  linha inteira, e com o tratamento de aspas (que junta "=HYPER"LINK( numa célula só). Para não custar o
  arquivo inteiro célula a célula, as linhas candidatas são achadas antes, no texto sem aspas e em minúsculas;
  a leitura sem aspas confere só essas linhas, nesse mesmo texto, e a leitura com aspas, só os registros que as
  tocam, cada registro com até 1 MB.
.xlsm (e .xltm, .xlam) e qualquer tipo declarado macroEnabled: recusados com a mensagem de macro.

A triagem de um envio tem prazo (o `prazo`, em time.monotonic()): o arquivo que não termina a tempo é recusado
com MOTIVO_DEMORADO, e o serviço não fica preso num arquivo feito para custar caro. O arquivo recusado não é
gravado em lugar nenhum; o aprovado segue byte a byte para o Storage.
"""

from array import array
import codecs
import csv
from dataclasses import dataclass
import hashlib
import heapq
import io
import itertools
import os
import posixpath
import re
import string
import time
import unicodedata
import urllib.parse
from xml.parsers import expat
import zipfile

MEGABYTE = 1024 * 1024
TIPOS = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
# O que os navegadores declaram para cada extensão: o Windows com o Excel instalado declara
# application/vnd.ms-excel para o .csv, e alguns navegadores de celular, application/octet-stream. Quem decide
# é o conteúdo; o tipo declarado só precisa ser compatível.
TIPOS_DECLARADOS = {
    "csv": {"text/csv", "application/csv", "text/x-csv", "application/x-csv", "text/comma-separated-values",
            "text/plain", "application/vnd.ms-excel", "application/octet-stream"},
    "xlsx": {TIPOS["xlsx"], "application/octet-stream", "application/zip", "application/x-zip-compressed"},
}
EXTENSOES_COM_MACRO = {"xlsm", "xltm", "xlam"}
NOME_EXIBIDO_MAXIMO = 100

MOTIVO_MACRO = "Arquivo com macros (.xlsm) não é aceito: salve a planilha como .xlsx ou .csv."
MOTIVO_FORMATO = "Formato não aceito: envie .csv ou .xlsx."
MOTIVO_NAO_E_XLSX = "O conteúdo não é uma planilha .xlsx."
MOTIVO_FORMULA = "O arquivo tem fórmula que chama endereço externo, programa ou DDE."
MOTIVO_CONTEUDO_ATIVO = ("A planilha tem conteúdo além dos dados (objeto, vínculo, conexão, comentário antigo ou outro "
                         "conteúdo ativo): salve uma cópia só com os dados, ou exporte como .csv.")
MOTIVO_XML = "A planilha tem declarações XML não permitidas."
MOTIVO_ESTRUTURA = ("A planilha tem XML fora do padrão de uma planilha (aninhamento, nomes ou marcação grandes "
                    "demais).")
MOTIVO_TIPO_TROCADO = "A planilha tem uma parte interna com o tipo trocado."
MOTIVO_DEMORADO = ("O arquivo é grande demais para conferir agora: envie um período menor ou divida o relatório em "
                   "partes.")
MOTIVO_CSV_MALFORMADO = "O .csv está malformado (aspas sem fechar ou campo grande demais)."

_ZIP = b"PK\x03\x04"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_BOM = b"\xef\xbb\xbf"
LIMITE_DE_PARTES = 2000
LIMITE_DESCOMPACTADO = 100 * MEGABYTE
TAXA_MAXIMA_DE_COMPRESSAO = 100
LIMITE_DE_FORMULA = 32768
LIMITE_DE_ANINHAMENTO = 256
LIMITE_DE_NOMES = 2000
LIMITE_DO_TAMANHO_DOS_NOMES = 256 * 1024
# Entradas guardadas por parte: tipos de conteúdo (Default e Override) e relações de dados.
LIMITE_DE_ENTRADAS = 10_000
LIMITE_DE_MARCACAO = MEGABYTE
LIMITE_DO_REGISTRO = MEGABYTE
BLOCO = 65536

TIPO_PASTA_DE_TRABALHO = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_PLANILHA = "application/vnd.openxmlformats-officedocument.spreadsheetml."
# Tipos de conteúdo das partes que uma planilha só de dados pode ter (em minúsculas, por igualdade).
TIPOS_DE_PARTE_PERMITIDOS = frozenset(tipo.lower() for tipo in (
    TIPO_PASTA_DE_TRABALHO,
    _PLANILHA + "worksheet+xml", _PLANILHA + "sharedStrings+xml", _PLANILHA + "styles+xml",
    _PLANILHA + "calcChain+xml", _PLANILHA + "table+xml", _PLANILHA + "sheetMetadata+xml",
    _PLANILHA + "printerSettings",
    "application/vnd.openxmlformats-officedocument.theme+xml",
    "application/vnd.openxmlformats-officedocument.drawing+xml",
    "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
    "application/vnd.ms-office.chartstyle+xml", "application/vnd.ms-office.chartcolorstyle+xml",
    "application/vnd.openxmlformats-package.core-properties+xml",
    "application/vnd.openxmlformats-officedocument.extended-properties+xml",
    "application/vnd.openxmlformats-officedocument.custom-properties+xml",
    "application/vnd.openxmlformats-package.relationships+xml",
    "image/png", "image/jpeg", "image/gif",
))
_TIPOS_BINARIOS = frozenset({(_PLANILHA + "printerSettings").lower(), "image/png", "image/jpeg", "image/gif"})
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
RELACAO_DA_PASTA_DE_TRABALHO = _R + "officeDocument"
# Tipos de relação de dados (internas): pasta, abas, textos, estilos, tema, tabelas, desenhos, gráficos, imagens
# e propriedades.
RELACOES_PERMITIDAS = frozenset({
    RELACAO_DA_PASTA_DE_TRABALHO, _R + "extended-properties", _R + "custom-properties", _R + "worksheet",
    _R + "styles", _R + "sharedStrings", _R + "theme", _R + "calcChain", _R + "table", _R + "drawing",
    _R + "chart", _R + "image", _R + "printerSettings", _R + "sheetMetadata",
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail",
    "http://schemas.microsoft.com/office/2011/relationships/chartStyle",
    "http://schemas.microsoft.com/office/2011/relationships/chartColorStyle",
})
# O tipo de conteúdo que a parte tem de ter para o papel que a relação lhe dá (em minúsculas).
_IMAGENS = frozenset({"image/png", "image/jpeg", "image/gif"})
_TIPOS_DO_PAPEL = {relacao: frozenset(tipo.lower() for tipo in tipos) for relacao, tipos in {
    RELACAO_DA_PASTA_DE_TRABALHO: {TIPO_PASTA_DE_TRABALHO},
    _R + "worksheet": {_PLANILHA + "worksheet+xml"},
    _R + "styles": {_PLANILHA + "styles+xml"},
    _R + "sharedStrings": {_PLANILHA + "sharedStrings+xml"},
    _R + "calcChain": {_PLANILHA + "calcChain+xml"},
    _R + "table": {_PLANILHA + "table+xml"},
    _R + "sheetMetadata": {_PLANILHA + "sheetMetadata+xml"},
    _R + "printerSettings": {_PLANILHA + "printerSettings"},
    _R + "theme": {"application/vnd.openxmlformats-officedocument.theme+xml"},
    _R + "drawing": {"application/vnd.openxmlformats-officedocument.drawing+xml"},
    _R + "chart": {"application/vnd.openxmlformats-officedocument.drawingml.chart+xml"},
    _R + "image": _IMAGENS,
    _R + "extended-properties": {"application/vnd.openxmlformats-officedocument.extended-properties+xml"},
    _R + "custom-properties": {"application/vnd.openxmlformats-officedocument.custom-properties+xml"},
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties":
        {"application/vnd.openxmlformats-package.core-properties+xml"},
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail": _IMAGENS,
    "http://schemas.microsoft.com/office/2011/relationships/chartStyle": {"application/vnd.ms-office.chartstyle+xml"},
    "http://schemas.microsoft.com/office/2011/relationships/chartColorStyle":
        {"application/vnd.ms-office.chartcolorstyle+xml"},
}.items()}
assert set(_TIPOS_DO_PAPEL) == RELACOES_PERMITIDAS
_NAMESPACE_DOS_TIPOS = "http://schemas.openxmlformats.org/package/2006/content-types"
RELACAO_DE_HIPERLINK = _R + "hyperlink"
# Um local do próprio arquivo (#Aba!A1, #'Outra aba'!B2:C3, #NomeDefinido), sem caminho, endereço nem pasta de
# trabalho.
_LOCAL_NO_PROPRIO_ARQUIVO = re.compile(r"#(?![^#]*\.xl)[^\\/\[\]]*", re.IGNORECASE)
# Elementos que só objeto, controle, vínculo ou publicação na web têm, em qualquer namespace.
_ELEMENTOS_ATIVOS = frozenset({"oleObject", "oleObjects", "control", "controls", "ddeLink", "oleLink",
                               "externalReference", "externalReferences", "webPublishItem", "webPublishItems",
                               "webPublishObject", "webPublishObjects"})
# Elementos cujo texto é fórmula, em qualquer namespace (inclusive as extensões x14 e xm, e o c:f dos gráficos).
_ELEMENTOS_DE_FORMULA = frozenset({"f", "formula", "formula1", "formula2", "definedName",
                                   "calculatedColumnFormula", "totalsRowFormula"})
# Atributos que guardam fórmula: o valor do cfvo da formatação condicional (escala de cor, barra, ícones) e o
# vínculo do texto de uma forma com uma célula. O atributo macro de uma forma (a macro do clique) não tem uso num
# .xlsx: preenchido, só aponta para outro arquivo.
_ATRIBUTOS_DE_FORMULA = {"cfvo": frozenset({"val"}), "sp": frozenset({"textlink"})}
_FORMAS = frozenset({"sp", "pic", "graphicFrame", "cxnSp"})
_ATIVO, _FORMULA = "ativo", "formula"
# Um só dicionário, consultado uma vez por elemento: o tipo do elemento, ou os atributos dele a conferir.
_ESPECIAIS = {**{nome: _ATIVO for nome in _ELEMENTOS_ATIVOS}, **{nome: _FORMULA for nome in _ELEMENTOS_DE_FORMULA},
              **{nome: _ATRIBUTOS_DE_FORMULA.get(nome, frozenset()) for nome in _FORMAS},
              **_ATRIBUTOS_DE_FORMULA}
# O escape de caractere do OOXML (_xHHHH_), que vale também no texto de fórmula (ST_Formula restringe ST_Xstring).
_ESCAPE_DO_OOXML = re.compile(r"_x([0-9A-Fa-f]{4})_")

_BINARIOS = (_ZIP, _OLE, b"%PDF", b"\x7fELF", b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"{\\rtf", b"\x1f\x8b",
             b"Rar!", b"7z\xbc\xaf")
_UTF16 = (b"\xff\xfe", b"\xfe\xff")
_MARCACAO = (b"<?xml", b"<html", b"<!doctype", b"<script", b"<svg", b"<table")
_CONTROLES = bytes(byte for byte in range(32) if byte not in (9, 10, 12, 13)) + b"\x7f"

# Funções que buscam endereço externo, abrem vínculo ou chamam programa, em minúsculas e como trechos de
# expressão regular: os nomes em inglês (como o .xlsx os guarda e o Excel em inglês lê o .csv) e em português
# (como o Excel em português pode ler o .csv). O Ç de SERVIÇOWEB vale por qualquer caractere (maiúsculo, ou lido
# na codificação errada: Ã‡, ou o caractere de substituição). CHAMAR e REGISTRO, os nomes de CALL e REGISTER em
# português, ficam de fora: só funcionam em folha de macro (que o .xlsx recusa e o .csv não tem) e são palavras
# comuns num texto.
_FUNCOES_PERIGOSAS = (
    "hyperlink", "hiperlink", "webservice", r"servi.{1,2}oweb", "image", "imagem", "filterxml", "filtroxml",
    "importtext", "importartexto", "importcsv", "importxml", "importdata", "importhtml", "importfeed",
    "importrange", "rtd", "call", "register", r"register\.id", r"ident\.registro", "exec", "dde", "ddeauto",
)
_NOMES = "(?:" + "|".join(_FUNCOES_PERIGOSAS) + ")"
# O que torna perigosa uma fórmula, no texto com as letras ASCII em minúsculas (os nomes de função são ASCII).
# Sem quantificador aninhado nem trechos que se sobreponham: tempo linear. Só roda sobre textos curtos (uma
# fórmula do .xlsx, uma célula ou uma linha candidata do .csv, até 1 MB): sobre o arquivo inteiro, a alternância
# dos nomes custaria segundos sem soltar o GIL.
_PERIGO = re.compile(
    r"\b" + _NOMES + r"\s*\("              # chamada de função perigosa (com o prefixo _xlfn. ou não)
    r"|_xleta\." + _NOMES + r"\b"          # a função passada como valor, como o Excel a grava no arquivo
    r"|\|[^|!]*!"                           # DDE: programa|'tópico'!item (o tópico pode ter quebra de linha)
    r"|'[^'\\/\[\]]*[\\/\[\]][^']*'!"      # outro arquivo, com caminho ou endereço: '\\host\pasta\[x.xlsx]Aba'!A1
    r"|\]\w[\w.]*!"                         # outra pasta de trabalho: [Livro.xlsx]Aba!A1
)
# A função passada como valor, como se digita (MAP(A1:A9;WEBSERVICE), LET(f,WEBSERVICE,...)): o nome sozinho
# como argumento. Só vale depois de um parêntese aberto na mesma fórmula (é sempre o argumento de uma chamada);
# sem essa condição, um texto como "-1;Imagem;TV" seria recusado.
_ARGUMENTO = re.compile(r"[(,;]\s*(?:_xlfn\.|_xleta\.)?" + _NOMES + r"\s*[),;]")
# A função entre parênteses e chamada em seguida, como em =(WEBSERVICE)(A1): aqui, o parêntese da chamada vem
# depois do nome. Por cautela (não deu para conferir se o Excel aceita), conta como a função passada como valor.
# O parêntese da chamada vem colado ao que fecha (com espaço, o Excel lê a interseção, não uma chamada).
_VALOR_CHAMADO = re.compile(r"[(,;]\s*(?:_xlfn\.|_xleta\.)?" + _NOMES + r"\s*\)(?:[\s)]*\))?\(")
# O parêntese de uma chamada: colado a um nome ou a outro parêntese; e, por cautela, também com espaço depois das
# funções que recebem outra função (as do Excel em inglês).
_LAMBDAS = r"\b(?:_xlfn\.)?(?:map|reduce|scan|makearray|byrow|bycol|let|lambda|groupby|pivotby)"
_PARENTESE_DE_CHAMADA = re.compile(r"[\w)]\(|" + _LAMBDAS + r"\s+\(")
_FUNCAO_QUE_RECEBE_FUNCAO = re.compile(_LAMBDAS + r"$")
_BLOCO_DAS_CHAMADAS = 65536
# O que leva uma linha do .csv à leitura célula a célula, no texto sem aspas e com as letras ASCII em minúsculas,
# achado por str.find ou por uma expressão curta buscada em janelas de 1 MB (nenhuma percorre o arquivo inteiro
# segurando o GIL): o nome de uma função perigosa seguido de parêntese, vírgula, ponto e vírgula ou parêntese de
# fechamento, ou depois de _xleta.; a barra vertical do DDE; o apóstrofo que fecha a referência a outro arquivo
# ('!); e o colchete da pasta de trabalho (]Aba!). É um superconjunto do _PERIGO, do _VALOR_CHAMADO e do
# _ARGUMENTO: cada trecho que eles acham numa célula começa num destes gatilhos, dentro da mesma célula.
# Um gatilho por nome: a parte literal do começo deixa a busca saltar, em C, cada ocorrência que não vem seguida de
# parêntese, vírgula ou ponto e vírgula (a palavra "Imagem" num texto não custa nada); o \Z é o fim da janela da
# busca, para o espaço que chega até ela.
_GATILHOS_DAS_FUNCOES = tuple(re.compile(nome + r"\s*(?P<fim>[(),;]|\Z)") for nome in _FUNCOES_PERIGOSAS)
_NOME_PERIGOSO = re.compile(_NOMES)
# Cada busca do pré-filtro percorre no máximo 1 MB (uma só chamada em C sobre o arquivo inteiro seguraria o GIL), e
# a janela seguinte volta um pouco, para o nome cortado na borda.
_JANELA_DA_BUSCA = LIMITE_DO_REGISTRO
_SOBREPOSICAO_DA_JANELA = 64
_PASTA_DE_TRABALHO = re.compile(r"\w[\w.]*!")
_INICIO_DE_FORMULA = re.compile(r"""[\s"']*[=+\-@]""")
# O primeiro caractere de uma célula que pode começar como fórmula (além dos espaços, que o str.isspace() confere
# como o \s da expressão acima).
_COMECO_DE_FORMULA = frozenset("=+-@\"'")
_BLOCO_DO_PLANO = 1_000_000
_SEPARADORES_DO_EXCEL = (";", ",", "\t", "|")
# Para a leitura do registro inteiro com aspas: um separador que nunca está no texto (os caracteres de controle
# são recusados antes).
_SEPARADOR_AUSENTE = "\x1f"
_SEPARADOR_DECLARADO = re.compile(r"""\s*["']?sep=(.)["']?\s*""", re.IGNORECASE)
_NOME_PROIBIDO = re.compile(r'[<>:"/\\|?*]')
_CONFERIR_PRAZO_A_CADA = 4096


def _cp1252_do_windows() -> str:
    """A tabela do cp1252 como o Windows o lê: os 5 bytes que o Python não define viram o caractere de mesmo número.
    Decodificar pela tabela (codecs.charmap_decode) é todo em C; um tratador de erro em Python custaria segundos
    num arquivo feito desses bytes."""
    caracteres = []
    for byte in range(256):
        try:
            caracteres.append(bytes([byte]).decode("cp1252"))
        except UnicodeDecodeError:
            caracteres.append(chr(byte))
    return "".join(caracteres)


_CP1252 = _cp1252_do_windows()
# O leitor de CSV recusa campo acima do limite dele (128 KB por padrão); o teto deste serviço é o do registro.
csv.field_size_limit(max(csv.field_size_limit(), LIMITE_DO_REGISTRO + 1))


class _Recusa(Exception):
    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


@dataclass(frozen=True)
class Resultado:
    nome: str
    aprovado: bool
    motivo: str = None
    formato: str = None
    tipo: str = None
    conteudo: bytes = None
    sha256: str = None
    tamanho: int = 0


def _no_prazo(prazo) -> None:
    if prazo is not None and time.monotonic() > prazo:
        raise _Recusa(MOTIVO_DEMORADO)


def _ultimo_componente(nome) -> str:
    return re.split(r"[\\/]", nome if isinstance(nome, str) else "")[-1]


def nome_exibido(nome) -> str:
    """O nome para mostrar e guardar: sem pasta, sem caracteres de controle, invisíveis ou proibidos em nome de
    arquivo (inclusive < > e aspas), até 100 caracteres (cortando o começo do nome, nunca a extensão), e sem
    começar como fórmula. Quem o exibir numa página ou planilha ainda tem de escapá-lo."""
    texto = "".join(caractere for caractere in _ultimo_componente(nome)
                    if unicodedata.category(caractere) not in ("Cc", "Cf", "Zl", "Zp"))
    texto = " ".join(_NOME_PROIBIDO.sub("", texto).split()).strip(" .") or "arquivo"
    if texto[0] in "=+-@":
        texto = "_" + texto
    if len(texto) > NOME_EXIBIDO_MAXIMO:
        base, extensao_do_nome = os.path.splitext(texto)
        texto = base[:NOME_EXIBIDO_MAXIMO - len(extensao_do_nome)] + extensao_do_nome
    return texto[:NOME_EXIBIDO_MAXIMO]


def extensao(nome) -> str:
    """A extensão do nome original, em minúsculas (um nome todo fora do alfabeto latino continua valendo)."""
    return os.path.splitext(_ultimo_componente(nome).strip())[1].lower().lstrip(".")


def triar(nome, tipo_declarado, conteudo: bytes, max_bytes: int, prazo=None) -> Resultado:
    exibido, formato = nome_exibido(nome), extensao(nome)
    tamanho = len(conteudo or b"")

    def recusa(motivo):
        return Resultado(exibido, False, motivo, formato if formato in TIPOS else None, tamanho=tamanho)

    tipo = str(tipo_declarado or "").split(";", 1)[0].strip().lower()
    if formato in EXTENSOES_COM_MACRO or "macroenabled" in tipo:
        return recusa(MOTIVO_MACRO)
    if formato not in TIPOS:
        return recusa(MOTIVO_FORMATO)
    if tipo not in TIPOS_DECLARADOS[formato]:
        return recusa(f"O tipo do arquivo não confere com a extensão .{formato}.")
    if not tamanho:
        return recusa("O arquivo está vazio.")
    if tamanho > max_bytes:
        return recusa(f"O arquivo passa de {max_bytes // MEGABYTE} MB.")
    problema = conferir_xlsx(conteudo, prazo) if formato == "xlsx" else conferir_csv(conteudo, prazo)
    if problema:
        return recusa(problema)
    return Resultado(exibido, True, None, formato, TIPOS[formato], conteudo, hashlib.sha256(conteudo).hexdigest(),
                     tamanho)


# ---------------------------------------------------------------------------------------------------------------
# .xlsx
# ---------------------------------------------------------------------------------------------------------------
def _motivo_do_tipo(tipo: str) -> str:
    if "macroenabled" in tipo or "vba" in tipo or "macrosheet" in tipo or "dialogsheet" in tipo:
        return MOTIVO_MACRO
    if tipo.endswith(".main+xml"):
        return MOTIVO_NAO_E_XLSX  # modelo ou outro formato do Office
    if "activex" in tipo:
        return "A planilha tem controles ActiveX."
    if "oleobject" in tipo:
        return "A planilha tem objetos incorporados."
    if "externallink" in tipo:
        return "A planilha tem vínculos com outros arquivos."
    if "connections" in tipo or "querytable" in tipo:
        return "A planilha tem conexões de dados externas."
    return MOTIVO_CONTEUDO_ATIVO


def _motivo_da_relacao(tipo: str) -> str:
    final = tipo.rsplit("/", 1)[-1].lower()
    if "vba" in final or "macrosheet" in final or "dialogsheet" in final:
        return MOTIVO_MACRO
    if final in ("hyperlink", "externallink", "externallinkpath", "attachedtemplate"):
        return "A planilha tem vínculos com outros arquivos ou endereços."
    if final in ("oleobject", "package", "control", "activexcontrol"):
        return "A planilha tem objetos incorporados."
    if final in ("connections", "querytable"):
        return "A planilha tem conexões de dados externas."
    return MOTIVO_CONTEUDO_ATIVO


def _local(nome: str) -> str:
    """O nome do elemento sem o prefixo de namespace (o que vale para o Excel é o nome local)."""
    return nome[nome.rfind(":") + 1:]


def _proibido(*_argumentos):
    raise _Recusa(MOTIVO_XML)


def _perigoso(texto: str, inicio: int = 0, fim: int = None) -> bool:
    """O trecho [inicio, fim) do texto (já com as letras ASCII em minúsculas) chama endereço externo, programa ou
    DDE, ou aponta para outro arquivo: o _PERIGO, a função entre parênteses e chamada em seguida, ou o nome passado
    como argumento depois do parêntese de uma chamada (colado a um nome, como em MAP(, ou a outro parêntese, como
    em LAMBDA(...)(: o Excel não aceita espaço entre a função e o parêntese). Assim "TV LED 32 (HD);Imagem" não
    conta. Buscas lineares no trecho."""
    fim = len(texto) if fim is None else fim
    if _PERIGO.search(texto, inicio, fim) or _VALOR_CHAMADO.search(texto, inicio, fim):
        return True
    chamada = _PARENTESE_DE_CHAMADA.search(texto, inicio, fim)
    return chamada is not None and _ARGUMENTO.search(texto, chamada.end() - 1, fim) is not None


# Para o pré-filtro: a sequência de letras e pontos depois de um colchete que chega ao alcance da busca (1 MB a
# partir do achado: uma só chamada em C sobre o arquivo inteiro seguraria o GIL). Chegar ao alcance conta como
# achado, porque a linha passa de 1 MB e é recusada do mesmo jeito.
_CORRIDA_DA_PASTA = re.compile(r"[\w.]*")


def _alcance(texto: str, posicao: int) -> int:
    return min(len(texto), posicao + LIMITE_DO_REGISTRO)


def _formula_perigosa(texto: str) -> bool:
    """O texto de uma fórmula do .xlsx, já sem o escape _xHHHH_, é perigoso."""
    if "_x" in texto:
        texto = _ESCAPE_DO_OOXML.sub(lambda escape: chr(int(escape.group(1), 16)), texto)
    return _perigoso(_minusculas(texto))


class _Leitor:
    """O expat de uma parte, sem processamento de namespace (o nome local sai do nome qualificado), que segue a
    codificação declarada (UTF-8 ou UTF-16) e decodifica entidades e CDATA; DOCTYPE e ENTITY são recusados.

    A memória do expat não cresce com o tamanho da parte, e sim com o aninhamento (cada elemento aberto fica na
    pilha), com os nomes distintos (guardados até o fim da parte) e com a marcação ainda incompleta (uma
    marcação ou um comentário fica inteiro no buffer até fechar). Por isso, a cada bloco, são conferidos os
    elementos abertos, os nomes e o trecho pendente. O tratador de abertura de quem usa o leitor conta os
    elementos abertos; o de fechamento só guarda o nome numa lista (em C, sem custo de chamada em Python), que
    é contada e esvaziada a cada bloco."""

    def __init__(self, namespaces: bool = False):
        self.nomes = {}
        self.fechamentos = []
        self._nomes_somados, self._tamanho_dos_nomes = 0, 0
        # Com namespaces, o nome chega como "namespace local" (para o [Content_Types].xml, onde o namespace conta).
        self.expat = expat.ParserCreate(namespace_separator=" " if namespaces else None, intern=self.nomes)
        self.expat.buffer_text = True
        self.expat.StartDoctypeDeclHandler = _proibido
        self.expat.EntityDeclHandler = _proibido
        self.expat.ExternalEntityRefHandler = _proibido
        self.expat.EndElementHandler = self.fechamentos.append

    def ler(self, pacote, parte, prazo, abertos) -> None:
        """A parte inteira, em blocos, com o prazo conferido a cada bloco; `abertos()` diz quantos elementos o
        tratador de abertura já contou. Os nomes distintos contam pela quantidade e pelo tamanho somado (um nome
        pode ter quase 1 MB, o teto da marcação)."""
        fechados, lidos = 0, 0
        with pacote.open(parte) as fluxo:
            while True:
                _no_prazo(prazo)
                bloco = fluxo.read(BLOCO)
                self.expat.Parse(bloco, not bloco)
                lidos += len(bloco)
                fechados += len(self.fechamentos)
                self.fechamentos.clear()
                if len(self.nomes) > self._nomes_somados:
                    self._tamanho_dos_nomes += sum(map(len, itertools.islice(self.nomes, self._nomes_somados, None)))
                    self._nomes_somados = len(self.nomes)
                if (abertos() - fechados > LIMITE_DE_ANINHAMENTO or len(self.nomes) > LIMITE_DE_NOMES
                        or self._tamanho_dos_nomes > LIMITE_DO_TAMANHO_DOS_NOMES
                        or lidos - self.expat.CurrentByteIndex > LIMITE_DE_MARCACAO):
                    raise _Recusa(MOTIVO_ESTRUTURA)
                if not bloco:
                    return


def _nome_da_parte(nome: str) -> str:
    """O nome de uma parte para comparar: sem a barra do começo, sem o escape %HH e em minúsculas (no OPC, o nome
    da parte não diferencia maiúsculas)."""
    return urllib.parse.unquote(nome.lstrip("/")).lower()


def _tipos_de_conteudo(pacote, parte, prazo):
    """(padrões por extensão, tipos por nome de parte), em minúsculas. Só valem Default e Override do namespace dos
    tipos de conteúdo, como no Excel (o de outro namespace, ignorado por ele, não pode trocar o tipo de uma parte
    aqui), e a extensão ou a parte repetida recusa o pacote."""
    padroes, especificos = {}, {}
    abertos = 0

    def ao_abrir(nome, atributos):
        nonlocal abertos
        abertos += 1
        espaco, _separador, local = nome.rpartition(" ")
        if espaco != _NAMESPACE_DOS_TIPOS or local not in ("Default", "Override"):
            return
        if local == "Default":
            destino, chave = padroes, str(atributos.get("Extension", "")).lower()
        else:
            destino, chave = especificos, _nome_da_parte(str(atributos.get("PartName", "")))
        if chave in destino:
            raise _Recusa(MOTIVO_NAO_E_XLSX)
        # Uma entrada por parte basta (o pacote tem até 2000 partes): milhões delas, cada uma guardada aqui,
        # levariam a triagem a centenas de MB sem passar por nenhuma outra trava.
        if len(padroes) + len(especificos) >= LIMITE_DE_ENTRADAS:
            raise _Recusa(MOTIVO_ESTRUTURA)
        destino[chave] = str(atributos.get("ContentType", "")).lower()

    leitor = _Leitor(namespaces=True)
    leitor.expat.StartElementHandler = ao_abrir
    leitor.ler(pacote, parte, prazo, lambda: abertos)
    return padroes, especificos


def _relacoes(pacote, parte, prazo) -> list:
    """(tipo, alvo) de cada relação da parte; recusa a relação externa ou que não seja de dados. A exceção é o
    hiperlink para um local do próprio arquivo (#Aba!A1), que o openpyxl grava como relação externa."""
    relacoes = []
    abertos = 0

    def ao_abrir(nome, atributos):
        nonlocal abertos
        abertos += 1
        if _local(nome) != "Relationship":
            return
        tipo = str(atributos.get("Type", ""))
        if str(atributos.get("TargetMode", "Internal")) != "Internal":
            if tipo == RELACAO_DE_HIPERLINK and _LOCAL_NO_PROPRIO_ARQUIVO.fullmatch(str(atributos.get("Target", ""))):
                return
            raise _Recusa("A planilha tem vínculos com outros arquivos ou endereços.")
        if tipo not in RELACOES_PERMITIDAS:
            raise _Recusa(_motivo_da_relacao(tipo))
        if len(relacoes) >= LIMITE_DE_ENTRADAS:
            raise _Recusa(MOTIVO_ESTRUTURA)
        relacoes.append((tipo, str(atributos.get("Target", ""))))

    leitor = _Leitor()
    leitor.expat.StartElementHandler = ao_abrir
    leitor.ler(pacote, parte, prazo, lambda: abertos)
    return relacoes


def _conferir_partes_xml(pacote, parte, prazo) -> None:
    """Objeto, controle e vínculo recusados; o texto de toda fórmula conferido já decodificado. Fora das fórmulas
    só o tratador de abertura roda em Python (o texto só é pedido dentro de uma fórmula, e o fechamento, fora
    dela, é o da lista do leitor), o que mantém a leitura de uma aba grande perto do custo do próprio expat."""
    leitor = _Leitor()
    analisador, fechamentos = leitor.expat, leitor.fechamentos
    abertos, na_formula, pedacos, tamanho = 0, 0, [], 0

    def ao_texto(texto):
        nonlocal tamanho
        pedacos.append(texto)
        tamanho += len(texto)
        if tamanho > LIMITE_DE_FORMULA:
            raise _Recusa("A planilha tem fórmula longa demais.")

    def ao_fechar_na_formula(nome):
        nonlocal na_formula
        fechamentos.append(nome)
        na_formula -= 1
        if na_formula:
            return
        analisador.CharacterDataHandler = None
        analisador.EndElementHandler = fechamentos.append
        if _formula_perigosa("".join(pedacos)):
            raise _Recusa(MOTIVO_FORMULA)

    def conferir_atributos(local, conferidos, atributos):
        for indice in range(0, len(atributos), 2):
            nome, valor = atributos[indice], atributos[indice + 1]
            nome = nome[nome.rfind(":") + 1:]
            if nome in conferidos and _formula_perigosa(valor):
                raise _Recusa(MOTIVO_FORMULA)
            if nome == "macro" and local in _FORMAS and valor.strip():
                raise _Recusa(MOTIVO_CONTEUDO_ATIVO)

    def ao_abrir(nome, atributos, especiais=_ESPECIAIS):
        nonlocal abertos, na_formula, tamanho
        abertos += 1
        local = nome[nome.rfind(":") + 1:]
        tipo = especiais.get(local)
        if tipo is _ATIVO:
            raise _Recusa(MOTIVO_CONTEUDO_ATIVO)
        if na_formula:
            na_formula += 1  # elemento dentro de uma fórmula: o texto dele conta como da fórmula
        elif tipo is _FORMULA:
            na_formula, tamanho = 1, 0
            pedacos.clear()
            analisador.CharacterDataHandler = ao_texto
            analisador.EndElementHandler = ao_fechar_na_formula
        elif tipo is not None and atributos:
            conferir_atributos(local, tipo, atributos)

    # Em lista, o expat não monta um dicionário de atributos por elemento (só os de cfvo e das formas são lidos).
    analisador.ordered_attributes = True
    analisador.StartElementHandler = ao_abrir
    leitor.ler(pacote, parte, prazo, lambda: abertos)


def _extensao_da_parte(nome: str) -> str:
    """A extensão como o OPC a entende: depois do último ponto do último segmento (".rels" tem a extensão rels,
    que o posixpath.splitext não vê)."""
    base = nome.rsplit("/", 1)[-1]
    return base.rpartition(".")[2] if "." in base else ""


def _alvo(origem: str, alvo: str) -> str:
    """O nome (comparável, como o de _nome_da_parte) da parte para onde a relação aponta, resolvido como o OPC
    resolve uma referência (RFC 3986): sem o escape %HH, com a barra invertida como barra, sem consulta nem
    fragmento, relativo à pasta da parte de origem (ou absoluto), e com os segmentos . e .. tirados (o .. acima da
    raiz fica na raiz). Assim "/../xl/aba.xml" ou "xl/%2E/aba.xml" chegam à mesma parte que o Excel abriria."""
    alvo = urllib.parse.unquote(alvo).replace("\\", "/").split("#", 1)[0].split("?", 1)[0].strip()
    pasta = "/" + posixpath.dirname(posixpath.dirname(origem))  # xl/_rels/workbook.xml.rels -> /xl
    caminho = posixpath.normpath(alvo if alvo.startswith("/") else posixpath.join(pasta, alvo))
    return caminho.lstrip("/").lower()


def conferir_xlsx(dados: bytes, prazo=None):
    """None se a planilha passa; senão, o motivo da recusa."""
    if dados.startswith(_OLE):
        return "Planilha protegida por senha ou do Excel antigo: salve como .xlsx, sem senha."
    if not dados.startswith(_ZIP):
        return MOTIVO_NAO_E_XLSX
    try:
        with zipfile.ZipFile(io.BytesIO(dados)) as pacote:
            return _conferir_pacote(pacote, prazo)
    except _Recusa as recusa:
        return recusa.motivo
    except Exception:  # noqa: BLE001 - ZIP ou XML corrompido (zlib, expat, zipfile): o arquivo é recusado
        return MOTIVO_NAO_E_XLSX


def _conferir_pacote(pacote, prazo):
    partes = pacote.infolist()
    if len(partes) > LIMITE_DE_PARTES:
        return "A planilha tem partes internas demais."
    total, nomes = 0, {}
    for parte in partes:
        nome = parte.filename
        minusculo = _nome_da_parte(nome)
        if parte.flag_bits & 0x1:
            return "A planilha tem partes cifradas."
        if parte.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            return MOTIVO_NAO_E_XLSX
        # O nome cru e o decodificado (%HH): sem barra no começo nem invertida, sem dois-pontos, sem os segmentos
        # . e .. e sem caractere de controle ou invisível.
        segmentos = minusculo.split("/")
        if (nome.startswith("/") or "\\" in nome + minusculo or ":" in nome + minusculo or ".." in segmentos
                or "." in segmentos or any(unicodedata.category(c) in ("Cc", "Cf") for c in nome + minusculo)):
            return "A planilha tem caminhos internos inválidos."
        if minusculo in nomes:
            return "A planilha tem partes internas repetidas."
        total += parte.file_size
        if total > LIMITE_DESCOMPACTADO:
            return "A planilha descompactada passa do limite."
        if parte.file_size > MEGABYTE and parte.file_size > TAXA_MAXIMA_DE_COMPRESSAO * max(parte.compress_size, 1):
            return "A planilha tem compressão suspeita."
        nomes[minusculo] = parte
    if "[content_types].xml" not in nomes or "_rels/.rels" not in nomes:
        return MOTIVO_NAO_E_XLSX
    padroes, especificos = _tipos_de_conteudo(pacote, nomes["[content_types].xml"], prazo)

    tipos = {}
    for minusculo, parte in nomes.items():
        if minusculo == "[content_types].xml" or minusculo.endswith("/"):
            continue
        tipo = especificos.get(minusculo) or padroes.get(_extensao_da_parte(minusculo))
        if not tipo:
            return MOTIVO_NAO_E_XLSX
        if tipo not in TIPOS_DE_PARTE_PERMITIDOS:
            return _motivo_do_tipo(tipo)
        tipos[minusculo] = tipo

    # A pasta de trabalho: o alvo da relação officeDocument, do tipo de planilha comum.
    pastas = [_alvo("_rels/.rels", alvo)
              for tipo, alvo in _relacoes(pacote, nomes["_rels/.rels"], prazo) if tipo == RELACAO_DA_PASTA_DE_TRABALHO]
    if len(pastas) != 1 or tipos.get(pastas[0]) != TIPO_PASTA_DE_TRABALHO:
        return MOTIVO_NAO_E_XLSX
    for minusculo, tipo in tipos.items():
        if minusculo.endswith(".rels"):
            # O papel de uma parte vem da relação que aponta para ela, e o tipo dela tem de ser o desse papel: uma
            # aba declarada como imagem ficaria sem leitura aqui e seria aberta como aba pelo Excel.
            # A relação de dados que não cai numa parte do pacote (fora dele, ou num nome que o Excel resolveria
            # de outro jeito) também recusa: é onde a leitura daqui e a do Excel poderiam divergir.
            for tipo_da_relacao, alvo in _relacoes(pacote, nomes[minusculo], prazo):
                destino = _alvo(minusculo, alvo)
                if destino not in tipos:
                    return MOTIVO_NAO_E_XLSX
                if tipos[destino] not in _TIPOS_DO_PAPEL[tipo_da_relacao]:
                    return MOTIVO_TIPO_TROCADO
        elif tipo not in _TIPOS_BINARIOS:
            _conferir_partes_xml(pacote, nomes[minusculo], prazo)
    return None


# ---------------------------------------------------------------------------------------------------------------
# .csv
# ---------------------------------------------------------------------------------------------------------------
def _texto(dados: bytes) -> str:
    """O texto como o Excel o lê. Com a marca do UTF-8 (BOM), UTF-8, trocando o byte inválido pelo caractere de
    substituição (a marca nunca fica grudada na primeira célula). Sem ela, UTF-8 ou, não sendo, cp1252, a
    codificação do Excel em português, com os 5 bytes que ele não define lidos como no Windows."""
    if dados.startswith(_BOM):
        return dados[len(_BOM):].decode("utf-8", errors="replace")
    try:
        return dados.decode("utf-8")
    except UnicodeDecodeError:
        return codecs.charmap_decode(dados, "strict", _CP1252)[0]


def _linhas(texto: str, com_quebra: bool = False):
    """As linhas do texto (já com as quebras em LF) uma a uma, sem montar a lista inteira na memória."""
    inicio, total = 0, len(texto)
    while inicio < total:
        fim = texto.find("\n", inicio)
        fim = total if fim == -1 else fim
        linha = texto[inicio:fim]
        yield linha + "\n" if com_quebra else linha
        inicio = fim + 1


def _minusculas(texto: str) -> str:
    """Só as letras ASCII em minúsculas (como no texto da busca das linhas candidatas)."""
    if texto.isascii():
        return texto.lower()
    return texto.encode("utf-8", "surrogatepass").lower().decode("utf-8", "surrogatepass")


def _celula_perigosa(celula: str) -> bool:
    """A célula da leitura com aspas começa como fórmula e é perigosa, lida sem as aspas que sobraram nela, como o
    texto da busca das linhas candidatas (tirar aspas só junta símbolos: nenhuma chamada válida some)."""
    return _INICIO_DE_FORMULA.match(celula) is not None and _perigoso(_minusculas(celula.replace('"', "")))


def _depois_da_linha(plano: str, posicao: int) -> int:
    """O começo da linha seguinte à da posição, ou -1 na última."""
    quebra = plano.find("\n", posicao)
    return -1 if quebra == -1 else quebra + 1


def _achados_da_funcao(plano: str, gatilho, chamadas: bytearray, prazo):
    """Em ordem, as posições onde a função é chamada (seguida de parêntese) ou vem seguida de vírgula, ponto e
    vírgula ou parêntese de fechamento (o nome passado como argumento) com um parêntese de chamada até 1 MB antes,
    ou, só com o parêntese de fechamento, até 1 MB depois (a função entre parênteses e chamada em seguida). O Excel
    limita a fórmula a 8.192 caracteres, e o registro aqui tem até 1 MB. No máximo uma por linha: depois de um
    achado, a busca segue da linha seguinte. O espaço depois do nome que chega ao fim da janela também conta, por
    cautela (o que vem depois dele só entra na janela seguinte)."""
    posicao, total, buscas = 0, len(plano), 0
    while posicao < total:
        janela = min(total, posicao + _JANELA_DA_BUSCA)
        achado = gatilho.search(plano, posicao, janela)
        buscas += 1
        if buscas % _CONFERIR_PRAZO_A_CADA == 0:
            _no_prazo(prazo)
        if achado is None:
            if janela == total:
                return
            posicao = janela - _SOBREPOSICAO_DA_JANELA
            continue
        fim = achado.group("fim")
        if (fim == "(" or (fim and _chamada_perto(chamadas, achado.start(), depois=fim == ")"))
                or (not fim and janela < total)):
            yield achado.start()
            seguinte = _depois_da_linha(plano, achado.start())
            if seguinte == -1:
                return
            posicao = seguinte
        else:
            posicao = achado.end()


def _achados_de_xleta(plano: str, prazo):
    """Em ordem, onde uma função perigosa vem depois de _xleta. (a função passada como valor, como o Excel a grava);
    no máximo uma por linha."""
    posicao, buscas = plano.find("_xleta."), 0
    while posicao != -1:
        if _NOME_PERIGOSO.match(plano, posicao + 7):
            yield posicao
            seguinte = _depois_da_linha(plano, posicao)
            posicao = -1 if seguinte == -1 else plano.find("_xleta.", seguinte)
        else:
            posicao = plano.find("_xleta.", posicao + 1)
        buscas += 1
        if buscas % _CONFERIR_PRAZO_A_CADA == 0:
            _no_prazo(prazo)


def _parentese_de_chamada_em(plano: str, posicao: int) -> bool:
    """Se o parêntese da posição é o de uma chamada, como o _PARENTESE_DE_CHAMADA o reconhece: colado a um nome
    ou a outro parêntese, ou depois de espaços que seguem uma das funções que recebem outra função. Espaços demais
    para olhar contam como chamada (por cautela)."""
    anterior = plano[posicao - 1] if posicao else ""
    if anterior in (")", "_") or anterior.isalnum():
        return True
    if not anterior.isspace():
        return False
    inicio_dos_espacos = posicao - 1
    while inicio_dos_espacos > 0 and plano[inicio_dos_espacos - 1].isspace():
        inicio_dos_espacos -= 1
        if posicao - inicio_dos_espacos > 64:
            return True
    return _FUNCAO_QUE_RECEBE_FUNCAO.search(plano, max(0, inicio_dos_espacos - 24), inicio_dos_espacos) is not None


def _blocos_com_chamada(plano: str, prazo) -> bytearray:
    """Os blocos de 64 KB do texto que têm algum parêntese de chamada (um por bloco basta: depois do primeiro, a
    busca salta para o bloco seguinte)."""
    marcas = bytearray(len(plano) // _BLOCO_DAS_CHAMADAS + 1)
    posicao, buscas = plano.find("("), 0
    while posicao != -1:
        if _parentese_de_chamada_em(plano, posicao):
            bloco = posicao // _BLOCO_DAS_CHAMADAS
            marcas[bloco] = 1
            posicao = plano.find("(", (bloco + 1) * _BLOCO_DAS_CHAMADAS)
        else:
            posicao = plano.find("(", posicao + 1)
        buscas += 1
        if buscas % _CONFERIR_PRAZO_A_CADA == 0:
            _no_prazo(prazo)
    return marcas


def _chamada_perto(chamadas: bytearray, posicao: int, depois: bool = False) -> bool:
    """Se há parêntese de chamada no bloco da posição ou nos de até 1 MB antes e, com `depois`, também nos de até
    1 MB depois (com um bloco de folga de cada lado)."""
    bloco = posicao // _BLOCO_DAS_CHAMADAS
    alcance = LIMITE_DO_REGISTRO // _BLOCO_DAS_CHAMADAS + 1
    return chamadas.find(1, max(0, bloco - alcance), bloco + 1 + (alcance if depois else 0)) != -1


def _achados_de_dde(plano: str, prazo):
    """Em ordem, as barras verticais cuja exclamação seguinte vem antes da barra seguinte (o \\|[^|!]*! do DDE,
    inclusive com quebra de linha no meio); no máximo uma por linha."""
    barra, exclamacao, buscas = plano.find("|"), plano.find("!"), 0
    while barra != -1 and exclamacao != -1:
        if exclamacao < barra:
            exclamacao = plano.find("!", barra)
            continue
        proxima = plano.find("|", barra + 1)
        if proxima == -1 or exclamacao < proxima:
            yield barra
            seguinte = _depois_da_linha(plano, barra)
            barra = -1 if seguinte == -1 else plano.find("|", seguinte)
        else:
            barra = proxima
        buscas += 1
        if buscas % _CONFERIR_PRAZO_A_CADA == 0:
            _no_prazo(prazo)


def _achados_de_apostrofo(plano: str, prazo):
    """Em ordem, o apóstrofo que fecha uma referência a outro arquivo ('!); no máximo um por linha."""
    posicao, buscas = plano.find("'!"), 0
    while posicao != -1:
        yield posicao
        seguinte = _depois_da_linha(plano, posicao)
        posicao = -1 if seguinte == -1 else plano.find("'!", seguinte)
        buscas += 1
        if buscas % _CONFERIR_PRAZO_A_CADA == 0:
            _no_prazo(prazo)


def _achados_de_colchete(plano: str, prazo):
    """Em ordem, o colchete que fecha o nome de uma pasta de trabalho antes da aba (]Aba!); no máximo um por
    linha."""
    posicao, buscas, total = plano.find("]"), 0, len(plano)
    while posicao != -1:
        alcance = _alcance(plano, posicao + 1)
        if (_PASTA_DE_TRABALHO.match(plano, posicao + 1, alcance)
                or (alcance < total and _CORRIDA_DA_PASTA.match(plano, posicao + 1, alcance).end() == alcance)):
            yield posicao
            seguinte = _depois_da_linha(plano, posicao)
            posicao = -1 if seguinte == -1 else plano.find("]", seguinte)
        else:
            posicao = plano.find("]", posicao + 1)
        buscas += 1
        if buscas % _CONFERIR_PRAZO_A_CADA == 0:
            _no_prazo(prazo)


def _linhas_candidatas(plano: str, prazo):
    """(número, de 0, e texto) de cada linha do texto sem aspas e com as letras ASCII em minúsculas onde alguma
    leitura do Excel pode ter fórmula perigosa. É um superconjunto: cada trecho que o _PERIGO, o _VALOR_CHAMADO ou
    o _ARGUMENTO acham numa célula começa num gatilho dentro dela, e cada busca, que só pula o resto da linha de um
    achado, passa por todas as linhas com gatilho. Tirar as aspas e trocar as maiúsculas não muda as quebras de
    linha, então a numeração vale para o texto original. A linha candidata com mais de 1 MB recusa o arquivo (seria
    conferida inteira)."""
    chamadas = _blocos_com_chamada(plano, prazo)
    fontes = [_achados_da_funcao(plano, gatilho, chamadas, prazo) for gatilho in _GATILHOS_DAS_FUNCOES]
    fontes += [_achados_de_xleta(plano, prazo), _achados_de_dde(plano, prazo), _achados_de_apostrofo(plano, prazo),
               _achados_de_colchete(plano, prazo)]
    numero, contado_ate, fim_da_ultima, total = 0, 0, -1, len(plano)
    for posicao in heapq.merge(*fontes):
        if posicao <= fim_da_ultima:
            continue  # na linha já entregue
        numero += plano.count("\n", contado_ate, posicao)
        contado_ate = posicao
        inicio = plano.rfind("\n", 0, posicao) + 1
        fim = plano.find("\n", posicao)
        fim = total if fim == -1 else fim
        if fim - inicio > LIMITE_DO_REGISTRO:
            raise _Recusa(MOTIVO_CSV_MALFORMADO)
        fim_da_ultima = fim
        yield numero, plano[inicio:fim]


def _leitura_sem_aspas(separador: str):
    """(separador, começo da primeira célula como fórmula, separador seguido de célula que começa como fórmula),
    sem tomar o próprio separador por espaço (a tabulação) nem por início de fórmula."""
    escapado = re.escape(separador)
    prefixo = r"(?:(?!" + escapado + r")[\s'])*"
    return (separador, re.compile(prefixo + r"[=+\-@]"),
            re.compile(escapado + r"(?=" + prefixo + r"(?!" + escapado + r")[=+\-@])"))


def _fim_da_celula(linha: str, separador: str, inicio: int) -> int:
    fim = linha.find(separador, inicio)
    return len(linha) if fim == -1 else fim


def _formula_na_linha(linha: str, leituras: list, prazo) -> bool:
    """Leitura sem aspas de uma linha candidata (do texto sem aspas e em minúsculas): a linha inteira (o
    separador do Excel pode não estar nela) e, para cada separador presente, as células que começam como
    fórmula, achadas e conferidas no lugar (sem partir a linha em pedaços). O prazo vale por separador e a cada
    4096 células."""
    if _INICIO_DE_FORMULA.match(linha) and _perigoso(linha):
        return True
    for separador, primeira, demais in leituras:
        if separador not in linha:
            continue
        _no_prazo(prazo)
        if primeira.match(linha) and _perigoso(linha, 0, _fim_da_celula(linha, separador, 0)):
            return True
        for contadas, achado in enumerate(demais.finditer(linha), 1):
            inicio = achado.end()
            if _perigoso(linha, inicio, _fim_da_celula(linha, separador, inicio)):
                return True
            if contadas % _CONFERIR_PRAZO_A_CADA == 0:
                _no_prazo(prazo)
    return False


def _formula_entre_aspas(texto: str, candidatas, separadores: list, prazo) -> bool:
    """Leitura com aspas, pelo leitor de CSV: um passe por separador presente e um do registro inteiro (por um
    separador que nunca está no texto; os ausentes dão todos essa leitura). Só os registros que tocam uma linha
    candidata são conferidos, e o passe para depois da última. Cada registro tem até 1 MB (um registro maior seria
    lido inteiro, célula a célula, na memória). O prazo vale por registro e a cada 4096 linhas ou células."""
    presentes = [separador for separador in separadores if separador in texto]
    lidos = 0

    def linhas():
        nonlocal lidos
        for numero, linha in enumerate(_linhas(texto, com_quebra=True), 1):
            lidos += len(linha)
            if lidos > LIMITE_DO_REGISTRO:
                raise _Recusa(MOTIVO_CSV_MALFORMADO)
            if numero % _CONFERIR_PRAZO_A_CADA == 0:
                _no_prazo(prazo)
            yield linha

    for separador in presentes + [_SEPARADOR_AUSENTE]:
        leitor = csv.reader(linhas(), delimiter=separador)
        indice, primeira, lidos = 0, 0, 0
        for registro in leitor:
            lidos = 0
            _no_prazo(prazo)
            ultima = leitor.line_num - 1
            while indice < len(candidatas) and candidatas[indice] < primeira:
                indice += 1
            if indice == len(candidatas):
                break
            if candidatas[indice] <= ultima:
                for contadas, celula in enumerate(registro, 1):
                    # Só a célula que pode começar como fórmula passa pela regra (o primeiro caractere decide, sem
                    # chamar a expressão regular em cada célula de um relatório comum).
                    primeiro = celula[:1]
                    if (primeiro in _COMECO_DE_FORMULA or primeiro.isspace()) and _celula_perigosa(celula):
                        return True
                    if contadas % _CONFERIR_PRAZO_A_CADA == 0:
                        _no_prazo(prazo)
            primeira = ultima + 1
    return False


def conferir_csv(dados: bytes, prazo=None):
    """None se o .csv passa; senão, o motivo da recusa."""
    if dados.startswith(_UTF16):
        return "O .csv está em UTF-16: salve como CSV (UTF-8 ou separado por ponto e vírgula)."
    inicio = dados[:64].lstrip(_BOM + b" \t\r\n")
    if inicio.startswith(_BINARIOS):
        return "O conteúdo não é um .csv: é um arquivo binário com a extensão trocada."
    if inicio.lower().startswith(_MARCACAO):
        return "O conteúdo não é um .csv: é uma página ou um XML com a extensão trocada."
    corpo = dados[:-1] if dados.endswith(b"\x1a") else dados  # o fim de arquivo de alguns sistemas
    if corpo.translate(None, _CONTROLES) != corpo:
        return "O .csv tem caracteres de controle: salve como CSV (UTF-8 ou separado por ponto e vírgula)."
    if b"\r" in corpo:
        # O Excel quebra a linha também no CR sozinho (o CSV do Mac antigo).
        corpo = corpo.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    texto = _texto(corpo)
    if prazo is not None and time.monotonic() > prazo:
        return MOTIVO_DEMORADO
    # O separador se procura nas primeiras linhas com conteúdo do começo do arquivo (64 KB bastam, e um arquivo
    # que começa com milhões de linhas em branco não prende a triagem aqui).
    primeiras = [linha for linha in _linhas(texto[:BLOCO]) if linha.strip()][:50]
    if not primeiras:
        if not texto.strip():
            return "O arquivo está vazio."
        return "O conteúdo não parece uma tabela: não há separador de colunas."
    declarado = _SEPARADOR_DECLARADO.fullmatch(primeiras[0])
    # As aspas e as letras ASCII mudam no texto da leitura sem aspas (tiradas, ou em minúsculas): como separador,
    # não teriam leitura nele. Nenhum exportador usa letra como separador.
    if declarado and (declarado.group(1) == '"' or declarado.group(1) in string.ascii_letters):
        return MOTIVO_CSV_MALFORMADO
    separadores = list(_SEPARADORES_DO_EXCEL) + ([declarado.group(1)] if declarado and declarado.group(1)
                                                 not in _SEPARADORES_DO_EXCEL else [])
    if not declarado and not any(separador in linha for linha in primeiras for separador in separadores):
        return "O conteúdo não parece uma tabela: não há separador de colunas."
    com_aspas = '"' in texto
    try:
        # O texto sem aspas e com só as letras ASCII em minúsculas, tirado do texto já decodificado (os mesmos
        # caracteres das outras leituras) pelos bytes UTF-8, por blocos: as duas trocas em C, sem reservar os 12
        # bytes por caractere do str.lower() de um texto com acento, e sem a decodificação de um texto inteiro
        # reservar memória pelo número de bytes (um caractere de 3 bytes, como o de substituição, triplicaria). O
        # texto sai da memória antes da junção; a leitura com aspas, quando é precisa, decodifica de novo.
        blocos = []
        for inicio_do_bloco in range(0, len(texto), _BLOCO_DO_PLANO):
            blocos.append(texto[inicio_do_bloco:inicio_do_bloco + _BLOCO_DO_PLANO].encode("utf-8", "surrogatepass")
                          .replace(b'"', b"").lower().decode("utf-8", "surrogatepass"))
            _no_prazo(prazo)
        del texto
        plano = "".join(blocos)
        del blocos
        leituras = [_leitura_sem_aspas(separador) for separador in separadores]
        candidatas = array("i")
        for numero, linha in _linhas_candidatas(plano, prazo):
            _no_prazo(prazo)
            if _formula_na_linha(linha, leituras, prazo):
                return MOTIVO_FORMULA
            candidatas.append(numero)
        del plano
        if candidatas and com_aspas:
            texto = _texto(corpo)
            _no_prazo(prazo)
            if _formula_entre_aspas(texto, candidatas, separadores, prazo):
                return MOTIVO_FORMULA
    except _Recusa as recusa:
        return recusa.motivo
    except csv.Error:
        return MOTIVO_CSV_MALFORMADO
    return None
