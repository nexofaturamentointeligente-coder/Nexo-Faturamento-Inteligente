"""Configurações em navegador real (Chromium via Playwright), com a CSP real do servidor.

Envio único dos formulários e, nos dois temas, sem rolagem horizontal e com contraste AA nas
amostras do módulo. Habilitado por NEXO_TEST_BROWSER=1, como em tests/test_overflow_navegador.py.
"""

from datetime import date, datetime, timezone
import unittest

from tests.test_overflow_navegador import NavegadorCase, _BROWSER
from tests.test_tema_navegador import _CONTRASTE

ADMIN = "admin-test@example.invalid"
CLIENTE = "cliente-test@example.invalid"
PAGINA_CLIENTE = "/cliente/configuracoes"
PERFIL = "form[action$='/configuracoes/perfil']"
EXCLUSAO = "form[action$='/configuracoes/solicitar-exclusao']"


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class ConfiguracoesNavegadorTests(NavegadorCase):
    def _sessao(self, email, tema=None, largura=1366, altura=768):
        pagina = self.abrir(largura, altura)
        pagina.goto(self.base + "/auth/login", wait_until="load")
        if tema:
            pagina.evaluate("(t) => localStorage.setItem('theme', t)", tema)
        pagina.fill("#email", email)
        pagina.fill("#senha", self.password)
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("button[type=submit]")
        return pagina

    def _posts(self, pagina):
        posts = []
        pagina.on("request", lambda pedido: posts.append(pedido.url) if pedido.method == "POST" else None)
        return posts

    def test_segundo_envio_e_cancelado_e_a_pagina_recebe_o_resultado_do_primeiro(self):
        pagina = self._sessao(CLIENTE)
        self.ir(pagina, PAGINA_CLIENTE)
        posts = self._posts(pagina)
        with pagina.expect_navigation(wait_until="load"):
            cancelados = pagina.evaluate("""(seletor) => {
                const formulario = document.querySelector(seletor), vistos = [];
                window.addEventListener('submit', (evento) => vistos.push(evento.defaultPrevented));
                formulario.requestSubmit();
                formulario.requestSubmit();
                return vistos; }""", PERFIL)
        self.assertEqual(cancelados, [False, True])
        self.assertEqual(len(posts), 1)
        self.assertIn("Dados atualizados.", pagina.content())

    def test_prazo_e_volta_pelo_navegador_liberam_o_formulario(self):
        pagina = self._sessao(CLIENTE)
        self.ir(pagina, PAGINA_CLIENTE)
        estados = pagina.evaluate("""(seletor) => {
            const formulario = document.querySelector(seletor), original = window.setTimeout;
            let liberar = null;
            formulario.addEventListener('submit', (evento) => evento.preventDefault());  // o teste não navega
            window.setTimeout = (funcao, ms) => {
                if (ms >= 1000) { liberar = funcao; return 0; }
                return original(funcao, ms); };
            formulario.requestSubmit();
            window.setTimeout = original;
            const enviado = formulario.dataset.enviado;
            liberar();
            const depoisDoPrazo = formulario.dataset.enviado || null;
            formulario.requestSubmit();
            const reenviado = formulario.dataset.enviado;
            window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
            return [enviado, depoisDoPrazo, reenviado, formulario.dataset.enviado || null]; }""", PERFIL)
        self.assertEqual(estados, ["1", None, "1", None])

    def test_confirmacao_recusada_nao_trava_o_envio(self):
        pagina = self._sessao(CLIENTE)
        self.ir(pagina, PAGINA_CLIENTE)
        posts = self._posts(pagina)
        pagina.once("dialog", lambda dialogo: dialogo.dismiss())
        pagina.click(EXCLUSAO + " button[type=submit]")
        self.assertEqual(posts, [])
        self.assertIsNone(pagina.get_attribute(EXCLUSAO, "data-enviado"))
        pagina.once("dialog", lambda dialogo: dialogo.accept())
        with pagina.expect_navigation(wait_until="load"):
            pagina.click(EXCLUSAO + " button[type=submit]")
        self.assertEqual(len(posts), 1)
        self.assertIn("Solicitação registrada", pagina.content())

    def test_sem_rolagem_horizontal_nos_dois_temas(self):
        telas = {
            CLIENTE: (PAGINA_CLIENTE,),
            ADMIN: ("/admin/configuracoes", f"/admin/empresas/{self.empresa_id}/detalhes", "/admin/contratos"),
        }
        for tema in ("dark", "light"):
            for email, caminhos in telas.items():
                pagina = self._sessao(email, tema=tema, largura=320, altura=700)
                for largura in (320, 390):
                    pagina.set_viewport_size({"width": largura, "height": 700})
                    for caminho in caminhos:
                        self.ir(pagina, caminho)
                        sobra = pagina.evaluate("""() => {
                            document.querySelectorAll('details').forEach((d) => { d.open = true; });
                            return document.documentElement.scrollWidth - window.innerWidth; }""")
                        with self.subTest(tema=tema, largura=largura, caminho=caminho):
                            self.assertLessEqual(sobra, 0)
                pagina.context.close()

    def test_nome_longo_sem_espacos_nao_cria_rolagem(self):
        from models import ChamadoSuporte, MensagemSuporte, Usuario
        instante = datetime(2026, 2, 1, 13, tzinfo=timezone.utc)
        self.db.session.get(Usuario, self.cliente_id).nome = "W" * 120
        chamado = ChamadoSuporte(id_empresa=self.empresa_id, id_usuario_cliente=self.cliente_id, assunto="Dúvida fictícia",
                                 categoria="FINANCEIRO", status_chamado="ABERTO", data_abertura=instante)
        self.db.session.add(chamado)
        self.db.session.flush()
        self.db.session.add(MensagemSuporte(id_chamado=chamado.id_chamado, id_usuario_remetente=self.cliente_id,
                                            data_envio=instante, mensagem="Texto fictício"))
        self.db.session.commit()
        telas = {
            CLIENTE: ("/cliente/dashboard", PAGINA_CLIENTE, f"/cliente/tickets/{chamado.id_chamado}"),
            ADMIN: (f"/admin/tickets/{chamado.id_chamado}",),
        }
        for email, caminhos in telas.items():
            pagina = self._sessao(email)
            for largura in (1366, 1024, 992, 390, 320):
                pagina.set_viewport_size({"width": largura, "height": 700})
                for caminho in caminhos:
                    self.ir(pagina, caminho)
                    sobra = pagina.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
                    with self.subTest(largura=largura, caminho=caminho):
                        self.assertLessEqual(sobra, 0)
            pagina.context.close()

    def test_foco_e_titulo_nao_ficam_atras_da_topbar(self):
        encoberto = """() => {
            const alvo = document.activeElement;
            if (!alvo || alvo === document.body || alvo.closest('.topbar, .sidebar, .skip-link')) return null;
            const caixa = alvo.getBoundingClientRect();
            const por_cima = document.elementFromPoint(caixa.left + caixa.width / 2, caixa.top + caixa.height / 2);
            return por_cima && por_cima.closest('.topbar') ? (alvo.id || alvo.innerText) : null; }"""
        for largura, altura in ((1366, 768), (390, 844), (320, 700)):
            pagina = self._sessao(CLIENTE, largura=largura, altura=altura)
            self.ir(pagina, PAGINA_CLIENTE)
            pagina.focus("#alerta_chamado")
            for _ in range(19):
                pagina.keyboard.press("Shift+Tab")
                with self.subTest(largura=largura):
                    self.assertIsNone(pagina.evaluate(encoberto))
            self.ir(pagina, PAGINA_CLIENTE)
            pagina.focus(".skip-link")
            pagina.keyboard.press("Enter")
            titulo, topbar = pagina.evaluate("""() => [document.querySelector('.page-head h1').getBoundingClientRect().top,
                                                       document.querySelector('.topbar').getBoundingClientRect().bottom]""")
            with self.subTest(largura=largura):
                self.assertGreaterEqual(titulo, topbar)
            pagina.context.close()

    def test_foco_em_botao_da_topbar_nao_rola_a_pagina(self):
        posicao = "() => window.scrollY"
        for largura, altura in ((1366, 768), (390, 844), (320, 700)):
            pagina = self._sessao(CLIENTE, largura=largura, altura=altura)
            self.ir(pagina, PAGINA_CLIENTE)
            pagina.evaluate("() => window.scrollTo(0, 500)")
            antes = pagina.evaluate(posicao)
            pagina.evaluate("() => document.getElementById('themeToggle').focus()")
            with self.subTest(largura=largura, foco="tema"):
                self.assertLessEqual(abs(pagina.evaluate(posicao) - antes), 2)
            if largura < 992:  # fechar a gaveta devolve o foco ao botão de menu, na topbar
                pagina.evaluate("() => document.getElementById('navToggle').click()")
                pagina.wait_for_selector("#navClose", state="visible")
                pagina.keyboard.press("Escape")
                with self.subTest(largura=largura, foco="menu"):
                    self.assertLessEqual(abs(pagina.evaluate(posicao) - antes), 2)
            pagina.context.close()

    def test_icone_do_autor_fica_na_linha_do_nome(self):
        from models import ChamadoSuporte, MensagemSuporte, Usuario
        instante = datetime(2026, 2, 1, 13, tzinfo=timezone.utc)
        self.db.session.get(Usuario, self.cliente_id).nome = "Ana Lima"
        chamado = ChamadoSuporte(id_empresa=self.empresa_id, id_usuario_cliente=self.cliente_id, assunto="Dúvida fictícia",
                                 categoria="FINANCEIRO", status_chamado="ABERTO", data_abertura=instante)
        self.db.session.add(chamado)
        self.db.session.flush()
        self.db.session.add(MensagemSuporte(id_chamado=chamado.id_chamado, id_usuario_remetente=self.cliente_id,
                                            data_envio=instante, mensagem="Texto fictício"))
        self.db.session.commit()
        pagina = self._sessao(CLIENTE)
        for largura in (1366, 390, 320):
            pagina.set_viewport_size({"width": largura, "height": 700})
            self.ir(pagina, f"/cliente/tickets/{chamado.id_chamado}")
            acima = pagina.evaluate("""() => [...document.querySelectorAll('.chat-author')].filter((autor) => {
                const icone = autor.querySelector('i').getBoundingClientRect(), trecho = document.createRange();
                trecho.selectNodeContents(autor.lastChild);
                return icone.bottom <= trecho.getBoundingClientRect().top; }).length""")
            with self.subTest(largura=largura):
                self.assertEqual(acima, 0)

    def test_interruptor_desligado_tem_contraste_nos_dois_temas(self):
        for tema, cor in (("dark", "a49cc4"), ("light", "5f5a78")):
            pagina = self._sessao(CLIENTE, tema=tema)
            self.ir(pagina, PAGINA_CLIENTE)
            fundo = pagina.evaluate("""() => {
                const interruptor = document.querySelector('#alerta_chamado');
                interruptor.checked = false;
                return getComputedStyle(interruptor).backgroundImage; }""")
            with self.subTest(tema=tema):
                self.assertIn(cor, fundo)
            pagina.context.close()

    def test_resposta_longa_do_nexobot_abre_mostrando_o_comeco(self):
        pagina = self._sessao(CLIENTE)
        self.ir(pagina, "/cliente/dashboard")
        pagina.click("#nexobotLauncher")
        pagina.wait_for_selector("#nexobotPanel:not([hidden])")
        pagina.fill("#nexobotInput", "Como exporto meus dados?")
        pagina.keyboard.press("Enter")
        pagina.wait_for_function("() => document.querySelectorAll('.nb-msg.nb-bot:not(.nb-typing)').length >= 2")
        pagina.wait_for_timeout(800)  # a rolagem da conversa é suave
        distancia = pagina.evaluate("""() => {
            const caixa = document.getElementById('nexobotMsgs');
            const bolhas = caixa.querySelectorAll('.nb-msg.nb-bot:not(.nb-typing) .nb-bubble');
            return bolhas[bolhas.length - 1].getBoundingClientRect().top - caixa.getBoundingClientRect().top; }""")
        self.assertGreaterEqual(distancia, 0)

    def test_menu_segue_marcado_depois_de_erro_ao_publicar(self):
        pagina = self._sessao(ADMIN)
        self.ir(pagina, "/admin/configuracoes")
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("form[action$='/configuracoes/documentos'] button[type=submit]")
        self.assertIn("Escreva o texto do documento", pagina.content())
        self.assertEqual(pagina.inner_text(".sidebar .nav-item.active").strip(), "Configurações")
        self.erros_js = [e for e in self.erros_js if "400" not in e]  # a recusa responde 400 de propósito

    def test_emails_cabem_em_tela_estreita(self):
        from flask import render_template
        from types import SimpleNamespace
        with self.app.test_request_context("/"):
            documento = render_template("emails/email_alterado.html", usuario=SimpleNamespace(nome="W" * 120),
                                        email_novo_mascarado="no***@example.invalid", pela_equipe=False)
        pagina = self.abrir(320, 700)
        for largura in (320, 390):
            pagina.set_viewport_size({"width": largura, "height": 700})
            pagina.set_content(documento, wait_until="load")
            sobra = pagina.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            with self.subTest(largura=largura):
                self.assertLessEqual(sobra, 0)

    def test_contraste_das_amostras_do_modulo_nos_dois_temas(self):
        from models import DocumentoLegal
        self.contratar(data_inicio=date(2026, 1, 1), data_fim=date(2026, 3, 10))
        self.ativar_regras()
        self.db.session.add(DocumentoLegal(tipo="TERMOS_USO", versao=1, conteudo="Texto fictício dos Termos de Uso.",
                                           publicado_em=datetime(2026, 2, 1, 13, tzinfo=timezone.utc), id_usuario_admin=self.admin_id))
        self.db.session.commit()
        amostras = {
            (CLIENTE, PAGINA_CLIENTE): (".config-secao-titulo", ".nexo-label", ".nexo-label .req", ".field-hint", ".form-control",
                                        ".dados-contrato dt", ".dados-contrato dd", ".btn-ghost", ".config-documento-topo h4",
                                        ".config-documento summary", ".config-documento-texto"),
            (CLIENTE, "/cliente/dashboard"): (".aviso-contrato strong", ".aviso-contrato p", ".aviso-contrato a"),
            (ADMIN, "/admin/configuracoes"): (".config-documento-topo h4", ".nexo-card-head .sub", ".nexo-label", ".form-control",
                                              ".field-hint", ".dados-contrato dt", ".dados-contrato dd"),
            (ADMIN, f"/admin/empresas/{self.empresa_id}/detalhes"): (".usuario-corrigir-email summary", ".usuario-corrigir-email .field-hint",
                                                                     ".usuario-corrigir-email .nexo-label"),
            (ADMIN, "/admin/contratos"): (".nexo-card-head .sub", ".field-hint"),
        }
        for tema in ("dark", "light"):
            sessoes = {}
            for (email, caminho), seletores in amostras.items():
                if email not in sessoes:
                    sessoes[email] = self._sessao(email, tema=tema)
                pagina = sessoes[email]
                self.ir(pagina, caminho)
                pagina.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")
                for medida in pagina.evaluate(_CONTRASTE, list(seletores)):
                    with self.subTest(tema=tema, caminho=caminho, seletor=medida["seletor"]):
                        self.assertNotIn("ausente", medida, "amostra não encontrada na página")
                        self.assertGreaterEqual(medida["razao"], medida["minimo"], medida)
            for pagina in sessoes.values():
                pagina.context.close()


if __name__ == "__main__":
    unittest.main()
