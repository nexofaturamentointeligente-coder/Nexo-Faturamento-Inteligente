"""/socket.io/ desativado: abas abertas antes da troca do sininho por polling.

O cliente Socket.IO antigo continua tentando o handshake (GET e, no transporte de
polling, POST) até a aba ser recarregada. A resposta é 410 em texto puro: sem login,
sem banco, sem template, sem CSRF e sem cache, como o /ping, também com o cookie de
um cliente logado, no primeiro acesso, com termos pendentes e com o banco fora do ar.
"""
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from tests.deploy_support import DeployTestCase

CAMINHOS = (
    "/socket.io/?EIO=4&transport=polling&t=PfakeX1",
    "/socket.io/?EIO=4&transport=websocket&sid=abc",
    "/socket.io/",
    "/socket.io/qualquer/coisa",
)


class SocketIODesativadoTests(DeployTestCase):
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

    def assert_410(self, resposta):
        self.assertEqual(resposta.status_code, 410)
        self.assertEqual(resposta.get_data(as_text=True), "Canal desativado: recarregue a página.")
        self.assertEqual(resposta.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        self.assertEqual(resposta.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Set-Cookie", resposta.headers)

    def test_get_e_post_sem_csrf_recebem_410_sem_consultar_o_banco(self):
        comandos = self._comandos_sql()
        for caminho in CAMINHOS:
            with self.subTest(caminho=caminho):
                self.assert_410(self.client.get(caminho))
                # O transporte de polling do cliente antigo faz POST, sem token CSRF.
                self.assert_410(self.client.post(caminho, data="2probe",
                                                 headers={"Content-Type": "text/plain;charset=UTF-8"}))
        self.assertEqual(comandos, [])
        regras = [regra for regra in self.app.url_map.iter_rules() if regra.endpoint == "socketio_desativado"]
        self.assertEqual({regra.rule for regra in regras}, {"/socket.io/", "/socket.io/<path:resto>"})

    def test_cookie_de_cliente_logado_nao_faz_consultar_o_banco(self):
        self.login()
        comandos = self._comandos_sql()
        self.assert_410(self.client.get(CAMINHOS[0]))
        self.assertEqual(comandos, [])
        # Controle: a mesma sessão numa página do portal consulta o banco.
        self.client.get("/cliente/dashboard")
        self.assertNotEqual(comandos, [])

    def test_primeiro_acesso_e_trava_dos_termos_nao_rodam(self):
        import termos_legais
        from models import Usuario
        self.assertIn("socketio_desativado", termos_legais.ENDPOINTS_LIBERADOS)
        usuario = self.db.session.get(Usuario, self.cliente_id)
        usuario.primeiro_acesso = True
        self.db.session.commit()
        self.login()
        self.assertIn("/auth/primeiro-acesso", self.client.get("/cliente/dashboard").headers.get("Location", ""))
        with patch.object(termos_legais, "pendentes_da_sessao", return_value=[]) as pendentes:
            comandos = self._comandos_sql()
            self.assert_410(self.client.get(CAMINHOS[0]))
        self.assertEqual(comandos, [])
        pendentes.assert_not_called()

    def test_banco_fora_do_ar_nao_muda_a_resposta(self):
        self.login()

        def banco_fora(*_argumentos):
            raise OperationalError("SELECT", {}, Exception("banco fora do ar"))

        event.listen(self.db.engine, "before_cursor_execute", banco_fora)
        self.addCleanup(event.remove, self.db.engine, "before_cursor_execute", banco_fora)
        # Controle: com o banco fora, a mesma sessão no portal recebe o 503.
        self.assertEqual(self.client.get("/cliente/dashboard").status_code, 503)
        self.assert_410(self.client.get(CAMINHOS[0]))
        self.assert_410(self.client.post(CAMINHOS[0], data="2probe"))
