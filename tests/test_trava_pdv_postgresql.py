"""Trava do total do PDV em PostgreSQL real: migração, RLS, valores e corridas com lock.

Por padrão o ensaio é ignorado. Para habilitar, indique apenas o diretório dos
binários PostgreSQL em NEXO_TEST_POSTGRES_BIN e execute:
    python -B -m unittest tests.test_trava_pdv_postgresql -v

Mesmo protocolo de tests/test_contratos_postgresql.py, de onde vem o cluster temporário:
um processo separado, com ambiente filtrado e dotenv desabilitado, e nenhum banco ou
servidor existente. Os dados são fictícios. O preflight e o worker isolados do ETL
ficam simulados; o cálculo é o real, em processo.

Complementa tests/test_trava_pdv.py, que cobre as regras em SQLite; aqui só o que
depende do PostgreSQL real:
  1. código no ar antes da migração: consultar a tabela inexistente abortaria a
     transação, e a publicação precisa falhar fechada;
  2. a revisão e2f7a94c1d35 aplicada com o processo no ar bate com o modelo, liga o RLS
     e passa a valer sem reiniciar;
  3. os 3 cenários pedidos e os valores que estourariam uma coluna com precisão;
  4. CHECKs e o teto do valor digitado;
  5. corridas: enviar, corrigir, processar e publicar a mesma análise se serializam
     pelo lock de linha, e quem espera decide com o dado novo;
  6. exclusões de análise, de quem informou e de empresa, com conferência gravada;
  7. downgrade recusado com dados, aceito com a tabela vazia, e novo upgrade.
"""

from datetime import date, timedelta
from decimal import Decimal
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_contratos_postgresql import (
    MIGRATIONS, PASSWORD, ROOT, T0, TOOLS, _PrivateCluster, _safe_environment, _tool,
)


REVISAO_ANTERIOR = "c7d41e9a2b60"
CHECKS = (
    "private_engine",
    # 1. Antes da migração
    "schema_before_migration", "no_field_before_migration", "flow_without_table",
    "publication_refused_without_table", "draft_without_table",
    # 2. Migração com o processo no ar
    "absence_cached_up_to_30s", "presence_after_recheck", "migration_reaches_head", "metadata_matches_models",
    "named_constraints", "column_types", "rls_enabled_without_policy", "old_analysis_publishes_with_warning",
    # 3. Conferência
    "identical_total_concluded", "fifty_cents_concluded", "fifteen_reais_diverges_and_blocks",
    "cards_show_the_values", "absurd_value_diverges_without_overflow", "huge_value_fits_and_renders",
    "rls_hides_rows_from_other_role",
    # 4. Restrições
    "constraints_refuse_invalid_rows", "ceiling_fits_the_column",
    # 5. Corridas
    "upload_then_publish_refused", "publish_then_upload_refused", "correction_then_publish_refused",
    "publish_then_correction_refused", "publish_then_publish_refused", "process_then_publish_sees_divergence",
    "process_then_publish_succeeds", "process_then_upload_leaves_pending", "process_then_client_upload_refused",
    # 6. Exclusões
    "delete_analysis_takes_conference", "delete_informant_keeps_conference",
    "permanent_company_delete_takes_conference",
    # 7. Downgrade
    "downgrade_refused_with_data", "downgrade_removes_empty_table", "no_query_fails_after_downgrade",
    "upgrade_again",
)
# 1.500,00 no total, sem coluna de situação.
VENDAS = "produto;quantidade;faturamento\nArroz;10;1.000,00\nFeijão;5;500,00\n".encode()
COMPRAS = "fornecedor;total_compra\nFornecedor sintético;800,00\n".encode()


class PostgreSQLTravaPdvTests(unittest.TestCase):
    def test_trava_do_pdv_em_postgresql_real(self):
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
        with tempfile.TemporaryDirectory(prefix="nexo-trava-pdv-pg-") as work:
            work_dir = Path(work).resolve()
            data_dir = work_dir / "private-cluster"
            try:
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "tests.test_trava_pdv_postgresql", "--isolated-worker",
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
        print("\nEnsaio PostgreSQL trava do PDV: " + json.dumps(report, ensure_ascii=True, sort_keys=True))


class _Porta:
    """Segura a primeira chamada, com o lock da análise na mão, até o ensaio liberar."""

    def __init__(self, real):
        self.real = real
        self.chegou, self.liberada = threading.Event(), threading.Event()

    def __call__(self, *args, **kwargs):
        if not self.chegou.is_set():
            self.chegou.set()
            if not self.liberada.wait(20):
                raise RuntimeError("Porta do ensaio não liberada")
        return self.real(*args, **kwargs)


class _ClusterDaTrava(_PrivateCluster):
    """Auxiliar do processo isolado; não é um caso descoberto pelo unittest."""

    contador = 0

    def _etapas(self):
        import etl_processor
        import etl_uploads
        # Dois pedidos na mesma análise só coincidem entre processos do gunicorn: o limite
        # de um upload por processo sai do caminho, e as threads fazem o papel dos processos.
        with patch.object(etl_uploads, "run_validation"), \
                patch("etl_processor.calcular_kpis_em_memoria", etl_processor._calcular_kpis_local), \
                patch.object(etl_uploads, "_UPLOAD_ADMISSION", threading.BoundedSemaphore(8)):
            self._etapa_antes_da_migracao()
            self._etapa_migracao_com_o_processo_no_ar()
            self._etapa_conferencia()
            self._etapa_restricoes()
            self._etapa_corridas()
            self._etapa_exclusoes()
            self._etapa_downgrade_da_trava()

    # ------------------------------------------------------------------ apoio
    def _analise(self, nome="E1"):
        """Uma análise nova, em mês próprio."""
        from models import Analise
        self.contador += 1
        ano, mes = 2024 + self.contador // 12, self.contador % 12 + 1
        inicio = date(ano, mes, 1)
        with self._sessao() as sessao:
            analise = Analise(
                id_empresa=self.ids[nome][0], id_plano_referencia=self.plano_id,
                id_usuario_admin_responsavel=self.admin_id, periodo_inicio=inicio,
                periodo_fim=inicio + timedelta(days=27), mes_referencia=mes, ano_referencia=ano,
                tipo_analise="MENSAL", status_analise="AGUARDANDO_RELATORIO",
            )
            sessao.add(analise)
            sessao.commit()
            return analise.id_analise

    def _dados_envio(self, web, pagina, tipo, conteudo, total, **extras):
        dados = {"tipo_relatorio": tipo, "arquivo": (io.BytesIO(conteudo), f"{tipo.lower()}.csv"),
                 "csrf_token": self._csrf(web, pagina), **extras}
        if total is not None:
            dados["valor_pdv_informado"] = total
        return dados

    def _dados_envio_admin(self, web, id_analise, tipo="VENDAS", conteudo=VENDAS, total="1.500,00"):
        return self._dados_envio(web, f"/admin/analise/{id_analise}/upload", tipo, conteudo, total)

    def _dados_envio_cliente(self, web, id_analise, tipo="VENDAS", conteudo=VENDAS, total="1.500,00"):
        return self._dados_envio(web, "/cliente/upload", tipo, conteudo, total, id_analise=str(id_analise))

    @staticmethod
    def _post_envio_admin(web, id_analise, dados):
        return web.post(f"/admin/analise/{id_analise}/upload", data=dados, content_type="multipart/form-data")

    @staticmethod
    def _post_envio_cliente(web, dados):
        return web.post("/cliente/upload", data=dados, content_type="multipart/form-data")

    def _enviar_admin(self, id_analise, tipo="VENDAS", conteudo=VENDAS, total="1.500,00"):
        return self._post_envio_admin(self.admin_a, id_analise,
                                      self._dados_envio_admin(self.admin_a, id_analise, tipo, conteudo, total))

    def _dados_processar(self, web, id_analise):
        return {"csrf_token": self._csrf(web, f"/admin/analise/{id_analise}/upload")}

    def _processar(self, id_analise):
        return self.admin_a.post(f"/admin/analise/{id_analise}/processar",
                                 data=self._dados_processar(self.admin_a, id_analise))

    def _dados_correcao(self, web, id_analise, total):
        return {"valor_pdv_informado": total, "csrf_token": self._csrf(web, f"/admin/analise/{id_analise}/upload")}

    @staticmethod
    def _post_correcao(web, id_analise, dados):
        return web.post(f"/admin/analise/{id_analise}/total-pdv", data=dados)

    @staticmethod
    def _dados_editor(web, id_analise, acao="publicar", titulo="Título fictício"):
        corpo = web.get(f"/admin/analise/{id_analise}/relatorio").get_data(as_text=True)
        return {
            "csrf_token": re.search(r'name="csrf_token"[^>]*value="([^"]+)"', corpo).group(1), "acao": acao,
            "versao_rascunho": re.search(r'name="versao_rascunho" value="(\d+)"', corpo).group(1),
            "titulo": titulo, "resumo_executivo": "Resumo fictício", "conclusao_estrategica": "Conclusão fictícia",
        }

    @staticmethod
    def _post_editor(web, id_analise, dados):
        return web.post(f"/admin/analise/{id_analise}/relatorio", data=dados)

    def _publicar(self, id_analise, acao="publicar"):
        return self._post_editor(self.admin_a, id_analise, self._dados_editor(self.admin_a, id_analise, acao))

    def _analise_enviada(self, total="1.500,00", conteudo=VENDAS):
        """Vendas e Compras recebidos, com a conferência PENDENTE."""
        id_analise = self._analise()
        for tipo, arquivo, valor in (("VENDAS", conteudo, total), ("COMPRAS", COMPRAS, None)):
            resposta = self._enviar_admin(id_analise, tipo, arquivo, valor)
            if resposta.status_code != 302:
                raise AssertionError(f"envio de {tipo} devolveu {resposta.status_code}")
        return id_analise

    def _analise_conferida(self, total="1.500,00", conteudo=VENDAS):
        id_analise = self._analise_enviada(total, conteudo)
        resposta = self._processar(id_analise)
        if resposta.status_code != 200:
            raise AssertionError(f"processamento devolveu {resposta.status_code}")
        return id_analise

    @staticmethod
    def _flashes(web):
        with web.session_transaction() as sessao:
            return [str(mensagem) for _categoria, mensagem in sessao.pop("_flashes", ())]

    def _estado(self, id_analise):
        from models import Analise, ValidacaoPdv
        with self._sessao() as sessao:
            analise = sessao.get(Analise, id_analise)
            linha = sessao.get(ValidacaoPdv, id_analise)
            relatorio = analise.relatorio
            return {
                "analise": analise.status_analise, "publicada": bool(relatorio and relatorio.publicado),
                "titulo": relatorio.titulo if relatorio else None,
                "conferencia": linha.status_validacao if linha else None,
                "total": linha.valor_pdv_informado if linha else None,
                "calculado": linha.faturamento_bruto_calculado if linha else None,
                "diferenca": linha.diferenca if linha else None,
            }

    def _esperar_lock(self, limite=6.0):
        """True quando outra conexão espera o lock de linha da análise."""
        from sqlalchemy import text
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                conexao = conexao.execution_options(isolation_level="AUTOCOMMIT")
                fim = time.monotonic() + limite
                while time.monotonic() < fim:
                    for consulta in conexao.execute(text(
                            "SELECT query FROM pg_stat_activity WHERE datname = current_database() "
                            "AND cardinality(pg_blocking_pids(pid)) > 0")).scalars():
                        consulta = " ".join((consulta or "").split()).upper()
                        if "FROM ANALISE" in consulta and consulta.endswith("FOR UPDATE"):
                            return True
                    time.sleep(0.01)  # espera curta pelo lock entre conexões
        return False

    @staticmethod
    def _em_paralelo(funcao):
        saida = {}

        def corpo():
            try:
                saida["resposta"] = funcao()
            except BaseException as erro:  # devolvido ao fluxo principal para as verificações
                saida["erro"] = erro

        fio = threading.Thread(target=corpo, daemon=True)
        fio.start()
        return fio, saida

    def _corrida(self, alvo, atributo, primeiro, segundo):
        """`primeiro` para na porta com o lock da análise, e `segundo` precisa esperar por ele.
        Devolve se a espera aconteceu e o status HTTP de cada um (ou o nome do erro)."""
        porta = _Porta(getattr(alvo, atributo))
        with patch.object(alvo, atributo, porta):
            fio_primeiro, saida_primeiro = self._em_paralelo(primeiro)
            try:
                chegou = porta.chegou.wait(15)
                fio_segundo, saida_segundo = self._em_paralelo(segundo)
                esperou = chegou and self._esperar_lock()
            finally:
                porta.liberada.set()
            fio_primeiro.join(30)
            fio_segundo.join(30)
        if fio_primeiro.is_alive() or fio_segundo.is_alive():
            raise AssertionError("Pedido da corrida não terminou")

        def desfecho(saida):
            return saida["resposta"].status_code if "resposta" in saida else repr(saida.get("erro"))

        return esperou, desfecho(saida_primeiro), desfecho(saida_segundo)

    # ------------------------------------------------- 1. antes da migração
    def _etapa_antes_da_migracao(self):
        from flask_migrate import upgrade
        from models import Plano, Segmento, Usuario

        self.stage = "migração até a revisão anterior à trava"
        self._migrar(upgrade, REVISAO_ANTERIOR)
        self._limpar_cache_esquema()
        self.check("schema_before_migration", self._revisao() == REVISAO_ANTERIOR
                   and "validacao_pdv" not in self._tabelas())

        self.stage = "dados fictícios"
        with self._sessao() as sessao:
            plano = Plano(nome_plano="BRONZE", valor_mensal=Decimal("100"), qtd_analises_mes=1,
                          tipo_analise_permitida="MENSAL", nivel_entrega_analise="BASICA",
                          nivel_dashboard="RESUMIDO", nivel_atendimento="BAIXO")
            segmento = Segmento(nome_segmento="Trava do PDV sintética")
            admin = Usuario(nome="Admin fictício", email="admin-trava@example.invalid", role="ADMIN",
                            primeiro_acesso=False)
            admin.set_senha(PASSWORD)
            sessao.add_all([plano, segmento, admin])
            sessao.commit()
            self.plano_id, self.segmento_id, self.admin_id = plano.id_plano, segmento.id_segmento, admin.id_usuario
        self._nova_empresa("E1", senha=True)
        self.admin_a = self._login("admin-trava@example.invalid")[0]
        self.admin_b = self._login("admin-trava@example.invalid")[0]
        self.cliente_web = self._login("cliente-e1@example.invalid")[0]

        self.stage = "código no ar antes da migração"
        self.anterior = self._analise()
        paginas = [web.get(caminho).get_data(as_text=True) for web, caminho in (
            (self.cliente_web, "/cliente/upload"), (self.admin_a, f"/admin/analise/{self.anterior}/upload"))]
        self.check("no_field_before_migration", all('name="valor_pdv_informado"' not in p for p in paginas))
        envios = [self._post_envio_cliente(self.cliente_web, self._dados_envio_cliente(
            self.cliente_web, self.anterior, tipo, conteudo, None)).status_code
            for tipo, conteudo in (("VENDAS", VENDAS), ("COMPRAS", COMPRAS))]
        processado = self._processar(self.anterior)
        estado = self._estado_sem_tabela(self.anterior)
        telas = [web.get(caminho).status_code for web, caminho in (
            (self.cliente_web, "/cliente/dashboard"), (self.admin_a, "/admin/dashboard"),
            (self.admin_a, f"/admin/analise/{self.anterior}/upload"),
            (self.admin_a, f"/admin/analise/{self.anterior}/relatorio"))]
        self.check("flow_without_table",
                   envios == [302, 302] and processado.status_code == 200
                   and "não instalada neste banco" in processado.get_data(as_text=True)
                   and estado["analise"] == "EM_ANALISE" and telas == [200] * 4,
                   f"envios={envios} processado={processado.status_code} estado={estado} telas={telas}")
        self._flashes(self.admin_a)
        recusa = self._publicar(self.anterior)
        estado = self._estado_sem_tabela(self.anterior)
        avisos = self._flashes(self.admin_a)
        self.check("publication_refused_without_table",
                   recusa.status_code == 302 and not estado["publicada"] and estado["analise"] == "EM_ANALISE"
                   and any("Aplique a migração e2f7a94c1d35" in m for m in avisos), f"{estado} {avisos}")
        rascunho = self._publicar(self.anterior, "rascunho")
        self.check("draft_without_table", rascunho.status_code == 302
                   and self._estado_sem_tabela(self.anterior)["titulo"] == "Título fictício")

    def _estado_sem_tabela(self, id_analise):
        from models import Analise
        with self._sessao() as sessao:
            analise = sessao.get(Analise, id_analise)
            relatorio = analise.relatorio
            return {"analise": analise.status_analise, "publicada": bool(relatorio and relatorio.publicado),
                    "titulo": relatorio.titulo if relatorio else None}

    # ------------------------------------- 2. migração com o processo no ar
    def _etapa_migracao_com_o_processo_no_ar(self):
        from alembic.script import ScriptDirectory
        from flask_migrate import upgrade
        from sqlalchemy import CheckConstraint, text
        import validacao_pdv
        from models import ValidacaoPdv

        self.stage = "migração e2f7a94c1d35 com o processo no ar"
        with self.app.app_context():
            self.db.session.remove()
        self._migrar(upgrade, "head")
        with self.app.app_context():
            ausente = validacao_pdv.esquema_validacao_pdv_disponivel()
            self.db.session.remove()
            # Passados os 30 s da ausência guardada, sem reiniciar o processo.
            validacao_pdv._esquema_por_engine[self.db.engine] = time.monotonic() - 31
            presente = validacao_pdv.esquema_validacao_pdv_disponivel()
            self.db.session.remove()
        self.check("absence_cached_up_to_30s", ausente is False)
        self.check("presence_after_recheck", presente is True)
        self.head = ScriptDirectory(MIGRATIONS).get_current_head()
        self.check("migration_reaches_head", self._revisao() == self.head == "e2f7a94c1d35"
                   and "validacao_pdv" in self._tabelas(), f"revisão={self._revisao()} head={self.head}")
        diferencas = self._diferencas_metadata()
        self.check("metadata_matches_models", diferencas == [], repr(diferencas)[:400])

        checks_no_modelo = {r.name for r in ValidacaoPdv.__table__.constraints if isinstance(r, CheckConstraint)}
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                restricoes = {linha[0]: tuple(linha[1:]) for linha in conexao.execute(text(
                    "SELECT conname, contype::text, confdeltype::text FROM pg_constraint "
                    "WHERE conrelid = 'validacao_pdv'::regclass"))}
                colunas = {linha[0]: tuple(linha[1:]) for linha in conexao.execute(text(
                    "SELECT column_name, data_type, numeric_precision, numeric_scale, character_maximum_length, "
                    "is_nullable FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'validacao_pdv'"))}
                rls = conexao.execute(text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'validacao_pdv'::regclass")).first()
                politicas = conexao.scalar(text(
                    "SELECT count(*) FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'validacao_pdv'"))
        esperadas = {
            "ck_validacao_pdv_valor": ("c", " "), "ck_validacao_pdv_status": ("c", " "),
            "ck_validacao_pdv_resultado": ("c", " "),
            "fk_validacao_pdv_analise": ("f", "c"), "fk_validacao_pdv_informante": ("f", "n"),
        }
        self.check("named_constraints",
                   {nome: valor for nome, valor in restricoes.items() if nome in esperadas} == esperadas
                   and checks_no_modelo == {nome for nome, (tipo, _) in esperadas.items() if tipo == "c"},
                   f"{restricoes} {checks_no_modelo}")
        sem_precisao = ("numeric", None, None, None, "YES")
        instante = "timestamp with time zone"
        self.check("column_types", colunas == {
            "id_analise": ("integer", 32, 0, None, "NO"),
            "valor_pdv_informado": ("numeric", 14, 2, None, "NO"),
            "faturamento_bruto_calculado": sem_precisao, "diferenca": sem_precisao,
            "valor_devolucoes": sem_precisao, "valor_cancelado": sem_precisao,
            "status_validacao": ("character varying", None, None, 20, "NO"),
            "id_usuario_informante": ("integer", 32, 0, None, "YES"),
            "informado_em": (instante, None, None, None, "NO"),
            "validado_em": (instante, None, None, None, "YES"),
        }, str(colunas))
        self.check("rls_enabled_without_policy", rls == (True, False) and politicas == 0, f"{rls} {politicas}")

        self.stage = "análise anterior à trava"
        self._flashes(self.admin_a)
        reprocessado = self._processar(self.anterior)
        publicada = self._publicar(self.anterior)
        estado = self._estado(self.anterior)
        self.check("old_analysis_publishes_with_warning",
                   "Sem conferência com o PDV" in reprocessado.get_data(as_text=True)
                   and publicada.status_code == 302 and estado["publicada"] and estado["conferencia"] is None,
                   str(estado))

    # ------------------------------------------------------- 3. conferência
    def _etapa_conferencia(self):
        from sqlalchemy import text
        from etl_processor import ResultadoETL
        import validacao_pdv

        self.stage = "os 3 cenários pedidos"
        identico = self._analise_conferida("1.500,00")
        estado = self._estado(identico)
        self.check("identical_total_concluded",
                   estado["conferencia"] == "CONCLUIDO" and estado["diferenca"] == Decimal("0.00")
                   and estado["calculado"] == Decimal("1500.00") and estado["analise"] == "EM_ANALISE"
                   and not estado["publicada"] and self._publicar(identico).status_code == 302
                   and self._estado(identico)["publicada"] and self._estado(identico)["analise"] == "CONCLUIDO", str(estado))
        tolerancia = self._analise_conferida("1.500,50")
        estado = self._estado(tolerancia)
        self.check("fifty_cents_concluded",
                   estado["conferencia"] == "CONCLUIDO" and estado["diferenca"] == Decimal("0.50")
                   and self._publicar(tolerancia).status_code == 302 and self._estado(tolerancia)["publicada"], str(estado))
        divergente = self._analise_conferida("1.515,00")
        self._flashes(self.admin_a)
        recusa = self._publicar(divergente)
        estado = self._estado(divergente)
        avisos = self._flashes(self.admin_a)
        self.check("fifteen_reais_diverges_and_blocks",
                   recusa.status_code == 302 and estado["conferencia"] == "DIVERGENTE_PDV"
                   and estado["diferenca"] == Decimal("15.00") and not estado["publicada"]
                   and estado["analise"] == "EM_ANALISE" and any("Publicação bloqueada" in m for m in avisos),
                   f"{estado} {avisos}")
        consultor = self.admin_a.get("/admin/dashboard").get_data(as_text=True)
        cliente = self.cliente_web.get("/cliente/dashboard").get_data(as_text=True)
        self.check("cards_show_the_values",
                   all(t in consultor for t in ("Divergência no total do PDV", "1.515,00", "1.500,00", "R$ 15,00"))
                   and all(t in cliente for t in ("Diferença no total de vendas", "1.515,00", "preliminares")))

        self.stage = "valores que estourariam uma coluna com precisão"
        absurdo = self._analise_conferida(
            "1.500,00", b"produto;quantidade;faturamento\nArroz;1;10.000.000.000.000,00\n")
        estado = self._estado(absurdo)
        self.check("absurd_value_diverges_without_overflow",
                   estado["conferencia"] == "DIVERGENTE_PDV" and estado["diferenca"] == Decimal("9999999998500"), str(estado))
        gigante = self._analise_enviada("1.500,00")
        with self.app.app_context():
            validacao_pdv.conferir_e_registrar(gigante, ResultadoETL(
                True, "ok", {"faturamento_total": 1e300}, {"valor_devolucoes": 1e300, "valor_cancelado": 1e300}, []))
            self.db.session.commit()
            self.db.session.remove()
        estado = self._estado(gigante)
        pagina = self.admin_a.get(f"/admin/analise/{gigante}/upload")
        self.check("huge_value_fits_and_renders",
                   estado["conferencia"] == "DIVERGENTE_PDV" and estado["calculado"] > Decimal("1e300")
                   and pagina.status_code == 200, f"{estado['conferencia']} {pagina.status_code}")

        self.stage = "RLS com linhas gravadas"
        with self.app.app_context():
            with self.db.engine.begin() as conexao:
                conexao.execute(text("CREATE ROLE anon_api NOSUPERUSER NOLOGIN"))
                conexao.execute(text("GRANT USAGE ON SCHEMA public TO anon_api"))
                conexao.execute(text("GRANT SELECT ON validacao_pdv TO anon_api"))
            with self.db.engine.connect() as conexao:
                dono = conexao.scalar(text("SELECT count(*) FROM validacao_pdv"))
                conexao.execute(text("SET LOCAL ROLE anon_api"))
                outro = conexao.scalar(text("SELECT count(*) FROM validacao_pdv"))
                conexao.rollback()
        self.check("rls_hides_rows_from_other_role", dono > 0 and outro == 0, f"dono={dono} outro={outro}")

    # -------------------------------------------------------- 4. restrições
    def _etapa_restricoes(self):
        from sqlalchemy import text
        from sqlalchemy.exc import DataError, IntegrityError
        from etl_processor import em_centavos
        from models import ValidacaoPdv
        import validacao_pdv

        self.stage = "CHECKs e teto do valor digitado"
        id_analise = self._analise()
        um = Decimal("1")
        invalidas = (
            dict(valor_pdv_informado=Decimal("-1"), status_validacao="PENDENTE"),
            dict(valor_pdv_informado=um, status_validacao="OUTRO"),
            dict(valor_pdv_informado=um, status_validacao="CONCLUIDO"),
            dict(valor_pdv_informado=um, status_validacao="PENDENTE", diferenca=um),
            dict(valor_pdv_informado=um, status_validacao="PENDENTE", validado_em=T0),
            dict(valor_pdv_informado=um, status_validacao="DIVERGENTE_PDV", validado_em=T0,
                 faturamento_bruto_calculado=um, diferenca=Decimal("-1")),
        )
        recusadas = 0
        for campos in invalidas:
            with self._sessao() as sessao:
                sessao.add(ValidacaoPdv(id_analise=id_analise, informado_em=T0, **campos))
                try:
                    sessao.flush()
                except IntegrityError:
                    recusadas += 1
        self.check("constraints_refuse_invalid_rows", recusadas == len(invalidas), f"{recusadas}/{len(invalidas)}")
        with self._sessao() as sessao:
            sessao.add(ValidacaoPdv(id_analise=id_analise, informado_em=T0, status_validacao="PENDENTE",
                                    valor_pdv_informado=em_centavos(validacao_pdv.VALOR_MAXIMO)))
            sessao.flush()
            teto = sessao.scalar(text("SELECT valor_pdv_informado FROM validacao_pdv WHERE id_analise = :a"),
                                 {"a": id_analise})
        with self._sessao() as sessao:
            sessao.add(ValidacaoPdv(id_analise=id_analise, informado_em=T0, status_validacao="PENDENTE",
                                    valor_pdv_informado=Decimal("1000000000000.00")))
            try:
                sessao.flush()
                estourou = False
            except DataError:
                estourou = True
        self.check("ceiling_fits_the_column", teto == Decimal("999999999999.99") and estourou, f"{teto} {estourou}")

    # ---------------------------------------------------------- 5. corridas
    def _etapa_corridas(self):
        import blueprints.admin as rotas_admin
        import etl_processor
        import storage
        import validacao_pdv

        a, b = self.admin_a, self.admin_b
        cliente = self.cliente_web

        self.stage = "corrida: envio segura o lock, publicação espera"
        id_a = self._analise_conferida("1.500,00")
        dados_envio, dados_editor = self._dados_envio_admin(a, id_a, total="1.515,00"), self._dados_editor(b, id_a)
        self._flashes(b)
        esperou, envio, publicacao = self._corrida(
            storage, "salvar", lambda: self._post_envio_admin(a, id_a, dados_envio),
            lambda: self._post_editor(b, id_a, dados_editor))
        estado, avisos = self._estado(id_a), self._flashes(b)
        self.check("upload_then_publish_refused",
                   esperou and (envio, publicacao) == (302, 302) and not estado["publicada"]
                   and estado["conferencia"] == "PENDENTE" and estado["total"] == Decimal("1515.00")
                   and any("ainda não foi conferido" in m for m in avisos),
                   f"esperou={esperou} envio={envio} publicacao={publicacao} {estado} {avisos}")

        self.stage = "corrida: publicação segura o lock, envio espera"
        id_b = self._analise_conferida("1.500,00")
        # Com rascunho salvo, quem espera já leu o relatório como não publicado antes do lock.
        self._publicar(id_b, "rascunho")
        dados_editor, dados_envio = self._dados_editor(a, id_b), self._dados_envio_admin(b, id_b, total="1.515,00")
        self._flashes(b)
        esperou, publicacao, envio = self._corrida(
            rotas_admin, "_confirmar_relatorio", lambda: self._post_editor(a, id_b, dados_editor),
            lambda: self._post_envio_admin(b, id_b, dados_envio))
        estado, avisos = self._estado(id_b), self._flashes(b)
        self.check("publish_then_upload_refused",
                   esperou and (publicacao, envio) == (302, 302) and estado["publicada"]
                   and estado["conferencia"] == "CONCLUIDO" and estado["total"] == Decimal("1500.00")
                   and any("Despublique-a antes de substituir" in m for m in avisos),
                   f"esperou={esperou} publicacao={publicacao} envio={envio} {estado} {avisos}")

        self.stage = "corrida: correção segura o lock, publicação espera"
        id_c = self._analise_conferida("1.500,00")
        dados_correcao, dados_editor = self._dados_correcao(a, id_c, "1.515,00"), self._dados_editor(b, id_c)
        self._flashes(b)
        esperou, correcao, publicacao = self._corrida(
            validacao_pdv, "registrar_valor_informado", lambda: self._post_correcao(a, id_c, dados_correcao),
            lambda: self._post_editor(b, id_c, dados_editor))
        estado, avisos = self._estado(id_c), self._flashes(b)
        self.check("correction_then_publish_refused",
                   esperou and (correcao, publicacao) == (302, 302) and not estado["publicada"]
                   and estado["conferencia"] == "PENDENTE" and estado["total"] == Decimal("1515.00")
                   and any("ainda não foi conferido" in m for m in avisos),
                   f"esperou={esperou} correcao={correcao} publicacao={publicacao} {estado} {avisos}")

        self.stage = "corrida: publicação segura o lock, correção espera"
        id_d = self._analise_conferida("1.500,00")
        dados_editor, dados_correcao = self._dados_editor(a, id_d), self._dados_correcao(b, id_d, "1.515,00")
        self._flashes(b)
        esperou, publicacao, correcao = self._corrida(
            rotas_admin, "_confirmar_relatorio", lambda: self._post_editor(a, id_d, dados_editor),
            lambda: self._post_correcao(b, id_d, dados_correcao))
        estado, avisos = self._estado(id_d), self._flashes(b)
        self.check("publish_then_correction_refused",
                   esperou and (publicacao, correcao) == (302, 302) and estado["publicada"]
                   and estado["total"] == Decimal("1500.00") and estado["conferencia"] == "CONCLUIDO"
                   and any("antes de corrigir o total" in m for m in avisos),
                   f"esperou={esperou} publicacao={publicacao} correcao={correcao} {estado} {avisos}")

        self.stage = "corrida: duas publicações do mesmo formulário"
        id_e = self._analise_conferida("1.500,00")
        dados_a = self._dados_editor(a, id_e, titulo="Texto da primeira sessão")
        dados_b = self._dados_editor(b, id_e, titulo="Texto da segunda sessão")
        self._flashes(b)
        esperou, primeira, segunda = self._corrida(
            rotas_admin, "_confirmar_relatorio", lambda: self._post_editor(a, id_e, dados_a),
            lambda: self._post_editor(b, id_e, dados_b))
        estado, avisos = self._estado(id_e), self._flashes(b)
        self.check("publish_then_publish_refused",
                   esperou and (primeira, segunda) == (302, 302) and estado["publicada"]
                   and estado["titulo"] == "Texto da primeira sessão"
                   and any("alterado em outra sessão" in m for m in avisos),
                   f"esperou={esperou} primeira={primeira} segunda={segunda} {estado} {avisos}")

        self.stage = "corrida: processamento segura o lock, publicação vê a divergência"
        id_f = self._analise_enviada("1.515,00")
        dados_processar, dados_editor = self._dados_processar(a, id_f), self._dados_editor(b, id_f)
        self._flashes(b)
        esperou, processamento, publicacao = self._corrida(
            etl_processor, "calcular_kpis_em_memoria",
            lambda: a.post(f"/admin/analise/{id_f}/processar", data=dados_processar),
            lambda: self._post_editor(b, id_f, dados_editor))
        estado, avisos = self._estado(id_f), self._flashes(b)
        self.check("process_then_publish_sees_divergence",
                   esperou and (processamento, publicacao) == (200, 302) and not estado["publicada"]
                   and estado["conferencia"] == "DIVERGENTE_PDV" and any("diverge do total" in m for m in avisos),
                   f"esperou={esperou} processamento={processamento} publicacao={publicacao} {estado} {avisos}")

        self.stage = "corrida: processamento segura o lock, publicação passa com o total conferido"
        id_g = self._analise_enviada("1.500,00")
        dados_processar, dados_editor = self._dados_processar(a, id_g), self._dados_editor(b, id_g)
        esperou, processamento, publicacao = self._corrida(
            etl_processor, "calcular_kpis_em_memoria",
            lambda: a.post(f"/admin/analise/{id_g}/processar", data=dados_processar),
            lambda: self._post_editor(b, id_g, dados_editor))
        estado = self._estado(id_g)
        self.check("process_then_publish_succeeds",
                   esperou and (processamento, publicacao) == (200, 302) and estado["publicada"]
                   and estado["conferencia"] == "CONCLUIDO" and estado["analise"] == "CONCLUIDO",
                   f"esperou={esperou} processamento={processamento} publicacao={publicacao} {estado}")

        self.stage = "corrida: processamento segura o lock, envio do consultor espera"
        id_h = self._analise_enviada("1.500,00")
        dados_processar, dados_envio = self._dados_processar(a, id_h), self._dados_envio_admin(b, id_h, total="1.515,00")
        esperou, processamento, envio = self._corrida(
            etl_processor, "calcular_kpis_em_memoria",
            lambda: a.post(f"/admin/analise/{id_h}/processar", data=dados_processar),
            lambda: self._post_envio_admin(b, id_h, dados_envio))
        estado = self._estado(id_h)
        self.check("process_then_upload_leaves_pending",
                   esperou and (processamento, envio) == (200, 302) and estado["conferencia"] == "PENDENTE"
                   and estado["total"] == Decimal("1515.00") and estado["calculado"] is None,
                   f"esperou={esperou} processamento={processamento} envio={envio} {estado}")

        self.stage = "corrida: processamento segura o lock, envio do cliente espera"
        id_i = self._analise_enviada("1.500,00")
        dados_processar = self._dados_processar(a, id_i)
        dados_cliente = self._dados_envio_cliente(cliente, id_i, total="1.515,00")
        esperou, processamento, envio = self._corrida(
            etl_processor, "calcular_kpis_em_memoria",
            lambda: a.post(f"/admin/analise/{id_i}/processar", data=dados_processar),
            lambda: self._post_envio_cliente(cliente, dados_cliente))
        estado = self._estado(id_i)
        self.check("process_then_client_upload_refused",
                   esperou and (processamento, envio) == (200, 404) and estado["conferencia"] == "CONCLUIDO"
                   and estado["total"] == Decimal("1500.00"),
                   f"esperou={esperou} processamento={processamento} envio={envio} {estado}")

    # --------------------------------------------------------- 6. exclusões
    def _etapa_exclusoes(self):
        from models import Empresa, Usuario, ValidacaoPdv

        self.stage = "exclusão de análise com conferência"
        id_empresa = self.ids["E1"][0]
        id_analise = self._analise_conferida("1.515,00")
        resposta = self.admin_a.post(
            f"/admin/analises/gerenciar/{id_empresa}/excluir/{id_analise}",
            data={"confirmar": "sim", "csrf_token": self._csrf(self.admin_a, f"/admin/analises/gerenciar/{id_empresa}")})
        with self._sessao() as sessao:
            linha = sessao.get(ValidacaoPdv, id_analise)
        self.check("delete_analysis_takes_conference", resposta.status_code == 302 and linha is None)

        self.stage = "exclusão de quem informou"
        id_analise = self._analise()
        with self._sessao() as sessao:
            informante = Usuario(nome="Informante fictício", email="informante@example.invalid", role="ADMIN",
                                 primeiro_acesso=False)
            informante.senha_hash = "synthetic-no-login-hash"
            sessao.add(informante)
            sessao.flush()
            sessao.add(ValidacaoPdv(id_analise=id_analise, valor_pdv_informado=Decimal("1"), informado_em=T0,
                                    status_validacao="PENDENTE", id_usuario_informante=informante.id_usuario))
            sessao.commit()
            sessao.delete(informante)
            sessao.commit()
        with self._sessao() as sessao:
            linha = sessao.get(ValidacaoPdv, id_analise)
            self.check("delete_informant_keeps_conference",
                       linha is not None and linha.id_usuario_informante is None)

        self.stage = "exclusão permanente de empresa com conferência"
        id_e2 = self._nova_empresa("E2", senha=True)
        id_analise = self._analise("E2")
        cliente = self._login("cliente-e2@example.invalid")[0]
        envio = self._post_envio_cliente(cliente, self._dados_envio_cliente(cliente, id_analise))
        lixeira = self.admin_a.post(f"/admin/empresa/excluir/{id_e2}",
                                    data={"csrf_token": self._csrf(self.admin_a, "/admin/empresas")})
        exclusao = self.admin_a.post(f"/admin/empresa/excluir-permanente/{id_e2}",
                                     data={"csrf_token": self._csrf(self.admin_a, "/admin/lixeira"),
                                           "senha_confirmacao": PASSWORD})
        with self._sessao() as sessao:
            self.check("permanent_company_delete_takes_conference",
                       (envio.status_code, lixeira.status_code, exclusao.status_code) == (302, 302, 302)
                       and sessao.get(Empresa, id_e2) is None and sessao.get(ValidacaoPdv, id_analise) is None,
                       f"{envio.status_code} {lixeira.status_code} {exclusao.status_code}")
        del self.ids["E2"]

    # --------------------------------------------------------- 7. downgrade
    def _contar_conferencias(self):
        from sqlalchemy import text
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                return conexao.scalar(text("SELECT count(*) FROM validacao_pdv"))

    def _etapa_downgrade_da_trava(self):
        from flask_migrate import downgrade, upgrade
        from sqlalchemy import text

        self.stage = "downgrade com conferências gravadas"
        with self.app.app_context():
            self.db.session.remove()
        antes = self._contar_conferencias()
        try:
            self._migrar(downgrade, REVISAO_ANTERIOR)
            recusado = False
        except RuntimeError:
            recusado = True
        self.check("downgrade_refused_with_data",
                   recusado and antes > 0 and self._revisao() == self.head and self._contar_conferencias() == antes,
                   f"recusado={recusado} antes={antes} revisão={self._revisao()}")

        self.stage = "downgrade com a tabela vazia"
        with self.app.app_context():
            with self.db.engine.begin() as conexao:
                conexao.execute(text("DELETE FROM validacao_pdv"))
        self._migrar(downgrade, REVISAO_ANTERIOR)
        self.check("downgrade_removes_empty_table", self._revisao() == REVISAO_ANTERIOR
                   and "validacao_pdv" not in self._tabelas())

        self.stage = "telas depois do downgrade, com o processo reiniciado"
        self._limpar_cache_esquema()
        telas = [web.get(caminho).status_code for web, caminho in (
            (self.cliente_web, "/cliente/dashboard"), (self.cliente_web, "/cliente/upload"),
            (self.admin_a, "/admin/dashboard"), (self.admin_a, f"/admin/analise/{self.anterior}/relatorio"))]
        self.check("no_query_fails_after_downgrade", telas == [200] * 4, str(telas))

        self.stage = "novo upgrade até o head"
        with self.app.app_context():
            self.db.session.remove()
        self._migrar(upgrade, "head")
        self._limpar_cache_esquema()
        diferencas = self._diferencas_metadata()
        with self.app.app_context():
            with self.db.engine.connect() as conexao:
                rls = conexao.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid = 'validacao_pdv'::regclass"))
        self.check("upgrade_again", self._revisao() == self.head and diferencas == [] and rls is True
                   and self._contar_conferencias() == 0, f"revisão={self._revisao()} {repr(diferencas)[:300]} rls={rls}")


def _worker_main(bin_dir, work_dir):
    cluster = _ClusterDaTrava(Path(bin_dir).resolve(), Path(work_dir).resolve())
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
