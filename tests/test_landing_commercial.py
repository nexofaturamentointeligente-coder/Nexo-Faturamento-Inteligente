"""COM-001: apresentação comercial, destinos honestos e navegação real.

Atualizado no redesenho da landing (decisões do dono, 16/11/2026): topo com "Entrar" e "Cadastre-se", hero
centralizado sem o painel, benefícios financeiros, indicadores ampliados, quatro planos, CTA final só com o
título e o CONTRATAR, e o rodapé com os contatos fixos (o e-mail de suporte é o único mailto).
HTTP/DOM usa a aplicação e migrações isoladas existentes. Playwright é
habilitado por NEXO_TEST_BROWSER=1, como na CI; nenhuma conexão de produção.
Os depoimentos inseridos nos testes de carrossel existem somente no DOM de teste.
"""

from html.parser import HTMLParser
import os
import re
import threading
import unittest
from unittest.mock import patch

from flask import render_template

from tests.deploy_support import DeployTestCase
from tests.landing_support import servir_bootstrap
from tests.test_nexobot_navegador import _CONECTAR_LOCAL

EMAIL_SUPORTE = "suportenexocolab@gmail.com"
# Os endereços dos documentos legais (frente J, blueprint documentos_publicos): os únicos links "legais" aceitos.
ROTAS_LEGAIS = ("/termos-de-uso", "/politica-de-privacidade")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class _LandingDOM(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elements, self.visible = [], []
        self.excluded = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag in {"script", "style"}:
            self.excluded += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.excluded = max(0, self.excluded - 1)

    def handle_data(self, data):
        if not self.excluded:
            self.visible.append(data)

    def elements_with(self, attribute):
        return [(tag, attrs) for tag, attrs in self.elements if attribute in attrs]

    @property
    def text(self):
        return " ".join(" ".join(self.visible).split())


class LandingCommercialHTTPTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        # Os contatos da landing fixos no teste, mesmo com as variáveis no ambiente de quem roda a suíte.
        self.app.config.update(EMAIL_SUPORTE=EMAIL_SUPORTE, CNPJ_EMPRESA="")

    def landing(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        return html, _LandingDOM(html)

    def test_landing_has_clear_benefit_headline_and_commercial_process(self):
        html, dom = self.landing()
        self.assertEqual(sum(tag == "h1" for tag, _ in dom.elements), 1)
        self.assertIn("Decisões mais claras", dom.text)
        self.assertIn("relatórios de compras e vendas", dom.text)
        for title in ("Envie seus relatórios", "Organização dos dados", "Indicadores estratégicos", "Parecer estratégico"):
            self.assertIn(title, dom.text)
        self.assertIn("Parecer Estratégico do Sistema", dom.text)
        # A revisão pela equipe (COM-001, 3.2) continua no Método; a nota de baixo dos passos saiu (pedido do dono).
        self.assertIn("A equipe NEXO revisa a análise antes de disponibilizá-la no portal", dom.text)
        self.assertNotIn("A equipe NEXO acompanha a análise", dom.text)
        self.assertIn('<main id="conteudo"', html)

    def test_header_has_login_link_signup_button_and_mobile_navigation_contract(self):
        html, _ = self.landing()
        header = html.split("<header", 1)[1].split("</header>", 1)[0]
        # "Entrar" é o único link de login do topo; "Cadastre-se" abre o cadastro (sem JavaScript, vai ao rodapé).
        self.assertEqual(header.count('href="/auth/login"'), 1)
        self.assertIn('<a href="/auth/login" class="lp-entrar">Entrar</a>', header)
        self.assertIn('<a href="#contato" class="btn-nexo btn-nexo-primary lp-cadastre" data-cadastro-gatilho>'
                      'Cadastre-se</a>', header)
        self.assertNotIn("Acessar Portal", header)
        self.assertNotIn("Ver Demo do Painel", header)
        self.assertIn('id="landing-menu-toggle"', header)
        self.assertIn('aria-controls="landing-navigation"', header)
        self.assertIn('aria-expanded="false"', header)
        self.assertIn('id="landing-navigation"', header)

    def test_anchor_targets_and_accessibility_references_are_valid(self):
        _, dom = self.landing()
        identifiers = [attrs["id"] for _, attrs in dom.elements_with("id")]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for _, attrs in dom.elements:
            href = attrs.get("href", "")
            if href.startswith("#"):
                self.assertIn(href[1:], identifiers, href)
            for name in ("aria-controls", "aria-describedby", "aria-labelledby"):
                for target in attrs.get(name, "").split():
                    self.assertIn(target, identifiers, (name, target))

    def test_local_destinations_and_assets_exist_and_login_remains_functional(self):
        _, dom = self.landing()
        paths = {attrs[name] for _, attrs in dom.elements for name in ("href", "src")
                 if attrs.get(name, "").startswith("/")}
        for path in paths:
            with self.subTest(path=path):
                response = self.client.head(path)
                try:
                    self.assertIn(response.status_code, (200, 302))
                finally:
                    response.close()
        self.fixtures()
        self.login()
        response = self.client.get("/cliente/dashboard")
        self.assertEqual(response.status_code, 200)

    def test_academic_labels_and_technical_sales_copy_are_absent(self):
        _, dom = self.landing()
        for forbidden in ("acadêmico", "projeto integrador", "faculdade", "projeto didático", "pandas", "pipeline", "plotly.js", "kpis do mvp"):
            self.assertNotIn(forbidden, dom.text.casefold())
        self.assertNotRegex(dom.text, r"\bMVP\b")
        for badge in ("Sem cadastro automático", "Atendimento consultivo", "Dados processados com Pandas"):
            self.assertNotIn(badge, dom.text)

    def test_official_indicators_are_preserved_and_the_list_shows_what_the_portal_has(self):
        html, dom = self.landing()
        texto = dom.text.casefold()
        for label in ("Faturamento total", "Total comprado", "Indicador de Pressão de Estoque",
                      "Produto mais vendido", "Produto com maior faturamento", "saldo estimado parado", "Curva ABC"):
            self.assertIn(label.casefold(), texto)
        # A lista ampliada (pedido do dono): só o que já tem tela no portal.
        for label in ("Dinheiro recuperável (estimativa)", "Saúde do cadastro (0 a 100)", "Vendas sem custo informado",
                      "Produtos vendidos fora do preço normal", "Síntese executiva com semáforos de ação",
                      "Prejuízo direto no caixa", "Margem e sobra bruta", "Plano de Ação (12 meses)",
                      "Evolução & Conquistas"):
            self.assertIn(label.casefold(), texto)
        # Os indicadores executivos calculados sem tela não são anunciados.
        for proibido in ("21 indicadores", "indicadores executivos", "gmroi", "ticket médio"):
            self.assertNotIn(proibido, texto)
        indicadores = html.split('id="indicadores"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Prata em diante", indicadores)
        self.assertGreaterEqual(indicadores.count('<span class="lp-selo">Estimativa</span>'), 2)
        # O texto longo de baixo da lista saiu (pedido do dono); a estimativa segue marcada no próprio item.
        self.assertNotIn("não uma medição de estoque físico, lucro ou margem", dom.text)
        self.assertNotIn('id="stock-estimate-note"', html)
        self.assertNotIn("Aviso de Transparência", dom.text)
        self.assertIn("Indicadores Estratégicos", dom.text)

    def test_contracting_and_review_buttons_do_not_promise_unavailable_flows(self):
        html, dom = self.landing()
        pending = [attrs for tag, attrs in dom.elements if tag == "button" and "disabled" in attrs]
        # CONTRATAR único (decisão do dono, 15/11/2026), avaliação, as setas do carrossel e o "Continuar" do cadastro.
        self.assertGreaterEqual(len(pending), 4)
        described = {attrs.get("aria-describedby") for attrs in pending}
        self.assertTrue({"contratacao-status"} <= described)
        for title in ("BRONZE", "PRATA", "OURO", "PERSONALIZADO / ENTERPRISE", "CONTRATAR", "Sob consulta"):
            self.assertIn(title, dom.text)
        self.assertEqual(dom.text.count("Sob consulta"), 4)
        self.assertEqual(dom.text.count("CONTRATAR"), 1)
        self.assertNotIn("Contrate Agora", dom.text)
        self.assertIn("A contratação ainda não está disponível nesta página", dom.text)
        self.assertIn('href="/avaliar"', html)
        plans = html.split('id="planos"', 1)[1].split("</section>", 1)[0]
        self.assertNotIn('href="/auth/login"', plans)
        # Abaixo dos cards não há mais o aviso de contratação nem o "Como obter acesso" (pedido do dono).
        self.assertNotIn('href="/como-aderir"', plans)
        self.assertNotIn("Contrate pelo WhatsApp", plans)
        self.assertNotIn("<button", plans)
        # CTA final: só o título e o CONTRATAR.
        cta = html.split('aria-labelledby="final-cta-title"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Pronto para transformar seu negócio?", cta)
        self.assertIn("Deixe a NEXO fazer parte disso!", cta)
        for removido in ("Falar com a equipe NEXO", "Como obter acesso", "contact-availability",
                         "Organize seus relatórios, entenda os indicadores"):
            self.assertNotIn(removido, cta)
        # Sem número válido não há pré-cadastro; o único diálogo é o do cadastro do lead, com o seu formulário.
        self.assertNotIn('<dialog id="precadastro"', html)
        dialogos = [attrs.get("id") for tag, attrs in dom.elements if tag == "dialog"]
        formularios = [attrs.get("id") for tag, attrs in dom.elements if tag == "form"]
        self.assertEqual((dialogos, formularios), (["cadastro-lead"], ["cadastro-lead-form"]))

    def test_footer_contacts_are_the_official_ones_and_no_corporate_data_is_invented(self):
        html, dom = self.landing()
        links = [attrs.get("href", "") for _, attrs in dom.elements]
        # Sem número: nenhum WhatsApp; o e-mail de suporte (fixo, decisão do dono) é o único mailto; nada de tel:.
        self.assertFalse(any("wa.me" in link or link.startswith("tel:") for link in links))
        self.assertEqual([link for link in links if link.startswith("mailto:")], [f"mailto:{EMAIL_SUPORTE}"])
        self.assertNotIn("suporte@nexofaturamento.com.br", dom.text)
        self.assertNotIn("5511999999999", dom.text)
        # CNPJ_EMPRESA vazio: nenhum CNPJ na página.
        self.assertNotRegex(dom.text, r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")
        self.assertNotIn("CNPJ", dom.text)
        self.assertNotIn("ainda não estão disponíveis nesta página", dom.text)
        # Links legais só para as páginas públicas dos documentos (entram na junção, no marcador LINKS_LEGAIS).
        legais = [link for link in links if any(token in link.casefold() for token in ("privacidade", "termos",
                                                                                       "cancelamento"))]
        self.assertTrue(set(legais) <= set(ROTAS_LEGAIS), legais)
        rodape = html.split("<footer", 1)[1].split("</footer>", 1)[0]
        self.assertIn('href="https://www.instagram.com/nexo.faturamento/"', rodape)
        self.assertIn("Desenvolvido por João César N. S. Castro", rodape)
        # Links externos em nova aba, sem dar acesso à página de origem.
        for tag, attrs in dom.elements:
            if tag == "a" and attrs.get("href", "").startswith("https://"):
                self.assertEqual((attrs.get("target"), attrs.get("rel")), ("_blank", "noopener noreferrer"),
                                 attrs.get("href"))

    def test_empty_reviews_have_no_fictitious_customer_or_testimonial(self):
        _, dom = self.landing()
        self.assertEqual(dom.elements_with("data-review-slide"), [])
        self.assertIn("Ainda não há depoimentos publicados", dom.text)
        for name in ("Alternatintas", "Renata Almeida", "Renato Almeida", "Tintas Almeida"):
            self.assertNotIn(name, dom.text)

    def test_future_public_review_dto_requires_approval_and_consent_and_escapes_text(self):
        # Não cria avaliações ou tabelas: somente renderização futura do template.
        reviews = [
            dict(approved=False, publication_consent=True, quote="UNAPPROVED_SENTINEL", author="A"),
            dict(approved=True, publication_consent=False, quote="UNCONSENTED_SENTINEL", author="B"),
            dict(approved=True, publication_consent=True, quote='<img src=x onerror="window.XSS=1">', author="Synthetic test", company=""),
        ]
        with self.app.test_request_context("/"):
            html = render_template("landing.html", landing_reviews=reviews)
        dom = _LandingDOM(html)
        self.assertEqual(len(dom.elements_with("data-review-slide")), 1)
        self.assertNotIn("UNAPPROVED_SENTINEL", html)
        self.assertNotIn("UNCONSENTED_SENTINEL", html)
        self.assertIn("&lt;img", html)
        self.assertFalse(any(tag == "img" and "onerror" in attrs for tag, attrs in dom.elements))

    def test_review_cards_show_the_rating_as_stars_when_the_dto_brings_it(self):
        # DTO de depoimentos_publicos (frente R): nota 1-5 em estrelas; sem nota, nenhuma estrela.
        reviews = [
            dict(approved=True, publication_consent=True, quote="Com nota", author="Pessoa A", nota=4),
            dict(approved=True, publication_consent=True, quote="Sem nota", author="Pessoa B"),
            dict(approved=True, publication_consent=True, quote="Nota fora da escala", author="Pessoa C", nota=9),
        ]
        with self.app.test_request_context("/"):
            html = render_template("landing.html", landing_reviews=reviews)
        notas = re.findall(r'<p class="review-nota" role="img" aria-label="([^"]+)">(.*?)</p>', html)
        self.assertEqual([rotulo for rotulo, _ in notas], ["Nota 4 de 5"])
        self.assertEqual(notas[0][1].count("<svg"), 5)
        self.assertEqual(notas[0][1].count("is-cheia"), 4)

    def test_demo_values_have_text_alternatives_and_explicit_illustrative_labels(self):
        html, dom = self.landing()
        # O painel do hero saiu (pedido do dono); os gráficos ficam só na Demonstração.
        self.assertNotIn('id="hero-chart"', html)
        for identifier in ("demo-chart-evolucao", "demo-chart-top"):
            self.assertIn(f'id="{identifier}"', html)
            self.assertIn(f'data-chart-fallback-for="{identifier}"', html)
        self.assertGreaterEqual(dom.text.count("Valores ilustrativos"), 3)
        for value in ("142.800", "156.300", "13.500", "28.450", "12.700"):
            self.assertIn(value, dom.text)
        self.assertEqual(sum(tag == "details" and attrs.get("class") == "chart-summary" for tag, attrs in dom.elements), 2)
        # Produtos genéricos de varejo (a landing vale para qualquer nicho).
        for nicho in ("Tinta", "Massa Corrida", "Esmalte", "Selador", "tintas", "pintura"):
            self.assertNotIn(nicho, dom.text)
        self.assertNotIn("tintas", html.split("</head>", 1)[0])

    def test_video_source_and_existing_player_controls_are_retained(self):
        html, dom = self.landing()
        video = next(attrs for tag, attrs in dom.elements if tag == "video")
        for attribute in ("muted", "loop", "playsinline"):
            self.assertIn(attribute, video)
        self.assertEqual(video["preload"], "none")
        self.assertEqual(video["poster"], "/static/video/nexo_demo_poster.jpg")
        self.assertIn('/static/video/nexo_demo_motion.mp4', html)
        for identifier in ("nexo-demo-frame", "nexo-demo-video", "nexo-demo-sound-enable",
                           "nexo-demo-fullscreen", "nexo-demo-error", "nexo-demo-retry",
                           "nexo-demo-pause", "nexo-demo-mute"):
            self.assertIn(f'id="{identifier}"', html)
        response = self.client.head('/static/video/nexo_demo_motion.mp4')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "video/mp4")
        response.close()


_BROWSER = os.environ.get("NEXO_TEST_BROWSER") == "1" and sync_playwright is not None


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale Playwright para validar a landing em navegador.")
class LandingCommercialBrowserTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        from werkzeug.serving import make_server
        for target, original in _CONECTAR_LOCAL.items():
            self.stack.enter_context(patch(target, original))
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._stop_server)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.chromium.launch(executable_path=os.environ.get("NEXO_TEST_CHROMIUM") or None)
        self.addCleanup(self.browser.close)
        self.errors = []

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def page(self, width=1440, *, javascript=True, plotly_stub=None, reduced_motion="reduce"):
        context = self.browser.new_context(viewport={"width": width, "height": 900},
                                           java_script_enabled=javascript, reduced_motion=reduced_motion)
        self.addCleanup(context.close)
        page = context.new_page()
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        def route(request):
            url = request.request.url
            if url.startswith(self.base):
                if plotly_stub is not None and url.rstrip("/") == self.base.rstrip("/"):
                    # SEC-009: o Plotly da landing é fixado por SRI; o dublê que espia
                    # newPlot só entra se o TESTE retirar o atributo da própria página
                    # (o produto continua exigindo o hash do arquivo real).
                    resposta = request.fetch()
                    html = re.sub(r'(<script[^>]*plotly\.min\.js[^>]*?)\s+integrity="[^"]*"', r"\1", resposta.text())
                    request.fulfill(response=resposta, body=html)
                else:
                    request.continue_()
            elif "/bootstrap@5.3.3/dist/css/bootstrap.min.css" in url:
                # A cópia íntegra local, quando existe (sem rede externa); senão, como na CI.
                servir_bootstrap(request)
            elif plotly_stub is not None and "/plotly.js-dist-min@2.35.2/plotly.min.js" in url:
                request.fulfill(status=200, content_type="application/javascript", body=plotly_stub)
            else:
                request.abort()
        page.route("**/*", route)
        page.goto(self.base + "/", wait_until="load")
        return page

    def tearDown(self):
        self.assertEqual(self.errors, [], "Exceções JavaScript na landing")

    def test_all_required_viewports_have_no_horizontal_overflow_and_usable_header(self):
        for width in (320, 375, 390, 430, 768, 1024, 1440):
            with self.subTest(width=width):
                page = self.page(width)
                self.assertTrue(page.locator(".hero h1").is_visible())
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 1)
                header = page.locator(".nexo-header").bounding_box()
                self.assertGreater(header["height"], 0)
                # "Entrar" (login) e "Cadastre-se" visíveis e inteiros dentro da tela em toda largura.
                entrar = page.locator('.header-actions a[href="/auth/login"]')
                cadastre = page.locator(".header-actions [data-cadastro-gatilho]")
                for controle, rotulo in ((entrar, "Entrar"), (cadastre, "Cadastre-se")):
                    self.assertTrue(controle.is_visible())
                    self.assertEqual(controle.inner_text().strip(), rotulo)
                    caixa = controle.bounding_box()
                    self.assertGreaterEqual(caixa["x"], 0)
                    self.assertLessEqual(caixa["x"] + caixa["width"], width + 1)
                    self.assertGreaterEqual(caixa["height"], 44)
                toggle = page.locator("#landing-menu-toggle")
                if width <= 992:
                    self.assertTrue(toggle.is_visible())
                    self.assertFalse(page.locator("#landing-navigation").is_visible())
                    toggle.click()
                    self.assertTrue(page.locator("#landing-navigation").is_visible())
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 1)
                else:
                    self.assertFalse(toggle.is_visible())
                    self.assertTrue(page.locator("#landing-navigation").is_visible())
                page.context.close()

    def test_mobile_menu_keyboard_escape_and_visible_focus_work_without_plotly(self):
        page = self.page(390)
        self.assertTrue(page.evaluate("typeof window.Plotly === 'undefined'"))
        toggle = page.locator("#landing-menu-toggle")
        toggle.focus()
        page.keyboard.press("Enter")
        self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
        page.keyboard.press("Escape")
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
        self.assertEqual(page.evaluate("document.activeElement.id"), "landing-menu-toggle")
        self.assertNotEqual(toggle.evaluate("element => getComputedStyle(element).outlineStyle"), "none")

    def test_skip_link_and_menu_anchor_reach_real_content(self):
        page = self.page(375)
        page.keyboard.press("Tab")
        self.assertEqual(page.evaluate("document.activeElement.getAttribute('href')"), "#conteudo")
        page.keyboard.press("Enter")
        self.assertEqual(page.evaluate("document.activeElement.id"), "conteudo")
        page.locator("#landing-menu-toggle").click()
        page.locator('#landing-navigation a[href="#planos"]').click()
        self.assertTrue(page.url.endswith("#planos"))
        self.assertEqual(page.locator("#landing-menu-toggle").get_attribute("aria-expanded"), "false")

    def test_no_javascript_keeps_navigation_and_chart_data_accessible(self):
        page = self.page(320, javascript=False)
        self.assertTrue(page.locator("#landing-navigation").is_visible())
        self.assertFalse(page.locator("#landing-menu-toggle").is_visible())
        self.assertEqual(page.locator("details.chart-summary").count(), 2)
        details = page.locator("details.chart-summary").first
        details.locator("summary").click()
        self.assertIn("156.300", details.inner_text())
        # Sem JavaScript, o "Cadastre-se" leva aos contatos do rodapé.
        page.locator(".header-actions [data-cadastro-gatilho]").click()
        self.assertTrue(page.url.endswith("#contato"))

    def test_no_graph_cdn_keeps_data_and_empty_review_state_honest(self):
        page = self.page(390)
        self.assertEqual(page.locator('[data-chart-ready="true"]').count(), 0)
        self.assertEqual(page.locator('[data-chart-fallback-for]:visible').count(), 2)
        self.assertEqual(page.locator('[data-review-slide]').count(), 0)
        self.assertTrue(page.locator('[data-review-empty]').is_visible())
        self.assertFalse(page.locator('[data-review-controls]').is_visible())
        self.assertEqual(page.get_by_role('link', name='Deixar Avaliação', exact=True).get_attribute('href'),
                         '/avaliar')

    def test_approved_review_component_supports_navigation_and_indicators_without_fake_published_content(self):
        page = self.page(390)
        page.evaluate("""() => {
            const root = document.querySelector('[data-review-carousel]');
            const slots = root.querySelector('[data-review-slides]');
            for (let index = 1; index <= 2; index++) {
                const slide = document.createElement('article');
                slide.dataset.reviewSlide = '';
                slide.textContent = 'Conteúdo somente do teste ' + index;
                slots.appendChild(slide);
            }
            window.NexoLanding.initReviews(root);
        }""")
        root = page.locator('[data-review-carousel]')
        self.assertEqual(root.locator('[data-review-slide]:visible').count(), 1)
        self.assertEqual(root.locator('[data-review-indicators] button').count(), 2)
        root.locator('[data-review-next]').focus()
        page.keyboard.press('Enter')
        self.assertIn('teste 2', root.locator('[data-review-slide]:visible').inner_text())
        self.assertEqual(root.locator('[data-review-indicators] button[aria-current=true]').inner_text(), '2')
        root.locator('[data-review-prev]').click()
        self.assertIn('teste 1', root.locator('[data-review-slide]:visible').inner_text())
        root.locator('[data-review-indicators] button').nth(1).click()
        self.assertIn('teste 2', root.locator('[data-review-slide]:visible').inner_text())
        self.assertIn('2 de 2', root.locator('[data-review-status]').text_content())

    def test_chart_controller_renders_the_two_demo_datasets_when_library_loads(self):
        stub = """window.__charts=[];window.Plotly={newPlot:function(el,data,layout,config){
            window.__charts.push({id:el.id,data:data,separators:layout.separators});return Promise.resolve(el);
        },relayout:function(){return Promise.resolve();}};"""
        page = self.page(plotly_stub=stub)
        page.wait_for_function("() => document.querySelectorAll('[data-chart-ready=true]').length === 2")
        values = page.evaluate("window.__charts")
        self.assertEqual([item['id'] for item in values], ['demo-chart-evolucao', 'demo-chart-top'])
        self.assertEqual(values[0]['data'][0]['y'], [138400, 124900, 149600, 152300, 156300])
        self.assertEqual(values[0]['data'][1]['y'], [132100, 119800, 138200, 141600, 142800])
        self.assertEqual(values[1]['data'][0]['x'], [28450, 24800, 19200, 16500, 12700])
        # Números no formato brasileiro (R$ 10.000) e produtos genéricos de varejo.
        self.assertEqual({item['separators'] for item in values}, {',.'})
        self.assertNotIn('Tinta', ' '.join(values[1]['data'][0]['y']))
        self.assertEqual(page.locator('[data-chart-fallback-for]:visible').count(), 0)

    def test_secondary_text_tokens_meet_normal_text_contrast(self):
        page = self.page()
        values = page.evaluate("""() => {
            const style=getComputedStyle(document.documentElement);
            function rgb(value) { let s=value.trim().slice(1); return [0,2,4].map(i=>parseInt(s.slice(i,i+2),16)/255); }
            function light(value) {const a=rgb(value).map(c=>c<=0.04045?c/12.92:Math.pow((c+0.055)/1.055,2.4));return a[0]*0.2126+a[1]*0.7152+a[2]*0.0722;}
            const fg=light(style.getPropertyValue('--nexo-text-3'));
            return ['--nexo-bg','--nexo-surface','--nexo-surface-2'].map(name=>{
                const bg=light(style.getPropertyValue(name));return (Math.max(fg,bg)+0.05)/(Math.min(fg,bg)+0.05);
            });
        }""")
        self.assertTrue(all(value >= 4.5 for value in values), values)


if __name__ == "__main__":
    unittest.main()
