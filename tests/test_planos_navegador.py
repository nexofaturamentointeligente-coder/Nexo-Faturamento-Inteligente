"""Planos por nível e Raio-X no navegador: o convite sobre o dado desfocado e o envio de vários
relatórios (static/js/planos.js e a dropzone de base.html).

Ignorado por padrão, como os demais testes de navegador: precisa de NEXO_TEST_BROWSER=1, do
Playwright e das cópias íntegras da CDN (ver tests/test_overflow_navegador.py, de onde vêm o
servidor local, o navegador e os dados: o cliente é BRONZE e já tem um Raio-X gravado).
"""

import json
import unittest
from unittest.mock import patch

from tests.raio_x_support import FEVEREIRO, JANEIRO, executar_como_o_worker
from tests.test_overflow_navegador import NavegadorCase, _BROWSER

PAGINA = "/onboarding/diagnostico"


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class PlanosNavegadorTests(NavegadorCase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch("etl_runtime.run_raio_x", side_effect=executar_como_o_worker))

    def _pagina(self):
        pagina = self.abrir(1366, 900)
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, PAGINA)
        return pagina

    def _envios(self, pagina):
        envios = []
        pagina.on("request", lambda req: envios.append(req.url) if req.method == "POST" else None)
        return envios

    @staticmethod
    def _arquivo(nome, conteudo, tipo="text/csv"):
        return {"name": nome, "mimeType": tipo, "buffer": conteudo}

    def test_convite_abre_no_foco_e_fecha_com_esc(self):
        pagina = self._pagina()
        marcador = pagina.locator(".dado-protegido").first
        self.assertEqual(marcador.get_attribute("href"), "/onboarding/planos#prata")
        self.assertEqual(marcador.inner_text().strip(), "R$ 0.000,00")  # marcador fixo, não o valor
        marcador.focus()
        pagina.wait_for_selector(".popover", state="visible")
        self.assertIn("plano Prata", pagina.locator(".popover").inner_text())
        pagina.keyboard.press("Escape")
        pagina.wait_for_selector(".popover", state="detached")

    def test_convite_fica_aberto_com_o_ponteiro_sobre_ele(self):
        pagina = self._pagina()
        marcador = pagina.locator(".dado-protegido").first
        marcador.scroll_into_view_if_needed()
        marcador.hover()
        balao = pagina.wait_for_selector(".popover", state="visible")
        caixa = balao.bounding_box()
        pagina.mouse.move(caixa["x"] + caixa["width"] / 2, caixa["y"] + caixa["height"] / 2, steps=5)
        pagina.wait_for_timeout(400)
        self.assertTrue(pagina.locator(".popover").is_visible())
        pagina.mouse.move(5, 5)
        pagina.wait_for_selector(".popover", state="detached")

    def test_arquivo_fora_do_formato_e_barrado_antes_do_envio(self):
        pagina = self._pagina()
        envios = self._envios(pagina)
        entrada = pagina.locator("input[name=arquivos]")
        entrada.set_input_files([self._arquivo("jan.csv", JANEIRO),
                                 self._arquivo("contrato.pdf", b"%PDF-1.4", "application/pdf")])
        erro = pagina.locator("[data-dz-multiplo] [data-dz-erro]")
        self.assertIn("contrato.pdf", erro.inner_text())
        pagina.click("form[data-envio-longo] button[type=submit]")
        pagina.wait_for_timeout(300)
        self.assertEqual(envios, [])
        # O clique barrado não deixa o formulário marcado como enviado: o envio válido segue.
        self.assertIsNone(pagina.evaluate("() => document.querySelector('form[data-envio-longo]').dataset.enviado"
                                          " || null"))
        entrada.set_input_files([self._arquivo("jan.csv", JANEIRO)])
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("form[data-envio-longo] button[type=submit]")
        self.assertEqual(len(envios), 1)

    def test_limites_de_tamanho_e_quantidade_barram_antes_do_envio(self):
        pagina = self._pagina()
        envios = self._envios(pagina)
        entrada = pagina.locator("input[name=arquivos]")
        erro = pagina.locator("[data-dz-multiplo] [data-dz-erro]")
        rotulo = pagina.locator("[data-dz-multiplo] [data-dz-label]")
        pagina.evaluate("() => { document.querySelector('[data-dz-multiplo]').dataset.maxBytes = '100'; }")
        entrada.set_input_files([self._arquivo("jan.csv", JANEIRO), self._arquivo("fev.csv", FEVEREIRO)])
        self.assertIn("Os arquivos somam", erro.inner_text())
        self.assertEqual(rotulo.inner_text(), "Arraste os relatórios ou clique para selecionar")
        self.assertEqual(pagina.evaluate("() => document.querySelector('input[name=arquivos]').files.length"), 0)
        pagina.evaluate("() => { document.querySelector('[data-dz-multiplo]').dataset.maxBytes = '20971520'; }")
        entrada.set_input_files([self._arquivo(f"mes-{n:02d}.csv", JANEIRO) for n in range(37)])
        self.assertIn("no máximo 36 arquivos", erro.inner_text())
        pagina.click("form[data-envio-longo] button[type=submit]")
        pagina.wait_for_timeout(300)
        self.assertEqual(envios, [])

    def test_envio_sem_arquivo_nao_descarta_o_proximo(self):
        pagina = self._pagina()
        envios = self._envios(pagina)
        pagina.click("form[data-envio-longo] button[type=submit]")
        self.assertIn("Selecione um arquivo", pagina.locator("[data-dz-multiplo] [data-dz-erro]").inner_text())
        pagina.locator("input[name=arquivos]").set_input_files([self._arquivo("jan.csv", JANEIRO),
                                                                self._arquivo("fev.csv", FEVEREIRO)])
        self.assertEqual(pagina.locator("[data-dz-multiplo] [data-dz-label]").inner_text(), "2 arquivos selecionados")
        # O botão fica ocupado durante a leitura: o estado é guardado antes de a página trocar.
        pagina.evaluate("""() => document.querySelector('form[data-envio-longo]').addEventListener('submit', () =>
            setTimeout(() => { const b = document.querySelector('form[data-envio-longo] button[type=submit]');
                sessionStorage.setItem('botao', JSON.stringify([b.disabled, b.textContent.trim()])); }, 0))""")
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("form[data-envio-longo] button[type=submit]")
        self.assertEqual(len(envios), 1)
        self.assertEqual(json.loads(pagina.evaluate("() => sessionStorage.getItem('botao')")),
                         [True, "Lendo os relatórios…"])
        self.assertIn("Raio-X concluído com 2 relatório(s)", pagina.content())


if __name__ == "__main__":
    unittest.main()
