"""SEC-003 em PostgreSQL privado: execute explicitamente, sem DSN externo."""

from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash

from . import postgresql_auth_tokens as pg_cases
from . import test_auth_tokens_security as token_cases
from . import test_session_revocation_security as session_cases
from .postgresql_support import PrivatePostgreSQL
from extensions import db
from blueprints import auth


class PostgreSQLSessionRevocationSecurityTests(session_cases.SessionRevocationCases, token_cases._TokenCase):
    @classmethod
    def setUpClass(cls):
        cls.postgresql = PrivatePostgreSQL()
        cls.addClassCleanup(cls.postgresql.close)

    def setUp(self):
        pg_cases.PostgreSQLAuthTokenSecurityTests.setUp(self)
        self._session_routes()

    # O Socket.IO saiu (o sininho usa polling): não há mais entrega assíncrona que
    # precise segurar FOR SHARE no usuário. Cada consulta é uma requisição HTTP
    # comum, validada na entrada como qualquer página.
    def test_logout_revokes_the_session_for_later_polls(self):
        http = self._login_client()
        cookie = self._cookie(http)
        self._notify(text="AUTHORIZED_BEFORE_LOGOUT")
        self.assertIn("AUTHORIZED_BEFORE_LOGOUT", self._polled_texts(http))
        self._logout(http)
        self.assertEqual(self._session_rows(), [(True, "logout")])
        self._notify(text="PRIVATE_AFTER_LOGOUT")
        response = self._assert_poll_denied(self._replay(cookie))
        self.assertNotIn("PRIVATE_AFTER_LOGOUT", response.get_data(as_text=True))
        self._assert_private_denied(http)

    def test_poll_works_with_single_connection_pool(self):
        """Com uma única conexão no pool, criar a notificação e consultá-la não trava."""
        with self.app.app_context():
            db.session.remove()
            previous = self.engine
            replacement = create_engine(
                previous.url, pool_size=1, max_overflow=0, pool_timeout=1,
                connect_args={
                    "connect_timeout": 5, "password": "", "passfile": str(self.postgresql.passfile),
                    "options": "-c statement_timeout=15000 -c lock_timeout=10000",
                },
            )
            # A extensão expõe o mapa de engines da aplicação. A troca afeta
            # somente este app/banco sintético e o cleanup dispõe o novo pool.
            db.engines[None] = replacement
            self.engine = replacement
            previous.dispose()
        client = self._login_client()
        self._notify(text="SINGLE_CONNECTION_POOL_PRIVATE")
        self.assertEqual(self._polled_texts(client), ["SINGLE_CONNECTION_POOL_PRIVATE"])

    def test_polls_concurrent_with_password_update_answer_200_or_401_then_deny(self):
        """Consultas em paralelo com a troca de senha no PostgreSQL real: cada uma
        responde 200 (validada antes do COMMIT) ou 401, nunca erro; depois do COMMIT
        o cookie antigo não recebe mais nada."""
        cookie = self._cookie(self._login_client())
        token = self._token()
        reset_client = self.app.test_client()
        csrf = self._csrf(reset_client)
        update_pending, polled_during_update, status = threading.Event(), threading.Event(), []

        # O UPDATE da senha espera até o polling consultar com a troca em andamento:
        # a sobreposição é garantida, não deixada ao acaso.
        def hold_update(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE USUARIO SET") and not update_pending.is_set():
                update_pending.set()
                if not polled_during_update.wait(12):
                    raise AssertionError("O polling não consultou durante a troca de senha.")

        def poll_during_update():
            self.assertTrue(update_pending.wait(8))
            for _ in range(4):
                status.append(self._poll(self._replay(cookie)).status_code)
            polled_during_update.set()

        event.listen(self.engine, "before_cursor_execute", hold_update)
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="poll-race") as executor:
                polls = executor.submit(poll_during_update)
                try:
                    self._assert_success(self._post(token, client=reset_client, csrf=csrf))
                finally:
                    polled_during_update.set()
                polls.result(timeout=30)
        finally:
            event.remove(self.engine, "before_cursor_execute", hold_update)
        # Antes do COMMIT a credencial antiga ainda vale: 200, nunca erro.
        self.assertEqual(status, [200] * 4)
        self._notify(text="PRIVATE_AFTER_PASSWORD_COMMIT")
        response = self._assert_poll_denied(self._replay(cookie))
        self.assertNotIn("PRIVATE_AFTER_PASSWORD_COMMIT", response.get_data(as_text=True))

    def test_password_update_moves_delivery_to_the_new_login(self):
        old = self._cookie(self._login_client())
        self._reset_password()
        self.assertTrue(check_password_hash(self._account()[0], token_cases.NEW_PASSWORD))
        current = self._login_client(password=token_cases.NEW_PASSWORD)
        self._notify(text="PRIVATE_AFTER_COMMIT")
        self.assertNotIn("PRIVATE_AFTER_COMMIT", self._assert_poll_denied(self._replay(old)).get_data(as_text=True))
        self.assertIn("PRIVATE_AFTER_COMMIT", self._polled_texts(current))

    def test_login_cannot_bind_old_password_verification_to_newer_credential(self):
        """Um login já validou a senha antiga quando outra sessão a modifica."""
        before_cookie, release = threading.Event(), threading.Event()
        client = self.app.test_client()
        csrf = self._csrf(client)
        reset_client = self.app.test_client()
        reset_csrf = self._csrf(reset_client)
        token = self._token()

        login_user = auth.login_user
        def pause_before_cookie(*args, **kwargs):
            before_cookie.set()
            if not release.wait(12):
                raise AssertionError("Login de teste não foi liberado.")
            return login_user(*args, **kwargs)
        try:
            with patch.object(auth, "login_user", side_effect=pause_before_cookie):
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="old-password-login") as executor:
                    login = executor.submit(client.post, "/auth/login", data={
                        "email": "active@example.invalid", "senha": token_cases.OLD_PASSWORD,
                        "csrf_token": csrf,
                    })
                    try:
                        self.assertTrue(before_cookie.wait(8))
                        self._assert_success(self._post(token, client=reset_client, csrf=reset_csrf))
                    finally:
                        release.set()
                    self.assertEqual(login.result(timeout=12).status_code, 302)
        finally:
            release.set()
        self._assert_private_denied(client)
        self.assertEqual(self._login_client(password=token_cases.NEW_PASSWORD)
                         .get("/_security/protected").status_code, 200)

    def test_first_access_renewal_does_not_adopt_a_later_password_change(self):
        """A renovação do próprio cookie usa o hash confirmado, sem refresh cego."""
        client = self._login_client("pending")
        csrf = self._csrf(client, "/auth/primeiro-acesso")
        first_committed, release = threading.Event(), threading.Event()
        def committed(session):
            if threading.current_thread().name.startswith("first-access-renewal"):
                first_committed.set()
                if not release.wait(12):
                    raise AssertionError("Renovação de teste não foi liberada.")
        event.listen(Session, "after_commit", committed)
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-access-renewal") as executor:
                first = executor.submit(client.post, "/auth/primeiro-acesso", data={
                    "nova_senha": token_cases.NEW_PASSWORD,
                    "confirma_senha": token_cases.NEW_PASSWORD, "csrf_token": csrf,
                })
                try:
                    self.assertTrue(first_committed.wait(8))
                    token = self._token("pending")
                    self._assert_success(self._post(token, password=token_cases.NEW_PASSWORD + "-second",
                                                   client=self.app.test_client()))
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=12).status_code, 302)
        finally:
            release.set()
            event.remove(Session, "after_commit", committed)
        self.assertTrue(check_password_hash(self._account("pending")[0],
                                            token_cases.NEW_PASSWORD + "-second"))
        self._assert_private_denied(client)
