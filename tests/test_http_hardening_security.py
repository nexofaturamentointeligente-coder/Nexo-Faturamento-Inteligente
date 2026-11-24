"""SEC-007/009/010: script demo guardado, SRI nas CDNs, cabeçalhos e CSP."""

import base64
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

from .deploy_support import DeployTestCase, ensure_test_environment
import criar_cliente_demo

ROOT = Path(__file__).resolve().parents[1]
SRI = {
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css":
        "sha384-QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH",
    "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css":
        "sha384-XGjxtQfXaH2tnPFa9x+ruJTuLE3Aa6LhHSWRr1XeTyhezb4abCG4ccI5AkVDxqC+",
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js":
        "sha384-YvpcrYf0tY3lHB60NNkmXc5s9fDVZLESaAA55NDzOxhy9GkcIdslK1eN7N6jIeHz",
    "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js":
        "sha384-cCVCZkAjYNxaYKbM8lsArLznDF/SvMFr1jcZrvOpSTCa0W40ZAdLzHCEulnUa5i7",
}
_TAG_CDN = re.compile(r"<(script|link)\b[^>]*\b(?:src|href)=\"(https://[^\"]+)\"[^>]*>", re.S)
_ATRIBUTO = re.compile(r'([\w-]+)="([^"]*)"')


class SubresourceIntegrityTests(unittest.TestCase):
    def _tags(self):
        for caminho in sorted((ROOT / "templates").rglob("*.html")):
            texto = caminho.read_text(encoding="utf-8")
            for match in _TAG_CDN.finditer(texto):
                url = match.group(2)
                if url.startswith(("https://fonts.googleapis.com", "https://fonts.gstatic.com")):
                    continue  # folhas de fonte: sem versão fixa publicada pelo Google
                yield caminho.relative_to(ROOT), url, dict(_ATRIBUTO.findall(match.group(0)))

    def test_every_cdn_resource_is_pinned_with_sri_and_crossorigin(self):
        vistos = set()
        for arquivo, url, attrs in self._tags():
            with self.subTest(arquivo=str(arquivo), url=url):
                self.assertIn(url, SRI, "recurso de CDN fora do inventário fixado")
                self.assertEqual(attrs.get("integrity"), SRI[url])
                self.assertEqual(attrs.get("crossorigin"), "anonymous")
                self.assertRegex(url, r"@\d+\.\d+\.\d+/|/\d+\.\d+\.\d+/", "versão exata obrigatória")
                vistos.add(url)
        self.assertEqual(vistos, set(SRI), "inventário e templates divergem")

    def test_ci_workflow_uses_the_same_hashes(self):
        texto = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / ".github" / "workflows").glob("*.yml"))
        for url, integridade in SRI.items():
            if "bootstrap-icons" in url:
                continue  # não faz parte das cópias locais do CI
            self.assertIn(integridade.split("-", 1)[1], texto, url)

    def test_local_cdn_copies_match_the_pinned_hashes_when_present(self):
        pasta = os.environ.get("NEXO_TEST_CDN_DIR")
        if not pasta:
            self.skipTest("NEXO_TEST_CDN_DIR ausente")
        conferidos = 0
        for url, integridade in SRI.items():
            arquivo = Path(pasta) / url.rsplit("/", 1)[1]
            if arquivo.is_file():
                resumo = base64.b64encode(hashlib.sha384(arquivo.read_bytes()).digest()).decode()
                self.assertEqual("sha384-" + resumo, integridade, url)
                conferidos += 1
        self.assertGreater(conferidos, 0)


class SecurityHeaderTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()

    def _csp(self, resposta):
        self.assertNotIn("Content-Security-Policy-Report-Only", resposta.headers)
        return resposta.headers["Content-Security-Policy"]

    def test_public_pages_carry_the_full_header_set(self):
        for caminho in ("/", "/auth/login", "/como-aderir", "/healthz", "/rota-inexistente"):
            with self.subTest(caminho=caminho):
                resposta = self.client.get(caminho)
                cabecalhos = resposta.headers
                csp = self._csp(resposta)
                self.assertIn("default-src 'self'", csp)
                self.assertIn("frame-ancestors 'none'", csp)
                self.assertIn("object-src 'none'", csp)
                self.assertIn("base-uri 'self'", csp)
                self.assertIn("form-action 'self'", csp)
                self.assertIn("https://cdn.jsdelivr.net", csp)
                self.assertNotIn("'unsafe-eval'", csp)
                self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
                self.assertEqual(cabecalhos["X-Frame-Options"], "DENY")
                self.assertEqual(cabecalhos["X-Content-Type-Options"], "nosniff")
                self.assertEqual(cabecalhos["Referrer-Policy"], "strict-origin-when-cross-origin")
                self.assertIn("camera=()", cabecalhos["Permissions-Policy"])
                self.assertEqual(cabecalhos["Cross-Origin-Opener-Policy"], "same-origin")
                self.assertNotIn("Strict-Transport-Security", cabecalhos, "HSTS só em produção/HTTPS")

    def test_csp_pins_cdn_sources_to_exact_paths_not_whole_origins(self):
        """Liberar a origem inteira do CDN deixaria qualquer pacote nele publicado
        executar sob a política (contornando o nonce). Cada fonte externa de
        script/estilo é exatamente um recurso do inventário SRI; o prefixo das
        fontes de ícone é o único caminho de diretório, e só em font-src."""
        csp = self._csp(self.client.get("/auth/login"))
        diretivas = {d.split(" ", 1)[0]: d.split(" ", 1)[1].split() for d in csp.split("; ") if " " in d}
        scripts = [s for s in diretivas["script-src"] if s.startswith("https://")]
        self.assertEqual(sorted(scripts), sorted(u for u in SRI if u.endswith(".js")))
        estilos = [s for s in diretivas["style-src"] if s.startswith("https://")]
        self.assertEqual(sorted(u for u in estilos if "jsdelivr" in u), sorted(u for u in SRI if u.endswith(".css")))
        for diretiva in ("script-src", "style-src", "font-src"):
            for fonte in diretivas[diretiva]:
                self.assertNotIn(fonte, ("https://cdn.jsdelivr.net", "https://cdn.jsdelivr.net/", "https://cdn.socket.io"),
                                 f"{diretiva} libera a origem inteira {fonte}")
        self.assertIn("https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/", diretivas["font-src"])

    def _diretivas(self, caminho):
        csp = self._csp(self.client.get(caminho))
        return {d.split(" ", 1)[0]: d.split(" ", 1)[1].split() for d in csp.split("; ") if " " in d}

    def test_only_the_landing_connects_to_the_lead_microservice(self):
        """Os diálogos de cadastro da landing enviam o lead ao microsserviço do Raio-X por fetch: só a landing
        recebe a origem dele (MICROSSERVICO_RAIOX_URL) no connect-src; o form-action continua 'self'."""
        origem = "https://nexo-externo.onrender.com"
        self.app.config["MICROSSERVICO_RAIOX_URL"] = origem
        landing = self._diretivas("/")
        self.assertEqual(landing["connect-src"][0], "'self'")
        self.assertEqual(landing["connect-src"].count(origem), 1)
        self.assertEqual(landing["form-action"], ["'self'"])
        self.assertEqual(landing["default-src"], ["'self'"])
        for caminho in ("/auth/login", "/como-aderir", "/healthz", "/rota-inexistente"):
            with self.subTest(caminho=caminho):
                self.assertNotIn(origem, self._diretivas(caminho)["connect-src"])

    def test_invalid_microservice_url_never_reaches_the_policy(self):
        """Um valor posto direto no config é conferido de novo antes de ir ao cabeçalho: só https://host[:porta]
        entra, e nada injeta diretiva nem libera outra origem."""
        casos = {
            "https://Nexo-Externo.onrender.com/": "https://nexo-externo.onrender.com",
            "https://servico.example.invalid:8443": "https://servico.example.invalid:8443",
            "http://nexo-externo.onrender.com": None,
            "https://nexo-externo.onrender.com/api/leads": None,
            "https://servico.example.invalid; script-src *": None,
            "https://servico.example.invalid script-src": None,
            "https://usuario@servico.example.invalid": None,
            "javascript:alert(1)": None,
            "": None,
            None: None,
        }
        for valor, esperado in casos.items():
            with self.subTest(valor=valor):
                self.app.config["MICROSSERVICO_RAIOX_URL"] = valor
                diretivas = self._diretivas("/")
                externas = [fonte for fonte in diretivas["connect-src"] if fonte.startswith("https://")]
                self.assertEqual(externas, [esperado] if esperado else [])
                self.assertNotIn("*", diretivas["script-src"])

    def test_inline_scripts_use_the_request_nonce_and_nonces_rotate(self):
        paginas = []
        for _ in range(2):
            resposta = self.client.get("/auth/login")
            csp = self._csp(resposta)
            nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
            html = resposta.get_data(as_text=True)
            self.assertGreaterEqual(len(nonce), 20)
            self.assertIn(f'<script nonce="{nonce}">', html)
            self.assertNotIn("<script>", html)
            self.assertNotRegex(html, r"\son(click|submit|load|change|input)=")
            paginas.append(nonce)
        self.assertNotEqual(paginas[0], paginas[1])

    def test_authenticated_pages_have_nonce_scripts_and_no_store(self):
        self.login("admin-test@example.invalid")
        for caminho in ("/admin/dashboard", "/admin/guia", "/admin/empresas", f"/admin/analise/{self.analise_id}/relatorio"):
            with self.subTest(caminho=caminho):
                resposta = self.client.get(caminho)
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(resposta.headers["Cache-Control"], "no-store")
                nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", self._csp(resposta)).group(1)
                html = resposta.get_data(as_text=True)
                self.assertIn(f'nonce="{nonce}"', html)
                self.assertNotIn("<script>", html)
                self.assertNotIn("onsubmit=", html)
        # O formulário de remoção do Guia confirma via data-confirm (sem onsubmit).
        from models import GuiaTopico
        self.db.session.add(GuiaTopico(categoria="Categoria fictícia", pergunta="Pergunta fictícia?",
                                       resposta="Resposta fictícia."))
        self.db.session.commit()
        guia = self.client.get("/admin/guia").get_data(as_text=True)
        self.assertIn('data-confirm="Remover este tópico', guia)
        self.assertNotIn("onsubmit=", guia)

    def test_cache_policy_distinguishes_static_private_and_auth_routes(self):
        self.assertNotIn("Content-Security-Policy", self.client.get("/static/css/nexo.css").headers)
        self.assertNotEqual(self.client.get("/static/css/nexo.css").headers.get("Cache-Control"), "no-store")
        self.assertEqual(self.client.get("/auth/login").headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/auth/recuperar-senha").headers["Cache-Control"], "no-store")
        anonima = self.client.get("/")
        self.assertNotEqual(anonima.headers.get("Cache-Control"), "no-store")
        self.login()
        for caminho in ("/cliente/dashboard", "/cliente/tickets", "/notificacoes/1/abrir", "/api/suporte-bot"):
            with self.subTest(caminho=caminho):
                self.assertEqual(self.client.get(caminho).headers["Cache-Control"], "no-store")
        self.logout()
        self.assertEqual(self.client.get("/auth/login").headers["Cache-Control"], "no-store")

    def test_report_only_mode_switches_the_header_name(self):
        self.app.config["CSP_REPORT_ONLY"] = True
        try:
            resposta = self.client.get("/auth/login")
            self.assertIn("Content-Security-Policy-Report-Only", resposta.headers)
            self.assertNotIn("Content-Security-Policy", resposta.headers)
        finally:
            self.app.config["CSP_REPORT_ONLY"] = False

    def test_media_route_keeps_its_own_stricter_policy(self):
        import storage
        referencia = storage.salvar(b"\x89PNG\r\n\x1a\n", "guia/imagem-teste.png")
        self.db.session.commit()
        self.login("admin-test@example.invalid")
        resposta = self.client.get("/midia/" + referencia.split(":", 1)[1])
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.headers["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual(resposta.headers["X-Frame-Options"], "DENY")

    def test_production_sends_hsts_only_over_https(self):
        env = os.environ.copy()
        env.update(FLASK_ENV="production", SECRET_KEY="x" * 64, DATABASE_URL="postgresql://localhost/nexo_test",
                   NEXO_PUBLIC_URL="https://nexo-test.example.invalid", PROXY_FIX_HOPS="1",
                   PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1")
        codigo = (
            "import dotenv; dotenv.load_dotenv = lambda *a, **kw: False\n"
            "from app import create_app\n"
            "app = create_app('production')\n"
            "c = app.test_client()\n"
            "host = {'Host': 'nexo-test.example.invalid'}\n"
            "r = c.get('/healthz', headers={**host, 'X-Forwarded-Proto': 'https'})\n"
            "assert r.headers['Strict-Transport-Security'] == 'max-age=31536000', r.headers\n"
            "assert 'upgrade-insecure-requests' in r.headers['Content-Security-Policy']\n"
            "assert 'wss://' not in r.headers['Content-Security-Policy']\n"
            "assert 'ws://' not in r.headers['Content-Security-Policy']\n"
            "assert \"connect-src 'self';\" in r.headers['Content-Security-Policy']\n"
            "r = c.get('/healthz', headers=host)\n"
            "assert 'Strict-Transport-Security' not in r.headers\n"
        )
        resultado = subprocess.run([sys.executable, "-B", "-c", codigo], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(resultado.returncode, 0, resultado.stderr[-2000:])


class DemoScriptSecurityTests(unittest.TestCase):
    def setUp(self):
        ensure_test_environment()

    def test_import_has_no_side_effects_and_main_is_guarded(self):
        fonte = (ROOT / "criar_cliente_demo.py").read_text(encoding="utf-8")
        self.assertIn('if __name__ == "__main__":', fonte)
        self.assertNotIn("nexo12345", fonte)
        # Nenhuma atribuição de senha literal no código (a docstring só cita a variável).
        self.assertNotRegex(fonte, r"(?m)^\s*\w*SENHA\w*\s*=\s*['\"]")
        self.assertNotIn("@nexo.com", fonte)
        self.assertNotIn("print(f\"  Senha temporária: {", fonte)

    def test_refuses_production_and_remote_databases_before_any_write(self):
        base = {"NEXO_AMBIENTE_DEMO": "1", "NEXO_DEMO_SENHA": "senha-temporaria-sintetica-123", "FLASK_ENV": "development"}
        recusas = [
            {**base, "NEXO_AMBIENTE_DEMO": "0"},
            {**base, "FLASK_ENV": "production"},
            {**base, "DATABASE_URL": "postgresql://user:x@db.example.invalid:5432/nexo"},
            {**base, "DATABASE_URL": "postgresql://user:x@aws-0-sa-east-1.pooler.supabase.invalid:6543/postgres"},
            {**base, "DIRECT_URL": "postgresql://user:x@db.example.invalid:5432/nexo"},
            {**base, "NEXO_DEMO_SENHA": "curta"},
            {**base, "NEXO_DEMO_SENHA": ""},
            {**base, "NEXO_DEMO_EMAIL": "sem-arroba"},
        ]
        for ambiente in recusas:
            with self.subTest(ambiente={k: v for k, v in ambiente.items() if base.get(k) != v}):
                with self.assertRaises(criar_cliente_demo.DemoRecusada):
                    criar_cliente_demo.verificar_ambiente(ambiente)
        with patch("app.create_app") as fabrica, patch.dict(os.environ, {**base, "FLASK_ENV": "production"}, clear=False):
            self.assertEqual(criar_cliente_demo.main(), 2)
            fabrica.assert_not_called()
        aceitos = [{**base}, {**base, "DATABASE_URL": "sqlite:///demo.db"},
                   {**base, "DATABASE_URL": "postgresql://u:p@localhost:5432/nexo"},
                   {**base, "DATABASE_URL": "postgresql://u:p@127.0.0.1/nexo", "DIRECT_URL": "postgresql://u:p@localhost/nexo"}]
        for ambiente in aceitos:
            with self.subTest(ambiente=ambiente.get("DATABASE_URL", "default")):
                parametros = criar_cliente_demo.verificar_ambiente(ambiente)
                self.assertEqual(parametros["email"], criar_cliente_demo.EMAIL_PADRAO)
                self.assertTrue(parametros["email"].endswith(".invalid"))

    def test_local_run_creates_the_demo_client_without_printing_the_password(self):
        from .deploy_support import DeployTestCase as _Caso

        class Caso(_Caso):
            def runTest(self):
                pass
        caso = Caso()
        caso.setUp()
        try:
            caso.fixtures()
            senha = "senha-temporaria-sintetica-123"
            import io
            import contextlib
            saida = io.StringIO()
            with contextlib.redirect_stdout(saida):
                resultado = criar_cliente_demo.criar_ou_rearmar(caso.app, criar_cliente_demo.EMAIL_PADRAO, senha)
            self.assertEqual(resultado["acao"], "criado")
            from models import Usuario
            from sqlalchemy import select
            with caso.app.app_context():
                usuario = caso.db.session.scalar(select(Usuario).where(Usuario.email == criar_cliente_demo.EMAIL_PADRAO))
                self.assertTrue(usuario.primeiro_acesso)
                self.assertTrue(usuario.check_senha(senha))
                self.assertEqual(usuario.role, "CLIENTE")
                self.assertEqual(criar_cliente_demo.criar_ou_rearmar(caso.app, criar_cliente_demo.EMAIL_PADRAO, senha)["acao"],
                                 "atualizado (flag re-armada)")
                admin = caso.db.session.scalar(select(Usuario).where(Usuario.role == "ADMIN"))
                with self.assertRaises(criar_cliente_demo.DemoRecusada):
                    criar_cliente_demo.criar_ou_rearmar(caso.app, admin.email, senha)
            self.assertNotIn(senha, saida.getvalue())
        finally:
            caso.doCleanups()


if __name__ == "__main__":
    unittest.main()
