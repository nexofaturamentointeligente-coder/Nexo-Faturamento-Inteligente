"""E-mails transacionais (T01–T17): estados operacionais, Brevo e SMTP simulados.

Executar da raiz: python -B -m unittest tests.test_emails_transacionais -v

Nenhuma mensagem real é enviada e não há rede: urlopen, smtplib e a thread de
envio são substituídos, e qualquer conexão de socket falha o teste. Endereços
são fictícios (.invalid). As chaves de exemplo são montadas em tempo de
execução e não têm o formato de uma credencial real.
"""

import contextlib
from collections import deque
import email.message
import html as html_lib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import smtplib
import ssl
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from flask import Flask
from flask_mail import Message
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash

from blueprints import admin, auth
import emails
import extensions
from emails import EstadoEnvio
from extensions import db
from models import Empresa, Notificacao, Usuario
from tests.test_auth_tokens_security import _TokenCase, FIXTURE_HASH, NEW_PASSWORD, OLD_PASSWORD
from .deploy_support import DeployTestCase

ROOT = Path(__file__).resolve().parents[1]
URL_BREVO = "https://api.brevo-test.example.invalid/v3/smtp/email"
CHAVE_API = "xkeysib-" + "synthetic-test-key-" + "PRIVATE-KEY-SENTINEL"
REMETENTE = "NEXO Teste <remetente-teste@example.invalid>"
ID_MENSAGEM = "<202610311200.123456789@relay.example.invalid>"
_REF = re.compile(r"Ref\. [0-9a-f]{12}\.")


class _RespostaBrevo:
    """Resposta 201 da API transacional (urlopen simulado)."""

    def __init__(self, corpo=None, status=201):
        self.status = status
        self._corpo = json.dumps({"messageId": ID_MENSAGEM}).encode() if corpo is None else corpo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self._corpo


def _threading_com(classe_thread):
    """O módulo threading visto só por emails.py, com a Thread substituída.

    Substituir threading.Thread globalmente quebraria quem cria threads por
    conta própria (o armazenamento em memória do Flask-Limiter usa Timer).
    """
    atributos = {nome: getattr(threading, nome) for nome in dir(threading) if not nome.startswith("__")}
    atributos["Thread"] = classe_thread
    return SimpleNamespace(**atributos)


def _erro_http(codigo, corpo=None, cabecalhos=None):
    hdrs = email.message.Message()
    for chave, valor in (cabecalhos or {}).items():
        hdrs[chave] = valor
    bruto = json.dumps(corpo).encode() if isinstance(corpo, dict) else (corpo or b"")
    return HTTPError(URL_BREVO, codigo, "PRIVATE_REASON_SENTINEL", hdrs, io.BytesIO(bruto))


class _EmailCase(_TokenCase):
    """Fluxos reais (templates, tokens, rotas) com transporte e thread controlados."""

    THREADS_SIMULADAS = True

    def setUp(self):
        super().setUp()
        # Os testes de tokens substituem os gatilhos; aqui valem os reais.
        for alvo, nome in ((auth, "email_recuperacao_senha"), (auth, "email_boas_vindas"),
                           (admin, "email_boas_vindas"), (auth, "envio_disponivel")):
            self.stack.enter_context(patch.object(alvo, nome, getattr(emails, nome)))
        self.app.config.update(
            MAIL_ATIVO=True, BREVO_API_KEY=None, BREVO_API_URL=URL_BREVO,
            EMAIL_TIMEOUT_SEGUNDOS=7.0, EMAIL_ALERTA_ADMIN=False,
        )
        self.app.config["MAIL_DEFAULT_SENDER"] = REMETENTE
        self.app.extensions["mail"].default_sender = REMETENTE
        self.jobs, self.sent = [], []
        jobs = self.jobs

        class FilaDeThreads:
            def __init__(fila, target, args, daemon):
                fila.target, fila.args, fila.daemon = target, args, daemon

            def start(fila):
                jobs.append(fila)

            def run(fila):
                fila.target(*fila.args)

        if self.THREADS_SIMULADAS:
            self.stack.enter_context(patch.object(emails, "threading", _threading_com(FilaDeThreads)))
        self.transport = self.stack.enter_context(patch.object(emails.mail, "send", side_effect=self.sent.append))
        self.pausas = self.stack.enter_context(patch.object(emails, "_dormir"))
        self.stack.enter_context(patch.object(emails.logger, "disabled", False))
        for nome, valor in (("_HISTORICO", deque(maxlen=50)), ("_EM_ANDAMENTO", {}), ("_ULTIMO_ALERTA", {})):
            self.stack.enter_context(patch.object(emails, nome, valor))
        with self.app.app_context():
            self.empresa_id = db.session.get(Usuario, self.ids["active"]).id_empresa

    # ------------------------------------------------------------ apoio
    def _entregar(self):
        jobs, self.jobs[:] = list(self.jobs), []
        for job in jobs:
            job.run()

    def _admin(self):
        client = self.app.test_client()
        resposta = client.post("/auth/login", data={
            "email": "admin@example.invalid", "senha": OLD_PASSWORD, "csrf_token": self._csrf(client),
        })
        self.assertEqual(resposta.status_code, 302)
        return client

    def _cadastrar(self, client, **campos):
        dados = {
            "data_contratacao": "2026-01-01", "id_segmento": str(self.segment_id),
            "id_plano_atual": str(self.plan_id), "cnpj": "99999999999999",
            "razao_social": "Synthetic T company", "nome_fantasia": "Synthetic Fantasia",
            "email_contato": "contato-novo@example.invalid",
            "usuario_nome": "Synthetic Novo", "usuario_email": "novo@example.invalid",
        }
        dados.update(campos)
        dados["csrf_token"] = self._csrf(client, "/admin/empresas/nova")
        return client.post("/admin/empresas/nova", data=dados)

    def _flashes(self, client):
        with client.session_transaction() as sessao:
            return " ".join(texto for _categoria, texto in sessao.get("_flashes", []))

    def _ultimo(self, tipo=None):
        for envio in emails.historico_envios():
            if tipo is None or envio.tipo == tipo:
                return envio
        self.fail("Nenhum envio registrado.")

    def _link(self, html, prefixo):
        links = [html_lib.unescape(link) for link in re.findall(r'href="([^"]+)"', html)]
        links = [link for link in links if urlsplit(link).path.startswith(prefixo)]
        self.assertEqual(len(links), 1, links)
        return links[0]

    def _token(self, link):
        return urlsplit(link).path.rsplit("/", 1)[1]

    def _usuario_por_email(self, endereco):
        with self.app.app_context():
            return db.session.scalars(select(Usuario).where(
                func.lower(Usuario.email) == endereco.lower())).all()

    def _criar_usuario(self, endereco, *, empresa_id=None, pendente=False):
        with self.app.app_context():
            usuario = Usuario(nome="Legacy synthetic", email=endereco, role="CLIENTE",
                              id_empresa=empresa_id or self.empresa_id, primeiro_acesso=pendente)
            usuario.senha_hash = FIXTURE_HASH
            db.session.add(usuario)
            db.session.commit()
            return usuario.id_usuario

    def _assert_sem_segredos(self, texto, *extras):
        for proibido in (CHAVE_API, "PRIVATE_", *extras):
            self.assertNotIn(proibido, texto)

    def _assert_mensagem_de_operacao(self, texto):
        """Tela de operação: verdadeira e com referência, sem jargão de infraestrutura."""
        self.assertRegex(texto, _REF)
        for tecnico in ("BREVO_", "MAIL_", "xkeysib", "Render", "SMTP", "Brevo", "IPs autorizados", "HTTP "):
            self.assertNotIn(tecnico, texto)


class CadastroEmpresaTests(_EmailCase):
    def test_t01_cadastro_persiste_empresa_e_usuario_e_solicita_ativacao(self):
        client = self._admin()
        resposta = self._cadastrar(client, usuario_email="  Novo.Cliente@Example.Invalid ",
                                   email_contato=" Contato.Novo@Example.Invalid")
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.headers["Location"].endswith("/admin/empresas"))
        with self.app.app_context():
            empresa = db.session.scalar(select(Empresa).where(Empresa.cnpj == "99999999999999"))
            usuario = db.session.scalar(select(Usuario).where(Usuario.id_empresa == empresa.id_empresa))
            # Gravados na forma canônica: login e recuperação encontram a conta.
            self.assertEqual(empresa.email_contato, "contato.novo@example.invalid")
            self.assertEqual(usuario.email, "novo.cliente@example.invalid")
            self.assertEqual((usuario.role, usuario.ativo, usuario.primeiro_acesso), ("CLIENTE", True, True))
            id_usuario = usuario.id_usuario
        # Envio síncrono: a mensagem já foi entregue ao transporte simulado.
        self.assertEqual(self.jobs, [])
        self.assertEqual(len(self.sent), 1)
        mensagem = self.sent[0]
        self.assertEqual(mensagem.recipients, ["novo.cliente@example.invalid"])
        self.assertIn("Bem-vindo", mensagem.subject)
        for trecho in ("NEXO", "Bem-vindo(a) ao NEXO", "Synthetic Fantasia", "novo.cliente@example.invalid",
                       "Definir Senha de Acesso", "15 minutos", "Como acessar depois",
                       self.app.config["PUBLIC_URL"] + "/auth/login", "Nunca enviamos senhas"):
            self.assertIn(trecho, mensagem.html)
        link = self._link(mensagem.html, "/auth/primeiro-acesso/")
        self.assertTrue(link.startswith(self.app.config["PUBLIC_URL"] + "/auth/primeiro-acesso/"))
        self.assertIn(link, mensagem.body)  # alternativa em texto simples
        with self.app.app_context():
            self.assertEqual(auth._ler_token_ativacao(self._token(link)).id_usuario, id_usuario)
        envio = self._ultimo("boas_vindas")
        self.assertEqual((envio.estado, envio.transporte, envio.referencia),
                         (EstadoEnvio.ACEITO_PELO_PROVEDOR, "smtp", f"usuario:{id_usuario}"))
        mensagens = self._flashes(client)
        self.assertIn("cadastrada", mensagens)
        self.assertIn("O e-mail de ativação foi aceito pelo serviço de e-mail.", mensagens)
        self._assert_mensagem_de_operacao(mensagens)
        self.assertIn("não confirma a chegada à caixa de entrada", mensagens)
        self.assertRegex(mensagens, _REF)
        self.assertNotIn("/auth/primeiro-acesso/", mensagens)
        # O link funciona até o fim: ativação e login.
        anonimo = self.app.test_client()
        self._assert_success(self._post(self._token(link), "activation", client=anonimo))
        login = anonimo.post("/auth/login", data={"email": "NOVO.CLIENTE@example.invalid", "senha": NEW_PASSWORD,
                                                  "csrf_token": self._csrf(anonimo)})
        self.assertEqual(login.status_code, 302)
        self.assertTrue(login.headers["Location"].endswith("/cliente/dashboard"))

    def test_t01_email_invalido_ou_ja_usado_nao_cria_cadastro(self):
        client = self._admin()
        for campos in ({"usuario_email": "sem-arroba"}, {"email_contato": "contato@semponto"},
                       {"usuario_email": "  "}):
            with self.subTest(campos=campos):
                resposta = self._cadastrar(client, **campos)
                self.assertTrue(resposta.headers["Location"].endswith("/admin/empresas/nova"))
                self.assertIn("e-mails válidos", self._flashes(client))
        resposta = self._cadastrar(client, usuario_email="ACTIVE@Example.Invalid")
        self.assertTrue(resposta.headers["Location"].endswith("/admin/empresas/nova"))
        self.assertIn("Já existe um usuário com este e-mail", self._flashes(client))
        with self.app.app_context():
            self.assertIsNone(db.session.scalar(select(Empresa).where(Empresa.cnpj == "99999999999999")))
        self.assertEqual(self.sent, [])

    def test_t02_ativacao_pelo_brevo_monta_a_mensagem_e_reconhece_o_aceite(self):
        self.app.config["BREVO_API_KEY"] = CHAVE_API
        client = self._admin()
        with patch.object(emails.urllib.request, "urlopen", return_value=_RespostaBrevo()) as http, \
                self.assertLogs(level="INFO") as logs:
            resposta = self._cadastrar(client)
        self.assertEqual(resposta.status_code, 302)
        self.transport.assert_not_called()
        http.assert_called_once()
        requisicao = http.call_args.args[0]
        self.assertEqual(http.call_args.kwargs["timeout"], 7.0)
        self.assertEqual((requisicao.full_url, requisicao.get_method()), (URL_BREVO, "POST"))
        self.assertEqual(urlsplit(requisicao.full_url).scheme, "https")
        self.assertEqual(requisicao.get_header("Api-key"), CHAVE_API)
        self.assertEqual(requisicao.get_header("Content-type"), "application/json")
        self.assertIn("NEXO", requisicao.get_header("User-agent"))
        corpo = json.loads(requisicao.data)
        self.assertEqual(corpo["sender"], {"name": "NEXO Teste", "email": "remetente-teste@example.invalid"})
        self.assertEqual(corpo["replyTo"], {"name": "NEXO Teste", "email": "remetente-teste@example.invalid"})
        self.assertEqual(corpo["to"], [{"email": "novo@example.invalid"}])
        self.assertIn("Bem-vindo", corpo["subject"])
        self.assertIn("Definir Senha de Acesso", corpo["htmlContent"])
        link = self._link(corpo["htmlContent"], "/auth/primeiro-acesso/")
        self.assertIn(link, corpo["textContent"])
        envio = self._ultimo("boas_vindas")
        self.assertEqual(corpo["tags"], ["nexo-boas_vindas"])
        self.assertEqual(corpo["headers"], {"X-Nexo-Correlacao": envio.correlacao})
        self.assertEqual((envio.estado, envio.transporte, envio.id_mensagem, envio.tentativas),
                         (EstadoEnvio.ACEITO_PELO_PROVEDOR, "brevo", ID_MENSAGEM, 1))
        self.assertTrue(envio.aceito)
        mensagens = self._flashes(client)
        self.assertIn("O e-mail de ativação foi aceito pelo serviço de e-mail.", mensagens)
        self._assert_mensagem_de_operacao(mensagens)
        diagnostico = client.get("/admin/diagnostico-email").get_data(as_text=True)
        self.assertIn(f"ID no Brevo: {html_lib.escape(ID_MENSAGEM)}", diagnostico)
        self.assertIn("Consultar entrega", diagnostico)
        self.assertIn(f"Ref. {envio.correlacao}.", mensagens)
        saida = "\n".join(logs.output)
        self.assertIn("aceito pelo transporte", saida)
        self.assertIn(f"id_mensagem={ID_MENSAGEM}", saida)
        self.assertIn(f"correlacao={envio.correlacao}", saida)
        self._assert_sem_segredos(saida, link, self._token(link), "novo@example.invalid")

    def test_cadastro_de_empresa_cancelada_continua_sem_ativacao(self):
        client = self._admin()
        self._cadastrar(client, status_conta="CANCELADA")
        self.assertEqual(self.sent, [])
        self.assertIn("não foi enviado", self._flashes(client))


class RecuperacaoSenhaTests(_EmailCase):
    def test_t03_recuperacao_gera_token_valido_url_publica_e_chama_o_transporte(self):
        with self.assertLogs(level="INFO") as logs:
            pagina = self._request_link("active@example.invalid")
            self.assertIn("Se o e-mail estiver cadastrado", pagina.get_data(as_text=True))
            self.assertEqual(len(self.jobs), 1)   # agendado, ainda não entregue
            self.assertEqual(self.sent, [])
            agendado = self._ultimo("recuperacao_senha")
            self.assertEqual(agendado.estado, EstadoEnvio.AGENDADO)
            self.assertFalse(agendado.aceito)
            self._entregar()
        self.assertEqual(len(self.sent), 1)
        mensagem = self.sent[0]
        self.assertEqual(mensagem.recipients, ["active@example.invalid"])
        link = self._link(mensagem.html, "/auth/resetar-senha/")
        self.assertTrue(link.startswith(self.app.config["PUBLIC_URL"] + "/auth/resetar-senha/"))
        with self.app.app_context():
            self.assertEqual(auth._ler_token_reset(self._token(link)).id_usuario, self.ids["active"])
        self.assertEqual(agendado.estado, EstadoEnvio.ACEITO_PELO_PROVEDOR)
        saida = "\n".join(logs.output)
        self.assertIn(f"estado=AGENDADO categoria=- http=- codigo=- erro=- tentativas=0 correlacao={agendado.correlacao}",
                      saida)
        self.assertIn(f"estado=ACEITO_PELO_PROVEDOR", saida)
        self.assertIn(f"Solicitação de recuperação de senha processada | usuario={self.ids['active']} "
                      f"estado=AGENDADO correlacao={agendado.correlacao}", saida)
        self._assert_sem_segredos(saida, link, self._token(link), "active@example.invalid")

    def test_t04_email_inexistente_resposta_generica_sem_envio(self):
        with self.assertLogs(auth.logger, level="INFO") as logs:
            existente = self._request_link("active@example.invalid", self.app.test_client())
            inexistente = self._request_link("ninguem@example.invalid", self.app.test_client())
        generica = "Se o e-mail estiver cadastrado, enviaremos um link de redefinição válido por 15 minutos."
        self.assertIn(generica, existente.get_data(as_text=True))
        self.assertIn(generica, inexistente.get_data(as_text=True))
        self.assertEqual(len(self.jobs), 1)  # só a conta existente
        self.assertEqual(self.jobs[0].args[1].recipients, ["active@example.invalid"])
        saida = "\n".join(logs.output)
        self.assertIn("nenhuma conta com o e-mail informado", saida)
        self.assertNotIn("ninguem@example.invalid", saida)

    def test_t05_sem_chave_brevo_e_sem_smtp_o_envio_e_suprimido_sem_falsa_confirmacao(self):
        self.app.config["MAIL_ATIVO"] = False
        paginas = []
        for endereco in ("active@example.invalid", "nao-existe@example.invalid"):
            with self.assertLogs(auth.logger, level="WARNING") as logs:
                paginas.append(self._request_link(endereco).get_data(as_text=True))
            self.assertIn("a configuração atual não permite enviar e-mails", "\n".join(logs.output))
            self.assertNotIn(endereco, "\n".join(logs.output))
        # Estado do sistema, não da conta: a mesma resposta para qualquer endereço,
        # sem prometer um link que não sai.
        for pagina in paginas:
            self.assertIn("No momento não é possível enviar o link de redefinição por e-mail", pagina)
            self.assertNotIn("Se o e-mail estiver cadastrado", pagina)
        self.assertEqual((self.jobs, self.sent), ([], []))
        # O pedido entra no histórico como suprimido, sem nenhum dado da conta.
        suprimidos = emails.historico_envios()
        self.assertEqual([(e.tipo, e.estado, e.categoria, e.referencia) for e in suprimidos],
                         [("recuperacao_senha", EstadoEnvio.DESATIVADO, "transporte_desativado", None)] * 2)
        self.assertEqual(self._account(), (FIXTURE_HASH, False, True))  # nenhum token emitido
        diagnostico = emails.diagnostico_configuracao(self.app.config)
        self.assertFalse(diagnostico["ativo"])
        self.assertTrue(diagnostico["bloqueante"])
        # O administrador vê no cadastro que nada saiu, sem confirmação falsa.
        client = self._admin()
        with self.assertLogs(emails.logger, level="INFO") as logs:
            self._cadastrar(client)
        self.assertIn("suprimido: transporte de e-mail desativado", "\n".join(logs.output))
        envio = self._ultimo("boas_vindas")
        self.assertEqual((envio.estado, envio.categoria), (EstadoEnvio.DESATIVADO, "transporte_desativado"))
        self.assertFalse(envio)
        mensagens = self._flashes(client)
        self.assertIn("O e-mail de ativação não foi enviado: o envio de e-mails está indisponível no momento",
                      mensagens)
        self.assertIn("reenvie o link", mensagens)
        self.assertNotIn("aceito", mensagens)
        self._assert_mensagem_de_operacao(mensagens)

    def test_t05_variaveis_vazias_ou_com_espacos_valem_como_ausentes(self):
        codigo = (
            "from config import Config\n"
            "assert Config.BREVO_API_KEY is None, 'chave so com espacos deveria valer como ausente'\n"
            "assert Config.MAIL_ATIVO is False\n"
            "assert Config.MAIL_DEFAULT_SENDER, 'remetente vazio deveria cair no padrao'\n"
            "assert Config.BREVO_API_URL == 'https://api.brevo.com/v3/smtp/email'\n"
            "assert Config.EMAIL_TIMEOUT_SEGUNDOS == 15.0\n"
        )
        self._executar(codigo, BREVO_API_KEY="   \n", MAIL_DEFAULT_SENDER="", BREVO_API_URL=" ",
                       MAIL_USERNAME="", MAIL_PASSWORD="", NEXO_EMAIL_TIMEOUT="abc")
        self._executar(
            "from config import Config\n"
            "assert Config.BREVO_API_KEY == 'xkeysib-synthetic', repr(Config.BREVO_API_KEY)\n"
            "assert Config.MAIL_ATIVO is True\n"
            "assert Config.EMAIL_TIMEOUT_SEGUNDOS == 60.0\n"
            "assert Config.MAIL_USE_TLS is False and Config.MAIL_USE_SSL is True\n"
            "assert Config.MAIL_SERVER == 'smtp.gmail.com' and Config.MAIL_PORT == 587\n",
            BREVO_API_KEY=" xkeysib-synthetic \n", NEXO_EMAIL_TIMEOUT="500",
            MAIL_USE_TLS="0", MAIL_USE_SSL="yes", MAIL_SERVER=" ", MAIL_PORT="",
        )

    def _executar(self, codigo, **ambiente):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BREVO_", "MAIL_", "NEXO_EMAIL"))}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHON_DOTENV_DISABLED="1")
        env.update(ambiente)
        preludio = "import dotenv\ndotenv.load_dotenv = lambda *a, **kw: False\n"
        resultado = subprocess.run([sys.executable, "-B", "-c", preludio + codigo], cwd=ROOT, env=env,
                                   capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        return resultado

    def test_recuperacao_com_envio_desativado_nao_grava_nada_na_requisicao(self):
        self.app.config.update(MAIL_ATIVO=False, EMAIL_ALERTA_ADMIN=True)
        for endereco in ("active@example.invalid", "pending@example.invalid", "ninguem@example.invalid"):
            self._request_link(endereco, self.app.test_client())
        with self.app.app_context():
            self.assertEqual(db.session.scalar(select(func.count()).select_from(Notificacao)), 0)

    def test_login_e_recuperacao_fazem_o_mesmo_numero_de_consultas_com_ou_sem_conta(self):
        from sqlalchemy import event
        contagem = []

        def contar(conexao, cursor, sql, parametros, contexto, varios):
            if "FROM usuario" in sql and sql.lstrip().upper().startswith("SELECT"):
                contagem[-1] += 1

        with self.app.app_context():
            motor = db.engine
        event.listen(motor, "before_cursor_execute", contar)
        self.addCleanup(event.remove, motor, "before_cursor_execute", contar)
        for endereco in ("active@example.invalid", "ninguem@example.invalid"):
            client = self.app.test_client()
            csrf = self._csrf(client)
            contagem.append(0)
            client.post("/auth/login", data={"email": endereco, "senha": "senha-errada-sintetica", "csrf_token": csrf})
        self.assertEqual(contagem[0], contagem[1], contagem)

    def test_recuperacao_nunca_devolve_erro_que_revele_a_conta(self):
        with patch.object(auth, "gerar_token_recuperacao_senha", side_effect=RuntimeError("PRIVATE_SENTINEL")), \
                self.assertLogs(auth.logger, level="WARNING") as logs:
            pagina = self._request_link("active@example.invalid")
        self.assertIn("Se o e-mail estiver cadastrado", pagina.get_data(as_text=True))
        self.assertIn("não processada (RuntimeError)", "\n".join(logs.output))
        self.assertNotIn("PRIVATE_SENTINEL", "\n".join(logs.output))

    def test_conta_pendente_recebe_nova_ativacao_e_inelegivel_nao_recebe_nada(self):
        self._request_link("pending@example.invalid")
        self._request_link("inactive@example.invalid")
        self._entregar()
        self.assertEqual([m.recipients for m in self.sent], [["pending@example.invalid"]])
        self._link(self.sent[0].html, "/auth/primeiro-acesso/")


class FalhasDoProvedorTests(_EmailCase):
    def setUp(self):
        super().setUp()
        self.app.config["BREVO_API_KEY"] = CHAVE_API

    def _recuperar(self, efeito):
        with patch.object(emails.urllib.request, "urlopen", side_effect=efeito) as http, \
                self.assertLogs(level="INFO") as logs:
            self._request_link("active@example.invalid")
            self._entregar()
        return http, "\n".join(logs.output), self._ultimo("recuperacao_senha")

    def test_t06_chave_invalida_e_identificada_sem_revelar_a_credencial(self):
        http, saida, envio = self._recuperar([_erro_http(401, {
            "code": "unauthorized", "message": f"Key not found PRIVATE_PROVIDER_SENTINEL {CHAVE_API}"})])
        http.assert_called_once()  # credencial recusada não é repetida
        self.assertEqual((envio.estado, envio.categoria, envio.http_status, envio.codigo_provedor),
                         (EstadoEnvio.FALHA_NO_ENVIO, "credencial_rejeitada", 401, "unauthorized"))
        self.assertIn("xkeysib-", envio.descricao_categoria)
        self.assertIn("categoria=credencial_rejeitada http=401 codigo=unauthorized erro=RuntimeError", saida)
        self.assertNotIn("Key not found", saida)
        self._assert_sem_segredos(saida, "active@example.invalid", "/auth/resetar-senha/")
        # No cadastro, o admin recebe a orientação na hora.
        client = self._admin()
        with patch.object(emails.urllib.request, "urlopen", side_effect=[_erro_http(401, {"code": "unauthorized"})]):
            self._cadastrar(client)
        mensagens = self._flashes(client)
        self.assertIn("O e-mail de ativação não foi enviado: o serviço de e-mail precisa de ajuste de configuração",
                      mensagens)
        self._assert_mensagem_de_operacao(mensagens)
        self._assert_sem_segredos(mensagens)
        envio = self._ultimo("boas_vindas")
        self.assertIn(f"Ref. {envio.correlacao}.", mensagens)
        diagnostico = client.get("/admin/diagnostico-email").get_data(as_text=True)
        self.assertIn(envio.correlacao, diagnostico)
        self.assertIn("o Brevo recusou a chave de API", diagnostico)

    def test_t06_ip_nao_autorizado_no_brevo_tem_categoria_propria(self):
        mensagem = ("We have detected you are using an unrecognised IP address 203.0.113.77. If you performed "
                    "this action make sure to add the new IP address in this link: "
                    "https://app.brevo.com/security/authorised_ips")
        _http, saida, envio = self._recuperar([_erro_http(401, {"code": "unauthorized", "message": mensagem})])
        self.assertEqual(envio.categoria, "ip_nao_autorizado")
        self.assertIn("IPs autorizados", envio.descricao_categoria)
        self.assertNotIn("203.0.113.77", saida)
        self.assertNotIn("unrecognised", saida)

    def test_smtp_com_ssl_e_tls_ao_mesmo_tempo_vale_o_ssl(self):
        config = dict(self.app.config, BREVO_API_KEY=None, MAIL_USE_SSL=True, MAIL_USE_TLS=True, MAIL_PORT=465)
        diagnostico = emails.diagnostico_configuracao(config)
        self.assertFalse(diagnostico["bloqueante"])
        self.assertTrue(any("vale o SSL direto" in p["texto"] and not p["bloqueante"]
                            for p in diagnostico["pendencias"]))

    def test_t06_chave_smtp_no_lugar_da_chave_de_api_e_apontada_no_diagnostico(self):
        self.app.config["BREVO_API_KEY"] = "xsmtpsib-" + "synthetic-smtp-key"
        diagnostico = emails.diagnostico_configuracao(self.app.config)
        self.assertEqual(diagnostico["chave_formato"], "smtp")
        self.assertTrue(diagnostico["bloqueante"])
        self.assertTrue(any("chave SMTP" in p["texto"] for p in diagnostico["pendencias"]))
        self.assertNotIn("synthetic-smtp-key", json.dumps(diagnostico, default=str))

    def test_t07_falha_de_conexao_e_controlada_e_nao_corrompe_dados(self):
        recusas = [URLError(ConnectionRefusedError(111, "PRIVATE_NET_SENTINEL")) for _ in range(2)]
        http, saida, envio = self._recuperar(recusas)
        self.assertEqual(http.call_count, 2)          # uma nova tentativa: o pedido nem saiu
        self.pausas.assert_called_once_with(1.5)
        self.assertEqual((envio.estado, envio.categoria, envio.tentativas),
                         (EstadoEnvio.FALHA_NO_ENVIO, "rede", 2))
        self.assertIn("erro=URLError", saida)
        self.assertEqual(self._account(), (FIXTURE_HASH, False, True))  # credencial intacta
        # Cadastro com a rede fora: empresa e usuário confirmados, sem duplicação.
        client = self._admin()
        with patch.object(emails.urllib.request, "urlopen",
                          side_effect=[URLError(OSError("PRIVATE")), URLError(OSError("PRIVATE"))]):
            resposta = self._cadastrar(client)
        self.assertEqual(resposta.status_code, 302)
        mensagens = self._flashes(client)
        self.assertIn("o serviço de e-mail está temporariamente indisponível", mensagens)
        self._assert_mensagem_de_operacao(mensagens)
        self.assertEqual(len(self._usuario_por_email("novo@example.invalid")), 1)
        # Com o provedor de volta, a própria recuperação reemite a ativação.
        with patch.object(emails.urllib.request, "urlopen", return_value=_RespostaBrevo()):
            self._request_link("novo@example.invalid")
            self._entregar()
        self.assertEqual(self._ultimo("boas_vindas").estado, EstadoEnvio.ACEITO_PELO_PROVEDOR)

    def test_t07_prazo_esgotado_na_resposta_nao_e_repetido_nem_dado_como_aceito(self):
        http, saida, envio = self._recuperar([TimeoutError("PRIVATE")])
        http.assert_called_once()  # o pedido pode ter chegado: repetir duplicaria
        self.assertEqual((envio.estado, envio.categoria), (EstadoEnvio.FALHA_NO_ENVIO, "tempo_esgotado"))
        self.assertIn("confira os registros do provedor", envio.descricao_categoria)

    def test_t07_prazo_esgotado_na_conexao_tem_uma_nova_tentativa_e_e_falha_certa(self):
        # O urllib embrulha em URLError o que falha antes de o pedido sair
        # (conexão, TLS, envio do corpo); o prazo na leitura da resposta vem cru.
        http, _saida, envio = self._recuperar([URLError(TimeoutError("PRIVATE")) for _ in range(2)])
        self.assertEqual(http.call_count, 2)
        self.pausas.assert_called_once_with(1.5)
        self.assertEqual((envio.estado, envio.categoria, envio.tentativas),
                         (EstadoEnvio.FALHA_NO_ENVIO, "conexao_sem_resposta", 2))
        self.assertNotIn(envio.categoria, emails.CATEGORIAS_RESULTADO_INCERTO)
        self.assertIn("não chegou a ser enviado", envio.descricao_categoria)
        _http, _saida, envio = self._recuperar([URLError(TimeoutError("PRIVATE")), _RespostaBrevo()])
        self.assertEqual((envio.estado, envio.tentativas), (EstadoEnvio.ACEITO_PELO_PROVEDOR, 2))

    def test_5xx_que_nao_garante_pedido_nao_processado_nao_e_repetido(self):
        # Só 429, 500 e 503 são repetidos; outro 5xx não prova que nada saiu.
        for status in (501, 507):
            with self.subTest(status=status):
                http, _saida, envio = self._recuperar([_erro_http(status), _RespostaBrevo()])
                http.assert_called_once()
                self.assertEqual((envio.estado, envio.categoria, envio.http_status, envio.tentativas),
                                 (EstadoEnvio.FALHA_NO_ENVIO, "provedor_indisponivel", status, 1))
        self.pausas.assert_not_called()

    def test_t07_indisponibilidade_temporaria_tem_uma_nova_tentativa(self):
        http, _saida, envio = self._recuperar([
            _erro_http(429, {"code": "too_many_requests"}, {"Retry-After": "3"}), _RespostaBrevo()])
        self.assertEqual(http.call_count, 2)
        self.pausas.assert_called_once_with(3.0)
        self.assertEqual((envio.estado, envio.tentativas), (EstadoEnvio.ACEITO_PELO_PROVEDOR, 2))
        _http, _saida, envio = self._recuperar([_erro_http(503), _erro_http(503)])
        self.assertEqual((envio.estado, envio.categoria, envio.http_status),
                         (EstadoEnvio.FALHA_NO_ENVIO, "provedor_indisponivel", 503))

    def test_redirecionamento_nao_leva_a_chave_nem_vira_aceite(self):
        class Redirecionada(_RespostaBrevo):
            def geturl(self):
                return "https://outro-host.example.invalid/pagina"

        http, _saida, envio = self._recuperar([Redirecionada(corpo=b"<html>", status=200)])
        requisicao = http.call_args.args[0]
        self.assertIn("Api-key", requisicao.unredirected_hdrs)   # o urllib não repassa num redirect
        self.assertNotIn("Api-key", requisicao.headers)
        self.assertEqual((envio.estado, envio.categoria), (EstadoEnvio.FALHA_NO_ENVIO, "configuracao_invalida"))
        _http, _saida, envio = self._recuperar([_RespostaBrevo(status=200)])  # 200 não é o aceite do envio
        self.assertEqual(envio.categoria, "configuracao_invalida")

    def test_gateway_ou_conexao_caida_apos_envio_nao_repetem_e_avisam_resultado_incerto(self):
        for efeito, categoria in ((_erro_http(502), "gateway_sem_resposta"), (_erro_http(504), "gateway_sem_resposta"),
                                  (ConnectionResetError("PRIVATE"), "conexao_interrompida")):
            with self.subTest(categoria=categoria, efeito=type(efeito).__name__):
                http, _saida, envio = self._recuperar([efeito])
                http.assert_called_once()  # o pedido pode ter sido processado: não repetir
                self.assertEqual(envio.categoria, categoria)
                self.assertIn(envio.categoria, emails.CATEGORIAS_RESULTADO_INCERTO)
        client = self._admin()
        with patch.object(emails.urllib.request, "urlopen", side_effect=[_erro_http(504)]):
            self._cadastrar(client)
        mensagens = self._flashes(client)
        self.assertIn("O e-mail de ativação pode não ter sido enviado", mensagens)
        self.assertNotIn("não foi enviado", mensagens)

    def test_retry_after_longo_nao_e_atropelado(self):
        http, _saida, envio = self._recuperar([_erro_http(429, {"code": "too_many_requests"}, {"Retry-After": "60"})])
        http.assert_called_once()
        self.pausas.assert_not_called()
        self.assertEqual(envio.categoria, "limite_excedido")

    def test_aviso_que_nao_foi_gravado_nao_consome_a_janela(self):
        self.app.config["EMAIL_ALERTA_ADMIN"] = True
        erro401 = lambda: _erro_http(401, {"code": "unauthorized"})  # noqa: E731
        with patch.object(emails.urllib.request, "urlopen", side_effect=[erro401(), erro401()]):
            self._request_link("active@example.invalid")
            self._entregar()               # envio falha e agenda o aviso
            with patch("notifications.notificar_admins", side_effect=SQLAlchemyError("PRIVATE")):
                self._entregar()           # o aviso não é gravado
            self.assertEqual(emails._ULTIMO_ALERTA, {})
            self._request_link("active@example.invalid")
            self._entregar()
            self._entregar()
        with self.app.app_context():
            self.assertEqual(db.session.scalar(select(func.count()).select_from(Notificacao).where(
                Notificacao.id_usuario == self.ids["admin"])), 1)

    def test_t08_remetente_recusado_pelo_provedor_tem_diagnostico_e_orientacao(self):
        _http, saida, envio = self._recuperar([_erro_http(400, {
            "code": "invalid_parameter", "message": "Sender is not valid PRIVATE_SENDER_SENTINEL"})])
        self.assertEqual((envio.categoria, envio.http_status, envio.codigo_provedor),
                         ("remetente_nao_autorizado", 400, "invalid_parameter"))
        self.assertIn("valide o remetente", envio.descricao_categoria)
        self.assertNotIn("Sender is not valid", saida)

    def test_t08_remetente_mal_configurado_falha_antes_da_rede(self):
        self.app.extensions["mail"].default_sender = "NEXO sem endereco"
        with patch.object(emails.urllib.request, "urlopen") as http:
            self._request_link("active@example.invalid")
            self._entregar()
        http.assert_not_called()
        envio = self._ultimo("recuperacao_senha")
        self.assertEqual((envio.estado, envio.categoria), (EstadoEnvio.FALHA_NO_ENVIO, "remetente_invalido"))
        config = dict(self.app.config, MAIL_DEFAULT_SENDER="NEXO")
        self.assertTrue(emails.diagnostico_configuracao(config)["bloqueante"])
        config["MAIL_DEFAULT_SENDER"] = "NEXO <nexo.teste@gmail.com>"
        diagnostico = emails.diagnostico_configuracao(config)
        self.assertFalse(diagnostico["bloqueante"])
        self.assertTrue(diagnostico["remetente_dominio_gratuito"])
        self.assertTrue(any("domínio gratuito" in p["texto"] for p in diagnostico["pendencias"]))

    def test_t14_url_sem_https_nunca_recebe_a_chave(self):
        self.app.config["BREVO_API_URL"] = "http://api.brevo-test.example.invalid/v3/smtp/email"
        self.assertTrue(emails.diagnostico_configuracao(self.app.config)["bloqueante"])
        client = self._admin()
        with patch.object(emails.urllib.request, "urlopen") as http:
            # Recuperação: a configuração bloqueia qualquer envio, então nada é
            # prometido nem tentado (igual para qualquer endereço).
            pagina = self._request_link("active@example.invalid").get_data(as_text=True)
            self._entregar()
            self.assertIn("No momento não é possível enviar o link", pagina)
            self.assertEqual(self._ultimo("recuperacao_senha").categoria, "envio_bloqueado")
            # Cadastro (ação do admin): a tentativa falha antes da rede.
            self._cadastrar(client)
        http.assert_not_called()
        self.assertEqual(self._ultimo("boas_vindas").categoria, "configuracao_invalida")

    def test_t15_logs_e_aviso_ao_admin_sem_token_senha_ou_segredo(self):
        self.app.config.update(EMAIL_ALERTA_ADMIN=True, MAIL_PASSWORD="PRIVATE_SMTP_PASSWORD")
        corpo = {"code": "unauthorized", "message": f"Key not found {CHAVE_API} active@example.invalid"}
        with patch.object(emails.urllib.request, "urlopen", side_effect=[_erro_http(401, corpo), _erro_http(401, corpo)]), \
                self.assertLogs(level="DEBUG") as logs:
            self._request_link("active@example.invalid")
            self._entregar()
            self._request_link("active@example.invalid")
            self._entregar()
            # SMTP com senha recusada e template quebrado também não vazam nada.
            self.app.config["BREVO_API_KEY"] = None
            self.transport.side_effect = smtplib.SMTPAuthenticationError(535, b"PRIVATE_SMTP_PASSWORD rejected")
            self._request_link("active@example.invalid")
            self._entregar()
            with self.app.test_request_context(), \
                    patch.object(emails, "render_template", side_effect=ValueError("PRIVATE_TEMPLATE")):
                emails.email_recuperacao_senha(SimpleNamespace(email="active@example.invalid", id_usuario=1),
                                               "https://app.example.invalid/auth/resetar-senha/PRIVATE-TOKEN")
            self._entregar()  # avisos aos admins: gravados em thread própria
        saida = "\n".join(logs.output)
        self._assert_sem_segredos(saida, "active@example.invalid", "/auth/resetar-senha/", "Key not found",
                                  "rejected")
        self.assertIn("categoria=smtp_autenticacao", saida)
        self.assertIn("categoria=template", saida)
        with self.app.app_context():
            avisos = db.session.scalars(select(Notificacao).where(
                Notificacao.id_usuario == self.ids["admin"])).all()
            textos = [(a.texto, a.link_destino) for a in avisos]
        # Um aviso por categoria (duas falhas iguais de credencial = um aviso).
        self.assertEqual(len([t for t in textos if "recusou a chave de API" in t[0]]), 1, textos)
        self.assertTrue(any("servidor SMTP recusou as credenciais" in t[0] for t in textos), textos)
        for texto, link in textos:
            self.assertTrue(texto.startswith("Falha no envio de e-mail (recuperação de senha):"), texto)
            self.assertRegex(texto, _REF)
            self.assertEqual(link, "/admin/diagnostico-email")
            self._assert_sem_segredos(texto, "active@example.invalid", "/auth/resetar-senha/")
            # Selo "Aberto" (pendência), nunca "Resolvido"/"Nova Análise".
            from notifications import classificar_notificacao
            self.assertEqual(classificar_notificacao(texto)["tipo"], "aberto")


class TokensDoEmailTests(_EmailCase):
    def _link_recuperacao(self):
        self._request_link("active@example.invalid")
        self._entregar()
        return self._link(self.sent[-1].html, "/auth/resetar-senha/")

    def test_t09_token_expirado_e_negado_com_orientacao_para_pedir_outro(self):
        link = self._link_recuperacao()
        agora = int(time.time())
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=agora + 901):
            resposta = self.client.get(urlsplit(link).path, follow_redirects=True)
            self._assert_rejected(self._post(self._token(link)))
        texto = resposta.get_data(as_text=True)
        self.assertIn("Token inválido ou expirado. Solicite um novo link de redefinição.", texto)
        self.assertEqual(self._account()[0], FIXTURE_HASH)

    def test_t10_token_ja_usado_e_rejeitado_e_o_titular_recebe_aviso_sem_token(self):
        link = self._link_recuperacao()
        self.sent.clear()
        self._assert_success(self._post(self._token(link)))
        self._entregar()
        self.assertEqual(len(self.sent), 1)
        aviso = self.sent[0]
        self.assertEqual(aviso.recipients, ["active@example.invalid"])
        self.assertIn("foi alterada", aviso.subject)
        self.assertNotIn(self._token(link), aviso.html)
        self.assertEqual(self._ultimo("senha_alterada").estado, EstadoEnvio.ACEITO_PELO_PROVEDOR)
        self._assert_rejected(self._post(self._token(link)))
        with self.app.app_context():
            self.assertTrue(check_password_hash(db.session.get(Usuario, self.ids["active"]).senha_hash, NEW_PASSWORD))

    def _formulario_https(self, client, caminho):
        pagina = client.get(caminho, base_url="https://localhost")
        self.assertEqual(pagina.status_code, 200)
        texto = pagina.get_data(as_text=True)
        self.assertIn('minlength="12"', texto)          # o mesmo mínimo da política de senha
        self.assertIn("Mínimo de 12 caracteres", texto)
        self.assertEqual(pagina.headers["Referrer-Policy"], "strict-origin")
        csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', texto).group(1)
        return {"nova_senha": NEW_PASSWORD, "confirma_senha": NEW_PASSWORD, "csrf_token": csrf}

    def test_links_do_email_funcionam_sob_https_com_o_referer_que_o_navegador_envia(self):
        """Produção (HTTPS atrás do ProxyFix): o CSRF do Flask-WTF exige Referer de mesma origem.

        Com "no-referrer" o navegador não enviava Referer e o POST da nova senha
        era recusado (400). Com "strict-origin" ele envia só a origem — nunca o
        caminho com o token — e o POST é aceito.
        """
        link = self._link_recuperacao()
        self._request_link("pending@example.invalid")
        self._entregar()
        ativacao = self._link(self.sent[-1].html, "/auth/primeiro-acesso/")
        for link_email, usuario in ((link, "active"), (ativacao, "pending")):
            with self.subTest(link=urlsplit(link_email).path.split("/")[2]):
                client = self.app.test_client()
                caminho = urlsplit(link_email).path
                dados = self._formulario_https(client, caminho)
                sem_referer = client.post(caminho, data=dados, base_url="https://localhost")
                self.assertEqual(sem_referer.status_code, 400)
                self.assertEqual(self._account(usuario)[0], FIXTURE_HASH)
                com_origem = client.post(caminho, data=dados, base_url="https://localhost",
                                         headers={"Referer": "https://localhost/"})
                self._assert_success(com_origem)
                with self.app.app_context():
                    self.assertTrue(check_password_hash(db.session.get(Usuario, self.ids[usuario]).senha_hash,
                                                        NEW_PASSWORD))

    def test_checagem_de_senha_vazada_indisponivel_nao_derruba_a_troca(self):
        import http.client
        import politica_senha
        with patch.dict(os.environ, {"NEXO_SENHAS_VAZADAS": "1"}), \
                patch.object(politica_senha, "urlopen", side_effect=http.client.IncompleteRead(b"")), \
                patch.object(politica_senha.logger, "disabled", False), \
                self.assertLogs(politica_senha.logger, level="WARNING") as logs:
            self.assertIsNone(politica_senha.senha_vazada(NEW_PASSWORD))
        self.assertIn("IncompleteRead", "\n".join(logs.output))

    def test_falha_no_aviso_de_senha_alterada_nao_desfaz_a_troca(self):
        link = self._link_recuperacao()
        with patch.object(auth, "email_senha_alterada", side_effect=RuntimeError("PRIVATE")):
            self._assert_success(self._post(self._token(link)))
        with self.app.app_context():
            self.assertTrue(check_password_hash(db.session.get(Usuario, self.ids["active"]).senha_hash, NEW_PASSWORD))


class MaiusculasMinusculasTests(_EmailCase):
    def test_t11_conta_legada_com_maiusculas_e_encontrada_no_login_e_na_recuperacao(self):
        self._criar_usuario("Legacy.Mixed@Example.Invalid")
        for digitado in ("legacy.mixed@example.invalid", "LEGACY.MIXED@EXAMPLE.INVALID", " Legacy.Mixed@Example.Invalid "):
            with self.subTest(digitado=digitado):
                self._request_link(digitado, self.app.test_client())
        self._entregar()
        # O e-mail vai para o endereço como está gravado; nada foi alterado no banco.
        self.assertEqual([m.recipients for m in self.sent], [["Legacy.Mixed@Example.Invalid"]] * 3)
        client = self.app.test_client()
        login = client.post("/auth/login", data={"email": "legacy.mixed@example.invalid", "senha": OLD_PASSWORD,
                                                 "csrf_token": self._csrf(client)})
        self.assertEqual(login.status_code, 302)
        self.assertTrue(login.headers["Location"].endswith("/cliente/dashboard"))
        # O cadastro não cria outra conta com o mesmo e-mail em outra caixa.
        admin_client = self._admin()
        self._cadastrar(admin_client, usuario_email="LEGACY.mixed@example.invalid")
        self.assertIn("Já existe um usuário com este e-mail", self._flashes(admin_client))
        self.assertEqual(len(self._usuario_por_email("legacy.mixed@example.invalid")), 1)

    def test_t11_cadastro_normaliza_e_impede_duplicata_por_caixa(self):
        client = self._admin()
        self._cadastrar(client, usuario_email="Brand.New@Example.Invalid")
        self._cadastrar(client, cnpj="88888888888888", usuario_email="brand.new@example.invalid")
        self.assertIn("Já existe um usuário com este e-mail", self._flashes(client))
        contas = self._usuario_por_email("brand.new@example.invalid")
        self.assertEqual([u.email for u in contas], ["brand.new@example.invalid"])
        with self.app.app_context():
            self.assertIsNone(db.session.scalar(select(Empresa).where(Empresa.cnpj == "88888888888888")))

    def test_t11_registros_conflitantes_nao_escolhem_conta_e_avisam_no_log(self):
        self._criar_usuario("dup@example.invalid")
        self._criar_usuario("DUP@example.invalid")
        with self.assertLogs(auth.logger, level="WARNING") as logs:
            pagina = self._request_link("Dup@Example.Invalid")
        self.assertIn("Se o e-mail estiver cadastrado", pagina.get_data(as_text=True))
        self.assertEqual(self.jobs, [])
        self.assertIn("mais de um cadastro tem este e-mail", "\n".join(logs.output))
        self.assertNotIn("dup@example.invalid", "\n".join(logs.output).lower())
        self._request_link("DUP@example.invalid")  # texto exato continua valendo
        self._entregar()
        self.assertEqual(self.sent[0].recipients, ["DUP@example.invalid"])

    def test_t11_destinatarios_da_empresa_nao_se_repetem_por_caixa(self):
        empresa = SimpleNamespace(deletado_em=None, status_conta="ATIVA", email_contato="Contato@Example.Invalid",
                                  usuarios=[SimpleNamespace(role="CLIENTE", ativo=True, email="contato@example.invalid"),
                                            SimpleNamespace(role="CLIENTE", ativo=True, email="Outro@Example.Invalid"),
                                            SimpleNamespace(role="CLIENTE", ativo=False, email="inativo@example.invalid")])
        self.assertEqual(emails._emails_da_empresa(empresa), ["Contato@Example.Invalid", "Outro@Example.Invalid"])
        self.assertEqual(emails._destinatarios_validos(["a@example.invalid", "A@EXAMPLE.INVALID", "invalido", ""]),
                         ["a@example.invalid"])
        # Contato da empresa que é o e-mail de um usuário desativado não recebe.
        empresa.email_contato = "INATIVO@example.invalid"
        self.assertEqual(emails._emails_da_empresa(empresa), ["contato@example.invalid", "Outro@Example.Invalid"])

    def test_chave_do_limite_por_conta_nao_e_hash_reversivel_do_email(self):
        import hashlib
        with self.app.test_request_context("/auth/recuperar-senha", method="POST",
                                           data={"email": " Active@Example.Invalid "}):
            chave = extensions.chave_limite_conta()
        with self.app.test_request_context("/auth/login", method="POST", data={"email": "active@example.invalid"}):
            self.assertEqual(extensions.chave_limite_conta(), chave)  # mesma conta, mesmo balde
        self.assertTrue(chave.startswith("conta:"))
        sem_segredo = hashlib.sha256(b"active@example.invalid").hexdigest()[:24]
        self.assertNotIn(sem_segredo, chave)


class TelasDoAdministradorTests(_EmailCase):
    def test_diagnostico_somente_para_admin_e_sem_segredos(self):
        self.app.config["BREVO_API_KEY"] = CHAVE_API
        anonimo = self.app.test_client().get("/admin/diagnostico-email")
        self.assertEqual(anonimo.status_code, 302)
        cliente = self.app.test_client()
        cliente.post("/auth/login", data={"email": "active@example.invalid", "senha": OLD_PASSWORD,
                                          "csrf_token": self._csrf(cliente)})
        self.assertEqual(cliente.get("/admin/diagnostico-email").status_code, 403)
        pagina = self._admin().get("/admin/diagnostico-email")
        self.assertEqual(pagina.status_code, 200)
        texto = pagina.get_data(as_text=True)
        self.assertIn("API HTTPS do Brevo (porta 443)", texto)
        self.assertIn("formato de chave de API (xkeysib-)", texto)
        self.assertIn("remetente-teste@", texto)
        self.assertIn("Enviar e-mail de teste para mim", texto)
        self.assertNotIn(CHAVE_API, texto)
        self.assertNotIn("synthetic-test-key", texto)

    def test_envio_de_teste_vai_somente_para_o_proprio_admin(self):
        client = self._admin()
        resposta = client.post("/admin/diagnostico-email/teste", data={
            "csrf_token": self._csrf(client, "/admin/diagnostico-email"),
            "destinatario": "intruso@example.invalid", "email": "intruso@example.invalid",
        })
        self.assertTrue(resposta.headers["Location"].endswith("/admin/diagnostico-email"))
        self.assertEqual([m.recipients for m in self.sent], [["admin@example.invalid"]])
        self.assertNotIn("/auth/", self.sent[0].html)  # sem link de acesso nem token
        self.assertIn("aceito pelo serviço de e-mail (SMTP (Flask-Mail))", self._flashes(client))
        anonimo = self.app.test_client()
        sem_login = anonimo.post("/admin/diagnostico-email/teste", data={"csrf_token": self._csrf(anonimo)})
        self.assertEqual(sem_login.status_code, 302)
        self.assertIn("/auth/login", sem_login.headers["Location"])
        self.assertEqual(client.post("/admin/diagnostico-email/teste").status_code, 400)  # sem CSRF
        self.assertEqual(len(self.sent), 1)
        pagina = client.get("/admin/diagnostico-email").get_data(as_text=True)
        self.assertIn("teste de configuração", pagina)
        self.assertIn("Aceito pelo provedor", pagina)

    def test_reenvio_de_ativacao_pelo_detalhe_da_empresa(self):
        client = self._admin()
        detalhe = client.get(f"/admin/empresas/{self.empresa_id}/detalhes").get_data(as_text=True)
        self.assertIn("Acesso ao portal", detalhe)
        self.assertIn("AGUARDANDO ATIVAÇÃO", detalhe)
        self.assertIn(f"/admin/usuarios/{self.ids['pending']}/reenviar-ativacao", detalhe)
        self.assertNotIn(f"/admin/usuarios/{self.ids['active']}/reenviar-ativacao", detalhe)
        csrf = self._csrf(client, f"/admin/empresas/{self.empresa_id}/detalhes")
        resposta = client.post(f"/admin/usuarios/{self.ids['pending']}/reenviar-ativacao", data={"csrf_token": csrf})
        self.assertTrue(resposta.headers["Location"].endswith(f"/admin/empresas/{self.empresa_id}/detalhes"))
        self.assertEqual([m.recipients for m in self.sent], [["pending@example.invalid"]])
        link = self._link(self.sent[0].html, "/auth/primeiro-acesso/")
        mensagens = self._flashes(client)
        self.assertIn("O e-mail de ativação foi aceito pelo serviço de e-mail.", mensagens)
        self._assert_mensagem_de_operacao(mensagens)
        self._assert_success(self._post(self._token(link), "activation", client=self.app.test_client()))
        # Conta já ativa, ADMIN ou empresa cancelada: nada é enviado.
        for alvo, esperado in ((self.ids["active"], "já ativou o acesso"), (self.ids["admin"], "Usuário não encontrado")):
            client.post(f"/admin/usuarios/{alvo}/reenviar-ativacao", data={"csrf_token": csrf})
            self.assertIn(esperado, self._flashes(client))
        pendente = self._criar_usuario("outro-pendente@example.invalid", pendente=True)
        with self.app.app_context():
            db.session.get(Empresa, self.empresa_id).status_conta = "CANCELADA"
            db.session.commit()
        client.post(f"/admin/usuarios/{pendente}/reenviar-ativacao", data={"csrf_token": csrf})
        self.assertIn("não está elegível", self._flashes(client))
        self.assertEqual(len(self.sent), 1)
        cliente = self.app.test_client()
        cliente.post("/auth/login", data={"email": "other@example.invalid", "senha": OLD_PASSWORD,
                                          "csrf_token": self._csrf(cliente)})
        self.assertEqual(cliente.post(f"/admin/usuarios/{pendente}/reenviar-ativacao",
                                      data={"csrf_token": self._csrf(cliente, "/auth/logout")}).status_code, 403)

    def test_dashboard_sem_faixa_tecnica_e_pendencia_no_diagnostico(self):
        client = self._admin()
        self.app.config["MAIL_ATIVO"] = False
        painel = client.get("/admin/dashboard").get_data(as_text=True)
        for tecnico in ("pendência de configuração", "BREVO_API_KEY", "envelope-exclamation"):
            self.assertNotIn(tecnico, painel)
        self.assertIn("Diagnóstico de e-mail", painel)  # o acesso ao mecanismo administrativo continua
        diagnostico = client.get("/admin/diagnostico-email").get_data(as_text=True)
        self.assertIn("Impede o envio", diagnostico)
        self.assertIn("DESATIVADO", diagnostico)


class LimitesDoAdministradorTests(_EmailCase):
    ENABLE_LIMITS = True

    def test_envio_de_teste_tem_limite_por_usuario(self):
        client = self._admin()
        csrf = self._csrf(client, "/admin/diagnostico-email")
        codigos = [client.post("/admin/diagnostico-email/teste", data={"csrf_token": csrf}).status_code
                   for _ in range(6)]
        self.assertEqual(codigos, [302] * 5 + [429])
        self.assertEqual(len(self.sent), 5)


class AgendamentoEEncerramentoTests(_EmailCase):
    THREADS_SIMULADAS = False

    def test_t17_falha_ao_iniciar_a_thread_nao_vira_agendamento_nem_aceite(self):
        class ThreadQuebrada:
            def __init__(self, target, args, daemon):
                pass

            def start(self):
                raise RuntimeError("PRIVATE_SCHEDULER_SENTINEL")

        self.app.config["EMAIL_ALERTA_ADMIN"] = True
        with patch.object(emails, "threading", _threading_com(ThreadQuebrada)), \
                self.assertLogs(emails.logger, level="INFO") as logs:
            self._request_link("active@example.invalid")
        envio = self._ultimo("recuperacao_senha")
        self.assertEqual((envio.estado, envio.categoria), (EstadoEnvio.FALHA_NO_ENVIO, "agendamento"))
        self.assertFalse(envio)
        saida = "\n".join(logs.output)
        self.assertNotIn("estado=AGENDADO", saida)
        self.assertNotIn("PRIVATE_SCHEDULER_SENTINEL", saida)
        self.assertEqual(emails._EM_ANDAMENTO, {})
        # A requisição pública não grava aviso no banco (sem oráculo de tempo).
        with self.app.app_context():
            self.assertEqual(db.session.scalar(select(func.count()).select_from(Notificacao).where(
                Notificacao.id_usuario == self.ids["admin"])), 0)

    def test_t17_envio_em_andamento_no_encerramento_e_aguardado_ou_registrado_como_interrompido(self):
        self.app.config["BREVO_API_KEY"] = CHAVE_API
        liberar, iniciou = threading.Event(), threading.Event()

        def brevo_lento(app, msg):
            iniciou.set()
            liberar.wait(5)
            return ID_MENSAGEM

        with patch.object(emails, "_enviar_brevo", side_effect=brevo_lento):
            self._request_link("active@example.invalid")
            envio = self._ultimo("recuperacao_senha")
            hilo = emails._EM_ANDAMENTO[envio.correlacao][0]
            self.assertTrue(iniciou.wait(5))
            with self.assertLogs(emails.logger, level="WARNING") as logs:
                emails._aguardar_envios_pendentes(prazo_maximo=0.2)
            # Agendado não é entregue: o desfecho segue desconhecido e explícito.
            self.assertEqual(envio.estado, EstadoEnvio.AGENDADO)
            self.assertFalse(envio.aceito)
            saida = "\n".join(logs.output)
            self.assertIn("interrompido no encerramento do processo", saida)
            self.assertIn(f"correlacao={envio.correlacao}", saida)
            liberar.set()
            hilo.join(5)
        self.assertEqual(envio.estado, EstadoEnvio.ACEITO_PELO_PROVEDOR)
        self.assertEqual(emails._EM_ANDAMENTO, {})

    def test_t17_encerramento_espera_o_envio_que_termina_no_prazo(self):
        self.app.config["BREVO_API_KEY"] = CHAVE_API

        def brevo_rapido(app, msg):
            time.sleep(0.2)
            return ID_MENSAGEM

        with patch.object(emails, "_enviar_brevo", side_effect=brevo_rapido):
            self._request_link("active@example.invalid")
            envio = self._ultimo("recuperacao_senha")
            with self.assertNoLogs(emails.logger, level="WARNING"):
                emails._aguardar_envios_pendentes(prazo_maximo=5)
        self.assertEqual(envio.estado, EstadoEnvio.ACEITO_PELO_PROVEDOR)


class PublicacaoEChamadoTests(DeployTestCase):
    """T12/T13: avisos só depois do COMMIT, com banco migrado de verdade."""

    def setUp(self):
        super().setUp()
        from models import Analise, ChamadoSuporte, IndicadorAnalise, MensagemSuporte
        self.fixtures()
        analise = self.db.session.get(Analise, self.analise_id)
        analise.status_analise = "EM_ANALISE"
        analise.indicador = IndicadorAnalise(faturamento_total=250, total_comprado=100,
                                             saldo_estimado_compras_vendas=150)
        chamado = ChamadoSuporte(id_empresa=analise.id_empresa, id_usuario_cliente=self.cliente_id,
                                 assunto="Chamado fictício", categoria="DUVIDA_TECNICA",
                                 status_chamado="ABERTO", prioridade="MEDIA")
        self.db.session.add(chamado)
        self.db.session.flush()
        self.db.session.add(MensagemSuporte(id_chamado=chamado.id_chamado, id_usuario_remetente=self.cliente_id,
                                            mensagem="Mensagem fictícia."))
        self.db.session.commit()
        self.chamado_id = chamado.id_chamado
        self.editor = f"/admin/analise/{self.analise_id}/relatorio"
        self.login("admin-test@example.invalid")
        # A migração (Alembic fileConfig) desativa os loggers já existentes.
        self.stack.enter_context(patch.object(emails.logger, "disabled", False))
        self.stack.enter_context(patch.object(self.app.logger, "disabled", False))
        for nome, valor in (("_HISTORICO", deque(maxlen=50)), ("_EM_ANDAMENTO", {}), ("_ULTIMO_ALERTA", {})):
            self.stack.enter_context(patch.object(emails, nome, valor))

    def _versao(self):
        from models import RelatorioAnalise
        self.db.session.expire_all()
        rel = self.db.session.scalar(select(RelatorioAnalise).where(RelatorioAnalise.id_analise == self.analise_id))
        return rel.versao_rascunho if rel else 0

    def _publicar(self):
        return self.client.post(self.editor, data={
            "csrf_token": self.csrf_token(self.editor), "acao": "publicar", "versao_rascunho": str(self._versao()),
            "titulo": "Titulo sintético", "resumo_executivo": "Resumo sintético",
            "conclusao_estrategica": "Conclusão sintética",
        })

    def _resolver(self):
        caminho = f"/admin/tickets/{self.chamado_id}"
        return self.client.post(caminho, data={"acao": "alterar_status", "status_chamado": "RESOLVIDO",
                                               "csrf_token": self.csrf_token(caminho)})

    def _confirmado(self, consulta):
        # Outra conexão: só enxerga o que já foi confirmado (COMMIT).
        with Session(bind=self.db.engine) as outra:
            return outra.scalar(consulta)

    def test_t12_email_da_publicacao_so_e_solicitado_apos_o_commit(self):
        from models import RelatorioAnalise
        vistos = []

        def registrar(analise, link_url):
            vistos.append((self._confirmado(select(RelatorioAnalise.publicado).where(
                RelatorioAnalise.id_analise == self.analise_id)), link_url))
            return True

        with patch.object(admin, "email_analise_publicada", side_effect=registrar), \
                patch.object(self.db.session, "commit", side_effect=SQLAlchemyError("PRIVATE_COMMIT_SENTINEL")):
            self._publicar()
        self.assertEqual(vistos, [])
        with patch.object(admin, "email_analise_publicada", side_effect=registrar):
            self.assertEqual(self._publicar().status_code, 302)
        self.assertEqual(vistos, [(True, f"https://nexo-test.example.invalid/cliente/analise/{self.analise_id}")])

    def test_t12_publicacao_com_transporte_real_simulado(self):
        enviados, jobs = [], []

        class FilaDeThreads:
            def __init__(fila, target, args, daemon):
                fila.target, fila.args = target, args

            def start(fila):
                jobs.append(fila)

        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None)
        with patch.object(emails, "threading", _threading_com(FilaDeThreads)), \
                patch.object(emails.mail, "send", side_effect=enviados.append):
            self.assertEqual(self._publicar().status_code, 302)
            for job in jobs:
                job.target(*job.args)
        self.assertEqual([m.recipients for m in enviados], [["empresa1@example.invalid", "cliente-test@example.invalid"]])
        self.assertIn(f"https://nexo-test.example.invalid/cliente/analise/{self.analise_id}", enviados[0].html)
        envio = emails.historico_envios()[0]
        self.assertEqual((envio.tipo, envio.estado), ("analise_publicada", EstadoEnvio.ACEITO_PELO_PROVEDOR))

    def test_t13_email_do_chamado_resolvido_so_e_solicitado_apos_o_commit(self):
        from models import ChamadoSuporte
        vistos = []

        def registrar(chamado, link_url):
            vistos.append(self._confirmado(select(ChamadoSuporte.status_chamado).where(
                ChamadoSuporte.id_chamado == self.chamado_id)))
            return True

        with patch.object(admin, "email_ticket_resolvido", side_effect=registrar), \
                patch.object(self.db.session, "commit", side_effect=SQLAlchemyError("PRIVATE_COMMIT_SENTINEL")):
            self._resolver()
        self.assertEqual(vistos, [])
        with patch.object(admin, "email_ticket_resolvido", side_effect=registrar):
            self.assertEqual(self._resolver().status_code, 302)
        self.assertEqual(vistos, ["RESOLVIDO"])

    def test_t13_falha_no_email_do_chamado_nao_desfaz_a_resolucao(self):
        from models import ChamadoSuporte
        with patch.object(admin, "email_ticket_resolvido", side_effect=RuntimeError("PRIVATE_SENTINEL")), \
                self.assertLogs(self.app.logger, level="WARNING") as logs:
            self.assertEqual(self._resolver().status_code, 302)
        self.assertEqual(self._confirmado(select(ChamadoSuporte.status_chamado).where(
            ChamadoSuporte.id_chamado == self.chamado_id)), "RESOLVIDO")
        self.assertNotIn("PRIVATE_SENTINEL", "\n".join(logs.output))

    def test_t13_salvar_de_novo_como_resolvido_nao_duplica_aviso_nem_email(self):
        from models import ChamadoSuporte, Notificacao as NotificacaoModelo
        with patch.object(admin, "email_ticket_resolvido", return_value=True) as aviso:
            self._resolver()
            fechamento = self._confirmado(select(ChamadoSuporte.data_fechamento).where(
                ChamadoSuporte.id_chamado == self.chamado_id))
            self._resolver()
        aviso.assert_called_once()
        self.assertEqual(self._confirmado(select(ChamadoSuporte.data_fechamento).where(
            ChamadoSuporte.id_chamado == self.chamado_id)), fechamento)
        self.assertEqual(self._confirmado(select(func.count()).select_from(NotificacaoModelo).where(
            NotificacaoModelo.id_usuario == self.cliente_id,
            NotificacaoModelo.texto.like("%marcado como resolvido%"))), 1)

    def test_t13_conta_sem_vinculo_contratual_nao_recebe_email_do_chamado(self):
        from datetime import datetime, timezone
        from models import ChamadoSuporte, RegimeContratual
        chamado = self.db.session.get(ChamadoSuporte, self.chamado_id)
        # Regime contratual ativo e nenhum contrato vigente: conta encerrada
        # (o sininho também não avisa — mesmo critério).
        self.db.session.add(RegimeContratual(id_empresa=chamado.id_empresa, ativado_em=datetime.now(timezone.utc),
                                             id_usuario_admin_ativacao=self.admin_id))
        self.db.session.commit()
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None)
        with patch.object(emails.mail, "send") as envio_smtp, self.app.test_request_context():
            resultado = emails.email_ticket_resolvido(self.db.session.get(ChamadoSuporte, self.chamado_id),
                                                      "https://nexo-test.example.invalid/x")
        envio_smtp.assert_not_called()
        self.assertEqual(resultado.estado, EstadoEnvio.SEM_DESTINATARIO)

    def test_t12_falha_ao_conferir_a_politica_depois_do_commit_nao_vira_erro(self):
        from models import RelatorioAnalise
        with patch.object(admin, "pode_consultar_analise", side_effect=SQLAlchemyError("PRIVATE_SENTINEL")), \
                patch.object(admin, "email_analise_publicada") as aviso:
            resposta = self._publicar()
        self.assertEqual(resposta.status_code, 302)
        aviso.assert_not_called()
        self.assertTrue(self._confirmado(select(RelatorioAnalise.publicado).where(
            RelatorioAnalise.id_analise == self.analise_id)))
        with self.client.session_transaction() as sessao:
            mensagens = " ".join(texto for _c, texto in sessao.get("_flashes", []))
        self.assertIn("não foi possível confirmar agora o acesso do cliente", mensagens)

    def test_t13_chamado_de_empresa_cancelada_nao_gera_email(self):
        from models import ChamadoSuporte, Empresa as EmpresaModelo
        chamado = self.db.session.get(ChamadoSuporte, self.chamado_id)
        self.db.session.get(EmpresaModelo, chamado.id_empresa).status_conta = "CANCELADA"
        self.db.session.commit()
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None)
        with patch.object(emails.mail, "send") as envio_smtp:
            with self.app.test_request_context():
                resultado = emails.email_ticket_resolvido(self.db.session.get(ChamadoSuporte, self.chamado_id),
                                                          "https://nexo-test.example.invalid/x")
        envio_smtp.assert_not_called()
        self.assertEqual(resultado.estado, EstadoEnvio.SEM_DESTINATARIO)


class RenderEConfiguracaoTests(unittest.TestCase):
    """T14: no Render Free o transporte recomendado é a API HTTPS (443)."""

    def _executar(self, codigo, **ambiente):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BREVO_", "MAIL_", "NEXO_", "RENDER"))}
        env.update(FLASK_ENV="production", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                   PYTHON_DOTENV_DISABLED="1", SECRET_KEY=secrets.token_hex(32),
                   DATABASE_URL="postgresql://localhost/nexo_test", DIRECT_URL="", NEXO_DB_DIRECT="0",
                   UPLOAD_FOLDER=str(ROOT / ".audit_tmp" / "email-config-uploads"))
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

    def test_t14_render_com_brevo_usa_https_e_links_publicos_https(self):
        resultado = self._executar(
            "from app import create_app\n"
            "from origem_publica import url_publica\n"
            "import emails\n"
            "app = create_app('production')\n"
            "with app.app_context():\n"
            "    link = url_publica('auth.resetar_senha', token='TOKEN')\n"
            "    assert link.startswith('https://nexo-teste.example.invalid/auth/resetar-senha/'), link\n"
            "    d = emails.diagnostico_configuracao()\n"
            "    assert d['transporte'] == 'brevo' and d['api_https'] and d['api_host'] == 'api.brevo.com', d\n"
            "    assert d['hospedagem_bloqueia_smtp'] is True and d['alerta_admin'] is True and not d['bloqueante'], d\n"
            "    assert app.config['BREVO_API_URL'] == 'https://api.brevo.com/v3/smtp/email'\n",
            RENDER="true", RENDER_EXTERNAL_URL="https://nexo-teste.example.invalid",
            BREVO_API_KEY="xkeysib-synthetic-PRIVATE-KEY", MAIL_DEFAULT_SENDER="NEXO <nexo@example.invalid>",
        )
        self.assertIn("Configuração de e-mail | transporte=brevo", resultado.stderr)
        self.assertNotIn("PRIVATE-KEY", resultado.stdout + resultado.stderr)

    def test_t14_smtp_no_render_ou_chave_smtp_aparecem_como_pendencia_bloqueante_no_log(self):
        resultado = self._executar(
            "from app import create_app\n"
            "app = create_app('production')\n",
            RENDER="true", RENDER_EXTERNAL_URL="https://nexo-teste.example.invalid",
            MAIL_USERNAME="smtp-user@example.invalid", MAIL_PASSWORD="PRIVATE_SMTP_PASSWORD",
        )
        self.assertIn("pendência (bloqueia o envio): O transporte ativo é SMTP", resultado.stderr)
        self.assertNotIn("PRIVATE_SMTP_PASSWORD", resultado.stderr)
        resultado = self._executar(
            "from app import create_app\n"
            "app = create_app('production')\n",
            RENDER="true", RENDER_EXTERNAL_URL="https://nexo-teste.example.invalid",
            BREVO_API_KEY="xsmtpsib-synthetic-PRIVATE-KEY",
        )
        self.assertIn("parece ser uma chave SMTP", resultado.stderr)
        self.assertNotIn("PRIVATE-KEY", resultado.stderr)
        resultado = self._executar(
            "from app import create_app\n"
            "app = create_app('production')\n",
            RENDER_EXTERNAL_URL="https://nexo-teste.example.invalid",
        )
        self.assertIn("pendência (bloqueia o envio): Envio de e-mail DESATIVADO", resultado.stderr)

    def test_t14_blueprint_do_render_declara_brevo_e_nao_smtp(self):
        import yaml
        servico = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))["services"][0]
        variaveis = {v["key"]: v for v in servico["envVars"]}
        self.assertEqual(variaveis["BREVO_API_KEY"].get("sync"), False)  # segredo preenchido no painel
        self.assertIn("MAIL_DEFAULT_SENDER", variaveis)
        for smtp in ("MAIL_SERVER", "MAIL_PORT", "MAIL_USERNAME", "MAIL_PASSWORD"):
            self.assertNotIn(smtp, variaveis)
        self.assertEqual(servico["plan"], "free")


class ComponentesTests(unittest.TestCase):
    """Peças isoladas: SMTP com prazo/TLS, texto simples e varredura de segredos."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for alvo in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            self.stack.enter_context(patch(alvo, side_effect=AssertionError("Rede proibida no teste")))
        self.app = Flask("app", root_path=str(ROOT))
        self.app.config.update(MAIL_SERVER="smtp.example.invalid", MAIL_PORT=587, MAIL_USE_TLS=True,
                               MAIL_USE_SSL=False, MAIL_USERNAME="smtp-user@example.invalid",
                               MAIL_PASSWORD="PRIVATE_SMTP_PASSWORD", MAIL_DEFAULT_SENDER="r@example.invalid",
                               EMAIL_TIMEOUT_SEGUNDOS=4.0, TESTING=False)
        extensions.mail.init_app(self.app)

    def _smtp_falso(self, falha_login=False, falha_tls=None, falha_envio=None):
        instancias = []

        class SMTPFalso:
            def __init__(falso, host, port, timeout=None, context=None, **kwargs):
                falso.host, falso.port, falso.timeout, falso.context = host, port, timeout, context
                falso.eventos = []
                instancias.append(falso)

            def set_debuglevel(falso, nivel):
                falso.eventos.append(("debug", nivel))

            def starttls(falso, context=None):
                falso.eventos.append(("starttls", context))
                if falha_tls is not None:
                    raise falha_tls

            def login(falso, usuario, senha):
                falso.eventos.append(("login", usuario))
                if falha_login:
                    raise smtplib.SMTPAuthenticationError(535, b"PRIVATE")

            def sendmail(falso, *args):
                falso.eventos.append(("sendmail",))
                if falha_envio is not None:
                    raise falha_envio

            def quit(falso):
                raise smtplib.SMTPServerDisconnected("PRIVATE")

            def close(falso):
                falso.eventos.append(("close",))

        return SMTPFalso, instancias

    def _mensagem(self):
        return Message(subject="Assunto", recipients=["d@example.invalid"], html="<p>x</p>", body="x")

    def test_smtp_abre_com_prazo_e_certificado_verificado(self):
        falso, instancias = self._smtp_falso()
        with self.app.app_context(), patch.object(extensions.smtplib, "SMTP", falso):
            extensions.mail.send(self._mensagem())  # QUIT com falha não vira erro do envio
        smtp = instancias[0]
        self.assertEqual((smtp.host, smtp.port, smtp.timeout), ("smtp.example.invalid", 587, 4.0))
        contexto = next(evento[1] for evento in smtp.eventos if evento[0] == "starttls")
        self.assertIsInstance(contexto, ssl.SSLContext)
        self.assertEqual(contexto.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(contexto.check_hostname)
        self.assertEqual([e[0] for e in smtp.eventos], ["debug", "starttls", "login", "sendmail", "close"])

    def test_smtp_ssl_com_prazo_e_falha_de_login_fecha_a_conexao(self):
        self.app.config.update(MAIL_USE_SSL=True, MAIL_USE_TLS=False, MAIL_PORT=465)
        extensions.mail.init_app(self.app)
        falso, instancias = self._smtp_falso(falha_login=True)
        with self.app.app_context(), patch.object(extensions.smtplib, "SMTP_SSL", falso):
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                extensions.mail.send(self._mensagem())
        smtp = instancias[0]
        self.assertEqual((smtp.port, smtp.timeout), (465, 4.0))
        self.assertIsInstance(smtp.context, ssl.SSLContext)
        self.assertEqual(smtp.eventos[-1], ("close",))

    def test_queda_na_transacao_smtp_e_resultado_incerto_e_antes_dela_e_falha_certa(self):
        casos = (
            ("envio", smtplib.SMTPServerDisconnected("PRIVATE"), "conexao_interrompida"),
            ("envio", TimeoutError("PRIVATE"), "conexao_interrompida"),
            ("envio", smtplib.SMTPRecipientsRefused({}), "smtp_destinatario_recusado"),
            ("tls", smtplib.SMTPServerDisconnected("PRIVATE"), "smtp_conexao"),
        )
        for fase, erro, categoria in casos:
            with self.subTest(fase=fase, erro=type(erro).__name__):
                falso, instancias = self._smtp_falso(**{f"falha_{fase}": erro})
                envio = emails.EnvioEmail("recuperacao_senha")
                with self.app.app_context(), patch.object(extensions.smtplib, "SMTP", falso), \
                        patch.object(emails, "_HISTORICO", deque(maxlen=50)), \
                        patch.object(emails, "_dormir") as pausa:
                    emails._executar_envio(self.app, self._mensagem(), envio)
                pausa.assert_not_called()  # o SMTP nunca repete
                self.assertEqual(len(instancias), 1)
                self.assertEqual((envio.estado, envio.transporte, envio.categoria, envio.tentativas),
                                 (EstadoEnvio.FALHA_NO_ENVIO, "smtp", categoria, 1))
                self.assertEqual(categoria in emails.CATEGORIAS_RESULTADO_INCERTO,
                                 categoria == "conexao_interrompida")
                self.assertEqual(instancias[0].eventos[-1], ("close",))

    def test_categorias_de_erro_smtp(self):
        casos = {
            smtplib.SMTPAuthenticationError(535, b"x"): "smtp_autenticacao",
            smtplib.SMTPSenderRefused(550, b"x", "r@example.invalid"): "smtp_remetente_recusado",
            smtplib.SMTPRecipientsRefused({}): "smtp_destinatario_recusado",
            smtplib.SMTPServerDisconnected("x"): "smtp_conexao",
            OSError(101, "Network is unreachable"): "smtp_conexao",
            TimeoutError("x"): "smtp_conexao",
            ssl.SSLCertVerificationError("x"): "tls",
        }
        for erro, categoria in casos.items():
            with self.subTest(erro=type(erro).__name__):
                self.assertEqual(emails._categoria_excecao(erro, "smtp")[0], categoria)
        # API: prazo antes de o pedido sair (URLError) x prazo na resposta (cru).
        self.assertEqual(emails._categoria_excecao(URLError(TimeoutError("x")), "brevo"),
                         ("conexao_sem_resposta", True))
        self.assertEqual(emails._categoria_excecao(TimeoutError("x"), "brevo"), ("tempo_esgotado", False))

    def test_texto_simples_tem_os_links_e_ignora_estilo(self):
        texto = emails._texto_simples(
            '<html><head><style>.x{color:red}</style></head><body><h1>Olá</h1>'
            '<p>Clique <a href="https://a.example.invalid/x?y=1&amp;z=2">aqui</a> agora.</p>'
            '<a href="https://b.example.invalid/">https://b.example.invalid/</a></body></html>')
        self.assertIn("Olá", texto)
        self.assertIn("aqui: https://a.example.invalid/x?y=1&z=2", texto)
        self.assertEqual(texto.count("https://b.example.invalid/"), 1)
        self.assertNotIn("color", texto)

    def test_varredura_de_segredos_reconhece_chaves_do_brevo(self):
        spec = importlib.util.spec_from_file_location("varredura_segredos_teste",
                                                      ROOT / "tools" / "varredura_segredos.py")
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        chave_api = "xkeysib-" + "a1" * 32 + "-" + "Ab" * 8
        chave_smtp = "xsmtpsib-" + "b2" * 32 + "-" + "Cd" * 8

        def rotulos(linha):
            return {rotulo for rotulo, padrao in modulo.PADROES if padrao.search(linha)}

        self.assertIn("Chave do Brevo (API)", rotulos(f"BREVO_API_KEY={chave_api}"))
        self.assertIn("Chave do Brevo (SMTP)", rotulos(f"BREVO_API_KEY={chave_smtp}"))
        self.assertEqual(rotulos("chaves de API começam com xkeysib- e as SMTP com xsmtpsib-"), set())


if __name__ == "__main__":
    unittest.main()
