"""Avaliação de Cenário (lead): motor no processo isolado, contrato, triagem, gravação e rotas.

  1. Motor (avaliacao_motor): a matriz produto x ano sai das mesmas regras do Raio-X, com a regra
     do ano (até hoje: período futuro recusado, mês corrente contado até hoje), a recusa de
     períodos sobrepostos e os tetos de tamanho e de valor.
  2. Equivalência: o resumo refeito a partir da staging gravada bate com o Raio-X dos mesmos
     arquivos (índice, faturamento, itens sem custo, margem negativa, abaixo do custo, estoque
     negativo); o recuperável difere só em centavos, pelo arredondamento por linha.
  3. Contrato do processo isolado (etl_runtime.validar_avaliacao) e isolamento do módulo.
  4. Triagem (triagem_cenario): marcas, blocos, motivos e resumos sem nome de produto; ritmo
     por dia coberto; limiares exatos nas fronteiras; Pareto e giro crítico.
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
import random
import re
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
LOTE = (NOV24, DEZ24, JAN25, FEV25)
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
# 4. Triagem
# =============================================================================
def _linha(chave, ano, *, quantidade="10", venda="100.00", com_custo=None, custo="60.00", perda="0.00",
           vazamento="0.00", recuperavel="0.00", meses=12, conflito=False, estoque=False):
    com_custo = venda if com_custo is None else com_custo
    return {
        "ano_exercicio": ano, "chave_produto": chave, "codigo_produto": None, "descricao_produto": f"Produto {chave}",
        "quantidade": Decimal(quantidade), "valor_venda_bruto": Decimal(venda),
        "quantidade_com_custo": Decimal(quantidade) if Decimal(com_custo) > 0 else Decimal("0"),
        "faturamento_com_custo": Decimal(com_custo), "custo_total": Decimal(custo) if Decimal(com_custo) > 0 else Decimal("0"),
        "perda": Decimal(perda), "vazamento_preco": Decimal(vazamento), "recuperavel": Decimal(recuperavel),
        "linhas": meses, "meses": meses, "variacao": False, "conflito_cadastral": conflito, "estoque_negativo": estoque,
    }


CENARIO = [
    # Urgência: recuperável alto e parou de vender no último ano.
    _linha("U", 2024, venda="1000.00", custo="600.00", vazamento="200.00", recuperavel="200.00"),
    # Urgência: recuperável alto e venda média caiu 60%.
    _linha("Q", 2024, quantidade="100", venda="1000.00", custo="500.00", recuperavel="80.00", perda="80.00"),
    _linha("Q", 2025, quantidade="40", venda="500.00", custo="250.00", recuperavel="70.00", perda="70.00"),
    # Atenção: margem de 5%.
    _linha("M", 2024, custo="95.00"), _linha("M", 2025, custo="95.00"),
    # Atenção: venda sem custo cadastrado.
    _linha("S", 2024), _linha("S", 2025, com_custo="0.00"),
    # Atenção: preço médio abaixo do custo médio.
    _linha("B", 2024), _linha("B", 2025, venda="90.00", custo="100.00"),
    # Eletiva: venda média caiu 30%, margem saudável, nada a recuperar.
    _linha("E", 2024, quantidade="100"), _linha("E", 2025, quantidade="70"),
    # Nenhum bloco.
    _linha("N", 2024), _linha("N", 2025),
]
# 12 relatórios mensais em cada ano, cobrindo o ano inteiro.
RELATORIOS = triagem_cenario.Cobertura(relatorios={2024: 12, 2025: 12}, dias={2024: 366, 2025: 365},
                                       mensais={2024: True, 2025: True})


class TriagemTests(unittest.TestCase):
    def test_marcas_da_linha(self):
        sem_custo = triagem_cenario.marcas(_linha("S", 2025, com_custo="0.00"))
        self.assertEqual((sem_custo["flag_venda_sem_custo"], sem_custo["flag_venda_abaixo_custo"],
                          sem_custo["custo_medio_registrado"], sem_custo["flag_omissao_erp"]), (True, False, None, False))
        abaixo = triagem_cenario.marcas(_linha("B", 2025, quantidade="3", venda="90.00", custo="100.00",
                                               recuperavel="10.00"))
        self.assertEqual((abaixo["flag_venda_sem_custo"], abaixo["flag_venda_abaixo_custo"]), (False, True))
        self.assertEqual(abaixo["custo_medio_registrado"], Decimal("33.3333"))
        self.assertEqual(abaixo["vazamento_estimado_reais"], Decimal("10.00"))

    def test_blocos_motivos_e_resumos(self):
        resultado = triagem_cenario.triar(CENARIO, RELATORIOS)
        self.assertEqual(resultado.blocos, {"U": "urgencia", "Q": "urgencia", "M": "atencao", "S": "atencao",
                                            "B": "atencao", "E": "eletiva", "N": None})
        self.assertEqual(resultado.motivos["U"], ("recuperavel_alto", "venda_parou"))
        self.assertEqual(resultado.motivos["Q"], ("recuperavel_alto", "venda_em_queda_forte"))
        self.assertEqual(resultado.motivos["M"], ("margem_baixa",))
        self.assertIn("venda_sem_custo", resultado.motivos["S"])
        self.assertIn("venda_abaixo_do_custo", resultado.motivos["B"])
        self.assertEqual(resultado.motivos["E"], ("venda_em_queda",))
        self.assertEqual((resultado.itens_venda_sem_custo, resultado.itens_venda_abaixo_custo), (1, 1))
        urgencia = resultado.resumos["urgencia"]
        self.assertEqual((urgencia["itens"], urgencia["faturamento"], urgencia["recuperavel"]), (2, "2500.00", "350.00"))
        self.assertEqual(urgencia["motivos"], {"recuperavel_alto": 2, "venda_em_queda_forte": 1, "venda_parou": 1})
        self.assertEqual((resultado.resumos["atencao"]["itens"], resultado.resumos["eletiva"]["itens"]), (3, 1))
        self.assertEqual(urgencia["versao"], triagem_cenario.VERSAO_TRIAGEM)
        texto = json.dumps(resultado.resumos, ensure_ascii=False)
        self.assertNotIn("Produto", texto)

    def test_triagem_nao_depende_da_ordem_das_linhas(self):
        referencia = triagem_cenario.triar(CENARIO, RELATORIOS)
        embaralhado = list(CENARIO)
        random.Random(7).shuffle(embaralhado)
        self.assertEqual(triagem_cenario.triar(embaralhado, RELATORIOS), referencia)

    def test_todo_motivo_tem_rotulo(self):
        resultado = triagem_cenario.triar(CENARIO, RELATORIOS)
        for motivos in resultado.motivos.values():
            for motivo in motivos:
                self.assertIn(motivo, triagem_cenario.ROTULOS_DOS_MOTIVOS)


class RitmoTests(unittest.TestCase):
    """O ritmo de venda é por dia coberto pelos relatórios: anos de durações diferentes não viram
    queda falsa, e o giro só existe com relatórios mensais."""

    @staticmethod
    def cobertura(**anos):
        return triagem_cenario.Cobertura(
            relatorios={int(a[1:]): r for a, (r, _d, _m) in anos.items()},
            dias={int(a[1:]): d for a, (_r, d, _m) in anos.items()},
            mensais={int(a[1:]): m for a, (_r, _d, m) in anos.items()})

    def test_ano_inteiro_e_semestre_corrente_no_mesmo_ritmo_nao_e_queda(self):
        linhas = [_linha("X", 2025, quantidade="120", venda="1200.00", custo="600.00", recuperavel="300.00", meses=1),
                  _linha("X", 2026, quantidade="60", venda="600.00", custo="300.00", recuperavel="150.00", meses=1)]
        resultado = triagem_cenario.triar(linhas, self.cobertura(a2025=(1, 365, False), a2026=(1, 181, False)))
        self.assertEqual((resultado.blocos["X"], resultado.motivos["X"]), ("eletiva", ("recuperavel_residual",)))

    def test_relatorio_anual_e_mensais_no_mesmo_ritmo_nao_e_queda(self):
        linhas = [_linha("X", 2025, quantidade="120", meses=1), _linha("X", 2026, quantidade="100", meses=10)]
        resultado = triagem_cenario.triar(linhas, self.cobertura(a2025=(1, 365, False), a2026=(10, 304, True)))
        self.assertEqual(resultado.blocos["X"], None)

    def test_queda_de_ritmo_de_verdade_continua_aparecendo(self):
        linhas = [_linha("X", 2025, quantidade="120", recuperavel="30.00", meses=1),
                  _linha("X", 2026, quantidade="20", recuperavel="0.00", meses=6)]
        resultado = triagem_cenario.triar(linhas, self.cobertura(a2025=(1, 365, False), a2026=(6, 181, True)))
        # 120 por ano (0,33 por dia) contra 20 em meio ano (0,11 por dia): queda de 66%.
        self.assertEqual(resultado.motivos["X"], ("recuperavel_alto", "venda_em_queda_forte"))

    def test_mes_curto_sem_venda_so_conta_quando_a_venda_era_esperada(self):
        linhas = [_linha("LENTO", 2025, quantidade="6", recuperavel="10.00", meses=1),
                  _linha("RAPIDO", 2025, quantidade="365", recuperavel="10.00", meses=1),
                  _linha("OUTRO", 2026, quantidade="10", meses=1)]
        resultado = triagem_cenario.triar(linhas, self.cobertura(a2025=(1, 365, False), a2026=(1, 31, True)))
        # LENTO previa 0,5 unidade em 31 dias: não vender não diz nada. RAPIDO previa 31.
        self.assertNotIn("venda_parou", resultado.motivos["LENTO"])
        self.assertIn("venda_parou", resultado.motivos["RAPIDO"])

    def test_giro_so_com_relatorios_mensais_no_ultimo_ano(self):
        linhas = [_linha("X", 2026, quantidade="10", meses=1)]
        # Vendeu em 1 dos 4 relatórios mensais: giro de 25%. Num relatório que cobre os 4 meses de uma
        # vez, a mesma venda não diz em quantos meses o produto vendeu.
        mensais = triagem_cenario.triar(linhas, self.cobertura(a2026=(4, 120, True)))
        anual = triagem_cenario.triar(linhas, self.cobertura(a2026=(1, 120, False)))
        self.assertEqual((mensais.blocos["X"], mensais.motivos["X"]), ("eletiva", ("giro_baixo",)))
        self.assertEqual(anual.blocos["X"], None)

    def test_cobertura_da_meta_do_envio(self):
        meta = {"arquivos_por_ano": {"2025": 1, "2026": 3}, "dias_por_ano": {"2025": 365, "2026": 90},
                "relatorios_mensais_por_ano": {"2025": False, "2026": True}}
        cobertura = triagem_cenario.Cobertura.do_envio(meta)
        self.assertEqual((cobertura.relatorios, cobertura.dias, cobertura.mensais),
                         ({2025: 1, 2026: 3}, {2025: 365, 2026: 90}, {2025: False, 2026: True}))
        invalidas = (
            {}, {"arquivos_por_ano": {"2025": 1}}, {**meta, "dias_por_ano": {"2025": 365}},
            {**meta, "dias_por_ano": {"2025": 0, "2026": 90}}, {**meta, "arquivos_por_ano": {"2025": "1", "2026": 3}},
            {**meta, "relatorios_mensais_por_ano": {"2025": 1, "2026": True}}, [],
        )
        for meta_invalida in invalidas:
            with self.subTest(meta=meta_invalida), self.assertRaises(ValueError):
                triagem_cenario.Cobertura.do_envio(meta_invalida)

    def test_linha_de_ano_fora_da_cobertura_e_recusada(self):
        with self.assertRaises(ValueError):
            triagem_cenario.triar([_linha("X", 2024)], self.cobertura(a2025=(1, 365, False)))


class FronteirasDaTriagemTests(unittest.TestCase):
    """Os limiares valem exatamente no valor da constante (fração exata, sem o erro de uma divisão em
    ponto flutuante) e os cortes da triagem: Pareto, giro crítico e três anos."""

    cobertura = staticmethod(RitmoTests.cobertura)

    def test_queda_de_exatamente_50_por_cento_entre_meses_de_28_e_30_dias(self):
        # 2,8 kg em fevereiro (28 dias) e 1,5 kg em abril (30 dias): o ritmo caiu exatamente pela metade.
        # Em ponto flutuante, 1 - (1,5/30)/(2,8/28) dá 0,4999...
        def linhas(abril):
            return [_linha("X", 2025, quantidade="2.8", recuperavel="10.00", meses=1),
                    _linha("X", 2026, quantidade=abril, recuperavel="10.00", meses=1)]
        cobertura = self.cobertura(a2025=(1, 28, True), a2026=(1, 30, True))
        exata = triagem_cenario.triar(linhas("1.5"), cobertura)
        self.assertEqual((exata.blocos["X"], exata.motivos["X"]),
                         ("urgencia", ("recuperavel_alto", "venda_em_queda_forte")))
        menor = triagem_cenario.triar(linhas("1.501"), cobertura)
        self.assertEqual((menor.blocos["X"], menor.motivos["X"]), ("eletiva", ("venda_em_queda", "recuperavel_residual")))

    def test_venda_esperada_de_exatamente_uma_unidade(self):
        # 2,44 kg em 2024 (366 dias) prevê exatamente 1 kg em 150 dias de 2025: sem venda nesse período,
        # o produto parou. Em ponto flutuante, 2,44/366 x 150 dá 0,9999...
        cobertura = self.cobertura(a2024=(12, 366, True), a2025=(5, 150, True))
        parou = triagem_cenario.triar([_linha("X", 2024, quantidade="2.44")], cobertura)
        # Sem recuperável alto, a venda que parou fica na Eletiva.
        self.assertEqual((parou.blocos["X"], parou.motivos["X"]), ("eletiva", ("venda_parou",)))
        lento = triagem_cenario.triar([_linha("X", 2024, quantidade="2.439")], cobertura)
        self.assertEqual((lento.blocos["X"], lento.motivos["X"]), (None, ()))

    def test_margem_de_exatamente_10_por_cento_nao_e_baixa(self):
        # Em ponto flutuante, a margem de R$ 3,00 com custo de R$ 2,70 dá 0,0999..., subtraindo em
        # Decimal ou em float antes da divisão.
        cobertura = self.cobertura(a2025=(1, 365, False))
        no_limite = triagem_cenario.triar([_linha("X", 2025, venda="3.00", custo="2.70")], cobertura)
        abaixo = triagem_cenario.triar([_linha("X", 2025, venda="3.00", custo="2.71")], cobertura)
        self.assertEqual((no_limite.blocos["X"], abaixo.motivos["X"]), (None, ("margem_baixa",)))

    def test_margem_em_queda_de_exatamente_5_pontos(self):
        # Margem de 30% para 25%: em ponto flutuante, 0,30 - 0,25 dá 0,0499...
        cobertura = self.cobertura(a2024=(1, 366, False), a2025=(1, 365, False))

        def linhas(custo_2025):
            return [_linha("X", 2024, custo="70.00"), _linha("X", 2025, custo=custo_2025)]
        caiu = triagem_cenario.triar(linhas("75.00"), cobertura)
        self.assertEqual((caiu.blocos["X"], caiu.motivos["X"]), ("atencao", ("margem_em_queda",)))
        quase = triagem_cenario.triar(linhas("74.99"), cobertura)
        self.assertEqual((quase.blocos["X"], quase.motivos["X"]), (None, ()))
        # Margem que subiu não é queda.
        subiu = triagem_cenario.triar([_linha("X", 2024, custo="75.00"), _linha("X", 2025, custo="70.00")], cobertura)
        self.assertEqual(subiu.blocos["X"], None)

    def test_corte_de_pareto_em_80_por_cento_e_empate(self):
        cobertura = self.cobertura(a2024=(12, 366, True), a2025=(12, 365, True))

        def pararam(**recuperaveis):
            # Produtos que pararam de vender em 2025, cada um com R$ 100,00 de faturamento em 2024.
            return [_linha(chave, 2024, recuperavel=valor) for chave, valor in recuperaveis.items()]
        resultado = triagem_cenario.triar(pararam(A="50.00", B="30.00", C="15.00", D="5.00"), cobertura)
        # A e B somam 80% do recuperável: o corte para em B; C e D ficam com o recuperável residual.
        self.assertEqual(resultado.blocos, {"A": "urgencia", "B": "urgencia", "C": "eletiva", "D": "eletiva"})
        self.assertEqual(resultado.motivos["A"], ("recuperavel_alto", "venda_parou"))
        self.assertEqual(resultado.motivos["C"], ("recuperavel_residual",))
        # Empate no corte: a chave desempata, sempre do mesmo jeito (A, B e M somam os 80%; Z fica de fora).
        empate = pararam(A="40.00", Z="20.00", M="20.00", B="20.00")
        esperado = {"A": "urgencia", "B": "urgencia", "M": "urgencia", "Z": "eletiva"}
        self.assertEqual(triagem_cenario.triar(empate, cobertura).blocos, esperado)
        self.assertEqual(triagem_cenario.triar(list(reversed(empate)), cobertura).blocos, esperado)

    def test_giro_critico_com_recuperavel_alto_vai_para_urgencia(self):
        quatro = self.cobertura(a2026=(4, 120, True))
        uma_vez = triagem_cenario.triar([_linha("X", 2026, recuperavel="10.00", meses=1)], quatro)
        self.assertEqual((uma_vez.blocos["X"], uma_vez.motivos["X"]), ("urgencia", ("recuperavel_alto", "giro_critico")))
        duas_vezes = triagem_cenario.triar([_linha("X", 2026, recuperavel="10.00", meses=2)], quatro)
        self.assertEqual((duas_vezes.blocos["X"], duas_vezes.motivos["X"]),
                         ("eletiva", ("giro_baixo", "recuperavel_residual")))
        # Com 3 relatórios mensais, vender em 1 deles ainda não é giro crítico.
        tres = triagem_cenario.triar([_linha("X", 2026, recuperavel="10.00", meses=1)],
                                     self.cobertura(a2026=(3, 90, True)))
        self.assertEqual((tres.blocos["X"], tres.motivos["X"]), ("eletiva", ("giro_baixo", "recuperavel_residual")))

    def test_tres_anos_comparam_o_ultimo_com_o_anterior_mais_recente_com_venda(self):
        cobertura = self.cobertura(a2024=(1, 366, False), a2025=(1, 365, False), a2026=(1, 365, False))
        linhas = [
            # X: 2025 e 2026 no mesmo ritmo; a queda contra 2024 não conta.
            _linha("X", 2024, quantidade="100"), _linha("X", 2025, quantidade="50"), _linha("X", 2026, quantidade="50"),
            # Y: sem venda em 2025; 2026 compara com 2024 (100 em 366 dias contra 40 em 365: queda de 60%).
            _linha("Y", 2024, quantidade="100"), _linha("Y", 2026, quantidade="40"),
        ]
        resultado = triagem_cenario.triar(linhas, cobertura)
        self.assertEqual(resultado.blocos["X"], None)
        self.assertEqual((resultado.blocos["Y"], resultado.motivos["Y"]), ("eletiva", ("venda_em_queda",)))


class QuantizacaoTests(unittest.TestCase):
    def test_dinheiro_com_2_casas_e_quantidade_com_3_arredondando_o_meio_para_cima(self):
        import avaliacao_cenario
        valores = dict.fromkeys(avaliacao_motor.COLUNAS, 0)
        valores.update(ano_exercicio=2025, chave_produto="0" * 16, codigo_produto=None, descricao_produto="X",
                       quantidade=1.0005, quantidade_com_custo=2.0004, valor_venda_bruto=2.675, recuperavel=0.125,
                       custo_total=1.005, variacao=False, conflito_cadastral=False, estoque_negativo=False)
        linha = avaliacao_cenario.quantizar({"colunas": list(avaliacao_motor.COLUNAS),
                                             "valores": [[valores[c] for c in avaliacao_motor.COLUNAS]]})[0]
        # O valor que o JSON traz ("2.675"), e não o binário do float (2,67499...).
        self.assertEqual((linha["quantidade"], linha["quantidade_com_custo"]), (Decimal("1.001"), Decimal("2.000")))
        self.assertEqual((linha["valor_venda_bruto"], linha["recuperavel"], linha["custo_total"]),
                         (Decimal("2.68"), Decimal("0.13"), Decimal("1.01")))


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
        self.assertEqual((leite.perda, leite.vazamento_estimado_reais, leite.bloco_triagem),
                         (Decimal("10.00"), Decimal("10.00"), "urgencia"))
        self.assertEqual({l.versao_regras for l in linhas}, {avaliacao_motor.VERSAO_REGRAS_LEAD})
        self.db.session.refresh(lead)
        recomendado = raio_x.recomendar(resultado["raio_x"], 24)["plano"]
        self.assertEqual((lead.status, lead.arquivos, lead.vazamento_total_reais, lead.plano_recomendado_codigo),
                         ("concluido", 4, Decimal("20.00"), recomendado))
        self.assertEqual(lead.score_saude_cadastral,
                         Decimal(str(resultado["raio_x"]["saude"]["indice"])).quantize(Decimal("0.01")))
        self.assertEqual((lead.periodo_inicio, lead.periodo_fim), (date(2024, 11, 1), date(2025, 2, 28)))
        self.assertEqual((lead.itens_venda_sem_custo, lead.itens_venda_abaixo_custo), (1, 0))
        self.assertEqual((lead.registrado_por, lead.versao_triagem), (self.admin_id, triagem_cenario.VERSAO_TRIAGEM))
        self.assertIsNotNone(lead.consentimento_em)
        resumos = {b: json.loads(getattr(lead, f"resumo_{b}_json")) for b in triagem_cenario.BLOCOS}
        self.assertEqual({b: r["itens"] for b, r in resumos.items()}, {"urgencia": 1, "atencao": 1, "eletiva": 2})
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
        lead, _ = self._registrar(*LOTE)
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
        lead, _ = self._registrar(*LOTE)
        identificador = str(lead.id)
        self.db.session.remove()
        saida = self.app.test_cli_runner().invoke(args=["leads", "retriar", identificador])
        self.assertEqual(saida.exit_code, 0, saida.output)
        self.assertIn("urgência 1, atenção 1, eletiva 2", saida.output)
        for dado in ("lead@example.invalid", "Contato fictício", "Mercado fictício", "Leite"):
            self.assertNotIn(dado, saida.output)
        invalido = self.app.test_cli_runner().invoke(args=["leads", "retriar", "nao-e-uuid"])
        self.assertNotEqual(invalido.exit_code, 0)


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

    def _lote(self):
        return [(f"mes_{i}.csv", conteudo) for i, conteudo in enumerate(LOTE, 1)]

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
        resposta = self._enviar(*self._lote())
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
        self._enviar(*self._lote())
        lead = self.db.session.scalar(select(LeadAvaliacao))
        detalhe = f"{self.PAGINA}/{lead.id}"
        motivos_do_item = "<td>Valor recuperável alto; Ritmo de venda caiu 50% ou mais</td>"
        self.assertIn(motivos_do_item, self.client.get(detalhe).get_data(as_text=True))
        lead.versao_triagem = "triagem-anterior"
        self.db.session.commit()
        html = self.client.get(detalhe).get_data(as_text=True)
        # O bloco gravado e o resumo dele ficam; os motivos refeitos com as regras atuais, não.
        self.assertIn("Triagem feita com regras anteriores", html)
        self.assertIn("Leite 1L", html)
        self.assertNotIn(motivos_do_item, html)

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
                 ("post", f"{detalhe}/retriar"))
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
        self.assertEqual(pagina.status_code, 200)
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
