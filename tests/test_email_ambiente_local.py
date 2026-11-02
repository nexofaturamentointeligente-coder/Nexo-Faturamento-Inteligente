"""E-mail em ambiente local: SMTP pelo .env, links locais e Diagnóstico de e-mail.

Executar da raiz: python -B -m unittest tests.test_email_ambiente_local -v

Nenhuma mensagem real é enviada e não há rede: o SMTP é simulado e qualquer
conexão de socket falha o teste. Endereços são fictícios (.invalid).
"""

import io
import json
from pathlib import Path
import re
import smtplib
import socket
import ssl
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from dotenv import dotenv_values

import emails
import extensions
from tests.test_auth_tokens_security import NEW_PASSWORD, OLD_PASSWORD
from tests.test_disparador_emails import _GmailBase, _Subprocesso
from tests.test_emails_transacionais import ROOT, _EmailCase

SENHA_SMTP = "PRIVATE_SENHA_DE_APP_SENTINELA"
USUARIO_SMTP = "conta-nexo@example.invalid"
ORIGEM_LOCAL = "http://127.0.0.1:5000"
_CITA_PLATAFORMA = re.compile(r"\bRender\b|RENDER|onrender")


class ModuloSemPlataformaTests(unittest.TestCase):
    def test_modulo_de_emails_nao_cita_a_plataforma_de_hospedagem(self):
        for arquivo in ("emails.py", "contas_email.py", "extensions.py", "templates/admin/diagnostico_email.html"):
            with self.subTest(arquivo=arquivo):
                texto = (ROOT / arquivo).read_text(encoding="utf-8")
                self.assertEqual(_CITA_PLATAFORMA.findall(texto), [])

    def test_modelo_do_env_traz_as_variaveis_de_email_sem_citar_a_plataforma(self):
        texto = (ROOT / ".env.example").read_text(encoding="utf-8")
        bloco = texto[texto.index("# E-mail: SMTP padrão"):texto.index("NEXO_SMTP_PERMITIDO=0")]
        self.assertEqual(_CITA_PLATAFORMA.findall(bloco), [])
        valores = dotenv_values(stream=io.StringIO(texto))
        for nome in ("MAIL_SERVER", "MAIL_PORT", "MAIL_USE_TLS", "MAIL_USE_SSL", "MAIL_USERNAME", "MAIL_PASSWORD",
                     "MAIL_DEFAULT_SENDER", "BREVO_API_KEY"):
            self.assertIn(nome, valores)
        self.assertEqual(valores["MAIL_PASSWORD"], "")


class ConfiguracaoSMTPLocalTests(_Subprocesso):
    AMBIENTE = dict(FLASK_ENV="development", MAIL_SERVER="smtp.zoho.com", MAIL_PORT="587", MAIL_USE_TLS="True",
                    MAIL_USERNAME=USUARIO_SMTP, MAIL_PASSWORD=SENHA_SMTP, MAIL_DEFAULT_SENDER=USUARIO_SMTP)

    def _modo(self, **ambiente):
        return self._executar(
            "from config import DevelopmentConfig as C\n"
            "print('TLS=%s SSL=%s' % (C.MAIL_USE_TLS, C.MAIL_USE_SSL))\n",
            FLASK_ENV="development", **ambiente,
        ).stdout

    def test_variaveis_do_ambiente_chegam_ao_flask_mail_e_ativam_o_smtp(self):
        resultado = self._executar(
            "from app import create_app\n"
            "import emails\n"
            "app = create_app('development')\n"
            "m = app.extensions['mail']\n"
            "assert (m.server, m.port, m.use_tls, m.use_ssl) == ('smtp.zoho.com', 587, True, False), m.server\n"
            f"assert (m.username, m.password) == ({USUARIO_SMTP!r}, {SENHA_SMTP!r})\n"
            f"assert m.default_sender == {USUARIO_SMTP!r}\n"
            "assert emails.transporte_configurado(app.config) == 'smtp'\n"
            "d = emails.diagnostico_configuracao(app.config)\n"
            "assert d['smtp_endereco'] == 'smtp.zoho.com:587' and d['smtp_senha_definida'] is True, d\n"
            "assert d['hospedagem_bloqueia_smtp'] is False and not d['bloqueante'], d\n",
            **self.AMBIENTE,
        )
        self.assertNotIn(SENHA_SMTP, resultado.stdout + resultado.stderr)

    def test_espaco_especial_da_senha_de_app_vira_espaco_comum(self):
        self._executar(
            "from app import create_app\n"
            "app = create_app('development')\n"
            "assert app.config['MAIL_PASSWORD'] == 'abcd efgh', 'senha com espaco especial'\n",
            **dict(self.AMBIENTE, MAIL_PASSWORD="abcd\u00a0efgh"),
        )

    def test_porta_465_sem_mail_use_ssl_usa_ssl_direto(self):
        self.assertIn("SSL=True", self._modo(MAIL_PORT="465", MAIL_USE_TLS="True"))
        self.assertIn("SSL=False", self._modo(MAIL_PORT="465", MAIL_USE_SSL="false"))
        self.assertIn("TLS=True SSL=False", self._modo(MAIL_PORT="587"))
        self.assertIn("TLS=True SSL=False", self._modo())

    def test_brevo_continua_opcional_e_tem_prioridade_sobre_o_smtp(self):
        self._executar(
            "from app import create_app\n"
            "import emails\n"
            "app = create_app('development')\n"
            "assert emails.transporte_configurado(app.config) == 'brevo'\n",
            BREVO_API_KEY="xkeysib-synthetic", **self.AMBIENTE,
        )

    def test_links_usam_a_origem_local_em_desenvolvimento(self):
        codigo = (
            "from app import create_app\n"
            "from origem_publica import url_publica\n"
            "app = create_app('development')\n"
            "with app.test_request_context('/', base_url='https://atacante.example.invalid'):\n"
            "    print(url_publica('auth.ativar_acesso', token='TOKEN'))\n"
            "    print(url_publica('auth.resetar_senha', token='TOKEN'))\n"
        )
        linhas = self._executar(codigo, FLASK_ENV="development").stdout.split()
        self.assertEqual(linhas[-2:], [f"{ORIGEM_LOCAL}/auth/primeiro-acesso/TOKEN",
                                       f"{ORIGEM_LOCAL}/auth/resetar-senha/TOKEN"])
        outra = self._executar(codigo, FLASK_ENV="development", NEXO_PUBLIC_URL="http://localhost:5050").stdout
        self.assertIn("http://localhost:5050/auth/primeiro-acesso/TOKEN", outra)


class LogDeInicializacaoTests(unittest.TestCase):
    def _linhas(self, nivel="DEBUG", **config):
        app = SimpleNamespace(config=dict({"BREVO_API_KEY": None, "MAIL_DEFAULT_SENDER": USUARIO_SMTP}, **config))
        with patch.object(emails.logger, "disabled", False), self.assertLogs(emails.logger, level=nivel) as logs:
            emails.registrar_diagnostico_inicial(app)
        return [(registro.levelname, registro.getMessage()) for registro in logs.records]

    def test_local_com_transporte_tem_uma_unica_linha_neutra(self):
        self.assertEqual(self._linhas(MAIL_ATIVO=True, MAIL_USERNAME=USUARIO_SMTP, MAIL_PASSWORD=SENHA_SMTP),
                         [("DEBUG", "Transporte local ativo")])

    def test_local_sem_credenciais_aponta_o_env(self):
        self.assertEqual(self._linhas(MAIL_ATIVO=False),
                         [("DEBUG", "Credenciais de e-mail não configuradas no .env")])

    def test_local_nao_repete_pendencias_no_log(self):
        linhas = self._linhas(MAIL_ATIVO=True, MAIL_USERNAME=USUARIO_SMTP, MAIL_PASSWORD=SENHA_SMTP,
                              MAIL_DEFAULT_SENDER="sem endereco valido")
        self.assertEqual(linhas, [("DEBUG", "Transporte local ativo")])

    def test_producao_mantem_o_resumo_e_as_pendencias_sem_citar_a_plataforma(self):
        linhas = self._linhas(nivel="INFO", PRODUCAO_HTTP=True, MAIL_ATIVO=False)
        self.assertEqual(linhas[0][0], "INFO")
        self.assertIn("Configuração de e-mail | transporte=desativado", linhas[0][1])
        self.assertEqual(linhas[1][0], "WARNING")
        self.assertIn("pendência (bloqueia o envio): Envio de e-mail DESATIVADO", linhas[1][1])
        self.assertIn("MAIL_USERNAME e MAIL_PASSWORD sem valor", linhas[1][1])
        self.assertEqual(_CITA_PLATAFORMA.findall(" ".join(texto for _nivel, texto in linhas)), [])


class DiagnosticoConfiguracaoLocalTests(unittest.TestCase):
    def test_hospedagem_que_bloqueia_smtp_so_orienta_o_que_funciona(self):
        d = emails.diagnostico_configuracao({"MAIL_ATIVO": False, "HOSPEDAGEM_BLOQUEIA_SMTP": True})
        texto = d["pendencias"][0]["texto"]
        self.assertIn("BREVO_API_KEY", texto)
        self.assertNotIn("MAIL_USERNAME", texto)
        d = emails.diagnostico_configuracao({"MAIL_ATIVO": False, "HOSPEDAGEM_BLOQUEIA_SMTP": True,
                                             "SMTP_PERMITIDO": True})
        self.assertIn("MAIL_USERNAME", d["pendencias"][0]["texto"])

    def test_brevo_com_remetente_escolhido_nao_manda_definir_o_remetente(self):
        base = dict(self.BASE, BREVO_API_KEY="xkeysib-sintetica", BREVO_API_URL="https://api.example.invalid/v3")
        pendencias = lambda config: " ".join(p["texto"] for p in emails.diagnostico_configuracao(config)["pendencias"])
        self.assertIn("continua sendo o remetente", pendencias(base))
        self.assertNotIn("continua sendo o remetente", pendencias(dict(base, MAIL_DEFAULT_SENDER_EXPLICITO=True)))

    BASE = {"MAIL_ATIVO": True, "BREVO_API_KEY": None, "MAIL_SERVER": "mail.exemplo.invalid", "MAIL_PORT": 587,
            "MAIL_USE_TLS": True, "MAIL_USE_SSL": False, "MAIL_USERNAME": USUARIO_SMTP,
            "MAIL_PASSWORD": SENHA_SMTP, "MAIL_DEFAULT_SENDER": USUARIO_SMTP, "PUBLIC_URL": ORIGEM_LOCAL}

    def test_diagnostico_descreve_o_smtp_sem_expor_a_senha(self):
        d = emails.diagnostico_configuracao(self.BASE)
        self.assertEqual(d["transporte"], "smtp")
        self.assertEqual(d["smtp_endereco"], "mail.exemplo.invalid:587")
        self.assertEqual(d["smtp_seguranca"], "STARTTLS (MAIL_USE_TLS)")
        self.assertEqual(d["smtp_usuario"], USUARIO_SMTP)
        self.assertTrue(d["smtp_senha_definida"])
        self.assertTrue(d["origem_local"])
        self.assertTrue(d["ambiente_local"])
        self.assertEqual(d["pendencias"], [])
        self.assertNotIn("render", d)
        self.assertNotIn(SENHA_SMTP, json.dumps(d, default=str))

    def test_credencial_incompleta_diz_exatamente_o_que_falta(self):
        d = emails.diagnostico_configuracao(dict(self.BASE, MAIL_ATIVO=False, MAIL_PASSWORD=None))
        self.assertFalse(d["ativo"])
        self.assertEqual(d["smtp_faltando"], ["MAIL_PASSWORD"])
        self.assertTrue(any("MAIL_PASSWORD sem valor" in p["texto"] and p["bloqueante"] for p in d["pendencias"]))

    def test_modos_da_conexao_e_conexao_sem_criptografia(self):
        self.assertEqual(emails.diagnostico_configuracao(dict(self.BASE, MAIL_USE_SSL=True, MAIL_PORT=465))
                         ["smtp_seguranca"], "SSL direto (MAIL_USE_SSL)")
        aberto = emails.diagnostico_configuracao(dict(self.BASE, MAIL_USE_TLS=False))
        self.assertEqual(aberto["smtp_seguranca"], "Sem criptografia")
        self.assertFalse(aberto["bloqueante"])
        self.assertTrue(any("sem criptografia" in p["texto"] for p in aberto["pendencias"]))

    def test_origem_http_so_e_pendencia_fora_da_maquina_local(self):
        self.assertEqual(emails.diagnostico_configuracao(dict(self.BASE, PUBLIC_URL="http://localhost:5050"))
                         ["pendencias"], [])
        remota = emails.diagnostico_configuracao(dict(self.BASE, PUBLIC_URL="http://nexo.example.invalid"))
        self.assertFalse(remota["origem_local"])
        self.assertTrue(any("não usa https" in p["texto"] for p in remota["pendencias"]))

    def test_categorias_orientam_pelo_env_sem_citar_a_plataforma(self):
        self.assertIn(".env", emails.CATEGORIAS["transporte_desativado"])
        self.assertIn("MAIL_SERVER", emails.CATEGORIAS["smtp_conexao"])
        self.assertEqual(_CITA_PLATAFORMA.findall(" ".join(emails.CATEGORIAS.values())), [])


class VerificacaoConexaoSMTPTests(_GmailBase):
    def setUp(self):
        super().setUp()
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None, MAIL_PASSWORD=SENHA_SMTP)
        self.stack.enter_context(patch.object(emails, "_CONEXAO_SMTP", None))

    def _verificar(self):
        with self.app.app_context():
            return emails.verificar_conexao_smtp()

    def test_sucesso_negocia_tls_faz_login_e_nao_envia_mensagem(self):
        falso, instancias = self._smtp_falso()
        with patch.object(extensions.smtplib, "SMTP", falso):
            resultado = self._verificar()
        self.assertTrue(resultado["ok"])
        self.assertEqual((resultado["origem"], resultado["categoria"]), ("verificacao", None))
        smtp = instancias[0]
        self.assertEqual([e[0] for e in smtp.eventos], ["debug", "starttls", "login", "quit"])
        contexto = next(e[1] for e in smtp.eventos if e[0] == "starttls")
        self.assertEqual((contexto.verify_mode, contexto.check_hostname), (ssl.CERT_REQUIRED, True))
        self.assertIs(emails.ultima_conexao_smtp(), resultado)

    def test_porta_465_conecta_por_ssl_direto(self):
        self.app.config.update(MAIL_USE_SSL=True, MAIL_USE_TLS=True, MAIL_PORT=465)
        extensions.mail.init_app(self.app)
        falso, instancias = self._smtp_falso(starttls_suportado=False)
        with patch.object(extensions.smtplib, "SMTP_SSL", falso):
            resultado = self._verificar()
        self.assertTrue(resultado["ok"])
        self.assertEqual(instancias[0].port, 465)
        self.assertEqual([e[0] for e in instancias[0].eventos], ["debug", "login", "quit"])

    def test_falhas_viram_categoria_sem_vazar_credencial_nem_resposta_do_servidor(self):
        casos = (
            ("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 PRIVATE conta-nexo"), "smtp_autenticacao", "535/5.7.8"),
            ("login", smtplib.SMTPAuthenticationError(534, b"5.7.9 PRIVATE"), "smtp_senha_de_app", "534/5.7.9"),
            ("login", smtplib.SMTPAuthenticationError(454, b"4.7.0 PRIVATE"), "smtp_login_temporario", "454/4.7.0"),
            ("login", UnicodeEncodeError("ascii", "PRIVATE", 0, 1, "PRIVATE"), "smtp_valor_invalido", None),
            ("__init__", UnicodeError("PRIVATE"), "smtp_valor_invalido", None),
            ("login", smtplib.SMTPNotSupportedError("PRIVATE AUTH"), "smtp_nao_suportado", None),
            ("starttls", smtplib.SMTPNotSupportedError("PRIVATE STARTTLS"), "smtp_nao_suportado", None),
            ("starttls", ssl.SSLCertVerificationError("PRIVATE"), "tls", None),
            ("__init__", TimeoutError("PRIVATE"), "smtp_conexao", None),
            ("__init__", socket.gaierror("PRIVATE"), "smtp_conexao", None),
            ("__init__", ConnectionRefusedError("PRIVATE"), "smtp_conexao", None),
        )
        for metodo, erro, categoria, codigo in casos:
            with self.subTest(metodo=metodo, categoria=categoria):
                falso, instancias = self._smtp_falso()

                def levantar(*_a, **_k):
                    raise erro
                substituto = levantar if metodo == "__init__" else type("Falha", (falso,), {metodo: levantar})
                with patch.object(extensions.smtplib, "SMTP", substituto), \
                        self.assertLogs(emails.logger, level="WARNING") as logs:
                    resultado = self._verificar()
                self.assertFalse(resultado["ok"])
                self.assertEqual((resultado["categoria"], resultado["codigo"]), (categoria, codigo))
                self.assertEqual(resultado["descricao"], emails.CATEGORIAS[categoria])
                saida = "\n".join(logs.output) + json.dumps(resultado, default=str)
                for privado in ("PRIVATE", SENHA_SMTP, "conta.teste@gmail.com"):
                    self.assertNotIn(privado, saida)
                if metodo != "__init__":
                    self.assertEqual(instancias[0].eventos[-1], ("close",))  # conexão não fica aberta
                self.assertIs(emails.ultima_conexao_smtp(), resultado)

    def test_codigo_534_fora_do_gmail_nao_fala_em_senha_de_app_do_google(self):
        self.app.config["MAIL_SERVER"] = "mail.exemplo.invalid"
        extensions.mail.init_app(self.app)
        falso, _instancias = self._smtp_falso()

        class Recusa(falso):
            def login(falso_self, *a):
                raise smtplib.SMTPAuthenticationError(534, b"5.7.9 PRIVATE")

        with patch.object(extensions.smtplib, "SMTP", Recusa):
            resultado = self._verificar()
        self.assertEqual(resultado["categoria"], "smtp_autenticacao")
        self.assertNotIn("Google", resultado["descricao"])

    def test_copia_oculta_recusada_nao_conta_como_aceita(self):
        falso, _instancias = self._smtp_falso()

        class RecusaCopia(falso):
            def sendmail(falso_self, *a, **k):
                return {"caixa@example.invalid": (550, b"5.1.1 PRIVATE")}

        def enviar(classe, copia):
            envio = emails.EnvioEmail("novo_chamado")
            with self.app.app_context(), patch.object(extensions.smtplib, "SMTP", classe):
                mensagem = self._mensagem()
                mensagem.bcc = copia
                emails._executar_envio(self.app, mensagem, envio)
            return envio

        falha = enviar(RecusaCopia, ["caixa@example.invalid"])
        self.assertEqual((falha.estado, falha.categoria),
                         (emails.EstadoEnvio.FALHA_NO_ENVIO, "smtp_destinatario_recusado"))
        # Recusa de outro destinatário não muda o resultado de quem foi aceito.
        aceito = enviar(RecusaCopia, ["outra.caixa@example.invalid"])
        self.assertEqual(aceito.estado, emails.EstadoEnvio.ACEITO_PELO_PROVEDOR)

    def test_sem_smtp_em_uso_nao_conecta_nem_guarda_status(self):
        casos = (
            ("transporte_desativado", dict(MAIL_ATIVO=False), False),
            ("smtp_nao_usado", dict(BREVO_API_KEY="xkeysib-synthetic"), False),
            ("smtp_suprimido", {}, True),
        )
        for categoria, config, suprimido in casos:
            with self.subTest(categoria=categoria):
                self.app.config.update({"MAIL_ATIVO": True, "BREVO_API_KEY": None, **config})
                self.app.extensions["mail"].suppress = suprimido
                with patch.object(extensions.smtplib, "SMTP") as smtp:
                    resultado = self._verificar()
                smtp.assert_not_called()
                self.assertFalse(resultado["ok"])
                self.assertEqual(resultado["categoria"], categoria)
                self.assertIsNone(emails.ultima_conexao_smtp())

    def test_envio_real_atualiza_o_status_so_quando_diz_algo_sobre_a_conexao(self):
        def enviar(classe):
            envio = emails.EnvioEmail("teste")
            with self.app.app_context(), patch.object(extensions.smtplib, "SMTP", classe):
                emails._executar_envio(self.app, self._mensagem(), envio)
            return envio

        falso, _instancias = self._smtp_falso()
        self.assertEqual(enviar(falso).estado, emails.EstadoEnvio.ACEITO_PELO_PROVEDOR)
        aceito = emails.ultima_conexao_smtp()
        self.assertEqual((aceito["ok"], aceito["origem"]), (True, "envio"))

        class DestinatarioRecusado(falso):
            def sendmail(falso_self, *a, **k):
                raise smtplib.SMTPRecipientsRefused({"x@example.invalid": (550, b"PRIVATE")})

        self.assertEqual(enviar(DestinatarioRecusado).categoria, "smtp_destinatario_recusado")
        self.assertIs(emails.ultima_conexao_smtp(), aceito)  # a conexão continuou funcionando

        class LoginRecusado(falso):
            def login(falso_self, *a):
                raise smtplib.SMTPAuthenticationError(535, b"5.7.8 PRIVATE")

        self.assertEqual(enviar(LoginRecusado).categoria, "smtp_autenticacao")
        falha = emails.ultima_conexao_smtp()
        self.assertEqual((falha["ok"], falha["categoria"], falha["origem"]), (False, "smtp_autenticacao", "envio"))

        # Falha antes do login, com uma categoria genérica: a conexão não se completou.
        class StarttlsRecusado(falso):
            def starttls(falso_self, *a, **k):
                raise smtplib.SMTPResponseException(454, b"4.7.0 PRIVATE")

        self.assertEqual(enviar(falso).estado, emails.EstadoEnvio.ACEITO_PELO_PROVEDOR)
        self.assertEqual(enviar(StarttlsRecusado).categoria, "smtp_erro")
        falha = emails.ultima_conexao_smtp()
        self.assertEqual((falha["ok"], falha["categoria"], falha["origem"]), (False, "smtp_erro", "envio"))


class _PainelCase(_EmailCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(MAIL_SERVER="mail.exemplo.invalid", MAIL_PORT=587, MAIL_USE_TLS=True, MAIL_USE_SSL=False,
                               MAIL_USERNAME=USUARIO_SMTP, MAIL_PASSWORD=SENHA_SMTP, PUBLIC_URL=ORIGEM_LOCAL)
        self.app.extensions["mail"].suppress = False
        self.stack.enter_context(patch.object(emails, "_CONEXAO_SMTP", None))
        self.conexao = self.stack.enter_context(patch.object(emails.mail, "connect"))

    def _pagina(self, client):
        resposta = client.get("/admin/diagnostico-email")
        self.assertEqual(resposta.status_code, 200)
        return resposta.get_data(as_text=True)

    def _testar_conexao(self, client):
        return client.post("/admin/diagnostico-email/conexao",
                           data={"csrf_token": self._csrf(client, "/admin/diagnostico-email")})


class PainelDiagnosticoLocalTests(_PainelCase):
    def test_painel_mostra_o_smtp_do_env_sem_expor_a_senha_nem_citar_a_plataforma(self):
        pagina = self._pagina(self._admin())
        for esperado in ("mail.exemplo.invalid:587", "STARTTLS (MAIL_USE_TLS)", "conta-nexo@", "Definida (nunca é exibida)",
                         "Arquivo .env lido por este servidor", "reinicie o servidor", ORIGEM_LOCAL,
                         "os links dos e-mails só abrem neste computador", "NÃO VERIFICADA", "Testar conexão SMTP",
                         "Enviar e-mail de teste para mim", "O servidor SMTP aceitou"):
            self.assertIn(esperado, pagina)
        self.assertNotIn(SENHA_SMTP, pagina)
        # O caminho do .env traz o nome da pasta do projeto, que contém a palavra.
        self.assertEqual(_CITA_PLATAFORMA.findall(pagina.replace(self.app.config["ARQUIVO_ENV"], "")), [])
        for so_do_brevo in ("Consultar entrega", "Transacional", "Chave do Brevo"):
            self.assertNotIn(so_do_brevo, pagina)

    def test_teste_de_conexao_confirma_sem_enviar_e_o_status_fica_na_tela(self):
        client = self._admin()
        resposta = self._testar_conexao(client)
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.headers["Location"].endswith("/admin/diagnostico-email"))
        self.assertIn("Conexão SMTP confirmada", self._flashes(client))
        self.assertEqual(self.sent, [])
        pagina = self._pagina(client)
        self.assertIn("CONECTADO", pagina)
        self.assertIn("teste de conexão", pagina)
        self.assertNotIn("NÃO VERIFICADA", pagina)

    def test_falha_de_conexao_explica_a_causa_com_o_codigo_e_sem_a_resposta_do_servidor(self):
        self.conexao.return_value.__enter__.side_effect = smtplib.SMTPAuthenticationError(535, b"5.7.8 PRIVATE_RESPOSTA")
        client = self._admin()
        self._testar_conexao(client)
        mensagens = self._flashes(client)
        self.assertIn("A conexão SMTP falhou: o servidor SMTP recusou as credenciais", mensagens)
        pagina = self._pagina(client)
        self.assertIn("FALHA", pagina)
        self.assertIn("SMTP 535/5.7.8", pagina)
        for privado in ("PRIVATE_RESPOSTA", SENHA_SMTP):
            self.assertNotIn(privado, mensagens + pagina)

    def test_envio_de_teste_aceito_atualiza_o_status_da_conexao(self):
        client = self._admin()
        client.post("/admin/diagnostico-email/teste",
                    data={"csrf_token": self._csrf(client, "/admin/diagnostico-email")})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("foi aceito pelo serviço de e-mail (SMTP (Flask-Mail))", self._flashes(client))
        pagina = self._pagina(client)
        self.assertIn('b-green">Aceito pelo provedor</span>', pagina)
        self.assertIn("CONECTADO", pagina)
        self.assertIn("— envio de e-mail</dd>", pagina)

    def test_sem_credenciais_pede_o_preenchimento_e_nao_oferece_o_teste_de_conexao(self):
        self.app.config.update(MAIL_ATIVO=False, MAIL_USERNAME=None, MAIL_PASSWORD=None)
        client = self._admin()
        pagina = self._pagina(client)
        self.assertIn("NÃO DEFINIDO", pagina)
        self.assertIn("NÃO DEFINIDA", pagina)
        self.assertIn("Preencha MAIL_USERNAME e MAIL_PASSWORD no .env", pagina)
        self.assertNotIn("Testar conexão SMTP", pagina)
        self._testar_conexao(client)
        self.conexao.assert_not_called()
        self.assertIn("A conexão SMTP não foi testada", self._flashes(client))

    def test_com_brevo_em_uso_o_cartao_de_conexao_smtp_nao_aparece(self):
        self.app.config.update(BREVO_API_KEY="xkeysib-synthetic", BREVO_API_URL="https://api.brevo.example.invalid/x")
        client = self._admin()
        pagina = self._pagina(client)
        self.assertIn("Chave do Brevo", pagina)
        self.assertIn("O provedor registrou a entrega", pagina)
        self.assertNotIn("Testar conexão SMTP", pagina)
        self._testar_conexao(client)
        self.conexao.assert_not_called()
        self.assertIn("o envio está usando a API do Brevo", self._flashes(client))

    def test_fora_do_ambiente_local_o_caminho_do_env_nao_aparece(self):
        self.app.config["PRODUCAO_HTTP"] = True
        pagina = self._pagina(self._admin())
        self.assertNotIn("Arquivo .env lido por este servidor", pagina)
        self.assertNotIn(self.app.config["ARQUIVO_ENV"], pagina)
        self.assertIn("Lida das variáveis de ambiente", pagina)

    def test_caminho_do_env_e_o_da_raiz_do_projeto(self):
        self.assertEqual(Path(self.app.config["ARQUIVO_ENV"]), ROOT / ".env")

    def test_selo_do_arquivo_env_acompanha_o_arquivo(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "sintetico.env"
            arquivo.write_text("", encoding="utf-8")
            self.app.config["ARQUIVO_ENV"] = str(arquivo)
            client = self._admin()
            pagina = self._pagina(client)
            self.assertIn('b-green">encontrado</span>', pagina)
            self.assertNotIn('b-amber">não encontrado</span>', pagina)
            self.app.config["ARQUIVO_ENV"] = str(Path(pasta) / "ausente.env")
            pagina = self._pagina(client)
            self.assertIn('b-amber">não encontrado</span>', pagina)
            self.assertNotIn('b-green">encontrado</span>', pagina)

    def test_selo_nao_diz_ativo_quando_uma_pendencia_impede_o_envio(self):
        self.app.config["MAIL_DEFAULT_SENDER"] = "NEXO"
        pagina = self._pagina(self._admin())
        self.assertIn('b-red">BLOQUEADO</span>', pagina)
        self.assertNotIn('b-green">ATIVO</span>', pagina)

    def test_conexao_com_falha_nao_convive_com_nenhuma_pendencia(self):
        client = self._admin()
        self.assertIn("Nenhuma pendência detectada", self._pagina(client))
        self.conexao.side_effect = smtplib.SMTPAuthenticationError(535, b"5.7.8 PRIVATE")
        self._testar_conexao(client)
        pagina = self._pagina(client)
        self.assertNotIn("Nenhuma pendência detectada", pagina)
        self.assertIn("A última conexão com o servidor SMTP falhou.", pagina)
        self.assertNotIn("PRIVATE", pagina)

    def test_subtitulo_da_conexao_nao_promete_criptografia_que_nao_existe(self):
        self.app.config.update(MAIL_USE_TLS=False, MAIL_USE_SSL=False)
        pagina = self._pagina(self._admin())
        self.assertIn("faz o login sem criptografia", pagina)
        self.assertNotIn("negocia a criptografia", pagina)

    def test_fora_do_ambiente_local_nao_manda_editar_o_env(self):
        self.app.config.update(MAIL_ATIVO=False, MAIL_USERNAME=None, MAIL_PASSWORD=None, PRODUCAO_HTTP=True)
        pagina = self._pagina(self._admin())
        self.assertIn("nas variáveis de ambiente do servidor e reinicie o servidor", pagina)
        self.assertNotIn("Arquivo .env lido por este servidor", pagina)

    def test_login_nao_volta_para_acao_que_so_aceita_post(self):
        anonimo = self.app.test_client()
        perdida = anonimo.post("/admin/diagnostico-email/conexao", data={"csrf_token": self._csrf(anonimo)})
        self.assertIn("/auth/login?next=", perdida.headers["Location"])
        for destino, esperado in (("/admin/diagnostico-email/conexao", "/admin/dashboard"),
                                  ("/admin/diagnostico-email", "/admin/diagnostico-email"),
                                  ("/rota/que/nao/existe", "/admin/dashboard")):
            with self.subTest(destino=destino):
                client = self.app.test_client()
                resposta = client.post(f"/auth/login?next={destino}", data={
                    "email": "admin@example.invalid", "senha": OLD_PASSWORD, "csrf_token": self._csrf(client),
                })
                self.assertEqual((resposta.status_code, resposta.headers["Location"]), (302, esperado))

    def test_teste_de_conexao_exige_admin_csrf_e_post(self):
        anonimo = self.app.test_client()
        sem_login = anonimo.post("/admin/diagnostico-email/conexao", data={"csrf_token": self._csrf(anonimo)})
        self.assertEqual(sem_login.status_code, 302)
        self.assertIn("/auth/login", sem_login.headers["Location"])
        cliente = self.app.test_client()
        cliente.post("/auth/login", data={"email": "active@example.invalid", "senha": OLD_PASSWORD,
                                          "csrf_token": self._csrf(cliente)})
        self.assertEqual(cliente.post("/admin/diagnostico-email/conexao",
                                      data={"csrf_token": self._csrf(cliente, "/auth/logout")}).status_code, 403)
        client = self._admin()
        self.assertEqual(client.post("/admin/diagnostico-email/conexao").status_code, 400)  # sem CSRF
        self.assertEqual(client.get("/admin/diagnostico-email/conexao").status_code, 405)
        self.conexao.assert_not_called()


class LimiteTesteConexaoTests(_PainelCase):
    ENABLE_LIMITS = True

    def test_teste_de_conexao_tem_limite_por_usuario(self):
        client = self._admin()
        csrf = self._csrf(client, "/admin/diagnostico-email")
        codigos = [client.post("/admin/diagnostico-email/conexao", data={"csrf_token": csrf}).status_code
                   for _ in range(31)]
        self.assertEqual(codigos, [302] * 30 + [429])
        self.assertEqual(self.conexao.call_count, 30)
        # O limite é por hora: a mensagem não promete liberar em um minuto.
        bloqueio = client.post("/admin/diagnostico-email/conexao", data={"csrf_token": csrf})
        pagina = bloqueio.get_data(as_text=True)
        self.assertEqual(bloqueio.status_code, 429)
        self.assertNotIn("Aguarde um minuto", pagina)
        self.assertRegex(pagina, r"Aguarde \d+ minutos e tente novamente")
        self.assertIn('href="/admin/dashboard"', pagina)

    def test_limite_por_minuto_continua_dizendo_um_minuto(self):
        client = self.app.test_client()
        csrf = self._csrf(client)
        for _ in range(10):
            client.post("/auth/login", data={"email": "ninguem@example.invalid", "senha": "x", "csrf_token": csrf})
        bloqueio = client.post("/auth/login", data={"email": "ninguem@example.invalid", "senha": "x",
                                                    "csrf_token": csrf})
        self.assertEqual(bloqueio.status_code, 429)
        self.assertIn("Aguarde um minuto e tente novamente", bloqueio.get_data(as_text=True))


class LinksDosEmailsLocaisTests(_PainelCase):
    def _unico_link(self, mensagem, prefixo):
        link = self._link(mensagem.html, prefixo)
        self.assertTrue(link.startswith(ORIGEM_LOCAL + prefixo), link)
        self.assertIn(link, mensagem.body)
        return link

    def test_cadastro_envia_link_local_e_o_cliente_ativa_a_conta_por_ele(self):
        admin = self._admin()
        self.assertEqual(self._cadastrar(admin).status_code, 302)
        link = self._unico_link(self.sent[-1], "/auth/primeiro-acesso/")
        self.assertIn(f'{ORIGEM_LOCAL}/auth/login', self.sent[-1].html)  # endereço do portal no convite
        cliente = self.app.test_client()
        caminho = urlsplit(link).path
        self.assertEqual(cliente.get(caminho).status_code, 200)
        ativacao = cliente.post(caminho, data={
            "nova_senha": NEW_PASSWORD, "confirma_senha": NEW_PASSWORD, "csrf_token": self._csrf(cliente, caminho),
        })
        self.assertEqual(ativacao.status_code, 302)
        self.assertTrue(ativacao.headers["Location"].endswith("/auth/login"))
        entrada = cliente.post("/auth/login", data={"email": "novo@example.invalid", "senha": NEW_PASSWORD,
                                                    "csrf_token": self._csrf(cliente)})
        self.assertEqual(entrada.status_code, 302)
        self.assertTrue(entrada.headers["Location"].endswith("/cliente/dashboard"))

    def test_recuperacao_usa_a_origem_local_mesmo_com_host_forjado(self):
        cliente = self.app.test_client()
        forjado = "http://atacante.example.invalid"
        formulario = cliente.get("/auth/recuperar-senha", base_url=forjado).get_data(as_text=True)
        csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', formulario).group(1)
        resposta = cliente.post("/auth/recuperar-senha", base_url=forjado,
                                data={"email": "active@example.invalid", "csrf_token": csrf})
        self.assertEqual(resposta.status_code, 302)
        self._entregar()
        link = self._unico_link(self.sent[-1], "/auth/resetar-senha/")
        self.assertNotIn("atacante", link)
        self.assertEqual(cliente.get(urlsplit(link).path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
