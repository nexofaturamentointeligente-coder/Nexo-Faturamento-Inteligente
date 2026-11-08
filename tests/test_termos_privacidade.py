"""Termos de Uso e Política de Privacidade: versões, aceite obrigatório, auditoria e alertas.

Os 3 cenários pedidos e as bordas, ponta a ponta pelas rotas, com migrações reais em
SQLite (DeployTestCase). Dados sintéticos (example.invalid); nenhum e-mail real sai, e os
IPs são de faixas reservadas para documentação (RFC 5737 e RFC 3849). O relógio é o da
política (NEXO_RELOGIO): T0 = 02/03/2026 13:00 UTC, 10:00 em Brasília.

Executar da raiz: python -B -m unittest tests.test_termos_privacidade -v
"""

import contextlib
from datetime import timedelta, timezone
import io
import json
import re
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import preferencias_usuario
import termos_legais
from tests.contratos_support import T0, TEXTO_PRIVADO
from tests.deploy_support import ROOT
from tests.test_configuracoes_conta import ADMIN, PAGINA_ADMIN, _ConfigCase

TERMOS = "Termos de Uso fictícios.\n\nCláusula sintética de teste."
POLITICA = "Política de Privacidade fictícia.\n\n<script>alert('politica')</script>"
IP_CLIENTE = "203.0.113.7"
CONFIRMACAO = "Li e concordo com os Termos de Uso e a Política de Privacidade"
PAGINAS_DO_CLIENTE = ("/cliente/dashboard", "/cliente/upload", "/cliente/historico", "/cliente/guia",
                      "/cliente/tickets", "/cliente/configuracoes")


class _TermosCase(_ConfigCase):
    def setUp(self):
        super().setUp()
        self.admin_http, _ = self._entrar(ADMIN)
        self.cliente_http, _ = self._entrar()
        self.cliente_http.environ_base["REMOTE_ADDR"] = IP_CLIENTE

    # ------------------------------------------------------------ apoio
    def _publicar(self, tipo="TERMOS_USO", conteudo=TERMOS, versao=0, alteracao=None, seguir=True):
        dados = {"tipo": tipo, "conteudo": conteudo, "versao_exibida": str(versao)}
        if alteracao is not None:
            dados["alteracao"] = alteracao
        return self._enviar("/admin/configuracoes/documentos", cliente=self.admin_http, pagina=PAGINA_ADMIN,
                            seguir=seguir, **dados)

    def _publicar_os_dois(self):
        self._publicar()
        self._publicar("POLITICA_PRIVACIDADE", POLITICA)

    def _modal(self, cliente=None, caminho="/cliente/dashboard"):
        resposta = (cliente or self.cliente_http).get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        return resposta.get_data(as_text=True)

    def _aceitar(self, cliente=None, confirmo="1", documentos=None, proximo="/cliente/historico", seguir=False):
        cliente = cliente or self.cliente_http
        pagina = self._modal(cliente)
        dados = {"csrf_token": re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina).group(1), "proximo": proximo,
                 "documento": documentos if documentos is not None else re.findall(r'name="documento" value="(\d+)"', pagina)}
        if confirmo is not None:
            dados["confirmo"] = confirmo
        return cliente.post("/cliente/termos/aceitar", data=dados, follow_redirects=seguir)

    def _aceites(self):
        from models import AceiteDocumento, DocumentoLegal
        self.db.session.expire_all()
        return self.db.session.execute(
            select(AceiteDocumento, DocumentoLegal).join(DocumentoLegal)
            .order_by(DocumentoLegal.tipo, AceiteDocumento.id_aceite)).all()

    def _vigente(self, tipo="TERMOS_USO"):
        import configuracoes_conta
        self.db.session.expire_all()
        return configuracoes_conta.documento_vigente(tipo)

    def _flashes(self, cliente):
        with cliente.session_transaction() as sessao:
            return [str(mensagem) for _categoria, mensagem in sessao.get("_flashes", ())]


# =====================================================================
# Cenário 1: bloqueio de navegação para quem não aceitou
# =====================================================================
class BloqueioTests(_TermosCase):
    def test_1_sem_aceite_qualquer_pagina_do_portal_vira_o_modal(self):
        self.publicar(self.analise_id)  # análise publicada: o conteúdo existiria sem a trava
        self._publicar_os_dois()
        for caminho in (*PAGINAS_DO_CLIENTE, f"/cliente/analise/{self.analise_id}"):
            with self.subTest(caminho=caminho):
                html = self._modal(caminho=caminho)
                self.assertIn('id="modalTermos"', html)
                self.assertIn('aria-modal="true"', html)
                self.assertIn(CONFIRMACAO, html)
                self.assertIn("Cláusula sintética de teste.", html)
                self.assertIn("&lt;script&gt;alert(&#39;politica&#39;)&lt;/script&gt;", html)
                self.assertNotIn("<script>alert('politica')", html)
                # A página pedida não foi desenhada, e o portal atrás do modal fica inerte.
                self.assertNotIn(TEXTO_PRIVADO, html)
                self.assertNotIn('name="arquivo"', html)
                self.assertRegex(html, r'<div class="app-shell" id="appShell" inert>')
                # Sem botão de fechar, sem NexoBot, sem tempo real e sem atalho para o conteúdo.
                modal = html[html.index('id="modalTermos"'):html.index('class="modal-backdrop show"')]
                self.assertNotIn("data-bs-dismiss", modal)
                self.assertNotIn("btn-close", modal)
                for ausente in ('id="nexobot"', "socket.io.min.js", 'class="skip-link"'):
                    self.assertNotIn(ausente, html)

    def test_pdf_de_analise_publicada_tambem_fica_bloqueado(self):
        self.publicar(self.analise_id)
        self._publicar_os_dois()
        resposta = self.cliente_http.get(f"/cliente/analise/{self.analise_id}/pdf")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.mimetype, "text/html")
        self.assertIn('id="modalTermos"', resposta.get_data(as_text=True))

    def test_envios_por_post_sao_recusados_sem_gravar(self):
        from models import ChamadoSuporte
        csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', self._modal()).group(1)
        self._publicar()
        resposta = self.cliente_http.post("/cliente/tickets", data={
            "csrf_token": csrf, "assunto": "Chamado barrado", "categoria": "FINANCEIRO", "mensagem": "Texto"})
        self.assertEqual(resposta.status_code, 303)
        self.assertTrue(resposta.headers["Location"].endswith("/cliente/dashboard"))
        self.assertEqual(self._contar(ChamadoSuporte), 0)
        # A pessoa fica sabendo que o envio não valeu: o aviso aparece dentro do modal.
        self.assertIn("Sua última ação não foi concluída", self._modal())
        resposta = self.cliente_http.post("/cliente/configuracoes/perfil", data={"csrf_token": csrf, "nome": "Nome Novo"})
        self.assertEqual(resposta.status_code, 303)
        self.assertEqual(self._usuario().nome, "Cliente fictício")
        self.assertEqual(self.cliente_http.post("/notificacoes/marcar-lidas", data={"csrf_token": csrf}).status_code, 303)

    def test_api_do_nexobot_responde_json_e_nao_html(self):
        self._publicar()
        html = self._modal()
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', html).group(1)
        resposta = self.cliente_http.post("/api/suporte-bot", json={"mensagem": "Oi"},
                                          headers={"X-CSRFToken": token})
        # Nem 401 nem 403: com esses, o widget diria "sessão expirada" em vez de mostrar o motivo.
        self.assertEqual(resposta.status_code, 428)
        self.assertIn("aceite os Termos de Uso", resposta.get_json()["resposta"])

    def test_como_aderir_com_sessao_tambem_vira_o_modal(self):
        """Com sessão, a página é desenhada dentro do portal: não pode abrir o NexoBot nem o tempo real."""
        self._publicar()
        html = self._modal(caminho="/como-aderir")
        self.assertIn('id="modalTermos"', html)
        for ausente in ('id="nexobot"', "socket.io.min.js"):
            self.assertNotIn(ausente, html)
        self.assertEqual(self.app.test_client().get("/como-aderir").status_code, 200)

    def test_aceite_comecado_pelo_pdf_volta_para_a_analise(self):
        self.publicar(self.analise_id)
        self._publicar()
        html = self.cliente_http.get(f"/cliente/analise/{self.analise_id}/pdf").get_data(as_text=True)
        proximo = re.search(r'name="proximo" value="([^"]+)"', html).group(1)
        self.assertEqual(proximo, f"/cliente/analise/{self.analise_id}")
        resposta = self._aceitar(proximo=proximo)
        self.assertTrue(resposta.headers["Location"].endswith(f"/cliente/analise/{self.analise_id}"))

    def test_saidas_continuam_abertas_sair_exportar_e_pedir_exclusao(self):
        from models import ChamadoSuporte
        self._publicar_os_dois()
        html = self._modal()
        self.assertIn('action="/auth/logout"', html[html.index('id="modalTermos"'):])
        exportacao = self.cliente_http.get("/cliente/configuracoes/exportar-dados")
        self.assertEqual((exportacao.status_code, exportacao.mimetype), (200, "application/json"))
        csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)
        resposta = self.cliente_http.post("/cliente/configuracoes/solicitar-exclusao", data={"csrf_token": csrf},
                                          follow_redirects=True)
        self.assertIn("Solicitação registrada", resposta.get_data(as_text=True))
        self.assertEqual(self._contar(ChamadoSuporte), 1)
        resposta = self.cliente_http.post("/auth/logout", data={"csrf_token": csrf})
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/auth/login", resposta.headers["Location"])

    def test_sem_documento_publicado_ninguem_e_barrado(self):
        for caminho in PAGINAS_DO_CLIENTE:
            self.assertNotIn('id="modalTermos"', self._modal(caminho=caminho), caminho)

    def test_admin_e_anonimo_nao_passam_pela_trava(self):
        self._publicar_os_dois()
        for caminho in ("/admin/dashboard", PAGINA_ADMIN, "/admin/termos-aceites"):
            resposta = self.admin_http.get(caminho)
            self.assertEqual(resposta.status_code, 200, caminho)
            self.assertNotIn('id="modalTermos"', resposta.get_data(as_text=True))
        anonimo = self.app.test_client().get("/cliente/dashboard")
        self.assertEqual(anonimo.status_code, 302)
        self.assertIn("/auth/login", anonimo.headers["Location"])

    def test_primeiro_acesso_vem_antes_do_aceite(self):
        self._publicar()
        usuario = self._usuario()
        usuario.primeiro_acesso = True
        self.db.session.commit()
        cliente, _ = self._entrar()
        resposta = cliente.get("/cliente/dashboard")
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/auth/primeiro-acesso", resposta.headers["Location"])
        self.assertEqual(cliente.get("/auth/primeiro-acesso").status_code, 200)

    def test_rota_inexistente_e_metodo_errado_seguem_404_e_405(self):
        self._publicar()
        self.assertEqual(self.cliente_http.get("/cliente/nao-existe").status_code, 404)
        self.assertEqual(self.cliente_http.get("/cliente/termos/aceitar").status_code, 405)

    def test_falha_de_banco_na_trava_deixa_a_pagina_seguir(self):
        self._publicar()
        with patch.object(termos_legais, "pendentes", side_effect=OperationalError("SELECT", {}, Exception("queda"))), \
                patch.object(termos_legais.logger, "disabled", False), \
                self.assertLogs(termos_legais.logger, level="WARNING"):
            html = self._modal()
        self.assertNotIn('id="modalTermos"', html)

    def test_sem_a_revisao_nova_o_portal_segue_sem_trava(self):
        from flask_migrate import downgrade
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            downgrade(directory=str(ROOT / "migrations"), revision="e2f7a94c1d35")
        preferencias_usuario._esquema_por_engine.clear()
        self.assertFalse(preferencias_usuario.esquema_configuracoes_disponivel())
        cliente, _ = self._entrar()
        self.assertNotIn('id="modalTermos"', self._modal(cliente))
        self.assertIn("a8d3f6b91c24", self.admin_http.get(PAGINA_ADMIN).get_data(as_text=True))


# =====================================================================
# Cenário 2: o aceite grava usuário, versão, instante e IP
# =====================================================================
class RegistroTests(_TermosCase):
    def test_2_aceite_grava_ip_versao_e_instante(self):
        self._publicar_os_dois()
        resposta = self._aceitar()
        self.assertEqual(resposta.status_code, 302)
        self.assertTrue(resposta.headers["Location"].endswith("/cliente/historico"))
        aceites = self._aceites()
        self.assertEqual([(documento.tipo, documento.rotulo_versao) for _aceite, documento in aceites],
                         [("POLITICA_PRIVACIDADE", "v1.0"), ("TERMOS_USO", "v1.0")])
        for aceite, documento in aceites:
            self.assertEqual(aceite.id_usuario, self.cliente_id)
            self.assertEqual(aceite.id_documento, documento.id_documento)
            self.assertEqual(aceite.ip_usuario, IP_CLIENTE)
            # Gravado em UTC (instante exato); exibido em Brasília.
            self.assertEqual(aceite.aceito_em, T0)
            self.assertIs(aceite.aceito_em.tzinfo, timezone.utc)
        self.assertNotIn('id="modalTermos"', self._modal())
        auditoria = self.admin_http.get("/admin/termos-aceites").get_data(as_text=True)
        self.assertIn("02/03/2026 às 10:00", auditoria)
        self.assertIn(f"IP {IP_CLIENTE}", auditoria)

    def test_caixa_de_concordo_e_conferida_no_servidor(self):
        self._publicar_os_dois()
        for valor in (None, "", "0", "on"):
            with self.subTest(confirmo=valor):
                self.assertEqual(self._aceitar(confirmo=valor).status_code, 302)
                self.assertEqual(self._aceites(), [])
        self.assertTrue(any("Marque" in mensagem for mensagem in self._flashes(self.cliente_http)))
        self.assertIn('id="modalTermos"', self._modal())

    def test_versao_publicada_durante_a_leitura_nao_e_aceita_sem_ser_lida(self):
        self._publicar_os_dois()
        pagina = self._modal()
        lidos = re.findall(r'name="documento" value="(\d+)"', pagina)
        self._publicar(conteudo="Termos revisados durante a leitura.", versao=1)
        resposta = self._aceitar(documentos=lidos, seguir=True)
        self.assertIn("foram atualizados enquanto você lia", resposta.get_data(as_text=True))
        self.assertEqual(self._aceites(), [])
        self.assertIn("Termos revisados durante a leitura.", self._modal())

    def test_documento_forjado_no_formulario_e_recusado(self):
        self._publicar()
        for documentos in (["abc"], ["1 OR 1=1"], ["-1"]):
            with self.subTest(documentos=documentos):
                self._aceitar(documentos=documentos)
                self.assertEqual(self._aceites(), [])

    def test_envio_duplo_nao_duplica_e_csrf_e_obrigatorio(self):
        self._publicar_os_dois()
        pagina = self._modal()
        sem_csrf = self.cliente_http.post("/cliente/termos/aceitar", data={
            "confirmo": "1", "documento": re.findall(r'name="documento" value="(\d+)"', pagina)})
        self.assertEqual(sem_csrf.status_code, 400)
        self.assertEqual(self._aceites(), [])
        self._aceitar()
        csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina).group(1)
        repetido = self.cliente_http.post("/cliente/termos/aceitar", data={
            "csrf_token": csrf, "confirmo": "1", "documento": re.findall(r'name="documento" value="(\d+)"', pagina)})
        self.assertEqual(repetido.status_code, 302)
        self.assertEqual(len(self._aceites()), 2)

    def test_corrida_de_aceite_so_vale_se_nada_ficou_pendente(self):
        from sqlalchemy.exc import IntegrityError
        self._publicar()
        corrida = IntegrityError("INSERT", {}, Exception("corrida sintética"))
        # Outra aba gravou primeiro: a restrição única dispara e não sobra pendência.
        def outra_aba_gravou(usuario, ids, ip):
            from models import AceiteDocumento
            self.db.session.rollback()
            with self.db.engine.begin() as conexao:
                conexao.execute(AceiteDocumento.__table__.insert().values(
                    id_usuario=usuario.id_usuario, id_documento=int(ids[0]), aceito_em=T0, ip_usuario=IP_CLIENTE))
            raise corrida
        with patch.object(termos_legais, "aceitar_pendentes", side_effect=outra_aba_gravou):
            self.assertIn("Aceite registrado.", self._aceitar(seguir=True).get_data(as_text=True))
        # Falha de verdade: a pendência continua e a pessoa é avisada.
        self.db.session.execute(self.db.text("DELETE FROM aceite_documento"))
        self.db.session.commit()
        with patch.object(termos_legais, "aceitar_pendentes", side_effect=corrida):
            pagina = self._aceitar(seguir=True).get_data(as_text=True)
        self.assertIn("Não foi possível registrar o aceite", pagina)
        self.assertIn('id="modalTermos"', pagina)

    def test_proximo_so_aceita_caminho_local(self):
        self._publicar()
        for proximo in ("https://exemplo.invalid/fora", "//exemplo.invalid", "/cliente/termos/aceitar"):
            with self.subTest(proximo=proximo):
                self._aceites()
                resposta = self._aceitar(proximo=proximo)
                self.assertTrue(resposta.headers["Location"].endswith("/cliente/dashboard"), resposta.headers["Location"])

    def test_ip_vem_do_remote_addr_e_nao_de_cabecalho_forjado(self):
        self._publicar()
        pagina = self._modal()
        csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', pagina).group(1)
        self.cliente_http.post("/cliente/termos/aceitar", data={
            "csrf_token": csrf, "confirmo": "1", "proximo": "/cliente/dashboard",
            "documento": re.findall(r'name="documento" value="(\d+)"', pagina),
        }, headers={"X-Forwarded-For": "198.51.100.66"})
        self.assertEqual(self._aceites()[0][0].ip_usuario, IP_CLIENTE)

    def test_normalizacao_do_ip(self):
        for bruto, esperado in (("203.0.113.7", "203.0.113.7"), (" 2001:DB8::1 ", "2001:db8::1"),
                                ("::ffff:203.0.113.9", "203.0.113.9"), ("não é ip", None), ("", None), (None, None)):
            with self.subTest(bruto=bruto):
                self.assertEqual(termos_legais.ip_do_cliente(bruto), esperado)

    def test_aceite_pelas_configuracoes_tambem_grava_ip(self):
        self._publicar()
        self._enviar("/cliente/configuracoes/aceitar-documento", cliente=self.cliente_http, pagina="/cliente/dashboard",
                     tipo="TERMOS_USO", versao="1")
        self.assertEqual(self._aceites()[0][0].ip_usuario, IP_CLIENTE)

    def test_exportacao_lgpd_traz_rotulo_e_ip_do_aceite(self):
        self._publicar()
        self._aceitar()
        dados = json.loads(self.cliente_http.get("/cliente/configuracoes/exportar-dados").get_data(as_text=True))
        aceite = dados["titular"]["aceites"][0]
        self.assertEqual((aceite["documento"], aceite["versao"], aceite["rotulo"], aceite["ip"]),
                         ("TERMOS_USO", 1, "v1.0", IP_CLIENTE))
        self.assertTrue(aceite["aceito_em"].endswith("+00:00"))


# =====================================================================
# Cenário 3: o admin publica uma versão nova e o modal volta
# =====================================================================
class ReaceiteTests(_TermosCase):
    def test_3_nova_versao_reabre_o_modal_so_do_documento_alterado(self):
        self._publicar_os_dois()
        self._aceitar()
        self.assertNotIn('id="modalTermos"', self._modal())
        self.relogio(T0 + timedelta(days=1))
        self.assertIn("versão v1.1 publicada", self._publicar(conteudo="Termos de Uso revisados.", versao=1)
                      .get_data(as_text=True))
        html = self._modal()
        self.assertIn('id="modalTermos"', html)
        self.assertIn("Termos de Uso revisados.", html)
        self.assertIn("v1.1", html)
        self.assertNotIn("Política de Privacidade fictícia.", html)
        self.assertEqual(len(re.findall(r'name="documento" value="\d+"', html)), 1)
        self._aceitar()
        self.assertNotIn('id="modalTermos"', self._modal())
        termos = [(documento.rotulo_versao, aceite.aceito_em) for aceite, documento in self._aceites()
                  if documento.tipo == "TERMOS_USO"]
        self.assertEqual(termos, [("v1.0", T0), ("v1.1", T0 + timedelta(days=1))])

    def test_nova_versao_maior_tambem_pede_aceite(self):
        self._publicar()
        self._aceitar()
        self._publicar(conteudo="Termos reescritos.", versao=1, alteracao="nova")
        self.assertEqual(self._vigente().rotulo_versao, "v2.0")
        self.assertIn("v2.0", self._modal())

    def test_rotulos_de_revisao_e_de_nova_versao(self):
        rotulos = []
        for indice, (conteudo, alteracao) in enumerate((("A", None), ("B", "revisao"), ("C", "nova"), ("D", "revisao"),
                                                       ("E", "revisao"), ("F", "nova"))):
            self._publicar(conteudo=conteudo, versao=indice, alteracao=alteracao)
            rotulos.append(self._vigente().rotulo_versao)
        self.assertEqual(rotulos, ["v1.0", "v1.1", "v2.0", "v2.1", "v2.2", "v3.0"])
        resposta = self._publicar(conteudo="G", versao=6, alteracao="maior")
        self.assertEqual(resposta.status_code, 400)
        self.assertIn("revisão ou uma nova versão", resposta.get_data(as_text=True))
        self.assertEqual(self._vigente().rotulo_versao, "v3.0")

    def test_historico_mostra_versoes_inativas_com_autor_e_data(self):
        self._publicar()
        self.relogio(T0 + timedelta(days=2))
        self._publicar(conteudo="Segunda redação.", versao=1, alteracao="nova")
        html = self.admin_http.get(PAGINA_ADMIN).get_data(as_text=True)
        self.assertIn("Documentos Legais", html)
        self.assertIn("Versão vigente: v2.0", html)
        self.assertIn("Histórico de versões (2)", html)
        self.assertRegex(html, r"v1\.0</strong>\s*<span class=\"nexo-badge b-muted\">Inativa desde <time[^>]*>04/03/2026 às 10:00")
        self.assertIn("Admin fictício", html)
        self.assertIn("Revisão (v2.1)", html)
        self.assertIn("Nova versão (v3.0)", html)


# =====================================================================
# Auditoria, alerta de pendência e lembrete por e-mail
# =====================================================================
class AuditoriaTests(_TermosCase):
    def _cenario(self):
        """Um cliente que aceitou, um pendente, um desativado e um sem a senha definida."""
        pendente = self._novo_cliente("pendente@example.invalid", nome="Pessoa Pendente")
        desativado = self._novo_cliente("desativado@example.invalid", nome="Pessoa Desativada")
        sem_senha = self._novo_cliente("sem.senha@example.invalid", nome="Pessoa Sem Senha")
        # Sem _usuario(): o expire_all dele descartaria a primeira alteração antes do commit.
        self.db.session.get(self.Usuario, desativado).ativo = False
        self.db.session.get(self.Usuario, sem_senha).primeiro_acesso = True
        self.db.session.commit()
        self._publicar()
        self._aceitar()
        return pendente

    def test_tabela_status_versao_data_ip_e_adesao(self):
        self._cenario()
        html = self.admin_http.get("/admin/termos-aceites").get_data(as_text=True)
        self.assertIn("50% dos usuários aceitaram a v1.0 (1 de 2)", html)
        self.assertIn(">ACEITO<", html)
        self.assertEqual(html.count(">PENDENTE<"), 3)
        for texto in ("Pessoa Pendente", "Usuário desativado", "Ainda não definiu a senha", "02/03/2026 às 10:00",
                      f"IP {IP_CLIENTE}", "Enviar Lembrete por E-mail"):
            self.assertIn(texto, html)
        # Lembrete só para quem consegue aceitar: o pendente com acesso.
        self.assertEqual(html.count("Enviar Lembrete por E-mail</button>"), 1)

    def test_so_o_admin_abre_a_auditoria(self):
        self._publicar()
        self._aceitar()
        self.assertEqual(self.cliente_http.get("/admin/termos-aceites").status_code, 403)
        anonimo = self.app.test_client().get("/admin/termos-aceites")
        self.assertIn("/auth/login", anonimo.headers["Location"])
        self.assertEqual(self.admin_http.get("/admin/termos-aceites").headers["Cache-Control"], "no-store")

    def test_admin_recebe_no_sininho_a_pendencia_vencida_uma_vez_por_versao(self):
        from models import Notificacao
        self._cenario()
        texto = "Aceite pendente há mais de 7 dias: Termos de Uso v1.0. Veja quem falta em Termos e aceites."
        self.relogio(T0 + timedelta(days=7))
        self._entrar(ADMIN)
        self.assertEqual(self._contar(Notificacao, Notificacao.texto == texto), 0)
        self.relogio(T0 + timedelta(days=8))
        for _ in range(2):
            self.assertEqual(self._entrar(ADMIN)[1].status_code, 302)
        avisos = self.db.session.scalars(select(Notificacao).where(Notificacao.texto == texto)).all()
        self.assertEqual([(aviso.id_usuario, aviso.link_destino) for aviso in avisos],
                         [(self.admin_id, "/admin/termos-aceites")])
        admin = self._entrar(ADMIN)[0]
        dashboard = admin.get("/admin/dashboard").get_data(as_text=True)
        self.assertIn("1 cliente(s) com aceite dos termos pendente há mais de 7 dias", dashboard)
        self.assertIn(">Aberto<", dashboard)  # ação pendente para a equipe
        auditoria = admin.get("/admin/termos-aceites").get_data(as_text=True)
        self.assertIn("1 cliente(s) com aceite pendente há mais de 7 dias.", auditoria)
        self.assertIn('b-red">PENDENTE', auditoria)

    def test_sem_pendente_com_acesso_nao_ha_alerta(self):
        from models import Notificacao
        pendente = self._cenario()
        self._usuario(pendente).ativo = False
        self.db.session.commit()
        self.relogio(T0 + timedelta(days=30))
        self._entrar(ADMIN)
        self.assertEqual(self._contar(Notificacao, Notificacao.texto.like("Aceite pendente%")), 0)

    def test_falha_no_alerta_nao_impede_o_login_do_admin(self):
        self._cenario()
        self.relogio(T0 + timedelta(days=8))
        with patch.object(termos_legais, "alertar_pendencias_atrasadas", side_effect=RuntimeError("falha sintética")), \
                patch.object(termos_legais.logger, "disabled", False), \
                self.assertLogs(termos_legais.logger, level="WARNING"):
            cliente, resposta = self._entrar(ADMIN)
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(cliente.get("/admin/dashboard").status_code, 200)

    def test_lembrete_por_email_so_para_quem_esta_pendente(self):
        pendente = self._cenario()
        rota = f"/admin/termos-aceites/{pendente}/lembrete"
        with self._emails() as enviados:
            resposta = self._enviar(rota, cliente=self.admin_http, pagina="/admin/termos-aceites")
            self.assertIn("lembrete de aceite", resposta.get_data(as_text=True))
            self.assertEqual([mensagem.recipients for mensagem in enviados], [["pendente@example.invalid"]])
            # O assunto cita só o que está pendente: aqui, só os Termos foram publicados.
            self.assertIn("Termos de Uso v1.0", enviados[0].subject)
            self.assertNotIn("Política", enviados[0].subject)
            corpo = enviados[0].html
            self.assertIn("Termos de Uso v1.0", corpo)
            self.assertIn("/cliente/dashboard", corpo)
            self.assertNotIn(TERMOS.split("\n")[0], corpo)  # o texto do documento não vai por e-mail
            # Quem já aceitou, não consegue aceitar (desativado) ou não é cliente não recebe nada.
            desativado = self.db.session.scalar(select(self.Usuario.id_usuario).where(
                self.Usuario.email == "desativado@example.invalid"))
            for alvo in (self.cliente_id, self.admin_id, desativado):
                self._enviar(f"/admin/termos-aceites/{alvo}/lembrete", cliente=self.admin_http, pagina="/admin/termos-aceites")
            self.assertEqual(len(enviados), 1)
        self.assertEqual(self.admin_http.get(rota).status_code, 405)
        self.assertEqual(self.cliente_http.post(rota, data={"csrf_token": "x"}).status_code, 400)


class MigracaoTests(_TermosCase):
    def _downgrade(self):
        from flask_migrate import downgrade
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            downgrade(directory=str(ROOT / "migrations"), revision="e2f7a94c1d35")

    def test_downgrade_recusa_apagar_ip_de_aceite(self):
        self._publicar()
        self._aceitar()
        with self.assertRaises(SystemExit):
            self._downgrade()
        self.assertEqual(self._aceites()[0][0].ip_usuario, IP_CLIENTE)

    def test_downgrade_recusa_apagar_rotulo_de_revisao(self):
        self._publicar()
        self._publicar(conteudo="Revisão.", versao=1)
        with self.assertRaises(SystemExit):
            self._downgrade()
        self.assertEqual(self._vigente().rotulo_versao, "v1.1")

    def _downgrade_com_o_processo_no_ar(self):
        """Colunas removidas sem limpar o cache do engine, como um processo que segue no ar."""
        self._publicar()
        self._aceitar()
        self.assertNotIn('id="modalTermos"', self._modal())  # guarda a presença no cache do engine
        self.db.session.execute(self.db.text("DELETE FROM aceite_documento"))
        self.db.session.execute(self.db.text("DELETE FROM documento_legal"))
        self._downgrade()

    def test_downgrade_com_o_processo_no_ar_a_trava_esquece_o_esquema(self):
        self._downgrade_com_o_processo_no_ar()
        self.assertEqual(self.cliente_http.get("/cliente/dashboard").status_code, 200)
        # A consulta que falhou esqueceu a presença: as demais funções reconferem o banco.
        self.assertFalse(preferencias_usuario.esquema_configuracoes_disponivel())

    def test_downgrade_com_o_processo_no_ar_nao_derruba_o_painel_do_admin(self):
        self._downgrade_com_o_processo_no_ar()
        self.assertEqual(self.admin_http.get("/admin/dashboard").status_code, 200)
        self.assertFalse(preferencias_usuario.esquema_configuracoes_disponivel())

    def test_upgrade_rotula_as_versoes_ja_publicadas(self):
        from flask_migrate import upgrade
        self._downgrade()
        with self.db.engine.begin() as conexao:
            for versao in (1, 2):
                conexao.execute(self.db.text(
                    "INSERT INTO documento_legal (tipo, versao, conteudo, publicado_em, id_usuario_admin) "
                    "VALUES ('TERMOS_USO', :versao, 'Texto anterior', '2026-01-01 12:00:00', :admin)"),
                    {"versao": versao, "admin": self.admin_id})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            upgrade(directory=str(ROOT / "migrations"), revision="head")
        from models import DocumentoLegal
        self.db.session.expire_all()
        self.assertEqual([documento.rotulo_versao for documento in self.db.session.scalars(
            select(DocumentoLegal).order_by(DocumentoLegal.versao))], ["v1.0", "v2.0"])


if __name__ == "__main__":
    unittest.main()
