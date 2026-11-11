"""Planos por nível (entitlements), Raio-X de entrada e os componentes de upgrade.

Executar da raiz: python -B -m unittest tests.test_planos_raio_x -v
Migrações reais em SQLite; dados sintéticos (example.invalid); nenhuma rede. O processo
isolado do ETL exige o módulo `resource` (Linux): aqui ele é trocado por uma chamada no
próprio processo que faz o mesmo que o worker (validação, cálculo e ida e volta em JSON).
"""

import contextlib
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import inspect, select
from sqlalchemy.exc import OperationalError

import entitlements
import etl_uploads
from etl_inputs import ETLInputError
import raio_x
from tests.deploy_support import ROOT
from tests.raio_x_support import FEVEREIRO, JANEIRO, executar_como_o_worker, relatorio_do_mes as _mes
from tests.test_configuracoes_conta import SENHA, _ConfigCase

PAGINA = "/onboarding/diagnostico"
AUDITORIA = "/onboarding/diagnostico/auditoria"
API = "/api/onboarding/diagnostico"
API_AUDITORIA = "/api/onboarding/diagnostico/auditoria"


# =====================================================================
# Motor (raio_x.py), sem banco
# =====================================================================
class MotorRaioXTests(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory(prefix="raio-x-teste-")
        self.addCleanup(self.pasta.cleanup)

    def _arquivos(self, *conteudos, extensao="csv"):
        caminhos = []
        for numero, conteudo in enumerate(conteudos, 1):
            caminho = Path(self.pasta.name) / f"arquivo_{numero:02d}.{extensao}"
            caminho.write_bytes(conteudo)
            caminhos.append(str(caminho))
        return caminhos

    def _diagnosticar(self, *conteudos):
        return executar_como_o_worker(self._arquivos(*conteudos))

    def test_saude_scanner_recuperavel_e_precificacao_conferidos_a_mao(self):
        r = self._diagnosticar(JANEIRO, FEVEREIRO)
        self.assertEqual(set(r), raio_x.CHAVES_RESULTADO)
        self.assertEqual([f["rotulo"] for f in r["fatias"]], ["01/2026", "02/2026"])
        # Janeiro: Café a 18 contra tabela 20 (R$ 20 abaixo do preço); Leite vendido R$ 10 abaixo do custo.
        janeiro = r["fatias"][0]
        self.assertEqual((janeiro["faturamento"], janeiro["faturamento_sem_custo"]), (320.0, 30.0))
        self.assertEqual((janeiro["vazamento_preco"], janeiro["perda_abaixo_custo"], janeiro["recuperavel"]),
                         (20.0, 10.0, 30.0))
        self.assertEqual((janeiro["custo_total"], janeiro["margem_valor"]), (269.0, 21.0))
        saude = r["saude"]
        self.assertEqual((saude["faturamento_total"], saude["itens_vendidos"], saude["itens_sem_custo"]),
                         (650.0, 4, 0))
        # Auditável: Café (380) e Açúcar com custo (30); Leite 1L e Leite Integral dividem o código 3.
        self.assertEqual(saude["indice"], round(100 * 410 / 650, 2))
        self.assertEqual(saude["inconsistencias"]["codigo_com_varios_nomes"], 1)
        self.assertEqual(saude["inconsistencias"]["estoque_negativo"], 1)
        self.assertEqual(r["scanner"]["itens_com_variacao"], 1)
        self.assertEqual(r["scanner"]["itens"][0]["produto"], "Cafe 500g")
        self.assertEqual((r["scanner"]["itens"][0]["preco_min"], r["scanner"]["itens"][0]["preco_max"]), (18.0, 20.0))
        self.assertEqual(r["recuperavel"], {"total": 30.0, "abaixo_custo": 10.0, "variacao_preco": 20.0})
        auditoria = r["auditoria"]
        self.assertEqual([i["produto"] for i in auditoria["itens_abaixo_custo"]], ["Leite 1L"])
        self.assertEqual((auditoria["custo_total"], auditoria["margem_valor"]), (559.0, 61.0))
        # Custo do Café de 15 para 18 (+20%), preço de 18 para 20 (+11,1%): perdeu R$ 1 por unidade x 10.
        alerta = auditoria["precificacao"][0]
        self.assertEqual((alerta["produto"], alerta["variacao_custo"], alerta["impacto"]), ("Cafe 500g", 0.2, 10.0))
        self.assertEqual(r["periodo"], {"inicio": "2026-01-01", "fim": "2026-02-28", "meses": 2})
        self.assertEqual(r["avisos"], [])

    def test_referencia_e_vendas_abaixo_do_custo_detalhadas_por_item(self):
        r = self._diagnosticar(JANEIRO, FEVEREIRO)
        cafe = r["scanner"]["itens"][0]
        self.assertEqual((cafe["preco_referencia"], cafe["referencia_tabela"]), (20.0, True))
        # Leite 1L: abaixo do custo só em janeiro (R$ 100 contra R$ 110); fevereiro não entra na linha.
        leite = r["auditoria"]["itens_abaixo_custo"][0]
        self.assertEqual((leite["quantidade_abaixo"], leite["faturamento_abaixo"], leite["custo_abaixo"],
                          leite["perda"]), (20.0, 100.0, 110.0, 10.0))

    def test_diferenca_dentro_da_tolerancia_nao_vira_vazamento(self):
        # Preço médio 0,9949 contra tabela 1,00: R$ 0,0051 por unidade, abaixo da tolerância de R$ 0,01.
        dentro = self._diagnosticar(_mes(2026, 3, ["1;Pao frances;30000;1,00;29847,00;20000,00"]))
        self.assertEqual((dentro["scanner"]["itens_com_variacao"], dentro["recuperavel"]["total"]), (0, 0.0))
        self.assertEqual(raio_x.recomendar(dentro, 24)["plano"], "BRONZE")
        # R$ 0,054 abaixo da tabela de 10,00 (tolerância 0,05): conta, e pelo preço sem arredondar.
        acima = self._diagnosticar(_mes(2026, 3, ["2;Cafe;10000;10,00;99460,00;50000,00"]))
        self.assertEqual(acima["recuperavel"]["variacao_preco"], 540.0)

    def test_empate_em_quantidade_fracionada_fica_com_o_menor_preco(self):
        linhas = ["1;Queijo kg;1,1;;55,00;", "1;Queijo kg;2,2;;110,00;", "1;Queijo kg;3,3;;148,50;"]
        r = self._diagnosticar(_mes(2026, 3, linhas))
        self.assertEqual(r["recuperavel"]["variacao_preco"], 0.0)
        self.assertEqual(r["scanner"]["itens"][0]["preco_referencia"], 45.0)

    def test_inconsistencias_contam_cada_codigo_e_produto_uma_vez(self):
        mes = ["3;Leite 1L;20;5,00;100,00;90,00", "3;Leite Integral;2;5,00;10,00;9,00"]
        r = self._diagnosticar(*(_mes(2026, numero, mes) for numero in (1, 2, 3)))
        inconsistencias = r["saude"]["inconsistencias"]
        self.assertEqual((inconsistencias["codigo_com_varios_nomes"], r["saude"]["itens_vendidos"]), (1, 2))
        estoque = ("Codigo;Produto;Qtde;Valor Total;Estoque\n1;Cafe;10;180,00;-3\n").encode("utf-8")
        self.assertEqual(self._diagnosticar(estoque, estoque + b"\n", estoque + b"\n\n")
                         ["saude"]["inconsistencias"]["estoque_negativo"], 1)

    def test_coluna_de_custo_unitario_vira_custo_da_linha(self):
        conteudo = ("Codigo;Produto;Qtde;Preco Unitario;Valor Total;Custo Unitario\n"
                    "1;Cafe;10;20,00;200,00;15,00\n2;Leite;4;5,00;20,00;6,00\n").encode("utf-8")
        r = self._diagnosticar(conteudo)
        itens = {item["produto"]: item for item in r["auditoria"]["margem_por_item"]}
        self.assertEqual((itens["Cafe"]["custo_total"], itens["Cafe"]["custo_medio"], itens["Cafe"]["margem_percentual"]),
                         (150.0, 15.0, 0.25))
        # Custo unitário 6 acima do preço 5: perda de R$ 4 que a leitura como custo total escondia.
        self.assertEqual(r["auditoria"]["perda_abaixo_custo"], 4.0)

    def test_titulos_de_custo_por_unidade_e_de_custo_da_linha(self):
        from etl_processor import _norm_chave
        unitarios = ("Custo Unitario", "Custo Unit.", "Custo Un.", "Custo Und", "Custo Unid.", "Custo Unidade",
                     "Custo p/ Unidade", "Custo por Unidade", "Custo Médio", "Preço de Custo", "Preço Custo")
        totais = ("Total Custo", "Custo Total", "Vlr Custo", "Custo", "Total Custo Médio", "Custo Médio Total",
                  "Tot. Custo Médio", "Total Preço de Custo", "Qtde x Custo Médio", "Custo Unit x Qtde")
        self.assertEqual([t for t in unitarios if not raio_x.custo_e_unitario(_norm_chave(t))], [])
        self.assertEqual([t for t in totais if raio_x.custo_e_unitario(_norm_chave(t))], [])
        conteudo = ("Codigo;Produto;Qtde;Valor Total;Total Custo Medio\n1;Cafe;10;200,00;150,00\n"
                    "2;Leite;50;250,00;200,00\n").encode("utf-8")
        auditoria = self._diagnosticar(conteudo)["auditoria"]
        self.assertEqual((auditoria["custo_total"], auditoria["perda_abaixo_custo"]), (350.0, 0.0))

    def test_perda_da_auditoria_fecha_em_centavos(self):
        # Granel: 1,115 kg a R$ 9,90 com custo de R$ 9,95/kg. Vendido 11,04; custo 11,09; perda 0,05.
        conteudo = "Codigo;Produto;Qtde;Preco Unitario;Custo Unitario\n1;Queijo kg;1,115;9,90;9,95\n".encode("utf-8")
        item = self._diagnosticar(conteudo)["auditoria"]["itens_abaixo_custo"][0]
        self.assertEqual((item["faturamento_abaixo"], item["custo_abaixo"], item["perda"]), (11.04, 11.09, 0.05))

    # Um item genérico vendido por valor, com custo de cadastro absurdo: 80 vezes a venda.
    CUSTO_ABSURDO = ("Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 31/01/2026\n"
                     "Codigo;Produto;Qtde;Valor Total;Total Custo\n1;Cafe 500g;10;200,00;150,00\n"
                     "2;Leite 1L;20;100,00;110,00\n9;Item Generico;100;500,00;40000,00\n").encode("utf-8")

    def test_item_de_custo_implausivel_fica_a_confirmar_fora_da_perda_e_do_recuperavel(self):
        r = self._diagnosticar(self.CUSTO_ABSURDO)
        auditoria = r["auditoria"]
        self.assertEqual((auditoria["perda_abaixo_custo"], auditoria["itens_venda_abaixo_custo"]), (10.0, 1))
        self.assertEqual([item["produto"] for item in auditoria["itens_abaixo_custo"]], ["Leite 1L"])
        self.assertEqual(auditoria["custo_a_confirmar"], {
            "itens": 1, "faturamento_com_custo": 500.0, "custo_total": 40000.0, "perda_desconsiderada": 39500.0,
            "lista": [{"produto": "Item Generico", "codigo": "9", "quantidade_com_custo": 100.0,
                       "faturamento_com_custo": 500.0, "custo_total": 40000.0, "custo_medio": 400.0}]})
        self.assertEqual(r["recuperavel"], {"total": 10.0, "abaixo_custo": 10.0, "variacao_preco": 0.0})
        self.assertEqual((r["fatias"][0]["perda_abaixo_custo"], r["fatias"][0]["recuperavel"]), (10.0, 10.0))
        # O custo total, a margem e a contagem de margem negativa continuam com o item.
        self.assertEqual((auditoria["custo_total"], auditoria["itens_margem_negativa"]), (40260.0, 2))
        self.assertIn("1 produto(s) com custo a partir de 3 vezes a venda", " ".join(r["avisos"]))

    def test_totais_sem_os_itens_a_confirmar_fecham_e_nunca_saem_negativos(self):
        """Sorteio com semente fixa (o roteiro da revisão independente): com itens de custo implausível em
        vários meses, a perda e o recuperável das fatias fecham com os totais, nada sai negativo nem como
        zero negativo, e o que saiu da perda e da contagem é exatamente o que está em custo_a_confirmar."""
        import random
        sorteio = random.Random(6)

        def br(valor):
            return f"{valor:.2f}".replace(".", ",")

        for ensaio in range(30):
            arquivos = []
            for mes in range(1, sorteio.randrange(2, 5)):
                linhas = []
                for produto in range(1, sorteio.randrange(4, 12)):
                    for _ in range(sorteio.randrange(1, 4)):
                        quantidade = sorteio.randrange(1, 6)
                        venda = round(quantidade * sorteio.choice((1.1, 2.35, 7.9, 12.49)) * sorteio.choice((1, 1, 0.9, 1.05)), 2)
                        fator = sorteio.uniform(3.5, 9.0) if produto <= 3 else sorteio.uniform(0.3, 1.6)
                        linhas.append(f"{produto};PRODUTO {produto};{quantidade};;{br(venda)};{br(venda * fator)}")
                arquivos.append(_mes(2026, mes, linhas))
            with self.subTest(ensaio=ensaio):
                novo = self._diagnosticar(*arquivos)
                with patch.object(raio_x, "RAZAO_CUSTO_IMPLAUSIVEL", 10 ** 18):
                    antigo = self._diagnosticar(*arquivos)
                auditoria, recuperavel, fatias = novo["auditoria"], novo["recuperavel"], novo["fatias"]
                a_confirmar = auditoria["custo_a_confirmar"]
                self.assertEqual((a_confirmar["itens"], antigo["auditoria"]["custo_a_confirmar"]["itens"]), (3, 0))
                folga = 0.005 * len(fatias) + 0.006
                self.assertAlmostEqual(sum(fatia["perda_abaixo_custo"] for fatia in fatias),
                                       auditoria["perda_abaixo_custo"], delta=folga)
                self.assertAlmostEqual(sum(fatia["recuperavel"] for fatia in fatias), recuperavel["total"], delta=folga)
                self.assertEqual(recuperavel["abaixo_custo"], auditoria["perda_abaixo_custo"])
                self.assertNotRegex(json.dumps(novo), r"-0\.0(?![0-9])")
                self.assertGreaterEqual(min(auditoria["perda_abaixo_custo"], recuperavel["total"],
                                            *(fatia["recuperavel"] for fatia in fatias),
                                            *(fatia["perda_abaixo_custo"] for fatia in fatias)), 0)
                self.assertAlmostEqual(antigo["auditoria"]["perda_abaixo_custo"],
                                       auditoria["perda_abaixo_custo"] + a_confirmar["perda_desconsiderada"], delta=0.011)
                self.assertEqual(antigo["auditoria"]["itens_venda_abaixo_custo"],
                                 auditoria["itens_venda_abaixo_custo"] + a_confirmar["itens"])

    def test_custo_abaixo_de_tres_vezes_a_venda_continua_na_perda(self):
        r = self._diagnosticar(self.CUSTO_ABSURDO.replace(b"40000,00", b"1499,99"))
        self.assertEqual(r["auditoria"]["custo_a_confirmar"]["itens"], 0)
        self.assertEqual((r["auditoria"]["perda_abaixo_custo"], r["auditoria"]["itens_venda_abaixo_custo"]), (1009.99, 2))
        self.assertEqual(self._diagnosticar(self.CUSTO_ABSURDO.replace(b"40000,00", b"1500,00"))
                         ["auditoria"]["custo_a_confirmar"]["itens"], 1)

    def test_custo_a_confirmar_segue_o_que_o_plano_libera(self):
        import raio_x_servico
        dados = self._diagnosticar(self.CUSTO_ABSURDO)
        tudo = frozenset({"margem_bruta", "auditoria_abaixo_custo", "diagnostico_cmv", "alerta_precificacao"})

        def bloco(liberadas):
            return raio_x_servico.visao(dados, liberadas, None)["auditoria"]["custo_a_confirmar"]

        self.assertEqual(bloco(tudo), dados["auditoria"]["custo_a_confirmar"])
        # Sem nada liberado: só a contagem e a perda desconsiderada, que são totais como a perda abaixo do custo.
        self.assertEqual(bloco(frozenset()), {"itens": 1, "faturamento_com_custo": None, "custo_total": None,
                                              "perda_desconsiderada": 39500.0, "lista": None})
        self.assertNotIn("40000", json.dumps(raio_x_servico.visao(dados, frozenset(), None)))
        # A lista segue a das vendas abaixo do custo: é da auditoria de custos e traz o custo de cada item.
        so_auditoria = bloco(frozenset({"auditoria_abaixo_custo"}))
        self.assertEqual(so_auditoria["lista"], dados["auditoria"]["custo_a_confirmar"]["lista"])
        self.assertEqual((so_auditoria["custo_total"], so_auditoria["faturamento_com_custo"]), (None, None))
        self.assertIsNone(bloco(tudo - {"auditoria_abaixo_custo"})["lista"])
        # Os totais de custo do bloco seguem o custo total da auditoria: são do diagnóstico de CMV.
        so_cmv = bloco(frozenset({"diagnostico_cmv"}))
        self.assertEqual((so_cmv["custo_total"], so_cmv["faturamento_com_custo"], so_cmv["lista"]), (40000.0, 500.0, None))
        # A visão é uma cópia: o resultado gravado não muda.
        self.assertEqual(dados["auditoria"]["custo_a_confirmar"]["custo_total"], 40000.0)

    def test_raio_x_gravado_antes_da_unificacao_nao_tem_custo_a_confirmar(self):
        import raio_x_servico
        dados = self._diagnosticar(JANEIRO)
        del dados["auditoria"]["custo_a_confirmar"]
        self.assertIsNone(raio_x_servico.visao(dados, frozenset(), None)["auditoria"]["custo_a_confirmar"])

    def test_faixa_do_historico_oculto_sai_das_datas(self):
        import raio_x_servico
        anual = ("Relatorio de vendas por produto\nPeriodo: 01/01/2024 a 31/12/2024\n"
                 "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n1;Cafe;10;20,00;200,00;150,00\n"
                 ).encode("utf-8")
        mensais = [_mes(2025, numero, [f"1;Cafe;10;20,00;20{numero},00;150,00"]) for numero in range(1, 9)]
        historico = raio_x_servico.visao(self._diagnosticar(anual, *mensais), frozenset(), 6)["historico"]
        self.assertEqual((historico["ocultas"], historico["primeira_oculta"], historico["ultima_oculta"]),
                         (3, "01/2024", "02/2025"))

    def test_total_calculado_pelo_preco_unitario_nao_mostra_variacao(self):
        conteudo = "Codigo;Produto;Qtde;Preco Unitario;Total Custo\n1;Cafe;10;20,00;150,00\n".encode("utf-8")
        r = self._diagnosticar(conteudo)
        self.assertFalse(r["scanner"]["variacao_visivel"])

    def test_relatorio_sem_venda_valida_e_recusado(self):
        conteudo = ("Codigo;Produto;Qtde;Valor Total;Situacao\n1;Cafe;10;180,00;Devolucao\n"
                    "2;Leite;0;0,00;Concluida\n").encode("utf-8")
        with self.assertRaises(ETLInputError) as erro:
            self._diagnosticar(conteudo)
        self.assertEqual(erro.exception.code, "raio_x_sem_vendas")

    def test_sobreposicao_com_relatorio_anual_avisa_cada_mensal(self):
        anual = ("Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 31/12/2026\n"
                 "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n1;Cafe;10;20,00;200,00;150,00\n"
                 ).encode("utf-8")
        mensais = [_mes(2026, numero, [f"1;Cafe;10;20,00;20{numero},00;150,00"]) for numero in (2, 3, 4)]
        avisos = [a for a in self._diagnosticar(anual, *mensais)["avisos"] if "se sobrepõem" in a]
        self.assertEqual(avisos, [f"Os arquivos 1 e {n} cobrem períodos que se sobrepõem: os totais somam os dois."
                                  for n in (2, 3, 4)])

    def test_recuperavel_conta_uma_vez_a_venda_abaixo_do_custo_e_do_preco(self):
        # 10 un. a R$ 6 (tabela R$ 10, custo R$ 8): R$ 40 abaixo do preço e R$ 20 abaixo do custo.
        r = self._diagnosticar(_mes(2026, 3, ["9;Item;10;10,00;60,00;80,00"]))
        self.assertEqual(r["recuperavel"], {"total": 40.0, "abaixo_custo": 20.0, "variacao_preco": 40.0})

    def test_lista_a_confirmar_vem_do_maior_custo_para_o_menor_e_tem_limite(self):
        # Pedido pelo teste de mutação da revisão final: a ordem e o limite da lista não tinham teste.
        linhas = [f"{n};GENERICO {n};10;;100,00;{1000 + n},00" for n in range(1, 61)]  # custo de 10 vezes a venda
        bloco = self._diagnosticar(_mes(2026, 1, linhas))["auditoria"]["custo_a_confirmar"]
        self.assertEqual((bloco["itens"], len(bloco["lista"])), (60, raio_x.LIMITE_ITENS_LISTA))
        self.assertEqual([item["custo_total"] for item in bloco["lista"][:3]], [1060.0, 1059.0, 1058.0])
        self.assertEqual(bloco["custo_total"], float(sum(1000 + n for n in range(1, 61))))

    def test_totais_zerados_pelos_itens_a_confirmar_nao_saem_como_zero_negativo(self):
        """Quando TODA a perda é de itens a confirmar, a subtração em ponto flutuante dá um negativo minúsculo:
        cada total sai 0.0, e não -0.0 (os dois relatórios vêm da busca da revisão independente final)."""
        casos = (["1;PRODUTO 1;5;;5,50;43,99", "2;PRODUTO 2;5;;11,75;79,33", "2;PRODUTO 2;1;;2,35;10,71"],
                 ["1;PRODUTO 1;3;;7,05;51,93", "1;PRODUTO 1;5;;5,50;37,84", "2;PRODUTO 2;4;;4,40;36,94",
                  "3;PRODUTO 3;5;;62,45;271,54"])
        for numero, linhas in enumerate(casos):
            with self.subTest(caso=numero):
                r = self._diagnosticar(_mes(2026, 1, linhas))
                self.assertEqual(r["auditoria"]["custo_a_confirmar"]["itens"], len({linha.split(";")[0] for linha in linhas}))
                self.assertEqual((r["auditoria"]["perda_abaixo_custo"], r["recuperavel"]["total"],
                                  r["recuperavel"]["abaixo_custo"], r["fatias"][0]["recuperavel"],
                                  r["fatias"][0]["perda_abaixo_custo"]), (0.0, 0.0, 0.0, 0.0, 0.0))
                self.assertNotRegex(json.dumps(r), r"-0\.0(?![0-9])")

    def test_linha_por_venda_usa_o_preco_mais_praticado_e_o_menor_no_empate(self):
        linhas = ["1;Pao;1;;2,00;", "1;Pao;1;;2,00;", "1;Pao;1;;1,50;", "2;Bolo;2;;20,00;", "2;Bolo;2;;24,00;"]
        r = self._diagnosticar(_mes(2026, 3, linhas))
        itens = {item["produto"]: item for item in r["scanner"]["itens"]}
        self.assertEqual(itens["Pao"]["vazamento"], 0.5)        # referência 2,00 (2 un.) contra 1,50
        self.assertEqual(itens["Bolo"]["vazamento"], 0.0)       # empate: referência 10,00, a menor
        self.assertTrue(r["scanner"]["variacao_visivel"])

    def test_uma_linha_por_produto_sem_preco_unitario_nao_mostra_variacao(self):
        conteudo = ("Codigo;Produto;Qtde;Valor Total;Total Custo\n1;Cafe;10;180,00;150,00\n2;Leite;5;25,00;20,00\n")
        r = self._diagnosticar(conteudo.encode("utf-8"))
        self.assertFalse(r["scanner"]["variacao_visivel"])
        self.assertEqual(r["scanner"]["itens_com_variacao"], 0)
        self.assertIn("não traz o período", " ".join(r["avisos"]))

    def test_canceladas_e_devolucoes_ficam_fora_das_contas_de_preco_e_custo(self):
        conteudo = ("Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Situacao\n"
                    "1;Cafe;10;20,00;200,00;150,00;Concluida\n"
                    "1;Cafe;2;20,00;10,00;30,00;Cancelada\n"
                    "1;Cafe;1;20,00;20,00;15,00;Devolucao\n")
        r = self._diagnosticar(conteudo.encode("utf-8"))
        self.assertEqual(r["saude"]["faturamento_total"], 200.0)
        self.assertEqual(r["saude"]["devolucoes"], 20.0)
        self.assertEqual(r["recuperavel"]["total"], 0.0)

    def test_sem_coluna_de_custo_recomenda_prata_e_avisa(self):
        conteudo = "Produto;Qtde;Valor Total\nCafe;10;180,00\nLeite;5;25,00\n".encode("utf-8")
        r = self._diagnosticar(conteudo)
        self.assertEqual(r["saude"]["pct_faturamento_sem_custo"], 1.0)
        self.assertIn("não traz a coluna de custo", " ".join(r["avisos"]))
        recomendacao = raio_x.recomendar(r, 24)
        self.assertEqual(recomendacao["plano"], "PRATA")
        self.assertIn("não trazem o custo", recomendacao["motivos"][0])

    def test_cadastro_saudavel_fica_no_bronze_e_historico_longo_com_problema_vai_ao_ouro(self):
        saudavel = self._diagnosticar(_mes(2026, 1, ["1;Cafe;10;20,00;200,00;150,00"]))
        self.assertEqual(raio_x.recomendar(saudavel, 24)["plano"], "BRONZE")
        problema = self._diagnosticar(JANEIRO, FEVEREIRO)
        problema["periodo"]["meses"] = 36
        recomendacao = raio_x.recomendar(problema, 24)
        self.assertEqual(recomendacao["plano"], "OURO")
        self.assertIn("36 meses", recomendacao["motivos"][-1])
        problema["periodo"]["meses"] = 24
        self.assertEqual(raio_x.recomendar(problema, 24)["plano"], "PRATA")

    def test_periodos_sobrepostos_geram_aviso(self):
        r = self._diagnosticar(JANEIRO, JANEIRO.replace(b"180,00", b"181,00"))
        self.assertIn("se sobrepõem", " ".join(r["avisos"]))

    def test_arquivo_sem_colunas_de_vendas_tem_mensagem_segura(self):
        invalido = "Coluna secreta;Outra\n<script>x</script>;1\n".encode("utf-8")
        with self.assertRaises(ETLInputError) as erro:
            self._diagnosticar(JANEIRO, invalido)
        self.assertEqual(erro.exception.code, "raio_x_dados")
        self.assertTrue(erro.exception.safe_message.startswith("Arquivo 2:"))
        self.assertNotIn("secreta", erro.exception.safe_message)

    def test_quantidade_de_arquivos_fora_do_limite(self):
        for caminhos in ([], ["x.csv"] * (raio_x.MAX_ARQUIVOS + 1)):
            with self.subTest(arquivos=len(caminhos)), self.assertRaises(ETLInputError):
                raio_x.diagnosticar(caminhos)

    def test_motor_nao_carrega_banco_nem_configuracao(self):
        """O worker isolado importa raio_x: nada de models nem config (segredos) no processo filho."""
        codigo = ("import sys; sys.path.insert(0, sys.argv[1]); import raio_x; "
                  "print('models' in sys.modules or 'config' in sys.modules or 'flask' in sys.modules)")
        saida = subprocess.run([sys.executable, "-I", "-B", "-c", codigo, str(ROOT)], capture_output=True,
                               text=True, timeout=120, check=True)
        self.assertEqual(saida.stdout.strip(), "False")

    def test_worker_tem_a_operacao_e_o_teto_de_arquivos(self):
        fonte = (ROOT / "etl_worker.py").read_text(encoding="utf-8")
        self.assertIn('request["operation"] == "raio_x"', fonte)
        self.assertIn("MAX_ARQUIVOS", fonte)
        self.assertIn("validate_input(path", fonte)


# =====================================================================
# Base com migrações reais
# =====================================================================
class _PlanosCase(_ConfigCase):
    def setUp(self):
        super().setUp()
        from models import DiagnosticoEntrada, Empresa, Plano
        self.Plano, self.DiagnosticoEntrada, self.Empresa = Plano, DiagnosticoEntrada, Empresa
        self.cliente_http, _ = self._entrar()
        self.stack.enter_context(patch("etl_runtime.run_raio_x", side_effect=executar_como_o_worker))

    def _mudar_plano(self, nome, id_empresa=None):
        plano = self.db.session.scalar(select(self.Plano).where(self.Plano.nome_plano == nome))
        if plano is None:
            plano = self.Plano(nome_plano=nome, valor_mensal=300, qtd_analises_mes=1, tipo_analise_permitida="MENSAL",
                               nivel_entrega_analise="COMPLETA", nivel_dashboard="GERENCIAL",
                               nivel_atendimento="MEDIO")
            self.db.session.add(plano)
            self.db.session.flush()
        self.db.session.get(self.Empresa, id_empresa or self.empresa_id).id_plano_atual = plano.id_plano
        self.db.session.commit()

    def _status(self, status, id_empresa=None):
        self.db.session.get(self.Empresa, id_empresa or self.empresa_id).status_conta = status
        self.db.session.commit()

    def _enviar_raio_x(self, *arquivos, cliente=None, api=False, seguir=False):
        cliente = cliente or self.cliente_http
        token = self._csrf(cliente, PAGINA) if not api else self._token(cliente)
        dados = {"arquivos": [(io.BytesIO(conteudo), nome) for nome, conteudo in arquivos]}
        if api:
            return cliente.post(API, data=dados, headers={"X-CSRFToken": token}, content_type="multipart/form-data")
        dados["csrf_token"] = token
        return cliente.post(PAGINA, data=dados, content_type="multipart/form-data", follow_redirects=seguir)

    def _token(self, cliente):
        html = cliente.get("/cliente/dashboard").get_data(as_text=True)
        return re.search(r'<meta name="csrf-token" content="([^"]+)"', html).group(1)

    def _linha(self, id_empresa=None):
        self.db.session.expire_all()
        return self.db.session.get(self.DiagnosticoEntrada, id_empresa or self.empresa_id)

    def _menu(self, html):
        nav = re.search(r'<nav\b[^>]*class="[^"]*sidebar-nav[^"]*"[^>]*>(.*?)</nav>', html, re.S).group(1)
        itens = []
        for atributos, conteudo in re.findall(r"<a\b([^>]*)>(.*?)</a>", nav, re.S):
            href = re.search(r'href="([^"]*)"', atributos).group(1)
            texto = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", conteudo)).strip()
            itens.append((texto, href, atributos, conteudo))
        return itens


# =====================================================================
# Migração b3e9d6c1f7a2
# =====================================================================
class MigracaoPlanosTests(_PlanosCase):
    def _migrar(self, comando, revisao):
        import flask_migrate
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            getattr(flask_migrate, comando)(directory=str(ROOT / "migrations"), revision=revisao)

    def _catalogo(self):
        consulta = self.db.text("SELECT pf.codigo_plano, f.chave FROM plano_funcionalidade pf "
                                "JOIN funcionalidade f ON f.id_funcionalidade = pf.id_funcionalidade")
        matriz = {}
        for codigo, chave in self.db.session.execute(consulta):
            matriz.setdefault(codigo, set()).add(chave)
        return matriz

    def test_niveis_funcionalidades_e_matriz_cumulativa(self):
        niveis = self.db.session.execute(self.db.text(
            "SELECT codigo, nivel_hierarquico, limite_historico_meses FROM plano_nivel ORDER BY nivel_hierarquico"
        )).all()
        self.assertEqual([tuple(n) for n in niveis],
                         [("BRONZE", 1, 6), ("PRATA", 2, 24), ("OURO", 3, None), ("CUSTOM", 4, None)])
        matriz = self._catalogo()
        self.assertEqual(matriz["BRONZE"], {"curva_abc_vendas", "produtos_parados_ruptura", "checklist_acoes",
                                            "raio_x_entrada"})
        self.assertEqual(matriz["PRATA"] - matriz["BRONZE"], {"auditoria_abaixo_custo", "margem_bruta",
                                                              "diagnostico_cmv", "alerta_precificacao"})
        self.assertEqual(matriz["OURO"] - matriz["PRATA"], {"curva_abc_clientes", "taxas_meios_pagamento",
                                                            "recompra_whatsapp"})
        self.assertEqual(matriz["CUSTOM"] - matriz["OURO"], {"dre_consolidada", "analise_vendedores",
                                                             "seletor_multifilial", "parsers_sob_medida"})
        self.assertTrue(matriz["BRONZE"] < matriz["PRATA"] < matriz["OURO"] < matriz["CUSTOM"])
        # O código só cita chaves que a migração gravou.
        chaves = matriz["CUSTOM"]
        self.assertLessEqual(set(entitlements.FUNCIONALIDADES_NO_PORTAL), chaves)
        self.assertLessEqual({i.funcionalidade for i in entitlements.MENU_CLIENTE if i.funcionalidade}, chaves)

    def test_plano_e_empresa_nao_ganham_colunas(self):
        colunas = {tabela: {c["name"] for c in inspect(self.db.engine).get_columns(tabela)}
                   for tabela in ("plano", "empresa")}
        self.assertEqual(colunas["plano"], {
            "id_plano", "nome_plano", "descricao", "valor_mensal", "qtd_analises_mes", "tipo_analise_permitida",
            "nivel_entrega_analise", "nivel_dashboard", "nivel_atendimento", "ativo", "data_criacao",
            "data_atualizacao"})
        self.assertNotIn("plano_id", colunas["empresa"])
        self.assertNotIn("status_assinatura", colunas["empresa"])

    def test_downgrade_remove_o_catalogo_e_o_upgrade_o_regrava(self):
        planos_antes = self._contar(self.Plano)
        self._migrar("downgrade", "a8d3f6b91c24")
        tabelas = set(inspect(self.db.engine).get_table_names())
        self.assertFalse(tabelas & set(entitlements.TABELAS_PLANOS))
        self.assertEqual(self._contar(self.Plano), planos_antes)
        self._migrar("upgrade", "head")
        self.assertEqual(len(self._catalogo()["CUSTOM"]), 15)

    def test_downgrade_recusa_apagar_raio_x_gravado(self):
        resposta = self._enviar_raio_x(("jan.csv", JANEIRO))
        self.assertEqual(resposta.status_code, 303)
        with self.assertRaises(SystemExit):
            self._migrar("downgrade", "a8d3f6b91c24")
        self.assertIsNotNone(self._linha())

    def test_downgrade_recusa_apagar_catalogo_alterado(self):
        self.db.session.execute(self.db.text("UPDATE plano_nivel SET limite_historico_meses = 12 WHERE codigo = 'BRONZE'"))
        with self.assertRaises(SystemExit):
            self._migrar("downgrade", "a8d3f6b91c24")
        self.assertIn("plano_nivel", inspect(self.db.engine).get_table_names())

    def _sem_esquema_com_o_processo_no_ar(self):
        """Tabelas removidas sem limpar o cache do engine, como um processo que segue no ar."""
        self.assertEqual(self.cliente_http.get(PAGINA).status_code, 200)  # guarda a presença no cache
        self._migrar("downgrade", "a8d3f6b91c24")

    def test_sem_o_esquema_o_portal_segue_com_o_menu_de_sempre(self):
        self._sem_esquema_com_o_processo_no_ar()
        resposta = self.cliente_http.get("/cliente/dashboard")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual([t for t, *_ in self._menu(resposta.get_data(as_text=True))],
                         ["Visão Geral", "Enviar relatórios", "Histórico", "Guia", "Suporte", "Configurações"])
        self.assertFalse(entitlements.esquema_planos_disponivel())

    def test_planos_como_primeira_pagina_depois_do_downgrade_responde_503(self):
        self._sem_esquema_com_o_processo_no_ar()
        self.assertEqual(self.cliente_http.get("/onboarding/planos").status_code, 503)

    def test_catalogo_vazio_conta_como_recurso_nao_ativado(self):
        """Tabelas criadas sem a migração (create_all): nada de 403 "fora de qualquer plano"."""
        self.db.session.execute(self.db.text("DELETE FROM plano_funcionalidade"))
        self.db.session.execute(self.db.text("DELETE FROM funcionalidade"))
        self.db.session.execute(self.db.text("DELETE FROM plano_nivel"))
        self.db.session.commit()
        entitlements._esquema_por_engine.clear()
        api = self.cliente_http.get(API)
        self.assertEqual((api.status_code, api.get_json()["codigo"]), (503, "recurso_indisponivel"))
        self.assertEqual(self.cliente_http.get("/onboarding/planos").status_code, 503)

    def test_sem_o_esquema_as_rotas_novas_respondem_503(self):
        self._sem_esquema_com_o_processo_no_ar()
        pagina = self.cliente_http.get(PAGINA)
        self.assertEqual(pagina.status_code, 503)
        self.assertIn("ainda não foi ativado", pagina.get_data(as_text=True))
        api = self.cliente_http.get(API)
        self.assertEqual(api.status_code, 503)
        self.assertEqual(api.get_json()["codigo"], "recurso_indisponivel")
        self.assertEqual(self.cliente_http.get("/onboarding/planos").status_code, 503)


# =====================================================================
# entitlements.py
# =====================================================================
class EntitlementsTests(_PlanosCase):
    def _requisicao(self):
        """Contexto novo, como o de cada requisição: o cache de entitlements mora no `g` dele."""
        pilha = contextlib.ExitStack()
        pilha.enter_context(self.app.app_context())
        pilha.enter_context(self.app.test_request_context())
        return pilha

    def test_acesso_por_nivel(self):
        with self._requisicao():
            self.assertTrue(entitlements.empresa_tem_acesso(self.empresa_id, "raio_x_entrada"))
            self.assertFalse(entitlements.empresa_tem_acesso(self.empresa_id, "auditoria_abaixo_custo"))
        self._mudar_plano("PRATA")
        with self._requisicao():
            self.assertTrue(entitlements.empresa_tem_acesso(self.empresa_id, "auditoria_abaixo_custo"))
            self.assertFalse(entitlements.empresa_tem_acesso(self.empresa_id, "curva_abc_clientes"))
        self._mudar_plano("OURO")
        with self._requisicao():
            self.assertTrue(entitlements.empresa_tem_acesso(self.empresa_id, "curva_abc_clientes"))
            self.assertFalse(entitlements.empresa_tem_acesso(self.empresa_id, "dre_consolidada"))

    def test_entradas_invalidas_negam(self):
        for empresa, chave in ((self.empresa_id, "inexistente"), (self.empresa_id, ""), (self.empresa_id, None),
                               (None, "raio_x_entrada"), ("abc", "raio_x_entrada"), (999999, "raio_x_entrada")):
            with self.subTest(empresa=empresa, chave=chave):
                self.assertFalse(entitlements.empresa_tem_acesso(empresa, chave))

    def test_assinatura_inativa_nega_mesmo_o_que_o_plano_libera(self):
        from horario import agora_utc
        for mudar in (lambda e: setattr(e, "status_conta", "SUSPENSA"),
                      lambda e: setattr(e, "status_conta", "CANCELADA"),
                      lambda e: setattr(e, "deletado_em", agora_utc().replace(tzinfo=None))):
            with self.subTest(), self._requisicao():
                empresa = self.db.session.get(self.Empresa, self.empresa_id)
                empresa.status_conta, empresa.deletado_em = "ATIVA", None
                mudar(empresa)
                self.db.session.commit()
                self.assertFalse(entitlements.empresa_tem_acesso(self.empresa_id, "raio_x_entrada"))

    def test_plano_necessario_e_historico_por_nivel(self):
        with self._requisicao():
            self.assertEqual(entitlements.plano_necessario("margem_bruta").codigo, "PRATA")
            self.assertEqual(entitlements.plano_necessario("dre_consolidada").codigo, "CUSTOM")
            self.assertIsNone(entitlements.plano_necessario("inexistente"))
            empresa = self.db.session.get(self.Empresa, self.empresa_id)
            self.assertEqual(entitlements.limite_historico_meses(empresa), 6)
        self._mudar_plano("OURO")
        with self._requisicao():
            self.assertIsNone(entitlements.limite_historico_meses(self.db.session.get(self.Empresa, self.empresa_id)))

    def test_falha_de_banco_nega_e_volta_a_conferir_o_esquema(self):
        with self._requisicao():
            self.assertTrue(entitlements.esquema_planos_disponivel())
            with patch.object(entitlements, "catalogo", side_effect=OperationalError("SELECT", {}, Exception("x"))):
                self.assertFalse(entitlements.empresa_tem_acesso(self.empresa_id, "raio_x_entrada"))
            self.assertNotIn(self.db.session.get_bind(), entitlements._esquema_por_engine)

    def test_403_json_traz_o_plano_necessario_e_a_url_de_upgrade(self):
        resposta = self.cliente_http.get(API_AUDITORIA)
        self.assertEqual(resposta.status_code, 403)
        corpo = resposta.get_json()
        self.assertEqual(corpo["codigo"], "plano_insuficiente")
        self.assertEqual(corpo["plano_atual"], {"codigo": "BRONZE", "nome": "Bronze"})
        self.assertEqual(corpo["plano_necessario"], {"codigo": "PRATA", "nome": "Prata"})
        self.assertEqual(corpo["url_upgrade"], "/onboarding/planos#prata")
        self.assertEqual(corpo["funcionalidade"]["chave"], "auditoria_abaixo_custo")
        self.assertEqual(corpo["fonte"], "local")
        self.assertIn("Prata", corpo["erro"])
        self.assertIn("no-store", resposta.headers["Cache-Control"])

    def test_403_html_na_pagina_e_json_quando_pedido(self):
        pagina = self.cliente_http.get(AUDITORIA)
        self.assertEqual(pagina.status_code, 403)
        html = pagina.get_data(as_text=True)
        self.assertIn("a partir do plano Prata", html)
        self.assertIn('href="/onboarding/planos#prata"', html)
        pedido_json = self.cliente_http.get(AUDITORIA, headers={"Accept": "application/json"})
        self.assertEqual((pedido_json.status_code, pedido_json.get_json()["plano_necessario"]["codigo"]),
                         (403, "PRATA"))

    def test_erro_de_banco_na_conferencia_responde_503_e_nao_oferece_upgrade(self):
        falha = OperationalError("SELECT", {}, Exception("conexão perdida"))
        with patch.object(entitlements, "funcionalidades_da_empresa", side_effect=falha):
            api = self.cliente_http.get(API)
            pagina = self.cliente_http.get(PAGINA)
        corpo = api.get_json()
        self.assertEqual((api.status_code, corpo["codigo"], corpo["plano_necessario"]),
                         (503, "verificacao_indisponivel", None))
        self.assertEqual(pagina.status_code, 503)
        self.assertNotIn("Conhecer o plano", pagina.get_data(as_text=True))

    def test_pagina_de_planos_com_erro_passageiro_pede_para_tentar_de_novo(self):
        falha = OperationalError("SELECT", {}, Exception("conexão perdida"))
        with patch.object(entitlements, "catalogo_planos", side_effect=falha):
            resposta = self.cliente_http.get("/onboarding/planos")
        html = resposta.get_data(as_text=True)
        self.assertEqual(resposta.status_code, 503)
        self.assertIn("Não foi possível conferir o seu plano agora", html)
        self.assertNotIn("ainda não foram ativados", html)

    def test_assinatura_suspensa_recebe_403_com_o_suporte(self):
        self._status("SUSPENSA")
        corpo = self.cliente_http.get(API).get_json()
        self.assertEqual((corpo["codigo"], corpo["url_upgrade"]), ("assinatura_inativa", "/cliente/tickets"))

    def test_admin_e_anonimo_nao_entram(self):
        admin, _ = self._entrar("admin-test@example.invalid")
        self.assertEqual(admin.get(PAGINA).status_code, 403)
        self.assertEqual(admin.get(API).status_code, 403)
        anonimo = self.app.test_client().get(PAGINA)
        self.assertEqual(anonimo.status_code, 302)
        self.assertIn("/auth/login", anonimo.headers["Location"])

    def test_menu_do_bronze_tem_o_item_do_prata_com_cadeado(self):
        itens = {texto: (href, atributos, conteudo)
                 for texto, href, atributos, conteudo in self._menu(self.cliente_http.get(PAGINA).get_data(as_text=True))}
        self.assertEqual(itens["Raio-X do PDV"][0], PAGINA)
        self.assertIn("active", itens["Raio-X do PDV"][1])
        href, atributos, conteudo = itens["Auditoria de custos UPGRADE"]
        self.assertEqual(href, "/onboarding/planos#prata")
        self.assertIn("is-bloqueado", atributos)
        self.assertIn("disponível a partir do plano Prata", atributos)
        self.assertIn("bi-lock-fill", conteudo)
        self.assertIn('class="nav-roadmap-badge nav-upgrade-badge"', conteudo)

    def test_menu_do_prata_libera_a_auditoria(self):
        self._mudar_plano("PRATA")
        itens = {texto: href for texto, href, *_ in self._menu(self.cliente_http.get(PAGINA).get_data(as_text=True))}
        self.assertEqual(itens["Auditoria de custos"], AUDITORIA)
        self.assertNotIn("Auditoria de custos UPGRADE", itens)

    def test_menu_da_empresa_suspensa_nao_oferece_recurso_de_plano(self):
        self._status("SUSPENSA")
        textos = [t for t, *_ in self._menu(self.cliente_http.get("/cliente/dashboard").get_data(as_text=True))]
        self.assertEqual(textos, ["Visão Geral", "Enviar relatórios", "Histórico", "Guia", "Suporte", "Configurações"])

    def test_menu_sobrevive_a_falha_de_banco(self):
        with patch.object(entitlements, "menu_cliente", side_effect=OperationalError("SELECT", {}, Exception("x"))):
            resposta = self.cliente_http.get("/cliente/dashboard")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(len(self._menu(resposta.get_data(as_text=True))), 6)


# =====================================================================
# Raio-X no portal (páginas e API)
# =====================================================================
class RaioXPortalTests(_PlanosCase):
    def test_envio_grava_o_resultado_e_o_bronze_nao_recebe_custo_nem_margem(self):
        resposta = self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        self.assertEqual((resposta.status_code, resposta.headers["Location"]), (303, PAGINA))
        linha = self._linha()
        self.assertEqual((linha.arquivos, linha.plano_recomendado, linha.valor_recuperavel, linha.indice_saude),
                         (2, "PRATA", Decimal("30.0"), Decimal("63.08")))
        self.assertEqual((linha.periodo_inicio, linha.periodo_fim), (date(2026, 1, 1), date(2026, 2, 28)))
        html = self.cliente_http.get(PAGINA).get_data(as_text=True)
        self.assertIn("Potencial recuperável identificado", html)
        self.assertIn("R$</small> 30,00", html)
        self.assertIn("Conhecer o plano Prata", html)
        self.assertIn("dado-protegido", html)
        # Custo médio do Café (R$ 16,50), margem (R$ 50,00) e margem % (13,2%) não saem para o Bronze.
        for valor in ("16,50", "R$ 50,00", "13,2%", "7,2%"):
            self.assertNotIn(valor, html)
        corpo = self.cliente_http.get(API).get_json()
        item = corpo["resultado"]["scanner"]["itens"][0]
        self.assertEqual((item["custo_medio"], item["margem_valor"], item["margem_percentual"]), (None, None, None))
        self.assertIsNone(corpo["resultado"]["auditoria"]["itens_abaixo_custo"])
        self.assertIsNone(corpo["resultado"]["auditoria"]["precificacao"])
        self.assertTrue(corpo["resultado"]["protegido"]["margem"])
        self.assertEqual(corpo["recomendacao"]["plano"], {"codigo": "PRATA", "nome": "Prata"})
        self.assertTrue(corpo["recomendacao"]["upgrade"])
        self.assertNotIn("16.5", json.dumps(corpo))

    def test_prata_ve_margem_e_a_auditoria(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        html = self.cliente_http.get(PAGINA).get_data(as_text=True)
        self.assertIn("R$ 16,50", html)
        self.assertIn("13,2%", html)
        self.assertNotIn("dado-protegido-marcador", html)
        self.assertIn("já cobre o que o Raio-X encontrou", html)
        auditoria = self.cliente_http.get(AUDITORIA)
        self.assertEqual(auditoria.status_code, 200)
        texto = auditoria.get_data(as_text=True)
        self.assertIn("Leite 1L", texto)
        self.assertIn("Cafe 500g", texto)
        self.assertIn("20,0%", texto)  # alta do custo do Café
        corpo = self.cliente_http.get(API_AUDITORIA).get_json()
        self.assertEqual(corpo["auditoria"]["itens_abaixo_custo"][0]["perda"], 10.0)

    def test_auditoria_mostra_o_custo_a_confirmar_e_o_bronze_nao_recebe_a_lista(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", MotorRaioXTests.CUSTO_ABSURDO))
        texto = self.cliente_http.get(AUDITORIA).get_data(as_text=True)
        self.assertIn("Fora desta perda: 1 item(ns) com custo a confirmar (R$ 39.500,00)", texto)
        self.assertIn("Custo a confirmar:", texto)
        self.assertIn('id="tabelaCustoAConfirmar"', texto)
        self.assertIn("Item Generico", texto)
        bloco = self.cliente_http.get(API_AUDITORIA).get_json()["auditoria"]["custo_a_confirmar"]
        self.assertEqual((bloco["itens"], bloco["lista"][0]["produto"]), (1, "Item Generico"))
        # A página do Raio-X traz o aviso para todos os planos; a lista dos itens é da auditoria de custos.
        self.assertIn("ficaram fora da perda e do potencial recuperável", self.cliente_http.get(PAGINA).get_data(as_text=True))
        self._mudar_plano("BRONZE")
        bronze = self.cliente_http.get(API).get_json()["resultado"]["auditoria"]["custo_a_confirmar"]
        self.assertEqual((bronze["itens"], bronze["lista"], bronze["custo_total"]), (1, None, None))
        self.assertNotIn("40000", json.dumps(self.cliente_http.get(API).get_json()))

    def test_raio_x_gravado_antes_do_saneamento_abre_a_auditoria(self):
        # Raio-X gravado pelo motor de 91e5fb0 (a referência do golden): sem os campos de CMV
        # líquido. A tela e a API continuam abrindo, com os campos novos nulos.
        from tests.test_raio_x_regression import EXPECTED
        antigo = json.loads(EXPECTED.read_text(encoding="utf-8"))["raio_x"]["janeiro_fevereiro"]["resultado"]
        self.assertNotIn("cmv_liquido", antigo["auditoria"])
        self.assertEqual(antigo["versao"], "raio-x-2026-11.3")
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        linha = self._linha()
        gravado = json.loads(linha.resultado)
        linha.resultado = json.dumps({**antigo, "recomendacao": gravado["recomendacao"],
                                      "nomes_arquivos": gravado["nomes_arquivos"]})
        linha.versao_regras = antigo["versao"]
        self.db.session.commit()
        pagina = self.cliente_http.get(AUDITORIA)
        self.assertEqual(pagina.status_code, 200)
        self.assertNotIn("Líquida, com bonificações e devoluções", pagina.get_data(as_text=True))
        corpo = self.cliente_http.get(API_AUDITORIA)
        self.assertEqual(corpo.status_code, 200)
        self.assertIsNone(corpo.get_json()["auditoria"]["cmv_liquido"])
        self.assertEqual(self.cliente_http.get(PAGINA).status_code, 200)

    def test_raio_x_novo_grava_a_versao_do_saneamento(self):
        self._enviar_raio_x(("jan.csv", JANEIRO))
        self.assertEqual(self._linha().versao_regras, raio_x.VERSAO_REGRAS)
        # 11.4: saneamento de 11/2026; 11.5: unificação do motor (custo a confirmar, centavos, formatos).
        self.assertEqual(raio_x.VERSAO_REGRAS, "raio-x-2026-11.5")

    def test_api_envia_e_le_o_raio_x(self):
        self.assertEqual(self.cliente_http.get(API).status_code, 404)
        resposta = self._enviar_raio_x(("jan.csv", JANEIRO), api=True)
        self.assertEqual(resposta.status_code, 201)
        corpo = resposta.get_json()
        self.assertEqual(corpo["resultado"]["recuperavel"]["total"], 30.0)
        self.assertIn("no-store", resposta.headers["Cache-Control"])
        self.assertEqual(self.cliente_http.get(API).get_json()["resultado"]["saude"],
                         corpo["resultado"]["saude"])

    def test_envio_em_xls_e_em_planilha_xml_como_o_erp_exporta(self):
        from tests import leiautes_erp_support as leiautes
        pasta = tempfile.TemporaryDirectory(prefix="raio-x-formatos-")
        self.addCleanup(pasta.cleanup)
        linhas, mesclas, formatos, verdade = leiautes.relatorio_por_produto()
        xls = Path(leiautes.gravar_xls(linhas, mesclas, formatos, Path(pasta.name) / "giro.xls")).read_bytes()
        xml = Path(leiautes.gravar_planilha_xml(linhas, mesclas, Path(pasta.name) / "giro.xml")).read_bytes()
        for nome, conteudo in (("giro.xls", xls), ("giro.xml", xml)):
            with self.subTest(arquivo=nome):
                resposta = self._enviar_raio_x((nome, conteudo), api=True)
                self.assertEqual(resposta.status_code, 201, resposta.get_data(as_text=True)[:300])
                self.assertEqual(resposta.get_json()["resultado"]["saude"]["faturamento_total"], verdade["venda"])
        self.assertEqual(self._linha().versao_regras, raio_x.VERSAO_REGRAS)
        # Página HTML com a extensão .xls (a "exportação para Excel" de sistemas web) continua recusada,
        # e o Raio-X gravado fica como estava.
        html = b"<html><body><table><tr><td>Produto</td><td>Qtde</td></tr></table></body></html>"
        recusa = self._enviar_raio_x(("exportado.xls", html), api=True)
        self.assertEqual((recusa.status_code, recusa.get_json()["codigo"]), (422, "quarentena:marcacao"))
        self.assertIn("Arquivo 1: arquivo fora do padrão aceito", recusa.get_json()["erro"])
        self.assertIn('accept=".csv,.xlsx,.xls,.xml"', self.cliente_http.get(PAGINA).get_data(as_text=True))

    def test_api_sem_csrf_e_recusada(self):
        resposta = self.cliente_http.post(API, data={"arquivos": [(io.BytesIO(JANEIRO), "jan.csv")]},
                                          content_type="multipart/form-data")
        self.assertEqual(resposta.status_code, 400)
        self.assertIsNone(self._linha())

    def test_erros_de_envio_nao_gravam_e_explicam(self):
        casos = (
            ((), "Selecione ao menos um relatório"),
            ((("notas.txt", b"texto"),), "formato não suportado"),
            ((("jan.csv", JANEIRO), ("copia.csv", JANEIRO)), "é igual a outro arquivo"),
            ((("vazio.csv", b""),), "está vazio"),
            ((("jan.csv", JANEIRO), ("ruim.csv", b"A;B\n1;2\n")), "Arquivo 2: não foi possível identificar"),
        )
        for arquivos, mensagem in casos:
            with self.subTest(mensagem=mensagem):
                html = self._enviar_raio_x(*arquivos, seguir=True).get_data(as_text=True)
                self.assertIn(mensagem, html)
                self.assertIsNone(self._linha())
        demais = [(f"m{i}.csv", JANEIRO + str(i).encode()) for i in range(raio_x.MAX_ARQUIVOS + 1)]
        corpo = self._enviar_raio_x(*demais, api=True).get_json()
        self.assertEqual(corpo["codigo"], "raio_x_arquivos")

    def test_envio_acima_do_limite_somado_responde_413_em_portugues(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 2048
        grande = ("jan.csv", JANEIRO + b"9;Item;1;1,00;1,00;0,50\n" * 200)
        pagina = self._enviar_raio_x(grande)
        self.assertEqual(pagina.status_code, 413)
        self.assertIn("Envio grande demais", pagina.get_data(as_text=True))
        self.assertIn("passam de 2 KB somados. Nada foi gravado: envie menos meses por vez.",
                      pagina.get_data(as_text=True))
        api = self._enviar_raio_x(grande, api=True)
        self.assertEqual(api.status_code, 413)
        self.assertEqual(api.get_json()["fonte"], "local")
        self.assertIsNone(self._linha())
        # Formulário de um arquivo só: o conselho é outro.
        unico = self.cliente_http.post("/cliente/upload", data={"arquivo": (io.BytesIO(grande[1]), "jan.csv")},
                                       content_type="multipart/form-data")
        self.assertEqual(unico.status_code, 413)
        self.assertIn("envie um arquivo menor", unico.get_data(as_text=True))

    def test_envio_ocupado_nao_processa(self):
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            html = self._enviar_raio_x(("jan.csv", JANEIRO), seguir=True).get_data(as_text=True)
            self.assertIn("Há outro envio ou processamento em andamento", html)
            api = self._enviar_raio_x(("jan.csv", JANEIRO), api=True)
            # A fila de espera do cliente: 503 com o tempo sugerido para tentar de novo.
            self.assertEqual((api.status_code, api.headers.get("Retry-After")), (503, "30"))
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()
        self.assertIsNone(self._linha())

    def test_processo_isolado_ocupado_responde_como_a_fila_de_espera(self):
        # A vaga do processo isolado (etl_runtime._ADMISSION) ocupada: o _run_job real recusa antes
        # de abrir processo; a API responde 503 com Retry-After e a página avisa, sem gravar nada.
        import etl_runtime
        real = lambda caminhos, limits=None: etl_runtime._run_job("raio_x", caminhos, limits=limits)
        self.assertTrue(etl_runtime._ADMISSION.acquire(blocking=False))
        try:
            with patch("etl_runtime.run_raio_x", side_effect=real):
                html = self._enviar_raio_x(("jan.csv", JANEIRO), seguir=True).get_data(as_text=True)
                api = self._enviar_raio_x(("jan.csv", JANEIRO), api=True)
        finally:
            etl_runtime._ADMISSION.release()
        self.assertIn("Há outro envio ou processamento em andamento", html)
        self.assertEqual((api.status_code, api.headers.get("Retry-After")), (503, "30"))
        self.assertEqual(api.get_json()["erro"], "Há outro envio ou processamento em andamento. Tente novamente em instantes.")
        self.assertIsNone(self._linha())

    def test_assinatura_suspensa_durante_a_leitura_nao_grava(self):
        def suspender_no_meio(caminhos, limits=None):
            resultado = executar_como_o_worker(caminhos, limits)
            self.db.session.get(self.Empresa, self.empresa_id).status_conta = "SUSPENSA"
            self.db.session.commit()
            return resultado
        with patch("etl_runtime.run_raio_x", side_effect=suspender_no_meio):
            html = self._enviar_raio_x(("jan.csv", JANEIRO), seguir=True).get_data(as_text=True)
        self.assertIn("o Raio-X não foi gravado", html)
        self.assertIsNone(self._linha())

    def test_cada_empresa_ve_so_o_proprio_raio_x(self):
        self._enviar_raio_x(("jan.csv", JANEIRO))
        self._novo_cliente("outra-empresa@example.invalid", id_empresa=self.outra_empresa_id)
        outra, _ = self._entrar("outra-empresa@example.invalid")
        self.assertEqual(outra.get(API).status_code, 404)
        self.assertNotIn("Potencial recuperável identificado", outra.get(PAGINA).get_data(as_text=True))

    def test_novo_envio_substitui_o_anterior(self):
        self._enviar_raio_x(("jan.csv", JANEIRO))
        self._enviar_raio_x(("fev.csv", FEVEREIRO))
        self.assertEqual(self._contar(self.DiagnosticoEntrada), 1)
        self.assertEqual(self._linha().periodo_inicio, date(2026, 2, 1))

    def test_historico_do_bronze_mostra_os_ultimos_seis_meses(self):
        meses = [(f"{mes:02d}.csv", _mes(2025, mes, [f"1;Cafe;10;20,00;{180 + mes},00;150,00"])) for mes in range(1, 9)]
        self._enviar_raio_x(*meses)
        resultado = self.cliente_http.get(API).get_json()["resultado"]
        self.assertEqual([f["rotulo"] for f in resultado["fatias"]],
                         ["03/2025", "04/2025", "05/2025", "06/2025", "07/2025", "08/2025"])
        self.assertEqual(resultado["historico"], {"limite_meses": 6, "ocultas": 2, "primeira_oculta": "01/2025",
                                                  "ultima_oculta": "02/2025"})
        html = self.cliente_http.get(PAGINA).get_data(as_text=True)
        self.assertIn("2 relatórios anteriores (01/2025 a 02/2025) ficam fora do histórico do seu plano "
                      "(últimos 6 meses)", html)
        self.assertIn("O plano Prata mostra até 24 meses", html)
        self._mudar_plano("PRATA")
        self.assertEqual(len(self.cliente_http.get(API).get_json()["resultado"]["fatias"]), 8)

    def test_exclusao_permanente_da_empresa_leva_o_raio_x(self):
        from horario import agora_utc
        self._enviar_raio_x(("jan.csv", JANEIRO))
        empresa = self.db.session.get(self.Empresa, self.empresa_id)
        empresa.deletado_em = agora_utc().replace(tzinfo=None)
        self.db.session.commit()
        admin, _ = self._entrar("admin-test@example.invalid")
        token = self._csrf(admin, "/admin/lixeira")
        resposta = admin.post(f"/admin/empresa/excluir-permanente/{self.empresa_id}",
                              data={"csrf_token": token, "senha_confirmacao": SENHA})
        self.assertEqual(resposta.status_code, 302)
        self.assertIsNone(self._linha())  # expira o mapa de identidade antes de reler a empresa
        self.assertIsNone(self.db.session.get(self.Empresa, self.empresa_id))

    def test_aceite_pendente_dos_termos_trava_a_api_com_428(self):
        from horario import agora_utc
        from models import DocumentoLegal
        self.db.session.add(DocumentoLegal(tipo="TERMOS_USO", versao=1, conteudo="Termo fictício.",
                                           publicado_em=agora_utc(), id_usuario_admin=self.admin_id))
        self.db.session.commit()
        self.assertEqual(self.cliente_http.get(API).status_code, 428)


# =====================================================================
# Página de planos
# =====================================================================
class PaginaPlanosTests(_PlanosCase):
    def test_mostra_os_niveis_sem_precos_e_marca_o_atual(self):
        resposta = self.cliente_http.get("/onboarding/planos")
        self.assertEqual(resposta.status_code, 200)
        html = resposta.get_data(as_text=True)
        for ancora in ("bronze", "prata", "ouro", "custom"):
            self.assertIn(f'id="{ancora}"', html)
        bronze = html.split('id="bronze"', 1)[1].split("</article>", 1)[0]
        self.assertIn("Seu plano", bronze)
        self.assertIn("Últimos 6 meses", bronze)
        self.assertIn("Em implantação", html.split('id="ouro"', 1)[1].split("</article>", 1)[0])
        self.assertNotIn("Em implantação", html.split('id="prata"', 1)[1].split("</article>", 1)[0])
        grade = html.split('class="planos-grade', 1)[1].split("mudarPlanoTitulo", 1)[0]
        for preco in ("150,00", "300,00", "650,00"):
            self.assertNotIn(preco, grade)
        self.assertIsNone(re.search(r"R\$\s*\d", grade))  # "Margem bruta (R$ e %)" é nome, não preço

    def test_pedido_de_mudanca_abre_um_chamado(self):
        from models import ChamadoSuporte
        html = self.cliente_http.get("/onboarding/planos").get_data(as_text=True)
        opcoes = re.findall(r'<option value="([^"]+)"', html)
        self.assertEqual(opcoes, ["Mudança de plano: Prata", "Mudança de plano: Ouro",
                                  "Mudança de plano: Custom / Enterprise"])
        resposta = self._enviar("/cliente/tickets", cliente=self.cliente_http, pagina="/onboarding/planos",
                                seguir=False, assunto=opcoes[0], categoria="FINANCEIRO",
                                mensagem="Quero mudar de plano.")
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(self.db.session.scalar(select(ChamadoSuporte.assunto)), "Mudança de plano: Prata")


if __name__ == "__main__":
    unittest.main()
