"""Documentos legais públicos, edição no painel do admin e a regra da rolagem no HTML.

Páginas /termos-de-uso e /politica-de-privacidade e o JSON /documentos-legais/vigentes
(blueprints/documentos_publicos.py), a trava de aceite que os deixa abertos, a tela
Configurações › Documentos Legais em modo leitura, com o editor num <details>, e o HTML do
aceite (modal e Configurações do cliente), que nasce travado até o fim de cada texto. O
comportamento da rolagem no navegador está em tests/test_aceite_rolagem_navegador.py.

Migrações reais em SQLite (DeployTestCase), dados sintéticos e frases de teste no lugar de
texto jurídico. O relógio é o da política (NEXO_RELOGIO): T0 = 02/03/2026 13:00 UTC, 10:00 em
Brasília. Com NEXO_TEST_POSTGRES_BIN, um ensaio isolado em PostgreSQL real (como os de
tests/test_*_postgresql.py) confere também a publicação pela rota do admin, o instante em UTC no
JSON com a sessão do banco em outro fuso e a tabela ausente sem deixar a transação abortada.

Executar da raiz: python -B -m unittest tests.test_documentos_publicos -v
"""

import contextlib
from datetime import timedelta
import io
import re
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import preferencias_usuario
import raio_x_servico
import termos_legais
from blueprints import documentos_publicos
from tests.contratos_support import T0
from tests.deploy_support import ROOT
from tests.test_configuracoes_conta import PAGINA_ADMIN
from tests.test_termos_privacidade import POLITICA, TERMOS, _TermosCase

PAGINAS = {"TERMOS_USO": "/termos-de-uso", "POLITICA_PRIVACIDADE": "/politica-de-privacidade"}
VIGENTES = "/documentos-legais/vigentes"
NAO_PUBLICADO = "Este documento ainda não foi publicado."
INDISPONIVEL = "Não foi possível abrir o documento agora."
JSON_INDISPONIVEL = {"documentos": [], "erro": "indisponivel"}
LOG_INDISPONIVEL = "WARNING:blueprints.documentos_publicos:Documentos legais públicos indisponíveis (OperationalError)."


class _PublicosCase(_TermosCase):
    def _anonimo(self):
        return self.app.test_client()

    @contextlib.contextmanager
    def _banco_fora(self):
        """Banco fora na consulta do documento; no log, só o tipo do erro (o fileConfig do Alembic, nas
        migrações do setUp, desliga os loggers já criados)."""
        queda = OperationalError("SELECT", {}, Exception("queda sintética"))
        with patch.object(documentos_publicos, "documento_vigente", side_effect=queda), \
                patch.object(documentos_publicos.logger, "disabled", False), \
                self.assertLogs(documentos_publicos.logger, level="WARNING") as registro:
            yield registro
        self.assertEqual(registro.output, [LOG_INDISPONIVEL])

    def _downgrade(self, revisao):
        from flask_migrate import downgrade
        self.db.session.commit()
        self.db.session.remove()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch("logging.config.fileConfig"):
            downgrade(directory=str(ROOT / "migrations"), revision=revisao)


# =====================================================================
# Páginas públicas
# =====================================================================
class PaginasPublicasTests(_PublicosCase):
    def test_sem_login_mostra_a_vigente_escapada_com_rotulo_data_e_quebras(self):
        self._publicar_os_dois()
        termos = self._anonimo().get(PAGINAS["TERMOS_USO"])
        self.assertEqual(termos.status_code, 200)
        self.assertEqual(termos.headers["Cache-Control"], "no-store")
        html = termos.get_data(as_text=True)
        # O texto inteiro, com as quebras de linha como o admin digitou (o CSS as mantém: pre-wrap).
        self.assertIn(f'<div class="doc-texto">{TERMOS}</div>', html)
        self.assertIn("Versão vigente: v1.0", html)
        self.assertIn('<time datetime="2026-03-02T13:00:00+00:00">02/03/2026 às 10:00</time> (horário de Brasília)', html)
        self.assertIn('aria-current="page">Termos de Uso</a>', html)
        self.assertIn('href="/politica-de-privacidade"', html)
        self.assertNotIn("Política de Privacidade fictícia.", html)
        politica = self._anonimo().get(PAGINAS["POLITICA_PRIVACIDADE"]).get_data(as_text=True)
        self.assertIn("Política de Privacidade fictícia.\n\n&lt;script&gt;alert(&#39;politica&#39;)&lt;/script&gt;", politica)
        # Página sem script nenhum: o único "<script" seria o do documento, e ele sai escapado.
        self.assertNotIn("<script", politica)
        self.assertNotRegex(politica, r"\son[a-z]+=")

    def test_marca_sem_tema_e_arquivos_estaticos_que_existem(self):
        html = self._anonimo().get(PAGINAS["TERMOS_USO"]).get_data(as_text=True)
        # A assinatura da marca, como nas outras páginas públicas: o cubo decorativo e o nome uma vez, em texto.
        topo = html[html.index('<header class="doc-topo">'):html.index("</header>")]
        imagens = re.findall(r"<img [^>]*>", topo)
        self.assertEqual(len(imagens), 1)
        self.assertIn('src="/static/img/nexo-cubo-transparente.png" alt="" width="253" height="279"', imagens[0])
        texto = " ".join(re.sub(r"<[^>]+>", " ", topo).split())
        self.assertEqual(re.findall(r"\bNEXO\b", texto), ["NEXO"])
        self.assertEqual(texto.count("Faturamento Inteligente"), 1)
        # Sempre escura, como a landing: sem tema e sem armazenamento no navegador.
        for termo in ("data-theme", "localStorage", "nexo-tema-claro"):
            self.assertNotIn(termo, html)
        estaticos = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
        self.assertIn("/static/css/landing.css", estaticos)
        self.assertIn("/static/css/documentos-publicos.css", estaticos)
        for caminho in estaticos:
            with self.subTest(arquivo=caminho):
                resposta = self._anonimo().get(caminho)
                self.assertEqual(resposta.status_code, 200)
                resposta.close()

    def test_sem_versao_publicada_avisa_sem_inventar_texto(self):
        for caminho in PAGINAS.values():
            with self.subTest(caminho=caminho):
                resposta = self._anonimo().get(caminho)
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(resposta.headers["Cache-Control"], "no-store")
                html = resposta.get_data(as_text=True)
                self.assertIn(NAO_PUBLICADO, html)
                self.assertNotIn('class="doc-texto"', html)
                self.assertNotIn("Versão vigente", html)
        # Só os Termos publicados: a Política continua com o aviso.
        self._publicar()
        self.assertNotIn(NAO_PUBLICADO, self._anonimo().get(PAGINAS["TERMOS_USO"]).get_data(as_text=True))
        self.assertIn(NAO_PUBLICADO, self._anonimo().get(PAGINAS["POLITICA_PRIVACIDADE"]).get_data(as_text=True))

    def test_versao_nova_aparece_na_requisicao_seguinte(self):
        self._publicar()
        visitante = self._anonimo()
        self.assertIn("Cláusula sintética de teste.", visitante.get(PAGINAS["TERMOS_USO"]).get_data(as_text=True))
        self.relogio(T0 + timedelta(days=1))
        self._publicar(conteudo="Termos revisados para o teste.", versao=1)
        resposta = visitante.get(PAGINAS["TERMOS_USO"])
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        html = resposta.get_data(as_text=True)
        self.assertIn('<div class="doc-texto">Termos revisados para o teste.</div>', html)
        self.assertIn("Versão vigente: v1.1", html)
        self.assertIn("03/03/2026 às 10:00", html)
        self.assertNotIn("Cláusula sintética de teste.", html)

    def test_cliente_com_aceite_pendente_abre_as_paginas_e_o_json(self):
        self._publicar_os_dois()
        self.assertIn('id="modalTermos"', self._modal())  # o portal continua travado
        for caminho in (*PAGINAS.values(), VIGENTES):
            with self.subTest(caminho=caminho):
                resposta = self.cliente_http.get(caminho)
                self.assertEqual(resposta.status_code, 200)
                self.assertEqual(resposta.headers["Cache-Control"], "no-store")
                self.assertNotIn('id="modalTermos"', resposta.get_data(as_text=True))
        self.assertIn("Cláusula sintética de teste.", self.cliente_http.get(PAGINAS["TERMOS_USO"]).get_data(as_text=True))
        self.assertEqual(len(self.cliente_http.get(VIGENTES).get_json()["documentos"]), 2)
        # Ler a página não vale como aceite: o modal continua no portal.
        self.assertEqual(self._aceites(), [])
        self.assertIn('id="modalTermos"', self._modal())
        for endpoint in ("documentos_publicos.termos_de_uso", "documentos_publicos.politica_de_privacidade",
                         "documentos_publicos.vigentes"):
            self.assertIn(endpoint, termos_legais.ENDPOINTS_LIBERADOS)
        # O admin, fora da trava, também abre as páginas.
        self.assertEqual(self.admin_http.get(PAGINAS["TERMOS_USO"]).status_code, 200)

    def test_banco_fora_nao_derruba_a_pagina(self):
        self._publicar()
        with self._banco_fora():
            resposta = self._anonimo().get(PAGINAS["TERMOS_USO"])
        self.assertEqual(resposta.status_code, 503)
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        html = resposta.get_data(as_text=True)
        self.assertIn(INDISPONIVEL, html)
        self.assertNotIn("Cláusula sintética", html)
        self.assertNotIn("queda sintética", html)
        # Com o banco de volta, a requisição seguinte mostra o texto.
        self.assertEqual(self._anonimo().get(PAGINAS["TERMOS_USO"]).status_code, 200)

    def test_sem_as_tabelas_as_paginas_e_o_json_avisam_com_503(self):
        self._downgrade("b6935e8de9d4")
        preferencias_usuario._esquema_por_engine.clear()
        raio_x_servico._fechamento_por_engine.clear()
        self.assertFalse(preferencias_usuario.esquema_configuracoes_disponivel())
        for caminho in PAGINAS.values():
            with self.subTest(caminho=caminho):
                resposta = self._anonimo().get(caminho)
                self.assertEqual(resposta.status_code, 503)
                self.assertIn(INDISPONIVEL, resposta.get_data(as_text=True))
        resposta = self._anonimo().get(VIGENTES)
        self.assertEqual((resposta.status_code, resposta.get_json()), (503, JSON_INDISPONIVEL))
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")

    def test_coluna_removida_com_o_processo_no_ar_vira_503_e_o_esquema_e_conferido_de_novo(self):
        self._publicar()
        self.assertEqual(self._anonimo().get(PAGINAS["TERMOS_USO"]).status_code, 200)  # presença no cache do engine
        self.db.session.execute(self.db.text("DELETE FROM documento_legal"))
        self._downgrade("e2f7a94c1d35")  # sem versao_maior e versao_menor, com o cache dizendo que existem
        with patch.object(documentos_publicos.logger, "disabled", False), \
                self.assertLogs(documentos_publicos.logger, level="WARNING"):
            resposta = self._anonimo().get(PAGINAS["TERMOS_USO"])
        self.assertEqual(resposta.status_code, 503)
        self.assertFalse(preferencias_usuario.esquema_configuracoes_disponivel())

    def test_so_get(self):
        for caminho in (*PAGINAS.values(), VIGENTES):
            with self.subTest(caminho=caminho):
                self.assertEqual(self._anonimo().post(caminho).status_code, 405)


# =====================================================================
# JSON das versões vigentes (contrato com a landing e o microsserviço)
# =====================================================================
class VigentesJsonTests(_PublicosCase):
    def test_formato_do_contrato_na_ordem_dos_tipos(self):
        from models import DocumentoLegal
        # A Política sai antes: a ordem do JSON é a dos tipos, não a da publicação.
        self._publicar("POLITICA_PRIVACIDADE", POLITICA)
        self.relogio(T0 + timedelta(hours=1))
        self._publicar()
        resposta = self._anonimo().get(VIGENTES)
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.mimetype, "application/json")
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        self.db.session.expire_all()
        ids = {documento.tipo: documento.id_documento for documento in self.db.session.scalars(select(DocumentoLegal))}
        self.assertEqual(resposta.get_json(), {"documentos": [
            {"tipo": "TERMOS_USO", "id": ids["TERMOS_USO"], "rotulo": "v1.0",
             "publicado_em": "2026-03-02T14:00:00+00:00", "conteudo": TERMOS},
            {"tipo": "POLITICA_PRIVACIDADE", "id": ids["POLITICA_PRIVACIDADE"], "rotulo": "v1.0",
             "publicado_em": "2026-03-02T13:00:00+00:00", "conteudo": POLITICA},
        ]})
        self.assertIsInstance(resposta.get_json()["documentos"][0]["id"], int)

    def test_so_a_vigente_de_cada_tipo(self):
        self._publicar()
        self._publicar(conteudo="Revisão dos termos de teste.", versao=1)
        documentos = self._anonimo().get(VIGENTES).get_json()["documentos"]
        self.assertEqual([(item["tipo"], item["rotulo"], item["conteudo"]) for item in documentos],
                         [("TERMOS_USO", "v1.1", "Revisão dos termos de teste.")])
        self.assertEqual(documentos[0]["id"], self._vigente().id_documento)

    def test_nenhum_publicado_devolve_a_lista_vazia(self):
        resposta = self._anonimo().get(VIGENTES)
        self.assertEqual((resposta.status_code, resposta.get_json()), (200, {"documentos": []}))
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")

    def test_banco_fora_ou_esquema_ausente_responde_503(self):
        self._publicar_os_dois()
        with self._banco_fora():
            resposta = self._anonimo().get(VIGENTES)
        self.assertEqual((resposta.status_code, resposta.get_json()), (503, JSON_INDISPONIVEL))
        self.assertEqual(resposta.headers["Cache-Control"], "no-store")
        self.assertNotIn(b"queda", resposta.data)
        with patch.object(documentos_publicos, "esquema_configuracoes_disponivel", return_value=False):
            resposta = self._anonimo().get(VIGENTES)
        self.assertEqual((resposta.status_code, resposta.get_json()), (503, JSON_INDISPONIVEL))


# =====================================================================
# Configurações › Documentos Legais (admin)
# =====================================================================
class PainelDoAdminTests(_PublicosCase):
    def _artigo(self, html, tipo):
        """O trecho de um documento na tela do admin, do <article> ao </article>."""
        titulo = html.index(f'id="documento-{tipo}"')
        return html[html.rindex("<article", 0, titulo):html.index("</article>", titulo)]

    def _pagina_admin(self):
        return self.admin_http.get(PAGINA_ADMIN).get_data(as_text=True)

    def test_texto_vigente_em_modo_leitura_e_editar_num_details(self):
        self._publicar()
        termos = self._artigo(self._pagina_admin(), "TERMOS_USO")
        # Modo leitura: o texto vigente num bloco com rolagem, antes e fora do formulário.
        leitura = re.search(r'<div class="config-documento-texto"[^>]*role="region"[^>]*>(.*?)</div>', termos, re.S)
        self.assertEqual(leitura.group(1), TERMOS)
        self.assertLess(termos.index('class="config-documento-texto"'), termos.index("<form"))
        # Editar: o formulário fica num <details> fechado, que abre sem JavaScript.
        self.assertRegex(termos, r'<details class="config-editor">\s*<summary class="btn-ghost btn-sm-x">')
        self.assertIn('<span class="config-editor-fechado">Editar</span>', termos)
        self.assertGreater(termos.index("<form"), termos.index('<details class="config-editor">'))
        self.assertIn("Salvar</button>", termos)
        self.assertIn("Revisão (v1.1)", termos)
        self.assertIn("Nova versão (v2.0)", termos)
        self.assertRegex(termos, r'<a href="/termos-de-uso" target="_blank" rel="noopener noreferrer">.*Ver página pública')
        # Sem versão publicada, o editor já nasce aberto, e não há texto vigente para ler.
        politica = self._artigo(self._pagina_admin(), "POLITICA_PRIVACIDADE")
        self.assertIn('<details class="config-editor" open>', politica)
        self.assertNotIn('class="config-documento-texto"', politica)
        self.assertIn("Salvar publica a v1.0", politica)
        self.assertIn('href="/politica-de-privacidade"', politica)

    def test_salvar_cria_a_v1_1_e_mantem_a_v1_0(self):
        from models import DocumentoLegal
        self._publicar()
        anterior = self._vigente()
        antes = (anterior.id_documento, anterior.conteudo, anterior.publicado_em, anterior.versao_maior,
                 anterior.versao_menor, anterior.id_usuario_admin)
        self.assertEqual(self._contar(DocumentoLegal), 1)
        self.relogio(T0 + timedelta(days=1))
        resposta = self._publicar(conteudo="Termos de Uso, segunda redação de teste.", versao=1)
        self.assertIn("versão v1.1 publicada e já vale na página pública", resposta.get_data(as_text=True))
        self.assertEqual(self._contar(DocumentoLegal), 2)
        self.db.session.expire_all()
        mantida = self.db.session.get(DocumentoLegal, antes[0])
        self.assertEqual((mantida.id_documento, mantida.conteudo, mantida.publicado_em, mantida.versao_maior,
                          mantida.versao_menor, mantida.id_usuario_admin), antes)
        vigente = self._vigente()
        self.assertEqual((vigente.rotulo_versao, vigente.conteudo), ("v1.1", "Termos de Uso, segunda redação de teste."))
        html = self._pagina_admin()
        self.assertIn("Versão vigente: v1.1", html)
        self.assertRegex(html, r'v1\.0</strong>\s*<span class="nexo-badge b-muted">Inativa desde <time[^>]*>03/03/2026 às 10:00')
        # Na página pública, a v1.1 na hora; no JSON, o id novo.
        publica = self._anonimo().get(PAGINAS["TERMOS_USO"]).get_data(as_text=True)
        self.assertIn("Versão vigente: v1.1", publica)
        self.assertIn("segunda redação de teste", publica)
        self.assertEqual(self._anonimo().get(VIGENTES).get_json()["documentos"][0]["id"], vigente.id_documento)

    def test_recusa_400_volta_com_o_editor_aberto_e_o_rascunho(self):
        from models import DocumentoLegal
        self._publicar()
        resposta = self._publicar(conteudo="Rascunho que não pode se perder.", versao=0)  # outra versão já saiu
        self.assertEqual(resposta.status_code, 400)
        html = resposta.get_data(as_text=True)
        self.assertIn("Outra versão foi publicada", html)
        termos = self._artigo(html, "TERMOS_USO")
        self.assertIn('<details class="config-editor" open>', termos)
        self.assertIn(">Rascunho que não pode se perder.</textarea>", termos)
        # O modo leitura continua com o texto vigente, intacto.
        self.assertIn(f'role="region" aria-label="Texto vigente de Termos de Uso, v1.0">{TERMOS}</div>', termos)
        self.assertEqual(self._contar(DocumentoLegal), 1)
        # Na tela comum, o editor dos Termos volta fechado.
        self.assertIn('<details class="config-editor">', self._artigo(self._pagina_admin(), "TERMOS_USO"))

    def test_sem_as_tabelas_o_editor_fica_travado_sem_link_publico(self):
        self._downgrade("b6935e8de9d4")
        preferencias_usuario._esquema_por_engine.clear()
        raio_x_servico._fechamento_por_engine.clear()
        html = self.admin_http.get(PAGINA_ADMIN).get_data(as_text=True)
        self.assertIn("c7d41e9a2b60", html)
        self.assertNotIn("Ver página pública", html)
        self.assertNotIn("Ainda não há texto publicado", html)
        for tipo in PAGINAS:
            with self.subTest(tipo=tipo):
                artigo = self._artigo(html, tipo)
                self.assertRegex(artigo, r'<textarea [^>]*disabled>')
                self.assertIn('<button type="submit" class="btn-nexo" disabled><i class="bi bi-check2" aria-hidden="true"></i> Salvar</button>',
                              artigo)


# =====================================================================
# Regra da rolagem no HTML do servidor (o comportamento fica no navegador)
# =====================================================================
class RolagemNoHtmlTests(_PublicosCase):
    def test_modal_nasce_travado_com_o_aviso_o_noscript_e_o_script(self):
        self._publicar_os_dois()
        html = self._modal()
        modal = html[html.index('id="modalTermos"'):html.index('class="modal-backdrop show"')]
        self.assertIn("data-aceite-rolagem>", modal)
        caixa = re.search(r'<input[^>]*id="termosConfirmo"[^>]*>', modal).group(0)
        for atributo in (" required", " disabled", " data-aceite-libera", 'aria-describedby="termosAvisoRolagem"'):
            self.assertIn(atributo, caixa)
        botao = re.search(r'<button type="submit" class="btn-nexo"[^>]*>', modal).group(0)
        self.assertIn(" disabled data-aceite-libera", botao)
        textos = re.findall(r'<div class="termos-documento-texto"[^>]*>', modal)
        self.assertEqual(len(textos), 2)
        for texto in textos:
            self.assertIn('tabindex="0" role="region"', texto)
            self.assertIn("data-aceite-texto", texto)
        self.assertIn('data-aceite-nome="Termos de Uso"', modal)
        self.assertIn('data-aceite-nome="Política de Privacidade"', modal)
        # O marcador do fim é o último filho de cada texto.
        self.assertEqual(len(re.findall(r'<span class="aceite-fim" data-aceite-fim aria-hidden="true"></span></div>', modal)), 2)
        self.assertRegex(modal, r'<span id="termosAvisoRolagem" data-aceite-aviso aria-live="polite"\s+'
                                r'data-pronto="Você chegou ao fim dos textos\. Agora marque a caixa para aceitar\.">'
                                r'Role até o fim de cada texto para habilitar\.</span>')
        self.assertIn("<noscript><p", modal)
        self.assertIn("ative o JavaScript", modal)
        self.assertIn('<script src="/static/js/aceite-rolagem.js" defer></script>', html)
        self.assertNotRegex(html, r"\son(click|submit|load|change|input|scroll|keydown)=")
        resposta = self._anonimo().get("/static/js/aceite-rolagem.js")
        self.assertEqual(resposta.status_code, 200)
        self.assertIn("javascript", resposta.mimetype)
        resposta.close()

    def test_um_documento_so_usa_o_singular(self):
        self._publicar()
        modal = self._modal()
        self.assertIn("leia o documento abaixo até o fim e confirme", modal)
        self.assertIn(">Role até o fim do texto para habilitar.</span>", modal)
        self.assertIn('data-pronto="Você chegou ao fim do texto. Agora marque a caixa para aceitar."', modal)

    def test_aceite_pelas_configuracoes_tambem_nasce_travado(self):
        self._publicar()
        # A trava troca as Configurações pelo modal; sem pendência na trava, a tela mostra o caminho alternativo.
        with patch.object(termos_legais, "pendentes_da_sessao", return_value=[]):
            html = self.cliente_http.get("/cliente/configuracoes").get_data(as_text=True)
        self.assertNotIn('id="modalTermos"', html)
        inicio = html.index("data-aceite-rolagem")
        artigo = html[inicio:html.index("</article>", inicio)]
        self.assertRegex(artigo, r'<div class="config-documento-texto is-aceite" tabindex="0" role="region"[^>]*data-aceite-texto')
        self.assertIn('<span class="aceite-fim" data-aceite-fim aria-hidden="true"></span></div>', artigo)
        self.assertRegex(artigo, r'<button type="submit" class="btn-nexo" disabled data-aceite-libera[^>]*>.*Li e aceito a v1\.0')
        self.assertIn(">Role até o fim do texto para habilitar.</span>", artigo)
        self.assertIn("<noscript>", artigo)
        self.assertNotIn("<details>", artigo)  # o texto fica à vista, não escondido
        self.assertEqual(html.count('<script src="/static/js/aceite-rolagem.js" defer></script>'), 1)
        # Já aceito: só a leitura, sem botão, sem trava e sem o script.
        self._enviar("/cliente/configuracoes/aceitar-documento", cliente=self.cliente_http, pagina="/cliente/dashboard",
                     tipo="TERMOS_USO", versao="1")
        html = self.cliente_http.get("/cliente/configuracoes").get_data(as_text=True)
        self.assertNotIn("data-aceite-rolagem", html)
        self.assertNotIn("aceite-rolagem.js", html)
        self.assertIn("Você já aceitou a versão atual.", html)


# =====================================================================
# PostgreSQL real: ensaio isolado, como tests/test_planos_postgresql.py
# =====================================================================
# Ignorado sem NEXO_TEST_POSTGRES_BIN. Um processo separado, com ambiente filtrado e dotenv desligado, cria o
# próprio cluster temporário (porta livre em 127.0.0.1, sem socket Unix) e aplica as migrações reais; nenhum
# banco existente é usado. Só o que depende do PostgreSQL: o instante em UTC no JSON com a sessão em outro
# fuso, a publicação pela rota real e a falha de banco (tabela ausente) sem deixar a transação abortada.
CHECKS_POSTGRESQL = (
    "private_engine", "migration_reaches_head", "pages_and_json_without_publication",
    "v10_published_by_the_admin_route", "page_shows_v10_escaped_without_cache",
    "json_contract_in_utc_with_the_session_in_tokyo", "client_with_pending_acceptance_reads_the_pages",
    "v11_saved_without_overwriting_v10", "page_and_json_show_v11_on_the_next_request",
    "missing_table_503_without_aborting_the_next_request", "table_back_page_200",
)


class PostgreSQLDocumentosPublicosTests(unittest.TestCase):
    def test_documentos_publicos_em_postgresql_real(self):
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        from tests.test_contratos_postgresql import ROOT as RAIZ, TOOLS, _safe_environment, _tool
        bruto = os.environ.get("NEXO_TEST_POSTGRES_BIN")
        if not bruto:
            self.skipTest("Defina NEXO_TEST_POSTGRES_BIN para criar um cluster PostgreSQL temporário isolado.")
        self.assertTrue("://" not in bruto and Path(bruto).is_absolute(),
                        "NEXO_TEST_POSTGRES_BIN aceita apenas um caminho local absoluto; URLs são recusadas.")
        try:
            bin_dir = Path(bruto).resolve(strict=True)
        except (OSError, ValueError):
            self.fail("NEXO_TEST_POSTGRES_BIN não indica um diretório local acessível; valor omitido.")
        self.assertTrue(all(_tool(bin_dir, ferramenta).is_file() for ferramenta in TOOLS),
                        "O diretório precisa conter initdb, pg_ctl e createdb.")
        with tempfile.TemporaryDirectory(prefix="nexo-documentos-pg-") as pasta:
            work_dir = Path(pasta).resolve()
            data_dir = work_dir / "private-cluster"
            try:
                resultado = subprocess.run(
                    [sys.executable, "-B", "-m", "tests.test_documentos_publicos", "--isolated-worker",
                     str(bin_dir), str(work_dir)],
                    cwd=str(RAIZ), env=_safe_environment(), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=240, check=False,
                )
            except subprocess.TimeoutExpired:
                self.fail("O ensaio isolado excedeu 240 segundos; nenhuma saída ou credencial foi exibida.")
            finally:
                self.assertTrue(data_dir.resolve().is_relative_to(work_dir))
                if (data_dir / "postmaster.pid").is_file():
                    subprocess.run(
                        [str(_tool(bin_dir, "pg_ctl")), "--pgdata", str(data_dir), "--mode", "fast",
                         "--wait", "--timeout", "20", "stop"],
                        env=_safe_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=30, check=False,
                    )
        try:
            relatorio = json.loads(resultado.stdout)
        except (json.JSONDecodeError, TypeError):
            self.fail("O processo isolado não produziu um relatório seguro válido; saídas brutas omitidas.")
        self.assertEqual(resultado.returncode, 0,
                         f"Ensaio isolado falhou na etapa {relatorio.get('stage', 'desconhecida')} "
                         f"({relatorio.get('error', 'erro não identificado')}); saídas brutas omitidas.")
        self.assertEqual(relatorio.get("status"), "ok")
        self.assertEqual(sorted(relatorio.get("checks", [])), sorted(CHECKS_POSTGRESQL))
        self.assertFalse(relatorio.get("production_access"))
        print("\nEnsaio PostgreSQL documentos públicos: " + json.dumps(relatorio, ensure_ascii=True, sort_keys=True))


def _cluster_dos_documentos(bin_dir, work_dir):
    """O auxiliar do processo isolado, criado só nele (não é um caso descoberto pelo unittest)."""
    from decimal import Decimal
    from pathlib import Path
    from tests.test_contratos_postgresql import MIGRATIONS, PASSWORD, _PrivateCluster

    class _ClusterDosDocumentos(_PrivateCluster):
        ADMIN = "admin-documentos@example.invalid"

        def _sql(self, consulta, **parametros):
            from sqlalchemy import text
            with self.app.app_context():
                with self.db.engine.connect() as conexao:
                    return conexao.execute(text(consulta), parametros).all()

        def _executar(self, *comandos):
            from sqlalchemy import text
            with self.app.app_context():
                with self.db.engine.begin() as conexao:
                    for comando in comandos:
                        conexao.execute(text(comando))
                self.db.engine.dispose()  # as conexões novas leem o que mudou (fuso da sessão)

        def _publicar(self, admin_web, tipo, conteudo, versao):
            return admin_web.post("/admin/configuracoes/documentos", data={
                "csrf_token": self._csrf(admin_web, "/admin/configuracoes"), "tipo": tipo, "conteudo": conteudo,
                "versao_exibida": str(versao)})

        def _etapas(self):
            from alembic.script import ScriptDirectory
            from flask_migrate import upgrade
            from models import Plano, Segmento, Usuario

            # O relógio da política avança um dia (v1.1): as sessões do ensaio não podem vencer por isso.
            self.app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=3650)
            self.stage = "migração até o head"
            self._migrar(upgrade, "head")
            self._limpar_cache_esquema()
            self.check("migration_reaches_head", self._revisao() == ScriptDirectory(MIGRATIONS).get_current_head())

            self.stage = "dados fictícios"
            with self._sessao() as sessao:
                plano = Plano(nome_plano="BRONZE", valor_mensal=Decimal("100"), qtd_analises_mes=1,
                              tipo_analise_permitida="MENSAL", nivel_entrega_analise="BASICA",
                              nivel_dashboard="RESUMIDO", nivel_atendimento="BAIXO")
                segmento = Segmento(nome_segmento="Documentos públicos sintéticos")
                admin = Usuario(nome="Admin fictício", email=self.ADMIN, role="ADMIN", primeiro_acesso=False)
                admin.set_senha(PASSWORD)
                sessao.add_all([plano, segmento, admin])
                sessao.commit()
                self.plano_id, self.segmento_id, self.admin_id = plano.id_plano, segmento.id_segmento, admin.id_usuario
            self._nova_empresa("E1", senha=True)
            visitante = self.app.test_client()

            self.stage = "sem publicação"
            paginas = [visitante.get(caminho) for caminho in PAGINAS.values()]
            vazio = visitante.get(VIGENTES)
            self.check("pages_and_json_without_publication",
                       all(p.status_code == 200 and NAO_PUBLICADO in p.get_data(as_text=True) for p in paginas)
                       and (vazio.status_code, vazio.get_json()) == (200, {"documentos": []}))

            self.stage = "publicação da v1.0 pela rota do admin"
            admin_web, resposta = self._login(self.ADMIN)
            publicadas = [self._publicar(admin_web, "TERMOS_USO", TERMOS, 0),
                          self._publicar(admin_web, "POLITICA_PRIVACIDADE", POLITICA, 0)]
            linhas = self._sql("SELECT tipo, id_documento, versao_maior, versao_menor FROM documento_legal ORDER BY tipo")
            ids_v10 = {tipo: identificador for tipo, identificador, _maior, _menor in linhas}
            self.check("v10_published_by_the_admin_route", resposta.status_code == 302
                       and [p.status_code for p in publicadas] == [302, 302]
                       and sorted((tipo, maior, menor) for tipo, _id, maior, menor in linhas)
                       == [("POLITICA_PRIVACIDADE", 1, 0), ("TERMOS_USO", 1, 0)], str(linhas))

            self.stage = "página pública da v1.0"
            politica = visitante.get(PAGINAS["POLITICA_PRIVACIDADE"])
            html = politica.get_data(as_text=True)
            self.check("page_shows_v10_escaped_without_cache", politica.status_code == 200
                       and politica.headers.get("Cache-Control") == "no-store"
                       and "&lt;script&gt;alert(&#39;politica&#39;)&lt;/script&gt;" in html and "<script" not in html
                       and "Versão vigente: v1.0" in html and "02/03/2026 às 10:00" in html)

            self.stage = "JSON com a sessão do banco em outro fuso"
            usuario = self._sql("SELECT current_user")[0][0]
            self._executar(f'ALTER ROLE "{usuario}" SET timezone = \'Asia/Tokyo\'')
            fuso = self._sql("SHOW timezone")[0][0]
            documentos = visitante.get(VIGENTES).get_json()["documentos"]
            self.check("json_contract_in_utc_with_the_session_in_tokyo", fuso == "Asia/Tokyo" and documentos == [
                {"tipo": "TERMOS_USO", "id": ids_v10["TERMOS_USO"], "rotulo": "v1.0",
                 "publicado_em": "2026-03-02T13:00:00+00:00", "conteudo": TERMOS},
                {"tipo": "POLITICA_PRIVACIDADE", "id": ids_v10["POLITICA_PRIVACIDADE"], "rotulo": "v1.0",
                 "publicado_em": "2026-03-02T13:00:00+00:00", "conteudo": POLITICA},
            ], f"{fuso} {documentos}")

            self.stage = "cliente com aceite pendente"
            cliente_web, entrada = self._login("cliente-e1@example.invalid")
            painel = cliente_web.get("/cliente/dashboard").get_data(as_text=True)
            termos = cliente_web.get(PAGINAS["TERMOS_USO"])
            json_cliente = cliente_web.get(VIGENTES)
            self.check("client_with_pending_acceptance_reads_the_pages", entrada.status_code == 302
                       and 'id="modalTermos"' in painel and termos.status_code == 200
                       and 'id="modalTermos"' not in termos.get_data(as_text=True)
                       and json_cliente.status_code == 200 and len(json_cliente.get_json()["documentos"]) == 2)

            self.stage = "revisão v1.1 sem sobrescrever a v1.0"
            antes = self._sql("SELECT conteudo, publicado_em FROM documento_legal WHERE id_documento = :i",
                              i=ids_v10["TERMOS_USO"])
            self.relogio["agora"] = self.relogio["agora"].replace(day=3)
            revisao = self._publicar(admin_web, "TERMOS_USO", "Termos revisados no PostgreSQL.", 1)
            depois = self._sql("SELECT conteudo, publicado_em FROM documento_legal WHERE id_documento = :i",
                               i=ids_v10["TERMOS_USO"])
            contagem = self._sql("SELECT COUNT(*) FROM documento_legal WHERE tipo = 'TERMOS_USO'")[0][0]
            self.check("v11_saved_without_overwriting_v10", revisao.status_code == 302 and antes == depois
                       and contagem == 2, f"{revisao.status_code} {contagem}")

            self.stage = "v1.1 na requisição seguinte"
            pagina = visitante.get(PAGINAS["TERMOS_USO"]).get_data(as_text=True)
            vigentes = visitante.get(VIGENTES).get_json()["documentos"]
            self.check("page_and_json_show_v11_on_the_next_request",
                       "Versão vigente: v1.1" in pagina and "Termos revisados no PostgreSQL." in pagina
                       and "03/03/2026 às 10:00" in pagina
                       and [(d["tipo"], d["rotulo"]) for d in vigentes] == [("TERMOS_USO", "v1.1"), ("POLITICA_PRIVACIDADE", "v1.0")]
                       and vigentes[0]["id"] != ids_v10["TERMOS_USO"]
                       and vigentes[0]["publicado_em"] == "2026-03-03T13:00:00+00:00", str(vigentes))

            self.stage = "tabela ausente com o processo no ar"
            self._executar("ALTER TABLE documento_legal RENAME TO documento_legal_fora")
            falha = visitante.get(PAGINAS["TERMOS_USO"])  # o cache do esquema ainda diz que a tabela existe
            json_falha = visitante.get(VIGENTES)
            portal = cliente_web.get("/cliente/dashboard")  # a trava falha aberta, sem transação abortada
            self.check("missing_table_503_without_aborting_the_next_request", falha.status_code == 503
                       and INDISPONIVEL in falha.get_data(as_text=True)
                       and (json_falha.status_code, json_falha.get_json()) == (503, JSON_INDISPONIVEL)
                       and portal.status_code == 200 and 'id="modalTermos"' not in portal.get_data(as_text=True),
                       f"{falha.status_code} {json_falha.status_code} {portal.status_code}")

            self.stage = "tabela de volta"
            self._executar("ALTER TABLE documento_legal_fora RENAME TO documento_legal")
            self._limpar_cache_esquema()
            de_volta = visitante.get(PAGINAS["TERMOS_USO"])
            self.check("table_back_page_200", de_volta.status_code == 200
                       and "Termos revisados no PostgreSQL." in de_volta.get_data(as_text=True))

    return _ClusterDosDocumentos(Path(bin_dir).resolve(), Path(work_dir).resolve())


def _worker_main(bin_dir, work_dir):
    import json
    cluster = _cluster_dos_documentos(bin_dir, work_dir)
    # Não encaminha mensagens de Flask, SQLAlchemy, Alembic ou PostgreSQL.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            relatorio = cluster.run()
    except Exception as erro:  # noqa: BLE001 - o relatório leva só o tipo (e a verificação que falhou)
        detalhe = f": {erro}" if isinstance(erro, AssertionError) else ""
        print(json.dumps({"status": "error", "stage": cluster.stage, "error": type(erro).__name__ + detalhe}))
        return 1
    print(json.dumps(relatorio))
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == "--isolated-worker":
        raise SystemExit(_worker_main(sys.argv[2], sys.argv[3]))
    unittest.main()
