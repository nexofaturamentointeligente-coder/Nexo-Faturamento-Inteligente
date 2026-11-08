"""Saneamento do motor (11/2026): cada defeito medido no diagnóstico tem aqui o seu caso.

  1. rodapés, totais e controle de impressão saem, e produtos com palavras parecidas ficam
     ("Filtro de Café 103", "Software Antivírus", "Papel Impresso");
  2. linhas de agrupamento ("Total do Dia", "Subtotal", "Total Geral") não somam como produto;
  3. quantidade: fração ("1/2" = 0,5), sinal contábil no fim ("2-", "150,00-"), menos
     tipográfico, notação científica; data e faixa não viram número inventado;
  4. compras líquidas (NF cancelada fora, devolução ao fornecedor descontada, bonificação só na
     quantidade) e, no Raio-X, CMV e margem líquidos com bonificações e devoluções;
  5. codificação: UTF-8 com um byte de Windows-1252 no meio não divide o mesmo produto em dois;
  6. dinheiro com vírgula e 3 casas ("12,500"): milhar numa coluna en-US, decimal com alerta
     nas demais; títulos comuns de ERP ("Quant.", "Vlr. Total", "CMV") reconhecidos só como
     reserva, sem mudar o mapeamento de relatório que já era lido.

O golden de regressão (test_raio_x_regression) garante que relatório sem esses casos sai
igual ao de antes. Os arquivos são fictícios; tudo roda no próprio processo.
"""

import json
from pathlib import Path
import tempfile
import unittest

from etl_inputs import read_raw
from etl_limits import ETLLimits, using_limits
import etl_processor
from etl_processor import _e_rodape, _parse_valor
import raio_x
import raio_x_servico
import security_gatekeeper as baia
from tests.raio_x_support import executar_como_o_worker

LIMITES = ETLLimits()
COMPRAS_SIMPLES = "Fornecedor;Valor NF\nFornecedor ficticio;100,00\n"


class _Arquivos(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="nexo-saneamento-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)

    def arquivo(self, nome, conteudo, encoding="utf-8"):
        caminho = self.pasta / nome
        caminho.write_bytes(conteudo if isinstance(conteudo, bytes) else conteudo.encode(encoding))
        return str(caminho)

    def calcular(self, vendas, compras=COMPRAS_SIMPLES):
        caminho_compras = self.arquivo("compras.csv", compras) if isinstance(compras, str) else compras
        with using_limits(LIMITES):
            return etl_processor._calcular_kpis_local(vendas, caminho_compras)


# =============================================================================
# 1 e 2. Rodapés, totais e títulos de seção
# =============================================================================
class RodapesTests(_Arquivos):
    PRODUTOS = (
        "Filtro de Café 103", "Software Antivírus", "Papel Impresso A4",
        "Total Gel 50g", "Página Kids Revista", "Período Fértil Teste", "Café Filtro Papel",
        "Movimento Kids Tênis", "Kit Usuário Iniciante", "Impressora Térmica", "Totalmente Integral 1L",
        # O filtro antigo apagava os sem acento com "filtro", "software" e "impresso"; os demais nenhum
        # dos dois apaga.
        "FILTRO DE CAFE 103", "SOFTWARE ANTIVIRUS", "PAPEL IMPRESSO",
        "TECIDO IMPRESSO EM 2/3 CORES", "CIGARRO SEM FILTRO",
        "TOTAL 12 CLEAN MINT 90G", "TOTAL 100% INTEGRAL 1L", "TOTAL FLEX 5W30 1L", "10/12/14 CONJUNTO INFANTIL",
        "20/05/20 ADUBO NPK 1KG", "FILTROS", "CADERNO 1/4 96 PAG 1/4", "LIVRO PAG 1/2 COLORIDO",
        "FOLHA SULFITE A4", "DIA DAS MAES KIT", "SALDO", "VENDAS",
        "IMPRESSO EM 2 CORES", "FILTRO DE AR: PALIO 1.0", "SOFTWARE XYZ",
        # O filtro antigo mantinha e o novo também (sem acento ele não casava; serviço vendido).
        "Marcador de Página 3M", "Apostila 1º Período 2026", "Kaspersky Total Security 1 Usuário", "Transporte",
        "TRANSPORTE: ENTREGA CENTRO", "VENDA CAIXA FECHADA SKOL 350ML C/12",
        # Terceira rodada: número sem dois-pontos nem total de páginas é nome de produto, como sempre foi.
        "TRANSPORTE 2", "TRANSPORTE 01", "FOLHA 40", "FL 2",
    )
    # O filtro antigo (palavra inteira, sem acento) tirava estes; o novo tem de continuar tirando.
    RODAPES_DO_FILTRO_ANTIGO = (
        "TOTAL PERIODO", "TOTAL PAGINA", "VENDAS PERIODO", "RESUMO MENSAL", "RESUMO FISCAL", "RESUMO CAIXA",
        "TOTAL USUARIO ADMIN", "QTDE TOTAL PERIODO", "TRANSPORTE PAGINA ANTERIOR", "SALDO PERIODO",
        "VALOR TOTAL DA PAGINA", "TOTAL POR USUARIO", "FILTRO POR GRUPO: TODOS", "LOJA 1 - PERIODO 01/03 A 31/03",
        "CAIXA 01 USUARIO JOAO", "PAGINA", "PERIODO", "USUARIO", "RESUMO", "IMPRESSO", "FILTRO", "SOFTWARE",
        "FIM DA PAGINA", "CONTINUA NA PROXIMA PAGINA", "VENDAS - RESUMO", "MOVIMENTA",
        # Com número, data, dois-pontos ou palavra de rodapé (verificação adversarial de 11/2026).
        "RESUMO 03/2026", "RESUMO DE 01/03/2026 A 31/03/2026", "RESUMO MARCO/2026", "Resumo CFOP 5102",
        "RESUMO - MARÇO/2026", "Filtro Loja 01 - Centro", "Relatorio impresso as 10:35 por ADMIN",
        "Operador JOAO / Usuario ADMIN - Caixa 01", "PERIODO MARCO/2026", "Impresso dia 10/04/2026 as 08:12",
        "Relatorio emitido pelo usuario ADMIN em 10/04/2026", "PAGINA N. 1", "TOT. PERIODO", "TOT.PAGINA",
        "SOMA PERIODO", "VALOR PERIODO", "VENDA BRUTA PERIODO", "RESUMO TOTAL", "RESUMO DOS VENDEDORES",
        "PERIODO ATUAL", "PERIODO ANTERIOR", "COMPRAS PERIODO", "ENTRADAS PERIODO", "PAGINA....: 2",
        "PERIODO.: 01/03/2026 A 31/03/2026", "PERIODO - 01/03/2026 A 31/03/2026", "PAGINA Nº 001",
        "IMPRESSO DIA 10/03/2026", "RELATORIO IMPRESSO EM 10.03.2026", "ECF 001 - USUARIO 05",
        "XYZ SOFTWARE (11) 3333-4444", "Relatorio gerado pelo software Gestao Facil v4.2", "FATURAMENTO DO USUARIO",
        "RESUMO DO DIA", "RESUMO DOS PRODUTOS", "RESUMO DAS FORMAS DE PAGAMENTO", "RESUMO DO USUARIO ADMIN",
        # A mesma palavra do filtro repetida conta duas vezes; "neste", "nesta", "deste" e "desta" são
        # preposições (cada caso pega uma das duas regras sozinha).
        "RESUMO JOAO RESUMO", "FILTRO MARIA FILTRO", "PERIODO NESTE", "RESUMO DESTA", "PAGINA DESTE",
        # Terceira rodada (ataque): plurais, termos de fechamento de PDV e preposições que faltavam.
        "RESUMO DOS CAIXAS", "RESUMO DOS DIAS", "RESUMO DO FECHAMENTO", "RESUMO TEF", "RESUMO DA REDUCAO Z",
        "RESUMO SANGRIA", "PERIODO ANALISADO", "PERIODO DE APURACAO", "FILTRO TODOS", "NOME DO USUARIO",
        "QUEBRA DE PAGINA", "FILTRO PRODUTO DE 000001 ATE 999999", "PERIODO NESSE", "IMPRESSO NUMA",
        "FILTRO DOS GRUPOS", "SOFTWARE DOS CAIXAS",
        # Sem acento, "resumo", "periodo", "pagina" e "usuario" não têm resgate: estes nomes saem como
        # sempre saíram (com acento, como "Manual do Usuário", nenhum dos dois filtros apaga).
        "MANUAL DO USUARIO", "KIT USUARIO INICIANTE", "LIVRO RESUMO HISTORIA", "PERIODO FERTIL TESTE",
        "TESTE DO PERIODO FERTIL", "PAGINA KIDS REVISTA", "Absorvente Noturno do Periodo", "Resumo de Matemática",
    )
    # Nenhum dos dois filtros tirava: "Total do Dia" e afins somavam como produto.
    RODAPES_NOVOS = (
        "TOTAL DIA:", "Total Dia", "Total Diário", "Movimento do Dia", "Total do Usuário", "Valor Total do Dia",
        "Folha 1/3", "FL. 1/3", "TOTAL DIA 01/03/2026:", "Total em 01/02/2026", "TOTAL CAIXA 01", "Total Dinheiro",
        "TOTAL CARTAO", "TOTAL PAG.", "Total Anterior: 1.234,56", "A transportar", "Data: 01/03/2026",
        "01/03/2026 - SEGUNDA-FEIRA", "SUB TOTAL", "TOTAL EM DINHEIRO", "TOTAL EM CARTAO", "TOTAL DO LABORATÓRIO",
        "TOTAL DO FABRICANTE", "TOTAL FRENTISTA", "TOTAL DEPTO.: 01 MERCEARIA", "TOTAL PDV 01", "TOT. GERAL", "SOMA",
        "Total Março", "TOTAL DESCONTOS", "TOTAL A PRAZO", "TOTAL DE COMPRAS", "TOTAL COMPRADO", "TOTAL DAS ENTRADAS",
        "TOTAL DA NF", "Caixa: 01 Usuário: JOAO", "Filtros aplicados", "Período.....: 01/03/2026 a 31/03/2026",
        "Transporte: 1.234,56", "Vendas do Dia", "Saldo do Período", "Página 2 de 5",
    )
    RODAPES = (
        "Página 1 de 3", "Pág. 2/5", "PAGINA: 4", "Movimentação do Dia", "Movimentação de Caixa",
        "Total do Dia 01/02/2026", "Total do período", "TOTAL POR GRUPO", "Total de Vendas", "Total Líquido",
        "SUBTOTAL", "Sub-Total", "Totais", "Total Geral", "*** TOTAL GERAL ***", "'=== TOTAL GERAL ===",
        "Total:", "TOTAL", "Total R$ 1.234,56", "Usuário: ADMIN", "Usuario ADMIN", "Operador: Maria",
        "Filtro: Todos", "Filtros aplicados: loja 1", "Impresso em 01/02/2026 10:00",
        "Relatório impresso em 01/02/2026", "Gerado por: sistema", "Data de emissão: 01/02/2026",
        "Período: 01/01/2026 a 31/01/2026", "Periodo de 01/01/26 a 31/01/26", "LabSofti Software Ltda",
        "Resumo de vendas", "Resumo:", "Resumo geral", "01/02/2026", "Vendas no período",
        "Software: GestorPDV 3.1", "Desenvolvido por Fulano Sistemas",
    )

    def test_produtos_com_palavras_de_rodape_continuam_produtos(self):
        for nome in self.PRODUTOS:
            with self.subTest(nome=nome):
                self.assertFalse(_e_rodape(nome))

    def test_rodapes_totais_e_controle_de_impressao_saem(self):
        for texto in self.RODAPES + self.RODAPES_NOVOS:
            with self.subTest(texto=texto):
                self.assertTrue(_e_rodape(texto))

    def test_o_que_o_filtro_antigo_tirava_como_rodape_continua_saindo(self):
        import itertools
        import re
        antigo = re.compile(r"\b(?:total geral|totais|subtotal|movimenta|labsofti|software|resumo|periodo|"
                            r"usuario|filtro|impresso|pagina)\b", re.IGNORECASE)
        prefixos = ("TOTAL", "TOT.", "SALDO", "RESUMO", "VENDAS", "TRANSPORTE", "VALOR TOTAL", "ACUMULADO",
                    "FATURAMENTO", "SOMA", "QTDE", "ITENS", "RELATORIO", "OPERADOR JOAO -")
        gerados = [" ".join(p for p in partes if p) + sufixo for partes, sufixo in itertools.product(
            itertools.product(prefixos, ("", "DO", "DA", "DE", "POR", "NO", "NESTE"),
                              ("PERIODO", "PAGINA", "USUARIO", "RESUMO", "FILTRO", "IMPRESSO", "SOFTWARE")),
            ("", ":", " 01/03/2026", ": 1.234,56", " ADMIN", " 1"))]
        # O piso: tudo o que o filtro antigo tirava (o gerado e a lista) continua saindo.
        tirados = [texto for texto in self.RODAPES_DO_FILTRO_ANTIGO + tuple(gerados) if antigo.search(texto)]
        self.assertGreater(len(tirados), 3000)
        for texto in tirados:
            with self.subTest(texto=texto):
                self.assertTrue(_e_rodape(texto))
        # Produtos que o filtro antigo apagava e agora ficam: a correção pedida.
        resgatados = [nome for nome in self.PRODUTOS if antigo.search(nome)]
        self.assertGreaterEqual(len(resgatados), 10)

    def test_linha_longa_nao_trava_o_filtro(self):
        # A regra antiga de data retrocedia de forma exponencial; as de agora são lineares e o texto
        # comparado tem teto.
        import time
        inicio = time.perf_counter()
        # As de "transporte" e "impresso" só são rápidas por causa do teto de 400 caracteres e dos
        # espaços colapsados: sem o teto, 16 mil dígitos depois de "transporte" levam 20 s.
        for texto in ("01/01/2020 " + "11:11 " * 3000 + "x", "1" * 16000, "total " * 3000 + "x",
                      "resumo de " * 1600, "filtro " + "a" * 16000 + ":", "transporte " + "1" * 16000 + "x",
                      "transporte" + " " * 16000 + "-", "impresso" + "\t" * 16000 + ":"):
            _e_rodape(texto)
        self.assertLess(time.perf_counter() - inicio, 2.0)

    def test_relatorio_agrupado_por_dia_nao_soma_o_total_do_dia(self):
        vendas = self.arquivo("vendas.csv", (
            "Produto;Qtde;Valor Total\nCafé 500g;2;40,00\nTOTAL DIA 01/03/2026:;2;40,00\n"
            "Leite 1L;3;15,00\nTOTAL DIA 02/03/2026:;3;15,00\nTOTAL PERIODO;5;55,00\n"
        ))
        resultado = self.calcular(vendas)
        self.assertAlmostEqual(resultado.kpis["faturamento_total"], 55.0, places=6)
        self.assertEqual({p["nome"] for p in resultado.kpis["curva_abc"]["produtos"]}, {"Café 500g", "Leite 1L"})

    def test_produto_sem_venda_com_traco_continua_como_antes(self):
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Valor Total\nCafé 500g;2;40,00\nAçúcar 1kg;-;-\n")
        resultado = self.calcular(vendas)
        self.assertEqual([p["nome"] for p in resultado.kpis["curva_abc"]["produtos"]], ["Café 500g", "Açúcar 1kg"])
        self.assertEqual(resultado.telemetria["linhas_vendas"], 2)

    def test_compras_com_fornecedor_e_produto_ignoram_rodape_na_coluna_do_produto(self):
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Valor Total\nCafé 500g;2;40,00\n")
        compras = ("Fornecedor;Produto;Qtde Comprada;Total Custo\nDistribuidora A;Café 500g;10;150,00\n"
                   "Distribuidora A;Leite 1L;5;20,00\nDistribuidora A;SUBTOTAL;15;170,00\n")
        resultado = self.calcular(vendas, compras)
        self.assertEqual(resultado.kpis["total_comprado"], 170.0)
        self.assertEqual(resultado.kpis["produto_maior_saldo_parado_nome"], "Café 500g")

    def test_total_do_dia_nao_infla_o_faturamento_e_produtos_legitimos_ficam(self):
        vendas = self.arquivo("vendas.csv", (
            "Produto;Qtde;Valor Total\n"
            "Café 500g;2;40,00\n"
            "Leite 1L;3;15,00\n"
            "Total do Dia 01/02/2026;5;55,00\n"
            "Filtro de Café 103;1;12,00\n"
            "Software Antivírus;1;99,90\n"
            "Papel Impresso A4;2;30,00\n"
            "Subtotal;10;196,90\n"
            "Página 1 de 1;;\n"
            "Usuário: ADMIN;;\n"
            "BEBIDAS;;\n"
            "Água 500ml;0;0,00\n"
            "TOTAL GERAL;10;196,90\n"
        ))
        resultado = self.calcular(vendas)
        self.assertAlmostEqual(resultado.kpis["faturamento_total"], 196.90, places=6)
        nomes = {p["nome"] for p in resultado.kpis["curva_abc"]["produtos"]}
        self.assertEqual(nomes, {"Café 500g", "Leite 1L", "Filtro de Café 103", "Software Antivírus",
                                 "Papel Impresso A4", "Água 500ml"})
        self.assertEqual(resultado.telemetria["linhas_vendas"], 6)

    def test_linha_sem_numero_e_titulo_de_secao_e_nao_produto(self):
        vendas = self.arquivo("vendas.csv", (
            "Produto;Qtde;Valor Total\nMERCEARIA;;\nArroz 5kg;1;27,90\nBEBIDAS;;\nSuco 1L;2;12,00\n"
        ))
        resultado = self.calcular(vendas)
        self.assertEqual([p["nome"] for p in resultado.kpis["curva_abc"]["produtos"]], ["Arroz 5kg", "Suco 1L"])

    def test_compras_tambem_ignoram_rodape_e_titulo(self):
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Valor Total\nCafé 500g;2;40,00\n")
        compras = ("Fornecedor;Valor NF\nDistribuidora A;100,00\nTotal do período;100,00\n"
                   "Fornecedor B Ltda;;\nPágina 1 de 1;;\n")
        resultado = self.calcular(vendas, compras)
        self.assertEqual(resultado.kpis["total_comprado"], 100.0)
        self.assertEqual(resultado.telemetria["linhas_compras"], 1)

    def test_raio_x_nao_conta_rodape_como_item(self):
        mes = self.arquivo("marco.csv", (
            "Relatorio de vendas por produto\nPeriodo: 01/03/2026 a 31/03/2026\n"
            "Codigo;Produto;Qtde;Valor Total;Total Custo\n"
            "1;Filtro de Café 103;4;48,00;30,00\n"
            "2;Software Antivírus;1;99,90;60,00\n"
            ";Total do Dia 15/03/2026;5;147,90;90,00\n"
            ";SUBTOTAL;5;147,90;90,00\n"
        ))
        resultado = executar_como_o_worker([mes], LIMITES)
        self.assertEqual(resultado["saude"]["itens_vendidos"], 2)
        self.assertEqual(resultado["saude"]["faturamento_total"], 147.90)


# =============================================================================
# 3. Quantidade: frações, sinal contábil no fim e textos que não são número
# =============================================================================
class QuantidadeTests(unittest.TestCase):
    MANTIDOS = {
        "10": 10.0, "1.500": 1.5, "1,5": 1.5, "1.234,56": 1234.56, "1,234.56": 1234.56,
        "R$ 1.234,56": 1234.56, "(2)": -2.0, "-2": -2.0, "3 kg": 3.0, "2 un(s)": 2.0,
        "-2 un(s)": -2.0, "'-2 un(s)": -2.0, "1.234.567": 1234567.0, "": None, "-": None,
        "--": None, "nan": None, "None": None, "abc": None, None: None,
        # Unidade com barra não é fração: vale o número do começo, como sempre valeu.
        "2 UN/CX": 2.0, "1,250 KG/UN": 1.25, "4 UND/PCT": 4.0, "0,5 L/KG": 0.5, "1 P/ KG": 1.0,
        "18,350/KG": 18.35, "24/UN": 24.0, "6/UN": 6.0, "1,250/KG": 1.25, "(2) UN/CX": -2.0, "2-UN/CX": 2.0,
    }
    CORRIGIDOS = {
        "1/2": 0.5, "3/4 kg": 0.75, "1 1/2": 1.5, "1 1/2 un(s)": 1.5, "(1/2)": -0.5, "(1/2 kg)": -0.5,
        "1/2-": -0.5, "-1/4": -0.25, "1/0": None, "3 CX/12": 3.0, "1 CX C/12": 1.0,
        "(1/2) KG": -0.5, "1/2 KG-": -0.5, "1/2-3": None,
        "2-": -2.0, "150,00-": -150.0, "1.234,50-": -1234.5, "−2": -2.0, "–3": -3.0,
        "1e3": 1000.0, "1,5E+03": 1500.0, "-2E2": -200.0,
        "2026-01-02": None, "2026-01-02 00:00:00": None, "01/02/2026": None, "1/2/3": None,
        "1-2": None, "10 - 2": None,
    }

    def test_formatos_de_sempre_nao_mudam(self):
        for bruto, esperado in self.MANTIDOS.items():
            with self.subTest(bruto=bruto):
                self.assertEqual(_parse_valor(bruto), esperado)

    def test_fracao_sinal_no_fim_e_texto_que_nao_e_quantidade(self):
        for bruto, esperado in self.CORRIGIDOS.items():
            with self.subTest(bruto=bruto):
                self.assertEqual(_parse_valor(bruto), esperado)

    def test_fracao_grande_demais_vira_infinito_e_nao_excecao(self):
        # Como os dígitos colados viravam antes: a leitura recusa com "fora do intervalo aceito".
        import math
        self.assertEqual(_parse_valor("1" + "0" * 400 + "/1"), math.inf)
        self.assertEqual(_parse_valor("9" * 5000 + "/1"), math.inf)
        self.assertEqual(_parse_valor("-1" + "0" * 400 + "/1"), -math.inf)
        # Acima de 4.300 dígitos (o limite do int() do Python), os zeros à esquerda não viram infinito.
        self.assertEqual(_parse_valor("0" * 4301 + "/1"), 0.0)
        self.assertEqual(_parse_valor("0" * 4300 + "12/24"), 0.5)
        self.assertIsNone(_parse_valor("1/" + "0" * 4301))

    def test_celula_de_dinheiro_longa_nao_trava_a_leitura(self):
        # _RE_DECIMAL retrocedia de forma quadrática: 16 mil dígitos levavam 3 s por célula.
        import time
        inicio = time.perf_counter()
        self.assertEqual(etl_processor.converter_valor_monetario("1" * 16383 + "x"), 0.0)
        self.assertEqual(etl_processor.converter_valor_monetario("1" * 300 + ",5"), float("1" * 300 + ".5"))
        self.assertLess(time.perf_counter() - inicio, 1.0)

    def test_celula_longa_com_barra_e_espacos_nao_trava_a_leitura(self):
        # Os \s* seguidos da unidade com barra retrocediam: 400 espaços levavam 40 s.
        import time
        inicio = time.perf_counter()
        self.assertIsNone(_parse_valor("1" + " " * 50_000 + "1/"))
        self.assertIsNone(_parse_valor("(" + " " * 50_000 + "1" + " " * 50_000 + "/"))
        self.assertLess(time.perf_counter() - inicio, 1.0)
        # Espaço a mais não muda a leitura.
        self.assertEqual(_parse_valor("1/2" + " " * 100 + "KG"), 0.5)
        self.assertEqual(_parse_valor("2" + " " * 100 + "UN/CX"), 2.0)

    def test_devolucao_com_sinal_no_fim_e_fracao_chegam_ao_faturamento(self):
        with tempfile.TemporaryDirectory(prefix="nexo-saneamento-") as pasta:
            vendas = Path(pasta) / "vendas.csv"
            vendas.write_text("Produto;Qtde;Valor Total\nQueijo kg;1/2;24,95\nQueijo kg;1 1/2;74,85\n"
                              "Pão kg;2-;10,00-\n", encoding="utf-8")
            compras = Path(pasta) / "compras.csv"
            compras.write_text(COMPRAS_SIMPLES, encoding="utf-8")
            with using_limits(LIMITES):
                resultado = etl_processor._calcular_kpis_local(str(vendas), str(compras))
        produtos = {p["nome"]: p for p in resultado.kpis["curva_abc"]["produtos"]}
        self.assertEqual(produtos["Queijo kg"]["quantidade"], 2.0)
        self.assertEqual(produtos["Pão kg"]["quantidade"], -2.0)
        self.assertAlmostEqual(resultado.kpis["faturamento_total"], 89.80, places=6)


# =============================================================================
# 3b. Dinheiro com vírgula e 3 casas; títulos de ERP de reserva
# =============================================================================
class MilharComVirgulaTests(_Arquivos):
    def test_coluna_en_us_le_virgula_com_3_casas_como_milhar(self):
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Valor Total\nCafe;2;1,234.56\nLeite;1;12,500\n")
        resultado = self.calcular(vendas)
        self.assertAlmostEqual(resultado.kpis["faturamento_total"], 13734.56, places=6)
        self.assertEqual(resultado.telemetria["valores_monetarios"], {"virgula_como_milhar": 1})
        self.assertTrue([a for a in resultado.alertas if "foram lidos como milhar (doze mil e quinhentos)" in a])

    def test_relatorio_brasileiro_com_3_casas_fica_igual_e_sem_alerta(self):
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Valor Total;Total Custo\nCafe;2;12,500;31,125\n"
                                            "Leite;1;3,50;9,750\n")
        resultado = self.calcular(vendas)
        self.assertAlmostEqual(resultado.kpis["faturamento_total"], 16.0, places=6)
        self.assertNotIn("valores_monetarios", resultado.telemetria)
        self.assertFalse([a for a in resultado.alertas if "vírgula e 3 casas" in a])

    def test_virgula_com_3_casas_numa_coluna_brasileira_de_arquivo_en_us_tem_alerta(self):
        # O valor de venda é en-US pelo texto; o custo, brasileiro ("2,50"): "12,500" no custo é 12,5,
        # e o arquivo misto pede conferência.
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Valor Total;Total Custo\nCafe;2;1,234.56;12,500\n"
                                            "Leite;1;3.50;2,50\n")
        resultado = self.calcular(vendas)
        self.assertAlmostEqual(resultado.kpis["faturamento_total"], 1238.06, places=6)
        self.assertEqual(resultado.telemetria["valores_monetarios"], {"virgula_como_decimal": 1})
        self.assertTrue([a for a in resultado.alertas if "foram lidos como decimal (doze e meio)" in a])

    def test_celula_numerica_do_xlsx_nao_prova_formato_en_us(self):
        import pandas as pd
        from collections import Counter
        qualidade = Counter()
        serie = pd.Series([12.5, 7.99, "5,899"], dtype=object)
        self.assertEqual(list(etl_processor._converter_serie_monetaria(serie, qualidade)), [12.5, 7.99, 5.899])

    def test_xlsx_real_le_como_sempre_e_sem_alerta(self):
        # No XLSX a leitura entrega tudo como texto ("32.9"): número de verdade não prova en-US, e
        # "5,899" digitado como texto continua 5,899, sem alerta novo.
        from openpyxl import Workbook
        livro = Workbook()
        aba = livro.active
        aba.append(["Produto", "Qtde", "Valor Total", "Total Custo"])
        aba.append(["Cafe", 2, 32.9, 20.5])
        aba.append(["Leite", 1, 7.99, "5,899"])
        caminho = self.pasta / "vendas.xlsx"
        livro.save(caminho)
        resultado = self.calcular(str(caminho))
        self.assertAlmostEqual(resultado.kpis["faturamento_total"], 40.89, places=6)
        self.assertNotIn("valores_monetarios", resultado.telemetria)
        self.assertFalse([a for a in resultado.alertas if "vírgula e 3 casas" in a])
        # O mesmo conteúdo em CSV (texto de verdade, en-US no valor): "5,899" no custo, que tem
        # "20.5", vira cinco mil oitocentos e noventa e nove.
        csv = self.arquivo("vendas.csv", "Produto;Qtde;Valor Total;Total Custo\nCafe;2;32.9;20.5\nLeite;1;7.99;5,899\n")
        resultado_csv = self.calcular(csv)
        self.assertEqual(resultado_csv.telemetria["valores_monetarios"], {"virgula_como_milhar": 1})

    def test_valor_unico_continua_com_a_leitura_brasileira(self):
        from etl_processor import converter_valor_monetario
        self.assertEqual(converter_valor_monetario("12,500"), 12.5)
        self.assertEqual(converter_valor_monetario("12,500", ponto_decimal=True), 12.5)
        self.assertEqual(converter_valor_monetario("12,500", virgula_milhar=True), 12500.0)

    def test_coluna_com_dois_decimais_nao_recebe_alerta(self):
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Valor Total\nCafe;2;1.234,56\nLeite;1;12,50\n")
        resultado = self.calcular(vendas)
        self.assertAlmostEqual(resultado.kpis["faturamento_total"], 1247.06, places=6)
        self.assertNotIn("valores_monetarios", resultado.telemetria)


class TitulosDeReservaTests(_Arquivos):
    @staticmethod
    def mapa(*titulos, aliases=etl_processor.ALIAS_VENDAS):
        return etl_processor._mapear_cabecalho([etl_processor._norm_chave(t) for t in titulos], aliases)

    def test_titulos_comuns_de_erp_sao_reconhecidos(self):
        self.assertEqual(self.mapa("Produto", "Quant.", "Vlr. Total", "CMV"),
                         {"produto": 0, "valor_custo": 3, "valor_venda": 2, "quantidade": 1})
        self.assertEqual(self.mapa("Produto", "Qtd", "Valor Líquido")["valor_venda"], 2)
        self.assertEqual(self.mapa("Produto", "Qtde", "Vl. Total")["valor_venda"], 2)
        self.assertEqual(self.mapa("Produto", "Qtde", "Total R$")["valor_venda"], 2)
        self.assertEqual(self.mapa("Produto", "Qtde", "Vlr. Unit.")["preco_unitario"], 2)

    def test_reserva_nao_disputa_com_o_dicionario_principal(self):
        # Com "Custo" e "Valor Total", "CMV" e "Total" ficam como estavam: sem uso.
        self.assertEqual(self.mapa("Produto", "Qtde", "CMV", "Custo", "Total", "Valor Total"),
                         {"produto": 0, "quantidade": 1, "valor_custo": 3, "valor_venda": 5})

    def test_reserva_compara_o_titulo_inteiro(self):
        mapa = self.mapa("Produto", "Quantia", "Vlr Total Desconto", "Quantidade", "Vlr Total")
        self.assertEqual(mapa, {"produto": 0, "quantidade": 3, "valor_venda": 4})
        self.assertNotIn("quantidade", self.mapa("Produto", "Quantia", "Vlr Total"))

    def test_unidade_de_medida_nao_vira_quantidade(self):
        self.assertNotIn("quantidade", self.mapa("Produto", "Un", "Vlr Total"))

    def test_titulo_de_reserva_repetido_e_ambiguo(self):
        # Dois "Quant." na mesma linha: a reserva não escolhe; o cabeçalho em duas linhas desfaz a
        # dúvida e a quantidade é a vendida, e não a pedida (verificação adversarial de 11/2026).
        self.assertNotIn("quantidade", self.mapa("Produto", "Quant.", "Quant.", "Valor Total"))
        vendas = self.arquivo("vendas.csv", "Produto;Quant.;Quant.;Valor Total\n;Pedida;Vendida;\n"
                                            "Café 500g;5;2;40,00\nLeite 1L;9;3;15,00\n")
        resultado = self.calcular(vendas)
        self.assertEqual((resultado.kpis["produto_mais_vendido_nome"], resultado.kpis["produto_mais_vendido_quantidade"]),
                         ("Leite 1L", 3.0))

    def test_total_explicito_vence_o_unitario(self):
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Preço Unit.;Total\nCafe;2;20,00;38,00\n")
        resultado = self.calcular(vendas)
        self.assertEqual(resultado.kpis["faturamento_total"], 38.0)

    def test_titulos_de_reserva_chegam_ao_calculo(self):
        vendas = self.arquivo("vendas.csv", "Produto;Quant.;Vlr Total;CMV\nCafe;2;40,00;30,00\nLeite;3;15,00;20,00\n")
        resultado = self.calcular(vendas)
        self.assertEqual(resultado.kpis["faturamento_total"], 55.0)
        self.assertCountEqual(resultado.telemetria["colunas_vendas"],
                              ["produto", "valor_custo", "valor_venda", "quantidade"])
        self.assertEqual(resultado.telemetria["itens_venda_abaixo_custo_pdv"], 1)

    def test_compras_por_item_com_titulos_de_reserva(self):
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Valor Total\nCafe;2;40,00\n")
        compras = "Produto;Quantidade;Custo Total\nCafe;5;75,00\n"
        resultado = self.calcular(vendas, compras)
        self.assertEqual(resultado.kpis["total_comprado"], 75.0)
        self.assertCountEqual(resultado.telemetria["colunas_compras"], ["produto", "qtde_comprada", "valor_nf"])
        self.assertEqual(resultado.kpis["produto_maior_saldo_parado_nome"], "Cafe")
        self.assertEqual(resultado.kpis["saldo_estimado_parado"], 3.0)

    def test_baia_aceita_o_que_o_processamento_aceita(self):
        for nome, conteudo in (("vendas.csv", "Produto;Quant.;Vlr Total\nCafe;2;40,00\n"),
                               ("compras.csv", "Produto;Qtd;Vl. Total\nCafe;5;75,00\n")):
            with self.subTest(nome=nome):
                baia.exigir_estrutura_minima(read_raw(self.arquivo(nome, conteudo), LIMITES))

    def test_cabecalho_em_duas_linhas_que_o_dicionario_lia_continua_igual(self):
        # A primeira linha sozinha passaria com a reserva ("Total"), mas o dicionário principal lê as
        # duas linhas juntas, como sempre: vale o Total Líquido nas vendas e o Total Produtos nas compras.
        vendas = self.arquivo("vendas.csv", "Descrição;Qtde;Total;Total\n;;Bruto;Líquido\nCafé 500g;2;40,00;36,00\n"
                                            "Leite 1L;3;15,00;13,50\n")
        compras = "Fornecedor;Total;Total\n;NF;Produtos\nDistribuidora A;1.000,00;900,00\n"
        resultado = self.calcular(vendas, compras)
        self.assertAlmostEqual(resultado.kpis["faturamento_total"], 49.5, places=6)
        self.assertEqual(resultado.kpis["total_comprado"], 900.0)
        self.assertEqual(resultado.telemetria["coluna_valor_compras"], "produtos_total")

    def test_coluna_de_percentual_nao_vira_valor_nem_custo(self):
        # "% Total" e "% CMV" viram "total" e "cmv" na normalização; a reserva não as usa.
        vendas = self.arquivo("vendas.csv", "Produto;Qtde;Preço Unit.;% Total;% CMV\nCafe;2;20,00;50;30\n")
        resultado = self.calcular(vendas)
        self.assertEqual(resultado.kpis["faturamento_total"], 40.0)
        self.assertNotIn("valor_custo", resultado.telemetria["colunas_vendas"])
        for titulo in ("Total (%)", "Total %", "% Total"):
            with self.subTest(titulo=titulo):
                arquivo = self.arquivo("v.csv", f"Produto;Qtde;Vlr. Unit.;{titulo}\nCafe;2;20,00;50\n")
                self.assertEqual(self.calcular(arquivo).kpis["faturamento_total"], 40.0)

    def test_dicionario_de_fora_nao_tem_reserva(self):
        self.assertEqual(etl_processor._mapear_cabecalho(["vlr total"], {"valor": ["valor"]}), {})


# =============================================================================
# 4a. Compras líquidas
# =============================================================================
class ComprasTests(_Arquivos):
    VENDAS = "Produto;Qtde;Valor Total\nCafé 500g;2;40,00\n"

    def test_nf_cancelada_sai_devolucao_desconta_e_bonificacao_so_na_quantidade(self):
        compras = (
            "Fornecedor;Produto;Qtde Comprada;Total Custo;Situação\n"
            "Fornecedor A;Café 500g;10;150,00;Normal\n"
            "Fornecedor A;Leite 1L;20;80,00;Cancelada\n"
            "Fornecedor B;Café 500g;2;30,00;Devolução de compra\n"
            "Fornecedor B;Açúcar 1kg;5;0,00;Bonificação\n"
            "Fornecedor C;Arroz 5kg;3;60,00;Bonificação\n"
        )
        resultado = self.calcular(self.arquivo("vendas.csv", self.VENDAS), compras)
        self.assertEqual(resultado.kpis["total_comprado"], 120.0)
        self.assertEqual(resultado.kpis["saldo_estimado_compras_vendas"], 80.0)
        self.assertEqual(resultado.telemetria["compras_consolidacao"], {
            "linhas_canceladas": 1, "valor_cancelado": 80.0, "linhas_devolucao": 1, "valor_devolucoes": 30.0,
            "linhas_bonificacao": 2, "quantidade_bonificada": 8.0,
        })
        # Café: 10 comprados - 2 devolvidos - 2 vendidos = 6 parados.
        self.assertEqual(resultado.kpis["produto_maior_saldo_parado_nome"], "Café 500g")
        self.assertEqual(resultado.kpis["saldo_estimado_parado"], 6.0)
        self.assertIn("Compras líquidas: 1 nota(s) cancelada(s), estornada(s) ou inutilizada(s) ficaram fora do "
                      "total comprado; 1 devolução(ões) ao fornecedor descontaram R$ 30,00; 2 bonificação(ões) "
                      "entraram só na quantidade comprada, sem valor.", resultado.alertas)

    def test_sem_situacao_fica_a_regra_de_sempre(self):
        # Sem marca explícita, entra só a linha com valor positivo, como antes: o negativo, o zero, o
        # vazio e o inválido saem, sem virar devolução nem bonificação.
        compras = ("Produto;Qtde Comprada;Total Custo\nCafé 500g;10;150,00\nCafé 500g;-2;-30,00\n"
                   "Açúcar 1kg;5;0,00\nArroz 5kg;20;-\nFeijão 1kg;3;\nMilho;4;N/D\n")
        resultado = self.calcular(self.arquivo("vendas.csv", self.VENDAS), compras)
        self.assertEqual(resultado.kpis["total_comprado"], 150.0)
        self.assertEqual(resultado.telemetria["linhas_compras"], 1)
        self.assertNotIn("compras_consolidacao", resultado.telemetria)
        self.assertEqual((resultado.kpis["produto_maior_saldo_parado_nome"], resultado.kpis["saldo_estimado_parado"]),
                         ("Café 500g", 8.0))

    def test_devolucao_de_venda_status_devolvida_e_troca_seguem_a_regra_de_sempre(self):
        # Só a devolução AO FORNECEDOR desconta: devolução de venda (o cliente devolveu à loja), o
        # status da NF original e a troca entram como sempre entraram.
        compras = ("Fornecedor;Valor NF;Natureza da Operação\nDistribuidora A;1.000,00;COMPRA PARA COMERCIALIZACAO\n"
                   "Cliente X;200,00;DEVOLUCAO DE VENDA\nDistribuidora B;1.500,00;Devolvida parcialmente\n"
                   "Distribuidora C;300,00;Entrada por troca\n")
        resultado = self.calcular(self.arquivo("vendas.csv", self.VENDAS), compras)
        self.assertEqual(resultado.kpis["total_comprado"], 3000.0)
        self.assertNotIn("compras_consolidacao", resultado.telemetria)

    def test_devolucao_ao_fornecedor_pela_natureza_ou_pelo_cfop(self):
        compras = ("Fornecedor;Valor NF;Natureza da Operação\nDistribuidora A;1.000,00;COMPRA PARA COMERCIALIZACAO\n"
                   "Distribuidora A;100,00;CFOP 5.202 - DEVOL. DE MERCADORIA\nDistribuidora B;50,00;Devolução ao fornecedor\n"
                   "Distribuidora C;25,00;Devolução de compra para comercialização\n")
        resultado = self.calcular(self.arquivo("vendas.csv", self.VENDAS), compras)
        self.assertEqual(resultado.kpis["total_comprado"], 825.0)
        self.assertEqual(resultado.telemetria["compras_consolidacao"]["linhas_devolucao"], 3)

    def test_textos_de_erp_que_nao_sao_cancelamento_devolucao_nem_bonificacao(self):
        # Verificação adversarial de 11/2026: cada um destes zerava, negava ou tirava uma NF paga.
        casos = {
            "compra com bonificação": ("Natureza da Operação", "COMPRA C/ BONIFICACAO"),
            "saldo do pedido cancelado": ("Status do Pedido", "Atendido - saldo cancelado"),
            "número de operação": ("Operação", "5202"),
            "NF original devolvida ao fornecedor": ("Situação", "Devolvida ao fornecedor"),
            "sem bonificação": ("Situação", "SEM BONIFICACAO"),
            # Segunda rodada: negação longe da marca e a natureza escrita pelo fornecedor.
            "não houve devolução": ("Situação", "Não houve devolução ao fornecedor"),
            "nota não foi cancelada": ("Situação", "Nota não foi cancelada"),
            "venda e bonificação do fornecedor": ("Natureza da Operação", "VENDA E BONIFICACAO"),
            # Terceira rodada: a marca desfeita depois dela ou com outro sujeito antes.
            "cancelamento rejeitado": ("Status", "Cancelamento rejeitado"),
            "autorizada com cancelamento rejeitado": ("Status", "Autorizada - cancelamento rejeitado"),
            "cancelamento pendente": ("Status", "Cancelamento pendente"),
            "pagamento estornado": ("Status", "Pagamento estornado"),
            "saldo do pedido cancelado sem dizer pedido": ("Status", "Atendido parcial - saldo cancelado"),
            "devolução pendente": ("Situação", "Devolução ao fornecedor pendente"),
        }
        for caso, (coluna, texto) in casos.items():
            with self.subTest(caso=caso):
                compras = (f"Fornecedor;NF;{coluna};Valor NF\nDistribuidora Alfa Ltda;12345;Normal;1.250,00\n"
                           f"Atacadão Beta S/A;778;{texto};830,40\n")
                resultado = self.calcular(self.arquivo("vendas.csv", self.VENDAS), compras)
                self.assertAlmostEqual(resultado.kpis["total_comprado"], 2080.40, places=6)
                self.assertNotIn("compras_consolidacao", resultado.telemetria)

    def test_duas_colunas_de_situacao_nao_viram_devolucao(self):
        # "Devolvida" (status da NF original) junto de "Compra para comercialização" (natureza).
        compras = ("Fornecedor;NF;Status;Natureza da Operação;Valor NF\n"
                   "Distribuidora Alfa Ltda;12345;Autorizada;Compra para comercialização;1.250,00\n"
                   "Atacadão Beta S/A;778;Devolvida;Compra para comercialização;830,40\n")
        resultado = self.calcular(self.arquivo("vendas.csv", self.VENDAS), compras)
        self.assertAlmostEqual(resultado.kpis["total_comprado"], 2080.40, places=6)

    def test_coluna_do_produto_nas_compras_so_perde_os_totais_inequivocos(self):
        compras = ("Fornecedor;NF;Produto;Qtde;Valor Total\nDistribuidora Alfa Ltda;12345;TRANSPORTE;1;95,00\n"
                   "Grafica Pi Ltda;66;IMPRESSO EM PAPEL KRAFT 40X30;1000;380,00\nDistribuidora Alfa Ltda;12345;FILTRO;20;180,00\n"
                   "Distribuidora Alfa Ltda;12345;FILTRO DE AR: PALIO 1.0;10;250,00\nDistribuidora Alfa Ltda;12345;SOFTWARE;1;450,00\n"
                   "Distribuidora Alfa Ltda;12345;IMPRESSO;500;210,00\nDistribuidora Alfa Ltda;12345;SUBTOTAL;1532;1.565,00\n")
        resultado = self.calcular(self.arquivo("vendas.csv", self.VENDAS), compras)
        self.assertEqual(resultado.kpis["total_comprado"], 1565.0)
        self.assertEqual(resultado.telemetria["linhas_compras"], 6)

    def test_nf_cancelada_no_nivel_de_nota_fiscal(self):
        compras = ("Fornecedor;Valor NF;Situação NF\nDistribuidora A;1.000,00;Autorizada\n"
                   "Distribuidora B;500,00;Cancelada\nDistribuidora C;250,00;Não cancelada\n")
        resultado = self.calcular(self.arquivo("vendas.csv", self.VENDAS), compras)
        self.assertEqual(resultado.kpis["total_comprado"], 1250.0)

    def test_compra_comum_nao_ganha_telemetria_nem_alerta_novos(self):
        resultado = self.calcular(self.arquivo("vendas.csv", self.VENDAS))
        self.assertNotIn("compras_consolidacao", resultado.telemetria)
        self.assertFalse([a for a in resultado.alertas if a.startswith("Compras líquidas")])


# =============================================================================
# 4b. Raio-X: CMV e margem líquidos com bonificações e devoluções
# =============================================================================
class CmvLiquidoTests(_Arquivos):
    MARCO = (
        "Relatorio de vendas por produto\nPeriodo: 01/03/2026 a 31/03/2026\n"
        "Codigo;Produto;Qtde;Valor Total;Total Custo;Situacao\n"
        "1;Cafe 500g;10;200,00;150,00;Concluida\n"
        "2;Acucar 1kg;5;30,00;20,00;Concluida\n"
        "1;Cafe 500g;2;0,00;30,00;Concluida\n"
        "2;Acucar 1kg;1;6,00;4,00;Devolucao\n"
    )

    def test_bonificacao_soma_custo_e_devolucao_desconta_custo_e_receita(self):
        auditoria = executar_como_o_worker([self.arquivo("marco.csv", self.MARCO)], LIMITES)["auditoria"]
        # Os campos de sempre seguem só as vendas analisadas.
        self.assertEqual((auditoria["custo_total"], auditoria["margem_valor"]), (170.0, 60.0))
        self.assertEqual((auditoria["custo_bonificacoes"], auditoria["custo_devolucoes"]), (30.0, 4.0))
        # CMV líquido = 170 + 30 - 4; receita líquida com custo = 230 - 6; margem = 224 - 196.
        self.assertEqual(auditoria["cmv_liquido"], 196.0)
        self.assertEqual(auditoria["margem_liquida_valor"], 28.0)
        self.assertEqual(auditoria["margem_liquida_percentual"], 0.125)

    def test_valor_em_branco_nao_e_bonificacao(self):
        # Só o zero informado ("0,00") é bonificação; a célula em branco é venda sem valor
        # (inconsistência do cadastro), e o custo dela não entra no CMV líquido.
        em_branco = self.MARCO.replace("1;Cafe 500g;2;0,00;30,00;Concluida", "1;Cafe 500g;2;;30,00;Concluida")
        resultado = executar_como_o_worker([self.arquivo("marco.csv", em_branco)], LIMITES)
        self.assertEqual(resultado["auditoria"]["custo_bonificacoes"], 0.0)
        self.assertEqual(resultado["auditoria"]["cmv_liquido"], 166.0)

    def test_sem_custo_nao_ha_cmv_nem_margem_liquidos(self):
        sem_custo = self.arquivo("marco.csv", "Produto;Qtde;Valor Total\nCafe 500g;10;200,00\n")
        auditoria = executar_como_o_worker([sem_custo], LIMITES)["auditoria"]
        self.assertEqual((auditoria["custo_bonificacoes"], auditoria["custo_devolucoes"]), (0.0, 0.0))
        self.assertIsNone(auditoria["cmv_liquido"])
        self.assertIsNone(auditoria["margem_liquida_valor"])

    def test_plano_sem_cmv_e_margem_nao_recebe_os_valores_liquidos(self):
        resultado = executar_como_o_worker([self.arquivo("marco.csv", self.MARCO)], LIMITES)
        bronze = raio_x_servico.visao(resultado, frozenset({"raio_x_entrada"}), 6)["auditoria"]
        for campo in ("cmv_liquido", "custo_bonificacoes", "custo_devolucoes", "margem_liquida_valor",
                      "margem_liquida_percentual"):
            with self.subTest(campo=campo):
                self.assertIsNone(bronze[campo])
        prata = raio_x_servico.visao(resultado, frozenset({"raio_x_entrada", "margem_bruta", "diagnostico_cmv"}),
                                     24)["auditoria"]
        self.assertEqual((prata["cmv_liquido"], prata["margem_liquida_valor"]), (196.0, 28.0))

    def test_raio_x_gravado_antes_dos_campos_liquidos_continua_legivel(self):
        antigo = executar_como_o_worker([self.arquivo("marco.csv", self.MARCO)], LIMITES)
        for campo in raio_x_servico.CAMPOS_MARGEM_LIQUIDA + raio_x_servico.CAMPOS_CMV_LIQUIDO:
            antigo["auditoria"].pop(campo)
        for liberadas in (frozenset({"raio_x_entrada"}),
                          frozenset({"raio_x_entrada", "margem_bruta", "diagnostico_cmv", "auditoria_abaixo_custo"})):
            with self.subTest(liberadas=sorted(liberadas)):
                visto = raio_x_servico.visao(antigo, liberadas, 6)["auditoria"]
                # Os campos novos chegam nulos, com a mesma forma do que o plano não libera.
                for campo in raio_x_servico.CAMPOS_MARGEM_LIQUIDA + raio_x_servico.CAMPOS_CMV_LIQUIDO:
                    self.assertIsNone(visto[campo])
        self.assertIsNone(raio_x_servico.visao(antigo, frozenset({"raio_x_entrada"}), 6)["auditoria"]["custo_total"])

    def test_devolucao_com_custo_negativo_desconta_o_custo(self):
        negativo = self.MARCO.replace("2;Acucar 1kg;1;6,00;4,00;Devolucao", "2;Acucar 1kg;1;6,00;-4,00;Devolucao")
        auditoria = executar_como_o_worker([self.arquivo("marco.csv", negativo)], LIMITES)["auditoria"]
        self.assertEqual((auditoria["custo_devolucoes"], auditoria["cmv_liquido"]), (4.0, 196.0))


# =============================================================================
# 5. Codificação: UTF-8 com um byte de Windows-1252 no meio
# =============================================================================
class CodificacaoTests(_Arquivos):
    MISTO = ("Produto;Qtde;Valor Total\nCafé 500g;2;40,00\n".encode("utf-8") + b"Feij\xe3o 1kg;1;9,00\n"
             + "Pão Francês;1;8,00\n".encode("utf-8"))

    def test_sequencias_utf8_desfeitas_sao_refeitas_e_contadas(self):
        matriz = read_raw(self.arquivo("misto.csv", self.MISTO), LIMITES)
        self.assertEqual(matriz.attrs["encoding"], "cp1252")
        self.assertEqual([matriz.iat[i, 0] for i in range(1, 4)], ["Café 500g", "Feijão 1kg", "Pão Francês"])
        self.assertEqual(matriz.attrs["celulas_recodificadas"], 2)

    def test_arquivo_todo_em_windows_1252_nao_muda(self):
        cp1252 = self.arquivo("cp1252.csv", "Produto;Qtde;Valor Total\nCafé “especial”;2;40,00\nAÇÃO Ç;1;1,00\n",
                              encoding="cp1252")
        matriz = read_raw(cp1252, LIMITES)
        self.assertEqual((matriz.iat[1, 0], matriz.iat[2, 0]), ("Café “especial”", "AÇÃO Ç"))
        self.assertEqual(matriz.attrs["celulas_recodificadas"], 0)

    def test_windows_1252_legitimo_com_nbsp_e_simbolos_nao_e_corrompido(self):
        # "É" seguido de NBSP, "®" ou "”" também forma UTF-8 válido, mas dá letras de outro alfabeto
        # ("ɠ", "ɮ"): o arquivo é Windows-1252 de verdade e fica como está.
        texto = ("Produto;Qtde;Valor Total\nCAFÉ\xa0500G;2;40,00\nNESCAFÉ® 100G;1;9,00\nFEIJÃO 1KG;1;9,00\n"
                 "MAÇÃ\xa0VERDE;1;5,00\nPÃO “ESPECIAL”;1;1,00\n")
        matriz = read_raw(self.arquivo("cp1252.csv", texto, encoding="cp1252"), LIMITES)
        self.assertEqual([matriz.iat[i, 0] for i in range(1, 6)],
                         ["CAFÉ\xa0500G", "NESCAFÉ® 100G", "FEIJÃO 1KG", "MAÇÃ\xa0VERDE", "PÃO “ESPECIAL”"])
        self.assertEqual(matriz.attrs["celulas_recodificadas"], 0)

    def test_utf8_com_byte_que_o_cp1252_nao_define_tambem_e_refeito(self):
        # "Á" em UTF-8 é C3 81, e 0x81 não existe no cp1252: o arquivo cai para latin1.
        conteudo = "Produto;Qtde;Valor Total\nÁGUA MINERAL 500ML;2;4,00\n".encode("utf-8") + b"Caf\xe9 500g;1;10,00\n"
        matriz = read_raw(self.arquivo("latin1.csv", conteudo), LIMITES)
        self.assertEqual(matriz.attrs["encoding"], "latin1")
        self.assertEqual([matriz.iat[1, 0], matriz.iat[2, 0]], ["ÁGUA MINERAL 500ML", "Café 500g"])
        self.assertEqual(matriz.attrs["celulas_recodificadas"], 1)

    def test_mesmo_produto_igual_em_todos_os_meses_do_envio(self):
        # Fevereiro tem mais acentos de Windows-1252 soltos do que sequências UTF-8: a decisão é da
        # célula, e "Café 500g" sai igual nos dois meses. Sem código, a chave do produto é o nome.
        cabecalho = "Produto;Qtde;Valor Total;Total Custo\n"
        janeiro = (("Relatorio\nPeriodo: 01/01/2026 a 31/01/2026\n" + cabecalho
                    + "Café 500g;10;200,00;150,00\nPão Francês;5;30,00;20,00\n").encode("utf-8")
                   + b"Feij\xe3o 1kg;4;36,00;28,00\n")
        fevereiro = (("Relatorio\nPeriodo: 01/02/2026 a 28/02/2026\n" + cabecalho
                      + "Café 500g;8;160,00;120,00\n").encode("utf-8")
                     + "Feijão 1kg;2;18,00;14,00\nAçúcar União 1kg;3;15,00;9,00\nMaçã Gala kg;1;8,00;5,00\n"
                     .encode("cp1252"))
        resultado = executar_como_o_worker([self.arquivo("jan.csv", janeiro), self.arquivo("fev.csv", fevereiro)],
                                           LIMITES)
        self.assertEqual(resultado["saude"]["itens_vendidos"], 5)
        for arquivo, celulas in ((1, 2), (2, 1)):
            self.assertIn(f"O arquivo {arquivo} misturava codificações de texto: os acentos de {celulas} célula(s) "
                          "foram corrigidos, para o mesmo produto não virar dois entre os meses.", resultado["avisos"])

    def test_utf8_puro_nao_passa_pelo_reparo(self):
        matriz = read_raw(self.arquivo("utf8.csv", "Produto;Qtde;Valor Total\nCafé 500g;2;40,00\n"), LIMITES)
        self.assertEqual(matriz.attrs["encoding"], "utf-8-sig")
        self.assertEqual(matriz.attrs["celulas_recodificadas"], 0)

    def test_analise_avisa_e_conta_o_reparo(self):
        resultado = self.calcular(self.arquivo("misto.csv", self.MISTO))
        self.assertEqual(resultado.telemetria["celulas_recodificadas"], {"vendas": 2})
        self.assertIn("Café 500g", {p["nome"] for p in resultado.kpis["curva_abc"]["produtos"]})
        self.assertTrue([a for a in resultado.alertas if a.startswith("Codificação do texto: 2 célula(s)")])

    def test_mesmo_produto_nao_vira_dois_entre_os_meses(self):
        janeiro = self.arquivo("janeiro.csv", (
            "Relatorio\nPeriodo: 01/01/2026 a 31/01/2026\nProduto;Qtde;Valor Total\nCafé 500g;2;40,00\n"
            "Pão Francês;1;8,00\n"))
        fevereiro = self.arquivo("fevereiro.csv", (
            "Relatorio\nPeriodo: 01/02/2026 a 28/02/2026\n".encode("utf-8") + self.MISTO))
        resultado = executar_como_o_worker([janeiro, fevereiro], LIMITES)
        self.assertEqual(resultado["saude"]["itens_vendidos"], 3)
        self.assertIn("O arquivo 2 misturava codificações de texto: os acentos de 2 célula(s) foram corrigidos, "
                      "para o mesmo produto não virar dois entre os meses.", resultado["avisos"])

    def test_copia_neutralizada_da_baia_continua_conferindo(self):
        com_formula = self.MISTO + b"=1+1;1;1,00\n"
        caminho = self.arquivo("misto.csv", com_formula)
        with using_limits(LIMITES):
            relatorio = baia.triar(caminho, ".csv", LIMITES)
        self.assertEqual(relatorio["liberado"], "copia_neutralizada")
        copia = read_raw(str(Path(caminho).with_name(baia.NOME_LIBERADO + ".csv")), LIMITES)
        self.assertEqual(copia.iat[1, 0], "Café 500g")
        self.assertEqual(copia.iat[4, 0], "'=1+1")


if __name__ == "__main__":
    unittest.main()
