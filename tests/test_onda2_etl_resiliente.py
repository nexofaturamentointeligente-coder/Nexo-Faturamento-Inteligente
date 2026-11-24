"""Onda 2: ETL resiliente e proteção de dados.

  1. Motor: cada arquivo do cálculo mensal é lido uma vez (o período sai da mesma matriz) e as
     normalizações textuais puras têm cache limitado (8.192 entradas, textos de até 128 caracteres).
  2. Processo isolado: result.json gravado de uma vez (parcial nunca vale) e falha do servidor (o processo não
     entregou resultado: morto antes de gravar, encerrado com erro, disco, motor que não carrega) separada do erro do
     arquivo e da saída inválida (resultado ilegível ou fora do protocolo continua recusa "worker"); no
     reprocessamento do admin ela não marca ERRO nem troca o último resultado.
  3. Entrada: planilha só com cabeçalho ou com cabeçalho e total é recusada na triagem (no processo
     isolado), e disco cheio ao receber o envio responde 503 com Retry-After.
  4. Sala de espera da vaga única: por ordem de chegada, até 4 esperando (1 por empresa), com prazo;
     quem espera não segura conexão do banco; a recusa não desconta a cota de envios.
"""
from dataclasses import asdict
from datetime import date
import errno
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flask import request

from tests.deploy_support import DeployTestCase, ROOT
from tests.test_trava_pdv import TravaPdvCase
from . import test_auth_tokens_security as token_cases
from . import test_session_revocation_security as session_cases

VENDAS_COM_PERIODO = ("Relatorio de Vendas\nPeriodo: 01/10/2025 a 31/10/2025\nCodigo;Produto;Qtde;Valor Total\n"
                      "1;CAFE 500G;10;100,00\n2;ACUCAR 1KG;5;25,50\n3;CAFE 500G;2;20,00\n")
VENDAS_SEM_PERIODO = "Codigo;Produto;Qtde;Valor Total\n1;CAFE 500G;10;100,00\n2;ACUCAR 1KG;5;25,50\n"
COMPRAS_COM_PERIODO = ("Relatorio de Compras\nPeriodo: 01/09/2025 a 30/09/2025\n"
                       "Fornecedor;Nota Fiscal;Data Entrada;Valor Total\nALFA;123;05/09/2025;500,00\n")
COMPRAS_SEM_PERIODO = "Fornecedor;Nota Fiscal;Data Entrada;Valor Total\nALFA;123;05/09/2025;500,00\n"


def _pasta(caso):
    trabalho = tempfile.TemporaryDirectory(prefix="nexo-onda2-")
    caso.addCleanup(trabalho.cleanup)
    return Path(trabalho.name)


# =============================================================================
# 1. Motor
# =============================================================================
class CacheTextualTests(unittest.TestCase):
    AMOSTRAS = ["Café Pilão 500g", "AÇÚCAR  REFINADO 1KG", "  produto—especial  ", "x" * 128, "y" * 129, "",
                "0001234", "ÇÃO", "﻿BOM\xa0NBSP\tTAB", "Ñandú", "nan"]
    NAO_TEXTO = [None, 12.0, 7, float("nan")]

    def funcoes(self):
        import etl_processor as motor
        return (motor._strip_acentos, motor._norm_chave, motor._normalizar_produto,
                motor.normalizar_codigo_do_produto)

    def test_mesma_saida_do_calculo_sem_cache_e_so_textos_curtos_entram(self):
        for funcao in self.funcoes():
            with self.subTest(funcao=funcao.__name__):
                self.assertEqual(funcao.cache_info().maxsize, 8192)
                funcao.cache_clear()
                for valor in self.AMOSTRAS + self.NAO_TEXTO:
                    esperado = funcao.sem_cache(valor)
                    self.assertEqual(funcao(valor), esperado)
                    self.assertEqual(funcao(valor), esperado)  # a segunda vem do cache, se couber nele
                curtos = {valor for valor in self.AMOSTRAS if len(valor) <= 128}
                self.assertEqual(funcao.cache_info().currsize, len(curtos))

    def test_subclasse_de_str_nao_entra_no_cache(self):
        class Texto(str):
            pass
        import etl_processor as motor
        motor._normalizar_produto.cache_clear()
        self.assertEqual(motor._normalizar_produto(Texto("Café")), "CAFE")
        self.assertEqual(motor._normalizar_produto.cache_info().currsize, 0)

    def test_memoria_limitada_mesmo_com_um_texto_diferente_em_cada_celula(self):
        import etl_processor as motor
        motor._normalizar_produto.cache_clear()
        for indice in range(9000):
            motor._normalizar_produto(f"produto {indice}")
        self.assertEqual(motor._normalizar_produto.cache_info().currsize, 8192)
        motor._normalizar_produto.cache_clear()


class LeituraUnicaTests(unittest.TestCase):
    def _csv(self, pasta, nome, texto):
        caminho = pasta / nome
        caminho.write_text(texto, encoding="utf-8")
        return str(caminho)

    def _calcular(self, vendas, compras):
        import etl_processor as motor
        pasta = _pasta(self)
        caminhos = (self._csv(pasta, "vendas.csv", vendas), self._csv(pasta, "compras.csv", compras))
        with patch.object(motor, "_ler_bruto", side_effect=motor._ler_bruto) as leitura:
            resultado = motor._calcular_kpis_local(*caminhos)
        return resultado.kpis, [chamada.args[0] for chamada in leitura.call_args_list], caminhos

    def test_cada_arquivo_e_lido_uma_vez_e_o_periodo_sai_da_mesma_matriz(self):
        kpis, lidos, caminhos = self._calcular(VENDAS_COM_PERIODO, COMPRAS_COM_PERIODO)
        self.assertEqual(lidos, list(caminhos))
        self.assertEqual((kpis["periodo_base_inicio"], kpis["periodo_base_fim"]), (date(2025, 10, 1), date(2025, 10, 31)))

    def test_sem_periodo_nas_vendas_vale_o_das_compras(self):
        kpis, lidos, caminhos = self._calcular(VENDAS_SEM_PERIODO, COMPRAS_COM_PERIODO)
        self.assertEqual(lidos, list(caminhos))
        self.assertEqual((kpis["periodo_base_inicio"], kpis["periodo_base_fim"]), (date(2025, 9, 1), date(2025, 9, 30)))

    def test_sem_periodo_em_nenhum_dos_dois(self):
        kpis, _, _ = self._calcular(VENDAS_SEM_PERIODO, COMPRAS_SEM_PERIODO)
        self.assertEqual((kpis["periodo_base_inicio"], kpis["periodo_base_fim"]), (None, None))


# =============================================================================
# 2. Processo isolado: gravação atômica e falha do servidor
# =============================================================================
class GravacaoAtomicaTests(unittest.TestCase):
    def test_resultado_gravado_de_uma_vez_sem_sobra_do_parcial(self):
        import etl_worker
        saida = _pasta(self) / "result.json"
        etl_worker._gravar_resultado(saida, b'{"ok": true, "result": {}}')
        self.assertEqual(saida.read_bytes(), b'{"ok": true, "result": {}}')
        self.assertFalse(saida.with_name("result.json.parcial").exists())

    def test_gravacao_interrompida_nunca_deixa_um_result_json(self):
        import etl_worker
        saida = _pasta(self) / "result.json"
        real = Path.write_bytes

        def disco_cheio_no_meio(caminho, dados):
            real(caminho, dados[: len(dados) // 2])  # metade gravada, como num disco que encheu
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch.object(Path, "write_bytes", disco_cheio_no_meio), self.assertRaises(OSError):
            etl_worker._gravar_resultado(saida, b'{"ok": true, "result": {"campo": 1}}')
        self.assertFalse(saida.exists())  # só o parcial existe; o processo pai não o lê


class ResultadoDoProcessoIsoladoTests(unittest.TestCase):
    """O processo pai (etl_runtime._run_job) com um processo isolado de teste no lugar do worker."""

    def setUp(self):
        import etl_runtime
        self.runtime = etl_runtime
        varredura = patch.object(etl_runtime, "varrer_temporarios_orfaos", return_value=0)
        varredura.start()
        self.addCleanup(varredura.stop)
        self.pasta = _pasta(self)
        self.entrada = self.pasta / "sintetico.csv"
        self.entrada.write_text("produto;quantidade;faturamento\nSintético;1;10,00\n")

    def _rodar(self, corpo):
        from etl_limits import ETLLimits
        script = self.pasta / "worker_de_teste.py"
        script.write_text("import json, os, sys\nfrom pathlib import Path\noutput = Path(sys.argv[2])\n" + corpo + "\n")
        with patch.object(self.runtime, "_WORKER_PATH", script):
            return self.runtime.run_validation(str(self.entrada), ".csv", ETLLimits(timeout_seconds=30))

    def _assert_falha_do_servidor(self, corpo):
        from etl_erros import ETLInputError, MENSAGEM_INFRAESTRUTURA, eh_falha_de_infraestrutura
        with self.assertRaises(ETLInputError) as falha:
            self._rodar(corpo)
        self.assertEqual(falha.exception.code, "infra")
        self.assertEqual(falha.exception.safe_message, MENSAGEM_INFRAESTRUTURA)
        self.assertTrue(eh_falha_de_infraestrutura(falha.exception))

    def test_processo_morto_sem_resultado(self):
        self._assert_falha_do_servidor("os._exit(9)")

    def test_so_o_parcial_existe_quando_o_processo_morre_antes_da_troca_de_nome(self):
        self._assert_falha_do_servidor(
            "output.with_name(output.name + '.parcial').write_text(json.dumps({'ok': True, 'result': {}}))\n"
            "os._exit(9)")

    def _assert_saida_invalida(self, corpo):
        from etl_erros import ETLInputError, eh_falha_de_infraestrutura
        with self.assertRaises(ETLInputError) as falha:
            self._rodar(corpo)
        self.assertEqual(falha.exception.code, "worker")
        self.assertFalse(eh_falha_de_infraestrutura(falha.exception))

    def test_resultado_ilegivel_ou_fora_do_protocolo_continua_recusa_do_processamento(self):
        # Com a gravação atômica, o result.json só existe inteiro: ilegível não é interrupção.
        for corpo in ("output.write_text('{\"ok\": tr')", "output.write_text(json.dumps({'resultado': {}}))",
                      "output.write_text(json.dumps({'ok': True, 'result': []}))"):
            with self.subTest(corpo=corpo):
                self._assert_saida_invalida(corpo)

    def test_saida_com_erro_mesmo_tendo_gravado_ok(self):
        self._assert_falha_do_servidor("output.write_text(json.dumps({'ok': True, 'result': {}}))\nsys.exit(3)")

    def test_recusa_do_arquivo_continua_erro_do_arquivo(self):
        from etl_erros import ETLInputError, eh_falha_de_infraestrutura
        with self.assertRaises(ETLInputError) as falha:
            self._rodar("output.write_text(json.dumps({'ok': False, 'code': 'data', 'message': 'Sem colunas.'}))")
        self.assertEqual((falha.exception.code, falha.exception.safe_message), ("data", "Sem colunas."))
        self.assertFalse(eh_falha_de_infraestrutura(falha.exception))


class WorkerFalhaDoServidorTests(unittest.TestCase):
    """O etl_worker.py de verdade, num processo à parte, com a triagem trocada por uma falha."""

    def _rodar(self, efeito):
        from etl_limits import ETLLimits
        pasta = _pasta(self)
        entrada = pasta / "entrada.csv"
        entrada.write_text("Produto;Qtde;Valor Total\nCAFE;1;10,00\n", encoding="utf-8")
        pedido, saida = pasta / "request.json", pasta / "result.json"
        pedido.write_text(json.dumps({"operation": "validate", "paths": [str(entrada)], "extension": ".csv",
                                      "limits": asdict(ETLLimits())}))
        envoltorio = pasta / "envoltorio.py"
        envoltorio.write_text(
            "import errno, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import security_gatekeeper\n"
            "def falhar(*argumentos, **nomeados):\n"
            f"    {efeito}\n"
            "security_gatekeeper.triar = falhar\n"
            "import etl_worker\n"
            f"sys.argv = ['etl_worker.py', {str(pedido)!r}, {str(saida)!r}]\n"
            "raise SystemExit(etl_worker.main())\n")
        subprocess.run([sys.executable, "-I", "-B", str(envoltorio)], capture_output=True, timeout=180, check=False)
        self.assertFalse(saida.with_name("result.json.parcial").exists())
        return json.loads(saida.read_text())

    def test_disco_cheio_e_motor_que_nao_carrega_sao_falha_do_servidor(self):
        from etl_erros import MENSAGEM_INFRAESTRUTURA
        for efeito in ("raise OSError(errno.ENOSPC, 'No space left on device')",
                       "raise ImportError('motor indisponível')"):
            with self.subTest(efeito=efeito):
                envelope = self._rodar(efeito)
                self.assertEqual((envelope["ok"], envelope["code"], envelope["message"]),
                                 (False, "infra", MENSAGEM_INFRAESTRUTURA))

    def test_outro_oserror_continua_como_antes(self):
        envelope = self._rodar("raise OSError(errno.EACCES, 'acesso negado')")
        self.assertEqual((envelope["ok"], envelope["code"]), (False, "worker"))


class ReprocessamentoComFalhaDoServidorTests(TravaPdvCase):
    """No reprocessamento do admin, a falha do servidor não marca ERRO nem troca o último resultado."""

    def _estado(self):
        from models import IndicadorAnalise, UploadRelatorio
        from sqlalchemy import select
        self.db.session.expire_all()
        uploads = {u.tipo_relatorio: (u.status_processamento, u.hash_arquivo, u.mensagem_erro)
                   for u in self.db.session.scalars(select(UploadRelatorio).where(
                       UploadRelatorio.id_analise == self.analise_id))}
        indicador = self.db.session.scalar(select(IndicadorAnalise).where(
            IndicadorAnalise.id_analise == self.analise_id))
        valores = None if indicador is None else (indicador.faturamento_total, indicador.total_comprado)
        return self._analise().status_analise, uploads, valores

    def test_falha_do_servidor_preserva_arquivos_e_ultimo_resultado(self):
        from etl_erros import ETLInputError, MENSAGEM_INFRAESTRUTURA
        self._enviar_e_processar("1.500,00")
        antes = self._estado()
        self.assertTrue(all(status == "PROCESSADO" for status, _, _ in antes[1].values()))
        self.assertIsNotNone(antes[2])
        for codigo in ("infra", "timeout"):
            with self.subTest(codigo=codigo):
                with patch("etl_regras.calcular_kpis_em_memoria",
                           side_effect=ETLInputError(codigo, MENSAGEM_INFRAESTRUTURA)):
                    resposta = self._processar()
                self.assertEqual(resposta.status_code, 302)
                self.assertEqual(self._estado(), antes)
                self.assertTrue(any("Falha temporária no processamento" in m for m in self._flashes(self.adm)))

    def test_erro_do_arquivo_continua_marcando_erro(self):
        from etl_erros import ETLInputError
        self._enviar_e_processar("1.500,00")
        antes = self._estado()
        with patch("etl_regras.calcular_kpis_em_memoria",
                   side_effect=ETLInputError("data", "Não foi possível identificar os dados essenciais.")):
            self._processar()
        status, uploads, valores = self._estado()
        self.assertEqual(status, "RELATORIO_RECEBIDO")
        self.assertTrue(all(estado == "ERRO" for estado, _, _ in uploads.values()))
        self.assertEqual(valores, antes[2])  # os indicadores anteriores continuam (SAVEPOINT)

    def test_disco_cheio_ao_preparar_o_arquivo_armazenado_e_falha_do_servidor(self):
        import storage
        self._enviar_e_processar("1.500,00")
        antes = self._estado()
        with patch.object(storage.tempfile, "TemporaryDirectory",
                          side_effect=OSError(errno.ENOSPC, "No space left on device")):
            self.assertEqual(self._processar().status_code, 302)
        self.assertEqual(self._estado(), antes)

    def test_planilha_so_com_cabecalho_e_recusada_no_envio_e_o_arquivo_anterior_fica(self):
        import etl_runtime
        import etl_uploads
        with patch.object(etl_uploads, "run_validation", etl_runtime.run_validation), \
                patch.object(etl_runtime, "varrer_temporarios_orfaos", return_value=0):
            self.assertEqual(self._enviar_cliente().status_code, 302)
            antes = self._estado()[1]
            self.assertIn("VENDAS", antes)
            resposta = self._enviar_cliente(conteudo=b"Codigo;Produto;Qtde;Valor Total\n;TOTAL GERAL;0;0,00\n")
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(any("nenhuma linha de dados" in m for m in self._flashes(self.cli)))
        self.assertEqual(self._estado()[1], antes)


# =============================================================================
# 3. Entrada: planilha sem linhas de dados e disco cheio
# =============================================================================
class TriagemSemLinhasTests(unittest.TestCase):
    RECUSADOS = {
        "só o cabeçalho": "Codigo;Produto;Qtde;Valor Total\n",
        "cabeçalho e linhas vazias": "Codigo;Produto;Qtde;Valor Total\n;;;\n;;;\n",
        "cabeçalho e total": ("Relatorio de Vendas\nPeriodo: 01/10/2025 a 31/10/2025\nCodigo;Produto;Qtde;Valor Total\n"
                              ";TOTAL GERAL;0;0,00\n"),
        "cabeçalho, total e subtotal": "Produto;Qtde;Valor Total\nSubtotal;0;0,00\nTotal Geral;0;0,00\n",
    }
    ACEITOS = {
        "uma linha de dado": "Codigo;Produto;Qtde;Valor Total\n1;CAFE 500G;1;10,00\n",
        "dado e total": "Codigo;Produto;Qtde;Valor Total\n1;CAFE 500G;1;10,00\n;TOTAL GERAL;1;10,00\n",
        "só números": "Codigo;Produto;Qtde;Valor Total\n123;456;1;10,00\n",
        "data na primeira coluna": "Data;Produto;Qtde;Valor Total\n01/10/2025;CAFE 500G;1;10,00\n",
        "compras por fornecedor": "Fornecedor;Nota Fiscal;Valor Total\nALFA;1;10,00\n",
    }

    def _triar(self, conteudo, extensao="csv"):
        from etl_limits import ETLLimits
        import security_gatekeeper
        caminho = _pasta(self) / f"relatorio.{extensao}"
        if isinstance(conteudo, bytes):
            caminho.write_bytes(conteudo)
        else:
            caminho.write_text(conteudo, encoding="utf-8")
        return security_gatekeeper.triar(caminho, extensao, ETLLimits())

    def test_sem_linhas_de_dados_e_recusado_na_entrada(self):
        import security_gatekeeper
        for nome, conteudo in self.RECUSADOS.items():
            with self.subTest(nome=nome):
                with self.assertRaises(security_gatekeeper.SecurityGatekeeperError) as recusa:
                    self._triar(conteudo)
                self.assertEqual(recusa.exception.motivo, "sem_linhas_de_dados")
                self.assertIn(security_gatekeeper.ORIENTACAO_SEM_LINHAS, recusa.exception.safe_message)

    def test_qualquer_linha_que_possa_ser_dado_passa(self):
        for nome, conteudo in self.ACEITOS.items():
            with self.subTest(nome=nome):
                self.assertEqual(self._triar(conteudo)["liberado"], "original")

    def test_xlsx_so_com_cabecalho_e_recusado(self):
        from openpyxl import Workbook
        import security_gatekeeper
        pasta = Workbook()
        aba = pasta.active
        aba.append(["Codigo", "Produto", "Qtde", "Valor Total"])
        dados = io.BytesIO()
        pasta.save(dados)
        with self.assertRaises(security_gatekeeper.SecurityGatekeeperError) as recusa:
            self._triar(dados.getvalue(), "xlsx")
        self.assertEqual(recusa.exception.motivo, "sem_linhas_de_dados")


class DiscoCheioTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()

        def sem_espaco():
            raise OSError(errno.ENOSPC, "No space left on device")

        def acesso_negado():
            raise OSError(errno.EACCES, "Permission denied")

        self.app.add_url_rule("/__teste/disco", "teste_disco", sem_espaco)
        self.app.add_url_rule("/__teste/negado", "teste_negado", acesso_negado)
        self.app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)

    def test_disco_cheio_responde_503_com_retry_after(self):
        with patch.object(self.app.logger, "disabled", False), self.assertLogs(self.app.logger, "WARNING") as logs:
            resposta = self.client.get("/__teste/disco")
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(resposta.headers["Retry-After"], "30")
        corpo = resposta.get_data(as_text=True)
        self.assertIn("falta espaço temporário no servidor", corpo)
        self.assertNotIn("No space left", corpo)
        self.assertIn(f"errno={errno.ENOSPC}", "\n".join(logs.output))

    def test_outro_oserror_continua_500(self):
        self.assertEqual(self.client.get("/__teste/negado").status_code, 500)

    def test_disco_cheio_ao_receber_o_corpo_do_envio(self):
        import flask
        self.login()
        token = self.csrf_token("/cliente/upload")
        with patch.object(flask.Request, "_get_file_stream", side_effect=OSError(errno.ENOSPC, "No space left")):
            resposta = self.client.post("/cliente/upload", data={
                "csrf_token": token, "tipo_relatorio": "VENDAS", "id_analise": "1",
                "arquivo": (io.BytesIO(b"Produto;Qtde\nCAFE;1\n"), "vendas.csv")}, content_type="multipart/form-data")
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(resposta.headers["Retry-After"], "30")
        import etl_uploads
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))  # a vaga não ficou presa
        etl_uploads._UPLOAD_ADMISSION.release()

    def test_disco_cheio_na_gravacao_do_upload_nao_vira_erro_do_arquivo(self):
        from werkzeug.datastructures import FileStorage
        import etl_uploads
        arquivo = FileStorage(io.BytesIO(b"Produto;Qtde\nCAFE;1\n"), filename="vendas.csv")
        with self.app.app_context(), patch.object(etl_uploads.tempfile, "TemporaryDirectory",
                                                  side_effect=OSError(errno.ENOSPC, "No space left")):
            with self.assertRaises(OSError):
                etl_uploads.ler_upload_validado(arquivo, "csv")


# =============================================================================
# 4. Sala de espera
# =============================================================================
class SalaDeEsperaTests(unittest.TestCase):
    def setUp(self):
        import etl_uploads
        self.sala = etl_uploads._SalaDeEspera(vagas=1, max_espera=4)
        self.threads = []

    def tearDown(self):
        for thread in self.threads:
            thread.join(10)

    def _ocupar_em_outra_thread(self):
        segurando, soltar = threading.Event(), threading.Event()

        def dono():
            self.sala.acquire(blocking=False)
            segurando.set()
            soltar.wait(10)
            self.sala.release()

        self._iniciar(dono)
        self.assertTrue(segurando.wait(5))
        self.addCleanup(soltar.set)
        return soltar

    def _iniciar(self, alvo, *argumentos):
        thread = threading.Thread(target=alvo, args=argumentos, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread

    def _esperar_fila(self, tamanho):
        limite = time.monotonic() + 5
        while self.sala.na_espera() != tamanho:
            self.assertLess(time.monotonic(), limite, f"fila não chegou a {tamanho}")
            time.sleep(0.01)

    def test_vaga_livre_entra_na_hora_sem_chamar_o_preparo_da_espera(self):
        chamadas = []
        self.assertTrue(self.sala.entrar("empresa:1", 5, antes_de_esperar=lambda: chamadas.append(1)))
        self.sala.release()
        self.assertEqual(chamadas, [])

    def test_espera_a_vez_por_ordem_de_chegada_liberando_o_banco_antes(self):
        soltar = self._ocupar_em_outra_thread()
        ordem, preparos = [], []

        def esperar(chave):
            if self.sala.entrar(chave, 5, antes_de_esperar=lambda: preparos.append(chave)):
                ordem.append(chave)
                time.sleep(0.02)
                self.sala.release()

        for indice, chave in enumerate(("a", "b", "c", "d"), 1):
            self._iniciar(esperar, chave)
            self._esperar_fila(indice)
        soltar.set()
        for thread in self.threads:
            thread.join(10)
        self.assertEqual(ordem, ["a", "b", "c", "d"])
        self.assertEqual(sorted(preparos), ["a", "b", "c", "d"])

    def test_fila_cheia_mesma_empresa_e_a_propria_thread_recusam_na_hora(self):
        soltar = self._ocupar_em_outra_thread()
        for indice, chave in enumerate(("a", "b", "c", "d"), 1):
            self._iniciar(lambda c=chave: self.sala.entrar(c, 5) and self.sala.release())
            self._esperar_fila(indice)
        for chave, motivo in (("e", "fila cheia"), ("a", "mesma empresa já na fila")):
            with self.subTest(motivo=motivo):
                inicio = time.monotonic()
                self.assertTrue(self.sala.recusaria_agora(chave))
                self.assertFalse(self.sala.entrar(chave, 5))
                self.assertLess(time.monotonic() - inicio, 0.5)
        soltar.set()
        for thread in self.threads:
            thread.join(10)
        self.assertTrue(self.sala.acquire(blocking=False))
        inicio = time.monotonic()
        self.assertFalse(self.sala.entrar("z", 5))  # esperar por si mesma só terminaria no prazo
        self.assertLess(time.monotonic() - inicio, 0.5)
        self.sala.release()

    def test_prazo_esgotado_recusa_e_libera_o_lugar_na_fila(self):
        self._ocupar_em_outra_thread()
        self.assertFalse(self.sala.recusaria_agora("a"))
        inicio = time.monotonic()
        self.assertFalse(self.sala.entrar("a", 0.2))
        self.assertGreaterEqual(time.monotonic() - inicio, 0.2)
        self.assertEqual(self.sala.na_espera(), 0)
        self.assertFalse(self.sala.recusaria_agora("a"))  # o lugar dela saiu da fila


class SeisEnviosSimultaneosTests(DeployTestCase):
    """Seis envios ao mesmo tempo, de seis empresas: um processa, quatro esperam a vez (sem segurar conexão
    do banco) e são atendidos em ordem; o sexto é recusado na hora, com a cota intacta."""

    def setUp(self):
        super().setUp()
        self.fixtures()
        import etl_uploads
        import falhas_banco
        from extensions import csrf
        self.sala = etl_uploads._SalaDeEspera()
        self.liberacoes = []
        real = falhas_banco.rollback_seguro
        for alvo in (patch.object(etl_uploads, "_UPLOAD_ADMISSION", self.sala),
                     patch.object(etl_uploads, "ESPERA_MAXIMA_S", 15),
                     patch.object(etl_uploads, "_chave_de_espera", lambda: request.headers["X-Empresa"]),
                     patch.object(falhas_banco, "rollback_seguro",
                                  lambda: (self.liberacoes.append(request.headers["X-Empresa"]), real()))):
            alvo.start()
            self.addCleanup(alvo.stop)
        self.trava, self.ativos, self.pico, self.atendidos, self.cotas = threading.Lock(), 0, 0, [], {}
        self.liberar_primeiro = threading.Event()

        @etl_uploads.limitar_upload
        def envio():
            with self.trava:
                self.ativos += 1
                self.pico = max(self.pico, self.ativos)
                self.atendidos.append(request.headers["X-Empresa"])
            if request.headers["X-Empresa"] == "empresa:1":
                self.liberar_primeiro.wait(15)
            else:
                time.sleep(0.05)
            with self.trava:
                self.ativos -= 1
            return "processado"

        def registrar_cota(resposta):
            self.cotas[request.headers.get("X-Empresa")] = etl_uploads.deduzir_cota(resposta)
            return resposta

        self.app.add_url_rule("/__teste/envio", "teste_envio", csrf.exempt(envio), methods=["POST"])
        self.app.after_request(registrar_cota)

    def test_seis_envios(self):
        respostas, duracoes = {}, {}

        def enviar(empresa):
            inicio = time.monotonic()
            respostas[empresa] = self.app.test_client().post("/__teste/envio", headers={"X-Empresa": empresa})
            duracoes[empresa] = time.monotonic() - inicio

        threads = [threading.Thread(target=enviar, args=("empresa:1",), daemon=True)]
        threads[0].start()
        limite = time.monotonic() + 5
        while not self.atendidos:
            self.assertLess(time.monotonic(), limite)
            time.sleep(0.01)
        for indice in range(2, 6):
            thread = threading.Thread(target=enviar, args=(f"empresa:{indice}",), daemon=True)
            thread.start()
            threads.append(thread)
            while self.sala.na_espera() != indice - 1:
                self.assertLess(time.monotonic(), limite)
                time.sleep(0.01)
        enviar("empresa:6")  # fila cheia: recusa na hora
        self.liberar_primeiro.set()
        for thread in threads:
            thread.join(20)
        self.assertEqual(self.atendidos, [f"empresa:{indice}" for indice in range(1, 6)])
        self.assertEqual(self.pico, 1)  # um processamento por vez, como sempre
        for indice in range(1, 6):
            self.assertEqual(respostas[f"empresa:{indice}"].get_data(as_text=True), "processado")
            self.assertTrue(self.cotas[f"empresa:{indice}"])
        recusa = respostas["empresa:6"]
        self.assertEqual(recusa.status_code, 302)
        self.assertLess(duracoes["empresa:6"], 1)
        self.assertFalse(self.cotas["empresa:6"])  # a recusa não desconta a cota
        # Os quatro que esperaram encerraram a transação antes (conexão devolvida ao pool).
        self.assertEqual(sorted(self.liberacoes), [f"empresa:{indice}" for indice in range(2, 6)])


class CotaNaoDescontadaNaRecusaTests(token_cases._TokenCase):
    ENABLE_LIMITS = True  # o Flask-Limiter só instala os limites se nasce ligado
    _login_client = session_cases.SessionRevocationCases._login_client

    def test_recusas_por_vaga_ocupada_nao_gastam_a_cota_do_envio(self):
        import etl_uploads
        client = self._login_client()
        token = self._csrf(client, "/cliente/dashboard")
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            # O envio tem 30 por hora: se a recusa contasse, a 31ª já seria 429.
            for _ in range(31):
                self.assertEqual(client.post("/cliente/upload", data={"csrf_token": token}).status_code, 302)
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()
        self.assertNotEqual(client.post("/cliente/upload", data={"csrf_token": token}).status_code, 429)


if __name__ == "__main__":
    unittest.main()
