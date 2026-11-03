"""NEXO-SEC-006: regressões Flask/Werkzeug com entradas pequenas e sintéticas.

Os probes Flask mínimos isolam a correção de Vary, que os guards do NEXO
poderiam mascarar ao acessar a sessão primeiro. As integrações usam a aplicação
real, CSRF ativo e bancos temporários da fixture existente. Limites reduzidos
são exclusivos destes testes; nenhuma configuração de produção é alterada.
"""

import hashlib
import io
import unittest

from flask import Flask, jsonify, request, session, url_for
from flask_login import login_required
from openpyxl import Workbook
from sqlalchemy import select
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.formparser import MultiPartParser
from werkzeug.middleware.proxy_fix import ProxyFix

from tests.deploy_support import DeployTestCase


CSV = b"Produto;Quantidade;Valor total\nProduto ficticio;2;20,00\n"


class SessionVaryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(SECRET_KEY="synthetic-sec006-session-key", TESTING=True)

        @self.app.get("/probe/<operation>")
        def probe(operation):
            if operation == "contains_present":
                result = "existing" in session
            elif operation == "contains_missing":
                result = "missing" in session
            elif operation == "length":
                result = len(session)
            elif operation == "getitem":
                result = session["existing"]
            elif operation == "get":
                result = session.get("existing")
            elif operation == "setdefault_existing":
                result = session.setdefault("existing", "unused")
            elif operation == "setdefault_new":
                result = session.setdefault("new", "synthetic-new-value")
            else:
                raise AssertionError("Operação desconhecida no teste isolado.")
            return jsonify(result=result)

        @self.app.get("/public")
        def public():
            return "synthetic-public-data"

        self.client = self.app.test_client()
        self._seed_cookie()

    def _seed_cookie(self, empty=False):
        with self.client.session_transaction() as data:
            data.clear()
            if not empty:
                data["existing"] = "synthetic-session-value"

    def _assert_cookie_vary(self, response, *, writes_cookie=False):
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cookie", response.vary)
        self.assertEqual("Set-Cookie" in response.headers, writes_cookie)

    def test_membership_for_present_and_missing_keys_varies_on_cookie(self):
        for operation, expected in (("contains_present", True), ("contains_missing", False)):
            with self.subTest(operation=operation):
                response = self.client.get("/probe/" + operation)
                self._assert_cookie_vary(response)
                self.assertIs(response.json["result"], expected)

    def test_length_for_populated_and_empty_session_varies_on_cookie(self):
        for empty, expected in ((False, 1), (True, 0)):
            with self.subTest(empty=empty):
                self._seed_cookie(empty=empty)
                response = self.client.get("/probe/length")
                self._assert_cookie_vary(response)
                self.assertEqual(response.json["result"], expected)

    def test_read_operations_keep_vary_without_rewriting_cookie(self):
        for operation in ("getitem", "get", "setdefault_existing"):
            with self.subTest(operation=operation):
                response = self.client.get("/probe/" + operation)
                self._assert_cookie_vary(response)
                self.assertEqual(response.json["result"], "synthetic-session-value")

    def test_setdefault_new_key_varies_and_sets_cookie(self):
        response = self.client.get("/probe/setdefault_new")
        self._assert_cookie_vary(response, writes_cookie=True)
        self.assertEqual(response.json["result"], "synthetic-new-value")

    def test_untouched_session_does_not_force_cookie_vary(self):
        response = self.client.get("/public")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Cookie", response.vary)
        self.assertNotIn("Set-Cookie", response.headers)


class MultipartMemoryCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _body(size):
        return (b"--sec006-boundary\r\n"
                b'Content-Disposition: form-data; name="synthetic"\r\n\r\n'
                + b"x" * size + b"\r\n--sec006-boundary--\r\n")

    def test_field_budget_is_cumulative_across_small_chunks(self):
        # Cada bloco é menor que o orçamento. A versão vulnerável só limita
        # o buffer transitório e aceita o campo acumulado acima do orçamento.
        body = self._body(256)
        parser = MultiPartParser(max_form_memory_size=128, buffer_size=64)
        with self.assertRaises(RequestEntityTooLarge):
            parser.parse(io.BytesIO(body), b"sec006-boundary", len(body))

    def test_small_multipart_field_remains_valid(self):
        body = self._body(32)
        parser = MultiPartParser(max_form_memory_size=128, buffer_size=64)
        form, files = parser.parse(io.BytesIO(body), b"sec006-boundary", len(body))
        self.assertEqual(form["synthetic"], "x" * 32)
        self.assertEqual(len(files), 0)


class NexoFormUploadCompatibilityTests(DeployTestCase):
    def setUp(self):
        super().setUp()

        @login_required
        def form_probe():
            return jsonify(length=len(request.form.get("value", "")))

        @login_required
        def external_probe():
            return url_for("auth.recuperar_senha", _external=True)

        self.app.add_url_rule("/_sec006/form", "sec006_form", form_probe, methods=["POST"])
        self.app.add_url_rule("/_sec006/external", "sec006_external", external_probe)
        self.fixtures()
        self.login_response = self.login()

    def _upload(self, contents=CSV, *, filename="synthetic.csv", kind="VENDAS",
                extra=None, csrf=True, include_file=True):
        data = {"id_analise": str(self.analise_id), "tipo_relatorio": kind}
        if kind == "VENDAS":
            data["valor_pdv_informado"] = "150.000,00"
        if csrf:
            data["csrf_token"] = self.csrf_token("/cliente/upload")
        if include_file:
            data["arquivo"] = (io.BytesIO(contents), filename)
        data.update(extra or {})
        return self.client.post("/cliente/upload", data=data, content_type="multipart/form-data")

    def _uploads(self):
        from models import UploadRelatorio
        return self.db.session.scalars(select(UploadRelatorio).order_by(UploadRelatorio.id_upload)).all()

    def _assert_safe_response(self, response, status):
        self.assertEqual(response.status_code, status)
        self.assertNotIn(b"Traceback", response.data)
        self.assertNotIn(b"SQLALCHEMY", response.data)

    @staticmethod
    def _xlsx():
        workbook = Workbook()
        workbook.active.append(["Fornecedor", "Valor NF"])
        workbook.active.append(["Fornecedor ficticio", 40])
        stream = io.BytesIO()
        try:
            workbook.save(stream)
            return stream.getvalue()
        finally:
            workbook.close()

    def test_authenticated_response_keeps_cookie_vary_and_cookie_policy(self):
        response = self.client.get("/cliente/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cookie", response.vary)
        self.assertIn("HttpOnly", self.login_response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", self.login_response.headers["Set-Cookie"])

    def test_valid_csv_and_xlsx_uploads_preserve_stored_bytes_and_hashes(self):
        import storage
        xlsx = self._xlsx()
        expected = {"VENDAS": CSV, "COMPRAS": xlsx}
        for kind, contents, filename in (
            ("VENDAS", CSV, "synthetic.csv"),
            ("COMPRAS", xlsx, "synthetic.xlsx"),
        ):
            response = self._upload(contents, filename=filename, kind=kind)
            self._assert_safe_response(response, 302)
        uploads = self._uploads()
        self.assertEqual(len(uploads), 2)
        for upload in uploads:
            contents = expected[upload.tipo_relatorio]
            self.assertEqual(upload.tamanho_arquivo, len(contents))
            self.assertEqual(upload.hash_arquivo, hashlib.sha256(contents).hexdigest())
            self.assertTrue(upload.caminho_arquivo.startswith("db:"))
            stored = storage.obter(upload.caminho_arquivo[3:])
            self.assertEqual(stored.conteudo, contents)

    def test_empty_file_does_not_create_upload(self):
        response = self._upload(b"")
        self._assert_safe_response(response, 302)
        self.assertEqual(self._uploads(), [])
        body = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("Arquivo vazio", body)

    def test_missing_file_does_not_create_upload(self):
        response = self._upload(include_file=False)
        self._assert_safe_response(response, 302)
        self.assertEqual(self._uploads(), [])

    def test_malformed_multipart_cannot_create_upload(self):
        csrf = self.csrf_token("/cliente/upload")
        response = self.client.post(
            "/cliente/upload",
            data=b'--broken\r\nContent-Disposition: form-data; name="id_analise"\r\n\r\n1',
            content_type="multipart/form-data; boundary=broken",
            headers={"X-CSRFToken": csrf},
        )
        # Werkzeug's silent multipart parser produces no usable form fields;
        # the existing NEXO validation redirects safely without storing data.
        self._assert_safe_response(response, 302)
        self.assertEqual(response.headers["Location"], "/cliente/upload")
        self.assertEqual(self._uploads(), [])

    def test_multipart_without_csrf_is_rejected_before_storage(self):
        response = self._upload(csrf=False)
        self._assert_safe_response(response, 400)
        self.assertEqual(self._uploads(), [])

    def test_multipart_total_body_limit_returns_413_without_storage(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 1024
        response = self._upload(b"x" * 2048)
        self._assert_safe_response(response, 413)
        self.assertEqual(self._uploads(), [])

    def test_multipart_part_limit_accepts_five_and_rejects_six_parts(self):
        self.app.config["MAX_FORM_PARTS"] = 5
        self._assert_safe_response(self._upload(), 302)
        self.assertEqual(len(self._uploads()), 1)
        response = self._upload(extra={"synthetic_extra": "small"})
        self._assert_safe_response(response, 413)
        uploads = self._uploads()
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0].hash_arquivo, hashlib.sha256(CSV).hexdigest())

    def test_multipart_field_budget_rejects_large_field_without_overwriting_upload(self):
        self.app.config["MAX_FORM_MEMORY_SIZE"] = 16 * 1024
        self._assert_safe_response(self._upload(), 302)
        response = self._upload(extra={"synthetic_large_field": "x" * (32 * 1024)})
        self._assert_safe_response(response, 413)
        uploads = self._uploads()
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0].hash_arquivo, hashlib.sha256(CSV).hexdigest())

    def test_urlencoded_uses_total_limit_not_multipart_field_budget(self):
        # Werkzeug 3.1.9 deliberately applies the field-memory budget only to
        # multipart. URL-encoded bodies remain bounded by MAX_CONTENT_LENGTH.
        self.app.config.update(MAX_FORM_MEMORY_SIZE=128, MAX_CONTENT_LENGTH=4096)
        data = {"value": "x" * 1024, "csrf_token": self.csrf_token("/cliente/upload")}
        response = self.client.post("/_sec006/form", data=data)
        self._assert_safe_response(response, 200)
        self.assertEqual(response.json["length"], 1024)
        self.app.config["MAX_CONTENT_LENGTH"] = 512
        response = self.client.post("/_sec006/form", data=data)
        self._assert_safe_response(response, 413)

    def test_forwarded_https_keeps_external_url_generation_compatible(self):
        self.app.wsgi_app = ProxyFix(self.app.wsgi_app, x_for=1, x_proto=1)
        response = self.client.get("/_sec006/external", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "https://localhost/auth/recuperar-senha")


if __name__ == "__main__":
    unittest.main()
