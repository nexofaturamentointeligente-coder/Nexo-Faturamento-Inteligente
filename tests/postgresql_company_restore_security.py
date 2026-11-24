"""SEC-004: transações reais em PostgreSQL privado, sem DSN de produção.

Executar explicitamente com NEXO_TEST_POSTGRES_BIN. Cada caso recebe um banco
vazio em cluster temporário, exclusivamente por socket Unix. As corridas abaixo
observam pg_blocking_pids: não inferem serialização de atrasos ou de um mock.
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch

from sqlalchemy import event, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import postgresql_auth_tokens as pg_cases
from . import test_auth_tokens_security as token_cases
from . import test_session_revocation_security as session_cases
from .postgresql_support import PrivatePostgreSQL
from blueprints import auth
from company_security import excluir_empresa, restaurar_empresa
from extensions import db
from models import Empresa, Notificacao, Usuario


class PostgreSQLCompanyRestoreSecurityTests(token_cases._TokenCase):
    """Somente os casos SEC-004; não duplica as suítes SEC-002/SEC-003."""

    _session_routes = session_cases.SessionRevocationCases._session_routes
    _login_client = session_cases.SessionRevocationCases._login_client
    _assert_private_denied = session_cases.SessionRevocationCases._assert_private_denied
    _notify = session_cases.SessionRevocationCases._notify
    # Polling do sininho (substituiu o Socket.IO): GET /notificacoes/novas.
    POLL = session_cases.SessionRevocationCases.POLL
    _poll = session_cases.SessionRevocationCases._poll
    _polled_texts = session_cases.SessionRevocationCases._polled_texts
    _assert_poll_denied = session_cases.SessionRevocationCases._assert_poll_denied
    _cookie = session_cases.SessionRevocationCases._cookie
    _replay = session_cases.SessionRevocationCases._replay

    @classmethod
    def setUpClass(cls):
        cls.postgresql = PrivatePostgreSQL()
        cls.addClassCleanup(cls.postgresql.close)

    def setUp(self):
        pg_cases.PostgreSQLAuthTokenSecurityTests.setUp(self)
        self._session_routes()
        with self.app.app_context():
            self.company_id = db.session.get(Usuario, self.ids["active"]).id_empresa
            self.other_company_id = db.session.get(Usuario, self.ids["other"]).id_empresa

    def _state(self):
        with Session(self.engine) as consulta:
            company = consulta.get(Empresa, self.company_id)
            users = consulta.scalars(select(Usuario).order_by(Usuario.id_usuario)).all()
            return (
                None if company is None else company.deletado_em,
                {user.id_usuario: (user.ativo, user.geracao_autorizacao, user.senha_hash,
                                   user.id_empresa, user.role) for user in users},
            )

    def _transition(self, operation, ready=None, release=None, company_id=None):
        with self.app.app_context():
            function = excluir_empresa if operation == "delete" else restaurar_empresa
            result = function(db.session, self.company_id if company_id is None else company_id)
            db.session.flush()
            if ready is not None:
                ready.set()
                if not release.wait(12):
                    raise AssertionError("Transação SEC-004 de teste não foi liberada.")
            db.session.commit()
            return result is not None

    def _wait_for_blocked_transaction(self, query_fragment=""):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with self.engine.connect() as connection:
                blocked = connection.scalar(text("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database() AND pid <> pg_backend_pid()
                      AND wait_event_type = 'Lock'
                      AND cardinality(pg_blocking_pids(pid)) > 0
                      AND query ILIKE :query
                """), {"query": "%" + query_fragment + "%"})
            if blocked:
                return
            threading.Event().wait(0.01)
        self.fail("A corrida não demonstrou espera por lock real no PostgreSQL privado.")

    def _serialized_pair(self, first_operation, second_operation):
        ready, release = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sec004-lifecycle") as executor:
            first = executor.submit(self._transition, first_operation, ready, release)
            try:
                self.assertTrue(ready.wait(8))
                second = executor.submit(self._transition, second_operation)
                self._wait_for_blocked_transaction("empresa")
            finally:
                release.set()
            return first.result(timeout=12), second.result(timeout=12)

    def test_simultaneous_deletes_increment_generation_only_once(self):
        before = self._state()[1]
        self.assertEqual(self._serialized_pair("delete", "delete"), (True, False))
        deleted, after = self._state()
        self.assertIsNotNone(deleted)
        for uid, original in before.items():
            if original[3] == self.company_id:
                self.assertEqual(after[uid], (original[0], 1, *original[2:]))
            else:
                self.assertEqual(after[uid], original)

    def test_simultaneous_restores_do_not_change_individual_activation(self):
        self._change("pending", ativo=False)
        self.assertTrue(self._transition("delete"))
        before = self._state()[1]
        self.assertEqual(self._serialized_pair("restore", "restore"), (True, False))
        self.assertIsNone(self._state()[0])
        self.assertEqual(self._state()[1], before)

    def test_restore_waits_for_delete_then_preserves_committed_generation(self):
        self.assertEqual(self._serialized_pair("delete", "restore"), (True, True))
        deleted, users = self._state()
        self.assertIsNone(deleted)
        self.assertEqual(users[self.ids["active"]][0:2], (True, 1))

    def test_delete_waits_for_restore_then_starts_a_new_generation(self):
        self._transition("delete")
        self.assertEqual(self._serialized_pair("restore", "delete"), (True, True))
        deleted, users = self._state()
        self.assertIsNotNone(deleted)
        self.assertEqual(users[self.ids["active"]][0:2], (True, 2))

    def test_later_administrative_deactivation_survives_restore(self):
        ready, release = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sec004-admin-state") as executor:
            deleted = executor.submit(self._transition, "delete", ready, release)
            try:
                self.assertTrue(ready.wait(8))
                changed = executor.submit(self._change, "active", ativo=False)
                self._wait_for_blocked_transaction("UPDATE usuario")
            finally:
                release.set()
            self.assertTrue(deleted.result(timeout=12))
            changed.result(timeout=12)
        self.assertTrue(self._transition("restore"))
        self.assertEqual(self._state()[1][self.ids["active"]][0:2], (False, 1))
        client = self.app.test_client()
        denied = client.post("/auth/login", data={
            "email": "active@example.invalid", "senha": token_cases.OLD_PASSWORD,
            "csrf_token": self._csrf(client),
        })
        self.assertEqual(denied.status_code, 200)
        self.assertIn("desativada", denied.get_data(as_text=True))
        self._assert_private_denied(client)

    def test_existing_user_write_commits_before_delete_without_lost_activation(self):
        ready, release = threading.Event(), threading.Event()

        def deactivate():
            with self.engine.begin() as connection:
                connection.execute(update(Usuario).where(
                    Usuario.id_usuario == self.ids["active"],
                ).values(ativo=False))
                ready.set()
                if not release.wait(12):
                    raise AssertionError("Atualização administrativa de teste não foi liberada.")

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sec004-prior-state") as executor:
            changed = executor.submit(deactivate)
            try:
                self.assertTrue(ready.wait(8))
                deleted = executor.submit(self._transition, "delete")
                self._wait_for_blocked_transaction("usuario")
            finally:
                release.set()
            changed.result(timeout=12)
            self.assertTrue(deleted.result(timeout=12))
        self._transition("restore")
        self.assertEqual(self._state()[1][self.ids["active"]][0:2], (False, 1))

    def test_real_postgresql_error_rolls_back_company_and_all_generations(self):
        before = self._state()
        failed = []

        def fail_once(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE USUARIO SET") and not failed:
                failed.append(True)
                cursor.execute("SELECT 1 / 0")

        event.listen(self.engine, "before_cursor_execute", fail_once)
        try:
            with self.app.app_context():
                excluir_empresa(db.session, self.company_id)
                with self.assertRaises(SQLAlchemyError):
                    db.session.commit()
                db.session.rollback()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_once)
        self.assertEqual(failed, [True])
        self.assertEqual(self._state(), before)
        self.assertTrue(self._transition("delete"))

    # Sem Socket.IO não há entrega assíncrona segurando FOR SHARE: o que vale é a
    # validação de cada consulta do sininho contra o banco real.
    def test_company_delete_moves_delivery_to_the_new_login_after_restore(self):
        old_http = self._login_client()
        cookie = self._cookie(old_http)
        self._notify(text="AUTHORIZED_BEFORE_COMPANY_DELETE_COMMIT")
        self.assertIn("AUTHORIZED_BEFORE_COMPANY_DELETE_COMMIT", self._polled_texts(old_http))
        self.assertTrue(self._transition("delete"))
        self._notify(text="PRIVATE_AFTER_COMPANY_DELETE_COMMIT")
        self.assertNotIn("PRIVATE_AFTER_COMPANY_DELETE_COMMIT",
                         self._assert_poll_denied(self._replay(cookie)).get_data(as_text=True))
        self._transition("restore")
        fresh = self._login_client()
        self._notify(text="PRIVATE_RESTORED_NEW_AUTHORIZATION")
        self.assertNotIn("PRIVATE_RESTORED_NEW_AUTHORIZATION",
                         self._assert_poll_denied(self._replay(cookie)).get_data(as_text=True))
        self.assertIn("PRIVATE_RESTORED_NEW_AUTHORIZATION", self._polled_texts(fresh))
        self._assert_private_denied(old_http)

    def test_polls_concurrent_with_company_delete_answer_200_or_401_then_deny(self):
        cookie = self._cookie(self._login_client())
        ready, release, status = threading.Event(), threading.Event(), []
        # A exclusão fica pendente (escrita feita, sem COMMIT) enquanto o polling
        # consulta: a sobreposição é garantida, não deixada ao acaso.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sec004-poll-race") as executor:
            deleted = executor.submit(self._transition, "delete", ready, release)
            try:
                self.assertTrue(ready.wait(8))
                for _ in range(4):
                    status.append(self._poll(self._replay(cookie)).status_code)
            finally:
                release.set()
            self.assertTrue(deleted.result(timeout=12))
        # Antes do COMMIT a empresa ainda está ativa: 200, nunca erro.
        self.assertEqual(status, [200] * 4)
        self._transition("restore")
        self._assert_poll_denied(self._replay(cookie))

    def test_login_started_before_delete_cannot_adopt_the_restored_generation(self):
        before_cookie, release = threading.Event(), threading.Event()
        client = self.app.test_client()
        csrf = self._csrf(client)
        login_user = auth.login_user

        def pause_login(*args, **kwargs):
            before_cookie.set()
            if not release.wait(12):
                raise AssertionError("Login SEC-004 de teste não foi liberado.")
            return login_user(*args, **kwargs)

        try:
            with patch.object(auth, "login_user", side_effect=pause_login):
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sec004-old-login") as executor:
                    logged = executor.submit(client.post, "/auth/login", data={
                        "email": "active@example.invalid", "senha": token_cases.OLD_PASSWORD,
                        "csrf_token": csrf,
                    })
                    try:
                        self.assertTrue(before_cookie.wait(8))
                        self._transition("delete")
                        self._transition("restore")
                    finally:
                        release.set()
                    self.assertEqual(logged.result(timeout=12).status_code, 302)
        finally:
            release.set()
        self._assert_private_denied(client)
        fresh = self._login_client()
        self.assertEqual(fresh.get("/_security/protected").status_code, 200)

    def test_first_access_cookie_cannot_adopt_a_generation_changed_after_its_commit(self):
        client = self._login_client("pending")
        csrf = self._csrf(client, "/auth/primeiro-acesso")
        committed, release = threading.Event(), threading.Event()

        def after_commit(session):
            if threading.current_thread().name.startswith("sec004-first-access"):
                committed.set()
                if not release.wait(12):
                    raise AssertionError("Renovação SEC-004 de teste não foi liberada.")

        event.listen(Session, "after_commit", after_commit)
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="sec004-first-access") as executor:
                changed = executor.submit(client.post, "/auth/primeiro-acesso", data={
                    "nova_senha": token_cases.NEW_PASSWORD,
                    "confirma_senha": token_cases.NEW_PASSWORD, "csrf_token": csrf,
                })
                try:
                    self.assertTrue(committed.wait(8))
                    self._transition("delete")
                    self._transition("restore")
                finally:
                    release.set()
                self.assertEqual(changed.result(timeout=12).status_code, 302)
        finally:
            release.set()
            event.remove(Session, "after_commit", after_commit)
        self._assert_private_denied(client)
        self.assertFalse(self._account("pending")[1])
        fresh = self._login_client("pending", password=token_cases.NEW_PASSWORD)
        self.assertEqual(fresh.get("/_security/protected").status_code, 200)

    def test_password_cas_validated_before_delete_is_rejected_after_restore(self):
        token = self._token()
        with self.app.app_context():
            snapshot = auth._ler_token_reset(token)
            self.assertIsNotNone(snapshot)
            self._transition("delete")
            self._transition("restore")
            self.assertFalse(auth._atualizar_credencial(snapshot, token_cases.NEW_PASSWORD))
        self.assertEqual(self._account()[0], token_cases.FIXTURE_HASH)
        self._assert_rejected(self._post(token))
        self._assert_success(self._post(self._token()))

    def test_company_lock_serializes_new_user_foreign_key_insert(self):
        ready, release = threading.Event(), threading.Event()

        def insert_user():
            with Session(self.engine) as consulta:
                user = Usuario(nome="Synthetic concurrent account", email="new-company@example.invalid",
                               role="CLIENTE", ativo=True, primeiro_acesso=False,
                               id_empresa=self.company_id, senha_hash=token_cases.FIXTURE_HASH)
                consulta.add(user)
                consulta.commit()
                return user.id_usuario

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sec004-insert") as executor:
            deleted = executor.submit(self._transition, "delete", ready, release)
            try:
                self.assertTrue(ready.wait(8))
                inserted = executor.submit(insert_user)
                self._wait_for_blocked_transaction("INSERT INTO usuario")
            finally:
                release.set()
            self.assertTrue(deleted.result(timeout=12))
            identifier = inserted.result(timeout=12)
        with self.app.app_context():
            import session_security
            user = db.session.get(Usuario, identifier)
            self.assertTrue(user.ativo)
            self.assertFalse(session_security.usuario_pode_acessar(user))
        self.assertIsNotNone(self._state()[0])

    def test_permanent_delete_serializes_with_restore_without_orphans(self):
        self._notify(text="Synthetic notification to remove")
        self._transition("delete")
        client = self._login_client("admin")
        csrf = self._csrf(client, "/admin/lixeira")
        locked, release = threading.Event(), threading.Event()

        def after(connection, cursor, statement, parameters, context, executemany):
            if ("FOR UPDATE" in statement.upper() and "empresa" in statement.lower()
                    and threading.current_thread().name.startswith("sec004-permanent")):
                locked.set()
                if not release.wait(12):
                    raise AssertionError("Exclusão permanente SEC-004 de teste não foi liberada.")

        event.listen(self.engine, "after_cursor_execute", after)
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sec004-permanent") as executor:
                permanent = executor.submit(client.post,
                    f"/admin/empresa/excluir-permanente/{self.company_id}",
                    data={"csrf_token": csrf, "senha_confirmacao": token_cases.OLD_PASSWORD})
                try:
                    self.assertTrue(locked.wait(8))
                    restored = executor.submit(self._transition, "restore")
                    self._wait_for_blocked_transaction("empresa")
                finally:
                    release.set()
                self.assertEqual(permanent.result(timeout=12).status_code, 302)
                self.assertFalse(restored.result(timeout=12))
        finally:
            release.set()
            event.remove(self.engine, "after_cursor_execute", after)
        with Session(self.engine) as consulta:
            self.assertIsNone(consulta.get(Empresa, self.company_id))
            self.assertEqual(consulta.scalars(select(Usuario).where(
                Usuario.id_empresa == self.company_id,
            )).all(), [])
            self.assertEqual(consulta.scalars(select(Notificacao).where(
                Notificacao.id_usuario.in_([self.ids["active"], self.ids["pending"]]),
            )).all(), [])
            self.assertIsNotNone(consulta.get(Empresa, self.other_company_id))
            self.assertIsNotNone(consulta.get(Usuario, self.ids["admin"]))

    def test_authorization_generation_is_persisted_and_other_tenant_session_survives(self):
        old = self._login_client()
        other = self._login_client("other")
        admin = self._login_client("admin")
        cookie = old.get_cookie(self.app.config["SESSION_COOKIE_NAME"]).value
        before = self._state()[1]
        self._transition("delete")
        self._transition("restore")
        self.engine.dispose()  # Conexões novas precisam consultar a geração persistida.
        replay = self.app.test_client()
        replay.set_cookie(self.app.config["SESSION_COOKIE_NAME"], cookie)
        self._assert_private_denied(replay)
        self.assertEqual(other.get("/_security/protected").status_code, 200)
        self.assertEqual(admin.get("/_security/protected").status_code, 200)
        after = self._state()[1]
        self.assertEqual(after[self.ids["other"]], before[self.ids["other"]])
        self.assertEqual(after[self.ids["admin"]], before[self.ids["admin"]])
        self.assertEqual(after[self.ids["active"]][1], 1)
        self.assertEqual(self._login_client().get("/_security/protected").status_code, 200)
