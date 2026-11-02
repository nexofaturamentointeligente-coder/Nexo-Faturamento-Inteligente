"""Regras financeiras do ETL: conversão de dinheiro, cancelamentos e devoluções, Curva ABC.

Chama _calcular_kpis_local direto, sem o processo isolado (que só existe no Linux),
para estas regras poderem ser conferidas em qualquer máquina. Dados sintéticos.
"""

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import etl_processor
from etl_limits import ETLLimits
from etl_processor import ETLValidationError, _classificar_curva_abc, _parse_valor, converter_valor_monetario
from tests.contratos_support import ContratosCase
from tests.etl_equivalence_support import PURCHASES, write_csv, write_xlsx

CABECALHO = ["produto", "quantidade", "faturamento"]


def soma_exata(valores):
    return sum(Decimal(str(valor)) for valor in valores)


def grupo_de(valores):
    """DataFrame já agregado por produto, como _classificar_curva_abc recebe."""
    nomes = [f"Produto {indice:05d}" for indice in range(1, len(valores) + 1)]
    return pd.DataFrame({"_prod_norm": [nome.upper() for nome in nomes], "nome": nomes,
                         "qtde": [1.0] * len(valores), "fat": [float(valor) for valor in valores]})


class _CasoComArquivos(unittest.TestCase):
    def setUp(self):
        trabalho = tempfile.TemporaryDirectory(prefix="nexo-etl-regras-")
        self.addCleanup(trabalho.cleanup)
        self.pasta = Path(trabalho.name)
        self.compras = write_csv(self.pasta / "compras.csv", PURCHASES)

    def calcular(self, vendas, compras=None):
        caminho = write_csv(self.pasta / "vendas.csv", vendas)
        return etl_processor._calcular_kpis_local(str(caminho), str(compras or self.compras))

    def faturamento(self, *valores):
        linhas = [[f"Produto {i}", "1", valor] for i, valor in enumerate(valores)]
        return self.calcular([CABECALHO, *linhas])


class ConversaoMonetariaTests(unittest.TestCase):
    def test_formatos_aceitos(self):
        casos = [
            ("12.500,50", 12500.5), ("R$ 12.500,50", 12500.5), ("R$\xa012.500,50", 12500.5),
            ("r$12.500,50", 12500.5), ("​12.500,50﻿", 12500.5), (" 12.500,50\t", 12500.5),
            ("12 500,50", 12500.5), ("'12.500,50", 12500.5), ("1.234.567,89", 1234567.89),
            ("12.500", 12500.0), ("1.000", 1000.0), ("999.999", 999999.0), ("1.234.567", 1234567.0),
            ("1234,56", 1234.56), ("12,5", 12.5), ("12,500", 12.5), ("0,50", 0.5), (",5", 0.5),
            ("1,234.56", 1234.56), ("1,234,567", 1234567.0), ("1,234,567.89", 1234567.89),
            ("1234.56", 1234.56), ("12.5", 12.5), ("12.50", 12.5), ("1234.567", 1234.567),
            ("12.5000", 12.5), (".5", 0.5), ("100", 100.0), ("0", 0.0),
            ("(1.234,56)", -1234.56), ("(R$ 10,00)", -10.0), ("-12.500", -12500.0), ("- 10,00", -10.0),
            ("-R$ 10,00", -10.0), ("R$ -10,00", -10.0), ("10,00-", -10.0), ("−10,00", -10.0),
            ("+10,00", 10.0), ("1E+05", 100000.0), ("1,5e3", 1500.0),
            (1234.5, 1234.5), (12500, 12500.0), (Decimal("12.50"), 12.5),
        ]
        for bruto, esperado in casos:
            with self.subTest(bruto=bruto):
                self.assertEqual(converter_valor_monetario(bruto), esperado)

    def test_zero_a_esquerda_nunca_e_milhar(self):
        # A regra de 3 casas leria 0.125 como 125 e 0.500 como 500.
        self.assertEqual(converter_valor_monetario("0.125"), 0.125)
        self.assertEqual(converter_valor_monetario("0.500"), 0.5)
        self.assertEqual(converter_valor_monetario("-0.125"), -0.125)

    def test_nulos_e_invalidos_viram_zero_sem_interromper(self):
        casos = [None, "", "   ", "\xa0", "-", "--", "nan", "NaN", "None", "null", "N/A", "R$", "abc",
                 "12abc", "12,50 UN", "1.2.3", "1,2,3", "12.34.567", "1.234,56,78", "1,234,56.78", "--10",
                 "1-2", "2026-01-02", "12%", "()", ".", ",", True, False, float("nan"), float("inf"),
                 Decimal("NaN"), "1e999", object(), []]
        for bruto in casos:
            with self.subTest(bruto=repr(bruto)):
                self.assertEqual(converter_valor_monetario(bruto), 0.0)

    def test_zero_negativo_nao_existe(self):
        self.assertEqual(str(converter_valor_monetario("-0,00")), "0.0")

    def test_quantidade_continua_com_ponto_decimal(self):
        # "1.500" numa coluna de quantidade é 1,5 kg: a regra de milhar é só de dinheiro.
        self.assertEqual(_parse_valor("1.500"), 1.5)
        self.assertEqual(_parse_valor("1,5"), 1.5)


class ValoresNasPlanilhasTests(_CasoComArquivos):
    def test_formato_brasileiro_de_ponta_a_ponta(self):
        resultado = self.faturamento("12.500,50", "12.500", "R$ 1.000,00", "0,50")
        self.assertEqual(resultado.kpis["faturamento_total"], 12500.5 + 12500 + 1000 + 0.5)

    def test_valor_sozinho_com_3_casas_e_lido_como_milhar_e_avisa(self):
        resultado = self.faturamento("12.500", "1.000")
        self.assertEqual(resultado.kpis["faturamento_total"], 13500.0)
        self.assertEqual(resultado.telemetria["valores_monetarios"], {"lidos_como_milhar": 2})
        self.assertTrue(any("lidos como milhar" in alerta for alerta in resultado.alertas))

    def test_virgula_decimal_na_coluna_confirma_o_milhar(self):
        resultado = self.faturamento("12.500", "1.234,50", "12.5")
        self.assertEqual(resultado.kpis["faturamento_total"], 12500 + 1234.5 + 12.5)

    def test_coluna_com_ponto_decimal_nao_multiplica_por_mil(self):
        resultado = self.faturamento("12.500", "3.75", "1234.5")
        self.assertEqual(resultado.kpis["faturamento_total"], 12.5 + 3.75 + 1234.5)
        self.assertEqual(resultado.telemetria["valores_monetarios"], {"lidos_como_decimal": 1})
        self.assertTrue(any("lidos como decimal" in alerta for alerta in resultado.alertas))

    def test_coluna_com_tudo_em_3_casas_acima_do_milhar_e_decimal(self):
        # "1500.000" não é milhar válido: a coluna mostra que usa 3 casas decimais.
        resultado = self.faturamento("1500.000", "100.000")
        self.assertEqual(resultado.kpis["faturamento_total"], 1600.0)

    def test_valor_invalido_entra_como_zero_e_o_restante_e_calculado(self):
        resultado = self.faturamento("abc", "100,00", "")
        self.assertEqual(resultado.kpis["faturamento_total"], 100.0)
        self.assertEqual(resultado.telemetria["valores_monetarios"], {"invalidos": 1})
        self.assertTrue(any("não puderam ser interpretados" in alerta for alerta in resultado.alertas))

    def test_quantidade_com_3_casas_nao_infla_o_total_derivado(self):
        resultado = self.calcular([["produto", "quantidade", "preco_unitario"], ["Arroz", "1.500", "10,00"]])
        self.assertEqual(resultado.kpis["faturamento_total"], 15.0)
        self.assertEqual(resultado.kpis["produto_mais_vendido_quantidade"], 1.5)

    def test_compras_seguem_a_mesma_regra_de_dinheiro(self):
        compras = write_csv(self.pasta / "compras2.csv", [["fornecedor", "total_compra"], ["Fornecedor sintético", "12.500"]])
        resultado = self.calcular([CABECALHO, ["Arroz", "1", "100,00"]], compras)
        self.assertEqual(resultado.kpis["total_comprado"], 12500.0)

    def test_numero_de_verdade_no_excel_nunca_e_milhar(self):
        vendas = write_xlsx(self.pasta / "vendas.xlsx", [("Dados", [CABECALHO, ["Arroz", 1, 12.125], ["Feijão", 2, -5.899]])])
        resultado = etl_processor._calcular_kpis_local(str(vendas), str(self.compras))
        self.assertEqual(resultado.kpis["faturamento_total"], 12.125 - 5.899)

    def test_texto_no_excel_segue_a_regra_de_milhar(self):
        vendas = write_xlsx(self.pasta / "vendas.xlsx", [("Dados", [CABECALHO, ["Arroz", 1, "12.500"], ["Feijão", 2, "1.000,50"]])])
        resultado = etl_processor._calcular_kpis_local(str(vendas), str(self.compras))
        self.assertEqual(resultado.kpis["faturamento_total"], 12500 + 1000.5)

    def test_excel_sem_valor_ambiguo_e_lido_uma_vez_so(self):
        vendas = write_xlsx(self.pasta / "vendas.xlsx", [("Dados", [CABECALHO, ["Arroz", 1, 12.5]])])
        original = pd.read_excel
        with patch("etl_inputs.pd.read_excel", wraps=original) as leitor:
            etl_processor._ler_bruto(str(vendas))
        leitor.assert_called_once()


class CancelamentosEDevolucoesTests(_CasoComArquivos):
    COLUNAS = [*CABECALHO, "status", "operacao"]

    def test_faturamento_liquido_real(self):
        resultado = self.calcular([self.COLUNAS,
            ["Arroz", "10", "1.000,00", "CONCLUIDA", "VENDA"],
            ["Arroz", "1", "100,00", "Concluída", "Devolução"],
            ["Feijão", "5", "500,00", "Cancelada", "VENDA"],
            ["Óleo", "2", "200,00", "CANC", "VENDA"],
            ["Sal", "3", "300,00", "ESTORNADO", "VENDA"],
            ["Açúcar", "4", "400,00", "Inutilizada", "VENDA"],
            ["Café", "5", "250,00", "NÃO CANCELADA", "VENDA"],
            ["Milho", "2", "-80,00", "CONCLUIDA", "TROCA"],
            ["Trigo", "1", "999,00", "CANCELADO", "DEVOLUCAO"],
        ])
        telemetria = resultado.telemetria
        self.assertEqual(resultado.kpis["faturamento_total"], 1000 - 100 + 250 - 80)
        self.assertEqual(telemetria["linhas_canceladas"], 5)
        self.assertEqual(telemetria["valor_cancelado"], 500 + 200 + 300 + 400 + 999)
        self.assertEqual(telemetria["linhas_devolucao"], 2)
        self.assertEqual(telemetria["valor_devolucoes"], 180.0)
        self.assertEqual(telemetria["vendas_validas"], 1250.0)
        self.assertEqual(telemetria["vendas_validas"] - telemetria["valor_devolucoes"], resultado.kpis["faturamento_total"])
        self.assertTrue(any("Faturamento líquido" in alerta for alerta in resultado.alertas))

    def test_devolucao_positiva_e_negativa_dao_o_mesmo_resultado(self):
        for valor in ("100,00", "-100,00", "(100,00)"):
            with self.subTest(valor=valor):
                resultado = self.calcular([self.COLUNAS, ["Arroz", "10", "1.000,00", "OK", "VENDA"],
                                           ["Arroz", "1", valor, "OK", "DEVOLUCAO"]])
                self.assertEqual(resultado.kpis["faturamento_total"], 900.0)

    def test_devolucao_tambem_deduz_a_quantidade(self):
        resultado = self.calcular([self.COLUNAS, ["Arroz", "10", "1.000,00", "OK", "VENDA"],
                                   ["Arroz", "3", "300,00", "OK", "TROCA"]])
        self.assertEqual(resultado.kpis["produto_mais_vendido_quantidade"], 7.0)

    def test_total_derivado_de_unitario_tambem_e_deduzido(self):
        resultado = self.calcular([["produto", "quantidade", "preco_unitario", "operacao"],
                                   ["Arroz", "10", "10,00", "VENDA"], ["Arroz", "2", "10,00", "DEVOLUCAO"]])
        self.assertEqual(resultado.kpis["faturamento_total"], 80.0)

    def test_nomes_de_coluna_reconhecidos(self):
        for titulo in ("Status", "Situação", "Status da Venda", "Situação do Pedido", "Status NF",
                       "Operação", "Tipo de Operação", "Natureza da Operação", "Tipo Movimento", "Tipo"):
            with self.subTest(titulo=titulo):
                resultado = self.calcular([[*CABECALHO, titulo], ["Arroz", "1", "100,00", "OK"],
                                           ["Feijão", "1", "50,00", "CANCELADA"]])
                self.assertEqual(resultado.kpis["faturamento_total"], 100.0)

    def test_status_em_duas_colunas_sao_lidos_juntos(self):
        resultado = self.calcular([[*CABECALHO, "Status Venda", "Situação NF"],
                                   ["Arroz", "1", "100,00", "OK", "AUTORIZADA"],
                                   ["Feijão", "1", "50,00", "OK", "CANCELADA"]])
        self.assertEqual(resultado.kpis["faturamento_total"], 100.0)

    def test_status_de_outro_assunto_nao_cancela_a_venda(self):
        for titulo in ("Status Pagamento", "Situação do Cliente", "Status de Entrega", "Tipo de Pagamento", "Status do Produto"):
            with self.subTest(titulo=titulo):
                resultado = self.calcular([[*CABECALHO, titulo], ["Arroz", "1", "100,00", "CANCELADO"]])
                self.assertEqual(resultado.kpis["faturamento_total"], 100.0)
                self.assertNotIn("linhas_canceladas", resultado.telemetria)

    def test_palavra_que_so_contem_as_letras_nao_conta(self):
        resultado = self.calcular([[*CABECALHO, "status"], ["Arroz", "1", "100,00", "MERCANCIA"],
                                   ["Feijão", "1", "50,00", "ENTROCADO"], ["Óleo", "1", "25,00", "SEM DEVOLUCAO"]])
        self.assertEqual(resultado.kpis["faturamento_total"], 175.0)

    def test_nome_do_produto_nao_decide_nada(self):
        resultado = self.calcular([[*CABECALHO, "status"], ["Troca de óleo", "1", "100,00", "OK"],
                                   ["Cancela de ferro", "1", "50,00", "OK"]])
        self.assertEqual(resultado.kpis["faturamento_total"], 150.0)

    def test_todas_canceladas_e_recusado(self):
        with self.assertRaises(ETLValidationError):
            self.calcular([[*CABECALHO, "status"], ["Arroz", "1", "100,00", "CANCELADA"]])

    def test_sem_coluna_de_situacao_nada_muda(self):
        resultado = self.calcular([CABECALHO, ["Arroz", "10", "100,00"], ["Feijão", "1", "-10,00"]])
        self.assertEqual(resultado.kpis["faturamento_total"], 90.0)
        self.assertNotIn("linhas_canceladas", resultado.telemetria)

    def test_coluna_interna_nao_aparece_na_telemetria(self):
        resultado = self.calcular([[*CABECALHO, "status"], ["Arroz", "1", "100,00", "OK"]])
        self.assertEqual([c for c in resultado.telemetria["colunas_vendas"] if c.startswith("_")], [])

    def test_devolucao_nao_gera_alerta_de_venda_abaixo_do_custo(self):
        resultado = self.calcular([[*CABECALHO, "total_custo", "operacao"],
                                   ["Arroz", "10", "1.000,00", "500,00", "VENDA"],
                                   ["Arroz", "1", "100,00", "50,00", "DEVOLUCAO"]])
        self.assertEqual(resultado.telemetria["itens_venda_abaixo_custo_pdv"], 0)


class CurvaAbcTests(_CasoComArquivos):
    def test_lista_todos_os_produtos_em_ordem_e_fecha_em_100(self):
        valores = [(indice * 37) % 1009 + 1 for indice in range(1, 151)]
        curva = _classificar_curva_abc(grupo_de(valores))
        produtos = curva["produtos"]
        self.assertEqual(len(produtos), 150)
        self.assertEqual([p["posicao"] for p in produtos], list(range(1, 151)))
        faturamentos = [p["faturamento"] for p in produtos]
        self.assertEqual(faturamentos, sorted(faturamentos, reverse=True))
        self.assertEqual(soma_exata(p["perc_individual"] for p in produtos), Decimal("100"))
        self.assertEqual(produtos[-1]["perc_acumulado"], 100.0)
        acumulado = Decimal(0)
        for produto in produtos:
            acumulado += Decimal(str(produto["perc_individual"]))
            self.assertEqual(acumulado, Decimal(str(produto["perc_acumulado"])))

    def test_soma_exata_com_divisoes_que_nao_fecham(self):
        for valores in ([1, 1, 1], [1] * 7, [10, 10, 10, 1], [33.33, 33.33, 33.34], [0.01] * 3, [1234.56, 0.01]):
            with self.subTest(valores=valores):
                produtos = _classificar_curva_abc(grupo_de(valores))["produtos"]
                self.assertEqual(soma_exata(p["perc_individual"] for p in produtos), Decimal("100"))
                self.assertEqual(produtos[-1]["perc_acumulado"], 100.0)

    def test_limiares_exatos(self):
        produtos = _classificar_curva_abc(grupo_de([800, 100, 50, 20, 15, 10, 4, 1]))["produtos"]
        self.assertEqual([p["perc_acumulado"] for p in produtos], [80.0, 90.0, 95.0, 97.0, 98.5, 99.5, 99.9, 100.0])
        self.assertEqual("".join(p["classe"] for p in produtos), "ABBCCCCC")

    def test_um_centesimo_acima_do_limiar_ja_muda_de_classe(self):
        produtos = _classificar_curva_abc(grupo_de([5000, 3001, 1499, 500]))["produtos"]
        self.assertEqual([p["perc_acumulado"] for p in produtos], [50.0, 80.01, 95.0, 100.0])
        self.assertEqual("".join(p["classe"] for p in produtos), "ABBC")

    def test_lider_e_sempre_classe_a_e_os_demais_seguem_as_faixas(self):
        for valores, classes in (([85, 10, 5], "ABC"), ([97, 3], "AC"), ([60], "A"), ([80, 15, 5], "ABC"), ([80, 10, 10], "ABC")):
            with self.subTest(valores=valores):
                curva = _classificar_curva_abc(grupo_de(valores))
                self.assertEqual("".join(p["classe"] for p in curva["produtos"]), classes)
                self.assertEqual(soma_exata(p["perc_individual"] for p in curva["produtos"]), Decimal("100"))
                self.assertEqual(curva["produtos"][-1]["perc_acumulado"], 100.0)

    def test_resumo_acompanha_a_protecao_do_lider(self):
        resumo = _classificar_curva_abc(grupo_de([85, 10, 5]))["resumo"]
        self.assertEqual([(item["classe"], item["qtd_produtos"], item["perc_faturamento"]) for item in resumo],
                         [("A", 1, 85.0), ("B", 1, 10.0), ("C", 1, 5.0)])

    def test_zerados_e_negativos_entram_e_a_conta_fecha_com_o_faturamento(self):
        resultado = self.calcular([CABECALHO, ["Arroz", "1", "100,00"], ["Feijão", "1", "0,00"], ["Óleo", "1", "-20,00"]])
        produtos = resultado.kpis["curva_abc"]["produtos"]
        self.assertEqual([p["nome"] for p in produtos], ["Arroz", "Feijão", "Óleo"])
        self.assertEqual([p["perc_individual"] for p in produtos], [125.0, 0.0, -25.0])
        self.assertEqual([p["perc_acumulado"] for p in produtos], [125.0, 125.0, 100.0])
        self.assertEqual("".join(p["classe"] for p in produtos), "ACC")
        self.assertEqual(sum(p["faturamento"] for p in produtos), resultado.kpis["faturamento_total"])
        self.assertEqual(soma_exata(p["perc_individual"] for p in produtos), Decimal("100"))
        self.assertTrue(any("receita líquida negativa" in alerta for alerta in resultado.alertas))

    def test_receita_liquida_nao_positiva_nao_gera_curva_e_avisa(self):
        for vendas in (["50,00", "-50,00"], ["-10,00"], ["0,00"], ["0,10", "0,20", "-0,30"]):
            with self.subTest(vendas=vendas):
                resultado = self.faturamento(*vendas)
                self.assertEqual(resultado.kpis["curva_abc"], {"produtos": [], "resumo": []})
                self.assertTrue(any("Curva ABC não calculada" in alerta for alerta in resultado.alertas))

    def test_empate_tem_ordem_estavel(self):
        grupo = pd.DataFrame({"_prod_norm": ["C", "A", "B"], "nome": ["C", "A", "B"], "qtde": [1.0] * 3, "fat": [10.0] * 3})
        self.assertEqual([p["nome"] for p in _classificar_curva_abc(grupo)["produtos"]], ["A", "B", "C"])

    def test_resumo_cobre_tudo_e_fecha_em_100(self):
        curva = _classificar_curva_abc(grupo_de([(i * 13) % 97 + 1 for i in range(500)]))
        self.assertEqual(sum(item["qtd_produtos"] for item in curva["resumo"]), 500)
        self.assertEqual(soma_exata(item["perc_faturamento"] for item in curva["resumo"]), Decimal("100"))

    def test_catalogo_grande_sem_desvio_e_dentro_do_limite_de_saida(self):
        quantidade = 20_000
        curva = _classificar_curva_abc(grupo_de([1] * quantidade))
        produtos = curva["produtos"]
        self.assertEqual(len(produtos), quantidade)
        self.assertEqual(soma_exata(p["perc_individual"] for p in produtos), Decimal("100"))
        for posicao in (1, 5_000, 16_000, 19_000, quantidade):
            self.assertAlmostEqual(produtos[posicao - 1]["perc_acumulado"], posicao / quantidade * 100, delta=0.0051)
        # O item seguinte a cada limiar acumula 80,005% e 95,005%: é gravado como 80,00 e
        # 95,00, e a classe segue o valor gravado.
        self.assertEqual([item["qtd_produtos"] for item in curva["resumo"]], [16_001, 3_000, 999])
        carga = json.dumps(curva, ensure_ascii=True, allow_nan=False)
        self.assertLess(len(carga), ETLLimits().max_output_bytes)

    def test_resultado_atravessa_o_json_do_processo_isolado(self):
        resultado = self.calcular([[*CABECALHO, "status"], ["Arroz", "2", "100,00", "OK"], ["Feijão", "1", "50,00", "DEVOLUCAO"],
                                   ["Óleo", "1", "abc", "OK"], ["Sal", "1", "9,00", "CANCELADA"]])
        json.dumps(resultado._asdict(), ensure_ascii=True, allow_nan=False, default=str)


class GravacaoDaCurvaTests(ContratosCase):
    def _linhas(self):
        from models import ProdutoCurvaABC
        self.db.session.expire_all()
        return self.db.session.query(ProdutoCurvaABC).filter_by(id_analise=self.analise_id) \
            .order_by(ProdutoCurvaABC.posicao_ranking).all()

    def _gravar(self, valores):
        curva = _classificar_curva_abc(grupo_de(valores))
        etl_processor._persistir_curva_abc(self.db.session, self.analise_id, {"curva_abc": curva})
        self.db.session.commit()

    def test_grava_todos_e_a_soma_no_banco_fecha_em_100(self):
        self._gravar([(i * 37) % 1009 + 1 for i in range(1, 61)] + [0, -5])
        linhas = self._linhas()
        self.assertEqual(len(linhas), 62)
        self.assertEqual(sum(linha.percentual_individual for linha in linhas), Decimal("100"))
        self.assertEqual(linhas[-1].percentual_acumulado, Decimal("100"))
        self.assertEqual(linhas[-1].classe_abc, "C")

    def test_reprocessar_substitui_a_curva_anterior(self):
        self._gravar([1] * 60)
        self._gravar([3, 2, 1])
        self.assertEqual([linha.posicao_ranking for linha in self._linhas()], [1, 2, 3])

    def test_curva_vazia_apaga_a_anterior(self):
        self._gravar([1] * 5)
        etl_processor._persistir_curva_abc(self.db.session, self.analise_id, {"curva_abc": {"produtos": [], "resumo": []}})
        self.db.session.commit()
        self.assertEqual(self._linhas(), [])

    def test_processamento_completo_grava_o_liquido_e_a_curva_inteira(self):
        from models import Analise
        with tempfile.TemporaryDirectory(prefix="nexo-etl-regras-") as trabalho:
            pasta = Path(trabalho)
            linhas = [[f"Produto {i:03d}", "1", "100,00", "OK", "VENDA"] for i in range(30)]
            linhas += [["Produto 000", "1", "100,00", "OK", "DEVOLUCAO"], ["Produto 001", "1", "100,00", "CANCELADA", "VENDA"]]
            vendas = write_csv(pasta / "vendas.csv", [[*CABECALHO, "status", "operacao"], *linhas])
            compras = write_csv(pasta / "compras.csv", PURCHASES)
            with patch("etl_processor.calcular_kpis_em_memoria", etl_processor._calcular_kpis_local):
                resultado = etl_processor.processar_arquivos_analise(str(vendas), str(compras), self.analise_id, self.db.session)
        self.assertTrue(resultado.sucesso, resultado.mensagem)
        self.db.session.commit()
        self.assertEqual(self.db.session.get(Analise, self.analise_id).indicador.faturamento_total, Decimal("2900"))
        linhas_gravadas = self._linhas()
        self.assertEqual(len(linhas_gravadas), 30)
        self.assertEqual(sum(linha.faturamento for linha in linhas_gravadas), Decimal("2900"))
        self.assertEqual(sum(linha.percentual_individual for linha in linhas_gravadas), Decimal("100"))

    def test_telas_e_pdf_abrem_com_produto_negativo_e_mais_de_20_itens(self):
        self.publicar(self.analise_id, date(2026, 1, 1), date(2026, 1, 31))
        self._gravar([100] * 25 + [-10])
        self.login()
        pagina = self.client.get(f"/cliente/analise/{self.analise_id}")
        self.assertEqual(pagina.status_code, 200)
        self.assertEqual(pagina.get_data(as_text=True).count("<tr data-classe="), 26)
        self.assertEqual(self.client.get("/cliente/dashboard").status_code, 200)
        pdf = self.client.get(f"/cliente/analise/{self.analise_id}/pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.get_data().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
