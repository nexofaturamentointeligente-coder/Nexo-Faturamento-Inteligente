"""SEC-005: leitores tabulares limitados, sem banco, extração ZIP ou dados em logs.

As verificações cooperativas complementam o isolamento de processo do chamador;
não constituem, sozinhas, timeout rígido de uma biblioteca nativa.

Formatos: .csv (texto delimitado), .xlsx (openpyxl e pandas, depois da conferência do pacote), .xls do
Excel 97-2003 (etl_biff) e Planilha XML 2003 (etl_planilha_xml). Os dois últimos são leitores próprios,
só com a biblioteca padrão: do arquivo saem apenas os valores das células (fórmula entra pelo valor
guardado; macro, objeto embutido e vínculo não são abertos), e a matriz devolvida segue a mesma convenção
de texto do .xlsx (_texto_tipado), para o motor ler o mesmo relatório do mesmo jeito nos três formatos.
"""

import csv
from datetime import datetime
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import time
from xml.parsers import expat
import zipfile
import zlib

import pandas as pd
from openpyxl import load_workbook
from openpyxl.cell.cell import TYPE_ERROR, TYPE_NUMERIC
from pandas.io.parsers.base_parser import STR_NA_VALUES

from etl_limits import ETLLimits


ENCODINGS = ("utf-8-sig", "cp1252", "utf-8", "latin1")
SEPARATORS = (";", ",", "\t", "|")
_CHUNK = 65536
_CELL = re.compile(r"([A-Za-z]{1,3})([1-9][0-9]{0,6})\Z")
_LOOKS_LIKE_THOUSANDS = re.compile(r"-?[0-9]{1,3}\.[0-9]{3}")


class ETLInputError(Exception):
    """Somente código e mensagem estáticos são apropriados para a interface."""

    def __init__(self, code, safe_message):
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


def _reject(code, message):
    raise ETLInputError(code, message)


class _Budget:
    def __init__(self, limits):
        self.limits = limits
        self.deadline = time.monotonic() + limits.timeout_seconds
        self.uncompressed = 0
        self.cells = 0
        self.sheets = 0
        self.actual_cells = 0

    def check(self):
        if time.monotonic() > self.deadline:
            _reject("timeout", "O prazo de validação do arquivo foi excedido.")


def _file_size(path, limits):
    try:
        info = os.stat(path)
    except OSError:
        _reject("unavailable", "O arquivo de processamento não está disponível.")
    if not stat.S_ISREG(info.st_mode):
        _reject("invalid_file", "O arquivo informado não é um arquivo regular.")
    if info.st_size == 0:
        _reject("empty", "O arquivo está vazio.")
    if info.st_size > limits.max_file_bytes:
        _reject("file_bytes", "O arquivo excede o limite de tamanho permitido.")
    return info.st_size


def _trim(frame):
    if frame is None or frame.empty:
        return frame
    clean = frame.dropna(axis=0, how="all").dropna(axis=1, how="all")
    return clean.reset_index(drop=True) if not clean.empty else frame


class _CSVLines:
    """Fornece linhas físicas completas, limitando também registros com aspas."""

    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget
        self.record_chars = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.budget.check()
        remaining = self.budget.limits.max_record_chars - self.record_chars
        line = self.stream.readline(remaining + 1)
        if not line:
            raise StopIteration
        self.record_chars += len(line)
        if self.record_chars > self.budget.limits.max_record_chars:
            _reject("record_chars", "O CSV contém um registro acima do limite permitido.")
        # Compatibilidade: o leitor anterior removia NUL antes do csv.reader.
        return line.replace("\x00", "")


class _SeparatorShape:
    """O que um separador candidato faz do arquivo: quantos registros têm cada largura (`widths`); se ele
    separa, em dois campos ou mais, algum registro que não tem vírgula nenhuma (`comma_free`); e, dos
    registros com vírgula que ele separa em MAIS campos do que a vírgula separaria, o menor número de
    vírgulas de cada largura (`wider`)."""

    def __init__(self):
        self.widths, self.comma_free, self.wider = {}, False, {}

    def add(self, row):
        fields = len(row)
        self.widths[fields] = self.widths.get(fields, 0) + 1
        if fields < 2 or self.comma_free:
            return
        commas = sum(field.count(",") for field in row)
        if not commas:
            self.comma_free = True
        elif fields > commas + 1 and commas < self.wider.get(fields, commas + 1):
            self.wider[fields] = commas

    def beats_comma(self, width, comma_width):
        """O separador, de largura típica `width`, organiza ao menos um registro melhor do que a vírgula, de
        largura típica `comma_width`: um registro sem vírgula nenhuma, ou uma linha inteira da tabela dele
        que ele separa em mais campos do que a vírgula e que a vírgula não leva à largura dela."""
        return self.comma_free or any(fields >= width and commas + 1 < comma_width
                                      for fields, commas in self.wider.items())


def _csv_pass(path, encoding, separator, budget, *, materialize=False, validate_fields=False, shape=None):
    rows, columns, values = 0, 0, [] if materialize else None
    with open(path, "r", encoding=encoding, newline="") as stream:
        lines = _CSVLines(stream, budget)
        reader = csv.reader(lines, delimiter=separator)
        while True:
            lines.record_chars = 0
            try:
                row = next(reader)
            except StopIteration:
                break
            rows += 1
            columns = max(columns, len(row))
            if shape is not None:
                shape.add(row)
            if validate_fields and rows > budget.limits.max_rows:
                _reject("rows", "O CSV excede o limite de linhas permitido.")
            if validate_fields and columns > budget.limits.max_columns:
                _reject("columns", "O CSV excede o limite de colunas permitido.")
            if validate_fields and rows * columns > min(
                    budget.limits.max_cells_per_file, budget.limits.max_cells_per_sheet):
                _reject("cells", "O CSV excede o orçamento de células permitido.")
            # Só o separador selecionado define campos. Um separador candidato
            # incorreto pode combinar uma linha legítima em um único campo.
            if validate_fields and any(len(field) > budget.limits.max_field_chars for field in row):
                _reject("field_chars", "O CSV contém um campo acima do limite permitido.")
            if materialize:
                values.append(row)
    return rows, columns, values



def _csv_signature(path):
    # CSV é texto; assinaturas fortes de formatos binários/documentais não
    # se tornam CSV por haver ';' incidental em algum conteúdo posterior.
    with open(path, "rb") as stream:
        prefix = stream.read(64).lstrip(b" \t\r\n")
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    signatures = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",
                  b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"%PDF-",
                  b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
    if prefix.startswith(signatures) or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"):
        _reject("csv_incompatible", "O conteúdo do arquivo não é compatível com o formato CSV.")


# Um arquivo UTF-8 com um único byte de outra codificação (um "é" de Windows-1252 colado de
# outro sistema) não decodifica como UTF-8 e cai inteiro para cp1252 (ou latin1): os acentos
# certos viram "Ã©". Cada sequência UTF-8 desfeita assim é refeita célula a célula; o byte
# estranho fica como o cp1252 o lê. Sem isso, "Café" de um mês e "CafÃ©" de outro viram dois
# produtos, e a chave do produto (sem acento) ainda sai "CAFA".
def _continuacoes_utf8(encoding):
    """Como `encoding` lê os bytes de continuação do UTF-8 (0x80 a 0xBF)."""
    caracteres = []
    for byte in range(0x80, 0xC0):
        try:
            caracteres.append(bytes([byte]).decode(encoding))
        except UnicodeDecodeError:
            pass  # indefinido no cp1252: um arquivo com ele nem é lido como cp1252
    return "".join(caracteres)


def _padrao_utf8_desfeito(encoding):
    continuacao = "[" + re.escape(_continuacoes_utf8(encoding)) + "]"
    return re.compile(f"[Â-ß]{continuacao}|[à-ï]{continuacao}{{2}}"
                      f"|[ð-ô]{continuacao}{{3}}")


_UTF8_DESFEITO = {encoding: _padrao_utf8_desfeito(encoding) for encoding in ("cp1252", "latin1")}


# Caractere que uma sequência UTF-8 desfeita costuma esconder num texto em português: Latin-1 (letras
# acentuadas, ç, º, NBSP) e a pontuação do Windows-1252 (aspas curvas, travessão, reticências, €, •, ™).
# "É" + NBSP de um CSV cp1252 legítimo também forma UTF-8 válido, mas dá "ɠ" (U+0260), e fica como está.
_PONTUACAO_CP1252 = frozenset("€‚„…†‡‰‹›‘’“”•–—™")


def _caractere_plausivel(caractere):
    return "\xa0" <= caractere <= "ÿ" or caractere in _PONTUACAO_CP1252


def _utf8_refeito(trecho, encoding):
    """O caractere da sequência UTF-8 lida como `encoding`, ou None se ela não for plausível.

    O BOM lido como texto ("ï»¿") fica como está: é o resto de BOM que a Baia trata à parte
    (security_gatekeeper._RESTOS_DE_BOM), e a cópia neutralizada o regrava byte a byte."""
    try:
        refeito = trecho.encode(encoding).decode("utf-8")
    except UnicodeError:
        return None
    return refeito if _caractere_plausivel(refeito) else None


def _refazer_utf8(texto, encoding):
    """O texto com cada sequência UTF-8 plausível lida como `encoding` refeita ("CafÃ©" -> "Café").

    "Ã" + NBSP depois de letra ("MAÇÃ VERDE" num CSV cp1252 legítimo) daria "à" no meio da
    palavra, o que o português não tem: fica como está."""
    def refazer(encontrado):
        trecho = encontrado.group()
        refeito = _utf8_refeito(trecho, encoding)
        if refeito is None:
            return trecho
        inicio = encontrado.start()
        if refeito == "à" and inicio > 0 and texto[inicio - 1].isalpha():
            return trecho
        return refeito
    return _UTF8_DESFEITO[encoding].sub(refazer, texto)


def _reparar_codificacao(rows, encoding, budget):
    """Refaz no lugar o UTF-8 desfeito das células; devolve quantas mudaram. A decisão é da célula,
    e não da proporção do arquivo: o mesmo nome sai igual em todos os meses de um envio, e o que
    não é UTF-8 plausível (ver _caractere_plausivel) fica como o cp1252 o lê."""
    if encoding not in _UTF8_DESFEITO:
        return 0
    reparadas = 0
    for row in rows:
        budget.check()
        for index, cell in enumerate(row):
            if cell and not cell.isascii():
                novo = _refazer_utf8(cell, encoding)
                if novo != cell:
                    row[index] = novo
                    reparadas += 1
    return reparadas


def _typical_width(widths):
    """(largura típica, linhas que a alcançam) de um separador candidato, a partir de {largura: linhas}.

    A largura típica é a maior que mais de um quinto das linhas com mais de um campo alcança, e nunca
    uma linha sozinha (quando há mais de uma). Num arquivo regular é a própria largura da tabela, como o
    máximo de antes. A diferença está nas poucas linhas mais largas que as outras: um nome de produto
    com várias vírgulas, num relatório de valores com vírgula decimal, dava a uma linha mais colunas
    pela vírgula do que a tabela tem pelo ponto e vírgula, e o máximo trocava o separador do arquivo
    inteiro. Linhas mais curtas (título, rótulo e valor, campos vazios do fim omitidos) não mudam a
    escolha enquanto as linhas inteiras da tabela passarem de um quinto."""
    multi = sorted(((width, count) for width, count in widths.items() if width > 1), reverse=True)
    total = sum(count for _width, count in multi)
    need = max(total // 5 + 1, min(2, total))
    reach = 0
    for width, count in multi:
        reach += count
        if reach >= need:
            return width, reach
    return 0, 0


def _choose_separator(candidates):
    """O candidato (separador, linhas, colunas, (largura típica, linhas que a alcançam), _SeparatorShape) que
    fica com o arquivo.

    Vence a maior largura típica; no empate, quem a alcança em mais linhas e, depois, a ordem de SEPARATORS,
    como sempre. A vírgula tem uma condição a mais, porque num relatório brasileiro ela é quase sempre a
    vírgula decimal: cada número com decimais dá à linha uma "coluna" a mais pela vírgula, e uma vírgula no
    nome do produto ("ESMALTE 3,6L") dá outra. Numa tabela em que todas as colunas, menos a do nome, têm
    vírgula decimal, a vírgula empata com o separador de verdade, ou passa dele por uma coluna. Por isso a
    vírgula perde para outro separador em dois casos:
      - ele tem a mesma largura típica (ou, numa tabela de três colunas ou mais, uma coluna a menos) e a
        alcança em ao menos metade das linhas que a vírgula alcança;
      - ele alcança a largura típica dele em mais que o dobro das linhas da vírgula, com qualquer largura
        (um bloco grande de "rótulo;valor" antes de uma tabela de dois produtos).
    Quando o outro separador só aparece em poucas linhas (um ";" no nome de dois fornecedores de um CSV
    separado por vírgula) ou organiza bem menos colunas (um ";" dentro de um campo de texto), a vírgula
    fica.

    Nos dois casos, o outro separador tem de organizar ao menos um registro melhor do que a vírgula
    (_SeparatorShape.beats_comma): um registro sem vírgula nenhuma, ou uma linha inteira da tabela dele que
    ele separa em mais campos do que a vírgula separaria e que a vírgula não leva à largura típica dela.
    Num relatório com vírgula decimal, é a linha de títulos ("Produto;Qtde;Valor Total", ou
    "Produto;Qtde, Un;Valor Total", com uma vírgula dentro de um título). Num CSV separado por vírgula de
    verdade, o ";" ou a "|" de dentro de um campo de texto ("organico;torrado;moido", "CAMISETA | AZUL | M")
    aparece em registros que a vírgula separa em tantos campos quanto ele, ou em mais, e a vírgula fica com
    o arquivo."""
    best = None
    for candidate in candidates:
        if best is None or candidate[3] > best[3]:
            best = candidate
    if best is None or best[0] != ",":
        return best
    width, lines = best[3]
    rival = None
    for candidate in candidates:
        other_width, other_lines = candidate[3]
        if candidate[0] == "," or not candidate[4].beats_comma(other_width, width):
            continue
        close = other_width == width or (other_width == width - 1 and other_width >= 3)
        if (2 * other_lines >= lines if close else other_lines > 2 * lines):
            if rival is None or candidate[3] > rival[3]:
                rival = candidate
    return best if rival is None else rival


def _csv_input(path, budget, materialize):
    _csv_signature(path)
    selected = None
    for encoding in ENCODINGS:
        candidates = []
        decode_failed = False
        for separator in SEPARATORS:
            shape = _SeparatorShape()
            try:
                rows, columns, _ = _csv_pass(path, encoding, separator, budget, shape=shape)
            except UnicodeDecodeError:
                decode_failed = True
                break
            except csv.Error:
                # O leitor anterior também descartava separadores que não
                # podiam ser interpretados pelo csv.reader padrão.
                continue
            candidates.append((separator, rows, columns, _typical_width(shape.widths), shape))
        if decode_failed:
            continue
        best = _choose_separator(candidates)
        if best is not None and best[2] > 1:
            selected = (encoding, *best[:3])
            break
    if selected is None:
        _reject("csv_invalid", "Não foi possível interpretar o CSV com os formatos suportados.")
    encoding, separator, rows, columns = selected
    # A segunda passagem valida os campos sem depender da pequena amostra ou
    # do Content-Length; só então o conteúdo pode ser utilizado/materializado.
    _, _, values = _csv_pass(path, encoding, separator, budget,
                             materialize=materialize, validate_fields=True)
    metadata = {"format": "csv", "rows": rows, "columns": columns,
                "cells": rows * columns, "sheets": 1, "encoding": encoding,
                "separator": separator}
    if not materialize:
        return metadata
    reparadas = _reparar_codificacao(values, encoding, budget)
    rectangular = [row + [None] * (columns - len(row)) for row in values]
    frame = _trim(pd.DataFrame(rectangular, dtype=object))
    # A Baia de Segurança regrava o CSV neutralizado com o mesmo separador, quando possível.
    frame.attrs.update({"format": "csv", "separator": separator, "encoding": encoding,
                        "celulas_recodificadas": reparadas})
    return frame


def _end_directory(stream, size, limits):
    # Conferir a quantidade ANTES de ZipFile materializar um diretório enorme.
    stream.seek(max(0, size - 65557))
    tail = stream.read(min(size, 65557))
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or len(tail) - position < 22:
        _reject("xlsx_zip", "A estrutura compactada do XLSX é inválida.")
    signature, disk, central_disk, disk_entries, entries, central_size, offset, comment = struct.unpack(
        "<4s4H2IH", tail[position:position + 22])
    absolute = size - len(tail) + position
    if (disk or central_disk or disk_entries != entries or position + 22 + comment != len(tail)
            or offset + central_size != absolute):
        _reject("xlsx_zip", "A estrutura compactada do XLSX é inválida ou incompatível.")
    if entries > limits.max_zip_entries:
        _reject("zip_entries", "O XLSX contém entradas internas em excesso.")
    if entries == 0 or entries == 65535:
        _reject("xlsx_zip", "A estrutura compactada do XLSX é inválida ou incompatível.")
    # O total declarado também pode mentir. Contar os headers centrais reais
    # com memória constante impede ZipFile de alocar milhares de ZipInfo antes
    # de detectar a inconsistência. Não materializar nomes ou extras aqui.
    cursor, observed = offset, 0
    while cursor < absolute:
        stream.seek(cursor)
        header = stream.read(46)
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            _reject("xlsx_zip", "O diretório interno do XLSX é inválido.")
        name_size, extra_size, comment_size = struct.unpack_from("<3H", header, 28)
        observed += 1
        if observed > limits.max_zip_entries:
            _reject("zip_entries", "O XLSX contém entradas internas em excesso.")
        cursor += 46 + name_size + extra_size + comment_size
        if cursor > absolute:
            _reject("xlsx_zip", "O diretório interno do XLSX é inválido.")
    if cursor != absolute or observed != entries:
        _reject("xlsx_zip", "O XLSX contém metadados de entradas inconsistentes.")
    return offset, entries


def _member_start(stream, info, directory_start):
    stream.seek(info.header_offset)
    raw = stream.read(30)
    if len(raw) != 30:
        _reject("xlsx_zip", "A estrutura compactada do XLSX é inválida.")
    signature, _, flags, method, _, _, crc, compressed, expanded, name_size, extra_size = struct.unpack(
        "<4s5H3I2H", raw)
    name_raw, extra = stream.read(name_size), stream.read(extra_size)
    try:
        name = name_raw.decode("utf-8" if flags & 0x800 else "cp437")
    except UnicodeDecodeError:
        _reject("xlsx_zip", "A estrutura compactada do XLSX é inválida.")
    if (signature != b"PK\x03\x04" or flags != info.flag_bits or method != info.compress_type
            or name != info.filename or flags & 1):
        _reject("xlsx_zip", "O XLSX é criptografado ou tem metadados incompatíveis.")
    if not flags & 8:
        if expanded == 0xFFFFFFFF or compressed == 0xFFFFFFFF:
            position = 0
            found = None
            while position + 4 <= len(extra):
                kind, length = struct.unpack_from("<HH", extra, position)
                body = extra[position + 4:position + 4 + length]
                if len(body) != length:
                    break
                if kind == 1:
                    found = body
                    break
                position += length + 4
            cursor = 0
            if found is None:
                _reject("xlsx_zip", "A estrutura compactada do XLSX é inválida.")
            for field in ("expanded", "compressed"):
                current = expanded if field == "expanded" else compressed
                if current == 0xFFFFFFFF:
                    if cursor + 8 > len(found):
                        _reject("xlsx_zip", "A estrutura compactada do XLSX é inválida.")
                    value = struct.unpack_from("<Q", found, cursor)[0]
                    cursor += 8
                    if field == "expanded":
                        expanded = value
                    else:
                        compressed = value
        if (crc, compressed, expanded) != (info.CRC, info.compress_size, info.file_size):
            _reject("xlsx_zip", "O XLSX contém metadados de tamanho inconsistentes.")
    start = info.header_offset + 30 + name_size + extra_size
    if info.header_offset < 0 or start + info.compress_size > directory_start:
        _reject("xlsx_zip", "A estrutura compactada do XLSX é inválida.")
    return start


def _coordinate(reference, limits):
    match = _CELL.fullmatch(reference or "")
    if match is None:
        _reject("xlsx_xml", "O XLSX contém coordenadas de células inválidas.")
    column = 0
    for character in match[1].upper():
        column = column * 26 + ord(character) - 64
    row = int(match[2])
    if row > limits.max_rows:
        _reject("rows", "O XLSX excede o limite de linhas permitido.")
    if column > limits.max_columns:
        _reject("columns", "O XLSX excede o limite de colunas permitido.")
    return row, column


class _XMLCheck:
    def __init__(self, name, budget):
        self.budget, self.name = budget, name
        self.worksheet = name.startswith("xl/worksheets/") and name.endswith(".xml")
        self.workbook = name == "xl/workbook.xml"
        self.shared = name == "xl/sharedStrings.xml"
        self.rows = self.columns = self.nodes = self.shared_count = 0
        self.depth = 0
        self.owners = []
        self.previous_row = 0
        self.row_cells = 0
        self.last_column = 0
        self.actual_cells = 0
        self.parser = expat.ParserCreate(namespace_separator="}")
        self.parser.StartElementHandler = self.start
        self.parser.EndElementHandler = self.end
        self.parser.CharacterDataHandler = self.text
        self.parser.StartDoctypeDeclHandler = self.forbidden
        self.parser.EntityDeclHandler = self.forbidden
        self.parser.ExternalEntityRefHandler = self.forbidden

    def forbidden(self, *args):
        _reject("xlsx_xml", "O XLSX contém estrutura XML não permitida.")

    def start(self, name, attributes):
        self.depth += 1
        self.nodes += 1
        if self.depth > 64 or self.nodes > self.budget.limits.max_cells_per_file * 12 + 16384:
            _reject("xml_nodes", "A estrutura XML do XLSX excede o orçamento permitido.")
        tag = name.rsplit("}", 1)[-1]
        if self.depth == 1:
            self.worksheet = self.worksheet or tag == "worksheet"
            self.workbook = self.workbook or tag == "workbook"
            self.shared = self.shared or tag == "sst"
        if self.workbook and tag == "sheet":
            self.budget.sheets += 1
            if self.budget.sheets > self.budget.limits.max_sheets:
                _reject("sheets", "O XLSX excede o limite de abas permitido.")
        if self.shared and tag == "si":
            self.shared_count += 1
            if self.shared_count > self.budget.limits.max_cells_per_file:
                _reject("cells", "O XLSX excede o orçamento de textos compartilhados.")
        if (self.shared and tag == "si") or (self.worksheet and tag == "c"):
            self.owners.append([self.depth, 0])
        if self.worksheet and tag == "row":
            raw_row = attributes.get("r")
            if raw_row is not None:
                if not raw_row.isascii() or not raw_row.isdecimal() or len(raw_row) > 7:
                    _reject("xlsx_xml", "O XLSX contém coordenadas de linhas inválidas.")
                row = int(raw_row)
            else:
                row = self.previous_row + 1
            if row <= self.previous_row or row > self.budget.limits.max_rows:
                _reject("rows", "O XLSX excede o limite de linhas ou contém linhas inválidas.")
            self.previous_row = row
            self.rows = max(self.rows, row)
            self.row_cells = 0
            self.last_column = 0
        if self.worksheet and tag == "c":
            self.row_cells += 1
            self.actual_cells += 1
            self.budget.actual_cells += 1
            if (self.actual_cells > self.budget.limits.max_cells_per_sheet
                    or self.budget.actual_cells > self.budget.limits.max_cells_per_file):
                _reject("cells", "O XLSX excede o orçamento de células permitido.")
            reference = attributes.get("r")
            row, column = (_coordinate(reference, self.budget.limits) if reference is not None
                           else (self.previous_row, self.last_column + 1))
            if row != self.previous_row or not row or column <= self.last_column:
                _reject("xlsx_xml", "O XLSX contém coordenadas de células inconsistentes.")
            self.last_column = column
            self.rows, self.columns = max(self.rows, row), max(self.columns, column)
            if self.rows > self.budget.limits.max_rows:
                _reject("rows", "O XLSX excede o limite de linhas permitido.")
            if self.columns > self.budget.limits.max_columns:
                _reject("columns", "O XLSX excede o limite de colunas permitido.")
            if self.rows * self.columns > self.budget.limits.max_cells_per_sheet:
                _reject("cells", "Uma aba do XLSX excede o orçamento de células permitido.")

    def end(self, name):
        if self.owners and self.owners[-1][0] == self.depth:
            self.owners.pop()
        self.depth -= 1

    def text(self, value):
        for owner in self.owners:
            owner[1] += len(value)
            if owner[1] > self.budget.limits.max_field_chars:
                _reject("field_chars", "O XLSX contém uma célula acima do limite permitido.")

    def feed(self, data, final=False):
        self.budget.check()
        self.parser.Parse(data, final)

    def finish(self):
        self.feed(b"", True)
        if self.worksheet:
            self.budget.cells += self.rows * self.columns
            if self.budget.cells > self.budget.limits.max_cells_per_file:
                _reject("cells", "O XLSX excede o orçamento acumulado de células permitido.")



class _MaybeXML:
    """Identifica XML também em aliases OPC e prefixos divididos entre blocos."""

    _PREFIXES = (b"<", b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff",
                 b"\x00<", b"\x00\x00\xfe\xff", b"\x00\x00\x00<")

    def __init__(self, name, budget):
        self.checker = _XMLCheck(name, budget)
        self.is_xml = True if name.lower().endswith((".xml", ".rels")) else None
        self.pending = b""

    def feed(self, data):
        if self.is_xml is False:
            return
        if self.is_xml is None:
            data = self.pending + data
            self.pending = b""
            prefix = data.lstrip(b" \t\r\n")
            if prefix:
                if any(prefix.startswith(marker) for marker in self._PREFIXES):
                    self.is_xml = True
                elif any(marker.startswith(prefix) for marker in self._PREFIXES):
                    # Só alguns bytes do BOM foram produzidos pelo inflater.
                    # Um bloco (64 KiB) mais até três bytes é o máximo retido.
                    self.pending = data
                    return
                else:
                    self.is_xml = False
                    return
        self.checker.feed(data)

    def finish(self):
        if self.pending:
            _reject("xlsx_xml", "O XLSX contém uma parte XML incompleta.")
        if self.is_xml:
            self.checker.finish()


def _stream_member(stream, info, budget, xml):
    expanded = crc = 0
    decompressor = zlib.decompressobj(-15) if info.compress_type == zipfile.ZIP_DEFLATED else None

    def consume(data):
        nonlocal expanded, crc
        if not data:
            return
        expanded += len(data)
        budget.uncompressed += len(data)
        if expanded > info.file_size or budget.uncompressed > budget.limits.max_uncompressed_bytes:
            _reject("uncompressed_bytes", "O XLSX excede o limite de expansão permitido.")
        if expanded > max(info.compress_size, 1) * budget.limits.max_zip_ratio:
            _reject("zip_ratio", "O XLSX apresenta expansão compactada excessiva.")
        crc = zlib.crc32(data, crc)
        if xml is not None:
            xml.feed(data)

    remaining = info.compress_size
    while remaining:
        budget.check()
        part = stream.read(min(_CHUNK, remaining))
        if not part:
            _reject("xlsx_zip", "O arquivo XLSX está truncado.")
        remaining -= len(part)
        if decompressor is None:
            consume(part)
        else:
            pending = part
            while pending:
                budget.check()
                output = decompressor.decompress(pending, _CHUNK)
                pending = decompressor.unconsumed_tail
                consume(output)
                if decompressor.unused_data:
                    _reject("xlsx_zip", "O XLSX contém dados compactados inconsistentes.")
    if decompressor is not None:
        if not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
            _reject("xlsx_zip", "O arquivo XLSX está truncado ou tem estrutura inválida.")
    if expanded != info.file_size or crc & 0xFFFFFFFF != info.CRC:
        _reject("xlsx_zip", "O XLSX falhou na verificação de integridade.")
    if xml is not None:
        xml.finish()


def _zip_preflight(path, size, budget):
    with open(path, "rb") as raw:
        directory_start, entries = _end_directory(raw, size, budget.limits)
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) != entries or archive.start_dir != directory_start:
                _reject("xlsx_zip", "A estrutura compactada do XLSX é inválida.")
            names = set()
            intervals = []
            declared = 0
            for info in infos:
                budget.check()
                name = info.filename
                parts = PurePosixPath(name).parts
                if (not name or name in names or "\\" in name or "\x00" in name
                        or name.startswith("/") or ".." in parts
                        or stat.S_ISLNK(info.external_attr >> 16)):
                    _reject("xlsx_zip", "O XLSX contém entradas internas inválidas.")
                names.add(name)
                if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    _reject("xlsx_zip", "O XLSX é criptografado ou usa compressão incompatível.")
                declared += info.file_size
                if declared > budget.limits.max_uncompressed_bytes:
                    _reject("uncompressed_bytes", "O XLSX excede o limite de expansão permitido.")
                if info.file_size > max(info.compress_size, 1) * budget.limits.max_zip_ratio:
                    _reject("zip_ratio", "O XLSX apresenta expansão compactada excessiva.")
                start = _member_start(raw, info, directory_start)
                intervals.append((info.header_offset, start + info.compress_size))
                xml = _MaybeXML(name, budget)
                raw.seek(start)
                _stream_member(raw, info, budget, xml)
            ordered = sorted(intervals)
            if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
                _reject("xlsx_zip", "O XLSX contém entradas internas sobrepostas.")
            if not {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"}.issubset(names):
                _reject("xlsx_zip", "O arquivo não possui a estrutura de uma planilha XLSX.")
            if budget.sheets == 0:
                _reject("empty", "O XLSX não contém abas de dados.")
    return entries


def _filled(cell):
    value = cell.value
    if value is None or cell.data_type == TYPE_ERROR:
        return False
    if cell.data_type == TYPE_NUMERIC:
        # Equivale ao conversor do pandas antes de TextParser(dtype=str).
        value = int(value) if int(value) == value else float(value)
    return not (isinstance(value, str) and value in STR_NA_VALUES)


def _disambiguate_numbers(frame, path, sheet, budget):
    """Com dtype=str, o número 12.125 e o texto "12.125" ficam iguais, mas só o texto
    pode ser milhar pt-BR. Um zero à direita preserva o valor do número e tira a dúvida."""
    candidates = [(row, column) for column in range(frame.shape[1])
                  for row, value in enumerate(frame.iloc[:, column].tolist())
                  if isinstance(value, str) and _LOOKS_LIKE_THOUSANDS.fullmatch(value)]
    if not candidates:
        return frame
    budget.check()
    typed = pd.read_excel(path, dtype=object, header=None, engine="openpyxl", sheet_name=sheet)
    if typed.shape != frame.shape:
        _reject("invalid", "Não foi possível validar a estrutura do arquivo informado.")
    for row, column in candidates:
        if isinstance(typed.iat[row, column], float):
            frame.iat[row, column] += "0"
    return frame


def _xlsx_input(path, size, budget, materialize):
    entries = _zip_preflight(path, size, budget)
    book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    best_index, best_score = None, -1
    rows_max = columns_max = observed_cells = 0
    try:
        sheets = book.worksheets
        if len(sheets) > budget.limits.max_sheets:
            _reject("sheets", "O XLSX excede o limite de abas permitido.")
        for index, sheet in enumerate(sheets):
            budget.check()
            sheet.reset_dimensions()
            score, row_count, column_count = 0, 0, 0
            for row in sheet.rows:
                budget.check()
                row_count += 1
                column_count = max(column_count, len(row))
                if row_count > budget.limits.max_rows or column_count > budget.limits.max_columns:
                    _reject("cells", "Uma aba do XLSX excede o limite de linhas ou colunas.")
                if row_count * column_count > budget.limits.max_cells_per_sheet:
                    _reject("cells", "Uma aba do XLSX excede o orçamento de células permitido.")
                for cell in row:
                    if isinstance(cell.value, str) and len(cell.value) > budget.limits.max_field_chars:
                        _reject("field_chars", "O XLSX contém uma célula acima do limite permitido.")
                    score += _filled(cell)
            observed_cells += row_count * column_count
            if observed_cells > budget.limits.max_cells_per_file:
                _reject("cells", "O XLSX excede o orçamento acumulado de células permitido.")
            rows_max, columns_max = max(rows_max, row_count), max(columns_max, column_count)
            if score > best_score:
                best_index, best_score = index, score
    finally:
        book.close()
    if best_index is None:
        _reject("empty", "O XLSX não contém abas de dados.")
    metadata = {"format": "xlsx", "sheets": budget.sheets, "zip_entries": entries,
                "uncompressed_bytes": budget.uncompressed, "cells": max(budget.cells, observed_cells),
                "rows": rows_max, "columns": columns_max, "selected_sheet": best_index,
                "filled_cells_selected": best_score}
    # Uma só aba é materializada. O parser pandas original preserva datas,
    # booleanos, erros, fórmulas cacheadas, STR_NA_VALUES e dtype=str exatamente.
    if materialize:
        budget.check()
        frame = pd.read_excel(path, dtype=str, header=None, engine="openpyxl", sheet_name=best_index)
        if frame.empty:
            _reject("empty", "O XLSX não contém dados na aba selecionada.")
        budget.check()
        return _trim(_disambiguate_numbers(frame, path, best_index, budget))
    if rows_max == 0 or columns_max == 0:
        _reject("empty", "O XLSX não contém dados nas abas.")
    return metadata


# Planilha do Excel 97-2003: o contêiner OLE2 começa por esta assinatura.
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Os leitores de .xls e de Planilha XML contam as células COM VALOR (o relatório paginado de ERP espalha
# poucas células por muitas colunas). A matriz devolvida é retangular: a área (linhas x colunas) também
# tem teto, de quatro vezes o orçamento de células por aba, para a memória não depender das posições. O
# leitor confere esse teto antes de montar a matriz: duas células distantes num arquivo de poucos bytes
# são recusadas sem ocupar a área.
_AREA_POR_CELULA = 4


def _texto_tipado(valor):
    """O valor tipado de uma célula no texto que o leitor de .xlsx (pandas, dtype=str) daria: inteiro sem
    ".0", decimal como o Python o escreve (com um zero a mais quando parece milhar pt-BR: "19.4310"), data
    como "2023-11-09 00:00:00", booleano como "True" ou "False". Vazio, número não finito e os textos que
    o pandas lê como ausentes ("", "NA", "N/A", "NULL", "nan") saem como None. O texto perde o NUL (U+0000),
    como no leitor de CSV: o motor o descarta mais adiante, e um NUL antes de "=" esconderia a fórmula da
    neutralização (o .xlsx não guarda NUL)."""
    if valor is None:
        return None
    if isinstance(valor, bool):
        return "True" if valor else "False"
    if isinstance(valor, (int, float)):
        if not math.isfinite(valor):
            return None
        if valor == int(valor):
            return str(int(valor))
        texto = repr(float(valor))
        return texto + "0" if _LOOKS_LIKE_THOUSANDS.fullmatch(texto) else texto
    if isinstance(valor, datetime):
        return str(valor)
    texto = str(valor).replace("\x00", "")
    return None if texto in STR_NA_VALUES else texto


def _typed_sheet_input(path, budget, materialize, kind):
    """.xls (BIFF8) e Planilha XML 2003: uma leitura só, com os limites aplicados durante ela."""
    if kind == "xls":
        with open(path, "rb") as stream:
            if stream.read(len(_OLE2)) != _OLE2:
                _reject("unsupported", "O arquivo .xls não é uma planilha do Excel 97-2003 (pode ser uma página "
                        "HTML ou um texto com a extensão trocada). Abra no Excel, salve como .xlsx e envie novamente.")
        import etl_biff as reader
    else:
        import etl_planilha_xml as reader
    limits = budget.limits
    try:
        sheet = reader.ler(path, max_abas=limits.max_sheets, max_linhas=limits.max_rows,
                           max_colunas=limits.max_columns, max_celulas=limits.max_cells_per_sheet,
                           max_caracteres=limits.max_field_chars,
                           max_area=_AREA_POR_CELULA * limits.max_cells_per_sheet, verificar=budget.check,
                           materializar=materialize)
    except reader.LeituraRecusada as error:
        _reject(error.codigo, error.mensagem)
    if sheet["celulas_no_arquivo"] > limits.max_cells_per_file:
        _reject("cells", "A planilha excede o orçamento acumulado de células permitido.")
    if not materialize:
        return {"format": kind, "sheets": sheet["abas"], "cells": sheet["celulas_no_arquivo"],
                "rows": sheet["linhas"], "columns": sheet["colunas"], "selected_sheet": sheet["aba"],
                "filled_cells_selected": sheet["celulas"]}
    values = sheet.pop("valores")
    missing = float("nan")  # o mesmo vazio do leitor de .xlsx
    for number, row in enumerate(values):
        if not number & 0x3FF:
            budget.check()
        for index, value in enumerate(row):
            text = _texto_tipado(value)
            row[index] = missing if text is None else text
    frame = _trim(pd.DataFrame(values, dtype=object))
    if frame.empty or frame.isna().all().all():
        _reject("empty", "A planilha não contém dados na aba selecionada.")
    frame.attrs["format"] = kind
    return frame


def _run(path, extension, limits, materialize):
    budget = _Budget(limits)
    size = _file_size(path, limits)
    extension = extension.lower().lstrip(".")
    try:
        if extension == "csv":
            result = _csv_input(path, budget, materialize)
        elif extension == "xlsx":
            result = _xlsx_input(path, size, budget, materialize)
        elif extension in ("xls", "xml"):
            result = _typed_sheet_input(path, budget, materialize, extension)
        else:
            _reject("unsupported", "Formato não suportado. Envie um arquivo CSV, XLSX ou XLS.")
    except ETLInputError:
        raise
    except MemoryError:
        raise  # O isolador distingue o orçamento de memória de formato inválido.
    except Exception:
        _reject("invalid", "Não foi possível validar a estrutura do arquivo informado.")
    if not materialize:
        result["file_bytes"] = size
    return result


def read_raw(path, limits: ETLLimits):
    """Retorna a matriz legada apenas após validação estrutural e de recursos.

    Toda leitura sai com as células neutralizadas contra fórmula (CSV/Excel injection), para
    que nenhum texto executável chegue ao banco, à tela ou a uma exportação. A neutralização
    não muda nenhum número lido pelo ETL (security_gatekeeper.neutralizar_celula).
    """
    from security_gatekeeper import neutralizar_matriz
    return neutralizar_matriz(_run(path, Path(path).suffix, limits, True))


def validate_input(path, extension, limits: ETLLimits):
    """Validação completa de formato/orçamento, sem cálculo de KPI ou banco."""
    return _run(path, extension, limits, False)
