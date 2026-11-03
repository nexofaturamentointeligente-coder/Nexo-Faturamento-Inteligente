"""Gestão contratual pelo HTTP real: portal do CLIENTE, login e administração.

Complementa tests.test_contratos_gestao (operações no banco) exercitando as
rotas reais, com CSRF, cookies de sessão, Socket.IO de teste e migrações reais
em SQLite temporário (tests.contratos_support.ContratosCase). Cobre:
  (a) regime legado ATIVA sem regressão (dashboard, histórico, análise, PDF);
  (b) regime contratual: só análises de períodos integralmente pagos chegam ao
      HTML e ao JSON embutido (dashboardData/tojson); as demais respondem 403
      sem conteúdo privado e sem bytes de PDF;
  (c) suspensão: consulta paga e PDF por 14 dias corridos (limite exclusivo);
  (d) suspensão expirada antes da reconciliação: sessão, novo login, API do
      NexoBot e recuperação de senha;
  (e) login: conta encerrada x conta desativada x credencial inválida;
  (f) isolamento entre empresas e análises não publicadas;
  (g) envio de arquivos durante a suspensão;
  (h) administração contratual pelo HTTP (sucesso, validação, 403, 404,
      versão desatualizada, visão geral e reconciliação);
  (i) nova contratação após cancelamento;
  (j) bloqueios administrativos (nova análise, edição da empresa, publicação);
  (k) handshake Socket.IO após a expiração da suspensão.

Somente dados fictícios (example.invalid, CNPJs sintéticos). O relógio da
política é injetado por NEXO_RELOGIO: nenhum teste espera tempo real nem
acessa a rede. Prazos seguem a proposta pendente de homologação comercial:
3 meses de calendário de suspensão, 14 dias corridos de PDF, limites
exclusivos, fuso America/Sao_Paulo.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import html
import io
import json
import re
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

# Importado antes da primeira create_app (como em test_session_revocation_security):
# os handlers de realtime entram na lista do SocketIO compartilhado e são
# registrados em cada app de teste, não só no servidor da primeira.
import realtime  # noqa: F401
from extensions import socketio
from tests.contratos_support import T0, TEXTO_PRIVADO, ContratosCase

CLIENTE = "cliente-test@example.invalid"
ADMIN = "admin-test@example.invalid"
INATIVO = "cliente-inativo-test@example.invalid"

# Suspensão registrada em T0 (02/03/2026 10:00 em São Paulo).
SUSPENSAO_LIMITE = datetime(2026, 6, 2, 13, tzinfo=timezone.utc)   # + 3 meses de calendário
PDF_LIMITE = datetime(2026, 3, 16, 13, tzinfo=timezone.utc)        # + 14 dias corridos
UM_MICRO = timedelta(microseconds=1)

MENSAGEM_PDF_ENCERRADO = "Seu prazo para exportação de relatórios foi encerrado"
MENSAGEM_GENERICA_LOGIN = "E-mail ou senha incorretos."
MENSAGEM_DESATIVADA = "Esta conta está desativada"
MENSAGEM_RECUPERACAO = "Se o e-mail estiver cadastrado, enviaremos um link de redefinição"

# Perfis de análise. A SIGILOSA nunca pode aparecer quando o período não é
# pago; as LIBERADAS servem de controle positivo (o que é pago aparece).
SIGILOSA = dict(texto=TEXTO_PRIVADO, produto="Produto sigiloso de fevereiro",
                faturamento=Decimal("18500"), comprado=Decimal("14200"), saldo=Decimal("-4300"))
LIBERADA = dict(texto="Devolutiva ficticia liberada de janeiro", produto="Produto liberado de janeiro",
                faturamento=Decimal("27350"), comprado=Decimal("19900"), saldo=Decimal("-7450"))
LIBERADA_DEZ = dict(texto="Devolutiva ficticia liberada de dezembro", produto="Produto liberado de dezembro",
                    faturamento=Decimal("25100"), comprado=Decimal("21000"), saldo=Decimal("-4100"))

# Como os números aparecem: m.brl no HTML ("18.500,00") e |float/tojson no JS (18500.0).
MARCAS_SIGILOSAS = (SIGILOSA["texto"], "exclusiva da empresa um", SIGILOSA["produto"],
                    "18.500", "18500", "14.200", "14200", "4.300")
MARCAS_LIBERADAS = (LIBERADA["texto"], LIBERADA["produto"], "27.350", "27350")

CSV_VENDAS = b"Produto;Quantidade;Valor total\nProduto sintetico;2;20,00\n"

_TOKENS_CSRF = re.compile(r'(name="csrf_token"[^>]*value="|name="csrf-token" content=")[^"]*"')


def _sem_tokens(corpo: str) -> str:
    """Remove os tokens CSRF (aleatórios) antes de procurar números no HTML."""
    return _TOKENS_CSRF.sub(r'\1"', corpo)


class PortalContratualCase(ContratosCase):
    """Helpers HTTP comuns; nenhum e-mail sai do processo."""

    def setUp(self):
        super().setUp()
        self.mail_recuperacao = self.stack.enter_context(
            patch("blueprints.auth.email_recuperacao_senha", return_value=True))
        self.stack.enter_context(patch("blueprints.auth.email_boas_vindas", return_value=True))
        # Gatilhos substituídos: o transporte também (a recuperação não para no
        # aviso de envio de e-mail indisponível).
        self.stack.enter_context(patch("blueprints.auth.envio_disponivel", return_value=True))
        self.mail_publicacao = self.stack.enter_context(
            patch("blueprints.admin.email_analise_publicada", return_value=True))

    # ------------------------------------------------------------ clientes HTTP
    def _csrf(self, client, caminho):
        resposta = client.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        achado = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', resposta.get_data(as_text=True))
        self.assertIsNotNone(achado, f"Token CSRF ausente em {caminho}")
        return achado.group(1)

    def _meta_csrf(self, client, caminho="/cliente/dashboard"):
        resposta = client.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        return re.search(r'<meta name="csrf-token" content="([^"]+)"',
                         resposta.get_data(as_text=True)).group(1)

    def _tentar_login(self, email=CLIENTE, senha=None):
        client = self.app.test_client()
        resposta = client.post("/auth/login", data={
            "email": email, "senha": self.password if senha is None else senha,
            "csrf_token": self._csrf(client, "/auth/login"),
        })
        return client, resposta

    def _entrar(self, email=CLIENTE):
        client, resposta = self._tentar_login(email)
        self.assertEqual(resposta.status_code, 302, resposta.get_data(as_text=True)[:400])
        return client

    def _cookie(self, client):
        return client.get_cookie(self.app.config["SESSION_COOKIE_NAME"]).value

    def _replay(self, cookie):
        client = self.app.test_client()
        client.set_cookie(self.app.config["SESSION_COOKIE_NAME"], cookie)
        return client

    def _flashes(self, client):
        with client.session_transaction() as sessao:
            return [str(mensagem) for _categoria, mensagem in sessao.get("_flashes", ())]

    def _sessao(self, client):
        with client.session_transaction() as sessao:
            return dict(sessao)

    @staticmethod
    def _texto(resposta):
        return html.unescape(resposta.get_data(as_text=True))

    def _socket(self, client):
        # Sem o handler de connect o servidor aceitaria qualquer handshake.
        self.assertIn("connect", socketio.server.handlers.get("/", {}))
        # Contexto de aplicação próprio: o handshake não herda o g do teste,
        # onde agora() fixaria o instante da primeira conexão.
        with self.app.app_context():
            conexao = socketio.test_client(self.app, flask_test_client=client)

        def fechar():
            if conexao.is_connected():
                with self.app.app_context():
                    conexao.disconnect()
        self.addCleanup(fechar)
        return conexao

    # ---------------------------------------------------------------- dados
    def _publicar(self, id_analise, inicio, fim, perfil, concluida_em, base=None):
        from models import ProdutoCurvaABC
        analise = self.publicar(id_analise, inicio, fim, base=base, texto=perfil["texto"])
        indicador = analise.indicador
        indicador.faturamento_total = perfil["faturamento"]
        indicador.total_comprado = perfil["comprado"]
        indicador.saldo_estimado_compras_vendas = perfil["saldo"]
        indicador.produto_mais_vendido_nome = perfil["produto"]
        indicador.produto_mais_vendido_quantidade = Decimal("7")
        indicador.produto_maior_faturamento_nome = perfil["produto"]
        indicador.produto_maior_faturamento_valor = perfil["faturamento"]
        analise.curva_abc = [ProdutoCurvaABC(
            posicao_ranking=1, produto_nome=perfil["produto"], faturamento=perfil["faturamento"],
            quantidade=Decimal("7"), percentual_individual=Decimal("100"),
            percentual_acumulado=Decimal("100"), classe_abc="A",
        )]
        analise.data_conclusao = concluida_em
        self.db.session.commit()
        return analise.id_analise

    def _cenario(self, *, pagar_janeiro=True, regras=True, dezembro=False):
        """Janeiro (liberada) e fevereiro (sigilosa, mais recente) publicadas.

        Contrato vigente em 2026 (desde dez/2025 se `dezembro`); só janeiro (e
        dezembro) pagos. Fevereiro é a última devolutiva: se vazar, vai para o
        topo do dashboard.
        """
        if dezembro:
            self.contratar(data_inicio=date(2025, 12, 1))
            self.dez = self._publicar(self.nova_analise(date(2025, 12, 1), date(2025, 12, 31)).id_analise,
                                      date(2025, 12, 1), date(2025, 12, 31), LIBERADA_DEZ,
                                      datetime(2026, 1, 5, 12))
            self.pagar(date(2025, 12, 1), date(2025, 12, 31), referencia="Comprovante fictício de dezembro")
        else:
            self.contratar()
        self.jan = self._publicar(self.analise_id, date(2026, 1, 1), date(2026, 1, 31), LIBERADA,
                                  datetime(2026, 2, 5, 12))
        self.fev = self._publicar(self.nova_analise(date(2026, 2, 1), date(2026, 2, 28)).id_analise,
                                  date(2026, 2, 1), date(2026, 2, 28), SIGILOSA, datetime(2026, 3, 1, 12))
        if pagar_janeiro:
            self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        if regras:
            self.ativar_regras()

    def _usuario_inativo(self):
        from models import Usuario
        usuario = Usuario(nome="Cliente inativo fictício", email=INATIVO, role="CLIENTE",
                          id_empresa=self.empresa_id, primeiro_acesso=False, ativo=False)
        usuario.set_senha(self.password)
        self.db.session.add(usuario)
        self.db.session.commit()
        return usuario.id_usuario

    # --------------------------------------------------------------- leitura
    def _empresa(self, empresa_id=None):
        from models import Empresa
        self.db.session.expire_all()
        return self.db.session.get(Empresa, empresa_id or self.empresa_id)

    def _contrato(self, id_contrato):
        from models import Contrato
        self.db.session.expire_all()
        return self.db.session.get(Contrato, id_contrato)

    def _vigente(self, empresa_id=None):
        self.db.session.expire_all()
        return self.vigente(empresa_id)

    def _geracao(self, id_usuario=None):
        from models import Usuario
        self.db.session.expire_all()
        return self.db.session.get(Usuario, id_usuario or self.cliente_id).geracao_autorizacao

    def _eventos(self, empresa_id=None):
        from models import EventoContratual
        self.db.session.expire_all()
        return self.db.session.scalars(select(EventoContratual).where(
            EventoContratual.id_empresa == (empresa_id or self.empresa_id),
        ).order_by(EventoContratual.id_evento)).all()

    def _notificacoes(self, id_usuario=None):
        from models import Notificacao
        self.db.session.expire_all()
        return self.db.session.scalar(select(func.count(Notificacao.id_notificacao)).where(
            Notificacao.id_usuario == (id_usuario or self.cliente_id)))

    def _uploads(self, id_analise):
        from models import UploadRelatorio
        self.db.session.expire_all()
        return self.db.session.scalars(select(UploadRelatorio).where(
            UploadRelatorio.id_analise == id_analise)).all()

    @staticmethod
    def _dashboard_data(corpo):
        achado = re.search(r"const dashboardData = (.+?);\n", corpo)
        return None if achado is None else json.loads(achado.group(1))

    # ------------------------------------------------------------- asserções
    def _assert_sem_marcas(self, corpo, marcas=MARCAS_SIGILOSAS):
        limpo = _sem_tokens(corpo)
        for marca in marcas:
            self.assertNotIn(marca, limpo, f"Dado privado exposto: {marca!r}")

    def _assert_com_marcas(self, corpo, marcas):
        limpo = _sem_tokens(corpo)
        for marca in marcas:
            self.assertIn(marca, limpo)

    def _assert_pdf(self, resposta):
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.mimetype, "application/pdf")
        self.assertTrue(resposta.data.startswith(b"%PDF-"))

    def _assert_negado(self, resposta, titulo="Análise indisponível", marcas=MARCAS_SIGILOSAS):
        """403 renderizado sem nada da análise e sem bytes de PDF."""
        self.assertEqual(resposta.status_code, 403)
        self.assertNotEqual(resposta.mimetype, "application/pdf")
        self.assertFalse(resposta.data.startswith(b"%PDF"))
        self.assertNotIn("attachment", resposta.headers.get("Content-Disposition", ""))
        corpo = self._texto(resposta)
        self.assertIn(titulo, corpo)
        self._assert_sem_marcas(corpo, marcas)

    def _assert_404(self, resposta, marcas=MARCAS_SIGILOSAS):
        self.assertEqual(resposta.status_code, 404)
        self.assertNotEqual(resposta.mimetype, "application/pdf")
        self._assert_sem_marcas(self._texto(resposta), marcas)

    def _assert_para_login(self, resposta):
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/auth/login", resposta.headers["Location"])
        self._assert_sem_marcas(self._texto(resposta), MARCAS_SIGILOSAS + MARCAS_LIBERADAS)

    def _assert_conta_encerrada(self, client, resposta):
        self.assertEqual(resposta.status_code, 200)
        self.assertNotIn("Location", resposta.headers)
        self.assertEqual(resposta.headers.get("Cache-Control"), "no-store")
        corpo = self._texto(resposta)
        self.assertIn("Conta encerrada", corpo)
        self.assertIn("img/nexo-cubo-brilho.png", corpo)
        self.assertNotIn(MENSAGEM_DESATIVADA, corpo)
        self.assertNotIn(MENSAGEM_GENERICA_LOGIN, corpo)
        self._assert_sem_marcas(corpo, MARCAS_SIGILOSAS + MARCAS_LIBERADAS
                                + ("Teste 1", "Empresa fictícia 1", "00000000000001"))
        sessao = self._sessao(client)
        self.assertNotIn("_user_id", sessao)
        self.assertNotIn("credential_fingerprint", sessao)
        self.assertNotIn("_flashes", sessao)
        self._assert_para_login(client.get("/cliente/dashboard"))

    def _assert_login_negado(self, client, resposta, mensagem):
        self.assertEqual(resposta.status_code, 200)
        corpo = self._texto(resposta)
        self.assertIn(mensagem, corpo)
        self.assertNotIn("Conta encerrada", corpo)
        self.assertNotIn("_user_id", self._sessao(client))


# =============================================================================
# (a) Regime legado
# =============================================================================
class RegimeLegadoPortalTests(PortalContratualCase):
    """Sem linha em regime_contratual nada muda: tudo publicado é consultável."""

    def setUp(self):
        super().setUp()
        self.jan = self._publicar(self.analise_id, date(2026, 1, 1), date(2026, 1, 31), LIBERADA,
                                  datetime(2026, 2, 5, 12))
        self.fev = self._publicar(self.nova_analise(date(2026, 2, 1), date(2026, 2, 28)).id_analise,
                                  date(2026, 2, 1), date(2026, 2, 28), SIGILOSA, datetime(2026, 3, 1, 12))

    def _assert_portal_completo(self, client):
        dashboard = client.get("/cliente/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        corpo = self._texto(dashboard)
        # Controle positivo: as marcas usadas nos testes negativos são de fato renderizadas.
        self._assert_com_marcas(corpo, (SIGILOSA["texto"], SIGILOSA["produto"], "18.500,00", "14.200,00"))
        dados = self._dashboard_data(corpo)
        self.assertEqual(dados["comparativo"]["values"], [14200.0, 18500.0])
        self.assertEqual(dados["curva_abc"]["labels"], [SIGILOSA["produto"]])
        self.assertEqual(dados["historico"]["labels"], ["01/2026", "02/2026"])
        self.assertEqual(dados["historico"]["vendas"], [27350.0, 18500.0])

        historico = client.get("/cliente/historico")
        self.assertEqual(historico.status_code, 200)
        for id_analise in (self.jan, self.fev):
            self.assertIn(f'/cliente/analise/{id_analise}"', historico.get_data(as_text=True))

        analise = client.get(f"/cliente/analise/{self.fev}")
        self.assertEqual(analise.status_code, 200)
        corpo = self._texto(analise)
        self._assert_com_marcas(corpo, (SIGILOSA["texto"], SIGILOSA["produto"], "18.500,00", "18500.0"))
        self.assertIn(f'/cliente/analise/{self.fev}/pdf"', corpo)
        self.assertNotIn(MENSAGEM_PDF_ENCERRADO, corpo)
        self._assert_pdf(client.get(f"/cliente/analise/{self.fev}/pdf"))
        self._assert_pdf(client.get(f"/cliente/analise/{self.jan}/pdf"))

    def test_legado_ativa_dashboard_historico_analise_e_pdf(self):
        self.assertEqual(self.situacao().regime, "LEGADO")
        self._assert_portal_completo(self._entrar())

    def test_contrato_registrado_sem_regras_ativas_nao_restringe(self):
        self.contratar()  # nenhuma cobertura paga e regras inativas
        situacao = self.situacao()
        self.assertEqual((situacao.regime, situacao.estado), ("LEGADO", "ATIVA"))
        self._assert_portal_completo(self._entrar())

    def test_legado_suspensa_mantem_consulta_e_pdf(self):
        from models import Empresa
        empresa = self.db.session.get(Empresa, self.empresa_id)
        empresa.status_conta = "SUSPENSA"
        self.db.session.commit()
        client = self._entrar()
        dashboard = self._texto(client.get("/cliente/dashboard"))
        self.assertIn("Conta suspensa: você ainda pode acessar seu histórico", dashboard)
        self.assertEqual(client.get(f"/cliente/analise/{self.fev}").status_code, 200)
        self._assert_pdf(client.get(f"/cliente/analise/{self.fev}/pdf"))


# =============================================================================
# (b) Regime contratual ATIVA: cobertura paga
# =============================================================================
class CoberturaPortalTests(PortalContratualCase):
    """Só o período integralmente pago chega ao HTML, ao JSON embutido e ao PDF."""

    def test_periodo_pago_abre_analise_e_pdf(self):
        self._cenario()
        client = self._entrar()
        resposta = client.get(f"/cliente/analise/{self.jan}")
        self.assertEqual(resposta.status_code, 200)
        corpo = self._texto(resposta)
        self._assert_com_marcas(corpo, MARCAS_LIBERADAS)
        self._assert_sem_marcas(corpo)
        self.assertIn(f'/cliente/analise/{self.jan}/pdf"', corpo)
        self._assert_pdf(client.get(f"/cliente/analise/{self.jan}/pdf"))

    def test_periodo_nao_pago_responde_403_sem_conteudo_nem_pdf(self):
        self._cenario()
        client = self._entrar()
        self._assert_negado(client.get(f"/cliente/analise/{self.fev}"))
        self._assert_negado(client.get(f"/cliente/analise/{self.fev}/pdf"))
        # A recusa não derruba a sessão: o restante do portal segue disponível.
        self.assertEqual(client.get("/cliente/dashboard").status_code, 200)

    def test_dashboard_e_historico_omitem_periodo_nao_pago(self):
        self._cenario(dezembro=True)
        client = self._entrar()
        dashboard = client.get("/cliente/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        corpo = self._texto(dashboard)
        self._assert_sem_marcas(corpo)
        self.assertNotIn(f'/cliente/analise/{self.fev}"', corpo)
        self.assertIn(f'/cliente/analise/{self.jan}"', corpo)
        dados = self._dashboard_data(corpo)
        self.assertIsNotNone(dados, "dashboardData ausente com análises pagas")
        self.assertEqual(dados["comparativo"]["values"], [19900.0, 27350.0])
        self.assertEqual(dados["pressao_estoque"], -7450.0)
        self.assertEqual(dados["curva_abc"]["labels"], [LIBERADA["produto"]])
        # Série temporal agregada só com os períodos pagos (dez/2025 e jan/2026).
        self.assertEqual(dados["historico"]["labels"], ["12/2025", "01/2026"])
        self.assertEqual(dados["historico"]["vendas"], [25100.0, 27350.0])
        self.assertEqual(dados["historico"]["compras"], [21000.0, 19900.0])
        self.assertNotIn("18500", json.dumps(dados))

        historico = client.get("/cliente/historico")
        self.assertEqual(historico.status_code, 200)
        corpo = self._texto(historico)
        self._assert_sem_marcas(corpo)
        self.assertNotIn(f'/cliente/analise/{self.fev}"', corpo)
        for id_analise in (self.dez, self.jan):
            self.assertIn(f'/cliente/analise/{id_analise}"', corpo)

    def test_sem_cobertura_nada_e_listado_nem_aberto(self):
        self._cenario(pagar_janeiro=False)
        client = self._entrar()
        dashboard = client.get("/cliente/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        corpo = self._texto(dashboard)
        self._assert_sem_marcas(corpo, MARCAS_SIGILOSAS + MARCAS_LIBERADAS)
        self.assertIsNone(self._dashboard_data(corpo))
        self.assertIn("Sua primeira análise está em preparação.", corpo)
        historico = self._texto(client.get("/cliente/historico"))
        self._assert_sem_marcas(historico, MARCAS_SIGILOSAS + MARCAS_LIBERADAS)
        for id_analise in (self.jan, self.fev):
            self.assertNotIn(f'/cliente/analise/{id_analise}"', historico)
            self._assert_negado(client.get(f"/cliente/analise/{id_analise}"),
                                marcas=MARCAS_SIGILOSAS + MARCAS_LIBERADAS)
            self._assert_negado(client.get(f"/cliente/analise/{id_analise}/pdf"),
                                marcas=MARCAS_SIGILOSAS + MARCAS_LIBERADAS)

    def test_cobertura_parcial_nega_e_complemento_consecutivo_libera(self):
        self._cenario()
        self.pagar(date(2026, 2, 1), date(2026, 2, 14), referencia="Comprovante fictício parcial")
        client = self._entrar()
        self._assert_negado(client.get(f"/cliente/analise/{self.fev}"))
        self._assert_negado(client.get(f"/cliente/analise/{self.fev}/pdf"))
        self._assert_sem_marcas(self._texto(client.get("/cliente/dashboard")))
        self.pagar(date(2026, 2, 15), date(2026, 2, 28), referencia="Comprovante fictício complementar")
        resposta = client.get(f"/cliente/analise/{self.fev}")
        self.assertEqual(resposta.status_code, 200)
        self.assertIn(SIGILOSA["texto"], self._texto(resposta))
        self._assert_pdf(client.get(f"/cliente/analise/{self.fev}/pdf"))
        dados = self._dashboard_data(self._texto(client.get("/cliente/dashboard")))
        self.assertEqual(dados["comparativo"]["values"], [14200.0, 18500.0])

    def test_periodo_real_da_base_fora_do_pago_nega(self):
        # Março declarado, mas a base lida do PDV começa em 25/02 (não pago).
        self.contratar()
        self.jan = self._publicar(self.analise_id, date(2026, 1, 1), date(2026, 1, 31), LIBERADA,
                                  datetime(2026, 2, 5, 12))
        marco = self._publicar(self.nova_analise(date(2026, 3, 1), date(2026, 3, 31)).id_analise,
                               date(2026, 3, 1), date(2026, 3, 31), SIGILOSA, datetime(2026, 4, 2, 12),
                               base=(date(2026, 2, 25), date(2026, 3, 31)))
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.pagar(date(2026, 3, 1), date(2026, 3, 31), referencia="Comprovante fictício de março")
        self.ativar_regras()
        client = self._entrar()
        self._assert_negado(client.get(f"/cliente/analise/{marco}"))
        self._assert_negado(client.get(f"/cliente/analise/{marco}/pdf"))
        dashboard = self._texto(client.get("/cliente/dashboard"))
        self._assert_sem_marcas(dashboard)
        self.assertNotIn(f'/cliente/analise/{marco}"', self._texto(client.get("/cliente/historico")))
        self.pagar(date(2026, 2, 1), date(2026, 2, 28), referencia="Comprovante fictício de fevereiro")
        self.assertEqual(client.get(f"/cliente/analise/{marco}").status_code, 200)

    def test_revogacao_de_cobertura_retira_acesso_na_requisicao_seguinte(self):
        import contract_admin
        from contract_policy import obter_coberturas_pagas
        self._cenario()
        client = self._entrar()
        self.assertEqual(client.get(f"/cliente/analise/{self.jan}").status_code, 200)
        [cobertura] = [c.id_cobertura for c in obter_coberturas_pagas(self.empresa_id, instante=self.agora)]
        contract_admin.revogar_cobertura(self.db.session, self.empresa_id, cobertura,
                                         motivo="Estorno fictício", responsavel=self.admin,
                                         instante=self.agora)
        self.db.session.commit()
        self._assert_negado(client.get(f"/cliente/analise/{self.jan}"), marcas=MARCAS_LIBERADAS)
        self._assert_negado(client.get(f"/cliente/analise/{self.jan}/pdf"), marcas=MARCAS_LIBERADAS)
        self._assert_sem_marcas(self._texto(client.get("/cliente/dashboard")),
                                MARCAS_SIGILOSAS + MARCAS_LIBERADAS)


# =============================================================================
# (c) Suspensão dentro do prazo
# =============================================================================
class SuspensaoPortalTests(PortalContratualCase):
    """Consulta paga até o limite da suspensão; PDF por 14 dias corridos."""

    def setUp(self):
        super().setUp()
        self._cenario()
        self.suspender()
        self.client_cli = self._entrar()

    def test_dentro_do_prazo_de_pdf_exporta(self):
        for instante in (T0, T0 + timedelta(days=13), PDF_LIMITE - UM_MICRO):
            with self.subTest(instante=instante):
                self.relogio(instante)
                resposta = self.client_cli.get(f"/cliente/analise/{self.jan}")
                self.assertEqual(resposta.status_code, 200)
                corpo = self._texto(resposta)
                self.assertIn("Exportar PDF", corpo)
                self.assertIn(f'/cliente/analise/{self.jan}/pdf"', corpo)
                self.assertNotIn(MENSAGEM_PDF_ENCERRADO, corpo)
                self._assert_pdf(self.client_cli.get(f"/cliente/analise/{self.jan}/pdf"))
                # Suspensão não dá direito ao período não pago.
                self._assert_negado(self.client_cli.get(f"/cliente/analise/{self.fev}"))
                self._assert_negado(self.client_cli.get(f"/cliente/analise/{self.fev}/pdf"))

    def test_apos_prazo_de_pdf_consulta_sem_exportacao(self):
        for instante in (PDF_LIMITE, PDF_LIMITE + timedelta(days=20), SUSPENSAO_LIMITE - UM_MICRO):
            with self.subTest(instante=instante):
                self.relogio(instante)
                resposta = self.client_cli.get(f"/cliente/analise/{self.jan}")
                self.assertEqual(resposta.status_code, 200)
                corpo = self._texto(resposta)
                self.assertIn(MENSAGEM_PDF_ENCERRADO, corpo)
                self.assertIn(LIBERADA["texto"], corpo)
                self.assertNotIn("Exportar PDF", corpo)
                self.assertNotIn(f'/cliente/analise/{self.jan}/pdf"', corpo)
                pdf = self.client_cli.get(f"/cliente/analise/{self.jan}/pdf")
                self._assert_negado(pdf, titulo="Exportação encerrada")
                self.assertIn(MENSAGEM_PDF_ENCERRADO, self._texto(pdf))

    def test_banner_do_dashboard_mostra_as_datas_limite(self):
        self.relogio(T0 + timedelta(days=1))
        corpo = self._texto(self.client_cli.get("/cliente/dashboard"))
        self.assertIn("Conta suspensa: acesso histórico temporário", corpo)
        self.assertIn("Consulta disponível até 02/06/2026 às 10:00", corpo)
        self.assertIn("Exportação de PDFs até 16/03/2026 às 10:00", corpo)
        self.assertNotIn(MENSAGEM_PDF_ENCERRADO, corpo)
        self._assert_com_marcas(corpo, (LIBERADA["texto"], "27.350,00"))
        self._assert_sem_marcas(corpo)
        historico = self._texto(self.client_cli.get("/cliente/historico"))
        self.assertIn("disponíveis até 02/06/2026 às 10:00", historico)
        self.assertNotIn(f'/cliente/analise/{self.fev}"', historico)

        self.relogio(PDF_LIMITE)
        corpo = self._texto(self.client_cli.get("/cliente/dashboard"))
        self.assertIn("Consulta disponível até 02/06/2026 às 10:00", corpo)
        self.assertIn(MENSAGEM_PDF_ENCERRADO, corpo)
        self.assertNotIn("Exportação de PDFs até", corpo)

    def test_sessao_aberta_vale_ate_o_instante_limite(self):
        self.relogio(SUSPENSAO_LIMITE - UM_MICRO)
        self.assertEqual(self.client_cli.get("/cliente/dashboard").status_code, 200)
        self.assertEqual(self.client_cli.get(f"/cliente/analise/{self.jan}").status_code, 200)


# =============================================================================
# (d) Suspensão expirada antes da reconciliação
# =============================================================================
class SuspensaoExpiradaTests(PortalContratualCase):
    """No instante-limite o banco ainda diz SUSPENSO, mas a política já encerra."""

    def setUp(self):
        super().setUp()
        self._cenario()
        self.suspender()
        self.client_cli = self._entrar()
        self.cookie = self._cookie(self.client_cli)

    def test_sessao_existente_e_derrubada_no_limite(self):
        self.relogio(SUSPENSAO_LIMITE)
        self._assert_para_login(self.client_cli.get("/cliente/dashboard"))
        self.assertNotIn("_user_id", self._sessao(self.client_cli))
        for caminho in ("/cliente/historico", f"/cliente/analise/{self.jan}",
                        f"/cliente/analise/{self.jan}/pdf", "/cliente/upload", "/cliente/tickets"):
            with self.subTest(caminho=caminho):
                resposta = self._replay(self.cookie).get(caminho)
                self._assert_para_login(resposta)
                self.assertFalse(resposta.data.startswith(b"%PDF"))
        # Nada foi gravado: o bloqueio vem da política, não da reconciliação.
        self.assertEqual(self._vigente().situacao, "SUSPENSO")
        self.assertEqual(self._empresa().status_conta, "SUSPENSA")

    def test_novo_login_mostra_conta_encerrada(self):
        self.relogio(SUSPENSAO_LIMITE)
        client, resposta = self._tentar_login()
        self._assert_conta_encerrada(client, resposta)
        imagem = client.get("/static/img/nexo-cubo-brilho.png")
        self.assertEqual((imagem.status_code, imagem.mimetype), (200, "image/png"))
        imagem.close()
        # Senha errada continua genérica, sem revelar a situação contratual.
        client, resposta = self._tentar_login(senha="Senha-ficticia-errada-000!")
        self._assert_login_negado(client, resposta, MENSAGEM_GENERICA_LOGIN)

    def test_api_do_nexobot_nao_responde_a_sessao_derrubada(self):
        token = self._meta_csrf(self.client_cli)
        ok = self.client_cli.post("/api/suporte-bot", json={"mensagem": "Abra o dashboard."},
                                  headers={"X-CSRFToken": token})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("resposta", ok.get_json())
        # SEC-001: o login rotaciona a sessão (o token CSRF nasce depois dele);
        # a cópia do cookie precisa ser a que já carrega o token usado acima.
        cookie = self._cookie(self.client_cli)
        self.relogio(SUSPENSAO_LIMITE)
        negado = self._replay(cookie).post("/api/suporte-bot", json={"mensagem": "Abra o dashboard."},
                                           headers={"X-CSRFToken": token})
        self._assert_para_login(negado)
        self.assertFalse(negado.is_json)

    def test_recuperacao_de_senha_nao_envia_email(self):
        def solicitar():
            client = self.app.test_client()
            return client.post("/auth/recuperar-senha", data={
                "email": CLIENTE, "csrf_token": self._csrf(client, "/auth/recuperar-senha"),
            }, follow_redirects=True)
        # Controle: dentro do prazo a recuperação funciona normalmente.
        antes = solicitar()
        self.assertIn(MENSAGEM_RECUPERACAO, self._texto(antes))
        self.mail_recuperacao.assert_called_once()
        self.mail_recuperacao.reset_mock()
        self.relogio(SUSPENSAO_LIMITE)
        depois = solicitar()
        self.assertEqual(depois.status_code, 200)
        self.assertIn(MENSAGEM_RECUPERACAO, self._texto(depois))
        self.assertNotIn("Conta encerrada", self._texto(depois))
        self.mail_recuperacao.assert_not_called()


# =============================================================================
# (e) Login: conta encerrada, desativada e credencial inválida
# =============================================================================
class LoginContaEncerradaTests(PortalContratualCase):
    def setUp(self):
        super().setUp()
        self.admin_client = self._entrar(ADMIN)

    def _form_empresa(self, **alteracoes):
        empresa = self._empresa()
        caminho = f"/admin/empresas/{empresa.id_empresa}/editar"
        dados = {
            "id_segmento": str(empresa.id_segmento), "id_plano_atual": str(empresa.id_plano_atual),
            "razao_social": empresa.razao_social, "nome_fantasia": empresa.nome_fantasia or "",
            "email_contato": empresa.email_contato, "data_contratacao": empresa.data_contratacao.isoformat(),
            "status_conta": empresa.status_conta, "status_conta_exibido": empresa.status_conta,
            "csrf_token": self._csrf(self.admin_client, caminho),
        }
        dados.update(alteracoes)
        return self.admin_client.post(caminho, data=dados)

    def _cancelar_pelo_formulario(self):
        resposta = self._form_empresa(status_conta="CANCELADA")
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.headers["Location"].endswith("/admin/empresas"))
        self.assertEqual(self._empresa().status_conta, "CANCELADA")

    def _para_lixeira(self):
        resposta = self.admin_client.post(f"/admin/empresa/excluir/{self.empresa_id}", data={
            "csrf_token": self._csrf(self.admin_client, "/admin/empresas")})
        self.assertEqual(resposta.status_code, 302)
        self.assertIsNotNone(self._empresa().deletado_em)

    def test_cancelada_pelo_formulario_admin_no_regime_legado(self):
        self.publicar(self.analise_id)
        self._cancelar_pelo_formulario()
        self.assertEqual(self.situacao().regime, "LEGADO")
        self._assert_conta_encerrada(*self._tentar_login())

    def test_cancelada_por_cancelar_contrato(self):
        import contract_admin
        for regras in (False, True):
            with self.subTest(regras_ativas=regras):
                contrato = self.contratar()
                if regras:
                    self.ativar_regras()
                contract_admin.cancelar_contrato(self.db.session, self.empresa_id, contrato.id_contrato,
                                                 contrato.versao, motivo="Rescisão fictícia",
                                                 responsavel=self.admin, instante=self.agora)
                self.db.session.commit()
                self.assertEqual(self.situacao().estado, "CANCELADA")
                self._assert_conta_encerrada(*self._tentar_login())
                # Prepara a segunda volta: nova contratação reativa a empresa.
                self.db.session.expire_all()
                self.relogio(self.agora + timedelta(minutes=1))

    def test_regras_ativas_sem_contrato_vigente_mostra_conta_encerrada(self):
        import contract_admin
        from company_security import aplicar_status_conta, bloquear_empresa
        contrato = self.contratar()
        self.ativar_regras()
        contract_admin.cancelar_contrato(self.db.session, self.empresa_id, contrato.id_contrato,
                                         contrato.versao, motivo="Rescisão fictícia",
                                         responsavel=self.admin, instante=self.agora)
        self.db.session.commit()
        aplicar_status_conta(self.db.session, bloquear_empresa(self.db.session, self.empresa_id), "ATIVA")
        self.db.session.commit()  # alteração fora do fluxo contratual
        self.assertEqual(self.situacao().estado, "SEM_CONTRATO")
        self._assert_conta_encerrada(*self._tentar_login())

    def test_senha_errada_de_conta_cancelada_mostra_apenas_mensagem_generica(self):
        self._cancelar_pelo_formulario()
        client, resposta = self._tentar_login(senha="Senha-ficticia-errada-000!")
        self._assert_login_negado(client, resposta, MENSAGEM_GENERICA_LOGIN)
        self.assertNotIn(MENSAGEM_DESATIVADA, self._texto(resposta))

    def test_usuario_inativo_da_empresa_cancelada_ve_desativada(self):
        self._usuario_inativo()
        self._cancelar_pelo_formulario()
        client, resposta = self._tentar_login(INATIVO)
        self._assert_login_negado(client, resposta, MENSAGEM_DESATIVADA)
        self.assertNotIn(MENSAGEM_GENERICA_LOGIN, self._texto(resposta))
        # Mesmo com senha correta do usuário ativo, a resposta é a de conta encerrada.
        self._assert_conta_encerrada(*self._tentar_login())

    def test_empresa_ativa_na_lixeira_ve_desativada(self):
        self._para_lixeira()
        client, resposta = self._tentar_login()
        self._assert_login_negado(client, resposta, MENSAGEM_DESATIVADA)

    def test_empresa_cancelada_na_lixeira_ve_desativada(self):
        self._cancelar_pelo_formulario()
        self._para_lixeira()
        client, resposta = self._tentar_login()
        self._assert_login_negado(client, resposta, MENSAGEM_DESATIVADA)

    def test_email_inexistente_mostra_mensagem_generica(self):
        self._cancelar_pelo_formulario()
        client, resposta = self._tentar_login("nao-cadastrado@example.invalid")
        self._assert_login_negado(client, resposta, MENSAGEM_GENERICA_LOGIN)
        self.assertNotIn(MENSAGEM_DESATIVADA, self._texto(resposta))

    def test_email_inexistente_paga_o_custo_do_hash_ficticio_e_conta_real_nao(self):
        import blueprints.auth as rotas_auth
        with patch.object(rotas_auth, "_conferir_senha_ficticia",
                          wraps=rotas_auth._conferir_senha_ficticia) as ficticio:
            self._tentar_login("nao-cadastrado@example.invalid")
            self.assertEqual(ficticio.call_count, 1)
            self._tentar_login(CLIENTE, senha="Senha-ficticia-errada-000!")
            self.assertEqual(ficticio.call_count, 1)  # conta real: o hash verdadeiro já foi conferido


# =============================================================================
# (f) Isolamento
# =============================================================================
class IsolamentoPortalTests(PortalContratualCase):
    def test_analise_de_outra_empresa_404_mesmo_com_cobertura_propria(self):
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.ativar_regras()
        propria = self._publicar(self.analise_id, date(2026, 1, 1), date(2026, 1, 31), LIBERADA,
                                 datetime(2026, 2, 5, 12))
        alheia = self._publicar(self.outra_analise_id, date(2026, 1, 1), date(2026, 1, 31), SIGILOSA,
                                datetime(2026, 2, 6, 12))
        # A outra empresa também paga o mesmo período: nada muda para o cliente.
        self.contratar(empresa_id=self.outra_empresa_id)
        self.pagar(date(2026, 1, 1), date(2026, 1, 31), empresa_id=self.outra_empresa_id)
        client = self._entrar()
        self._assert_404(client.get(f"/cliente/analise/{alheia}"))
        self._assert_404(client.get(f"/cliente/analise/{alheia}/pdf"))
        self.assertEqual(client.get(f"/cliente/analise/{propria}").status_code, 200)
        for caminho in ("/cliente/dashboard", "/cliente/historico"):
            corpo = self._texto(client.get(caminho))
            self._assert_sem_marcas(corpo)
            self.assertNotIn(f'/cliente/analise/{alheia}"', corpo)

    def test_analise_nao_concluida_ou_nao_publicada_404(self):
        from models import Analise, RelatorioAnalise
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 2, 28))
        self.ativar_regras()
        aberta = self.nova_analise(date(2026, 2, 1), date(2026, 2, 28), status="EM_ANALISE")
        aberta.relatorio = RelatorioAnalise(titulo="Rascunho fictício", conclusao_estrategica=TEXTO_PRIVADO,
                                            resumo_executivo=TEXTO_PRIVADO, publicado=False)
        self.db.session.commit()
        client = self._entrar()
        for estado in ("EM_ANALISE", "CONCLUIDO"):  # CONCLUIDO sem devolutiva publicada
            with self.subTest(estado=estado):
                analise = self.db.session.get(Analise, aberta.id_analise)
                analise.status_analise = estado
                self.db.session.commit()
                self._assert_404(client.get(f"/cliente/analise/{aberta.id_analise}"))
                self._assert_404(client.get(f"/cliente/analise/{aberta.id_analise}/pdf"))
        self._assert_404(client.get("/cliente/analise/999999"))
        self._assert_404(client.get("/cliente/analise/999999/pdf"))


# =============================================================================
# (g) Envio de arquivos
# =============================================================================
class UploadContratualTests(PortalContratualCase):
    def setUp(self):
        super().setUp()
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.ativar_regras()
        self.jan = self.analise_id  # aberta (AGUARDANDO_RELATORIO), período pago
        self.fev = self.nova_analise(date(2026, 2, 1), date(2026, 2, 28)).id_analise  # aberta, não paga
        self.client_cli = self._entrar()

    def _enviar(self, id_analise):
        return self.client_cli.post("/cliente/upload", data={
            "id_analise": str(id_analise), "tipo_relatorio": "VENDAS", "valor_pdv_informado": "150.000,00",
            "arquivo": (io.BytesIO(CSV_VENDAS), "vendas.csv"),
            "csrf_token": self._csrf(self.client_cli, "/cliente/upload"),
        }, content_type="multipart/form-data")

    def _listadas(self):
        resposta = self.client_cli.get("/cliente/upload")
        self.assertEqual(resposta.status_code, 200)
        corpo = resposta.get_data(as_text=True)
        return {i for i in (self.jan, self.fev) if f'name="id_analise" value="{i}"' in corpo}, self._texto(resposta)

    def _assert_enviado(self, resposta, id_analise):
        self.assertEqual(resposta.status_code, 302, self._texto(resposta)[:400])
        self.assertTrue(resposta.headers["Location"].endswith("/cliente/upload"))
        self.assertIn("Relatório de VENDAS enviado. A equipe NEXO fará a validação e o processamento.",
                      self._flashes(self.client_cli))
        [upload] = self._uploads(id_analise)
        self.assertEqual((upload.tipo_relatorio, upload.status_processamento), ("VENDAS", "PENDENTE"))

    def test_suspensa_lista_e_aceita_somente_analise_aberta_de_periodo_pago(self):
        self.suspender()
        listadas, corpo = self._listadas()
        self.assertEqual(listadas, {self.jan})
        self.assertIn("novos arquivos só podem ser enviados para análises de períodos já pagos", corpo)
        import blueprints.cliente as rotas_cliente
        with patch.object(rotas_cliente, "ler_upload_validado",
                          wraps=rotas_cliente.ler_upload_validado) as leitura:
            recusa = self._enviar(self.fev)
        leitura.assert_not_called()  # recusa antes de ler o arquivo
        self._assert_negado(recusa, titulo="Envio indisponível")
        self.assertEqual(self._uploads(self.fev), [])
        self._assert_enviado(self._enviar(self.jan), self.jan)

    def test_envio_e_reverificado_apos_a_validacao_do_arquivo(self):
        """O prazo pode vencer durante a leitura do arquivo: o relógio é relido antes de gravar."""
        self.suspender()
        import blueprints.cliente as rotas_cliente
        original = rotas_cliente.ler_upload_validado

        def lenta(arquivo, extensao):
            resultado = original(arquivo, extensao)
            self.relogio(SUSPENSAO_LIMITE + timedelta(hours=1))  # o limite passa durante a validação
            return resultado

        with patch.object(rotas_cliente, "ler_upload_validado", side_effect=lenta):
            resposta = self._enviar(self.jan)
        self._assert_negado(resposta, titulo="Envio indisponível")
        self.assertEqual(self._uploads(self.jan), [])

    def test_ativa_envia_normalmente(self):
        listadas, _ = self._listadas()
        self.assertEqual(listadas, {self.jan, self.fev})
        self._assert_enviado(self._enviar(self.fev), self.fev)


# =============================================================================
# (h) Administração contratual pelo HTTP
# =============================================================================
class AdministracaoContratualHttpTests(PortalContratualCase):
    def setUp(self):
        super().setUp()
        from models import Plano
        prata = Plano(nome_plano="PRATA", valor_mensal=300, qtd_analises_mes=1, tipo_analise_permitida="MENSAL",
                      nivel_entrega_analise="COMPLETA", nivel_dashboard="GERENCIAL", nivel_atendimento="MEDIO")
        self.db.session.add(prata)
        self.db.session.commit()
        self.prata = prata.id_plano
        self.bronze = self.db.session.scalar(select(Plano.id_plano).where(Plano.nome_plano == "BRONZE"))
        self.adm = self._entrar(ADMIN)
        self.base = f"/admin/empresas/{self.empresa_id}"

    def _operar(self, caminho, client=None, **campos):
        """GET do formulário real (CSRF e versão exibida) seguido do POST."""
        client = client or self.adm
        pagina = client.get(caminho)
        self.assertEqual(pagina.status_code, 200, caminho)
        corpo = pagina.get_data(as_text=True)
        dados = {"csrf_token": re.search(r'name="csrf_token"[^>]*value="([^"]+)"', corpo).group(1)}
        versao = re.search(r'name="versao" value="(\d+)"', corpo)
        if versao:
            dados["versao"] = versao.group(1)
        dados.update(campos)
        return client.post(caminho, data=dados)

    def _assert_sucesso(self, resposta, mensagem, tipo_evento):
        self.assertEqual(resposta.status_code, 302, self._texto(resposta)[:600])
        self.assertTrue(resposta.headers["Location"].endswith(f"{self.base}/detalhes"))
        self.assertIn(mensagem, self._flashes(self.adm))
        evento = self._eventos()[-1]
        self.assertEqual((evento.tipo, evento.origem, evento.id_usuario_responsavel),
                         (tipo_evento, "ADMIN", self.admin_id))
        return evento

    def _assert_rerender(self, resposta, mensagem):
        self.assertEqual(resposta.status_code, 200)
        corpo = self._texto(resposta)
        self.assertIn(mensagem, corpo)
        self.assertIn('<form method="POST"', corpo)
        return corpo

    def _estado(self):
        from models import CoberturaPaga, Contrato, Empresa, RegimeContratual
        self.db.session.expire_all()
        return (
            [(c.id_contrato, c.id_empresa, c.situacao, c.versao)
             for c in self.db.session.scalars(select(Contrato).order_by(Contrato.id_contrato))],
            [(k.id_cobertura, k.revogada_em) for k in self.db.session.scalars(select(CoberturaPaga))],
            len(self._eventos()) + len(self._eventos(self.outra_empresa_id)),
            self.db.session.scalar(select(func.count(RegimeContratual.id_empresa))),
            [(e.id_empresa, e.status_conta, e.id_plano_atual) for e in self.db.session.scalars(select(Empresa))],
        )

    def test_ciclo_completo_pelas_rotas_admin(self):
        from models import CoberturaPaga
        geracao = self._geracao()
        resposta = self._operar(f"{self.base}/contratos/novo", id_plano=str(self.bronze), valor_mensal="150,00",
                                data_inicio="2026-01-01", data_fim="2026-12-31",
                                referencia_formalizacao="Contrato fictício em papel nº 7",
                                justificativa="Conciliação de cliente fictício")
        self._assert_sucesso(resposta, "Contrato registrado.", "CONTRATACAO")
        contrato = self._vigente()
        c = contrato.id_contrato
        self.assertEqual((contrato.valor_mensal_contratado, contrato.situacao, contrato.versao),
                         (Decimal("150.00"), "ATIVO", 1))

        resposta = self._operar(f"{self.base}/contratos/{c}/cobertura", periodo_inicio="2026-01-01",
                                periodo_fim="2026-01-31", referencia="Comprovante fictício de janeiro")
        cobertura = self._assert_sucesso(resposta, "Período pago registrado.", "COBERTURA_CONFIRMADA").id_cobertura

        resposta = self._operar(f"{self.base}/regras-contratuais/ativar", justificativa="Conciliação fictícia")
        self._assert_sucesso(resposta, "Regras contratuais ativadas para esta empresa.", "REGRAS_ATIVADAS")
        self.assertEqual(self.situacao().regime, "CONTRATUAL")

        # datetime-local no fuso da política: 10:00 em São Paulo = 13:00 UTC.
        resposta = self._operar(f"{self.base}/contratos/{c}/suspender", suspenso_em="2026-03-02T10:00",
                                justificativa="Inadimplência fictícia")
        self._assert_sucesso(resposta, "Suspensão registrada; os prazos foram calculados pela política vigente.",
                             "SUSPENSAO")
        contrato = self._contrato(c)
        self.assertEqual((contrato.situacao, contrato.suspenso_em, contrato.suspensao_limite, contrato.pdf_limite),
                         ("SUSPENSO", T0, SUSPENSAO_LIMITE, PDF_LIMITE))
        self.assertEqual(self._empresa().status_conta, "SUSPENSA")

        resposta = self._operar(f"{self.base}/contratos/{c}/prorrogar", novo_limite="2026-07-01T10:00",
                                justificativa="Acordo fictício de prorrogação")
        self._assert_sucesso(resposta, "Prorrogação registrada no histórico.", "PRORROGACAO")
        contrato = self._contrato(c)
        self.assertEqual((contrato.suspensao_limite, contrato.suspensao_limite_original, contrato.pdf_limite),
                         (datetime(2026, 7, 1, 13, tzinfo=timezone.utc), SUSPENSAO_LIMITE, PDF_LIMITE))

        resposta = self._operar(f"{self.base}/contratos/{c}/reativar", justificativa="Pagamento fictício em dia")
        self._assert_sucesso(resposta, "Contrato reativado.", "REATIVACAO")
        self.assertEqual((self._contrato(c).situacao, self._empresa().status_conta), ("ATIVO", "ATIVA"))

        resposta = self._operar(f"{self.base}/contratos/{c}/renovar", id_plano=str(self.bronze),
                                valor_mensal="160,00", data_inicio="2026-03-01", data_fim="2027-02-28")
        self._assert_sucesso(resposta, "Renovação registrada; o contrato anterior foi preservado como encerrado.",
                             "RENOVACAO")
        self.assertEqual(self._contrato(c).situacao, "ENCERRADO")
        renovado = self._vigente()
        self.assertEqual((renovado.id_contrato_anterior, renovado.valor_mensal_contratado), (c, Decimal("160.00")))

        resposta = self._operar(f"{self.base}/contratos/{renovado.id_contrato}/alterar-plano",
                                id_plano=str(self.prata), valor_mensal="300,00", data_inicio="2026-03-02",
                                data_fim="2027-02-28", justificativa="Cliente fictício pediu o plano Prata")
        self._assert_sucesso(resposta, "Alteração de plano registrada; o contrato anterior foi preservado "
                             "como encerrado.", "ALTERACAO_PLANO")
        atual = self._vigente()
        self.assertEqual((atual.id_plano, atual.id_contrato_anterior, self._empresa().id_plano_atual),
                         (self.prata, renovado.id_contrato, self.prata))

        resposta = self._operar(f"{self.base}/coberturas/{cobertura}/revogar", justificativa="Estorno fictício")
        self._assert_sucesso(resposta, "Cobertura revogada; o registro foi preservado no histórico.",
                             "COBERTURA_REVOGADA")
        self.assertIsNotNone(self.db.session.get(CoberturaPaga, cobertura).revogada_em)

        resposta = self._operar(f"{self.base}/contratos/{atual.id_contrato}/cancelar",
                                justificativa="Rescisão fictícia", confirmacao="CANCELAR")
        self._assert_sucesso(resposta, "Contrato cancelado; as sessões dos usuários da empresa foram revogadas.",
                             "CANCELAMENTO")
        self.assertEqual((self._contrato(atual.id_contrato).situacao, self._empresa().status_conta),
                         ("CANCELADO", "CANCELADA"))
        self.assertEqual(self._geracao(), geracao + 1)

        resposta = self._operar(f"{self.base}/regras-contratuais/desativar", justificativa="Contingência fictícia")
        self._assert_sucesso(resposta, "Regras contratuais desativadas: a empresa voltou ao regime legado.",
                             "REGRAS_DESATIVADAS")

        self.assertEqual([e.tipo for e in self._eventos()], [
            "CONTRATACAO", "COBERTURA_CONFIRMADA", "REGRAS_ATIVADAS", "SUSPENSAO", "PRORROGACAO",
            "REATIVACAO", "RENOVACAO", "ALTERACAO_PLANO", "COBERTURA_REVOGADA", "CANCELAMENTO",
            "REGRAS_DESATIVADAS",
        ])
        detalhes = self.adm.get(f"{self.base}/detalhes")
        self.assertEqual(detalhes.status_code, 200)
        corpo = self._texto(detalhes)
        for rotulo in ("Contratação", "Período pago confirmado", "Prorrogação da suspensão",
                       "Alteração de plano", "Período pago revogado", "Cancelamento"):
            self.assertIn(rotulo, corpo)

    def test_datas_fora_do_intervalo_sao_recusadas_sem_erro_500(self):
        c = self.contratar().id_contrato
        antes = self._estado()
        for campo, rota, extra in (
            ("suspenso_em", "suspender", {}),
            ("novo_limite", "prorrogar", {}),
        ):
            for valor in ("9999-12-31T23:00", "1999-12-31T23:00", "2101-01-01T00:00"):
                with self.subTest(rota=rota, valor=valor):
                    resposta = self._operar(f"{self.base}/contratos/{c}/{rota}", justificativa="x",
                                            **{campo: valor}, **extra)
                    self._assert_rerender(resposta, "Data e hora fora do intervalo permitido.")
        self.assertEqual(self._estado(), antes)

    def test_post_sem_token_csrf_e_recusado_e_nada_muda(self):
        antes = self._estado()
        c = self.contratar().id_contrato
        depois_de_contratar = self._estado()
        self.assertNotEqual(antes, depois_de_contratar)
        for caminho, dados in (
            (f"{self.base}/contratos/novo", dict(id_plano=str(self.bronze), valor_mensal="150,00",
                                                 data_inicio="2026-01-01")),
            (f"{self.base}/contratos/{c}/suspender", dict(justificativa="x", versao="1")),
            (f"{self.base}/contratos/{c}/cancelar", dict(justificativa="x", confirmacao="CANCELAR", versao="1")),
            (f"{self.base}/contratos/{c}/cobertura", dict(periodo_inicio="2026-01-01", periodo_fim="2026-01-31",
                                                         referencia="x", versao="1")),
            (f"{self.base}/regras-contratuais/ativar", dict(justificativa="x")),
            ("/admin/contratos/reconciliar", {}),
        ):
            with self.subTest(caminho=caminho):
                self.assertEqual(self.adm.post(caminho, data=dados).status_code, 400)
        self.assertEqual(self._estado(), depois_de_contratar)

    def test_conciliacao_de_empresa_suspensa_so_volta_a_ativa_pela_acao_explicita(self):
        self._empresa().status_conta = "SUSPENSA"
        self.db.session.commit()
        contrato = self.contratar()
        c = contrato.id_contrato
        self.assertEqual(self._empresa().status_conta, "SUSPENSA")
        detalhes = self._texto(self.adm.get(f"{self.base}/detalhes"))
        self.assertIn("Reativar empresa", detalhes)
        # Renovação e troca de plano históricas não reativam a empresa em silêncio.
        resposta = self._operar(f"{self.base}/contratos/{c}/alterar-plano", id_plano=str(self.prata),
                                valor_mensal="300,00", data_inicio="2026-02-01", data_fim="2026-12-31",
                                justificativa="Troca histórica documentada")
        self._assert_sucesso(resposta, "Alteração de plano registrada; o contrato anterior foi preservado "
                             "como encerrado.", "ALTERACAO_PLANO")
        self.assertEqual(self._empresa().status_conta, "SUSPENSA")
        atual = self._vigente()
        formulario = self._texto(self.adm.get(f"{self.base}/contratos/{atual.id_contrato}/renovar"))
        self.assertIn("o registro não a reativa", formulario)
        # As regras só podem ser ativadas com status e contrato coerentes.
        resposta = self._operar(f"{self.base}/regras-contratuais/ativar", justificativa="Conciliação fictícia")
        self._assert_rerender(resposta, "O status da empresa (SUSPENSA) difere do contrato (ATIVO)")
        self.assertEqual(self.db.session.scalar(select(func.count()).select_from(
            __import__("models").RegimeContratual)), 0)
        geracao = self._geracao()
        resposta = self._operar(f"{self.base}/contratos/{atual.id_contrato}/reativar",
                                justificativa="Regularização fictícia")
        evento = self._assert_sucesso(resposta, "Contrato reativado.", "REATIVACAO")
        self.assertEqual((evento.situacao_anterior, evento.situacao_posterior), ("ATIVO", "ATIVO"))
        self.assertEqual(json.loads(evento.detalhes)["status_conta_anterior"], "SUSPENSA")
        self.assertEqual(self._empresa().status_conta, "ATIVA")
        self.assertEqual(self._geracao(), geracao)  # ATIVA <-> SUSPENSA não revoga sessões
        self._assert_sucesso(self._operar(f"{self.base}/regras-contratuais/ativar", justificativa="Conciliação fictícia"),
                             "Regras contratuais ativadas para esta empresa.", "REGRAS_ATIVADAS")
        # Sem empresa suspensa e sem contrato suspenso, reativar continua recusado.
        resposta = self._operar(f"{self.base}/contratos/{atual.id_contrato}/reativar", justificativa="x")
        self._assert_rerender(resposta, "Somente contratos suspensos, ou uma empresa SUSPENSA com contrato ativo")

    def test_painel_do_admin_trata_suspensao_legada_como_pendencia_de_conciliacao(self):
        from models import Empresa
        self.contratar()
        contrato = self.suspender()  # empresa e contrato suspensos, mas ainda no regime legado
        pagina = self._texto(self.adm.get("/admin/dashboard"))
        self.assertIn("0 suspensão(ões) vencida(s) aguardando reconciliação", pagina)
        self.assertIn("0 terminando em breve", pagina)
        self.relogio(contrato.suspensao_limite - timedelta(days=1))
        pagina = self._texto(self.adm.get("/admin/dashboard"))
        self.assertIn("0 terminando em breve", pagina)  # prazos só valem no regime contratual
        self.relogio(contrato.suspensao_limite + timedelta(days=1))
        pagina = self._texto(self.adm.get("/admin/dashboard"))
        self.assertIn("0 suspensão(ões) vencida(s) aguardando reconciliação", pagina)
        self.assertRegex(pagina, r"[1-9]\d* pendência\(s\)")  # aparece como pendência a conciliar
        self.assertEqual(self._empresa().status_conta, "SUSPENSA")  # nada foi cancelado
        self.assertIsNotNone(self.db.session.get(Empresa, self.empresa_id))

    def test_erros_de_validacao_rerenderizam_o_formulario(self):
        novo = f"{self.base}/contratos/novo"
        for campos, mensagem in (
            (dict(data_inicio="2026-06-01", data_fim="2026-05-31"),
             "O término da vigência não pode ser anterior ao início."),
            (dict(data_inicio="2026-02-30"), "Data inválida (use o seletor de data)."),
            (dict(data_inicio="2026-01-01", valor_mensal="abc"), "Valor mensal contratado inválido."),
            (dict(data_inicio=""), "Preencha todas as datas obrigatórias."),
        ):
            with self.subTest(mensagem):
                dados = {"id_plano": str(self.bronze), "valor_mensal": "150,00", **campos}
                corpo = self._assert_rerender(self._operar(novo, **dados), mensagem)
                if campos.get("data_fim"):
                    self.assertIn('value="2026-05-31"', corpo)  # o formulário preserva o digitado
        self.assertIsNone(self._vigente())
        self.assertEqual(self._eventos(), [])

        contrato = self.contratar()
        c = contrato.id_contrato
        rota = f"{self.base}/contratos/{c}"
        for caminho, campos, mensagem in (
            (f"{rota}/cobertura", dict(periodo_inicio="2027-01-01", periodo_fim="2027-01-31", referencia="x"),
             "O período pago precisa estar dentro da vigência do contrato."),
            (f"{rota}/cobertura", dict(periodo_inicio="2026-02-10", periodo_fim="2026-02-01", referencia="x"),
             "O fim do período pago não pode ser anterior ao início."),
            (f"{rota}/suspender", dict(justificativa=""), "Informe o motivo da suspensão."),
            (f"{rota}/suspender", dict(justificativa="x", suspenso_em="2026-03-02T10:01"),
             "O início da suspensão não pode estar no futuro."),
            (f"{rota}/suspender", dict(justificativa="x", suspenso_em="02/03/2026 10:00"),
             "Data e hora inválidas (use o seletor)."),
            (f"{rota}/prorrogar", dict(justificativa="x", novo_limite="2026-08-01T10:00"),
             "Somente suspensões registradas podem ser prorrogadas."),
            (f"{rota}/reativar", dict(justificativa="x"),
             "Somente contratos suspensos, ou uma empresa SUSPENSA com contrato ativo, podem ser reativados."),
            (f"{rota}/cancelar", dict(justificativa="Rescisão fictícia"),
             "Digite CANCELAR para confirmar o cancelamento."),
            (f"{rota}/cancelar", dict(justificativa="Rescisão fictícia", confirmacao="cancelar"),
             "Digite CANCELAR para confirmar o cancelamento."),
            (f"{rota}/alterar-plano", dict(id_plano=str(self.bronze), valor_mensal="150,00",
                                           data_inicio="2026-03-01", justificativa="x"),
             "Selecione um plano diferente do atual para registrar a alteração."),
            (f"{rota}/alterar-plano", dict(id_plano=str(self.prata), valor_mensal="300,00",
                                           data_inicio="2026-03-01"),
             "Informe a justificativa."),
            (novo, dict(id_plano=str(self.bronze), valor_mensal="150,00", data_inicio="2026-01-01"),
             "A empresa já possui contrato vigente: use a renovação ou a alteração de plano."),
            (f"{self.base}/regras-contratuais/desativar", dict(justificativa="x"),
             "As regras contratuais já estão inativas para esta empresa."),
        ):
            with self.subTest(caminho=caminho, mensagem=mensagem):
                self._assert_rerender(self._operar(caminho, **campos), mensagem)
        contrato = self._contrato(c)
        self.assertEqual((contrato.situacao, contrato.versao), ("ATIVO", 1))
        self.assertEqual([e.tipo for e in self._eventos()], ["CONTRATACAO"])
        self.assertEqual(self._empresa().status_conta, "ATIVA")

    def test_cliente_recebe_403_em_todas_as_rotas_de_contratos(self):
        contrato = self.contratar()
        cobertura = self.pagar(date(2026, 1, 1), date(2026, 1, 31)).evento.id_cobertura
        c = contrato.id_contrato
        cliente = self._entrar()
        token = self._meta_csrf(cliente)
        antes = self._estado()
        rotas = [f"{self.base}/contratos/novo", f"{self.base}/contratos/{c}/renovar",
                 f"{self.base}/contratos/{c}/alterar-plano", f"{self.base}/contratos/{c}/cobertura",
                 f"{self.base}/coberturas/{cobertura}/revogar"]
        rotas += [f"{self.base}/contratos/{c}/{acao}" for acao in ("suspender", "prorrogar", "reativar", "cancelar")]
        rotas += [f"{self.base}/regras-contratuais/{acao}" for acao in ("ativar", "desativar")]
        dados = {"csrf_token": token, "versao": "1", "justificativa": "Tentativa fictícia",
                 "confirmacao": "CANCELAR", "id_plano": str(self.prata), "valor_mensal": "1,00",
                 "data_inicio": "2027-01-01", "periodo_inicio": "2026-02-01", "periodo_fim": "2026-02-28",
                 "referencia": "Comprovante forjado", "novo_limite": "2026-09-01T10:00"}
        for rota in rotas + [f"{self.base}/detalhes", "/admin/contratos"]:
            with self.subTest(rota=rota, metodo="GET"):
                resposta = cliente.get(rota)
                self.assertEqual(resposta.status_code, 403)
                self.assertNotIn("Comprovante fictício", self._texto(resposta))
        for rota in rotas + ["/admin/contratos/reconciliar"]:
            with self.subTest(rota=rota, metodo="POST"):
                self.assertEqual(cliente.post(rota, data=dados).status_code, 403)
        self.assertEqual(self._estado(), antes)
        self.assertEqual(cliente.get("/cliente/dashboard").status_code, 200)  # 403 não derruba a sessão

    def test_ids_de_outra_empresa_na_url_respondem_404(self):
        proprio = self.contratar()
        alheio = self.contratar(empresa_id=self.outra_empresa_id)
        cobertura_alheia = self.pagar(date(2026, 1, 1), date(2026, 1, 31),
                                      empresa_id=self.outra_empresa_id).evento.id_cobertura
        token = self._csrf(self.adm, f"{self.base}/detalhes")
        antes = self._estado()
        dados = {"csrf_token": token, "versao": "1", "justificativa": "Tentativa fictícia",
                 "confirmacao": "CANCELAR", "id_plano": str(self.prata), "valor_mensal": "1,00",
                 "data_inicio": "2027-01-01", "periodo_inicio": "2026-02-01", "periodo_fim": "2026-02-28",
                 "referencia": "x", "novo_limite": "2026-09-01T10:00"}
        rotas = [f"{self.base}/contratos/{alheio.id_contrato}/{sufixo}" for sufixo in (
            "renovar", "alterar-plano", "cobertura", "suspender", "prorrogar", "reativar", "cancelar")]
        rotas += [f"{self.base}/coberturas/{cobertura_alheia}/revogar",
                  f"{self.base}/contratos/{proprio.id_contrato}/apagar",
                  "/admin/empresas/999999/contratos/novo",
                  "/admin/empresas/999999/regras-contratuais/ativar",
                  f"{self.base}/regras-contratuais/excluir"]
        for rota in rotas:
            with self.subTest(rota=rota):
                self.assertEqual(self.adm.get(rota).status_code, 404)
                self.assertEqual(self.adm.post(rota, data=dados).status_code, 404)
        self.assertEqual(self.adm.get("/admin/empresas/999999/detalhes").status_code, 404)  # só GET
        self.assertEqual(self._estado(), antes)

    def test_versao_desatualizada_no_formulario_e_recusada(self):
        contrato = self.contratar()
        caminho = f"{self.base}/contratos/{contrato.id_contrato}/cancelar"
        tela = self.adm.get(caminho).get_data(as_text=True)
        versao_exibida = re.search(r'name="versao" value="(\d+)"', tela).group(1)
        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', tela).group(1)
        self.assertEqual(versao_exibida, "1")
        # Outra aba suspende o contrato enquanto a tela de cancelamento segue aberta.
        resposta = self._operar(f"{self.base}/contratos/{contrato.id_contrato}/suspender",
                                justificativa="Inadimplência fictícia")
        self._assert_sucesso(resposta, "Suspensão registrada; os prazos foram calculados pela política vigente.",
                             "SUSPENSAO")
        for versao, mensagem in ((versao_exibida, "O contrato foi alterado por outra operação. Recarregue a "
                                  "página e confira antes de salvar."), ("abc", "Valor numérico inválido.")):
            with self.subTest(versao=versao):
                resposta = self.adm.post(caminho, data={
                    "csrf_token": token, "versao": versao, "justificativa": "Rescisão fictícia",
                    "confirmacao": "CANCELAR"})
                self._assert_rerender(resposta, mensagem)
        self.assertEqual(self._contrato(contrato.id_contrato).situacao, "SUSPENSO")
        self.assertNotIn("CANCELAMENTO", [e.tipo for e in self._eventos()])
        self.assertEqual(self._empresa().status_conta, "SUSPENSA")

    def test_prorrogacao_pelo_formulario_mantem_portal_apos_o_limite_original(self):
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.ativar_regras()
        jan = self._publicar(self.analise_id, date(2026, 1, 1), date(2026, 1, 31), LIBERADA,
                             datetime(2026, 2, 5, 12))
        contrato = self.suspender()
        cliente = self._entrar()
        resposta = self._operar(f"{self.base}/contratos/{contrato.id_contrato}/prorrogar",
                                novo_limite="2026-07-01T10:00", justificativa="Acordo fictício")
        self._assert_sucesso(resposta, "Prorrogação registrada no histórico.", "PRORROGACAO")
        self.relogio(SUSPENSAO_LIMITE + timedelta(days=1))  # depois do limite original
        corpo = self._texto(cliente.get("/cliente/dashboard"))
        self.assertIn("Consulta disponível até 01/07/2026 às 10:00", corpo)
        self.assertIn(MENSAGEM_PDF_ENCERRADO, corpo)  # prorrogação não amplia o PDF
        self.assertEqual(cliente.get(f"/cliente/analise/{jan}").status_code, 200)
        self._assert_negado(cliente.get(f"/cliente/analise/{jan}/pdf"), titulo="Exportação encerrada",
                            marcas=MARCAS_LIBERADAS)
        self.relogio(datetime(2026, 7, 1, 13, tzinfo=timezone.utc))
        self._assert_para_login(cliente.get("/cliente/dashboard"))

    def test_suspensao_retroativa_pelo_formulario_conta_prazos_do_inicio_oficial(self):
        self.relogio(T0 - timedelta(days=20))
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.ativar_regras()
        jan = self._publicar(self.analise_id, date(2026, 1, 1), date(2026, 1, 31), LIBERADA,
                             datetime(2026, 2, 5, 12))
        self.relogio(T0)
        c = self._vigente().id_contrato
        # Início oficial dez dias antes do registro: 20/02/2026 10:00 em São Paulo.
        resposta = self._operar(f"{self.base}/contratos/{c}/suspender", suspenso_em="2026-02-20T10:00",
                                justificativa="Inadimplência fictícia retroativa")
        self._assert_sucesso(resposta, "Suspensão registrada; os prazos foram calculados pela política vigente.",
                             "SUSPENSAO")
        inicio = datetime(2026, 2, 20, 13, tzinfo=timezone.utc)
        limite, pdf = datetime(2026, 5, 20, 13, tzinfo=timezone.utc), datetime(2026, 3, 6, 13, tzinfo=timezone.utc)
        contrato = self._contrato(c)
        self.assertEqual((contrato.suspenso_em, contrato.suspensao_limite, contrato.pdf_limite), (inicio, limite, pdf))
        cliente = self._entrar()
        corpo = self._texto(cliente.get("/cliente/dashboard"))
        self.assertIn("Consulta disponível até 20/05/2026 às 10:00", corpo)
        self.assertIn("Exportação de PDFs até 06/03/2026 às 10:00", corpo)
        self.relogio(pdf - UM_MICRO)
        self._assert_pdf(cliente.get(f"/cliente/analise/{jan}/pdf"))
        self.relogio(pdf)
        self._assert_negado(cliente.get(f"/cliente/analise/{jan}/pdf"), titulo="Exportação encerrada",
                            marcas=MARCAS_LIBERADAS)
        self.relogio(limite - UM_MICRO)
        self.assertEqual(cliente.get(f"/cliente/analise/{jan}").status_code, 200)
        self.relogio(limite)
        self._assert_para_login(cliente.get(f"/cliente/analise/{jan}"))
        self._assert_conta_encerrada(*self._tentar_login())

    def test_visao_geral_lista_vencidas_e_reconciliacao_cancela_e_revoga(self):
        from contract_admin import MOTIVO_EXPIRACAO
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.ativar_regras()
        contrato = self.suspender()
        cliente = self._entrar()
        cookie = self._cookie(cliente)
        geracao = self._geracao()
        pagina = self.adm.get("/admin/contratos")
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("Nenhuma suspensão vencida.", self._texto(pagina))

        self.relogio(SUSPENSAO_LIMITE + timedelta(hours=1))
        pagina = self.adm.get("/admin/contratos")
        self.assertEqual(pagina.status_code, 200)
        corpo = self._texto(pagina)
        self.assertIn("Suspensões encerradas aguardando reconciliação", corpo)
        self.assertNotIn("Nenhuma suspensão vencida.", corpo)
        self.assertIn("Teste 1", corpo)
        self.assertIn("limite 02/06/2026 às 10:00", corpo)
        self.assertIn("1 suspensão(ões) vencida(s)", corpo)
        detalhes = self.adm.get(f"{self.base}/detalhes")
        self.assertEqual(detalhes.status_code, 200)
        self.assertIn("Prazo da suspensão encerrado.", self._texto(detalhes))
        self.assertEqual(self.adm.get("/admin/dashboard").status_code, 200)
        # A visão geral é só leitura: nada muda antes da aplicação.
        self.assertEqual((self._contrato(contrato.id_contrato).situacao, self._geracao()), ("SUSPENSO", geracao))

        token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina.get_data(as_text=True)).group(1)
        resposta = self.adm.post("/admin/contratos/reconciliar", data={"csrf_token": token})
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.headers["Location"].endswith("/admin/contratos"))
        self.assertIn("Reconciliação concluída: 1 contrato(s) cancelado(s) de 1 vencido(s).", self._flashes(self.adm))
        contrato = self._contrato(contrato.id_contrato)
        self.assertEqual((contrato.situacao, contrato.cancelamento_motivo), ("CANCELADO", MOTIVO_EXPIRACAO))
        self.assertEqual(self._empresa().status_conta, "CANCELADA")
        self.assertEqual(self._geracao(), geracao + 1)
        ultimo = self._eventos()[-1]
        self.assertEqual((ultimo.tipo, ultimo.origem, ultimo.id_usuario_responsavel),
                         ("CANCELAMENTO", "RECONCILIACAO", None))
        self.assertEqual(json.loads(ultimo.detalhes)["acionado_por"], self.admin_id)  # quem clicou
        self._assert_para_login(self._replay(cookie).get("/cliente/dashboard"))
        self._assert_conta_encerrada(*self._tentar_login())
        self.assertIn("Nenhuma suspensão vencida.", self._texto(self.adm.get("/admin/contratos")))

        # Idempotente: segunda aplicação não cancela nem revoga de novo.
        token = self._csrf(self.adm, "/admin/contratos")
        self.adm.post("/admin/contratos/reconciliar", data={"csrf_token": token})
        self.assertIn("Reconciliação concluída: 0 contrato(s) cancelado(s) de 0 vencido(s).", self._flashes(self.adm))
        self.assertEqual([e.tipo for e in self._eventos()].count("CANCELAMENTO"), 1)
        self.assertEqual(self._geracao(), geracao + 1)


# =============================================================================
# (i) Nova contratação após cancelamento
# =============================================================================
class NovaContratacaoTests(PortalContratualCase):
    def test_nova_contratacao_exige_novo_login_e_mantem_periodos_nao_pagos_bloqueados(self):
        from models import Plano
        self._cenario()
        adm = self._entrar(ADMIN)
        cliente = self._entrar()
        cookie = self._cookie(cliente)
        self.assertEqual(cliente.get(f"/cliente/analise/{self.jan}").status_code, 200)
        geracao = self._geracao()
        contrato = self.vigente()
        base = f"/admin/empresas/{self.empresa_id}"

        tela = adm.get(f"{base}/contratos/{contrato.id_contrato}/cancelar").get_data(as_text=True)
        resposta = adm.post(f"{base}/contratos/{contrato.id_contrato}/cancelar", data={
            "csrf_token": re.search(r'name="csrf_token"[^>]*value="([^"]+)"', tela).group(1),
            "versao": re.search(r'name="versao" value="(\d+)"', tela).group(1),
            "justificativa": "Rescisão fictícia", "confirmacao": "CANCELAR"})
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual((self._empresa().status_conta, self._geracao()), ("CANCELADA", geracao + 1))
        self._assert_para_login(self._replay(cookie).get("/cliente/dashboard"))
        self._assert_conta_encerrada(*self._tentar_login())

        formulario = adm.get(f"{base}/contratos/novo")
        self.assertIn("Empresa CANCELADA: este contrato a reativa.", self._texto(formulario))
        resposta = adm.post(f"{base}/contratos/novo", data={
            "csrf_token": re.search(r'name="csrf_token"[^>]*value="([^"]+)"',
                                    formulario.get_data(as_text=True)).group(1),
            "id_plano": str(self.db.session.scalar(select(Plano.id_plano))), "valor_mensal": "150,00",
            "data_inicio": "2026-03-01", "data_fim": "2027-02-28", "justificativa": "Nova contratação fictícia"})
        self.assertEqual(resposta.status_code, 302)
        with adm.session_transaction() as sessao:
            self.assertIn("Contrato registrado.", [m for _c, m in sessao.get("_flashes", ())])
        self.assertEqual((self._empresa().status_conta, self._geracao()), ("ATIVA", geracao + 2))
        self.assertEqual(self.situacao().regime, "CONTRATUAL")

        # O cookie anterior ao cancelamento não volta a valer.
        antigo = self._replay(cookie)
        self._assert_para_login(antigo.get("/cliente/dashboard"))
        self.assertNotIn("_user_id", self._sessao(antigo))
        novo = self._entrar()
        self.assertEqual(novo.get("/cliente/dashboard").status_code, 200)
        self.assertEqual(novo.get(f"/cliente/analise/{self.jan}").status_code, 200)  # cobertura preservada
        self._assert_negado(novo.get(f"/cliente/analise/{self.fev}"))
        self._assert_negado(novo.get(f"/cliente/analise/{self.fev}/pdf"))
        self._assert_sem_marcas(self._texto(novo.get("/cliente/dashboard")))


# =============================================================================
# (j) Bloqueios administrativos
# =============================================================================
class BloqueiosAdministrativosTests(PortalContratualCase):
    def setUp(self):
        super().setUp()
        self.adm = self._entrar(ADMIN)

    def _contagem_analises(self):
        from models import Analise
        self.db.session.expire_all()
        return self.db.session.scalar(select(func.count(Analise.id_analise)))

    def _abrir_analise(self, inicio, fim, empresa_id=None):
        return self.adm.post("/admin/analises/nova", data={
            "id_empresa": str(empresa_id or self.empresa_id), "tipo_analise": "MENSAL",
            "periodo_inicio": inicio.isoformat(), "periodo_fim": fim.isoformat(),
            "mes_referencia": str(inicio.month), "ano_referencia": str(inicio.year),
            "csrf_token": self._csrf(self.adm, "/admin/analises/nova"),
        })

    def _publicar_devolutiva(self, id_analise):
        caminho = f"/admin/analise/{id_analise}/relatorio"
        return self.adm.post(caminho, data={
            "acao": "publicar", "titulo": "Devolutiva fictícia", "resumo_executivo": TEXTO_PRIVADO,
            "conclusao_estrategica": TEXTO_PRIVADO, "csrf_token": self._csrf(self.adm, caminho),
        })

    def _em_analise(self, id_analise):
        from models import Analise
        self.db.session.get(Analise, id_analise).status_analise = "EM_ANALISE"
        self.db.session.commit()

    def test_nova_analise_fora_da_vigencia_e_recusada(self):
        from models import Analise
        contrato = self.contratar(data_inicio=date(2026, 1, 1), data_fim=date(2026, 6, 30))
        self.ativar_regras()
        total = self._contagem_analises()
        for inicio, fim in ((date(2026, 7, 1), date(2026, 7, 31)), (date(2026, 6, 15), date(2026, 7, 14)),
                            (date(2025, 12, 1), date(2025, 12, 31))):
            with self.subTest(inicio=inicio):
                resposta = self._abrir_analise(inicio, fim)
                self.assertEqual(resposta.status_code, 302)
                self.assertTrue(resposta.headers["Location"].endswith("/admin/analises/nova"))
                self.assertIn("O período da análise está fora da vigência dos contratos da empresa.",
                              self._flashes(self.adm))
        self.assertEqual(self._contagem_analises(), total)
        resposta = self._abrir_analise(date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(self._contagem_analises(), total + 1)
        nova = self.db.session.scalar(select(Analise).order_by(Analise.id_analise.desc()).limit(1))
        self.assertEqual((nova.periodo_inicio, nova.id_plano_referencia), (date(2026, 5, 1), contrato.id_plano))
        # Regime legado (outra empresa, sem contrato) segue sem essa restrição.
        self.assertEqual(self._abrir_analise(date(2027, 7, 1), date(2027, 7, 31), self.outra_empresa_id).status_code, 302)
        self.assertEqual(self._contagem_analises(), total + 2)

    def test_edicao_da_empresa_com_contrato_vigente_nao_muda_status_nem_plano(self):
        from models import Plano
        prata = Plano(nome_plano="PRATA", valor_mensal=300, qtd_analises_mes=1, tipo_analise_permitida="MENSAL",
                      nivel_entrega_analise="COMPLETA", nivel_dashboard="GERENCIAL", nivel_atendimento="MEDIO")
        self.db.session.add(prata)
        self.db.session.commit()
        self.contratar()
        caminho = f"/admin/empresas/{self.empresa_id}/editar"
        self.assertIn("Geridos pelos contratos", self._texto(self.adm.get(caminho)))
        empresa = self._empresa()
        plano_original, geracao = empresa.id_plano_atual, self._geracao()

        def enviar(**alteracoes):
            empresa = self._empresa()
            dados = {
                "id_segmento": str(empresa.id_segmento), "id_plano_atual": str(empresa.id_plano_atual),
                "razao_social": empresa.razao_social, "nome_fantasia": empresa.nome_fantasia or "",
                "email_contato": empresa.email_contato, "data_contratacao": empresa.data_contratacao.isoformat(),
                "status_conta": empresa.status_conta, "status_conta_exibido": empresa.status_conta,
                "csrf_token": self._csrf(self.adm, caminho), **alteracoes,
            }
            return self.adm.post(caminho, data=dados)

        for regras in (False, True):
            if regras:
                self.ativar_regras()
            for alteracao in (dict(status_conta="CANCELADA"), dict(status_conta="SUSPENSA"),
                              dict(id_plano_atual=str(prata.id_plano))):
                with self.subTest(regras=regras, alteracao=alteracao):
                    resposta = enviar(**alteracao)
                    self.assertEqual(resposta.status_code, 302)
                    self.assertTrue(resposta.headers["Location"].endswith(f"/admin/empresas/{self.empresa_id}/detalhes"))
                    self.assertIn("Status e plano desta empresa são geridos pelos contratos: use Detalhes → Contratos.",
                                  self._flashes(self.adm))
                    empresa = self._empresa()
                    self.assertEqual((empresa.status_conta, empresa.id_plano_atual), ("ATIVA", plano_original))
                    self.assertEqual(self._geracao(), geracao)
        # Dados cadastrais continuam editáveis.
        resposta = enviar(razao_social="Empresa fictícia 1 renomeada")
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.headers["Location"].endswith("/admin/empresas"))
        self.assertIn("Empresa atualizada com sucesso.", self._flashes(self.adm))
        empresa = self._empresa()
        self.assertEqual((empresa.razao_social, empresa.status_conta, empresa.id_plano_atual),
                         ("Empresa fictícia 1 renomeada", "ATIVA", plano_original))

    def test_publicar_periodo_nao_pago_nao_notifica_o_cliente(self):
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.ativar_regras()
        fev = self.nova_analise(date(2026, 2, 1), date(2026, 2, 28), status="EM_ANALISE").id_analise
        resposta = self._publicar_devolutiva(fev)
        self.assertEqual(resposta.status_code, 302)
        flashes = self._flashes(self.adm)
        self.assertIn("Devolutiva publicada para o cliente.", flashes)
        self.assertTrue(any("o cliente não foi avisado" in f for f in flashes), flashes)
        self.assertEqual(self._notificacoes(), 0)
        self.mail_publicacao.assert_not_called()
        cliente = self._entrar()
        self._assert_negado(cliente.get(f"/cliente/analise/{fev}"))
        self.assertEqual(self._texto(cliente.get("/cliente/dashboard")).count("Relatório Estratégico"), 0)

        # Controle: período pago notifica normalmente.
        self._em_analise(self.analise_id)
        resposta = self._publicar_devolutiva(self.analise_id)
        self.assertEqual(resposta.status_code, 302)
        flashes = self._flashes(self.adm)
        self.assertFalse(any("o cliente não foi avisado" in f for f in flashes), flashes)
        self.assertEqual(self._notificacoes(), 1)
        self.mail_publicacao.assert_called_once()
        self.assertEqual(cliente.get(f"/cliente/analise/{self.analise_id}").status_code, 200)

    def test_publicar_com_suspensao_expirada_nao_notifica(self):
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.ativar_regras()
        self.suspender()
        self._em_analise(self.analise_id)  # janeiro: período pago
        self.relogio(SUSPENSAO_LIMITE)     # expirada, ainda não reconciliada
        resposta = self._publicar_devolutiva(self.analise_id)
        self.assertEqual(resposta.status_code, 302)
        flashes = self._flashes(self.adm)
        self.assertTrue(any("o cliente não foi avisado" in f for f in flashes), flashes)
        self.assertEqual(self._notificacoes(), 0)
        self.mail_publicacao.assert_not_called()

    def test_empresa_sem_contrato_publica_e_notifica_como_antes(self):
        self._em_analise(self.analise_id)
        resposta = self._publicar_devolutiva(self.analise_id)
        self.assertEqual(resposta.status_code, 302)
        flashes = self._flashes(self.adm)
        self.assertIn("Devolutiva publicada para o cliente.", flashes)
        self.assertFalse(any("o cliente não foi avisado" in f for f in flashes), flashes)
        self.assertEqual(self._notificacoes(), 1)
        self.mail_publicacao.assert_called_once()
        cliente = self._entrar()
        resposta = cliente.get(f"/cliente/analise/{self.analise_id}")
        self.assertEqual(resposta.status_code, 200)
        self.assertIn(TEXTO_PRIVADO, self._texto(resposta))


# =============================================================================
# (k) Socket.IO
# =============================================================================
class SocketContratualTests(PortalContratualCase):
    def test_handshake_recusado_com_suspensao_expirada(self):
        self._cenario()
        self.suspender()
        cookie = self._cookie(self._entrar())
        self.assertTrue(self._socket(self._replay(cookie)).is_connected())  # suspensa, dentro do prazo
        self.relogio(SUSPENSAO_LIMITE - UM_MICRO)
        self.assertTrue(self._socket(self._replay(cookie)).is_connected())
        self.relogio(SUSPENSAO_LIMITE)
        self.assertFalse(self._socket(self._replay(cookie)).is_connected())
        self.relogio(SUSPENSAO_LIMITE + timedelta(days=10))
        self.assertFalse(self._socket(self._replay(cookie)).is_connected())


if __name__ == "__main__":
    unittest.main()
