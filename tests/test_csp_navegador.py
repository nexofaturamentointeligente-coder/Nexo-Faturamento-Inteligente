"""SEC-010 no navegador: a CSP aplicada não bloqueia nada que o portal usa.

Cada página relevante (pública, administrativa e do cliente) é aberta no
Chromium com a política REAL enviada pelo servidor; qualquer violação
(securitypolicyviolation) ou erro de console reprova o teste. Gráficos
Plotly, Socket.IO, Bootstrap, NexoBot e os formulários com confirmação
são exercitados com as cópias íntegras da CDN (NEXO_TEST_CDN_DIR).
"""

import json
import unittest

from tests.test_overflow_navegador import NavegadorCase, _BROWSER

_COLETOR = """
window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__cspViolations.push({diretiva: e.violatedDirective, origem: e.blockedURI, linha: e.lineNumber,
                               fonte: e.sourceFile, amostra: (e.sample || '').slice(0, 80)});
});
"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class CSPNavegadorTests(NavegadorCase):
    def _violacoes(self, pagina):
        return pagina.evaluate("() => window.__cspViolations || []")

    def _abrir_com_coletor(self):
        pagina = self.abrir(1366, 900)
        pagina.add_init_script(_COLETOR)
        return pagina

    def _assert_pagina_limpa(self, pagina, caminho, status=200):
        self.ir(pagina, caminho, status)
        pagina.wait_for_timeout(400)
        violacoes = self._violacoes(pagina)
        self.assertEqual(violacoes, [], f"{caminho}: {json.dumps(violacoes, ensure_ascii=False)}")
        # A página inteira executou (scripts com nonce rodaram): o shell define NEXO.
        if status == 200 and caminho.startswith(("/admin/", "/cliente/")):
            self.assertTrue(pagina.evaluate("() => typeof window.NEXO === 'object'"), caminho)

    def test_public_pages_run_under_csp_without_violations(self):
        pagina = self._abrir_com_coletor()
        for caminho in ("/", "/como-aderir", "/auth/login", "/auth/recuperar-senha", "/pagina-inexistente"):
            with self.subTest(caminho=caminho):
                self._assert_pagina_limpa(pagina, caminho, 404 if "inexistente" in caminho else 200)
        self.assertEqual(self.erros_js, [])

    def test_admin_pages_charts_and_confirm_form_work_under_csp(self):
        pagina = self._abrir_com_coletor()
        self.entrar(pagina, "admin-test@example.invalid")
        for caminho in ("/admin/dashboard", "/admin/empresas", "/admin/analises", "/admin/lixeira",
                        f"/admin/analise/{self.analise_id}/relatorio", "/admin/guia", "/admin/tickets",
                        "/admin/contratos"):
            with self.subTest(caminho=caminho):
                self._assert_pagina_limpa(pagina, caminho)
        self.ir(pagina, "/admin/dashboard")
        pagina.wait_for_function("() => document.querySelectorAll('.chart-box .plot-container').length > 0",
                                 timeout=15000)
        self.assertTrue(pagina.evaluate("() => typeof Plotly !== 'undefined' && typeof bootstrap !== 'undefined'"))
        self.assertEqual(self._violacoes(pagina), [])
        # Formulário com data-confirm: a confirmação (sem onsubmit) acontece e pode cancelar o envio.
        self.ir(pagina, "/admin/guia")
        if pagina.locator("form[data-confirm]").count():
            pagina.once("dialog", lambda dialogo: dialogo.dismiss())
            pagina.locator("form[data-confirm] button[type=submit]").first.click()
            pagina.wait_for_timeout(300)
            self.assertTrue(pagina.url.endswith("/admin/guia"), "envio cancelado pela confirmação")
        self.assertEqual(self._violacoes(pagina), [])
        self.assertEqual(self.erros_js, [])

    def test_client_pages_socket_and_nexobot_work_under_csp(self):
        pagina = self._abrir_com_coletor()
        self.entrar(pagina, "cliente-test@example.invalid")
        for caminho in ("/cliente/dashboard", f"/cliente/analise/{self.analise_id}", "/cliente/tickets",
                        "/cliente/upload", "/cliente/historico", "/onboarding/diagnostico", "/onboarding/planos"):
            with self.subTest(caminho=caminho):
                self._assert_pagina_limpa(pagina, caminho)
        self.ir(pagina, "/cliente/dashboard")
        pagina.wait_for_function("() => typeof io !== 'undefined'", timeout=15000)
        conectado = pagina.evaluate("""() => new Promise((resolve) => {
            var s = io({transports: ['websocket', 'polling']});
            var fim = setTimeout(function () { resolve(false); }, 8000);
            s.on('connect', function () { clearTimeout(fim); s.disconnect(); resolve(true); });
        })""")
        self.assertTrue(conectado, "Socket.IO bloqueado pela CSP (connect-src)")
        self.assertTrue(pagina.locator("#nexobot").count() >= 1)
        self.assertEqual(self._violacoes(pagina), [])
        self.assertEqual(self.erros_js, [])

    def test_response_headers_seen_by_the_browser(self):
        pagina = self._abrir_com_coletor()
        resposta = pagina.goto(self.base + "/auth/login", wait_until="load")
        cabecalhos = resposta.headers
        self.assertIn("content-security-policy", cabecalhos)
        self.assertEqual(cabecalhos.get("x-frame-options"), "DENY")
        self.assertEqual(cabecalhos.get("cache-control"), "no-store")
        self.assertIn("nonce-", cabecalhos["content-security-policy"])
        self.assertEqual(self._violacoes(pagina), [])


if __name__ == "__main__":
    unittest.main()
