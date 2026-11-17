"""Painel da Avaliação de Cenário no navegador real (Edge ou Chromium pelo Playwright), com a CSP real do servidor.

  1. "Ver Avaliação de Cenário" abre o painel lateral: o diagnóstico do lead carrega (aria-busy volta a false), o
     título ganha o nome do lead e recebe o foco, a página atrás não rola; Esc fecha e devolve o foco ao botão; o
     clique no fundo também fecha.
  2. ?ver=<id> abre o painel sozinho; ao fechar, o ?ver= sai do endereço e recarregar não abre de novo.
  3. Em 390 e 1280 px: nem a página nem o painel rolam na horizontal, nada do painel sai dele, e ele fica dentro da
     tela, com a altura toda (no celular, a tela inteira; no computador, 1100 px à direita).
  4. Sessão vencida: o aviso "Sua sessão expirou: recarregue a página." no painel, nunca a tela de login dentro dele.
  5. O lead da landing: o painel mostra que ainda não foi processado; "Processar relatórios do lead" (Storage e
     processo isolado por dublês, no próprio processo) volta para a lista com o painel aberto e os números.
Em todos: nenhuma violação de CSP (securitypolicyviolation) nem erro de JavaScript (NavegadorCase.tearDown).
Habilitado por NEXO_TEST_BROWSER=1, como os outros testes de navegador; dados fictícios.
"""

import hashlib
import json
import unittest
from unittest.mock import patch

from tests.test_csp_navegador import _COLETOR
from tests.test_overflow_navegador import NavegadorCase, _BROWSER

_CARREGADO = """() => {
    const d = document.getElementById('painelLead');
    if (!d || !d.open) return false;
    const corpo = d.querySelector('[data-painel-corpo]');
    return corpo.getAttribute('aria-busy') === 'false' && !!corpo.querySelector('[data-painel-fragmento]');
}"""
_FECHADO = "() => !document.getElementById('painelLead').open"
_MEDIR_PAINEL = """() => {
    const d = document.getElementById('painelLead');
    const corpo = d.querySelector('[data-painel-corpo]');
    const r = d.getBoundingClientRect();
    // Nada do painel além das bordas dele, fora o que está dentro de uma tabela que rola na horizontal.
    const fora = [...d.querySelectorAll('*')].filter(el => {
        const b = el.getBoundingClientRect();
        if (!b.width || !b.height || getComputedStyle(el).visibility === 'hidden') return false;
        for (let p = el.parentElement; p && p !== d; p = p.parentElement) {
            if (getComputedStyle(p).overflowX !== 'visible' && p !== corpo && p.scrollWidth > p.clientWidth + 1) return false;
        }
        return b.right > r.right + 1 || b.left < r.left - 1;
    }).map(el => el.tagName.toLowerCase() + '.' + String(el.className).split(' ')[0]).slice(0, 5);
    return {paginaRola: document.documentElement.scrollWidth > innerWidth + 1,
            painelRola: d.scrollWidth > d.clientWidth + 1, corpoRola: corpo.scrollWidth > corpo.clientWidth + 1,
            x0: r.left, x1: r.right, y0: r.top, largura: r.width, altura: r.height, vw: innerWidth, vh: innerHeight,
            fora};
}"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class PainelLeadNavegadorTests(NavegadorCase):
    def _lista(self, largura, altura, caminho="/admin/leads"):
        pagina = self.abrir(largura, altura)
        pagina.add_init_script(_COLETOR)
        self.entrar(pagina, "admin-test@example.invalid")
        self.ir(pagina, caminho)
        return pagina

    def _botao(self, pagina, lead_id=None):
        return pagina.locator(f'a[data-painel-url="/admin/leads/{lead_id or self.lead_id}/painel"]')

    def _sem_violacoes(self, pagina):
        violacoes = pagina.evaluate("() => window.__cspViolations || []")
        self.assertEqual(violacoes, [], json.dumps(violacoes, ensure_ascii=False))

    def test_abre_carrega_e_fecha_devolvendo_o_foco(self):
        pagina = self._lista(1280, 800)
        botao = self._botao(pagina)
        self.assertEqual((botao.get_attribute("aria-haspopup"), botao.get_attribute("aria-controls")),
                         ("dialog", "painelLead"))
        botao.click()
        pagina.wait_for_function(_CARREGADO, timeout=20000)
        estado = pagina.evaluate("""() => ({
            foco: document.activeElement && document.activeElement.id,
            titulo: document.getElementById('painelLeadTitulo').textContent,
            texto: document.querySelector('[data-painel-corpo]').textContent,
            pagina: document.querySelector('[data-painel-pagina]').getAttribute('href'),
            anuncio: document.querySelector('[data-painel-estado]').textContent,
            semRolagem: getComputedStyle(document.documentElement).overflow === 'hidden',
            tabelasProntas: [...document.querySelectorAll('#painelLead .table-responsive-x')].every(c => c.dataset.pronta === '1')})""")
        self.assertEqual(estado["foco"], "painelLeadTitulo")
        self.assertTrue(estado["titulo"].startswith("Avaliação de Cenário: Supermercado Fictício Irmãos Albuquerque"),
                        estado["titulo"])
        for trecho in ("Resumo executivo: caixa resgatável", "Matriz de triagem", "Estoque parado e excedente",
                       "Simulador de liberação de caixa", "Maria Aparecida dos Santos"):
            self.assertIn(trecho, estado["texto"])
        self.assertEqual(estado["pagina"], f"/admin/leads/{self.lead_id}")
        self.assertEqual(estado["anuncio"], "Avaliação de Cenário carregada.")
        self.assertTrue(estado["semRolagem"])
        # As tabelas do painel passaram pelo ajuste do portal (cartões ou rolagem com foco).
        self.assertTrue(estado["tabelasProntas"])
        pagina.keyboard.press("Escape")
        pagina.wait_for_function(_FECHADO)
        self.assertEqual(pagina.evaluate("() => document.activeElement.getAttribute('data-painel-url')"),
                         f"/admin/leads/{self.lead_id}/painel")
        self.assertEqual(pagina.evaluate("() => document.querySelector('[data-painel-corpo]').childElementCount"), 0)
        # O clique no fundo escurecido (à esquerda do painel) também fecha.
        botao.click()
        pagina.wait_for_function(_CARREGADO, timeout=20000)
        pagina.mouse.click(30, 400)
        pagina.wait_for_function(_FECHADO)
        self.assertFalse(pagina.evaluate("() => getComputedStyle(document.documentElement).overflow === 'hidden'"))
        self._sem_violacoes(pagina)

    def test_ver_na_consulta_abre_sozinho_e_sai_do_endereco(self):
        pagina = self._lista(1280, 800, f"/admin/leads?ver={self.lead_id}")
        pagina.wait_for_function(_CARREGADO, timeout=20000)
        self.assertEqual(pagina.evaluate("() => document.activeElement.id"), "painelLeadTitulo")
        pagina.locator("[data-painel-fechar]").click()
        pagina.wait_for_function(_FECHADO)
        self.assertNotIn("ver=", pagina.url)
        self.assertEqual(pagina.evaluate("() => document.activeElement.getAttribute('data-painel-url')"),
                         f"/admin/leads/{self.lead_id}/painel")
        pagina.reload(wait_until="load")
        pagina.wait_for_timeout(400)
        self.assertFalse(pagina.evaluate("() => document.getElementById('painelLead').open"))
        self._sem_violacoes(pagina)

    def test_sem_rolagem_horizontal_em_390_e_1280(self):
        for largura, altura in ((390, 844), (1280, 800)):
            with self.subTest(largura=largura):
                pagina = self._lista(largura, altura)
                self.assertFalse(pagina.evaluate("() => document.documentElement.scrollWidth > innerWidth + 1"),
                                 f"{largura}px: a lista rola na horizontal")
                self._botao(pagina).click()
                pagina.wait_for_function(_CARREGADO, timeout=20000)
                pagina.wait_for_timeout(300)  # o ajuste das tabelas e dos KPIs roda depois das fontes
                medida = pagina.evaluate(_MEDIR_PAINEL)
                contexto = f"{largura}px: {medida}"
                self.assertFalse(medida["paginaRola"], contexto)
                self.assertFalse(medida["painelRola"], contexto)
                self.assertFalse(medida["corpoRola"], contexto)
                self.assertEqual(medida["fora"], [], contexto)
                self.assertGreaterEqual(medida["x0"], -1, contexto)
                self.assertLessEqual(medida["x1"], medida["vw"] + 1, contexto)
                self.assertAlmostEqual(medida["y0"], 0, delta=1, msg=contexto)
                self.assertAlmostEqual(medida["altura"], medida["vh"], delta=1, msg=contexto)
                self.assertAlmostEqual(medida["x1"], medida["vw"], delta=1, msg=contexto)
                largura_esperada = medida["vw"] if largura < 576 else min(1100, 0.96 * medida["vw"])
                self.assertAlmostEqual(medida["largura"], largura_esperada, delta=1, msg=contexto)
                # Só o corpo rola: no fim dele, o cabeçalho do painel continua no topo.
                pagina.evaluate("() => { const c = document.querySelector('[data-painel-corpo]'); c.scrollTop = c.scrollHeight; }")
                topo = pagina.evaluate("() => document.querySelector('.painel-lead-topo').getBoundingClientRect().top")
                self.assertAlmostEqual(topo, 0, delta=1, msg=contexto)
                self._sem_violacoes(pagina)
                pagina.context.close()

    def test_sessao_vencida_mostra_o_aviso_e_nunca_o_login(self):
        pagina = self._lista(1280, 800)
        pagina.context.clear_cookies()
        self._botao(pagina).click()
        pagina.wait_for_function("""() => { const c = document.querySelector('[data-painel-corpo]');
            return c.getAttribute('aria-busy') === 'false' && c.textContent.trim().length > 0; }""", timeout=20000)
        texto = pagina.evaluate("() => document.querySelector('[data-painel-corpo]').textContent.trim()")
        self.assertEqual(texto, "Sua sessão expirou: recarregue a página.")
        self.assertEqual(pagina.locator("#painelLead form, #painelLead input").count(), 0)
        self.assertEqual(pagina.evaluate("() => document.activeElement.id"), "painelLeadTitulo")
        self._sem_violacoes(pagina)

    def test_lead_da_landing_processado_pelo_painel(self):
        from sqlalchemy import update
        from models import LeadAvaliacao
        from tests.raio_x_support import avaliar_como_o_worker
        from tests.test_leads_painel import _StorageDuble
        from tests.test_portal_diagnostico import LOTE
        storage = _StorageDuble()
        lead = LeadAvaliacao(nome_contato="Contato da Landing", email="landing@example.invalid", whatsapp="11912345678",
                             nome_loja="Loja da Landing Fictícia", status="pendente", origem="site")
        self.db.session.add(lead)
        self.db.session.flush()
        registros = []
        for indice, conteudo in enumerate(LOTE, 1):
            sha = hashlib.sha256(conteudo).hexdigest()
            caminho = f"2026/11/{lead.id}/{indice:02d}-{sha[:16]}.csv"
            storage.objetos[caminho] = conteudo
            registros.append({"caminho": caminho, "nome": f"vendas-{indice}.csv", "bytes": len(conteudo),
                              "formato": "csv", "sha256": sha})
        tabela = LeadAvaliacao.__table__
        self.db.session.execute(update(tabela).where(tabela.c.id == lead.id).values(
            interesse="raio_x", status_arquivo="aprovado", arquivos_storage=json.dumps(registros)))
        self.db.session.commit()
        lead_id = lead.id
        self.stack.enter_context(patch("armazenamento_leads.do_app", return_value=storage))
        self.stack.enter_context(patch("etl_runtime.run_avaliacao_cenario",
                                       side_effect=lambda caminhos, limits=None: avaliar_como_o_worker(caminhos, limits)))
        pagina = self._lista(1280, 800)
        self._botao(pagina, lead_id).click()
        pagina.wait_for_function(_CARREGADO, timeout=20000)
        texto = pagina.evaluate("() => document.querySelector('[data-painel-corpo]').textContent")
        self.assertIn("Avaliação de Cenário ainda não processada.", texto)
        self.assertIn("vendas-1.csv", texto)
        with pagina.expect_navigation(wait_until="load"):
            pagina.locator("#painelLead button[type=submit]", has_text="Processar relatórios do lead").click()
        self.assertIn(f"ver={lead_id}", pagina.url)
        pagina.wait_for_function(_CARREGADO, timeout=20000)
        estado = pagina.evaluate("""() => ({
            avisos: (document.querySelector('[data-painel-avisos]') || {}).textContent || '',
            texto: document.querySelector('[data-painel-corpo]').textContent})""")
        self.assertIn("Avaliação concluída: 4 relatório(s) de vendas.", estado["avisos"])
        for trecho in ("Matriz de triagem", "Resumo executivo: caixa resgatável", "Processar de novo"):
            self.assertIn(trecho, estado["texto"])
        self.assertNotIn("ainda não processada", estado["texto"])
        self.assertEqual(len(storage.pedidos), 4)
        self._sem_violacoes(pagina)


if __name__ == "__main__":
    unittest.main()
