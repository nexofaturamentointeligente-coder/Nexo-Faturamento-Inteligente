"""Avaliação de Cenário (lead): motor no processo isolado, contrato, triagem, gravação e rotas.

  1. Motor (avaliacao_motor): a matriz produto x ano sai das mesmas regras do Raio-X, com a regra
     do ano (até hoje: período futuro recusado, mês corrente contado até hoje), a recusa de
     períodos sobrepostos e os tetos de tamanho e de valor.
  2. Equivalência: o resumo refeito a partir da staging gravada bate com o Raio-X dos mesmos
     arquivos (índice, faturamento, itens sem custo, margem negativa, abaixo do custo, estoque
     negativo); o recuperável difere só em centavos, pelo arredondamento por linha.
  3. Contrato do processo isolado (etl_runtime.validar_avaliacao) e isolamento do módulo.
  4. Quantização das linhas do worker (2 casas no dinheiro, 3 na quantidade e no estoque, a categoria
     do índice). A triagem pura (marcas, blocos, limiares, ritmo e fronteiras) e o diagnóstico estão
     em tests/test_triagem_cenario.py.
  5. Gravação (avaliacao_cenario): lead, linhas e resumo numa transação; reenvio; retriagem;
     expurgo do lote inteiro; tetos das colunas.
  6. Rotas do consultor (blueprints/leads.py): só ADMIN em todas, autorização do lead, 503 com
     Retry-After na vaga ocupada, 413 em português, expurgo pela tela e transação encerrada antes
     do processo isolado.

O worker real exige Linux: aqui o motor roda no próprio processo, com a mesma validação, a mesma
ida e volta em JSON e a mesma conferência de contrato (raio_x_support.avaliar_como_o_worker).
Os dados são fictícios.
"""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import func, select, text

import avaliacao_motor
from etl_inputs import ETLInputError
from etl_limits import ETLLimits
import etl_runtime
import raio_x
import triagem_cenario
from tests.deploy_support import ROOT, DeployTestCase
from tests.raio_x_support import avaliar_como_o_worker, executar_como_o_worker, relatorio_do_mes

NOV24 = relatorio_do_mes(2024, 11, ["1;Cafe 500g;10;20,00;200,00;150,00", "2;Acucar 1kg;5;6,00;30,00;",
                                    "3;Leite 1L;20;5,00;100,00;110,00"])
DEZ24 = relatorio_do_mes(2024, 12, ["1;Cafe 500g;10;20,00;200,00;150,00", "2;Acucar 1kg;5;6,00;30,00;",
                                    "3;Leite 1L;20;5,00;100,00;90,00"])
JAN25 = relatorio_do_mes(2025, 1, ["1;Café 500g;8;20,00;150,00;120,00", "3;Leite 1L;5;5,00;25,00;20,00"])
FEV25 = relatorio_do_mes(2025, 2, ["1;Café 500g;4;20,00;80,00;60,00", "3;Leite 1L;5;5,00;25,00;20,00",
                                   "4;Arroz 5kg;2;27,90;55,80;44,00"])
# nov-dez/2024 e jan-fev/2025: os dois anos são mensais e não têm mês em comum, então a queda de ritmo
# não é avaliada (a regra que evita a sazonalidade virar parada).
LOTE = (NOV24, DEZ24, JAN25, FEV25)
NOV25 = relatorio_do_mes(2025, 11, ["1;Café 500g;8;20,00;150,00;120,00", "3;Leite 1L;5;5,00;25,00;20,00"])
DEZ25 = relatorio_do_mes(2025, 12, ["1;Café 500g;4;20,00;80,00;60,00", "3;Leite 1L;5;5,00;25,00;20,00",
                                   "4;Arroz 5kg;2;27,90;55,80;44,00"])
# Os mesmos meses nos dois anos (nov-dez): o Leite cai 75% no mesmo período e vai para a Urgência.
LOTE_MESMOS_MESES = (NOV24, DEZ24, NOV25, DEZ25)
NOMES = ("Cafe", "Café", "Leite", "Acucar", "Arroz")


def _relatorio(ano, mes, linhas, *, periodo=None):
    fim = monthrange(ano, mes)[1]
    periodo = periodo or f"01/{mes:02d}/{ano} a {fim:02d}/{mes:02d}/{ano}"
    cabecalho = ("Relatorio de vendas por produto\nPeriodo: " + periodo + "\n"
                 "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque\n")
    return (cabecalho + "".join(linha + "\n" for linha in linhas)).encode("utf-8")


def lote_de_dois_anos(produtos=40):
    """24 relatórios mensais (2024 e 2025) com custo ausente, venda abaixo do custo, variação de
    preço, estoque negativo, conflito de cadastro e produtos que param ou começam a vender."""
    meses = []
    for indice in range(24):
        ano, mes = 2024 + indice // 12, indice % 12 + 1
        linhas = []
        for p in range(produtos):
            if (p % 9 == 0 and ano == 2025) or (p % 11 == 0 and ano == 2024):
                continue  # parou de vender em 2025 ou começou em 2025
            quantidade = 3 + (p * 7 + indice * 5) % 17
            preco = round(4.5 + p * 1.37, 2)
            praticado = preco if (p + indice) % 6 else round(preco * 0.9, 2)
            total = round(quantidade * praticado, 2)
            custo = "" if p % 8 == 0 else f"{round(quantidade * preco * (1.08 if p % 13 == 0 else 0.7), 2):.2f}"
            estoque = -2 if (p % 10 == 0 and indice % 4 == 0) else 5
            linhas.append(f"{100 + p};Produto {p};{quantidade};{preco:.2f};{total:.2f};{custo};{estoque}"
                          .replace(".", ","))
        if indice == 7:
            linhas.append(f"{100 + 5};Produto 5 Variante;2;10,00;20,00;14,00;1")  # mesmo código, outro nome
        meses.append(_relatorio(ano, mes, linhas))
    return meses


class _Arquivos(unittest.TestCase):
    def setUp(self):
        pasta = tempfile.TemporaryDirectory(prefix="nexo-avaliacao-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)

    def caminhos(self, *conteudos):
        saida = []
        for numero, conteudo in enumerate(conteudos, 1):
            caminho = self.pasta / f"arquivo_{len(list(self.pasta.iterdir())) + 1:02d}.csv"
            caminho.write_bytes(conteudo)
            saida.append(str(caminho))
        return saida


def _linhas(resultado):
    return [dict(zip(resultado["linhas"]["colunas"], valores)) for valores in resultado["linhas"]["valores"]]


# =============================================================================
# 1. Motor
# =============================================================================
class MotorTests(_Arquivos):
    def test_matriz_produto_ano_com_as_somas_do_raio_x(self):
        caminhos = self.caminhos(*LOTE)
        resultado = avaliar_como_o_worker(caminhos)
        linhas = _linhas(resultado)
        chave = {nome: avaliacao_motor.chave_produto(nome) for nome in ("CAFE 500G", "ACUCAR 1KG", "LEITE 1L", "ARROZ 5KG")}
        por_ano = {(l["ano_exercicio"], l["chave_produto"]): l for l in linhas}
        self.assertEqual(len(linhas), 6)
        cafe_2024, cafe_2025 = por_ano[(2024, chave["CAFE 500G"])], por_ano[(2025, chave["CAFE 500G"])]
        self.assertEqual((cafe_2024["descricao_produto"], cafe_2025["descricao_produto"]), ("Cafe 500g", "Café 500g"))
        self.assertEqual((cafe_2024["quantidade"], cafe_2024["valor_venda_bruto"], cafe_2024["custo_total"]),
                         (20.0, 400.0, 300.0))
        # Janeiro: 8 un a R$ 150,00 com tabela de R$ 20,00: R$ 10,00 de vazamento de preço.
        self.assertEqual((cafe_2025["quantidade"], cafe_2025["valor_venda_bruto"], cafe_2025["vazamento_preco"],
                          cafe_2025["recuperavel"], cafe_2025["variacao"], cafe_2025["meses"]),
                         (12.0, 230.0, 10.0, 10.0, True, 2))
        leite_2024 = por_ano[(2024, chave["LEITE 1L"])]
        self.assertEqual((leite_2024["perda"], leite_2024["recuperavel"], leite_2024["linhas"]), (10.0, 10.0, 2))
        acucar = por_ano[(2024, chave["ACUCAR 1KG"])]
        self.assertEqual((acucar["valor_venda_bruto"], acucar["faturamento_com_custo"], acucar["quantidade_com_custo"]),
                         (60.0, 0.0, 0.0))
        self.assertEqual(por_ano[(2025, chave["ARROZ 5KG"])]["meses"], 1)
        self.assertEqual(cafe_2024["codigo_produto"], "1")
        # O Raio-X que vai junto é o mesmo do cliente, e a matriz soma o mesmo faturamento.
        self.assertEqual(resultado["raio_x"], executar_como_o_worker(caminhos))
        self.assertAlmostEqual(sum(l["valor_venda_bruto"] for l in linhas),
                               resultado["raio_x"]["saude"]["faturamento_total"], places=6)
        self.assertEqual(resultado["meta"]["arquivos_por_ano"], {"2024": 2, "2025": 2})
        # Cobertura: novembro e dezembro (61 dias), janeiro e fevereiro (59 dias), todos mensais.
        self.assertEqual(resultado["meta"]["dias_por_ano"], {"2024": 61, "2025": 59})
        self.assertEqual(resultado["meta"]["relatorios_mensais_por_ano"], {"2024": True, "2025": True})
        self.assertEqual((resultado["meta"]["arquivos"], resultado["meta"]["linhas_lidas"]), (4, 11))
        self.assertEqual(resultado["versao"], avaliacao_motor.VERSAO_REGRAS_LEAD)

    def test_conflito_e_estoque_negativo_valem_para_o_produto_no_ano(self):
        # Em janeiro o produto P só aparece como bonificação (valor zero), com dois códigos e estoque
        # negativo; em fevereiro ele vende. As marcas são do produto no ano, como no Raio-X.
        janeiro = _relatorio(2026, 1, ["1;Produto P;2;5,00;0,00;8,00;-3", "9;Produto P;1;5,00;0,00;4,00;-3",
                                       "2;Outro;1;5,00;5,00;3,00;1"])
        fevereiro = _relatorio(2026, 2, ["1;Produto P;3;5,00;15,00;12,00;2"])
        resultado = avaliar_como_o_worker(self.caminhos(janeiro, fevereiro))
        produto_p = next(l for l in _linhas(resultado) if l["chave_produto"] == avaliacao_motor.chave_produto("PRODUTO P"))
        self.assertEqual((produto_p["conflito_cadastral"], produto_p["estoque_negativo"]), (True, True))
        self.assertGreater(resultado["raio_x"]["saude"]["inconsistencias"]["estoque_negativo"], 0)

    def test_nome_so_com_espacos_no_comeco_vira_a_chave_do_produto(self):
        espacos = _relatorio(2026, 3, [f"1;{' ' * 205}Cafe;2;5,00;10,00;6,00;1"])
        linha = _linhas(avaliar_como_o_worker(self.caminhos(espacos)))[0]
        self.assertEqual(linha["descricao_produto"], "CAFE")
        self.assertEqual(linha["chave_produto"], avaliacao_motor.chave_produto("CAFE"))

    def test_meta_nao_leva_nome_de_produto(self):
        resultado = avaliar_como_o_worker(self.caminhos(*LOTE))
        meta = json.dumps(resultado["meta"], ensure_ascii=False)
        for nome in NOMES:
            self.assertNotIn(nome, meta)

    def test_regra_do_ano_e_periodos_sobrepostos(self):
        casos = {
            "avaliacao_sem_periodo": (b"Codigo;Produto;Qtde;Valor Total\n1;Cafe;1;10,00\n",),
            "avaliacao_periodo_entre_anos": (_relatorio(2025, 12, ["1;Cafe;1;10,00;10,00;5,00;1"],
                                                        periodo="01/12/2025 a 31/01/2026"),),
            "avaliacao_ano": (_relatorio(2025, 1, ["1;Cafe;1;10,00;10,00;5,00;1"], periodo="01/01/99 a 31/01/99"),),
            "avaliacao_ano ": (_relatorio(1999, 1, ["1;Cafe;1;10,00;10,00;5,00;1"]),),
            "avaliacao_sobreposicao": (JAN25, _relatorio(2025, 1, ["1;Cafe;1;10,00;10,00;5,00;1"],
                                                         periodo="15/01/2025 a 15/02/2025")),
        }
        for codigo, conteudos in casos.items():
            with self.subTest(codigo=codigo), self.assertRaises(ETLInputError) as erro:
                avaliar_como_o_worker(self.caminhos(*conteudos))
            self.assertEqual(erro.exception.code, codigo.strip())
        with self.assertRaises(ETLInputError) as erro:
            avaliar_como_o_worker(self.caminhos(*casos["avaliacao_sobreposicao"]))
        self.assertTrue(erro.exception.safe_message.startswith("Os arquivos "))

    def test_teto_de_saida_recusa_com_mensagem_propria(self):
        with self.assertRaises(ETLInputError) as erro:
            avaliar_como_o_worker(self.caminhos(*LOTE), ETLLimits(max_output_bytes=6_000))
        self.assertEqual(erro.exception.code, "avaliacao_grande")

    def test_valor_que_a_coluna_nao_guarda_e_recusado(self):
        enorme = _relatorio(2025, 3, ["1;Cafe;100000000000;1,00;100000000000,00;;1"])
        with self.assertRaises(ETLInputError) as erro:
            avaliar_como_o_worker(self.caminhos(enorme))
        self.assertEqual(erro.exception.code, "avaliacao_valor")

    def test_mes_corrente_cobre_so_os_dias_que_ja_passaram(self):
        # Em 08/11/2026, o relatório de novembro com o mês inteiro no cabeçalho cobre 8 dias.
        outubro = _relatorio(2026, 10, ["1;Cafe;31;10,00;310,00;200,00;1"])
        novembro = _relatorio(2026, 11, ["1;Cafe;8;10,00;80,00;50,00;1"])
        no_meio = avaliar_como_o_worker(self.caminhos(outubro, novembro), hoje=date(2026, 11, 8))
        self.assertEqual(no_meio["meta"]["dias_por_ano"], {"2026": 39})
        self.assertEqual(no_meio["meta"]["relatorios_mensais_por_ano"], {"2026": True})
        # O mesmo envio, lido depois do fim do mês, cobre o mês inteiro.
        depois = avaliar_como_o_worker(self.caminhos(outubro, novembro), hoje=date(2026, 12, 15))
        self.assertEqual(depois["meta"]["dias_por_ano"], {"2026": 61})

    def test_periodo_que_comeca_depois_de_hoje_e_recusado(self):
        hoje = date(2026, 11, 8)
        linha = ["1;Cafe;1;10,00;10,00;5,00;1"]
        dezembro = _relatorio(2026, 12, linha)
        amanha = _relatorio(2026, 11, linha, periodo="09/11/2026 a 30/11/2026")
        mensagens = []
        for conteudo in (dezembro, amanha):
            with self.assertRaises(ETLInputError) as erro:
                avaliar_como_o_worker(self.caminhos(conteudo), hoje=hoje)
            self.assertEqual(erro.exception.code, "avaliacao_periodo_futuro")
            mensagens.append(erro.exception.safe_message)
        self.assertTrue(mensagens[0].startswith("Arquivo 1: o período começa em 01/12/2026, depois de hoje."))
        # Começar hoje vale: um dia coberto.
        hoje_mesmo = _relatorio(2026, 11, linha, periodo="08/11/2026 a 30/11/2026")
        self.assertEqual(avaliar_como_o_worker(self.caminhos(hoje_mesmo), hoje=hoje)["meta"]["dias_por_ano"],
                         {"2026": 1})

    def test_bordas_do_ano_e_da_sobreposicao(self):
        hoje = date(2026, 11, 8)
        linha = ["1;Cafe;1;10,00;10,00;5,00;1"]
        aceitos = {
            "ano corrente": (_relatorio(2026, 1, linha),),
            "primeiro ano aceito": (_relatorio(2000, 1, linha),),
            "períodos vizinhos": (_relatorio(2025, 1, linha), _relatorio(2025, 2, linha)),
        }
        for caso, conteudos in aceitos.items():
            with self.subTest(caso=caso):
                resultado = avaliar_como_o_worker(self.caminhos(*conteudos), hoje=hoje)
                self.assertEqual(resultado["meta"]["arquivos"], len(conteudos))
        recusados = {
            "ano seguinte": ("avaliacao_ano", (_relatorio(2027, 1, linha),)),
            "um dia em comum": ("avaliacao_sobreposicao", (
                _relatorio(2025, 1, linha), _relatorio(2025, 2, linha, periodo="31/01/2025 a 28/02/2025"))),
            "período dentro de outro": ("avaliacao_sobreposicao", (
                _relatorio(2025, 1, linha, periodo="01/01/2025 a 31/03/2025"), _relatorio(2025, 2, linha))),
        }
        for caso, (codigo, conteudos) in recusados.items():
            with self.subTest(caso=caso), self.assertRaises(ETLInputError) as erro:
                avaliar_como_o_worker(self.caminhos(*conteudos), hoje=hoje)
            self.assertEqual(erro.exception.code, codigo)

    def test_marca_de_um_ano_sem_venda_vai_para_o_produto_nos_outros_anos(self):
        # Em dezembro de 2025 o produto P só aparece como bonificação, com dois códigos e estoque
        # negativo; em 2026 ele vende. O Raio-X do envio conta o produto, e a linha de 2026 leva as marcas.
        dezembro = _relatorio(2025, 12, ["1;Produto P;2;5,00;0,00;8,00;-3", "9;Produto P;1;5,00;0,00;4,00;-3",
                                         "2;Outro;1;5,00;5,00;3,00;1"])
        janeiro = _relatorio(2026, 1, ["1;Produto P;3;5,00;15,00;12,00;2", "2;Outro;1;5,00;5,00;3,00;1"])
        resultado = avaliar_como_o_worker(self.caminhos(dezembro, janeiro))
        linhas = _linhas(resultado)
        produto_p = [l for l in linhas if l["chave_produto"] == avaliacao_motor.chave_produto("PRODUTO P")]
        self.assertEqual([l["ano_exercicio"] for l in produto_p], [2026])
        self.assertEqual((produto_p[0]["conflito_cadastral"], produto_p[0]["estoque_negativo"]), (True, True))
        outro = [l for l in linhas if l["chave_produto"] == avaliacao_motor.chave_produto("OUTRO")]
        self.assertEqual(len(outro), 2)
        self.assertFalse(any(l["conflito_cadastral"] or l["estoque_negativo"] for l in outro))
        self.assertGreater(resultado["raio_x"]["saude"]["inconsistencias"]["estoque_negativo"], 0)


# =============================================================================
# 2. Equivalência: o resumo refeito da staging gravada bate com o Raio-X
# =============================================================================
    def _cadastro(self, *casos):
        from tests.test_cadastro_regression import montar_conjuntos
        conjuntos = montar_conjuntos(self.pasta)
        caminhos = [str(conjuntos[caso]) for caso in casos]
        return avaliar_como_o_worker(caminhos, hoje=date(2026, 11, 8)), caminhos

    def test_cadastro_estoque_e_ajustes_por_produto(self):
        resultado, caminhos = self._cadastro("marco", "abril")
        categorias = resultado["linhas"]["categorias"]
        produtos = {linha["descricao_produto"]: linha for linha in _linhas(resultado)}

        def categoria(nome):
            indice = produtos[nome]["categoria"]
            return None if indice is None else categorias[indice]
        # A categoria do relatório mais recente com venda do produto (Departamento antes de Seção e de
        # Grupo); o marcador genérico ("SEM GRUPO") e a célula vazia contam como ausentes.
        self.assertEqual({nome: categoria(nome) for nome in produtos}, {
            "Leite Integral 1L": "ALIMENTOS", "Café Torrado 500g": "MERCEARIA", "Água Mineral 500ml": "BEBIDAS",
            "Sabão em Pó 1kg": None, "Biscoito Recheado": None, "Detergente 500ml": "LIMPEZA"})
        self.assertEqual(sorted(categorias), ["ALIMENTOS", "BEBIDAS", "LIMPEZA", "MERCEARIA"])
        self.assertEqual({n for n, l in produtos.items() if l["categoria_ausente"]},
                         {"Sabão em Pó 1kg", "Biscoito Recheado"})
        # NCM: "0901.21.00" vale pelos dígitos; "00000000" não; a Água, sem NCM em março, tem em abril.
        self.assertEqual({n for n, l in produtos.items() if l["ncm_ausente"]}, {"Sabão em Pó 1kg"})
        # Estoque: o último informado; abril não tem a coluna (o Detergente só vendeu em abril).
        self.assertEqual({n: l["estoque_final"] for n, l in produtos.items()}, {
            "Leite Integral 1L": 12, "Café Torrado 500g": 4, "Água Mineral 500ml": 30, "Sabão em Pó 1kg": -2,
            "Biscoito Recheado": 8, "Detergente 500ml": None})
        # Bonificação e devolução por produto, com as mesmas regras do Raio-X; o Chocolate só teve
        # bonificação (nenhuma venda): o custo dela vai para a meta, sem nome de produto.
        leite, cafe, sabao = produtos["Leite Integral 1L"], produtos["Café Torrado 500g"], produtos["Sabão em Pó 1kg"]
        self.assertEqual(leite["custo_bonificacoes"], 8)
        self.assertEqual((cafe["custo_devolucoes"], cafe["devolucoes_com_custo"]), (14, 18.9))
        self.assertEqual((sabao["custo_devolucoes"], sabao["devolucoes_com_custo"]), (13.3, 11.9))
        meta = resultado["meta"]
        self.assertEqual(meta["ajustes_sem_venda_por_ano"],
                         {"2026": {"custo_bonificacoes": 9, "custo_devolucoes": 0, "devolucoes_com_custo": 0}})
        auditoria = resultado["raio_x"]["auditoria"]
        for coluna in ("custo_bonificacoes", "custo_devolucoes"):
            soma = sum(l[coluna] for l in produtos.values()) + meta["ajustes_sem_venda_por_ano"]["2026"][coluna]
            self.assertAlmostEqual(soma, auditoria[coluna], places=2)
        # Margem negativa no único relatório com custo do Sabão (R$ 71,40 de venda, R$ 80,00 de custo).
        self.assertEqual((sabao["meses_com_custo"], sabao["meses_margem_negativa"], sabao["ultimo_mes_negativo"]),
                         (1, 1, True))
        # Biscoito: o relatório tem a coluna de custo e a célula está em branco.
        biscoito = produtos["Biscoito Recheado"]
        self.assertEqual((biscoito["faturamento_em_relatorio_com_custo"], biscoito["faturamento_com_custo"],
                          biscoito["meses_com_custo"]), (50, 0, 0))
        # Um só ano: não há janela dos mesmos meses (ela compara os dois últimos anos do envio). O giro de
        # quem só vendeu em abril conta desde abril.
        agua = produtos["Água Mineral 500ml"]
        self.assertEqual(agua["quantidade_mesmos_meses"], 0)
        self.assertEqual({n: l["relatorios_desde_primeira_venda"] for n, l in produtos.items()}, {
            "Leite Integral 1L": 2, "Café Torrado 500g": 2, "Água Mineral 500ml": 2, "Sabão em Pó 1kg": 2,
            "Biscoito Recheado": 2, "Detergente 500ml": 1})
        self.assertEqual(
            {chave: meta[chave] for chave in ("relatorios_com_custo_por_ano", "relatorios_com_ncm_por_ano",
                                              "relatorios_com_categoria_por_ano", "relatorios_com_estoque_por_ano",
                                              "relatorios_completos_por_ano", "dias_mesmos_meses_por_ano",
                                              "relatorios_cabecalho_ambiguo")},
            {"relatorios_com_custo_por_ano": {"2026": 2}, "relatorios_com_ncm_por_ano": {"2026": 2},
             "relatorios_com_categoria_por_ano": {"2026": 2}, "relatorios_com_estoque_por_ano": {"2026": 1},
             "relatorios_completos_por_ano": {"2026": 2}, "dias_mesmos_meses_por_ano": {"2026": 0},
             "relatorios_cabecalho_ambiguo": 0})
        # A leitura do cadastro não muda o Raio-X do lead.
        self.assertEqual(resultado["raio_x"], json.loads(json.dumps(raio_x.diagnosticar(caminhos))))
        self.assertNotIn("Chocolate", json.dumps(meta, ensure_ascii=False))

    def test_cabecalho_ambiguo_e_xlsx(self):
        # "Cód. NCM" antes de "Código": o Raio-X lê a primeira como código do produto; NCM, categoria e
        # conflito desse relatório não são avaliados.
        resultado, _ = self._cadastro("cod_ncm_antes")
        meta = resultado["meta"]
        self.assertEqual((meta["relatorios_cabecalho_ambiguo"], meta["relatorios_com_ncm_por_ano"],
                          resultado["linhas"]["categorias"]), (1, {"2026": 0}, []))
        for linha in _linhas(resultado):
            self.assertEqual((linha["ncm_ausente"], linha["categoria_ausente"], linha["categoria"]), (False, False, None))
        # XLSX: o NCM numérico (4012010.0) vale; Família é a categoria.
        resultado, _ = self._cadastro("xlsx")
        produtos = {linha["descricao_produto"]: linha for linha in _linhas(resultado)}
        self.assertEqual({n for n, l in produtos.items() if l["ncm_ausente"]}, {"Água Mineral 500ml", "Sabão em Pó 1kg"})
        self.assertEqual(sorted(resultado["linhas"]["categorias"]), ["BEBIDAS", "LATICINIOS", "MERCEARIA"])


class EquivalenciaTests(_Arquivos):
    def test_staging_reproduz_o_raio_x(self):
        import avaliacao_cenario
        resultado = avaliar_como_o_worker(self.caminhos(*lote_de_dois_anos()))
        saude = resultado["raio_x"]["saude"]
        linhas = avaliacao_cenario.quantizar(resultado["linhas"])
        produtos = {}
        for linha in linhas:
            produtos.setdefault(linha["chave_produto"], []).append(linha)

        def soma(itens, nome):
            return sum((l[nome] for l in itens), Decimal("0"))

        faturamento = soma(linhas, "valor_venda_bruto")
        com_custo = soma(linhas, "faturamento_com_custo")
        self.assertEqual(faturamento, Decimal(str(saude["faturamento_total"])))
        self.assertEqual(len(produtos), saude["itens_vendidos"])
        self.assertEqual(sum(1 for itens in produtos.values() if soma(itens, "custo_total") <= 0),
                         saude["itens_sem_custo"])
        self.assertAlmostEqual(float((faturamento - com_custo) / faturamento), saude["pct_faturamento_sem_custo"],
                               delta=0.0001)
        self.assertEqual(sum(1 for itens in produtos.values()
                             if 0 < soma(itens, "custo_total") and soma(itens, "faturamento_com_custo")
                             < soma(itens, "custo_total")), saude["inconsistencias"]["itens_margem_negativa"])
        self.assertEqual(sum(1 for itens in produtos.values() if soma(itens, "perda") > 0),
                         resultado["raio_x"]["auditoria"]["itens_venda_abaixo_custo"])
        self.assertEqual(sum(1 for itens in produtos.values() if any(l["estoque_negativo"] for l in itens)),
                         saude["inconsistencias"]["estoque_negativo"])
        auditavel = sum((soma(itens, "faturamento_com_custo") for itens in produtos.values()
                         if not any(l["conflito_cadastral"] for l in itens)), Decimal("0"))
        self.assertAlmostEqual(float(100 * auditavel / faturamento), saude["indice"], delta=0.01)
        # O lote exercita o que o teste promete.
        self.assertGreater(saude["itens_sem_custo"], 0)
        self.assertGreater(saude["inconsistencias"]["itens_margem_negativa"], 0)
        self.assertGreater(saude["inconsistencias"]["estoque_negativo"], 0)
        self.assertGreater(saude["inconsistencias"]["codigo_com_varios_nomes"], 0)
        # Recuperável: cada linha arredonda ao centavo; a diferença fica abaixo de meio centavo por linha.
        diferenca = abs(soma(linhas, "recuperavel") - Decimal(str(resultado["raio_x"]["recuperavel"]["total"])))
        self.assertLessEqual(diferenca, Decimal("0.005") * len(linhas))


# =============================================================================
# 3. Contrato do processo isolado e isolamento do módulo
# =============================================================================
class ContratoTests(_Arquivos):
    def setUp(self):
        super().setUp()
        self.valido = avaliar_como_o_worker(self.caminhos(*LOTE))

    def _invalido(self, mudar):
        copia = json.loads(json.dumps(self.valido))
        mudar(copia)
        with self.assertRaises(ETLInputError) as erro:
            etl_runtime.validar_avaliacao(copia)
        self.assertEqual(erro.exception.code, "worker")

    def test_resultado_fora_do_formato_e_recusado(self):
        indice = {nome: i for i, nome in enumerate(avaliacao_motor.COLUNAS)}
        mudancas = {
            "chave a mais": lambda r: r.update(extra=1),
            "raio-x incompleto": lambda r: r["raio_x"].pop("saude"),
            "colunas fora de ordem": lambda r: r["linhas"]["colunas"].reverse(),
            "linha curta": lambda r: r["linhas"]["valores"][0].pop(),
            "ano em float": lambda r: r["linhas"]["valores"][0].__setitem__(indice["ano_exercicio"], 2024.0),
            "descrição vazia": lambda r: r["linhas"]["valores"][0].__setitem__(indice["descricao_produto"], ""),
            "código longo": lambda r: r["linhas"]["valores"][0].__setitem__(indice["codigo_produto"], "9" * 61),
            "valor infinito": lambda r: r["linhas"]["valores"][0].__setitem__(indice["perda"], float("inf")),
            "valor negativo": lambda r: r["linhas"]["valores"][0].__setitem__(indice["quantidade"], -1.0),
            "marca numérica": lambda r: r["linhas"]["valores"][0].__setitem__(indice["variacao"], 1),
            "chave longa": lambda r: r["linhas"]["valores"][0].__setitem__(indice["chave_produto"], "a" * 200),
            "chave fora do formato": lambda r: r["linhas"]["valores"][0].__setitem__(indice["chave_produto"], "ZZ" * 8),
            "descrição só com espaços": lambda r: r["linhas"]["valores"][0].__setitem__(indice["descricao_produto"], "   "),
            "ano fora do envio": lambda r: r["linhas"]["valores"][0].__setitem__(indice["ano_exercicio"], 2023),
            "linha repetida": lambda r: r["linhas"]["valores"].append(list(r["linhas"]["valores"][0])),
            "versão de outro motor": lambda r: r.__setitem__("versao", "lead-antiga"),
            "meta ausente": lambda r: r.__setitem__("meta", []),
            "meta vazia": lambda r: r.__setitem__("meta", {}),
            "meta com texto livre": lambda r: r["meta"].__setitem__("observacao", "Café 500g do João"),
            "arquivos por ano em texto": lambda r: r["meta"]["arquivos_por_ano"].__setitem__("2024", "x"),
            "arquivos por ano sem fechar": lambda r: r["meta"]["arquivos_por_ano"].__setitem__("2024", 9),
            "qualidade com chave desconhecida": lambda r: r["meta"]["qualidade"].__setitem__("Café", 1),
            "cobertura sem um dos anos": lambda r: r["meta"]["dias_por_ano"].pop("2024"),
            "período ausente": lambda r: r["raio_x"].__setitem__("periodo", None),
            "data fora do formato": lambda r: r["raio_x"]["periodo"].__setitem__("inicio", "01/11/2024"),
            "índice em texto": lambda r: r["raio_x"]["saude"].__setitem__("indice", "abc"),
            "índice acima de 100": lambda r: r["raio_x"]["saude"].__setitem__("indice", 101),
        }
        for caso, mudar in mudancas.items():
            with self.subTest(caso=caso):
                self._invalido(mudar)

    def test_colunas_da_etapa_2_fora_do_formato_sao_recusadas(self):
        from tests.test_cadastro_regression import montar_conjuntos
        conjuntos = montar_conjuntos(self.pasta)
        self.valido = avaliar_como_o_worker([str(conjuntos["marco"]), str(conjuntos["abril"])],
                                            hoje=date(2026, 11, 8))
        indice = {nome: i for i, nome in enumerate(avaliacao_motor.COLUNAS)}
        nomes = [valores[indice["descricao_produto"]] for valores in self.valido["linhas"]["valores"]]
        leite, sabao = nomes.index("Leite Integral 1L"), nomes.index("Sabão em Pó 1kg")

        def campo(linha, nome, valor):
            return lambda r: r["linhas"]["valores"][linha].__setitem__(indice[nome], valor)

        def acima(linha, nome, base):
            return lambda r: campo(linha, nome, r["linhas"]["valores"][linha][indice[base]] + 1)(r)
        ajustes_zerados = {"custo_bonificacoes": 0, "custo_devolucoes": 0, "devolucoes_com_custo": 0}
        mudancas = {
            "sem a lista de categorias": lambda r: r["linhas"].pop("categorias"),
            "chave a mais nas linhas": lambda r: r["linhas"].__setitem__("extra", []),
            "categoria repetida": lambda r: r["linhas"]["categorias"].append(r["linhas"]["categorias"][0]),
            "categoria longa": lambda r: r["linhas"]["categorias"].__setitem__(0, "X" * 81),
            "categoria só com espaços": lambda r: r["linhas"]["categorias"].__setitem__(0, "   "),
            "categoria que não é texto": lambda r: r["linhas"]["categorias"].__setitem__(0, 7),
            "índice de categoria fora da lista":
                lambda r: campo(leite, "categoria", len(r["linhas"]["categorias"]))(r),
            "índice de categoria negativo": campo(leite, "categoria", -1),
            "índice de categoria booleano": campo(leite, "categoria", True),
            "categoria e categoria ausente juntas": campo(leite, "categoria_ausente", True),
            "marca da Etapa 2 numérica": campo(leite, "ncm_ausente", 0),
            "mais relatórios negativos que com custo": campo(sabao, "meses_margem_negativa", 2),
            "último negativo sem relatório negativo": campo(leite, "ultimo_mes_negativo", True),
            "relatórios completos acima dos relatórios": acima(leite, "meses_completos", "meses"),
            "relatórios com custo acima dos relatórios": acima(leite, "meses_com_custo", "meses"),
            "venda em relatório com custo acima da venda":
                acima(leite, "faturamento_em_relatorio_com_custo", "valor_venda_bruto"),
            "venda com custo acima da venda em relatório com custo":
                acima(leite, "faturamento_com_custo", "faturamento_em_relatorio_com_custo"),
            "quantidade nos mesmos meses acima da quantidade": acima(leite, "quantidade_mesmos_meses", "quantidade"),
            "contagem em float": campo(leite, "meses_com_custo", 2.0),
            "estoque em texto": campo(leite, "estoque_final", "12"),
            "estoque infinito": campo(leite, "estoque_final", float("inf")),
            "estoque acima do teto": campo(leite, "estoque_final", 10.0 ** 12),
            "bonificação negativa": campo(leite, "custo_bonificacoes", -1),
            "devolução infinita": campo(sabao, "custo_devolucoes", float("inf")),
            "NCM ausente sem a coluna de NCM no ano":
                lambda r: r["meta"]["relatorios_com_ncm_por_ano"].__setitem__("2026", 0),
            "estoque sem a coluna de estoque no ano":
                lambda r: r["meta"]["relatorios_com_estoque_por_ano"].__setitem__("2026", 0),
            "categoria sem a coluna de categoria no ano":
                lambda r: r["meta"]["relatorios_com_categoria_por_ano"].__setitem__("2026", 0),
            "venda com custo sem relatório com custo no ano":
                lambda r: r["meta"]["relatorios_com_custo_por_ano"].__setitem__("2026", 0),
            "contagem do ano acima dos relatórios":
                lambda r: r["meta"]["relatorios_completos_por_ano"].__setitem__("2026", 3),
            "dias nos mesmos meses acima dos dias":
                lambda r: r["meta"]["dias_mesmos_meses_por_ano"].__setitem__("2026", 9999),
            "meta sem a contagem de NCM": lambda r: r["meta"].pop("relatorios_com_ncm_por_ano"),
            "cabeçalho ambíguo acima dos relatórios":
                lambda r: r["meta"].__setitem__("relatorios_cabecalho_ambiguo", 3),
            "cabeçalho ambíguo booleano": lambda r: r["meta"].__setitem__("relatorios_cabecalho_ambiguo", True),
            "ajuste sem venda com chave a mais":
                lambda r: r["meta"]["ajustes_sem_venda_por_ano"]["2026"].__setitem__("extra", 1),
            "ajuste sem venda negativo":
                lambda r: r["meta"]["ajustes_sem_venda_por_ano"]["2026"].__setitem__("custo_bonificacoes", -1),
            "ajuste sem venda de ano fora do envio":
                lambda r: r["meta"]["ajustes_sem_venda_por_ano"].__setitem__("2025", dict(ajustes_zerados)),
        }
        for caso, mudar in mudancas.items():
            with self.subTest(caso=caso):
                self._invalido(mudar)

    def test_motor_nao_carrega_banco_nem_configuracao(self):
        codigo = ("import sys; sys.path.insert(0, sys.argv[1]); import avaliacao_motor; "
                  "print('models' in sys.modules or 'config' in sys.modules or 'flask' in sys.modules)")
        saida = subprocess.run([sys.executable, "-I", "-B", "-c", codigo, str(ROOT)], capture_output=True,
                               text=True, timeout=120, check=True)
        self.assertEqual(saida.stdout.strip(), "False")

    def test_prazo_e_memoria_do_processo_isolado(self):
        casos = (
            (ETLLimits(), 120, 384),
            (ETLLimits(timeout_seconds=30, memory_mb=512), 60, 384),
            (ETLLimits(timeout_seconds=120, memory_mb=256), 120, 256),
        )
        for limites, prazo, memoria in casos:
            with self.subTest(prazo=limites.timeout_seconds, memoria=limites.memory_mb):
                with patch("etl_runtime._run_job", return_value=self.valido) as job:
                    self.assertEqual(etl_runtime.run_avaliacao_cenario(["a.csv"], limites), self.valido)
                operacao, caminhos = job.call_args.args
                politica = job.call_args.kwargs["limits"]
                self.assertEqual((operacao, caminhos), ("avaliacao_cenario", ["a.csv"]))
                self.assertEqual((politica.timeout_seconds, politica.memory_mb), (prazo, memoria))
                self.assertEqual(politica.max_output_bytes, 4 * 1024 * 1024)

    def test_retorno_do_processo_isolado_e_conferido(self):
        # run_avaliacao_cenario confere o que o processo filho devolveu antes de entregar ao pai.
        malformado = json.loads(json.dumps(self.valido))
        malformado["meta"]["observacao"] = "Café 500g do João"
        with patch("etl_runtime._run_job", return_value=malformado), self.assertRaises(ETLInputError) as erro:
            etl_runtime.run_avaliacao_cenario(["a.csv"], ETLLimits())
        self.assertEqual(erro.exception.code, "worker")

    def test_worker_tem_a_operacao_com_a_mesma_validacao(self):
        fonte = (ROOT / "etl_worker.py").read_text(encoding="utf-8")
        trecho = fonte[fonte.index('request["operation"] == "avaliacao_cenario"'):]
        self.assertIn("MAX_ARQUIVOS", trecho)
        self.assertIn("validate_input(path", trecho)
        self.assertIn("avaliar_cenario(request[\"paths\"])", trecho)

    @unittest.skipUnless(importlib.util.find_spec("resource"), "o worker real exige Linux (módulo resource)")
    def test_worker_real_devolve_o_contrato(self):
        resultado = etl_runtime.run_avaliacao_cenario(self.caminhos(*LOTE), ETLLimits())
        self.assertEqual(len(resultado["linhas"]["valores"]), 6)
        self.assertEqual(resultado["raio_x"]["saude"]["faturamento_total"], 995.8)


# =============================================================================
# 4. Quantização (a triagem pura está em tests/test_triagem_cenario.py)
# =============================================================================
class QuantizacaoTests(unittest.TestCase):
    def test_dinheiro_com_2_casas_e_quantidade_com_3_arredondando_o_meio_para_cima(self):
        import avaliacao_cenario
        valores = dict.fromkeys(avaliacao_motor.COLUNAS, 0)
        valores.update(ano_exercicio=2025, chave_produto="0" * 16, codigo_produto=None, descricao_produto="X",
                       quantidade=1.0005, quantidade_com_custo=2.0004, valor_venda_bruto=2.675, recuperavel=0.125,
                       custo_total=1.005, variacao=False, conflito_cadastral=False, estoque_negativo=False,
                       ultimo_mes_negativo=False, ncm_ausente=False, categoria_ausente=False,
                       estoque_final=2.0005, categoria=None)
        linha = avaliacao_cenario.quantizar({"colunas": list(avaliacao_motor.COLUNAS),
                                             "valores": [[valores[c] for c in avaliacao_motor.COLUNAS]]})[0]
        # O valor que o JSON traz ("2.675"), e não o binário do float (2,67499...).
        self.assertEqual((linha["quantidade"], linha["quantidade_com_custo"]), (Decimal("1.001"), Decimal("2.000")))
        self.assertEqual((linha["valor_venda_bruto"], linha["recuperavel"], linha["custo_total"]),
                         (Decimal("2.68"), Decimal("0.13"), Decimal("1.01")))
        # Estoque com 3 casas; sem categoria no envio, a linha grava categoria_produto nula.
        self.assertEqual((linha["estoque_final"], linha["categoria_produto"]), (Decimal("2.001"), None))
        self.assertNotIn("categoria", linha)
        com_categoria = avaliacao_cenario.quantizar({
            "colunas": list(avaliacao_motor.COLUNAS), "categorias": ["BEBIDAS", "LIMPEZA"],
            "valores": [[{**valores, "categoria": 1}[c] for c in avaliacao_motor.COLUNAS]]})[0]
        self.assertEqual(com_categoria["categoria_produto"], "LIMPEZA")


# =============================================================================
# 5. Gravação
# =============================================================================
class _BancoCase(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        pasta = tempfile.TemporaryDirectory(prefix="nexo-avaliacao-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)

    def caminhos(self, *conteudos):
        saida = []
        for conteudo in conteudos:
            caminho = self.pasta / f"arquivo_{uuid.uuid4().hex[:8]}.csv"
            caminho.write_bytes(conteudo)
            saida.append(str(caminho))
        return saida

    def contar(self, modelo):
        return self.db.session.scalar(select(func.count()).select_from(modelo))


CONTATO = {"nome_contato": "Contato fictício", "email": "lead@example.invalid", "whatsapp": "11900000000",
           "nome_loja": "Mercado fictício"}


class GravacaoTests(_BancoCase):
    def _registrar(self, *conteudos, **campos):
        import avaliacao_cenario
        resultado = avaliar_como_o_worker(self.caminhos(*conteudos))
        lead = avaliacao_cenario.registrar(resultado, registrado_por=self.admin_id, meses_prata=24,
                                           **({"contato": CONTATO} if "lead_id" not in campos else {}), **campos)
        self.db.session.commit()
        return lead, resultado

    def test_lead_linhas_marcas_e_resumo_numa_gravacao(self):
        from models import BrainstormingDadosPdv
        lead, resultado = self._registrar(*LOTE)
        linhas = self.db.session.scalars(select(BrainstormingDadosPdv).where(BrainstormingDadosPdv.lead_id == lead.id)).all()
        self.assertEqual(len(linhas), 6)
        self.assertEqual(len({l.criado_em for l in linhas}), 1)
        por_chave = {(l.ano_exercicio, l.descricao_produto): l for l in linhas}
        acucar = por_chave[(2024, "Acucar 1kg")]
        self.assertEqual((acucar.flag_venda_sem_custo, acucar.custo_medio_registrado), (True, None))
        cafe = por_chave[(2024, "Cafe 500g")]
        self.assertEqual((cafe.custo_medio_registrado, cafe.valor_venda_bruto, cafe.custo_total),
                         (Decimal("15.0000"), Decimal("400.00"), Decimal("300.00")))
        leite = por_chave[(2024, "Leite 1L")]
        # Sem mês em comum entre 2024 e 2025, a queda do Leite não é avaliada: fica na Atenção pela margem.
        self.assertEqual((leite.perda, leite.vazamento_estimado_reais, leite.bloco_triagem),
                         (Decimal("10.00"), Decimal("10.00"), "atencao"))
        self.assertEqual(json.loads(lead.meta_json)["dias_mesmos_meses_por_ano"], {"2024": 0, "2025": 0})
        self.assertEqual({l.versao_regras for l in linhas}, {avaliacao_motor.VERSAO_REGRAS_LEAD})
        self.db.session.refresh(lead)
        recomendado = raio_x.recomendar(resultado["raio_x"], 24)["plano"]
        self.assertEqual((lead.status, lead.arquivos, lead.vazamento_total_reais, lead.plano_recomendado_codigo),
                         ("concluido", 4, Decimal("20.00"), recomendado))
        self.assertEqual(lead.score_saude_cadastral,
                         Decimal(str(resultado["raio_x"]["saude"]["indice"])).quantize(Decimal("0.01")))
        self.assertEqual((lead.periodo_inicio, lead.periodo_fim), (date(2024, 11, 1), date(2025, 2, 28)))
        # Leite 1L vendeu abaixo do custo em 2024 (perda de R$ 10): a marca é o fato, sem materialidade.
        self.assertEqual((lead.itens_venda_sem_custo, lead.itens_venda_abaixo_custo, lead.itens_omissao_erp), (1, 1, 0))
        self.assertEqual(json.loads(lead.limiares_triagem_json), triagem_cenario.LIMIARES_PADRAO.para_dict())
        self.assertEqual((lead.registrado_por, lead.versao_triagem), (self.admin_id, triagem_cenario.VERSAO_TRIAGEM))
        self.assertIsNotNone(lead.consentimento_em)
        resumos = {b: json.loads(getattr(lead, f"resumo_{b}_json")) for b in triagem_cenario.BLOCOS}
        self.assertEqual({b: r["itens"] for b, r in resumos.items()}, {"urgencia": 0, "atencao": 2, "eletiva": 1})
        guardado = lead.meta_json + "".join(getattr(lead, f"resumo_{b}_json") for b in triagem_cenario.BLOCOS)
        for nome in NOMES:
            self.assertNotIn(nome, guardado)

    def test_reenvio_troca_as_linhas_do_mesmo_lead(self):
        from models import BrainstormingDadosPdv, LeadAvaliacao
        lead, _ = self._registrar(*LOTE)
        identificador = lead.id
        self._registrar(JAN25, FEV25, lead_id=identificador)
        self.assertEqual(self.contar(LeadAvaliacao), 1)
        self.assertEqual(self.contar(BrainstormingDadosPdv), 3)
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(LeadAvaliacao, identificador).arquivos, 2)

    def test_reenvio_de_lead_convertido_e_recusado(self):
        import avaliacao_cenario
        lead, _ = self._registrar(*LOTE)
        lead.status = "convertido"
        self.db.session.commit()
        with self.assertRaises(avaliacao_cenario.LeadIndisponivel):
            self._registrar(JAN25, lead_id=lead.id)

    def test_retriar_refaz_marcas_e_blocos_a_partir_da_staging(self):
        import avaliacao_cenario
        from models import BrainstormingDadosPdv
        lead, _ = self._registrar(*LOTE_MESMOS_MESES)
        antes = {l.id: (l.bloco_triagem, l.flag_venda_sem_custo, l.vazamento_estimado_reais) for l in
                 self.db.session.scalars(select(BrainstormingDadosPdv))}
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET bloco_triagem = NULL, "
                                     "flag_venda_sem_custo = 0, \"vazamento_estimado_r$\" = 0"))
        self.db.session.execute(text("UPDATE lead_avaliacao SET resumo_urgencia_json = NULL, versao_triagem = NULL"))
        self.db.session.commit()
        avaliacao_cenario.retriar(lead.id)
        self.db.session.commit()
        self.db.session.expire_all()
        depois = {l.id: (l.bloco_triagem, l.flag_venda_sem_custo, l.vazamento_estimado_reais) for l in
                  self.db.session.scalars(select(BrainstormingDadosPdv))}
        self.assertEqual(depois, antes)
        self.db.session.refresh(lead)
        self.assertEqual((json.loads(lead.resumo_urgencia_json)["itens"], lead.versao_triagem),
                         (1, triagem_cenario.VERSAO_TRIAGEM))

    def test_retriar_recusa_sem_dados_com_outra_versao_ou_expirado(self):
        import avaliacao_cenario
        from models import LeadAvaliacao
        vazio = LeadAvaliacao(**CONTATO)
        self.db.session.add(vazio)
        self.db.session.commit()
        lead, _ = self._registrar(*LOTE)
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET versao_regras = 'lead-antiga'"))
        self.db.session.commit()
        expirado, _ = self._registrar(JAN25)
        expirado.status = "expirado"
        self.db.session.commit()
        for identificador in (vazio.id, lead.id, expirado.id, uuid.uuid4()):
            with self.subTest(lead=str(identificador)), self.assertRaises(avaliacao_cenario.LeadIndisponivel):
                avaliacao_cenario.retriar(identificador)
            self.db.session.rollback()

    def test_resultado_malformado_vira_recusa_e_nao_erro_500(self):
        # Defesa em profundidade: mesmo sem a conferência do contrato, o que não fecha na gravação é
        # "resultado inválido", e nada é gravado.
        import avaliacao_cenario
        from models import LeadAvaliacao
        valido = avaliar_como_o_worker(self.caminhos(*LOTE))
        defeitos = {
            "meta vazia": lambda r: r.__setitem__("meta", {}),
            "arquivos por ano em texto": lambda r: r["meta"]["arquivos_por_ano"].__setitem__("2024", "x"),
            "período ausente": lambda r: r["raio_x"].__setitem__("periodo", None),
            "índice em texto": lambda r: r["raio_x"]["saude"].__setitem__("indice", "abc"),
        }
        for caso, defeito in defeitos.items():
            resultado = json.loads(json.dumps(valido))
            defeito(resultado)
            with self.subTest(caso=caso), self.assertRaises(ETLInputError) as erro:
                avaliacao_cenario.registrar(resultado, contato=CONTATO, registrado_por=self.admin_id, meses_prata=24)
            self.assertEqual(erro.exception.code, "worker")
            self.db.session.rollback()
        self.assertEqual(self.contar(LeadAvaliacao), 0)

    def test_nome_so_com_espacos_grava_com_a_chave_como_descricao(self):
        from models import BrainstormingDadosPdv
        espacos = _relatorio(2026, 3, [f"1;{' ' * 205}Cafe;2;5,00;10,00;6,00;1"])
        self._registrar(espacos)
        self.assertEqual(self.db.session.scalar(select(BrainstormingDadosPdv.descricao_produto)), "CAFE")

    def test_valor_acima_da_coluna_nao_grava_nada(self):
        import avaliacao_cenario
        from models import LeadAvaliacao
        resultado = avaliar_como_o_worker(self.caminhos(*LOTE))
        posicao = avaliacao_motor.COLUNAS.index("recuperavel")
        resultado["linhas"]["valores"][0][posicao] = 1e10
        with self.assertRaises(ETLInputError) as erro:
            avaliacao_cenario.registrar(resultado, contato=CONTATO, registrado_por=self.admin_id, meses_prata=24)
        self.assertEqual(erro.exception.code, "avaliacao_valor")
        self.db.session.rollback()
        self.assertEqual(self.contar(LeadAvaliacao), 0)

    def test_soma_arredondada_ate_o_teto_nao_grava_nada(self):
        # R$ 999.999.999.999,996 passa pelo contrato (abaixo de 10^12) e vira 1.000.000.000.000,00 com
        # 2 casas, o que a coluna NUMERIC(14,2) não guarda: recusa com mensagem, sem erro 500.
        import avaliacao_cenario
        from models import LeadAvaliacao
        resultado = avaliar_como_o_worker(self.caminhos(*LOTE))
        resultado["linhas"]["valores"][0][avaliacao_motor.COLUNAS.index("valor_venda_bruto")] = 999999999999.996
        with self.assertRaises(ETLInputError) as erro:
            avaliacao_cenario.registrar(resultado, contato=CONTATO, registrado_por=self.admin_id, meses_prata=24)
        self.assertEqual(erro.exception.code, "avaliacao_valor")
        self.db.session.rollback()
        self.assertEqual(self.contar(LeadAvaliacao), 0)

    def test_custo_medio_acima_da_coluna_nao_grava_nada(self):
        # O custo médio (NUMERIC(14,4)) é derivado no processo pai: 1e8 de custo em 0,001 unidade dá 1e11.
        import avaliacao_cenario
        from models import LeadAvaliacao
        resultado = avaliar_como_o_worker(self.caminhos(*LOTE))
        indice = {nome: i for i, nome in enumerate(avaliacao_motor.COLUNAS)}
        linha = resultado["linhas"]["valores"][0]
        linha[indice["quantidade"]] = linha[indice["quantidade_com_custo"]] = 0.001
        linha[indice["valor_venda_bruto"]] = linha[indice["faturamento_com_custo"]] = 100000000.0
        linha[indice["custo_total"]] = 100000000.0
        with self.assertRaises(ETLInputError) as erro:
            avaliacao_cenario.registrar(resultado, contato=CONTATO, registrado_por=self.admin_id, meses_prata=24)
        self.assertEqual(erro.exception.code, "avaliacao_valor")
        self.db.session.rollback()
        self.assertEqual(self.contar(LeadAvaliacao), 0)

    def test_expurgo_leva_o_envio_inteiro_e_o_resumo_fica(self):
        import leads_avaliacao
        from horario import agora_utc
        from models import BrainstormingDadosPdv
        lead, _ = self._registrar(*LOTE)
        score = lead.score_saude_cadastral
        resultado = leads_avaliacao.expurgar_dados_pdv(self.db.session, agora=agora_utc() + timedelta(days=31))
        self.db.session.commit()
        self.assertEqual(resultado, leads_avaliacao.ResultadoExpurgo(leads_expirados=1, linhas_removidas=6))
        self.assertEqual(self.contar(BrainstormingDadosPdv), 0)
        self.db.session.refresh(lead)
        self.assertEqual((lead.status, lead.score_saude_cadastral), ("expirado", score))

    def test_comando_retriar_mostra_so_contagens(self):
        lead, _ = self._registrar(*LOTE_MESMOS_MESES)
        identificador = str(lead.id)
        self.db.session.remove()
        saida = self.app.test_cli_runner().invoke(args=["leads", "retriar", identificador])
        self.assertEqual(saida.exit_code, 0, saida.output)
        self.assertIn("urgência 1, atenção 1, eletiva 1", saida.output)
        for dado in ("lead@example.invalid", "Contato fictício", "Mercado fictício", "Leite"):
            self.assertNotIn(dado, saida.output)
        invalido = self.app.test_cli_runner().invoke(args=["leads", "retriar", "nao-e-uuid"])
        self.assertNotEqual(invalido.exit_code, 0)


    def test_retriagem_e_reenvio_usam_os_limiares_gravados(self):
        import avaliacao_cenario
        from models import LeadAvaliacao
        lead, _ = self._registrar(*LOTE)
        identificador = lead.id
        self.assertEqual(json.loads(lead.limiares_triagem_json), triagem_cenario.LIMIARES_PADRAO.para_dict())
        avaliacao_cenario.retriar(identificador, triagem_cenario.Limiares(margem_baixa=Decimal("0.05")))
        self.db.session.commit()

        def gravados():
            self.db.session.expire_all()
            return json.loads(self.db.session.get(LeadAvaliacao, identificador).limiares_triagem_json)
        self.assertEqual(gravados()["margem_baixa"], "0.05")
        # Sem limiares, a retriagem e o reenvio usam os gravados no lead.
        avaliacao_cenario.retriar(identificador)
        self.db.session.commit()
        self.assertEqual(gravados()["margem_baixa"], "0.05")
        self._registrar(JAN25, FEV25, lead_id=identificador)
        self.assertEqual(gravados()["margem_baixa"], "0.05")
        # Gravação ilegível: a retriagem sem limiares recusa, sem cair nos padrão em silêncio.
        self.db.session.get(LeadAvaliacao, identificador).limiares_triagem_json = '{"margem_baixa": "abc"}'
        self.db.session.commit()
        with self.assertRaises(avaliacao_cenario.LeadIndisponivel):
            avaliacao_cenario.retriar(identificador)
        self.db.session.rollback()
        with self.assertRaises(ValueError):
            avaliacao_cenario.retriar(identificador, {"margem_baixa": "0.05"})

    def test_comando_retriar_com_limiares_e_agendar_expurgo_fora_do_postgresql(self):
        from models import LeadAvaliacao
        lead, _ = self._registrar(*LOTE)
        identificador = str(lead.id)
        self.db.session.remove()
        cli = self.app.test_cli_runner()
        saida = cli.invoke(args=["leads", "retriar", identificador, "--limiares",
                                 '{"margem_baixa": "0.05", "relatorios_para_giro_critico": 6}'])
        self.assertEqual(saida.exit_code, 0, saida.output)
        self.assertIn("com omissão no ERP 0", saida.output)
        gravados = json.loads(self.db.session.get(LeadAvaliacao, lead.id).limiares_triagem_json)
        self.assertEqual((gravados["margem_baixa"], gravados["relatorios_para_giro_critico"]), ("0.05", 6))
        self.db.session.remove()
        for invalido in ('{"margem_baixa": 0.05}', '{"inexistente": "1"}', '{"queda_moderada": "0.9"}', "[]", "{",
                         '{"margem_baixa": NaN}'):
            with self.subTest(limiares=invalido):
                recusa = cli.invoke(args=["leads", "retriar", identificador, "--limiares", invalido])
                self.assertNotEqual(recusa.exit_code, 0)
                self.assertIn("Limiares inválidos", recusa.output)
        sem_lead = cli.invoke(args=["leads", "retriar", str(uuid.uuid4()), "--limiares", "{}"])
        self.assertNotEqual(sem_lead.exit_code, 0)
        self.assertIn("Lead inexistente", sem_lead.output)
        # O agendamento é do pg_cron: fora do PostgreSQL, recusa sem tocar em nada.
        agendar = cli.invoke(args=["leads", "agendar-expurgo"])
        self.assertNotEqual(agendar.exit_code, 0)
        self.assertIn("não é PostgreSQL", agendar.output)


# =============================================================================
# 6. Rotas do consultor
# =============================================================================
class RotasTests(_BancoCase):
    PAGINA = "/admin/leads"

    def setUp(self):
        super().setUp()
        self.worker_chamado = 0

        def worker(caminhos, limits=None):
            # A transação da requisição termina antes do processo isolado (até 2 minutos).
            self.transacao_aberta = self.db.session().in_transaction()
            self.worker_chamado += 1
            return avaliar_como_o_worker(caminhos, limits)

        self.stack.enter_context(patch("etl_runtime.run_avaliacao_cenario", side_effect=worker))

    def _enviar(self, *arquivos, autorizacao=True, email="lead@example.invalid", destino=None):
        token = self.csrf_token(self.PAGINA)
        dados = {"csrf_token": token, "nome_contato": "Contato fictício", "nome_loja": "Mercado fictício",
                 "email": email, "whatsapp": "(11) 90000-0000",
                 "arquivos": [(io.BytesIO(conteudo), nome) for nome, conteudo in arquivos]}
        if autorizacao:
            dados["autorizacao"] = "1"
        return self.client.post(destino or self.PAGINA, data=dados, content_type="multipart/form-data")

    def _lote(self, lote=LOTE):
        return [(f"mes_{i}.csv", conteudo) for i, conteudo in enumerate(lote, 1)]

    def _lote_com_cadastro(self):
        from tests.test_cadastro_regression import montar_conjuntos
        conjuntos = montar_conjuntos(self.pasta)
        casos = ("marco", "abril")
        return [(f"{caso}.csv", conjuntos[caso].read_bytes()) for caso in casos], [str(conjuntos[c]) for c in casos]

    def test_so_consultor_acessa(self):
        self.assertEqual(self.client.get(self.PAGINA).status_code, 302)
        self.login()
        self.assertEqual(self.client.get(self.PAGINA).status_code, 403)
        self.logout()
        self.login("admin-test@example.invalid")
        pagina = self.client.get(self.PAGINA)
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("Nova avaliação", pagina.get_data(as_text=True))

    def test_envio_grava_e_o_detalhe_mostra_a_triagem(self):
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        resposta = self._enviar(*self._lote(LOTE_MESMOS_MESES))
        self.assertEqual(resposta.status_code, 303, resposta.get_data(as_text=True)[:500])
        lead = self.db.session.scalar(select(LeadAvaliacao))
        self.assertEqual(resposta.headers["Location"], f"{self.PAGINA}/{lead.id}")
        self.assertEqual((lead.whatsapp, lead.registrado_por), ("11900000000", self.admin_id))
        self.assertFalse(self.transacao_aberta)
        html = self.client.get(f"{self.PAGINA}/{lead.id}").get_data(as_text=True)
        for texto in ("Urgência", "Atenção", "Eletiva", "Leite 1L", "Ritmo de venda caiu 50% ou mais",
                      "Mercado fictício"):
            self.assertIn(texto, html)
        self.assertNotIn("Triagem feita com regras anteriores", html)
        self.assertIn("Mercado fictício", self.client.get(self.PAGINA).get_data(as_text=True))

    def test_triagem_de_versao_anterior_nao_mistura_motivos_novos(self):
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        self._enviar(*self._lote(LOTE_MESMOS_MESES))
        lead = self.db.session.scalar(select(LeadAvaliacao))
        detalhe = f"{self.PAGINA}/{lead.id}"
        motivos_do_item = "<td>Potencial recuperável alto; Ritmo de venda caiu 50% ou mais</td>"
        self.assertIn(motivos_do_item, self.client.get(detalhe).get_data(as_text=True))
        lead.versao_triagem = "triagem-anterior"
        self.db.session.commit()
        html = self.client.get(detalhe).get_data(as_text=True)
        # O bloco gravado e o resumo dele ficam; os motivos refeitos com as regras atuais, não.
        self.assertIn("Triagem feita com regras anteriores", html)
        self.assertIn("Leite 1L", html)
        self.assertNotIn(motivos_do_item, html)

    def test_diagnostico_json_do_consultor(self):
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        arquivos, caminhos = self._lote_com_cadastro()
        self.assertEqual(self._enviar(*arquivos).status_code, 303)
        lead = self.db.session.scalar(select(LeadAvaliacao))
        caminho = f"{self.PAGINA}/{lead.id}/diagnostico.json"
        resposta = self.client.get(caminho)
        self.assertEqual((resposta.status_code, resposta.mimetype, resposta.headers["Cache-Control"]),
                         (200, "application/json", "no-store"))
        texto = resposta.get_data(as_text=True)

        def sem_constante(nome):
            raise AssertionError(f"{nome} no JSON")
        payload = json.loads(texto, parse_constant=sem_constante)
        self.assertEqual((payload["situacao"], payload["audiencia"], payload["lead"]["id"]),
                         ("atual", "consultor", str(lead.id)))
        for proibido in ("Contato fictício", "lead@example.invalid", "11900000000", "Mercado fictício",
                         "nome_contato", "whatsapp", "nome_loja", "registrado_por"):
            self.assertNotIn(proibido, texto)
        # A triagem refeita sobre a staging é a gravada: os mesmos itens em cada bloco.
        for bloco in triagem_cenario.BLOCOS:
            self.assertEqual(payload["blocos"][bloco]["total"], payload["resumos"][bloco]["itens"])
        self.assertIn("Biscoito Recheado", [item["produto"] for item in payload["blocos"]["atencao"]["itens"]])
        # Matriz por categoria, "Sem categoria" por último; os totais fecham com a auditoria do Raio-X.
        self.assertTrue(payload["categorias_publicadas"])
        nomes = [linha["categoria"] for linha in payload["categorias"]]
        self.assertEqual((sorted(nomes[:-1]), nomes[-1]),
                         (["ALIMENTOS", "BEBIDAS", "LIMPEZA", "MERCEARIA"], "Sem categoria"))
        totais, raio = payload["totais"], raio_x.diagnosticar(caminhos)
        auditoria = raio["auditoria"]
        self.assertEqual(totais["faturamento"], raio["saude"]["faturamento_total"])
        self.assertEqual(
            (totais["venda_com_custo"], totais["custo_das_vendas"], totais["custo_bonificacoes"],
             totais["custo_devolucoes"], totais["cmv_liquido"], totais["vendas_abaixo_do_custo"]),
            (auditoria["faturamento_com_custo"], auditoria["custo_total"], auditoria["custo_bonificacoes"],
             auditoria["custo_devolucoes"], auditoria["cmv_liquido"], auditoria["perda_abaixo_custo"]))
        self.assertEqual(
            (totais["margem_bruta_estimada"]["valor"], totais["margem_com_bonificacoes_e_devolucoes"]["valor"]),
            (auditoria["margem_valor"], auditoria["margem_liquida_valor"]))
        self.assertAlmostEqual(totais["margem_bruta_estimada"]["percentual"], auditoria["margem_percentual"],
                               delta=0.0001)
        self.assertAlmostEqual(totais["margem_com_bonificacoes_e_devolucoes"]["percentual"],
                               auditoria["margem_liquida_percentual"], delta=0.0001)
        self.assertIsNone(totais["perdas_operacionais"])
        # As categorias somam o total; a bonificação do produto sem venda (R$ 9) entra só no total.
        self.assertAlmostEqual(sum(linha["faturamento"] for linha in payload["categorias"]), totais["faturamento"],
                               places=2)
        self.assertAlmostEqual(sum(linha["custo_bonificacoes"] for linha in payload["categorias"]) + 9,
                               totais["custo_bonificacoes"], places=2)
        # Itens por bloco: entre 1 e 200; um limite que não é número vale o padrão (30).
        for limite, esperado in (("1", 1), ("-5", 1), ("abc", 30), ("100000", 200)):
            with self.subTest(limite=limite):
                for bloco in self.client.get(f"{caminho}?limite={limite}").get_json()["blocos"].values():
                    self.assertEqual(bloco["mostrando"], min(bloco["total"], esperado))
        # A tela mostra a mesma matriz e o atalho para o JSON.
        html = self.client.get(f"{self.PAGINA}/{lead.id}").get_data(as_text=True)
        for trecho in ("Matriz por categoria", "ALIMENTOS", "Sem categoria", "Itens com omissão no ERP",
                       "Diagnóstico em JSON", "Biscoito Recheado"):
            self.assertIn(trecho, html)
        # Lead inexistente: 404 em JSON, sem cache.
        inexistente = self.client.get(f"{self.PAGINA}/{uuid.uuid4()}/diagnostico.json")
        self.assertEqual((inexistente.status_code, inexistente.get_json(), inexistente.headers["Cache-Control"]),
                         (404, {"erro": "lead_inexistente"}, "no-store"))

    def test_diagnostico_de_outra_versao_ou_vencido_fica_so_com_o_resumo(self):
        from horario import agora_utc
        from models import BrainstormingDadosPdv, LeadAvaliacao
        self.login("admin-test@example.invalid")
        arquivos, _ = self._lote_com_cadastro()
        self._enviar(*arquivos)
        lead = self.db.session.scalar(select(LeadAvaliacao))
        detalhe = f"{self.PAGINA}/{lead.id}"
        # Linhas de outra versão das regras: o JSON fica só com o resumo; a tela lista os itens no bloco
        # gravado, sem motivos, impacto nem matriz.
        self.db.session.execute(text("UPDATE brainstorming_dados_pdv SET versao_regras = 'lead-antiga'"))
        self.db.session.commit()
        payload = self.client.get(f"{detalhe}/diagnostico.json").get_json()
        self.assertEqual((payload["situacao"], payload["blocos"], payload["categorias"], payload["totais"]),
                         ("desatualizado", None, None, None))
        self.assertNotIn("Biscoito", json.dumps(payload, ensure_ascii=False))
        html = self.client.get(detalhe).get_data(as_text=True)
        self.assertIn("Dados gravados por outra versão das regras", html)
        self.assertIn("Biscoito Recheado", html)
        for ausente in ("Matriz por categoria", "Diagnóstico em JSON"):
            self.assertNotIn(ausente, html)
        # Passados os 30 dias, antes de o expurgo rodar: só o resumo, no JSON e na tela.
        for registro in self.db.session.scalars(select(BrainstormingDadosPdv)):
            registro.versao_regras = avaliacao_motor.VERSAO_REGRAS_LEAD
            registro.criado_em = agora_utc() - timedelta(days=31)
        self.db.session.commit()
        with patch("blueprints.leads.expurgo_oportunista"):
            payload = self.client.get(f"{detalhe}/diagnostico.json").get_json()
            html = self.client.get(detalhe).get_data(as_text=True)
        self.assertEqual((payload["situacao"], payload["blocos"], payload["categorias"]), ("expirado", None, None))
        self.assertNotIn("Biscoito", json.dumps(payload, ensure_ascii=False))
        for ausente in ("Biscoito Recheado", "Matriz por categoria", "Diagnóstico em JSON"):
            self.assertNotIn(ausente, html)
        self.assertEqual(self.contar(BrainstormingDadosPdv), 6)

    def test_resultado_malformado_do_processo_isolado_responde_422(self):
        from models import LeadAvaliacao

        def malformado(caminhos, limits=None):
            resultado = avaliar_como_o_worker(caminhos, limits)
            resultado["meta"] = {}
            return resultado

        self.login("admin-test@example.invalid")
        with patch("etl_runtime.run_avaliacao_cenario", side_effect=malformado):
            resposta = self._enviar(*self._lote())
        self.assertEqual(resposta.status_code, 422)
        self.assertIn("Resultado inválido do processamento isolado.", resposta.get_data(as_text=True))
        self.assertEqual(self.contar(LeadAvaliacao), 0)

    def test_autorizacao_e_contato_validos_sao_exigidos(self):
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        sem_autorizacao = self._enviar(*self._lote(), autorizacao=False)
        email_invalido = self._enviar(*self._lote(), email="sem-arroba")
        self.assertEqual((sem_autorizacao.status_code, email_invalido.status_code), (422, 422))
        self.assertIn("Confirme que o lead autorizou", sem_autorizacao.get_data(as_text=True))
        self.assertEqual((self.contar(LeadAvaliacao), self.worker_chamado), (0, 0))

    def test_vaga_ocupada_responde_503_com_retry_after(self):
        import etl_uploads
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        token = self.csrf_token(self.PAGINA)
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            resposta = self.client.post(self.PAGINA, data={
                "csrf_token": token, "nome_contato": "Contato", "nome_loja": "Loja", "email": "lead@example.invalid",
                "autorizacao": "1", "arquivos": [(io.BytesIO(NOV24), "nov.csv")]}, content_type="multipart/form-data")
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()
        self.assertEqual((resposta.status_code, resposta.headers.get("Retry-After")), (503, "30"))
        self.assertIn("nenhum arquivo foi lido", resposta.get_data(as_text=True))
        self.assertEqual((self.contar(LeadAvaliacao), self.worker_chamado), (0, 0))

    def test_processo_isolado_ocupado_tambem_responde_503(self):
        # A vaga do processo isolado (etl_runtime._ADMISSION) ocupada: o _run_job real recusa antes
        # de abrir processo, e a tela responde como a fila de espera, sem gravar nada.
        import etl_runtime
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        self._enviar(*self._lote())
        lead = self.db.session.scalar(select(LeadAvaliacao))
        detalhe = f"{self.PAGINA}/{lead.id}"
        token = self.csrf_token(detalhe)
        real = lambda caminhos, limits=None: etl_runtime._run_job("avaliacao_cenario", caminhos, limits=limits)
        self.assertTrue(etl_runtime._ADMISSION.acquire(blocking=False))
        try:
            with patch("etl_runtime.run_avaliacao_cenario", side_effect=real):
                novo = self._enviar(("nov.csv", NOV24), email="outro@example.invalid")
                reenvio = self.client.post(f"{detalhe}/reenviar", data={
                    "csrf_token": token, "autorizacao": "1", "arquivos": [(io.BytesIO(NOV24), "nov.csv")]},
                    content_type="multipart/form-data")
        finally:
            etl_runtime._ADMISSION.release()
        for resposta in (novo, reenvio):
            self.assertEqual((resposta.status_code, resposta.headers.get("Retry-After")), (503, "30"))
            self.assertIn("Aguarde alguns segundos e envie de novo", resposta.get_data(as_text=True))
        self.assertEqual(self.contar(LeadAvaliacao), 1)
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(LeadAvaliacao, lead.id).arquivos, len(LOTE))

    def test_erro_do_motor_volta_com_a_mensagem_e_nada_e_gravado(self):
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        resposta = self._enviar(("sem_periodo.csv", b"Codigo;Produto;Qtde;Valor Total\n1;Cafe;1;10,00\n"))
        self.assertEqual(resposta.status_code, 422)
        self.assertIn("Arquivo 1: o período não está no cabeçalho", resposta.get_data(as_text=True))
        self.assertEqual(self.contar(LeadAvaliacao), 0)

    def test_reenvio_e_retriagem_pela_tela(self):
        from models import BrainstormingDadosPdv, LeadAvaliacao
        self.login("admin-test@example.invalid")
        self._enviar(*self._lote())
        lead = self.db.session.scalar(select(LeadAvaliacao))
        detalhe = f"{self.PAGINA}/{lead.id}"
        token = self.csrf_token(detalhe)
        reenvio = self.client.post(f"{detalhe}/reenviar", data={
            "csrf_token": token, "autorizacao": "1",
            "arquivos": [(io.BytesIO(JAN25), "jan.csv"), (io.BytesIO(FEV25), "fev.csv")]},
            content_type="multipart/form-data")
        self.assertEqual(reenvio.status_code, 303)
        self.assertEqual(self.contar(BrainstormingDadosPdv), 3)
        retriagem = self.client.post(f"{detalhe}/retriar", data={"csrf_token": self.csrf_token(detalhe)})
        self.assertEqual(retriagem.status_code, 303)
        self.assertIn("Triagem refeita", self.client.get(detalhe).get_data(as_text=True))

    def test_post_sem_csrf_e_recusado(self):
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        resposta = self.client.post(self.PAGINA, data={"nome_contato": "x", "autorizacao": "1"})
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(self.contar(LeadAvaliacao), 0)

    def test_lead_inexistente_e_404(self):
        self.login("admin-test@example.invalid")
        self.assertEqual(self.client.get(f"{self.PAGINA}/{uuid.uuid4()}").status_code, 404)

    def test_todas_as_rotas_exigem_consultor(self):
        from models import BrainstormingDadosPdv, LeadAvaliacao
        self.login("admin-test@example.invalid")
        self._enviar(*self._lote())
        lead = self.db.session.scalar(select(LeadAvaliacao))
        self.logout()
        detalhe = f"{self.PAGINA}/{lead.id}"
        rotas = (("get", self.PAGINA), ("post", self.PAGINA), ("get", detalhe), ("post", f"{detalhe}/reenviar"),
                 ("post", f"{detalhe}/retriar"), ("get", f"{detalhe}/diagnostico.json"))
        chamadas = self.worker_chamado

        def pedir(metodo, caminho, token):
            if metodo == "get":
                return self.client.get(caminho)
            return self.client.post(caminho, data={
                "csrf_token": token, "autorizacao": "1", "nome_contato": "Contato", "nome_loja": "Loja",
                "email": "lead@example.invalid", "arquivos": [(io.BytesIO(NOV24), "nov.csv")]},
                content_type="multipart/form-data")

        # Sem login, mesmo com token CSRF válido: vai para o login, sem ler nada.
        token = self.csrf_token()
        for metodo, caminho in rotas:
            with self.subTest(quem="anônimo", metodo=metodo, caminho=caminho):
                resposta = pedir(metodo, caminho, token)
                self.assertEqual(resposta.status_code, 302)
                self.assertIn("/auth/login", resposta.headers["Location"])
        # Cliente logado: 403.
        self.login()
        token = self.csrf_token("/auth/logout")
        for metodo, caminho in rotas:
            with self.subTest(quem="cliente", metodo=metodo, caminho=caminho):
                self.assertEqual(pedir(metodo, caminho, token).status_code, 403)
        self.assertEqual(self.worker_chamado, chamadas)
        self.assertEqual((self.contar(LeadAvaliacao), self.contar(BrainstormingDadosPdv)), (1, 6))

    def test_reenvio_exige_autorizacao_respeita_a_vaga_e_renova_o_consentimento(self):
        import etl_uploads
        from horario import agora_utc
        from models import BrainstormingDadosPdv, LeadAvaliacao
        self.login("admin-test@example.invalid")
        self._enviar(*self._lote())
        lead = self.db.session.scalar(select(LeadAvaliacao))
        lead.consentimento_em = agora_utc() - timedelta(days=10)
        self.db.session.commit()
        self.db.session.expire_all()
        antes = self.db.session.get(LeadAvaliacao, lead.id).consentimento_em
        detalhe = f"{self.PAGINA}/{lead.id}"
        chamadas = self.worker_chamado

        def reenviar(autorizacao=True):
            dados = {"csrf_token": self.csrf_token(detalhe), "arquivos": [(io.BytesIO(JAN25), "jan.csv")]}
            if autorizacao:
                dados["autorizacao"] = "1"
            return self.client.post(f"{detalhe}/reenviar", data=dados, content_type="multipart/form-data")

        sem_autorizacao = reenviar(autorizacao=False)
        self.assertEqual(sem_autorizacao.status_code, 303)
        self.assertIn("Confirme que o lead autorizou", self.client.get(detalhe).get_data(as_text=True))
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            ocupado = reenviar()
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()
        self.assertEqual((ocupado.status_code, ocupado.headers.get("Retry-After")), (503, "30"))
        self.assertIn("nenhum arquivo foi lido", ocupado.get_data(as_text=True))
        self.assertEqual((self.worker_chamado, self.contar(BrainstormingDadosPdv)), (chamadas, 6))
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(LeadAvaliacao, lead.id).consentimento_em, antes)
        # Com a autorização e a vaga livre, o reenvio troca as linhas e renova o consentimento.
        self.assertEqual(reenviar().status_code, 303)
        self.assertEqual((self.worker_chamado, self.contar(BrainstormingDadosPdv)), (chamadas + 1, 2))
        self.db.session.expire_all()
        self.assertGreater(self.db.session.get(LeadAvaliacao, lead.id).consentimento_em - antes, timedelta(days=9))

    def test_expurgo_roda_pela_tela_no_maximo_uma_vez_por_hora(self):
        import blueprints.leads as rotas
        import leads_avaliacao
        from horario import agora_utc
        from models import BrainstormingDadosPdv, LeadAvaliacao
        self.login("admin-test@example.invalid")
        self._enviar(*self._lote())
        lead = self.db.session.scalar(select(LeadAvaliacao))
        real = leads_avaliacao.expurgar_dados_pdv

        def daqui_a_31_dias(sessao):
            return real(sessao, agora=agora_utc() + timedelta(days=31))

        with patch.dict(rotas._expurgo_ultimo, {"instante": None}), \
                patch("leads_avaliacao.expurgar_dados_pdv", side_effect=daqui_a_31_dias) as expurgo:
            # O detalhe roda o expurgo antes de ler o lead: os dados vencidos já não aparecem.
            detalhe = self.client.get(f"{self.PAGINA}/{lead.id}")
            self.assertEqual((detalhe.status_code, expurgo.call_count), (200, 1))
            self.assertNotIn("Leite 1L", detalhe.get_data(as_text=True))
            self.assertEqual(self.contar(BrainstormingDadosPdv), 0)
            self.db.session.expire_all()
            self.assertEqual(self.db.session.get(LeadAvaliacao, lead.id).status, "expirado")
            # Dentro da hora, a lista não roda o expurgo de novo; passada a hora, roda.
            self.assertEqual(self.client.get(self.PAGINA).status_code, 200)
            self.assertEqual(expurgo.call_count, 1)
            rotas._expurgo_ultimo["instante"] -= rotas.EXPURGO_A_CADA_SEGUNDOS
            self.assertEqual(self.client.get(self.PAGINA).status_code, 200)
            self.assertEqual(expurgo.call_count, 2)

    def test_lista_mostra_primeiro_a_avaliacao_mais_recente(self):
        # Um lead antigo reavaliado hoje vem antes de um lead criado ontem.
        import blueprints.leads as rotas
        from horario import agora_utc
        from models import LeadAvaliacao
        agora = agora_utc()
        reavaliado = LeadAvaliacao(**{**CONTATO, "nome_loja": "Loja reavaliada"}, criado_em=agora - timedelta(days=10),
                                   consentimento_em=agora)
        de_ontem = LeadAvaliacao(**{**CONTATO, "nome_loja": "Loja de ontem"}, criado_em=agora - timedelta(days=1),
                                 consentimento_em=agora - timedelta(days=1))
        sem_data = LeadAvaliacao(**{**CONTATO, "nome_loja": "Loja sem autorização"}, criado_em=agora - timedelta(days=5))
        self.db.session.add_all([reavaliado, de_ontem, sem_data])
        self.db.session.commit()
        lojas = [lead.nome_loja for lead in rotas._contexto_lista()["leads"]]
        self.assertEqual(lojas, ["Loja reavaliada", "Loja de ontem", "Loja sem autorização"])

    def test_erro_de_banco_no_expurgo_nao_derruba_a_tela(self):
        import blueprints.leads as rotas
        from sqlalchemy.exc import OperationalError
        self.login("admin-test@example.invalid")
        falha = OperationalError("DELETE FROM brainstorming_dados_pdv", {}, Exception("banco indisponível"))
        with patch.dict(rotas._expurgo_ultimo, {"instante": None}), \
                patch("leads_avaliacao.expurgar_dados_pdv", side_effect=falha):
            pagina = self.client.get(self.PAGINA)
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("Nova avaliação", pagina.get_data(as_text=True))

    def test_envio_acima_do_limite_responde_413_em_portugues(self):
        from models import BrainstormingDadosPdv, LeadAvaliacao
        self.login("admin-test@example.invalid")
        self._enviar(*self._lote())
        lead = self.db.session.scalar(select(LeadAvaliacao))
        detalhe = f"{self.PAGINA}/{lead.id}"
        token = self.csrf_token(detalhe)
        chamadas = self.worker_chamado
        self.app.config["MAX_CONTENT_LENGTH"] = 2048
        grande = NOV24 + b"9;Item;1;1,00;1,00;0,50;1\n" * 200
        novo = self._enviar(("nov.csv", grande), email="outro@example.invalid")
        reenvio = self.client.post(f"{detalhe}/reenviar", data={
            "csrf_token": token, "autorizacao": "1", "arquivos": [(io.BytesIO(grande), "nov.csv")]},
            content_type="multipart/form-data")
        for resposta in (novo, reenvio):
            self.assertEqual(resposta.status_code, 413)
            self.assertIn("passam de 2 KB somados. Nada foi gravado: envie menos meses por vez.",
                          resposta.get_data(as_text=True))
        self.assertEqual((self.worker_chamado, self.contar(LeadAvaliacao), self.contar(BrainstormingDadosPdv)),
                         (chamadas, 1, 6))

    def test_sem_as_migracoes_a_tela_avisa_e_nada_e_lido(self):
        from models import LeadAvaliacao
        self.login("admin-test@example.invalid")
        token = self.csrf_token(self.PAGINA)
        with patch("blueprints.leads.esquema_disponivel", return_value=False):
            pagina = self.client.get(self.PAGINA)
            envio = self.client.post(self.PAGINA, data={
                "csrf_token": token, "nome_contato": "Contato", "nome_loja": "Loja", "email": "lead@example.invalid",
                "autorizacao": "1", "arquivos": [(io.BytesIO(NOV24), "nov.csv")]}, content_type="multipart/form-data")
            detalhe = self.client.get(f"{self.PAGINA}/{uuid.uuid4()}")
            diagnostico = self.client.get(f"{self.PAGINA}/{uuid.uuid4()}/diagnostico.json")
        self.assertEqual(pagina.status_code, 200)
        self.assertEqual((diagnostico.status_code, diagnostico.get_json()["erro"],
                          diagnostico.headers["Cache-Control"]), (503, "esquema_pendente", "no-store"))
        self.assertIn("Retry-After", diagnostico.headers)
        self.assertIn("Falta aplicar as migrações da Avaliação de Cenário", pagina.get_data(as_text=True))
        self.assertEqual((envio.status_code, detalhe.status_code), (303, 303))
        self.assertEqual((self.contar(LeadAvaliacao), self.worker_chamado), (0, 0))


# =============================================================================
# 7. Infraestrutura: trava rígida de 500 mil células por aba (SEC-005)
# =============================================================================
class TravaDeCelulasTests(unittest.TestCase):
    def test_nenhuma_configuracao_passa_de_500_mil_celulas_por_aba(self):
        from dataclasses import replace
        from etl_limits import limits_from_environment
        self.assertEqual(ETLLimits().max_cells_per_sheet, 500_000)
        with self.assertRaises(ValueError):
            replace(ETLLimits(), max_cells_per_sheet=500_001)
        with patch.dict("os.environ", {"ETL_MAX_CELLS_PER_SHEET": "1000000"}), self.assertRaises(ValueError) as erro:
            limits_from_environment()
        self.assertNotIn("1000000", str(erro.exception))

    def test_csv_e_xlsx_com_mais_de_500_mil_celulas_numa_aba_sao_recusados(self):
        from openpyxl import Workbook
        from etl_inputs import read_raw, validate_input
        with tempfile.TemporaryDirectory(prefix="nexo-celulas-") as pasta:
            # 6 colunas x 83.334 linhas = 500.004 células, abaixo das 100 mil linhas.
            csv = Path(pasta) / "grande.csv"
            csv.write_text("a;b;c;d;e;f\n" + "1;2;3;4;5;6\n" * 83_333, encoding="utf-8")
            livro = Workbook(write_only=True)
            folha = livro.create_sheet("Dados")
            # Valores que mudam a cada linha: linhas idênticas comprimiriam demais e cairiam antes
            # na trava da razão de compressão (zip_ratio), e não na de células.
            for i in range(83_334):
                folha.append([i, i * 3, i * 7 % 99_991, i % 97, i * 13 % 10_007, i * 17 % 65_521])
            xlsx = Path(pasta) / "grande.xlsx"
            livro.save(xlsx)
            for caminho in (csv, xlsx):
                with self.subTest(formato=caminho.suffix):
                    with self.assertRaises(ETLInputError) as erro:
                        validate_input(str(caminho), caminho.suffix, ETLLimits())
                    self.assertEqual(erro.exception.code, "cells")
                    with self.assertRaises(ETLInputError):
                        read_raw(str(caminho), ETLLimits())


if __name__ == "__main__":
    unittest.main()
