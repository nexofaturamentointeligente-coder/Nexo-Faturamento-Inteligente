"""SEC-005: equivalência com resultados capturados ANTES da correção.

Expected gerado do blob do ETL em ff148796 com 22 pares inteiramente sintéticos.
Não contém cópia do motor antigo, planilhas reais ou dados de clientes. Todos os
campos de KPIs, Curva ABC, período, telemetria e alertas são comparados.

Única revisão do expected: em sales_derived_mixed_money, a Curva ABC e os alertas seguem
as regras financeiras de 11/2026 (ver test_etl_regras_financeiras): itens zerados ou
negativos entram na curva. Os KPIs seguem iguais.
"""

import json
import math
from pathlib import Path
import tempfile
import unittest

from tests.etl_equivalence_support import fixture_pairs, plain, raw_snapshot, reader_fixtures
import etl_processor


EXPECTED = Path(__file__).parent / "fixtures" / "sec005_financial_expected.json"


class FinancialEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="nexo-sec005-equivalence-")
        cls.addClassCleanup(cls.work.cleanup)
        cls.pairs = fixture_pairs(Path(cls.work.name))
        cls.readers = reader_fixtures(Path(cls.work.name) / "raw")
        cls.expected = json.loads(EXPECTED.read_text())
        if cls.expected["source_commit"] != "ff148796d9b579418b3782d472eb44fb0025fe6f":
            raise AssertionError("Referência financeira original incorreta.")

    def _assert_equivalent(self, actual, expected, path="result"):
        if isinstance(expected, dict):
            self.assertIsInstance(actual, dict, path)
            self.assertEqual(set(actual), set(expected), path)
            for key in expected:
                self._assert_equivalent(actual[key], expected[key], path + "." + key)
        elif isinstance(expected, list):
            self.assertIsInstance(actual, list, path)
            self.assertEqual(len(actual), len(expected), path)
            for index, value in enumerate(expected):
                self._assert_equivalent(actual[index], value, path + f"[{index}]")
        elif isinstance(expected, float):
            self.assertIsInstance(actual, (int, float), path)
            # O motor usa float64 e soma vetorizada; 1e-9 absoluto/1e-12 relativo
            # admite somente ruído IEEE-754, sem arredondar valores financeiros.
            self.assertTrue(math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-9),
                            f"{path}: {actual!r} != {expected!r}")
        else:
            self.assertEqual(actual, expected, path)

    def _financial(self, case):
        sales, purchases = self.pairs[case]
        actual = etl_processor.calcular_kpis_em_memoria(str(sales), str(purchases))
        self.assertTrue(actual.sucesso)
        self._assert_equivalent(plain(actual._asdict()), self.expected["financial"][case], case)

    def test_c1_hemorragia_nf_level_and_alerts(self):
        self._financial("c1_hemorragia_nf")

    def test_c2_refem_derived_purchases_and_cover_sheet(self):
        self._financial("c2_refem_unitario")

    def test_c3_ruptura_verbose_headers_and_zero_purchase(self):
        self._financial("c3_ruptura_verboso")

    def test_c4_abc_exact_thresholds_and_equilibrium(self):
        self._financial("c4_equilibrio_abc")

    def test_all_supported_csv_separators_and_encoding_precedence(self):
        for case in ("csv_semicolon_utf8sig", "csv_comma_latin1", "csv_tab_cp1252",
                     "csv_pipe_utf8", "csv_latin1_fallback"):
            with self.subTest(case=case):
                self._financial(case)

    def test_csv_quotes_multiline_nul_and_late_header(self):
        for case in ("csv_quoted_multiline", "csv_nul_removal", "csv_late_delimiter_header"):
            with self.subTest(case=case):
                self._financial(case)

    def test_compound_and_shifted_headers_with_footer(self):
        for case in ("header_compound", "header_shifted_footer"):
            with self.subTest(case=case):
                self._financial(case)

    def test_explicit_totals_normalized_products_and_raw_cost_alerts(self):
        self._financial("explicit_total_normalization_alerts")

    def test_derived_sales_brazilian_and_international_money(self):
        self._financial("sales_derived_mixed_money")

    def test_purchase_products_total_takes_priority(self):
        self._financial("purchase_products_total_priority")

    def test_xlsx_sheet_tie_keeps_original_sheet_order(self):
        self._financial("xlsx_sheet_tie")

    def test_xlsx_cached_formulas_and_na_strings(self):
        self._financial("xlsx_formula_cached_na")

    def test_period_priority_and_reversed_dates(self):
        for case in ("reversed_period_dates", "purchase_period_fallback"):
            with self.subTest(case=case):
                self._financial(case)

    def test_single_product_keeps_existing_ranking_policy(self):
        self._financial("one_product_rankings")

    def test_raw_input_matrices_preserve_values_and_missing_cells(self):
        for case, pair in self.pairs.items():
            for kind, path in zip(("sales", "purchases"), pair):
                with self.subTest(case=case, kind=kind):
                    actual = raw_snapshot(etl_processor._ler_bruto(str(path)))
                    self._assert_equivalent(actual, self.expected["raw"][case + "_" + kind])
        for case, path in self.readers.items():
            with self.subTest(case=case):
                self._assert_equivalent(raw_snapshot(etl_processor._ler_bruto(str(path))),
                                        self.expected["raw"][case])


if __name__ == "__main__":
    unittest.main()
