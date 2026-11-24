"""NEXO-SEC-003: sessões reais e o polling do sininho com banco/usuários sintéticos."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import secrets
from unittest.mock import patch

from flask import jsonify
from flask_login import current_user, login_required
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash

from . import test_auth_tokens_security as token_cases
import company_security
from extensions import db
import falhas_banco
from horario import agora_utc
from models import Notificacao, SessaoAutenticada, Usuario
import notifications
import session_security


class SessionRevocationCases:
    """Cenários comuns; não é TestCase e não duplica a suíte SEC-002."""

    def _session_routes(self):
        @login_required
        def protected():
            return jsonify(private="synthetic-private-data", uid=current_user.id_usuario,
                           company=current_user.id_empresa)
        self.app.add_url_rule("/_security/protected", "security_protected", protected)

    def _login_client(self, user="active", password=token_cases.OLD_PASSWORD):
        client = self.app.test_client()
        response = client.post("/auth/login", data={
            "email": f"{user}@example.invalid", "senha": password,
            "csrf_token": self._csrf(client),
        })
        self.assertEqual(response.status_code, 302)
        return client

    def _assert_private_denied(self, client):
        response = client.get("/_security/protected")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/login", response.headers["Location"])
        self.assertNotIn("synthetic-private-data", response.get_data(as_text=True))
        with client.session_transaction() as session:
            self.assertNotIn("_user_id", session)

    def _reset_password(self, user="active"):
        token = self._token(user)
        self._assert_success(self._post(token, client=self.app.test_client()))
        return token

    # Polling do sininho (GET /notificacoes/novas), que substituiu o Socket.IO:
    # cada consulta passa pela validação completa da sessão.
    POLL = "/notificacoes/novas"

    def _poll(self, client, desde=0):
        return client.get(f"{self.POLL}?desde={desde}", headers={"Accept": "application/json"})

    def _polled_texts(self, client, desde=0):
        """Textos que o polling entrega a uma sessão VÁLIDA (exige 200: "nada entregue"
        precisa vir de uma consulta autorizada, não de uma negada)."""
        response = self._poll(client, desde)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:200])
        return [item["texto"] for item in response.get_json()["novas"]]

    def _polled_texts_from_other_request(self, client):
        """Polling em outra thread: o cliente de teste reaproveitaria o contexto de
        aplicação aberto pelo teste (e a transação ainda sem COMMIT dele)."""
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(self._polled_texts, client).result(timeout=30)

    def _assert_poll_denied(self, client):
        response = self._poll(client)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"erro": "Sessão encerrada. Entre novamente."})
        return response

    def _notify(self, user="active", text="Synthetic private notification", commit=True):
        with self.app.app_context():
            result = notifications.criar_notificacao(self.ids[user], text, "/cliente/dashboard")
            identifier = result.id_notificacao
            if commit:
                db.session.commit()
            else:
                db.session.rollback()
            return identifier

    def test_login_and_current_session_access_protected_http(self):
        client = self._login_client()
        response = client.get("/_security/protected")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["uid"], self.ids["active"])

    def test_client_session_contains_only_credential_fingerprint_not_hash(self):
        client = self._login_client()
        with client.session_transaction() as session:
            self.assertIn(session_security.CHAVE_CREDENCIAL, session)
            fingerprint = session[session_security.CHAVE_CREDENCIAL]
            self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
            self.assertNotIn(token_cases.FIXTURE_HASH, repr(dict(session)))
            self.assertNotIn(token_cases.OLD_PASSWORD, repr(dict(session)))
        self.assertNotEqual(fingerprint, self._token())

    def test_recovery_revokes_both_preexisting_http_sessions(self):
        first, second = self._login_client(), self._login_client()
        self._reset_password()
        self._assert_private_denied(first)
        self._assert_private_denied(second)

    def test_replayed_old_cookie_is_denied_even_in_new_client(self):
        previous = self._login_client()
        cookie = previous.get_cookie(self.app.config["SESSION_COOKIE_NAME"]).value
        self._reset_password()
        replay = self.app.test_client()
        replay.set_cookie(self.app.config["SESSION_COOKIE_NAME"], cookie)
        self._assert_private_denied(replay)

    def test_new_password_logs_in_and_old_password_does_not(self):
        self._reset_password()
        stale = self.app.test_client()
        response = stale.post("/auth/login", data={
            "email": "active@example.invalid", "senha": token_cases.OLD_PASSWORD,
            "csrf_token": self._csrf(stale),
        })
        self.assertEqual(response.status_code, 200)
        self._assert_private_denied(stale)
        renewed = self._login_client(password=token_cases.NEW_PASSWORD)
        self.assertEqual(renewed.get("/_security/protected").status_code, 200)

    def test_legitimate_external_credential_change_revokes_http_cookie(self):
        client = self._login_client()
        self._change(senha_hash=generate_password_hash("Synthetic-external-password-789!"))
        self._assert_private_denied(client)

    def test_password_reset_revokes_previous_session(self):
        # A política de senha (FASE 6) recusa reusar a senha atual, então o reset
        # usa uma senha nova e válida; a sessão HTTP anterior cai de qualquer modo
        # (SEC-001: a troca de credencial revoga todas as sessões registradas).
        client = self._login_client()
        token = self._token()
        self._assert_success(self._post(token, password=token_cases.NEW_PASSWORD,
                                       client=self.app.test_client()))
        self._assert_private_denied(client)

    def test_legacy_session_without_fingerprint_is_not_accepted(self):
        client = self._login_client()
        with client.session_transaction() as session:
            session.pop(session_security.CHAVE_CREDENCIAL)
        self._assert_private_denied(client)

    def test_session_fingerprint_from_other_user_cannot_authenticate(self):
        first, other = self._login_client(), self._login_client("other")
        with first.session_transaction() as session:
            foreign_fingerprint = session[session_security.CHAVE_CREDENCIAL]
        with other.session_transaction() as session:
            session[session_security.CHAVE_CREDENCIAL] = foreign_fingerprint
        self._assert_private_denied(other)
        self.assertEqual(first.get("/_security/protected").status_code, 200)

    def test_malformed_session_fingerprint_fails_closed(self):
        for value in (None, [], "é" * 64, "0" * 63, "0" * 64):
            with self.subTest(value_type=type(value).__name__):
                client = self._login_client()
                with client.session_transaction() as session:
                    session[session_security.CHAVE_CREDENCIAL] = value
                self._assert_private_denied(client)

    def test_deactivated_user_loses_existing_http_session(self):
        client = self._login_client()
        self._change(ativo=False)
        self._assert_private_denied(client)

    def test_deleted_user_cookie_is_cleared_without_private_response(self):
        client = self._login_client()
        with self.app.app_context():
            db.session.execute(delete(Usuario).where(Usuario.id_usuario == self.ids["active"]))
            db.session.commit()
        self._assert_private_denied(client)

    def test_server_restart_still_invalidates_existing_session(self):
        client = self._login_client()
        self.app.config["BOOT_ID"] = "synthetic-next-boot"
        self._assert_private_denied(client)

    def _with_failing_selects(self, request, marker=None, sentinel="PRIVATE_DB_SENTINEL"):
        """Executa `request()` com os SELECTs falhando (todos, ou só os que citam `marker`)."""
        def fail_select(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT") and (marker is None or marker in statement):
                raise SQLAlchemyError(sentinel)
        event.listen(self.engine, "before_cursor_execute", fail_select)
        try:
            return request()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_select)

    def _assert_unavailable_page(self, response, *private):
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], str(falhas_banco.RETRY_AFTER_S))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.get_data(as_text=True)
        for text in ("PRIVATE_DB_SENTINEL", "synthetic-private-data", *private):
            self.assertNotIn(text, body)
        return body

    def test_database_read_failure_denies_http_and_keeps_the_session(self):
        # Banco fora do ar não é sessão inválida: 503 sem dado privado, mas o
        # cookie fica intacto e a sessão volta a valer quando o banco volta.
        client = self._login_client()
        response = self._with_failing_selects(lambda: client.get("/_security/protected"))
        body = self._assert_unavailable_page(response)
        self.assertIn(falhas_banco.MENSAGEM, body)
        with client.session_transaction() as session:
            self.assertEqual(session["_user_id"], str(self.ids["active"]))
            self.assertIn(session_security.CHAVE_SESSAO, session)
        response = client.get("/_security/protected")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["uid"], self.ids["active"])
        self.assertEqual(self._session_rows(), [(False, None)])

    def test_authorization_query_failure_after_user_load_keeps_session_and_hides_page(self):
        # O usuário carrega, mas a consulta da sessão registrada falha: a resposta
        # sai como anônima (sem nome, sininho ou dado privado) e o cookie fica.
        client = self._login_client()
        self._notify(text="PRIVATE_DURING_AUTHORIZATION_FAILURE")
        response = self._with_failing_selects(lambda: client.get("/cliente/dashboard"),
                                              marker="sessao_autenticada")
        body = self._assert_unavailable_page(response, "PRIVATE_DURING_AUTHORIZATION_FAILURE",
                                             "bellBadge", "nexoToasts")
        self.assertIn(falhas_banco.MENSAGEM, body)
        with client.session_transaction() as session:
            self.assertEqual(session["_user_id"], str(self.ids["active"]))
        self.assertEqual(client.get("/_security/protected").status_code, 200)

    def test_logout_during_database_outage_says_the_session_continues(self):
        # Quem clica em Sair com o banco fora precisa saber que NÃO saiu (computador
        # compartilhado); depois que o banco volta, sair funciona normalmente.
        client = self._login_client()
        csrf = self._csrf(client, "/auth/logout")
        response = self._with_failing_selects(
            lambda: client.post("/auth/logout", data={"csrf_token": csrf}))
        body = self._assert_unavailable_page(response)
        self.assertIn("Você continua conectado", body)
        self.assertNotIn("Sessão terminada", body)
        with client.session_transaction() as session:
            self.assertEqual(session["_user_id"], str(self.ids["active"]))
        self.assertEqual(self._session_rows(), [(False, None)])
        self._logout(client)
        self.assertEqual(self._session_rows(), [(True, "logout")])

    def test_database_failure_never_revives_a_revoked_session(self):
        client = self._login_client()
        with self.app.app_context():
            self.assertEqual(session_security.revogar_sessoes_usuario(
                db.session, self.ids["active"], "revogacao_global"), 1)
            db.session.commit()
        response = self._with_failing_selects(lambda: client.get("/_security/protected"))
        self._assert_unavailable_page(response)
        self._assert_private_denied(client)
        self._assert_poll_denied(client)

    def test_first_access_renews_only_current_session(self):
        first, second = self._login_client("pending"), self._login_client("pending")
        csrf = self._csrf(first, "/auth/primeiro-acesso")
        response = first.post("/auth/primeiro-acesso", data={
            "nova_senha": token_cases.NEW_PASSWORD, "confirma_senha": token_cases.NEW_PASSWORD,
            "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/cliente/dashboard"))
        self.assertEqual(first.get("/_security/protected").status_code, 200)
        self._assert_private_denied(second)
        self.assertFalse(self._account("pending")[1])

    def test_activation_link_invalidates_logged_pending_session(self):
        client = self._login_client("pending")
        token = self._token("pending", "activation")
        self._assert_success(self._post(token, "activation", client=self.app.test_client()))
        self._assert_private_denied(client)

    def test_password_reset_does_not_revoke_other_user_or_company(self):
        changed, other = self._login_client(), self._login_client("other")
        self._reset_password()
        self._assert_private_denied(changed)
        response = other.get("/_security/protected")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["uid"], self.ids["other"])

    def test_valid_session_polls_and_anonymous_gets_401(self):
        response = self._poll(self._login_client())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.get_json()), {"nao_lidas", "novas", "ultimo"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self._assert_poll_denied(self.app.test_client())

    def test_poll_of_pending_first_access_delivers_nothing(self):
        client = self._login_client("pending")
        self._notify("pending", text="PRIVATE_PENDING_FIRST_ACCESS")
        response = self._poll(client)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/auth/primeiro-acesso"))
        self.assertNotIn("PRIVATE_PENDING_FIRST_ACCESS", response.get_data(as_text=True))

    def test_poll_rejects_previous_credential_cookie(self):
        client = self._login_client()
        self._reset_password()
        self._assert_poll_denied(client)

    def test_every_poll_rechecks_the_persisted_credential(self):
        client = self._login_client()
        self._notify(text="PRIVATE_BEFORE_EXTERNAL_CHANGE")
        self.assertIn("PRIVATE_BEFORE_EXTERNAL_CHANGE", self._polled_texts(client))
        self._change(senha_hash=generate_password_hash("Synthetic-external-password-789!"))
        self._notify(text="PRIVATE_AFTER_EXTERNAL_CHANGE")
        response = self._assert_poll_denied(client)
        self.assertNotIn("PRIVATE_AFTER_EXTERNAL_CHANGE", response.get_data(as_text=True))

    def test_poll_denied_after_credential_change_made_outside_the_app(self):
        # Sem callback local de revogação (troca por SQL direto, outro processo):
        # a credencial persistida basta para negar a consulta seguinte.
        cookie = self._cookie(self._login_client())
        self._change(senha_hash=generate_password_hash("Synthetic-external-password-789!"))
        self._assert_poll_denied(self._replay(cookie))

    def test_poll_denied_after_server_restart(self):
        client = self._login_client()
        self.app.config["BOOT_ID"] = "synthetic-next-boot"
        self._assert_poll_denied(client)

    def test_poll_denied_for_deactivated_user(self):
        client = self._login_client()
        self._change(ativo=False)
        self._assert_poll_denied(client)

    def test_database_error_on_poll_returns_503_without_private_data_and_keeps_cookie(self):
        client = self._login_client()
        self._notify(text="PRIVATE_DB_FAILURE_EVENT")
        response = self._with_failing_selects(lambda: self._poll(client), sentinel="PRIVATE_SOCKET_DB_SENTINEL")
        body = self._assert_unavailable_page(response, "PRIVATE_DB_FAILURE_EVENT", "PRIVATE_SOCKET_DB_SENTINEL")
        self.assertEqual(response.get_json()["erro"], falhas_banco.MENSAGEM)
        self.assertNotIn("<html", body.lower())
        with client.session_transaction() as session:
            self.assertEqual(session["_user_id"], str(self.ids["active"]))
        self.assertIn("PRIVATE_DB_FAILURE_EVENT", self._polled_texts(client))

    def test_password_recovery_denies_preexisting_poll(self):
        client = self._login_client()
        self.assertEqual(self._poll(client).status_code, 200)
        self._reset_password()
        self._assert_poll_denied(client)

    def test_password_change_preserves_other_company_poll_and_private_delivery(self):
        changed, other = self._login_client(), self._login_client("other")
        self._reset_password()
        self._assert_poll_denied(changed)
        self._notify("other", text="OTHER_COMPANY_STILL_AUTHORIZED")
        self.assertIn("OTHER_COMPANY_STILL_AUTHORIZED", self._polled_texts(other))

    def test_revoked_session_never_polls_new_private_notification(self):
        cookie = self._cookie(self._login_client())
        self._change(senha_hash=generate_password_hash("Synthetic-external-password-789!"))
        self._notify(text="PRIVATE_AFTER_REVOCATION")
        response = self._assert_poll_denied(self._replay(cookie))
        self.assertNotIn("PRIVATE_AFTER_REVOCATION", response.get_data(as_text=True))

    def test_notifications_are_isolated_between_users_and_companies(self):
        first, other = self._login_client(), self._login_client("other")
        self._notify(text="PRIVATE_COMPANY_ONE")
        data = self._poll(first).get_json()
        self.assertEqual([item["texto"] for item in data["novas"]], ["PRIVATE_COMPANY_ONE"])
        self.assertEqual(set(data["novas"][0]), {"id", "texto", "link", "hora", "hora_completa", "instante",
                                                 "badge", "badge_rotulo"})
        self.assertNotIn("PRIVATE_COMPANY_ONE", self._polled_texts(other))
        # O navegador manda o maior id que já tem: a mesma notificação não volta.
        self.assertEqual(self._polled_texts(first, desde=data["ultimo"]), [])

    def test_poll_returns_only_unread_newer_than_desde_and_the_unread_count(self):
        client = self._login_client()
        first = self._notify(text="UNREAD_FIRST")
        self._notify(text="UNREAD_SECOND")
        data = self._poll(client, desde=first).get_json()
        self.assertEqual([item["texto"] for item in data["novas"]], ["UNREAD_SECOND"])
        self.assertEqual(data["nao_lidas"], 2)
        with self.app.app_context():
            db.session.execute(update(Notificacao).where(Notificacao.id_notificacao > first)
                               .values(lida=True))
            db.session.commit()
        data = self._poll(client, desde=first).get_json()
        self.assertEqual((data["novas"], data["nao_lidas"]), ([], 1))
        for strange in ("abc", "-1", "1e9", "9" * 11, "", "+1"):
            with self.subTest(desde=strange):
                response = client.get(f"{self.POLL}?desde={strange}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["nao_lidas"], 1)

    def test_new_poll_after_new_login_receives_current_generation_only(self):
        old = self._login_client()
        self._reset_password()
        new = self._login_client(password=token_cases.NEW_PASSWORD)
        self._notify(text="CURRENT_GENERATION_PRIVATE")
        self.assertNotIn("CURRENT_GENERATION_PRIVATE", self._assert_poll_denied(old).get_data(as_text=True))
        self.assertIn("CURRENT_GENERATION_PRIVATE", self._polled_texts(new))

    def test_notification_is_visible_only_after_business_commit(self):
        client = self._login_client()
        with self.app.app_context():
            notification = notifications.criar_notificacao(self.ids["active"], "COMMITTED_PRIVATE", "/")
            self.assertIsNotNone(notification.id_notificacao)
            self.assertNotIn("COMMITTED_PRIVATE", self._polled_texts_from_other_request(client))
            db.session.commit()
        self.assertIn("COMMITTED_PRIVATE", self._polled_texts(client))

    def test_notification_rollback_shows_nothing_and_persists_nothing(self):
        client = self._login_client()
        identifier = self._notify(text="ROLLED_BACK_PRIVATE", commit=False)
        self.assertNotIn("ROLLED_BACK_PRIVATE", self._polled_texts(client))
        with self.app.app_context():
            self.assertIsNone(db.session.get(Notificacao, identifier))

    def test_savepoint_commit_is_not_visible_before_outer_commit(self):
        client = self._login_client()
        with self.app.app_context():
            notifications.criar_notificacao(self.ids["active"], "OUTER_COMMITTED_PRIVATE", "/")
            with db.session.begin_nested():
                notifications.criar_notificacao(self.ids["active"], "NESTED_COMMITTED_PRIVATE", "/")
            self.assertEqual(self._polled_texts_from_other_request(client), [])
            db.session.commit()
        texts = self._polled_texts(client)
        self.assertIn("OUTER_COMMITTED_PRIVATE", texts)
        self.assertIn("NESTED_COMMITTED_PRIVATE", texts)

    def test_savepoint_rollback_preserves_outer_notification_only(self):
        client = self._login_client()
        with self.app.app_context():
            outer = notifications.criar_notificacao(self.ids["active"], "OUTER_STILL_COMMITTED_PRIVATE", "/")
            outer_id = outer.id_notificacao
            try:
                with db.session.begin_nested():
                    inner = notifications.criar_notificacao(self.ids["active"], "NESTED_ROLLED_BACK_PRIVATE", "/")
                    inner_id = inner.id_notificacao
                    raise ValueError("synthetic savepoint rollback")
            except ValueError:
                pass
            self.assertEqual(self._polled_texts_from_other_request(client), [])
            db.session.commit()
            self.assertIsNotNone(db.session.get(Notificacao, outer_id))
            self.assertIsNone(db.session.get(Notificacao, inner_id))
        texts = self._polled_texts(client)
        self.assertIn("OUTER_STILL_COMMITTED_PRIVATE", texts)
        self.assertNotIn("NESTED_ROLLED_BACK_PRIVATE", texts)

    def test_outer_rollback_discards_already_released_savepoint_notifications(self):
        client = self._login_client()
        with self.app.app_context():
            outer = notifications.criar_notificacao(self.ids["active"], "OUTER_ROLLBACK_PRIVATE", "/")
            outer_id = outer.id_notificacao
            with db.session.begin_nested():
                inner = notifications.criar_notificacao(self.ids["active"], "INNER_ROLLBACK_PRIVATE", "/")
                inner_id = inner.id_notificacao
            db.session.rollback()
            self.assertIsNone(db.session.get(Notificacao, outer_id))
            self.assertIsNone(db.session.get(Notificacao, inner_id))
        self.assertEqual(self._polled_texts(client), [])

    def test_failed_password_commit_does_not_revoke_valid_session(self):
        client = self._login_client()
        token = self._token()
        anonymous = self.app.test_client()
        csrf = self._csrf(anonymous)
        with patch.object(db.session, "commit", side_effect=SQLAlchemyError("PRIVATE_COMMIT_SENTINEL")):
            self._assert_rejected(self._post(token, client=anonymous, csrf=csrf))
        self.assertEqual(client.get("/_security/protected").status_code, 200)
        self.assertEqual(self._poll(client).status_code, 200)
        self.assertEqual(self._account()[0], token_cases.FIXTURE_HASH)

    # --- SEC-001 (segunda auditoria): sessões registradas e revogáveis ------

    def _cookie(self, client):
        return client.get_cookie(self.app.config["SESSION_COOKIE_NAME"]).value

    def _replay(self, cookie):
        client = self.app.test_client()
        client.set_cookie(self.app.config["SESSION_COOKIE_NAME"], cookie)
        return client

    def _logout(self, client):
        response = client.post("/auth/logout", data={"csrf_token": self._csrf(client, "/auth/logout")})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/auth/login"))
        return response

    def _session_rows(self, user="active"):
        with self.app.app_context():
            rows = db.session.scalars(select(SessaoAutenticada).where(
                SessaoAutenticada.id_usuario == self.ids[user],
            ).order_by(SessaoAutenticada.id_sessao))
            return [(row.revogada_em is not None, row.motivo_revogacao) for row in rows]

    def test_login_registers_one_session_row_holding_only_a_hash(self):
        client = self._login_client()
        with client.session_transaction() as session:
            token = session[session_security.CHAVE_SESSAO]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        with self.app.app_context():
            row = db.session.scalar(select(SessaoAutenticada).where(
                SessaoAutenticada.id_usuario == self.ids["active"]))
            self.assertEqual(row.identificador_hash, session_security.hash_token_sessao(token))
            self.assertNotIn(token, repr(row.__dict__))
            self.assertIsNone(row.revogada_em)
            self.assertGreater(row.expira_em, row.criada_em)

    def test_logout_revokes_current_session_and_rejects_replayed_cookie(self):
        client = self._login_client()
        cookie = self._cookie(client)
        self._logout(client)
        self._assert_private_denied(client)
        self._assert_private_denied(self._replay(cookie))
        self.assertEqual(self._session_rows(), [(True, "logout")])

    def test_logout_by_get_only_confirms_and_keeps_the_session(self):
        client = self._login_client()
        response = client.get("/auth/logout")
        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/auth/logout"', response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(client.get("/_security/protected").status_code, 200)
        self.assertEqual(self._session_rows(), [(False, None)])

    def test_logout_without_csrf_token_does_not_end_the_session(self):
        client = self._login_client()
        self.assertEqual(client.post("/auth/logout", data={}).status_code, 400)
        self.assertEqual(client.get("/_security/protected").status_code, 200)
        self.assertEqual(self._session_rows(), [(False, None)])

    def test_logout_denies_poll_and_replayed_cookie(self):
        client = self._login_client()
        cookie = self._cookie(client)
        self.assertEqual(self._poll(client).status_code, 200)
        self._logout(client)
        self._assert_poll_denied(client)
        self._assert_poll_denied(self._replay(cookie))

    def test_logout_of_one_session_preserves_the_other_session_of_the_same_user(self):
        first, second = self._login_client(), self._login_client()
        self._logout(first)
        self._assert_private_denied(first)
        self.assertEqual(second.get("/_security/protected").status_code, 200)
        self.assertEqual(sorted(self._session_rows()), [(False, None), (True, "logout")])

    def test_notification_after_logout_reaches_only_the_surviving_session(self):
        first, second = self._login_client(), self._login_client()
        gone = self._cookie(first)
        self._logout(first)
        self._notify(text="PRIVATE_AFTER_LOGOUT")
        self.assertNotIn("PRIVATE_AFTER_LOGOUT", self._assert_poll_denied(self._replay(gone)).get_data(as_text=True))
        self.assertIn("PRIVATE_AFTER_LOGOUT", self._polled_texts(second))

    def test_session_revoked_in_database_is_denied_on_the_next_poll(self):
        # Revogação feita em outro processo: não há varredura nem conexão a
        # derrubar; a consulta seguinte já é negada.
        client = self._login_client()
        with self.app.app_context():
            self.assertEqual(session_security.revogar_sessoes_usuario(
                db.session, self.ids["active"], "revogacao_global"), 1)
            db.session.commit()
        self._notify(text="PRIVATE_AFTER_REMOTE_REVOCATION")
        response = self._assert_poll_denied(client)
        self.assertNotIn("PRIVATE_AFTER_REMOTE_REVOCATION", response.get_data(as_text=True))
        self._assert_private_denied(client)

    def test_password_change_revokes_every_session_row_of_the_user_only(self):
        first, second, other = self._login_client(), self._login_client(), self._login_client("other")
        self._reset_password()
        self.assertEqual(self._session_rows(), [(True, "troca_senha"), (True, "troca_senha")])
        self.assertEqual(self._session_rows("other"), [(False, None)])
        self._assert_private_denied(first)
        self._assert_private_denied(second)
        self.assertEqual(other.get("/_security/protected").status_code, 200)

    def test_expired_session_row_is_denied_even_with_an_intact_cookie(self):
        client = self._login_client()
        with self.app.app_context():
            # Sessão criada há 9 h e vencida há 1 h (respeita ck_sessao_validade).
            db.session.execute(update(SessaoAutenticada).where(
                SessaoAutenticada.id_usuario == self.ids["active"],
            ).values(criada_em=agora_utc() - timedelta(hours=9),
                     expira_em=agora_utc() - timedelta(hours=1)))
            db.session.commit()
        self._assert_poll_denied(self._replay(self._cookie(client)))
        self._assert_private_denied(client)

    def test_unknown_or_malformed_session_identifier_is_denied(self):
        for value in (None, "", "x" * 43, secrets.token_urlsafe(32), "é" * 43, 123, "A" * 44):
            with self.subTest(value=repr(value)[:16]):
                client = self._login_client()
                with client.session_transaction() as session:
                    session[session_security.CHAVE_SESSAO] = value
                self._assert_private_denied(client)

    def test_session_identifier_of_another_user_is_denied(self):
        first, other = self._login_client(), self._login_client("other")
        with first.session_transaction() as session:
            foreign = session[session_security.CHAVE_SESSAO]
        with other.session_transaction() as session:
            session[session_security.CHAVE_SESSAO] = foreign
        self._assert_private_denied(other)
        self.assertEqual(first.get("/_security/protected").status_code, 200)

    def test_login_rotates_the_session_and_drops_anonymous_state(self):
        client = self.app.test_client()
        csrf = self._csrf(client)
        with client.session_transaction() as session:
            session["anonymous_sentinel"] = "PRE_LOGIN_STATE"
        response = client.post("/auth/login", data={
            "email": "active@example.invalid", "senha": token_cases.OLD_PASSWORD, "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertNotIn("anonymous_sentinel", session)
            first_token = session[session_security.CHAVE_SESSAO]
        with self._login_client().session_transaction() as session:
            self.assertNotEqual(session[session_security.CHAVE_SESSAO], first_token)

    def test_database_failure_while_registering_the_session_denies_login(self):
        client = self.app.test_client()
        csrf = self._csrf(client)
        with patch.object(db.session, "commit", side_effect=SQLAlchemyError("PRIVATE_REGISTER_SENTINEL")):
            response = client.post("/auth/login", data={
                "email": "active@example.invalid", "senha": token_cases.OLD_PASSWORD, "csrf_token": csrf,
            })
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PRIVATE_REGISTER_SENTINEL", response.get_data(as_text=True))
        with client.session_transaction() as session:
            self.assertNotIn("_user_id", session)
            self.assertNotIn(session_security.CHAVE_SESSAO, session)
        self._assert_private_denied(client)
        self.assertEqual(self._session_rows(), [])

    def test_database_failure_during_logout_does_not_claim_success(self):
        client = self._login_client()
        with patch.object(db.session, "commit", side_effect=SQLAlchemyError("PRIVATE_LOGOUT_SENTINEL")):
            response = client.post("/auth/logout", data={"csrf_token": self._csrf(client, "/auth/logout")})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PRIVATE_LOGOUT_SENTINEL", response.get_data(as_text=True))
        self.assertNotIn("Sessão terminada", response.get_data(as_text=True))
        self.assertEqual(self._session_rows(), [(False, None)])
        self._logout(client)
        self.assertEqual(self._session_rows(), [(True, "logout")])

    def test_company_trash_revokes_sessions_and_restore_does_not_revive_them(self):
        client = self._login_client()
        cookie = self._cookie(client)
        with self.app.app_context():
            empresa = db.session.get(Usuario, self.ids["active"]).id_empresa
            self.assertIsNotNone(company_security.excluir_empresa(db.session, empresa))
            db.session.commit()
        self.assertEqual(self._session_rows(), [(True, "empresa")])
        self._assert_private_denied(client)
        self._notify(text="PRIVATE_AFTER_TRASH")
        self.assertNotIn("PRIVATE_AFTER_TRASH", self._assert_poll_denied(self._replay(cookie)).get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNotNone(company_security.restaurar_empresa(db.session, empresa))
            db.session.commit()
        self._assert_private_denied(self._replay(cookie))
        self.assertEqual(self._login_client().get("/_security/protected").status_code, 200)

    def test_new_login_cleans_only_revoked_or_expired_rows_of_that_user(self):
        first = self._login_client()
        self._logout(first)
        other = self._login_client("other")
        self._login_client()
        self.assertEqual(self._session_rows(), [(False, None)])
        self.assertEqual(self._session_rows("other"), [(False, None)])
        self.assertEqual(other.get("/_security/protected").status_code, 200)


class SessionRevocationSecurityTests(SessionRevocationCases, token_cases._TokenCase):
    def setUp(self):
        super().setUp()
        self._session_routes()
