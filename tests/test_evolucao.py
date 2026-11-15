"""Evolução & Conquistas mensais (Etapa 3, 14/11/2026): o retrato de cada Raio-X e a comparação com o Marco Zero.

Executar da raiz: python -B -m unittest tests.test_evolucao -v
Sem rede; o Raio-X sai do motor de verdade no próprio processo (tests.raio_x_support), e as telas saem do app com as
migrações reais em SQLite (o caso base de tests.test_planos_raio_x).

  - O retrato: o mês do Raio-X no horário de Brasília, os ritmos por mês do Plano de Ação, só agregados (nenhum nome
    de produto), e o envio sem datas (sem ritmo).
  - A comparação: nenhum retrato, um (o estado inicial), dois e três ou mais (Marco Zero, anterior e atual); a
    economia mensal e a projeção de 12 meses; os selos nas bordas das regras do dono; o resumo; o foco do próximo
    mês; a linha do tempo com Sanado, Em evolução e Pendente; o plano sem a margem; nenhuma divisão por zero.
  - A gravação: um retrato por mês (o Raio-X refeito no mesmo mês substitui), o Marco Zero do Raio-X anterior à
    Etapa 3, e o Raio-X que grava igual sem a tabela ou com erro no retrato.
  - As telas: a Visão Geral, o Histórico e o Plano de Ação, com 0, 1 e 2 Raio-X, no Bronze e no Prata.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

import evolucao
import raio_x_servico
from tests.raio_x_support import executar_como_o_worker
# Pelo módulo, e não pelo nome: a classe importada no módulo seria coletada (e rodada) de novo aqui.
import tests.test_planos_raio_x as planos
from tests.test_raio_x_apresentacao import BRONZE, PRATA

PAGINA, AUDITORIA = planos.PAGINA, planos.AUDITORIA
DASHBOARD, HISTORICO = "/cliente/dashboard", "/cliente/historico"
_CAB = "Relatorio de vendas por produto\nPeriodo: {}\nCodigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque\n"
# Outubro: perda (Leite abaixo do custo), vazamento (Café com preço variando) e venda sem custo (Açúcar).
OUTUBRO = (_CAB.format("01/10/2026 a 31/10/2026") + "1;Cafe 500g;10;20,00;200,00;150,00;5\n"
           "1;Cafe 500g;10;20,00;160,00;150,00;5\n2;Leite 1L;20;5,00;100,00;120,00;4\n"
           "3;Acucar 1kg;5;6,00;30,00;;3\n").encode("utf-8")
# Novembro: a perda zerada, o vazamento menor e o custo cadastrado.
NOVEMBRO = (_CAB.format("01/11/2026 a 30/11/2026") + "1;Cafe 500g;10;20,00;200,00;150,00;5\n"
            "1;Cafe 500g;10;20,00;190,00;150,00;5\n2;Leite 1L;20;7,00;140,00;120,00;4\n"
            "3;Acucar 1kg;5;6,00;30,00;20,00;3\n").encode("utf-8")
SEM_DATAS = ("Relatorio de vendas por produto\nCodigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n"
             "1;Cafe 500g;10;20,00;180,00;190,00\n").encode("utf-8")
UTC = timezone.utc


def _diagnosticar(*conteudos):
    with tempfile.TemporaryDirectory(prefix="evolucao-") as pasta:
        caminhos = []
        for numero, conteudo in enumerate(conteudos, 1):
            caminho = Path(pasta) / f"arquivo_{numero:02d}.csv"
            caminho.write_bytes(conteudo)
            caminhos.append(str(caminho))
        return executar_como_o_worker(caminhos)


def _retrato_do_motor(conteudo, quando):
    return raio_x_servico.retrato_do_raio_x(_diagnosticar(conteudo), quando)


def _retrato(dia, recuperavel=60.0, perda=20.0, vazamento=40.0, pct=0.1, negativa=1, alertas=0, margem=0.3,
             estoque_negativo=0, estoque=True, projetavel=True, venda_com_custo=500.0, variacao=True, meses=1.0,
             sem_coluna=0):
    """Um retrato sintético (o formato de evolucao.retrato), com os ritmos por mês dados."""
    mensal = (lambda valor: valor if projetavel else None)
    return {
        "versao": evolucao.VERSAO, "versao_regras": "raio-x-2026-11.7",
        "competencia": date(dia.year, dia.month, 1).isoformat(),
        "gerado_em": datetime(dia.year, dia.month, dia.day, 15, tzinfo=UTC).isoformat(), "data": dia.isoformat(),
        "periodo": {"inicio": None, "fim": None, "texto": "1 mês" if projetavel else "",
                    "aviso": None if projetavel else "vendas sem as datas do período",
                    "meses": meses if projetavel else None, "meses_auditados": meses if projetavel else None,
                    "projetavel": projetavel},
        "mensal": {"recuperavel": mensal(recuperavel), "perda": mensal(perda), "vazamento": mensal(vazamento),
                   "faturamento": mensal(1000.0)},
        "total": {"recuperavel": recuperavel, "perda": perda, "vazamento": vazamento, "faturamento": 1000.0,
                  "venda_com_custo": venda_com_custo},
        "pct_sem_custo": pct,
        "itens": {"margem_negativa": negativa, "custo_a_confirmar": alertas, "sem_custo": 1, "vendidos": 10,
                  "estoque_negativo": estoque_negativo},
        "margem_ajustada": margem, "variacao_visivel": variacao, "arquivos_com_custo": 1 if venda_com_custo else 0,
        "arquivos_sem_coluna": sem_coluna,
        "estoque": estoque, "etapas": {"estancar": "pendente", "travar": "pendente", "controle": "pendente"},
    }


def _numeros(valor):
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


def _por_id(lista, chave):
    return {item["id"]: item for item in lista}[chave]


# =====================================================================
# Retrato
# =====================================================================
class RetratoTests(unittest.TestCase):
    def test_retrato_de_um_raio_x_de_verdade(self):
        r = _retrato_do_motor(OUTUBRO, datetime(2026, 11, 3, 13, tzinfo=UTC))
        self.assertTrue(evolucao.valido(r))
        self.assertEqual((r["competencia"], r["data"], r["versao_regras"]), ("2026-11-01", "2026-11-03",
                                                                             "raio-x-2026-11.7"))
        self.assertEqual(r["periodo"]["texto"], "1 mês (01/10/2026 a 31/10/2026)")
        self.assertEqual(r["mensal"], {"recuperavel": 60.0, "perda": 20.0, "vazamento": 40.0, "faturamento": 490.0})
        self.assertEqual((r["pct_sem_custo"], r["itens"]["margem_negativa"], r["itens"]["sem_custo"]),
                         (0.0612, 1, 1))
        self.assertEqual((r["margem_ajustada"], r["variacao_visivel"], r["estoque"]), (0.087, True, True))
        self.assertEqual((r["periodo"]["meses"], r["periodo"]["meses_auditados"], r["arquivos_sem_coluna"]),
                         (1.0, 1.0, 0))
        # Só agregados: nenhum nome de produto no retrato gravado.
        texto = json.dumps(r, ensure_ascii=False)
        for produto in ("Cafe", "Leite", "Acucar"):
            self.assertNotIn(produto, texto)

    def test_mes_do_raio_x_no_horario_de_brasilia(self):
        # 02h de 01/12 em UTC ainda é 30/11 em Brasília: o retrato é de novembro.
        self.assertEqual(evolucao.competencia(datetime(2026, 12, 1, 2, tzinfo=UTC)), date(2026, 11, 1))
        self.assertEqual(evolucao.competencia(datetime(2026, 12, 1, 3, tzinfo=UTC)), date(2026, 12, 1))
        with self.assertRaises(ValueError):
            evolucao.competencia(datetime(2026, 12, 1))

    def test_envio_sem_datas_nao_tem_ritmo(self):
        r = _retrato_do_motor(SEM_DATAS, datetime(2026, 11, 3, 13, tzinfo=UTC))
        self.assertFalse(r["periodo"]["projetavel"])
        self.assertEqual(r["mensal"], {"recuperavel": None, "perda": None, "vazamento": None, "faturamento": None})
        # O total existe (custo de 190,00 numa venda de 180,00), mas sem o período não vira ritmo por mês.
        self.assertEqual(r["total"]["perda"], 10.0)
        self.assertEqual((r["periodo"]["texto"], r["periodo"]["aviso"]), ("", "vendas sem as datas do período"))

    def test_mes_parcial_vale_pelo_ritmo_real(self):
        # Um relatório de 01/11 a 14/11, feito em 14/11: 14 dos 30 dias de novembro. O ritmo é o total dividido por
        # 14/30 de mês, sem o piso de 1 mês do Plano de Ação (que poria 14 dias contra 30 como se fossem iguais).
        meio = (_CAB.format("01/11/2026 a 14/11/2026") + "2;Leite 1L;20;5,00;100,00;120,00;4\n").encode("utf-8")
        r = _retrato_do_motor(meio, datetime(2026, 11, 14, 15, tzinfo=UTC))
        self.assertAlmostEqual(r["periodo"]["meses"], 14 / 30)
        self.assertEqual((r["total"]["perda"], r["mensal"]["perda"]), (20.0, 42.86))
        # O mesmo ritmo diário num mês cheio: estável, sem economia inventada, com o aviso do ritmo estimado.
        cheio = (_CAB.format("01/12/2026 a 31/12/2026") + "2;Leite 1L;31;5,00;155,00;186,00;4\n").encode("utf-8")
        painel = evolucao.comparar([r, _retrato_do_motor(cheio, datetime(2027, 1, 2, 13, tzinfo=UTC))], PRATA)
        perda = _por_id(painel["indicadores"], "perda")
        self.assertEqual((perda["marco_texto"], perda["atual_texto"]), ("R$ 42,86/mês", "R$ 31,00/mês"))
        self.assertIn("O Marco Zero cobre menos de 1 mês: o ritmo por mês dele é estimado pelos dias cobertos.",
                      painel["avisos"])

    def test_retrato_estranho_fica_de_fora(self):
        for estranho in (None, "texto", {}, {"versao": "outra-1"}, {**_retrato(date(2026, 1, 5)), "competencia": "x"},
                         {**_retrato(date(2026, 1, 5)), "gerado_em": None}):
            with self.subTest(estranho=estranho):
                self.assertFalse(evolucao.valido(estranho))


# =====================================================================
# Comparação
# =====================================================================
class CompararTests(unittest.TestCase):
    def test_nenhum_retrato_valido_nao_tem_painel(self):
        self.assertIsNone(evolucao.comparar([], evolucao.TUDO))
        self.assertIsNone(evolucao.comparar(None, evolucao.TUDO))
        self.assertIsNone(evolucao.comparar([None, "x", {"a": 1}], evolucao.TUDO))

    def test_um_retrato_e_o_estado_inicial(self):
        painel = evolucao.comparar([_retrato(date(2026, 11, 3))], PRATA)
        self.assertEqual((painel["estado"], painel["quantidade"], painel["marco"]["data"]), ("inicial", 1, "03/11/2026"))
        self.assertEqual(painel["mensagem"], "A comparação de evolução será ativada automaticamente no próximo "
                                             "fechamento mensal (todo dia 1º): é só enviar o relatório de vendas do mês "
                                             "no Raio-X do PDV.")
        self.assertEqual(painel["foco"], "travar os descontos no balcão (R$ 40,00 por mês em vendas abaixo do preço "
                                         "normal)")
        self.assertNotIn("economia", painel)
        self.assertEqual([m["status"] for m in painel["linha_do_tempo"][0]["metas"]], ["partida"] * 3)

    def test_dois_retratos_economia_projecao_e_resumo(self):
        marco = _retrato(date(2026, 11, 3), recuperavel=60.0, perda=20.0, vazamento=40.0, pct=0.0612, margem=0.087)
        atual = _retrato(date(2026, 12, 1), recuperavel=10.0, perda=0.0, vazamento=10.0, pct=0.0, negativa=0,
                         margem=0.2143)
        painel = evolucao.comparar([atual, marco], PRATA)
        self.assertEqual((painel["estado"], painel["marco"]["data"], painel["atual"]["data"]),
                         ("comparado", "03/11/2026", "01/12/2026"))
        # Com dois retratos, o anterior é o próprio Marco Zero.
        self.assertEqual(painel["anterior"]["data"], "03/11/2026")
        self.assertEqual((painel["economia"]["mensal"], painel["economia"]["anual"]), (50.0, 600.0))
        vazamento = _por_id(painel["indicadores"], "vazamento")
        self.assertEqual((vazamento["vs_marco"], vazamento["variacao_marco_texto"]), ("melhorou", "75% a menos"))
        sem_custo = _por_id(painel["indicadores"], "sem_custo")
        self.assertEqual(sem_custo["variacao_marco_texto"], "6,1 pontos a menos")
        self.assertEqual(painel["regressoes"], [])
        self.assertEqual(painel["conquistados"], 4)
        self.assertEqual(painel["resumo"],
                         "Comparado ao seu Raio-X inicial de 03/11/2026, você zerou as vendas abaixo do custo, reduziu "
                         "os vazamentos de preço em 75%, melhorou a qualidade do cadastro (venda sem custo de 6,1% para "
                         "0%), reduziu em 1 item a lista de margem negativa e alertas de cadastro e aumentou a sobra "
                         "bruta ajustada de 8,7% para 21,4%. Mantendo esse ritmo, seu ganho projetado em 12 meses é de "
                         "R$ 600,00. Excelente evolução — para o próximo mês, o foco principal é travar os descontos no "
                         "balcão (R$ 10,00 por mês em vendas abaixo do preço normal).")

    def test_regressao_aponta_onde_piorou(self):
        marco = _retrato(date(2026, 11, 3), recuperavel=60.0, perda=20.0, vazamento=40.0)
        atual = _retrato(date(2026, 12, 1), recuperavel=90.0, perda=50.0, vazamento=40.0, pct=0.105)
        painel = evolucao.comparar([marco, atual], PRATA)
        economia = painel["economia"]
        self.assertEqual((economia["motivo"], economia["positiva"], economia["negativa"], economia["mensal_absoluto"],
                          economia["anual_absoluto"]), (None, False, True, 30.0, 360.0))
        # Com dois fechamentos, a base é o próprio Marco Zero.
        self.assertEqual([(r["rotulo"], r["antes"], r["depois"], r["contra_texto"]) for r in painel["regressoes"]],
                         [("Perdas nas vendas abaixo do custo", "R$ 20,00/mês", "R$ 50,00/mês", "desde o Marco Zero"),
                          ("Faturamento sem custo informado", "10%", "10,5%", "desde o Marco Zero")])
        self.assertIn("Atenção: perdas nas vendas abaixo do custo e faturamento sem custo informado pioraram desde o "
                      "Marco Zero.", painel["resumo"])
        self.assertIn("No ritmo atual, a loja deixa escapar R$ 360,00 a mais em 12 meses do que no Marco Zero.",
                      painel["resumo"])
        self.assertTrue(painel["resumo"].endswith("Para o próximo mês, o foco principal é estancar as vendas abaixo do "
                                                  "custo (R$ 50,00 por mês)."))
        self.assertNotIn("Excelente", painel["resumo"])

    def test_tres_retratos_marco_anterior_e_atual(self):
        marco = _retrato(date(2026, 10, 2), margem=0.30)
        anterior = _retrato(date(2026, 11, 1), margem=0.35)
        atual = _retrato(date(2026, 12, 1), margem=0.33)
        painel = evolucao.comparar([atual, marco, anterior], PRATA)
        self.assertEqual((painel["marco"]["data"], painel["anterior"]["data"], painel["atual"]["data"]),
                         ("02/10/2026", "01/11/2026", "01/12/2026"))
        margem = _por_id(painel["indicadores"], "margem")
        self.assertEqual((margem["vs_marco"], margem["vs_anterior"]), ("melhorou", "piorou"))
        # Margem em Alta pede o atual acima do anterior E do Marco Zero.
        self.assertFalse(_por_id(painel["selos"], "margem_em_alta")["conquistado"])
        self.assertEqual(painel["rotulo_anterior"], "mês anterior")
        self.assertEqual(painel["regressoes"], [{"rotulo": "Sobra bruta ajustada", "contra": "anterior",
                                                 "contra_texto": "desde o mês anterior", "antes": "35%",
                                                 "depois": "33%"}])
        self.assertIn("(mês anterior: 35%; Marco Zero: 30%)", _por_id(painel["selos"], "margem_em_alta")["texto"])
        self.assertEqual([linha["data"] for linha in painel["linha_do_tempo"]], ["01/12/2026", "01/11/2026",
                                                                                 "02/10/2026"])
        self.assertTrue(painel["linha_do_tempo"][-1]["marco_zero"])

    def test_raio_x_refeito_no_mesmo_mes_fica_o_mais_recente(self):
        primeiro = _retrato(date(2026, 11, 3), recuperavel=60.0)
        refeito = {**_retrato(date(2026, 11, 20), recuperavel=30.0), "competencia": "2026-11-01"}
        painel = evolucao.comparar([refeito, primeiro], PRATA)
        self.assertEqual((painel["estado"], painel["quantidade"], painel["marco"]["data"]), ("inicial", 1, "20/11/2026"))

    def test_selos_nas_bordas_das_regras(self):
        def selo(atual, marco=None, chave="cadastro_blindado"):
            painel = evolucao.comparar([marco or _retrato(date(2026, 11, 3)), atual], PRATA)
            return _por_id(painel["selos"], chave)["conquistado"]

        dezembro = date(2026, 12, 1)
        # Cadastro Blindado: abaixo de 2%, ou queda de MAIS de 50% desde o Marco Zero (10%), pelo número que a tela
        # mostra: 1,99% aparece como "2%" e não basta.
        tres = _retrato(date(2026, 11, 3), pct=0.03)   # Marco Zero de 3%: a queda até 2% não passa de 50%
        self.assertTrue(selo(_retrato(dezembro, pct=0.0194), tres))
        self.assertFalse(selo(_retrato(dezembro, pct=0.0199), tres))
        self.assertFalse(selo(_retrato(dezembro, pct=0.05)))     # queda de 50% exatos
        self.assertFalse(selo(_retrato(dezembro, pct=0.0499)))   # aparece como 5%: queda de 50%
        self.assertTrue(selo(_retrato(dezembro, pct=0.0494)))
        self.assertFalse(selo(_retrato(dezembro, pct=0.02), _retrato(date(2026, 11, 3), pct=0.02)))
        # Balcão Travado: queda de MAIS de 30% no vazamento por mês (Marco Zero: 40,00), como a tela mostra a queda.
        self.assertFalse(selo(_retrato(dezembro, vazamento=28.0), chave="balcao_travado"))   # 30% exatos
        self.assertFalse(selo(_retrato(dezembro, vazamento=27.99), chave="balcao_travado"))  # aparece como 30%
        self.assertTrue(selo(_retrato(dezembro, vazamento=27.9), chave="balcao_travado"))
        # Sem vazamento no Marco Zero e no último fechamento: o balcão já está travado; surgir vazamento não é.
        sem = _retrato(date(2026, 11, 3), vazamento=0.0)
        self.assertTrue(selo(_retrato(dezembro, vazamento=0.0), sem, "balcao_travado"))
        self.assertFalse(selo(_retrato(dezembro, vazamento=5.0), sem, "balcao_travado"))
        # Sem o preço de cada venda, não há como medir.
        self.assertFalse(selo(_retrato(dezembro, variacao=False), chave="balcao_travado"))
        # Prejuízo Zero: perda zerada, com venda com custo informado.
        self.assertTrue(selo(_retrato(dezembro, perda=0.0), chave="prejuizo_zero"))
        self.assertFalse(selo(_retrato(dezembro, perda=0.01), chave="prejuizo_zero"))
        self.assertFalse(selo(_retrato(dezembro, perda=0.0, venda_com_custo=0.0), chave="prejuizo_zero"))

    def test_plano_sem_margem_nao_ve_a_sobra_bruta(self):
        marco, atual = _retrato(date(2026, 11, 3), margem=0.2), _retrato(date(2026, 12, 1), margem=0.4)
        painel = evolucao.comparar([marco, atual], BRONZE)
        self.assertNotIn("margem", [i["id"] for i in painel["indicadores"]])
        self.assertFalse(painel["margem_liberada"])
        selo = _por_id(painel["selos"], "margem_em_alta")
        self.assertEqual((selo["bloqueado"], selo["conquistado"], selo["texto"]), (True, False, ""))
        self.assertNotIn("sobra", painel["resumo"])
        self.assertNotIn("40%", json.dumps(painel, ensure_ascii=False))

    def test_sem_periodo_nao_ha_economia_nem_projecao(self):
        marco = _retrato(date(2026, 11, 3), projetavel=False)
        atual = _retrato(date(2026, 12, 1), recuperavel=10.0)
        painel = evolucao.comparar([marco, atual], PRATA)
        self.assertEqual(painel["economia"], {"motivo": "periodo"})
        self.assertIn("Sem o período de algum dos Raio-X, não dá para projetar o ganho de 12 meses.", painel["resumo"])
        self.assertEqual(_por_id(painel["indicadores"], "perda")["vs_marco"], "sem_dados")
        # Com o total zero, o ritmo é zero mesmo sem o período.
        zerado = _retrato(date(2026, 11, 3), recuperavel=0.0, perda=0.0, vazamento=0.0, projetavel=False)
        painel = evolucao.comparar([zerado, atual], PRATA)
        self.assertEqual(painel["economia"]["mensal"], -10.0)

    def test_marco_zero_sem_base_nao_divide_por_zero(self):
        marco = _retrato(date(2026, 11, 3), recuperavel=0.0, perda=0.0, vazamento=0.0, pct=0.0, negativa=0)
        atual = _retrato(date(2026, 12, 1), recuperavel=15.0, perda=5.0, vazamento=10.0, pct=0.01, negativa=2)
        painel = evolucao.comparar([marco, atual], PRATA)
        perda = _por_id(painel["indicadores"], "perda")
        self.assertEqual((perda["vs_marco"], perda["variacao_marco"], perda["variacao_marco_texto"]),
                         ("piorou", {"tipo": "pct", "valor": None}, None))
        for numero in _numeros(painel):
            self.assertTrue(math.isfinite(numero), numero)

    def test_numeros_absurdos_nao_derrubam_a_comparacao(self):
        # Um retrato corrompido (1e308, um centavo de centavo, contagens gigantes) não derruba o painel nem gera
        # infinito: o número fora da escala de uma loja vale como sem número.
        marco = _retrato(date(2026, 11, 3), recuperavel=1e-300, vazamento=1e-300, pct=1e308, margem=-1e308)
        marco["itens"]["custo_a_confirmar"] = 10 ** 400
        atual = _retrato(date(2026, 12, 1), recuperavel=1e14, vazamento=1e14, pct=0.5, margem=1e300)
        for retratos in ([marco], [marco, atual], [atual, marco, _retrato(date(2027, 1, 4))]):
            with self.subTest(quantidade=len(retratos)):
                painel = evolucao.comparar(retratos, PRATA)
                json.dumps(painel, ensure_ascii=False, allow_nan=False)
                for numero in _numeros(painel):
                    self.assertTrue(math.isfinite(numero), numero)
        # Fora da escala de uma loja (acima de 10^15), o número vale como sem número: sem economia inventada.
        gigante = _retrato(date(2026, 11, 3), recuperavel=1e308, vazamento=1e16, pct=1e16)
        painel = evolucao.comparar([gigante, _retrato(date(2026, 12, 1), recuperavel=0.0)], PRATA)
        self.assertEqual(painel["economia"], {"motivo": "periodo"})
        self.assertEqual(_por_id(painel["indicadores"], "vazamento")["marco_texto"], "—")
        json.dumps(painel, ensure_ascii=False, allow_nan=False)
        for numero in _numeros(painel):
            self.assertTrue(math.isfinite(numero), numero)

    def test_linha_do_tempo_com_sanado_em_evolucao_e_pendente(self):
        marco = _retrato(date(2026, 10, 2), perda=20.0, alertas=2, vazamento=40.0, pct=0.1, estoque_negativo=3)
        meio = _retrato(date(2026, 11, 1), perda=10.0, alertas=2, vazamento=40.0, pct=0.1, estoque_negativo=1)
        atual = _retrato(date(2026, 12, 1), perda=0.0, alertas=0, vazamento=0.0, pct=0.0, estoque_negativo=0)
        sem_estoque = _retrato(date(2027, 1, 4), perda=0.0, alertas=0, vazamento=50.0, pct=0.0, estoque_negativo=0,
                               estoque=False, variacao=False)
        linhas = evolucao.comparar([marco, meio, atual, sem_estoque], PRATA)["linha_do_tempo"]
        status = {linha["data"]: [m["status"] for m in linha["metas"]] for linha in linhas}
        self.assertEqual(status, {
            "02/10/2026": ["partida", "partida", "partida"],
            "01/11/2026": ["evolucao", "pendente", "evolucao"],
            "01/12/2026": ["sanado", "sanado", "sanado"],
            # Sem o preço de cada venda, a Etapa 2 não se avalia; sem o estoque, a Etapa 3 não sai sanada.
            "04/01/2027": ["sanado", "sem_dados", "evolucao"],
        })
        self.assertEqual([m["rotulo"] for m in linhas[0]["metas"]], ["Sanado", "Sem dados", "Em evolução"])

    def test_foco_e_a_maior_pendencia(self):
        def foco(**campos):
            return evolucao.comparar([_retrato(date(2026, 11, 3), **campos)], PRATA)["foco"]

        self.assertEqual(foco(perda=50.0, vazamento=40.0), "estancar as vendas abaixo do custo (R$ 50,00 por mês)")
        self.assertEqual(foco(perda=0.0, vazamento=0.0, pct=0.123),
                         "cadastrar o custo dos produtos vendidos sem custo (12,3% da venda)")
        self.assertEqual(foco(perda=0.0, vazamento=0.0, pct=0.0, alertas=3),
                         "corrigir o custo dos 3 itens com alerta de cadastro")
        self.assertEqual(foco(perda=0.0, vazamento=0.0, pct=0.0, estoque_negativo=1),
                         "regularizar as notas do produto com estoque negativo")
        self.assertEqual(foco(perda=0.0, vazamento=0.0, pct=0.0, estoque_negativo=2),
                         "regularizar as notas dos 2 produtos com estoque negativo")
        self.assertEqual(foco(perda=0.0, vazamento=0.0, pct=0.0, alertas=1),
                         "corrigir o custo do item com alerta de cadastro")
        self.assertEqual(foco(perda=0.0, vazamento=0.0, pct=0.0), "manter o controle e refazer o Raio-X todo mês")
        # Sem o ritmo por mês, o total diz se há pendência.
        self.assertEqual(foco(perda=30.0, vazamento=0.0, projetavel=False), "estancar as vendas abaixo do custo")

    def test_cobertura_de_custo_pior_nao_vira_economia(self):
        # O último fechamento veio sem a coluna de custo num relatório e com mais venda sem custo: a perda escondida
        # não pode virar economia, selo nem "Sanado".
        marco = _retrato(date(2026, 11, 3), recuperavel=60.0, perda=20.0, pct=0.05, margem=0.2)
        atual = _retrato(date(2026, 12, 1), recuperavel=40.0, perda=0.0, pct=0.4, margem=0.5, sem_coluna=1, alertas=0)
        painel = evolucao.comparar([marco, atual], PRATA)
        self.assertEqual(painel["economia"], {"motivo": "cobertura"})
        self.assertEqual([(i["id"], i["vs_marco"]) for i in painel["indicadores"]],
                         [("perda", "nao_comparavel"), ("vazamento", "igual"), ("sem_custo", "piorou"),
                          ("cadastro", "nao_comparavel"), ("margem", "nao_comparavel")])
        self.assertFalse(_por_id(painel["selos"], "prejuizo_zero")["conquistado"])
        self.assertFalse(_por_id(painel["selos"], "margem_em_alta")["conquistado"])
        self.assertEqual(painel["linha_do_tempo"][0]["metas"][0]["status"], "sem_dados")
        self.assertIn("não dá para comparar o dinheiro que escapa por mês", painel["resumo"])
        self.assertNotIn("evolução —", painel["resumo"])
        # Com mais venda COM custo no último fechamento (o cadastro melhorou), a conta vale, com o aviso.
        painel = evolucao.comparar([_retrato(date(2026, 11, 3), pct=0.3, recuperavel=60.0),
                                    _retrato(date(2026, 12, 1), pct=0.0, recuperavel=50.0)], PRATA)
        self.assertEqual((painel["economia"]["motivo"], painel["economia"]["mensal"]), (None, 10.0))
        self.assertIn("perdas que antes não apareciam agora entram na conta", " ".join(painel["avisos"]))

    def test_numero_exibido_decide_melhorou_e_piorou(self):
        # Diferenças que a tela não mostra (uma casa em %) não viram "Melhorou", regressão nem selo.
        marco = _retrato(date(2026, 11, 3), pct=0.0612, margem=0.0871, vazamento=40.0)
        atual = _retrato(date(2026, 12, 1), pct=0.0608, margem=0.0874, vazamento=39.99)
        painel = evolucao.comparar([marco, atual], PRATA)
        sem_custo, margem = _por_id(painel["indicadores"], "sem_custo"), _por_id(painel["indicadores"], "margem")
        self.assertEqual((sem_custo["vs_marco"], sem_custo["variacao_marco_texto"]), ("igual", "sem mudança"))
        self.assertEqual((margem["vs_marco"], margem["variacao_marco_texto"]), ("igual", "sem mudança"))
        self.assertFalse(_por_id(painel["selos"], "margem_em_alta")["conquistado"])
        vazamento = _por_id(painel["indicadores"], "vazamento")
        self.assertEqual((vazamento["vs_marco"], vazamento["variacao_marco_texto"]),
                         ("melhorou", "menos de 0,1% a menos"))
        self.assertIn("você reduziu os vazamentos de preço.", painel["resumo"])
        self.assertNotIn("em 0%", painel["resumo"])

    def test_rotulo_do_fechamento_anterior(self):
        # Com fechamentos faltando, o "anterior" não é o mês anterior.
        retratos = [_retrato(date(2026, 1, 5), perda=10.0), _retrato(date(2026, 2, 2), perda=10.0),
                    _retrato(date(2026, 5, 4), perda=30.0)]
        painel = evolucao.comparar(retratos, PRATA)
        self.assertEqual(painel["rotulo_anterior"], "fechamento de fevereiro/2026")
        self.assertEqual(painel["regressoes"][0]["contra_texto"], "desde o fechamento de fevereiro/2026")
        painel = evolucao.comparar(retratos[::2], PRATA)
        self.assertIsNone(painel["rotulo_anterior"])
        self.assertEqual(painel["regressoes"][0]["contra_texto"], "desde o Marco Zero")
        self.assertNotIn("mês anterior", _por_id(painel["selos"], "margem_em_alta")["texto"])

    def test_regras_comparaveis(self):
        for versao, esperado in (("raio-x-2026-11.7", True), ("raio-x-2026-11.5", True), ("raio-x-2026-11.4", False),
                                 ("raio-x-2026-11.3", False), ("raio-x-2027-01.1", True), (None, False), ("x", False)):
            with self.subTest(versao=versao):
                self.assertEqual(evolucao.regras_comparaveis(versao), esperado)

    def test_comparacao_com_raio_x_de_verdade(self):
        marco = _retrato_do_motor(OUTUBRO, datetime(2026, 11, 3, 13, tzinfo=UTC))
        atual = _retrato_do_motor(NOVEMBRO, datetime(2026, 12, 1, 13, tzinfo=UTC))
        painel = evolucao.comparar([marco, atual], PRATA)
        self.assertEqual((painel["economia"]["mensal"], painel["economia"]["anual"], painel["conquistados"]),
                         (50.0, 600.0, 4))
        self.assertEqual([m["status"] for m in painel["linha_do_tempo"][0]["metas"]], ["sanado", "evolucao", "sanado"])

    def test_marco_zero_com_relatorio_sem_a_coluna(self):
        # Marco Zero com um relatório sem a coluna de custo (10% da venda sem custo) e fechamento completo com 15%: a
        # perda é medida em menos venda (a venda sem custo decide) e não vira economia, selo nem "Sanado".
        marco = _retrato(date(2026, 3, 2), recuperavel=60.0, perda=20.0, pct=0.10, sem_coluna=1, margem=0.222)
        atual = _retrato(date(2026, 4, 1), recuperavel=50.0, perda=0.0, pct=0.15, alertas=0, margem=0.275)
        painel = evolucao.comparar([marco, atual], PRATA)
        self.assertEqual(painel["economia"], {"motivo": "cobertura"})
        self.assertFalse(_por_id(painel["selos"], "prejuizo_zero")["conquistado"])
        self.assertFalse(_por_id(painel["selos"], "margem_em_alta")["conquistado"])
        self.assertNotIn("evolução —", painel["resumo"])
        # O último fechamento fica sem dados na Etapa 1; a linha do Marco Zero continua o ponto de partida.
        self.assertEqual([linha["metas"][0]["status"] for linha in painel["linha_do_tempo"]], ["sem_dados", "partida"])
        # Com menos venda sem custo no fechamento completo, a conta vale.
        melhor = _retrato(date(2026, 4, 1), recuperavel=50.0, perda=0.0, pct=0.02, alertas=0)
        self.assertEqual(evolucao.comparar([marco, melhor], PRATA)["economia"]["mensal"], 10.0)

    def test_fechamento_com_relatorio_sem_a_coluna(self):
        # Os dois com um relatório sem a coluna de custo: a perda do último não é medida inteira e não vira ganho.
        marco = _retrato(date(2026, 3, 2), recuperavel=60.0, perda=20.0, pct=0.1, sem_coluna=1)
        atual = _retrato(date(2026, 4, 1), recuperavel=40.0, perda=0.0, pct=0.1, sem_coluna=1, alertas=0)
        painel = evolucao.comparar([marco, atual], PRATA)
        self.assertEqual(painel["economia"], {"motivo": "cobertura"})
        self.assertFalse(_por_id(painel["selos"], "prejuizo_zero")["conquistado"])
        self.assertNotIn("zerou as vendas abaixo do custo", painel["resumo"])
        self.assertEqual([linha["metas"][0]["status"] for linha in painel["linha_do_tempo"]], ["sem_dados", "partida"])
        # O Marco Zero sem nenhuma venda com custo não mediu perda: a conta vale, mas o zero de um fechamento com um
        # relatório sem a coluna não é selo nem "Sanado", e o resumo não diz que a perda foi medida em menos venda.
        cego = _retrato(date(2026, 3, 2), recuperavel=40.0, perda=0.0, pct=1.0, venda_com_custo=0.0)
        painel = evolucao.comparar([cego, atual], PRATA)
        self.assertIsNone(painel["economia"]["motivo"])
        selo = _por_id(painel["selos"], "prejuizo_zero")
        self.assertFalse(selo["conquistado"])
        self.assertIn("um relatório veio sem a coluna de custo", selo["texto"])
        self.assertNotEqual(painel["linha_do_tempo"][0]["metas"][0]["status"], "sanado")
        self.assertIn("perdas que antes não apareciam agora entram na conta", " ".join(painel["avisos"]))
        self.assertNotIn("menos venda", painel["resumo"])

    def test_textos_de_cobertura_nomeiam_a_base(self):
        # A sobra bruta não se compara com o mês anterior (que tinha mais venda com custo): o selo diz qual base falhou.
        marco = _retrato(date(2026, 10, 2), pct=0.10, margem=0.20)
        anterior = _retrato(date(2026, 11, 2), pct=0.02, margem=0.25)
        atual = _retrato(date(2026, 12, 1), pct=0.05, margem=0.30)
        selo = _por_id(evolucao.comparar([marco, anterior, atual], PRATA)["selos"], "margem_em_alta")
        self.assertFalse(selo["conquistado"])
        self.assertIn("do que no mês anterior", selo["texto"])
        self.assertNotIn("Marco Zero", selo["texto"])
        # O aviso diz com quem a perda não se compara.
        pior = evolucao.comparar([_retrato(date(2026, 11, 3), pct=0.02), _retrato(date(2026, 12, 1), pct=0.08)], PRATA)
        self.assertIn("não são comparados com o Marco Zero", " ".join(pior["avisos"]))

    def test_venda_sem_custo_quase_zero_nao_e_zero(self):
        # 0,04% da venda sem custo aparece como "menos de 0,1%": nem "nenhuma venda", nem "Sanado", e o foco é cadastrar
        # o custo, como no Plano de Ação do mesmo Raio-X.
        marco = _retrato(date(2026, 11, 3), pct=0.061)
        atual = _retrato(date(2026, 12, 1), recuperavel=0.0, perda=0.0, vazamento=0.0, pct=0.0004, negativa=0,
                         alertas=0)
        painel = evolucao.comparar([marco, atual], PRATA)
        selo = _por_id(painel["selos"], "cadastro_blindado")
        self.assertEqual((selo["conquistado"], selo["texto"]),
                         (True, "Menos de 0,1% da venda saiu sem custo informado no último fechamento."))
        self.assertEqual(painel["linha_do_tempo"][0]["metas"][2]["status"], "evolucao")
        self.assertEqual(painel["foco"], "cadastrar o custo dos produtos vendidos sem custo (menos de 0,1% da venda)")
        sem_custo = _por_id(painel["indicadores"], "sem_custo")
        self.assertEqual((sem_custo["vs_marco"], sem_custo["atual_texto"]), ("melhorou", "menos de 0,1%"))
        # Zero gravado continua zero.
        zerado = _retrato(date(2026, 12, 1), recuperavel=0.0, perda=0.0, vazamento=0.0, pct=0.0, negativa=0, alertas=0)
        painel = evolucao.comparar([marco, zerado], PRATA)
        self.assertEqual(_por_id(painel["selos"], "cadastro_blindado")["texto"],
                         "Nenhuma venda saiu sem custo informado no último fechamento.")
        self.assertEqual(painel["linha_do_tempo"][0]["metas"][2]["status"], "sanado")

    def test_uma_casa_so_no_cartao_no_resumo_e_no_selo(self):
        # Vazamento de R$ 333,33 para R$ 233,16 (queda de 30,05%): o cartão, o resumo e o selo dizem 30,1%.
        painel = evolucao.comparar([_retrato(date(2026, 11, 3), vazamento=333.33),
                                    _retrato(date(2026, 12, 1), vazamento=233.16)], PRATA)
        self.assertEqual(_por_id(painel["indicadores"], "vazamento")["variacao_marco_texto"], "30,1% a menos")
        self.assertIn("reduziu os vazamentos de preço em 30,1%", painel["resumo"])
        selo = _por_id(painel["selos"], "balcao_travado")
        self.assertTrue(selo["conquistado"])
        self.assertIn("caiu 30,1%", selo["texto"])
        # Em qualquer par, o cartão e o selo dizem a mesma queda.
        for antes_centavos in range(1001, 500000, 7919):
            antes = Decimal(antes_centavos) / 100
            for fator in ("0.3", "0.30051", "0.4996", "0.68", "0.70049"):
                depois = (antes * (1 - Decimal(fator))).quantize(Decimal("0.01"))
                with self.subTest(antes=antes, depois=depois):
                    texto = evolucao._texto_da_variacao(evolucao._variacao(antes, depois, "brl_mes"))
                    self.assertEqual(texto, f"{evolucao._pct(evolucao._queda(antes, depois))} a menos")

    def test_mes_parcial_no_estado_inicial_e_na_comparacao(self):
        # Raio-X de meio mês: o estado inicial mostra o dinheiro por mês e o foco como a página do Raio-X e o Plano de
        # Ação (abaixo de 1 mês, o total); a comparação usa o ritmo real, com o aviso.
        meio = _retrato(date(2026, 3, 2), recuperavel=120.0, perda=40.0, vazamento=80.0, meses=0.5)
        meio["total"].update({"recuperavel": 60.0, "perda": 20.0, "vazamento": 40.0})
        painel = evolucao.comparar([meio], PRATA)
        self.assertEqual((painel["marco"]["recuperavel_mensal_plano"], painel["marco"]["recuperavel_mensal"]),
                         (60.0, 120.0))
        self.assertEqual(painel["foco"],
                         "travar os descontos no balcão (R$ 40,00 por mês em vendas abaixo do preço normal)")
        self.assertEqual(painel["avisos"], ["O Marco Zero cobre menos de 1 mês: na comparação com o próximo fechamento, "
                                            "o ritmo por mês dele será estimado pelos dias cobertos."])
        painel = evolucao.comparar([meio, _retrato(date(2026, 4, 1))], PRATA)
        self.assertEqual(painel["economia"]["marco_mensal"], 120.0)
        self.assertTrue(painel["marco"]["parcial"])
        # Com 1 mês ou mais, mas menos de 1 mês de relatórios com a coluna de custo, o aviso é o da perda.
        auditado = _retrato(date(2026, 3, 2), meses=1.5)
        auditado["periodo"]["meses_auditados"] = 0.5
        avisos = evolucao.comparar([auditado, _retrato(date(2026, 4, 1))], PRATA)["avisos"]
        self.assertIn("O Marco Zero tem menos de 1 mês de relatórios com a coluna de custo: a perda por mês dele é "
                      "estimada pelos dias cobertos.", avisos)
        # O mesmo do lado do último fechamento.
        atual = _retrato(date(2026, 4, 1))
        atual["periodo"]["meses_auditados"] = 0.5
        self.assertIn("O último fechamento tem menos de 1 mês de relatórios com a coluna de custo: a perda por mês dele "
                      "é estimada pelos dias cobertos.",
                      evolucao.comparar([_retrato(date(2026, 3, 2)), atual], PRATA)["avisos"])
        # Estado inicial com 1,5 mês e meio mês de relatórios com a coluna de custo: a perda do foco é a da Auditoria
        # (pelos meses auditados, com o piso de 1 mês: o total), e o aviso fala da comparação que vem.
        inicial = _retrato(date(2026, 3, 2), recuperavel=50.0, perda=40.0, vazamento=10.0, meses=1.5)
        inicial["periodo"]["meses_auditados"] = 0.5
        inicial["total"]["perda"] = 20.0
        painel = evolucao.comparar([inicial], PRATA)
        self.assertEqual(painel["foco"], "estancar as vendas abaixo do custo (R$ 20,00 por mês)")
        self.assertEqual(painel["avisos"], ["O Marco Zero tem menos de 1 mês de relatórios com a coluna de custo: na "
                                            "comparação com o próximo fechamento, a perda por mês dele será estimada "
                                            "pelos dias cobertos."])

    def test_nenhum_dos_dois_com_venda_com_custo(self):
        # Nem o Marco Zero nem o último fechamento mediram perda: a base de custo não mudou, e não há aviso.
        marco = _retrato(date(2026, 3, 2), recuperavel=30.0, perda=0.0, vazamento=30.0, pct=1.0, venda_com_custo=0.0)
        atual = _retrato(date(2026, 4, 1), recuperavel=20.0, perda=0.0, vazamento=20.0, pct=1.0, venda_com_custo=0.0)
        self.assertEqual(evolucao._comparacao_de_custo(marco, atual), "igual")
        painel = evolucao.comparar([marco, atual], PRATA)
        self.assertEqual((painel["economia"]["mensal"], painel["avisos"], painel["causa_cobertura"]), (10.0, [], None))

    def test_relatorio_sem_a_coluna_com_menos_venda_sem_custo(self):
        # Os dois com um relatório sem a coluna de custo e a venda sem custo em queda: a perda não se compara (falta a
        # coluna num relatório do último fechamento), mas nenhum texto diz "mais venda sem custo" nem "menos venda".
        marco = _retrato(date(2026, 3, 2), recuperavel=60.0, perda=20.0, pct=0.143, sem_coluna=1, margem=0.2)
        atual = _retrato(date(2026, 4, 1), recuperavel=40.0, perda=0.0, pct=0.053, sem_coluna=1, margem=0.3, alertas=0)
        painel = evolucao.comparar([marco, atual], PRATA)
        self.assertEqual((painel["economia"], painel["causa_cobertura"]), ({"motivo": "cobertura"}, "sem_coluna"))
        self.assertEqual(_por_id(painel["indicadores"], "sem_custo")["vs_marco"], "melhorou")
        textos = {"resumo": painel["resumo"], "avisos": " ".join(painel["avisos"]),
                  "prejuizo_zero": _por_id(painel["selos"], "prejuizo_zero")["texto"],
                  "margem_em_alta": _por_id(painel["selos"], "margem_em_alta")["texto"]}
        for onde, texto in textos.items():
            with self.subTest(onde=onde):
                self.assertIn("um relatório veio sem a coluna de custo", texto)
                self.assertNotIn("menos venda", texto)
                self.assertNotIn("mais venda", texto)
        self.assertFalse(_por_id(painel["selos"], "margem_em_alta")["conquistado"])
        # Com mais de 1 ponto a mais de venda sem custo, a causa é a venda sem custo; comparável, não há causa.
        pior = _retrato(date(2026, 4, 1), recuperavel=40.0, perda=0.0, pct=0.30, sem_coluna=1, alertas=0)
        painel = evolucao.comparar([marco, pior], PRATA)
        self.assertEqual(painel["causa_cobertura"], "mais_sem_custo")
        self.assertIn("mais venda sem custo informado", " ".join(painel["avisos"]))
        self.assertIsNone(evolucao.comparar([marco, _retrato(date(2026, 4, 1), pct=0.143)], PRATA)["causa_cobertura"])
        sem_custo = _retrato(date(2026, 4, 1), pct=1.0, venda_com_custo=0.0)
        self.assertEqual(evolucao.comparar([marco, sem_custo], PRATA)["causa_cobertura"], "sem_custo")
        # O ritmo que arredonda a R$ 0,00 (um centavo em vários meses) não aparece como zero no selo.
        centavo = _retrato(date(2026, 4, 1), perda=0.0)
        centavo["total"]["perda"] = 0.01
        selo = _por_id(evolucao.comparar([_retrato(date(2026, 3, 2)), centavo], PRATA)["selos"], "prejuizo_zero")
        self.assertEqual((selo["conquistado"], selo["texto"]),
                         (False, "Ainda há R$ 0,01 no período em vendas abaixo do custo: corrija o preço desses "
                                 "produtos."))

    def test_venda_sem_custo_que_o_percentual_gravado_nao_mostra(self):
        # R$ 4,00 sem custo em R$ 100.004,00: o motor grava 0,0000 (4 casas), mas o retrato guarda a venda sem custo em
        # reais, e o painel diz o mesmo que o Plano de Ação do mesmo Raio-X (ainda há produto a cadastrar).
        grande = (_CAB.format("01/11/2026 a 30/11/2026") + "1;Cafe 500g;5000;20,00;100000,00;75000,00;5\n"
                  "3;Acucar 1kg;1;4,00;4,00;;3\n").encode("utf-8")
        atual = _retrato_do_motor(grande, datetime(2026, 12, 1, 13, tzinfo=UTC))
        self.assertEqual((atual["pct_sem_custo"], atual["total"]["venda_sem_custo"], atual["etapas"]["controle"]),
                         (0.0, 4.0, "pendente"))
        painel = evolucao.comparar([_retrato_do_motor(OUTUBRO, datetime(2026, 11, 3, 13, tzinfo=UTC)), atual], PRATA)
        self.assertEqual(_por_id(painel["selos"], "cadastro_blindado")["texto"],
                         "Menos de 0,1% da venda saiu sem custo informado no último fechamento.")
        self.assertEqual(_por_id(painel["indicadores"], "sem_custo")["atual_texto"], "menos de 0,1%")
        self.assertEqual(painel["linha_do_tempo"][0]["metas"][2]["status"], "evolucao")
        self.assertEqual(painel["foco"], "cadastrar o custo dos produtos vendidos sem custo (menos de 0,1% da venda)")
        # O retrato de antes do campo (sem a venda sem custo em reais) segue pelo percentual gravado.
        antigo = {**atual, "total": {k: v for k, v in atual["total"].items() if k != "venda_sem_custo"}}
        selo = _por_id(evolucao.comparar([_retrato(date(2026, 11, 3)), antigo], PRATA)["selos"], "cadastro_blindado")
        self.assertEqual(selo["texto"], "Nenhuma venda saiu sem custo informado no último fechamento.")


# =====================================================================
# Gravação e telas (app com as migrações reais em SQLite)
# =====================================================================
# O relógio dos testes começa em 02/03/2026 (tests.contratos_support.T0): o Marco Zero leva fevereiro; o fechamento
# de 01/04/2026, março.
FEV_RUIM = OUTUBRO.replace(b"01/10/2026 a 31/10/2026", b"01/02/2026 a 28/02/2026")
MAR_BOM = NOVEMBRO.replace(b"01/11/2026 a 30/11/2026", b"01/03/2026 a 31/03/2026")
ABRIL = datetime(2026, 4, 1, 13, tzinfo=UTC)


class _EvolucaoCase(planos._PlanosCase):
    def _html(self, caminho):
        resposta = self.cliente_http.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        return resposta.get_data(as_text=True)

    def _retratos(self, id_empresa=None):
        from models import DiagnosticoFechamento
        self.db.session.expire_all()
        return self.db.session.scalars(select(DiagnosticoFechamento)
                                       .where(DiagnosticoFechamento.id_empresa == (id_empresa or self.empresa_id))
                                       .order_by(DiagnosticoFechamento.competencia)).all()

    def _marco_e_fechamento(self):
        self.assertEqual(self._enviar_raio_x(("fev.csv", FEV_RUIM)).status_code, 303)
        self.relogio(ABRIL)
        self.assertEqual(self._enviar_raio_x(("mar.csv", MAR_BOM)).status_code, 303)

    def _migrar(self, comando, revisao):
        import contextlib
        import io
        import flask_migrate
        from tests.deploy_support import ROOT
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch("logging.config.fileConfig"):
            getattr(flask_migrate, comando)(directory=str(ROOT / "migrations"), revision=revisao)

    @staticmethod
    def _modal(html):
        return html.split('id="planoAcao"', 1)[1]


class GravacaoTests(_EvolucaoCase):
    def test_cada_raio_x_grava_o_retrato_do_mes(self):
        self.assertEqual(self._enviar_raio_x(("fev.csv", FEV_RUIM)).status_code, 303)
        retratos = self._retratos()
        self.assertEqual([(r.competencia, r.versao_regras) for r in retratos], [(date(2026, 3, 1), "raio-x-2026-11.7")])
        dados = json.loads(retratos[0].indicadores)
        self.assertEqual((dados["mensal"]["recuperavel"], dados["data"]), (60.0, "2026-03-02"))
        for produto in ("Cafe", "Leite", "Acucar"):
            self.assertNotIn(produto, retratos[0].indicadores)
        self.relogio(ABRIL)
        self._enviar_raio_x(("mar.csv", MAR_BOM))
        self.assertEqual([r.competencia for r in self._retratos()], [date(2026, 3, 1), date(2026, 4, 1)])

    def test_raio_x_refeito_no_mesmo_mes_substitui_o_retrato(self):
        self._enviar_raio_x(("fev.csv", FEV_RUIM))
        self.relogio(datetime(2026, 3, 20, 13, tzinfo=UTC))
        self._enviar_raio_x(("mar.csv", MAR_BOM))
        retratos = self._retratos()
        self.assertEqual(len(retratos), 1)
        self.assertEqual(json.loads(retratos[0].indicadores)["data"], "2026-03-20")

    def test_marco_zero_do_raio_x_feito_antes_da_etapa_3(self):
        # O Raio-X de março foi feito sem a tabela (antes da migração): o fechamento de abril grava o retrato dele
        # (o Marco Zero) e o seu.
        with mock.patch("raio_x_servico.fechamentos_disponiveis", return_value=False):
            self._enviar_raio_x(("fev.csv", FEV_RUIM))
            self.assertEqual(self._retratos(), [])
            # Sem a tabela, o painel mostra o estado inicial com o Raio-X atual, sem gravar nada.
            self.assertIn("Seu Marco Zero está registrado", self._html(DASHBOARD))
        self.relogio(ABRIL)
        self._enviar_raio_x(("mar.csv", MAR_BOM))
        retratos = self._retratos()
        self.assertEqual([r.competencia for r in retratos], [date(2026, 3, 1), date(2026, 4, 1)])
        self.assertEqual(json.loads(retratos[0].indicadores)["data"], "2026-03-02")

    def test_sem_a_tabela_o_raio_x_grava_igual(self):
        # A migração ainda não foi aplicada no banco, com o processo no ar (o cache dizia que a tabela existia).
        self.assertEqual(self._html(DASHBOARD).count("Evolução &amp; Conquistas"), 0)
        self.assertTrue(raio_x_servico.fechamentos_disponiveis())
        self._migrar("downgrade", "c5f8a1d3e7b9")
        self.assertEqual(self._enviar_raio_x(("fev.csv", FEV_RUIM)).status_code, 303)
        self.assertIsNotNone(self._linha())
        html = self._html(DASHBOARD)
        self.assertIn("Seu Marco Zero está registrado", html)
        self.assertEqual(self._html(HISTORICO).count("Seu Marco Zero está registrado"), 1)
        self.assertIn("Evolução &amp; Conquistas", self._modal(self._html(PAGINA)))
        self._migrar("upgrade", "head")
        raio_x_servico.esquecer_fechamentos()

    def test_erro_no_retrato_nao_derruba_o_raio_x(self):
        with mock.patch("raio_x_servico._gravar_retrato", side_effect=SQLAlchemyError("falha simulada")):
            self.assertEqual(self._enviar_raio_x(("fev.csv", FEV_RUIM)).status_code, 303)
        self.assertIsNotNone(self._linha())
        self.assertEqual(self._retratos(), [])
        with mock.patch("evolucao.retrato", side_effect=KeyError("saude")):
            self.relogio(ABRIL)
            self.assertEqual(self._enviar_raio_x(("mar.csv", MAR_BOM)).status_code, 303)
        self.assertEqual(json.loads(self._linha().resultado)["periodo"]["inicio"], "2026-03-01")
        self.assertEqual(self._retratos(), [])

    def test_downgrade_recusa_apagar_retratos(self):
        self._enviar_raio_x(("fev.csv", FEV_RUIM))
        with self.assertLogs("flask_migrate", "ERROR") as registro, self.assertRaises(SystemExit):
            self._migrar("downgrade", "c5f8a1d3e7b9")
        self.assertIn("Downgrade da evolução mensal recusado", registro.output[0])
        self.assertEqual(len(self._retratos()), 1)

    def test_raio_x_com_regras_antigas_nao_vira_marco_zero(self):
        # Um Raio-X gravado com a raio-x-2026-11.4 (o custo implausível ainda dentro da perda), antes da Etapa 3: o
        # Raio-X seguinte não o usa como Marco Zero, e o painel não o mostra sozinho.
        with mock.patch("raio_x_servico.fechamentos_disponiveis", return_value=False):
            self._enviar_raio_x(("fev.csv", FEV_RUIM))
        linha = self._linha()
        dados = json.loads(linha.resultado)
        dados["versao"] = "raio-x-2026-11.4"
        linha.resultado = json.dumps(dados, ensure_ascii=False)
        self.db.session.commit()
        self.assertNotIn("data-evolucao=", self._html(DASHBOARD))
        self.relogio(ABRIL)
        self._enviar_raio_x(("mar.csv", MAR_BOM))
        self.assertEqual([r.competencia for r in self._retratos()], [date(2026, 4, 1)])

    def test_retratos_de_outra_empresa_nao_entram(self):
        self._marco_e_fechamento()
        from models import DiagnosticoFechamento
        self.assertEqual(self.db.session.scalar(select(DiagnosticoFechamento.id_fechamento)
                                                .where(DiagnosticoFechamento.id_empresa == self.outra_empresa_id)),
                         None)
        self.assertEqual(raio_x_servico.retratos_da_empresa(self.outra_empresa_id), [])


class TelasEvolucaoTests(_EvolucaoCase):
    def test_sem_raio_x_nao_ha_painel(self):
        self._mudar_plano("PRATA")
        self.assertNotIn("Evolução &amp; Conquistas", self._html(DASHBOARD))
        self.assertNotIn("Evolução mês a mês", self._html(HISTORICO))

    def test_um_raio_x_mostra_o_estado_inicial(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("fev.csv", FEV_RUIM))
        mensagem = ("A comparação de evolução será ativada automaticamente no próximo fechamento mensal (todo dia 1º): "
                    "é só enviar o relatório de vendas do mês no Raio-X do PDV.")
        for caminho in (DASHBOARD, HISTORICO):
            with self.subTest(caminho=caminho):
                html = self._html(caminho)
                self.assertIn("Seu Marco Zero está registrado", html)
                self.assertIn(mensagem, html)
                self.assertIn("Raio-X de 02/03/2026 · vendas: 1 mês (01/02/2026 a 28/02/2026)", html)
                self.assertNotIn("Economia Gerada no Mês", html)
        modal = self._modal(self._html(PAGINA))
        self.assertIn('id="evolucaoTituloPlano"', modal)
        self.assertIn("Seu Marco Zero está registrado", modal)

    def test_fechamento_seguinte_compara_com_o_marco_zero(self):
        self._mudar_plano("PRATA")
        self._marco_e_fechamento()
        html = self._html(DASHBOARD)
        painel = html.split('id="evolucaoTituloVisao"', 1)[1]
        self.assertIn("Marco Zero: Raio-X de 02/03/2026 · último fechamento: Raio-X de 01/04/2026", html)
        self.assertIn("Economia Gerada no Mês", painel)
        self.assertIn("<small>R$</small> 50,00", painel)
        self.assertIn("Projeção Anual no Ritmo Atual", painel)
        self.assertIn("<small>R$</small> 600,00", painel)
        self.assertEqual(painel.count(">Conquistado<"), 4)
        self.assertIn("Comparado ao seu Raio-X inicial de 02/03/2026, você zerou as vendas abaixo do custo", painel)
        self.assertIn("Mantendo esse ritmo, seu ganho projetado em 12 meses é de R$ 600,00.", painel)
        self.assertIn(f'href="{HISTORICO}#evolucaoLinhaDoTempo"', painel)
        # Histórico: a linha do tempo mês a mês, do mais recente ao Marco Zero.
        historico = self._html(HISTORICO)
        linha = historico.split('id="evolucaoLinhaDoTempo"', 1)[1]
        self.assertLess(linha.index("Abril/2026"), linha.index("Março/2026"))
        self.assertIn('<span class="nexo-badge b-purple">Marco Zero</span>', linha)
        self.assertEqual(linha.count(">Sanado<"), 2 + 1)   # duas metas + a legenda
        self.assertIn(">Em evolução<", linha)
        # O Plano de Ação mostra o mesmo painel, no topo do modal (nas duas páginas).
        for caminho in (PAGINA, AUDITORIA):
            with self.subTest(caminho=caminho):
                modal = self._modal(self._html(caminho))
                self.assertLess(modal.index('id="evolucaoTituloPlano"'), modal.index('id="planoBloco1"'))
                self.assertIn("<small>R$</small> 600,00", modal)

    def test_bronze_nao_ve_a_sobra_bruta(self):
        self._mudar_plano("BRONZE")
        self._marco_e_fechamento()
        painel = self._html(DASHBOARD).split('id="evolucaoTituloVisao"', 1)[1]
        self.assertIn("A sobra bruta aparece a partir do plano Prata.", painel)
        self.assertNotIn("21,4%", painel)
        self.assertNotIn("8,7%", painel)
        self.assertEqual(painel.count(">Conquistado<"), 3)
        self.assertIn(">Bloqueado<", painel)

    def test_regressao_aparece_no_card(self):
        self._mudar_plano("PRATA")
        # Marco Zero bom (fevereiro), fechamento ruim (março), com a mesma base de custo.
        bom = MAR_BOM.replace(b"01/03/2026 a 31/03/2026", b"01/02/2026 a 28/02/2026")
        ruim = MAR_BOM.replace(b"01/03/2026 a 31/03/2026", b"01/03/2026 a 31/03/2026").replace(
            b"2;Leite 1L;20;7,00;140,00;120,00;4", b"2;Leite 1L;20;5,00;100,00;120,00;4")
        self.assertEqual(self._enviar_raio_x(("fev.csv", bom)).status_code, 303)
        self.relogio(ABRIL)
        self.assertEqual(self._enviar_raio_x(("mar.csv", ruim)).status_code, 303)
        painel = self._html(DASHBOARD).split('id="evolucaoTituloVisao"', 1)[1]
        self.assertIn("Onde houve regressão", painel)
        self.assertIn("<strong>Perdas nas vendas abaixo do custo</strong>: de R$ 0,00/mês para R$ 20,00/mês desde o "
                      "Marco Zero.", painel)
        self.assertIn("a mais escapando por mês do que no Marco Zero", painel)
        self.assertNotIn("Excelente evolução", painel)

    def test_fechamento_sem_a_coluna_de_custo_nao_vira_economia(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("fev.csv", FEV_RUIM))
        self.relogio(ABRIL)
        sem_custo = ("Relatorio de vendas por produto\nPeriodo: 01/03/2026 a 31/03/2026\n"
                     "Codigo;Produto;Qtde;Preco Unitario;Valor Total\n1;Cafe 500g;10;20,00;200,00\n"
                     "1;Cafe 500g;10;20,00;160,00\n2;Leite 1L;20;5,00;100,00\n3;Acucar 1kg;5;6,00;30,00\n").encode()
        self._enviar_raio_x(("mar.csv", sem_custo))
        painel = self._html(DASHBOARD).split('id="evolucaoTituloVisao"', 1)[1]
        self.assertIn("Sem conta de economia: o último fechamento mede a perda em menos venda que o Marco Zero", painel)
        self.assertNotIn("de ganho em 12 meses", painel)
        self.assertNotIn("Boa evolução", painel)
        self.assertNotIn("Excelente evolução", painel)

    def test_linha_do_tempo_usa_celulas_da_tabela(self):
        self._mudar_plano("PRATA")
        self._marco_e_fechamento()
        linha = self._html(HISTORICO).split('id="evolucaoLinhaDoTempo"', 1)[1]
        self.assertIn('<td class="evolucao-fechamento">', linha)
        self.assertNotIn('<th scope="row"', linha)

    def test_retrato_ilegivel_nao_derruba_as_paginas(self):
        self._mudar_plano("PRATA")
        self._marco_e_fechamento()
        self.db.session.execute(text("UPDATE diagnostico_fechamento SET indicadores = '{lixo' "
                                     "WHERE competencia = '2026-03-01'"))
        self.db.session.commit()
        for caminho in (DASHBOARD, HISTORICO, PAGINA, AUDITORIA):
            with self.subTest(caminho=caminho):
                self.assertEqual(self.cliente_http.get(caminho).status_code, 200)
        # Sem o retrato de março, o fechamento de abril vira o Marco Zero (estado inicial).
        self.assertIn("Seu Marco Zero está registrado", self._html(DASHBOARD))

    def test_falha_de_banco_no_painel_nao_derruba_a_visao_geral(self):
        self._mudar_plano("PRATA")
        self._marco_e_fechamento()
        with mock.patch("raio_x_servico.retratos_da_empresa", side_effect=SQLAlchemyError("falha simulada")):
            for caminho in (DASHBOARD, HISTORICO, PAGINA):
                with self.subTest(caminho=caminho):
                    resposta = self.cliente_http.get(caminho)
                    self.assertEqual(resposta.status_code, 200)
                    self.assertNotIn("data-evolucao=", resposta.get_data(as_text=True))
                    self.assertNotIn('id="evolucaoLinhaDoTempo"', resposta.get_data(as_text=True))

    def test_mes_parcial_mostra_o_numero_do_plano_e_marca_o_estimado(self):
        self._mudar_plano("PRATA")
        quinzena = FEV_RUIM.replace(b"01/02/2026 a 28/02/2026", b"01/02/2026 a 14/02/2026")
        self.assertEqual(self._enviar_raio_x(("fev.csv", quinzena)).status_code, 303)
        # Um Raio-X só: o mesmo dinheiro por mês do Plano de Ação, no mesmo modal (abaixo de 1 mês, o total).
        modal = self._modal(self._html(PAGINA))
        self.assertIn("Hoje, a loja deixa escapar <strong>R$ 60,00 por mês</strong>", modal)
        self.assertIn("R$ 60,00 por mês, no ritmo do período analisado", modal)
        self.relogio(ABRIL)
        self.assertEqual(self._enviar_raio_x(("mar.csv", MAR_BOM)).status_code, 303)
        painel = self._html(DASHBOARD).split('id="evolucaoTituloVisao"', 1)[1]
        self.assertIn("caiu de R$ 120,00 (estimado) para R$ 10,00 por mês.", painel)
        self.assertIn("O Marco Zero cobre menos de 1 mês: o ritmo por mês dele é estimado pelos dias cobertos.", painel)

    def test_lado_estimado_no_card_da_economia(self):
        # O lado de menos de 1 mês leva "(estimado)" no texto da economia, nos três desfechos e dos dois lados.
        def cartao(marco, atual):
            painel = evolucao.comparar([marco, atual], PRATA)
            with self.app.test_request_context("/"):
                return str(self.app.jinja_env.get_template("_evolucao.html").module.painel(painel))

        def meio_mes(dia, recuperavel):
            return _retrato(dia, recuperavel=recuperavel, meses=0.5)

        marco, abril = _retrato(date(2026, 3, 2), recuperavel=60.0), date(2026, 4, 1)
        self.assertIn("caiu de R$ 60,00 para R$ 20,00 (estimado) por mês.", cartao(marco, meio_mes(abril, 20.0)))
        self.assertIn("subiu de R$ 60,00 para R$ 90,00 (estimado) por mês.", cartao(marco, meio_mes(abril, 90.0)))
        self.assertIn("(R$ 60,00 por mês, estimado)", cartao(marco, meio_mes(abril, 60.0)))
        self.assertIn("subiu de R$ 60,00 (estimado) para R$ 90,00 por mês.",
                      cartao(meio_mes(date(2026, 3, 2), 60.0), _retrato(abril, recuperavel=90.0)))
        # Sem lado de menos de 1 mês, nada é marcado.
        self.assertNotIn("estimado", cartao(marco, _retrato(abril, recuperavel=20.0)))

    def test_relatorio_sem_a_coluna_nos_dois_tem_texto_proprio(self):
        # A loja que sempre manda um relatório sem a coluna de custo: o painel diz que falta a coluna, e não que há
        # mais venda sem custo (ela caiu).
        self._mudar_plano("PRATA")

        def janeiro_sem_coluna(preco, total):
            return ("Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 31/01/2026\n"
                    f"Codigo;Produto;Qtde;Preco Unitario;Valor Total\n9;Sal 1kg;10;{preco};{total}\n").encode("utf-8")

        self.assertEqual(self._enviar_raio_x(("jan.csv", janeiro_sem_coluna("9,00", "90,00")),
                                             ("fev.csv", FEV_RUIM)).status_code, 303)
        self.relogio(ABRIL)
        self.assertEqual(self._enviar_raio_x(("jan.csv", janeiro_sem_coluna("3,00", "30,00")),
                                             ("fev.csv", FEV_RUIM)).status_code, 303)
        painel = self._html(DASHBOARD).split('id="evolucaoTituloVisao"', 1)[1]
        self.assertIn("Sem conta de economia: no último fechamento, um relatório veio sem a coluna de custo", painel)
        for trecho in ("mede a perda em menos venda", "mais venda sem custo informado", "mais venda saiu sem custo"):
            with self.subTest(trecho=trecho):
                self.assertNotIn(trecho, painel)
