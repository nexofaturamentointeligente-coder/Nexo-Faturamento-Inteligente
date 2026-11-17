"""Redesenho da landing e captação de leads (decisões do dono, 16/11/2026), pelo HTML e pela configuração.

- Topo: "Entrar" (login) e "Cadastre-se" (abre o cadastro do lead; sem JavaScript, leva ao rodapé).
- Cadastro do lead frio (#cadastro-lead): termos com a regra da rolagem, dados (nome, empresa, e-mail e telefone da
  empresa), a Avaliação de Cenário Externo opcional (relatórios .csv/.xlsx) e o resultado. O envio é do script
  (static/js/landing-captacao.js) ao microsserviço; sem número de WhatsApp, nada no HTML menciona o WhatsApp.
- Rodapé de ponta a ponta: e-mail de suporte, WhatsApp (só com número), Instagram, crédito do desenvolvedor e o
  CNPJ só com CNPJ_EMPRESA válido.
- Configuração: o número oficial quando a variável não existe, o e-mail de suporte, o CNPJ e a origem https do
  microsserviço.
O comportamento no navegador (regra da rolagem, envio e respostas do serviço) fica em
tests/test_landing_captacao_navegador.py. Migrações reais em SQLite, dados fictícios e nenhuma rede.
"""

from html.parser import HTMLParser
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from tests.deploy_support import DeployTestCase

ROOT = Path(__file__).resolve().parents[1]
NUMERO = "5562988887777"
SERVICO = "https://nexo-externo.onrender.com"
EMAIL = "suportenexocolab@gmail.com"
LINKEDIN = "https://www.linkedin.com/in/jo%C3%A3o-c%C3%A9sar-netto-souza-castro-152441304"
INSTAGRAM = "https://www.instagram.com/nexo.faturamento/"
# As páginas públicas dos documentos legais (frente J): os únicos destinos locais aceitos nos diálogos.
ROTAS_LEGAIS = ("/termos-de-uso", "/politica-de-privacidade")


class _Dom(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elementos, self.textos, self._oculto = [], [], 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elementos.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._oculto += 1

    def handle_startendtag(self, tag, attrs):
        self.elementos.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._oculto = max(0, self._oculto - 1)

    def handle_data(self, dados):
        if not self._oculto:
            self.textos.append(dados)

    @property
    def texto(self):
        return " ".join(" ".join(self.textos).split())

    def por_id(self, identificador):
        return next(attrs for _, attrs in self.elementos if attrs.get("id") == identificador)


def _trecho(html, inicio, fim):
    return inicio + html.split(inicio, 1)[1].split(fim, 1)[0]


class ConfiguracaoDaLandingTests(unittest.TestCase):
    """As regras do config.py, chamadas direto (os valores da classe Config são lidos na importação)."""

    def test_numero_oficial_so_quando_a_variavel_nao_existe(self):
        import config
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHATSAPP_EMPRESA_NUMERO", None)
            self.assertEqual(config._whatsapp_empresa(), "5561999055282")
        for valor, esperado in (("", ""), ("   ", ""), (" 5562988887777 ", "5562988887777"), ("(61) 99905-5282",
                                                                                               "(61) 99905-5282")):
            with self.subTest(valor=valor), patch.dict(os.environ, {"WHATSAPP_EMPRESA_NUMERO": valor}):
                # Presente e vazia continua vazia (os testes e quem quer esconder os botões contam com isso);
                # a normalização para só dígitos é da factory (whatsapp_service.configurar).
                self.assertEqual(config._whatsapp_empresa(), esperado)

    def test_email_de_suporte_com_a_forma_que_o_servico_aceita(self):
        import config
        self.assertEqual(config.EMAIL_SUPORTE_PADRAO, EMAIL)
        for valor, esperado in ((EMAIL, EMAIL), ("  Suporte@Example.COM ", "suporte@example.com"),
                                ("sem-arroba", ""), ("a..b@example.com", ""), ("a@b", ""), ("a b@example.com", ""),
                                (None, "")):
            with self.subTest(valor=valor):
                self.assertEqual(config.email_publico(valor), esperado)

    def test_cnpj_so_com_digitos_verificadores_corretos(self):
        import config
        for valor, esperado in (("11.222.333/0001-81", "11222333000181"), ("11222333000181", "11222333000181"),
                                ("11222333000182", ""), ("00000000000000", ""), ("1122233300018", ""),
                                ("11.222.333/0001-81x", ""), ("１１２２２３３３０００１８１", ""), ("", ""), (None, "")):
            with self.subTest(valor=valor):
                self.assertEqual(config.cnpj_valido(valor), esperado)

    def test_microsservico_so_como_origem_https(self):
        import config
        self.assertEqual(config.MICROSSERVICO_RAIOX_PADRAO, SERVICO)
        for valor, esperado in ((SERVICO, SERVICO), ("https://Nexo-Externo.onrender.com/", SERVICO),
                                ("https://servico.example.invalid:443", "https://servico.example.invalid"),
                                ("https://servico.example.invalid:8443", "https://servico.example.invalid:8443"),
                                ("http://servico.example.invalid", ""), ("https://servico.example.invalid/api", ""),
                                ("https://servico.example.invalid?x=1", ""), ("https://u:s@servico.example.invalid", ""),
                                ("https://servico.example.invalid:99999", ""), ("https://-x.example.invalid", ""),
                                ("https://[::1]", ""), ("javascript:alert(1)", ""), ("", ""), (None, "")):
            with self.subTest(valor=valor):
                self.assertEqual(config.origem_https(valor), esperado)


class _LandingCase(DeployTestCase):
    def setUp(self):
        super().setUp()
        # Os contatos fixos no teste, mesmo com as variáveis no ambiente de quem roda a suíte.
        self.app.config.update(EMAIL_SUPORTE=EMAIL, CNPJ_EMPRESA="", MICROSSERVICO_RAIOX_URL=SERVICO)

    def landing(self, numero=""):
        self.app.config["WHATSAPP_EMPRESA_NUMERO"] = numero
        resposta = self.client.get("/")
        self.assertEqual(resposta.status_code, 200)
        html = resposta.get_data(as_text=True)
        return html, _Dom(html)


class TopoEHeroTests(_LandingCase):
    def test_entrar_e_cadastre_se_no_topo(self):
        html, dom = self.landing()
        topo = _trecho(html, "<header", "</header>")
        acoes = _Dom(_trecho(topo, '<div class="header-actions">', "</div>"))
        links = [attrs for tag, attrs in acoes.elementos if tag == "a"]
        self.assertEqual([(attrs.get("href"), attrs.get("class")) for attrs in links],
                         [("/auth/login", "lp-entrar"), ("#contato", "btn-nexo btn-nexo-primary lp-cadastre")])
        self.assertIn("data-cadastro-gatilho", links[1])
        self.assertEqual(acoes.texto.split()[:2], ["Entrar", "Cadastre-se"])
        # O destino sem JavaScript existe: os contatos do rodapé.
        self.assertEqual(dom.por_id("contato").get("class"), "nexo-footer lp-rodape")
        # A marca e os ganchos do menu continuam.
        self.assertIn('<a href="#inicio" class="brand" aria-label="NEXO Faturamento Inteligente, início">', topo)
        for gancho in ('class="nexo-header"', 'id="landing-menu-toggle"', 'id="landing-navigation"'):
            self.assertIn(gancho, html)

    def test_hero_centralizado_sem_tag_sem_nicho_e_sem_o_painel(self):
        html, dom = self.landing()
        hero = _trecho(html, '<section class="hero lp-hero" id="inicio"', "</section>")
        self.assertNotIn("section-tag", hero)
        self.assertNotIn("Inteligência para a sua loja", hero)
        self.assertNotIn("Para lojas de tintas", dom.texto)
        self.assertNotIn("Mais clareza para decidir", dom.texto)
        self.assertNotIn("Painel demonstrativo", hero)
        self.assertNotIn("hero-chart", html)
        self.assertIn('Decisões mais claras para <span class="accent">fazer sua loja crescer.</span>', hero)
        self.assertIn("relatórios de compras e vendas", _Dom(hero).texto)
        descricao = re.search(r'<meta name="description" content="([^"]+)"', html).group(1)
        self.assertNotIn("tintas", descricao)
        rodape = _Dom(_trecho(html, "<footer", "</footer>")).texto
        self.assertNotIn("tintas", rodape)
        self.assertNotIn("pintura", rodape)

    def test_beneficios_e_passos_sem_tag_e_sem_nota(self):
        html, dom = self.landing()
        for secao in ('id="problema"', 'id="como-funciona"'):
            with self.subTest(secao=secao):
                trecho = html.split(secao, 1)[1].split("</section>", 1)[0]
                self.assertNotIn("section-tag", trecho)
                self.assertNotIn("commercial-note", trecho)
                self.assertNotIn("process-note", trecho)
        beneficios = html.split('id="problema"', 1)[1].split("</section>", 1)[0]
        titulos = re.findall(r"<h3>([^<]+)</h3>", beneficios)
        self.assertEqual(titulos, ["Pare de vender abaixo do custo", "Recupere o que escapa nos descontos",
                                   "Libere o capital parado", "Saiba quanto dá para recuperar"])
        # Cada card com o seu ícone.
        icones = re.findall(r'<div class="nexo-card-icon [a-z]+" aria-hidden="true">(<svg.*?</svg>)</div>', beneficios)
        self.assertEqual(len(icones), 4)
        self.assertEqual(len(set(icones)), 4)
        # Sem promessa de resultado.
        for promessa in ("garant", "aumente", "dobre", "lucro garantido"):
            self.assertNotIn(promessa, _Dom(beneficios).texto.casefold())

    def test_avaliacoes_com_a_tag_nova(self):
        html, _ = self.landing()
        avaliacoes = html.split('id="avaliacoes"', 1)[1].split("</section>", 1)[0]
        self.assertIn('<span class="section-tag section-tag--destaque">Avaliações</span>', avaliacoes)
        self.assertNotIn(">Depoimentos<", avaliacoes)
        # O .section-tag global não mudou (o Raio-X público também o usa).
        folha = (ROOT / "static" / "css" / "landing.css").read_text(encoding="utf-8")
        self.assertIn("font-size: 0.8rem; font-weight: 600;", folha.split(".section-tag {", 1)[1].split("}", 1)[0])

    def test_quatro_planos_sob_consulta_sem_botao_e_sem_recurso_em_implantacao(self):
        html, dom = self.landing()
        planos = html.split('id="planos"', 1)[1].split("</section>", 1)[0]
        cards = re.findall(r'<article class="plan ([a-z ]+)">', planos)
        self.assertEqual(cards, ["bronze", "prata", "ouro featured", "personalizado"])
        nomes = re.findall(r'<h3 class="plan-name">([^<]+)</h3>', planos)
        self.assertEqual(nomes, ["BRONZE", "PRATA", "OURO", "PERSONALIZADO / ENTERPRISE"])
        self.assertEqual(planos.count('<div class="plan-price">Sob consulta</div>'), 4)
        self.assertIn('<span class="plan-flag">Mais completo</span>', planos.split('<article class="plan ouro', 1)[1])
        for proibido in ("<a ", "<button", "RFM", "recompra", "multifilial", "DRE", "meios de pagamento",
                         "Em implantação", "em breve"):
            self.assertNotIn(proibido, planos)
        # Nenhum preço (valor em reais); "R$" só aparece como unidade da margem.
        self.assertNotRegex(planos, r"R\$\s*\d")


class CadastroDoLeadTests(_LandingCase):
    def test_estrutura_do_dialogo_sem_numero(self):
        html, dom = self.landing()
        dialogo = _trecho(html, '<dialog id="cadastro-lead"', "</dialog>")
        pagina = _Dom(dialogo)
        raiz = pagina.por_id("cadastro-lead")
        self.assertEqual((raiz.get("class"), raiz.get("data-servico"), raiz.get("data-email")),
                         ("lp-dialogo", SERVICO, EMAIL))
        self.assertEqual(raiz.get("aria-labelledby"), "cadastro-lead-titulo-1")
        etapas = {attrs["id"]: (attrs.get("class") or "").split() for tag, attrs in pagina.elementos
                  if tag == "section"}
        self.assertEqual(etapas, {"cadastro-lead-etapa-1": ["precadastro-etapa"],
                                  "cadastro-lead-etapa-2": ["precadastro-etapa", "hidden"],
                                  "cadastro-lead-etapa-3": ["precadastro-etapa", "hidden"],
                                  "cadastro-lead-etapa-4": ["precadastro-etapa", "hidden"]})
        # (a) Termos: dois documentos em contêineres com rolagem, caixas e "Continuar" desabilitados.
        caixas = [attrs for tag, attrs in pagina.elementos if tag == "input" and attrs.get("type") == "checkbox"]
        self.assertEqual([attrs["id"] for attrs in caixas],
                         ["cadastro-lead-check-termos", "cadastro-lead-check-privacidade"])
        self.assertTrue(all("disabled" in attrs for attrs in caixas))
        self.assertIn("disabled", pagina.por_id("cadastro-lead-continuar"))
        self.assertEqual([attrs.get("data-termo") for tag, attrs in pagina.elementos if "data-termo" in attrs],
                         ["termos", "privacidade"])
        # (b) Dados: os quatro obrigatórios; o telefone não se chama "whatsapp" (o script o envia nesse campo).
        [formulario] = [attrs for tag, attrs in pagina.elementos if tag == "form"]
        self.assertEqual(formulario, {"id": "cadastro-lead-form", "class": "precadastro-form"})
        obrigatorios = {attrs["name"]: attrs["type"] for tag, attrs in pagina.elementos if "required" in attrs}
        self.assertEqual(obrigatorios, {"nome": "text", "empresa": "text", "email": "email", "telefone": "tel"})
        telefone = pagina.por_id("cadastro-lead-telefone")
        self.assertEqual((telefone.get("inputmode"), telefone.get("maxlength")), ("tel", "60"))
        isca = next(attrs for tag, attrs in pagina.elementos if attrs.get("name") == "campo_isca")
        self.assertEqual((isca.get("tabindex"), isca.get("autocomplete")), ("-1", "off"))
        # (c) Avaliação de Cenário Externo: relatórios .csv/.xlsx e as duas saídas.
        arquivos = pagina.por_id("cadastro-lead-arquivos")
        self.assertEqual((arquivos.get("type"), arquivos.get("accept")), ("file", ".csv,.xlsx"))
        self.assertIn("multiple", arquivos)
        self.assertIn("Avaliação de Cenário Externo", pagina.texto)
        self.assertIn('data-enviar="1">Enviar para a avaliação</button>', dialogo)
        self.assertIn('data-enviar="3">Agora não, só quero contato</button>', dialogo)
        # (d) Resultado: protocolo, situação dos relatórios e próximos passos.
        for identificador in ("cadastro-lead-protocolo", "cadastro-lead-resultado-arquivos",
                              "cadastro-lead-resultado-texto"):
            self.assertIn(f'id="{identificador}"', dialogo)
        self.assertIn("Próximos passos", pagina.texto)
        # Sem número, nada menciona o WhatsApp, em lugar nenhum da página.
        self.assertNotRegex(html.casefold(), r"whatsapp|wa\.me")
        self.assertIn("<small id=\"cadastro-lead-telefone-dica\" class=\"precadastro-dica\">Com DDD.</small>", dialogo)
        # Nenhum destino local no diálogo (os documentos vêm do script); na junção, só as páginas públicas deles.
        self.assertEqual([attrs["href"] for tag, attrs in pagina.elementos
                          if tag == "a" and attrs.get("href", "").startswith("/") and attrs["href"] not in ROTAS_LEGAIS], [])

    def test_com_numero_o_painel_oferece_ajuda_pelo_whatsapp(self):
        html, _ = self.landing(NUMERO)
        dialogo = _trecho(html, '<dialog id="cadastro-lead"', "</dialog>")
        ajuda = _Dom(dialogo)
        [link] = [attrs for tag, attrs in ajuda.elementos if tag == "a" and "data-ajuda-whatsapp" in attrs]
        self.assertTrue(link["href"].startswith(f"https://wa.me/{NUMERO}?text="))
        self.assertEqual((link.get("target"), link.get("rel"), link.get("data-numero")),
                         ("_blank", "noopener noreferrer", NUMERO))
        self.assertIn("Precisa de ajuda para exportar seus relatórios do sistema? Fale conosco no WhatsApp", ajuda.texto)
        self.assertIn("Com DDD. Se tiver, use o número com WhatsApp.", ajuda.texto)
        # O campo continua sem "whatsapp" no name (é o mesmo HTML com e sem número).
        self.assertIn('name="telefone"', dialogo)


class RodapeTests(_LandingCase):
    def test_contatos_fixos_e_links_externos_em_nova_aba(self):
        for numero in ("", NUMERO):
            with self.subTest(numero=numero):
                html, _ = self.landing(numero)
                rodape = _trecho(html, "<footer", "</footer>")
                dom = _Dom(rodape)
                links = [attrs for tag, attrs in dom.elementos if tag == "a"]
                hrefs = [attrs["href"] for attrs in links]
                self.assertIn(f"mailto:{EMAIL}", hrefs)
                self.assertIn(INSTAGRAM, hrefs)
                self.assertIn(LINKEDIN, hrefs)
                self.assertIn("Desenvolvido por João César N. S. Castro", dom.texto)
                self.assertIn(EMAIL, dom.texto)
                whatsapp = [href for href in hrefs if href.startswith("https://wa.me/")]
                if numero:
                    self.assertEqual(len(whatsapp), 1)
                    self.assertIn("Fale diretamente pelo WhatsApp com a nossa equipe", dom.texto)
                else:
                    self.assertEqual(whatsapp, [])
                    self.assertNotIn("WhatsApp", dom.texto)
                for attrs in links:
                    if attrs["href"].startswith("https://"):
                        self.assertEqual((attrs.get("target"), attrs.get("rel")), ("_blank", "noopener noreferrer"))
                self.assertNotIn("ainda não estão disponíveis", dom.texto)
                self.assertIn("© 2026 NEXO Faturamento Inteligente. Todos os direitos reservados.", dom.texto)

    def test_cnpj_so_com_a_variavel_preenchida(self):
        html, _ = self.landing()
        self.assertNotIn("CNPJ", _Dom(html).texto)
        self.assertNotIn("lp-cnpj", html)
        self.app.config["CNPJ_EMPRESA"] = "11222333000181"
        html, _ = self.landing()
        rodape = _Dom(_trecho(html, "<footer", "</footer>")).texto
        self.assertIn("CNPJ 11.222.333/0001-81", rodape)

    def test_marcadores_para_a_juncao(self):
        fonte = (ROOT / "templates" / "landing.html").read_text(encoding="utf-8")
        # Os links dos documentos legais (rodapé e aceite dos dois diálogos) e o encaixe das avaliações: o
        # marcador até a junção, o código de verdade depois dela.
        self.assertTrue(fonte.count("{# LINKS_LEGAIS #}") == 2 or "documentos_publicos." in fonte)
        self.assertTrue("AVALIACOES_LANDING" in fonte or "_avaliacao_landing.html" in fonte)


class ScriptsDaCaptacaoTests(unittest.TestCase):
    def test_scripts_sem_numero_fixo_sem_html_vindo_de_fora_e_sem_armazenamento(self):
        for nome in ("landing-captacao.js", "landing-precadastro.js"):
            with self.subTest(script=nome):
                fonte = (ROOT / "static" / "js" / nome).read_text(encoding="utf-8")
                self.assertNotRegex(fonte, r"\b55\d{10,11}\b")
                self.assertNotRegex(fonte, r"wa\.me/\d")
                for proibido in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                                 "localStorage", "sessionStorage", "credentials: 'include'"):
                    self.assertNotIn(proibido, fonte)
                # Nenhum U+2028/U+2029 literal: quebraria a expressão regular (o escape fica como texto).
                self.assertNotIn("\u2028", fonte)
                self.assertNotIn("\u2029", fonte)
        captacao = (ROOT / "static" / "js" / "landing-captacao.js").read_text(encoding="utf-8")
        for trecho in ("'/api/leads/raio-x'", "'/documentos-legais/vigentes'", "credentials: 'omit'",
                       "scrollTop + texto.clientHeight >= texto.scrollHeight - 2", "PRAZO_DO_ENVIO = 90000",
                       "MAXIMO_DE_ARQUIVOS = 12", "MAXIMO_DE_BYTES = 15 * 1024 * 1024"):
            self.assertIn(trecho, captacao)


if __name__ == "__main__":
    unittest.main()
