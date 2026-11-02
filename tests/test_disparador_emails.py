"""Disparador de e-mails: transportes, falhas do provedor, entrega e telas.

Complementa tests/test_emails_transacionais.py com os casos da comparação com
o projeto original (Gmail SMTP + Flask-Mail): Gmail SMTP em ambiente que o
permite, API HTTPS do Brevo no Render Free, códigos de erro documentados pelo
Brevo, confirmação de entrega (teste controlado) e mensagens das telas de
operação. Executar da raiz: python -B -m unittest tests.test_disparador_emails -v

Sem rede e sem envio real: urlopen e smtplib são substituídos e qualquer
conexão de socket falha o teste. Endereços e chaves são fictícios.
"""

import contextlib
import json
import os
import secrets
import smtplib
import ssl
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from flask import Flask
from flask_mail import Message
from markupsafe import escape
from sqlalchemy import event

import emails
import extensions
from blueprints import auth
from emails import EstadoEnvio
from extensions import db
from tests.test_emails_transacionais import (
    CHAVE_API, ID_MENSAGEM, ROOT, URL_BREVO, _EmailCase, _RespostaBrevo, _erro_http,
)


def _eventos(*itens, status=200):
    """Resposta de GET /v3/smtp/statistics/events (urlopen simulado)."""
    return _RespostaBrevo(corpo=json.dumps({"events": list(itens)}).encode(), status=status)


class _Subprocesso(unittest.TestCase):
    """Configuração lida do ambiente num processo novo (sem .env e sem rede)."""

    def _executar(self, codigo, **ambiente):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BREVO_", "MAIL_", "NEXO_", "RENDER"))}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHON_DOTENV_DISABLED="1",
                   SECRET_KEY=secrets.token_hex(32), DATABASE_URL="postgresql://localhost/nexo_test",
                   DIRECT_URL="", NEXO_DB_DIRECT="0",
                   UPLOAD_FOLDER=str(ROOT / ".audit_tmp" / "disparador-config-uploads"))
        env.update(ambiente)
        preludio = (
            "import dotenv, socket\n"
            "dotenv.load_dotenv = lambda *a, **kw: False\n"
            "def sem_rede(*a, **kw): raise AssertionError('Rede proibida no teste')\n"
            "socket.socket.connect = sem_rede\n"
        )
        resultado = subprocess.run([sys.executable, "-B", "-c", preludio + codigo], cwd=ROOT, env=env,
                                   capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        return resultado


# ---------------------------------------------------------------------------
# T05 — Gmail SMTP em ambiente que permite SMTP (fora do Render Free)
# ---------------------------------------------------------------------------
class _GmailBase(_Subprocesso):
    """App mínima com Flask-Mail apontado para o Gmail (SMTP simulado)."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for alvo in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            self.stack.enter_context(patch(alvo, side_effect=AssertionError("Rede proibida no teste")))
        self.app = Flask("app", root_path=str(ROOT))
        self.app.config.update(MAIL_SERVER="smtp.gmail.com", MAIL_PORT=587, MAIL_USE_TLS=True,
                               MAIL_USE_SSL=False, MAIL_USERNAME="conta.teste@gmail.com",
                               MAIL_PASSWORD="PRIVATE_APP_PASSWORD", MAIL_DEFAULT_SENDER="conta.teste@gmail.com",
                               EMAIL_TIMEOUT_SEGUNDOS=4.0, EMAIL_ALERTA_ADMIN=False, TESTING=False)
        extensions.mail.init_app(self.app)
        self.stack.enter_context(patch.object(emails, "_HISTORICO", emails.deque(maxlen=50)))
        self.stack.enter_context(patch.object(emails.logger, "disabled", False))

    def _smtp_falso(self, *, starttls_suportado=True):
        instancias = []

        class SMTPFalso:
            def __init__(falso, host, port, timeout=None, context=None, **kwargs):
                falso.host, falso.port, falso.timeout, falso.context = host, port, timeout, context
                falso.eventos = []
                instancias.append(falso)

            def set_debuglevel(falso, nivel):
                falso.eventos.append(("debug", nivel))

            def starttls(falso, context=None):
                if not starttls_suportado:
                    raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
                falso.eventos.append(("starttls", context))

            def login(falso, usuario, senha):
                falso.eventos.append(("login", usuario))

            def sendmail(falso, de, para, *args):
                falso.eventos.append(("sendmail", de, tuple(para)))

            def quit(falso):
                falso.eventos.append(("quit",))

            def close(falso):
                falso.eventos.append(("close",))

        return SMTPFalso, instancias

    def _mensagem(self):
        return Message(subject="Assunto", recipients=["cliente@example.invalid"], html="<p>x</p>", body="x")


class GmailSMTPTests(_GmailBase):
    def test_t05_variaveis_do_projeto_original_selecionam_o_gmail_smtp(self):
        codigo = (
            "import emails\n"
            "from config import DevelopmentConfig as C\n"
            "cfg = {k: getattr(C, k) for k in dir(C) if k.isupper()}\n"
            "assert (C.MAIL_SERVER, C.MAIL_PORT, C.MAIL_USE_TLS, C.MAIL_USE_SSL) == "
            "('smtp.gmail.com', 587, True, False), cfg\n"
            "assert C.MAIL_ATIVO and C.MAIL_DEFAULT_SENDER == 'conta.teste@gmail.com', C.MAIL_DEFAULT_SENDER\n"
            "assert emails.transporte_configurado(cfg) == 'smtp'\n"
            "d = emails.diagnostico_configuracao(cfg)\n"
            "print('BLOQUEANTE=' + str(d['bloqueante']))\n"
            "print('\\n'.join(p['texto'] for p in d['pendencias']))\n"
        )
        # Os nomes e formatos do .env do projeto original (MAIL_DEFAULT_SENDER em branco).
        ambiente = dict(FLASK_ENV="development", MAIL_SERVER="smtp.gmail.com", MAIL_PORT="587",
                        MAIL_USE_TLS="true", MAIL_USE_SSL="false", MAIL_USERNAME="conta.teste@gmail.com",
                        MAIL_PASSWORD="PRIVATE_APP_PASSWORD", MAIL_DEFAULT_SENDER="")
        fora = self._executar(codigo, **ambiente)
        self.assertIn("BLOQUEANTE=False", fora.stdout)
        # O remetente é a própria conta autenticada: nada a conferir.
        self.assertNotIn("própria conta autenticada em MAIL_USERNAME", fora.stdout)
        outro = emails.diagnostico_configuracao(dict(MAIL_ATIVO=True, MAIL_USERNAME="conta.teste@gmail.com",
                                                     MAIL_PASSWORD="x", MAIL_DEFAULT_SENDER="outra.conta@gmail.com"))
        self.assertTrue(any("própria conta autenticada em MAIL_USERNAME" in p["texto"] for p in outro["pendencias"]))
        self.assertNotIn("PRIVATE_APP_PASSWORD", fora.stdout + fora.stderr)
        # O mesmo .env no Render Free: SMTP bloqueado vira pendência que impede o envio.
        render = self._executar(codigo, RENDER="true", RENDER_EXTERNAL_URL="https://nexo.example.invalid",
                                **ambiente)
        self.assertIn("BLOQUEANTE=True", render.stdout)
        self.assertIn("a hospedagem atual bloqueia as portas SMTP", render.stdout)

    def test_t05_gmail_na_587_usa_starttls_com_certificado_verificado(self):
        falso, instancias = self._smtp_falso()
        with self.app.app_context(), patch.object(extensions.smtplib, "SMTP", falso):
            extensions.mail.send(self._mensagem())
        smtp = instancias[0]
        self.assertEqual((smtp.host, smtp.port, smtp.timeout), ("smtp.gmail.com", 587, 4.0))
        contexto = next(e[1] for e in smtp.eventos if e[0] == "starttls")
        self.assertEqual((contexto.verify_mode, contexto.check_hostname), (ssl.CERT_REQUIRED, True))
        self.assertEqual([e[0] for e in smtp.eventos], ["debug", "starttls", "login", "sendmail", "quit"])
        self.assertEqual(smtp.eventos[2], ("login", "conta.teste@gmail.com"))

    def test_t05_ssl_na_465_com_tls_ligado_nao_tenta_starttls(self):
        # MAIL_USE_TLS tem padrão ligado: com MAIL_USE_SSL=true na 465, o STARTTLS
        # sobre SSL derrubava o envio (o servidor não oferece STARTTLS na 465).
        self.app.config.update(MAIL_USE_SSL=True, MAIL_USE_TLS=True, MAIL_PORT=465)
        extensions.mail.init_app(self.app)
        falso, instancias = self._smtp_falso(starttls_suportado=False)
        with self.app.app_context(), patch.object(extensions.smtplib, "SMTP_SSL", falso):
            extensions.mail.send(self._mensagem())
        smtp = instancias[0]
        self.assertEqual(smtp.port, 465)
        self.assertIsInstance(smtp.context, ssl.SSLContext)
        self.assertEqual([e[0] for e in smtp.eventos], ["debug", "login", "sendmail", "quit"])

    def test_t05_t07_recusas_do_gmail_tem_codigo_numerico_e_orientacao_propria(self):
        casos = (
            (smtplib.SMTPAuthenticationError(534, b"5.7.9 Application-specific password required. PRIVATE"),
             "smtp_senha_de_app", "534/5.7.9"),
            (smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted. PRIVATE"),
             "smtp_autenticacao", "535/5.7.8"),
            (smtplib.SMTPSenderRefused(550, b"5.4.5 Daily user sending limit exceeded. PRIVATE",
                                       "conta.teste@gmail.com"), "limite_excedido", "550/5.4.5"),
            (smtplib.SMTPRecipientsRefused({"cliente@example.invalid": (550, b"5.1.1 PRIVATE does not exist")}),
             "smtp_destinatario_recusado", "550/5.1.1"),
            (smtplib.SMTPServerDisconnected("PRIVATE"), "smtp_conexao", None),
        )
        for erro, categoria, codigo in casos:
            with self.subTest(categoria=categoria):
                envio = emails.EnvioEmail("recuperacao_senha")
                with self.app.app_context(), patch.object(emails.mail, "send", side_effect=erro), \
                        self.assertLogs(emails.logger, level="WARNING") as logs:
                    emails._executar_envio(self.app, self._mensagem(), envio)
                self.assertEqual((envio.estado, envio.categoria, envio.codigo_provedor),
                                 (EstadoEnvio.FALHA_NO_ENVIO, categoria, codigo))
                saida = "\n".join(logs.output)
                self.assertIn(f"categoria={categoria}", saida)
                self.assertIn(f"codigo={codigo or '-'}", saida)
                for privado in ("PRIVATE", "Application-specific", "cliente@example.invalid", "conta.teste@gmail"):
                    self.assertNotIn(privado, saida)
        self.assertIn("senha de app", emails.CATEGORIAS["smtp_senha_de_app"])

    def test_t05_diagnostico_orienta_porta_e_modo_do_gmail(self):
        base = dict(self.app.config, MAIL_ATIVO=True, BREVO_API_KEY=None)
        ssl_na_587 = emails.diagnostico_configuracao(dict(base, MAIL_USE_SSL=True, MAIL_USE_TLS=False, MAIL_PORT=587))
        self.assertTrue(any("porta 587, que usa STARTTLS" in p["texto"] for p in ssl_na_587["pendencias"]))
        tls_na_465 = emails.diagnostico_configuracao(dict(base, MAIL_USE_SSL=False, MAIL_USE_TLS=True, MAIL_PORT=465))
        self.assertTrue(any("A porta 465 usa SSL direto" in p["texto"] for p in tls_na_465["pendencias"]))
        certo = emails.diagnostico_configuracao(dict(base, MAIL_USE_SSL=False, MAIL_USE_TLS=True, MAIL_PORT=587))
        self.assertFalse(any("porta" in p["texto"] for p in certo["pendencias"]))


# ---------------------------------------------------------------------------
# T06–T08 — API HTTPS do Brevo: códigos documentados, limite e falhas
# ---------------------------------------------------------------------------
class BrevoErrosTests(_EmailCase):
    def setUp(self):
        super().setUp()
        self.app.config["BREVO_API_KEY"] = CHAVE_API

    def _recuperar(self, efeito):
        with patch.object(emails.urllib.request, "urlopen", side_effect=efeito) as http, \
                self.assertLogs(level="INFO") as logs:
            self._request_link("active@example.invalid")
            self._entregar()
        return http, "\n".join(logs.output), self._ultimo("recuperacao_senha")

    def test_t06_dmarc_exige_dominio_proprio_autenticado(self):
        _http, saida, envio = self._recuperar([_erro_http(400, {
            "code": "DMARC policy requires domain authentication",
            "message": "PRIVATE_SENTINEL sender nexo@gmail.com not authenticated"})])
        self.assertEqual((envio.categoria, envio.http_status, envio.codigo_provedor),
                         ("dominio_nao_autenticado", 400, "dmarc_policy_requires_domain_authentication"))
        self.assertIn("domínio próprio", envio.descricao_categoria)
        self.assertIn("codigo=dmarc_policy_requires_domain_authentication", saida)
        self._assert_sem_segredos(saida, "nexo@gmail.com", "active@example.invalid")

    def test_t07_codigos_em_texto_livre_do_brevo(self):
        casos = (
            (401, {"code": "api-key not found"}, "credencial_rejeitada", "api_key_not_found"),
            (401, {"code": "Authentication failed"}, "credencial_rejeitada", "authentication_failed"),
            (402, {"code": "Insufficient credits"}, "creditos_insuficientes", "insufficient_credits"),
            # Texto fora do enum oficial nunca vai ao log (pode conter dados).
            (400, {"code": "PRIVATE_SENTINEL cliente@empresa.example.invalid"}, "requisicao_rejeitada", None),
        )
        for status, corpo, categoria, codigo in casos:
            with self.subTest(codigo=codigo or "fora-do-enum"):
                http, saida, envio = self._recuperar([_erro_http(status, corpo)])
                http.assert_called_once()
                self.assertEqual((envio.categoria, envio.codigo_provedor), (categoria, codigo))
                self.assertIn(f"codigo={codigo or '-'}", saida)
                self._assert_sem_segredos(saida, "cliente@empresa.example.invalid")

    def test_t08_429_respeita_o_x_sib_ratelimit_reset_do_brevo(self):
        http, _saida, envio = self._recuperar([
            _erro_http(429, {"code": "too_many_requests"}, {"x-sib-ratelimit-reset": "3"}), _RespostaBrevo()])
        self.assertEqual(http.call_count, 2)
        self.pausas.assert_called_once_with(3.0)
        self.assertEqual((envio.estado, envio.tentativas), (EstadoEnvio.ACEITO_PELO_PROVEDOR, 2))
        self.pausas.reset_mock()
        # Janela longa: repetir agora só consumiria cota e falharia de novo.
        http, _saida, envio = self._recuperar([
            _erro_http(429, {"code": "too_many_requests"}, {"x-sib-ratelimit-reset": "45"}), _RespostaBrevo()])
        http.assert_called_once()
        self.pausas.assert_not_called()
        self.assertEqual(envio.categoria, "limite_excedido")
        # O reset do limite não se aplica a uma indisponibilidade (503).
        http, _saida, envio = self._recuperar([
            _erro_http(503, None, {"x-sib-ratelimit-reset": "3600"}), _RespostaBrevo()])
        self.assertEqual(http.call_count, 2)
        self.pausas.assert_called_once_with(1.5)
        self.assertEqual(envio.estado, EstadoEnvio.ACEITO_PELO_PROVEDOR)


# ---------------------------------------------------------------------------
# Teste controlado: confirmação de entrega no provedor (etapa 3 de 4)
# ---------------------------------------------------------------------------
class _EntregaBase(_EmailCase):
    """Admin logado e um envio de teste aceito pelo Brevo (com messageId)."""

    def setUp(self):
        super().setUp()
        self.app.config["BREVO_API_KEY"] = CHAVE_API
        self.client = self._admin()
        with patch.object(emails.urllib.request, "urlopen", return_value=_RespostaBrevo()):
            self.client.post("/admin/diagnostico-email/teste",
                             data={"csrf_token": self._csrf(self.client, "/admin/diagnostico-email")})
        self._flashes(self.client)
        self.envio = self._ultimo("teste")
        self.assertEqual((self.envio.estado, self.envio.id_mensagem), (EstadoEnvio.ACEITO_PELO_PROVEDOR, ID_MENSAGEM))

    def _consultar(self, efeito, correlacao=None, *, client=None, csrf=True):
        client = client or self.client
        dados = {"csrf_token": self._csrf(client, "/admin/diagnostico-email")} if csrf else {}
        with patch.object(emails.urllib.request, "urlopen", side_effect=efeito) as http, \
                patch.object(emails.logger, "disabled", False), self.assertLogs(level="INFO") as logs:
            resposta = client.post(f"/admin/diagnostico-email/entrega/{correlacao or self.envio.correlacao}",
                                   data=dados)
            emails.logger.info("fim da consulta")  # garante ao menos uma linha
        return resposta, http, self._flashes(client), "\n".join(logs.output)


class ConfirmacaoDeEntregaTests(_EntregaBase):
    def test_consulta_registra_a_entrega_sem_dados_pessoais(self):
        privado = {"email": "admin@example.invalid", "subject": "PRIVATE_SUBJECT", "ip": "203.0.113.9",
                   "reason": "PRIVATE_REASON", "link": "https://privado.example.invalid/x"}
        resposta, http, mensagens, saida = self._consultar([_eventos(
            dict(privado, event="requests", date="2026-11-01T10:00:00.000+00:00", messageId=ID_MENSAGEM),
            dict(privado, event="delivered", date="2026-11-01T10:00:05Z", messageId=ID_MENSAGEM.strip("<>")),
            dict(privado, event="delivered", date="2026-11-01T10:00:06Z", messageId="<outra@relay.invalid>"),
            dict(privado, event="evento_desconhecido", date="2026-11-01T10:00:07Z", messageId=ID_MENSAGEM),
        )])
        self.assertTrue(resposta.headers["Location"].endswith("/admin/diagnostico-email"))
        requisicao = http.call_args.args[0]
        partes = urlsplit(requisicao.full_url)
        self.assertEqual((requisicao.get_method(), partes.scheme, partes.netloc, partes.path),
                         ("GET", "https", urlsplit(URL_BREVO).netloc, "/v3/smtp/statistics/events"))
        self.assertEqual(parse_qs(partes.query), {"messageId": [ID_MENSAGEM], "days": ["7"], "limit": ["100"],
                                                  "sort": ["asc"]})
        self.assertIn("Api-key", requisicao.unredirected_hdrs)   # nunca acompanha redirecionamento
        self.assertNotIn("Api-key", requisicao.headers)
        self.assertIn("registrou a entrega ao servidor do destinatário", mensagens)
        self.assertIn("só o destinatário confirma", mensagens)
        self.assertEqual([e["evento"] for e in self.envio.eventos_provedor], ["requests", "delivered"])
        pagina = self.client.get("/admin/diagnostico-email").get_data(as_text=True)
        self.assertIn("Recebido pelo Brevo", pagina)
        self.assertIn("Entregue ao servidor do destinatário", pagina)
        for privado_valor in ("PRIVATE_SUBJECT", "203.0.113.9", "PRIVATE_REASON", "privado.example.invalid"):
            self.assertNotIn(privado_valor, pagina + mensagens + saida)
        self.assertIn(f"correlacao={self.envio.correlacao} ok=True eventos=requests,delivered", saida)
        self.assertNotIn("admin@example.invalid", saida)
        self.assertNotIn(CHAVE_API, saida)

    def test_consulta_sem_eventos_com_problema_ou_com_falha(self):
        _r, _h, mensagens, _s = self._consultar([_eventos()])
        self.assertIn("ainda não registrou eventos", mensagens)
        _r, _h, mensagens, _s = self._consultar([_eventos(
            {"event": "requests", "date": "2026-11-01T10:00:00Z", "messageId": ID_MENSAGEM},
            {"event": "hardBounces", "date": "2026-11-01T10:00:02Z", "messageId": ID_MENSAGEM,
             "reason": "PRIVATE_REASON"})])
        self.assertIn("registrou um problema na entrega", mensagens)
        self.assertIn("Devolvido (hard bounce)", mensagens)
        self.assertNotIn("PRIVATE_REASON", mensagens)
        _r, _h, mensagens, _s = self._consultar([_eventos(
            {"event": "requests", "date": "2026-11-01T10:00:00Z", "messageId": ID_MENSAGEM})])
        self.assertIn("ainda não a entrega", mensagens)
        _r, _h, mensagens, saida = self._consultar([_erro_http(401, {"code": "unauthorized"})])
        self.assertIn("Não foi possível consultar a entrega no Brevo", mensagens)
        self.assertIn("recusou a chave de API", mensagens)
        self.assertIn("ok=False", saida)
        _r, _h, mensagens, _s = self._consultar([URLError(OSError("PRIVATE"))])
        self.assertIn("o Brevo não concluiu a consulta agora (o aceite do envio continua valendo)", mensagens)
        # Corpo que não é o da API de eventos não vira "sem eventos".
        _r, _h, mensagens, _s = self._consultar([_RespostaBrevo(corpo=b"<html>", status=200)])
        self.assertIn("Não foi possível consultar a entrega no Brevo", mensagens)

    def test_consulta_exige_admin_csrf_e_envio_do_historico(self):
        anonimo = self.app.test_client()
        with patch.object(emails.urllib.request, "urlopen") as http:
            sem_login = anonimo.post(f"/admin/diagnostico-email/entrega/{self.envio.correlacao}",
                                     data={"csrf_token": self._csrf(anonimo)})
            self.assertEqual(sem_login.status_code, 302)
            self.assertIn("/auth/login", sem_login.headers["Location"])
            sem_csrf = self.client.post(f"/admin/diagnostico-email/entrega/{self.envio.correlacao}")
            self.assertEqual(sem_csrf.status_code, 400)
            self.client.post("/admin/diagnostico-email/entrega/000000000000",
                             data={"csrf_token": self._csrf(self.client, "/admin/diagnostico-email")})
            self.assertIn("não está mais no histórico recente", self._flashes(self.client))
            # Envio por SMTP não tem identificação no Brevo: nada é consultado.
            self.app.config["BREVO_API_KEY"] = None
            self.client.post("/admin/diagnostico-email/teste",
                             data={"csrf_token": self._csrf(self.client, "/admin/diagnostico-email")})
            self._flashes(self.client)
            smtp = self._ultimo("teste")
            self.assertEqual(smtp.transporte, "smtp")
            self.client.post(f"/admin/diagnostico-email/entrega/{smtp.correlacao}",
                             data={"csrf_token": self._csrf(self.client, "/admin/diagnostico-email")})
            self.assertIn("não tem identificação no Brevo", self._flashes(self.client))
        http.assert_not_called()


# ---------------------------------------------------------------------------
# T03/T04/T11 — Recuperação: sem promessa falsa e sem revelar contas
# ---------------------------------------------------------------------------
class RecuperacaoPublicaTests(_EmailCase):
    def test_t03_com_envio_ativo_a_resposta_nao_afirma_envio_concluido(self):
        pagina = self._request_link("active@example.invalid").get_data(as_text=True)
        self.assertIn("enviaremos um link de redefinição válido por 15 minutos", pagina)
        self.assertNotIn("enviamos um link", pagina)
        self.assertEqual(len(self.jobs), 1)            # só agendado neste instante
        self.assertEqual(self._ultimo("recuperacao_senha").estado, EstadoEnvio.AGENDADO)

    def test_t04_com_envio_desativado_a_resposta_e_identica_e_sem_consulta_ao_banco(self):
        self.app.config["MAIL_ATIVO"] = False
        resultados = []
        for endereco in ("active@example.invalid", "Active@Example.invalid", "nao-existe@example.invalid"):
            consultas = []
            with self.app.app_context():
                motor = db.engine
            ouvinte = lambda *a, **kw: consultas.append(a[2])  # noqa: E731 — instrução SQL
            event.listen(motor, "before_cursor_execute", ouvinte)
            try:
                pagina = self._request_link(endereco).get_data(as_text=True)
            finally:
                event.remove(motor, "before_cursor_execute", ouvinte)
            usuarios = [sql for sql in consultas if "usuario" in sql.lower()]
            resultados.append(("No momento não é possível enviar o link" in pagina, usuarios))
        self.assertEqual(resultados, [(True, [])] * 3)


# ---------------------------------------------------------------------------
# Telas de operação: verdadeiras, profissionais e com referência
# ---------------------------------------------------------------------------
class TelasDeOperacaoTests(_EmailCase):
    def test_cadastro_com_cada_falha_do_provedor_mostra_mensagem_profissional(self):
        self.app.config["BREVO_API_KEY"] = CHAVE_API
        ip = ("We have detected you are using an unrecognised IP address 203.0.113.77. "
              "https://app.brevo.com/security/authorised_ips")
        casos = (
            ([_erro_http(401, {"code": "unauthorized"})], "precisa de ajuste de configuração"),
            ([_erro_http(401, {"code": "unauthorized", "message": ip})], "precisa de ajuste de configuração"),
            ([_erro_http(400, {"code": "invalid_parameter", "message": "sender is not valid"})],
             "precisa de ajuste de configuração"),
            ([_erro_http(400, {"code": "DMARC policy requires domain authentication"})],
             "precisa de ajuste de configuração"),
            ([_erro_http(429, {"code": "too_many_requests"}, {"x-sib-ratelimit-reset": "60"})],
             "o limite de envios do serviço de e-mail foi atingido. Tente novamente mais tarde"),
            ([URLError(OSError("PRIVATE")), URLError(OSError("PRIVATE"))], "temporariamente indisponível"),
            ([_erro_http(504)], "pode não ter sido enviado"),
        )
        client = self._admin()
        for indice, (efeito, esperado) in enumerate(casos):
            with self.subTest(esperado=esperado, indice=indice):
                with patch.object(emails.urllib.request, "urlopen", side_effect=efeito):
                    self._cadastrar(client, cnpj=f"8877766600010{indice}",
                                    usuario_email=f"novo{indice}@example.invalid")
                mensagens = self._flashes(client)
                self.assertIn(esperado, mensagens)
                self._assert_mensagem_de_operacao(mensagens)
                self.assertNotIn("203.0.113.77", mensagens)
                envio = self._ultimo("boas_vindas")
                diagnostico = client.get("/admin/diagnostico-email").get_data(as_text=True)
                # O detalhe técnico continua disponível no mecanismo administrativo.
                self.assertIn(str(escape(envio.descricao_categoria)), diagnostico)


# ---------------------------------------------------------------------------
# T09 — sem envio duplicado
# ---------------------------------------------------------------------------
class DuplicidadeTests(_EmailCase):
    def test_t09_cadastro_repetido_nao_gera_segunda_ativacao(self):
        client = self._admin()
        self.assertEqual(self._cadastrar(client).status_code, 302)
        self._flashes(client)
        self.assertEqual(len(self.sent), 1)
        self._cadastrar(client)                        # mesmo CNPJ e mesmo e-mail
        self.assertIn("já existe um usuário com este e-mail", self._flashes(client).lower())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self._usuario_por_email("novo@example.invalid")), 1)


# ---------------------------------------------------------------------------
# Configuração de produção (Render)
# ---------------------------------------------------------------------------
class ConfiguracaoDeProducaoTests(_Subprocesso):
    def test_nexo_public_url_em_branco_usa_a_url_injetada_pelo_render(self):
        resultado = self._executar(
            "from app import create_app\n"
            "from origem_publica import url_publica\n"
            "app = create_app('production')\n"
            "with app.app_context():\n"
            "    print(url_publica('auth.recuperar_senha'))\n",
            FLASK_ENV="production", RENDER="true", NEXO_PUBLIC_URL="   ",
            RENDER_EXTERNAL_URL="https://nexo-teste.example.invalid",
            BREVO_API_KEY="xkeysib-synthetic-PRIVATE-KEY", MAIL_DEFAULT_SENDER="NEXO <nexo@example.invalid>",
        )
        self.assertIn("https://nexo-teste.example.invalid/auth/recuperar-senha", resultado.stdout)

    def test_diagnostico_aponta_credenciais_smtp_sem_uso_quando_o_brevo_esta_ativo(self):
        config = {"MAIL_ATIVO": True, "BREVO_API_KEY": "xkeysib-synthetic", "BREVO_API_URL": URL_BREVO,
                  "MAIL_USERNAME": "conta.teste@gmail.com", "MAIL_PASSWORD": "PRIVATE",
                  "MAIL_DEFAULT_SENDER": "NEXO <nexo@example.invalid>", "PUBLIC_URL": "https://nexo.example.invalid"}
        diagnostico = emails.diagnostico_configuracao(config)
        self.assertEqual(diagnostico["transporte"], "brevo")
        self.assertFalse(diagnostico["bloqueante"])
        self.assertTrue(any("não são usadas" in p["texto"] for p in diagnostico["pendencias"]))
        self.assertNotIn("PRIVATE", json.dumps(diagnostico, default=str))



# ---------------------------------------------------------------------------
# Achados da revisão adversarial (cada um reproduzido antes da correção)
# ---------------------------------------------------------------------------
class RevisaoConsultaDeEntregaTests(_EntregaBase):
    def _pagina(self):
        return self.client.get("/admin/diagnostico-email").get_data(as_text=True)

    def test_consulta_que_falha_nao_vira_nenhum_evento_nem_apaga_os_obtidos(self):
        self._consultar([_erro_http(401, {"code": "unauthorized"})])
        pagina = self._pagina()
        self.assertIn("A última consulta ao Brevo não foi concluída", pagina)
        self.assertNotIn("Nenhum evento registrado pelo Brevo", pagina)
        self._consultar([_eventos({"event": "delivered", "date": "2026-11-01T10:00:05Z", "messageId": ID_MENSAGEM})])
        self.assertIn("Entregue ao servidor do destinatário", self._pagina())
        self._consultar([TimeoutError("PRIVATE")])
        pagina = self._pagina()
        self.assertIn("Entregue ao servidor do destinatário", pagina)       # o obtido continua
        self.assertIn("A última consulta ao Brevo não foi concluída", pagina)
        self._consultar([_eventos()])
        self.assertIn("Nenhum evento registrado pelo Brevo na última consulta", self._pagina())

    def test_comportamento_do_destinatario_nao_e_guardado_exibido_nem_registrado(self):
        base = {"date": "2026-11-01T10:00:00Z", "messageId": ID_MENSAGEM, "email": "admin@example.invalid"}
        _r, _h, mensagens, saida = self._consultar([_eventos(
            dict(base, event="requests"), dict(base, event="delivered"), dict(base, event="opened"),
            dict(base, event="clicks"), dict(base, event="unsubscribed"), dict(base, event="loadedByProxy"),
            dict(base, event="spam"))])
        self.assertEqual([e["evento"] for e in self.envio.eventos_provedor], ["requests", "delivered"])
        pagina = self._pagina()
        for comportamento in ("Aberto", "clicado", "Descadastrado", "Denunciado", "opened", "clicks",
                              "unsubscribed", "loadedByProxy", ",spam"):
            self.assertNotIn(comportamento, mensagens + saida)
        for rotulo in ("<li>Aberto", "Link clicado", "Descadastrado", "Denunciado como spam", "proxy de privacidade"):
            self.assertNotIn(rotulo, pagina)
        self.assertIn("<li>Entregue ao servidor do destinatário", pagina)
        self.assertIn("eventos=requests,delivered ", saida)

    def test_resposta_malformada_ou_data_extrema_nao_derrubam_a_pagina(self):
        for corpo in (b"[" * 30000 + b"]" * 30000,
                      json.dumps({"events": [{"event": ["delivered"], "messageId": ID_MENSAGEM}]}).encode(),
                      json.dumps({"events": {"event": "delivered"}}).encode()):
            with self.subTest(tamanho=len(corpo)):
                resposta, _h, mensagens, _s = self._consultar([_RespostaBrevo(corpo=corpo, status=200)])
                self.assertEqual(resposta.status_code, 302)
                self.assertIn("o Brevo não concluiu a consulta agora", mensagens)
        resposta, _h, _m, _s = self._consultar([_eventos(
            {"event": "delivered", "date": "0001-01-01T00:00:00Z", "messageId": ID_MENSAGEM},
            {"event": "requests", "date": "9999-12-31T23:59:59-03:00", "messageId": ID_MENSAGEM})])
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual([e["data"] for e in self.envio.eventos_provedor], [None, None])
        pagina = self.client.get("/admin/diagnostico-email")
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("Entregue ao servidor do destinatário", pagina.get_data(as_text=True))

    def test_falha_da_consulta_nao_contradiz_o_aceite_do_envio(self):
        for efeito in (_erro_http(504), TimeoutError("PRIVATE"), _erro_http(429, {"code": "too_many_requests"}),
                       _erro_http(403, {"code": "permission_denied"}), ConnectionResetError("PRIVATE")):
            with self.subTest(efeito=type(efeito).__name__):
                _r, _h, mensagens, _s = self._consultar([efeito])
                self.assertIn("o Brevo não concluiu a consulta agora (o aceite do envio continua valendo)", mensagens)
                for enganoso in ("aceite não foi confirmado", "limite de envios", "envio transacional", "reenviar"):
                    self.assertNotIn(enganoso, mensagens)
        _r, _h, mensagens, _s = self._consultar([_erro_http(401, {"code": "unauthorized"})])
        self.assertIn("recusou a chave de API", mensagens)   # a chave vale para a consulta também


class RevisaoClassificacaoTests(_EmailCase):
    def setUp(self):
        super().setUp()
        self.app.config["BREVO_API_KEY"] = CHAVE_API

    def test_destinatario_recusado_pelo_brevo_e_endereco_invalido_no_cadastro(self):
        client = self._admin()
        with patch.object(emails.urllib.request, "urlopen", side_effect=[_erro_http(400, {
                "code": "invalid_parameter", "message": "email is not valid in to"})]):
            self._cadastrar(client)
        envio = self._ultimo("boas_vindas")
        self.assertEqual((envio.categoria, envio.http_status), ("destinatario_invalido", 400))
        mensagens = self._flashes(client)
        self.assertIn("o endereço de e-mail do destinatário é inválido ou não foi aceito", mensagens)
        self.assertNotIn("ajuste de configuração", mensagens)
        self.assertNotIn("reenvie o link", mensagens)         # reenviar ao mesmo endereço não adianta
        self._assert_mensagem_de_operacao(mensagens)

    def test_causa_desconhecida_nao_vira_ajuste_de_configuracao(self):
        client = self._admin()
        with patch.object(emails.urllib.request, "urlopen", side_effect=[_erro_http(400, {
                "code": "invalid_parameter", "message": "PRIVATE_SENTINEL"})]):
            self._cadastrar(client)
        self.assertEqual(self._ultimo("boas_vindas").categoria, "requisicao_rejeitada")
        mensagens = self._flashes(client)
        self.assertIn("não foi enviado (a causa está no Diagnóstico de e-mail)", mensagens)
        self.assertNotIn("ajuste de configuração", mensagens)
        self._assert_mensagem_de_operacao(mensagens)

    def test_dmarc_num_429_ou_5xx_mantem_o_significado_do_status(self):
        with patch.object(emails.urllib.request, "urlopen", side_effect=[
                _erro_http(503, {"message": "temporary failure while checking DMARC record"}), _RespostaBrevo()]) as http:
            self._request_link("active@example.invalid")
            self._entregar()
        self.assertEqual(http.call_count, 2)
        self.assertEqual(self._ultimo("recuperacao_senha").estado, EstadoEnvio.ACEITO_PELO_PROVEDOR)
        with patch.object(emails.urllib.request, "urlopen", side_effect=[
                _erro_http(429, {"message": "domain authentication rate exceeded"}, {"x-sib-ratelimit-reset": "60"})]):
            self._request_link("active@example.invalid")
            self._entregar()
        self.assertEqual(self._ultimo("recuperacao_senha").categoria, "limite_excedido")

    def test_cota_diaria_do_gmail_pede_para_tentar_mais_tarde(self):
        self.app.config.update(BREVO_API_KEY=None, MAIL_USERNAME="conta.teste@gmail.com", MAIL_PASSWORD="PRIVATE")
        client = self._admin()
        self.transport.side_effect = smtplib.SMTPSenderRefused(
            550, b"5.4.5 Daily user sending limit exceeded. PRIVATE", "conta.teste@gmail.com")
        self._cadastrar(client)
        envio = self._ultimo("boas_vindas")
        self.assertEqual((envio.categoria, envio.codigo_provedor), ("limite_excedido", "550/5.4.5"))
        mensagens = self._flashes(client)
        self.assertIn("o limite de envios do serviço de e-mail foi atingido. Tente novamente mais tarde", mensagens)
        self.assertNotIn("em alguns minutos", mensagens)


class RevisaoSMTPTests(_GmailBase):
    """Status estendido só no início da resposta (nunca dígitos de IP ou endereço)."""

    def test_status_estendido_so_vale_no_inicio_da_resposta(self):
        casos = (
            (smtplib.SMTPRecipientsRefused({"x@example.invalid": (550, b"<maria.2.10.85@example.invalid>: "
                                                                        b"Recipient address rejected")}),
             "550", "smtp_destinatario_recusado"),
            (smtplib.SMTPDataError(554, b"rejected: 10.5.4.5 listed at zen.spamhaus.org"), "554", "smtp_erro"),
            (smtplib.SMTPSenderRefused(550, b"see https://help.example.invalid/codes/5.4.5 for details",
                                       "x@example.invalid"), "550", "smtp_remetente_recusado"),
            (smtplib.SMTPDataError(550, b"4.7.0 classe diferente do codigo"), "550", "smtp_erro"),
            (smtplib.SMTPAuthenticationError(534, b"5.7.9 Application-specific password required. Learn more "
                                                  b"at\n5.7.9  https://support.google.com/mail/?p=x"),
             "534/5.7.9", "smtp_senha_de_app"),
        )
        for erro, codigo, categoria in casos:
            with self.subTest(codigo=codigo, categoria=categoria):
                envio = emails.EnvioEmail("recuperacao_senha")
                with self.app.app_context(), patch.object(emails.mail, "send", side_effect=erro), \
                        self.assertLogs(emails.logger, level="WARNING") as logs:
                    emails._executar_envio(self.app, self._mensagem(), envio)
                self.assertEqual((envio.codigo_provedor, envio.categoria), (codigo, categoria))
                saida = "\n".join(logs.output)
                for privado in ("2.10.85", "10.5.4.5", "help.example", "maria"):
                    self.assertNotIn(privado, saida)


class RevisaoRecuperacaoTests(_EmailCase):
    def test_configuracao_que_bloqueia_todo_envio_nao_promete_link(self):
        casos = (
            ("chave SMTP no lugar da de API", dict(BREVO_API_KEY="xsmtpsib-" + "synthetic-smtp-key")),
            ("URL da API sem https", dict(BREVO_API_KEY=CHAVE_API,
                                          BREVO_API_URL="http://api.brevo-test.example.invalid/v3/smtp/email")),
            ("SMTP no Render Free", dict(BREVO_API_KEY=None, HOSPEDAGEM_BLOQUEIA_SMTP=True, MAIL_USERNAME="c@example.invalid",
                                         MAIL_PASSWORD="PRIVATE")),
        )
        for nome, config in casos:
            with self.subTest(nome):
                self.app.config.update({"BREVO_API_KEY": None, "BREVO_API_URL": URL_BREVO, "HOSPEDAGEM_BLOQUEIA_SMTP": False,
                                        "SMTP_PERMITIDO": False, **config})
                with patch.object(emails.urllib.request, "urlopen") as http:
                    pagina = self._request_link("active@example.invalid").get_data(as_text=True)
                    self._entregar()
                http.assert_not_called()
                self.assertIn("No momento não é possível enviar o link de redefinição", pagina)
                self.assertNotIn("Se o e-mail estiver cadastrado", pagina)
                self.assertEqual(self._ultimo("recuperacao_senha").categoria, "envio_bloqueado")
        # Remetente inválido também bloqueia (verificado antes de qualquer conta).
        self.app.config.update(BREVO_API_KEY=CHAVE_API, HOSPEDAGEM_BLOQUEIA_SMTP=False)
        self.app.config["MAIL_DEFAULT_SENDER"] = "NEXO sem endereco"
        pagina = self._request_link("active@example.invalid").get_data(as_text=True)
        self.assertIn("No momento não é possível enviar o link de redefinição", pagina)

    def test_smtp_em_instancia_paga_do_render_com_nexo_smtp_permitido(self):
        self.app.config.update(BREVO_API_KEY=None, HOSPEDAGEM_BLOQUEIA_SMTP=True, SMTP_PERMITIDO=True,
                               MAIL_USERNAME="c@example.invalid", MAIL_PASSWORD="PRIVATE")
        pagina = self._request_link("active@example.invalid").get_data(as_text=True)
        self.assertIn("Se o e-mail estiver cadastrado, enviaremos um link", pagina)
        self.assertEqual(len(self.jobs), 1)
        diagnostico = emails.diagnostico_configuracao(self.app.config)
        self.assertFalse(diagnostico["bloqueante"])
        self.assertTrue(any("NEXO_SMTP_PERMITIDO=1" in p["texto"] and not p["bloqueante"]
                            for p in diagnostico["pendencias"]))

    def test_envio_suprimido_avisa_os_admins_no_sininho_sem_dados_da_conta(self):
        from models import Notificacao
        from sqlalchemy import select
        self.app.config.update(MAIL_ATIVO=False, EMAIL_ALERTA_ADMIN=True)
        for endereco in ("active@example.invalid", "nao-existe@example.invalid"):
            self._request_link(endereco)
            self._entregar()
        with self.app.app_context():
            avisos = db.session.scalars(select(Notificacao.texto).where(
                Notificacao.id_usuario == self.ids["admin"])).all()
        self.assertEqual(len(avisos), 1, avisos)              # no máximo 1 por categoria a cada 30 min
        self.assertIn("o serviço de e-mail não está configurado", avisos[0])
        for privado in ("active@example.invalid", "nao-existe@example.invalid", "usuario:"):
            self.assertNotIn(privado, avisos[0])


class RevisaoDiagnosticoTests(unittest.TestCase):
    BASE = {"MAIL_ATIVO": True, "BREVO_API_URL": URL_BREVO, "PUBLIC_URL": "https://nexo.example.invalid",
            "MAIL_USERNAME": "conta.teste@gmail.com", "MAIL_PASSWORD": "PRIVATE"}

    def test_mail_username_que_e_o_remetente_nao_e_descrito_como_sem_uso(self):
        com_fallback = emails.diagnostico_configuracao(dict(
            self.BASE, BREVO_API_KEY="xkeysib-synthetic", MAIL_DEFAULT_SENDER="conta.teste@gmail.com"))
        textos = " ".join(p["texto"] for p in com_fallback["pendencias"])
        self.assertIn("MAIL_USERNAME continua sendo o remetente", textos)
        self.assertNotIn("não são usadas", textos)
        explicito = emails.diagnostico_configuracao(dict(
            self.BASE, BREVO_API_KEY="xkeysib-synthetic", MAIL_DEFAULT_SENDER="NEXO <nexo@example.invalid>"))
        self.assertIn("não são usadas", " ".join(p["texto"] for p in explicito["pendencias"]))

    def test_smtp_no_render_so_deixa_de_bloquear_com_nexo_smtp_permitido(self):
        smtp = dict(self.BASE, BREVO_API_KEY=None, MAIL_DEFAULT_SENDER="conta.teste@gmail.com", HOSPEDAGEM_BLOQUEIA_SMTP=True)
        self.assertTrue(emails.diagnostico_configuracao(smtp)["bloqueante"])
        self.assertFalse(emails.envio_disponivel(smtp))
        liberado = dict(smtp, SMTP_PERMITIDO=True)
        self.assertFalse(emails.diagnostico_configuracao(liberado)["bloqueante"])
        self.assertTrue(emails.envio_disponivel(liberado))


class RevisaoConfiguracaoTests(_Subprocesso):
    def test_nexo_smtp_permitido_e_lido_do_ambiente(self):
        resultado = self._executar(
            "from config import DevelopmentConfig as C\n"
            "print('PERMITIDO=' + str(C.SMTP_PERMITIDO))\n",
            FLASK_ENV="development", NEXO_SMTP_PERMITIDO="1")
        self.assertIn("PERMITIDO=True", resultado.stdout)
        resultado = self._executar(
            "from config import DevelopmentConfig as C\n"
            "print('PERMITIDO=' + str(C.SMTP_PERMITIDO))\n",
            FLASK_ENV="development")
        self.assertIn("PERMITIDO=False", resultado.stdout)


if __name__ == "__main__":
    unittest.main()
