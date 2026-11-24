"""SEC-005: links sensíveis saem da origem canônica; hosts inesperados recebem 400."""

from pathlib import Path
import re
import unittest

from flask import Flask, url_for

from . import test_auth_tokens_security as token_cases
import origem_publica
from origem_publica import OrigemInvalida, configurar_origem_publica, normalizar_origem, url_publica

ROOT = Path(__file__).resolve().parents[1]
ORIGEM = "https://nexo-test.example.invalid"
HOSTILE = {"Host": "attacker.invalid", "X-Forwarded-Host": "forged.invalid",
           "X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}


class PublicOriginLinkTests(token_cases._TokenCase):
    def setUp(self):
        super().setUp()
        self.app.config["PUBLIC_URL"] = ORIGEM
        # Antes da primeira requisição: rota privada para o teste de hosts.
        self.app.add_url_rule("/_origin/protected", "origin_protected",
                              lambda: {"private": "synthetic-private-data"})

    def _csrf_with(self, client, path, headers):
        # O cookie do cliente de teste é guardado por host: o GET do token usa
        # o mesmo Host hostil do POST, senão a sessão (e o token) não viajam.
        response = client.get(path, headers=headers)
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return match.group(1)

    def _request_recovery(self, email, headers):
        client = self.app.test_client()
        csrf = self._csrf_with(client, "/auth/login", headers)
        response = client.post("/auth/recuperar-senha", data={"email": email, "csrf_token": csrf},
                               headers=headers)
        self.assertEqual(response.status_code, 302)

    def test_recovery_link_ignores_request_host_and_forwarded_headers(self):
        self._request_recovery("active@example.invalid", HOSTILE)
        link = self.recovery_mail.call_args.args[1]
        self.assertTrue(link.startswith(ORIGEM + "/auth/resetar-senha/"), link)
        for forged in ("attacker", "forged", "localhost"):
            self.assertNotIn(forged, link)

    def test_activation_link_ignores_request_host_and_forwarded_headers(self):
        self._request_recovery("pending@example.invalid", HOSTILE)
        link = self.activation_mail.call_args.args[1]
        self.assertTrue(link.startswith(ORIGEM + "/auth/primeiro-acesso/"), link)
        self.assertNotIn("attacker", link)
        self.assertNotIn("forged", link)

    def test_admin_registration_link_ignores_request_host(self):
        client = self.app.test_client()
        login = client.post("/auth/login", data={
            "email": "admin@example.invalid", "senha": token_cases.OLD_PASSWORD,
            "csrf_token": self._csrf_with(client, "/auth/login", HOSTILE),
        }, headers=HOSTILE)
        self.assertEqual(login.status_code, 302)
        response = client.post("/admin/empresas/nova", data={
            "csrf_token": self._csrf_with(client, "/admin/empresas/nova", HOSTILE),
            "data_contratacao": "2026-01-01", "id_segmento": str(self.segment_id),
            "id_plano_atual": str(self.plan_id), "cnpj": "99999999999999",
            "razao_social": "Synthetic origin company", "email_contato": "company-new@example.invalid",
            "usuario_nome": "Synthetic New", "usuario_email": "new@example.invalid",
        }, headers=HOSTILE)
        self.assertEqual(response.status_code, 302)
        link = self.registration_mail.call_args.args[1]
        self.assertTrue(link.startswith(ORIGEM + "/auth/primeiro-acesso/"), link)
        self.assertNotIn("attacker", link)

    def test_url_publica_matches_route_path_regardless_of_request_context(self):
        with self.app.test_request_context("/x", base_url="https://attacker.invalid/",
                                           headers={"X-Forwarded-Host": "forged.invalid"}):
            esperado = ORIGEM + url_for("cliente.analise", id_analise=7)
            self.assertEqual(url_publica("cliente.analise", id_analise=7), esperado)
            self.assertEqual(url_publica("cliente.ticket_detalhe", id_chamado=3, _external=True),
                             ORIGEM + url_for("cliente.ticket_detalhe", id_chamado=3))
            self.assertEqual(url_publica("auth.login", _anchor="topo"), ORIGEM + "/auth/login#topo")
        with self.app.app_context():
            self.assertEqual(url_publica("auth.recuperar_senha"), ORIGEM + "/auth/recuperar-senha")

    def test_no_sensitive_link_is_built_from_the_request_host(self):
        # Guarda de regressão: nenhum url_for(..., _external=True) no código servido.
        offenders = []
        for path in [ROOT / "app.py", ROOT / "emails.py", ROOT / "notifications.py",
                     *sorted((ROOT / "blueprints").glob("*.py"))]:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"_external\s*=\s*True", line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [])

    def test_trusted_hosts_reject_unexpected_host_before_any_route(self):
        client = self.app.test_client()
        trusted = {"Host": "nexo-test.example.invalid"}
        login = client.post("/auth/login", data={
            "email": "active@example.invalid", "senha": token_cases.OLD_PASSWORD,
            "csrf_token": self._csrf_with(client, "/auth/login", trusted),
        }, headers=trusted)
        self.assertEqual(login.status_code, 302)
        self.app.config["TRUSTED_HOSTS"] = ["nexo-test.example.invalid"]
        try:
            # Comparação exata do Werkzeug (navegadores enviam o host em minúsculas).
            for host in ("nexo-test.example.invalid", "nexo-test.example.invalid:8443"):
                with self.subTest(host=host):
                    self.assertEqual(client.get("/healthz", headers={"Host": host}).status_code, 200)
                    self.assertEqual(client.get("/_origin/protected", headers={"Host": host}).status_code, 200)
            for host in ("localhost", "evil.invalid", "nexo-test.example.invalid.evil.invalid",
                         "sub.nexo-test.example.invalid", "nexo test.invalid", "evil.invalid:443"):
                with self.subTest(host=host):
                    for path in ("/healthz", "/_origin/protected", "/auth/login"):
                        response = client.get(path, headers={"Host": host, "X-Forwarded-Host": "nexo-test.example.invalid"})
                        self.assertEqual(response.status_code, 400)
                        self.assertNotIn("synthetic-private-data", response.get_data(as_text=True))
                        self.assertNotIn(host, response.get_data(as_text=True))
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.app.config["TRUSTED_HOSTS"] = [".example.invalid"]
            self.assertEqual(client.get("/healthz", headers={"Host": "sub.nexo-test.example.invalid"}).status_code, 200)
            self.assertEqual(client.get("/healthz", headers={"Host": "evil.invalid"}).status_code, 400)
        finally:
            self.app.config["TRUSTED_HOSTS"] = None


class OriginNormalizationTests(unittest.TestCase):
    def test_accepts_scheme_host_and_port_only(self):
        self.assertEqual(normalizar_origem(" HTTPS://Nexo-Test.Example.invalid/ "), ORIGEM)
        self.assertEqual(normalizar_origem("https://h.example.invalid:8443"), "https://h.example.invalid:8443")
        self.assertEqual(normalizar_origem("http://127.0.0.1:5000"), "http://127.0.0.1:5000")
        self.assertEqual(normalizar_origem(ORIGEM, exigir_https=True), ORIGEM)

    def test_rejects_unusable_values_without_echoing_them(self):
        for value in ("", None, 12, "ftp://h.example.invalid", "//h.example.invalid",
                      "https://user:secret-value@h.example.invalid", "https://h.example.invalid/path",
                      "https://h.example.invalid/?q=1", "https://h.example.invalid/#frag",
                      "https://h.example.invalid:99999", "https://h.example.invalid:abc",
                      "https://bad host.invalid", "https://[::1]", "https://-bad.invalid",
                      "https://h.example.invalid\\evil.invalid"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(OrigemInvalida) as context:
                    normalizar_origem(value)
                if isinstance(value, str) and value:
                    self.assertNotIn(value, str(context.exception))
                    self.assertNotIn("secret-value", str(context.exception))
        with self.assertRaises(OrigemInvalida):
            normalizar_origem("http://h.example.invalid", exigir_https=True)

    def _app(self, **config):
        app = Flask(__name__)
        app.config.update(**config)
        return app

    def test_production_requires_https_origin_and_derives_trusted_hosts(self):
        app = self._app(PUBLIC_URL="https://Portal.Example.invalid/",
                        PUBLIC_URL_PLATAFORMA="https://nexo-test.example.invalid",
                        TRUSTED_HOSTS_EXTRAS=[".interno.example.invalid", "Outro.Example.invalid", "portal.example.invalid"])
        configurar_origem_publica(app, producao=True)
        self.assertEqual(app.config["PUBLIC_URL"], "https://portal.example.invalid")
        self.assertEqual(app.config["TRUSTED_HOSTS"], [
            "portal.example.invalid", "nexo-test.example.invalid", ".interno.example.invalid", "outro.example.invalid",
        ])
        self.assertNotIn("SOCKETIO_CORS_ORIGINS", app.config)
        for config in ({}, {"PUBLIC_URL": "http://portal.example.invalid"},
                       {"PUBLIC_URL": "https://portal.example.invalid/app"},
                       {"PUBLIC_URL": ORIGEM, "PUBLIC_URL_PLATAFORMA": "nope"},
                       {"PUBLIC_URL": ORIGEM, "TRUSTED_HOSTS_EXTRAS": ["bad host"]}):
            with self.subTest(config=config):
                with self.assertRaises(RuntimeError) as context:
                    configurar_origem_publica(self._app(**config), producao=True)
                self.assertNotIn("portal.example.invalid/app", str(context.exception))

    def test_development_defaults_to_local_origin_without_host_restriction(self):
        app = self._app()
        configurar_origem_publica(app, producao=False)
        self.assertEqual(app.config["PUBLIC_URL"], origem_publica.ORIGEM_DESENVOLVIMENTO)
        self.assertIsNone(app.config["TRUSTED_HOSTS"])
        restricted = self._app(PUBLIC_URL="http://localhost:5000", TRUSTED_HOSTS_EXTRAS=["127.0.0.1"])
        configurar_origem_publica(restricted, producao=False)
        self.assertEqual(restricted.config["TRUSTED_HOSTS"], ["localhost", "127.0.0.1"])
        with self.assertRaises(RuntimeError):
            configurar_origem_publica(self._app(PUBLIC_URL="ftp://x.invalid"), producao=False)


if __name__ == "__main__":
    unittest.main()
