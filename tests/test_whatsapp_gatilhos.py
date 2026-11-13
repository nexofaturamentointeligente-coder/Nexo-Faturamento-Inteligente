"""Gatilhos do WhatsApp nas rotas (whatsapp_service.py), com migrações reais em SQLite.

Executar da raiz: python -B -m unittest tests.test_whatsapp_gatilhos -v

O caminho real da mensagem roda de ponta a ponta: a rota chama a fábrica
whatsapp_service.notificador(), trocada aqui por um WhatsAppNotificador de verdade,
configurado com dados fictícios e síncrono (assincrono=False); o único ponto de rede
(whatsapp_service._abrir) vira um coletor que guarda cada pedido (método, URL, corpo JSON,
cabeçalhos e prazo), confere no instante do POST a vaga única de upload e o que o banco
já confirmou, e responde 201 como a Evolution API.

Nas classes *Desligado* a fábrica real fica no lugar (só observada): a configuração dos
testes deixa EVOLUTION_* vazias, então as rotas seguem iguais e o _abrir nunca é chamado.

Qualquer conexão de socket falha o teste (DeployTestCase). Dados sintéticos
(example.invalid); os relatórios de vendas são os fictícios de tests/raio_x_support.py.
"""

import contextlib
from calendar import monthrange
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
import io
import json
import re
import unittest
from unittest.mock import patch
import urllib.error

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import etl_uploads
from tests.contratos_support import ContratosCase
from tests.deploy_support import DeployTestCase
from tests.raio_x_support import FEVEREIRO, JANEIRO, avaliar_como_o_worker, executar_como_o_worker, relatorio_do_mes
import whatsapp_service

URL_API = "https://evolution.example.invalid"
CHAVE_API = "chave-de-teste"
INSTANCIA = "nexo"
ENDPOINT = f"{URL_API}/message/sendText/{INSTANCIA}"
ADMIN = "admin-test@example.invalid"
CLIENTE = "cliente-test@example.invalid"
# Empresa 1 das fixtures (tests/deploy_support.py) e o cliente dela.
EMPRESA = "Teste 1"
NOME_CLIENTE = "Cliente fictício"
TELEFONE_EMPRESA = "(62) 98888-7766"
NUMERO_EMPRESA = "5562988887766"
WHATSAPP_LEAD = "(11) 90000-0000"
NUMERO_LEAD = "5511900000000"
PAGINA_LEADS = "/admin/leads"
PAGINA_RAIO_X = "/onboarding/diagnostico"
API_RAIO_X = "/api/onboarding/diagnostico"
ROTULOS = ("Aberto", "Em atendimento", "Resolvido")

# O lote do lead de tests/test_avaliacao_cenario.py (LOTE): nov-dez/2024 e jan-fev/2025.
NOV24 = relatorio_do_mes(2024, 11, ["1;Cafe 500g;10;20,00;200,00;150,00", "2;Acucar 1kg;5;6,00;30,00;",
                                    "3;Leite 1L;20;5,00;100,00;110,00"])
DEZ24 = relatorio_do_mes(2024, 12, ["1;Cafe 500g;10;20,00;200,00;150,00", "2;Acucar 1kg;5;6,00;30,00;",
                                    "3;Leite 1L;20;5,00;100,00;90,00"])
JAN25 = relatorio_do_mes(2025, 1, ["1;Café 500g;8;20,00;150,00;120,00", "3;Leite 1L;5;5,00;25,00;20,00"])
FEV25 = relatorio_do_mes(2025, 2, ["1;Café 500g;4;20,00;80,00;60,00", "3;Leite 1L;5;5,00;25,00;20,00",
                                   "4;Arroz 5kg;2;27,90;55,80;44,00"])
LOTE = (NOV24, DEZ24, JAN25, FEV25)
# Um mês com um preço por item e custo abaixo da venda: nada a recuperar.
SEM_VAZAMENTO = relatorio_do_mes(2025, 3, ["1;Cafe 500g;10;20,00;200,00;150,00", "4;Arroz 5kg;2;27,90;55,80;44,00"])


def _arquivos(lote):
    return [(f"mes_{numero}.csv", conteudo) for numero, conteudo in enumerate(lote, 1)]


def _reais(valor) -> str:
    """O valor como as telas o mostram (macro brl): "R$ 1.234,56", centavos meio para cima."""
    numero = Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "R$ " + f"{numero:,.2f}".translate(str.maketrans(",.", ".,"))


class _RespostaEvolution:
    """Resposta 201 da Evolution API: o que o urllib devolveria (status e uso em with)."""
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *_erro):
        return False


class _WhatsAppMixin:
    """Coletor dos POSTs à Evolution API e helpers HTTP comuns. Sem métodos test_*."""

    # False: a fábrica real (envio desligado pela configuração dos testes), só observada.
    whatsapp_ligado = True

    def _preparar_whatsapp(self):
        self.pedidos = []
        # Conferência opcional feita no instante do POST (o que o banco já confirmou). Só
        # devolve valores: uma exceção aqui seria engolida pelo fail-safe do notificador.
        self.ao_disparar = None
        if self.whatsapp_ligado:
            def fabrica(**_opcoes):
                return whatsapp_service.WhatsAppNotificador(URL_API, CHAVE_API, INSTANCIA, assincrono=False)
            self.fabrica = self.stack.enter_context(
                patch.object(whatsapp_service, "notificador", side_effect=fabrica))
        else:
            self.fabrica = self.stack.enter_context(
                patch.object(whatsapp_service, "notificador", wraps=whatsapp_service.notificador))
        self.abrir = self.stack.enter_context(patch.object(whatsapp_service, "_abrir", side_effect=self._coletar))

    def _coletar(self, pedido, timeout):
        # A vaga única de upload/processamento: livre quando o aviso sai fora do `with vaga_de_entrada()`.
        livre = etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False)
        if livre:
            etl_uploads._UPLOAD_ADMISSION.release()
        self.pedidos.append({
            "metodo": pedido.get_method(), "url": pedido.full_url, "timeout": timeout,
            "cabecalhos": {nome.lower(): valor for nome, valor in pedido.header_items()},
            "corpo": json.loads(pedido.data.decode("utf-8")),
            "vaga_livre": livre,
            "no_banco": self.ao_disparar() if self.ao_disparar else None,
        })
        return _RespostaEvolution()

    def _mensagem(self, numero):
        """O único POST desde a última conferência, com o envelope da Evolution API; o consome."""
        self.assertEqual(len(self.pedidos), 1, [p["corpo"] for p in self.pedidos])
        pedido = self.pedidos.pop()
        self.assertEqual((pedido["metodo"], pedido["url"], pedido["timeout"]), ("POST", ENDPOINT, 5))
        self.assertEqual(pedido["cabecalhos"].get("apikey"), CHAVE_API)
        self.assertEqual(pedido["cabecalhos"].get("content-type"), "application/json")
        self.assertEqual(sorted(pedido["corpo"]), ["number", "text"])
        self.assertEqual(pedido["corpo"]["number"], numero)
        pedido["texto"] = pedido["corpo"]["text"]
        self.assertIsInstance(pedido["texto"], str)
        return pedido

    def _sem_mensagem(self):
        self.assertEqual(self.pedidos, [])

    # ------------------------------------------------------------------ HTTP
    def _csrf(self, cliente, caminho):
        resposta = cliente.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        achado = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', resposta.get_data(as_text=True))
        self.assertIsNotNone(achado, f"Token CSRF ausente em {caminho}")
        return achado.group(1)

    def _meta_csrf(self, cliente, caminho="/cliente/dashboard"):
        resposta = cliente.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        return re.search(r'<meta name="csrf-token" content="([^"]+)"', resposta.get_data(as_text=True)).group(1)

    def _entrar(self, email):
        cliente = self.app.test_client()
        resposta = cliente.post("/auth/login", data={
            "email": email, "senha": self.password, "csrf_token": self._csrf(cliente, "/auth/login")})
        self.assertEqual(resposta.status_code, 302, resposta.get_data(as_text=True)[:300])
        return cliente

    def _flashes(self, cliente):
        with cliente.session_transaction() as sessao:
            return [str(texto) for _categoria, texto in sessao.get("_flashes", ())]

    def _confirmado(self, consulta):
        """Outra conexão: só enxerga o que já foi confirmado (COMMIT)."""
        with Session(bind=self.db.engine) as outra:
            return outra.scalar(consulta)

    @contextlib.contextmanager
    def _avisos(self, logger):
        """Os registros WARNING ou acima do logger (o fileConfig do Alembic o deixa desligado)."""
        with patch.object(logger, "disabled", False), self.assertLogs(logger, level="WARNING") as registro:
            yield registro


# =====================================================================
# Portal: chamados, análises e Raio-X de entrada
# =====================================================================
class _PortalCase(_WhatsAppMixin, ContratosCase):
    """Admin e cliente pelo HTTP, com o relógio da política em T0 (tests/contratos_support.py) e o
    Raio-X no próprio processo, como em tests/test_planos_raio_x.py."""

    def setUp(self):
        super().setUp()
        self._preparar_whatsapp()
        self.stack.enter_context(patch("etl_runtime.run_raio_x", side_effect=executar_como_o_worker))
        # A abertura do envio só avisa o mês corrente, o anterior ou um futuro: "hoje" fixo em
        # 13/11/2026, para outubro, novembro e dezembro de 2026 continuarem recentes.
        self.stack.enter_context(patch("blueprints.admin._hoje_em_brasilia", return_value=date(2026, 11, 13)))
        self.admin_http = self._entrar(ADMIN)

    def _empresa(self, id_empresa=None):
        from models import Empresa
        self.db.session.expire_all()
        return self.db.session.get(Empresa, id_empresa or self.empresa_id)

    def _telefone(self, valor=TELEFONE_EMPRESA):
        self._empresa().telefone_contato = valor
        self.db.session.commit()

    # -------------------------------------------------------------- chamados
    def _chamado(self, status="ABERTO"):
        from models import ChamadoSuporte, MensagemSuporte
        chamado = ChamadoSuporte(id_empresa=self.empresa_id, id_usuario_cliente=self.cliente_id,
                                 assunto="Chamado fictício", categoria="DUVIDA_TECNICA", status_chamado=status)
        self.db.session.add(chamado)
        self.db.session.flush()
        self.db.session.add(MensagemSuporte(id_chamado=chamado.id_chamado, id_usuario_remetente=self.cliente_id,
                                            mensagem="Mensagem fictícia do cliente."))
        self.db.session.commit()
        return chamado.id_chamado

    def _no_chamado(self, id_chamado, **dados):
        """POST do admin em /admin/tickets/<id> (acao=responder ou alterar_status)."""
        caminho = f"/admin/tickets/{id_chamado}"
        return self.admin_http.post(caminho, data={"csrf_token": self._csrf(self.admin_http, caminho), **dados})

    def _status_confirmado(self, id_chamado):
        from models import ChamadoSuporte
        return self._confirmado(select(ChamadoSuporte.status_chamado).where(ChamadoSuporte.id_chamado == id_chamado))

    def _respostas_da_equipe(self, id_chamado):
        from models import MensagemSuporte
        return self._confirmado(select(func.count()).select_from(MensagemSuporte).where(
            MensagemSuporte.id_chamado == id_chamado, MensagemSuporte.id_usuario_remetente == self.admin_id))

    def _chamados_confirmados(self):
        from models import ChamadoSuporte
        return self._confirmado(select(func.count()).select_from(ChamadoSuporte))

    # -------------------------------------------------------------- análises
    def _criar_analise(self, mes=10, ano=2026, **campos):
        """POST /admin/analises/nova (análise MENSAL da empresa 1) com o mês inteiro como período."""
        caminho = "/admin/analises/nova"
        dados = {"csrf_token": self._csrf(self.admin_http, caminho), "id_empresa": str(self.empresa_id),
                 "tipo_analise": "MENSAL", "periodo_inicio": date(ano, mes, 1).isoformat(),
                 "periodo_fim": date(ano, mes, monthrange(ano, mes)[1]).isoformat(),
                 "mes_referencia": str(mes), "ano_referencia": str(ano)}
        dados.update(campos)
        return self.admin_http.post(caminho, data=dados)

    def _analises_confirmadas(self, mes, ano):
        from models import Analise
        return self._confirmado(select(func.count()).select_from(Analise).where(
            Analise.id_empresa == self.empresa_id, Analise.mes_referencia == mes, Analise.ano_referencia == ano))

    def _em_analise(self, id_analise):
        """A análise depois do ETL (EM_ANALISE, com indicadores), pronta para publicar."""
        from models import Analise, IndicadorAnalise
        analise = self.db.session.get(Analise, id_analise)
        analise.status_analise = "EM_ANALISE"
        analise.indicador = IndicadorAnalise(faturamento_total=250, total_comprado=100,
                                             saldo_estimado_compras_vendas=150)
        self.db.session.commit()

    def _publicar(self, id_analise):
        from models import RelatorioAnalise
        caminho = f"/admin/analise/{id_analise}/relatorio"
        token = self._csrf(self.admin_http, caminho)
        self.db.session.expire_all()
        relatorio = self.db.session.scalar(select(RelatorioAnalise).where(RelatorioAnalise.id_analise == id_analise))
        return self.admin_http.post(caminho, data={
            "csrf_token": token, "acao": "publicar",
            "versao_rascunho": str(relatorio.versao_rascunho if relatorio else 0),
            "titulo": "Título sintético", "resumo_executivo": "Resumo sintético",
            "conclusao_estrategica": "Conclusão sintética"})

    def _publicada(self, id_analise):
        """(status da análise, devolutiva publicada), como confirmados no banco."""
        from models import Analise, RelatorioAnalise
        return (self._confirmado(select(Analise.status_analise).where(Analise.id_analise == id_analise)),
                self._confirmado(select(RelatorioAnalise.publicado).where(RelatorioAnalise.id_analise == id_analise)))

    # --------------------------------------------------------------- Raio-X
    def _enviar_raio_x(self, cliente, *arquivos, api=False):
        dados = {"arquivos": [(io.BytesIO(conteudo), nome) for nome, conteudo in arquivos]}
        if api:
            return cliente.post(API_RAIO_X, data=dados, headers={"X-CSRFToken": self._meta_csrf(cliente)},
                                content_type="multipart/form-data")
        dados["csrf_token"] = self._csrf(cliente, PAGINA_RAIO_X)
        return cliente.post(PAGINA_RAIO_X, data=dados, content_type="multipart/form-data")

    def _raio_x_gravado(self):
        from models import DiagnosticoEntrada
        self.db.session.expire_all()
        return self.db.session.get(DiagnosticoEntrada, self.empresa_id)

    def _raio_x_confirmados(self):
        from models import DiagnosticoEntrada
        return self._confirmado(select(func.count()).select_from(DiagnosticoEntrada))


class ChamadoRespondidoTests(_PortalCase):
    """Resposta da equipe (acao=responder): aviso ao telefone de contato da empresa do chamado."""

    def test_resposta_da_equipe_avisa_o_telefone_da_empresa_depois_do_commit(self):
        self._telefone()
        id_chamado = self._chamado()
        self.ao_disparar = lambda: (self._status_confirmado(id_chamado), self._respostas_da_equipe(id_chamado))
        resposta = self._no_chamado(id_chamado, acao="responder", mensagem="Resposta fictícia da equipe.")
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.headers["Location"].endswith(f"/admin/tickets/{id_chamado}"))
        self.assertIn("Resposta enviada ao cliente.", self._flashes(self.admin_http))
        pedido = self._mensagem(NUMERO_EMPRESA)
        texto = pedido["texto"]
        self.assertIn(f"chamado #{id_chamado}", texto)
        self.assertIn("Em atendimento", texto)
        self.assertIn(NOME_CLIENTE, texto)
        # A conversa fica no portal: a mensagem só avisa.
        self.assertNotIn("Resposta fictícia da equipe", texto)
        # O POST sai depois do COMMIT: a resposta e o novo status já estavam confirmados.
        self.assertEqual(pedido["no_banco"], ("EM_ATENDIMENTO", 1))
        self.assertEqual((self._status_confirmado(id_chamado), self._respostas_da_equipe(id_chamado)),
                         ("EM_ATENDIMENTO", 1))

    def test_sem_telefone_de_contato_nao_dispara(self):
        id_chamado = self._chamado()
        resposta = self._no_chamado(id_chamado, acao="responder", mensagem="Resposta fictícia.")
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("Resposta enviada ao cliente.", self._flashes(self.admin_http))
        self.assertEqual((self._status_confirmado(id_chamado), self._respostas_da_equipe(id_chamado)),
                         ("EM_ATENDIMENTO", 1))
        self._sem_mensagem()
        self.assertEqual(self.abrir.call_count, 0)

    def test_formatos_de_telefone_aceitos_saem_com_o_ddi_55(self):
        id_chamado = self._chamado()
        aceitos = (("+55 (62) 98888-7766", NUMERO_EMPRESA), ("0 62 98888-7766", NUMERO_EMPRESA),
                   ("62988887766", NUMERO_EMPRESA), ("(62) 3222-1234", "556232221234"))
        for numero, (telefone, esperado) in enumerate(aceitos, 1):
            with self.subTest(telefone=telefone):
                self._telefone(telefone)
                resposta = self._no_chamado(id_chamado, acao="responder", mensagem=f"Resposta fictícia {numero}.")
                self.assertEqual(resposta.status_code, 302)
                self.assertEqual(self._respostas_da_equipe(id_chamado), numero)
                self._mensagem(esperado)

    def test_telefone_que_nao_e_brasileiro_valido_nao_dispara(self):
        # O cadastro do admin aceita texto livre (ramal, número curto, estrangeiro, DDD inexistente).
        id_chamado = self._chamado()
        invalidos = ("3222-1234", "(62) 98888-7766 ramal 12", "+1 202 555 0100", "(20) 98888-7766")
        for numero, telefone in enumerate(invalidos, 1):
            with self.subTest(telefone=telefone):
                self._telefone(telefone)
                with self._avisos(whatsapp_service.logger) as registro:
                    resposta = self._no_chamado(id_chamado, acao="responder", mensagem=f"Resposta fictícia {numero}.")
                self.assertEqual(resposta.status_code, 302)
                self.assertEqual(self._respostas_da_equipe(id_chamado), numero)
                self._sem_mensagem()
                self.assertIn("não é um número brasileiro válido", "\n".join(registro.output))

    def test_quem_desligou_os_alertas_de_chamado_nao_recebe_whatsapp(self):
        import preferencias_usuario
        self._telefone()
        id_chamado = self._chamado()
        cliente = self._entrar(CLIENTE)
        # Só o lembrete de renovação fica marcado: os alertas de chamado ficam desligados.
        resposta = cliente.post("/cliente/configuracoes/preferencias", data={
            "csrf_token": self._csrf(cliente, "/cliente/configuracoes"), "lembrete_renovacao": "1"})
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("Preferências salvas.", self._flashes(cliente))
        self.db.session.expire_all()
        self.assertEqual(preferencias_usuario.obter_preferencias(self.cliente_id),
                         preferencias_usuario.Preferencias(lembrete_renovacao=True, alerta_chamado=False))
        self._no_chamado(id_chamado, acao="responder", mensagem="Resposta fictícia.")
        self._no_chamado(id_chamado, acao="alterar_status", status_chamado="RESOLVIDO")
        self.assertEqual((self._status_confirmado(id_chamado), self._respostas_da_equipe(id_chamado)), ("RESOLVIDO", 1))
        self._sem_mensagem()

    def test_empresa_sem_acesso_ao_portal_nao_recebe_whatsapp(self):
        self._telefone()
        id_chamado = self._chamado()
        self._empresa().status_conta = "CANCELADA"
        self.db.session.commit()
        self.assertEqual(self._no_chamado(id_chamado, acao="responder", mensagem="Resposta à cancelada.").status_code, 302)
        self._sem_mensagem()
        empresa = self._empresa()
        empresa.status_conta, empresa.deletado_em = "ATIVA", datetime(2026, 3, 1, 12)  # na lixeira
        self.db.session.commit()
        self.assertEqual(self._no_chamado(id_chamado, acao="responder", mensagem="Resposta à lixeira.").status_code, 302)
        self._sem_mensagem()
        self.assertEqual(self._respostas_da_equipe(id_chamado), 2)
        # Controle: com o acesso de volta, a mesma resposta avisa.
        self._empresa().deletado_em = None
        self.db.session.commit()
        self._no_chamado(id_chamado, acao="responder", mensagem="Resposta com acesso.")
        self.assertIn(f"chamado #{id_chamado}", self._mensagem(NUMERO_EMPRESA)["texto"])

    def test_resposta_do_cliente_e_chamado_novo_nao_disparam(self):
        self._telefone()
        id_chamado = self._chamado(status="RESOLVIDO")
        cliente = self._entrar(CLIENTE)
        caminho = f"/cliente/tickets/{id_chamado}"
        resposta = cliente.post(caminho, data={"csrf_token": self._csrf(cliente, caminho),
                                               "mensagem": "Nova dúvida fictícia."})
        self.assertEqual(resposta.status_code, 302)
        # A resposta do cliente reabre o chamado resolvido, sem WhatsApp.
        self.assertEqual(self._status_confirmado(id_chamado), "EM_ATENDIMENTO")
        novo = cliente.post("/cliente/tickets", data={
            "csrf_token": self._csrf(cliente, "/cliente/tickets"), "assunto": "Chamado novo fictício",
            "categoria": "FINANCEIRO", "mensagem": "Dúvida fictícia."})
        self.assertEqual(novo.status_code, 302)
        self.assertEqual(self._chamados_confirmados(), 2)
        self._sem_mensagem()
        # As rotas do cliente nem montam o notificador.
        self.assertEqual(self.fabrica.call_count, 0)

    def test_api_fora_do_ar_nao_muda_a_resposta_da_rota(self):
        self._telefone()
        id_chamado = self._chamado()
        falhas = (urllib.error.URLError("rede fictícia fora do ar"), TimeoutError("prazo fictício esgotado"),
                  urllib.error.HTTPError(ENDPOINT, 500, "Erro fictício", None,
                                         io.BytesIO(b'{"number": "5562988887766", "text": "eco"}')))
        for numero, falha in enumerate(falhas, 1):
            with self.subTest(falha=type(falha).__name__):
                self.abrir.side_effect = falha
                with self._avisos(whatsapp_service.logger) as registro:
                    resposta = self._no_chamado(id_chamado, acao="responder", mensagem=f"Resposta fictícia {numero}.")
                self.assertEqual(resposta.status_code, 302)
                self.assertIn("Resposta enviada ao cliente.", self._flashes(self.admin_http))
                self.assertEqual(self._respostas_da_equipe(id_chamado), numero)
                self.assertEqual(self.abrir.call_count, numero)
                log = "\n".join(registro.output)
                self.assertIn("***7766", log)
                # Sem o número inteiro, a chave, o nome ou o corpo devolvido pela API.
                for privado in (NUMERO_EMPRESA, CHAVE_API, NOME_CLIENTE, "eco"):
                    self.assertNotIn(privado, log)
        self.abrir.side_effect = urllib.error.URLError("rede fictícia fora do ar")
        with self._avisos(whatsapp_service.logger) as registro:
            resposta = self._no_chamado(id_chamado, acao="alterar_status", status_chamado="RESOLVIDO")
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("Status do chamado atualizado.", self._flashes(self.admin_http))
        self.assertEqual(self._status_confirmado(id_chamado), "RESOLVIDO")
        self.assertIn("Evolution API indisponível", "\n".join(registro.output))
        self.assertEqual(self.abrir.call_count, len(falhas) + 1)

    def test_falha_ao_montar_o_notificador_nao_muda_a_resposta_da_rota(self):
        self._telefone()
        id_chamado = self._chamado()
        self.fabrica.side_effect = RuntimeError("PRIVATE_SENTINEL")
        with self._avisos(self.app.logger) as registro:
            resposta = self._no_chamado(id_chamado, acao="responder", mensagem="Resposta fictícia.")
            self.assertEqual(resposta.status_code, 302)
            self.assertIn("Resposta enviada ao cliente.", self._flashes(self.admin_http))
            mudanca = self._no_chamado(id_chamado, acao="alterar_status", status_chamado="RESOLVIDO")
            self.assertEqual(mudanca.status_code, 302)
            self.assertIn("Status do chamado atualizado.", self._flashes(self.admin_http))
        self.assertEqual((self._respostas_da_equipe(id_chamado), self._status_confirmado(id_chamado)), (1, "RESOLVIDO"))
        self._sem_mensagem()
        log = "\n".join(registro.output)
        self.assertEqual(log.count("Aviso por WhatsApp do chamado não disparado (RuntimeError)."), 2, log)
        self.assertNotIn("PRIVATE_SENTINEL", log)


class ChamadoStatusTests(_PortalCase):
    """Mudança de status pelo admin (acao=alterar_status): só mudança real avisa, inclusive a reabertura."""

    def _conferir_rotulo(self, texto, rotulo):
        """O rótulo das telas, e só ele; nunca o código do status."""
        self.assertIn(rotulo, texto)
        for outro in ROTULOS:
            if outro != rotulo:
                self.assertNotIn(outro, texto)
        self.assertNotRegex(texto, r"ABERTO|EM_ATENDIMENTO|RESOLVIDO")

    def test_resolver_avisa_e_salvar_de_novo_como_resolvido_nao(self):
        self._telefone()
        id_chamado = self._chamado()
        self.ao_disparar = lambda: self._status_confirmado(id_chamado)
        resposta = self._no_chamado(id_chamado, acao="alterar_status", status_chamado="RESOLVIDO")
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("Status do chamado atualizado.", self._flashes(self.admin_http))
        pedido = self._mensagem(NUMERO_EMPRESA)
        self.assertIn(f"chamado #{id_chamado}", pedido["texto"])
        self._conferir_rotulo(pedido["texto"], "Resolvido")
        self.assertEqual(pedido["no_banco"], "RESOLVIDO")
        # Salvar de novo o mesmo status não é mudança: nada sai.
        resposta = self._no_chamado(id_chamado, acao="alterar_status", status_chamado="RESOLVIDO")
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("Status do chamado atualizado.", self._flashes(self.admin_http))
        self.assertEqual(self._status_confirmado(id_chamado), "RESOLVIDO")
        self._sem_mensagem()

    def test_reabertura_pelo_admin_avisa_com_o_status_aberto(self):
        self._telefone()
        id_chamado = self._chamado(status="RESOLVIDO")
        resposta = self._no_chamado(id_chamado, acao="alterar_status", status_chamado="ABERTO")
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(self._status_confirmado(id_chamado), "ABERTO")
        self._conferir_rotulo(self._mensagem(NUMERO_EMPRESA)["texto"], "Aberto")
        # Salvar ABERTO de novo não avisa; ABERTO -> EM_ATENDIMENTO é mudança real e avisa.
        self._no_chamado(id_chamado, acao="alterar_status", status_chamado="ABERTO")
        self._sem_mensagem()
        self._no_chamado(id_chamado, acao="alterar_status", status_chamado="EM_ATENDIMENTO")
        self._conferir_rotulo(self._mensagem(NUMERO_EMPRESA)["texto"], "Em atendimento")
        # Status fora da lista é recusado, sem gravar nem avisar.
        self._no_chamado(id_chamado, acao="alterar_status", status_chamado="FECHADO")
        self.assertIn("Status inválido.", self._flashes(self.admin_http))
        self.assertEqual(self._status_confirmado(id_chamado), "EM_ATENDIMENTO")
        self._sem_mensagem()


class AberturaDoEnvioTests(_PortalCase):
    """Análise nova (AGUARDANDO_RELATORIO) aberta pelo admin: o envio do mês abriu para a empresa."""

    def test_analise_mensal_nova_avisa_a_abertura_do_envio(self):
        from models import Analise
        self._telefone()
        self.ao_disparar = lambda: self._analises_confirmadas(10, 2026)
        resposta = self._criar_analise(mes=10, ano=2026)
        self.assertEqual(resposta.status_code, 302)
        self.db.session.expire_all()
        analise = self.db.session.scalar(select(Analise).where(
            Analise.id_empresa == self.empresa_id, Analise.mes_referencia == 10, Analise.ano_referencia == 2026))
        self.assertEqual((analise.status_analise, analise.tipo_analise), ("AGUARDANDO_RELATORIO", "MENSAL"))
        self.assertTrue(resposta.headers["Location"].endswith(f"/admin/analise/{analise.id_analise}/upload"))
        self.assertIn(f"Análise #{analise.id_analise} criada para {EMPRESA}.", self._flashes(self.admin_http))
        pedido = self._mensagem(NUMERO_EMPRESA)
        self.assertIn("outubro de 2026", pedido["texto"])
        self.assertIn(EMPRESA, pedido["texto"])
        self.assertEqual(pedido["no_banco"], 1)

    def test_cada_quinzena_aberta_avisa_a_sua_quinzena(self):
        # Plano OURO: cada quinzena aberta avisa, dizendo qual quinzena do mês abriu.
        from models import Plano
        ouro = Plano(nome_plano="OURO", valor_mensal=650, qtd_analises_mes=2, tipo_analise_permitida="QUINZENAL",
                     nivel_entrega_analise="PREMIUM", nivel_dashboard="COMPLETO", nivel_atendimento="ALTO")
        self.db.session.add(ouro)
        self.db.session.commit()
        empresa = self._empresa()
        empresa.id_plano_atual, empresa.telefone_contato = ouro.id_plano, TELEFONE_EMPRESA
        self.db.session.commit()
        textos = []
        for quinzena, (inicio, fim) in ((1, ("2026-10-01", "2026-10-15")), (2, ("2026-10-16", "2026-10-31"))):
            with self.subTest(quinzena=quinzena):
                resposta = self._criar_analise(mes=10, ano=2026, tipo_analise="QUINZENAL",
                                               quinzena_referencia=str(quinzena), periodo_inicio=inicio, periodo_fim=fim)
                self.assertEqual(resposta.status_code, 302)
                textos.append(self._mensagem(NUMERO_EMPRESA)["texto"])
        self.assertEqual(self._analises_confirmadas(10, 2026), 2)
        self.assertIn("O envio dos relatórios da 1ª quinzena de outubro de 2026 está aberto", textos[0])
        self.assertIn("O envio dos relatórios da 2ª quinzena de outubro de 2026 está aberto", textos[1])

    def test_lancamento_retroativo_nao_avisa_e_o_mes_anterior_avisa(self):
        # Hoje é 13/11/2026: setembro é histórico (o admin sobe os arquivos), outubro é o mês
        # anterior, e janeiro de 2027 é futuro.
        self._telefone()
        for mes, ano in ((9, 2026), (12, 2025), (1, 2025)):
            with self.subTest(mes=mes, ano=ano):
                self.assertEqual(self._criar_analise(mes=mes, ano=ano).status_code, 302)
                self.assertEqual(self._analises_confirmadas(mes, ano), 1)
        self._sem_mensagem()
        self.assertEqual(self._criar_analise(mes=10, ano=2026).status_code, 302)
        self.assertIn("outubro de 2026", self._mensagem(NUMERO_EMPRESA)["texto"])
        self.assertEqual(self._criar_analise(mes=1, ano=2027).status_code, 302)
        self.assertIn("janeiro de 2027", self._mensagem(NUMERO_EMPRESA)["texto"])

    def test_virada_do_ano_conta_dezembro_como_mes_anterior(self):
        self._telefone()
        with patch("blueprints.admin._hoje_em_brasilia", return_value=date(2027, 1, 5)):
            self.assertEqual(self._criar_analise(mes=11, ano=2026).status_code, 302)
            self._sem_mensagem()
            self.assertEqual(self._criar_analise(mes=12, ano=2026).status_code, 302)
            self.assertIn("dezembro de 2026", self._mensagem(NUMERO_EMPRESA)["texto"])

    def test_analise_nova_sem_telefone_nao_avisa(self):
        resposta = self._criar_analise(mes=11, ano=2026)
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(self._analises_confirmadas(11, 2026), 1)
        self._sem_mensagem()

    def test_analise_recusada_nao_avisa(self):
        self._telefone()
        resposta = self._criar_analise(mes=12, ano=2026, mes_referencia="13")
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("Mês de referência deve estar entre 1 e 12.", self._flashes(self.admin_http))
        self._empresa().status_conta = "SUSPENSA"
        self.db.session.commit()
        self._criar_analise(mes=12, ano=2026)
        self.assertIn("Empresa em status SUSPENSA — novas análises bloqueadas.", self._flashes(self.admin_http))
        self.assertEqual((self._analises_confirmadas(12, 2026), self._analises_confirmadas(13, 2026)), (0, 0))
        self._sem_mensagem()


class AnalisePublicadaTests(_PortalCase):
    """Devolutiva publicada (acao=publicar): relatório pronto, sem valor, só quando o cliente pode abrir."""

    def setUp(self):
        super().setUp()
        self.email = self.stack.enter_context(patch("blueprints.admin.email_analise_publicada", return_value=True))

    def test_publicacao_que_o_cliente_pode_abrir_avisa_sem_valor(self):
        from models import RelatorioAnalise
        self._telefone()
        self._em_analise(self.analise_id)
        self.ao_disparar = lambda: self._confirmado(select(RelatorioAnalise.publicado).where(
            RelatorioAnalise.id_analise == self.analise_id))
        resposta = self._publicar(self.analise_id)
        self.assertEqual(resposta.status_code, 302)
        flashes = self._flashes(self.admin_http)
        self.assertIn("Devolutiva publicada para o cliente.", flashes)
        self.assertFalse(any("não foi avisado" in texto for texto in flashes), flashes)
        self.assertEqual(self._publicada(self.analise_id), ("CONCLUIDO", True))
        pedido = self._mensagem(NUMERO_EMPRESA)
        # Aviso da empresa: cumprimenta a empresa, como a abertura do envio.
        self.assertTrue(pedido["texto"].startswith(f"Olá, {EMPRESA}! O seu relatório está pronto"), pedido["texto"])
        self.assertIn("Acesse o portal", pedido["texto"])
        # A análise mensal não calcula potencial recuperável: a mensagem sai sem valor.
        for ausente in ("Encontramos", "R$", "potencial recuperável"):
            self.assertNotIn(ausente, pedido["texto"])
        self.assertTrue(pedido["no_banco"])
        self.email.assert_called_once()

    def test_publicacao_sem_telefone_nao_avisa_por_whatsapp(self):
        self._em_analise(self.analise_id)
        self.assertEqual(self._publicar(self.analise_id).status_code, 302)
        self.assertEqual(self._publicada(self.analise_id), ("CONCLUIDO", True))
        self.email.assert_called_once()
        self._sem_mensagem()

    def test_periodo_que_o_cliente_nao_pode_abrir_nao_avisa(self):
        self._telefone()
        self.contratar()
        self.pagar(date(2026, 1, 1), date(2026, 1, 31))
        self.ativar_regras()
        fevereiro = self.nova_analise(date(2026, 2, 1), date(2026, 2, 28), status="EM_ANALISE").id_analise
        resposta = self._publicar(fevereiro)
        self.assertEqual(resposta.status_code, 302)
        flashes = self._flashes(self.admin_http)
        self.assertIn("Devolutiva publicada para o cliente.", flashes)
        self.assertTrue(any("o cliente não foi avisado" in texto for texto in flashes), flashes)
        self.assertEqual(self._publicada(fevereiro), ("CONCLUIDO", True))
        self.email.assert_not_called()
        self._sem_mensagem()
        # Controle: janeiro, período pago, avisa.
        self._em_analise(self.analise_id)
        self.assertEqual(self._publicar(self.analise_id).status_code, 302)
        self.assertIn(f"Olá, {EMPRESA}! O seu relatório está pronto", self._mensagem(NUMERO_EMPRESA)["texto"])
        self.email.assert_called_once()

    def test_republicar_a_devolutiva_avisa_de_novo_como_o_email(self):
        # Comportamento atual, igual ao do e-mail e do sininho: publicar de novo uma devolutiva já
        # publicada (CONCLUIDO -> CONCLUIDO, para corrigir o texto) repete o aviso.
        self._telefone()
        self._em_analise(self.analise_id)
        self._publicar(self.analise_id)
        self._mensagem(NUMERO_EMPRESA)
        self.assertEqual(self._publicar(self.analise_id).status_code, 302)
        self.assertIn(f"Olá, {EMPRESA}! O seu relatório está pronto", self._mensagem(NUMERO_EMPRESA)["texto"])
        self.assertEqual(self.email.call_count, 2)


class RaioXEntradaTests(_PortalCase):
    """Raio-X de entrada do cliente (página e API): relatório pronto com o potencial recuperável."""

    def setUp(self):
        super().setUp()
        self.cliente_http = self._entrar(CLIENTE)

    def test_raio_x_pela_pagina_avisa_com_o_valor_recuperavel(self):
        self._telefone()
        self.ao_disparar = self._raio_x_confirmados
        resposta = self._enviar_raio_x(self.cliente_http, ("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO))
        self.assertEqual((resposta.status_code, resposta.headers["Location"]), (303, PAGINA_RAIO_X))
        self.assertIn("Raio-X concluído com 2 relatório(s) de vendas.", self._flashes(self.cliente_http))
        linha = self._raio_x_gravado()
        self.assertGreater(linha.valor_recuperavel, 0)
        pedido = self._mensagem(NUMERO_EMPRESA)
        for trecho in (NOME_CLIENTE, f"O relatório de {EMPRESA} está pronto",
                       f"Encontramos {_reais(linha.valor_recuperavel)} em potencial recuperável.",
                       "Acesse o portal"):
            self.assertIn(trecho, pedido["texto"])
        # Depois do commit e fora da vaga única de upload.
        self.assertEqual(pedido["no_banco"], 1)
        self.assertTrue(pedido["vaga_livre"])

    def test_raio_x_pela_api_avisa_e_sem_potencial_sai_sem_valor(self):
        self._telefone()
        resposta = self._enviar_raio_x(self.cliente_http, ("jan.csv", JANEIRO), api=True)
        self.assertEqual(resposta.status_code, 201)
        linha = self._raio_x_gravado()
        self.assertGreater(linha.valor_recuperavel, 0)
        # O valor da mensagem é o da tela e do JSON (recuperavel.total), gravado em DiagnosticoEntrada.
        self.assertEqual(Decimal(str(resposta.get_json()["resultado"]["recuperavel"]["total"])), linha.valor_recuperavel)
        pedido = self._mensagem(NUMERO_EMPRESA)
        self.assertIn(NOME_CLIENTE, pedido["texto"])
        self.assertIn(f"Encontramos {_reais(linha.valor_recuperavel)} em potencial recuperável.", pedido["texto"])
        self.assertTrue(pedido["vaga_livre"])
        # Sem nada a recuperar, o aviso sai sem a linha do valor.
        resposta = self._enviar_raio_x(self.cliente_http, ("fev.csv", FEVEREIRO), api=True)
        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(self._raio_x_gravado().valor_recuperavel, 0)
        texto = self._mensagem(NUMERO_EMPRESA)["texto"]
        self.assertIn(f"O relatório de {EMPRESA} está pronto", texto)
        self.assertNotIn("Encontramos", texto)
        self.assertNotIn("R$", texto)

    def test_raio_x_sem_telefone_nao_avisa(self):
        self.assertEqual(self._enviar_raio_x(self.cliente_http, ("jan.csv", JANEIRO)).status_code, 303)
        self.assertEqual(self._enviar_raio_x(self.cliente_http, ("fev.csv", FEVEREIRO), api=True).status_code, 201)
        self.assertEqual(self._raio_x_gravado().periodo_inicio, date(2026, 2, 1))
        self._sem_mensagem()

    def test_raio_x_recusado_nao_avisa(self):
        from models import Empresa
        self._telefone()
        pagina = self._enviar_raio_x(self.cliente_http, ("vazio.csv", b""))
        self.assertEqual(pagina.status_code, 303)
        self.assertTrue(any("está vazio" in texto for texto in self._flashes(self.cliente_http)))
        self.assertEqual(self._enviar_raio_x(self.cliente_http, ("vazio.csv", b""), api=True).status_code, 422)
        # Vaga única ocupada: nada é processado nem avisado.
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            ocupada = self._enviar_raio_x(self.cliente_http, ("jan.csv", JANEIRO), api=True)
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()
        self.assertEqual(ocupada.status_code, 503)

        # Assinatura suspensa durante a leitura: o ETL rodou, mas nada é gravado nem avisado.
        def suspender_no_meio(caminhos, limits=None):
            resultado = executar_como_o_worker(caminhos, limits)
            self.db.session.get(Empresa, self.empresa_id).status_conta = "SUSPENSA"
            self.db.session.commit()
            return resultado
        with patch("etl_runtime.run_raio_x", side_effect=suspender_no_meio):
            suspensa = self._enviar_raio_x(self.cliente_http, ("jan.csv", JANEIRO))
        self.assertEqual(suspensa.status_code, 303)
        self.assertTrue(any("o Raio-X não foi gravado" in texto for texto in self._flashes(self.cliente_http)))
        self.assertIsNone(self._raio_x_gravado())
        self._sem_mensagem()

    def test_api_do_whatsapp_fora_do_ar_nao_muda_o_raio_x(self):
        self._telefone()
        self.abrir.side_effect = urllib.error.URLError("rede fictícia fora do ar")
        with self._avisos(whatsapp_service.logger) as registro:
            pagina = self._enviar_raio_x(self.cliente_http, ("jan.csv", JANEIRO))
            self.assertEqual(pagina.status_code, 303)
            self.assertIn("Raio-X concluído com 1 relatório(s) de vendas.", self._flashes(self.cliente_http))
            api = self._enviar_raio_x(self.cliente_http, ("jan.csv", JANEIRO), ("fev.csv", FEVEREIRO), api=True)
        self.assertEqual(api.status_code, 201)
        self.assertEqual(self._raio_x_gravado().arquivos, 2)
        self.assertEqual(self.abrir.call_count, 2)
        log = "\n".join(registro.output)
        self.assertEqual(log.count("Evolution API indisponível"), 2, log)
        self.assertNotIn(NUMERO_EMPRESA, log)

    def test_falha_ao_montar_o_notificador_nao_derruba_o_raio_x(self):
        """onboarding._disparar_aviso_de_raio_x protege a fábrica como admin._avisar_por_whatsapp e
        leads._avisar_por_whatsapp: se ela levantar, a rota responde como sempre, com o Raio-X já
        gravado, e só fica um aviso no log (antes da proteção, a rota respondia 500)."""
        self._telefone()
        self.fabrica.side_effect = RuntimeError("PRIVATE_SENTINEL")
        # Como em produção: a exceção vira resposta de erro, em vez de subir para o teste.
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        resposta = self._enviar_raio_x(self.cliente_http, ("jan.csv", JANEIRO))
        self.assertEqual(self._raio_x_confirmados(), 1)
        self.assertEqual(resposta.status_code, 303)
        self.assertIn("Raio-X concluído com 1 relatório(s) de vendas.", self._flashes(self.cliente_http))


class NotificadorDesligadoTests(_PortalCase):
    """Sem troca da fábrica: EVOLUTION_* vazias nos testes, o envio fica desligado e a rede intocada."""

    whatsapp_ligado = False

    def test_rotas_do_portal_seguem_iguais_e_a_rede_nunca_e_chamada(self):
        self._telefone()
        id_chamado = self._chamado()
        self._em_analise(self.analise_id)
        cliente = self._entrar(CLIENTE)
        with self._avisos(whatsapp_service.logger) as registro:
            respostas = {
                "chamado respondido": self._no_chamado(id_chamado, acao="responder", mensagem="Resposta fictícia."),
                "status alterado": self._no_chamado(id_chamado, acao="alterar_status", status_chamado="RESOLVIDO"),
                "análise nova": self._criar_analise(mes=10, ano=2026),
                "análise publicada": self._publicar(self.analise_id),
                "Raio-X pela página": self._enviar_raio_x(cliente, ("jan.csv", JANEIRO)),
                "Raio-X pela API": self._enviar_raio_x(cliente, ("fev.csv", FEVEREIRO), api=True),
            }
        self.assertEqual({rota: resposta.status_code for rota, resposta in respostas.items()}, {
            "chamado respondido": 302, "status alterado": 302, "análise nova": 302, "análise publicada": 302,
            "Raio-X pela página": 303, "Raio-X pela API": 201})
        # O que cada rota grava continua gravado.
        self.assertEqual((self._status_confirmado(id_chamado), self._respostas_da_equipe(id_chamado)), ("RESOLVIDO", 1))
        self.assertEqual(self._analises_confirmadas(10, 2026), 1)
        self.assertEqual(self._publicada(self.analise_id), ("CONCLUIDO", True))
        self.assertEqual(self._raio_x_gravado().periodo_inicio, date(2026, 2, 1))
        # Os seis gatilhos chegaram à fábrica real, que estava desligada: nenhuma chamada de rede.
        self.assertEqual(self.fabrica.call_count, 6)
        self.assertEqual(self.abrir.call_count, 0)
        self._sem_mensagem()
        desligado = [linha for linha in registro.output if "WhatsApp desligado" in linha]
        self.assertEqual(len(desligado), 6, registro.output)
        for linha in desligado:
            self.assertIn("EVOLUTION_API_URL, EVOLUTION_API_KEY, EVOLUTION_INSTANCE_NAME", linha)
            self.assertIn("***7766", linha)
            self.assertNotIn(NUMERO_EMPRESA, linha)


# =====================================================================
# Avaliação de Cenário (lead), pela tela do consultor
# =====================================================================
class _LeadsCase(_WhatsAppMixin, DeployTestCase):
    """O molde de tests/test_avaliacao_cenario.RotasTests: o motor no próprio processo, o consultor logado."""

    def setUp(self):
        super().setUp()
        self.fixtures()
        self._preparar_whatsapp()
        self.stack.enter_context(patch("etl_runtime.run_avaliacao_cenario", side_effect=avaliar_como_o_worker))
        self.admin_http = self._entrar(ADMIN)

    def _avaliar(self, *arquivos, whatsapp=WHATSAPP_LEAD, email="lead@example.invalid", aceitou_aviso=True):
        dados = {
            "csrf_token": self._csrf(self.admin_http, PAGINA_LEADS), "nome_contato": "Contato fictício",
            "nome_loja": "Mercado fictício", "email": email, "whatsapp": whatsapp, "autorizacao": "1",
            "arquivos": [(io.BytesIO(conteudo), nome) for nome, conteudo in arquivos]}
        if aceitou_aviso:
            dados["aviso_whatsapp"] = "1"
        return self.admin_http.post(PAGINA_LEADS, data=dados, content_type="multipart/form-data")

    def _reenviar(self, lead_id, *arquivos, aceitou_aviso=True):
        detalhe = f"{PAGINA_LEADS}/{lead_id}"
        dados = {
            "csrf_token": self._csrf(self.admin_http, detalhe), "autorizacao": "1",
            "arquivos": [(io.BytesIO(conteudo), nome) for nome, conteudo in arquivos]}
        if aceitou_aviso:
            dados["aviso_whatsapp"] = "1"
        return self.admin_http.post(f"{detalhe}/reenviar", data=dados, content_type="multipart/form-data")

    def _retriar(self, lead_id):
        detalhe = f"{PAGINA_LEADS}/{lead_id}"
        return self.admin_http.post(f"{detalhe}/retriar", data={"csrf_token": self._csrf(self.admin_http, detalhe)})

    def _lead(self):
        from models import LeadAvaliacao
        self.db.session.expire_all()
        return self.db.session.scalar(select(LeadAvaliacao))

    def _leads_confirmados(self):
        from models import LeadAvaliacao
        return self._confirmado(select(func.count()).select_from(LeadAvaliacao))


class LeadTests(_LeadsCase):
    """Avaliação concluída (avaliar e reenviar): relatório pronto pelo WhatsApp do lead, sem portal."""

    def test_sem_a_confirmacao_do_consultor_o_lead_nao_recebe_whatsapp(self):
        # A autorização de uso dos relatórios não cobre o contato por WhatsApp: sem a caixa
        # "aviso_whatsapp", nada sai, nem na avaliação nem no reenvio, mesmo com o WhatsApp cadastrado.
        self.assertEqual(self._avaliar(*_arquivos(LOTE), aceitou_aviso=False).status_code, 303)
        lead = self._lead()
        self.assertEqual(lead.whatsapp, "11900000000")
        self.assertEqual(self._reenviar(lead.id, ("jan.csv", JAN25), ("fev.csv", FEV25),
                                        aceitou_aviso=False).status_code, 303)
        self._sem_mensagem()
        # Com a confirmação no reenvio, o aviso sai.
        self.assertEqual(self._reenviar(lead.id, ("jan.csv", JAN25), ("fev.csv", FEV25)).status_code, 303)
        self._mensagem(NUMERO_LEAD)

    def test_a_caixa_de_confirmacao_aparece_desmarcada_nos_dois_formularios(self):
        lista = self.admin_http.get(PAGINA_LEADS).get_data(as_text=True)
        caixa = re.search(r'<input[^>]*id="leadAvisoWhatsapp"[^>]*>', lista).group(0)
        self.assertIn('name="aviso_whatsapp"', caixa)
        self.assertNotIn("checked", caixa)
        self.assertNotIn("required", caixa)
        self.assertEqual(self._avaliar(*_arquivos(LOTE), aceitou_aviso=False).status_code, 303)
        detalhe = self.admin_http.get(f"{PAGINA_LEADS}/{self._lead().id}").get_data(as_text=True)
        caixa = re.search(r'<input[^>]*id="reenvioAvisoWhatsapp"[^>]*>', detalhe).group(0)
        self.assertNotIn("checked", caixa)
        self.assertNotIn("required", caixa)

    def test_avaliacao_concluida_avisa_o_whatsapp_do_lead(self):
        self.ao_disparar = self._leads_confirmados
        resposta = self._avaliar(*_arquivos(LOTE))
        self.assertEqual(resposta.status_code, 303, resposta.get_data(as_text=True)[:300])
        lead = self._lead()
        self.assertEqual(resposta.headers["Location"], f"{PAGINA_LEADS}/{lead.id}")
        self.assertIn("Avaliação concluída: 4 relatório(s) de vendas.", self._flashes(self.admin_http))
        self.assertEqual(lead.whatsapp, "11900000000")
        valor = lead.vazamento_total_reais
        self.assertGreater(valor, 0)
        pedido = self._mensagem(NUMERO_LEAD)
        for trecho in ("Contato fictício", "O relatório de Mercado fictício está pronto",
                       f"Encontramos {_reais(valor)} em potencial recuperável.",
                       "A equipe NEXO vai entrar em contato"):
            self.assertIn(trecho, pedido["texto"])
        # O lead não tem acesso ao portal.
        self.assertNotIn("Acesse o portal", pedido["texto"])
        # Depois do commit e fora da vaga única de upload.
        self.assertEqual(pedido["no_banco"], 1)
        self.assertTrue(pedido["vaga_livre"])

    def test_lead_sem_potencial_recuperavel_recebe_o_aviso_sem_valor(self):
        self.assertEqual(self._avaliar(("mar.csv", SEM_VAZAMENTO)).status_code, 303)
        lead = self._lead()
        self.assertEqual(lead.vazamento_total_reais, 0)
        texto = self._mensagem(NUMERO_LEAD)["texto"]
        self.assertIn("O relatório de Mercado fictício está pronto", texto)
        self.assertIn("A equipe NEXO vai entrar em contato", texto)
        self.assertNotIn("Encontramos", texto)
        self.assertNotIn("R$", texto)

    def test_whatsapp_com_ddi_digitado_sai_no_mesmo_numero(self):
        self.assertEqual(self._avaliar(*_arquivos(LOTE), whatsapp="+55 (11) 90000-0000").status_code, 303)
        self.assertEqual(self._lead().whatsapp, "+5511900000000")
        self._mensagem(NUMERO_LEAD)

    def test_lead_sem_whatsapp_nao_dispara(self):
        resposta = self._avaliar(*_arquivos(LOTE), whatsapp="")
        self.assertEqual(resposta.status_code, 303)
        self.assertEqual(self._leads_confirmados(), 1)
        self.assertIsNone(self._lead().whatsapp)
        self._sem_mensagem()
        self.assertEqual(self.fabrica.call_count, 0)

    def test_reenvio_avisa_e_retriagem_nao(self):
        self._avaliar(*_arquivos(LOTE))
        lead_id = self._lead().id
        self._mensagem(NUMERO_LEAD)
        self.ao_disparar = self._leads_confirmados
        reenvio = self._reenviar(lead_id, ("jan.csv", JAN25), ("fev.csv", FEV25))
        self.assertEqual(reenvio.status_code, 303)
        self.assertIn("Avaliação refeita com 2 relatório(s) de vendas.", self._flashes(self.admin_http))
        lead = self._lead()
        self.assertEqual((lead.id, lead.arquivos), (lead_id, 2))
        self.assertGreater(lead.vazamento_total_reais, 0)
        pedido = self._mensagem(NUMERO_LEAD)
        # O contato é o gravado no primeiro envio; o valor, o da avaliação refeita.
        for trecho in ("Contato fictício", "O relatório de Mercado fictício está pronto",
                       f"Encontramos {_reais(lead.vazamento_total_reais)} em potencial recuperável."):
            self.assertIn(trecho, pedido["texto"])
        self.assertEqual(pedido["no_banco"], 1)
        self.assertTrue(pedido["vaga_livre"])
        # Retriagem: o consultor ajusta a triagem dos mesmos dados; não é relatório novo.
        retriagem = self._retriar(lead_id)
        self.assertEqual(retriagem.status_code, 303)
        self.assertIn("Triagem refeita com as regras atuais.", self._flashes(self.admin_http))
        self._sem_mensagem()
        self.assertEqual(self.fabrica.call_count, 2)

    def test_avaliacao_recusada_nao_dispara(self):
        recusada = self._avaliar(("sem_periodo.csv", b"Codigo;Produto;Qtde;Valor Total\n1;Cafe;1;10,00\n"))
        self.assertEqual(recusada.status_code, 422)
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            ocupada = self._avaliar(*_arquivos(LOTE))
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()
        self.assertEqual(ocupada.status_code, 503)
        self.assertEqual(self._leads_confirmados(), 0)
        self._sem_mensagem()
        self.assertEqual(self.fabrica.call_count, 0)

    def test_api_fora_do_ar_nao_muda_a_avaliacao(self):
        self.abrir.side_effect = urllib.error.URLError("rede fictícia fora do ar")
        with self._avisos(whatsapp_service.logger) as registro:
            resposta = self._avaliar(*_arquivos(LOTE))
        self.assertEqual(resposta.status_code, 303)
        self.assertIn("Avaliação concluída: 4 relatório(s) de vendas.", self._flashes(self.admin_http))
        self.assertEqual(self._leads_confirmados(), 1)
        self.assertEqual(self.abrir.call_count, 1)
        log = "\n".join(registro.output)
        self.assertIn("Evolution API indisponível", log)
        self.assertIn("***0000", log)
        self.assertNotIn(NUMERO_LEAD, log)

    def test_falha_ao_montar_o_notificador_nao_muda_a_avaliacao(self):
        self.fabrica.side_effect = RuntimeError("PRIVATE_SENTINEL")
        with self._avisos(self.app.logger) as registro:
            resposta = self._avaliar(*_arquivos(LOTE))
        self.assertEqual(resposta.status_code, 303)
        self.assertIn("Avaliação concluída: 4 relatório(s) de vendas.", self._flashes(self.admin_http))
        self.assertEqual(self._leads_confirmados(), 1)
        self._sem_mensagem()
        log = "\n".join(registro.output)
        self.assertIn("Aviso por WhatsApp do lead não disparado (RuntimeError).", log)
        self.assertNotIn("PRIVATE_SENTINEL", log)


class LeadNotificadorDesligadoTests(_LeadsCase):
    """Sem troca da fábrica: a avaliação e o reenvio seguem iguais, sem nenhuma chamada de rede."""

    whatsapp_ligado = False

    def test_avaliacao_e_reenvio_seguem_iguais_e_a_rede_nunca_e_chamada(self):
        with self._avisos(whatsapp_service.logger) as registro:
            avaliacao = self._avaliar(*_arquivos(LOTE))
            lead_id = self._lead().id
            reenvio = self._reenviar(lead_id, ("jan.csv", JAN25), ("fev.csv", FEV25))
        self.assertEqual((avaliacao.status_code, reenvio.status_code), (303, 303))
        self.assertEqual((self._leads_confirmados(), self._lead().arquivos), (1, 2))
        self.assertEqual(self.fabrica.call_count, 2)
        self.assertEqual(self.abrir.call_count, 0)
        self._sem_mensagem()
        self.assertEqual(sum("WhatsApp desligado" in linha for linha in registro.output), 2, registro.output)


if __name__ == "__main__":
    unittest.main()
