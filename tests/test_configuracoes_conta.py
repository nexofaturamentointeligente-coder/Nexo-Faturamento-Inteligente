"""Módulo de Configurações do cliente e do admin, com migrações reais em SQLite.

Executar da raiz: python -B -m unittest tests.test_configuracoes_conta -v

Dados sintéticos (example.invalid); nenhum e-mail real sai: o transporte é substituído
e qualquer conexão de socket falha o teste. O relógio é o da política (NEXO_RELOGIO).
"""

import contextlib
from datetime import date, timedelta
from decimal import Decimal
import io
import json
import re
import smtplib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.security import check_password_hash

import configuracoes_conta
import contract_admin
import contract_policy
import emails
import politica_senha
import preferencias_usuario
from blueprints import auth
from tests.contratos_support import T0, TEXTO_PRIVADO, ContratosCase
from tests.deploy_support import ROOT
from tests.test_auth_tokens_security import OLD_PASSWORD, _TokenCase
from tests.test_nexobot_api import NexoBotApiBase

SENHA = "Trilha serena 4821 norte"
NOVA = "Horizonte azul 2026 rumo"
CLIENTE = "cliente-test@example.invalid"
ADMIN = "admin-test@example.invalid"
PAGINA_CLIENTE = "/cliente/configuracoes"
PAGINA_ADMIN = "/admin/configuracoes"
ROTAS_CLIENTE = ("perfil", "senha", "email", "aceitar-documento", "solicitar-exclusao", "preferencias")
ROTAS_ADMIN = ("senha", "documentos", "copia-suporte")


class _ConfigCase(ContratosCase):
    def setUp(self):
        super().setUp()
        from models import Empresa, Usuario
        for id_usuario in (self.admin_id, self.cliente_id):
            self.db.session.get(Usuario, id_usuario).set_senha(SENHA)
        self.password = SENHA
        self.db.session.commit()
        self.Empresa, self.Usuario = Empresa, Usuario

    def _csrf(self, cliente, caminho):
        resposta = cliente.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', resposta.get_data(as_text=True)).group(1)

    def _entrar(self, email=CLIENTE, senha=SENHA, cliente=None):
        cliente = cliente or self.app.test_client()
        resposta = cliente.post("/auth/login", data={"email": email, "senha": senha,
                                                     "csrf_token": self._csrf(cliente, "/auth/login")})
        return cliente, resposta

    def _enviar(self, rota, cliente=None, pagina=PAGINA_CLIENTE, seguir=True, **dados):
        cliente = cliente or self.client
        return cliente.post(rota, data={"csrf_token": self._csrf(cliente, pagina), **dados}, follow_redirects=seguir)

    def _usuario(self, id_usuario=None):
        self.db.session.expire_all()
        return self.db.session.get(self.Usuario, id_usuario or self.cliente_id)

    def _empresa(self, id_empresa=None):
        self.db.session.expire_all()
        return self.db.session.get(self.Empresa, id_empresa or self.empresa_id)

    def _novo_cliente(self, email, id_empresa=None, nome="Outra Pessoa"):
        usuario = self.Usuario(nome=nome, email=email, role="CLIENTE", primeiro_acesso=False,
                               id_empresa=id_empresa or self.empresa_id)
        usuario.set_senha(SENHA)
        self.db.session.add(usuario)
        self.db.session.commit()
        return usuario.id_usuario

    def _contar(self, modelo, *condicoes):
        self.db.session.expire_all()
        return self.db.session.scalar(select(func.count()).select_from(modelo).where(*condicoes))

    @contextlib.contextmanager
    def _emails(self):
        """Transporte substituído; ao sair, espera as threads de envio."""
        enviados = []
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None, EMAIL_ALERTA_ADMIN=False)
        with patch.object(emails.mail, "send", side_effect=enviados.append):
            yield enviados
            emails._aguardar_envios_pendentes(5.0)


class AcessoTests(_ConfigCase):
    def test_anonimo_vai_para_o_login_e_outro_perfil_recebe_403(self):
        anonimo = self.app.test_client()
        csrf = self._csrf(anonimo, "/auth/login")
        rotas = [f"/cliente/configuracoes/{r}" for r in ROTAS_CLIENTE] + [f"/admin/configuracoes/{r}" for r in ROTAS_ADMIN]
        for rota in rotas:
            with self.subTest(rota=rota):
                resposta = anonimo.post(rota, data={"csrf_token": csrf})
                self.assertEqual(resposta.status_code, 302)
                self.assertIn("/auth/login", resposta.headers["Location"])
        for caminho in (PAGINA_CLIENTE, PAGINA_ADMIN, "/cliente/configuracoes/exportar-dados"):
            self.assertIn("/auth/login", anonimo.get(caminho).headers["Location"])
        cliente, _ = self._entrar()
        csrf = self._csrf(cliente, PAGINA_CLIENTE)
        self.assertEqual(cliente.get(PAGINA_ADMIN).status_code, 403)
        for rota in ROTAS_ADMIN:
            self.assertEqual(cliente.post(f"/admin/configuracoes/{rota}", data={"csrf_token": csrf}).status_code, 403, rota)
        admin, _ = self._entrar(ADMIN)
        csrf = self._csrf(admin, PAGINA_ADMIN)
        self.assertEqual(admin.get(PAGINA_CLIENTE).status_code, 403)
        self.assertEqual(admin.get("/cliente/configuracoes/exportar-dados").status_code, 403)
        for rota in ROTAS_CLIENTE:
            self.assertEqual(admin.post(f"/cliente/configuracoes/{rota}", data={"csrf_token": csrf}).status_code, 403, rota)

    def test_acoes_nao_existem_por_get(self):
        cliente, _ = self._entrar()
        for rota in ROTAS_CLIENTE:
            self.assertEqual(cliente.get(f"/cliente/configuracoes/{rota}").status_code, 405, rota)
        admin, _ = self._entrar(ADMIN)
        for rota in ROTAS_ADMIN:
            self.assertEqual(admin.get(f"/admin/configuracoes/{rota}").status_code, 405, rota)

    def test_sem_csrf_nada_muda(self):
        cliente, _ = self._entrar()
        resposta = cliente.post("/cliente/configuracoes/perfil", data={"nome": "Nome Forjado"})
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(self._usuario().nome, "Cliente fictício")
        resposta = cliente.post("/cliente/configuracoes/senha", data={"senha_atual": SENHA, "nova_senha": NOVA,
                                                                       "confirmacao_nova_senha": NOVA})
        self.assertEqual(resposta.status_code, 400)
        self.assertTrue(self._usuario().check_senha(SENHA))

    def test_todo_formulario_post_das_paginas_leva_csrf(self):
        for email, caminho in ((CLIENTE, PAGINA_CLIENTE), (ADMIN, PAGINA_ADMIN)):
            cliente, _ = self._entrar(email)
            html = cliente.get(caminho).get_data(as_text=True)
            formularios = re.findall(r"<form\b[^>]*method=\"POST\"[^>]*>(.*?)</form>", html, re.S | re.I)
            self.assertGreaterEqual(len(formularios), 3)
            for corpo in formularios:
                self.assertIn('name="csrf_token"', corpo)

    def test_paginas_nao_ficam_em_cache(self):
        for email, caminho in ((CLIENTE, PAGINA_CLIENTE), (ADMIN, PAGINA_ADMIN)):
            self.assertEqual(self._entrar(email)[0].get(caminho).headers["Cache-Control"], "no-store")

    def test_primeiro_acesso_pendente_nao_abre_as_configuracoes(self):
        usuario = self._usuario()
        usuario.primeiro_acesso = True
        self.db.session.commit()
        cliente, _ = self._entrar()
        resposta = cliente.get(PAGINA_CLIENTE)
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/auth/primeiro-acesso", resposta.headers["Location"])


class PerfilTests(_ConfigCase):
    def setUp(self):
        super().setUp()
        self.client, _ = self._entrar()

    def test_salva_nome_e_telefone_so_da_propria_empresa(self):
        pagina = self._enviar("/cliente/configuracoes/perfil", nome="  Ana   Maria ", telefone=" (11)  91234-5678 ",
                              id_empresa=str(self.outra_empresa_id), id_usuario=str(self.admin_id))
        self.assertIn("Dados atualizados.", pagina.get_data(as_text=True))
        self.assertEqual(self._usuario().nome, "Ana Maria")
        self.assertEqual(self._empresa().telefone_contato, "(11) 91234-5678")
        self.assertIsNone(self._empresa(self.outra_empresa_id).telefone_contato)
        self.assertEqual(self._usuario(self.admin_id).nome, "Admin fictício")

    def test_telefone_vazio_remove_e_nome_com_apostrofo_e_aceito_e_escapado(self):
        self._empresa().telefone_contato = "(11) 3333-4444"
        self.db.session.commit()
        pagina = self._enviar("/cliente/configuracoes/perfil", nome='Ana "D\'Ávila"', telefone="").get_data(as_text=True)
        self.assertIsNone(self._empresa().telefone_contato)
        self.assertEqual(self._usuario().nome, 'Ana "D\'Ávila"')
        self.assertNotIn('value="Ana "D', pagina)
        self.assertIn("&#34;", pagina)

    def test_entradas_invalidas_nao_gravam_nada(self):
        casos = (dict(nome="", telefone=""), dict(nome="A", telefone=""), dict(nome="1234", telefone=""),
                 dict(nome="x" * 121, telefone=""), dict(nome="<script>alert(1)</script>", telefone=""),
                 dict(nome="Nome‮Invertido", telefone=""), dict(nome="Ana Maria", telefone="abc"),
                 dict(nome="Ana Maria", telefone="123"), dict(nome="Ana Maria", telefone="11 91234-5678<b>"),
                 dict(nome="Ana Maria", telefone="1" * 16))
        for dados in casos:
            with self.subTest(dados=dados):
                self._enviar("/cliente/configuracoes/perfil", **dados)
                self.assertEqual(self._usuario().nome, "Cliente fictício")
                self.assertIsNone(self._empresa().telefone_contato)


class PerfilConcorrenciaENomeTests(_ConfigCase):
    def setUp(self):
        super().setUp()
        self.client, _ = self._entrar()

    def _salvar(self, **dados):
        return self._enviar("/cliente/configuracoes/perfil", **dados).get_data(as_text=True)

    def test_nome_com_endereco_e_recusado_e_nome_comum_passa(self):
        for nome in ("https://evil.example/retomar Silva", "Acesse:https://evil.example", "www.evil.example",
                     "evil.example", "a@evil.example", "Ana/Bia"):
            self.assertIn("não pode conter endereço", self._salvar(nome=nome, telefone=""), nome)
        for nome in ("João d'Ávila-Souza", "Dr. João", "J.R. Silva", "Maria (Financeiro)"):
            self.assertIn("Dados atualizados.", self._salvar(nome=nome, telefone=""), nome)
            self.assertEqual(self._usuario().nome, nome)

    def test_nome_antigo_com_barra_nao_impede_salvar_o_telefone(self):
        usuario = self._usuario()
        usuario.nome = "Ana/Bia Financeiro"
        self.db.session.commit()
        self.assertIn("Dados atualizados.", self._salvar(nome="Ana/Bia Financeiro", telefone="(11) 91234-5678"))
        self.assertEqual(self._empresa().telefone_contato, "(11) 91234-5678")

    def test_formulario_aberto_nao_desfaz_o_telefone_do_colega(self):
        empresa = self._empresa()
        empresa.telefone_contato = "(11) 3333-4444"
        self.db.session.commit()
        self._novo_cliente("colega@example.invalid")
        colega, _ = self._entrar("colega@example.invalid")
        self.assertIn('name="telefone_exibido" value="(11) 3333-4444"', self.client.get(PAGINA_CLIENTE).get_data(as_text=True))
        self._enviar("/cliente/configuracoes/perfil", cliente=colega, nome="Colega", telefone="(21) 99999-0000", telefone_exibido="(11) 3333-4444")
        self.assertIn("Dados atualizados.", self._salvar(nome="Só o nome", telefone="(11) 3333-4444", telefone_exibido="(11) 3333-4444"))
        self.assertEqual(self._empresa().telefone_contato, "(21) 99999-0000")
        self.assertEqual(self._usuario().nome, "Só o nome")
        # Troca de verdade, ainda que com a tela antiga, e limpar de propósito continuam valendo.
        self._salvar(nome="Só o nome", telefone="(31) 98888-7777", telefone_exibido="(11) 3333-4444")
        self.assertEqual(self._empresa().telefone_contato, "(31) 98888-7777")
        self._salvar(nome="Só o nome", telefone="", telefone_exibido="(31) 98888-7777")
        self.assertIsNone(self._empresa().telefone_contato)

    def test_campo_ausente_nao_apaga_o_telefone(self):
        empresa = self._empresa()
        empresa.telefone_contato = "(11) 3333-4444"
        self.db.session.commit()
        self.assertIn("Dados atualizados.", self._salvar(nome="Ana Maria"))
        self.assertEqual(self._empresa().telefone_contato, "(11) 3333-4444")

    def test_mensagem_de_chamado_identifica_o_papel_de_quem_escreveu(self):
        from models import ChamadoSuporte
        self._salvar(nome="Consultor NEXO", telefone="")
        self._enviar("/cliente/tickets", pagina="/cliente/tickets", assunto="Dúvida", categoria="FINANCEIRO", mensagem="Texto fictício")
        self.db.session.expire_all()
        id_chamado = self.db.session.scalar(select(ChamadoSuporte.id_chamado))
        admin, _ = self._entrar(ADMIN)
        for cliente, caminho in ((self.client, f"/cliente/tickets/{id_chamado}"), (admin, f"/admin/tickets/{id_chamado}")):
            self.assertIn("Consultor NEXO · Cliente", cliente.get(caminho).get_data(as_text=True), caminho)


class FormularioExpiradoTests(_ConfigCase):
    def test_token_invalido_mostra_a_pagina_do_portal_e_nada_muda(self):
        cliente, _ = self._entrar()
        nome = self._usuario().nome
        resposta = cliente.post("/cliente/configuracoes/perfil", data={"nome": "Outro Nome", "csrf_token": "adulterado"})
        corpo = resposta.get_data(as_text=True)
        self.assertEqual(resposta.status_code, 400)
        self.assertNotIn("Location", resposta.headers)
        self.assertIn("Formulário expirado", corpo)
        self.assertIn("Nada foi gravado", corpo)
        self.assertNotIn("Redirecting", corpo)
        self.assertEqual(self._usuario().nome, nome)

    def test_api_continua_em_json(self):
        cliente, _ = self._entrar()
        resposta = cliente.post("/api/suporte-bot", json={"mensagem": "oi"})
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(resposta.mimetype, "application/json")


class SenhaTests(_ConfigCase):
    def _trocar(self, senha_atual=SENHA, nova=NOVA, confirmacao=None, cliente=None, rota="/cliente/configuracoes/senha",
                pagina=PAGINA_CLIENTE):
        return self._enviar(rota, cliente=cliente or self.client, pagina=pagina, senha_atual=senha_atual, nova_senha=nova,
                            confirmacao_nova_senha=NOVA if confirmacao is None and nova == NOVA else confirmacao or nova)

    def setUp(self):
        super().setUp()
        self.client, _ = self._entrar()

    def test_senha_atual_errada_nao_altera_nada(self):
        pagina = self._trocar(senha_atual="senha errada 123").get_data(as_text=True)
        self.assertIn("A senha atual está incorreta.", pagina)
        self.assertTrue(self._usuario().check_senha(SENHA))
        self.assertIn("A senha atual está incorreta.", self._trocar(senha_atual="x" * 2000).get_data(as_text=True))

    def test_politica_forte_no_servidor(self):
        casos = (("abc123", "ao menos 12"), ("abcdefghijklmnopq rs", "letras e números"),
                 ("192837465 019283", "letras e números"), ("Cliente fictício 2026 ok", "nome ou seu e-mail"),
                 (SENHA, "diferente da senha atual"), ("a1" * 65, "no máximo 128"), ("password12345", "comum ou previsível"))
        for nova, mensagem in casos:
            with self.subTest(nova=nova[:20]):
                self.assertIn(mensagem, self._trocar(nova=nova).get_data(as_text=True))
                self.assertTrue(self._usuario().check_senha(SENHA))
        self.assertIn("não coincidem", self._trocar(confirmacao="Outra senha 2026 aqui").get_data(as_text=True))

    def test_composicao_so_vale_para_as_configuracoes(self):
        self.assertIsNone(politica_senha.validar_nova_senha("frase longa so de letras", "frase longa so de letras"))
        self.assertIn("letras e números", politica_senha.validar_nova_senha(
            "frase longa so de letras", "frase longa so de letras", exigir_letras_e_numeros=True))

    def test_troca_derruba_as_outras_sessoes_mantem_esta_e_avisa_o_dono(self):
        outra, _ = self._entrar()
        self.assertEqual(outra.get(PAGINA_CLIENTE).status_code, 200)
        hash_antigo = self._usuario().senha_hash
        with patch.object(auth, "email_senha_alterada") as aviso:
            pagina = self._trocar()
        self.assertIn("Senha alterada.", pagina.get_data(as_text=True))
        usuario = self._usuario()
        self.assertNotEqual(usuario.senha_hash, hash_antigo)
        self.assertTrue(check_password_hash(usuario.senha_hash, NOVA))
        self.assertTrue(usuario.senha_hash.startswith(("scrypt:", "pbkdf2:")))
        self.assertEqual(self.client.get(PAGINA_CLIENTE).status_code, 200)
        self.assertEqual(outra.get(PAGINA_CLIENTE).status_code, 302)
        self.assertEqual(aviso.call_args.kwargs, {"nas_configuracoes": True})
        self.assertEqual(self._entrar(senha=SENHA)[1].headers.get("Location"), None)
        self.assertEqual(self._entrar(senha=NOVA)[1].status_code, 302)

    def test_falha_no_aviso_nao_vira_erro_da_troca(self):
        with patch.object(auth, "email_senha_alterada", side_effect=RuntimeError("falha sintética")):
            self.assertIn("Senha alterada.", self._trocar().get_data(as_text=True))
        self.assertTrue(self._usuario().check_senha(NOVA))

    def test_aviso_real_de_senha_diz_que_foi_nas_configuracoes(self):
        with self._emails() as enviados:
            self._trocar()
        self.assertEqual(enviados[0].recipients, [CLIENTE])
        self.assertIn("alterada nas Configurações da conta", enviados[0].html)
        self.assertNotIn(NOVA, enviados[0].html)

    def test_admin_troca_a_propria_senha_exigindo_a_atual(self):
        admin, _ = self._entrar(ADMIN)
        rota = {"rota": "/admin/configuracoes/senha", "pagina": PAGINA_ADMIN, "cliente": admin}
        with patch.object(auth, "email_senha_alterada"):
            self.assertIn("A senha atual está incorreta.", self._trocar(senha_atual="errada", **rota).get_data(as_text=True))
            self.assertTrue(self._usuario(self.admin_id).check_senha(SENHA))
            self.assertIn("Senha alterada.", self._trocar(**rota).get_data(as_text=True))
        self.assertTrue(self._usuario(self.admin_id).check_senha(NOVA))
        self.assertEqual(admin.get(PAGINA_ADMIN).status_code, 200)
        self.assertTrue(self._usuario().check_senha(SENHA))


class EmailDeAcessoTests(_ConfigCase):
    NOVO = "novo.endereco@example.invalid"

    def setUp(self):
        super().setUp()
        self.client, _ = self._entrar()

    def _trocar(self, novo=NOVO, senha=SENHA):
        return self._enviar("/cliente/configuracoes/email", novo_email=novo, confirmacao_novo_email=novo,
                            senha_atual=senha)

    def test_recusas_nao_alteram_nada(self):
        self._novo_cliente("Ocupado@example.invalid")
        casos = ((self.NOVO, "senha errada", "A senha atual está incorreta."), ("", SENHA, "e-mail válido"),
                 ("sem-arroba", SENHA, "e-mail válido"), ("a@b.co\r\nBcc: x@example.invalid", SENHA, "e-mail válido"),
                 ("CLIENTE-test@example.invalid", SENHA, "já é o e-mail de acesso"),
                 ("ocupado@example.invalid", SENHA, "não pode ser usado"), (ADMIN.upper(), SENHA, "não pode ser usado"))
        for novo, senha, mensagem in casos:
            with self.subTest(novo=novo):
                self.assertIn(mensagem, self._trocar(novo, senha).get_data(as_text=True))
                self.assertEqual(self._usuario().email, CLIENTE)

    def test_troca_normaliza_atualiza_o_login_e_mantem_so_esta_sessao(self):
        outra, _ = self._entrar()
        geracao = self._usuario().geracao_autorizacao
        contato = self._empresa().email_contato
        with self._emails() as enviados:
            pagina = self._trocar(" Novo.Endereco@Example.invalid ")
        self.assertIn("E-mail de acesso alterado", pagina.get_data(as_text=True))
        usuario = self._usuario()
        self.assertEqual(usuario.email, self.NOVO)
        self.assertEqual(usuario.geracao_autorizacao, geracao + 1)
        self.assertEqual(self._empresa().email_contato, contato)
        self.assertTrue(usuario.check_senha(SENHA))
        self.assertEqual(self.client.get(PAGINA_CLIENTE).status_code, 200)
        self.assertIn(self.NOVO.split("@")[0], self.client.get(PAGINA_CLIENTE).get_data(as_text=True))
        self.assertEqual(outra.get(PAGINA_CLIENTE).status_code, 302)
        self.assertEqual(self._entrar(self.NOVO)[1].status_code, 302)
        self.assertNotIn("Location", self._entrar(CLIENTE)[1].headers)
        # O aviso vai só para o endereço antigo, com o novo mascarado e sem link de acesso.
        self.assertEqual([m.recipients for m in enviados], [[CLIENTE]])
        self.assertIn("no***@example.invalid", enviados[0].html)
        self.assertNotIn(self.NOVO, enviados[0].html)
        self.assertNotIn("/auth/resetar-senha", enviados[0].html)

    def test_link_de_senha_enviado_ao_endereco_antigo_deixa_de_valer(self):
        with self.app.test_request_context("/"):
            token = auth.gerar_token_recuperacao_senha(self._usuario())
            self.assertIsNotNone(auth._ler_token_reset(token))
        self._trocar()
        self.db.session.expire_all()
        with self.app.test_request_context("/"):
            self.assertIsNone(auth._ler_token_reset(token))

    def test_endereco_tomado_no_meio_da_troca_vira_mensagem(self):
        self._novo_cliente(self.NOVO)
        with patch.object(auth, "email_em_uso", return_value=False):
            pagina = self._trocar()
        self.assertIn("não pode ser usado", pagina.get_data(as_text=True))
        self.assertEqual(self._usuario().email, CLIENTE)
        self.assertEqual(self.client.get(PAGINA_CLIENTE).status_code, 200)

    def test_credencial_alterada_no_meio_da_troca_nao_grava(self):
        def outra_requisicao(*_a, **_k):
            with self.app.app_context():
                usuario = self.db.session.get(self.Usuario, self.cliente_id)
                usuario.set_senha("Outra senha 2026 forte")
                self.db.session.commit()
            return False
        with patch.object(auth, "email_em_uso", side_effect=outra_requisicao):
            resposta = self._enviar("/cliente/configuracoes/email", seguir=False, novo_email=self.NOVO,
                                    confirmacao_novo_email=self.NOVO, senha_atual=SENHA)
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(self._usuario().email, CLIENTE)


class ExportacaoTests(_ConfigCase):
    ROTA = "/cliente/configuracoes/exportar-dados"
    PROIBIDAS = {"senha_hash", "geracao_autorizacao", "observacoes", "suspensao_motivo", "cancelamento_motivo",
                 "referencia_formalizacao", "id_usuario_admin_responsavel", "caminho_arquivo", "hash_arquivo",
                 "mensagem_erro", "pontos_positivos", "pontos_de_alerta", "recomendacoes", "id_empresa", "deletado_em"}

    def _exportar(self, caminho=None):
        resposta = self.client.get(caminho or self.ROTA)
        self.assertEqual(resposta.status_code, 200)
        return resposta, json.loads(resposta.get_data(as_text=True))

    def _chaves(self, valor):
        if isinstance(valor, dict):
            for chave, item in valor.items():
                yield chave
                yield from self._chaves(item)
        elif isinstance(valor, list):
            for item in valor:
                yield from self._chaves(item)

    def test_arquivo_para_baixar_sem_cache(self):
        self.client, _ = self._entrar()
        resposta, dados = self._exportar()
        self.assertEqual(resposta.mimetype, "application/json")
        self.assertRegex(resposta.headers["Content-Disposition"],
                         rf'^attachment; filename="nexo_dados_empresa_{self.empresa_id}_20260302\.json"$')
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        self.assertEqual(resposta.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(set(dados), {"exportacao", "titular", "empresa", "contratos", "analises_concluidas", "chamados"})
        self.assertEqual((dados["titular"]["nome"], dados["titular"]["email"]), ("Cliente fictício", CLIENTE))
        self.assertEqual(dados["empresa"]["cnpj"], "00000000000001")

    def test_so_a_empresa_da_sessao_mesmo_com_parametro_forjado(self):
        self.publicar(self.analise_id)
        self.publicar(self.outra_analise_id, texto="Texto sigiloso da outra empresa")
        self.contratar()
        self.contratar(empresa_id=self.outra_empresa_id, valor_mensal="999.00")
        self.client, _ = self._entrar()
        for caminho in (self.ROTA, f"{self.ROTA}?id_empresa={self.outra_empresa_id}&empresa_id={self.outra_empresa_id}"):
            resposta, dados = self._exportar(caminho)
            corpo = resposta.get_data(as_text=True)
            self.assertEqual(dados["empresa"]["cnpj"], "00000000000001")
            self.assertEqual([a["id_analise"] for a in dados["analises_concluidas"]], [self.analise_id])
            self.assertEqual(len(dados["contratos"]), 1)
            self.assertNotIn("Texto sigiloso da outra empresa", corpo)
            self.assertNotIn("00000000000002", corpo)
            self.assertNotIn("999.00", corpo)

    def test_lista_de_campos_nao_vaza_dado_interno(self):
        from models import Contrato, ProdutoCurvaABC
        analise = self.publicar(self.analise_id)
        analise.curva_abc = [ProdutoCurvaABC(posicao_ranking=1, produto_nome="Produto A", faturamento=20, quantidade=2,
                                             percentual_individual=80, percentual_acumulado=80, classe_abc="A")]
        rel = analise.relatorio
        rel.pontos_de_alerta = "Anotação interna da consultoria"
        rel.congelar_publicacao()
        rel.conclusao_estrategica = "Rascunho ainda em edição"
        self.db.session.commit()
        contrato = self.contratar(observacoes="Observação interna do contrato", referencia_formalizacao="Pasta 42")
        self.assertIsInstance(contrato, Contrato)
        self.client, _ = self._entrar()
        resposta, dados = self._exportar()
        corpo = resposta.get_data(as_text=True)
        self.assertFalse(self.PROIBIDAS & set(self._chaves(dados)), self.PROIBIDAS & set(self._chaves(dados)))
        for segredo in ("Rascunho ainda em edição", "Anotação interna", "Observação interna", "Pasta 42",
                        self._usuario().senha_hash):
            self.assertNotIn(segredo, corpo)
        [item] = dados["analises_concluidas"]
        self.assertEqual(item["relatorio"]["conclusao_estrategica"], TEXTO_PRIVADO)
        self.assertEqual(Decimal(item["indicadores"]["faturamento_total"]), Decimal("18500"))
        self.assertEqual(item["curva_abc"][0]["produto"], "Produto A")
        self.assertEqual(Decimal(dados["contratos"][0]["valor_mensal_contratado"]), Decimal("150"))
        self.assertEqual(dados["contratos"][0]["plano"], "BRONZE")

    def test_analise_nao_publicada_nao_entra(self):
        self.client, _ = self._entrar()
        self.assertEqual(self._exportar()[1]["analises_concluidas"], [])
        analise = self.publicar(self.analise_id)
        analise.relatorio.publicado = False
        self.db.session.commit()
        self.assertEqual(self._exportar()[1]["analises_concluidas"], [])

    def test_regime_contratual_so_exporta_periodo_pago(self):
        self.publicar(self.analise_id, date(2026, 1, 1), date(2026, 1, 31))
        self.contratar()
        self.ativar_regras()
        self.client, _ = self._entrar()
        self.assertEqual(self._exportar()[1]["analises_concluidas"], [])
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(len(self._exportar()[1]["analises_concluidas"]), 1)

    def test_prazo_de_exportacao_encerrado_tira_as_analises_do_arquivo(self):
        self.publicar(self.analise_id, date(2026, 1, 1), date(2026, 1, 31))
        self.contratar()
        self.ativar_regras()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.suspender()
        self.relogio(T0 + timedelta(days=15))
        self.client, _ = self._entrar()
        resposta, dados = self._exportar()
        self.assertEqual(dados["analises_concluidas"], [])
        self.assertIn("prazo para exportar", dados["aviso"])
        self.assertEqual(dados["empresa"]["cnpj"], "00000000000001")
        self.assertNotIn(TEXTO_PRIVADO, resposta.get_data(as_text=True))
        self.assertIn("as análises não entram no arquivo", self.client.get(PAGINA_CLIENTE).get_data(as_text=True))


class DocumentosTests(_ConfigCase):
    ROTA = "/admin/configuracoes/documentos"
    TEXTO = "Termo fictício.\n\n<script>alert('xss')</script>\nSegunda linha."

    def setUp(self):
        super().setUp()
        self.admin_http, _ = self._entrar(ADMIN)
        self.client, _ = self._entrar()

    def _publicar(self, tipo="TERMOS_USO", conteudo=TEXTO, versao=0, seguir=True):
        return self._enviar(self.ROTA, cliente=self.admin_http, pagina=PAGINA_ADMIN, seguir=seguir, tipo=tipo,
                            conteudo=conteudo, versao_exibida=str(versao))

    def _aceitar(self, tipo="TERMOS_USO", versao=1):
        return self._enviar("/cliente/configuracoes/aceitar-documento", tipo=tipo, versao=str(versao))

    def test_sem_publicacao_nada_e_inventado(self):
        pagina = self.client.get(PAGINA_CLIENTE).get_data(as_text=True)
        self.assertEqual(pagina.count("A NEXO ainda não publicou este documento."), 2)
        self.assertNotIn("Li e aceito", pagina)
        self.assertIn("foi atualizado", self._aceitar().get_data(as_text=True))
        from models import AceiteDocumento
        self.assertEqual(self._contar(AceiteDocumento), 0)

    def test_publicar_exibe_escapado_ao_cliente_e_registra_autoria(self):
        from models import DocumentoLegal
        self.assertIn("versão 1 publicada", self._publicar().get_data(as_text=True))
        documento = self.db.session.scalar(select(DocumentoLegal))
        self.assertEqual((documento.tipo, documento.versao, documento.id_usuario_admin),
                         ("TERMOS_USO", 1, self.admin_id))
        self.assertEqual(documento.conteudo, self.TEXTO)
        for html in (self.client.get(PAGINA_CLIENTE).get_data(as_text=True),
                     self.admin_http.get(PAGINA_ADMIN).get_data(as_text=True)):
            self.assertNotIn("<script>alert(", html)
            self.assertIn("&lt;script&gt;alert(", html)
        self.assertIn("Nenhum aceite registrado", self.client.get(PAGINA_CLIENTE).get_data(as_text=True))

    def test_aceite_guarda_o_inicial_e_a_versao_nova_pede_novo_aceite(self):
        from models import AceiteDocumento
        self._publicar()
        self.assertIn("Aceite registrado.", self._aceitar().get_data(as_text=True))
        self.assertEqual(self._contar(AceiteDocumento), 1)
        self._aceitar()  # repetido não cria outra linha
        self.assertEqual(self._contar(AceiteDocumento), 1)
        pagina = self.client.get(PAGINA_CLIENTE).get_data(as_text=True)
        self.assertIn("Você já aceitou a versão atual.", pagina)
        self.assertNotIn("Li e aceito a versão 1", pagina)
        self.assertRegex(pagina, r"02/03/2026 às 10:00.*\(versão 1\)")
        self._publicar(conteudo="Texto da segunda versão.", versao=1)
        self.relogio(T0 + timedelta(days=1))
        pagina = self.client.get(PAGINA_CLIENTE).get_data(as_text=True)
        self.assertIn("Li e aceito a versão 2", pagina)
        self.assertIn("Versão atual: 2", pagina)
        self.assertIn("(versão 1)", pagina)
        self.assertIn("Aceite registrado.", self._aceitar(versao=2).get_data(as_text=True))
        pagina = self.client.get(PAGINA_CLIENTE).get_data(as_text=True)
        self.assertIn("(versão 1)", pagina)
        self.assertIn("Versão 2, em", pagina)
        self.assertRegex(pagina, r"Aceite inicial.*02/03/2026 às 10:00")

    def test_aceite_de_versao_antiga_ou_tipo_invalido_e_recusado(self):
        from models import AceiteDocumento
        self._publicar()
        self._publicar(conteudo="Versão dois.", versao=1)
        for tipo, versao in (("TERMOS_USO", 1), ("TERMOS_USO", 99), ("OUTRO", 1), ("POLITICA_PRIVACIDADE", 1)):
            with self.subTest(tipo=tipo, versao=versao):
                self._aceitar(tipo, versao)
                self.assertEqual(self._contar(AceiteDocumento), 0)
        self._enviar("/cliente/configuracoes/aceitar-documento", tipo="TERMOS_USO", versao="2 OR 1=1")
        self.assertEqual(self._contar(AceiteDocumento), 0)

    def test_aceite_de_um_usuario_nao_aparece_para_outro(self):
        outro = self._novo_cliente("outra.pessoa@example.invalid")
        self.assertTrue(outro)
        self._publicar()
        self._aceitar()
        colega, _ = self._entrar("outra.pessoa@example.invalid")
        pagina = colega.get(PAGINA_CLIENTE).get_data(as_text=True)
        self.assertIn("Nenhum aceite registrado", pagina)
        self.assertIn("Li e aceito a versão 1", pagina)

    def test_texto_igual_nao_cria_versao_e_recusas_preservam_o_que_foi_digitado(self):
        from models import DocumentoLegal
        self._publicar()
        self.assertIn("o texto não mudou", self._publicar(conteudo=self.TEXTO.replace("\n", "\r\n") + "  \n", versao=1)
                      .get_data(as_text=True))
        self.assertEqual(self._contar(DocumentoLegal), 1)
        conflito = self._publicar(conteudo="Texto novo em edição", versao=0)
        self.assertEqual(conflito.status_code, 400)
        html = conflito.get_data(as_text=True)
        self.assertIn("Outra versão foi publicada", html)
        self.assertIn("Texto novo em edição", html)
        self.assertEqual(self._contar(DocumentoLegal), 1)
        for dados in (dict(tipo="OUTRO"), dict(conteudo="   \n "), dict(conteudo="x" * 200_001), dict(versao="abc")):
            argumentos = {"versao": 1, **dados}
            with self.subTest(dados=list(dados)):
                self.assertEqual(self._publicar(**argumentos).status_code, 400)
                self.assertEqual(self._contar(DocumentoLegal), 1)

    def test_publicacao_simultanea_nao_derruba_a_pagina(self):
        from models import DocumentoLegal
        self._publicar()
        with patch.object(configuracoes_conta, "documento_vigente", return_value=None):
            resposta = self._publicar(conteudo="Corrida entre administradores", versao=0)
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("publicada ao mesmo tempo", resposta.get_data(as_text=True))
        self.assertEqual(self._contar(DocumentoLegal), 1)


class ExclusaoTests(_ConfigCase):
    ROTA = "/cliente/configuracoes/solicitar-exclusao"

    def setUp(self):
        super().setUp()
        self.client, _ = self._entrar()

    def test_abre_um_unico_chamado_e_nao_apaga_nada(self):
        from models import Analise, ChamadoSuporte, Notificacao
        analises = self._contar(Analise)
        pagina = self._enviar(self.ROTA).get_data(as_text=True)
        self.assertIn("Nenhum dado foi apagado ainda", pagina)
        chamado = self.db.session.scalar(select(ChamadoSuporte))
        self.assertEqual((chamado.id_empresa, chamado.id_usuario_cliente, chamado.status_chamado, chamado.prioridade),
                         (self.empresa_id, self.cliente_id, "ABERTO", "ALTA"))
        self.assertEqual(chamado.assunto, configuracoes_conta.ASSUNTO_EXCLUSAO_DADOS)
        self.assertEqual(len(chamado.mensagens), 1)
        self.assertEqual(self._contar(Notificacao, Notificacao.id_usuario == self.admin_id), 1)
        self.assertEqual(self._contar(Analise), analises)
        self.assertEqual(self._empresa().deletado_em, None)
        self.assertEqual(self.client.get(f"/cliente/tickets/{chamado.id_chamado}").status_code, 200)
        self.assertIn(f"chamado #{chamado.id_chamado}", self.client.get(PAGINA_CLIENTE).get_data(as_text=True))
        self.assertNotIn("Solicitar Exclusão de Dados", self.client.get(PAGINA_CLIENTE).get_data(as_text=True))
        # Enviar de novo (dois cliques, aba antiga) não duplica nem avisa de novo.
        self.assertIn("Já existe uma solicitação", self._enviar(self.ROTA).get_data(as_text=True))
        self.assertEqual(self._contar(ChamadoSuporte), 1)
        self.assertEqual(self._contar(Notificacao, Notificacao.id_usuario == self.admin_id), 1)

    def test_chamado_de_outra_empresa_nao_conta_e_o_resolvido_permite_novo(self):
        from models import ChamadoSuporte
        outro = self._novo_cliente("outra.empresa@example.invalid", self.outra_empresa_id)
        self.db.session.add(ChamadoSuporte(id_empresa=self.outra_empresa_id, id_usuario_cliente=outro,
                                           assunto=configuracoes_conta.ASSUNTO_EXCLUSAO_DADOS,
                                           categoria="DUVIDA_TECNICA"))
        self.db.session.commit()
        self.assertIn("Solicitar Exclusão de Dados", self.client.get(PAGINA_CLIENTE).get_data(as_text=True))
        self._enviar(self.ROTA)
        minha = ChamadoSuporte.id_empresa == self.empresa_id
        self.assertEqual(self._contar(ChamadoSuporte, minha), 1)
        self.db.session.scalar(select(ChamadoSuporte).where(minha)).status_chamado = "RESOLVIDO"
        self.db.session.commit()
        self._enviar(self.ROTA)
        self.assertEqual(self._contar(ChamadoSuporte, minha), 2)


class PreferenciasTests(_ConfigCase):
    ROTA = "/cliente/configuracoes/preferencias"

    def setUp(self):
        super().setUp()
        self.client, _ = self._entrar()
        self.colega = self._novo_cliente("colega@example.invalid")

    def _desligar_alertas(self):
        self._enviar(self.ROTA, lembrete_renovacao="1")

    def _chamado(self):
        from models import ChamadoSuporte
        chamado = ChamadoSuporte(id_empresa=self.empresa_id, id_usuario_cliente=self.cliente_id,
                                 assunto="Chamado fictício", categoria="DUVIDA_TECNICA")
        self.db.session.add(chamado)
        self.db.session.commit()
        return chamado.id_chamado

    def _admin_acao(self, id_chamado, **dados):
        admin, _ = self._entrar(ADMIN)
        caminho = f"/admin/tickets/{id_chamado}"
        return self._enviar(caminho, cliente=admin, pagina=caminho, **dados)

    def test_padrao_ligado_e_a_escolha_persiste(self):
        from models import PreferenciaUsuario
        self.assertEqual(preferencias_usuario.obter_preferencias(self.cliente_id), preferencias_usuario.Preferencias(True, True))
        pagina = self.client.get(PAGINA_CLIENTE).get_data(as_text=True)
        self.assertEqual(len(re.findall(r'role="switch"[^>]*checked', pagina)), 2)
        self.assertEqual(self._contar(PreferenciaUsuario), 0)
        self.assertIn("Preferências salvas.", self._enviar(self.ROTA).get_data(as_text=True))
        self.db.session.expire_all()
        self.assertEqual(preferencias_usuario.obter_preferencias(self.cliente_id), preferencias_usuario.Preferencias(False, False))
        self.assertEqual(preferencias_usuario.obter_preferencias(self.colega), preferencias_usuario.Preferencias(True, True))
        self.assertEqual(re.findall(r'role="switch"[^>]*checked', self.client.get(PAGINA_CLIENTE).get_data(as_text=True)), [])
        self._enviar(self.ROTA, alerta_chamado="1")
        self.db.session.expire_all()
        self.assertEqual(preferencias_usuario.obter_preferencias(self.cliente_id), preferencias_usuario.Preferencias(False, True))

    def test_resposta_e_resolucao_nao_avisam_quem_desligou_no_sininho(self):
        from models import Notificacao
        id_chamado = self._chamado()
        self._desligar_alertas()
        self._admin_acao(id_chamado, acao="responder", mensagem="Resposta fictícia")
        self._admin_acao(id_chamado, acao="alterar_status", status_chamado="RESOLVIDO")
        self.assertEqual(self._contar(Notificacao, Notificacao.id_usuario == self.cliente_id), 0)
        self.assertEqual(self._contar(Notificacao, Notificacao.id_usuario == self.colega), 2)

    def test_outras_notificacoes_da_empresa_nao_mudam(self):
        import notifications
        self._desligar_alertas()
        self.assertEqual(notifications.notificar_clientes_empresa(self.empresa_id, "Sua análise está disponível."), 2)
        self.db.session.commit()

    def test_email_de_chamado_resolvido_respeita_a_escolha(self):
        id_chamado = self._chamado()
        self._desligar_alertas()
        with self._emails() as enviados:
            self._admin_acao(id_chamado, acao="alterar_status", status_chamado="RESOLVIDO")
        destinatarios = [d for m in enviados for d in m.recipients]
        self.assertNotIn(CLIENTE, destinatarios)
        self.assertIn("colega@example.invalid", destinatarios)
        self.assertIn("empresa1@example.invalid", destinatarios)

    def test_contato_da_empresa_igual_ao_de_quem_desligou_tambem_sai(self):
        self._empresa().email_contato = CLIENTE.upper()
        self.db.session.commit()
        id_chamado = self._chamado()
        self._desligar_alertas()
        with self._emails() as enviados:
            self._admin_acao(id_chamado, acao="alterar_status", status_chamado="RESOLVIDO")
        self.assertEqual([d.lower() for m in enviados for d in m.recipients], ["colega@example.invalid"])

    def test_aviso_de_seguranca_nao_tem_escolha(self):
        self._enviar(self.ROTA)
        with self._emails() as enviados:
            self._enviar("/cliente/configuracoes/senha", senha_atual=SENHA, nova_senha=NOVA, confirmacao_nova_senha=NOVA)
        self.assertEqual(enviados[0].recipients, [CLIENTE])


class CopiaDeChamadosTests(_ConfigCase):
    ROTA = "/admin/configuracoes/copia-suporte"
    CAIXA = "caixa.suporte@example.invalid"

    def setUp(self):
        super().setUp()
        self.client, _ = self._entrar()
        self.admin_http, _ = self._entrar(ADMIN)

    def _configurar(self, valor=CAIXA):
        return self._enviar(self.ROTA, cliente=self.admin_http, pagina=PAGINA_ADMIN, email_copia=valor, senha_confirmacao=SENHA)

    def _abrir_chamado(self, assunto="Dúvida fictícia", mensagem="<b>Texto</b> do chamado"):
        return self._enviar("/cliente/tickets", pagina="/cliente/tickets", assunto=assunto,
                            categoria="FINANCEIRO", mensagem=mensagem)

    def test_salva_normalizado_recusa_invalido_e_vazio_desliga(self):
        from models import ConfiguracaoSistema
        # Sem envio ativo a caixa é salva, e a tela diz que nada sai por enquanto.
        self.assertIn("nenhuma cópia sai até ele voltar", self._configurar(" Caixa.Suporte@Example.invalid ").get_data(as_text=True))
        with self._emails():
            self.assertIn("serão copiados", self._configurar(" Caixa.Suporte@Example.invalid ").get_data(as_text=True))
        self.db.session.expire_all()
        linha = self.db.session.get(ConfiguracaoSistema, "suporte_bcc_email")
        self.assertEqual((linha.valor, linha.id_usuario_admin), (self.CAIXA, self.admin_id))
        self.assertIn(f'value="{self.CAIXA}"', self.admin_http.get(PAGINA_ADMIN).get_data(as_text=True))
        for invalido in ("sem-arroba", "a@b.co\r\nBcc: x@example.invalid", "a@b.co, c@d.co"):
            self.assertIn("Informe um e-mail válido", self._configurar(invalido).get_data(as_text=True))
            self.assertEqual(configuracoes_conta.email_copia_suporte(), self.CAIXA)
        self.assertIn("desligada", self._configurar("").get_data(as_text=True))
        self.db.session.expire_all()
        self.assertIsNone(configuracoes_conta.email_copia_suporte())

    def _aviso_aos_admins(self):
        from models import Notificacao
        return self._contar(Notificacao, Notificacao.id_usuario == self.admin_id, Notificacao.texto.like("A caixa que recebe%"))

    def test_so_o_cookie_nao_troca_a_caixa(self):
        for senha in ("", "senha errada"):
            resposta = self._enviar(self.ROTA, cliente=self.admin_http, pagina=PAGINA_ADMIN, email_copia=self.CAIXA, senha_confirmacao=senha)
            self.assertIn("Confirme sua senha", resposta.get_data(as_text=True))
        self.db.session.expire_all()
        self.assertIsNone(configuracoes_conta.email_copia_suporte())
        self.assertEqual(self._aviso_aos_admins(), 0)

    def test_troca_avisa_os_administradores_e_fica_no_log_sem_o_endereco(self):
        with patch.object(self.app.logger, "disabled", False), self.assertLogs(self.app.logger, "INFO") as registro:
            self._configurar()
        saida = "\n".join(registro.output)
        self.assertIn(f"Caixa de cópia de chamados alterada | admin={self.admin_id}", saida)
        self.assertNotIn(self.CAIXA, saida)
        self.assertEqual(self._aviso_aos_admins(), 1)
        self._configurar()  # mesmo endereço: nada mudou, ninguém é avisado
        self.assertEqual(self._aviso_aos_admins(), 1)
        resposta = self._enviar(self.ROTA, cliente=self.admin_http, pagina=PAGINA_ADMIN, email_copia="", senha_confirmacao="senha errada")
        self.assertIn("Confirme sua senha", resposta.get_data(as_text=True))
        self.db.session.expire_all()
        self.assertEqual(configuracoes_conta.email_copia_suporte(), self.CAIXA)
        self.assertIn("desligada", self._configurar("").get_data(as_text=True))
        self.assertEqual(self._aviso_aos_admins(), 2)

    def test_texto_do_cartao_acompanha_o_envio(self):
        self.assertIn("nenhuma cópia sai até ele voltar", self.admin_http.get(PAGINA_ADMIN).get_data(as_text=True))
        with self._emails():
            self.assertIn("Cada novo chamado é enviado a esta caixa", self.admin_http.get(PAGINA_ADMIN).get_data(as_text=True))

    def test_sem_caixa_configurada_nenhum_email_sai(self):
        with self._emails() as enviados:
            self.assertIn("Chamado aberto", self._abrir_chamado().get_data(as_text=True))
        self.assertEqual(enviados, [])

    def test_novo_chamado_vai_em_copia_oculta_com_o_texto_escapado(self):
        self._configurar()
        with self._emails() as enviados:
            self._abrir_chamado(assunto="Linha\r\nBcc: invasor@example.invalid", mensagem="<b>Texto</b> do chamado")
        [mensagem] = enviados
        remetente = emails._remetente(self.app.extensions["mail"].default_sender)[1]
        self.assertEqual(mensagem.recipients, [remetente])
        self.assertEqual(mensagem.bcc, [self.CAIXA])
        self.assertEqual(set(mensagem.send_to), {remetente, self.CAIXA})
        bruto = mensagem.as_string()
        self.assertNotIn(self.CAIXA, bruto)
        self.assertNotIn("invasor", bruto.split("\r\n\r\n", 1)[0])
        self.assertIn("&lt;b&gt;Texto&lt;/b&gt; do chamado", mensagem.html)
        self.assertIn("/admin/tickets/", mensagem.html)
        self.assertIn("Financeiro", mensagem.html)

    def test_solicitacao_de_exclusao_tambem_gera_a_copia_uma_vez(self):
        self._configurar()
        with self._emails() as enviados:
            self._enviar("/cliente/configuracoes/solicitar-exclusao")
            self._enviar("/cliente/configuracoes/solicitar-exclusao")
        self.assertEqual(len(enviados), 1)
        self.assertEqual(enviados[0].bcc, [self.CAIXA])

    def test_caixa_igual_ao_remetente_nao_duplica(self):
        remetente = emails._remetente(self.app.extensions["mail"].default_sender)[1]
        self._configurar(remetente)
        with self._emails() as enviados:
            self._abrir_chamado()
        self.assertEqual([d.lower() for d in enviados[0].recipients], [remetente.lower()])
        self.assertEqual(enviados[0].bcc, [])

    def test_falha_na_copia_nao_desfaz_o_chamado(self):
        from models import ChamadoSuporte
        self._configurar()
        with patch.object(configuracoes_conta, "email_novo_chamado", side_effect=RuntimeError("falha sintética")):
            self.assertIn("Chamado aberto", self._abrir_chamado().get_data(as_text=True))
        self.assertEqual(self._contar(ChamadoSuporte), 1)

    def test_brevo_recebe_a_copia_no_campo_proprio(self):
        self._configurar()
        corpos = []

        class Resposta:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self, *_a):
                return b'{"messageId": "<id@sintetico>"}'

            def geturl(self):
                return self.url

        def abrir(requisicao, timeout=None):
            corpos.append(json.loads(requisicao.data.decode("utf-8")))
            resposta = Resposta()
            resposta.url = requisicao.full_url
            return resposta

        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY="xkeysib-sintetica", EMAIL_ALERTA_ADMIN=False,
                               BREVO_API_URL="https://api.example.invalid/v3/smtp/email")
        with patch.object(emails.urllib.request, "urlopen", side_effect=abrir):
            self._abrir_chamado()
            emails._aguardar_envios_pendentes(5.0)
        self.assertEqual(corpos[0]["bcc"], [{"email": self.CAIXA}])
        self.assertNotIn(self.CAIXA, json.dumps(corpos[0]["to"]))
        self.assertNotIn(self.CAIXA, json.dumps(corpos[0].get("headers", {})))


class LembreteRenovacaoTests(_ConfigCase):
    def _rodar(self, *argumentos, codigo=0):
        resultado = self.app.test_cli_runner().invoke(args=["contratos", "lembretes-renovacao", *argumentos])
        self.assertEqual(resultado.exit_code, codigo, f"{resultado.exception!r} {resultado.output[-300:]}")
        self.assertNotIn("example.invalid", resultado.output)
        return resultado.output

    def _vencendo(self, data_fim=date(2026, 3, 10)):
        self.contratar(data_inicio=date(2026, 1, 1), data_fim=data_fim)
        self.ativar_regras()

    def _lembretes(self, id_usuario=None):
        from models import Notificacao
        return self._contar(Notificacao, Notificacao.id_usuario == (id_usuario or self.cliente_id))

    def _rodada_com(self, ao_enviar, *argumentos, codigo=0):
        """Roda o comando chamando `ao_enviar(numero_do_envio, usuario)` antes de cada e-mail."""
        real, numero = configuracoes_conta.email_lembrete_renovacao, []

        def enviar(usuario, data_fim, link):
            numero.append(usuario.email)
            ao_enviar(len(numero), usuario)
            return real(usuario, data_fim, link)

        with self._emails() as enviados, patch.object(configuracoes_conta, "email_lembrete_renovacao", side_effect=enviar):
            saida = self._rodar("--aplicar", *argumentos, codigo=codigo)
        return saida, [m.recipients[0] for m in enviados]

    def test_rodada_interrompida_deixa_os_demais_pendentes(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")
        terceira = self._novo_cliente("terceira@example.invalid")

        def queda(numero, _usuario):
            if numero == 2:
                raise RuntimeError("queda fictícia")

        _, enviados = self._rodada_com(queda, codigo=1)
        self.assertEqual(enviados, [CLIENTE])
        # Quem estava em andamento fica com o sininho; quem não foi tentado continua pendente.
        self.assertEqual((self._lembretes(), self._lembretes(colega), self._lembretes(terceira)), (1, 1, 0))
        with self._emails() as enviados:
            self.assertIn("pessoas avisadas: 1; e-mails aceitos: 1", self._rodar("--aplicar"))
        self.assertEqual([m.recipients for m in enviados], [["terceira@example.invalid"]])
        self.assertEqual(self._lembretes(terceira), 1)

    def test_contrato_renovado_no_meio_da_rodada_nao_gera_mais_avisos(self):
        from models import Contrato
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")

        def renovar(_numero, _usuario):
            for contrato in self.db.session.scalars(select(Contrato)):
                contrato.data_fim = date(2027, 3, 10)
            self.db.session.commit()

        saida, enviados = self._rodada_com(renovar)
        self.assertEqual(enviados, [CLIENTE])
        self.assertIn("pessoas avisadas: 1", saida)
        self.assertEqual(self._lembretes(colega), 0)

    def test_empresa_na_lixeira_no_meio_da_rodada_nao_gera_mais_avisos(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")

        def lixeira(_numero, _usuario):
            self._empresa().deletado_em = T0
            self.db.session.commit()

        _, enviados = self._rodada_com(lixeira)
        self.assertEqual(enviados, [CLIENTE])
        self.assertEqual(self._lembretes(colega), 0)

    def _transporte_que_recusa(self, resposta):
        def transporte(mensagem):
            if mensagem.recipients == ["colega@example.invalid"]:
                raise smtplib.SMTPRecipientsRefused({"colega@example.invalid": resposta})
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None, EMAIL_ALERTA_ADMIN=False)
        return patch.object(emails.mail, "send", side_effect=transporte)

    def test_caixa_inexistente_fica_com_o_sininho_e_nao_repete(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")
        with self._transporte_que_recusa((550, b"5.1.1 Mailbox not found")):
            saida = self._rodar("--aplicar", codigo=1)
        self.assertIn("Endereços recusados em definitivo: 1", saida)
        self.assertNotIn("E-mails não aceitos", saida)
        self.assertEqual(self._lembretes(colega), 1)
        with self._transporte_que_recusa((550, b"5.1.1 Mailbox not found")) as envio:
            self.assertIn("pessoas avisadas: 0", self._rodar("--aplicar"))
        self.assertEqual(envio.call_count, 0)

    def test_endereco_invalido_fica_com_o_sininho(self):
        self._vencendo()
        usuario = self._usuario(self._novo_cliente("colega@example.invalid"))
        usuario.email = "joão@example.invalid"
        self.db.session.commit()
        with self._emails():
            self.assertIn("Endereços recusados em definitivo: 1", self._rodar("--aplicar", codigo=1))
        self.assertEqual(self._lembretes(usuario.id_usuario), 1)

    def test_recusa_de_politica_ou_temporaria_continua_pendente(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")
        for resposta in ((550, b"5.7.1 Relaying denied"), (451, b"4.3.0 Try later"), (550, b"Rejected")):
            with self.subTest(resposta=resposta):
                with self._transporte_que_recusa(resposta):
                    saida = self._rodar("--aplicar", codigo=1)
                self.assertIn("A próxima execução tenta de novo", saida)
                self.assertEqual(self._lembretes(colega), 0)

    def test_botao_mostra_o_que_foi_recusado(self):
        self._vencendo()
        self._novo_cliente("colega@example.invalid")
        admin, _ = self._entrar(ADMIN)
        with self._transporte_que_recusa((550, b"5.1.1 Mailbox not found")):
            pagina = self._enviar("/admin/contratos/lembretes-renovacao", cliente=admin, pagina="/admin/contratos").get_data(as_text=True)
        self.assertIn("1 endereço(s) recusado(s) em definitivo", pagina)
        self.assertIn("2 pessoa(s) avisada(s)", pagina)

    def test_status_do_remetente_nao_consome_o_lembrete(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")
        for resposta in ((550, b"5.1.0 <r@x.invalid>: Sender address rejected: User unknown"),
                         (553, b"5.1.8 <r@x.invalid>: Sender address rejected: Domain not found"),
                         (553, b"5.1.7 Bad sender syntax")):
            with self.subTest(resposta=resposta):
                with self._transporte_que_recusa(resposta):
                    saida = self._rodar("--aplicar", codigo=1)
                self.assertNotIn("recusados em definitivo", saida)
                self.assertEqual(self._lembretes(colega), 0)

    def test_status_do_destinatario_continua_definitivo_e_o_comando_diz_quem(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")
        with self._transporte_que_recusa((550, b"5.1.10 Null MX")):
            saida = self._rodar("--aplicar", codigo=1)
        self.assertIn("recusados em definitivo: 1", saida)
        self.assertIn(f"empresa {self.empresa_id} usuário {colega}", saida)
        self.assertEqual(self._lembretes(colega), 1)

    def test_preferencia_desligada_no_meio_da_rodada_e_respeitada(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")
        http, _ = self._entrar("colega@example.invalid")

        def desligar(_numero, _usuario):
            self._enviar("/cliente/configuracoes/preferencias", cliente=http, alerta_chamado="1")

        _, enviados = self._rodada_com(desligar)
        self.assertEqual(enviados, [CLIENTE])
        self.assertEqual(self._lembretes(colega), 0)

    def test_regime_desativado_no_meio_da_rodada_para_os_avisos(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")

        def desativar(_numero, _usuario):
            contract_admin.desativar_regras_contratuais(self.db.session, self.empresa_id, justificativa="Fictícia",
                                                         responsavel=self.admin, instante=self.agora)
            self.db.session.commit()

        _, enviados = self._rodada_com(desativar)
        self.assertEqual(enviados, [CLIENTE])
        self.assertEqual(self._lembretes(colega), 0)

    def test_renovacao_real_no_meio_da_rodada_para_os_avisos(self):
        from models import Contrato
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")

        def renovar(_numero, _usuario):
            atual = self.db.session.scalar(select(Contrato).where(Contrato.situacao == "ATIVO"))
            contract_admin.renovar_contrato(
                self.db.session, self.empresa_id, atual.id_contrato, atual.versao,
                self.dados(data_inicio=date(2026, 3, 2), data_fim=date(2027, 3, 1), valor_mensal=Decimal("150.00")),
                responsavel=self.admin, instante=self.agora)
            self.db.session.commit()

        saida, enviados = self._rodada_com(renovar)
        self.assertEqual(enviados, [CLIENTE])
        self.assertIn("pessoas avisadas: 1", saida)
        self.assertEqual(self._lembretes(colega), 0)

    def test_conta_apagada_no_meio_da_rodada_nao_derruba_o_resto(self):
        from models import Usuario
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")
        terceira = self._novo_cliente("terceira@example.invalid")

        def apagar(numero, _usuario):
            if numero == 1:
                self.db.session.delete(self.db.session.get(Usuario, colega))
                self.db.session.commit()

        _, enviados = self._rodada_com(apagar)
        self.assertEqual(enviados, [CLIENTE, "terceira@example.invalid"])
        self.assertEqual(self._lembretes(terceira), 1)

    def test_rodada_que_comeca_durante_o_envio_de_outra_nao_repete(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")

        def outra_rodada(numero, _usuario):
            if numero == 1:
                configuracoes_conta.enviar_lembretes_renovacao(aplicar=True)

        _, enviados = self._rodada_com(outra_rodada)
        self.assertEqual(sorted(enviados), sorted([CLIENTE, "colega@example.invalid"]))
        self.assertEqual((self._lembretes(), self._lembretes(colega)), (1, 1))

    def test_tela_diz_quem_foi_recusado_e_o_que_nao_foi_aceito(self):
        self._vencendo()
        self._novo_cliente("colega@example.invalid", nome="Colega Recusada")
        admin, _ = self._entrar(ADMIN)
        with self._transporte_que_recusa((550, b"5.1.1 Mailbox not found")):
            pagina = self._enviar("/admin/contratos/lembretes-renovacao", cliente=admin, pagina="/admin/contratos").get_data(as_text=True)
        self.assertIn("Colega Recusada", pagina)
        self._novo_cliente("colega2@example.invalid")
        with self._emails(), patch.object(configuracoes_conta, "email_lembrete_renovacao",
                                          return_value=SimpleNamespace(aceito=False, categoria="smtp_conexao", codigo_provedor=None)):
            pagina = self._enviar("/admin/contratos/lembretes-renovacao", cliente=admin, pagina="/admin/contratos").get_data(as_text=True)
        self.assertIn("1 e-mail(s) não foram aceitos: a próxima execução tenta de novo", pagina)

    def test_nomes_longos_nao_estouram_o_cookie_e_falha_ao_le_los_nao_vira_erro(self):
        self._vencendo()
        self._novo_cliente("colega@example.invalid", nome="漢" * 120)
        admin, _ = self._entrar(ADMIN)
        with self._transporte_que_recusa((550, b"5.1.1 Mailbox not found")):
            pagina = self._enviar("/admin/contratos/lembretes-renovacao", cliente=admin, pagina="/admin/contratos").get_data(as_text=True)
        self.assertIn("漢" * 39 + "…", pagina)
        self.assertNotIn("漢" * 40, pagina)

    def test_falha_ao_ler_os_nomes_nao_esconde_o_resumo_da_rodada(self):
        self._vencendo()
        self._novo_cliente("colega@example.invalid")
        admin, _ = self._entrar(ADMIN)
        real = self.db.session.scalars

        def falha_nos_nomes(consulta, *argumentos, **nomeados):
            if "usuario.nome" in str(consulta):
                raise OperationalError("SELECT", {}, Exception("queda fictícia"))
            return real(consulta, *argumentos, **nomeados)

        csrf = self._csrf(admin, "/admin/contratos")
        with self._transporte_que_recusa((550, b"5.1.1 Mailbox not found")), \
                patch.object(self.db.session, "scalars", side_effect=falha_nos_nomes):
            resposta = admin.post("/admin/contratos/lembretes-renovacao", data={"csrf_token": csrf})
        self.assertEqual(resposta.status_code, 302)
        pagina = admin.get(resposta.headers["Location"]).get_data(as_text=True)
        self.assertIn("1 endereço(s) recusado(s) em definitivo", pagina)
        self.assertIn("em Empresas, Detalhes.", pagina)

    def test_empresa_suspensa_no_meio_da_rodada_para_os_avisos(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")

        def suspender(_numero, _usuario):
            self._empresa().status_conta = "SUSPENSA"
            self.db.session.commit()

        _, enviados = self._rodada_com(suspender)
        self.assertEqual(enviados, [CLIENTE])
        self.assertEqual(self._lembretes(colega), 0)

    def test_selo_do_sininho_nao_chama_o_lembrete_de_aberto(self):
        from notifications import classificar_notificacao
        self.assertEqual(classificar_notificacao("Seu contrato com a NEXO vence em 10/03/2026. Fale com a equipe para renovar.")["rotulo"], "Contrato")
        self.assertEqual(classificar_notificacao("A caixa que recebe a cópia dos novos chamados foi alterada. Confira em Configurações.")["rotulo"], "Aviso")

    def test_simulacao_nao_grava_nem_envia(self):
        self._vencendo()
        with self._emails() as enviados:
            saida = self._rodar()
        self.assertIn("SIMULAÇÃO", saida)
        self.assertIn(f"empresa {self.empresa_id}", saida)
        self.assertIn("1 pessoa(s) a avisar", saida)
        self.assertEqual((self._lembretes(), enviados), (0, []))

    def test_aplicacao_avisa_uma_vez_e_respeita_quem_desligou(self):
        self._vencendo()
        colega = self._novo_cliente("colega@example.invalid")
        pendente = self._novo_cliente("pendente@example.invalid")
        self._usuario(pendente).primeiro_acesso = True
        self.db.session.commit()
        cliente, _ = self._entrar("colega@example.invalid")
        self._enviar("/cliente/configuracoes/preferencias", cliente=cliente, alerta_chamado="1")
        with self._emails() as enviados:
            saida = self._rodar("--aplicar")
        self.assertIn("pessoas avisadas: 1; e-mails aceitos: 1", saida)
        self.assertEqual([m.recipients for m in enviados], [[CLIENTE]])
        self.assertIn("10/03/2026", enviados[0].subject)
        self.assertEqual((self._lembretes(), self._lembretes(colega), self._lembretes(pendente)), (1, 0, 0))
        with self._emails() as enviados:
            self.assertIn("pessoas avisadas: 0", self._rodar("--aplicar"))
        self.assertEqual((self._lembretes(), enviados), (1, []))
        self._enviar("/cliente/configuracoes/preferencias", cliente=cliente, alerta_chamado="1", lembrete_renovacao="1")
        with self._emails():
            self.assertIn("pessoas avisadas: 1", self._rodar("--aplicar"))
        self.assertEqual(self._lembretes(colega), 1)

    def test_fora_da_janela_ou_do_regime_ninguem_e_avisado(self):
        self._vencendo(date(2026, 3, 17))  # 15 dias: fora da janela de 14
        with self._emails():
            self.assertIn("janela de 14 dias: 0", self._rodar("--aplicar"))
            self.contratar(empresa_id=self.outra_empresa_id, data_fim=date(2026, 3, 10))  # regime legado
            self.assertIn("janela de 14 dias: 0", self._rodar("--aplicar"))

    def test_contrato_suspenso_nao_recebe_lembrete(self):
        self._vencendo()
        self.suspender()
        with self._emails():
            self.assertIn("janela de 14 dias: 0", self._rodar("--aplicar"))

    def test_faixa_da_visao_geral_acompanha_o_prazo_e_a_escolha(self):
        self._vencendo()
        cliente, _ = self._entrar()
        faixa = "Seu contrato vence em 8 dias."
        self.assertIn(faixa, cliente.get("/cliente/dashboard").get_data(as_text=True))
        self._enviar("/cliente/configuracoes/preferencias", cliente=cliente)
        self.assertNotIn(faixa, cliente.get("/cliente/dashboard").get_data(as_text=True))
        self._enviar("/cliente/configuracoes/preferencias", cliente=cliente, lembrete_renovacao="1")
        self.relogio(T0 + timedelta(days=8))
        self.assertIn("Seu contrato vence hoje.", cliente.get("/cliente/dashboard").get_data(as_text=True))
        self.relogio(T0 - timedelta(days=7))
        self.assertNotIn("Seu contrato vence", cliente.get("/cliente/dashboard").get_data(as_text=True))

    def test_sem_regime_contratual_nao_ha_faixa(self):
        self.contratar(data_fim=date(2026, 3, 10))
        cliente, _ = self._entrar()
        self.assertNotIn("Seu contrato vence", cliente.get("/cliente/dashboard").get_data(as_text=True))


class StatusSMTPTests(_ConfigCase):
    SENHA_SMTP = "SENTINELA-SENHA-SMTP"

    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(emails, "_CONEXAO_SMTP", None))
        self.client, _ = self._entrar(ADMIN)
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None, MAIL_SERVER="mail.exemplo.invalid", MAIL_PORT=587,
                               MAIL_USE_TLS=True, MAIL_USE_SSL=False, MAIL_USERNAME="conta@example.invalid",
                               MAIL_PASSWORD=self.SENHA_SMTP, MAIL_DEFAULT_SENDER="conta@example.invalid")

    def _pagina(self):
        html = self.client.get(PAGINA_ADMIN).get_data(as_text=True)
        self.assertNotIn(self.SENHA_SMTP, html)
        self.assertIn('href="/admin/diagnostico-email"', html)
        return html

    def test_cada_estado_da_conexao(self):
        html = self._pagina()
        self.assertIn('b-muted">NÃO VERIFICADA', html)
        self.assertIn("mail.exemplo.invalid:587", html)
        self.assertIn('action="/admin/diagnostico-email/conexao"', html)
        emails._anotar_conexao_smtp(True, "verificacao")
        self.assertIn('b-green">CONECTADO', self._pagina())
        emails._anotar_conexao_smtp(False, "envio", categoria="smtp_autenticacao")
        html = self._pagina()
        self.assertIn('b-red">FALHA', html)
        self.assertIn(emails.CATEGORIAS["smtp_autenticacao"].split("(")[0], html)
        self.app.config["MAIL_DEFAULT_SENDER"] = "NEXO"
        self.assertIn('b-red">BLOQUEADO', self._pagina())
        self.app.config["MAIL_ATIVO"] = False
        self.assertIn('b-red">DESATIVADO', self._pagina())
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY="xkeysib-sintetica", MAIL_DEFAULT_SENDER="conta@example.invalid")
        html = self._pagina()
        self.assertIn('b-muted">NÃO USADO', html)
        self.assertNotIn('action="/admin/diagnostico-email/conexao"', html)


class ExclusaoPermanenteTests(_ConfigCase):
    def test_apaga_preferencias_e_aceites_e_conserva_os_documentos(self):
        from models import AceiteDocumento, DocumentoLegal, PreferenciaUsuario
        admin, _ = self._entrar(ADMIN)
        self._enviar("/admin/configuracoes/documentos", cliente=admin, pagina=PAGINA_ADMIN, tipo="TERMOS_USO",
                     conteudo="Termo fictício", versao_exibida="0")
        cliente, _ = self._entrar()
        self._enviar("/cliente/configuracoes/aceitar-documento", cliente=cliente, tipo="TERMOS_USO", versao="1")
        self._enviar("/cliente/configuracoes/preferencias", cliente=cliente)
        self.assertEqual((self._contar(AceiteDocumento), self._contar(PreferenciaUsuario)), (1, 1))
        csrf = self._csrf(admin, "/admin/lixeira")
        self.assertEqual(admin.post(f"/admin/empresa/excluir/{self.empresa_id}", data={"csrf_token": csrf}).status_code, 302)
        resposta = admin.post(f"/admin/empresa/excluir-permanente/{self.empresa_id}",
                              data={"csrf_token": csrf, "senha_confirmacao": SENHA}, follow_redirects=True)
        self.assertIn("excluídos permanentemente", resposta.get_data(as_text=True))
        self.assertIsNone(self._empresa())
        self.assertEqual((self._contar(AceiteDocumento), self._contar(PreferenciaUsuario), self._contar(DocumentoLegal)),
                         (0, 0, 1))


class AvisoDeAnaliseExcluidaTests(_ConfigCase):
    def _analise_com_aviso(self):
        from models import Notificacao
        analise = self.nova_analise(date(2026, 1, 1), date(2026, 1, 31))
        link = f"/admin/analise/{analise.id_analise}/upload"
        self.db.session.add_all([
            Notificacao(id_usuario=self.admin_id, texto="Nova planilha de Vendas enviada por Empresa fictícia.", link_destino=link),
            Notificacao(id_usuario=self.admin_id, texto="Aviso de outra análise.", link_destino=f"{link}x"),
        ])
        self.db.session.commit()
        return analise.id_analise

    def _textos(self):
        from models import Notificacao
        self.db.session.expire_all()
        return list(self.db.session.scalars(select(Notificacao.texto)))

    def test_excluir_a_analise_leva_junto_o_aviso_que_cita_a_empresa(self):
        from blueprints.admin import _excluir_analise
        from models import Analise
        id_analise = self._analise_com_aviso()
        ok, _ = _excluir_analise(self.db.session.get(Analise, id_analise))
        self.assertTrue(ok)
        self.assertEqual(self._textos(), ["Aviso de outra análise."])

    def test_falha_no_banco_preserva_o_aviso_e_a_analise(self):
        from blueprints.admin import _excluir_analise
        from models import Analise
        id_analise = self._analise_com_aviso()
        with patch.object(self.db.session, "commit", side_effect=IntegrityError("x", {}, Exception("x"))):
            ok, _ = _excluir_analise(self.db.session.get(Analise, id_analise))
        self.assertFalse(ok)
        self.assertEqual(len(self._textos()), 2)
        self.assertIsNotNone(self.db.session.get(Analise, id_analise))


class EsquemaAusenteTests(_ConfigCase):
    """Código implantado antes da migração c7d41e9a2b60: o portal continua inteiro."""

    def _downgrade(self):
        from flask_migrate import downgrade
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            downgrade(directory=str(ROOT / "migrations"), revision="b6935e8de9d4")
        preferencias_usuario._esquema_por_engine.clear()

    def test_sem_as_tabelas_o_portal_funciona_e_os_ajustes_ficam_indisponiveis(self):
        from models import ChamadoSuporte
        self._downgrade()
        self.assertFalse(preferencias_usuario.esquema_configuracoes_disponivel())
        cliente, _ = self._entrar()
        admin, _ = self._entrar(ADMIN)
        pagina = cliente.get(PAGINA_CLIENTE)
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("Os documentos ainda não estão disponíveis.", pagina.get_data(as_text=True))
        self.assertNotIn("c7d41e9a2b60", pagina.get_data(as_text=True))
        pagina = admin.get(PAGINA_ADMIN)
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("c7d41e9a2b60", pagina.get_data(as_text=True))
        # O que não depende das tabelas novas segue funcionando.
        self.assertIn("Dados atualizados.", self._enviar("/cliente/configuracoes/perfil", cliente=cliente, nome="Ana Maria",
                                                         telefone="").get_data(as_text=True))
        self.assertEqual(cliente.get("/cliente/configuracoes/exportar-dados").status_code, 200)
        self.assertIn("Solicitação registrada", self._enviar("/cliente/configuracoes/solicitar-exclusao", cliente=cliente)
                      .get_data(as_text=True))
        self.assertEqual(self._contar(ChamadoSuporte), 1)
        self.assertEqual(cliente.get("/cliente/dashboard").status_code, 200)
        # O que depende avisa, sem erro 500.
        for rota, http, pagina_origem, dados in (
                ("/cliente/configuracoes/preferencias", cliente, PAGINA_CLIENTE, {}),
                ("/cliente/configuracoes/aceitar-documento", cliente, PAGINA_CLIENTE, {"tipo": "TERMOS_USO", "versao": "1"}),
                ("/admin/configuracoes/copia-suporte", admin, PAGINA_ADMIN, {"email_copia": "a@example.invalid", "senha_confirmacao": SENHA})):
            with self.subTest(rota=rota):
                resposta = self._enviar(rota, cliente=http, pagina=pagina_origem, **dados)
                self.assertEqual(resposta.status_code, 200)
                self.assertIn("ainda não está disponível", resposta.get_data(as_text=True))
        resposta = self._enviar("/admin/configuracoes/documentos", cliente=admin, pagina=PAGINA_ADMIN, tipo="TERMOS_USO",
                                conteudo="Texto", versao_exibida="0")
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("ainda não está disponível", resposta.get_data(as_text=True))
        # Troca de senha e chamados não dependem das tabelas novas.
        self.assertIn("Chamado aberto", self._enviar("/cliente/tickets", cliente=cliente, pagina="/cliente/tickets",
                                                     assunto="Assunto", categoria="FINANCEIRO", mensagem="Mensagem")
                      .get_data(as_text=True))
        with patch.object(auth, "email_senha_alterada"):
            self.assertIn("Senha alterada.", self._enviar("/admin/configuracoes/senha", cliente=admin, pagina=PAGINA_ADMIN,
                                                          senha_atual=SENHA, nova_senha=NOVA,
                                                          confirmacao_nova_senha=NOVA).get_data(as_text=True))

    def test_painel_de_contratos_e_lembrete_funcionam_sem_as_tabelas(self):
        self.contratar(data_inicio=date(2026, 1, 1), data_fim=date(2026, 3, 10))
        self.ativar_regras()
        self._downgrade()
        admin, _ = self._entrar(ADMIN)
        self.assertIn("1 pessoa(s) ainda não receberam o lembrete", admin.get("/admin/contratos").get_data(as_text=True))
        with self._emails() as enviados:
            resposta = self._enviar("/admin/contratos/lembretes-renovacao", cliente=admin, pagina="/admin/contratos")
        self.assertIn("1 pessoa(s) avisada(s)", resposta.get_data(as_text=True))
        self.assertEqual([m.recipients for m in enviados], [[CLIENTE]])

    def test_migracao_aplicada_passa_a_valer_sem_reiniciar(self):
        self._downgrade()
        self.assertFalse(preferencias_usuario.esquema_configuracoes_disponivel())
        from flask_migrate import upgrade
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            upgrade(directory=str(ROOT / "migrations"), revision="c7d41e9a2b60")
        with patch.object(preferencias_usuario.time, "monotonic",
                          return_value=preferencias_usuario.time.monotonic() + 31):
            self.assertTrue(preferencias_usuario.esquema_configuracoes_disponivel())

    def test_esquema_parcial_fica_indisponivel_sem_erro(self):
        from models import ConfiguracaoSistema
        self._downgrade()
        ConfiguracaoSistema.__table__.create(self.db.engine)
        # O fileConfig da migração do setUp desliga os loggers já criados.
        with patch.object(preferencias_usuario.logger, "disabled", False), \
                self.assertLogs(preferencias_usuario.logger, level="WARNING"):
            self.assertFalse(preferencias_usuario.esquema_configuracoes_disponivel())

    def test_downgrade_recusa_apagar_dados(self):
        from flask_migrate import downgrade
        from models import ConfiguracaoSistema
        self.db.session.add(ConfiguracaoSistema(chave="suporte_bcc_email", valor="a@example.invalid", atualizado_em=T0,
                                                id_usuario_admin=self.admin_id))
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"), self.assertRaises(SystemExit):
            downgrade(directory=str(ROOT / "migrations"), revision="b6935e8de9d4")
        self.assertEqual(self._contar(ConfiguracaoSistema), 1)

    def test_contrato_antes_da_propria_migracao_nao_e_exigido_na_exportacao(self):
        with patch.object(configuracoes_conta, "esquema_contratual_disponivel", return_value=False):
            self.assertEqual(configuracoes_conta._contratos_exportados(self.empresa_id), [])
        self.assertTrue(contract_policy.esquema_contratual_disponivel())


class CorrecoesDaRevisaoTests(_ConfigCase):
    """Regressões dos achados confirmados pela revisão adversarial do módulo."""

    def test_admin_com_a_flag_antiga_nao_troca_a_senha_sem_informar_a_atual(self):
        self._usuario(self.admin_id).primeiro_acesso = True
        self.db.session.commit()
        admin, _ = self._entrar(ADMIN)
        pagina = admin.get("/auth/primeiro-acesso")
        self.assertEqual((pagina.status_code, pagina.headers["Location"]), (302, "/admin/dashboard"))
        resposta = admin.post("/auth/primeiro-acesso", data={
            "nova_senha": NOVA, "confirma_senha": NOVA, "csrf_token": self._csrf(admin, PAGINA_ADMIN),
        })
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(self._usuario(self.admin_id).check_senha(SENHA))

    def test_sessao_vencida_nao_impede_a_troca_de_senha_nem_de_email(self):
        self.app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
        self._entrar()  # navegador fechado sem sair: a linha fica sem revogação
        self.relogio(T0 + timedelta(hours=5))
        cliente, _ = self._entrar()
        self.relogio(T0 + timedelta(hours=8, minutes=1))
        pagina = self._enviar("/cliente/configuracoes/senha", cliente=cliente, senha_atual=SENHA, nova_senha=NOVA,
                              confirmacao_nova_senha=NOVA).get_data(as_text=True)
        self.assertIn("Senha alterada.", pagina)
        self.assertTrue(self._usuario().check_senha(NOVA))
        self._entrar(senha=NOVA)
        self.relogio(T0 + timedelta(hours=16, minutes=2))
        cliente, _ = self._entrar(senha=NOVA)
        novo = "novo.endereco@example.invalid"
        self.assertIn("E-mail de acesso alterado", self._enviar(
            "/cliente/configuracoes/email", cliente=cliente, novo_email=novo, confirmacao_novo_email=novo,
            senha_atual=NOVA).get_data(as_text=True))

    def test_confirmacao_do_novo_email_precisa_ser_igual(self):
        cliente, _ = self._entrar()
        pagina = self._enviar("/cliente/configuracoes/email", cliente=cliente, novo_email="certo@example.invalid",
                              confirmacao_novo_email="cetro@example.invalid", senha_atual=SENHA)
        self.assertIn("A confirmação não é igual", pagina.get_data(as_text=True))
        self.assertEqual(self._usuario().email, CLIENTE)

    def test_senha_de_digitos_sobrescritos_vira_mensagem_e_nao_erro_500(self):
        self.assertIsNone(politica_senha.validar_nova_senha("²³⁴²³⁴²³⁴²³⁴", "²³⁴²³⁴²³⁴²³⁴"))
        self.assertIn("comum ou previsível", politica_senha.validar_nova_senha("123456789012", "123456789012"))

    def test_email_com_caractere_invisivel_ou_acento_na_caixa_e_recusado(self):
        for invalido in ("a\u200bb@example.invalid", "a\u202eb@example.invalid", "a\x01b@example.invalid", "a\x7fb@example.invalid",
                         "joão@example.invalid", "a@ｃｏｒｐ.invalid"):
            self.assertFalse(emails.email_valido(invalido), repr(invalido))
        self.assertTrue(emails.email_valido("nome.sobrenome+tag@xn--exmplo-cva.com"))
        cliente, _ = self._entrar()
        novo = "a\u200bb@example.invalid"
        self.assertIn("Informe um e-mail válido", self._enviar(
            "/cliente/configuracoes/email", cliente=cliente, novo_email=novo, confirmacao_novo_email=novo,
            senha_atual=SENHA).get_data(as_text=True))
        admin, _ = self._entrar(ADMIN)
        self.assertIn("Informe um e-mail válido", self._enviar(
            "/admin/configuracoes/copia-suporte", cliente=admin, pagina=PAGINA_ADMIN,
            email_copia="joão@example.invalid", senha_confirmacao=SENHA).get_data(as_text=True))

    def test_aviso_de_senha_diz_que_a_sessao_da_troca_continua_ativa(self):
        cliente, _ = self._entrar()
        with self._emails() as enviados:
            self._enviar("/cliente/configuracoes/senha", cliente=cliente, senha_atual=SENHA, nova_senha=NOVA,
                         confirmacao_nova_senha=NOVA)
        self.assertIn("continua ativa", enviados[0].html)
        self.assertNotIn("abertas já foram encerradas", enviados[0].html)

    def _lixeira_e_exclusao(self, admin):
        csrf = self._csrf(admin, "/admin/lixeira")
        self.assertEqual(admin.post(f"/admin/empresa/excluir/{self.empresa_id}", data={"csrf_token": csrf}).status_code, 302)
        return admin.post(f"/admin/empresa/excluir-permanente/{self.empresa_id}",
                          data={"csrf_token": csrf, "senha_confirmacao": SENHA}, follow_redirects=True)

    def test_exclusao_permanente_com_relatorio_conclui_e_nao_deixa_rastro_no_sininho(self):
        from models import Analise, ChamadoSuporte, Notificacao
        self.publicar(self.analise_id)
        self.publicar(self.outra_analise_id, texto="Texto da outra empresa")
        cliente, _ = self._entrar()
        self._enviar("/cliente/tickets", cliente=cliente, pagina="/cliente/tickets", assunto="Dados de Pessoa Fictícia",
                     categoria="FINANCEIRO", mensagem="Mensagem")
        outro = self._novo_cliente("outra.empresa@example.invalid", self.outra_empresa_id)
        outra, _ = self._entrar("outra.empresa@example.invalid")
        self.assertTrue(outro)
        self._enviar("/cliente/tickets", cliente=outra, pagina="/cliente/tickets", assunto="Chamado da outra empresa",
                     categoria="FINANCEIRO", mensagem="Mensagem")
        admin, _ = self._entrar(ADMIN)
        self.assertIn("excluídos permanentemente", self._lixeira_e_exclusao(admin).get_data(as_text=True))
        self.assertIsNone(self._empresa())
        self.assertEqual(self._contar(Analise, Analise.id_empresa == self.empresa_id), 0)
        self.assertEqual(self._contar(ChamadoSuporte, ChamadoSuporte.id_empresa == self.empresa_id), 0)
        textos = list(self.db.session.scalars(select(Notificacao.texto)))
        self.assertFalse([t for t in textos if "Pessoa Fictícia" in t or "Teste 1" in t], textos)
        self.assertTrue([t for t in textos if "Chamado da outra empresa" in t], textos)

    def test_exportacao_traz_aceites_preferencias_e_os_chamados_de_quem_exporta(self):
        admin, _ = self._entrar(ADMIN)
        self._enviar("/admin/configuracoes/documentos", cliente=admin, pagina=PAGINA_ADMIN, tipo="TERMOS_USO",
                     conteudo="Termo fictício", versao_exibida="0")
        cliente, _ = self._entrar()
        self._enviar("/cliente/configuracoes/aceitar-documento", cliente=cliente, tipo="TERMOS_USO", versao="1")
        self._enviar("/cliente/configuracoes/preferencias", cliente=cliente, lembrete_renovacao="1")
        self._enviar("/cliente/tickets", cliente=cliente, pagina="/cliente/tickets", assunto="Meu chamado",
                     categoria="FINANCEIRO", mensagem="Texto meu")
        colega = self._novo_cliente("colega@example.invalid")
        self.assertTrue(colega)
        outro, _ = self._entrar("colega@example.invalid")
        self._enviar("/cliente/tickets", cliente=outro, pagina="/cliente/tickets", assunto="Chamado do colega",
                     categoria="FINANCEIRO", mensagem="Texto do colega")
        dados = json.loads(cliente.get("/cliente/configuracoes/exportar-dados").get_data(as_text=True))
        self.assertEqual(dados["titular"]["preferencias"], {"lembrete_renovacao": True, "alerta_chamado": False})
        self.assertEqual([(a["documento"], a["versao"]) for a in dados["titular"]["aceites"]], [("TERMOS_USO", 1)])
        self.assertEqual([c["assunto"] for c in dados["chamados"]], ["Meu chamado"])
        self.assertEqual(dados["chamados"][0]["mensagens"][0]["autor"], "cliente")
        self.assertEqual(dados["chamados"][0]["mensagens"][0]["mensagem"], "Texto meu")

    def test_nome_do_arquivo_usa_a_data_de_brasilia(self):
        from datetime import datetime, timezone
        self.relogio(datetime(2026, 3, 3, 1, 30, tzinfo=timezone.utc))  # 22:30 de 02/03 em Brasília
        cliente, _ = self._entrar()
        self.assertIn("_20260302.json", cliente.get("/cliente/configuracoes/exportar-dados").headers["Content-Disposition"])

    def test_chamado_comum_com_o_mesmo_assunto_nao_conta_como_pedido_de_exclusao(self):
        from models import ChamadoSuporte
        cliente, _ = self._entrar()
        self._enviar("/cliente/tickets", cliente=cliente, pagina="/cliente/tickets", categoria="FINANCEIRO",
                     assunto=configuracoes_conta.ASSUNTO_EXCLUSAO_DADOS, mensagem="Boleto vencido")
        self.assertIn("Solicitar Exclusão de Dados", cliente.get(PAGINA_CLIENTE).get_data(as_text=True))
        self.assertIn("Solicitação registrada", self._enviar("/cliente/configuracoes/solicitar-exclusao", cliente=cliente)
                      .get_data(as_text=True))
        self.assertEqual(self._contar(ChamadoSuporte, ChamadoSuporte.prioridade == "ALTA"), 1)

    def test_pedido_de_exclusao_aparece_com_prioridade_alta_para_a_equipe(self):
        from models import ChamadoSuporte
        cliente, _ = self._entrar()
        self._enviar("/cliente/configuracoes/solicitar-exclusao", cliente=cliente)
        admin, _ = self._entrar(ADMIN)
        id_chamado = self.db.session.scalar(select(ChamadoSuporte.id_chamado))
        for caminho in ("/admin/suporte", f"/admin/tickets/{id_chamado}"):
            self.assertIn("Prioridade alta", admin.get(caminho).get_data(as_text=True), caminho)

    def test_telefone_antigo_fora_do_formato_nao_impede_corrigir_o_nome(self):
        self._empresa().telefone_contato = "(11) 3333-4444 ramal 22"
        self.db.session.commit()
        cliente, _ = self._entrar()
        self.assertIn("Dados atualizados.", self._enviar("/cliente/configuracoes/perfil", cliente=cliente, nome="Ana Maria",
                                                         telefone="(11) 3333-4444 ramal 22").get_data(as_text=True))
        self.assertEqual((self._usuario().nome, self._empresa().telefone_contato), ("Ana Maria", "(11) 3333-4444 ramal 22"))
        self.assertIn("Informe o telefone", self._enviar("/cliente/configuracoes/perfil", cliente=cliente, nome="Ana Maria",
                                                         telefone="outro telefone").get_data(as_text=True))

    def test_caixa_invalida_ja_gravada_nao_impede_o_chamado_nem_gera_mensagem_ao_remetente(self):
        from models import ChamadoSuporte, ConfiguracaoSistema
        self.db.session.add(ConfiguracaoSistema(chave="suporte_bcc_email", valor="a\u200bb@example.invalid", atualizado_em=T0,
                                                id_usuario_admin=self.admin_id))
        self.db.session.commit()
        cliente, _ = self._entrar()
        with self._emails() as enviados:
            pagina = self._enviar("/cliente/tickets", cliente=cliente, pagina="/cliente/tickets", assunto="Assunto",
                                  categoria="FINANCEIRO", mensagem="Mensagem")
        self.assertIn("Chamado aberto", pagina.get_data(as_text=True))
        self.assertEqual(self._contar(ChamadoSuporte), 1)
        self.assertEqual(enviados, [])

    def test_primeira_gravacao_concorrente_repete_em_vez_de_mostrar_erro(self):
        from blueprints import cliente as rotas_cliente
        chamadas = []
        original = rotas_cliente.salvar_preferencias

        def primeira_perde_a_corrida(*args, **kwargs):
            chamadas.append(1)
            if len(chamadas) == 1:
                raise IntegrityError("INSERT", {}, Exception("corrida sintética"))
            return original(*args, **kwargs)

        cliente, _ = self._entrar()
        with patch.object(rotas_cliente, "salvar_preferencias", side_effect=primeira_perde_a_corrida):
            pagina = self._enviar("/cliente/configuracoes/preferencias", cliente=cliente, alerta_chamado="1")
        self.assertIn("Preferências salvas.", pagina.get_data(as_text=True))
        self.assertEqual(len(chamadas), 2)

    def test_segundo_clique_na_publicacao_nao_acusa_conflito_falso(self):
        from models import DocumentoLegal
        admin, _ = self._entrar(ADMIN)
        for _ in range(2):
            resposta = self._enviar("/admin/configuracoes/documentos", cliente=admin, pagina=PAGINA_ADMIN, tipo="TERMOS_USO",
                                    conteudo="Texto do documento", versao_exibida="0")
            self.assertEqual(resposta.status_code, 200)
        self.assertEqual(self._contar(DocumentoLegal), 1)

    def test_email_do_lembrete_nao_aceito_nao_conta_e_sai_na_rodada_seguinte(self):
        from models import Notificacao
        self.contratar(data_inicio=date(2026, 1, 1), data_fim=date(2026, 3, 10))
        self.ativar_regras()
        comando = ["contratos", "lembretes-renovacao", "--aplicar"]
        with self._emails():
            with patch.object(configuracoes_conta, "email_lembrete_renovacao",
                              return_value=SimpleNamespace(aceito=False, categoria="smtp_conexao", codigo_provedor=None)):
                resultado = self.app.test_cli_runner().invoke(args=comando)
            self.assertEqual(resultado.exit_code, 1, resultado.output)
            self.assertIn("E-mails não aceitos: 1", resultado.output)
            self.assertEqual(self._contar(Notificacao, Notificacao.id_usuario == self.cliente_id), 0)
        with self._emails() as enviados:
            resultado = self.app.test_cli_runner().invoke(args=comando)
        self.assertEqual(resultado.exit_code, 0, resultado.output)
        self.assertEqual([m.recipients for m in enviados], [[CLIENTE]])
        self.assertEqual(self._contar(Notificacao, Notificacao.id_usuario == self.cliente_id), 1)

    def test_comando_recusa_gravar_com_o_envio_indisponivel(self):
        from models import Notificacao
        self.contratar(data_inicio=date(2026, 1, 1), data_fim=date(2026, 3, 10))
        self.ativar_regras()
        self.app.config["MAIL_ATIVO"] = False
        resultado = self.app.test_cli_runner().invoke(args=["contratos", "lembretes-renovacao", "--aplicar"])
        self.assertEqual(resultado.exit_code, 1, resultado.output)
        self.assertIn("indisponível", resultado.output)
        self.assertEqual(self._contar(Notificacao), 0)

    def test_botao_de_lembretes_no_painel_de_contratos(self):
        from models import Notificacao
        self.contratar(data_inicio=date(2026, 1, 1), data_fim=date(2026, 3, 10))
        self.ativar_regras()
        admin, _ = self._entrar(ADMIN)
        pagina = admin.get("/admin/contratos").get_data(as_text=True)
        self.assertIn("1 pessoa(s) ainda não receberam o lembrete", pagina)
        cliente, _ = self._entrar()
        self.assertEqual(cliente.get("/admin/contratos").status_code, 403)
        self.assertEqual(admin.get("/admin/contratos/lembretes-renovacao").status_code, 405)
        with self._emails() as enviados:
            resposta = self._enviar("/admin/contratos/lembretes-renovacao", cliente=admin, pagina="/admin/contratos")
        self.assertIn("1 pessoa(s) avisada(s)", resposta.get_data(as_text=True))
        self.assertEqual(len(enviados), 1)
        self.assertEqual(self._contar(Notificacao, Notificacao.id_usuario == self.cliente_id), 1)

    def test_tela_do_lembrete_e_dos_avisos_nao_promete_o_que_nao_acontece(self):
        cliente, _ = self._entrar()
        pagina = cliente.get(PAGINA_CLIENTE).get_data(as_text=True)
        self.assertIn("passa a valer quando a gestão do seu contrato estiver ativa", pagina)
        self.assertNotIn("você recebe um aviso por e-mail", pagina)  # envio desligado nesta base
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None)
        self.assertIn("você recebe um aviso por e-mail", cliente.get(PAGINA_CLIENTE).get_data(as_text=True))
        self.contratar(data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31))
        self.ativar_regras()
        cliente, _ = self._entrar()
        self.assertIn("quando a equipe NEXO dispara o lembrete", cliente.get(PAGINA_CLIENTE).get_data(as_text=True))

    def test_pagina_do_admin_mostra_o_email_de_acesso(self):
        admin, _ = self._entrar(ADMIN)
        self.assertIn("E-mail de acesso", admin.get(PAGINA_ADMIN).get_data(as_text=True))

    def test_copia_com_remetente_invalido_registra_o_motivo_verdadeiro(self):
        from models import ChamadoSuporte, MensagemSuporte
        chamado = ChamadoSuporte(id_empresa=self.empresa_id, id_usuario_cliente=self.cliente_id, assunto="Assunto",
                                 categoria="FINANCEIRO")
        chamado.mensagens = [MensagemSuporte(id_usuario_remetente=self.cliente_id, mensagem="Texto")]
        self.db.session.add(chamado)
        self.db.session.commit()
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None)
        padrao = self.app.extensions["mail"].default_sender
        self.app.extensions["mail"].default_sender = "NEXO"
        self.addCleanup(setattr, self.app.extensions["mail"], "default_sender", padrao)
        envio = emails.email_novo_chamado(chamado, "caixa@example.invalid", "Financeiro", "http://127.0.0.1/x")
        self.assertEqual((envio.estado, envio.categoria), (emails.EstadoEnvio.FALHA_NO_ENVIO, "remetente_invalido"))


class CorrigirEmailPeloAdminTests(_ConfigCase):
    def setUp(self):
        super().setUp()
        self.rota = f"/admin/usuarios/{self.cliente_id}/corrigir-email"
        self.pagina = f"/admin/empresas/{self.empresa_id}/detalhes"
        self.admin_http, _ = self._entrar(ADMIN)

    def _corrigir(self, novo="dono.legitimo@example.invalid", senha=SENHA):
        return self._enviar(self.rota, cliente=self.admin_http, pagina=self.pagina, novo_email=novo,
                            confirmacao_novo_email=novo, senha_confirmacao=senha)

    def test_exige_admin_csrf_post_e_a_senha_do_admin(self):
        cliente, _ = self._entrar()
        self.assertEqual(cliente.post(self.rota, data={"csrf_token": self._csrf(cliente, PAGINA_CLIENTE), "novo_email": "a@example.invalid",
                                                       "senha_confirmacao": SENHA}).status_code, 403)
        self.assertEqual(self.admin_http.get(self.rota).status_code, 405)
        self.assertEqual(self.admin_http.post(self.rota, data={"novo_email": "a@example.invalid", "senha_confirmacao": SENHA}).status_code, 400)
        self.assertIn("Confirme sua senha", self._corrigir(senha="senha errada").get_data(as_text=True))
        self.assertEqual(self._usuario().email, CLIENTE)

    def test_nao_alcanca_administrador(self):
        rota = f"/admin/usuarios/{self.admin_id}/corrigir-email"
        resposta = self._enviar(rota, cliente=self.admin_http, pagina=self.pagina, novo_email="a@example.invalid", senha_confirmacao=SENHA)
        self.assertIn("Usuário não encontrado", resposta.get_data(as_text=True))
        self.assertEqual(self._usuario(self.admin_id).email, ADMIN)

    def test_recusa_endereco_invalido_ou_em_uso(self):
        self._novo_cliente("ocupado@example.invalid")
        self.assertIn("Informe um e-mail válido", self._corrigir("a\u200bb@example.invalid").get_data(as_text=True))
        self.assertIn("não pode ser usado", self._corrigir("OCUPADO@example.invalid").get_data(as_text=True))
        self.assertEqual(self._usuario().email, CLIENTE)

    def test_devolve_a_conta_ao_dono_e_tira_quem_a_tomou(self):
        intruso, _ = self._entrar()
        geracao = self._usuario().geracao_autorizacao
        with self._emails() as enviados:
            pagina = self._corrigir(" Dono.Legitimo@Example.invalid ")
        self.assertIn("E-mail de acesso corrigido", pagina.get_data(as_text=True))
        usuario = self._usuario()
        self.assertEqual(usuario.email, "dono.legitimo@example.invalid")
        self.assertEqual(usuario.geracao_autorizacao, geracao + 1)
        self.assertFalse(usuario.check_senha(SENHA))
        self.assertEqual(intruso.get(PAGINA_CLIENTE).status_code, 302)
        self.assertNotIn("Location", self._entrar("dono.legitimo@example.invalid")[1].headers)
        por_destino = {tuple(m.recipients): m for m in enviados}
        self.assertEqual(len(enviados), 2)
        self.assertIn("/auth/resetar-senha/", por_destino[("dono.legitimo@example.invalid",)].html)
        self.assertNotIn("/auth/", por_destino[(CLIENTE,)].html)
        self.assertEqual(self.admin_http.get(PAGINA_ADMIN).status_code, 200)

    def test_cadastro_digitado_errado_recebe_a_ativacao_no_endereco_certo(self):
        usuario = self._usuario()
        usuario.primeiro_acesso = True
        self.db.session.commit()
        with self._emails() as enviados:
            self._corrigir()
        ativacao = next(m for m in enviados if m.recipients == ["dono.legitimo@example.invalid"])
        self.assertIn("/auth/primeiro-acesso/", ativacao.html)

    def test_confirmacao_diferente_ou_ausente_nao_muda_nada(self):
        with self._emails() as enviados:
            for extra in ({"confirmacao_novo_email": "dono.legitmo@example.invalid"}, {}):
                resposta = self._enviar(self.rota, cliente=self.admin_http, pagina=self.pagina,
                                        novo_email="dono.legitimo@example.invalid", senha_confirmacao=SENHA, **extra)
                self.assertIn("A confirmação não é igual", resposta.get_data(as_text=True))
        usuario = self._usuario()
        self.assertEqual((usuario.email, enviados), (CLIENTE, []))
        self.assertTrue(usuario.check_senha(SENHA))

    def test_link_diz_o_que_aconteceu_e_o_endereco_anterior_e_avisado(self):
        with self._emails() as enviados:
            self._corrigir()
        por_destino = {tuple(m.recipients): m for m in enviados}
        link = por_destino[("dono.legitimo@example.invalid",)]
        for corpo in (link.html, link.body):
            self.assertIn("A equipe NEXO corrigiu o e-mail de acesso", corpo)
            self.assertIn("deixou de valer", corpo)
            self.assertNotIn("permanece inalterada", corpo)
            self.assertNotIn("Recebemos um pedido", corpo)
        aviso = por_destino[(CLIENTE,)]
        for corpo in (aviso.html, aviso.body):
            self.assertIn("pela equipe NEXO", corpo)
            self.assertIn("Não pediu essa correção?", corpo)
            self.assertNotIn("nas Configurações da conta", corpo)
            self.assertNotIn("dono.legitimo@", corpo)
            self.assertNotIn("/auth/", corpo)

    def test_troca_pelo_dono_e_recuperacao_comum_mantem_os_textos(self):
        cliente, _ = self._entrar()
        with self._emails() as enviados:
            self._enviar("/cliente/configuracoes/email", cliente=cliente, senha_atual=SENHA, novo_email="novo@example.invalid",
                         confirmacao_novo_email="novo@example.invalid")
        self.assertIn("nas Configurações da conta", enviados[0].html)
        self.assertNotIn("pela equipe NEXO", enviados[0].html)
        anonimo = self.app.test_client()
        with self._emails() as enviados:
            anonimo.post("/auth/recuperar-senha", data={"csrf_token": self._csrf(anonimo, "/auth/recuperar-senha"), "email": "novo@example.invalid"})
        self.assertIn("permanece inalterada", enviados[0].html)
        self.assertIn("Recebemos um pedido", enviados[0].html)

    def test_falha_no_aviso_nao_derruba_a_correcao(self):
        with self._emails() as enviados, patch.object(auth, "email_email_alterado", side_effect=RuntimeError("falha fictícia")):
            pagina = self._corrigir().get_data(as_text=True)
        self.assertIn("E-mail de acesso corrigido", pagina)
        self.assertEqual([m.recipients for m in enviados], [["dono.legitimo@example.invalid"]])

    def test_falha_ao_preparar_o_link_ainda_avisa_o_endereco_anterior(self):
        with self._emails() as enviados, patch.object(auth, "gerar_token_recuperacao_senha", side_effect=RuntimeError("falha fictícia")):
            pagina = self._corrigir().get_data(as_text=True)
        self.assertIn("O link para definir a senha não foi enviado", pagina)
        self.assertEqual(self._usuario().email, "dono.legitimo@example.invalid")
        self.assertEqual([m.recipients for m in enviados], [[CLIENTE]])

    def test_conta_pendente_recebe_o_aviso_de_convite_cancelado(self):
        usuario = self._usuario()
        usuario.primeiro_acesso = True
        self.db.session.commit()
        with self._emails() as enviados:
            self._corrigir()
        aviso = next(m for m in enviados if m.recipients == [CLIENTE])
        self.assertIn("convite", aviso.subject)
        for corpo in (aviso.html, aviso.body):
            self.assertIn("Se você não conhece o NEXO, ignore este e-mail", corpo)
            for trecho in ("do***@", "example.invalid", "devolver o acesso", "A senha anterior", "/auth/"):
                self.assertNotIn(trecho, corpo)

    def test_queda_do_banco_antes_de_gravar_vira_aviso(self):
        queda = OperationalError("SELECT", {}, Exception("queda fictícia"))
        with patch("blueprints.admin.usuario_pode_acessar", side_effect=queda):
            self.assertIn("Não foi possível consultar o usuário agora", self._corrigir().get_data(as_text=True))
        with patch.object(auth, "email_em_uso", side_effect=queda):
            self.assertIn("Não foi possível alterar o e-mail. Tente novamente.", self._corrigir().get_data(as_text=True))
        self.assertEqual(self._usuario().email, CLIENTE)

    def test_conta_sem_contrato_vigente_nao_oferece_a_correcao(self):
        self.contratar()
        self.ativar_regras()
        contrato = self.suspender()
        self.relogio(contrato.suspensao_limite.replace(tzinfo=T0.tzinfo) + timedelta(days=1))
        self.assertNotIn(f'action="{self.rota}"', self.admin_http.get(self.pagina).get_data(as_text=True))

    def test_conta_sem_acesso_nao_oferece_a_correcao_e_explica(self):
        csrf = self._csrf(self.admin_http, self.pagina)
        empresa = self._empresa()
        empresa.status_conta = "CANCELADA"
        self.db.session.commit()
        self.assertNotIn(f'action="{self.rota}"', self.admin_http.get(self.pagina).get_data(as_text=True))
        with self._emails() as enviados:
            resposta = self.admin_http.post(self.rota, data={"csrf_token": csrf, "novo_email": "a@example.invalid", "confirmacao_novo_email": "a@example.invalid",
                                                             "senha_confirmacao": SENHA}, follow_redirects=True)
        self.assertIn("sem acesso ao portal", resposta.get_data(as_text=True))
        self.assertEqual((self._usuario().email, enviados), (CLIENTE, []))

    def test_tela_de_detalhes_oferece_a_correcao(self):
        pagina = self.admin_http.get(self.pagina).get_data(as_text=True)
        self.assertIn(f'action="{self.rota}"', pagina)
        self.assertIn('name="confirmacao_novo_email"', pagina)
        self.assertIn("o link não sai agora", pagina)
        with self._emails():
            self.assertIn("vai para o novo endereço", self.admin_http.get(self.pagina).get_data(as_text=True))


class ConexaoSMTPTests(_ConfigCase):
    def test_envio_em_lote_restaura_o_sendmail_no_host_certo(self):
        from flask_mail import Message
        import extensions

        class HostFalso:
            def __init__(self):
                self.fechado = False

            def sendmail(self, *_args, **_kwargs):
                if self.fechado:
                    raise smtplib.SMTPServerDisconnected("please run connect() first")
                return {}

            def quit(self):
                self.fechado = True

        hosts = []

        def novo_host(_conexao):
            hosts.append(HostFalso())
            return hosts[-1]

        limite = SimpleNamespace(max_emails=1, suppress=False)
        with patch.object(extensions._ConexaoSMTPComPrazo, "configure_host", novo_host):
            conexao = extensions._ConexaoSMTPComPrazo(limite)
            conexao.host = novo_host(conexao)
            conexao.num_emails = 0
            for assunto in ("a", "b"):
                conexao.send(Message(subject=assunto, recipients=["c@example.invalid"], sender="r@example.invalid", body="x"))
        self.assertEqual(len(hosts), 3)


class NexoBotConfiguracoesTests(NexoBotApiBase):
    """O caminho citado na resposta e o botão precisam ser o mesmo."""

    def test_o_que_a_propria_pessoa_ajusta_leva_a_configuracoes(self):
        for pergunta in (
            "Termos e Política", "Onde vejo os termos e a política?", "Como aceito a política?", "Preciso aceitar a política?",
            "Qual a versão da política que eu aceitei?", "politica privacidade", "política da privacidade",
            "alterar nome", "mudar o nome", "trocar e-mail", "alterar email", "mudar o email", "editar meu perfil",
            "editar meus dados", "alterar meus dados", "mudar meus dados", "Como desligo os alertas de chamados?",
            "Como desligo os avisos de chamados?", "Como desligo as notificações?", "O que eu posso alterar sozinho?",
            "quero mudar meu celular", "trocar meu telefone", "alterar meu email", "mudar meu nome",
            "desativar alertas de chamados", "ativar lembretes", "desativar lembrete", "desativar notificações",
            "O que vem na exportação de dados?", "Quero uma cópia dos meus dados", "Baixar meus dados", "baixar os dados da empresa",
        ):
            with self.subTest(pergunta=pergunta):
                self.assertEqual(self.perguntar(pergunta)["destino"], "configuracoes")

    def test_troca_de_senha_sem_topico_no_guia_mostra_os_dois_caminhos(self):
        # Com o tópico do Guia no banco a resposta vem dele; a base interna é o que sobra sem o tópico.
        from nexobot import saber
        registro = saber.POR_ID["nexo.acesso.recuperar_senha"]
        self.assertEqual(registro.destino, "configuracoes")
        self.assertIn("Logado: abra Configurações", registro.passos[0])
        self.assertIn("Esqueceu a senha", registro.passos[1])

    def test_pontuacao_e_cortesia_nao_mudam_o_caminho(self):
        for pergunta in ("Pode desativar os alertas?", "Desative os avisos.", "Altere o e-mail.", "Pode trocar o nome?", "editar perfil.",
                         "desativar os alertas dos chamados", "desativar os avisos do chamado", "desativar os avisos por e-mail",
                         "trocar o e-mail da minha conta", "alterar o e-mail de login", "trocar o e-mail cadastrado",
                         "alterar meus dados de contato", "atualizar meus dados de acesso", "alterar meus dados em Configurações",
                         "alterar meus dados no portal"):
            with self.subTest(pergunta=pergunta):
                self.assertEqual(self.perguntar(pergunta)["destino"], "configuracoes")
        for pergunta in ("Pode mudar o tema?", "Mude o tema.", "mudar o tema, por favor", "mudar o tema para escuro", "trocar o tema para o claro"):
            with self.subTest(pergunta=pergunta):
                resposta = self.perguntar(pergunta)
                self.assertIsNone(resposta["destino"])
                self.assertIn("botão de sol ou lua", resposta["resposta"])

    def test_pedido_de_mudar_os_proprios_dados_vem_de_configuracoes(self):
        for pergunta in ("Quero mudar meu e-mail", "Preciso mudar meu e-mail", "Quero mudar meu telefone", "Quero mudar meu nome", "Quero trocar meu e-mail"):
            with self.subTest(pergunta=pergunta):
                resposta = self.perguntar(pergunta)
                self.assertEqual(resposta["destino"], "configuracoes")
                self.assertIn("O que posso fazer em Configurações?", resposta["resposta"])

    def test_alerta_que_nao_e_preferencia_nao_vai_para_configuracoes(self):
        for pergunta in ("alterar os semáforos de alerta", "mudar o nível de alerta"):
            with self.subTest(pergunta=pergunta):
                self.assertEqual(self.perguntar(pergunta)["destino"], "suporte")

    def test_termos_e_politica_em_qualquer_ordem(self):
        for pergunta in ("Onde vejo a política e os termos?", "política e termos de uso", "Preciso aceitar a nova política?",
                         "Como aceitar a nova política?", "Tenho que aceitar de novo a política?", "Li a política, como aceito?"):
            with self.subTest(pergunta=pergunta):
                self.assertEqual(self.perguntar(pergunta)["destino"], "configuracoes")

    def test_desligar_os_lembretes_leva_a_configuracoes(self):
        for pergunta in ("Como desativo os lembretes?", "Como paro de receber os lembretes?", "Não quero mais receber os lembretes",
                         "Como faço para não receber os lembretes?", "Como desligo os lembretes de renovação?"):
            with self.subTest(pergunta=pergunta):
                self.assertEqual(self.perguntar(pergunta)["destino"], "configuracoes")

    def test_trocar_a_planilha_e_a_copia_do_pdf_nao_vao_para_configuracoes(self):
        for pergunta, destino in (("Quero mudar meu arquivo", "upload"), ("Preciso mudar minha planilha", "upload"),
                                  ("Quero uma cópia da devolutiva", "historico")):
            with self.subTest(pergunta=pergunta):
                self.assertEqual(self.perguntar(pergunta)["destino"], destino)

    def test_o_que_depende_da_equipe_continua_em_suporte(self):
        for pergunta in ("Como altero o e-mail de contato?", "alterar o nome fantasia", "mudar o nome da empresa", "alterar o e-mail da empresa",
                         "trocar e-mail de contato", "alterar o cnpj", "desativar conta", "ativar minha empresa", "cancelar meu contrato",
                         "altere o nome da minha empresa", "alterar o e-mail da minha empresa", "trocar o e-mail da nossa empresa",
                         "alterar o nome da loja", "mudar o email da loja", "alterar o e-mail do financeiro", "mudar o nome do plano",
                         "Quero mudar o nome da minha empresa", "alterar os alertas de risco", "mudar os alertas da análise",
                         "alterar o aviso prévio do contrato", "mudar o tema do chamado", "trocar o tema da análise"):
            with self.subTest(pergunta=pergunta):
                self.assertEqual(self.perguntar(pergunta)["destino"], "suporte")

    def test_tema_orienta_para_o_botao_do_topo(self):
        for pergunta in ("ativar tema claro", "ativar tema escuro", "ativar o modo escuro", "mudar tema", "trocar para o modo claro"):
            with self.subTest(pergunta=pergunta):
                resposta = self.perguntar(pergunta)
                self.assertIsNone(resposta["destino"])
                self.assertIn("botão de sol ou lua", resposta["resposta"])

    def test_politica_fora_do_portal_continua_fora_do_escopo(self):
        for pergunta in ("política", "o que acha da política brasileira?", "política de cancelamento", "Você aceita falar de política?",
                         "Em termos de política, o que você acha do governo?"):
            with self.subTest(pergunta=pergunta):
                self.assertEqual(self.perguntar(pergunta)["meta"]["intencao"], "fora_do_escopo")

    def test_regra_de_senha_e_lembrete_nas_respostas(self):
        for pergunta in ("Qual a política de senha?", "Como funciona a política de senha?"):
            self.assertIn("12 caracteres", self.perguntar(pergunta)["resposta"], pergunta)
        for pergunta in ("Quais eventos geram notificação?", "Para que serve o sininho?"):
            self.assertIn("lembrete de renovação", self.perguntar(pergunta)["resposta"], pergunta)

    def test_trocar_a_senha_em_configuracoes_nao_encerra_esta_sessao(self):
        resposta = self.perguntar("O portal é seguro?")["resposta"]
        self.assertIn("Trocar a senha em Configurações encerra as outras sessões", resposta)
        self.assertNotIn("Trocar a senha ou ter a conta desativada", resposta)
        resposta = self.perguntar("Por que fui deslogado?")["resposta"]
        self.assertIn("Trocar a senha em Configurações encerra as outras sessões", resposta)
        self.assertNotIn("Trocar a senha ou ter a conta desativada", resposta)


class LimitesTests(_TokenCase):
    """Limites por usuário: a chave é a conta, não o IP nem a sessão."""
    ENABLE_LIMITS = True

    def _entrar_cliente(self):
        cliente = self.app.test_client()
        resposta = cliente.post("/auth/login", data={"email": "active@example.invalid", "senha": OLD_PASSWORD,
                                                     "csrf_token": self._csrf(cliente)})
        self.assertEqual(resposta.status_code, 302)
        return cliente

    def test_reautenticacao_soma_senha_e_email_e_nao_libera_com_a_senha_certa(self):
        cliente = self._entrar_cliente()
        csrf = self._csrf(cliente, PAGINA_CLIENTE)
        codigos = []
        for i in range(10):
            rota = "senha" if i % 2 else "email"
            codigos.append(cliente.post(f"/cliente/configuracoes/{rota}", data={
                "csrf_token": csrf, "senha_atual": "errada", "nova_senha": NOVA, "confirmacao_nova_senha": NOVA,
                "novo_email": "novo@example.invalid", "confirmacao_novo_email": "novo@example.invalid"}).status_code)
        self.assertEqual(codigos, [302] * 10)
        bloqueio = cliente.post("/cliente/configuracoes/senha", data={
            "csrf_token": csrf, "senha_atual": OLD_PASSWORD, "nova_senha": NOVA, "confirmacao_nova_senha": NOVA})
        self.assertEqual(bloqueio.status_code, 429)
        self.assertRegex(bloqueio.get_data(as_text=True), r"Aguarde \d+ minutos")
        # Nova sessão do mesmo usuário não zera o limite.
        outra = self._entrar_cliente()
        self.assertEqual(outra.post("/cliente/configuracoes/email", data={
            "csrf_token": self._csrf(outra, PAGINA_CLIENTE), "senha_atual": OLD_PASSWORD,
            "novo_email": "novo@example.invalid"}).status_code, 429)

    def test_abertura_de_chamados_tem_limite_por_usuario(self):
        cliente = self._entrar_cliente()
        csrf = self._csrf(cliente, "/cliente/tickets")
        dados = {"csrf_token": csrf, "assunto": "Assunto", "categoria": "FINANCEIRO", "mensagem": "Mensagem"}
        codigos = [cliente.post("/cliente/tickets", data=dados).status_code for _ in range(31)]
        self.assertEqual(codigos, [302] * 30 + [429])
        self.assertEqual(cliente.get("/cliente/tickets").status_code, 200)

    def test_exportacao_tem_limite_por_usuario(self):
        cliente = self._entrar_cliente()
        codigos = [cliente.get("/cliente/configuracoes/exportar-dados").status_code for _ in range(11)]
        self.assertEqual(codigos, [200] * 10 + [429])


if __name__ == "__main__":
    unittest.main()
