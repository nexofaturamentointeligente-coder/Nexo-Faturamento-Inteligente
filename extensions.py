"""
NEXO - Faturamento Inteligente | Extensões Flask
================================================
Instâncias compartilhadas de SQLAlchemy e LoginManager.
Padrão: criar aqui, inicializar dentro da factory em app.py.
Isso evita import circular entre models.py e app.py.
"""

import hashlib
import hmac
import smtplib
import ssl

from flask import current_app
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_mail import Connection, Mail
from flask_socketio import SocketIO
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base declarativa SQLAlchemy 2.x. Todos os modelos herdam dela."""
    pass


# Instância única usada em toda a aplicação.
db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()
# Flask-Migrate (Alembic): versionamento de schema do Postgres/Supabase.
migrate = Migrate()
class ValorSMTPInvalido(smtplib.SMTPException):
    """MAIL_SERVER, MAIL_USERNAME ou MAIL_PASSWORD com caractere que o smtplib não codifica."""


class _ConexaoSMTPComPrazo(Connection):
    """Conexão SMTP do Flask-Mail com prazo e certificado TLS verificado.

    O Flask-Mail 0.10 abre o SMTP sem timeout (uma porta bloqueada prende a
    thread por minutos) e o starttls() padrão do smtplib não confere o
    certificado do servidor (a senha SMTP poderia ser entregue a um
    intermediário). Mesmo fluxo da classe original: SSL/STARTTLS, login.
    """

    def configure_host(self):
        prazo = current_app.config.get("EMAIL_TIMEOUT_SEGUNDOS", 15.0)
        contexto = ssl.create_default_context()
        try:
            if self.mail.use_ssl:
                host = smtplib.SMTP_SSL(self.mail.server, self.mail.port, timeout=prazo, context=contexto)
            else:
                host = smtplib.SMTP(self.mail.server, self.mail.port, timeout=prazo)
        except UnicodeError:
            raise ValorSMTPInvalido() from None
        try:
            host.set_debuglevel(int(self.mail.debug))
            # STARTTLS só numa conexão ainda sem TLS. Sobre SMTP_SSL (porta 465)
            # ele é inválido: com MAIL_USE_SSL e MAIL_USE_TLS ligados juntos (o
            # padrão de MAIL_USE_TLS é ligado), vale o SSL e o envio segue.
            if self.mail.use_tls and not self.mail.use_ssl:
                host.starttls(context=contexto)
            if self.mail.username and self.mail.password:
                try:
                    host.login(self.mail.username, self.mail.password)
                except UnicodeError:
                    raise ValorSMTPInvalido() from None
        except BaseException:
            host.close()
            raise
        return host

    def send(self, message, envelope_from=None):
        # A partir daqui a mensagem está na transação SMTP (MAIL/RCPT/DATA): uma
        # queda agora deixa o resultado incerto (emails.py não afirma "não saiu").
        message.nexo_fase_smtp = "envio"
        if self.host is None:
            return super().send(message, envelope_from)
        # O Flask-Mail descarta o retorno de sendmail, onde estão os destinatários recusados
        # quando ao menos um foi aceito. emails.py confere a cópia oculta com esta lista.
        host = self.host  # com MAIL_MAX_EMAILS, o send troca self.host por uma conexão nova
        sendmail = host.sendmail

        def sendmail_com_recusados(*args, **kwargs):
            message.nexo_recusados = sendmail(*args, **kwargs) or {}
            return message.nexo_recusados

        host.sendmail = sendmail_com_recusados
        try:
            return super().send(message, envelope_from)
        finally:
            host.sendmail = sendmail

    def __exit__(self, exc_type, exc_value, tb):
        # O QUIT final não muda o resultado: com a mensagem já aceita (ou com a
        # falha original seguindo adiante), uma conexão derrubada pelo servidor
        # não pode virar "falha no envio" de algo que o SMTP aceitou.
        if self.host is not None:
            try:
                self.host.quit()
            except (smtplib.SMTPException, OSError):
                self.host.close()


class MailComPrazo(Mail):
    """Flask-Mail cujas conexões usam _ConexaoSMTPComPrazo (mesma API: send/connect)."""

    def connect(self):
        app = getattr(self, "app", None) or current_app
        try:
            return _ConexaoSMTPComPrazo(app.extensions["mail"])
        except KeyError as err:
            raise RuntimeError("The current application was not configured with Flask-Mail") from err


# Flask-Mail: envio de e-mails transacionais por SMTP (alternativa à API do
# Brevo; ver emails.py).
mail = MailComPrazo()
# Flask-SocketIO: notificações em tempo real (sininho ao vivo).
# async_mode "threading" é o modo estável no Python 3.12 (sem eventlet/gevent),
# usando o driver simple-websocket. As origens permitidas no handshake são
# definidas na factory (config SOCKETIO_CORS_ORIGINS) para evitar Cross-Site
# WebSocket Hijacking — nunca "*" com cookie de sessão.
socketio = SocketIO(async_mode="threading", logger=False, engineio_logger=False)
# Proteção CSRF (Flask-WTF): valida token em TODAS as requisições mutáveis
# (POST/PUT/PATCH/DELETE). Inicializada na factory.
csrf = CSRFProtect()
# Rate limiting (Flask-Limiter): trava por IP em rotas sensíveis (login,
# recuperação de senha) contra brute force/abuso. Sem default global — só
# aplicamos limites explícitos via decorator nas rotas que precisam.
limiter = Limiter(key_func=get_remote_address)


def chave_limite_conta() -> str:
    """Segunda chave do login e da recuperação: a CONTA alvo (hash do e-mail
    normalizado), somada ao limite por IP. Um ataque distribuído por muitos IPs
    contra um único e-mail esbarra aqui; sem e-mail no formulário vale o IP."""
    from flask import request
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return get_remote_address()
    # HMAC com a SECRET_KEY: o Flask-Limiter escreve a chave no log quando o
    # limite estoura, e um SHA-256 sem segredo de um e-mail é reversível por
    # dicionário (revelaria qual conta foi alvo).
    segredo = current_app.config.get("SECRET_KEY") or ""
    if isinstance(segredo, str):
        segredo = segredo.encode("utf-8")
    return "conta:" + hmac.new(segredo, email.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def chave_limite_usuario() -> str:
    """Limite por usuário autenticado (ações caras ou irreversíveis); anônimo = IP."""
    from flask_login import current_user
    identidade = current_user.get_id() if current_user.is_authenticated else None
    return f"usuario:{identidade}" if identidade else get_remote_address()

# Rota para onde Flask-Login redireciona quando @login_required falha.
login_manager.login_view = "auth.login"
login_manager.login_message = "Faça login para acessar esta página."
login_manager.login_message_category = "warning"
