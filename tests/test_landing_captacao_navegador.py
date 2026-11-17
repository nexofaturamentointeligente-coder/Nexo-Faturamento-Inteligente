"""Cadastro do lead e pré-cadastro da landing no navegador real (Chromium/Edge via Playwright).

O que só o navegador mostra (decisões do dono, 16/11/2026):
  - a regra da rolagem: cada "Li e aceito" nasce desabilitado e só libera no fim do texto do documento (rolando
    com o mouse ou pelo teclado), ou na hora, se o texto cabe sem rolagem; sem documentos publicados, nada é
    enviado;
  - o envio ao microsserviço (POST multipart, sem credenciais) e o tratamento de cada resposta: 201 (protocolo e a
    situação de cada relatório), 400 (o campo marcado), 409 (os termos mudaram: leitura de novo), 429 (quando
    tentar), 503 e erro de rede (serviço indisponível, com o WhatsApp ou o e-mail);
  - os relatórios conferidos antes do envio (formato, arquivo vazio), para não gastar o limite do serviço;
  - o estado "enviando": botões travados, aviso de até 1 minuto, um envio só, e o diálogo não fecha.
A rede é só local: /documentos-legais/vigentes responde um texto neutro de teste e o microsserviço é
interceptado (page.route) com respostas falsas. Habilitado por NEXO_TEST_BROWSER=1, como na CI.
"""

import os
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.deploy_support import DeployTestCase
from tests.landing_support import (ROTA_DO_LEAD, SERVICO, campos_multipart, copia_local, documentos,
                                   responder_json)
from tests.test_nexobot_navegador import _CONECTAR_LOCAL

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

_BROWSER = os.environ.get("NEXO_TEST_BROWSER") == "1" and sync_playwright is not None
NUMERO = "5562988887777"
EMAIL = "suportenexocolab@gmail.com"
_COLETOR_CSP = """
window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__cspViolations.push({diretiva: e.violatedDirective, origem: e.blockedURI});
});
"""
_FORA_DO_DIALOGO = """(id) => {
    const caixa = document.getElementById(id).getBoundingClientRect();
    return [...document.querySelectorAll('#' + id + ' a, #' + id + ' button, #' + id + ' input:not([tabindex="-1"]),'
        + ' #' + id + ' select, #' + id + ' textarea')].filter(el => {
        const b = el.getBoundingClientRect();
        return b.width > 1 && (b.left < caixa.left - 1 || b.right > caixa.right + 1);
    }).map(el => el.id || el.className || el.tagName);
}"""
CRIADO = {"status": "recebido", "protocolo": "A1B2C3D4", "interesse": "so_contato", "status_arquivo": "sem_arquivo",
          "arquivos": []}


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class CaptacaoNavegadorTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        from werkzeug.serving import make_server
        for alvo, original in _CONECTAR_LOCAL.items():
            self.stack.enter_context(patch(alvo, original))
        self.app.config.update(EMAIL_SUPORTE=EMAIL, CNPJ_EMPRESA="", MICROSSERVICO_RAIOX_URL=SERVICO,
                               WHATSAPP_EMPRESA_NUMERO="")
        self.servidor = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.servidor.server_port}"
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.addCleanup(self._parar_servidor)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.navegador = self.pw.chromium.launch(executable_path=os.environ.get("NEXO_TEST_CHROMIUM") or None)
        self.addCleanup(self.navegador.close)
        self.erros = []
        self.envios = []          # (campos, arquivos) de cada POST ao serviço
        self.respostas = []       # o que o serviço responde, na ordem: (status, corpo[, cabeçalhos]), "rede" ou "pendente"
        self.pendentes = []       # rotas guardadas para responder depois (estado "enviando")
        self.documentos = (200, documentos())
        self.leituras_dos_documentos = 0

    def _parar_servidor(self):
        self.servidor.shutdown()
        self.servidor.server_close()

    def tearDown(self):
        self.assertEqual(self.erros, [], "Exceções JavaScript na landing")

    # ------------------------------------------------------------ rede local e respostas falsas
    def _rota(self, rota):
        pedido = rota.request
        url = pedido.url
        if url.startswith(self.base + "/documentos-legais/vigentes"):
            self.leituras_dos_documentos += 1
            status, corpo = self.documentos
            responder_json(rota, corpo, status)
        elif url.startswith(self.base):
            rota.continue_()
        elif url.startswith(SERVICO):
            self._servico(rota)
        elif copia_local(url):
            caminho, tipo = copia_local(url)
            rota.fulfill(status=200, path=caminho, content_type=tipo)
        elif pedido.resource_type == "stylesheet":
            rota.fulfill(status=200, content_type="text/css", body="")
        else:
            rota.abort()

    def _servico(self, rota):
        pedido = rota.request
        if not pedido.url.startswith(ROTA_DO_LEAD):
            responder_json(rota, {"status": "alive"})
            return
        # Conferências registradas (uma exceção aqui deixaria o pedido sem resposta até o prazo do script).
        if pedido.method != "POST":
            self.erros.append(f"método {pedido.method} no serviço")
        if not pedido.headers.get("content-type", "").startswith("multipart/form-data; boundary="):
            self.erros.append("envio sem multipart/form-data")
        if "cookie" in {nome.lower() for nome in pedido.headers}:
            self.erros.append("envio com cookie (credenciais)")
        self.envios.append(campos_multipart(pedido))
        resposta = self.respostas.pop(0)
        if resposta == "rede":
            rota.abort()
        elif resposta == "pendente":
            self.pendentes.append(rota)
        else:
            status, corpo = resposta[:2]
            cabecalhos = {"Access-Control-Allow-Origin": self.base, "Access-Control-Expose-Headers": "Retry-After"}
            cabecalhos.update(resposta[2] if len(resposta) > 2 else {})
            responder_json(rota, corpo, status, cabecalhos)

    def abrir(self, largura=1366, altura=900):
        contexto = self.navegador.new_context(viewport={"width": largura, "height": altura}, reduced_motion="reduce",
                                              locale="pt-BR", timezone_id="America/Sao_Paulo")
        self.addCleanup(contexto.close)
        pagina = contexto.new_page()
        pagina.on("pageerror", lambda erro: self.erros.append(str(erro)))
        pagina.add_init_script(_COLETOR_CSP)
        pagina.route("**/*", self._rota)
        pagina.goto(self.base + "/", wait_until="load")
        return pagina

    # ------------------------------------------------------------ passos do cadastro do lead
    def abrir_cadastro(self, pagina):
        pagina.locator(".header-actions [data-cadastro-gatilho]").click()
        pagina.locator("#cadastro-lead").wait_for(state="visible")

    def rolar_ate_o_fim(self, pagina, dialogo, chave):
        pagina.locator(f"#{dialogo} [data-termo='{chave}'] [data-termo-texto]").evaluate(
            "el => { el.scrollTop = el.scrollHeight; }")

    def aceitar_termos(self, pagina, dialogo="cadastro-lead", continuar="#cadastro-lead-continuar"):
        pagina.locator(f"#{dialogo} [data-termo='termos'] [data-termo-texto]").wait_for(state="visible")
        for chave in ("termos", "privacidade"):
            self.rolar_ate_o_fim(pagina, dialogo, chave)
            caixa = pagina.locator(f"#{dialogo}-check-{chave}")
            pagina.wait_for_function("id => !document.getElementById(id).disabled", arg=f"{dialogo}-check-{chave}")
            caixa.check()
        pagina.locator(continuar).click()

    def preencher_dados(self, pagina, email="maria@example.invalid"):
        pagina.fill("#cadastro-lead-nome", "Maria Fictícia")
        pagina.fill("#cadastro-lead-empresa", "Loja Exemplo")
        pagina.fill("#cadastro-lead-email", email)
        pagina.fill("#cadastro-lead-telefone", "")
        pagina.locator("#cadastro-lead-telefone").press_sequentially("61988887777")
        pagina.locator("#cadastro-lead-form button[type=submit]").click()

    def aviso(self, pagina, etapa):
        return pagina.locator(f"{etapa} [data-aviso]")

    def assert_sem_csp(self, pagina):
        self.assertEqual(pagina.evaluate("window.__cspViolations"), [])

    # ------------------------------------------------------------ cenários
    def test_regra_da_rolagem_libera_cada_caixa_so_no_fim_do_texto(self):
        pagina = self.abrir(390, 844)
        self.abrir_cadastro(pagina)
        texto_termos = pagina.locator("#cadastro-lead [data-termo='termos'] [data-termo-texto]")
        texto_termos.wait_for(state="visible")
        termos = pagina.locator("#cadastro-lead-check-termos")
        privacidade = pagina.locator("#cadastro-lead-check-privacidade")
        continuar = pagina.locator("#cadastro-lead-continuar")
        self.assertTrue(termos.is_disabled() and privacidade.is_disabled() and continuar.is_disabled())
        self.assertIn("v1.0", pagina.locator("#cadastro-lead [data-termo='termos'] [data-termo-versao]").inner_text())
        self.assertIn("16/11/2026", pagina.locator("#cadastro-lead [data-termo='termos'] [data-termo-versao]").inner_text())
        # O texto entra como texto (quebras preservadas), num contêiner com rolagem.
        self.assertGreater(texto_termos.evaluate("el => el.scrollHeight - el.clientHeight"), 50)
        self.assertEqual(texto_termos.evaluate("el => el.children.length"), 0)
        # Até a metade não basta.
        texto_termos.evaluate("el => { el.scrollTop = (el.scrollHeight - el.clientHeight) / 2; }")
        pagina.wait_for_timeout(150)
        self.assertTrue(termos.is_disabled())
        # No fim, só a caixa daquele documento libera.
        self.rolar_ate_o_fim(pagina, "cadastro-lead", "termos")
        pagina.wait_for_function("() => !document.getElementById('cadastro-lead-check-termos').disabled")
        self.assertTrue(privacidade.is_disabled())
        self.assertIn("chegou ao fim", pagina.locator("#cadastro-lead-termos-dica").inner_text())
        termos.check()
        self.assertTrue(continuar.is_disabled())
        # Pelo teclado: o contêiner recebe o foco e o End leva ao fim do texto.
        texto_privacidade = pagina.locator("#cadastro-lead [data-termo='privacidade'] [data-termo-texto]")
        texto_privacidade.focus()
        pagina.keyboard.press("End")
        pagina.wait_for_function("() => !document.getElementById('cadastro-lead-check-privacidade').disabled")
        privacidade.check()
        self.assertTrue(continuar.is_enabled())
        # Desmarcar volta a travar o avanço.
        termos.uncheck()
        self.assertTrue(continuar.is_disabled())
        self.assert_sem_csp(pagina)
        self.assertEqual(self.envios, [])

    def test_texto_que_cabe_sem_rolagem_libera_na_hora(self):
        self.documentos = (200, documentos(paragrafos_termos=1, paragrafos_privacidade=1))
        pagina = self.abrir()
        self.abrir_cadastro(pagina)
        pagina.wait_for_function("() => !document.getElementById('cadastro-lead-check-termos').disabled"
                                 " && !document.getElementById('cadastro-lead-check-privacidade').disabled")
        texto = pagina.locator("#cadastro-lead [data-termo='termos'] [data-termo-texto]")
        self.assertLessEqual(texto.evaluate("el => el.scrollHeight - el.clientHeight"), 2)
        # Fechar sem enviar guarda o que foi digitado; ao reabrir, os termos vigentes são buscados e lidos de novo.
        self.aceitar_termos(pagina)
        pagina.fill("#cadastro-lead-nome", "Maria Fictícia")
        pagina.keyboard.press("Escape")
        pagina.locator("#cadastro-lead").wait_for(state="hidden")
        self.abrir_cadastro(pagina)
        self.assertTrue(pagina.locator("#cadastro-lead-etapa-1").is_visible())
        pagina.wait_for_function("() => !document.getElementById('cadastro-lead-check-termos').disabled")
        self.assertFalse(pagina.locator("#cadastro-lead-check-termos").is_checked())
        self.assertEqual(self.leituras_dos_documentos, 2)
        self.aceitar_termos(pagina)
        self.assertEqual(pagina.input_value("#cadastro-lead-nome"), "Maria Fictícia")
        self.assertEqual(self.envios, [])

    def test_sem_documentos_publicados_avisa_e_nao_envia_nada(self):
        for status, corpo, mensagem in ((200, {"documentos": []}, "ainda não foram publicados"),
                                        (404, {"erro": "não encontrado"}, "ainda não foram publicados"),
                                        (503, {"documentos": [], "erro": "indisponivel"}, "Não foi possível carregar")):
            with self.subTest(status=status):
                self.documentos = (status, corpo)
                pagina = self.abrir()
                self.abrir_cadastro(pagina)
                aviso = self.aviso(pagina, "#cadastro-lead-etapa-1")
                aviso.locator("p").first.wait_for(state="visible")
                self.assertIn(mensagem, aviso.inner_text())
                self.assertTrue(pagina.locator("#cadastro-lead-continuar").is_disabled())
                self.assertTrue(pagina.locator("#cadastro-lead [data-termo='termos']").is_hidden())
                # Sem número: o e-mail de suporte como alternativa, e nenhuma menção ao WhatsApp.
                self.assertEqual(aviso.locator("a").get_attribute("href"), f"mailto:{EMAIL}")
                self.assertNotIn("WhatsApp", aviso.inner_text())
                if status == 503:
                    # Indisponível: "Tentar de novo" busca os documentos outra vez.
                    self.documentos = (200, documentos())
                    aviso.get_by_role("button", name="Tentar de novo").click()
                    pagina.locator("#cadastro-lead [data-termo='termos'] [data-termo-texto]").wait_for(state="visible")
                    self.assertEqual(aviso.inner_text().strip(), "")
                pagina.context.close()
        self.assertEqual(self.envios, [])

    def test_cadastro_so_com_o_contato_grava_no_servico_e_mostra_o_protocolo(self):
        self.respostas = [(201, CRIADO)]
        pagina = self.abrir(320, 640)
        gatilho = pagina.locator(".header-actions [data-cadastro-gatilho]")
        self.assertEqual((gatilho.get_attribute("role"), gatilho.get_attribute("aria-haspopup")), ("button", "dialog"))
        self.abrir_cadastro(pagina)
        self.assertEqual(pagina.evaluate("document.activeElement.id"), "cadastro-lead-titulo-1")
        self.aceitar_termos(pagina)
        self.assertEqual(pagina.evaluate("document.activeElement.id"), "cadastro-lead-nome")
        # O telefone usa a mesma máscara e a mesma regra do pré-cadastro (a regra roda no envio, depois da
        # validação nativa dos outros campos).
        pagina.fill("#cadastro-lead-telefone", "+55 61 98888-7777")
        self.assertEqual(pagina.input_value("#cadastro-lead-telefone"), "(61) 98888-7777")
        pagina.fill("#cadastro-lead-nome", "Maria Fictícia")
        pagina.fill("#cadastro-lead-empresa", "Loja Exemplo")
        pagina.fill("#cadastro-lead-email", "maria@example.invalid")
        pagina.fill("#cadastro-lead-telefone", "3222-1234 21")
        pagina.locator("#cadastro-lead-form button[type=submit]").click()
        self.assertTrue(pagina.locator("#cadastro-lead-etapa-2").is_visible())
        self.assertEqual(pagina.eval_on_selector("#cadastro-lead-telefone", "el => el.validationMessage"),
                         "Informe um telefone do Brasil com DDD: 10 ou 11 números.")
        # E-mail sem domínio completo também não passa (a forma que o serviço aceita).
        pagina.fill("#cadastro-lead-telefone", "(61) 98888-7777")
        pagina.fill("#cadastro-lead-email", "maria@exemplo")
        pagina.locator("#cadastro-lead-form button[type=submit]").click()
        self.assertTrue(pagina.locator("#cadastro-lead-etapa-2").is_visible())
        self.assertEqual(pagina.eval_on_selector("#cadastro-lead-email", "el => el.validationMessage"),
                         "Informe um e-mail válido.")
        self.preencher_dados(pagina, email="Maria@Example.invalid")
        pagina.locator("#cadastro-lead-etapa-3").wait_for(state="visible")
        self.assertEqual(pagina.evaluate(_FORA_DO_DIALOGO, "cadastro-lead"), [])
        pagina.get_by_role("button", name="Agora não, só quero contato").click()
        pagina.locator("#cadastro-lead-etapa-4").wait_for(state="visible")
        self.assertEqual(pagina.locator("#cadastro-lead-protocolo").inner_text().strip(), "A1B2C3D4")
        self.assertTrue(pagina.locator("#cadastro-lead-resultado-arquivos").is_hidden())
        self.assertIn("Próximos passos", pagina.locator("#cadastro-lead-etapa-4").inner_text())
        [(campos, arquivos)] = self.envios
        self.assertEqual(arquivos, [])
        self.assertEqual(campos, {"nome": "Maria Fictícia", "empresa": "Loja Exemplo", "email": "maria@example.invalid",
                                  "whatsapp": "(61) 98888-7777", "interesse": "3", "termos_aceitos": "true",
                                  "documento_termos": "11", "documento_privacidade": "12", "campo_isca": ""})
        self.assertEqual(pagina.evaluate(_FORA_DO_DIALOGO, "cadastro-lead"), [])
        # Fechar devolve o foco ao "Cadastre-se"; abrir de novo começa um cadastro novo, pelos termos.
        pagina.get_by_role("button", name="Fechar", exact=True).click()
        pagina.locator("#cadastro-lead").wait_for(state="hidden")
        self.assertEqual(pagina.evaluate("document.activeElement.getAttribute('data-cadastro-gatilho')"), "")
        self.abrir_cadastro(pagina)
        self.assertTrue(pagina.locator("#cadastro-lead-etapa-1").is_visible())
        self.assertEqual(pagina.input_value("#cadastro-lead-nome"), "")
        self.assert_sem_csp(pagina)

    def test_relatorios_conferidos_no_navegador_antes_de_gastar_o_envio(self):
        self.respostas = [(201, {"status": "recebido", "protocolo": "E5F6A7B8", "interesse": "raio_x",
                                 "status_arquivo": "parcial", "arquivos": [
                                     {"nome": "vendas.csv", "situacao": "aprovado"},
                                     {"nome": "compras.xlsx", "situacao": "recusado",
                                      "motivo": "O conteúdo não é uma planilha .xlsx."}]})]
        pagina = self.abrir()
        self.abrir_cadastro(pagina)
        self.aceitar_termos(pagina)
        self.preencher_dados(pagina)
        etapa = "#cadastro-lead-etapa-3"
        enviar = pagina.locator(f"{etapa} [data-enviar='1']")
        # Sem relatório, o envio com relatórios não sai.
        enviar.click()
        self.assertIn("Escolha ao menos um relatório", self.aviso(pagina, etapa).inner_text())
        # .xlsm e arquivo vazio são recusados aqui, com o motivo; o envio não sai enquanto eles estiverem na lista.
        pagina.set_input_files("#cadastro-lead-arquivos", [
            {"name": "macro.xlsm", "mimeType": "application/vnd.ms-excel.sheet.macroEnabled.12", "buffer": b"PK\x03\x04x"},
            {"name": "vazio.csv", "mimeType": "text/csv", "buffer": b""}])
        recusados = pagina.locator(f"{etapa} .lp-arquivo.is-recusado")
        self.assertEqual(recusados.count(), 2)
        self.assertIn("Arquivo com macros (.xlsm) não é aceito", recusados.nth(0).inner_text())
        self.assertIn("O arquivo está vazio.", recusados.nth(1).inner_text())
        enviar.click()
        self.assertIn("Remova os arquivos marcados antes de enviar.", self.aviso(pagina, etapa).inner_text())
        self.assertEqual(self.envios, [])
        for _ in range(2):
            pagina.locator(f"{etapa} .lp-arquivo.is-recusado .lp-arquivo-remover").first.click()
        # Os aceitos se somam a cada escolha.
        pagina.set_input_files("#cadastro-lead-arquivos", [
            {"name": "vendas.csv", "mimeType": "text/csv", "buffer": b"codigo;produto;total\n1;Teste;10,00\n"}])
        pagina.set_input_files("#cadastro-lead-arquivos", [
            {"name": "compras.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "buffer": b"PK\x03\x04" + b"0" * 64}])
        self.assertEqual(pagina.locator(f"{etapa} .lp-arquivo").count(), 2)
        enviar.click()
        pagina.locator("#cadastro-lead-etapa-4").wait_for(state="visible")
        [(campos, arquivos)] = self.envios
        self.assertEqual(campos["interesse"], "1")
        self.assertEqual([(nome, arquivo) for nome, arquivo, _ in arquivos],
                         [("arquivos", "vendas.csv"), ("arquivos", "compras.xlsx")])
        linhas = pagina.locator("#cadastro-lead-resultado-arquivos li")
        self.assertEqual(linhas.count(), 2)
        self.assertIn("Aceito", linhas.nth(0).inner_text())
        self.assertIn("Recusado", linhas.nth(1).inner_text())
        self.assertIn("O conteúdo não é uma planilha .xlsx.", linhas.nth(1).inner_text())
        self.assertIn("parte dos relatórios", pagina.locator("#cadastro-lead-resultado-texto").inner_text())
        self.assert_sem_csp(pagina)

    def test_cada_resposta_de_erro_do_servico_tem_o_seu_caminho(self):
        self.respostas = [
            (503, {"erro": "Serviço indisponível no momento. Tente de novo em instantes."}, {"Retry-After": "30"}),
            "rede",
            (429, {"erro": "Muitos envios deste endereço. Tente mais tarde."}, {"Retry-After": "120"}),
            (400, {"erro": "Informe um e-mail válido.", "campo": "email"}),
            (409, {"erro": "Os termos foram atualizados enquanto você lia. Leia a versão atual e confirme de novo.",
                   "campo": "termos_aceitos"}),
            (201, CRIADO),
        ]
        pagina = self.abrir()
        self.abrir_cadastro(pagina)
        self.aceitar_termos(pagina)
        self.preencher_dados(pagina)
        etapa = "#cadastro-lead-etapa-3"
        so_contato = pagina.get_by_role("button", name="Agora não, só quero contato")
        aviso = self.aviso(pagina, etapa)

        def enviar_e_ler():
            quantos = len(self.envios)
            # O clique já deixa a etapa "enviando" (aria-busy="true"); a resposta a devolve a "false".
            so_contato.click()
            pagina.wait_for_function("() => document.getElementById('cadastro-lead-etapa-3').getAttribute('aria-busy')"
                                     " === 'false'")
            self.assertEqual(len(self.envios), quantos + 1)
            return aviso.inner_text()

        # 503: o motivo do serviço e, sem número, o e-mail de suporte.
        texto = enviar_e_ler()
        self.assertIn("Serviço indisponível no momento", texto)
        self.assertEqual(aviso.locator("a").get_attribute("href"), f"mailto:{EMAIL}")
        # Erro de rede (serviço dormindo, fora do ar ou origem fora do CORS): serviço indisponível.
        texto = enviar_e_ler()
        self.assertIn("Não foi possível falar com o serviço de cadastro agora", texto)
        # 429: quando tentar de novo, pelo Retry-After.
        texto = enviar_e_ler()
        self.assertIn("Muitos envios deste endereço. Tente de novo em cerca de 2 minutos.", texto)
        # 400 de um campo: volta aos dados, com o campo marcado.
        so_contato.click()
        pagina.locator("#cadastro-lead-etapa-2").wait_for(state="visible")
        self.assertEqual(pagina.evaluate("document.activeElement.id"), "cadastro-lead-email")
        self.assertEqual(pagina.eval_on_selector("#cadastro-lead-email", "el => el.validationMessage"),
                         "Informe um e-mail válido.")
        self.assertIn("Confira o campo marcado", self.aviso(pagina, "#cadastro-lead-etapa-2").inner_text())
        pagina.fill("#cadastro-lead-email", "outra@example.invalid")
        pagina.locator("#cadastro-lead-form button[type=submit]").click()
        pagina.locator(etapa).wait_for(state="visible")
        # 409: os termos mudaram; o diálogo volta à leitura, com os documentos buscados de novo e as caixas zeradas.
        leituras = self.leituras_dos_documentos
        self.documentos = (200, documentos(ids=(21, 22), rotulo="v1.1"))
        so_contato.click()
        pagina.locator("#cadastro-lead-etapa-1").wait_for(state="visible")
        aviso_termos = self.aviso(pagina, "#cadastro-lead-etapa-1")
        aviso_termos.locator("p").wait_for(state="visible")
        self.assertIn("Os termos foram atualizados enquanto você lia", aviso_termos.inner_text())
        self.assertEqual(self.leituras_dos_documentos, leituras + 1)
        self.assertFalse(pagina.locator("#cadastro-lead-check-termos").is_checked())
        self.assertIn("v1.1", pagina.locator("#cadastro-lead [data-termo='termos'] [data-termo-versao]").inner_text())
        self.aceitar_termos(pagina)
        pagina.locator("#cadastro-lead-form button[type=submit]").click()
        so_contato.click()
        pagina.locator("#cadastro-lead-etapa-4").wait_for(state="visible")
        # O envio final leva os ids da versão nova.
        campos, _ = self.envios[-1]
        self.assertEqual((campos["documento_termos"], campos["documento_privacidade"], campos["email"]),
                         ("21", "22", "outra@example.invalid"))
        self.assertEqual(len(self.envios), 6)
        self.assert_sem_csp(pagina)

    def test_enviando_trava_os_botoes_avisa_a_demora_e_nao_fecha(self):
        self.respostas = ["pendente"]
        pagina = self.abrir()
        self.abrir_cadastro(pagina)
        self.aceitar_termos(pagina)
        self.preencher_dados(pagina)
        etapa = "#cadastro-lead-etapa-3"
        pagina.set_input_files("#cadastro-lead-arquivos", [
            {"name": "vendas.csv", "mimeType": "text/csv", "buffer": b"codigo;produto;total\n1;Teste;10,00\n"}])
        pagina.locator(f"{etapa} [data-enviar='1']").click()
        pagina.wait_for_function("() => document.getElementById('cadastro-lead-etapa-3').getAttribute('aria-busy')"
                                 " === 'true'")
        ocupado = pagina.locator(f"{etapa} [data-ocupado]")
        self.assertTrue(ocupado.is_visible())
        self.assertIn("pode levar até 1 minuto", ocupado.inner_text())
        for botao in pagina.locator(f"{etapa} [data-acao]").all():
            self.assertTrue(botao.is_disabled())
        self.assertTrue(pagina.locator("#cadastro-lead .precadastro-fechar").is_disabled())
        self.assertTrue(pagina.locator("#cadastro-lead-arquivos").is_disabled())
        # Durante o envio, o Esc não fecha o diálogo.
        pagina.keyboard.press("Escape")
        pagina.wait_for_timeout(150)
        self.assertTrue(pagina.locator("#cadastro-lead").is_visible())
        self.assertEqual(len(self.envios), 1)
        [rota] = self.pendentes
        responder_json(rota, dict(CRIADO, interesse="raio_x", status_arquivo="aprovado",
                                  arquivos=[{"nome": "vendas.csv", "situacao": "aprovado"}]), 201,
                       {"Access-Control-Allow-Origin": self.base})
        pagina.locator("#cadastro-lead-etapa-4").wait_for(state="visible")
        self.assertTrue(pagina.locator("#cadastro-lead .precadastro-fechar").is_enabled())
        self.assertEqual(len(self.envios), 1)

    def test_precadastro_com_relatorios_oferece_o_whatsapp_na_falha_e_no_resultado(self):
        self.app.config["WHATSAPP_EMPRESA_NUMERO"] = NUMERO
        self.respostas = [(503, {"erro": "Serviço indisponível no momento. Tente de novo em instantes."}),
                          (201, {"status": "recebido", "protocolo": "C0FFEE12", "interesse": "raio_x",
                                 "status_arquivo": "aprovado", "arquivos": [{"nome": "vendas.csv",
                                                                             "situacao": "aprovado"}]})]
        pagina = self.abrir(390, 844)
        pagina.locator("[data-precadastro-gatilho] a").click()
        pagina.locator("#precadastro").wait_for(state="visible")
        self.aceitar_termos(pagina, "precadastro", "#btn-next-1")
        pagina.fill("#lead-nome", "Maria Fictícia")
        pagina.fill("#lead-empresa", "Loja Exemplo")
        pagina.locator("#lead-whatsapp").press_sequentially("62988881234")
        pagina.select_option("#lead-plano", "Ouro")
        pagina.fill("#lead-email", "maria@example.invalid")
        pagina.locator("#btn-next-2").click()
        pagina.locator("#btn-raiox-fazer").click()
        pagina.locator("#step-4").wait_for(state="visible")
        # O painel de envio tem o texto de apoio e a ajuda pelo WhatsApp.
        ajuda = pagina.locator("#step-4 [data-ajuda-whatsapp]")
        self.assertTrue(ajuda.is_visible())
        self.assertTrue(ajuda.get_attribute("href").startswith(f"https://wa.me/{NUMERO}?text="))
        pagina.set_input_files("#precadastro-arquivos", [
            {"name": "vendas.csv", "mimeType": "text/csv", "buffer": b"codigo;produto;total\n1;Teste;10,00\n"}])
        pagina.locator("#btn-raiox-enviar").click()
        aviso = self.aviso(pagina, "#step-4")
        aviso.locator("p").first.wait_for(state="visible")
        self.assertIn("Serviço indisponível no momento", aviso.inner_text())
        # O contato não se perde: o WhatsApp com a mensagem, sem protocolo.
        falha = parse_qs(urlsplit(aviso.locator("a").get_attribute("href")).query)["text"][0]
        self.assertIn("Plano de interesse: Ouro", falha)
        self.assertIn("Situação do Raio-X: Envio dos relatórios pelo site não concluído", falha)
        self.assertNotIn("Protocolo:", falha)
        self.assertEqual(aviso.locator("a").get_attribute("target"), "_blank")
        pagina.locator("#btn-raiox-enviar").click()
        pagina.locator("#step-5").wait_for(state="visible")
        self.assertEqual(pagina.locator("#precadastro-protocolo").inner_text().strip(), "C0FFEE12")
        resultado = parse_qs(urlsplit(pagina.locator("#precadastro-whatsapp").get_attribute("href")).query)["text"][0]
        self.assertTrue(resultado.endswith("Situação do Raio-X: Relatórios enviados pelo site\nSolicitou auxílio: Não\n"
                                           "Protocolo: C0FFEE12\nOrigem: Landing Page da NEXO"), resultado)
        self.assertEqual(len(self.envios), 2)
        campos, arquivos = self.envios[-1]
        self.assertEqual((campos["interesse"], campos["whatsapp"]), ("1", "(62) 98888-1234"))
        self.assertEqual([arquivo for _, arquivo, _ in arquivos], ["vendas.csv"])
        self.assertNotIn("plano", campos)
        self.assertEqual(pagina.evaluate(_FORA_DO_DIALOGO, "precadastro"), [])
        self.assert_sem_csp(pagina)


if __name__ == "__main__":
    unittest.main()
