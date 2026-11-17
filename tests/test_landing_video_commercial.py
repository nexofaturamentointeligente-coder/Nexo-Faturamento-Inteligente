"""COM-001: player existente, com mídia real, em Chromium.

O servidor é uma prévia Flask local do template e dos arquivos estáticos reais;
não importa o backend, não lê .env e não acessa banco ou serviços de produção.
Não altera a política de autoplay do navegador nem simula sucesso de play().
Login/rotas da aplicação real são verificados em test_landing_commercial.py.
"""

import os
from pathlib import Path
import threading
import unittest

from flask import Blueprint, Flask, render_template
from werkzeug.serving import make_server, WSGIRequestHandler

from tests.landing_support import servir_bootstrap

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


_BROWSER = os.environ.get("NEXO_TEST_BROWSER") == "1" and sync_playwright is not None
_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale Playwright.")
class LandingVideoCommercialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class QuietLocalRequests(WSGIRequestHandler):
            def log(self, log_type, message, *args):
                pass

        app = Flask(__name__, template_folder=str(_ROOT / "templates"),
                    static_folder=str(_ROOT / "static"), static_url_path="/static")
        app.add_url_rule("/", "index", lambda: render_template("landing.html"))
        app.add_url_rule("/como-aderir", "como_aderir", lambda: "Prévia local de teste")
        auth = Blueprint("auth", __name__, url_prefix="/auth")
        auth.add_url_rule("/login", "login", lambda: "Prévia local de teste")
        app.register_blueprint(auth)
        documentos = Blueprint("documentos_publicos", __name__)
        documentos.add_url_rule("/termos-de-uso", "termos_de_uso", lambda: "Prévia local de teste")
        documentos.add_url_rule("/politica-de-privacidade", "politica_de_privacidade",
                                lambda: "Prévia local de teste")
        app.register_blueprint(documentos)
        cls.server = make_server("127.0.0.1", 0, app, threaded=True,
                                 request_handler=QuietLocalRequests)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.pw = sync_playwright().start()
        cls.addClassCleanup(cls.pw.stop)
        cls.browser = cls.pw.chromium.launch(executable_path=os.environ.get("NEXO_TEST_CHROMIUM") or None)
        cls.addClassCleanup(cls.browser.close)

    def page(self, *, width=1440, reduced_motion="reduce", fail_media=False):
        context = self.browser.new_context(viewport={"width": width, "height": 900},
                                           reduced_motion=reduced_motion)
        self.addCleanup(context.close)
        page = context.new_page()
        self.errors = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))

        def route(request):
            url = request.request.url
            if url.startswith(self.base):
                if fail_media and url.endswith("/nexo_demo_motion.mp4"):
                    request.fulfill(status=404, body="Falha de mídia somente do teste")
                else:
                    request.continue_()
            elif "/bootstrap@5.3.3/dist/css/bootstrap.min.css" in url:
                # A cópia íntegra local, quando existe (sem rede externa); senão, como na CI.
                servir_bootstrap(request)
            else:
                request.abort()

        page.route("**/*", route)
        page.goto(self.base + "/", wait_until="load")
        return page

    def tearDown(self):
        self.assertEqual(getattr(self, "errors", []), [], "Exceções JavaScript no player")

    def center_video(self, page):
        page.locator("#nexo-demo-video").evaluate("video => video.scrollIntoView({block:'center',behavior:'instant'})")
        page.wait_for_function("() => document.querySelector('#nexo-demo-frame').dataset.visibility === 'full'")

    def start_by_gesture(self, page):
        self.center_video(page)
        page.locator("#nexo-demo-sound-enable").click()
        page.wait_for_function("() => document.querySelector('#nexo-demo-frame').dataset.playback === 'playing'")
        page.wait_for_function("() => document.querySelector('#nexo-demo-video').readyState >= 2")

    def allow_sound_by_gesture(self, page):
        self.start_by_gesture(page)
        page.wait_for_function("() => ['allowed','needs-gesture'].includes(document.querySelector('#nexo-demo-frame').dataset.audio)")
        if page.locator("#nexo-demo-sound-enable").is_visible():
            page.locator("#nexo-demo-sound-enable").click()
        page.wait_for_function("() => !document.querySelector('#nexo-demo-video').muted")
        self.assertEqual(page.locator("#nexo-demo-frame").get_attribute("data-audio"), "allowed")

    def test_reduced_motion_waits_for_explicit_play_and_loads_actual_mp4(self):
        page = self.page()
        self.center_video(page)
        self.assertTrue(page.locator("#nexo-demo-video").evaluate("video => video.paused"))
        self.assertEqual(page.locator("#nexo-demo-frame").get_attribute("data-playback"), "needs-gesture")
        self.assertEqual(page.locator("#nexo-demo-sound-enable").inner_text().strip(), "Reproduzir demonstração")
        self.start_by_gesture(page)
        media = page.locator("#nexo-demo-video").evaluate("video => ({source:video.currentSrc,width:video.videoWidth,height:video.videoHeight,duration:video.duration,loop:video.loop})")
        self.assertTrue(media["source"].endswith("/static/video/nexo_demo_motion.mp4"))
        self.assertEqual((media["width"], media["height"]), (1920, 1080))
        self.assertGreater(media["duration"], 35)
        self.assertTrue(media["loop"])

    def test_default_motion_starts_when_near_viewport_and_keeps_playing_muted_offscreen(self):
        page = self.page(reduced_motion="no-preference")
        self.center_video(page)
        page.wait_for_function("() => document.querySelector('#nexo-demo-frame').dataset.playback === 'playing'")
        page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
        page.wait_for_function("() => document.querySelector('#nexo-demo-frame').dataset.visibility === 'partial'")
        self.assertTrue(page.locator("#nexo-demo-video").evaluate("video => video.muted && !video.paused"))

    def test_explicit_pause_survives_scroll_pageshow_and_visibility_events_until_keyboard_resume(self):
        page = self.page(width=390)
        self.start_by_gesture(page)
        pause = page.locator("#nexo-demo-pause")
        pause.focus()
        page.keyboard.press("Enter")
        self.assertTrue(page.locator("#nexo-demo-video").evaluate("video => video.paused && video.muted"))
        page.evaluate("""() => { window.scrollTo({top:0,behavior:'instant'});
            window.dispatchEvent(new Event('pageshow'));
            document.dispatchEvent(new Event('visibilitychange'));
            window.dispatchEvent(new Event('online')); }""")
        self.center_video(page)
        self.assertEqual(page.locator("#nexo-demo-frame").get_attribute("data-playback"), "paused")
        self.assertTrue(page.locator("#nexo-demo-video").evaluate("video => video.paused"))
        self.assertEqual(pause.get_attribute("aria-label"), "Retomar demonstração")
        pause.focus()
        page.keyboard.press("Enter")
        page.wait_for_function("() => !document.querySelector('#nexo-demo-video').paused")
        self.assertEqual(page.locator("#nexo-demo-frame").get_attribute("data-user-paused"), "false")

    def test_sound_requires_full_visibility_and_explicit_mute_persists_after_scroll(self):
        page = self.page()
        self.allow_sound_by_gesture(page)
        # Um único pixel sob o cabeçalho fixo já é visibilidade parcial.
        page.evaluate("""() => {
            const video=document.querySelector('#nexo-demo-video').getBoundingClientRect();
            const header=document.querySelector('.nexo-header').getBoundingClientRect();
            window.scrollBy({top:video.top-header.bottom+1,behavior:'instant'});
        }""")
        page.wait_for_function("() => document.querySelector('#nexo-demo-frame').dataset.visibility === 'partial'")
        self.assertTrue(page.locator("#nexo-demo-video").evaluate("video => video.muted"))
        page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
        page.wait_for_function("() => document.querySelector('#nexo-demo-video').muted")
        self.center_video(page)
        page.wait_for_function("() => !document.querySelector('#nexo-demo-video').muted")
        page.locator("#nexo-demo-mute").click()
        self.assertEqual(page.locator("#nexo-demo-frame").get_attribute("data-user-muted"), "true")
        page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
        self.center_video(page)
        self.assertTrue(page.locator("#nexo-demo-video").evaluate("video => video.muted && !video.paused"))
        page.locator("#nexo-demo-mute").click()
        page.wait_for_function("() => !document.querySelector('#nexo-demo-video').muted")
        self.assertEqual(page.locator("#nexo-demo-frame").get_attribute("data-user-muted"), "false")

    def test_loop_restarts_actual_media_without_ending_playback(self):
        page = self.page()
        self.start_by_gesture(page)
        page.locator("#nexo-demo-video").evaluate("video => { video.currentTime = video.duration - 0.25; }")
        page.wait_for_function("() => document.querySelector('#nexo-demo-video').currentTime < 2")
        self.assertFalse(page.locator("#nexo-demo-video").evaluate("video => video.paused"))

    def test_fullscreen_button_enters_and_exits_actual_element_fullscreen(self):
        page = self.page()
        self.start_by_gesture(page)
        page.locator("#nexo-demo-fullscreen").click()
        page.wait_for_function("() => document.fullscreenElement && document.fullscreenElement.id === 'nexo-demo-frame'")
        page.wait_for_function("() => document.querySelector('#nexo-demo-frame').dataset.fullscreen === 'true'")
        self.assertEqual(page.locator("#nexo-demo-frame").get_attribute("data-fullscreen"), "true")
        self.assertEqual(page.locator("#nexo-demo-fullscreen").get_attribute("aria-label"), "Sair da tela cheia")
        page.locator("#nexo-demo-fullscreen").click()
        page.wait_for_function("() => !document.fullscreenElement")
        page.wait_for_function("() => document.querySelector('#nexo-demo-frame').dataset.fullscreen === 'false'")
        self.assertEqual(page.locator("#nexo-demo-frame").get_attribute("data-fullscreen"), "false")

    def test_media_error_is_visible_and_retry_recovers_real_video(self):
        page = self.page(fail_media=True)
        self.center_video(page)
        page.locator("#nexo-demo-sound-enable").click()
        page.wait_for_function("() => document.querySelector('#nexo-demo-frame').dataset.playback === 'error'")
        self.assertTrue(page.locator("#nexo-demo-error").is_visible())
        self.assertTrue(page.locator("#nexo-demo-video").evaluate("video => video.muted"))
        page.route("**/static/video/nexo_demo_motion.mp4", lambda request: request.continue_())
        page.locator("#nexo-demo-retry").click()
        page.wait_for_function("() => document.querySelector('#nexo-demo-frame').dataset.playback === 'playing'")
        self.assertFalse(page.locator("#nexo-demo-error").is_visible())
        self.assertEqual(page.locator("#nexo-demo-video").evaluate("video => video.videoWidth"), 1920)


if __name__ == "__main__":
    unittest.main()
