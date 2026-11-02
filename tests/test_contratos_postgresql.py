"""Gestão contratual em PostgreSQL real: migração, instantes, acesso e concorrência.

Por padrão o ensaio é ignorado. Para habilitar, indique apenas o diretório dos
binários PostgreSQL em NEXO_TEST_POSTGRES_BIN e execute:
    python -B -m unittest tests.test_contratos_postgresql -v

Mesmo protocolo da NEXO-AJ-06.1 (tests/test_cancelled_company_postgresql.py):
um processo separado, com ambiente filtrado e dotenv desabilitado, inicializa
seu próprio cluster temporário (porta aleatória em 127.0.0.1, sem socket Unix)
e aplica as migrações reais. Nenhum banco ou servidor existente é usado; os
dados são fictícios (example.invalid) e o relógio da política é injetado por
NEXO_RELOGIO, sem esperar prazos em tempo real.

Escopo (complementa tests/test_contratos_gestao.py, que cobre as operações em
SQLite; aqui só o que depende do PostgreSQL real):
  1. a revisão 81287c74387a aplicada sobre dados legados (ATIVA, SUSPENSA,
     CANCELADA, lixeira e análises) não cria contrato, cobertura, evento nem
     regime, preserva contagens e estados, bate com os modelos
     (compare_metadata) e cria as constraints nomeadas e o índice único
     parcial uq_contrato_aplicavel_empresa;
  2. InstanteUTC grava timestamptz e devolve UTC com fuso, qualquer que seja o
     TimeZone da sessão; prazos de calendário gravados; comparação
     suspensao_limite > agora correta no SQL (limite exclusivo);
  3. a cláusula SQL de acesso (usuario_pode_acessar / condicoes_empresa_acessivel)
     concorda com obter_situacao_contratual em todos os estados;
  4. reconciliações concorrentes cancelam uma única vez (lock consultivo e
     locks de linha) e revogam a geração uma única vez;
  5. contratações concorrentes deixam um único contrato vigente;
  6. prorrogação x reconciliação serializadas pelos locks da empresa;
  7. exclusão permanente (rota ADMIN) de empresa na lixeira com contratos,
     coberturas, eventos e renovação encadeada é recusada sem apagar nada
     (histórico financeiro preservado) e sem afetar as outras empresas;
  8. downgrade para e4c004a26b01 recusado com dados contratuais, aceito com as
     tabelas vazias (portal segue no regime legado) e novo upgrade até o head.
"""

import contextlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = str(ROOT / "migrations")
TOOLS = ("initdb", "pg_ctl", "createdb")
SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
    "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
}
CHECKS = (
    "private_engine",
    # 1. Migração sobre dados legados
    "legacy_schema_before_migration", "migration_reaches_head", "migration_preserves_legacy_counts",
    "migration_creates_no_contract_data", "migration_preserves_legacy_state", "metadata_matches_models",
    "named_constraints", "partial_unique_index",
    # 2. InstanteUTC e prazos
    "instant_columns_timestamptz", "instant_roundtrip_utc", "calendar_deadlines_persisted",
    "sql_deadline_comparison", "naive_instant_refused",
    # 3. Cláusula SQL x política
    "access_legacy", "access_active_contract", "access_suspended_within_deadline",
    "access_denied_at_limit", "access_denied_after_limit", "access_denied_without_contract",
    "access_denied_cancelled", "pdf_export_limit_exclusive", "sql_clause_matches_policy",
    # 4. Reconciliações concorrentes
    "advisory_lock_blocks_reconciliation", "concurrent_reconciliation_single_cancellation",
    "concurrent_reconciliation_single_revocation", "reconciliation_idempotent",
    # 5. Contratações concorrentes
    "concurrent_contract_waits_company_lock", "concurrent_contract_single_vigente",
    "concurrent_contract_race_single_vigente", "partial_unique_index_enforced",
    # 6. Prorrogação x reconciliação
    "reconciliation_waits_extension_lock", "extension_first_prevents_cancellation",
    "reconciliation_first_rejects_extension",
    # 7. Exclusão permanente
    "permanent_delete_blocked_with_contract_history", "permanent_delete_keeps_other_companies",
    # 8. Downgrade e novo upgrade
    "downgrade_refused_with_contract_data", "downgrade_with_empty_tables",
    "legacy_access_without_schema", "upgrade_again_after_downgrade",
)
PASSWORD = "Synthetic-pg-contratos-password!"
REVISAO_ANTERIOR = "e4c004a26b01"
REVISAO_CONTRATUAL = "81287c74387a"
TABELAS_CONTRATUAIS = (
    "regime_contratual", "contrato", "cobertura_paga", "evento_contratual", "historico_preco_plano",
)
# Tabelas NÃO contratuais que a cadeia de migrações cria depois de 81287c74387a
# (a migração até "head" também as traz; nascem vazias como as contratuais).
TABELAS_POSTERIORES = ("sessao_autenticada", "preferencia_usuario", "documento_legal", "aceite_documento",
                       "configuracao_sistema")
TABELAS_POR_EMPRESA = ("regime_contratual", "contrato", "cobertura_paga", "evento_contratual")
UTC = timezone.utc
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
TOQUIO = ZoneInfo("Asia/Tokyo")
MICRO = timedelta(microseconds=1)
# Linha do tempo fictícia. Cada cenário de reconciliação usa uma suspensão
# própria para que nenhum outro contrato esteja vencido no instante avaliado.
C0 = datetime(2026, 3, 1, 13, 0, tzinfo=UTC)          # contratações
T0 = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)          # 02/03/2026 10:00 em São Paulo
S_RECONCILIACAO = T0                                    # limite 02/06/2026
S_PRORROGACAO = T0 + timedelta(days=5)                  # limite 07/06/2026
S_RECONCILIACAO_PRIMEIRO = T0 + timedelta(days=10)      # limite 12/06/2026
S_ACESSO = T0 + timedelta(days=20)                      # limite 22/06/2026
LIMITE_ACESSO = datetime(2026, 6, 22, 13, 0, tzinfo=UTC)  # 3 meses de calendário
PDF_ACESSO = datetime(2026, 4, 5, 13, 0, tzinfo=UTC)      # 14 dias corridos


def _safe_environment():
    """Nunca herdar DATABASE_URL, PGSERVICE, PGPASSWORD ou chaves de serviços."""
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_KEYS}
    env.update({"PYTHONUTF8": "1", "FLASK_ENV": "development", "NEXO_DB_DIRECT": "0"})
    return env


def _tool(bin_dir, name):
    return bin_dir / (name + (".exe" if os.name == "nt" else ""))


class PostgreSQLContratosTests(unittest.TestCase):
    def test_gestao_contratual_em_postgresql_real(self):
        raw_bin = os.environ.get("NEXO_TEST_POSTGRES_BIN")
        if not raw_bin:
            self.skipTest("Defina NEXO_TEST_POSTGRES_BIN para criar um cluster PostgreSQL temporário isolado.")
        self.assertTrue("://" not in raw_bin and Path(raw_bin).is_absolute(),
                        "NEXO_TEST_POSTGRES_BIN aceita apenas um caminho local absoluto; URLs são recusadas.")
        try:
            bin_dir = Path(raw_bin).resolve(strict=True)
        except (OSError, ValueError):
            self.fail("NEXO_TEST_POSTGRES_BIN não indica um diretório local acessível; valor omitido.")
        self.assertTrue(all(_tool(bin_dir, tool).is_file() for tool in TOOLS),
                        "O diretório precisa conter initdb, pg_ctl e createdb.")
        with tempfile.TemporaryDirectory(prefix="nexo-contratos-pg-") as work:
            work_dir = Path(work).resolve()
            data_dir = work_dir / "private-cluster"
            try:
                result = subprocess.run(
                    [sys.executable, "-B", str(Path(__file__).resolve()), "--isolated-worker", str(bin_dir), str(work_dir)],
                    cwd=str(ROOT), env=_safe_environment(), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=240, check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail("O ensaio isolado excedeu 240 segundos; nenhuma saída ou credencial foi exibida.")
            finally:
                self.assertTrue(data_dir.resolve().is_relative_to(work_dir))
                if (data_dir / "postmaster.pid").is_file():
                    subprocess.run(
                        [str(_tool(bin_dir, "pg_ctl")), "--pgdata", str(data_dir), "--mode", "fast",
                         "--wait", "--timeout", "20", "stop"],
                        env=_safe_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=30, check=False,
                    )
        try:
            report = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            self.fail("O processo isolado não produziu um relatório seguro válido; saídas brutas omitidas.")
        self.assertEqual(result.returncode, 0,
                         f"Ensaio isolado falhou na etapa {report.get('stage', 'desconhecida')} "
                         f"({report.get('error', 'erro não identificado')}); saídas brutas omitidas.")
        self.assertEqual(report.get("status"), "ok")
        self.assertEqual(sorted(report.get("checks", [])), sorted(CHECKS))
        self.assertFalse(report.get("production_access"))
        print("\nEnsaio PostgreSQL gestão contratual: " + json.dumps(report, ensure_ascii=True, sort_keys=True))


class _PrivateCluster:
    """Auxiliar do processo isolado; não é um caso descoberto pelo unittest."""

    def __init__(self, bin_dir, work_dir):
        self.bin_dir, self.work_dir = bin_dir, work_dir
        self.env = _safe_environment()
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env.get("PATH", "")
        self.stage = "preparação"
        self.checks = []
        self.ids = {}  # nome -> (id_empresa, id_cliente)
        self.relogio = {"agora": T0}

    def command(self, name, *args):
        if name not in TOOLS:
            raise RuntimeError("Utilitário não permitido")
        try:
            result = subprocess.run([str(_tool(self.bin_dir, name)), *map(str, args)], env=self.env,
                                    cwd=str(self.work_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("Utilitário local indisponível ou excedeu o limite") from None
        if result.returncode:
            raise RuntimeError("Utilitário PostgreSQL retornou erro; saída omitida")

    def check(self, name, condition, detail=""):
        if not condition:
            raise AssertionError(f"{name} {detail}".strip())
        self.checks.append(name)

    def run(self):
        if any(self.work_dir.iterdir()):
            raise RuntimeError("A pasta de trabalho precisa começar vazia")
        data_dir = self.work_dir / "private-cluster"
        user = "nexo_contratos_" + secrets.token_hex(4)
        secret = secrets.token_urlsafe(24)
        # Apenas a porta é descoberta; nunca procura/conecta a servidores existentes.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        password_file = self.work_dir / "init-password"
        password_file.write_text(secret + "\n", encoding="utf-8")
        password_file.chmod(0o600)
        self.stage = "initdb privado"
        self.command("initdb", "--pgdata", data_dir, "--username", user, "--pwfile", password_file,
                     "--auth", "scram-sha-256", "--encoding", "UTF8", "--locale", "C")
        password_file.unlink()
        with (data_dir / "postgresql.conf").open("a", encoding="utf-8") as config:
            config.write(f"\nlisten_addresses = '127.0.0.1'\nport = {port}\nunix_socket_directories = ''\n")
        self.env.update({"PGHOST": "127.0.0.1", "PGPORT": str(port), "PGUSER": user,
                         "PGPASSWORD": secret, "PGCONNECT_TIMEOUT": "5", "PGSSLMODE": "disable"})
        started = False
        app = None
        try:
            self.stage = "início do cluster privado"
            self.command("pg_ctl", "--pgdata", data_dir, "--log", self.work_dir / "postgres.log",
                         "--wait", "--timeout", "30", "start")
            started = True
            self.command("createdb", "--no-password", "--template=template0", "--encoding=UTF8", "nexo_contratos")
            app = self.scenarios(user, secret, port)
            return {"status": "ok", "checks": self.checks, "production_access": False}
        finally:
            if app is not None:
                with app.app_context():
                    from extensions import db
                    db.session.remove()
                    db.engine.dispose()
            if started or (data_dir / "postmaster.pid").exists():
                self.command("pg_ctl", "--pgdata", data_dir, "--mode", "fast", "--wait", "--timeout", "30", "stop")

    def scenarios(self, user, secret, port):
        self.stage = "aplicação no banco fictício"
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        # fileConfig do env.py do Alembic é isolado; revisões e DDL são reais.
        with patch.dict(os.environ, {k: v for k, v in self.env.items() if not k.startswith("PG")}, clear=True), \
                patch("dotenv.load_dotenv", return_value=False), patch("logging.config.fileConfig"):
            from flask import Flask
            from sqlalchemy import URL
            from config import DevelopmentConfig
            import app as app_module
            uri = URL.create("postgresql+psycopg2", username=user, password=secret,
                             host="127.0.0.1", port=port, database="nexo_contratos")
            values = {
                "SQLALCHEMY_DATABASE_URI": uri, "SQLALCHEMY_DIRECT_URI": uri,
                "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True, "connect_args": {
                    "connect_timeout": 5, "options": "-c statement_timeout=15000 -c lock_timeout=8000"}},
                "DEBUG": False, "SQLALCHEMY_ECHO": False, "TESTING": True, "SECRET_KEY": secrets.token_hex(32),
                "UPLOAD_FOLDER": self.work_dir / "uploads", "MAIL_ATIVO": False, "MAIL_SUPPRESS_SEND": True,
                "BREVO_API_KEY": None, "ANTHROPIC_API_KEY": None, "HF_API_TOKEN": None,
                "RATELIMIT_ENABLED": False, "RATELIMIT_STORAGE_URI": "memory://", "PROXY_FIX_HOPS": 0,
            }
            with contextlib.ExitStack() as stack:
                for key, value in values.items():
                    stack.enter_context(patch.object(DevelopmentConfig, key, value, create=True))
                stack.enter_context(patch.object(app_module, "Flask", side_effect=lambda *a, **kw: Flask(
                    *a, instance_path=str(self.work_dir / "instance"), **kw)))
                app = app_module.create_app("development")
            # Relógio único da política, injetado e controlado pelo ensaio.
            app.config["NEXO_RELOGIO"] = lambda: self.relogio["agora"]

            from extensions import db
            self.app, self.db = app, db
            with app.app_context():
                url = db.engine.url
                self.check("private_engine", url.get_backend_name() == "postgresql"
                           and (url.host, url.port) == ("127.0.0.1", port))
            self._etapa_migracao_legada()
            self._etapa_instante_utc()
            self._etapa_acesso()
            self._etapa_reconciliacao_concorrente()
            self._etapa_contratacao_concorrente()
            self._etapa_prorrogacao_concorrente()
            self._etapa_exclusao_permanente()
            self._etapa_downgrade()
            return app

    # ------------------------------------------------------------------ apoio
    @contextlib.contextmanager
    def _sessao(self):
        """Contexto de aplicação e Session própria (como as entregas pós-commit)."""
        from sqlalchemy.orm import Session
        with self.app.app_context():
            with Session(bind=self.db.engine) as sessao:
                try:
                    yield sessao
                finally:
                    sessao.rollback()

    def _operar(self, funcao):
        """Executa uma operação administrativa e confirma (as funções não fazem commit)."""
        from models import Usuario
        with self._sessao() as sessao:
            valor = funcao(sessao, sessao.get(Usuario, self.admin_id))
            sessao.commit()
            return valor

    def _thread(self, funcao):
        """Thread com app_context e Session próprios; devolve (thread, saída)."""
        from sqlalchemy.orm import Session
        from models import Usuario
        saida = {}

        def corpo():
            try:
                with self.app.app_context():
                    with Session(bind=self.db.engine) as sessao:
                        try:
                            saida["valor"] = funcao(sessao, sessao.get(Usuario, self.admin_id))
                        finally:
                            sessao.rollback()
            except BaseException as erro:  # devolvido ao fluxo principal para as verificações
                saida["erro"] = erro

        thread = threading.Thread(target=corpo, daemon=True)
        thread.start()
        return thread, saida

    @staticmethod
    def _pid(sessao):
        return sessao.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar()

    def _aguardar_bloqueio(self, pid_bloqueado, pid_bloqueador, limite=10.0):
        """True quando pid_bloqueado espera pid_bloqueador no lock da EMPRESA.

        Exige que o comando em espera seja o SELECT ... FROM empresa ... FOR
        UPDATE de company_security.bloquear_empresa: esperar em outro ponto (por
        exemplo, no índice único do contrato) não prova a ordem Empresa -> Contrato.
        """
        from sqlalchemy import text
        if pid_bloqueado is None or pid_bloqueador is None:
            return False
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                conexao = conexao.execution_options(isolation_level="AUTOCOMMIT")
                fim = time.monotonic() + limite
                while time.monotonic() < fim:
                    linha = conexao.execute(text(
                        "SELECT pg_blocking_pids(:pid), query FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": pid_bloqueado}).first()
                    if linha is not None and pid_bloqueador in (linha[0] or []):
                        consulta = " ".join((linha[1] or "").split()).upper()
                        return "FROM EMPRESA" in consulta and consulta.endswith("FOR UPDATE")
                    time.sleep(0.01)  # espera curta pelo lock entre conexões; não é prazo contratual
        return False

    def _revisao(self):
        from sqlalchemy import text
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                return conexao.scalar(text("SELECT version_num FROM alembic_version"))

    def _tabelas(self):
        from sqlalchemy import inspect
        with self.app.app_context():
            return set(inspect(self.db.engine).get_table_names()) - {"alembic_version"}

    # Esquema POSTERIOR à migração contratual (SEC-001: sessao_autenticada; SEC-003:
    # colunas da cópia publicada), ortogonal ao regime contratual. O downgrade até
    # e4c004a26b01 o remove; o código atual precisa dele para logar (fail-closed) e
    # para ler relatórios. É restaurado só no estágio legado e removido antes do novo
    # upgrade, que o recria (create_table/add_column estritos). Nomes constantes.
    _COLUNAS_POSTERIORES = (
        ("titulo_publicado", "VARCHAR"), ("resumo_executivo_publicado", "TEXT"),
        ("conclusao_estrategica_publicada", "TEXT"), ("gerado_por_ia_publicado", "BOOLEAN"),
        ("versao_rascunho", "INTEGER NOT NULL DEFAULT 1"),
    )

    def _restaurar_esquema_posterior(self):
        from sqlalchemy import text
        from models import SessaoAutenticada
        with self.app.app_context():
            SessaoAutenticada.__table__.create(self.db.engine, checkfirst=True)
            with self.db.engine.begin() as conexao:
                for coluna, tipo in self._COLUNAS_POSTERIORES:
                    conexao.execute(text(f"ALTER TABLE relatorio_analise ADD COLUMN IF NOT EXISTS {coluna} {tipo}"))
        self._limpar_cache_esquema()

    def _remover_esquema_posterior(self):
        from sqlalchemy import text
        from models import SessaoAutenticada
        with self.app.app_context():
            with self.db.engine.begin() as conexao:
                for coluna, _tipo in reversed(self._COLUNAS_POSTERIORES):
                    conexao.execute(text(f"ALTER TABLE relatorio_analise DROP COLUMN IF EXISTS {coluna}"))
            SessaoAutenticada.__table__.drop(self.db.engine, checkfirst=True)
        self._limpar_cache_esquema()

    def _contagens(self):
        from sqlalchemy import text
        tabelas = sorted(self._tabelas())
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                return {tabela: conexao.scalar(text(f'SELECT COUNT(*) FROM "{tabela}"')) for tabela in tabelas}

    def _contratuais_da_empresa(self, id_empresa, *, outras=False):
        from sqlalchemy import text
        operador = "<>" if outras else "="
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                return {tabela: conexao.scalar(text(f"SELECT COUNT(*) FROM {tabela} WHERE id_empresa {operador} :e"),
                                               {"e": id_empresa})
                        for tabela in TABELAS_POR_EMPRESA}

    def _estado_legado(self):
        from sqlalchemy import text
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                return tuple(tuple(map(tuple, conexao.execute(text(consulta)).all())) for consulta in (
                    "SELECT id_empresa, status_conta, deletado_em, id_plano_atual FROM empresa ORDER BY id_empresa",
                    "SELECT id_usuario, ativo, geracao_autorizacao, senha_hash, id_empresa FROM usuario "
                    "ORDER BY id_usuario",
                    "SELECT id_analise, id_empresa, status_analise, periodo_inicio, periodo_fim FROM analise "
                    "ORDER BY id_analise",
                ))

    def _diferencas_metadata(self):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                contexto = MigrationContext.configure(conexao, opts={"compare_type": True})
                return compare_metadata(contexto, self.db.metadata)

    def _migrar(self, operacao, revisao):
        """Flask-Migrate converte falhas em SystemExit(1); aqui vira exceção comum."""
        with self.app.app_context():
            try:
                operacao(directory=MIGRATIONS, revision=revisao)
            except SystemExit as saida:
                raise RuntimeError(f"Migração para {revisao} falhou (código {saida.code})") from None

    @staticmethod
    def _limpar_cache_esquema():
        import contract_policy
        contract_policy._esquema_por_engine.clear()

    def _nova_empresa(self, nome, *, status="ATIVA", deletado_em=None, senha=False):
        from models import Empresa, Usuario
        numero = len(self.ids) + 1
        with self._sessao() as sessao:
            empresa = Empresa(
                id_segmento=self.segmento_id, id_plano_atual=self.plano_id,
                cnpj=f"9900000000{numero:04d}", razao_social=f"Empresa fictícia {nome}",
                nome_fantasia=f"Contratos {nome}", email_contato=f"empresa-{nome.lower()}@example.invalid",
                data_contratacao=date(2026, 1, 1), status_conta=status, deletado_em=deletado_em,
            )
            sessao.add(empresa)
            sessao.flush()
            cliente = Usuario(nome=f"Cliente {nome}", email=f"cliente-{nome.lower()}@example.invalid",
                              role="CLIENTE", id_empresa=empresa.id_empresa, primeiro_acesso=False)
            if senha:
                cliente.set_senha(PASSWORD)
            else:
                cliente.senha_hash = "synthetic-no-login-hash"
            sessao.add(cliente)
            sessao.flush()
            self.ids[nome] = (empresa.id_empresa, cliente.id_usuario)
            sessao.commit()
        return self.ids[nome][0]

    def _nova_analise(self, nome, inicio, fim, *, publicada=False):
        from sqlalchemy import text
        from models import Analise, IndicadorAnalise
        with self._sessao() as sessao:
            analise = Analise(
                id_empresa=self.ids[nome][0], id_plano_referencia=self.plano_id,
                id_usuario_admin_responsavel=self.admin_id, periodo_inicio=inicio, periodo_fim=fim,
                mes_referencia=inicio.month, ano_referencia=inicio.year, tipo_analise="MENSAL",
                status_analise="CONCLUIDO" if publicada else "AGUARDANDO_RELATORIO",
            )
            if publicada:
                analise.data_conclusao = datetime(2026, 2, 5, 12)
                analise.indicador = IndicadorAnalise(
                    faturamento_total=Decimal("18500"), total_comprado=Decimal("14200"),
                    saldo_estimado_compras_vendas=Decimal("-4300"),
                )
            sessao.add(analise)
            sessao.flush()
            if publicada:
                # Linha LEGADA de relatório: este estágio roda no esquema anterior à
                # cópia publicada (SEC-003, migração b6935e8de9d4), em que as colunas
                # *_publicado/versao_rascunho ainda não existem. O ORM/Core emitiria
                # o default de versao_rascunho, então a linha é inserida como era.
                sessao.execute(text(
                    "INSERT INTO relatorio_analise (id_analise, titulo, resumo_executivo, "
                    "conclusao_estrategica, publicado, data_publicacao, gerado_por_ia) "
                    "VALUES (:id, :titulo, :resumo, :conclusao, TRUE, :quando, FALSE)"
                ), {"id": analise.id_analise, "titulo": f"Análise fictícia {nome}",
                    "resumo": "Resumo fictício", "conclusao": "Conclusão fictícia",
                    "quando": datetime(2026, 2, 5, 12)})
            sessao.commit()

    def _dados(self, **valores):
        from contract_admin import DadosContrato
        base = dict(id_plano=self.plano_id, valor_mensal=Decimal("150.00"), data_inicio=date(2026, 1, 1),
                    data_fim=date(2026, 12, 31))
        base.update(valores)
        return DadosContrato(**base)

    def _contratar(self, nome, instante=C0, **valores):
        import contract_admin
        id_empresa, dados = self.ids[nome][0], self._dados(**valores)
        return self._operar(lambda s, admin: contract_admin.criar_contrato(
            s, id_empresa, dados, responsavel=admin, instante=instante).contrato.id_contrato)

    def _ativar_regras(self, nome, instante=C0):
        import contract_admin
        id_empresa = self.ids[nome][0]
        self._operar(lambda s, admin: contract_admin.ativar_regras_contratuais(
            s, id_empresa, justificativa="Conciliação fictícia", responsavel=admin, instante=instante))

    def _pagar(self, nome, id_contrato, inicio, fim, instante):
        import contract_admin
        id_empresa = self.ids[nome][0]
        return self._operar(lambda s, admin: contract_admin.registrar_cobertura(
            s, id_empresa, id_contrato, periodo_inicio=inicio, periodo_fim=fim,
            referencia="Comprovante fictício", responsavel=admin, instante=instante).evento.id_cobertura)

    def _suspender(self, nome, id_contrato, suspenso_em):
        import contract_admin
        id_empresa, versao = self.ids[nome][0], self._contrato(id_contrato)["versao"]
        self._operar(lambda s, admin: contract_admin.suspender_contrato(
            s, id_empresa, id_contrato, versao, motivo="Inadimplência fictícia", responsavel=admin,
            suspenso_em=suspenso_em, instante=suspenso_em))

    def _cancelar(self, nome, id_contrato, instante):
        import contract_admin
        id_empresa, versao = self.ids[nome][0], self._contrato(id_contrato)["versao"]
        self._operar(lambda s, admin: contract_admin.cancelar_contrato(
            s, id_empresa, id_contrato, versao, motivo="Cancelamento fictício", responsavel=admin,
            instante=instante))

    def _contrato(self, id_contrato):
        from models import Contrato
        with self._sessao() as sessao:
            contrato = sessao.get(Contrato, id_contrato)
            return {campo: getattr(contrato, campo) for campo in (
                "situacao", "versao", "id_contrato_anterior", "suspenso_em", "suspensao_limite",
                "suspensao_limite_original", "pdf_limite", "pdf_limite_original", "data_criacao",
                "data_atualizacao", "cancelamento_motivo",
            )}

    def _empresa(self, nome):
        from models import Empresa, Usuario
        id_empresa, id_cliente = self.ids[nome]
        with self._sessao() as sessao:
            empresa = sessao.get(Empresa, id_empresa)
            cliente = sessao.get(Usuario, id_cliente)
            return (empresa.status_conta if empresa else None,
                    cliente.geracao_autorizacao if cliente else None)

    def _eventos(self, nome, tipo):
        from sqlalchemy import select
        from models import EventoContratual
        with self._sessao() as sessao:
            return sessao.scalars(select(EventoContratual.origem).where(
                EventoContratual.id_empresa == self.ids[nome][0], EventoContratual.tipo == tipo,
            ).order_by(EventoContratual.id_evento)).all()

    def _acesso(self, nome, instante):
        """usuario_pode_acessar (cláusula SQL) do CLIENTE da empresa no instante do relógio."""
        from models import Usuario
        from session_security import usuario_pode_acessar
        self.relogio["agora"] = instante
        with self._sessao() as sessao:
            return usuario_pode_acessar(sessao.get(Usuario, self.ids[nome][1]), consulta=sessao)

    @staticmethod
    def _resumo(relatorio):
        """Primitivos do relatório (lidos antes do commit, que expira os objetos)."""
        return {
            "bloqueada": relatorio.bloqueada,
            "candidatos": [c["id_empresa"] for c in relatorio.candidatos],
            "cancelados": [c["id_empresa"] for c in relatorio.cancelados],
            "revogados": [u.id_usuario for u in relatorio.revogados],
        }

    def _csrf(self, cliente, caminho):
        pagina = cliente.get(caminho).get_data(as_text=True)
        encontrado = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina)
        if encontrado is None:
            raise AssertionError(f"csrf ausente em {caminho}")
        return encontrado.group(1)

    def _login(self, email):
        cliente = self.app.test_client()
        resposta = cliente.post("/auth/login", data={"email": email, "senha": PASSWORD,
                                                     "csrf_token": self._csrf(cliente, "/auth/login")})
        return cliente, resposta

    # ------------------------------------------------- 1. migração sobre legado
    def _etapa_migracao_legada(self):
        from alembic.script import ScriptDirectory
        from flask_migrate import upgrade
        from sqlalchemy import text
        from contract_policy import obter_situacao_contratual
        from models import Empresa, Plano, Segmento, Usuario, Notificacao

        self.stage = "migração até e4c004a26b01"
        self._migrar(upgrade, REVISAO_ANTERIOR)
        self.check("legacy_schema_before_migration", self._revisao() == REVISAO_ANTERIOR
                   and not self._tabelas() & set(TABELAS_CONTRATUAIS))

        self.stage = "dados legados fictícios"
        with self._sessao() as sessao:
            plano = Plano(nome_plano="BRONZE", valor_mensal=Decimal("100"), qtd_analises_mes=1,
                          tipo_analise_permitida="MENSAL", nivel_entrega_analise="BASICA",
                          nivel_dashboard="RESUMIDO", nivel_atendimento="BAIXO")
            segmento = Segmento(nome_segmento="Gestão contratual sintética")
            admin = Usuario(nome="Admin fictício", email="admin-contratos@example.invalid", role="ADMIN",
                            primeiro_acesso=False)
            admin.set_senha(PASSWORD)
            sessao.add_all([plano, segmento, admin])
            sessao.commit()
            self.plano_id, self.segmento_id, self.admin_id = plano.id_plano, segmento.id_segmento, admin.id_usuario
        self._nova_empresa("E1", senha=True)
        self._nova_empresa("E2", status="SUSPENSA")
        self._nova_empresa("E3", status="CANCELADA")
        self._nova_empresa("E4", deletado_em=datetime(2026, 2, 1, 12))
        for nome, publicada in (("E1", True), ("E1", False), ("E2", True), ("E3", True), ("E4", False)):
            mes = 1 if publicada else 2
            self._nova_analise(nome, date(2026, mes, 1), date(2026, mes, 28), publicada=publicada)
        with self._sessao() as sessao:
            sessao.add(Notificacao(id_usuario=self.ids["E1"][1], texto="Notificação fictícia legada"))
            sessao.commit()
        antes, estado_antes = self._contagens(), self._estado_legado()

        self.stage = "migração 81287c74387a sobre o legado"
        with self.app.app_context():
            self.db.session.remove()
        self._migrar(upgrade, "head")
        self._limpar_cache_esquema()
        script = ScriptDirectory(MIGRATIONS)
        head = script.get_current_head()
        cadeia = {revisao.revision for revisao in script.iterate_revisions(head, REVISAO_ANTERIOR)}
        self.head = head
        self.check("migration_reaches_head", self._revisao() == head and REVISAO_CONTRATUAL in cadeia,
                   f"revisão={self._revisao()} head={head}")
        depois = self._contagens()
        self.check("migration_preserves_legacy_counts",
                   set(depois) == set(antes) | set(TABELAS_CONTRATUAIS) | set(TABELAS_POSTERIORES)
                   and all(depois[tabela] == quantidade for tabela, quantidade in antes.items())
                   and antes["empresa"] == 4 and antes["analise"] == 5,
                   f"antes={antes} depois={depois}")
        self.check("migration_creates_no_contract_data",
                   all(depois[tabela] == 0 for tabela in TABELAS_CONTRATUAIS + TABELAS_POSTERIORES),
                   str({tabela: depois[tabela] for tabela in TABELAS_CONTRATUAIS + TABELAS_POSTERIORES}))
        esperados = {"E1": "ATIVA", "E2": "SUSPENSA", "E3": "CANCELADA", "E4": "EXCLUIDA"}
        with self._sessao() as sessao:
            situacoes = {nome: obter_situacao_contratual(sessao.get(Empresa, self.ids[nome][0]), instante=T0,
                                                         sessao=sessao)
                         for nome in esperados}
        self.check("migration_preserves_legacy_state",
                   self._estado_legado() == estado_antes
                   and all((s.regime, s.estado) == ("LEGADO", esperados[n]) for n, s in situacoes.items()),
                   str({n: (s.regime, s.estado) for n, s in situacoes.items()}))
        diferencas = self._diferencas_metadata()
        self.check("metadata_matches_models", diferencas == [], repr(diferencas)[:400])

        esperadas = {
            ("contrato", "ck_contrato_situacao", "c"), ("contrato", "ck_contrato_valor", "c"),
            ("contrato", "ck_contrato_vigencia", "c"), ("contrato", "ck_contrato_meses_suspensao", "c"),
            ("contrato", "ck_contrato_versao", "c"), ("contrato", "ck_contrato_anterior_distinto", "c"),
            ("contrato", "ck_contrato_suspensao", "c"), ("contrato", "ck_contrato_ativo_sem_prazos", "c"),
            ("contrato", "ck_contrato_encerrado", "c"), ("contrato", "ck_contrato_cancelado", "c"),
            ("contrato", "uq_contrato_id_empresa", "u"), ("contrato", "uq_contrato_anterior", "u"),
            ("contrato", "fk_contrato_anterior_mesma_empresa", "f"),
            ("cobertura_paga", "fk_cobertura_contrato_mesma_empresa", "f"),
            ("cobertura_paga", "ck_cobertura_periodo", "c"), ("cobertura_paga", "ck_cobertura_referencia", "c"),
            ("cobertura_paga", "ck_cobertura_revogacao", "c"),
            ("cobertura_paga", "uq_cobertura_id_empresa", "u"),
            ("evento_contratual", "fk_evento_contrato_mesma_empresa", "f"),
            ("evento_contratual", "fk_evento_cobertura_mesma_empresa", "f"),
            ("evento_contratual", "ck_evento_tipo", "c"), ("evento_contratual", "ck_evento_origem", "c"),
            ("historico_preco_plano", "ck_historico_preco_valor", "c"),
        }
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                existentes = {tuple(linha) for linha in conexao.execute(text(
                    "SELECT rel.relname, con.conname, con.contype::text FROM pg_constraint con "
                    "JOIN pg_class rel ON rel.oid = con.conrelid "
                    "JOIN pg_namespace esq ON esq.oid = rel.relnamespace "
                    "WHERE esq.nspname = current_schema() AND rel.relname = ANY(:tabelas)"),
                    {"tabelas": list(TABELAS_CONTRATUAIS)})}
                indice = conexao.execute(text(
                    "SELECT ix.indisunique, pg_get_expr(ix.indpred, ix.indrelid), pg_get_indexdef(ix.indexrelid) "
                    "FROM pg_index ix JOIN pg_class i ON i.oid = ix.indexrelid "
                    "JOIN pg_class t ON t.oid = ix.indrelid "
                    "WHERE i.relname = 'uq_contrato_aplicavel_empresa' AND t.relname = 'contrato'")).first()
        self.check("named_constraints", esperadas <= existentes, str(sorted(esperadas - existentes)))
        self.check("partial_unique_index", indice is not None and indice[0] is True and indice[1] is not None
                   and all(parte in indice[1] for parte in ("situacao", "ATIVO", "SUSPENSO"))
                   and "(id_empresa)" in indice[2], repr(indice))

    # -------------------------------------------------- 2. InstanteUTC e prazos
    def _etapa_instante_utc(self):
        from sqlalchemy import func, select, text
        from sqlalchemy.exc import StatementError
        from models import Contrato, InstanteUTC

        self.stage = "InstanteUTC e prazos gravados"
        self._nova_empresa("ES")
        contrato_es = self._contratar("ES")
        self._pagar("ES", contrato_es, date(2026, 1, 1), date(2026, 2, 28), C0)
        self._ativar_regras("ES")
        # Instante oficial informado em São Paulo (10:00 local = 13:00 UTC).
        self._suspender("ES", contrato_es, S_ACESSO.astimezone(SAO_PAULO))
        self.contrato_es = contrato_es

        colunas = [(tabela.name, coluna.name) for tabela in self.db.metadata.sorted_tables
                   if tabela.name in TABELAS_CONTRATUAIS
                   for coluna in tabela.columns if isinstance(coluna.type, InstanteUTC)]
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                tipos = {(t, c): conexao.scalar(text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"),
                    {"t": t, "c": c}) for t, c in colunas}
        self.check("instant_columns_timestamptz", len(colunas) >= 12
                   and set(tipos.values()) == {"timestamp with time zone"}, str(tipos))

        with self._sessao() as sessao:
            # O TimeZone da sessão não altera o instante nem o fuso devolvido.
            sessao.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
            contrato = sessao.get(Contrato, contrato_es)
            valores = [contrato.suspenso_em, contrato.suspensao_limite, contrato.suspensao_limite_original,
                       contrato.pdf_limite, contrato.pdf_limite_original, contrato.data_criacao,
                       contrato.data_atualizacao]
            texto_toquio = sessao.scalar(text("SELECT suspenso_em::text FROM contrato WHERE id_contrato = :i"),
                                         {"i": contrato_es})
            mesmo_instante = sessao.scalar(text(
                "SELECT suspenso_em = TIMESTAMPTZ '2026-03-22 13:00:00+00' FROM contrato WHERE id_contrato = :i"),
                {"i": contrato_es})
            epoca = sessao.scalar(text("SELECT EXTRACT(EPOCH FROM suspensao_limite) FROM contrato "
                                       "WHERE id_contrato = :i"), {"i": contrato_es})
        self.check("instant_roundtrip_utc",
                   all(v is not None and v.tzinfo is UTC for v in valores)
                   and valores[0] == S_ACESSO and texto_toquio.endswith("+09") and mesmo_instante is True
                   and float(epoca) == LIMITE_ACESSO.timestamp(),
                   f"tz={[getattr(v, 'tzinfo', None) for v in valores]} texto={texto_toquio}")
        gravado = self._contrato(contrato_es)
        self.check("calendar_deadlines_persisted",
                   gravado["situacao"] == "SUSPENSO" and gravado["suspenso_em"] == S_ACESSO
                   and gravado["suspensao_limite"] == gravado["suspensao_limite_original"] == LIMITE_ACESSO
                   and gravado["pdf_limite"] == gravado["pdf_limite_original"] == PDF_ACESSO,
                   f"limite={gravado['suspensao_limite']} pdf={gravado['pdf_limite']}")

        def maior(sessao, instante):
            return sessao.scalar(select(func.count()).select_from(Contrato).where(
                Contrato.id_contrato == contrato_es, Contrato.suspensao_limite > instante))

        with self._sessao() as sessao:
            sessao.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
            orm = [maior(sessao, LIMITE_ACESSO - MICRO), maior(sessao, LIMITE_ACESSO),
                   maior(sessao, LIMITE_ACESSO + MICRO), maior(sessao, LIMITE_ACESSO.astimezone(SAO_PAULO)),
                   maior(sessao, (LIMITE_ACESSO - MICRO).astimezone(TOQUIO))]
            bruto = [sessao.scalar(text("SELECT COUNT(*) FROM contrato WHERE id_contrato = :i "
                                        "AND suspensao_limite > :agora"), {"i": contrato_es, "agora": agora})
                     for agora in ((LIMITE_ACESSO - MICRO).astimezone(SAO_PAULO), LIMITE_ACESSO.astimezone(SAO_PAULO))]
        self.check("sql_deadline_comparison", orm == [1, 0, 0, 0, 1] and bruto == [1, 0],
                   f"orm={orm} bruto={bruto}")

        recusas = []
        with self._sessao() as sessao:
            try:
                maior(sessao, datetime(2026, 6, 22, 12, 0))
            except (StatementError, ValueError):
                recusas.append("comparação")
            sessao.rollback()
            contrato = sessao.get(Contrato, contrato_es)
            contrato.data_atualizacao = datetime(2026, 3, 23, 12, 0)
            try:
                sessao.flush()
            except (StatementError, ValueError):
                recusas.append("escrita")
            sessao.rollback()
        self.check("naive_instant_refused", recusas == ["comparação", "escrita"]
                   and self._contrato(contrato_es)["data_atualizacao"] == S_ACESSO, str(recusas))

    # ------------------------------------------- 3. cláusula SQL x política
    def _avaliar(self, instante):
        from sqlalchemy import select
        from contract_policy import obter_situacao_contratual
        from models import Empresa, Usuario
        from session_security import condicoes_empresa_acessivel, usuario_pode_acessar
        self.relogio["agora"] = instante
        resultado = {}
        with self._sessao() as sessao:
            pela_clausula = set(sessao.scalars(select(Empresa.id_empresa).where(
                *condicoes_empresa_acessivel(instante, sessao=sessao))))
            for nome, (id_empresa, id_cliente) in self.ids.items():
                situacao = obter_situacao_contratual(sessao.get(Empresa, id_empresa), instante=instante,
                                                     sessao=sessao)
                acesso = usuario_pode_acessar(sessao.get(Usuario, id_cliente), consulta=sessao)
                resultado[nome] = {
                    "linha": (situacao.regime, situacao.estado, situacao.acesso_portal, acesso,
                              id_empresa in pela_clausula),
                    "pdf": situacao.exportacao_pdf_aberta,
                }
        return resultado

    def _etapa_acesso(self):
        self.stage = "cláusula SQL de acesso x política"
        contratos = {}
        for nome in ("EA", "EN", "EC"):
            self._nova_empresa(nome)
            contratos[nome] = self._contratar(nome)
            self._ativar_regras(nome)
        self._cancelar("EC", contratos["EC"], C0 + timedelta(hours=1))
        # Regras ativas, contrato cancelado e status voltado a ATIVA fora do fluxo contratual.
        self._cancelar("EN", contratos["EN"], C0 + timedelta(hours=1))
        from company_security import aplicar_status_conta, bloquear_empresa
        id_en = self.ids["EN"][0]
        self._operar(lambda s, admin: aplicar_status_conta(s, bloquear_empresa(s, id_en), "ATIVA"))

        instantes = {
            "dentro": S_ACESSO + timedelta(days=1), "pdf-": PDF_ACESSO - MICRO, "pdf": PDF_ACESSO,
            "limite-": LIMITE_ACESSO - MICRO, "limite": LIMITE_ACESSO, "limite+": LIMITE_ACESSO + timedelta(seconds=1),
        }
        matriz = {rotulo: self._avaliar(instante) for rotulo, instante in instantes.items()}

        def todos(nome, esperado):
            obtido = {rotulo: avaliacao[nome]["linha"] for rotulo, avaliacao in matriz.items()}
            return all(linha == esperado for linha in obtido.values()), f"{nome}={obtido}"

        def em(rotulos, nome, esperado):
            obtido = {rotulo: matriz[rotulo][nome]["linha"] for rotulo in rotulos}
            return all(linha == esperado for linha in obtido.values()), f"{nome}={obtido}"

        legados = [todos("E1", ("LEGADO", "ATIVA", True, True, True)),
                   todos("E2", ("LEGADO", "SUSPENSA", True, True, True)),
                   todos("E3", ("LEGADO", "CANCELADA", False, False, False)),
                   todos("E4", ("LEGADO", "EXCLUIDA", False, False, False))]
        self.check("access_legacy", all(ok for ok, _ in legados), " ".join(d for ok, d in legados if not ok))
        self.check("access_active_contract", *todos("EA", ("CONTRATUAL", "ATIVA", True, True, True)))
        self.check("access_suspended_within_deadline",
                   *em(("dentro", "pdf-", "pdf", "limite-"), "ES", ("CONTRATUAL", "SUSPENSA", True, True, True)))
        self.check("access_denied_at_limit",
                   *em(("limite",), "ES", ("CONTRATUAL", "SUSPENSAO_EXPIRADA", False, False, False)))
        self.check("access_denied_after_limit",
                   *em(("limite+",), "ES", ("CONTRATUAL", "SUSPENSAO_EXPIRADA", False, False, False)))
        self.check("access_denied_without_contract", *todos("EN", ("CONTRATUAL", "SEM_CONTRATO", False, False, False)))
        self.check("access_denied_cancelled", *todos("EC", ("CONTRATUAL", "CANCELADA", False, False, False)))
        pdf = {rotulo: matriz[rotulo]["ES"]["pdf"] for rotulo in ("dentro", "pdf-", "pdf", "limite-")}
        self.check("pdf_export_limit_exclusive",
                   pdf == {"dentro": True, "pdf-": True, "pdf": False, "limite-": False}, str(pdf))
        divergencias = {(rotulo, nome): avaliacao["linha"] for rotulo, por_nome in matriz.items()
                        for nome, avaliacao in por_nome.items()
                        if len(set(avaliacao["linha"][2:])) != 1}
        self.check("sql_clause_matches_policy", not divergencias, str(divergencias))

    # ---------------------------------------- 4. reconciliações concorrentes
    def _etapa_reconciliacao_concorrente(self):
        import contract_admin
        from sqlalchemy import func, select
        from sqlalchemy.orm import Session

        self.stage = "reconciliações concorrentes"
        id_er = self._nova_empresa("ER")
        contrato_er = self._contratar("ER")
        self._ativar_regras("ER")
        self._suspender("ER", contrato_er, S_RECONCILIACAO)
        instante = self._contrato(contrato_er)["suspensao_limite"] + timedelta(hours=1)
        self.relogio["agora"] = instante
        _, geracao_antes = self._empresa("ER")

        # Outra transação segurando o lock consultivo: aplicação não altera nada.
        with self.app.app_context():
            with self.db.engine.connect() as trava:
                trava.execute(select(func.pg_advisory_xact_lock(contract_admin.CHAVE_LOCK_RECONCILIACAO)))
                with Session(bind=self.db.engine) as sessao:
                    bloqueada = self._resumo(contract_admin.reconciliar_vencimentos(
                        sessao, aplicar=True, instante=instante))
                    sessao.commit()
                    simulacao = self._resumo(contract_admin.reconciliar_vencimentos(
                        sessao, aplicar=False, instante=instante))
                    sessao.rollback()
                trava.rollback()
        self.check("advisory_lock_blocks_reconciliation",
                   bloqueada == {"bloqueada": True, "candidatos": [], "cancelados": [], "revogados": []}
                   and simulacao["candidatos"] == [id_er] and simulacao["cancelados"] == []
                   and self._contrato(contrato_er)["situacao"] == "SUSPENSO", f"{bloqueada} {simulacao}")

        barreira = threading.Barrier(2, timeout=20)

        def reconciliar(sessao, admin):
            barreira.wait()
            resumo = self._resumo(contract_admin.reconciliar_vencimentos(sessao, aplicar=True, instante=instante))
            sessao.commit()
            return resumo

        execucoes = [self._thread(reconciliar) for _ in range(2)]
        for thread, _ in execucoes:
            thread.join(30)
        saidas = [saida for _, saida in execucoes]
        resumos = [saida.get("valor") for saida in saidas]
        concluidas = all(not thread.is_alive() for thread, _ in execucoes) and all(
            "erro" not in saida and resumo is not None for saida, resumo in zip(saidas, resumos))
        cancelados = [i for resumo in resumos if resumo for i in resumo["cancelados"]]
        revogados = [i for resumo in resumos if resumo for i in resumo["revogados"]]
        bloqueadas = sorted(bool(resumo and resumo["bloqueada"]) for resumo in resumos)
        status, geracao = self._empresa("ER")
        eventos = self._eventos("ER", "CANCELAMENTO")
        contrato = self._contrato(contrato_er)
        # Desfechos aceitos: uma execução bloqueada pelo lock consultivo, ou as
        # duas executadas em série (a segunda já sem candidatos).
        self.check("concurrent_reconciliation_single_cancellation",
                   concluidas and cancelados == [id_er] and bloqueadas in ([False, True], [False, False])
                   and eventos == ["RECONCILIACAO"] and contrato["situacao"] == "CANCELADO"
                   and contrato["cancelamento_motivo"] == contract_admin.MOTIVO_EXPIRACAO and status == "CANCELADA",
                   f"saidas={[type(s.get('erro')).__name__ for s in saidas]} resumos={resumos} eventos={eventos}")
        self.check("concurrent_reconciliation_single_revocation",
                   revogados == [self.ids["ER"][1]] and geracao == geracao_antes + 1,
                   f"revogados={revogados} geracao={geracao_antes}->{geracao}")
        terceira = self._operar(lambda s, admin: self._resumo(contract_admin.reconciliar_vencimentos(
            s, aplicar=True, instante=instante + timedelta(hours=1))))
        self.check("reconciliation_idempotent",
                   terceira == {"bloqueada": False, "candidatos": [], "cancelados": [], "revogados": []}
                   and self._eventos("ER", "CANCELAMENTO") == ["RECONCILIACAO"]
                   and self._empresa("ER") == ("CANCELADA", geracao_antes + 1)
                   and self._acesso("ER", instante) is False, str(terceira))

    # ---------------------------------------- 5. contratações concorrentes
    def _contratos_da_empresa(self, nome):
        from sqlalchemy import select
        from models import Contrato
        with self._sessao() as sessao:
            return sessao.execute(select(Contrato.id_contrato, Contrato.situacao).where(
                Contrato.id_empresa == self.ids[nome][0]).order_by(Contrato.id_contrato)).all()

    def _etapa_contratacao_concorrente(self):
        import contract_admin
        from contract_admin import ErroContratual
        from sqlalchemy.exc import IntegrityError
        from models import Contrato

        self.stage = "contratações concorrentes (lock da empresa)"
        id_ek = self._nova_empresa("EK")
        dados = self._dados()
        pids, a_pronto, liberar_a, b_iniciada = {}, threading.Event(), threading.Event(), threading.Event()

        def criar_primeiro(sessao, admin):
            pids["a"] = self._pid(sessao)
            id_contrato = contract_admin.criar_contrato(sessao, id_ek, dados, responsavel=admin,
                                                        instante=T0).contrato.id_contrato
            a_pronto.set()
            if not liberar_a.wait(30):
                raise RuntimeError("liberação não recebida")
            sessao.commit()
            return id_contrato

        def criar_segundo(sessao, admin):
            pids["b"] = self._pid(sessao)
            b_iniciada.set()
            id_contrato = contract_admin.criar_contrato(sessao, id_ek, dados, responsavel=admin,
                                                        instante=T0).contrato.id_contrato
            sessao.commit()
            return id_contrato

        thread_a, saida_a = self._thread(criar_primeiro)
        thread_b, saida_b = None, {}
        try:
            esperou = a_pronto.wait(20)
            if esperou:
                thread_b, saida_b = self._thread(criar_segundo)
                esperou = b_iniciada.wait(20) and self._aguardar_bloqueio(pids.get("b"), pids.get("a"))
        finally:
            liberar_a.set()
        for thread in (thread_a, thread_b):
            if thread is not None:
                thread.join(30)
        self.check("concurrent_contract_waits_company_lock", esperou, str(pids))
        contratos = self._contratos_da_empresa("EK")
        self.check("concurrent_contract_single_vigente",
                   isinstance(saida_a.get("valor"), int) and "erro" not in saida_a
                   and isinstance(saida_b.get("erro"), (ErroContratual, IntegrityError))
                   and contratos == [(saida_a["valor"], "ATIVO")]
                   and self._eventos("EK", "CONTRATACAO") == ["ADMIN"],
                   f"a={type(saida_a.get('erro')).__name__} b={type(saida_b.get('erro')).__name__} {contratos}")

        self.stage = "contratações concorrentes (corrida livre)"
        id_ek2 = self._nova_empresa("EK2")
        barreira = threading.Barrier(2, timeout=20)

        def criar(sessao, admin):
            barreira.wait()
            id_contrato = contract_admin.criar_contrato(sessao, id_ek2, dados, responsavel=admin,
                                                        instante=T0).contrato.id_contrato
            sessao.commit()
            return id_contrato

        execucoes = [self._thread(criar) for _ in range(2)]
        for thread, _ in execucoes:
            thread.join(30)
        saidas = [saida for _, saida in execucoes]
        sucessos = [saida["valor"] for saida in saidas if "valor" in saida and "erro" not in saida]
        erros = [saida["erro"] for saida in saidas if "erro" in saida]
        contratos = self._contratos_da_empresa("EK2")
        self.check("concurrent_contract_race_single_vigente",
                   len(sucessos) == 1 and len(erros) == 1
                   and isinstance(erros[0], (ErroContratual, IntegrityError))
                   and contratos == [(sucessos[0], "ATIVO")] and self._eventos("EK2", "CONTRATACAO") == ["ADMIN"],
                   f"erros={[type(e).__name__ for e in erros]} {contratos}")

        # O índice parcial barra um segundo vigente mesmo fora de contract_admin,
        # mas não restringe contratos encerrados/cancelados.
        resultado = []
        for situacao, extras in (("ATIVO", {}), ("ENCERRADO", {"encerrado_em": T0})):
            with self._sessao() as sessao:
                sessao.add(Contrato(id_empresa=id_ek, id_plano=self.plano_id,
                                    valor_mensal_contratado=Decimal("1.00"), data_inicio=date(2026, 1, 1),
                                    situacao=situacao, id_usuario_admin_responsavel=self.admin_id, versao=1,
                                    data_criacao=T0, **extras))
                try:
                    sessao.flush()
                    resultado.append((situacao, "aceito"))
                except IntegrityError as erro:
                    resultado.append((situacao, getattr(erro.orig, "pgcode", None)))
                sessao.rollback()
        self.check("partial_unique_index_enforced",
                   resultado == [("ATIVO", "23505"), ("ENCERRADO", "aceito")]
                   and len(self._contratos_da_empresa("EK")) == 1, str(resultado))

    # ------------------------------------- 6. prorrogação x reconciliação
    def _etapa_prorrogacao_concorrente(self):
        import contract_admin
        from contract_admin import ErroContratual
        from models import Contrato

        self.stage = "prorrogação confirmada antes da reconciliação"
        id_ep = self._nova_empresa("EP")
        contrato_ep = self._contratar("EP")
        self._ativar_regras("EP")
        self._suspender("EP", contrato_ep, S_PRORROGACAO)
        limite = self._contrato(contrato_ep)["suspensao_limite"]
        instante = limite + timedelta(hours=1)  # já vencido, ainda não reconciliado
        novo_limite = instante + timedelta(days=30)
        self.relogio["agora"] = instante
        _, geracao_antes = self._empresa("EP")
        pids, p_pronto, liberar_p, q_iniciada = {}, threading.Event(), threading.Event(), threading.Event()

        def prorrogar(sessao, admin):
            pids["p"] = self._pid(sessao)
            versao = sessao.get(Contrato, contrato_ep).versao
            contract_admin.registrar_prorrogacao(
                sessao, id_ep, contrato_ep, versao, novo_limite=novo_limite,
                justificativa="Acordo fictício registrado", responsavel=admin, instante=instante)
            p_pronto.set()
            if not liberar_p.wait(30):
                raise RuntimeError("liberação não recebida")
            sessao.commit()
            return True

        def reconciliar(sessao, admin):
            pids["q"] = self._pid(sessao)
            q_iniciada.set()
            resumo = self._resumo(contract_admin.reconciliar_vencimentos(sessao, aplicar=True, instante=instante))
            sessao.commit()
            return resumo

        thread_p, saida_p = self._thread(prorrogar)
        thread_q, saida_q = None, {}
        try:
            esperou = p_pronto.wait(20)
            if esperou:
                thread_q, saida_q = self._thread(reconciliar)
                esperou = q_iniciada.wait(20) and self._aguardar_bloqueio(pids.get("q"), pids.get("p"))
        finally:
            liberar_p.set()
        for thread in (thread_p, thread_q):
            if thread is not None:
                thread.join(30)
        self.check("reconciliation_waits_extension_lock", esperou, str(pids))
        contrato = self._contrato(contrato_ep)
        self.check("extension_first_prevents_cancellation",
                   saida_p.get("valor") is True and "erro" not in saida_p and "erro" not in saida_q
                   and saida_q.get("valor") == {"bloqueada": False, "candidatos": [id_ep], "cancelados": [],
                                                "revogados": []}
                   and contrato["situacao"] == "SUSPENSO" and contrato["suspensao_limite"] == novo_limite
                   and contrato["suspensao_limite_original"] == limite
                   and self._eventos("EP", "CANCELAMENTO") == [] and self._eventos("EP", "PRORROGACAO") == ["ADMIN"]
                   and self._empresa("EP") == ("SUSPENSA", geracao_antes) and self._acesso("EP", instante) is True,
                   f"p={type(saida_p.get('erro')).__name__} q={saida_q.get('valor')} "
                   f"q_erro={type(saida_q.get('erro')).__name__} situacao={contrato['situacao']}")

        self.stage = "reconciliação confirmada antes da prorrogação"
        id_eq = self._nova_empresa("EQ")
        contrato_eq = self._contratar("EQ")
        self._ativar_regras("EQ")
        self._suspender("EQ", contrato_eq, S_RECONCILIACAO_PRIMEIRO)
        instante = self._contrato(contrato_eq)["suspensao_limite"] + timedelta(hours=1)
        self.relogio["agora"] = instante
        _, geracao_antes = self._empresa("EQ")
        r_pronta, liberar_r, p_iniciada = threading.Event(), threading.Event(), threading.Event()

        def reconciliar_primeiro(sessao, admin):
            pids["r"] = self._pid(sessao)
            resumo = self._resumo(contract_admin.reconciliar_vencimentos(sessao, aplicar=True, instante=instante))
            r_pronta.set()
            if not liberar_r.wait(30):
                raise RuntimeError("liberação não recebida")
            sessao.commit()
            return resumo

        def prorrogar_depois(sessao, admin):
            pids["p2"] = self._pid(sessao)
            versao = sessao.get(Contrato, contrato_eq).versao
            p_iniciada.set()
            contract_admin.registrar_prorrogacao(
                sessao, id_eq, contrato_eq, versao, novo_limite=instante + timedelta(days=30),
                justificativa="Acordo fictício tardio", responsavel=admin, instante=instante)
            sessao.commit()
            return True

        thread_r, saida_r = self._thread(reconciliar_primeiro)
        thread_p, saida_p = None, {}
        try:
            esperou = r_pronta.wait(20)
            if esperou:
                thread_p, saida_p = self._thread(prorrogar_depois)
                esperou = p_iniciada.wait(20) and self._aguardar_bloqueio(pids.get("p2"), pids.get("r"))
        finally:
            liberar_r.set()
        for thread in (thread_r, thread_p):
            if thread is not None:
                thread.join(30)
        contrato = self._contrato(contrato_eq)
        self.check("reconciliation_first_rejects_extension",
                   esperou and "erro" not in saida_r
                   and saida_r.get("valor", {}).get("cancelados") == [id_eq]
                   and isinstance(saida_p.get("erro"), ErroContratual)
                   and contrato["situacao"] == "CANCELADO"
                   and self._eventos("EQ", "CANCELAMENTO") == ["RECONCILIACAO"]
                   and self._eventos("EQ", "PRORROGACAO") == []
                   and self._empresa("EQ") == ("CANCELADA", geracao_antes + 1),
                   f"esperou={esperou} r={saida_r.get('valor')} p={type(saida_p.get('erro')).__name__} "
                   f"situacao={contrato['situacao']}")

    # ------------------------------------------------ 7. exclusão permanente
    def _etapa_exclusao_permanente(self):
        import contract_admin
        from sqlalchemy import func, select
        from models import Analise, Empresa, Notificacao, Usuario

        self.stage = "exclusão permanente com histórico contratual"
        self.relogio["agora"] = T0
        id_ex = self._nova_empresa("EX")
        id_cliente_ex = self.ids["EX"][1]
        self._nova_analise("EX", date(2026, 1, 1), date(2026, 1, 31), publicada=True)
        with self._sessao() as sessao:
            sessao.add(Notificacao(id_usuario=id_cliente_ex, texto="Notificação fictícia EX"))
            sessao.commit()
        primeiro = self._contratar("EX")
        cobertura_jan = self._pagar("EX", primeiro, date(2026, 1, 1), date(2026, 1, 31), C0)
        self._ativar_regras("EX")
        versao, dados = self._contrato(primeiro)["versao"], self._dados(data_inicio=date(2026, 2, 1))
        segundo = self._operar(lambda s, admin: contract_admin.renovar_contrato(
            s, id_ex, primeiro, versao, dados, responsavel=admin, justificativa="Renovação fictícia",
            instante=C0 + timedelta(hours=1)).contrato.id_contrato)
        self._pagar("EX", segundo, date(2026, 2, 1), date(2026, 2, 28), C0 + timedelta(hours=2))
        self._operar(lambda s, admin: contract_admin.revogar_cobertura(
            s, id_ex, cobertura_jan, motivo="Estorno fictício", responsavel=admin,
            instante=C0 + timedelta(hours=3)))
        antes = self._contratuais_da_empresa(id_ex)
        outras_antes = self._contratuais_da_empresa(id_ex, outras=True)
        encadeado = self._contrato(segundo)["id_contrato_anterior"] == primeiro

        admin, resposta = self._login("admin-contratos@example.invalid")
        logado = resposta.status_code == 302
        lixeira = admin.post(f"/admin/empresa/excluir/{id_ex}",
                             data={"csrf_token": self._csrf(admin, "/admin/empresas")})
        exclusao = admin.post(f"/admin/empresa/excluir-permanente/{id_ex}",
                              data={"csrf_token": self._csrf(admin, "/admin/lixeira"),
                                    "senha_confirmacao": PASSWORD})
        pagina = admin.get("/admin/lixeira").get_data(as_text=True)
        depois = self._contratuais_da_empresa(id_ex)
        with self._sessao() as sessao:
            restos = (sessao.get(Empresa, id_ex), sessao.get(Usuario, id_cliente_ex),
                      sessao.scalar(select(func.count()).select_from(Analise).where(Analise.id_empresa == id_ex)),
                      sessao.scalar(select(func.count()).select_from(Notificacao).where(
                          Notificacao.id_usuario == id_cliente_ex)))
        # Histórico contratual é registro financeiro e de auditoria: a exclusão
        # permanente é recusada e nada é apagado (retenção pendente de decisão).
        self.check("permanent_delete_blocked_with_contract_history",
                   logado and encadeado and lixeira.status_code == 302 and exclusao.status_code == 302
                   and "/admin/lixeira" in exclusao.headers.get("Location", "")
                   and antes == {"regime_contratual": 1, "contrato": 2, "cobertura_paga": 2, "evento_contratual": 6}
                   and depois == antes and restos[0] is not None and restos[1] is not None and restos[2] > 0
                   and "possui histórico contratual" in pagina and "excluídos permanentemente" not in pagina,
                   f"login={resposta.status_code} lixeira={lixeira.status_code} exclusao={exclusao.status_code} "
                   f"antes={antes} depois={depois} restos={[r is None if not isinstance(r, int) else r for r in restos]}")
        self.check("permanent_delete_keeps_other_companies",
                   self._contratuais_da_empresa(id_ex, outras=True) == outras_antes
                   and all(n > 0 for n in outras_antes.values()), str(outras_antes))
        del self.ids["EX"]

    # ---------------------------------------------- 8. downgrade e novo upgrade
    def _etapa_downgrade(self):
        from flask_migrate import downgrade, upgrade
        from sqlalchemy import text
        from contract_policy import esquema_contratual_disponivel

        self.stage = "downgrade com dados contratuais"
        self.relogio["agora"] = T0
        with self.app.app_context():
            self.db.session.remove()
        antes = self._contagens()
        with self.app.app_context():
            try:
                downgrade(directory=MIGRATIONS, revision=REVISAO_ANTERIOR)
                recusado = False
            except SystemExit as saida:
                recusado = saida.code == 1
        self._limpar_cache_esquema()
        self.check("downgrade_refused_with_contract_data",
                   recusado and self._revisao() == self.head and self._contagens() == antes
                   and all(antes[tabela] > 0 for tabela in TABELAS_POR_EMPRESA),
                   f"recusado={recusado} revisão={self._revisao()}")

        self.stage = "downgrade com tabelas vazias"
        with self.app.app_context():
            with self.db.engine.begin() as conexao:
                for comando in ("DELETE FROM evento_contratual", "DELETE FROM cobertura_paga",
                                "UPDATE contrato SET id_contrato_anterior = NULL", "DELETE FROM contrato",
                                "DELETE FROM regime_contratual", "DELETE FROM historico_preco_plano"):
                    conexao.execute(text(comando))
        # O downgrade até e4c004a26b01 remove as tabelas contratuais E as posteriores
        # (sessao_autenticada é descendente da migração contratual na cadeia).
        removidas = set(TABELAS_CONTRATUAIS) | set(TABELAS_POSTERIORES)
        legado = {tabela: n for tabela, n in self._contagens().items() if tabela not in removidas}
        self._migrar(downgrade, REVISAO_ANTERIOR)
        self._limpar_cache_esquema()
        self.check("downgrade_with_empty_tables",
                   self._revisao() == REVISAO_ANTERIOR and not self._tabelas() & removidas
                   and self._contagens() == legado, f"revisão={self._revisao()}")

        self._restaurar_esquema_posterior()  # ver _COLUNAS_POSTERIORES
        self.stage = "portal legado sem o esquema contratual"
        with self._sessao() as sessao:
            sem_esquema = esquema_contratual_disponivel(sessao) is False
        cliente, resposta = self._login("cliente-e1@example.invalid")
        painel = cliente.get("/cliente/dashboard").status_code
        self.check("legacy_access_without_schema",
                   sem_esquema and resposta.status_code == 302 and painel == 200
                   and self._acesso("E1", T0) is True and self._acesso("E2", T0) is True
                   and self._acesso("E3", T0) is False,
                   f"login={resposta.status_code} painel={painel}")

        self.stage = "novo upgrade até o head"
        self._remover_esquema_posterior()  # as migrações o recriam; sessões legadas caem
        self._migrar(upgrade, "head")
        self._limpar_cache_esquema()
        contagens = self._contagens()
        with self._sessao() as sessao:
            com_esquema = esquema_contratual_disponivel(sessao) is True
        diferencas = self._diferencas_metadata()
        cliente, resposta = self._login("cliente-e1@example.invalid")
        self.check("upgrade_again_after_downgrade",
                   self._revisao() == self.head and com_esquema and diferencas == []
                   and all(contagens[tabela] == 0 for tabela in TABELAS_CONTRATUAIS + TABELAS_POSTERIORES)
                   and {t: n for t, n in contagens.items() if t not in removidas} == legado
                   and resposta.status_code == 302 and self._acesso("E1", T0) is True,
                   f"revisão={self._revisao()} diferencas={repr(diferencas)[:300]} login={resposta.status_code}")


def _worker_main(bin_dir, work_dir):
    cluster = _PrivateCluster(Path(bin_dir).resolve(), Path(work_dir).resolve())
    # Não encaminha mensagens de Flask, SQLAlchemy, Alembic ou PostgreSQL.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            report = cluster.run()
    except Exception as error:
        detail = f": {error}" if isinstance(error, AssertionError) else ""
        print(json.dumps({"status": "error", "stage": cluster.stage, "error": type(error).__name__ + detail}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--isolated-worker":
        raise SystemExit(_worker_main(sys.argv[2], sys.argv[3]))
    unittest.main()
