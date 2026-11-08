"""Baia de Segurança (security_gatekeeper.py): triagem dos relatórios antes do motor e do banco.

Cobre as seis exigências da especificação:
  1. assinatura binária: só XLSX, XLS, CSV e TXT verdadeiros; executáveis, scripts, HTML,
     PDF, imagens, compactados e disfarces de extensão são recusados;
  2. neutralização de fórmula em célula (=, +, -, @, TAB e CR), sem mudar número nenhum;
  3. macros, ActiveX, objetos OLE, vínculos e fórmulas do XLSX: só os valores seguem;
  4. teto de 50 MiB na leitura em blocos;
  5. estrutura mínima de relatório de PDV;
  6. quarentena com a mensagem padrão, sem detalhe interno, e log sem conteúdo.

O worker real do ETL exige Linux: aqui a triagem roda no próprio processo, com a mesma
função e a mesma ida e volta em JSON (validar_como_o_worker). Os arquivos são fictícios.
"""

import hashlib
import io
import json
import logging
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from openpyxl import Workbook
from sqlalchemy import select
from werkzeug.datastructures import FileStorage

from etl_inputs import ETLInputError, read_raw
from etl_limits import ETLLimits, get_limits, using_limits
import security_gatekeeper as baia
from security_gatekeeper import MENSAGEM_PADRAO, SecurityGatekeeperError
from tests.deploy_support import DeployTestCase


LIMITES = ETLLimits()
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
VENDAS = b"Produto;Quantidade;Valor total\nCafe 500g;2;20,00\nLeite 1L;-1;-5,00\n"
COMPRAS = b"Fornecedor;Valor NF\nFornecedor ficticio;40,00\n"
CARGA_DDE = "=cmd|' /C calc'!A0"


def validar_como_o_worker(caminho, extensao, limits=None):
    """etl_runtime.run_validation sem o processo filho: a mesma triagem (operação validate do
    etl_worker) e a mesma ida e volta em JSON. O erro volta só com código e mensagem."""
    politica = get_limits() if limits is None else limits
    with using_limits(politica):
        try:
            relatorio = baia.triar(caminho, extensao, politica)
        except ETLInputError as erro:
            raise ETLInputError(erro.code, erro.safe_message) from None
    return json.loads(json.dumps(relatorio, ensure_ascii=True, allow_nan=False))


def executavel_pe():
    cabecalho = bytearray(b"MZ" + bytes(0x3A) + (0x80).to_bytes(4, "little"))
    return bytes(cabecalho) + bytes(0x80 - len(cabecalho)) + b"PE\x00\x00" + bytes(256)


def documento_ole(fluxo):
    """Arquivo composto OLE2 mínimo: cabeçalho, um setor de FAT e o diretório com o fluxo."""
    cabecalho = bytearray(512)
    cabecalho[0:8] = OLE
    cabecalho[0x18:0x1C] = (0x3E).to_bytes(2, "little") + (3).to_bytes(2, "little")
    cabecalho[0x1C:0x22] = b"\xfe\xff" + (9).to_bytes(2, "little") + (6).to_bytes(2, "little")
    cabecalho[0x2C:0x34] = (1).to_bytes(4, "little") + (1).to_bytes(4, "little")
    cabecalho[0x4C:0x200] = b"\x00\x00\x00\x00" + b"\xff" * (0x200 - 0x50)

    def entrada(nome, tipo):
        bruto = nome.encode("utf-16-le") + b"\x00\x00"
        dados = bytearray(128)
        dados[:len(bruto)] = bruto
        dados[0x40:0x42] = len(bruto).to_bytes(2, "little")
        dados[0x42] = tipo
        return bytes(dados)

    return bytes(cabecalho) + bytes(512) + entrada("Root Entry", 5) + entrada(fluxo, 2) + bytes(256)


def planilha(linhas, *, textos_literais=()):
    """XLSX do openpyxl. `textos_literais`: coordenadas gravadas como texto mesmo começando com =."""
    livro = Workbook()
    aba = livro.active
    for linha in linhas:
        aba.append(linha)
    for coordenada in textos_literais:
        aba[coordenada].data_type = "s"
    saida = io.BytesIO()
    livro.save(saida)
    livro.close()
    return saida.getvalue()


def com_conteudo_ativo(xlsx):
    """O XLSX com partes de macro, ActiveX, OLE, vínculo externo, conexão e interface própria."""
    extras = {
        "xl/vbaProject.bin": OLE + bytes(600),
        "xl/activeX/activeX1.xml": b'<?xml version="1.0"?><ax:ocx xmlns:ax="urn:ficticio"/>',
        "xl/embeddings/oleObject1.bin": OLE + bytes(200),
        "xl/externalLinks/externalLink1.xml": b'<?xml version="1.0"?><externalLink xmlns="urn:ficticio"/>',
        "xl/connections.xml": b'<?xml version="1.0"?><connections xmlns="urn:ficticio"/>',
        "customUI/customUI.xml": b'<?xml version="1.0"?><customUI xmlns="urn:ficticio"/>',
    }
    saida = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(xlsx)) as origem, zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as destino:
        for info in origem.infolist():
            dados = origem.read(info.filename)
            if info.filename == "[Content_Types].xml":
                # Pasta habilitada para macro (.xlsm) com o projeto VBA declarado.
                dados = dados.replace(b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                                      b"application/vnd.ms-excel.sheet.macroEnabled.main+xml")
                dados = dados.replace(b"</Types>", b'<Override PartName="/xl/vbaProject.bin" '
                                      b'ContentType="application/vnd.ms-office.vbaProject"/></Types>')
            destino.writestr(info.filename, dados)
        for nome, dados in extras.items():
            destino.writestr(nome, dados)
    return saida.getvalue()


_PRINCIPAL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CELULAS_DE_DADOS = (
    '<row r="1"><c r="A1" t="inlineStr"><is><t>Produto</t></is></c><c r="B1" t="inlineStr"><is><t>Qtde</t></is></c>'
    '<c r="C1" t="inlineStr"><is><t>Valor total</t></is></c></row>'
    '<row r="2"><c r="A2" t="inlineStr"><is><t>Cafe</t></is></c><c r="B2"><v>2</v></c>{c2}</row>'
    '<row r="3"><c r="A3" t="inlineStr"><is><t>Leite</t></is></c><c r="B3"><v>1</v></c>{c3}</row>')
ABA_SEM_FORMULA = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                   + _CELULAS_DE_DADOS.format(c2='<c r="C2"><v>20</v></c>', c3='<c r="C3"><v>5</v></c>')
                   + "</sheetData></worksheet>")
ABA_COM_FORMULA = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                   + _CELULAS_DE_DADOS.format(c2='<c r="C2"><f>B2*10</f><v>20</v></c>',
                                              c3="<c r=\"C3\"><f>cmd|' /C calc'!A0</f><v>5</v></c>")
                   + "</sheetData></worksheet>")


def xlsx_manual(aba=ABA_SEM_FORMULA, *, caminho_aba="xl/worksheets/sheet1.xml", codificacao="utf-8",
                partes=None, relacoes="", nomes=""):
    """XLSX montado à mão para pôr partes onde o openpyxl não põe: o pacote acha as partes pelas
    relações, em qualquer caminho, e o motor lê a aba de lá normalmente."""
    tipos_planilha = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    conteudo = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{tipos_planilha}.sheet.main+xml"/>'
            f'<Override PartName="/{caminho_aba}" ContentType="{tipos_planilha}.worksheet+xml"/></Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{_PRINCIPAL}/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{_PRINCIPAL}">'
            f'<sheets><sheet name="Dados" sheetId="1" r:id="rId1"/></sheets>{nomes}</workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{_PRINCIPAL}/worksheet" Target="/{caminho_aba}"/>{relacoes}'
            '</Relationships>'),
    }
    saida = io.BytesIO()
    with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as pacote:
        for nome, dados in conteudo.items():
            pacote.writestr(nome, dados.encode("utf-8"))
        declaracao = f'<?xml version="1.0" encoding="{codificacao.upper()}" standalone="yes"?>\n'
        pacote.writestr(caminho_aba, (declaracao + aba).encode(codificacao))
        for nome, dados in (partes or {}).items():
            pacote.writestr(nome, dados)
    return saida.getvalue()


class _Pasta(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory(prefix="nexo-baia-teste-")
        self.addCleanup(self.pasta.cleanup)
        self.dir = Path(self.pasta.name)
        self.contador = 0

    def arquivo(self, conteudo, extensao):
        self.contador += 1
        pasta = self.dir / f"envio{self.contador}"
        pasta.mkdir()
        caminho = pasta / f"arquivo.{extensao}"
        caminho.write_bytes(conteudo)
        return caminho

    def recusa(self, conteudo, extensao, motivo):
        caminho = self.arquivo(conteudo, extensao)
        with self.assertRaises(SecurityGatekeeperError) as erro:
            baia.inspecionar_arquivo(caminho, extensao)
        self.assertEqual(erro.exception.motivo, motivo)
        self.assertEqual(erro.exception.code, "quarentena:" + motivo)
        self.assertTrue(erro.exception.safe_message.startswith(MENSAGEM_PADRAO))
        self.assertNotIn(str(self.dir), erro.exception.safe_message)
        self.assertNotIn(motivo, erro.exception.safe_message)
        return erro.exception

    def triar(self, conteudo, extensao):
        caminho = self.arquivo(conteudo, extensao)
        formato = baia.inspecionar_arquivo(caminho, extensao)
        relatorio = validar_como_o_worker(str(caminho), "." + formato, LIMITES)
        return caminho, relatorio, baia.ler_liberado(caminho, formato, baia.teto_bytes(LIMITES))


# =============================================================================
# 1. Assinatura binária e extensão
# =============================================================================
class AssinaturaTests(_Pasta):
    def test_executaveis_scripts_e_formatos_estranhos_sao_recusados(self):
        casos = {
            "executavel_pe": executavel_pe(),
            "executavel_elf": b"\x7fELF\x02\x01\x01" + bytes(64),
            "executavel_macho": b"\xcf\xfa\xed\xfe" + bytes(64),
            "executavel_wasm": b"\x00asm\x01\x00\x00\x00",
            "script": b"#!/bin/sh\nrm -rf /tmp/x\n",
            "marcacao": b"  <!DOCTYPE html><html><script>alert(1)</script></html>",
            "pdf": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
            "rtf": b"{\\rtf1\\ansi{\\object\\objemb}}",
            "imagem": b"\x89PNG\r\n\x1a\n" + bytes(32),
            "compactado": b"\x1f\x8b\x08\x00" + bytes(32),
            "midia": b"RIFF\x24\x00\x00\x00WAVEfmt ",
            "binario": bytes(range(256)) * 8,
            "documento_ole": documento_ole("WordDocument"),
        }
        for motivo, conteudo in casos.items():
            for extensao in ("csv", "xlsx", "txt"):
                with self.subTest(motivo=motivo, extensao=extensao):
                    self.recusa(conteudo, extensao, motivo)
        with self.subTest("lote do Windows"):
            self.recusa(b"@echo off\r\ndel /q *.*\r\n", "csv", "script")
        with self.subTest("planilha XML do Excel 2003 e SVG"):
            self.recusa(b'\xef\xbb\xbf<?xml version="1.0"?><Workbook/>', "csv", "marcacao")
            self.recusa(b"<svg onload=alert(1)>", "txt", "marcacao")

    def test_disfarce_de_extensao_e_recusado_com_orientacao(self):
        xlsx = planilha([["Produto", "Qtde"], ["Cafe", 1]])
        erro = self.recusa(VENDAS, "xlsx", "disfarce_texto_como_xlsx")
        self.assertIn(baia.ORIENTACAO_EXTENSAO, erro.safe_message)
        self.recusa(xlsx, "csv", "disfarce_zip_como_csv")
        self.recusa(xlsx, "txt", "disfarce_zip_como_txt")
        self.recusa(xlsx, "xls", "disfarce_zip_como_xls")
        self.recusa(VENDAS, "xls", "disfarce_texto_como_xls")
        self.recusa(VENDAS, "exe", "extensao_nao_aceita")
        self.recusa(VENDAS, "", "extensao_nao_aceita")

    def test_xls_verdadeiro_e_reconhecido_e_recusado_com_orientacao(self):
        for fluxo in ("Workbook", "Book"):
            with self.subTest(fluxo=fluxo):
                erro = self.recusa(documento_ole(fluxo), "xls", "xls_binario")
                self.assertEqual(erro.safe_message, f"{MENSAGEM_PADRAO} {baia.ORIENTACAO_XLS}")
                self.recusa(documento_ole(fluxo), "xlsx", "xls_disfarcado")
                self.recusa(documento_ole(fluxo), "csv", "xls_disfarcado")

    def test_formatos_verdadeiros_passam_e_dizem_como_ler(self):
        xlsx = planilha([["Produto", "Qtde"], ["Cafe", 1]])
        utf16 = "Produto\tQtde\tValor total\r\nCafé\t2\t10,00\r\n".encode("utf-16")
        casos = [(VENDAS, "csv", "csv"), (VENDAS, "txt", "csv"), (xlsx, "xlsx", "xlsx"),
                 (utf16, "csv", "csv"),  # "Texto Unicode" do Excel: o leitor do ETL já o aceita
                 (b"\xef\xbb\xbf" + VENDAS, "csv", "csv"),
                 (b"MZ01;Produto;Qtde;Valor\nX;Cafe;1;2\n", "csv", "csv"),  # texto que começa com MZ
                 (b"Produto;Qtde\r\nCafe;1\x1a", "csv", "csv")]  # fim de arquivo do DOS
        for conteudo, extensao, formato in casos:
            with self.subTest(extensao=extensao, inicio=conteudo[:6]):
                self.assertEqual(baia.inspecionar_arquivo(self.arquivo(conteudo, extensao), extensao), formato)

    def test_rotulo_numera_o_arquivo_sem_mudar_o_motivo(self):
        caminho = self.arquivo(executavel_pe(), "csv")
        with self.assertRaises(SecurityGatekeeperError) as erro:
            baia.inspecionar_arquivo(caminho, "csv", rotulo="Arquivo 3")
        self.assertEqual(erro.exception.safe_message, "Arquivo 3: arquivo fora do padrão aceito para auditoria.")
        self.assertEqual(erro.exception.motivo, "executavel_pe")

    def test_erro_da_baia_e_um_erro_de_entrada_do_etl(self):
        erro = SecurityGatekeeperError("binario")
        self.assertIsInstance(erro, ETLInputError)
        self.assertEqual(str(erro), MENSAGEM_PADRAO)
        self.assertEqual(baia.motivo_de_quarentena(ETLInputError("quarentena:binario", MENSAGEM_PADRAO)), "binario")
        self.assertIsNone(baia.motivo_de_quarentena(ETLInputError("csv_invalid", "x")))


# =============================================================================
# 2. Neutralização de fórmula, sem mudar número
# =============================================================================
class NeutralizacaoTests(unittest.TestCase):
    NUMEROS = ("-12,50", "-1.234,56", "- 1.234,56", "+5", "+5%", "-10%", "-1 UN", "-0,5 kg", "-R$ 3,00",
               "-1,5E+03", "+2e-3", "-", "--", "+", "-(10,00)", "10,00-", "-10,00-", "-12.125", "-0",
               "   -3", "R$ -7,00", "(12,00)", "1.000.000,00", "12.5", "0,125")
    TEXTOS = ("Cafe 500g", " Cafe", "Produto", "PROMOÇÃO", "+Sabor Morango", "-Agua mineral", "Leite (1L)")
    CARGAS = (CARGA_DDE, "+cmd|' /C calc'!A0", "-2+3+cmd|' /C calc'!A0", "@SUM(1+1)*cmd|' /C calc'!A0",
              '=HYPERLINK("http://exemplo.invalid/?d="&A1,"ver")', "-SOMA(A1:A9)", '+WEBSERVICE("http://x")',
              "=1+1", "@usuario", "=== TOTAL ===", "+A1*2", "-E3*2")
    # Quantidades com sinal e uma marca ao lado: ganham o apóstrofo, e o ETL precisa ler o mesmo sinal.
    SINAIS_COM_MARCA = ("-2 un(s)", "-5 PCT(S)", "-2 PC(S)", "-1,000 KG(S)", "-2 KG/UN", "-1,5 kg/un", "-3*",
                        "-2 [DEV]", "-1 und(s)", "-2 UN;", "-1 CX(12)", "-0,5 KG*", "-1 [UN]", "-5 {kg}", "-3!",
                        "-1 UN/CX", "--5", "+2 un(s)")

    def test_cargas_de_formula_ganham_o_apostrofo(self):
        for valor in self.CARGAS:
            with self.subTest(valor=valor):
                self.assertEqual(baia.neutralizar_celula(valor), "'" + valor)

    def test_tab_cr_e_lf_iniciais_viram_espaco_e_a_formula_depois_e_neutralizada(self):
        casos = {"\t=1+1": "' =1+1", "\r=cmd|x!A1": "' =cmd|x!A1", "\n@x": "' @x", "\t-5": " -5",
                 "\r\nProduto": "  Produto", " \t+cmd|x!A": "'  +cmd|x!A", "\t": " ", "\tNA": " NA"}
        for valor, esperado in casos.items():
            with self.subTest(valor=valor):
                self.assertEqual(baia.neutralizar_celula(valor), esperado)

    def test_resto_de_bom_e_separador_interno(self):
        """Uma planilha que reconheça o BOM ao abrir, ou que separe o arquivo por outro
        separador, vê uma célula começando depois dele: a mesma regra vale ali."""
        casos = {"ï»¿=cmd|x!A1": "ï»¿'=cmd|x!A1", "﻿@x": "﻿'@x", "ÿþ=1+1": "ÿþ'=1+1",
                 "ï»¿Produto": "ï»¿Produto", "ok;=cmd|x!A1": "ok;'=cmd|x!A1", "ok, @x": "ok, '@x",
                 # Cada trecho vai até o próximo separador do mesmo tipo: separado por ";", o trecho
                 # é "+cmd|x!A1" (DDE); separado por "|", é "+cmd" (inofensivo).
                 "a;+cmd|x!A1": "a;'+cmd|x!A1", "a|+cmd|x!A1": "a|+cmd|x!A1", "a\t-SOMA(A1)": "a\t'-SOMA(A1)",
                 "Leite 1L, -2 un(s)": "Leite 1L, '-2 un(s)", "=a;=b": "'=a;'=b",
                 "Arroz 5kg, -10% OFF": "Arroz 5kg, -10% OFF", "1,-5": "1,-5", "10;-5": "10;-5",
                 "a@b.com;c@d.com": "a@b.com;c@d.com", "Café, 500g": "Café, 500g"}
        for valor, esperado in casos.items():
            with self.subTest(valor=valor):
                self.assertEqual(baia.neutralizar_celula(valor), esperado)
                self.assertEqual(baia.neutralizar_celula(esperado), esperado)

    def test_numeros_e_nomes_comuns_ficam_como_estao(self):
        for valor in self.NUMEROS + self.TEXTOS:
            with self.subTest(valor=valor):
                self.assertEqual(baia.neutralizar_celula(valor), valor)
        for valor in (None, 12, 12.5, float("nan"), True, ""):
            with self.subTest(valor=valor):
                self.assertIs(baia.neutralizar_celula(valor), valor)

    def test_neutralizacao_nao_muda_nenhum_numero_nem_chave_de_produto_do_etl(self):
        from etl_processor import _normalizar_produto, _parse_valor, converter_valor_monetario
        valores = self.NUMEROS + self.TEXTOS + self.CARGAS + self.SINAIS_COM_MARCA + (
            "\t-5", "\r\n12,50", "\t=5", "1,-5", "10;-5", "Leite, -2 un(s)", "2,=5", "ï»¿=5", "﻿-2 un(s)")
        for valor in valores:
            neutro = baia.neutralizar_celula(valor)
            with self.subTest(valor=valor):
                self.assertEqual(converter_valor_monetario(neutro), converter_valor_monetario(valor))
                self.assertEqual(_parse_valor(neutro), _parse_valor(valor))
                self.assertEqual(_normalizar_produto(neutro), _normalizar_produto(valor))
        # A lista tem de fato valores com sinal que ganham o apóstrofo: o teste não passa por vazio.
        self.assertTrue(all(baia.neutralizar_celula(v).startswith("'") for v in self.SINAIS_COM_MARCA[:-2]))
        self.assertEqual(_parse_valor(baia.neutralizar_celula("-2 un(s)")), -2.0)

    def test_e_idempotente(self):
        for valor in self.NUMEROS + self.TEXTOS + self.CARGAS + ("\t=1", "\r-5", "\t", "ï»¿=1", "a;=b;+cmd|x!A1"):
            with self.subTest(valor=valor):
                uma = baia.neutralizar_celula(valor)
                self.assertEqual(baia.neutralizar_celula(uma), uma)


# =============================================================================
# 3 e 5. Triagem no processo isolado: conteúdo ativo, cópia só com valores e estrutura
# =============================================================================
class TriagemTests(_Pasta):
    def test_csv_limpo_e_liberado_sem_copia(self):
        _, relatorio, liberado = self.triar(VENDAS, "csv")
        self.assertEqual(relatorio, {"format": "csv", "liberado": "original", "achados": [],
                                     "celulas_neutralizadas": 0, "linhas": 3, "colunas": 3})
        self.assertIsNone(liberado)

    def test_csv_com_formula_vira_copia_neutralizada_lida_igual_pelo_motor(self):
        conteudo = ("Produto;Quantidade;Valor total\n" f"{CARGA_DDE};2;20,00\n"
                    "\t=HYPERLINK(\"x\");1;-5,00\n\"Arroz, feijão\";1;3,50\n").encode("utf-8")
        caminho, relatorio, liberado = self.triar(conteudo, "csv")
        self.assertEqual((relatorio["liberado"], relatorio["celulas_neutralizadas"]), ("copia_neutralizada", 2))
        texto = liberado.decode("utf-8-sig")
        self.assertIn("'" + CARGA_DDE, texto)
        self.assertIn("' =HYPERLINK", texto)
        self.assertNotRegex(texto, r"(^|[;\n])[=+@\t]")
        releitura = read_raw(str(caminho.with_name("liberado.csv")), LIMITES)
        self.assertEqual(releitura.values.tolist(), read_raw(str(caminho), LIMITES).values.tolist())

    def test_formula_do_xlsx_sai_e_fica_o_valor(self):
        conteudo = planilha([["Produto", "Qtde", "Valor total"], ["Cafe", 2, 20.5], ["Leite", -1, -5],
                             ["Total", "=SUM(B2:B3)", "=C2+C3"], [CARGA_DDE, 1, 1]], textos_literais=("A5",))
        caminho, relatorio, liberado = self.triar(conteudo, "xlsx")
        self.assertEqual(relatorio["achados"], ["formulas"])
        self.assertEqual(relatorio["celulas_neutralizadas"], 1)
        with zipfile.ZipFile(io.BytesIO(liberado)) as copia:
            folha = copia.read("xl/worksheets/sheet1.xml")
            textos = copia.read("xl/sharedStrings.xml")
        self.assertNotRegex(folha.decode("utf-8"), r"<f[\s>/]")
        self.assertIn(b"'=cmd|", textos)
        self.assertNotIn(b"SUM(", folha + textos)
        original = read_raw(str(caminho), LIMITES)
        self.assertEqual(read_raw(str(caminho.with_name("liberado.xlsx")), LIMITES).values.tolist(),
                         original.values.tolist())

    def test_macros_activex_ole_vinculos_e_conexoes_sao_descartados(self):
        conteudo = com_conteudo_ativo(planilha([["Produto", "Qtde", "Valor total"], ["Cafe", 2, 20.5]]))
        caminho, relatorio, liberado = self.triar(conteudo, "xlsx")
        self.assertEqual(relatorio["liberado"], "copia_neutralizada")
        self.assertTrue({"macro_vba", "activex", "objeto_ole", "vinculo_externo", "conexao_dados",
                         "interface_personalizada"} <= set(relatorio["achados"]), relatorio["achados"])
        with zipfile.ZipFile(io.BytesIO(liberado)) as copia:
            self.assertEqual(sorted(copia.namelist()), ["[Content_Types].xml", "_rels/.rels",
                                                        "xl/_rels/workbook.xml.rels", "xl/sharedStrings.xml",
                                                        "xl/workbook.xml", "xl/worksheets/sheet1.xml"])
            tipos = copia.read("[Content_Types].xml")
        self.assertNotIn(b"macroEnabled", tipos)
        self.assertNotIn(b"vbaProject", liberado)
        self.assertEqual(baia.inventariar_xlsx(caminho.with_name("liberado.xlsx")), [])

    def test_numeros_do_etl_iguais_no_original_e_na_copia(self):
        from etl_processor import _calcular_kpis_local
        vendas = planilha([["Codigo", "Produto", "Qtde", "Valor Total", "Total Custo"],
                           [1, "Cafe 500g", 10, "180,00", 150], [2, "=== PROMO ===", 5, 30.5, None],
                           [3, "Leite 1L", -2, -10, -9], [4, "Total", "=SUM(C2:C4)", "=D2+D3", None]],
                          textos_literais=("B3",))
        caminho, relatorio, _ = self.triar(com_conteudo_ativo(vendas), "xlsx")
        self.assertEqual(relatorio["liberado"], "copia_neutralizada")
        compras = self.arquivo(COMPRAS, "csv")
        with using_limits(LIMITES):
            original = _calcular_kpis_local(str(caminho), str(compras))
            copia = _calcular_kpis_local(str(caminho.with_name("liberado.xlsx")), str(compras))
        self.assertTrue(original.sucesso, original.mensagem)
        self.assertEqual(copia.kpis, original.kpis)
        self.assertEqual(copia.alertas, original.alertas)

    def test_so_libera_o_original_quando_toda_parte_e_de_dados(self):
        """Lista do que é permitido, e não do que é proibido: conteúdo ativo fora dos caminhos de
        costume (onde o pacote o acha pelas relações) também leva à cópia só com valores."""
        caminho, relatorio, liberado = self.triar(xlsx_manual(), "xlsx")
        self.assertEqual((relatorio["liberado"], relatorio["achados"], liberado), ("original", [], None))
        ole = OLE + bytes(300)
        externa = (f'<Relationship Id="rId9" Type="{_PRINCIPAL}/oleObject" Target="file:///C:/x.exe" '
                   'TargetMode="External"/>')
        dde = (b'<?xml version="1.0"?><externalLink xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
               b'<ddeLink ddeService="cmd" ddeTopic="/c calc"/></externalLink>')
        variantes = {
            "aba renomeada, com fórmula": dict(aba=ABA_COM_FORMULA, caminho_aba="xl/planilhas/dados.xml"),
            "aba com outra extensão": dict(aba=ABA_COM_FORMULA, caminho_aba="xl/worksheets/sheet1.dat"),
            "aba em UTF-16 com fórmula": dict(aba=ABA_COM_FORMULA, codificacao="utf-16"),
            "aba no lugar do tema": dict(aba=ABA_COM_FORMULA, caminho_aba="xl/theme/theme1.xml"),
            "aba no lugar dos estilos, em UTF-16": dict(aba=ABA_COM_FORMULA, caminho_aba="xl/styles.xml",
                                                        codificacao="utf-16"),
            "aba no lugar das propriedades": dict(aba=ABA_COM_FORMULA, caminho_aba="docProps/core.xml"),
            "OLE fora de xl/embeddings": dict(partes={"xl/midia/objeto1.dat": ole}),
            "OLE com nome de configuração de impressão": dict(partes={"xl/printerSettings/printerSettings1.bin": ole}),
            "relação externa": dict(relacoes=externa),
            "DDE fora de xl/externalLinks": dict(
                partes={"xl/vinculos/v1.xml": dde},
                relacoes=f'<Relationship Id="rId8" Type="{_PRINCIPAL}/externalLink" Target="vinculos/v1.xml"/>'),
            "nome definido com DDE": dict(nomes="<definedNames><definedName name=\"x\">cmd|'/c calc'!A0"
                                                "</definedName></definedNames>"),
        }
        for caso, opcoes in variantes.items():
            with self.subTest(caso=caso):
                caminho, relatorio, liberado = self.triar(xlsx_manual(**opcoes), "xlsx")
                self.assertEqual(relatorio["liberado"], "copia_neutralizada", relatorio)
                self.assertTrue(relatorio["achados"])
                with zipfile.ZipFile(io.BytesIO(liberado)) as copia:
                    self.assertEqual(len(copia.namelist()), 6)
                    for parte in copia.namelist():
                        self.assertNotIn(b"cmd", copia.read(parte), parte)
                self.assertEqual(baia.inventariar_xlsx(caminho.with_name("liberado.xlsx")), [])
                self.assertEqual(read_raw(str(caminho.with_name("liberado.xlsx")), LIMITES).values.tolist(),
                                 [["Produto", "Qtde", "Valor total"], ["Cafe", "2", "20"], ["Leite", "1", "5"]])

    def test_numeros_iguais_aos_da_leitura_de_antes_da_baia(self):
        """A neutralização da leitura não muda KPI: compara com a leitura sem ela (o read_raw de antes
        da Baia), num relatório com devoluções marcadas por unidade, como alguns PDVs exportam."""
        import etl_inputs
        import etl_processor
        conteudo = ("Produto;Qtde;Preço Unit;Estoque\nCAFE 500G;10 un(s);18,00;5\nCAFE 500G;-2 un(s);18,00;-4 un(s)\n"
                    "LEITE 1L;6 un(s);5,00;3\nACUCAR 1KG;3 pct(s);4,50;1\nACUCAR 1KG;-1 pct(s);4,50;-1*\n"
                    "FEIJAO 1KG;-1,5 kg/un;8,00;2\n").encode("cp1252")
        vendas, compras = self.arquivo(conteudo, "csv"), self.arquivo(COMPRAS, "csv")
        self.assertEqual(read_raw(str(vendas), LIMITES).attrs["celulas_neutralizadas"], 5)

        def antes_da_baia(caminho, limits):
            return etl_inputs._run(caminho, Path(caminho).suffix, limits, True)

        with using_limits(LIMITES):
            atual = etl_processor._calcular_kpis_local(str(vendas), str(compras))
            with patch.object(etl_processor, "read_raw", side_effect=antes_da_baia):
                antes = etl_processor._calcular_kpis_local(str(vendas), str(compras))
        self.assertTrue(atual.sucesso, atual.mensagem)
        self.assertEqual(atual.kpis, antes.kpis)
        self.assertEqual(atual.alertas, antes.alertas)

    def test_titulo_entre_aspas_que_comeca_com_menor_que_continua_aceito(self):
        conteudo = ('"<< RELATÓRIO DE VENDAS POR PRODUTO >>";;\nProduto;Qtde;Valor Total\n'
                    "=== PROMO ===;1;1,00\nCAFE 500G;2;36,00\n").encode("cp1252")
        _, relatorio, liberado = self.triar(conteudo, "csv")
        self.assertEqual(relatorio["liberado"], "copia_neutralizada")
        self.assertTrue(liberado.startswith(b'"<<'))
        self.assertIn(b"'=== PROMO ===", liberado)

    def test_copia_csv_perto_do_teto_cresce_so_pelos_apostrofos(self):
        from dataclasses import replace
        limites = replace(LIMITES, max_file_bytes=200_000)
        linha = "Feijão carioca tipo 1 pacote;2;20,00\n"
        cabecalho = "Produto;Quantidade;Valor total\n=== PROMOÇÃO ===;1;1,00\n"
        conteudo = (cabecalho + linha * ((199_500 - len(cabecalho)) // len(linha))).encode("cp1252")
        self.assertTrue(199_000 < len(conteudo) < 200_000, len(conteudo))
        caminho = self.arquivo(conteudo, "csv")
        relatorio = validar_como_o_worker(str(caminho), ".csv", limites)
        copia = baia.ler_liberado(caminho, "csv", baia.teto_bytes(limites))
        self.assertEqual(relatorio["liberado"], "copia_neutralizada")
        self.assertEqual(len(copia), len(conteudo) + 1)  # só o apóstrofo, na mesma codificação
        self.assertEqual(copia.replace(b"'===", b"===", 1), conteudo)

    def test_bom_que_o_leitor_nao_consome_nao_deixa_formula_na_primeira_celula(self):
        """UTF-8 com BOM e um byte cp1252 (o leitor cai no cp1252) ou UTF-16 com BOM: a cópia
        mantém o BOM, e quem o reconhecer ao abrir vê a primeira célula começando no apóstrofo."""
        corpo = ("=cmd|' /C calc'!A0\nProduto;Quantidade;Valor total\nCafé 500g;2;20,00\nLeite 1L;1;5,00\n")
        casos = {"utf-8 + cp1252": (b"\xef\xbb\xbf" + corpo.encode("cp1252"), b"\xef\xbb\xbf'="),
                 "utf-16": (corpo.replace(";", "\t").encode("utf-16"), b"\xff\xfe'=")}
        for caso, (conteudo, inicio) in casos.items():
            with self.subTest(caso=caso):
                _, relatorio, liberado = self.triar(conteudo, "csv")
                self.assertEqual(relatorio["liberado"], "copia_neutralizada")
                self.assertTrue(liberado.startswith(inicio), liberado[:12])

    def test_formula_depois_de_um_separador_interno(self):
        conteudo = b"Produto,Quantidade,Valor total,Obs\nArroz 5kg,2,20.00,ok;=cmd|' /C calc'!A0\nLeite 1L,1,5.00,ok\n"
        caminho, relatorio, liberado = self.triar(conteudo, "csv")
        self.assertEqual((relatorio["liberado"], relatorio["celulas_neutralizadas"]), ("copia_neutralizada", 1))
        self.assertIn(b"ok;'=cmd|", liberado)
        for parte in liberado.decode("utf-8").replace("\n", ",").replace(";", ",").split(","):
            self.assertFalse(parte.lstrip().startswith(("=", "@")), parte)

    def test_copia_xlsx_compacta_com_numeros_numericos(self):
        conteudo = planilha([["Produto", "Qtde", "Valor total"], ["Cafe", 2, 20.5], ["Leite", 12.125, "=B3*2"],
                             ["Acucar", "007", "12.125"]])
        caminho, relatorio, liberado = self.triar(conteudo, "xlsx")
        self.assertEqual(relatorio["achados"], ["formulas"])
        with zipfile.ZipFile(io.BytesIO(liberado)) as copia:
            folha = copia.read("xl/worksheets/sheet1.xml").decode("utf-8")
            textos = copia.read("xl/sharedStrings.xml").decode("utf-8")
        self.assertIn("<c><v>20.5</v></c>", folha)  # número que o motor lê igual: célula numérica
        self.assertNotIn('<row r="', folha)
        for texto in ("007", "12.1250", "Cafe"):  # zero à esquerda, milhar e texto ficam como texto
            self.assertIn(f">{texto}<", textos)
        self.assertEqual(read_raw(str(caminho.with_name("liberado.xlsx")), LIMITES).values.tolist(),
                         read_raw(str(caminho), LIMITES).values.tolist())

    def test_copia_acima_do_teto_tem_orientacao_de_tamanho(self):
        from dataclasses import replace
        linhas = "".join(f"=PROMO {n};1;1,00\n" for n in range(40))
        conteudo = ("Produto;Quantidade;Valor total\n" + linhas).encode("cp1252")
        limites = replace(LIMITES, max_file_bytes=len(conteudo) + 5)  # 40 apóstrofos não cabem
        caminho = self.arquivo(conteudo, "csv")
        with self.assertRaises(ETLInputError) as erro:
            validar_como_o_worker(str(caminho), ".csv", limites)
        self.assertEqual(erro.exception.code, "quarentena:copia_acima_do_teto")
        self.assertEqual(erro.exception.safe_message, f"{MENSAGEM_PADRAO} {baia.ORIENTACAO_TAMANHO}")
        self.assertFalse(caminho.with_name("liberado.csv").exists())

    def test_estrutura_minima_de_relatorio_de_pdv(self):
        for conteudo in (b"a;b\n1;2\n", b"Nome;Telefone\nFulano;11999999999\n",
                         b"Produto;Observacao\nCafe;bom\n"):
            with self.subTest(conteudo=conteudo[:20]):
                caminho = self.arquivo(conteudo, "csv")
                with self.assertRaises(ETLInputError) as erro:
                    validar_como_o_worker(str(caminho), ".csv", LIMITES)
                self.assertEqual(erro.exception.code, "quarentena:estrutura_ausente")
                self.assertEqual(erro.exception.safe_message, f"{MENSAGEM_PADRAO} {baia.ORIENTACAO_ESTRUTURA}")
        for conteudo in (VENDAS, COMPRAS, b"Produto;Valor total\nCafe;10,00\n", b"Produto;Qtde\nCafe;1\n",
                         b"Relatorio\nPeriodo: 01/01/2026\n\nDescricao;Preco unitario\nCafe;5,00\n"):
            with self.subTest(conteudo=conteudo[:20]):
                self.assertEqual(validar_como_o_worker(str(self.arquivo(conteudo, "csv")), ".csv",
                                                       LIMITES)["liberado"], "original")

    def test_copia_que_o_motor_nao_reproduz_e_recusada_sem_deixar_arquivo(self):
        conteudo = planilha([["Produto", "Qtde", "Valor total"], ["Cafe", 2, "=B2*10"]])
        caminho = self.arquivo(conteudo, "xlsx")
        with patch.object(baia, "_mesma_matriz", return_value=False):
            with self.assertRaises(SecurityGatekeeperError) as erro:
                baia.triar(caminho, ".xlsx", LIMITES)
        self.assertEqual(erro.exception.motivo, "neutralizacao_inviavel")
        self.assertIn(baia.ORIENTACAO_NEUTRALIZAR, erro.exception.safe_message)
        self.assertFalse(caminho.with_name("liberado.xlsx").exists())

    def test_nao_sobrescreve_uma_copia_existente_nem_le_formato_trocado(self):
        caminho = self.arquivo(VENDAS, "csv")
        caminho.with_name("liberado.csv").write_bytes(b"x")
        with self.assertRaises(SecurityGatekeeperError) as erro:
            baia.triar(caminho, ".csv", LIMITES)
        self.assertEqual(erro.exception.motivo, "liberado_existente")
        with self.assertRaises(SecurityGatekeeperError) as erro:
            baia.triar(self.arquivo(VENDAS, "csv"), ".xlsx", LIMITES)
        self.assertEqual(erro.exception.motivo, "formato_de_leitura")

    def test_copia_liberada_passa_de_novo_pelo_teto_e_pela_assinatura(self):
        caminho = self.arquivo(VENDAS, "csv")
        copia = caminho.with_name("liberado.csv")
        copia.write_bytes(executavel_pe())
        with self.assertRaises(SecurityGatekeeperError):
            baia.ler_liberado(caminho, "csv", 10_000)
        copia.write_bytes(VENDAS)
        with self.assertRaises(SecurityGatekeeperError) as erro:
            baia.ler_liberado(caminho, "csv", len(VENDAS) - 1)
        self.assertEqual(erro.exception.motivo, "liberado_invalido")
        self.assertEqual(baia.ler_liberado(caminho, "csv", len(VENDAS)), VENDAS)

    def test_toda_leitura_do_motor_sai_neutralizada(self):
        caminho = self.arquivo(("Produto;Quantidade;Valor total\n" f"{CARGA_DDE};-2;-20,00\n").encode(), "csv")
        matriz = read_raw(str(caminho), LIMITES)
        self.assertEqual(matriz.iat[1, 0], "'" + CARGA_DDE)
        self.assertEqual((matriz.iat[1, 1], matriz.iat[1, 2]), ("-2", "-20,00"))
        self.assertEqual(matriz.attrs["celulas_neutralizadas"], 1)
        self.assertEqual(matriz.attrs["separator"], ";")


# =============================================================================
# 4. Teto de 50 MiB e leitura em blocos
# =============================================================================
class TetoTests(unittest.TestCase):
    def test_teto_e_o_menor_entre_o_etl_e_cinquenta_mib(self):
        self.assertEqual(baia.TETO_ABSOLUTO_BYTES, 50 * 1024 * 1024)
        self.assertEqual(baia.teto_bytes(ETLLimits()), 20 * 1024 * 1024)
        self.assertEqual(baia.teto_bytes(SimpleNamespace(max_file_bytes=200 * 1024 * 1024)), 50 * 1024 * 1024)

    def test_upload_acima_do_teto_absoluto_para_de_ler_no_teto_mais_um(self):
        import etl_uploads

        class Fluxo(io.RawIOBase):
            lidos = 0

            def read(self, tamanho=-1):
                if not 0 < tamanho <= 64 * 1024:
                    raise AssertionError("Leitura sem limite")
                Fluxo.lidos += tamanho
                return b"a" * tamanho

        with patch.object(baia, "TETO_ABSOLUTO_BYTES", 1000), patch.object(etl_uploads, "run_validation") as validar:
            with self.assertRaises(ETLInputError) as erro:
                etl_uploads.ler_upload_validado(Fluxo(), "csv", SimpleNamespace(max_file_bytes=10 ** 9))
        self.assertEqual(erro.exception.code, "file_size")
        self.assertEqual(Fluxo.lidos, 1001)
        validar.assert_not_called()


# =============================================================================
# 6. Quarentena: log sem conteúdo e envio pelo portal
# =============================================================================
class QuarentenaLogTests(unittest.TestCase):
    def setUp(self):
        # As migrações dos outros testes chamam fileConfig() do Alembic, que desliga os loggers.
        patcher = patch.object(baia.logger, "disabled", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_log_tem_motivo_tamanho_e_hash_e_nunca_o_conteudo(self):
        erro = SecurityGatekeeperError("executavel_pe")
        with self.assertLogs("security_gatekeeper", logging.WARNING) as registro:
            baia.registrar_recusa(erro, sha256="ab" * 32, tamanho=4096, extensao="csv", origem="upload")
        linha = registro.output[0]
        for trecho in ("quarentena:executavel_pe", "bytes=4096", "sha256=" + "ab" * 32, "origem=upload"):
            self.assertIn(trecho, linha)

    def test_codigo_vindo_de_fora_e_saneado_e_fila_ocupada_nao_e_registrada(self):
        with self.assertLogs("security_gatekeeper", logging.WARNING) as registro:
            baia.registrar_recusa(ETLInputError("x\n<script>FALSO", "m"), sha256="0", tamanho=1,
                                  extensao="c\nsv", origem="upload")
        self.assertNotIn("\n", registro.output[0].split(":", 2)[2])
        self.assertNotIn("<script>", registro.output[0])
        with self.assertNoLogs("security_gatekeeper", logging.WARNING):
            baia.registrar_recusa(ETLInputError("busy", "m"), sha256="0", tamanho=1, extensao="csv",
                                  origem="upload")


class BaiaNoPortalTests(DeployTestCase):
    """Os envios do cliente e do consultor passam pela Baia antes do armazenamento."""

    def setUp(self):
        super().setUp()
        import etl_uploads
        self.stack.enter_context(patch.object(etl_uploads, "run_validation", side_effect=validar_como_o_worker))
        self.stack.enter_context(patch.object(baia.logger, "disabled", False))  # fileConfig() das migrações
        self.fixtures()
        self.login()
        self.admin_web = self.app.test_client()
        resposta = self.admin_web.post("/auth/login", data={
            "email": "admin-test@example.invalid", "senha": self.password,
            "csrf_token": self._csrf(self.admin_web, "/auth/login")})
        self.assertEqual(resposta.status_code, 302)

    def _csrf(self, cliente, caminho):
        corpo = cliente.get(caminho).get_data(as_text=True)
        return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', corpo)[1]

    def _enviar(self, conteudo, nome, tipo="VENDAS", *, admin=False):
        cliente = self.admin_web if admin else self.client
        caminho = f"/admin/analise/{self.analise_id}/upload" if admin else "/cliente/upload"
        dados = {"id_analise": str(self.analise_id), "tipo_relatorio": tipo, "csrf_token": self._csrf(cliente, caminho),
                 "arquivo": (io.BytesIO(conteudo), nome)}
        if tipo == "VENDAS":
            dados["valor_pdv_informado"] = "150.000,00"
        resposta = cliente.post(caminho, data=dados, content_type="multipart/form-data")
        self.assertEqual(resposta.status_code, 302)
        return cliente.get(resposta.headers["Location"]).get_data(as_text=True)

    def _gravado(self, tipo):
        from models import UploadRelatorio
        import storage
        self.db.session.expire_all()
        upload = self.db.session.scalar(select(UploadRelatorio).where(
            UploadRelatorio.id_analise == self.analise_id, UploadRelatorio.tipo_relatorio == tipo))
        if upload is None:
            return None, None
        return upload, storage.obter(upload.caminho_arquivo[3:]).conteudo

    def test_cliente_e_consultor_gravam_so_a_copia_neutralizada(self):
        csv_formula = ("Produto;Quantidade;Valor total\n" f"{CARGA_DDE};2;20,00\n").encode("utf-8")
        xlsx_macro = com_conteudo_ativo(planilha([["Fornecedor", "Valor NF"], ["Fornecedor ficticio", 40]]))
        for admin in (False, True):
            with self.subTest(admin=admin):
                self._enviar(csv_formula, "vendas.csv", admin=admin)
                upload, gravado = self._gravado("VENDAS")
                # A linha é a desta volta: o envio do consultor troca a do cliente.
                self.assertEqual(upload.id_usuario_admin, self.admin_id if admin else None)
                self.assertIn(b"'" + CARGA_DDE.encode(), gravado)
                self.assertEqual(upload.hash_arquivo, hashlib.sha256(gravado).hexdigest())
                self.assertEqual(upload.tamanho_arquivo, len(gravado))
                self.assertEqual(upload.nome_arquivo_original, "vendas.csv")
                self._enviar(xlsx_macro, "compras.xlsx", "COMPRAS", admin=admin)
                upload, gravado = self._gravado("COMPRAS")
                self.assertEqual(upload.id_usuario_admin, self.admin_id if admin else None)
                self.assertNotIn(b"vbaProject", gravado)
                with zipfile.ZipFile(io.BytesIO(gravado)) as copia:
                    self.assertNotIn("xl/vbaProject.bin", copia.namelist())
                self.assertEqual(upload.extensao_arquivo, "XLSX")

    def test_arquivo_limpo_e_gravado_byte_a_byte(self):
        self._enviar(VENDAS, "vendas.csv")
        upload, gravado = self._gravado("VENDAS")
        self.assertEqual(gravado, VENDAS)
        self.assertEqual(upload.hash_arquivo, hashlib.sha256(VENDAS).hexdigest())

    def test_executavel_e_html_disfarcados_nao_chegam_ao_banco(self):
        for conteudo, nome in ((executavel_pe(), "vendas.csv"), (b"<html><body>x</body></html>", "vendas.xlsx"),
                               (b"#!/bin/sh\necho x\n", "vendas.csv")):
            for admin in (False, True):
                with self.subTest(nome=nome, admin=admin):
                    with self.assertLogs("security_gatekeeper", logging.WARNING) as registro:
                        html = self._enviar(conteudo, "segredo-do-cliente-" + nome, admin=admin)
                    self.assertIn(MENSAGEM_PADRAO, html)
                    self.assertEqual(self._gravado("VENDAS"), (None, None))
                    self.assertIn("sha256=" + hashlib.sha256(conteudo).hexdigest(), registro.output[0])
                    self.assertNotIn("segredo-do-cliente", " ".join(registro.output))


class BaiaNoRaioXTests(DeployTestCase):
    def test_raio_x_recusa_o_arquivo_disfarcado_antes_do_processo_isolado(self):
        import raio_x_servico
        self.stack.enter_context(patch.object(baia.logger, "disabled", False))  # fileConfig() das migrações
        arquivos = [FileStorage(stream=io.BytesIO(VENDAS), filename="jan.csv"),
                    FileStorage(stream=io.BytesIO(executavel_pe()), filename="fev.csv")]
        with patch("etl_runtime.run_raio_x") as processo, \
                self.assertLogs("security_gatekeeper", logging.WARNING):
            with self.assertRaises(SecurityGatekeeperError) as erro:
                raio_x_servico.processar_envio(arquivos, LIMITES)
        processo.assert_not_called()
        self.assertEqual(erro.exception.safe_message, "Arquivo 2: arquivo fora do padrão aceito para auditoria.")


class WorkerUsaABaiaTests(unittest.TestCase):
    def test_operacao_validate_do_worker_e_a_triagem_e_so_ela_grava_o_tamanho_do_arquivo(self):
        fonte = (Path(__file__).resolve().parents[1] / "etl_worker.py").read_text(encoding="utf-8")
        self.assertIn("from security_gatekeeper import triar", fonte)
        self.assertRegex(fonte, r'if request\.get\("operation"\) == "validate":\n\s+escrita = max\(')
        self.assertIn("resource.RLIMIT_FSIZE, (escrita,) * 2", fonte)

    @unittest.skipUnless(hasattr(__import__("os"), "fork"), "O worker real do ETL exige POSIX")
    def test_worker_real_devolve_a_triagem_e_a_copia(self):
        import etl_runtime
        with tempfile.TemporaryDirectory(prefix="nexo-baia-worker-") as pasta:
            caminho = Path(pasta) / "arquivo.csv"
            caminho.write_bytes(("Produto;Quantidade;Valor total\n" f"{CARGA_DDE};2;20,00\n").encode())
            relatorio = etl_runtime.run_validation(str(caminho), ".csv")
            self.assertEqual(relatorio["liberado"], "copia_neutralizada")
            self.assertIn(b"'=cmd|", (Path(pasta) / "liberado.csv").read_bytes())


if __name__ == "__main__":
    unittest.main()
