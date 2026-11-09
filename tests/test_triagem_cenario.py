"""Triagem da Avaliação de Cenário (triagem_cenario) e diagnóstico (diagnostico_cenario), puros.

  1. Limiares: os padrão, a validação por tipo e relação, a gravação e a leitura estritas, os ajustes
     e o resultado que muda com eles.
  2. Marcas (flags) por produto e ano, nas bordas: com e sem a coluna de custo, custo parcial, perda de
     um centavo, cada fonte de omissão no ERP e as contagens do lead.
  3. Blocos: cada motivo de Urgência, Atenção e Eletiva sozinho, a precedência, a cauda longa num
     catálogo grande e a independência da ordem das linhas.
  4. Ritmo e giro: anos de durações diferentes, a mesma janela de meses (item sazonal) e o giro só com
     relatórios mensais.
  5. Fronteiras exatas (Fraction): queda de 50%, venda esperada de 1 unidade, margem de 10%, queda de
     margem de 5 pontos, Pareto com empate.
  6. Diagnóstico: matriz por categoria, impacto, venda contínua e payload (retenção, versão, limite de
     itens, nenhum contato do lead).

As linhas são montadas à mão (dados fictícios), no formato de avaliacao_cenario.linha_da_staging.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import random
import unittest
import uuid

import avaliacao_motor
import diagnostico_cenario
import triagem_cenario
from triagem_cenario import Cobertura, Limiares

D = Decimal


def _linha(chave, ano, *, quantidade="10", venda="100.00", com_custo=None, custo="60.00", perda="0.00",
           vazamento="0.00", recuperavel="0.00", meses=12, conflito=False, estoque=False, fat_rel=None,
           meses_com_custo=None, meses_negativos=0, ultimo_negativo=False, meses_completos=None,
           mesmos_meses=None, estoque_final=None, categoria=None, sem_ncm=False, sem_categoria=False,
           bonificacoes="0.00", devolucoes="0.00", receita_devolvida="0.00", codigo=None, desde=0):
    com_custo = venda if com_custo is None else com_custo
    tem_custo = D(com_custo) > 0
    return {
        "ano_exercicio": ano, "chave_produto": chave, "codigo_produto": codigo, "descricao_produto": f"Produto {chave}",
        "quantidade": D(quantidade), "valor_venda_bruto": D(venda),
        "quantidade_com_custo": D(quantidade) if tem_custo else D("0"),
        "faturamento_com_custo": D(com_custo), "custo_total": D(custo) if tem_custo else D("0"),
        "perda": D(perda), "vazamento_preco": D(vazamento), "recuperavel": D(recuperavel),
        "linhas": meses, "meses": meses, "variacao": False, "conflito_cadastral": conflito, "estoque_negativo": estoque,
        "faturamento_em_relatorio_com_custo": D(venda if fat_rel is None else fat_rel),
        "meses_com_custo": (meses if tem_custo else 0) if meses_com_custo is None else meses_com_custo,
        "meses_margem_negativa": meses_negativos, "ultimo_mes_negativo": ultimo_negativo,
        "meses_completos": meses if meses_completos is None else meses_completos,
        "quantidade_mesmos_meses": D(quantidade if mesmos_meses is None else mesmos_meses),
        "estoque_final": None if estoque_final is None else D(estoque_final), "categoria_produto": categoria,
        "ncm_ausente": sem_ncm, "categoria_ausente": sem_categoria, "custo_bonificacoes": D(bonificacoes),
        "custo_devolucoes": D(devolucoes), "devolucoes_com_custo": D(receita_devolvida),
        # 0: a triagem usa todos os relatórios do ano como base do giro (produto com venda antes).
        "relatorios_desde_primeira_venda": desde,
    }


def cobertura(**anos):
    """cobertura(a2025=(relatórios, dias, mensais)), sem as contagens da Etapa 2 (valem como presentes)."""
    return Cobertura(relatorios={int(a[1:]): r for a, (r, _d, _m) in anos.items()},
                     dias={int(a[1:]): d for a, (_r, d, _m) in anos.items()},
                     mensais={int(a[1:]): m for a, (_r, _d, m) in anos.items()})


def meta(anos: dict, **trocas) -> dict:
    """Meta completa do envio (avaliacao_motor._meta) para anos {ano: (relatórios, dias, mensais)}."""
    texto = {str(a): v for a, v in anos.items()}
    dados = {
        "versao_regras": avaliacao_motor.VERSAO_REGRAS_LEAD, "versao_raio_x": "raio-x-2026-11.4",
        "arquivos": sum(r for r, _d, _m in anos.values()),
        "arquivos_por_ano": {a: r for a, (r, _d, _m) in texto.items()},
        "dias_por_ano": {a: d for a, (_r, d, _m) in texto.items()},
        "relatorios_mensais_por_ano": {a: m for a, (_r, _d, m) in texto.items()},
        "arquivos_com_custo": sum(r for r, _d, _m in anos.values()), "linhas_lidas": 10, "qualidade": {},
        "situacao": {}, "celulas_recodificadas": 0,
        **{chave: {a: r for a, (r, _d, _m) in texto.items()} for chave in avaliacao_motor.CHAVES_RELATORIOS_POR_ANO},
        "dias_mesmos_meses_por_ano": {a: d for a, (_r, d, _m) in texto.items()},
        "relatorios_cabecalho_ambiguo": 0,
        "ajustes_sem_venda_por_ano": {a: {chave: 0 for chave in avaliacao_motor.CHAVES_AJUSTES} for a in texto},
        # Etapa 3: cada ano coberto do dia 1º de janeiro até os dias do ano (sem passar de 31/12).
        "periodos_cobertos": [
            [f"{a}-01-01", min(date(int(a), 1, 1) + timedelta(days=d - 1), date(int(a), 12, 31)).isoformat()]
            for a, (_r, d, _m) in sorted(texto.items())],
        "relatorios_varias_unidades": 0,
    }
    dados.update(trocas)
    return dados


CENARIO = [
    # Urgência: recuperável alto e parou de vender no último ano.
    _linha("U", 2024, venda="1000.00", custo="600.00", vazamento="200.00", recuperavel="200.00"),
    # Urgência: recuperável alto e venda média caiu 60%.
    _linha("Q", 2024, quantidade="100", venda="1000.00", custo="500.00", recuperavel="80.00", perda="80.00"),
    _linha("Q", 2025, quantidade="40", venda="500.00", custo="250.00", recuperavel="70.00", perda="70.00"),
    # Atenção: margem de 5% (o lead tem cerca de 41%).
    _linha("M", 2024, custo="95.00"), _linha("M", 2025, custo="95.00"),
    # Atenção: o custo sumiu em 2025 (o relatório tem a coluna de custo).
    _linha("S", 2024), _linha("S", 2025, com_custo="0.00"),
    # Atenção: vendido abaixo do custo em 2025.
    _linha("B", 2024), _linha("B", 2025, venda="90.00", custo="100.00", perda="10.00", recuperavel="10.00"),
    # Eletiva: venda média caiu 30%, margem saudável, nada a recuperar.
    _linha("E", 2024, quantidade="100"), _linha("E", 2025, quantidade="70"),
    # Nenhum bloco.
    _linha("N", 2024), _linha("N", 2025),
]
# 12 relatórios mensais em cada ano, cobrindo o ano inteiro.
RELATORIOS = cobertura(a2024=(12, 366, True), a2025=(12, 365, True))


# =============================================================================
# 1. Limiares
# =============================================================================
class LimiaresTests(unittest.TestCase):
    def test_padrao_e_gravacao(self):
        padrao = Limiares()
        self.assertEqual((padrao.pareto_recuperavel, padrao.queda_critica, padrao.margem_baixa, padrao.corte_cauda_longa,
                          padrao.relatorios_para_giro_critico), (D("0.80"), D("0.50"), D("0.10"), D("0.95"), 4))
        gravado = padrao.para_dict()
        self.assertEqual(gravado["versao"], triagem_cenario.VERSAO_TRIAGEM)
        self.assertEqual((gravado["margem_baixa"], gravado["relatorios_para_giro_critico"]), ("0.10", 4))
        self.assertTrue(all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in gravado.values()))

    def test_ida_e_volta_pelo_json_e_exata(self):
        lido = Limiares.de_dict(json.loads(json.dumps(Limiares().para_dict())))
        self.assertEqual(lido, Limiares())
        # A margem de exatamente 10% continua "não baixa" depois da ida e volta (Decimal em texto, nunca float).
        linhas = [_linha("X", 2025, venda="3.00", custo="2.70"), _linha("R", 2025, venda="1000.00", custo="500.00")]
        self.assertEqual(triagem_cenario.triar(linhas, cobertura(a2025=(1, 365, False)), lido).blocos["X"], None)

    def test_validacao_recusa_tipo_faixa_e_relacao(self):
        invalidos = {
            "float": {"margem_baixa": 0.1}, "fração acima de 1": {"pareto_recuperavel": D("1.5")},
            "NaN": {"queda_critica": D("NaN")}, "inteiro zero": {"relatorios_para_giro_critico": 0},
            "booleano como inteiro": {"relatorios_minimos_margem_negativa": True},
            "inteiro como Decimal": {"relatorios_para_giro_critico": D("4")},
            "queda moderada acima da crítica": {"queda_moderada": D("0.6")},
            "giro crítico acima do baixo": {"giro_critico": D("0.6")},
            "razão de implausível 1": {"razao_custo_implausivel": D("1")},
            "venda esperada negativa": {"venda_esperada_minima": D("-1")},
        }
        for caso, campos in invalidos.items():
            with self.subTest(caso=caso), self.assertRaises(ValueError):
                Limiares(**campos)
        self.assertEqual(Limiares(venda_esperada_minima=D("2")).venda_esperada_minima, D("2"))

    def test_leitura_estrita(self):
        base = Limiares().para_dict()
        defeitos = {
            "não é dicionário": [], "chave faltando": {k: v for k, v in base.items() if k != "margem_baixa"},
            "chave a mais": {**base, "extra": "1"}, "float": {**base, "margem_baixa": 0.1},
            "expoente": {**base, "margem_baixa": "1e-1"}, "NaN": {**base, "margem_baixa": "NaN"},
            "infinito": {**base, "margem_baixa": "Infinity"}, "outra versão": {**base, "versao": "triagem-antiga"},
            "inteiro em texto": {**base, "relatorios_para_giro_critico": "4"},
        }
        for caso, dados in defeitos.items():
            with self.subTest(caso=caso), self.assertRaises(ValueError):
                Limiares.de_dict(dados)

    def test_ajustes_trocam_so_o_pedido(self):
        ajustado = Limiares().com_ajustes({"margem_baixa": "0.08", "relatorios_para_giro_critico": 6})
        self.assertEqual((ajustado.margem_baixa, ajustado.relatorios_para_giro_critico, ajustado.queda_critica),
                         (D("0.08"), 6, D("0.50")))
        for ajustes in ({"inexistente": "1"}, {"queda_moderada": "0.9"}, {"margem_baixa": 0.08}, []):
            with self.subTest(ajustes=ajustes), self.assertRaises(ValueError):
                Limiares().com_ajustes(ajustes)

    def test_limiares_por_parametro_mudam_o_resultado(self):
        # Margem de 9% num lead de 50%: baixa com o padrão (10%); com 8%, não.
        linhas = [_linha("X", 2025, custo="91.00"), _linha("R", 2025, venda="1000.00", custo="500.00")]
        base = cobertura(a2025=(1, 365, False))
        self.assertEqual(triagem_cenario.triar(linhas, base).motivos["X"], ("margem_baixa_relativa",))
        self.assertNotIn("margem_baixa_relativa",
                         triagem_cenario.triar(linhas, base, Limiares(margem_baixa=D("0.08"))).motivos["X"])
        with self.assertRaises(ValueError):
            triagem_cenario.triar(linhas, base, {"margem_baixa": "0.08"})

    def test_rotulo_usa_os_limiares_e_o_historico(self):
        self.assertEqual(triagem_cenario.rotulo_do_motivo("venda_em_queda_forte"), "Ritmo de venda caiu 50% ou mais")
        self.assertEqual(triagem_cenario.rotulo_do_motivo("venda_em_queda_forte", Limiares(queda_critica=D("0.4"))),
                         "Ritmo de venda caiu 40% ou mais")
        self.assertEqual(triagem_cenario.rotulo_do_motivo("perda_abaixo_do_custo"), "Vendas abaixo do custo")
        self.assertEqual(triagem_cenario.rotulo_do_motivo("venda_sem_custo"), "Venda sem custo cadastrado")
        self.assertEqual(triagem_cenario.rotulo_do_motivo("codigo_desconhecido"), "codigo_desconhecido")


# =============================================================================
# 2. Marcas (flags) por produto e ano
# =============================================================================
class MarcasTests(unittest.TestCase):
    def test_venda_sem_custo_so_nos_relatorios_com_a_coluna(self):
        sem_custo = triagem_cenario.marcas(_linha("S", 2025, com_custo="0.00"))
        self.assertEqual((sem_custo["flag_venda_sem_custo"], sem_custo["flag_venda_abaixo_custo"],
                          sem_custo["custo_medio_registrado"], sem_custo["flag_omissao_erp"]), (True, False, None, False))
        # O relatório não trazia a coluna de custo: a falta é do arquivo, não do ERP.
        sem_coluna = triagem_cenario.marcas(_linha("S", 2025, com_custo="0.00", fat_rel="0.00"))
        self.assertEqual((sem_coluna["flag_venda_sem_custo"], sem_coluna["flag_omissao_erp"]), (False, False))
        # Custo em parte das vendas: não é "sem custo", é omissão (custo parcial).
        parcial = _linha("S", 2025, com_custo="40.00", custo="20.00")
        self.assertEqual((triagem_cenario.marcas(parcial)["flag_venda_sem_custo"],
                          triagem_cenario.motivos_de_omissao(parcial)), (False, ("custo_parcial",)))
        # Relatórios com e sem a coluna no mesmo ano: o custo parcial só conta nos que têm a coluna.
        misto = _linha("S", 2025, venda="100.00", com_custo="40.00", custo="20.00", fat_rel="40.00")
        self.assertEqual(triagem_cenario.motivos_de_omissao(misto), ())

    def test_venda_abaixo_do_custo_e_o_fato_e_o_motivo_tem_materialidade(self):
        centavo = _linha("X", 2025, perda="0.01")
        self.assertTrue(triagem_cenario.marcas(centavo)["flag_venda_abaixo_custo"])
        linhas = [centavo, _linha("R", 2025)]
        self.assertNotIn("abaixo_do_custo", triagem_cenario.triar(linhas, cobertura(a2025=(1, 365, False))).motivos["X"])
        relevante = [_linha("X", 2025, perda="1.00"), _linha("R", 2025)]
        self.assertIn("abaixo_do_custo", triagem_cenario.triar(relevante, cobertura(a2025=(1, 365, False))).motivos["X"])

    def test_cada_fonte_de_omissao_no_erp(self):
        casos = {
            "conflito_de_cadastro": _linha("X", 2025, conflito=True),
            "custo_parcial": _linha("X", 2025, com_custo="50.00", custo="30.00"),
            "custo_implausivel": _linha("X", 2025, custo="300.00"),
            "estoque_negativo": _linha("X", 2025, estoque=True),
            "sem_ncm": _linha("X", 2025, sem_ncm=True),
            "sem_categoria": _linha("X", 2025, sem_categoria=True),
        }
        for motivo, linha in casos.items():
            with self.subTest(motivo=motivo):
                self.assertEqual(triagem_cenario.motivos_de_omissao(linha), (motivo,))
                self.assertTrue(triagem_cenario.marcas(linha)["flag_omissao_erp"])
        self.assertFalse(triagem_cenario.marcas(_linha("X", 2025))["flag_omissao_erp"])
        # Cabeçalho ambíguo no envio: o conflito de cadastro não é avaliado.
        self.assertFalse(triagem_cenario.marcas(casos["conflito_de_cadastro"], conflito_confiavel=False)["flag_omissao_erp"])

    def test_custo_medio_com_4_casas(self):
        marcas = triagem_cenario.marcas(_linha("B", 2025, quantidade="3", custo="100.00"))
        self.assertEqual(marcas["custo_medio_registrado"], D("33.3333"))

    def test_contagens_do_lead(self):
        resultado = triagem_cenario.triar(CENARIO, RELATORIOS)
        # S ficou sem custo em 2025; Q e B venderam abaixo do custo; ninguém tem omissão.
        self.assertEqual((resultado.itens_venda_sem_custo, resultado.itens_venda_abaixo_custo,
                          resultado.itens_omissao_erp), (1, 2, 0))
        com_omissao = CENARIO + [_linha("O", 2025, sem_ncm=True), _linha("O", 2024, conflito=True)]
        resultado = triagem_cenario.triar(com_omissao, RELATORIOS)
        # As omissões saem na ordem fixa dos rótulos, não na dos anos.
        self.assertEqual((resultado.itens_omissao_erp, resultado.omissoes["O"]), (1, ("conflito_de_cadastro", "sem_ncm")))
        ambiguo = triagem_cenario.triar([_linha("O", 2025, conflito=True)], Cobertura(
            relatorios={2025: 1}, dias={2025: 365}, mensais={2025: False}, cabecalho_ambiguo=1))
        self.assertEqual((ambiguo.itens_omissao_erp, ambiguo.blocos["O"]), (0, None))


# =============================================================================
# 3. Blocos
# =============================================================================
class BlocosTests(unittest.TestCase):
    def test_cenario_de_referencia(self):
        resultado = triagem_cenario.triar(CENARIO, RELATORIOS)
        self.assertEqual(resultado.blocos, {"U": "urgencia", "Q": "urgencia", "M": "atencao", "S": "atencao",
                                            "B": "atencao", "E": "eletiva", "N": None})
        self.assertEqual(resultado.motivos["U"], ("recuperavel_alto", "venda_parou"))
        self.assertEqual(resultado.motivos["Q"], ("recuperavel_alto", "venda_em_queda_forte"))
        self.assertEqual(resultado.motivos["M"], ("margem_baixa_relativa",))
        self.assertEqual(resultado.motivos["S"], ("sem_custo",))
        self.assertEqual(resultado.motivos["B"], ("abaixo_do_custo", "margem_em_queda"))
        self.assertEqual(resultado.motivos["E"], ("venda_em_queda",))
        urgencia = resultado.resumos["urgencia"]
        self.assertEqual((urgencia["itens"], urgencia["faturamento"], urgencia["recuperavel"]), (2, "2500.00", "350.00"))
        self.assertEqual(urgencia["motivos"], {"recuperavel_alto": 2, "venda_em_queda_forte": 1, "venda_parou": 1})
        self.assertEqual((resultado.resumos["atencao"]["itens"], resultado.resumos["eletiva"]["itens"]), (3, 1))
        self.assertEqual(urgencia["versao"], triagem_cenario.VERSAO_TRIAGEM)
        self.assertNotIn("Produto", json.dumps(resultado.resumos, ensure_ascii=False))

    def test_triagem_nao_depende_da_ordem_das_linhas(self):
        referencia = triagem_cenario.triar(CENARIO, RELATORIOS)
        embaralhado = list(CENARIO)
        random.Random(7).shuffle(embaralhado)
        self.assertEqual(triagem_cenario.triar(embaralhado, RELATORIOS), referencia)

    def test_todo_motivo_tem_rotulo(self):
        for codigo in triagem_cenario.ROTULOS_DOS_MOTIVOS:
            self.assertNotIn("{", triagem_cenario.rotulo_do_motivo(codigo))
        for codigo in triagem_cenario.ROTULOS_DAS_OMISSOES:
            self.assertTrue(triagem_cenario.ROTULOS_DAS_OMISSOES[codigo])

    def test_precedencia_urgencia_atencao_eletiva(self):
        # O mesmo produto com motivo de Urgência e de Atenção fica só na Urgência, com os motivos dela.
        linhas = [_linha("X", 2024, recuperavel="30.00", vazamento="30.00"), _linha("R", 2024), _linha("R", 2025)]
        resultado = triagem_cenario.triar(linhas, RELATORIOS)
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), ("urgencia", ("recuperavel_alto", "venda_parou")))
        # Atenção e Eletiva: fica na Atenção.
        linhas = [_linha("X", 2024, quantidade="100"), _linha("X", 2025, quantidade="70", custo="95.00"),
                  _linha("R", 2024), _linha("R", 2025)]
        self.assertEqual(triagem_cenario.triar(linhas, RELATORIOS).blocos["X"], "atencao")


class UrgenciaTests(unittest.TestCase):
    def _parou(self, estoque_final=None, recuperavel="30.00"):
        return [_linha("X", 2024, recuperavel=recuperavel, estoque_final=estoque_final),
                _linha("R", 2024), _linha("R", 2025)]

    def test_estagnacao_com_estoque_e_estoque_parado(self):
        resultado = triagem_cenario.triar(self._parou(estoque_final="12"), RELATORIOS)
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]),
                         ("urgencia", ("recuperavel_alto", "estoque_parado", "venda_parou")))

    def test_estagnacao_sem_estoque_e_possivel_ruptura_na_atencao(self):
        for estoque in ("0", "-3"):
            with self.subTest(estoque=estoque):
                resultado = triagem_cenario.triar(self._parou(estoque_final=estoque), RELATORIOS)
                self.assertEqual(resultado.blocos["X"], "atencao")
                self.assertIn("possivel_ruptura", resultado.motivos["X"])

    def test_sem_a_coluna_de_estoque_e_queda_de_venda(self):
        resultado = triagem_cenario.triar(self._parou(), RELATORIOS)
        self.assertEqual(resultado.motivos["X"], ("recuperavel_alto", "venda_parou"))

    def test_impacto_minimo_do_lead(self):
        # R$ 30 recuperáveis (30% do produto) num lead de R$ 1 milhão: abaixo de 0,1% do lead, não é Urgência.
        linhas = self._parou() + [_linha("G", 2024, venda="500000.00"), _linha("G", 2025, venda="500000.00")]
        resultado = triagem_cenario.triar(linhas, RELATORIOS)
        self.assertNotEqual(resultado.blocos["X"], "urgencia")
        self.assertIn("recuperavel_menor", resultado.motivos["X"])

    def test_custo_implausivel_sai_dos_paretos(self):
        # 20 itens legítimos com R$ 40 recuperáveis e um erro de embalagem com R$ 92.000 de "perda".
        legitimos = [_linha(f"L{i:02d}", 2024, recuperavel="40.00") for i in range(20)]
        erro = [_linha("CAIXA", 2024, venda="10000.00", custo="92000.00", perda="82000.00", recuperavel="82000.00")]
        resultado = triagem_cenario.triar(legitimos + erro + [_linha("R", 2025)], RELATORIOS)
        self.assertEqual(sum(1 for i in range(20) if resultado.blocos[f"L{i:02d}"] == "urgencia"), 16)
        self.assertEqual(resultado.blocos["CAIXA"], "atencao")
        self.assertIn("custo_implausivel", resultado.motivos["CAIXA"])
        self.assertIn("CAIXA", resultado.a_confirmar)

    def _negativa(self, **campos):
        padrao = dict(venda="100.00", custo="130.00", perda="30.00", meses=6, meses_com_custo=6, meses_negativos=4,
                      ultimo_negativo=True)
        padrao.update(campos)
        return [_linha("X", 2025, **padrao), _linha("R", 2025, venda="1000.00", custo="600.00")]

    def test_margem_negativa_recorrente(self):
        base = cobertura(a2025=(6, 181, True))
        resultado = triagem_cenario.triar(self._negativa(), base)
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), ("urgencia", ("margem_negativa_recorrente",)))
        nao_recorrente = {
            "2 de 6 relatórios negativos": {"meses_negativos": 2},
            "o mais recente com custo positivo": {"ultimo_negativo": False},
            "só 2 relatórios com custo": {"meses": 2, "meses_com_custo": 2, "meses_negativos": 2},
            "prejuízo abaixo de 3% da venda": {"custo": "102.00", "perda": "2.00"},
        }
        for caso, campos in nao_recorrente.items():
            with self.subTest(caso=caso):
                self.assertNotIn("margem_negativa_recorrente",
                                 triagem_cenario.triar(self._negativa(**campos), base).motivos["X"])

    def test_margem_negativa_com_relatorio_nao_mensal_exige_o_ultimo_ano_negativo(self):
        base = cobertura(a2025=(1, 365, False))
        resultado = triagem_cenario.triar(self._negativa(meses=1, meses_com_custo=1, meses_negativos=1), base)
        self.assertIn("margem_negativa_recorrente", resultado.motivos["X"])
        positivo = triagem_cenario.triar(self._negativa(meses=1, meses_com_custo=1, custo="90.00", perda="5.00"), base)
        self.assertNotIn("margem_negativa_recorrente", positivo.motivos["X"])


class AtencaoTests(unittest.TestCase):
    BASE = cobertura(a2024=(1, 366, False), a2025=(1, 365, False))

    def _motivos(self, *linhas):
        resultado = triagem_cenario.triar([*linhas, _linha("R", 2024, venda="1000.00", custo="600.00"),
                                           _linha("R", 2025, venda="1000.00", custo="600.00")], self.BASE)
        return resultado.blocos["X"], resultado.motivos["X"]

    def test_cada_motivo_sozinho(self):
        casos = {
            "sem_custo": [_linha("X", 2025, com_custo="0.00")],
            "abaixo_do_custo": [_linha("X", 2025, perda="5.00")],
            "margem_em_queda": [_linha("X", 2024, custo="60.00"), _linha("X", 2025, custo="66.00")],
            "margem_baixa_relativa": [_linha("X", 2025, custo="92.00")],
            "vazamento_de_preco": [_linha("X", 2025, vazamento="3.00", recuperavel="0.50")],
            "custo_parcial": [_linha("X", 2025, com_custo="50.00", custo="30.00")],
            "custo_implausivel": [_linha("X", 2025, custo="300.00")],
            "conflito_de_cadastro": [_linha("X", 2025, conflito=True)],
        }
        for motivo, linhas in casos.items():
            with self.subTest(motivo=motivo):
                bloco, motivos = self._motivos(*linhas)
                self.assertEqual(bloco, "atencao")
                self.assertIn(motivo, motivos)

    def test_margem_estrutural_da_categoria_nao_e_baixa(self):
        # Combustível com 8% num posto em que toda a categoria tem cerca de 8%: não é "margem baixa".
        combustiveis = [_linha(f"C{i}", 2025, custo="92.00", categoria="COMBUSTIVEIS") for i in range(4)]
        loja = [_linha(f"L{i}", 2025, custo="60.00", categoria="CONVENIENCIA") for i in range(4)]
        resultado = triagem_cenario.triar(combustiveis + loja, cobertura(a2025=(1, 365, False)))
        self.assertTrue(all(resultado.blocos[f"C{i}"] is None for i in range(4)))
        # O mesmo produto num lead sem categoria compara com a margem do lead (cerca de 24%): é baixa.
        sem_categoria = [dict(l, categoria_produto=None) for l in combustiveis + loja]
        self.assertEqual(triagem_cenario.triar(sem_categoria, cobertura(a2025=(1, 365, False))).motivos["C0"],
                         ("margem_baixa_relativa",))


class EletivaTests(unittest.TestCase):
    def test_cada_motivo_sozinho(self):
        mensal = cobertura(a2024=(12, 366, True), a2025=(12, 365, True))
        casos = {
            "venda_em_queda": ([_linha("X", 2024, quantidade="100"), _linha("X", 2025, quantidade="70")], mensal),
            "giro_baixo": ([_linha("X", 2025, meses=6)], mensal),
            "estoque_negativo": ([_linha("X", 2025, estoque=True)], mensal),
            "sem_ncm": ([_linha("X", 2025, sem_ncm=True)], mensal),
            "sem_categoria": ([_linha("X", 2025, sem_categoria=True)], mensal),
            "recuperavel_menor": ([_linha("X", 2025, recuperavel="2.00")], mensal),
        }
        for motivo, (linhas, base) in casos.items():
            with self.subTest(motivo=motivo):
                resultado = triagem_cenario.triar(linhas + [_linha("R", 2024), _linha("R", 2025)], base)
                self.assertEqual(resultado.blocos["X"], "eletiva")
                self.assertIn(motivo, resultado.motivos["X"])

    def test_recuperavel_menor_tem_piso(self):
        resultado = triagem_cenario.triar([_linha("X", 2025, recuperavel="0.50"), _linha("R", 2025)],
                                          cobertura(a2025=(1, 365, False)))
        self.assertEqual(resultado.blocos["X"], None)

    def test_margem_abaixo_do_mix_relativa_a_categoria(self):
        # Categoria com 40% de margem: um item com 20% e participação relevante é candidato a mix.
        itens = [_linha(f"B{i}", 2025, custo="60.00", categoria="BEBIDAS") for i in range(3)]
        itens.append(_linha("X", 2025, custo="80.00", categoria="BEBIDAS"))
        resultado = triagem_cenario.triar(itens, cobertura(a2025=(1, 365, False)))
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), ("eletiva", ("margem_abaixo_do_mix",)))
        # Com participação abaixo de 1% do faturamento, não.
        grande = itens + [_linha("G", 2025, venda="100000.00", custo="60000.00", categoria="OUTRA")]
        self.assertNotIn("margem_abaixo_do_mix", triagem_cenario.triar(grande, cobertura(a2025=(1, 365, False))).motivos["X"])

    def test_cauda_longa_exige_giro_baixo_e_venda_em_ano_anterior(self):
        mensal = cobertura(a2024=(12, 366, True), a2025=(12, 365, True))
        grandes = [_linha(f"G{i}", a, venda="10000.00", custo="6000.00") for i in range(3) for a in (2024, 2025)]
        cauda = [_linha("T", 2024, venda="10.00", custo="6.00"), _linha("T", 2025, venda="10.00", custo="6.00", meses=3)]
        resultado = triagem_cenario.triar(grandes + cauda, mensal)
        self.assertIn("cauda_longa", resultado.motivos["T"])
        # Lançamento (só no último ano) e item de cauda que vende todo mês ficam de fora.
        lancamento = [_linha("T", 2025, venda="10.00", custo="6.00", meses=3)]
        self.assertNotIn("cauda_longa", triagem_cenario.triar(grandes + lancamento, mensal).motivos["T"])
        continuo = [cauda[0], _linha("T", 2025, venda="10.00", custo="6.00", meses=12)]
        self.assertNotIn("cauda_longa", triagem_cenario.triar(grandes + continuo, mensal).motivos["T"])

    def test_catalogo_grande_nao_inunda_a_eletiva(self):
        # 1.000 itens com faturamento de Pareto, todos vendendo em 10 a 12 dos 12 meses, margem saudável:
        # a Classe C (metade dos itens) não vira Eletiva sozinha.
        gerador = random.Random(11)
        linhas = []
        for i in range(1000):
            venda = D(str(round(10000 / (i + 1) ** 1.1, 2))) + D("1.00")
            meses = gerador.choice((10, 11, 12))
            for ano in (2024, 2025):
                linhas.append(_linha(f"P{i:04d}", ano, venda=str(venda), custo=str((venda * D("0.65")).quantize(D("0.01"))),
                                     meses=meses))
        resultado = triagem_cenario.triar(linhas, cobertura(a2024=(12, 366, True), a2025=(12, 365, True)))
        self.assertGreater(len(resultado.cauda), 300)
        self.assertLess(sum(1 for b in resultado.blocos.values() if b == "eletiva"), 50)

    def test_venda_parou_sem_outro_motivo_fica_na_eletiva(self):
        resultado = triagem_cenario.triar([_linha("X", 2024, quantidade="2.44"), _linha("R", 2025)],
                                          cobertura(a2024=(12, 366, True), a2025=(5, 150, True)))
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), ("eletiva", ("venda_parou",)))


# =============================================================================
# 4. Ritmo e giro
# =============================================================================
class RitmoTests(unittest.TestCase):
    """O ritmo de venda é por dia coberto pelos relatórios: anos de durações diferentes não viram
    queda falsa, e o giro só existe com relatórios mensais."""

    def test_ano_inteiro_e_semestre_corrente_no_mesmo_ritmo_nao_e_queda(self):
        linhas = [_linha("X", 2025, quantidade="120", venda="1200.00", custo="600.00", recuperavel="300.00", meses=1),
                  _linha("X", 2026, quantidade="60", venda="600.00", custo="300.00", recuperavel="150.00", meses=1)]
        resultado = triagem_cenario.triar(linhas, cobertura(a2025=(1, 365, False), a2026=(1, 181, False)))
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), ("eletiva", ("recuperavel_menor",)))

    def test_relatorio_anual_e_mensais_no_mesmo_ritmo_nao_e_queda(self):
        linhas = [_linha("X", 2025, quantidade="120", meses=1), _linha("X", 2026, quantidade="100", meses=10)]
        resultado = triagem_cenario.triar(linhas, cobertura(a2025=(1, 365, False), a2026=(10, 304, True)))
        self.assertEqual(resultado.blocos["X"], None)

    def test_queda_de_ritmo_de_verdade_continua_aparecendo(self):
        linhas = [_linha("X", 2025, quantidade="120", recuperavel="30.00", meses=1),
                  _linha("X", 2026, quantidade="20", recuperavel="0.00", meses=6)]
        resultado = triagem_cenario.triar(linhas, cobertura(a2025=(1, 365, False), a2026=(6, 181, True)))
        # 120 por ano (0,33 por dia) contra 20 em meio ano (0,11 por dia): queda de 66%.
        self.assertEqual(resultado.motivos["X"], ("recuperavel_alto", "venda_em_queda_forte"))

    def test_mes_curto_sem_venda_so_conta_quando_a_venda_era_esperada(self):
        linhas = [_linha("LENTO", 2025, quantidade="6", recuperavel="10.00", meses=1),
                  _linha("RAPIDO", 2025, quantidade="365", recuperavel="10.00", meses=1),
                  _linha("OUTRO", 2026, quantidade="10", meses=1)]
        resultado = triagem_cenario.triar(linhas, cobertura(a2025=(1, 365, False), a2026=(1, 31, True)))
        # LENTO previa 0,5 unidade em 31 dias: não vender não diz nada. RAPIDO previa 31.
        self.assertNotIn("venda_parou", resultado.motivos["LENTO"])
        self.assertIn("venda_parou", resultado.motivos["RAPIDO"])

    def test_item_sazonal_compara_os_mesmos_meses(self):
        # Panetone: 300 unidades em novembro e dezembro de 2025, nada de janeiro a outubro de 2026. Nos
        # mesmos meses (janeiro a outubro), 2025 também não vendeu: não "parou".
        base = Cobertura(relatorios={2025: 12, 2026: 10}, dias={2025: 365, 2026: 304}, mensais={2025: True, 2026: True},
                         dias_mesmos_meses={2025: 304, 2026: 304})
        linhas = [_linha("P", 2025, quantidade="300", recuperavel="200.00", meses=2, mesmos_meses="0"),
                  _linha("R", 2025, venda="1000.00"), _linha("R", 2026, venda="1000.00", meses=10)]
        resultado = triagem_cenario.triar(linhas, base)
        self.assertNotIn("venda_parou", resultado.motivos["P"])
        self.assertNotEqual(resultado.blocos["P"], "urgencia")
        # Sem a janela (relatório anual), o ano inteiro de 2025 previa venda: parou.
        anual = cobertura(a2025=(1, 365, False), a2026=(10, 304, True))
        self.assertIn("venda_parou", triagem_cenario.triar(linhas, anual).motivos["P"])

    def test_giro_so_com_relatorios_mensais_no_ultimo_ano(self):
        linhas = [_linha("X", 2026, quantidade="10", meses=1)]
        # Vendeu em 1 dos 4 relatórios mensais: giro de 25%. Num relatório que cobre os 4 meses de uma
        # vez, a mesma venda não diz em quantos meses o produto vendeu.
        mensais = triagem_cenario.triar(linhas, cobertura(a2026=(4, 120, True)))
        anual = triagem_cenario.triar(linhas, cobertura(a2026=(1, 120, False)))
        self.assertEqual((mensais.blocos["X"], mensais.motivos["X"]), ("eletiva", ("giro_baixo",)))
        self.assertEqual(anual.blocos["X"], None)

    def test_cobertura_da_meta_do_envio(self):
        dados = meta({2025: (1, 365, False), 2026: (3, 90, True)})
        base = Cobertura.do_envio(dados)
        self.assertEqual((base.relatorios, base.dias, base.mensais, base.com_custo, base.dias_mesmos_meses),
                         ({2025: 1, 2026: 3}, {2025: 365, 2026: 90}, {2025: False, 2026: True}, {2025: 1, 2026: 3},
                          {2025: 365, 2026: 90}))
        invalidas = (
            {}, [], {"arquivos_por_ano": {"2025": 1}}, {**dados, "dias_por_ano": {"2025": 365}},
            {**dados, "dias_por_ano": {"2025": 0, "2026": 90}}, {**dados, "arquivos_por_ano": {"2025": "1", "2026": 3}},
            {**dados, "relatorios_mensais_por_ano": {"2025": 1, "2026": True}},
            {**dados, "relatorios_com_custo_por_ano": {"2025": 2, "2026": 3}},
            {**dados, "relatorios_com_ncm_por_ano": {"2025": 1}},
            {**dados, "dias_mesmos_meses_por_ano": {"2025": 400, "2026": 90}},
            {**dados, "relatorios_cabecalho_ambiguo": -1},
            {k: v for k, v in dados.items() if k != "relatorios_completos_por_ano"},
        )
        for meta_invalida in invalidas:
            with self.subTest(meta=str(meta_invalida)[:80]), self.assertRaises(ValueError):
                Cobertura.do_envio(meta_invalida)

    def test_linha_de_ano_fora_da_cobertura_e_recusada(self):
        with self.assertRaises(ValueError):
            triagem_cenario.triar([_linha("X", 2024)], cobertura(a2025=(1, 365, False)))


# =============================================================================
# 5. Fronteiras exatas
# =============================================================================
class FronteirasDaTriagemTests(unittest.TestCase):
    """Os limiares valem exatamente no valor da constante (fração exata, sem o erro de uma divisão em
    ponto flutuante) e os cortes da triagem: Pareto, giro crítico e três anos."""

    def test_queda_de_exatamente_50_por_cento_entre_meses_de_28_e_30_dias(self):
        # 2,8 kg em fevereiro (28 dias) e 1,5 kg em abril (30 dias): o ritmo caiu exatamente pela metade.
        # Em ponto flutuante, 1 - (1,5/30)/(2,8/28) dá 0,4999...
        def linhas(abril):
            return [_linha("X", 2025, quantidade="2.8", recuperavel="10.00", meses=1),
                    _linha("X", 2026, quantidade=abril, recuperavel="10.00", meses=1)]
        base = cobertura(a2025=(1, 28, True), a2026=(1, 30, True))
        exata = triagem_cenario.triar(linhas("1.5"), base)
        self.assertEqual((exata.blocos["X"], exata.motivos["X"]), ("urgencia", ("recuperavel_alto", "venda_em_queda_forte")))
        menor = triagem_cenario.triar(linhas("1.501"), base)
        self.assertEqual((menor.blocos["X"], menor.motivos["X"]), ("eletiva", ("venda_em_queda", "recuperavel_menor")))

    def test_venda_esperada_de_exatamente_uma_unidade(self):
        # 2,44 kg em 2024 (366 dias) prevê exatamente 1 kg em 150 dias de 2025: sem venda nesse período,
        # o produto parou. Em ponto flutuante, 2,44/366 x 150 dá 0,9999...
        base = cobertura(a2024=(12, 366, True), a2025=(5, 150, True))
        parou = triagem_cenario.triar([_linha("X", 2024, quantidade="2.44")], base)
        self.assertEqual((parou.blocos["X"], parou.motivos["X"]), ("eletiva", ("venda_parou",)))
        lento = triagem_cenario.triar([_linha("X", 2024, quantidade="2.439")], base)
        self.assertEqual((lento.blocos["X"], lento.motivos["X"]), (None, ()))

    def test_margem_de_exatamente_10_por_cento_nao_e_baixa(self):
        # Em ponto flutuante, a margem de R$ 3,00 com custo de R$ 2,70 dá 0,0999..., subtraindo em
        # Decimal ou em float antes da divisão. A referência (o lead) tem margem de 50%; o item fica
        # abaixo de 1% do faturamento, fora da regra de mix.
        base = cobertura(a2025=(1, 365, False))
        referencia = _linha("R", 2025, venda="1000.00", custo="500.00")
        no_limite = triagem_cenario.triar([_linha("X", 2025, venda="3.00", custo="2.70"), referencia], base)
        abaixo = triagem_cenario.triar([_linha("X", 2025, venda="3.00", custo="2.71"), referencia], base)
        self.assertEqual((no_limite.blocos["X"], abaixo.motivos["X"]), (None, ("margem_baixa_relativa",)))

    def test_margem_em_queda_de_exatamente_5_pontos(self):
        # Margem de 30% para 25%: em ponto flutuante, 0,30 - 0,25 dá 0,0499...
        base = cobertura(a2024=(1, 366, False), a2025=(1, 365, False))

        def linhas(custo_2025):
            return [_linha("X", 2024, custo="70.00"), _linha("X", 2025, custo=custo_2025)]
        caiu = triagem_cenario.triar(linhas("75.00"), base)
        self.assertEqual((caiu.blocos["X"], caiu.motivos["X"]), ("atencao", ("margem_em_queda",)))
        quase = triagem_cenario.triar(linhas("74.99"), base)
        self.assertEqual((quase.blocos["X"], quase.motivos["X"]), (None, ()))
        subiu = triagem_cenario.triar([_linha("X", 2024, custo="75.00"), _linha("X", 2025, custo="70.00")], base)
        self.assertEqual(subiu.blocos["X"], None)

    def test_corte_de_pareto_em_80_por_cento_e_empate(self):
        base = cobertura(a2024=(12, 366, True), a2025=(12, 365, True))

        def pararam(**recuperaveis):
            # Produtos que pararam de vender em 2025, cada um com R$ 100,00 de faturamento em 2024.
            return [_linha(chave, 2024, recuperavel=valor) for chave, valor in recuperaveis.items()]
        resultado = triagem_cenario.triar(pararam(A="50.00", B="30.00", C="15.00", D="5.00"), base)
        # A e B somam 80% do recuperável: o corte para em B; C e D ficam com o recuperável menor.
        self.assertEqual(resultado.blocos, {"A": "urgencia", "B": "urgencia", "C": "eletiva", "D": "eletiva"})
        self.assertEqual(resultado.motivos["A"], ("recuperavel_alto", "venda_parou"))
        self.assertEqual(resultado.motivos["C"], ("recuperavel_menor",))
        # Empate no corte: a chave desempata, sempre do mesmo jeito (A, B e M somam os 80%; Z fica de fora).
        empate = pararam(A="40.00", Z="20.00", M="20.00", B="20.00")
        esperado = {"A": "urgencia", "B": "urgencia", "M": "urgencia", "Z": "eletiva"}
        self.assertEqual(triagem_cenario.triar(empate, base).blocos, esperado)
        self.assertEqual(triagem_cenario.triar(list(reversed(empate)), base).blocos, esperado)

    def test_giro_critico_com_recuperavel_alto_vai_para_urgencia(self):
        quatro = cobertura(a2026=(4, 120, True))
        uma_vez = triagem_cenario.triar([_linha("X", 2026, recuperavel="10.00", meses=1)], quatro)
        self.assertEqual((uma_vez.blocos["X"], uma_vez.motivos["X"]), ("urgencia", ("recuperavel_alto", "giro_critico")))
        duas_vezes = triagem_cenario.triar([_linha("X", 2026, recuperavel="10.00", meses=2)], quatro)
        self.assertEqual((duas_vezes.blocos["X"], duas_vezes.motivos["X"]), ("eletiva", ("giro_baixo", "recuperavel_menor")))
        tres = triagem_cenario.triar([_linha("X", 2026, recuperavel="10.00", meses=1)], cobertura(a2026=(3, 90, True)))
        self.assertEqual((tres.blocos["X"], tres.motivos["X"]), ("eletiva", ("giro_baixo", "recuperavel_menor")))

    def test_tres_anos_comparam_o_ultimo_com_o_anterior_mais_recente_com_venda(self):
        base = cobertura(a2024=(1, 366, False), a2025=(1, 365, False), a2026=(1, 365, False))
        linhas = [
            # X: 2025 e 2026 no mesmo ritmo; a queda contra 2024 não conta.
            _linha("X", 2024, quantidade="100"), _linha("X", 2025, quantidade="50"), _linha("X", 2026, quantidade="50"),
            # Y: sem venda em 2025; 2026 compara com 2024 (100 em 366 dias contra 40 em 365: queda de 60%).
            _linha("Y", 2024, quantidade="100"), _linha("Y", 2026, quantidade="40"),
        ]
        resultado = triagem_cenario.triar(linhas, base)
        self.assertEqual(resultado.blocos["X"], None)
        self.assertEqual((resultado.blocos["Y"], resultado.motivos["Y"]), ("eletiva", ("venda_em_queda",)))


# =============================================================================
# 6. Diagnóstico: matriz por categoria, impacto e payload
# =============================================================================
AGORA = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


LEAD_ID = uuid.UUID(int=7)


def _registros(linhas, *, versao=avaliacao_motor.VERSAO_REGRAS_LEAD, dias_atras=1, lead_id=LEAD_ID):
    return [dict(l, versao_regras=versao, criado_em=AGORA - timedelta(days=dias_atras), lead_id=lead_id)
            for l in linhas]


def _lead(anos, *, limiares=None, versao_triagem=triagem_cenario.VERSAO_TRIAGEM, status="concluido", **meta_trocas):
    return {
        "id": LEAD_ID, "status": status, "versao_regras": avaliacao_motor.VERSAO_REGRAS_LEAD,
        "versao_triagem": versao_triagem, "periodo_inicio": date(2025, 1, 1), "periodo_fim": date(2025, 12, 31),
        "arquivos": 12, "score_saude_cadastral": D("87.50"), "plano_recomendado_codigo": "PRATA",
        "vazamento_total_reais": D("10.00"), "itens_venda_sem_custo": 0, "itens_venda_abaixo_custo": 0,
        "itens_omissao_erp": 0, "resumos_json": {b: '{"itens": 0}' for b in triagem_cenario.BLOCOS},
        "meta_json": json.dumps(meta(anos, **meta_trocas)),
        "limiares_triagem_json": json.dumps((limiares or Limiares()).para_dict()),
        # Campos de contato que nunca podem sair no payload (o montador recebe só o que precisa).
        "nome_contato": "Contato fictício", "email": "lead@example.invalid",
    }


ANO = {2025: (12, 365, True)}


class DiagnosticoTests(unittest.TestCase):
    def _matriz(self, linhas, anos=ANO, **meta_trocas):
        dados = meta(anos, **meta_trocas)
        base = Cobertura.do_envio(dados)
        triagem = triagem_cenario.triar(linhas, base)
        return diagnostico_cenario.matriz_por_categoria(linhas, base, triagem, dados), triagem

    def test_matriz_por_categoria_soma_e_ordena_por_impacto(self):
        linhas = [
            _linha("A1", 2025, venda="1000.00", custo="600.00", recuperavel="10.00", categoria="BEBIDAS",
                   bonificacoes="5.00", devolucoes="20.00", receita_devolvida="30.00"),
            _linha("A2", 2025, venda="500.00", custo="300.00", categoria="Bebidas "),
            _linha("B1", 2025, venda="400.00", custo="380.00", perda="30.00", recuperavel="30.00", categoria="LIMPEZA"),
            _linha("S1", 2025, venda="50.00", custo="20.00"),
        ]
        ajustes = {"2025": {"custo_bonificacoes": 7, "custo_devolucoes": 0, "devolucoes_com_custo": 0}}
        matriz, _ = self._matriz(linhas, ajustes_sem_venda_por_ano=ajustes)
        categorias = matriz["categorias"]
        self.assertEqual([c["categoria"] for c in categorias], ["LIMPEZA", "BEBIDAS", "Sem categoria"])
        bebidas = categorias[1]
        self.assertEqual((bebidas["itens"], bebidas["faturamento"], bebidas["custo_das_vendas"]), (2, 1500.0, 900.0))
        # CMV líquido = 900 + 5 - 20; margem com bonificações e devoluções = (1500 - 30) - 885.
        self.assertEqual((bebidas["cmv_liquido"], bebidas["margem_com_bonificacoes_e_devolucoes"]["valor"]), (885.0, 585.0))
        self.assertEqual(bebidas["margem_bruta_estimada"], {"valor": 600.0, "percentual": 0.4})
        totais = matriz["totais"]
        # O total soma também o ajuste de produto sem venda no ano (R$ 7 de bonificação).
        self.assertEqual((totais["custo_bonificacoes"], totais["cmv_liquido"]), (12.0, 1292.0))
        self.assertIsNone(totais["perdas_operacionais"])
        self.assertTrue(matriz["categorias_publicadas"])

    def test_matriz_so_publicada_com_categoria_em_metade_do_faturamento(self):
        linhas = [_linha("A", 2025, venda="100.00", categoria="BEBIDAS"), _linha("B", 2025, venda="900.00")]
        matriz, _ = self._matriz(linhas)
        self.assertFalse(matriz["categorias_publicadas"])
        self.assertIsNone(matriz["categorias"])
        self.assertEqual(matriz["totais"]["faturamento"], 1000.0)

    def test_venda_continua_so_com_ultimo_ano_mensal(self):
        linhas = [_linha("A", 2025, venda="100.00", meses=12), _linha("B", 2025, venda="50.00", meses=8),
                  _linha("C", 2025, venda="30.00", meses=9)]
        matriz, _ = self._matriz(linhas)
        # 9 de 12 relatórios completos é 75%: entra; 8 de 12 não.
        self.assertEqual(matriz["totais"]["faturamento_venda_continua"], 130.0)
        anual, _ = self._matriz([dict(l, meses=1, linhas=1, meses_completos=1, meses_com_custo=1) for l in linhas],
                                anos={2025: (1, 365, False)})
        self.assertIsNone(anual["totais"]["faturamento_venda_continua"])

    def test_impacto_e_o_maior_componente_e_o_implausivel_fica_a_confirmar(self):
        linhas = [
            _linha("V", 2025, venda="100.00", recuperavel="12.00"),
            _linha("S", 2025, venda="300.00", com_custo="0.00"),
            _linha("I", 2025, venda="100.00", custo="400.00", perda="300.00", recuperavel="300.00"),
        ]
        dados = meta(ANO)
        base = Cobertura.do_envio(dados)
        triagem = triagem_cenario.triar(linhas, base)
        produtos = {l["chave_produto"]: {2025: l} for l in linhas}
        self.assertEqual(diagnostico_cenario.impacto_do_produto(produtos["V"], "V", triagem, 2025)[0], D("12.00"))
        # Venda sem custo exposta: R$ 300 vendidos nos relatórios com a coluna de custo, sem custo.
        self.assertEqual(diagnostico_cenario.impacto_do_produto(produtos["S"], "S", triagem, 2025)[:2],
                         (D("300.00"), D("300.00")))
        self.assertEqual(diagnostico_cenario.impacto_do_produto(produtos["I"], "I", triagem, 2025),
                         (D("0"), D("0"), "a_confirmar"))

    def test_payload_atual_sem_contato_com_limite_e_serializavel(self):
        linhas = [_linha(f"P{i}", 2025, venda=f"{100 + i}.00", com_custo="0.00", categoria="BEBIDAS") for i in range(40)]
        payload = diagnostico_cenario.montar_payload(_lead(ANO), _registros(linhas), agora=AGORA, retencao_dias=30, limite=5)
        self.assertEqual(payload["situacao"], "atual")
        self.assertEqual(payload["audiencia"], "consultor")
        atencao = payload["blocos"]["atencao"]
        self.assertEqual((atencao["total"], atencao["mostrando"], len(atencao["itens"])), (40, 5, 5))
        # Ordenado por impacto (a venda sem custo exposta): o de maior venda primeiro.
        self.assertEqual(atencao["itens"][0]["produto"], "Produto P39")
        texto = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        for proibido in ("Contato fictício", "lead@example.invalid", "nome_contato", "whatsapp", "nome_loja"):
            self.assertNotIn(proibido, texto)
        self.assertEqual(payload["limiares"], Limiares().para_dict())

    def test_payload_vencido_ou_expirado_so_com_o_resumo(self):
        linhas = [_linha("A", 2025, categoria="BEBIDAS")]
        vencido = diagnostico_cenario.montar_payload(_lead(ANO), _registros(linhas, dias_atras=31), agora=AGORA,
                                                     retencao_dias=30)
        expirado = diagnostico_cenario.montar_payload(_lead(ANO, status="expirado"), _registros(linhas), agora=AGORA,
                                                      retencao_dias=30)
        sem_linhas = diagnostico_cenario.montar_payload(_lead(ANO), [], agora=AGORA, retencao_dias=30)
        for payload in (vencido, expirado, sem_linhas):
            self.assertEqual(payload["situacao"], "expirado")
            self.assertIsNone(payload["blocos"])
            self.assertIsNone(payload["categorias"])
            texto = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("Produto A", texto)
            self.assertNotIn("BEBIDAS", texto)

    def test_payload_de_outra_versao_fica_desatualizado(self):
        linhas = [_linha("A", 2025, categoria="BEBIDAS")]
        antigas = diagnostico_cenario.montar_payload(_lead(ANO), _registros(linhas, versao="lead-2026-11.2"),
                                                     agora=AGORA, retencao_dias=30)
        triagem_antiga = diagnostico_cenario.montar_payload(_lead(ANO, versao_triagem="triagem-2026-11.2"),
                                                            _registros(linhas), agora=AGORA, retencao_dias=30)
        limiares_ilegiveis = _lead(ANO)
        limiares_ilegiveis["limiares_triagem_json"] = '{"margem_baixa": NaN}'
        corrompido = diagnostico_cenario.montar_payload(limiares_ilegiveis, _registros(linhas), agora=AGORA,
                                                        retencao_dias=30)
        for payload in (antigas, triagem_antiga, corrompido):
            self.assertEqual(payload["situacao"], "desatualizado")
            self.assertIsNone(payload["categorias"])
            self.assertNotIn("Produto A", json.dumps(payload, ensure_ascii=False))

    def test_payload_usa_os_limiares_gravados_no_lead(self):
        linhas = [_linha("X", 2025, custo="91.00"), _linha("R", 2025, venda="1000.00", custo="500.00")]
        padrao = diagnostico_cenario.montar_payload(_lead(ANO), _registros(linhas), agora=AGORA, retencao_dias=30)
        proprios = diagnostico_cenario.montar_payload(_lead(ANO, limiares=Limiares(margem_baixa=D("0.08"))),
                                                      _registros(linhas), agora=AGORA, retencao_dias=30)
        self.assertEqual(padrao["blocos"]["atencao"]["total"], 1)
        self.assertEqual(proprios["blocos"]["atencao"]["total"], 0)
        self.assertEqual(proprios["limiares"]["margem_baixa"], "0.08")


if __name__ == "__main__":
    unittest.main()
