"""Configurações da conta em PostgreSQL privado: migração, RLS e corridas reais.

Executar explicitamente (não há skip silencioso se os binários faltarem):
    NEXO_TEST_POSTGRES_BIN=/caminho/local/bin python -B -m unittest tests.postgresql_configuracoes -v

Cada caso recebe banco próprio em cluster privado, sem TCP, sem .env nem servidor
existente, e sem envio de e-mail.
"""

import contextlib
from datetime import date
import io
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy import func, inspect, select, text

from .deploy_support import ROOT
from .postgresql_support import PrivatePostgreSQL
from extensions import db

SENHA = "Trilha serena 4821 norte"
NOVA = "Horizonte azul 2026 rumo"
PAGINA_CLIENTE = "/cliente/configuracoes"
PAGINA_ADMIN = "/admin/configuracoes"
TABELAS = ("preferencia_usuario", "documento_legal", "aceite_documento", "configuracao_sistema")


class PostgreSQLConfiguracoesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.postgresql = PrivatePostgreSQL()
        cls.addClassCleanup(cls.postgresql.close)

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="nexo-pg-config-")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.database = self.postgresql.create_database()
        self.addCleanup(self.postgresql.drop_database, self.database)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for alvo in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            self.stack.enter_context(patch(alvo, side_effect=AssertionError("Rede externa proibida")))
        with patch("dotenv.load_dotenv", return_value=False):
            import app as app_module
            from config import DevelopmentConfig
        uri = self.postgresql.url(self.database)
        valores = {
            "SECRET_KEY": "isolated-postgresql-config-key",
            "SQLALCHEMY_DATABASE_URI": uri, "SQLALCHEMY_DIRECT_URI": uri,
            "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True, "connect_args": {
                "connect_timeout": 5, "password": "", "passfile": str(self.postgresql.passfile),
                "options": "-c statement_timeout=15000 -c lock_timeout=10000",
            }},
            "UPLOAD_FOLDER": self.root / "uploads", "DEBUG": False,
            "SQLALCHEMY_ECHO": False, "TESTING": True,
            "MAIL_ATIVO": False, "MAIL_SUPPRESS_SEND": True,
            "MAIL_USERNAME": None, "MAIL_PASSWORD": None,
            "BREVO_API_KEY": None, "ANTHROPIC_API_KEY": None, "HF_API_TOKEN": None,
            "RATELIMIT_ENABLED": False, "PROXY_FIX_HOPS": 0,
        }
        with contextlib.ExitStack() as configuracao:
            for chave, valor in valores.items():
                configuracao.enter_context(patch.object(DevelopmentConfig, chave, valor, create=True))
            configuracao.enter_context(patch.object(
                app_module, "Flask", side_effect=lambda *a, **kw: Flask(
                    *a, instance_path=str(self.root / "instance"), **kw,
                ),
            ))
            self.app = app_module.create_app("development")
        with self.app.app_context():
            self.assertEqual(db.engine.url.host, str(self.postgresql.socket))
            self.assertEqual(db.engine.url.database, self.database)
            self._migrar("upgrade")
            self._fixtures()
        self.addCleanup(self._encerrar)

    def _encerrar(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def _migrar(self, comando, revisao="head"):
        import flask_migrate
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            getattr(flask_migrate, comando)(directory=str(ROOT / "migrations"), revision=revisao)

    def _fixtures(self):
        from models import Empresa, Plano, Segmento, Usuario
        plano = Plano(nome_plano="BRONZE", valor_mensal=100, qtd_analises_mes=1, tipo_analise_permitida="MENSAL",
                      nivel_entrega_analise="BASICA", nivel_dashboard="RESUMIDO", nivel_atendimento="BAIXO")
        segmento = Segmento(nome_segmento="Segmento fictício")
        db.session.add_all([plano, segmento])
        db.session.flush()
        empresas = [Empresa(id_segmento=segmento.id_segmento, id_plano_atual=plano.id_plano,
                            cnpj=f"0000000000000{i}", razao_social=f"Empresa fictícia {i}",
                            email_contato=f"empresa{i}@example.invalid", data_contratacao=date(2026, 1, 1))
                    for i in (1, 2)]
        db.session.add_all(empresas)
        db.session.flush()
        usuarios = [
            Usuario(nome="Admin Um", email="admin1@example.invalid", role="ADMIN", primeiro_acesso=False),
            Usuario(nome="Admin Dois", email="admin2@example.invalid", role="ADMIN", primeiro_acesso=False),
            Usuario(nome="Cliente Um", email="um@example.invalid", role="CLIENTE", primeiro_acesso=False,
                    id_empresa=empresas[0].id_empresa),
            Usuario(nome="Colega Um", email="colega@example.invalid", role="CLIENTE", primeiro_acesso=False,
                    id_empresa=empresas[0].id_empresa),
            Usuario(nome="Cliente Dois", email="dois@example.invalid", role="CLIENTE", primeiro_acesso=False,
                    id_empresa=empresas[1].id_empresa),
        ]
        for usuario in usuarios:
            usuario.set_senha(SENHA)
        db.session.add_all(usuarios)
        db.session.commit()
        self.id_admin = usuarios[0].id_usuario
        self.id_cliente = usuarios[2].id_usuario
        self.id_empresa = empresas[0].id_empresa

    def _csrf(self, cliente, caminho):
        resposta = cliente.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', resposta.get_data(as_text=True)).group(1)

    def _entrar(self, email):
        cliente = self.app.test_client()
        resposta = cliente.post("/auth/login", data={"email": email, "senha": SENHA,
                                                     "csrf_token": self._csrf(cliente, "/auth/login")})
        self.assertEqual(resposta.status_code, 302)
        return cliente

    def _ao_mesmo_tempo(self, pedidos):
        """Solta todos os POST juntos e devolve os códigos HTTP (exceção de um pedido falha o teste)."""
        barreira = threading.Barrier(len(pedidos))
        resultados, erros = [None] * len(pedidos), []

        def executar(indice, cliente, rota, dados):
            try:
                barreira.wait(timeout=10)
                resultados[indice] = cliente.post(rota, data=dados).status_code
            except Exception as erro:  # noqa: BLE001
                erros.append(erro)

        linhas = [threading.Thread(target=executar, args=(i, *pedido)) for i, pedido in enumerate(pedidos)]
        for linha in linhas:
            linha.start()
        for linha in linhas:
            linha.join(30)
        self.assertEqual(erros, [])
        return resultados

    def _contar(self, modelo):
        with self.app.app_context():
            return db.session.scalar(select(func.count()).select_from(modelo))

    def test_migracao_liga_rls_e_bate_com_os_modelos(self):
        from alembic.autogenerate import compare_metadata
        from alembic.config import Config
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory
        configuracao = Config()
        configuracao.set_main_option("script_location", str(ROOT / "migrations"))
        with self.app.app_context():
            self.assertEqual(db.session.scalar(text("SELECT version_num FROM alembic_version")),
                             ScriptDirectory.from_config(configuracao).get_current_head())
            for tabela in (*TABELAS, "validacao_pdv"):
                self.assertTrue(db.session.scalar(text(
                    "SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:t)"), {"t": tabela}), tabela)
            db.session.rollback()
            with db.engine.connect() as conexao:
                contexto = MigrationContext.configure(conexao, opts={"compare_type": True})
                diferencas = [d for d in compare_metadata(contexto, db.metadata) if any(t in str(d) for t in TABELAS)]
            self.assertEqual(diferencas, [])

    def test_rls_bloqueia_a_api_publica_e_nao_atrapalha_a_aplicacao(self):
        """Como no Supabase: a aplicação entra como dona das tabelas, sem superusuário;
        a API pública entra como outro papel, que recebe GRANT mas não tem política."""
        from models import ConfiguracaoSistema
        import configuracoes_conta
        with self.app.app_context():
            for comando in ("CREATE ROLE dono_app NOSUPERUSER NOLOGIN", "CREATE ROLE anon_api NOSUPERUSER NOLOGIN",
                            "GRANT USAGE ON SCHEMA public TO dono_app, anon_api",
                            "GRANT SELECT ON usuario TO dono_app"):
                db.session.execute(text(comando))
            for tabela in TABELAS:
                db.session.execute(text(f"ALTER TABLE {tabela} OWNER TO dono_app"))
                db.session.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tabela} TO anon_api"))
            db.session.execute(text("GRANT REFERENCES ON usuario TO dono_app"))
            db.session.commit()
            db.session.execute(text("SET ROLE dono_app"))
            db.session.execute(text(
                "INSERT INTO configuracao_sistema (chave, valor, atualizado_em, id_usuario_admin) "
                "VALUES ('suporte_bcc_email', 'caixa@example.invalid', now(), :admin)"), {"admin": self.id_admin})
            self.assertEqual(db.session.scalar(text("SELECT count(*) FROM configuracao_sistema")), 1)
            db.session.execute(text("RESET ROLE"))
            db.session.commit()
            db.session.execute(text("SET ROLE anon_api"))
            self.assertEqual(db.session.scalar(text("SELECT count(*) FROM configuracao_sistema")), 0)
            with self.assertRaises(Exception):
                db.session.execute(text(
                    "INSERT INTO configuracao_sistema (chave, valor, atualizado_em, id_usuario_admin) "
                    "VALUES ('suporte_bcc_email', 'x', now(), 1)"))
            db.session.rollback()
            self.assertEqual(configuracoes_conta.email_copia_suporte(), "caixa@example.invalid")
            self.assertIsNotNone(db.session.get(ConfiguracaoSistema, "suporte_bcc_email"))

    def test_downgrade_recusa_com_dados_e_remove_quando_vazias(self):
        from models import PreferenciaUsuario
        from horario import agora_utc
        with self.app.app_context():
            db.session.add(PreferenciaUsuario(id_usuario=self.id_cliente, data_atualizacao=agora_utc()))
            db.session.commit()
            db.session.remove()
            with self.assertRaises(SystemExit):
                self._migrar("downgrade", "b6935e8de9d4")
            self.assertTrue(inspect(db.engine).has_table("preferencia_usuario"))
            db.session.execute(text("DELETE FROM preferencia_usuario"))
            db.session.commit()
            db.session.remove()
            self._migrar("downgrade", "b6935e8de9d4")
            for tabela in TABELAS:
                self.assertFalse(inspect(db.engine).has_table(tabela), tabela)
            self._migrar("upgrade")
            self.assertTrue(inspect(db.engine).has_table("documento_legal"))

    def test_sem_as_tabelas_nenhuma_consulta_aborta_a_transacao(self):
        """No PostgreSQL, consultar uma tabela que não existe aborta a transação inteira."""
        import preferencias_usuario
        import validacao_pdv
        from models import ChamadoSuporte
        with self.app.app_context():
            db.session.remove()
            self._migrar("downgrade", "b6935e8de9d4")
        preferencias_usuario._esquema_por_engine.clear()
        validacao_pdv._esquema_por_engine.clear()
        cliente = self._entrar("um@example.invalid")
        admin = self._entrar("admin1@example.invalid")
        self.assertEqual(cliente.get(PAGINA_CLIENTE).status_code, 200)
        self.assertEqual(admin.get(PAGINA_ADMIN).status_code, 200)
        self.assertEqual(cliente.get("/cliente/dashboard").status_code, 200)
        self.assertEqual(cliente.get("/cliente/configuracoes/exportar-dados").status_code, 200)
        resposta = cliente.post("/cliente/tickets", data={
            "csrf_token": self._csrf(cliente, "/cliente/tickets"), "assunto": "Assunto", "categoria": "FINANCEIRO",
            "mensagem": "Mensagem"})
        self.assertEqual(resposta.status_code, 302)
        destino = resposta.headers["Location"]
        id_chamado = destino.rsplit("/", 1)[-1]
        caminho = f"/admin/tickets/{id_chamado}"
        csrf = self._csrf(admin, caminho)
        self.assertEqual(admin.post(caminho, data={"csrf_token": csrf, "acao": "responder",
                                                   "mensagem": "Resposta"}).status_code, 302)
        self.assertEqual(admin.post(caminho, data={"csrf_token": csrf, "acao": "alterar_status",
                                                   "status_chamado": "RESOLVIDO"}).status_code, 302)
        self.assertEqual(cliente.post("/cliente/configuracoes/preferencias", data={
            "csrf_token": self._csrf(cliente, PAGINA_CLIENTE)}).status_code, 302)
        self.assertEqual(self._contar(ChamadoSuporte), 1)

    def test_duas_solicitacoes_simultaneas_abrem_um_chamado(self):
        from models import ChamadoSuporte
        pedidos = []
        for email in ("um@example.invalid", "colega@example.invalid", "um@example.invalid"):
            cliente = self._entrar(email)
            pedidos.append((cliente, "/cliente/configuracoes/solicitar-exclusao",
                            {"csrf_token": self._csrf(cliente, PAGINA_CLIENTE)}))
        self.assertEqual(self._ao_mesmo_tempo(pedidos), [302, 302, 302])
        self.assertEqual(self._contar(ChamadoSuporte), 1)

    def test_mesmo_email_simultaneo_fica_com_uma_conta_so(self):
        from models import Usuario
        novo = "disputado@example.invalid"
        clientes = [self._entrar(e) for e in ("um@example.invalid", "dois@example.invalid")]
        pedidos = [(c, "/cliente/configuracoes/email", {"csrf_token": self._csrf(c, PAGINA_CLIENTE), "novo_email": novo,
                                                        "confirmacao_novo_email": novo, "senha_atual": SENHA}) for c in clientes]
        self.assertEqual(self._ao_mesmo_tempo(pedidos), [302, 302])
        with self.app.app_context():
            self.assertEqual(db.session.scalar(select(func.count()).select_from(Usuario).where(Usuario.email == novo)), 1)
        # A perdedora continua logada, com o e-mail antigo; a vencedora, renovada.
        for cliente in clientes:
            self.assertEqual(cliente.get(PAGINA_CLIENTE).status_code, 200)

    def test_publicacao_simultanea_cria_uma_versao(self):
        from models import DocumentoLegal
        admins = [self._entrar(e) for e in ("admin1@example.invalid", "admin2@example.invalid")]
        pedidos = [(a, "/admin/configuracoes/documentos", {
            "csrf_token": self._csrf(a, PAGINA_ADMIN), "tipo": "TERMOS_USO", "versao_exibida": "0",
            "conteudo": f"Texto do administrador {i}"}) for i, a in enumerate(admins)]
        codigos = self._ao_mesmo_tempo(pedidos)
        self.assertEqual(sorted(codigos), [302, 400])
        self.assertEqual(self._contar(DocumentoLegal), 1)

    def test_aceite_simultaneo_grava_uma_linha(self):
        from models import AceiteDocumento
        admin = self._entrar("admin1@example.invalid")
        admin.post("/admin/configuracoes/documentos", data={
            "csrf_token": self._csrf(admin, PAGINA_ADMIN), "tipo": "TERMOS_USO", "versao_exibida": "0", "conteudo": "Termo"})
        clientes = [self._entrar("um@example.invalid") for _ in range(3)]
        pedidos = [(c, "/cliente/configuracoes/aceitar-documento", {
            "csrf_token": self._csrf(c, PAGINA_CLIENTE), "tipo": "TERMOS_USO", "versao": "1"}) for c in clientes]
        self.assertEqual(self._ao_mesmo_tempo(pedidos), [302, 302, 302])
        self.assertEqual(self._contar(AceiteDocumento), 1)

    def _contratar(self, *empresas):
        import contract_admin
        from datetime import timedelta
        from decimal import Decimal
        from contract_policy import data_local
        from horario import agora_utc
        from models import Plano, Usuario
        with self.app.app_context():
            admin = db.session.get(Usuario, self.id_admin)
            hoje = data_local(agora_utc())
            for id_empresa in empresas:
                contract_admin.criar_contrato(db.session, id_empresa, contract_admin.DadosContrato(
                    id_plano=db.session.scalar(select(Plano.id_plano)), valor_mensal=Decimal("150.00"),
                    data_inicio=hoje - timedelta(days=30), data_fim=hoje + timedelta(days=5),
                ), responsavel=admin)
                contract_admin.ativar_regras_contratuais(db.session, id_empresa, justificativa="Conciliação fictícia",
                                                         responsavel=admin)
            db.session.commit()

    def _derrubar_conexoes(self):
        """O que um reinício ou failover do banco faz com as conexões abertas."""
        conexao = self.postgresql.connect(self.database)
        conexao.autocommit = True
        try:
            with conexao.cursor() as cursor:
                cursor.execute("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                               "WHERE datname = current_database() AND pid <> pg_backend_pid()")
        finally:
            conexao.close()

    def _derrubar_conexoes_por_fora(self):
        """Como _derrubar_conexoes, por outro banco: esta base já pode estar recusando conexões."""
        conexao = self.postgresql.connect("postgres")
        conexao.autocommit = True
        try:
            with conexao.cursor() as cursor:
                cursor.execute("SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = %s", (self.database,))
        finally:
            conexao.close()

    def _segunda_empresa(self):
        from models import Empresa
        with self.app.app_context():
            return db.session.scalar(select(Empresa.id_empresa).where(Empresa.cnpj == "00000000000002"))

    def _enviar_lembretes(self, admin):
        return admin.post("/admin/contratos/lembretes-renovacao", data={"csrf_token": self._csrf(admin, "/admin/contratos")},
                          follow_redirects=True).get_data(as_text=True)

    def test_queda_do_banco_na_rodada_de_lembretes_deixa_os_demais_pendentes(self):
        import emails
        self._contratar(self.id_empresa, self._segunda_empresa())
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None, EMAIL_ALERTA_ADMIN=False)
        admin, enviados = self._entrar("admin1@example.invalid"), []

        def transporte(mensagem):
            enviados.append(mensagem.recipients[0])
            if len(enviados) == 1:
                self._derrubar_conexoes()

        with patch.object(emails.mail, "send", side_effect=transporte):
            self.assertIn("Não foi possível concluir o envio", self._enviar_lembretes(admin))
            self.assertEqual(enviados, ["um@example.invalid"])
            pagina = self._enviar_lembretes(admin)
        self.assertIn("2 pessoa(s) avisada(s)", pagina)
        self.assertEqual(sorted(enviados), ["colega@example.invalid", "dois@example.invalid", "um@example.invalid"])

    def test_empresa_na_lixeira_durante_a_rodada_nao_e_avisada(self):
        import emails
        from models import Notificacao
        segunda = self._segunda_empresa()
        self._contratar(self.id_empresa, segunda)
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None, EMAIL_ALERTA_ADMIN=False)
        admin, outro = self._entrar("admin1@example.invalid"), self._entrar("admin2@example.invalid")
        csrf, enviados, respostas = self._csrf(outro, "/admin/lixeira"), [], []

        def transporte(mensagem):
            enviados.append(mensagem.recipients[0])
            if len(enviados) == 1:
                linha = threading.Thread(target=lambda: respostas.append(
                    outro.post(f"/admin/empresa/excluir/{segunda}", data={"csrf_token": csrf}).status_code))
                linha.start()
                linha.join(20)

        with patch.object(emails.mail, "send", side_effect=transporte):
            pagina = self._enviar_lembretes(admin)
        self.assertEqual(respostas, [302])
        self.assertIn("2 pessoa(s) avisada(s)", pagina)
        self.assertEqual(sorted(enviados), ["colega@example.invalid", "um@example.invalid"])
        self.assertEqual(self._contar(Notificacao), 2)

    def test_queda_do_banco_ao_preparar_o_link_nao_vira_erro(self):
        from blueprints import auth
        from models import Usuario
        admin, pagina = self._entrar("admin1@example.invalid"), f"/admin/empresas/{self.id_empresa}/detalhes"
        original, novo = auth._permite_finalidade, "dono.real@example.invalid"

        def permite(usuario, finalidade):
            self._derrubar_conexoes()
            return original(usuario, finalidade)

        with patch.object(auth, "_permite_finalidade", side_effect=permite):
            resposta = admin.post(f"/admin/usuarios/{self.id_cliente}/corrigir-email", data={
                "csrf_token": self._csrf(admin, pagina), "novo_email": novo, "confirmacao_novo_email": novo, "senha_confirmacao": SENHA})
        self.assertEqual(resposta.status_code, 302)
        with self.app.app_context():
            usuario = db.session.get(Usuario, self.id_cliente)
            self.assertEqual(usuario.email, novo)
            self.assertFalse(usuario.check_senha(SENHA))

    def test_banco_fora_do_ar_logo_depois_da_correcao_ainda_avisa_o_endereco_anterior(self):
        import emails
        from blueprints import auth
        admin, pagina = self._entrar("admin1@example.invalid"), f"/admin/empresas/{self.id_empresa}/detalhes"
        csrf, gravar, novo, enviados = self._csrf(admin, pagina), auth._gravar_novo_email, "dono.real@example.invalid", []
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None, EMAIL_ALERTA_ADMIN=False)

        def executar(comando):
            conexao = self.postgresql.connect("postgres")
            conexao.autocommit = True
            try:
                with conexao.cursor() as cursor:
                    cursor.execute(comando)
            finally:
                conexao.close()

        def gravar_e_derrubar(*argumentos, **nomeados):
            resultado = gravar(*argumentos, **nomeados)
            executar(f'ALTER DATABASE "{self.database}" ALLOW_CONNECTIONS false')
            self._derrubar_conexoes_por_fora()
            return resultado

        self.addCleanup(executar, f'ALTER DATABASE "{self.database}" ALLOW_CONNECTIONS true')
        with patch.object(auth, "_gravar_novo_email", side_effect=gravar_e_derrubar), \
                patch.object(emails.mail, "send", side_effect=enviados.append):
            resposta = admin.post(f"/admin/usuarios/{self.id_cliente}/corrigir-email", data={
                "csrf_token": csrf, "novo_email": novo, "confirmacao_novo_email": novo, "senha_confirmacao": SENHA})
            emails._aguardar_envios_pendentes(5.0)
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual([m.recipients for m in enviados], [["um@example.invalid"]])

    def test_banco_fora_do_ar_logo_depois_da_troca_pelo_dono_ainda_avisa_o_endereco_antigo(self):
        import emails
        from blueprints import auth
        cliente, novo, enviados = self._entrar("um@example.invalid"), "novo.um@example.invalid", []
        csrf, gravar = self._csrf(cliente, PAGINA_CLIENTE), auth._gravar_novo_email
        self.app.config.update(MAIL_ATIVO=True, BREVO_API_KEY=None, EMAIL_ALERTA_ADMIN=False)
        recusar = f'ALTER DATABASE "{self.database}" ALLOW_CONNECTIONS '

        def executar(comando):
            conexao = self.postgresql.connect("postgres")
            conexao.autocommit = True
            try:
                with conexao.cursor() as cursor:
                    cursor.execute(comando)
            finally:
                conexao.close()

        def gravar_e_derrubar(*argumentos, **nomeados):
            resultado = gravar(*argumentos, **nomeados)
            executar(recusar + "false")
            self._derrubar_conexoes_por_fora()
            return resultado

        self.addCleanup(executar, recusar + "true")
        with patch.object(auth, "_gravar_novo_email", side_effect=gravar_e_derrubar), \
                patch.object(emails.mail, "send", side_effect=enviados.append):
            resposta = cliente.post("/cliente/configuracoes/email", data={
                "csrf_token": csrf, "senha_atual": SENHA, "novo_email": novo, "confirmacao_novo_email": novo})
            emails._aguardar_envios_pendentes(5.0)
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual([m.recipients for m in enviados], [["um@example.invalid"]])

    def test_downgrade_nao_entra_em_deadlock_com_requisicao_em_andamento(self):
        # A requisição lê usuario e depois uma tabela nova: o LOCK precisa respeitar essa ordem.
        conexao = self.postgresql.connect(self.database)
        resultado = {}

        def rodar():
            with self.app.app_context():
                try:
                    self._migrar("downgrade", "b6935e8de9d4")
                    resultado["saida"] = "removeu"
                except Exception as erro:  # noqa: BLE001
                    resultado["saida"] = type(erro).__name__

        try:
            with conexao.cursor() as cursor:
                cursor.execute("SELECT id_usuario FROM usuario LIMIT 1")
                linha = threading.Thread(target=rodar)
                linha.start()
                with self.app.app_context():
                    limite, esperando = time.monotonic() + 8, 0
                    while time.monotonic() < limite and not esperando:
                        esperando = db.session.scalar(text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND wait_event_type = 'Lock' AND query ILIKE 'LOCK TABLE%'"))
                        db.session.rollback()
                        time.sleep(0.05)
                self.assertTrue(esperando, "O downgrade não esperou a requisição em andamento.")
                cursor.execute("SELECT count(*) FROM preferencia_usuario")
            conexao.commit()
        finally:
            conexao.close()
        linha.join(30)
        self.assertEqual(resultado.get("saida"), "removeu")

    def test_rodadas_simultaneas_de_lembrete_avisam_uma_vez(self):
        import configuracoes_conta
        from models import Notificacao
        self._contratar(self.id_empresa)
        barreira, erros = threading.Barrier(3), []

        def rodada():
            try:
                with self.app.app_context():
                    barreira.wait(timeout=10)
                    configuracoes_conta.enviar_lembretes_renovacao(aplicar=True)
            except Exception as erro:  # noqa: BLE001
                erros.append(erro)

        with patch.object(configuracoes_conta, "email_lembrete_renovacao", return_value=SimpleNamespace(aceito=True)) as envio:
            linhas = [threading.Thread(target=rodada) for _ in range(3)]
            for linha in linhas:
                linha.start()
            for linha in linhas:
                linha.join(30)
        self.assertEqual(erros, [])
        # Dois usuários ativos na empresa: uma notificação e um e-mail para cada um.
        self.assertEqual(self._contar(Notificacao), 2)
        self.assertEqual(envio.call_count, 2)

    def test_downgrade_espera_escrita_em_andamento_e_recusa(self):
        with self.app.app_context():
            conexao = db.engine.connect()
            transacao = conexao.begin()
            conexao.execute(text("INSERT INTO preferencia_usuario (id_usuario, data_atualizacao) VALUES (:id, now())"),
                            {"id": self.id_cliente})
        resultado = {}

        def rodar():
            with self.app.app_context():
                try:
                    self._migrar("downgrade", "b6935e8de9d4")
                    resultado["saida"] = "removeu"
                except SystemExit:
                    resultado["saida"] = "recusou"

        linha = threading.Thread(target=rodar)
        linha.start()
        with self.app.app_context():
            limite = time.monotonic() + 8
            while time.monotonic() < limite:
                esperando = db.session.scalar(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND query ILIKE 'LOCK TABLE%'"))
                db.session.rollback()
                if esperando:
                    break
                time.sleep(0.05)
            self.assertTrue(esperando, "O downgrade não esperou a escrita em andamento.")
            transacao.commit()
            conexao.close()
        linha.join(30)
        self.assertEqual(resultado.get("saida"), "recusou")
        with self.app.app_context():
            self.assertTrue(inspect(db.engine).has_table("preferencia_usuario"))

    def test_exclusao_permanente_com_relatorio_no_banco_real(self):
        from datetime import date as data
        from models import Analise, Empresa, IndicadorAnalise, Plano, RelatorioAnalise
        with self.app.app_context():
            analise = Analise(id_empresa=self.id_empresa, id_plano_referencia=db.session.scalar(select(Plano.id_plano)),
                              id_usuario_admin_responsavel=self.id_admin, periodo_inicio=data(2026, 1, 1),
                              periodo_fim=data(2026, 1, 31), mes_referencia=1, ano_referencia=2026,
                              tipo_analise="MENSAL", status_analise="CONCLUIDO")
            analise.indicador = IndicadorAnalise(faturamento_total=10, total_comprado=5, saldo_estimado_compras_vendas=-5)
            analise.relatorio = RelatorioAnalise(titulo="Relatório", conclusao_estrategica="Conclusão", publicado=True)
            db.session.add(analise)
            db.session.commit()
        admin = self._entrar("admin1@example.invalid")
        csrf = self._csrf(admin, "/admin/lixeira")
        self.assertEqual(admin.post(f"/admin/empresa/excluir/{self.id_empresa}", data={"csrf_token": csrf}).status_code, 302)
        resposta = admin.post(f"/admin/empresa/excluir-permanente/{self.id_empresa}",
                              data={"csrf_token": csrf, "senha_confirmacao": SENHA}, follow_redirects=True)
        self.assertIn("excluídos permanentemente", resposta.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNone(db.session.get(Empresa, self.id_empresa))

    def test_sessao_vencida_nao_impede_a_troca_de_senha_no_banco_real(self):
        cliente_antigo = self._entrar("um@example.invalid")
        cliente = self._entrar("um@example.invalid")
        self.assertTrue(cliente_antigo)
        with self.app.app_context():
            atual = db.session.scalar(text("SELECT max(id_sessao) FROM sessao_autenticada"))
            db.session.execute(text(
                "UPDATE sessao_autenticada SET criada_em = now() - interval '2 hours', expira_em = now() - interval '1 hour' "
                "WHERE id_usuario = :id AND id_sessao <> :atual"), {"id": self.id_cliente, "atual": atual})
            db.session.commit()
        resposta = cliente.post("/cliente/configuracoes/senha", data={
            "csrf_token": self._csrf(cliente, PAGINA_CLIENTE), "senha_atual": SENHA, "nova_senha": NOVA,
            "confirmacao_nova_senha": NOVA}, follow_redirects=True)
        self.assertIn("Senha alterada.", resposta.get_data(as_text=True))

    def test_troca_de_senha_e_exportacao_no_banco_real(self):
        import contract_admin
        from decimal import Decimal
        from models import Plano, Usuario
        with self.app.app_context():
            admin = db.session.get(Usuario, self.id_admin)
            contract_admin.criar_contrato(db.session, self.id_empresa, contract_admin.DadosContrato(
                id_plano=db.session.scalar(select(Plano.id_plano)), valor_mensal=Decimal("150.00"),
                data_inicio=date(2026, 1, 1), data_fim=date(2036, 12, 31),
            ), responsavel=admin)
            db.session.commit()
        cliente = self._entrar("um@example.invalid")
        outra = self._entrar("um@example.invalid")
        resposta = cliente.get("/cliente/configuracoes/exportar-dados")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.get_json()["contratos"][0]["valor_mensal_contratado"], "150.00")
        resposta = cliente.post("/cliente/configuracoes/senha", data={
            "csrf_token": self._csrf(cliente, PAGINA_CLIENTE), "senha_atual": SENHA, "nova_senha": NOVA,
            "confirmacao_nova_senha": NOVA})
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(cliente.get(PAGINA_CLIENTE).status_code, 200)
        self.assertEqual(outra.get(PAGINA_CLIENTE).status_code, 302)


if __name__ == "__main__":
    unittest.main()
