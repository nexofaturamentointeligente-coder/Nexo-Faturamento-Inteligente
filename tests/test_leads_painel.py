"""Leads do consultor: a tabela, o painel da Avaliação de Cenário, as planilhas da landing e o processamento.

  1. A tabela (GET /admin/leads): empresa (com o selo do site e o protocolo), contato, e-mail (mailto), WhatsApp
     (wa.me), data no horário de Brasília, o aceite dos termos (lido numa consulta só para a lista inteira, com cada
     tipo de lead), a situação e o caixa resgatável, com o "Ver Avaliação de Cenário"; ?ver=<id> abre o painel.
  2. O fragmento do painel (GET /admin/leads/<id>/painel): só o consultor, sem cache, marcado, sem base.html, com o
     diagnóstico completo (o mesmo da página do lead, sem desfoque) e os ids prefixados; o lead pendente; o log.
  3. A página completa do lead continua com o mesmo conteúdo de antes do painel (tests/fixtures/
     lead_antes_do_painel.html, a cópia fiel do template de antes), mais as planilhas do lead da landing.
  4. As planilhas no Storage (um dublê do cliente; nenhum serviço externo): só as do registro conferido viram pedido;
     download avulso e .zip com os cabeçalhos certos; 404, 502 e 503 curtos e sem detalhe interno; log só com ids.
  5. O processamento (POST /admin/leads/<id>/processar): grava a avaliação no próprio lead e volta com o painel
     aberto, sem WhatsApp; vaga ocupada, processo isolado ocupado, sem planilhas, lead convertido ou expirado, sem
     Storage, erro do Storage, do motor e do banco, CSRF e o consultor.
  6. Banco sem as revisões do microsserviço (f2b6d8a4c1e9) e do aceite (c8e2a4f6b0d1): a tela segue, com "—".

Dados fictícios.
"""

import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from unittest.mock import MagicMock, patch
import uuid
import zipfile

from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import OperationalError

import armazenamento_leads
from armazenamento_leads import ErroDeArmazenamento
from tests.deploy_support import ROOT, DeployTestCase
from tests.raio_x_support import avaliar_como_o_worker
from tests.test_avaliacao_cenario import CONTATO, LOTE_MESMOS_MESES
from tests.test_portal_diagnostico import LOTE

PAGINA = "/admin/leads"
# 16/11/2026 às 14:03 no horário de Brasília.
INSTANTE = datetime(2026, 11, 16, 17, 3, tzinfo=timezone.utc)
CHAVE = "sb_secret_EXEMPLO_sintetico_nunca_na_tela"
PROJETO = "abcdefghijklmnopqrst"
XLSX = b"PK\x03\x04planilha-ficticia-de-teste"
SEM_PERIODO = b"Codigo;Produto;Qtde;Valor Total\n1;Cafe;1;10,00\n"
ORIENTACAO = "Configure SUPABASE_KEY no serviço para baixar e processar as planilhas."


class _StorageDuble(armazenamento_leads.ArmazenamentoLeads):
    """O cliente real (com a conferência de tamanho e SHA-256 do baixar) lendo de um dicionário, sem rede."""

    def __init__(self):
        super().__init__(f"https://{PROJETO}.supabase.co", CHAVE, "leads-raio-x")
        self.objetos = {}
        self.pedidos = []
        self.falha = None

    def ler(self, caminho, maximo=None):
        self.pedidos.append(caminho)
        if self.falha is not None:
            raise self.falha
        if caminho not in self.objetos:
            raise ErroDeArmazenamento("o Storage respondeu 404", 404)
        conteudo = self.objetos[caminho]
        if maximo is not None and len(conteudo) > maximo:
            raise ErroDeArmazenamento("o arquivo passa do tamanho permitido")
        return conteudo


def _normalizar(html):
    return re.sub(r">\s+<", "><", re.sub(r"\s+", " ", html)).strip()


def _texto(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _linha(html, nome_loja):
    """A <tr> da tabela de leads com a loja."""
    tabela = html[html.index('id="tabelaLeads"'):]
    tabela = tabela[:tabela.index("</table>")]
    for linha in re.findall(r"<tr>(.*?)</tr>", tabela, re.S):
        if nome_loja in linha:
            return linha
    raise AssertionError(f"{nome_loja} fora da tabela")


def _celulas(linha):
    return re.findall(r"<td\b[^>]*>(.*?)</td>", linha, re.S)


class _PainelCase(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()
        pasta = tempfile.TemporaryDirectory(prefix="nexo-painel-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.worker_chamado = 0
        self.transacao_aberta = None

        def worker(caminhos, limits=None):
            # A transação da requisição termina antes do processo isolado (e antes da rede do Storage).
            self.transacao_aberta = self.db.session().in_transaction()
            self.worker_chamado += 1
            return avaliar_como_o_worker(caminhos, limits)

        self.stack.enter_context(patch("etl_runtime.run_avaliacao_cenario", side_effect=worker))
        self.storage = _StorageDuble()
        self.whatsapp = MagicMock()
        self.stack.enter_context(patch("whatsapp_service.notificador", return_value=self.whatsapp))
        self.login("admin-test@example.invalid")

    # ------------------------------------------------------------ dados
    def com_storage(self):
        self.stack.enter_context(patch("armazenamento_leads.do_app", return_value=self.storage))

    def caminhos(self, *conteudos):
        saida = []
        for conteudo in conteudos:
            caminho = self.pasta / f"arquivo_{uuid.uuid4().hex[:8]}.csv"
            caminho.write_bytes(conteudo)
            saida.append(str(caminho))
        return saida

    def lead_do_consultor(self, *conteudos, **contato):
        import avaliacao_cenario
        resultado = avaliar_como_o_worker(self.caminhos(*(conteudos or LOTE)))
        lead = avaliacao_cenario.registrar(resultado, contato={**CONTATO, **contato}, registrado_por=self.admin_id,
                                           meses_prata=24)
        self.db.session.commit()
        return lead.id

    def lead_simples(self, **campos):
        """Um lead gravado direto pelo ORM, sem avaliação (o consultor por padrão)."""
        from models import LeadAvaliacao
        dados = {"nome_contato": "Contato Simples", "email": f"simples-{uuid.uuid4().hex[:6]}@example.invalid",
                 "whatsapp": None, "nome_loja": f"Loja Simples {uuid.uuid4().hex[:6]}", "criado_em": INSTANTE,
                 "atualizado_em": INSTANTE}
        dados.update(campos)
        lead = LeadAvaliacao(**dados)
        self.db.session.add(lead)
        self.db.session.commit()
        return lead.id

    def documentos(self):
        """Os Termos de Uso v1.0 e a Política de Privacidade v1.1 publicados (os ids)."""
        from models import DocumentoLegal
        if not hasattr(self, "_documentos"):
            termos = DocumentoLegal(tipo="TERMOS_USO", versao=1, versao_maior=1, versao_menor=0,
                                    conteudo="Texto fictício dos termos.", publicado_em=INSTANTE,
                                    id_usuario_admin=self.admin_id)
            politica = DocumentoLegal(tipo="POLITICA_PRIVACIDADE", versao=1, versao_maior=1, versao_menor=1,
                                      conteudo="Texto fictício da política.", publicado_em=INSTANTE,
                                      id_usuario_admin=self.admin_id)
            self.db.session.add_all([termos, politica])
            self.db.session.flush()
            self._documentos = (termos.id_documento, politica.id_documento)
        return self._documentos

    def lead_do_site(self, conteudos=(), *, nomes=None, interesse="raio_x", status_arquivo=None, termos=True,
                     aceite=True, ip="203.0.113.9", extras=(), texto=None, **campos):
        """O lead como o microsserviço do Raio-X grava: pendente, do site, com o interesse, a situação dos arquivos, o
        JSON dos caminhos no Storage, o resumo do aceite e uma linha de aceite por documento. Os relatórios vão para
        o dublê do Storage nos caminhos do microsserviço."""
        from models import AceiteDocumentoLead, LeadAvaliacao
        dados = {"nome_contato": "Contato do Site", "email": "contato.site@example.invalid", "whatsapp": "11912345678",
                 "nome_loja": "Loja do Site Fictícia", "status": "pendente", "origem": "site", "criado_em": INSTANTE,
                 "atualizado_em": INSTANTE}
        dados.update(campos)
        lead = LeadAvaliacao(**dados)
        self.db.session.add(lead)
        self.db.session.flush()
        registros = []
        for indice, conteudo in enumerate(conteudos, 1):
            formato = "xlsx" if conteudo.startswith(b"PK") else "csv"
            sha = hashlib.sha256(conteudo).hexdigest()
            caminho = f"2026/11/{lead.id}/{indice:02d}-{sha[:16]}.{formato}"
            registros.append({"caminho": caminho, "nome": (nomes or {}).get(indice, f"vendas-{indice:02d}.{formato}"),
                              "bytes": len(conteudo), "formato": formato, "sha256": sha})
            self.storage.objetos[caminho] = conteudo
        if texto is None:
            todos = registros + [extra(lead.id) if callable(extra) else extra for extra in extras]
            texto = json.dumps(todos, ensure_ascii=False) if todos else None
        valores = {"interesse": interesse, "arquivos_storage": texto,
                   "status_arquivo": status_arquivo or ("aprovado" if conteudos else "sem_arquivo")}
        if termos:
            valores.update(termos_versao="TU-v1.0 PP-v1.1", termos_aceitos_em=INSTANTE)
        tabela = LeadAvaliacao.__table__
        self.db.session.execute(update(tabela).where(tabela.c.id == lead.id).values(**valores))
        if aceite:
            for id_documento in self.documentos():
                self.db.session.add(AceiteDocumentoLead(id_lead=lead.id, id_documento=id_documento, aceito_em=INSTANTE,
                                                        ip=ip))
        self.db.session.commit()
        return lead.id

    # ------------------------------------------------------------ apoio
    def html(self, caminho, status=200):
        resposta = self.client.get(caminho)
        self.assertEqual(resposta.status_code, status, caminho)
        return resposta.get_data(as_text=True)

    def painel(self, lead_id):
        resposta = self.client.get(f"{PAGINA}/{lead_id}/painel")
        self.assertEqual(resposta.status_code, 200)
        return resposta.get_data(as_text=True)

    def processar(self, lead_id, token=None):
        token = self.csrf_token(PAGINA) if token is None else token
        return self.client.post(f"{PAGINA}/{lead_id}/processar", data={"csrf_token": token} if token else {})

    def flashes(self, resposta):
        """O que a volta (303) mostra: a lista com as mensagens."""
        self.assertEqual(resposta.status_code, 303)
        return self.html(resposta.headers["Location"])

    def contar_linhas_pdv(self, lead_id):
        from models import BrainstormingDadosPdv
        return self.db.session.scalar(select(func.count()).select_from(BrainstormingDadosPdv)
                                      .where(BrainstormingDadosPdv.lead_id == lead_id))

    def recarregar(self, lead_id):
        from models import LeadAvaliacao
        self.db.session.expire_all()
        return self.db.session.get(LeadAvaliacao, lead_id)

    def _migrar(self, comando, revisao):
        from flask_migrate import downgrade, upgrade
        funcao = {"downgrade": downgrade, "upgrade": upgrade}[comando]
        self.db.session.remove()
        with patch("logging.config.fileConfig"), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            funcao(directory=str(ROOT / "migrations"), revision=revisao)


# =============================================================================
# 1. A tabela
# =============================================================================
class ListaTests(_PainelCase):
    def test_colunas_contato_e_botao_ver_avaliacao(self):
        lead_id = self.lead_do_site([LOTE[0]])
        html = self.html(PAGINA)
        tabela = html[html.index('id="tabelaLeads"'):html.index("</thead>")]
        cabecalhos = [_texto(th) for th in re.findall(r"<th\b[^>]*>(.*?)</th>", tabela, re.S)]
        self.assertEqual(cabecalhos, ["Empresa", "Contato", "E-mail", "Telefone", "Data", "Conformidade", "Situação",
                                      "Caixa resgatável", "Ações"])
        empresa, contato, email, telefone, data, conformidade, situacao, caixa, acoes = _celulas(
            _linha(html, "Loja do Site Fictícia"))
        protocolo = str(lead_id).split("-")[0].upper()
        self.assertIn("Loja do Site Fictícia", empresa)
        self.assertIn('<span class="nexo-badge b-purple"><span class="dot"></span>Site</span>', empresa)
        self.assertIn(f"protocolo {protocolo}", empresa)
        self.assertEqual(_texto(contato), "Contato do Site")
        self.assertIn('href="mailto:contato.site@example.invalid"', email)
        # O e-mail longo quebra depois da arroba (<wbr>, macro email).
        self.assertEqual(_texto(email.replace("<wbr>", "")), "contato.site@example.invalid")
        self.assertIn('href="https://wa.me/5511912345678?text=" target="_blank" rel="noopener noreferrer"', telefone)
        self.assertIn("(11) 91234-5678", telefone)
        self.assertIn('<span class="visually-hidden"> de Contato do Site (abre o WhatsApp em nova aba)</span>', telefone)
        self.assertIn('title="16/11/2026 às 14:03 (Horário de Brasília)">16/11/2026 às 14:03</time>', data)
        self.assertIn("Termos aceitos", conformidade)
        for trecho in ("PENDENTE", "Raio-X com relatórios", "relatórios aprovados"):
            self.assertIn(trecho, situacao)
        self.assertEqual(_texto(caixa), "— recuperável")
        botao = re.search(r'<a\b([^>]*)>(.*?)</a>', acoes, re.S)
        atributos = dict(re.findall(r'([\w-]+)="([^"]*)"', botao.group(1)))
        self.assertEqual((atributos["href"], atributos["data-painel-url"]),
                         (f"/admin/leads/{lead_id}", f"/admin/leads/{lead_id}/painel"))
        self.assertIn("btn-nexo", atributos["class"])
        visivel = re.sub(r'<span class="visually-hidden">.*?</span>', "", botao.group(2))
        self.assertEqual(_texto(visivel), "Ver Avaliação de Cenário")
        self.assertIn('<span class="visually-hidden"> de Loja do Site Fictícia</span>', botao.group(2))
        # O painel: um <dialog> só, fechado, com o script próprio (sem script nem manipulador inline).
        self.assertEqual(html.count('<dialog class="painel-lead" id="painelLead"'), 1)
        self.assertIn('<script src="/static/js/leads-painel.js" defer></script>', html)
        self.assertIn('<link href="/static/css/leads-painel.css" rel="stylesheet">', html)
        self.assertNotRegex(html[html.index("<dialog"):html.index("</dialog>")], r"\son[a-z]+=")

    def test_telefone_sem_numero_ou_que_nao_serve_para_o_wame(self):
        self.lead_simples(nome_loja="Loja Sem WhatsApp")
        self.lead_simples(nome_loja="Loja Estrangeira", whatsapp="+12025550100")
        self.lead_simples(nome_loja="Loja Com DDI", whatsapp="+5562988887766")
        html = self.html(PAGINA)
        self.assertEqual(_texto(_celulas(_linha(html, "Loja Sem WhatsApp"))[3]), "—")
        celula = _celulas(_linha(html, "Loja Estrangeira"))[3]
        self.assertNotIn("wa.me", celula)
        self.assertIn("+12025550100", celula)
        celula = _celulas(_linha(html, "Loja Com DDI"))[3]
        self.assertIn('href="https://wa.me/5562988887766?text="', celula)
        self.assertIn("(62) 98888-7766", celula)

    def test_conformidade_de_cada_tipo_de_lead(self):
        self.lead_do_site(nome_loja="Loja Com Aceite", email="aceite@example.invalid")
        self.lead_do_site(nome_loja="Loja Só Com O Resumo", email="resumo@example.invalid", aceite=False)
        self.lead_do_site(nome_loja="Loja Sem Nada", email="nada@example.invalid", aceite=False, termos=False)
        self.lead_simples(nome_loja="Loja Do Consultor", consentimento_em=INSTANTE)
        self.lead_simples(nome_loja="Loja Do Raio-X Público", origem="site", consentimento_em=INSTANTE,
                          versao_autorizacao="autorizacao-relatorios-2026-11-15")
        html = self.html(PAGINA)

        def conformidade(loja):
            return _texto(_celulas(_linha(html, loja))[5])

        self.assertEqual(conformidade("Loja Com Aceite"), "Termos aceitos Termos de Uso v1.0 · Política de "
                                                          "Privacidade v1.1 IP 203.0.113.9 · 16/11/2026 às 14:03")
        self.assertEqual(conformidade("Loja Só Com O Resumo"),
                         "Termos aceitos TU-v1.0 PP-v1.1 IP não registrado · 16/11/2026 às 14:03")
        self.assertEqual(conformidade("Loja Sem Nada"), "Sem aceite registrado")
        self.assertEqual(conformidade("Loja Do Consultor"), "Autorização do consultor Confirmada em 16/11/2026 às 14:03")
        self.assertEqual(conformidade("Loja Do Raio-X Público"), "Sem aceite dos termos Autorização dos relatórios: "
                                                                 "autorizacao-relatorios-2026-11-15 16/11/2026 às 14:03")

    def test_aceite_e_dados_do_site_numa_consulta_so(self):
        ids = [self.lead_do_site([f"relatorio {numero}".encode()], nome_loja=f"Loja {numero}",
                                 email=f"loja{numero}@example.invalid", ip=f"203.0.113.{numero + 1}")
               for numero in range(6)]
        self.lead_simples(nome_loja="Loja Do Consultor")
        consultas = []

        def ouvir(_conexao, _cursor, sql, *_resto):
            consultas.append(sql)

        event.listen(self.db.engine, "before_cursor_execute", ouvir)
        self.addCleanup(event.remove, self.db.engine, "before_cursor_execute", ouvir)
        protocolo = str(ids[2]).split("-")[0]
        for caminho, lojas in ((PAGINA, range(6)), (f"{PAGINA}?protocolo={protocolo}", (2,))):
            with self.subTest(caminho=caminho):
                consultas.clear()
                html = self.html(caminho)
                aceites = [sql for sql in consultas if re.search(r"\bFROM aceite_documento_lead\b", sql)]
                colunas = [sql for sql in consultas if "lead_avaliacao.arquivos_storage" in sql]
                self.assertEqual((len(aceites), len(colunas)), (1, 1), consultas)
                for numero in lojas:
                    self.assertIn(f"IP 203.0.113.{numero + 1}", _celulas(_linha(html, f"Loja {numero}"))[5])

    def test_ver_na_consulta_abre_o_painel(self):
        lead_id = self.lead_do_site()
        html = self.html(f"{PAGINA}?ver={str(lead_id).upper()}")
        self.assertIn(f'data-painel-abrir="/admin/leads/{lead_id}/painel"', html)
        dialogo = html[html.index('id="painelLead"'):]
        self.assertIn(f'href="/admin/leads/{lead_id}" data-painel-pagina', dialogo)
        for invalido in ("x", "../admin", "1" * 100, ""):
            with self.subTest(ver=invalido):
                self.assertNotIn("data-painel-abrir", self.html(f"{PAGINA}?ver={invalido}"))

    def test_chave_do_storage_nunca_vai_para_a_tela(self):
        lead_id = self.lead_do_site([LOTE[0]])
        self.app.config["SUPABASE_KEY"] = CHAVE
        self.com_storage()
        for caminho in (PAGINA, f"{PAGINA}/{lead_id}", f"{PAGINA}/{lead_id}/painel", f"{PAGINA}?ver={lead_id}"):
            with self.subTest(caminho=caminho):
                html = self.html(caminho)
                self.assertNotIn(CHAVE, html)
                self.assertNotIn("supabase.co", html)
                self.assertNotIn("2026/11/", html)


# =============================================================================
# 2. O fragmento do painel
# =============================================================================
class FragmentoTests(_PainelCase):
    def test_fragmento_com_o_diagnostico_completo_sem_cache_e_sem_base(self):
        lead_id = self.lead_do_consultor()
        resposta = self.client.get(f"{PAGINA}/{lead_id}/painel")
        self.assertEqual((resposta.status_code, resposta.mimetype), (200, "text/html"))
        self.assertEqual((resposta.headers["Cache-Control"], resposta.headers["X-Nexo-Painel"]), ("no-store", "1"))
        html = resposta.get_data(as_text=True)
        for de_pagina in ("<html", "<!DOCTYPE", "<head", "<body", 'class="sidebar"', "<script", "<dialog"):
            self.assertNotIn(de_pagina, html)
        self.assertRegex(html, rf'data-painel-fragmento data-painel-titulo="Mercado fictício" '
                               rf'data-painel-pagina="/admin/leads/{lead_id}"')
        # O diagnóstico inteiro, o mesmo da página do lead: indicadores, caixa, triagem, estoque, simulador e itens.
        for trecho in ("Saúde do cadastro", "Potencial recuperável", "Plano sugerido", "Itens sem custo em nenhuma venda",
                       "Itens com alguma venda abaixo do custo", "Itens com omissão no ERP",
                       "Resumo executivo: caixa resgatável", "Matriz de triagem", "Matriz por categoria",
                       "Estoque parado e excedente", "Simulador de liberação de caixa (Urgência)", "Leite 1L",
                       "Contato fictício", 'href="mailto:lead@example.invalid"', "https://wa.me/5511900000000"):
            self.assertIn(trecho, html)
        # Sem o desfoque nem o bloqueio da vitrine do Raio-X público.
        for bloqueio in ("publica-borrao", "publica-bloqueado", "Conteúdo bloqueado", "blur", "data-nexo-convite"):
            self.assertNotIn(bloqueio, html)
        # O lead do consultor não tem planilhas da landing nem o formulário de refazer.
        self.assertNotIn("Planilhas enviadas pelo lead", html)
        self.assertNotIn("Refazer a avaliação", html)
        self.assertIn("Autorização do consultor", html)

    def test_triagem_marcas_saude_e_plano_que_sairam_da_lista_estao_no_painel(self):
        from models import LeadAvaliacao
        lead_id = self.lead_do_consultor()
        lead = self.db.session.get(LeadAvaliacao, lead_id)
        html = self.painel(lead_id)
        for bloco, rotulo in (("urgencia", "Urgência"), ("atencao", "Atenção"), ("eletiva", "Eletiva")):
            itens = json.loads(getattr(lead, f"resumo_{bloco}_json"))["itens"]
            self.assertRegex(html, rf'<a href="#painel-bloco{bloco}">{rotulo}</a></td>\s*'
                                   rf'<td class="text-end fw-num">{itens}</td>')
        for rotulo, valor in (("Itens sem custo em nenhuma venda", lead.itens_venda_sem_custo),
                              ("Itens com alguma venda abaixo do custo", lead.itens_venda_abaixo_custo),
                              ("Itens com omissão no ERP", lead.itens_omissao_erp)):
            self.assertRegex(html, rf'(?s){rotulo}</span>.*?<div class="kpi-value">{valor}</div>')
        saude = f"{float(lead.score_saude_cadastral):.1f}".replace(".", ",")
        self.assertIn(f'<div class="kpi-value">{saude}<small> / 100</small></div>', html)
        self.assertIn(f"plan-{lead.plano_recomendado_codigo.lower()}", html)

    def test_fragmento_igual_ao_diagnostico_da_pagina_completa(self):
        lead_id = self.lead_do_consultor()
        fragmento = _normalizar(self.painel(lead_id))
        pagina = _normalizar(self.html(f"{PAGINA}/{lead_id}"))
        # Depois do cabeçalho do lead (a primeira seção) vem o diagnóstico, até o fim do fragmento.
        inicio = fragmento.index("</section>") + len("</section>")
        diagnostico = fragmento[inicio:fragmento.rindex("</div>")].replace("painel-", "")
        self.assertGreater(len(diagnostico), 5000)
        self.assertIn(diagnostico, pagina)

    def test_ids_do_fragmento_nao_colidem_com_os_da_lista(self):
        lead_id = self.lead_do_site(list(LOTE))
        self.com_storage()
        self.assertEqual(self.processar(lead_id).status_code, 303)
        fragmento = self.painel(lead_id)
        lista = self.html(PAGINA)
        ids = re.findall(r'\bid="([^"]+)"', fragmento)
        self.assertGreater(len(ids), 15)
        self.assertEqual([i for i in ids if not i.startswith("painel-")], [])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids) & set(re.findall(r'\bid="([^"]+)"', lista)), set())
        # Toda referência do fragmento (rótulo, descrição, âncora) aponta para um id dele.
        referencias = re.findall(r'\b(?:for|aria-labelledby|aria-describedby)="([^"]+)"', fragmento)
        referencias += re.findall(r'href="#([^"]+)"', fragmento)
        for referencia in referencias:
            for alvo in referencia.split():
                self.assertIn(alvo, ids)

    def test_lead_pendente_sem_diagnostico(self):
        lead_id = self.lead_do_site(list(LOTE), nomes={1: "Vendas <Novembro>.csv"})
        sem_storage = self.painel(lead_id)
        self.assertIn("Avaliação de Cenário ainda não processada.", sem_storage)
        self.assertIn("Planilhas enviadas pelo lead", sem_storage)
        self.assertIn("Vendas &lt;Novembro&gt;.csv", sem_storage)
        self.assertNotIn("Vendas <Novembro>", sem_storage)
        for numeros in ("Matriz de triagem", "Dados de PDV indisponíveis", "Saúde do cadastro"):
            self.assertNotIn(numeros, sem_storage)
        self.assertIn(ORIENTACAO, sem_storage)
        self.assertEqual(sem_storage.count('disabled aria-describedby="painel-planilhasSemStorage"'), 6)
        self.com_storage()
        html = self.painel(lead_id)
        self.assertNotIn(ORIENTACAO, html)
        formulario = re.search(r"<form\b[^>]*processar[^>]*>.*?</form>", html, re.S).group(0)
        for trecho in (f'action="/admin/leads/{lead_id}/processar"', "data-envio-unico", "data-envio-longo",
                       "data-processar-planilhas", 'name="csrf_token"', "Processar relatórios do lead"):
            self.assertIn(trecho, formulario)
        self.assertIn(f'href="/admin/leads/{lead_id}/arquivos.zip" download', html)
        self.assertIn("Baixar planilhas originais", html)
        self.assertEqual(len(re.findall(rf'href="/admin/leads/{lead_id}/arquivos/\d+" download', html)), 4)

    def test_so_o_consultor_acessa(self):
        # Sem documento publicado: o cliente não fica parado no aceite dos termos e chega à checagem do papel.
        lead_id = self.lead_do_site([LOTE[0]], aceite=False)
        self.logout()
        anonimo = self.client.get(f"{PAGINA}/{lead_id}/painel")
        self.assertEqual(anonimo.status_code, 302)
        self.assertIn("/auth/login", anonimo.headers["Location"])
        self.login()
        cliente = self.client.get(f"{PAGINA}/{lead_id}/painel")
        self.assertEqual(cliente.status_code, 403)
        for resposta in (anonimo, cliente):
            self.assertNotIn("X-Nexo-Painel", resposta.headers)
            self.assertNotIn("Loja do Site Fictícia", resposta.get_data(as_text=True))

    def test_lead_inexistente_e_banco_sem_as_migracoes(self):
        inexistente = self.client.get(f"{PAGINA}/{uuid.uuid4()}/painel")
        self.assertEqual(inexistente.status_code, 404)
        self.assertNotIn("X-Nexo-Painel", inexistente.headers)
        lead_id = self.lead_do_site([LOTE[0]])
        with patch("blueprints.leads.esquema_disponivel", return_value=False):
            resposta = self.client.get(f"{PAGINA}/{lead_id}/painel")
        self.assertEqual((resposta.status_code, resposta.headers["Retry-After"], resposta.headers["X-Nexo-Painel"],
                          resposta.headers["Cache-Control"]), (503, "30", "1", "no-store"))
        html = resposta.get_data(as_text=True)
        self.assertIn("Falta aplicar as migrações da Avaliação de Cenário.", html)
        self.assertNotIn("Loja do Site Fictícia", html)

    def test_leitura_no_log_so_com_ids(self):
        lead_id = self.lead_do_consultor()
        with patch.object(self.app.logger, "disabled", False), self.assertLogs(self.app.logger, "INFO") as registro:
            self.painel(lead_id)
        mensagens = [r.getMessage() for r in registro.records]
        self.assertIn(f"Diagnóstico do lead {lead_id} lido pelo consultor {self.admin_id}.", mensagens)
        for mensagem in mensagens:
            for dado in (*CONTATO.values(), "Leite 1L"):
                self.assertNotIn(dado, mensagem)


# =============================================================================
# 3. A página completa do lead
# =============================================================================
class PaginaCompletaTests(_PainelCase):
    def _antes_e_depois(self, lead_id, consulta=""):
        """A página do lead pelo template de antes do painel (a fixture) e pelo de agora, com o MESMO contexto e na
        mesma requisição (o mesmo token CSRF e o mesmo nonce)."""
        from flask import render_template, render_template_string, request, url_for
        from flask_login import login_user
        import blueprints.leads as rotas
        from models import LeadAvaliacao, Usuario
        antes_fonte = (ROOT / "tests" / "fixtures" / "lead_antes_do_painel.html").read_text(encoding="utf-8")
        with self.app.test_request_context(f"{PAGINA}/{lead_id}{consulta}"):
            login_user(self.db.session.get(Usuario, self.admin_id))
            lead = self.db.session.get(LeadAvaliacao, lead_id)
            # Como a rota do detalhe: parâmetros inválidos do simulador viram o aviso, com os padrão.
            try:
                parametros, erro_parametros = rotas._parametros_da_consulta(request.args), None
            except ValueError as erro:
                parametros, erro_parametros = None, str(erro)
            contexto = rotas._contexto_detalhe(lead, parametros, erro_parametros)
            antes = render_template_string(antes_fonte, **contexto)
            depois = render_template("admin/lead.html", **contexto)
            novidades = (f'<link href="{url_for("static", filename="css/leads-painel.css")}" rel="stylesheet">',
                         f'<script src="{url_for("static", filename="js/leads-painel.js")}" defer></script>')
        for novidade in novidades:
            self.assertEqual(depois.count(novidade), 1)
            depois = depois.replace(novidade, "")
        return antes, depois

    def test_pagina_do_lead_igual_a_de_antes_do_painel(self):
        from models import LeadAvaliacao
        com_estoque = self.lead_do_consultor()
        sem_estoque = self.lead_do_consultor(*LOTE_MESMOS_MESES, email="outro@example.invalid")
        triagem_antiga = self.lead_do_consultor(email="antiga@example.invalid")
        self.db.session.get(LeadAvaliacao, triagem_antiga).versao_triagem = "triagem-anterior"
        expirado = self.lead_do_consultor(email="expirado@example.invalid")
        self.db.session.get(LeadAvaliacao, expirado).status = "expirado"
        self.db.session.commit()
        casos = {"com estoque": (com_estoque, ""), "auditoria de preço": (com_estoque, "?markup_informado=0.9"),
                 "simulador inválido": (com_estoque, "?descontos=abc"), "sem estoque": (sem_estoque, ""),
                 "triagem de outra versão": (triagem_antiga, ""), "dados expirados": (expirado, "")}
        for caso, (lead_id, consulta) in casos.items():
            with self.subTest(caso=caso):
                antes, depois = self._antes_e_depois(lead_id, consulta)
                self.assertGreater(len(antes), 20000)
                self.assertEqual(_normalizar(depois), _normalizar(antes))
        # As seções que a comparação cobriu (com o markup, a auditoria de preço aparece).
        antes, _depois = self._antes_e_depois(com_estoque, "?markup_informado=0.9")
        for secao in ("caixaTitulo", "triagemTitulo", "matrizTitulo", "estoqueTitulo", "simuladorTitulo",
                      "auditoriaTitulo", "blocourgencia", "refazerTitulo"):
            self.assertIn(f'id="{secao}"', antes)

    def test_pagina_do_lead_do_site_ganha_so_as_planilhas(self):
        lead_id = self.lead_do_site([LOTE[0], XLSX], nomes={1: "<b>vendas</b>.csv"})
        antes, depois = self._antes_e_depois(lead_id)
        secao = re.compile(r'<section class="nexo-card mb-4 planilhas-lead".*?</section>', re.S)
        self.assertEqual(len(secao.findall(depois)), 1)
        self.assertEqual(_normalizar(secao.sub("", depois)), _normalizar(antes))
        # O aceite dos termos (o bloco de antes) continua lá.
        self.assertIn('id="aceiteTitulo"', antes)
        planilhas = secao.search(depois).group(0)
        for trecho in ("Planilhas enviadas pelo lead", "Raio-X com relatórios · relatórios aprovados",
                       "&lt;b&gt;vendas&lt;/b&gt;.csv", f"CSV · {len(LOTE[0])} bytes", f"XLSX · {len(XLSX)} bytes",
                       ORIENTACAO):
            self.assertIn(trecho, planilhas)
        self.assertNotIn("<b>vendas", planilhas)


# =============================================================================
# 4. As planilhas no Storage
# =============================================================================
class PlanilhasTests(_PainelCase):
    def test_sem_storage_configurado_botoes_desabilitados_e_503(self):
        # No SQLite dos testes não há Storage do Supabase, com ou sem chave: do_app devolve None.
        self.app.config["SUPABASE_KEY"] = CHAVE
        lead_id = self.lead_do_site([LOTE[0]])
        pagina = self.html(f"{PAGINA}/{lead_id}")
        self.assertIn(f'<p class="field-hint mt-2 mb-0" id="planilhasSemStorage">{ORIENTACAO}</p>', pagina)
        self.assertNotIn("/arquivos", pagina)
        for caminho in (f"{PAGINA}/{lead_id}/arquivos/1", f"{PAGINA}/{lead_id}/arquivos.zip"):
            with self.subTest(caminho=caminho):
                resposta = self.client.get(caminho)
                self.assertEqual((resposta.status_code, resposta.mimetype), (503, "text/plain"))
                self.assertEqual(resposta.get_data(as_text=True), ORIENTACAO)
        self.assertIn(ORIENTACAO, self.flashes(self.processar(lead_id)))
        self.assertEqual((self.storage.pedidos, self.worker_chamado), ([], 0))

    def test_registro_fora_do_formato_nunca_vira_pedido(self):
        outro = uuid.uuid4()
        ruins = [
            lambda lead: {"caminho": f"2026/11/{outro}/03-{'a' * 16}.csv", "nome": "de outro lead.csv", "bytes": 5,
                          "formato": "csv", "sha256": "a" * 64},
            lambda lead: {"caminho": f"2026/11/{lead}/../{outro}/04-{'b' * 16}.csv", "nome": "volta.csv", "bytes": 5,
                          "formato": "csv", "sha256": "b" * 64},
            lambda lead: {"caminho": f"2026/11/{lead}/05-{'c' * 16}.exe", "nome": "programa.exe", "bytes": 5,
                          "formato": "exe", "sha256": "c" * 64},
            lambda lead: {"caminho": f"2026/11/{lead}/06-{'d' * 16}.csv", "nome": "vazio.csv", "bytes": 0,
                          "formato": "csv", "sha256": "d" * 64},
        ]
        self.com_storage()
        lead_id = self.lead_do_site([LOTE[0], LOTE[1]], extras=ruins)
        for extra in ruins:
            self.storage.objetos[extra(lead_id)["caminho"]] = b"nunca"
        html = self.painel(lead_id)
        for nome in ("de outro lead.csv", "volta.csv", "programa.exe", "vazio.csv"):
            self.assertNotIn(nome, html)
        self.assertEqual(len(re.findall(r'/arquivos/\d+" download', html)), 2)
        for indice in (3, 4, 5, 6):
            self.assertEqual(self.client.get(f"{PAGINA}/{lead_id}/arquivos/{indice}").status_code, 404)
        self.assertEqual(self.storage.pedidos, [])
        resposta = self.client.get(f"{PAGINA}/{lead_id}/arquivos.zip")
        self.assertEqual(resposta.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resposta.data)) as pacote:
            self.assertEqual(pacote.namelist(), ["vendas-01.csv", "vendas-02.csv"])
        self.assertEqual(self.flashes(self.processar(lead_id)).count("Avaliação concluída: 2 relatório(s) de vendas."), 2)
        validos = [registro["caminho"] for registro in json.loads(self.db.session.execute(
            text("SELECT arquivos_storage FROM lead_avaliacao WHERE id = :id"), {"id": lead_id.hex}).scalar())[:2]]
        self.assertEqual(self.storage.pedidos, validos * 2)

    def test_json_ruim_e_indice_inexistente(self):
        self.com_storage()
        ruim = self.lead_do_site(texto="isto não é JSON", nome_loja="Loja Do JSON Ruim", email="ruim@example.invalid")
        html = self.painel(ruim)
        self.assertIn("Nenhuma planilha disponível: o registro dos arquivos deste lead não confere e nada foi pedido "
                      "ao Storage.", html)
        for caminho in (f"{PAGINA}/{ruim}/arquivos/1", f"{PAGINA}/{ruim}/arquivos.zip"):
            self.assertEqual(self.client.get(caminho).status_code, 404)
        self.assertIn("Este lead não tem planilhas válidas no Storage para processar.", self.flashes(self.processar(ruim)))
        bom = self.lead_do_site([LOTE[0]])
        for indice in ("0", "2", "99", "-1", "x"):
            self.assertEqual(self.client.get(f"{PAGINA}/{bom}/arquivos/{indice}").status_code, 404)
        self.assertEqual(self.client.get(f"{PAGINA}/{uuid.uuid4()}/arquivos/1").status_code, 404)
        self.assertEqual(self.storage.pedidos, [])
        # O lead que pediu ajuda (sem relatórios) diz isso no painel.
        ajuda = self.lead_do_site(interesse="ajuda_relatorios", nome_loja="Loja Da Ajuda", email="ajuda@example.invalid")
        self.assertIn('O lead não enviou relatórios: escolheu "Pediu ajuda com os relatórios" no pré-cadastro do site.',
                      self.painel(ajuda))

    def test_download_avulso_com_os_cabecalhos(self):
        self.com_storage()
        lead_id = self.lead_do_site([LOTE[0], XLSX], nomes={1: "Vendas Janeiro <2024>.csv", 2: "Resumo ç.xlsx"})
        with patch.object(self.app.logger, "disabled", False), self.assertLogs(self.app.logger, "INFO") as registro:
            resposta = self.client.get(f"{PAGINA}/{lead_id}/arquivos/1")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.data, LOTE[0])
        self.assertEqual(resposta.headers["Content-Type"], "text/csv")
        self.assertEqual(resposta.headers["Content-Disposition"],
                         "attachment; filename=\"Vendas_Janeiro_2024.csv\"; filename*=UTF-8''Vendas_Janeiro_2024.csv")
        self.assertEqual((resposta.headers["X-Content-Type-Options"], resposta.headers["Cache-Control"]),
                         ("nosniff", "no-store"))
        mensagens = [r.getMessage() for r in registro.records]
        self.assertIn(f"Planilha 1 do lead {lead_id} baixada pelo consultor {self.admin_id}.", mensagens)
        for mensagem in mensagens:
            for dado in ("Vendas Janeiro", "2026/11/", "Contato do Site", CHAVE):
                self.assertNotIn(dado, mensagem)
        xlsx = self.client.get(f"{PAGINA}/{lead_id}/arquivos/2")
        self.assertEqual((xlsx.status_code, xlsx.data), (200, XLSX))
        self.assertEqual(xlsx.headers["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn('filename="Resumo_c.xlsx"', xlsx.headers["Content-Disposition"])

    def test_lead_expirado_nao_baixa_as_planilhas(self):
        # LGPD: passada a retenção, nem a planilha original sai (404, nenhum pedido ao Storage, nenhum link na tela).
        self.com_storage()
        lead_id = self.lead_do_site([LOTE[0], LOTE[1]], status="expirado")
        for caminho in (f"{PAGINA}/{lead_id}/arquivos/1", f"{PAGINA}/{lead_id}/arquivos.zip"):
            with self.subTest(caminho=caminho):
                self.assertEqual(self.client.get(caminho).status_code, 404)
        self.assertEqual(self.storage.pedidos, [])
        for html in (self.html(f"{PAGINA}/{lead_id}"), self.painel(lead_id)):
            self.assertNotIn("/arquivos", html)
            self.assertNotIn("Baixar planilhas originais", html)
            self.assertIn("as planilhas não são baixadas nem processadas de novo", html)

    def test_zip_com_todas_e_nomes_unicos(self):
        self.com_storage()
        lead_id = self.lead_do_site([LOTE[0], LOTE[1], XLSX], nomes={1: "vendas.csv", 2: "vendas.csv", 3: "CON.xlsx"})
        with patch.object(self.app.logger, "disabled", False), self.assertLogs(self.app.logger, "INFO") as registro:
            resposta = self.client.get(f"{PAGINA}/{lead_id}/arquivos.zip")
        self.assertEqual(resposta.status_code, 200)
        protocolo = str(lead_id).split("-")[0].upper()
        self.assertEqual(resposta.headers["Content-Type"], "application/zip")
        self.assertEqual(resposta.headers["Content-Disposition"], f"attachment; filename=\"planilhas-lead-{protocolo}.zip\"; "
                                                                  f"filename*=UTF-8''planilhas-lead-{protocolo}.zip")
        self.assertEqual((resposta.headers["X-Content-Type-Options"], resposta.headers["Cache-Control"]),
                         ("nosniff", "no-store"))
        with zipfile.ZipFile(io.BytesIO(resposta.data)) as pacote:
            self.assertEqual(pacote.namelist(), ["vendas.csv", "vendas-2.csv", "relatorio-03.xlsx"])
            self.assertEqual([pacote.read(nome) for nome in pacote.namelist()], [LOTE[0], LOTE[1], XLSX])
        self.assertIn(f"Planilhas do lead {lead_id} (3 arquivo(s)) baixadas em .zip pelo consultor {self.admin_id}.",
                      [r.getMessage() for r in registro.records])

    def test_erro_do_storage_responde_502_sem_detalhe(self):
        self.com_storage()
        lead_id = self.lead_do_site([LOTE[0], LOTE[1]])
        self.storage.falha = ErroDeArmazenamento("o Storage respondeu 500", 500)
        with patch.object(self.app.logger, "disabled", False), self.assertLogs(self.app.logger, "WARNING") as registro:
            respostas = [self.client.get(f"{PAGINA}/{lead_id}/arquivos/1"), self.client.get(f"{PAGINA}/{lead_id}/arquivos.zip")]
        for resposta in respostas:
            self.assertEqual((resposta.status_code, resposta.mimetype), (502, "text/plain"))
            corpo = resposta.get_data(as_text=True)
            self.assertEqual(corpo, "Não foi possível baixar as planilhas do lead no Storage agora. Tente de novo em "
                                    "instantes.")
        do_storage = [r.getMessage() for r in registro.records if "Storage" in r.getMessage()]
        self.assertEqual(len(do_storage), 2)
        for mensagem in do_storage:
            self.assertIn(f"lead {lead_id}", mensagem)
            self.assertIn("(o Storage respondeu 500)", mensagem)
            for segredo in (CHAVE, "2026/11/", PROJETO, "vendas-01"):
                self.assertNotIn(segredo, mensagem)
        # O conteúdo que não bate com o registrado também não sai.
        self.storage.falha = None
        caminho = next(iter(self.storage.objetos))
        self.storage.objetos[caminho] = LOTE[0][:-1] + b"9"
        self.assertEqual(self.client.get(f"{PAGINA}/{lead_id}/arquivos/1").status_code, 502)

    def test_downloads_so_para_o_consultor(self):
        self.com_storage()
        lead_id = self.lead_do_site([LOTE[0]], aceite=False)
        self.logout()
        for caminho in (f"{PAGINA}/{lead_id}/arquivos/1", f"{PAGINA}/{lead_id}/arquivos.zip"):
            resposta = self.client.get(caminho)
            self.assertEqual(resposta.status_code, 302)
            self.assertIn("/auth/login", resposta.headers["Location"])
        self.login()
        for caminho in (f"{PAGINA}/{lead_id}/arquivos/1", f"{PAGINA}/{lead_id}/arquivos.zip"):
            self.assertEqual(self.client.get(caminho).status_code, 403)
        self.assertEqual(self.storage.pedidos, [])


# =============================================================================
# 5. O processamento das planilhas
# =============================================================================
class ProcessarTests(_PainelCase):
    def test_processa_as_planilhas_e_volta_com_o_painel(self):
        from models import AceiteDocumentoLead
        self.com_storage()
        lead_id = self.lead_do_site(list(LOTE))
        antes = self.painel(lead_id)
        self.assertIn("Processar relatórios do lead", antes)
        resposta = self.processar(lead_id)
        self.assertEqual(resposta.status_code, 303, resposta.get_data(as_text=True)[:400])
        self.assertEqual(resposta.headers["Location"], f"/admin/leads?ver={lead_id}")
        lista = self.html(resposta.headers["Location"])
        # A mensagem no topo da lista e dentro do painel, que abre sozinho na volta.
        self.assertEqual(lista.count("Avaliação concluída: 4 relatório(s) de vendas."), 2)
        self.assertIn(f'data-painel-abrir="/admin/leads/{lead_id}/painel"', lista)
        lead = self.recarregar(lead_id)
        self.assertEqual((lead.status, lead.arquivos, lead.origem, lead.registrado_por), ("concluido", 4, "site", self.admin_id))
        self.assertIsNotNone(lead.vazamento_total_reais)
        self.assertGreater(self.contar_linhas_pdv(lead_id), 0)
        # O que o microsserviço gravou continua: o resumo do aceite e as linhas de aceite.
        self.assertEqual(tuple(self.db.session.execute(text(
            "SELECT termos_versao, interesse FROM lead_avaliacao WHERE id = :id"), {"id": lead_id.hex}).one()),
            ("TU-v1.0 PP-v1.1", "raio_x"))
        self.assertEqual(self.db.session.scalar(select(func.count()).select_from(AceiteDocumentoLead)
                                                .where(AceiteDocumentoLead.id_lead == lead_id)), 2)
        self.assertEqual((self.worker_chamado, self.transacao_aberta), (1, False))
        self.assertEqual(len(self.storage.pedidos), 4)
        # Nenhuma mensagem ao lead.
        self.assertEqual(self.whatsapp.method_calls, [])
        painel = self.painel(lead_id)
        for trecho in ("Matriz de triagem", "Processar de novo", "Resumo executivo: caixa resgatável"):
            self.assertIn(trecho, painel)
        self.assertNotIn("ainda não processada", painel)
        # Na lista, o número da negociação.
        caixa = _texto(_celulas(_linha(self.html(PAGINA), "Loja do Site Fictícia"))[7])
        self.assertTrue(caixa.startswith("R$ "), caixa)
        self.assertIn("parado há 60 dias ou mais", caixa)

    def test_relatorio_repetido_vai_uma_vez(self):
        self.com_storage()
        lead_id = self.lead_do_site([LOTE[0], LOTE[1], LOTE[0], LOTE[2], LOTE[3]])
        self.assertIn("Avaliação concluída: 4 relatório(s) de vendas.", self.flashes(self.processar(lead_id)))
        self.assertEqual(len(self.storage.pedidos), 4)

    def test_vaga_ocupada_responde_503_sem_baixar(self):
        import etl_uploads
        self.com_storage()
        lead_id = self.lead_do_site(list(LOTE))
        token = self.csrf_token(PAGINA)
        self.assertTrue(etl_uploads._UPLOAD_ADMISSION.acquire(blocking=False))
        try:
            resposta = self.processar(lead_id, token)
        finally:
            etl_uploads._UPLOAD_ADMISSION.release()
        self.assertEqual((resposta.status_code, resposta.headers.get("Retry-After")), (503, "30"))
        html = resposta.get_data(as_text=True)
        self.assertIn("nenhum arquivo foi lido", html)
        self.assertIn(f'data-painel-abrir="/admin/leads/{lead_id}/painel"', html)
        self.assertEqual((self.storage.pedidos, self.worker_chamado), ([], 0))
        self.assertEqual(self.recarregar(lead_id).status, "pendente")

    def test_processo_isolado_ocupado_tambem_responde_503(self):
        from etl_inputs import ETLInputError
        self.com_storage()
        lead_id = self.lead_do_site(list(LOTE))
        with patch("etl_runtime.run_avaliacao_cenario", side_effect=ETLInputError("busy", "Processador ocupado.")):
            resposta = self.processar(lead_id)
        self.assertEqual((resposta.status_code, resposta.headers.get("Retry-After")), (503, "30"))
        self.assertIn("Aguarde alguns segundos e envie de novo", resposta.get_data(as_text=True))
        self.assertEqual((self.recarregar(lead_id).status, self.contar_linhas_pdv(lead_id)), ("pendente", 0))

    def test_sem_planilhas_lead_convertido_expirado_ou_sem_storage(self):
        so_contato = self.lead_do_site(interesse="so_contato", nome_loja="Loja Só Contato", email="so@example.invalid")
        convertido = self.lead_do_site([LOTE[0]], status="convertido", nome_loja="Loja Convertida",
                                       email="convertida@example.invalid")
        expirado = self.lead_do_site([LOTE[1]], status="expirado", nome_loja="Loja Expirada",
                                     email="expirada@example.invalid")
        sem_storage = self.lead_do_site([LOTE[2]], nome_loja="Loja Sem Storage", email="sem@example.invalid")
        self.assertIn(ORIENTACAO, self.flashes(self.processar(sem_storage)))
        self.com_storage()
        casos = {so_contato: "Este lead não tem planilhas válidas no Storage para processar.",
                 convertido: "Lead já convertido em cliente: as planilhas não são processadas de novo.",
                 expirado: "Os dados deste lead passaram dos 30 dias de retenção: as planilhas não são processadas"}
        for lead_id, mensagem in casos.items():
            with self.subTest(mensagem=mensagem):
                self.assertIn(mensagem, self.flashes(self.processar(lead_id)))
        avisos = {convertido: "as planilhas não são processadas de novo",
                  # Expirado: também sem baixar (LGPD), e a frase diz isso.
                  expirado: "as planilhas não são baixadas nem processadas de novo"}
        for lead_id, aviso in avisos.items():
            painel = self.painel(lead_id)
            self.assertNotIn("Processar", painel)
            self.assertIn(aviso, painel)
        self.assertEqual(self.processar(uuid.uuid4()).status_code, 404)
        self.assertEqual((self.storage.pedidos, self.worker_chamado), ([], 0))

    def test_erro_do_storage_ou_do_motor_nao_grava_nada(self):
        self.com_storage()
        lead_id = self.lead_do_site(list(LOTE))
        self.storage.falha = ErroDeArmazenamento("o Storage não respondeu (URLError)")
        self.assertIn("Não foi possível baixar as planilhas do lead no Storage agora.", self.flashes(self.processar(lead_id)))
        self.assertEqual((self.worker_chamado, self.recarregar(lead_id).status), (0, "pendente"))
        self.storage.falha = None
        sem_periodo = self.lead_do_site([SEM_PERIODO], nome_loja="Loja Sem Período", email="periodo@example.invalid")
        self.assertIn("Arquivo 1: o período não está no cabeçalho", self.flashes(self.processar(sem_periodo)))
        self.assertEqual((self.recarregar(sem_periodo).status, self.contar_linhas_pdv(sem_periodo)), ("pendente", 0))

    def test_lead_indisponivel_e_erro_de_banco(self):
        import avaliacao_cenario
        self.com_storage()
        lead_id = self.lead_do_site(list(LOTE))
        with patch("avaliacao_cenario.registrar",
                   side_effect=avaliacao_cenario.LeadIndisponivel("Lead inexistente ou já convertido em cliente.")):
            self.assertIn("Lead inexistente ou já convertido em cliente.", self.flashes(self.processar(lead_id)))
        with patch("avaliacao_cenario.registrar", side_effect=OperationalError("INSERT", {}, Exception("fora do ar"))):
            self.assertIn("Não foi possível gravar a avaliação. Tente novamente.", self.flashes(self.processar(lead_id)))
        self.assertEqual((self.recarregar(lead_id).status, self.contar_linhas_pdv(lead_id)), ("pendente", 0))

    def test_exige_csrf_e_consultor(self):
        self.com_storage()
        lead_id = self.lead_do_site(list(LOTE), aceite=False)
        self.assertEqual(self.processar(lead_id, token="").status_code, 400)
        self.logout()
        token = self.csrf_token("/auth/login")
        anonimo = self.processar(lead_id, token)
        self.assertEqual(anonimo.status_code, 302)
        self.assertIn("/auth/login", anonimo.headers["Location"])
        self.login()
        self.assertEqual(self.processar(lead_id, self.csrf_token("/cliente/dashboard")).status_code, 403)
        self.assertEqual((self.storage.pedidos, self.worker_chamado), ([], 0))
        self.assertEqual(self.recarregar(lead_id).status, "pendente")


# =============================================================================
# 6. Banco sem as revisões do microsserviço e do aceite
# =============================================================================
class SemRevisoesTests(_PainelCase):
    def test_tela_segue_sem_as_colunas_do_microsservico_e_sem_o_aceite(self):
        from sqlalchemy import inspect
        self._migrar("downgrade", "e7a1c3f5b9d2")
        colunas = {coluna["name"] for coluna in inspect(self.db.engine).get_columns("lead_avaliacao")}
        self.assertFalse({"interesse", "arquivos_storage", "termos_versao"} & colunas)
        self.assertNotIn("aceite_documento_lead", inspect(self.db.engine).get_table_names())
        do_site = self.lead_simples(nome_loja="Loja Do Site Antigo", origem="site", status="pendente")
        self.lead_simples(nome_loja="Loja Do Consultor Antigo", consentimento_em=INSTANTE)
        lista = self.html(PAGINA)
        self.assertEqual(_texto(_celulas(_linha(lista, "Loja Do Site Antigo"))[5]), "—")
        self.assertEqual(_texto(_celulas(_linha(lista, "Loja Do Site Antigo"))[6]), "PENDENTE")
        self.assertIn("Autorização do consultor", _celulas(_linha(lista, "Loja Do Consultor Antigo"))[5])
        painel = self.painel(do_site)
        pagina = self.html(f"{PAGINA}/{do_site}")
        for html in (painel, pagina):
            self.assertNotIn("Planilhas enviadas pelo lead", html)
        self.assertRegex(painel, r"<dt>Conformidade</dt><dd>\s*—\s*</dd>")
        self.com_storage()
        for caminho in (f"{PAGINA}/{do_site}/arquivos/1", f"{PAGINA}/{do_site}/arquivos.zip"):
            self.assertEqual(self.client.get(caminho).status_code, 404)
        self.assertIn("Este lead não tem planilhas válidas no Storage para processar.", self.flashes(self.processar(do_site)))
        self.assertEqual(self.storage.pedidos, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
