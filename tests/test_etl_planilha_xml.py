"""Leitor da Planilha XML 2003 do Excel (etl_planilha_xml): contrato, tipos, posições, limites e recusas.

Não há amostra real do formato: os arquivos são montados aqui e em tests/planilha_xml_support.py, pelos
elementos e atributos da especificação, com dados inventados. São nove grupos:

  1. TiposDeDadoTests: o que cada ss:Type vira, e o que não é valor (Error, Data vazio, fórmula, comentário).
  2. PosicoesTests: ss:Index em Row e Cell, mesclas, faixas de linhas e a matriz retangular.
  3. AbasTests: a escolha da aba e as contagens.
  4. CodificacoesTests: UTF-8, UTF-16 e as de um byte; o que fica de fora.
  5. LimitesTests: cada limite do contrato, aplicado durante a leitura, e os orçamentos de estrutura.
  6. RecusasTests: DOCTYPE, entidades, raiz errada, XML truncado, instrução de processamento, número, data.
  7. ContratoTests: as chaves do resultado, `verificar`, `materializar`, MemoryError e a biblioteca padrão.
  8. RelatorioDoErpTests: o relatório fictício completo, conferido célula a célula, em várias gravações.
  9. RobustezTests: arquivo cortado ou corrompido em qualquer ponto nunca solta exceção crua.
"""

import ast
from datetime import datetime
import io
from pathlib import Path
import pickle
import random
import sys
import tempfile
import unittest
from unittest import mock
from xml.parsers import expat

import etl_planilha_xml as leitor
from tests import planilha_xml_support as apoio
from tests.planilha_xml_support import aba, celula, linha, livro, tabela

try:
    from tests import leiautes_erp_support as leiautes
except ImportError:  # os leiautes do ERP nasceram na mesma auditoria e podem não estar nesta cópia
    leiautes = None

LIMITES = {"max_abas": 8, "max_linhas": 10_000, "max_colunas": 64, "max_celulas": 100_000, "max_caracteres": 4_000}
CODIGOS = {"invalid", "unsupported", "sheets", "rows", "columns", "cells", "field_chars", "empty"}
MENSAGENS = frozenset(valor for nome, valor in vars(leitor).items() if nome.startswith("_M_"))
CHAVES = {"abas", "aba", "linhas", "colunas", "celulas", "celulas_no_arquivo", "valores"}
ESPACO = apoio.ESPACO
RICO = 'xmlns="http://www.w3.org/TR/REC-html40"'
TEXTO = '<Data ss:Type="String">{}</Data>'


class _Caso(unittest.TestCase):
    """Quase todo teste lê de um arquivo em memória: o leitor roda inteiro, só o open() dele devolve os bytes
    do teste (criar centenas de arquivos é a parte lenta neste Windows). Os testes que tratam do arquivo em
    si (caminho, blocos, nada gravado) usam o disco, num diretório temporário."""

    @property
    def pasta(self):
        if not hasattr(self, "_pasta"):
            pasta = tempfile.TemporaryDirectory(prefix="nexo-planilha-xml-")
            self.addCleanup(pasta.cleanup)
            self._pasta = Path(pasta.name)
        return self._pasta

    def arquivo(self, conteudo, codificacao="utf-8"):
        """Grava o conteúdo (texto ou bytes) num arquivo novo do diretório temporário e devolve o caminho."""
        numero = len(list(self.pasta.iterdir())) + 1
        return apoio.gravar(self.pasta / f"planilha_{numero}.xml", conteudo, codificacao)

    def ler(self, conteudo, codificacao="utf-8", **opcoes):
        dados = conteudo if isinstance(conteudo, bytes) else conteudo.encode(codificacao)
        with mock.patch.object(leitor, "open", lambda _caminho, _modo: io.BytesIO(dados), create=True):
            return leitor.ler("em-memoria.xml", **{**LIMITES, **opcoes})

    def ler_do_disco(self, conteudo, codificacao="utf-8", **opcoes):
        return leitor.ler(self.arquivo(conteudo, codificacao), **{**LIMITES, **opcoes})

    def valores(self, conteudo, codificacao="utf-8", **opcoes):
        return self.ler(conteudo, codificacao, **opcoes)["valores"]

    def recusa(self, codigo, conteudo, mensagem=None, codificacao="utf-8", **opcoes):
        with self.assertRaises(leitor.LeituraRecusada) as contexto:
            self.ler(conteudo, codificacao, **opcoes)
        erro = contexto.exception
        self.assertEqual(erro.codigo, codigo, erro.mensagem)
        self.assertIn(erro.mensagem, MENSAGENS)  # mensagem fixa: nada do arquivo entra nela
        self.assertEqual(str(erro), erro.mensagem)
        if mensagem is not None:
            self.assertEqual(erro.mensagem, mensagem)
        return erro.mensagem

    def unica(self, celula_xml, **opcoes):
        """O valor da única célula de uma planilha de uma linha."""
        resultado = self.ler(tabela(linha(celula_xml)), **opcoes)
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (1, 1, 1))
        return resultado["valores"][0][0]


# =============================================================================
# 1. Tipos de dado
# =============================================================================
class TiposDeDadoTests(_Caso):
    def test_texto_chega_como_veio(self):
        for texto in ("Lixa ficticia", "0700019", "12.125", "1.460.035,77", "  com espaços nas pontas  ", " ",
                      "Ação é “assim” – 0,9L €", "=1+1", "TRUE", "2026-11-09"):
            with self.subTest(texto=texto):
                valor = self.unica(celula(texto))
                self.assertEqual(valor, texto)
                self.assertIs(type(valor), str)

    def test_texto_com_entidades_padrao_e_quebras_de_linha(self):
        bruto = ('<Cell><Data ss:Type="String">A &amp; B &lt;c&gt; &quot;d&quot; &apos;e&apos; &#231;&#xE3;o'
                 '&#10;segunda linha\nterceira<![CDATA[ <d> & ]]></Data></Cell>')
        self.assertEqual(self.unica(bruto), 'A & B <c> "d" \'e\' ção\nsegunda linha\nterceira <d> & ')
        # A referência &#13; fica; o fim de linha do Windows escrito no arquivo vira \n, como em todo XML.
        self.assertEqual(self.unica('<Cell><Data ss:Type="String">x&#13;&#10;y\r\nz</Data></Cell>'), "x\r\ny\nz")

    def test_numero_inteiro_vira_int_e_os_demais_float(self):
        casos = [("12", 12, int), ("-4", -4, int), ("+7", 7, int), ("007", 7, int), ("0", 0, int),
                 ("14408.63", 14408.63, float), ("12.0", 12.0, float), ("5.", 5.0, float), (".5", 0.5, float),
                 ("-0.25", -0.25, float), ("1E3", 1000.0, float), ("1.5e-3", 0.0015, float), ("2E+2", 200.0, float),
                 (" 19.431 ", 19.431, float), ("\n 8 \n", 8, int), ("3.8081", 3.8081, float),
                 ("12345678901234567890", 12345678901234567890, int)]
        for texto, esperado, tipo in casos:
            with self.subTest(texto=texto):
                valor = self.unica(celula(texto, tipo="Number"))
                self.assertEqual(valor, esperado)
                self.assertIs(type(valor), tipo)

    def test_data_e_data_com_hora_viram_datetime_sem_fuso(self):
        casos = {"2026-11-09T17:03:38.000": datetime(2026, 11, 9, 17, 3, 38),
                 "2026-11-09T00:00:00.000": datetime(2026, 11, 9),
                 "2026-11-09": datetime(2026, 11, 9),
                 "2026-11-09T17:03": datetime(2026, 11, 9, 17, 3),
                 "2026-11-09T17:03:38": datetime(2026, 11, 9, 17, 3, 38),
                 "2026-11-09T17:03:38.5": datetime(2026, 11, 9, 17, 3, 38, 500000),
                 "2026-11-09T17:03:38.123456789": datetime(2026, 11, 9, 17, 3, 38, 123456),
                 "1899-12-31T08:30:00.000": datetime(1899, 12, 31, 8, 30),  # só a hora, como o Excel grava
                 " 2024-02-29T23:59:59.999 ": datetime(2024, 2, 29, 23, 59, 59, 999000)}
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                valor = self.unica(celula(texto, tipo="DateTime"))
                self.assertEqual(valor, esperado)
                self.assertIs(type(valor), datetime)
                self.assertIsNone(valor.tzinfo)

    def test_logico_vira_bool(self):
        for texto, esperado in (("1", True), ("0", False), ("true", True), ("false", False), ("TRUE", True),
                                ("False", False), (" 1 ", True)):
            with self.subTest(texto=texto):
                self.assertIs(self.unica(celula(texto, tipo="Boolean")), esperado)

    def test_zero_e_falso_sao_valores(self):
        resultado = self.ler(tabela(linha(celula(0), celula(False), celula(0.0), celula("0", tipo="Number"))))
        self.assertEqual(resultado["celulas"], 4)
        registro = resultado["valores"][0]
        self.assertEqual([type(valor) for valor in registro], [int, bool, float, int])
        self.assertEqual(registro, [0, False, 0.0, 0])

    def test_erro_vira_vazio_e_nao_conta(self):
        resultado = self.ler(tabela(linha(celula("a"), celula("#DIV/0!", tipo="Error"), celula("c")),
                                    linha(celula("#N/A", tipo="Error"), celula("<B>#REF!</B>", tipo="Error"))))
        self.assertEqual(resultado["valores"], [["a", None, "c"]])
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (1, 3, 2))

    def test_data_vazio_e_celula_so_com_formatacao_nao_contam(self):
        vazias = ['<Cell><Data ss:Type="String"></Data></Cell>', '<Cell><Data ss:Type="String"/></Cell>',
                  '<Cell><Data ss:Type="Number"/></Cell>', '<Cell><Data ss:Type="Number">  </Data></Cell>',
                  '<Cell><Data ss:Type="DateTime"></Data></Cell>', '<Cell><Data ss:Type="Boolean">\n</Data></Cell>',
                  "<Cell/>", '<Cell ss:StyleID="s62"/>', '<Cell ss:StyleID="s62"></Cell>']
        resultado = self.ler(tabela(linha(celula("a"), *vazias, celula("z"))))
        self.assertEqual(resultado["valores"], [["a"] + [None] * len(vazias) + ["z"]])
        self.assertEqual(resultado["celulas"], 2)

    def test_formula_e_ignorada_e_vale_o_valor_guardado(self):
        resultado = self.ler(tabela(linha(
            celula(5, formula="=1+1"),                      # o arquivo diz 5: a fórmula nunca é avaliada
            celula(formula="=SUM(R[-3]C:R[-1]C)"),          # fórmula sem valor guardado: célula vazia
            celula("texto", formula='=HYPERLINK("http://127.0.0.1:9/x";"texto")'),
            celula("=cmd|' /C calc'!A0"))))                 # texto que parece fórmula chega como texto
        self.assertEqual(resultado["valores"], [[5, None, "texto", "=cmd|' /C calc'!A0"]])
        self.assertEqual(resultado["celulas"], 3)

    def test_texto_rico_junta_o_texto(self):
        bruto = (f'<Cell><ss:Data ss:Type="String" {RICO}><B>Total</B> do <I>dia</I>: '
                 '<Font html:Color="#FF0000">9,90</Font><Sub>1</Sub> &amp; <U><S>fim</S></U></ss:Data></Cell>')
        self.assertEqual(self.unica(bruto), "Total do dia: 9,901 & fim")
        # Número com marcação em volta continua número.
        numero = f'<Cell><ss:Data ss:Type="Number" {RICO}><B>12</B>.5</ss:Data></Cell>'
        self.assertEqual(self.unica(numero), 12.5)

    def test_comentario_vinculo_e_anexos_da_celula_nao_sao_valor(self):
        comentario = f'<Comment ss:Author="Autor Ficticio"><ss:Data {RICO}><B>nota:</B> conferir</ss:Data></Comment>'
        resultado = self.ler(tabela(linha(
            f'<Cell><Data ss:Type="Number">7</Data>{comentario}</Cell>',
            f"<Cell>{comentario}</Cell>",                                   # só o comentário: célula vazia
            f'<Cell>{comentario}<Data ss:Type="String">depois</Data></Cell>',
            celula("Site", vinculo="http://127.0.0.1:9/nada"),
            '<Cell><Data ss:Type="String">x</Data><PhoneticText xmlns="urn:schemas-microsoft-com:office:excel">'
            'fonetica</PhoneticText><NamedCell ss:Name="Print_Titles"/></Cell>')))
        self.assertEqual(resultado["valores"], [[7, None, "depois", "Site", "x"]])
        self.assertEqual(resultado["celulas"], 4)


# =============================================================================
# 2. Posições: índices, mesclas e a matriz
# =============================================================================
class PosicoesTests(_Caso):
    def test_linhas_e_celulas_com_indice_pulado(self):
        resultado = self.ler(tabela(
            linha(celula("a"), celula("c", indice=3), indice=2),
            linha(celula("e", indice=5)),                       # linha 3, pela ordem
            linha(celula("x", indice=2), celula("y"), indice=6)))
        self.assertEqual(resultado["valores"], [
            [None, None, None, None, None],
            ["a", None, "c", None, None],
            [None, None, None, None, "e"],
            [None, None, None, None, None],
            [None, None, None, None, None],
            [None, "x", "y", None, None]])
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (6, 5, 5))

    def test_linha_em_branco_ocupa_a_posicao(self):
        texto = tabela(linha(), linha(celula("a")), linha(estilo="s62"), linha(celula(estilo="s62")),
                       linha(celula("b")))
        self.assertEqual(self.valores(texto), [[None], ["a"], [None], [None], ["b"]])

    def test_mescla_horizontal_desloca_a_celula_seguinte(self):
        # Pela especificação, a célula sem índice depois de uma mescla fica na primeira coluna depois dela.
        self.assertEqual(self.valores(tabela(linha(celula("A", atravessa=2), celula("D"),
                                                   celula("F", indice=6, atravessa=1), celula("H")))),
                         [["A", None, None, "D", None, "F", None, "H"]])

    def test_mescla_vertical_deixa_vazias_as_celulas_cobertas(self):
        self.assertEqual(self.valores(tabela(
            linha(celula("grupo", desce=2), celula("b1")),
            linha(celula("b2", indice=2)),
            linha(celula("b3", indice=2)),
            linha(celula("a4"), celula("b4")))),
            [["grupo", "b1"], [None, "b2"], [None, "b3"], ["a4", "b4"]])

    def test_mescla_em_bloco(self):
        self.assertEqual(self.valores(tabela(
            linha(celula("bloco", atravessa=1, desce=1), celula("c1")),
            linha(celula("c2", indice=3)),
            linha(celula("a3"), celula("b3"), celula("c3")))),
            [["bloco", None, "c1"], [None, None, "c2"], ["a3", "b3", "c3"]])

    def test_valor_dentro_da_area_de_uma_mescla_e_recusado(self):
        casos = {
            "ao lado, com índice dentro da mescla": tabela(linha(celula("A", atravessa=2), celula("B", indice=2))),
            "ao lado, depois de célula vazia coberta": tabela(linha(celula("A", atravessa=3), celula(indice=2),
                                                                    celula("C"))),
            "ao lado, depois de célula vazia coberta que também mescla": tabela(linha(
                celula("A", atravessa=5), celula(indice=2, atravessa=1), celula("D"))),
            "embaixo, na primeira célula da linha": tabela(linha(celula("grupo", desce=1), celula("b1")),
                                                           linha(celula("a2"))),
            "embaixo, na segunda coluna do bloco": tabela(linha(celula("bloco", atravessa=1, desce=1)),
                                                          linha(celula("b2", indice=2))),
            "duas linhas abaixo": tabela(linha(celula("grupo", desce=2)), linha(), linha(celula("a3"))),
        }
        for rotulo, texto in casos.items():
            with self.subTest(caso=rotulo):
                self.recusa("invalid", texto, leitor._M_MESCLA)
        # Depois do fim da mescla a coluna volta a aceitar valor.
        self.assertEqual(self.valores(tabela(linha(celula("grupo", desce=1)), linha(), linha(celula("a3")))),
                         [["grupo"], [None], ["a3"]])

    def test_celula_vazia_dentro_de_mescla_e_tolerada(self):
        self.assertEqual(self.valores(tabela(
            linha(celula("A", atravessa=2, desce=1), celula(indice=2, estilo="s62"), celula("D", indice=4)),
            linha(celula(estilo="s62"), celula("", tipo="String"), celula("D2", indice=4)))),
            [["A", None, None, "D"], [None, None, None, "D2"]])

    def test_indice_invalido_repetido_ou_fora_de_ordem_e_recusado(self):
        casos = {
            "coluna repetida": tabela(linha(celula("a", indice=2), celula("b", indice=2))),
            "coluna para trás": tabela(linha(celula("a", indice=3), celula("b", indice=1))),
            "coluna igual à implícita anterior": tabela(linha(celula("a"), celula("b", indice=1))),
            "coluna zero": tabela(linha(celula("a", indice=0))),
            "linha repetida": tabela(linha(celula("a"), indice=3), linha(celula("b"), indice=3)),
            "linha para trás": tabela(linha(celula("a"), indice=3), linha(celula("b"), indice=2)),
            "linha zero": tabela(linha(celula("a"), indice=0)),
        }
        for texto in ("x", "-1", "+2", "1.0", "", " 2", "1234567890", "٣", "2e0"):
            casos[f"coluna {texto!r}"] = tabela(linha(celula("a", indice=texto)))
            casos[f"linha {texto!r}"] = tabela(linha(celula("a"), indice=texto))
            casos[f"mescla ao lado {texto!r}"] = tabela(linha(celula("a", atravessa=texto)))
            casos[f"mescla para baixo {texto!r}"] = tabela(linha(celula("a", desce=texto)))
            casos[f"faixa {texto!r}"] = tabela(linha(faixa=texto), linha(celula("a"), indice=99))
        for rotulo, texto in casos.items():
            with self.subTest(caso=rotulo):
                self.recusa("invalid", texto, leitor._M_INDICES)

    def test_faixa_de_linhas_vazias(self):
        resultado = self.ler(tabela(
            linha(celula("a")),
            linha(indice=2, faixa=2),          # linhas 2, 3 e 4: só formatação
            linha(celula("b"), indice=5),      # depois de uma faixa o Excel grava o índice
            linha(faixa=0),                    # faixa de zero linhas é uma linha vazia comum
            linha(celula("c"))))
        self.assertEqual(resultado["valores"], [["a"], [None], [None], [None], ["b"], [None], ["c"]])
        casos = {"índice dentro da faixa": tabela(linha(indice=2, faixa=2), linha(celula("b"), indice=4)),
                 "linha sem índice depois da faixa": tabela(linha(faixa=2), linha(celula("b"))),
                 "linha vazia sem índice depois da faixa": tabela(linha(faixa=2), linha(),
                                                                  linha(celula("b"), indice=9)),
                 "célula com valor em linha com faixa": tabela(linha(celula("x"), faixa=1)),
                 "célula vazia em linha com faixa": tabela(linha(celula(estilo="s62"), faixa=1))}
        for rotulo, texto in casos.items():
            with self.subTest(caso=rotulo):
                self.recusa("invalid", texto, leitor._M_INDICES)

    def test_atributo_de_posicao_sem_o_espaco_de_nomes_e_recusado(self):
        # Ignorar o atributo deslocaria a célula em silêncio; lê-lo seria adivinhar o que o Excel não lê.
        for bruto in ('<Row><Cell Index="3"><Data ss:Type="String">a</Data></Cell></Row>',
                      '<Row Index="2"><Cell><Data ss:Type="String">a</Data></Cell></Row>',
                      '<Row><Cell MergeAcross="1"><Data ss:Type="String">a</Data></Cell></Row>',
                      '<Row><Cell MergeDown="1" ss:Index="2"><Data ss:Type="String">a</Data></Cell></Row>',
                      '<Row Span="1" ss:Index="2"/><Row ss:Index="9"><Cell><Data ss:Type="String">a</Data></Cell>'
                      "</Row>"):
            with self.subTest(xml=bruto):
                self.recusa("invalid", tabela(bruto), leitor._M_ATRIBUTO_SOLTO)

    def test_matriz_retangular_aparada_no_fim_e_sem_linhas_compartilhadas(self):
        resultado = self.ler(tabela(
            linha(),
            linha(celula("a", indice=2), celula(indice=9, estilo="s62")),   # branco formatado na coluna 9
            linha(celula("b", indice=3)),
            linha(celula(estilo="s62")),
            linha(indice=50, estilo="s62")))                                # linha formatada bem abaixo
        valores = resultado["valores"]
        self.assertEqual(valores, [[None, None, None], [None, "a", None], [None, None, "b"]])
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (3, 3, 2))
        self.assertEqual({len(registro) for registro in valores}, {resultado["colunas"]})
        valores[0][0] = "mudou"
        self.assertEqual(valores[1:], [[None, "a", None], [None, None, "b"]])

    def test_prefixo_e_o_de_menos_vale_o_espaco_de_nomes(self):
        texto = (f'<?xml version="1.0"?><p:Workbook xmlns:p="{ESPACO}"><p:Worksheet p:Name="a"><p:Table>'
                 '<p:Row p:Index="2"><p:Cell p:Index="2"><p:Data p:Type="Number">5</p:Data></p:Cell></p:Row>'
                 "</p:Table></p:Worksheet></p:Workbook>")
        self.assertEqual(self.valores(texto), [[None, None], [None, 5]])
        # Sem declaração XML e sem a instrução do Excel também é a mesma planilha.
        self.assertEqual(self.valores(tabela(linha(celula(5)), declaracao="", instrucao="")), [[5]])

    def test_linha_e_coluna_ocultas_sao_lidas(self):
        texto = livro(' <Worksheet ss:Name="a"><Table><Column ss:Hidden="1"/><Row ss:Hidden="1">'
                      '<Cell><Data ss:Type="Number">9</Data></Cell></Row></Table></Worksheet>')
        self.assertEqual(self.valores(texto), [[9]])


# =============================================================================
# 3. Abas
# =============================================================================
class AbasTests(_Caso):
    def test_escolhe_a_aba_de_mais_celulas_com_valor(self):
        texto = livro(aba(linha(celula("capa")), nome="Capa"),
                      aba(linha(celula("a"), celula(1)), linha(celula("b"), celula(2)), nome="Dados"),
                      aba(nome="Vazia"))
        resultado = self.ler(texto)
        self.assertEqual(set(resultado), CHAVES)
        self.assertEqual({chave: resultado[chave] for chave in CHAVES - {"valores"}},
                         {"abas": 3, "aba": 1, "linhas": 2, "colunas": 2, "celulas": 4, "celulas_no_arquivo": 5})
        self.assertEqual(resultado["valores"], [["a", 1], ["b", 2]])

    def test_no_empate_fica_a_primeira(self):
        texto = livro(aba(linha(celula("p1"), celula("p2")), nome="Primeira"),
                      aba(linha(celula("s1")), linha(celula("s2")), nome="Segunda"))
        resultado = self.ler(texto)
        self.assertEqual((resultado["aba"], resultado["valores"]), (0, [["p1", "p2"]]))

    def test_area_grande_com_pouco_valor_nao_ganha(self):
        grande = aba(linha(celula("longe", indice=60), indice=900), linha(*[celula(estilo="s62")] * 30), nome="Area")
        texto = livro(grande, aba(linha(celula("a"), celula("b")), nome="Valor"))
        resultado = self.ler(texto)
        self.assertEqual((resultado["aba"], resultado["celulas"], resultado["celulas_no_arquivo"]), (1, 2, 3))

    def test_aba_sem_tabela_e_abas_depois_da_escolhida(self):
        texto = livro(' <Worksheet ss:Name="SemTabela"/>', aba(linha(celula(1), celula(2), celula(3)), nome="Dados"),
                      aba(linha(celula("x")), nome="Depois"))
        resultado = self.ler(texto)
        self.assertEqual((resultado["abas"], resultado["aba"], resultado["valores"]), (3, 1, [[1, 2, 3]]))

    def test_planilha_sem_celula_com_valor_e_vazia(self):
        casos = {"sem aba": livro(), "aba sem linha": livro(aba()),
                 "aba sem tabela": livro(' <Worksheet ss:Name="a"/>'),
                 "só formatação": tabela(linha(celula(estilo="s62"), celula(estilo="s62")), linha(estilo="s62")),
                 "só erro e vazio": tabela(linha(celula("#N/A", tipo="Error"), celula("", tipo="String"))),
                 "só fórmula sem valor": tabela(linha(celula(formula="=1+1")))}
        for rotulo, texto in casos.items():
            with self.subTest(caso=rotulo):
                self.recusa("empty", texto, leitor._M_SEM_VALOR)


# =============================================================================
# 4. Codificações
# =============================================================================
class CodificacoesTests(_Caso):
    UNICODE = "Ação é “assim” – 0,9L €"

    @staticmethod
    def documento(texto, codificacao=None):
        declaracao = f'<?xml version="1.0" encoding="{codificacao}"?>' if codificacao else ""
        return tabela(linha(celula(texto), celula(1.5)), declaracao=declaracao)

    def test_utf8_com_e_sem_declaracao_e_com_bom(self):
        for rotulo, codificacao, bom in (("sem declaração", None, b""), ("declarada", "UTF-8", b""),
                                         ("minúscula e com BOM", "utf-8", b"\xef\xbb\xbf"),
                                         ("só o BOM", None, b"\xef\xbb\xbf")):
            with self.subTest(caso=rotulo):
                dados = bom + self.documento(self.UNICODE, codificacao).encode("utf-8")
                self.assertEqual(self.valores(dados), [[self.UNICODE, 1.5]])

    def test_utf16_nas_duas_ordens_com_e_sem_bom(self):
        for rotulo, declarada, codec, bom in (("LE com BOM", "UTF-16", "utf-16-le", b"\xff\xfe"),
                                              ("BE com BOM", "UTF-16", "utf-16-be", b"\xfe\xff"),
                                              ("LE sem BOM", "UTF-16LE", "utf-16-le", b""),
                                              ("BE sem BOM", "UTF-16BE", "utf-16-be", b""),
                                              ("BOM sem declaração", None, "utf-16-le", b"\xff\xfe")):
            with self.subTest(caso=rotulo):
                dados = bom + self.documento(self.UNICODE, declarada).encode(codec)
                self.assertEqual(self.valores(dados), [[self.UNICODE, 1.5]])

    def test_codificacoes_de_um_byte(self):
        for declarada, codec, texto in (("ISO-8859-1", "latin-1", "Ação: maçã, pêssego e avó"),
                                        ("iso-8859-1", "latin-1", "Ação: maçã, pêssego e avó"),
                                        ("windows-1252", "cp1252", "Ação é “assim” – 0,9L €"),
                                        ("Windows-1252", "cp1252", "Preço… até 10‰"),
                                        ("ISO-8859-15", "iso-8859-15", "Preço em € da maçã"),
                                        ("US-ASCII", "ascii", "Acao sem acento")):
            with self.subTest(declarada=declarada):
                self.assertEqual(self.valores(self.documento(texto, declarada).encode(codec)), [[texto, 1.5]])
        # Em ASCII o acento vem por referência de caractere.
        bruto = '<?xml version="1.0" encoding="us-ascii"?>' + tabela(
            linha('<Cell><Data ss:Type="String">A&#231;&#227;o</Data></Cell>'), declaracao="")
        self.assertEqual(self.valores(bruto.encode("ascii")), [["Ação"]])

    def test_codificacao_fora_da_lista_e_recusada_com_orientacao(self):
        for nome in ("utf8", "UTF-7", "UTF-32", "shift_jis", "gb2312", "ibm850", "cp1252", "latin1", "mbcs", "rot13",
                     "hex", "zlib", "unicode_escape", "nao-existe"):
            with self.subTest(codificacao=nome):
                mensagem = self.recusa("unsupported", self.documento("Acao", nome).encode("ascii"),
                                       leitor._M_CODIFICACAO)
                self.assertIn(".xlsx", mensagem)
        # O UTF-32 é reconhecido pelo BOM, em qualquer tamanho de bloco de leitura.
        utf32 = self.documento("Acao").encode("utf-32")  # com BOM
        utf32_be = b"\x00\x00\xfe\xff" + self.documento("Acao").encode("utf-32-be")
        for bloco in (leitor._BLOCO, 1, 3):
            with self.subTest(utf32=bloco), mock.patch.object(leitor, "_BLOCO", bloco):
                self.recusa("unsupported", utf32, leitor._M_CODIFICACAO)
                self.recusa("unsupported", utf32_be, leitor._M_CODIFICACAO)

    def test_bom_de_utf8_com_outra_codificacao_declarada_e_recusado(self):
        # O expat seguiria a declaração e leria o UTF-8 byte a byte: "Ação" viraria "AÃ§Ã£o" em silêncio.
        for declarada in ("ISO-8859-1", "ISO-8859-15", "windows-1252", "US-ASCII", "UTF-16"):
            dados = b"\xef\xbb\xbf" + self.documento("Ação", declarada).encode("utf-8")
            for bloco in (leitor._BLOCO, 1):
                with self.subTest(declarada=declarada, bloco=bloco), mock.patch.object(leitor, "_BLOCO", bloco):
                    self.recusa("invalid", dados, leitor._M_CODIFICACAO_INCOERENTE)
        # Sem o BOM a declaração é a única palavra do arquivo: o texto de um byte é lido como declarado.
        em_latin1 = self.documento("Ação", "ISO-8859-1").encode("latin-1")
        self.assertEqual(self.valores(em_latin1), [["Ação", 1.5]])

    def test_arquivo_menor_que_o_bom(self):
        for bloco in (leitor._BLOCO, 1):
            with mock.patch.object(leitor, "_BLOCO", bloco):
                for dados in (b"<", b"<a", b"\xef\xbb\xbf", b"\xff\xfe", b"\x00"):
                    with self.subTest(dados=dados, bloco=bloco):
                        self.recusa("invalid", dados, leitor._M_MALFORMADO)
                for dados in (b"<a>", b"<a/>"):  # a raiz já apareceu, e não é a da planilha
                    with self.subTest(dados=dados, bloco=bloco):
                        self.recusa("unsupported", dados, leitor._M_RAIZ_OUTRA)

    def test_a_lista_e_conferida_antes_de_o_python_procurar_o_codec(self):
        modulo = "encodings.mac_iceland"
        if modulo in sys.modules:
            self.skipTest("o codec de ensaio já estava carregado neste processo")
        self.recusa("unsupported", self.documento("Acao", "mac_iceland").encode("ascii"), leitor._M_CODIFICACAO)
        self.assertNotIn(modulo, sys.modules)

    def test_conteudo_que_nao_segue_a_codificacao_declarada_e_recusado(self):
        em_cp1252 = self.documento("Ação", "UTF-8").encode("cp1252")
        self.recusa("invalid", em_cp1252, leitor._M_MALFORMADO)
        em_utf16 = self.documento("Ação", "UTF-8").encode("utf-16")
        self.recusa("invalid", em_utf16, leitor._M_CODIFICACAO_INCOERENTE)
        em_utf8 = self.documento("Ação", "UTF-16").encode("utf-8")
        self.recusa("invalid", em_utf8, leitor._M_CODIFICACAO_INCOERENTE)
        # Byte sem caractere no windows-1252.
        sem_caractere = self.documento("A", "windows-1252").encode("ascii").replace(b">A<", b">A\x81<")
        self.recusa("invalid", sem_caractere, leitor._M_MALFORMADO)


# =============================================================================
# 5. Limites
# =============================================================================
class LimitesTests(_Caso):
    def test_limite_de_abas(self):
        tres = livro(*(aba(linha(celula(numero)), nome=f"A{numero}") for numero in range(3)))
        self.assertEqual(self.ler(tres, max_abas=3)["abas"], 3)
        self.recusa("sheets", tres, leitor._M_ABAS, max_abas=2)
        # Aba vazia também é aba.
        self.recusa("sheets", livro(aba(linha(celula(1))), aba(), aba()), max_abas=2)

    def test_limite_de_linhas_conta_so_ate_a_ultima_celula_com_valor(self):
        texto = tabela(linha(celula("a")), linha(celula("b"), indice=10))
        self.assertEqual(self.ler(texto, max_linhas=10)["linhas"], 10)
        self.recusa("rows", texto, leitor._M_LINHAS, max_linhas=9)
        formatadas = tabela(linha(celula("a")), linha(celula(estilo="s62"), indice=500),
                            linha(indice=9000, estilo="s62"), linha(celula("", tipo="String"), indice=9001))
        self.assertEqual(self.ler(formatadas, max_linhas=1)["linhas"], 1)

    def test_limite_de_colunas_conta_so_ate_a_ultima_celula_com_valor(self):
        texto = tabela(linha(celula("a"), celula("b", indice=10)))
        self.assertEqual(self.ler(texto, max_colunas=10)["colunas"], 10)
        self.recusa("columns", texto, leitor._M_COLUNAS, max_colunas=9)
        formatadas = tabela(linha(celula("a", atravessa=500), celula(estilo="s62", indice=800)),
                            linha(celula(indice=900, estilo="s62", desce=3)))
        self.assertEqual(self.ler(formatadas, max_colunas=1)["colunas"], 1)

    def test_limite_de_celulas_e_das_que_tem_valor_e_por_aba(self):
        seis = tabela(linha(*(celula(numero) for numero in range(6))))
        self.assertEqual(self.ler(seis, max_celulas=6)["celulas"], 6)
        self.recusa("cells", seis, leitor._M_CELULAS, max_celulas=5)
        # A área não conta: 5 valores numa área de 33 x 33, entre 200 células só com formatação.
        espalhado = tabela(*(linha(celula(numero, indice=8 * numero + 1), *[celula(estilo="s62")] * 40,
                                   indice=8 * numero + 1) for numero in range(5)))
        resultado = self.ler(espalhado, max_celulas=5)
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (33, 33, 5))
        # O limite é de cada aba, e não do arquivo.
        duas = livro(aba(linha(*(celula(numero) for numero in range(5))), nome="A"),
                     aba(linha(*(celula(numero) for numero in range(5))), nome="B"))
        self.assertEqual(self.ler(duas, max_celulas=5)["celulas_no_arquivo"], 10)
        # Vale para toda aba, também para a que não seria a escolhida.
        self.recusa("cells", livro(aba(linha(celula(1), celula(2)), nome="A"),
                                   aba(linha(celula(1), celula(2), celula(3)), nome="B")), max_celulas=2)

    def test_limite_de_area_e_conferido_antes_de_montar_a_matriz(self):
        # Duas células distantes: 2 valores numa área de 9.000 x 60. A matriz custaria a área inteira.
        longe = tabela(linha(celula("a")), linha(celula("b", indice=60), indice=9_000))
        contagens = self.ler(longe, max_area=540_000, materializar=False)
        self.assertEqual((contagens["linhas"], contagens["colunas"], contagens["celulas"]), (9_000, 60, 2))
        with mock.patch.object(leitor, "_matriz") as matriz:
            for materializar in (True, False):
                with self.subTest(materializar=materializar):
                    self.recusa("cells", longe, leitor._M_AREA, max_area=539_999, materializar=materializar)
        matriz.assert_not_called()
        # No teto exato a leitura passa; sem `max_area` (o padrão), valem só os limites de linhas e de colunas.
        pequena = tabela(linha(celula("a")), linha(celula("b", indice=10), indice=10))
        self.assertEqual(len(self.valores(pequena, max_area=100)), 10)
        self.assertEqual(len(self.valores(pequena)), 10)
        self.recusa("cells", pequena, leitor._M_AREA, max_area=99)

    def test_limite_de_caracteres_por_celula(self):
        dez = tabela(linha(celula("x" * 10)))
        self.assertEqual(self.valores(dez, max_caracteres=10), [["x" * 10]])
        self.recusa("field_chars", dez, leitor._M_CARACTERES, max_caracteres=9)
        # Conta caracteres, e não bytes.
        self.assertEqual(self.valores(tabela(linha(celula("ç" * 10))), max_caracteres=10), [["ç" * 10]])
        # O texto que chega em pedaços (entidades, texto rico) soma.
        pedacos = tabela(linha(TEXTO.format("a&amp;" * 6).join(("<Cell>", "</Cell>"))))
        self.assertEqual(self.valores(pedacos, max_caracteres=12), [["a&" * 6]])
        self.recusa("field_chars", pedacos, max_caracteres=11)
        rico = tabela(linha(f'<Cell><ss:Data ss:Type="String" {RICO}><B>xxxxx</B>yyyyy<I>z</I></ss:Data></Cell>'))
        self.recusa("field_chars", rico, max_caracteres=10)
        # Vale para qualquer tipo, e para um texto que atravessa vários blocos de leitura.
        self.recusa("field_chars", tabela(linha(celula("1" * 50, tipo="Number"))), max_caracteres=20)
        self.recusa("field_chars", tabela(linha(celula("y" * 300_000))), max_caracteres=1_000)

    def test_orcamento_de_elementos_por_aba(self):
        # 400 linhas de 50 células só com formatação: 20.400 elementos, acima de 12 x 1 + 16.384.
        formatadas = [linha(*[celula(estilo="s62")] * 50) for _ in range(400)]
        self.recusa("cells", tabela(*formatadas, linha(celula("a"))), leitor._M_NOS, max_celulas=1)
        self.assertEqual(self.ler(tabela(*formatadas, linha(celula("a"))), max_celulas=1_000)["celulas"], 1)
        # O orçamento é de cada aba: duas abas com a metade passam.
        metade = formatadas[:200]
        duas = livro(aba(*metade, linha(celula("a")), nome="A"), aba(*metade, nome="B"))
        self.assertEqual(self.ler(duas, max_celulas=1)["celulas"], 1)
        # O que fica fora das abas tem o mesmo orçamento, contado à parte do de cada aba.
        def estilos(quantos):
            return "<Styles>" + '<Style ss:ID="s1"><Font/></Style>' * quantos + "</Styles>"
        self.recusa("cells", livro(aba(linha(celula("a"))), antes=estilos(9_000)), leitor._M_NOS, max_celulas=1)
        self.recusa("cells", livro(aba(linha(celula("a"))), estilos(9_000)), leitor._M_NOS, max_celulas=1)
        com_valor = aba(*metade, linha(celula("a")))
        self.assertEqual(self.ler(livro(com_valor, estilos(4_000)), max_celulas=1)["celulas"], 1)
        self.assertEqual(self.ler(livro(com_valor, antes=estilos(4_000)), max_celulas=1)["celulas"], 1)
        self.recusa("cells", livro(aba(linha(celula("a"))), estilos(4_500), antes=estilos(4_000)), max_celulas=1)

    def test_mescla_para_baixo_entra_no_orcamento(self):
        # 300 células vazias mescladas por 64 colunas e 2 linhas: 300 x 64 colunas cobertas.
        mescladas = [linha(celula(atravessa=63, desce=1), indice=2 * numero + 1) for numero in range(300)]
        texto = tabela(*mescladas, linha(celula("a"), indice=700))
        self.recusa("cells", texto, leitor._M_NOS, max_celulas=1)
        self.assertEqual(self.ler(texto, max_celulas=1_000)["linhas"], 700)

    def test_profundidade_maxima(self):
        def aninhado(niveis):
            return livro(aba(linha(celula("a"))), antes="<x>" * niveis + "</x>" * niveis)
        self.assertEqual(self.valores(aninhado(63)), [["a"]])     # Workbook e mais 63: 64 níveis
        self.recusa("invalid", aninhado(64), leitor._M_PROFUNDIDADE)
        rico = tabela(linha(f'<Cell><ss:Data ss:Type="String" {RICO}>' + "<B>" * 70 + "x" + "</B>" * 70
                            + "</ss:Data></Cell>"))
        self.recusa("invalid", rico, leitor._M_PROFUNDIDADE)

    def test_endereco_de_espaco_de_nomes_tem_tamanho_maximo(self):
        def com_endereco(tamanho):
            abertura = apoio.ABRE_LIVRO[:-1] + f' xmlns:z="{"u" * tamanho}">'
            return livro(aba(linha(celula("a"))), abertura=abertura)
        self.assertEqual(self.valores(com_endereco(256)), [["a"]])
        self.recusa("invalid", com_endereco(257), leitor._M_ENDERECO)
        # Na raiz de outro documento, o endereço enorme é recusado antes de qualquer elemento.
        self.recusa("invalid", f'<r xmlns="{"u" * 100_000}">' + "<a/>" * 20_000 + "</r>", leitor._M_ENDERECO)

    def test_limites_valem_durante_a_leitura_e_nao_no_fim(self):
        # Depois do ponto em que o limite estoura o arquivo é lixo: a recusa é a do limite.
        lixo = "<<< isto não é XML"
        tres = livro(*(aba(linha(celula(numero)), nome=f"A{numero}") for numero in range(3)))
        corte = tres.index("<Worksheet", tres.index("A1")) + len('<Worksheet ss:Name="A2">')
        self.recusa("sheets", tres[:corte] + lixo, max_abas=2)
        seis = tabela(linha(*(celula(numero) for numero in range(6))))
        corte = seis.index("</Row>")
        self.recusa("cells", seis[:corte] + lixo, leitor._M_CELULAS, max_celulas=5)
        self.recusa("invalid", seis[:corte] + lixo, leitor._M_MALFORMADO, max_celulas=6)
        longe = tabela(linha(celula("a", indice=10), indice=10))
        self.recusa("columns", longe[:longe.index("</Cell>")] + lixo, max_colunas=9)
        self.recusa("rows", longe[:longe.index("</Cell>")] + lixo, max_linhas=9)
        comprido = longe[:longe.index("</Data>")].replace(">a", ">" + "a" * 50_000)
        self.recusa("field_chars", comprido + lixo, leitor._M_CARACTERES, max_caracteres=40)


# =============================================================================
# 6. Recusas
# =============================================================================
class RecusasTests(_Caso):
    CORPO = (f'<Workbook xmlns="{ESPACO}" xmlns:ss="{ESPACO}"><Worksheet ss:Name="a"><Table><Row><Cell>'
             '<Data ss:Type="String">{}</Data></Cell></Row></Table></Worksheet></Workbook>')

    def test_doctype_entidade_e_notacao_sao_recusados(self):
        canario = self.pasta / "canario.txt"
        canario.write_text("SEGREDO FICTICIO", encoding="utf-8")
        casos = {
            "DOCTYPE simples": ("<!DOCTYPE Workbook>", "a"),
            "DOCTYPE de HTML": ("<!DOCTYPE html>", "a"),
            "DTD externa": ('<!DOCTYPE Workbook SYSTEM "http://127.0.0.1:9/x.dtd">', "a"),
            "DTD pública": ('<!DOCTYPE Workbook PUBLIC "-//X//DTD X//EN" "http://127.0.0.1:9/x.dtd">', "a"),
            "entidade": ('<!DOCTYPE Workbook [<!ENTITY nome "valor">]>', "&nome;"),
            "entidade externa": (f'<!DOCTYPE Workbook [<!ENTITY externa SYSTEM "{canario.as_uri()}">]>', "&externa;"),
            "entidade de parâmetro": ('<!DOCTYPE Workbook [<!ENTITY % p SYSTEM "http://127.0.0.1:9/p.dtd"> %p;]>',
                                      "a"),
            "notação": ('<!DOCTYPE Workbook [<!NOTATION n SYSTEM "x">]>', "a"),
            "atributo padrão": ('<!DOCTYPE Workbook [<!ATTLIST Cell extra CDATA "x">]>', "a"),
        }
        for rotulo, (doctype, conteudo) in casos.items():
            with self.subTest(caso=rotulo):
                mensagem = self.recusa("invalid", '<?xml version="1.0"?>' + doctype + self.CORPO.format(conteudo),
                                       leitor._M_PROIBIDO)
                self.assertNotIn("SEGREDO", mensagem)
                self.assertIn(".xlsx", mensagem)

    def test_bomba_de_entidades_e_recusada_no_doctype(self):
        niveis = "".join(f'<!ENTITY e{n} "{("&e%d;" % (n - 1)) * 10}">' for n in range(1, 10))
        bomba = ('<?xml version="1.0"?><!DOCTYPE Workbook [<!ENTITY e0 "aaaaaaaaaa">' + niveis + "]>"
                 + self.CORPO.format("&e9;"))
        # A mensagem é a do DOCTYPE: a leitura parou antes de qualquer entidade existir.
        self.recusa("invalid", bomba, leitor._M_PROIBIDO)
        self.recusa("invalid", bomba, leitor._M_PROIBIDO, codificacao="utf-16")

    def test_o_expat_para_no_ponto_em_que_o_leitor_recusa(self):
        """O que sustenta as recusas acima: quando um tratador levanta, o expat não processa o resto do
        bloco. Se uma versão do Python mudar isso, este teste avisa."""
        interno = leitor._Leitor(8, 100, 100, 100, 100, True)
        analisador = interno._analisador
        self.assertIsNone(analisador.intern)  # o expat não acumula os nomes que o arquivo trouxer
        dados = ('<?xml version="1.0"?><!DOCTYPE Workbook [<!ENTITY e "valor">]>' + self.CORPO.format("&e;")).encode()
        with self.assertRaises(leitor.LeituraRecusada):
            interno.alimentar(dados, True)
        self.assertEqual(analisador.ErrorCode, expat.errors.codes[expat.errors.XML_ERROR_ABORTED])
        self.assertLess(analisador.CurrentByteIndex, dados.index(b"<!ENTITY"))

    def test_cada_camada_da_protecao_recusa_sozinha(self):
        """A recusa do DOCTYPE vem primeiro e esconde as outras. Sem ela (e sem as que viriam antes), cada
        tratador seguinte recusa por conta própria: declaração de entidade, notação, entidade sem análise,
        entidade externa e entidade que o expat pulou."""
        notacao = '<!NOTATION n SYSTEM "x">'
        casos = {
            "declaração de entidade": (["StartDoctypeDeclHandler"], '[<!ENTITY e "valor">]', "&e;"),
            "notação": (["StartDoctypeDeclHandler"], f"[{notacao}]", "a"),
            "entidade sem análise": (["StartDoctypeDeclHandler", "EntityDeclHandler", "NotationDeclHandler"],
                                     f'[{notacao}<!ENTITY imagem SYSTEM "x.gif" NDATA n>]', "a"),
            "entidade externa": (["StartDoctypeDeclHandler", "EntityDeclHandler"],
                                 '[<!ENTITY externa SYSTEM "http://127.0.0.1:9/x">]', "&externa;"),
            "entidade pulada": (["StartDoctypeDeclHandler"], 'SYSTEM "http://127.0.0.1:9/x.dtd"', "&desconhecida;"),
        }

        def interno(sem_tratadores, subconjunto, conteudo):
            estado = leitor._Leitor(8, 100, 100, 100, 100, True)
            for nome in sem_tratadores:
                setattr(estado._analisador, nome, None)
            dados = f'<?xml version="1.0"?><!DOCTYPE Workbook {subconjunto}>' + self.CORPO.format(conteudo)
            estado.alimentar(dados.encode(), True)
            return estado

        for rotulo, (sem_tratadores, subconjunto, conteudo) in casos.items():
            with self.subTest(camada=rotulo):
                with self.assertRaises(leitor.LeituraRecusada) as contexto:
                    interno(sem_tratadores, subconjunto, conteudo)
                erro = contexto.exception
                self.assertEqual((erro.codigo, erro.mensagem), ("invalid", leitor._M_PROIBIDO))
        # Contraprova: sem tratador nenhum o expat expande a entidade. São os tratadores que a barram.
        todos = ["StartDoctypeDeclHandler", "EntityDeclHandler", "UnparsedEntityDeclHandler", "NotationDeclHandler",
                 "ExternalEntityRefHandler", "SkippedEntityHandler"]
        self.assertEqual(interno(todos, '[<!ENTITY e "valor">]', "&e;").resultado(None)["valores"], [["valor"]])

    def test_entidade_nao_declarada_e_referencia_de_caractere_invalida(self):
        for conteudo in ("&nada;", "&#0;", "&#1;", "&#xD800;", "&#xFFFF;", "&amp", "a & b", "a < b"):
            with self.subTest(conteudo=conteudo):
                self.recusa("invalid", self.CORPO.format(conteudo), leitor._M_MALFORMADO)

    def test_raiz_que_nao_e_a_da_planilha_e_recusada_com_orientacao(self):
        principal = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        corpo = '<Worksheet><Table><Row><Cell><Data ss:Type="String">a</Data></Cell></Row></Table></Worksheet>'
        casos = {
            "HTML": ("<html><body><table><tr><td>a</td></tr></table></body></html>", leitor._M_RAIZ_HTML),
            "HTML em maiúsculas": ("<HTML><BODY></BODY></HTML>", leitor._M_RAIZ_HTML),
            "XHTML": ('<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>', leitor._M_RAIZ_HTML),
            "NF-e": ('<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00"><NFe><infNFe Id="NFe0"/>'
                     "</NFe></nfeProc>", leitor._M_RAIZ_FISCAL),
            "CT-e": ('<cteProc xmlns="http://www.portalfiscal.inf.br/cte"/>', leitor._M_RAIZ_FISCAL),
            "CF-e": ("<CFe><infCFe/></CFe>", leitor._M_RAIZ_FISCAL),
            "SVG": ('<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>', leitor._M_RAIZ_SVG),
            "pasta de um XLSX": (f'<workbook xmlns="{principal}"><sheets/></workbook>', leitor._M_RAIZ_XLSX),
            "aba de um XLSX": (f'<worksheet xmlns="{principal}"><sheetData/></worksheet>', leitor._M_RAIZ_XLSX),
            "tipos de um XLSX": ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
                                 leitor._M_RAIZ_XLSX),
            "XML qualquer": ("<relatorio><linha>1</linha></relatorio>", leitor._M_RAIZ_OUTRA),
            "Workbook sem espaço de nomes": (f'<Workbook xmlns:ss="{ESPACO}">{corpo}</Workbook>',
                                             leitor._M_RAIZ_OUTRA),
            "Workbook de outro espaço": (f'<Workbook xmlns="urn:outro" xmlns:ss="{ESPACO}">{corpo}</Workbook>',
                                         leitor._M_RAIZ_OUTRA),
            "Worksheet como raiz": (f'<Worksheet xmlns="{ESPACO}" xmlns:ss="{ESPACO}"><Table/></Worksheet>',
                                    leitor._M_RAIZ_OUTRA),
            "documento do Word 2003": (
                '<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml"/>',
                leitor._M_RAIZ_OUTRA),
            "planilha ODF plana": ('<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"/>',
                                   leitor._M_RAIZ_OUTRA),
        }
        for rotulo, (texto, esperada) in casos.items():
            with self.subTest(caso=rotulo):
                mensagem = self.recusa("unsupported", '<?xml version="1.0" encoding="UTF-8"?>' + texto, esperada)
                for formato in (".xlsx", ".xls", ".csv"):
                    self.assertIn(formato, mensagem)
        # A raiz errada é recusada sem ler o resto: o que vem depois nem precisa ser XML.
        self.recusa("unsupported", "<html><body><br><p>sem fechar", leitor._M_RAIZ_HTML)

    def test_xml_truncado(self):
        inteiro = tabela(linha(celula("Ação"), celula(12.5), celula(datetime(2026, 11, 9))), linha(celula("fim")))
        self.assertEqual(self.ler(inteiro)["celulas"], 4)
        for corte in ("<Worksheet", 'ss:Name="Rep', "<Table>", "<Row>", "<Cell>", "<Data", "A", "12.", "</Data>",
                      "</Cell>", "</Row>", "</Table>", "</Worksheet>", "</Workbook"):
            with self.subTest(corte=corte):
                self.recusa("invalid", inteiro[:inteiro.index(corte) + len(corte)], leitor._M_MALFORMADO)
        self.recusa("invalid", '<?xml version="1.0"?>', leitor._M_MALFORMADO)
        self.recusa("invalid", "   \r\n  ", leitor._M_MALFORMADO)
        self.recusa("empty", b"", leitor._M_VAZIO)

    def test_instrucao_de_processamento_estranha(self):
        valido = tabela(linha(celula("a")))

        def antes_da_raiz(instrucao):
            return valido.replace("<Workbook", instrucao + "<Workbook", 1)

        casos = {"folha de estilo": antes_da_raiz('<?xml-stylesheet type="text/xsl" href="http://127.0.0.1:9/x.xsl"?>'),
                 # Um gerador que marque o arquivo com uma instrução própria cai aqui: ver _INSTRUCOES_ACEITAS.
                 "de outro aplicativo": antes_da_raiz('<?fr-application created="Gerador Ficticio"?>'),
                 "dentro da tabela": valido.replace("<Row>", "<?php echo 1; ?><Row>", 1),
                 "dentro do texto": valido.replace(">a<", "><?alvo dado?>a<", 1),
                 "depois da raiz": valido + "<?fim?>"}
        for rotulo, texto in casos.items():
            with self.subTest(caso=rotulo):
                self.recusa("invalid", texto, leitor._M_INSTRUCAO)
        # A do Excel é aceita onde estiver, e a falta dela também.
        self.assertEqual(self.valores(valido + apoio.INSTRUCAO), [["a"]])
        self.assertEqual(self.valores(tabela(linha(celula("a")), instrucao="")), [["a"]])

    def test_numero_que_nao_e_numero(self):
        for texto in ("12,5", "1.234,56", "1,234.56", "abc", "NaN", "INF", "-INF", "Infinity", "1e999", "-1E999",
                      "0x10", "1_000", "١٢", "1 2", "--1", "1e", "e5", ".", "+", "12%", "R$ 5", "(4.00)", "5-",
                      "1.2.3", "9" * 65):
            with self.subTest(texto=texto):
                self.recusa("invalid", tabela(linha(celula(texto, tipo="Number"))), leitor._M_NUMERO)

    def test_data_invalida(self):
        for texto in ("2026-02-30T00:00:00.000", "2026-13-01T00:00:00.000", "2025-02-29", "2026-11-09T24:00:00.000",
                      "2026-11-09T10:60:00", "2026-11-09T10:00:61", "0000-01-01T00:00:00", "09/11/2026", "09/11/26",
                      "2026-11-09 10:00:00", "2026-11-09T10", "20261109", "2026-11-9", "46000", "hoje",
                      "2026-11-09T10:00:00.", "٢٠٢٦-١١-٠٩"):
            with self.subTest(texto=texto):
                self.recusa("invalid", tabela(linha(celula(texto, tipo="DateTime"))), leitor._M_DATA)
        # Com fuso a data teria duas leituras: o leitor diz que não lê e o que enviar.
        for texto in ("2026-11-09T17:03:38Z", "2026-11-09T17:03:38.000Z", "2026-11-09T17:03:38-03:00",
                      "2026-11-09T17:03:38+0000", "2026-11-09-03:00"):
            with self.subTest(texto=texto):
                mensagem = self.recusa("unsupported", tabela(linha(celula(texto, tipo="DateTime"))), leitor._M_FUSO)
                self.assertIn(".xlsx", mensagem)

    def test_valor_logico_invalido(self):
        for texto in ("2", "-1", "sim", "verdadeiro", "yes", "T"):
            with self.subTest(texto=texto):
                self.recusa("invalid", tabela(linha(celula(texto, tipo="Boolean"))), leitor._M_LOGICO)

    def test_celula_sem_tipo_ou_com_tipo_desconhecido(self):
        for bruto in ("<Cell><Data>5</Data></Cell>", '<Cell><Data ss:Type="">5</Data></Cell>',
                      '<Cell><Data ss:Type="string">a</Data></Cell>', '<Cell><Data ss:Type="Texto">a</Data></Cell>',
                      '<Cell><Data ss:Type="Integer">5</Data></Cell>', '<Cell><Data Type="String">a</Data></Cell>',
                      '<Cell><Data x:Type="String">a</Data></Cell>'):
            with self.subTest(xml=bruto):
                self.recusa("invalid", tabela(linha(bruto)), leitor._M_TIPO)

    def test_elemento_da_planilha_fora_do_lugar(self):
        um = TEXTO.format("a")
        casos = {
            "dois Data na célula": tabela(f"<Row><Cell>{um}{um}</Cell></Row>"),
            "Data direto na linha": tabela(f"<Row>{um}</Row>"),
            "Data dentro de Data": tabela(f'<Row><Cell><Data ss:Type="String">a{um}</Data></Cell></Row>'),
            "Cell dentro de Data": tabela('<Row><Cell><Data ss:Type="String"><Cell/></Data></Cell></Row>'),
            "Comment dentro de Data": tabela('<Row><Cell><Data ss:Type="String"><Comment/></Data></Cell></Row>'),
            "Cell direto na tabela": tabela(f"<Cell>{um}</Cell>"),
            "Cell dentro de Cell": tabela(f"<Row><Cell><Cell>{um}</Cell></Cell></Row>"),
            "Row dentro de Row": tabela(f"<Row><Row><Cell>{um}</Cell></Row></Row>"),
            "Row direto na aba": livro(f' <Worksheet ss:Name="a"><Row><Cell>{um}</Cell></Row></Worksheet>'),
            "Row dentro de outro elemento": tabela(f"<x:Grupo><Row><Cell>{um}</Cell></Row></x:Grupo>"),
            "duas tabelas na aba": livro(' <Worksheet ss:Name="a"><Table/><Table/></Worksheet>'),
            "Table direto no livro": livro(f"<Table><Row><Cell>{um}</Cell></Row></Table>"),
            "Worksheet dentro de Worksheet": livro(' <Worksheet ss:Name="a"><Worksheet ss:Name="b"/></Worksheet>'),
            "Workbook dentro de Workbook": livro(f'<Workbook xmlns="{ESPACO}"/>'),
        }
        for rotulo, texto in casos.items():
            with self.subTest(caso=rotulo):
                self.recusa("invalid", texto, leitor._M_ESTRUTURA)

    def test_texto_fora_de_data_e_recusado(self):
        # Um gerador que escreve o valor direto em Cell não pode virar planilha vazia em silêncio.
        for bruto in ("<Row><Cell>12,50</Cell></Row>", "<Row>solto<Cell/></Row>", "<Row/>solto",
                      '<Row><Cell><Data ss:Type="Number">1</Data>resto</Cell></Row>'):
            with self.subTest(xml=bruto):
                self.recusa("invalid", tabela(bruto), leitor._M_TEXTO_SOLTO)
        self.recusa("invalid", livro(' <Worksheet ss:Name="a">solto</Worksheet>'), leitor._M_TEXTO_SOLTO)
        self.recusa("invalid", livro("solto", aba(linha(celula("a")))), leitor._M_TEXTO_SOLTO)
        # Texto de propriedade, de opção e de quebra de página não é célula.
        quebras = ('  <PageBreaks xmlns="urn:schemas-microsoft-com:office:excel"><RowBreaks><RowBreak><Row>25</Row>'
                   "</RowBreak></RowBreaks></PageBreaks>\n")
        propriedades = ('<DocumentProperties xmlns="urn:schemas-microsoft-com:office:office">'
                        "<Author>Autor Ficticio</Author></DocumentProperties>")
        outros = livro(aba(linha(celula("a")), depois=quebras), antes=propriedades)
        self.assertEqual(self.valores(outros), [["a"]])

    def test_conteudo_que_nao_e_xml(self):
        valido = tabela(linha(celula("a")))
        casos = {"zip": b"PK\x03\x04" + bytes(200), "ole": bytes.fromhex("d0cf11e0a1b11ae1") + bytes(504),
                 "pdf": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< >>\nendobj\n",
                 "csv": b"Codigo;Nome\r\n1;Lixa ficticia\r\n", "json": b'{"a": 1}', "nulos": bytes(4096),
                 "nulo no meio": valido.replace(">a<", ">a\x00<").encode(),
                 "controle no texto": valido.replace(">a<", ">a\x01<").encode(),
                 "lixo depois da raiz": (valido + "<outro/>").encode(),
                 "texto depois da raiz": (valido + "fim").encode(),
                 "duas declarações": ('<?xml version="1.0"?>' + valido).encode(),
                 "marca fora de ordem": valido.replace("</Cell></Row>", "</Row></Cell>").encode(),
                 "atributo repetido": valido.replace("<Row>", '<Row ss:Index="1" ss:Index="1">').encode(),
                 "prefixo sem declaração": valido.replace("<Row>", '<Row q:Index="1">').encode()}
        for rotulo, dados in casos.items():
            with self.subTest(caso=rotulo):
                self.recusa("invalid", dados, leitor._M_MALFORMADO)


# =============================================================================
# 7. Contrato
# =============================================================================
class _Prazo(Exception):
    pass


class ContratoTests(_Caso):
    def test_chaves_e_tipos_do_resultado(self):
        resultado = self.ler(tabela(linha(celula("a"), celula(1))))
        self.assertEqual(set(resultado), CHAVES)
        for chave in CHAVES - {"valores"}:
            self.assertIs(type(resultado[chave]), int, chave)
        self.assertIs(type(resultado["valores"]), list)
        self.assertTrue(all(type(registro) is list for registro in resultado["valores"]))

    def test_sem_materializar_devolve_as_mesmas_contagens_e_as_mesmas_recusas(self):
        texto = apoio.RELATORIO_DO_ERP
        completo = self.ler(texto)
        contagens = self.ler(texto, materializar=False)
        self.assertIsNone(contagens["valores"])
        self.assertEqual({chave: completo[chave] for chave in CHAVES - {"valores"}},
                         {chave: contagens[chave] for chave in CHAVES - {"valores"}})
        self.recusa("invalid", tabela(linha(celula("12,5", tipo="Number"))), leitor._M_NUMERO, materializar=False)
        self.recusa("cells", tabela(linha(celula(1), celula(2))), max_celulas=1, materializar=False)
        self.recusa("empty", livro(aba()), materializar=False)

    def test_aceita_caminho_em_texto_ou_path_e_le_igual_duas_vezes(self):
        caminho = self.arquivo(apoio.RELATORIO_DO_ERP)
        primeira = leitor.ler(caminho, **LIMITES)
        self.assertEqual(leitor.ler(Path(caminho), **LIMITES), primeira)
        self.assertEqual(leitor.ler(caminho, **LIMITES), primeira)

    def test_verificar_e_chamada_antes_de_cada_bloco(self):
        texto = tabela(*(linha(celula(f"PRODUTO FICTICIO {numero:05d}"), celula(numero)) for numero in range(2_500)))
        caminho = self.arquivo(texto)
        blocos = -(-Path(caminho).stat().st_size // leitor._BLOCO)
        self.assertGreaterEqual(blocos, 3)
        chamadas = []
        leitor.ler(caminho, verificar=lambda: chamadas.append(1), materializar=False, **LIMITES)
        self.assertEqual(len(chamadas), blocos + 1)  # uma antes de cada bloco e a da leitura que acha o fim
        # Com a matriz, mais uma a cada 4.096 linhas.
        longa = self.arquivo(tabela(linha(celula("começo")), linha(celula("fim"), indice=9_000)))
        chamadas.clear()
        resultado = leitor.ler(longa, verificar=lambda: chamadas.append(1), **LIMITES)
        self.assertEqual((resultado["linhas"], len(chamadas)), (9_000, 2 + 9_000 // 4_096))

    def test_o_que_verificar_levanta_passa_adiante(self):
        def prazo_na_chamada(numero):
            chamadas = []

            def verificar():
                chamadas.append(1)
                if len(chamadas) == numero:
                    raise _Prazo("prazo")
            return verificar

        texto = tabela(*(linha(celula(f"PRODUTO FICTICIO {numero:05d}"), celula(numero)) for numero in range(2_500)))
        with self.assertRaises(_Prazo):
            self.ler(texto, verificar=prazo_na_chamada(2))  # antes do segundo bloco
        # Também durante a montagem da matriz (a terceira chamada aqui é a da linha 4.096).
        longa = tabela(linha(celula("começo")), linha(celula("fim"), indice=9_000))
        with self.assertRaises(_Prazo):
            self.ler(longa, verificar=prazo_na_chamada(3))
        # Nem a recusa do próprio chamador é convertida.
        with self.assertRaises(leitor.LeituraRecusada) as contexto:
            self.ler(tabela(linha(celula("a"))), verificar=lambda: leitor._recusar("timeout", "prazo do chamador"))
        self.assertEqual(contexto.exception.codigo, "timeout")

    def test_memory_error_propaga(self):
        numero = tabela(linha(celula(1.5)))
        with mock.patch.object(leitor, "_numero", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                self.ler(numero)

        def sem_memoria():
            raise MemoryError
        with self.assertRaises(MemoryError):
            self.ler(numero, verificar=sem_memoria)
        # A falta de memória dentro do expat chega como erro do expat: o leitor a devolve como MemoryError.
        erro = expat.ExpatError("out of memory")
        erro.code = expat.errors.codes[expat.errors.XML_ERROR_NO_MEMORY]
        falso = mock.Mock()
        falso.Parse.side_effect = erro
        with mock.patch.object(leitor.expat, "ParserCreate", return_value=falso):
            with self.assertRaises(MemoryError):
                self.ler(numero)

    def test_falha_inesperada_durante_a_leitura_vira_recusa_e_nao_excecao_crua(self):
        for alvo, texto in (("_data", tabela(linha(celula(datetime(2026, 11, 9))))),
                            ("_numero", tabela(linha(celula(1.5))))):
            for falha in (RuntimeError("x"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "x"), KeyError("x"),
                          RecursionError("x"), OverflowError("x")):
                with self.subTest(alvo=alvo, falha=type(falha).__name__):
                    with mock.patch.object(leitor, alvo, side_effect=falha):
                        self.recusa("invalid", texto, leitor._M_MALFORMADO)

    def test_limite_invalido_e_erro_de_quem_chama(self):
        caminho = self.arquivo(tabela(linha(celula("a"))))
        for nome in LIMITES:
            for valor in (0, -1, None, "8", 1.5, True):
                with self.subTest(limite=nome, valor=valor):
                    with self.assertRaises(ValueError) as contexto:
                        leitor.ler(caminho, **{**LIMITES, nome: valor})
                    self.assertIn(nome, str(contexto.exception))
        # `max_area` é opcional: None (o padrão) dispensa o teto; outro valor que não seja inteiro positivo é erro.
        for valor in (0, -1, "8", 1.5, True):
            with self.subTest(limite="max_area", valor=valor):
                with self.assertRaises(ValueError) as contexto:
                    leitor.ler(caminho, max_area=valor, **LIMITES)
                self.assertIn("max_area", str(contexto.exception))
        self.assertEqual(leitor.ler(caminho, max_area=None, **LIMITES)["celulas"], 1)
        with self.assertRaises(TypeError) as contexto:
            leitor.ler(caminho, verificar=5, **LIMITES)
        self.assertIn("verificar", str(contexto.exception))
        with self.assertRaises(TypeError):
            leitor.ler(caminho, 8, 100, 100, 100, 100)  # os limites só entram pelo nome

    def test_arquivo_que_nao_existe_e_erro_de_disco_e_nao_de_formato(self):
        with self.assertRaises(FileNotFoundError):
            leitor.ler(str(self.pasta / "nao_existe.xml"), **LIMITES)

    def test_recusa_tem_codigo_e_mensagem(self):
        erro = leitor.LeituraRecusada("rows", "Mensagem fixa.")
        self.assertEqual((erro.codigo, erro.mensagem, str(erro)), ("rows", "Mensagem fixa.", "Mensagem fixa."))
        self.assertIsInstance(erro, Exception)
        self.assertNotIsInstance(erro, (ValueError, OSError))
        copia = pickle.loads(pickle.dumps(erro))  # atravessa um processo sem perder o código
        self.assertIs(type(copia), leitor.LeituraRecusada)
        self.assertEqual((copia.codigo, copia.mensagem), ("rows", "Mensagem fixa."))
        self.assertTrue(MENSAGENS)
        for mensagem in MENSAGENS:
            self.assertTrue(mensagem.endswith("."), mensagem)

    def test_leitura_nao_grava_nada(self):
        caminho = Path(self.arquivo(apoio.RELATORIO_DO_ERP))
        antes = (caminho.read_bytes(), caminho.stat().st_mtime_ns)
        leitor.ler(str(caminho), **LIMITES)
        with self.assertRaises(leitor.LeituraRecusada):
            leitor.ler(str(caminho), **{**LIMITES, "max_celulas": 3})
        self.assertEqual([item.name for item in self.pasta.iterdir()], [caminho.name])
        self.assertEqual((caminho.read_bytes(), caminho.stat().st_mtime_ns), antes)

    def test_modulo_so_usa_a_biblioteca_padrao_e_so_abre_arquivo_para_ler(self):
        arvore = ast.parse(Path(leitor.__file__).read_text(encoding="utf-8"))
        importados = set()
        aberturas = []
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                importados.update(item.name for item in no.names)
            elif isinstance(no, ast.ImportFrom):
                importados.add(no.module)
            elif isinstance(no, ast.Call) and isinstance(no.func, ast.Name):
                self.assertNotIn(no.func.id, {"eval", "exec", "compile", "__import__"})
                if no.func.id == "open":
                    aberturas.append([item.value for item in no.args[1:] if isinstance(item, ast.Constant)])
        self.assertEqual(importados, {"__future__", "datetime", "re", "xml.parsers"})
        self.assertEqual(aberturas, [["rb"]])


# =============================================================================
# 8. O relatório fictício completo, no desenho do ERP
# =============================================================================
EMISSAO = datetime(2026, 11, 9, 17, 3, 38)
RODAPE = "Software desenvolvido por Fabricante Ficticio Informatica - www.example.invalid"
CABECALHO_1 = {1: "Codigo", 2: "Nome do produto", 10: "Qtde", 11: "Total", 13: "Total", 15: "Marg. de", 17: "Lucro",
               18: "Estoque"}
CABECALHO_2 = {10: "Vend", 11: "Venda", 13: "Custo", 15: "Lucro %", 18: "Atual"}
# {linha: {coluna: valor}}, base um, escrito à mão a partir do desenho do relatório (e não do XML).
RELATORIO_ESPERADO = {
    2: {6: "Giro de Vendas por Produto", 15: "Página ...:", 16: EMISSAO, 17: "1 de 2"},
    3: {6: "Período de  : 09/11/23 á 09/11/26 - Andamento: Todas vendas"},
    4: {1: "Produtos ....:  -Todos  até  -Todos"},
    5: {1: "Clientes ....:  -Todos  até  -Todos"},
    6: {1: "Vendedores ..:  -Todos  até  -Todos"},
    7: {1: "Gr.Clientes .:  -Todos  até  -Todos"},
    8: {1: "Gr.Produtos .:  -Todos  até  -Todos"},
    9: {1: "Sub-Gr.Prods :  -Todos  até  -Todos"},
    10: {1: "Fornecedores :  -Todos  até  -Todos"},
    11: {1: "Filiais .....:  9-LOJA FICTICIA  até  9-LOJA FICTICIA"},
    12: {1: "L_FiltroProduto" + "X" * 40},
    14: CABECALHO_1,
    15: CABECALHO_2,
    16: {1: "0700019", 2: "MASSA DE REPARO FICTICIA 1KG", 10: 12, 11: 480, 13: 300, 15: 60, 16: "%", 17: 180, 18: 3},
    18: {1: "0600027", 2: "ADAPTADOR FICTICIO 25MM X 3/4", 10: 40, 11: 220, 13: 120, 15: 83.33, 16: "%", 17: 100,
         18: -4},
    20: {1: "0300035", 2: "ADESIVO FICTICIO P/ LAMINAÇÃO 880G & CIA", 10: 332, 11: 14408.63, 13: 8993.42, 15: 60.21,
         16: "%", 17: 5415.21, 18: 29},
    22: {1: "0300043", 2: "SOLVENTE FICTICIO 0,9L", 10: 16, 11: 270.4, 13: 179.96, 15: 50.25, 16: "%", 17: 90.44,
         18: 41},
    24: {3: RODAPE},
    26: CABECALHO_1,
    27: CABECALHO_2,
    28: {1: "0100051", 2: "ESMALTE FICTICIO BRANCO 3,6L", 10: 90, 11: 8100, 13: 5400, 15: 50, 16: "%", 17: 2700,
         18: 12.5},
    30: {1: "0500069", 2: "LIXA FICTICIA GRAO 80", 10: 165, 11: 2710.22, 13: 1704.45, 15: 59, 16: "%", 17: 1005.77,
         18: -2},
    32: {1: "0600077", 2: "VERNIZ FICTICIO 3,0L", 10: 8, 11: 703.2, 15: "%", 17: 0, 18: 0},
    34: {1: 'Movimentação produto: "PRODUTO GENERICO" - Qtde Vend.: 0,0 - Total Liq.: 0,00'},
    35: {1: "Totais:", 2: "Descontos:", 4: 512.34, 9: 663, 11: 26892.45, 13: 16697.83, 15: "45,00%", 17: 9491.42},
    37: {1: "Valor total dos fretes ...:", 5: 8.5},
    39: {1: 'Atenção .: Total das vendas não esta considerando "produto genérico".'},
    41: {3: RODAPE},
}
LINHAS_DE_PRODUTO = (16, 18, 20, 22, 28, 30, 32)


class RelatorioDoErpTests(_Caso):
    def conferir(self, resultado):
        self.assertEqual({chave: resultado[chave] for chave in CHAVES - {"valores"}},
                         {"abas": 3, "aba": 0, "linhas": 41, "colunas": 18, "celulas": 115, "celulas_no_arquivo": 118})
        self.assertEqual(sum(len(registro) for registro in RELATORIO_ESPERADO.values()), 115)
        valores = resultado["valores"]
        self.assertEqual(len(valores), 41)
        for numero, registro in enumerate(valores, 1):
            self.assertEqual(len(registro), 18)
            esperado = RELATORIO_ESPERADO.get(numero, {})
            for coluna, valor in enumerate(registro, 1):
                with self.subTest(linha=numero, coluna=coluna):
                    self.assertEqual(valor, esperado.get(coluna))
                    self.assertIs(type(valor), type(esperado.get(coluna)))

    def test_relatorio_completo_celula_a_celula(self):
        resultado = self.ler_do_disco(apoio.RELATORIO_DO_ERP)
        self.conferir(resultado)
        # Os números chegam como números: as colunas fecham com a linha de totais do próprio relatório.
        valores = resultado["valores"]
        for coluna in (11, 13, 17):
            soma = sum(valores[numero - 1][coluna - 1] or 0 for numero in LINHAS_DE_PRODUTO)
            self.assertAlmostEqual(soma, valores[34][coluna - 1], places=2)
        self.assertEqual(sum(valores[numero - 1][9] for numero in LINHAS_DE_PRODUTO), valores[34][8])

    def test_o_mesmo_relatorio_em_outras_codificacoes_e_fins_de_linha(self):
        texto = apoio.RELATORIO_DO_ERP
        for declarada, codec in (("UTF-8", "utf-8-sig"), ("UTF-16", "utf-16"), ("windows-1252", "cp1252"),
                                 ("ISO-8859-1", "latin-1")):
            with self.subTest(codificacao=declarada):
                declarado = texto.replace('<?xml version="1.0"?>', f'<?xml version="1.0" encoding="{declarada}"?>', 1)
                self.conferir(self.ler(declarado.encode(codec)))
        with self.subTest(fim_de_linha="CRLF"):
            self.conferir(self.ler(texto.replace("\n", "\r\n")))
        with self.subTest(fim_de_linha="nenhum"):
            self.conferir(self.ler(" ".join(parte.strip() for parte in texto.split("\n"))))

    def test_o_mesmo_relatorio_lido_em_blocos_minusculos(self):
        # Marcas, entidades e caracteres de mais de um byte ficam partidos entre os blocos.
        for tamanho in (1, 7, 64):
            for codec in ("utf-8", "utf-16"):
                with self.subTest(bloco=tamanho, codificacao=codec):
                    with mock.patch.object(leitor, "_BLOCO", tamanho):
                        self.conferir(self.ler(apoio.RELATORIO_DO_ERP.encode(codec)))

    def test_arquivo_de_varios_blocos(self):
        def item(numero):
            return [f"PRODUTO FICTICIO {numero:05d} P/ LAMINAÇÃO", numero, numero / 8,
                    datetime(2025, 1 + numero % 12, 1 + numero % 28)]

        itens = range(3_000)
        texto = tabela(*(linha(*map(celula, item(numero)), celula("%", indice=9)) for numero in itens))
        caminho = self.arquivo(texto)
        self.assertGreater(Path(caminho).stat().st_size, 5 * leitor._BLOCO)
        resultado = leitor.ler(caminho, **LIMITES)
        self.assertEqual(resultado["valores"], [item(numero) + [None, None, None, None, "%"] for numero in itens])
        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]), (3_000, 9, 15_000))

    @unittest.skipIf(leiautes is None, "tests/leiautes_erp_support.py não está nesta cópia")
    def test_os_oito_leiautes_do_erp_gravados_como_planilha_xml(self):
        """Os oito relatórios fictícios da auditoria (os mesmos que o motor lê em .xlsx), gravados como
        Planilha XML do jeito do Excel e do jeito dos geradores de relatório: a matriz é a mesma."""
        for nome, montar in leiautes.RELATORIOS.items():
            linhas, mesclas, _formatos, _verdade = montar()
            esperada = apoio.matriz_esperada(linhas)
            celulas = sum(1 for registro in linhas for valor in registro.values() if valor is not None)
            for indices in ("necessarios", "sempre"):
                for em_branco in ("omitidas", "vazias"):
                    with self.subTest(relatorio=nome, indices=indices, linhas_em_branco=em_branco):
                        texto = apoio.texto_do_relatorio(linhas, mesclas, indices=indices, linhas_em_branco=em_branco)
                        resultado = self.ler(texto)
                        self.assertEqual(resultado["valores"], esperada)
                        self.assertEqual((resultado["linhas"], resultado["colunas"], resultado["celulas"]),
                                         (len(esperada), len(esperada[0]), celulas))

    def test_grades_sorteadas_gravadas_dos_quatro_jeitos(self):
        """Grades esparsas sorteadas (semente fixa), com mesclas que não se sobrepõem e valores de todos os
        tipos: gravada com ou sem os índices que dá para omitir, com ou sem as linhas em branco, a matriz lida
        é sempre a que foi gravada, valor por valor e tipo por tipo."""
        def com_tipos(matriz):
            return [[(type(valor).__name__, valor) for valor in registro] for registro in matriz]

        sorteio = random.Random(20261110)
        amostras = ("texto", "Ação & <marca>", "0700019", " ", 0, 7, -3, 12.5, -0.125, 1e-7, True, False,
                    datetime(2026, 11, 9), datetime(2025, 2, 28, 23, 59, 59))
        conferidas = 0
        for rodada in range(40):
            altura, largura = sorteio.randint(1, 14), sorteio.randint(1, 12)
            cobertas, mesclas = set(), []
            for _ in range(sorteio.randint(0, 6)):
                topo, esquerda = sorteio.randrange(altura), sorteio.randrange(largura)
                base = min(altura - 1, topo + sorteio.randint(0, 2))
                direita = min(largura - 1, esquerda + sorteio.randint(0, 3))
                area = {(lin, col) for lin in range(topo, base + 1) for col in range(esquerda, direita + 1)}
                if len(area) > 1 and not area & cobertas:
                    cobertas |= area
                    mesclas.append((topo, esquerda, base, direita))
            origens = {(topo, esquerda) for topo, esquerda, _base, _direita in mesclas}
            linhas = [{} for _ in range(altura)]
            for lin in range(altura):
                for col in range(largura):
                    if ((lin, col) not in cobertas or (lin, col) in origens) and sorteio.random() < 0.45:
                        linhas[lin][col] = sorteio.choice(amostras)
            esperada = apoio.matriz_esperada(linhas)
            if not esperada:
                continue
            conferidas += 1
            for indices in ("necessarios", "sempre"):
                for em_branco in ("omitidas", "vazias"):
                    with self.subTest(rodada=rodada, indices=indices, linhas_em_branco=em_branco):
                        texto = apoio.texto_do_relatorio(linhas, mesclas, indices=indices, linhas_em_branco=em_branco)
                        self.assertEqual(com_tipos(self.valores(texto)), com_tipos(esperada))
        self.assertGreaterEqual(conferidas, 35)

    def test_gravador_de_apoio_e_leitor_fecham_com_tipos_misturados(self):
        linhas = [{}, {0: "título", 4: datetime(2026, 11, 9, 17, 3, 38)}, {},
                  {0: "a", 1: 1, 2: 1.5, 3: True, 5: 'aspas " e <marca> & e\nquebra'}, {}, {2: -0.125}, {}, {}]
        mesclas = [(1, 0, 1, 3), (3, 3, 4, 4), (5, 0, 6, 1)]   # a última começa numa célula sem valor
        esperada = apoio.matriz_esperada(linhas)
        self.assertEqual(len(esperada), 6)
        for indices in ("necessarios", "sempre"):
            for em_branco in ("omitidas", "vazias"):
                with self.subTest(indices=indices, linhas_em_branco=em_branco):
                    resultado = self.ler(apoio.texto_do_relatorio(linhas, mesclas, indices=indices,
                                                                  linhas_em_branco=em_branco))
                    self.assertEqual(resultado["valores"], esperada)
                    self.assertEqual([type(valor) for valor in resultado["valores"][3]],
                                     [str, int, float, bool, type(None), str])


# =============================================================================
# 9. Robustez: nenhuma exceção crua
# =============================================================================
class RobustezTests(_Caso):
    def resultado_ou_recusa(self, dados):
        """Lê `dados` e confere o desfecho: o resultado do contrato ou uma recusa do contrato."""
        try:
            resultado = self.ler(dados)
        except leitor.LeituraRecusada as erro:
            self.assertIn(erro.codigo, CODIGOS)
            self.assertIn(erro.mensagem, MENSAGENS)
            return erro.codigo
        self.assertEqual(set(resultado), CHAVES)
        self.assertEqual(len(resultado["valores"]), resultado["linhas"])
        self.assertEqual({len(registro) for registro in resultado["valores"]}, {resultado["colunas"]})
        return None

    def bases(self):
        texto = apoio.RELATORIO_DO_ERP
        em_1252 = texto.replace('<?xml version="1.0"?>', '<?xml version="1.0" encoding="windows-1252"?>', 1)
        return {"utf-8": texto.encode("utf-8"), "utf-16": texto.encode("utf-16"),
                "windows-1252": em_1252.encode("cp1252")}

    def test_cortado_em_qualquer_ponto_e_sempre_xml_incompleto(self):
        for nome, dados in self.bases().items():
            fim = len(dados) - len("</Workbook>\n".encode("utf-16-le" if nome == "utf-16" else "ascii"))
            for corte in range(1, fim, 53):
                with self.subTest(codificacao=nome, corte=corte):
                    self.assertEqual(self.resultado_ou_recusa(dados[:corte]), "invalid")

    def test_corrompido_ao_acaso_nunca_solta_excecao_crua(self):
        sorteio = random.Random(20261110)
        desfechos = set()
        for nome, dados in self.bases().items():
            for rodada in range(350):
                copia = bytearray(dados)
                for _ in range(sorteio.randint(1, 3)):
                    posicao = sorteio.randrange(len(copia))
                    operacao = sorteio.randrange(4)
                    if operacao == 0:
                        copia[posicao] = sorteio.randrange(256)
                    elif operacao == 1:
                        del copia[posicao:posicao + sorteio.randint(1, 24)]
                    elif operacao == 2:
                        copia[posicao:posicao] = bytes(sorteio.randrange(256) for _ in range(sorteio.randint(1, 8)))
                    else:
                        outro = sorteio.randrange(len(copia))
                        copia[posicao:posicao] = copia[outro:outro + sorteio.randint(1, 60)]
                with self.subTest(codificacao=nome, rodada=rodada):
                    desfechos.add(self.resultado_ou_recusa(bytes(copia)))
        # O sorteio (semente fixa) passa pelos dois desfechos: leitura e recusa.
        self.assertIn(None, desfechos)
        self.assertIn("invalid", desfechos)

    def test_substituto_solto_em_utf16_nao_solta_excecao_crua(self):
        texto = '<?xml version="1.0" encoding="UTF-16"?>' + livro(aba(linha(celula("x"))), declaracao="")
        inicio = texto.encode("utf-16-le")
        marca = "x</Data>".encode("utf-16-le")
        for solto in (b"\x00\xd8", b"\x00\xdc", b"\xff\xdb\xff\xdb"):
            with self.subTest(solto=solto.hex()):
                self.resultado_ou_recusa(b"\xff\xfe" + inicio.replace(marca, b"x\x00" + solto + marca[2:]))


if __name__ == "__main__":
    unittest.main()
