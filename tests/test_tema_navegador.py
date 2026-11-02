"""Tema claro/escuro em navegador real (Chromium via Playwright), com a CSP real do servidor.

Alternar persiste entre páginas e após recarga; o <html> já está no tema antes da primeira
pintura, sem clique; sem violação de CSP nem erro no console; localStorage bloqueado não quebra
e o botão ainda alterna a página atual; valor inválido é ignorado; a tela de login continua
escura com theme=light guardado; sem rolagem horizontal em 320 px no claro; amostras principais
passam o contraste AA, compondo os fundos semitransparentes; o indicador de foco passa 3:1 contra
os pixels reais em volta dele; alternar antes de os gráficos serem desenhados (CDN lenta) não
deixa paleta velha; a logo da sidebar continua transparente e o texto igual ao do tema escuro;
botões do NexoBot, item não lido do sino e dropzone mudam no hover e no foco; nome de usuário
longo cede lugar ao título da topbar, nos dois temas, sem mudar nada no nome que cabe.

Habilitado por NEXO_TEST_BROWSER=1, como em tests/test_overflow_navegador.py (fixtures, CDN local
de hash conferido e coleta de erros de console vêm de NavegadorCase).
"""

import io
import json
import unittest

from PIL import Image

from tests.test_csp_navegador import _COLETOR
from tests.test_overflow_navegador import NavegadorCase, _BROWSER
from tests.test_tema_interface import _luminancia

ADMIN = "admin-test@example.invalid"
CLIENTE = "cliente-test@example.invalid"
# Armazenamento que lança exceção ao ser acessado, como num navegador com dados de site bloqueados.
_BLOQUEAR_STORAGE = """['localStorage', 'sessionStorage'].forEach(function (nome) {
    Object.defineProperty(window, nome, {
        configurable: true, get: function () { throw new DOMException('bloqueado no teste', 'SecurityError'); } });
});"""
# Instante em que o atributo mudou e tema no momento em que o <body> passa a existir.
_RELOGIO_DO_TEMA = """(function () {
    window.__temaAoNascerBody = null; window.__temaAplicadoEm = null;
    new MutationObserver(function () {
        var raiz = document.documentElement;
        if (raiz && window.__temaAplicadoEm === null && raiz.getAttribute('data-theme') === 'light') window.__temaAplicadoEm = performance.now();
        if (document.body && window.__temaAoNascerBody === null) window.__temaAoNascerBody = raiz.getAttribute('data-theme');
    }).observe(document, {childList: true, subtree: true, attributes: true});
})();"""
_ESTADO = """() => { const raiz = document.documentElement, b = document.getElementById('themeToggle');
    return {tema: raiz.getAttribute('data-theme'), bs: raiz.getAttribute('data-bs-theme'),
            rotulo: b && b.getAttribute('aria-label'),
            icone: b && b.querySelector('i').className}; }"""
_CLARO = {"tema": "light", "bs": "light", "rotulo": "Ativar tema escuro", "icone": "bi bi-moon"}
_ESCURO = {"tema": "dark", "bs": "dark", "rotulo": "Ativar tema claro", "icone": "bi bi-sun"}
# Compõe os fundos semitransparentes até o opaco; um gradiente no caminho fica registrado.
_CONTRASTE = """(seletores) => {
    const cor = s => { const m = s.match(/rgba?\\(([^)]+)\\)/); if (!m) return {r: 0, g: 0, b: 0, a: 0};
        const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(parseFloat); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
    const sobre = (t, b) => { const a = t.a + b.a * (1 - t.a); if (!a) return {r: 0, g: 0, b: 0, a: 0};
        return {r: (t.r * t.a + b.r * b.a * (1 - t.a)) / a, g: (t.g * t.a + b.g * b.a * (1 - t.a)) / a, b: (t.b * t.a + b.b * b.a * (1 - t.a)) / a, a}; };
    const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    const lum = c => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
    const razao = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
    const fundo = el => { let c = {r: 0, g: 0, b: 0, a: 0};
        for (let e = el; e; e = e.parentElement) { c = sobre(c, cor(getComputedStyle(e).backgroundColor)); if (c.a >= 1) return c; }
        return sobre(c, {r: 255, g: 255, b: 255, a: 1}); };
    return seletores.map(s => { const el = document.querySelector(s); if (!el) return {seletor: s, ausente: true};
        const cs = getComputedStyle(el), f = fundo(el), t = sobre(cor(cs.color), f), fs = parseFloat(cs.fontSize), peso = parseInt(cs.fontWeight, 10);
        const grande = fs >= 24 || (fs >= 18.66 && peso >= 700);
        return {seletor: s, razao: razao(t, f), minimo: grande ? 3 : 4.5, cor: cs.color, fundo: [f.r, f.g, f.b].map(Math.round).join(',')}; });
}"""
# Corte do título: abaixo de 576 px ele quebra em até duas linhas (line-clamp, corte na vertical);
# acima, fica numa linha só com reticências (corte na horizontal).
_MEDIR_TITULO = """() => { const t = document.querySelector('.topbar-title');
    return {rola: document.documentElement.scrollWidth > innerWidth + 1, titulo: t.textContent.trim(),
            corte: t.scrollWidth > t.clientWidth + 1 || t.scrollHeight > t.clientHeight + 1,
            avatar: getComputedStyle(document.querySelector('.topbar-right .user-chip')).display !== 'none'}; }"""
_TITULO_LONGO = "Título bem comprido para forçar três linhas dentro da barra do topo do portal"
# A marcação das Configurações e de admin/contrato_acao.html (nenhuma página de teste exibe as duas).
_CAIXAS = ('<div class="form-check mb-2"><input class="form-check-input" type="checkbox" id="testeCaixa">'
           '<label class="form-check-label" for="testeCaixa">Caixa</label></div>'
           '<div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" role="switch" id="testeSwitch">'
           '<label class="form-check-label" for="testeSwitch">Interruptor</label></div>')
_SEM_TRANSICAO = "*,*::before,*::after{transition:none!important;animation:none!important}"
# A marcação de nexobot.js para os botões internos (a API só os dá em fluxos próprios).
_BOTOES_DO_BOT = ('<div class="nb-msg nb-bot"><div class="nb-bubble">'
                  '<button type="button" class="nb-navigation" id="testeNavegacao">Abrir <i class="bi bi-arrow-right-short"></i></button>'
                  '<div class="nb-opcoes"><button type="button" class="nb-opcao" id="testeOpcao">Opção</button></div>'
                  '<div class="nb-chips"><button type="button" class="nb-chip is-usada" id="testeUsada">Usada</button></div>'
                  '</div></div>')
_PINTURA = """(e) => { const c = getComputedStyle(e);
    return {cor: c.color, fundo: c.backgroundColor, imagem: c.backgroundImage, borda: c.borderTopColor}; }"""
_FOCO_VISIVEL = """(() => { const e = document.activeElement;
    return {classe: e.className, teclado: e.matches(':focus-visible'), fundo: getComputedStyle(e).backgroundColor}; })()"""
# Maior título do portal, com o nome do usuário e o título medidos na mesma tela.
_MEDIR_NOME = """(titulo) => { const t = document.querySelector('.topbar-title'), s = document.querySelector('.user-meta strong');
    t.lastChild.textContent = titulo;
    return {corte: t.scrollWidth > t.clientWidth + 1, nomeCortado: s.scrollWidth > s.clientWidth + 1, nomeTitle: s.getAttribute('title'),
            rola: document.documentElement.scrollWidth > innerWidth + 1}; }"""
_MAIOR_TITULO = "Resultado do processamento"
_NOME_47 = "Maria Aparecida de Souza Albuquerque Figueiredo"
_NOME_120 = ("Maria Aparecida de Souza Albuquerque Figueiredo Cavalcanti de Vasconcelos "
             "Bragança e Orleans Pereira da Silva Nogueira Lima")
# Rótulo do eixo X desenhado em cada paleta (textStrong).
_EIXO = {"light": "rgb(27, 22, 48)", "dark": "rgb(236, 233, 248)"}


def _razao_pixels(a, b):
    x, y = _luminancia(a), _luminancia(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class TemaNavegadorTests(NavegadorCase):
    def _sessao(self, email, tema=None, largura=1366, altura=768, antes=None):
        pagina = self.abrir(largura, altura)
        for script in antes or ():
            pagina.add_init_script(script)
        pagina.goto(self.base + "/auth/login", wait_until="load")
        if tema:
            pagina.evaluate("(t) => localStorage.setItem('theme', t)", tema)
        pagina.fill("#email", email)
        pagina.fill("#senha", self.password)
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("button[type=submit]")
        return pagina

    def _estado(self, pagina):
        return pagina.evaluate(_ESTADO)

    def _esperar(self, pagina, expressao):
        """wait_for_function repete a expressão com eval, que a CSP do portal barra (só a primeira
        passa, dentro da avaliação do Playwright): a repetição fica aqui."""
        for _ in range(200):
            if pagina.evaluate(expressao):
                return
            pagina.wait_for_timeout(50)
        self.fail(f"não aconteceu: {expressao}")

    def test_alternar_persiste_entre_paginas_e_apos_recarga(self):
        pagina = self._sessao(CLIENTE)
        self.ir(pagina, "/cliente/historico")
        self.assertEqual(self._estado(pagina), _ESCURO)
        # Sem gráfico a troca é instantânea: a página não recarrega.
        pagina.evaluate("window.__mesmaPagina = true")
        pagina.click("#themeToggle")
        self.assertEqual(self._estado(pagina), _CLARO)
        self.assertTrue(pagina.evaluate("window.__mesmaPagina === true"))
        self.assertEqual(pagina.evaluate("localStorage.getItem('theme')"), "light")
        self.ir(pagina, "/cliente/tickets")
        self.assertEqual(self._estado(pagina), _CLARO)
        pagina.reload(wait_until="load")
        self.assertEqual(self._estado(pagina), _CLARO)
        pagina.click("#themeToggle")
        self.assertEqual(self._estado(pagina), _ESCURO)
        self.assertEqual(pagina.evaluate("localStorage.getItem('theme')"), "dark")
        self.ir(pagina, "/cliente/guia")
        self.assertEqual(self._estado(pagina), _ESCURO)
        # Voltar/avançar pelo cache de navegação: a página restaurada segue o valor guardado.
        pagina.evaluate("""() => { localStorage.setItem('theme', 'light');
            window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true})); }""")
        self.assertEqual(self._estado(pagina), _CLARO)

    def test_teclado_alterna_o_tema(self):
        pagina = self._sessao(ADMIN)
        self.ir(pagina, "/admin/empresas")
        pagina.keyboard.press("Tab")
        pagina.focus("#themeToggle")
        pagina.keyboard.press("Enter")
        self.assertEqual(self._estado(pagina), _CLARO)
        pagina.keyboard.press("Space")
        self.assertEqual(self._estado(pagina), _ESCURO)
        self.assertEqual(pagina.evaluate("document.activeElement.id"), "themeToggle")
        # Sem recarga, a página seguinte não rouba o foco para o botão.
        self.ir(pagina, "/admin/lixeira")
        self.assertNotEqual(pagina.evaluate("document.activeElement.id"), "themeToggle")
        # Com gráfico a página recarrega: pelo teclado, o foco volta ao botão.
        self.ir(pagina, "/admin/dashboard")
        pagina.wait_for_selector(".js-plotly-plot")
        pagina.keyboard.press("Tab")
        pagina.focus("#themeToggle")
        with pagina.expect_navigation(wait_until="load"):
            pagina.keyboard.press("Enter")
        self.assertEqual(self._estado(pagina), _CLARO)
        self.assertEqual(pagina.evaluate("document.activeElement.id"), "themeToggle")

    def test_pagina_com_grafico_recarrega_e_troca_a_paleta(self):
        pagina = self._sessao(CLIENTE)
        self.ir(pagina, "/cliente/dashboard")
        pagina.wait_for_selector(".js-plotly-plot .xtick text")
        self.assertEqual(self._estado(pagina), _ESCURO)
        self.assertEqual(pagina.evaluate("NEXO.colors.textStrong"), "#ece9f8")
        pagina.evaluate("window.__mesmaPagina = true")
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("#themeToggle")
        pagina.wait_for_selector(".js-plotly-plot .xtick text")
        self.assertEqual(self._estado(pagina), _CLARO)
        self.assertFalse(pagina.evaluate("window.__mesmaPagina === true"))
        self.assertEqual(pagina.evaluate("NEXO.tema"), "light")
        self.assertEqual(pagina.evaluate("NEXO.colors.textStrong"), "#1b1630")
        # O rótulo do eixo desenhado pelo Plotly segue a paleta clara.
        preenchimento = pagina.evaluate("getComputedStyle(document.querySelector('#chartCliente .xtick text')).fill")
        self.assertEqual(preenchimento, "rgb(27, 22, 48)")
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("#themeToggle")
        self.assertEqual(self._estado(pagina), _ESCURO)
        self.assertEqual(pagina.evaluate("NEXO.colors.textStrong"), "#ece9f8")

    def test_tema_aplicado_antes_da_primeira_pintura_sem_clique(self):
        pagina = self._sessao(ADMIN, tema="light", antes=[_RELOGIO_DO_TEMA])
        self.ir(pagina, "/admin/empresas")
        self.assertEqual(self._estado(pagina), _CLARO)
        self.assertEqual(pagina.evaluate("window.__temaAoNascerBody"), "light")
        aplicado = pagina.evaluate("window.__temaAplicadoEm")
        pinturas = pagina.evaluate("performance.getEntriesByType('paint').map(p => [p.name, p.startTime])")
        self.assertIsNotNone(aplicado)
        for nome, instante in pinturas:
            self.assertLess(aplicado, instante, nome)
        # A folha clara já vale no fundo do portal.
        self.assertEqual(pagina.evaluate("getComputedStyle(document.body).backgroundColor"), "rgb(243, 240, 250)")

    def test_sem_violacao_de_csp_nem_erro_no_console_no_claro(self):
        for email, caminhos in ((ADMIN, ("/admin/dashboard", "/admin/empresas", "/admin/suporte", "/admin/configuracoes")),
                                (CLIENTE, ("/cliente/dashboard", f"/cliente/analise/{self.analise_id}", "/cliente/upload", "/cliente/configuracoes"))):
            pagina = self._sessao(email, tema="light", antes=[_COLETOR])
            for caminho in caminhos:
                with self.subTest(caminho=caminho):
                    self.ir(pagina, caminho)
                    pagina.wait_for_timeout(300)
                    self.assertEqual(self._estado(pagina), _CLARO)
                    violacoes = pagina.evaluate("window.__cspViolations || []")
                    self.assertEqual(violacoes, [], f"{caminho}: {json.dumps(violacoes, ensure_ascii=False)}")
            # A troca com recarga e a instantânea também não violam nada.
            self.ir(pagina, caminhos[1])
            pagina.evaluate("document.getElementById('themeToggle').click()")
            pagina.wait_for_timeout(600)
            self.assertEqual(pagina.evaluate("window.__cspViolations || []"), [])
            pagina.context.close()
        self.assertEqual(self.erros_js, [])

    def test_localstorage_bloqueado_nao_quebra_e_o_botao_alterna_a_pagina_atual(self):
        pagina = self._sessao(CLIENTE, antes=[_BLOQUEAR_STORAGE])
        self.ir(pagina, "/cliente/historico")
        self.assertEqual(self._estado(pagina), _ESCURO)
        self.assertTrue(pagina.evaluate("typeof window.NEXO === 'object' && typeof bootstrap === 'object'"))
        pagina.click("#themeToggle")
        self.assertEqual(self._estado(pagina), _CLARO)
        pagina.click("#themeToggle")
        self.assertEqual(self._estado(pagina), _ESCURO)
        # Em página com gráfico não recarrega (perderia a escolha) e o gráfico continua legível:
        # desenhado na paleta escura, ele ganha um fundo dessa paleta.
        self.ir(pagina, "/cliente/dashboard")
        pagina.wait_for_selector(".js-plotly-plot .xtick text")
        pagina.evaluate("window.__mesmaPagina = true")
        pagina.click("#themeToggle")
        pagina.wait_for_timeout(200)
        self.assertEqual(self._estado(pagina), _CLARO)
        self.assertTrue(pagina.evaluate("window.__mesmaPagina === true"))
        self.assertEqual(pagina.evaluate("getComputedStyle(document.getElementById('chartCliente')).backgroundColor"), "rgb(20, 15, 39)")
        pagina.click("#themeToggle")
        self.assertEqual(self._estado(pagina), _ESCURO)
        self.assertEqual(pagina.evaluate("getComputedStyle(document.getElementById('chartCliente')).backgroundColor"), "rgba(0, 0, 0, 0)")
        # Sem como guardar, a página seguinte volta ao padrão.
        pagina.click("#themeToggle")
        self.ir(pagina, "/cliente/historico")
        self.assertEqual(self._estado(pagina), _ESCURO)

    def test_valor_invalido_em_localstorage_e_ignorado(self):
        pagina = self._sessao(ADMIN)
        for valor in ("banana", "LIGHT", "light ", "", "<img src=x onerror=alert(1)>"):
            with self.subTest(valor=valor):
                pagina.evaluate("(v) => localStorage.setItem('theme', v)", valor)
                self.ir(pagina, "/admin/empresas")
                self.assertEqual(self._estado(pagina), _ESCURO)

    def test_login_continua_escuro_com_theme_light_guardado(self):
        pagina = self.abrir(1366, 768)
        pagina.goto(self.base + "/auth/login", wait_until="load")
        pagina.evaluate("localStorage.setItem('theme', 'light')")
        for caminho, status in (("/auth/login", 200), ("/como-aderir", 200), ("/auth/recuperar-senha", 200), ("/pagina-inexistente", 404)):
            with self.subTest(caminho=caminho):
                resposta = pagina.goto(self.base + caminho, wait_until="load")
                self.assertEqual(resposta.status, status)
                self.assertEqual(pagina.evaluate("document.documentElement.getAttribute('data-bs-theme')"), "dark")
                self.assertNotEqual(pagina.evaluate("document.documentElement.getAttribute('data-theme')"), "light")
                self.assertEqual(pagina.locator("#themeToggle").count(), 0)
                self.assertEqual(pagina.locator('link[href*="nexo-tema-claro"]').count(), 0)
                self.assertEqual(pagina.evaluate("getComputedStyle(document.body).backgroundColor"), "rgb(8, 8, 13)")
                self.assertEqual(pagina.evaluate("localStorage.getItem('theme')"), "light")
        self.erros_js = [e for e in self.erros_js if "404" not in e]
        # Depois de sair, a preferência continua guardada e o login continua escuro.
        pagina = self._sessao(CLIENTE, tema="light")
        self.ir(pagina, "/cliente/historico")
        self.assertEqual(self._estado(pagina), _CLARO)
        pagina.evaluate("document.querySelector('.nav-logout-form').submit()")
        pagina.wait_for_url("**/auth/login*")
        self.assertEqual(pagina.evaluate("document.documentElement.getAttribute('data-theme')"), "dark")
        self.assertEqual(pagina.evaluate("getComputedStyle(document.body).backgroundColor"), "rgb(8, 8, 13)")

    def test_sem_rolagem_horizontal_em_320_px_no_claro(self):
        from models import ChamadoSuporte
        chamado = self.db.session.scalar(self.db.select(ChamadoSuporte.id_chamado).order_by(ChamadoSuporte.id_chamado))
        paginas = ((ADMIN, ("/admin/dashboard", "/admin/empresas", "/admin/empresas/nova", "/admin/contratos", "/admin/lixeira",
                            "/admin/analises", "/admin/analises/gerenciar", "/admin/suporte", f"/admin/tickets/{chamado}",
                            "/admin/configuracoes", "/admin/diagnostico-email", "/admin/guia")),
                   (CLIENTE, ("/cliente/dashboard", "/cliente/upload", "/cliente/historico", f"/cliente/analise/{self.analise_id}",
                              "/cliente/guia", "/cliente/tickets", f"/cliente/tickets/{chamado}", "/cliente/configuracoes")))
        for email, caminhos in paginas:
            pagina = self._sessao(email, tema="light", largura=320, altura=568)
            for caminho in caminhos:
                with self.subTest(caminho=caminho):
                    self.ir(pagina, caminho)
                    self.assertEqual(self._estado(pagina), _CLARO)
                    medida = pagina.evaluate(_MEDIR_TITULO)
                    self.assertFalse(medida["rola"], medida)
                    # O título da página não é cortado pelo botão novo da topbar.
                    self.assertFalse(medida["corte"], medida)
            pagina.context.close()

    def test_titulo_da_topbar_cabe_com_o_botao_novo_nos_dois_temas(self):
        """O botão de tema toma largura da topbar: abaixo de 390 px o avatar (sem ação) cede
        o lugar; em 390 px e acima a topbar mantém tudo o que já tinha."""
        for largura, altura, avatar in ((360, 780, False), (375, 812, False), (390, 844, True), (768, 1024, True)):
            pagina = self._sessao(ADMIN, largura=largura, altura=altura)
            for caminho in ("/admin/configuracoes", "/admin/diagnostico-email"):
                self.ir(pagina, caminho)
                for clique in (False, True):
                    if clique:
                        pagina.click("#themeToggle")
                    with self.subTest(largura=largura, caminho=caminho, tema=self._estado(pagina)["tema"]):
                        medida = pagina.evaluate(_MEDIR_TITULO)
                        self.assertFalse(medida["corte"], medida)
                        self.assertEqual(medida["avatar"], avatar, medida)
                pagina.click("#themeToggle")
            # A medida enxerga o corte nos dois modos: duas linhas (vertical) e uma linha (horizontal).
            pagina.evaluate("(texto) => { document.querySelector('.topbar-title').lastChild.textContent = texto; }", _TITULO_LONGO)
            with self.subTest(largura=largura, titulo="longo"):
                self.assertTrue(pagina.evaluate(_MEDIR_TITULO)["corte"])
            pagina.context.close()

    def test_contraste_das_amostras_principais(self):
        amostras = {
            (ADMIN, "/admin/dashboard"): (".page-head h1", ".page-head .sub", ".topbar-title", ".topbar-title small", ".user-meta strong",
                                          ".user-meta small", ".kpi-label", ".kpi-value", ".kpi-foot", ".nexo-card-head h2",
                                          ".nexo-card-head .sub", ".nexo-table thead th", ".nexo-table tbody td", ".nexo-table .t-mut",
                                          ".nexo-badge.b-green", ".btn-ghost"),
            (ADMIN, "/admin/empresas/nova"): (".nexo-label", ".nexo-label .req", ".form-control", ".form-select", ".field-hint", ".input-group-text"),
            (ADMIN, "/admin/lixeira"): (".nexo-badge.plan-badge", ".btn-ghost.is-danger", ".nexo-table .t-strong"),
            (CLIENTE, "/cliente/dashboard"): (".audit-k", ".audit-v", ".audit-foot", ".nota-pressao", ".sc-title", ".sc-key", ".sc-val",
                                              ".nexo-badge.b-green", ".contact-card p"),
            (CLIENTE, f"/cliente/analise/{self.analise_id}"): (".abc-pill", ".abc-pill.is-active", ".abc-badge.abc-a", ".abc-badge.abc-c", ".rb-label", ".rb-body"),
        }
        for (email, caminho), seletores in amostras.items():
            pagina = self._sessao(email, tema="light")
            self.ir(pagina, caminho)
            for medida in pagina.evaluate(_CONTRASTE, list(seletores)):
                with self.subTest(caminho=caminho, seletor=medida["seletor"]):
                    self.assertNotIn("ausente", medida, "amostra não encontrada na página")
                    self.assertGreaterEqual(medida["razao"], medida["minimo"], medida)
            # Texto em gradiente vira cor sólida: nada transparente no título da página.
            self.assertNotEqual(pagina.evaluate("getComputedStyle(document.querySelector('.page-head h1')).color"), "rgba(0, 0, 0, 0)")
            pagina.context.close()
        # Tabela que rola: a máscara do escuro apagaria o texto da última coluna.
        pagina = self._sessao(CLIENTE, tema="light", largura=390, altura=844)
        self.ir(pagina, "/cliente/dashboard")
        mascaras = pagina.evaluate("[...document.querySelectorAll('.table-responsive-x.rola-mais')].map(t => getComputedStyle(t).maskImage)")
        self.assertTrue(mascaras, "nenhuma tabela rolando em 390 px: o cenário mudou")
        self.assertEqual(set(mascaras), {"none"})

    def test_borda_de_campo_e_foco_passam_3_para_1(self):
        pagina = self._sessao(ADMIN, tema="light")
        self.ir(pagina, "/admin/empresas/nova")
        medida = pagina.evaluate("""() => {
            const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
            const lum = c => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
            const razao = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
            const rgb = s => s.match(/[\\d.]+/g).slice(0, 3).map(Number);
            const campo = document.querySelector('#razao_social'), cartao = campo.closest('.nexo-card');
            const fundo = rgb(getComputedStyle(cartao).backgroundColor);
            const repouso = razao(rgb(getComputedStyle(campo).borderTopColor), fundo);
            campo.focus();
            const foco = razao(rgb(getComputedStyle(campo).borderTopColor), fundo);
            return {repouso, foco, fundo}; }""")
        self.assertGreaterEqual(medida["repouso"], 3, medida)
        self.assertGreaterEqual(medida["foco"], 3, medida)
        # Botão de tema pelo teclado: contorno visível sobre a topbar clara.
        pagina.keyboard.press("Tab")
        pagina.focus("#themeToggle")
        contorno = pagina.evaluate("""() => { const cs = getComputedStyle(document.getElementById('themeToggle'));
            return {estilo: cs.outlineStyle, largura: parseFloat(cs.outlineWidth), cor: cs.outlineColor}; }""")
        self.assertEqual(contorno["estilo"], "solid")
        self.assertGreaterEqual(contorno["largura"], 2)
        self.assertEqual(contorno["cor"], "rgb(109, 40, 217)")

    def test_logo_da_sidebar_transparente_e_texto_igual_nos_dois_temas(self):
        medir = """() => { const img = document.querySelector('.sidebar-brand .brand-logo img'), pai = img.parentElement,
                cs = getComputedStyle(img), ps = getComputedStyle(pai),
                forte = document.querySelector('.brand-text strong'), fraco = document.querySelector('.brand-text small'),
                f = getComputedStyle(forte), s = getComputedStyle(fraco), caixa = img.getBoundingClientRect();
            return {img: [cs.backgroundColor, cs.backgroundImage, cs.boxShadow, cs.filter], altura: caixa.height,
                    pai: [ps.backgroundColor, ps.backgroundImage, ps.boxShadow, ps.borderRadius, ps.overflow],
                    src: img.getAttribute('src'), carregada: img.complete && img.naturalWidth === 253,
                    nome: forte.textContent.trim(), fonte_nome: [f.fontSize, f.fontWeight, f.letterSpacing, f.color, f.backgroundImage],
                    sub: fraco.textContent.trim(), fonte_sub: [s.fontSize, s.color, s.letterSpacing]}; }"""
        pagina = self._sessao(CLIENTE)
        self.ir(pagina, "/cliente/historico")
        escuro = pagina.evaluate(medir)
        pagina.click("#themeToggle")
        pagina.wait_for_timeout(250)  # transições do tema terminadas
        claro = pagina.evaluate(medir)
        self.assertEqual(claro, escuro)
        self.assertEqual(claro["img"], ["rgba(0, 0, 0, 0)", "none", "none", "none"])
        self.assertEqual(claro["pai"], ["rgba(0, 0, 0, 0)", "none", "none", "0px", "visible"])
        self.assertTrue(claro["carregada"])
        self.assertEqual((claro["nome"], claro["sub"]), ("NEXO", "Faturamento Inteligente"))
        # Sidebar escura e quase opaca; o resto da página, claro.
        fundo = pagina.evaluate("getComputedStyle(document.getElementById('sidebar')).backgroundColor")
        canais = [float(v) for v in fundo[fundo.index("(") + 1:-1].split(",")]
        self.assertLess(max(canais[:3]), 60, fundo)
        self.assertGreaterEqual(canais[3], 0.95, fundo)

    def _foco_por_lado(self, pagina, seletor):
        """Maior razão entre o mesmo pixel com e sem foco, nos 7 px além de cada lado do elemento.

        Compara com os pixels reais: o fundo em volta nem sempre é o do pai (o botão de fechar a
        gaveta fica sobre o véu, o link de pular cai sobre a sidebar e sobre o fundo claro)."""
        pagina.keyboard.press("Tab")
        m = pagina.evaluate("""(s) => { const e = document.querySelector(s); e.focus({preventScroll: true}); e.scrollIntoView({block: 'center'});
            const r = e.getBoundingClientRect();
            return {x0: Math.floor(r.left), y0: Math.floor(r.top), x1: Math.ceil(r.right), y1: Math.ceil(r.bottom), vw: innerWidth, vh: innerHeight,
                    teclado: e.matches(':focus-visible')}; }""", seletor)
        self.assertTrue(m["teclado"], f"{seletor}: a medida precisa do foco de teclado")
        margem = 8
        esquerda, topo = max(0, m["x0"] - margem), max(0, m["y0"] - margem)
        recorte = {"x": esquerda, "y": topo, "width": min(m["vw"], m["x1"] + margem) - esquerda, "height": min(m["vh"], m["y1"] + margem) - topo}
        com = Image.open(io.BytesIO(pagina.screenshot(clip=recorte, animations="disabled"))).convert("RGB")
        pagina.evaluate("(s) => document.querySelector(s).blur()", seletor)
        sem = Image.open(io.BytesIO(pagina.screenshot(clip=recorte, animations="disabled"))).convert("RGB")
        meio_x, meio_y = (m["x0"] + m["x1"]) // 2 - esquerda, (m["y0"] + m["y1"]) // 2 - topo
        pontos = {"direita": [(m["x1"] - 1 + d - esquerda, meio_y) for d in range(1, 8)], "esquerda": [(m["x0"] - d - esquerda, meio_y) for d in range(1, 8)],
                  "cima": [(meio_x, m["y0"] - d - topo) for d in range(1, 8)], "baixo": [(meio_x, m["y1"] - 1 + d - topo) for d in range(1, 8)]}
        lados = {}
        for lado, lista in pontos.items():
            dentro = [p for p in lista if 0 <= p[0] < com.width and 0 <= p[1] < com.height]
            if dentro:
                lados[lado] = round(max(_razao_pixels(com.getpixel(p), sem.getpixel(p)) for p in dentro), 2)
        return lados

    def _reter_script(self, pagina, caminho, padrao="**/socket.io.min.js"):
        """Vai à página com um script da CDN preso, como numa CDN lenta. Devolve o que o solta."""
        presos = []
        pagina.route(padrao, lambda rota: presos.append(rota))
        pagina.goto(self.base + caminho, wait_until="commit")
        pagina.wait_for_selector("#themeToggle")
        for _ in range(200):
            if presos:
                break
            pagina.wait_for_timeout(50)
        self.assertTrue(presos, f"{padrao} não foi pedido")

        def soltar():
            pagina.unroute(padrao)
            for rota in presos:
                rota.fallback()
            pagina.wait_for_load_state("load")
        return soltar

    def _esperar_paleta(self, pagina):
        self._esperar(pagina, "!!(window.NEXO && NEXO.colors)")
        pagina.evaluate("window.__mesmaPagina = true")
        # A janela da corrida: a paleta já foi calculada e nenhum gráfico foi desenhado.
        self.assertEqual(pagina.evaluate("[document.readyState, document.querySelectorAll('.js-plotly-plot').length]"), ["loading", 0])

    def _conferir_grafico(self, pagina, grafico, tema):
        pagina.wait_for_selector(f"#{grafico} .xtick text")
        self.assertEqual(pagina.evaluate("[document.documentElement.getAttribute('data-theme'), NEXO.tema]"), [tema, tema])
        self.assertEqual(pagina.evaluate("(id) => getComputedStyle(document.querySelector('#' + id + ' .xtick text')).fill", grafico), _EIXO[tema])
        # Tudo o que deriva da paleta segue junto.
        self.assertEqual(pagina.evaluate("[NEXO.spike.spikecolor, NEXO.abcColor.C]"), pagina.evaluate("[NEXO.colors.spike, NEXO.colors.neutral]"))

    def test_alternar_antes_de_desenhar_os_graficos_nao_deixa_paleta_velha(self):
        """CDN lenta: entre o cálculo da paleta e o desenho há um script que trava o parser. A
        troca nessa janela é instantânea e os gráficos já saem na paleta do tema novo."""
        for email, caminho, grafico in ((CLIENTE, "/cliente/dashboard", "chartCliente"),
                                        (CLIENTE, f"/cliente/analise/{self.analise_id}", "chartAnaliseCli"),
                                        (ADMIN, "/admin/dashboard", "chartPlano")):
            pagina = self._sessao(email)
            for inicio, novo in (("dark", "light"), ("light", "dark")):
                with self.subTest(caminho=caminho, inicio=inicio):
                    pagina.evaluate("(t) => localStorage.setItem('theme', t)", inicio)
                    soltar = self._reter_script(pagina, caminho)
                    self._esperar_paleta(pagina)
                    self.assertEqual(pagina.evaluate("NEXO.tema"), inicio)
                    pagina.click("#themeToggle")
                    self.assertEqual(self._estado(pagina), _CLARO if novo == "light" else _ESCURO)
                    soltar()
                    self._conferir_grafico(pagina, grafico, novo)
                    self.assertTrue(pagina.evaluate("window.__mesmaPagina === true"))
                    self.assertEqual(pagina.evaluate("localStorage.getItem('theme')"), novo)
                    # Já desenhado, o clique seguinte volta a recarregar.
                    with pagina.expect_navigation(wait_until="load"):
                        pagina.click("#themeToggle")
                    self._conferir_grafico(pagina, grafico, inicio)
            pagina.context.close()

    def test_outra_aba_troca_o_tema_antes_de_desenhar_os_graficos(self):
        pagina = self._sessao(CLIENTE)
        outra = pagina.context.new_page()
        outra.route("**/*", self._rota)
        self.ir(outra, "/cliente/tickets")
        soltar = self._reter_script(pagina, "/cliente/dashboard")
        self._esperar_paleta(pagina)
        outra.click("#themeToggle")
        self._esperar(pagina, "document.documentElement.getAttribute('data-theme') === 'light'")
        soltar()
        self._conferir_grafico(pagina, "chartCliente", "light")
        self.assertTrue(pagina.evaluate("window.__mesmaPagina === true"))

    def test_armazenamento_bloqueado_antes_de_desenhar_os_graficos(self):
        """Sem recarga possível: o gráfico nasce na paleta do tema escolhido e, trocando de novo,
        ganha o fundo da paleta em que foi desenhado, seja qual for a fase do clique."""
        # Clique com a paleta já calculada (socket.io preso) e clique antes dela (Plotly preso).
        for padrao, esperar_paleta in (("**/socket.io.min.js", True), ("**/plotly.min.js", False)):
            with self.subTest(preso=padrao):
                pagina = self._sessao(CLIENTE, antes=[_BLOQUEAR_STORAGE])
                soltar = self._reter_script(pagina, "/cliente/dashboard", padrao)
                if esperar_paleta:
                    self._esperar_paleta(pagina)
                pagina.evaluate("window.__mesmaPagina = true")
                pagina.click("#themeToggle")
                self.assertEqual(self._estado(pagina), _CLARO)
                soltar()
                self._conferir_grafico(pagina, "chartCliente", "light")
                fundo = "getComputedStyle(document.getElementById('chartCliente')).backgroundColor"
                self.assertEqual(pagina.evaluate(fundo), "rgba(0, 0, 0, 0)")
                pagina.click("#themeToggle")
                pagina.wait_for_timeout(200)
                self.assertEqual(self._estado(pagina), _ESCURO)
                self.assertEqual(pagina.evaluate("NEXO.tema"), "light")
                self.assertEqual(pagina.evaluate(fundo), "rgb(255, 255, 255)")
                pagina.click("#themeToggle")
                pagina.wait_for_timeout(200)
                self.assertEqual(pagina.evaluate(fundo), "rgba(0, 0, 0, 0)")
                self.assertTrue(pagina.evaluate("window.__mesmaPagina === true"))
                pagina.context.close()

    def test_pagina_sem_grafico_nao_recarrega_durante_o_carregamento(self):
        pagina = self._sessao(CLIENTE)
        soltar = self._reter_script(pagina, "/cliente/historico")
        self._esperar_paleta(pagina)
        pagina.click("#themeToggle")
        self.assertEqual(self._estado(pagina), _CLARO)
        soltar()
        self.assertEqual(self._estado(pagina), _CLARO)
        self.assertTrue(pagina.evaluate("window.__mesmaPagina === true"))
        self.assertEqual(pagina.evaluate("NEXO.tema"), "light")

    def test_foco_do_fechar_menu_passa_3_para_1_sobre_o_veu(self):
        """O botão fica fora da sidebar, sobre o véu; só existe na gaveta, abaixo de 992 px."""
        for largura, altura in ((320, 568), (390, 844), (768, 1024)):
            pagina = self._sessao(CLIENTE, tema="light", largura=largura, altura=altura)
            self.ir(pagina, "/cliente/tickets")
            pagina.add_style_tag(content=_SEM_TRANSICAO)
            pagina.focus("#navToggle")
            pagina.keyboard.press("Enter")
            pagina.wait_for_selector("#navClose", state="visible")
            pagina.wait_for_timeout(100)
            lados = self._foco_por_lado(pagina, "#navClose")
            with self.subTest(largura=largura):
                self.assertEqual(set(lados), {"direita", "esquerda", "cima", "baixo"})
                self.assertGreaterEqual(min(lados.values()), 3, lados)
            pagina.context.close()

    def test_foco_do_link_pular_passa_3_para_1_nos_dois_fundos(self):
        """No desktop o link cai metade sobre a sidebar escura, metade sobre o fundo claro."""
        for largura, altura in ((1366, 768), (992, 768), (390, 844)):
            pagina = self._sessao(CLIENTE, tema="light", largura=largura, altura=altura)
            self.ir(pagina, "/cliente/tickets")
            pagina.add_style_tag(content=_SEM_TRANSICAO)
            pagina.wait_for_timeout(100)
            lados = self._foco_por_lado(pagina, ".skip-link")
            with self.subTest(largura=largura):
                self.assertEqual(set(lados), {"direita", "esquerda", "cima", "baixo"})
                self.assertGreaterEqual(min(lados.values()), 3, lados)
            pagina.context.close()

    def test_foco_de_caixa_e_interruptor_passa_3_para_1_em_qualquer_estado(self):
        """Marcada ou ligada, a borda quase não muda: o indicador precisa ser perceptível mesmo
        assim. O anel é só do teclado; o clique de mouse não o desenha."""
        for tema in ("light", "dark"):
            pagina = self._sessao(ADMIN, tema=tema)
            self.ir(pagina, "/admin/empresas/nova")
            pagina.add_style_tag(content=_SEM_TRANSICAO)
            pagina.evaluate("(h) => document.querySelector('#razao_social').closest('form').insertAdjacentHTML('afterbegin', h)", _CAIXAS)
            for seletor in ("#testeCaixa", "#testeSwitch"):
                for marcada in (False, True):
                    with self.subTest(tema=tema, seletor=seletor, marcada=marcada):
                        pagina.evaluate("([s, m]) => { document.querySelector(s).checked = m; }", [seletor, marcada])
                        lados = self._foco_por_lado(pagina, seletor)
                        self.assertEqual(set(lados), {"direita", "esquerda", "cima", "baixo"})
                        self.assertGreaterEqual(min(lados.values()), 3, lados)
                pagina.click(seletor)
                self.assertEqual(pagina.evaluate("(s) => getComputedStyle(document.querySelector(s)).outlineStyle", seletor), "none")
                pagina.evaluate("(s) => document.querySelector(s).blur()", seletor)
            pagina.context.close()

    def _repouso_e_hover(self, pagina, seletor):
        alvo = pagina.locator(seletor).first
        alvo.scroll_into_view_if_needed()
        pagina.mouse.move(0, 0)
        repouso = alvo.evaluate(_PINTURA)
        alvo.hover()
        return repouso, alvo.evaluate(_PINTURA)

    def test_botoes_do_nexobot_mudam_no_hover(self):
        """A regra clara de repouso não pode anular o hover de nexo.css."""
        pagina = self._sessao(CLIENTE, tema="light")
        self.ir(pagina, "/cliente/tickets")
        pagina.add_style_tag(content=_SEM_TRANSICAO)
        pagina.click("#nexobotLauncher")
        pagina.evaluate("(h) => document.getElementById('nexobotMsgs').insertAdjacentHTML('beforeend', h)", _BOTOES_DO_BOT)
        for seletor in ("#testeNavegacao", "#testeOpcao"):
            with self.subTest(seletor=seletor):
                repouso, hover = self._repouso_e_hover(pagina, seletor)
                self.assertEqual((repouso["cor"], repouso["fundo"]), ("rgb(91, 33, 182)", "rgb(243, 237, 254)"))
                self.assertEqual((hover["cor"], hover["borda"]), ("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)"))
                self.assertEqual(hover["imagem"], "linear-gradient(135deg, rgb(91, 33, 182) 0%, rgb(126, 34, 206) 100%)")
                # Texto branco passa AA nas duas pontas do degradê.
                for parada in ((91, 33, 182), (126, 34, 206)):
                    self.assertGreaterEqual(_razao_pixels((255, 255, 255), parada), 4.5)
        repouso, hover = self._repouso_e_hover(pagina, "#testeUsada")
        self.assertEqual((repouso["cor"], repouso["fundo"]), ("rgb(95, 90, 120)", "rgba(0, 0, 0, 0)"))
        self.assertEqual((hover["cor"], hover["fundo"]), ("rgb(27, 22, 48)", "rgb(240, 236, 249)"))

    def test_item_nao_lido_do_sino_muda_no_hover_e_no_foco(self):
        from models import Notificacao
        self.db.session.add_all([Notificacao(id_usuario=self.cliente_id, texto="Não lida fictícia.", link_destino="/"),
                                 Notificacao(id_usuario=self.cliente_id, texto="Lida fictícia.", link_destino="/", lida=True)])
        self.db.session.commit()
        pagina = self._sessao(CLIENTE, tema="light")
        self.ir(pagina, "/cliente/tickets")
        pagina.add_style_tag(content=_SEM_TRANSICAO)
        pagina.click("#bellBtn")
        pagina.wait_for_selector(".bell-menu.show")
        for seletor, em_repouso in ((".bell-item.is-unread", "rgba(124, 58, 237, 0.05)"), (".bell-item:not(.is-unread)", "rgba(0, 0, 0, 0)")):
            with self.subTest(seletor=seletor):
                repouso, hover = self._repouso_e_hover(pagina, seletor)
                self.assertEqual(repouso["fundo"], em_repouso)
                self.assertEqual(hover["fundo"], "rgb(243, 237, 254)")
                pagina.mouse.move(0, 0)
                pagina.evaluate("(s) => document.querySelector(s).focus()", seletor)
                pagina.keyboard.press("Shift+Tab")
                pagina.keyboard.press("Tab")
                foco = pagina.evaluate(_FOCO_VISIVEL)
                self.assertTrue(foco["teclado"], foco)
                self.assertIn("bell-item", foco["classe"])
                self.assertEqual(foco["fundo"], "rgb(243, 237, 254)")

    def test_foco_de_teclado_pinta_a_borda_da_dropzone(self):
        pagina = self._sessao(CLIENTE, tema="light")
        self.ir(pagina, "/cliente/upload")
        pagina.add_style_tag(content=_SEM_TRANSICAO)
        pagina.keyboard.press("Tab")
        total = pagina.locator(".dropzone input[type=file]").count()
        self.assertGreater(total, 0)
        for indice in range(total):
            with self.subTest(dropzone=indice):
                medida = pagina.evaluate("""(i) => { const e = document.querySelectorAll('.dropzone input[type=file]')[i]; e.focus();
                    return {teclado: e.matches(':focus-visible'), borda: getComputedStyle(e.closest('.dropzone')).borderTopColor}; }""", indice)
                self.assertTrue(medida["teclado"], medida)
                self.assertEqual(medida["borda"], "rgb(109, 40, 217)")

    def test_nome_longo_cede_lugar_ao_titulo_da_topbar_nos_dois_temas(self):
        """O botão de tema toma 54 px da topbar: abaixo de 1100 px um nome longo cortaria o título.
        O nome é que cede, e só quando precisa; com folga, ele continua inteiro."""
        from models import Usuario
        usuario = self.db.session.get(Usuario, self.admin_id)
        pagina = self._sessao(ADMIN)
        for tema in ("dark", "light"):
            pagina.evaluate("(t) => localStorage.setItem('theme', t)", tema)
            for nome, largura, cortado in ((_NOME_47, 992, True), (_NOME_47, 1024, True), (_NOME_47, 1366, False), (_NOME_120, 1366, True),
                                           (_NOME_120, 1920, False), ("Admin fictício", 992, False)):
                with self.subTest(tema=tema, nome=len(nome), largura=largura):
                    usuario.nome = nome
                    self.db.session.commit()
                    pagina.set_viewport_size({"width": largura, "height": 800})
                    self.ir(pagina, "/admin/guia")
                    self.assertEqual(self._estado(pagina)["tema"], tema)
                    medida = pagina.evaluate(_MEDIR_NOME, _MAIOR_TITULO)
                    self.assertFalse(medida["corte"], medida)
                    self.assertFalse(medida["rola"], medida)
                    self.assertEqual(medida["nomeCortado"], cortado, medida)
                    # O nome inteiro continua disponível: a sidebar também o corta.
                    self.assertEqual(medida["nomeTitle"], nome)

    def test_regra_do_nome_nao_muda_nenhum_pixel_do_nome_que_cabe(self):
        """overflow: hidden cortaria as descendentes na fonte de reserva (altura de linha 1,2)."""
        from models import Usuario
        self.db.session.get(Usuario, self.admin_id).nome = "Gyp Jaçy Qwpg"
        self.db.session.commit()
        pagina = self._sessao(ADMIN)
        self.ir(pagina, "/admin/guia")
        pagina.add_style_tag(content=_SEM_TRANSICAO)
        pagina.wait_for_timeout(200)
        caixa = pagina.evaluate("""() => { const r = document.querySelector('.user-meta').getBoundingClientRect();
            return {x: r.left - 4, y: r.top - 4, width: r.width + 8, height: r.height + 8}; }""")
        com = Image.open(io.BytesIO(pagina.screenshot(clip=caixa, animations="disabled"))).convert("RGB")
        pagina.add_style_tag(content=".user-meta { max-width: none; } .user-meta strong { margin: 0; padding: 0; overflow: visible; white-space: normal; }")
        pagina.wait_for_timeout(100)
        sem = Image.open(io.BytesIO(pagina.screenshot(clip=caixa, animations="disabled"))).convert("RGB")
        # O desfoque da topbar varia alguns níveis entre duas capturas iguais; a descendente
        # cortada muda mais de 180 níveis (medido na fonte de reserva).
        maior = max(max(abs(a - b) for a, b in zip(x, y)) for x, y in zip(com.getdata(), sem.getdata()))
        self.assertLessEqual(maior, 8)

    def test_sincroniza_entre_abas_pelo_evento_storage(self):
        pagina = self._sessao(CLIENTE)
        self.ir(pagina, "/cliente/historico")
        outra = pagina.context.new_page()
        outra.on("pageerror", lambda erro: self.erros_js.append(f"outra aba: {erro}"))
        outra.route("**/*", self._rota)
        self.ir(outra, "/cliente/tickets")
        outra.evaluate("window.__mesmaPagina = true")
        com_grafico = pagina.context.new_page()
        com_grafico.route("**/*", self._rota)
        self.ir(com_grafico, "/cliente/dashboard")
        com_grafico.wait_for_selector(".js-plotly-plot .xtick text")
        com_grafico.evaluate("window.__mesmaPagina = true")
        pagina.click("#themeToggle")
        # Sem gráfico: troca na hora, sem recarregar. Com gráfico: recarrega na paleta nova.
        self._esperar(outra, "document.documentElement.getAttribute('data-theme') === 'light'")
        self.assertEqual(self._estado(outra), _CLARO)
        self.assertTrue(outra.evaluate("window.__mesmaPagina === true"))
        self._esperar(com_grafico, "document.documentElement.getAttribute('data-theme') === 'light' && window.__mesmaPagina !== true")
        com_grafico.wait_for_load_state("load")
        self.assertEqual(self._estado(com_grafico), _CLARO)
        # Valor apagado em outra aba: volta ao padrão.
        pagina.evaluate("localStorage.removeItem('theme')")
        self._esperar(outra, "document.documentElement.getAttribute('data-theme') === 'dark'")
        self.assertEqual(self._estado(outra), _ESCURO)


if __name__ == "__main__":
    unittest.main()
