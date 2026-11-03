"""SEC-005: uploads e materialização limitada com arquivos e bancos sintéticos."""

import hashlib
import io
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpyxl import Workbook
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.datastructures import FileStorage

from tests.deploy_support import DeployTestCase
from extensions import db
from models import Analise, ArquivoArmazenado, Notificacao, UploadRelatorio
from etl_inputs import ETLInputError
import etl_uploads
import storage


CSV = b"Produto;Quantidade;Valor total\nProduto sintetico;2;20,00\n"
PURCHASES = b"Fornecedor;Valor NF\nFornecedor sintetico;40,00\n"


def xlsx_bytes():
    workbook = Workbook()
    workbook.active.append(["Fornecedor", "Valor NF"])
    workbook.active.append(["Fornecedor sintetico", 40])
    stream = io.BytesIO()
    try:
        workbook.save(stream)
        return stream.getvalue()
    finally:
        workbook.close()


class UploadReadBoundsTests(unittest.TestCase):
    def test_exact_byte_limit_keeps_sha_and_preflights_owned_file(self):
        observed = []
        def validate(path, extension, limits):
            observed.append(Path(path))
            self.assertEqual(Path(path).read_bytes(), CSV)
            self.assertEqual(extension, ".csv")
        with patch.object(etl_uploads, "run_validation", side_effect=validate):
            contents, digest = etl_uploads.ler_upload_validado(
                io.BytesIO(CSV), "csv", SimpleNamespace(max_file_bytes=len(CSV)),
            )
        self.assertEqual(contents, CSV)
        self.assertEqual(digest, hashlib.sha256(CSV).hexdigest())
        self.assertFalse(observed[0].exists())
        self.assertFalse(observed[0].parent.exists())

    def test_one_byte_above_limit_is_rejected_before_validation(self):
        with patch.object(etl_uploads, "run_validation") as validate:
            with self.assertRaises(ETLInputError) as error:
                etl_uploads.ler_upload_validado(io.BytesIO(CSV + b"x"), "csv", SimpleNamespace(max_file_bytes=len(CSV)))
        self.assertEqual(error.exception.code, "file_size")
        validate.assert_not_called()

    def test_stream_is_always_read_with_a_finite_bound_without_content_length(self):
        class TracedStream(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= 64 * 1024:
                    raise AssertionError("Leitura ilimitada proibida")
                return super().read(size)
        arquivo = FileStorage(stream=TracedStream(CSV), filename="vendas.csv", content_length=0)
        with patch.object(etl_uploads, "run_validation"):
            contents, _ = etl_uploads.ler_upload_validado(arquivo, "csv", SimpleNamespace(max_file_bytes=len(CSV)))
        self.assertEqual(contents, CSV)

    def test_empty_stream_is_rejected_before_validation(self):
        with patch.object(etl_uploads, "run_validation") as validate:
            with self.assertRaises(ETLInputError) as error:
                etl_uploads.ler_upload_validado(io.BytesIO(), "csv", SimpleNamespace(max_file_bytes=10))
        self.assertEqual(error.exception.code, "empty_file")
        validate.assert_not_called()

    def test_validation_failure_removes_only_owned_temporary_directory(self):
        observed = []
        with tempfile.TemporaryDirectory(prefix="nexo-upload-source-test-") as directory:
            untouched = Path(directory) / "source.csv"
            untouched.write_bytes(CSV)
            def fail(path, extension, limits):
                observed.append(Path(path))
                raise ETLInputError("malformed", "Arquivo inválido.")
            with patch.object(etl_uploads, "run_validation", side_effect=fail):
                with self.assertRaises(ETLInputError):
                    etl_uploads.ler_upload_validado(io.BytesIO(CSV), "csv", SimpleNamespace(max_file_bytes=len(CSV)))
            self.assertEqual(untouched.read_bytes(), CSV)
        self.assertFalse(observed[0].parent.exists())


class ETLUploadSecurityTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        self.login()
        self.admin_client = self.app.test_client()
        response = self.admin_client.post("/auth/login", data={
            "email": "admin-test@example.invalid", "senha": self.password,
            "csrf_token": self._csrf(self.admin_client, "/auth/login"),
        })
        self.assertEqual(response.status_code, 302)

    def _csrf(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return match[1]

    def _upload(self, content=CSV, filename="vendas.csv", kind="VENDAS", *, admin=False,
                analysis=None, csrf=True, client=None):
        client = (self.admin_client if admin else self.client) if client is None else client
        identifier = self.analise_id if analysis is None else analysis
        path = f"/admin/analise/{identifier}/upload" if admin else "/cliente/upload"
        data = {"id_analise": str(identifier), "tipo_relatorio": kind,
                "arquivo": (io.BytesIO(content), filename)}
        if kind == "VENDAS":
            data["valor_pdv_informado"] = "150.000,00"
        if csrf:
            data["csrf_token"] = self._csrf(client, path)
        return client.post(path, data=data, content_type="multipart/form-data")

    def _snapshot(self):
        db.session.expire_all()
        uploads = db.session.execute(select(
            UploadRelatorio.id_upload, UploadRelatorio.id_analise,
            UploadRelatorio.caminho_arquivo, UploadRelatorio.hash_arquivo,
            UploadRelatorio.tamanho_arquivo, UploadRelatorio.status_processamento,
        ).order_by(UploadRelatorio.id_upload)).all()
        blobs = db.session.execute(select(
            ArquivoArmazenado.chave, ArquivoArmazenado.conteudo,
        ).order_by(ArquivoArmazenado.chave)).all()
        analyses = db.session.execute(select(
            Analise.id_analise, Analise.status_analise,
        ).order_by(Analise.id_analise)).all()
        notifications = db.session.execute(select(Notificacao.id_notificacao).order_by(Notificacao.id_notificacao)).all()
        return (uploads, [(key, hashlib.sha256(content).hexdigest()) for key, content in blobs], analyses, notifications)

    def test_admin_and_client_valid_csv_xlsx_preserve_bytes_sha_and_status(self):
        for admin in (False, True):
            for kind, content, name in (("VENDAS", CSV, "vendas.csv"), ("COMPRAS", xlsx_bytes(), "compras.xlsx")):
                with self.subTest(admin=admin, kind=kind):
                    response = self._upload(content, name, kind, admin=admin)
                    self.assertEqual(response.status_code, 302)
                    upload = db.session.scalar(select(UploadRelatorio).where(
                        UploadRelatorio.id_analise == self.analise_id, UploadRelatorio.tipo_relatorio == kind,
                    ).execution_options(populate_existing=True))
                    self.assertEqual(upload.hash_arquivo, hashlib.sha256(content).hexdigest())
                    self.assertEqual(upload.tamanho_arquivo, len(content))
                    self.assertEqual(storage.obter(upload.caminho_arquivo[3:]).conteudo, content)
                    self.assertEqual(upload.status_processamento, "PENDENTE")
                    self.assertEqual(upload.id_usuario_admin, self.admin_id if admin else None)
        db.session.expire_all()
        self.assertEqual(db.session.get(Analise, self.analise_id).status_analise, "RELATORIO_RECEBIDO")

    def test_empty_file_never_replaces_previous_upload_for_either_role(self):
        self.assertEqual(self._upload().status_code, 302)
        before = self._snapshot()
        for admin in (False, True):
            response = self._upload(b"", admin=admin)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self._snapshot(), before)
            html = (self.admin_client if admin else self.client).get(response.headers["Location"]).get_data(as_text=True)
            self.assertIn("Arquivo vazio", html)

    def test_oversize_file_never_replaces_previous_upload_for_either_role(self):
        self._upload()
        before = self._snapshot()
        self.app.config["ETL_LIMITS"] = {"max_file_bytes": len(CSV) - 1}
        for admin in (False, True):
            response = self._upload(admin=admin)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self._snapshot(), before)

    def test_invalid_extension_does_not_persist_for_either_role(self):
        before = self._snapshot()
        for admin in (False, True):
            self.assertEqual(self._upload(CSV, "vendas.exe", admin=admin).status_code, 302)
            self.assertEqual(self._snapshot(), before)

    def test_disguised_and_malformed_xlsx_do_not_replace_existing_upload(self):
        self._upload()
        before = self._snapshot()
        for content in (CSV, b"PK\x03\x04not-a-workbook", b"<html>invalid</html>"):
            for admin in (False, True):
                self.assertEqual(self._upload(content, "disfarce.xlsx", admin=admin).status_code, 302)
                self.assertEqual(self._snapshot(), before)

    def test_invalid_csrf_is_rejected_before_preflight_for_both_roles(self):
        before = self._snapshot()
        with patch.object(etl_uploads, "run_validation") as validate:
            for admin in (False, True):
                self.assertEqual(self._upload(admin=admin, csrf=False).status_code, 400)
        validate.assert_not_called()
        self.assertEqual(self._snapshot(), before)

    def test_client_cannot_upload_to_other_company_or_missing_analysis(self):
        before = self._snapshot()
        with patch.object(etl_uploads, "run_validation") as validate:
            for identifier in (self.outra_analise_id, 2147483647):
                self.assertEqual(self._upload(analysis=identifier).status_code, 404)
        validate.assert_not_called()
        self.assertEqual(self._snapshot(), before)

    def test_client_cannot_upload_in_disallowed_status(self):
        db.session.get(Analise, self.analise_id).status_analise = "EM_ANALISE"
        db.session.commit()
        before = self._snapshot()
        with patch.object(etl_uploads, "run_validation") as validate:
            self.assertEqual(self._upload(csrf=False).status_code, 400)
            # A CSRF token obtained on a different valid page reaches the status guard.
            data = {"id_analise": str(self.analise_id), "tipo_relatorio": "VENDAS",
                    "arquivo": (io.BytesIO(CSV), "vendas.csv"),
                    "csrf_token": self._csrf(self.client, "/cliente/dashboard")}
            self.assertEqual(self.client.post("/cliente/upload", data=data).status_code, 404)
        validate.assert_not_called()
        self.assertEqual(self._snapshot(), before)

    def test_upload_preflight_never_runs_commercial_etl(self):
        from blueprints import admin
        with patch.object(admin, "processar_arquivos_analise", side_effect=AssertionError("ETL não pertence ao upload")):
            self.assertEqual(self._upload().status_code, 302)
            self.assertEqual(self._upload(admin=True).status_code, 302)

    def test_busy_admission_rejects_post_without_validation_but_keeps_get_available(self):
        before = self._snapshot()
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            with patch.object(etl_uploads, "run_validation") as validate:
                self.assertEqual(self._upload().status_code, 302)
                self.assertEqual(self._upload(admin=True).status_code, 302)
                self.assertEqual(self.client.get("/cliente/upload").status_code, 200)
            validate.assert_not_called()
            self.assertEqual(self._snapshot(), before)
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()
        self.assertEqual(self._upload().status_code, 302)
        self.assertEqual(len(self._snapshot()[0]), 1)

    def test_busy_admission_does_not_bypass_csrf(self):
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            self.assertEqual(self._upload(csrf=False).status_code, 400)
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()

    def test_busy_processing_uses_shared_admission_before_storage_and_status_changes(self):
        from blueprints import admin
        before = self._snapshot()
        csrf = self._csrf(self.admin_client, "/admin/dashboard")
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            with patch.object(admin.storage, "materializar_para_etl") as materialize:
                with patch.object(admin, "processar_arquivos_analise") as process:
                    response = self.admin_client.post(
                        f"/admin/analise/{self.analise_id}/processar", data={"csrf_token": csrf},
                    )
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/admin/dashboard"))
            materialize.assert_not_called()
            process.assert_not_called()
            self.assertEqual(self._snapshot(), before)
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()

    def test_invalid_limits_fail_closed_without_modifying_previous_upload(self):
        self._upload()
        before = self._snapshot()
        for value in (0, -1, True, 21 * 1024 * 1024):
            self.app.config["ETL_LIMITS"] = {"max_file_bytes": value}
            for admin in (False, True):
                self.assertEqual(self._upload(admin=admin).status_code, 302)
                self.assertEqual(self._snapshot(), before)

    def test_database_failure_rolls_back_new_blob_and_keeps_legacy_source(self):
        legacy = self.work_dir / "legacy.csv"
        legacy.write_bytes(CSV)
        db.session.add(UploadRelatorio(
            id_analise=self.analise_id, id_usuario_admin=self.admin_id,
            tipo_relatorio="VENDAS", nome_arquivo_original="legacy.csv",
            caminho_arquivo=str(legacy), extensao_arquivo="CSV",
            tamanho_arquivo=len(CSV), hash_arquivo=hashlib.sha256(CSV).hexdigest(),
            status_processamento="PENDENTE",
        ))
        db.session.commit()
        before = self._snapshot()
        with patch.object(db.session, "commit", side_effect=SQLAlchemyError("PRIVATE_UPLOAD_SQL_SENTINEL")):
            response = self._upload(CSV + b"Outro;1;1\n", admin=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(legacy.read_bytes(), CSV)
        self.assertNotIn("PRIVATE_UPLOAD_SQL_SENTINEL", self.admin_client.get(response.headers["Location"]).get_data(as_text=True))

    def test_shared_legacy_reference_is_preserved_after_authorized_replacement(self):
        legacy = self.work_dir / "shared.csv"
        legacy.write_bytes(CSV)
        for identifier in (self.analise_id, self.outra_analise_id):
            db.session.add(UploadRelatorio(
                id_analise=identifier, id_usuario_admin=self.admin_id, tipo_relatorio="VENDAS",
                nome_arquivo_original="shared.csv", caminho_arquivo=str(legacy), extensao_arquivo="CSV",
                tamanho_arquivo=len(CSV), hash_arquivo=hashlib.sha256(CSV).hexdigest(), status_processamento="PENDENTE",
            ))
        db.session.commit()
        self.assertEqual(self._upload(admin=True).status_code, 302)
        self.assertEqual(legacy.read_bytes(), CSV)
        other = db.session.scalar(select(UploadRelatorio).where(UploadRelatorio.id_analise == self.outra_analise_id))
        self.assertEqual(other.caminho_arquivo, str(legacy))


class ETLStorageMaterializationTests(DeployTestCase):
    def test_database_materialization_is_owned_and_cleanup_preserves_blob(self):
        reference = storage.salvar(CSV, "source.csv")
        db.session.commit()
        with storage.materializar_para_etl(reference, len(CSV)) as path:
            materialized = Path(path)
            self.assertEqual(materialized.read_bytes(), CSV)
            self.assertNotEqual(materialized.parent, storage._CACHE_DIR)
        self.assertFalse(materialized.exists())
        self.assertEqual(storage.obter("source.csv").conteudo, CSV)

    def test_database_actual_length_rejects_metadata_lie_before_loading_blob(self):
        reference = storage.salvar(CSV, "oversize.csv")
        storage.obter("oversize.csv").tamanho = 1
        db.session.commit()
        statements = []
        def before(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.upper().lstrip())
        event.listen(db.engine, "before_cursor_execute", before)
        try:
            with self.assertRaises(ETLInputError) as error:
                with storage.materializar_para_etl(reference, 4):
                    self.fail("O BYTEA excessivo não pode ser materializado")
        finally:
            event.remove(db.engine, "before_cursor_execute", before)
        self.assertEqual(error.exception.code, "file_size")
        self.assertFalse(any(sql.startswith("SELECT ARQUIVO_ARMAZENADO.CONTEUDO") for sql in statements))

    def test_processing_copy_does_not_trust_or_delete_shared_cache(self):
        reference = storage.salvar(CSV, "cached.csv")
        db.session.commit()
        storage._CACHE_DIR.mkdir(parents=True, exist_ok=True)
        shared = storage._CACHE_DIR / "cached.csv"
        shared.write_bytes(b"synthetic-existing-cache")
        with storage.materializar_para_etl(reference, len(CSV)) as path:
            self.assertEqual(Path(path).read_bytes(), CSV)
        self.assertEqual(shared.read_bytes(), b"synthetic-existing-cache")

    def test_legacy_materialization_is_bounded_and_never_deletes_original(self):
        # SEC-004: referências legadas só são lidas dentro de UPLOAD_FOLDER.
        pasta = Path(self.app.config["UPLOAD_FOLDER"])
        pasta.mkdir(parents=True, exist_ok=True)
        source = pasta / "legacy.csv"
        source.write_bytes(CSV)
        with storage.materializar_para_etl(str(source), len(CSV)) as path:
            copy = Path(path)
            self.assertNotEqual(copy, source)
            self.assertEqual(copy.read_bytes(), CSV)
        self.assertFalse(copy.exists())
        self.assertEqual(source.read_bytes(), CSV)
        with self.assertRaises(ETLInputError):
            with storage.materializar_para_etl(str(source), len(CSV) - 1):
                self.fail("Arquivo legado acima do limite")
        self.assertEqual(source.read_bytes(), CSV)

    def test_legacy_reference_outside_upload_folder_is_not_materialized(self):
        outside = self.work_dir / "outside-legacy.csv"
        outside.write_bytes(CSV)
        for reference in (str(outside), "../outside-legacy.csv"):
            with self.subTest(reference=reference):
                with self.assertRaises(ETLInputError) as error:
                    with storage.materializar_para_etl(reference, len(CSV)):
                        self.fail("Arquivo fora de UPLOAD_FOLDER não pode ser materializado")
                self.assertEqual(error.exception.code, "storage_unavailable")
        self.assertEqual(outside.read_bytes(), CSV)

    def test_parallel_owned_copies_do_not_remove_each_other(self):
        reference = storage.salvar(CSV, "parallel.csv")
        db.session.commit()
        with storage.materializar_para_etl(reference, len(CSV)) as first:
            with storage.materializar_para_etl(reference, len(CSV)) as second:
                self.assertNotEqual(first, second)
            self.assertFalse(Path(second).exists())
            self.assertEqual(Path(first).read_bytes(), CSV)
        self.assertFalse(Path(first).exists())

    def test_failed_processing_context_cleans_owned_copy_only(self):
        reference = storage.salvar(CSV, "failure.csv")
        db.session.commit()
        with self.assertRaises(ETLInputError):
            with storage.materializar_para_etl(reference, len(CSV)) as path:
                owned = Path(path)
                raise ETLInputError("synthetic_failure", "Falha sintética.")
        self.assertFalse(owned.parent.exists())
        self.assertEqual(storage.obter("failure.csv").conteudo, CSV)

    def test_database_key_is_not_interpreted_as_destination_path(self):
        reference = storage.salvar(CSV, "../../synthetic-escape.csv")
        db.session.commit()
        with storage.materializar_para_etl(reference, len(CSV)) as path:
            self.assertEqual(Path(path).name, "arquivo.csv")
            self.assertEqual(Path(path).read_bytes(), CSV)

    def test_missing_empty_and_unsupported_sources_fail_with_safe_messages(self):
        empty = storage.salvar(b"", "empty.csv")
        db.session.commit()
        for reference in ("db:missing.csv", empty, "db:legacy.xls", str(self.work_dir / "missing.csv")):
            with self.subTest(reference=reference):
                with self.assertRaises(ETLInputError) as error:
                    with storage.materializar_para_etl(reference, 100):
                        self.fail("Fonte inválida")
                self.assertNotIn(str(self.work_dir), error.exception.safe_message)
