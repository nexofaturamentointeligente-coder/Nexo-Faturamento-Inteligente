"""Onda 3: portal, admin e qualidade.

  1. Chamados: mensagem de até 5.000 caracteres depois de normalizar as quebras de linha, com o mesmo maxlength
     nos formulários; corpo de até 128 KiB, recusado antes da leitura do formulário (inclusive do token CSRF);
     avisos aos admins agrupados em 10 minutos; e o clique duplo (120 s na abertura, 60 s na resposta sem mensagem
     de outro participante no meio), com a empresa bloqueada antes da conferência.
  2. Limites com o Flask-Limiter ligado: 30 respostas por hora para o cliente, nenhum teto para o admin; a tela do
     429 explica a rede compartilhada quando o limite é contado pelo endereço, e só nesse caso.
  3. Admin: análise repetida (mesma empresa, competência, tipo e quinzena) recusada no código, com a empresa
     bloqueada; ids, status, faturamento e campos obrigatórios do cadastro e da edição de empresa conferidos, com
     o erro no formulário (nunca 500).

Dados fictícios.
"""
from datetime import date, timedelta
from decimal import Decimal
import html
import re
import unittest
from unittest.mock import patch

from sqlalchemy import func, select, update

import chamados_suporte
from tests.deploy_support import DeployTestCase
from . import test_auth_tokens_security as token_cases
from . import test_session_revocation_security as session_cases

CLIENTE = "cliente-test@example.invalid"
ADMIN = "admin-test@example.invalid"
REDE_COMPARTILHADA = "outras pessoas da mesma rede"
NOVO_CHAMADO = "Novo chamado de Teste 1: “Dúvida fictícia”."
NOVA_RESPOSTA = "Nova resposta de Teste 1 no chamado “Dúvida fictícia”."
RESPOSTA_DA_EQUIPE = "A equipe NEXO respondeu seu chamado “Dúvida fictícia”."


class _PortalCase(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        self.cli = self._entrar(CLIENTE)
        self.adm = self._entrar(ADMIN)
        # A cópia por e-mail do chamado novo e o WhatsApp da resposta da equipe: contados, sem rede.
        self.copias = self.stack.enter_context(patch("configuracoes_conta.avisar_novo_chamado"))
        self.whatsapp = self.stack.enter_context(patch("blueprints.admin._whatsapp_chamado_atualizado"))

    # ------------------------------------------------------------ apoio HTTP
    def _csrf(self, http, caminho):
        resposta = http.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', resposta.get_data(as_text=True)).group(1)

    def _entrar(self, email):
        http = self.app.test_client()
        resposta = http.post("/auth/login", data={"email": email, "senha": self.password,
                                                  "csrf_token": self._csrf(http, "/auth/login")})
        self.assertEqual(resposta.status_code, 302)
        return http

    @staticmethod
    def _flashes(http):
        with http.session_transaction() as sessao:
            return [str(mensagem) for _categoria, mensagem in sessao.get("_flashes", ())]

    def _abrir(self, assunto="Dúvida fictícia", mensagem="Mensagem fictícia do cliente.", categoria="FINANCEIRO"):
        return self.cli.post("/cliente/tickets", data={
            "csrf_token": self._csrf(self.cli, "/cliente/tickets"), "assunto": assunto, "categoria": categoria,
            "mensagem": mensagem})

    def _conversa(self, resposta):
        self.assertEqual(resposta.status_code, 302)
        return int(re.search(r"/cliente/tickets/(\d+)$", resposta.headers["Location"]).group(1))

    def _responder_cliente(self, id_chamado, mensagem):
        caminho = f"/cliente/tickets/{id_chamado}"
        return self.cli.post(caminho, data={"csrf_token": self._csrf(self.cli, caminho), "mensagem": mensagem})

    def _responder_admin(self, id_chamado, mensagem):
        caminho = f"/admin/tickets/{id_chamado}"
        return self.adm.post(caminho, data={"csrf_token": self._csrf(self.adm, caminho), "acao": "responder",
                                            "mensagem": mensagem})

    # --------------------------------------------------------------- estado
    def _chamados(self):
        from models import ChamadoSuporte
        self.db.session.expire_all()
        return self.db.session.scalars(select(ChamadoSuporte.id_chamado).order_by(ChamadoSuporte.id_chamado)).all()

    def _mensagens(self, id_chamado):
        from models import MensagemSuporte
        self.db.session.expire_all()
        return [(m.id_mensagem, m.id_usuario_remetente, m.mensagem) for m in self.db.session.scalars(
            select(MensagemSuporte).where(MensagemSuporte.id_chamado == id_chamado)
            .order_by(MensagemSuporte.id_mensagem))]

    def _textos(self, id_chamado):
        return [texto for _id, _autor, texto in self._mensagens(id_chamado)]

    def _avisos(self, id_usuario):
        from models import Notificacao
        self.db.session.expire_all()
        return [n.texto for n in self.db.session.scalars(
            select(Notificacao).where(Notificacao.id_usuario == id_usuario).order_by(Notificacao.id_notificacao))]

    def _envelhecer(self, modelo, coluna, ident, segundos):
        """Recua o registro no tempo: o clique duplo e o agrupamento comparam com o relógio do portal."""
        objeto = self.db.session.get(modelo, ident)
        setattr(objeto, coluna, getattr(objeto, coluna) - timedelta(seconds=segundos))
        self.db.session.commit()

    def _empresa_do_cliente(self):
        from models import Usuario
        return self.db.session.get(Usuario, self.cliente_id).id_empresa


# =============================================================================
# 1. Chamados: tamanho da mensagem e do corpo
# =============================================================================
class TamanhoDaMensagemTests(_PortalCase):
    def test_ate_5000_caracteres_depois_de_normalizar_as_quebras(self):
        # 50 linhas de 99 letras e uma letra no fim: 5.049 caracteres com o CRLF que o navegador envia, 5.000
        # com a quebra contada como 1 (como no maxlength do formulário).
        texto = "\r\n".join(["a" * 99] * 50) + "b"
        self.assertEqual((len(texto), len(texto.replace("\r\n", "\n"))), (5049, 5000))
        id_chamado = self._conversa(self._abrir(mensagem=texto))
        self.assertEqual(self._responder_cliente(id_chamado, texto[:-1] + "c").status_code, 302)
        self.assertEqual(self._responder_admin(id_chamado, texto[:-1] + "d").status_code, 302)
        gravadas = self._textos(id_chamado)
        self.assertEqual([len(mensagem) for mensagem in gravadas], [5000, 5000, 5000])
        # Gravada já normalizada: sem o CR do navegador.
        self.assertFalse(any("\r" in mensagem for mensagem in gravadas))

    def test_acima_de_5000_e_recusada_sem_gravar(self):
        longa = "x" * 5001
        self.assertEqual(self._abrir(mensagem=longa).status_code, 302)
        self.assertIn(chamados_suporte.MENSAGEM_LONGA, self._flashes(self.cli))
        self.assertEqual(self._chamados(), [])
        id_chamado = self._conversa(self._abrir())
        for responder, http in ((self._responder_cliente, self.cli), (self._responder_admin, self.adm)):
            with self.subTest(autor="cliente" if http is self.cli else "admin"):
                self.assertEqual(responder(id_chamado, longa).status_code, 302)
                self.assertIn(chamados_suporte.MENSAGEM_LONGA, self._flashes(http))
        self.assertEqual(len(self._mensagens(id_chamado)), 1)
        self.assertEqual(self._avisos(self.admin_id), [NOVO_CHAMADO])
        # Os espaços e a quebra das pontas não contam: 5.000 letras passam.
        self.assertEqual(self._responder_cliente(id_chamado, "  " + "y" * 5000 + "\r\n").status_code, 302)
        self.assertEqual(self._textos(id_chamado)[-1], "y" * 5000)

    def test_formularios_com_o_mesmo_maxlength(self):
        id_chamado = self._conversa(self._abrir())
        for http, caminho in ((self.cli, "/cliente/tickets"), (self.cli, f"/cliente/tickets/{id_chamado}"),
                              (self.adm, f"/admin/tickets/{id_chamado}")):
            with self.subTest(caminho=caminho):
                pagina = http.get(caminho).get_data(as_text=True)
                (campo,) = re.findall(r'<textarea[^>]*name="mensagem"[^>]*>', pagina)
                self.assertIn('maxlength="5000"', campo)


class TetoDoCorpoTests(_PortalCase):
    def test_corpo_acima_de_128_kib_e_recusado_antes_do_formulario(self):
        id_chamado = self._conversa(self._abrir())
        grande = "z" * (128 * 1024)  # com os outros campos, o corpo passa de 128 KiB
        for http, caminho, extras in ((self.cli, "/cliente/tickets", {"assunto": "Assunto", "categoria": "FINANCEIRO"}),
                                      (self.cli, f"/cliente/tickets/{id_chamado}", {}),
                                      (self.adm, f"/admin/tickets/{id_chamado}", {"acao": "responder"})):
            with self.subTest(caminho=caminho):
                # Sem o token CSRF: o 413 vem antes da leitura do formulário (senão seria o 400 do CSRF).
                resposta = http.post(caminho, data={"mensagem": grande, **extras})
                self.assertEqual(resposta.status_code, 413)
                corpo = html.unescape(resposta.get_data(as_text=True))
                self.assertIn(chamados_suporte.MENSAGEM_CORPO_GRANDE, corpo)
                self.assertNotIn("z" * 100, corpo)
        self.assertEqual(len(self._chamados()), 1)
        self.assertEqual(len(self._mensagens(id_chamado)), 1)

    def test_abaixo_do_teto_segue_para_a_regra_da_mensagem(self):
        id_chamado = self._conversa(self._abrir())
        resposta = self._responder_cliente(id_chamado, "z" * (100 * 1024))
        self.assertEqual(resposta.status_code, 302)
        self.assertIn(chamados_suporte.MENSAGEM_LONGA, self._flashes(self.cli))

    def test_as_outras_rotas_seguem_com_o_teto_geral(self):
        anonimo = self.app.test_client()
        resposta = anonimo.post("/auth/login", data={
            "email": "pessoa@example.invalid", "senha": "s" * (200 * 1024),
            "csrf_token": self._csrf(anonimo, "/auth/login")})
        self.assertNotEqual(resposta.status_code, 413)


# =============================================================================
# 2. Chamados: clique duplo e avisos agrupados
# =============================================================================
class CliqueDuploNaAberturaTests(_PortalCase):
    def test_o_mesmo_chamado_em_ate_120_s_nao_duplica(self):
        from blueprints import cliente as rotas_cliente
        with patch.object(rotas_cliente, "bloquear_empresa", wraps=rotas_cliente.bloquear_empresa) as trava:
            primeiro = self._conversa(self._abrir())
            # Espaços e a quebra do navegador não mudam o conteúdo.
            segundo = self._conversa(self._abrir(assunto="Dúvida  fictícia ", mensagem="Mensagem fictícia do cliente.\r\n"))
        self.assertEqual(segundo, primeiro)
        self.assertIn(chamados_suporte.MENSAGEM_CHAMADO_REPETIDO, self._flashes(self.cli))
        # A empresa foi bloqueada antes da conferência, nos dois pedidos.
        self.assertEqual([chamada.args[1] for chamada in trava.call_args_list], [self._empresa_do_cliente()] * 2)
        self.assertEqual(self._chamados(), [primeiro])
        self.assertEqual(self._textos(primeiro), ["Mensagem fictícia do cliente."])
        self.assertEqual(self._avisos(self.admin_id), [NOVO_CHAMADO])
        self.assertEqual(self.copias.call_count, 1)

    def test_outro_conteudo_chamado_resolvido_ou_prazo_vencido_abrem_outro(self):
        from models import ChamadoSuporte
        primeiro = self._conversa(self._abrir())
        outros = [self._conversa(self._abrir(mensagem="Outra mensagem fictícia.")),
                  self._conversa(self._abrir(assunto="Outro assunto fictício")),
                  self._conversa(self._abrir(categoria="DUVIDA_TECNICA"))]
        self.assertEqual(len({primeiro, *outros}), 4)
        # O mesmo conteúdo, com o anterior já resolvido: outro chamado.
        self.db.session.get(ChamadoSuporte, primeiro).status_chamado = "RESOLVIDO"
        self.db.session.commit()
        depois_de_resolver = self._conversa(self._abrir())
        self.assertNotEqual(depois_de_resolver, primeiro)
        # E passado o prazo de 120 s: outro chamado.
        self._envelhecer(ChamadoSuporte, "data_abertura", depois_de_resolver, 121)
        self.assertNotIn(self._conversa(self._abrir()), {primeiro, depois_de_resolver})
        self.assertEqual(len(self._chamados()), 6)
        self.assertEqual(self.copias.call_count, 6)


class CliqueDuploNaRespostaTests(_PortalCase):
    def test_a_mesma_mensagem_em_ate_60_s_sem_outra_no_meio_nao_duplica(self):
        from blueprints import cliente as rotas_cliente
        from models import MensagemSuporte
        id_chamado = self._conversa(self._abrir())
        with patch.object(rotas_cliente, "bloquear_empresa", wraps=rotas_cliente.bloquear_empresa) as trava:
            for texto in ("Mensagem repetida.", "  Mensagem repetida.\r\n"):
                self.assertEqual(self._responder_cliente(id_chamado, texto).status_code, 302)
        self.assertIn(chamados_suporte.MENSAGEM_RESPOSTA_REPETIDA, self._flashes(self.cli))
        self.assertEqual([chamada.args[1] for chamada in trava.call_args_list], [self._empresa_do_cliente()] * 2)
        self.assertEqual(self._textos(id_chamado), ["Mensagem fictícia do cliente.", "Mensagem repetida."])
        # Com a mensagem da equipe no meio, a mesma mensagem do cliente vale.
        self._responder_admin(id_chamado, "Resposta fictícia.")
        self._responder_cliente(id_chamado, "Mensagem repetida.")
        # E passado o prazo de 60 s, também.
        self._envelhecer(MensagemSuporte, "data_envio", self._mensagens(id_chamado)[-1][0], 61)
        self._responder_cliente(id_chamado, "Mensagem repetida.")
        self.assertEqual(self._textos(id_chamado), ["Mensagem fictícia do cliente.", "Mensagem repetida.",
                                                    "Resposta fictícia.", "Mensagem repetida.", "Mensagem repetida."])

    def test_clique_duplo_da_equipe_avisa_o_cliente_uma_vez(self):
        from blueprints import admin as rotas_admin
        id_chamado = self._conversa(self._abrir())
        with patch.object(rotas_admin, "bloquear_empresa", wraps=rotas_admin.bloquear_empresa) as trava:
            for _ in range(2):
                self.assertEqual(self._responder_admin(id_chamado, "Resposta fictícia.").status_code, 302)
        self.assertIn(chamados_suporte.MENSAGEM_RESPOSTA_REPETIDA, self._flashes(self.adm))
        self.assertEqual([chamada.args[1] for chamada in trava.call_args_list], [self._empresa_do_cliente()] * 2)
        self.assertEqual(self._textos(id_chamado), ["Mensagem fictícia do cliente.", "Resposta fictícia."])
        self.assertEqual(self._avisos(self.cliente_id), [RESPOSTA_DA_EQUIPE])
        self.assertEqual(self.whatsapp.call_count, 1)


class AvisosAgrupadosTests(_PortalCase):
    def test_respostas_do_cliente_em_10_minutos_viram_um_aviso_por_admin(self):
        from models import Notificacao, Usuario
        outro = Usuario(nome="Outro admin fictício", email="admin2-test@example.invalid", role="ADMIN",
                        primeiro_acesso=False)
        outro.set_senha(self.password)
        self.db.session.add(outro)
        self.db.session.commit()
        outro_id = outro.id_usuario
        id_chamado = self._conversa(self._abrir())
        for numero in range(1, 4):
            self.assertEqual(self._responder_cliente(id_chamado, f"Mensagem fictícia {numero}.").status_code, 302)
        # As mensagens ficam todas; o aviso, um só por admin.
        self.assertEqual(len(self._mensagens(id_chamado)), 4)
        for admin in (self.admin_id, outro_id):
            self.assertEqual(self._avisos(admin), [NOVO_CHAMADO, NOVA_RESPOSTA])
        # O outro admin leu os avisos: a resposta seguinte avisa só a ele.
        self.db.session.execute(update(Notificacao).where(Notificacao.id_usuario == outro_id).values(lida=True))
        self.db.session.commit()
        self._responder_cliente(id_chamado, "Mensagem fictícia 4.")
        self.assertEqual(self._avisos(self.admin_id), [NOVO_CHAMADO, NOVA_RESPOSTA])
        self.assertEqual(self._avisos(outro_id), [NOVO_CHAMADO, NOVA_RESPOSTA, NOVA_RESPOSTA])
        # Passados 10 minutos do aviso ainda não lido, outro aviso.
        ultimo = self.db.session.scalar(select(func.max(Notificacao.id_notificacao)).where(
            Notificacao.id_usuario == self.admin_id))
        self._envelhecer(Notificacao, "data_criacao", ultimo, 601)
        self._responder_cliente(id_chamado, "Mensagem fictícia 5.")
        self.assertEqual(self._avisos(self.admin_id), [NOVO_CHAMADO, NOVA_RESPOSTA, NOVA_RESPOSTA])
        self.assertEqual(len(self._mensagens(id_chamado)), 6)

    def test_respostas_da_equipe_seguem_um_aviso_por_resposta(self):
        id_chamado = self._conversa(self._abrir())
        for numero in range(1, 3):
            self._responder_admin(id_chamado, f"Resposta fictícia {numero}.")
        self.assertEqual(self._avisos(self.cliente_id), [RESPOSTA_DA_EQUIPE] * 2)


# =============================================================================
# 3. Limites: respostas do cliente e a tela do 429
# =============================================================================
class LimitesDoSuporteTests(token_cases._TokenCase):
    ENABLE_LIMITS = True  # o Flask-Limiter só instala os limites se nasce ligado
    _login_client = session_cases.SessionRevocationCases._login_client

    def _chamado(self):
        from extensions import db
        from models import ChamadoSuporte, MensagemSuporte, Usuario
        with self.app.app_context():
            empresa = db.session.get(Usuario, self.ids["active"]).id_empresa
            chamado = ChamadoSuporte(id_empresa=empresa, id_usuario_cliente=self.ids["active"],
                                     assunto="Dúvida fictícia", categoria="FINANCEIRO", status_chamado="ABERTO")
            db.session.add(chamado)
            db.session.flush()
            db.session.add(MensagemSuporte(id_chamado=chamado.id_chamado, id_usuario_remetente=self.ids["active"],
                                           mensagem="Mensagem fictícia."))
            db.session.commit()
            return chamado.id_chamado

    def test_cliente_tem_30_respostas_por_hora_e_a_equipe_nenhum_teto(self):
        id_chamado = self._chamado()
        cliente = self._login_client("active")
        caminho = f"/cliente/tickets/{id_chamado}"
        token = self._csrf(cliente, caminho)
        codigos = [cliente.post(caminho, data={"csrf_token": token, "mensagem": f"Mensagem fictícia {numero}."})
                   .status_code for numero in range(31)]
        self.assertEqual(codigos, [302] * 30 + [429])
        # Limite da conta, não da rede: a tela não fala de rede compartilhada.
        recusa = cliente.post(caminho, data={"csrf_token": token, "mensagem": "Mais uma."})
        self.assertEqual(recusa.status_code, 429)
        self.assertNotIn(REDE_COMPARTILHADA, html.unescape(recusa.get_data(as_text=True)))
        admin = self._login_client("admin")
        caminho_admin = f"/admin/tickets/{id_chamado}"
        token = self._csrf(admin, caminho_admin)
        codigos = [admin.post(caminho_admin, data={"csrf_token": token, "acao": "responder",
                                                   "mensagem": f"Resposta fictícia {numero}."}).status_code
                   for numero in range(35)]
        self.assertEqual(codigos, [302] * 35)

    def test_429_pelo_endereco_explica_a_rede_compartilhada(self):
        csrf = self._csrf()
        # E-mails distintos: estoura o limite por IP, não o da conta.
        respostas = [self.client.post("/auth/login", data={
            "email": f"pessoa{numero}@example.invalid", "senha": "senha-errada-ficticia", "csrf_token": csrf})
            for numero in range(11)]
        self.assertEqual([resposta.status_code for resposta in respostas], [200] * 10 + [429])
        texto = html.unescape(respostas[-1].get_data(as_text=True))
        self.assertIn(REDE_COMPARTILHADA, texto)
        self.assertRegex(texto, r"Aguarde (um minuto|\d+ minutos) e tente novamente\.")

    def test_429_pela_conta_nao_fala_de_rede(self):
        self.app.config["LIMITE_LOGIN_CONTA"] = "3 per minute"
        csrf = self._csrf()
        respostas = [self.client.post("/auth/login", data={
            "email": "alvo@example.invalid", "senha": "senha-errada-ficticia", "csrf_token": csrf}) for _ in range(4)]
        self.assertEqual([resposta.status_code for resposta in respostas], [200] * 3 + [429])
        self.assertNotIn(REDE_COMPARTILHADA, html.unescape(respostas[-1].get_data(as_text=True)))


# =============================================================================
# 4. Admin: análise repetida e cadastro/edição de empresa
# =============================================================================
class _AdminCase(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        self.login(ADMIN)
        from models import Analise, Empresa, Plano, Segmento
        self.empresa_1, self.empresa_2 = self.db.session.scalars(
            select(Empresa.id_empresa).order_by(Empresa.id_empresa)).all()
        self.plano = self.db.session.scalar(select(Plano.id_plano))
        self.segmento = self.db.session.scalar(select(Segmento.id_segmento))
        self.Analise, self.Empresa = Analise, Empresa

    def _flashes(self):
        with self.client.session_transaction() as sessao:
            return [str(mensagem) for _categoria, mensagem in sessao.get("_flashes", ())]

    def _contar(self, modelo):
        self.db.session.expire_all()
        return self.db.session.scalar(select(func.count()).select_from(modelo))


class AnaliseRepetidaTests(_AdminCase):
    def _nova(self, id_empresa=None, tipo="MENSAL", mes=1, ano=2026, quinzena=None):
        inicio = date(ano, mes, 16 if quinzena == 2 else 1)
        fim = date(ano, mes, 15 if quinzena == 1 else 28)
        dados = {"csrf_token": self.csrf_token("/admin/analises/nova"), "id_empresa": str(id_empresa or self.empresa_1),
                 "tipo_analise": tipo, "periodo_inicio": inicio.isoformat(), "periodo_fim": fim.isoformat(),
                 "mes_referencia": str(mes), "ano_referencia": str(ano)}
        if quinzena is not None:
            dados["quinzena_referencia"] = str(quinzena)
        return self.client.post("/admin/analises/nova", data=dados)

    def _destino(self, resposta):
        self.assertEqual(resposta.status_code, 302)
        achado = re.search(r"/admin/analise/(\d+)/upload$", resposta.headers["Location"])
        self.assertIsNotNone(achado, resposta.headers["Location"])
        return int(achado.group(1))

    def test_mesma_empresa_competencia_e_tipo_nao_duplica(self):
        from blueprints import admin as rotas_admin
        total = self._contar(self.Analise)
        # A da base: MENSAL de 01/2026 da empresa 1.
        with patch.object(rotas_admin, "bloquear_empresa", wraps=rotas_admin.bloquear_empresa) as trava:
            self.assertEqual(self._destino(self._nova()), self.analise_id)
        self.assertTrue(any(f"#{self.analise_id}" in mensagem for mensagem in self._flashes()))
        self.assertEqual([chamada.args[1] for chamada in trava.call_args_list], [self.empresa_1])
        self.assertEqual(self._contar(self.Analise), total)
        # Outra competência cria uma; o clique duplo dela, não.
        criada = self._destino(self._nova(mes=2))
        self.assertEqual(self._destino(self._nova(mes=2)), criada)
        self.assertNotEqual(self._destino(self._nova(mes=2, ano=2025)), criada)  # outro ano, outra competência
        self.assertEqual(self._contar(self.Analise), total + 2)
        # A mesma competência em outra empresa: outra análise.
        self.assertNotEqual(self._destino(self._nova(id_empresa=self.empresa_2, mes=2)), criada)
        self.assertEqual(self._contar(self.Analise), total + 3)

    def test_quinzenas_e_tipos_distintos_sao_analises_distintas(self):
        from models import Plano
        ouro = Plano(nome_plano="OURO", valor_mensal=900, qtd_analises_mes=2, tipo_analise_permitida="QUINZENAL",
                     nivel_entrega_analise="PREMIUM", nivel_dashboard="COMPLETO", nivel_atendimento="ALTO")
        self.db.session.add(ouro)
        self.db.session.flush()
        self.db.session.get(self.Empresa, self.empresa_1).id_plano_atual = ouro.id_plano
        self.db.session.commit()
        total = self._contar(self.Analise)
        primeira = self._destino(self._nova(tipo="QUINZENAL", mes=3, quinzena=1))
        self.assertEqual(self._destino(self._nova(tipo="QUINZENAL", mes=3, quinzena=1)), primeira)
        segunda = self._destino(self._nova(tipo="QUINZENAL", mes=3, quinzena=2))
        mensal = self._destino(self._nova(mes=3))
        self.assertEqual(len({primeira, segunda, mensal}), 3)
        self.assertEqual(self._contar(self.Analise), total + 3)


class EmpresaComValoresAdulteradosTests(_AdminCase):
    ADULTERADOS = {
        "id_segmento": ("", "abc", "-1", "1.0", "１", "99999", "1" * 10, None),
        "id_plano_atual": ("", "x", "-3", "99999", "2147483648", None),
        "status_conta": ("XYZ", ""),
        "faturamento_base_mensal": ("abc", "nan", "inf", "-Infinity", "-5", "1e400", "1" * 40, "1,2,3"),
        "razao_social": ("  ", None),
    }

    def _cadastro(self, **troca):
        dados = {"cnpj": "11222333000144", "razao_social": "Empresa nova fictícia", "nome_fantasia": "Nova fictícia",
                 "email_contato": "nova@example.invalid", "telefone_contato": "", "data_contratacao": "2026-02-01",
                 "faturamento_base_mensal": "", "id_segmento": str(self.segmento), "id_plano_atual": str(self.plano),
                 "status_conta": "ATIVA", "usuario_nome": "Responsável fictício",
                 "usuario_email": "responsavel@example.invalid"}
        dados.update(troca)
        dados = {chave: valor for chave, valor in dados.items() if valor is not None}
        return self.client.post("/admin/empresas/nova",
                                data={"csrf_token": self.csrf_token("/admin/empresas/nova"), **dados})

    def test_cadastro_volta_ao_formulario_com_o_erro(self):
        total = self._contar(self.Empresa)
        casos = dict(self.ADULTERADOS, cnpj=("", None), usuario_nome=("", None))
        for campo, valores in casos.items():
            for valor in valores:
                with self.subTest(campo=campo, valor=valor):
                    resposta = self._cadastro(**{campo: valor})
                    self.assertEqual(resposta.status_code, 302)
                    self.assertTrue(resposta.headers["Location"].endswith("/admin/empresas/nova"))
                    self.assertEqual(len(self._flashes()), 1)
                    self.assertEqual(self._contar(self.Empresa), total)

    def test_cadastro_valido_com_os_formatos_do_faturamento(self):
        for numero, (digitado, esperado) in enumerate(
                (("", None), ("1500.50", Decimal("1500.50")), ("1.500,50", Decimal("1500.50")),
                 ("15000", Decimal("15000"))), 1):
            with self.subTest(digitado=digitado):
                resposta = self._cadastro(cnpj=f"1122233300014{numero}", email_contato=f"nova{numero}@example.invalid",
                                          usuario_email=f"responsavel{numero}@example.invalid",
                                          faturamento_base_mensal=digitado)
                self.assertEqual(resposta.status_code, 302)
                self.assertTrue(resposta.headers["Location"].endswith("/admin/empresas"))
                self.db.session.expire_all()
                empresa = self.db.session.scalar(select(self.Empresa).where(
                    self.Empresa.cnpj == f"1122233300014{numero}"))
                self.assertEqual(empresa.faturamento_base_mensal, esperado)

    def _edicao(self, **troca):
        caminho = f"/admin/empresas/{self.empresa_1}/editar"
        dados = {"razao_social": "Empresa fictícia 1", "nome_fantasia": "Teste 1",
                 "email_contato": "empresa1@example.invalid", "telefone_contato": "", "data_contratacao": "2026-01-01",
                 "faturamento_base_mensal": "", "id_segmento": str(self.segmento), "id_plano_atual": str(self.plano),
                 "status_conta": "ATIVA", "status_conta_exibido": "ATIVA"}
        dados.update(troca)
        dados = {chave: valor for chave, valor in dados.items() if valor is not None}
        return self.client.post(caminho, data={"csrf_token": self.csrf_token(caminho), **dados})

    def _retrato(self):
        self.db.session.expire_all()
        empresa = self.db.session.get(self.Empresa, self.empresa_1)
        return (empresa.id_segmento, empresa.id_plano_atual, empresa.razao_social, empresa.status_conta,
                empresa.faturamento_base_mensal, empresa.data_atualizacao)

    def test_edicao_volta_ao_formulario_sem_mudar_nada(self):
        antes = self._retrato()
        for campo, valores in self.ADULTERADOS.items():
            for valor in valores:
                with self.subTest(campo=campo, valor=valor):
                    resposta = self._edicao(**{campo: valor})
                    self.assertEqual(resposta.status_code, 302)
                    self.assertTrue(resposta.headers["Location"].endswith(f"/admin/empresas/{self.empresa_1}/editar"))
                    self.assertEqual(len(self._flashes()), 1)
                    self.assertEqual(self._retrato(), antes)
        # Controle: a mesma edição com valores válidos grava.
        resposta = self._edicao(razao_social="Empresa fictícia 1 editada", faturamento_base_mensal="2.000,00")
        self.assertTrue(resposta.headers["Location"].endswith("/admin/empresas"))
        self.assertEqual(self._retrato()[2:5:2], ("Empresa fictícia 1 editada", Decimal("2000.00")))

    def test_segmento_atual_inativo_continua_aceito_na_edicao(self):
        from models import Segmento
        self.db.session.get(Segmento, self.segmento).ativo = False
        outro = Segmento(nome_segmento="Segmento inativo fictício", ativo=False)
        self.db.session.add(outro)
        self.db.session.commit()
        recusa = self._edicao(id_segmento=str(outro.id_segmento))
        self.assertTrue(recusa.headers["Location"].endswith(f"/admin/empresas/{self.empresa_1}/editar"))
        aceita = self._edicao(razao_social="Empresa fictícia 1 com segmento antigo")
        self.assertTrue(aceita.headers["Location"].endswith("/admin/empresas"))
        self.assertEqual(self._retrato()[0], self.segmento)


if __name__ == "__main__":
    unittest.main()
