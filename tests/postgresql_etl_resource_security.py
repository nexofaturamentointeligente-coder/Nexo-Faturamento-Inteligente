"""SEC-005: ETL e uploads em PostgreSQL privado, sem banco/arquivos reais.

Executar explicitamente com NEXO_TEST_POSTGRES_BIN. As corridas observam
pg_blocking_pids de conexões reais e usam o cálculo ETL verdadeiro depois da
barreira; os mocks apenas coordenam a janela de concorrência.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import hashlib
from io import BytesIO
import threading
import time
from unittest.mock import patch

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from . import postgresql_auth_tokens as pg_cases
from . import test_auth_tokens_security as token_cases
from . import test_session_revocation_security as session_cases
from .postgresql_support import PrivatePostgreSQL
from blueprints import admin
from extensions import db
from models import (
    Analise, ArquivoArmazenado, IndicadorAnalise, Notificacao, ProdutoCurvaABC,
    UploadRelatorio, Usuario,
)
import etl_processor
import etl_uploads
import storage


SALES = ("Produto;Quantidade;Valor total\n"
         "Arroz sintético;2;20,00\nFeijão sintético;1;10,00\n").encode("utf-8")
PURCHASES = "Fornecedor;Total compra\nFornecedor sintético;40,00\n".encode("utf-8")
REPLACEMENT = "Produto;Quantidade;Valor total\nProduto novo;4;80,00\n".encode("utf-8")


class _IndependentWorkerAdmissions:
    """Representa exclusivamente a admissão local de dois workers distintos.

    Cada thread da corrida recebe seu semáforo de capacidade 1, como receberia
    em um processo Gunicorn separado. Autenticação, CSRF, consultas, locks e
    cálculo/persistência continuam reais. O teste demonstra a serialização pelo
    PostgreSQL entre workers, que não depende do semáforo local de um processo.
    """

    def __init__(self):
        self.local = threading.local()

    def acquire(self, blocking=True):
        if not hasattr(self.local, "gate"):
            self.local.gate = threading.BoundedSemaphore(1)
        return self.local.gate.acquire(blocking=blocking)

    def release(self):
        self.local.gate.release()


class PostgreSQLETLResourceSecurityTests(token_cases._TokenCase):
    """Casos exclusivos SEC-005; não herda testes históricos de autenticação."""

    _login_client = session_cases.SessionRevocationCases._login_client

    @classmethod
    def setUpClass(cls):
        cls.postgresql = PrivatePostgreSQL()
        cls.addClassCleanup(cls.postgresql.close)

    def setUp(self):
        pg_cases.PostgreSQLAuthTokenSecurityTests.setUp(self)
        with self.app.app_context():
            company = db.session.get(Usuario, self.ids["active"]).id_empresa
            other = db.session.get(Usuario, self.ids["other"]).id_empresa
            self.company_id, self.other_company_id = company, other
            analyses = [Analise(
                id_empresa=identifier, id_plano_referencia=self.plan_id,
                id_usuario_admin_responsavel=self.ids["admin"],
                periodo_inicio=date(2026, 1, 1), periodo_fim=date(2026, 1, 31),
                mes_referencia=1, ano_referencia=2026, tipo_analise="MENSAL",
                status_analise="RELATORIO_RECEBIDO",
            ) for identifier in (company, other)]
            db.session.add_all(analyses)
            db.session.flush()
            self.analysis_id, self.other_analysis_id = [row.id_analise for row in analyses]
            for analysis in analyses:
                for kind, content in (("VENDAS", SALES), ("COMPRAS", PURCHASES)):
                    digest = hashlib.sha256(content).hexdigest()
                    name = f"sec005_{analysis.id_analise}_{kind}_{digest[:8]}.csv"
                    reference = storage.salvar(content, name)
                    db.session.add(UploadRelatorio(
                        id_analise=analysis.id_analise, id_usuario_admin=self.ids["admin"],
                        tipo_relatorio=kind, nome_arquivo_original=name,
                        caminho_arquivo=reference, extensao_arquivo="CSV",
                        tamanho_arquivo=len(content), hash_arquivo=digest,
                        status_processamento="PENDENTE",
                    ))
                self._old_indicator(analysis.id_analise)
            db.session.commit()
        self.admin_client = self._login_client("admin")
        self.admin_csrf = self._upload_csrf(self.admin_client)

    def _old_indicator(self, analysis_id):
        db.session.add(IndicadorAnalise(
            id_analise=analysis_id, faturamento_total=Decimal("777.00"),
            total_comprado=Decimal("888.00"), saldo_estimado_compras_vendas=Decimal("111.00"),
            produto_mais_vendido_nome="Indicador anterior sintético",
            produto_mais_vendido_quantidade=Decimal("5"),
            produto_maior_faturamento_nome="Outro produto anterior",
            produto_maior_faturamento_valor=Decimal("777"),
            versao_processamento=7,
        ))
        for position, name, value, cumulative, kind in (
            (1, "ABC anterior A", "600", "77.22", "A"),
            (2, "ABC anterior B", "177", "100", "C"),
        ):
            db.session.add(ProdutoCurvaABC(
                id_analise=analysis_id, posicao_ranking=position, produto_nome=name,
                faturamento=Decimal(value), quantidade=Decimal("1"),
                percentual_individual=Decimal("77.22" if position == 1 else "22.78"),
                percentual_acumulado=Decimal(cumulative), classe_abc=kind,
            ))

    def _upload_csrf(self, client, analysis=None):
        identifier = self.analysis_id if analysis is None else analysis
        return self._csrf(client, f"/admin/analise/{identifier}/upload")

    def _process(self, client=None, csrf=None, analysis=None):
        identifier = self.analysis_id if analysis is None else analysis
        return (client or self.admin_client).post(
            f"/admin/analise/{identifier}/processar",
            data={"csrf_token": self.admin_csrf if csrf is None else csrf},
        )

    def _upload(self, client, csrf, content=REPLACEMENT, analysis=None, *, customer=False):
        identifier = self.analysis_id if analysis is None else analysis
        endpoint = "/cliente/upload" if customer else f"/admin/analise/{identifier}/upload"
        return client.post(endpoint, data={
            "csrf_token": csrf, "id_analise": str(identifier), "tipo_relatorio": "VENDAS",
            "valor_pdv_informado": "150.000,00", "arquivo": (BytesIO(content), "substituto.csv"),
        }, content_type="multipart/form-data")

    def _snapshot(self, analysis=None):
        identifier = self.analysis_id if analysis is None else analysis
        with Session(self.engine) as consulta:
            indicator = consulta.execute(select(IndicadorAnalise.__table__).where(
                IndicadorAnalise.id_analise == identifier,
            )).mappings().one()
            curve = consulta.execute(select(ProdutoCurvaABC.__table__).where(
                ProdutoCurvaABC.id_analise == identifier,
            ).order_by(ProdutoCurvaABC.posicao_ranking)).mappings().all()
            return dict(indicator), [dict(row) for row in curve]

    def _state(self, analysis=None):
        identifier = self.analysis_id if analysis is None else analysis
        with Session(self.engine) as consulta:
            analysis_row = consulta.get(Analise, identifier)
            uploads = consulta.scalars(select(UploadRelatorio).where(
                UploadRelatorio.id_analise == identifier,
            ).order_by(UploadRelatorio.tipo_relatorio)).all()
            return analysis_row.status_analise, {
                row.tipo_relatorio: dict(id=row.id_upload, reference=row.caminho_arquivo,
                    digest=row.hash_arquivo, status=row.status_processamento,
                    message=row.mensagem_erro)
                for row in uploads
            }

    def _notification_count(self):
        with Session(self.engine) as consulta:
            return consulta.scalar(select(db.func.count()).select_from(Notificacao))

    def _wait_for_analysis_lock(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.engine.connect() as connection:
                waiting = connection.scalar(text("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database() AND pid <> pg_backend_pid()
                      AND wait_event_type = 'Lock'
                      AND cardinality(pg_blocking_pids(pid)) > 0
                      AND query ILIKE '%analise%'
                """))
            if waiting:
                return
            threading.Event().wait(0.01)
        self.fail("A operação concorrente não aguardou o lock real da análise no PostgreSQL privado.")

    def test_valid_processing_persists_kpis_curve_and_expected_status_without_auto_publication(self):
        before_other = self._snapshot(self.other_analysis_id)
        response = self._process()
        self.assertEqual(response.status_code, 200)
        indicator, curve = self._snapshot()
        self.assertEqual(indicator["faturamento_total"], Decimal("30"))
        self.assertEqual(indicator["total_comprado"], Decimal("40"))
        self.assertEqual(indicator["saldo_estimado_compras_vendas"], Decimal("10"))
        self.assertEqual(indicator["versao_processamento"], 8)
        self.assertEqual(len(curve), 2)
        self.assertEqual(sum(row["faturamento"] for row in curve), Decimal("30"))
        status, uploads = self._state()
        self.assertEqual(status, "EM_ANALISE")
        self.assertTrue(all(row["status"] == "PROCESSADO" for row in uploads.values()))
        self.assertEqual(self._snapshot(self.other_analysis_id), before_other)
        self.assertGreater(self._notification_count(), 0)

    def test_rejected_processing_preserves_previous_indicator_and_entire_curve(self):
        before, other_before, uploads_before = self._snapshot(), self._snapshot(self.other_analysis_id), self._state()[1]
        self.app.config["ETL_LIMITS"] = {"max_rows": 2}
        response = self._process()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._snapshot(self.other_analysis_id), other_before)
        status, uploads = self._state()
        self.assertEqual(status, "RELATORIO_RECEBIDO")
        for kind, row in uploads.items():
            self.assertEqual(row["status"], "ERRO")
            self.assertEqual(row["reference"], uploads_before[kind]["reference"])
            self.assertEqual(row["digest"], uploads_before[kind]["digest"])
        self.assertEqual(self._notification_count(), 0)

    def test_valid_reprocessing_after_rejection_updates_once_and_sends_success_only_then(self):
        self.app.config["ETL_LIMITS"] = {"max_rows": 2}
        self.assertEqual(self._process().status_code, 302)
        self.assertEqual(self._snapshot()[0]["versao_processamento"], 7)
        self.assertEqual(self._notification_count(), 0)
        self.app.config["ETL_LIMITS"] = {}
        self.assertEqual(self._process().status_code, 200)
        self.assertEqual(self._snapshot()[0]["versao_processamento"], 8)
        self.assertEqual(self._state()[0], "EM_ANALISE")
        self.assertGreater(self._notification_count(), 0)

    def test_real_persistence_failure_rolls_back_savepoint_and_keeps_outer_transaction_usable(self):
        before = self._snapshot()
        sales, purchases = self.root / "sales.csv", self.root / "purchases.csv"
        sales.write_bytes(SALES)
        purchases.write_bytes(PURCHASES)
        failed = []

        def fail_curve_delete(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("DELETE FROM PRODUTO_CURVA_ABC") and not failed:
                failed.append(True)
                cursor.execute("SELECT 1 / 0")

        event.listen(self.engine, "before_cursor_execute", fail_curve_delete)
        try:
            with self.app.app_context():
                result = etl_processor.processar_arquivos_analise(
                    str(sales), str(purchases), self.analysis_id, db.session,
                )
                self.assertFalse(result.sucesso)
                self.assertEqual(db.session.scalar(text("SELECT 1")), 1)
                # Uma escrita independente confirma que só o SAVEPOINT ETL foi revertido.
                db.session.get(Analise, self.other_analysis_id).status_analise = "EM_ANALISE"
                db.session.commit()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_curve_delete)
        self.assertEqual(failed, [True])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._state(self.other_analysis_id)[0], "EM_ANALISE")
        self.assertEqual(self._notification_count(), 0)
        self.assertNotIn("SELECT", result.mensagem)
        self.assertNotIn("division", result.mensagem.lower())
        self.assertEqual(result.kpis, {})

    def test_same_analysis_concurrent_processing_is_serialized_by_real_row_lock(self):
        entered, release = threading.Event(), threading.Event()
        first_client, second_client = self._login_client("admin"), self._login_client("admin")
        first_csrf, second_csrf = self._upload_csrf(first_client), self._upload_csrf(second_client)
        original = admin.processar_arquivos_analise
        calls = []

        def pause_first(*args, **kwargs):
            calls.append(threading.current_thread().name)
            if len(calls) == 1:
                entered.set()
                if not release.wait(20):
                    raise AssertionError("Processamento privado de teste não foi liberado.")
            return original(*args, **kwargs)

        with patch.object(etl_uploads, "_UPLOAD_ADMISSION", _IndependentWorkerAdmissions()), \
                patch.object(admin, "processar_arquivos_analise", side_effect=pause_first):
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sec005-process") as executor:
                first = executor.submit(self._process, first_client, first_csrf)
                try:
                    self.assertTrue(entered.wait(10))
                    second = executor.submit(self._process, second_client, second_csrf)
                    self._wait_for_analysis_lock()
                    self.assertEqual(len(calls), 1)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=30).status_code, 200)
                self.assertEqual(second.result(timeout=30).status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self._snapshot()[0]["versao_processamento"], 9)
        self.assertEqual(self._snapshot()[0]["faturamento_total"], Decimal("30"))
        self.assertEqual(len(self._snapshot()[1]), 2)

    def test_customer_upload_waits_for_processing_and_rechecks_committed_status(self):
        entered, release = threading.Event(), threading.Event()
        customer = self._login_client("active")
        customer_csrf = self._csrf(customer, "/cliente/upload")
        original = admin.processar_arquivos_analise
        before_uploads = self._state()[1]

        def pause(*args, **kwargs):
            entered.set()
            if not release.wait(20):
                raise AssertionError("Processamento privado não liberado.")
            return original(*args, **kwargs)

        with patch.object(etl_uploads, "_UPLOAD_ADMISSION", _IndependentWorkerAdmissions()), \
                patch.object(admin, "processar_arquivos_analise", side_effect=pause):
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sec005-customer") as executor:
                processing = executor.submit(self._process)
                try:
                    self.assertTrue(entered.wait(10))
                    uploaded = executor.submit(self._upload, customer, customer_csrf, customer=True)
                    self._wait_for_analysis_lock()
                finally:
                    release.set()
                self.assertEqual(processing.result(timeout=30).status_code, 200)
                self.assertEqual(uploaded.result(timeout=30).status_code, 404)
        self.assertEqual(self._state()[0], "EM_ANALISE")
        for kind, previous in before_uploads.items():
            self.assertEqual(self._state()[1][kind]["digest"], previous["digest"])
            self.assertEqual(self._state()[1][kind]["reference"], previous["reference"])
        self.assertEqual(self._snapshot()[0]["faturamento_total"], Decimal("30"))

    def test_rejected_processing_keeps_lock_until_error_then_new_upload_stays_pending(self):
        entered, release = threading.Event(), threading.Event()
        uploader = self._login_client("admin")
        uploader_csrf = self._upload_csrf(uploader)
        original = admin.processar_arquivos_analise
        before = self._snapshot()
        # Campo do upload novo é menor que o orçamento; apenas o CSV já armazenado excede.
        self.app.config["ETL_LIMITS"] = {"max_rows": 2}

        def pause(*args, **kwargs):
            entered.set()
            if not release.wait(20):
                raise AssertionError("Processamento rejeitado não liberado.")
            return original(*args, **kwargs)

        with patch.object(etl_uploads, "_UPLOAD_ADMISSION", _IndependentWorkerAdmissions()), \
                patch.object(admin, "processar_arquivos_analise", side_effect=pause):
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sec005-rejected") as executor:
                processing = executor.submit(self._process)
                try:
                    self.assertTrue(entered.wait(10))
                    uploaded = executor.submit(self._upload, uploader, uploader_csrf)
                    self._wait_for_analysis_lock()
                finally:
                    release.set()
                self.assertEqual(processing.result(timeout=30).status_code, 302)
                self.assertEqual(uploaded.result(timeout=30).status_code, 302)
        status, uploads = self._state()
        self.assertEqual(status, "RELATORIO_RECEBIDO")
        self.assertEqual(uploads["VENDAS"]["digest"], hashlib.sha256(REPLACEMENT).hexdigest())
        self.assertEqual(uploads["VENDAS"]["status"], "PENDENTE")
        self.assertIsNone(uploads["VENDAS"]["message"])
        self.assertEqual(uploads["COMPRAS"]["status"], "ERRO")
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._notification_count(), 0)

    def test_non_admin_cannot_process_and_customer_cannot_replace_another_company(self):
        before, other_before = self._snapshot(), self._snapshot(self.other_analysis_id)
        customer = self._login_client("active")
        csrf = self._csrf(customer, "/cliente/upload")
        self.assertEqual(self._process(customer, csrf).status_code, 403)
        self.assertEqual(self._upload(customer, csrf, analysis=self.other_analysis_id, customer=True).status_code, 404)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._snapshot(self.other_analysis_id), other_before)
        self.assertEqual(self._notification_count(), 0)

    def test_stored_blob_actual_length_is_enforced_before_processing_despite_false_size_metadata(self):
        before = self._snapshot()
        with self.app.app_context():
            upload = db.session.scalar(select(UploadRelatorio).where(
                UploadRelatorio.id_analise == self.analysis_id,
                UploadRelatorio.tipo_relatorio == "VENDAS",
            ))
            key = upload.caminho_arquivo.removeprefix("db:")
            blob = db.session.get(ArquivoArmazenado, key)
            blob.tamanho = 1
            upload.tamanho_arquivo = 1
            db.session.commit()
        self.app.config["ETL_LIMITS"] = {"max_file_bytes": len(SALES) - 1}
        self.assertEqual(self._process().status_code, 302)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._notification_count(), 0)
        self.assertTrue(all(row["status"] == "ERRO" for row in self._state()[1].values()))
