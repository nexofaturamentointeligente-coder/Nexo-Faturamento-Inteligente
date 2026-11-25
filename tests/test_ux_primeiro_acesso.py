"""Telas de senha em navegador real (Chromium via Playwright), com a CSP real do servidor.

Ativação pelo link do e-mail (/auth/primeiro-acesso/<token>) e primeiro acesso com a senha temporária
(/auth/primeiro-acesso): um segundo clique no botão não manda a senha de novo (ele chegaria com o link já
usado, ou com a sessão já renovada, e mostraria um erro com a senha já salva); o prazo e o Voltar do
navegador liberam o formulário; o botão de olho troca o nome acessível junto com o ícone; a dica da senha
fica ligada ao campo e a tela cabe em 320 px. Os scripts dessas telas são os do próprio template (com
nonce): o guarda de envio único do base.html não chega a elas. Habilitado por NEXO_TEST_BROWSER=1, como
em tests/test_overflow_navegador.py; dados fictícios (example.invalid).
"""

import unittest

from tests.test_csp_navegador import _COLETOR
from tests.test_overflow_navegador import NavegadorCase, _BROWSER

NOVO = "cliente-primeiro-acesso@example.invalid"
NOVA_SENHA = "Frase-de-ativacao-robusta-2026"
FORMULARIO = "form[data-envio-unico]"

# Dois envios seguidos, como num clique duplo: para cada evento submit, se ele foi cancelado.
_DOIS_ENVIOS = """(seletor) => {
    const formulario = document.querySelector(seletor), vistos = [];
    window.addEventListener('submit', (evento) => vistos.push(evento.defaultPrevented));
    formulario.requestSubmit();
    formulario.requestSubmit();
    return vistos; }"""

# Sem navegar: depois do prazo (10 s) e no Voltar (pageshow persistido), o formulário e o botão voltam.
_PRAZO_E_VOLTAR = """(seletor) => {
    const formulario = document.querySelector(seletor), botao = formulario.querySelector('button[type="submit"]');
    const original = window.setTimeout, estado = () => [formulario.dataset.enviado || null, botao.disabled];
    let liberar = null;
    formulario.addEventListener('submit', (evento) => evento.preventDefault());  // o teste não navega
    window.setTimeout = (funcao, ms) => {
        if (ms >= 1000) { liberar = funcao; return 0; }
        return original(funcao, ms); };
    formulario.requestSubmit();
    window.setTimeout = original;
    const enviado = estado();
    liberar();
    const depoisDoPrazo = estado();
    formulario.requestSubmit();
    const reenviado = estado();
    window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
    return [enviado, depoisDoPrazo, reenviado, estado()]; }"""

# A mesma medida de tests/test_overflow_navegador.py (páginas públicas): rolagem e controle fora da tela.
_MEDIR = """() => ({rola: document.documentElement.scrollWidth > innerWidth + 1,
    fora: [...document.querySelectorAll('a, button, input, h1, p')].filter(el => {
        const b = el.getBoundingClientRect(); const cs = getComputedStyle(el);
        return b.width && cs.visibility !== 'hidden' && (b.left < -1 || b.right > innerWidth + 1); }).length})"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class PrimeiroAcessoNavegadorTests(NavegadorCase):
    def _novo_cliente(self):
        from models import Usuario
        usuario = Usuario(nome="Cliente no primeiro acesso", email=NOVO, role="CLIENTE",
                          id_empresa=self.empresa_id, primeiro_acesso=True)
        usuario.set_senha(self.password)
        self.db.session.add(usuario)
        self.db.session.commit()
        return usuario.id_usuario

    def _link_de_ativacao(self, id_usuario):
        from blueprints import auth
        from models import Usuario
        with self.app.app_context():
            return "/auth/primeiro-acesso/" + auth.gerar_token_definir_senha(self.db.session.get(Usuario, id_usuario))

    def _pagina(self, largura, altura):
        pagina = self.abrir(largura, altura)
        pagina.add_init_script(_COLETOR)
        return pagina

    def _posts(self, pagina):
        posts = []
        pagina.on("request", lambda pedido: posts.append(pedido.url) if pedido.method == "POST" else None)
        return posts

    def _primeiro_acesso_pendente(self, id_usuario):
        from models import Usuario
        with self.app.app_context():
            return self.db.session.get(Usuario, id_usuario).primeiro_acesso

    def assert_sem_violacao(self, pagina):
        self.assertEqual(pagina.evaluate("() => window.__cspViolations || []"), [])

    def assert_dica_e_olho(self, pagina):
        campo, olho = pagina.locator("#nova_senha"), pagina.locator("[data-toggle-pw='nova_senha']")
        self.assertEqual(campo.get_attribute("aria-describedby"), "nova_senha_dica")
        self.assertIn("Mínimo de 12 caracteres", pagina.locator("#nova_senha_dica").inner_text())
        for tipo, rotulo in (("text", "Ocultar senha"), ("password", "Mostrar senha")):
            olho.click()
            self.assertEqual((campo.get_attribute("type"), olho.get_attribute("aria-label"),
                              olho.get_attribute("title")), (tipo, rotulo, rotulo))

    def assert_cabe(self, pagina, contexto):
        medida = pagina.evaluate(_MEDIR)
        self.assertFalse(medida["rola"], f"{contexto}: a página rola na horizontal")
        self.assertEqual(medida["fora"], 0, f"{contexto}: elemento fora da tela")

    def preencher(self, pagina):
        pagina.fill("#nova_senha", NOVA_SENHA)
        pagina.fill("#confirma_senha", NOVA_SENHA)

    def test_ativacao_com_clique_duplo_envia_uma_vez_e_leva_ao_login(self):
        id_novo = self._novo_cliente()
        pagina = self._pagina(320, 568)
        self.ir(pagina, self._link_de_ativacao(id_novo))
        self.assert_cabe(pagina, "ativação @ 320px")
        self.assertIn("15 minutos", pagina.locator(".auth-tagline").inner_text())
        self.assert_dica_e_olho(pagina)
        self.preencher(pagina)
        posts = self._posts(pagina)
        with pagina.expect_navigation(wait_until="load"):
            cancelados = pagina.evaluate(_DOIS_ENVIOS, FORMULARIO)
        self.assertEqual(cancelados, [False, True])
        self.assertEqual(len(posts), 1)
        self.assertTrue(pagina.url.endswith("/auth/login"), pagina.url)
        self.assertIn("Conta ativada. Agora entre com seu e-mail e a senha que você criou.",
                      pagina.locator(".auth-flash-stack").inner_text())
        self.assertFalse(self._primeiro_acesso_pendente(id_novo))
        self.assert_sem_violacao(pagina)

    def test_prazo_e_volta_pelo_navegador_liberam_o_formulario_e_o_botao(self):
        pagina = self._pagina(1366, 900)
        self.ir(pagina, self._link_de_ativacao(self._novo_cliente()))
        self.preencher(pagina)
        self.assertEqual(pagina.evaluate(_PRAZO_E_VOLTAR, FORMULARIO),
                         [["1", True], [None, False], ["1", True], [None, False]])
        self.assert_sem_violacao(pagina)

    def test_primeiro_acesso_com_clique_duplo_envia_uma_vez_e_abre_o_portal(self):
        id_novo = self._novo_cliente()
        pagina = self._pagina(320, 568)
        self.entrar(pagina, NOVO)
        self.assertTrue(pagina.url.endswith("/auth/primeiro-acesso"), pagina.url)
        self.assert_cabe(pagina, "primeiro acesso @ 320px")
        self.assertIn("senha temporária", pagina.locator(".auth-sub").inner_text())
        # Só o formulário da senha tem o envio único: o "Sair" continua um POST comum.
        self.assertEqual(pagina.locator("form[data-envio-unico]").count(), 1)
        self.assertEqual(pagina.locator("form[action$='/auth/logout']:not([data-envio-unico])").count(), 1)
        self.assert_dica_e_olho(pagina)
        self.preencher(pagina)
        posts = self._posts(pagina)
        with pagina.expect_navigation(wait_until="load"):
            cancelados = pagina.evaluate(_DOIS_ENVIOS, FORMULARIO)
        self.assertEqual(cancelados, [False, True])
        self.assertEqual(len(posts), 1)
        self.assertTrue(pagina.url.endswith("/cliente/dashboard"), pagina.url)
        self.assertIn("Senha definida com sucesso", pagina.content())
        self.assertFalse(self._primeiro_acesso_pendente(id_novo))
        self.assert_sem_violacao(pagina)


if __name__ == "__main__":
    unittest.main()
