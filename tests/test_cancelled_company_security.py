"""NEXO-AJ-06.1: CLIENTE de empresa CANCELADA sem acesso, com dados sintéticos.

SQLite em diretório temporário, rotas reais com CSRF, o polling do sininho e
estado persistido. SUSPENSA mantém o acesso atual (restrições na NEXO-AJ-05.x)
e a lixeira continua independente do status comercial. Os locks reais de
PostgreSQL ficam em tests.test_cancelled_company_postgresql (opt-in).
"""

from datetime import date
import re

from sqlalchemy import event, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from . import test_auth_tokens_security as token_cases
from . import test_company_restore_security as restore_cases
from . import test_session_revocation_security as session_cases
from blueprints import auth
from extensions import db
from models import (
    Analise, Empresa, IndicadorAnalise, Notificacao, ProdutoCurvaABC, RelatorioAnalise, Usuario,
)
import notifications


PRIVATE_TEXT = "CONCLUSAO_PRIVADA_SINTETICA_AJ061"


class CancelledCompanySecurityTests(token_cases._TokenCase):
    # Reutilizar helpers, sem herdar os testes históricos SEC-003/SEC-004.
    _session_routes = session_cases.SessionRevocationCases._session_routes
    _login_client = session_cases.SessionRevocationCases._login_client
    _assert_private_denied = session_cases.SessionRevocationCases._assert_private_denied
    _notify = session_cases.SessionRevocationCases._notify
    # Polling do sininho (substituiu o Socket.IO): GET /notificacoes/novas.
    POLL = session_cases.SessionRevocationCases.POLL
    _poll = session_cases.SessionRevocationCases._poll
    _polled_texts = session_cases.SessionRevocationCases._polled_texts
    _assert_poll_denied = session_cases.SessionRevocationCases._assert_poll_denied
    _action = restore_cases.CompanyRestoreSecurityTests._action
    _trash = restore_cases.CompanyRestoreSecurityTests._trash
    _restore = restore_cases.CompanyRestoreSecurityTests._restore
    _users = restore_cases.CompanyRestoreSecurityTests._users
    _attempt_login = restore_cases.CompanyRestoreSecurityTests._attempt_login
    _cookie = restore_cases.CompanyRestoreSecurityTests._cookie
    _replay = restore_cases.CompanyRestoreSecurityTests._replay

    def setUp(self):
        super().setUp()
        self._session_routes()
        with self.app.app_context():
            first = db.session.get(Usuario, self.ids["active"])
            other = db.session.get(Usuario, self.ids["other"])
            self.company_id, self.other_company_id = first.id_empresa, other.id_empresa
            # Empresa A: ativo, primeiro acesso pendente e usuário já bloqueado.
            db.session.get(Usuario, self.ids["inactive"]).id_empresa = self.company_id
            db.session.commit()
        self.analysis_id = self._published_analysis(self.company_id)
        self.other_analysis_id = self._published_analysis(self.other_company_id)
        self.admin_client = self._login_client("admin")

    # ---------------------------------------------------------------- helpers
    def _published_analysis(self, company_id):
        with self.app.app_context():
            company = db.session.get(Empresa, company_id)
            analise = Analise(
                id_empresa=company_id, id_plano_referencia=company.id_plano_atual,
                id_usuario_admin_responsavel=self.ids["admin"],
                periodo_inicio=date(2026, 1, 1), periodo_fim=date(2026, 1, 31),
                mes_referencia=1, ano_referencia=2026, tipo_analise="MENSAL", status_analise="CONCLUIDO",
            )
            db.session.add(analise)
            db.session.flush()
            analise.indicador = IndicadorAnalise(faturamento_total=25, total_comprado=40,
                                                saldo_estimado_compras_vendas=15)
            analise.relatorio = RelatorioAnalise(titulo=f"Análise sintética {company_id}", publicado=True,
                                                 conclusao_estrategica=f"{PRIVATE_TEXT}_{company_id}")
            analise.curva_abc = [ProdutoCurvaABC(
                posicao_ranking=1, produto_nome="Produto sintético A", faturamento=20,
                quantidade=2, percentual_individual=80, percentual_acumulado=80, classe_abc="A")]
            db.session.commit()
            return analise.id_analise

    def _status(self, company=None):
        with self.app.app_context():
            return db.session.get(Empresa, company or self.company_id).status_conta

    def _edit_path(self, company=None):
        return f"/admin/empresas/{company or self.company_id}/editar"

    def _edit_form(self, status, company=None, displayed=None):
        """Campos do formulário real, com o status exibido como a tela o envia."""
        company = company or self.company_id
        with self.app.app_context():
            empresa = db.session.get(Empresa, company)
            data = {
                "id_segmento": empresa.id_segmento, "id_plano_atual": empresa.id_plano_atual,
                "razao_social": empresa.razao_social, "email_contato": empresa.email_contato,
                "data_contratacao": empresa.data_contratacao.isoformat(), "status_conta": status,
                "status_conta_exibido": empresa.status_conta if displayed is None else displayed,
            }
        data["csrf_token"] = self._csrf(self.admin_client, self._edit_path(company))
        return data

    def _set_status(self, status, company=None):
        """Troca pelo formulário real do ADMIN (lock, revogação e COMMIT da rota)."""
        response = self.admin_client.post(self._edit_path(company), data=self._edit_form(status, company))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/empresas"))
        self.assertEqual(self._status(company), status)
        return response

    def _set_status_out_of_band(self, status, company=None):
        """Alteração feita por outro processo/script: sem callback local de revogação."""
        with self.app.app_context():
            db.session.execute(update(Empresa).where(
                Empresa.id_empresa == (company or self.company_id),
            ).values(status_conta=status))
            db.session.commit()

    def _admin_flashes(self):
        with self.admin_client.session_transaction() as data:
            return " ".join(str(item[1]) for item in data.get("_flashes", ()))

    def _meta_csrf(self, client, path="/cliente/dashboard"):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        return re.search(r'<meta name="csrf-token" content="([^"]+)"', response.get_data(as_text=True)).group(1)

    def _assert_login_page_denied(self, client, response):
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)
        body = response.get_data(as_text=True)
        self.assertNotIn(PRIVATE_TEXT, body)
        # NEXO-AJ-06.3: senha correta de empresa CANCELADA mostra a página própria
        # de conta encerrada, sem criar sessão autenticada.
        self.assertIn("Conta encerrada", body)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        with client.session_transaction() as data:
            self.assertNotIn("_user_id", data)
            self.assertNotIn("credential_fingerprint", data)
        self._assert_private_denied(client)

    def _assert_redirected_to_login(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/login", response.headers["Location"])
        self.assertNotIn(PRIVATE_TEXT, response.get_data(as_text=True))

    def _generations(self, *names):
        users = self._users()
        return {name: users[self.ids[name]]["generation"] for name in names}

    # -------------------------------------------------------------- cenários
    def test_a_active_company_client_logs_in_and_uses_portal(self):
        client = self._login_client()
        self.assertEqual(client.get("/_security/protected").status_code, 200)
        self.assertEqual(client.get("/cliente/dashboard").status_code, 200)
        pdf = client.get(f"/cliente/analise/{self.analysis_id}/pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF-"))

    def test_b_suspended_company_keeps_current_access_and_sessions(self):
        before_session = self._login_client()
        before = self._generations("active", "pending", "inactive")
        self._set_status("SUSPENSA")
        self.assertEqual(self._generations("active", "pending", "inactive"), before)
        self.assertEqual(before_session.get("/_security/protected").status_code, 200)
        client = self._login_client()
        self.assertEqual(client.get("/_security/protected").status_code, 200)
        dashboard = client.get("/cliente/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Conta suspensa", dashboard.get_data(as_text=True))
        self.assertEqual(client.get(f"/cliente/analise/{self.analysis_id}").status_code, 200)
        self.assertEqual(client.get(f"/cliente/analise/{self.analysis_id}/pdf").status_code, 200)
        self.assertEqual(self._poll(client).status_code, 200)

    def test_b_suspended_company_keeps_polling_notifications(self):
        client = self._login_client()
        self._set_status("SUSPENSA")
        self._notify(text="PRIVATE_SUSPENDED_STILL_DELIVERED")
        self.assertIn("PRIVATE_SUSPENDED_STILL_DELIVERED", self._polled_texts(client))

    def test_b_suspended_company_keeps_password_recovery_and_activation(self):
        self._set_status("SUSPENSA")
        self.recovery_mail.reset_mock()
        self.activation_mail.reset_mock()
        client = self.app.test_client()
        for email in ("active@example.invalid", "pending@example.invalid"):
            client.post("/auth/recuperar-senha", data={
                "email": email, "csrf_token": self._csrf(client, "/auth/recuperar-senha"),
            })
        self.recovery_mail.assert_called_once()
        self.activation_mail.assert_called_once()
        old_hash = self._account()[0]
        self._assert_success(self._post(self._token()))
        self.assertNotEqual(self._account()[0], old_hash)
        self._assert_success(self._post(self._token("pending", purpose="activation"), purpose="activation"))
        self.assertFalse(self._account("pending")[1])

    def test_b_suspended_company_keeps_logged_first_access(self):
        self._set_status("SUSPENSA")
        pending = self._login_client("pending")
        response = pending.post("/auth/primeiro-acesso", data={
            "nova_senha": token_cases.NEW_PASSWORD, "confirma_senha": token_cases.NEW_PASSWORD,
            "csrf_token": self._csrf(pending, "/auth/primeiro-acesso"),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/cliente/dashboard"))
        self.assertFalse(self._account("pending")[1])
        self.assertEqual(pending.get("/_security/protected").status_code, 200)

    def test_c_cancelled_company_client_cannot_log_in(self):
        self._set_status("CANCELADA")
        for name in ("active", "pending"):
            with self.subTest(user=name):
                client, response = self._attempt_login(name)
                self._assert_login_page_denied(client, response)
                self._assert_redirected_to_login(client.get("/cliente/dashboard"))

    def test_c_wrong_password_for_cancelled_account_keeps_generic_message(self):
        self._set_status("CANCELADA")
        client = self.app.test_client()
        response = client.post("/auth/login", data={
            "email": "active@example.invalid", "senha": "Synthetic-wrong-password-000!",
            "csrf_token": self._csrf(client),
        })
        body = response.get_data(as_text=True)
        self.assertIn("E-mail ou senha incorretos.", body)
        self.assertNotIn("desativada", body)
        self.assertNotIn("Conta encerrada", body)

    def test_d_existing_session_loses_access_when_admin_cancels_company(self):
        client = self._login_client()
        cookie = self._cookie(client)
        before = self._users()
        self._set_status("CANCELADA")
        after = self._users()
        for name in ("active", "pending", "inactive"):
            identifier = self.ids[name]
            self.assertEqual(after[identifier]["generation"], before[identifier]["generation"] + 1)
            for field in ("active", "hash", "role", "company", "first_access"):
                self.assertEqual(after[identifier][field], before[identifier][field], (name, field))
        for name in ("other", "admin"):
            self.assertEqual(after[self.ids[name]], before[self.ids[name]])
        self._assert_private_denied(client)
        self._assert_private_denied(self._replay(cookie))

    def test_d_out_of_band_cancellation_is_enforced_on_next_request(self):
        client = self._login_client()
        before = self._generations("active")
        self._set_status_out_of_band("CANCELADA")
        # Sem revogação local: a autorização central relê o status no banco.
        self.assertEqual(self._generations("active"), before)
        self._assert_private_denied(client)

    def test_d_generation_changes_only_when_entering_or_leaving_cancelled(self):
        start = self._generations("active", "pending", "inactive")
        for status in ("SUSPENSA", "ATIVA"):
            self._set_status(status)
        self.assertEqual(self._generations("active", "pending", "inactive"), start)
        self._set_status("CANCELADA")
        cancelled = self._generations("active", "pending", "inactive")
        self.assertEqual(cancelled, {name: value + 1 for name, value in start.items()})
        self._set_status("CANCELADA")
        self.assertEqual(self._generations("active", "pending", "inactive"), cancelled)
        self._set_status("ATIVA")
        self.assertEqual(self._generations("active", "pending", "inactive"),
                         {name: value + 2 for name, value in start.items()})

    def test_e_cancelled_client_cannot_open_portal_pages_by_direct_url(self):
        cookie = self._cookie(self._login_client())
        self.assertEqual(self._replay(cookie).get("/cliente/dashboard").status_code, 200)
        self._set_status_out_of_band("CANCELADA")
        for path in ("/cliente/dashboard", "/cliente/historico", f"/cliente/analise/{self.analysis_id}",
                     "/cliente/upload", "/cliente/tickets", "/cliente/guia", "/auth/primeiro-acesso"):
            with self.subTest(path=path):
                self._assert_redirected_to_login(self._replay(cookie).get(path))

    def test_f_cancelled_client_cannot_export_pdf(self):
        client = self._login_client()
        self.assertEqual(client.get(f"/cliente/analise/{self.analysis_id}/pdf").status_code, 200)
        cookie = self._cookie(client)
        for cancel in (self._set_status_out_of_band, self._set_status):
            with self.subTest(origem=cancel.__name__):
                cancel("CANCELADA")
                response = self._replay(cookie).get(f"/cliente/analise/{self.analysis_id}/pdf")
                self._assert_redirected_to_login(response)
                self.assertFalse(response.data.startswith(b"%PDF-"))
                self._set_status_out_of_band("ATIVA")

    def test_g_cancelled_client_cannot_use_protected_apis_or_notifications(self):
        client = self._login_client()
        token = self._meta_csrf(client)
        ok = client.post("/api/suporte-bot", json={"mensagem": "Abra o dashboard."},
                         headers={"X-CSRFToken": token})
        self.assertEqual(ok.status_code, 200)
        cookie = self._cookie(client)
        notification = self._notify(text="PRIVATE_NOTIFICATION_BEFORE_CANCEL")
        self._set_status("CANCELADA")
        api = self._replay(cookie).post("/api/suporte-bot", json={"mensagem": "Abra o dashboard."},
                                        headers={"X-CSRFToken": token})
        self._assert_redirected_to_login(api)
        self.assertFalse(api.is_json)
        for response in (
            self._replay(cookie).post("/notificacoes/marcar-lidas", data={"csrf_token": token}),
            self._replay(cookie).post(f"/notificacoes/{notification}/abrir", data={"csrf_token": token}),
            self._replay(cookie).get(f"/notificacoes/{notification}/abrir"),
        ):
            self._assert_redirected_to_login(response)
        polled = self._assert_poll_denied(self._replay(cookie))
        self.assertNotIn("PRIVATE_NOTIFICATION_BEFORE_CANCEL", polled.get_data(as_text=True))
        with self.app.app_context():
            self.assertFalse(db.session.get(Notificacao, notification).lida)

    def test_h_cancelled_client_poll_receives_no_private_notification(self):
        client, other = self._login_client(), self._login_client("other")
        self._notify(text="PRIVATE_BEFORE_CANCEL")
        self.assertIn("PRIVATE_BEFORE_CANCEL", self._polled_texts(client))
        self._set_status("CANCELADA")
        self._notify(text="PRIVATE_AFTER_CANCEL")
        response = self._assert_poll_denied(client)
        self.assertNotIn("PRIVATE_AFTER_CANCEL", response.get_data(as_text=True))
        self._notify("other", text="OTHER_COMPANY_STILL_AUTHORIZED")
        self.assertIn("OTHER_COMPANY_STILL_AUTHORIZED", self._polled_texts(other))

    def test_h_admin_route_cancellation_denies_the_next_poll(self):
        client = self._login_client()
        self.assertEqual(self._poll(client).status_code, 200)
        self._set_status("CANCELADA")
        self._assert_poll_denied(client)

    def test_h_out_of_band_cancellation_denies_the_next_poll(self):
        # Outro processo cancelou (sem passar pela rota): não há conexão a derrubar;
        # a consulta seguinte relê o banco e é negada.
        client = self._login_client()
        self._notify(text="PRIVATE_BEFORE_OUT_OF_BAND")
        self.assertIn("PRIVATE_BEFORE_OUT_OF_BAND", self._polled_texts(client))
        self._set_status_out_of_band("CANCELADA")
        self._notify(text="PRIVATE_OUT_OF_BAND")
        response = self._assert_poll_denied(client)
        self.assertNotIn("PRIVATE_OUT_OF_BAND", response.get_data(as_text=True))

    def test_h_denied_poll_after_cancellation_never_reads_notifications(self):
        client = self._login_client()
        self.assertEqual(self._poll(client).status_code, 200)
        self._set_status_out_of_band("CANCELADA")
        consultas = []
        def registrar(conn, cursor, statement, parameters, context, executemany):
            if "notificacao" in statement.lower():
                consultas.append(statement)
        event.listen(self.engine, "before_cursor_execute", registrar)
        try:
            self._assert_poll_denied(client)
        finally:
            event.remove(self.engine, "before_cursor_execute", registrar)
        self.assertEqual(consultas, [])

    def test_i_cancelled_client_cookie_cannot_poll(self):
        cookie = self._cookie(self._login_client())
        self.assertEqual(self._poll(self._replay(cookie)).status_code, 200)
        self._set_status_out_of_band("CANCELADA")
        self._assert_poll_denied(self._replay(cookie))
        self._set_status_out_of_band("ATIVA")
        self._set_status("CANCELADA")
        self._assert_poll_denied(self._replay(cookie))

    def test_j_admin_keeps_administrative_access_to_cancelled_company(self):
        before = self._users()[self.ids["admin"]]
        self._set_status("CANCELADA")
        self.assertEqual(self._users()[self.ids["admin"]], before)
        listing = self.admin_client.get("/admin/empresas")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("Synthetic company 1", listing.get_data(as_text=True))
        self.assertEqual(self.admin_client.get(self._edit_path()).status_code, 200)
        self.assertEqual(self._poll(self.admin_client).status_code, 200)
        self._notify("admin", text="ADMIN_STILL_RECEIVES")
        self.assertIn("ADMIN_STILL_RECEIVES", self._polled_texts(self.admin_client))
        with self.app.app_context():
            company = db.session.get(Empresa, self.company_id)
            self.assertIsNone(company.deletado_em)
            self.assertEqual(db.session.scalar(select(func.count()).select_from(Analise).where(
                Analise.id_empresa == self.company_id)), 1)
            self.assertTrue(db.session.get(Analise, self.analysis_id).relatorio.publicado)

    def test_k_trash_and_restore_keep_cancelled_status_and_block_client(self):
        start = self._generations("active")["active"]
        self._set_status("CANCELADA")
        self._trash()
        self._restore()
        self.assertEqual(self._status(), "CANCELADA")
        self.assertIn("continua CANCELADA", self._admin_flashes())
        # Cancelamento e lixeira são eixos distintos: cada um revoga uma vez.
        self.assertEqual(self._generations("active")["active"], start + 2)
        client, response = self._attempt_login()
        self._assert_login_page_denied(client, response)

    def test_l_client_cannot_read_other_company_data(self):
        client = self._login_client()
        for path in (f"/cliente/analise/{self.other_analysis_id}", f"/cliente/analise/{self.other_analysis_id}/pdf"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(f"{PRIVATE_TEXT}_{self.other_company_id}", response.get_data(as_text=True))
        # Cancelar a outra empresa não afeta a primeira, e vice-versa.
        self._set_status("CANCELADA", company=self.other_company_id)
        self.assertEqual(client.get(f"/cliente/analise/{self.analysis_id}").status_code, 200)
        self._set_status("ATIVA", company=self.other_company_id)
        other = self._login_client("other")
        self._set_status("CANCELADA")
        self.assertEqual(other.get("/_security/protected").status_code, 200)
        self.assertEqual(other.get(f"/cliente/analise/{self.other_analysis_id}").status_code, 200)

    def test_m_reactivated_company_requires_new_login_and_then_allows_access(self):
        cookie = self._cookie(self._login_client())
        for status in ("ATIVA", "SUSPENSA"):
            with self.subTest(status=status):
                self._set_status("CANCELADA")
                before = self._generations("active")["active"]
                self._set_status(status)
                # Sair de CANCELADA também revoga: nenhum cookie anterior volta a valer.
                self.assertEqual(self._generations("active")["active"], before + 1)
                self._assert_private_denied(self._replay(cookie))
                client = self._login_client()
                self.assertEqual(client.get("/_security/protected").status_code, 200)
                cookie = self._cookie(client)
                self._notify(text=f"PRIVATE_AFTER_{status}")
                self.assertIn(f"PRIVATE_AFTER_{status}", self._polled_texts(client))

    def test_m_route_reactivation_after_out_of_band_cancellation_requires_new_login(self):
        cookie = self._cookie(self._login_client())
        token = self._token()
        self._set_status_out_of_band("CANCELADA")
        self._set_status("ATIVA")
        self._assert_private_denied(self._replay(cookie))
        self._assert_rejected(self._post(token))
        self.assertEqual(self._login_client().get("/_security/protected").status_code, 200)

    def test_n_database_failure_during_authorization_fails_closed_and_keeps_the_cookie(self):
        client = self._login_client()
        cookie = self._cookie(client)
        other_session = self._login_client()
        admin = self._login_client("admin")
        def fail_status_lookup(connection, cursor, statement, parameters, context, executemany):
            if "status_conta" in statement and statement.lstrip().upper().startswith("SELECT"):
                raise SQLAlchemyError("PRIVATE_STATUS_DB_SENTINEL")
        event.listen(self.engine, "before_cursor_execute", fail_status_lookup)
        try:
            response = client.get("/_security/protected")
            replay_poll = self._poll(self._replay(cookie))
            self._notify(text="PRIVATE_DURING_DB_FAILURE")
            other_poll = self._poll(other_session)
            admin_poll = self._poll(admin)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_status_lookup)
        self.assertEqual(response.status_code, 503)
        body = response.get_data(as_text=True)
        self.assertNotIn("synthetic-private-data", body)
        self.assertNotIn("PRIVATE_STATUS_DB_SENTINEL", body)
        # Banco fora não é sessão inválida: nada privado sai, mas o cookie fica.
        with client.session_transaction() as data:
            self.assertEqual(data["_user_id"], str(self.ids["active"]))
        for poll in (replay_poll, other_poll):
            self.assertEqual(poll.status_code, 503)
            self.assertNotIn("PRIVATE_DURING_DB_FAILURE", poll.get_data(as_text=True))
            self.assertNotIn("PRIVATE_STATUS_DB_SENTINEL", poll.get_data(as_text=True))
        self.assertEqual(admin_poll.status_code, 200)  # ADMIN não depende da consulta à empresa
        # Banco de volta: as mesmas sessões voltam a valer, sem novo login.
        self.assertEqual(client.get("/_security/protected").status_code, 200)
        self.assertIn("PRIVATE_DURING_DB_FAILURE", self._polled_texts(other_session))

    # ------------------------------------------------------- tokens de senha
    def test_password_tokens_are_not_issued_or_accepted_for_cancelled_company(self):
        recovery = self._token()
        activation = self._token("pending", purpose="activation")
        before = self._account(), self._account("pending")
        self._set_status_out_of_band("CANCELADA")
        self._assert_rejected(self._post(recovery))
        self._assert_rejected(self._post(activation, purpose="activation"))
        self.assertEqual((self._account(), self._account("pending")), before)
        with self.app.app_context():
            for name, issuer in (("active", auth.gerar_token_recuperacao_senha),
                                 ("pending", auth.gerar_token_definir_senha)):
                with self.assertRaises(ValueError):
                    issuer(db.session.get(Usuario, self.ids[name]))
        self.recovery_mail.reset_mock()
        self.activation_mail.reset_mock()
        client = self.app.test_client()
        for email in ("active@example.invalid", "pending@example.invalid"):
            response = client.post("/auth/recuperar-senha", data={
                "email": email, "csrf_token": self._csrf(client, "/auth/recuperar-senha"),
            })
            self.assertEqual(response.status_code, 302)
        self.recovery_mail.assert_not_called()
        self.activation_mail.assert_not_called()

    def test_password_change_cas_rejects_company_cancelled_after_token_check(self):
        with self.app.app_context():
            estado = auth._estado_credencial(db.session.get(Usuario, self.ids["active"]))
        before = self._account()
        self._set_status_out_of_band("CANCELADA")
        with self.app.test_request_context():
            self.assertFalse(auth._atualizar_credencial(estado, token_cases.NEW_PASSWORD))
        self.assertEqual(self._account(), before)

    # ------------------------------------------------ transação e ADMIN
    def test_failed_status_commit_does_not_revoke_sessions_or_polling(self):
        client = self._login_client()
        before = self._generations("active", "pending", "inactive")
        data = self._edit_form("CANCELADA")
        sent = []
        def fail_second_update(conn, cursor, statement, parameters, context, executemany):
            sql = statement.lstrip().upper()
            if sql.startswith("UPDATE"):
                sent.append(sql.split()[1])
                if len(sent) == 2:  # depois de uma escrita já enviada ao banco
                    raise SQLAlchemyError("PRIVATE_COMMIT_SENTINEL")
        event.listen(self.engine, "before_cursor_execute", fail_second_update)
        try:
            response = self.admin_client.post(self._edit_path(), data=data)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_second_update)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(sent), 2)
        self.assertIn("USUARIO", sent)
        self.assertEqual(self._status(), "ATIVA")
        self.assertEqual(self._generations("active", "pending", "inactive"), before)
        self.assertEqual(client.get("/_security/protected").status_code, 200)
        self._notify(text="PRIVATE_AFTER_FAILED_CANCEL")
        self.assertIn("PRIVATE_AFTER_FAILED_CANCEL", self._polled_texts(client))

    def test_stale_or_incomplete_admin_form_cannot_change_status_silently(self):
        stale = self._edit_form("ATIVA")  # tela aberta com a empresa ATIVA
        self._set_status("CANCELADA")
        stale["csrf_token"] = self._csrf(self.admin_client, self._edit_path())
        response = self.admin_client.post(self._edit_path(), data=stale)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(self._edit_path()))
        self.assertEqual(self._status(), "CANCELADA")
        self.assertIn("alterado por outra operação", self._admin_flashes())
        missing = self._edit_form("ATIVA")
        del missing["status_conta"], missing["status_conta_exibido"]
        self.assertEqual(self.admin_client.post(self._edit_path(), data=missing).status_code, 302)
        self.assertEqual(self._status(), "CANCELADA")
        invalid = self._edit_form("cancelada")
        response = self.admin_client.post(self._edit_path(), data=invalid)
        self.assertTrue(response.headers["Location"].endswith(self._edit_path()))
        self.assertEqual(self._status(), "CANCELADA")
        self.assertIn("Status da conta inválido", self._admin_flashes())

    def test_company_created_as_cancelled_gets_no_activation_link(self):
        path = "/admin/empresas/nova"
        response = self.admin_client.post(path, data={
            "id_segmento": self.segment_id, "id_plano_atual": self.plan_id, "cnpj": "00000000000099",
            "razao_social": "Synthetic cancelled company", "email_contato": "company99@example.invalid",
            "data_contratacao": "2026-01-01", "status_conta": "CANCELADA",
            "usuario_nome": "Cancelled user", "usuario_email": "cancelled@example.invalid",
            "csrf_token": self._csrf(self.admin_client, path),
        })
        self.assertEqual(response.status_code, 302)
        self.registration_mail.assert_not_called()
        self.assertIn("não foi enviado", self._admin_flashes())
        with self.app.app_context():
            user = db.session.scalar(select(Usuario).where(Usuario.email == "cancelled@example.invalid"))
            self.assertTrue(user.primeiro_acesso)
            self.assertEqual(db.session.get(Empresa, user.id_empresa).status_conta, "CANCELADA")

    def test_company_notifications_are_not_created_for_cancelled_company(self):
        def notify_company():
            with self.app.app_context():
                created = notifications.notificar_clientes_empresa(self.company_id, "PRIVATE_COMPANY_EVENT")
                db.session.commit()
                return created
        self._set_status("CANCELADA")
        self.assertEqual(notify_company(), 0)
        with self.app.app_context():
            self.assertEqual(db.session.scalar(select(func.count()).select_from(Notificacao).where(
                Notificacao.texto == "PRIVATE_COMPANY_EVENT")), 0)
        self._set_status("SUSPENSA")
        self.assertEqual(notify_company(), 2)  # active e pending; inactive segue de fora
