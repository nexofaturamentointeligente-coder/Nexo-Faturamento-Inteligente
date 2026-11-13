"""Isolamento de testes: somente SQLite e arquivos sob .audit_tmp/."""

import contextlib
from datetime import date
import io
import os
from pathlib import Path
import re
import secrets
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AUDIT_TMP = ROOT / ".audit_tmp"
_ENV_READY = False


def ensure_test_environment():
    global _ENV_READY
    if _ENV_READY:
        return
    AUDIT_TMP.mkdir(exist_ok=True)
    os.environ.update({
        "FLASK_ENV": "development",
        "SECRET_KEY": secrets.token_hex(32),
        "DATABASE_URL": f"sqlite:///{(AUDIT_TMP / 'unused-test.db').as_posix()}",
        "DIRECT_URL": "", "NEXO_DB_DIRECT": "0",
        "UPLOAD_FOLDER": str(AUDIT_TMP / "uploads"),
        "MAIL_USERNAME": "", "MAIL_PASSWORD": "", "BREVO_API_KEY": "",
        "MAIL_DEFAULT_SENDER": "nexo-test@example.invalid",
        "MAIL_SERVER": "localhost", "MAIL_PORT": "587",
        "MAIL_USE_TLS": "false", "MAIL_USE_SSL": "false",
        "ANTHROPIC_API_KEY": "", "HF_API_TOKEN": "",
        "RATELIMIT_STORAGE_URI": "memory://", "PROXY_FIX_HOPS": "0",
        "RENDER_EXTERNAL_URL": "", "SOCKETIO_CORS_ORIGINS": "http://localhost",
        # SEC-005: origem canônica fictícia dos links; sem restrição de Host
        # (o cliente de teste usa "localhost" e o navegador, 127.0.0.1:porta).
        "NEXO_PUBLIC_URL": "https://nexo-test.example.invalid", "NEXO_TRUSTED_HOSTS": "",
        "ADMIN_EMAIL": "admin-test@example.invalid", "ADMIN_SENHA": "",
        "MAX_CONTENT_LENGTH_MB": "20",
        # WhatsApp (whatsapp_service.py) desligado e sem botões wa.me: nenhum teste dispara de verdade.
        "EVOLUTION_API_URL": "", "EVOLUTION_API_KEY": "", "EVOLUTION_INSTANCE_NAME": "",
        "WHATSAPP_EMPRESA_NUMERO": "",
    })
    _ENV_READY = True


class DeployTestCase(unittest.TestCase):
    """Cada teste recebe banco novo, migrações reais e rede bloqueada."""

    def setUp(self):
        ensure_test_environment()
        self.work = tempfile.TemporaryDirectory(prefix="deploy-test-", dir=AUDIT_TMP)
        self.addCleanup(self.work.cleanup)
        self.work_dir = Path(self.work.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("Rede proibida no teste")))
        # Mesmo que haja um .env privado no checkout, ele não é lido pelo teste.
        with patch("dotenv.load_dotenv", return_value=False):
            import app as app_module
            from config import DevelopmentConfig
        from flask import Flask
        from flask.testing import FlaskClient
        from extensions import db
        import storage
        from flask_migrate import upgrade

        self.db = db
        uri = f"sqlite:///{(self.work_dir / 'test.db').as_posix()}"
        with contextlib.ExitStack() as config_stack:
            for key, value in {
                "SQLALCHEMY_DATABASE_URI": uri,
                "SQLALCHEMY_DIRECT_URI": uri,
                "DEBUG": False, "SQLALCHEMY_ECHO": False,
                "UPLOAD_FOLDER": self.work_dir / "uploads",
                "TESTING": True, "MAIL_ATIVO": False,
                "MAIL_SUPPRESS_SEND": True, "RATELIMIT_ENABLED": False,
                # Mesmo com EVOLUTION_* no ambiente de quem roda a suíte (o config.py o lê na importação).
                "EVOLUTION_API_URL": None, "EVOLUTION_API_KEY": None, "EVOLUTION_INSTANCE_NAME": None,
                "WHATSAPP_EMPRESA_NUMERO": "",
            }.items():
                config_stack.enter_context(patch.object(DevelopmentConfig, key, value, create=True))
            config_stack.enter_context(patch.object(
                app_module, "Flask",
                side_effect=lambda *a, **kw: Flask(*a, instance_path=str(self.work_dir / "instance"), **kw),
            ))
            self.app = app_module.create_app("development")
        self.stack.enter_context(patch.object(storage, "_CACHE_DIR", self.work_dir / "cache"))
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.addCleanup(self._dispose_db)
        # Uma barreira explícita antes de qualquer DDL, mesmo em caso de regressão.
        self.assertEqual(db.engine.url.get_backend_name(), "sqlite")
        self.assertTrue(Path(db.engine.url.database).resolve().is_relative_to(self.work_dir.resolve()))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            upgrade(directory=str(ROOT / "migrations"))
        class IsolatedClient(FlaskClient):
            def open(client_self, *args, **kwargs):
                # Não compartilha o g/current_user do contexto usado nas fixtures.
                with client_self.application.app_context():
                    return super().open(*args, **kwargs)

        self.app.test_client_class = IsolatedClient
        self.client = self.app.test_client()

    def _dispose_db(self):
        self.db.session.remove()
        self.db.engine.dispose()

    def csrf_token(self, path="/auth/login"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match, f"Token CSRF ausente em {path}")
        return match.group(1)

    def login(self, email="cliente-test@example.invalid"):
        response = self.client.post("/auth/login", data={
            "email": email, "senha": self.password,
            "csrf_token": self.csrf_token(),
        })
        self.assertEqual(response.status_code, 302)
        return response

    def logout(self):
        # SEC-002: o logout é POST + CSRF; o GET só mostra a confirmação.
        response = self.client.post("/auth/logout", data={
            "csrf_token": self.csrf_token("/auth/logout"),
        })
        self.assertEqual(response.status_code, 302)
        return response

    def fixtures(self):
        from models import Analise, Empresa, Plano, Segmento, Usuario
        db = self.db
        plano = Plano(nome_plano="BRONZE", valor_mensal=100, qtd_analises_mes=1,
                      tipo_analise_permitida="MENSAL", nivel_entrega_analise="BASICA",
                      nivel_dashboard="RESUMIDO", nivel_atendimento="BAIXO")
        segmento = Segmento(nome_segmento="Segmento fictício de teste")
        db.session.add_all([plano, segmento])
        db.session.flush()
        empresas = [Empresa(
            id_segmento=segmento.id_segmento, id_plano_atual=plano.id_plano,
            cnpj=f"0000000000000{i}", razao_social=f"Empresa fictícia {i}",
            nome_fantasia=f"Teste {i}", email_contato=f"empresa{i}@example.invalid",
            data_contratacao=date(2026, 1, 1),
        ) for i in (1, 2)]
        db.session.add_all(empresas)
        db.session.flush()
        self.password = secrets.token_urlsafe(18)
        admin = Usuario(nome="Admin fictício", email="admin-test@example.invalid",
                        role="ADMIN", primeiro_acesso=False)
        cliente = Usuario(nome="Cliente fictício", email="cliente-test@example.invalid",
                          role="CLIENTE", id_empresa=empresas[0].id_empresa, primeiro_acesso=False)
        for user in (admin, cliente):
            user.set_senha(self.password)
        db.session.add_all([admin, cliente])
        db.session.flush()
        analises = [Analise(
            id_empresa=empresa.id_empresa, id_plano_referencia=plano.id_plano,
            id_usuario_admin_responsavel=admin.id_usuario,
            periodo_inicio=date(2026, 1, 1), periodo_fim=date(2026, 1, 31),
            mes_referencia=1, ano_referencia=2026, tipo_analise="MENSAL",
        ) for empresa in empresas]
        db.session.add_all(analises)
        db.session.commit()
        self.admin_id, self.cliente_id = admin.id_usuario, cliente.id_usuario
        self.analise_id, self.outra_analise_id = [a.id_analise for a in analises]
        return analises
