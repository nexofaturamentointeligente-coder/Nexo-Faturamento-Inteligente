"""Links wa.me no navegador real (Chromium/Edge via Playwright), com o número configurado.

Com WHATSAPP_EMPRESA_NUMERO preenchido, as telas que ganharam botão de WhatsApp (landing,
login, "Como aderir", Visão Geral com a faixa de renovação, Suporte, chamado, Planos e a
resposta de upgrade) não rolam na horizontal em 320, 390 e 1366 px, cada link novo fica
inteiro dentro da tela, o NexoBot não o encobre no fim da página e nenhuma violação de CSP
aparece. O wa.me nunca é aberto e a rede continua só local: o único clique é no CONTRATAR da landing,
que abre o pré-cadastro (e as saídas dele só são lidas, não clicadas).

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
            # Na landing: CONTRATAR, "Falar com a equipe NEXO" e o rodapé (o pré-cadastro fica fechado).
            for caminho, esperados in (("/", 3), ("/auth/login", 0), ("/como-aderir", 2)):
                with self.subTest(caminho=caminho, largura=largura):
                    self.ir(pagina, caminho)
                    pagina.wait_for_timeout(300)
                    medida = self._assert_links(pagina, f"{caminho} @ {largura}px", esperados)
                    self.assertEqual(medida["fora"], [], f"{caminho} @ {largura}px: controle fora da tela")
            pagina.context.close()

    def test_precadastro_abre_pelo_contratar_e_monta_a_mensagem(self):
        """O CONTRATAR abre o pré-cadastro, que começa no formulário (Etapa 1 oculta). Avançar não
        sai da página nem chama o servidor; as saídas montam o wa.me com o número do config e só o
        que o visitante digitou. O diálogo cabe na tela, fecha com Esc e devolve o foco."""
        from urllib.parse import parse_qs, urlsplit
        esperado = ("*NOVO CONTATO COMERCIAL | NEXO*\n\n"
                    "Nome: Maria Fictícia\n"
                    "Empresa: Tintas Exemplo\n"
                    "WhatsApp: (62) 98888-1234\n"
                    "Plano de interesse: Prata\n"
                    "E-mail: maria@example.invalid\n"
                    "Faixa de faturamento: Até R$ 50 mil\n"
                    "Principal necessidade: Repor\n\nsem excesso\n\n"
                    "Situação do Raio-X: Não solicitado\n"
                    "Solicitou auxílio: Não\n"
                    "Origem: Landing Page da NEXO")
        medir_dialogo = """() => {
            const caixa = document.getElementById('precadastro').getBoundingClientRect();
            const fora = [...document.querySelectorAll('#precadastro a, #precadastro button, #precadastro input,'
                + ' #precadastro select, #precadastro textarea')].filter(el => {
                const b = el.getBoundingClientRect();
                return b.width && (b.left < caixa.left - 1 || b.right > caixa.right + 1);
            }).map(el => el.id || el.tagName);
            return {x0: caixa.left, x1: caixa.right, vw: innerWidth, fora,
                    rola: document.documentElement.scrollWidth > innerWidth + 1};
        }"""
        requisicoes = []
        for largura, altura in ((320, 568), (1366, 768)):
            with self.subTest(largura=largura):
                pagina = self._abrir(largura, altura)
                self.ir(pagina, "/")
                pagina.on("request", lambda req: requisicoes.append((req.method, req.url)))
                contratar = pagina.locator("[data-precadastro-gatilho] a")
                self.assertEqual((contratar.get_attribute("role"), contratar.get_attribute("aria-haspopup")),
                                 ("button", "dialog"))
                contratar.click()
                dialogo = pagina.locator("#precadastro")
                dialogo.wait_for(state="visible")
                self.assertFalse(pagina.locator("#step-1").is_visible())
                self.assertTrue(pagina.locator("#step-2").is_visible())
                self.assertEqual(pagina.evaluate("document.activeElement.id"), "lead-nome")
                # Vazio, o navegador barra o avanço.
                pagina.locator("#btn-next-2").click()
                self.assertTrue(pagina.locator("#step-2").is_visible())
                # Colado como o WhatsApp copia (com o DDI), com o 00 internacional ou com texto antes: fica só DDD e
                # número; o DDD 55 do Rio Grande do Sul fica. O que viraria outro número (12 dígitos sem "+", DDI
                # sem DDD, estrangeiro) fica como veio, inválido.
                for colado, no_campo in (("+55 11 98765-4321", "(11) 98765-4321"),
                                         ("0055 11 98765-4321", "(11) 98765-4321"),
                                         ("Meu WhatsApp é: +55 11 98765-4321", "(11) 98765-4321"),
                                         ("\u202d+55 11 98765-4321\u202c", "(11) 98765-4321"),
                                         ("(0xx11) 3222\u20111234", "(11) 3222-1234"),
                                         ("3222-1234 21", "3222-1234 21"),
                                         ("55 99999-1234", "(55) 99999-1234"),
                                         ("+55 99999-8877", "+55 99999-8877"),
                                         ("+1 202 555 0100", "+1 202 555 0100"),
                                         ("559927654321", "559927654321")):
                    pagina.fill("#lead-whatsapp", colado)
                    self.assertEqual(pagina.input_value("#lead-whatsapp"), no_campo, colado)
                # Nome só com espaços e esse número não avançam.
                pagina.fill("#lead-nome", "   ")
                pagina.fill("#lead-empresa", "Tintas Exemplo")
                pagina.select_option("#lead-plano", "Prata")
                pagina.locator("#btn-next-2").click()
                self.assertTrue(pagina.locator("#step-2").is_visible())
                self.assertFalse(pagina.locator("#step-3").is_visible())
                self.assertEqual(pagina.eval_on_selector("#lead-nome", "el => el.validationMessage"),
                                 "Preencha este campo.")
                self.assertEqual(pagina.eval_on_selector("#lead-whatsapp", "el => el.validationMessage"),
                                 "Informe um WhatsApp do Brasil com DDD: 10 ou 11 números.")
                # Corrigir no meio do número: a máscara não joga o cursor para o fim; o blur remonta.
                pagina.fill("#lead-whatsapp", "")
                pagina.locator("#lead-whatsapp").press_sequentially("119814567")
                self.assertEqual(pagina.input_value("#lead-whatsapp"), "(11) 9814-567")
                pagina.locator("#lead-whatsapp").evaluate("el => el.setSelectionRange(8, 8)")
                pagina.locator("#lead-whatsapp").press_sequentially("23")
                pagina.locator("#lead-empresa").focus()
                self.assertEqual(pagina.input_value("#lead-whatsapp"), "(11) 98123-4567")
                pagina.fill("#lead-nome", "  Maria   Fictícia ")
                pagina.fill("#lead-whatsapp", "")
                pagina.locator("#lead-whatsapp").press_sequentially("62988881234")
                self.assertEqual(pagina.input_value("#lead-whatsapp"), "(62) 98888-1234")
                pagina.select_option("#lead-plano", "Prata")
                pagina.fill("#lead-email", "maria@example.invalid")
                pagina.select_option("#lead-faturamento", "Até R$ 50 mil")
                pagina.fill("#lead-desafio", "Repor\n\n\n\nsem excesso")
                pagina.locator("#btn-next-2").click()
                self.assertTrue(pagina.locator("#step-3").is_visible())
                self.assertEqual(pagina.url, self.base + "/")
                # Com o Raio-X público desligado (o padrão), o botão dele não aparece.
                self.assertTrue(pagina.locator("#btn-raiox-fazer").is_hidden())
                medida = pagina.evaluate(medir_dialogo)
                self.assertFalse(medida["rola"], f"{largura}px: a página rola na horizontal")
                self.assertGreaterEqual(medida["x0"], 0, medida)
                self.assertLessEqual(medida["x1"], medida["vw"], medida)
                self.assertEqual(medida["fora"], [], f"{largura}px: controle fora do diálogo")
                href = pagina.locator("#btn-raiox-pular").get_attribute("href")
                partes = urlsplit(href)
                self.assertEqual((partes.scheme, partes.netloc, partes.path), ("https", "wa.me", "/" + NUMERO))
                self.assertEqual(parse_qs(partes.query, strict_parsing=True), {"text": [esperado]})
                ajuda = parse_qs(urlsplit(pagina.locator("#btn-raiox-ajuda").get_attribute("href")).query)["text"][0]
                self.assertTrue(ajuda.endswith("Situação do Raio-X: Não realizado\n"
                                               "Solicitou auxílio: Sim, cliente solicitou auxílio com relatórios\n"
                                               "Origem: Landing Page da NEXO"), ajuda)
                pagina.keyboard.press("Escape")
                dialogo.wait_for(state="hidden")
                self.assertTrue(pagina.evaluate("document.activeElement.closest('[data-precadastro-gatilho]') !== null"))
                self.assertEqual(pagina.evaluate("window.__cspViolations"), [])
                pagina.context.close()
        # Nada do pré-cadastro foi ao servidor: depois de carregar a página, nenhum POST e nenhuma
        # chamada à aplicação além de arquivos estáticos (fontes e mídia podem carregar sob demanda).
        self.assertEqual([r for r in requisicoes
                          if r[0] != "GET" or (r[1].startswith(self.base) and "/static/" not in r[1])], [])

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
