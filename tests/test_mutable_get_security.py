"""SEC-002: nenhuma requisição GET altera estado (logout e abrir notificação)."""

import ast
from pathlib import Path
import re

from sqlalchemy import select

from . import test_auth_tokens_security as token_cases
from extensions import db
from models import Notificacao
import notifications

ROOT = Path(__file__).resolve().parents[1]


class MutableGetSecurityTests(token_cases._TokenCase):
    def _login(self, user="active"):
        client = self.app.test_client()
        response = client.post("/auth/login", data={
            "email": f"{user}@example.invalid", "senha": token_cases.OLD_PASSWORD,
            "csrf_token": self._csrf(client),
        })
        self.assertEqual(response.status_code, 302)
        return client

    def _notification(self, user="active", link="/cliente/tickets"):
        with self.app.app_context():
            created = notifications.criar_notificacao(self.ids[user], "Synthetic notification", link)
            db.session.commit()
            return created.id_notificacao

    def _read(self, identifier):
        with self.app.app_context():
            return db.session.get(Notificacao, identifier).lida

    def _dashboard_html(self, client):
        response = client.get("/cliente/dashboard")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_get_open_notification_only_navigates_and_never_marks_as_read(self):
        client = self._login()
        identifier = self._notification()
        response = client.get(f"/notificacoes/{identifier}/abrir")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/cliente/tickets")
        self.assertFalse(self._read(identifier))

    def test_post_open_notification_marks_as_read_and_keeps_destination(self):
        client = self._login()
        identifier = self._notification()
        response = client.post(f"/notificacoes/{identifier}/abrir", data={"csrf_token": self._csrf(client, "/cliente/dashboard")})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/cliente/tickets")
        self.assertTrue(self._read(identifier))

    def test_post_open_notification_without_csrf_is_rejected(self):
        client = self._login()
        identifier = self._notification()
        self.assertEqual(client.post(f"/notificacoes/{identifier}/abrir", data={}).status_code, 400)
        self.assertFalse(self._read(identifier))

    def test_open_notification_of_another_user_is_not_marked_and_falls_back_to_dashboard(self):
        owner, intruder = self._login(), self._login("other")
        identifier = self._notification()
        for response in (
            intruder.post(f"/notificacoes/{identifier}/abrir", data={"csrf_token": self._csrf(intruder, "/cliente/dashboard")}),
            intruder.get(f"/notificacoes/{identifier}/abrir"),
        ):
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/cliente/dashboard"))
        self.assertFalse(self._read(identifier))
        self.assertEqual(owner.get("/cliente/dashboard").status_code, 200)

    def test_external_destination_never_redirects_outside_the_portal(self):
        client = self._login()
        for link in ("https://evil.invalid/x", "//evil.invalid/x", "/\\evil.invalid", "javascript:alert(1)"):
            with self.subTest(link=link):
                identifier = self._notification(link=link)
                response = client.post(f"/notificacoes/{identifier}/abrir", data={"csrf_token": self._csrf(client, "/cliente/dashboard")})
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/cliente/dashboard"))

    def test_anonymous_requests_cannot_open_or_mark_notifications(self):
        identifier = self._notification()
        anonymous = self.app.test_client()
        for response in (anonymous.get(f"/notificacoes/{identifier}/abrir"),
                         anonymous.post(f"/notificacoes/{identifier}/abrir",
                                        data={"csrf_token": self._csrf(anonymous)})):
            self.assertEqual(response.status_code, 302)
            self.assertIn("/auth/login", response.headers["Location"])
        self.assertFalse(self._read(identifier))

    def test_bell_and_logout_are_rendered_as_post_forms_with_csrf(self):
        client = self._login()
        identifier = self._notification()
        html = self._dashboard_html(client)
        self.assertNotIn('href="/auth/logout"', html)
        self.assertNotIn(f'href="/notificacoes/{identifier}/abrir"', html)
        logout = re.search(r'<form method="POST" action="/auth/logout" class="nav-logout-form">(.*?)</form>', html, re.S)
        self.assertIsNotNone(logout)
        self.assertIn('name="csrf_token"', logout.group(1))
        self.assertIn('class="nav-item nav-logout"', logout.group(1))
        item = re.search(rf'<form method="POST" action="/notificacoes/{identifier}/abrir" class="bell-item-form">(.*?)</form>',
                         html, re.S)
        self.assertIsNotNone(item)
        self.assertIn('name="csrf_token"', item.group(1))
        self.assertIn('<button type="submit" class="bell-item is-unread">', item.group(1))
        # O JS do polling monta o mesmo formulário com o token do <meta>.
        self.assertIn('<meta name="csrf-token"', html)
        self.assertIn("var id = parseInt(n.id, 10);", html)
        self.assertIn("form.method = 'POST';", html)
        self.assertIn("form.setAttribute('action', '/notificacoes/' + id + '/abrir');", html)
        self.assertNotIn("a.href = '/notificacoes/'", html)

    def test_first_access_page_logs_out_by_post_only(self):
        client = self._login("pending")
        html = client.get("/auth/primeiro-acesso").get_data(as_text=True)
        self.assertNotIn('href="/auth/logout"', html)
        self.assertIn('<form method="POST" action="/auth/logout"', html)

    def test_no_template_links_to_a_mutating_endpoint(self):
        offenders = []
        for path in sorted((ROOT / "templates").rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"href=\"([^\"]*)\"", text):
                if re.search(r"auth\.logout|/auth/logout|notificacoes\.abrir|/abrir", match.group(1)):
                    offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
        self.assertEqual(offenders, [])

    def test_no_get_only_route_writes_to_the_database(self):
        """Guarda de regressão por análise estática: rota só-GET não comita nem apaga."""
        offenders = []
        for path in sorted((ROOT / "blueprints").glob("*.py")) + [ROOT / "app.py"]:
            source = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.FunctionDef):
                    continue
                methods = None
                routed = False
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and getattr(decorator.func, "attr", "") == "route":
                        routed = True
                        for keyword in decorator.keywords:
                            if keyword.arg == "methods":
                                methods = sorted(element.value for element in keyword.value.elts)
                if not routed or (methods is not None and methods != ["GET"]):
                    continue
                body = ast.get_source_segment(source, node) or ""
                if re.search(r"\.commit\(\)|session\.add\(|session\.delete\(|storage\.remover\(|os\.remove\(|\.unlink\(", body):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name}")
        self.assertEqual(offenders, [])

    def test_notification_rows_remain_isolated_per_user(self):
        identifier = self._notification()
        with self.app.app_context():
            owners = db.session.scalars(select(Notificacao.id_usuario).where(
                Notificacao.id_notificacao == identifier)).all()
        self.assertEqual(owners, [self.ids["active"]])
