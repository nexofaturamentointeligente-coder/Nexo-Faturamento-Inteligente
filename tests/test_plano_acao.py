"""Plano de Ação (12 meses) e o horizonte de tempo do Raio-X do PDV e da Auditoria de custos (Etapa 2, 14/11/2026).

Executar da raiz: python -B -m unittest tests.test_plano_acao -v
Sem rede; o Raio-X sai do motor de verdade no próprio processo (tests.raio_x_support), e as telas saem do app
com as migrações reais em SQLite (o caso base de tests.test_planos_raio_x).

  - O horizonte: os dias cobertos (o mês fechado vale 1, o mês parcial pela fração dos dias), cortados na data do
    Raio-X, as lacunas e o envio sem datas (sem projeção); os meses auditados (só os relatórios com custo).
  - O ritmo por mês e a projeção de 12 meses, em centavos e metade para cima, sem NaN, divisão por zero nem erro com
    valor absurdo que o motor aceita.
  - O selo de amostra parcial, lido do aviso que o próprio motor escreve.
  - O Plano de Ação: os cenários de 12 meses (70% e 100% + 1,5% de margem), as 3 etapas com os produtos reais,
    a etapa já regularizada, a que não dá para avaliar (sem coluna de custo, coluna vazia, sem preço, sem estoque)
    e a lista que o plano esconde (só a contagem e o cadeado).
  - O campo novo do motor (raio-x-2026-11.7): a venda dos produtos com variação de preço ou sem custo.
  - As telas: o botão, o modal, o período no card do dinheiro recuperável e a janela dos cards da Auditoria.
"""

from datetime import date
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

import pandas as pd

import plano_acao
import raio_x
import raio_x_servico
from tests.raio_x_support import FEVEREIRO, JANEIRO, executar_como_o_worker, relatorio_do_mes
# Pelo módulo, e não pelo nome: a classe importada no módulo seria coletada (e rodada) de novo aqui.
import tests.test_planos_raio_x as planos
from tests.test_raio_x_apresentacao import BRONZE, PRATA

PAGINA, AUDITORIA = planos.PAGINA, planos.AUDITORIA
CUSTO_ABSURDO = planos.MotorRaioXTests.CUSTO_ABSURDO
_CAB = "Relatorio de vendas por produto\nPeriodo: {}\nCodigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque\n"
_CAB_SEM_CUSTO = "Relatorio de vendas por produto\nPeriodo: {}\nCodigo;Produto;Qtde;Preco Unitario;Valor Total\n"
# Tudo em dia: custo informado, preço de tabela respeitado, estoque positivo.
LIMPO = (_CAB.format("01/03/2026 a 31/03/2026") + "1;Cafe 500g;10;20,00;200,00;150,00;5\n"
         "2;Acucar 1kg;5;6,00;30,00;20,00;3\n").encode("utf-8")
SEM_CUSTO = (_CAB_SEM_CUSTO.format("01/03/2026 a 31/03/2026") + "1;Cafe 500g;10;20,00;180,00\n").encode("utf-8")
# A coluna de custo existe, mas vazia: nenhuma venda com custo informado.
COLUNA_VAZIA = (_CAB.format("01/03/2026 a 31/03/2026") + "1;Cafe 500g;10;20,00;200,00;;5\n"
                "2;Acucar 1kg;5;6,00;30,00;;3\n3;Leite 1L;20;5,00;100,00;;4\n").encode("utf-8")
SEM_DATAS = ("Relatorio de vendas por produto\nCodigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n"
             "1;Cafe 500g;10;20,00;180,00;190,00\n").encode("utf-8")
# Tudo com custo e sem preço variando, mas sem a coluna de estoque.
SEM_ESTOQUE = ("Relatorio de vendas por produto\nPeriodo: 01/03/2026 a 31/03/2026\n"
               "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n1;Cafe 500g;10;20,00;200,00;150,00\n"
               ).encode("utf-8")


def _diagnosticar(*conteudos):
    with tempfile.TemporaryDirectory(prefix="plano-acao-") as pasta:
        caminhos = []
        for numero, conteudo in enumerate(conteudos, 1):
            caminho = Path(pasta) / f"arquivo_{numero:02d}.csv"
            caminho.write_bytes(conteudo)
            caminhos.append(str(caminho))
        return executar_como_o_worker(caminhos)


def _plano(bruto, liberadas=PRATA, ate=None):
    return plano_acao.preparar(raio_x_servico.visao(bruto, liberadas, None), bruto, ate)


def _numeros(valor):
    """Todos os números de uma estrutura (para conferir que nenhum é NaN nem infinito)."""
    if isinstance(valor, bool):
        return
    if isinstance(valor, (int, float)):
        yield valor
    elif isinstance(valor, dict):
        for item in valor.values():
            yield from _numeros(item)
    elif isinstance(valor, (list, tuple)):
        for item in valor:
            yield from _numeros(item)


def _aviso_de_amostra(vendidos: int, com_saldo: int, dias: int = 120, numero: str = "1") -> str:
    saldos = pd.Series({f"p{i}": 5.0 for i in range(com_saldo)})
    vendas = pd.Series([f"p{i}" for i in range(vendidos)])
    aviso = raio_x._aviso_de_amostra_parcial(saldos, vendas, (date(2026, 1, 1), date(2026, 1, 1) + pd.Timedelta(days=dias - 1)),
                                             numero)
    assert aviso is not None
    return aviso


def _fatia(inicio, fim, indice=0):
    return {"inicio": inicio, "fim": fim, "indice": indice}


# =====================================================================
# Horizonte e ritmo
# =====================================================================
class HorizonteTests(unittest.TestCase):
    def test_meses_fechados_valem_um_cada(self):
        tempo = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia("2026-02-01", "2026-02-28")])
        self.assertEqual((tempo["meses"], tempo["lacunas"], tempo["projetavel"], tempo["motivo"]), (2, 0, True, None))
        self.assertEqual((tempo["inicio"], tempo["fim"]), (date(2026, 1, 1), date(2026, 2, 28)))
        self.assertEqual(tempo["texto"], "2 meses (01/01/2026 a 28/02/2026)")

    def test_mes_parcial_entra_pela_fracao_dos_dias(self):
        # 17/08 a 14/11/2026 (os últimos 90 dias): 15/31 de agosto, setembro e outubro inteiros e 14/30 de novembro,
        # 2,95 meses (e não 4).
        tempo = plano_acao.horizonte([_fatia("2026-08-17", "2026-11-14")])
        self.assertEqual(tempo["meses"], Decimal(15) / 31 + 2 + Decimal(14) / 30)
        self.assertEqual(tempo["meses_texto"], "3 meses")
        # 01/09 a 14/11: setembro e outubro inteiros e 14/30 de novembro, 2,47 meses.
        self.assertEqual(plano_acao.horizonte([_fatia("2026-09-01", "2026-11-14")])["meses_texto"], "2,5 meses")
        # 15/01 a 14/02: 17/31 + 14/28 = 1,048 mês, mostrado como "1 mês" (e não "2 meses").
        tempo = plano_acao.horizonte([_fatia("2026-01-15", "2026-02-14")])
        self.assertEqual(tempo["meses"], Decimal(17) / 31 + Decimal(14) / 28)
        self.assertEqual(tempo["meses_texto"], "1 mês")

    def test_corta_na_data_do_raio_x(self):
        # O relatório por produto com o ano inteiro no cabeçalho, enviado em 14/11/2026: dezembro ainda não começou.
        tempo = plano_acao.horizonte([_fatia("2026-01-01", "2026-12-31")], date(2026, 11, 14))
        self.assertEqual(tempo["meses"], 10 + Decimal(14) / 30)
        self.assertEqual(tempo["fim"], date(2026, 11, 14))
        self.assertEqual(tempo["texto"], "10,5 meses (01/01/2026 a 14/11/2026)")
        # Sem a data do Raio-X, o período do cabeçalho vale inteiro.
        self.assertEqual(plano_acao.horizonte([_fatia("2026-01-01", "2026-12-31")])["meses"], 12)

    def test_relatorio_com_venda_depois_da_data_do_raio_x_tira_a_projecao(self):
        futura = {**_fatia("2026-12-01", "2026-12-31"), "faturamento": 100.0, "itens": 1}
        tempo = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), futura], date(2026, 11, 14))
        self.assertEqual((tempo["meses"], tempo["projetavel"], tempo["motivo"], tempo["sem_data"], tempo["futuro"]),
                         (1, False, "periodo_futuro", 0, 1))
        self.assertEqual(tempo["texto"], "1 mês (01/01/2026 a 31/01/2026), mais 1 relatório com período depois da "
                                         "data do Raio-X")
        self.assertEqual(tempo["linha_sem_ritmo"],
                         "Sem ritmo por mês: um ou mais relatórios trazem um período depois da data do Raio-X.")
        sozinha = plano_acao.horizonte([futura], date(2026, 11, 14))
        self.assertEqual(sozinha["linha_periodo"], "Período analisado: os relatórios trazem um período depois da data "
                                                   "do Raio-X")
        self.assertEqual(plano_acao.janela(sozinha, sozinha, None),
                         "Relatórios com período depois da data do Raio-X: sem média por mês")

    def test_relatorio_sem_datas_e_outro_com_periodo_futuro(self):
        # 1 relatório sem datas e 1 com período (e venda) depois da data do Raio-X: os textos não supõem quantos há
        # de cada caso, e o aviso do plano diz o que fazer com cada um.
        futura = {**_fatia("2026-12-01", "2026-12-31"), "faturamento": 100.0, "itens": 1}
        tempo = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia(None, None), futura],
                                     date(2026, 11, 14))
        self.assertEqual((tempo["meses"], tempo["projetavel"], tempo["motivo"], tempo["sem_data"], tempo["futuro"]),
                         (1, False, "sem_datas_e_futuro", 1, 1))
        self.assertEqual(tempo["texto"], "1 mês (01/01/2026 a 31/01/2026), mais 1 relatório sem datas, mais 1 "
                                         "relatório com período depois da data do Raio-X")
        self.assertEqual(tempo["linha_sem_ritmo"], "Sem ritmo por mês: há relatórios sem as datas do período e com "
                                                   "período depois da data do Raio-X.")
        self.assertEqual(plano_acao.janela(tempo, tempo, None),
                         "Em 1 mês com datas, mais 1 relatório sem datas, mais 1 relatório com período depois da data "
                         "do Raio-X: sem média por mês")
        aviso = plano_acao.SEM_PROJECAO["sem_datas_e_futuro"]
        self.assertTrue(aviso.startswith("Um ou mais relatórios enviados não trazem as datas do período, e um ou mais "
                                         "trazem um período depois da data do Raio-X"))
        self.assertIn("Envie com o período no topo os relatórios que não o trazem, confira o período dos demais", aviso)
        self.assertNotIn("Alguns", aviso)
        sozinhos = plano_acao.horizonte([_fatia(None, None), futura], date(2026, 11, 14))
        self.assertEqual(sozinhos["linha_periodo"], "Período analisado: os relatórios não trazem as datas ou trazem um "
                                                    "período depois da data do Raio-X")
        self.assertEqual(plano_acao.janela(sozinhos, sozinhos, None),
                         "Relatórios sem as datas do período ou com período depois da data do Raio-X: sem média por mês")

    def test_fatia_vazia_do_mes_seguinte_e_ignorada(self):
        # O motor fatia pelo relógio dele (UTC): às 22h de 31/03 em Brasília, ele já cria abril, vazio.
        vazia = {**_fatia("2026-04-01", "2026-04-30"), "faturamento": 0.0, "itens": 0}
        tempo = plano_acao.horizonte([_fatia("2026-03-01", "2026-03-31"), vazia], date(2026, 3, 31))
        self.assertEqual((tempo["meses"], tempo["projetavel"], tempo["motivo"], tempo["futuro"]), (1, True, None, 0))

    def test_mesmo_dia_em_dois_relatorios_conta_uma_vez(self):
        tempo = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia("2026-01-10", "2026-01-20")])
        self.assertEqual(tempo["meses"], 1)
        tempo = plano_acao.horizonte([_fatia("2026-01-01", "2026-02-15"), _fatia("2026-02-01", "2026-02-28")])
        self.assertEqual(tempo["meses"], 2)

    def test_meses_sem_relatorio_no_meio_viram_lacunas(self):
        # Janeiro e abril: o ritmo divide por 2 meses com venda, não pelos 4 do intervalo.
        tempo = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia("2026-04-01", "2026-04-30")])
        self.assertEqual((tempo["meses"], tempo["lacunas"], tempo["projetavel"]), (2, 2, True))
        self.assertEqual(tempo["texto"], "2 meses (01/01/2026 a 30/04/2026), sem relatório em 2 meses desse intervalo")
        self.assertEqual(plano_acao.ritmo(100.0, tempo)["mensal"], 50.0)

    def test_sem_datas_nao_ha_projecao(self):
        for fatias in ([], None, [_fatia(None, None)], [{"inicio": "lixo", "fim": "2026-01-31"}],
                       [_fatia("2026-02-01", "2026-01-01")], ["texto"]):
            with self.subTest(fatias=fatias):
                tempo = plano_acao.horizonte(fatias)
                self.assertEqual((tempo["meses"], tempo["projetavel"], tempo["motivo"]), (None, False, "sem_datas"))
                self.assertIsNone(plano_acao.ritmo(100.0, tempo))

    def test_um_relatorio_sem_datas_no_envio_tira_a_projecao(self):
        # As vendas do relatório sem datas entram nos totais: dividir só pelos meses datados exageraria o ritmo.
        tempo = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia(None, None)])
        self.assertEqual((tempo["meses"], tempo["projetavel"], tempo["motivo"]), (1, False, "sem_datas"))
        self.assertIsNone(plano_acao.ritmo(100.0, tempo))
        self.assertEqual(tempo["texto"], "1 mês (01/01/2026 a 31/01/2026), mais 1 relatório sem datas")
        dois = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia(None, None), _fatia(None, None)])
        self.assertTrue(dois["texto"].endswith(", mais 2 relatórios sem datas"))

    def test_texto_dos_meses(self):
        for meses, texto in ((Decimal(1), "1 mês"), (Decimal("1.5"), "1,5 mês"), (Decimal(2), "2 meses"),
                             (Decimal("2.94"), "2,9 meses"), (Decimal("0.4"), "menos de 1 mês"), (Decimal(12), "12 meses")):
            with self.subTest(meses=meses):
                self.assertEqual(plano_acao.texto_dos_meses(meses), texto)


class RitmoTests(unittest.TestCase):
    TRES_MESES = {"meses": Decimal(3), "projetavel": True}

    def test_projecao_e_o_ritmo_vezes_doze_sem_arredondar_o_ritmo_antes(self):
        ritmo = plano_acao.ritmo(1000.0, self.TRES_MESES)
        # 1.000 / 3 = 333,333...: o mês mostra 333,33, e os 12 meses saem da conta exata (4.000,00), não de 333,33 x 12.
        self.assertEqual(ritmo, {"total": 1000.0, "mensal": 333.33, "anual": 4000.0})

    def test_centavo_arredonda_metade_para_cima(self):
        # 0,05 / 2 = 0,025: metade para cima dá 0,03 (o arredondamento do Python daria 0,02).
        self.assertEqual(plano_acao.ritmo(0.05, {"meses": Decimal(2), "projetavel": True})["mensal"], 0.03)

    def test_valor_que_nao_e_numero_nao_vira_conta(self):
        for valor in (None, "100", True, float("nan"), float("inf"), [], {}):
            with self.subTest(valor=valor):
                self.assertIsNone(plano_acao.ritmo(valor, self.TRES_MESES))

    def test_meses_efetivos_nunca_abaixo_de_um(self):
        self.assertIsNone(plano_acao.ritmo(100.0, {"meses": 0, "projetavel": True}))
        self.assertIsNone(plano_acao.ritmo(100.0, {"meses": None, "projetavel": True}))
        self.assertEqual(plano_acao.ritmo(100.0, {"meses": Decimal(1), "projetavel": True})["anual"], 1200.0)
        # Meio mês de vendas: meses_efetivos = max(1, 0,5) = 1 (o ritmo não dobra).
        self.assertEqual(plano_acao.ritmo(100.0, {"meses": Decimal("0.5"), "projetavel": True})["mensal"], 100.0)

    def test_valor_absurdo_nao_quebra_a_conta(self):
        # 28 dígitos do contexto padrão do Decimal não bastam: 1e29 por mês daria InvalidOperation no quantize.
        for valor in (1e29, 1e100, 9.99e25):
            with self.subTest(valor=valor):
                ritmo = plano_acao.ritmo(valor, {"meses": Decimal(3), "projetavel": True})
                self.assertTrue(all(math.isfinite(numero) for numero in ritmo.values()))


class JanelaTests(unittest.TestCase):
    DOIS = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia("2026-02-01", "2026-02-28")])
    UM = plano_acao.horizonte([_fatia("2026-02-01", "2026-02-28")])

    def test_texto_com_a_media(self):
        self.assertEqual(plano_acao.janela(self.DOIS, self.DOIS, {"mensal": 5.0}), "Nos 2 meses auditados (média de R$ 5,00/mês)")
        self.assertEqual(plano_acao.janela(self.UM, self.UM, {"mensal": 1234.5}), "No 1 mês auditado (média de R$ 1.234,50/mês)")
        self.assertEqual(plano_acao.janela(self.DOIS, self.DOIS, None), "Nos 2 meses auditados")

    def test_de_n_enviados_so_quando_os_numeros_mostrados_diferem(self):
        # 1 mês auditado de 1,03 enviado: os dois aparecem como "1 mês", então não há "de 1 mês enviados".
        quase = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia("2026-02-01", "2026-02-01")])
        self.assertEqual(plano_acao.janela(self.UM, quase, None), "No 1 mês auditado")
        meio = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia("2026-02-01", "2026-02-14")])
        self.assertEqual(plano_acao.janela(self.UM, meio, None), "No 1 mês auditado de 1,5 mês enviado")

    def test_meses_sem_custo_nao_sao_auditados(self):
        self.assertEqual(plano_acao.janela(self.UM, self.DOIS, {"mensal": 60.0}),
                         "No 1 mês auditado de 2 meses enviados (média de R$ 60,00/mês)")
        sem_custo = plano_acao.horizonte([])
        self.assertEqual(plano_acao.janela(sem_custo, self.DOIS, None), "Sem custo informado nos relatórios: sem média por mês")

    def test_sem_datas(self):
        self.assertEqual(plano_acao.janela(plano_acao.horizonte([]), plano_acao.horizonte([]), None),
                         "Relatórios sem as datas do período: sem média por mês")
        misto = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia(None, None)])
        self.assertEqual(plano_acao.janela(misto, misto, None), "Em 1 mês com datas, mais 1 relatório sem datas: sem média por mês")
        dois = plano_acao.horizonte([_fatia("2026-01-01", "2026-01-31"), _fatia(None, None), _fatia(None, None)])
        self.assertEqual(plano_acao.janela(dois, dois, None), "Em 1 mês com datas, mais 2 relatórios sem datas: sem média por mês")


# =====================================================================
# Amostra parcial e estoque
# =====================================================================
class AmostraParcialTests(unittest.TestCase):
    def test_le_o_percentual_do_aviso_do_motor(self):
        aviso = _aviso_de_amostra(12, 100)
        self.assertIn("(12%)", aviso)
        self.assertEqual(plano_acao.amostra_parcial(["Outro aviso.", aviso]), {"percentual": 12, "avisos": 1})

    def test_com_dois_avisos_fica_o_maior_percentual(self):
        avisos = [_aviso_de_amostra(5, 100), _aviso_de_amostra(1210, 10000, dias=400, numero="2")]
        self.assertEqual(plano_acao.amostra_parcial(avisos), {"percentual": 12, "avisos": 2})

    def test_sem_o_aviso_nao_ha_selo(self):
        for avisos in (None, [], ["Amostra parcial: texto que não é do motor."], [None, 3]):
            with self.subTest(avisos=avisos):
                self.assertIsNone(plano_acao.amostra_parcial(avisos))

    def test_estoque_nos_relatorios_pelo_indicador_de_ruptura(self):
        com, sem = _diagnosticar(JANEIRO), _diagnosticar(CUSTO_ABSURDO)
        self.assertIs(plano_acao.tem_estoque(com), True)
        self.assertIs(plano_acao.tem_estoque(sem), False)
        # Raio-X gravado antes dos indicadores (raio-x-2026-11.6): não se sabe.
        sem["indicadores_executivos"] = None
        self.assertIsNone(plano_acao.tem_estoque(sem))
        self.assertIsNone(plano_acao.tem_estoque({}))


# =====================================================================
# Campo novo do motor (raio-x-2026-11.7)
# =====================================================================
class VendaComVariacaoOuSemCustoTests(unittest.TestCase):
    def test_uniao_das_duas_partes(self):
        bruto = _diagnosticar(JANEIRO, FEVEREIRO)
        self.assertEqual(bruto["versao"], "raio-x-2026-11.7")
        # Café (variação de preço): toda a venda, 180 + 200; Açúcar (sem variação): só a venda sem custo, 30 em
        # janeiro; Leite: tem custo e não tem variação. 410 de 650.
        scanner, saude = bruto["scanner"], bruto["saude"]
        self.assertEqual((scanner["faturamento_envolvido"], saude["faturamento_sem_custo"]), (380.0, 30.0))
        self.assertEqual(scanner["faturamento_variacao_ou_sem_custo"], 410.0)

    def test_produto_com_variacao_e_sem_custo_nao_conta_duas_vezes(self):
        bruto = _diagnosticar(SEM_CUSTO)
        # O único produto vendeu abaixo do preço de tabela e sem custo: a venda dele conta uma vez.
        self.assertEqual((bruto["scanner"]["faturamento_envolvido"], bruto["saude"]["faturamento_sem_custo"]),
                         (180.0, 180.0))
        self.assertEqual(bruto["scanner"]["faturamento_variacao_ou_sem_custo"], 180.0)


# =====================================================================
# O Plano de Ação
# =====================================================================
class PlanoDeAcaoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dois_meses = _diagnosticar(JANEIRO, FEVEREIRO)

    def test_comparativo_de_12_meses(self):
        pa = _plano(self.dois_meses)
        self.assertEqual(pa["recuperavel"], {"total": 30.0, "mensal": 15.0, "anual": 180.0})
        plano = pa["plano"]
        # Custo da inação: 30 em 2 meses = 15 por mês = 180 em 12; no escuro: 30 sem custo em 2 meses = 180 em 12.
        self.assertEqual(plano["inacao"], {"anual": 180.0, "mensal": 15.0, "escuro_anual": 180.0, "escuro_mensal": 15.0,
                                           "pct_sem_custo": 0.0462})
        # Conservador: 70% de 180 = 126. Otimizado: 180 + 1,5% de (410 / 2 x 12 = 2.460) = 180 + 36,90 = 216,90.
        self.assertEqual(plano["retorno"], {"conservador": 126.0, "otimizado": 216.9, "sangria_anual": 180.0,
                                            "base_anual": 2460.0, "ganho_margem": 36.9, "sem_base": False})
        self.assertEqual((pa["percentual_conservador"], pa["percentual_ganho_de_margem"]), ("70%", "1,5%"))
        self.assertEqual(pa["explicacao_do_retorno"], "Esse valor vai direto para o lucro líquido da empresa, pois as "
                                                      "despesas fixas da loja já estão pagas.")
        # Cards da Auditoria: a perda de 10 (o Leite em janeiro) nos 2 meses auditados.
        self.assertEqual(pa["janela_prejuizo"], "Nos 2 meses auditados (média de R$ 5,00/mês)")

    def test_etapas_com_os_produtos_reais(self):
        estancar, travar, controle = _plano(self.dois_meses)["plano"]["etapas"]
        self.assertEqual([(e["numero"], e["prazo"], e["titulo"]) for e in (estancar, travar, controle)],
                         [(1, "Semana 1", "Estancar prejuízo imediato"), (2, "Semanas 2 e 3", "Travar descontos no balcão"),
                          (3, "Mês 1 em diante", "Controle e previsibilidade")])
        # Etapa 1: o Leite vendido a 5,00 a unidade com custo de 5,50 (20 unidades, 100 contra 110).
        self.assertEqual(estancar["estado"], "pendente")
        self.assertEqual(estancar["abaixo"]["itens"], [{"produto": "Leite 1L", "codigo": "3", "perda": 10.0,
                                                        "preco_unitario": 5.0, "custo_unitario": 5.5}])
        self.assertEqual((estancar["abaixo"]["quantidade"], estancar["abaixo"]["perda"], estancar["abaixo"]["bloqueado"]),
                         (1, 10.0, False))
        # Etapa 2: o Café deixou 20,00 na mesa (saiu a 18,00 com tabela de 20,00).
        self.assertEqual(travar["estado"], "pendente")
        self.assertEqual(travar["itens"], [{"produto": "Cafe 500g", "codigo": "1", "vazamento": 20.0, "preco_min": 18.0,
                                            "preco_referencia": 20.0, "referencia_tabela": True}])
        # Etapa 3: todo produto teve custo em algum mês, mas o Açúcar saiu sem custo em janeiro (30,00): a meta é
        # conferir o custo, e o Leite aparece com estoque negativo em janeiro.
        self.assertEqual((controle["estado"], controle["meta_custo"], controle["meta_estoque"], controle["sem_custo"],
                          controle["estoque_negativo"], controle["venda_sem_custo"]),
                         ("pendente", "conferir", "negativo", 0, 1, 30.0))
        self.assertEqual(controle["orientacao"], "Cumpra as metas abaixo e refaça o Raio-X todo mês para acompanhar.")
        self.assertFalse(any(e["concluida_sozinha"] for e in (estancar, travar, controle)))

    def test_bronze_ve_a_contagem_e_nao_a_lista_da_auditoria(self):
        bronze = _plano(_diagnosticar(CUSTO_ABSURDO), BRONZE)
        estancar = bronze["plano"]["etapas"][0]
        self.assertEqual((estancar["abaixo"]["quantidade"], estancar["abaixo"]["itens"], estancar["abaixo"]["bloqueado"]),
                         (1, [], True))
        self.assertEqual((estancar["alertas"]["quantidade"], estancar["alertas"]["itens"], estancar["alertas"]["bloqueado"]),
                         (1, [], True))
        self.assertNotIn("Leite", json.dumps(bronze, ensure_ascii=False, default=str))
        # O prejuízo dos produtos no vermelho (indicador 14) é da auditoria: o Bronze não o vê.
        self.assertIsNone(bronze["prejuizo_produtos"])
        # Com a auditoria, os alertas de cadastro trazem o custo e o preço de cada um e quantas vezes um passa o outro.
        alerta = _plano(_diagnosticar(CUSTO_ABSURDO))["plano"]["etapas"][0]["alertas"]["itens"][0]
        self.assertEqual(alerta, {"produto": "Item Generico", "codigo": "9", "custo_unitario": 400.0,
                                  "preco_unitario": 5.0, "vezes": 80.0})

    def test_ate_cinco_produtos_por_lista_e_o_resto_contado(self):
        linhas = [f"{i};Produto {i};10;20,00;{150 - i},00;160,00" for i in range(1, 9)]
        estancar, travar, _ = _plano(_diagnosticar(relatorio_do_mes(2026, 1, linhas)))["plano"]["etapas"]
        self.assertEqual(len(estancar["abaixo"]["itens"]), 5)
        self.assertEqual((estancar["abaixo"]["quantidade"], estancar["abaixo"]["restantes"],
                          estancar["abaixo"]["alem_da_lista"]), (8, 3, False))
        # A maior perda primeiro (o produto 8 vendeu a 142,00 com custo de 160,00).
        self.assertEqual(estancar["abaixo"]["itens"][0]["produto"], "Produto 8")
        self.assertEqual(len(travar["itens"]), 5)
        self.assertEqual(travar["itens"][0]["produto"], "Produto 8")

    def test_alem_da_lista_da_auditoria(self):
        # 60 produtos abaixo do custo: a Auditoria lista 50; o "e mais" conta 45, e o total vem à parte.
        linhas = [f"{i};Produto {i};10;20,00;{150 - i % 7},00;160,00" for i in range(1, 61)]
        estancar = _plano(_diagnosticar(relatorio_do_mes(2026, 1, linhas)))["plano"]["etapas"][0]
        self.assertEqual((estancar["abaixo"]["quantidade"], estancar["abaixo"]["na_lista"], estancar["abaixo"]["restantes"],
                          estancar["abaixo"]["alem_da_lista"]), (60, raio_x.LIMITE_ITENS_LISTA, 45, True))

    def test_etapas_ja_regularizadas_saem_concluidas(self):
        pa = _plano(_diagnosticar(LIMPO))
        self.assertEqual([(e["estado"], e["concluida_sozinha"]) for e in pa["plano"]["etapas"]],
                         [("regularizada", True)] * 3)
        for etapa in pa["plano"]["etapas"]:
            with self.subTest(etapa=etapa["id"]):
                self.assertTrue(etapa["mensagem"].startswith("Etapa já regularizada"))
        # Nada a recuperar: os cenários são zero, sem divisão por zero.
        self.assertEqual(pa["plano"]["retorno"]["conservador"], 0.0)
        self.assertEqual(pa["plano"]["inacao"]["anual"], 0.0)

    def test_sem_coluna_de_custo_a_etapa_1_nao_se_avalia_e_pede_o_relatorio(self):
        estancar, travar, controle = _plano(_diagnosticar(SEM_CUSTO))["plano"]["etapas"]
        self.assertEqual((estancar["estado"], estancar["motivo"], estancar["concluida_sozinha"]),
                         ("sem_dados", "sem_coluna", False))
        self.assertIn("não trazem a coluna de custo", estancar["mensagem"])
        self.assertIn("exporte o relatório com a coluna de custo", estancar["mensagem"])
        self.assertEqual(travar["estado"], "pendente")
        # A Etapa 3 pede o relatório com a coluna (ou o cadastro, se o sistema não tem o custo), e o estoque.
        self.assertEqual((controle["estado"], controle["meta_custo"], controle["meta_estoque"]),
                         ("pendente", "sem_coluna", "sem_estoque"))

    def test_venda_com_custo_infima_nao_vira_sem_dados(self):
        # A venda com custo de 4,00 em 100.004,00: o motor grava 100,0% sem custo (4 casas), mas houve venda com
        # custo, e o Leite saiu abaixo dele: a etapa fica pendente, com o produto.
        conteudo = (_CAB.format("01/02/2026 a 28/02/2026") + "1;Cafe 500g;1000;100,00;100000,00;;5\n"
                    "2;Leite 1L;1;4,00;4,00;5,00;3\n").encode("utf-8")
        bruto = _diagnosticar(conteudo)
        self.assertEqual(bruto["saude"]["pct_faturamento_sem_custo"], 1.0)
        estancar = _plano(bruto)["plano"]["etapas"][0]
        self.assertEqual(estancar["estado"], "pendente")
        self.assertEqual([item["produto"] for item in estancar["abaixo"]["itens"]], ["Leite 1L"])

    def test_envio_misto_pede_os_relatorios_com_a_coluna_de_custo(self):
        # Janeiro sem a coluna de custo (o Açúcar só vendeu nele), fevereiro com ela.
        janeiro = (_CAB_SEM_CUSTO.format("01/01/2026 a 31/01/2026") + "1;Cafe 500g;10;20,00;200,00\n"
                   "2;Acucar 1kg;5;6,00;30,00\n").encode("utf-8")
        fevereiro = ("Relatorio de vendas por produto\nPeriodo: 01/02/2026 a 28/02/2026\n"
                     "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque\n"
                     "1;Cafe 500g;10;20,00;200,00;150,00;5\n").encode("utf-8")
        bruto = _diagnosticar(janeiro, fevereiro)
        self.assertEqual(plano_acao.arquivos_sem_coluna_de_custo(bruto["avisos"]), [1])
        self.assertEqual(plano_acao.relatorios_sem_coluna_de_custo(bruto["avisos"]), 1)
        controle = _plano(bruto)["plano"]["etapas"][2]
        # A linha a mais pede o arquivo 1 com a coluna; nos relatórios que a trazem, todo produto teve custo.
        self.assertEqual((controle["estado"], controle["meta_custo"], controle["arquivos_sem_coluna"],
                          controle["relatorios_sem_coluna"], controle["venda_sem_custo_nos_com_coluna"]),
                         ("pendente", "ok_nos_com_coluna", [1], 1, 0.0))

    def test_envio_misto_com_produto_sem_custo_no_relatorio_que_traz_a_coluna(self):
        # O Leite saiu sem custo em fevereiro, que traz a coluna: o custo falta no sistema, e não na exportação. A
        # Etapa 3 pede as duas coisas: o arquivo 1 com a coluna e o custo das vendas de fevereiro.
        janeiro = (_CAB_SEM_CUSTO.format("01/01/2026 a 31/01/2026") + "1;Cafe 500g;10;20,00;200,00\n"
                   "2;Acucar 1kg;5;6,00;30,00\n").encode("utf-8")
        fevereiro = (_CAB.format("01/02/2026 a 28/02/2026") + "1;Cafe 500g;10;20,00;200,00;150,00;5\n"
                     "3;Leite 1L;20;5,00;100,00;;4\n").encode("utf-8")
        estancar, _, controle = _plano(_diagnosticar(janeiro, fevereiro))["plano"]["etapas"]
        self.assertEqual((estancar["estado"], estancar["motivo"]), ("regularizada", "parcial"))
        self.assertEqual((controle["estado"], controle["meta_custo"], controle["arquivos_sem_coluna"],
                          controle["venda_sem_custo_nos_com_coluna"]),
                         ("pendente", "cadastrar_nos_com_coluna", [1], 100.0))
        # Com a coluna de fevereiro toda vazia, a Etapa 1 manda cadastrar o custo (Etapa 3), e a Etapa 3 o pede.
        vazio = (_CAB.format("01/02/2026 a 28/02/2026") + "1;Cafe 500g;10;20,00;200,00;;5\n"
                 "3;Leite 1L;20;5,00;100,00;;4\n").encode("utf-8")
        estancar, _, controle = _plano(_diagnosticar(janeiro, vazio))["plano"]["etapas"]
        self.assertEqual((estancar["estado"], estancar["motivo"]), ("sem_dados", "sem_venda_com_custo"))
        self.assertEqual((controle["meta_custo"], controle["arquivos_sem_coluna"],
                          controle["venda_sem_custo_nos_com_coluna"]), ("cadastrar_nos_com_coluna", [1], 300.0))
        # Sem saber quais relatórios trazem a coluna (resultado sem os arquivos), vale a regra de todo o envio.
        bruto = _diagnosticar(janeiro, fevereiro)
        del bruto["arquivos"]
        controle = _plano(bruto)["plano"]["etapas"][2]
        self.assertEqual((controle["meta_custo"], controle["arquivos_sem_coluna"], controle["sem_custo"]),
                         ("cadastrar", [1], 2))

    def test_coluna_de_custo_vazia_nao_e_etapa_regularizada(self):
        estancar, _, controle = _plano(_diagnosticar(COLUNA_VAZIA))["plano"]["etapas"]
        self.assertEqual((estancar["estado"], estancar["motivo"], estancar["concluida_sozinha"]),
                         ("sem_dados", "sem_venda_com_custo", False))
        self.assertIn("Nenhuma venda dos relatórios veio com custo informado", estancar["mensagem"])
        self.assertEqual((controle["estado"], controle["meta_custo"], controle["sem_custo"]), ("pendente", "cadastrar", 3))

    def test_parte_da_venda_sem_custo_a_etapa_1_diz_que_vale_so_para_as_vendas_com_custo(self):
        conteudo = (_CAB.format("01/03/2026 a 31/03/2026") + "1;Cafe 500g;10;20,00;200,00;150,00;5\n"
                    "2;Acucar 1kg;5;6,00;30,00;;3\n").encode("utf-8")
        estancar, _, controle = _plano(_diagnosticar(conteudo))["plano"]["etapas"]
        self.assertEqual((estancar["estado"], estancar["motivo"]), ("regularizada", "parcial"))
        self.assertIn("nas vendas com custo informado", estancar["mensagem"])
        self.assertEqual((controle["estado"], controle["meta_custo"]), ("pendente", "cadastrar"))

    def test_relatorio_sem_variacao_visivel_nao_e_etapa_regularizada(self):
        bruto = _diagnosticar(LIMPO)
        bruto["scanner"]["variacao_visivel"] = False
        travar = _plano(bruto)["plano"]["etapas"][1]
        self.assertEqual((travar["estado"], travar["concluida_sozinha"]), ("sem_dados", False))
        self.assertIn("não mostram o preço de cada venda", travar["mensagem"])

    def test_sem_estoque_conhecido_a_etapa_3_nao_se_da_por_cumprida(self):
        bruto = _diagnosticar(SEM_ESTOQUE)
        controle = _plano(bruto)["plano"]["etapas"][2]
        self.assertEqual((controle["estado"], controle["meta_custo"], controle["meta_estoque"]), ("pendente", "ok", "sem_estoque"))
        # Raio-X anterior aos indicadores: não se sabe se havia estoque, então a etapa também fica pendente.
        bruto.pop("indicadores_executivos")
        controle = _plano(bruto)["plano"]["etapas"][2]
        self.assertEqual((controle["estado"], controle["meta_estoque"]), ("pendente", "desconhecido"))

    def test_sem_datas_nao_ha_comparativo_mas_ha_roteiro(self):
        pa = _plano(_diagnosticar(SEM_DATAS))
        self.assertIsNone(pa["plano"]["inacao"])
        self.assertIsNone(pa["plano"]["retorno"])
        self.assertIsNone(pa["recuperavel"])
        self.assertIn("não trazem as datas do período", pa["sem_projecao"])
        self.assertEqual(pa["janela_prejuizo"], "Relatórios sem as datas do período: sem média por mês")
        self.assertEqual(pa["plano"]["etapas"][0]["abaixo"]["itens"][0]["produto"], "Cafe 500g")

    def test_meses_sem_coluna_de_custo_nao_entram_na_media_da_auditoria(self):
        # Janeiro a março sem a coluna de custo; abril com ela e o Leite vendido a 100,00 com custo de 160,00.
        meses = [(_CAB_SEM_CUSTO.format(f"01/{mes:02d}/2026 a {fim}/{mes:02d}/2026") + "1;Cafe 500g;10;20,00;200,00\n"
                  ).encode("utf-8") for mes, fim in ((1, 31), (2, 28), (3, 31))]
        abril = ("Relatorio de vendas por produto\nPeriodo: 01/04/2026 a 30/04/2026\n"
                 "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n1;Cafe 500g;10;20,00;200,00;150,00\n"
                 "2;Leite 1L;20;5,00;100,00;160,00\n").encode("utf-8")
        pa = _plano(_diagnosticar(*meses, abril))
        self.assertEqual(pa["horizonte"]["meses"], 4)
        self.assertEqual(pa["horizonte_auditado"]["meses"], 1)
        self.assertEqual(pa["prejuizo"]["mensal"], 60.0)
        self.assertEqual(pa["janela_prejuizo"], "No 1 mês auditado de 4 meses enviados (média de R$ 60,00/mês)")

    def test_relatorio_do_ano_inteiro_vale_ate_a_data_do_raio_x(self):
        conteudo = (_CAB.format("01/01/2026 a 31/12/2026") + "1;Cafe 500g;10;20,00;180,00;150,00;5\n").encode("utf-8")
        bruto = _diagnosticar(conteudo)
        pa = _plano(bruto, ate=date(2026, 11, 14))
        self.assertEqual(pa["horizonte"]["fim"], date(2026, 11, 14))
        # 20,00 recuperáveis em 10,47 meses: 1,91 por mês (com o ano inteiro, seriam 1,67).
        self.assertEqual(pa["recuperavel"]["mensal"], 1.91)

    def test_raio_x_anterior_ao_campo_novo(self):
        bruto = json.loads(json.dumps(self.dois_meses))
        del bruto["scanner"]["faturamento_variacao_ou_sem_custo"]
        retorno = _plano(bruto)["plano"]["retorno"]
        # O otimizado fica sem a parcela de margem (e a tela pede para refazer o Raio-X), sem inventar a base.
        self.assertEqual((retorno["otimizado"], retorno["ganho_margem"], retorno["base_anual"], retorno["sem_base"]),
                         (180.0, None, None, True))

    def test_raio_x_anterior_aos_indicadores(self):
        bruto = json.loads(json.dumps(self.dois_meses))
        bruto.pop("indicadores_executivos")
        pa = _plano(bruto)
        self.assertIsNone(pa["prejuizo_produtos"])
        self.assertEqual(pa["janela_produtos"], "Nos 2 meses auditados")
        # O Leite com estoque negativo é conhecido mesmo sem os indicadores.
        self.assertEqual(pa["plano"]["etapas"][2]["meta_estoque"], "negativo")

    def test_valores_absurdos_que_o_motor_aceita_nao_derrubam_o_plano(self):
        cabecalho = "Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 31/03/2026\nCodigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n"
        casos = {
            "custo de 30 dígitos": "1;Cafe 500g;10;20,00;200,00;150,00\n2;Leite 1L;2;5,00;10,00;100000000000000000000000000000,00\n",
            "custo 1E+100": "1;Cafe 500g;10;20,00;200,00;150,00\n2;Leite 1L;2;5,00;10,00;1E+100\n",
            "venda sem custo de 1e25": "1;Cafe 500g;10;20,00;200,00;150,00\n2;Item X;1;10000000000000000000000000;10000000000000000000000000;\n",
        }
        for nome, linhas in casos.items():
            for liberadas in (PRATA, BRONZE):
                with self.subTest(caso=nome, prata=liberadas is PRATA):
                    pa = _plano(_diagnosticar((cabecalho + linhas).encode("utf-8")), liberadas)
                    self.assertTrue(all(math.isfinite(numero) for numero in _numeros(pa)))

    def test_nenhum_numero_e_nan_nem_infinito(self):
        for nome, bruto in (("dois_meses", self.dois_meses), ("limpo", _diagnosticar(LIMPO)),
                            ("sem_custo", _diagnosticar(SEM_CUSTO)), ("sem_datas", _diagnosticar(SEM_DATAS)),
                            ("absurdo", _diagnosticar(CUSTO_ABSURDO)), ("coluna_vazia", _diagnosticar(COLUNA_VAZIA))):
            for liberadas in (PRATA, BRONZE):
                with self.subTest(caso=nome, prata=liberadas is PRATA):
                    pa = _plano(bruto, liberadas)
                    self.assertTrue(all(math.isfinite(numero) for numero in _numeros(pa)))

    def test_resultado_vazio_ou_estranho_nao_quebra(self):
        for resultado in ({}, {"saude": None, "scanner": "x", "auditoria": [], "recuperavel": {"total": "abc"}}):
            with self.subTest(resultado=resultado):
                pa = plano_acao.preparar(resultado, {})
                self.assertIsNone(pa["plano"]["inacao"])
                self.assertEqual(len(pa["plano"]["etapas"]), 3)


# =====================================================================
# As telas
# =====================================================================
class TelasPlanoDeAcaoTests(planos._PlanosCase):
    def _html(self, caminho):
        resposta = self.cliente_http.get(caminho)
        self.assertEqual(resposta.status_code, 200)
        return resposta.get_data(as_text=True)

    @staticmethod
    def _modal(html):
        return html.split('id="planoAcao"', 1)[1]

    @staticmethod
    def _card(html):
        return html.split('class="recomendacao-valor"', 1)[1].split('class="recomendacao-corpo"', 1)[0]

    def _editar_raio_x(self, mudar):
        linha = self._linha()
        dados = json.loads(linha.resultado)
        mudar(dados)
        linha.resultado = json.dumps(dados, ensure_ascii=False)
        self.db.session.commit()

    def test_sem_raio_x_nao_ha_botao(self):
        self._mudar_plano("PRATA")
        for caminho in (PAGINA, AUDITORIA):
            with self.subTest(caminho=caminho):
                html = self._html(caminho)
                self.assertNotIn("Ver Plano de Ação", html)
                self.assertNotIn('id="planoAcao"', html)

    def test_botao_e_modal_nas_duas_paginas(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        for caminho in (PAGINA, AUDITORIA):
            with self.subTest(caminho=caminho):
                html = self._html(caminho)
                cabeca = html.split('<div class="page-head-actions">', 1)[1].split("</div>", 1)[0]
                self.assertIn('<span aria-hidden="true">🎯</span> Ver Plano de Ação (12 Meses)</button>', cabeca)
                self.assertIn('data-bs-target="#planoAcao"', cabeca)
                self.assertIn(f'data-plano-chave="nexo.planoAcao.{self.empresa_id}"', html)
                self.assertIn("/static/js/plano-acao.js", html)
                modal = self._modal(html)
                self.assertIn("Imprimir / Salvar PDF", modal)
                self.assertIn('data-bs-dismiss="modal"', modal)
                for texto in ("vendas: 2 meses (01/01/2026 a 28/02/2026)", "Custo da Inação (12 meses)",
                              "R$</small> 180,00", "R$ 15,00 por mês, no ritmo do período analisado (2 meses)",
                              "sai sem custo informado (4,6% das vendas)",
                              "Retorno Projetado (12 meses)", "Cenário Conservador", "R$ 126,00", "Cenário Otimizado",
                              "R$ 216,90", "70% da sangria anualizada recuperada",
                              "Esse valor vai direto para o lucro líquido da empresa, pois as despesas fixas da loja "
                              "já estão pagas.",
                              "Etapa 1 <span class=\"plano-acao-prazo\">(Semana 1 — Estancar prejuízo imediato)",
                              "Etapa 2 <span class=\"plano-acao-prazo\">(Semanas 2 e 3 — Travar descontos no balcão)",
                              "Etapa 3 <span class=\"plano-acao-prazo\">(Mês 1 em diante — Controle e previsibilidade)",
                              "Leite 1L", "Cafe 500g", "Conferir o custo dos produtos com venda sem custo informado",
                              "0 de 3 etapas concluídas", "Concluída"):
                    self.assertIn(texto, modal)
                self.assertNotIn("custo cadastrado (", modal)
                self.assertEqual(len(re.findall(r'<input class="form-check-input" type="checkbox" id="planoEtapa\d"',
                                                modal)), 3)
                # Cada caixa leva a etapa no nome acessível; a orientação da Etapa 3 não contradiz a meta.
                for numero in (1, 2, 3):
                    self.assertIn(f'<span class="visually-hidden">Etapa {numero}: </span>Concluída</label>', modal)
                self.assertNotIn("Cadastre o custo dos produtos vendidos sem custo", modal)
                self.assertIn("meses analisados (no mínimo 1)", modal)

    def test_card_do_dinheiro_recuperavel_com_o_periodo_e_o_ritmo(self):
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        card = self._card(self._html(PAGINA))
        self.assertIn("Acumulado no período analisado: 2 meses (01/01/2026 a 28/02/2026)", card)
        self.assertIn("Ritmo de R$ 15,00/mês • Projeção de R$ 180,00/ano se nada for corrigido", card)
        self.assertNotIn("Amostra parcial", card)

    def test_selo_de_amostra_parcial_no_card_e_no_plano(self):
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        self._editar_raio_x(lambda dados: dados["avisos"].append(_aviso_de_amostra(12, 100)))
        html = self._html(PAGINA)
        self.assertIn("Amostra parcial (12% dos itens ativos) — o potencial da loja completa tende a ser maior",
                      self._card(html))
        self.assertIn("Amostra parcial (12% dos itens ativos)", self._modal(html))

    def test_selo_de_amostra_no_plano_tambem_sem_projecao(self):
        self._enviar_raio_x(("jan.csv", JANEIRO), ("sem-datas.csv", SEM_DATAS))
        self._editar_raio_x(lambda dados: dados["avisos"].append(_aviso_de_amostra(12, 100)))
        modal = self._modal(self._html(PAGINA))
        self.assertIn("não trazem as datas do período", modal)
        self.assertIn("Amostra parcial (12% dos itens ativos)", modal)

    def test_janela_de_tempo_dos_cards_da_auditoria(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        html = self._html(AUDITORIA)
        prejuizo = html.split("Prejuízo direto no caixa</span>", 1)[1].split('<div class="col-12', 1)[0]
        self.assertIn("Nos 2 meses auditados (média de R$ 5,00/mês)", prejuizo)
        produtos = html.split("Produtos dando prejuízo</span>", 1)[1].split("</div></div>", 1)[0]
        self.assertIn("Nos 2 meses auditados (média de R$ 0,00/mês)", produtos)

    def test_janela_sem_datas(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("sem-datas.csv", SEM_DATAS))
        html = self._html(AUDITORIA)
        self.assertIn("Relatórios sem as datas do período: sem média por mês", html)
        modal = self._modal(html)
        self.assertIn("não trazem as datas do período", modal)
        self.assertNotIn("Custo da Inação (12 meses)", modal)
        card = self._card(self._html(PAGINA))
        self.assertIn("Período analisado: os relatórios não trazem as datas", card)
        self.assertIn("Sem ritmo por mês", card)

    def test_lacuna_e_relatorio_sem_datas_no_card_e_no_subtitulo(self):
        # O relógio dos testes está em 02/03/2026: outubro de 2025 e janeiro de 2026, sem novembro e dezembro.
        outubro = relatorio_do_mes(2025, 10, ["1;Cafe 500g;10;20,00;200,00;150,00"])
        self._enviar_raio_x(("out.csv", outubro), ("jan.csv", JANEIRO))
        html = self._html(PAGINA)
        frase = "2 meses (01/10/2025 a 31/01/2026), sem relatório em 2 meses desse intervalo"
        self.assertIn(f"Acumulado no período analisado: {frase}", self._card(html))
        self.assertIn(f"vendas: {frase}", self._modal(html))
        self._enviar_raio_x(("jan.csv", JANEIRO), ("sem-datas.csv", SEM_DATAS))
        self.assertIn("Acumulado no período analisado: 1 mês (01/01/2026 a 31/01/2026), mais 1 relatório sem datas",
                      self._card(self._html(PAGINA)))

    def test_periodo_depois_da_data_do_raio_x_no_card_e_no_plano(self):
        # O relógio dos testes está em 02/03/2026: um relatório de abril (cabeçalho errado) com venda.
        abril = relatorio_do_mes(2026, 4, ["1;Cafe 500g;10;20,00;180,00;150,00"])
        self._enviar_raio_x(("abr.csv", abril))
        html = self._html(PAGINA)
        card = self._card(html)
        self.assertIn("Período analisado: os relatórios trazem um período depois da data do Raio-X", card)
        self.assertIn("Sem ritmo por mês: um ou mais relatórios trazem um período depois da data do Raio-X.", card)
        self.assertIn("trazem um período depois da data do Raio-X", self._modal(html))
        self.assertNotIn("não trazem as datas", card)

    def test_envio_misto_mostra_o_arquivo_sem_a_coluna_e_o_custo_que_falta_nos_outros(self):
        janeiro = (_CAB_SEM_CUSTO.format("01/01/2026 a 31/01/2026") + "1;Cafe 500g;10;20,00;200,00\n"
                   "2;Acucar 1kg;5;6,00;30,00\n").encode("utf-8")
        fevereiro = (_CAB.format("01/02/2026 a 28/02/2026") + "1;Cafe 500g;10;20,00;200,00;150,00;5\n"
                     "3;Leite 1L;20;5,00;100,00;;4\n").encode("utf-8")
        self._enviar_raio_x(("jan.csv", janeiro), ("fev.csv", fevereiro))
        modal = self._modal(self._html(PAGINA))
        etapa3 = modal.split('data-plano-etapa-cartao="3"', 1)[1].split('id="planoBloco3"', 1)[0]
        self.assertIn("O arquivo 1 (jan.csv) enviado veio sem a <strong>coluna de custo</strong>: exporte esse "
                      "relatório com ela e refaça o Raio-X", etapa3)
        self.assertIn("Cadastrar ou conferir o custo dos produtos vendidos sem custo nos relatórios que trazem a "
                      "coluna: <strong>R$ 100,00</strong> das vendas desses relatórios saíram sem custo informado.",
                      etapa3)
        # Dois arquivos sem a coluna: os números dos dois, no plural.
        self._enviar_raio_x(("jan.csv", janeiro), ("fev.csv", fevereiro),
                            ("dez.csv", (_CAB_SEM_CUSTO.format("01/12/2025 a 31/12/2025")
                                         + "1;Cafe 500g;10;20,00;200,00\n").encode("utf-8")))
        etapa3 = self._modal(self._html(PAGINA)).split('data-plano-etapa-cartao="3"', 1)[1]
        self.assertIn("Os arquivos 1 (jan.csv) e 3 (dez.csv) enviados vieram sem a <strong>coluna de custo</strong>: "
                      "exporte esses relatórios com ela", etapa3)
        # Na Auditoria (que não mostra os avisos) e no PDF, o nome diz qual arquivo reexportar: fevereiro (com a
        # coluna) enviado antes de janeiro (sem ela) deixa janeiro como o arquivo 2.
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("fev.csv", fevereiro), ("jan.csv", janeiro))
        etapa3 = self._modal(self._html(AUDITORIA)).split('data-plano-etapa-cartao="3"', 1)[1]
        self.assertIn("O arquivo 2 (jan.csv) enviado veio sem a <strong>coluna de custo</strong>", etapa3)

    def test_relatorio_sem_datas_e_outro_com_periodo_futuro_no_plano(self):
        abril = relatorio_do_mes(2026, 4, ["1;Cafe 500g;10;20,00;180,00;150,00"])
        self._enviar_raio_x(("jan.csv", JANEIRO), ("sem-datas.csv", SEM_DATAS), ("abr.csv", abril))
        html = self._html(PAGINA)
        self.assertIn("Sem ritmo por mês: há relatórios sem as datas do período e com período depois da data do "
                      "Raio-X.", self._card(html))
        modal = self._modal(html)
        self.assertIn("Um ou mais relatórios enviados não trazem as datas do período, e um ou mais trazem um período "
                      "depois da data do Raio-X", modal)
        self.assertNotIn("Alguns relatórios", modal)

    def test_bronze_ve_o_cadeado_no_lugar_dos_produtos(self):
        self._enviar_raio_x(("jan.csv", CUSTO_ABSURDO))
        modal = self._modal(self._html(PAGINA))
        etapa1 = modal.split('data-plano-etapa-cartao="1"', 1)[1].split('data-plano-etapa-cartao="2"', 1)[0]
        self.assertIn("A lista dos produtos aparece a partir do plano", etapa1)
        self.assertIn("A lista dos itens aparece a partir do plano", etapa1)
        self.assertNotIn("Leite 1L", etapa1)
        self.assertNotIn("Item Generico", etapa1)
        self.assertIn("1 produto</strong>, R$ 10,00 de prejuízo no período", etapa1)

    def test_etapas_regularizadas_saem_marcadas_e_travadas(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("mar.csv", LIMPO))
        modal = self._modal(self._html(AUDITORIA))
        self.assertEqual(len(re.findall(r'data-plano-etapa="\d" aria-describedby="[^"]+" checked disabled>', modal)), 3)
        self.assertIn("3 de 3 etapas concluídas", modal)
        self.assertEqual(modal.count("Etapa já regularizada"), 3)
        # Nada vendido sem custo: a frase diz isso, em vez de "R$ 0,00 ... no escuro".
        self.assertIn("<strong>No escuro:</strong> nenhuma venda saiu sem custo informado no período analisado.", modal)
        self.assertNotIn("R$ 0,00 do faturamento de 12 meses", modal)

    def test_links_das_listas_completas(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        # Na Auditoria, a lista das vendas com prejuízo está na própria página; a dos preços, no Raio-X.
        auditoria = self._modal(self._html(AUDITORIA))
        self.assertIn('href="#abaixoTitulo" data-plano-ir', auditoria)
        self.assertIn(f'href="{PAGINA}#scannerTitulo" data-plano-ir', auditoria)
        raio_x_html = self._modal(self._html(PAGINA))
        self.assertIn(f'href="{AUDITORIA}#abaixoTitulo" data-plano-ir', raio_x_html)
        # Os alertas de cadastro: o link aponta para o resumo do card recolhido (focável), na Auditoria.
        self._enviar_raio_x(("jan.csv", CUSTO_ABSURDO))
        self.assertIn('href="#alertasCadastroResumo" data-plano-ir', self._modal(self._html(AUDITORIA)))
        self.assertIn(f'href="{AUDITORIA}#alertasCadastroResumo" data-plano-ir', self._modal(self._html(PAGINA)))
        self.assertIn('<summary id="alertasCadastroResumo">', self._html(AUDITORIA))
        self.assertIn('href="#scannerTitulo" data-plano-ir', raio_x_html)

    def test_raio_x_anterior_ao_campo_novo_pede_para_refazer(self):
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        self._editar_raio_x(lambda dados: dados["scanner"].pop("faturamento_variacao_ou_sem_custo"))
        modal = self._modal(self._html(PAGINA))
        self.assertIn("refaça o Raio-X para somar ao cenário otimizado o ganho de 1,5% de margem", modal)
        self.assertIn("R$ 180,00</strong> <span>100% da sangria anualizada.</span>", modal)

    def test_valor_absurdo_nao_derruba_as_paginas_nem_as_apis(self):
        self._mudar_plano("PRATA")
        absurdo = ("Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 31/03/2026\n"
                   "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n1;Cafe 500g;10;20,00;200,00;150,00\n"
                   "2;Leite 1L;2;5,00;10,00;100000000000000000000000000000,00\n").encode("utf-8")
        self.assertEqual(self._enviar_raio_x(("jan.csv", absurdo)).status_code, 303)
        for caminho in (PAGINA, AUDITORIA, planos.API, planos.API_AUDITORIA):
            with self.subTest(caminho=caminho):
                self.assertEqual(self.cliente_http.get(caminho).status_code, 200)
        self.assertIn('id="planoAcao"', self._html(AUDITORIA))

    def test_plano_que_nao_monta_nao_derruba_a_pagina(self):
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        with mock.patch("plano_acao.preparar", side_effect=ArithmeticError("teste")):
            html = self._html(PAGINA)
        self.assertNotIn('id="planoAcao"', html)
        self.assertNotIn("Ver Plano de Ação", html)
        self.assertIn("recomendacao-valor", html)

    def test_impressao_e_tema_claro_na_folha(self):
        css = (planos.ROOT / "static" / "css" / "nexo.css").read_text(encoding="utf-8")
        impressao = css.split("@media print", 1)[1]
        self.assertIn("body.plano-acao-aberto > *:not(#planoAcao) { display: none !important; }", impressao)
        self.assertIn("plano-acao-nao-imprime { display: none !important; }", impressao)
        self.assertIn("body.plano-acao-aberto .nexobot { opacity: 0; visibility: hidden; pointer-events: none; }", css)
        claro = (planos.ROOT / "static" / "css" / "nexo-tema-claro.css").read_text(encoding="utf-8")
        self.assertIn('html[data-theme="light"] .plano-acao-card', claro)
        self.assertIn('html[data-theme="light"] .plano-acao .form-check-input:checked', claro)


if __name__ == "__main__":
    unittest.main()
