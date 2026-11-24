"""Gestão contratual em navegador real (Chromium via Playwright).

Escopo: o que só o navegador mostra, nas larguras 1440 (desktop), 1280
(notebook), 768 (tablet) e 375 (celular):
  1. "Conta encerrada" após a senha correta de empresa CANCELADA (e no
     instante-limite exclusivo de uma suspensão): título e cubo com brilho
     visíveis e carregados, sem transbordamento horizontal, sem texto
     privado, sem link para /cliente e sem sessão autenticada;
  2. painel do cliente suspenso no regime contratual, dentro do prazo de PDF:
     faixa de suspensão com as datas calculadas pela política;
  3. página da análise no instante-limite do PDF e depois dele: aviso visível
     e nenhum botão "Exportar PDF" (antes do limite, o PDF é baixado);
  4. ADMIN: detalhes da empresa (contrato, coberturas e histórico), formulários
     de novo contrato e de prorrogação e /admin/contratos, sem transbordamento
     da página (tabelas largas rolam dentro de .table-responsive-x) e com o
     cubo sem brilho na sidebar;
  5. fluxos reais: o ADMIN preenche e envia o formulário de período pago e o de
     prorrogação (datetime-local lido no fuso America/Sao_Paulo, com o
     navegador em outro fuso) e vê a mensagem de sucesso.

As regras de banco já cobertas em tests/test_contratos_gestao.py não são
repetidas: os cenários são montados em Python (ContratosCase, migrações reais
em SQLite) antes de abrir o navegador, e o relógio da política é injetado em
app.config["NEXO_RELOGIO"]; nenhum prazo depende de espera real.

Rede: somente o servidor local (127.0.0.1). Folhas de estilo e scripts de CDN
são respondidos localmente, sem rede: por padrão o Bootstrap fica ausente
(scripts e fontes vazios; no lugar do bootstrap.min.css, apenas as regras de
visibilidade de que o layout depende, como o menu suspenso fechado). Se
NEXO_TEST_CDN_DIR apontar para cópias íntegras (hash SRI conferido) de
bootstrap.min.css / bootstrap.bundle.min.js 5.3.3, elas são servidas para o
layout real. Nenhuma captura de tela é gravada. Habilitado por
NEXO_TEST_BROWSER=1, como na CI; dados fictícios (example.invalid).
"""

import base64
from datetime import date, datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from tests.contratos_support import T0, TEXTO_PRIVADO, ContratosCase
from tests.test_nexobot_navegador import _CONECTAR_LOCAL

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # dependência opcional de desenvolvimento
    sync_playwright = None


_BROWSER = os.environ.get("NEXO_TEST_BROWSER") == "1" and sync_playwright is not None

LARGURAS = (("desktop", 1440, 900), ("notebook", 1280, 800), ("tablet", 768, 1024), ("celular", 375, 812))
LARGURA_SIDEBAR_FIXA = 992  # abaixo disso a sidebar fica fora da tela até abrir o menu

CLIENTE = "cliente-test@example.invalid"
ADMIN = "admin-test@example.invalid"
CUBO_BRILHO = "/static/img/nexo-cubo-brilho.png"
CUBO_SEM_BRILHO = "/static/img/nexo-cubo-transparente.png"

# Oráculo independente da implementação (regras propostas): suspensão em T0 =
# 02/03/2026 10:00 em São Paulo; 3 meses de calendário e 14 dias corridos.
LIMITE_SUSPENSAO = datetime(2026, 6, 2, 13, 0, tzinfo=timezone.utc)
LIMITE_PDF = datetime(2026, 3, 16, 13, 0, tzinfo=timezone.utc)
TEXTO_LIMITE_SUSPENSAO = "02/06/2026 às 10:00"
TEXTO_LIMITE_PDF = "16/03/2026 às 10:00"
AVISO_PDF_ENCERRADO = "Seu prazo para exportação de relatórios foi encerrado"
# Dados da empresa que não podem aparecer na página de conta encerrada.
DADOS_PRIVADOS = (TEXTO_PRIVADO, "Empresa fictícia 1", "00000000000001", "Teste 1", "Cliente fictício")

_CDN = "https://cdn.jsdelivr.net/npm/"
_CDN_INTEGRO = {
    _CDN + "bootstrap@5.3.3/dist/css/bootstrap.min.css": (
        "bootstrap.min.css", "text/css",
        "QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH"),
    _CDN + "bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js": (
        "bootstrap.bundle.min.js", "application/javascript",
        "YvpcrYf0tY3lHB60NNkmXc5s9fDVZLESaAA55NDzOxhy9GkcIdslK1eN7N6jIeHz"),
    # SEC-009: os templates fixam SRI em todo recurso de CDN; uma cópia que não
    # seja byte a byte idêntica é bloqueada pelo navegador (erro no console).
    _CDN + "bootstrap-icons@1.11.3/font/bootstrap-icons.min.css": (
        "bootstrap-icons.min.css", "text/css",
        "XGjxtQfXaH2tnPFa9x+ruJTuLE3Aa6LhHSWRr1XeTyhezb4abCG4ccI5AkVDxqC+"),
    _CDN + "plotly.js-dist-min@2.35.2/plotly.min.js": (
        "plotly.min.js", "application/javascript",
        "cCVCZkAjYNxaYKbM8lsArLznDF/SvMFr1jcZrvOpSTCa0W40ZAdLzHCEulnUa5i7"),
}


# Sem a cópia íntegra, o lugar do bootstrap.min.css é ocupado só pelas regras
# de visibilidade do Bootstrap 5.3.3 de que estas páginas dependem; sem elas o
# menu de notificações apareceria aberto no topo e criaria um falso estouro.
_BOOTSTRAP_MINIMO = (
    ".dropdown-menu{position:absolute;display:none}.dropdown-menu.show{display:block}"
    ".visually-hidden{position:absolute!important;width:1px!important;height:1px!important;"
    "padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;"
    "white-space:nowrap!important;border:0!important}"
)


def _copias_locais_da_cdn():
    """Cópias locais opcionais, aceitas somente com o hash SRI oficial."""
    pasta = os.environ.get("NEXO_TEST_CDN_DIR")
    copias = {}
    if not pasta:
        return copias
    for url, (nome, tipo, sri) in _CDN_INTEGRO.items():
        arquivo = Path(pasta) / nome
        if arquivo.is_file():
            resumo = base64.b64encode(hashlib.sha384(arquivo.read_bytes()).digest()).decode()
            if resumo == sri:
                copias[url] = (str(arquivo), tipo)
    return copias


# Fontes que o bootstrap-icons.min.css íntegro carrega por URL com query string
# (sem SRI possível). Cópia local quando existir em NEXO_TEST_CDN_DIR; sem ela,
# corpo vazio: o Chromium só avisa que não decodificou a fonte (não é erro).
_FONTES_CDN = {
    _CDN + "bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2": ("bootstrap-icons.woff2", "font/woff2"),
    _CDN + "bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff": ("bootstrap-icons.woff", "font/woff"),
}


def _servir_fonte(rota, url):
    """Responde a uma fonte conhecida da CDN (cópia local ou vazia); outras são abortadas."""
    conhecida = _FONTES_CDN.get(url.split("?", 1)[0])
    if conhecida is None:
        rota.abort()
        return
    nome, tipo = conhecida
    pasta = os.environ.get("NEXO_TEST_CDN_DIR")
    arquivo = Path(pasta) / nome if pasta else None
    if arquivo is not None and arquivo.is_file():
        rota.fulfill(status=200, path=str(arquivo), content_type=tipo)
    else:
        rota.fulfill(status=200, content_type=tipo, body=b"")


_MEDIR_LAYOUT = """() => {
    const largura = window.innerWidth;
    const raiz = document.documentElement;
    const descrever = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
        + (el.classList.length ? '.' + [...el.classList].join('.') : '');
    const recortado = el => {
        for (let pai = el.parentElement; pai && pai !== document.body; pai = pai.parentElement) {
            if (getComputedStyle(pai).overflowX !== 'visible') return true;
        }
        return false;
    };
    const fora = [];
    for (const el of document.body.querySelectorAll('*')) {
        if (el.closest('#sidebar, .sidebar-overlay, script, style, template')) continue;
        const estilo = getComputedStyle(el);
        if (estilo.display === 'none' || estilo.visibility === 'hidden' || estilo.position === 'fixed') continue;
        const caixa = el.getBoundingClientRect();
        if (!caixa.width || !caixa.height || recortado(el)) continue;
        if (caixa.left < -1 || caixa.right > largura + 1) {
            fora.push(descrever(el) + ' ' + Math.round(caixa.left) + '..' + Math.round(caixa.right));
        }
    }
    const tabelas = [...document.querySelectorAll('.table-responsive-x')].map(t => {
        const caixa = t.getBoundingClientRect();
        return {esquerda: caixa.left, direita: caixa.right, overflowX: getComputedStyle(t).overflowX,
                rola_dentro: t.scrollWidth > t.clientWidth};
    });
    return {scrollWidth: raiz.scrollWidth, innerWidth: largura, fora: fora.slice(0, 12), tabelas};
}"""

_MEDIR_IMAGEM = """img => {
    const caixa = img.getBoundingClientRect();
    const estilo = getComputedStyle(img);
    return {src: img.getAttribute('src'), completa: img.complete, natural: img.naturalWidth,
            esquerda: caixa.left, direita: caixa.right, topo: caixa.top, base: caixa.bottom,
            largura: caixa.width, altura: caixa.height,
            visivel: estilo.visibility !== 'hidden' && estilo.display !== 'none' && Number(estilo.opacity) > 0};
}"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class ContratosNavegadorTests(ContratosCase):
    def setUp(self):
        super().setUp()
        from werkzeug.serving import make_server
        # A rede de teste bloqueia conexões; o servidor local precisa delas (só 127.0.0.1).
        for alvo, original in _CONECTAR_LOCAL.items():
            self.stack.enter_context(patch(alvo, original))
        self.servidor = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.servidor.server_port}"
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.addCleanup(self._parar_servidor)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.navegador = self.pw.chromium.launch(executable_path=os.environ.get("NEXO_TEST_CHROMIUM") or None)
        self.addCleanup(self.navegador.close)
        self.cdn_local = _copias_locais_da_cdn()
        self.erros_js = []
        if len(self.cdn_local) < len(_CDN_INTEGRO):
            # SEC-009: um recurso fixado por SRI sem cópia íntegra local seria
            # bloqueado pelo navegador (erro no console), não substituído.
            self.skipTest("Cópias íntegras da CDN incompletas (NEXO_TEST_CDN_DIR)")

    def _parar_servidor(self):
        self.servidor.shutdown()
        self.servidor.server_close()

    def tearDown(self):
        self.assertEqual(self.erros_js, [], "Erros de JavaScript no console")

    # ------------------------------------------------------------ navegador
    def _rota(self, rota):
        requisicao = rota.request
        url = requisicao.url
        if url.startswith(self.base + "/"):
            rota.continue_()
        elif url in self.cdn_local:
            caminho, tipo = self.cdn_local[url]
            rota.fulfill(status=200, path=caminho, content_type=tipo)
        elif requisicao.resource_type == "font":
            _servir_fonte(rota, url)
        elif url == _CDN + "bootstrap@5.3.3/dist/css/bootstrap.min.css":
            rota.fulfill(status=200, content_type="text/css", body=_BOOTSTRAP_MINIMO)
        elif requisicao.resource_type == "stylesheet":
            rota.fulfill(status=200, content_type="text/css", body="")
        elif requisicao.resource_type == "script":
            rota.fulfill(status=200, content_type="application/javascript", body="")
        else:
            rota.abort()

    def abrir(self, largura, altura, fuso="America/Sao_Paulo"):
        contexto = self.navegador.new_context(viewport={"width": largura, "height": altura},
                                              reduced_motion="reduce", locale="pt-BR", timezone_id=fuso)
        self.addCleanup(contexto.close)
        pagina = contexto.new_page()
        pagina.on("pageerror", lambda erro: self.erros_js.append(f"{largura}px: {erro}"))
        pagina.on("console", lambda msg: self.erros_js.append(f"{largura}px console: {msg.text}")
                  if msg.type == "error" else None)
        pagina.route("**/*", self._rota)
        return pagina

    def entrar(self, pagina, email):
        """Login pelo formulário real; devolve a resposta final da navegação."""
        pagina.goto(self.base + "/auth/login", wait_until="load")
        pagina.fill("#email", email)
        pagina.fill("#senha", self.password)
        with pagina.expect_navigation(wait_until="load") as navegacao:
            pagina.click("button[type=submit]")
        return navegacao.value

    def ir(self, pagina, caminho, status=200):
        resposta = pagina.goto(self.base + caminho, wait_until="load")
        self.assertEqual(resposta.status, status, caminho)
        return resposta

    # ------------------------------------------------------------ medições
    def assert_sem_transbordamento(self, pagina, largura):
        medida = pagina.evaluate(_MEDIR_LAYOUT)
        self.assertEqual(medida["innerWidth"], largura, medida)
        self.assertLessEqual(medida["scrollWidth"], medida["innerWidth"], medida)
        for tabela in medida["tabelas"]:
            # Tabela larga rola dentro do próprio contêiner, que cabe na tela.
            self.assertGreaterEqual(tabela["esquerda"], -0.5, medida)
            self.assertLessEqual(tabela["direita"], largura + 0.5, medida)
            if tabela["rola_dentro"]:
                self.assertIn(tabela["overflowX"], ("auto", "scroll"), medida)
        return medida

    def medir_imagem(self, pagina, seletor):
        return pagina.locator(seletor).evaluate(_MEDIR_IMAGEM)

    def assert_dentro_da_tela(self, caixa, largura, altura, contexto):
        self.assertGreaterEqual(caixa["esquerda"], -0.5, contexto)
        self.assertLessEqual(caixa["direita"], largura + 0.5, contexto)
        self.assertGreaterEqual(caixa["topo"], -0.5, contexto)
        self.assertLessEqual(caixa["base"], altura + 0.5, contexto)

    def assert_cubo_sidebar(self, pagina, largura, altura):
        """Cubo SEM brilho carregado; abaixo de 992px, visível ao abrir o menu."""
        seletor = ".sidebar-brand .brand-logo img"
        self.assertEqual(pagina.locator("#sidebar img").count(), 1)
        medida = self.medir_imagem(pagina, seletor)
        self.assertEqual(medida["src"], CUBO_SEM_BRILHO, medida)
        self.assertTrue(medida["completa"] and medida["natural"] > 0, medida)
        if largura < LARGURA_SIDEBAR_FIXA:
            self.assertLessEqual(medida["direita"], 0.5, f"sidebar deveria estar recolhida: {medida}")
            pagina.click("#navToggle")
            pagina.wait_for_function(
                "() => Math.abs(document.getElementById('sidebar').getBoundingClientRect().left) < 0.5")
            medida = self.medir_imagem(pagina, seletor)
        self.assertTrue(medida["visivel"] and medida["altura"] > 0 and medida["largura"] > 0, medida)
        self.assert_dentro_da_tela(medida, largura, altura, medida)
        self.assertTrue(pagina.locator(".sidebar-brand .brand-text").is_visible())
        self.assert_sem_transbordamento(pagina, largura)

    def assert_sem_erros_js(self, contexto):
        self.assertEqual(self.erros_js, [], contexto)

    def descartar_403_esperado(self):
        """O Chromium registra no console o documento 403 que o teste pediu de
        propósito; é falha de recurso esperada, não erro de script."""
        esperado = "console: Failed to load resource: the server responded with a status of 403 (FORBIDDEN)"
        restantes = [e for e in self.erros_js if not e.endswith(esperado)]
        self.assertEqual(len(self.erros_js) - len(restantes), 1, self.erros_js)
        self.erros_js = restantes

    def sessao_do_navegador(self, pagina):
        """Conteúdo do cookie de sessão do Flask guardado pelo navegador."""
        valores = [c["value"] for c in pagina.context.cookies(self.base) if c["name"] == "session"]
        if not valores:
            return {}
        return dict(self.app.session_interface.get_signing_serializer(self.app).loads(valores[0]))

    def texto(self, pagina, seletor):
        """Texto do DOM (sem text-transform do CSS), com espaços normalizados."""
        return " ".join(pagina.locator(seletor).text_content().split())

    def dados_contrato(self, pagina):
        """Pares <dt>/<dd> do quadro "Situação contratual" dos detalhes da empresa."""
        return dict(pagina.locator(".dados-contrato.mb-3").evaluate("""dl => [...dl.querySelectorAll(':scope > div')]
            .map(d => [d.querySelector('dt').textContent.trim(),
                       d.querySelector('dd').textContent.replace(/\\s+/g, ' ').trim()])"""))

    # ------------------------------------------------------------ cenários
    def cenario_suspenso(self):
        """Contrato com janeiro/2026 pago, análise de janeiro publicada, regras
        ativas e suspensão registrada em T0."""
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.publicar(self.analise_id)
        self.ativar_regras()
        contrato = self.suspender()
        self.assertEqual((contrato.suspensao_limite, contrato.pdf_limite), (LIMITE_SUSPENSAO, LIMITE_PDF))
        return contrato

    def cancelar(self, motivo="Cancelamento fictício"):
        import contract_admin
        contrato = self.vigente()
        contract_admin.cancelar_contrato(self.db.session, self.empresa_id, contrato.id_contrato,
                                         contrato.versao, motivo=motivo, responsavel=self.admin,
                                         instante=self.agora)
        self.db.session.commit()

    def assert_conta_encerrada(self, pagina, resposta, largura, altura):
        rotulo = f"{largura}px"
        # Resposta direta ao POST: 200, sem redirect e sem cache.
        self.assertEqual(resposta.status, 200, rotulo)
        self.assertEqual(resposta.request.method, "POST", rotulo)
        self.assertIsNone(resposta.request.redirected_from, rotulo)
        self.assertTrue(pagina.url.endswith("/auth/login"), pagina.url)
        self.assertIn("no-store", resposta.headers.get("cache-control", ""), rotulo)

        titulo = pagina.locator("h1.auth-title")
        self.assertEqual(titulo.inner_text().strip(), "Conta encerrada", rotulo)
        self.assertTrue(titulo.is_visible(), rotulo)
        caixa = titulo.bounding_box()
        self.assert_dentro_da_tela({"esquerda": caixa["x"], "direita": caixa["x"] + caixa["width"],
                                    "topo": caixa["y"], "base": caixa["y"] + caixa["height"]},
                                   largura, altura, rotulo)

        cubo = self.medir_imagem(pagina, ".auth-logo img")
        self.assertEqual(cubo["src"], CUBO_BRILHO, cubo)
        self.assertTrue(cubo["completa"] and cubo["natural"] > 0, cubo)
        self.assertTrue(cubo["visivel"] and cubo["altura"] > 0, cubo)
        self.assert_dentro_da_tela(cubo, largura, altura, cubo)
        self.assert_sem_transbordamento(pagina, largura)

        html = pagina.content()
        for privado in DADOS_PRIVADOS:
            self.assertNotIn(privado, html, rotulo)
        destinos = pagina.eval_on_selector_all("a[href], form[action]",
                                               "els => els.map(e => e.href || e.action)")
        self.assertTrue(destinos, rotulo)
        self.assertEqual([d for d in destinos if "/cliente" in d], [], rotulo)
        # Sem flash e sem a mensagem genérica de conta desativada.
        self.assertEqual(pagina.locator(".alert, .flash-stack").count(), 0, rotulo)
        self.assertNotIn("desativada", html)
        self.assertNotIn("remember_token", [c["name"] for c in pagina.context.cookies()])
        # Sem login_user e sem flash gravado: a sessão só guarda o CSRF do formulário.
        sessao = self.sessao_do_navegador(pagina)
        self.assertNotIn("_user_id", sessao, rotulo)
        self.assertNotIn("_flashes", sessao, rotulo)
        self.assert_sem_erros_js(rotulo)

        # Nenhuma sessão autenticada: o portal manda de volta ao login, sem dados
        # e sem flash pendente da tentativa anterior.
        pagina.goto(self.base + "/cliente/dashboard", wait_until="load")
        self.assertIn("/auth/login", pagina.url, rotulo)
        html = pagina.content()
        self.assertNotIn(TEXTO_PRIVADO, html)
        self.assertNotIn("desativada", html)
        self.assertEqual(pagina.locator("h1.auth-title", has_text="Conta encerrada").count(), 0)

    # ===================================================== 1. conta encerrada
    def test_conta_encerrada_apos_login_de_empresa_cancelada(self):
        self.cenario_suspenso()
        self.cancelar()
        self.assertEqual(self.situacao().estado, "CANCELADA")
        for nome, largura, altura in LARGURAS:
            with self.subTest(largura=nome):
                pagina = self.abrir(largura, altura)
                resposta = self.entrar(pagina, CLIENTE)
                self.assert_conta_encerrada(pagina, resposta, largura, altura)

    def test_conta_encerrada_no_instante_limite_da_suspensao(self):
        """Limite exclusivo: um minuto antes ainda entra; no instante-limite, encerrada."""
        self.cenario_suspenso()
        self.relogio(LIMITE_SUSPENSAO - timedelta(minutes=1))
        logado = self.abrir(1440, 900)
        resposta = self.entrar(logado, CLIENTE)
        self.assertTrue(resposta.url.endswith("/cliente/dashboard"), resposta.url)
        self.assertTrue(logado.locator(".banner-suspensa").first.is_visible())

        self.relogio(LIMITE_SUSPENSAO)
        for nome, largura, altura in LARGURAS:
            with self.subTest(largura=nome):
                pagina = self.abrir(largura, altura)
                resposta = self.entrar(pagina, CLIENTE)
                self.assert_conta_encerrada(pagina, resposta, largura, altura)
        # A sessão aberta antes do limite também deixa de valer no instante-limite.
        logado.goto(self.base + "/cliente/dashboard", wait_until="load")
        self.assertIn("/auth/login", logado.url)
        self.assertNotIn(TEXTO_PRIVADO, logado.content())

    # ======================================= 2. painel do cliente suspenso
    def test_painel_suspenso_mostra_prazos_dentro_do_prazo_de_pdf(self):
        self.cenario_suspenso()
        self.relogio(T0 + timedelta(days=5))
        for nome, largura, altura in LARGURAS:
            with self.subTest(largura=nome):
                pagina = self.abrir(largura, altura)
                resposta = self.entrar(pagina, CLIENTE)
                self.assertTrue(resposta.url.endswith("/cliente/dashboard"), resposta.url)
                faixa = pagina.locator(".banner-suspensa")
                self.assertEqual(faixa.count(), 1)
                self.assertTrue(faixa.is_visible())
                texto = self.texto(pagina, ".banner-suspensa")
                self.assertIn("Conta suspensa", texto)
                self.assertIn(f"Consulta disponível até {TEXTO_LIMITE_SUSPENSAO}", texto)
                self.assertIn(f"Exportação de PDFs até {TEXTO_LIMITE_PDF}", texto)
                self.assertNotIn(AVISO_PDF_ENCERRADO, texto)
                caixa = faixa.bounding_box()
                self.assertGreaterEqual(caixa["x"], 0)
                self.assertLessEqual(caixa["x"] + caixa["width"], largura + 0.5)
                # A análise de período pago continua acessível no painel.
                self.assertGreaterEqual(pagina.locator(f"a[href='/cliente/analise/{self.analise_id}']").count(), 1)
                self.assert_sem_transbordamento(pagina, largura)
                self.assert_sem_erros_js(nome)

    # ============================================ 3. análise após o prazo de PDF
    def test_analise_sem_exportacao_no_limite_e_apos_o_prazo_de_pdf(self):
        self.cenario_suspenso()
        caminho = f"/cliente/analise/{self.analise_id}"
        # Controle: um minuto antes do limite o botão existe e o PDF é baixado.
        self.relogio(LIMITE_PDF - timedelta(minutes=1))
        pagina = self.abrir(1440, 900)
        self.entrar(pagina, CLIENTE)
        self.ir(pagina, caminho)
        botao = pagina.locator(f"a[href='{caminho}/pdf']")
        self.assertTrue(botao.is_visible())
        self.assertEqual(pagina.locator(".banner-suspensa").count(), 0)
        with pagina.expect_download() as baixa:
            botao.click()
        self.assertTrue(baixa.value.suggested_filename.endswith(".pdf"), baixa.value.suggested_filename)
        self.assertIsNone(baixa.value.failure())

        for instante in (LIMITE_PDF, T0 + timedelta(days=30)):
            self.relogio(instante)
            for nome, largura, altura in LARGURAS:
                with self.subTest(instante=instante.isoformat(), largura=nome):
                    pagina = self.abrir(largura, altura)
                    self.entrar(pagina, CLIENTE)
                    self.ir(pagina, caminho)
                    self.assertIn(TEXTO_PRIVADO, pagina.content())  # consulta segue liberada
                    aviso = pagina.locator(".banner-suspensa", has_text=AVISO_PDF_ENCERRADO)
                    self.assertEqual(aviso.count(), 1)
                    self.assertTrue(aviso.is_visible())
                    caixa = aviso.bounding_box()
                    self.assertLessEqual(caixa["x"] + caixa["width"], largura + 0.5)
                    self.assertEqual(pagina.locator("a[href$='/pdf']").count(), 0)
                    self.assertEqual(pagina.get_by_text("Exportar PDF").count(), 0)
                    self.assert_sem_transbordamento(pagina, largura)
                    # O painel mostra o mesmo encerramento, ainda com a consulta disponível.
                    self.ir(pagina, "/cliente/dashboard")
                    texto = self.texto(pagina, ".banner-suspensa")
                    self.assertIn(AVISO_PDF_ENCERRADO, texto)
                    self.assertIn(f"Consulta disponível até {TEXTO_LIMITE_SUSPENSAO}", texto)
                    self.assert_sem_transbordamento(pagina, largura)
                    self.assert_sem_erros_js(nome)
        # A rota do PDF também recusa, sem gerar arquivo.
        resposta = pagina.goto(self.base + caminho + "/pdf", wait_until="load")
        self.assertEqual(resposta.status, 403)
        self.assertNotIn("application/pdf", resposta.headers.get("content-type", ""))
        self.assertIn("Exportação encerrada", pagina.locator("h1").inner_text())
        self.descartar_403_esperado()

    # ============================================================ 4. ADMIN
    def cenario_admin(self):
        """Empresa 1 com contrato suspenso e prorrogado, cobertura vigente com
        referência longa sem espaços, cobertura revogada e histórico; empresa 2
        no regime legado, sem contrato."""
        import contract_admin
        from models import CoberturaPaga
        self.contratar(data_formalizacao=date(2026, 1, 2),
                       referencia_formalizacao="Contrato fictício assinado em papel nº 1",
                       observacoes="Observação fictícia de teste.")
        self.pagar(date(2026, 1, 1), date(2026, 1, 31),
                   referencia="Comprovante-fictício-" + "X" * 120)
        self.pagar(date(2026, 2, 1), date(2026, 2, 28), referencia="Comprovante fictício de fevereiro")
        fevereiro = self.db.session.scalar(select(CoberturaPaga.id_cobertura).where(
            CoberturaPaga.periodo_inicio == date(2026, 2, 1)))
        contract_admin.revogar_cobertura(self.db.session, self.empresa_id, fevereiro,
                                         motivo="Estorno fictício " * 12, responsavel=self.admin,
                                         instante=self.agora)
        self.db.session.commit()
        self.ativar_regras()
        self.suspender(motivo="Inadimplência fictícia " * 10)
        contrato = self.vigente()
        contract_admin.registrar_prorrogacao(
            self.db.session, self.empresa_id, contrato.id_contrato, contrato.versao,
            novo_limite=datetime(2026, 7, 2, 13, 0, tzinfo=timezone.utc),
            justificativa="Acordo fictício de prorrogação " * 8, responsavel=self.admin,
            instante=self.agora,
        )
        self.db.session.commit()
        return self.vigente()

    def test_admin_detalhes_formularios_e_visao_geral(self):
        contrato = self.cenario_admin()
        self.relogio(T0 + timedelta(days=1))
        detalhes = f"/admin/empresas/{self.empresa_id}/detalhes"
        novo = f"/admin/empresas/{self.outra_empresa_id}/contratos/novo"
        prorrogar = f"/admin/empresas/{self.empresa_id}/contratos/{contrato.id_contrato}/prorrogar"
        for nome, largura, altura in LARGURAS:
            pagina = self.abrir(largura, altura)
            resposta = self.entrar(pagina, ADMIN)
            self.assertTrue(resposta.url.endswith("/admin/dashboard"), resposta.url)

            with self.subTest(largura=nome, pagina="detalhes"):
                self.ir(pagina, detalhes)
                for secao in ("Situação contratual", "Contratos", "Coberturas pagas", "Histórico contratual"):
                    self.assertTrue(pagina.locator(".nexo-card-head h2", has_text=secao).first.is_visible(), secao)
                situacao = self.dados_contrato(pagina)
                self.assertEqual(situacao["Regime"], "Contratual", situacao)
                self.assertEqual(situacao["Estado efetivo"], "SUSPENSA", situacao)
                self.assertEqual(situacao["Limite original"], TEXTO_LIMITE_SUSPENSAO, situacao)
                self.assertEqual(situacao["Limite vigente"], "02/07/2026 às 10:00 prorrogado", situacao)
                self.assertEqual(situacao["Exportação de PDF até"], TEXTO_LIMITE_PDF, situacao)
                self.assertEqual(situacao["Períodos pagos (consolidados)"], "01/01/2026 a 31/01/2026", situacao)
                tabelas = pagina.locator(".table-responsive-x table")
                self.assertEqual(tabelas.count(), 3)
                self.assertEqual(tabelas.nth(0).locator("tbody tr").count(), 1)
                self.assertIn("SUSPENSO", tabelas.nth(0).inner_text())
                coberturas = tabelas.nth(1).inner_text()
                self.assertEqual(tabelas.nth(1).locator("tbody tr").count(), 2)
                self.assertIn("VIGENTE", coberturas)
                self.assertIn("REVOGADA", coberturas)
                historico = tabelas.nth(2).inner_text()
                for rotulo in ("Contratação", "Período pago confirmado", "Período pago revogado",
                               "Regras contratuais ativadas", "Suspensão", "Prorrogação da suspensão"):
                    self.assertIn(rotulo, historico)
                self.assert_sem_transbordamento(pagina, largura)
                self.assert_cubo_sidebar(pagina, largura, altura)
                self.assert_sem_erros_js(nome)

            with self.subTest(largura=nome, pagina="novo contrato"):
                self.ir(pagina, novo)
                self.assertEqual(self.texto(pagina, ".page-head h1"), "Novo contrato")
                for seletor, tipo in (("#data_inicio", "date"), ("#data_fim", "date"),
                                      ("#data_formalizacao", "date"), ("#valor_mensal", "text")):
                    self.assertEqual(pagina.get_attribute(seletor, "type"), tipo, seletor)
                self.assertTrue(pagina.locator("form.relatorio-doc input[name=csrf_token]").input_value())
                self.assertEqual(pagina.locator("form.relatorio-doc input[name=versao]").count(), 0)
                # O script da página preenche o valor de catálogo do plano escolhido.
                opcao = pagina.locator("#id_plano option[data-valor]").first
                pagina.select_option("#id_plano", value=opcao.get_attribute("value"))
                self.assertEqual(pagina.input_value("#valor_mensal"), opcao.get_attribute("data-valor"))
                self.assertTrue(pagina.get_by_role("button", name="Salvar contrato").is_visible())
                self.assert_sem_transbordamento(pagina, largura)
                self.assert_cubo_sidebar(pagina, largura, altura)
                self.assert_sem_erros_js(nome)

            with self.subTest(largura=nome, pagina="prorrogação"):
                self.ir(pagina, prorrogar)
                self.assertEqual(self.texto(pagina, ".page-head h1"), "Prorrogar suspensão")
                self.assertIn("não representa pagamento", self.texto(pagina, ".aviso-contrato"))
                self.assertEqual(pagina.get_attribute("#novo_limite", "type"), "datetime-local")
                self.assertIsNotNone(pagina.get_attribute("#novo_limite", "required"))
                self.assertEqual(pagina.get_attribute("#novo_limite_pdf", "type"), "datetime-local")
                self.assertEqual(pagina.get_attribute("#autorizacao_pdf", "type"), "checkbox")
                self.assertIsNotNone(pagina.get_attribute("#justificativa", "required"))
                self.assertTrue(pagina.locator("form.relatorio-doc input[name=csrf_token]").input_value())
                self.assertEqual(pagina.locator("form.relatorio-doc input[name=versao]").input_value(),
                                 str(contrato.versao))
                self.assertIn("America/Sao_Paulo", self.texto(pagina, "form.relatorio-doc"))
                self.assert_sem_transbordamento(pagina, largura)
                self.assert_cubo_sidebar(pagina, largura, altura)
                self.assert_sem_erros_js(nome)

            with self.subTest(largura=nome, pagina="visão geral"):
                self.ir(pagina, "/admin/contratos")
                self.assertEqual(self.texto(pagina, ".page-head h1"), "Visão geral contratual")
                suspensas = pagina.locator(".kpi-tile", has_text="Empresas suspensas").locator(".kpi-value")
                self.assertEqual(suspensas.inner_text().strip(), "1")
                legado = pagina.locator(".nexo-card", has_text="Empresas no regime legado")
                self.assertIn("Teste 2", legado.inner_text())
                self.assertEqual(pagina.locator(f"a[href='{detalhes}']").count(), 0)  # nada vencido/pendente
                self.assert_sem_transbordamento(pagina, largura)
                self.assert_cubo_sidebar(pagina, largura, altura)
                self.assert_sem_erros_js(nome)

    # ===================================================== 5. fluxos reais
    def _registrar_periodo(self, pagina, inicio, fim, referencia):
        pagina.fill("#periodo_inicio", inicio)
        pagina.fill("#periodo_fim", fim)
        pagina.fill("#referencia", referencia)
        with pagina.expect_navigation(wait_until="load") as navegacao:
            pagina.get_by_role("button", name="Confirmar período pago").click()
        return navegacao.value

    def test_admin_registra_periodo_pago_pelo_formulario(self):
        from models import CoberturaPaga, EventoContratual
        contrato = self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        fevereiro = self.nova_analise(date(2026, 2, 1), date(2026, 2, 28))
        self.publicar(fevereiro.id_analise, texto="Conclusão fictícia de fevereiro")
        self.ativar_regras()
        instante = T0 + timedelta(hours=2)
        self.relogio(instante)
        analise_fev = f"/cliente/analise/{fevereiro.id_analise}"
        detalhes = f"/admin/empresas/{self.empresa_id}/detalhes"

        # Antes do registro, o cliente não consulta a análise de fevereiro.
        cliente = self.abrir(1440, 900)
        self.entrar(cliente, CLIENTE)
        self.ir(cliente, analise_fev, status=403)
        self.assertIn("Análise indisponível", cliente.locator("h1").inner_text())
        self.assertNotIn("Conclusão fictícia de fevereiro", cliente.content())
        self.descartar_403_esperado()

        admin = self.abrir(1440, 900, fuso="Asia/Tokyo")
        self.entrar(admin, ADMIN)
        self.ir(admin, detalhes)
        with admin.expect_navigation(wait_until="load"):
            admin.locator(".acoes-contrato a", has_text="Registrar período pago").click()
        self.assertTrue(admin.url.endswith(f"/contratos/{contrato.id_contrato}/cobertura"), admin.url)
        self.assert_sem_transbordamento(admin, 1440)
        resposta = self._registrar_periodo(admin, "2026-02-01", "2026-02-28", "Comprovante fictício de fevereiro")
        self.assertEqual(resposta.status, 200)
        self.assertTrue(admin.url.endswith(detalhes), admin.url)
        sucesso = admin.locator(".flash-stack .alert-success")
        self.assertTrue(sucesso.is_visible())
        self.assertIn("Período pago registrado.", sucesso.inner_text())
        coberturas = admin.locator(".nexo-card", has_text="Coberturas pagas").locator("tbody").inner_text()
        self.assertIn("01/02/2026 a 28/02/2026", coberturas)
        self.assertIn("Comprovante fictício de fevereiro", coberturas)
        self.assertEqual(self.dados_contrato(admin)["Períodos pagos (consolidados)"],
                         "01/01/2026 a 28/02/2026")  # janeiro + fevereiro consolidados
        self.assert_sem_erros_js("registro")

        self.db.session.expire_all()
        registro = self.db.session.scalar(select(CoberturaPaga).where(
            CoberturaPaga.periodo_inicio == date(2026, 2, 1)))
        self.assertEqual((registro.periodo_fim, registro.id_contrato, registro.id_usuario_admin_confirmacao,
                          registro.confirmado_em, registro.revogada_em),
                         (date(2026, 2, 28), contrato.id_contrato, self.admin_id, instante, None))
        confirmados = select(func.count(EventoContratual.id_evento)).where(
            EventoContratual.id_empresa == self.empresa_id, EventoContratual.tipo == "COBERTURA_CONFIRMADA")
        self.assertEqual(self.db.session.scalar(confirmados), 2)

        # Sobreposição: o servidor recusa, mantém o formulário preenchido e nada grava.
        self.ir(admin, f"/admin/empresas/{self.empresa_id}/contratos/{contrato.id_contrato}/cobertura")
        resposta = self._registrar_periodo(admin, "2026-02-15", "2026-03-10", "Comprovante fictício sobreposto")
        self.assertTrue(admin.url.endswith("/cobertura"), admin.url)
        self.assertIn("se sobrepõe", admin.locator(".flash-stack .alert-danger").inner_text())
        self.assertEqual(admin.input_value("#periodo_inicio"), "2026-02-15")
        self.assertEqual(admin.input_value("#referencia"), "Comprovante fictício sobreposto")
        self.db.session.expire_all()
        self.assertEqual(self.db.session.scalar(select(func.count(CoberturaPaga.id_cobertura))), 2)

        # O cliente, na mesma sessão, passa a consultar fevereiro (e exportar: conta ativa).
        self.ir(cliente, analise_fev)
        self.assertIn("Conclusão fictícia de fevereiro", cliente.content())
        self.assertEqual(cliente.locator(f"a[href='{analise_fev}/pdf']").count(), 1)

        # O mesmo formulário funciona no celular.
        celular = self.abrir(375, 812)
        self.entrar(celular, ADMIN)
        self.ir(celular, f"/admin/empresas/{self.empresa_id}/contratos/{contrato.id_contrato}/cobertura")
        self.assert_sem_transbordamento(celular, 375)
        self._registrar_periodo(celular, "2026-03-01", "2026-03-31", "Comprovante fictício de março")
        self.assertTrue(celular.url.endswith(detalhes), celular.url)
        self.assertIn("Período pago registrado.", celular.locator(".flash-stack .alert-success").inner_text())
        self.assertEqual(self.dados_contrato(celular)["Períodos pagos (consolidados)"], "01/01/2026 a 31/03/2026")
        self.assert_sem_transbordamento(celular, 375)

    def test_admin_prorroga_suspensao_no_fuso_de_sao_paulo(self):
        """datetime-local é lido no fuso da política, não no do navegador (Tóquio)."""
        from models import Contrato
        contrato = self.cenario_suspenso()
        self.relogio(T0 + timedelta(days=3))
        admin = self.abrir(1280, 800, fuso="Asia/Tokyo")
        self.entrar(admin, ADMIN)
        self.ir(admin, f"/admin/empresas/{self.empresa_id}/detalhes")
        with admin.expect_navigation(wait_until="load"):
            admin.locator(".acoes-contrato a", has_text="Prorrogar suspensão").click()
        admin.fill("#novo_limite", "2026-08-02T10:00")
        admin.fill("#justificativa", "Acordo fictício de prorrogação")
        with admin.expect_navigation(wait_until="load"):
            admin.get_by_role("button", name="Confirmar: prorrogar suspensão").click()
        self.assertTrue(admin.url.endswith(f"/admin/empresas/{self.empresa_id}/detalhes"), admin.url)
        self.assertIn("Prorrogação registrada no histórico.",
                      admin.locator(".flash-stack .alert-success").inner_text())
        situacao = self.dados_contrato(admin)
        self.assertEqual(situacao["Limite vigente"], "02/08/2026 às 10:00 prorrogado", situacao)
        self.assertEqual(situacao["Limite original"], TEXTO_LIMITE_SUSPENSAO, situacao)
        self.assertEqual(situacao["Exportação de PDF até"], TEXTO_LIMITE_PDF, situacao)  # PDF não é ampliado
        self.assert_sem_transbordamento(admin, 1280)

        self.db.session.expire_all()
        gravado = self.db.session.get(Contrato, contrato.id_contrato)
        self.assertEqual((gravado.suspensao_limite, gravado.suspensao_limite_original, gravado.pdf_limite),
                         (datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc), LIMITE_SUSPENSAO, LIMITE_PDF))

        cliente = self.abrir(375, 812)
        self.entrar(cliente, CLIENTE)
        texto = self.texto(cliente, ".banner-suspensa")
        self.assertIn("Consulta disponível até 02/08/2026 às 10:00", texto)
        self.assertIn(f"Exportação de PDFs até {TEXTO_LIMITE_PDF}", texto)
        self.assert_sem_transbordamento(cliente, 375)


if __name__ == "__main__":
    unittest.main()
