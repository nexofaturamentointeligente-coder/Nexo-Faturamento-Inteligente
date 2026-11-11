"""SEC-005: entradas sintéticas pequenas, limites e integridade reais dos leitores."""

from dataclasses import replace
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile
import zlib

import pandas as pd

from etl_inputs import ETLInputError, read_raw, validate_input
from etl_limits import ETLLimits
from tests.etl_equivalence_support import write_csv, write_xlsx


class ETLResourceLimitsTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="nexo-etl-input-limits-")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.limits = ETLLimits()

    def csv(self, rows=None, **kwargs):
        return write_csv(self.root / "data.csv", rows or [["a", "b"], ["1", "2"]], **kwargs)

    def xlsx(self, sheets=None):
        return write_xlsx(self.root / "data.xlsx", sheets or [("Dados", [["a", "b"], ["1", "2"]])])

    def rejected(self, path, *, limits=None, extension=None, code=None):
        with self.assertRaises(ETLInputError) as captured:
            validate_input(path, extension or path.suffix, limits or self.limits)
        error = captured.exception
        self.assertEqual(str(error), error.safe_message)
        self.assertNotIn(str(path), str(error))
        if code is not None:
            self.assertEqual(error.code, code)
        return error

    def _members(self, path):
        with zipfile.ZipFile(path) as archive:
            return [(info.filename, archive.read(info)) for info in archive.infolist()]

    def _zip(self, members, *, compression=zipfile.ZIP_DEFLATED):
        path = self.root / "modified.xlsx"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w", compression=compression) as archive:
                for name, content in members:
                    archive.writestr(name, content)
        return path

    def _worksheet(self, content):
        original = self.xlsx()
        return self._zip([(name, content if name == "xl/worksheets/sheet1.xml" else data)
                          for name, data in self._members(original)])

    def test_small_csv_preserves_values(self):
        frame = read_raw(self.csv(), self.limits)
        self.assertEqual(frame.values.tolist(), [["a", "b"], ["1", "2"]])

    def test_small_xlsx_preserves_values(self):
        frame = read_raw(self.xlsx(), self.limits)
        self.assertEqual(frame.values.tolist(), [["a", "b"], ["1", "2"]])

    def test_zero_byte_csv_is_rejected(self):
        path = self.root / "empty.csv"; path.touch()
        self.rejected(path, code="empty")

    def test_empty_workbook_is_rejected(self):
        self.rejected(self.xlsx([("Empty", [])]), code="empty")

    def test_file_exactly_at_byte_limit_is_accepted(self):
        path = self.csv()
        self.assertEqual(validate_input(path, "csv", replace(self.limits, max_file_bytes=path.stat().st_size))["file_bytes"], path.stat().st_size)

    def test_file_one_byte_over_limit_is_rejected(self):
        path = self.csv()
        self.rejected(path, limits=replace(self.limits, max_file_bytes=path.stat().st_size - 1), code="file_bytes")

    def test_missing_file_error_has_no_internal_path(self):
        self.rejected(self.root / "PRIVATE_PATH_SENTINEL.csv", code="unavailable")

    def test_directory_cannot_be_used_as_input(self):
        self.rejected(self.root, extension="csv", code="invalid_file")

    def test_xls_that_is_not_an_excel_workbook_is_rejected(self):
        # O .xls do Excel 97-2003 foi habilitado de propósito na unificação de 11/2026 (etl_biff, testes em
        # test_etl_biff.py e test_cobertura_leiautes_erp.py). O que só tem a extensão continua fora.
        path = self.root / "data.xls"; path.write_bytes(b"synthetic-old-format")
        self.rejected(path, code="unsupported")
        ole = self.root / "container.xls"; ole.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + bytes(504))
        self.rejected(ole, code="invalid")

    def test_xml_that_is_not_a_spreadsheet_is_rejected(self):
        path = self.root / "data.xml"; path.write_bytes(b"<?xml version=\"1.0\"?><root><a>1</a></root>")
        self.rejected(path, code="unsupported")

    def test_unsupported_extension_is_rejected(self):
        self.rejected(self.csv(), extension="json", code="unsupported")

    def test_csv_exact_row_limit_is_accepted(self):
        self.assertEqual(validate_input(self.csv(), "csv", replace(self.limits, max_rows=2))["rows"], 2)

    def test_csv_one_row_over_limit_is_rejected(self):
        self.rejected(self.csv(), limits=replace(self.limits, max_rows=1), code="rows")

    def test_csv_exact_column_limit_is_accepted(self):
        self.assertEqual(validate_input(self.csv(), "csv", replace(self.limits, max_columns=2))["columns"], 2)

    def test_csv_one_column_over_limit_is_rejected(self):
        self.rejected(self.csv(), limits=replace(self.limits, max_columns=1), code="columns")

    def test_csv_exact_rectangular_cell_budget_is_accepted(self):
        self.assertEqual(validate_input(self.csv(), "csv", replace(self.limits, max_cells_per_file=4))["cells"], 4)

    def test_csv_padding_is_included_in_cell_budget(self):
        path = self.csv([["Decoration"], ["a", "b"], ["1", "2"]])
        self.rejected(path, limits=replace(self.limits, max_cells_per_file=5), code="cells")

    def test_csv_exact_field_limit_is_accepted(self):
        path = self.csv([["aa", "bb"]])
        self.assertEqual(read_raw(path, replace(self.limits, max_field_chars=2)).values.tolist(), [["aa", "bb"]])

    def test_csv_giant_field_is_rejected_before_dataframe(self):
        path = self.csv([["a", "b"], ["x" * 20, "2"]])
        with patch("etl_inputs.pd.DataFrame", side_effect=AssertionError("No allocation")):
            self.rejected(path, limits=replace(self.limits, max_field_chars=19), code="field_chars")

    def test_csv_record_exact_limit_includes_line_ending(self):
        path = self.root / "record.csv"; path.write_text("a;b\n1;2\n")
        self.assertEqual(validate_input(path, "csv", replace(self.limits, max_record_chars=4))["rows"], 2)

    def test_csv_record_over_limit_is_rejected(self):
        path = self.root / "record.csv"; path.write_text("a;b\n1;2\n")
        self.rejected(path, limits=replace(self.limits, max_record_chars=3), code="record_chars")

    def test_csv_multiline_quoted_record_has_cumulative_budget(self):
        path = self.csv([["a", "b"], ["one\ntwo\nthree", "2"]])
        self.rejected(path, limits=replace(self.limits, max_record_chars=12), code="record_chars")

    def test_csv_irregular_rows_preserve_none_padding(self):
        frame = read_raw(self.csv([["Decoration"], ["a", "b"], ["1", "2"]]), self.limits)
        self.assertEqual(frame.iloc[0, 0], "Decoration")
        self.assertIsNone(frame.iloc[0, 1])

    def test_csv_quotes_line_breaks_and_nul_keep_legacy_values(self):
        frame = read_raw(self.csv([["a", "b"], ["one;\nline\x00", "2"]]), self.limits)
        self.assertEqual(frame.iloc[1, 0], "one;\nline")

    def test_csv_all_supported_delimiters_are_detected(self):
        for delimiter in (";", ",", "\t", "|"):
            with self.subTest(delimiter=delimiter):
                path = self.csv(delimiter=delimiter)
                self.assertEqual(validate_input(path, "csv", self.limits)["separator"], delimiter)

    def test_csv_late_wide_header_drives_full_file_separator_choice(self):
        rows = [["decorative"] for _ in range(70)] + [["a", "b", "c"], ["1", "2", "3"]]
        self.assertEqual(validate_input(self.csv(rows, delimiter="|"), "csv", self.limits)["columns"], 3)

    def test_csv_encoding_order_and_latin1_fallback(self):
        for content, encoding in ((b"a;b\nCaf\xc3\xa9;2\n", "utf-8-sig"),
                                  (b"a;b\nCaf\xe9;2\n", "cp1252"),
                                  (b"a;b\nA\x81;2\n", "latin1")):
            path = self.root / "encoding.csv"; path.write_bytes(content)
            with self.subTest(encoding=encoding):
                self.assertEqual(validate_input(path, "csv", self.limits)["encoding"], encoding)

    def test_known_binary_formats_cannot_impersonate_csv_with_incidental_delimiters(self):
        for prefix in (b"%PDF-1.4\n", b"\x89PNG\r\n\x1a\n", b"PK\x03\x04",
                       b"GIF89a", b"\xff\xd8\xff", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            path = self.root / "binary.csv"
            path.write_bytes(prefix + b"Produto;Quantidade;Valor total\nItem;1;10\n")
            with self.subTest(prefix=prefix):
                self.rejected(path, code="csv_incompatible")

    def test_csv_validation_does_not_allocate_dataframe(self):
        path = self.csv()
        with patch("etl_inputs.pd.DataFrame", side_effect=AssertionError("No DataFrame on upload")):
            self.assertEqual(validate_input(path, "csv", self.limits)["cells"], 4)

    def test_csv_reader_never_changes_process_global_field_limit(self):
        import csv
        previous = csv.field_size_limit()
        read_raw(self.csv(), self.limits)
        self.assertEqual(csv.field_size_limit(), previous)

    def test_xlsx_exact_sheet_limit_is_accepted(self):
        path = self.xlsx([("A", [["a", "b"]]), ("B", [["1", "2"]])])
        self.assertEqual(validate_input(path, "xlsx", replace(self.limits, max_sheets=2))["sheets"], 2)

    def test_xlsx_one_sheet_over_limit_is_rejected(self):
        path = self.xlsx([("A", [["a", "b"]]), ("B", [["1", "2"]])])
        self.rejected(path, limits=replace(self.limits, max_sheets=1), code="sheets")

    def test_xlsx_cover_and_tie_select_first_maximum_sheet(self):
        path = self.xlsx([("Cover", [["Cover"]]), ("A", [["a", "b"]]), ("B", [["1", "2"]])])
        self.assertEqual(read_raw(path, self.limits).values.tolist(), [["a", "b"]])

    def test_xlsx_only_winner_is_materialized_by_pandas(self):
        path = self.xlsx([("Cover", [["Cover"]]), ("Data", [["a", "b"], ["1", "2"]])])
        original = pd.read_excel
        with patch("etl_inputs.pd.read_excel", wraps=original) as reader:
            read_raw(path, self.limits)
        reader.assert_called_once()
        self.assertEqual(reader.call_args.kwargs["sheet_name"], 1)

    def test_xlsx_validation_does_not_materialize_any_pandas_sheet(self):
        path = self.xlsx()
        with patch("etl_inputs.pd.read_excel", side_effect=AssertionError("No DataFrame on upload")):
            self.assertEqual(validate_input(path, "xlsx", self.limits)["filled_cells_selected"], 4)

    def test_xlsx_exact_rows_columns_and_cells_are_accepted(self):
        limits = replace(self.limits, max_rows=2, max_columns=2, max_cells_per_sheet=4, max_cells_per_file=4)
        self.assertEqual(validate_input(self.xlsx(), "xlsx", limits)["cells"], 4)

    def test_xlsx_one_row_above_maximum_is_rejected(self):
        self.rejected(self.xlsx(), limits=replace(self.limits, max_rows=1), code="rows")

    def test_xlsx_one_column_above_maximum_is_rejected(self):
        self.rejected(self.xlsx(), limits=replace(self.limits, max_columns=1), code="columns")

    def test_xlsx_sparse_coordinates_include_rectangular_allocation(self):
        content = b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="20"><c r="D20" t="n"><v>2</v></c></row></sheetData></worksheet>'
        self.rejected(self._worksheet(content), limits=replace(self.limits, max_cells_per_sheet=79), code="cells")

    def test_xlsx_cumulative_cells_include_all_sheets(self):
        path = self.xlsx([("A", [["a", "b"], ["1", "2"]]), ("B", [["a", "b"], ["1", "2"]])])
        self.rejected(path, limits=replace(self.limits, max_cells_per_file=7), code="cells")

    def test_xlsx_field_limit_applies_before_openpyxl(self):
        path = self.xlsx([("Data", [["x" * 20, "b"]])])
        with patch("etl_inputs.load_workbook", side_effect=AssertionError("No parser allocation")):
            self.rejected(path, limits=replace(self.limits, max_field_chars=19), code="field_chars")

    def test_zip_exact_entry_limit_is_accepted(self):
        path = self.xlsx(); count = len(self._members(path))
        self.assertEqual(validate_input(path, "xlsx", replace(self.limits, max_zip_entries=count))["zip_entries"], count)

    def test_zip_excess_entries_rejected_before_workbook_load(self):
        path = self.xlsx(); count = len(self._members(path))
        self.rejected(path, limits=replace(self.limits, max_zip_entries=count - 1), code="zip_entries")

    def test_zip_exact_uncompressed_size_is_accepted(self):
        path = self.xlsx(); expanded = sum(len(data) for _, data in self._members(path))
        self.assertEqual(validate_input(path, "xlsx", replace(self.limits, max_uncompressed_bytes=expanded))["uncompressed_bytes"], expanded)

    def test_zip_uncompressed_size_over_budget_is_rejected(self):
        path = self.xlsx(); expanded = sum(len(data) for _, data in self._members(path))
        self.rejected(path, limits=replace(self.limits, max_uncompressed_bytes=expanded - 1), code="uncompressed_bytes")

    def test_zip_compression_bomb_is_small_and_rejected(self):
        path = self._zip(self._members(self.xlsx()) + [("oversized.bin", b"A" * 20000)])
        self.assertLess(path.stat().st_size, 10000)
        self.rejected(path, code="zip_ratio")

    def test_zip_duplicate_member_is_rejected(self):
        members = self._members(self.xlsx()); self.rejected(self._zip(members + [members[0]]), code="xlsx_zip")

    def test_zip_paths_are_never_extracted(self):
        path = self._zip(self._members(self.xlsx()) + [("../PRIVATE_SENTINEL", b"never extract")])
        self.rejected(path, code="xlsx_zip")
        self.assertFalse((self.root.parent / "PRIVATE_SENTINEL").exists())

    def test_zip_unsupported_compression_is_rejected(self):
        self.rejected(self._zip(self._members(self.xlsx()), compression=zipfile.ZIP_BZIP2), code="xlsx_zip")

    def test_zip_truncation_is_controlled(self):
        path = self.xlsx(); path.write_bytes(path.read_bytes()[:-10])
        self.rejected(path, code="xlsx_zip")

    def test_non_workbook_zip_is_not_accepted_by_extension(self):
        self.rejected(self._zip([("content.txt", b"synthetic")]), code="xlsx_zip")

    def test_non_zip_bytes_cannot_impersonate_xlsx(self):
        path = self.root / "not.xlsx"; path.write_bytes(b"a;b\n1;2\n")
        self.rejected(path, code="xlsx_zip")

    def test_xml_dtd_and_entities_are_rejected(self):
        content = b'<!DOCTYPE worksheet [<!ENTITY x "synthetic">]><worksheet><sheetData><row r="1"><c r="A1"><v>&x;</v></c></row></sheetData></worksheet>'
        self.rejected(self._worksheet(content), code="xlsx_xml")

    def test_xml_malformed_structure_is_controlled(self):
        self.rejected(self._worksheet(b"<worksheet><broken>"), code="invalid")

    def test_xml_invalid_cell_reference_is_rejected(self):
        content = b'<worksheet><sheetData><row r="1"><c r="PRIVATE"><v>1</v></c></row></sheetData></worksheet>'
        self.rejected(self._worksheet(content), code="xlsx_xml")

    def test_xml_duplicate_cells_cannot_hide_actual_cell_budget(self):
        content = b'<worksheet><sheetData><row r="1"><c r="A1"><v>1</v></c><c r="A1"><v>2</v></c></row></sheetData></worksheet>'
        self.rejected(self._worksheet(content), limits=replace(self.limits, max_cells_per_sheet=1), code="cells")

    def test_xml_extreme_sparse_row_rejected_before_openpyxl(self):
        content = b'<worksheet><sheetData><row r="1048576"><c r="A1048576"><v>1</v></c></row></sheetData></worksheet>'
        with patch("etl_inputs.load_workbook", side_effect=AssertionError("Do not allocate sparse rows")):
            self.rejected(self._worksheet(content), code="rows")

    def test_xml_renamed_sheet_cannot_bypass_coordinate_budget(self):
        members = self._members(self.xlsx())
        content = b'<worksheet><sheetData><row r="100001"><c r="A100001"><v>1</v></c></row></sheetData></worksheet>'
        members = [("custom/sheet.xml", content) if name == "xl/worksheets/sheet1.xml" else (name, data) for name, data in members]
        self.rejected(self._zip(members), code="rows")

    def test_raw_decompression_rejects_forged_sizes_even_when_zipfile_accepts_prefix(self):
        content = b"x" * 100
        path = self._zip([("payload.bin", content)])
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("payload.bin"); central = archive.start_dir
        data = bytearray(path.read_bytes())
        prefix_crc = zlib.crc32(content[:50])
        struct.pack_into("<I", data, info.header_offset + 14, prefix_crc)
        struct.pack_into("<I", data, info.header_offset + 22, 50)
        struct.pack_into("<I", data, central + 16, prefix_crc)
        struct.pack_into("<I", data, central + 24, 50)
        path.write_bytes(data)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read("payload.bin"), content[:50])
        self.rejected(path, code="uncompressed_bytes")

    def test_crc_corruption_is_detected_independently_of_metadata_agreement(self):
        path = self._zip([("payload.bin", b"synthetic")])
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("payload.bin"); central = archive.start_dir
        data = bytearray(path.read_bytes())
        struct.pack_into("<I", data, info.header_offset + 14, 1)
        struct.pack_into("<I", data, central + 16, 1)
        path.write_bytes(data)
        self.rejected(path, code="xlsx_zip")

    def test_encryption_flag_is_rejected_without_attempting_decryption(self):
        path = self._zip([("payload.bin", b"synthetic")])
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("payload.bin"); central = archive.start_dir
        data = bytearray(path.read_bytes())
        struct.pack_into("<H", data, info.header_offset + 6, 1)
        struct.pack_into("<H", data, central + 8, 1)
        path.write_bytes(data)
        self.rejected(path, code="xlsx_zip")

    def test_csv_respects_single_sheet_budget_as_well_as_file_budget(self):
        self.rejected(self.csv(), limits=replace(self.limits, max_cells_per_sheet=3), code="cells")

    def test_real_central_entry_count_checked_before_zipfile_allocation(self):
        path = self._zip([(f"small-{index}.bin", b"x") for index in range(5)])
        data = bytearray(path.read_bytes())
        end = data.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", data, end + 8, 1, 1)
        path.write_bytes(data)
        with patch("etl_inputs.zipfile.ZipFile", side_effect=AssertionError("No ZipInfo allocation")):
            self.rejected(path, limits=replace(self.limits, max_zip_entries=4), code="zip_entries")

    def test_valid_workbook_part_aliases_do_not_bypass_accumulated_cells(self):
        path = self.xlsx([("A", [["a", "b"], ["1", "2"]]),
                          ("B", [["c", "d"], ["3", "4"]])])
        members = []
        for name, raw in self._members(path):
            if name.endswith(".xml") or name.endswith(".rels"):
                raw = raw.replace(b"sheet1.xml", b"sheet1.bin").replace(b"sheet2.xml", b"sheet2.bin")
            name = name.replace("sheet1.xml", "sheet1.bin").replace("sheet2.xml", "sheet2.bin")
            members.append((name, raw))
        aliased = self._zip(members)
        # Esquema válido que pandas/openpyxl realmente seguem por relacionamento.
        self.assertEqual(pd.read_excel(aliased, sheet_name=1, header=None).shape, (2, 2))
        self.rejected(aliased, limits=replace(self.limits, max_cells_per_file=7), code="cells")

    def test_shared_strings_xml_with_binary_extension_still_rejects_dtd(self):
        members = []
        for name, raw in self._members(self.xlsx()):
            if name == "[Content_Types].xml":
                raw = raw.replace(b"</Types>", b'<Override PartName="/xl/strings.bin" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
            members.append((name, raw))
        members.append(("xl/strings.bin", b'<!DOCTYPE sst [<!ENTITY x "synthetic">]><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>&x;</t></si></sst>'))
        path = self._zip(members)
        with patch("etl_inputs.load_workbook", side_effect=AssertionError("No XML entity parsing")):
            self.rejected(path, code="xlsx_xml")

    def test_library_exception_details_are_sanitized(self):
        path = self.xlsx()
        with patch("etl_inputs.load_workbook", side_effect=AttributeError("PRIVATE_LIBRARY_SENTINEL")):
            error = self.rejected(path, code="invalid")
        self.assertNotIn("PRIVATE_LIBRARY_SENTINEL", str(error))

    def test_cooperative_deadline_checked_during_input_scan(self):
        path = self.csv()
        with patch("etl_inputs.time.monotonic", side_effect=[0, 2]):
            self.rejected(path, limits=replace(self.limits, timeout_seconds=1), code="timeout")

    def test_invalid_limit_configuration_never_disables_protection(self):
        for value in (0, -1, True, 20 * 1024 * 1024 + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.limits, max_file_bytes=value)
        with self.assertRaises(ValueError):
            ETLLimits.from_mapping({"unknown": 1})


    def test_renamed_xml_split_bom_and_utf16_prefix_cannot_bypass_entity_guard(self):
        from etl_inputs import _Budget, _MaybeXML
        payload = '<!DOCTYPE worksheet [<!ENTITY x "forbidden">]><worksheet>&x;</worksheet>'
        for encoding, bom in (("utf-8", b"\xef\xbb\xbf"), ("utf-16-be", b""),
                              ("utf-16-le", b"\xff\xfe")):
            raw = bom + payload.encode(encoding)
            with self.subTest(encoding=encoding):
                check = _MaybeXML("xl/renamed.bin", _Budget(self.limits))
                with self.assertRaises(ETLInputError) as captured:
                    for byte in raw:
                        check.feed(bytes([byte]))
                    check.finish()
                self.assertEqual(captured.exception.code, "xlsx_xml")


if __name__ == "__main__":
    unittest.main()
