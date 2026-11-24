"""Configuração de produção em subprocessos isolados, sem serviços externos."""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def run_case(self, code, **values):
        env = os.environ.copy()
        for key in (
            "SECRET_KEY", "DATABASE_URL", "DIRECT_URL", "NEXO_DB_DIRECT",
            "MAIL_USERNAME", "MAIL_PASSWORD", "BREVO_API_KEY", "ANTHROPIC_API_KEY",
            "HF_API_TOKEN", "RATELIMIT_STORAGE_URI", "RENDER_EXTERNAL_URL",
            "NEXO_PUBLIC_URL", "NEXO_TRUSTED_HOSTS",
        ):
            env.pop(key, None)
        env.update(
            FLASK_ENV="production", PYTHONDONTWRITEBYTECODE="1",
            PYTHONIOENCODING="utf-8", PYTHON_DOTENV_DISABLED="1",
            UPLOAD_FOLDER=str(ROOT / ".audit_tmp" / "config-uploads"),
        )
        env.update(values)
        # Desabilita dotenv também em versões anteriores ao suporte à variável.
        prelude = (
            "import dotenv, socket\n"
            "dotenv.load_dotenv = lambda *a, **kw: False\n"
            "def no_network(*a, **kw): raise AssertionError('Rede proibida no teste')\n"
            "socket.socket.connect = no_network\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", prelude + code], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def assert_rejected(self, message, **values):
        self.run_case(
            "from app import create_app\n"
            "try:\n    create_app('production')\n"
            "except RuntimeError as exc:\n"
            f"    assert {message!r} in str(exc)\n"
            "else:\n    raise AssertionError('Configuração insegura aceita')\n",
            **values,
        )

    def test_production_requires_secret(self):
        self.assert_rejected("SECRET_KEY")

    def test_production_rejects_blank_secret(self):
        self.assert_rejected("SECRET_KEY", SECRET_KEY="   ")

    def test_production_rejects_development_secret(self):
        self.assert_rejected("SECRET_KEY", SECRET_KEY="dev-key-trocar-em-producao")

    def test_production_requires_database(self):
        self.assert_rejected("DATABASE_URL", SECRET_KEY=secrets.token_hex(32))

    def test_production_rejects_sqlite(self):
        self.assert_rejected(
            "PostgreSQL", SECRET_KEY=secrets.token_hex(32),
            DATABASE_URL="sqlite:///:memory:",
        )

    def test_production_migrations_require_direct_url(self):
        self.assert_rejected(
            "DIRECT_URL", SECRET_KEY=secrets.token_hex(32), NEXO_DB_DIRECT="1",
            DATABASE_URL="postgresql://localhost/nexo_test",
        )

    # --- SEC-005: origem pública canônica e hosts confiáveis ---------------

    _PRODUCAO = dict(DATABASE_URL="postgresql://localhost/nexo_test")

    def test_production_requires_public_origin(self):
        self.assert_rejected("NEXO_PUBLIC_URL", SECRET_KEY=secrets.token_hex(32), **self._PRODUCAO)

    def test_production_rejects_http_or_malformed_public_origin(self):
        for value in ("http://nexo-test.example.invalid", "https://nexo-test.example.invalid/portal",
                      "https://user:secret@nexo-test.example.invalid", "https://nexo test.invalid"):
            with self.subTest(value=value):
                self.assert_rejected("NEXO_PUBLIC_URL", SECRET_KEY=secrets.token_hex(32),
                                     NEXO_PUBLIC_URL=value, **self._PRODUCAO)

    def test_platform_url_serves_as_public_origin_and_trusted_host(self):
        self.run_case(
            "from app import create_app\n"
            "app = create_app('production')\n"
            "assert app.config['PUBLIC_URL'] == 'https://nexo-test.example.invalid'\n"
            "assert app.config['TRUSTED_HOSTS'] == ['nexo-test.example.invalid']\n"
            "assert 'SOCKETIO_CORS_ORIGINS' not in app.config\n",
            SECRET_KEY=secrets.token_hex(32), RENDER_EXTERNAL_URL="https://nexo-test.example.invalid/",
            **self._PRODUCAO,
        )

    def test_postgres_engine_fails_fast_and_keeps_tcp_alive(self):
        # Prazo de conexão e keepalive TCP do lado do aplicativo: com o banco
        # inalcançável, a tentativa falha em 5 s em vez de prender a thread por
        # minutos. O pool continua o mesmo (nenhum limite do banco mudou).
        self.run_case(
            "from app import create_app\n"
            "app = create_app('production')\n"
            "opcoes = app.config['SQLALCHEMY_ENGINE_OPTIONS']\n"
            "assert opcoes['connect_args'] == {'connect_timeout': 5, 'keepalives': 1, 'keepalives_idle': 30, "
            "'keepalives_interval': 10, 'keepalives_count': 3}, opcoes\n"
            "assert (opcoes['pool_size'], opcoes['max_overflow'], opcoes['pool_timeout']) == (5, 10, 10), opcoes\n"
            "assert opcoes['pool_pre_ping'] and opcoes['hide_parameters'], opcoes\n",
            SECRET_KEY=secrets.token_hex(32), RENDER_EXTERNAL_URL="https://nexo-test.example.invalid/",
            **self._PRODUCAO,
        )

    def test_custom_domain_keeps_platform_host_and_extra_hosts_trusted(self):
        self.run_case(
            "from app import create_app\n"
            "app = create_app('production')\n"
            "assert app.config['PUBLIC_URL'] == 'https://portal.example.invalid'\n"
            "assert app.config['TRUSTED_HOSTS'] == ['portal.example.invalid', 'nexo-test.example.invalid',"
            " '.interno.example.invalid', 'outro.example.invalid']\n",
            SECRET_KEY=secrets.token_hex(32), NEXO_PUBLIC_URL="https://Portal.Example.invalid",
            RENDER_EXTERNAL_URL="https://nexo-test.example.invalid",
            NEXO_TRUSTED_HOSTS=".interno.example.invalid, Outro.Example.invalid", **self._PRODUCAO,
        )

    def test_postgres_alias_and_parameters(self):
        self.run_case(
            "from config import _resolve_database_uri\n"
            "assert _resolve_database_uri() == 'postgresql+psycopg2://localhost/nexo_test?sslmode=require'\n",
            DATABASE_URL="postgres://localhost/nexo_test?pgbouncer=true&connection_limit=5&sslmode=require",
        )

    def test_memory_sqlite_is_not_a_filename(self):
        self.run_case(
            "from config import _resolve_database_uri\n"
            "assert _resolve_database_uri() == 'sqlite:///:memory:'\n",
            DATABASE_URL="sqlite:///:memory:",
        )

    def test_development_import_without_production_secret(self):
        self.run_case(
            "from app import create_app\n"
            "app = create_app('development')\n"
            "assert app.config['SECRET_KEY']\n",
            DATABASE_URL="sqlite:///:memory:",
        )

    def test_wsgi_production_import_and_liveness_without_database_connection(self):
        self.run_case(
            "from wsgi import app\n"
            "assert not app.debug\n"
            "assert app.config['SESSION_COOKIE_SECURE']\n"
            "assert app.config['SESSION_COOKIE_HTTPONLY']\n"
            "assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'\n"
            "client = app.test_client()\n"
            "host = {'Host': 'nexo-test.example.invalid'}\n"
            "assert client.get('/healthz', headers=host).status_code == 200\n"
            "assert client.get('/auth/login', headers=host).status_code == 200\n"
            # SEC-005: em produção, Host fora da lista confiável recebe 400.
            "assert client.get('/healthz').status_code == 400\n"
            "assert client.get('/healthz', headers={'Host': 'evil.invalid'}).status_code == 400\n",
            SECRET_KEY=secrets.token_hex(32),
            DATABASE_URL="postgresql://localhost/nexo_test",
            NEXO_PUBLIC_URL="https://nexo-test.example.invalid",
        )


if __name__ == "__main__":
    unittest.main()
