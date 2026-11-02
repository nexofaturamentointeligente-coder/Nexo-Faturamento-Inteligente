"""SEC-005: leitores tabulares limitados, sem banco, extração ZIP ou dados em logs.

As verificações cooperativas complementam o isolamento de processo do chamador;
não constituem, sozinhas, timeout rígido de uma biblioteca nativa.
"""

import csv
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


def _csv_pass(path, encoding, separator, budget, *, materialize=False, validate_fields=False):
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


def _csv_input(path, budget, materialize):
    _csv_signature(path)
    selected = None
    for encoding in ENCODINGS:
        best = None
        decode_failed = False
        for separator in SEPARATORS:
            try:
                rows, columns, _ = _csv_pass(path, encoding, separator, budget)
            except UnicodeDecodeError:
                decode_failed = True
                break
            except csv.Error:
                # O leitor anterior também descartava separadores que não
                # podiam ser interpretados pelo csv.reader padrão.
                continue
            if best is None or columns > best[2]:
                best = (separator, rows, columns)
        if decode_failed:
            continue
        if best is not None and best[2] > 1:
            selected = (encoding, *best)
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
    rectangular = [row + [None] * (columns - len(row)) for row in values]
    return _trim(pd.DataFrame(rectangular, dtype=object))


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


def _run(path, extension, limits, materialize):
    budget = _Budget(limits)
    size = _file_size(path, limits)
    extension = extension.lower().lstrip(".")
    try:
        if extension == "csv":
            result = _csv_input(path, budget, materialize)
        elif extension == "xlsx":
            result = _xlsx_input(path, size, budget, materialize)
        elif extension == "xls":
            _reject("unsupported", "Formato .xls não suportado. Converta o arquivo para .xlsx ou .csv.")
        else:
            _reject("unsupported", "Formato não suportado. Envie um arquivo CSV ou XLSX.")
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
    """Retorna a matriz legada apenas após validação estrutural e de recursos."""
    return _run(path, Path(path).suffix, limits, True)


def validate_input(path, extension, limits: ETLLimits):
    """Validação completa de formato/orçamento, sem cálculo de KPI ou banco."""
    return _run(path, extension, limits, False)
