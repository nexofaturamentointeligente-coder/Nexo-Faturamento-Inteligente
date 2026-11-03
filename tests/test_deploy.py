"""Regressão de preparação para deploy, sem serviços nem dados reais.

Executar da raiz: python -B -m unittest tests.test_deploy -v
Os arquivos temporários ficam em .audit_tmp/ e são removidos ao terminar.
"""

import ast
import contextlib
import io
from pathlib import Path
import re
from unittest.mock import patch

from tests.deploy_support import DeployTestCase, ROOT


VENDAS = "Produto;Quantidade;Valor total\nProduto A;2;20,00\nProduto B;1;5,00\n"
COMPRAS = "Fornecedor;Valor NF\nFornecedor fictício;40,00\n"


class DeployTests(DeployTestCase):
    def test_seed_initial_data_is_idempotent_on_test_database(self):
        import secrets
        from sqlalchemy import func, select
        from models import Plano, Segmento, Usuario
        from seed import seed_admin, seed_planos, seed_segmentos
        self.app.config.update(
            ADMIN_EMAIL="seed-test@example.invalid", ADMIN_NOME="Admin fictício",
            ADMIN_SENHA=secrets.token_urlsafe(24),
        )
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()):
                seed_planos()
                seed_segmentos()
                seed_admin(self.app)
                self.db.session.commit()
        for model, expected in ((Plano, 3), (Segmento, 3), (Usuario, 1)):
            self.assertEqual(self.db.session.scalar(select(func.count()).select_from(model)), expected)

    def test_migrations_empty_database_repeat_upgrade_and_metadata(self):
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from flask_migrate import upgrade
        from sqlalchemy import inspect, text

        expected_head = ScriptDirectory(str(ROOT / "migrations")).get_current_head()
        self.assertEqual(self.db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), expected_head)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            upgrade(directory=str(ROOT / "migrations"))
        self.assertEqual(set(inspect(self.db.engine).get_table_names()) - {"alembic_version"}, set(self.db.metadata.tables))
        with self.db.engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            self.assertEqual(compare_metadata(context, self.db.metadata), [])

    def test_health_routes_and_database_failure(self):
        saude = self.client.get("/healthz").json
        self.assertEqual(saude["status"], "ok")
        # Fora do Render não há RENDER_GIT_COMMIT: o campo existe e diz "desconhecida".
        self.assertEqual(saude["versao"], "desconhecida")
        response = self.client.get("/healthz/db")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["db"], "ok")
        with patch.object(self.db.session, "execute", side_effect=RuntimeError("simulated outage")):
            response = self.client.get("/healthz/db")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json, {"status": "erro", "db": "indisponivel"})
        self.assertNotIn(b"simulated outage", response.data)

    def test_public_pages_and_referenced_static_assets(self):
        for path in ("/", "/como-aderir", "/auth/login"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                for asset in re.findall(r'(?:src|href)=["\'](/static/[^"\']+)', response.get_data(as_text=True)):
                    self.assertEqual(self.client.get(asset).status_code, 200, asset)

    def test_templates_imports_and_static_references_exist(self):
        from jinja2 import meta, nodes
        for name in self.app.jinja_env.list_templates():
            with self.subTest(template=name):
                source, _, _ = self.app.jinja_env.loader.get_source(self.app.jinja_env, name)
                tree = self.app.jinja_env.parse(source)
                for included in meta.find_referenced_templates(tree):
                    if included:
                        self.app.jinja_env.get_template(included)
                for call in tree.find_all(nodes.Call):
                    if (isinstance(call.node, nodes.Name) and call.node.name == "url_for"
                            and call.args and isinstance(call.args[0], nodes.Const)
                            and call.args[0].value == "static"):
                        for argument in call.kwargs:
                            if argument.key == "filename" and isinstance(argument.value, nodes.Const):
                                self.assertTrue((ROOT / "static" / argument.value.value).is_file())
        for folder in (ROOT, ROOT / "blueprints"):
            for source in folder.glob("*.py"):
                tree = ast.parse(source.read_text(encoding="utf-8-sig"))
                for call in ast.walk(tree):
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                            and call.func.id == "render_template" and call.args
                            and isinstance(call.args[0], ast.Constant)):
                        self.app.jinja_env.get_template(call.args[0].value)

    def test_login_logout_admin_and_cliente_with_csrf(self):
        self.fixtures()
        for email, dashboard in (("admin-test@example.invalid", "/admin/dashboard"),
                                 ("cliente-test@example.invalid", "/cliente/dashboard")):
            with self.subTest(email=email):
                response = self.login(email)
                self.assertEqual(response.headers["Location"], dashboard)
                self.assertEqual(self.client.get(dashboard).status_code, 200)
                # SEC-002: GET apenas confirma e não encerra a sessão.
                self.assertEqual(self.client.get("/auth/logout").status_code, 200)
                self.assertEqual(self.client.get(dashboard).status_code, 200)
                self.logout()
                self.assertEqual(self.client.get(dashboard).status_code, 302)

    def test_missing_csrf_and_invalid_password_do_not_authenticate(self):
        self.fixtures()
        self.assertEqual(self.client.post("/auth/login", data={
            "email": "admin-test@example.invalid", "senha": self.password,
        }).status_code, 400)
        response = self.client.post("/auth/login", data={
            "email": "admin-test@example.invalid", "senha": "wrong-test-password",
            "csrf_token": self.csrf_token(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)

    def test_cliente_cannot_access_admin_or_other_company(self):
        self.fixtures()
        self.login()
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 403)
        for suffix in ("", "/pdf"):
            self.assertEqual(self.client.get(f"/cliente/analise/{self.outra_analise_id}{suffix}").status_code, 404)
        response = self.client.post("/cliente/upload", data={
            "id_analise": str(self.outra_analise_id), "tipo_relatorio": "VENDAS",
            "csrf_token": self.csrf_token("/cliente/upload"),
            "arquivo": (io.BytesIO(VENDAS.encode()), "vendas.csv"),
        })
        self.assertEqual(response.status_code, 404)

    def test_cliente_uploads_persist_in_database_and_survive_cache_removal(self):
        import storage
        from models import Analise, UploadRelatorio
        from sqlalchemy import select
        self.fixtures()
        self.login()
        for tipo, source in (("VENDAS", VENDAS), ("COMPRAS", COMPRAS)):
            response = self.client.post("/cliente/upload", data={
                "id_analise": str(self.analise_id), "tipo_relatorio": tipo, "valor_pdv_informado": "150.000,00",
                "csrf_token": self.csrf_token("/cliente/upload"),
                "arquivo": (io.BytesIO(source.encode()), f"{tipo.lower()}.csv"),
            })
            self.assertEqual(response.status_code, 302)
        self.db.session.remove()
        uploads = self.db.session.execute(select(UploadRelatorio)).scalars().all()
        self.assertEqual(len(uploads), 2)
        self.assertEqual(self.db.session.get(Analise, self.analise_id).status_analise, "RELATORIO_RECEBIDO")
        for upload in uploads:
            self.assertTrue(upload.caminho_arquivo.startswith("db:"))
            self.assertIsNone(upload.id_usuario_admin)
            expected = (VENDAS if upload.tipo_relatorio == "VENDAS" else COMPRAS).encode()
            path = Path(storage.resolver(upload.caminho_arquivo))
            self.assertTrue(path.is_relative_to(self.work_dir))
            self.assertEqual(path.read_bytes(), expected)
            path.unlink()
            self.assertEqual(Path(storage.resolver(upload.caminho_arquivo)).read_bytes(), expected)
        self.assertEqual(list((self.work_dir / "uploads").iterdir()), [])

    def test_storage_rollback_does_not_publish_blob(self):
        import storage
        storage.salvar(b"test-only", "rollback.txt")
        self.db.session.flush()
        self.db.session.rollback()
        self.assertIsNone(storage.obter("rollback.txt"))

    def test_etl_csv_and_xlsx_fictitious_data(self):
        from etl_processor import calcular_kpis_em_memoria
        from openpyxl import Workbook
        vendas = self.work_dir / "vendas.csv"
        compras = self.work_dir / "compras.csv"
        vendas.write_text(VENDAS, encoding="utf-8")
        compras.write_text(COMPRAS, encoding="utf-8")
        for ext in ("csv", "xlsx"):
            with self.subTest(format=ext):
                if ext == "xlsx":
                    for path, rows in ((vendas.with_suffix(".xlsx"), [["Produto", "Quantidade", "Valor total"], ["Produto A", 2, 20], ["Produto B", 1, 5]]),
                                       (compras.with_suffix(".xlsx"), [["Fornecedor", "Valor NF"], ["Fornecedor fictício", 40]])):
                        workbook = Workbook()
                        for row in rows:
                            workbook.active.append(row)
                        workbook.save(path)
                        workbook.close()
                result = calcular_kpis_em_memoria(str(vendas.with_suffix(f".{ext}")), str(compras.with_suffix(f".{ext}")))
                self.assertTrue(result.sucesso)
                self.assertEqual(result.kpis["faturamento_total"], 25)
                self.assertEqual(result.kpis["total_comprado"], 40)
                self.assertEqual(result.kpis["saldo_estimado_compras_vendas"], 15)
                self.assertEqual(len(result.kpis["curva_abc"]["produtos"]), 2)
                self.assertEqual(result.kpis["curva_abc"]["produtos"][0]["classe"], "A")

    def test_etl_rejects_invalid_input_with_validation_error(self):
        from etl_processor import calcular_kpis_em_memoria, ETLValidationError
        missing = self.work_dir / "missing.csv"
        with self.assertRaises(ETLValidationError):
            calcular_kpis_em_memoria(str(missing), str(missing))
        invalid = self.work_dir / "invalid.csv"
        invalid.write_text("x;y\nnot;a report\n", encoding="utf-8")
        with self.assertRaises(ETLValidationError):
            calcular_kpis_em_memoria(str(invalid), str(invalid))

    def test_published_analysis_pdf_response(self):
        from models import RelatorioAnalise
        analise, _ = self.fixtures()
        analise.status_analise = "CONCLUIDO"
        analise.relatorio = RelatorioAnalise(titulo="Análise fictícia", publicado=True,
                                            conclusao_estrategica="Conclusão usada somente em teste.")
        self.db.session.commit()
        self.login()
        response = self.client.get(f"/cliente/analise/{self.analise_id}/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertIn(b"%%EOF", response.data[-100:])
        self.assertIn("attachment", response.headers["Content-Disposition"])
