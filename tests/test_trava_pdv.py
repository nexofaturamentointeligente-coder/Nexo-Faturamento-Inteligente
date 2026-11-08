"""Trava de validação do total do PDV.

Os 3 cenários pedidos e as bordas, ponta a ponta: envio pela rota, ETL real em processo,
conferência e publicação pela rota, em SQLite descartável com as migrações reais. O
preflight e o worker isolados exigem Linux e ficam de fora aqui; as corridas com lock de
linha estão em tests/test_trava_pdv_postgresql.py. Dados sintéticos, sem .env e sem e-mail real.
"""

import contextlib
from datetime import date
from decimal import Decimal
import html
import io
import re
import unittest
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError

import etl_processor
from etl_processor import (
    ResultadoETL, STATUS_PDV_CONCLUIDO, STATUS_PDV_DIVERGENTE, conferir_total_pdv, em_centavos, faturamento_bruto,
    validar_total_pdv,
)
import etl_uploads
from notifications import classificar_notificacao
import preferencias_usuario
import validacao_pdv
from tests.contratos_support import ContratosCase
from tests.deploy_support import ROOT

# 1.500,00 no total, sem coluna de situação.
VENDAS = "produto;quantidade;faturamento\nArroz;10;1.000,00\nFeijão;5;500,00\n".encode()
COMPRAS = "fornecedor;total_compra\nFornecedor sintético;800,00\n".encode()
CLIENTE = "cliente-test@example.invalid"
ADMIN = "admin-test@example.invalid"


class RegraDaConferenciaTests(unittest.TestCase):
    """A comparação, sem banco."""

    def test_cenarios_pedidos(self):
        self.assertEqual(validar_total_pdv(150000.00, 150000.00)["status"], STATUS_PDV_CONCLUIDO)
        self.assertEqual(validar_total_pdv(150000.00, 150000.50)["status"], STATUS_PDV_CONCLUIDO)
        self.assertEqual(validar_total_pdv(150000.00, 150015.00)["status"], STATUS_PDV_DIVERGENTE)

    def test_fronteira_de_um_real_nao_depende_de_ponto_flutuante(self):
        # Em float, 131072.70 - 131071.70 dá 1.0000000000145519, e 1,00 viraria divergência.
        self.assertGreater(abs(131072.70 - 131071.70), 1.00)
        conferencia = validar_total_pdv(131072.70, 131071.70)
        self.assertEqual(conferencia["diferenca"], Decimal("1.00"))
        self.assertEqual(conferencia["status"], STATUS_PDV_CONCLUIDO)
        self.assertEqual(validar_total_pdv(100.00, 101.00)["status"], STATUS_PDV_CONCLUIDO)
        self.assertEqual(validar_total_pdv(100.00, 101.01)["status"], STATUS_PDV_DIVERGENTE)
        self.assertEqual(validar_total_pdv(101.01, 100.00)["status"], STATUS_PDV_DIVERGENTE)

    def test_diferenca_e_absoluta_e_registrada_em_centavos(self):
        conferencia = validar_total_pdv(1484.995, 1500)
        self.assertEqual(conferencia["faturamento_bruto_calculado"], Decimal("1485.00"))
        self.assertEqual(conferencia["valor_pdv_informado"], Decimal("1500.00"))
        self.assertEqual(conferencia["diferenca"], Decimal("15.00"))

    def test_ruido_da_soma_em_float_nao_vira_divergencia(self):
        soma = sum([0.1] * 10) * 100  # 99.99999999999999
        self.assertEqual(validar_total_pdv(soma, 101.00)["diferenca"], Decimal("1.00"))

    def test_valor_absurdo_nao_estoura_a_precisao_do_decimal(self):
        self.assertEqual(em_centavos(1e300).as_tuple().exponent, -2)

    def test_retorno_do_etl_leva_a_conferencia_para_auditoria(self):
        resultado = ResultadoETL(True, "ok", {"faturamento_total": 900.0}, {"valor_devolucoes": 100.0}, [])
        self.assertEqual(faturamento_bruto(resultado), 1000.0)
        conferido = conferir_total_pdv(resultado, 1015.0)
        self.assertEqual(conferido.telemetria["conferencia_pdv"]["status"], STATUS_PDV_DIVERGENTE)
        self.assertEqual(conferido.telemetria["conferencia_pdv"]["diferenca"], Decimal("15.00"))
        self.assertEqual(conferido.kpis, resultado.kpis)
        self.assertEqual(conferido.telemetria["valor_devolucoes"], 100.0)
        self.assertNotIn("conferencia_pdv", resultado.telemetria)


class ValorDigitadoTests(unittest.TestCase):
    def test_formatos_aceitos(self):
        for digitado, esperado in (
                ("150.000,00", "150000.00"), ("R$ 150.000,00", "150000.00"), ("r$150.000,00", "150000.00"),
                (" 150.000,00\xa0", "150000.00"), ("150000,00", "150000.00"), ("150000", "150000.00"),
                ("150.000", "150000.00"), ("1.234.567,89", "1234567.89"), ("1500.50", "1500.50"),
                ("0,50", "0.50"), ("12,5", "12.50"), ("999.999.999.999,99", "999999999999.99"),
                ("0", "0.00"), ("0,00", "0.00")):
            with self.subTest(digitado=digitado):
                self.assertEqual(validacao_pdv.interpretar_valor_digitado(digitado), Decimal(esperado))

    def test_recusa_o_que_o_conversor_de_planilha_leria_em_silencio(self):
        for digitado in (None, "", "   ", "abc", "R$", "150,000", "1.234,567", "1,234.56", "1234.567", "0.500",
                         "012.500", "-100,00", "(10,00)", "10,00-", "+10,00", "1e3", "1E+05", "12%", "1.2.3",
                         "R$R$ 10,00", "١٥٠", "１５０", "1.000.000.000.000,00", "9" * 40, ["150.000,00"], 150000):
            with self.subTest(digitado=digitado):
                with self.assertRaises(validacao_pdv.ValorPdvInvalido):
                    validacao_pdv.interpretar_valor_digitado(digitado)


class TravaPdvCase(ContratosCase):
    def setUp(self):
        super().setUp()
        # O preflight em processo isolado e o worker do ETL exigem Linux: o cálculo é o real, em processo.
        for simulacao in (patch.object(etl_uploads, "run_validation"),
                          patch("etl_processor.calcular_kpis_em_memoria", etl_processor._calcular_kpis_local)):
            simulacao.start()
            self.addCleanup(simulacao.stop)
        self.cli = self._entrar(CLIENTE)
        self.adm = self._entrar(ADMIN)
        self.pagina_admin = f"/admin/analise/{self.analise_id}/upload"
        self.editor = f"/admin/analise/{self.analise_id}/relatorio"

    # ------------------------------------------------------------ apoio HTTP
    def _csrf(self, cliente, caminho):
        resposta = cliente.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        achado = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', resposta.get_data(as_text=True))
        self.assertIsNotNone(achado, caminho)
        return achado.group(1)

    def _entrar(self, email):
        cliente = self.app.test_client()
        resposta = cliente.post("/auth/login", data={
            "email": email, "senha": self.password, "csrf_token": self._csrf(cliente, "/auth/login"),
        })
        self.assertEqual(resposta.status_code, 302)
        return cliente

    @staticmethod
    def _texto(resposta):
        return html.unescape(resposta.get_data(as_text=True))

    def _flashes(self, cliente):
        with cliente.session_transaction() as sessao:
            return [str(mensagem) for _categoria, mensagem in sessao.get("_flashes", ())]

    def _enviar_cliente(self, tipo="VENDAS", conteudo=VENDAS, total="1.500,00"):
        dados = {"id_analise": str(self.analise_id), "tipo_relatorio": tipo,
                 "arquivo": (io.BytesIO(conteudo), f"{tipo.lower()}.csv"),
                 "csrf_token": self._csrf(self.cli, "/cliente/upload")}
        if total is not None:
            dados["valor_pdv_informado"] = total
        return self.cli.post("/cliente/upload", data=dados, content_type="multipart/form-data")

    def _enviar_admin(self, tipo="VENDAS", conteudo=VENDAS, total="1.500,00"):
        dados = {"tipo_relatorio": tipo, "arquivo": (io.BytesIO(conteudo), f"{tipo.lower()}.csv"),
                 "csrf_token": self._csrf(self.adm, self.pagina_admin)}
        if total is not None:
            dados["valor_pdv_informado"] = total
        return self.adm.post(self.pagina_admin, data=dados, content_type="multipart/form-data")

    def _processar(self):
        return self.adm.post(f"/admin/analise/{self.analise_id}/processar",
                             data={"csrf_token": self._csrf(self.adm, self.pagina_admin)})

    def _enviar_e_processar(self, total, conteudo=VENDAS):
        self.assertEqual(self._enviar_cliente(conteudo=conteudo, total=total).status_code, 302)
        self.assertEqual(self._enviar_cliente(tipo="COMPRAS", conteudo=COMPRAS, total=None).status_code, 302)
        resposta = self._processar()
        self.assertEqual(resposta.status_code, 200, self._texto(resposta)[:400])
        return resposta

    def _publicar(self, acao="publicar"):
        pagina = self.adm.get(self.editor)
        self.assertEqual(pagina.status_code, 200)
        corpo = pagina.get_data(as_text=True)
        return self.adm.post(self.editor, data={
            "csrf_token": re.search(r'name="csrf_token"[^>]*value="([^"]+)"', corpo).group(1), "acao": acao,
            "versao_rascunho": re.search(r'name="versao_rascunho" value="(\d+)"', corpo).group(1),
            "titulo": "Título fictício", "resumo_executivo": "Resumo fictício", "conclusao_estrategica": "Conclusão fictícia",
        })

    # --------------------------------------------------------------- estado
    def _analise(self):
        from models import Analise
        self.db.session.expire_all()
        return self.db.session.get(Analise, self.analise_id)

    def _validacao(self):
        from models import ValidacaoPdv
        self.db.session.expire_all()
        return self.db.session.get(ValidacaoPdv, self.analise_id)

    def _publicada(self):
        relatorio = self._analise().relatorio
        return relatorio is not None and relatorio.publicado

    def _assert_nao_publicada(self):
        self.assertFalse(self._publicada())
        self.assertEqual(self._analise().status_analise, "EM_ANALISE")
        for sufixo in ("", "/pdf"):
            self.assertEqual(self.cli.get(f"/cliente/analise/{self.analise_id}{sufixo}").status_code, 404)

    def _sem_tabela(self):
        from flask_migrate import downgrade
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            downgrade(directory=str(ROOT / "migrations"), revision="c7d41e9a2b60")
        validacao_pdv._esquema_por_engine.clear()
        # O downgrade também tira as colunas da a8d3f6b91c24, que vem depois: como reiniciar o processo.
        preferencias_usuario._esquema_por_engine.clear()


class CenariosPedidosTests(TravaPdvCase):
    def test_1_total_identico_conclui_e_nao_publica_sozinho(self):
        self._enviar_e_processar("1.500,00")
        validacao = self._validacao()
        self.assertEqual(validacao.status_validacao, "CONCLUIDO")
        self.assertEqual(validacao.diferenca, Decimal("0.00"))
        self.assertEqual(validacao.faturamento_bruto_calculado, Decimal("1500.00"))
        # CONCLUIDO é da conferência: publicar continua sendo decisão humana.
        self._assert_nao_publicada()
        self.assertEqual(self._publicar().status_code, 302)
        self.assertTrue(self._publicada())
        self.assertEqual(self._analise().status_analise, "CONCLUIDO")
        self.assertEqual(self.cli.get(f"/cliente/analise/{self.analise_id}").status_code, 200)

    def test_2_diferenca_de_50_centavos_fica_na_tolerancia(self):
        self._enviar_e_processar("1.500,50")
        validacao = self._validacao()
        self.assertEqual(validacao.status_validacao, "CONCLUIDO")
        self.assertEqual(validacao.diferenca, Decimal("0.50"))
        self._assert_nao_publicada()
        self._publicar()
        self.assertTrue(self._publicada())

    def test_3_diferenca_de_15_reais_diverge_e_ativa_o_bloqueio(self):
        with patch("blueprints.admin.email_analise_publicada") as email:
            resposta = self._enviar_e_processar("1.515,00")
            validacao = self._validacao()
            self.assertEqual(validacao.status_validacao, "DIVERGENTE_PDV")
            self.assertEqual(validacao.diferenca, Decimal("15.00"))
            self.assertEqual(validacao.valor_pdv_informado, Decimal("1515.00"))
            self.assertEqual(validacao.faturamento_bruto_calculado, Decimal("1500.00"))
            self.assertIsNotNone(validacao.validado_em)
            corpo = self._texto(resposta)
            self.assertIn("publicação bloqueada", corpo)
            self.assertNotIn("Indicadores consolidados", corpo)
            # Bloqueio: a publicação é recusada no servidor, e nada chega ao cliente.
            self.assertEqual(self._publicar().status_code, 302)
            self.assertTrue(any("Publicação bloqueada" in m for m in self._flashes(self.adm)))
            self._assert_nao_publicada()
            email.assert_not_called()
        self.assertEqual(self._validacao().status_validacao, "DIVERGENTE_PDV")


class FluxoDaTravaTests(TravaPdvCase):
    def test_diferenca_de_exatamente_um_real_e_de_um_real_e_um_centavo(self):
        self._enviar_e_processar("1.501,00")
        self.assertEqual(self._validacao().status_validacao, "CONCLUIDO")
        self.assertEqual(self._enviar_admin(total="1.501,01").status_code, 302)
        self._processar()
        self.assertEqual(self._validacao().status_validacao, "DIVERGENTE_PDV")

    def test_compara_com_o_bruto_e_mantem_o_liquido_no_kpi(self):
        vendas = ("produto;quantidade;faturamento;status;operacao\n"
                  "Arroz;10;1.000,00;OK;VENDA\nArroz;1;100,00;OK;DEVOLUCAO\nFeijão;5;500,00;CANCELADA;VENDA\n").encode()
        self._enviar_e_processar("1.000,00", conteudo=vendas)
        validacao = self._validacao()
        self.assertEqual(validacao.status_validacao, "CONCLUIDO")
        self.assertEqual(validacao.faturamento_bruto_calculado, Decimal("1000.00"))
        self.assertEqual(validacao.valor_devolucoes, Decimal("100.00"))
        self.assertEqual(validacao.valor_cancelado, Decimal("500.00"))
        self.assertEqual(self._analise().indicador.faturamento_total, Decimal("900"))
        # Trocar o arquivo zera também as parcelas, que eram do arquivo anterior.
        self.assertEqual(self._enviar_admin(conteudo=vendas, total="1.000,00").status_code, 302)
        self.assertIsNone(self._validacao().valor_devolucoes)
        self.assertIsNone(self._validacao().valor_cancelado)

    def test_saida_da_divergencia_e_reenviar_vendas_e_processar(self):
        self._enviar_e_processar("1.515,00")
        self.assertEqual(self._enviar_admin(total="1.500,00").status_code, 302)
        self.assertEqual(self._validacao().status_validacao, "PENDENTE")
        self.assertIsNone(self._validacao().diferenca)
        self._publicar()
        self.assertTrue(any("ainda não foi conferido" in m for m in self._flashes(self.adm)))
        self.assertFalse(self._publicada())
        self._processar()
        self.assertEqual(self._validacao().status_validacao, "CONCLUIDO")
        self._publicar()
        self.assertTrue(self._publicada())

    def test_trocar_vendas_depois_de_conferir_exige_novo_processamento(self):
        self._enviar_e_processar("1.500,00")
        self.assertEqual(self._enviar_admin(conteudo=b"produto;quantidade;faturamento\nOutro;1;9,00\n").status_code, 302)
        self._publicar()
        self._assert_nao_publicada()

    def test_trocar_compras_nao_mexe_na_conferencia(self):
        self._enviar_e_processar("1.515,00")
        self.assertEqual(self._enviar_admin(tipo="COMPRAS", conteudo=COMPRAS, total=None).status_code, 302)
        self.assertEqual(self._validacao().status_validacao, "DIVERGENTE_PDV")
        self.assertEqual(self._validacao().diferenca, Decimal("15.00"))

    def test_reprocessar_sem_mudar_nada_mantem_a_divergencia(self):
        self._enviar_e_processar("1.515,00")
        self._processar()
        self.assertEqual(self._validacao().status_validacao, "DIVERGENTE_PDV")
        self._publicar()
        self._assert_nao_publicada()

    def test_falha_do_etl_nao_grava_conferencia_nova(self):
        self._enviar_e_processar("1.500,00")
        with patch("etl_processor.calcular_kpis_em_memoria",
                   side_effect=etl_processor.ETLValidationError("Falha sintética.")):
            self._enviar_admin(total="1.515,00")
            self._processar()
        self.assertEqual(self._validacao().status_validacao, "PENDENTE")
        self.assertEqual(self._analise().status_analise, "RELATORIO_RECEBIDO")
        self._publicar()
        self.assertFalse(self._publicada())

    def test_despublicar_e_reprocessar_com_divergencia_nao_republica(self):
        self._enviar_e_processar("1.500,00")
        self._publicar()
        self.assertTrue(self._publicada())
        self._publicar("despublicar")
        self.assertEqual(self._enviar_admin(total="1.515,00").status_code, 302)
        self._processar()
        self._publicar()
        self._assert_nao_publicada()

    def test_analise_publicada_recusa_troca_de_arquivo_e_mantem_a_conferencia(self):
        self._enviar_e_processar("1.500,00")
        self._publicar()
        self.assertEqual(self._enviar_admin(total="1.515,00").status_code, 302)
        self.assertEqual(self._validacao().valor_pdv_informado, Decimal("1500.00"))
        self.assertEqual(self._validacao().status_validacao, "CONCLUIDO")

    def test_corrigir_texto_de_devolutiva_publicada_continua_liberado(self):
        self._enviar_e_processar("1.500,00")
        self._publicar()
        self.assertEqual(self._publicar().status_code, 302)
        self.assertTrue(any("publicada" in m for m in self._flashes(self.adm)))

    def test_analise_sem_valor_anterior_a_trava_processa_avisa_e_publica(self):
        self._enviar_e_processar("1.500,00")
        self.db.session.delete(self._validacao())
        self.db.session.commit()
        resposta = self._processar()
        self.assertIn("Sem conferência com o PDV", self._texto(resposta))
        self.assertIsNone(self._validacao())
        self._publicar()
        self.assertTrue(self._publicada())

    def _pedir_ia(self):
        pagina = self.adm.get(self.editor).get_data(as_text=True)
        with patch("ai_devolutiva.gerar_devolutiva_ia", return_value={
                "resumo_executivo": "Resumo gerado", "conclusao_estrategica": "Conclusão gerada", "fonte": "local"}) as gerar:
            resposta = self.adm.post(self.editor, data={
                "csrf_token": re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina).group(1),
                "acao": "gerar_ia", "versao_rascunho": re.search(r'name="versao_rascunho" value="(\d+)"', pagina).group(1),
            })
        self.assertEqual(resposta.status_code, 302)
        return gerar

    def test_geracao_por_ia_bloqueada_durante_a_divergencia(self):
        self._enviar_e_processar("1.515,00")
        self._pedir_ia().assert_not_called()
        self.assertIsNone(self._analise().relatorio)
        self.assertTrue(any("Geração por IA bloqueada" in m for m in self._flashes(self.adm)))

    def test_geracao_por_ia_bloqueada_com_kpis_velhos_depois_do_reenvio(self):
        self._enviar_e_processar("1.515,00")
        self._enviar_admin(total="1.500,00")
        self.assertEqual(self._validacao().status_validacao, "PENDENTE")
        self._pedir_ia().assert_not_called()
        self.assertRegex(self.adm.get(self.editor).get_data(as_text=True), r'value="gerar_ia"[^>]*disabled')

    def test_geracao_por_ia_liberada_com_o_total_conferido(self):
        self._enviar_e_processar("1.500,00")
        self._pedir_ia().assert_called_once()

    def test_arquivo_com_valor_absurdo_diverge_em_vez_de_estourar_a_coluna(self):
        from models import ValidacaoPdv
        # 1E+13 é um EAN ou CNPJ deslocado para a coluna de valor: no PostgreSQL, Numeric(14, 2) estouraria.
        for coluna in ("faturamento_bruto_calculado", "diferenca", "valor_devolucoes", "valor_cancelado"):
            self.assertIsNone(ValidacaoPdv.__table__.c[coluna].type.precision, coluna)
        self._enviar_e_processar("1.500,00", conteudo=b"produto;quantidade;faturamento\nArroz;1;10.000.000.000.000,00\n")
        validacao = self._validacao()
        self.assertEqual(validacao.status_validacao, "DIVERGENTE_PDV")
        self.assertEqual(validacao.diferenca, Decimal("9999999998500"))

    def test_periodo_sem_vendas_validas_aceita_total_zero(self):
        vendas = b"produto;quantidade;faturamento\nArroz;0;0,00\n"
        self._enviar_e_processar("0,00", conteudo=vendas)
        self.assertEqual(self._validacao().status_validacao, "CONCLUIDO")
        self.assertEqual(self._validacao().valor_pdv_informado, Decimal("0.00"))

    def test_relatorio_sem_situacao_soma_as_linhas_negativas_e_o_card_explica(self):
        self._enviar_e_processar("1.000,00", conteudo=b"produto;quantidade;faturamento\nArroz;1;1.000,00\nFeijao;1;-100,00\n")
        validacao = self._validacao()
        self.assertEqual(validacao.faturamento_bruto_calculado, Decimal("900.00"))
        self.assertEqual(validacao.status_validacao, "DIVERGENTE_PDV")
        self.assertIsNone(validacao.valor_devolucoes)
        self.assertIn("não traz coluna de status ou operação", self._texto(self.adm.get(self.pagina_admin)))

    def test_log_registra_a_recusa_real_e_nao_a_visualizacao(self):
        self._enviar_e_processar("1.515,00")
        with self.assertNoLogs("blueprints.admin", level="WARNING"):
            with patch.object(self.app.logger, "warning") as aviso:
                self.adm.get(self.editor)
        aviso.assert_not_called()
        with patch.object(self.app.logger, "warning") as aviso:
            self._publicar()
        aviso.assert_called_once()
        self.assertNotIn("1.515", str(aviso.call_args))

    def test_rascunho_continua_liberado_durante_a_divergencia(self):
        self._enviar_e_processar("1.515,00")
        self.assertEqual(self._publicar("rascunho").status_code, 302)
        self.assertEqual(self._analise().relatorio.titulo, "Título fictício")
        self.assertFalse(self._publicada())

    def test_aviso_ao_cliente_nao_leva_valores_nem_promete_devolutiva(self):
        from models import Notificacao
        self._enviar_e_processar("1.515,00")
        textos = [n.texto for n in self.db.session.query(Notificacao).filter_by(id_usuario=self.cliente_id)]
        self.assertTrue(any("diferença no total de vendas informado" in t for t in textos), textos)
        for texto in textos:
            self.assertNotIn("R$", texto)
            self.assertNotIn("processamos", texto)
            # "Aberto" ao lado de "diferença" se leria como dívida.
            self.assertEqual(classificar_notificacao(texto)["rotulo"], "Aviso")

    def test_aviso_normal_quando_o_total_confere(self):
        from models import Notificacao
        self._enviar_e_processar("1.500,00")
        textos = [n.texto for n in self.db.session.query(Notificacao).filter_by(id_usuario=self.cliente_id)]
        self.assertTrue(any("Recebemos e processamos" in t for t in textos), textos)

    def test_excluir_a_analise_leva_a_conferencia(self):
        from models import ValidacaoPdv
        self._enviar_e_processar("1.515,00")
        self.db.session.delete(self._analise())
        self.db.session.commit()
        self.assertEqual(self.db.session.query(ValidacaoPdv).count(), 0)

    def test_excluir_quem_informou_preserva_a_conferencia(self):
        from horario import agora_utc
        from models import Usuario, ValidacaoPdv
        informante = Usuario(nome="Informante fictício", email="informante@example.invalid", role="ADMIN",
                             primeiro_acesso=False)
        informante.set_senha(self.password)
        self.db.session.add(informante)
        self.db.session.flush()
        self.db.session.add(ValidacaoPdv(id_analise=self.analise_id, valor_pdv_informado=Decimal("1"),
                                         status_validacao="PENDENTE", informado_em=agora_utc(),
                                         id_usuario_informante=informante.id_usuario))
        self.db.session.commit()
        self.db.session.delete(informante)
        self.db.session.commit()
        self.assertIsNone(self._validacao().id_usuario_informante)

    def test_restricoes_do_banco(self):
        from horario import agora_utc
        from models import ValidacaoPdv
        instante = agora_utc()
        base = dict(id_analise=self.analise_id, informado_em=instante)
        for campos in (
                dict(valor_pdv_informado=Decimal("-1"), status_validacao="PENDENTE"),
                dict(valor_pdv_informado=Decimal("1"), status_validacao="OUTRO"),
                dict(valor_pdv_informado=Decimal("1"), status_validacao="CONCLUIDO"),
                dict(valor_pdv_informado=Decimal("1"), status_validacao="PENDENTE", diferenca=Decimal("1")),
                dict(valor_pdv_informado=Decimal("1"), status_validacao="DIVERGENTE_PDV", validado_em=instante,
                     faturamento_bruto_calculado=Decimal("1"), diferenca=Decimal("-1"))):
            with self.subTest(campos=campos):
                self.db.session.add(ValidacaoPdv(**base, **campos))
                with self.assertRaises(IntegrityError):
                    self.db.session.flush()
                self.db.session.rollback()

    def test_modelo_e_migracao_tem_as_mesmas_restricoes(self):
        # Os testes rodam sobre a migração: sem isto, o modelo poderia divergir em silêncio.
        from sqlalchemy import CheckConstraint, inspect
        from models import ValidacaoPdv
        inspetor = inspect(self.db.engine)
        self.assertEqual(
            {r.name: " ".join(str(r.sqltext).split()) for r in ValidacaoPdv.__table__.constraints
             if isinstance(r, CheckConstraint)},
            {r["name"]: " ".join(r["sqltext"].split()) for r in inspetor.get_check_constraints("validacao_pdv")},
        )
        self.assertEqual(
            {fk.name: fk.ondelete for fk in ValidacaoPdv.__table__.foreign_key_constraints},
            {fk["name"]: fk["options"].get("ondelete") for fk in inspetor.get_foreign_keys("validacao_pdv")},
        )


class CorrecaoDoTotalTests(TravaPdvCase):
    """Erro de digitação: o portal não devolve o arquivo, então o total se corrige sozinho."""

    def _corrigir(self, total, cliente=None):
        cliente = cliente or self.adm
        return cliente.post(f"/admin/analise/{self.analise_id}/total-pdv", data={
            "valor_pdv_informado": total, "csrf_token": self._csrf(self.adm, self.pagina_admin),
        })

    def test_corrigir_o_total_e_processar_libera_a_publicacao(self):
        self._enviar_e_processar("1.515,00")
        self.assertEqual(self._corrigir("1.500,00").status_code, 302)
        validacao = self._validacao()
        self.assertEqual(validacao.status_validacao, "PENDENTE")
        self.assertEqual(validacao.valor_pdv_informado, Decimal("1500.00"))
        self.assertEqual(validacao.id_usuario_informante, self.admin_id)
        self._publicar()
        self.assertFalse(self._publicada())
        self._processar()
        self.assertEqual(self._validacao().status_validacao, "CONCLUIDO")
        self._publicar()
        self.assertTrue(self._publicada())

    def test_total_invalido_nao_muda_nada(self):
        self._enviar_e_processar("1.515,00")
        for total in ("", "abc", "150,000", "-1,00"):
            with self.subTest(total=total):
                self.assertEqual(self._corrigir(total).status_code, 302)
                self.assertEqual(self._validacao().status_validacao, "DIVERGENTE_PDV")
                self.assertEqual(self._validacao().valor_pdv_informado, Decimal("1515.00"))

    def test_sem_relatorio_de_vendas_nao_ha_o_que_corrigir(self):
        self.assertEqual(self._corrigir("1.500,00").status_code, 302)
        self.assertIsNone(self._validacao())

    def test_analise_publicada_recusa_a_correcao(self):
        self._enviar_e_processar("1.500,00")
        self._publicar()
        self._corrigir("9.999,00")
        self.assertEqual(self._validacao().valor_pdv_informado, Decimal("1500.00"))
        self.assertEqual(self._validacao().status_validacao, "CONCLUIDO")

    def test_analise_anterior_a_trava_ganha_conferencia(self):
        self._enviar_e_processar("1.500,00")
        self.db.session.delete(self._validacao())
        self.db.session.commit()
        self._corrigir("1.500,00")
        self.assertEqual(self._validacao().status_validacao, "PENDENTE")

    def test_cliente_nao_acessa_a_correcao(self):
        self._enviar_e_processar("1.515,00")
        resposta = self.cli.post(f"/admin/analise/{self.analise_id}/total-pdv", data={
            "valor_pdv_informado": "1.500,00", "csrf_token": self._csrf(self.cli, "/cliente/upload"),
        })
        self.assertEqual(resposta.status_code, 403)
        self.assertEqual(self._validacao().valor_pdv_informado, Decimal("1515.00"))

    def test_sem_csrf_nao_corrige(self):
        self._enviar_e_processar("1.515,00")
        resposta = self.adm.post(f"/admin/analise/{self.analise_id}/total-pdv", data={"valor_pdv_informado": "1.500,00"})
        self.assertEqual(resposta.status_code, 400)
        self.assertEqual(self._validacao().valor_pdv_informado, Decimal("1515.00"))

    def test_formulario_so_aparece_com_o_relatorio_de_vendas(self):
        self.assertNotIn("Corrigir só o total", self._texto(self.adm.get(self.pagina_admin)))
        self._enviar_cliente()
        self.assertIn("Corrigir só o total", self._texto(self.adm.get(self.pagina_admin)))


class ReferenciaTests(TravaPdvCase):
    def test_quinzenas_do_mesmo_mes_tem_rotulos_diferentes(self):
        from types import SimpleNamespace
        base = dict(mes_referencia=3, ano_referencia=2026)
        self.assertEqual(validacao_pdv.referencia(SimpleNamespace(quinzena_referencia=None, **base)), "03/2026")
        self.assertEqual(validacao_pdv.referencia(SimpleNamespace(quinzena_referencia=1, **base)), "03/2026 (1ª quinzena)")
        self.assertEqual(validacao_pdv.referencia(SimpleNamespace(quinzena_referencia=2, **base)), "03/2026 (2ª quinzena)")


class CampoDoFormularioTests(TravaPdvCase):
    def test_campo_como_pedido_so_no_envio_de_vendas(self):
        for cliente, pagina in ((self.cli, "/cliente/upload"), (self.adm, self.pagina_admin)):
            with self.subTest(pagina=pagina):
                corpo = cliente.get(pagina).get_data(as_text=True)
                self.assertEqual(corpo.count('name="valor_pdv_informado"'), 1)
                campo = re.search(r"<input[^>]*name=\"valor_pdv_informado\"[^>]*>", corpo).group(0)
                for atributo in ('type="text"', 'placeholder="Ex: 150.000,00"', "required"):
                    self.assertIn(atributo, campo)
                # Fora do label da dropzone, que abriria o seletor de arquivo.
                self.assertLess(corpo.index('name="valor_pdv_informado"'), corpo.index('name="arquivo"'))
                self.assertNotIn('name="valor_pdv_informado"', corpo[corpo.index("data-dz>"):corpo.index('name="arquivo"')])

    def test_cliente_sem_valor_valido_nao_grava_nem_le_o_arquivo(self):
        from models import UploadRelatorio
        for total in (None, "", "   ", "abc", "150,000", "-1,00"):
            with self.subTest(total=total):
                etl_uploads.run_validation.reset_mock()
                self.assertEqual(self._enviar_cliente(total=total).status_code, 302)
                etl_uploads.run_validation.assert_not_called()
                self.assertEqual(self.db.session.query(UploadRelatorio).count(), 0)
                self.assertIsNone(self._validacao())
                self.assertTrue(any("formato 150.000,00" in m for m in self._flashes(self.cli)))
                self.cli.get("/cliente/upload")  # consome o aviso

    def test_admin_sem_valor_valido_nao_grava(self):
        from models import UploadRelatorio
        for total in (None, "abc"):
            with self.subTest(total=total):
                self.assertEqual(self._enviar_admin(total=total).status_code, 302)
                self.assertEqual(self.db.session.query(UploadRelatorio).count(), 0)
                self.assertIsNone(self._validacao())

    def test_compras_nao_pede_nem_usa_o_valor(self):
        from models import UploadRelatorio
        self.assertEqual(self._enviar_cliente(tipo="COMPRAS", conteudo=COMPRAS, total=None).status_code, 302)
        self.assertEqual(self.db.session.query(UploadRelatorio).count(), 1)
        self.assertIsNone(self._validacao())
        self._enviar_cliente()
        self._enviar_cliente(tipo="COMPRAS", conteudo=COMPRAS, total="9,99")
        self.assertEqual(self._validacao().valor_pdv_informado, Decimal("1500.00"))

    def test_guardas_existentes_vem_antes_do_valor(self):
        resposta = self.cli.post("/cliente/upload", data={
            "id_analise": str(self.outra_analise_id), "tipo_relatorio": "VENDAS",
            "arquivo": (io.BytesIO(VENDAS), "vendas.csv"), "csrf_token": self._csrf(self.cli, "/cliente/upload"),
        }, content_type="multipart/form-data")
        self.assertEqual(resposta.status_code, 404)

    def test_informante_fica_registrado(self):
        self._enviar_cliente()
        self.assertEqual(self._validacao().id_usuario_informante, self.cliente_id)
        self._enviar_admin(total="1.600,00")
        self.assertEqual(self._validacao().id_usuario_informante, self.admin_id)
        self.assertEqual(self._validacao().valor_pdv_informado, Decimal("1600.00"))

    def test_mensagem_de_erro_nao_repete_o_que_foi_digitado(self):
        self._enviar_cliente(total="<script>alert(1)</script>")
        self.assertFalse(any("script" in m for m in self._flashes(self.cli)))


class CardNoPainelTests(TravaPdvCase):
    def _painel_cliente(self):
        resposta = self.cli.get("/cliente/dashboard")
        self.assertEqual(resposta.status_code, 200)
        return self._texto(resposta)

    def test_consultor_ve_os_tres_valores_e_a_saida(self):
        self._enviar_e_processar("1.515,00")
        for pagina in ("/admin/dashboard", self.pagina_admin, self.editor):
            with self.subTest(pagina=pagina):
                corpo = self._texto(self.adm.get(pagina))
                self.assertIn('aviso-contrato is-perigo', corpo)
                for trecho in ("Valor informado no PDV", "1.515,00", "Valor total calculado pelo ETL", "1.500,00",
                               "Divergência apurada", "R$ 15,00"):
                    self.assertIn(trecho, corpo)

    def test_editor_desliga_publicar_e_ia_mas_nao_o_rascunho(self):
        self._enviar_e_processar("1.515,00")
        corpo = self.adm.get(self.editor).get_data(as_text=True)
        self.assertRegex(corpo, r'value="publicar"[^>]*disabled')
        self.assertRegex(corpo, r'value="gerar_ia"[^>]*disabled')
        self.assertNotRegex(corpo, r'value="rascunho"[^>]*disabled')

    def test_painel_do_consultor_deixa_de_mostrar_depois_de_resolver(self):
        self._enviar_e_processar("1.515,00")
        self.assertIn("Divergência no total do PDV", self._texto(self.adm.get("/admin/dashboard")))
        self._enviar_admin()
        self._processar()
        self.assertNotIn("Divergência no total do PDV", self._texto(self.adm.get("/admin/dashboard")))

    def test_empresa_na_lixeira_nao_aparece_no_painel_do_consultor(self):
        from horario import agora_utc
        from models import Empresa
        self._enviar_e_processar("1.515,00")
        self.db.session.get(Empresa, self.empresa_id).deletado_em = agora_utc()
        self.db.session.commit()
        self.assertEqual(validacao_pdv.divergentes(), [])

    def test_cliente_em_periodo_liberado_ve_os_tres_valores_como_preliminares(self):
        self._enviar_e_processar("1.515,00")
        corpo = self._painel_cliente()
        self.assertIn('aviso-contrato is-alerta', corpo)
        for trecho in ("1.515,00", "Valor total calculado pelo ETL", "1.500,00", "Divergência apurada", "R$ 15,00", "preliminares"):
            self.assertIn(trecho, corpo)

    def test_cliente_em_periodo_nao_pago_nao_recebe_calculado_nem_divergencia(self):
        self.contratar()
        self.ativar_regras()
        self._enviar_e_processar("1.515,00")
        self.assertEqual(self._validacao().status_validacao, "DIVERGENTE_PDV")
        corpo = self._painel_cliente()
        self.assertIn("Diferença no total de vendas de 01/2026", corpo)
        self.assertIn("1.515,00", corpo)  # o que ele mesmo informou
        for trecho in ("1.500,00", "Valor total calculado", "Divergência apurada", "R$ 15,00", "preliminares"):
            self.assertNotIn(trecho, corpo)

    def test_periodo_nao_pago_nao_mostra_total_digitado_pelo_consultor(self):
        self.contratar()
        self.ativar_regras()
        self._enviar_e_processar("1.515,00")
        # O consultor viu o calculado no card e digitou outro valor: nada disso chega ao cliente.
        self.assertEqual(self._enviar_admin(total="1.777,77").status_code, 302)
        self._processar()
        self.assertEqual(self._validacao().status_validacao, "DIVERGENTE_PDV")
        corpo = self._painel_cliente()
        self.assertIn("Diferença no total de vendas", corpo)
        self.assertNotIn("1.777,77", corpo)
        self.assertNotIn("Valor informado no PDV", corpo)

    def test_periodo_nao_pago_nao_mostra_o_calculado_redigitado_na_tela_de_envio(self):
        from models import Analise
        self.contratar()
        self.ativar_regras()
        self._enviar_e_processar("1.515,00")
        self.assertEqual(self._enviar_admin(total="1.500,00").status_code, 302)
        # Uma falha de processamento devolve a análise à tela de envio do cliente.
        analise = self.db.session.get(Analise, self.analise_id)
        analise.status_analise = "RELATORIO_RECEBIDO"
        self.db.session.commit()
        corpo = self._texto(self.cli.get("/cliente/upload"))
        self.assertIn('name="valor_pdv_informado"', corpo)
        self.assertNotIn("1.500,00", corpo.replace('placeholder="Ex: 150.000,00"', ""))
        self.assertNotIn("Último informado", corpo)

    def test_tela_de_envio_mostra_ao_cliente_o_total_que_ele_digitou(self):
        self.contratar()
        self.ativar_regras()
        self._enviar_cliente(total="1.515,00")
        self.assertIn("Último informado: R$ 1.515,00", self._texto(self.cli.get("/cliente/upload")))

    def test_periodo_liberado_mostra_o_total_mesmo_digitado_pelo_consultor(self):
        self._enviar_e_processar("1.515,00")
        self._enviar_admin(total="1.777,77")
        self._processar()
        self.assertIn("1.777,77", self._painel_cliente())

    def test_cliente_em_periodo_pago_do_regime_contratual_ve_os_valores(self):
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.ativar_regras()
        self._enviar_e_processar("1.515,00")
        self.assertIn("Divergência apurada", self._painel_cliente())

    def test_outra_empresa_nao_ve_o_card(self):
        from models import Usuario
        self._enviar_e_processar("1.515,00")
        outro = Usuario(nome="Outro fictício", email="outro@example.invalid", role="CLIENTE",
                        id_empresa=self.outra_empresa_id, primeiro_acesso=False)
        outro.set_senha(self.password)
        self.db.session.add(outro)
        self.db.session.commit()
        corpo = self._texto(self._entrar("outro@example.invalid").get("/cliente/dashboard"))
        self.assertNotIn("Diferença no total de vendas", corpo)
        self.assertNotIn("1.515,00", corpo)

    def test_sem_divergencia_nao_ha_card(self):
        self._enviar_e_processar("1.500,00")
        self.assertNotIn("Diferença no total de vendas", self._painel_cliente())
        self.assertNotIn("Divergência no total do PDV", self._texto(self.adm.get("/admin/dashboard")))

    def test_exportacao_de_dados_nao_traz_a_conferencia(self):
        self._enviar_e_processar("1.500,00")
        self._publicar()
        corpo = self.cli.get("/cliente/configuracoes/exportar-dados").get_data(as_text=True)
        self.assertNotIn("valor_pdv_informado", corpo)


class SemATabelaTests(TravaPdvCase):
    """Código no ar antes da migração e2f7a94c1d35: tudo funciona, menos publicar."""

    def test_envio_processamento_e_paineis_seguem_sem_campo_e_sem_card(self):
        self._sem_tabela()
        self.assertFalse(validacao_pdv.esquema_validacao_pdv_disponivel())
        for cliente, pagina in ((self.cli, "/cliente/upload"), (self.adm, self.pagina_admin)):
            corpo = self._texto(cliente.get(pagina))
            self.assertNotIn('name="valor_pdv_informado"', corpo)
            self.assertNotIn("informe o total de vendas", corpo)
        self.assertEqual(self._enviar_cliente(total=None).status_code, 302)
        self.assertEqual(self._enviar_cliente(tipo="COMPRAS", conteudo=COMPRAS, total=None).status_code, 302)
        resposta = self._processar()
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("não instalada neste banco", self._texto(resposta))
        self.assertEqual(self._analise().status_analise, "EM_ANALISE")
        self.assertIsNotNone(self._analise().indicador)
        for cliente, pagina in ((self.cli, "/cliente/dashboard"), (self.adm, "/admin/dashboard"),
                                (self.adm, self.pagina_admin), (self.adm, self.editor)):
            self.assertEqual(cliente.get(pagina).status_code, 200, pagina)
        # Com o relatório de Vendas já recebido, a correção do total também não aparece.
        self.assertNotIn("Corrigir só o total", self._texto(self.adm.get(self.pagina_admin)))

    def test_corrigir_texto_de_devolutiva_ja_publicada_segue_sem_a_tabela(self):
        self._enviar_e_processar("1.500,00")
        self._publicar()
        self.assertTrue(self._publicada())
        self.db.session.delete(self._validacao())
        self.db.session.commit()
        self._sem_tabela()
        self._flashes(self.adm)
        self.assertEqual(self._publicar().status_code, 302)
        avisos = self._flashes(self.adm)
        self.assertTrue(any("publicada para o cliente" in m for m in avisos), avisos)
        self.assertFalse(any("Publicação suspensa" in m for m in avisos), avisos)

    def test_publicacao_falha_fechada_mas_rascunho_segue(self):
        self._sem_tabela()
        self._enviar_cliente(total=None)
        self._enviar_cliente(tipo="COMPRAS", conteudo=COMPRAS, total=None)
        self._processar()
        self._publicar()
        self.assertTrue(any("Aplique a migração e2f7a94c1d35" in m for m in self._flashes(self.adm)))
        self._assert_nao_publicada()
        self.assertEqual(self._publicar("rascunho").status_code, 302)
        self.assertEqual(self._analise().relatorio.titulo, "Título fictício")

    def test_excluir_analise_nao_depende_da_tabela(self):
        self._sem_tabela()
        self.db.session.delete(self._analise())
        self.db.session.commit()

    def test_migracao_aplicada_vale_depois_de_limpar_o_cache_de_ausencia(self):
        from flask_migrate import upgrade
        self._sem_tabela()
        self.assertFalse(validacao_pdv.esquema_validacao_pdv_disponivel())
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            upgrade(directory=str(ROOT / "migrations"), revision="head")
        self.assertFalse(validacao_pdv.esquema_validacao_pdv_disponivel())  # ausência vale por até 30 s
        validacao_pdv._esquema_por_engine.clear()
        self.assertTrue(validacao_pdv.esquema_validacao_pdv_disponivel())

    def test_downgrade_recusa_apagar_conferencias(self):
        from flask_migrate import downgrade
        self._enviar_cliente()
        self.db.session.commit()
        self.db.session.remove()
        # O Flask-Migrate transforma o RuntimeError da migração em saída com erro.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            with self.assertRaises(SystemExit):
                downgrade(directory=str(ROOT / "migrations"), revision="c7d41e9a2b60")
        self.assertEqual(self._validacao().status_validacao, "PENDENTE")


if __name__ == "__main__":
    unittest.main()
