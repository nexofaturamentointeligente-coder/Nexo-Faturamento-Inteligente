"""Baia de Segurança (Security Gatekeeper): triagem de todo relatório enviado, antes do motor
de leitura e do banco.

O trabalho fica dividido em duas metades, porque o processo do servidor não interpreta
planilhas (SEC-005):

No processo do servidor (só biblioteca padrão, memória constante):
  - teto de tamanho na cópia em blocos: o menor entre o ETLLimits e 50 MiB (teto_bytes);
  - assinatura dos primeiros bytes (inspecionar_arquivo). Passam só XLSX (ZIP), XLS (OLE2 com
    o fluxo Workbook) e texto delimitado (CSV/TXT), e o conteúdo precisa bater com a
    extensão. Executáveis (PE, ELF, Mach-O, WebAssembly), scripts (shebang, lote do
    Windows), HTML/XML/SVG, PDF, RTF, imagens, mídia, outros compactados, documentos OLE e
    qualquer conteúdo binário ficam de fora;
  - quarentena: o arquivo recusado fica só no diretório temporário da requisição e some com
    ele. O log leva o motivo, o tamanho e o SHA-256, nunca o conteúdo, o nome do arquivo ou
    o caminho.

No processo isolado do ETL (etl_worker, operação "validate", função triar):
  - a validação estrutural e de orçamento de sempre (etl_inputs);
  - o inventário do XLSX por lista do que é permitido: o próprio arquivo só segue quando toda
    parte é de dados (pasta, abas, estilos, textos, tema, propriedades), as relações são
    internas e de dados, e o parser XML não acha fórmula, nome definido, objeto, controle ou
    vínculo DDE, em qualquer codificação. Qualquer outra parte, em qualquer caminho (macros,
    ActiveX, OLE, vínculos, conexões, binários), leva à cópia;
  - a neutralização de fórmula em célula (CSV/Excel injection), a mesma que o motor aplica
    em toda leitura (etl_inputs.read_raw);
  - a estrutura mínima de relatório de PDV;
  - a liberação: o próprio arquivo, quando não havia nada a neutralizar; senão, uma cópia só
    com os valores (XLSX de uma aba, ou CSV), conferida por uma segunda leitura com o mesmo
    motor. Se a cópia não reproduz exatamente o que o motor leria do original, o arquivo é
    recusado: nada com conteúdo ativo é gravado e nenhum número muda em silêncio.

Dois destinos, duas listas de formatos:
  - Arquivo GUARDADO (o envio das análises mensais, etl_uploads): só .csv e .xlsx
    (config.ALLOWED_EXTENSIONS), porque a liberação grava o próprio arquivo ou a cópia só com valores
    nesses dois formatos. O XLS (BIFF) é reconhecido e recusado com a orientação de salvar como .xlsx
    ou .csv. O TXT é lido como CSV (texto delimitado); habilitá-lo é decisão de produto.
  - LEITURA DIRETA (Raio-X e Avaliação de Cenário, inspecionar_arquivo(..., leitura_direta=True)): o
    arquivo é lido no processo isolado e descartado, sem cópia a liberar. Além de .csv e .xlsx, passam
    o .xls verdadeiro do Excel 97-2003 (OLE2 com o fluxo Workbook) e a Planilha XML 2003 (.xml com o
    elemento Workbook da planilha, sem DOCTYPE nem entidade). Os leitores desses dois formatos
    (etl_biff e etl_planilha_xml) só tiram valores de célula: a fórmula entra pelo valor guardado, e
    macro, objeto embutido e vínculo ficam sem abrir. É o expurgo em memória: nada além de valores sai
    do arquivo, e a leitura do motor neutraliza as células como nos outros formatos. HTML com a
    extensão .xls (a "exportação para Excel" de sistemas web) continua recusado.
"""

from __future__ import annotations

import codecs
import csv
import errno
import logging
import math
import os
from pathlib import Path
import re
import stat
import struct
import zipfile
from xml.parsers import expat
from xml.sax.saxutils import escape

from etl_inputs import ETLInputError, SEPARATORS, read_raw

logger = logging.getLogger(__name__)

MENSAGEM_PADRAO = "Arquivo fora do padrão aceito para auditoria."
# Teto da especificação. O ETLLimits já é menor (20 MiB, ver etl_limits._HARD_CAPS): este
# valor só impede que um teto futuro mais alto passe dos 50 MiB.
TETO_ABSOLUTO_BYTES = 50 * 1024 * 1024
EXTENSOES_RECONHECIDAS = frozenset({"xlsx", "xls", "csv", "txt"})
# Formato em que o motor (etl_inputs) lê cada extensão que ele sabe ler.
FORMATO_DE_LEITURA = {"xlsx": "xlsx", "csv": "csv", "txt": "csv"}
# Nome da cópia neutralizada, ao lado do arquivo, no diretório temporário do processo pai.
NOME_LIBERADO = "liberado"
_AMOSTRA = 8192

# Formatos a mais da leitura direta (Raio-X e Avaliação de Cenário): o arquivo não é guardado.
FORMATOS_DE_LEITURA_DIRETA = frozenset({"xls", "xml"})
_ESPACO_DA_PLANILHA_XML = b"urn:schemas-microsoft-com:office:spreadsheet"
_RE_WORKBOOK_XML = re.compile(rb"<(?:[a-z0-9_.-]+:)?workbook[\s>]")

ORIENTACAO_XLS = "Salve a planilha como .xlsx ou .csv e envie novamente."
# O .xls que não é a planilha do Excel 97-2003: a página HTML, a Planilha XML ou o texto delimitado que
# muitos sistemas exportam com a extensão .xls, o .xlsx renomeado, o Excel antigo sem contêiner, outro
# documento do Office. A frase não afirma que o arquivo não é planilha: o motivo documento_ole também sai
# para a planilha verdadeira em que a Baia não acha a pasta de trabalho (ver _ole_e_planilha).
ORIENTACAO_XLS_ILEGIVEL = ("Não foi possível reconhecer o arquivo .xls como planilha do Excel 97-2003 (ele pode ser "
                           "uma página HTML ou um texto com a extensão trocada). Abra no Excel, salve como .xlsx "
                           "e envie novamente.")
_MOTIVOS_DO_XLS_ILEGIVEL = frozenset({"marcacao", "binario", "documento_ole", "disfarce_texto_como_xls",
                                      "disfarce_zip_como_xls"})
ORIENTACAO_XML = ("O .xml precisa ser uma Planilha XML 2003 do Excel. Salve a planilha como .xlsx e envie "
                  "novamente.")
ORIENTACAO_EXTENSAO = "O conteúdo não corresponde à extensão do arquivo."
ORIENTACAO_ESTRUTURA = ("O relatório precisa ter colunas legíveis de produto e de quantidade ou valor, "
                        "como o PDV exporta.")
ORIENTACAO_TAMANHO = "Divida o relatório em arquivos menores, por período, e envie novamente."
ORIENTACAO_NEUTRALIZAR = ("Salve a planilha só com os valores, sem fórmulas, macros ou objetos "
                          "incorporados, e envie novamente.")


class SecurityGatekeeperError(ETLInputError):
    """Recusa da Baia. A mensagem é sempre a padrão, mais uma orientação fixa quando há uma;
    `motivo` serve ao log e aos testes. Ele vai no código ("quarentena:<motivo>") para
    atravessar o processo isolado, que só devolve código e mensagem."""

    PREFIXO = "quarentena:"

    def __init__(self, motivo: str, orientacao: str | None = None, *, rotulo: str | None = None):
        mensagem = MENSAGEM_PADRAO
        if rotulo:
            mensagem = f"{rotulo}: {mensagem[0].lower()}{mensagem[1:]}"
        if orientacao:
            mensagem = f"{mensagem} {orientacao}"
        super().__init__(self.PREFIXO + motivo, mensagem)
        self.motivo = motivo
        self.orientacao = orientacao


def motivo_de_quarentena(erro) -> str | None:
    """O motivo de uma recusa da Baia, também quando ela veio do processo isolado."""
    codigo = getattr(erro, "code", None)
    if isinstance(codigo, str) and codigo.startswith(SecurityGatekeeperError.PREFIXO):
        return codigo[len(SecurityGatekeeperError.PREFIXO):]
    return None


def teto_bytes(limits) -> int:
    """Teto da leitura em blocos: o do ETLLimits, nunca acima de 50 MiB."""
    return min(int(limits.max_file_bytes), TETO_ABSOLUTO_BYTES)


# ---------------------------------------------------------------------------
# Primeira triagem: assinatura dos primeiros bytes (processo do servidor)
# ---------------------------------------------------------------------------
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ASSINATURAS = (
    (b"\x7fELF", "executavel_elf"),
    (b"\xfe\xed\xfa\xce", "executavel_macho"), (b"\xfe\xed\xfa\xcf", "executavel_macho"),
    (b"\xce\xfa\xed\xfe", "executavel_macho"), (b"\xcf\xfa\xed\xfe", "executavel_macho"),
    (b"\xca\xfe\xba\xbe", "executavel_macho"),  # binário universal do macOS ou classe Java
    (b"\x00asm", "executavel_wasm"),
    (b"L\x00\x00\x00\x01\x14\x02\x00", "atalho_windows"),
    (b"\x89PNG\r\n\x1a\n", "imagem"), (b"\xff\xd8\xff", "imagem"),
    (b"II*\x00", "imagem"), (b"MM\x00*", "imagem"),
    (b"Rar!\x1a\x07", "compactado"), (b"7z\xbc\xaf\x27\x1c", "compactado"),
    (b"\x1f\x8b", "compactado"), (b"\xfd7zXZ\x00", "compactado"), (b"\x28\xb5\x2f\xfd", "compactado"),
    (b"PK\x05\x06", "compactado"), (b"PK\x07\x08", "compactado"),
)
# Formatos textuais reconhecíveis pelo início, depois de BOM e espaços (comparação em minúsculas).
_INICIOS_PROIBIDOS = (
    (b"<", "marcacao"),  # HTML, XML, SVG, XHTML e a planilha XML do Excel 2003
    (b"#!", "script"), (b"@echo", "script"),
    (b"%pdf-", "pdf"), (b"{\\rtf", "rtf"), (b"gif87a", "imagem"), (b"gif89a", "imagem"),
)
# Controles comuns em texto exportado: TAB, quebras, tabulação vertical, avanço de página e o
# fim de arquivo do DOS. NUL é tratado à parte: o leitor do ETL o descarta (texto UTF-16).
_CONTROLES_DE_TEXTO = frozenset(b"\t\n\r\x0b\x0c\x1a")


def _e_executavel_pe(amostra: bytes) -> bool:
    if amostra[:2] != b"MZ" or len(amostra) < 0x40:
        return False
    deslocamento = struct.unpack_from("<I", amostra, 0x3C)[0]
    return deslocamento + 4 <= len(amostra) and amostra[deslocamento:deslocamento + 4] == b"PE\x00\x00"


def _e_texto(amostra: bytes) -> bool:
    """Texto que o leitor de CSV aceitaria: sem NUL em excesso e com até 1% de controles."""
    sem_nul = amostra.replace(b"\x00", b"")
    # UTF-16 perde metade dos bytes e UTF-32 três quartos; mais que isso é binário.
    if not sem_nul or len(sem_nul) * 4 < len(amostra):
        return False
    controles = sum(1 for byte in sem_nul if (byte < 0x20 and byte not in _CONTROLES_DE_TEXTO) or byte == 0x7F)
    return controles * 100 <= len(sem_nul)


def _inicio_textual(amostra: bytes) -> bytes:
    for bom in (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE, codecs.BOM_UTF8,
                codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        if amostra.startswith(bom):
            amostra = amostra[len(bom):]
            break
    return amostra.replace(b"\x00", b"").lstrip(b" \t\r\n\x0b\x0c").lower()[:64]


def _ole_e_planilha(caminho) -> bool:
    """O documento OLE2 é uma pasta de trabalho do Excel 97-2003? Lê só o cabeçalho e o primeiro
    setor do diretório, onde ficam o fluxo Workbook (ou Book, no Excel 5/95)."""
    try:
        with open(caminho, "rb") as arquivo:
            cabecalho = arquivo.read(512)
            if len(cabecalho) < 512 or cabecalho[:8] != _OLE or cabecalho[0x1C:0x1E] != b"\xfe\xff":
                return False
            expoente = struct.unpack_from("<H", cabecalho, 0x1E)[0]
            primeiro = struct.unpack_from("<I", cabecalho, 0x30)[0]
            if expoente not in (9, 12) or primeiro >= 0xFFFFFFFA:
                return False
            tamanho = 1 << expoente
            arquivo.seek((primeiro + 1) * tamanho)
            setor = arquivo.read(tamanho)
    except OSError:
        return False
    for inicio in range(0, len(setor) - 127, 128):
        entrada = setor[inicio:inicio + 128]
        comprimento = struct.unpack_from("<H", entrada, 0x40)[0]
        if entrada[0x42] != 2 or not 2 <= comprimento <= 64 or comprimento % 2:
            continue
        if entrada[:comprimento - 2].decode("utf-16-le", "replace").lower() in ("workbook", "book"):
            return True
    return False


def identificar_conteudo(amostra: bytes, caminho=None) -> str:
    """Natureza do conteúdo pelos primeiros bytes: "zip", "xls" ou "texto". O resto é recusado,
    com o motivo no código do erro."""
    if not amostra:
        raise SecurityGatekeeperError("vazio")
    if amostra.startswith(b"PK\x03\x04"):
        return "zip"
    if amostra.startswith(_OLE):
        if caminho is not None and _ole_e_planilha(caminho):
            return "xls"
        raise SecurityGatekeeperError("documento_ole")
    if _e_executavel_pe(amostra):
        raise SecurityGatekeeperError("executavel_pe")
    for assinatura, motivo in _ASSINATURAS:
        if amostra.startswith(assinatura):
            raise SecurityGatekeeperError(motivo)
    if amostra[:4] == b"RIFF":
        raise SecurityGatekeeperError("midia")
    if not _e_texto(amostra):
        raise SecurityGatekeeperError("binario")
    inicio = _inicio_textual(amostra)
    for marcador, motivo in _INICIOS_PROIBIDOS:
        if inicio.startswith(marcador):
            raise SecurityGatekeeperError(motivo)
    return "texto"


def _e_planilha_xml(amostra: bytes) -> bool:
    """A amostra é o começo de uma Planilha XML 2003 (SpreadsheetML)? Texto que começa pela declaração XML
    ou pelo próprio elemento, com o elemento Workbook no espaço de nomes da planilha e sem DOCTYPE nem
    declaração de entidade (o leitor recusa as duas em qualquer ponto do arquivo). HTML, SVG e qualquer
    outro XML ficam de fora."""
    if not _e_texto(amostra) or not _inicio_textual(amostra).startswith((b"<?xml", b"<workbook", b"<ss:workbook")):
        return False
    corpo = amostra.replace(b"\x00", b"").lower()
    return (b"<!doctype" not in corpo and b"<!entity" not in corpo and _ESPACO_DA_PLANILHA_XML in corpo
            and _RE_WORKBOOK_XML.search(corpo) is not None)


def inspecionar_arquivo(caminho, extensao: str, *, rotulo: str | None = None, leitura_direta: bool = False) -> str:
    """Primeira triagem, no processo do servidor, lendo só os primeiros bytes do arquivo já
    gravado com teto. Devolve o formato em que o motor deve lê-lo ("xlsx" ou "csv"; na leitura
    direta, também "xls" e "xml").

    `leitura_direta`: o arquivo vai ser lido no processo isolado e descartado (Raio-X e Avaliação de
    Cenário), sem cópia a liberar. Só aí passam o .xls verdadeiro e a Planilha XML 2003."""
    extensao = (extensao or "").lower().lstrip(".")
    try:
        if extensao not in EXTENSOES_RECONHECIDAS and not (leitura_direta and extensao in FORMATOS_DE_LEITURA_DIRETA):
            raise SecurityGatekeeperError("extensao_nao_aceita")
        with open(caminho, "rb") as arquivo:
            amostra = arquivo.read(_AMOSTRA)
        if extensao == "xml":
            if not amostra:
                raise SecurityGatekeeperError("vazio")
            if not _e_planilha_xml(amostra):
                raise SecurityGatekeeperError("xml_nao_e_planilha", ORIENTACAO_XML)
            return "xml"
        natureza = identificar_conteudo(amostra, caminho)
        if natureza == "xls":
            if leitura_direta and extensao == "xls":
                return "xls"  # Excel 97-2003 verdadeiro: lido pelo etl_biff, só os valores das células
            # Arquivo guardado: reconhecido, mas a liberação só grava .csv e .xlsx.
            raise SecurityGatekeeperError("xls_binario" if extensao == "xls" else "xls_disfarcado", ORIENTACAO_XLS)
        esperado = "zip" if FORMATO_DE_LEITURA.get(extensao) == "xlsx" else "texto"
        if extensao == "xls" or natureza != esperado:
            raise SecurityGatekeeperError(f"disfarce_{natureza}_como_{extensao}", ORIENTACAO_EXTENSAO)
        return FORMATO_DE_LEITURA[extensao]
    except SecurityGatekeeperError as erro:
        orientacao = erro.orientacao
        if extensao == "xls" and erro.motivo in _MOTIVOS_DO_XLS_ILEGIVEL:
            orientacao = ORIENTACAO_XLS_ILEGIVEL
        raise SecurityGatekeeperError(erro.motivo, orientacao, rotulo=rotulo) from None


def ler_liberado(entrada, extensao: str, teto: int) -> bytes | None:
    """Bytes da cópia neutralizada que triar() deixou ao lado do arquivo, ou None quando o
    próprio arquivo foi liberado. A cópia passa de novo pelo teto e pela assinatura."""
    destino = Path(entrada).with_name(f"{NOME_LIBERADO}.{extensao}")
    try:
        info = destino.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= teto:
        raise SecurityGatekeeperError("liberado_invalido", ORIENTACAO_NEUTRALIZAR)
    # Sem seguir link e conferindo o próprio descritor aberto: nada troca o arquivo no caminho.
    descritor = os.open(destino, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descritor, "rb") as arquivo:
        aberto = os.fstat(arquivo.fileno())
        if not stat.S_ISREG(aberto.st_mode) or (aberto.st_dev, aberto.st_ino) != (info.st_dev, info.st_ino):
            raise SecurityGatekeeperError("liberado_invalido", ORIENTACAO_NEUTRALIZAR)
        conteudo = arquivo.read(teto + 1)
    if len(conteudo) > teto or identificar_conteudo(conteudo[:_AMOSTRA]) != ("zip" if extensao == "xlsx" else "texto"):
        raise SecurityGatekeeperError("liberado_invalido", ORIENTACAO_NEUTRALIZAR)
    return conteudo


_CODIGO_SEGURO = re.compile(r"[^a-z0-9_:]")


def registrar_recusa(erro, *, sha256: str, tamanho: int, extensao: str, origem: str) -> None:
    """Log da quarentena: motivo, tamanho e SHA-256 do arquivo recusado. Nunca o conteúdo, o
    nome do arquivo ou o caminho temporário."""
    codigo = _CODIGO_SEGURO.sub("", str(getattr(erro, "code", "desconhecido")).lower())[:64]
    if codigo == "busy":
        return  # fila ocupada não diz nada do arquivo
    logger.warning("Baia de Segurança: arquivo recusado (origem=%s, motivo=%s, extensao=%s, bytes=%d, sha256=%s).",
                   origem, codigo, _CODIGO_SEGURO.sub("", extensao.lower())[:8], tamanho, sha256)


def registrar_liberacao(relatorio, *, sha256_original: str, sha256_liberado: str, origem: str) -> None:
    """Log da cópia neutralizada que substituiu o arquivo enviado."""
    relatorio = relatorio if isinstance(relatorio, dict) else {}
    achados = ",".join(_CODIGO_SEGURO.sub("", str(item)) for item in relatorio.get("achados", [])[:16]) or "-"
    celulas = relatorio.get("celulas_neutralizadas", 0)
    logger.info("Baia de Segurança: cópia neutralizada liberada (origem=%s, achados=%s, celulas=%s, "
                "sha256_original=%s, sha256_liberado=%s).", origem, achados,
                celulas if isinstance(celulas, int) else "?", sha256_original, sha256_liberado)


# ---------------------------------------------------------------------------
# Neutralização de fórmula em célula (CSV/Excel injection)
# ---------------------------------------------------------------------------
# Restos de BOM que o leitor não consumiu (UTF-8 lido como cp1252, UTF-16 sem conversão): uma
# planilha que reconheça o BOM ao abrir a cópia vê a célula a partir do que vem depois dele.
_RESTOS_DE_BOM = ("﻿", "ï»¿", "ÿþ", "þÿ")
_INICIOS_SUSPEITOS = frozenset("=+-@\t\r\n ﻿ïÿþ")
_BRANCOS = " \t\r\n"
# Depois de um + ou - inicial, sinais que só uma fórmula usa: operadores, referências externas
# (! e [ ]), DDE (|), texto entre aspas e chamada de função (nome colado ao parêntese).
_SINAL_DE_FORMULA = re.compile(r"""[=|!"&*/^<>\[\]{};:@'+\-]|[^\W\d][\w.]*\(""")
_SO_SINAIS = re.compile(r"[+\-\s]+")
_NOTACAO_CIENTIFICA = re.compile(r"[+-]\s*\d+(?:[.,]\d+)?[eE][+-]?\d+\s*")
# Um trecho dentro da célula que vira célula própria quando outro programa separa o arquivo por
# outro separador (";" num CSV de ",", por exemplo).
_SEPARADOR_INTERNO = re.compile(r"[;,\t|][ \t\r\n]*(?=[=+\-@])")


def _formula_no_inicio(nucleo: str) -> bool:
    """O trecho, lido como uma célula a partir do primeiro caractere, seria fórmula?"""
    if not nucleo:
        return False
    if nucleo[0] in "=@":
        return True
    # O "-" final é o sinal contábil do PDV ("10,00-"), não operador.
    return bool(nucleo[0] in "+-" and _SINAL_DE_FORMULA.search(nucleo.rstrip(" -"), 1)
                and not _SO_SINAIS.fullmatch(nucleo) and not _NOTACAO_CIENTIFICA.fullmatch(nucleo))


def _neutralizar_trechos(texto: str) -> str:
    """Põe o apóstrofo depois de cada separador interno seguido de fórmula ("ok;'=cmd…"). Cada
    trecho é julgado até a próxima ocorrência do mesmo separador: é a célula que um programa
    que separe o arquivo por ele veria."""
    partes, ultimo = [], 0
    for achado in _SEPARADOR_INTERNO.finditer(texto):
        fim = achado.end()
        if _formula_no_inicio(texto[fim:].split(texto[achado.start()], 1)[0]):
            partes += [texto[ultimo:fim], "'"]
            ultimo = fim
    return "".join(partes) + texto[ultimo:] if partes else texto


def neutralizar_celula(valor):
    """Texto de célula que uma planilha não executa como fórmula.

    Começo em =, @, TAB, CR ou LF, ou em + e - seguidos de algo que só fórmula tem, ganha o
    apóstrofo da recomendação OWASP; TAB, CR e LF iniciais viram espaço. O mesmo vale depois
    de um resto de BOM e depois de cada separador (; , TAB |) dentro da célula. Números com
    sinal ("-12,50", "-1 UN", "+5%", "-R$ 3,00", "-1,5E+03") ficam como estão; quando um
    valor com sinal recebe o apóstrofo, os dois leitores do ETL (quantidade e dinheiro) o
    ignoram, e a chave do produto ignora a pontuação: nenhum número muda. Não mexe no que não
    é texto e é idempotente.
    """
    if type(valor) is not str or not valor:
        return valor
    for resto in _RESTOS_DE_BOM:
        if valor.startswith(resto) and len(valor) > len(resto):
            return resto + neutralizar_celula(valor[len(resto):])
    texto = valor
    if valor[0] in _INICIOS_SUSPEITOS:
        nucleo = valor.lstrip(_BRANCOS)
        inicio = len(valor) - len(nucleo)
        if valor[:inicio].strip(" "):
            texto = " " * inicio + nucleo
        if _formula_no_inicio(nucleo):
            texto = "'" + texto
    return _neutralizar_trechos(texto)


def _talvez_ativa(valor) -> bool:
    return type(valor) is str and bool(valor) and (
        valor[0] in _INICIOS_SUSPEITOS or _SEPARADOR_INTERNO.search(valor) is not None)


def neutralizar_matriz(matriz):
    """Aplica neutralizar_celula à matriz lida, no lugar, e anota em attrs quantas células
    mudaram ("celulas_neutralizadas"). Uma coluna alterada é trocada de uma vez só."""
    if matriz is None or matriz.empty:
        return matriz
    import pandas as pd
    alteradas = 0
    for posicao in range(matriz.shape[1]):
        valores = matriz.iloc[:, posicao].tolist()
        mudancas = 0
        for indice, valor in enumerate(valores):
            if _talvez_ativa(valor):
                novo = neutralizar_celula(valor)
                if novo != valor:
                    valores[indice] = novo
                    mudancas += 1
        if mudancas:
            matriz.isetitem(posicao, pd.Series(valores, index=matriz.index, dtype=matriz.dtypes.iloc[posicao]))
            alteradas += mudancas
    matriz.attrs["celulas_neutralizadas"] = alteradas
    return matriz


# ---------------------------------------------------------------------------
# Segunda triagem: processo isolado do ETL
# ---------------------------------------------------------------------------
# O que um XLSX só com dados tem: o que o Excel, o LibreOffice e o openpyxl gravam numa pasta
# comum. O original só é liberado quando TODA parte está nesta lista (o pacote acha as partes
# pelas relações, em qualquer caminho); qualquer outra leva à cópia só com valores.
_PARTES_PASSIVAS = re.compile(
    r"\[content_types\]\.xml|_rels/\.rels|docprops/(?:core|app|custom)\.xml|xl/workbook\.xml"
    r"|xl/_rels/workbook\.xml\.rels|xl/worksheets/sheet[1-9][0-9]{0,4}\.xml|xl/styles\.xml"
    r"|xl/sharedstrings\.xml|xl/theme/theme[1-9][0-9]{0,2}\.xml")
_RELACOES_PASSIVAS = frozenset("http://schemas.openxmlformats.org/" + tipo for tipo in (
    "officeDocument/2006/relationships/officeDocument",
    "package/2006/relationships/metadata/core-properties",
    "officeDocument/2006/relationships/extended-properties",
    "officeDocument/2006/relationships/custom-properties",
    "officeDocument/2006/relationships/worksheet",
    "officeDocument/2006/relationships/styles",
    "officeDocument/2006/relationships/sharedStrings",
    "officeDocument/2006/relationships/theme",
))
# Só para o log dizer o que havia: os caminhos de costume das partes ativas conhecidas.
_PARTES_ATIVAS = (
    ("macro_vba", re.compile(r"(?:^|/)vbaproject[^/]*\.bin$|(?:^|/)vbadata\.xml$")),
    ("macro_xlm", re.compile(r"^xl/macrosheets/")),
    ("macro_dialogo", re.compile(r"^xl/dialogsheets/")),
    ("activex", re.compile(r"^xl/activex/")),
    ("objeto_ole", re.compile(r"^xl/embeddings/")),
    ("vinculo_externo", re.compile(r"^xl/externallinks/")),
    ("conexao_dados", re.compile(r"^xl/connections\.xml$|^xl/querytables/|^xl/model/")),
    ("interface_personalizada", re.compile(r"^customui/")),
    ("controle_formulario", re.compile(r"^xl/ctrlprops/")),
    ("complemento_web", re.compile(r"^xl/webextensions/")),
)
# Elementos que só fórmula, nome definido, objeto, controle ou vínculo DDE têm, em qualquer
# namespace (inclusive as extensões x14 e xm).
_ELEMENTOS_ATIVOS = {
    "f": "formulas", "formula": "formulas", "formula1": "formulas", "formula2": "formulas",
    "definedName": "nomes_definidos", "oleObject": "objeto_ole", "oleObjects": "objeto_ole",
    "control": "controle_formulario", "controls": "controle_formulario",
    "ddeLink": "vinculo_externo", "oleLink": "vinculo_externo",
}
_LIMITE_TIPOS = 1 << 20


class _Encontrado(Exception):
    """Interrompe a leitura de uma parte assim que algo ativo aparece."""


def _ler_xml(pacote, nome, ao_abrir) -> None:
    """Lê uma parte XML em fluxo com o expat, que segue a codificação declarada (UTF-8 ou
    UTF-16), chamando ao_abrir(nome local, atributos) em cada elemento."""
    leitor = expat.ParserCreate(namespace_separator="}")
    leitor.StartElementHandler = lambda elemento, atributos: ao_abrir(elemento.rsplit("}", 1)[-1], atributos)

    def proibido(*_):
        raise SecurityGatekeeperError("xml_proibido")

    leitor.StartDoctypeDeclHandler = proibido
    leitor.EntityDeclHandler = proibido
    leitor.ExternalEntityRefHandler = proibido
    try:
        with pacote.open(nome) as membro:
            while True:
                bloco = membro.read(65536)
                leitor.Parse(bloco, not bloco)
                if not bloco:
                    return
    except _Encontrado:
        return


def _elementos_ativos(pacote, nome) -> set:
    achados = set()

    def ao_abrir(local, _atributos):
        achado = _ELEMENTOS_ATIVOS.get(local)
        if achado:
            achados.add(achado)
            raise _Encontrado

    _ler_xml(pacote, nome, ao_abrir)
    return achados


def _relacoes_passivas(pacote, nome) -> bool:
    """Toda relação é de dados (pasta, aba, estilos, textos, tema, propriedades) e interna."""
    estranhas = []

    def ao_abrir(local, atributos):
        if local == "Relationship" and (atributos.get("Type") not in _RELACOES_PASSIVAS
                                        or atributos.get("TargetMode", "Internal") != "Internal"):
            estranhas.append(local)
            raise _Encontrado

    _ler_xml(pacote, nome, ao_abrir)
    return not estranhas


def inventariar_xlsx(caminho) -> list:
    """O que impede liberar o próprio XLSX, já validado por etl_inputs (estrutura, tamanhos e
    expansão conferidos). Lista vazia quando o arquivo só tem dados: todas as partes passivas,
    relações internas de dados, nenhuma fórmula, nome definido, objeto, controle ou vínculo em
    parte nenhuma, nem tipo de conteúdo de macro.

    Toda parte da lista é lida pelo parser XML, e não só as abas: uma relação de aba pode
    apontar para o nome de uma parte passiva (tema, estilos, propriedades) e o pacote a lê
    como aba de lá."""
    achados = set()
    with zipfile.ZipFile(caminho) as pacote:
        nomes = [info.filename for info in pacote.infolist()]
        for nome in nomes:
            minusculo = nome.lower()
            if not _PARTES_PASSIVAS.fullmatch(minusculo):
                conhecidos = {achado for achado, padrao in _PARTES_ATIVAS if padrao.search(minusculo)}
                achados |= conhecidos or {"parte_nao_reconhecida"}
                continue
            try:
                if minusculo == "[content_types].xml":
                    with pacote.open(nome) as membro:
                        tipos = membro.read(_LIMITE_TIPOS).lower()
                    if b"macroenabled" in tipos or b"vbaproject" in tipos:
                        achados.add("macro_vba")
                elif minusculo in ("_rels/.rels", "xl/_rels/workbook.xml.rels"):
                    if not _relacoes_passivas(pacote, nome):
                        achados.add("relacao_nao_reconhecida")
                achados |= _elementos_ativos(pacote, nome)
            except expat.ExpatError:
                achados.add("parte_ilegivel")
    return sorted(achados)


def _minimo_vendas(mapa) -> bool:
    return "produto" in mapa and bool({"quantidade", "valor_venda", "preco_unitario"} & mapa.keys())


def _minimo_compras(mapa) -> bool:
    return (("produto" in mapa or "fornecedor" in mapa)
            and bool({"valor_nf", "produtos_total", "custo_unitario", "qtde_comprada"} & mapa.keys()))


def exigir_estrutura_minima(matriz) -> None:
    """Colunas legíveis de relatório de PDV: produto e quantidade ou valor (vendas), ou
    fornecedor/produto e valor (compras), com a mesma detecção de cabeçalho do ETL. É mais
    branda que a do processamento: nada que o processamento aceita é recusado aqui."""
    from etl_processor import ALIAS_COMPRAS, ALIAS_VENDAS, ETLValidationError, _detectar_cabecalho
    for aliases, minimo in ((ALIAS_VENDAS, _minimo_vendas), (ALIAS_COMPRAS, _minimo_compras)):
        try:
            _detectar_cabecalho(matriz, aliases, minimo)
            return
        except ETLValidationError:
            continue
    raise SecurityGatekeeperError("estrutura_ausente", ORIENTACAO_ESTRUTURA)


def _ausente(valor) -> bool:
    return valor is None or (isinstance(valor, float) and math.isnan(valor))


def _linhas(matriz):
    for linha in matriz.itertuples(index=False, name=None):
        yield [None if _ausente(valor) else str(valor) for valor in linha]


def _mesma_matriz(lida, esperada) -> bool:
    if lida.shape != esperada.shape:
        return False
    for linha_lida, linha_esperada in zip(lida.itertuples(index=False, name=None),
                                          esperada.itertuples(index=False, name=None)):
        for valor_lido, valor_esperado in zip(linha_lida, linha_esperada):
            if _ausente(valor_lido) or _ausente(valor_esperado):
                if not (_ausente(valor_lido) and _ausente(valor_esperado)):
                    return False
            elif type(valor_lido) is not str or valor_lido != valor_esperado:
                return False
    return True


_CABECALHO_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_RELACOES = "http://schemas.openxmlformats.org/package/2006/relationships"
_TIPO_DOCUMENTO = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PLANILHA = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_TIPO_SPREADSHEETML = "application/vnd.openxmlformats-officedocument.spreadsheetml"
_PARTES_XLSX = (
    ("[Content_Types].xml",
     _CABECALHO_XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
     '<Default Extension="xml" ContentType="application/xml"/>'
     f'<Override PartName="/xl/workbook.xml" ContentType="{_TIPO_SPREADSHEETML}.sheet.main+xml"/>'
     f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{_TIPO_SPREADSHEETML}.worksheet+xml"/>'
     f'<Override PartName="/xl/sharedStrings.xml" ContentType="{_TIPO_SPREADSHEETML}.sharedStrings+xml"/>'
     '</Types>'),
    ("_rels/.rels",
     _CABECALHO_XML + f'<Relationships xmlns="{_RELACOES}"><Relationship Id="rId1" '
     f'Type="{_TIPO_DOCUMENTO}/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
    ("xl/workbook.xml",
     _CABECALHO_XML + f'<workbook xmlns="{_PLANILHA}" xmlns:r="{_TIPO_DOCUMENTO}">'
     '<sheets><sheet name="Dados" sheetId="1" r:id="rId1"/></sheets></workbook>'),
    ("xl/_rels/workbook.xml.rels",
     _CABECALHO_XML + f'<Relationships xmlns="{_RELACOES}"><Relationship Id="rId1" '
     f'Type="{_TIPO_DOCUMENTO}/worksheet" Target="worksheets/sheet1.xml"/>'
     f'<Relationship Id="rId2" Type="{_TIPO_DOCUMENTO}/sharedStrings" Target="sharedStrings.xml"/>'
     '</Relationships>'),
)
_FORA_DO_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _parte_zip(nome):
    info = zipfile.ZipInfo(nome, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _coluna(indice: int) -> str:
    letras, indice = "", indice + 1
    while indice:
        indice, resto = divmod(indice - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def _texto_xml(valor: str) -> str:
    if _FORA_DO_XML.search(valor):
        raise ValueError("Caractere que o XML não representa.")
    # CR em texto XML vira LF na leitura; a referência numérica o preserva.
    return escape(valor).replace("\r", "&#13;")


_PARECE_MILHAR = re.compile(r"-?[0-9]{1,3}\.[0-9]{3}")


def _numero_que_o_motor_le_igual(valor: str) -> bool:
    """O texto é exatamente como o motor (pandas, dtype=str) escreve aquele número: gravado como
    célula numérica, ele volta igual e a cópia não cresce. O formato de milhar fica como texto,
    porque o motor acrescenta um zero ao número que se parece com ele."""
    try:
        numero = float(valor)
    except ValueError:
        return False
    if not math.isfinite(numero) or _PARECE_MILHAR.fullmatch(valor):
        return False
    return valor == (str(int(numero)) if numero.is_integer() else repr(numero))


def _escrever_xlsx(linhas, destino) -> None:
    """XLSX mínimo de uma aba, só com valores: nenhuma fórmula, estilo, macro, objeto ou
    vínculo. Número que o motor lê igual vai como célula numérica e o resto pela tabela de
    textos compartilhados, como o Excel grava; a linha e a célula seguinte não levam
    coordenada, para a cópia não ficar maior que o original. Escrito em fluxo."""
    textos = {}
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as pacote:
        for nome, conteudo in _PARTES_XLSX:
            pacote.writestr(_parte_zip(nome), conteudo.encode("utf-8"))
        referencias = 0
        with pacote.open(_parte_zip("xl/worksheets/sheet1.xml"), "w") as folha:
            folha.write((_CABECALHO_XML + f'<worksheet xmlns="{_PLANILHA}"><sheetData>').encode("utf-8"))
            for numero, linha in enumerate(linhas, 1):
                celulas, anterior = [], -1
                for indice, valor in enumerate(linha):
                    if valor is None:
                        continue
                    coordenada = "" if indice == anterior + 1 else f' r="{_coluna(indice)}{numero}"'
                    anterior = indice
                    if _numero_que_o_motor_le_igual(valor):
                        celulas.append(f"<c{coordenada}><v>{valor}</v></c>")
                    else:
                        posicao = textos.setdefault(valor, len(textos))
                        celulas.append(f'<c{coordenada} t="s"><v>{posicao}</v></c>')
                        referencias += 1
                folha.write(f'<row>{"".join(celulas)}</row>'.encode("utf-8"))
            folha.write(b"</sheetData></worksheet>")
        with pacote.open(_parte_zip("xl/sharedStrings.xml"), "w") as tabela:
            tabela.write((_CABECALHO_XML + f'<sst xmlns="{_PLANILHA}" count="{referencias}" '
                          f'uniqueCount="{len(textos)}">').encode("utf-8"))
            for valor in textos:
                tabela.write(f'<si><t xml:space="preserve">{_texto_xml(valor)}</t></si>'.encode("utf-8"))
            tabela.write(b"</sst>")


def _escrever_csv(linhas, destino, separador: str, citar_tudo: bool, codificacao: str) -> None:
    """CSV na codificação do original e com LF, para a cópia crescer só pelos apóstrofos. Uma
    linha curta continua curta: o leitor completa com vazio (None) o que falta, e um campo
    vazio escrito viraria texto vazio."""
    with open(destino, "w", encoding=codificacao, newline="") as saida:
        escritor = csv.writer(saida, delimiter=separador, lineterminator="\n",
                              quoting=csv.QUOTE_ALL if citar_tudo else csv.QUOTE_MINIMAL)
        for linha in linhas:
            while linha and linha[-1] is None:
                linha.pop()
            if not linha or any(valor is None for valor in linha):
                raise ValueError("Linha que o CSV não representa.")
            escritor.writerow(linha)


def _codificacao_da_copia(original, matriz) -> str:
    """A codificação em que o leitor do ETL decodificou o original; o BOM só se ele o tinha."""
    codificacao = matriz.attrs.get("encoding")
    if codificacao == "utf-8-sig":
        with open(original, "rb") as arquivo:
            return "utf-8-sig" if arquivo.read(3) == codecs.BOM_UTF8 else "utf-8"
    return codificacao if codificacao in ("utf-8", "cp1252", "latin1") else "utf-8-sig"


# Recusas da releitura que dizem só que a cópia ficou grande demais para os limites do motor.
_CODIGOS_DE_TAMANHO = frozenset({"file_bytes", "file_size", "uncompressed_bytes", "zip_ratio", "record_chars",
                                 "field_chars", "cells", "rows", "columns", "xml_nodes"})


def _liberar_copia(matriz, formato: str, original: Path, destino: Path, limits) -> str | None:
    """Grava a cópia só com valores e confere: a releitura pelo motor precisa dar exatamente a
    matriz já neutralizada. No CSV tenta primeiro a codificação e o separador do original, com
    e sem aspas em todos os campos, e só aceita a cópia que também passa pela assinatura.
    Devolve None quando a cópia foi liberada; senão, o motivo da recusa: "copia_acima_do_teto"
    se toda tentativa esbarrou num limite de tamanho, "neutralizacao_inviavel" nos demais casos."""
    so_tamanho = True
    if formato == "xlsx":
        tentativas = [(None, False, None)]
    else:
        separador = matriz.attrs.get("separator")
        separadores = ([separador] if separador in SEPARATORS else []) + [s for s in SEPARATORS if s != separador]
        codificacao = _codificacao_da_copia(original, matriz)
        codificacoes = [codificacao] + (["utf-8-sig"] if codificacao != "utf-8-sig" else [])
        tentativas = [(s, tudo, c) for c in codificacoes for s in separadores for tudo in (False, True)]
    for separador, citar_tudo, codificacao in tentativas:
        try:
            if formato == "xlsx":
                _escrever_xlsx(_linhas(matriz), destino)
            else:
                _escrever_csv(_linhas(matriz), destino, separador, citar_tudo, codificacao)
                # ler_liberado confere a assinatura de novo. Sem as aspas do original, um título
                # como "<< RELATÓRIO >>" faria a cópia parecer marcação: tenta outra forma.
                with open(destino, "rb") as copia:
                    if identificar_conteudo(copia.read(_AMOSTRA)) != "texto":
                        so_tamanho = False
                        continue
            if _mesma_matriz(read_raw(str(destino), limits), matriz):
                return None
            so_tamanho = False
        except ETLInputError as erro:
            so_tamanho = so_tamanho and erro.code in _CODIGOS_DE_TAMANHO
        except OSError as erro:  # no worker, o RLIMIT_FSIZE corta a gravação acima do teto
            so_tamanho = so_tamanho and erro.errno == errno.EFBIG
        except (ValueError, csv.Error, zipfile.BadZipFile):
            so_tamanho = False
    try:
        destino.unlink()
    except FileNotFoundError:
        pass
    return "copia_acima_do_teto" if so_tamanho else "neutralizacao_inviavel"


def triar(caminho, extensao: str, limits) -> dict:
    """Segunda triagem, no processo isolado (etl_worker, operação "validate").

    Valida e lê o arquivo com o motor (que já neutraliza as células), exige a estrutura
    mínima de relatório e, no XLSX, inventaria o conteúdo ativo. Havendo algo a neutralizar,
    deixa ao lado do arquivo a cópia só com valores (NOME_LIBERADO), conferida por releitura.
    """
    caminho = Path(caminho)
    formato = (extensao or "").lower().lstrip(".")
    if formato not in ("csv", "xlsx") or caminho.suffix.lower() != "." + formato:
        raise SecurityGatekeeperError("formato_de_leitura")
    destino = caminho.with_name(f"{NOME_LIBERADO}.{formato}")
    if destino.exists() or destino.is_symlink():
        raise SecurityGatekeeperError("liberado_existente")
    matriz = read_raw(str(caminho), limits)
    exigir_estrutura_minima(matriz)
    achados = inventariar_xlsx(caminho) if formato == "xlsx" else []
    neutralizadas = int(matriz.attrs.get("celulas_neutralizadas", 0))
    copia = bool(achados or neutralizadas)
    recusa = _liberar_copia(matriz, formato, caminho, destino, limits) if copia else None
    if recusa:
        raise SecurityGatekeeperError(
            recusa, ORIENTACAO_TAMANHO if recusa == "copia_acima_do_teto" else ORIENTACAO_NEUTRALIZAR)
    return {"format": formato, "liberado": "copia_neutralizada" if copia else "original",
            "achados": achados, "celulas_neutralizadas": neutralizadas,
            "linhas": int(matriz.shape[0]), "colunas": int(matriz.shape[1])}
