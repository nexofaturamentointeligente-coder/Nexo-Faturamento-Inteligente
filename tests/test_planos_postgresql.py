"""Planos por nível e Raio-X em PostgreSQL real: RLS, políticas da Data API, catálogo e downgrade.

Por padrão o ensaio é ignorado. Para habilitar, indique apenas o diretório dos
binários PostgreSQL em NEXO_TEST_POSTGRES_BIN e execute:
    python -B -m unittest tests.test_planos_postgresql -v

Mesmo protocolo de tests/test_contratos_postgresql.py, de onde vem o cluster temporário:
um processo separado, com ambiente filtrado e dotenv desabilitado, e nenhum banco ou
servidor existente. Os dados são fictícios. O cluster recebe os papéis com os nomes da
Data API do Supabase (anon, authenticated, service_role) e postgres, e o privilégio padrão
que o Supabase dá a anon e authenticated nas tabelas novas: a revisão b3e9d6c1f7a2 precisa
revogá-lo, e a política restritiva precisa negar até a quem receber GRANT depois. O
cálculo do Raio-X roda no próprio processo (o worker isolado exige Linux).

Complementa tests/test_planos_raio_x.py, que cobre as regras em SQLite; aqui só o que
depende do PostgreSQL real:
  1. código no ar antes da migração: menu de sempre e rotas novas em 503, sem abortar a
     transação de ninguém;
  2. a revisão aplicada com o processo no ar: modelo, catálogo, RLS sem FORCE, políticas
     por papel, privilégios revogados e o dono da tabela sem restrição;
  3. menu e 403 do Bronze, Raio-X gravado em NUMERIC e a margem liberada no Prata;
  4. downgrade recusado com Raio-X gravado, aceito sem ele (levando as políticas), portal
     no ar com o cache antigo e novo upgrade.
"""

from datetime import date
from decimal import Decimal
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.raio_x_support import FEVEREIRO, JANEIRO, executar_como_o_worker
from tests.test_contratos_postgresql import (
    MIGRATIONS, PASSWORD, ROOT, TOOLS, _PrivateCluster, _safe_environment, _tool,
)


REVISAO_ANTERIOR = "a8d3f6b91c24"
REVISAO_PLANOS = "b3e9d6c1f7a2"
TABELAS = ("plano_nivel", "funcionalidade", "plano_funcionalidade", "diagnostico_entrada")
CATALOGO = {"plano_nivel": 4, "funcionalidade": 15, "plano_funcionalidade": 38}
POLITICAS = {
    ("deny_api_access", "RESTRICTIVE", "anon,authenticated", "false", "false"),
    ("acesso_servidor", "PERMISSIVE", "postgres,service_role", "true", "true"),
}
CHECKS = (
    "private_engine",
    # 1. Antes da migração
    "schema_before_migration", "portal_without_schema", "routes_503_without_schema",
    # 2. Migração com o processo no ar
    "absence_cached_up_to_30s", "presence_after_recheck", "migration_reaches_head", "metadata_matches_models",
    "catalog_seeded", "rls_enabled_without_force", "policies_for_api_and_server_roles",
    "api_roles_lose_default_privileges", "restrictive_policy_denies_even_with_grant", "owner_reads_catalog",
    # 3. Entitlements e Raio-X
    "bronze_menu_and_403", "raio_x_saved", "prata_sees_margin", "other_company_sees_nothing",
    # 4. Downgrade
    "downgrade_refused_with_raio_x", "downgrade_removes_tables_and_policies",
    "portal_after_downgrade_process_running", "upgrade_again",
)


class PostgreSQLPlanosTests(unittest.TestCase):
    def test_planos_por_nivel_em_postgresql_real(self):
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
        with tempfile.TemporaryDirectory(prefix="nexo-planos-pg-") as work:
            work_dir = Path(work).resolve()
            data_dir = work_dir / "private-cluster"
            try:
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "tests.test_planos_postgresql", "--isolated-worker",
                     str(bin_dir), str(work_dir)],
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
        print("\nEnsaio PostgreSQL planos por nível: " + json.dumps(report, ensure_ascii=True, sort_keys=True))


class _ClusterDosPlanos(_PrivateCluster):
    """Auxiliar do processo isolado; não é um caso descoberto pelo unittest."""

    def _etapas(self):
        with patch("etl_runtime.run_raio_x", side_effect=executar_como_o_worker):
            self._etapa_antes_da_migracao()
            self._etapa_migracao_com_o_processo_no_ar()
            self._etapa_portal()
            self._etapa_downgrade_dos_planos()

    @staticmethod
    def _limpar_cache_esquema():
        import entitlements
        _PrivateCluster._limpar_cache_esquema()
        entitlements._esquema_por_engine.clear()

    def _sql(self, consulta, **parametros):
        from sqlalchemy import text
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                return conexao.execute(text(consulta), parametros).all()

    def _executar(self, *comandos):
        from sqlalchemy import text
        with self.app.app_context():
            with self.db.engine.begin() as conexao:
                for comando in comandos:
                    conexao.execute(text(comando))

    def _politicas(self):
        linhas = self._sql(
            "SELECT tablename, policyname, permissive, "
            "array_to_string(ARRAY(SELECT unnest(roles) ORDER BY 1), ','), qual, with_check "
            "FROM pg_policies WHERE schemaname = current_schema() AND tablename = ANY(:tabelas)",
            tabelas=list(TABELAS),
        )
        politicas = {}
        for tabela, nome, tipo, papeis, usando, checando in linhas:
            politicas.setdefault(tabela, set()).add((nome, tipo, papeis, usando, checando))
        return politicas

    # ------------------------------------------------- 1. antes da migração
    def _etapa_antes_da_migracao(self):
        from flask_migrate import upgrade
        from models import Plano, Segmento, Usuario

        self.stage = "migração até a revisão anterior aos planos"
        self._migrar(upgrade, REVISAO_ANTERIOR)
        self._limpar_cache_esquema()
        self.check("schema_before_migration",
                   self._revisao() == REVISAO_ANTERIOR and not self._tabelas() & set(TABELAS))

        self.stage = "dados fictícios"
        with self._sessao() as sessao:
            bronze = Plano(nome_plano="BRONZE", valor_mensal=Decimal("100"), qtd_analises_mes=1,
                           tipo_analise_permitida="MENSAL", nivel_entrega_analise="BASICA",
                           nivel_dashboard="RESUMIDO", nivel_atendimento="BAIXO")
            prata = Plano(nome_plano="PRATA", valor_mensal=Decimal("300"), qtd_analises_mes=1,
                          tipo_analise_permitida="MENSAL", nivel_entrega_analise="COMPLETA",
                          nivel_dashboard="GERENCIAL", nivel_atendimento="MEDIO")
            segmento = Segmento(nome_segmento="Planos por nível sintéticos")
            admin = Usuario(nome="Admin fictício", email="admin-planos@example.invalid", role="ADMIN",
                            primeiro_acesso=False)
            admin.set_senha(PASSWORD)
            sessao.add_all([bronze, prata, segmento, admin])
            sessao.commit()
            self.plano_id, self.prata_id = bronze.id_plano, prata.id_plano
            self.segmento_id, self.admin_id = segmento.id_segmento, admin.id_usuario
        self._nova_empresa("E1", senha=True)
        self._nova_empresa("E2", senha=True)

        self.stage = "papéis da Data API do Supabase"
        self._executar(
            'CREATE ROLE "anon" NOLOGIN', 'CREATE ROLE "authenticated" NOLOGIN',
            'CREATE ROLE "service_role" NOLOGIN BYPASSRLS', 'CREATE ROLE "postgres" NOLOGIN',
            # O que o Supabase faz no schema public: toda tabela nova nasce acessível pela Data API.
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated",
        )

        self.stage = "portal antes da migração"
        self.cliente_web, resposta = self._login("cliente-e1@example.invalid")
        painel = self.cliente_web.get("/cliente/dashboard")
        html = painel.get_data(as_text=True)
        self.check("portal_without_schema", resposta.status_code == 302 and painel.status_code == 200
                   and "Raio-X do PDV" not in html and "UPGRADE" not in html,
                   f"login={resposta.status_code} painel={painel.status_code}")
        telas = [self.cliente_web.get(caminho).status_code
                 for caminho in ("/onboarding/diagnostico", "/api/onboarding/diagnostico", "/onboarding/planos")]
        self.check("routes_503_without_schema", telas == [503, 503, 503], str(telas))

    # ------------------------------------------------- 2. migração com o processo no ar
    def _etapa_migracao_com_o_processo_no_ar(self):
        from alembic.script import ScriptDirectory
        from flask_migrate import upgrade
        import entitlements

        self.stage = "migração com o processo no ar"
        with self.app.app_context():
            self.db.session.remove()
        self._migrar(upgrade, "head")
        with self.app.app_context():
            ausente = entitlements.esquema_planos_disponivel()
            entitlements._esquema_por_engine[self.db.engine] = time.monotonic() - 31
            presente = entitlements.esquema_planos_disponivel()
            self.db.session.remove()
        self.check("absence_cached_up_to_30s", ausente is False)
        self.check("presence_after_recheck", presente is True)
        self.head = ScriptDirectory(MIGRATIONS).get_current_head()
        cadeia = {r.revision for r in ScriptDirectory(MIGRATIONS).iterate_revisions(self.head, REVISAO_ANTERIOR)}
        self.check("migration_reaches_head", self._revisao() == self.head and REVISAO_PLANOS in cadeia
                   and set(TABELAS) <= self._tabelas(), f"revisão={self._revisao()}")
        diferencas = self._diferencas_metadata()
        self.check("metadata_matches_models", diferencas == [], repr(diferencas)[:400])

        contagens = self._contagens()
        matriz = {}
        for codigo, chave in self._sql("SELECT pf.codigo_plano, f.chave FROM plano_funcionalidade pf "
                                       "JOIN funcionalidade f ON f.id_funcionalidade = pf.id_funcionalidade"):
            matriz.setdefault(codigo, set()).add(chave)
        niveis = [tuple(n) for n in self._sql("SELECT codigo, nivel_hierarquico, limite_historico_meses "
                                              "FROM plano_nivel ORDER BY nivel_hierarquico")]
        self.check("catalog_seeded",
                   all(contagens[t] == n for t, n in CATALOGO.items()) and contagens["diagnostico_entrada"] == 0
                   and niveis == [("BRONZE", 1, 6), ("PRATA", 2, 24), ("OURO", 3, None), ("CUSTOM", 4, None)]
                   and matriz["BRONZE"] < matriz["PRATA"] < matriz["OURO"] < matriz["CUSTOM"],
                   str({t: contagens.get(t) for t in TABELAS}))

        rls = {nome: (ligado, forcado) for nome, ligado, forcado in self._sql(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = ANY(:tabelas) AND relkind = 'r'", tabelas=list(TABELAS))}
        self.check("rls_enabled_without_force", rls == {t: (True, False) for t in TABELAS}, str(rls))
        politicas = self._politicas()
        self.check("policies_for_api_and_server_roles", politicas == {t: POLITICAS for t in TABELAS},
                   str(politicas)[:400])
        privilegios = [tuple(linha) for linha in self._sql(
            "SELECT papel, tabela FROM unnest(ARRAY['anon', 'authenticated']) papel, unnest(:tabelas) tabela "
            "WHERE has_table_privilege(papel, tabela, 'SELECT') OR has_table_privilege(papel, tabela, 'INSERT') "
            "OR has_table_privilege(papel, tabela, 'TRUNCATE')", tabelas=list(TABELAS))]
        self.check("api_roles_lose_default_privileges", privilegios == [], str(privilegios))

        self.stage = "política restritiva com GRANT posterior"
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError
        self._executar("GRANT SELECT, INSERT ON plano_nivel TO anon")
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                transacao = conexao.begin()
                conexao.execute(text("SET LOCAL ROLE anon"))
                visiveis = conexao.scalar(text("SELECT count(*) FROM plano_nivel"))
                try:
                    conexao.execute(text(
                        "INSERT INTO plano_nivel (codigo, nome, modelo_atendimento, atendimento, nivel_hierarquico, "
                        "criado_em) VALUES ('XX', 'Invasor', 'X', 'X', 99, now())"))
                    inserido = True
                except DBAPIError:
                    inserido = False
                transacao.rollback()
        self._executar("REVOKE ALL ON plano_nivel FROM anon")
        self.check("restrictive_policy_denies_even_with_grant", visiveis == 0 and inserido is False,
                   f"visiveis={visiveis} inserido={inserido}")
        self.check("owner_reads_catalog", self._sql("SELECT count(*) FROM plano_nivel")[0][0] == 4)

    # ------------------------------------------------- 3. entitlements e Raio-X
    def _etapa_portal(self):
        self.stage = "menu e 403 do Bronze"
        html = self.cliente_web.get("/cliente/dashboard").get_data(as_text=True)
        negado = self.cliente_web.get("/api/onboarding/diagnostico/auditoria")
        self.check("bronze_menu_and_403",
                   'href="/onboarding/planos#prata"' in html and "Auditoria de custos" in html
                   and negado.status_code == 403 and negado.get_json()["plano_necessario"]["codigo"] == "PRATA",
                   f"status={negado.status_code}")

        self.stage = "Raio-X gravado"
        dados = {"csrf_token": self._csrf(self.cliente_web, "/onboarding/diagnostico"),
                 "arquivos": [(io.BytesIO(JANEIRO), "jan.csv"), (io.BytesIO(FEVEREIRO), "fev.csv")]}
        resposta = self.cliente_web.post("/onboarding/diagnostico", data=dados, content_type="multipart/form-data")
        linha = [tuple(l) for l in self._sql(
            "SELECT plano_recomendado, valor_recuperavel, indice_saude, periodo_inicio, periodo_fim "
            "FROM diagnostico_entrada WHERE id_empresa = :e", e=self.ids["E1"][0])]
        self.check("raio_x_saved", resposta.status_code == 303 and linha == [
            ("PRATA", Decimal("30.0"), Decimal("63.08"), date(2026, 1, 1), date(2026, 2, 28))], str(linha))

        self.stage = "Prata vê a margem"
        html_bronze = self.cliente_web.get("/onboarding/diagnostico").get_data(as_text=True)
        self._executar(f"UPDATE empresa SET id_plano_atual = {int(self.prata_id)} WHERE id_empresa = {int(self.ids['E1'][0])}")
        html_prata = self.cliente_web.get("/onboarding/diagnostico").get_data(as_text=True)
        auditoria = self.cliente_web.get("/onboarding/diagnostico/auditoria").status_code
        self.check("prata_sees_margin", "R$ 16,50" not in html_bronze and "R$ 16,50" in html_prata
                   and auditoria == 200, f"auditoria={auditoria}")

        self.stage = "outra empresa"
        outra, _ = self._login("cliente-e2@example.invalid")
        self.check("other_company_sees_nothing", outra.get("/api/onboarding/diagnostico").status_code == 404)

    # ------------------------------------------------- 4. downgrade
    def _etapa_downgrade_dos_planos(self):
        from flask_migrate import downgrade, upgrade
        import entitlements

        self.stage = "downgrade com Raio-X gravado"
        with self.app.app_context():
            self.db.session.remove()
        antes = self._contagens()
        try:
            self._migrar(downgrade, REVISAO_ANTERIOR)
            recusado = False
        except RuntimeError:
            recusado = True
        self.check("downgrade_refused_with_raio_x",
                   recusado and self._revisao() == self.head and self._contagens() == antes,
                   f"recusado={recusado} revisão={self._revisao()}")

        self.stage = "downgrade sem Raio-X"
        self._executar("DELETE FROM diagnostico_entrada")
        self._migrar(downgrade, REVISAO_ANTERIOR)
        self.check("downgrade_removes_tables_and_policies",
                   self._revisao() == REVISAO_ANTERIOR and not self._tabelas() & set(TABELAS)
                   and self._politicas() == {}, f"revisão={self._revisao()}")

        self.stage = "portal com o processo no ar e o cache antigo"
        telas = [self.cliente_web.get(caminho).status_code
                 for caminho in ("/cliente/dashboard", "/onboarding/diagnostico", "/cliente/historico")]
        with self.app.app_context():
            esquecido = entitlements.esquema_planos_disponivel() is False
            self.db.session.remove()
        self.check("portal_after_downgrade_process_running", telas == [200, 503, 200] and esquecido, str(telas))

        self.stage = "novo upgrade"
        self._migrar(upgrade, "head")
        self._limpar_cache_esquema()
        contagens = self._contagens()
        self.check("upgrade_again", self._revisao() == self.head
                   and all(contagens[t] == n for t, n in CATALOGO.items())
                   and self._politicas() == {t: POLITICAS for t in TABELAS}
                   and self._diferencas_metadata() == []
                   and self.cliente_web.get("/onboarding/diagnostico").status_code == 200,
                   str({t: contagens.get(t) for t in TABELAS}))


def _worker_main(bin_dir, work_dir):
    cluster = _ClusterDosPlanos(Path(bin_dir).resolve(), Path(work_dir).resolve())
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
