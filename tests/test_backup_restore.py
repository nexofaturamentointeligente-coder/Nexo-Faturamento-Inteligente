"""Ensaio PostgreSQL com dados fictícios, sem aceitar uma conexão externa.

Por padrão o ensaio é ignorado. Para habilitar, indique apenas o diretório dos
binários PostgreSQL em NEXO_TEST_POSTGRES_BIN e execute:
    python -B -m unittest tests.test_backup_restore -v

Um processo separado, com ambiente filtrado e dotenv desabilitado, inicializa
seu próprio cluster temporário. Nenhum banco ou servidor existente é usado.
"""

import contextlib
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("initdb", "pg_ctl", "createdb", "pg_dump", "pg_restore")
SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
    "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
}
VENDAS = "Produto;Quantidade;Valor total\nProduto fictício A;2;20,00\nProduto fictício B;1;5,00\n".encode()
COMPRAS = "Fornecedor;Valor NF\nFornecedor fictício;40,00\n".encode()
GUIA = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def _safe_environment():
    """Nunca herdar DATABASE_URL, PGSERVICE, PGPASSWORD ou chaves de serviços."""
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_KEYS}
    env.update({"PYTHONUTF8": "1", "FLASK_ENV": "development", "NEXO_DB_DIRECT": "0"})
    return env


def _tool(bin_dir, name):
    return bin_dir / (name + (".exe" if os.name == "nt" else ""))


def _etl_or_platform_refusal(calculate, etl_error, sales, purchases):
    """Executa o ETL do ensaio; None somente quando a SEC-005 recusa a plataforma.

    O ETL roda em subprocesso com limites POSIX (etl_worker.py) e recusa, com
    code="platform", sistemas sem esse isolamento, como o Windows. Qualquer
    outra falha continua reprovando o ensaio.
    """
    try:
        return calculate(sales, purchases)
    except etl_error as error:
        if getattr(error, "code", None) != "platform":
            raise
        return None


def _acceptable_not_executed(not_executed, platform):
    """Só o ETL pode ficar sem execução, e nunca no Linux (plataforma do CI e da produção)."""
    return (isinstance(not_executed, list) and set(not_executed) <= {"etl"}
            and not (platform.startswith("linux") and not_executed))


class PostgreSQLBackupRestoreTests(unittest.TestCase):
    def test_backup_and_restore_private_postgresql_cluster(self):
        raw_bin = os.environ.get("NEXO_TEST_POSTGRES_BIN")
        if not raw_bin:
            self.skipTest("Defina NEXO_TEST_POSTGRES_BIN para criar um cluster PostgreSQL temporário isolado.")
        self.assertTrue("://" not in raw_bin and Path(raw_bin).is_absolute(),
                        "NEXO_TEST_POSTGRES_BIN aceita apenas um caminho local absoluto; URLs são recusadas.")
        try:
            bin_dir = Path(raw_bin).resolve(strict=True)
        except (OSError, ValueError):
            self.fail("NEXO_TEST_POSTGRES_BIN não indica um diretório local acessível; valor omitido.")
        self.assertTrue(bin_dir.is_dir(), "NEXO_TEST_POSTGRES_BIN deve indicar um diretório local de binários.")
        self.assertTrue(all(_tool(bin_dir, tool).is_file() for tool in TOOLS),
                        "O diretório precisa conter initdb, pg_ctl, createdb, pg_dump e pg_restore.")
        # Isola também os imports: outros testes podem ter carregado config.py.
        with tempfile.TemporaryDirectory(prefix="nexo-backup-drill-") as work:
            work_dir = Path(work).resolve()
            data_dir = work_dir / "private-cluster"
            try:
                result = subprocess.run(
                    [sys.executable, "-B", str(Path(__file__).resolve()), "--isolated-worker", str(bin_dir), str(work_dir)],
                    cwd=str(ROOT), env=_safe_environment(), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=180, check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail("O ensaio isolado excedeu 180 segundos; nenhuma saída ou credencial foi exibida.")
            finally:
                # O pai conhece o diretório que criou: encerra seu cluster mesmo se
                # o worker exceder o limite ou falhar antes do próprio finally.
                self.assertTrue(data_dir.resolve().is_relative_to(work_dir))
                if (data_dir / "postmaster.pid").is_file():
                    stopped = subprocess.run(
                        [str(_tool(bin_dir, "pg_ctl")), "--pgdata", str(data_dir), "--mode", "fast",
                         "--wait", "--timeout", "20", "stop"],
                        env=_safe_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=30, check=False,
                    )
                    self.assertEqual(stopped.returncode, 0, "O cluster pertencente ao teste não confirmou encerramento.")
        try:
            report = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            self.fail("O processo isolado não produziu um relatório seguro válido; saídas brutas omitidas.")
        self.assertEqual(result.returncode, 0,
                         f"Ensaio isolado falhou na etapa {report.get('stage', 'desconhecida')} "
                         f"({report.get('error_type', 'erro não identificado')}); saídas brutas omitidas.")
        self.assertEqual(report.get("status"), "ok")
        # 19 tabelas do domínio + alembic_version + sessao_autenticada (SEC-001)
        # + as 4 das Configurações da conta.
        self.assertEqual(report.get("tables"), 25)
        self.assertEqual(report.get("uploads"), 4)
        self.assertEqual(report.get("files"), 5)
        not_executed = report.get("not_executed", [])
        self.assertTrue(_acceptable_not_executed(not_executed, sys.platform),
                        "Verificação obrigatória do ensaio não foi executada nesta plataforma.")
        # Verificação não executada nunca entra em "checks"; as 17 seguem contabilizadas.
        self.assertFalse(set(report.get("checks", [])) & set(not_executed),
                         "Verificação não executada listada como aprovada.")
        self.assertEqual(len(report.get("checks", [])) + len(not_executed), 17)
        print("\nEnsaio PostgreSQL isolado: " + json.dumps(report, ensure_ascii=True, sort_keys=True))


class BackupDrillSafetyTests(unittest.TestCase):
    def test_safe_environment_excludes_connections_and_secrets(self):
        with patch.dict(os.environ, {
            "PATH": "local-tools", "DATABASE_URL": "secret-database", "DIRECT_URL": "secret-direct",
            "PGHOST": "existing-host", "PGSERVICE": "existing-service", "PGPASSWORD": "secret-password",
            "PGPASSFILE": "existing-passfile", "PGSERVICEFILE": "existing-servicefile",
            "ANTHROPIC_API_KEY": "secret-api", "BREVO_API_KEY": "secret-mail", "HF_API_TOKEN": "secret-token",
            "NEXO_DB_DIRECT": "1", "FLASK_ENV": "production",
        }, clear=True):
            env = _safe_environment()
        self.assertEqual(env, {"PATH": "local-tools", "PYTHONUTF8": "1",
                               "FLASK_ENV": "development", "NEXO_DB_DIRECT": "0"})

    def test_default_execution_skips_before_subprocess(self):
        case = PostgreSQLBackupRestoreTests("test_backup_and_restore_private_postgresql_cluster")
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run") as run:
            with self.assertRaises(unittest.SkipTest):
                case.test_backup_and_restore_private_postgresql_cluster()
            run.assert_not_called()

    def test_invalid_bin_directory_fails_before_subprocess(self):
        case = PostgreSQLBackupRestoreTests("test_backup_and_restore_private_postgresql_cluster")
        with tempfile.TemporaryDirectory(prefix="nexo-empty-pg-bin-") as empty:
            with patch.dict(os.environ, {"NEXO_TEST_POSTGRES_BIN": empty}, clear=True), patch("subprocess.run") as run:
                with self.assertRaises(AssertionError):
                    case.test_backup_and_restore_private_postgresql_cluster()
                run.assert_not_called()

    def test_database_url_is_rejected_without_echoing_credentials(self):
        case = PostgreSQLBackupRestoreTests("test_backup_and_restore_private_postgresql_cluster")
        with patch.dict(os.environ, {"NEXO_TEST_POSTGRES_BIN": "postgresql://fictional:synthetic-secret@127.0.0.1/db"}, clear=True):
            with patch("subprocess.run") as run:
                with self.assertRaises(AssertionError) as error:
                    case.test_backup_and_restore_private_postgresql_cluster()
                self.assertNotIn("synthetic-secret", str(error.exception))
                run.assert_not_called()

    def test_only_platform_refusal_skips_etl_check(self):
        class SyntheticETLError(Exception):
            def __init__(self, code):
                super().__init__(code)
                self.code = code

        def failing(error):
            def calculate(_sales, _purchases):
                raise error
            return calculate

        self.assertIsNone(_etl_or_platform_refusal(
            failing(SyntheticETLError("platform")), SyntheticETLError, "vendas", "compras"))
        for error in (SyntheticETLError("data"), SyntheticETLError("timeout"),
                      SyntheticETLError(None), RuntimeError("platform")):
            with self.subTest(error=repr(error)), self.assertRaises(type(error)):
                _etl_or_platform_refusal(failing(error), SyntheticETLError, "vendas", "compras")
        result = object()
        self.assertIs(_etl_or_platform_refusal(
            lambda _sales, _purchases: result, SyntheticETLError, "vendas", "compras"), result)

    def test_not_executed_checks_are_never_accepted_on_linux(self):
        self.assertTrue(_acceptable_not_executed([], "linux"))
        self.assertTrue(_acceptable_not_executed([], "win32"))
        self.assertTrue(_acceptable_not_executed(["etl"], "win32"))
        self.assertFalse(_acceptable_not_executed(["etl"], "linux"))
        for missing in (["restore"], ["sha256"], ["etl", "restore"], "etl", None):
            with self.subTest(missing=missing):
                self.assertFalse(_acceptable_not_executed(missing, "win32"))

    def test_parent_never_counts_unexecuted_checks_as_approved(self):
        case = PostgreSQLBackupRestoreTests("test_backup_and_restore_private_postgresql_cluster")
        checks = [f"verificacao_{index}" for index in range(16)]
        # Cada caso reprovado é detectado por uma única asserção do teste pai.
        cases = (("win32", checks, True), ("win32", checks[1:] + ["etl"], False),
                 ("win32", checks[1:], False), ("linux", checks, False))
        with tempfile.TemporaryDirectory(prefix="nexo-fake-pg-bin-") as fake:
            for tool in TOOLS:
                _tool(Path(fake), tool).write_bytes(b"")
            for platform, approved, accepted in cases:
                report = {"status": "ok", "tables": 25, "uploads": 4, "files": 5,  # espelha o ensaio real
                          "checks": approved, "not_executed": ["etl"]}
                completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(report))
                with self.subTest(platform=platform, checks=approved[-1:], accepted=accepted), \
                        patch.dict(os.environ, {"NEXO_TEST_POSTGRES_BIN": fake}, clear=True), \
                        patch("subprocess.run", return_value=completed), \
                        patch.object(sys, "platform", platform), contextlib.redirect_stdout(io.StringIO()):
                    if accepted:
                        case.test_backup_and_restore_private_postgresql_cluster()
                    else:
                        self.assertRaises(AssertionError, case.test_backup_and_restore_private_postgresql_cluster)


class _PrivateDrill(unittest.TestCase):
    """Auxiliar interno; não é um caso descoberto pelo unittest."""

    def __init__(self, bin_dir):
        super().__init__()
        self.bin_dir = bin_dir
        self.stage = "preparação"
        self.not_executed = []
        self.env = _safe_environment()
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env.get("PATH", "")

    def command(self, name, *args, timeout=40):
        self.assertIn(name, TOOLS)
        try:
            result = subprocess.run(
                [str(_tool(self.bin_dir, name)), *map(str, args)],
                env=self.env, cwd=str(self.work_dir), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if name == "pg_restore" and "--list" in args else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("Utilitário local indisponível ou excedeu o limite") from None
        if result.returncode:
            # Erros do cliente/servidor podem conter conexão, SQL ou dados.
            raise RuntimeError("Utilitário PostgreSQL retornou erro; saída omitida")
        return result.stdout or ""

    def app_for(self, database, suffix):
        from flask import Flask
        from config import DevelopmentConfig
        from sqlalchemy import URL
        import app as app_module

        uri = URL.create("postgresql+psycopg2", username=self.username,
                         password=self.password, host="127.0.0.1", port=self.port,
                         database=database)
        # Config é alterada ANTES de db.init_app, jamais depois de criar engine.
        values = {
            "SQLALCHEMY_DATABASE_URI": uri, "SQLALCHEMY_DIRECT_URI": uri,
            "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True, "connect_args": {
                "connect_timeout": 5, "options": "-c statement_timeout=10000 -c lock_timeout=5000"}},
            "DEBUG": False, "SQLALCHEMY_ECHO": False, "TESTING": True,
            "SECRET_KEY": secrets.token_hex(32), "UPLOAD_FOLDER": self.work_dir / suffix / "uploads",
            "MAIL_ATIVO": False, "MAIL_SUPPRESS_SEND": True, "MAIL_USERNAME": None,
            "MAIL_PASSWORD": None, "BREVO_API_KEY": None, "ANTHROPIC_API_KEY": None,
            "HF_API_TOKEN": None, "RATELIMIT_ENABLED": False,
            "RATELIMIT_STORAGE_URI": "memory://", "PROXY_FIX_HOPS": 0,
        }
        with contextlib.ExitStack() as stack:
            for key, value in values.items():
                stack.enter_context(patch.object(DevelopmentConfig, key, value, create=True))
            stack.enter_context(patch.object(
                app_module, "Flask",
                side_effect=lambda *args, **kwargs: Flask(
                    *args, instance_path=str(self.work_dir / suffix / "instance"), **kwargs),
            ))
            return app_module.create_app("development")

    def assert_private_engine(self, database):
        from extensions import db
        url = db.engine.url
        self.assertEqual(url.get_backend_name(), "postgresql")
        self.assertEqual((url.host, url.port, url.username, url.database),
                         ("127.0.0.1", self.port, self.username, database))

    def snapshot(self):
        from extensions import db
        from sqlalchemy import select
        return {name: [tuple(row) for row in db.session.execute(
            select(table).order_by(*table.primary_key.columns))]
            for name, table in db.metadata.tables.items()}

    def fixture(self):
        from extensions import db
        from models import (AceiteDocumento, Analise, ChamadoSuporte, CoberturaPaga, ConfiguracaoSistema,
                            Contrato, DocumentoLegal, Empresa, EventoContratual, GuiaTopico, HistoricoPrecoPlano,
                            IndicadorAnalise, MensagemSuporte, Notificacao, Plano, PreferenciaUsuario,
                            ProdutoCurvaABC, RegimeContratual, RelatorioAnalise, Segmento, UploadRelatorio,
                            Usuario)
        import storage

        plano = Plano(nome_plano="BRONZE", valor_mensal=100, qtd_analises_mes=1,
                      tipo_analise_permitida="MENSAL", nivel_entrega_analise="BASICA",
                      nivel_dashboard="RESUMIDO", nivel_atendimento="BAIXO")
        segmento = Segmento(nome_segmento="Segmento fictício de recuperação")
        db.session.add_all([plano, segmento])
        db.session.flush()
        empresas = [Empresa(
            id_segmento=segmento.id_segmento, id_plano_atual=plano.id_plano,
            cnpj=f"0000000000000{i}", razao_social=f"Empresa fictícia {i}",
            nome_fantasia=f"Recuperação fictícia {i}", email_contato=f"empresa{i}@example.invalid",
            data_contratacao=date(2026, 1, 1),
        ) for i in (1, 2)]
        db.session.add_all(empresas)
        db.session.flush()
        self.login_password = secrets.token_urlsafe(24)
        admin = Usuario(nome="Admin fictício", email="admin-restore@example.invalid",
                        role="ADMIN", primeiro_acesso=False)
        clientes = [Usuario(nome=f"Cliente fictício {i}", email=f"cliente{i}@example.invalid",
                            role="CLIENTE", id_empresa=empresa.id_empresa, primeiro_acesso=False)
                    for i, empresa in enumerate(empresas, 1)]
        for usuario in (admin, *clientes):
            usuario.set_senha(self.login_password)
        db.session.add_all([admin, *clientes])
        db.session.flush()
        self.company_ids = [empresa.id_empresa for empresa in empresas]
        self.analysis_ids = []
        for empresa, cliente in zip(empresas, clientes):
            analise = Analise(
                id_empresa=empresa.id_empresa, id_plano_referencia=plano.id_plano,
                id_usuario_admin_responsavel=admin.id_usuario,
                periodo_inicio=date(2026, 1, 1), periodo_fim=date(2026, 1, 31),
                mes_referencia=1, ano_referencia=2026, tipo_analise="MENSAL", status_analise="CONCLUIDO",
            )
            db.session.add(analise)
            db.session.flush()
            self.analysis_ids.append(analise.id_analise)
            analise.indicador = IndicadorAnalise(faturamento_total=25, total_comprado=40,
                                                saldo_estimado_compras_vendas=15)
            analise.relatorio = RelatorioAnalise(titulo="Análise fictícia restaurada", publicado=True,
                                                 conclusao_estrategica="Conclusão exclusivamente fictícia.")
            analise.curva_abc = [ProdutoCurvaABC(
                posicao_ranking=1, produto_nome="Produto fictício A", faturamento=20,
                quantidade=2, percentual_individual=80, percentual_acumulado=80, classe_abc="A")]
            for tipo, conteudo in (("VENDAS", VENDAS), ("COMPRAS", COMPRAS)):
                digest = hashlib.sha256(conteudo).hexdigest()
                referencia = storage.salvar(conteudo, f"analise_{analise.id_analise}_{tipo}_{digest}.csv")
                analise.uploads.append(UploadRelatorio(
                    tipo_relatorio=tipo, id_usuario_admin=admin.id_usuario,
                    nome_arquivo_original=f"{tipo.lower()}_ficticias.csv", caminho_arquivo=referencia,
                    extensao_arquivo="CSV", tamanho_arquivo=len(conteudo), hash_arquivo=digest,
                    status_processamento="PROCESSADO"))
            chamado = ChamadoSuporte(id_empresa=empresa.id_empresa, id_usuario_cliente=cliente.id_usuario,
                                     assunto="Chamado fictício", categoria="DUVIDA_TECNICA")
            chamado.mensagens = [MensagemSuporte(id_usuario_remetente=cliente.id_usuario,
                                                  mensagem="Mensagem fictícia restaurada.")]
            db.session.add_all([chamado, Notificacao(id_usuario=cliente.id_usuario, texto="Aviso fictício")])
        # Gestão contratual: toda tabela nova precisa de linha (sequências e
        # pg_restore). A empresa 1 fica no regime contratual com janeiro pago.
        instante = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        contrato = Contrato(id_empresa=self.company_ids[0], id_plano=plano.id_plano,
                            valor_mensal_contratado=100, data_inicio=date(2026, 1, 1),
                            data_fim=date(2026, 12, 31), situacao="ATIVO",
                            id_usuario_admin_responsavel=admin.id_usuario, data_criacao=instante)
        db.session.add(contrato)
        db.session.flush()
        cobertura = CoberturaPaga(id_contrato=contrato.id_contrato, id_empresa=self.company_ids[0],
                                  periodo_inicio=date(2026, 1, 1), periodo_fim=date(2026, 1, 31),
                                  referencia_confirmacao="Comprovante fictício", confirmado_em=instante,
                                  id_usuario_admin_confirmacao=admin.id_usuario, data_criacao=instante)
        db.session.add(cobertura)
        db.session.flush()
        db.session.add_all([
            EventoContratual(id_empresa=self.company_ids[0], id_contrato=contrato.id_contrato,
                             id_cobertura=cobertura.id_cobertura, tipo="COBERTURA_CONFIRMADA",
                             ocorrido_em=instante, origem="ADMIN", id_usuario_responsavel=admin.id_usuario,
                             situacao_anterior="ATIVO", situacao_posterior="ATIVO"),
            RegimeContratual(id_empresa=self.company_ids[0], ativado_em=instante,
                             id_usuario_admin_ativacao=admin.id_usuario),
            HistoricoPrecoPlano(id_plano=plano.id_plano, valor_anterior=90, valor_novo=100,
                                alterado_em=instante, origem="fixture-fictícia"),
        ])
        # Configurações da conta: a mesma regra, toda tabela nova com linha.
        documento = DocumentoLegal(tipo="TERMOS_USO", versao=1, conteudo="Termo fictício restaurado.",
                                   publicado_em=instante, id_usuario_admin=admin.id_usuario)
        db.session.add(documento)
        db.session.flush()
        db.session.add_all([
            AceiteDocumento(id_usuario=clientes[0].id_usuario, id_documento=documento.id_documento,
                            aceito_em=instante),
            PreferenciaUsuario(id_usuario=clientes[0].id_usuario, alerta_chamado=False, data_atualizacao=instante),
            ConfiguracaoSistema(chave="suporte_bcc_email", valor="caixa@example.invalid", atualizado_em=instante,
                                id_usuario_admin=admin.id_usuario),
        ])
        guia_ref = storage.salvar(GUIA, "guia/imagem_ficticia.png")
        db.session.add(GuiaTopico(categoria="Teste", pergunta="Recuperação fictícia?",
                                 resposta="Conteúdo fictício restaurado.", imagem_url=guia_ref))
        db.session.commit()

    def validate_restore(self, app, source_snapshot, revision):
        from alembic.script import ScriptDirectory
        from extensions import db
        from sqlalchemy import inspect, select, text
        from sqlalchemy.exc import IntegrityError
        from models import (Analise, ArquivoArmazenado, Empresa, GuiaTopico,
                            UploadRelatorio, Usuario)
        from etl_processor import ETLValidationError, calcular_kpis_em_memoria
        import storage

        with app.app_context():
            self.assert_private_engine("nexo_restore_target")
            self.assertEqual(set(inspect(db.engine).get_table_names()), set(db.metadata.tables) | {"alembic_version"})
            self.assertEqual(db.session.scalar(text("SELECT version_num FROM alembic_version")), revision)
            self.assertEqual(revision, ScriptDirectory(str(ROOT / "migrations")).get_current_head())
            self.assertEqual(self.snapshot(), source_snapshot)
            for company_id, analysis_id in zip(self.company_ids, self.analysis_ids):
                company = db.session.get(Empresa, company_id)
                analysis = db.session.get(Analise, analysis_id)
                self.assertEqual([item.id_analise for item in company.analises], [analysis_id])
                self.assertEqual(analysis.empresa.id_empresa, company_id)
                self.assertEqual(company.usuarios[0].empresa.id_empresa, company_id)
                self.assertTrue(analysis.admin_responsavel.is_admin)
                self.assertEqual(analysis.plano_referencia.id_plano, company.plano_atual.id_plano)
                self.assertEqual(len(analysis.uploads), 2)
                self.assertEqual(analysis.indicador.faturamento_total, 25)
                self.assertTrue(analysis.relatorio.publicado)
                self.assertEqual(analysis.curva_abc[0].produto_nome, "Produto fictício A")
            cache = self.work_dir / "fresh-restored-cache"
            self.assertFalse(cache.exists())
            with patch.object(storage, "_CACHE_DIR", cache):
                for upload in db.session.scalars(select(UploadRelatorio)):
                    expected = VENDAS if upload.tipo_relatorio == "VENDAS" else COMPRAS
                    recovered_path = Path(storage.resolver(upload.caminho_arquivo))
                    self.assertTrue(recovered_path.resolve().is_relative_to(cache.resolve()))
                    recovered = recovered_path.read_bytes()
                    self.assertEqual(recovered, expected)
                    self.assertEqual(hashlib.sha256(recovered).hexdigest(), upload.hash_arquivo)
                    self.assertEqual(len(recovered), upload.tamanho_arquivo)
                    stored = db.session.get(ArquivoArmazenado, upload.caminho_arquivo[3:])
                    self.assertEqual(stored.tamanho, len(expected))
                    self.assertEqual(stored.content_type, "text/csv")
                guia = db.session.scalar(select(GuiaTopico))
                self.assertEqual(Path(storage.resolver(guia.imagem_url)).read_bytes(), GUIA)
                uploads = db.session.get(Analise, self.analysis_ids[0]).uploads
                paths = {item.tipo_relatorio: storage.resolver(item.caminho_arquivo) for item in uploads}
                result = _etl_or_platform_refusal(
                    calcular_kpis_em_memoria, ETLValidationError, paths["VENDAS"], paths["COMPRAS"])
                if result is None:
                    # Registrado como não executado; os bytes/hashes acima seguem obrigatórios.
                    self.not_executed.append("etl")
                else:
                    self.assertTrue(result.sucesso)
                    self.assertEqual(result.kpis["faturamento_total"], 25)
                    self.assertEqual(result.kpis["total_comprado"], 40)
            # Confere todas as sequências numéricas, sem remover nenhuma linha.
            for table in db.metadata.sorted_tables:
                pk = next(iter(table.primary_key.columns))
                # Chave natural (regime_contratual.id_empresa) não tem sequência própria.
                if pk.type.python_type is not int or pk.autoincrement is False:
                    continue
                sequence = db.session.scalar(text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                                             {"table_name": table.name, "column_name": pk.name})
                self.assertIsNotNone(sequence)
                # Tabela vazia (p.ex. sessao_autenticada antes de qualquer login) tem
                # sequência válida mas nenhuma linha: o máximo é 0, não um erro.
                maximum = max((row[0] for row in db.session.execute(select(pk))), default=0)
                next_value = db.session.scalar(text("SELECT nextval(CAST(:sequence AS regclass))"),
                                              {"sequence": sequence})
                self.assertGreater(next_value, maximum)
            original_max = max(row[0] for row in db.session.execute(select(Usuario.id_usuario)))
            novo = Usuario(nome="Novo admin fictício", email="novo@example.invalid", role="ADMIN", primeiro_acesso=False)
            novo.set_senha(secrets.token_urlsafe(24))
            db.session.add(novo)
            db.session.commit()
            self.assertGreater(novo.id_usuario, original_max)
            with self.assertRaises(IntegrityError):
                with db.session.begin_nested():
                    invalid = Usuario(nome="Inválido fictício", email="invalido@example.invalid",
                                      role="CLIENTE", id_empresa=999999, senha_hash="hash-ficticio")
                    db.session.add(invalid)
                    db.session.flush()
            db.session.remove()

        self.stage = "rotas Flask com dados restaurados"
        client = app.test_client()
        self.assertEqual(client.get("/healthz/db").json, {"status": "ok", "db": "ok"})
        response = client.get("/auth/login")
        self.assertEqual(response.status_code, 200)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response.get_data(as_text=True)).group(1)
        response = client.post("/auth/login", data={"email": "cliente1@example.invalid",
                                                    "senha": self.login_password, "csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/cliente/dashboard")
        self.assertEqual(client.get("/cliente/dashboard").status_code, 200)
        self.assertEqual(client.get(f"/cliente/analise/{self.analysis_ids[0]}").status_code, 200)
        self.assertEqual(client.get(f"/cliente/analise/{self.analysis_ids[1]}").status_code, 404)
        self.assertEqual(client.get("/midia/guia/imagem_ficticia.png").data, GUIA)

    def run_drill(self, work_dir):
        self.assertTrue(work_dir.is_dir())
        self.assertEqual(list(work_dir.iterdir()), [])
        with contextlib.nullcontext(work_dir) as work:
            self.work_dir = Path(work).resolve()
            data_dir = self.work_dir / "private-cluster"
            self.assertTrue(data_dir.resolve().is_relative_to(self.work_dir))
            self.assertFalse(data_dir.exists())
            self.username = "nexo_drill_" + secrets.token_hex(6)
            self.password = secrets.token_urlsafe(32)
            password_file = self.work_dir / "init-password"
            password_file.write_text(self.password + "\n", encoding="utf-8")
            password_file.chmod(0o600)
            # Apenas a porta é descoberta; nunca procura/conecta a servidores existentes.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserve:
                reserve.bind(("127.0.0.1", 0))
                self.port = reserve.getsockname()[1]
            passfile = self.work_dir / "private.pgpass"
            passfile.write_text(f"127.0.0.1:{self.port}:*:{self.username}:{self.password}\n", encoding="utf-8")
            passfile.chmod(0o600)
            self.env.update({"PGHOST": "127.0.0.1", "PGPORT": str(self.port),
                             "PGUSER": self.username, "PGPASSFILE": str(passfile),
                             "PGCONNECT_TIMEOUT": "5", "PGSSLMODE": "disable"})
            self.stage = "initdb privado"
            self.command("initdb", "--pgdata", data_dir, "--username", self.username,
                         "--pwfile", password_file, "--auth", "scram-sha-256", "--encoding", "UTF8", "--locale", "C")
            # A restrição vale mesmo se o default do PostgreSQL mudar.
            with (data_dir / "postgresql.conf").open("a", encoding="utf-8") as config:
                config.write(f"\nlisten_addresses = '127.0.0.1'\nport = {self.port}\nunix_socket_directories = ''\n")
            started = False
            apps = []
            try:
                self.stage = "início do cluster privado"
                # Não altera configurações nem executa pg_ctl contra outro diretório.
                self.command("pg_ctl", "--pgdata", data_dir, "--log", self.work_dir / "postgres.log",
                             "--wait", "--timeout", "20", "start")
                started = True
                for database in ("nexo_restore_source", "nexo_restore_target"):
                    self.command("createdb", "--no-password", "--template=template0", "--encoding=UTF8", database)
                self.stage = "migrações reais no banco fictício"
                with patch.dict(os.environ, self.env, clear=True), patch("dotenv.load_dotenv", return_value=False):
                    # Nenhuma importação da aplicação ocorreu antes desta barreira.
                    if str(ROOT) not in sys.path:
                        sys.path.insert(0, str(ROOT))
                    from extensions import db
                    from flask_migrate import upgrade
                    from sqlalchemy import text
                    source = self.app_for("nexo_restore_source", "source")
                    apps.append(source)
                    with source.app_context():
                        self.assert_private_engine("nexo_restore_source")
                        upgrade(directory=str(ROOT / "migrations"))
                        self.stage = "fixtures fictícias"
                        self.fixture()
                        snapshot = self.snapshot()
                        revision = db.session.scalar(text("SELECT version_num FROM alembic_version"))
                        db.session.remove()
                        db.engine.dispose()
                    self.stage = "pg_dump customizado"
                    archive = self.work_dir / "synthetic.dump"
                    self.command("pg_dump", "--no-password", "--format=custom", "--file", archive,
                                 "--dbname", "nexo_restore_source")
                    self.assertGreater(archive.stat().st_size, 0)
                    self.assertEqual(archive.read_bytes()[:5], b"PGDMP")
                    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                    checksum = archive.with_suffix(".sha256")
                    checksum.write_text(digest + "\n", encoding="ascii")
                    self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), checksum.read_text().strip())
                    self.stage = "inspeção e integridade"
                    listing = self.command("pg_restore", "--list", archive)
                    for table in snapshot:
                        self.assertIn(f"TABLE DATA public {table}", listing)
                    self.assertIn("TABLE DATA public alembic_version", listing)
                    self.stage = "CLI real de prontidão"
                    from scripts.verificar_prontidao_backup import cli
                    cli_output = io.StringIO()
                    with contextlib.redirect_stdout(cli_output):
                        self.assertEqual(cli(["--backup", str(archive), "--sha256", digest, "--list"]), 0)
                    self.assertIn(digest, cli_output.getvalue())
                    self.assertIn("Catálogo: pg_restore --list concluído", cli_output.getvalue())
                    invalid = self.work_dir / "truncated.dump"
                    invalid.write_bytes(b"PGDMP\x01")
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli(["--backup", str(invalid), "--list"]), 1)
                    self.stage = "pg_restore no destino vazio"
                    self.command("pg_restore", "--no-password", "--single-transaction", "--exit-on-error",
                                 "--no-owner", "--no-privileges", "--dbname", "nexo_restore_target", archive)
                    self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), digest)
                    target = self.app_for("nexo_restore_target", "target")
                    apps.append(target)
                    self.stage = "comparação completa e relacionamentos"
                    self.validate_restore(target, snapshot, revision)
                    checks = ["migrations", "custom_dump", "sha256", "pg_restore_list", "readiness_cli",
                              "invalid_archive_rejected", "restore",
                              "all_rows", "relationships", "binary_files", "sequences", "foreign_keys",
                              "etl", "health", "csrf_login", "dashboard", "company_isolation"]
                    return {"status": "ok", "tables": len(snapshot) + 1, "uploads": 4, "files": 5,
                            "checks": [check for check in checks if check not in self.not_executed],
                            "not_executed": self.not_executed,
                            "production_access": False}
            finally:
                # Só encerra o cluster inicializado por este processo, sempre por caminho absoluto.
                for app in apps:
                    with app.app_context():
                        from extensions import db
                        db.session.remove()
                        db.engine.dispose()
                if started or (data_dir / "postmaster.pid").exists():
                    self.command("pg_ctl", "--pgdata", data_dir, "--mode", "fast", "--wait", "--timeout", "20", "stop")


def _worker_main(bin_dir, work_dir):
    drill = _PrivateDrill(Path(bin_dir).resolve())
    # Não encaminha mensagens de Flask, SQLAlchemy, Alembic ou PostgreSQL.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            report = drill.run_drill(Path(work_dir).resolve())
    except Exception as error:
        print(json.dumps({"status": "error", "stage": drill.stage, "error_type": type(error).__name__}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--isolated-worker":
        raise SystemExit(_worker_main(sys.argv[2], sys.argv[3]))
    unittest.main()
