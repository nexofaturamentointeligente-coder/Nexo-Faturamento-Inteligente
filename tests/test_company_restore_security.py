"""SEC-004: lixeira, autorização e restauração com dados sintéticos.

SQLite em diretório temporário; endpoints reais, CSRF ativo e inspeção do
estado persistido. Concorrência e migração possuem suítes próprias PostgreSQL
e Alembic: estes casos não substituem essas verificações.
"""

from datetime import date, datetime, timezone
import hashlib
import hmac

from sqlalchemy import event, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from . import test_auth_tokens_security as token_cases
from . import test_session_revocation_security as session_cases
from blueprints import auth
from extensions import db
from models import (
    Analise, ChamadoSuporte, Empresa, MensagemSuporte, Notificacao, Usuario,
)
import session_security


class CompanyRestoreSecurityTests(token_cases._TokenCase):
    # Reutilizar helpers, não herdar/duplicar os testes históricos SEC-003.
    _session_routes = session_cases.SessionRevocationCases._session_routes
    _login_client = session_cases.SessionRevocationCases._login_client
    _assert_private_denied = session_cases.SessionRevocationCases._assert_private_denied
    _notify = session_cases.SessionRevocationCases._notify
    # Polling do sininho (substituiu o Socket.IO): GET /notificacoes/novas.
    POLL = session_cases.SessionRevocationCases.POLL
    _poll = session_cases.SessionRevocationCases._poll
    _polled_texts = session_cases.SessionRevocationCases._polled_texts
    _assert_poll_denied = session_cases.SessionRevocationCases._assert_poll_denied

    def setUp(self):
        super().setUp()
        self._session_routes()
        with self.app.app_context():
            first = db.session.get(Usuario, self.ids["active"])
            other = db.session.get(Usuario, self.ids["other"])
            self.company_id, self.other_company_id = first.id_empresa, other.id_empresa
            # Empresa A: ativo, primeiro acesso ativo e bloqueado previamente.
            # Empresa B: outro usuário ativo; ADMIN global não tem empresa.
            db.session.get(Usuario, self.ids["inactive"]).id_empresa = self.company_id
            db.session.commit()
        self.admin_client = self._login_client("admin")

    def _action(self, operation, company=None, client=None, *, csrf=True):
        client = self.admin_client if client is None else client
        identifier = self.company_id if company is None else company
        data = {"csrf_token": self._csrf(client, "/admin/empresas")} if csrf else {}
        if operation == "excluir-permanente" and csrf:
            data["senha_confirmacao"] = token_cases.OLD_PASSWORD  # step-up da exclusão irreversível
        return client.post(f"/admin/empresa/{operation}/{identifier}", data=data)

    def _trash(self):
        response = self._action("excluir")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Empresa, self.company_id).deletado_em)
        return response

    def _restore(self):
        response = self._action("restaurar")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Empresa, self.company_id).deletado_em)
        return response

    def _users(self):
        with self.app.app_context():
            return {
                account.id_usuario: {
                    "active": account.ativo, "generation": account.geracao_autorizacao,
                    "hash": account.senha_hash, "role": account.role,
                    "company": account.id_empresa, "first_access": account.primeiro_acesso,
                }
                for account in db.session.scalars(select(Usuario)).all()
            }

    def _company(self):
        with self.app.app_context():
            company = db.session.get(Empresa, self.company_id)
            return company.deletado_em, company.status_conta

    def _attempt_login(self, user="active"):
        client = self.app.test_client()
        response = client.post("/auth/login", data={
            "email": f"{user}@example.invalid", "senha": token_cases.OLD_PASSWORD,
            "csrf_token": self._csrf(client),
        })
        return client, response

    def _cookie(self, client):
        return client.get_cookie(self.app.config["SESSION_COOKIE_NAME"]).value

    def _replay(self, cookie):
        replay = self.app.test_client()
        replay.set_cookie(self.app.config["SESSION_COOKIE_NAME"], cookie)
        return replay

    def test_soft_delete_preserves_individual_administrative_states_but_blocks_access(self):
        active = self._login_client()
        pending = self._login_client("pending")
        before = self._users()
        self._trash()
        after = self._users()
        for name in ("active", "pending", "inactive"):
            identifier = self.ids[name]
            self.assertEqual(after[identifier]["active"], before[identifier]["active"])
            self.assertEqual(after[identifier]["generation"], before[identifier]["generation"] + 1)
        self._assert_private_denied(active)
        self._assert_private_denied(pending)
        for name in ("active", "pending", "inactive"):
            client, _ = self._attempt_login(name)
            self._assert_private_denied(client)

    def test_restore_keeps_previously_blocked_user_and_allows_legitimate_new_login(self):
        self._trash()
        self._restore()
        users = self._users()
        self.assertTrue(users[self.ids["active"]]["active"])
        self.assertTrue(users[self.ids["pending"]]["active"])
        self.assertFalse(users[self.ids["inactive"]]["active"])
        self.assertEqual(self._login_client().get("/_security/protected").status_code, 200)
        blocked, _ = self._attempt_login("inactive")
        self._assert_private_denied(blocked)

    def test_restore_does_not_change_passwords_roles_first_access_or_company_bindings(self):
        before = self._users()
        self._trash()
        self._restore()
        after = self._users()
        for identifier, account in before.items():
            for field in ("active", "hash", "role", "company", "first_access"):
                self.assertEqual(after[identifier][field], account[field], (identifier, field))

    def test_other_company_and_global_admin_keep_states_cookies_and_polling(self):
        other = self._login_client("other")
        before = self._users()
        self._trash()
        self._restore()
        after = self._users()
        for name in ("other", "admin"):
            self.assertEqual(after[self.ids[name]], before[self.ids[name]])
        self.assertEqual(other.get("/_security/protected").status_code, 200)
        self.assertEqual(self.admin_client.get("/admin/lixeira").status_code, 200)
        self._notify("other", "ONLY_OTHER_COMPANY")
        self.assertIn("ONLY_OTHER_COMPANY", self._polled_texts(other))
        admin_poll = self._poll(self.admin_client)
        self.assertEqual(admin_poll.status_code, 200)
        self.assertNotIn("ONLY_OTHER_COMPANY", admin_poll.get_data(as_text=True))

    def test_legacy_trash_unknown_previous_states_remain_blocked(self):
        # Estado conservador produzido pelo upgrade SEC-004 para lixeira antiga.
        # A própria execução Alembic é verificada em suíte de migração separada.
        with self.app.app_context():
            company = db.session.get(Empresa, self.company_id)
            company.deletado_em = datetime(2026, 1, 1, tzinfo=timezone.utc)
            db.session.execute(update(Usuario).where(
                Usuario.id_empresa == self.company_id,
            ).values(ativo=False, geracao_autorizacao=1))
            db.session.commit()
        self._restore()
        for name in ("active", "pending", "inactive"):
            account = self._users()[self.ids[name]]
            self.assertFalse(account["active"])
            self.assertEqual(account["generation"], 1)
            client, _ = self._attempt_login(name)
            self._assert_private_denied(client)
        with self.admin_client.session_transaction() as data:
            messages = " ".join(str(item[1]) for item in data.get("_flashes", ()))
        self.assertIn("bloquead", messages.lower())
        self.assertIn("revis", messages.lower())
        self.assertNotIn(token_cases.FIXTURE_HASH, messages)
        self.assertNotIn("active@example.invalid", messages)

    def test_repeated_restore_does_not_add_permissions_or_change_generation(self):
        self._trash()
        self._restore()
        before = self._users()
        self._action("restaurar")
        self.assertEqual(self._users(), before)
        self.assertIsNone(self._company()[0])

    def test_repeated_delete_does_not_increment_generation_again(self):
        self._trash()
        before = self._users(), self._company()
        self._action("excluir")
        self.assertEqual((self._users(), self._company()), before)

    def test_successive_cycles_preserve_accounts_and_increment_each_revocation(self):
        for generation in (1, 2, 3):
            self._trash()
            self._restore()
            users = self._users()
            self.assertTrue(users[self.ids["active"]]["active"])
            self.assertFalse(users[self.ids["inactive"]]["active"])
            self.assertEqual(users[self.ids["active"]]["generation"], generation)
            self.assertEqual(users[self.ids["inactive"]]["generation"], generation)

    def test_administrative_deactivation_during_trash_is_not_overwritten(self):
        self._trash()
        # Não existe tela de alteração de ativo: simular decisão persistida
        # por operador autorizado, sem criar atalho de autorização na aplicação.
        self._change(ativo=False)
        self._restore()
        self.assertFalse(self._users()[self.ids["active"]]["active"])
        client, _ = self._attempt_login()
        self._assert_private_denied(client)

    def test_delete_failure_rolls_back_company_marker_and_all_user_generations(self):
        before = self._users(), self._company()
        company_updated = []
        def fail_user_write(connection, cursor, statement, parameters, context, executemany):
            sql = statement.lstrip().upper()
            if sql.startswith("UPDATE EMPRESA SET"):
                company_updated.append(True)
            if sql.startswith("UPDATE USUARIO SET"):
                raise SQLAlchemyError("SYNTHETIC_PRIVATE_PERSISTENCE_FAILURE")
        event.listen(self.engine, "before_cursor_execute", fail_user_write)
        try:
            response = self._action("excluir")
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_user_write)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(company_updated)
        self.assertEqual((self._users(), self._company()), before)
        self.assertNotIn("SYNTHETIC_PRIVATE_PERSISTENCE_FAILURE", response.get_data(as_text=True))

    def test_restore_failure_keeps_company_trashed_and_users_unchanged(self):
        self._trash()
        before = self._users(), self._company()
        def fail_company_write(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE EMPRESA SET"):
                raise SQLAlchemyError("SYNTHETIC_RESTORE_FAILURE")
        event.listen(self.engine, "before_cursor_execute", fail_company_write)
        try:
            response = self._action("restaurar")
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_company_write)
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self._users(), self._company()), before)

    def test_all_lifecycle_posts_require_valid_csrf(self):
        before = self._users(), self._company()
        for operation in ("excluir", "restaurar", "excluir-permanente"):
            for data in ({}, {"csrf_token": "synthetic-invalid-token"}):
                with self.subTest(operation=operation, supplied=bool(data)):
                    response = self.admin_client.post(
                        f"/admin/empresa/{operation}/{self.company_id}", data=data,
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual((self._users(), self._company()), before)

    def test_non_admin_client_cannot_delete_restore_or_permanently_delete(self):
        client = self._login_client()
        csrf = self._csrf(client, "/cliente/dashboard")
        before = self._users(), self._company()
        for operation in ("excluir", "restaurar", "excluir-permanente"):
            response = client.post(f"/admin/empresa/{operation}/{self.company_id}", data={"csrf_token": csrf})
            self.assertEqual(response.status_code, 403)
            self.assertEqual((self._users(), self._company()), before)

    def test_lifecycle_gets_are_not_mutating_endpoints(self):
        before = self._users(), self._company()
        for operation in ("excluir", "restaurar", "excluir-permanente"):
            self.assertEqual(self.admin_client.get(f"/admin/empresa/{operation}/{self.company_id}").status_code, 405)
        self.assertEqual((self._users(), self._company()), before)

    def test_old_cookie_never_recovers_validity_after_restore_without_intermediate_request(self):
        old = self._login_client()
        cookie = self._cookie(old)
        self._trash()
        self._restore()
        self._assert_private_denied(self._replay(cookie))
        self._assert_private_denied(old)

    def test_new_login_after_restore_has_new_fingerprint_without_password_change(self):
        old = self._login_client()
        with old.session_transaction() as data:
            previous = data[session_security.CHAVE_CREDENCIAL]
        password_before = self._account()[0]
        self._trash()
        self._restore()
        renewed = self._login_client()
        with renewed.session_transaction() as data:
            self.assertNotEqual(data[session_security.CHAVE_CREDENCIAL], previous)
        self.assertEqual(self._account()[0], password_before)
        self.assertEqual(renewed.get("/_security/protected").status_code, 200)

    def test_legacy_cookie_without_credential_fingerprint_stays_rejected_after_restore(self):
        old = self._login_client()
        with old.session_transaction() as data:
            data.pop(session_security.CHAVE_CREDENCIAL)
        cookie = self._cookie(old)
        self._trash()
        self._restore()
        self._assert_private_denied(self._replay(cookie))

    def test_recovery_and_activation_tokens_before_delete_do_not_revive(self):
        recovery = self._token()
        activation = self._token("pending", "activation")
        self._trash()
        self._restore()
        self._assert_rejected(self._post(recovery, client=self.app.test_client()))
        self._assert_rejected(self._post(activation, "activation", client=self.app.test_client()))
        self.assertEqual(self._account()[0], token_cases.FIXTURE_HASH)
        self.assertEqual(self._account("pending")[0], token_cases.FIXTURE_HASH)
        self.assertTrue(self._account("pending")[1])

    def test_trash_blocks_password_links_and_sends_no_recovery_or_activation_mail(self):
        recovery = self._token()
        activation = self._token("pending", "activation")
        self._trash()
        self.recovery_mail.reset_mock()
        self.activation_mail.reset_mock()
        for user in ("active", "pending"):
            self._request_link(f"{user}@example.invalid", self.app.test_client())
        self.recovery_mail.assert_not_called()
        self.activation_mail.assert_not_called()
        self._assert_rejected(self._post(recovery, client=self.app.test_client()))
        self._assert_rejected(self._post(activation, "activation", client=self.app.test_client()))

    def test_restored_first_access_still_requires_credential_definition(self):
        self._trash()
        self._restore()
        pending = self._login_client("pending")
        response = pending.get("/_security/protected")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/auth/primeiro-acesso"))
        self.assertEqual(self._poll(pending).status_code, 302)
        self._assert_success(self._post(self._token("pending", "activation"), "activation", client=self.app.test_client()))
        self.assertFalse(self._account("pending")[1])
        renewed = self._login_client("pending", password=token_cases.NEW_PASSWORD)
        self.assertEqual(renewed.get("/_security/protected").status_code, 200)

    def test_trashed_company_blocks_even_cookie_stamped_with_current_generation(self):
        self._trash()
        forged = self.app.test_client()
        with self.app.app_context():
            account = db.session.get(Usuario, self.ids["active"])
            fingerprint = session_security.impressao_sessao(account)
        with forged.session_transaction() as data:
            data.update(_user_id=str(self.ids["active"]), boot_id=self.app.config["BOOT_ID"])
            data[session_security.CHAVE_CREDENCIAL] = fingerprint
        self._assert_poll_denied(forged)
        self._assert_private_denied(forged)

    def test_old_cookie_cannot_poll_after_restore(self):
        # A restauração não revive a sessão anterior à lixeira: o cookie antigo é
        # negado já na validação e a consulta das notificações nem roda.
        cookie = self._cookie(self._login_client())
        self._trash()
        self._restore()
        self._notify(text="SYNTHETIC_SECRET")
        response = self._assert_poll_denied(self._replay(cookie))
        self.assertNotIn("SYNTHETIC_SECRET", response.get_data(as_text=True))

    def test_old_cookie_polls_no_private_notification_during_or_after_trash(self):
        old = self._cookie(self._login_client())
        self._trash()
        self._notify(text="PRIVATE_DURING_TRASH")
        self.assertNotIn("PRIVATE_DURING_TRASH", self._assert_poll_denied(self._replay(old)).get_data(as_text=True))
        self._restore()
        fresh = self._login_client()
        self._notify(text="PRIVATE_AFTER_RESTORE")
        self.assertNotIn("PRIVATE_AFTER_RESTORE", self._assert_poll_denied(self._replay(old)).get_data(as_text=True))
        self.assertIn("PRIVATE_AFTER_RESTORE", self._polled_texts(fresh))

    def test_other_company_never_receives_restored_company_notifications(self):
        other = self._login_client("other")
        self._trash()
        self._restore()
        restored = self._login_client()
        self._notify(text="ISOLATED_RESTORED_COMPANY_A")
        self.assertNotIn("ISOLATED_RESTORED_COMPANY_A", self._polled_texts(other))
        self.assertIn("ISOLATED_RESTORED_COMPANY_A", self._polled_texts(restored))

    def test_permanent_delete_removes_only_target_users_and_auxiliary_history(self):
        with self.app.app_context():
            analysis = Analise(
                id_empresa=self.company_id, id_plano_referencia=self.plan_id,
                id_usuario_admin_responsavel=self.ids["admin"],
                periodo_inicio=date(2026, 1, 1), periodo_fim=date(2026, 1, 31),
                mes_referencia=1, ano_referencia=2026, tipo_analise="MENSAL",
            )
            ticket = ChamadoSuporte(
                id_empresa=self.company_id, id_usuario_cliente=self.ids["active"],
                assunto="Synthetic ticket", categoria="DUVIDA_TECNICA",
            )
            db.session.add_all([analysis, ticket])
            db.session.flush()
            message = MensagemSuporte(
                id_chamado=ticket.id_chamado, id_usuario_remetente=self.ids["active"],
                mensagem="Synthetic private message",
            )
            db.session.add_all([message, Notificacao(id_usuario=self.ids["active"], texto="Synthetic old notification")])
            db.session.commit()
        self._trash()
        response = self._action("excluir-permanente")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Empresa, self.company_id))
            self.assertIsNotNone(db.session.get(Empresa, self.other_company_id))
            for name in ("active", "pending", "inactive"):
                self.assertIsNone(db.session.get(Usuario, self.ids[name]))
            for model in (Analise, ChamadoSuporte, MensagemSuporte, Notificacao):
                self.assertEqual(db.session.scalar(select(func.count()).select_from(model)), 0)
            self.assertIsNotNone(db.session.get(Usuario, self.ids["other"]))
            self.assertIsNotNone(db.session.get(Usuario, self.ids["admin"]))

    def test_permanent_delete_requires_company_already_in_trash(self):
        before = self._users(), self._company()
        self._action("excluir-permanente")
        self.assertEqual((self._users(), self._company()), before)

    def test_soft_delete_restore_preserves_existing_commercial_status_policy(self):
        for status in ("ATIVA", "SUSPENSA", "CANCELADA"):
            with self.subTest(status=status):
                with self.app.app_context():
                    db.session.get(Empresa, self.company_id).status_conta = status
                    db.session.commit()
                before = self._users()
                self._trash()
                self.assertEqual(self._company()[1], status)
                self._restore()
                self.assertEqual(self._company()[1], status)
                self.assertEqual(self._users()[self.ids["active"]]["active"], before[self.ids["active"]]["active"])
                # NEXO-AJ-06.1: a restauração preserva o status comercial, e só
                # CANCELADA nega o CLIENTE; SUSPENSA mantém o acesso atual.
                if status == "CANCELADA":
                    client, _ = self._attempt_login()
                    self._assert_private_denied(client)
                else:
                    self.assertEqual(self._login_client().get("/_security/protected").status_code, 200)

    def test_trash_interface_keeps_protected_restore_and_permanent_delete_forms(self):
        self._trash()
        response = self.admin_client.get("/admin/lixeira")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(f'/admin/empresa/restaurar/{self.company_id}', html)
        self.assertIn(f'/admin/empresa/excluir-permanente/{self.company_id}', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('method="POST"', html)
        self.assertNotIn("Restaure para reativar", html)

    def test_absent_company_lifecycle_requests_do_not_change_existing_accounts(self):
        before = self._users(), self._company()
        for operation in ("excluir", "restaurar", "excluir-permanente"):
            response = self._action(operation, company=2147483647)
            self.assertEqual(response.status_code, 302)
        self.assertEqual((self._users(), self._company()), before)

    def test_generation_zero_preserves_credential_hmacs_and_later_generations_do_not(self):
        with self.app.app_context():
            account = db.session.get(Usuario, self.ids["active"])
            key = self.app.config["SECRET_KEY"].encode("utf-8")
            content = f"{account.id_usuario}\0{account.senha_hash}".encode("utf-8")
            session_zero = hmac.new(key, b"nexo-session-credential-v1\0" + content, hashlib.sha256).hexdigest()
            token_zero = hmac.new(key, b"nexo-password-credential-v2\0" + content, hashlib.sha256).hexdigest()
            self.assertEqual(account.geracao_autorizacao, 0)
            self.assertEqual(session_security.impressao_sessao(account), session_zero)
            self.assertEqual(auth._impressao_credencial(account), token_zero)
        self._trash()
        self._restore()
        with self.app.app_context():
            account = db.session.get(Usuario, self.ids["active"])
            self.assertNotEqual(session_security.impressao_sessao(account), session_zero)
            self.assertNotEqual(auth._impressao_credencial(account), token_zero)
            self.assertNotEqual(session_security.impressao_sessao(account), auth._impressao_credencial(account))

    def test_credential_update_snapshot_before_deletion_cannot_change_password_after_restore(self):
        with self.app.app_context():
            snapshot = auth._estado_credencial(db.session.get(Usuario, self.ids["active"]))
        self._trash()
        self._restore()
        with self.app.app_context():
            self.assertFalse(auth._atualizar_credencial(snapshot, token_cases.NEW_PASSWORD))
        self.assertEqual(self._account()[0], token_cases.FIXTURE_HASH)

    def test_company_lookup_failure_fails_closed_without_details_and_keeps_the_cookie(self):
        client = self._login_client()
        def fail_company_select(connection, cursor, statement, parameters, context, executemany):
            sql = statement.upper()
            if sql.lstrip().startswith("SELECT") and "FROM EMPRESA" in sql:
                raise SQLAlchemyError("SYNTHETIC_PRIVATE_COMPANY_LOOKUP_FAILURE")
        event.listen(self.engine, "before_cursor_execute", fail_company_select)
        try:
            response = client.get("/_security/protected")
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_company_select)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SYNTHETIC_PRIVATE_COMPANY_LOOKUP_FAILURE", response.get_data(as_text=True))
        self.assertNotIn("synthetic-private-data", response.get_data(as_text=True))
        # Banco fora não é sessão inválida: nada privado sai, mas o cookie fica e a
        # mesma sessão volta a valer quando a consulta da empresa volta a responder.
        with client.session_transaction() as data:
            self.assertEqual(data["_user_id"], str(self.ids["active"]))
        self.assertEqual(client.get("/_security/protected").status_code, 200)
