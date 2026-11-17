"""Identidade visual: o cubo NEXO em PNG transparente substitui a marca antiga.

A imagem com o nome embutido (nexo-logo.jpeg) e o monograma "N" da landing
saem; o nome NEXO aparece uma única vez, como texto, em cada assinatura.
HTTP/DOM usa a aplicação e as migrações isoladas; Playwright é habilitado por
NEXO_TEST_BROWSER=1, como na CI. Nenhuma conexão de produção.
"""

from html.parser import HTMLParser
import os
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch

from flask import render_template
from PIL import Image

from tests.deploy_support import DeployTestCase, ROOT
from tests.landing_support import servir_bootstrap
from tests.test_nexobot_navegador import _CONECTAR_LOCAL

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


CUBO_BRILHO = "img/nexo-cubo-brilho.png"
CUBO_SEM_BRILHO = "img/nexo-cubo-transparente.png"
STATIC = ROOT / "static"


class _Assinatura(HTMLParser):
    """Imagens e texto visível de um trecho de HTML."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.imagens, self.textos = [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.imagens.append(dict(attrs))

    def handle_data(self, data):
        if data.strip():
            self.textos.append(data.strip())

    @property
    def texto(self):
        return " ".join(self.textos)


def _trecho(html, inicio, fim):
    pedaco = html.split(inicio, 1)[1]
    return inicio + pedaco.split(fim, 1)[0]


class IdentidadeVisualArquivosTests(unittest.TestCase):
    def test_cubos_sao_png_com_transparencia_real_e_versoes_distintas(self):
        semitransparencia = {}
        for nome in (CUBO_BRILHO, CUBO_SEM_BRILHO):
            with Image.open(STATIC / nome) as imagem:
                self.assertEqual(imagem.format, "PNG", nome)
                self.assertEqual(imagem.mode, "RGBA", nome)
                alfa = imagem.getchannel("A")
                largura, altura = imagem.size
                # Fundo transparente de verdade: cantos sem cobertura e cubo opaco no centro.
                for canto in ((0, 0), (largura - 1, 0), (0, altura - 1), (largura - 1, altura - 1)):
                    self.assertEqual(alfa.getpixel(canto), 0, f"{nome}: canto {canto} não é transparente")
                histograma = alfa.histogram()
                self.assertGreater(histograma[0], sum(histograma) * 0.4, f"{nome}: pouca área transparente")
                self.assertGreater(histograma[255], 0, f"{nome}: sem traço opaco")
                semitransparencia[nome] = sum(histograma[1:255])
        # O halo luminoso é feito de pixels semitransparentes.
        self.assertGreater(semitransparencia[CUBO_BRILHO], semitransparencia[CUBO_SEM_BRILHO] * 1.5)

    def test_marca_antiga_nao_existe_nem_e_referenciada(self):
        self.assertFalse((STATIC / "img" / "nexo-logo.jpeg").exists())
        for pasta, padrao in (("templates", "*.html"), ("static/css", "*.css"), ("static/js", "*.js")):
            for arquivo in (ROOT / pasta).rglob(padrao):
                conteudo = arquivo.read_text(encoding="utf-8")
                self.assertNotIn("nexo-logo", conteudo, str(arquivo))
                self.assertNotIn("brand-mark", conteudo, str(arquivo))

    def test_contenedores_do_simbolo_nao_pintam_fundo_nem_recortam(self):
        # Toda regra (inclusive em grupo, pseudoelemento ou media query) que
        # alcance o símbolo não pode desenhar fundo, moldura ou recortar o PNG.
        classes = re.compile(r"\.(?:auth-logo|brand-logo|brand-symbol)(?![\w-])")
        proibidos = re.compile(r"(?:^|;)\s*(background|box-shadow|border|outline|overflow|filter|clip-path|mask)",
                               re.I)
        for nome in ("nexo.css", "landing.css"):
            folha = re.sub(r"/\*.*?\*/", "", (STATIC / "css" / nome).read_text(encoding="utf-8"), flags=re.S)
            for seletores, regra in re.findall(r"([^{}]+)\{([^{}]*)\}", folha):
                if classes.search(seletores):
                    self.assertIsNone(proibidos.search(regra), f"{nome}: {seletores.strip()} {{{regra.strip()}}}")


class IdentidadeVisualHTTPTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        from models import Analise, IndicadorAnalise, RelatorioAnalise
        self.fixtures()
        # Análise sintética publicada, para a tela de análise do cliente abrir.
        analise = self.db.session.get(Analise, self.analise_id)
        analise.status_analise = "CONCLUIDO"
        analise.indicador = IndicadorAnalise(faturamento_total=25, total_comprado=40, saldo_estimado_compras_vendas=15)
        analise.relatorio = RelatorioAnalise(titulo="Análise sintética", conclusao_estrategica="Conclusão sintética.",
                                             publicado=True)
        self.db.session.commit()
        self.empresa_id = analise.id_empresa

    def pagina(self, caminho):
        resposta = self.client.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        return resposta.get_data(as_text=True)

    def assert_assinatura(self, html, arquivo):
        assinatura = _Assinatura(html)
        self.assertEqual(len(assinatura.imagens), 1, html)
        imagem = assinatura.imagens[0]
        self.assertEqual(imagem["src"], f"/static/{arquivo}")
        # Decorativa: o nome já está em texto ao lado, sem leitura duplicada.
        self.assertEqual(imagem["alt"], "")
        self.assertEqual((imagem["width"], imagem["height"]), ("253", "279"))
        self.assertEqual(re.findall(r"\bNEXO\b", assinatura.texto), ["NEXO"], assinatura.texto)
        self.assertEqual(assinatura.texto.count("Faturamento Inteligente"), 1, assinatura.texto)

    def assert_auth(self, html):
        self.assert_assinatura(_trecho(html, '<div class="auth-brand"', "<h1"), CUBO_BRILHO)

    def test_arquivos_estaticos_sao_servidos_como_png(self):
        for arquivo in (CUBO_BRILHO, CUBO_SEM_BRILHO):
            resposta = self.client.get(f"/static/{arquivo}")
            self.assertEqual(resposta.status_code, 200)
            self.assertEqual(resposta.mimetype, "image/png")
            self.assertTrue(resposta.data.startswith(b"\x89PNG\r\n\x1a\n"))
            resposta.close()

    def test_landing_usa_o_cubo_no_cabecalho_e_no_rodape(self):
        html = self.pagina("/")
        for inicio, fim in (("<header", "</header>"), ("<footer", "</footer>")):
            regiao = _trecho(html, inicio, fim)
            marca = _trecho(regiao, '<a href="#inicio" class="brand"', "</a>")
            self.assertIn('aria-label="NEXO Faturamento Inteligente, início"', marca)
            self.assertIn('class="brand-symbol"', marca)
            self.assert_assinatura(marca, CUBO_SEM_BRILHO)
        self.assertNotIn('aria-hidden="true">N<', html)

    def test_paginas_publicas_de_acesso_usam_o_cubo_com_brilho(self):
        for caminho in ("/auth/login", "/auth/recuperar-senha", "/como-aderir"):
            with self.subTest(caminho=caminho):
                self.assert_auth(self.pagina(caminho))

    def test_paginas_de_senha_por_token_e_primeiro_acesso_usam_o_cubo_com_brilho(self):
        with self.app.test_request_context("/"):
            paginas = (
                render_template("auth/resetar_senha.html", token="token-sintetico"),
                render_template("auth/primeiro_acesso_token.html", token="token-sintetico", usuario=None),
                render_template("auth/primeiro_acesso.html"),
                render_template("auth/conta_encerrada.html"),
            )
        for html in paginas:
            self.assert_auth(html)

    def test_sidebar_do_admin_e_do_cliente_usam_o_cubo_sem_brilho(self):
        for email, caminhos in (
            ("admin-test@example.invalid", ("/admin/dashboard", "/admin/guia", "/admin/contratos",
                                            f"/admin/empresas/{self.empresa_id}/detalhes")),
            ("cliente-test@example.invalid", ("/cliente/dashboard", "/cliente/historico",
                                              f"/cliente/analise/{self.analise_id}", "/cliente/upload",
                                              "/cliente/guia", "/cliente/tickets")),
        ):
            self.client = self.app.test_client()
            self.login(email)
            for caminho in caminhos:
                with self.subTest(caminho=caminho):
                    html = self.pagina(caminho)
                    self.assert_assinatura(_trecho(html, '<div class="sidebar-brand">', '<nav class="sidebar-nav">'),
                                           CUBO_SEM_BRILHO)


_BROWSER = os.environ.get("NEXO_TEST_BROWSER") == "1" and sync_playwright is not None


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale Playwright para validar a marca em navegador.")
class IdentidadeVisualBrowserTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        from werkzeug.serving import make_server
        for alvo, original in _CONECTAR_LOCAL.items():
            self.stack.enter_context(patch(alvo, original))
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._parar_servidor)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.chromium.launch(executable_path=os.environ.get("NEXO_TEST_CHROMIUM") or None)
        self.addCleanup(self.browser.close)

    def _parar_servidor(self):
        self.server.shutdown()
        self.server.server_close()

    def abrir(self, caminho, largura, email=None, altura=900):
        contexto = self.browser.new_context(viewport={"width": largura, "height": altura}, reduced_motion="reduce")
        self.addCleanup(contexto.close)
        pagina = contexto.new_page()

        def rota(requisicao):
            url = requisicao.request.url
            if url.startswith(self.base):
                requisicao.continue_()
            elif "/bootstrap@5.3.3/dist/css/bootstrap.min.css" in url:
                # A cópia íntegra local, quando existe (sem rede externa); senão, como na CI.
                servir_bootstrap(requisicao)
            else:
                requisicao.abort()
        pagina.route("**/*", rota)
        if email:
            pagina.goto(self.base + "/auth/login", wait_until="load")
            pagina.fill("#email", email)
            pagina.fill("#senha", self.password)
            with pagina.expect_navigation():
                pagina.click("button[type=submit]")
        pagina.goto(self.base + caminho, wait_until="load")
        return pagina

    def medir(self, pagina, seletor):
        return pagina.locator(seletor).evaluate("""img => {
            const caixa = img.getBoundingClientRect();
            const estilo = getComputedStyle(img);
            const pai = getComputedStyle(img.parentElement);
            const pseudo = ["::before", "::after"].map(p => getComputedStyle(img.parentElement, p).content);
            return {carregada: img.complete && img.naturalWidth === 253 && img.naturalHeight === 279,
                    altura: caixa.height, largura: caixa.width,
                    img: [estilo.backgroundColor, estilo.backgroundImage, estilo.boxShadow, estilo.filter],
                    pai: [pai.backgroundColor, pai.backgroundImage, pai.boxShadow, pai.borderRadius, pai.overflow],
                    pseudo,
                    estouro: document.documentElement.scrollWidth - document.documentElement.clientWidth};
        }""")

    def assert_transparente(self, medida, pai_proprio=True):
        self.assertTrue(medida["carregada"], medida)
        self.assertEqual(medida["img"], ["rgba(0, 0, 0, 0)", "none", "none", "none"], medida)
        if pai_proprio:
            # Contêiner exclusivo do símbolo: nenhum fundo, moldura, recorte ou ornamento.
            self.assertEqual(medida["pai"], ["rgba(0, 0, 0, 0)", "none", "none", "0px", "visible"], medida)
            self.assertEqual(medida["pseudo"], ["none", "none"], medida)
        self.assertLessEqual(medida["estouro"], 0, medida)

    def test_landing_cabecalho_e_rodape_do_celular_ao_desktop(self):
        for largura, altura in ((1440, 40), (1024, 40), (768, 40), (375, 34), (320, 34)):
            pagina = self.abrir("/", largura)
            for seletor in (".nexo-header .brand-symbol", ".nexo-footer .brand-symbol"):
                with self.subTest(largura=largura, seletor=seletor):
                    medida = self.medir(pagina, seletor)
                    # O pai é o link da marca, que também contém o nome em texto.
                    self.assert_transparente(medida, pai_proprio=False)
                    self.assertEqual(medida["pai"][:3], ["rgba(0, 0, 0, 0)", "none", "none"], medida)
                    self.assertAlmostEqual(medida["altura"], altura, delta=0.5)
                    self.assertAlmostEqual(medida["largura"], altura * 253 / 279, delta=1)

    def test_telas_de_acesso_mostram_o_cubo_inteiro_dentro_do_cartao(self):
        for caminho, largura, altura in (("/auth/login", 1440, 104), ("/auth/login", 1024, 104),
                                         ("/auth/login", 768, 104), ("/auth/login", 375, 88),
                                         ("/auth/login", 320, 88), ("/auth/recuperar-senha", 1440, 104),
                                         ("/auth/recuperar-senha", 320, 88), ("/como-aderir", 1024, 104)):
            with self.subTest(caminho=caminho, largura=largura):
                pagina = self.abrir(caminho, largura)
                medida = self.medir(pagina, ".auth-logo img")
                self.assert_transparente(medida)
                self.assertAlmostEqual(medida["altura"], altura, delta=0.5)
                cubo = pagina.locator(".auth-logo img").bounding_box()
                cartao = pagina.locator(".auth-card").bounding_box()
                self.assertGreaterEqual(cubo["x"], cartao["x"])
                self.assertGreaterEqual(cubo["y"], cartao["y"])
                self.assertLessEqual(cubo["x"] + cubo["width"], cartao["x"] + cartao["width"])
                # Centralizado no cartão.
                self.assertAlmostEqual(cubo["x"] + cubo["width"] / 2, cartao["x"] + cartao["width"] / 2, delta=1)

    def test_sidebar_do_admin_e_do_cliente(self):
        for email, caminho, largura in (("admin-test@example.invalid", "/admin/dashboard", 1440),
                                        ("admin-test@example.invalid", "/admin/dashboard", 1024),
                                        ("cliente-test@example.invalid", "/cliente/dashboard", 1440),
                                        ("cliente-test@example.invalid", "/cliente/historico", 1280)):
            with self.subTest(email=email, caminho=caminho, largura=largura):
                pagina = self.abrir(caminho, largura, email=email)
                medida = self.medir(pagina, ".sidebar-brand .brand-logo img")
                self.assert_transparente(medida)
                self.assertAlmostEqual(medida["altura"], 42, delta=0.5)
                self.assertTrue(pagina.locator(".sidebar-brand .brand-text").is_visible())
                cubo = pagina.locator(".sidebar-brand .brand-logo img").bounding_box()
                texto = pagina.locator(".sidebar-brand .brand-text").bounding_box()
                self.assertLess(cubo["x"] + cubo["width"], texto["x"])
                self.assertAlmostEqual(cubo["y"] + cubo["height"] / 2, texto["y"] + texto["height"] / 2, delta=1.5)

if __name__ == "__main__":
    unittest.main()
