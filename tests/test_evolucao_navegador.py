"""Evolução & Conquistas no navegador (Etapa 3, 14/11/2026): o painel da Visão Geral e do Plano de Ação e a linha do
tempo do Histórico nas larguras do celular ao desktop, o contraste nos dois temas e a impressão do Plano de Ação com o
painel (templates/_evolucao.html e a folha nexo.css).

Ignorado por padrão, como os demais testes de navegador: precisa de NEXO_TEST_BROWSER=1, do Playwright e das cópias
íntegras da CDN (ver tests/test_overflow_navegador.py). O cliente passa para o PRATA, ganha o Raio-X de oito meses de
tests/test_raio_x_navegador.py (o fechamento de março, pelo relógio dos testes) e dois retratos anteriores: o Marco Zero
de janeiro, com perdas maiores e margem maior, e o fechamento de fevereiro, sem perda abaixo do custo (assim o painel
mostra melhora, piora e regressão contra o mês anterior).
"""

from datetime import date, datetime
import json
import unittest

from tests.test_overflow_navegador import NavegadorCase, _BROWSER
# Pelos módulos, e não pelos nomes: as classes importadas no módulo seriam coletadas (e rodadas) de novo aqui.
import tests.test_evolucao as testes_evolucao
import tests.test_plano_acao_navegador as navegador_plano
import tests.test_raio_x_navegador as navegador_raio_x
from tests.test_tema_navegador import _CONTRASTE

DASHBOARD, HISTORICO, AUDITORIA = "/cliente/dashboard", "/cliente/historico", navegador_raio_x.AUDITORIA
_BASE = navegador_raio_x.RaioXNavegadorTests
_PLANO = navegador_plano.PlanoAcaoNavegadorTests
LARGURAS = (390, 768, 1024, 1366, 1920)
_SELETORES = (".evolucao-titulo", ".evolucao-sub", ".evolucao-rotulo", ".evolucao-valor", ".evolucao-texto",
              ".evolucao-regressoes-titulo", ".evolucao-regressoes li", ".evolucao-subtitulo", ".evolucao-contagem",
              ".evolucao-indicador-nome", ".evolucao-indicador-valor", ".evolucao-indicador-de",
              ".evolucao-indicador.is-melhorou .evolucao-indicador-variacao",
              ".evolucao-indicador.is-piorou .evolucao-indicador-variacao", ".evolucao-selo-topo strong",
              ".evolucao-selo-regra", ".evolucao-selo-texto", ".evolucao-resumo span", ".evolucao .nexo-badge.b-green",
              ".evolucao .nexo-badge.b-red", ".evolucao .nexo-badge.b-muted")
_MEDIR = """(seletor) => { const p = document.querySelector(seletor);
    const fora = [...p.querySelectorAll('*')].filter(e => { const r = e.getBoundingClientRect();
        return r.width > 0 && (r.right > innerWidth + 1 || r.left < -1) && !e.closest('.table-responsive-x'); })
        .map(e => e.tagName + '.' + e.className);
    return {pagina: document.documentElement.scrollWidth > innerWidth + 1, fora: fora.slice(0, 5),
            rola: p.scrollWidth > p.clientWidth + 1}; }"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class EvolucaoNavegadorTests(NavegadorCase):
    def setUp(self):
        super().setUp()
        self._prata()
        self._raio_x_com_avisos()
        self._retratos_anteriores()

    _prata = _BASE._prata
    _raio_x_com_avisos = _BASE._raio_x_com_avisos
    _pagina = _BASE._pagina
    _esperar = _PLANO._esperar
    _abrir_plano = _PLANO._abrir_plano

    def _retratos_anteriores(self):
        from models import DiagnosticoFechamento
        for dia, campos in ((date(2026, 1, 5), dict(recuperavel=5000.0, perda=3000.0, vazamento=2000.0, pct=0.5,
                                                    negativa=50, alertas=10, margem=0.9)),
                            (date(2026, 2, 2), dict(recuperavel=900.0, perda=0.0, vazamento=900.0, pct=0.2,
                                                    negativa=5, alertas=1, margem=0.2))):
            retrato = testes_evolucao._retrato(dia, **campos)
            self.db.session.add(DiagnosticoFechamento(
                id_empresa=self.empresa_id, competencia=date(dia.year, dia.month, 1),
                gerado_em=datetime.fromisoformat(retrato["gerado_em"]), versao_regras=retrato["versao_regras"],
                indicadores=json.dumps(retrato, ensure_ascii=False)))
        self.db.session.commit()

    def _tema(self, pagina, tema, pronto="[data-evolucao]"):
        """Troca o tema. Com gráfico na página (a Visão Geral), a troca recarrega: espera a página nova inteira, com o
        elemento `pronto`, antes de medir."""
        if tema != "light":
            return
        pagina.locator("#themeToggle").click()
        espera = ("() => document.documentElement.getAttribute('data-theme') === 'light' "
                  "&& document.readyState === 'complete' && !!document.querySelector(" + repr(pronto) + ")")
        for _ in range(200):
            try:
                if pagina.evaluate(espera):
                    return
            except Exception:  # noqa: BLE001 - a página recarregando troca o contexto da avaliação
                pass
            pagina.wait_for_timeout(50)
        self.fail("o tema claro não terminou de carregar")

    def test_painel_da_visao_geral_sem_rolagem_lateral(self):
        for largura in LARGURAS:
            with self.subTest(largura=largura):
                pagina = self._pagina(DASHBOARD, largura, 900)
                painel = pagina.locator("[data-evolucao]")
                self.assertTrue(painel.is_visible())
                texto = painel.text_content()
                for trecho in ("Evolução & Conquistas", "Economia Gerada no Mês", "Projeção Anual no Ritmo Atual",
                               "Onde houve regressão", "Conquistas", "Comparado ao seu Raio-X inicial de 05/01/2026"):
                    self.assertIn(trecho, texto)
                medida = pagina.evaluate(_MEDIR, "[data-evolucao]")
                self.assertFalse(medida["pagina"], medida)
                self.assertEqual(medida["fora"], [], medida)
                self.assertFalse(medida["rola"], medida)
                pagina.context.close()

    def test_contraste_do_painel_nos_dois_temas(self):
        for tema in ("dark", "light"):
            with self.subTest(tema=tema):
                pagina = self._pagina(DASHBOARD)
                self._tema(pagina, tema)
                for medida in pagina.evaluate(_CONTRASTE, list(_SELETORES)):
                    with self.subTest(tema=tema, seletor=medida["seletor"]):
                        self.assertNotIn("ausente", medida, "amostra não encontrada no painel")
                        self.assertGreaterEqual(medida["razao"], medida["minimo"], medida)
                pagina.context.close()

    def test_linha_do_tempo_no_historico(self):
        pagina = self._pagina(DASHBOARD)
        with pagina.expect_navigation(wait_until="load"):
            pagina.locator("[data-evolucao]").get_by_role("link", name="Linha do tempo").click()
        self.assertTrue(pagina.url.endswith(HISTORICO + "#evolucaoLinhaDoTempo"))
        self._esperar(pagina, "() => { const r = document.getElementById('evolucaoLinhaDoTempo').getBoundingClientRect(); "
                              "return r.top >= 0 && r.top < innerHeight; }")
        meses = pagina.evaluate("() => [...document.querySelectorAll('.evolucao-tabela tbody tr')]"
                                ".map(r => r.querySelector('.evolucao-fechamento .t-strong').textContent.trim())")
        self.assertEqual(meses, ["Março/2026", "Fevereiro/2026", "Janeiro/2026"])
        pagina.context.close()
        for tema in ("dark", "light"):
            with self.subTest(tema=tema):
                celular = self._pagina(HISTORICO, 390, 844)
                self._tema(celular, tema, "#evolucaoLinhaDoTempo")
                medida = celular.evaluate(_MEDIR, "#evolucaoLinhaDoTempo")
                self.assertFalse(medida["pagina"], medida)
                self.assertEqual(medida["fora"], [], medida)
                # A tabela tem Marco Zero e Em evolução; a legenda, todos os status.
                for contraste in celular.evaluate(_CONTRASTE, [
                        "#evolucaoLinhaTitulo", ".evolucao-fechamento .t-strong", ".evolucao-fechamento-sub",
                        ".evolucao-tabela .nexo-badge.b-purple", ".evolucao-tabela .nexo-badge.b-info",
                        ".evolucao-legenda li", ".evolucao-legenda .nexo-badge.b-green",
                        ".evolucao-legenda .nexo-badge.b-amber", ".evolucao-legenda .nexo-badge.b-muted",
                        ".evolucao-linha .evolucao-resumo span"]):
                    with self.subTest(tema=tema, seletor=contraste["seletor"]):
                        self.assertNotIn("ausente", contraste)
                        self.assertGreaterEqual(contraste["razao"], contraste["minimo"], contraste)
                celular.context.close()

    def test_painel_no_plano_e_na_impressao(self):
        for tema in ("dark", "light"):
            with self.subTest(tema=tema):
                pagina = self._pagina(AUDITORIA)
                self._tema(pagina, tema)
                modal = self._abrir_plano(pagina)
                self.assertTrue(modal.locator("[data-evolucao]").is_visible())
                medida = pagina.evaluate(_MEDIR, "#planoAcao [data-evolucao]")
                self.assertEqual(medida["fora"], [], medida)
                pagina.emulate_media(media="print")
                self.assertTrue(pagina.evaluate("() => getComputedStyle(document.querySelector('#planoAcao "
                                                "[data-evolucao]')).display !== 'none'"))
                for contraste in pagina.evaluate(_CONTRASTE, [f"#planoAcao {s}" for s in _SELETORES]):
                    with self.subTest(tema=tema, seletor=contraste["seletor"]):
                        self.assertNotIn("ausente", contraste)
                        self.assertGreaterEqual(contraste["razao"], 4.5, contraste)
                pagina.emulate_media(media="screen")
                pagina.context.close()
