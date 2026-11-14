"""Raio-X do PDV e Auditoria de custos para quem não é do ramo (14/11/2026).

Executar da raiz: python -B -m unittest tests.test_raio_x_apresentacao -v
Sem rede; o Raio-X sai do motor de verdade no próprio processo (tests.raio_x_support), e as telas
saem do app com as migrações reais em SQLite (o caso base de tests.test_planos_raio_x).

  - A margem e o custo dos cards "Sobra bruta nas vendas" e "Custo dos produtos vendidos" sem os itens de
    custo a confirmar (raio_x_servico.margem_sem_custo_a_confirmar, custo_sem_custo_a_confirmar e os campos
    "sem_a_confirmar" da visão).
  - A ordem de impacto, as marcas do filtro rápido e o texto da busca das tabelas
    (raio_x_apresentacao), sem nada do que o plano esconde.
  - O resumo de uma linha dos arquivos e o card dos avisos, recolhido.
  - As telas: o botão (i) em cada card, a barra e o rodapé das tabelas escondidos até o script.
  - A Auditoria da Etapa 1 (14/11/2026): os textos do dono, o (i) curto, os alertas de cadastro recolhidos, a
    tabela de lucro bruto (sem os itens a confirmar, pelo maior lucro, com as faixas de margem) e a faixa positiva.
"""

import copy
import json
from pathlib import Path
import re
import tempfile
import unittest

import raio_x
import raio_x_apresentacao as ap
import raio_x_servico
from tests.raio_x_support import FEVEREIRO, JANEIRO, executar_como_o_worker, relatorio_do_mes
# Pelo módulo, e não pelo nome: a classe importada no módulo seria coletada (e rodada) de novo aqui.
import tests.test_planos_raio_x as planos

PAGINA, AUDITORIA = planos.PAGINA, planos.AUDITORIA
CUSTO_ABSURDO = planos.MotorRaioXTests.CUSTO_ABSURDO

PRATA = frozenset({"raio_x_entrada", "auditoria_abaixo_custo", "margem_bruta", "diagnostico_cmv",
                   "alerta_precificacao", "curva_abc_vendas", "produtos_parados_ruptura"})
BRONZE = frozenset({"raio_x_entrada", "curva_abc_vendas", "produtos_parados_ruptura"})


def _diagnosticar(*conteudos):
    with tempfile.TemporaryDirectory(prefix="raio-x-apresentacao-") as pasta:
        caminhos = []
        for numero, conteudo in enumerate(conteudos, 1):
            caminho = Path(pasta) / f"arquivo_{numero:02d}.csv"
            caminho.write_bytes(conteudo)
            caminhos.append(str(caminho))
        return executar_como_o_worker(caminhos)


# =====================================================================
# Margem sem os itens de custo a confirmar
# =====================================================================
class MargemSemCustoAConfirmarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Café 200/150, Leite 100/110 e o Item Genérico 500/40.000 (custo 80 vezes a venda: a confirmar).
        cls.absurdo = _diagnosticar(CUSTO_ABSURDO)

    def test_tira_a_venda_e_o_custo_dos_itens_a_confirmar(self):
        auditoria = self.absurdo["auditoria"]
        # A margem de sempre, com o item a confirmar, despenca para -4.932,5%.
        self.assertEqual((auditoria["margem_valor"], auditoria["margem_percentual"]), (-39460.0, -49.325))
        limpa = raio_x_servico.margem_sem_custo_a_confirmar(auditoria)
        # Sem ele: (200 + 100) - (150 + 110) = 40 sobre 300.
        self.assertEqual(limpa, {"valor": 40.0, "percentual": 0.1333, "base": 300.0, "itens_fora": 1})

    def test_sem_item_a_confirmar_e_a_margem_de_sempre(self):
        auditoria = _diagnosticar(JANEIRO, FEVEREIRO)["auditoria"]
        limpa = raio_x_servico.margem_sem_custo_a_confirmar(auditoria)
        self.assertEqual((limpa["valor"], limpa["percentual"], limpa["itens_fora"]),
                         (auditoria["margem_valor"], auditoria["margem_percentual"], 0))
        self.assertEqual(limpa["base"], auditoria["faturamento_com_custo"])

    def test_conta_em_centavos_inteiros(self):
        # 0,10 + 0,20 em ponto flutuante dá 0,30000000000000004: a conta em centavos não carrega o resto.
        auditoria = {"faturamento_com_custo": 0.3, "custo_total": 0.1 + 0.2 - 0.1, "margem_valor": 0.1,
                     "custo_a_confirmar": {"itens": 0}}
        self.assertEqual(raio_x_servico.margem_sem_custo_a_confirmar(auditoria)["valor"], 0.1)

    def test_todo_o_custo_a_confirmar_nao_inventa_margem(self):
        auditoria = {"faturamento_com_custo": 500.0, "custo_total": 40000.0, "margem_valor": -39500.0,
                     "custo_a_confirmar": {"itens": 1, "faturamento_com_custo": 500.0, "custo_total": 40000.0}}
        self.assertEqual(raio_x_servico.margem_sem_custo_a_confirmar(auditoria),
                         {"valor": None, "percentual": None, "base": 0.0, "itens_fora": 1})

    def test_raio_x_antigo_sem_o_bloco_e_sem_custo(self):
        self.assertEqual(raio_x_servico.margem_sem_custo_a_confirmar(
            {"faturamento_com_custo": 100.0, "custo_total": 60.0, "custo_a_confirmar": None}),
            {"valor": 40.0, "percentual": 0.4, "base": 100.0, "itens_fora": 0})
        self.assertEqual(raio_x_servico.margem_sem_custo_a_confirmar(
            {"faturamento_com_custo": 0.0, "custo_total": None}),
            {"valor": None, "percentual": None, "base": None, "itens_fora": 0})
        # Lixo no bloco (JSON editado à mão; o json.loads aceita NaN e Infinity): nada levanta.
        nan, inf = float("nan"), float("inf")
        for bloco in ("x", {"itens": "1"}, {"itens": True}, {"itens": 2, "custo_total": "a"},
                      {"itens": 1, "custo_total": nan, "faturamento_com_custo": 1.0},
                      {"itens": 1, "custo_total": 1.0, "faturamento_com_custo": inf}):
            with self.subTest(bloco=bloco):
                raio_x_servico.margem_sem_custo_a_confirmar(
                    {"faturamento_com_custo": 10.0, "custo_total": 5.0, "custo_a_confirmar": bloco})
        for valor in (nan, inf, -inf):
            with self.subTest(valor=valor):
                self.assertIsNone(raio_x_servico.margem_sem_custo_a_confirmar(
                    {"faturamento_com_custo": valor, "custo_total": 5.0})["valor"])
                self.assertIsNone(raio_x_servico.margem_sem_custo_a_confirmar(
                    {"faturamento_com_custo": 10.0, "custo_total": valor})["valor"])

    def test_custo_sem_o_item_a_confirmar(self):
        # Café 150 + Leite 110, sem os 40.000 do Item Genérico.
        self.assertEqual(raio_x_servico.custo_sem_custo_a_confirmar(self.absurdo["auditoria"]), 260.0)
        auditoria = _diagnosticar(JANEIRO, FEVEREIRO)["auditoria"]
        self.assertEqual(raio_x_servico.custo_sem_custo_a_confirmar(auditoria), auditoria["custo_total"])

    def test_custo_limpo_nas_bordas(self):
        custo = raio_x_servico.custo_sem_custo_a_confirmar
        # Todo o custo é do item a confirmar: sobra zero, nunca negativo.
        self.assertEqual(custo({"custo_total": 40000.0, "custo_a_confirmar": {"itens": 1, "custo_total": 40000.0}}), 0.0)
        # Raio-X antigo (sem o bloco) e sem custo.
        self.assertEqual(custo({"custo_total": 60.0, "custo_a_confirmar": None}), 60.0)
        self.assertEqual(custo({"custo_total": 0.0}), 0.0)
        self.assertIsNone(custo({"custo_total": None}))
        # Em centavos inteiros: 0,10 + 0,20 - 0,10 não deixa resto.
        self.assertEqual(custo({"custo_total": 0.1 + 0.2, "custo_a_confirmar": {"itens": 1, "custo_total": 0.1}}), 0.2)
        # Lixo no bloco ou no número (JSON editado à mão): nada levanta.
        nan = float("nan")
        self.assertIsNone(custo({"custo_total": nan}))
        self.assertIsNone(custo({"custo_total": 10.0, "custo_a_confirmar": {"itens": 1, "custo_total": nan}}))
        self.assertEqual(custo({"custo_total": 10.0, "custo_a_confirmar": {"itens": "1", "custo_total": 5.0}}), 10.0)

    def test_custo_limpo_e_do_diagnostico_de_cmv(self):
        self.assertEqual(raio_x_servico.visao(self.absurdo, PRATA, None)["auditoria"]["custo_total_sem_a_confirmar"],
                         260.0)
        for liberadas in (BRONZE, BRONZE | {"margem_bruta"}):
            with self.subTest(liberadas=sorted(liberadas)):
                auditoria = raio_x_servico.visao(self.absurdo, liberadas, None)["auditoria"]
                self.assertIsNone(auditoria["custo_total_sem_a_confirmar"])
                self.assertNotIn("260.0", json.dumps(auditoria))

    def test_card_do_custo(self):
        card = ap.custo_do_card(raio_x_servico.visao(self.absurdo, PRATA, None)["auditoria"])
        self.assertEqual(card, {"valor": 260.0, "base": 300.0, "itens_fora": 1, "so_isolados": False})
        # Sem o custo e sem a margem, com item a confirmar: nem o custo nem as vendas auditadas.
        card = ap.custo_do_card(raio_x_servico.visao(self.absurdo, BRONZE, None)["auditoria"])
        self.assertEqual(card, {"valor": None, "base": None, "itens_fora": 1, "so_isolados": False})
        # Sem item a confirmar: a venda com custo de sempre, que todo plano vê.
        normal = _diagnosticar(JANEIRO, FEVEREIRO)
        card = ap.custo_do_card(raio_x_servico.visao(normal, BRONZE, None)["auditoria"])
        self.assertEqual(card, {"valor": None, "base": normal["auditoria"]["faturamento_com_custo"], "itens_fora": 0,
                                "so_isolados": False})
        # Todo o custo informado era do item isolado: o card mostra zero e avisa no rodapé.
        so_isolado = {"custo_total_sem_a_confirmar": 0.0, "faturamento_com_custo_sem_a_confirmar": 0.0,
                      "custo_a_confirmar": {"itens": 1}}
        self.assertTrue(ap.custo_do_card(so_isolado)["so_isolados"])

    def test_nota_dos_itens_isolados(self):
        self.assertEqual(ap.nota_dos_isolados(0), "")
        self.assertEqual(ap.nota_dos_isolados(None), "")
        self.assertEqual(ap.nota_dos_isolados(1), "1 item com erro grave de cadastro (custo de 3 vezes o preço de "
                                                  "venda ou mais) foi isolado e não entra nesta conta.")
        self.assertTrue(ap.nota_dos_isolados(1200).startswith("1.200 itens com erro grave de cadastro"))
        self.assertTrue(ap.nota_dos_isolados(2).endswith("foram isolados e não entram nesta conta."))

    def test_visao_do_prata_traz_os_campos_limpos(self):
        auditoria = raio_x_servico.visao(self.absurdo, PRATA, None)["auditoria"]
        self.assertEqual((auditoria["margem_sem_a_confirmar_valor"], auditoria["margem_sem_a_confirmar_percentual"],
                          auditoria["faturamento_com_custo_sem_a_confirmar"]), (40.0, 0.1333, 300.0))
        # Leite (100 contra 110) e o Item Genérico têm margem negativa; sem o genérico, sobra 1.
        self.assertEqual((auditoria["itens_margem_negativa"], auditoria["itens_margem_negativa_sem_a_confirmar"]), (2, 1))
        # Os campos de sempre não mudam (API e telas antigas).
        self.assertEqual(auditoria["margem_valor"], -39460.0)

    def test_visao_do_bronze_nao_leva_margem_nem_a_venda_dos_itens_a_confirmar(self):
        auditoria = raio_x_servico.visao(self.absurdo, BRONZE, 6)["auditoria"]
        for campo in ("margem_sem_a_confirmar_valor", "margem_sem_a_confirmar_percentual",
                      "faturamento_com_custo_sem_a_confirmar"):
            self.assertIsNone(auditoria[campo], campo)
        # A contagem é aberta, como a de itens com margem negativa.
        self.assertEqual(auditoria["itens_margem_negativa_sem_a_confirmar"], 1)
        texto = json.dumps(auditoria)
        for valor in ("0.1333", "300.0", "40.0"):
            self.assertNotIn(valor, texto)

    def test_plano_so_com_a_margem_ve_a_margem_limpa_como_no_indicador_3(self):
        """A margem limpa pede a funcionalidade da margem, a mesma do indicador 3, que já entrega essa margem e essa
        venda com custo sem os itens a confirmar a quem tem só a margem (revisão de 14/11/2026)."""
        so_margem = BRONZE | {"margem_bruta"}
        visao = raio_x_servico.visao(self.absurdo, so_margem, None)
        auditoria = visao["auditoria"]
        self.assertEqual((auditoria["margem_sem_a_confirmar_valor"], auditoria["margem_sem_a_confirmar_percentual"],
                          auditoria["faturamento_com_custo_sem_a_confirmar"]), (40.0, 0.1333, 300.0))
        indicador = visao["indicadores_executivos"]["indicadores"]["margem"]
        self.assertEqual((indicador["bruta_valor"], indicador["bruta_percentual"], indicador["faturamento_com_custo"]),
                         (40.0, 0.1333, 300.0))
        card = ap.margem_do_card(auditoria)
        self.assertEqual((card["percentual"], card["itens_fora"], card["cor"]), (0.1333, 1, "amber"))
        self.assertNotIn("depende_do_cmv", card)
        # O custo do bloco continua do diagnóstico de CMV.
        self.assertIsNone(auditoria["custo_a_confirmar"]["custo_total"])

    def test_card_sem_custo_confiavel(self):
        so_generico = ("Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 31/01/2026\n"
                       "Codigo;Produto;Qtde;Valor Total;Total Custo\n9;Item Generico;100;500,00;40000,00\n"
                       "1;Cafe 500g;10;200,00;\n").encode("utf-8")
        auditoria = raio_x_servico.visao(_diagnosticar(so_generico), PRATA, None)["auditoria"]
        card = ap.margem_do_card(auditoria)
        self.assertEqual({chave: card[chave] for chave in ("percentual", "valor", "itens_fora", "sem_custo_confiavel")},
                         {"percentual": None, "valor": None, "itens_fora": 1, "sem_custo_confiavel": True})

    def test_cor_do_card_pela_margem_baixa_da_auditoria(self):
        """A cor do card da sobra segue a "margem baixa" do filtro da tabela de lucro bruto, na mesma tela (20%)."""
        self.assertEqual(ap.MARGEM_BAIXA, 0.20)
        self.assertEqual([ap._cor_da_margem(valor) for valor in (-0.01, 0.0, 0.1994, 0.1996, 0.2, None)],
                         ["red", "amber", "amber", "green", "green", "info"])  # 0,1996 aparece como 20,0%

    def test_raio_x_antigo_nao_tira_linha_da_tabela_de_lucro(self):
        """Sem o bloco custo_a_confirmar (gravado até a raio-x-2026-11.4), os cards contam todos os produtos: a tabela
        de lucro bruto também, para nenhum produto sumir sem explicação."""
        antigo = copy.deepcopy(self.absurdo)
        antigo["auditoria"].pop("custo_a_confirmar")
        produtos = [linha["item"]["produto"] for linha in ap.preparar(raio_x_servico.visao(antigo, PRATA, None))
                    ["lucro_por_produto"]]
        self.assertIn("Item Generico", produtos)
        atual = [linha["item"]["produto"] for linha in ap.preparar(raio_x_servico.visao(self.absurdo, PRATA, None))
                 ["lucro_por_produto"]]
        self.assertNotIn("Item Generico", atual)

    def test_raio_x_antigo_avisa_no_card(self):
        antigo = copy.deepcopy(self.absurdo)
        antigo["auditoria"].pop("custo_a_confirmar")
        auditoria = raio_x_servico.visao(antigo, PRATA, None)["auditoria"]
        card = ap.margem_do_card(auditoria)
        self.assertEqual((card["antigo"], card["percentual"], card["itens_fora"]), (True, -49.325, 0))


# =====================================================================
# Tabelas: ordem de impacto, busca e marcas do filtro
# =====================================================================
class TabelasTests(unittest.TestCase):
    def _fatias(self, recuperaveis, sem_custo=None):
        return [{"rotulo": f"{mes:02d}/2026", "recuperavel": valor, "perda_abaixo_custo": 0.0,
                 "vazamento_preco": valor, "pct_faturamento_sem_custo": (sem_custo or {}).get(mes, 0.0)}
                for mes, valor in enumerate(recuperaveis, 1)]

    def test_mes_a_mes_longo_sai_pelo_impacto_e_guarda_o_calendario(self):
        linhas = ap.preparar_fatias(self._fatias([5, 0, 30, 30, 1, 0, 8]))
        self.assertEqual([linha["fatia"]["rotulo"] for linha in linhas],
                         ["04/2026", "03/2026", "07/2026", "01/2026", "05/2026", "06/2026", "02/2026"])
        self.assertEqual([linha["pos_impacto"] for linha in linhas], list(range(7)))
        self.assertEqual(sorted(linha["pos_calendario"] for linha in linhas), list(range(7)))

    def test_mes_a_mes_curto_fica_no_calendario(self):
        linhas = ap.preparar_fatias(self._fatias([5, 0, 30]))
        self.assertEqual([linha["fatia"]["rotulo"] for linha in linhas], ["01/2026", "02/2026", "03/2026"])
        self.assertEqual([linha["pos_impacto"] for linha in linhas], [1, 2, 0])

    def test_marcas_de_perda_e_de_alerta_do_mes(self):
        limiar = raio_x.LIMIAR_SEM_CUSTO
        linhas = ap.preparar_fatias(self._fatias([0, 3, 0], sem_custo={1: limiar, 3: limiar - 0.0001}))
        marcas = {linha["fatia"]["rotulo"]: linha["marcas"] for linha in linhas}
        self.assertEqual(marcas, {"01/2026": "alerta atencao", "02/2026": "perda atencao", "03/2026": ""})

    def test_busca_do_mes_por_extenso(self):
        self.assertEqual(ap.texto_do_periodo({"rotulo": "03/2026"}), "03/2026 março de 2026")
        self.assertEqual(ap.texto_do_periodo({"rotulo": "Arquivo 2"}), "Arquivo 2")
        self.assertEqual(ap.texto_do_periodo({"rotulo": "01/01/2026 a 15/02/2026"}), "01/01/2026 a 15/02/2026")

    def test_pareto_marca_os_que_somam_80_por_cento(self):
        itens = [{"perda": 50}, {"perda": 30}, {"perda": 10}, {"perda": 10}]
        self.assertEqual(ap.marcas_pareto(itens, "perda"), ["principais", "principais", "", ""])
        # Com o total do envio (a lista cortada), os 80% podem não fechar dentro dela.
        self.assertEqual(ap.marcas_pareto(itens, "perda", total=1000), ["principais"] * 4)
        self.assertEqual(ap.marcas_pareto([], "perda"), [])
        self.assertEqual(ap.marcas_pareto([{"perda": 0}], "perda"), [""])

    def test_custo_a_confirmar_pela_propria_linha(self):
        """Sem depender da lista do bloco (cortada em 50, nula sem a auditoria): a regra do motor em centavos na
        margem por produto e, no scanner, a margem de -200% ou menos. Só com a margem à vista."""
        margem_por_item = {"produto": "Generico Grande", "codigo": None, "custo_total": 900.0,
                           "faturamento_com_custo": 300.0, "margem_valor": -600.0, "margem_percentual": -2.0}
        quase = {**margem_por_item, "custo_total": 899.99, "margem_valor": -599.99, "margem_percentual": -1.99997}
        scanner = {"produto": "Generico", "codigo": "9", "vazamento": 0.0, "margem_valor": -39500.0,
                   "margem_percentual": -79.0}
        linhas = ap.linhas_de_produto([margem_por_item, quase, scanner], margem_visivel=True)
        self.assertEqual([(linha["a_confirmar"], linha["marcas"]) for linha in linhas],
                         [(True, "a_confirmar"), (False, "negativa baixa"), (True, "a_confirmar")])
        # Com a margem escondida, a linha não traz o número e nada é marcado por ela.
        escondida = ap.linhas_de_produto([{**scanner, "margem_valor": None, "margem_percentual": None}],
                                         margem_visivel=False)
        self.assertEqual((escondida[0]["a_confirmar"], escondida[0]["marcas"]), (False, ""))

    def test_margem_abaixo_do_minimo_inclui_a_negativa(self):
        linhas = ap.linhas_de_produto([{"produto": "A", "margem_valor": -1.0, "margem_percentual": -0.1},
                                       {"produto": "B", "margem_valor": 1.0, "margem_percentual": 0.1499},
                                       {"produto": "C", "margem_valor": 2.0, "margem_percentual": 0.15}],
                                      margem_visivel=True)
        self.assertEqual([linha["marcas"] for linha in linhas], ["negativa baixa", "baixa", ""])

    def test_lucro_bruto_sem_os_itens_a_confirmar_e_pelo_maior_lucro(self):
        itens = [{"produto": "A", "codigo": "1", "margem_valor": 10.0, "margem_percentual": 0.5},
                 {"produto": "B", "codigo": "2", "margem_valor": 30.0, "margem_percentual": 0.3},
                 {"produto": "C", "codigo": "3", "margem_valor": -5.0, "margem_percentual": -0.05},
                 {"produto": "D", "codigo": "4", "margem_valor": None, "margem_percentual": None},
                 {"produto": "Generico", "codigo": "9", "margem_valor": -39500.0, "margem_percentual": -79.0},
                 {"produto": "Da lista", "codigo": "8", "margem_valor": 1.0, "margem_percentual": 0.1},
                 {"produto": "E", "codigo": "5", "margem_valor": 30.0, "margem_percentual": 0.2},
                 {"produto": "F", "codigo": "6", "margem_valor": 2.0, "margem_percentual": 0.4}]
        linhas = ap.linhas_do_lucro(itens, a_confirmar=frozenset({("Da lista", "8")}))
        # O maior lucro em reais primeiro; no empate, a ordem do motor; sem número, no fim.
        self.assertEqual([linha["item"]["produto"] for linha in linhas], ["B", "E", "A", "F", "C", "D"])
        # A ordem dos filtros de margem baixa e negativa: o maior prejuízo primeiro (C), sem número no fim (D).
        self.assertEqual([linha["pos_piores"] for linha in linhas], [3, 4, 2, 1, 0, 5])
        self.assertEqual(set(linhas[0]), {"item", "texto", "marcas", "a_confirmar", "pos_piores"})
        # Sem separar (Raio-X antigo), nenhum sai.
        self.assertEqual(len(ap.linhas_do_lucro(itens, separar=False)), len(itens))
        # As faixas comparam a margem como a tela a mostra, com uma casa: 40,0% não é "acima de 40%", e 20,0% não é
        # "abaixo de 20%".
        bordas = [{"produto": nome, "margem_valor": 1.0, "margem_percentual": margem}
                  for nome, margem in (("a", 0.4004), ("b", 0.4006), ("c", 0.1996), ("d", 0.1994))]
        self.assertEqual([linha["marcas"] for linha in ap.linhas_do_lucro(bordas)],
                         ["", "margem_alta", "", "margem_baixa"])
        # Faixas do dono: acima de 40% e abaixo de 20% (os limites exatos ficam fora); a negativa também é baixa.
        self.assertEqual([linha["marcas"] for linha in linhas],
                         ["", "", "margem_alta", "", "margem_baixa negativa", ""])
        self.assertEqual(ap.linhas_do_lucro(None), [])

    def test_margem_negativa_so_marca_quando_o_plano_mostra(self):
        itens = [{"produto": "Leite 1L", "codigo": "3", "vazamento": 2.0, "margem_valor": -10.0},
                 {"produto": "Item Generico", "codigo": "9", "vazamento": 0.0, "margem_valor": -39500.0}]
        confirmar = frozenset({("Item Generico", "9")})
        aberto = ap.linhas_de_produto(itens, margem_visivel=True, a_confirmar=confirmar)
        self.assertEqual([(linha["marcas"], linha["a_confirmar"]) for linha in aberto],
                         [("abaixo negativa", False), ("a_confirmar", True)])
        protegido = ap.linhas_de_produto([{**item, "margem_valor": None} for item in itens], margem_visivel=False)
        self.assertNotIn("negativa", " ".join(linha["marcas"] for linha in protegido))
        self.assertEqual(aberto[0]["texto"], "Leite 1L 3")


# =====================================================================
# Catálogo dos avisos: todo texto que o motor gera tem a versão simples
# =====================================================================
def _ids(avisos):
    organizados = ap.organizar_avisos(avisos)
    soltos = [item["tecnico"] for item in organizados["afetam"] if item["id"] is None]
    return [item["id"] for item in organizados["afetam"] + organizados["informativos"] if item["id"]], soltos


class CatalogoDeAvisosTests(unittest.TestCase):
    def assert_casa(self, texto, esperado):
        casados = [modelo.id for modelo in ap.CATALOGO_DE_AVISOS if re.fullmatch(modelo.regex, texto)]
        self.assertEqual(casados, [esperado], texto)

    def test_os_avisos_de_cada_relatorio_somado(self):
        from datetime import date
        resumos = [
            {"indice": 0, "inicio": date(2026, 1, 1), "fim": date(2026, 1, 31), "tem_custo": False,
             "celulas_recodificadas": 3},
            {"indice": 1, "inicio": date(2026, 1, 15), "fim": date(2026, 2, 14), "tem_custo": True,
             "celulas_recodificadas": 1},
            {"indice": 2, "inicio": None, "fim": None, "tem_custo": True},
        ]
        avisos = raio_x._avisos(resumos)
        self.assertEqual(len(avisos), 5)
        ids = [modelo for texto in avisos for modelo in [m.id for m in ap.CATALOGO_DE_AVISOS
                                                          if re.fullmatch(m.regex, texto)]]
        self.assertEqual(sorted(ids), sorted(["periodos_sobrepostos", "arquivo_sem_periodo",
                                              "arquivo_sem_coluna_de_custo", "acentos_corrigidos",
                                              "acentos_corrigidos"]))

    def test_o_papel_de_cada_arquivo_do_lote(self):
        esperados = {"vendas": "papel_venda_por_produto_auxiliar", "precos": "papel_lista_de_precos_auxiliar",
                     "documentos": "papel_resumo_por_pedido_auxiliar", "compras": "papel_relatorio_de_compras_fora",
                     "resumo_de_vendas": "papel_resumo_de_vendas_sem_produto_fora",
                     "ambiguo": "papel_venda_de_colunas_duvidosas_fora", "nao_reconhecido": "papel_nao_reconhecido_fora"}
        # O motor não pode ganhar papel novo sem a versão simples dele.
        self.assertEqual(set(raio_x._PAPEIS_DO_LOTE), set(esperados) | {"estoque", "ilegivel"})
        for papel, esperado in esperados.items():
            with self.subTest(papel=papel):
                self.assert_casa(raio_x._aviso_do_papel(1, {"papel": papel, "rotulo": "", "artigo": ""}, com_ancora=True),
                                 esperado)
        for rotulo in ("posição de estoque", "inventário de estoque"):
            self.assert_casa(raio_x._aviso_do_papel(2, {"papel": "estoque", "rotulo": rotulo}, com_ancora=True),
                             "papel_estoque_auxiliar")
        # O título de tabela auxiliar que não pôde ser lida (raio_x._ILEGIVEL).
        self.assertEqual(set(raio_x._ILEGIVEL), {"estoque", "precos"})
        for chave, esperado in (("estoque", "papel_estoque_ilegivel_fora"), ("precos", "papel_lista_de_precos_ilegivel_fora")):
            tabela = {"papel": "ilegivel", "rotulo": raio_x._ILEGIVEL[chave][0]}
            self.assert_casa(raio_x._aviso_do_papel(3, tabela, com_ancora=True), esperado)
        # Sem a venda item a item, a tabela auxiliar fica fora das contas (com o artigo e o rótulo do leitor).
        for tabela, esperado in (
                ({"papel": "estoque", "artigo": "uma", "rotulo": "posição de estoque"}, "tabela_de_estoque_sem_relatorio_principal"),
                ({"papel": "estoque", "artigo": "um", "rotulo": "inventário de estoque"}, "tabela_de_estoque_sem_relatorio_principal"),
                ({"papel": "precos", "artigo": "uma", "rotulo": "lista de preços"}, "lista_de_precos_sem_relatorio_principal"),
                ({"papel": "documentos", "artigo": "um", "rotulo": "resumo por pedido ou documento"},
                 "resumo_por_pedido_sem_relatorio_principal")):
            with self.subTest(tabela=tabela["rotulo"]):
                self.assert_casa(raio_x._aviso_do_papel(4, tabela, com_ancora=False), esperado)
        self.assert_casa(raio_x._AVISO_DAS_LOJAS.format(numero=2), "loja_fora_da_venda_principal")

    def test_saldo_de_estoque_em_todas_as_formas(self):
        for fonte, esperado in (
                ({"principais": [1], "completaram": [2]}, "saldo_de_estoque_uma_loja"),
                ({"principais": [1], "fora": [2]}, "saldo_de_estoque_uma_loja"),
                ({"principais": [1], "completaram": [2, 3], "fora": [4, 5, 6]}, "saldo_de_estoque_uma_loja"),
                ({"principais": [1, 2], "lojas": 2}, "saldo_de_estoque_varias_lojas"),
                ({"principais": [1, 2, 3], "lojas": 3}, "saldo_de_estoque_varias_lojas"),
                ({"principais": [1, 2], "lojas": 2, "completaram": [3]}, "saldo_de_estoque_varias_lojas_com_complemento"),
                ({"principais": [1, 2], "lojas": 2, "fora": [3, 4]}, "saldo_de_estoque_varias_lojas_com_complemento")):
            with self.subTest(fonte=fonte):
                self.assert_casa(raio_x._aviso_do_saldo(fonte), esperado)

    def test_amostra_parcial_com_milhar(self):
        import pandas as pd
        from datetime import date
        for com_saldo, vendidos, fim in ((30, 2, date(2026, 5, 2)), (1636, 113, date(2029, 11, 11))):
            saldos = pd.Series([5.0] * com_saldo, index=[f"p{i}" for i in range(com_saldo)])
            vendidas = pd.Series([f"p{i}" for i in range(vendidos)])
            texto = raio_x._aviso_de_amostra_parcial(saldos, vendidas, (date(2026, 1, 1), fim), "1")
            self.assertIsNotNone(texto)
            self.assert_casa(texto, "amostra_parcial")

    def test_custo_a_confirmar_no_singular_e_no_plural(self):
        um = ap.organizar_avisos(_diagnosticar(CUSTO_ABSURDO)["avisos"])["afetam"]
        self.assertEqual([aviso["id"] for aviso in um], ["custo_a_confirmar"])
        self.assertTrue(um[0]["texto"].startswith("1 produto tem, nas vendas com custo, custo de 3 vezes"), um[0]["texto"])
        self.assertIn("o que ele parece ter perdido não entra na perda abaixo do custo, ele fica fora do custo e da sobra bruta da "
                      "Auditoria de custos", um[0]["texto"])
        dois = ap.organizar_avisos(["2 produto(s) com custo a partir de 3 vezes a venda ficaram fora da perda e do "
                                    "potencial recuperável: confira no cadastro a unidade ou a embalagem do custo."])
        self.assertTrue(dois["afetam"][0]["texto"].startswith("2 produtos têm, nas vendas com custo"))
        self.assertIn("eles ficam fora do custo e da sobra bruta", dois["afetam"][0]["texto"])

    def test_numero_do_arquivo_ganha_o_nome_e_aviso_desconhecido_aparece_inteiro(self):
        nomes = ["vendas_out.xls", "estoque.xls", "compras.xls"]
        avisos = [raio_x._aviso_do_papel(1, {"papel": "estoque", "rotulo": "posição de estoque"}, com_ancora=True),
                  raio_x._aviso_do_papel(2, {"papel": "compras"}, com_ancora=True),
                  raio_x._aviso_do_saldo({"principais": [1], "completaram": [0], "fora": [2]}),
                  "Um aviso que o catálogo não conhece."]
        org = ap.organizar_avisos(avisos, nomes)
        textos = " ".join(item["texto"] for item in org["informativos"] + org["afetam"])
        self.assertIn("O arquivo 2 (estoque.xls) é uma tabela de estoque", textos)
        self.assertIn("O arquivo 3 (compras.xls) é um relatório de compras", textos)
        self.assertEqual(org["papeis"], {2: "auxiliar", 3: "fora_das_contas"})
        desconhecido = [item for item in org["afetam"] if item["id"] is None]
        self.assertEqual([(item["titulo"], item["texto"]) for item in desconhecido],
                         [("Aviso do processamento", "Um aviso que o catálogo não conhece.")])
        # Aviso vazio ou que não é texto (JSON editado à mão) não quebra a tela.
        self.assertEqual(ap.organizar_avisos([None, 3, "  "]), {"afetam": [], "informativos": [], "papeis": {}})

    def test_textos_gravados_por_versoes_anteriores_do_lote(self):
        """Raio-X gravado no meio da 11.6 (cc62a44 e 9c4f531) com os textos de antes: casam do mesmo jeito."""
        for texto, esperado in (
                ("O arquivo 2 entrou como posição de estoque (tabela auxiliar).", "papel_estoque_auxiliar"),
                ("O arquivo 4 entrou como lista de preços (o cadastro dos produtos).", "papel_lista_de_precos_auxiliar"),
                ("O resumo por documento casou com 97% das vendas do arquivo 1: o desconto, o cliente e a hora vieram "
                 "dele.", "resumo_por_pedido_completou_varios_dados"),
                ("O resumo por documento casou com só 9% das vendas do arquivo 1 (o mínimo é 90%): ficou fora.",
                 "resumo_por_pedido_bateu_pouco")):
            with self.subTest(texto=texto):
                self.assert_casa(texto, esperado)

    @staticmethod
    def _f_string_do_motor(trecho, sem=None, **variaveis):
        """O texto do aviso de raio_x.py que tem `trecho` (e não tem `sem`) nas partes fixas, montado com `variaveis`:
        o f-string é lido do próprio motor (ast), e não copiado aqui, então muda junto com ele."""
        import ast
        arvore = ast.parse(Path(raio_x.__file__).read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if not (isinstance(no, ast.Call) and getattr(no.func, "attr", None) == "append" and no.args
                    and isinstance(no.args[0], ast.JoinedStr)):
                continue
            fixo = "".join(str(parte.value) for parte in no.args[0].values if isinstance(parte, ast.Constant))
            if trecho in fixo and not (sem and sem in fixo):
                codigo = compile(ast.Expression(no.args[0]), "raio_x.py", "eval")
                return eval(codigo, {"_CASAMENTO_MINIMO_DOS_DOCUMENTOS": raio_x._CASAMENTO_MINIMO_DOS_DOCUMENTOS,
                                     **variaveis})
        raise AssertionError(f"aviso com {trecho!r} não encontrado em raio_x.py")

    def test_avisos_do_lote_que_os_relatorios_ficticios_nao_disparam(self):
        motor = self._f_string_do_motor
        for texto, esperado in (
                (motor("nome(s) de produto com mais de um código", ambiguos=2), "nomes_com_mais_de_um_codigo"),
                (motor("do valor das vendas do arquivo", sem="casou com só", taxa=0.954, numero=1,
                       texto="ele não trouxe nada que a venda já não tivesse"), "resumo_por_pedido_sem_novidade"),
                (motor("do valor das vendas do arquivo", sem="casou com só", taxa=1.0, numero=1, texto="a hora veio dele"),
                 "resumo_por_pedido_completou_um_dado"),
                (motor("do valor das vendas do arquivo", sem="casou com só", taxa=0.97, numero=12,
                       texto="o desconto, o cliente e a hora vieram dele"), "resumo_por_pedido_completou_varios_dados"),
                (motor("do valor das vendas do arquivo", sem="casou com só", taxa=0.91, numero=1, texto="o desconto e a hora vieram dele"),
                 "resumo_por_pedido_completou_varios_dados"),
                (motor("casou com só", taxa=0.0, numero=1), "resumo_por_pedido_bateu_pouco"),
                (motor("casou com só", taxa=0.896, numero=3), "resumo_por_pedido_bateu_pouco"),
                (motor("veio das tabelas auxiliares", numero=1, casados=1, total=1), "custo_completado_por_outros_arquivos"),
                (motor("sem venda nem saldo entraram no", catalogo=[1]), "produtos_so_no_cadastro")):
            with self.subTest(texto=texto):
                self.assert_casa(texto, esperado)
        # "Bateu com menos de 90%" vale também quando o arredondamento do motor escreve 90% (89,6%).
        org = ap.organizar_avisos([motor("casou com só", taxa=0.896, numero=3)])
        self.assertIn("bateu com menos de 90% do valor das vendas do seu relatório principal, o arquivo 3.",
                      org["informativos"][0]["texto"])

    def test_cada_ponto_do_motor_que_gera_aviso_esta_no_catalogo(self):
        """Alarme: um aviso novo no motor (raio_x.py) precisa da versão simples aqui, senão aparece cru na tela."""
        fonte = Path(raio_x.__file__).read_text(encoding="utf-8")
        self.assertEqual(fonte.count("avisos.append("), 13)
        self.assertEqual(fonte.count("avisos_do_lote.append("), 2)
        # Cada modelo casa só ele mesmo nos exemplos de que os testes acima precisam (não há regex que engula outra).
        ids = [modelo.id for modelo in ap.CATALOGO_DE_AVISOS]
        self.assertEqual(len(ids), len(set(ids)))
        for modelo in ap.CATALOGO_DE_AVISOS:
            re.compile(modelo.regex)
            self.assertIn(modelo.grupo, ("afeta", "informativo"))
            self.assertIn(modelo.papel, ("auxiliar", "fora_das_contas", "nenhum"))
            for campo in ("titulo", "texto", "porque"):
                self.assertTrue(getattr(modelo, campo).strip(), (modelo.id, campo))


class CatalogoDoLoteTests(unittest.TestCase):
    """O lote inteiro, com as réplicas dos relatórios do ERP (tests/leiautes_erp_support.py): nenhum aviso sai sem a
    versão simples, e o resumo de uma linha conta o papel de cada arquivo."""

    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="nexo-avisos-lote-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.numero = 0
        from tests import leiautes_erp_support
        self.leiautes = leiautes_erp_support

    def xls(self, nome, relatorio):
        self.numero += 1
        return self.leiautes.gravar_xls(*relatorio[:3], self.pasta / f"{self.numero:02d}_{nome}.xls")

    def test_lote_com_todos_os_relatorios_e_auxiliares(self):
        ancora = self.xls("detalhado", self.leiautes.relatorio_detalhado(clientes=1))
        outros = [self.xls(nome, relatorio()) for nome, relatorio in self.leiautes.RELATORIOS.items() if nome != "detalhado"]
        outros += [self.xls(nome, relatorio()) for nome, relatorio in self.leiautes.AUXILIARES.items()]
        resultado = executar_como_o_worker([ancora] + outros)
        ids, soltos = _ids(resultado["avisos"])
        self.assertEqual(soltos, [])
        org = ap.organizar_avisos(resultado["avisos"])
        total = 1 + len(outros)
        resumo = ap.resumo_dos_arquivos(total, org["papeis"])
        self.assertTrue(resumo.startswith(f"{total} arquivos lidos • 1 principal de vendas"), resumo)
        self.assertEqual(len(org["papeis"]), len(outros))
        self.cobertos = set(ids)
        print("\nCOBERTOS_LOTE", sorted(self.cobertos))

    def test_tabelas_auxiliares_sem_a_venda_item_a_item(self):
        mes = relatorio_do_mes(2026, 9, ["1;CAFE 500G;10;20,00;180,00;150,00"])
        self.numero += 1
        venda = self.pasta / f"{self.numero:02d}_mes.csv"
        venda.write_bytes(mes)
        auxiliares = [self.xls(nome, relatorio()) for nome, relatorio in self.leiautes.AUXILIARES.items()
                      if nome != "posicao_de_compras"]
        resultado = executar_como_o_worker([str(venda)] + auxiliares)
        ids, soltos = _ids(resultado["avisos"])
        self.assertEqual(soltos, [])
        self.assertIn("tabela_de_estoque_sem_relatorio_principal", ids)
        org = ap.organizar_avisos(resultado["avisos"])
        self.assertEqual(ap.resumo_dos_arquivos(1 + len(auxiliares), org["papeis"]),
                         f"{1 + len(auxiliares)} arquivos lidos • 1 de vendas e {len(auxiliares)} fora da soma")
        print("\nCOBERTOS_SEM_ANCORA", sorted(set(ids)))


# =====================================================================
# Resumo dos arquivos
# =====================================================================
class ResumoDosArquivosTests(unittest.TestCase):
    def test_lote_com_venda_principal(self):
        papeis = {2: "auxiliar", 3: "auxiliar", 4: "auxiliar", 5: "auxiliar"}
        papeis.update({numero: "fora_das_contas" for numero in range(6, 14)})
        self.assertEqual(ap.resumo_dos_arquivos(13, papeis),
                         "13 arquivos lidos • 1 principal de vendas, 4 auxiliares e 8 fora da soma")

    def test_relatorios_mensais_sem_tabela_auxiliar(self):
        self.assertEqual(ap.resumo_dos_arquivos(3, {}), "3 arquivos lidos • 3 de vendas")
        self.assertEqual(ap.resumo_dos_arquivos(1, {}), "1 arquivo lido • 1 de vendas")
        self.assertEqual(ap.resumo_dos_arquivos(3, {3: "fora_das_contas"}),
                         "3 arquivos lidos • 2 de vendas e 1 fora da soma")
        self.assertEqual(ap.resumo_dos_arquivos(2, {2: "auxiliar"}),
                         "2 arquivos lidos • 1 principal de vendas e 1 auxiliar")

    def test_total_menor_que_os_papeis_nao_fica_negativo(self):
        self.assertEqual(ap.resumo_dos_arquivos(0, {1: "auxiliar"}), "1 arquivo lido • 1 auxiliar")


# =====================================================================
# As telas
# =====================================================================
class TelasRaioXTests(planos._PlanosCase):
    def _html(self, caminho):
        resposta = self.cliente_http.get(caminho)
        self.assertEqual(resposta.status_code, 200)
        return resposta.get_data(as_text=True)

    def _tiles(self, html):
        return re.findall(r'<div class="kpi-tile">', html)

    def test_cada_card_tem_o_botao_de_explicacao(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        for caminho in (PAGINA, AUDITORIA):
            with self.subTest(caminho=caminho):
                html = self._html(caminho)
                infos = re.findall(r'<details class="kpi-info(?: kpi-info-curta)?" data-kpi-info[ >]', html)
                # Um (i) por card; no Raio-X, mais o do dinheiro recuperável.
                self.assertEqual(len(infos), len(self._tiles(html)) + (1 if caminho == PAGINA else 0))
                for rotulo in re.findall(r'aria-label="Entenda o indicador: ([^"]+)"', html):
                    self.assertTrue(rotulo.strip())
                self.assertNotRegex(html, r'<details class="kpi-info[^"]*"[^>]* open')

    def test_explicacoes_tem_as_tres_partes(self):
        chaves = [chave for chave in ap.EXPLICACOES if chave != "inconsistencias"]
        self.assertGreaterEqual(len(chaves), 8)
        for chave in chaves:
            with self.subTest(chave=chave):
                for parte in ("significa", "calculo", "se_ruim"):
                    self.assertTrue(ap.EXPLICACOES[chave][parte].strip())

    def test_dicas_da_auditoria_tem_uma_ou_duas_frases(self):
        self.assertEqual(set(ap.DICAS_DA_AUDITORIA), {"custo", "sobra", "prejuizo", "produtos_prejuizo"})
        for chave, texto in ap.DICAS_DA_AUDITORIA.items():
            with self.subTest(chave=chave):
                frases = [frase for frase in re.split(r"(?<=[.!?])\s+", texto.strip()) if frase]
                self.assertTrue(1 <= len(frases) <= 2, frases)
                self.assertTrue(texto.endswith("."))

    def test_tabelas_saem_com_barra_e_rodape_escondidos_ate_o_script(self):
        self._mudar_plano("PRATA")
        meses = [relatorio_do_mes(2025, mes, [f"1;Cafe 500g;10;20,00;{180 + mes},00;150,00"]) for mes in range(1, 8)]
        self._enviar_raio_x(*[(f"m{numero}.csv", conteudo) for numero, conteudo in enumerate(meses, 1)])
        html = self._html(PAGINA)
        bloco = html.split('id="tabelaFatias"', 1)[0].rsplit("data-tabela-rapida", 1)[1]
        self.assertIn("data-tabela-ferramentas hidden", bloco)
        rodape = html.split('id="tabelaFatias"', 1)[1]
        self.assertIn("data-tabela-rodape hidden", rodape)
        # Sete meses: a ordem já é a de impacto, e cada linha leva as duas posições.
        corpo = html.split('id="corpoFatias"', 1)[1].split("</tbody>", 1)[0]
        self.assertEqual(len(re.findall(r"data-pos-impacto=", corpo)), 7)
        self.assertIn("/static/js/raio-x.js", html)

    def test_bronze_nao_ganha_filtro_nem_marca_de_margem(self):
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        html = self._html(PAGINA)
        self.assertNotIn('value="negativa"', html)
        for marcas in re.findall(r'data-marcas="([^"]*)"', html):
            self.assertNotIn("negativa", marcas)

    def test_cards_do_custo_e_da_sobra_sem_o_item_a_confirmar(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", CUSTO_ABSURDO))
        html = self._html(AUDITORIA)
        self.assertIn("Onde a loja perdeu dinheiro vendendo barato demais ou com custo desatualizado no sistema.", html)
        # Custo dos produtos vendidos: Café 150 + Leite 110, sem os 40.000 do Item Genérico, sobre as mesmas vendas.
        custo = html.split("Custo dos produtos vendidos</span>", 1)[1].split('<div class="col-12', 1)[0]
        self.assertIn("R$</small> 260,00", custo)
        self.assertIn("Referente a R$ 300,00 em vendas auditadas", custo)
        self.assertNotIn("40.260", custo)
        # Sobra bruta: 300 - 260 = 40, 13,3% das vendas auditadas.
        sobra = html.split("Sobra bruta nas vendas</span>", 1)[1].split('<div class="col-12', 1)[0]
        self.assertIn("R$</small> 40,00", sobra)
        self.assertIn("Diferença entre venda e custo dos produtos", sobra)
        self.assertIn("Margem de 13,3% sobre as vendas auditadas", sobra)
        self.assertNotIn("-4932", sobra.replace(".", ""))
        # O (i) curto da sobra diz, numa nota discreta, que o item foi isolado (o pedido põe a nota só nele).
        nota = "1 item com erro grave de cadastro (custo de 3 vezes o preço de venda ou mais) foi isolado"
        self.assertNotIn(nota, custo)
        self.assertIn(nota, sobra)
        # O (i) fica no canto: depois do ícone colorido.
        self.assertLess(custo.index('class="kpi-icon'), custo.index("data-kpi-info-passar"))
        self.assertIn("data-kpi-info-passar", custo)
        # Prejuízo direto no caixa: o Leite (110 contra 100); o Item Genérico fica fora.
        prejuizo = html.split("Prejuízo direto no caixa</span>", 1)[1].split('<div class="col-12', 1)[0]
        self.assertIn("R$</small> 10,00", prejuizo)
        self.assertIn("1 produto vendido por menos do que custou", prejuizo)
        # Os produtos dando prejuízo também saem sem ele.
        negativos = html.split("Produtos dando prejuízo</span>", 1)[1].split("kpi-value", 1)[1][:40]
        self.assertIn(">1<", negativos)
        self.assertIn("Itens que fecharam o período no vermelho", html)

    def test_alertas_de_cadastro_recolhidos(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", CUSTO_ABSURDO))
        html = self._html(AUDITORIA)
        # Fechado por padrão (sem o atributo open), com o resumo de uma linha.
        self.assertIn('<details class="nexo-card mb-4 alertas-cadastro" id="alertasCadastro">', html)
        self.assertIn("Alertas de cadastro: 1 item com custo distorcido ignorado na conta", html)
        self.assertIn("Produtos com custo cadastrado mais de 3x acima do preço de venda — geralmente embalagem/caixa "
                      "cadastrada como unidade ou item genérico (ex: 'Diversos'). Corrija no seu PDV para a margem "
                      "ficar exata.", html)
        corpo = html.split('id="alertasCadastro"', 1)[1].split("</details>", 1)[0]
        self.assertIn('id="tabelaCustoAConfirmar"', corpo)
        self.assertIn("Item Generico", corpo)
        # Filtro rápido: os itens que somam 80% do custo distorcido (aqui, o único).
        self.assertIn("Maiores distorções (80% do custo)", corpo)
        self.assertEqual(re.findall(r'data-marcas="([^"]*)"', corpo.split('id="corpoCustoAConfirmar"', 1)[1]),
                         ["principais"])
        # Sem item a confirmar, não há alerta.
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        self.assertNotIn("alertasCadastro", self._html(AUDITORIA))

    def test_lucro_bruto_por_produto_sem_o_item_a_confirmar_e_pelo_maior_lucro(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", CUSTO_ABSURDO))
        html = self._html(AUDITORIA)
        self.assertIn("Lucro bruto por produto", html)
        self.assertIn("Quanto cada produto deixou de sobra após pagar o próprio custo.", html)
        corpo = html.split('id="corpoLucro"', 1)[1].split("</tbody>", 1)[0]
        self.assertNotIn("Item Generico", corpo)
        # Café (+50, 25%) antes do Leite (-10, -10%).
        self.assertLess(corpo.index("Cafe 500g"), corpo.index("Leite 1L"))
        self.assertEqual(re.findall(r'data-marcas="([^"]*)"', corpo), ["", "margem_baixa negativa"])
        # Os filtros de margem baixa e negativa começam pelo maior prejuízo (o Leite).
        self.assertEqual(re.findall(r'data-pos-piores="(\d+)"', corpo), ["1", "0"])
        self.assertIn('data-ordem="piores"', html)
        for rotulo in ("Margem alta (&gt; 40%)", "Margem baixa (&lt; 20%)", "No vermelho (margem negativa)"):
            self.assertIn(rotulo, html)
        self.assertIn("Produtos vendidos com prejuízo", html)
        self.assertIn("Vendas onde o preço cobrado ficou menor que o custo de compra (maior perda primeiro).", html)

    def test_todo_o_custo_era_do_item_isolado(self):
        """Só o Item Genérico tem custo: o custo e a sobra ficam sem base, e os textos falam de um item só."""
        self._mudar_plano("PRATA")
        so_generico = ("Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 31/01/2026\n"
                       "Codigo;Produto;Qtde;Valor Total;Total Custo\n9;Item Generico;100;500,00;40000,00\n"
                       "1;Cafe 500g;10;200,00;\n").encode("utf-8")
        self._enviar_raio_x(("jan.csv", so_generico))
        html = self._html(AUDITORIA)
        custo = html.split("Custo dos produtos vendidos</span>", 1)[1].split('<div class="col-12', 1)[0]
        self.assertIn("R$</small> 0,00", custo)
        self.assertIn("Todo o custo informado é do item com custo distorcido: corrija o cadastro para ver este custo.",
                      custo)
        self.assertIn("Todo o custo informado é do item com custo distorcido: corrija o cadastro para ver a sobra.",
                      html)
        self.assertIn("Nenhuma venda com custo informado fora o item com custo distorcido:", html)
        self.assertIn("Nenhuma venda abaixo do custo nos relatórios enviados, fora o item com custo distorcido, nos "
                      "alertas de cadastro logo abaixo.", html)

    def test_sem_custo_subindo_so_a_faixa_positiva(self):
        self._mudar_plano("PRATA")
        meses = [relatorio_do_mes(2026, mes, ["1;Cafe 500g;10;20,00;200,00;150,00"]) for mes in (1, 2)]
        self._enviar_raio_x(*[(f"m{numero}.csv", conteudo) for numero, conteudo in enumerate(meses, 1)])
        html = self._html(AUDITORIA)
        self.assertIn("✓</span> Nenhum produto teve aumento de custo sem repasse no preço neste período.", html)
        self.assertNotIn("precificacaoTitulo", html)
        # Com a alta do custo do Café em fevereiro, a seção volta com o título do dono e a tabela.
        self._enviar_raio_x(("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        html = self._html(AUDITORIA)
        self.assertIn("Produtos com custo subindo sem reajuste de preço", html)
        self.assertIn('id="tabelaPrecificacao"', html)
        self.assertNotIn("Nenhum produto teve aumento de custo", html)

    def test_raio_x_gravado_mostra_os_motivos_de_hoje(self):
        """Os motivos ficam gravados com o Raio-X; a tela os refaz com as regras de hoje (raio_x.recomendar é pura):
        a contagem de margem negativa bate com a dos pontos do cadastro, e o custo a confirmar tem motivo próprio."""
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", CUSTO_ABSURDO))
        linha = self._linha()
        gravado = json.loads(linha.resultado)
        gravado["recomendacao"]["motivos"] = ["2 item(ns) com margem negativa: vendidos, no período, abaixo do custo "
                                              "informado."]
        linha.resultado = json.dumps(gravado)
        self.db.session.commit()
        html = self._html(PAGINA)
        self.assertNotIn("2 item(ns) com margem negativa", html)
        self.assertIn("1 produto com margem negativa: vendido, no período, abaixo do custo informado.", html)
        self.assertIn("1 produto com custo a confirmar", html)
        self.assertEqual(self.cliente_http.get(planos.API).get_json()["recomendacao"]["plano"]["codigo"], "PRATA")

    def test_lista_cortada_pelo_motor_avisa_e_nao_diz_todos(self):
        """O motor corta as listas em raio_x.LIMITE_ITENS_LISTA: a tela diz de quantos e marca a lista como cortada
        (o botão diz "Ver os 50 produtos da lista", não "todos")."""
        linhas = [f"{i};Produto {i};10;20,00;190,00;150,00" for i in range(1, 56)]
        self._enviar_raio_x(("jan.csv", relatorio_do_mes(2026, 1, linhas)))
        html = self._html(PAGINA)
        bloco = html.split('id="tabelaScanner"', 1)[0].rsplit("data-tabela-rapida", 1)[1]
        self.assertIn("data-cortada", bloco)
        self.assertIn(f"A lista traz os {raio_x.LIMITE_ITENS_LISTA} produtos com mais dinheiro abaixo do preço normal, "
                      "de 55 com variação de preço.", html)

    def test_avisos_num_card_recolhido(self):
        self._mudar_plano("PRATA")
        self._enviar_raio_x(("jan.csv", CUSTO_ABSURDO))
        html = self._html(PAGINA)
        self.assertIn('<details class="avisos-raio-x" id="avisosRaioX">', html)
        self.assertIn("Ler avisos e detalhes técnicos", html)
        self.assertIn("1 arquivo lido", html)
        # O texto original continua lá dentro, no detalhe técnico.
        self.assertIn("ficaram fora da perda e do potencial recuperável", html)


if __name__ == "__main__":
    unittest.main()
