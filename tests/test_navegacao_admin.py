"""
Regressão HTTP (sem navegador) da navegação lateral depois que a "Base de
Conhecimento" saiu do menu administrativo (PR #33, 28/10/2026).

Em toda página administrativa — inclusive as do CMS do Guia, que continuam
alcançáveis por URL — a sidebar renderiza exatamente Dashboard, Empresas,
Contratos, Lixeira, Análises, Suporte e Configurações; nenhum link da sidebar (menu ou rodapé)
aponta para /admin/guia; fora das páginas do próprio CMS, "/admin/guia" não
aparece em lugar nenhum da página; o item ativo é o da página corrente e, no
CMS, nenhum; "Sair" segue único no rodapé. A sidebar do cliente não ganha o
item e não aponta para /admin.

Roda com o cliente de teste do Flask (sem Playwright, sem cópias da CDN): é
executado pelo `unittest discover` do job security-sqlite-browser em todo PR e
push na main (o job security-postgresql executa só os módulos tests.postgresql_*).
O equivalente em Chromium (gaveta do celular, teclado, indicador do item ativo)
está em tests/test_overflow_navegador.py.
"""

import re
import unittest

from tests.deploy_support import DeployTestCase

ROTULOS_ADMIN = ["Dashboard", "Empresas", "Contratos", "Lixeira", "Análises", "Suporte", "Configurações"]
# O cliente das fixtures é BRONZE: o Raio-X do PDV é livre e a Auditoria de custos (PRATA) aparece
# com o cadeado e o selo UPGRADE, cujo texto conta no rótulo (planos por nível, entitlements.py).
ROTULOS_CLIENTE = ["Visão Geral", "Enviar relatórios", "Histórico", "Raio-X do PDV", "Auditoria de custos UPGRADE",
                   "Guia", "Suporte", "Configurações"]

_ASIDE = re.compile(r'<aside\b[^>]*\bclass="[^"]*\bsidebar\b[^"]*"[^>]*>(.*?)</aside>', re.S)
_NAV = re.compile(r'<nav\b[^>]*\bclass="[^"]*\bsidebar-nav\b[^"]*"[^>]*>(.*?)</nav>', re.S)
_LINK = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S)
_ATRIBUTO = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


def _texto(trecho):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", trecho)).strip()


def _links(trecho):
    """Todos os <a> do trecho: (href, classes, texto visível), sem depender da ordem dos atributos."""
    links = []
    for atributos, conteudo in _LINK.findall(trecho):
        attrs = dict(_ATRIBUTO.findall(atributos))
        links.append((attrs.get("href", ""), set((attrs.get("class") or "").split()), _texto(conteudo)))
    return links


class _Sidebar:
    def __init__(self, html, caminho):
        aside = _ASIDE.search(html)
        nav = _NAV.search(html)
        assert aside and nav, f"{caminho}: sidebar ou menu não encontrados"
        self.aside = aside.group(1)
        self.links_aside = _links(self.aside)
        itens = [l for l in _links(nav.group(1)) if "nav-item" in l[1]]
        self.rotulos = [texto for _, _, texto in itens]
        self.ativos = [texto for _, classes, texto in itens if "active" in classes]
        # SEC-002: "Sair" é um <button> de formulário POST, não mais um <a>.
        self.sair = [l for l in self.links_aside if "nav-logout" in l[1]]
        for atributos, conteudo in re.findall(r"<button\b([^>]*)>(.*?)</button>", self.aside, re.S):
            attrs = dict(_ATRIBUTO.findall(atributos))
            classes = set((attrs.get("class") or "").split())
            if "nav-logout" in classes:
                self.sair.append(("", classes, _texto(conteudo)))


class NavegacaoAdminSemBaseDeConhecimentoTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        from models import Analise, GuiaTopico, IndicadorAnalise, RelatorioAnalise
        self.fixtures()
        analise = self.db.session.get(Analise, self.analise_id)
        self.empresa_id = analise.id_empresa
        # Análise publicada (a tela de análise do cliente abre) e um tópico do Guia
        # (a página de edição do CMS existe).
        analise.status_analise = "CONCLUIDO"
        analise.indicador = IndicadorAnalise(faturamento_total=25, total_comprado=40, saldo_estimado_compras_vendas=15)
        analise.relatorio = RelatorioAnalise(titulo="Análise sintética", conclusao_estrategica="Conclusão sintética.",
                                             publicado=True)
        topico = GuiaTopico(categoria="Categoria fictícia", pergunta="Pergunta fictícia?", resposta="Resposta fictícia.")
        self.db.session.add(topico)
        self.db.session.commit()
        self.topico_id = topico.id

    def _pagina(self, caminho):
        resposta = self.client.get(caminho)
        self.assertEqual(resposta.status_code, 200, caminho)
        return resposta.get_data(as_text=True)

    def paginas_admin(self):
        """Página administrativa -> rótulo do item ativo esperado (None no CMS, que não tem item)."""
        e, a, t = self.empresa_id, self.analise_id, self.topico_id
        return {
            "/admin/dashboard": "Dashboard",
            "/admin/empresas": "Empresas", "/admin/empresas/nova": "Empresas",
            f"/admin/empresas/{e}/editar": "Empresas", f"/admin/empresas/{e}/detalhes": "Empresas",
            "/admin/contratos": "Contratos",
            "/admin/lixeira": "Lixeira",
            "/admin/analises": "Análises", "/admin/analises/nova": "Análises",
            "/admin/analises/gerenciar": "Análises", f"/admin/analises/gerenciar/{e}": "Análises",
            f"/admin/analise/{a}/upload": "Análises", f"/admin/analise/{a}/relatorio": "Análises",
            "/admin/tickets": "Suporte", "/admin/suporte": "Suporte",
            "/admin/configuracoes": "Configurações",
            "/admin/guia": None, "/admin/guia/novo": None, f"/admin/guia/editar/{t}": None,
        }

    def test_sidebar_administrativa_sem_base_de_conhecimento_em_todas_as_paginas(self):
        self.login("admin-test@example.invalid")
        for caminho, ativo in self.paginas_admin().items():
            with self.subTest(caminho=caminho):
                html = self._pagina(caminho)
                sidebar = _Sidebar(html, caminho)
                self.assertEqual(sidebar.rotulos, ROTULOS_ADMIN)
                self.assertEqual(sidebar.ativos, [ativo] if ativo else [])
                self.assertNotIn("Base de Conhecimento", sidebar.aside)
                # Nenhum link da sidebar (menu ou rodapé) leva ao CMS do Guia; "Sair" é único.
                self.assertEqual([h for h, _, _ in sidebar.links_aside if h.startswith("/admin/guia")], [])
                self.assertEqual([texto for _, _, texto in sidebar.sair], ["Sair"])
                if not caminho.startswith("/admin/guia"):
                    # Fora do próprio CMS, nada na página (topbar, atalhos, scripts) aponta para ele.
                    self.assertNotIn("/admin/guia", html)

    def test_sidebar_do_cliente_nao_ganha_o_item_nem_aponta_para_a_administracao(self):
        self.login("cliente-test@example.invalid")
        for caminho in ("/cliente/dashboard", "/cliente/upload", "/cliente/historico",
                        f"/cliente/analise/{self.analise_id}", "/cliente/guia", "/cliente/tickets", "/cliente/configuracoes"):
            with self.subTest(caminho=caminho):
                sidebar = _Sidebar(self._pagina(caminho), caminho)
                self.assertEqual(sidebar.rotulos, ROTULOS_CLIENTE)
                self.assertNotIn("Base de Conhecimento", sidebar.aside)
                self.assertEqual([h for h, _, _ in sidebar.links_aside if h.startswith("/admin")], [])


class ConfiguracoesESuporteTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        from models import Analise, ChamadoSuporte, MensagemSuporte
        self.fixtures()
        empresa = self.db.session.get(Analise, self.analise_id).id_empresa
        chamado = ChamadoSuporte(id_empresa=empresa, id_usuario_cliente=self.cliente_id, assunto="Assunto fictício",
                                 categoria="DUVIDA_TECNICA")
        chamado.mensagens = [MensagemSuporte(id_usuario_remetente=self.cliente_id, mensagem="<b>Texto longo</b> " * 200)]
        self.db.session.add(chamado)
        self.db.session.commit()
        self.chamado_id = chamado.id_chamado

    def test_configuracoes_respeitam_o_perfil(self):
        self.assertIn("/auth/login", self.client.get("/admin/configuracoes").headers["Location"])
        self.login("cliente-test@example.invalid")
        self.assertEqual(self.client.get("/cliente/configuracoes").status_code, 200)
        self.assertEqual(self.client.get("/admin/configuracoes").status_code, 403)
        self.logout()
        self.login("admin-test@example.invalid")
        self.assertEqual(self.client.get("/admin/configuracoes").status_code, 200)
        self.assertEqual(self.client.get("/cliente/configuracoes").status_code, 403)

    def test_cards_de_suporte_escapam_o_texto_e_levam_ao_chamado_completo(self):
        self.login("admin-test@example.invalid")
        for caminho in ("/admin/suporte", "/admin/tickets"):
            html = self.client.get(caminho).get_data(as_text=True)
            self.assertIn('class="ticket-card-mensagem"', html)
            self.assertIn("&lt;b&gt;Texto longo", html)
            self.assertNotIn("<b>Texto longo", html)
            self.assertIn(f'href="/admin/tickets/{self.chamado_id}"', html)
            self.assertIn("Ver chamado", html)
        detalhe = self.client.get(f"/admin/tickets/{self.chamado_id}").get_data(as_text=True)
        self.assertEqual(detalhe.count("&lt;b&gt;Texto longo"), 200)


if __name__ == "__main__":
    unittest.main()
