"""Banco indisponível: 503 amigável em vez de 500, sem SQL na tela ou no log.

Fase 1 da auditoria de estresse: queda do banco (conexão recusada, derrubada,
prazo esgotado, pool cheio) responde 503 com "Sistema indisponível", o código
para o suporte e Retry-After; erro de programação continua 500 com traceback.
O login com o banco fora também responde 503, sem criar sessão.
"""

import io
import logging
import re
import sqlite3
import unittest
from unittest.mock import patch

import psycopg2
from sqlalchemy import event
from sqlalchemy.exc import DatabaseError, IntegrityError, InterfaceError, OperationalError, ProgrammingError
from sqlalchemy.exc import TimeoutError as PoolEsgotado

import falhas_banco
from tests.deploy_support import DeployTestCase

SEGREDO = "SEGREDO_HOST db.projeto.supabase.co SELECT senha_hash FROM usuario"
_CODIGO = re.compile(r"correlacao=([0-9a-f]{12})")


class _ErroPg(Exception):
    """Erro do driver com SQLSTATE, como os do psycopg2 vindos do servidor."""

    def __init__(self, pgcode):
        super().__init__(SEGREDO)
        self.pgcode = pgcode


def _queda(pgcode=None, classe=OperationalError, **extras):
    orig = psycopg2.OperationalError(SEGREDO) if pgcode is None else _ErroPg(pgcode)
    return classe(SEGREDO, {"senha": SEGREDO}, orig, **extras)


class ClassificacaoDaQuedaTests(unittest.TestCase):
    def test_quedas_de_conexao_e_recurso_contam_como_indisponibilidade(self):
        casos = {
            "conexão recusada ou prazo de conexão (sem SQLSTATE)": _queda(),
            "conexão fechada (InterfaceError do driver)": InterfaceError(
                SEGREDO, None, psycopg2.InterfaceError("connection already closed")),
            "08006 falha de conexão": _queda("08006"),
            "53300 conexões demais": _queda("53300"),
            "57014 tempo da consulta esgotado": _queda("57014"),
            "57P01 servidor desligando": _queda("57P01"),
            "40P01 deadlock": _queda("40P01"),
            "conexão invalidada pelo dialeto": _queda("42P01", classe=ProgrammingError,
                                                     connection_invalidated=True),
            "pool esgotado": PoolEsgotado("QueuePool limit of size 5 overflow 10 reached"),
        }
        for nome, erro in casos.items():
            with self.subTest(nome):
                self.assertTrue(falhas_banco.eh_banco_indisponivel(erro))

    def test_erro_de_programacao_ou_local_nao_e_queda(self):
        casos = {
            "tabela inexistente no SQLite": OperationalError(
                "INSERT", {}, sqlite3.OperationalError("no such table: mensagem_suporte")),
            "SQLSTATE de sintaxe numa OperationalError": _queda("42601"),
            "integridade": IntegrityError("INSERT", {}, _ErroPg("23505")),
            "programação": ProgrammingError("SELECT", {}, _ErroPg("42P01")),
            "exceção qualquer": RuntimeError("falha"),
        }
        for nome, erro in casos.items():
            with self.subTest(nome):
                self.assertFalse(falhas_banco.eh_banco_indisponivel(erro))

    def test_descricao_para_o_log_nunca_leva_a_mensagem_do_driver(self):
        self.assertEqual(falhas_banco.descrever(_queda()), "OperationalError")
        self.assertEqual(falhas_banco.descrever(_queda("57014")), "OperationalError, pgcode=57014")
        self.assertNotIn("SEGREDO", falhas_banco.descrever(_queda("57014")))

    def test_descricao_nomeia_a_causa_provavel_sem_ecoar_host_ou_usuario(self):
        casos = {
            'connection to server at "db.x.supabase.co" (10.0.0.1), port 5432 failed: '
            'FATAL:  password authentication failed for user "postgres"': "autenticacao_recusada",
            'connection to server at "db.x.supabase.co" (10.0.0.1), port 5432 failed: timeout expired':
                "prazo_de_conexao_esgotado",
            'connection to server at "127.0.0.1", port 5432 failed: Connection refused': "conexao_recusada",
            "SSL SYSCALL error: EOF detected": "ssl",
            "server closed the connection unexpectedly": "conexao_encerrada_pelo_servidor",
        }
        for mensagem, motivo in casos.items():
            with self.subTest(motivo=motivo):
                descricao = falhas_banco.descrever(OperationalError("SELECT 1", {}, psycopg2.OperationalError(mensagem)))
                self.assertEqual(descricao, f"OperationalError, motivo={motivo}")
                for vazamento in ("supabase", "10.0.0.1", "127.0.0.1", "postgres\"", "5432"):
                    self.assertNotIn(vazamento, descricao)


class RespostaDaQuedaTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        self.erro = None
        self.sql_antes_do_erro = None
        self.sql = []

        def _falha():
            self.sql_antes_do_erro = len(self.sql)
            raise self.erro

        for caminho, nome in (("/__teste/banco", "teste_banco"), ("/api/__teste/banco", "teste_api_banco")):
            self.app.add_url_rule(caminho, nome, _falha)
        # Como em produção: os errorhandlers respondem em vez de propagar.
        self.app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)

        def _registrar(conn, cursor, statement, parameters, context, executemany):
            self.sql.append(statement)
        engine = self.db.engine
        event.listen(engine, "before_cursor_execute", _registrar)
        self.addCleanup(event.remove, engine, "before_cursor_execute", _registrar)

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

    def _entrar(self):
        http = self.app.test_client()
        pagina = http.get("/auth/login").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina).group(1)
        resposta = http.post("/auth/login", data={"email": "cliente-test@example.invalid",
                                                  "senha": self.password, "csrf_token": csrf})
        self.assertEqual(resposta.status_code, 302)
        return http

    def _assert_503(self, resposta, log):
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(resposta.headers["Retry-After"], "30")
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        corpo = resposta.get_data(as_text=True)
        self.assertNotIn("SEGREDO", corpo)
        self.assertNotIn("SEGREDO", log)
        self.assertNotIn("Traceback", log)
        (codigo,) = _CODIGO.findall(log)
        return corpo, codigo

    def test_queda_em_qualquer_rota_vira_503_com_codigo_para_o_suporte(self):
        self.erro = _queda()
        resposta, log = self._capturar(lambda: self.client.get("/__teste/banco"))
        corpo, codigo = self._assert_503(resposta, log)
        self.assertIn(falhas_banco.TITULO, corpo)
        self.assertIn(falhas_banco.MENSAGEM, corpo)
        self.assertIn(f"Código para o suporte: <code>{codigo}</code>", corpo)
        self.assertIn("WARNING Banco indisponível em /__teste/banco [GET] (OperationalError, correlacao=", log)

    def test_api_recebe_json_com_a_correlacao(self):
        self.erro = _queda("57014")
        resposta, log = self._capturar(lambda: self.client.get("/api/__teste/banco"))
        _, codigo = self._assert_503(resposta, log)
        self.assertEqual(resposta.get_json(), {
            "erro": falhas_banco.MENSAGEM, "resposta": falhas_banco.MENSAGEM, "fonte": "local",
            "acao": None, "destino": None, "meta": {"correlacao": codigo},
        })
        self.assertIn("(OperationalError, pgcode=57014, correlacao=", log)

    def test_pool_esgotado_tambem_responde_503(self):
        self.erro = PoolEsgotado("QueuePool limit of size 5 overflow 10 reached")
        resposta, log = self._capturar(lambda: self.client.get("/__teste/banco"))
        self._assert_503(resposta, log)

    def test_conexao_derrubada_no_meio_da_consulta_responde_503(self):
        # O pooler derruba a conexão: o psycopg2 levanta DatabaseError e o dialeto
        # marca connection_invalidated (não é OperationalError nem InterfaceError).
        self.erro = DatabaseError("SELECT 1", {}, psycopg2.DatabaseError("SSL SYSCALL error: EOF detected"),
                                  connection_invalidated=True)
        resposta, log = self._capturar(lambda: self.client.get("/__teste/banco"))
        self._assert_503(resposta, log)
        self.assertIn("(DatabaseError, motivo=ssl, correlacao=", log)

    def test_sininho_que_falha_deixa_o_polling_sem_ponto_de_partida(self):
        # A página sai sem o sininho, e o data-ultimo vazio impede que a primeira
        # consulta trate as não lidas antigas como novas (alertas flutuantes).
        http = self._entrar()

        def _falha_sininho(conn, cursor, statement, parameters, context, executemany):
            if "notificacao" in statement.lower():
                raise OperationalError(statement, {}, psycopg2.OperationalError(SEGREDO))
        engine = self.db.engine
        event.listen(engine, "before_cursor_execute", _falha_sininho)
        try:
            pagina = http.get("/cliente/tickets")
        finally:
            event.remove(engine, "before_cursor_execute", _falha_sininho)
        self.assertEqual(pagina.status_code, 200)
        html = pagina.get_data(as_text=True)
        self.assertIn('data-ultimo=""', html)
        self.assertNotIn("SEGREDO", html)
        self.assertRegex(http.get("/cliente/tickets").get_data(as_text=True), r'data-ultimo="\d+"')

    def test_pagina_503_nao_consulta_o_banco_de_novo(self):
        # Logado, o sininho, o menu e o NexoBot consultariam o banco ao montar a página;
        # com o banco fora, cada tentativa somaria um prazo de conexão à resposta.
        http = self._entrar()
        self.erro = _queda()
        self.sql.clear()
        resposta, log = self._capturar(lambda: http.get("/__teste/banco"))
        self._assert_503(resposta, log)
        self.assertIsNotNone(self.sql_antes_do_erro)
        self.assertEqual(len(self.sql), self.sql_antes_do_erro, self.sql[self.sql_antes_do_erro:])
        # A sessão segue intacta: a próxima página funciona sem novo login.
        self.assertEqual(http.get("/cliente/tickets").status_code, 200)

    def test_erro_de_programacao_continua_500_com_traceback(self):
        for erro in (OperationalError("INSERT", {}, sqlite3.OperationalError("no such table: x")),
                     _queda("42601"), IntegrityError("INSERT", {}, _ErroPg("23505"))):
            with self.subTest(erro=type(erro).__name__):
                self.erro = erro
                resposta, log = self._capturar(lambda: self.client.get("/__teste/banco"))
                self.assertEqual(resposta.status_code, 500)
                self.assertIn("Algo deu errado", resposta.get_data(as_text=True))
                self.assertIn("Traceback", log)
                self.assertNotIn("Banco indisponível", log)


class LoginComBancoForaTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()

    def _postar_login(self, http):
        pagina = http.get("/auth/login").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina).group(1)
        return http.post("/auth/login", data={"email": "cliente-test@example.invalid",
                                              "senha": self.password, "csrf_token": csrf})

    def _assert_login_503(self, alvo):
        # Os casos de deploy desligam os loggers já existentes; este teste lê o do auth.
        logger = logging.getLogger("blueprints.auth")
        self.addCleanup(setattr, logger, "disabled", logger.disabled)
        logger.disabled = False
        http = self.app.test_client()
        with patch(alvo, side_effect=_queda()), self.assertLogs("blueprints.auth", logging.WARNING) as logs:
            resposta = self._postar_login(http)
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(resposta.headers["Retry-After"], "30")
        corpo = resposta.get_data(as_text=True)
        self.assertIn(falhas_banco.MENSAGEM, corpo)
        self.assertNotIn("SEGREDO", corpo)
        self.assertNotIn("SEGREDO", "\n".join(logs.output))
        self.assertIn("Login indisponível (OperationalError).", "\n".join(logs.output))
        with http.session_transaction() as sessao:
            self.assertNotIn("_user_id", sessao)
        # Banco de volta: o mesmo login entra normalmente.
        self.assertEqual(self._postar_login(http).status_code, 302)

    def test_busca_da_conta_com_o_banco_fora_responde_503(self):
        self._assert_login_503("blueprints.auth.buscar_usuario_por_email")

    def test_conferencia_de_acesso_com_o_banco_fora_responde_503(self):
        self._assert_login_503("blueprints.auth.usuario_pode_acessar")

    def test_erro_de_programacao_no_login_nao_vira_503(self):
        # Coluna inexistente depois de um deploy sem migration não é queda: segue
        # para o caminho do 500 (aqui, em teste, a exceção propaga).
        http = self.app.test_client()
        erro = ProgrammingError("SELECT", {}, _ErroPg("42703"))
        with patch("blueprints.auth.buscar_usuario_por_email", side_effect=erro):
            with self.assertRaises(ProgrammingError):
                self._postar_login(http)
