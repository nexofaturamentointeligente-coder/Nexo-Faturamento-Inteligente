"""Regressões de segurança sem banco real, rede ou credenciais externas."""

import inspect
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, get_flashed_messages
from flask_login import LoginManager
from PIL import Image
from sqlalchemy.exc import IntegrityError
from werkzeug.datastructures import FileStorage

from blueprints import admin, api, auth
from extensions import db
from models import Usuario
from tests import test_auth_tokens_security as token_cases
import session_security


def _app():
    app = Flask(__name__)
    # PUBLIC_URL: origem canônica fictícia dos links (SEC-005), como a factory define.
    app.config.update(SECRET_KEY="isolated-security-test-key", BOOT_ID="test-boot",
                      PUBLIC_URL="https://nexo-test.example.invalid")
    return app


def _usuario(**overrides):
    dados = dict(
        id_usuario=1, is_authenticated=True, is_active=True,
        is_cliente=True, primeiro_acesso=False,
    )
    return SimpleNamespace(**(dados | overrides))


def _login(app, usuario, boot_id="test-boot"):
    manager = LoginManager(app)
    manager.user_loader(lambda user_id: usuario if user_id == "1" else None)
    client = app.test_client()
    with client.session_transaction() as sessao:
        sessao["_user_id"] = "1"
        sessao["_fresh"] = True
        sessao["boot_id"] = boot_id
    return client


class BotInputTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.app.register_blueprint(api.api_bp)
        self.client = _login(self.app, _usuario())

    def test_rejects_non_object_json(self):
        with patch.object(api, "responder") as responder:
            for dados in (["mensagem"], "texto", 12, True, None):
                with self.subTest(dados=dados):
                    response = self.client.post("/api/suporte-bot", json=dados)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("erro", response.get_json())
            responder.assert_not_called()

    def test_rejects_non_text_message(self):
        with patch.object(api, "responder") as responder:
            for mensagem in (3, False, [], {}):
                with self.subTest(mensagem=mensagem):
                    response = self.client.post("/api/suporte-bot", json={"mensagem": mensagem})
                    self.assertEqual(response.status_code, 400)
            responder.assert_not_called()

    def test_valid_message_keeps_limit_and_response(self):
        with patch.object(api, "responder", return_value={"resposta": "ok", "fonte": "local"}) as responder:
            response = self.client.post("/api/suporte-bot", json={"mensagem": "  " + "a" * 1100})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["resposta"], "ok")
            # NexoBot Advanced: o contexto opcional é repassado (ausente = None).
            responder.assert_called_once_with(
                "a" * 1000, ultimo_destino=None, contexto=None, pagina_atual=None,
            )

    def test_admin_remains_forbidden(self):
        self.client = _login(self.app, _usuario(is_cliente=False))
        response = self.client.post("/api/suporte-bot", json={"mensagem": "oi"})
        self.assertEqual(response.status_code, 403)


class PollingSessionTests(token_cases._TokenCase):
    """O polling do sininho (substituiu o Socket.IO) só responde a sessão válida."""

    def _connect(self, usuario, boot_id="test-boot"):
        client = self.app.test_client()
        if usuario is not None:
            with self.app.app_context():
                stored = db.session.get(Usuario, self.ids["active"])
                stored.ativo = usuario.is_active
                stored.primeiro_acesso = usuario.primeiro_acesso
                # SEC-001: só um cookie com sessão registrada no banco consulta.
                token, _ = session_security.registrar_sessao(db.session, stored.id_usuario)
                db.session.commit()
                fingerprint = session_security.impressao_sessao(stored)
                with client.session_transaction() as session:
                    session["_user_id"] = str(stored.id_usuario)
                    session["_fresh"] = True
                    session["boot_id"] = self.app.config["BOOT_ID"] if boot_id == "test-boot" else boot_id
                    session[session_security.CHAVE_CREDENCIAL] = fingerprint
                    session[session_security.CHAVE_SESSAO] = token
        response = client.get("/notificacoes/novas?desde=0")
        if response.status_code == 200:
            self.assertEqual(set(response.get_json()), {"nao_lidas", "novas", "ultimo"})
            return True
        self.assertIn(response.status_code, (401, 302))
        self.assertNotIn("novas", response.get_data(as_text=True))
        return False

    def test_current_active_session_connects(self):
        self.assertTrue(self._connect(_usuario()))

    def test_previous_boot_is_rejected(self):
        self.assertFalse(self._connect(_usuario(), "old-boot"))

    def test_inactive_user_is_rejected(self):
        self.assertFalse(self._connect(_usuario(is_active=False)))

    def test_pending_first_access_is_rejected(self):
        self.assertFalse(self._connect(_usuario(primeiro_acesso=True)))

    def test_anonymous_user_is_rejected(self):
        self.assertFalse(self._connect(None))


class GuideImageTests(unittest.TestCase):
    @staticmethod
    def _image(formato):
        stream = BytesIO()
        Image.new("RGB", (3, 3), "blue").save(stream, format=formato)
        return stream.getvalue()

    def _save(self, content, filename):
        app = _app()
        with app.test_request_context("/", method="POST"):
            with patch.object(admin.storage, "salvar", return_value="db:guia/test.png") as salvar:
                result = admin._salvar_imagem_guia(FileStorage(stream=BytesIO(content), filename=filename))
                messages = get_flashed_messages()
                return result, salvar.call_args_list, messages

    def test_html_disguised_as_png_is_rejected(self):
        result, calls, messages = self._save(b"<html><script>alert(1)</script></html>", "teste.png")
        self.assertIsNone(result)
        self.assertFalse(calls)
        self.assertTrue(messages)

    def test_extension_must_match_content(self):
        result, calls, _ = self._save(self._image("PNG"), "teste.jpg")
        self.assertIsNone(result)
        self.assertFalse(calls)

    def test_truncated_png_is_rejected(self):
        result, calls, _ = self._save(self._image("PNG")[:40], "teste.png")
        self.assertIsNone(result)
        self.assertFalse(calls)

    def test_existing_image_formats_remain_supported(self):
        for extension, formato in (("png", "PNG"), ("jpg", "JPEG"), ("jpeg", "JPEG"), ("webp", "WEBP"), ("gif", "GIF")):
            with self.subTest(extension=extension):
                content = self._image(formato)
                result, calls, messages = self._save(content, f"../teste.{extension}")
                self.assertEqual(result, "db:guia/test.png")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].args[0], content)
                self.assertTrue(calls[0].args[1].startswith("guia/"))
                self.assertNotIn("..", calls[0].args[1])
                self.assertFalse(messages)


class SensitiveLogTests(unittest.TestCase):
    def test_reset_link_and_email_do_not_enter_logs(self):
        app = _app()
        app.register_blueprint(auth.auth_bp, url_prefix="/auth")
        usuario = SimpleNamespace(
            email="synthetic@example.invalid", ativo=True, id_usuario=1,
            senha_hash="synthetic-credential-state", role="CLIENTE", primeiro_acesso=False,
            geracao_autorizacao=0, id_empresa=1,
        )
        fake_db = SimpleNamespace(session=MagicMock())
        fake_db.session.execute.return_value.scalar_one_or_none.return_value = usuario
        # A busca por e-mail (contas_email.py) faz uma única consulta lower(email).
        fake_db.session.execute.return_value.scalars.return_value.all.return_value = [usuario]
        with app.test_request_context("/auth/recuperar-senha", method="POST", data={"email": usuario.email}):
            # Esta fixture isola somente o sigilo dos logs; os testes de
            # autenticação/SEC-004 exercitam a elegibilidade com banco real.
            with patch.object(auth, "db", fake_db), patch.object(auth, "usuario_pode_acessar", return_value=True), \
                    patch.object(auth, "envio_disponivel", return_value=True), \
                    patch.object(auth, "email_recuperacao_senha", return_value=True) as enviar:
                with patch.object(auth.logger, "disabled", False), self.assertLogs(auth.logger, level="INFO") as logs:
                    response = inspect.unwrap(auth.recuperar_senha)()
                self.assertEqual(response.status_code, 302)
                link = enviar.call_args.args[1]
                self.assertNotIn(link, "\n".join(logs.output))
                self.assertNotIn(usuario.email, "\n".join(logs.output))

    def test_database_error_values_do_not_enter_admin_logs(self):
        app = _app()
        app.register_blueprint(admin.admin_bp, url_prefix="/admin")
        fake_db = SimpleNamespace(session=MagicMock())
        fake_db.session.commit.side_effect = IntegrityError(
            "INSERT", {"sensitive": "PRIVATE_SENTINEL"}, ValueError("PRIVATE_SENTINEL"),
        )
        empresa = SimpleNamespace(id_empresa=1)
        usuario = MagicMock()
        form = {
            "data_contratacao": "2026-01-01", "id_segmento": "1", "id_plano_atual": "1",
            "cnpj": "00000000000000", "razao_social": "Synthetic", "email_contato": "synthetic@example.invalid",
            "usuario_nome": "Synthetic", "usuario_email": "synthetic@example.invalid",
        }
        with app.test_request_context("/admin/empresas/nova", method="POST", data=form):
            with patch.object(admin, "db", fake_db), patch.object(admin, "Empresa", return_value=empresa), patch.object(admin, "Usuario", return_value=usuario), patch.object(admin, "_planos_segmentos", return_value=([], [])):
                with self.assertLogs(app.logger, level="WARNING") as logs:
                    response = inspect.unwrap(admin.empresa_nova)()
                self.assertEqual(response.status_code, 302)
                self.assertIn("ValueError", "\n".join(logs.output))
                self.assertNotIn("PRIVATE_SENTINEL", "\n".join(logs.output))
                fake_db.session.rollback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
