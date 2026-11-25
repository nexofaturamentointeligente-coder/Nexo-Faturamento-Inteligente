"""Visão Geral em navegador real (Chromium via Playwright), com a página no topo.

O botão "Abrir análise completa" do cartão "Sua análise mais recente" fica abaixo do texto, à esquerda, em todas
as larguras (.bloco-acao-empilhado em static/css/nexo.css). No canto direito da primeira dobra, o botão do NexoBot
o cobria assim que a página abria: o NexoBot só se recolhe ao rolar (static/js/nexobot.js o mantém visível com a
página no topo). Nas alturas de janela de computador (tela menos a barra do navegador) em que isso acontecia, com e
sem o aviso de diferença no total de vendas e com e sem o "Próximo passo", o ponto no meio da borda direita do botão
é do próprio botão, e não do #nexobot. Habilitado por NEXO_TEST_BROWSER=1, como em
tests/test_overflow_navegador.py; dados fictícios (example.invalid).
"""

import unittest
from datetime import date

from tests.test_overflow_navegador import NavegadorCase, _BROWSER

CLIENTE = "cliente-test@example.invalid"
# Janelas de computador em que o NexoBot cobria o botão com a página no topo (revisão visual-1).
JANELAS = ((1366, 615), (1440, 725), (1536, 713), (1366, 900))
# As pontas: o botão também fica abaixo do texto na tela larga e no celular.
PONTAS = ((1920, 1080), (390, 844))
# Na ordem: o aviso de diferença sai a partir do terceiro estado (a conferência é apagada).
ESTADOS = ((True, True), (True, False), (False, False), (False, True))

_MEDIR = r"""() => {
    const secao = document.querySelector('section[aria-labelledby="ultimaAnaliseTitulo"]');
    const link = secao && secao.querySelector('.bloco-acao > a.btn-nexo');
    const texto = secao && secao.querySelector('.bloco-acao-texto');
    const raiz = document.getElementById('nexobot'), bot = document.getElementById('nexobotLauncher');
    if (!link || !texto || !raiz || !bot) return null;
    const r = link.getBoundingClientRect(), t = texto.getBoundingClientRect(), b = bot.getBoundingClientRect();
    // O meio da borda direita, um pixel para dentro (o pixel da borda já é do que vem ao lado).
    const x = r.right - 1, y = r.top + r.height / 2;
    const naTela = x >= 0 && x < innerWidth && y >= 0 && y < innerHeight;
    const topo = naTela ? document.elementFromPoint(x, y) : null;
    const cs = getComputedStyle(bot);
    const botVisivel = b.width > 0 && b.height > 0 && cs.visibility !== 'hidden' && cs.opacity !== '0';
    return {
        rotulo: link.textContent.replace(/\s+/g, ' ').trim(), rolagem: window.scrollY, naTela,
        doLink: !!topo && (topo === link || link.contains(topo)),
        doNexobot: !!(topo && topo.closest('#nexobot')),
        noPonto: topo ? topo.tagName.toLowerCase() + (topo.id ? '#' + topo.id : '') : null,
        abaixoDoTexto: r.top >= t.bottom - 0.5, aEsquerda: Math.abs(r.left - t.left) <= 1,
        cruza: botVisivel && Math.min(r.right, b.right) > Math.max(r.left, b.left)
            && Math.min(r.bottom, b.bottom) > Math.max(r.top, b.top),
        recolhido: raiz.classList.contains('is-recolhido'),
        proximoPasso: !!document.getElementById('proximoPassoTitulo'),
        diferenca: [...document.querySelectorAll('.aviso-contrato strong')]
            .some((s) => s.textContent.includes('Diferença no total de vendas')),
        link: [r.left, r.top, r.width, r.height].map(Math.round),
        nexobot: [b.left, b.top, b.width, b.height].map(Math.round),
    };
}"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class VisaoGeralNavegadorTests(NavegadorCase):
    def _estado(self, diferenca, proximo_passo):
        """O período aberto de fevereiro (dados_extensos) esperando ou não os relatórios do cliente (o cartão
        "Próximo passo") e com ou sem a conferência divergente do total do PDV (o aviso de diferença)."""
        from models import Analise, ValidacaoPdv
        s = self.db.session
        aberta = s.scalars(self.db.select(Analise).where(Analise.id_empresa == self.empresa_id,
                                                          Analise.periodo_inicio == date(2026, 2, 1))).one()
        aberta.status_analise = "AGUARDANDO_RELATORIO" if proximo_passo else "RELATORIO_RECEBIDO"
        conferencia = s.get(ValidacaoPdv, aberta.id_analise)
        if not diferenca and conferencia is not None:
            s.delete(conferencia)
        s.commit()

    def _medir(self, pagina, largura, altura):
        pagina.set_viewport_size({"width": largura, "height": altura})
        self.ir(pagina, "/cliente/dashboard")
        return pagina.evaluate(_MEDIR)

    def assert_no_topo(self, medida, contexto):
        self.assertIsNotNone(medida, f"{contexto}: cartão da análise mais recente ausente")
        self.assertEqual(medida["rotulo"], "Abrir análise completa", contexto)
        self.assertEqual(medida["rolagem"], 0, f"{contexto}: a página não está no topo")
        self.assertFalse(medida["recolhido"], f"{contexto}: o NexoBot devia estar visível com a página no topo")

    def assert_botao_abaixo_do_texto(self, medida, contexto):
        self.assertFalse(medida["cruza"], f"{contexto}: NexoBot sobre o botão ({medida})")
        self.assertTrue(medida["abaixoDoTexto"] and medida["aEsquerda"], f"{contexto}: botão ao lado do texto ({medida})")

    def test_abrir_analise_completa_fora_do_nexobot_com_a_pagina_no_topo(self):
        pagina = self.abrir(*JANELAS[-1])
        self.entrar(pagina, CLIENTE)
        conferidos = 0
        for diferenca, proximo_passo in ESTADOS:
            self._estado(diferenca, proximo_passo)
            for largura, altura in JANELAS:
                contexto = f"{largura}x{altura}, diferença={diferenca}, próximo passo={proximo_passo}"
                with self.subTest(contexto):
                    medida = self._medir(pagina, largura, altura)
                    self.assert_no_topo(medida, contexto)
                    self.assertEqual((medida["diferenca"], medida["proximoPasso"]), (diferenca, proximo_passo),
                                     f"{contexto}: estado da página diferente do pedido")
                    if medida["naTela"]:
                        conferidos += 1
                        self.assertTrue(medida["doLink"] and not medida["doNexobot"],
                                        f"{contexto}: o ponto da borda direita é de {medida['noPonto']} ({medida})")
                    self.assert_botao_abaixo_do_texto(medida, contexto)
        # Com o botão abaixo do texto, ele sai da dobra em parte das combinações (e aí o NexoBot não o cobre);
        # nas demais, o elementFromPoint foi conferido.
        self.assertGreater(conferidos, 0)

    def test_botao_abaixo_do_texto_na_tela_larga_e_no_celular(self):
        pagina = self.abrir(*PONTAS[0])
        self.entrar(pagina, CLIENTE)
        for largura, altura in PONTAS:
            with self.subTest(largura=largura):
                medida = self._medir(pagina, largura, altura)
                self.assert_no_topo(medida, f"{largura}x{altura}")
                self.assert_botao_abaixo_do_texto(medida, f"{largura}x{altura}")


if __name__ == "__main__":
    unittest.main()
