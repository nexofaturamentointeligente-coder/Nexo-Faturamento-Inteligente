"""GET /ping e /health: o despertador do Render Free.

Responde "OK" em texto puro, sem login, sem cache e sem nenhuma consulta ao banco — também com o
cookie de sessão de um cliente logado, no primeiro acesso ou com termos pendentes, e com o banco
fora do ar. O healthCheckPath do Render continua o /healthz (test_supply_chain_install).
"""
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from tests.deploy_support import DeployTestCase


class PingTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()

    def _comandos_sql(self):
        comandos = []

        def ouvir(_conexao, _cursor, comando, _parametros, _contexto, _varios):
            comandos.append(comando)

        event.listen(self.db.engine, "before_cursor_execute", ouvir)
        self.addCleanup(event.remove, self.db.engine, "before_cursor_execute", ouvir)
        return comandos

    def assert_ok(self, resposta):
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.get_data(as_text=True), "OK")
        self.assertEqual(resposta.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        # Os cabeçalhos de segurança de sempre (seguranca_http) valem também aqui.
        self.assertEqual(resposta.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resposta.headers["X-Frame-Options"], "DENY")
        self.assertIn("Content-Security-Policy", resposta.headers)
        self.assertNotIn("Set-Cookie", resposta.headers)

    def test_ping_e_health_respondem_ok_em_texto_puro(self):
        for caminho in ("/ping", "/health"):
            with self.subTest(caminho=caminho):
                self.assert_ok(self.client.get(caminho))
        cabeca = self.client.head("/ping")
        self.assertEqual(cabeca.status_code, 200)
        self.assertEqual(cabeca.data, b"")
        self.assertEqual(self.client.post("/ping").status_code, 405)
        regras = {regra.rule: regra for regra in self.app.url_map.iter_rules() if regra.endpoint == "ping"}
        self.assertEqual(set(regras), {"/ping", "/health"})
        for regra in regras.values():
            self.assertEqual(regra.methods, {"GET", "HEAD", "OPTIONS"})

    def test_sem_login_e_sem_consulta_ao_banco(self):
        comandos = self._comandos_sql()
        for caminho in ("/ping", "/health", "/ping"):
            self.assert_ok(self.client.get(caminho))
        self.assertEqual(comandos, [])

    def test_cookie_de_cliente_logado_nao_faz_consultar_o_banco(self):
        self.login()
        comandos = self._comandos_sql()
        self.assert_ok(self.client.get("/ping"))
        self.assertEqual(comandos, [])
        # Controle: a mesma sessão numa página do portal consulta o banco (usuário e sessão).
        self.client.get("/cliente/dashboard")
        self.assertNotEqual(comandos, [])

    def test_primeiro_acesso_nao_desvia_o_ping(self):
        from models import Usuario
        usuario = self.db.session.get(Usuario, self.cliente_id)
        usuario.primeiro_acesso = True
        self.db.session.commit()
        self.login()
        # Controle: no portal, a mesma sessão vai para a troca de senha.
        self.assertIn("/auth/primeiro-acesso", self.client.get("/cliente/dashboard").headers.get("Location", ""))
        comandos = self._comandos_sql()
        self.assert_ok(self.client.get("/ping"))
        self.assertEqual(comandos, [])

    def test_trava_dos_termos_nao_roda_no_ping(self):
        import termos_legais
        self.assertIn("ping", termos_legais.ENDPOINTS_LIBERADOS)
        self.login()
        with patch.object(termos_legais, "pendentes_da_sessao", return_value=[]) as pendentes:
            self.client.get("/cliente/dashboard")
            self.assertTrue(pendentes.called)  # controle: no portal, a trava confere os termos
            pendentes.reset_mock()
            self.assert_ok(self.client.get("/ping"))
        pendentes.assert_not_called()

    def test_banco_fora_do_ar_nao_derruba_o_ping(self):
        self.login()

        def banco_fora(*_argumentos):
            raise OperationalError("SELECT", {}, Exception("banco fora do ar"))

        event.listen(self.db.engine, "before_cursor_execute", banco_fora)
        self.addCleanup(event.remove, self.db.engine, "before_cursor_execute", banco_fora)
        # Controle: com o banco fora, a mesma sessão no portal recebe o 503 da autenticação.
        self.assertEqual(self.client.get("/cliente/dashboard").status_code, 503)
        self.assert_ok(self.client.get("/ping"))
        self.assert_ok(self.client.get("/health"))

    def test_numero_comercial_vazio_nos_testes(self):
        # create_app passa pelo whatsapp_service.configurar: vazio (o caso base força) e não inválido.
        self.assertEqual(self.app.config["WHATSAPP_EMPRESA_NUMERO"], "")
        self.assertIs(self.app.config["WHATSAPP_EMPRESA_NUMERO_INVALIDO"], False)
        self.assertIsNone(self.app.config["EVOLUTION_API_URL"])
