"""Etapa 2 da Avaliação de Cenário: as bordas da triagem e do diagnóstico, nas funções puras.

Complementa tests/test_triagem_cenario.py (de onde vêm os montadores _linha, cobertura, meta, _lead e
_registros) nas regras corrigidas na Etapa 2 (triagem-2026-11.3):

  1. Ritmo (_ritmo): a janela dos mesmos meses só com os dois últimos anos do envio mensais e o ano
     anterior do produto igual ao do envio; a janela vazia (sem comparação, com aviso no payload); os
     períodos inteiros nos outros casos.
  2. Giro de lançamento: a base é a dos relatórios desde a primeira venda, e o giro crítico exige
     essa base.
  3. Estoque parado: em qualquer bloco, fora a possível ruptura e os itens a confirmar; o impacto do
     item que parou de vender, na ordem dos itens e das categorias.
  4. Margem negativa recorrente: as fronteiras da contagem, o prejuízo mínimo e o relatório único.
  5. Estoque: o último informado, mesmo zero; a matriz sem a coluna de estoque.
  6. Ordenação pelo impacto do último ano; a venda sem a coluna de custo; a oportunidade de margem
     só com motivo de margem.
  7. Margem de referência: sem os itens a confirmar, a do lead na categoria pequena e a categoria do
     ano mais recente.
  8. Fronteiras exatas dos limiares, os 23 limiares padrão e o giro só com relatórios mensais.
  9. Diagnóstico: margem líquida sem receita positiva, linhas de outro lead, categorias demais e dado
     gravado ilegível (meta, limiares e resumos).
 10. Limiares: dígitos, inteiros e a versão.

As linhas são montadas à mão (dados fictícios), no formato de avaliacao_cenario.linha_da_staging.
"""

from decimal import Decimal
from fractions import Fraction
import json
import unittest
import uuid

import diagnostico_cenario
import triagem_cenario
from triagem_cenario import Cobertura, Limiares, LimiaresDeOutraVersao
from tests.test_triagem_cenario import AGORA, D, _lead, _linha, _registros, cobertura, meta


def _por_ano(*linhas):
    """As linhas de um produto por ano, como triar() as agrupa."""
    return {linha["ano_exercicio"]: linha for linha in linhas}


def _ritmo(linhas, base, limiares=None):
    """(queda, giro, vendeu, relatórios da base do giro) do produto no último ano da cobertura."""
    return triagem_cenario._ritmo(_por_ano(*linhas), base, max(base.relatorios), limiares or Limiares())


def _matriz(linhas, anos, **meta_trocas):
    """A matriz por categoria e a triagem, com a cobertura e os ajustes da meta do envio."""
    dados = meta(anos, **meta_trocas)
    base = Cobertura.do_envio(dados)
    triagem = triagem_cenario.triar(linhas, base)
    return diagnostico_cenario.matriz_por_categoria(linhas, base, triagem, dados), triagem


def _json_estrito(payload) -> str:
    """O payload em JSON estrito (sem NaN nem Infinity), conferido na volta."""
    texto = json.dumps(payload, ensure_ascii=False, allow_nan=False)

    def recusar(nome):
        raise AssertionError(f"{nome} no JSON")
    json.loads(texto, parse_constant=recusar)
    return texto


# =============================================================================
# 1. Ritmo e a janela dos mesmos meses
# =============================================================================
class RitmoDaJanelaTests(unittest.TestCase):
    """A queda compara só a janela dos mesmos meses quando os dois últimos anos do envio são mensais
    e o ano anterior do produto é o anterior do envio; nos outros casos, os períodos inteiros."""

    # 2025 inteiro (12 relatórios mensais) e 2026 até 08/11 (10 completos e novembro parcial): a janela
    # vai de janeiro a outubro nos dois anos (304 dias), sem o mês corrente.
    PARCIAL = Cobertura(relatorios={2025: 12, 2026: 11}, dias={2025: 365, 2026: 312},
                        mensais={2025: True, 2026: True}, completos={2025: 12, 2026: 10},
                        dias_mesmos_meses={2025: 304, 2026: 304})

    def test_janela_parcial_compara_so_os_mesmos_meses_nos_dois_anos(self):
        # X: 304 unidades de janeiro a outubro de 2025 (mais 426 em novembro e dezembro) contra 152 de
        # janeiro a outubro de 2026 (mais 8 em novembro): na janela, o ritmo caiu exatamente pela
        # metade. Pelos períodos inteiros seriam 74%; com um lado só na janela, 47% ou 79%.
        x = [_linha("X", 2025, quantidade="730", mesmos_meses="304", recuperavel="30.00"),
             _linha("X", 2026, quantidade="160", mesmos_meses="152", meses=11, recuperavel="30.00")]
        self.assertEqual(_ritmo(x, self.PARCIAL)[0], Fraction(1, 2))
        # Sazonal: o mesmo ritmo de janeiro a outubro, com o pico de novembro e dezembro só em 2025.
        sazonal = [_linha("S", 2025, quantidade="752", mesmos_meses="152"),
                   _linha("S", 2026, quantidade="160", mesmos_meses="152", meses=11)]
        self.assertEqual(_ritmo(sazonal, self.PARCIAL)[0], 0)
        # A venda esperada também é a da janela: 0,99 unidade de janeiro a outubro de 2025 prevê menos
        # de 1 nos 304 dias da janela de 2026 (nos 312 dias cobertos de 2026, passaria de 1).
        rara = [_linha("E", 2025, quantidade="600.99", mesmos_meses="0.99")]
        self.assertIsNone(_ritmo(rara, self.PARCIAL)[0])
        referencia = [_linha("R", 2025), _linha("R", 2026, meses=11)]
        resultado = triagem_cenario.triar(x + sazonal + rara + referencia, self.PARCIAL)
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]),
                         ("urgencia", ("recuperavel_alto", "venda_em_queda_forte")))
        self.assertEqual((resultado.blocos["S"], resultado.blocos["E"]), (None, None))
        self.assertFalse(resultado.sem_mes_em_comum)

    def test_janela_vazia_nao_compara_o_ritmo_e_o_payload_avisa(self):
        # Novembro e dezembro de 2024 contra janeiro e fevereiro de 2025: mensais, sem mês em comum.
        anos = {2024: (2, 61, True), 2025: (2, 59, True)}
        linhas = [_linha("P", 2024, quantidade="100", mesmos_meses="0", meses=2),
                  _linha("Y", 2024, quantidade="100", mesmos_meses="0", meses=2),
                  _linha("Y", 2025, quantidade="10", mesmos_meses="0", meses=2)]
        vazia = Cobertura.do_envio(meta(anos, dias_mesmos_meses_por_ano={"2024": 0, "2025": 0}))
        self.assertEqual(_ritmo(linhas[:1], vazia)[:3], (None, None, False))
        self.assertIsNone(_ritmo(linhas[1:], vazia)[0])
        resultado = triagem_cenario.triar(linhas, vazia)
        self.assertEqual((resultado.blocos, resultado.motivos), ({"P": None, "Y": None}, {"P": (), "Y": ()}))
        self.assertTrue(resultado.sem_mes_em_comum)
        # A janela vazia de um lado só também deixa o ritmo sem comparação.
        for janela in ({2024: 61, 2025: 0}, {2024: 0, 2025: 59}):
            with self.subTest(janela=janela):
                base = Cobertura(relatorios={2024: 2, 2025: 2}, dias={2024: 61, 2025: 59},
                                 mensais={2024: True, 2025: True}, dias_mesmos_meses=janela)
                lado = triagem_cenario.triar(linhas, base)
                self.assertEqual((lado.blocos, lado.sem_mes_em_comum), ({"P": None, "Y": None}, True))
        # Sem a janela na cobertura (montada à mão), valem os períodos inteiros: P parou e Y caiu.
        inteiros = triagem_cenario.triar(linhas, cobertura(a2024=(2, 61, True), a2025=(2, 59, True)))
        self.assertEqual(inteiros.motivos, {"P": ("venda_parou",), "Y": ("venda_em_queda",)})
        self.assertFalse(inteiros.sem_mes_em_comum)
        # O diagnóstico avisa que a queda não foi avaliada; com mês em comum, não.
        aviso = "não têm mês em comum"
        sem_mes = diagnostico_cenario.montar_payload(
            _lead(anos, dias_mesmos_meses_por_ano={"2024": 0, "2025": 0}), _registros(linhas), agora=AGORA,
            retencao_dias=30)
        self.assertEqual(sem_mes["situacao"], "atual")
        self.assertTrue(any(aviso in texto for texto in sem_mes["avisos"]), sem_mes["avisos"])
        com_mes = diagnostico_cenario.montar_payload(_lead(anos), _registros(linhas), agora=AGORA, retencao_dias=30)
        self.assertEqual(com_mes["situacao"], "atual")
        self.assertFalse(any(aviso in texto for texto in com_mes["avisos"]))

    def test_ano_anterior_do_produto_mais_antigo_compara_os_periodos_inteiros(self):
        # Três anos mensais; Z vendeu em 2024 e em 2026, não em 2025. A janela é de 2025 e 2026 (2024,
        # mais antigo, fica com janela 0): Z compara 2026 inteiro com 2024 inteiro, queda de 50%.
        base = Cobertura(relatorios={2024: 12, 2025: 12, 2026: 10}, dias={2024: 366, 2025: 365, 2026: 304},
                         mensais={2024: True, 2025: True, 2026: True},
                         dias_mesmos_meses={2024: 0, 2025: 304, 2026: 304})
        z = [_linha("Z", 2024, quantidade="366", mesmos_meses="0"), _linha("Z", 2026, quantidade="152", meses=10)]
        self.assertEqual(_ritmo(z, base)[0], Fraction(1, 2))
        resultado = triagem_cenario.triar(z + [_linha("R", 2025), _linha("R", 2026, meses=10)], base)
        self.assertEqual((resultado.blocos["Z"], resultado.motivos["Z"]), ("eletiva", ("venda_em_queda",)))
        self.assertFalse(resultado.sem_mes_em_comum)

    def test_ano_anterior_nao_mensal_compara_os_periodos_inteiros(self):
        # 2025 num relatório anual e 2026 em 10 mensais: 1 unidade por dia contra 0,5. A janela do motor
        # fica vazia, e nem uma janela preenchida (cobertura montada à mão) vale sem os dois anos mensais.
        casos = {
            "janela do motor (vazia)": ({2025: 0, 2026: 0}, "0", "0"),
            "janela montada à mão": ({2025: 304, 2026: 304}, "152", "152"),
        }
        for caso, (janela, mesmos_2025, mesmos_2026) in casos.items():
            with self.subTest(caso=caso):
                base = Cobertura(relatorios={2025: 1, 2026: 10}, dias={2025: 365, 2026: 304},
                                 mensais={2025: False, 2026: True}, dias_mesmos_meses=janela)
                w = [_linha("W", 2025, quantidade="365", mesmos_meses=mesmos_2025, meses=1),
                     _linha("W", 2026, quantidade="152", mesmos_meses=mesmos_2026, meses=10)]
                self.assertEqual(_ritmo(w, base)[0], Fraction(1, 2))
                resultado = triagem_cenario.triar(w, base)
                self.assertEqual((resultado.blocos["W"], resultado.motivos["W"], resultado.sem_mes_em_comum),
                                 ("eletiva", ("venda_em_queda",), False))


# =============================================================================
# 2. Giro de lançamento
# =============================================================================
class GiroDeLancamentoTests(unittest.TestCase):
    """O giro de um lançamento (sem venda nos anos anteriores) conta os relatórios desde a primeira
    venda dele (0: todos os do ano); o de quem vendeu antes, todos os relatórios do ano."""

    BASE = cobertura(a2025=(12, 365, True), a2026=(10, 304, True))

    def _triar(self, *linhas, limiares=None):
        referencia = [_linha("R", 2025), _linha("R", 2026, meses=10)]
        return triagem_cenario.triar([*linhas, *referencia], self.BASE, limiares)

    def test_lancamento_que_vendeu_em_todos_os_relatorios_desde_que_comecou_nao_tem_giro_baixo(self):
        # Começou em setembro: vendeu nos 2 relatórios desde então, de 10 no ano. Giro de 100%.
        lancamento = _linha("X", 2026, meses=2, desde=2)
        self.assertEqual(_ritmo([lancamento], self.BASE)[1:], (Fraction(1), True, 2))
        resultado = self._triar(lancamento)
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), (None, ()))

    def test_lancamento_com_base_de_4_relatorios_ou_mais_pode_ter_giro_critico(self):
        # Vendeu em 1 dos 10 relatórios desde a primeira venda: giro de 10%, crítico.
        uma_vez = _linha("X", 2026, meses=1, desde=10, recuperavel="10.00")
        self.assertEqual(_ritmo([uma_vez], self.BASE)[1:], (Fraction(1, 10), True, 10))
        resultado = self._triar(uma_vez)
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), ("urgencia", ("recuperavel_alto", "giro_critico")))
        # Com exatamente 4 relatórios na base, 1 venda é 25%: crítico.
        quatro = self._triar(_linha("X", 2026, meses=1, desde=4, recuperavel="10.00"))
        self.assertEqual(quatro.motivos["X"], ("recuperavel_alto", "giro_critico"))
        # A base precisa de relatorios_para_giro_critico relatórios: com 6 exigidos, 1 venda em 5
        # relatórios (20%) é só giro baixo, mesmo o ano tendo 10; em 6 relatórios, é crítico.
        exigente = Limiares(relatorios_para_giro_critico=6)
        cinco = self._triar(_linha("X", 2026, meses=1, desde=5, recuperavel="10.00"), limiares=exigente)
        self.assertEqual((cinco.blocos["X"], cinco.motivos["X"]), ("eletiva", ("giro_baixo", "recuperavel_menor")))
        seis = self._triar(_linha("X", 2026, meses=1, desde=6, recuperavel="10.00"), limiares=exigente)
        self.assertEqual((seis.blocos["X"], seis.motivos["X"]), ("urgencia", ("recuperavel_alto", "giro_critico")))

    def test_produto_com_venda_no_ano_anterior_usa_todos_os_relatorios_do_ano(self):
        # Vendeu em 2025 e voltou em setembro de 2026: 2 de 10 relatórios, giro de 20% (a contagem
        # desde a primeira venda do ano não vale para quem já vendia).
        volta = [_linha("X", 2025), _linha("X", 2026, meses=2, desde=2)]
        self.assertEqual(_ritmo(volta, self.BASE)[1:], (Fraction(1, 5), True, 10))
        resultado = self._triar(*volta)
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), ("eletiva", ("giro_baixo",)))
        # Lançamento sem a contagem (0, linha montada à mão): também todos os relatórios do ano.
        self.assertEqual(_ritmo([_linha("X", 2026, meses=2)], self.BASE)[1:], (Fraction(1, 5), True, 10))


# =============================================================================
# 3. Estoque parado
# =============================================================================
class EstoqueParadoTests(unittest.TestCase):
    """Todo produto com estagnação crítica e estoque > 0 está em estagnados, em qualquer bloco; o
    diagnóstico soma estoque x custo médio, sem a possível ruptura e sem os itens a confirmar."""

    ANOS = {2025: (12, 365, True), 2026: (10, 304, True)}

    def test_parado_com_estoque_em_qualquer_bloco_fora_a_ruptura_e_o_item_a_confirmar(self):
        linhas = [
            # Parou de vender com 7 unidades a R$ 6,50 de custo médio: Eletiva só pela estagnação.
            _linha("Z", 2025, custo="65.00", estoque_final="7"),
            # Parou sem estoque (zero ou negativo): possível ruptura, na Atenção.
            _linha("Y", 2025, estoque_final="0"), _linha("Y2", 2025, estoque_final="-3"),
            # Parou com 4 unidades e margem de 5%: Atenção pela margem; o estoque parado conta.
            _linha("M", 2025, custo="95.00", estoque_final="4"),
            # Parou com 3 unidades e sem NCM: Eletiva pelo NCM; o estoque parado conta.
            _linha("N", 2025, sem_ncm=True, estoque_final="3"),
            # Custo implausível (R$ 30 por unidade vendida a R$ 10) parado com 5 unidades: a confirmar.
            _linha("I", 2025, custo="300.00", estoque_final="5"),
            _linha("R", 2025, venda="1000.00", custo="600.00"),
            _linha("R", 2026, venda="1000.00", custo="600.00", meses=10),
        ]
        matriz, triagem = _matriz(linhas, self.ANOS)
        self.assertEqual((triagem.blocos["Z"], triagem.motivos["Z"]), ("eletiva", ("estoque_parado", "venda_parou")))
        self.assertEqual((triagem.blocos["M"], triagem.motivos["M"]), ("atencao", ("margem_baixa_relativa",)))
        self.assertEqual((triagem.blocos["N"], triagem.motivos["N"]), ("eletiva", ("sem_ncm",)))
        for chave in ("Y", "Y2"):
            with self.subTest(chave=chave):
                self.assertEqual((triagem.blocos[chave], triagem.motivos[chave]), ("atencao", ("possivel_ruptura",)))
        self.assertIn("I", triagem.a_confirmar)
        self.assertEqual(triagem.estagnados - {"I"}, {"Z", "M", "N"})
        # 7 x 6,50 + 4 x 9,50 + 3 x 6,00; o item a confirmar (5 x 30,00) e as rupturas ficam de fora.
        self.assertEqual(matriz["totais"]["estoque_parado"], 101.5)
        self.assertEqual(diagnostico_cenario.estoque_parado(_por_ano(linhas[0]), "Z", triagem), D("45.5"))
        self.assertIsNone(diagnostico_cenario.estoque_parado(_por_ano(linhas[5]), "I", triagem))

    def test_impacto_do_item_sem_linha_no_ultimo_ano_e_o_estoque_parado(self):
        linhas = [
            # Parou em 2026 com 7 unidades a R$ 6,50 e tinha R$ 0,50 recuperáveis em 2025.
            _linha("Z", 2025, custo="65.00", vazamento="0.50", recuperavel="0.50", estoque_final="7", categoria="ZEBRA"),
            # Vende em 2026 com R$ 2,00 recuperáveis: impacto menor, e o nome viria antes.
            _linha("A", 2026, vazamento="2.00", recuperavel="2.00", meses=10, categoria="ALFA"),
        ]
        dados = meta(self.ANOS)
        base = Cobertura.do_envio(dados)
        triagem = triagem_cenario.triar(linhas, base)
        self.assertEqual((triagem.blocos["Z"], triagem.motivos["Z"]), ("eletiva", ("estoque_parado", "venda_parou")))
        self.assertEqual((triagem.blocos["A"], triagem.motivos["A"]), ("eletiva", ("recuperavel_menor",)))
        # Impacto de 2026: o estoque parado (7 x 6,50); acumulado: com os R$ 0,50 de 2025.
        self.assertEqual(diagnostico_cenario.impacto_do_produto(_por_ano(linhas[0]), "Z", triagem, 2026),
                         (D("45.5"), D("46.0"), "estoque_parado"))
        itens = diagnostico_cenario.itens_dos_blocos(linhas, base, triagem)["eletiva"]["itens"]
        self.assertEqual([(i["produto"], i["impacto"], i["impacto_acumulado"], i["origem_do_impacto"]) for i in itens],
                         [("Produto Z", 45.5, 46.0, "estoque_parado"), ("Produto A", 2.0, 2.0, "potencial_recuperavel")])
        matriz = diagnostico_cenario.matriz_por_categoria(linhas, base, triagem, dados)
        self.assertEqual([(c["categoria"], c["impacto"], c["impacto_acumulado"], c["estoque_parado"])
                          for c in matriz["categorias"]],
                         [("ZEBRA", 45.5, 46.0, 45.5), ("ALFA", 2.0, 2.0, 0.0)])


# =============================================================================
# 4. Margem negativa recorrente
# =============================================================================
class MargemNegativaRecorrenteTests(unittest.TestCase):
    """Com mais de 1 relatório no último ano, sempre pela contagem dos relatórios com custo; com um
    relatório só (o anual), pelo ano negativo."""

    SEIS = cobertura(a2025=(6, 181, True))

    def _negativa(self, relatorios=6, **campos):
        padrao = dict(venda="100.00", custo="130.00", perda="30.00", meses=relatorios, meses_com_custo=relatorios,
                      meses_negativos=min(4, relatorios), ultimo_negativo=True)
        padrao.update(campos)
        return [_linha("X", 2025, **padrao), _linha("R", 2025, venda="1000.00", custo="600.00", meses=relatorios)]

    def test_fronteiras_exatas_da_contagem_e_do_prejuizo_sao_urgencia(self):
        exatos = {
            "3 de 6 relatórios negativos (50%)": {"meses_negativos": 3},
            "exatamente 3 relatórios com custo": {"meses": 3, "meses_com_custo": 3, "meses_negativos": 2},
            "prejuízo de exatamente 3% da venda com custo": {"custo": "103.00", "perda": "3.00"},
        }
        for caso, campos in exatos.items():
            with self.subTest(caso=caso):
                resultado = triagem_cenario.triar(self._negativa(**campos), self.SEIS)
                self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]),
                                 ("urgencia", ("margem_negativa_recorrente",)))
        logo_abaixo = {
            "2 de 5 relatórios negativos": {"meses": 5, "meses_com_custo": 5, "meses_negativos": 2},
            "2 relatórios com custo": {"meses": 2, "meses_com_custo": 2, "meses_negativos": 2},
            "prejuízo de 2,99% da venda com custo": {"custo": "102.99", "perda": "2.99"},
            "o mais recente com custo positivo": {"ultimo_negativo": False},
        }
        for caso, campos in logo_abaixo.items():
            with self.subTest(caso=caso):
                resultado = triagem_cenario.triar(self._negativa(**campos), self.SEIS)
                self.assertNotIn("margem_negativa_recorrente", resultado.motivos["X"])

    def test_dois_relatorios_nao_mensais_com_um_so_com_custo_nao_e_recorrente(self):
        # Dois semestres de 2025, só o segundo com a coluna de custo, e ele negativo: 1 relatório com
        # custo não chega a 3. O ano negativo só vale com um relatório único no ano.
        def linhas(relatorios):
            x = _linha("X", 2025, venda="100.00", com_custo="50.00", fat_rel="50.00", custo="65.00", perda="15.00",
                       meses=relatorios, meses_com_custo=1, meses_negativos=1, ultimo_negativo=True)
            return [x, _linha("R", 2025, venda="1000.00", custo="600.00", meses=relatorios)]
        semestres = triagem_cenario.triar(linhas(2), cobertura(a2025=(2, 365, False)))
        self.assertEqual(semestres.blocos["X"], "atencao")
        self.assertNotIn("margem_negativa_recorrente", semestres.motivos["X"])
        # O mesmo produto num relatório anual único: o ano negativo é recorrente.
        anual = triagem_cenario.triar(linhas(1), cobertura(a2025=(1, 365, False)))
        self.assertEqual((anual.blocos["X"], anual.motivos["X"]), ("urgencia", ("margem_negativa_recorrente",)))

    def test_relatorio_unico_do_ano_vale_pelo_ano(self):
        anual = cobertura(a2025=(1, 365, False))
        negativo = triagem_cenario.triar(self._negativa(relatorios=1), anual)
        self.assertEqual((negativo.blocos["X"], negativo.motivos["X"]), ("urgencia", ("margem_negativa_recorrente",)))
        # Com venda abaixo do custo em parte do ano, mas o ano positivo (o relatório não é negativo): não.
        positivo = triagem_cenario.triar(self._negativa(relatorios=1, custo="90.00", perda="5.00", meses_negativos=0,
                                                        ultimo_negativo=False), anual)
        self.assertNotIn("margem_negativa_recorrente", positivo.motivos["X"])


# =============================================================================
# 5. Estoque informado
# =============================================================================
class EstoqueInformadoTests(unittest.TestCase):
    def test_estoque_zerado_no_ultimo_ano_com_queda_forte_e_possivel_ruptura(self):
        base = cobertura(a2024=(1, 366, False), a2025=(1, 365, False))

        def linhas(estoque_2024, estoque_2025):
            # 100 unidades em 2024 e 30 em 2025 (queda de 70%), com R$ 30 recuperáveis.
            return [_linha("X", 2024, quantidade="100", estoque_final=estoque_2024),
                    _linha("X", 2025, quantidade="30", venda="30.00", custo="18.00", recuperavel="30.00",
                           estoque_final=estoque_2025)]
        # O último estoque informado é o zero de 2025, não os 50 de 2024.
        ruptura = triagem_cenario.triar(linhas("50", "0"), base)
        self.assertEqual((ruptura.blocos["X"], ruptura.motivos["X"]), ("atencao", ("possivel_ruptura",)))
        self.assertNotIn("X", ruptura.estagnados)
        # Ao contrário (zero em 2024 e 50 em 2025): estoque parado, na Urgência.
        parado = triagem_cenario.triar(linhas("0", "50"), base)
        self.assertEqual((parado.blocos["X"], parado.motivos["X"]),
                         ("urgencia", ("recuperavel_alto", "estoque_parado", "venda_em_queda_forte")))
        self.assertIn("X", parado.estagnados)

    def test_matriz_sem_a_coluna_de_estoque_em_nenhum_ano_tem_estoque_parado_nulo(self):
        anos = {2024: (12, 366, True), 2025: (12, 365, True)}
        referencia = [_linha("R", 2024, categoria="BEBIDAS"), _linha("R", 2025, categoria="BEBIDAS")]
        sem_coluna, _ = _matriz([_linha("P", 2024, categoria="BEBIDAS"), *referencia], anos,
                                relatorios_com_estoque_por_ano={"2024": 0, "2025": 0})
        self.assertIsNone(sem_coluna["totais"]["estoque_parado"])
        self.assertEqual([c["estoque_parado"] for c in sem_coluna["categorias"]], [None])
        # Com a coluna em um dos anos: P parou com 5 unidades a R$ 6,00.
        com_coluna, _ = _matriz([_linha("P", 2024, estoque_final="5", categoria="BEBIDAS"), *referencia], anos,
                                relatorios_com_estoque_por_ano={"2024": 12, "2025": 0})
        self.assertEqual(com_coluna["totais"]["estoque_parado"], 30.0)
        self.assertEqual([c["estoque_parado"] for c in com_coluna["categorias"]], [30.0])


# =============================================================================
# 6. Ordenação por impacto
# =============================================================================
class OrdemPorImpactoTests(unittest.TestCase):
    ANOS = {2024: (1, 366, False), 2025: (1, 365, False)}

    def test_impacto_do_ultimo_ano_vem_antes_do_acumulado_nos_itens_e_nas_categorias(self):
        linhas = [
            # ANTIGO: R$ 50 de vazamento em 2024 e nada em 2025; vende mais que RECENTE.
            _linha("ANTIGO", 2024, venda="300.00", custo="180.00", vazamento="50.00", recuperavel="50.00",
                   categoria="ANTIGA"),
            _linha("ANTIGO", 2025, venda="300.00", custo="180.00", categoria="ANTIGA"),
            # RECENTE: R$ 10 de vazamento em 2025.
            _linha("RECENTE", 2024, categoria="NOVA"),
            _linha("RECENTE", 2025, vazamento="10.00", recuperavel="10.00", categoria="NOVA"),
        ]
        dados = meta(self.ANOS)
        base = Cobertura.do_envio(dados)
        triagem = triagem_cenario.triar(linhas, base)
        self.assertEqual((triagem.blocos["ANTIGO"], triagem.blocos["RECENTE"]), ("atencao", "atencao"))
        itens = diagnostico_cenario.itens_dos_blocos(linhas, base, triagem)["atencao"]["itens"]
        self.assertEqual([(i["produto"], i["impacto"], i["impacto_acumulado"]) for i in itens],
                         [("Produto RECENTE", 10.0, 10.0), ("Produto ANTIGO", 0.0, 50.0)])
        matriz = diagnostico_cenario.matriz_por_categoria(linhas, base, triagem, dados)
        self.assertEqual([(c["categoria"], c["impacto"], c["impacto_acumulado"]) for c in matriz["categorias"]],
                         [("NOVA", 10.0, 10.0), ("ANTIGA", 0.0, 50.0)])

    def test_venda_so_em_relatorio_sem_a_coluna_de_custo_tem_impacto_zero(self):
        linhas = [
            # Vendeu R$ 100 só em relatórios sem a coluna de custo: a falta é do arquivo.
            _linha("SEMCOLUNA", 2025, com_custo="0.00", fat_rel="0.00"),
            # R$ 40 dos R$ 100 em relatórios com a coluna, sem custo.
            _linha("PARTE", 2025, com_custo="0.00", fat_rel="40.00"),
            _linha("SEMCUSTO", 2025, com_custo="0.00"),
            _linha("R", 2025, venda="1000.00", custo="600.00"),
        ]
        triagem = triagem_cenario.triar(linhas, cobertura(a2025=(1, 365, False)))

        def impacto(indice):
            linha = linhas[indice]
            return diagnostico_cenario.impacto_do_produto({2025: linha}, linha["chave_produto"], triagem, 2025)
        self.assertEqual(impacto(0), (D("0"), D("0"), None))
        self.assertEqual(impacto(1), (D("40.00"), D("40.00"), "venda_sem_custo"))
        self.assertEqual(impacto(2), (D("100.00"), D("100.00"), "venda_sem_custo"))

    def test_abaixo_da_referencia_sem_motivo_de_margem_nao_ganha_oportunidade_de_margem(self):
        bebidas = [_linha(f"B{i}", 2025, venda="1000.00", custo="600.00", categoria="BEBIDAS") for i in range(3)]
        # X: 30% numa categoria de 39,06%: abaixo da referência, mas acima de 10% e de 60% dela (nenhum
        # motivo de margem). Fica na Eletiva pelo NCM.
        x = _linha("X", 2025, custo="70.00", sem_ncm=True, categoria="BEBIDAS")
        # Y: 20%, abaixo de 60% da referência, com 3% do faturamento: margem abaixo do mix.
        y = _linha("Y", 2025, custo="80.00", categoria="BEBIDAS")
        triagem = triagem_cenario.triar([*bebidas, x, y], cobertura(a2025=(1, 365, False)))
        referencia = Fraction(1250, 3200)
        self.assertEqual((triagem.margem_referencia["X"], triagem.margem_referencia["Y"]), (referencia, referencia))
        self.assertEqual((triagem.motivos["X"], triagem.motivos["Y"]), (("sem_ncm",), ("margem_abaixo_do_mix",)))
        self.assertEqual(diagnostico_cenario.impacto_do_produto({2025: x}, "X", triagem, 2025), (D("0"), D("0"), None))
        # Y: (39,0625% - 20%) x R$ 100 de venda com custo.
        self.assertEqual(diagnostico_cenario.impacto_do_produto({2025: y}, "Y", triagem, 2025),
                         (D("19.0625"), D("19.0625"), "oportunidade_de_margem"))


# =============================================================================
# 7. Margem de referência
# =============================================================================
class MargemDeReferenciaTests(unittest.TestCase):
    ANUAL = cobertura(a2025=(1, 365, False))

    def test_item_de_custo_implausivel_fica_fora_da_referencia(self):
        linhas = [_linha("R", 2025, venda="1000.00", custo="600.00"), _linha("X", 2025, custo="93.00"),
                  # Embalagem cadastrada como unidade: R$ 1.000 de custo para R$ 100 vendidos.
                  _linha("I", 2025, custo="1000.00")]
        resultado = triagem_cenario.triar(linhas, self.ANUAL)
        self.assertIn("I", resultado.a_confirmar)
        # Sem o item a confirmar, o lead tem 37% de margem (com ele, a margem seria negativa e os 7% de X
        # não seriam baixos).
        self.assertEqual(resultado.margem_referencia["X"], Fraction(407, 1100))
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), ("atencao", ("margem_baixa_relativa",)))

    def test_categoria_com_menos_de_3_itens_com_custo_usa_a_margem_do_lead(self):
        loja = [_linha(f"L{i}", 2025, venda="1000.00", custo="600.00", categoria="LOJA") for i in range(3)]
        x = _linha("X", 2025, custo="93.00", categoria="PEQUENA")
        pares = [_linha(f"P{i}", 2025, custo="92.00", categoria="PEQUENA") for i in (2, 3)]
        # 1 ou 2 itens na PEQUENA: a referência de X é o lead (cerca de 38%) e os 7% dele são baixos.
        for itens, referencia in ((1, Fraction(1207, 3100)), (2, Fraction(1215, 3200))):
            with self.subTest(itens_na_categoria=itens):
                resultado = triagem_cenario.triar([*loja, x, *pares[:itens - 1]], self.ANUAL)
                self.assertEqual(resultado.margem_referencia["X"], referencia)
                self.assertEqual(resultado.motivos["X"], ("margem_baixa_relativa",))
        # Com 3 itens, vale a margem da categoria (cerca de 7,7%): os 7% de X não estão 5 pontos abaixo.
        tres = triagem_cenario.triar([*loja, x, *pares], self.ANUAL)
        self.assertEqual((tres.margem_referencia["X"], tres.blocos["X"]), (Fraction(23, 300), None))

    def test_categoria_do_produto_e_a_do_ano_mais_recente(self):
        anos = {2024: (1, 366, False), 2025: (1, 365, False)}
        base = cobertura(a2024=(1, 366, False), a2025=(1, 365, False))
        nova = [_linha(f"N{i}", 2025, custo="92.00", categoria="NOVA") for i in range(3)]
        antiga = [_linha(f"A{i}", 2025, venda="1000.00", custo="600.00", categoria="ANTIGA") for i in range(3)]
        x = [_linha("X", 2024, custo="93.00", categoria="ANTIGA"), _linha("X", 2025, custo="93.00", categoria="NOVA")]
        resultado = triagem_cenario.triar(nova + antiga + x, base)
        # X é da NOVA (7,6% com ele): os 7% dele não estão 5 pontos abaixo. Na ANTIGA (38%), seriam.
        self.assertEqual((resultado.margem_referencia["X"], resultado.blocos["X"]), (Fraction(38, 500), None))
        matriz = diagnostico_cenario.matriz_por_categoria(nova + antiga + x, base, resultado, meta(anos))
        self.assertEqual({c["categoria"]: c["itens"] for c in matriz["categorias"]}, {"NOVA": 4, "ANTIGA": 3})
        # Sem categoria no ano mais recente, vale a do ano anterior que tem uma: X volta para a ANTIGA.
        sem_categoria_em_2025 = [x[0], dict(x[1], categoria_produto="  ")]
        outro = triagem_cenario.triar(nova + antiga + sem_categoria_em_2025, base)
        self.assertEqual((outro.margem_referencia["X"], outro.motivos["X"]),
                         (Fraction(1214, 3200), ("margem_baixa_relativa",)))


# =============================================================================
# 8. Fronteiras exatas dos limiares da Etapa 2
# =============================================================================
class FronteirasDaEtapa2Tests(unittest.TestCase):
    ANUAL = cobertura(a2025=(1, 365, False))

    def test_distancia_de_exatamente_5_pontos_da_referencia_e_margem_baixa(self):
        def linhas(custo_x):
            return [_linha("X", 2025, custo=custo_x), _linha("R", 2025, venda="900.00", custo="778.00")]
        # Lead com 13% de margem e X com 8%: exatamente 5 pontos abaixo.
        no_limite = triagem_cenario.triar(linhas("92.00"), self.ANUAL)
        self.assertEqual(no_limite.margem_referencia["X"], Fraction(13, 100))
        self.assertEqual((no_limite.blocos["X"], no_limite.motivos["X"]), ("atencao", ("margem_baixa_relativa",)))
        # X com 8,01% (e o lead com 13,001%): 4,991 pontos, não.
        perto = triagem_cenario.triar(linhas("91.99"), self.ANUAL)
        self.assertEqual((perto.blocos["X"], perto.motivos["X"]), (None, ()))

    def test_queda_moderada_de_exatamente_20_por_cento(self):
        base = cobertura(a2024=(1, 366, False), a2025=(1, 365, False))

        def linhas(quantidade_2025):
            return [_linha("X", 2024, quantidade="366"), _linha("X", 2025, quantidade=quantidade_2025)]
        # 1 unidade por dia em 2024 e 0,8 por dia em 2025.
        exata = triagem_cenario.triar(linhas("292"), base)
        self.assertEqual((exata.blocos["X"], exata.motivos["X"]), ("eletiva", ("venda_em_queda",)))
        quase = triagem_cenario.triar(linhas("292.001"), base)
        self.assertEqual((quase.blocos["X"], quase.motivos["X"]), (None, ()))

    def test_participacao_de_exatamente_1_por_cento_entra_no_mix(self):
        def linhas(venda_x, custo_x):
            bebidas = [_linha(f"B{i}", 2025, venda="3300.00", custo="1980.00", categoria="BEBIDAS") for i in range(3)]
            return bebidas + [_linha("X", 2025, venda=venda_x, custo=custo_x, categoria="BEBIDAS")]
        # X: R$ 100 de R$ 10.000 (1%), com 20% de margem numa categoria de 39,8%.
        exata = triagem_cenario.triar(linhas("100.00", "80.00"), self.ANUAL)
        self.assertEqual((exata.blocos["X"], exata.motivos["X"]), ("eletiva", ("margem_abaixo_do_mix",)))
        abaixo = triagem_cenario.triar(linhas("99.99", "79.99"), self.ANUAL)
        self.assertEqual((abaixo.blocos["X"], abaixo.motivos["X"]), (None, ()))

    def test_recuperavel_residual_de_exatamente_1_por_cento(self):
        def linhas(recuperavel):
            return [_linha("X", 2025, vazamento=recuperavel, recuperavel=recuperavel), _linha("R", 2025)]
        exato = triagem_cenario.triar(linhas("1.00"), self.ANUAL)
        self.assertEqual((exato.blocos["X"], exato.motivos["X"]), ("eletiva", ("recuperavel_menor",)))
        abaixo = triagem_cenario.triar(linhas("0.99"), self.ANUAL)
        self.assertEqual((abaixo.blocos["X"], abaixo.motivos["X"]), (None, ()))

    def test_matriz_publicada_com_exatamente_metade_do_faturamento_com_categoria(self):
        ano = {2025: (12, 365, True)}

        def linhas(com_categoria, sem_categoria):
            return [_linha("A", 2025, venda=com_categoria, categoria="BEBIDAS"), _linha("B", 2025, venda=sem_categoria)]
        metade, _ = _matriz(linhas("500.00", "500.00"), ano)
        self.assertEqual((metade["categorias_publicadas"], metade["cobertura_categoria"]), (True, 0.5))
        self.assertEqual([c["categoria"] for c in metade["categorias"]], ["BEBIDAS", "Sem categoria"])
        quase, _ = _matriz(linhas("499.99", "500.01"), ano)
        self.assertEqual((quase["categorias_publicadas"], quase["categorias"]), (False, None))

    def test_os_23_limiares_padrao(self):
        gravados = Limiares().para_dict()
        self.assertEqual(gravados, {
            "versao": triagem_cenario.VERSAO_TRIAGEM,
            # Urgência (a)
            "pareto_recuperavel": "0.80", "recuperavel_minimo_do_produto": "0.03", "impacto_minimo_do_lead": "0.001",
            "queda_critica": "0.50", "giro_critico": "0.25", "relatorios_para_giro_critico": 4,
            "venda_esperada_minima": "1",
            # Urgência (b)
            "pareto_prejuizo": "0.80", "prejuizo_minimo_do_produto": "0.03", "relatorios_minimos_margem_negativa": 3,
            "fracao_relatorios_negativos": "0.50",
            # Atenção
            "margem_baixa": "0.10", "distancia_margem_baixa": "0.05", "queda_de_margem": "0.05",
            "vazamento_relevante": "0.03", "abaixo_custo_minimo": "0.01", "razao_custo_implausivel": "3",
            # Eletiva
            "queda_moderada": "0.20", "giro_baixo": "0.50", "corte_cauda_longa": "0.95", "fator_margem_mix": "0.60",
            "participacao_minima_mix": "0.01", "recuperavel_residual_minimo": "0.01",
        })
        self.assertEqual(len(gravados) - 1, 23)
        self.assertEqual(triagem_cenario.LIMIARES_PADRAO, Limiares())

    def test_dois_relatorios_trimestrais_nao_dao_giro(self):
        x = _linha("X", 2026, meses=1)
        trimestres = cobertura(a2026=(2, 181, False))
        self.assertIsNone(_ritmo([x], trimestres)[1])
        resultado = triagem_cenario.triar([x], trimestres)
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), (None, ()))
        # Os mesmos 2 relatórios, mensais: vendeu em 1 de 2, giro baixo.
        mensais = triagem_cenario.triar([x], cobertura(a2026=(2, 59, True)))
        self.assertEqual((mensais.blocos["X"], mensais.motivos["X"]), ("eletiva", ("giro_baixo",)))


# =============================================================================
# 9. Diagnóstico: margem líquida, linhas de outro lead, categorias e dado ilegível
# =============================================================================
class DiagnosticoBordasTests(unittest.TestCase):
    ANO = {2025: (12, 365, True)}

    def _payload(self, lead, linhas):
        return diagnostico_cenario.montar_payload(lead, _registros(linhas), agora=AGORA, retencao_dias=30)

    def test_margem_com_bonificacoes_e_devolucoes_sem_receita_liquida_positiva(self):
        casos = {
            # R$ 150 de receita devolvida (venda do ano anterior) contra R$ 100 vendidos: -50 - (-30).
            "receita líquida negativa": (dict(devolucoes="90.00", receita_devolvida="150.00"), -20.0),
            "receita líquida zero": (dict(devolucoes="60.00", receita_devolvida="100.00"), 0.0),
        }
        for caso, (campos, valor) in casos.items():
            with self.subTest(caso=caso):
                matriz, _ = _matriz([_linha("X", 2025, categoria="BEBIDAS", **campos)], self.ANO)
                for linha in (matriz["totais"], matriz["categorias"][0]):
                    self.assertEqual(linha["margem_com_bonificacoes_e_devolucoes"], {"valor": valor, "percentual": None})
                    self.assertEqual(linha["margem_bruta_estimada"], {"valor": 40.0, "percentual": 0.4})

    def test_payload_recusa_linha_de_outro_lead_ou_sem_lead(self):
        linhas = [_linha("A", 2025, categoria="BEBIDAS"), _linha("B", 2025)]
        proprias = _registros(linhas)
        self.assertEqual(diagnostico_cenario.montar_payload(_lead(self.ANO), proprias, agora=AGORA,
                                                            retencao_dias=30)["situacao"], "atual")
        outro = _registros(linhas[1:], lead_id=uuid.UUID(int=8))
        casos = {
            "linha de outro lead": proprias[:1] + outro,
            "só linhas de outro lead": outro,
            "linha sem lead_id": proprias[:1] + [{k: v for k, v in proprias[1].items() if k != "lead_id"}],
            "lead_id nulo": proprias[:1] + [dict(proprias[1], lead_id=None)],
        }
        for status in ("concluido", "expirado"):
            for caso, registros in casos.items():
                with self.subTest(status=status, caso=caso), self.assertRaises(ValueError):
                    diagnostico_cenario.montar_payload(_lead(self.ANO, status=status), registros, agora=AGORA,
                                                       retencao_dias=30)

    def test_mais_de_60_categorias_publica_60_e_soma_as_demais(self):
        linhas = [_linha(f"P{i:02d}", 2025, venda=f"{1000 - 10 * i}.00", custo=f"{600 - 6 * i}.00", categoria=f"C{i:02d}")
                  for i in range(63)]
        # O item sem categoria tem o maior impacto (R$ 40 recuperáveis) e mesmo assim vai por último.
        linhas.append(_linha("SEM", 2025, venda="50.00", custo="30.00", vazamento="40.00", recuperavel="40.00"))
        matriz, _ = _matriz(linhas, self.ANO)
        nomes = [c["categoria"] for c in matriz["categorias"]]
        self.assertEqual(nomes, [f"C{i:02d}" for i in range(60)] + ["Demais categorias (3)", "Sem categoria"])
        demais, sem = matriz["categorias"][60], matriz["categorias"][61]
        # As 3 de menor faturamento (R$ 400, 390 e 380) somadas numa linha só.
        self.assertEqual((demais["itens"], demais["faturamento"], demais["custo_das_vendas"]), (3, 1170.0, 702.0))
        self.assertEqual((sem["itens"], sem["impacto"]), (1, 40.0))
        totais = matriz["totais"]
        self.assertEqual((totais["itens"], totais["faturamento"], totais["impacto"]), (64, 43520.0, 40.0))
        for campo in ("itens", "faturamento", "venda_com_custo", "custo_das_vendas", "potencial_recuperavel",
                      "vazamento_de_preco", "impacto"):
            with self.subTest(campo=campo):
                self.assertAlmostEqual(sum(c[campo] for c in matriz["categorias"]), totais[campo], places=2)
        # Com exatamente 60 categorias, nenhuma linha de demais.
        sessenta, _ = _matriz(linhas[:60] + linhas[-1:], self.ANO)
        self.assertEqual([c["categoria"] for c in sessenta["categorias"]],
                         [f"C{i:02d}" for i in range(60)] + ["Sem categoria"])

    def test_meta_com_ajuste_fora_do_formato_fica_desatualizado(self):
        zerados = {"custo_bonificacoes": 0, "custo_devolucoes": 0, "devolucoes_com_custo": 0}
        linhas = [_linha("A", 2025, categoria="BEBIDAS")]
        defeitos = {
            "ajuste em texto": {"2025": {**zerados, "custo_bonificacoes": "abc"}},
            "ajuste NaN em texto": {"2025": {**zerados, "custo_devolucoes": "NaN"}},
            "ajuste sem uma chave": {"2025": {"custo_devolucoes": 0, "devolucoes_com_custo": 0}},
            "ajuste nulo": {"2025": {**zerados, "devolucoes_com_custo": None}},
            "ajustes do ano nulos": {"2025": None},
        }
        for caso, ajustes in defeitos.items():
            with self.subTest(caso=caso):
                payload = self._payload(_lead(self.ANO, ajustes_sem_venda_por_ano=ajustes), linhas)
                self.assertEqual((payload["situacao"], payload["totais"], payload["categorias"], payload["blocos"]),
                                 ("desatualizado", None, None, None))
                self.assertNotIn("Produto A", _json_estrito(payload))
        valido = self._payload(_lead(self.ANO, ajustes_sem_venda_por_ano={"2025": zerados}), linhas)
        self.assertEqual(valido["situacao"], "atual")

    def test_meta_sem_os_ajustes_com_numero_em_texto_ou_fora_do_teto_fica_desatualizado(self):
        # A meta gravada vale com o formato e o teto do contrato do processo isolado: sem os ajustes (ou
        # sem o ano), com número em texto, negativo ou acima do teto do dinheiro, o diagnóstico não
        # mostra totais que não fecham com o Raio-X.
        zerados = {"custo_bonificacoes": 0, "custo_devolucoes": 0, "devolucoes_com_custo": 0}
        linhas = [_linha("A", 2025, categoria="BEBIDAS")]
        defeitos = {
            "sem os ajustes": None,
            "ajustes vazios (sem o ano)": {},
            "número em texto": {"2025": {**zerados, "custo_bonificacoes": "9"}},
            "número enorme em texto": {"2025": {**zerados, "custo_bonificacoes": "1e999"}},
            "negativo": {"2025": {**zerados, "custo_devolucoes": -5}},
            "acima do teto": {"2025": {**zerados, "custo_bonificacoes": 1e300}},
            "booleano": {"2025": {**zerados, "custo_bonificacoes": True}},
        }
        for caso, ajustes in defeitos.items():
            with self.subTest(caso=caso):
                lead = _lead(self.ANO)
                dados = json.loads(lead["meta_json"])
                if ajustes is None:
                    dados.pop("ajustes_sem_venda_por_ano")
                else:
                    dados["ajustes_sem_venda_por_ano"] = ajustes
                lead["meta_json"] = json.dumps(dados)
                payload = self._payload(lead, linhas)
                self.assertEqual((payload["situacao"], payload["totais"]), ("desatualizado", None))
                _json_estrito(payload)

    def test_limiares_aninhados_sem_fim_ficam_desatualizado(self):
        linhas = [_linha("A", 2025, categoria="BEBIDAS")]
        aninhados = {"objeto": '{"a":' * 100_000 + "1" + "}" * 100_000, "lista": "[" * 100_000 + "]" * 100_000}
        for caso, texto in aninhados.items():
            with self.subTest(caso=caso):
                lead = _lead(self.ANO)
                lead["limiares_triagem_json"] = texto
                payload = self._payload(lead, linhas)
                self.assertEqual((payload["situacao"], payload["limiares"], payload["blocos"]), ("desatualizado", None, None))
                _json_estrito(payload)

    def test_resumo_com_numero_nao_finito_fica_nulo_e_o_json_e_estrito(self):
        linhas = [_linha("A", 2025, categoria="BEBIDAS")]
        for numero in ("1e999", "-1e999", "NaN", "Infinity", "-Infinity"):
            with self.subTest(numero=numero):
                lead = _lead(self.ANO)
                lead["resumos_json"] = dict(lead["resumos_json"], urgencia='{"itens": 0, "faturamento": ' + numero + "}")
                payload = self._payload(lead, linhas)
                self.assertIsNone(payload["resumos"]["urgencia"])
                self.assertEqual(payload["resumos"]["atencao"], {"itens": 0})
                _json_estrito(payload)

    def test_limiares_com_nan_ficam_nulos_e_o_json_e_estrito(self):
        lead = _lead(self.ANO)
        lead["limiares_triagem_json"] = json.dumps({**Limiares().para_dict(), "margem_baixa": float("nan")})
        payload = self._payload(lead, [_linha("A", 2025, categoria="BEBIDAS")])
        self.assertEqual((payload["situacao"], payload["limiares"]), ("desatualizado", None))
        _json_estrito(payload)

    def test_limiares_de_outra_versao_ficam_desatualizado(self):
        lead = _lead(self.ANO)
        lead["limiares_triagem_json"] = json.dumps({**Limiares().para_dict(), "versao": "triagem-2026-11.2"})
        payload = self._payload(lead, [_linha("A", 2025, categoria="BEBIDAS")])
        self.assertEqual((payload["situacao"], payload["totais"], payload["categorias"], payload["blocos"]),
                         ("desatualizado", None, None, None))
        self.assertNotIn("Produto A", _json_estrito(payload))

    def test_marca_abaixo_do_custo_da_matriz_conta_o_fato_sem_o_motivo(self):
        # Um centavo vendido abaixo do custo: abaixo de 1% da venda com custo, sem o motivo da Atenção.
        linhas = [_linha("X", 2025, perda="0.01", categoria="BEBIDAS"), _linha("R", 2025, categoria="BEBIDAS")]
        matriz, triagem = _matriz(linhas, self.ANO)
        self.assertEqual((triagem.blocos["X"], triagem.motivos["X"]), (None, ()))
        self.assertEqual(matriz["totais"]["marcas"]["abaixo_do_custo"], 1)
        self.assertEqual(matriz["categorias"][0]["marcas"]["abaixo_do_custo"], 1)


# =============================================================================
# 10. Limiares: dígitos, inteiros e versão
# =============================================================================
class LimiaresBordasTests(unittest.TestCase):
    def test_ate_6_digitos_inteiros_e_6_decimais(self):
        base = Limiares().para_dict()
        aceitos = {"margem_baixa": "0.100000", "venda_esperada_minima": "999999",
                   "razao_custo_implausivel": "123456.123456"}
        recusados = {"margem_baixa": "0.1000000", "venda_esperada_minima": "1000000",
                     "razao_custo_implausivel": "1234567.5"}
        for nome, valor in aceitos.items():
            with self.subTest(aceito=nome):
                self.assertEqual(getattr(Limiares.de_dict({**base, nome: valor}), nome), Decimal(valor))
                self.assertEqual(getattr(Limiares().com_ajustes({nome: valor}), nome), Decimal(valor))
                self.assertEqual(getattr(Limiares(**{nome: Decimal(valor)}), nome), Decimal(valor))
        for nome, valor in recusados.items():
            with self.subTest(recusado=nome):
                with self.assertRaises(ValueError):
                    Limiares.de_dict({**base, nome: valor})
                with self.assertRaises(ValueError):
                    Limiares().com_ajustes({nome: valor})
                with self.assertRaises(ValueError):
                    Limiares(**{nome: Decimal(valor)})

    def test_inteiro_acima_de_um_milhao_e_recusado(self):
        base = Limiares().para_dict()
        for nome in ("relatorios_para_giro_critico", "relatorios_minimos_margem_negativa"):
            with self.subTest(nome=nome):
                self.assertEqual(getattr(Limiares(**{nome: 10 ** 6}), nome), 10 ** 6)
                self.assertEqual(getattr(Limiares.de_dict({**base, nome: 10 ** 6}), nome), 10 ** 6)
                self.assertEqual(getattr(Limiares().com_ajustes({nome: 10 ** 6}), nome), 10 ** 6)
                with self.assertRaises(ValueError):
                    Limiares(**{nome: 10 ** 6 + 1})
                with self.assertRaises(ValueError):
                    Limiares.de_dict({**base, nome: 10 ** 6 + 1})
                with self.assertRaises(ValueError):
                    Limiares().com_ajustes({nome: 10 ** 6 + 1})

    def test_limiares_de_outra_versao_sao_um_value_error_proprio(self):
        self.assertTrue(issubclass(LimiaresDeOutraVersao, ValueError))
        base = Limiares().para_dict()
        # A versão é conferida antes das chaves: limiares antigos com outras chaves também são "outra versão".
        for dados in ({**base, "versao": "triagem-2026-11.2"}, {"versao": "triagem-2026-11.2", "margem_baixa": "0.1"}):
            with self.subTest(chaves=len(dados)), self.assertRaises(LimiaresDeOutraVersao):
                Limiares.de_dict(dados)
        # Chave a mais na versão atual é gravação ilegível, não outra versão.
        with self.assertRaises(ValueError) as erro:
            Limiares.de_dict({**base, "extra": "1"})
        self.assertNotIsInstance(erro.exception, LimiaresDeOutraVersao)


if __name__ == "__main__":
    unittest.main()
