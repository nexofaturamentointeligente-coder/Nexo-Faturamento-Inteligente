"""Raio-X do PDV e Auditoria de custos no navegador (14/11/2026): o botão (i) de cada indicador, o card
dos avisos recolhido e as tabelas com as 5 linhas de maior impacto, busca, filtro rápido e ordem
(static/js/raio-x.js).

Ignorado por padrão, como os demais testes de navegador: precisa de NEXO_TEST_BROWSER=1, do Playwright e
das cópias íntegras da CDN (ver tests/test_overflow_navegador.py, de onde vêm o servidor local, o
navegador e as medidas de layout). O cliente passa para o PRATA e ganha um Raio-X de oito meses com
oito produtos vendidos abaixo do preço de tabela e do custo e um item de custo a confirmar.
"""

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from tests.test_overflow_navegador import NavegadorCase, _BROWSER

PAGINA = "/onboarding/diagnostico"
AUDITORIA = "/onboarding/diagnostico/auditoria"
MESES = range(5, 13)


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class RaioXNavegadorTests(NavegadorCase):
    def setUp(self):
        super().setUp()
        self._prata()
        self._raio_x_com_avisos()

    def _prata(self):
        from models import Empresa, Plano
        s = self.db.session
        plano = s.scalar(select(Plano).where(Plano.nome_plano == "PRATA"))
        if plano is None:
            plano = Plano(nome_plano="PRATA", valor_mensal=300, qtd_analises_mes=1, tipo_analise_permitida="MENSAL",
                          nivel_entrega_analise="COMPLETA", nivel_dashboard="GERENCIAL", nivel_atendimento="MEDIO")
            s.add(plano)
            s.flush()
        s.get(Empresa, self.empresa_id).id_plano_atual = plano.id_plano
        s.commit()

    def _raio_x_com_avisos(self):
        import raio_x
        import raio_x_servico
        from tests.raio_x_support import executar_como_o_worker, relatorio_do_mes
        with tempfile.TemporaryDirectory(prefix="raio-x-navegador-ux-") as pasta:
            caminhos = []
            for mes in MESES:
                # Oito produtos vendidos abaixo do preço de tabela (R$ 20,00) e, a partir de certo desconto,
                # abaixo do custo (R$ 195,00 por 10 unidades). O desconto cai mês a mês: o mês de maior
                # impacto é o mais antigo (maio), e a ordem "mais recentes" começa por dezembro. Em junho,
                # um item de custo a confirmar. Perda abaixo do custo por produto, de 2 a 8: 6, 19, 36, 51,
                # 70, 86 e 105 (373 no total): os que somam 80% são o 8, o 7, o 6 e o 5.
                linhas = [f"{i};Produto de teste {i};10;20,00;{200 - i * (13 - mes) // 2},00;195,00" for i in range(1, 9)]
                if mes == 6:
                    linhas.append("99;Item Generico;100;5,00;500,00;40000,00")
                caminho = Path(pasta) / f"arquivo_{mes:02d}.csv"
                caminho.write_bytes(relatorio_do_mes(2025, mes, linhas))
                caminhos.append(str(caminho))
            resultado = executar_como_o_worker(caminhos)
        raio_x_servico.salvar(self.empresa_id, self.cliente_id, resultado, raio_x.recomendar(resultado, 24),
                              [f"vendas-{mes:02d}-2025.csv" for mes in MESES])
        self.db.session.commit()

    def _pagina(self, caminho, largura=1366, altura=900):
        pagina = self.abrir(largura, altura)
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, caminho)
        return pagina

    @staticmethod
    def _visiveis(pagina, corpo):
        return pagina.evaluate(
            "(id) => Array.from(document.getElementById(id).rows).filter(r => !r.hidden).map(r => r.cells[0].innerText.trim())",
            corpo)

    # ------------------------------------------------------------ (i)
    def test_explicacao_abre_uma_de_cada_vez_e_fecha_com_esc_e_clique_fora(self):
        pagina = self._pagina(AUDITORIA)
        infos = pagina.locator("details[data-kpi-info]")
        self.assertEqual(infos.count(), 4)
        primeiro, segundo = infos.nth(0), infos.nth(1)
        primeiro.locator("summary").click()
        balao = primeiro.locator(".kpi-info-balao")
        self.assertTrue(balao.is_visible())
        # Na Auditoria, o (i) é curto: uma ou duas frases, sem as três partes do Raio-X.
        texto = balao.inner_text()
        self.assertIn("Quanto custaram os produtos que você vendeu", texto)
        self.assertNotIn("Como calculamos", texto)
        caixa = balao.bounding_box()
        self.assertGreaterEqual(caixa["x"], 0)
        self.assertLessEqual(caixa["x"] + caixa["width"], 1366)
        # O balão fica por cima do que estiver embaixo dele (o card vizinho, a seção seguinte).
        ponto = [caixa["x"] + caixa["width"] / 2, min(caixa["y"] + caixa["height"] - 4, 890)]
        dono = pagina.evaluate("([x, y]) => { const e = document.elementFromPoint(x, y); "
                               "return !!(e && e.closest('.kpi-info-balao')); }", ponto)
        self.assertTrue(dono, "o balão aberto ficou por baixo de outro elemento")
        segundo.locator("summary").click()
        self.assertFalse(primeiro.evaluate("e => e.open"))
        self.assertTrue(segundo.evaluate("e => e.open"))
        pagina.keyboard.press("Escape")
        self.assertFalse(segundo.evaluate("e => e.open"))
        self.assertTrue(segundo.locator("summary").evaluate("e => e === document.activeElement"))
        segundo.locator("summary").click()
        pagina.mouse.click(5, 300)
        self.assertFalse(segundo.evaluate("e => e.open"))

    def test_explicacao_do_raio_x_tem_as_tres_partes(self):
        pagina = self._pagina(PAGINA)
        info = pagina.locator("details[data-kpi-info]").first
        info.locator("summary").click()
        texto = info.locator(".kpi-info-balao").inner_text()
        for parte in ("O que significa", "Como calculamos", "Se estiver ruim"):
            self.assertIn(parte.upper() if parte.upper() in texto else parte, texto)
        # No Raio-X o (i) não abre ao passar o mouse: só pelo clique.
        segundo = pagina.locator("details[data-kpi-info]").nth(1)
        segundo.locator("summary").hover()
        pagina.wait_for_timeout(300)
        self.assertFalse(segundo.evaluate("e => e.open"))

    def test_explicacao_da_auditoria_abre_ao_passar_o_mouse_e_o_clique_fixa(self):
        pagina = self._pagina(AUDITORIA)
        primeiro = pagina.locator("details[data-kpi-info]").nth(0)
        primeiro.locator("summary").hover()
        self.assertTrue(primeiro.evaluate("e => e.open"))
        self.assertTrue(primeiro.locator(".kpi-info-balao").is_visible())
        # O ponteiro vai do botão ao balão (parte do <details>): continua aberto.
        primeiro.locator(".kpi-info-texto").hover()
        pagina.wait_for_timeout(400)
        self.assertTrue(primeiro.evaluate("e => e.open"))
        # Saiu: fecha pouco depois.
        pagina.mouse.move(5, 300)
        pagina.wait_for_timeout(400)
        self.assertFalse(primeiro.evaluate("e => e.open"))
        # O clique fixa o balão aberto, mesmo com o ponteiro longe; o próximo clique fecha.
        primeiro.locator("summary").click()
        pagina.mouse.move(5, 300)
        pagina.wait_for_timeout(400)
        self.assertTrue(primeiro.evaluate("e => e.open"))
        # A nota discreta dos itens isolados fica só no (i) da sobra bruta (o segundo card).
        self.assertEqual(primeiro.locator(".kpi-info-nota").count(), 0)
        self.assertIn("1 item com erro grave de cadastro",
                      pagina.locator("details[data-kpi-info]").nth(1).locator(".kpi-info-nota").text_content())
        primeiro.locator("summary").click()
        self.assertFalse(primeiro.evaluate("e => e.open"))
        # Passar por outro (i) fecha o fixado: um de cada vez.
        primeiro.locator("summary").click()
        segundo = pagina.locator("details[data-kpi-info]").nth(1)
        segundo.locator("summary").hover()
        self.assertTrue(segundo.evaluate("e => e.open"))
        pagina.wait_for_timeout(100)  # o evento toggle, que fecha os outros, vem numa tarefa depois
        self.assertFalse(primeiro.evaluate("e => e.open"))

    def test_explicacao_abre_pelo_teclado(self):
        pagina = self._pagina(PAGINA)
        botao = pagina.locator("details[data-kpi-info] > summary").first
        botao.focus()
        pagina.keyboard.press("Enter")
        self.assertTrue(pagina.locator("details[data-kpi-info]").first.evaluate("e => e.open"))
        self.assertIn("Entenda o indicador", botao.get_attribute("aria-label"))

    # ------------------------------------------------------------ avisos
    def test_avisos_recolhidos_abrem_pelo_resumo(self):
        pagina = self._pagina(PAGINA)
        avisos = pagina.locator("#avisosRaioX")
        self.assertFalse(avisos.evaluate("e => e.open"))
        resumo = avisos.locator(":scope > summary")
        self.assertIn("8 arquivos lidos", resumo.inner_text())
        self.assertIn("Ler avisos e detalhes técnicos", resumo.inner_text())
        self.assertFalse(pagina.locator(".avisos-corpo").is_visible())
        resumo.click()
        self.assertTrue(pagina.locator(".avisos-corpo").is_visible())
        self.assertIn("Fechar avisos", resumo.inner_text())
        self.assertTrue(pagina.get_by_role("heading", name="O que afeta seus números").is_visible())

    # ------------------------------------------------------------ tabelas
    def test_mes_a_mes_mostra_5_expande_busca_filtra_e_reordena(self):
        pagina = self._pagina(PAGINA)
        self.assertEqual(len(self._visiveis(pagina, "corpoFatias")), 5)
        bloco = pagina.locator("[data-tabela-rapida]").filter(has=pagina.locator("#tabelaFatias"))
        self.assertTrue(bloco.locator("[data-tabela-ferramentas]").is_visible())
        botao = bloco.locator("[data-tabela-mais]")
        self.assertEqual(botao.inner_text().strip(), "Ver todos os 8 meses")
        botao.click()
        self.assertEqual(len(self._visiveis(pagina, "corpoFatias")), 8)
        self.assertEqual(botao.inner_text().strip(), "Mostrar menos (top 5)")
        self.assertEqual(botao.get_attribute("aria-expanded"), "true")
        botao.click()
        self.assertEqual(len(self._visiveis(pagina, "corpoFatias")), 5)
        # A busca acha o mês por extenso; o filtro e a ordem trabalham juntos.
        bloco.locator("[data-tabela-busca]").fill("dezembro")
        self.assertEqual(self._visiveis(pagina, "corpoFatias"), ["12/2025"])
        bloco.locator("[data-tabela-busca]").fill("")
        self.assertEqual(self._visiveis(pagina, "corpoFatias")[0], "05/2025")  # o de maior impacto
        bloco.locator("[data-tabela-ordem]").select_option("recentes")
        self.assertEqual(self._visiveis(pagina, "corpoFatias"), ["12/2025", "11/2025", "10/2025", "09/2025", "08/2025"])
        # As listras alternam pelas linhas visíveis, e não pela posição entre as escondidas.
        self.assertEqual(pagina.evaluate("() => Array.from(document.getElementById('corpoFatias').rows)"
                                         ".filter(r => !r.hidden).map(r => r.classList.contains('is-listra'))"),
                         [False, True, False, True, False])
        bloco.locator("[data-tabela-busca]").fill("nada que exista")
        self.assertEqual(self._visiveis(pagina, "corpoFatias"), [])
        self.assertIn("Nenhum mês", bloco.locator("[data-tabela-contagem]").inner_text())

    def test_tabelas_da_auditoria_com_top_5_e_filtro_80_por_cento(self):
        pagina = self._pagina(AUDITORIA)
        linhas = self._visiveis(pagina, "corpoAbaixoCusto")
        self.assertEqual(len(linhas), 5)
        bloco = pagina.locator("[data-tabela-rapida]").filter(has=pagina.locator("#tabelaAbaixoCusto"))
        bloco.locator("[data-tabela-filtro]").select_option("principais")
        principais = self._visiveis(pagina, "corpoAbaixoCusto")
        self.assertEqual(principais, linhas[:4])  # 8, 7, 6 e 5: os primeiros da ordem de maior perda
        self.assertIn("Produto de teste 8", principais[0])
        bloco.locator("[data-tabela-busca]").fill("teste 2")
        self.assertEqual(self._visiveis(pagina, "corpoAbaixoCusto"), [])  # não está entre os 80%
        bloco.locator("[data-tabela-filtro]").select_option("")
        self.assertEqual(len(self._visiveis(pagina, "corpoAbaixoCusto")), 1)
        # O card da sobra bruta não leva o item de custo a confirmar: a nota do (i) diz que ele foi isolado.
        card = pagina.locator(".kpi-tile").filter(has_text="Sobra bruta nas vendas")
        self.assertIn("1 item com erro grave de cadastro", card.locator(".kpi-info-nota").text_content())

    def test_alertas_de_cadastro_recolhidos_abrem_com_o_texto_e_a_tabela(self):
        pagina = self._pagina(AUDITORIA)
        alertas = pagina.locator("#alertasCadastro")
        self.assertFalse(alertas.evaluate("e => e.open"))
        resumo = alertas.locator(":scope > summary")
        self.assertEqual(resumo.inner_text().strip(), "Alertas de cadastro: 1 item com custo distorcido ignorado na conta")
        self.assertFalse(pagina.locator("#tabelaCustoAConfirmar").is_visible())
        resumo.click()
        self.assertTrue(alertas.evaluate("e => e.open"))
        self.assertTrue(pagina.locator("#tabelaCustoAConfirmar").is_visible())
        self.assertIn("Corrija no seu PDV para a margem ficar exata.", alertas.inner_text())
        self.assertIn("Item Generico", pagina.locator("#tabelaCustoAConfirmar").inner_text())
        resumo.click()
        self.assertFalse(pagina.locator("#tabelaCustoAConfirmar").is_visible())

    def test_tabela_dos_alertas_vira_cartoes_ao_abrir_no_celular(self):
        """A tabela dos alertas fica num <details> fechado, medido com largura zero: ao abrir, a base refaz a conta e,
        no celular, ela vira cartões como as outras (ou ganha a rolagem com teclado)."""
        pagina = self._pagina(AUDITORIA, 390, 844)
        pagina.locator("#alertasCadastro > summary").click()
        # O layout é refeito no evento toggle, que vem numa tarefa depois do clique: espera em vez de medir já.
        pagina.wait_for_function("() => { const c = document.querySelector('#alertasCadastro .table-responsive-x'); "
                                 "return c.classList.contains('em-cartoes') || c.getAttribute('role') === 'region'; }",
                                 timeout=5000)
        self.assertTrue(pagina.locator("#tabelaAbaixoCusto").locator("xpath=ancestor::div[contains(@class,'table-responsive-x')]")
                        .evaluate("e => e.classList.contains('em-cartoes')"))
        self.assertLessEqual(pagina.evaluate("() => document.documentElement.scrollWidth"),
                             pagina.evaluate("() => document.documentElement.clientWidth"))

    def test_info_no_canto_superior_direito_dos_cards(self):
        pagina = self._pagina(AUDITORIA)
        for indice in range(4):
            tile = pagina.locator(".kpi-tile").nth(indice)
            info = tile.locator("details[data-kpi-info] > summary").bounding_box()
            icone = tile.locator(".kpi-icon").bounding_box()
            self.assertGreater(info["x"], icone["x"] + icone["width"] - 1, f"card {indice}")

    def test_lucro_bruto_por_produto_filtra_pela_faixa_de_margem(self):
        pagina = self._pagina(AUDITORIA)
        visiveis = self._visiveis(pagina, "corpoLucro")
        self.assertEqual(len(visiveis), 5)
        # O maior lucro bruto primeiro (o produto 1, com o menor desconto); o item de custo a confirmar não entra.
        self.assertTrue(visiveis[0].startswith("Produto de teste 1"), visiveis)
        self.assertEqual(pagina.locator("#corpoLucro").get_by_text("Item Generico").count(), 0)
        bloco = pagina.locator("[data-tabela-rapida]").filter(has=pagina.locator("#tabelaLucro"))
        botao = bloco.locator("[data-tabela-mais]")
        self.assertEqual(botao.inner_text().strip(), "Ver todos os 8 produtos")
        filtro = bloco.locator("[data-tabela-filtro]")
        self.assertEqual(filtro.locator("option").all_inner_texts(),
                         ["Todos", "Margem alta (> 40%)", "Margem baixa (< 20%)", "No vermelho (margem negativa)"])
        # Todas as margens do Raio-X de teste ficam abaixo de 20%, e nenhuma passa de 40%.
        filtro.select_option("margem_baixa")
        self.assertEqual(len(self._visiveis(pagina, "corpoLucro")), 5)
        self.assertIn("8 de 8 produtos no filtro", bloco.locator("[data-tabela-contagem]").inner_text())
        filtro.select_option("margem_alta")
        self.assertEqual(self._visiveis(pagina, "corpoLucro"), [])
        self.assertIn("Nenhum produto", bloco.locator("[data-tabela-contagem]").inner_text())
        # No vermelho: do produto 3 em diante (os dois primeiros fecham o período com sobra), o maior prejuízo
        # primeiro (o produto 8); a legenda da tabela diz a ordem.
        filtro.select_option("negativa")
        self.assertTrue(self._visiveis(pagina, "corpoLucro")[0].startswith("Produto de teste 8"))
        self.assertEqual(pagina.locator("#tabelaLucro caption").text_content(),
                         "Lucro bruto por produto, do maior para o menor prejuízo")
        botao.click()
        self.assertEqual(len(self._visiveis(pagina, "corpoLucro")), 6)
        self.assertTrue(self._visiveis(pagina, "corpoLucro")[-1].startswith("Produto de teste 3"))
        # Margem baixa: também pelo maior prejuízo; de volta a Todos, o maior lucro primeiro.
        filtro.select_option("margem_baixa")
        self.assertTrue(self._visiveis(pagina, "corpoLucro")[0].startswith("Produto de teste 8"))
        filtro.select_option("")
        self.assertTrue(self._visiveis(pagina, "corpoLucro")[0].startswith("Produto de teste 1"))
        self.assertEqual(pagina.locator("#tabelaLucro caption").text_content(),
                         "Lucro bruto por produto, do maior para o menor lucro bruto")

    def test_conteudo_alinhado_ao_selo_do_usuario(self):
        """Etapa 1: a borda direita dos cards e das tabelas coincide com a do selo do usuário, o último item da
        topbar, em qualquer largura (abaixo de 390 px o selo some, e vale o último botão), sem rolagem lateral."""
        medir = """() => {
            const direita = Array.from(document.querySelectorAll('.topbar-right > *'))
                .filter(e => getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().width > 0)
                .map(e => e.getBoundingClientRect().right);
            const cards = Array.from(document.querySelectorAll('#conteudo .nexo-card, #conteudo .kpi-tile'))
                .filter(e => e.getBoundingClientRect().width > 0).map(e => e.getBoundingClientRect().right);
            const chip = document.querySelector('.topbar-right .user-chip');
            return {selo: Math.max(...direita), cards: Math.max(...cards), menor: Math.min(...cards),
                    chip_visivel: !!chip && getComputedStyle(chip).display !== 'none',
                    chip: chip ? chip.getBoundingClientRect().right : null,
                    vw: document.documentElement.clientWidth, rolagem: document.documentElement.scrollWidth};
        }"""
        for caminho in (PAGINA, AUDITORIA):
            for largura in (2560, 1920, 1366, 1024, 768, 390, 320):
                with self.subTest(caminho=caminho, largura=largura):
                    pagina = self._pagina(caminho, largura, 900)
                    m = pagina.evaluate(medir)
                    self.assertLessEqual(abs(m["selo"] - m["cards"]), 1, m)
                    if largura >= 390:
                        self.assertTrue(m["chip_visivel"], m)
                        self.assertLessEqual(abs(m["chip"] - m["cards"]), 1, m)
                    self.assertLessEqual(m["rolagem"], m["vw"], m)
                    pagina.context.close()

    def test_balao_abre_abaixo_do_botao_sem_cobri_lo(self):
        """O balão abre logo abaixo da linha do (i): nem o rótulo em várias linhas (cards estreitos) nem o valor
        centralizado da recomendação (computador) põem o balão por cima do botão que o abriu (revisão de 14/11)."""
        for caminho, largura in ((PAGINA, 1366), (PAGINA, 1200), (AUDITORIA, 1200), (AUDITORIA, 768)):
            with self.subTest(caminho=caminho, largura=largura):
                pagina = self._pagina(caminho, largura, 900)
                botoes = pagina.locator("details[data-kpi-info] > summary")
                for indice in range(botoes.count()):
                    botao = botoes.nth(indice)
                    botao.scroll_into_view_if_needed()
                    botao.click()
                    caixa = botao.bounding_box()
                    balao = botao.locator("xpath=..").locator(".kpi-info-balao").bounding_box()
                    self.assertGreaterEqual(balao["y"], caixa["y"] + caixa["height"] - 0.5, f"(i) {indice}")
                    centro = [caixa["x"] + caixa["width"] / 2, caixa["y"] + caixa["height"] / 2]
                    self.assertTrue(pagina.evaluate("([x, y]) => !!document.elementFromPoint(x, y).closest('summary')",
                                                    centro), f"(i) {indice} coberto")
                    botao.click()  # o mesmo botão fecha
                    self.assertFalse(botao.locator("xpath=..").evaluate("e => e.open"))
                pagina.context.close()

    def test_mostrar_menos_devolve_o_botao_a_vista_e_a_legenda_diz_a_ordem(self):
        pagina = self._pagina(PAGINA, 1366, 520)
        bloco = pagina.locator("[data-tabela-rapida]").filter(has=pagina.locator("#tabelaFatias"))
        legenda = pagina.locator("#tabelaFatias caption")
        self.assertTrue(legenda.text_content().endswith(", mais dinheiro a recuperar primeiro"))
        bloco.locator("[data-tabela-ordem]").select_option("recentes")
        self.assertTrue(legenda.text_content().endswith(", mais recentes primeiro"))
        botao = bloco.locator("[data-tabela-mais]")
        botao.click()
        botao.scroll_into_view_if_needed()
        botao.click()
        self.assertEqual(len(self._visiveis(pagina, "corpoFatias")), 5)
        caixa = botao.bounding_box()
        self.assertTrue(0 <= caixa["y"] and caixa["y"] + caixa["height"] <= 520, caixa)

    def test_celular_sem_rolagem_lateral_com_balao_e_avisos_abertos(self):
        for caminho in (PAGINA, AUDITORIA):
            with self.subTest(caminho=caminho):
                pagina = self._pagina(caminho, 320, 640)
                if caminho == PAGINA:
                    pagina.locator("#avisosRaioX > summary").click()
                pagina.locator("details[data-kpi-info] > summary").nth(1).click()
                self.assert_layout(pagina, 320, f"{caminho} @ 320px com o balão aberto")
                pagina.context.close()


if __name__ == "__main__":
    unittest.main()
