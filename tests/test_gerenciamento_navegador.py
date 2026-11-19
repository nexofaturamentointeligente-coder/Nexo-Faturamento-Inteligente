"""
Navegador real (Chromium/Playwright) para a etapa de 28/10/2026:

  * Gerenciamento de análises: listagem sem lixeira, botão Editar, cartões de
    empresa com busca e teclado, seleção, análises só da empresa, modal de
    confirmação (cancelar não exclui; confirmar exclui só a escolhida), volta.
  * Topbar global com desfoque gradual: duas camadas decorativas (forte no topo
    com máscara em degradê, suave na base), faixa de ponta a ponta da área
    principal, título legível, sino e menu acima do cabeçalho, sem interceptar
    cliques; em todas as páginas autenticadas e em 320–1920 px.
  * Bordas: divisórias com recuo nas pontas, realce de linha arredondado, coluna
    de ações compacta; sem células encobertas nem rolagem desnecessária a 1366 px.

Mesma infraestrutura de tests/test_overflow_navegador.py (servidor local, CDN
local íntegra, dados fictícios); habilitado por NEXO_TEST_BROWSER=1.
"""

import unittest
from datetime import date

from tests.test_overflow_navegador import LARGURAS, NavegadorCase

PAGINAS_TOPBAR = {
    "admin-test@example.invalid": ["/admin/dashboard", "/admin/empresas", "/admin/contratos", "/admin/analises",
                                   "/admin/analises/gerenciar", "/admin/lixeira", "/admin/tickets"],
    "cliente-test@example.invalid": ["/cliente/dashboard", "/cliente/historico", "/cliente/upload", "/cliente/guia",
                                     "/cliente/tickets"],
}

_MEDIR_TOPBAR = """() => {
    const topbar = document.querySelector('.topbar');
    const main = document.querySelector('.main-area');
    if (!topbar || !main) return null;
    const r = topbar.getBoundingClientRect(), m = main.getBoundingClientRect();
    const antes = getComputedStyle(topbar, '::before'), depois = getComputedStyle(topbar, '::after');
    const titulo = topbar.querySelector('.topbar-title');
    const tb = titulo.getBoundingClientRect();
    const noTitulo = document.elementFromPoint(tb.left + 12, tb.top + tb.height / 2);
    const vw = window.innerWidth;
    return {
        x0: r.left, x1: r.right, y0: r.top, y1: r.bottom, h: r.height,
        main_x0: m.left, main_x1: m.right, vw,
        sticky: getComputedStyle(topbar).position,
        fundo_proprio: getComputedStyle(topbar).backgroundColor,
        borda_propria: getComputedStyle(topbar).borderTopColor,
        anel: depois.boxShadow, cobertura: {antes: antes.inset || antes.top, depois: depois.inset || depois.top},
        forte: {filtro: antes.backdropFilter || antes.webkitBackdropFilter, mascara: antes.maskImage || antes.webkitMaskImage,
                eventos: antes.pointerEvents, z: antes.zIndex, conteudo: antes.content},
        suave: {filtro: depois.backdropFilter || depois.webkitBackdropFilter, mascara: depois.maskImage || depois.webkitMaskImage,
                eventos: depois.pointerEvents, z: depois.zIndex, conteudo: depois.content},
        titulo_visivel: !!noTitulo && !!noTitulo.closest('.topbar-title'),
        titulo_texto: titulo.textContent.replace(/\\s+/g, ' ').trim(),
        suporte: CSS.supports('backdrop-filter', 'blur(1px)') || CSS.supports('-webkit-backdrop-filter', 'blur(1px)'),
    };
}"""


class GerenciamentoNavegadorTests(NavegadorCase):
    def _empresa_mascarada(self):
        """Empresa fictícia cujo CNPJ foi gravado com máscara (o cadastro permite)."""
        from models import Empresa
        base = self.db.session.get(Empresa, self.empresa_id)
        self.db.session.add(Empresa(
            id_segmento=base.id_segmento, id_plano_atual=base.id_plano_atual,
            cnpj="11.222.333/0001-44", razao_social="Empresa Mascarada Fictícia Ltda",
            nome_fantasia="Mascarada Teste", email_contato="mascarada@example.invalid",
            data_contratacao=date(2026, 1, 1)))
        self.db.session.commit()

    # ------------------------------------------------------- gerenciamento
    def test_fluxo_editar_cartoes_busca_selecao_e_exclusao(self):
        self._empresa_mascarada()
        pagina = self.abrir(1366, 768)
        self.entrar(pagina, "admin-test@example.invalid")
        self.ir(pagina, "/admin/analises")
        # 5-6: nenhuma lixeira nas linhas; botão Editar presente.
        self.assertEqual(pagina.locator("#tabelaAnalises .btn-excluir-analise, #tabelaAnalises .bi-trash").count(), 0)
        self.assertTrue(pagina.locator("#btnGerenciar").is_visible())
        self.assertGreater(pagina.locator("#tabelaAnalises tbody tr[data-empresa]").count(), 0)
        # 7-8: Editar abre a seleção de empresas em cartões.
        pagina.click("#btnGerenciar")
        pagina.wait_for_url("**/admin/analises/gerenciar")
        cartoes = pagina.locator(".empresa-card")
        self.assertGreaterEqual(cartoes.count(), 2)
        self.assertEqual(pagina.locator(".btn-excluir-analise").count(), 0)
        # 9: busca por nome (sem acento também) e "Limpar busca".
        nome = pagina.locator(f'.empresa-card[href$="/{self.empresa_id}"] .ec-nome').text_content().strip()
        pagina.fill("#buscaGerenciar", nome.split()[0].lower())
        pagina.wait_for_timeout(200)
        visiveis = pagina.locator(".empresa-card:visible")
        self.assertGreaterEqual(visiveis.count(), 1)
        self.assertTrue(pagina.locator(f'.empresa-card[href$="/{self.empresa_id}"]').is_visible())
        pagina.fill("#buscaGerenciar", "zzzz-nao-existe")
        pagina.wait_for_timeout(200)
        self.assertEqual(pagina.locator(".empresa-card:visible").count(), 0)
        self.assertTrue(pagina.locator("#semResultados").is_visible())
        pagina.click("#limparBuscaGerenciar")
        self.assertEqual(pagina.locator("#buscaGerenciar").input_value(), "")
        self.assertEqual(pagina.locator(".empresa-card:visible").count(), cartoes.count())
        # 9a: sem acento encontra o nome acentuado ("paraiba" → "Vale do Paraíba"), e só ele.
        pagina.fill("#buscaGerenciar", "paraiba")
        pagina.wait_for_timeout(200)
        self.assertTrue(pagina.locator(f'.empresa-card[href$="/{self.empresa_longa_id}"]').is_visible())
        self.assertEqual(pagina.locator(".empresa-card:visible").count(), 1)
        pagina.fill("#buscaGerenciar", "")
        pagina.wait_for_timeout(200)
        # 9b: grade acessível (lista real, cartão = link sem role) e busca por CNPJ
        #     digitado sem máscara numa empresa gravada com máscara.
        self.assertEqual(pagina.locator("ul#gradeEmpresas > li > a.empresa-card").count(), cartoes.count())
        self.assertEqual(pagina.locator(".empresa-card[role]").count(), 0)
        self.assertTrue(pagina.get_by_role("link", name=nome).first.is_visible())
        pagina.fill("#buscaGerenciar", "11222333000144")
        pagina.wait_for_timeout(200)
        self.assertEqual(pagina.locator(".empresa-card:visible").count(), 1)
        self.assertTrue(pagina.locator('.empresa-card[data-cnpj="11222333000144"]').is_visible())
        pagina.fill("#buscaGerenciar", "11.222.333/0001")
        pagina.wait_for_timeout(200)
        self.assertEqual(pagina.locator(".empresa-card:visible").count(), 1)
        pagina.fill("#buscaGerenciar", "")
        pagina.wait_for_timeout(200)
        self.assertEqual(pagina.locator(".empresa-card:visible").count(), cartoes.count())
        # 10: seleção pelo teclado (Tab até o cartão e Enter) e pelo clique.
        alvo = pagina.locator(f'.empresa-card[href$="/{self.empresa_id}"]')
        alvo.focus()
        pagina.keyboard.press("Enter")
        pagina.wait_for_url(f"**/admin/analises/gerenciar/{self.empresa_id}")
        self.assertTrue(pagina.locator("#empresaSelecionada").is_visible())
        self.assertTrue(pagina.get_by_role("group", name="Empresa selecionada").is_visible())
        # 11: só as análises desta empresa (todas as linhas pertencem a ela; a da outra empresa não aparece).
        linhas = pagina.locator("#tabelaAnalisesEmpresa tbody tr[data-analise]")
        total = linhas.count()
        self.assertGreaterEqual(total, 2)
        self.assertEqual(pagina.locator(f'#tabelaAnalisesEmpresa tr[data-analise="{self.outra_analise_id}"]').count(), 0)
        # 13-15: lixeira só aqui; modal com empresa, período, tipo e identificador; cancelar não exclui.
        self.assertEqual(pagina.locator(".btn-excluir-analise").count(), total)
        primeira = linhas.first
        id_alvo = primeira.get_attribute("data-analise")
        primeira.locator(".btn-excluir-analise").click()
        pagina.wait_for_selector("#confirmExcluirAnalise.show")
        modal = pagina.locator("#confirmExcluirAnalise")
        self.assertIn("Excluir análise?", modal.locator(".modal-title").text_content())
        self.assertTrue(modal.locator("#excluirEmpresa").text_content().strip())
        self.assertRegex(modal.locator("#excluirPeriodo").text_content(), r"\d{2}/\d{2}/\d{4} – \d{2}/\d{2}/\d{4}")
        self.assertIn(modal.locator("#excluirTipo").text_content().strip(), ("Mensal", "Quinzenal"))
        self.assertEqual(modal.locator("#excluirId").text_content().strip(), id_alvo)
        self.assertIn("definitiva", modal.text_content())
        self.assertTrue(modal.locator("#formExcluirAnalise").get_attribute("action").endswith(f"/excluir/{id_alvo}"))
        modal.locator("button", has_text="Cancelar").click()
        pagina.wait_for_selector("#confirmExcluirAnalise", state="hidden")
        self.assertEqual(linhas.count(), total)
        # 16-18: confirmar exclui só a escolhida; as outras e a empresa permanecem.
        primeira.locator(".btn-excluir-analise").click()
        pagina.wait_for_selector("#confirmExcluirAnalise.show")
        # A URL já é a da empresa: espera-se a navegação real do POST → redirect → GET,
        # e a flash de sucesso da nova página, antes de contar as linhas.
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("#btnConfirmarExclusao")
        pagina.wait_for_selector(".flash-stack .alert", state="visible")
        self.assertIn("excluída", pagina.locator(".flash-stack .alert").first.text_content())
        self.assertEqual(pagina.locator("#tabelaAnalisesEmpresa tbody tr[data-analise]").count(), total - 1)
        self.assertEqual(pagina.locator(f'#tabelaAnalisesEmpresa tr[data-analise="{id_alvo}"]').count(), 0)
        self.assertTrue(pagina.locator("#empresaSelecionada").is_visible())
        # 12: volta à seleção de empresas.
        pagina.click("text=Voltar à seleção de empresas")
        pagina.wait_for_url("**/admin/analises/gerenciar")
        self.assertGreaterEqual(pagina.locator(".empresa-card").count(), 2)
        self.assert_layout(pagina, 1366, "/admin/analises/gerenciar @ 1366px")

    def test_gerenciamento_em_todas_as_larguras(self):
        for largura, altura in LARGURAS:
            pagina = self.abrir(largura, altura)
            self.entrar(pagina, "admin-test@example.invalid")
            for caminho in ("/admin/analises", "/admin/analises/gerenciar", f"/admin/analises/gerenciar/{self.empresa_id}"):
                self.ir(pagina, caminho)
                self.assert_layout(pagina, largura, f"{caminho} @ {largura}px")
                # Botões de ação legíveis (texto numa linha só, nunca letra por letra).
                for botao in pagina.locator(".btn-abrir").all()[:3]:
                    caixa = botao.bounding_box()
                    self.assertIsNotNone(caixa)
                    self.assertLessEqual(caixa["height"], 44, f"{caminho} @ {largura}px: botão de ação quebrado ({caixa})")
            # A tabela de análises cabe sem rolagem horizontal a partir do notebook.
            if largura >= 1366:
                self.ir(pagina, "/admin/analises")
                rola = pagina.evaluate("() => { const c = document.querySelector('.table-responsive-x'); return c.scrollWidth > c.clientWidth + 1; }")
                self.assertFalse(rola, f"/admin/analises @ {largura}px: a listagem ainda rola na horizontal")
            pagina.context.close()

    # ----------------------------------------------------------------- topbar
    def test_topbar_com_blur_gradual_em_todas_as_paginas(self):
        for largura, altura in ((1920, 1080), (1366, 768), (768, 1024), (390, 844), (320, 568)):
            for email, caminhos in PAGINAS_TOPBAR.items():
                pagina = self.abrir(largura, altura)
                self.entrar(pagina, email)
                for caminho in caminhos:
                    self.ir(pagina, caminho)
                    # Com a página rolada, conteúdo passa atrás do cabeçalho.
                    pagina.evaluate("window.scrollTo(0, Math.min(600, document.documentElement.scrollHeight))")
                    pagina.wait_for_timeout(250)
                    m = pagina.evaluate(_MEDIR_TOPBAR)
                    ctx = f"{caminho} @ {largura}px"
                    self.assertIsNotNone(m, ctx)
                    self.assertTrue(m["suporte"], "Chromium deve suportar backdrop-filter")
                    self.assertEqual(m["sticky"], "sticky", ctx)
                    # Duas camadas decorativas: forte com máscara em degradê, suave inteira; nenhuma intercepta cliques.
                    self.assertIn("blur(26px)", m["forte"]["filtro"], ctx)
                    self.assertIn("linear-gradient", m["forte"]["mascara"], ctx)
                    self.assertIn("blur(8px)", m["suave"]["filtro"], ctx)
                    self.assertEqual(m["suave"]["mascara"], "none", ctx)
                    self.assertEqual(m["forte"]["eventos"], "none", ctx)
                    self.assertEqual(m["suave"]["eventos"], "none", ctx)
                    self.assertEqual(m["fundo_proprio"], "rgba(0, 0, 0, 0)", f"{ctx}: o desfoque fica nas camadas, não num fundo opaco")
                    # Anel da borda e brilho pintados pela camada de cima, que cobre também a borda (inset -1px).
                    self.assertEqual(m["borda_propria"], "rgba(0, 0, 0, 0)", f"{ctx}: a borda do .topbar deve ser transparente ({m['borda_propria']})")
                    self.assertIn("inset", m["anel"], f"{ctx}: o ::after deve pintar o anel e o brilho ({m['anel']})")
                    self.assertEqual(m["cobertura"]["antes"], "-1px", ctx)
                    self.assertEqual(m["cobertura"]["depois"], "-1px", ctx)
                    # Faixa de ponta a ponta da área principal (desktop) ou da tela (celular).
                    if largura >= 992:
                        self.assertLessEqual(abs(m["x0"] - m["main_x0"]), 1, f"{ctx}: começa no limite da área de conteúdo ({m})")
                        self.assertLessEqual(abs(m["main_x1"] - 14 - m["x1"]), 1, f"{ctx}: vai até a borda direita ({m})")
                    else:
                        self.assertLessEqual(m["x0"], 12, ctx)
                        self.assertGreaterEqual(m["x1"], m["vw"] - 12, ctx)
                    self.assertGreaterEqual(m["y0"], 0, ctx)
                    self.assertTrue(m["titulo_visivel"], f"{ctx}: título encoberto ({m['titulo_texto']})")
                    self.assertTrue(m["titulo_texto"], ctx)
                pagina.context.close()

    def test_sino_abre_acima_da_topbar_sem_cortes(self):
        for largura, altura in ((1366, 768), (390, 844)):
            pagina = self.abrir(largura, altura)
            self.entrar(pagina, "admin-test@example.invalid")
            self.ir(pagina, "/admin/dashboard")
            pagina.evaluate("window.scrollTo(0, 500)")
            pagina.wait_for_timeout(200)
            pagina.click("#bellBtn")
            pagina.wait_for_selector(".bell-menu.show")
            m = pagina.evaluate("""() => {
                const menu = document.querySelector('.bell-menu.show'); const r = menu.getBoundingClientRect();
                const alvo = document.elementFromPoint(r.left + r.width / 2, r.top + Math.min(40, r.height / 2));
                return {x0: r.left, x1: r.right, y0: r.top, y1: r.bottom, vw: innerWidth, vh: innerHeight,
                        sobre_menu: !!alvo && !!alvo.closest('.bell-menu'), opaco: getComputedStyle(menu).backgroundColor};
            }""")
            ctx = f"sino @ {largura}px"
            self.assertGreaterEqual(m["x0"], -1, f"{ctx}: menu cortado à esquerda ({m})")
            self.assertLessEqual(m["x1"], m["vw"] + 1, f"{ctx}: menu cortado à direita ({m})")
            self.assertLessEqual(m["y1"], m["vh"] + 1, f"{ctx}: menu cortado embaixo ({m})")
            self.assertTrue(m["sobre_menu"], f"{ctx}: o menu não está acima do cabeçalho ({m})")
            pagina.keyboard.press("Escape")
            pagina.wait_for_selector(".bell-menu.show", state="hidden")
            pagina.context.close()

    # ----------------------------------------------------------------- bordas
    def test_bordas_refinadas_nas_tabelas(self):
        pagina = self.abrir(1366, 768)
        self.entrar(pagina, "admin-test@example.invalid")
        self.ir(pagina, "/admin/analises")
        m = pagina.evaluate("""() => {
            const t = document.querySelector('.nexo-table'); const cs = getComputedStyle(t);
            const tr = t.tBodies[0].rows[0]; const c0 = tr.cells[0]; const cN = tr.cells[tr.cells.length - 1];
            const th0 = t.tHead.rows[0].cells[0];
            return {collapse: cs.borderCollapse, spacing: cs.borderSpacing,
                    td0: getComputedStyle(c0).borderImageSource, tdN: getComputedStyle(cN).borderImageSource,
                    th0: getComputedStyle(th0).borderImageSource,
                    cabecalho: getComputedStyle(document.querySelector('.nexo-card-head') || document.body).borderImageSource};
        }""")
        self.assertEqual(m["collapse"], "separate", m)
        self.assertIn(m["spacing"], ("0px", "0px 0px"), m)
        self.assertIn("linear-gradient", m["td0"], "divisória da primeira célula sem recuo")
        self.assertIn("linear-gradient", m["tdN"], "divisória da última célula sem recuo")
        self.assertIn("linear-gradient", m["th0"], "divisória do cabeçalho sem recuo")
        # Realce arredondado ao passar o mouse (sem a barra reta de ponta a ponta).
        linha = pagina.locator(".nexo-table tbody tr[data-empresa]").first
        linha.hover()
        pagina.wait_for_timeout(250)
        h = pagina.evaluate("""() => { const tr = document.querySelector('.nexo-table tbody tr[data-empresa]');
            const c0 = getComputedStyle(tr.cells[0]), cN = getComputedStyle(tr.cells[tr.cells.length - 1]);
            return {r0: c0.borderTopLeftRadius, rN: cN.borderTopRightRadius, sombra: c0.boxShadow, fundo: c0.backgroundColor,
                    linha: getComputedStyle(tr).boxShadow}; }""")
        self.assertEqual(h["r0"], "12px", h)
        self.assertEqual(h["rN"], "12px", h)
        self.assertIn("inset", h["sombra"], h)
        self.assertEqual(h["linha"], "none", "a barra reta da linha inteira deve ter saído")
        self.assertNotEqual(h["fundo"], "rgba(0, 0, 0, 0)", h)
        # Última linha VISÍVEL sem divisória, mesmo com a linha-placeholder oculta no fim do tbody.
        u = pagina.evaluate("""() => {
            const linhas = [...document.querySelectorAll('.nexo-table tbody tr')].filter(r => !r.hidden && getComputedStyle(r).display !== 'none');
            const ult = linhas[linhas.length - 1]; const cs = getComputedStyle(ult.cells[0]);
            return {placeholder_oculto_no_fim: !!document.querySelector('.nexo-table tbody tr:last-child[hidden]'),
                    borda: cs.borderBottomWidth, imagem: cs.borderImageSource}; }""")
        self.assertTrue(u["placeholder_oculto_no_fim"], u)
        self.assertEqual(u["borda"], "0px", f"a última linha visível não deve ter divisória: {u}")
        self.assertEqual(u["imagem"], "none", u)
        self.assert_layout(pagina, 1366, "/admin/analises @ 1366px (bordas)")


if __name__ == "__main__":
    unittest.main()
