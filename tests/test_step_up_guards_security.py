"""Hardening complementar (FASE 6): reautenticação na exclusão irreversível,
análise publicada protegida contra reprocessamento/troca de arquivos, assunto
de chamado em uma linha, e-mails sem injeção de cabeçalho nem destinatários
de empresas encerradas, e-mail de chamado só após o COMMIT."""

import io
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .deploy_support import DeployTestCase
from .test_etl_upload_security import CSV
import emails


class StepUpAndGuardsSecurityTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()

    # ------------------------------------------------------------ helpers
    def _csrf_de(self, client, path):
        import re
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return match.group(1)

    def _empresa(self, id_empresa=None):
        from models import Empresa
        self.db.session.expire_all()
        return self.db.session.get(Empresa, self.empresa_id if id_empresa is None else id_empresa)

    @property
    def empresa_id(self):
        from models import Analise
        return self.db.session.get(Analise, self.analise_id).id_empresa

    def _na_lixeira(self):
        from company_security import excluir_empresa
        id_empresa = self.empresa_id
        excluir_empresa(self.db.session, id_empresa)
        self.db.session.commit()
        return id_empresa

    def _publicar(self):
        from models import RelatorioAnalise
        self.db.session.add(RelatorioAnalise(
            id_analise=self.analise_id, titulo="Titulo publicado", resumo_executivo="Resumo",
            conclusao_estrategica="Conclusao", publicado=True, data_publicacao=datetime.now(timezone.utc)))
        self.db.session.commit()

    def _uploads(self):
        from models import UploadRelatorio
        self.db.session.expire_all()
        return self.db.session.scalars(select(UploadRelatorio).where(
            UploadRelatorio.id_analise == self.analise_id)).all()

    # ------------------------------------------------------------ step-up
    def test_permanent_delete_requires_the_admin_password(self):
        id_empresa = self._na_lixeira()
        self.login("admin-test@example.invalid")
        caminho = f"/admin/empresa/excluir-permanente/{id_empresa}"
        for dados in ({}, {"senha_confirmacao": ""}, {"senha_confirmacao": self.password + "x"}):
            with self.subTest(dados=sorted(dados)):
                response = self.client.post(caminho, data={**dados, "csrf_token": self.csrf_token("/admin/lixeira")})
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/admin/lixeira"))
                self.assertIsNotNone(self._empresa(id_empresa))
        pagina = self.client.get("/admin/lixeira").get_data(as_text=True)
        self.assertIn("Confirme sua senha de administrador", pagina)
        response = self.client.post(caminho, data={
            "senha_confirmacao": self.password, "csrf_token": self.csrf_token("/admin/lixeira"),
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self._empresa(id_empresa))

    def test_trash_modal_asks_for_the_password(self):
        self.login("admin-test@example.invalid")
        pagina = self.client.get("/admin/lixeira").get_data(as_text=True)
        self.assertIn('name="senha_confirmacao"', pagina)
        self.assertIn('autocomplete="current-password"', pagina)
        self.assertRegex(pagina, r'<form[^>]*id="confirmPurgeForm"[^>]*>(?s:.*?)name="senha_confirmacao"')

    # ------------------------------------------------------------ análise publicada
    def test_published_analysis_refuses_reprocessing_and_file_replacement(self):
        from models import Analise, UploadRelatorio
        self.login("admin-test@example.invalid")
        upload = f"/admin/analise/{self.analise_id}/upload"
        # Antes de publicar, o envio de arquivo é aceito normalmente.
        response = self.client.post(upload, data={
            "tipo_relatorio": "VENDAS", "valor_pdv_informado": "150.000,00", "arquivo": (io.BytesIO(CSV), "vendas.csv"),
            "csrf_token": self.csrf_token(upload),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        self.assertEqual([u.tipo_relatorio for u in self._uploads()], ["VENDAS"])
        self._publicar()
        hashes = {u.tipo_relatorio: u.hash_arquivo for u in self._uploads()}
        response = self.client.post(upload, data={
            "tipo_relatorio": "COMPRAS", "arquivo": (io.BytesIO(CSV), "compras.csv"),
            "csrf_token": self.csrf_token(upload),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/admin/analise/{self.analise_id}/relatorio"))
        self.assertEqual({u.tipo_relatorio: u.hash_arquivo for u in self._uploads()}, hashes)
        with patch("blueprints.admin.processar_arquivos_analise") as processar:
            response = self.client.post(f"/admin/analise/{self.analise_id}/processar", data={
                "csrf_token": self.csrf_token("/admin/dashboard"),
            })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/admin/analise/{self.analise_id}/relatorio"))
        processar.assert_not_called()
        self.db.session.expire_all()
        self.assertNotEqual(self.db.session.get(Analise, self.analise_id).status_analise, "EM_ANALISE")
        self.assertEqual(self.db.session.scalar(select(UploadRelatorio.status_processamento).where(
            UploadRelatorio.id_analise == self.analise_id)), "PENDENTE")

    # ------------------------------------------------------------ chamados e e-mails
    def test_ticket_subject_is_a_single_capped_line(self):
        from blueprints.cliente import CATEGORIAS_TICKET
        from models import ChamadoSuporte
        self.login()
        assunto = "Primeira linha\r\nBcc: alvo@example.invalid\n" + "x" * 300
        response = self.client.post("/cliente/tickets", data={
            "assunto": assunto, "categoria": next(iter(CATEGORIAS_TICKET)),
            "mensagem": "Mensagem fictícia.", "csrf_token": self.csrf_token("/cliente/tickets"),
        })
        self.assertEqual(response.status_code, 302)
        chamado = self.db.session.scalar(select(ChamadoSuporte))
        self.assertIsNotNone(chamado)
        self.assertNotIn("\n", chamado.assunto)
        self.assertNotIn("\r", chamado.assunto)
        self.assertLessEqual(len(chamado.assunto), 160)
        self.assertTrue(chamado.assunto.startswith("Primeira linha Bcc: alvo@example.invalid x"))

    def test_email_subject_is_sanitized_and_closed_companies_get_no_mail(self):
        enviados = []

        class ThreadImediata:
            def __init__(self, target, args, daemon):
                enviados.append(args[1])

            def start(self):
                pass
        self.app.config["MAIL_ATIVO"] = True
        with self.app.test_request_context("/"), patch.object(emails.threading, "Thread", ThreadImediata):
            ok = emails.enviar_email("dest@example.invalid", "Linha 1\r\nBcc: x@example.invalid\n" + "y" * 300,
                                     "ticket_resolvido.html", chamado=type("C", (), {
                                         "assunto": "a", "empresa": self._empresa(), "id_chamado": 1})(),
                                     link_url="https://nexo-test.example.invalid/x")
        self.assertTrue(ok)
        self.assertEqual(len(enviados), 1)
        assunto = enviados[0].subject
        self.assertNotIn("\n", assunto)
        self.assertNotIn("\r", assunto)
        self.assertLessEqual(len(assunto), 200)
        self.assertTrue(assunto.startswith("Linha 1 Bcc: x@example.invalid y"))

        empresa = self._empresa()
        self.assertIn("cliente-test@example.invalid", emails._emails_da_empresa(empresa))
        empresa.status_conta = "CANCELADA"
        self.assertEqual(emails._emails_da_empresa(empresa), [])
        empresa.status_conta = "ATIVA"
        empresa.deletado_em = datetime.now(timezone.utc)
        self.assertEqual(emails._emails_da_empresa(empresa), [])
        self.assertEqual(emails._emails_da_empresa(None), [])
        self.db.session.rollback()

    def test_ticket_resolved_email_is_sent_only_after_a_confirmed_commit(self):
        from blueprints import admin
        from models import ChamadoSuporte, MensagemSuporte
        chamado = ChamadoSuporte(id_empresa=self.empresa_id, id_usuario_cliente=self.cliente_id,
                                 assunto="Chamado fictício", categoria="DUVIDA_TECNICA",
                                 status_chamado="ABERTO", prioridade="MEDIA")
        self.db.session.add(chamado)
        self.db.session.flush()
        self.db.session.add(MensagemSuporte(id_chamado=chamado.id_chamado, id_usuario_remetente=self.cliente_id,
                                            mensagem="Mensagem fictícia."))
        self.db.session.commit()
        id_chamado = chamado.id_chamado
        self.login("admin-test@example.invalid")
        caminho = f"/admin/tickets/{id_chamado}"
        dados = {"acao": "alterar_status", "status_chamado": "RESOLVIDO"}
        with patch.object(admin, "email_ticket_resolvido", return_value=True) as aviso, \
                patch.object(self.db.session, "commit", side_effect=SQLAlchemyError("PRIVATE_COMMIT_SENTINEL")):
            response = self.client.post(caminho, data={**dados, "csrf_token": self.csrf_token(caminho)})
        self.assertEqual(response.status_code, 302)
        aviso.assert_not_called()
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(ChamadoSuporte, id_chamado).status_chamado, "ABERTO")
        with patch.object(admin, "email_ticket_resolvido", return_value=True) as aviso:
            response = self.client.post(caminho, data={**dados, "csrf_token": self.csrf_token(caminho)})
        self.assertEqual(response.status_code, 302)
        aviso.assert_called_once()
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(ChamadoSuporte, id_chamado).status_chamado, "RESOLVIDO")


if __name__ == "__main__":
    unittest.main()
