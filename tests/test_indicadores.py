"""Indicadores executivos (21) e as cinco refatorações que vieram com eles (raio-x-2026-11.6, triagem-2026-11.4).

  1. Leitura nova, linha a linha: número da venda, CPF/CNPJ do cliente (só contado, nunca guardado), desconto e
     hora, pelo título inteiro da coluna.
  2. Refatoração 1: o relatório de compras ou de entradas enviado como vendas é recusado com a mensagem fixa.
  3. Refatoração 2: o teto de relatórios mensais por envio é 120 (10 anos); as fronteiras estão em
     tests/test_cobertura_leiautes_erp.py (FatiasMensaisTests).
  4. Refatoração 3: na triagem do lead, o item de custo a confirmar sai da contagem e do valor de "venda
     abaixo do custo" (e do vazamento) e fica só como inconsistência de custo.
  5. Refatoração 4: o Raio-X do cliente agrupa a venda item a item por mês (AAAA-MM), como a avaliação do lead.
  6. Refatoração 5 e os indicadores: modo analítico (venda item a item) e acumulado (resumo por produto), com
     os valores conferidos à mão em dois envios fictícios; a margem negativa por produto e mês marca o 4º
     trimestre.
  7. Contrato (o processo pai confere o nó inteiro) e o que cada plano vê (raio_x_servico.visao).
  8. Lead: a coluna nova grava os indicadores, o expurgo de 30 dias os apaga e o downgrade recusa com dados.
  9. Cada achado das três revisões independentes (Docs/INDICADORES_EXECUTIVOS.md, seção 12).

Todos os relatórios são fictícios. O CPF e o CNPJ usados são números de exemplo com dígito verificador
válido, sem dono.
"""

from datetime import date, timedelta
from decimal import Decimal
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import indicadores_executivos
from indicadores_executivos import cliente_identificado, hora_da_celula, numero_da_venda
import etl_processor
import etl_runtime
from etl_inputs import ETLInputError
import raio_x
import raio_x_servico
import triagem_cenario
from tests.raio_x_support import avaliar_como_o_worker, executar_como_o_worker
from tests.test_triagem_cenario import _linha, cobertura

D = Decimal
MENSAGEM_DE_COMPRAS = ("Arquivo 1: o relatório enviado é de compras/entradas. Por favor, envie o relatório de "
                       "vendas por produto.")
CPF_DE_EXEMPLO = "529.982.247-25"
CNPJ_DE_EXEMPLO = "11.222.333/0001-81"


def _cpf_com_digitos(base: str) -> str:
    """Os 9 dígitos com os dois verificadores (conta independente da do módulo)."""
    digitos = [int(c) for c in base]
    for tamanho in (9, 10):
        soma = sum(d * peso for d, peso in zip(digitos, range(tamanho + 1, 1, -1)))
        resto = soma % 11
        digitos.append(0 if resto < 2 else 11 - resto)
    return "".join(map(str, digitos))


class _Arquivos(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="nexo-indicadores-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.numero = 0

    def csv(self, texto: str) -> str:
        self.numero += 1
        caminho = self.pasta / f"arquivo_{self.numero:02d}.csv"
        caminho.write_bytes(texto.encode("utf-8"))
        return str(caminho)

    def recusa(self, codigo, funcao, *argumentos, **nomeados):
        with self.assertRaises(ETLInputError) as contexto:
            funcao(*argumentos, **nomeados)
        self.assertEqual(contexto.exception.code, codigo)
        return contexto.exception.safe_message


# =============================================================================
# Envios fictícios
# =============================================================================
# Analítico: venda item a item de outubro e novembro de 2025, com número da venda (pedido e nota na mesma
# célula, como o ERP), CPF/CNPJ, hora e desconto. 06/10 e 03/11 são segundas; 11/10 e 08/11, sábados.
ANALITICO = (
    "Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 30/11/2025\n"
    "Data;Hora;Nr. Doc.;CPF/CNPJ;Produto;Qtde;Valor Total;Desconto\n"
    f"06/10/2025;09:15;P1-0001  NFC1-0001;{CPF_DE_EXEMPLO};CAFE 500G;1;20,00;2,00\n"
    f"06/10/2025;09:15;P1-0001  NFC1-0001;{CPF_DE_EXEMPLO};LEITE 1L;2;10,00;0,00\n"
    "11/10/2025;14:40;P1-0002;;CAFE 500G;2;40,00;0,00\n"
    f"03/11/2025;09:50;P1-0003  NFC1-0003;{CPF_DE_EXEMPLO};CAFE 500G;1;20,00;0,00\n"
    f"08/11/2025;16:05;P1-0004;{CNPJ_DE_EXEMPLO};LEITE 1L;3;15,00;1,00\n"
)


def _mensal(mes: int, linhas: list) -> str:
    fim = {10: 31, 11: 30, 12: 31}[mes]
    return (f"Relatorio de vendas por produto\nPeriodo: 01/{mes:02d}/2025 a {fim}/{mes:02d}/2025\n"
            "Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque\n" + "".join(l + "\n" for l in linhas))


# Acumulado: resumo por produto de outubro, novembro e dezembro de 2025.
#   - CAFE: custo unitário de 12,00 em outubro, 13,00 em novembro e 13,20 em dezembro; estoque 50, 40 e 30;
#   - ACUCAR: vendido abaixo do custo nos três meses (perda de 5,00 por mês) e estoque -2 em dezembro;
#   - ITEM GENERICO: custo de 500,00 para 10,00 vendidos (custo a confirmar), só em outubro;
#   - SABAO: vendeu só em outubro; em novembro e dezembro aparece com o estoque e quantidade zero;
#   - VELA: só o estoque, em dezembro (produto listado sem venda);
#   - LEITE: margem de 10%, abaixo do mínimo de 15%.
ACUMULADO = (
    _mensal(10, ["1;CAFE 500G;10;200,00;120,00;50", "2;ACUCAR 1KG;5;20,00;25,00;0", "3;ITEM GENERICO;1;10,00;500,00;5",
                 "4;SABAO PO;2;30,00;20,00;10", "6;LEITE 1L;10;50,00;45,00;20"]),
    _mensal(11, ["1;CAFE 500G;10;200,00;130,00;40", "2;ACUCAR 1KG;5;20,00;25,00;3", "4;SABAO PO;0;0,00;0,00;10",
                 "6;LEITE 1L;10;50,00;45,00;20"]),
    _mensal(12, ["1;CAFE 500G;10;200,00;132,00;30", "2;ACUCAR 1KG;5;20,00;25,00;-2", "4;SABAO PO;0;0,00;0,00;10",
                 "5;VELA;0;0,00;0,00;7", "6;LEITE 1L;10;50,00;45,00;20"]),
)


# =============================================================================
# 1. Leitura nova
# =============================================================================
class LeituraDasLinhasTests(_Arquivos):
    def test_numero_da_venda_e_o_primeiro_trecho_com_algarismo(self):
        casos = {"P9-00001  NFC9-00001": "P9-00001", "P9-00003 ": "P9-00003", "NF 123": "123", "nf-e 77": "77",
                 123.0: "123", "456.0": "456", "": None, "nan": None, None: None, float("nan"): None, "s/n": None}
        for valor, esperado in casos.items():
            with self.subTest(valor=valor):
                self.assertEqual(numero_da_venda(valor), esperado)

    def test_cliente_so_com_cpf_ou_cnpj_valido(self):
        cpf = cliente_identificado(CPF_DE_EXEMPLO)
        self.assertIsNotNone(cpf)
        # O mesmo CPF em outro formato é o mesmo cliente; o resumo não é o número.
        self.assertEqual(cliente_identificado("52998224725"), cpf)
        self.assertEqual(cliente_identificado(52998224725.0), cpf)
        self.assertNotIn("52998224725", cpf)
        self.assertIsNotNone(cliente_identificado(CNPJ_DE_EXEMPLO))
        self.assertNotEqual(cliente_identificado(CNPJ_DE_EXEMPLO), cpf)
        # A célula numérica do XLSX perde o zero à esquerda: volta a ter 11 dígitos.
        com_zero = _cpf_com_digitos("012345678")
        self.assertEqual(cliente_identificado(float(com_zero)), cliente_identificado(com_zero))
        for invalido in ("529.982.247-24", "000.000.000-00", "111.111.111-11", "CONSUMIDOR FINAL", "", None,
                         "11.222.333/0001-82", "123", 1.5, float("nan")):
            with self.subTest(invalido=invalido):
                self.assertIsNone(cliente_identificado(invalido))

    def test_hora_da_celula(self):
        casos = {"14:35": 14, "2025-10-03 14:35:00": 14, "03/10/2025 09:05": 9, 0.5: 12, 0.0: 0,
                 "25:00": None, "14h35": None, None: None, 1.5: None}
        for valor, esperado in casos.items():
            with self.subTest(valor=valor):
                self.assertEqual(hora_da_celula(valor), esperado)

    def test_colunas_da_venda_pelo_titulo_inteiro(self):
        cabecalho = ("Data/Hora;Nr. Doc.;CPF/CNPJ;CNPJ Loja;Produto;Qtde;Valor Total;Vlr. Desconto;Desc. %;Hora;"
                     "Venda\n")
        df = etl_processor._vendas_do_bruto(etl_processor._ler_bruto(self.csv(
            cabecalho + "06/10/2025 09:15;P1;52998224725;11222333000181;CAFE;1;20,00;1,00;5;09:15;20,00\n")),
            "arquivo 1", cadastro=True)
        colunas = set(df.columns)
        self.assertTrue({"_data", "_documento", "_cliente", "_desconto", "_hora"} <= colunas, colunas)
        self.assertEqual(df["_desconto"].tolist(), ["1,00"])
        self.assertEqual(df["_cliente"].tolist(), ["52998224725"])  # o da loja não é o do cliente
        # Sem o modo de cadastro (análise mensal), nada disso é lido.
        sem = etl_processor._vendas_do_bruto(etl_processor._ler_bruto(self.csv(
            cabecalho + "06/10/2025 09:15;P1;52998224725;11222333000181;CAFE;1;20,00;1,00;5;09:15;20,00\n")),
            "arquivo 1")
        self.assertFalse({"_data", "_documento", "_cliente", "_desconto", "_hora"} & set(sem.columns))

    def test_titulos_que_nao_sao_dado_da_venda(self):
        for titulo in ("Venda", "Desc.", "Desc. %", "Desc. Item", "Ticket", "CNPJ Fornecedor", "Data Validade"):
            with self.subTest(titulo=titulo):
                df = etl_processor._vendas_do_bruto(etl_processor._ler_bruto(self.csv(
                    f"Produto;Qtde;Valor Total;{titulo}\nCAFE;1;20,00;x\n")), "arquivo 1", cadastro=True)
                self.assertFalse({"_documento", "_cliente", "_desconto", "_hora", "_data"} & set(df.columns))


# =============================================================================
# 2. Refatoração 1: compras ou entradas na rota de vendas
# =============================================================================
class RelatorioDeComprasTests(_Arquivos):
    COMPRAS = ("Relatorio de Compras\nPeriodo: 01/10/2025 a 31/10/2025\nCodigo;Produto;Qtde;Valor Total\n"
               "1;CAFE 500G;10;100,00\n")
    ENTRADAS = ("Entradas de Mercadorias\nPeriodo: 01/10/2025 a 31/10/2025\nProduto;Qtde;Valor Total\n"
                "CAFE 500G;10;100,00\n")
    SO_TOTAL_DA_COMPRA = "Codigo;Produto;Qtde;Valor Total da Compra\n1;CAFE 500G;10;100,00\n"
    PRECO_DE_VENDA_E_TOTAL_DA_COMPRA = ("Periodo: 01/10/2025 a 31/10/2025\nProduto;Qtde;Preco Venda;Valor Total da Compra\n"
                                        "CAFE 500G;10;20,00;100,00\n")

    def test_raio_x_e_avaliacao_recusam_com_a_mensagem_fixa(self):
        for nome, texto in (("compras", self.COMPRAS), ("entradas", self.ENTRADAS),
                            ("so_total_da_compra", self.SO_TOTAL_DA_COMPRA),
                            ("preco_de_venda_e_total_da_compra", self.PRECO_DE_VENDA_E_TOTAL_DA_COMPRA)):
            with self.subTest(relatorio=nome):
                caminho = self.csv(texto)
                self.assertEqual(self.recusa("raio_x_dados", executar_como_o_worker, [caminho]), MENSAGEM_DE_COMPRAS)
                self.assertEqual(self.recusa("raio_x_dados", avaliar_como_o_worker, [caminho], hoje=date(2026, 11, 9)),
                                 MENSAGEM_DE_COMPRAS)

    def test_o_numero_do_arquivo_e_o_do_envio(self):
        venda = self.csv(_mensal(10, ["1;CAFE 500G;10;200,00;120,00;50"]))
        mensagem = self.recusa("raio_x_dados", executar_como_o_worker, [venda, self.csv(self.COMPRAS)])
        self.assertEqual(mensagem, MENSAGEM_DE_COMPRAS.replace("Arquivo 1", "Arquivo 2"))

    def test_analise_mensal_recusa_como_recusa_de_dados(self):
        with self.assertRaises(etl_processor.ETLValidationError) as contexto:
            etl_processor.carregar_vendas(self.csv(self.COMPRAS))
        self.assertIsInstance(contexto.exception, etl_processor.ETLRelatorioDeCompras)
        self.assertIn("relatório de COMPRAS", str(contexto.exception))

    def test_relatorios_de_venda_nao_sao_recusados(self):
        casos = {
            # O preço unitário de compra ao lado do total da venda (venda com custo).
            "unitario_de_compra": ("Relatorio de vendas\nPeriodo: 01/10/2025 a 31/10/2025\n"
                                   "Produto;Qtde;Valor Unitario Compra;Valor Total\nCAFE 500G;10;8,00;100,00\n"),
            # O topo fala de entradas e saídas (movimento), e as colunas são de venda.
            "entradas_e_saidas": ("Movimento de Entradas e Saidas\nPeriodo: 01/10/2025 a 31/10/2025\n"
                                  "Produto;Qtde;Valor Total\nCAFE 500G;10;100,00\n"),
            # O título de vendas com "custo" ("Giro de vendas ... Pr. Custo").
            "giro_de_vendas": ("Giro de vendas por Produto(s) Vendido(s) Abaixo do Pr. Custo\n"
                               "Periodo: 01/10/2025 a 31/10/2025\nProduto;Qtde;Valor Total\nCAFE 500G;10;100,00\n"),
            # Achados da revisão independente: palavra solta no nome da loja, num filtro ou no endereço não é
            # título de relatório de compras.
            "nome_da_loja": ("SUPERMERCADO BOA COMPRA LTDA\nCurva ABC de Produtos\nPeriodo: 01/10/2025 a 31/10/2025\n"
                             "Produto;Qtde;Valor Total\nCAFE 500G;10;100,00\n"),
            "filtro_comprador": ("Ranking de Produtos\nComprador: TODOS\nPeriodo: 01/10/2025 a 31/10/2025\n"
                                 "Produto;Qtde;Valor Total\nCAFE 500G;10;100,00\n"),
            "endereco": ("MERCADO CENTRAL - Av. Entrada da Cidade\nPeriodo: 01/10/2025 a 31/10/2025\n"
                         "Produto;Qtde;Valor Total\nCAFE 500G;10;100,00\n"),
            # "Vlr. Compra" e "Valor de Compra" ao lado do preço de venda podem ser o custo da unidade: a venda
            # (quantidade x preço de venda) não é recusada.
            "vlr_compra_e_preco_de_venda": "Produto;Qtde;Preco Venda;Vlr. Compra\nCAFE 500G;10;10,00;6,00\n",
            "valor_de_compra_e_preco_unitario": "Produto;Qtde;Preco Unitario;Valor de Compra\nCAFE 500G;10;10,00;6,00\n",
        }
        for nome, texto in casos.items():
            with self.subTest(relatorio=nome):
                self.assertEqual(executar_como_o_worker([self.csv(texto)])["saude"]["faturamento_total"], 100.0)

    def test_exportacao_de_compras_sem_titulo_pela_quantidade_comprada(self):
        texto = ("Fornecedor;Nota Fiscal;Data Entrada;Produto;Qtde Comprada;Custo Unitario;Valor Total\n"
                 "ALFA;123;01/10/2025;CAFE 500G;10;9,00;90,00\n")
        self.assertEqual(self.recusa("raio_x_dados", executar_como_o_worker, [self.csv(texto)]), MENSAGEM_DE_COMPRAS)
        # Com a quantidade vendida ao lado ("vendas x compras"), é relatório de venda.
        com_venda = "Produto;Qtde Vendida;Qtde Comprada;Valor Total\nCAFE 500G;10;12;100,00\n"
        self.assertEqual(executar_como_o_worker([self.csv(com_venda)])["saude"]["faturamento_total"], 100.0)

    def test_relatorio_sem_colunas_de_venda_e_sem_sinal_de_compras_segue_com_a_mensagem_de_sempre(self):
        mensagem = self.recusa("raio_x_dados", executar_como_o_worker, [self.csv("Produto;Fornecedor\nCAFE;ALFA\n")])
        self.assertIn("não foi possível identificar produto, quantidade e valor de venda", mensagem)


# =============================================================================
# 3. Refatoração 2: teto de 120 relatórios mensais
# =============================================================================
class TetoDeFatiasTests(_Arquivos):
    DEZ_ANOS = ("Relatorio de vendas item a item\nPeriodo: 01/01/2016 a 31/12/2025\nData;Produto;Qtde;Valor Total\n"
                "15/01/2016;PRODUTO A;1;10,00\n20/12/2025;PRODUTO A;2;20,00\n")
    DOIS_MESES = ("Relatorio de vendas item a item\nPeriodo: 01/01/2026 a 28/02/2026\nData;Produto;Qtde;Valor Total\n"
                  "15/01/2026;PRODUTO B;1;5,00\n20/02/2026;PRODUTO B;2;10,00\n")

    def test_teto_e_mensagem(self):
        self.assertEqual(raio_x.MAX_FATIAS, 120)
        self.assertIn("Envie no máximo 10 anos por vez", raio_x._MAIS_QUE_O_TETO_DE_FATIAS)

    def test_raio_x_do_cliente_nao_recusa_o_que_antes_aceitava(self):
        """No lead, 120 meses e mais um arquivo de dois meses passam do teto. O Raio-X do cliente sempre aceitou
        esses arquivos: o envio que passaria do teto é lido sem fatiar nenhum arquivo, como antes das fatias, e o
        resultado não depende da ordem dos arquivos."""
        dez_anos, dois_meses = self.csv(self.DEZ_ANOS), self.csv(self.DOIS_MESES)
        self.recusa("raio_x_arquivos", avaliar_como_o_worker, [dez_anos, dois_meses], hoje=date(2026, 11, 9))
        resultado = executar_como_o_worker([dez_anos, dois_meses])
        self.assertEqual([fatia["rotulo"] for fatia in resultado["fatias"]],
                         ["01/01/2016 a 31/12/2025", "01/01/2026 a 28/02/2026"])
        self.assertEqual(resultado["saude"]["faturamento_total"], 45.0)
        self.assertTrue(indicadores_executivos.valido(resultado["indicadores_executivos"]))
        invertido = executar_como_o_worker([dois_meses, dez_anos])
        for chave in ("saude", "scanner", "recuperavel", "periodo", "indicadores_executivos"):
            self.assertEqual(invertido[chave], resultado[chave], chave)
        self.assertEqual(sorted(f["rotulo"] for f in invertido["fatias"]), sorted(f["rotulo"] for f in resultado["fatias"]))

    UM_MES = ("Relatorio de vendas item a item\nPeriodo: 01/01/2026 a 31/01/2026\nData;Produto;Qtde;Valor Total\n"
              "15/01/2026;PRODUTO B;1;5,00\n20/01/2026;PRODUTO B;2;10,00\n")

    def test_cento_e_vinte_meses_e_um_arquivo_nao_fatiado_em_qualquer_ordem(self):
        """Achado 23, na borda (terceira revisão): o arquivo que não é fatiado conta como um relatório no teto. Com
        os 120 meses antes dele, o envio passava com 121 fatias; com ele antes, com 2."""
        dez_anos, um_mes = self.csv(self.DEZ_ANOS), self.csv(self.UM_MES)
        resultado = executar_como_o_worker([dez_anos, um_mes])
        invertido = executar_como_o_worker([um_mes, dez_anos])
        self.assertEqual(len(resultado["fatias"]), 2)
        for chave in ("saude", "scanner", "recuperavel", "periodo", "indicadores_executivos"):
            self.assertEqual(invertido[chave], resultado[chave], chave)
        self.assertEqual(sorted(f["rotulo"] for f in invertido["fatias"]), sorted(f["rotulo"] for f in resultado["fatias"]))
        # No lead, 121 relatórios passam do teto nas duas ordens.
        for ordem in ([dez_anos, um_mes], [um_mes, dez_anos]):
            with self.subTest(primeiro=ordem[0]):
                self.recusa("raio_x_arquivos", avaliar_como_o_worker, ordem, hoje=date(2026, 11, 9))


# =============================================================================
# 4. Refatoração 3: custo a confirmar fora da venda abaixo do custo na triagem do lead
# =============================================================================
class TriagemSemCustoAConfirmarTests(unittest.TestCase):
    def setUp(self):
        # I: custo de 400 para 100 vendidos (custo a confirmar), com 5 de variação de preço; B: abaixo do custo.
        self.implausivel = _linha("I", 2025, venda="100.00", custo="400.00", perda="300.00", vazamento="5.00",
                                  recuperavel="300.00")
        self.abaixo = _linha("B", 2025, venda="90.00", custo="100.00", perda="10.00", recuperavel="10.00")
        self.linhas = [self.implausivel, self.abaixo, _linha("N", 2025)]
        self.triagem = triagem_cenario.triar(self.linhas, cobertura(a2025=(12, 365, True)))

    def test_contagem_e_motivo_so_do_item_plausivel(self):
        self.assertEqual(self.triagem.itens_venda_abaixo_custo, 1)
        self.assertEqual(self.triagem.a_confirmar, frozenset({"I"}))
        self.assertIn("custo_implausivel", self.triagem.motivos["I"])
        self.assertNotIn("abaixo_do_custo", self.triagem.motivos["I"])
        self.assertIn("abaixo_do_custo", self.triagem.motivos["B"])

    def test_marcas_e_vazamento_do_item_a_confirmar(self):
        marcas = triagem_cenario.marcas(self.implausivel, a_confirmar=True)
        self.assertFalse(marcas["flag_venda_abaixo_custo"])
        self.assertEqual(marcas["vazamento_estimado_reais"], D("5.00"))  # só a variação de preço
        # Sem o parâmetro, vale a própria linha (custo de 4 vezes a venda).
        self.assertEqual(triagem_cenario.marcas(self.implausivel), marcas)
        normal = triagem_cenario.marcas(self.abaixo, a_confirmar=False)
        self.assertTrue(normal["flag_venda_abaixo_custo"])
        self.assertEqual(normal["vazamento_estimado_reais"], D("10.00"))

    def test_recuperavel_do_bloco_sem_a_perda_contra_custo_implausivel(self):
        # Atenção: B (abaixo do custo, 10,00) e I (custo implausível, só os 5,00 de variação de preço).
        self.assertEqual(self.triagem.blocos["I"], "atencao")
        self.assertEqual(D(self.triagem.resumos["atencao"]["recuperavel"]), D("15.00"))

    def test_matriz_do_consultor_isola_o_custo_a_confirmar(self):
        import diagnostico_cenario
        from tests.test_triagem_cenario import meta
        dados = meta({2025: (12, 365, True)})
        base = triagem_cenario.Cobertura.do_envio(dados)
        triagem = triagem_cenario.triar(self.linhas, base)
        totais = diagnostico_cenario.matriz_por_categoria(self.linhas, base, triagem, dados)["totais"]
        self.assertEqual(totais["marcas"]["abaixo_do_custo"], 1)
        self.assertEqual(totais["vendas_abaixo_do_custo"], 10.0)
        self.assertEqual(totais["potencial_recuperavel"], 15.0)
        self.assertEqual(totais["a_confirmar"]["itens"], 1)

    def test_limiares_gravados_pela_versao_anterior_continuam_valendo(self):
        self.assertEqual(triagem_cenario.VERSAO_TRIAGEM, "triagem-2026-11.4")
        dados = {**triagem_cenario.LIMIARES_PADRAO.para_dict(), "versao": "triagem-2026-11.3", "margem_baixa": "0.08"}
        self.assertEqual(triagem_cenario.Limiares.de_dict(dados).margem_baixa, D("0.08"))
        with self.assertRaises(triagem_cenario.LimiaresDeOutraVersao):
            triagem_cenario.Limiares.de_dict({**dados, "versao": "triagem-2026-11.2"})


# =============================================================================
# 5 e 6. Indicadores nos dois modos
# =============================================================================
class ModoAnaliticoTests(_Arquivos):
    def setUp(self):
        super().setUp()
        self.resultado = executar_como_o_worker([self.csv(ANALITICO)])
        self.no = self.resultado["indicadores_executivos"]
        self.ind = self.no["indicadores"]

    def test_vinte_e_um_indicadores_no_contrato(self):
        self.assertEqual(tuple(self.ind), indicadores_executivos.CHAVES)
        self.assertEqual(len(self.ind), 21)
        self.assertTrue(indicadores_executivos.valido(self.no))
        self.assertEqual(self.no["modo"], "analitico")

    def test_raio_x_do_cliente_agrupa_por_mes(self):
        # Refatoração 4: o arquivo de dois meses vira 10/2025 e 11/2025, como na avaliação do lead.
        self.assertEqual([fatia["rotulo"] for fatia in self.resultado["fatias"]], ["10/2025", "11/2025"])
        self.assertEqual([fatia["faturamento"] for fatia in self.resultado["fatias"]], [70.0, 35.0])

    def test_faturamento_no_centavo(self):
        self.assertEqual(self.ind["faturamento"]["valor"], 105.0)
        self.assertEqual(self.ind["faturamento"]["valor"], self.resultado["saude"]["faturamento_total"])

    def test_ticket_e_itens_por_venda_pelo_numero_da_venda(self):
        # Quatro pedidos (P1-0001 tem duas linhas; a nota na mesma célula não vira outra venda).
        self.assertEqual(self.ind["ticket_medio"], {"status": "calculado", "valor": 26.25, "transacoes": 4})
        self.assertEqual(self.ind["itens_por_venda"],
                         {"status": "calculado", "valor": 2.25, "linhas_por_venda": 1.25, "transacoes": 4})

    def test_recompra_conta_clientes_sem_guardar_o_documento(self):
        self.assertEqual(self.ind["recompra"], {"status": "calculado", "taxa": 0.5, "clientes_identificados": 2,
                                                "clientes_recorrentes": 1, "pct_linhas_identificadas": 0.8})
        texto = json.dumps(self.resultado)
        for documento in ("52998224725", "529.982.247-25", "11222333000181", cliente_identificado(CPF_DE_EXEMPLO)):
            self.assertNotIn(documento, texto)

    def test_indice_de_descontos(self):
        self.assertEqual(self.ind["indice_descontos"],
                         {"status": "calculado", "valor": 0.0278, "descontos": 3.0, "faturamento_bruto": 108.0})

    def test_mapa_de_calor_por_dia_da_semana_e_hora(self):
        calor = self.ind["mapa_calor"]
        self.assertEqual((calor["status"], calor["tipo"], calor["hora_disponivel"]), ("calculado", "dia_semana_hora", True))
        self.assertEqual(calor["celulas"], [
            {"dia_semana": 0, "hora": 9, "faturamento": 50.0, "vendas": 2},
            {"dia_semana": 5, "hora": 14, "faturamento": 40.0, "vendas": 1},
            {"dia_semana": 5, "hora": 16, "faturamento": 15.0, "vendas": 1},
        ])

    def test_sem_custo_e_sem_estoque_os_indicadores_dizem_o_que_falta(self):
        for chave in ("margem", "cmv", "curva_abc_lucro", "margem_negativa", "margem_abaixo_minimo", "evolucao_custo"):
            self.assertEqual(self.ind[chave]["status"], "requer_custo", chave)
        for chave in ("giro_estoque", "ruptura", "encalhados", "cobertura_estoque", "gmroi"):
            self.assertEqual(self.ind[chave]["status"], "requer_estoque", chave)
        self.assertEqual(self.ind["saving"], {"status": "requer_relatorio_de_compras", "valor": None})
        self.assertEqual(self.ind["margem"]["liquida_status"], "requer_parametro")

    def test_curvas_abc_de_faturamento_e_volume(self):
        faturamento = self.ind["curva_abc_faturamento"]
        self.assertEqual([(c["classe"], c["itens"], c["valor"]) for c in faturamento["classes"]],
                         [("A", 1, 80.0), ("B", 0, 0.0), ("C", 1, 25.0)])
        self.assertEqual([i["produto"] for i in faturamento["itens"]], ["CAFE 500G", "LEITE 1L"])
        volume = self.ind["curva_abc_volume"]
        self.assertEqual([(i["produto"], i["classe"], i["valor"]) for i in volume["itens"]],
                         [("LEITE 1L", "A", 5.0), ("CAFE 500G", "C", 4.0)])

    def test_mix_sem_produto_listado_sem_venda(self):
        self.assertEqual(self.ind["mix"], {"status": "calculado", "skus_vendidos": 2, "skus_cadastrados": None,
                                           "percentual_sobre_cadastrados": None,
                                           "cadastrados_status": "requer_cadastro_de_produtos"})

    def test_sem_numero_da_venda_os_indicadores_por_venda_pedem_item_a_item(self):
        sem_documento = ANALITICO.replace("Nr. Doc.", "Observacao")
        ind = executar_como_o_worker([self.csv(sem_documento)])["indicadores_executivos"]["indicadores"]
        self.assertEqual(ind["ticket_medio"]["status"], "requer_item_a_item")
        self.assertEqual(ind["itens_por_venda"]["status"], "requer_item_a_item")
        # A recompra, sem o número, conta o dia como transação: o CPF comprou em dois dias.
        self.assertEqual((ind["recompra"]["status"], ind["recompra"]["clientes_recorrentes"]), ("calculado", 1))
        self.assertEqual(ind["mapa_calor"]["celulas"][0]["vendas"], None)


class ModoAcumuladoTests(_Arquivos):
    def setUp(self):
        super().setUp()
        self.caminhos = [self.csv(texto) for texto in ACUMULADO]
        self.resultado = executar_como_o_worker(self.caminhos)
        self.no = self.resultado["indicadores_executivos"]
        self.ind = self.no["indicadores"]

    def test_modo_e_indicadores_por_venda(self):
        self.assertTrue(indicadores_executivos.valido(self.no))
        self.assertEqual(self.no["modo"], "acumulado")
        for chave in ("ticket_medio", "itens_por_venda", "recompra"):
            self.assertEqual(self.ind[chave]["status"], "requer_item_a_item", chave)
        self.assertEqual(self.ind["indice_descontos"]["status"], "requer_coluna_de_desconto")

    def test_resumo_com_coluna_de_pedido_nao_vira_venda_item_a_item(self):
        """No resumo por produto, a coluna "Pedido" (o último pedido, um número qualquer) não é a venda: sem o
        modo analítico, ticket, itens por venda e recompra não saem, mesmo com o número e o CPF em todas as linhas."""
        resumo = ("Relatorio de vendas por produto\nPeriodo: 01/10/2025 a 31/10/2025\n"
                  "Pedido;CPF/CNPJ;Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque\n"
                  f"P1;{CPF_DE_EXEMPLO};1;CAFE 500G;10;200,00;120,00;50\n"
                  f"P2;{CPF_DE_EXEMPLO};6;LEITE 1L;10;50,00;45,00;20\n")
        no = executar_como_o_worker([self.csv(resumo)])["indicadores_executivos"]
        self.assertEqual(no["modo"], "acumulado")
        for chave in ("ticket_medio", "itens_por_venda", "recompra"):
            self.assertEqual(no["indicadores"][chave]["status"], "requer_item_a_item", chave)

    def test_faturamento_margem_e_cmv_sem_o_custo_a_confirmar(self):
        self.assertEqual(self.ind["faturamento"]["valor"], 850.0)
        self.assertEqual(self.ind["faturamento"]["valor"], self.resultado["saude"]["faturamento_total"])
        margem = self.ind["margem"]
        # Sem o ITEM GENERICO: 840 de venda com custo e 612 de custo.
        self.assertEqual((margem["bruta_valor"], margem["bruta_percentual"], margem["faturamento_com_custo"]),
                         (228.0, 0.2714, 840.0))
        self.assertEqual(margem["itens_custo_a_confirmar"], 1)
        self.assertEqual(self.ind["cmv"], {"status": "calculado", "valor": 612.0, "custo_a_confirmar": 500.0,
                                           "itens_custo_a_confirmar": 1})
        # A auditoria do Raio-X continua com o custo total de sempre (com o item a confirmar).
        self.assertEqual(self.resultado["auditoria"]["custo_total"], 1112.0)

    def test_giro_e_gmroi(self):
        giro = self.ind["giro_estoque"]
        # Estoque de dezembro a custo médio: CAFE 30 x 12,7333 + SABAO 10 x 10 + LEITE 20 x 4,50 = 572.
        self.assertEqual((giro["status"], giro["cmv"], giro["estoque_a_custo"], giro["dias_do_periodo"]),
                         ("calculado", 612.0, 572.0, 92))
        self.assertEqual(giro["valor"], round(612 / 572, 4))
        self.assertEqual(giro["anualizado"], round(612 / 572 * 365 / 92, 4))
        self.assertEqual(giro["itens_com_estoque_sem_custo"], 2)  # ITEM GENERICO e VELA
        gmroi = self.ind["gmroi"]
        # Estoque médio das três fotos (826,67, 714,33 e 572,00).
        self.assertEqual((gmroi["base"], gmroi["fotos_de_estoque"], gmroi["estoque_medio_a_custo"]),
                         ("estoque_medio", 3, 704.33))
        self.assertEqual((gmroi["valor"], gmroi["percentual"]), (round(228 / (2113 / 3), 4), round(228 / (2113 / 3) * 100, 2)))

    def test_ruptura_encalhados_e_cobertura(self):
        ruptura = self.ind["ruptura"]
        self.assertEqual((ruptura["itens"], ruptura["faturamento"]), (1, 60.0))
        self.assertEqual(ruptura["lista"], [{"produto": "ACUCAR 1KG", "codigo": "2", "estoque": -2.0,
                                             "faturamento": 60.0, "quantidade": 15.0}])
        encalhados = self.ind["encalhados"]
        # SABAO e ITEM GENERICO venderam em outubro (61 dias até 31/12); VELA não vendeu no período (92 dias).
        self.assertEqual((encalhados["itens"], encalhados["valor_a_custo"], encalhados["itens_sem_custo"]), (3, 100.0, 2))
        self.assertEqual((encalhados["data_de_referencia"], encalhados["base_dos_dias"]), ("2025-12-31", "fim_do_relatorio"))
        # Do mais parado para o menos (e, no empate, pelo estoque): a ordem não depende do custo.
        self.assertEqual([(i["produto"], i["dias_sem_venda"], i["sem_venda_no_periodo"], i["valor_a_custo"])
                          for i in encalhados["lista"]],
                         [("VELA", 92, True, None), ("SABAO PO", 61, False, 100.0), ("ITEM GENERICO", 61, False, None)])
        cobertura_ = self.ind["cobertura_estoque"]
        self.assertEqual((cobertura_["itens"], cobertura_["mediana_dias"], cobertura_["faixas"]),
                         (4, 276.0, {"ate_15": 0, "de_16_a_60": 0, "de_61_a_180": 2, "acima_de_180": 2}))

    def test_curvas_abc(self):
        faturamento = self.ind["curva_abc_faturamento"]
        self.assertEqual([(c["classe"], c["itens"], c["valor"], c["participacao"]) for c in faturamento["classes"]],
                         [("A", 1, 600.0, 0.7059), ("B", 1, 150.0, 0.1765), ("C", 3, 100.0, 0.1176)])
        volume = self.ind["curva_abc_volume"]
        self.assertEqual([(c["classe"], c["itens"]) for c in volume["classes"]], [("A", 2), ("B", 0), ("C", 3)])
        lucro = self.ind["curva_abc_lucro"]
        # Margem: CAFE 218, LEITE 15, SABAO 10; ACUCAR (negativa) e o ITEM GENERICO ficam fora da curva.
        self.assertEqual([(i["produto"], i["classe"], i["valor"]) for i in lucro["itens"]],
                         [("CAFE 500G", "A", 218.0), ("LEITE 1L", "C", 15.0), ("SABAO PO", "C", 10.0)])
        self.assertEqual(lucro["itens_fora_da_curva"], 2)

    def test_margem_negativa_por_mes_com_o_quarto_trimestre(self):
        negativa = self.ind["margem_negativa"]
        self.assertEqual((negativa["itens"], negativa["perda"], negativa["perda_q4"]), (1, 15.0, 15.0))
        self.assertEqual((negativa["itens_custo_a_confirmar"], negativa["faturamento_custo_a_confirmar"]), (1, 10.0))
        self.assertEqual(negativa["por_mes"], [
            {"mes": "2025-10", "impacto_q4": True, "itens": 1, "perda": 5.0},
            {"mes": "2025-11", "impacto_q4": True, "itens": 1, "perda": 5.0},
            {"mes": "2025-12", "impacto_q4": True, "itens": 1, "perda": 5.0},
        ])
        self.assertEqual(negativa["matriz"], [{"produto": "ACUCAR 1KG", "codigo": "2", "perda_total": 15.0, "meses": [
            {"mes": "2025-10", "perda": 5.0, "impacto_q4": True}, {"mes": "2025-11", "perda": 5.0, "impacto_q4": True},
            {"mes": "2025-12", "perda": 5.0, "impacto_q4": True}]}])
        # O ITEM GENERICO (custo 50 vezes a venda) não é margem negativa: é erro de cadastro.
        self.assertNotIn("ITEM GENERICO", json.dumps(negativa["lista"]))

    def test_quarto_trimestre_so_nos_meses_10_11_e_12(self):
        setembro = ("Relatorio de vendas por produto\nPeriodo: 01/09/2025 a 30/09/2025\n"
                    "Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque\n2;ACUCAR 1KG;5;20,00;25,00;5\n")
        ind = executar_como_o_worker([self.csv(setembro), self.caminhos[0]])["indicadores_executivos"]["indicadores"]
        self.assertEqual([(m["mes"], m["impacto_q4"]) for m in ind["margem_negativa"]["por_mes"]],
                         [("2025-09", False), ("2025-10", True)])
        self.assertEqual(ind["margem_negativa"]["perda_q4"], 5.0)
        self.assertEqual(ind["mapa_calor"]["impacto_q4"], [False, True])

    def test_margem_abaixo_do_minimo(self):
        abaixo = self.ind["margem_abaixo_minimo"]
        self.assertEqual((abaixo["itens"], abaixo["faturamento_com_custo"], abaixo["margem_minima"]), (1, 150.0, 0.15))
        self.assertEqual(abaixo["lista"], [{"produto": "LEITE 1L", "codigo": "6", "faturamento_com_custo": 150.0,
                                            "margem_percentual": 0.1}])

    def test_evolucao_do_custo_unitario_entre_os_meses(self):
        evolucao = self.ind["evolucao_custo"]
        self.assertEqual((evolucao["meses_comparados"], evolucao["itens_comparados"], evolucao["itens_em_alta"],
                          evolucao["itens_em_queda"], evolucao["variacao_ponderada"]), (3, 3, 1, 0, 0.0632))
        self.assertEqual(evolucao["lista"][0], {"produto": "CAFE 500G", "codigo": "1", "mes_inicial": "2025-10",
                                                "mes_final": "2025-12", "custo_unitario_inicial": 12.0,
                                                "custo_unitario_final": 13.2, "variacao": 0.1, "impacto": 12.0})

    def test_mapa_por_produto_e_mes(self):
        calor = self.ind["mapa_calor"]
        self.assertEqual((calor["tipo"], calor["meses"], calor["impacto_q4"]),
                         ("sku_mes", ["2025-10", "2025-11", "2025-12"], [True, True, True]))
        self.assertEqual([(l["produto"], l["valores"]) for l in calor["linhas"]], [
            ("CAFE 500G", [200.0, 200.0, 200.0]), ("LEITE 1L", [50.0, 50.0, 50.0]), ("ACUCAR 1KG", [20.0, 20.0, 20.0]),
            ("SABAO PO", [30.0, 0.0, 0.0]), ("ITEM GENERICO", [10.0, 0.0, 0.0])])

    def test_mix_sobre_os_produtos_cadastrados(self):
        # Cinco vendidos e a VELA, listada só com o estoque.
        self.assertEqual(self.ind["mix"], {"status": "calculado", "skus_vendidos": 5, "skus_cadastrados": 6,
                                           "percentual_sobre_cadastrados": 0.8333, "cadastrados_status": "calculado"})

    def test_o_lead_recebe_o_mesmo_no(self):
        do_lead = avaliar_como_o_worker(self.caminhos, hoje=date(2026, 11, 9))["raio_x"]["indicadores_executivos"]
        self.assertEqual(do_lead, self.no)


# =============================================================================
# 7. Contrato e planos
# =============================================================================
class ContratoTests(_Arquivos):
    def setUp(self):
        super().setUp()
        self.resultado = executar_como_o_worker([self.csv(texto) for texto in ACUMULADO])
        self.no = self.resultado["indicadores_executivos"]

    def test_no_adulterado_e_recusado(self):
        def adulterado(mudar):
            copia = copy.deepcopy(self.no)
            mudar(copia)
            return copia
        casos = {
            "chave_a_mais": lambda n: n.update({"extra": 1}),
            "indicador_a_menos": lambda n: n["indicadores"].pop("gmroi"),
            "status_desconhecido": lambda n: n["indicadores"]["mix"].update({"status": "ok"}),
            "versao": lambda n: n.update({"versao": "indicadores-0"}),
            "texto_livre_em_rotulo": lambda n: n["indicadores"]["mapa_calor"].update({"tipo": "qualquer texto"}),
            "controle_no_nome": lambda n: n["indicadores"]["ruptura"]["lista"][0].update({"produto": "A\x07B"}),
            "nome_longo": lambda n: n["indicadores"]["ruptura"]["lista"][0].update({"produto": "A" * 121}),
            "numero_infinito": lambda n: n["indicadores"]["cmv"].update({"valor": float("inf")}),
            "lista_enorme": lambda n: n["indicadores"]["mapa_calor"].update({"meses": ["2025-01"] * 3001}),
            "chave_estranha": lambda n: n["indicadores"]["cmv"].update({"Chave Estranha": 1}),
        }
        self.assertTrue(indicadores_executivos.valido(self.no))
        for nome, mudar in casos.items():
            with self.subTest(caso=nome):
                self.assertFalse(indicadores_executivos.valido(adulterado(mudar)))

    def test_processo_pai_recusa_o_raio_x_com_o_no_adulterado(self):
        adulterado = copy.deepcopy(self.resultado)
        adulterado["indicadores_executivos"]["indicadores"]["mix"]["status"] = "ok"
        for resultado in (adulterado, {k: v for k, v in self.resultado.items() if k != "indicadores_executivos"}):
            with patch.object(etl_runtime, "_run_job", return_value=resultado), self.assertRaises(ETLInputError) as erro:
                etl_runtime.run_raio_x(["arquivo.csv"])
            self.assertEqual(erro.exception.code, "worker")
        with patch.object(etl_runtime, "_run_job", return_value=self.resultado):
            self.assertEqual(etl_runtime.run_raio_x(["arquivo.csv"]), self.resultado)

    def test_processo_pai_recusa_a_avaliacao_com_o_no_adulterado(self):
        avaliacao = avaliar_como_o_worker([self.csv(texto) for texto in ACUMULADO], hoje=date(2026, 11, 9))
        avaliacao["raio_x"]["indicadores_executivos"]["indicadores"]["ruptura"]["lista"][0]["produto"] = "A\x00B"
        with self.assertRaises(ETLInputError) as erro:
            etl_runtime.validar_avaliacao(avaliacao)
        self.assertEqual(erro.exception.code, "worker")


BRONZE = frozenset({"raio_x_entrada", "curva_abc_vendas", "produtos_parados_ruptura", "checklist_acoes"})
PRATA = BRONZE | {"auditoria_abaixo_custo", "margem_bruta", "diagnostico_cmv", "alerta_precificacao"}


class PlanosTests(_Arquivos):
    def setUp(self):
        super().setUp()
        self.resultado = executar_como_o_worker([self.csv(texto) for texto in ACUMULADO])

    def test_bronze_ve_venda_volume_e_estoque_em_unidades(self):
        ind = raio_x_servico.visao(self.resultado, BRONZE, None)["indicadores_executivos"]["indicadores"]
        for chave in ("faturamento", "mix", "curva_abc_faturamento", "curva_abc_volume", "ruptura", "cobertura_estoque",
                      "encalhados", "mapa_calor"):
            self.assertEqual(ind[chave]["status"], "calculado", chave)
        for chave, funcionalidade in (("margem", "margem_bruta"), ("cmv", "diagnostico_cmv"),
                                      ("giro_estoque", "diagnostico_cmv"), ("curva_abc_lucro", "margem_bruta"),
                                      ("gmroi", "margem_bruta"), ("evolucao_custo", "alerta_precificacao"),
                                      ("margem_abaixo_minimo", "margem_bruta"), ("recompra", "curva_abc_clientes")):
            self.assertEqual(ind[chave], {"status": "protegido", "funcionalidade": funcionalidade}, chave)
        # As contagens de margem negativa e de custo a confirmar ficam, como na auditoria.
        self.assertEqual(ind["margem_negativa"], {"itens": 1, "itens_custo_a_confirmar": 1, "status": "protegido",
                                                  "funcionalidade": "auditoria_abaixo_custo"})
        # O estoque parado em unidades é do Bronze; o valor a custo, do diagnóstico de CMV.
        self.assertIsNone(ind["encalhados"]["valor_a_custo"])
        self.assertTrue(all(item["valor_a_custo"] is None for item in ind["encalhados"]["lista"]))
        self.assertNotIn("612.0", json.dumps(ind))

    def test_prata_ve_tudo_menos_a_recompra(self):
        ind = raio_x_servico.visao(self.resultado, PRATA, None)["indicadores_executivos"]["indicadores"]
        self.assertEqual(ind["margem"]["bruta_valor"], 228.0)
        self.assertEqual(ind["encalhados"]["valor_a_custo"], 100.0)
        self.assertEqual(ind["recompra"]["status"], "protegido")
        self.assertEqual(ind["margem_negativa"]["perda"], 15.0)

    def test_corte_do_historico_nos_meses(self):
        ind = raio_x_servico.visao(self.resultado, PRATA, 1)["indicadores_executivos"]["indicadores"]
        negativa = ind["margem_negativa"]
        self.assertEqual([m["mes"] for m in negativa["por_mes"]], ["2025-12"])
        self.assertEqual([(l["perda_total"], [m["mes"] for m in l["meses"]]) for l in negativa["matriz"]],
                         [(5.0, ["2025-12"])])
        self.assertEqual(negativa["perda_q4"], 5.0)
        calor = ind["mapa_calor"]
        self.assertEqual((calor["meses"], calor["impacto_q4"]), (["2025-12"], [True]))
        self.assertEqual(calor["linhas"][0]["valores"], [200.0])

    def test_raio_x_gravado_antes_continua_valendo(self):
        antigo = {k: v for k, v in self.resultado.items() if k != "indicadores_executivos"}
        self.assertTrue(raio_x.CHAVES_OBRIGATORIAS <= set(antigo))
        self.assertNotIn("indicadores_executivos", raio_x.CHAVES_OBRIGATORIAS)
        self.assertIsNone(raio_x_servico.visao(antigo, PRATA, None)["indicadores_executivos"])

    def test_todo_indicador_tem_funcionalidade_definida(self):
        self.assertEqual(set(raio_x_servico.FUNCIONALIDADE_DO_INDICADOR), set(indicadores_executivos.CHAVES))


# =============================================================================
# 7b. Achados da revisão independente
# =============================================================================
class AchadosDaRevisaoTests(_Arquivos):
    def _ind(self, *textos):
        return executar_como_o_worker([self.csv(texto) for texto in textos])["indicadores_executivos"]["indicadores"]

    def test_foto_de_estoque_do_arquivo_fatiado_junta_os_meses(self):
        # A vendeu só em setembro (estoque 99, custo 6), B só em outubro (estoque 9, custo 10): 684 a custo.
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/09/2025 a 31/10/2025\n"
                 "Data;Produto;Qtde;Valor Total;Total Custo;Estoque\n"
                 "05/09/2025;PRODUTO A;1;10,00;6,00;99\n10/09/2025;PRODUTO A;1;10,00;6,00;99\n"
                 "05/10/2025;PRODUTO B;1;20,00;10,00;9\n12/10/2025;PRODUTO B;1;20,00;10,00;9\n")
        ind = self._ind(texto)
        self.assertEqual(ind["giro_estoque"]["estoque_a_custo"], 684.0)
        gmroi = ind["gmroi"]
        # Duas fotos (uma por mês, acumulando), mas a de setembro não traz B, que vendeu só em outubro: vale o
        # estoque final.
        self.assertEqual((gmroi["estoque_medio_a_custo"], gmroi["fotos_de_estoque"], gmroi["base"]),
                         (684.0, 2, "estoque_final"))
        self.assertEqual(gmroi["valor"], round(28 / 684, 4))

    def test_data_do_resumo_por_produto_nao_e_a_data_da_venda(self):
        # "Data" num resumo por produto é o cadastro: os dois produtos venderam em outubro e não estão parados.
        texto = ("Relatorio de vendas por produto\nPeriodo: 01/10/2025 a 31/10/2025\n"
                 "Data;Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque\n"
                 "15/03/2019;1;CAFE 500G;10;200,00;120,00;5\n02/07/2020;2;LEITE 1L;10;50,00;45,00;8\n")
        ind = self._ind(texto)
        self.assertEqual((ind["encalhados"]["itens"], ind["encalhados"]["base_dos_dias"]), (0, "fim_do_relatorio"))
        self.assertNotEqual(ind["mapa_calor"]["tipo"], "dia_semana")

    def test_numero_da_venda_vale_dentro_da_loja_e_do_arquivo(self):
        # Cada loja numera as suas vendas: 4 vendas, R$ 55,00.
        lojas = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                 "Data;Loja;Nr. Doc.;Produto;Qtde;Valor Total\n"
                 "06/10/2025;LOJA 1;1;CAFE 500G;1;10,00\n06/10/2025;LOJA 2;1;CAFE 500G;1;20,00\n"
                 "07/10/2025;LOJA 1;2;CAFE 500G;1;10,00\n07/10/2025;LOJA 2;2;CAFE 500G;1;15,00\n")
        ind = self._ind(lojas)
        self.assertEqual((ind["ticket_medio"]["transacoes"], ind["ticket_medio"]["valor"]), (4, 13.75))
        self.assertEqual(ind["itens_por_venda"]["valor"], 1.0)
        # Dois arquivos com a mesma numeração também são vendas diferentes.
        def mes(numero):
            return (f"Relatorio de vendas item a item\nPeriodo: 01/{numero}/2025 a 28/{numero}/2025\n"
                    "Data;Nr. Doc.;Produto;Qtde;Valor Total\n"
                    f"05/{numero}/2025;1;CAFE 500G;1;10,00\n06/{numero}/2025;2;CAFE 500G;1;10,00\n")
        ind = self._ind(mes("09"), mes("10"))
        self.assertEqual(ind["ticket_medio"]["transacoes"], 4)

    def test_perda_mes_a_mes_e_do_envio_tem_nomes_diferentes(self):
        setembro = _mensal(10, ["1;CAFE 500G;10;300,00;100,00;5"]).replace("01/10/2025 a 31/10/2025",
                                                                            "01/09/2025 a 30/09/2025")
        outubro = _mensal(10, ["1;CAFE 500G;10;100,00;150,00;5"])
        negativa = self._ind(setembro, outubro)["margem_negativa"]
        # No envio o CAFE não tem margem negativa (400 de venda e 250 de custo); em outubro, sim (perda de 50).
        self.assertEqual((negativa["itens"], negativa["perda"]), (0, 0.0))
        self.assertEqual((negativa["perda_nos_meses"], negativa["perda_q4"]), (50.0, 50.0))

    def test_queda_de_cinco_por_cento_exatos_conta(self):
        outubro = _mensal(10, ["1;CAFE 500G;10;200,00;120,00;5"])
        novembro = _mensal(11, ["1;CAFE 500G;10;200,00;114,00;5"])
        evolucao = self._ind(outubro, novembro)["evolucao_custo"]
        self.assertEqual((evolucao["itens_em_queda"], evolucao["lista"][0]["variacao"]), (1, -0.05))

    def test_venda_em_relatorio_sem_periodo_nao_vira_encalhe(self):
        meses = [_mensal(10, ["2;LEITE 1L;5;25,00;20,00;8"]).replace("01/10/2025 a 31/10/2025", periodo)
                 for periodo in ("01/08/2025 a 31/08/2025", "01/09/2025 a 30/09/2025")]
        meses.append(_mensal(10, ["2;LEITE 1L;5;25,00;20,00;8", "9;PRODUTO X;0;0,00;0,00;10"]))
        sem_periodo = "Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque\n9;PRODUTO X;2;40,00;20,00;10\n"
        encalhados = self._ind(*meses, sem_periodo)["encalhados"]
        self.assertNotIn("PRODUTO X", json.dumps(encalhados))

    def test_coluna_de_custo_em_branco_e_sem_dados_e_nao_falta_de_coluna(self):
        texto = ("Relatorio de vendas por produto\nPeriodo: 01/10/2025 a 31/10/2025\n"
                 "Codigo;Produto;Qtde;Valor Total;Total Custo\n1;CAFE 500G;10;200,00;\n")
        ind = self._ind(texto)
        for chave in ("margem", "cmv", "curva_abc_lucro", "margem_negativa", "margem_abaixo_minimo"):
            self.assertEqual(ind[chave]["status"], "sem_dados", chave)
        self.assertEqual(ind["evolucao_custo"]["status"], "requer_mais_de_um_mes")
        sem_coluna = self._ind("Relatorio\nPeriodo: 01/10/2025 a 31/10/2025\nProduto;Qtde;Valor Total\nCAFE;1;10,00\n")
        self.assertEqual(sem_coluna["margem"]["status"], "requer_custo")

    def test_corte_do_historico_reordena_e_tira_o_que_ficou_vazio(self):
        novembro = _mensal(11, ["1;PRODUTO A;1;10,00;25,00;5", "3;PRODUTO C;10;1000,00;600,00;5"])
        dezembro = _mensal(12, ["1;PRODUTO A;1;10,00;11,00;5", "2;PRODUTO B;10;100,00;110,00;5"])
        resultado = executar_como_o_worker([self.csv(novembro), self.csv(dezembro)])
        ind = raio_x_servico.visao(resultado, PRATA, 1)["indicadores_executivos"]["indicadores"]
        # Em dezembro, B perdeu 10 e A, 1: a perda oculta de A em novembro não o põe na frente.
        self.assertEqual([(l["produto"], l["perda_total"]) for l in ind["margem_negativa"]["matriz"]],
                         [("PRODUTO B", 10.0), ("PRODUTO A", 1.0)])
        # C só vendeu em novembro: sai do mapa de dezembro.
        self.assertEqual([(l["produto"], l["valores"]) for l in ind["mapa_calor"]["linhas"]],
                         [("PRODUTO B", [100.0]), ("PRODUTO A", [10.0])])

    def test_valor_a_custo_fica_com_o_diagnostico_de_cmv_em_qualquer_combinacao(self):
        resultado = executar_como_o_worker([self.csv(texto) for texto in ACUMULADO])
        sem_cmv = frozenset({"raio_x_entrada", "auditoria_abaixo_custo", "margem_bruta", "produtos_parados_ruptura"})
        ind = raio_x_servico.visao(resultado, sem_cmv, None)["indicadores_executivos"]["indicadores"]
        self.assertIsNone(ind["margem_negativa"]["faturamento_custo_a_confirmar"])
        self.assertEqual(ind["margem_negativa"]["perda"], 15.0)
        self.assertIsNone(ind["gmroi"]["estoque_medio_a_custo"])
        self.assertIsNotNone(ind["gmroi"]["valor"])
        self.assertIsNone(ind["encalhados"]["valor_a_custo"])

    def test_contrato_recusa_tipo_inesperado_sem_excecao(self):
        resultado = executar_como_o_worker([self.csv(texto) for texto in ACUMULADO])
        for mudar in (lambda n: n.update({"modo": ["analitico"]}),
                      lambda n: n["indicadores"]["mix"].update({"status": ["calculado"]}),
                      lambda n: n["indicadores"].update({"mix": None})):
            no = copy.deepcopy(resultado["indicadores_executivos"])
            mudar(no)
            self.assertFalse(indicadores_executivos.valido(no))
            adulterado = {**resultado, "indicadores_executivos": no}
            with patch.object(etl_runtime, "_run_job", return_value=adulterado), self.assertRaises(ETLInputError) as erro:
                etl_runtime.run_raio_x(["arquivo.csv"])
            self.assertEqual(erro.exception.code, "worker")

    def test_venda_item_a_item_fatiada_igual_a_relatorios_mensais(self):
        """Refatoração 4: a venda item a item de dois meses dá o mesmo Raio-X que os dois relatórios mensais,
        inclusive o preço de referência e o potencial recuperável, que passam a ser de cada mês."""
        def linhas(*itens):
            return "".join(f"{dia};PRODUTO X;1;{valor}\n" for dia, valor in itens)
        janeiro = (("05/01/2026", "10,00"), ("06/01/2026", "10,00"), ("07/01/2026", "10,00"), ("08/01/2026", "10,00"))
        fevereiro = (("05/02/2026", "8,00"), ("06/02/2026", "8,00"))
        cabecalho = "Data;Produto;Qtde;Valor Total\n"
        inteiro = "Relatorio de vendas item a item\nPeriodo: 01/01/2026 a 28/02/2026\n" + cabecalho + linhas(*janeiro, *fevereiro)
        mensais = ["Relatorio de vendas item a item\nPeriodo: 01/01/2026 a 31/01/2026\n" + cabecalho + linhas(*janeiro),
                   "Relatorio de vendas item a item\nPeriodo: 01/02/2026 a 28/02/2026\n" + cabecalho + linhas(*fevereiro)]
        fatiado = executar_como_o_worker([self.csv(inteiro)])
        separado = executar_como_o_worker([self.csv(texto) for texto in mensais])
        for chave in ("recuperavel", "scanner", "saude", "periodo"):
            self.assertEqual(fatiado[chave], separado[chave], chave)
        self.assertEqual([f["rotulo"] for f in fatiado["fatias"]], ["01/2026", "02/2026"])


def _mes_de(ano: int, mes: int, linhas: list, cabecalho="Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque",
            titulo="Relatorio de vendas por produto") -> str:
    from calendar import monthrange
    fim = monthrange(ano, mes)[1]
    return (f"{titulo}\nPeriodo: 01/{mes:02d}/{ano} a {fim:02d}/{mes:02d}/{ano}\n{cabecalho}\n"
            + "".join(linha + "\n" for linha in linhas))


class SegundaRevisaoContasTests(_Arquivos):
    """Achados da segunda revisão independente nas contas dos indicadores."""

    def _ind(self, *textos):
        return executar_como_o_worker([self.csv(texto) for texto in textos])["indicadores_executivos"]["indicadores"]

    def test_gmroi_com_relatorio_que_so_lista_quem_vendeu_usa_o_estoque_final(self):
        # Quatro produtos com custo 10 e estoque 20 o tempo todo; cada mês o relatório só lista quem vendeu.
        meses = [_mes_de(2025, 10, ["1;A;1;20,00;10,00;20", "2;B;1;20,00;10,00;20"]),
                 _mes_de(2025, 11, ["1;A;1;20,00;10,00;20", "3;C;1;20,00;10,00;20"]),
                 _mes_de(2025, 12, ["1;A;1;20,00;10,00;20", "4;D;1;20,00;10,00;20"])]
        ind = self._ind(*meses)
        self.assertEqual(ind["giro_estoque"]["estoque_a_custo"], 800.0)
        gmroi = ind["gmroi"]
        self.assertEqual((gmroi["base"], gmroi["estoque_medio_a_custo"], gmroi["fotos_de_estoque"]),
                         ("estoque_final", 800.0, 3))
        # O "vendas x estoque", que lista todo produto todo mês, dá o estoque médio de verdade.
        completo = [_mes_de(2025, mes, [f"{n};{p};{1 if p in vendidos else 0};{20 if p in vendidos else 0},00;"
                                        f"{10 if p in vendidos else 0},00;20" for n, p in enumerate("ABCD", 1)])
                    for mes, vendidos in ((10, "AB"), (11, "AC"), (12, "AD"))]
        gmroi = self._ind(*completo)["gmroi"]
        self.assertEqual((gmroi["base"], gmroi["estoque_medio_a_custo"]), ("estoque_medio", 800.0))

    def test_dias_cobertos_sem_os_meses_que_nenhum_relatorio_cobre(self):
        # Janeiro e abril de 2025, sem fevereiro e março: 61 dias cobertos (e não 120).
        janeiro = _mes_de(2025, 1, ["1;A;31;310,00;155,00;31", "2;B;31;310,00;155,00;80"])
        abril = _mes_de(2025, 4, ["1;A;30;300,00;150,00;31"])
        ind = self._ind(janeiro, abril)
        cobertura = ind["cobertura_estoque"]
        self.assertEqual(cobertura["dias_do_periodo"], 61)
        # A vende 61 em 61 dias cobertos, 1 por dia: 31 de estoque são 31 dias.
        self.assertEqual({i["produto"]: i["dias"] for i in cobertura["lista"]}["A"], 31.0)
        self.assertEqual(ind["giro_estoque"]["dias_do_periodo"], 61)
        # B vendeu em janeiro: depois dele, só os 30 dias de abril são certos (pelo calendário seriam 89): não
        # é encalhe certo.
        self.assertEqual(ind["encalhados"]["itens"], 0)

    def test_item_nunca_vendido_conta_so_os_dias_cobertos(self):
        # Fevereiro e abril de 2025: 58 dias cobertos (pelo calendário seriam 89). A VELA, só com saldo e sem venda
        # nenhuma, não passa dos 60 dias com certeza.
        fevereiro = _mes_de(2025, 2, ["1;CAFE;5;50,00;25,00;10"])
        abril = _mes_de(2025, 4, ["1;CAFE;5;50,00;25,00;10", "5;VELA;0;0,00;0,00;7"])
        encalhados = self._ind(fevereiro, abril)["encalhados"]
        self.assertNotIn("VELA", json.dumps(encalhados))

    def test_indicador_fora_do_contrato_sai_sem_dados_sem_derrubar_o_raio_x(self):
        # A rede de proteção: um número além do que o contrato aceita leva só o indicador a sem_dados.
        absurdo = {"status": "calculado", "valor": 1e16, "devolucoes": 0.0, "liquido_de_devolucoes": 1e16}
        with patch.object(indicadores_executivos, "_faturamento", return_value=absurdo):
            resultado = executar_como_o_worker([self.csv(ACUMULADO[0])])
        no = resultado["indicadores_executivos"]
        self.assertTrue(indicadores_executivos.valido(no))
        self.assertEqual(no["indicadores"]["faturamento"], {"status": "sem_dados", "valor": None, "devolucoes": None,
                                                            "liquido_de_devolucoes": None})
        self.assertEqual(no["indicadores"]["mix"]["status"], "calculado")

    def test_duas_lojas_no_mesmo_periodo_nao_tem_estoque_da_rede(self):
        loja_1 = _mes_de(2025, 10, ["1;A;10;200,00;100,00;50"], titulo="Loja 1 - Relatorio de vendas por produto")
        loja_2 = _mes_de(2025, 10, ["1;A;10;200,00;100,00;30"], titulo="Loja 2 - Relatorio de vendas por produto")
        ind = self._ind(loja_1, loja_2)
        self.assertEqual(ind["faturamento"]["valor"], 400.0)
        for chave in ("giro_estoque", "ruptura", "encalhados", "cobertura_estoque", "gmroi"):
            self.assertEqual(ind[chave]["status"], "sem_dados", chave)

    def test_estoque_de_varias_lojas_na_venda_item_a_item_fatiada(self):
        # A loja 1 vende P em setembro (saldo 10); a loja 2, em outubro (saldo 5): o estoque da rede é 15.
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/09/2025 a 31/10/2025\n"
                 "Data;Loja;Produto;Qtde;Valor Total;Total Custo;Estoque\n"
                 "05/09/2025;LOJA 1;P;1;10,00;5,00;10\n06/09/2025;LOJA 1;P;1;10,00;5,00;10\n"
                 "05/10/2025;LOJA 2;P;1;10,00;5,00;5\n06/10/2025;LOJA 2;P;1;10,00;5,00;5\n")
        ind = self._ind(texto)
        self.assertEqual(ind["giro_estoque"]["estoque_a_custo"], 75.0)  # 15 x 5,00

    def test_deposito_so_com_saldo_soma_no_estoque_da_rede(self):
        texto = _mes_de(2025, 10, ["LOJA;1;P;5;50,00;25,00;10", "DEPOSITO;1;P;0;0,00;0,00;40"],
                        cabecalho="Loja;Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque")
        ind = self._ind(texto)
        self.assertEqual(ind["giro_estoque"]["estoque_a_custo"], 250.0)  # (10 + 40) x 5,00
        invertido = _mes_de(2025, 10, ["DEPOSITO;1;P;0;0,00;0,00;40", "LOJA;1;P;5;50,00;25,00;10"],
                            cabecalho="Loja;Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque")
        self.assertEqual(self._ind(invertido)["giro_estoque"]["estoque_a_custo"], 250.0)

    SEM_PERIODO = "Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque\n9;PRODUTO X;2;40,00;20,00;10\n"

    def _tres_meses(self, saldo_de_outubro):
        return [_mes_de(2025, 8, ["2;LEITE 1L;5;25,00;20,00;8", "9;PRODUTO X;1;20,00;10,00;5"]),
                _mes_de(2025, 9, ["2;LEITE 1L;5;25,00;20,00;8"]),
                _mes_de(2025, 10, ["2;LEITE 1L;5;25,00;20,00;8", f"9;PRODUTO X;0;0,00;0,00;{saldo_de_outubro}"])]

    def test_saldo_do_relatorio_sem_periodo_nao_passa_por_cima_do_mes_mais_recente(self):
        # O relatório sem período (saldo 10, data desconhecida) é o mais antigo: o saldo de outubro (-2) vale.
        ind = self._ind(*self._tres_meses(-2), self.SEM_PERIODO)
        self.assertIn("PRODUTO X", [i["produto"] for i in ind["ruptura"]["lista"]])

    def test_venda_em_relatorio_sem_periodo_tira_o_produto_dos_encalhados(self):
        # X vendeu em agosto (61 dias cobertos depois) e também num relatório sem período, que pode ser mais
        # recente: a última venda é desconhecida.
        ind = self._ind(*self._tres_meses(10), self.SEM_PERIODO)
        self.assertNotIn("PRODUTO X", json.dumps(ind["encalhados"]))
        # Sem o relatório sem período, ele é encalhado.
        self.assertIn("PRODUTO X", [i["produto"] for i in self._ind(*self._tres_meses(10))["encalhados"]["lista"]])

    def test_recompra_sem_cpf_num_dos_arquivos_pede_o_cpf(self):
        sem_cpf = ("Relatorio de vendas item a item\nPeriodo: 01/12/2025 a 31/12/2025\n"
                   "Data;Hora;Nr. Doc.;Produto;Qtde;Valor Total\n"
                   "06/12/2025;09:15;P2-0001;CAFE 500G;1;20,00\n07/12/2025;10:00;P2-0002;CAFE 500G;1;20,00\n")
        ind = self._ind(ANALITICO, sem_cpf)
        self.assertEqual(ind["ticket_medio"]["status"], "calculado")
        self.assertEqual(ind["recompra"]["status"], "requer_cpf_cnpj")

    def test_mapa_por_produto_e_mes_traz_o_mes_sem_venda(self):
        outubro = _mes_de(2025, 10, ["1;CAFE;1;10,00;5,00;5"])
        novembro = _mes_de(2025, 11, ["1;CAFE;0;0,00;0,00;5"])
        dezembro = _mes_de(2025, 12, ["1;CAFE;2;20,00;10,00;5"])
        calor = self._ind(outubro, novembro, dezembro)["mapa_calor"]
        self.assertEqual(calor["meses"], ["2025-10", "2025-11", "2025-12"])
        self.assertEqual(calor["linhas"][0]["valores"], [10.0, 0.0, 20.0])

    def test_cmv_sem_custo_plausivel_sai_sem_dados(self):
        # Só o item a confirmar tem custo (500 para 10 vendidos).
        ind = self._ind(_mes_de(2025, 10, ["1;GENERICO;1;10,00;500,00;5", "2;CAFE;1;20,00;;5"]))
        self.assertEqual(ind["cmv"], {"status": "sem_dados", "valor": None, "custo_a_confirmar": 500.0,
                                      "itens_custo_a_confirmar": 1})

    def test_curva_por_volume_soma_a_classe_com_tres_casas(self):
        # Granel: 0,875 kg do A (a classe A) e 0,125 e 0,005 kg do B e do C (a classe C, 0,130).
        texto = _mes_de(2025, 10, ["1;A;0,875;10,00;;", "2;B;0,125;10,00;;", "3;C;0,005;10,00;;"])
        classes = {c["classe"]: c["valor"] for c in self._ind(texto)["curva_abc_volume"]["classes"]}
        self.assertEqual((classes["A"], classes["C"]), (0.875, 0.13))

    def test_desconto_em_percentual_ou_absurdo_nao_vira_indice(self):
        base = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                "Data;Nr. Doc.;Produto;Qtde;Valor Total;Desconto\n")
        percentual = base + "06/10/2025;1;CAFE;1;20,00;10%\n07/10/2025;2;CAFE;1;20,00;0%\n"
        self.assertEqual(self._ind(percentual)["indice_descontos"]["status"], "sem_dados")
        absurdo = base + "06/10/2025;1;CAFE;1;20,00;999999999999999999999999999999\n07/10/2025;2;CAFE;1;20,00;0,00\n"
        resultado = executar_como_o_worker([self.csv(absurdo)])
        self.assertEqual(resultado["indicadores_executivos"]["indicadores"]["indice_descontos"]["status"], "sem_dados")
        self.assertTrue(indicadores_executivos.valido(resultado["indicadores_executivos"]))
        self.assertEqual(resultado["saude"]["faturamento_total"], 40.0)

    def test_cnpj_alfanumerico_identifica_o_cliente(self):
        # O exemplo da Receita (IN RFB 2.229/2024): 12.ABC.345/01DE-35.
        self.assertIsNotNone(cliente_identificado("12.ABC.345/01DE-35"))
        self.assertIsNone(cliente_identificado("12.ABC.345/01DE-34"))
        self.assertEqual(cliente_identificado("CPF 529.982.247-25"), cliente_identificado(CPF_DE_EXEMPLO))


class SegundaRevisaoLeituraTests(_Arquivos):
    """Achados da segunda revisão independente na recusa de compras e na leitura do cliente."""

    def _faturamento(self, texto):
        return executar_como_o_worker([self.csv(texto)])["saude"]["faturamento_total"]

    def test_titulo_de_vendas_com_colunas_de_compra_e_venda(self):
        periodo = "Periodo: 01/10/2025 a 31/10/2025\n"
        casos = {
            "qtde_comprada_do_cliente": ("Relatorio de Vendas por Cliente\n" + periodo
                                         + "Cliente;Produto;Qtde Comprada;Valor Total\nANA;CAFE;1;20,00\n", 20.0),
            "total_compra_com_preco_de_venda": ("Relatorio de Vendas por Produto\n" + periodo
                                                + "Produto;Qtde Vendida;Preco Venda;Total Compra\nCAFE;3;10,00;15,00\n", 30.0),
            "total_da_compra_do_pedido": ("Relatorio de Vendas\n" + periodo
                                          + "Pedido;Produto;Qtde;Preco Unitario;Total da Compra\n1;CAFE;2;10,00;20,00\n", 20.0),
            "vendas_x_compras": ("Vendas x Compras por Produto\n" + periodo
                                 + "Produto;Quant. Vendida;Quant. Comprada;Valor Total\nCAFE;5;8;50,00\n", 50.0),
            "pedidos_sem_titulo": ("Numero do Pedido;Data;Nome do Produto;Quantidade Comprada;Preco Unitario;Valor Total\n"
                                   "1;06/10/2025;CAFE;2;10,00;20,00\n", 20.0),
            "giro_com_quantidade_comprada": ("Giro de Vendas por Produto\n" + periodo
                                             + "Produto;Quantidade;Valor Total;Quantidade Comprada;Saldo\nCAFE;2;20,00;5;8\n", 20.0),
        }
        for nome, (texto, esperado) in casos.items():
            with self.subTest(relatorio=nome):
                self.assertEqual(self._faturamento(texto), esperado)
                if "Periodo" in texto:
                    etl_processor.carregar_vendas(self.csv(texto))  # a análise mensal também aceita

    def test_valor_de_filtro_ou_secao_nao_e_titulo_de_compras(self):
        corpo = "Periodo: 01/10/2025 a 31/10/2025\nProduto;Qtde;Valor Total\nBRUSCHETTA;2;30,00\n"
        for topo in ("Curva ABC de Produtos\nGrupo:;ENTRADAS\n", "Ranking de Produtos\nUsuario:;COMPRAS\n",
                     "Curva ABC de Produtos\nENTRADAS\n",
                     # O valor do filtro com cara de título ("Compras por Fornecedor" é o nome do setor).
                     "Curva ABC de Produtos\nSetor:;Compras por Fornecedor\n"):
            with self.subTest(topo=topo):
                self.assertEqual(self._faturamento(topo + corpo), 30.0)
        # "COMPRAS" sozinho na primeira célula do arquivo é o título.
        self.assertEqual(self.recusa("raio_x_dados", executar_como_o_worker, [self.csv("COMPRAS\n" + corpo)]),
                         MENSAGEM_DE_COMPRAS)

    def test_compra_do_cliente_e_venda_da_loja(self):
        corpo = "Periodo: 01/10/2025 a 31/10/2025\nCliente;Produto;Qtde;Valor Total\nANA;CAFE;3;60,00\n"
        for titulo in ("Compras por Cliente", "Historico de Compras do Cliente", "Ranking de Compras por Cliente"):
            with self.subTest(titulo=titulo):
                self.assertEqual(self._faturamento(titulo + "\n" + corpo), 60.0)

    def test_titulos_obvios_de_compras_e_entradas_sao_recusados(self):
        corpo = "Periodo: 01/10/2025 a 31/10/2025\nProduto;Qtde;Valor Total\nCAFE 500G;10;100,00\n"
        for titulo in ("Nota Fiscal de Entrada", "Relatorio de Notas Fiscais de Entrada", "Relatorio de Notas de Entrada",
                       "NF-e de Entrada", "Curva ABC de Compras", "Ranking de Compras", "Recebimento de Mercadorias",
                       "Compras do Mes", "ENTRADAS NO PERIODO DE 01/10/2025 A 31/10/2025",
                       "Relatorio de Compras\nMERCADO SILVA LTDA - Rua Padre Pedro Pinto 100 - Venda Nova"):
            with self.subTest(titulo=titulo):
                self.assertEqual(self.recusa("raio_x_dados", executar_como_o_worker, [self.csv(titulo + "\n" + corpo)]),
                                 MENSAGEM_DE_COMPRAS)
        sem_titulo = ("Fornecedor;NF;Data Entrada;Produto;Qtd. Compra;Custo Unit.;Valor Total\n"
                      "ALFA;1;01/10/2025;CAFE;10;9,00;90,00\n")
        self.assertEqual(self.recusa("raio_x_dados", executar_como_o_worker, [self.csv(sem_titulo)]), MENSAGEM_DE_COMPRAS)

    def test_cnpj_da_unidade_de_negocio_continua_sendo_a_loja(self):
        texto = _mes_de(2025, 10, ["11222333000181;1;CAFE;10;200,00;100,00;7", "52998224725000;1;CAFE;5;100,00;50,00;3"],
                        cabecalho="CNPJ Unidade de Negocio;Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque")
        no = executar_como_o_worker([self.csv(texto)])["indicadores_executivos"]
        self.assertEqual(no["modo"], "acumulado")
        self.assertEqual(no["indicadores"]["giro_estoque"]["estoque_a_custo"], 100.0)  # (7 + 3) x 10,00

    def test_cpf_de_quem_nao_e_o_cliente_nao_conta_na_recompra(self):
        def venda(titulo):
            return ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                    f"Data;Nr. Doc.;{titulo};Produto;Qtde;Valor Total\n"
                    f"06/10/2025;1;{CPF_DE_EXEMPLO};CAFE;1;10,00\n07/10/2025;2;{CPF_DE_EXEMPLO};CAFE;1;10,00\n")
        for titulo in ("CPF Operador", "CPF Caixa", "CNPJ Representante", "CNPJ Matriz", "CNPJ Intermediador"):
            with self.subTest(titulo=titulo):
                ind = executar_como_o_worker([self.csv(venda(titulo))])["indicadores_executivos"]["indicadores"]
                self.assertEqual(ind["recompra"]["status"], "requer_cpf_cnpj")
        for titulo in ("CPF", "CPF/CNPJ", "N CPF", "CPF na Nota", "CPF do Cliente", "Documento do Consumidor CPF"):
            with self.subTest(titulo=titulo):
                ind = executar_como_o_worker([self.csv(venda(titulo))])["indicadores_executivos"]["indicadores"]
                self.assertEqual((ind["recompra"]["status"], ind["recompra"]["clientes_recorrentes"]), ("calculado", 1))
        # Com o CNPJ da matriz antes do CPF do cliente, vale o do cliente.
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                 "Data;Nr. Doc.;CNPJ Matriz;CPF/CNPJ Cliente;Produto;Qtde;Valor Total\n"
                 f"06/10/2025;1;{CNPJ_DE_EXEMPLO};{CPF_DE_EXEMPLO};CAFE;1;10,00\n"
                 f"07/10/2025;2;{CNPJ_DE_EXEMPLO};;CAFE;1;10,00\n")
        recompra = executar_como_o_worker([self.csv(texto)])["indicadores_executivos"]["indicadores"]["recompra"]
        self.assertEqual((recompra["clientes_identificados"], recompra["clientes_recorrentes"]), (1, 0))


class TriagemPorAnoTests(unittest.TestCase):
    """Achado da revisão: o ano de custo a confirmar não esconde a perda real do mesmo produto noutro ano."""

    def setUp(self):
        self.p2024 = _linha("P", 2024, venda="100.00", custo="400.00", perda="300.00", recuperavel="300.00")
        self.p2025 = _linha("P", 2025, venda="1000.00", custo="1100.00", perda="100.00", recuperavel="100.00")
        self.linhas = [self.p2024, self.p2025, _linha("N", 2025)]
        self.triagem = triagem_cenario.triar(self.linhas, cobertura(a2024=(12, 366, True), a2025=(12, 365, True)))

    def test_contagem_motivo_e_marcas(self):
        self.assertEqual(self.triagem.itens_venda_abaixo_custo, 1)
        self.assertIn("abaixo_do_custo", self.triagem.motivos["P"])
        self.assertIn("custo_implausivel", self.triagem.motivos["P"])
        self.assertEqual(triagem_cenario.marcas(self.p2024)["flag_venda_abaixo_custo"], False)
        self.assertEqual(triagem_cenario.marcas(self.p2024)["vazamento_estimado_reais"], D("0.00"))
        self.assertEqual(triagem_cenario.marcas(self.p2025)["flag_venda_abaixo_custo"], True)
        self.assertEqual(triagem_cenario.marcas(self.p2025)["vazamento_estimado_reais"], D("100.00"))

    def test_matriz_do_consultor(self):
        import diagnostico_cenario
        from tests.test_triagem_cenario import meta
        dados = meta({2024: (12, 366, True), 2025: (12, 365, True)})
        base = triagem_cenario.Cobertura.do_envio(dados)
        totais = diagnostico_cenario.matriz_por_categoria(self.linhas, base, triagem_cenario.triar(self.linhas, base),
                                                         dados)["totais"]
        self.assertEqual((totais["marcas"]["abaixo_do_custo"], totais["vendas_abaixo_do_custo"],
                          totais["potencial_recuperavel"]), (1, 100.0, 100.0))

    def test_itens_dos_blocos_seguem_a_mesma_regra_e_fecham_com_o_resumo(self):
        """Achado da segunda revisão: o item do bloco somava a perda contra o custo a confirmar como recuperável."""
        import diagnostico_cenario
        # G: um ano só, com custo a confirmar (venda 20, custo 100, perda 80).
        linhas = self.linhas + [_linha("G", 2024, venda="20.00", custo="100.00", perda="80.00", recuperavel="80.00")]
        base = cobertura(a2024=(12, 366, True), a2025=(12, 365, True))
        triagem = triagem_cenario.triar(linhas, base)
        blocos = diagnostico_cenario.itens_dos_blocos(linhas, base, triagem, limite=100)
        itens = {item["produto"]: item for bloco in blocos.values() for item in bloco["itens"]}
        self.assertEqual((itens["Produto P"]["potencial_recuperavel"], itens["Produto P"]["vendas_abaixo_do_custo"]),
                         (100.0, 100.0))
        self.assertEqual((itens["Produto G"]["potencial_recuperavel"], itens["Produto G"]["vendas_abaixo_do_custo"]),
                         (0.0, 0.0))
        for bloco, dados in blocos.items():
            with self.subTest(bloco=bloco):
                self.assertEqual(round(sum(item["potencial_recuperavel"] for item in dados["itens"]), 2),
                                 float(D(triagem.resumos[bloco]["recuperavel"])))

    def test_definicoes_dizem_que_o_ano_de_custo_a_confirmar_fica_fora(self):
        import diagnostico_cenario
        for chave in ("vendas_abaixo_do_custo", "potencial_recuperavel", "marcas"):
            self.assertIn("custo a confirmar", diagnostico_cenario.DEFINICOES[chave], chave)

    def test_definicoes_seguem_a_versao_gravada_no_lead(self):
        """Achado 16 da terceira revisão: o lead triado pela 11.3 recebia as definições da 11.4."""
        import diagnostico_cenario
        original = copy.deepcopy(diagnostico_cenario.DEFINICOES)
        atual = diagnostico_cenario.definicoes_da_versao(triagem_cenario.VERSAO_TRIAGEM)
        anterior = diagnostico_cenario.definicoes_da_versao("triagem-2026-11.3")
        for chave in ("vendas_abaixo_do_custo", "potencial_recuperavel", "marcas"):
            with self.subTest(chave=chave):
                self.assertIn("custo a confirmar", atual[chave])
                self.assertNotIn("custo a confirmar", anterior[chave])
        self.assertEqual(set(anterior), set(atual))
        self.assertEqual(diagnostico_cenario.DEFINICOES, original)  # o dicionário global não muda

    def test_a_versao_atual_esta_nas_versoes_dos_limiares(self):
        """Achado 15 da terceira revisão: a versão nova sempre entra no conjunto."""
        self.assertIn(triagem_cenario.VERSAO_TRIAGEM, triagem_cenario.VERSOES_DOS_LIMIARES)
        self.assertEqual(triagem_cenario.Limiares.de_dict(triagem_cenario.LIMIARES_PADRAO.para_dict()),
                         triagem_cenario.LIMIARES_PADRAO)


# =============================================================================
# Terceira revisão independente
# =============================================================================
class TerceiraRevisaoLojasTests(_Arquivos):
    """Achados 1, 13 e 14: numa loja só, a célula da loja em branco (mesclada, ou preenchida só na primeira linha do
    grupo ou do cupom) é dessa loja. O estoque não soma duas vezes, e a venda não conta duas."""

    CABECALHO = "Loja;Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque"
    HOJE = date(2026, 11, 9)

    def _ind(self, caminho):
        return executar_como_o_worker([caminho])["indicadores_executivos"]["indicadores"]

    def _estoque_do_lead(self, caminho):
        resultado = avaliar_como_o_worker([caminho], hoje=self.HOJE)
        linhas = [dict(zip(resultado["linhas"]["colunas"], valores)) for valores in resultado["linhas"]["valores"]]
        return {linha["descricao_produto"]: linha["estoque_final"] for linha in linhas}

    def _xlsx(self, linhas, mescla):
        from openpyxl import Workbook
        livro = Workbook()
        folha = livro.active
        for linha in linhas:
            folha.append(linha)
        folha.merge_cells(mescla)
        self.numero += 1
        caminho = self.pasta / f"arquivo_{self.numero:02d}.xlsx"
        livro.save(caminho)
        livro.close()
        return str(caminho)

    def _confere(self, caminho):
        # CAFE: 30 na primeira linha (com a loja) e 28 na última (sem): o saldo é 28, e não 58. A custo: 28 x 10,00
        # + LEITE 12 x 3,00.
        ind = self._ind(caminho)
        self.assertEqual(ind["giro_estoque"]["estoque_a_custo"], 316.0)
        self.assertEqual({i["produto"]: i["estoque"] for i in ind["cobertura_estoque"]["lista"]}["CAFE"], 28.0)
        self.assertEqual(ind["gmroi"]["estoque_medio_a_custo"], 316.0)
        estoque = self._estoque_do_lead(caminho)
        self.assertEqual((estoque["CAFE"], estoque["LEITE"]), (28, 12))

    def test_loja_so_na_primeira_linha_do_csv(self):
        linhas = ["LOJA CENTRO;1;CAFE;1;20,00;10,00;30", ";2;LEITE;2;10,00;6,00;12", ";1;CAFE;1;20,00;10,00;28"]
        self._confere(self.csv(_mes_de(2025, 10, linhas, cabecalho=self.CABECALHO)))
        # Controles: a loja em todas as linhas e sem a coluna de loja dão o mesmo.
        todas = [linha if linha.startswith("LOJA") else "LOJA CENTRO" + linha for linha in linhas]
        self._confere(self.csv(_mes_de(2025, 10, todas, cabecalho=self.CABECALHO)))
        sem_coluna = [linha.split(";", 1)[1] for linha in linhas]
        self._confere(self.csv(_mes_de(2025, 10, sem_coluna)))

    def test_loja_mesclada_no_xlsx(self):
        caminho = self._xlsx([["Relatorio de vendas por produto"], ["Periodo: 01/10/2025 a 31/10/2025"],
                              self.CABECALHO.split(";"), ["LOJA CENTRO", 1, "CAFE", 1, 20, 10, 30],
                              [None, 2, "LEITE", 2, 10, 6, 12], [None, 1, "CAFE", 1, 20, 10, 28]], "A4:A6")
        self._confere(caminho)

    def test_venda_item_a_item_fatiada_de_uma_loja_com_a_loja_so_na_primeira_linha(self):
        # Outubro termina com saldo 29 e novembro com 19: o saldo da primeira linha de outubro não fica no acúmulo.
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 30/11/2025\n"
                 "Data;Loja;Produto;Qtde;Valor Total;Total Custo;Estoque\n"
                 "05/10/2025;LOJA CENTRO;CAFE;1;20,00;10,00;30\n06/10/2025;;CAFE;1;20,00;10,00;29\n"
                 "05/11/2025;;CAFE;1;20,00;10,00;19\n")
        caminho = self.csv(texto)
        self.assertEqual(self._ind(caminho)["giro_estoque"]["estoque_a_custo"], 190.0)
        self.assertEqual(self._estoque_do_lead(caminho)["CAFE"], 19)

    def test_numero_da_venda_com_a_loja_so_no_primeiro_item(self):
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                 "Data;Loja;Nr. Doc.;Produto;Qtde;Valor Total\n"
                 "06/10/2025;LOJA CENTRO;1001;CAFE;1;20,00\n06/10/2025;;1001;LEITE;1;10,00\n"
                 "07/10/2025;;1002;CAFE;1;20,00\n07/10/2025;;1002;LEITE;1;10,00\n")
        ind = self._ind(self.csv(texto))
        self.assertEqual((ind["ticket_medio"]["transacoes"], ind["ticket_medio"]["valor"]), (2, 30.0))
        self.assertEqual(ind["itens_por_venda"]["valor"], 2.0)
        # Ordenado por produto, com a loja só na primeira linha (mesclada no relatório inteiro): as linhas do mesmo
        # cupom não ficam juntas, e com uma loja só a loja não entra no número.
        por_produto = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                       "Data;Loja;Nr. Doc.;Produto;Qtde;Valor Total\n"
                       "06/10/2025;LOJA CENTRO;1001;CAFE;1;20,00\n07/10/2025;;1002;CAFE;1;20,00\n"
                       "06/10/2025;;1001;LEITE;1;10,00\n07/10/2025;;1002;LEITE;1;10,00\n")
        self.assertEqual(self._ind(self.csv(por_produto))["ticket_medio"]["transacoes"], 2)

    def test_numero_da_venda_de_duas_lojas_com_a_loja_so_no_primeiro_item(self):
        # O cupom 1001 existe nas duas lojas: são duas vendas, e a linha de baixo de cada uma é da loja do cupom.
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                 "Data;Loja;Nr. Doc.;Produto;Qtde;Valor Total\n"
                 "06/10/2025;LOJA 1;1001;CAFE;1;20,00\n06/10/2025;;1001;LEITE;1;10,00\n"
                 "06/10/2025;LOJA 2;1001;CAFE;1;20,00\n06/10/2025;;1001;LEITE;1;10,00\n"
                 "07/10/2025;LOJA 1;1002;CAFE;1;20,00\n07/10/2025;;1002;LEITE;1;10,00\n")
        self.assertEqual(self._ind(self.csv(texto))["ticket_medio"]["transacoes"], 3)


class TerceiraRevisaoComprasTests(_Arquivos):
    """Achados 2 a 7 e 9: a recusa de compras nas três rotas (análise mensal, Raio-X e lead)."""

    HOJE = date(2026, 11, 9)
    P = "Periodo: 01/10/2025 a 31/10/2025\n"
    CORPO = P + "Produto;Qtde;Valor Total\nCAFE;10;168,00\n"
    TOTAL_COMPRA = P + "Produto;Qtde;Preco Unitario;Total Compra\nCAFE;10;16,80;100,00\n"

    def aceito(self, texto, faturamento=168.0):
        caminho = self.csv(texto)
        self.assertEqual(executar_como_o_worker([caminho])["saude"]["faturamento_total"], faturamento)
        etl_processor.carregar_vendas(caminho)
        if "Periodo" in texto:
            lead = avaliar_como_o_worker([caminho], hoje=self.HOJE)
            self.assertEqual(lead["raio_x"]["saude"]["faturamento_total"], faturamento)

    def recusado(self, texto):
        caminho = self.csv(texto)
        with self.assertRaises(etl_processor.ETLRelatorioDeCompras):
            etl_processor.carregar_vendas(caminho)
        self.assertEqual(self.recusa("raio_x_dados", executar_como_o_worker, [caminho]), MENSAGEM_DE_COMPRAS)
        self.assertEqual(self.recusa("raio_x_dados", avaliar_como_o_worker, [caminho], hoje=self.HOJE),
                         MENSAGEM_DE_COMPRAS)

    def test_achado_2_a_quantidade_comprada_ao_lado_da_vendida(self):
        for nome, texto in {
                "faturamento": "Faturamento por Produto\n" + self.P
                + "Produto;Qtde;Qtde Compra;Custo Medio;Valor Total;Estoque\nCAFE;10;5;8,00;168,00;3\n",
                "giro_de_estoque": "Giro de Estoque\n" + self.P
                + "Produto;Qtde;Qtd. Compra;Custo Medio;Valor Total;Estoque\nCAFE;10;5;8,00;168,00;3\n",
                "so_o_periodo": self.P + "Produto;Qtde;Qtde Compra;Custo Medio;Valor Total;Estoque\nCAFE;10;5;8,00;168,00;3\n",
                "curva_abc": "Curva ABC de Produtos\n" + self.P
                + "Produto;Qtde;Custo Unitario;Qtde Compras;Estoque;Valor Total\nCAFE;10;8,00;5;3;168,00\n",
                "giro_de_mercadorias": "Giro de Mercadorias\n" + self.P
                + "Produto;Quantidade;Quantidade Compra;Custo;Valor Total\nCAFE;10;5;80,00;168,00\n",
                "qtde_comprada_ao_lado": self.P + "Produto;Qtde;Qtde Comprada;Custo Medio;Valor Total\nCAFE;10;5;8,00;168,00\n",
        }.items():
            with self.subTest(relatorio=nome):
                self.aceito(texto)
        # A quantidade lida é a comprada: continua recusado.
        for texto in ("Produto;Qtd. Compra;Custo Unit.;Valor Total\nCAFE;10;9,00;90,00\n",
                      "Produto;Qtde Compra;Qtde;Custo;Valor Total\nCAFE;10;5;80,00;90,00\n"):
            with self.subTest(recusado=texto.split("\n")[0]):
                self.recusado(texto)

    def test_achado_3_o_titulo_depois_do_rotulo_do_titulo(self):
        for topo in ("Relatório:;Compras por Fornecedor\n", "Tipo de Relatório:;Entradas de Mercadorias\n",
                     "Empresa:;MERCADO X LTDA\nRelatório:;RELATÓRIO DE COMPRAS\n", "Título:;Notas Fiscais de Entrada\n",
                     "Relatório:;COMPRAS\n", "Relatório: Compras por Fornecedor\n"):
            with self.subTest(recusado=topo):
                self.recusado(topo + self.CORPO)
        for topo in ("Curva ABC de Produtos\nGrupo:;ENTRADAS\n", "Ranking de Produtos\nUsuário:;COMPRAS\n",
                     "Relatório:;Vendas por produto\n", "Empresa:;COMPRA CERTA SUPERMERCADO LTDA\n"):
            with self.subTest(aceito=topo):
                self.aceito(topo + self.CORPO)
        self.aceito("Relatório:;Compras por Cliente\n" + self.P + "Cliente;Produto;Qtde;Valor Total\nANA;CAFE;10;168,00\n")

    def test_achado_4_titulos_de_vendas_e_a_compra_do_cliente(self):
        casos = {
            "Produtos Mais Vendidos": (self.P + "Produto;Qtde Vendida;Preco Venda;Total Compra\nCAFE;10;16,80;100,00\n",
                                       168.0),
            "Vendas de Produtos": (self.P + "Pedido;Produto;Qtde;Preco Unitario;Total da Compra\n1;CAFE;2;53,00;106,00\n",
                                   106.0),
            "Curva ABC de Produtos Vendidos": (self.P + "Produto;Qtde;Preco Unitario;Valor Total da Compra\n"
                                               "CAFE;10;16,80;168,00\n", 168.0),
            "Rel. Vendas por Produto": (self.TOTAL_COMPRA, 168.0),
            "Pedidos de Venda": (self.P + "Pedido;Cliente;Produto;Qtde Comprada;Custo Unit.;Valor Total\n"
                                 "1;ANA;CAFE;4;8,00;84,00\n", 84.0),
            "Compras por Cliente": (self.P + "Cliente;Produto;Qtde;Preco Unitario;Total da Compra\nANA;CAFE;4;19,00;76,00\n",
                                    76.0),
            "Histórico de Compras\nCliente:;JOAO DA SILVA": (self.P + "Data;Cupom;Produto;Qtde;Valor Total\n"
                                                             "06/10/2025;1;CAFE;4;76,00\n", 76.0),
            "Faturamento por Produto": (self.TOTAL_COMPRA, 168.0),
            "Relação de Vendas": (self.TOTAL_COMPRA, 168.0),
            "Demonstrativo de Vendas": (self.TOTAL_COMPRA, 168.0),
            "Consulta de Vendas": (self.TOTAL_COMPRA, 168.0),
            "Vendas do Período": (self.TOTAL_COMPRA, 168.0),
            "Vendas Diárias": (self.TOTAL_COMPRA, 168.0),
            "Itens Mais Vendidos": (self.TOTAL_COMPRA, 168.0),
        }
        for titulo, (corpo, faturamento) in casos.items():
            with self.subTest(titulo=titulo):
                self.aceito(titulo + "\n" + corpo, faturamento)
        # A palavra "Venda" solta, no nome da loja, não salva o relatório de compras; o "Fornecedor:" não é cliente.
        for texto in ("VENDA NOVA SUPERMERCADO LTDA\nRelatório de Compras\n" + self.CORPO,
                      "VENDA DO ZE MERCEARIA\nRelatório de Compras\n" + self.CORPO,
                      "VENDA DO ZE MERCEARIA\nProduto;Qtd. Compra;Custo Unit.;Valor Total\nCAFE;10;9,00;90,00\n",
                      "Histórico de Compras\nFornecedor:;ALFA\n" + self.CORPO):
            with self.subTest(recusado=texto.split("\n")[0]):
                self.recusado(texto)

    def test_achado_5_titulos_obvios_e_a_exportacao_pelo_fornecedor_e_pela_entrada(self):
        for titulo in ("Pedidos de Compra", "Ordens de Compra", "Compras Realizadas", "Compras/Entradas",
                       "Entrada de NF-e", "Consulta de Notas de Entrada", "Demonstrativo de Compras",
                       "Mercadorias Recebidas"):
            with self.subTest(titulo=titulo):
                self.recusado(titulo + "\n" + self.CORPO)
        self.recusado("Fornecedor;Nota Fiscal;Data Entrada;Produto;Quantidade;Valor Unitário;Valor Total\n"
                      "ALFA;1;01/10/2025;CAFE;10;16,80;168,00\n")
        # "Produtos Comprados" fica de fora (no comércio eletrônico, é o do cliente); a venda por fornecedor e a
        # venda com a data de entrada sob título de vendas passam.
        self.aceito("Produtos Comprados\n" + self.CORPO)
        self.aceito("Fornecedor;Produto;Qtde;Valor Total\nALFA;CAFE;10;168,00\n")
        self.aceito("Itens Vendidos\n" + self.P + "Fornecedor;Data Entrada;Produto;Qtde;Valor Total\n"
                    "ALFA;01/10/2025;CAFE;10;168,00\n")

    def test_achado_6_custo_do_envio_e_entrada_do_crediario_nao_sao_da_compra(self):
        for coluna in ("Custo de Envio", "Custo do Frete", "Entrada", "Valor do Frete"):
            with self.subTest(coluna=coluna):
                self.aceito(f"Numero do Pedido;Data;Nome do Produto;Quantidade Comprada;Preco Unitario;{coluna};"
                            "Valor Total\n1;06/10/2025;CAFE;2;100,00;4,00;204,00\n", 204.0)

    def test_achado_7_nome_da_loja_e_grupo_de_pratos(self):
        for topo in ("COMPRA POR MENOS SUPERMERCADO LTDA\nFaturamento por Produto\n",
                     "COMPRA POR MENOS SUPERMERCADO LTDA\nCurva ABC de Produtos\n",
                     "COMPRA NA HORA MINIMERCADO LTDA\nCurva ABC de Produtos\n", "Ranking de Entradas\n",
                     "Vendas de Produtos\nENTRADAS NA BRASA\n"):
            with self.subTest(aceito=topo):
                self.aceito(topo + self.CORPO)
        for topo in ("Compras por Produto\n", "Entradas no Período\n", "Ranking de Compras\n"):
            with self.subTest(recusado=topo):
                self.recusado(topo + self.CORPO)

    def test_qualificador_do_relatorio_no_titulo(self):
        """Revisão própria, depois da terceira: o qualificador entre o nome do relatório e o assunto ("Relatório
        Analítico de Vendas", "Relatório Sintético de Compras") não tira o título de vendas nem o de compras."""
        for titulo in ("Relatório Analítico de Vendas", "Relatório Sintético de Vendas", "Resumo Diário de Vendas",
                       "Relatório Gerencial de Vendas", "Curva ABC Geral de Vendas"):
            with self.subTest(aceito=titulo):
                self.aceito(titulo + "\n" + self.TOTAL_COMPRA)
        for titulo in ("Relatório Analítico de Compras", "Relatório Sintético de Entradas", "Relatório Mensal de Compras",
                       "Curva ABC Geral de Compras", "Resumo Consolidado das Entradas"):
            with self.subTest(recusado=titulo):
                self.recusado(titulo + "\n" + self.CORPO)

    def test_achado_9_rotulo_de_filtro_nao_e_titulo_de_vendas(self):
        for topo in ("Relatório de Compras\nVendas por:;Todas\n",
                     "Relatório de Compras\nHistórico de vendas considerado: 90 dias\n",
                     "Relatório de Compras\nVenda por: Todas\n", "Relatório de Compras\nVendas x Compras:;Sim\n"):
            with self.subTest(recusado=topo):
                self.recusado(topo + self.CORPO)
        # O rótulo do título e o título com dois-pontos que começa pelo nome do relatório, ou por "Vendas por" e
        # "Faturamento por" com o que vem depois, contam.
        for topo in ("Relatório: Vendas por produto\n", "Relatório de Vendas: Outubro/2025\n",
                     "Vendas por Produto: 01/10/2025 a 31/10/2025\n", "Faturamento por Produto: Outubro/2025\n"):
            with self.subTest(aceito=topo):
                self.aceito(topo + self.TOTAL_COMPRA)
        # O total de vendas como parâmetro do relatório de compras não é título.
        self.recusado("Relatório de Compras\nVendas no Período: R$ 1.234,00\n" + self.CORPO)


class TerceiraRevisaoLeituraTests(_Arquivos):
    """Achados 8, 10 e 11: o CPF/CNPJ por palavras, o travessão no desconto e o CPF com prefixo de letras."""

    HOJE = date(2026, 11, 9)

    def _recompra(self, titulo, documentos):
        linhas = "".join(f"{6 + numero:02d}/10/2025;{venda};{documento};{produto};1;10,00\n"
                         for numero, (venda, documento, produto) in enumerate(documentos))
        caminho = self.csv("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                           f"Data;Nr. Doc.;{titulo};Produto;Qtde;Valor Total\n" + linhas)
        return caminho, executar_como_o_worker([caminho])["indicadores_executivos"]["indicadores"]["recompra"]

    def test_achado_8_titulos_do_documento_do_cliente(self):
        a, b, c = CPF_DE_EXEMPLO, _cpf_com_digitos("123456789"), _cpf_com_digitos("987654321")
        # 7 linhas em 6 vendas: A em 3 vendas, B em 2 e C em 1 (dois clientes recorrentes de três).
        documentos = [(1, a, "CAFE"), (1, a, "LEITE"), (2, a, "CAFE"), (3, a, "CAFE"), (4, b, "CAFE"), (5, b, "CAFE"),
                      (6, c, "CAFE")]
        for titulo in ("CPF no Cupom", "CPF na Nota Fiscal", "CPF ou CNPJ", "Nº do CPF", "Número do CPF",
                       "CNPJ/CPF Dest.", "CPF Informado", "CPF NFC-e", "CPF Nota", "Doc. CPF/CNPJ", "CPF/CNPJ Cli.",
                       "Identificação (CPF/CNPJ)", "CPF/CNPJ/ID Estrangeiro", "CPF", "CPF/CNPJ", "CPF do Cliente"):
            with self.subTest(titulo=titulo):
                _caminho, recompra = self._recompra(titulo, documentos)
                self.assertEqual((recompra["status"], recompra["clientes_identificados"],
                                  recompra["clientes_recorrentes"]), ("calculado", 3, 2))
        caminho, _recompra = self._recompra("CPF ou CNPJ", documentos)
        lead = avaliar_como_o_worker([caminho], hoje=self.HOJE)["raio_x"]["indicadores_executivos"]["indicadores"]
        self.assertEqual(lead["recompra"]["clientes_recorrentes"], 2)
        for titulo in ("CPF Operador", "CPF Vendedor", "CPF/CNPJ Emitente", "CNPJ Loja"):
            with self.subTest(fora=titulo):
                self.assertEqual(self._recompra(titulo, documentos)[1]["status"], "requer_cpf_cnpj")

    def test_achado_10_travessao_no_desconto_e_a_celula_vazia(self):
        def indice(sinal):
            texto = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                     "Data;Nr. Doc.;Produto;Qtde;Valor Total;Desconto\n"
                     f"06/10/2025;1;CAFE;1;20,00;3,00\n07/10/2025;2;CAFE;1;20,00;{sinal}\n"
                     f"08/10/2025;3;CAFE;1;20,00;{sinal}\n09/10/2025;4;CAFE;1;28,00;1,00\n")
            return executar_como_o_worker([self.csv(texto)])["indicadores_executivos"]["indicadores"]["indice_descontos"]
        referencia = indice("-")
        self.assertEqual((referencia["status"], referencia["valor"]), ("calculado", 0.0435))  # 4,00 de 92,00
        for sinal in ("—", "‒", "―", "–", "", "0,00"):
            with self.subTest(sinal=sinal):
                self.assertEqual(indice(sinal), referencia)

    def test_achado_11_cpf_com_prefixo_de_letras_e_o_mesmo_cliente(self):
        cpf = "315.942.594-02"
        self.assertIsNotNone(cliente_identificado(cpf))
        for escrito in (f"CPF {cpf}", f"CPF: {cpf}", f"DOC {cpf}", cpf.replace(".", "").replace("-", "")):
            with self.subTest(escrito=escrito):
                self.assertEqual(cliente_identificado(escrito), cliente_identificado(cpf))
        # O CNPJ alfanumérico continua valendo, com e sem a pontuação.
        self.assertIsNotNone(cliente_identificado("12.ABC.345/01DE-35"))
        self.assertEqual(cliente_identificado("12ABC34501DE35"), cliente_identificado("12.ABC.345/01DE-35"))
        self.assertEqual(cliente_identificado("12.abc.345/01de-35"), cliente_identificado("12.ABC.345/01DE-35"))
        # O mesmo CPF com e sem o prefixo no mesmo envio: dois clientes, um recorrente.
        outro = _cpf_com_digitos("123456789")
        _caminho, recompra = self._recompra("CPF", [(1, f"CPF {cpf}", "CAFE"), (2, f"CPF {cpf}", "CAFE"),
                                                    (3, cpf, "CAFE"), (4, outro, "CAFE")])
        self.assertEqual((recompra["clientes_identificados"], recompra["clientes_recorrentes"]), (2, 1))


# =============================================================================
# Quarta revisão independente
# =============================================================================
class QuartaRevisaoTests(_Arquivos):
    """Achados confirmados da quarta revisão: o CPF do fiscal de caixa, o travessão antes do número, a loja do cupom
    com o primeiro item fora da venda, o CNPJ alfanumérico com espaço ou traço tipográfico e a recusa de compras."""

    HOJE = date(2026, 11, 9)
    P = "Periodo: 01/10/2025 a 31/10/2025\n"
    CORPO = P + "Produto;Qtde;Valor Total\nCAFE;10;168,00\n"

    def aceito(self, texto, faturamento=168.0):
        caminho = self.csv(texto)
        self.assertEqual(executar_como_o_worker([caminho])["saude"]["faturamento_total"], faturamento)
        etl_processor.carregar_vendas(caminho)
        if "Periodo" in texto:
            self.assertEqual(avaliar_como_o_worker([caminho], hoje=self.HOJE)["raio_x"]["saude"]["faturamento_total"],
                             faturamento)

    def recusado(self, texto):
        caminho = self.csv(texto)
        with self.assertRaises(etl_processor.ETLRelatorioDeCompras):
            etl_processor.carregar_vendas(caminho)
        self.assertEqual(self.recusa("raio_x_dados", executar_como_o_worker, [caminho]), MENSAGEM_DE_COMPRAS)
        self.assertEqual(self.recusa("raio_x_dados", avaliar_como_o_worker, [caminho], hoje=self.HOJE),
                         MENSAGEM_DE_COMPRAS)

    def _recompra(self, titulos, linhas):
        texto = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                 f"Data;Nr. Doc.;{';'.join(titulos)};Produto;Qtde;Valor Total\n"
                 + "".join(f"{6 + n:02d}/10/2025;{venda};{';'.join(documentos)};CAFE;1;10,00\n"
                           for n, (venda, documentos) in enumerate(linhas)))
        return executar_como_o_worker([self.csv(texto)])["indicadores_executivos"]["indicadores"]["recompra"]

    def test_cpf_do_fiscal_de_caixa_nao_e_o_do_cliente(self):
        a, b, c = CPF_DE_EXEMPLO, _cpf_com_digitos("123456789"), _cpf_com_digitos("987654321")
        fiscal = _cpf_com_digitos("111444777")
        vendas = [(1, a), (1, a), (2, a), (3, a), (4, b), (5, b), (6, c)]
        for titulo in ("CPF do Fiscal", "CPF Fiscal", "Fiscal CPF"):
            with self.subTest(titulo=titulo):
                recompra = self._recompra([titulo, "CPF"], [(v, (fiscal, d)) for v, d in vendas])
                self.assertEqual((recompra["status"], recompra["clientes_identificados"],
                                  recompra["clientes_recorrentes"]), ("calculado", 3, 2))
                sozinho = self._recompra([titulo], [(v, (fiscal,)) for v, _d in vendas])
                self.assertEqual(sozinho["status"], "requer_cpf_cnpj")
        # "Fiscal" ainda completa a nota, o cupom e o documento.
        for titulo in ("CPF na Nota Fiscal", "CPF no Cupom Fiscal", "CNPJ Nota Fiscal"):
            with self.subTest(titulo=titulo):
                self.assertEqual(self._recompra([titulo], [(v, (d,)) for v, d in vendas])["clientes_recorrentes"], 2)

    def test_travessao_sozinho_e_vazio_e_antes_do_numero_fica_como_era(self):
        from collections import Counter
        qualidade = Counter()
        valores = etl_processor._converter_serie_monetaria(
            pd.Series(["—", "R$ —", "(—)", "‒", "―", "— 40,00", "-40,00"]), qualidade)
        self.assertEqual(valores.tolist(), [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -40.0])
        self.assertEqual(qualidade["invalidos"], 1)  # só o "— 40,00", como no e468866
        self.assertIsNone(etl_processor._parse_valor("—"))
        self.assertEqual(etl_processor._parse_valor("— 4"), 4.0)  # como no e468866: não vira devolução

    def test_loja_do_cupom_com_o_primeiro_item_fora_da_venda(self):
        def texto(primeiro, outro_cupom=True):
            linhas = ["06/10/2025;LOJA 1;1001;CAFE;1;20,00;", "06/10/2025;;1001;LEITE;1;10,00;",
                      f"06/10/2025;LOJA 2;1001;CAFE;{primeiro}", "06/10/2025;;1001;LEITE;1;10,00;",
                      "06/10/2025;;1001;PAO;1;3,00;", "07/10/2025;LOJA 1;1002;CAFE;1;20,00;",
                      "07/10/2025;;1002;LEITE;1;10,00;"]
            if outro_cupom:
                linhas += ["08/10/2025;LOJA 2;1003;CAFE;1;20,00;", "08/10/2025;;1003;LEITE;1;10,00;"]
            return ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                    "Data;Loja;Nr. Doc.;Produto;Qtde;Valor Total;Situacao\n" + "\n".join(linhas) + "\n")
        for nome, primeiro in (("cancelado", "1;20,00;CANCELADO"), ("devolvido", "1;20,00;DEVOLVIDO"),
                               ("bonificado", "1;0,00;"), ("quantidade_zero", "0;20,00;")):
            for outro_cupom, esperado in ((True, 4), (False, 3)):
                with self.subTest(primeiro=nome, outro_cupom=outro_cupom):
                    caminho = self.csv(texto(primeiro, outro_cupom))
                    ind = executar_como_o_worker([caminho])["indicadores_executivos"]["indicadores"]
                    self.assertEqual(ind["ticket_medio"]["transacoes"], esperado)
                    lead = avaliar_como_o_worker([caminho], hoje=self.HOJE)["raio_x"]["indicadores_executivos"]
                    self.assertEqual(lead["indicadores"]["ticket_medio"]["transacoes"], esperado)
        # A devolução de outro cupom no meio do 1001 da loja 1 não parte o cupom.
        meio = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
                "Data;Loja;Nr. Doc.;Produto;Qtde;Valor Total;Situacao\n"
                "06/10/2025;LOJA 1;1001;CAFE;1;20,00;\n06/10/2025;;9001;ARROZ;1;5,00;DEVOLVIDO\n"
                "06/10/2025;;1001;LEITE;1;10,00;\n06/10/2025;LOJA 2;1001;CAFE;1;20,00;\n"
                "06/10/2025;;1001;LEITE;1;10,00;\n07/10/2025;LOJA 1;1002;CAFE;1;20,00;\n")
        ind = executar_como_o_worker([self.csv(meio)])["indicadores_executivos"]["indicadores"]
        self.assertEqual(ind["ticket_medio"]["transacoes"], 3)

    def test_cnpj_alfanumerico_com_espaco_ou_traco_tipografico(self):
        mascara = cliente_identificado("12.ABC.345/01DE-35")
        for escrito in ("12 ABC 345 01DE 35", "12.ABC.345/01DE–35", "12.ABC.345/01DE - 35", "12.ABC.345.01DE-35"):
            with self.subTest(escrito=escrito):
                self.assertEqual(cliente_identificado(escrito), mascara)
        # O prefixo de letras continua indo para os algarismos (o achado 11 da terceira revisão).
        self.assertEqual(cliente_identificado("CPF 315.942.594-02"), cliente_identificado("315.942.594-02"))

    def test_venda_consignada_e_titulos_com_periodo_nao_sao_compras(self):
        casos = {
            "consignado_com_data_venda": self.P + "Produto;Fornecedor;Data Entrada;Data Venda;Qtde;Valor Venda\n"
            "VESTIDO AZUL;MARIA;05/09/2025;06/10/2025;1;120,00\nBOLSA;ANA;10/09/2025;07/10/2025;1;48,00\n",
            "acerto_de_consignacao": "Acerto de Consignação\n" + self.P + "Produto;Fornecedor;Data Entrada;Qtde;"
            "Valor Total\nVESTIDO AZUL;MARIA;05/09/2025;1;120,00\nBOLSA;ANA;10/09/2025;1;48,00\n",
            "pecas_vendidas": "Peças Vendidas\n" + self.P + "Produto;Fornecedor;Data Entrada;Qtde;Valor Total\n"
            "VESTIDO AZUL;MARIA;05/09/2025;1;120,00\nBOLSA;ANA;10/09/2025;1;48,00\n",
            "vendas_x_compras_com_periodo": "Vendas x Compras - Período: 01/10/2025 a 31/10/2025\n" + self.P
            + "Produto;Quantidade Vendida;Preco Medio Venda;Quantidade Comprada;Custo Medio;Total Compra\n"
            "CAFE;10;16,80;12;9,00;108,00\n",
            "produtos_vendidos_emitido_em": "Produtos Vendidos - Emitido em 11/11/2026 10:35\n" + self.P
            + "Produto;Qtde;Preco Unitario;Total Compra\nCAFE;10;16,80;100,00\n",
            "relatorio_de_vendas_por": "Relatório de Vendas por:;Produto\n" + self.P
            + "Produto;Qtde;Preco Unitario;Total Compra\nCAFE;10;16,80;100,00\n",
            "total_da_venda_fora_do_dicionario": "Margem por Produto\n" + self.P
            + "Produto;Qtde;Preco Unitario;Total da Venda;Total da Compra\nCAFE;10;16,80;168,00;100,00\n",
            "valor_de_venda_e_total_compra": "Lucratividade por Produto\n" + self.P
            + "Produto;Qtde;Preco Venda;Valor de Venda;Total Compra;Lucro\nCAFE;10;16,80;168,00;100,00;68,00\n",
            "relatorio_de_faturamento_por_produto": "Relatório de Faturamento por Produto\n" + self.P
            + "Produto;Qtde;Preco Unitario;Total Compra\nCAFE;10;16,80;100,00\n",
            "historico_com_cod_cliente": "Histórico de Compras\nCód. Cliente:;123\n" + self.P
            + "Data;Cupom;Produto;Qtde;Valor Total\n06/10/2025;1;CAFE;10;168,00\n",
        }
        for nome, texto in casos.items():
            with self.subTest(aceito=nome):
                self.aceito(texto)

    def test_compras_com_titulo_colado_ao_cabecalho_ou_nome_do_relatorio_antes(self):
        for texto in (
                "Relatorio de compras por produto - periodo de 01/10/2025 a 31/10/2025\nItem;Qtde;Valor Total\n"
                "CAFE;2;20,00\nLEITE;1;10,00\n",
                "Relatório de Pedidos de Compra\n" + self.CORPO, "Relatório de Recebimento de Mercadorias\n" + self.CORPO,
                "Relatório Analítico de Notas Fiscais de Entrada\n" + self.CORPO,
                "Relatório de Compras\nVendas no Período;R$ 1.234,00\n" + self.CORPO):
            with self.subTest(recusado=texto.split("\n")[0]):
                self.recusado(texto)


# =============================================================================
# Ingestão em lote: a âncora temporal e as tabelas auxiliares
# =============================================================================
class IngestaoEmLoteTests(_Arquivos):
    """A venda item a item com a data de cada venda (a âncora) completada pelos outros arquivos do envio: custo,
    estoque e cadastro pelo produto; desconto, cliente e hora pelo número da venda; a trava contra a duplicação."""

    HOJE = date(2026, 11, 9)
    ANCORA = ("Relatorio de vendas item a item\nPeriodo: 01/10/2025 a 31/10/2025\n"
              "Data;Nr. Doc.;Produto;Qtde;Valor Total\n"
              "06/10/2025;P1-0001;CAFE 500G;1;20,00\n06/10/2025;P1-0001;LEITE 1L;2;10,00\n"
              "11/10/2025;P1-0002;CAFE 500G;2;40,00\n20/10/2025;P1-0003;CAFE 500G;1;20,00\n"
              "25/10/2025;P1-0004;LEITE 1L;3;15,00\n")
    # O resumo do ERP conta o pedido e a nota em dobro: o custo unitário (custo / quantidade) não muda com isso.
    RESUMO = ("Giro de vendas por produto\nPeriodo: 01/10/2025 a 31/10/2025\n"
              "Codigo;Produto;Qtde;Valor Total;Total Custo;Estoque\n"
              "1;CAFE 500G;8;160,00;96,00;30\n2;LEITE 1L;10;50,00;35,00;12\n3;ACUCAR 1KG;0;0,00;0,00;7\n")
    POSICAO = ("Posicao de Estoque\nCodigo;Descricao;Saldo Atual;Custo Medio\n"
               "1;CAFE 500G;25;11,00\n2;LEITE 1L;9;3,00\n4;ARROZ 5KG;4;20,00\n")
    PRECOS = ("Lista de Precos\nCodigo;Descricao;Preco de Venda\n"
              "1;CAFE 500G;20,00\n2;LEITE 1L;5,00\n5;FEIJAO 1KG;9,00\n6;OLEO 900ML;8,00\n")
    DOCUMENTOS = ("Vendas Resumida por Pedido\nPeriodo: 01/10/2025 a 31/10/2025\n"
                  "Nr. Pedido;Data;CPF/CNPJ;Desconto;Valor Total\n"
                  f"P1-0001;06/10/2025;{CPF_DE_EXEMPLO};3,00;30,00\nP1-0002;11/10/2025;;0,00;40,00\n"
                  f"P1-0003;20/10/2025;{CPF_DE_EXEMPLO};1,00;20,00\nP1-0004;25/10/2025;{CNPJ_DE_EXEMPLO};0,00;15,00\n")
    COMPRAS = ("Relatorio de Compras\nPeriodo: 01/10/2025 a 31/10/2025\nFornecedor;Nota Fiscal;Data Entrada;Valor Total\n"
               "ALFA;123;05/10/2025;500,00\n")

    def _lote(self, *textos):
        caminhos = [self.csv(texto) for texto in textos]
        return executar_como_o_worker(caminhos), avaliar_como_o_worker(caminhos, hoje=self.HOJE)

    def test_ancora_e_relatorio_por_produto_dao_os_21_indicadores_sem_somar_o_resumo(self):
        cliente, lead = self._lote(self.ANCORA, self.RESUMO)
        for resultado in (cliente, lead["raio_x"]):
            with self.subTest(rota="lead" if resultado is not cliente else "cliente"):
                self.assertEqual(resultado["saude"]["faturamento_total"], 105.0)  # só a âncora
                no = resultado["indicadores_executivos"]
                self.assertEqual(no["modo"], "analitico")
                ind = no["indicadores"]
                self.assertEqual(ind["ticket_medio"]["transacoes"], 4)
                # Custo: CAFE 4 x 12,00 e LEITE 5 x 3,50 = 65,50.
                self.assertEqual((ind["margem"]["status"], ind["margem"]["bruta_valor"]), ("calculado", 39.5))
                self.assertEqual(ind["cmv"]["valor"], 65.5)
                # Estoque: CAFE 30 x 12,00 + LEITE 12 x 3,50; o ACUCAR (7, sem custo) fica de fora do valor.
                self.assertEqual(ind["giro_estoque"]["estoque_a_custo"], 402.0)
                self.assertEqual(ind["giro_estoque"]["itens_com_estoque_sem_custo"], 1)
                self.assertEqual((ind["mix"]["skus_vendidos"], ind["mix"]["skus_cadastrados"]), (2, 3))
                self.assertIn("O arquivo 2 entrou como tabela auxiliar", " ".join(resultado["avisos"]))
                self.assertIn("As margens são estimadas", " ".join(resultado["avisos"]))
        linhas = [dict(zip(lead["linhas"]["colunas"], valores)) for valores in lead["linhas"]["valores"]]
        self.assertEqual({l["descricao_produto"]: l["estoque_final"] for l in linhas if l["descricao_produto"] != "ACUCAR 1KG"},
                         {"CAFE 500G": 30, "LEITE 1L": 12})
        # Sem a âncora, os dois arquivos continuam sendo relatórios de vendas, como antes.
        self.assertEqual(executar_como_o_worker([self.csv(self.RESUMO)])["saude"]["faturamento_total"], 210.0)

    def test_posicao_de_estoque_e_lista_de_precos(self):
        cliente, _lead = self._lote(self.ANCORA, self.POSICAO)
        ind = cliente["indicadores_executivos"]["indicadores"]
        self.assertEqual(cliente["saude"]["faturamento_total"], 105.0)
        self.assertEqual(ind["cmv"]["valor"], 59.0)  # CAFE 4 x 11,00 + LEITE 5 x 3,00
        self.assertEqual(ind["giro_estoque"]["estoque_a_custo"], 382.0)  # 25 x 11 + 9 x 3 + 4 x 20 (o ARROZ sem venda)
        # O relatório por produto vence no custo (o do período) e a posição de estoque no saldo.
        cliente, _lead = self._lote(self.ANCORA, self.RESUMO, self.POSICAO)
        ind = cliente["indicadores_executivos"]["indicadores"]
        self.assertEqual(ind["cmv"]["valor"], 65.5)
        self.assertEqual(ind["giro_estoque"]["estoque_a_custo"], 25 * 12.0 + 9 * 3.5 + 4 * 20.0)
        # A lista de preços é o cadastro: o mix sobre os cadastrados.
        cliente, _lead = self._lote(self.ANCORA, self.PRECOS)
        mix = cliente["indicadores_executivos"]["indicadores"]["mix"]
        self.assertEqual((mix["skus_vendidos"], mix["skus_cadastrados"], mix["percentual_sobre_cadastrados"]), (2, 4, 0.5))
        self.assertEqual(cliente["indicadores_executivos"]["indicadores"]["margem"]["status"], "requer_custo")

    def test_resumo_por_pedido_da_desconto_cliente_e_recompra(self):
        cliente, lead = self._lote(self.ANCORA, self.DOCUMENTOS)
        for no in (cliente["indicadores_executivos"], lead["raio_x"]["indicadores_executivos"]):
            ind = no["indicadores"]
            self.assertEqual(cliente["saude"]["faturamento_total"], 105.0)
            self.assertEqual((ind["indice_descontos"]["status"], ind["indice_descontos"]["descontos"]), ("calculado", 4.0))
            self.assertEqual((ind["recompra"]["clientes_identificados"], ind["recompra"]["clientes_recorrentes"]), (2, 1))
        # O resumo que não casa com a venda fica fora, com o aviso.
        outro = self.DOCUMENTOS.replace("P1-", "X9-")
        cliente, _lead = self._lote(self.ANCORA, outro)
        self.assertEqual(cliente["indicadores_executivos"]["indicadores"]["indice_descontos"]["status"],
                         "requer_coluna_de_desconto")
        self.assertIn("ficou fora", " ".join(cliente["avisos"]))

    def test_relatorio_de_compras_entra_no_lote_fora_das_contas(self):
        cliente, lead = self._lote(self.ANCORA, self.COMPRAS)
        self.assertEqual(cliente["saude"]["faturamento_total"], 105.0)
        self.assertIn("é um relatório de compras", " ".join(cliente["avisos"]))
        self.assertEqual(lead["raio_x"]["saude"]["faturamento_total"], 105.0)
        # Sem a âncora, continua recusado com a mensagem fixa.
        self.assertEqual(self.recusa("raio_x_dados", executar_como_o_worker, [self.csv(self.RESUMO), self.csv(self.COMPRAS)]),
                         MENSAGEM_DE_COMPRAS.replace("Arquivo 1", "Arquivo 2"))

    def test_sem_ancora_a_tabela_auxiliar_e_recusada_como_antes(self):
        mensagem = self.recusa("raio_x_dados", executar_como_o_worker, [self.csv(self.RESUMO), self.csv(self.POSICAO)])
        self.assertTrue(mensagem.startswith("Arquivo 2: não foi possível identificar produto"), mensagem)
        mensagem = self.recusa("raio_x_dados", executar_como_o_worker, [self.csv(self.PRECOS), self.csv(self.RESUMO)])
        self.assertTrue(mensagem.startswith("Arquivo 1: não foi possível identificar produto"), mensagem)

    def test_outra_loja_ou_outro_periodo_continua_somando_como_venda(self):
        com_loja = self.ANCORA.replace("Data;Nr. Doc.;", "Data;Loja;Nr. Doc.;").replace(";P1-", ";LOJA 1;P1-")
        resumo_da_loja_2 = self.RESUMO.replace("Codigo;", "Loja;Codigo;").replace("\n1;", "\nLOJA 2;1;").replace(
            "\n2;", "\nLOJA 2;2;").replace("\n3;", "\nLOJA 2;3;")
        cliente, _lead = None, None
        cliente = executar_como_o_worker([self.csv(com_loja), self.csv(resumo_da_loja_2)])
        self.assertEqual(cliente["saude"]["faturamento_total"], 315.0)  # 105 + 210: o resumo da loja 2 é venda
        setembro = self.RESUMO.replace("01/10/2025 a 31/10/2025", "01/09/2025 a 30/09/2025")
        cliente = executar_como_o_worker([self.csv(self.ANCORA), self.csv(setembro)])
        self.assertEqual(cliente["saude"]["faturamento_total"], 315.0)  # fora do período da âncora: venda

    def test_trava_contra_a_duplicacao_do_faturamento(self):
        repetido = pd.DataFrame({"custo_unitario": [12.0, 13.0], "estoque": [1.0, 2.0], "nome": ["CAFE 500G"] * 2,
                                 "codigo": [None, None]}, index=["CAFE 500G", "CAFE 500G"])
        with patch.object(raio_x, "_combinar_auxiliares", return_value=(repetido, 0)):
            for executor, extra in ((executar_como_o_worker, {}), (avaliar_como_o_worker, {"hoje": self.HOJE})):
                with self.subTest(rota=executor.__name__):
                    mensagem = self.recusa("raio_x_dados", executor, [self.csv(self.ANCORA), self.csv(self.RESUMO)],
                                           **extra)
                    self.assertEqual(mensagem, "Falha de Contenção de Dados: Duplicação de Faturamento Detectada")
        # A conferência final, no centavo: uma linha a mais da venda, ou um centavo a mais, interrompe.
        quadro = pd.DataFrame({"valor_venda": [10.0, 20.0], "_auxiliar": [False, False]})
        raio_x._conferir_contencao(quadro, 2, 30.0)
        for linhas, faturamento in ((1, 30.0), (2, 29.98)):
            with self.subTest(linhas=linhas, faturamento=faturamento):
                mensagem = self.recusa("raio_x_dados", raio_x._conferir_contencao, quadro, linhas, faturamento)
                self.assertEqual(mensagem, "Falha de Contenção de Dados: Duplicação de Faturamento Detectada")

    def test_leiautes_reais_do_erp_opcao_27_com_o_resumo_por_produto(self):
        from tests.leiautes_erp_support import gravar_xlsx, relatorio_detalhado, relatorio_por_produto
        detalhado = gravar_xlsx(*relatorio_detalhado()[:3], self.pasta / "detalhado.xlsx")
        por_produto = gravar_xlsx(*relatorio_por_produto()[:3], self.pasta / "por_produto.xlsx")
        sozinho = executar_como_o_worker([detalhado])
        lote = executar_como_o_worker([detalhado, por_produto])
        self.assertEqual(lote["saude"]["faturamento_total"], sozinho["saude"]["faturamento_total"])
        ind_sozinho = sozinho["indicadores_executivos"]["indicadores"]
        ind = lote["indicadores_executivos"]["indicadores"]
        self.assertEqual((ind_sozinho["margem"]["status"], ind_sozinho["giro_estoque"]["status"]),
                         ("requer_custo", "requer_estoque"))
        self.assertEqual((ind["margem"]["status"], ind["giro_estoque"]["status"], ind["gmroi"]["status"]),
                         ("calculado", "calculado", "calculado"))
        self.assertEqual(ind["ticket_medio"], ind_sozinho["ticket_medio"])
        self.assertTrue(indicadores_executivos.valido(lote["indicadores_executivos"]))


# =============================================================================
# 8. Lead: gravação, expurgo e migração
# =============================================================================
from tests.test_avaliacao_cenario import CONTATO, _BancoCase  # noqa: E402  (precisa do banco de teste)


class LeadTests(_BancoCase):
    def _registrar(self, numero=1):
        import avaliacao_cenario
        resultado = avaliar_como_o_worker(self.caminhos(*(texto.encode("utf-8") for texto in ACUMULADO)))
        contato = {**CONTATO, "email": f"lead-{numero}@example.invalid"}
        lead = avaliacao_cenario.registrar(resultado, contato=contato, registrado_por=self.admin_id, meses_prata=24)
        self.db.session.commit()
        return lead, resultado

    def test_indicadores_gravados_no_lead(self):
        lead, resultado = self._registrar()
        self.assertEqual(json.loads(lead.indicadores_executivos_json), resultado["raio_x"]["indicadores_executivos"])
        # A contagem de venda abaixo do custo da triagem não conta o ITEM GENERICO (custo a confirmar).
        self.assertEqual(lead.itens_venda_abaixo_custo, 1)

    def test_tela_do_lead_mostra_os_kpis_da_etapa_2_tambem_na_versao_anterior(self):
        """Achado da segunda revisão: o lead triado pela 11.3 voltava aos rótulos de antes da Etapa 2."""
        from models import LeadAvaliacao
        lead, _ = self._registrar()
        self.login("admin-test@example.invalid")
        atual = self.client.get(f"/admin/leads/{lead.id}").get_data(as_text=True)
        self.assertIn("Itens com omissão no ERP", atual)
        self.assertIn("fora o ano de custo a confirmar", atual)
        registro = self.db.session.get(LeadAvaliacao, lead.id)
        registro.versao_triagem = "triagem-2026-11.3"
        self.db.session.commit()
        anterior = self.client.get(f"/admin/leads/{lead.id}").get_data(as_text=True)
        self.assertIn("Itens com omissão no ERP", anterior)
        self.assertIn("Itens com alguma venda abaixo do custo", anterior)
        self.assertNotIn("fora o ano de custo a confirmar", anterior)  # a 11.3 contava esse ano

    def test_definicoes_do_diagnostico_json_seguem_a_versao_gravada(self):
        """Achado 16 da terceira revisão: o JSON do lead triado pela 11.3 dizia a regra da 11.4."""
        from models import LeadAvaliacao
        lead, _ = self._registrar()
        self.login("admin-test@example.invalid")
        chaves = ("vendas_abaixo_do_custo", "potencial_recuperavel", "marcas")
        atual = self.client.get(f"/admin/leads/{lead.id}/diagnostico.json").get_json()["definicoes"]
        self.assertTrue(all("custo a confirmar" in atual[chave] for chave in chaves), atual)
        registro = self.db.session.get(LeadAvaliacao, lead.id)
        registro.versao_triagem = "triagem-2026-11.3"
        self.db.session.commit()
        anterior = self.client.get(f"/admin/leads/{lead.id}/diagnostico.json").get_json()["definicoes"]
        self.assertFalse(any("custo a confirmar" in anterior[chave] for chave in chaves), anterior)

    def test_lead_de_antes_da_etapa_2_tem_as_marcas_da_versao_no_json(self):
        """Quarta revisão: na 11.2 (ou sem versão), a marca era o preço médio abaixo do custo médio, sem a omissão no
        ERP: o diagnostico.json diz isso, como a tela."""
        from models import LeadAvaliacao
        lead, _ = self._registrar()
        self.login("admin-test@example.invalid")
        for versao in ("triagem-2026-11.2", None):
            with self.subTest(versao=versao):
                registro = self.db.session.get(LeadAvaliacao, lead.id)
                registro.versao_triagem = versao
                self.db.session.commit()
                dados = self.client.get(f"/admin/leads/{lead.id}/diagnostico.json").get_json()
                self.assertIn("preço médio abaixo do custo médio", dados["definicoes"]["marcas"])
                self.assertNotIn("omissão no ERP.", dados["definicoes"]["marcas"])
                marcas = dados["visualizacoes"]["marcas"]
                self.assertEqual(marcas["rotulos"], ["Venda sem custo", "Preço médio abaixo do custo"])
                self.assertEqual(len(marcas["series"][0]["valores"]), 2)
        registro = self.db.session.get(LeadAvaliacao, lead.id)
        registro.versao_triagem = triagem_cenario.VERSAO_TRIAGEM
        self.db.session.commit()
        atual = self.client.get(f"/admin/leads/{lead.id}/diagnostico.json").get_json()
        self.assertEqual(atual["visualizacoes"]["marcas"]["rotulos"],
                         ["Venda sem custo", "Venda abaixo do custo", "Omissão no ERP"])

    def test_expurgo_apaga_os_indicadores_com_as_linhas(self):
        import leads_avaliacao
        from horario import agora_utc
        from models import BrainstormingDadosPdv, LeadAvaliacao
        from sqlalchemy import select, update
        vencido, _ = self._registrar(1)
        recente, _ = self._registrar(2)
        convertido, _ = self._registrar(3)
        convertido.status = "convertido"
        self.db.session.commit()
        ids = {"vencido": vencido.id, "recente": recente.id, "convertido": convertido.id}
        for nome in ("vencido", "convertido"):
            self.db.session.execute(update(BrainstormingDadosPdv).where(BrainstormingDadosPdv.lead_id == ids[nome])
                                    .values(criado_em=agora_utc() - timedelta(days=31)))
        self.db.session.commit()
        resultado = leads_avaliacao.expurgar_dados_pdv(self.db.session, agora=agora_utc())
        self.db.session.commit()
        self.assertEqual(resultado.leads_expirados, 1)
        estado = {lead.id: (lead.status, lead.indicadores_executivos_json is None)
                  for lead in self.db.session.scalars(select(LeadAvaliacao))}
        self.assertEqual(estado[ids["vencido"]], ("expirado", True))
        self.assertEqual(estado[ids["recente"]], ("concluido", False))
        self.assertEqual(estado[ids["convertido"]], ("convertido", False))

    def test_downgrade_recusa_com_indicadores_e_desce_sem_eles(self):
        from tests.test_leads_avaliacao import REVISAO, REVISAO_ETAPA_3, DowngradeTests
        from sqlalchemy import inspect, text
        migrar = DowngradeTests._migrar
        self._registrar()
        with self.assertLogs("flask_migrate", "ERROR") as registro, self.assertRaises(SystemExit):
            migrar(self, "downgrade", REVISAO_ETAPA_3)
        self.assertIn("Downgrade dos indicadores executivos do lead recusado", registro.output[0])
        self.assertEqual(self.db.session.scalar(text("SELECT version_num FROM alembic_version")), REVISAO)
        self.db.session.execute(text("UPDATE lead_avaliacao SET indicadores_executivos_json = NULL"))
        self.db.session.commit()
        linhas = self.db.session.scalar(text("SELECT count(*) FROM brainstorming_dados_pdv"))
        migrar(self, "downgrade", REVISAO_ETAPA_3)
        self.assertNotIn("indicadores_executivos_json",
                         {c["name"] for c in inspect(self.db.engine).get_columns("lead_avaliacao")})
        # Sem o modo batch, as linhas de PDV não saem em cascata.
        self.assertEqual(self.db.session.scalar(text("SELECT count(*) FROM brainstorming_dados_pdv")), linhas)
        migrar(self, "upgrade", "head")
        self.assertEqual(self.db.session.scalar(text("SELECT version_num FROM alembic_version")), REVISAO)


if __name__ == "__main__":
    unittest.main()
