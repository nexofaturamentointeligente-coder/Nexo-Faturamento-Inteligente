"""Bordas da Etapa 2 no motor real da Avaliação de Cenário (avaliacao_motor), com a triagem por cima.

Os relatórios CSV fictícios passam por avaliar_como_o_worker (a mesma validação, a ida e volta em JSON
e a conferência do contrato do processo pai), lidos em 08/11/2026. Quando o teste precisa da triagem,
as linhas são quantizadas como a gravação faz (avaliacao_cenario.quantizar) e triadas com a cobertura
da meta do envio (triagem_cenario.Cobertura.do_envio).

  1. Janela dos mesmos meses: ano inteiro contra janeiro a outubro, doze meses de novembro a outubro
     (sem mês em comum), só outubro em comum, o mês corrente parcial fora dos dois lados e o ano mais
     antigo fora da janela.
  2. Giro de lançamento: os relatórios desde a primeira venda e o mínimo do giro crítico.
  3. Relatório sem a coluna de custo: nem "sem custo" nem custo parcial; "sem custo" só no ano
     anterior também conta.
  4. Margem negativa por relatório: o mais recente positivo tira da Urgência; empate não é negativo.
  5. Estoque: zero é estoque informado, a célula vazia não; a linha sem venda no mesmo arquivo informa.
  6. Cabeçalho ambíguo: "Cód. Grupo" não alimenta NCM; "Cód. NCM" não alimenta categoria.
  7. Categoria com NBSP, TAB, espaço fino e espaço de largura zero vira uma categoria só.
  8. Teto do dinheiro da meta: bonificação de produto sem venda e valor cancelado.
  9. Nome e código com caractere de controle saem limpos e passam no contrato.
"""

from calendar import monthrange
from dataclasses import replace
from datetime import date
import unittest

import avaliacao_motor
from etl_inputs import ETLInputError
import raio_x
import triagem_cenario
from tests.raio_x_support import avaliar_como_o_worker
from tests.test_avaliacao_cenario import _Arquivos, _linhas, _relatorio

HOJE = date(2026, 11, 8)
# Panetone vendido abaixo da tabela (R$ 27 contra R$ 30): R$ 300 de vazamento por mês, o que o põe
# entre os recuperáveis altos do lead.
PANETONE = "4;Panetone;100;30,00;2700,00;1800,00;9"


def _relatorio_com(cabecalho, ano, mes, linhas):
    """Relatório de um mês com outras colunas (o _relatorio de test_avaliacao_cenario traz sempre
    "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque")."""
    fim = monthrange(ano, mes)[1]
    return (f"Relatorio de vendas por produto\nPeriodo: 01/{mes:02d}/{ano} a {fim:02d}/{mes:02d}/{ano}\n"
            f"{cabecalho}\n" + "".join(linha + "\n" for linha in linhas)).encode("utf-8")


def _item(codigo, nome, quantidade, preco, custo, estoque=9):
    """Linha no formato de _relatorio, em reais inteiros: total = quantidade x preço e custo total =
    quantidade x custo unitário."""
    return f"{codigo};{nome};{quantidade};{preco},00;{quantidade * preco},00;{quantidade * custo},00;{estoque}"


def _chave(nome_normalizado):
    return avaliacao_motor.chave_produto(nome_normalizado)


def _por_produto(resultado):
    """{(ano, descrição): linha da matriz}."""
    return {(linha["ano_exercicio"], linha["descricao_produto"]): linha for linha in _linhas(resultado)}


def _triagem(resultado, **trocas_na_cobertura):
    """A triagem como a gravação faz: linhas quantizadas e a cobertura da meta do envio. As trocas
    servem de controle (dias_mesmos_meses=None compara os períodos inteiros)."""
    import avaliacao_cenario
    linhas = avaliacao_cenario.quantizar(resultado["linhas"])
    cobertura = replace(triagem_cenario.Cobertura.do_envio(resultado["meta"]), **trocas_na_cobertura)
    return triagem_cenario.triar(linhas, cobertura)


class _Motor(_Arquivos):
    def avaliar(self, *relatorios):
        return avaliar_como_o_worker(self.caminhos(*relatorios), hoje=HOJE)


# =============================================================================
# 1. Janela dos mesmos meses
# =============================================================================
class JanelaDosMesmosMesesTests(_Motor):
    def test_ano_inteiro_contra_janeiro_a_outubro_compara_so_os_mesmos_meses(self):
        # 2025 de janeiro a dezembro e 2026 de janeiro a outubro: a janela é janeiro a outubro nos dois.
        relatorios = []
        for mes in range(1, 13):
            natal = mes >= 11
            linhas = [_item(1, "Arroz", 50 if natal else 10, 5, 3), _item(2, "Vinho", 400 if natal else 100, 10, 6),
                      _item(3, "Cerveja", 100, 4, 2)]
            relatorios.append(_relatorio(2025, mes, linhas + ([PANETONE] if natal else [])))
        for mes in range(1, 11):
            relatorios.append(_relatorio(2026, mes, [_item(1, "Arroz", 10, 5, 3), _item(2, "Vinho", 95, 10, 6),
                                                     _item(3, "Cerveja", 70, 4, 2)]))
        resultado = self.avaliar(*relatorios)
        self.assertEqual(resultado["meta"]["dias_mesmos_meses_por_ano"], {"2025": 304, "2026": 304})
        linhas = _por_produto(resultado)
        panetone, arroz = linhas[(2025, "Panetone")], linhas[(2025, "Arroz")]
        # Panetone: só novembro e dezembro, fora da janela. Arroz: só a venda de janeiro a outubro.
        self.assertEqual((panetone["quantidade"], panetone["quantidade_mesmos_meses"]), (200, 0))
        self.assertEqual((arroz["quantidade"], arroz["quantidade_mesmos_meses"]), (200, 100))
        self.assertEqual(linhas[(2026, "Arroz")]["quantidade_mesmos_meses"], 100)

        triagem = _triagem(resultado)
        self.assertFalse(triagem.sem_mes_em_comum)
        self.assertNotEqual(triagem.blocos[_chave("PANETONE")], "urgencia")
        self.assertNotIn("venda_parou", triagem.motivos[_chave("PANETONE")])
        # Vinho: 5% abaixo nos mesmos meses; com o Natal de 2025, o ano inteiro pareceria queda de 37%.
        self.assertNotIn("venda_em_queda", triagem.motivos[_chave("VINHO")])
        # A queda de 30% nos mesmos meses continua aparecendo.
        self.assertIn("venda_em_queda", triagem.motivos[_chave("CERVEJA")])
        # Controle: comparando os períodos inteiros, o Panetone "parou" e iria para a Urgência.
        inteiros = _triagem(resultado, dias_mesmos_meses=None)
        self.assertEqual((inteiros.blocos[_chave("PANETONE")], inteiros.motivos[_chave("PANETONE")]),
                         ("urgencia", ("recuperavel_alto", "estoque_parado", "venda_parou")))
        self.assertIn("venda_em_queda", inteiros.motivos[_chave("VINHO")])

    def test_doze_meses_de_novembro_a_outubro_nao_tem_mes_em_comum(self):
        relatorios = [_relatorio(2025, mes, [_item(1, "Arroz", 10, 5, 3), PANETONE]) for mes in (11, 12)]
        relatorios += [_relatorio(2026, mes, [_item(1, "Arroz", 10, 5, 3)]) for mes in range(1, 11)]
        resultado = self.avaliar(*relatorios)
        self.assertEqual(resultado["meta"]["dias_mesmos_meses_por_ano"], {"2025": 0, "2026": 0})
        self.assertEqual({linha["quantidade_mesmos_meses"] for linha in _linhas(resultado)}, {0})
        triagem = _triagem(resultado)
        self.assertTrue(triagem.sem_mes_em_comum)
        self.assertNotIn("venda_parou", triagem.motivos[_chave("PANETONE")])
        self.assertNotEqual(triagem.blocos[_chave("PANETONE")], "urgencia")
        # Controle: novembro e dezembro de 2025 contra janeiro a outubro de 2026 dariam "parou".
        self.assertIn("venda_parou", _triagem(resultado, dias_mesmos_meses=None).motivos[_chave("PANETONE")])

    def test_so_outubro_em_comum_compara_so_outubro(self):
        # Outubro de 2025 a outubro de 2026: outubro é o único mês do calendário nos dois anos. O Brinquedo
        # vende 100 em outubro nos dois anos e 20 nos outros meses; a Bola caiu à metade em outubro.
        def linhas(ano, mes):
            outubro = mes == 10
            bola = 100 if ano == 2025 else 50
            return [_item(7, "Brinquedo", 100 if outubro else 20, 10, 6),
                    _item(8, "Bola", bola if outubro else 20, 10, 6)]
        relatorios = [_relatorio(2025, mes, linhas(2025, mes)) for mes in (10, 11, 12)]
        relatorios += [_relatorio(2026, mes, linhas(2026, mes)) for mes in range(1, 11)]
        resultado = self.avaliar(*relatorios)
        self.assertEqual(resultado["meta"]["dias_mesmos_meses_por_ano"], {"2025": 31, "2026": 31})
        por_produto = _por_produto(resultado)
        self.assertEqual([por_produto[(ano, "Brinquedo")]["quantidade_mesmos_meses"] for ano in (2025, 2026)],
                         [100, 100])
        triagem = _triagem(resultado)
        motivos = triagem.motivos[_chave("BRINQUEDO")]
        self.assertNotIn("venda_em_queda", motivos)
        self.assertNotIn("venda_em_queda_forte", motivos)
        self.assertIn("venda_em_queda", triagem.motivos[_chave("BOLA")])
        # Controle: os períodos inteiros (140 em 92 dias contra 280 em 304) dariam queda de 39%.
        self.assertIn("venda_em_queda", _triagem(resultado, dias_mesmos_meses=None).motivos[_chave("BRINQUEDO")])

    def test_mes_corrente_parcial_fica_fora_da_janela_nos_dois_lados(self):
        # 2025 inteiro e 2026 de janeiro a novembro, lido em 08/11/2026: novembro de 2026 cobre 8 dias e
        # fica fora da janela, e novembro de 2025 também. O Kit Natal vende 1 unidade por dia nos dois
        # novembros (30 em 2025, 8 em 2026).
        relatorios = []
        for ano, meses, kits in ((2025, range(1, 13), 30), (2026, range(1, 12), 8)):
            for mes in meses:
                linhas = [_item(1, "Arroz", 10, 5, 3)] + ([_item(8, "Kit Natal", kits, 50, 30)] if mes == 11 else [])
                relatorios.append(_relatorio(ano, mes, linhas))
        resultado = self.avaliar(*relatorios)
        meta = resultado["meta"]
        self.assertEqual(meta["dias_por_ano"], {"2025": 365, "2026": 312})
        self.assertEqual(meta["dias_mesmos_meses_por_ano"], {"2025": 304, "2026": 304})
        por_produto = _por_produto(resultado)
        self.assertEqual([por_produto[(ano, "Kit Natal")]["quantidade_mesmos_meses"] for ano in (2025, 2026)], [0, 0])
        self.assertEqual([por_produto[(ano, "Arroz")]["quantidade_mesmos_meses"] for ano in (2025, 2026)], [100, 100])
        motivos = _triagem(resultado).motivos[_chave("KIT NATAL")]
        self.assertNotIn("venda_em_queda", motivos)
        self.assertNotIn("venda_em_queda_forte", motivos)
        # Controle: os períodos inteiros (30 em 365 dias contra 8 em 312) dariam queda de 69%.
        self.assertIn("venda_em_queda", _triagem(resultado, dias_mesmos_meses=None).motivos[_chave("KIT NATAL")])

    def test_ano_mais_antigo_fica_fora_da_janela_e_compara_o_periodo_inteiro(self):
        # Janeiro de 2024, 2025 e 2026: a janela é dos dois últimos anos. O Feijão não vendeu em 2025, então
        # 2026 compara com 2024 pelos períodos inteiros (100 contra 40 em 31 dias: queda de 60%).
        resultado = self.avaliar(
            _relatorio(2024, 1, [_item(1, "Arroz", 10, 5, 3), _item(2, "Feijao", 100, 8, 5)]),
            _relatorio(2025, 1, [_item(1, "Arroz", 10, 5, 3)]),
            _relatorio(2026, 1, [_item(1, "Arroz", 10, 5, 3), _item(2, "Feijao", 40, 8, 5)]))
        self.assertEqual(resultado["meta"]["dias_mesmos_meses_por_ano"], {"2024": 0, "2025": 31, "2026": 31})
        por_produto = _por_produto(resultado)
        self.assertEqual([por_produto[(2024, nome)]["quantidade_mesmos_meses"] for nome in ("Arroz", "Feijao")], [0, 0])
        self.assertEqual(por_produto[(2025, "Arroz")]["quantidade_mesmos_meses"], 10)
        self.assertEqual(_triagem(resultado).motivos[_chave("FEIJAO")], ("venda_em_queda",))


# =============================================================================
# 2. Giro de lançamento
# =============================================================================
class GiroDeLancamentoTests(_Motor):
    def test_giro_de_lancamento_conta_desde_a_primeira_venda(self):
        # 2026 de janeiro a novembro (11 relatórios, novembro parcial). O Lançamento vende em outubro e
        # novembro: 2 relatórios desde a primeira venda, com venda nos 2. A Novidade começou em agosto e
        # vendeu só em agosto: 1 venda em 4 relatórios, o mínimo para o giro crítico.
        relatorios = []
        for mes in range(1, 12):
            linhas = [_item(1, "Arroz", 10, 5, 3)]
            if mes >= 10:
                linhas.append(_item(9, "Lancamento", 5, 20, 12))
            if mes == 8:
                linhas.append(_item(10, "Novidade", 5, 20, 12))
            relatorios.append(_relatorio(2026, mes, linhas))
        # Na ordem inversa: a contagem segue o período dos relatórios, não a ordem do envio.
        resultado = self.avaliar(*reversed(relatorios))
        por_produto = _por_produto(resultado)
        self.assertEqual({nome: por_produto[(2026, nome)]["relatorios_desde_primeira_venda"]
                          for nome in ("Arroz", "Lancamento", "Novidade")},
                         {"Arroz": 11, "Lancamento": 2, "Novidade": 4})
        triagem = _triagem(resultado)
        lancamento = _chave("LANCAMENTO")
        # Giro de 2 em 2: nem giro baixo nem crítico (contra os 11 relatórios do ano, seria 2 em 11).
        self.assertEqual((triagem.blocos[lancamento], triagem.motivos[lancamento]), (None, ()))
        self.assertNotIn(lancamento, triagem.estagnados)
        # A Novidade tem giro crítico com estoque (estagnada) e giro baixo.
        self.assertIn(_chave("NOVIDADE"), triagem.estagnados)
        self.assertEqual(triagem.motivos[_chave("NOVIDADE")], ("giro_baixo",))


# =============================================================================
# 3. Relatório sem a coluna de custo
# =============================================================================
class RelatorioSemCustoTests(_Motor):
    def test_relatorio_sem_a_coluna_de_custo_nao_vira_sem_custo_nem_custo_parcial(self):
        janeiro = _relatorio(2026, 1, [_item(1, "Cafe", 10, 10, 6), _item(2, "Arroz", 10, 5, 3)])
        # Fevereiro exportado sem a coluna de custo: o Feijão só vendeu nele, o Café vendeu nos dois meses.
        fevereiro = _relatorio_com("Codigo;Produto;Qtde;Preco Unitario;Valor Total;Estoque", 2026, 2,
                                   ["1;Cafe;10;10,00;100,00;5", "3;Feijao;4;8,00;32,00;5"])
        resultado = self.avaliar(janeiro, fevereiro)
        self.assertEqual(resultado["meta"]["relatorios_com_custo_por_ano"], {"2026": 1})
        por_produto = _por_produto(resultado)
        feijao, cafe = por_produto[(2026, "Feijao")], por_produto[(2026, "Cafe")]
        self.assertEqual((feijao["valor_venda_bruto"], feijao["faturamento_em_relatorio_com_custo"]), (32, 0))
        self.assertEqual((cafe["valor_venda_bruto"], cafe["faturamento_em_relatorio_com_custo"],
                          cafe["faturamento_com_custo"]), (200, 100, 100))
        triagem = _triagem(resultado)
        self.assertNotIn("sem_custo", triagem.motivos[_chave("FEIJAO")])
        self.assertNotIn("custo_parcial", triagem.omissoes[_chave("CAFE")])
        self.assertNotIn("custo_parcial", triagem.motivos[_chave("CAFE")])
        self.assertEqual((triagem.itens_venda_sem_custo, triagem.itens_omissao_erp), (0, 0))

    def test_sem_custo_so_no_ano_anterior_tambem_conta(self):
        # 2025 com a coluna de custo e a célula do Açúcar em branco; 2026 com custo.
        resultado = self.avaliar(
            _relatorio(2025, 1, [_item(1, "Cafe", 10, 10, 6), "4;Acucar;5;6,00;30,00;;5"]),
            _relatorio(2026, 1, [_item(1, "Cafe", 10, 10, 6), _item(4, "Acucar", 5, 6, 4)]))
        acucar_2025 = _por_produto(resultado)[(2025, "Acucar")]
        self.assertEqual((acucar_2025["faturamento_em_relatorio_com_custo"], acucar_2025["custo_total"]), (30, 0))
        triagem = _triagem(resultado)
        acucar = _chave("ACUCAR")
        self.assertEqual((triagem.blocos[acucar], triagem.motivos[acucar]), ("atencao", ("sem_custo",)))
        self.assertEqual(triagem.itens_venda_sem_custo, 1)


# =============================================================================
# 4. Margem negativa por relatório
# =============================================================================
class MargemNegativaPorRelatorioTests(_Motor):
    def _trimestre(self, custo_do_cafe_em_marco):
        # Café com custo acima da venda em janeiro e fevereiro; Leite com custo igual à venda em janeiro e
        # fevereiro e acima dela em março.
        return self.avaliar(
            _relatorio(2026, 1, ["1;Cafe;10;10,00;100,00;130,00;5", "3;Leite;10;5,00;50,00;50,00;5"]),
            _relatorio(2026, 2, ["1;Cafe;10;10,00;100,00;130,00;5", "3;Leite;10;5,00;50,00;50,00;5"]),
            _relatorio(2026, 3, [f"1;Cafe;10;10,00;100,00;{custo_do_cafe_em_marco};5",
                                 "3;Leite;10;5,00;50,00;55,00;5"]))

    def test_mes_mais_recente_positivo_tira_a_margem_negativa_da_urgencia(self):
        resultado = self._trimestre("60,00")
        cafe = _por_produto(resultado)[(2026, "Cafe")]
        self.assertEqual((cafe["meses_com_custo"], cafe["meses_margem_negativa"], cafe["ultimo_mes_negativo"]),
                         (3, 2, False))
        triagem = _triagem(resultado)
        self.assertNotEqual(triagem.blocos[_chave("CAFE")], "urgencia")
        self.assertNotIn("margem_negativa_recorrente", triagem.motivos[_chave("CAFE")])
        # Controle: com março também negativo, a mesma série vai para a Urgência.
        self.assertEqual(_triagem(self._trimestre("130,00")).motivos[_chave("CAFE")], ("margem_negativa_recorrente",))

    def test_custo_igual_a_venda_nao_e_relatorio_negativo(self):
        leite = _por_produto(self._trimestre("60,00"))[(2026, "Leite")]
        self.assertEqual((leite["meses_com_custo"], leite["meses_margem_negativa"], leite["ultimo_mes_negativo"]),
                         (3, 1, True))


# =============================================================================
# 5. Estoque
# =============================================================================
class EstoqueFinalTests(_Motor):
    def test_estoque_zero_e_o_ultimo_informado_e_a_celula_vazia_nao(self):
        resultado = self.avaliar(
            _relatorio(2026, 1, [_item(1, "Arroz", 10, 5, 3, estoque=50), _item(2, "Feijao", 4, 8, 5, estoque=30)]),
            _relatorio(2026, 2, [_item(1, "Arroz", 10, 5, 3, estoque=20), _item(2, "Feijao", 4, 8, 5, estoque=12)]),
            _relatorio(2026, 3, [_item(1, "Arroz", 10, 5, 3, estoque=0), _item(2, "Feijao", 4, 8, 5, estoque="")]))
        por_produto = _por_produto(resultado)
        # Zero é estoque informado; a célula vazia não é (fica o de fevereiro).
        self.assertEqual({nome: por_produto[(2026, nome)]["estoque_final"] for nome in ("Arroz", "Feijao")},
                         {"Arroz": 0, "Feijao": 12})

    def test_linha_sem_venda_no_mesmo_arquivo_informa_o_estoque(self):
        # Relatório "vendas x estoque": o Óleo vende numa linha e aparece mais abaixo com quantidade 0 e o
        # saldo atual.
        resultado = self.avaliar(_relatorio(2026, 1, [_item(5, "Oleo", 2, 9, 6, estoque=10),
                                                      "5;Oleo;0;9,00;0,00;;50",
                                                      _item(1, "Arroz", 1, 5, 3, estoque=7)]))
        oleo = _por_produto(resultado)[(2026, "Oleo")]
        self.assertEqual((oleo["quantidade"], oleo["estoque_final"]), (2, 50))

    def test_saldo_de_um_mes_sem_venda_no_mesmo_ano_e_o_ultimo_informado(self):
        # O Óleo vende em janeiro com saldo 10 e em fevereiro só aparece com quantidade 0 e saldo 50: o
        # último saldo do ano é 50, mesmo sem venda em fevereiro. Na triagem, a queda forte nos mesmos
        # meses com 50 em estoque é estoque parado, não possível ruptura.
        resultado = self.avaliar(
            _relatorio(2025, 1, [_item(5, "Oleo", 100, 9, 6, estoque=30), _item(1, "Arroz", 10, 5, 3, estoque=20)]),
            _relatorio(2025, 2, [_item(1, "Arroz", 10, 5, 3, estoque=20)]),
            _relatorio(2026, 1, [_item(5, "Oleo", 10, 9, 6, estoque=0), _item(1, "Arroz", 10, 5, 3, estoque=20)]),
            _relatorio(2026, 2, ["5;Oleo;0;9,00;0,00;;50", _item(1, "Arroz", 10, 5, 3, estoque=20)]))
        oleo = _por_produto(resultado)[(2026, "Oleo")]
        self.assertEqual((oleo["meses"], oleo["estoque_final"]), (1, 50))
        triagem = _triagem(resultado)
        chave = _chave("OLEO")
        self.assertIn(chave, triagem.estagnados)
        self.assertNotIn("possivel_ruptura", triagem.motivos[chave])


# =============================================================================
# 6. Cabeçalho ambíguo
# =============================================================================
class CabecalhoAmbiguoTests(_Motor):
    def test_cod_grupo_antes_do_codigo_nao_alimenta_o_ncm_do_relatorio(self):
        # Março: "Cód. Grupo" antes de "Código" (o Raio-X lê o código do grupo como código do produto).
        # Abril: cabeçalho comum, com NCM. Só abril conta como relatório com NCM.
        marco = _relatorio_com("Cód. Grupo;Código;Descrição;NCM;Qtde;Valor Total;Total Custo", 2026, 3, [
            "G1;101;Leite Integral;04012010;10;50,00;40,00", "G1;108;Leite Desnatado;;8;44,00;33,60"])
        abril = _relatorio_com("Código;Descrição;NCM;Qtde;Valor Total;Total Custo", 2026, 4, [
            "101;Leite Integral;04012010;10;50,00;40,00", "109;Iogurte;;6;30,00;18,00"])
        resultado = self.avaliar(marco, abril)
        meta = resultado["meta"]
        self.assertEqual((meta["relatorios_cabecalho_ambiguo"], meta["relatorios_com_ncm_por_ano"]), (1, {"2026": 1}))
        # O Leite Desnatado só veio em março, sem NCM: não é avaliado. O Iogurte, sem NCM em abril, é.
        self.assertEqual({linha["descricao_produto"]: linha["ncm_ausente"] for linha in _linhas(resultado)},
                         {"Leite Integral": False, "Leite Desnatado": False, "Iogurte": True})
        # Nem o conflito de cadastro (os dois leites com o "código" do grupo) vira omissão no ERP.
        omissoes = _triagem(resultado).omissoes
        self.assertEqual({nome: omissoes[_chave(nome)] for nome in ("LEITE DESNATADO", "LEITE INTEGRAL", "IOGURTE")},
                         {"LEITE DESNATADO": (), "LEITE INTEGRAL": (), "IOGURTE": ("sem_ncm",)})

    def test_cod_ncm_antes_do_codigo_e_aceito_e_nao_alimenta_a_categoria(self):
        relatorio = _relatorio_com("Cód. NCM;Código;Descrição;Departamento;Qtde;Valor Total;Total Custo", 2026, 4, [
            "04012010;101;Leite Integral;LATICINIOS;10;50,00;40,00", "09012100;102;Cafe Torrado;;4;75,60;56,00"])
        resultado = self.avaliar(relatorio)
        meta = resultado["meta"]
        self.assertEqual((meta["relatorios_cabecalho_ambiguo"], meta["relatorios_com_categoria_por_ano"],
                          resultado["linhas"]["categorias"]), (1, {"2026": 0}, []))
        self.assertEqual({linha["descricao_produto"]: (linha["categoria"], linha["categoria_ausente"])
                          for linha in _linhas(resultado)},
                         {"Leite Integral": (None, False), "Cafe Torrado": (None, False)})


# =============================================================================
# 7. Categoria com espaço especial
# =============================================================================
class CategoriaComEspacoEspecialTests(_Motor):
    def test_nbsp_tab_e_espaco_fino_viram_espaco_e_o_de_largura_zero_sai(self):
        relatorio = _relatorio_com("Codigo;Produto;Departamento;Qtde;Valor Total;Total Custo", 2026, 5, [
            "1;Queijo Minas;Frios\xa0e\xa0Laticinios;2;20,00;12,00",
            "2;Presunto;Frios e\tLaticinios;3;30,00;18,00",
            "3;Iogurte;Frios e Laticinios;1;5,00;3,00",
            "4;Manteiga;Frios e Lati​cinios;1;9,00;5,00",
        ])
        resultado = self.avaliar(relatorio)
        self.assertEqual(resultado["linhas"]["categorias"], ["Frios e Laticinios"])
        self.assertEqual({linha["descricao_produto"]: linha["categoria"] for linha in _linhas(resultado)},
                         {"Queijo Minas": 0, "Presunto": 0, "Iogurte": 0, "Manteiga": 0})


# =============================================================================
# 8. Teto do dinheiro da meta
# =============================================================================
class TetoDoDinheiroDaMetaTests(_Motor):
    def test_bonificacao_de_produto_sem_venda_acima_do_teto_e_recusada(self):
        # O Brinde só aparece como bonificação (quantidade com valor "0,00" e custo): o custo vai para a meta
        # do ano (ajustes_sem_venda_por_ano), com o teto do dinheiro das linhas (menos de 10^12).
        def com_brinde(custo):
            return _relatorio(2026, 6, [_item(1, "Arroz", 10, 5, 3), f"6;Brinde;1;0,00;0,00;{custo};5"])
        for custo in ("9.000.000.000.000.000,00", "1.000.000.000.000,00"):
            with self.subTest(custo=custo):
                with self.assertRaises(ETLInputError) as erro:
                    self.avaliar(com_brinde(custo))
                self.assertEqual(erro.exception.code, "avaliacao_valor")
        abaixo = self.avaliar(com_brinde("999.999.999.999,99"))
        self.assertEqual(abaixo["meta"]["ajustes_sem_venda_por_ano"], {
            "2026": {"custo_bonificacoes": 999999999999.99, "custo_devolucoes": 0, "devolucoes_com_custo": 0}})

    def test_valor_cancelado_acima_do_teto_e_recusado(self):
        cancelado = _relatorio_com("Codigo;Produto;Situacao;Qtde;Valor Total;Total Custo", 2026, 6, [
            "1;Arroz;Concluida;10;50,00;30,00", "7;Cafe;Cancelada;1;9.000.000.000.000.000,00;1,00"])
        with self.assertRaises(ETLInputError) as erro:
            self.avaliar(cancelado)
        self.assertEqual(erro.exception.code, "avaliacao_valor")


# =============================================================================
# 9. Texto limpo
# =============================================================================
class TextoLimpoTests(_Motor):
    def test_nome_e_codigo_com_caractere_de_controle_saem_limpos_e_passam_no_contrato(self):
        caminhos = self.caminhos(_relatorio(2026, 7, ["12\x0734;Cafe\x07 Especial;2;5,00;10,00;6,00;1",
                                                       "2;Leite Inte​gral;1;5,00;5,00;3,00;1"]))
        # O leitor deixa os caracteres passarem até o motor.
        por_arquivo, _resumos, _envolvidos = raio_x._ler_envio(caminhos, cadastro=True)
        self.assertTrue({"Cafe\x07 Especial", "Leite Inte​gral"} <= set(por_arquivo["nome"]))
        self.assertIn("12\x0734", set(por_arquivo["codigo"]))
        # avaliar_como_o_worker confere o contrato do processo pai (etl_runtime.validar_avaliacao).
        resultado = avaliar_como_o_worker(caminhos, hoje=HOJE)
        self.assertEqual(sorted((linha["descricao_produto"], linha["codigo_produto"]) for linha in _linhas(resultado)),
                         [("Cafe Especial", "1234"), ("Leite Integral", "2")])


if __name__ == "__main__":
    unittest.main()
