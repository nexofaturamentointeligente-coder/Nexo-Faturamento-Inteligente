"""Links wa.me no navegador real (Chromium/Edge via Playwright), com o número configurado.

Com WHATSAPP_EMPRESA_NUMERO preenchido, as telas que ganharam botão de WhatsApp (landing,
login, "Como aderir", Visão Geral com a faixa de renovação, Suporte, chamado, Planos e a
resposta de upgrade) não rolam na horizontal em 320, 390 e 1366 px, cada link novo fica
inteiro dentro da tela, o NexoBot não o encobre no fim da página e nenhuma violação de CSP
aparece. Nada é clicado: o wa.me nunca é aberto e a rede continua só local.

Reaproveita o harness de tests/test_overflow_navegador.py (servidor local, CDN respondida
com as cópias íntegras de NEXO_TEST_CDN_DIR e dados fictícios extensos). Habilitado por
NEXO_TEST_BROWSER=1, como na CI.
"""

from datetime import date
import unittest

from tests.test_overflow_navegador import _BROWSER, NavegadorCase

NUMERO = "5562988887777"
LARGURAS = ((320, 568), (390, 844), (1366, 768))
_COLETOR_CSP = """
window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__cspViolations.push({diretiva: e.violatedDirective, origem: e.blockedURI, amostra: (e.sample || '').slice(0, 80)});
});
"""
_MEDIR = """() => {
    const vw = innerWidth;
    const visivel = el => { const cs = getComputedStyle(el); return cs.display !== 'none' && cs.visibility !== 'hidden'; };
    const texto = el => el.textContent.replace(/\\s+/g, ' ').trim();
    const links = [...document.querySelectorAll('a[href^="https://wa.me/"]')].map(a => {
        const b = a.getBoundingClientRect();
        return {texto: texto(a).slice(0, 60), x0: b.left, x1: b.right, alvo: a.target, rel: a.rel,
                visivel: visivel(a) && b.width > 0 && b.height > 0};
    });
    const fora = [...document.querySelectorAll('a, button, input, h1, h2')].filter(el => {
        const b = el.getBoundingClientRect();
        return b.width && visivel(el) && (b.left < -1 || b.right > vw + 1);
    }).map(el => el.tagName.toLowerCase() + ' "' + texto(el).slice(0, 40) + '"');
    return {rola: document.documentElement.scrollWidth > vw + 1, vw, links, fora, csp: window.__cspViolations || []};
}"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class WhatsAppNavegadorTests(NavegadorCase):
    def setUp(self):
        super().setUp()
        from sqlalchemy import select
        from models import ChamadoSuporte
        # O config já criado é lido na hora de desenhar cada página.
        self.app.config["WHATSAPP_EMPRESA_NUMERO"] = NUMERO
        # Contrato da empresa do cliente vencendo em 8 dias: a faixa de renovação aparece.
        self.contratar(data_inicio=date(2026, 1, 1), data_fim=date(2026, 3, 10))
        self.ativar_regras()
        self.chamado_id = self.db.session.scalar(select(ChamadoSuporte.id_chamado).where(
            ChamadoSuporte.id_empresa == self.empresa_id).order_by(ChamadoSuporte.id_chamado))

    def _abrir(self, largura, altura):
        pagina = self.abrir(largura, altura)
        pagina.add_init_script(_COLETOR_CSP)
        return pagina

    def _assert_links(self, pagina, contexto, esperados):
        medida = pagina.evaluate(_MEDIR)
        self.assertFalse(medida["rola"], f"{contexto}: a página rola na horizontal")
        self.assertEqual(len(medida["links"]), esperados, f"{contexto}: {medida['links']}")
        for link in medida["links"]:
            self.assertTrue(link["visivel"], f"{contexto}: link do WhatsApp invisível ({link})")
            self.assertGreaterEqual(link["x0"], -1, f"{contexto}: link do WhatsApp cortado à esquerda ({link})")
            self.assertLessEqual(link["x1"], medida["vw"] + 1, f"{contexto}: link do WhatsApp cortado à direita ({link})")
            self.assertEqual((link["alvo"], link["rel"]), ("_blank", "noopener noreferrer"), contexto)
        self.assertEqual(medida["csp"], [], f"{contexto}: violação de CSP")
        return medida

    def test_paginas_publicas_com_whatsapp_cabem_na_tela(self):
        for largura, altura in LARGURAS:
            pagina = self._abrir(largura, altura)
            for caminho, esperados in (("/", 5), ("/auth/login", 0), ("/como-aderir", 2)):
                with self.subTest(caminho=caminho, largura=largura):
                    self.ir(pagina, caminho)
                    pagina.wait_for_timeout(300)
                    medida = self._assert_links(pagina, f"{caminho} @ {largura}px", esperados)
                    self.assertEqual(medida["fora"], [], f"{caminho} @ {largura}px: controle fora da tela")
            pagina.context.close()

    def test_paginas_do_cliente_com_whatsapp_cabem_na_tela(self):
        paginas = (("/cliente/dashboard", 200, 2), ("/cliente/tickets", 200, 1),
                   (f"/cliente/tickets/{self.chamado_id}", 200, 1), ("/onboarding/planos", 200, 1),
                   ("/onboarding/diagnostico/auditoria", 403, 1), ("/como-aderir", 200, 2))
        for largura, altura in LARGURAS:
            pagina = self._abrir(largura, altura)
            self.entrar(pagina, "cliente-test@example.invalid")
            for caminho, status, esperados in paginas:
                contexto = f"{caminho} @ {largura}px"
                with self.subTest(caminho=caminho, largura=largura):
                    self.ir(pagina, caminho, status)
                    self._assert_links(pagina, contexto, esperados)
                    medida = self.medir(pagina)
                    self.assertFalse(medida["rolagem_x"], f"{contexto}: a página rola na horizontal")
                    self.assertEqual(medida["fora"], [], f"{contexto}: elementos fora da tela")
                    self.assert_fim_da_pagina(pagina, contexto)
                if status == 403:
                    # A resposta 403 da própria página aparece no console; não é erro de script.
                    self.erros_js = [erro for erro in self.erros_js if "403" not in erro]
            pagina.context.close()


if __name__ == "__main__":
    unittest.main()
