"""Erro 500: código de rastreio no log e na resposta, sem os valores do SQL no log."""

import io
import logging
import re
from unittest.mock import patch

from flask import abort
from sqlalchemy import text

from tests.deploy_support import DeployTestCase

SENTINELA = "CPF 123.456.789-09 pessoa.sentinela@lojaficticia.invalid"
MENSAGEM_500 = "Ocorreu um erro inesperado. Tente novamente em instantes."
_CODIGO = re.compile(r"\(correlacao=([0-9a-f]{12})\)")


class ErroInternoRastreioTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()

        def _quebra():
            raise RuntimeError("falha sintética do teste")

        def _aborta():
            abort(500)

        # Rotas sintéticas antes do primeiro pedido (depois dele o Flask recusa).
        for caminho, nome, funcao in (
            ("/__teste/quebra", "teste_quebra", _quebra),
            ("/api/__teste/quebra", "teste_api_quebra", _quebra),
            ("/__teste/aborta", "teste_aborta", _aborta),
            ("/api/__teste/aborta", "teste_api_aborta", _aborta),
        ):
            self.app.add_url_rule(caminho, nome, funcao)
        # Como em produção: o Flask grava o traceback e os errorhandlers respondem.
        self.app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)

    def _capturar(self, pedido):
        logger = self.app.logger
        anterior = logger.handlers[:], logger.level, logger.propagate, logger.disabled
        fluxo = io.StringIO()
        handler = logging.StreamHandler(fluxo)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.disabled = False
        try:
            resposta = pedido()
        finally:
            logger.handlers[:], logger.level, logger.propagate, logger.disabled = anterior
        return resposta, fluxo.getvalue()

    def _codigo_do_log(self, log):
        achados = _CODIGO.findall(log)
        self.assertEqual(len(achados), 1, log)
        return achados[0]

    def test_pagina_500_mostra_o_codigo_gravado_junto_do_traceback(self):
        resposta, log = self._capturar(lambda: self.client.get("/__teste/quebra"))
        self.assertEqual(resposta.status_code, 500)
        corpo = resposta.get_data(as_text=True)
        self.assertIn("Algo deu errado", corpo)
        self.assertIn(MENSAGEM_500, corpo)
        codigo = self._codigo_do_log(log)
        self.assertIn(f"Código para o suporte: <code>{codigo}</code>", corpo)
        # Um registro só: a linha com o código abre o traceback da exceção.
        self.assertEqual(log.count("ERROR "), 1, log)
        self.assertIn(f"ERROR Exception on /__teste/quebra [GET] (correlacao={codigo})\nTraceback", log)
        self.assertIn("RuntimeError: falha sintética do teste", log)
        self.assertNotIn("falha sintética", corpo)
        # Cada erro ganha um código novo, aleatório.
        segunda, log2 = self._capturar(lambda: self.client.get("/__teste/quebra"))
        self.assertEqual(segunda.status_code, 500)
        self.assertNotEqual(self._codigo_do_log(log2), codigo)

    def test_api_500_devolve_a_correlacao_no_meta(self):
        resposta, log = self._capturar(lambda: self.client.get("/api/__teste/quebra"))
        self.assertEqual(resposta.status_code, 500)
        self.assertEqual(resposta.mimetype, "application/json")
        codigo = self._codigo_do_log(log)
        self.assertEqual(resposta.get_json(), {
            "erro": MENSAGEM_500, "resposta": MENSAGEM_500, "fonte": "local",
            "acao": None, "destino": None, "meta": {"correlacao": codigo},
        })
        self.assertIn("Traceback", log)
        self.assertNotIn("falha sintética", resposta.get_data(as_text=True))

    def test_abort_500_explicito_e_outros_erros_respondem_sem_codigo(self):
        pagina = self.client.get("/__teste/aborta")
        self.assertEqual(pagina.status_code, 500)
        corpo = pagina.get_data(as_text=True)
        self.assertIn("Algo deu errado", corpo)
        self.assertNotIn("Código para o suporte", corpo)
        api = self.client.get("/api/__teste/aborta")
        self.assertEqual(api.status_code, 500)
        self.assertEqual(api.get_json()["erro"], MENSAGEM_500)
        self.assertNotIn("meta", api.get_json())
        ausente = self.client.get("/api/__teste/nao-existe")
        self.assertEqual(ausente.status_code, 404)
        self.assertNotIn("meta", ausente.get_json())
        self.assertNotIn("Código para o suporte", self.client.get("/__teste/nao-existe").get_data(as_text=True))

    def test_erro_de_banco_nao_grava_os_valores_dos_parametros_no_log(self):
        from config import DevelopmentConfig, ProductionConfig
        from models import ChamadoSuporte, Usuario
        for classe in (DevelopmentConfig, ProductionConfig):
            self.assertIs(classe.SQLALCHEMY_ENGINE_OPTIONS.get("hide_parameters"), True)
        self.assertTrue(self.db.engine.hide_parameters)

        cliente = self.db.session.get(Usuario, self.cliente_id)
        chamado = ChamadoSuporte(id_empresa=cliente.id_empresa, id_usuario_cliente=cliente.id_usuario,
                                 assunto="Assunto fictício", categoria="FINANCEIRO")
        self.db.session.add(chamado)
        self.db.session.commit()
        caminho = f"/cliente/tickets/{chamado.id_chamado}"
        self.login()
        token = self.csrf_token(caminho)
        # Quebra real do banco: sem a tabela, o INSERT da mensagem falha com
        # sqlite3.OperationalError, e o traceback leva o SQL ao log.
        self.db.session.execute(text("ALTER TABLE mensagem_suporte RENAME TO mensagem_suporte_fora"))
        self.db.session.commit()

        # A conferência do clique duplo (chamados_suporte.resposta_repetida) lê a mesma tabela antes do INSERT:
        # aqui ela passa, para a falha cair no INSERT que leva a mensagem (o valor que não pode ir ao log).
        with patch("chamados_suporte.resposta_repetida", return_value=False):
            resposta, log = self._capturar(lambda: self.client.post(
                caminho, data={"mensagem": SENTINELA, "csrf_token": token}))
        self.assertEqual(resposta.status_code, 500)
        self.assertIn("Traceback", log)
        self.assertIn("INSERT INTO mensagem_suporte", log)
        self.assertIn("[SQL parameters hidden due to hide_parameters=True]", log)
        for trecho in (SENTINELA, "123.456.789-09", "pessoa.sentinela", "lojaficticia"):
            self.assertNotIn(trecho, log)
        corpo = resposta.get_data(as_text=True)
        self.assertNotIn("pessoa.sentinela", corpo)
        self.assertIn(f"Código para o suporte: <code>{self._codigo_do_log(log)}</code>", corpo)
        # Sem a dublagem, quem falha primeiro é a leitura da conferência: o log também fica sem os valores.
        resposta, log = self._capturar(lambda: self.client.post(
            caminho, data={"mensagem": SENTINELA, "csrf_token": token}))
        self.assertEqual(resposta.status_code, 500)
        self.assertIn("FROM mensagem_suporte", log)
        self.assertIn("[SQL parameters hidden due to hide_parameters=True]", log)
        for trecho in (SENTINELA, "123.456.789-09", "pessoa.sentinela", "lojaficticia"):
            self.assertNotIn(trecho, log)
