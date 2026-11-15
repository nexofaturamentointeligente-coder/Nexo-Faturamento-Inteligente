"""Plano de Ação (12 meses) no navegador (Etapa 2, 14/11/2026): o botão, o modal, o checklist salvo no
navegador por empresa e por Raio-X, a barra de progresso, a impressão só do plano, os links para as listas
completas, o celular e o contraste nos dois temas (static/js/plano-acao.js e a folha nexo.css).

Ignorado por padrão, como os demais testes de navegador: precisa de NEXO_TEST_BROWSER=1, do Playwright e das
cópias íntegras da CDN (ver tests/test_overflow_navegador.py). O cliente passa para o PRATA e ganha o Raio-X de
oito meses de tests/test_raio_x_navegador.py (oito produtos abaixo do preço de tabela e do custo e um item de
custo a confirmar).
"""

import unittest

from tests.test_overflow_navegador import NavegadorCase, _BROWSER
# Pelo módulo, e não pelo nome: a classe importada no módulo seria coletada (e rodada) de novo aqui.
import tests.test_raio_x_navegador as navegador_raio_x
from tests.test_tema_navegador import _CONTRASTE

PAGINA, AUDITORIA = navegador_raio_x.PAGINA, navegador_raio_x.AUDITORIA
_BASE = navegador_raio_x.RaioXNavegadorTests


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class PlanoAcaoNavegadorTests(NavegadorCase):
    def setUp(self):
        super().setUp()
        self._prata()
        self._raio_x_com_avisos()

    _prata = _BASE._prata
    _raio_x_com_avisos = _BASE._raio_x_com_avisos
    _pagina = _BASE._pagina

    def _esperar(self, pagina, expressao, *argumentos):
        """Repete a conferência aqui: o wait_for_function do Playwright usa eval, que a CSP do portal barra."""
        for _ in range(200):
            if pagina.evaluate(expressao, *argumentos):
                return
            pagina.wait_for_timeout(50)
        self.fail(f"não aconteceu: {expressao}")

    def _abrir_plano(self, pagina):
        pagina.get_by_role("button", name="Ver Plano de Ação (12 Meses)").click()
        self._esperar(pagina, "() => { const m = document.getElementById('planoAcao'); "
                              "return m.classList.contains('show') && getComputedStyle(m).opacity === '1'; }")
        return pagina.locator("#planoAcao")

    def test_botao_abre_o_plano_fora_do_layout_e_fecha(self):
        for caminho in (PAGINA, AUDITORIA):
            with self.subTest(caminho=caminho):
                pagina = self._pagina(caminho)
                modal = self._abrir_plano(pagina)
                self.assertTrue(pagina.evaluate("() => document.getElementById('planoAcao').parentElement === document.body"))
                self.assertTrue(pagina.evaluate("() => document.body.classList.contains('plano-acao-aberto')"))
                self.assertTrue(modal.get_by_role("heading", name="Plano de Ação (12 meses)").is_visible())
                # Os produtos reais: o de maior perda abaixo do custo e o item de custo distorcido (o texto do HTML:
                # os rótulos dos cards saem em maiúsculas pela folha).
                texto = modal.text_content()
                self.assertIn("Produto de teste 8", texto)
                self.assertIn("Item Generico", texto)
                self.assertIn("Custo da Inação (12 meses)", texto)
                self.assertIn("nenhuma venda saiu sem custo informado no período analisado", texto)
                modal.get_by_role("button", name="Fechar o Plano de Ação").click()
                self._esperar(pagina, "() => !document.getElementById('planoAcao').classList.contains('show')")
                self._esperar(pagina, "() => !document.body.classList.contains('plano-acao-aberto')")
                pagina.context.close()

    def test_checklist_salvo_no_navegador_por_empresa_e_por_raio_x(self):
        pagina = self._pagina(AUDITORIA)
        chave = pagina.evaluate("() => { const m = document.getElementById('planoAcao'); "
                                "return m.dataset.planoChave + '.' + m.dataset.planoDiagnostico; }")
        self.assertTrue(chave.startswith(f"nexo.planoAcao.{self.empresa_id}."))
        # O checklist de um Raio-X anterior da mesma empresa sai ao abrir a página; o de outra empresa fica.
        pagina.evaluate("(c) => { localStorage.setItem(c.split('.').slice(0, 3).join('.') + '.2020-01-01T00:00:00+00:00', '{\"1\":true}');"
                        " localStorage.setItem('nexo.planoAcao.999999.x', '{}'); }", chave)
        pagina.reload(wait_until="load")
        modal = self._abrir_plano(pagina)
        self.assertEqual(modal.locator("[data-plano-progresso-texto]").inner_text(), "0 de 3 etapas concluídas")
        modal.locator("#planoEtapa1").check()
        self.assertEqual(modal.locator("[data-plano-progresso-texto]").inner_text(), "1 de 3 etapas concluídas")
        self.assertEqual(modal.locator('[data-plano-status="1"]').inner_text(), "Concluída")
        self.assertEqual(modal.locator('[data-plano-status="2"]').inner_text(), "Pendente")
        self.assertEqual(modal.locator("[data-plano-barra]").get_attribute("aria-valuenow"), "1")
        largura = pagina.evaluate("() => { const b = document.querySelector('[data-plano-barra]'); "
                                  "return b.querySelector('span').getBoundingClientRect().width / b.getBoundingClientRect().width; }")
        self.assertAlmostEqual(largura, 1 / 3, delta=0.02)
        self.assertEqual(pagina.evaluate("(c) => localStorage.getItem(c)", chave), '{"1":true}')
        chaves = pagina.evaluate("() => Object.keys(localStorage).filter(k => k.startsWith('nexo.planoAcao.')).sort()")
        self.assertEqual(chaves, sorted([chave, "nexo.planoAcao.999999.x"]))
        # Depois de recarregar, a marcação continua.
        pagina.reload(wait_until="load")
        modal = self._abrir_plano(pagina)
        self.assertTrue(modal.locator("#planoEtapa1").is_checked())
        self.assertFalse(modal.locator("#planoEtapa2").is_checked())
        self.assertEqual(modal.locator("[data-plano-progresso-texto]").inner_text(), "1 de 3 etapas concluídas")
        modal.locator("#planoEtapa1").uncheck()
        self.assertEqual(pagina.evaluate("(c) => localStorage.getItem(c)", chave), '{"1":false}')

    def test_sem_localstorage_o_checklist_funciona_sem_salvar(self):
        pagina = self.abrir(1366, 900)
        pagina.add_init_script("Object.defineProperty(window, 'localStorage', {get() { throw new Error('bloqueado'); }});")
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, AUDITORIA)
        modal = self._abrir_plano(pagina)
        modal.locator("#planoEtapa2").check()
        self.assertEqual(modal.locator("[data-plano-progresso-texto]").inner_text(), "1 de 3 etapas concluídas")

    def test_impressao_mostra_so_o_plano_inteiro_em_papel_claro(self):
        for tema in ("dark", "light"):
            with self.subTest(tema=tema):
                pagina = self._pagina(AUDITORIA)
                if tema == "light":
                    pagina.locator("#themeToggle").click()
                    self._esperar(pagina, "() => document.documentElement.getAttribute('data-theme') === 'light'")
                self._abrir_plano(pagina)
                pagina.emulate_media(media="print")
                medida = pagina.evaluate("""() => {
                    const m = document.getElementById('planoAcao'), c = m.querySelector('.modal-content');
                    const corpo = m.querySelector('.modal-body'), cs = getComputedStyle(m);
                    const visiveis = [...document.body.children].filter(e => e !== m && getComputedStyle(e).display !== 'none');
                    return {visiveis: visiveis.map(e => e.tagName + '.' + e.className), posicao: cs.position, display: cs.display,
                            fundo: getComputedStyle(c).backgroundColor, cor: getComputedStyle(c).color,
                            rolagem: getComputedStyle(corpo).overflowY, cortado: c.scrollHeight > c.clientHeight + 1,
                            botoes: getComputedStyle(m.querySelector('.plano-acao-acoes')).display,
                            titulo: getComputedStyle(m.querySelector('.modal-title')).color};
                }""")
                self.assertEqual(medida["visiveis"], [])
                self.assertEqual((medida["posicao"], medida["display"]), ("static", "block"))
                self.assertEqual(medida["fundo"], "rgb(255, 255, 255)")
                self.assertEqual(medida["rolagem"], "visible")
                self.assertFalse(medida["cortado"], "o conteúdo do plano ficou cortado na impressão")
                self.assertEqual(medida["botoes"], "none")
                for medida_contraste in pagina.evaluate(_CONTRASTE, ["#planoAcao .modal-title", "#planoAcao .plano-acao-sub",
                                                                    "#planoAcao .plano-acao-texto", "#planoAcao .plano-acao-dado",
                                                                    "#planoAcao .plano-acao-fazer", "#planoAcao .plano-acao-destaque",
                                                                    "#planoAcao .plano-acao-rodape"]):
                    with self.subTest(tema=tema, seletor=medida_contraste["seletor"]):
                        self.assertNotIn("ausente", medida_contraste)
                        self.assertGreaterEqual(medida_contraste["razao"], 4.5, medida_contraste)
                pagina.emulate_media(media="screen")
                pagina.context.close()

    def test_contraste_do_plano_nos_dois_temas(self):
        seletores = ["#planoAcao .modal-title", "#planoAcao .plano-acao-sub", "#planoAcao .plano-acao-rotulo",
                     "#planoAcao .plano-acao-valor", "#planoAcao .plano-acao-texto", "#planoAcao .plano-acao-cenarios dt",
                     "#planoAcao .plano-acao-cenarios dd span", "#planoAcao .plano-acao-destaque",
                     "#planoAcao .plano-acao-orientacao", "#planoAcao .plano-acao-produto", "#planoAcao .plano-acao-dado",
                     "#planoAcao .plano-acao-fazer", "#planoAcao .plano-acao-progresso-texto",
                     "#planoAcao .plano-acao-checklist-etapa", "#planoAcao .plano-acao-rodape", "#planoAcao .plano-acao-status"]
        for tema in ("dark", "light"):
            with self.subTest(tema=tema):
                pagina = self._pagina(AUDITORIA)
                if tema == "light":
                    pagina.locator("#themeToggle").click()
                    self._esperar(pagina, "() => document.documentElement.getAttribute('data-theme') === 'light'")
                for medida in pagina.evaluate(_CONTRASTE, [".kpi-foot-janela"]):
                    self.assertGreaterEqual(medida["razao"], medida["minimo"], medida)
                self._abrir_plano(pagina)
                for medida in pagina.evaluate(_CONTRASTE, seletores):
                    with self.subTest(tema=tema, seletor=medida["seletor"]):
                        self.assertNotIn("ausente", medida, "amostra não encontrada no plano")
                        self.assertGreaterEqual(medida["razao"], medida["minimo"], medida)
                pagina.context.close()

    def test_link_da_lista_completa_fecha_o_plano_e_leva_a_ela(self):
        pagina = self._pagina(AUDITORIA)
        modal = self._abrir_plano(pagina)
        modal.get_by_role("link", name="Ver todos os alertas de cadastro").click()
        self._esperar(pagina, "() => !document.getElementById('planoAcao').classList.contains('show')")
        self._esperar(pagina, "() => document.getElementById('alertasCadastro').open")
        self._esperar(pagina, "() => { const r = document.getElementById('alertasCadastro').getBoundingClientRect(); "
                              "return r.top >= 0 && r.top < innerHeight; }")
        # O foco vai ao resumo dos alertas (o <summary>, que é o que recebe foco no <details>).
        self.assertTrue(pagina.evaluate("() => document.activeElement.matches('#alertasCadastro > summary')"))
        # Para a lista de outra página, o link navega até ela.
        modal = self._abrir_plano(pagina)
        with pagina.expect_navigation(wait_until="load"):
            modal.get_by_role("link", name="Ver todos os produtos vendidos fora do preço normal").click()
        self.assertTrue(pagina.url.endswith(PAGINA + "#scannerTitulo"))

    def test_no_celular_o_plano_ocupa_a_tela_sem_rolagem_lateral(self):
        pagina = self._pagina(AUDITORIA, 390, 844)
        botao = pagina.get_by_role("button", name="Ver Plano de Ação (12 Meses)")
        caixa = botao.bounding_box()
        self.assertLessEqual(caixa["x"] + caixa["width"], 390)
        self._abrir_plano(pagina)
        medida = pagina.evaluate("""() => { const m = document.getElementById('planoAcao');
            const d = m.querySelector('.modal-dialog').getBoundingClientRect(), corpo = m.querySelector('.modal-body');
            return {largura: d.width, esquerda: d.left, rolaDeLado: corpo.scrollWidth > corpo.clientWidth + 1,
                    pagina: document.documentElement.scrollWidth > innerWidth + 1}; }""")
        self.assertEqual((round(medida["largura"]), round(medida["esquerda"])), (390, 0))
        self.assertFalse(medida["rolaDeLado"], "o conteúdo do plano rola para o lado no celular")
        self.assertFalse(medida["pagina"])

    def test_caixas_do_checklist_e_links_na_paleta_nos_dois_temas(self):
        # O modal fica fora do .app-shell: a caixa desmarcada precisa de borda de 3:1 sobre o fundo do modal, a marcada
        # no roxo da marca, e os links no lilás (escuro) ou no roxo (claro), nunca no azul do Bootstrap.
        borda = """(seletor) => {
            const cor = s => { const m = s.match(/rgba?\\(([^)]+)\\)/); if (!m) return {r: 0, g: 0, b: 0, a: 0};
                const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(parseFloat); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
            const sobre = (t, b) => { const a = t.a + b.a * (1 - t.a); if (!a) return {r: 0, g: 0, b: 0, a: 0};
                return {r: (t.r * t.a + b.r * b.a * (1 - t.a)) / a, g: (t.g * t.a + b.g * b.a * (1 - t.a)) / a, b: (t.b * t.a + b.b * b.a * (1 - t.a)) / a, a}; };
            const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
            const lum = c => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
            const el = document.querySelector(seletor);
            let f = {r: 0, g: 0, b: 0, a: 0};
            for (let e = el.parentElement; e; e = e.parentElement) { f = sobre(f, cor(getComputedStyle(e).backgroundColor)); if (f.a >= 1) break; }
            if (f.a < 1) f = sobre(f, {r: 255, g: 255, b: 255, a: 1});
            const b = sobre(cor(getComputedStyle(el).borderTopColor), f);
            const x = lum(b), y = lum(f);
            return {razao: (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05), fundoMarcada: getComputedStyle(el).backgroundColor}; }"""
        for tema, lilas in (("dark", "rgb(196, 160, 251)"), ("light", "rgb(109, 40, 217)")):
            with self.subTest(tema=tema):
                pagina = self._pagina(AUDITORIA)
                if tema == "light":
                    pagina.locator("#themeToggle").click()
                    self._esperar(pagina, "() => document.documentElement.getAttribute('data-theme') === 'light'")
                modal = self._abrir_plano(pagina)
                medida = pagina.evaluate(borda, "#planoEtapa1")
                self.assertGreaterEqual(medida["razao"], 3, medida)
                modal.locator("#planoEtapa1").check()
                marcada = pagina.evaluate("() => getComputedStyle(document.getElementById('planoEtapa1')).backgroundColor")
                self.assertIn(marcada, ("rgb(124, 58, 237)", "rgb(91, 33, 182)"))
                cor_link = pagina.evaluate("() => getComputedStyle(document.querySelector('#planoAcao .plano-acao-link')).color")
                self.assertEqual(cor_link, lilas)
                pagina.context.close()

    def test_nexobot_sai_de_cena_com_o_plano_aberto(self):
        pagina = self._pagina(AUDITORIA)
        visivel = "() => getComputedStyle(document.querySelector('.nexobot')).visibility"
        self.assertEqual(pagina.evaluate(visivel), "visible")
        modal = self._abrir_plano(pagina)
        self.assertEqual(pagina.evaluate(visivel), "hidden")
        modal.get_by_role("button", name="Fechar o Plano de Ação").click()
        self._esperar(pagina, "() => getComputedStyle(document.querySelector('.nexobot')).visibility === 'visible'")

    def test_checklist_em_duas_abas_nao_apaga_a_marcacao_da_outra(self):
        aba_a = self._pagina(PAGINA)
        aba_b = aba_a.context.new_page()
        self.ir(aba_b, AUDITORIA)
        modal_a, modal_b = self._abrir_plano(aba_a), self._abrir_plano(aba_b)
        modal_a.locator("#planoEtapa1").check()
        # A outra aba acompanha pelo evento 'storage'.
        self._esperar(aba_b, "() => document.getElementById('planoEtapa1').checked")
        modal_b.locator("#planoEtapa2").check()
        self._esperar(aba_a, "() => document.getElementById('planoEtapa2').checked")
        chave = aba_a.evaluate("() => { const m = document.getElementById('planoAcao'); "
                               "return m.dataset.planoChave + '.' + m.dataset.planoDiagnostico; }")
        self.assertEqual(aba_a.evaluate("(c) => JSON.parse(localStorage.getItem(c))", chave), {"1": True, "2": True})
        self.assertEqual(aba_a.locator("[data-plano-progresso-texto]").inner_text(), "2 de 3 etapas concluídas")

    def test_link_dos_alertas_vindo_do_raio_x_abre_o_card(self):
        pagina = self._pagina(PAGINA)
        modal = self._abrir_plano(pagina)
        with pagina.expect_navigation(wait_until="load"):
            modal.get_by_role("link", name="Ver todos os alertas de cadastro").click()
        self.assertTrue(pagina.url.endswith(AUDITORIA + "#alertasCadastroResumo"))
        self._esperar(pagina, "() => document.getElementById('alertasCadastro').open")
        self.assertTrue(pagina.evaluate("() => document.activeElement.matches('#alertasCadastro > summary')"))
        self.assertTrue(pagina.locator("#tabelaCustoAConfirmar").is_visible())

    def test_sem_violacao_de_csp_nem_erro_no_console(self):
        pagina = self.abrir(1366, 900)
        erros = []
        pagina.on("console", lambda mensagem: erros.append(mensagem.text) if mensagem.type == "error" else None)
        pagina.on("pageerror", lambda erro: erros.append(str(erro)))
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, AUDITORIA)
        modal = self._abrir_plano(pagina)
        modal.locator("#planoEtapa3").check()
        modal.get_by_role("button", name="Fechar o Plano de Ação").click()
        self.assertEqual(erros, [])


if __name__ == "__main__":
    unittest.main()
