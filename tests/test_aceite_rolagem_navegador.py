"""Regra da rolagem do aceite e documentos legais públicos, em navegador real (Chromium via Playwright).

Com a CSP real do servidor e o static/js/aceite-rolagem.js de verdade, em 390x844, 844x390 e 1366x768:
  - a caixa "Li e concordo" e o botão "Aceitar e continuar" nascem disabled e só liberam quando CADA texto
    chega ao fim (roda do mouse, End e PageDown no texto em foco); quase no fim não basta;
  - texto curto, sem rolagem, libera na hora; redimensionar a janela confere de novo;
  - o aviso em aria-live diz o que falta e o que fazer; a trava burlada no navegador não passa no servidor;
  - o aceite pelas Configurações do cliente segue a mesma regra;
  - o admin edita, salva a v1.1, e a página pública mostra a v1.1 na hora;
  - as páginas públicas, de 320 a 1440 px, sem rolagem horizontal e sem violação de CSP.
Habilitado por NEXO_TEST_BROWSER=1, como em tests/test_overflow_navegador.py.
"""

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from tests.test_overflow_navegador import NavegadorCase, _BROWSER

CLIENTE = "cliente-test@example.invalid"
ADMIN = "admin-test@example.invalid"
TELAS = ((390, 844), (844, 390), (1366, 768))
LONGO = "Texto fictício para o teste da rolagem.\n\n" + "Cláusula longa de teste. " * 300
CURTO = "Texto curto de teste."
BOTAO = "#modalTermos button[type=submit].btn-nexo"
_COLETOR = """
window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__cspViolations.push({diretiva: e.violatedDirective, origem: e.blockedURI});
});
"""
_ESTADO = """() => ({
    caixa: document.getElementById('termosConfirmo').disabled,
    botao: document.querySelector('#modalTermos button[type=submit].btn-nexo').disabled,
    aviso: document.getElementById('termosAvisoRolagem').textContent.trim(),
    lidos: [...document.querySelectorAll('#modalTermos [data-aceite-texto]')].map((t) => t.dataset.aceiteLido === '1'),
})"""
# Leva o texto `indice` até `falta` px do fim, como a rolagem da pessoa (o evento scroll vem no quadro seguinte).
_ROLAR = """([indice, falta]) => {
    const texto = document.querySelectorAll('[data-aceite-texto]')[indice];
    texto.scrollTop = texto.scrollHeight - texto.clientHeight - falta;
    return texto.scrollHeight - texto.clientHeight - texto.scrollTop; }"""
# Depois das fontes e de dois quadros: a primeira conferência do script já rodou.
_PRIMEIRA_CONFERENCIA = """() => document.fonts.ready.then(() => new Promise((pronto) =>
    requestAnimationFrame(() => requestAnimationFrame(pronto))))"""
_LIBERADA = "() => !document.getElementById('termosConfirmo').disabled"
_SOBRA_HORIZONTAL = "() => document.documentElement.scrollWidth - window.innerWidth"


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class AceiteRolagemNavegadorTests(NavegadorCase):
    def dados_extensos(self):
        """Só os documentos de cada teste: os dados longos das telas do portal não entram aqui."""

    # ------------------------------------------------------------ apoio
    def _publicar(self, termos=LONGO, politica=LONGO):
        from models import DocumentoLegal
        instante = datetime(2026, 2, 1, 13, tzinfo=timezone.utc)
        for tipo, texto in (("TERMOS_USO", termos), ("POLITICA_PRIVACIDADE", politica)):
            if texto:
                self.db.session.add(DocumentoLegal(tipo=tipo, versao=1, conteudo=texto, id_usuario_admin=self.admin_id,
                                                   publicado_em=instante))
        self.db.session.commit()

    def _limpar_aceites(self):
        from models import AceiteDocumento
        self.db.session.query(AceiteDocumento).delete()
        self.db.session.commit()

    def _sessao(self, largura, altura, email=CLIENTE):
        pagina = self.abrir(largura, altura)
        pagina.add_init_script(_COLETOR)
        self.entrar(pagina, email)
        return pagina

    def _modal(self, largura, altura):
        pagina = self._sessao(largura, altura)
        self.ir(pagina, "/cliente/dashboard")
        self.assertTrue(pagina.is_visible("#modalTermos"))
        pagina.evaluate(_PRIMEIRA_CONFERENCIA)
        return pagina

    def _estado(self, pagina):
        return pagina.evaluate(_ESTADO)

    def _sem_violacao(self, pagina, contexto):
        self.assertEqual(pagina.evaluate("() => window.__cspViolations || []"), [], contexto)

    def _sem_sobra_horizontal(self, pagina, contexto):
        self.assertLessEqual(pagina.evaluate(_SOBRA_HORIZONTAL), 0, contexto)
        self.assertLessEqual(pagina.evaluate(
            "() => { const c = document.querySelector('#modalTermos .modal-content'); return c.scrollWidth - c.clientWidth; }"),
            0, contexto)

    # ------------------------------------------------------------ modal do cliente
    def test_nasce_travado_e_libera_so_no_fim_de_cada_texto(self):
        self._publicar()
        for largura, altura in TELAS:
            contexto = f"{largura}x{altura}"
            with self.subTest(tela=contexto):
                pagina = self._modal(largura, altura)
                self._sem_sobra_horizontal(pagina, contexto)
                estado = self._estado(pagina)
                self.assertEqual((estado["caixa"], estado["botao"], estado["lidos"]), (True, True, [False, False]))
                self.assertEqual(estado["aviso"], "Role até o fim de cada texto para habilitar.")
                self.assertTrue(pagina.is_visible("#termosAvisoRolagem"))
                self.assertEqual(pagina.get_attribute("#termosAvisoRolagem", "aria-live"), "polite")
                for texto in pagina.query_selector_all("[data-aceite-texto]"):
                    self.assertEqual(texto.evaluate("(t) => getComputedStyle(t).overflowY"), "scroll")
                    self.assertGreater(texto.evaluate("(t) => t.scrollHeight - t.clientHeight"), 100)
                # O rótulo da caixa travada continua legível (o Bootstrap o deixaria com metade da opacidade).
                self.assertEqual(pagina.evaluate(
                    "() => getComputedStyle(document.querySelector('label[for=termosConfirmo]')).opacity"), "1")
                # Quase no fim do primeiro texto: nada libera.
                self.assertEqual(pagina.evaluate(_ROLAR, [0, 40]), 40)
                pagina.wait_for_timeout(200)
                self.assertEqual(self._estado(pagina)["lidos"], [False, False])
                # A roda do mouse até o fim do primeiro: falta o segundo, e o aviso diz qual.
                textos = pagina.locator("#modalTermos [data-aceite-texto]")
                textos.nth(0).hover()
                pagina.mouse.wheel(0, 400)
                pagina.wait_for_function("() => document.querySelectorAll('[data-aceite-lido]').length === 1")
                estado = self._estado(pagina)
                self.assertEqual((estado["caixa"], estado["botao"], estado["lidos"]), (True, True, [True, False]))
                self.assertEqual(estado["aviso"], "Falta rolar até o fim: Política de Privacidade.")
                # Subir de novo não desfaz a leitura; o fim do segundo libera a caixa e o botão.
                pagina.evaluate(_ROLAR, [0, 2000])
                textos.nth(1).hover()
                pagina.mouse.wheel(0, 100000)
                pagina.wait_for_function(_LIBERADA)
                estado = self._estado(pagina)
                self.assertEqual((estado["caixa"], estado["botao"], estado["lidos"]), (False, False, [True, True]))
                self.assertEqual(estado["aviso"], "Você chegou ao fim dos textos. Agora marque a caixa para aceitar.")
                # Sem marcar a caixa, o navegador barra; marcada, o aceite vai e o portal volta.
                posts = []
                pagina.on("request", lambda pedido: posts.append(pedido.url) if pedido.method == "POST" else None)
                pagina.click(BOTAO)
                pagina.wait_for_timeout(200)
                self.assertEqual(posts, [])
                pagina.check("#termosConfirmo")
                with pagina.expect_navigation(wait_until="load"):
                    pagina.click(BOTAO)
                self.assertEqual(pagina.locator("#modalTermos").count(), 0)
                self.assertIn("Aceite registrado.", pagina.content())
                self._sem_violacao(pagina, contexto)
                pagina.context.close()
                self._limpar_aceites()

    def test_texto_curto_libera_na_hora_e_o_longo_continua_travado(self):
        self._publicar(termos=CURTO, politica=CURTO)
        for largura, altura in TELAS:
            with self.subTest(tela=f"{largura}x{altura}"):
                pagina = self._modal(largura, altura)
                pagina.wait_for_function(_LIBERADA, timeout=5000)
                estado = self._estado(pagina)
                self.assertEqual((estado["caixa"], estado["botao"], estado["lidos"]), (False, False, [True, True]))
                self.assertTrue(pagina.evaluate(
                    "() => [...document.querySelectorAll('[data-aceite-texto]')].every((t) => t.scrollHeight <= t.clientHeight)"))
                pagina.context.close()
        # Um curto e um longo: o curto já conta, e o aviso aponta o que falta.
        from models import DocumentoLegal
        self.db.session.query(DocumentoLegal).delete()
        self.db.session.commit()
        self._publicar(termos=LONGO, politica=CURTO)
        pagina = self._modal(1366, 768)
        pagina.wait_for_function("() => document.querySelectorAll('[data-aceite-lido]').length === 1")
        estado = self._estado(pagina)
        self.assertEqual((estado["caixa"], estado["lidos"]), (True, [False, True]))
        self.assertEqual(estado["aviso"], "Falta rolar até o fim: Termos de Uso.")

    def test_teclado_end_e_pagedown_liberam(self):
        self._publicar()
        for largura, altura in TELAS:
            with self.subTest(tela=f"{largura}x{altura}"):
                pagina = self._modal(largura, altura)
                textos = pagina.locator("#modalTermos [data-aceite-texto]")
                # Tab do modal chega ao primeiro texto (a caixa e o botão travados ficam fora da ordem).
                pagina.keyboard.press("Tab")
                self.assertTrue(textos.nth(0).evaluate("(t) => t === document.activeElement"))
                pagina.keyboard.press("End")
                pagina.wait_for_function("() => document.querySelectorAll('[data-aceite-lido]').length === 1")
                self.assertTrue(pagina.is_disabled("#termosConfirmo"))
                pagina.keyboard.press("Tab")
                self.assertTrue(textos.nth(1).evaluate("(t) => t === document.activeElement"))
                for _ in range(120):
                    if not pagina.is_disabled("#termosConfirmo"):
                        break
                    pagina.keyboard.press("PageDown")
                    pagina.wait_for_timeout(40)
                self.assertFalse(pagina.is_disabled("#termosConfirmo"))
                # Liberada, a caixa entra na ordem do Tab logo depois do último texto.
                pagina.keyboard.press("Tab")
                self.assertEqual(pagina.evaluate("() => document.activeElement.id"), "termosConfirmo")
                pagina.keyboard.press("Space")
                self.assertTrue(pagina.is_checked("#termosConfirmo"))
                pagina.context.close()

    def test_redimensionar_a_janela_confere_de_novo(self):
        doze_linhas = "\n".join(f"Linha {numero} do texto de teste." for numero in range(1, 13))
        self._publicar(termos=doze_linhas, politica=CURTO)
        pagina = self._modal(844, 390)
        pagina.wait_for_function("() => document.querySelectorAll('[data-aceite-lido]').length === 1")
        estado = self._estado(pagina)
        self.assertEqual((estado["caixa"], estado["lidos"]), (True, [False, True]))
        self.assertGreater(pagina.evaluate("() => { const t = document.querySelector('[data-aceite-texto]'); return t.scrollHeight - t.clientHeight; }"), 2)
        # Janela mais alta: o texto passa a caber inteiro e libera sem rolar.
        pagina.set_viewport_size({"width": 1366, "height": 1000})
        pagina.wait_for_function(_LIBERADA, timeout=5000)
        self.assertEqual(pagina.evaluate("() => document.querySelector('[data-aceite-texto]').scrollTop"), 0)
        self.assertEqual(self._estado(pagina)["aviso"], "Você chegou ao fim dos textos. Agora marque a caixa para aceitar.")
        # Voltar a uma janela baixa não trava de novo: o texto já foi lido inteiro.
        pagina.set_viewport_size({"width": 844, "height": 390})
        pagina.wait_for_timeout(200)
        self.assertFalse(pagina.is_disabled("#termosConfirmo"))

    def test_trava_burlada_no_navegador_nao_passa_no_servidor(self):
        from models import AceiteDocumento
        self._publicar()
        pagina = self._modal(1366, 768)
        pagina.evaluate("() => document.querySelectorAll('[data-aceite-libera]').forEach((e) => { e.disabled = false; e.required = false; })")
        with pagina.expect_navigation(wait_until="load"):
            pagina.click(BOTAO)
        self.assertTrue(pagina.is_visible("#modalTermos"))
        self.assertIn("Marque “Li e concordo", pagina.content())
        self.assertEqual(self.db.session.query(AceiteDocumento).count(), 0)
        # O modal volta travado: a leitura começa de novo.
        self.assertTrue(pagina.is_disabled("#termosConfirmo"))

    # ------------------------------------------------------------ Configurações do cliente
    def test_aceite_pelas_configuracoes_segue_a_mesma_regra(self):
        import termos_legais
        self._publicar(politica=None)
        botao = "form[action$='/configuracoes/aceitar-documento'] button[type=submit]"
        # A trava troca as Configurações pelo modal; sem pendência na trava, aparece o caminho alternativo.
        with patch.object(termos_legais, "pendentes_da_sessao", return_value=[]):
            for largura, altura in TELAS:
                contexto = f"{largura}x{altura}"
                with self.subTest(tela=contexto):
                    pagina = self._sessao(largura, altura)
                    self.ir(pagina, "/cliente/configuracoes")
                    pagina.evaluate(_PRIMEIRA_CONFERENCIA)
                    self.assertTrue(pagina.is_disabled(botao))
                    self.assertEqual(pagina.inner_text("[data-aceite-aviso]").strip(), "Role até o fim do texto para habilitar.")
                    self.assertEqual(pagina.eval_on_selector("[data-aceite-texto]", "(t) => getComputedStyle(t).overflowY"), "scroll")
                    self.assertLessEqual(pagina.evaluate(_SOBRA_HORIZONTAL), 0, contexto)
                    self.assertEqual(pagina.evaluate(_ROLAR, [0, 40]), 40)
                    pagina.wait_for_timeout(200)
                    self.assertTrue(pagina.is_disabled(botao))
                    pagina.hover("[data-aceite-texto]")
                    pagina.mouse.wheel(0, 400)
                    pagina.wait_for_function(f"() => !document.querySelector(\"{botao}\").disabled")
                    self.assertEqual(pagina.inner_text("[data-aceite-aviso]").strip(), "Você chegou ao fim do texto e já pode aceitar.")
                    with pagina.expect_navigation(wait_until="load"):
                        pagina.click(botao)
                    self.assertIn("Aceite registrado.", pagina.content())
                    self._sem_violacao(pagina, contexto)
                    pagina.context.close()
                    self._limpar_aceites()

    # ------------------------------------------------------------ admin e páginas públicas
    def test_admin_edita_e_salva_a_v1_1_que_vale_na_hora_na_pagina_publica(self):
        self._publicar(termos="Texto da v1.0 para o teste.", politica=None)
        for largura, altura in ((1366, 768), (390, 844)):
            with self.subTest(tela=f"{largura}x{altura}"):
                pagina = self._sessao(largura, altura, email=ADMIN)
                self.ir(pagina, "/admin/configuracoes")
                editor = "#documento-TERMOS_USO"
                artigo = pagina.locator("article", has=pagina.locator(editor))
                self.assertFalse(artigo.locator("textarea").is_visible())  # modo leitura
                self.assertTrue(artigo.locator(".config-documento-texto[role=region]").is_visible())
                self.assertTrue(artigo.locator(".config-editor-fechado").is_visible())  # "Editar"
                artigo.locator(".config-editor > summary").click()
                self.assertTrue(artigo.locator("textarea").is_visible())
                self.assertTrue(artigo.locator(".config-editor-aberto").is_visible())  # "Fechar o editor"
                self.assertFalse(artigo.locator(".config-editor-fechado").is_visible())
                novo = f"Texto da revisão em {largura} px para o teste."
                artigo.locator("textarea").fill(novo)
                with pagina.expect_navigation(wait_until="load"):
                    artigo.locator("button[type=submit]").click()
                self.assertIn("publicada e já vale na página pública", pagina.content())
                self.assertLessEqual(pagina.evaluate(_SOBRA_HORIZONTAL), 0)
                self.ir(pagina, "/termos-de-uso")
                self.assertEqual(pagina.inner_text(".doc-texto").strip(), novo)
                self._sem_violacao(pagina, f"admin {largura}px")
                pagina.context.close()

    def test_paginas_publicas_sem_rolagem_horizontal_e_sem_violacao_de_csp(self):
        palavra_longa = "Palavra" * 60
        self._publicar(termos=f"{LONGO}\n\n{palavra_longa}", politica=None)
        pagina = self.abrir(320, 700)
        pagina.add_init_script(_COLETOR)
        for largura in (320, 390, 768, 1024, 1366, 1440):
            pagina.set_viewport_size({"width": largura, "height": 800})
            for caminho in ("/termos-de-uso", "/politica-de-privacidade"):
                with self.subTest(largura=largura, caminho=caminho):
                    self.ir(pagina, caminho)
                    self.assertLessEqual(pagina.evaluate(_SOBRA_HORIZONTAL), 0)
                    self.assertTrue(pagina.is_visible("h1"))
                    self._sem_violacao(pagina, f"{caminho} @ {largura}px")
        # Pular para o conteúdo: o primeiro Tab mostra o atalho, e o Enter leva ao texto.
        self.ir(pagina, "/termos-de-uso")
        pagina.keyboard.press("Tab")
        self.assertEqual(pagina.evaluate("() => document.activeElement.className"), "doc-pular")
        pagina.keyboard.press("Enter")
        self.assertEqual(pagina.evaluate("() => document.activeElement.id"), "conteudo")


if __name__ == "__main__":
    unittest.main()
