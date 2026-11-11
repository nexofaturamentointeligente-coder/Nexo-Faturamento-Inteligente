"""Leitor de planilha .xls do Excel 97-2003 (etl_biff): BIFF8 dentro de um contêiner OLE2.

  1. Valores: textos (acento, cedilha, um byte por caractere e UTF-16), números inteiros e fracionários,
     RK e MULRK, datas nos sistemas 1900 e 1904, data com hora, booleano, erro, fórmula com o valor
     guardado e célula em branco formatada.
  2. Leiaute: o relatório paginado de ERP (células espalhadas e linhas em branco no meio), duas abas, aba
     oculta, gráfico e folha de macro, gráfico embutido, leitura só de contagem e células fora de ordem.
  3. Textos compartilhados: SST partido em CONTINUE em todos os pontos, troca de largura na continuação,
     texto rico e fonético, par substituto partido e textos sem uso.
  4. Contêiner: mini-fluxo (mini-FAT), setores de 4096 bytes, setores extras da DIFAT, setores fora de
     ordem, último setor incompleto e os outros fluxos, que não são abertos.
  5. Limites: abas, linhas, colunas, células com valor, área e caracteres, aplicados durante a leitura;
     vazio.
  6. Recusas: o que não é OLE2, Excel antigo, arquivo com senha, truncado, ciclo e cruzamento nas cadeias,
     registro gigante ou cortado, índice de texto e de formato fora da tabela, aba fora do fluxo.
  7. Robustez: prazo de quem chama, MemoryError, falha inesperada e centenas de arquivos corrompidos.
  8. O módulo em si: só biblioteca padrão e a regra dos formatos de data.

Os arquivos são fictícios, montados em memória por tests/xls_support.py (sem xlwt, xlrd nem openpyxl).
"""

import ast
from datetime import datetime
import inspect
import io
from pathlib import Path
import struct
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock

import etl_biff
from etl_biff import LeituraRecusada, ler
from tests import xls_support as xls

LIMITES = dict(max_abas=16, max_linhas=100_000, max_colunas=128, max_celulas=500_000, max_caracteres=16_384)
CHAVES = {"abas", "aba", "linhas", "colunas", "celulas", "celulas_no_arquivo", "valores"}
CODIGOS = {"invalid", "unsupported", "sheets", "rows", "columns", "cells", "field_chars", "empty"}
EMISSAO = datetime(2026, 11, 9, 17, 3, 38)


def pasta_simples():
    """Uma pasta pequena com um valor de cada tipo, para os testes de contêiner e de robustez."""
    pasta = xls.Pasta()
    aba = pasta.aba("Dados")
    aba.por(0, 0, "PRODUTO FICTÍCIO AÇÃO").por(0, 1, 7).por(0, 2, 12.5).por(0, 3, datetime(2026, 11, 9))
    aba.por(1, 0, "Preço € Ω").por(1, 1, True).por(1, 2, EMISSAO).por(1, 3, None)
    aba.bruto(xls.varios_rk(2, 1, [xls.rk_de_inteiro(1234, True), xls.rk_de_real(1.5), xls.rk_de_inteiro(-5)]),
              xls.formula(3, 0, 2.5), xls.formula(3, 1, "texto de fórmula"), xls.rotulo(3, 2, "rótulo direto"))
    return pasta


VALORES_DA_PASTA_SIMPLES = [
    ["PRODUTO FICTÍCIO AÇÃO", 7.0, 12.5, datetime(2026, 11, 9)],
    ["Preço € Ω", True, EMISSAO, None],
    [None, 12.34, 1.5, -5.0],
    [2.5, "texto de fórmula", "rótulo direto", None],
]


class _BiffCase(unittest.TestCase):
    """Base dos testes. `ler` lê os bytes da memória (só o `open` do módulo é trocado), para a suíte, que lê
    mais de mil arquivos, não depender da velocidade do disco. `ler_do_disco` grava o arquivo num diretório
    temporário e lê pelo caminho, como em produção; cada grupo de testes passa por ele."""

    def setUp(self):
        self._diretorio = None
        self._gravados = 0

    @property
    def diretorio(self):
        if self._diretorio is None:
            pasta = tempfile.TemporaryDirectory(prefix="nexo-biff-")
            self.addCleanup(pasta.cleanup)
            self._diretorio = Path(pasta.name)
        return self._diretorio

    def gravar(self, dados):
        self._gravados += 1
        caminho = self.diretorio / f"planilha_{self._gravados}.xls"
        caminho.write_bytes(bytes(dados))
        return caminho

    def ler(self, dados, **opcoes):
        dados = bytes(dados)
        with mock.patch.object(etl_biff, "open", lambda caminho, modo: io.BytesIO(dados), create=True):
            return ler("na_memoria.xls", **{**LIMITES, **opcoes})

    def ler_do_disco(self, dados, **opcoes):
        return ler(self.gravar(dados), **{**LIMITES, **opcoes})

    def valores(self, dados, **opcoes):
        return self.ler(dados, **opcoes)["valores"]

    def recusa(self, codigo, dados, *, do_disco=False, **opcoes):
        """Confere que a leitura é recusada com o código dado; devolve a mensagem."""
        with self.assertRaises(LeituraRecusada) as contexto:
            (self.ler_do_disco if do_disco else self.ler)(dados, **opcoes)
        erro = contexto.exception
        self.assertEqual(erro.codigo, codigo)
        self.assertEqual(str(erro), erro.mensagem)
        self.assertTrue(erro.mensagem.endswith("."))
        return erro.mensagem

    def pasta_com(self, *registros, **opcoes):
        """Uma pasta de uma aba só com os registros dados."""
        pasta = xls.Pasta(**opcoes)
        pasta.aba().bruto(*registros)
        return pasta

    def conferir_tipos(self, valores):
        """Todo valor devolvido é str, float, bool, datetime ou None, e as linhas têm o mesmo comprimento."""
        self.assertEqual(len({len(linha) for linha in valores}), 1)
        for linha in valores:
            self.assertIs(type(linha), list)
            for valor in linha:
                self.assertIn(type(valor), (str, float, bool, datetime, type(None)))


# =============================================================================
# 1. Valores
# =============================================================================
class ValoresTests(_BiffCase):
    def test_textos_com_acento_e_cedilha_em_um_byte_e_em_utf16(self):
        pasta = xls.Pasta()
        aba = pasta.aba()
        aba.por(0, 0, "AÇÃO, PÃO DE QUEIJO E MAÇÃ")            # cabe em Latin-1: um byte por caractere
        aba.por(0, 1, "Preço em € – Ômega Ω")                  # só cabe em UTF-16
        aba.por(0, 2, "Sorriso 😀 fora do plano básico")       # par substituto
        aba.bruto(xls.rotulo(1, 0, "rótulo em um byte: é ç ã"), xls.rotulo(1, 1, "rótulo largo: é ç ã", largo=True),
                  xls.rotulo(1, 2, "rótulo rico", identificador=xls.RSTRING))
        resultado = self.ler_do_disco(pasta.arquivo())
        self.assertEqual(resultado["valores"], [
            ["AÇÃO, PÃO DE QUEIJO E MAÇÃ", "Preço em € – Ômega Ω", "Sorriso 😀 fora do plano básico"],
            ["rótulo em um byte: é ç ã", "rótulo largo: é ç ã", "rótulo rico"]])
        self.assertEqual(set(resultado), CHAVES)
        self.assertEqual({k: v for k, v in resultado.items() if k != "valores"},
                         {"abas": 1, "aba": 0, "linhas": 2, "colunas": 3, "celulas": 6, "celulas_no_arquivo": 6})
        # A tabela de textos guardou o primeiro em um byte por caractere e os outros em UTF-16.
        sst = xls.tabela_de_textos(pasta.textos)
        self.assertIn("AÇÃO, PÃO DE QUEIJO E MAÇÃ".encode("latin-1"), sst)
        self.assertIn("Preço em € – Ômega Ω".encode("utf-16-le"), sst)

    def test_texto_so_de_espacos_e_valor_e_texto_vazio_nao(self):
        pasta = xls.Pasta()
        pasta.aba().por(0, 0, "   ").por(0, 1, "").por(0, 2, "x").bruto(xls.rotulo(1, 0, ""), xls.rotulo(1, 1, " "))
        resultado = self.ler(pasta.arquivo())
        self.assertEqual(resultado["valores"], [["   ", None, "x"], [None, " ", None]])
        self.assertEqual(resultado["celulas"], 3)

    def test_numeros_inteiros_e_fracionarios_saem_float(self):
        pasta = xls.Pasta()
        aba = pasta.aba()
        numeros = [7, 0, -3, 12.5, 0.1, -12345678.91, 1e-7, 3.8081, 2 ** 40, 710718.23]
        for coluna, valor in enumerate(numeros):
            aba.por(0, coluna, valor)
        aba.bruto(xls.numero(1, 0, 42.0), xls.numero(1, 1, 3.5, pasta.xf(xls.FORMATO_DE_DINHEIRO)),
                  xls.numero(1, 2, 2.0, pasta.xf(xls.FORMATO_DE_QUANTIDADE)), xls.numero(1, 3, 0.07, pasta.xf(10)))
        valores = self.valores(pasta.arquivo())
        self.assertEqual(valores[0], [float(valor) for valor in numeros])
        self.assertEqual(valores[1][:4], [42.0, 3.5, 2.0, 0.07])
        for valor in valores[0] + valores[1][:4]:
            self.assertIs(type(valor), float)

    def test_rk_inteiro_real_e_dividido_por_cem(self):
        casos = [(xls.rk_de_inteiro(1234), 1234.0), (xls.rk_de_inteiro(1234, centesimos=True), 12.34),
                 (xls.rk_de_inteiro(-5), -5.0), (xls.rk_de_inteiro(-150, centesimos=True), -1.5),
                 (xls.rk_de_inteiro(2 ** 29 - 1), 536870911.0), (xls.rk_de_inteiro(-2 ** 29), -536870912.0),
                 (xls.rk_de_real(1.5), 1.5), (xls.rk_de_real(1.5, centesimos=True), 0.015),
                 (xls.rk_de_real(-0.25), -0.25), (xls.rk_de_inteiro(0), 0.0)]
        pasta = self.pasta_com(*(xls.rk(0, coluna, compacto) for coluna, (compacto, _) in enumerate(casos)))
        valores = self.valores(pasta.arquivo())
        self.assertEqual(valores, [[esperado for _, esperado in casos]])
        self.assertTrue(all(type(valor) is float for valor in valores[0]))

    def test_mulrk_grava_colunas_seguidas_da_mesma_linha(self):
        compactos = [xls.rk_de_inteiro(1), xls.rk_de_inteiro(250, centesimos=True), xls.rk_de_real(0.5)]
        pasta = self.pasta_com(xls.varios_rk(2, 3, compactos), xls.varios_rk(0, 0, [xls.rk_de_inteiro(9)]))
        resultado = self.ler(pasta.arquivo())
        self.assertEqual(resultado["valores"], [[9.0, None, None, None, None, None],
                                                [None] * 6,
                                                [None, None, None, 1.0, 2.5, 0.5]])
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (3, 6, 4))

    def test_datas_e_data_com_hora_no_sistema_1900(self):
        pasta = xls.Pasta()
        aba = pasta.aba()
        aba.por(0, 0, datetime(2026, 11, 9))                       # formato personalizado "dd/mm/yy"
        aba.por(0, 1, EMISSAO)                                     # "dd/MM/yyyy hh:mm"
        aba.por(0, 2, datetime(2023, 11, 9), 14)                   # formato embutido de data
        aba.por(0, 3, datetime(2024, 2, 29, 23, 59, 59), 22)       # formato embutido de data e hora
        aba.por(0, 4, datetime(1900, 3, 1), 14)                    # a primeira data da faixa (série 61)
        aba.por(0, 5, datetime(9999, 12, 31), 14)                  # a última
        aba.por(0, 6, datetime(2026, 1, 31, 12, 0, 0, 250_000))    # arredondada ao milissegundo
        valores = self.valores(pasta.arquivo())
        self.assertEqual(valores, [[datetime(2026, 11, 9), EMISSAO, datetime(2023, 11, 9),
                                    datetime(2024, 2, 29, 23, 59, 59), datetime(1900, 3, 1), datetime(9999, 12, 31),
                                    datetime(2026, 1, 31, 12, 0, 0, 250_000)]])
        self.assertTrue(all(type(valor) is datetime for valor in valores[0]))

    def test_datas_no_sistema_1904(self):
        pasta = xls.Pasta(sistema_1904=True)
        aba = pasta.aba()
        aba.por(0, 0, datetime(2026, 11, 9)).por(0, 1, EMISSAO).por(0, 2, datetime(1904, 1, 2), 14)
        self.assertEqual(xls.serie(datetime(2026, 11, 9), True), xls.serie(datetime(2026, 11, 9)) - 1462)
        self.assertEqual(self.valores(pasta.arquivo()), [[datetime(2026, 11, 9), EMISSAO, datetime(1904, 1, 2)]])

    def test_serie_fora_da_faixa_de_datas_continua_numero(self):
        for sistema_1904, fora, dentro in (
                (False, [0.5, 45.0, 60.0, 60.99, -3.0, 2958466.0, 1e12], [(61.0, datetime(1900, 3, 1))]),
                (True, [0.75, 0.0, -1.0, 2957004.0], [(1.0, datetime(1904, 1, 2)), (1.5, datetime(1904, 1, 2, 12))])):
            with self.subTest(sistema_1904=sistema_1904):
                pasta = xls.Pasta(sistema_1904=sistema_1904)
                data = pasta.xf(xls.FORMATO_DE_DATA)
                aba = pasta.aba()
                aba.bruto(*(xls.numero(0, coluna, valor, data) for coluna, valor in enumerate(fora)))
                aba.bruto(*(xls.numero(1, coluna, valor, data) for coluna, (valor, _) in enumerate(dentro)))
                valores = self.valores(pasta.arquivo())
                self.assertEqual(valores[0], fora)
                self.assertTrue(all(type(valor) is float for valor in valores[0]))
                self.assertEqual(valores[1][:len(dentro)], [esperado for _, esperado in dentro])

    def test_ultimo_instante_do_ultimo_dia_nao_estoura(self):
        """A fração que arredonda para o dia 01/01/10000 não vira data: o valor fica como número."""
        quase = 2958465.9999999995
        pasta = xls.Pasta()
        pasta.aba().bruto(xls.numero(0, 0, quase, pasta.xf(xls.FORMATO_DE_DATA)))
        self.assertEqual(self.valores(pasta.arquivo()), [[quase]])

    def test_formato_de_paginacao_com_d_solto_nao_e_data(self):
        pasta = xls.Pasta()
        pasta.aba().por(0, 0, 45000, xls.FORMATO_DE_PAGINA).por(0, 1, 45000.0, '0 "dias"').por(0, 2, 45000, "000")
        self.assertEqual(self.valores(pasta.arquivo()), [[45000.0, 45000.0, 45000.0]])

    def test_booleano_e_bool_e_erro_e_vazio(self):
        pasta = self.pasta_com(xls.booleano(0, 0, True), xls.booleano(0, 1, False), xls.erro(0, 2),
                               xls.erro(0, 3, 0x2A), xls.booleano(0, 4, True))
        resultado = self.ler(pasta.arquivo())
        self.assertEqual(resultado["valores"], [[True, False, None, None, True]])
        self.assertTrue(all(type(valor) is bool for valor in (resultado["valores"][0][0], resultado["valores"][0][1])))
        self.assertEqual(resultado["celulas"], 3)  # FALSO é valor; erro não é

    def test_formula_devolve_o_valor_guardado(self):
        pasta = xls.Pasta()
        data = pasta.xf(xls.FORMATO_DE_DATA)
        pasta.aba().bruto(
            xls.formula(0, 0, 2.5), xls.formula(0, 1, 46335.0, data), xls.formula(0, 2, True),
            xls.formula(0, 3, False), xls.formula(0, 4, "soma fictícia: ação"), xls.formula(0, 5, "largo Ω €"),
            xls.formula(1, 0, xls.Erro()), xls.formula(1, 1, ""), xls.formula(1, 2, "sem STRING", com_texto=False),
            xls.formula(1, 3, 10.0),
            # Fórmula compartilhada: o registro SHRFMLA fica entre a FORMULA e o STRING.
            xls.formula(2, 0, "compartilhada", com_texto=False), xls.registro(xls.SHRFMLA, bytes(12)),
            xls.texto_de_formula("compartilhada"))
        resultado = self.ler(pasta.arquivo())
        self.assertEqual(resultado["valores"], [
            [2.5, datetime(2026, 11, 9), True, False, "soma fictícia: ação", "largo Ω €"],
            [None, None, None, 10.0, None, None],
            ["compartilhada", None, None, None, None, None]])
        self.assertEqual(resultado["celulas"], 8)

    def test_texto_de_formula_continua_em_continue(self):
        longo = "fórmula de texto comprido " * 40
        largo = "Ω" * 300
        for limite in (20, 21, 64, 8224):
            with self.subTest(limite=limite):
                pasta = self.pasta_com(xls.formula(0, 0, longo, limite=limite), xls.numero(0, 1, 1.0),
                                       xls.formula(1, 0, largo, limite=limite))
                self.assertEqual(self.valores(pasta.arquivo()), [[longo, 1.0], [largo, None]])

    def test_texto_de_formula_pela_metade_e_recusado(self):
        registros = xls.formula(0, 0, "texto que continua " * 10, limite=30)
        cortado = registros[:registros.rindex(struct.pack("<HH", xls.CONTINUE, 30))]  # sem o último CONTINUE
        self.recusa("invalid", self.pasta_com(cortado, xls.numero(1, 0, 1.0)).arquivo())

    def test_string_solto_nao_vai_para_a_formula_errada(self):
        """Um STRING que não vem logo depois da sua FORMULA não é atribuído a célula nenhuma."""
        pasta = self.pasta_com(xls.formula(0, 0, "perdido", com_texto=False), xls.numero(0, 1, 5.0),
                               xls.texto_de_formula("intruso"), xls.texto_de_formula("outro intruso"))
        self.assertEqual(self.valores(pasta.arquivo()), [[None, 5.0]])

    def test_celula_em_branco_formatada_nao_conta(self):
        pasta = xls.Pasta()
        moeda = pasta.xf(xls.FORMATO_DE_DINHEIRO)
        pasta.aba().por(1, 1, "único valor").bruto(
            xls.branco(0, 0, moeda), xls.branco(1, 0), xls.branco(1, 9, moeda), xls.brancos(1, 2, 7, moeda),
            xls.branco(40, 3), xls.brancos(41, 0, 19), xls.branco(65535, 255))
        resultado = self.ler(pasta.arquivo())
        self.assertEqual(resultado["valores"], [[None, None], [None, "único valor"]])
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (2, 2, 1))
        # Nem os limites enxergam as células em branco.
        self.assertEqual(self.ler(pasta.arquivo(), max_linhas=2, max_colunas=2, max_celulas=1)["celulas"], 1)

    def test_numero_infinito_ou_nan_conta_como_vazio(self):
        pasta = self.pasta_com(xls.numero(0, 0, float("inf")), xls.numero(0, 1, float("nan")), xls.numero(0, 2, 1.0),
                               xls.numero(0, 3, float("-inf")), xls.formula(0, 4, float("inf")))
        resultado = self.ler(pasta.arquivo())
        self.assertEqual(resultado["valores"], [[None, None, 1.0]])
        self.assertEqual(resultado["celulas"], 1)

    def test_vale_o_ultimo_registro_de_uma_mesma_celula(self):
        pasta = self.pasta_com(xls.numero(0, 0, 1.0), xls.numero(0, 0, 2.0),      # valor sobre valor
                               xls.numero(0, 1, 3.0), xls.erro(0, 1),             # erro apaga o valor
                               xls.erro(0, 2), xls.numero(0, 2, 4.0),             # valor sobre erro
                               xls.numero(0, 3, 5.0), xls.branco(0, 3),           # em branco não apaga nada
                               xls.numero(1, 5, 6.0), xls.erro(1, 5),             # a linha 1 fica sem valor
                               xls.numero(0, 9, 7.0), xls.formula(0, 9, ""))      # o fim da linha 0 é apagado
        for materializar in (True, False):
            with self.subTest(materializar=materializar):
                resultado = self.ler(pasta.arquivo(), materializar=materializar)
                self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (1, 4, 3))
                self.assertEqual(resultado["valores"], [[2.0, None, 4.0, 5.0]] if materializar else None)


# =============================================================================
# 2. Leiaute
# =============================================================================
class LeiauteTests(_BiffCase):
    def test_relatorio_paginado_com_celulas_espalhadas_e_linhas_em_branco(self):
        pasta, esperado = xls.relatorio_paginado(paginas=3, itens_por_pagina=4)
        resultado = self.ler_do_disco(pasta.arquivo())
        self.assertEqual(self.ler(pasta.arquivo()), resultado)  # da memória, como leem os outros testes
        valores = resultado["valores"]
        linhas = max(linha for linha, _ in esperado) + 1
        self.assertEqual((resultado["linhas"], resultado["colunas"]), (linhas, 18))
        self.assertEqual(resultado["celulas"], len(esperado))
        self.assertEqual(len(valores), linhas)
        self.conferir_tipos(valores)
        self.assertEqual({(linha, coluna): valor for linha, celulas in enumerate(valores)
                          for coluna, valor in enumerate(celulas) if valor is not None}, esperado)
        # Os dados só ocupam as colunas do relatório; a coluna 18 só tem células em branco formatadas.
        self.assertEqual({coluna for _, coluna in esperado}, set(xls.COLUNAS_DO_RELATORIO))
        # Linhas inteiras em branco no meio do relatório (antes do título e entre os itens).
        em_branco = [indice for indice, celulas in enumerate(valores) if all(valor is None for valor in celulas)]
        self.assertGreaterEqual(len(em_branco), 12)
        self.assertIn(0, em_branco)
        # Tipos como o arquivo guarda: data, texto, número; a paginação "000 de " é número, não data.
        item = valores[5]
        self.assertEqual([type(item[coluna]) for coluna in xls.COLUNAS_DO_RELATORIO],
                         [datetime, float, str, str, float, float, float])
        self.assertEqual((valores[1][16], valores[1][17]), (EMISSAO, 1.0))

    def test_escolhe_a_aba_de_mais_celulas_com_valor(self):
        pasta = xls.Pasta()
        primeira = pasta.aba("Capa")
        primeira.por(0, 0, "capa").por(0, 1, 1)
        primeira.bruto(*(xls.brancos(linha, 0, 50) for linha in range(1, 40)))  # muita formatação, pouco valor
        segunda = pasta.aba("Dados")
        segunda.por(0, 0, "a").por(1, 0, "b").por(2, 2, 3.5)
        pasta.aba("Vazia")
        resultado = self.ler(pasta.arquivo())
        self.assertEqual({k: v for k, v in resultado.items() if k != "valores"},
                         {"abas": 3, "aba": 1, "linhas": 3, "colunas": 3, "celulas": 3, "celulas_no_arquivo": 5})
        self.assertEqual(resultado["valores"], [["a", None, None], ["b", None, None], [None, None, 3.5]])

    def test_no_empate_fica_a_primeira_aba(self):
        pasta = xls.Pasta()
        pasta.aba("Um").por(0, 0, "um").por(0, 1, 1)
        pasta.aba("Dois").por(0, 0, "dois").por(0, 1, 2)
        resultado = self.ler(pasta.arquivo())
        self.assertEqual((resultado["aba"], resultado["valores"]), (0, [["um", 1.0]]))

    def test_aba_oculta_conta_e_grafico_e_folha_de_macro_nao(self):
        pasta = xls.Pasta()
        grafico = pasta.aba("Gráfico", tipo=2)
        grafico.bruto(*(xls.numero(linha, 0, 1.0) for linha in range(30)))  # os números em cache de um gráfico
        pasta.aba("Macro", tipo=1).por(0, 0, "=EXEC() fictício").por(1, 0, "outra linha").por(2, 0, "mais uma")
        pasta.aba("Visível").por(0, 0, "visível")
        pasta.aba("Oculta", visibilidade=1).por(0, 0, "oculta").por(0, 1, 2)
        resultado = self.ler(pasta.arquivo())
        # "aba" é o índice entre as planilhas: Visível é a 0 e Oculta é a 1.
        self.assertEqual({k: v for k, v in resultado.items() if k != "valores"},
                         {"abas": 2, "aba": 1, "linhas": 1, "colunas": 2, "celulas": 2, "celulas_no_arquivo": 3})
        self.assertEqual(resultado["valores"], [["oculta", 2.0]])
        self.assertEqual(self.ler(pasta.arquivo(), max_abas=2)["abas"], 2)  # gráfico e macro não gastam o limite

    def test_grafico_embutido_na_aba_e_pulado_inteiro(self):
        embutido = (xls.bof(xls.GRAFICO) + xls.numero(0, 0, 999.0) + xls.bof(xls.GRAFICO) + xls.numero(5, 5, 888.0)
                    + xls.FIM + xls.rotulo(9, 9, "série do gráfico") + xls.FIM)
        pasta = self.pasta_com(xls.numero(0, 0, 1.0), embutido, xls.numero(1, 1, 2.0))
        self.assertEqual(self.valores(pasta.arquivo()), [[1.0, None], [None, 2.0]])

    def test_sem_materializar_devolve_as_mesmas_contagens_e_nenhum_valor(self):
        relatorio, _ = xls.relatorio_paginado()
        duas = xls.Pasta()
        duas.aba("A").por(3, 4, "x")
        duas.aba("B").por(0, 0, 1).por(7, 2, "y")
        for pasta in (relatorio, duas, pasta_simples()):
            for opcoes in ({}, {"minimo": 0}):
                dados = bytes(pasta.arquivo(**opcoes))
                completo = self.ler(dados)
                contagem = self.ler(dados, materializar=False)
                self.assertIsNone(contagem["valores"])
                self.assertEqual({k: v for k, v in completo.items() if k != "valores"},
                                 {k: v for k, v in contagem.items() if k != "valores"})
                self.assertEqual(set(contagem), CHAVES)

    def test_sem_materializar_os_textos_nao_sao_decodificados(self):
        pasta = xls.Pasta()
        pasta.aba().por(0, 0, "texto um").por(0, 1, "texto dois").bruto(xls.rotulo(1, 0, "rótulo"))
        dados = bytes(pasta.arquivo())
        with mock.patch.object(etl_biff, "_decodificar", side_effect=AssertionError("decodificou")):
            self.assertEqual(self.ler(dados, materializar=False)["celulas"], 3)
            with self.assertRaises(LeituraRecusada):  # com a matriz, o texto é decodificado
                self.ler(dados)

    def test_celulas_fora_de_ordem_no_arquivo(self):
        pasta = self.pasta_com(xls.numero(5, 2, 52.0), xls.numero(0, 3, 3.0), xls.numero(5, 0, 50.0),
                               xls.numero(2, 1, 21.0), xls.numero(0, 0, 0.5))
        self.assertEqual(self.valores(pasta.arquivo()), [
            [0.5, None, None, 3.0], [None] * 4, [None, 21.0, None, None], [None] * 4, [None] * 4,
            [50.0, None, 52.0, None]])

    def test_a_matriz_e_retangular_e_as_linhas_sao_independentes(self):
        pasta = self.pasta_com(xls.numero(0, 0, 1.0), xls.numero(3, 5, 2.0))
        valores = self.valores(pasta.arquivo())
        self.assertEqual([len(linha) for linha in valores], [6, 6, 6, 6])
        valores[1][0] = "mexida"  # mudar uma linha em branco não muda a outra
        self.assertIsNone(valores[2][0])

    def test_aba_esparsa_nao_ocupa_a_area_durante_a_leitura(self):
        """A memória da leitura acompanha as células com valor: 4.096 células espalhadas por 65.521 linhas e
        256 colunas não custam a área (que passaria de 130 MB só de ponteiros). Só a matriz custa a área."""
        registros = [xls.numero(linha, 255 if linha % 32 else 0, 1.0) for linha in range(0, 65536, 16)]
        caminho = self.gravar(self.pasta_com(*registros).arquivo())
        tracemalloc.start()
        try:
            resultado = ler(caminho, materializar=False, **{**LIMITES, "max_colunas": 256})
            pico = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (65521, 256, 4096))
        self.assertLess(pico, 4 * 1024 * 1024)

    def test_planilha_maior_e_lida_inteira(self):
        pasta = xls.Pasta()
        aba = pasta.aba()
        for linha in range(4000):
            aba.por(linha, 0, f"PRODUTO FICTÍCIO {linha % 700:04d}").por(linha, 1, linha).por(linha, 2, linha + 0.25)
            aba.por(linha, 3, datetime(2026, 1, 1 + linha % 28)).bruto(xls.brancos(linha, 4, 10), xls.desenho())
        resultado = self.ler_do_disco(pasta.arquivo())
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (4000, 4, 16000))
        self.assertEqual(resultado["valores"][3999], ["PRODUTO FICTÍCIO 0499", 3999.0, 3999.25, datetime(2026, 1, 24)])
        self.assertEqual(len({linha[0] for linha in resultado["valores"]}), 700)


# =============================================================================
# 3. Textos compartilhados (SST e CONTINUE)
# =============================================================================
class TextosCompartilhadosTests(_BiffCase):
    TEXTOS = ["AÇÃO E REAÇÃO", "curto", "Preço € 12,50 – Ω", "", "PRODUTO FICTÍCIO DE NOME COMPRIDO 0,9L",
              "😀 par substituto 😀", "ç", "último texto da tabela"]

    def pasta_de_textos(self, textos):
        pasta = xls.Pasta()
        aba = pasta.aba()
        for linha, texto in enumerate(textos):
            aba.bruto(xls.texto_compartilhado(linha, 0, linha))
        return pasta

    def esperados(self, textos):
        valores = [[item.valor if isinstance(item, xls.Texto) else item] for item in textos]
        while valores and not valores[-1][0]:
            valores.pop()
        return [[valor or None for valor in linha] for linha in valores]

    def test_sst_partido_em_continue_em_todos_os_pontos(self):
        pasta = self.pasta_de_textos(self.TEXTOS)
        esperado = self.esperados(self.TEXTOS)
        for limite in range(12, 90):
            with self.subTest(limite=limite):
                sst = xls.tabela_de_textos(self.TEXTOS, limite=limite)
                self.assertGreater(sst.count(struct.pack("<H", xls.CONTINUE)), 0)
                self.assertEqual(self.valores(pasta.arquivo(sst=sst)), esperado)
                self.assertEqual(self.ler(pasta.arquivo(sst=sst, minimo=0), materializar=False)["celulas"],
                                 len(esperado) - 1)  # um dos textos é vazio

    def test_continuacao_troca_a_largura_dos_caracteres(self):
        """Depois do corte, o byte de opção do CONTINUE diz a nova largura: pode ser diferente da inicial."""
        textos = ["texto que cabe em um byte por caractere, do começo ao fim",
                  xls.Texto("começa largo e termina em um byte por caractere", largo=True),
                  "Ω no começo e resto simples"]
        pasta = self.pasta_de_textos(textos)
        for largo_na_continuacao in (True, False):
            for limite in (20, 27, 33):
                with self.subTest(largo=largo_na_continuacao, limite=limite):
                    sst = xls.tabela_de_textos(textos, limite=limite, largo_na_continuacao=largo_na_continuacao)
                    self.assertEqual(self.valores(pasta.arquivo(sst=sst)), self.esperados(textos))

    def test_texto_rico_e_fonetico_sao_pulados(self):
        textos = [xls.Texto("negrito e normal", trechos=3),
                  xls.Texto("com leitura fonética", fonetico=bytes(range(37))),
                  xls.Texto("Ω rico e fonético", trechos=2, fonetico=b"\x01" * 50), "simples depois de tudo"]
        pasta = self.pasta_de_textos(textos)
        for limite in (8224, 64, 31, 17):
            with self.subTest(limite=limite):
                sst = xls.tabela_de_textos(textos, limite=limite)
                self.assertEqual(self.valores(pasta.arquivo(sst=sst)), self.esperados(textos))

    def test_textos_longos_atravessam_registros_de_8224_bytes(self):
        textos = ["X" * 20_000, "Ω" * 9_000, ("ab" * 3_000) + ("€" * 3_000), "ç" * 8_216, "fim"]
        pasta = self.pasta_de_textos(textos)
        sst = xls.tabela_de_textos(textos)
        self.assertGreater(sst.count(struct.pack("<HH", xls.CONTINUE, 8224)), 3)
        dados = bytes(pasta.arquivo(sst=sst))
        self.assertEqual(self.valores(dados, max_caracteres=20_000), self.esperados(textos))
        self.recusa("field_chars", dados, max_caracteres=19_999)

    def test_par_substituto_partido_entre_dois_registros(self):
        textos = ["a😀b😀c😀d"]
        pasta = self.pasta_de_textos(textos)
        for limite in range(12, 30):
            with self.subTest(limite=limite):
                sst = xls.tabela_de_textos(textos, limite=limite)
                self.assertEqual(self.valores(pasta.arquivo(sst=sst)), [["a😀b😀c😀d"]])

    def test_substituto_sem_par_vira_caractere_de_substituicao(self):
        """O texto devolvido sempre pode ser gravado em UTF-8 (não sobra meio par substituto)."""
        sst = xls.registro(xls.SST, struct.pack("<II", 1, 1) + struct.pack("<HB", 3, 1)
                           + struct.pack("<3H", ord("a"), 0xD83D, ord("b")))
        valores = self.valores(self.pasta_de_textos(["x"]).arquivo(sst=sst))
        self.assertEqual(valores, [["a�b"]])
        valores[0][0].encode("utf-8")

    def test_textos_sem_uso_na_tabela_nao_atrapalham(self):
        textos = [f"texto fictício {indice}" for indice in range(3000)]
        pasta = xls.Pasta()
        pasta.aba().bruto(xls.texto_compartilhado(0, 0, 2999), xls.texto_compartilhado(0, 1, 0),
                          xls.texto_compartilhado(1, 0, 2999))
        resultado = self.ler(pasta.arquivo(sst=xls.tabela_de_textos(textos, limite=200)))
        self.assertEqual(resultado["valores"], [["texto fictício 2999", "texto fictício 0"],
                                                ["texto fictício 2999", None]])
        self.assertIs(resultado["valores"][0][0], resultado["valores"][1][0])  # o texto é decodificado uma vez

    def test_continue_de_outro_registro_nao_entra_na_tabela(self):
        """Só o CONTINUE que vem logo depois do SST continua a tabela."""
        sst = (xls.tabela_de_textos(["um", "dois"]) + xls.registro(xls.PREENCHIMENTO, bytes(8))
               + xls.registro(xls.CONTINUE, b"\x00" + b"lixo de outro registro"))
        pasta = self.pasta_de_textos(["um", "dois"])
        self.assertEqual(self.valores(pasta.arquivo(sst=sst)), [["um"], ["dois"]])

    def test_tabela_incoerente_e_recusada(self):
        pasta = self.pasta_de_textos(["um", "dois"])
        casos = {
            "mais textos declarados que bytes": xls.tabela_de_textos(["um", "dois"], quantidade=5000),
            "um texto a mais que os gravados": xls.tabela_de_textos(["um", "dois", "três"], quantidade=4),
            "texto cortado no fim": xls.registro(
                xls.SST, struct.pack("<II", 1, 1) + struct.pack("<HB", 50, 0) + b"curto"),
            "tabela sem cabeçalho": xls.registro(xls.SST, b"\x01\x00\x00"),
            "duas tabelas": xls.tabela_de_textos(["um", "dois"]) * 2,
            "caractere de dois bytes partido": (
                xls.registro(xls.SST, struct.pack("<II", 1, 1) + struct.pack("<HB", 2, 1) + b"a\x00b")
                + xls.registro(xls.CONTINUE, b"\x01\x00")),
            "cabeçalho de texto partido": (
                xls.registro(xls.SST, struct.pack("<II", 2, 2) + struct.pack("<HB", 1, 0) + b"a" + b"\x03\x00")
                + xls.registro(xls.CONTINUE, b"\x00abc")),
            "continuação vazia": (
                xls.registro(xls.SST, struct.pack("<II", 1, 1) + struct.pack("<HB", 4, 0) + b"ab")
                + xls.registro(xls.CONTINUE, b"")),
            "fonético maior que a tabela": xls.registro(
                xls.SST, struct.pack("<II", 2, 2) + struct.pack("<HBI", 1, 4, 0xFFFFFFF0) + b"a" + bytes(40)),
        }
        for nome, sst in casos.items():
            with self.subTest(caso=nome):
                self.recusa("invalid", pasta.arquivo(sst=sst))
                self.recusa("invalid", pasta.arquivo(sst=sst), materializar=False)


# =============================================================================
# 4. Contêiner OLE2
# =============================================================================
class ConteinerTests(_BiffCase):
    def test_fluxo_pequeno_e_lido_pelo_mini_fluxo(self):
        pasta = pasta_simples()
        fluxo = pasta.fluxo(minimo=0)
        self.assertLess(len(fluxo), 4096)
        arquivo = pasta.arquivo(minimo=0)
        self.assertNotIn("Workbook", [nome for nome, cadeia in arquivo.cadeias.items() if cadeia and nome != "raiz"])
        self.assertTrue(arquivo.cadeias["raiz"] and arquivo.cadeias["minifat"])
        self.assertEqual(self.ler_do_disco(arquivo)["valores"], VALORES_DA_PASTA_SIMPLES)
        for ordem in ("invertida", "embaralhada"):
            self.assertEqual(self.valores(pasta.arquivo(minimo=0, ordem=ordem)), VALORES_DA_PASTA_SIMPLES)

    def test_mini_fluxo_com_outros_fluxos_pequenos_antes(self):
        """O fluxo da pasta não começa no primeiro mini-setor nem o diretório cabe num setor só."""
        pasta = pasta_simples()
        outros = [(f"Resumo{indice}", bytes([indice]) * (70 + 64 * indice)) for indice in range(6)]
        arquivo = xls.conteiner([*outros[:3], ("Workbook", pasta.fluxo(minimo=0)), *outros[3:]], ordem="embaralhada")
        self.assertGreater(len(arquivo.cadeias["diretorio"]), 1)
        self.assertEqual(self.valores(arquivo), VALORES_DA_PASTA_SIMPLES)

    def test_fluxo_em_setores_comuns(self):
        arquivo = pasta_simples().arquivo()
        self.assertTrue(arquivo.cadeias["Workbook"])
        self.assertFalse(arquivo.cadeias["raiz"])
        self.assertEqual(self.ler_do_disco(arquivo)["valores"], VALORES_DA_PASTA_SIMPLES)

    def test_setores_de_4096_bytes(self):
        pasta = pasta_simples()
        for opcoes in ({}, {"minimo": 0}, {"minimo": 70_000, "ordem": "embaralhada"},
                       {"minimo": 0, "ordem": "invertida"}):
            with self.subTest(**opcoes):
                arquivo = pasta.arquivo(setor=4096, **opcoes)
                self.assertEqual(len(arquivo.dados) % 4096, 0)
                self.assertEqual(struct.unpack_from("<H", arquivo.dados, 0x1E)[0], 12)
                self.assertEqual(self.ler_do_disco(arquivo)["valores"], VALORES_DA_PASTA_SIMPLES)

    def test_setores_fora_de_ordem_no_arquivo(self):
        relatorio, esperado = xls.relatorio_paginado(paginas=4, itens_por_pagina=30)
        referencia = self.ler(relatorio.arquivo())
        self.assertEqual(referencia["celulas"], len(esperado))
        for ordem in ("invertida", "embaralhada"):
            with self.subTest(ordem=ordem):
                arquivo = relatorio.arquivo(ordem=ordem, minimo=150_000)
                self.assertNotEqual(arquivo.cadeias["Workbook"], sorted(arquivo.cadeias["Workbook"]))
                self.assertEqual(self.ler(arquivo), referencia)

    def test_setores_extras_da_difat_em_arquivo_pequeno(self):
        """A lista dos setores da FAT continua em setores da DIFAT encadeados."""
        pasta = pasta_simples()
        arquivo = pasta.arquivo(minimo=300_000, fat_no_cabecalho=1, por_setor_da_difat=2)
        self.assertGreaterEqual(len(arquivo.setores_da_difat), 2)
        self.assertEqual(self.valores(arquivo), VALORES_DA_PASTA_SIMPLES)
        self.assertEqual(self.valores(pasta.arquivo(minimo=300_000, fat_no_cabecalho=0, por_setor_da_difat=1,
                                                    ordem="embaralhada")), VALORES_DA_PASTA_SIMPLES)

    def test_setores_extras_da_difat_em_arquivo_de_oito_megabytes(self):
        """Com mais de 109 setores de FAT (7 MB em setores de 512 bytes) o cabeçalho não basta."""
        pasta = pasta_simples()
        arquivo = pasta.arquivo(minimo=7_300_000)
        self.assertGreater(len(arquivo.setores_da_fat), 109)
        self.assertEqual(len(arquivo.setores_da_difat), 1)
        chamadas = []
        resultado = self.ler_do_disco(arquivo, verificar=lambda: chamadas.append(1))
        self.assertEqual(resultado["valores"], VALORES_DA_PASTA_SIMPLES)
        self.assertGreater(len(chamadas), 7_300_000 // 65_536)  # o prazo é conferido a cada bloco de 64 KiB

    def test_cabecalho_sem_difat_com_inicio_zerado_ou_livre(self):
        """Sem setores extras o começo da DIFAT deveria ser "fim"; há gravadores que deixam zero ou "livre"."""
        for inicio in (0, xls.LIVRE, xls.FIM_DE_CADEIA):
            with self.subTest(inicio=inicio):
                arquivo = pasta_simples().arquivo().trocar(0x44, "<I", inicio)
                self.assertEqual(self.valores(arquivo), VALORES_DA_PASTA_SIMPLES)

    def test_ultimo_setor_incompleto(self):
        """O arquivo pode acabar logo depois do último byte do fluxo, sem completar o setor."""
        pasta = pasta_simples()
        fluxo = pasta.fluxo(minimo=5000)
        setores_do_fluxo = -(-len(fluxo) // 512)
        self.assertNotEqual(len(fluxo) % 512, 0)

        def fluxo_no_fim(total):
            return [(logico + total - setores_do_fluxo) % total for logico in range(total)]

        arquivo = xls.conteiner([("Workbook", fluxo)], ordem=fluxo_no_fim)
        sobra = 512 - len(fluxo) % 512
        self.assertEqual(bytes(arquivo.dados[-sobra:]), bytes(sobra))
        self.assertEqual(self.ler_do_disco(arquivo.dados[:-sobra])["valores"], VALORES_DA_PASTA_SIMPLES)
        self.recusa("invalid", arquivo.dados[:-sobra - 1], do_disco=True)  # um byte a menos já corta o fluxo

    def test_outros_fluxos_nao_sao_abertos(self):
        pasta = pasta_simples()
        outra = xls.Pasta()
        outra.aba().por(0, 0, "pasta embutida em um objeto")
        outros = [("\x05SummaryInformation", b"\xfe\xff" + bytes(5000)), ("Objeto/Workbook", outra.fluxo()),
                  ("_VBA_PROJECT_CUR/Modulo1", b"Sub Ficticio()\r\nEnd Sub\r\n" * 300), ("Ole10Native", bytes(6000))]
        arquivo = xls.conteiner([outros[1], outros[0], ("Workbook", pasta.fluxo()), *outros[2:]])
        self.assertLess(arquivo.entradas["Objeto/Workbook"], arquivo.entradas["Workbook"])
        self.assertEqual(self.valores(arquivo), VALORES_DA_PASTA_SIMPLES)
        # As cadeias dos outros fluxos podem estar quebradas (ciclo, setor fora do arquivo): ninguém as segue.
        for caminho, _ in outros:
            cadeia = arquivo.cadeias[caminho]
            arquivo.trocar(arquivo.posicao_na_fat(cadeia[0]), "<I", cadeia[0])
            arquivo.trocar(arquivo.posicao_na_fat(cadeia[1]), "<I", 0x00FFFFFF)
        self.assertEqual(self.valores(arquivo), VALORES_DA_PASTA_SIMPLES)

    def test_pasta_de_trabalho_so_dentro_de_outra_pasta_nao_serve(self):
        arquivo = xls.conteiner([("Objeto/Workbook", pasta_simples().fluxo()), ("Resumo", bytes(100))])
        mensagem = self.recusa("invalid", arquivo)
        self.assertIn("pasta de trabalho", mensagem)

    def test_nome_do_fluxo_sem_diferenciar_maiusculas(self):
        arquivo = xls.conteiner([("WORKBOOK", pasta_simples().fluxo())])
        self.assertEqual(self.valores(arquivo), VALORES_DA_PASTA_SIMPLES)

    def test_tamanho_do_fluxo_com_lixo_na_metade_alta(self):
        """Com setores de 512 bytes o tamanho tem 32 bits; os outros 32 podem vir com lixo."""
        arquivo = pasta_simples().arquivo()
        arquivo.trocar(arquivo.posicao_da_entrada("Workbook", 0x7C), "<I", 0xDEADBEEF)
        self.assertEqual(self.valores(arquivo), VALORES_DA_PASTA_SIMPLES)
        grande = pasta_simples().arquivo(setor=4096)
        grande.trocar(grande.posicao_da_entrada("Workbook", 0x7C), "<I", 1)  # aqui os 64 bits valem
        self.recusa("invalid", grande)


# =============================================================================
# 5. Limites
# =============================================================================
class LimitesTests(_BiffCase):
    def test_limite_de_abas(self):
        pasta = xls.Pasta()
        for indice in range(3):
            pasta.aba(f"Plan{indice}").por(0, 0, indice)
        pasta.aba("Gráfico", tipo=2)
        dados = bytes(pasta.arquivo())
        self.assertEqual(self.ler(dados, max_abas=3)["abas"], 3)
        self.assertIn("abas", self.recusa("sheets", dados, max_abas=2))
        self.recusa("sheets", dados, max_abas=2, materializar=False)

    def test_limite_de_linhas_conta_ate_a_ultima_celula_com_valor(self):
        pasta = self.pasta_com(xls.numero(0, 0, 1.0), xls.numero(9, 0, 2.0), xls.branco(500, 0), xls.erro(600, 0))
        dados = bytes(pasta.arquivo())
        self.assertEqual(self.ler(dados, max_linhas=10)["linhas"], 10)
        self.assertIn("linhas", self.recusa("rows", dados, max_linhas=9))
        self.recusa("rows", dados, max_linhas=9, materializar=False)

    def test_limite_de_colunas_conta_ate_a_ultima_celula_com_valor(self):
        pasta = self.pasta_com(xls.numero(0, 0, 1.0), xls.numero(3, 17, 2.0), xls.branco(0, 200), xls.erro(1, 100))
        dados = bytes(pasta.arquivo())
        self.assertEqual(self.ler(dados, max_colunas=18)["colunas"], 18)
        self.assertIn("colunas", self.recusa("columns", dados, max_colunas=17))
        self.recusa("columns", dados, max_colunas=17, materializar=False)

    def test_limite_de_celulas_conta_as_que_tem_valor_e_nao_a_area(self):
        pasta = xls.Pasta()
        aba = pasta.aba()
        for linha in range(0, 1000, 100):   # 10 valores espalhados numa área de 901 x 101
            aba.por(linha, linha // 10, "valor").bruto(xls.brancos(linha, 101, 50))
        dados = bytes(pasta.arquivo())
        resultado = self.ler(dados, max_celulas=10)
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (901, 91, 10))
        self.assertIn("células", self.recusa("cells", dados, max_celulas=9))
        self.recusa("cells", dados, max_celulas=9, materializar=False)

    def test_limite_de_celulas_e_por_aba(self):
        pasta = xls.Pasta()
        for nome in ("A", "B", "C"):
            aba = pasta.aba(nome)
            for linha in range(5):
                aba.por(linha, 0, linha)
        dados = bytes(pasta.arquivo())
        self.assertEqual(self.ler(dados, max_celulas=5)["celulas_no_arquivo"], 15)
        self.recusa("cells", dados, max_celulas=4)

    def test_limite_de_area_e_conferido_antes_de_montar_a_matriz(self):
        """Duas células distantes: 2 valores numa área de 60.001 x 101. A matriz custaria a área inteira;
        com `max_area`, a recusa vem antes de ela existir, com ou sem `materializar`."""
        dados = bytes(self.pasta_com(xls.numero(0, 0, 1.0), xls.numero(60_000, 100, 2.0)).arquivo())
        area = 60_001 * 101
        contagens = self.ler(dados, max_area=area, materializar=False)
        self.assertEqual((contagens["linhas"], contagens["colunas"], contagens["celulas"]), (60_001, 101, 2))
        with mock.patch.object(etl_biff._Celulas, "matriz") as matriz:
            for materializar in (True, False):
                with self.subTest(materializar=materializar):
                    self.assertIn("área", self.recusa("cells", dados, max_area=area - 1, materializar=materializar))
        matriz.assert_not_called()
        # No teto exato a leitura passa; sem `max_area` (o padrão), valem só os limites de linhas e de colunas.
        pequena = bytes(self.pasta_com(xls.numero(0, 0, 1.0), xls.numero(9, 9, 2.0)).arquivo())
        self.assertEqual(len(self.valores(pequena, max_area=100)), 10)
        self.assertEqual(len(self.valores(pequena)), 10)
        self.recusa("cells", pequena, max_area=99)

    def test_regravar_a_mesma_celula_nao_gasta_o_limite(self):
        pasta = self.pasta_com(*(xls.numero(0, 0, float(vez)) for vez in range(50)), xls.numero(0, 1, 1.0))
        self.assertEqual(self.ler(pasta.arquivo(), max_celulas=2)["valores"], [[49.0, 1.0]])

    def test_limite_de_caracteres_por_texto(self):
        onze = "ONZE LETRAS"
        casos = {
            "tabela de textos": xls.Pasta(),
            "rótulo": self.pasta_com(xls.rotulo(0, 0, onze)),
            "rótulo largo": self.pasta_com(xls.rotulo(0, 0, onze, largo=True)),
            "texto de fórmula": self.pasta_com(xls.formula(0, 0, onze)),
        }
        casos["tabela de textos"].aba().por(0, 0, onze)
        for nome, pasta in casos.items():
            with self.subTest(caso=nome):
                dados = bytes(pasta.arquivo())
                self.assertEqual(self.valores(dados, max_caracteres=11), [[onze]])
                self.assertIn("caracteres", self.recusa("field_chars", dados, max_caracteres=10))
                self.recusa("field_chars", dados, max_caracteres=10, materializar=False)

    def test_texto_acima_do_limite_e_recusado_mesmo_sem_celula(self):
        """O limite vale para a tabela de textos inteira, como no XLSX."""
        pasta = xls.Pasta()
        pasta.aba().bruto(xls.texto_compartilhado(0, 0, 0))
        dados = bytes(pasta.arquivo(sst=xls.tabela_de_textos(["curto", "texto comprido que nenhuma célula usa"])))
        self.assertEqual(self.valores(dados, max_caracteres=37), [["curto"]])
        self.recusa("field_chars", dados, max_caracteres=36)

    def test_os_limites_valem_durante_a_leitura(self):
        """Com o arquivo cortado DEPOIS do ponto em que o limite estoura, a recusa é a do limite: a
        leitura parou antes de chegar ao fim (que nem existe)."""
        celulas = b"".join(xls.numero(linha, coluna, 1.0) for linha in range(20) for coluna in range(5))
        texto_longo = xls.rotulo(0, 0, "texto acima do limite")
        casos = {"cells": (celulas, {"max_celulas": 30}), "rows": (celulas, {"max_linhas": 5}),
                 "columns": (celulas, {"max_colunas": 3}), "field_chars": (texto_longo, {"max_caracteres": 5})}
        for codigo, (registros, limite) in casos.items():
            with self.subTest(codigo=codigo):
                fluxo = self.pasta_com(registros).fluxo()
                sem_fim = fluxo[:-len(xls.FIM)] + xls.registro(xls.NUMBER, bytes(14))[:9]  # sem EOF e truncado
                arquivo = xls.conteiner([("Workbook", sem_fim)])
                self.recusa(codigo, arquivo, **limite)
                self.recusa("invalid", arquivo)

    def test_arquivo_sem_dados_e_vazio(self):
        so_formatacao = self.pasta_com(xls.branco(0, 0), xls.brancos(1, 0, 8), xls.erro(2, 0), xls.formula(3, 0, ""))
        sem_abas = xls.Pasta()
        so_grafico = xls.Pasta()
        so_grafico.aba("Gráfico", tipo=2).bruto(xls.numero(0, 0, 1.0))
        duas_vazias = xls.Pasta()
        duas_vazias.aba("A")
        duas_vazias.aba("B")
        for nome, pasta in (("só formatação", so_formatacao), ("sem abas", sem_abas), ("só gráfico", so_grafico),
                            ("duas vazias", duas_vazias)):
            with self.subTest(caso=nome):
                self.recusa("empty", pasta.arquivo())
                self.recusa("empty", pasta.arquivo(minimo=0), materializar=False)
        self.assertEqual(self.recusa("empty", b"", do_disco=True), "O arquivo está vazio.")


# =============================================================================
# 6. Recusas
# =============================================================================
class RecusasTests(_BiffCase):
    def test_arquivo_que_nao_e_ole2(self):
        casos = {
            "xlsx (zip)": b"PK\x03\x04" + bytes(600),
            "csv": "Produto;Quantidade;Valor\r\nLIXA FICTÍCIA;2;7,60\r\n".encode("cp1252") * 30,
            "html com extensão xls": b"<html><body><table><tr><td>Produto</td></tr></table></body></html>" * 20,
            "pdf": b"%PDF-1.7\n" + bytes(700),
            "um byte": b"\xd0",
            "assinatura e mais nada": xls.ASSINATURA,
            "assinatura em arquivo curto": xls.ASSINATURA + bytes(300),
            "zeros": bytes(2048),
        }
        for nome, dados in casos.items():
            with self.subTest(caso=nome):
                self.assertIn("não é o de uma planilha .xls", self.recusa("invalid", dados, do_disco=True))
                self.recusa("invalid", dados, materializar=False)

    def test_planilha_de_excel_antigo_gravada_sem_conteiner(self):
        biff2 = xls.registro(0x0009, struct.pack("<HH", 2, 0x10)) + xls.registro(0x0003, bytes(15)) + xls.FIM
        biff8_solto = pasta_simples().fluxo()
        for nome, dados in (("BIFF2", biff2), ("BIFF4", xls.registro(0x0409, bytes(6)) + xls.FIM),
                            ("BIFF8 sem contêiner", biff8_solto)):
            with self.subTest(caso=nome):
                self.assertIn(".xlsx", self.recusa("unsupported", dados))

    def test_excel_5_95(self):
        pasta = pasta_simples()
        no_fluxo_book = xls.conteiner([("Book", pasta.fluxo(versao=xls.BIFF5))])
        biff5_em_workbook = xls.conteiner([("Workbook", pasta.fluxo(versao=xls.BIFF5))])
        for nome, arquivo in (("fluxo Book", no_fluxo_book), ("BIFF5 no fluxo Workbook", biff5_em_workbook),
                              ("fluxo Book pequeno", xls.conteiner([("Book", pasta.fluxo(minimo=0))]))):
            with self.subTest(caso=nome):
                mensagem = self.recusa("unsupported", arquivo)
                self.assertIn("Excel 5.0/95", mensagem)
                self.assertIn("salve como .xlsx", mensagem)
        # O arquivo que traz os dois formatos (Excel 97 a 2003 e 5.0/95) é lido pelo fluxo novo.
        duplo = xls.conteiner([("Book", pasta.fluxo(versao=xls.BIFF5)), ("Workbook", pasta.fluxo())])
        self.assertEqual(self.valores(duplo), VALORES_DA_PASTA_SIMPLES)
        # BOF de Excel 4 dentro do contêiner.
        antigo = xls.conteiner([("Workbook", (xls.registro(0x0409, bytes(6)) + xls.FIM).ljust(4096, b"\x00"))])
        self.assertIn(".xlsx", self.recusa("unsupported", antigo))

    def test_arquivo_cifrado(self):
        pasta = pasta_simples()
        cifrado = pasta.arquivo(antes_de_tudo=xls.registro(xls.FILEPASS, bytes(54)))
        for materializar in (True, False):
            mensagem = self.recusa("unsupported", cifrado, do_disco=True, materializar=materializar)
            self.assertIn("senha", mensagem)
            self.assertIn(".xlsx", mensagem)
        # Pasta do Excel 2007 em diante gravada com senha: é um contêiner OLE2 sem o fluxo da pasta.
        pacote = xls.conteiner([("EncryptionInfo", bytes(300)), ("EncryptedPackage", bytes(9000))])
        self.assertIn("senha", self.recusa("unsupported", pacote))

    def test_arquivo_truncado_em_qualquer_ponto(self):
        relatorio, _ = xls.relatorio_paginado()
        for nome, arquivo in (("setores comuns", relatorio.arquivo()),
                              ("mini-fluxo", pasta_simples().arquivo(minimo=0)),
                              ("embaralhado", relatorio.arquivo(ordem="embaralhada")),
                              ("4096", pasta_simples().arquivo(setor=4096))):
            dados = bytes(arquivo)
            self.ler(dados)
            cortes = sorted({1, 7, 8, 100, 511, 512, 513, len(dados) // 3, len(dados) // 2, len(dados) - 600,
                             len(dados) - 513, len(dados) - 512}
                            | set(range(600, len(dados) - 600, max(1, len(dados) // 23))))
            for corte in cortes:
                with self.subTest(caso=nome, corte=corte):
                    self.recusa("invalid", dados[:corte])
            self.recusa("invalid", dados[:len(dados) // 2], do_disco=True)

    def test_fluxo_cortado_no_meio_de_um_registro(self):
        fluxo = pasta_simples().fluxo()
        for corte in (1, 2, 3, 5, 9, 17):
            with self.subTest(corte=corte):
                self.recusa("invalid", xls.conteiner([("Workbook", fluxo[:-corte])]))
        globais = fluxo[:fluxo.index(xls.bof(xls.PLANILHA))]
        self.recusa("invalid", xls.conteiner([("Workbook", globais[:-4].ljust(4096, b"\x00"))]))  # globais sem EOF

    def test_ciclo_na_fat(self):
        arquivo = pasta_simples().arquivo(minimo=20_000)
        cadeia = arquivo.cadeias["Workbook"]
        casos = {
            "o quinto setor volta ao primeiro": (cadeia[4], cadeia[0]),
            "um setor aponta para si mesmo": (cadeia[2], cadeia[2]),
            "o último volta ao começo": (cadeia[-2], cadeia[0]),
            "a cadeia do diretório em ciclo": (arquivo.cadeias["diretorio"][0], arquivo.cadeias["diretorio"][0]),
        }
        for nome, (setor, proximo) in casos.items():
            with self.subTest(caso=nome):
                corrompido = pasta_simples().arquivo(minimo=20_000)
                corrompido.trocar(corrompido.posicao_na_fat(setor), "<I", proximo)
                self.recusa("invalid", corrompido)
                self.recusa("invalid", corrompido, materializar=False)

    def test_ciclo_na_mini_fat(self):
        arquivo = pasta_simples().arquivo(minimo=0)
        posicao = arquivo.posicao_do_setor(arquivo.cadeias["minifat"][0])
        self.recusa("invalid", pasta_simples().arquivo(minimo=0).trocar(posicao + 4 * 3, "<I", 1))
        self.recusa("invalid", pasta_simples().arquivo(minimo=0).trocar(posicao + 4 * 2, "<I", 0x00FFFFFF))
        self.recusa("invalid", pasta_simples().arquivo(minimo=0).trocar(0x3C, "<I", xls.FIM_DE_CADEIA))  # sem mini-FAT

    def test_cadeias_que_saem_do_arquivo_ou_se_cruzam(self):
        base = pasta_simples().arquivo(minimo=20_000)
        cadeia = base.cadeias["Workbook"]
        diretorio = base.cadeias["diretorio"][0]
        casos = {
            "setor depois do fim do arquivo": (base.posicao_na_fat(cadeia[3]), 0x00FFFFFF),
            "cadeia que acaba antes do tamanho declarado": (base.posicao_na_fat(cadeia[3]), xls.FIM_DE_CADEIA),
            "cadeia que entra no diretório": (base.posicao_na_fat(cadeia[3]), diretorio),
            "cadeia que entra na FAT": (base.posicao_na_fat(cadeia[3]), base.setores_da_fat[0]),
            "setor livre no meio da cadeia": (base.posicao_na_fat(cadeia[3]), xls.LIVRE),
            "diretório fora do arquivo": (0x30, 0x00FFFFFF),
            "FAT fora do arquivo": (0x4C, 0x00FFFFFF),
            "sem FAT": (0x4C, xls.LIVRE),
            "FAT no mesmo setor do diretório": (0x4C, diretorio),
            "DIFAT em ciclo": None,
        }
        for nome, troca in casos.items():
            with self.subTest(caso=nome):
                if troca is None:
                    corrompido = pasta_simples().arquivo(minimo=300_000, fat_no_cabecalho=1, por_setor_da_difat=2)
                    primeiro = corrompido.setores_da_difat[0]
                    corrompido.trocar(corrompido.posicao_do_setor(primeiro) + 508, "<I", primeiro)
                else:
                    corrompido = pasta_simples().arquivo(minimo=20_000).trocar(troca[0], "<I", troca[1])
                self.recusa("invalid", corrompido)

    def test_cabecalho_do_conteiner_incoerente(self):
        casos = {"ordem dos bytes": (0x1C, "<H", 0xFEFF), "tamanho do setor": (0x1E, "<H", 10),
                 "setor gigante": (0x1E, "<H", 31), "tamanho do mini-setor": (0x20, "<H", 7),
                 "corte do mini-fluxo": (0x38, "<I", 8192)}
        for nome, (posicao, formato, valor) in casos.items():
            with self.subTest(caso=nome):
                self.recusa("invalid", pasta_simples().arquivo().trocar(posicao, formato, valor))

    def test_diretorio_incoerente(self):
        base = pasta_simples().arquivo()
        casos = {
            "a primeira entrada não é a raiz": (base.posicao_da_entrada("", 0x42), "<B", 2),
            "irmã que não existe": (base.posicao_da_entrada("Workbook", 0x48), "<I", 500),
            "irmã que volta para a própria entrada": (base.posicao_da_entrada("Workbook", 0x48), "<I", 1),
            "filha da raiz é a raiz": (base.posicao_da_entrada("", 0x4C), "<I", 0),
            "fluxo maior que o arquivo": (base.posicao_da_entrada("Workbook", 0x78), "<I", 50_000_000),
            "fluxo de tamanho zero": (base.posicao_da_entrada("Workbook", 0x78), "<I", 0),
            "a pasta de trabalho virou pasta": (base.posicao_da_entrada("Workbook", 0x42), "<B", 1),
            "nome trocado": (base.posicao_da_entrada("Workbook", 0), "<H", ord("X")),
        }
        for nome, (posicao, formato, valor) in casos.items():
            with self.subTest(caso=nome):
                self.recusa("invalid", pasta_simples().arquivo().trocar(posicao, formato, valor))
        duas = xls.conteiner([("Workbook", pasta_simples().fluxo()), ("WorkBook", pasta_simples().fluxo())])
        self.recusa("invalid", duas)
        sem_pasta = xls.conteiner([("WordDocument", bytes(5000)), ("Resumo", bytes(100))])
        self.assertIn("pasta de trabalho", self.recusa("invalid", sem_pasta))

    def test_registro_gigante(self):
        """Nenhum registro do BIFF8 passa de 8224 bytes de dados, estejam eles lá ou não."""
        for tamanho in (8225, 20_000, 0xFFFF):
            for onde in ("globais", "aba"):
                with self.subTest(tamanho=tamanho, onde=onde):
                    gigante = struct.pack("<HH", xls.PREENCHIMENTO, tamanho) + bytes(tamanho)
                    pasta = pasta_simples()
                    if onde == "aba":
                        pasta.abas[0].bruto(gigante)
                        arquivo = pasta.arquivo()
                    else:
                        arquivo = pasta.arquivo(antes_do_fim=gigante)
                    self.recusa("invalid", arquivo)
                    self.recusa("invalid", arquivo, materializar=False)
        no_limite = pasta_simples()
        no_limite.abas[0].bruto(xls.registro(xls.PREENCHIMENTO, bytes(8224)))
        self.assertEqual(self.valores(no_limite.arquivo()), VALORES_DA_PASTA_SIMPLES)

    def test_registro_mais_curto_que_o_seu_tipo_exige(self):
        casos = {
            "NUMBER": xls.registro(xls.NUMBER, bytes(13)), "RK": xls.registro(xls.RK, bytes(9)),
            "LABELSST": xls.registro(xls.LABELSST, bytes(9)), "BOOLERR": xls.registro(xls.BOOLERR, bytes(7)),
            "FORMULA": xls.registro(xls.FORMULA, bytes(13)), "LABEL": xls.registro(xls.LABEL, bytes(8)),
            "MULRK": xls.registro(xls.MULRK, bytes(11)),
            "LABEL com texto maior que o registro": xls.registro(
                xls.LABEL, bytes(6) + struct.pack("<HB", 30, 0) + b"curto"),
            "LABEL largo com byte ímpar": xls.registro(xls.LABEL, bytes(6) + struct.pack("<HB", 2, 1) + b"abc"),
            "MULRK com sobra": xls.registro(xls.MULRK, bytes(4) + bytes(6) + bytes(3)),
            "MULRK com a última coluna errada": xls.varios_rk(0, 0, [xls.rk_de_inteiro(1)] * 3, ultima=7),
        }
        for nome, dados in casos.items():
            with self.subTest(caso=nome):
                self.recusa("invalid", self.pasta_com(xls.numero(0, 0, 1.0), dados).arquivo())
        for nome, dados in (("XF", xls.registro(xls.XF, bytes(3))), ("FORMAT", xls.registro(xls.FORMAT, bytes(4))),
                            ("BOUNDSHEET", xls.registro(xls.BOUNDSHEET, bytes(5))),
                            ("DATEMODE", xls.registro(xls.DATEMODE, b"\x01")),
                            ("FORMAT com texto maior",
                             xls.registro(xls.FORMAT, struct.pack("<HHB", 200, 90, 0) + b"0.0")),
                            ("BOF dentro dos globais", xls.bof(xls.GLOBAIS))):
            with self.subTest(caso=nome):
                self.recusa("invalid", pasta_simples().arquivo(antes_do_fim=dados))

    def test_indice_de_texto_fora_da_tabela(self):
        for indice in (2, 3, 0xFFFFFFFF):
            with self.subTest(indice=indice):
                pasta = xls.Pasta()
                pasta.aba().por(0, 0, "um").por(0, 1, "dois").bruto(xls.texto_compartilhado(1, 0, indice))
                self.recusa("invalid", pasta.arquivo())
                self.recusa("invalid", pasta.arquivo(), materializar=False)
        sem_tabela = self.pasta_com(xls.texto_compartilhado(0, 0, 0))
        self.recusa("invalid", sem_tabela.arquivo(sst=b""))

    def test_formato_que_nao_existe_em_celula_numerica(self):
        """Sem o formato não dá para saber se o número é data: o arquivo é recusado."""
        for registro in (xls.numero(0, 0, 1.0, xf=5), xls.rk(0, 0, xls.rk_de_inteiro(1), xf=0xFFFF),
                         xls.varios_rk(0, 0, [xls.rk_de_inteiro(1)], xf=1), xls.formula(0, 0, 1.0, xf=9)):
            pasta = self.pasta_com(registro)
            self.recusa("invalid", pasta.arquivo())
            self.recusa("invalid", pasta.arquivo(), materializar=False)
        em_texto = xls.Pasta()  # o formato de uma célula de texto não é usado
        em_texto.aba().bruto(xls.rotulo(0, 0, "texto", xf=9), xls.booleano(0, 1, True, xf=9))
        self.assertEqual(self.valores(em_texto.arquivo()), [["texto", True]])

    def test_deslocamento_de_aba_fora_do_fluxo(self):
        pasta = xls.Pasta()
        pasta.aba("A").por(0, 0, 1)
        pasta.aba("B").por(0, 0, 2).por(0, 1, 3)
        fluxo = pasta.fluxo()
        primeiro = fluxo.index(xls.bof(xls.PLANILHA))
        segundo = fluxo.index(xls.bof(xls.PLANILHA), primeiro + 1)
        self.assertEqual(self.valores(xls.conteiner([("Workbook", pasta.fluxo(deslocamentos=[primeiro, segundo]))])),
                         [[2.0, 3.0]])
        casos = {"depois do fim do fluxo": [primeiro, len(fluxo) + 100],
                 "no fim exato do fluxo": [primeiro, len(fluxo)],
                 "enorme": [primeiro, 0xFFFFFFF0], "dentro dos globais": [20, segundo], "no começo": [0, segundo],
                 "no meio de um registro": [primeiro + 2, segundo],
                 "num registro que não é BOF": [primeiro + 20, segundo],
                 "as duas abas no mesmo lugar": [primeiro, primeiro], "dentro da aba anterior": [primeiro, segundo - 4]}
        for nome, deslocamentos in casos.items():
            with self.subTest(caso=nome):
                self.recusa("invalid", xls.conteiner([("Workbook", pasta.fluxo(deslocamentos=deslocamentos))]))
        # A ordem das abas no fluxo pode não ser a da lista: vale o índice da lista.
        trocadas = self.ler(xls.conteiner([("Workbook", pasta.fluxo(deslocamentos=[segundo, primeiro]))]))
        self.assertEqual((trocadas["aba"], trocadas["valores"]), (0, [[2.0, 3.0]]))

    def test_aba_que_nao_e_planilha_do_biff8(self):
        pasta = pasta_simples()
        fluxo = bytearray(pasta.fluxo())
        posicao = fluxo.index(xls.bof(xls.PLANILHA))
        for nome, (campo, valor) in {"a lista diz planilha e o subfluxo é gráfico": (6, xls.GRAFICO),
                                     "versão diferente da dos globais": (4, 0x0400)}.items():
            with self.subTest(caso=nome):
                trocado = bytearray(fluxo)
                struct.pack_into("<H", trocado, posicao + campo, valor)
                self.recusa("invalid", xls.conteiner([("Workbook", trocado)]))

    def test_coluna_fora_do_formato(self):
        for registro in (xls.numero(0, 256, 1.0), xls.rotulo(0, 300, "x"), xls.booleano(0, 0xFFFF, True),
                         xls.varios_rk(0, 254, [xls.rk_de_inteiro(1)] * 3)):
            self.recusa("invalid", self.pasta_com(registro).arquivo(), max_colunas=100_000)
        na_borda = self.pasta_com(xls.numero(65535, 255, 1.0))
        resultado = self.ler(na_borda.arquivo(), max_colunas=256, materializar=False)
        self.assertEqual((resultado["linhas"], resultado["colunas"]), (65536, 256))

    def test_subfluxo_sem_fim(self):
        pasta = pasta_simples()
        fluxo = pasta.fluxo()
        sem_fim_da_aba = fluxo[:-len(xls.FIM)].ljust(len(fluxo) + 64, b"\x00")
        self.recusa("invalid", xls.conteiner([("Workbook", sem_fim_da_aba)]))
        embutido_aberto = self.pasta_com(xls.numero(0, 0, 1.0), xls.bof(xls.GRAFICO), xls.numero(1, 0, 2.0))
        self.recusa("invalid", embutido_aberto.arquivo())

    def test_fluxo_que_nao_comeca_por_bof(self):
        fluxo = pasta_simples().fluxo()
        self.recusa("invalid", xls.conteiner([("Workbook", xls.registro(xls.PREENCHIMENTO, bytes(4)) + fluxo)]))
        self.recusa("invalid", xls.conteiner([("Workbook", bytes(5000))]))
        planilha_no_lugar_dos_globais = xls.bof(xls.PLANILHA) + fluxo[len(xls.bof(xls.GLOBAIS)):]
        self.recusa("invalid", xls.conteiner([("Workbook", planilha_no_lugar_dos_globais)]))

    def test_mensagens_fixas_sem_dado_do_arquivo(self):
        segredo = "SEGREDO-FICTICIO-0123456789"
        pasta = xls.Pasta()
        pasta.aba(segredo).por(0, 0, segredo).bruto(xls.texto_compartilhado(1, 0, 99))
        with self.assertRaises(LeituraRecusada) as contexto:
            ler(self.gravar(pasta.arquivo()), **LIMITES)
        erro = contexto.exception
        self.assertNotIn(segredo, erro.mensagem + repr(erro) + repr(erro.args))
        self.assertNotIn("planilha_", erro.mensagem)  # nem o nome do arquivo
        self.assertIsNone(erro.__cause__)
        self.assertTrue(erro.__suppress_context__ or erro.__context__ is None)
        mensagens = {valor for nome, valor in vars(etl_biff).items() if nome.startswith("_") and isinstance(valor, str)
                     and nome.isupper()}
        self.assertIn(erro.mensagem, mensagens)


# =============================================================================
# 7. Robustez
# =============================================================================
class _Estouro(Exception):
    """A exceção de prazo de quem chama a leitura."""


class RobustezTests(_BiffCase):
    def test_a_excecao_do_prazo_passa_adiante_como_veio(self):
        dados = bytes(xls.relatorio_paginado(paginas=5, itens_por_pagina=40)[0].arquivo())
        chamadas = []
        self.ler(dados, verificar=lambda: chamadas.append(1))
        self.assertGreaterEqual(len(chamadas), 4)  # nos globais, na tabela de textos, na aba e na matriz
        for tipo in (_Estouro, ValueError, KeyError, struct.error, LeituraRecusada):
            for em_qual in range(1, len(chamadas) + 1):
                with self.subTest(tipo=tipo.__name__, chamada=em_qual):
                    erro = tipo("invalid", "prazo") if tipo is LeituraRecusada else tipo("prazo excedido")
                    contador = iter(range(1, em_qual + 1))

                    def verificar(erro=erro, contador=contador, em_qual=em_qual):
                        if next(contador) == em_qual:
                            raise erro

                    with self.assertRaises(tipo) as contexto:
                        self.ler(dados, verificar=verificar)
                    self.assertIs(contexto.exception, erro)
        with self.assertRaises(_Estouro):  # e pelo disco: o arquivo é fechado mesmo assim
            caminho = self.gravar(dados)
            ler(caminho, verificar=mock.Mock(side_effect=_Estouro), **LIMITES)
        caminho.unlink()

    def test_o_prazo_e_conferido_tambem_sem_materializar(self):
        dados = bytes(pasta_simples().arquivo(minimo=400_000))
        chamadas = []
        self.ler(dados, verificar=lambda: chamadas.append(1), materializar=False)
        self.assertGreaterEqual(len(chamadas), 400_000 // 65_536)

    def test_memory_error_passa_adiante(self):
        dados = bytes(pasta_simples().arquivo())
        with mock.patch.object(etl_biff._Celulas, "guardar", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                self.ler(dados)

    def test_falha_inesperada_da_leitura_vira_recusa(self):
        dados = bytes(pasta_simples().arquivo())
        for falha in (RuntimeError("falha fictícia"), IndexError(3), struct.error("curto"), RecursionError()):
            with self.subTest(falha=type(falha).__name__):
                with mock.patch.object(etl_biff._Celulas, "guardar", side_effect=falha):
                    self.recusa("invalid", dados)

    def test_arquivo_ausente_levanta_oserror(self):
        with self.assertRaises(OSError):
            ler(self.diretorio / "nao_existe.xls", **LIMITES)

    def test_o_arquivo_e_fechado_e_nada_e_gravado(self):
        caminho = self.gravar(pasta_simples().arquivo())
        antes = caminho.read_bytes()
        ler(caminho, **LIMITES)
        ler(str(caminho), **LIMITES)
        with self.assertRaises(LeituraRecusada):
            ler(caminho, **{**LIMITES, "max_celulas": 1})
        self.assertEqual([item.name for item in self.diretorio.iterdir()], [caminho.name])
        self.assertEqual(caminho.read_bytes(), antes)
        caminho.unlink()  # no Windows, apagar falharia com o arquivo ainda aberto

    def test_centenas_de_arquivos_corrompidos_nunca_soltam_excecao_crua(self):
        relatorio, _ = xls.relatorio_paginado()
        bases = {"setores comuns": bytes(pasta_simples().arquivo()),
                 "mini-fluxo": bytes(pasta_simples().arquivo(minimo=0)),
                 "relatório embaralhado": bytes(relatorio.arquivo(ordem="embaralhada")),
                 "difat": bytes(pasta_simples().arquivo(minimo=40_000, fat_no_cabecalho=0, por_setor_da_difat=1))}
        desfechos = {"leu": 0, "recusou": 0}
        for nome, original in bases.items():
            sorteio = xls.Sorteio(20261110)
            for indice in range(240):
                dados = xls.mutar(original, sorteio, indice % len(xls.MODOS_DE_MUTACAO))
                contagens = []
                for materializar in (True, False):
                    try:
                        resultado = self.ler(dados, materializar=materializar)
                    except LeituraRecusada as erro:
                        self.assertIn(erro.codigo, CODIGOS)
                        contagens.append(erro.codigo)
                        desfechos["recusou"] += 1
                    except Exception as erro:  # noqa: BLE001 - é exatamente o que o teste procura
                        self.fail(f"{nome}, mutação {indice} ({xls.MODOS_DE_MUTACAO[indice % 6]}): "
                                  f"exceção crua {type(erro).__name__}")
                    else:
                        self.assertEqual(set(resultado), CHAVES)
                        if materializar:
                            self.conferir_tipos(resultado["valores"])
                            self.assertEqual((len(resultado["valores"]), len(resultado["valores"][0])),
                                             (resultado["linhas"], resultado["colunas"]))
                        contagens.append({k: v for k, v in resultado.items() if k != "valores"})
                        desfechos["leu"] += 1
                # Com ou sem a matriz, o desfecho é o mesmo: as mesmas contagens ou a mesma recusa.
                self.assertEqual(contagens[0], contagens[1], f"{nome}, mutação {indice}")
        self.assertGreater(desfechos["recusou"], 400)
        self.assertGreater(desfechos["leu"], 150)


# =============================================================================
# 8. O módulo
# =============================================================================
class ModuloTests(unittest.TestCase):
    def test_so_importa_a_biblioteca_padrao(self):
        arvore = ast.parse(inspect.getsource(etl_biff))
        importados = set()
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                importados.update(nome.name.split(".")[0] for nome in no.names)
            elif isinstance(no, ast.ImportFrom):
                importados.add(no.module.split(".")[0])
        self.assertEqual(importados - {"__future__"}, {"array", "collections", "datetime", "struct"})
        self.assertTrue(importados - {"__future__"} <= set(sys.stdlib_module_names))

    def test_nao_abre_para_escrita_nem_executa_nada(self):
        fonte = inspect.getsource(etl_biff)
        for no in ast.walk(ast.parse(fonte)):
            if isinstance(no, ast.Call) and isinstance(no.func, ast.Name):
                self.assertNotIn(no.func.id, ("eval", "exec", "compile", "__import__"))
                if no.func.id == "open":
                    self.assertEqual([argumento.value for argumento in no.args[1:]], ["rb"])
        self.assertEqual(fonte.count("open("), 1)

    def test_contrato_da_excecao(self):
        erro = LeituraRecusada("rows", "O XLS excede o limite de linhas permitido.")
        self.assertEqual((erro.codigo, erro.mensagem, str(erro)), ("rows", "O XLS excede o limite de linhas permitido.",
                                                                  "O XLS excede o limite de linhas permitido."))
        self.assertIsInstance(erro, Exception)

    def test_formatos_personalizados_de_data(self):
        datas = ["dd/mm/yy", "dd/MM/yyyy hh:mm", "DD/MM/YYYY", "d-mmm-yy", "mmm-yy", "h:mm AM/PM", "hh:mm:ss",
                 "[h]:mm:ss", "mm:ss.0", "yyyy-mm-dd hh:mm:ss.000", "[$-416]d \\de mmmm \\de yyyy", "m/d/yy h:mm",
                 '"Emitido em" dd/mm/yyyy', "dd/mm/yy;@", "yy"]
        numeros = ["General", "0", "0.0", "0.00", "#,##0.00", "#,##0.00;(#,##0.00)", "#,##0.00;-#,##0.00", "0%",
                   "0.00E+00", "000", "000 de ", '0 "dias"', '0.0 "h"', '#,##0.00 "R$"', "R$ #,##0.00", "@", "",
                   '"dd/mm/yy"', "\\d\\m\\y 0", "[Red]0.00", "[$R$-416] #,##0.00", "_-* #,##0.00_-", "# ?/?",
                   "0.0_d", "00000-000"]
        for formato in datas:
            self.assertTrue(etl_biff._e_formato_de_data(formato), formato)
        for formato in numeros:
            self.assertFalse(etl_biff._e_formato_de_data(formato), formato)

    def test_formatos_embutidos_de_data(self):
        embutidos = etl_biff._FORMATOS_EMBUTIDOS_DE_DATA
        for indice in (14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47):
            self.assertIn(indice, embutidos)
        for indice in (0, 1, 2, 3, 4, 9, 10, 11, 12, 13, 37, 38, 39, 40, 44, 48, 49, 164):
            self.assertNotIn(indice, embutidos)


class FormatosNoArquivoTests(_BiffCase):
    def test_o_formato_do_arquivo_decide_se_o_numero_e_data(self):
        pasta = xls.Pasta()
        aba = pasta.aba()
        serie = xls.serie(datetime(2026, 11, 9))
        formatos = [(14, True), (22, True), (xls.FORMATO_DE_DATA, True), ("mm:ss.0", True), (0, False), (2, False),
                    (4, False), (xls.FORMATO_DE_DINHEIRO, False), (xls.FORMATO_DE_PAGINA, False), (49, False)]
        for coluna, (formato, _) in enumerate(formatos):
            aba.bruto(xls.numero(0, coluna, serie, pasta.xf(formato)))
        valores = self.valores(pasta.arquivo())
        self.assertEqual(valores[0], [datetime(2026, 11, 9) if e_data else serie for _, e_data in formatos])

    def test_registro_format_vale_mais_que_a_tabela_embutida(self):
        """Um arquivo pode redefinir um índice embutido: vale o texto que ele traz."""
        pasta = xls.Pasta()
        pasta.aba().bruto(xls.numero(0, 0, 46335.0, pasta.xf(14)), xls.numero(0, 1, 46335.0, pasta.xf(2)))
        redefinidos = (xls.registro(xls.FORMAT, struct.pack("<H", 14) + xls.texto("0.00"))
                       + xls.registro(xls.FORMAT, struct.pack("<H", 2) + xls.texto("dd/mm/yyyy")))
        self.assertEqual(self.valores(pasta.arquivo()), [[datetime(2026, 11, 9), 46335.0]])
        self.assertEqual(self.valores(pasta.arquivo(antes_do_fim=redefinidos)), [[46335.0, datetime(2026, 11, 9)]])


if __name__ == "__main__":
    unittest.main()
