"""Avaliação de Cenário em PostgreSQL real: RLS, políticas, privilégios, a função de expurgo e o downgrade.

Por padrão o ensaio é ignorado. Para habilitar, indique apenas o diretório dos
binários PostgreSQL em NEXO_TEST_POSTGRES_BIN e execute:
    python -B -m unittest tests.test_leads_postgresql -v

Mesmo protocolo de tests/test_contratos_postgresql.py, de onde vem o cluster temporário:
um processo separado, com ambiente filtrado e dotenv desabilitado, e nenhum banco ou
servidor existente. Os dados são fictícios. O cluster recebe os papéis da Data API do
Supabase (anon, authenticated, service_role) e postgres, e os privilégios padrão que o
Supabase dá a anon e authenticated nas tabelas e funções novas: a revisão f4c2a8e1d9b6
precisa revogá-los.

Complementa tests/test_leads_avaliacao.py (SQLite); aqui só o que depende do PostgreSQL:
  1. a revisão aplicada: modelo, RLS sem FORCE, políticas por papel, privilégios revogados,
     UUID e padrões gerados pelo banco, colunas com cifrão pelo ORM e as restrições;
  2. a função expurgar_brainstorming_pdv: SECURITY INVOKER, search_path fixo, EXECUTE fora
     do alcance de PUBLIC, anon e authenticated, e o mesmo resultado da regra em SQLAlchemy;
  3. o comando `flask leads expurgar-pdv` (simulação e aplicação) e o CASCADE;
  4. downgrade recusado com leads, aceito sem eles (levando função e políticas) e novo upgrade;
  5. Etapa 2 (7c4e2a9b5d31): as CHECKs novas no banco e o downgrade dela recusado com dados das
     regras lead-2026-11.3, aceito sem eles (colunas e CHECKs saem; as linhas antigas ficam).
"""

import contextlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tests.test_contratos_postgresql import ROOT, TOOLS, _PrivateCluster, _safe_environment, _tool


REVISAO_ANTERIOR = "b3e9d6c1f7a2"
# Head das tabelas de lead: a f4c2a8e1d9b6 as criou; 5e1b9c3a7d20 ampliou a staging,
# 6a8f2d4c0e13 agenda o expurgo no pg_cron, 7c4e2a9b5d31 (Etapa 2) acrescenta as colunas de cadastro,
# ajustes e limiares e 9b3e5d7f1a28 (Etapa 3), a data da última venda e o resumo da inteligência.
REVISAO_LEADS = "9b3e5d7f1a28"
REVISAO_ETAPA_2 = "7c4e2a9b5d31"
REVISAO_ANTES_DA_ETAPA_2 = "6a8f2d4c0e13"
COLUNAS_DA_ETAPA_2 = {
    "brainstorming_dados_pdv": {
        "faturamento_em_relatorio_com_custo", "custo_bonificacoes", "custo_devolucoes", "devolucoes_com_custo",
        "quantidade_mesmos_meses", "meses_com_custo", "meses_margem_negativa", "meses_completos",
        "relatorios_desde_primeira_venda",
        "ultimo_mes_negativo", "ncm_ausente", "categoria_ausente", "estoque_final", "categoria_produto"},
    "lead_avaliacao": {"itens_omissao_erp", "limiares_triagem_json"},
}
CHECKS_DA_ETAPA_2 = {"ck_brainstorming_dados_pdv_etapa2_somas", "ck_brainstorming_dados_pdv_etapa2_meses",
                     "ck_brainstorming_dados_pdv_categoria", "ck_lead_avaliacao_omissao"}
COLUNAS_DA_ETAPA_3 = {"brainstorming_dados_pdv": {"fim_ultimo_relatorio_com_venda"},
                      "lead_avaliacao": {"resumo_inteligencia_json"}}
CHECKS_DA_ETAPA_3 = {"ck_brainstorming_dados_pdv_ultima_venda"}
JOB_DO_EXPURGO = ("nexo_expurgo_leads_pdv", "15 6 * * *", "SELECT * FROM public.expurgar_brainstorming_pdv(30)")
# O pg_cron do Supabase, reduzido ao que a revisão 6a8f2d4c0e13 usa: o ensaio prova o agendamento sem
# instalar a extensão no cluster temporário.
CRON_DE_ENSAIO = (
    "CREATE SCHEMA cron",
    "CREATE TABLE cron.job (jobid bigserial PRIMARY KEY, jobname text UNIQUE, schedule text NOT NULL, "
    "command text NOT NULL)",
    "CREATE FUNCTION cron.schedule(job_name text, schedule text, command text) RETURNS bigint LANGUAGE sql AS $$ "
    "INSERT INTO cron.job (jobname, schedule, command) VALUES (job_name, schedule, command) "
    "ON CONFLICT (jobname) DO UPDATE SET schedule = EXCLUDED.schedule, command = EXCLUDED.command "
    "RETURNING jobid $$",
    "CREATE FUNCTION cron.unschedule(job_name text) RETURNS boolean LANGUAGE sql AS $$ "
    "WITH apagado AS (DELETE FROM cron.job WHERE jobname = job_name RETURNING 1) "
    "SELECT EXISTS (SELECT 1 FROM apagado) $$",
)
TABELAS = ("lead_avaliacao", "brainstorming_dados_pdv")
POLITICAS = {
    ("deny_api_access", "RESTRICTIVE", "anon,authenticated", "false", "false"),
    ("acesso_servidor", "PERMISSIVE", "postgres,service_role", "true", "true"),
}
FUNCAO = "expurgar_brainstorming_pdv(integer)"
CHECKS = (
    "private_engine",
    # 1. A revisão aplicada
    "schema_before_migration", "migration_reaches_head", "metadata_matches_models", "rls_enabled_without_force",
    "policies_for_api_and_server_roles", "api_roles_lose_default_privileges",
    "restrictive_policy_denies_even_with_grant", "uuid_and_defaults_from_database", "dollar_columns_via_orm",
    "constraints_enforced", "purge_job_scheduled", "service_writes_on_postgresql",
    # 2. Função de expurgo
    "purge_function_hardened", "purge_function_not_executable_by_api_roles", "purge_matches_sqlalchemy_rule",
    "purge_respects_concurrent_writer",
    # 3. Comando e CASCADE
    "purge_command_simulates_and_applies", "cascade_delete",
    # 4. Downgrade
    "downgrade_refused_with_leads", "downgrade_removes_tables_function_and_policies", "upgrade_again",
    "purge_job_removed_on_downgrade", "purge_job_back_on_upgrade",
    # 5. Etapa 2
    "etapa2_constraints_enforced", "etapa2_downgrade_refused_with_new_data", "etapa2_downgrade_and_upgrade_again",
    # 6. Etapa 3
    "etapa3_service_writes_dates_and_summary", "etapa3_constraints_enforced", "etapa3_downgrade_refused_with_new_data",
    "etapa3_downgrade_and_upgrade_again",
)


class PostgreSQLLeadsTests(unittest.TestCase):
    def test_avaliacao_de_cenario_em_postgresql_real(self):
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
        with tempfile.TemporaryDirectory(prefix="nexo-leads-pg-") as work:
            work_dir = Path(work).resolve()
            data_dir = work_dir / "private-cluster"
            try:
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "tests.test_leads_postgresql", "--isolated-worker",
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
        print("\nEnsaio PostgreSQL Avaliação de Cenário: " + json.dumps(report, ensure_ascii=True, sort_keys=True))


class _ClusterDosLeads(_PrivateCluster):
    """Auxiliar do processo isolado; não é um caso descoberto pelo unittest."""

    def _etapas(self):
        self._etapa_antes_da_migracao()
        self._etapa_migracao()
        self._etapa_servico()
        self._etapa_avaliacao_etapa_2()
        self._etapa_avaliacao_etapa_3()
        self._etapa_funcao_de_expurgo()
        self._etapa_comando_e_cascade()
        self._etapa_downgrade()

    def _sql(self, consulta, **parametros):
        from sqlalchemy import text
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                return conexao.execute(text(consulta), parametros).all()

    def _executar(self, *comandos, **parametros):
        from sqlalchemy import text
        with self.app.app_context():
            with self.db.engine.begin() as conexao:
                for comando in comandos:
                    conexao.execute(text(comando), parametros)

    def _politicas(self):
        politicas = {}
        for tabela, nome, tipo, papeis, usando, checando in self._sql(
                "SELECT tablename, policyname, permissive, "
                "array_to_string(ARRAY(SELECT unnest(roles) ORDER BY 1), ','), qual, with_check "
                "FROM pg_policies WHERE schemaname = current_schema() AND tablename = ANY(:tabelas)",
                tabelas=list(TABELAS)):
            politicas.setdefault(tabela, set()).add((nome, tipo, papeis, usando, checando))
        return politicas

    def _funcao_existe(self):
        return bool(self._sql("SELECT to_regprocedure(:f) IS NOT NULL", f=FUNCAO)[0][0])

    def _como_papel(self, papel, consulta):
        """Executa como o papel e devolve (resultado ou None, se negado)."""
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                transacao = conexao.begin()
                conexao.execute(text(f'SET LOCAL ROLE "{papel}"'))
                try:
                    return conexao.execute(text(consulta)).all()
                except DBAPIError:
                    return None
                finally:
                    transacao.rollback()

    def _lead(self, sessao, status, **campos):
        from models import LeadAvaliacao
        dados = {"nome_contato": "Contato fictício", "email": f"lead-{uuid.uuid4().hex[:8]}@example.invalid",
                 "nome_loja": "Loja fictícia", "status": status}
        lead = LeadAvaliacao(**{**dados, **campos})
        sessao.add(lead)
        sessao.flush()
        return lead

    def _linha(self, sessao, lead, idade):
        from models import BrainstormingDadosPdv
        sessao.add(BrainstormingDadosPdv(lead_id=lead.id, ano_exercicio=2026, descricao_produto="Produto fictício",
                                         chave_produto=uuid.uuid4().hex[:16],
                                         quantidade=Decimal("2"), valor_venda_bruto=Decimal("20.00"),
                                         criado_em=datetime.now(timezone.utc) - idade))

    def _estado(self):
        return (sorted((str(i), s) for i, s in self._sql("SELECT id, status FROM lead_avaliacao")),
                sorted((str(i), n) for i, n in self._sql(
                    "SELECT lead_id, count(*) FROM brainstorming_dados_pdv GROUP BY lead_id")))

    # ------------------------------------------------- 1. a revisão aplicada
    def _etapa_antes_da_migracao(self):
        from flask_migrate import upgrade
        self.stage = "migração até a revisão anterior"
        self._migrar(upgrade, REVISAO_ANTERIOR)
        self.check("schema_before_migration",
                   self._revisao() == REVISAO_ANTERIOR and not self._tabelas() & set(TABELAS)
                   and not self._funcao_existe())
        self.stage = "papéis da Data API do Supabase"
        self._executar(
            'CREATE ROLE "anon" NOLOGIN', 'CREATE ROLE "authenticated" NOLOGIN',
            'CREATE ROLE "service_role" NOLOGIN BYPASSRLS', 'CREATE ROLE "postgres" NOLOGIN',
            # O que o Supabase faz no schema public: tabela e função novas nascem acessíveis pela Data API.
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated",
            "GRANT USAGE ON SCHEMA public TO anon, authenticated",
        )
        self.stage = "pg_cron de ensaio"
        self._executar(*CRON_DE_ENSAIO)

    def _jobs(self):
        return [tuple(linha) for linha in self._sql("SELECT jobname, schedule, command FROM cron.job ORDER BY jobid")]

    # ------------------------------------------------- 1b. agendamento e gravação pelo serviço
    def _etapa_servico(self):
        import avaliacao_cenario
        from models import BrainstormingDadosPdv, LeadAvaliacao
        from sqlalchemy import func, select
        from sqlalchemy.exc import IntegrityError
        import triagem_cenario

        self.check("purge_job_scheduled", self._jobs() == [JOB_DO_EXPURGO], str(self._jobs()))

        self.stage = "gravação do lead pelo serviço"
        id_lead = self._registrar_pelo_servico()
        with self.app.app_context():
            linhas = self.db.session.scalar(select(func.count()).select_from(BrainstormingDadosPdv))
            gravado = self.db.session.get(LeadAvaliacao, id_lead)
            resumo = (gravado.status, gravado.arquivos, gravado.vazamento_total_reais, gravado.itens_venda_sem_custo,
                      gravado.itens_venda_abaixo_custo, gravado.itens_omissao_erp,
                      json.loads(gravado.limiares_triagem_json) == triagem_cenario.LIMIARES_PADRAO.para_dict())
            repetida = self.db.session.scalars(select(BrainstormingDadosPdv)).first()
            try:
                self.db.session.add(BrainstormingDadosPdv(
                    lead_id=id_lead, ano_exercicio=repetida.ano_exercicio, chave_produto=repetida.chave_produto,
                    descricao_produto="Repetido", quantidade=Decimal("1"), valor_venda_bruto=Decimal("1.00")))
                self.db.session.commit()
                unico = False
            except IntegrityError:
                self.db.session.rollback()
                unico = True
            avaliacao_cenario.retriar(id_lead)
            self.db.session.commit()
            refeito = self.db.session.get(LeadAvaliacao, id_lead).versao_triagem is not None
        self._executar("DELETE FROM lead_avaliacao")
        # Café 2025: 8 un a R$ 150,00 com tabela de R$ 20,00 (R$ 10,00); Leite: custo R$ 30,00 > R$ 25,00 (R$ 5,00).
        self.check("service_writes_on_postgresql",
                   linhas == 4 and resumo == ("concluido", 2, Decimal("15.00"), 1, 1, 0, True) and unico and refeito,
                   f"linhas={linhas} resumo={resumo} unico={unico} refeito={refeito}")

    def _registrar_pelo_servico(self):
        """Grava (com commit) um lead pelo serviço, com o motor como o worker: devolve o id."""
        import avaliacao_cenario
        from tests.raio_x_support import avaliar_como_o_worker, relatorio_do_mes
        meses = (relatorio_do_mes(2024, 11, ["1;Cafe 500g;10;20,00;200,00;150,00", "2;Acucar 1kg;5;6,00;30,00;"]),
                 relatorio_do_mes(2025, 1, ["1;Café 500g;8;20,00;150,00;120,00", "3;Leite 1L;5;5,00;25,00;30,00"]))
        with tempfile.TemporaryDirectory(prefix="nexo-leads-pg-") as pasta:
            caminhos = []
            for numero, conteudo in enumerate(meses, 1):
                caminho = Path(pasta) / f"arquivo_{numero:02d}.csv"
                caminho.write_bytes(conteudo)
                caminhos.append(str(caminho))
            with self.app.app_context():
                resultado = avaliar_como_o_worker(caminhos)
        with self.app.app_context():
            lead = avaliacao_cenario.registrar(
                resultado, contato={"nome_contato": "Contato fictício", "email": "lead@example.invalid",
                                    "whatsapp": None, "nome_loja": "Loja fictícia"}, meses_prata=24)
            self.db.session.commit()
            id_lead = lead.id
            self.db.session.remove()
        return id_lead

    # ------------------------------------------------- 5. Etapa 2 (7c4e2a9b5d31)
    def _colunas_e_checks_da_etapa_2(self):
        colunas = {tabela: {nome for (nome,) in self._sql(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = :t", t=tabela)} & COLUNAS_DA_ETAPA_2[tabela] for tabela in COLUNAS_DA_ETAPA_2}
        checks = {nome for (nome,) in self._sql(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' AND conname = ANY(:nomes)",
            nomes=sorted(CHECKS_DA_ETAPA_2))}
        return colunas, checks

    def _etapa_avaliacao_etapa_2(self):
        from flask_migrate import downgrade, upgrade
        from sqlalchemy.exc import IntegrityError

        self.stage = "CHECKs da Etapa 2"
        # Cada comando viola uma CHECK da 7c4e2a9b5d31, e só ela (a recusa tem de vir com o nome dela).
        somas, meses = "ck_brainstorming_dados_pdv_etapa2_somas", "ck_brainstorming_dados_pdv_etapa2_meses"
        violacoes = (
            ("UPDATE lead_avaliacao SET itens_omissao_erp = -1", "ck_lead_avaliacao_omissao"),
            ("UPDATE brainstorming_dados_pdv SET meses_com_custo = meses, meses_margem_negativa = meses + 1", meses),
            ("UPDATE brainstorming_dados_pdv SET meses_completos = meses + 1", meses),
            ("UPDATE brainstorming_dados_pdv SET meses_com_custo = meses + 1", meses),
            ("UPDATE brainstorming_dados_pdv SET custo_bonificacoes = -1", somas),
            ("UPDATE brainstorming_dados_pdv SET quantidade_mesmos_meses = -0.001", somas),
            ("UPDATE brainstorming_dados_pdv SET categoria_produto = '   '", "ck_brainstorming_dados_pdv_categoria"),
        )
        id_lead = self._registrar_pelo_servico()
        recusas = []
        for comando, restricao in violacoes:
            try:
                self._executar(comando)
                recusas.append(None)
            except IntegrityError as erro:
                recusas.append(getattr(getattr(erro.orig, "diag", None), "constraint_name", None))
        recusas_certas = recusas == [restricao for _comando, restricao in violacoes]
        # Uma categoria válida e um estoque negativo passam (estoque negativo é dado do PDV).
        self._executar("UPDATE brainstorming_dados_pdv SET categoria_produto = 'BEBIDAS', estoque_final = -2.5")
        aceitos = self._sql("SELECT count(*) FROM brainstorming_dados_pdv WHERE categoria_produto = 'BEBIDAS' "
                            "AND estoque_final = -2.5")[0][0]
        self.check("etapa2_constraints_enforced", recusas_certas and aceitos == 4, f"recusas={recusas} aceitos={aceitos}")

        self.stage = "downgrade da Etapa 2 com dados das regras novas"
        # Sem dado da Etapa 3, a 9b3e5d7f1a28 desce na mesma transação: a recusa é a da 7c4e2a9b5d31 (linhas
        # das regras lead-2026-11.3 e o lead com itens_omissao_erp).
        self._executar("UPDATE brainstorming_dados_pdv SET versao_regras = 'lead-2026-11.3', "
                       "fim_ultimo_relatorio_com_venda = NULL")
        self._executar("UPDATE lead_avaliacao SET resumo_inteligencia_json = NULL")
        antes = self._contagens()
        try:
            self._migrar(downgrade, REVISAO_ANTES_DA_ETAPA_2)
            recusado = False
        except RuntimeError:
            recusado = True
        colunas, checks = self._colunas_e_checks_da_etapa_2()
        self.check("etapa2_downgrade_refused_with_new_data",
                   recusado and self._revisao() == self.head and self._contagens() == antes
                   and colunas == COLUNAS_DA_ETAPA_2 and checks == CHECKS_DA_ETAPA_2,
                   f"recusado={recusado} revisão={self._revisao()}")

        self.stage = "downgrade da Etapa 2 sem os dados novos e novo upgrade"
        # Sem lead da Etapa 2: um lead antigo (colunas novas nulas) com uma linha sem versão fica.
        self._executar("DELETE FROM lead_avaliacao WHERE id = :i", i=str(id_lead))
        with self._sessao() as sessao:
            antigo = self._lead(sessao, "concluido", vazamento_total_reais=Decimal("7.00"))
            self._linha(sessao, antigo, timedelta(days=2))
            sessao.commit()
            id_antigo = str(antigo.id)
        self._migrar(downgrade, REVISAO_ANTES_DA_ETAPA_2)
        colunas_depois, checks_depois = self._colunas_e_checks_da_etapa_2()
        ficou = self._estado()
        self._migrar(upgrade, "head")
        colunas_de_volta, checks_de_volta = self._colunas_e_checks_da_etapa_2()
        padroes = self._sql("SELECT meses_com_custo, ncm_ausente, custo_bonificacoes, categoria_produto, estoque_final "
                            "FROM brainstorming_dados_pdv WHERE lead_id = :i", i=id_antigo)
        self.check("etapa2_downgrade_and_upgrade_again",
                   colunas_depois == {t: set() for t in COLUNAS_DA_ETAPA_2} and checks_depois == set()
                   and ficou == ([(id_antigo, "concluido")], [(id_antigo, 1)])
                   and self._revisao() == self.head and colunas_de_volta == COLUNAS_DA_ETAPA_2
                   and checks_de_volta == CHECKS_DA_ETAPA_2 and self._diferencas_metadata() == []
                   and [tuple(l) for l in padroes] == [(0, False, Decimal("0.00"), None, None)]
                   and self._jobs() == [JOB_DO_EXPURGO],
                   f"colunas={colunas_depois} checks={checks_depois} ficou={ficou} padroes={padroes}")
        self._executar("DELETE FROM lead_avaliacao")

    # ------------------------------------------------- 6. Etapa 3 (9b3e5d7f1a28)
    def _colunas_e_checks_da_etapa_3(self):
        colunas = {tabela: {nome for (nome,) in self._sql(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = :t", t=tabela)} & COLUNAS_DA_ETAPA_3[tabela] for tabela in COLUNAS_DA_ETAPA_3}
        checks = {nome for (nome,) in self._sql(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' AND conname = ANY(:nomes)",
            nomes=sorted(CHECKS_DA_ETAPA_3))}
        return colunas, checks

    def _etapa_avaliacao_etapa_3(self):
        from datetime import date
        from flask_migrate import downgrade, upgrade
        from sqlalchemy.exc import IntegrityError

        self.stage = "gravação da Etapa 3 pelo serviço"
        id_lead = self._registrar_pelo_servico()
        linhas = [tuple(l) for l in self._sql(
            "SELECT ano_exercicio, versao_regras, fim_ultimo_relatorio_com_venda FROM brainstorming_dados_pdv "
            "WHERE lead_id = :i ORDER BY ano_exercicio, fim_ultimo_relatorio_com_venda", i=str(id_lead))]
        bruto = self._sql("SELECT resumo_inteligencia_json FROM lead_avaliacao WHERE id = :i", i=str(id_lead))[0][0]
        try:
            resumo = json.loads(bruto)
        except (TypeError, ValueError):
            resumo = None
        # Novembro de 2024 e janeiro de 2025: a data é o fim do último relatório do ano com venda; os
        # relatórios não trazem estoque, então o resumo diz isso (nulo, e não zero).
        esperado = ([(2024, "lead-2026-11.4", date(2024, 11, 30))] * 2
                    + [(2025, "lead-2026-11.4", date(2025, 1, 31))] * 2)
        self.check("etapa3_service_writes_dates_and_summary",
                   linhas == esperado and isinstance(resumo, dict)
                   and resumo.get("versao") == "inteligencia-lead-2026-11.1" and resumo.get("com_estoque") is False
                   and resumo.get("capital_em_estoque") is None and "itens_abaixo_do_preco_de_referencia" in resumo,
                   f"linhas={linhas} resumo={str(resumo)[:200]}")

        self.stage = "CHECK da Etapa 3"
        recusas = []
        for valor in (date(1999, 12, 31), date(2101, 1, 1)):
            try:
                self._executar("UPDATE brainstorming_dados_pdv SET fim_ultimo_relatorio_com_venda = :d", d=valor)
                recusas.append(None)
            except IntegrityError as erro:
                recusas.append(getattr(getattr(erro.orig, "diag", None), "constraint_name", None))
        self._executar("UPDATE brainstorming_dados_pdv SET fim_ultimo_relatorio_com_venda = :d", d=date(2000, 1, 1))
        aceitos = self._sql("SELECT count(*) FROM brainstorming_dados_pdv "
                            "WHERE fim_ultimo_relatorio_com_venda = DATE '2000-01-01'")[0][0]
        self.check("etapa3_constraints_enforced",
                   recusas == ["ck_brainstorming_dados_pdv_ultima_venda"] * 2 and aceitos == 4,
                   f"recusas={recusas} aceitos={aceitos}")

        self.stage = "downgrade da Etapa 3 com dados das regras novas"
        antes = self._contagens()
        try:
            self._migrar(downgrade, REVISAO_ETAPA_2)
            recusado = False
        except RuntimeError:
            recusado = True
        colunas, checks = self._colunas_e_checks_da_etapa_3()
        self.check("etapa3_downgrade_refused_with_new_data",
                   recusado and self._revisao() == self.head and self._contagens() == antes
                   and colunas == COLUNAS_DA_ETAPA_3 and checks == CHECKS_DA_ETAPA_3,
                   f"recusado={recusado} revisão={self._revisao()}")

        self.stage = "downgrade da Etapa 3 sem os dados novos e novo upgrade"
        self._executar("UPDATE brainstorming_dados_pdv SET versao_regras = 'lead-2026-11.3', "
                       "fim_ultimo_relatorio_com_venda = NULL")
        self._executar("UPDATE lead_avaliacao SET resumo_inteligencia_json = NULL")
        estado = self._estado()
        self._migrar(downgrade, REVISAO_ETAPA_2)
        revisao_depois = self._revisao()
        colunas_depois, checks_depois = self._colunas_e_checks_da_etapa_3()
        ficou = self._estado()
        self._migrar(upgrade, "head")
        colunas_de_volta, checks_de_volta = self._colunas_e_checks_da_etapa_3()
        self.check("etapa3_downgrade_and_upgrade_again",
                   revisao_depois == REVISAO_ETAPA_2 and colunas_depois == {t: set() for t in COLUNAS_DA_ETAPA_3}
                   and checks_depois == set() and ficou == estado and self._revisao() == self.head
                   and colunas_de_volta == COLUNAS_DA_ETAPA_3 and checks_de_volta == CHECKS_DA_ETAPA_3
                   and self._estado() == estado and self._diferencas_metadata() == []
                   and self._jobs() == [JOB_DO_EXPURGO],
                   f"revisão={revisao_depois} colunas={colunas_depois} checks={checks_depois} ficou={ficou}")
        self._executar("DELETE FROM lead_avaliacao")

    def _etapa_migracao(self):
        from alembic.script import ScriptDirectory
        from flask_migrate import upgrade
        from sqlalchemy.exc import IntegrityError
        from models import LeadAvaliacao
        from tests.test_contratos_postgresql import MIGRATIONS

        self.stage = "migração até head"
        self._migrar(upgrade, "head")
        self.head = ScriptDirectory(MIGRATIONS).get_current_head()
        self.check("migration_reaches_head", self._revisao() == self.head == REVISAO_LEADS
                   and set(TABELAS) <= self._tabelas() and self._funcao_existe(), f"revisão={self._revisao()}")
        diferencas = self._diferencas_metadata()
        self.check("metadata_matches_models", diferencas == [], repr(diferencas)[:400])
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
            "OR has_table_privilege(papel, tabela, 'UPDATE') OR has_table_privilege(papel, tabela, 'DELETE') "
            "OR has_table_privilege(papel, tabela, 'TRUNCATE')", tabelas=list(TABELAS))]
        self.check("api_roles_lose_default_privileges", privilegios == [], str(privilegios))

        self.stage = "padrões do banco e colunas com cifrão"
        self._executar("INSERT INTO lead_avaliacao (nome_contato, email, nome_loja) "
                       "VALUES ('Fora da aplicação', 'fora@example.invalid', 'Loja')")
        self._executar("INSERT INTO brainstorming_dados_pdv (lead_id, ano_exercicio, chave_produto, descricao_produto, "
                       "quantidade, valor_venda_bruto) SELECT id, 2026, 'c0ffee0000000001', 'Produto', 1, 10 "
                       "FROM lead_avaliacao")
        [lead_bruto] = self._sql("SELECT id, status, criado_em IS NOT NULL, atualizado_em IS NOT NULL, "
                                 "vazamento_total_r$ FROM lead_avaliacao")
        [linha_bruta] = self._sql("SELECT id, flag_venda_sem_custo, flag_venda_abaixo_custo, flag_omissao_erp, "
                                  '"vazamento_estimado_r$", criado_em IS NOT NULL FROM brainstorming_dados_pdv')
        self.check("uuid_and_defaults_from_database",
                   uuid.UUID(str(lead_bruto[0])).version == 4 and lead_bruto[1:] == ("pendente", True, True, None)
                   and uuid.UUID(str(linha_bruta[0])).version == 4
                   and linha_bruta[1:] == (False, False, False, Decimal("0.00"), True),
                   f"{lead_bruto[1:]} {linha_bruta[1:]}")
        self._executar("DELETE FROM lead_avaliacao")

        with self._sessao() as sessao:
            lead = self._lead(sessao, "concluido", score_saude_cadastral=Decimal("72.50"),
                              vazamento_total_reais=Decimal("1234.56"), plano_recomendado_codigo="PRATA")
            sessao.commit()
            id_lead = lead.id
            sessao.expire_all()
            lido = sessao.get(LeadAvaliacao, id_lead)
            pelo_orm = (lido.vazamento_total_reais, lido.score_saude_cadastral, lido.plano_recomendado_codigo)
        pelo_sql = self._sql('SELECT "vazamento_total_r$", vazamento_total_r$ FROM lead_avaliacao WHERE id = :i',
                             i=id_lead)
        self.check("dollar_columns_via_orm", pelo_orm == (Decimal("1234.56"), Decimal("72.50"), "PRATA")
                   and pelo_sql == [(Decimal("1234.56"), Decimal("1234.56"))], f"{pelo_orm} {pelo_sql}")
        self._executar("DELETE FROM lead_avaliacao")

        self.stage = "restrições"
        recusas = []
        for campos in ({"status": "aprovado"}, {"score_saude_cadastral": Decimal("100.5")},
                       {"vazamento_total_reais": Decimal("-1")}, {"email": "sem-arroba"},
                       {"plano_recomendado_codigo": "DIAMANTE"}, {"nome_contato": " "},
                       {"vazamento_total_reais": Decimal("10000000000.00")}):
            with self._sessao() as sessao:
                try:
                    self._lead(sessao, campos.pop("status", "pendente"), **campos)
                    sessao.commit()
                    recusas.append(False)
                except IntegrityError:
                    recusas.append(True)
                except Exception as erro:  # NUMERIC(12,2) estourado é DataError
                    recusas.append(type(erro).__name__ == "DataError")
        self.check("constraints_enforced", all(recusas) and self._sql("SELECT count(*) FROM lead_avaliacao")[0][0] == 0,
                   str(recusas))

        self.stage = "política restritiva com GRANT e política permissiva posteriores"
        # O pior caso que a política restritiva existe para cobrir: uma linha gravada, um GRANT e uma
        # política permissiva criados depois (pelo painel, por exemplo). Sem a restritiva, anon veria
        # a linha e gravaria; com ela, nada.
        self._executar("INSERT INTO lead_avaliacao (nome_contato, email, nome_loja) "
                       "VALUES ('Existente', 'existente@example.invalid', 'Loja')",
                       "GRANT SELECT, INSERT ON lead_avaliacao TO anon",
                       "CREATE POLICY intrusa ON lead_avaliacao AS PERMISSIVE FOR ALL TO anon "
                       "USING (true) WITH CHECK (true)")
        visiveis = self._como_papel("anon", "SELECT count(*) FROM lead_avaliacao")
        inserido = self._como_papel("anon", "INSERT INTO lead_avaliacao (nome_contato, email, nome_loja) "
                                            "VALUES ('Invasor', 'x@example.invalid', 'X') RETURNING id")
        self._executar("DROP POLICY intrusa ON lead_avaliacao", "REVOKE ALL ON lead_avaliacao FROM anon",
                       "DELETE FROM lead_avaliacao")
        self.check("restrictive_policy_denies_even_with_grant", visiveis == [(0,)] and inserido is None,
                   f"visiveis={visiveis} inserido={inserido}")

    # ------------------------------------------------- 2. função de expurgo
    def _cenario(self):
        dia = timedelta(days=1)
        with self._sessao() as sessao:
            antigo = self._lead(sessao, "concluido", vazamento_total_reais=Decimal("50.00"))
            for _ in range(3):
                self._linha(sessao, antigo, 31 * dia)
            convertido = self._lead(sessao, "convertido")
            self._linha(sessao, convertido, 90 * dia)
            misto = self._lead(sessao, "processamento_manual")
            self._linha(sessao, misto, 45 * dia)
            self._linha(sessao, misto, 5 * dia)
            recente = self._lead(sessao, "pendente")
            self._linha(sessao, recente, 30 * dia - timedelta(hours=1))
            expirado = self._lead(sessao, "expirado")
            self._linha(sessao, expirado, 40 * dia)
            self._lead(sessao, "pendente")
            sessao.commit()
            return str(antigo.id)

    def _etapa_funcao_de_expurgo(self):
        import leads_avaliacao
        self.stage = "função de expurgo endurecida"
        [(seguranca, configuracao, volatilidade, linguagem)] = self._sql(
            "SELECT prosecdef, array_to_string(proconfig, ';'), provolatile, l.lanname FROM pg_proc p "
            "JOIN pg_language l ON l.oid = p.prolang WHERE p.oid = to_regprocedure(:f)", f=FUNCAO)
        self.check("purge_function_hardened",
                   seguranca is False and configuracao == "search_path=pg_catalog, public, pg_temp"
                   and volatilidade == "v" and linguagem == "plpgsql", f"{seguranca} {configuracao}")
        execucao = [tuple(l) for l in self._sql(
            "SELECT papel, has_function_privilege(papel, :f, 'EXECUTE') FROM unnest(ARRAY['anon', 'authenticated']) papel",
            f=FUNCAO)]
        publico = self._sql("SELECT has_function_privilege('public', :f, 'EXECUTE')", f=FUNCAO)[0][0]
        negado = self._como_papel("anon", "SELECT * FROM expurgar_brainstorming_pdv(30)")
        self.check("purge_function_not_executable_by_api_roles",
                   execucao == [("anon", False), ("authenticated", False)] and publico is False and negado is None,
                   f"{execucao} publico={publico}")

        self.stage = "função igual à regra em SQLAlchemy"
        self.antigo = self._cenario()
        inicial = self._estado()
        with self._sessao() as sessao:
            pelo_sqlalchemy = leads_avaliacao.expurgar_dados_pdv(sessao, agora=datetime.now(timezone.utc))
            sessao.flush()
            from sqlalchemy import text
            estado_sqlalchemy = (sorted((str(i), s) for i, s in sessao.execute(text(
                "SELECT id, status FROM lead_avaliacao"))),
                sorted((str(i), n) for i, n in sessao.execute(text(
                    "SELECT lead_id, count(*) FROM brainstorming_dados_pdv GROUP BY lead_id"))))
            sessao.rollback()
        with self._sessao() as sessao:
            pela_funcao = leads_avaliacao.expurgar_dados_pdv(sessao)
            from sqlalchemy import text
            estado_funcao = (sorted((str(i), s) for i, s in sessao.execute(text(
                "SELECT id, status FROM lead_avaliacao"))),
                sorted((str(i), n) for i, n in sessao.execute(text(
                    "SELECT lead_id, count(*) FROM brainstorming_dados_pdv GROUP BY lead_id"))))
            resumo = sessao.execute(text('SELECT "vazamento_total_r$" FROM lead_avaliacao WHERE id = :i'),
                                    {"i": self.antigo}).scalar()
            sessao.rollback()
        esperado = leads_avaliacao.ResultadoExpurgo(leads_expirados=1, linhas_removidas=5)
        self.check("purge_matches_sqlalchemy_rule",
                   pelo_sqlalchemy == pela_funcao == esperado and estado_sqlalchemy == estado_funcao
                   and self._estado() == inicial and dict(estado_funcao[0])[self.antigo] == "expirado"
                   and self.antigo not in dict(estado_funcao[1]) and resumo == Decimal("50.00"),
                   f"{pelo_sqlalchemy} {pela_funcao} resumo={resumo}")
        self._corrida_com_quem_grava()

    def _corrida_com_quem_grava(self):
        """Quem segura o lead grava enquanto o expurgo espera a trava; ao ganhar a vez, o expurgo
        respeita o que foi gravado. A: linha nova e status: apaga só a linha velha e o lead fica.
        B: apaga as linhas e grava outro status: nada a expurgar, e o status gravado fica."""
        # O cenário da etapa dá (1, 5); a versão de um comando só daria (2, 6) e 'expirado' em A, e
        # a que não exigia linha velha daria (2, 5) e 'expirado' em B.
        esperou_a, saida_a, viva_a = self._corrida(
            ("INSERT INTO brainstorming_dados_pdv (lead_id, ano_exercicio, chave_produto, descricao_produto, "
             "quantidade, valor_venda_bruto) VALUES (:i, 2026, 'e0e0e0e0e0e0e0e0', 'Envio novo', 1, 10)",
             "UPDATE lead_avaliacao SET status = 'concluido' WHERE id = :i"))
        esperou_b, saida_b, viva_b = self._corrida(
            ("DELETE FROM brainstorming_dados_pdv WHERE lead_id = :i",
             "UPDATE lead_avaliacao SET status = 'em_processamento' WHERE id = :i"))
        self.check("purge_respects_concurrent_writer",
                   esperou_a and esperou_b and not (viva_a or viva_b)
                   and saida_a.get("resultado") == (1, 6) and saida_a.get("estado") == [("concluido", 1, 1)]
                   and saida_b.get("resultado") == (1, 5) and saida_b.get("estado") == [("em_processamento", 0, 0)],
                   f"A: esperou={esperou_a} {saida_a} | B: esperou={esperou_b} {saida_b}")

    def _corrida(self, comandos_do_escritor):
        """Um lead 'concluido' com uma linha de 40 dias; o escritor o trava, roda os comandos e só
        confirma depois de o expurgo estar esperando a trava. Devolve (esperou, saída, viva)."""
        import threading
        import time
        from sqlalchemy import text
        self.stage = "expurgo concorrente com quem grava o lead"
        with self._sessao() as sessao:
            lead = self._lead(sessao, "concluido")
            self._linha(sessao, lead, timedelta(days=40))
            sessao.commit()
            id_lead = str(lead.id)
        with self.app.app_context():
            motor = self.db.engine
        saida = {}

        def expurgar():
            # Confere o efeito dentro da própria transação e desfaz: o cenário das etapas seguintes
            # (5 linhas velhas e 1 lead a expirar) continua igual.
            try:
                with motor.connect() as conexao:
                    transacao_expurgo = conexao.begin()
                    saida["resultado"] = tuple(conexao.execute(text(
                        "SELECT leads_expirados, linhas_removidas FROM expurgar_brainstorming_pdv(30)")).one())
                    saida["estado"] = [tuple(l) for l in conexao.execute(text(
                        "SELECT l.status, count(b.id), count(b.id) FILTER (WHERE b.descricao_produto = 'Envio novo') "
                        "FROM lead_avaliacao l LEFT JOIN brainstorming_dados_pdv b ON b.lead_id = l.id "
                        "WHERE l.id = :i GROUP BY l.status"), {"i": id_lead})]
                    transacao_expurgo.rollback()
            except Exception as erro:  # o relatório do ensaio só leva o tipo
                saida["erro"] = type(erro).__name__

        with motor.connect() as escritor:
            transacao = escritor.begin()
            escritor.execute(text("SELECT id FROM lead_avaliacao WHERE id = :i FOR UPDATE"), {"i": id_lead})
            for comando in comandos_do_escritor:
                escritor.execute(text(comando), {"i": id_lead})
            linha = threading.Thread(target=expurgar)
            linha.start()
            esperou = False
            for _ in range(200):  # até 10 s para o expurgo chegar à trava
                bloqueados = self._sql("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                       "AND query LIKE '%expurgar_brainstorming_pdv%'")[0][0]
                if bloqueados:
                    esperou = True
                    break
                time.sleep(0.05)
            transacao.commit()
        linha.join(timeout=30)
        self._executar("DELETE FROM lead_avaliacao WHERE id = :i", i=id_lead)
        return esperou, saida, linha.is_alive()

    # ------------------------------------------------- 3. comando e CASCADE
    def _etapa_comando_e_cascade(self):
        self.stage = "comando flask leads expurgar-pdv"
        inicial = self._estado()
        with self.app.app_context():
            self.db.session.remove()
        executor = self.app.test_cli_runner()
        simulado = executor.invoke(args=["leads", "expurgar-pdv"])
        depois_simulacao = self._estado()
        aplicado = executor.invoke(args=["leads", "expurgar-pdv", "--aplicar"])
        repetido = executor.invoke(args=["leads", "expurgar-pdv", "--aplicar"])
        status = dict(self._estado()[0])
        self.check("purge_command_simulates_and_applies",
                   simulado.exit_code == aplicado.exit_code == repetido.exit_code == 0
                   and "a remover: 5; leads a expirar: 1" in simulado.output and depois_simulacao == inicial
                   and "removidas: 5; leads expirados: 1" in aplicado.output
                   and "removidas: 0; leads expirados: 0" in repetido.output
                   and status[self.antigo] == "expirado" and "example.invalid" not in simulado.output + aplicado.output,
                   (simulado.output + aplicado.output)[-300:])

        self.stage = "CASCADE"
        antes = self._sql("SELECT count(*) FROM brainstorming_dados_pdv")[0][0]
        self._executar("DELETE FROM lead_avaliacao WHERE status = 'processamento_manual'")
        depois = self._sql("SELECT count(*) FROM brainstorming_dados_pdv")[0][0]
        self.check("cascade_delete", antes - depois == 1, f"antes={antes} depois={depois}")

    # ------------------------------------------------- 4. downgrade
    def _etapa_downgrade(self):
        from flask_migrate import downgrade, upgrade
        self.stage = "downgrade com leads gravados"
        antes = self._contagens()
        try:
            self._migrar(downgrade, REVISAO_ANTERIOR)
            recusado = False
        except RuntimeError:
            recusado = True
        self.check("downgrade_refused_with_leads",
                   recusado and self._revisao() == self.head and self._contagens() == antes and self._funcao_existe(),
                   f"recusado={recusado} revisão={self._revisao()}")

        self.stage = "downgrade sem leads"
        self._executar("DELETE FROM lead_avaliacao")
        self._migrar(downgrade, REVISAO_ANTERIOR)
        self.check("downgrade_removes_tables_function_and_policies",
                   self._revisao() == REVISAO_ANTERIOR and not self._tabelas() & set(TABELAS)
                   and not self._funcao_existe() and self._politicas() == {}
                   and {"plano_nivel", "diagnostico_entrada"} <= self._tabelas(), f"revisão={self._revisao()}")
        self.check("purge_job_removed_on_downgrade", self._jobs() == [], str(self._jobs()))

        self.stage = "novo upgrade"
        self._migrar(upgrade, "head")
        self.check("upgrade_again", self._revisao() == self.head and self._funcao_existe()
                   and self._politicas() == {t: POLITICAS for t in TABELAS} and self._diferencas_metadata() == [])
        self.check("purge_job_back_on_upgrade", self._jobs() == [JOB_DO_EXPURGO], str(self._jobs()))


def _worker_main(bin_dir, work_dir):
    cluster = _ClusterDosLeads(Path(bin_dir).resolve(), Path(work_dir).resolve())
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
