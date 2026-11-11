"""Inteligência de estoque e de preço da Avaliação de Cenário (Etapa 3, inteligencia_estoque).

  1. Parâmetros: os padrão, a gravação, a validação e os ajustes a partir do texto da consulta.
  2. Períodos cobertos e dias sem venda (piso): cobertura contínua, lacuna, relatório anual e a
     fronteira exata de cada faixa.
  3. Estoque parado por idade (60, 90 e 180 dias) e estoque excedente (cobertura-alvo).
  4. Simulador de liberação de caixa da Urgência: receita a preço médio, capital ao custo, cenários,
     desconto de equilíbrio e itens fora.
  5. Auditoria de preço: markup de referência da categoria (agregado x mediana), markup informado,
     tolerância, item sem referência e a parte já contada no potencial recuperável.
  6. Resumo gravado no lead (só números) e a seção "inteligencia" do payload do consultor (falha
     isolada, sem a coluna de estoque, várias lojas).
  7. Motor (lead-2026-11.4): a data da última venda, os períodos cobertos, o contrato do processo
     isolado e o relatório com várias lojas (soma do estoque, recusa da coluna de loja no lugar do
     produto, "Unidade" de medida fora da detecção, o Raio-X do cliente igual).
  8. Gravação, retriagem e a rota com os parâmetros do simulador (400 para parâmetro inválido).
  9. Memória: 20 mil produtos em dois anos, com o pico medido pelo tracemalloc.

Dados fictícios. As linhas seguem o formato de avaliacao_cenario.linha_da_staging.
"""

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import select

import avaliacao_motor
import diagnostico_cenario
from etl_inputs import ETLInputError
import etl_runtime
import inteligencia_estoque
from inteligencia_estoque import ParametrosInteligencia, PARAMETROS_PADRAO
import triagem_cenario
from triagem_cenario import Cobertura
from tests.deploy_support import DeployTestCase
from tests.raio_x_support import avaliar_como_o_worker, executar_como_o_worker
from tests.test_avaliacao_cenario import CONTATO, _relatorio
from tests.test_triagem_cenario import AGORA, _lead, _linha, _registros

D = Decimal


def linha(chave, ano, *, ultima=None, **campos):
    """Linha da staging com a data do fim do último relatório do ano com venda (padrão: 31/12)."""
    dados = _linha(chave, ano, **campos)
    dados["fim_ultimo_relatorio_com_venda"] = ultima if ultima is not None else date(ano, 12, 31)
    return dados


def triagem(blocos=None, a_confirmar=()):
    """O que a inteligência lê da triagem: os blocos e os itens de custo implausível."""
    return SimpleNamespace(blocos=dict(blocos or {}), a_confirmar=frozenset(a_confirmar))


def cobertura_de(dias, *, com_estoque=None, ambiguo=0):
    """Cobertura de um envio com {ano: dias} (12 relatórios mensais por ano)."""
    return Cobertura(relatorios={a: 12 for a in dias}, dias=dict(dias), mensais={a: True for a in dias},
                     com_estoque=com_estoque, cabecalho_ambiguo=ambiguo)


def meta_com(*pares):
    return {"periodos_cobertos": [[inicio, fim] for inicio, fim in pares]}


# =============================================================================
# 1. Parâmetros
# =============================================================================
class ParametrosTests(unittest.TestCase):
    def test_padrao_e_gravacao_so_com_texto_e_inteiros(self):
        self.assertEqual(PARAMETROS_PADRAO.faixas_dias, (60, 90, 180))
        self.assertEqual((PARAMETROS_PADRAO.cobertura_alvo_dias, PARAMETROS_PADRAO.fracao_liquidada,
                          PARAMETROS_PADRAO.markup_informado), (60, D("1"), None))
        gravado = PARAMETROS_PADRAO.para_dict()
        self.assertEqual(gravado["versao"], inteligencia_estoque.VERSAO_INTELIGENCIA)
        self.assertEqual(gravado["descontos"], ["0", "0.10", "0.20", "0.30"])
        self.assertEqual(gravado["faixas_dias"], [60, 90, 180])
        # Nada de float: o JSON devolve exatamente o que foi gravado.
        self.assertEqual(json.loads(json.dumps(gravado)), gravado)
        self.assertFalse(any(isinstance(v, float) for v in gravado.values()))

    def test_validacao_recusa_tipo_faixa_e_relacao(self):
        invalidos = {
            "faixas fora de ordem": {"faixas_dias": (90, 60)},
            "faixa repetida": {"faixas_dias": (60, 60)},
            "faixa zero": {"faixas_dias": (0, 60)},
            "seis faixas": {"faixas_dias": (10, 20, 30, 40, 50, 60)},
            "faixa booleana": {"faixas_dias": (True, 60)},
            "faixas em lista": {"faixas_dias": [60, 90]},
            "cobertura zero": {"cobertura_alvo_dias": 0},
            "cobertura em Decimal": {"cobertura_alvo_dias": D("60")},
            "desconto acima de 95%": {"descontos": (D("0.96"),)},
            "desconto negativo": {"descontos": (D("-0.1"),)},
            "desconto em float": {"descontos": (0.1,)},
            "sete descontos": {"descontos": tuple(D("0.1") for _ in range(7))},
            "nenhum desconto": {"descontos": ()},
            "fração zero": {"fracao_liquidada": D("0")},
            "fração acima de 1": {"fracao_liquidada": D("1.5")},
            "fração NaN": {"fracao_liquidada": D("NaN")},
            "markup acima de 10": {"markup_informado": D("10.5")},
            "markup infinito": {"markup_informado": D("Infinity")},
            "markup com 7 casas": {"markup_informado": D("0.1234567")},
            "tolerância acima de 20%": {"tolerancia_preco": D("0.25")},
            "itens listados zero": {"itens_listados": 0},
        }
        for caso, campos in invalidos.items():
            with self.subTest(caso=caso), self.assertRaises(ValueError):
                ParametrosInteligencia(**campos)

    def test_ajustes_a_partir_do_texto_da_consulta(self):
        ajustado = PARAMETROS_PADRAO.com_ajustes({"descontos": "0.1, 0.25", "fracao_liquidada": "0.5",
                                                  "markup_informado": "0.35", "cobertura_alvo_dias": "45"})
        self.assertEqual(ajustado.descontos, (D("0.1"), D("0.25")))
        self.assertEqual((ajustado.fracao_liquidada, ajustado.markup_informado, ajustado.cobertura_alvo_dias),
                         (D("0.5"), D("0.35"), 45))
        # O resto fica como estava.
        self.assertEqual(ajustado.faixas_dias, PARAMETROS_PADRAO.faixas_dias)
        invalidos = {
            "desconto com vírgula decimal": {"descontos": "0,5"},
            "notação científica": {"fracao_liquidada": "1e-1"},
            "texto": {"markup_informado": "abc"},
            "negativo": {"fracao_liquidada": "-0.5"},
            "dias negativos": {"cobertura_alvo_dias": "-1"},
            "dias com cinco dígitos": {"cobertura_alvo_dias": "10000"},
            "dígitos não ASCII": {"cobertura_alvo_dias": "١٢"},
            "dígito sobrescrito": {"cobertura_alvo_dias": "²"},
            "decimal não ASCII": {"markup_informado": "٠.٣"},
            "nome desconhecido": {"faixas": "60"},
            "NaN em texto": {"fracao_liquidada": "NaN"},
            "desconto acima de 95%": {"descontos": "0.99"},
        }
        for caso, ajustes in invalidos.items():
            with self.subTest(caso=caso), self.assertRaises(ValueError):
                PARAMETROS_PADRAO.com_ajustes(ajustes)
        with self.assertRaises(ValueError):
            PARAMETROS_PADRAO.com_ajustes(["descontos"])


# =============================================================================
# 2. Períodos cobertos e dias sem venda
# =============================================================================
class PeriodosTests(unittest.TestCase):
    def test_periodos_da_meta_em_ordem_e_sem_sobreposicao(self):
        periodos = inteligencia_estoque.periodos_da_meta(meta_com(("2025-01-01", "2025-06-30"),
                                                                  ("2025-09-01", "2025-10-31")))
        self.assertEqual(periodos, [(date(2025, 1, 1), date(2025, 6, 30)), (date(2025, 9, 1), date(2025, 10, 31))])
        invalidas = {
            "sem a chave": {},
            "lista vazia": {"periodos_cobertos": []},
            "texto": {"periodos_cobertos": "2025-01-01/2025-01-31"},
            "par incompleto": {"periodos_cobertos": [["2025-01-01"]]},
            "invertido": meta_com(("2025-02-01", "2025-01-01")),
            "sobreposto": meta_com(("2025-01-01", "2025-01-31"), ("2025-01-31", "2025-02-28")),
            "fora de ordem": meta_com(("2025-03-01", "2025-03-31"), ("2025-01-01", "2025-01-31")),
            "data inexistente": meta_com(("2025-02-01", "2025-02-30")),
        }
        for caso, meta in invalidas.items():
            with self.subTest(caso=caso), self.assertRaises((ValueError, TypeError)):
                inteligencia_estoque.periodos_da_meta(meta)

    def test_dias_cobertos_depois_da_ultima_venda(self):
        dias = inteligencia_estoque._dias_cobertos_depois
        continuo = [(date(2025, 1, 1), date(2025, 10, 31))]
        self.assertEqual(dias(date(2025, 8, 31), continuo), 61)
        self.assertEqual(dias(date(2025, 10, 31), continuo), 0)
        # Sem relatório de julho e agosto: esses meses não contam (a venda pode ter acontecido neles).
        com_lacuna = [(date(2025, 1, 1), date(2025, 6, 30)), (date(2025, 9, 1), date(2025, 10, 31))]
        self.assertEqual(dias(date(2025, 6, 30), com_lacuna), 61)
        self.assertEqual(dias(date(2025, 7, 15), com_lacuna), 61)
        self.assertEqual(dias(date(2024, 12, 31), com_lacuna), 181 + 61)

    def test_fronteiras_das_faixas(self):
        faixa = inteligencia_estoque._faixa
        faixas = (60, 90, 180)
        for dias_sem_venda, esperado in ((0, None), (59, None), (60, 0), (89, 0), (90, 1), (179, 1), (180, 2),
                                         (5000, 2)):
            with self.subTest(dias=dias_sem_venda):
                self.assertEqual(faixa(dias_sem_venda, faixas), esperado)
        self.assertEqual(inteligencia_estoque._rotulo_da_faixa(0, faixas, "dias sem venda"), "60 a 89 dias sem venda")
        self.assertEqual(inteligencia_estoque._rotulo_da_faixa(2, faixas, "dias sem venda"),
                         "180 dias sem venda ou mais")


# =============================================================================
# 3. Estoque parado por idade e estoque excedente
# =============================================================================
# Envio de janeiro a outubro de 2025 (304 dias). Custo médio de todos: 60 / 10 = 6.
COBERTURA_2025 = cobertura_de({2025: 304})
META_2025 = meta_com(("2025-01-01", "2025-10-31"))
ESTOQUE = [
    linha("A", 2025, ultima=date(2025, 8, 31), estoque_final="10"),    # 61 dias: 60 a 89
    linha("B", 2025, ultima=date(2025, 7, 31), estoque_final="5"),     # 92 dias: 90 a 179
    linha("C", 2025, ultima=date(2025, 4, 30), estoque_final="20"),    # 184 dias: 180 ou mais
    linha("D", 2025, ultima=date(2025, 9, 30), estoque_final="1"),     # 31 dias; 30,4 dias de estoque
    linha("E", 2025, ultima=date(2025, 10, 31), estoque_final="100"),  # vende; 3.040 dias de estoque
    linha("F", 2025, ultima=date(2025, 1, 31)),                        # sem estoque informado
    linha("G", 2025, ultima=date(2025, 1, 31), estoque_final="0"),     # sem estoque
    linha("H", 2025, ultima=date(2025, 1, 31), com_custo="0.00", estoque_final="3"),  # sem custo
    linha("X", 2025, ultima=date(2025, 1, 31), estoque_final="50"),    # custo implausível (a confirmar)
]


class EstoqueParadoTests(unittest.TestCase):
    def _calcular(self, linhas=ESTOQUE, meta=META_2025, base=COBERTURA_2025, parametros=PARAMETROS_PADRAO):
        return inteligencia_estoque.estoque_parado_por_idade(linhas, base, triagem(a_confirmar={"X"}), meta, parametros)

    def test_faixas_acumulados_e_capital(self):
        resultado = self._calcular()
        self.assertEqual(resultado["fim_da_cobertura"], "2025-10-31")
        # Capital em estoque: A 60 + B 30 + C 120 + D 6 + E 600 (o item a confirmar fica fora de tudo).
        self.assertEqual(resultado["capital_em_estoque"], 816.0)
        parado = resultado["parado"]
        self.assertEqual([(f["faixa"], f["itens"], f["capital"]) for f in parado["faixas"]],
                         [("60 a 89 dias sem venda", 1, 60.0), ("90 a 179 dias sem venda", 1, 30.0),
                          ("180 dias sem venda ou mais", 1, 120.0)])
        self.assertEqual([(f["dias_minimos"], f["dias_maximos"]) for f in parado["faixas"]],
                         [(60, 89), (90, 179), (180, None)])
        # "Há mais de N dias": cada faixa acumula as seguintes.
        self.assertEqual(parado["acima_de"], {"60": {"itens": 3, "capital": 210.0}, "90": {"itens": 2, "capital": 150.0},
                                              "180": {"itens": 1, "capital": 120.0}})
        self.assertEqual((parado["itens"], parado["capital"], parado["participacao_no_estoque"]), (3, 210.0, 0.2574))
        self.assertEqual([(i["produto"], i["dias_sem_venda"], i["capital"], i["estoque"], i["custo_medio"])
                          for i in parado["lista"]],
                         [("Produto C", 184, 120.0, 20.0, 6.0), ("Produto A", 61, 60.0, 10.0, 6.0),
                          ("Produto B", 92, 30.0, 5.0, 6.0)])
        self.assertEqual((parado["itens_com_lacuna"], resultado["itens_sem_custo"],
                          resultado["itens_sem_estoque_informado"], resultado["itens_sem_estoque_com_custo"],
                          resultado["itens_parados_sem_estoque_informado"]), (0, 1, 1, 1, 1))

    def test_sem_estoque_informado_ja_parado_so_a_partir_da_primeira_faixa(self):
        # Sem saldo: conta como sem estoque informado; com custo, também como sem estoque com custo (as somas de
        # estoque ficam parciais) e, a partir de 60 dias cobertos sem venda, como parado sem estoque (com saldo,
        # entraria na soma do parado). Quem ainda vende, a data ausente, o item sem custo (fora com saldo ou
        # sem) e o item a confirmar não contam como parados.
        sem_data = linha("SD", 2025)
        sem_data["fim_ultimo_relatorio_com_venda"] = None
        resultado = self._calcular([
            linha("S60", 2025, ultima=date(2025, 9, 1)),   # 60 dias cobertos: conta
            linha("S59", 2025, ultima=date(2025, 9, 2)),   # 59 dias: ainda não
            linha("SV", 2025, ultima=date(2025, 10, 31)),  # vendeu no último dia
            linha("SC", 2025, ultima=date(2025, 1, 31), com_custo="0.00"),  # sem custo
            linha("X", 2025, ultima=date(2025, 1, 31)),    # a confirmar: fora de tudo
            linha("C", 2025, ultima=date(2025, 4, 30), estoque_final="20"), sem_data])
        self.assertEqual((resultado["itens_sem_estoque_informado"], resultado["itens_sem_estoque_com_custo"],
                          resultado["itens_parados_sem_estoque_informado"]), (5, 4, 1))
        self.assertEqual(resultado["parado"]["acima_de"]["60"], {"itens": 1, "capital": 120.0})

    def test_excedente_acima_da_cobertura_alvo(self):
        excedente = self._calcular()["excedente"]
        # E: ritmo 10 / 304 por dia; 100 em estoque são 3.040 dias; acima de 60 dias: 100 - 600/304.
        esperado = float(((D(100) - D(10) / D(304) * 60) * 6).quantize(D("0.01")))
        self.assertEqual((excedente["itens"], excedente["capital_liberavel"]), (1, esperado))
        self.assertEqual(esperado, 588.16)
        [item] = excedente["lista"]
        self.assertEqual((item["produto"], item["dias_de_estoque"], item["capital_em_estoque"]), ("Produto E", 3040, 600.0))
        self.assertEqual([f["itens"] for f in excedente["faixas"]], [0, 0, 1])
        # Com cobertura-alvo de 20 dias, o D (30,4 dias de estoque) também sobra.
        ajustado = self._calcular(parametros=PARAMETROS_PADRAO.com_ajustes({"cobertura_alvo_dias": "20"}))["excedente"]
        self.assertEqual(ajustado["itens"], 2)
        self.assertEqual(ajustado["cobertura_alvo_dias"], 20)

    def test_lacuna_entre_relatorios_e_piso_dos_dias(self):
        meta = meta_com(("2025-01-01", "2025-06-30"), ("2025-09-01", "2025-10-31"))
        base = cobertura_de({2025: 181 + 61})
        resultado = self._calcular([linha("L", 2025, ultima=date(2025, 6, 30), estoque_final="10")], meta, base)
        [item] = resultado["parado"]["lista"]
        # 123 dias corridos, mas só 61 cobertos por relatório: o piso é 61, com a marca de lacuna.
        self.assertEqual((item["dias_sem_venda"], item["com_lacuna"]), (61, True))
        self.assertEqual(resultado["parado"]["itens_com_lacuna"], 1)

    def test_relatorio_anual_e_venda_so_no_ano_anterior(self):
        # Relatório anual: quem vendeu no ano tem piso 0 (a venda pode ter sido no último dia).
        anual = self._calcular([linha("Y", 2025, ultima=date(2025, 10, 31), estoque_final="10")])
        self.assertEqual(anual["parado"]["itens"], 0)
        # Vendeu só em 2024; 2025 tem relatórios de janeiro a junho: 181 dias sem venda.
        meta = meta_com(("2024-01-01", "2024-12-31"), ("2025-01-01", "2025-06-30"))
        base = cobertura_de({2024: 366, 2025: 181})
        resultado = self._calcular([linha("V", 2024, ultima=date(2024, 12, 31), estoque_final="4")], meta, base)
        [item] = resultado["parado"]["lista"]
        self.assertEqual((item["dias_sem_venda"], item["faixa"], item["capital"]), (181, "180 dias sem venda ou mais", 24.0))

    def test_fronteira_exata_de_60_dias(self):
        meta = meta_com(("2025-01-01", "2025-03-02"))
        base = cobertura_de({2025: 61})
        linhas = [linha("S", 2025, ultima=date(2025, 1, 1), estoque_final="1"),
                  linha("T", 2025, ultima=date(2025, 1, 2), estoque_final="1")]
        resultado = self._calcular(linhas, meta, base)
        self.assertEqual([(i["produto"], i["dias_sem_venda"]) for i in resultado["parado"]["lista"]], [("Produto S", 60)])

    def test_faixas_configuraveis(self):
        parametros = ParametrosInteligencia(faixas_dias=(30, 60))
        parado = self._calcular(parametros=parametros)["parado"]
        self.assertEqual([(f["faixa"], f["itens"]) for f in parado["faixas"]],
                         [("30 a 59 dias sem venda", 1), ("60 dias sem venda ou mais", 3)])
        self.assertEqual(set(parado["acima_de"]), {"30", "60"})

    def test_a_ordem_das_linhas_nao_muda_o_resultado(self):
        self.assertEqual(self._calcular(list(reversed(ESTOQUE))), self._calcular())


# =============================================================================
# 4. Simulador de liberação de caixa (Urgência)
# =============================================================================
def _sem_preco():
    dados = linha("U6", 2025, estoque_final="2")
    dados["valor_venda_bruto"] = D("0")
    return dados


SIMULACAO = [
    # U1: o preço médio é o do ano mais recente com venda (20, não 30).
    linha("U1", 2024, venda="300.00", custo="120.00"),
    linha("U1", 2025, venda="200.00", custo="120.00", estoque_final="5"),
    linha("U2", 2025, quantidade="4", venda="100.00", custo="60.00", estoque_final="2"),
    linha("U3", 2025),                                            # sem estoque
    linha("U4", 2025, com_custo="0.00", estoque_final="3"),       # sem custo
    linha("U5", 2025, estoque_final="9"),                         # a confirmar
    _sem_preco(),                                                 # sem preço
    linha("A1", 2025, estoque_final="9"),                         # Atenção: fora do simulador
]
BLOCOS_SIMULACAO = {chave: "urgencia" for chave in ("U1", "U2", "U3", "U4", "U5", "U6")} | {"A1": "atencao"}


class SimuladorTests(unittest.TestCase):
    def _simular(self, linhas=SIMULACAO, parametros=PARAMETROS_PADRAO, blocos=BLOCOS_SIMULACAO):
        return inteligencia_estoque.simular_liquidacao(linhas, cobertura_de({2024: 366, 2025: 365}),
                                                       triagem(blocos, a_confirmar={"U5"}), parametros)

    def test_receita_capital_cenarios_e_equilibrio(self):
        resultado = self._simular()
        self.assertEqual((resultado["escopo"], resultado["itens"]), ("urgencia", 2))
        self.assertEqual(resultado["itens_fora"], {"a_confirmar": 1, "sem_estoque_informado": 1, "estoque_zerado": 0,
                                                   "sem_custo": 1, "sem_preco": 1})
        # Saldo zerado (ou negativo) não é o mesmo que saldo não informado.
        zerado = self._simular([linha("Z", 2025, estoque_final="0"), linha("N", 2025, estoque_final="-2")],
                               blocos={"Z": "urgencia", "N": "urgencia"})
        self.assertEqual((zerado["itens_fora"]["estoque_zerado"], zerado["itens_fora"]["sem_estoque_informado"]), (2, 0))
        # U1: 5 x 20 e 5 x 12; U2: 2 x 25 e 2 x 15.
        self.assertEqual((resultado["receita_a_preco_medio"], resultado["capital_ao_custo"]), (150.0, 90.0))
        self.assertEqual(resultado["desconto_de_equilibrio"], 0.4)
        self.assertEqual([(c["desconto"], c["fracao_liquidada"], c["caixa"], c["resultado_contra_o_custo"])
                          for c in resultado["cenarios"]],
                         [(0.0, 1.0, 150.0, 60.0), (0.1, 1.0, 135.0, 45.0), (0.2, 1.0, 120.0, 30.0),
                          (0.3, 1.0, 105.0, 15.0)])
        self.assertEqual([(i["produto"], i["preco_medio"], i["custo_medio"], i["receita_a_preco_medio"])
                          for i in resultado["lista"]],
                         [("Produto U1", 20.0, 12.0, 100.0), ("Produto U2", 25.0, 15.0, 50.0)])

    def test_fracao_liquidada_e_desconto_configuraveis(self):
        parametros = PARAMETROS_PADRAO.com_ajustes({"descontos": "0.5", "fracao_liquidada": "0.5"})
        [cenario] = self._simular(parametros=parametros)["cenarios"]
        # Metade do estoque com 50% de desconto: 150 x 0,5 x 0,5 = 37,50; contra 90 x 0,5 de custo.
        self.assertEqual((cenario["caixa"], cenario["resultado_contra_o_custo"]), (37.5, -7.5))

    def test_item_vendido_abaixo_do_custo_e_simulador_vazio(self):
        import math
        abaixo = [linha("W", 2025, venda="50.00", custo="100.00", estoque_final="1")]
        resultado = self._simular(abaixo, blocos={"W": "urgencia"})
        # Equilíbrio negativo: nem sem desconto o estoque paga o custo.
        self.assertEqual((resultado["desconto_de_equilibrio"], resultado["abaixo_do_custo_sem_desconto"]), (-1.0, True))
        # Custo um centésimo de milésimo acima do preço: o arredondado é zero (nunca -0.0), e a marca diz o sentido.
        quase = self._simular([linha("Q", 2025, venda="100000.00", custo="100001.00", estoque_final="1")],
                              blocos={"Q": "urgencia"})
        self.assertEqual(quase["desconto_de_equilibrio"], 0.0)
        self.assertEqual(math.copysign(1, quase["desconto_de_equilibrio"]), 1.0)
        self.assertTrue(quase["abaixo_do_custo_sem_desconto"])
        self.assertFalse(self._simular()["abaixo_do_custo_sem_desconto"])
        vazio = self._simular([], blocos={})
        self.assertEqual((vazio["itens"], vazio["receita_a_preco_medio"], vazio["desconto_de_equilibrio"]), (0, 0.0, None))


# =============================================================================
# 5. Auditoria de preço
# =============================================================================
AUDITORIA = [
    # BEBIDAS, markups 0,5, 0,6, 0,4 e 0,1: agregado 560 / 400 - 1 = 0,4; mediana 0,45; referência 0,4.
    linha("B1", 2025, venda="150.00", custo="100.00", categoria="BEBIDAS"),
    linha("B2", 2025, venda="160.00", custo="100.00", categoria="Bebidas"),
    linha("B3", 2025, venda="140.00", custo="100.00", categoria="BEBIDAS"),
    linha("B4", 2025, venda="110.00", custo="100.00", categoria="BEBIDAS", recuperavel="10.00"),
    # Conflito de cadastro: fora da referência e da auditoria (quando o conflito é confiável).
    linha("K1", 2025, venda="200.00", custo="100.00", categoria="BEBIDAS", conflito=True),
    # LIMPEZA com 2 itens e um item sem categoria: sem referência.
    linha("L1", 2025, venda="120.00", custo="100.00", categoria="LIMPEZA"),
    linha("L2", 2025, venda="130.00", custo="100.00", categoria="LIMPEZA"),
    linha("N1", 2025, venda="100.00", custo="60.00"),
    # Custo implausível e item sem venda no último ano: fora.
    linha("Z1", 2025, venda="10.00", custo="100.00", categoria="BEBIDAS"),
    linha("O1", 2024, venda="10.00", custo="9.00", categoria="BEBIDAS"),
]


class AuditoriaTests(unittest.TestCase):
    def _auditar(self, linhas=AUDITORIA, parametros=PARAMETROS_PADRAO, ambiguo=0):
        base = cobertura_de({2024: 366, 2025: 365}, ambiguo=ambiguo)
        return inteligencia_estoque.auditar_precos(linhas, base, triagem(a_confirmar={"Z1"}), parametros)

    def test_referencia_da_categoria_pelo_menor_entre_agregado_e_mediana(self):
        resultado = self._auditar()
        self.assertEqual((resultado["base"], resultado["ano"], resultado["markup_informado"]), ("categoria", 2025, None))
        self.assertEqual((resultado["itens_avaliados"], resultado["itens_abaixo"], resultado["itens_acima"]), (4, 1, 2))
        # B4: 100 x 1,4 - 110 = 30, dos quais 10 já estão no potencial recuperável.
        self.assertEqual((resultado["diferenca_total"], resultado["ja_contado_no_recuperavel"], resultado["adicional"]),
                         (30.0, 10.0, 20.0))
        self.assertEqual(resultado["sem_referencia"], {"itens": 3, "venda": 350.0})
        self.assertEqual(resultado["categorias"], [{"categoria": "BEBIDAS", "itens": 1, "markup_referencia": 0.4,
                                                    "origem": "agregado", "diferenca": 30.0}])
        [item] = resultado["lista"]
        self.assertEqual({chave: item[chave] for chave in ("produto", "categoria", "preco_praticado", "custo_medio",
                                                           "markup_item", "preco_pelo_markup", "diferenca_por_unidade",
                                                           "diferenca", "ja_contado_no_recuperavel", "quantidade")},
                         {"produto": "Produto B4", "categoria": "BEBIDAS", "preco_praticado": 11.0, "custo_medio": 10.0,
                          "markup_item": 0.1, "preco_pelo_markup": 14.0, "diferenca_por_unidade": 3.0,
                          "diferenca": 30.0, "ja_contado_no_recuperavel": 10.0, "quantidade": 10.0})

    def test_cabecalho_ambiguo_inclui_o_conflito_e_a_mediana_vence(self):
        # Com cabeçalho ambíguo o conflito não é confiável: K1 (markup 1,0) entra. Agregado 0,52 e mediana
        # 0,5: a referência é a mediana; B3 (14 contra 15) também fica abaixo.
        resultado = self._auditar(ambiguo=1)
        self.assertEqual(resultado["categorias"][0]["origem"], "mediana")
        self.assertEqual(resultado["categorias"][0]["markup_referencia"], 0.5)
        self.assertEqual([i["produto"] for i in resultado["lista"]], ["Produto B4", "Produto B3"])
        self.assertEqual(resultado["diferenca_total"], 50.0)

    def test_markup_informado_vale_para_todos_os_itens(self):
        parametros = PARAMETROS_PADRAO.com_ajustes({"markup_informado": "0.3"})
        resultado = self._auditar(parametros=parametros)
        self.assertEqual((resultado["base"], resultado["markup_informado"]), ("informado", 0.3))
        self.assertEqual((resultado["itens_avaliados"], resultado["itens_abaixo"], resultado["itens_acima"]), (7, 2, 4))
        self.assertEqual(resultado["sem_referencia"], {"itens": 0, "venda": 0.0})
        self.assertEqual([(c["categoria"], c["diferenca"], c["origem"]) for c in resultado["categorias"]],
                         [("BEBIDAS", 20.0, "informado"), ("LIMPEZA", 10.0, "informado")])
        self.assertEqual((resultado["diferenca_total"], resultado["adicional"]), (30.0, 20.0))

    def test_tolerancia_de_preco(self):
        linhas = [linha("T1", 2025, venda="129.50", custo="100.00")]
        informado = {"markup_informado": "0.3"}
        # 12,95 contra 13,00: 5 centavos cabem na tolerância de 0,5% (6,5 centavos).
        self.assertEqual(self._auditar(linhas, PARAMETROS_PADRAO.com_ajustes(informado))["itens_abaixo"], 0)
        sem_tolerancia = self._auditar(linhas, PARAMETROS_PADRAO.com_ajustes({**informado, "tolerancia_preco": "0"}))
        self.assertEqual((sem_tolerancia["itens_abaixo"], sem_tolerancia["diferenca_total"]), (1, 0.5))

    def test_ja_contado_nao_passa_da_diferenca(self):
        linhas = [linha("R1", 2025, venda="110.00", custo="100.00", recuperavel="50.00")]
        resultado = self._auditar(linhas, PARAMETROS_PADRAO.com_ajustes({"markup_informado": "0.4"}))
        # Diferença 140 - 110 = 30; o recuperável (50) cobre a diferença inteira: nada adicional.
        self.assertEqual((resultado["diferenca_total"], resultado["ja_contado_no_recuperavel"], resultado["adicional"]),
                         (30.0, 30.0, 0.0))

    def test_minimo_de_itens_para_a_referencia(self):
        parametros = PARAMETROS_PADRAO.com_ajustes({"itens_minimos_markup": "5"})
        resultado = self._auditar(parametros=parametros)
        self.assertEqual((resultado["itens_avaliados"], resultado["sem_referencia"]["itens"]), (0, 7))


# =============================================================================
# 6. Resumo gravado e payload do consultor
# =============================================================================
class ResumoEPayloadTests(unittest.TestCase):
    def test_resumo_so_com_numeros(self):
        linhas = [dict(l, categoria_produto="BEBIDAS") for l in ESTOQUE] + AUDITORIA[:4]
        inteligencia = inteligencia_estoque.calcular(linhas, COBERTURA_2025, triagem(a_confirmar={"X"}), META_2025)
        resumo = inteligencia_estoque.resumo(inteligencia)
        self.assertEqual(set(resumo), {
            "versao", "com_estoque", "capital_em_estoque", "capital_parado", "itens_parados",
            "capital_excedente_liberavel", "urgencia_receita_a_preco_medio", "urgencia_capital_ao_custo",
            "urgencia_itens_com_estoque", "diferenca_de_preco_adicional", "itens_abaixo_do_preco_de_referencia",
            "itens_sem_estoque_informado", "itens_sem_estoque_com_custo", "itens_parados_sem_estoque_informado"})
        self.assertEqual(resumo["itens_sem_estoque_informado"], inteligencia["estoque"]["itens_sem_estoque_informado"])
        # Sem saldo, todos com custo: o F (sem venda desde janeiro, falta na soma do parado) e os quatro da
        # auditoria, que vendem até o fim e não entrariam nela.
        self.assertEqual((resumo["itens_sem_estoque_informado"], resumo["itens_sem_estoque_com_custo"],
                          resumo["itens_parados_sem_estoque_informado"]), (5, 5, 1))
        self.assertEqual(resumo["capital_parado"], {"60": 210.0, "90": 150.0, "180": 120.0})
        self.assertEqual(resumo["com_estoque"], True)
        texto = json.dumps(resumo, ensure_ascii=False)
        # Nada de nome de produto nem de categoria: o resumo fica depois do expurgo das linhas.
        for proibido in ("Produto", "BEBIDAS", "Bebidas"):
            self.assertNotIn(proibido, texto)

    def test_coluna_de_estoque_vazia_em_todas_as_linhas_vale_como_sem_estoque(self):
        # A coluna existe nos relatórios (com_estoque > 0), mas nenhuma linha traz o saldo: nulo, não zero.
        base = cobertura_de({2025: 304}, com_estoque={2025: 12})
        linhas = [linha("A", 2025, ultima=date(2025, 8, 31)), linha("E", 2025)]
        inteligencia = inteligencia_estoque.calcular(linhas, base, triagem(), META_2025)
        self.assertIsNone(inteligencia["estoque"])
        self.assertIsNone(inteligencia["simulador"])
        resumo = inteligencia_estoque.resumo(inteligencia)
        self.assertEqual((resumo["com_estoque"], resumo["capital_parado"], resumo["itens_sem_estoque_informado"],
                          resumo["itens_sem_estoque_com_custo"], resumo["itens_parados_sem_estoque_informado"]),
                         (False, None, None, None, None))
        # Um critério só (estoque_informado), também para a matriz: saldo zero informado é estoque; a coluna
        # ausente em todos os relatórios não é, mesmo com saldo na linha; meta sem a contagem decide pelas linhas.
        self.assertFalse(inteligencia_estoque.estoque_informado(linhas, base))
        self.assertTrue(inteligencia_estoque.estoque_informado([*linhas, linha("Z", 2025, estoque_final="0")], base))
        self.assertFalse(inteligencia_estoque.estoque_informado(
            [linha("Z", 2025, estoque_final="5")], cobertura_de({2025: 304}, com_estoque={2025: 0})))
        self.assertTrue(inteligencia_estoque.estoque_informado([linha("Z", 2025, estoque_final="5")],
                                                               cobertura_de({2025: 304})))
        self.assertFalse(inteligencia_estoque.estoque_informado(linhas, cobertura_de({2025: 304})))

    def test_sem_a_coluna_de_estoque_os_campos_de_estoque_ficam_nulos(self):
        base = cobertura_de({2025: 304}, com_estoque={2025: 0})
        inteligencia = inteligencia_estoque.calcular(ESTOQUE, base, triagem(), META_2025)
        self.assertIsNone(inteligencia["estoque"])
        self.assertIsNone(inteligencia["simulador"])
        self.assertIsNotNone(inteligencia["auditoria_de_preco"])
        resumo = inteligencia_estoque.resumo(inteligencia)
        self.assertEqual((resumo["com_estoque"], resumo["capital_em_estoque"], resumo["capital_parado"],
                          resumo["urgencia_itens_com_estoque"]), (False, None, None, None))
        self.assertIsInstance(resumo["itens_abaixo_do_preco_de_referencia"], int)

    def _payload(self, linhas, *, parametros=None, **meta_trocas):
        return diagnostico_cenario.montar_payload(_lead({2025: (12, 365, True)}, **meta_trocas), _registros(linhas),
                                                  agora=AGORA, retencao_dias=30, parametros_inteligencia=parametros)

    def test_matriz_com_a_coluna_de_estoque_vazia_tem_estoque_parado_nulo(self):
        # A coluna nos relatórios e nenhuma linha com saldo: a matriz e o gráfico das categorias dizem "não
        # informado" (nulo), como a inteligência, e nunca R$ 0,00 ao lado do aviso.
        linhas = [linha("A", 2025, ultima=date(2025, 8, 31), categoria="BEBIDAS"), linha("E", 2025, categoria="BEBIDAS")]
        payload = self._payload(linhas)
        self.assertIsNone(payload["inteligencia"]["estoque"])
        self.assertIsNone(payload["totais"]["estoque_parado"])
        self.assertEqual([c["estoque_parado"] for c in payload["categorias"]], [None])
        series = {s["nome"]: s["valores"] for s in payload["visualizacoes"]["categorias_por_impacto"]["series"]}
        self.assertEqual(series["Estoque parado"], [None])
        # Com saldo em uma das linhas, o estoque parado da matriz volta a ser número.
        com_saldo = self._payload([linha("A", 2025, ultima=date(2025, 8, 31), estoque_final="10", categoria="BEBIDAS"),
                                   linha("E", 2025, categoria="BEBIDAS")])
        self.assertIsInstance(com_saldo["totais"]["estoque_parado"], float)
        self.assertEqual([c["estoque_parado"] for c in com_saldo["categorias"]], [com_saldo["totais"]["estoque_parado"]])

    def test_payload_traz_a_inteligencia_com_os_parametros(self):
        linhas = [linha("A", 2025, ultima=date(2025, 8, 31), estoque_final="10"),
                  linha("E", 2025, estoque_final="100")]
        payload = self._payload(linhas)
        self.assertEqual(payload["versao"], "diagnostico-lead-2026-11.3")
        inteligencia = payload["inteligencia"]
        self.assertEqual(set(inteligencia), {"versao", "parametros", "estoque", "simulador", "auditoria_de_preco",
                                             "definicoes"})
        # Agosto a dezembro: 122 dias sem venda (faixa de 90 a 179).
        self.assertEqual([(i["produto"], i["dias_sem_venda"]) for i in inteligencia["estoque"]["parado"]["lista"]],
                         [("Produto A", 122)])
        self.assertEqual(inteligencia["parametros"]["descontos"], ["0", "0.10", "0.20", "0.30"])
        ajustado = self._payload(linhas, parametros=PARAMETROS_PADRAO.com_ajustes({"descontos": "0.5"}))
        self.assertEqual(ajustado["inteligencia"]["parametros"]["descontos"], ["0.5"])
        self.assertEqual(len(ajustado["inteligencia"]["simulador"]["cenarios"]), 1)
        # Nenhum contato do lead no payload.
        texto = json.dumps(payload, ensure_ascii=False, default=str)
        self.assertNotIn("lead@example.invalid", texto)
        self.assertNotIn("Contato fictício", texto)

    def test_falha_na_inteligencia_nao_derruba_a_matriz(self):
        linhas = [linha("A", 2025, estoque_final="10"), linha("B", 2025, estoque_final="5")]
        linhas[0]["fim_ultimo_relatorio_com_venda"] = "não é data"
        payload = self._payload(linhas)
        self.assertEqual(payload["situacao"], "atual")
        self.assertIsNotNone(payload["blocos"])
        self.assertIsNotNone(payload["totais"])
        self.assertIsNone(payload["inteligencia"])
        self.assertTrue(any("inteligência de estoque e de preço não pôde ser calculada" in a for a in payload["avisos"]))
        # Meta sem os períodos cobertos: o mesmo.
        sem_periodos = self._payload([linha("A", 2025, estoque_final="10")], periodos_cobertos=None)
        self.assertEqual((sem_periodos["situacao"], sem_periodos["inteligencia"]), ("atual", None))

    def test_avisos_de_estoque_ausente_e_de_varias_lojas(self):
        linhas = [linha("A", 2025, estoque_final="10")]
        sem_estoque = self._payload(linhas, relatorios_com_estoque_por_ano={"2025": 0})
        self.assertIsNone(sem_estoque["inteligencia"]["estoque"])
        self.assertTrue(any("não trazem estoque informado" in a for a in sem_estoque["avisos"]))
        lojas = self._payload(linhas, relatorios_varias_unidades=2)
        self.assertTrue(any(a.startswith("2 relatório(s) com várias lojas") for a in lojas["avisos"]))
        self.assertFalse(any("várias lojas" in a for a in self._payload(linhas)["avisos"]))


# =============================================================================
# 7. Motor, contrato e várias lojas
# =============================================================================
HOJE = date(2025, 11, 9)
SET25 = _relatorio(2025, 9, ["1;Cafe 500g;10;20,00;200,00;120,00;50", "2;Arroz 5kg;5;10,00;50,00;30,00;8"])
# Em outubro o Arroz só aparece com o saldo (quantidade 0): a última venda dele é de setembro.
OUT25 = _relatorio(2025, 10, ["1;Cafe 500g;8;20,00;160,00;96,00;45", "2;Arroz 5kg;0;10,00;0,00;;7"])
# Novembro exportado com o mês inteiro no cabeçalho, em 09/11: cobre só até hoje.
NOV25 = _relatorio(2025, 11, ["1;Cafe 500g;2;20,00;40,00;24,00;43"])
JUL25 = _relatorio(2025, 7, ["1;Cafe 500g;3;20,00;60,00;36,00;60"])


def _com_cabecalho(cabecalho, linhas, ano=2025, mes=11):
    texto = (f"Relatorio de vendas por produto\nPeriodo: 01/{mes:02d}/{ano} a 30/{mes:02d}/{ano}\n"
             + cabecalho + "\n" + "".join(l + "\n" for l in linhas))
    return texto.encode("utf-8")


DUAS_LOJAS = _com_cabecalho("Loja;Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque", [
    "Centro;1;Cafe 500g;10;20,00;200,00;150,00;7", "Bairro;1;Cafe 500g;5;20,00;100,00;75,00;3",
    "Centro;2;Arroz 5kg;2;27,90;55,80;44,00;4"])
SEM_LOJA = _com_cabecalho("Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque", [
    "1;Cafe 500g;10;20,00;200,00;150,00;7", "1;Cafe 500g;5;20,00;100,00;75,00;3",
    "2;Arroz 5kg;2;27,90;55,80;44,00;4"])


class _Arquivos(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="nexo-inteligencia-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)

    def caminhos(self, *conteudos):
        saida = []
        for conteudo in conteudos:
            caminho = self.pasta / f"arquivo_{uuid.uuid4().hex[:8]}.csv"
            caminho.write_bytes(conteudo)
            saida.append(str(caminho))
        return saida

    def avaliar(self, *conteudos, hoje=HOJE):
        resultado = avaliar_como_o_worker(self.caminhos(*conteudos), hoje=hoje)
        colunas = resultado["linhas"]["colunas"]
        return resultado, {l["descricao_produto"]: l for l in (dict(zip(colunas, v)) for v in resultado["linhas"]["valores"])}


class MotorTests(_Arquivos):
    def test_data_da_ultima_venda_e_periodos_cobertos(self):
        resultado, linhas = self.avaliar(SET25, OUT25, NOV25)
        self.assertEqual(resultado["versao"], "lead-2026-11.4")
        # O Café vendeu em novembro (cortado em hoje); o Arroz, só em setembro.
        self.assertEqual(linhas["Cafe 500g"]["fim_ultimo_relatorio_com_venda"], "2025-11-09")
        self.assertEqual(linhas["Arroz 5kg"]["fim_ultimo_relatorio_com_venda"], "2025-09-30")
        # O saldo do Arroz é o de outubro (7), mesmo sem venda nele, e não o de setembro (8).
        self.assertEqual(linhas["Arroz 5kg"]["estoque_final"], 7)
        self.assertEqual(resultado["meta"]["periodos_cobertos"], [["2025-09-01", "2025-11-09"]])
        self.assertEqual(resultado["meta"]["relatorios_varias_unidades"], 0)
        # Com uma lacuna (agosto sem relatório), dois períodos.
        com_lacuna, _ = self.avaliar(JUL25, SET25)
        self.assertEqual(com_lacuna["meta"]["periodos_cobertos"], [["2025-07-01", "2025-07-31"], ["2025-09-01", "2025-09-30"]])

    def test_inteligencia_de_ponta_a_ponta_sobre_o_envio(self):
        import avaliacao_cenario
        resultado, _ = self.avaliar(SET25, OUT25, NOV25)
        linhas = avaliacao_cenario.quantizar(resultado["linhas"])
        self.assertIsInstance(linhas[0]["fim_ultimo_relatorio_com_venda"], date)
        base = Cobertura.do_envio(resultado["meta"])
        triado = triagem_cenario.triar(linhas, base)
        inteligencia = inteligencia_estoque.calcular(linhas, base, triado, resultado["meta"])
        estoque = inteligencia["estoque"]
        # 70 dias cobertos (setembro, outubro e 9 dias de novembro). Café: 20 vendidos, custo médio 12,
        # 43 em estoque; Arroz: 5 vendidos, custo médio 6, 7 em estoque e 40 dias sem venda.
        self.assertEqual(estoque["capital_em_estoque"], 43 * 12 + 7 * 6)
        self.assertEqual(estoque["parado"]["itens"], 0)
        cafe = (D(43) - D(20) / D(70) * 60) * 12
        arroz = (D(7) - D(5) / D(70) * 60) * 6
        self.assertEqual(estoque["excedente"]["capital_liberavel"], float((cafe + arroz).quantize(D("0.01"))))

    def test_estoque_parado_de_ponta_a_ponta(self):
        meses = []
        for mes in range(1, 11):
            # O Feijão vende só em março; nos outros meses o relatório traz o saldo dele (quantidade 0).
            feijao = "3;Feijao 1kg;6;8,00;48,00;30,00;40" if mes == 3 else "3;Feijao 1kg;0;8,00;0,00;;40"
            meses.append(_relatorio(2025, mes, ["1;Cafe 500g;10;20,00;200,00;120,00;12", feijao]))
        resultado, linhas = self.avaliar(*meses)
        self.assertEqual(linhas["Feijao 1kg"]["fim_ultimo_relatorio_com_venda"], "2025-03-31")
        import avaliacao_cenario
        staging = avaliacao_cenario.quantizar(resultado["linhas"])
        base = Cobertura.do_envio(resultado["meta"])
        inteligencia = inteligencia_estoque.calcular(staging, base, triagem_cenario.triar(staging, base), resultado["meta"])
        [item] = inteligencia["estoque"]["parado"]["lista"]
        # Abril a outubro: 214 dias sem venda; 40 em estoque a 5 de custo médio.
        self.assertEqual((item["produto"], item["dias_sem_venda"], item["capital"], item["com_lacuna"]),
                         ("Feijao 1kg", 214, 200.0, False))


class ContratoTests(_Arquivos):
    def setUp(self):
        super().setUp()
        self.valido, _ = self.avaliar(SET25, OUT25)

    def _invalido(self, mudar):
        copia = json.loads(json.dumps(self.valido))
        mudar(copia)
        with self.assertRaises(ETLInputError) as erro:
            etl_runtime.validar_avaliacao(copia)
        self.assertEqual(erro.exception.code, "worker")

    def test_data_periodos_e_lojas_fora_do_formato_sao_recusados(self):
        indice = avaliacao_motor.COLUNAS.index("fim_ultimo_relatorio_com_venda")

        def data(valor):
            return lambda r: r["linhas"]["valores"][0].__setitem__(indice, valor)

        def meta(chave, valor):
            return lambda r: r["meta"].__setitem__(chave, valor)

        mudancas = {
            "data de outro ano": data("2024-09-30"),
            "data antes dos períodos": data("2025-08-31"),
            "data depois dos períodos": data("2025-11-01"),
            "data no formato brasileiro": data("30/09/2025"),
            "data inexistente": data("2025-02-30"),
            "data nula": data(None),
            "data numérica": data(20250930),
            "períodos ausentes": lambda r: r["meta"].pop("periodos_cobertos"),
            "períodos vazios": meta("periodos_cobertos", []),
            "período invertido": meta("periodos_cobertos", [["2025-10-31", "2025-09-01"]]),
            "períodos sobrepostos": meta("periodos_cobertos", [["2025-09-01", "2025-10-15"], ["2025-10-01", "2025-10-31"]]),
            "período de outro ano": meta("periodos_cobertos", [["2023-09-01", "2023-10-31"]]),
            "período com texto": meta("periodos_cobertos", [["início", "2025-10-31"]]),
            "período em objeto": meta("periodos_cobertos", [{"inicio": "2025-09-01", "fim": "2025-10-31"}]),
            "lojas acima dos arquivos": meta("relatorios_varias_unidades", 3),
            "lojas negativas": meta("relatorios_varias_unidades", -1),
            "lojas booleano": meta("relatorios_varias_unidades", True),
            "lojas em texto": meta("relatorios_varias_unidades", "1"),
        }
        for caso, mudar in mudancas.items():
            with self.subTest(caso=caso):
                self._invalido(mudar)
        # O resultado sem mudança passa.
        etl_runtime.validar_avaliacao(json.loads(json.dumps(self.valido)))

    def test_data_de_outro_ano_dentro_dos_periodos_e_recusada(self):
        # Dezembro de 2024 e janeiro de 2025 viram um período só: a data de 2024 numa linha de 2025 fica
        # dentro dele, e só a conferência do ano a recusa.
        valido, _ = self.avaliar(_relatorio(2024, 12, ["1;Cafe 500g;10;20,00;200,00;120,00;50"]),
                                 _relatorio(2025, 1, ["1;Cafe 500g;8;20,00;160,00;96,00;45"]))
        self.assertEqual(valido["meta"]["periodos_cobertos"], [["2024-12-01", "2025-01-31"]])
        ano = avaliacao_motor.COLUNAS.index("ano_exercicio")
        data = avaliacao_motor.COLUNAS.index("fim_ultimo_relatorio_com_venda")
        [posicao] = [i for i, valores in enumerate(valido["linhas"]["valores"]) if valores[ano] == 2025]
        self.assertEqual(valido["linhas"]["valores"][posicao][data], "2025-01-31")
        self.valido = valido
        self._invalido(lambda r: r["linhas"]["valores"][posicao].__setitem__(data, "2024-12-31"))


class VariasLojasTests(_Arquivos):
    def test_estoque_da_rede_somado_e_relatorio_marcado(self):
        resultado, linhas = self.avaliar(DUAS_LOJAS)
        # Último estoque de cada loja: 7 no Centro + 3 no Bairro.
        self.assertEqual(linhas["Cafe 500g"]["estoque_final"], 10)
        self.assertEqual(linhas["Cafe 500g"]["quantidade"], 15)
        self.assertEqual(resultado["meta"]["relatorios_varias_unidades"], 1)
        # Sem a coluna de loja, o estoque é o da última linha (o comportamento de antes).
        _sem, linhas_sem = self.avaliar(SEM_LOJA)
        self.assertEqual(linhas_sem["Cafe 500g"]["estoque_final"], 3)

    def test_mesma_loja_escrita_de_jeitos_diferentes_e_uma_so(self):
        uma = _com_cabecalho("Filial;Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque", [
            "1;1;Cafe 500g;10;20,00;200,00;150,00;7", "1.0;1;Cafe 500g;5;20,00;100,00;75,00;3"])
        resultado, linhas = self.avaliar(uma)
        self.assertEqual((resultado["meta"]["relatorios_varias_unidades"], linhas["Cafe 500g"]["estoque_final"]), (0, 3))
        from raio_x import _texto_da_unidade
        self.assertEqual({_texto_da_unidade(v) for v in ("Loja  Centro", "LOJA CENTRO", " loja centro ")}, {"loja centro"})
        self.assertEqual({_texto_da_unidade(v) for v in (1, 1.0, "1", "1.0")}, {"1"})
        self.assertEqual({_texto_da_unidade(v) for v in (None, float("nan"))}, {""})

    def test_unidade_de_medida_e_medidas_da_loja_nao_sao_loja(self):
        casos = {
            "Unidade (UN/KG)": ("Codigo;Produto;Unidade;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque",
                                ["1;Cafe 500g;UN;10;20,00;200,00;150,00;7", "2;Arroz 5kg;KG;2;27,90;55,80;44,00;4"]),
            "Margem Loja": ("Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque;Margem Loja",
                            ["1;Cafe 500g;10;20,00;200,00;150,00;7;25", "1;Cafe 500g;5;20,00;100,00;75,00;3;30"]),
            "Meta Filial": ("Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque;Meta Filial",
                            ["1;Cafe 500g;10;20,00;200,00;150,00;7;1000", "1;Cafe 500g;5;20,00;100,00;75,00;3;1200"]),
        }
        for caso, (cabecalho, linhas) in casos.items():
            with self.subTest(caso=caso):
                resultado, por_nome = self.avaliar(_com_cabecalho(cabecalho, linhas))
                self.assertEqual(resultado["meta"]["relatorios_varias_unidades"], 0)
                self.assertEqual(por_nome["Cafe 500g"]["estoque_final"], 7 if caso.startswith("Unidade") else 3)

    def test_coluna_de_loja_no_lugar_do_produto_e_recusada(self):
        recusados = {
            "Descrição Loja antes da descrição": ("Codigo;Descricao Loja;Produto;Qtde;Preco Unitario;Valor Total;Total Custo",
                                                  "1;Centro;Cafe 500g;10;20,00;200,00;150,00"),
            "Descrição Loja antes de Descrição do Produto": (
                "Codigo;Descricao Loja;Descricao do Produto;Qtde;Preco Unitario;Valor Total;Total Custo",
                "1;Centro;Cafe 500g;10;20,00;200,00;150,00"),
            "Cód. Loja sem código do produto": ("Cod. Loja;Produto;Qtde;Preco Unitario;Valor Total;Total Custo",
                                                "10;Cafe 500g;10;20,00;200,00;150,00"),
            "Código Loja antes do código": ("Codigo Loja;Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo",
                                            "10;1;Cafe 500g;10;20,00;200,00;150,00"),
        }
        for caso, (cabecalho, valores) in recusados.items():
            with self.subTest(caso=caso):
                with self.assertRaises(ETLInputError) as erro:
                    self.avaliar(_com_cabecalho(cabecalho, [valores]))
                self.assertEqual(erro.exception.code, "avaliacao_cabecalho_loja")
                self.assertIn("colunas de loja depois das do produto", str(erro.exception))
        # As colunas de loja depois das do produto: aceito, e a rede é detectada.
        depois = _com_cabecalho("Codigo;Descricao do Produto;Descricao Loja;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque",
                                ["1;Cafe 500g;Centro;10;20,00;200,00;150,00;7", "1;Cafe 500g;Bairro;5;20,00;100,00;75,00;3"])
        resultado, linhas = self.avaliar(depois)
        self.assertEqual((resultado["meta"]["relatorios_varias_unidades"], linhas["Cafe 500g"]["estoque_final"]), (1, 10))
        # "Produto Loja" ainda é o produto (fica como cabeçalho ambíguo, sem NCM nem categoria).
        resultado, linhas = self.avaliar(_com_cabecalho("Codigo;Produto Loja;Qtde;Preco Unitario;Valor Total;Total Custo",
                                                        ["1;Cafe 500g;10;20,00;200,00;150,00"]))
        self.assertEqual((list(linhas), resultado["meta"]["relatorios_cabecalho_ambiguo"]), (["Cafe 500g"], 1))

    def test_raio_x_do_cliente_nao_muda_com_a_coluna_de_loja(self):
        com_loja = executar_como_o_worker(self.caminhos(DUAS_LOJAS))
        sem_loja = executar_como_o_worker(self.caminhos(SEM_LOJA))
        # Os indicadores executivos (raio-x-2026-11.6) usam a loja para saber se o relatório é venda item a
        # item: uma linha por produto e loja é resumo por loja (modo acumulado). Sem a coluna, o mesmo produto
        # repetido parece venda item a item. O resto do Raio-X não muda.
        indicadores_com_loja = com_loja.pop("indicadores_executivos")
        sem_loja.pop("indicadores_executivos")
        self.assertEqual(com_loja, sem_loja)
        self.assertEqual(indicadores_com_loja["modo"], "acumulado")


# =============================================================================
# 8. Gravação, retriagem e rota
# =============================================================================
LOTE_COM_ESTOQUE = (SET25, OUT25)


class _BancoCase(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        pasta = tempfile.TemporaryDirectory(prefix="nexo-inteligencia-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)

    def caminhos(self, *conteudos):
        saida = []
        for conteudo in conteudos:
            caminho = self.pasta / f"arquivo_{uuid.uuid4().hex[:8]}.csv"
            caminho.write_bytes(conteudo)
            saida.append(str(caminho))
        return saida


class GravacaoTests(_BancoCase):
    def _registrar(self):
        import avaliacao_cenario
        resultado = avaliar_como_o_worker(self.caminhos(*LOTE_COM_ESTOQUE), hoje=date(2025, 12, 15))
        lead = avaliacao_cenario.registrar(resultado, contato=CONTATO, registrado_por=self.admin_id, meses_prata=24)
        self.db.session.commit()
        return lead, resultado

    def _recalculado(self, lead):
        import avaliacao_cenario
        from models import BrainstormingDadosPdv
        registros = self.db.session.scalars(select(BrainstormingDadosPdv).where(BrainstormingDadosPdv.lead_id == lead.id)
                                            .order_by(BrainstormingDadosPdv.chave_produto, BrainstormingDadosPdv.ano_exercicio)).all()
        linhas = [avaliacao_cenario.linha_da_staging(r) for r in registros]
        meta = json.loads(lead.meta_json)
        base = Cobertura.do_envio(meta)
        return linhas, inteligencia_estoque.resumo(inteligencia_estoque.calcular(
            linhas, base, triagem_cenario.triar(linhas, base, avaliacao_cenario.limiares_do_lead(lead)), meta))

    def test_registrar_grava_o_resumo_e_a_data_da_ultima_venda(self):
        lead, _resultado = self._registrar()
        gravado = json.loads(lead.resumo_inteligencia_json)
        linhas, recalculado = self._recalculado(lead)
        self.assertEqual(gravado, recalculado)
        self.assertEqual((gravado["com_estoque"], gravado["capital_em_estoque"]), (True, 45 * 12 + 7 * 6))
        self.assertEqual({l["descricao_produto"]: l["fim_ultimo_relatorio_com_venda"] for l in linhas},
                         {"Cafe 500g": date(2025, 10, 31), "Arroz 5kg": date(2025, 9, 30)})
        self.assertNotIn("Cafe", lead.resumo_inteligencia_json)

    def test_retriagem_refaz_o_resumo(self):
        import avaliacao_cenario
        lead, _resultado = self._registrar()
        original = lead.resumo_inteligencia_json
        lead.resumo_inteligencia_json = None
        self.db.session.commit()
        avaliacao_cenario.retriar(lead.id)
        self.db.session.commit()
        self.db.session.refresh(lead)
        self.assertEqual(json.loads(lead.resumo_inteligencia_json), json.loads(original))

    def test_resumo_ilegivel_nao_impede_a_gravacao(self):
        import avaliacao_cenario
        with patch.object(inteligencia_estoque, "calcular", side_effect=ArithmeticError("teste")):
            lead, _resultado = self._registrar()
        self.assertIsNone(lead.resumo_inteligencia_json)
        self.assertEqual(lead.status, "concluido")
        # Meta sem os períodos cobertos (envio de antes da Etapa 3), com estoque a calcular: sem resumo, sem erro.
        self.assertIsNone(avaliacao_cenario.resumo_da_inteligencia([linha("A", 2025, estoque_final="1")],
                                                                   cobertura_de({2025: 365}), triagem(), {}))


class RotaTests(_BancoCase):
    PAGINA = "/admin/leads"

    def setUp(self):
        super().setUp()

        def worker(caminhos, limits=None):
            return avaliar_como_o_worker(caminhos, limits)

        self.stack.enter_context(patch("etl_runtime.run_avaliacao_cenario", side_effect=worker))

    def _lead(self):
        import io
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        token = self.csrf_token(self.PAGINA)
        dados = {"csrf_token": token, "nome_contato": "Contato fictício", "nome_loja": "Mercado fictício",
                 "email": "lead@example.invalid", "whatsapp": "(11) 90000-0000", "autorizacao": "1",
                 "arquivos": [(io.BytesIO(c), f"mes_{i}.csv") for i, c in enumerate(LOTE_COM_ESTOQUE, 1)]}
        resposta = self.client.post(self.PAGINA, data=dados, content_type="multipart/form-data")
        self.assertEqual(resposta.status_code, 303, resposta.get_data(as_text=True)[:400])
        return self.db.session.scalar(select(LeadAvaliacao))

    def test_parametros_do_simulador_pela_consulta(self):
        lead = self._lead()
        caminho = f"{self.PAGINA}/{lead.id}/diagnostico.json"
        padrao = self.client.get(caminho)
        self.assertEqual(padrao.status_code, 200)
        self.assertEqual(padrao.get_json()["inteligencia"]["parametros"]["descontos"], ["0", "0.10", "0.20", "0.30"])
        ajustado = self.client.get(caminho + "?descontos=0.1,0.25&fracao_liquidada=0.5&markup_informado=0.35"
                                             "&cobertura_alvo_dias=45&outro=ignorado")
        self.assertEqual(ajustado.status_code, 200)
        inteligencia = ajustado.get_json()["inteligencia"]
        self.assertEqual((inteligencia["parametros"]["descontos"], inteligencia["parametros"]["fracao_liquidada"],
                          inteligencia["parametros"]["cobertura_alvo_dias"]), (["0.1", "0.25"], "0.5", 45))
        self.assertEqual([c["desconto"] for c in inteligencia["simulador"]["cenarios"]], [0.1, 0.25])
        self.assertEqual(inteligencia["auditoria_de_preco"]["base"], "informado")
        self.assertEqual(ajustado.headers.get("Cache-Control"), "no-store")

    def test_parametro_invalido_responde_400_sem_calcular(self):
        lead = self._lead()
        caminho = f"{self.PAGINA}/{lead.id}/diagnostico.json"
        for consulta in ("descontos=abc", "descontos=0.99", "fracao_liquidada=0", "cobertura_alvo_dias=0",
                         "markup_informado=1e3", "descontos=" + ",".join(["0.1"] * 7)):
            with self.subTest(consulta=consulta):
                resposta = self.client.get(f"{caminho}?{consulta}")
                self.assertEqual(resposta.status_code, 400)
                corpo = resposta.get_json()
                self.assertEqual(corpo["erro"], "parametros_invalidos")
                self.assertEqual(resposta.headers.get("Cache-Control"), "no-store")
                self.assertNotIn("Traceback", resposta.get_data(as_text=True))

    def test_so_o_consultor_le_o_diagnostico(self):
        lead = self._lead()
        self.logout()
        caminho = f"{self.PAGINA}/{lead.id}/diagnostico.json?descontos=0.1"
        self.assertEqual(self.client.get(caminho).status_code, 302)
        self.login()
        self.assertEqual(self.client.get(caminho).status_code, 403)


# =============================================================================
# 9. Memória
# =============================================================================
class MemoriaTests(unittest.TestCase):
    def test_vinte_mil_produtos_em_dois_anos(self):
        import tracemalloc
        linhas = []
        for i in range(20000):
            for ano in (2024, 2025):
                linhas.append(linha(f"{i:016x}", ano, ultima=date(ano, 1 + i % 12, 28), estoque_final=str(i % 50),
                                    categoria=f"CATEGORIA {i % 60}", venda=f"{100 + i % 37}.00",
                                    recuperavel=f"{i % 5}.00"))
        base = cobertura_de({2024: 366, 2025: 365})
        triado = triagem_cenario.triar(linhas, base)
        meta = meta_com(("2024-01-01", "2025-12-31"))
        tracemalloc.start()
        try:
            resultado = inteligencia_estoque.calcular(linhas, base, triado, meta)
            _atual, pico = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # O cálculo roda no processo da aplicação, sobre linhas já carregadas: folga larga para os 384 MB
        # do processo isolado e para o Render.
        self.assertLess(pico, 64 * 1024 * 1024, f"pico de {pico / 2**20:.1f} MiB")
        self.assertEqual(len(resultado["estoque"]["parado"]["lista"]), PARAMETROS_PADRAO.itens_listados)
        self.assertLessEqual(len(json.dumps(resultado)), 512 * 1024)


if __name__ == "__main__":
    unittest.main()
