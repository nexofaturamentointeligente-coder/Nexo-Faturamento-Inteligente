"""Cobertura do motor sobre o leiaute real dos relatórios de um ERP de varejo.

Nasceu da auditoria de 10/11/2026 (Docs/AUDITORIA_COBERTURA_MOTOR_ETL.md), que rodou o motor sobre oito
relatórios reais "Giro de Vendas", e acompanha a unificação que veio dela (Docs/UNIFICACAO_MOTOR_ETL.md). As
fixtures (tests/leiautes_erp_support.py) reproduzem a forma desses relatórios com dados inventados. São
quatro grupos:

  1. GarantiasTests: o que o motor já lia certo nesse leiaute e não pode regredir.
  2. RecusasTests: o que continua recusado, com a orientação que a tela mostra.
  3. LacunasResolvidasTests: os oito casos que a auditoria deixou como falha esperada (aceito e errado, ou
     não lido). A unificação os resolveu: cada teste afirma o comportamento desejado e passa.
  4. UnificacaoTests: as regras novas (leiaute paginado, fechamento dos centavos, fatias mensais, custo a
     confirmar e os formatos .xls e .xml).

Os testes marcados como "achado da revisão final" e "pedidos pelo teste de mutação" vêm da revisão independente
feita antes do commit (seção 10 do documento da unificação): os primeiros cobrem os defeitos que ela achou, e
os outros fixam regras que nenhum teste conferia (cada um falha quando a regra dele é trocada no motor).

O worker real exige Linux; como nos outros testes, as mesmas funções rodam no próprio processo.
"""

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import etl_biff
import etl_planilha_xml
import etl_processor
import raio_x
import security_gatekeeper
import triagem_cenario
from etl_inputs import ETLInputError, read_raw, validate_input
from etl_limits import get_limits, using_limits
from tests import leiautes_erp_support as leiautes
from tests.raio_x_support import avaliar_como_o_worker, executar_como_o_worker

HOJE = date(2026, 11, 9)
RESUMIDOS_POR_DIMENSAO = ("por_grupo_de_produto", "por_fornecedor", "por_forma_de_pagamento", "por_grupo_de_cliente",
                          "ranking_de_clientes")
PERIODO_DE_JANEIRO = "Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 31/01/2026\n"
PERIODO_DE_DOIS_MESES = "Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 28/02/2026\n"


class _LeiautesCase(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="nexo-leiautes-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)

    def xlsx(self, nome, **opcoes):
        """Grava o relatório fictício em .xlsx, na geometria real, e devolve (caminho, verdade)."""
        gravacao = {chave: opcoes.pop(chave) for chave in ("branco_com_estilo_na_coluna",) if chave in opcoes}
        linhas, mesclas, formatos, verdade = leiautes.RELATORIOS[nome](**opcoes)
        caminho = leiautes.gravar_xlsx(linhas, mesclas, formatos, self.pasta / f"{nome}_{len(list(self.pasta.iterdir()))}.xlsx",
                                       **gravacao)
        return caminho, verdade

    def csv(self, texto, nome="relatorio.csv"):
        caminho = self.pasta / nome
        caminho.write_bytes(texto.encode("utf-8"))
        return str(caminho)

    def detalhado_alterado(self, alterar, nome="detalhado_alterado.xlsx"):
        """O relatório detalhado com as linhas de item passadas por `alterar(linha)` antes de gravar."""
        linhas, mesclas, formatos, _verdade = leiautes.relatorio_detalhado()
        for linha in linhas:
            if isinstance(linha.get(0), datetime):
                alterar(linha)
        return leiautes.gravar_xlsx(linhas, mesclas, formatos, self.pasta / nome)

    def recusa(self, codigo, funcao, *argumentos, **nomeados):
        with self.assertRaises(ETLInputError) as contexto:
            funcao(*argumentos, **nomeados)
        self.assertEqual(contexto.exception.code, codigo)
        return contexto.exception.safe_message


# =============================================================================
# 1. O que o motor já lia certo no leiaute real
# =============================================================================
class GarantiasTests(_LeiautesCase):
    def test_relatorio_por_produto_e_lido_inteiro_e_fecha_com_a_linha_de_totais(self):
        caminho, verdade = self.xlsx("por_produto")
        vendas = etl_processor.carregar_vendas(caminho)
        self.assertEqual(len(vendas), verdade["produtos"])
        self.assertAlmostEqual(float(vendas["quantidade"].sum()), verdade["quantidade"], places=3)
        self.assertAlmostEqual(float(vendas["valor_venda"].sum()), verdade["venda"], places=2)
        self.assertAlmostEqual(float(vendas["valor_custo"].sum()), verdade["custo"], places=2)
        # O código é texto e mantém o zero à esquerda; o estoque negativo chega negativo.
        self.assertEqual(vendas["codigo"].iloc[0], "0700019")
        self.assertEqual(int((vendas["estoque"] < 0).sum()), verdade["estoque_negativo"])

    def test_cabecalho_repetido_filtros_totais_e_rodapes_nao_viram_produto(self):
        # Três páginas: o cabeçalho de duas linhas se repete, com o rodapé do software entre elas.
        caminho, verdade = self.xlsx("por_produto", paginas=3)
        vendas = etl_processor.carregar_vendas(caminho)
        esperados = {produto[1] for produto in leiautes.PRODUTOS} | {leiautes.SEM_CUSTO[1]}
        self.assertEqual(set(vendas["produto"]), esperados)
        self.assertEqual(len(vendas), verdade["produtos"])

    def test_raio_x_le_o_relatorio_por_produto_e_o_periodo_com_ano_de_dois_digitos(self):
        caminho, verdade = self.xlsx("por_produto")
        resultado = executar_como_o_worker([caminho])
        self.assertEqual(resultado["saude"]["faturamento_total"], verdade["venda"])
        self.assertEqual(resultado["saude"]["itens_vendidos"], verdade["produtos"])
        self.assertEqual(resultado["saude"]["itens_sem_custo"], verdade["sem_custo"])
        self.assertEqual(resultado["auditoria"]["custo_total"], verdade["custo"])
        # "Período de  : 09/11/23 á 09/11/26": ano com dois dígitos e "á" com acento agudo.
        self.assertEqual(resultado["periodo"], {"inicio": "2023-11-09", "fim": "2026-11-09", "meses": 37})

    def test_csv_como_a_tela_mostra_le_milhar_e_negativo_entre_parenteses(self):
        linhas, _mesclas, formatos, verdade = leiautes.relatorio_por_produto()
        caminho = leiautes.gravar_csv(linhas, formatos, self.pasta / "por_produto.csv")
        self.assertIn("(4,00)", Path(caminho).read_text(encoding="cp1252"))
        vendas = etl_processor.carregar_vendas(caminho)
        self.assertEqual(len(vendas), verdade["produtos"])
        self.assertAlmostEqual(float(vendas["valor_venda"].sum()), verdade["venda"], places=2)
        self.assertAlmostEqual(float(vendas["valor_custo"].sum()), verdade["custo"], places=2)
        self.assertEqual(int((vendas["estoque"] < 0).sum()), verdade["estoque_negativo"])

    def test_numero_com_tres_casas_na_planilha_nao_vira_milhar(self):
        linhas, mesclas, formatos, verdade = leiautes.relatorio_por_produto()
        linha = next(l for l in linhas if l.get(0) == "0700019")
        esperado = round(verdade["venda"] - linha[10] + 19.431, 3)
        linha[10] = 19.431
        caminho = leiautes.gravar_xlsx(linhas, mesclas, formatos, self.pasta / "tres_casas.xlsx")
        vendas = etl_processor.carregar_vendas(caminho)
        self.assertAlmostEqual(float(vendas["valor_venda"].sum()), esperado, places=3)

    def test_conversores_com_os_valores_reais(self):
        casos = {"1.460.035,77": 1460035.77, "(396.747,48)": -396747.48, "1434,0": 1434.0, "3.8081": 3.8081,
                 "19.4310": 19.431}
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(etl_processor._parse_valor(texto), esperado)
                self.assertEqual(etl_processor.converter_valor_monetario(texto), esperado)
        # Percentual numa coluna de dinheiro não entra como valor.
        self.assertEqual(etl_processor.converter_valor_monetario("-99,97 %"), 0.0)


# =============================================================================
# 2. O que continua recusado, com orientação
# =============================================================================
class RecusasTests(_LeiautesCase):
    def test_pdf_continua_recusado_com_orientacao(self):
        """A tela do ERP imprime PDF: não é planilha, e o motor não lê."""
        pdf = self.pasta / "relatorio.pdf"
        pdf.write_bytes(leiautes.PDF_MINIMO)
        self.assertIn("CSV, XLSX ou XLS", self.recusa("unsupported", validate_input, str(pdf), ".pdf", get_limits()))
        self.recusa("quarentena:extensao_nao_aceita", security_gatekeeper.inspecionar_arquivo, str(pdf), "pdf")
        self.recusa("quarentena:extensao_nao_aceita", security_gatekeeper.inspecionar_arquivo, str(pdf), "pdf",
                    leitura_direta=True)

    def test_periodo_de_36_meses_sem_data_por_linha_e_recusado_pela_avaliacao_de_cenario(self):
        """O relatório resumido por produto não traz a data de cada venda: um arquivo que cobre vários anos
        não pode ser fatiado, e a Avaliação de Cenário pede um relatório por mês ou por ano civil."""
        caminho, _verdade = self.xlsx("por_produto")
        mensagem = self.recusa("avaliacao_periodo_entre_anos", avaliar_como_o_worker, [caminho], hoje=HOJE)
        self.assertIn("09/11/2023 a 09/11/2026", mensagem)

    def test_teto_de_celulas_do_xlsx_conta_a_area_e_nao_as_celulas_com_valor(self):
        """O orçamento do .xlsx conta linhas x colunas, vazias incluídas: uma coluna só com células em branco
        formatadas (a última do relatório detalhado convertido) pode derrubar o arquivo. O .xls original do
        ERP, lido direto, conta só as células com valor (UnificacaoTests)."""
        com_branco, _verdade = self.xlsx("detalhado", branco_com_estilo_na_coluna=18)
        sem_branco, _verdade = self.xlsx("detalhado")
        linhas = len(leiautes.relatorio_detalhado()[0])
        preenchidas = sum(len(linha) for linha in leiautes.relatorio_detalhado()[0])
        teto = linhas * 19 - 1
        self.assertLess(preenchidas, teto)
        politica = replace(get_limits(), max_cells_per_sheet=teto, max_cells_per_file=teto)
        with using_limits(politica):
            self.recusa("cells", validate_input, com_branco, ".xlsx", politica)
            self.assertEqual(validate_input(sem_branco, ".xlsx", politica)["filled_cells_selected"], preenchidas)

    def test_abaixo_do_custo_e_recusado_com_a_orientacao_dos_titulos_repetidos(self):
        """O relatório de vendas abaixo do custo tem dois títulos "Valor Unitário" (compra e vendido): a
        leitura recusa, na planilha e no .csv, e o Raio-X diz o motivo depois do número do arquivo."""
        planilha, _verdade = self.xlsx("abaixo_do_custo")
        linhas, _mesclas, formatos, _verdade = leiautes.relatorio_abaixo_do_custo()
        alinhado = leiautes.gravar_csv(linhas, formatos, self.pasta / "abaixo_alinhado.csv",
                                       alinhar=leiautes.ALINHAMENTO_ABAIXO_DO_CUSTO)
        for caminho in (planilha, alinhado):
            with self.subTest(arquivo=Path(caminho).suffix):
                self.assertIn("dois títulos de coluna iguais", self.recusa("data", etl_processor.carregar_vendas, caminho))
                mensagem = self.recusa("raio_x_dados", executar_como_o_worker, [caminho])
                self.assertTrue(mensagem.startswith("Arquivo 1: o relatório tem dois títulos de coluna iguais"), mensagem)
                self.assertIn("Arquivo 1: o relatório tem dois títulos",
                              self.recusa("raio_x_dados", avaliar_como_o_worker, [caminho], hoje=HOJE))

    def test_relatorios_sem_coluna_de_produto_sao_recusados(self):
        """Os relatórios por grupo, fornecedor, forma de pagamento, grupo de cliente e o ranking não têm
        coluna de produto (quatro deles, nem quantidade): não são vendas por produto."""
        for nome in RESUMIDOS_POR_DIMENSAO:
            with self.subTest(relatorio=nome):
                caminho, _verdade = self.xlsx(nome)
                self.recusa("raio_x_dados", executar_como_o_worker, [caminho])


# =============================================================================
# 3. As oito lacunas da auditoria, resolvidas pela unificação
# =============================================================================
class LacunasResolvidasTests(_LeiautesCase):
    def test_b4_venda_detalhada_deveria_ser_lida(self):
        """B4. O relatório mais rico (venda item a item) tem o título numa coluna e o dado em outra. A leitura
        alinha os dois pela ordem, lê os itens e fecha com o total do relatório, no centavo."""
        caminho, verdade = self.xlsx("detalhado")
        resultado = executar_como_o_worker([caminho])
        self.assertAlmostEqual(resultado["saude"]["faturamento_total"], verdade["venda"], places=2)
        self.assertEqual(resultado["saude"]["faturamento_total"], round(verdade["venda"], 2))
        self.assertEqual(resultado["saude"]["itens_vendidos"], verdade["produtos"])

    def test_b9_b10_arquivo_de_varios_anos_com_data_por_linha_deveria_virar_meses(self):
        """B9 e B10. Com a data de cada venda, o arquivo de vários anos vira um relatório por mês, em memória,
        e a Avaliação de Cenário devolve as linhas por produto e ano."""
        caminho, verdade = self.xlsx("detalhado")
        resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
        self.assertTrue(resultado["linhas"]["valores"])
        meta = resultado["meta"]
        # Um relatório por mês do período do cabeçalho (09/11/2023 a 09/11/2026), com venda ou sem.
        self.assertEqual(meta["arquivos_por_ano"], {"2023": 2, "2024": 12, "2025": 12, "2026": 11})
        self.assertEqual(set(meta["relatorios_mensais_por_ano"].values()), {True})
        self.assertEqual(meta["linhas_lidas"], verdade["itens"])
        venda = resultado["linhas"]["colunas"].index("valor_venda_bruto")
        self.assertAlmostEqual(sum(linha[venda] for linha in resultado["linhas"]["valores"]),
                               round(verdade["venda"], 2), places=2)

    def test_b7_abaixo_do_custo_nao_pode_usar_o_unitario_de_compra_como_preco(self):
        """B7. Com dois títulos "Valor Unitário", o primeiro (compra) virava o preço de venda e o faturamento
        saía como quantidade x custo, sem aviso (na auditoria: R$ 8,2 milhões em vez de 3,9). A leitura
        recusa o título repetido e diz por quê."""
        linhas, _mesclas, formatos, _verdade = leiautes.relatorio_abaixo_do_custo()
        caminho = leiautes.gravar_csv(linhas, formatos, self.pasta / "abaixo_alinhado.csv",
                                      alinhar=leiautes.ALINHAMENTO_ABAIXO_DO_CUSTO)
        self.assertIn("dois títulos de coluna iguais", self.recusa("raio_x_dados", executar_como_o_worker, [caminho]))

    def test_compras_nao_podem_aceitar_em_silencio_um_relatorio_de_vendas(self):
        """O relatório de vendas por produto, lido como COMPRAS, era aceito: a linha de totais entrava como
        item e o "total comprado" saía o dobro do custo. A leitura das compras recusa."""
        caminho, _verdade = self.xlsx("por_produto")
        self.assertIn("é um relatório de VENDAS", self.recusa("data", etl_processor.carregar_compras, caminho))

    def test_baia_nao_pode_liberar_o_relatorio_por_forma_de_pagamento(self):
        """A estrutura mínima da Baia de Segurança lia dois avisos do rodapé como cabeçalho de compras e
        liberava um relatório sem produto nem fornecedor. Frase não é título de coluna: quarentena."""
        caminho, _verdade = self.xlsx("por_forma_de_pagamento")
        self.recusa("quarentena:estrutura_ausente", security_gatekeeper.triar, caminho, "xlsx", get_limits())

    def test_nome_de_produto_com_virgulas_nao_pode_trocar_o_separador_do_csv(self):
        """O separador escolhido era o que dava mais colunas em qualquer linha: um nome com vírgulas, num
        arquivo de valores com vírgula decimal, trocava o separador do arquivo inteiro. Vale a largura
        típica das linhas (a maior que mais de um quinto delas alcança, e nunca uma linha sozinha): as 8
        colunas separadas por ponto e vírgula."""
        cabecalho = "Codigo;Nome do produto;Qtde Vend;Total Venda;Total Custo;Marg.;Lucro;Estoque Atual\r\n"
        corpo = "".join(f"{n};PRODUTO FICTICIO {n};{n},0;1{n},50;{n},25;10,00;2,25;{n},00\r\n" for n in range(1, 6))
        com_virgulas = "9;KIT FICTICIO 0,9L, 3,6L E 18L;2,0;30,50;20,25;10,00;10,25;4,00\r\n"
        caminho = self.pasta / "virgulas.csv"
        caminho.write_bytes((cabecalho + corpo + com_virgulas).encode("cp1252"))
        matriz = read_raw(str(caminho), get_limits())
        self.assertEqual(matriz.shape[1], 8)
        self.assertEqual(matriz.attrs["separator"], ";")
        self.assertEqual(matriz.iat[6, 1], "KIT FICTICIO 0,9L, 3,6L E 18L")

    def test_custo_implausivel_nao_pode_dominar_a_perda_do_raio_x(self):
        """O Raio-X do cliente não tinha a regra de custo implausível da triagem do lead: um item genérico de
        custo absurdo gerava uma "perda abaixo do custo" maior que o faturamento inteiro (na auditoria, 99,8%
        de R$ 1,1 milhão vinha de um item). O item sai da perda e do recuperável e fica "a confirmar"."""
        caminho, verdade = self.xlsx("por_produto", generico=True)
        resultado = executar_como_o_worker([caminho])
        self.assertLess(resultado["auditoria"]["perda_abaixo_custo"], resultado["saude"]["faturamento_total"])
        a_confirmar = resultado["auditoria"]["custo_a_confirmar"]
        generico = leiautes.GENERICO
        self.assertEqual(a_confirmar["itens"], 1)
        self.assertEqual(a_confirmar["lista"][0]["produto"], generico[1])
        self.assertEqual(a_confirmar["custo_total"], generico[4])
        self.assertEqual(a_confirmar["perda_desconsiderada"], generico[4] - generico[3])
        self.assertEqual(resultado["auditoria"]["perda_abaixo_custo"], 0.0)
        self.assertEqual(resultado["recuperavel"]["total"], 0.0)
        self.assertNotIn(generico[1], [item["produto"] for item in resultado["auditoria"]["itens_abaixo_custo"]])
        # O custo total e a margem do envio continuam contando o item: só a perda e o recuperável o deixam de fora.
        self.assertEqual(resultado["auditoria"]["custo_total"], verdade["custo"])
        self.assertTrue(any("ficaram fora da perda" in aviso for aviso in resultado["avisos"]))

    def test_percentual_nao_pode_entrar_como_quantidade_ou_estoque(self):
        """O conversor de quantidade e de estoque lia "-99,97 %" como -99,97, sem alerta (o de dinheiro já
        barrava). Percentual não é quantidade."""
        self.assertIsNone(etl_processor._parse_valor("-99,97 %"))
        self.assertIsNone(etl_processor._parse_valor("45,00%"))
        self.assertEqual(etl_processor._parse_valor("-99,97"), -99.97)


# =============================================================================
# 4. As regras novas da unificação
# =============================================================================
class LeiautePaginadoTests(_LeiautesCase):
    def test_venda_detalhada_e_lida_item_a_item_pela_analise_mensal_tambem(self):
        caminho, verdade = self.xlsx("detalhado")
        vendas = etl_processor.carregar_vendas(caminho)
        self.assertEqual(len(vendas), verdade["itens"])
        self.assertEqual(set(vendas["produto"]), {item[2] for item in leiautes.ITENS})
        self.assertAlmostEqual(float(vendas["quantidade"].sum()), verdade["quantidade"], places=3)
        self.assertAlmostEqual(float(vendas["valor_venda"].sum()), verdade["venda"], places=4)

    def test_cliente_do_bloco_rodapes_e_totais_nao_viram_item(self):
        caminho, verdade = self.xlsx("detalhado", clientes=7)
        vendas = etl_processor.carregar_vendas(caminho)
        self.assertEqual(len(vendas), verdade["itens"])
        self.assertEqual(set(vendas["produto"]), {item[2] for item in leiautes.ITENS})

    def test_relatorio_com_titulo_e_dado_na_mesma_coluna_nao_e_mexido(self):
        caminho, _verdade = self.xlsx("por_produto")
        bruto = etl_processor._ler_bruto(caminho)
        idx, mapa, linhas_cab = etl_processor._detectar_cabecalho(bruto, etl_processor.ALIAS_VENDAS,
                                                                  etl_processor._validar_vendas)
        self.assertIs(etl_processor._alinhar_relatorio_paginado(bruto, idx, linhas_cab, mapa), bruto)

    def test_so_alinha_quando_a_coluna_do_titulo_do_produto_nao_tem_dado_nenhum(self):
        import pandas as pd
        cabecalho = ["Data", None, "Produto", "Qtde", "Valor Total", None]
        itens = [[f"0{n}/01/2026", f"PRODUTO {n}", None, None, str(n), f"{n}0,00"] for n in range(1, 6)]
        mapa = {"produto": 2, "quantidade": 3, "valor_venda": 4}
        paginado = pd.DataFrame([cabecalho, *itens, cabecalho, *itens], dtype=object)

        def linha(matriz, numero):
            return [None if etl_processor._celula_vazia(valor) else valor for valor in matriz.iloc[numero].tolist()]

        alinhado = etl_processor._alinhar_relatorio_paginado(paginado, 0, 1, mapa)
        self.assertEqual(linha(alinhado, 0), cabecalho)
        self.assertEqual(linha(alinhado, 1), ["01/01/2026", None, "PRODUTO 1", "1", "10,00", None])
        self.assertEqual(linha(alinhado, 6), [None] * 6)  # o cabeçalho repetido no meio sai: a linha fica vazia
        self.assertEqual(linha(alinhado, 11), ["05/01/2026", None, "PRODUTO 5", "5", "50,00", None])
        # Um valor que não é o título, na coluna do título do produto: não é o leiaute paginado.
        com_dado = pd.DataFrame([cabecalho, *itens, [None, None, "OBSERVACAO", None, None, None]], dtype=object)
        self.assertIs(etl_processor._alinhar_relatorio_paginado(com_dado, 0, 1, mapa), com_dado)

    def test_coluna_a_menos_compensada_por_outra_e_pega_quando_poe_numero_no_lugar_do_nome(self):
        """Achado da revisão: sem o vendedor e com a unidade ("UN") numa coluna sem título, há 7 títulos e 7
        colunas com dado, mas fora de posição: o preço unitário cairia no lugar do nome do produto. A prova de
        leitura pega este caso; o da coluna de TEXTO sem título ao lado do nome é limite conhecido (ver a
        docstring de _alinhar_relatorio_paginado e a seção 10 de Docs/UNIFICACAO_MOTOR_ETL.md)."""
        def deslocar(linha):
            linha.pop(1)
            linha[15] = "UN"
        caminho = self.detalhado_alterado(deslocar, "deslocado.xlsx")
        self.assertIn("sem nomes de produto válidos", self.recusa("data", etl_processor.carregar_vendas, caminho))
        self.recusa("raio_x_dados", executar_como_o_worker, [caminho])

    def test_numero_no_lugar_do_nome_do_produto_nao_e_alinhado(self):
        """Achado da revisão: a coluna "Produto" vazia e uma coluna sem título só com valores. O número não
        vira nome de produto: o relatório continua recusado, como antes."""
        import pandas as pd
        linhas = [["Codigo", None, "Produto", "Qtde", "Valor Total", "Total Custo"]]
        linhas += [[str(1000 + n), f"{200 + n},64", None, str(10 + n), f"{30 + n},83", f"{40 + n},89"] for n in range(8)]
        bruto = pd.DataFrame(linhas, dtype=object)
        mapa = {"codigo": 0, "produto": 2, "quantidade": 3, "valor_venda": 4, "valor_custo": 5}
        self.assertIs(etl_processor._alinhar_relatorio_paginado(bruto, 0, 1, mapa), bruto)
        with self.assertRaises(etl_processor.ETLValidationError):
            etl_processor._vendas_do_bruto(bruto, "sem_produto")

    def test_celula_em_branco_em_parte_das_linhas_de_item_nao_impede_a_leitura(self):
        """O vendedor em branco em metade das linhas: a linha de item continua a mesma, com uma célula a menos."""
        contador = iter(range(1000))

        def sem_vendedor(linha):
            if next(contador) % 2:
                linha.pop(1)
        caminho = self.detalhado_alterado(sem_vendedor, "sem_vendedor.xlsx")
        _linhas, _mesclas, _formatos, verdade = leiautes.relatorio_detalhado()
        vendas = etl_processor.carregar_vendas(caminho)
        self.assertEqual(len(vendas), verdade["itens"])
        self.assertEqual(set(vendas["produto"]), {item[2] for item in leiautes.ITENS})
        self.assertAlmostEqual(float(vendas["valor_venda"].sum()), verdade["venda"], places=4)

    def test_quantidade_de_titulos_diferente_da_de_colunas_com_dado_nao_e_alinhada(self):
        """Sem a correspondência um a um entre títulos e colunas com dado, o motor não adivinha: recusa."""
        # A coluna 13 fica fora das mesclas da linha de item: são 8 colunas com dado para 7 títulos.
        caminho = self.detalhado_alterado(lambda linha: linha.update({13: "EXTRA"}))
        self.assertIn("sem nomes de produto válidos", self.recusa("data", etl_processor.carregar_vendas, caminho))

    # --- Testes que o teste de mutação da revisão independente final pediu ---------------------------------
    CABECALHO = ["Data", None, "Produto", "Qtde", "Valor Total", None]
    MAPA = {"produto": 2, "quantidade": 3, "valor_venda": 4}

    def test_linhas_de_duas_celulas_nao_decidem_a_forma_das_linhas_de_item(self):
        """A forma das linhas de item sai das linhas com três células ou mais: as linhas de observação, de
        duas células, podem ser mais numerosas que os itens sem impedir o alinhamento."""
        import pandas as pd
        linhas = [self.CABECALHO]
        for n in range(1, 4):
            linhas.append([f"0{n}/01/2026", f"PRODUTO {n}", None, None, str(n), f"{n}0,00"])
            linhas.append([None, "obs.:", None, "entrega", None, None])
            linhas.append([None, "obs.:", None, "retirada", None, None])
        paginado = pd.DataFrame(linhas, dtype=object)
        alinhado = etl_processor._alinhar_relatorio_paginado(paginado, 0, 1, self.MAPA)
        self.assertIsNot(alinhado, paginado)
        self.assertEqual([valor for valor in alinhado.iloc[1:, 2].tolist() if not etl_processor._celula_vazia(valor)],
                         ["PRODUTO 1", "PRODUTO 2", "PRODUTO 3"])

    def test_corpo_sem_linha_de_tres_celulas_volta_como_veio(self):
        import pandas as pd
        bruto = pd.DataFrame([["Produto", "Qtde", "Valor Total"], ["CAFE", "2", None], ["LEITE", "1", None]], dtype=object)
        mapa = {"produto": 0, "quantidade": 1, "valor_venda": 2}
        self.assertIs(etl_processor._alinhar_relatorio_paginado(bruto, 0, 1, mapa), bruto)
        vendas = etl_processor.carregar_vendas(self.csv("Produto;Qtde;Valor Total\nCAFE;2;\nLEITE;1;\n"))
        self.assertEqual((len(vendas), float(vendas["valor_venda"].sum())), (2, 0.0))

    def test_celula_a_mais_na_minoria_das_linhas_entra_na_conta_das_colunas_de_dado(self):
        """O vendedor em branco em dois terços das linhas: a forma mais comum não tem o vendedor, e a forma
        com ele (um terço das linhas, acima de um quinto) completa as colunas de dado."""
        contador = iter(range(1000))

        def sem_vendedor(linha):
            if next(contador) % 3:
                linha.pop(1)
        caminho = self.detalhado_alterado(sem_vendedor, "vendedor_em_um_terco.xlsx")
        _linhas, _mesclas, _formatos, verdade = leiautes.relatorio_detalhado()
        vendas = etl_processor.carregar_vendas(caminho)
        self.assertEqual(len(vendas), verdade["itens"])
        self.assertAlmostEqual(float(vendas["valor_venda"].sum()), verdade["venda"], places=4)

    def test_celula_solta_numa_linha_so_nao_impede_o_alinhamento(self):
        """Uma anotação numa coluna sem título, numa linha de item em dezoito (menos de um quinto): não é
        coluna de dado, e o relatório continua lido inteiro."""
        contador = iter(range(1000))

        def anotar(linha):
            if next(contador) == 4:
                linha[13] = "OBS"
        caminho = self.detalhado_alterado(anotar, "anotacao.xlsx")
        _linhas, _mesclas, _formatos, verdade = leiautes.relatorio_detalhado()
        vendas = etl_processor.carregar_vendas(caminho)
        self.assertEqual(len(vendas), verdade["itens"])
        self.assertAlmostEqual(float(vendas["valor_venda"].sum()), verdade["venda"], places=4)

    def test_titulo_fora_da_coluna_visual_do_dado_nao_e_alinhado(self):
        """Geometria: três títulos à direita de todas as colunas com dado. A quantidade bate e a prova de
        leitura passaria, mas o título não está na coluna visual do dado: o motor não adivinha."""
        import pandas as pd
        cabecalho = [None, None, None, None, "Produto", "Qtde", "Valor Total"]
        itens = [[f"PRODUTO {n}", str(n), f"{n}0,00", None, None, None, None] for n in range(1, 6)]
        bruto = pd.DataFrame([cabecalho, *itens], dtype=object)
        mapa = {"produto": 4, "quantidade": 5, "valor_venda": 6}
        self.assertIs(etl_processor._alinhar_relatorio_paginado(bruto, 0, 1, mapa), bruto)

    def test_texto_no_lugar_da_quantidade_nao_e_alinhado(self):
        """A prova de leitura vale também para os números: a unidade ("UN") embaixo do título da quantidade. O
        texto que começa por letra e traz um número ("CX 12") também não é número (achado da verificação das
        correções: o conversor de quantidade tira o 12 do texto, e a quantidade saía errada). A moeda antes do
        número não atrapalha."""
        import pandas as pd
        mapa = {"codigo": 0, "produto": 2, "quantidade": 3, "valor_venda": 4, "valor_custo": 5}

        def paginado(quantidade, valor):
            linhas = [["Codigo", None, "Produto", "Qtde", "Valor Total", "Total Custo"]]
            linhas += [[str(1000 + n), f"PRODUTO {n}", None, quantidade, valor.format(30 + n), f"{20 + n},89"]
                       for n in range(8)]
            return pd.DataFrame(linhas, dtype=object)

        for quantidade, valor, alinha in (("UN", "{},83", False), ("CX 12", "{},83", False), ("2", "LOTE {}", False),
                                          ("2", "{},83", True), ("2 UN", "R$ {},83", True)):
            with self.subTest(quantidade=quantidade, valor=valor):
                bruto = paginado(quantidade, valor)
                self.assertEqual(etl_processor._alinhar_relatorio_paginado(bruto, 0, 1, mapa) is not bruto, alinha)

    def test_prova_de_leitura_pede_noventa_por_cento_das_linhas_de_item(self):
        """Um código de barras no lugar do nome em 5% das linhas de item ainda alinha, e em 10% exatos também
        (a prova pede ao menos 90%; em ponto flutuante, 1 - 0,9 dava menos que 0,1, e os 90% exatos eram
        recusados: achado da revisão final); em 15%, não."""
        import pandas as pd

        def paginado(nomes_numericos, itens=20):
            linhas = [[f"{n:02d}/01/2026", str(7890000 + n) if n <= nomes_numericos else f"PRODUTO {n}", None, None,
                       str(n), f"{n}0,00"] for n in range(1, itens + 1)]
            return pd.DataFrame([self.CABECALHO, *linhas], dtype=object)

        for nomes_numericos, itens, alinha in ((1, 20, True), (2, 20, True), (1, 10, True), (5, 50, True), (3, 20, False)):
            with self.subTest(nomes_numericos=nomes_numericos, itens=itens):
                bruto = paginado(nomes_numericos, itens)
                alinhado = etl_processor._alinhar_relatorio_paginado(bruto, 0, 1, self.MAPA)
                self.assertEqual(alinhado is not bruto, alinha)

    def test_matriz_alinhada_mantem_os_dados_da_leitura(self):
        """O formato do arquivo (só o texto do CSV prova o número en-US) e as células recodificadas seguem
        com a matriz alinhada."""
        import pandas as pd
        itens = [[f"0{n}/01/2026", f"PRODUTO {n}", None, None, str(n), f"{n}0,00"] for n in range(1, 6)]
        paginado = pd.DataFrame([self.CABECALHO, *itens], dtype=object)
        paginado.attrs.update({"format": "csv", "separator": ";", "celulas_recodificadas": 2})
        alinhado = etl_processor._alinhar_relatorio_paginado(paginado, 0, 1, self.MAPA)
        self.assertIsNot(alinhado, paginado)
        self.assertEqual(alinhado.attrs, {"format": "csv", "separator": ";", "celulas_recodificadas": 2})


class FechamentoDosCentavosTests(_LeiautesCase):
    INFORMADO = PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\n" + "".join(
        f"PRODUTO {letra};1;3,3333\n" for letra in "ABC")
    PARA_BAIXO = PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\n" + "".join(
        f"PRODUTO {letra};1;1,6666\n" for letra in "ABC")
    CALCULADO = PERIODO_DE_JANEIRO + "Produto;Qtde;Preco Unitario\n" + "".join(
        f"PRODUTO {letra};0,333;10,01\n" for letra in "ABC")

    def test_total_informado_com_fracao_de_centavo_fecha_com_a_soma_do_relatorio(self):
        # 3 x 3,3333 = 9,9999: cada linha arredondada daria 9,99; o relatório (e a análise mensal) somam 10,00.
        caminho = self.csv(self.INFORMADO)
        resultado = executar_como_o_worker([caminho])
        self.assertEqual(resultado["saude"]["faturamento_total"], 10.0)
        self.assertEqual(resultado["fatias"][0]["faturamento"], 10.0)
        self.assertEqual(round(float(etl_processor.carregar_vendas(caminho)["valor_venda"].sum()), 2), 10.0)

    def test_a_diferenca_tambem_e_tirada_quando_o_arredondamento_passa_da_soma(self):
        # 3 x 1,6666 = 4,9998: cada linha arredondada daria 5,01; o relatório soma 5,00.
        resultado = executar_como_o_worker([self.csv(self.PARA_BAIXO)])
        self.assertEqual(resultado["saude"]["faturamento_total"], 5.0)

    def test_cada_linha_continua_em_centavos_e_a_menos_de_um_centavo_do_valor_informado(self):
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(self.csv(self.INFORMADO), 0, False)
        valores = df["valor_venda"].tolist()
        self.assertEqual(sorted(valores), [3.33, 3.33, 3.34])
        self.assertEqual(valores[0], 3.34)  # restos iguais: a linha de cima recebe o centavo
        for valor in valores:
            self.assertLess(abs(valor - 3.3333), 0.01)

    def test_o_centavo_vai_primeiro_para_a_linha_em_que_nao_muda_o_preco_por_unidade(self):
        """Achado da revisão: num item de balança (0,2 kg), um centavo muda o preço por quilo em 5 centavos e
        pareceria variação de preço. Entre as linhas arredondadas para baixo, a de quantidade 1 vem antes,
        mesmo com resto menor."""
        texto = (PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\nQUEIJO KG;0,2;0,7649\n"
                 "PRODUTO B;1;5,0030\nPRODUTO C;1;3,0030\n")
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(self.csv(texto), 0, False)
        self.assertEqual(df["valor_venda"].tolist(), [0.76, 5.01, 3.0])  # 8,7709 no relatório: 8,77

    def test_linha_de_valor_minimo_continua_sendo_venda_analisada(self):
        """Achado da revisão: a linha que fica com zero centavo não pode sair da análise (o produto dela
        deixaria de contar entre os itens vendidos)."""
        texto = (PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\nBRINDE;1;0,0020\n"
                 + "".join(f"PRODUTO {letra};1;3,3333\n" for letra in "ABC"))
        caminho = self.csv(texto)
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(caminho, 0, False)
        self.assertEqual([round(valor, 2) for valor in df["valor_venda"]], [0.0, 3.34, 3.33, 3.33])
        self.assertGreater(df["valor_venda"].iloc[0], 0)
        resultado = executar_como_o_worker([caminho])
        self.assertEqual((resultado["saude"]["faturamento_total"], resultado["saude"]["itens_vendidos"]), (10.0, 4))

    def test_valor_calculado_pelo_motor_continua_arredondado_por_linha(self):
        # Sem coluna de total, a linha é quantidade x preço (0,333 x 10,01 = 3,33333): o PDV cobrou 3,33.
        resultado = executar_como_o_worker([self.csv(self.CALCULADO)])
        self.assertEqual(resultado["saude"]["faturamento_total"], 9.99)

    def test_total_sem_fracao_de_centavo_nao_muda(self):
        caminho, verdade = self.xlsx("por_produto")
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(caminho, 0, False)
        self.assertAlmostEqual(float(df["valor_venda"].sum()), verdade["venda"], places=9)
        self.assertEqual(df["valor_venda"].tolist(), etl_processor.carregar_vendas(caminho)["valor_venda"].tolist())

    def test_linha_de_quantidade_um_nao_e_sensivel_mesmo_valendo_menos_de_dois_reais(self):
        """Só é sensível a linha de quantidade MENOR que 1 E valor menor que R$ 2,00. A bala vendida por
        unidade (quantidade 1, R$ 1,00) concorre pelo resto como as outras, e tem o maior."""
        texto = (PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\nPRODUTO A;2;5,0030\nBALA UN;1;1,0049\n"
                 "PRODUTO C;2;3,0021\n")
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(self.csv(texto), 0, False)
        self.assertEqual(df["valor_venda"].tolist(), [5.0, 1.01, 3.0])  # 9,0100 no relatório: 9,01

    def test_linha_sem_fracao_de_centavo_nunca_recebe_o_centavo(self):
        """Só recebe o centavo a linha que o arredondamento levou para o outro lado: a de valor exato (5,00)
        não se afasta do informado, mesmo sendo a única que não é de balança."""
        texto = (PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\nPRODUTO B;1;5,00\nQUEIJO KG;0,2;0,7649\n"
                 "PRESUNTO KG;0,3;0,5049\n")
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(self.csv(texto), 0, False)
        self.assertEqual(df["valor_venda"].tolist(), [5.0, 0.77, 0.5])  # 6,2698 no relatório: 6,27

    def test_linha_de_um_centavo_so_devolve_o_centavo_quando_nao_ha_outra(self):
        """Achado da revisão final: 0,0051 arredonda para 0,01. Com zero centavo a linha continuaria venda
        analisada e passaria a contar também como bonificação (o custo dela entraria duas vezes no CMV
        líquido). Quem devolve o centavo é outra linha, mesmo estando abaixo dela."""
        texto = (PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total;Total Custo\nBRINDE;1;0,0051;5,00\n"
                 "PRODUTO B;1;3,3351;2,00\nPRODUTO C;1;3,3351;2,00\n")
        caminho = self.csv(texto)
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(caminho, 0, False)
        self.assertEqual(df["valor_venda"].tolist(), [0.01, 3.33, 3.34])  # 6,6753 no relatório: 6,68
        resultado = executar_como_o_worker([caminho])
        self.assertEqual((resultado["saude"]["faturamento_total"], resultado["saude"]["itens_vendidos"]), (6.68, 3))
        self.assertEqual((resultado["auditoria"]["custo_bonificacoes"], resultado["auditoria"]["cmv_liquido"]), (0.0, 9.0))

    def test_sem_outra_linha_a_de_um_centavo_devolve_e_continua_venda_analisada(self):
        """Só linhas de um centavo, todas arredondadas para cima: uma delas devolve o centavo e fica abaixo de
        meio centavo (com o valor informado, o arredondamento seguinte devolveria o centavo tirado)."""
        texto = (PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\n"
                 + "".join(f"BRINDE {letra};1;0,0051\n" for letra in "ABC"))
        caminho = self.csv(texto)
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(caminho, 0, False)
        self.assertEqual([round(valor, 2) for valor in df["valor_venda"]], [0.0, 0.01, 0.01])  # 0,0153: 0,02
        self.assertGreater(df["valor_venda"].iloc[0], 0)
        resultado = executar_como_o_worker([caminho])
        self.assertEqual((resultado["saude"]["faturamento_total"], resultado["saude"]["itens_vendidos"]), (0.02, 3))

    def test_na_hora_de_receber_a_linha_de_um_centavo_concorre_como_as_outras(self):
        # A regra da linha de um centavo vale só para devolver: aqui falta um centavo, e ela tem o maior resto.
        texto = (PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\nBALA;1;0,0149\nPRODUTO B;1;3,3320\n"
                 "PRODUTO C;1;3,3331\n")
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(self.csv(texto), 0, False)
        self.assertEqual(df["valor_venda"].tolist(), [0.02, 3.33, 3.33])  # 6,6800 no relatório: 6,68

    def test_linha_de_valor_alto_nao_desliga_o_fechamento(self):
        """O teto do fechamento é de R$ 1 bilhão por linha: a venda de R$ 1.500,00 no mesmo relatório não
        impede o fechamento das outras linhas."""
        texto = (PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\nGELADEIRA;1;1500,0000\n"
                 + "".join(f"PRODUTO {letra};1;3,3333\n" for letra in "ABC"))
        self.assertEqual(executar_como_o_worker([self.csv(texto)])["saude"]["faturamento_total"], 1510.0)

    def test_acima_do_teto_do_fechamento_as_linhas_ficam_como_vieram(self):
        """A partir de R$ 1 bilhão numa linha o float não guarda o milionésimo: o relatório não é fechado e
        cada linha é só arredondada depois, como sempre."""
        texto = (PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\nNAVIO;1;1000000000,3333\n"
                 + "".join(f"PRODUTO {letra};1;3,3333\n" for letra in "ABC"))
        caminho = self.csv(texto)
        df, _formato, _periodo, _rotulo = raio_x._ler_vendas(caminho, 0, False)
        self.assertEqual(df["valor_venda"].tolist(), [1000000000.3333, 3.3333, 3.3333, 3.3333])
        self.assertEqual(executar_como_o_worker([caminho])["saude"]["faturamento_total"], 1000000010.32)


class FatiasMensaisTests(_LeiautesCase):
    COM_DATA = PERIODO_DE_DOIS_MESES + "Data;Produto;Qtde;Valor Total\n"

    def test_cada_mes_do_periodo_vira_um_relatorio_do_raio_x_do_lead(self):
        caminho, verdade = self.xlsx("detalhado")
        resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
        fatias = resultado["raio_x"]["fatias"]
        # 37 meses do calendário entre 09/11/2023 e 09/11/2026; 18 deles têm venda no relatório fictício.
        self.assertEqual(len(fatias), 37)
        self.assertEqual(resultado["meta"]["arquivos"], 37)
        self.assertEqual(sum(1 for fatia in fatias if fatia["faturamento"] > 0), 18)
        # O primeiro e o último mês são cortados pelo período do cabeçalho; o primeiro não tem venda.
        self.assertEqual((fatias[0]["rotulo"], fatias[0]["inicio"], fatias[0]["fim"], fatias[0]["faturamento"]),
                         ("11/2023", "2023-11-09", "2023-11-30", 0.0))
        self.assertEqual((fatias[2]["rotulo"], fatias[2]["inicio"], fatias[2]["fim"]), ("01/2024", "2024-01-01", "2024-01-31"))
        self.assertEqual((fatias[-1]["inicio"], fatias[-1]["fim"]), ("2026-11-01", "2026-11-09"))
        self.assertAlmostEqual(sum(fatia["faturamento"] for fatia in fatias), round(verdade["venda"], 2), places=2)
        self.assertEqual(resultado["raio_x"]["saude"]["faturamento_total"], round(verdade["venda"], 2))
        self.assertEqual(resultado["raio_x"]["periodo"], {"inicio": "2023-11-09", "fim": "2026-11-09", "meses": 37})
        # O período inteiro do cabeçalho é período coberto, sem buraco nos meses sem venda.
        self.assertEqual(resultado["meta"]["periodos_cobertos"], [["2023-11-09", "2026-11-09"]])
        self.assertEqual(resultado["meta"]["dias_por_ano"], {"2023": 53, "2024": 366, "2025": 365, "2026": 313})

    def test_raio_x_do_cliente_continua_com_uma_fatia_por_arquivo(self):
        caminho, _verdade = self.xlsx("detalhado")
        resultado = executar_como_o_worker([caminho])
        self.assertEqual(len(resultado["fatias"]), 1)
        self.assertEqual(len(resultado["arquivos"]), 1)
        self.assertEqual(resultado["periodo"], {"inicio": "2023-11-09", "fim": "2026-11-09", "meses": 37})

    def test_mensagens_usam_o_numero_do_arquivo_enviado_e_nao_o_da_fatia(self):
        # Dois arquivos de janeiro a março: são seis relatórios mensais, e a mensagem fala dos arquivos 1 e 2.
        primeiro = self.csv(self.SO_FEVEREIRO, "primeiro.csv")
        segundo = self.csv(self.SO_FEVEREIRO.replace("36,00", "38,00"), "segundo.csv")
        mensagem = self.recusa("avaliacao_sobreposicao", avaliar_como_o_worker, [primeiro, segundo], hoje=HOJE)
        self.assertIn("Os arquivos 1 e 2 cobrem", mensagem)

    def test_dois_arquivos_de_varios_anos_passam_do_teto_de_relatorios(self):
        primeiro, _verdade = self.xlsx("detalhado")
        segundo, _verdade = self.xlsx("detalhado")
        mensagem = self.recusa("raio_x_arquivos", avaliar_como_o_worker, [primeiro, segundo], hoje=HOJE)
        self.assertIn("mais de 60 meses", mensagem)

    def test_data_fora_do_periodo_do_cabecalho_e_recusada(self):
        def adiantar(linha):
            if (linha[0].year, linha[0].month) == (2026, 11):
                linha[0] = datetime(2026, 12, 1)
        caminho = self.detalhado_alterado(adiantar)
        mensagem = self.recusa("raio_x_datas", avaliar_como_o_worker, [caminho], hoje=HOJE)
        self.assertIn("Arquivo 1: há vendas com data fora do período do cabeçalho (09/11/2023 a 09/11/2026)", mensagem)

    def test_venda_sem_data_legivel_no_meio_das_datadas_e_recusada(self):
        def apagar(linha):
            if (linha[0].year, linha[0].month) == (2026, 11):
                linha[0] = "s/d"
        caminho = self.detalhado_alterado(apagar)
        mensagem = self.recusa("raio_x_datas", avaliar_como_o_worker, [caminho], hoje=HOJE)
        self.assertIn("Arquivo 1: o relatório cobre mais de um mês e há linhas de venda sem data legível", mensagem)

    def test_coluna_que_nao_traz_data_nenhuma_nao_fatia(self):
        caminho = self.detalhado_alterado(lambda linha: linha.update({0: "s/d"}))
        self.recusa("avaliacao_periodo_entre_anos", avaliar_como_o_worker, [caminho], hoje=HOJE)

    def test_resumo_por_produto_com_coluna_de_data_nao_fatia(self):
        """Uma linha por produto: a coluna "Data" é outra coisa (a última venda, o cadastro)."""
        caminho = self.csv(self.COM_DATA + "15/01/2026;PRODUTO A;1;10,00\n20/02/2026;PRODUTO B;2;20,00\n")
        resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
        self.assertEqual(resultado["meta"]["arquivos"], 1)
        self.assertEqual(resultado["meta"]["relatorios_mensais_por_ano"], {"2026": False})

    def test_venda_item_a_item_em_csv_com_data_fatia_por_mes(self):
        caminho = self.csv(self.COM_DATA + "15/01/2026;PRODUTO A;1;10,00\n20/02/2026;PRODUTO A;2;20,00\n"
                                           "21/02/26;PRODUTO B;1;5,00\n")
        resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
        self.assertEqual(resultado["meta"]["arquivos_por_ano"], {"2026": 2})
        self.assertEqual([fatia["faturamento"] for fatia in resultado["raio_x"]["fatias"]], [10.0, 25.0])
        meses = resultado["linhas"]["colunas"].index("meses")
        self.assertEqual(sorted(linha[meses] for linha in resultado["linhas"]["valores"]), [1, 2])

    SO_FEVEREIRO = ("Relatorio de vendas item a item\nPeriodo: 01/01/2026 a 31/03/2026\n"
                    "Data;Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque\n"
                    "05/02/2026;1;CAFE 500G;2;36,00;30,00;5\n15/02/2026;2;LEITE 1L;3;15,00;12,00;4\n"
                    "20/02/2026;1;CAFE 500G;1;18,00;15,00;4\n")

    def test_mes_sem_venda_dentro_do_periodo_continua_sendo_periodo_coberto(self):
        """O cabeçalho diz janeiro a março e só fevereiro vendeu: os três meses são relatórios (dois sem venda).
        Os 90 dias contam no ritmo, e outro arquivo de janeiro continua sendo sobreposição."""
        caminho = self.csv(self.SO_FEVEREIRO)
        resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
        self.assertEqual(resultado["meta"]["arquivos_por_ano"], {"2026": 3})
        self.assertEqual(resultado["meta"]["dias_por_ano"], {"2026": 90})
        self.assertEqual([(fatia["rotulo"], fatia["faturamento"], fatia["itens"]) for fatia in resultado["raio_x"]["fatias"]],
                         [("01/2026", 0.0, 0), ("02/2026", 69.0, 2), ("03/2026", 0.0, 0)])
        colunas = resultado["linhas"]["colunas"]
        desde, meses = colunas.index("relatorios_desde_primeira_venda"), colunas.index("meses")
        self.assertEqual({(linha[meses], linha[desde]) for linha in resultado["linhas"]["valores"]}, {(1, 2)})
        janeiro = self.csv("Relatorio de vendas por produto\nPeriodo: 01/01/2026 a 31/01/2026\n"
                           "Codigo;Produto;Qtde;Valor Total\n1;CAFE 500G;2;36,00\n", "janeiro.csv")
        self.assertIn("Os arquivos 1 e 2 cobrem",
                      self.recusa("avaliacao_sobreposicao", avaliar_como_o_worker, [caminho, janeiro], hoje=HOJE))

    def test_mes_sem_venda_que_ainda_nao_comecou_nao_vira_relatorio(self):
        resultado = avaliar_como_o_worker([self.csv(self.SO_FEVEREIRO)], hoje=date(2026, 2, 10))
        self.assertEqual([fatia["rotulo"] for fatia in resultado["raio_x"]["fatias"]], ["01/2026", "02/2026"])
        self.assertEqual(resultado["meta"]["dias_por_ano"], {"2026": 41})

    def test_data_inconsistente_num_arquivo_de_um_ano_so_nao_fatia_nem_recusa(self):
        """O arquivo de um ano só já era lido como um relatório: com data fora do período, ou com a data só
        em parte das linhas, continua assim (a recusa com o motivo fica para o que cruza o ano)."""
        fora = self.SO_FEVEREIRO + "05/06/2026;1;CAFE 500G;1;18,00;15,00;4\n"
        parcial = self.SO_FEVEREIRO + ";2;LEITE 1L;1;5,00;4,00;3\n"
        for nome, texto, total in (("fora.csv", fora, 87.0), ("parcial.csv", parcial, 74.0)):
            with self.subTest(arquivo=nome):
                resultado = avaliar_como_o_worker([self.csv(texto, nome)], hoje=HOJE)
                self.assertEqual(resultado["meta"]["arquivos"], 1)
                self.assertEqual(resultado["meta"]["relatorios_mensais_por_ano"], {"2026": False})
                self.assertEqual(resultado["raio_x"]["saude"]["faturamento_total"], total)

    def test_arquivo_sem_venda_valida_e_com_estoque_e_recusado_com_mensagem(self):
        """O saldo informado de um produto sem venda não cria linha na matriz do lead (derrubava o processo
        com erro genérico): o envio sem venda nenhuma é recusado com a mensagem de sempre."""
        cabecalho = "Data;Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque\n"
        linhas = "05/01/2026;1;CAFE 500G;0;0,00;0,00;5\n06/02/2026;1;CAFE 500G;0;0,00;0,00;5\n"
        fatiado = self.csv("Relatorio\nPeriodo: 01/01/2026 a 28/02/2026\n" + cabecalho + linhas, "fatiado.csv")
        mensal = self.csv("Relatorio\nPeriodo: 01/01/2026 a 31/01/2026\n" + cabecalho + linhas.split("\n")[0] + "\n",
                          "mensal.csv")
        for caminho in (fatiado, mensal):
            with self.subTest(arquivo=Path(caminho).name):
                self.assertIn("Nenhuma venda com quantidade e valor maiores que zero",
                              self.recusa("raio_x_sem_vendas", avaliar_como_o_worker, [caminho], hoje=HOJE))

    def test_mais_meses_que_o_teto_de_fatias_e_recusado(self):
        caminho, _verdade = self.xlsx("detalhado")
        original = raio_x.MAX_FATIAS
        self.addCleanup(setattr, raio_x, "MAX_FATIAS", original)
        raio_x.MAX_FATIAS = 5
        mensagem = self.recusa("raio_x_arquivos", avaliar_como_o_worker, [caminho], hoje=HOJE)
        self.assertIn("Envie no máximo 5 anos por vez", mensagem)

    def test_data_da_linha_aceita_a_data_da_planilha_e_o_texto_do_pdv(self):
        casos = {"2023-11-09 00:00:00": date(2023, 11, 9), "2026-11-09 16:37:38.447000": date(2026, 11, 9),
                 "09/11/23": date(2023, 11, 9), "09/11/2023": date(2023, 11, 9), "09/11/2023 10:15": date(2023, 11, 9),
                 "s/d": None, "45239": None, "": None, None: None, "31/02/2026": None, "09/11/2023x": None}
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(etl_processor.data_da_linha(texto), esperado)

    # --- Achados da revisão independente final e testes que o teste de mutação dela pediu -----------------
    CINCO_ANOS = ("Relatorio de vendas item a item\nPeriodo: 01/01/2021 a 31/12/2025\nData;Produto;Qtde;Valor Total\n"
                  "15/01/2021;PRODUTO A;1;10,00\n20/12/2025;PRODUTO A;2;20,00\n")

    def test_venda_item_a_item_sem_periodo_no_cabecalho_e_recusada_com_orientacao(self):
        caminho = self.csv("Data;Produto;Qtde;Valor Total\n15/01/2026;PRODUTO A;1;10,00\n20/02/2026;PRODUTO A;2;20,00\n")
        mensagem = self.recusa("avaliacao_sem_periodo", avaliar_como_o_worker, [caminho], hoje=HOJE)
        self.assertIn("Arquivo 1: o período não está no cabeçalho", mensagem)

    def test_data_antes_do_inicio_do_periodo_do_cabecalho_tambem_e_data_fora(self):
        """A venda datada de ANTES do período: recusada no arquivo que cruza o ano; no de um ano só o arquivo
        continua um relatório, com a venda dentro (fatiar deixaria a venda de fora, calada)."""
        def atrasar(linha):
            if (linha[0].year, linha[0].month) == (2024, 1):
                linha[0] = datetime(2023, 10, 1)
        mensagem = self.recusa("raio_x_datas", avaliar_como_o_worker, [self.detalhado_alterado(atrasar)], hoje=HOJE)
        self.assertIn("Arquivo 1: há vendas com data fora do período do cabeçalho (09/11/2023 a 09/11/2026)", mensagem)
        texto = (self.SO_FEVEREIRO.replace("01/01/2026 a 31/03/2026", "01/02/2026 a 31/03/2026")
                 + "20/01/2026;1;CAFE 500G;1;18,00;15,00;4\n")
        resultado = avaliar_como_o_worker([self.csv(texto, "antes.csv")], hoje=HOJE)
        self.assertEqual(resultado["meta"]["arquivos"], 1)
        self.assertEqual(resultado["raio_x"]["saude"]["faturamento_total"], 87.0)

    def test_mes_sem_venda_que_comeca_hoje_ja_e_periodo_coberto(self):
        resultado = avaliar_como_o_worker([self.csv(self.SO_FEVEREIRO)], hoje=date(2026, 3, 1))
        self.assertEqual([fatia["rotulo"] for fatia in resultado["raio_x"]["fatias"]], ["01/2026", "02/2026", "03/2026"])
        self.assertEqual(resultado["meta"]["dias_por_ano"], {"2026": 60})

    def test_venda_em_mes_que_ainda_nao_comecou_nao_fatia_o_arquivo_de_um_ano_so(self):
        """Achado da revisão final: uma linha datada de mês futuro (pedido programado, data digitada errada)
        fazia recusar o arquivo inteiro, com a mensagem apontando o cabeçalho, que estava certo. O arquivo de
        um ano só continua um relatório, como era lido antes das fatias, com a venda dentro."""
        resultado = avaliar_como_o_worker([self.csv(self.SO_FEVEREIRO)], hoje=date(2026, 1, 20))
        self.assertEqual(resultado["meta"]["arquivos"], 1)
        self.assertEqual(resultado["meta"]["relatorios_mensais_por_ano"], {"2026": False})
        self.assertEqual(resultado["raio_x"]["saude"]["faturamento_total"], 69.0)

    def test_venda_em_mes_que_ainda_nao_comecou_e_recusada_no_arquivo_que_cruza_o_ano(self):
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/11/2025 a 31/03/2026\nData;Produto;Qtde;Valor Total\n"
                 "10/11/2025;PRODUTO A;1;10,00\n15/02/2026;PRODUTO A;2;20,00\n")
        caminho = self.csv(texto)
        mensagem = self.recusa("raio_x_datas", avaliar_como_o_worker, [caminho], hoje=date(2026, 1, 20))
        self.assertIn("Arquivo 1: há vendas com data em um mês que ainda não começou (hoje é 20/01/2026)", mensagem)
        # No primeiro dia do mês da venda, o mês já começou: o arquivo é fatiado até ele (março fica de fora).
        resultado = avaliar_como_o_worker([caminho], hoje=date(2026, 2, 1))
        self.assertEqual(resultado["meta"]["arquivos_por_ano"], {"2025": 2, "2026": 2})
        self.assertEqual(resultado["raio_x"]["saude"]["faturamento_total"], 30.0)

    def test_recusa_do_segundo_arquivo_fala_do_arquivo_2_e_nao_da_fatia(self):
        """O primeiro arquivo vira três relatórios mensais; a recusa do segundo, sem o período no cabeçalho,
        fala do arquivo 2, e não do 4."""
        segundo = self.csv("Codigo;Produto;Qtde;Valor Total\n1;CAFE 500G;2;36,00\n", "segundo.csv")
        mensagem = self.recusa("avaliacao_sem_periodo", avaliar_como_o_worker,
                               [self.csv(self.SO_FEVEREIRO, "primeiro.csv"), segundo], hoje=HOJE)
        self.assertTrue(mensagem.startswith("Arquivo 2: o período não está no cabeçalho"), mensagem)

    def test_cabecalho_de_loja_no_segundo_arquivo_fala_do_arquivo_2(self):
        """O primeiro arquivo vira três relatórios mensais; a recusa do segundo fala do arquivo 2, e não do 4."""
        segundo = self.csv("Relatorio de vendas por loja\nPeriodo: 01/04/2026 a 30/04/2026\n"
                           "Cod. Loja;Produto;Qtde;Preco Unitario;Valor Total;Total Custo\n"
                           "10;Cafe 500g;10;20,00;200,00;150,00\n", "segundo.csv")
        mensagem = self.recusa("avaliacao_cabecalho_loja", avaliar_como_o_worker,
                               [self.csv(self.SO_FEVEREIRO, "primeiro.csv"), segundo], hoje=HOJE)
        self.assertTrue(mensagem.startswith("Arquivo 2: a coluna lida como código"), mensagem)

    def test_contadores_do_arquivo_entram_uma_vez_so_nas_fatias(self):
        """A linha cancelada do arquivo conta uma vez na meta, e não uma vez por mês fatiado."""
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/01/2026 a 31/03/2026\n"
                 "Data;Produto;Qtde;Valor Total;Situacao\n"
                 "05/01/2026;CAFE 500G;2;36,00;Finalizada\n15/02/2026;CAFE 500G;1;18,00;Finalizada\n"
                 "20/02/2026;LEITE 1L;3;15,00;Cancelada\n10/03/2026;CAFE 500G;1;18,00;Finalizada\n")
        meta = avaliar_como_o_worker([self.csv(texto)], hoje=HOJE)["meta"]
        self.assertEqual(meta["arquivos"], 3)
        self.assertEqual((meta["situacao"]["linhas_canceladas"], meta["situacao"]["valor_cancelado"]), (1, 15.0))

    def test_aviso_do_arquivo_fatiado_aparece_uma_vez_so(self):
        """O arquivo fatiado em três meses não traz a coluna de custo: um aviso só, do arquivo 1."""
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/01/2026 a 31/03/2026\nData;Produto;Qtde;Valor Total\n"
                 "05/01/2026;CAFE 500G;2;36,00\n15/02/2026;CAFE 500G;1;18,00\n10/03/2026;CAFE 500G;1;18,00\n")
        avisos = avaliar_como_o_worker([self.csv(texto)], hoje=HOJE)["raio_x"]["avisos"]
        self.assertEqual([aviso for aviso in avisos if "coluna de custo" in aviso],
                         ["O arquivo 1 não traz a coluna de custo: as vendas dele contam como sem custo informado."])

    def test_sessenta_meses_exatos_cabem_no_teto(self):
        """O teto é de 60 relatórios mensais (5 anos): o arquivo de 01/2021 a 12/2025 é aceito inteiro."""
        resultado = avaliar_como_o_worker([self.csv(self.CINCO_ANOS)], hoje=HOJE)
        self.assertEqual(resultado["meta"]["arquivos"], 60)
        self.assertEqual(resultado["meta"]["arquivos_por_ano"], {str(ano): 12 for ano in range(2021, 2026)})
        self.assertEqual(resultado["raio_x"]["saude"]["faturamento_total"], 30.0)

    def test_um_arquivo_a_mais_depois_de_sessenta_meses_passa_do_teto(self):
        """60 meses fatiados e mais um relatório mensal: 61. A recusa é a do teto, com a mensagem dele, e não
        a do contrato do resultado (erro genérico do processamento)."""
        janeiro = self.csv(PERIODO_DE_JANEIRO + "Produto;Qtde;Valor Total\nPRODUTO A;1;10,00\n", "janeiro.csv")
        mensagem = self.recusa("raio_x_arquivos", avaliar_como_o_worker, [self.csv(self.CINCO_ANOS), janeiro], hoje=HOJE)
        self.assertIn("mais de 60 meses", mensagem)

    def test_periodo_absurdo_e_recusado_antes_de_montar_as_fatias(self):
        """O teto vale DENTRO de _fatias_mensais, antes de montar uma fatia por mês: o cabeçalho com mais de
        um século é recusado na hora. Sem o teto ali, a recusa de _ler_envio só viria depois de montadas
        milhares de fatias (na medição da revisão: 12 mil fatias, 29 s e 72 MiB para um período desde o ano 1000)."""
        caminho = self.csv("Relatorio de vendas item a item\nPeriodo: 01/01/1900 a 09/11/2026\n"
                           "Data;Produto;Qtde;Valor Total\n15/01/2026;PRODUTO A;1;10,00\n20/02/2026;PRODUTO A;2;20,00\n")
        df, _formato, periodo, rotulo = raio_x._ler_vendas(caminho, 0, True)
        with self.assertRaises(ETLInputError) as contexto:
            raio_x._fatias_mensais(df, periodo, rotulo, HOJE)
        self.assertEqual(contexto.exception.code, "raio_x_arquivos")
        self.assertIn("mais de 60 meses", self.recusa("raio_x_arquivos", avaliar_como_o_worker, [caminho], hoje=HOJE))

    def test_coluna_de_outra_data_nao_e_a_data_da_venda(self):
        """Só o título inteiro de data da venda vale ("Data", "Dt. Emissão"): a validade, o cadastro e a
        última compra são outras datas, e o arquivo de dois meses continua um relatório só."""
        for numero, titulo in enumerate(("Data Validade", "Data Cadastro", "Dt. Ultima Compra")):
            with self.subTest(titulo=titulo):
                caminho = self.csv(PERIODO_DE_DOIS_MESES + f"{titulo};Produto;Qtde;Valor Total\n"
                                   "15/01/2026;PRODUTO A;1;10,00\n20/02/2026;PRODUTO A;2;20,00\n", f"outra_data_{numero}.csv")
                resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
                self.assertEqual(resultado["meta"]["arquivos"], 1)
                self.assertEqual(resultado["meta"]["relatorios_mensais_por_ano"], {"2026": False})

    def test_titulos_de_data_da_venda_com_complemento_tambem_fatiam(self):
        """Os títulos que o comentário do motor promete ("Data venda", "Dt. Emissão") e os outros da expressão:
        todos são a data da venda, e o arquivo de dois meses vira dois relatórios. "Data Movimento" nunca
        chegava a ser conferida (era descartada antes, como coluna de situação), e "Data Faturamento" ficava
        com o valor da venda, que saía zerado (achados da revisão final)."""
        for numero, titulo in enumerate(("Data Venda", "Data da Venda", "Dt. Emissao", "Emissao", "Data do Pedido",
                                         "Dt. Doc", "Data NF", "Data da Nota", "Data do Cupom", "Data Lancamento",
                                         "Data Movimento", "Dt. Movimento", "Data do Movimento", "Data Faturamento",
                                         "Dt. Faturamento")):
            with self.subTest(titulo=titulo):
                caminho = self.csv(PERIODO_DE_DOIS_MESES + f"{titulo};Produto;Qtde;Valor Total\n"
                                   "15/01/2026;PRODUTO A;1;10,00\n20/02/2026;PRODUTO A;2;20,00\n", f"data_{numero}.csv")
                resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
                self.assertEqual(resultado["meta"]["arquivos_por_ano"], {"2026": 2})
                self.assertEqual([fatia["faturamento"] for fatia in resultado["raio_x"]["fatias"]], [10.0, 20.0])

    def test_com_duas_colunas_de_data_vale_a_primeira(self):
        caminho = self.csv(PERIODO_DE_DOIS_MESES + "Data;Dt. Emissao;Produto;Qtde;Valor Total\n"
                           "15/01/2026;20/02/2026;PRODUTO A;1;10,00\n20/02/2026;15/01/2026;PRODUTO A;2;20,00\n")
        resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
        self.assertEqual([fatia["faturamento"] for fatia in resultado["raio_x"]["fatias"]], [10.0, 20.0])

    def test_resumo_com_nome_repetido_por_acaso_nao_e_venda_item_a_item(self):
        """Achado da revisão final: bastava um nome repetido (dois códigos com o mesmo nome, ou a linha de
        devolução do produto) para o resumo por produto com coluna "Data" ser fatiado pela data errada. Venda
        item a item é o arquivo em que a maior parte das linhas de venda é de produto repetido."""
        cabecalho = "Data;Codigo;Produto;Qtde;Valor Total;Situacao\n"
        dois_codigos = ("10/01/2026;1;CAFE 500G;10;180,00;\n20/02/2026;2;CAFE 500G;5;90,00;\n"
                        "12/01/2026;3;LEITE 1L;3;15,00;\n15/01/2026;4;ACUCAR 1KG;2;10,00;\n18/02/2026;5;ARROZ 5KG;1;25,00;\n")
        com_devolucao = ("10/01/2026;1;CAFE 500G;10;180,00;\n20/02/2026;1;CAFE 500G;1;18,00;Devolucao\n"
                         "12/01/2026;3;LEITE 1L;3;15,00;\n")
        for nome, linhas, total in (("dois_codigos.csv", dois_codigos, 320.0), ("devolucao.csv", com_devolucao, 195.0)):
            with self.subTest(arquivo=nome):
                resultado = avaliar_como_o_worker([self.csv(PERIODO_DE_DOIS_MESES + cabecalho + linhas, nome)], hoje=HOJE)
                self.assertEqual(resultado["meta"]["arquivos"], 1)
                self.assertEqual(resultado["meta"]["relatorios_mensais_por_ano"], {"2026": False})
                self.assertEqual(resultado["raio_x"]["saude"]["faturamento_total"], total)

    def test_um_quinto_das_linhas_de_produto_repetido_ja_e_venda_item_a_item(self):
        """Achado da verificação das correções: com a regra da metade das linhas, a venda item a item de
        catálogo largo (autopeças, móveis sob encomenda) deixava de ser fatiada, e a que cruza o ano voltava a
        ser recusada. Basta um quinto das linhas de venda de produto repetido."""
        um_quinto = ("05/01/2026;PRODUTO A;1;10,00\n06/02/2026;PRODUTO A;1;10,00\n"
                     + "".join(f"{7 + n:02d}/0{1 + n % 2}/2026;PRODUTO {letra};1;5,00\n" for n, letra in enumerate("BCDEFGHI")))
        menos = um_quinto + "20/02/2026;PRODUTO J;1;5,00\n"
        fatiado = avaliar_como_o_worker([self.csv(self.COM_DATA + um_quinto, "um_quinto.csv")], hoje=HOJE)
        self.assertEqual([fatia["faturamento"] for fatia in fatiado["raio_x"]["fatias"]], [30.0, 30.0])
        inteiro = avaliar_como_o_worker([self.csv(self.COM_DATA + menos, "menos.csv")], hoje=HOJE)
        self.assertEqual((inteiro["meta"]["arquivos"], inteiro["raio_x"]["saude"]["faturamento_total"]), (1, 65.0))

    def test_resumo_com_uma_linha_por_codigo_e_o_mesmo_nome_nao_e_venda_item_a_item(self):
        """Achado da verificação das correções: a grade de tamanhos de uma confecção tem uma linha por código e
        a mesma descrição em todos os tamanhos. O nome se repete em todas as linhas e o código, em nenhuma: é
        resumo, e a coluna "Data" dele é a da última venda."""
        linhas = "".join(f"{10 + n:02d}/0{1 + n % 2}/2026;{100 + n};CAMISETA FICTICIA {'LISA' if n < 4 else 'LISTRADA'};"
                         f"{'PMGX'[n % 4]};{n + 1};{n + 1}0,00\n" for n in range(8))
        caminho = self.csv(PERIODO_DE_DOIS_MESES + "Data;Codigo;Produto;Tam;Qtde;Valor Total\n" + linhas)
        resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
        self.assertEqual((resultado["meta"]["arquivos"], resultado["raio_x"]["saude"]["faturamento_total"]), (1, 360.0))
        self.assertEqual(resultado["meta"]["relatorios_mensais_por_ano"], {"2026": False})

    def test_codigo_que_e_o_da_venda_nao_entra_na_conta_da_repeticao(self):
        """A coluna lida como código pode ser a do pedido ("Cód. Venda" à esquerda do produto). Cada código tem
        vários produtos: ele não é o código do produto, e a repetição é contada pelo nome."""
        linhas = ("05/01/2026;9001;CAFE 500G;1;18,00\n05/01/2026;9001;LEITE 1L;2;10,00\n"
                  "06/02/2026;9002;CAFE 500G;1;18,00\n06/02/2026;9002;ACUCAR 1KG;1;5,00\n")
        caminho = self.csv(PERIODO_DE_DOIS_MESES + "Data;Cod. Venda;Produto;Qtde;Valor Total\n" + linhas)
        resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
        self.assertEqual([fatia["faturamento"] for fatia in resultado["raio_x"]["fatias"]], [28.0, 23.0])

    def test_codigo_do_produto_e_o_que_tem_um_nome_so_em_mais_de_quatro_quintos_dos_codigos(self):
        # Uma linha por código, todas com o mesmo nome, e um código com dois nomes. Com seis códigos (um em
        # seis com dois nomes) a coluna é o código do produto e nada se repete; com cinco (um em cinco), não
        # é, e o nome repetido faz do arquivo venda item a item.
        def arquivo(codigos):
            linhas = "10/01/2026;1;CAMISETA FICTICIA;1;10,00\n11/02/2026;1;CAMISETA FICTICIA NOVA;1;10,00\n"
            linhas += "".join(f"{12 + n:02d}/0{1 + n % 2}/2026;{n};CAMISETA FICTICIA;1;10,00\n" for n in range(2, codigos + 1))
            return self.csv(PERIODO_DE_DOIS_MESES + "Data;Codigo;Produto;Qtde;Valor Total\n" + linhas, f"codigos_{codigos}.csv")

        self.assertEqual(avaliar_como_o_worker([arquivo(6)], hoje=HOJE)["meta"]["arquivos"], 1)
        self.assertEqual(avaliar_como_o_worker([arquivo(5)], hoje=HOJE)["meta"]["arquivos"], 2)

    def test_repeticao_do_produto_e_contada_por_loja(self):
        """Uma linha por produto e loja é resumo, e não venda item a item: a coluna "Data" dele é outra coisa.
        O mesmo produto repetido na mesma loja é venda item a item, e o arquivo é fatiado."""
        cabecalho = PERIODO_DE_DOIS_MESES + "Data;Loja;Produto;Qtde;Valor Total\n"
        resumo = ("28/02/2026;Loja 1;CAFE 500G;10;180,00\n27/02/2026;Loja 2;CAFE 500G;5;90,00\n"
                  "26/01/2026;Loja 1;LEITE 1L;3;15,00\n25/02/2026;Loja 2;LEITE 1L;2;10,00\n")
        item_a_item = ("05/01/2026;Loja 1;CAFE 500G;1;18,00\n06/02/2026;Loja 1;CAFE 500G;2;36,00\n"
                       "07/01/2026;Loja 2;CAFE 500G;1;18,00\n08/02/2026;Loja 2;CAFE 500G;1;18,00\n")
        inteiro = avaliar_como_o_worker([self.csv(cabecalho + resumo, "resumo_por_loja.csv")], hoje=HOJE)
        self.assertEqual((inteiro["meta"]["arquivos"], inteiro["raio_x"]["saude"]["faturamento_total"]), (1, 295.0))
        fatiado = avaliar_como_o_worker([self.csv(cabecalho + item_a_item, "item_por_loja.csv")], hoje=HOJE)
        self.assertEqual([fatia["faturamento"] for fatia in fatiado["raio_x"]["fatias"]], [36.0, 54.0])

    def test_coluna_de_situacao_da_loja_nao_e_a_coluna_da_loja(self):
        # "Situação Loja" é coluna de situação, e não a loja: dois valores nela não são duas lojas.
        texto = (PERIODO_DE_JANEIRO + "Codigo;Produto;Qtde;Valor Total;Situacao Loja\n1;CAFE;2;20,00;Aberta\n"
                 "2;LEITE;1;10,00;Fechada\n")
        meta = avaliar_como_o_worker([self.csv(texto)], hoje=HOJE)["meta"]
        self.assertEqual(meta["relatorios_varias_unidades"], 0)
        com_loja = texto.replace("Situacao Loja", "Loja")
        self.assertEqual(avaliar_como_o_worker([self.csv(com_loja, "com_loja.csv")], hoje=HOJE)["meta"]
                         ["relatorios_varias_unidades"], 1)


class FormatosTests(_LeiautesCase):
    """O .xls do Excel 97-2003 e a Planilha XML 2003, lidos direto pelo motor, sem conversão."""

    def formatos(self, nome, **opcoes):
        linhas, mesclas, formatos, verdade = leiautes.RELATORIOS[nome](**opcoes)
        return {"xlsx": leiautes.gravar_xlsx(linhas, mesclas, formatos, self.pasta / f"{nome}.xlsx"),
                "xls": leiautes.gravar_xls(linhas, mesclas, formatos, self.pasta / f"{nome}.xls"),
                "xml": leiautes.gravar_planilha_xml(linhas, mesclas, self.pasta / f"{nome}.xml")}, verdade

    def gravar(self, nome, conteudo):
        caminho = self.pasta / nome
        caminho.write_bytes(conteudo)
        return str(caminho)

    def test_o_mesmo_relatorio_da_a_mesma_matriz_nos_tres_formatos_de_planilha(self):
        for nome in leiautes.RELATORIOS:
            caminhos, _verdade = self.formatos(nome)
            base = read_raw(caminhos["xlsx"], get_limits())
            for extensao in ("xls", "xml"):
                with self.subTest(relatorio=nome, formato=extensao):
                    lida = read_raw(caminhos[extensao], get_limits())
                    self.assertTrue(security_gatekeeper._mesma_matriz(lida, base))
                    self.assertEqual(lida.attrs["format"], extensao)

    def test_raio_x_e_avaliacao_de_cenario_dao_o_mesmo_resultado_nos_tres_formatos(self):
        caminhos, verdade = self.formatos("detalhado")
        esperado = None
        for extensao, caminho in caminhos.items():
            with self.subTest(formato=extensao):
                raio = executar_como_o_worker([caminho])
                avaliacao = avaliar_como_o_worker([caminho], hoje=HOJE)
                resumo = (raio["saude"], raio["fatias"], avaliacao["linhas"], avaliacao["raio_x"]["fatias"],
                          avaliacao["meta"]["arquivos_por_ano"])
                esperado = esperado or resumo
                self.assertEqual(resumo, esperado)
                self.assertEqual(raio["saude"]["faturamento_total"], round(verdade["venda"], 2))
                self.assertEqual(avaliacao["meta"]["arquivos"], 37)

    def test_relatorio_por_produto_em_xls_e_lido_como_o_erp_exporta(self):
        caminhos, verdade = self.formatos("por_produto", generico=True)
        resultado = executar_como_o_worker([caminhos["xls"]])
        self.assertEqual(resultado["saude"]["faturamento_total"], verdade["venda"])
        self.assertEqual(resultado["saude"]["itens_vendidos"], verdade["produtos"])
        self.assertEqual(resultado["auditoria"]["custo_total"], verdade["custo"])
        self.assertEqual(resultado["auditoria"]["custo_a_confirmar"]["itens"], 1)
        vendas = etl_processor.carregar_vendas(caminhos["xls"])
        self.assertEqual(vendas["codigo"].iloc[0], "0700019")  # o código é texto e mantém o zero à esquerda

    def test_xls_e_planilha_xml_contam_as_celulas_com_valor_e_nao_a_area(self):
        caminhos, _verdade = self.formatos("detalhado")
        preenchidas = sum(1 for linha in leiautes.relatorio_detalhado()[0] for valor in linha.values()
                          if valor is not None)
        for extensao in ("xls", "xml"):
            with self.subTest(formato=extensao):
                # Com folga na área (4 vezes o orçamento), valem só as células com valor.
                politica = replace(get_limits(), max_cells_per_sheet=300, max_cells_per_file=300)
                with using_limits(politica):
                    meta = validate_input(caminhos[extensao], "." + extensao, politica)
                self.assertEqual(meta["filled_cells_selected"], preenchidas)
                self.assertGreater(meta["rows"] * meta["columns"], 300)
                justa = replace(politica, max_cells_per_sheet=preenchidas - 1)
                with using_limits(justa):
                    self.recusa("cells", validate_input, caminhos[extensao], "." + extensao, justa)
                # A matriz é retangular: a área também tem teto, de 4 vezes o orçamento de células.
                apertada = replace(politica, max_cells_per_sheet=preenchidas + 5)
                with using_limits(apertada):
                    mensagem = self.recusa("cells", validate_input, caminhos[extensao], "." + extensao, apertada)
                self.assertIn("área de células", mensagem)

    def test_area_acima_do_teto_e_recusada_antes_de_a_matriz_ser_montada(self):
        """Duas células distantes num arquivo de poucos bytes: a área passa do teto (4 vezes o orçamento de
        células) e as células com valor, não. Quem recusa é o leitor, antes de a matriz retangular existir,
        também na leitura do motor (read_raw), e não só na validação."""
        from tests import planilha_xml_support as px
        from tests import xls_support
        limites = get_limits()
        pasta = xls_support.Pasta()
        pasta.aba("Report").por(0, 0, "a").por(min(65_535, limites.max_rows - 1), limites.max_columns - 1, "b")
        distante = px.linha(px.celula("b", indice=limites.max_columns), indice=limites.max_rows)
        esparsos = {"xls": self.gravar("esparso.xls", bytes(pasta.arquivo())),
                    "xml": px.gravar(self.pasta / "esparso.xml", px.tabela(px.linha(px.celula("a")), distante))}
        with mock.patch.object(etl_biff._Celulas, "matriz") as do_xls, \
                mock.patch.object(etl_planilha_xml, "_matriz") as do_xml:
            for extensao, caminho in esparsos.items():
                with self.subTest(formato=extensao):
                    self.assertLess(Path(caminho).stat().st_size, 10_000)
                    self.assertIn("área de células", self.recusa("cells", read_raw, caminho, limites))
                    with using_limits(limites):
                        mensagem = self.recusa("cells", validate_input, caminho, "." + extensao, limites)
                    self.assertIn("área de células", mensagem)
        do_xls.assert_not_called()
        do_xml.assert_not_called()

    def test_macro_e_objeto_embutido_do_xls_nao_sao_abertos(self):
        linhas, mesclas, formatos, verdade = leiautes.relatorio_por_produto()
        limpo = leiautes.gravar_xls(linhas, mesclas, formatos, self.pasta / "limpo.xls")
        marca = b"Attribute VB_Name = \"MacroFicticia\"\r\nSub Auto_Open()\r\nEnd Sub\r\n"
        com_macro = leiautes.gravar_xls(linhas, mesclas, formatos, self.pasta / "com_macro.xls", outros=[
            ("_VBA_PROJECT_CUR/ThisWorkbook", marca * 200), ("MBD0018D2F8/Ole10Native", b"MZ" + bytes(6000))])
        self.assertGreater(Path(com_macro).stat().st_size, Path(limpo).stat().st_size + 10_000)
        self.assertEqual(security_gatekeeper.inspecionar_arquivo(com_macro, "xls", leitura_direta=True), "xls")
        lida = read_raw(com_macro, get_limits())
        self.assertTrue(security_gatekeeper._mesma_matriz(lida, read_raw(limpo, get_limits())))
        self.assertNotIn("MacroFicticia", lida.to_string())
        self.assertEqual(executar_como_o_worker([com_macro])["saude"]["faturamento_total"], verdade["venda"])

    def test_formula_do_xls_entra_pelo_valor_guardado_e_o_texto_ativo_e_neutralizado(self):
        from tests import xls_support
        pasta = xls_support.Pasta()
        folha = pasta.aba("Report")
        folha.por(0, 0, "Relatorio de vendas por produto").por(1, 0, "Periodo: 01/01/2026 a 31/01/2026")
        for coluna, titulo in enumerate(("Produto", "Qtde", "Valor Total")):
            folha.por(2, coluna, titulo)
        folha.por(3, 0, "PRODUTO A").por(3, 1, 2).bruto(xls_support.formula(3, 2, 21.0))
        folha.bruto(xls_support.formula(4, 0, "PRODUTO B")).por(4, 1, 1).por(4, 2, 10.5)
        folha.por(5, 0, "=cmd|' /C calc'!A0").por(5, 1, 1).por(5, 2, 1.0)
        caminho = self.gravar("formulas.xls", bytes(pasta.arquivo()))
        lida = read_raw(caminho, get_limits())
        self.assertEqual(lida.iloc[3].tolist(), ["PRODUTO A", "2", "21"])
        self.assertEqual(lida.iloc[4].tolist(), ["PRODUTO B", "1", "10.5"])
        self.assertEqual(lida.iat[5, 0], "'=cmd|' /C calc'!A0")
        self.assertEqual(lida.attrs["celulas_neutralizadas"], 1)
        self.assertEqual(executar_como_o_worker([caminho])["saude"]["faturamento_total"], 32.5)

    def test_xls_com_senha_html_disfarcado_e_conteiner_quebrado_sao_recusados(self):
        from tests import xls_support
        pasta = xls_support.Pasta()
        pasta.aba("Report").por(0, 0, "Produto").por(0, 1, "Qtde").por(1, 0, "PRODUTO A").por(1, 1, 1)
        cifrado = self.gravar("senha.xls", bytes(pasta.arquivo(
            antes_de_tudo=xls_support.registro(xls_support.FILEPASS, bytes(6)))))
        self.assertIn("senha", self.recusa("unsupported", validate_input, cifrado, ".xls", get_limits()))
        html = self.gravar("pagina.xls", b"<html><body><table><tr><td>Produto</td><td>Qtde</td></tr></table></body></html>")
        self.assertIn("não é uma planilha do Excel 97-2003",
                      self.recusa("unsupported", validate_input, html, ".xls", get_limits()))
        # A Baia recusa antes do leitor, e também diz o que fazer (achado da revisão final: a recusa vinha só
        # com a mensagem padrão, e a página HTML com extensão .xls é comum em sistemas que "exportam para Excel").
        orientacao = security_gatekeeper.ORIENTACAO_XLS_ILEGIVEL
        self.assertIn(orientacao, self.recusa("quarentena:marcacao", security_gatekeeper.inspecionar_arquivo, html, "xls",
                                              leitura_direta=True))
        quebrado = self.gravar("quebrado.xls", leiautes.XLS_MINIMO)
        self.recusa("invalid", validate_input, quebrado, ".xls", get_limits())
        self.assertIn(orientacao, self.recusa("quarentena:documento_ole", security_gatekeeper.inspecionar_arquivo, quebrado,
                                              "xls", leitura_direta=True))
        self.recusa("raio_x_dados", executar_como_o_worker,
                    [self.formatos("por_forma_de_pagamento")[0]["xls"]])

    def test_xml_que_nao_e_planilha_ou_traz_doctype_e_recusado(self):
        espaco = 'xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
        casos = {
            "doctype.xml": (f'<?xml version="1.0"?>\n<!DOCTYPE Workbook [<!ENTITY a "aaaaaaaaaa">]>\n<Workbook {espaco}/>',
                            "invalid", "DOCTYPE"),
            "pagina.xml": ("<html><body><table><tr><td>Produto</td></tr></table></body></html>", "unsupported", "HTML"),
            "nota.xml": ('<?xml version="1.0"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"/>', "unsupported",
                         "documento fiscal"),
        }
        for nome, (texto, codigo, trecho) in casos.items():
            with self.subTest(arquivo=nome):
                caminho = self.gravar(nome, texto.encode("utf-8"))
                self.assertIn(trecho, self.recusa(codigo, validate_input, caminho, ".xml", get_limits()))
                self.recusa("quarentena:xml_nao_e_planilha", security_gatekeeper.inspecionar_arquivo, caminho, "xml",
                            leitura_direta=True)

    def test_baia_so_libera_xls_e_planilha_xml_na_leitura_direta(self):
        caminhos, _verdade = self.formatos("por_produto")
        inspecionar = security_gatekeeper.inspecionar_arquivo
        self.assertEqual(inspecionar(caminhos["xls"], "xls", leitura_direta=True), "xls")
        self.assertEqual(inspecionar(caminhos["xml"], "xml", leitura_direta=True), "xml")
        self.assertEqual(inspecionar(caminhos["xlsx"], "xlsx", leitura_direta=True), "xlsx")
        # Arquivo guardado (o envio das análises mensais): só .csv e .xlsx, como antes.
        self.assertIn(security_gatekeeper.ORIENTACAO_XLS, self.recusa("quarentena:xls_binario", inspecionar,
                                                                     caminhos["xls"], "xls"))
        self.recusa("quarentena:extensao_nao_aceita", inspecionar, caminhos["xml"], "xml")
        self.recusa("quarentena:formato_de_leitura", security_gatekeeper.triar, caminhos["xls"], "xls", get_limits())
        # A extensão continua tendo de bater com o conteúdo.
        trocas = (("xls", "csv", "xls_disfarcado"), ("xls", "xml", "xml_nao_e_planilha"),
                  ("xlsx", "xls", "disfarce_zip_como_xls"), ("xml", "xls", "marcacao"), ("xml", "csv", "marcacao"))
        for origem, extensao, motivo in trocas:
            with self.subTest(conteudo=origem, extensao=extensao):
                disfarce = self.gravar(f"{origem}_como.{extensao}", Path(caminhos[origem]).read_bytes())
                self.recusa("quarentena:" + motivo, inspecionar, disfarce, extensao, leitura_direta=True)

    def test_nul_antes_da_formula_no_xls_nao_escapa_da_neutralizacao(self):
        """Achado da revisão final: o .xls guarda o NUL (U+0000), que o leitor de CSV descarta. Com um NUL na
        frente, a fórmula não era neutralizada, e o motor, que tira o NUL mais adiante, devolvia o nome do
        produto começando por "="."""
        from tests import xls_support
        pasta = xls_support.Pasta()
        folha = pasta.aba("Report")
        folha.por(0, 0, "Relatorio de vendas por produto").por(1, 0, "Periodo: 01/01/2026 a 31/01/2026")
        for coluna, titulo in enumerate(("Codigo", "Produto", "Qtde", "Valor Total")):
            folha.por(2, coluna, titulo)
        folha.por(3, 0, "\x00=1+1").por(3, 1, "\x00=cmd|' /C calc'!A0").por(3, 2, 2).por(3, 3, 21.0)
        folha.por(4, 0, "2").por(4, 1, "PRODUTO\x00 B").por(4, 2, 1).por(4, 3, 10.5)
        caminho = self.gravar("formula_com_nulo.xls", bytes(pasta.arquivo()))  # "nul.xls" é dispositivo no Windows
        lida = read_raw(caminho, get_limits())
        self.assertEqual(lida.iloc[3].tolist()[:2], ["'=1+1", "'=cmd|' /C calc'!A0"])
        self.assertEqual((lida.iat[4, 1], lida.attrs["celulas_neutralizadas"]), ("PRODUTO B", 2))
        resultados = json.dumps([executar_como_o_worker([caminho]), avaliar_como_o_worker([caminho], hoje=HOJE)["linhas"]],
                                ensure_ascii=False)
        self.assertIn("'=cmd|' /C calc'!A0", resultados)
        self.assertNotIn('"=', resultados)  # nenhum texto do resultado começa por "="

    # --- Testes que o teste de mutação da revisão independente final pediu ---------------------------------
    def tres_formatos(self, nome, linhas):
        return {"xlsx": leiautes.gravar_xlsx(linhas, [], {}, self.pasta / f"{nome}.xlsx"),
                "xls": leiautes.gravar_xls(linhas, [], {}, self.pasta / f"{nome}.xls"),
                "xml": leiautes.gravar_planilha_xml(linhas, [], self.pasta / f"{nome}.xml")}

    def densa(self, nome, linhas=10, colunas=10, abas=1):
        """A mesma planilha cheia (linhas x colunas, em cada aba) em .xls e em Planilha XML."""
        from tests import planilha_xml_support as px
        from tests import xls_support
        pasta = xls_support.Pasta()
        for numero in range(abas):
            folha = pasta.aba(f"Plan{numero + 1}")
            for linha in range(linhas):
                for coluna in range(colunas):
                    folha.por(linha, coluna, f"v{numero}{linha}{coluna}")
        folhas = [px.aba(*(px.linha(*(px.celula(f"v{numero}{linha}{coluna}") for coluna in range(colunas)))
                           for linha in range(linhas)), nome=f"Plan{numero + 1}") for numero in range(abas)]
        return {"xls": self.gravar(f"{nome}.xls", bytes(pasta.arquivo())),
                "xml": px.gravar(self.pasta / f"{nome}.xml", px.livro(*folhas))}

    def test_booleano_do_xls_e_da_planilha_xml_sai_como_no_xlsx(self):
        caminhos = self.tres_formatos("booleano", [{0: "Produto", 1: "Ativo"}, {0: "CAFE", 1: True}, {0: "LEITE", 1: False}])
        base = read_raw(caminhos["xlsx"], get_limits())
        self.assertEqual(base[1].tolist(), ["Ativo", "True", "False"])
        for extensao in ("xls", "xml"):
            with self.subTest(formato=extensao):
                lida = read_raw(caminhos[extensao], get_limits())
                self.assertEqual(lida[1].tolist(), ["Ativo", "True", "False"])
                self.assertTrue(security_gatekeeper._mesma_matriz(lida, base))

    def test_texto_de_ausente_do_xls_e_da_planilha_xml_sai_vazio_como_no_xlsx(self):
        import pandas as pd
        linhas = [{0: "Produto", 1: "Qtde", 2: "Valor Total", 3: "Total Custo"}, {0: "CAFE", 1: 2, 2: 20.0, 3: "N/A"},
                  {0: "LEITE", 1: 1, 2: 10.0, 3: "NULL"}, {0: "ACUCAR", 1: 1, 2: 5.0, 3: 4.0}]
        caminhos = self.tres_formatos("ausente", linhas)
        base = read_raw(caminhos["xlsx"], get_limits())
        self.assertTrue(pd.isna(base.iat[1, 3]) and pd.isna(base.iat[2, 3]))
        for extensao in ("xls", "xml"):
            with self.subTest(formato=extensao):
                lida = read_raw(caminhos[extensao], get_limits())
                self.assertTrue(pd.isna(lida.iat[1, 3]) and pd.isna(lida.iat[2, 3]), lida[3].tolist())
                self.assertTrue(security_gatekeeper._mesma_matriz(lida, base))

    def test_orcamento_de_celulas_do_arquivo_soma_as_abas_do_xls_e_da_planilha_xml(self):
        """Três abas de 6 células: cada uma cabe no orçamento por aba, e o arquivo soma 18."""
        for extensao, caminho in self.densa("abas", linhas=2, colunas=3, abas=3).items():
            with self.subTest(formato=extensao):
                folgada = replace(get_limits(), max_cells_per_sheet=6, max_cells_per_file=18)
                with using_limits(folgada):
                    self.assertEqual(validate_input(caminho, "." + extensao, folgada)["cells"], 18)
                justa = replace(folgada, max_cells_per_file=17)
                with using_limits(justa):
                    self.assertIn("orçamento acumulado", self.recusa("cells", validate_input, caminho, "." + extensao, justa))
                self.assertIn("orçamento acumulado", self.recusa("cells", read_raw, caminho, justa))

    def test_planilha_so_com_textos_de_ausente_e_recusada_como_vazia(self):
        from tests import planilha_xml_support as px
        from tests import xls_support
        pasta = xls_support.Pasta()
        pasta.aba("Report").por(0, 0, "N/A").por(1, 1, "NULL")
        caminhos = {"xls": self.gravar("ausentes.xls", bytes(pasta.arquivo())),
                    "xml": px.gravar(self.pasta / "ausentes.xml", px.tabela(px.linha(px.celula("N/A")),
                                                                            px.linha(px.celula("NULL", indice=2))))}
        for extensao, caminho in caminhos.items():
            with self.subTest(formato=extensao):
                self.assertIn("não contém dados", self.recusa("empty", read_raw, caminho, get_limits()))

    def test_orcamento_de_celulas_por_aba_vale_para_a_planilha_cheia(self):
        """10 x 10 células com valor: a área (100) cabe em 4 vezes o orçamento; quem recusa é a contagem das
        células com valor da aba, e não o teto de área nem o orçamento do arquivo."""
        for extensao, caminho in self.densa("cheia").items():
            with self.subTest(formato=extensao):
                justa = replace(get_limits(), max_cells_per_sheet=99, max_cells_per_file=1000)
                with using_limits(justa):
                    mensagem = self.recusa("cells", validate_input, caminho, "." + extensao, justa)
                self.assertNotIn("área", mensagem)
                self.assertNotIn("acumulado", mensagem)
                folgada = replace(justa, max_cells_per_sheet=100)
                with using_limits(folgada):
                    self.assertEqual(validate_input(caminho, "." + extensao, folgada)["filled_cells_selected"], 100)

    def test_tetos_de_linhas_colunas_abas_e_caracteres_da_politica_chegam_aos_leitores(self):
        """Planilha de 3 abas, 6 linhas e 4 colunas, com textos de 4 caracteres: passa no teto exato de cada
        limite e é recusada, com o código do limite, um abaixo."""
        exata = replace(get_limits(), max_rows=6, max_columns=4, max_sheets=3, max_field_chars=4)
        for extensao, caminho in self.densa("limites", linhas=6, colunas=4, abas=3).items():
            with self.subTest(formato=extensao, limite="exato"):
                with using_limits(exata):
                    self.assertEqual(validate_input(caminho, "." + extensao, exata)["sheets"], 3)
            for campo, codigo in (("max_rows", "rows"), ("max_columns", "columns"), ("max_sheets", "sheets"),
                                  ("max_field_chars", "field_chars")):
                with self.subTest(formato=extensao, limite=campo):
                    justa = replace(exata, **{campo: getattr(exata, campo) - 1})
                    with using_limits(justa):
                        self.recusa(codigo, validate_input, caminho, "." + extensao, justa)
                    self.recusa(codigo, read_raw, caminho, justa)


class SeparadorDoCsvTests(_LeiautesCase):
    """A escolha do separador pela largura típica não muda o arquivo que já era lido certo."""

    def matriz(self, texto, codificacao="utf-8"):
        caminho = self.pasta / f"separador_{len(list(self.pasta.iterdir()))}.csv"
        caminho.write_bytes(texto.encode(codificacao))
        return read_raw(str(caminho), get_limits())

    def test_arquivo_pequeno_com_um_nome_cheio_de_virgulas_fica_com_o_ponto_e_virgula(self):
        # Três linhas de dado: a do nome com vírgulas é um terço delas, e ainda assim não decide o separador.
        matriz = self.matriz("Codigo;Produto;Qtde;Valor Total\n1;Cafe 500g;10;180,00\n"
                             "2;Kit churrasco, sal, carvao, espeto, pano, faca;5;30,00\n3;Leite 1L;20;100,00\n")
        self.assertEqual((matriz.attrs["separator"], matriz.shape), (";", (4, 4)))
        self.assertEqual(matriz.iat[2, 1], "Kit churrasco, sal, carvao, espeto, pano, faca")

    def test_uma_linha_de_dado_so_continua_lida(self):
        self.assertEqual(self.matriz("Produto;Qtde;Valor Total\nCAFE;2;20,00\n").attrs["separator"], ";")
        self.assertEqual(self.matriz("CAFE;2;20,00\n").shape, (1, 3))
        self.assertEqual(self.matriz("Product,Qty,Total\nCoffee,2,20.00\n").attrs["separator"], ",")

    def test_campos_vazios_do_fim_omitidos_nao_trocam_o_separador(self):
        # Metade das linhas vem sem o último campo: pelo ponto e vírgula há 7 e 6 campos; pela vírgula, 5 em todas.
        linhas = ["Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque"]
        linhas += [f"{n};PRODUTO {n};{n},000;2,50;{n * 2},50;1,25" + (";3" if n % 2 else "") for n in range(1, 21)]
        matriz = self.matriz("\n".join(linhas) + "\n")
        self.assertEqual((matriz.attrs["separator"], matriz.shape), (";", (21, 7)))

    def test_bloco_de_rotulo_e_valor_antes_de_uma_tabela_pequena_nao_troca_o_separador(self):
        texto = ("Empresa:;Mercado Ficticio, Ltda\nPeriodo:;01/01/2026 a 31/01/2026\nEmitido em:;10/11/2026, 10:30\n"
                 "Filial:;Loja 1\nUsuario:;consultor\n"
                 "Codigo;Produto;Qtde;Valor Total;Total Custo\n1;CAFE 500G;2,000;20,00;15,00\n"
                 "2;LEITE 1L;3,000;15,00;12,00\n")
        matriz = self.matriz(texto)
        self.assertEqual((matriz.attrs["separator"], matriz.shape[1]), (";", 5))

    def test_virgula_tabulacao_e_barra_continuam_reconhecidas(self):
        casos = {",": "Product,Qty,Total\nCoffee,2,20.00\nMilk,3,15.00\n",
                 "\t": "Produto\tQtde\tValor Total\nCAFE\t2\t20,00\nLEITE\t3\t15,00\n",
                 "|": "Produto|Qtde|Valor Total\nCAFE|2|20,00\nLEITE|3|15,00\n"}
        for separador, texto in casos.items():
            with self.subTest(separador=separador):
                matriz = self.matriz(texto)
                self.assertEqual((matriz.attrs["separator"], matriz.shape), (separador, (3, 3)))

    def test_so_colunas_numericas_com_virgula_decimal_ficam_com_o_ponto_e_virgula(self):
        # Pela vírgula as linhas de dado também têm 3 campos: no empate de largura, a vírgula perde para o
        # separador que alcança ao menos metade das linhas dela.
        matriz = self.matriz("Codigo;Qtde;Valor\n1;2,5;10,00\n2;1,5;7,50\n3;4,0;9,25\n")
        self.assertEqual((matriz.attrs["separator"], matriz.iat[1, 1]), (";", "2,5"))

    def test_muitos_nomes_com_uma_virgula_nao_trocam_o_separador(self):
        # Loja de tintas: "ESMALTE 3,6L" em 40% ou em 80% dos nomes. Com a coluna de código, a vírgula empata na
        # largura (8 campos nas linhas com "3,6L"); sem ela, passa do ponto e vírgula por uma coluna (8 contra
        # 7). Em todos os casos o ponto e vírgula organiza todas as linhas, e é ele o separador (achado da
        # revisão final: o caso sem a coluna de código era recusado, antes e depois da unificação).
        for codigo, colunas in (("Codigo;", 8), ("", 7)):
            for com_virgula in (2, 4):
                with self.subTest(colunas=colunas, nomes_com_virgula_em_cinco=com_virgula):
                    linhas = [codigo + "Nome do produto;Qtde Vend;Total Venda;Total Custo;Marg.;Lucro;Estoque Atual"]
                    linhas += [(f"{n};" if codigo else "")
                               + f"ESMALTE FICTICIO {'3,6L' if n % 5 < com_virgula else '18L'};"
                               f"{n},0;1{n},50;{n},25;10,00;2,25;{n},00" for n in range(1, 41)]
                    matriz = self.matriz("\r\n".join(linhas) + "\r\n", "cp1252")
                    self.assertEqual((matriz.attrs["separator"], matriz.shape), (";", (41, colunas)))

    # --- Achados da revisão independente final e testes que o teste de mutação dela pediu -----------------
    def test_virgula_decimal_que_empata_na_largura_nao_fica_com_o_arquivo(self):
        """Regressão achada pela revisão final: numa tabela em que todas as colunas, menos a do nome, têm
        vírgula decimal, a vírgula empata na largura e ganhava na contagem de linhas. Relatórios que eram
        lidos passavam a ser recusados na análise mensal, no Raio-X e na Avaliação de Cenário."""
        esparsa = [f"PRODUTO FICTICIO {n};{n},000;{n * 3},50" + (";promocao" if n % 10 == 0 else "") for n in range(1, 21)]
        do_excel = ["Produto;Qtde;Preco Unitario;Valor Total;Obs"] + [
            f"PRODUTO FICTICIO {n};{n},000;2,50;{n * 2},50;" for n in range(1, 101)]
        do_excel = [linha if numero < 16 else linha.rstrip(";") for numero, linha in enumerate(do_excel)]
        tres_produtos = "CAFE FICTICIO 500G;10,000;180,00\nACUCAR FICTICIO 1KG;5,000;30,00\nLEITE FICTICIO 1L;20,000;100,00\n"
        casos = {
            "coluna final esparsa, omitida quando vazia": (
                "\n".join(["Relatorio de vendas por produto", "Periodo: 01/01/2026 a 31/01/2026",
                           "Produto;Qtde;Valor Total;Obs", *esparsa]) + "\n", ";", 20, 640.0),
            "salvo como CSV pelo Excel: o separador final só no primeiro bloco de 16 linhas": (
                "\r\n".join(do_excel) + "\r\n", ";", 100, 10150.0),
            "duas linhas de texto com duas vírgulas no topo": (
                "Mercado Ficticio Ltda, Rua das Flores, 123\nEmitido em 05/02/2026, 10:35, usuario ADMIN\n"
                "Produto;Qtde;Valor Total\n" + tres_produtos, ";", 3, 310.0),
            "cinco linhas de texto com duas vírgulas (o dobro exato das linhas da tabela)": (
                "".join(f"Loja {n}, Rua das Flores, {n}00\n" for n in range(1, 6))
                + "Produto;Qtde;Valor Total\n" + tres_produtos, ";", 3, 310.0),
            "cabeçalho com o separador final e os dados sem ele": (
                "Rua das Flores, 123, Centro\nEmitido em 05/02/2026, 10:35, usuario ADMIN\n"
                "Produto;Qtde;Valor Total;\n" + tres_produtos, ";", 3, 310.0),
            "tabulação com coluna final esparsa": (
                "\n".join(["Produto\tQtde\tValor Total\tObs", *esparsa]).replace(";", "\t") + "\n", "\t", 20, 640.0),
        }
        for numero, (rotulo, (texto, separador, linhas, venda)) in enumerate(casos.items()):
            with self.subTest(caso=rotulo):
                caminho = self.csv(texto, f"empate_{numero}.csv")
                self.assertEqual(validate_input(caminho, "csv", get_limits())["separator"], separador)
                vendas = etl_processor.carregar_vendas(caminho)
                self.assertEqual((len(vendas), round(float(vendas["valor_venda"].sum()), 2)), (linhas, venda))
        compras = ("Mercado Ficticio Ltda, CNPJ 00.000.000/0001-00\nRua das Flores, 123 - Centro\n"
                   "Relatorio de Compras por Fornecedor\nPeriodo: 01/01/2026 a 31/01/2026\nFornecedor;Valor Total\n"
                   "DISTRIBUIDORA FICTICIA A LTDA;1.500,00\nATACADO FICTICIO B S/A;800,00\nINDUSTRIA FICTICIA C LTDA;1.250,40\n")
        notas, coluna = etl_processor.carregar_compras(self.csv(compras, "compras_duas_colunas.csv"))
        self.assertEqual(round(float(notas[coluna].sum()), 2), 3550.40)

    def test_bloco_grande_de_rotulo_e_valor_antes_de_uma_tabela_de_dois_produtos(self):
        # Doze linhas de rótulo e valor e uma tabela de três linhas (menos de um quinto): pelo ponto e vírgula a
        # largura típica é a dos rótulos (2), e pela vírgula, a das duas linhas de dado (3). O ponto e vírgula
        # organiza 15 linhas e a vírgula, 2: é ele o separador, como era antes da unificação.
        rotulos = ["Empresa;Mercado Ficticio Ltda", "CNPJ;00.000.000/0001-00", "Relatorio;Vendas por produto",
                   "Periodo;01/01/2026 a 31/01/2026", "Loja;01", "Usuario;ADMIN", "Vendedor;Todos", "Grupo;Todos",
                   "Fornecedor;Todos", "Cliente;Todos", "Forma de pagamento;Todas", "Emissao;05/02/2026 10:35"]
        texto = "\n".join(rotulos) + ("\nCodigo;Produto;Qtde;Valor Total\n1;CAFE FICTICIO 500G;10,000;180,00\n"
                                      "2;ACUCAR FICTICIO 1KG;5,000;30,00\n")
        caminho = self.csv(texto)
        self.assertEqual(validate_input(caminho, "csv", get_limits())["separator"], ";")
        self.assertEqual(float(etl_processor.carregar_vendas(caminho)["valor_venda"].sum()), 210.0)

    def test_csv_separado_por_virgula_continua_com_a_virgula(self):
        """A vírgula só perde para o separador que organiza a tabela. O ponto e vírgula em poucos nomes, numa
        tabela de menos de três colunas por ele, ou dentro de um campo de texto não tira dela o arquivo."""
        fornecedores = ["Fornecedor,Valor Total"] + [
            f"FORNECEDOR {n}{'; FILIAL' if n in (7, 23) else ''},{n}00.50" for n in range(1, 41)]
        casos = {
            "duas colunas, com ponto e vírgula no nome de 2 de 40 fornecedores (empate de largura)": (
                "\n".join(fornecedores) + "\n", (41, 2)),
            "três colunas, com um ponto e vírgula em todos os nomes (uma coluna a menos, de só duas)": (
                "Product,Qty,Total\n" + "".join(f"Coffee {n}; ground,{n},{n}0.50\n" for n in range(1, 5)), (5, 3)),
            "cinco colunas, com dois ponto e vírgulas num campo de texto (duas colunas a menos)": (
                "Product,Qty,Price,Total,Tags\n"
                + "".join(f"Coffee {n},{n},10.00,{n}0.00,organic;fair;dark\n" for n in range(1, 5)), (5, 5)),
            "quatro colunas, com ponto e vírgula em mais linhas que a vírgula, mas não no dobro delas": (
                "Report; Store 1\nIssued; 2026-02-05\nProduct,Qty,Price,Total\n"
                + "".join(f"Coffee {n}; ground,{n},10.00,{n}0.00\n" for n in range(1, 5)), (7, 4)),
            # A linha de topo é um registro que o ponto e vírgula separa e que não tem vírgula: aqui quem mantém
            # a vírgula é a largura (uma coluna a menos só vale numa tabela de três colunas ou mais por ele).
            "três colunas, com um ponto e vírgula em todos os nomes e uma linha de topo que ele divide em duas": (
                "Store: 01; Cashier: 02\nProduct,Qty,Total\n"
                + "".join(f"Coffee {n}; ground,{n},{n}0.50\n" for n in range(1, 5)), (6, 3)),
        }
        for rotulo, (texto, forma) in casos.items():
            with self.subTest(caso=rotulo):
                matriz = self.matriz(texto)
                self.assertEqual((matriz.attrs["separator"], matriz.shape), (",", forma))

    def test_separador_dentro_de_um_campo_nao_tira_o_arquivo_da_virgula(self):
        """Regressão achada pela verificação das correções: num CSV separado por vírgula de verdade, o ";", a
        "|" ou a tabulação duas ou três vezes dentro de um campo de texto dava ao outro separador a largura da
        tabela, ou uma coluna a menos, e ele ficava com o arquivo: a linha de títulos virava uma célula só, e o
        relatório era recusado. O outro separador só fica com o arquivo quando organiza ao menos um registro
        melhor do que a vírgula: um registro sem vírgula nenhuma (num relatório com vírgula decimal, a linha
        de títulos), ou uma linha inteira da tabela dele que ele separa em mais campos do que a vírgula."""
        etiquetas = "".join(f"Coffee {n},{n},{n}0.00,organic;fair;dark\n" for n in range(1, 5))
        casos = {
            "quatro colunas, com dois ponto e vírgulas num campo de texto (uma coluna a menos)": (
                "Product,Qty,Total,Tags\n" + etiquetas, (5, 4)),
            "três colunas, com duas barras em todos os nomes (a mesma largura)": (
                "Product,Qty,Total\n" + "".join(f"SHIRT {n} | BLUE | M,{n},{n}0.50\n" for n in range(1, 5)), (5, 3)),
            "todos os campos entre aspas, com duas tabulações no nome": (
                '"Code","Product","Total"\n' + "".join(f'"{n}","SHIRT {n}\tBLUE\tM","{n}0.50"\n' for n in range(1, 5)),
                (5, 3)),
            "a linha do título do relatório, que nenhum dos dois separa, não é o registro sem vírgula": (
                "Sales report\nProduct,Qty,Total,Tags\n" + etiquetas, (6, 4)),
            # Daqui para baixo, os registros que o outro separador divide têm vírgula, e nenhum deles é uma linha
            # da tabela dele que ele organize melhor do que a vírgula (verificação da terceira rodada).
            "cinco colunas, a última omitida quando vazia, e um ponto e vírgula em todos os nomes": (
                "Produto,Qtde,Preco Unitario,Valor Total,Obs\n"
                + "".join(f"CAFE FICTICIO {n}; moido,{n},10.00,{n}0.00" + (",encarte\n" if n % 4 == 0 else "\n")
                          for n in range(1, 41)), (41, 5)),
            "quatro colunas, a última omitida quando vazia, e três etiquetas em todas as linhas": (
                "Produto,Qtde,Etiquetas,Obs\n"
                + "".join(f"CAFE FICTICIO {n},{n},organico;torrado;moido" + (",encarte\n" if n % 3 == 0 else "\n")
                          for n in range(1, 31)), (31, 4)),
            "quatro colunas, com uma lista de etiquetas mais comprida numa das linhas": (
                "Product,Qty,Total,Tags\n" + etiquetas + "Coffee 9,9,90.00,organic;fair;dark;ground;fresh\n", (6, 4)),
            "uma linha de topo com o outro separador e uma vírgula": (
                "Store 01; Cashier 02, Operator ADMIN\nProduct,Qty,Total,Tags\n" + etiquetas, (6, 4)),
            "uma linha de topo que o outro separador divide em menos campos do que a tabela dele": (
                "Store 01; Cashier 02; Operator ADMIN, shift 1\nProduct,Qty,Total,Tags\n"
                + "".join(f"Coffee {n},{n},{n}0.00,organic;fair;dark;ground\n" for n in range(1, 5)), (6, 4)),
        }
        for rotulo, (texto, forma) in casos.items():
            with self.subTest(caso=rotulo):
                matriz = self.matriz(texto)
                self.assertEqual((matriz.attrs["separator"], matriz.shape), (",", forma))
        vendas = etl_processor.carregar_vendas(self.csv(
            "Produto,Qtde,Valor Total,Etiquetas\n"
            + "".join(f"CAFE FICTICIO {n},{n},{n}0.00,organico;torrado;moido\n" for n in range(1, 5))))
        self.assertEqual((len(vendas), float(vendas["valor_venda"].sum())), (4, 100.0))

    def test_virgula_num_titulo_nao_devolve_a_virgula_o_relatorio_com_ponto_e_virgula(self):
        """Regressão achada pela verificação da terceira rodada: a condição do registro sem vírgula devolvia à
        vírgula o relatório com ";" em que um título tem vírgula ("Qtde, Un", "Cidade, UF"), porque nenhum
        registro ficava sem vírgula, e o relatório que era lido passava a ser recusado. Vale também a linha de
        títulos que o outro separador divide em mais campos do que a vírgula."""
        topo = "Mercado Ficticio Ltda, Rua das Flores, 123\nEmitido em 05/02/2026, 10:35, usuario ADMIN\n"
        dados = "CAFE FICTICIO 500G;10,000;180,50\nACUCAR FICTICIO 1KG;5,000;30,25\nLEITE FICTICIO 1L;20,000;100,75\n"
        for numero, titulos in enumerate(("Produto;Qtde, Un;Valor Total", "Produto;Qtde;Valor Total (R$ 0,00)",
                                          '"Produto, embalagem";"Qtde";"Valor Total"')):
            for separador in (";", "\t", "|"):
                with self.subTest(titulos=titulos, separador=separador):
                    caminho = self.csv((topo + titulos + "\n" + dados).replace(";", separador),
                                       f"titulo_com_virgula_{numero}_{ord(separador)}.csv")
                    self.assertEqual(validate_input(caminho, "csv", get_limits())["separator"], separador)
                    vendas = etl_processor.carregar_vendas(caminho)
                    self.assertEqual(
                        (len(vendas), float(vendas["quantidade"].sum()), float(vendas["valor_venda"].sum())),
                        (3, 35.0, 311.5))
        # Com tantas vírgulas nos títulos quanto nas linhas de dado, quem prova o ponto e vírgula é o registro sem
        # vírgula nenhuma, mesmo mais curto que a tabela (uma linha de rótulo e valor).
        partidos = "Produto (nome, marca);Qtde (un, kg);Valor Total\n"
        for rotulo, texto in (("linha de rótulo e valor", "Loja;Centro\n" + topo + partidos + dados),
                              ("linha em branco, só com os separadores", topo + partidos + ";;\n" + dados)):
            with self.subTest(registro_sem_virgula=rotulo):
                vendas = etl_processor.carregar_vendas(self.csv(texto, f"sem_virgula_{len(rotulo)}.csv"))
                self.assertEqual((len(vendas), float(vendas["valor_venda"].sum())), (3, 311.5))
        # Loja de tintas, sem coluna de código: pela vírgula, os nomes dão uma coluna a mais do que a tabela tem.
        tintas = "Produto;Qtde (kg, un);Valor Total\n" + "".join(
            f"TINTA FICTICIA 3,6L COR {n};{n},000;{n}0,50\n" for n in range(1, 6))
        vendas = etl_processor.carregar_vendas(self.csv(tintas, "tintas.csv"))
        self.assertEqual((len(vendas), float(vendas["valor_venda"].sum())), (5, 152.5))
        compras = (topo + "Relatorio de Compras por Fornecedor\nFornecedor;Cidade, UF;Valor NF\n"
                   "DISTRIBUIDORA FICTICIA A LTDA;CIDADE FICTICIA, SP;1.500,00\n"
                   "ATACADO FICTICIO B S/A;CIDADE FICTICIA, MG;800,00\nINDUSTRIA FICTICIA C LTDA;CIDADE FICTICIA, PR;1.250,40\n")
        notas, coluna = etl_processor.carregar_compras(self.csv(compras, "compras_cidade_uf.csv"))
        self.assertEqual(round(float(notas[coluna].sum()), 2), 3550.40)

    def test_csv_lido_pela_virgula_com_outro_separador_nos_titulos_e_recusado(self):
        """Achado da verificação da terceira rodada: quando os títulos têm tantas vírgulas quanto as linhas de
        dado, nenhum registro prova o outro separador, e o arquivo fica com a vírgula. O cabeçalho partido
        ainda dava produto, quantidade e valor em colunas diferentes, e os números saíam de pedaços das
        células, sem aviso (faturamento 150,00 em vez de 311,50; nas compras, total comprado 0,00). O .csv lido
        pela vírgula com outro separador dentro de uma célula da linha de títulos é recusado."""
        vendas = ("Mercado Ficticio Ltda, Rua das Flores, 123\nProduto (nome, marca);Qtde (un, kg);Valor Total\n"
                  "CAFE FICTICIO 500G;10,000;180,50\nACUCAR FICTICIO 1KG;5,000;30,25\nLEITE FICTICIO 1L;20,000;100,75\n")
        for separador in (";", "\t", "|"):
            with self.subTest(separador=separador):
                caminho = self.csv(vendas.replace(";", separador), f"titulos_partidos_{ord(separador)}.csv")
                self.assertEqual(validate_input(caminho, "csv", get_limits())["separator"], ",")
                self.assertIn("não dá para decidir o separador",
                              self.recusa("data", etl_processor.carregar_vendas, caminho))
                mensagem = self.recusa("raio_x_dados", executar_como_o_worker, [caminho])
                self.assertTrue(mensagem.startswith("Arquivo 1: a linha de títulos tem ponto e vírgula"), mensagem)
                self.assertIn("salve como .xlsx", mensagem)
        compras = ("Rua das Flores, 123, Centro\nEmitido em 05/02/2026, 10:35, usuario ADMIN\n"
                   "Relatorio de Compras por Fornecedor\nFornecedor, razao social;Cidade, UF;Valor NF\n"
                   "DISTRIBUIDORA FICTICIA A LTDA;CIDADE FICTICIA, SP;1.500,00\nATACADO FICTICIO B S/A;CIDADE FICTICIA, MG;800,00\n")
        self.assertIn("não dá para decidir o separador",
                      self.recusa("data", etl_processor.carregar_compras, self.csv(compras, "compras_partidas.csv")))
        # A conferência é só do .csv lido pela vírgula, e só da linha de títulos: o título com barra num
        # relatório com ";" e o ";" na linha de cima de um cabeçalho de duas linhas não recusam nada.
        com_barra = "Produto;Qtde | Un;Valor Total\nCAFE;2;20,00\nLEITE;1;10,00\n"
        self.assertEqual(float(etl_processor.carregar_vendas(self.csv(com_barra, "titulo_com_barra.csv"))["valor_venda"].sum()), 30.0)
        em_cima = "Relatorio de vendas por produto; loja 01,,\nItem,Qtde,Valor Total\nCAFE,2,20.00\nLEITE,1,10.00\n"
        lidas = etl_processor.carregar_vendas(self.csv(em_cima, "ponto_e_virgula_em_cima.csv"))
        self.assertEqual((len(lidas), float(lidas["valor_venda"].sum())), (2, 30.0))

    def test_bloco_de_rotulo_e_valor_maior_que_a_tabela_nao_troca_o_separador(self):
        # Seis linhas de rótulo e valor e uma tabela de quatro: as linhas inteiras da tabela são 40% das
        # linhas com mais de um campo (acima de um quinto, abaixo da metade), e a largura típica é a delas.
        tabela = ("Codigo;Produto;Qtde;Valor Total\n1;CAFE 500G;2,000;20,00\n2;LEITE 1L;3,000;15,00\n"
                  "3;ACUCAR 1KG;1,000;5,00\n")
        simples = ("Empresa:;Mercado Ficticio\nPeriodo:;01/01/2026 a 31/01/2026\nEmitido em:;10/11/2026\n"
                   "Filial:;Loja 1\nUsuario:;consultor\nRelatorio:;Vendas por produto\n")
        # Com duas vírgulas em cada rótulo, a vírgula dá 3 campos às seis linhas de rótulo e às três de dado:
        # só a largura típica do ponto e vírgula (4, a da tabela) o mantém como separador.
        com_virgulas = "".join(f"Endereco {n}:;Rua das Flores, {n}00, Centro\n" for n in range(1, 7))
        for rotulo, rotulos in (("rótulos simples", simples), ("rótulos com duas vírgulas", com_virgulas)):
            with self.subTest(caso=rotulo):
                matriz = self.matriz(rotulos + tabela)
                self.assertEqual((matriz.attrs["separator"], matriz.shape[1]), (";", 4))

    def test_dois_nomes_cheios_de_virgulas_em_vinte_linhas_nao_trocam_o_separador(self):
        # Pela vírgula, duas linhas em vinte (10%, abaixo de um quinto) têm 10 campos; as outras, 7.
        linhas = ["Codigo;Nome do produto;Qtde Vend;Total Venda;Total Custo;Marg.;Lucro;Estoque Atual"]
        linhas += [f"{n};PRODUTO FICTICIO {n};{n},0;1{n},50;{n},25;10,00;2,25;{n},00" for n in range(1, 19)]
        linhas += [f"{n};KIT FICTICIO 0,9L, 3,6L E 18L;2,0;30,50;20,25;10,00;10,25;4,00" for n in (19, 20)]
        matriz = self.matriz("\r\n".join(linhas) + "\r\n", "cp1252")
        self.assertEqual((matriz.attrs["separator"], matriz.shape), (";", (21, 8)))

    def test_poucas_linhas_mais_largas_nao_definem_a_largura_tipica(self):
        # Vinte linhas de texto com uma vírgula e uma tabela de três produtos, dois deles com o nome cheio de
        # vírgulas: pela vírgula, as duas linhas largas são menos de um quinto das linhas e não definem a
        # largura típica (2, a das linhas de texto), que perde para a do ponto e vírgula (4).
        texto = ("".join(f"Filtro {n}: Loja {n}, Centro\n" for n in range(1, 21))
                 + "Codigo;Produto;Qtde;Valor Total\n1;KIT SAL, CARVAO, ESPETO, PANO, FACA, GARFO;5;30,00\n"
                 "2;KIT COPO, PRATO, TALHER, TOALHA, VELA, FLOR;2;20,00\n3;CAFE 500G;2;20,00\n")
        matriz = self.matriz(texto)
        self.assertEqual((matriz.attrs["separator"], matriz.shape), (";", (24, 4)))

    def test_uma_linha_sozinha_nao_define_a_largura_tipica(self):
        # Duas linhas com vírgula: o título, com uma, e o nome cheio de vírgulas. A linha larga, sozinha, não é
        # a largura típica da vírgula (2), que perde para a do ponto e vírgula (3).
        matriz = self.matriz("Produto, descricao;Qtde;Valor Total\nKIT SAL, CARVAO, ESPETO, PANO;5;30,00\n")
        self.assertEqual((matriz.attrs["separator"], matriz.shape), (";", (2, 3)))

    def test_uma_linha_so_separada_por_virgula_continua_lida(self):
        matriz = self.matriz("Coffee,2,20.00\n")
        self.assertEqual((matriz.attrs["separator"], matriz.shape), (",", (1, 3)))

    def test_empate_completo_fica_com_o_primeiro_separador(self):
        # Pelo ponto e vírgula e pela tabulação, as três linhas têm dois campos: vale a ordem de sempre.
        matriz = self.matriz("Produto;Qtde\tValor\nCAFE;2\t5\nLEITE;1\t5\n")
        self.assertEqual((matriz.attrs["separator"], matriz.iat[1, 1]), (";", "2\t5"))


class ProtecoesSemFalsoPositivoTests(_LeiautesCase):
    """As proteções novas não recusam o relatório legítimo (achados da revisão independente)."""

    def test_coluna_de_percentual_nao_e_titulo_repetido(self):
        # Curva ABC: "% Valor Total" perde o "%" na normalização e ficaria igual a "Valor Total".
        casos = ("Codigo;Produto;Qtde;Valor Total;% Valor Total;% Acumulado\n1;CAFE;2;20,00;66,67;66,67\n"
                 "2;LEITE;1;10,00;33,33;100,00\n",
                 "Produto;Qtde;Faturamento;Faturamento (%);Custo;% Custo\nCAFE;2;20,00;66,67;12,00;60,00\n"
                 "LEITE;1;10,00;33,33;8,00;40,00\n")
        for numero, texto in enumerate(casos):
            with self.subTest(caso=numero):
                vendas = etl_processor.carregar_vendas(self.csv(texto, f"abc_{numero}.csv"))
                self.assertEqual((len(vendas), float(vendas["valor_venda"].sum())), (2, 30.0))

    def test_dois_titulos_de_valor_iguais_continuam_recusados(self):
        texto = "Produto;Qtde;Valor Unitario;Valor Unitario\nCAFE;2;8,00;10,00\nLEITE;1;4,00;5,00\n"
        self.assertIn("dois títulos de coluna iguais",
                      self.recusa("data", etl_processor.carregar_vendas, self.csv(texto)))

    def test_titulo_comprido_de_coluna_ao_lado_de_outros_titulos_e_titulo(self):
        texto = ("Produto;Quantidade Vendida;Valor Total de Vendas no Periodo de 01/01/2026 a 31/01/2026\n"
                 "CAFE;2;20,00\nLEITE;1;10,00\n")
        vendas = etl_processor.carregar_vendas(self.csv(texto))
        self.assertEqual(float(vendas["valor_venda"].sum()), 30.0)

    def test_compras_legitimas_nao_sao_confundidas_com_vendas(self):
        casos = {
            "cidade que começa por Venda": ("Mercado Ficticio Ltda\nVenda Nova do Imigrante - ES\n"
                                            "Fornecedor;NF;Data;Valor Total\nDistribuidora A;101;05/01/2026;1.300,00\n"
                                            "Atacado B;102;09/01/2026;1.000,00\n", 2300.0),
            "data de faturamento": ("Relatorio de Entradas de Mercadoria\nProduto;Qtde;Custo Unitario;Total;Dt. Faturamento\n"
                                    "CAFE;10;12,00;120,00;05/01/2026\nLEITE;10;5,00;50,00;09/01/2026\n", 170.0),
            "compras por produto, sem fornecedor": ("Codigo;Produto;Qtde Comprada;Total Custo\n1;CAFE;10;120,00\n"
                                                    "2;LEITE;10;50,00\n", 170.0),
        }
        for rotulo, (texto, total) in casos.items():
            with self.subTest(caso=rotulo):
                compras, coluna = etl_processor.carregar_compras(self.csv(texto, f"compras_{len(rotulo)}.csv"))
                self.assertEqual(float(compras[coluna].sum()), total)

    def test_vendas_por_fornecedor_no_campo_de_compras_e_recusado_pelo_titulo(self):
        # Tem a coluna "Fornecedor" e um valor total: sem o título, passaria por compras por nota.
        caminho, _verdade = self.xlsx("por_fornecedor")
        self.assertIn("é um relatório de VENDAS", self.recusa("data", etl_processor.carregar_compras, caminho))
        texto = "Vendas por fornecedor\nPeriodo: 01/01/2026 a 31/01/2026\nFornecedor;Valor Total\nDistribuidora A;1.300,00\n"
        self.assertIn("é um relatório de VENDAS",
                      self.recusa("data", etl_processor.carregar_compras, self.csv(texto)))

    # --- Achados da revisão independente final e testes que o teste de mutação dela pediu -----------------
    def test_coluna_de_percentual_antes_da_coluna_do_valor_nao_fica_com_o_valor(self):
        """Achado da revisão final: "% Valor Total" à esquerda de "Valor Total" ficava com o valor da venda, e o
        faturamento saía da soma dos percentuais (100,00). A coluna de percentual não fica com campo nenhum."""
        casos = ("Codigo;Produto;Qtde;% Valor Total;Valor Total\n1;CAFE;2;66,67;20,00\n2;LEITE;1;33,33;10,00\n",
                 "Produto;Qtde;Faturamento (%);Faturamento;% Custo;Custo\nCAFE;2;66,67;20,00;60,00;12,00\n"
                 "LEITE;1;33,33;10,00;40,00;8,00\n")
        for numero, texto in enumerate(casos):
            with self.subTest(caso=numero):
                vendas = etl_processor.carregar_vendas(self.csv(texto, f"percentual_{numero}.csv"))
                self.assertEqual(float(vendas["valor_venda"].sum()), 30.0)
        self.assertEqual(float(vendas["valor_custo"].sum()), 20.0)  # e não 100,00, a soma de "% Custo"
        # Dois títulos iguais de valor continuam recusados, com a coluna de percentual ao lado ou sem ela.
        repetido = "Produto;Qtde;% Valor Total;Valor Total;Valor Total\nCAFE;2;66,67;16,00;20,00\nLEITE;1;33,33;4,00;5,00\n"
        self.assertIn("dois títulos de coluna iguais",
                      self.recusa("data", etl_processor.carregar_vendas, self.csv(repetido, "repetido.csv")))

    def test_percentual_como_parametro_do_titulo_nao_tira_o_campo_da_coluna(self):
        """Achado da verificação das correções: a regra do percentual tirava o campo de todo título com "%".
        O "%" depois de um número é parâmetro do título ("Valor Total (-5% desc.)", "Preço c/ ICMS 18%"), e a
        coluna continua sendo a do valor, como era lida antes."""
        vendas = etl_processor.carregar_vendas(self.csv(
            "Produto;Qtde;Preco Unitario;Valor Total (-5% desc.)\nCAFE;2;10,00;19,00\nLEITE;1;7,00;6,65\n", "desconto.csv"))
        self.assertEqual(round(float(vendas["valor_venda"].sum()), 2), 25.65)  # o total informado, e não 2 x 10 + 7
        casos = {"Produto;Qtde;Valor Total c/ 10 %\nCAFE;2;22,00\nLEITE;1;7,70\n": ("valor_venda", 29.70),
                 "Produto;Qtde;Valor Total;Custo + 4% frete\nCAFE;2;20,00;12,48\nLEITE;1;7,00;3,52\n": ("valor_custo", 16.0),
                 "Produto;Qtde;Preco Venda c/ ICMS 18%\nCAFE;2;10,00\nLEITE;1;7,00\n": ("valor_venda", 27.0)}
        for numero, (texto, (campo, soma)) in enumerate(casos.items()):
            with self.subTest(titulos=texto.splitlines()[0]):
                vendas = etl_processor.carregar_vendas(self.csv(texto, f"parametro_{numero}.csv"))
                self.assertEqual(round(float(vendas[campo].sum()), 2), soma)
        notas, coluna = etl_processor.carregar_compras(self.csv(
            "Fornecedor;Valor NF c/ IPI 5%\nDistribuidora A;100,00\nAtacado B;50,00\n", "compras_com_ipi.csv"))
        self.assertEqual(float(notas[coluna].sum()), 150.0)
        # Não sendo coluna de percentual, o título com o parâmetro também conta para os títulos repetidos.
        repetido = "Produto;Qtde;Preco c/ ICMS 18%;Preco c/ ICMS 18%\nCAFE;2;8,00;10,00\nLEITE;1;4,00;5,00\n"
        self.assertIn("dois títulos de coluna iguais",
                      self.recusa("data", etl_processor.carregar_vendas, self.csv(repetido, "icms_repetido.csv")))

    def test_titulo_de_data_e_conferido_em_cada_linha_do_cabecalho(self):
        """Achado da verificação das correções: quando o cabeçalho é lido em duas linhas, o texto das duas
        juntas não começa por "data" ("periodo ... data faturamento"), e a coluna da data voltava a ficar com
        o valor da venda. Vale o título de data em qualquer das duas linhas."""
        casos = {
            "a linha do período logo acima e o total num título de reserva": (
                PERIODO_DE_DOIS_MESES + "Data Faturamento;Produto;Qtde;Total\n"),
            "o título de data na linha de cima de um cabeçalho de duas linhas": (
                "Relatorio de vendas item a item\nPeriodo: 01/01/2026 a 28/02/2026\n\n"
                "Data;Produto;Qtde;Valor\nFaturamento;;;Total\n"),
        }
        for numero, (rotulo, cabecalho) in enumerate(casos.items()):
            with self.subTest(caso=rotulo):
                caminho = self.csv(cabecalho + "15/01/2026;PRODUTO A;1;30,00\n20/02/2026;PRODUTO A;2;40,00\n",
                                   f"data_em_duas_linhas_{numero}.csv")
                self.assertEqual(float(etl_processor.carregar_vendas(caminho)["valor_venda"].sum()), 70.0)
                resultado = avaliar_como_o_worker([caminho], hoje=HOJE)
                self.assertEqual([fatia["faturamento"] for fatia in resultado["raio_x"]["fatias"]], [30.0, 40.0])

    def test_rotulo_de_data_no_topo_nao_tira_o_campo_da_coluna_de_baixo(self):
        """Regressão achada pela verificação das correções: quando o cabeçalho é reconhecido pelo par de linhas
        (a coluna do nome se chama "Item" ou "Nome", e o "produto" vem do título do relatório), a emissão do
        relatório na linha de cima ("Data: 05/02/2026"), sobre a coluna da quantidade ou do valor, passava por
        título de data e tirava o campo da coluna. O rótulo com o valor dele (a data ou a hora na própria
        célula, ou na próxima célula preenchida da linha) não é título de coluna."""
        casos = ("Relatorio de vendas por produto;Data: 05/02/2026;Pagina 1\nItem;Qtde;Valor Total\n",
                 "Relatorio de produtos vendidos;;Dt. emissao: 05/02/2026\nNome;Quantidade;Valor Total\n",
                 "Relatorio de vendas por produto;Data:;05/02/2026\nItem;Qtde;Valor Total\n",
                 "Relatorio de vendas por produto;Data 05/02/2026;Pagina 1\nItem;Qtde;Valor Total\n",
                 # O rótulo numa célula e o valor na célula ao lado, sem dois-pontos (verificação da terceira
                 # rodada); a célula de data de uma planilha chega como "2026-02-05 00:00:00".
                 "Relatorio de vendas por produto;Data de emissao;05/02/2026\nItem;Qtde;Valor Total\n",
                 "Relatorio de vendas por produto;Data;2026-02-05 00:00:00\nItem;Qtde;Valor Total\n",
                 "Relatorio de vendas por produto;Dt. Emissao;05/02/2026 10:35\nItem;Qtde;Valor Total\n",
                 "Relatorio de produtos vendidos;Data Inicial;01/01/2026;Data Final;31/01/2026\nNome;Quantidade;Valor Total\n")
        for numero, cabecalho in enumerate(casos):
            with self.subTest(topo=cabecalho.splitlines()[0]):
                vendas = etl_processor.carregar_vendas(self.csv(cabecalho + "CAFE;10;180,00\nACUCAR;5;30,00\n",
                                                                f"emissao_no_topo_{numero}.csv"))
                self.assertEqual((len(vendas), float(vendas["valor_venda"].sum())), (2, 210.0))

    def test_titulo_de_data_com_dois_pontos_ou_numero_continua_sem_campo_numerico(self):
        """Achado da verificação da terceira rodada: a exceção do rótulo valia para qualquer célula com
        dois-pontos ou dígito, e o título de data de verdade escrito assim ("Data Faturamento:", "Dt.
        Faturamento 2026") voltava a ficar com o valor da venda, e as vendas saíam zeradas. A exceção é só do
        rótulo com uma data ou uma hora, só na linha de cima do cabeçalho de duas linhas."""
        casos = {
            "dois-pontos no título de cima": "Produto;Data Faturamento:;Qtde;Valor\n;;;Total\n",
            "número no título de cima": "Produto;Dt. Faturamento 2026;Qtde;Valor\n;;;Total\n",
            "título com número ao lado do título de data": "Produto;Data;Vendas 2026;Valor\n;Faturamento;Qtde;Total\n",
            "texto que só termina por uma data ao lado do título de data":
                "Produto;Data;Venda ate 31/01/2026;Valor\n;Faturamento;Qtde;Total\n",
            "texto comprido que começa por uma data ao lado do título de data":
                "Produto;Data;01/01/2026 a 31/01/2026 (vendas);Valor\n;Faturamento;Qtde;Total\n",
            "o período dentro do título, no cabeçalho de uma linha":
                "Produto;Data Faturamento (01/01/2026 a 31/01/2026);Qtde;Valor Total\n",
            "o mês dentro do título de data da linha de baixo": "Produto;Ult.;Qtde;Valor\n;Dt. Custo 01/2026;Vendida;Total\n",
        }
        for numero, (rotulo, cabecalho) in enumerate(casos.items()):
            with self.subTest(caso=rotulo):
                vendas = etl_processor.carregar_vendas(self.csv(
                    cabecalho + "CAFE;05/01/2026;10;180,00\nACUCAR;09/01/2026;5;30,00\n", f"titulo_de_data_{numero}.csv"))
                self.assertEqual((len(vendas), float(vendas["quantidade"].sum()), float(vendas["valor_venda"].sum())),
                                 (2, 15.0, 210.0))
                self.assertNotIn("valor_custo", vendas.columns)

    def test_titulo_de_compra_ou_de_custo_nao_fica_com_o_valor_da_venda(self):
        """Achado da revisão final: "Preço Compra" à esquerda de "Preço Venda" ficava com o preço unitário, e o
        faturamento saía de quantidade x preço de compra, sem aviso (o mesmo dano dos dois "Valor Unitário"
        iguais, em outra grafia). Vale também para um segundo título de custo."""
        for numero, titulos in enumerate(("Preco Compra;Preco Venda", "Preco de Compra;Preco de Venda",
                                          "Valor Unitario Compra;Valor Unitario Vendido",
                                          "Custo Medio;Preco Custo Medio;Preco Venda")):
            with self.subTest(titulos=titulos):
                a_mais = "9,00;" if titulos.count(";") == 2 else ""
                texto = f"Produto;Qtde;{titulos}\nCAFE;10;{a_mais}12,00;18,00\nLEITE;20;{a_mais}4,00;5,00\n"
                vendas = etl_processor.carregar_vendas(self.csv(texto, f"compra_{numero}.csv"))
                self.assertEqual(float(vendas["valor_venda"].sum()), 280.0)  # 10 x 18 + 20 x 5, e não 10 x 12 + 20 x 4
                self.assertIn("vend", vendas.attrs["cabecalhos"]["preco_unitario"])
        # Com a coluna de total, o preço de compra não vira o preço de tabela (todo item tinha "variação").
        com_total = ("Produto;Qtde;Preco Compra;Preco Venda;Valor Total\nCAFE;10;12,00;18,00;180,00\n"
                     "LEITE;20;4,00;5,00;100,00\n")
        resultado = executar_como_o_worker([self.csv(PERIODO_DE_JANEIRO + com_total, "com_total.csv")])
        self.assertEqual((resultado["saude"]["faturamento_total"], resultado["scanner"]["itens_com_variacao"]), (280.0, 0))
        # O segundo título de custo, o total comprado e o CMV não ficam com o total da venda.
        for numero, titulo in enumerate(("Valor Total Custo", "Valor Total Comprado", "Valor Total CMV")):
            with self.subTest(titulo=titulo):
                texto = (f"Produto;Qtde;Custo Unitario;{titulo};Valor Total\nCAFE;10;12,00;120,00;180,00\n"
                         "LEITE;20;4,00;80,00;100,00\n")
                vendas = etl_processor.carregar_vendas(self.csv(texto, f"outro_total_{numero}.csv"))
                self.assertEqual((float(vendas["valor_venda"].sum()), vendas.attrs["cabecalhos"]["valor_venda"]),
                                 (280.0, "valor total"))

    def test_coluna_de_data_nao_fica_com_campo_numerico(self):
        """Achado da revisão final: "Data Faturamento" à esquerda de "Valor Total" ficava com o valor da venda
        (pela palavra "faturamento"), e todas as vendas saíam zeradas. O título que só contém a palavra
        ("Estoque na Data") continua valendo."""
        texto = ("Data Faturamento;Produto;Qtde;Valor Total;Dt. Ult. Custo;Estoque na Data\n"
                 "05/01/2026;CAFE;2;20,00;02/01/2026;7\n09/01/2026;LEITE;1;10,00;03/01/2026;4\n")
        vendas = etl_processor.carregar_vendas(self.csv(texto))
        self.assertEqual((float(vendas["valor_venda"].sum()), float(vendas["estoque"].sum())), (30.0, 11.0))
        self.assertNotIn("valor_custo", vendas.columns)

    def test_titulo_do_relatorio_sozinho_sobre_a_linha_de_titulos_nao_derruba_o_cabecalho(self):
        """Regressão achada pela revisão final: quando o título da coluna do nome não está no dicionário
        ("Item"), o cabeçalho é reconhecido pelo par de linhas, com o "produto" do título do relatório. A frase
        comprida, sozinha na linha de cima, é o título do relatório quando a de baixo é uma linha de títulos."""
        texto = ("Relatorio de vendas por produto - periodo de 01/01/2026 a 31/01/2026\nItem;Qtde;Valor Total\n"
                 "CAFE;2;20,00\nLEITE;1;10,00\n")
        vendas = etl_processor.carregar_vendas(self.csv(texto))
        self.assertEqual((len(vendas), float(vendas["valor_venda"].sum())), (2, 30.0))

    def test_dois_titulos_iguais_de_total_ou_de_custo_tambem_sao_recusados(self):
        casos = {"total": "Produto;Qtde;Valor Total;Valor Total\nCAFE;2;16,00;20,00\nLEITE;1;4,00;5,00\n",
                 "custo": "Produto;Qtde;Valor Total;Total Custo;Total Custo\nCAFE;2;20,00;12,00;15,00\nLEITE;1;5,00;3,00;4,00\n"}
        for rotulo, texto in casos.items():
            with self.subTest(coluna=rotulo):
                self.assertIn("dois títulos de coluna iguais",
                              self.recusa("data", etl_processor.carregar_vendas, self.csv(texto, f"repetido_{rotulo}.csv")))

    def test_entradas_com_a_quantidade_vendida_ao_lado_continuam_sendo_compras(self):
        """Passo 1: o relatório que se diz de entradas é de compras, mesmo com uma coluna que só existe em
        vendas ("Qtde Vend", do relatório de entradas com o giro ao lado)."""
        texto = ("Relatorio de Entradas de Mercadoria\nProduto;Qtde;Qtde Vend;Custo Unitario;Total\n"
                 "CAFE;10;8;12,00;120,00\nLEITE;10;9;5,00;50,00\n")
        compras, coluna = etl_processor.carregar_compras(self.csv(texto))
        self.assertEqual(float(compras[coluna].sum()), 170.0)

    def test_vendas_sem_titulo_no_campo_de_compras_sao_recusadas_pelas_colunas(self):
        """Passo 3: sem título que decida, as colunas que só existem em vendas recusam o arquivo."""
        texto = "Codigo;Produto;Qtde Vend;Total Venda;Total Custo\n1;CAFE;10;180,00;120,00\n2;LEITE;10;60,00;50,00\n"
        self.assertIn("é um relatório de VENDAS", self.recusa("data", etl_processor.carregar_compras, self.csv(texto)))

    def test_compras_com_coluna_de_venda_e_coluna_de_compra_nao_sao_recusadas(self):
        texto = ("Fornecedor;Produto;Qtde Vend;Qtde Comprada;Total Compra\nDistribuidora A;CAFE;8;10;120,00\n"
                 "Atacado B;LEITE;9;10;50,00\n")
        compras, coluna = etl_processor.carregar_compras(self.csv(texto))
        self.assertEqual(float(compras[coluna].sum()), 170.0)


class HarmoniaDosMotoresTests(_LeiautesCase):
    def test_razao_do_custo_implausivel_do_raio_x_e_a_padrao_da_triagem_do_lead(self):
        self.assertEqual(raio_x.RAZAO_CUSTO_IMPLAUSIVEL, triagem_cenario.LIMIARES_PADRAO.razao_custo_implausivel)

    def test_custo_de_exatamente_tres_vezes_a_venda_e_a_confirmar_com_qualquer_numero_de_linhas(self):
        """Achado da revisão final: a razão era comparada em ponto flutuante. Três vendas de 1,10 com custo de
        3,30 cada somam 9,899999999999999 de custo, abaixo de 3 vezes 3,30: o Raio-X deixava o item na perda
        abaixo do custo, e a triagem do lead, que compara em Decimal, o tratava como custo implausível."""
        texto = (PERIODO_DE_JANEIRO + "Codigo;Produto;Qtde;Valor Total;Total Custo\n" + "1;PRODUTO A;1;1,10;3,30\n" * 3
                 + "2;PRODUTO B;1;7,00;21,00\n3;PRODUTO C;1;10,00;29,99\n")
        caminho = self.csv(texto)
        auditoria = executar_como_o_worker([caminho])["auditoria"]
        self.assertEqual((auditoria["custo_a_confirmar"]["itens"], auditoria["itens_venda_abaixo_custo"]), (2, 1))
        self.assertEqual((auditoria["custo_a_confirmar"]["perda_desconsiderada"], auditoria["perda_abaixo_custo"]),
                         (20.6, 19.99))
        linhas = avaliar_como_o_worker([caminho], hoje=HOJE)["linhas"]
        venda, custo = (linhas["colunas"].index(campo) for campo in ("faturamento_com_custo", "custo_total"))
        implausiveis = sum(triagem_cenario.custo_implausivel({"faturamento_com_custo": Decimal(str(linha[venda])),
                                                              "custo_total": Decimal(str(linha[custo]))})
                           for linha in linhas["valores"])
        self.assertEqual(implausiveis, 2)

    def test_texto_do_lead_nao_volta_a_comecar_por_formula_depois_da_limpeza(self):
        """Achado da revisão final: a leitura neutraliza a célula que começa por "=", mas não a que começa por um
        caractere invisível (NBSP, espaço de largura zero, caractere de controle) seguido de "=". A limpeza da
        Avaliação de Cenário tira esse começo, e o texto gravado na staging voltava a começar por "="."""
        prefixos = (" ", "​", "\x01", "\x0b", "　")
        linhas = "".join(f"{prefixo}=1+{n};{prefixo}=cmd|' /C calc'!A{n};{prefixo}@SUM(1+1);{n + 1};{n + 1}0,00\n"
                         for n, prefixo in enumerate(prefixos))
        texto = PERIODO_DE_JANEIRO + "Codigo;Produto;Grupo;Qtde;Valor Total\n" + linhas
        resultado = avaliar_como_o_worker([self.csv(texto)], hoje=HOJE)["linhas"]
        colunas = resultado["colunas"]
        self.assertEqual(len(resultado["valores"]), len(prefixos))
        for linha in resultado["valores"]:
            for campo in ("codigo_produto", "descricao_produto"):
                self.assertRegex(linha[colunas.index(campo)], r"^'[=@]")
        self.assertEqual(resultado["categorias"], ["'@SUM(1+1)"])

    def test_frase_nao_e_titulo_de_coluna(self):
        import pandas as pd
        avisos = pd.DataFrame([
            ['Atenção .: Total das vendas é liquido (Abatendo, Descontos e Fretes) por produto e fornecedor', None],
            ['Atenção .: Valor total dos fretes não esta considerando o "produto genérico" do fornecedor', 8.5],
        ])
        with self.assertRaises(etl_processor.ETLValidationError):
            etl_processor._detectar_cabecalho(avisos, etl_processor.ALIAS_COMPRAS, etl_processor._validar_compras)

    def test_aviso_e_linha_de_duas_celulas_so_nao_viram_cabecalho_por_causa_da_frase(self):
        """O rodapé do relatório por forma de pagamento, na forma em que a regra da frase faz a diferença (a
        verificação das correções mostrou que o teste de cima passa também sem ela): o aviso, com a palavra
        "produto", numa coluna, e o rótulo "Valor total dos fretes" na coluna ao lado. Sem a conferência da
        frase, o par de linhas é um cabeçalho de compras (produto e valor total)."""
        import pandas as pd
        rodape = pd.DataFrame([
            ['Atenção .: Total das vendas não esta considerando "produto genérico".', None, None],
            [None, "Valor total dos fretes ...:", 8.5],
        ])
        detectar = (rodape, etl_processor.ALIAS_COMPRAS, etl_processor._validar_compras)
        with self.assertRaises(etl_processor.ETLValidationError):
            etl_processor._detectar_cabecalho(*detectar)
        with mock.patch.object(etl_processor, "_titulos_plausiveis", return_value=True):
            _indice, mapa, linhas_cab = etl_processor._detectar_cabecalho(*detectar)
        self.assertEqual((linhas_cab, sorted(mapa)), (2, ["produto", "valor_nf"]))


if __name__ == "__main__":
    unittest.main()
