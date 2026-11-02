"""Hardening complementar (FASE 6.1): política de senha, aviso de troca,
último acesso e limites por CONTA (independentes do IP de origem)."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import patch
from urllib.error import URLError

from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from blueprints import auth
from extensions import db
from models import Empresa, Usuario
import politica_senha
from tests.test_auth_tokens_security import _TokenCase, FIXTURE_HASH, OLD_PASSWORD

FRASE = "Cavalo correto bateria grampo 2026"


class PasswordPolicyUnitTests(unittest.TestCase):
    def _erro(self, senha, confirma=None, **contexto):
        return politica_senha.validar_nova_senha(senha, senha if confirma is None else confirma, **contexto)

    def test_length_bounds_and_passphrase(self):
        self.assertIn("12 caracteres", self._erro("Curta-pw-1!"))      # 11 caracteres
        self.assertIn("128", self._erro("Xy9!" * 33))                  # 132 caracteres
        self.assertIsNone(self._erro(FRASE))
        self.assertIsNone(self._erro("Xy9!" * 32))                     # 128: limite inclusivo

    def test_mismatch(self):
        self.assertIn("não coincidem", self._erro(FRASE, FRASE + "x"))

    def test_common_and_predictable_passwords(self):
        for senha in ("password1234", "Senha12345678!", "123456789012", "098765432109", "aaaaaaaaaaaa",
                      "abcdefghijkl", "NexoFaturamento1", "qwertyuiop12", "Administrador2026"):
            with self.subTest(senha=senha):
                self.assertIn("comum ou previs", self._erro(senha))
        for senha in ("Minha frase bem segura 2026", "tr0ub4dor&3-longo-x", "4 pneus novos no carro"):
            with self.subTest(senha=senha):
                self.assertIsNone(self._erro(senha))

    def test_personal_parts_are_refused(self):
        contexto = {"email": "joao.silva@example.invalid", "nome": "João da Silva"}
        self.assertIn("nome ou seu e-mail", self._erro("Silva-forte-2026-xyz", **contexto))
        self.assertIn("nome ou seu e-mail", self._erro("xx joao.silva 2026 xx", **contexto))
        self.assertIsNone(self._erro("Minha frase bem segura 2026", **contexto))
        self.assertIsNone(self._erro("Minha frase bem segura 2026", email="x@y", nome="Jo"))  # partes curtas

    def test_same_as_current_password(self):
        atual = generate_password_hash(FRASE)
        self.assertIn("diferente da senha atual", self._erro(FRASE, hash_atual=atual))
        self.assertIsNone(self._erro(FRASE + " nova", hash_atual=atual))

    def test_leaked_check_is_opt_in_and_fails_open(self):
        with patch.dict(os.environ, {"NEXO_SENHAS_VAZADAS": "0"}):
            self.assertIsNone(politica_senha.senha_vazada(FRASE))
        import hashlib
        resumo = hashlib.sha1(FRASE.encode()).hexdigest().upper()

        class Resposta(BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False
        corpo = f"0000000000000000000000000000000000A:0\r\n{resumo[5:]}:7\r\n".encode()
        with patch.dict(os.environ, {"NEXO_SENHAS_VAZADAS": "1"}):
            # side_effect (não return_value): cada chamada recebe um buffer novo,
            # não um já esgotado pela leitura anterior.
            with patch.object(politica_senha, "urlopen", side_effect=lambda *a, **k: Resposta(corpo)) as pedido:
                self.assertTrue(politica_senha.senha_vazada(FRASE))
                self.assertIn("vazamentos", self._erro(FRASE))
            url = pedido.call_args[0][0].full_url
            self.assertTrue(url.endswith("/range/" + resumo[:5]), url)      # só o prefixo sai
            self.assertNotIn(resumo[5:], url)
            with patch.object(politica_senha, "urlopen", return_value=Resposta(b"ABCDEF0123456789ABCDEF0123456789ABC:3\r\n")):
                self.assertFalse(politica_senha.senha_vazada(FRASE))
            with patch.object(politica_senha, "urlopen", side_effect=URLError("rede indisponível")), \
                    patch.object(politica_senha.logger, "disabled", False), \
                    self.assertLogs(politica_senha.logger, level="WARNING") as logs:
                self.assertIsNone(politica_senha.senha_vazada(FRASE))
                self.assertIsNone(self._erro(FRASE + " outra"))               # fail-open, só registra
            self.assertNotIn(FRASE, "\n".join(logs.output))


class PasswordPolicyFlowTests(_TokenCase):
    def test_reset_refuses_weak_password_and_keeps_token_usable(self):
        token = self._token()
        for senha in ("password1234", "curta", "active-conta-2026!"):   # comum, curta, contém o e-mail
            with self.subTest(senha=senha):
                response = self._post(token, password=senha)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self._account()[0], FIXTURE_HASH)
        self._assert_success(self._post(token, password=FRASE))
        self.assertTrue(check_password_hash(self._account()[0], FRASE))

    def test_activation_and_first_access_apply_the_same_policy(self):
        token = self._token("pending", "activation")
        self.assertEqual(self._post(token, "activation", password="Pending-conta-2026!").status_code, 200)
        self.assertEqual(self._account("pending")[0], FIXTURE_HASH)
        self._assert_success(self._post(token, "activation", password=FRASE))
        # Primeiro acesso obrigatório (sessão autenticada): senha igual à temporária é recusada.
        with self.app.app_context():
            db.session.get(Usuario, self.ids["active"]).primeiro_acesso = True
            db.session.commit()
        client = self.app.test_client()
        response = client.post("/auth/login", data={
            "email": "active@example.invalid", "senha": OLD_PASSWORD, "csrf_token": self._csrf(client),
        })
        self.assertEqual(response.status_code, 302)
        response = client.post("/auth/primeiro-acesso", data={
            "nova_senha": OLD_PASSWORD, "confirma_senha": OLD_PASSWORD,
            "csrf_token": self._csrf(client, "/auth/primeiro-acesso"),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._account("active")[0], FIXTURE_HASH)
        response = client.post("/auth/primeiro-acesso", data={
            "nova_senha": FRASE, "confirma_senha": FRASE,
            "csrf_token": self._csrf(client, "/auth/primeiro-acesso"),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(check_password_hash(self._account("active")[0], FRASE))

    def test_reset_sends_a_password_changed_notice_without_tokens(self):
        token = self._token()
        # Capturado durante a chamada (dentro do contexto da requisição), antes de o
        # objeto ORM se desanexar da sessão após o commit.
        capturado = {}

        def registrar(usuario, link):
            capturado["id"] = usuario.id_usuario
            capturado["link"] = link
            return True
        with patch.object(auth, "email_senha_alterada", side_effect=registrar) as aviso:
            self._assert_success(self._post(token))
        aviso.assert_called_once()
        self.assertEqual(capturado["id"], self.ids["active"])
        self.assertTrue(capturado["link"].endswith("/auth/recuperar-senha"), capturado["link"])
        self.assertNotIn(token, capturado["link"])
        # Tentativa recusada pela política não gera aviso.
        with patch.object(auth, "email_senha_alterada", return_value=True) as aviso:
            self.assertEqual(self._post(self._token(), password="password1234").status_code, 200)
        aviso.assert_not_called()

    def test_login_records_last_access(self):
        with self.app.app_context():
            self.assertIsNone(db.session.get(Usuario, self.ids["active"]).ultimo_acesso)
        antes = datetime.now(timezone.utc) - timedelta(seconds=5)
        response = self.client.post("/auth/login", data={
            "email": "active@example.invalid", "senha": OLD_PASSWORD, "csrf_token": self._csrf(),
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            registrado = db.session.get(Usuario, self.ids["active"]).ultimo_acesso
        self.assertIsNotNone(registrado)
        if registrado.tzinfo is None:
            registrado = registrado.replace(tzinfo=timezone.utc)
        self.assertGreaterEqual(registrado, antes)
        # Senha errada não registra acesso (cliente novo: o primeiro já está autenticado).
        with self.app.app_context():
            self.assertIsNone(db.session.get(Usuario, self.ids["other"]).ultimo_acesso)
        outro = self.app.test_client()
        outro.post("/auth/login", data={
            "email": "other@example.invalid", "senha": "senha-errada-sintetica",
            "csrf_token": self._csrf(outro),
        })
        with self.app.app_context():
            self.assertIsNone(db.session.get(Usuario, self.ids["other"]).ultimo_acesso)


class AccountRateLimitTests(_TokenCase):
    ENABLE_LIMITS = True

    def setUp(self):
        super().setUp()
        # Orçamentos pequenos (lidos a cada requisição) para exercitar o mecanismo
        # sem centenas de hashes de senha; os padrões de produção ficam em config.
        self.app.config["LIMITE_LOGIN_CONTA"] = "12 per 15 minutes"
        self.app.config["LIMITE_RECUPERACAO_CONTA"] = "3 per 15 minutes"

    def test_production_defaults_exceed_a_single_source_budget(self):
        """Um orçamento por conta menor que o teto por IP × janela seria esgotável de
        um único IP (lockout barato do e-mail alvo). Os padrões exigem vários IPs."""
        from config import Config
        import re
        login = Config.LIMITE_LOGIN_CONTA
        n_conta, janela = re.match(r"(\d+) per (\d+)? ?(minute|minutes|hour|hours)", login).group(1, 2)
        minutos = int(janela or 1) * (60 if "hour" in login else 1)
        self.assertGreater(int(n_conta), 10 * minutos, "orçamento por conta deve superar 10/min por IP × janela")
        self.assertRegex(Config.LIMITE_RECUPERACAO_CONTA, r"^\d+ per (\d+ )?(hour|hours|minutes?)$")

    def _de(self, ip):
        return {"REMOTE_ADDR": ip}

    def test_login_is_limited_per_account_across_source_addresses(self):
        csrf = self._csrf()
        codigos = []
        for tentativa in range(14):           # 7 tentativas por IP (< 10/min por IP), 2 IPs
            codigos.append(self.client.post("/auth/login", data={
                "email": "Active@example.invalid", "senha": "senha-errada-sintetica", "csrf_token": csrf,
            }, environ_base=self._de(f"10.0.0.{tentativa % 2 + 1}")).status_code)
        self.assertEqual(codigos, [200] * 12 + [429] * 2)  # orçamento de teste: 12
        # Outra conta, de um IP já usado, segue livre: o bloqueio é da conta alvo.
        response = self.client.post("/auth/login", data={
            "email": "other@example.invalid", "senha": "senha-errada-sintetica", "csrf_token": csrf,
        }, environ_base=self._de("10.0.0.1"))
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():   # nada mudou nas contas
            self.assertEqual(db.session.get(Usuario, self.ids["active"]).senha_hash, FIXTURE_HASH)

    def test_recovery_is_limited_per_account_across_source_addresses(self):
        csrf = self._csrf()
        codigos = [self.client.post("/auth/recuperar-senha", data={
            "email": " ACTIVE@example.invalid ", "csrf_token": csrf,
        }, environ_base=self._de(f"10.0.1.{i + 1}")).status_code for i in range(4)]
        self.assertEqual(codigos, [302, 302, 302, 429])
        self.assertEqual(self.recovery_mail.call_count, 3)
        response = self.client.post("/auth/recuperar-senha", data={
            "email": "other@example.invalid", "csrf_token": csrf,
        }, environ_base=self._de("10.0.1.1"))
        self.assertEqual(response.status_code, 302)

    def test_permanent_delete_attempts_are_limited_per_admin(self):
        from company_security import excluir_empresa
        client = self.app.test_client()
        response = client.post("/auth/login", data={
            "email": "admin@example.invalid", "senha": OLD_PASSWORD, "csrf_token": self._csrf(client),
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            empresa = db.session.scalar(select(Empresa).where(Empresa.cnpj == "00000000000002"))
            id_empresa = empresa.id_empresa
            excluir_empresa(db.session, id_empresa)
            db.session.commit()
        csrf = self._csrf(client, "/admin/lixeira")
        codigos = [client.post(f"/admin/empresa/excluir-permanente/{id_empresa}", data={
            "csrf_token": csrf, "senha_confirmacao": "senha-errada-sintetica",
        }).status_code for _ in range(6)]
        self.assertEqual(codigos, [302] * 5 + [429])
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Empresa, id_empresa))
            self.assertIsNotNone(db.session.get(Usuario, self.ids["other"]))


if __name__ == "__main__":
    unittest.main()
