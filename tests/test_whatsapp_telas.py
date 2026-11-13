"""Botões de contato pelo WhatsApp (links passivos wa.me) nas telas do NEXO.

Executar da raiz: python -B -m unittest tests.test_whatsapp_telas -v

Sem número comercial configurado (o padrão, COM-001), cada tela sai como antes do WhatsApp:
nenhuma menção a ele (fora o recurso de catálogo "Automação de recompra via WhatsApp", que a
tela de planos já mostra) e, em volta de cada ponto em que ele entra, o mesmo HTML de antes,
byte a byte (TRECHOS_DE_HOJE): os mesmos botões desabilitados, os textos de "canal ainda não
publicado" e nenhum elemento a mais. Com o número, só esses trechos mudam, e cada link leva a
mensagem certa (decodificada e comparada por inteiro), abre em nova aba com
rel="noopener noreferrer", não tem atributo on* e o nome acessível começa pelo rótulo
visível. Número malformado posto direto no config não desenha link nem troca texto.
Migrações reais em SQLite, dados fictícios (example.invalid) e nenhuma rede.
"""

from datetime import date
import difflib
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flask import render_template

from tests.contratos_support import ContratosCase

ROOT = Path(__file__).resolve().parents[1]
NUMERO = "5562988887777"
PREFIXO = f"https://wa.me/{NUMERO}?text="
NOVA_ABA = "(abre o WhatsApp em nova aba)"
MENSAGENS = {
    "contratar": "Olá! Quero contratar um plano do NEXO Faturamento Inteligente.",
    "mudar_plano": "Olá! Quero mudar o meu plano do NEXO Faturamento Inteligente.",
    "renovar": "Olá! Quero renovar o meu contrato do NEXO Faturamento Inteligente.",
    "duvidas": "Olá! Tenho dúvidas sobre o NEXO Faturamento Inteligente e quero falar com a equipe comercial.",
    "suporte": "Olá! Sou cliente do NEXO Faturamento Inteligente e preciso de atendimento.",
}
# Funcionalidade do plano OURO (recompra_whatsapp, migração b3e9d6c1f7a2), não canal de contato:
# a tela de planos já a mostra hoje.
RECURSO_DE_CATALOGO = "Automação de recompra via WhatsApp"
# Os {%- import/set -%} do topo da landing não podem deixar nada antes do doctype.
COMECO_DA_LANDING = '<!DOCTYPE html>\n<html lang="pt-BR">\n<head>\n'
VALORES_MALFORMADOS = ("62 9999-abc", "+5562988887777", "(62) 98888-7777", "５５６２９８８８８７７７７", "123",
                       "55629888877771234", "https://wa.me/5562988887777", "", "   ", 5562988887777, None)

# =====================================================================
# O HTML de hoje (sem número) em volta de cada ponto em que o WhatsApp entra, byte a byte.
# Tirado das telas desenhadas com os templates de antes do WhatsApp. Cada item é
# (início, fim, trecho): o trecho começa no `início`, que aparece uma só vez na página (ou,
# num par (âncora, marca), na primeira marca depois da âncora única) e vai até o primeiro
# `fim` depois dele, inclusive. Com número, cada trecho muda e nada fora deles muda
# (test_o_numero_so_muda_os_trechos_de_referencia): quem puser o WhatsApp num ponto novo
# precisa acrescentar aqui o HTML de hoje desse ponto.
# =====================================================================
_ALERTA_COMERCIAL = (
    '<div class="alert alert-secondary" role="note">', "Já recebeu seu acesso?",
    '<div class="alert alert-secondary" role="note">\n'
    '        <h2 style="font-size: 1rem;">Atendimento comercial</h2>\n'
    '        <p class="mb-0">O canal oficial de contratação ainda não foi publicado. Esta página será atualizada'
    ' quando o atendimento estiver disponível.</p>\n'
    '    </div>\n'
    '    <p>Já recebeu seu acesso?')
_CARTAO_CONTATO = (
    '<div class="contact-card mt-4" id="contato-nexo">', "</div>\n    </div>\n</div>",
    '<div class="contact-card mt-4" id="contato-nexo">\n'
    '    <div class="contact-card-icon"><i class="bi bi-headset"></i></div>\n'
    '    <div>\n'
    '        <h2>Contato NEXO</h2>\n'
    '        <p>Para dúvidas sobre sua análise, envie um chamado pelo suporte do portal.</p>\n'
    '        <div class="contact-details">\n'
    '            <a href="/cliente/tickets"><i class="bi bi-headset" aria-hidden="true"></i> Abrir suporte</a>\n'
    '        </div>\n'
    '    </div>\n'
    '</div>')
_ACOES_DO_SUPORTE = (
    '<div class="page-head-actions">', "</div>",
    '<div class="page-head-actions">\n'
    '        <button type="button" class="btn-nexo" data-toggle-novo><i class="bi bi-plus-lg"></i> Novo chamado'
    '</button>\n'
    '    </div>')
_BARRA_DE_UPGRADE = '<div class="d-flex flex-wrap gap-2 justify-content-center mt-3">'


def _barra_de_upgrade(destino=None, rotulo=None):
    """A barra do convite de upgrade de hoje: o pedido (quando há destino) e a Visão Geral."""
    pedido = (f'            <a href="{destino}" class="btn-nexo"><i class="bi bi-arrow-up-circle" aria-hidden="true">'
              f'</i> {rotulo}</a>\n            \n') if destino else ""
    return (_BARRA_DE_UPGRADE, "</div>",
            f"{_BARRA_DE_UPGRADE}\n            \n{pedido}"
            '            <a href="/cliente/dashboard" class="btn-ghost"><i class="bi bi-speedometer2" aria-hidden="true">'
            '</i> Visão Geral</a>\n'
            '        </div>')


TRECHOS_DE_HOJE = {
    "landing": (
        (('<article class="plan bronze">', "</ul>"), "</article>",
         '</ul>\n'
         '        <button type="button" class="btn-nexo btn-nexo-secondary w-100" disabled'
         ' aria-describedby="contratacao-status">Contrate Agora</button>\n'
         '      </article>'),
        (('<article class="plan prata">', "</ul>"), "</article>",
         '</ul>\n'
         '        <button type="button" class="btn-nexo btn-nexo-secondary w-100" disabled'
         ' aria-describedby="contratacao-status">Contrate Agora</button>\n'
         '      </article>'),
        (('<article class="plan ouro featured">', "</ul>"), "</article>",
         '</ul>\n'
         '        <button type="button" class="btn-nexo btn-nexo-primary w-100" disabled'
         ' aria-describedby="contratacao-status">Contrate Agora</button>\n'
         '      </article>'),
        ('<div class="plan-availability text-center mt-4" id="contratacao">', "</div>",
         '<div class="plan-availability text-center mt-4" id="contratacao">\n'
         '      <p class="commercial-note" id="contratacao-status">A contratação ainda não está disponível nesta'
         ' página. Os canais oficiais serão divulgados aqui.</p>\n'
         '      <a href="/como-aderir" class="text-link">Como obter acesso ao NEXO <svg class="inline-icon"'
         ' viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 12h16M13 5l7 7-7 7"/></svg></a>\n'
         '    </div>'),
        ('<div class="cta-buttons">', "</p>",
         '<div class="cta-buttons">\n'
         '        <button type="button" class="btn-nexo btn-nexo-primary btn-nexo-lg" disabled'
         ' aria-describedby="contact-availability">Falar com a equipe NEXO</button>\n'
         '        <a href="/como-aderir" class="btn-nexo btn-nexo-secondary btn-nexo-lg">Como obter acesso</a>\n'
         '      </div>\n'
         '      <p id="contact-availability" class="commercial-note mt-3">Nosso canal oficial de atendimento será'
         ' divulgado nesta página.</p>'),
        ("<h3>Contatos</h3>", "</div>",
         '<h3>Contatos</h3>\n'
         '        <p>Os canais oficiais de atendimento serão divulgados nesta página.</p>\n'
         '        <a href="/como-aderir">Entenda como obter acesso</a>\n'
         '      </div>'),
    ),
    "como_aderir_visitante": (_ALERTA_COMERCIAL,),
    "como_aderir_cliente": (_ALERTA_COMERCIAL,),
    "login": (
        ("Saiba como ter acesso ao portal!", '<p class="auth-tagline">',
         'Saiba como ter acesso ao portal!\n'
         '        </a>\n'
         '    </p>\n'
         '</div>\n'
         '<p class="auth-tagline">'),
    ),
    "recuperar_senha": (),  # sem ponto de inserção: vale a varredura, e a tela sai igual com número
    "painel_sem_faixa": (_CARTAO_CONTATO,),
    "painel_com_faixa": (
        ('<div class="aviso-contrato is-alerta" role="status">', "</div>\n</div>",
         '<div class="aviso-contrato is-alerta" role="status">\n'
         '    <i class="bi bi-calendar-event" aria-hidden="true"></i>\n'
         '    <div>\n'
         '        <strong>Seu contrato vence em 8 dias.</strong>\n'
         '        <p>Vigência até 10/03/2026. Fale com a equipe NEXO para renovar: <a href="/cliente/tickets">abrir'
         ' um chamado</a>. Você pode desligar este lembrete em <a href="/cliente/configuracoes">Configurações</a>.'
         '</p>\n'
         '    </div>\n'
         '</div>'),
        _CARTAO_CONTATO,
    ),
    "suporte_vazio": (_ACOES_DO_SUPORTE,),
    "suporte_com_chamado": (_ACOES_DO_SUPORTE,),
    "chamado": (
        ('<div class="page-head-actions">', "</div>",
         '<div class="page-head-actions">\n'
         '        <span class="nexo-badge b-amber"><span class="dot"></span>Aberto</span>\n'
         '        <a href="/cliente/tickets" class="btn-ghost"><i class="bi bi-arrow-left"></i> Voltar</a>\n'
         '    </div>'),
    ),
    "planos": (
        ("Mudar de plano</h2>", "</p>",
         'Mudar de plano</h2>\n'
         '            <p class="sub">A mudança é feita pela equipe NEXO. Este pedido abre um chamado no Suporte,'
         ' com o plano escolhido.</p>'),
        ("Podem me explicar as condições?</textarea>", "</form>",
         'Podem me explicar as condições?</textarea>\n'
         '            </div>\n'
         '            <button type="submit" class="btn-nexo"><i class="bi bi-send" aria-hidden="true"></i> Pedir a'
         ' mudança pelo Suporte</button>\n'
         '        </form>'),
    ),
    "planos_503": (
        ('<div class="empty-state">', "</div>\n</div>",
         '<div class="empty-state">\n'
         '        <div class="es-icon"><i class="bi bi-layers" aria-hidden="true"></i></div>\n'
         '        <h3>Os planos por nível ainda não foram ativados no portal.</h3>\n'
         '        <p>O seu plano continua aparecendo na Visão Geral. Em caso de dúvida, abra um chamado no Suporte.'
         '</p>\n'
         '    </div>\n'
         '</div>'),
    ),
    "upgrade_plano": (_barra_de_upgrade("/onboarding/planos#prata", "Conhecer o plano Prata"),),
    "upgrade_sem_plano": (_barra_de_upgrade("/onboarding/planos", "Falar com a equipe NEXO"),),
    "upgrade_suspensa": (_barra_de_upgrade("/cliente/tickets", "Falar com a equipe NEXO"),),
    "upgrade_503": (_barra_de_upgrade(),),  # sem convite de upgrade: nem com número aparece o WhatsApp
}
# Telas que nem com número mostram o WhatsApp: saem iguais com e sem ele.
TELAS_SEM_WHATSAPP = frozenset({"recuperar_senha", "upgrade_503", "login"})


def mensagem_plano(plano):
    return f"Olá! Quero contratar o plano {plano} do NEXO Faturamento Inteligente."


def mensagem_chamado(id_chamado):
    return f"Olá! Preciso de ajuda com o chamado #{id_chamado} no NEXO Faturamento Inteligente."


class _Pagina(HTMLParser):
    """Elementos, texto e links da página; de cada link, o texto visível e o nome acessível
    (sem o que é aria-hidden; o visível também sem o que é só para leitor de tela)."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elementos, self.links, self._textos = [], [], []
        self._link, self._pilha, self._script = None, [], 0
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elementos.append((tag, attrs))
        if tag in ("script", "style"):
            self._script += 1
        if self._link is None:
            if tag == "a":
                self._link, self._pilha = {"attrs": attrs, "visivel": [], "nome": []}, []
            return
        classes = (attrs.get("class") or "").split()
        self._pilha.append((tag, "visually-hidden" in classes, attrs.get("aria-hidden") == "true"))

    def handle_startendtag(self, tag, attrs):
        self.elementos.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._script = max(0, self._script - 1)
        if self._link is None:
            return
        if tag == "a":
            link, self._link = self._link, None
            self.links.append({"attrs": link["attrs"],
                               "visivel": " ".join("".join(link["visivel"]).split()),
                               "nome": " ".join("".join(link["nome"]).split())})
            return
        while self._pilha:
            if self._pilha.pop()[0] == tag:
                break

    def handle_data(self, dados):
        if not self._script:
            self._textos.append(dados)
        if self._link is None:
            return
        if not any(item[2] for item in self._pilha):
            self._link["nome"].append(dados)
            if not any(item[1] for item in self._pilha):
                self._link["visivel"].append(dados)

    @property
    def texto(self):
        return " ".join(" ".join(self._textos).split())

    def whatsapp(self):
        return [link for link in self.links if "wa.me" in link["attrs"].get("href", "")]

    def ids(self):
        return [attrs["id"] for _, attrs in self.elementos if "id" in attrs]


def _trecho(html, inicio, fim):
    """O HTML entre a primeira ocorrência de `inicio` e o `fim` seguinte."""
    return html.split(inicio, 1)[1].split(fim, 1)[0]


def _intervalo(html, inicio, fim):
    """Onde fica um trecho de TRECHOS_DE_HOJE na página: (começo, final)."""
    ancora, marca = inicio if isinstance(inicio, tuple) else (inicio, inicio)
    vezes = html.count(ancora)
    if vezes != 1:
        raise AssertionError(f"A âncora {ancora!r} aparece {vezes} vez(es) na página; tem de aparecer uma.")
    comeco = html.find(marca, html.index(ancora))
    final = html.find(fim, comeco + len(marca)) if comeco >= 0 else -1
    if final < 0:
        raise AssertionError(f"Não achei o trecho de {inicio!r} até {fim!r}.")
    return comeco, final + len(fim)


def _normalizar(html):
    """Troca o que muda a cada requisição (CSRF e nonce da CSP) por marcas."""
    html = re.sub(r'(name="csrf_token"[^>]*value=")[^"]+(")', r"\1CSRF\2", html)
    html = re.sub(r'(<meta name="csrf-token" content=")[^"]+(")', r"\1CSRF\2", html)
    return re.sub(r'nonce="[^"]+"', 'nonce="NONCE"', html)


def _fora_dos_trechos(html, trechos):
    """A página com cada trecho de referência trocado por uma marca."""
    partes, anterior = [], 0
    for ordem, (comeco, final) in enumerate(sorted(_intervalo(html, inicio, fim) for inicio, fim, _ in trechos)):
        partes += [html[anterior:comeco], f"<<trecho {ordem}>>"]
        anterior = final
    return _normalizar("".join(partes) + html[anterior:])


class _WhatsAppCase(ContratosCase):
    def setUp(self):
        super().setUp()
        self.anonimo = self.app.test_client()

    def com_numero(self, valor=NUMERO):
        # O config do Flask é lido na hora de desenhar a página (o Jinja o tem como global).
        self.app.config["WHATSAPP_EMPRESA_NUMERO"] = valor

    def html(self, caminho, status=200, cliente=None):
        resposta = (cliente or self.client).get(caminho)
        self.assertEqual(resposta.status_code, status, caminho)
        return resposta.get_data(as_text=True)

    def pagina(self, caminho, status=200, cliente=None):
        html = self.html(caminho, status, cliente)
        return html, _Pagina(html)

    def upgrade_sem_plano(self):
        """O convite de upgrade de um recurso que nenhum plano libera (o contato é para dúvidas)."""
        with self.app.test_request_context("/onboarding/diagnostico/auditoria"):
            return render_template("onboarding/upgrade.html", mensagem="Recurso fictício fora dos planos.", status=403,
                                   codigo="plano_insuficiente", funcionalidade={"chave": "x", "nome": "Recurso"},
                                   plano_atual=None, plano_necessario=None, url_upgrade="/onboarding/planos")

    def mudar_status_empresa(self, status):
        from models import Empresa
        self.db.session.get(Empresa, self.empresa_id).status_conta = status
        self.db.session.commit()

    def contrato_vencendo(self):
        # Vence em 10/03/2026; o relógio do caso está em 02/03/2026: faixa de 8 dias.
        self.contratar(data_inicio=date(2026, 1, 1), data_fim=date(2026, 3, 10))
        self.ativar_regras()

    def capturar_telas(self, *numeros):
        """O HTML de cada tela de TRECHOS_DE_HOJE para cada valor do número no config, na ordem
        em que o estado muda: sem chamado e sem contrato; depois com um chamado e o contrato
        vencendo em 8 dias (a faixa de renovação); por fim, com a assinatura suspensa.
        O cliente precisa ter entrado antes (self.login())."""
        import entitlements
        from models import ChamadoSuporte
        telas = {numero: {} for numero in numeros}

        def desenhar(nome, caminho=None, status=200, cliente=None):
            for numero in numeros:
                self.com_numero(numero)
                telas[numero][nome] = self.upgrade_sem_plano() if caminho is None else self.html(caminho, status, cliente)

        desenhar("landing", "/", cliente=self.anonimo)
        desenhar("como_aderir_visitante", "/como-aderir", cliente=self.anonimo)
        desenhar("login", "/auth/login", cliente=self.anonimo)
        desenhar("recuperar_senha", "/auth/recuperar-senha", cliente=self.anonimo)
        desenhar("como_aderir_cliente", "/como-aderir")
        desenhar("painel_sem_faixa", "/cliente/dashboard")
        desenhar("suporte_vazio", "/cliente/tickets")
        desenhar("planos", "/onboarding/planos")
        desenhar("upgrade_plano", "/onboarding/diagnostico/auditoria", 403)
        desenhar("upgrade_sem_plano")
        with patch.object(entitlements, "esquema_planos_disponivel", return_value=False):
            desenhar("planos_503", "/onboarding/planos", 503)
            desenhar("upgrade_503", "/onboarding/diagnostico/auditoria", 503)
        chamado = ChamadoSuporte(id_empresa=self.empresa_id, id_usuario_cliente=self.cliente_id,
                                 assunto="Chamado fictício", categoria="DUVIDA_TECNICA")
        self.db.session.add(chamado)
        self.db.session.commit()
        self.contrato_vencendo()
        desenhar("painel_com_faixa", "/cliente/dashboard")
        desenhar("suporte_com_chamado", "/cliente/tickets")
        desenhar("chamado", f"/cliente/tickets/{chamado.id_chamado}")
        self.mudar_status_empresa("SUSPENSA")
        desenhar("upgrade_suspensa", "/onboarding/diagnostico", 403)
        for numero in numeros:
            self.assertEqual(sorted(telas[numero]), sorted(TRECHOS_DE_HOJE))
        return telas

    def assert_link(self, link, mensagem, rotulo, *, classe, descrito_por=None, nome=None):
        attrs = link["attrs"]
        href = attrs.get("href", "")
        self.assertTrue(href.startswith(PREFIXO), href)
        partes = urlsplit(href)
        self.assertEqual((partes.scheme, partes.netloc, partes.path, partes.fragment),
                         ("https", "wa.me", "/" + NUMERO, ""), href)
        self.assertEqual(parse_qs(partes.query, keep_blank_values=True, strict_parsing=True),
                         {"text": [mensagem]}, href)
        self.assertEqual(attrs.get("target"), "_blank", href)
        self.assertEqual(attrs.get("rel"), "noopener noreferrer", href)
        self.assertEqual([nome_attr for nome_attr in attrs if nome_attr.lower().startswith("on")], [], href)
        self.assertEqual(attrs.get("class"), classe, href)
        self.assertEqual(attrs.get("aria-describedby"), descrito_por, href)
        self.assertEqual(link["visivel"], rotulo, href)
        self.assertEqual(link["nome"], nome or f"{rotulo} {NOVA_ABA}", href)

    def assert_sem_mencao_ao_whatsapp(self, html, contexto):
        """Nenhum wa.me e nenhuma menção ao WhatsApp, nem em atributo, fora o recurso de catálogo."""
        limpo = html.replace(RECURSO_DE_CATALOGO, "")
        mencoes = [limpo[max(0, achado.start() - 80):achado.end() + 40]
                   for achado in re.finditer(r"wa\.me|whatsapp", limpo, re.IGNORECASE)]
        self.assertEqual(mencoes, [], contexto)

    def assert_mesmo_html(self, obtido, esperado, contexto, rotulos=("hoje", "agora")):
        if obtido != esperado:
            diferenca = "\n".join(difflib.unified_diff(esperado.splitlines(), obtido.splitlines(), *rotulos, lineterm=""))
            self.fail(f"{contexto}: o HTML mudou.\n{diferenca}")

    def assert_tela_de_hoje(self, nome, html, contexto=""):
        """A tela como era antes do WhatsApp: nenhuma menção a ele e, em volta de cada ponto em
        que ele entra, o HTML de hoje, byte a byte (nem texto novo, nem elemento vazio a mais)."""
        rotulo = f"{nome}{contexto}"
        self.assert_sem_mencao_ao_whatsapp(html, rotulo)
        if nome == "landing":
            self.assertTrue(html.startswith(COMECO_DA_LANDING), f"{rotulo}: {html[:80]!r}")
        for inicio, fim, esperado in TRECHOS_DE_HOJE[nome]:
            try:
                comeco, final = _intervalo(html, inicio, fim)
            except AssertionError as erro:
                self.fail(f"{rotulo}: {erro}")
            self.assert_mesmo_html(html[comeco:final], esperado, f"{rotulo}, trecho {inicio!r}")


# =====================================================================
# Sem número (padrão) e com número malformado: as telas de hoje
# =====================================================================
class SemNumeroTests(_WhatsAppCase):
    def test_sem_numero_cada_tela_sai_como_hoje(self):
        """O padrão (o DeployTestCase deixa o número vazio): cada tela, inclusive os 503, a faixa
        de renovação, o chamado e a assinatura suspensa, sai como antes do WhatsApp."""
        self.assertEqual(self.app.config["WHATSAPP_EMPRESA_NUMERO"], "")
        self.login()
        for nome, html in self.capturar_telas("")[""].items():
            with self.subTest(tela=nome):
                self.assert_tela_de_hoje(nome, html)

    def test_numero_malformado_nao_desenha_link_e_mantem_as_telas_de_hoje(self):
        """Cada template decide pelo número já validado (m.numero_whatsapp), não pelo config cru."""
        self.login()
        for valor, telas in self.capturar_telas(*VALORES_MALFORMADOS).items():
            for nome, html in telas.items():
                with self.subTest(valor=valor, tela=nome):
                    self.assert_tela_de_hoje(nome, html, f" com {valor!r}")

    def test_o_numero_so_muda_os_trechos_de_referencia(self):
        """Com número, cada trecho de TRECHOS_DE_HOJE muda (as âncoras cercam mesmo os pontos de
        inserção) e nada fora deles muda (não há ponto de inserção sem referência). As telas sem
        WhatsApp saem iguais com e sem número."""
        self.login()
        telas = self.capturar_telas("", NUMERO)
        for nome, trechos in TRECHOS_DE_HOJE.items():
            sem, com = telas[""][nome], telas[NUMERO][nome]
            with self.subTest(tela=nome):
                self.assert_mesmo_html(_fora_dos_trechos(com, trechos), _fora_dos_trechos(sem, trechos),
                                       f"{nome}, fora dos trechos de referência", ("sem número", "com número"))
                if nome in TELAS_SEM_WHATSAPP:
                    self.assert_mesmo_html(_normalizar(com), _normalizar(sem), nome, ("sem número", "com número"))
                    continue
                for inicio, fim, esperado in trechos:
                    comeco, final = _intervalo(com, inicio, fim)
                    self.assertNotEqual(com[comeco:final], esperado, f"{nome}, trecho {inicio!r}")
        self.assertTrue(telas[NUMERO]["landing"].startswith(COMECO_DA_LANDING))


# =====================================================================
# Com número: cada tela e cada contexto de mensagem
# =====================================================================
class ComNumeroTests(_WhatsAppCase):
    def setUp(self):
        super().setUp()
        self.com_numero()

    def test_landing_contrata_cada_plano_e_tira_duvidas_pelo_whatsapp(self):
        html, dom = self.pagina("/", cliente=self.anonimo)
        for plano in ("BRONZE", "PRATA", "OURO"):
            with self.subTest(plano=plano):
                cartao = _trecho(html, f'<article class="plan {plano.lower()}', "</article>")
                [link] = _Pagina(cartao).whatsapp()
                cor = "primary" if plano == "OURO" else "secondary"
                self.assert_link(link, mensagem_plano(plano), "Contrate Agora",
                                 classe=f"btn-nexo btn-nexo-{cor} w-100", descrito_por="contratacao-status",
                                 nome=f"Contrate Agora o plano {plano} {NOVA_ABA}")
                self.assertNotIn("<button", cartao)
        [cta] = _Pagina(_trecho(html, 'aria-labelledby="final-cta-title"', "</section>")).whatsapp()
        self.assert_link(cta, MENSAGENS["duvidas"], "Falar com a equipe NEXO",
                         classe="btn-nexo btn-nexo-primary btn-nexo-lg", descrito_por="contact-availability")
        [rodape] = _Pagina(_trecho(html, "<footer", "</footer>")).whatsapp()
        # Sem classe: o mesmo estilo de .footer-col a do vizinho "Entenda como obter acesso".
        self.assert_link(rodape, MENSAGENS["duvidas"], "Fale conosco no WhatsApp", classe=None)
        self.assertEqual(len(dom.whatsapp()), 5)
        # Os textos dizem a verdade: nada de "canal ainda não divulgado" ao lado do link.
        self.assertIn("Contrate pelo WhatsApp: a equipe NEXO responde e combina o início com você.", dom.texto)
        self.assertIn("Atendimento comercial pelo WhatsApp", dom.texto)
        for frase in ("A contratação ainda não está disponível", "será divulgado nesta página",
                      "serão divulgados nesta página"):
            self.assertNotIn(frase, dom.texto)
        self.assertEqual(dom.texto.count("Contrate Agora"), 3)
        self.assertNotIn('disabled aria-describedby="contratacao-status"', html)
        self.assertNotIn('aria-describedby="contact-availability">Falar', html)
        # O que não é contato continua igual: avaliação desabilitada e cabeçalho com um só link de portal.
        self.assertIn('disabled aria-describedby="reviews-availability">Deixar Avaliação</button>', html)
        cabecalho = _trecho(html, "<header", "</header>")
        self.assertEqual(cabecalho.count('href="/auth/login"'), 1)
        self.assertNotIn("wa.me", cabecalho)
        # A landing não carrega o Bootstrap Icons: o ícone é SVG inline, escondido do leitor de tela.
        self.assertNotIn("bi-whatsapp", html)
        self.assertEqual(html.count('<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3.5'), 4)
        identificadores = dom.ids()
        self.assertEqual(len(identificadores), len(set(identificadores)))
        for _, attrs in dom.elementos:
            for nome in ("aria-controls", "aria-describedby", "aria-labelledby"):
                for alvo in attrs.get(nome, "").split():
                    self.assertIn(alvo, identificadores, (nome, alvo))

    def test_como_aderir_contrata_e_tira_duvidas_com_e_sem_sessao(self):
        self.login()
        for cliente, rotulo in ((self.anonimo, "visitante"), (self.client, "cliente")):
            with self.subTest(sessao=rotulo):
                html, dom = self.pagina("/como-aderir", cliente=cliente)
                contratar, duvidas = dom.whatsapp()
                self.assert_link(contratar, MENSAGENS["contratar"], "Contratar pelo WhatsApp", classe="btn-nexo")
                self.assert_link(duvidas, MENSAGENS["duvidas"], "Tirar dúvidas pelo WhatsApp", classe="btn-ghost")
                bloco = _trecho(html, "Atendimento comercial</h2>", "Já recebeu seu acesso?")
                self.assertEqual(len(_Pagina(bloco).whatsapp()), 2)
                self.assertIn("Fale com a equipe NEXO pelo WhatsApp para contratar um plano", dom.texto)
                self.assertNotIn("ainda não foi publicado", dom.texto)
                self.assertIn('<i class="bi bi-whatsapp" aria-hidden="true"></i>', bloco)

    def test_login_fica_como_hoje_e_leva_ao_como_aderir(self):
        # O cartão de login cabe em 375x667 sem rolagem (static/css/nexo.css): o contato comercial
        # fica no Como aderir, a um clique do "É novo por aqui?", e não no cartão.
        html, dom = self.pagina("/auth/login", cliente=self.anonimo)
        self.assertEqual(dom.whatsapp(), [])
        self.assert_sem_mencao_ao_whatsapp(html, "/auth/login")
        self.assertIn('href="/como-aderir"', html)
        self.assertEqual(len(self.pagina("/como-aderir", cliente=self.anonimo)[1].whatsapp()), 2)
        # Recuperação de senha e primeiro acesso continuam só por e-mail.
        html, _ = self.pagina("/auth/recuperar-senha", cliente=self.anonimo)
        self.assert_sem_mencao_ao_whatsapp(html, "/auth/recuperar-senha")

    def test_planos_pede_a_mudanca_pelo_whatsapp_e_mantem_o_pedido_por_chamado(self):
        self.login()
        html, dom = self.pagina("/onboarding/planos")
        [link] = dom.whatsapp()
        self.assert_link(link, MENSAGENS["mudar_plano"], "Pedir a mudança pelo WhatsApp", classe="btn-ghost")
        secao = _trecho(html, 'aria-labelledby="mudarPlanoTitulo"', "</section>")
        self.assertEqual(len(_Pagina(secao).whatsapp()), 1)
        self.assertIn('action="/cliente/tickets"', secao)
        self.assertIn("Pedir a mudança pelo Suporte</button>", secao)
        self.assertIn("Se preferir, peça pelo WhatsApp.", secao)
        self.assertTrue(re.findall(r'<option value="Mudança de plano: [^"]+"', secao))

    def test_planos_indisponivel_oferece_duvidas_pelo_whatsapp(self):
        import entitlements
        self.login()
        with patch.object(entitlements, "esquema_planos_disponivel", return_value=False):
            html, dom = self.pagina("/onboarding/planos", 503)
        [link] = dom.whatsapp()
        self.assert_link(link, MENSAGENS["duvidas"], "Tirar dúvidas pelo WhatsApp", classe="btn-ghost")
        self.assertIn("abra um chamado no Suporte ou fale com a equipe NEXO pelo WhatsApp.", dom.texto)

    def test_upgrade_oferece_o_contato_certo_para_cada_motivo(self):
        import entitlements
        self.login()
        html, dom = self.pagina("/onboarding/diagnostico/auditoria", 403)
        [link] = dom.whatsapp()
        self.assert_link(link, MENSAGENS["mudar_plano"], "Mudar de plano pelo WhatsApp", classe="btn-ghost")
        self.assertIn('href="/onboarding/planos#prata"', html)
        # Sem o esquema dos planos (503) não há convite de upgrade, nem pelo WhatsApp.
        with patch.object(entitlements, "esquema_planos_disponivel", return_value=False):
            html, dom = self.pagina("/onboarding/diagnostico/auditoria", 503)
        self.assertEqual(dom.whatsapp(), [])
        # Recurso que nenhum plano libera: o contato é para dúvidas.
        [link] = _Pagina(self.upgrade_sem_plano()).whatsapp()
        self.assert_link(link, MENSAGENS["duvidas"], "Tirar dúvidas pelo WhatsApp", classe="btn-ghost")
        # Assinatura não ativa: o pedido vai ao Suporte, e o WhatsApp é de atendimento.
        self.mudar_status_empresa("SUSPENSA")
        html, dom = self.pagina("/onboarding/diagnostico", 403)
        [link] = dom.whatsapp()
        self.assert_link(link, MENSAGENS["suporte"], "Atendimento pelo WhatsApp", classe="btn-ghost")
        self.assertIn('href="/cliente/tickets" class="btn-nexo"', html)

    def test_painel_tem_atendimento_no_card_de_contato_e_renovacao_na_faixa(self):
        self.contrato_vencendo()
        self.login()
        html, dom = self.pagina("/cliente/dashboard")
        faixa = _trecho(html, 'class="aviso-contrato is-alerta"', "</p>")
        self.assertIn("Seu contrato vence em 8 dias.", faixa)
        [renovar] = _Pagina(faixa).whatsapp()
        self.assert_link(renovar, MENSAGENS["renovar"], "falar pelo WhatsApp", classe=None)
        self.assertIn('<a href="/cliente/tickets">abrir um chamado</a> ou <a href="https://wa.me/', faixa)
        cartao = _trecho(html, 'id="contato-nexo"', "</main>")
        pagina = _Pagina(cartao)
        [suporte] = pagina.whatsapp()
        self.assert_link(suporte, MENSAGENS["suporte"], "Atendimento pelo WhatsApp", classe=None)
        self.assertEqual([link["attrs"]["href"] for link in pagina.links][0], "/cliente/tickets")
        self.assertIn("envie um chamado pelo suporte do portal ou fale com a equipe NEXO pelo WhatsApp.", pagina.texto)
        self.assertEqual(len(dom.whatsapp()), 2)

    def test_painel_sem_renovacao_tem_so_o_card_de_contato(self):
        self.login()
        html, dom = self.pagina("/cliente/dashboard")
        self.assertNotIn("Seu contrato vence", html)
        [suporte] = dom.whatsapp()
        self.assert_link(suporte, MENSAGENS["suporte"], "Atendimento pelo WhatsApp", classe=None)

    def test_suporte_atende_pelo_whatsapp_so_com_o_numero_do_chamado(self):
        from models import ChamadoSuporte, Empresa
        self.login()
        html, dom = self.pagina("/cliente/tickets")
        [link] = _Pagina(_trecho(html, 'class="page-head-actions"', "</div>")).whatsapp()
        self.assert_link(link, MENSAGENS["suporte"], "Atendimento pelo WhatsApp", classe="btn-ghost")
        self.assertEqual(len(dom.whatsapp()), 1)
        assunto = "Assunto sigiloso da empresa fictícia"
        chamado = ChamadoSuporte(id_empresa=self.empresa_id, id_usuario_cliente=self.cliente_id,
                                 assunto=assunto, categoria="DUVIDA_TECNICA")
        self.db.session.add(chamado)
        self.db.session.commit()
        html, dom = self.pagina(f"/cliente/tickets/{chamado.id_chamado}")
        [link] = _Pagina(_trecho(html, 'class="page-head-actions"', "</div>")).whatsapp()
        self.assert_link(link, mensagem_chamado(chamado.id_chamado), "Falar pelo WhatsApp", classe="btn-ghost",
                         nome=f"Falar pelo WhatsApp sobre o chamado #{chamado.id_chamado} {NOVA_ABA}")
        self.assertEqual(len(dom.whatsapp()), 1)
        texto = parse_qs(urlsplit(link["attrs"]["href"]).query)["text"][0]
        empresa = self.db.session.get(Empresa, self.empresa_id)
        for privado in (assunto, empresa.razao_social, empresa.nome_fantasia, empresa.cnpj,
                        "Cliente fictício", "cliente-test@example.invalid"):
            self.assertNotIn(privado, texto)
        # A lista também ganha o link na versão com chamados (cabeçalho igual).
        html, dom = self.pagina("/cliente/tickets")
        self.assertEqual(len(dom.whatsapp()), 1)


# =====================================================================
# Macro e textos (templates/_macros.html)
# =====================================================================
class NumeroDoAmbienteTests(_WhatsAppCase):
    """De ponta a ponta: o valor como vem do .env (cru) passa pela normalização da factory
    (whatsapp_service.configurar) até o href das telas."""

    def test_valor_cru_do_env_vira_o_link_certo_nas_telas(self):
        import whatsapp_service
        for bruto in ("+55 (62) 98888-7777", "(62) 98888-7777", "62988887777", " 0055 62 98888-7777 "):
            with self.subTest(bruto=bruto):
                self.app.config["WHATSAPP_EMPRESA_NUMERO"] = bruto
                whatsapp_service.configurar(self.app)
                self.assertEqual(self.app.config["WHATSAPP_EMPRESA_NUMERO"], NUMERO)
                _, dom = self.pagina("/", cliente=self.anonimo)
                links = dom.whatsapp()
                self.assertEqual(len(links), 5)
                for link in links:
                    self.assertTrue(link["attrs"]["href"].startswith(PREFIXO), link["attrs"]["href"])

    def test_valor_de_exemplo_ou_invalido_no_env_esconde_os_botoes(self):
        import whatsapp_service
        for bruto in ("5562999999999", "(62) 99999-9999", "62 3222-1234 ramal 2", "+1 202 555 0100"):
            with self.subTest(bruto=bruto):
                self.app.config["WHATSAPP_EMPRESA_NUMERO"] = bruto
                whatsapp_service.configurar(self.app)
                self.assertEqual(self.app.config["WHATSAPP_EMPRESA_NUMERO"], "")
                self.assertTrue(self.app.config["WHATSAPP_EMPRESA_NUMERO_INVALIDO"])
                html, dom = self.pagina("/", cliente=self.anonimo)
                self.assertEqual(dom.whatsapp(), [])
                self.assert_tela_de_hoje("landing", html, bruto)


class MacroWhatsAppTests(_WhatsAppCase):
    def _macros(self):
        return self.app.jinja_env.get_template("_macros.html").module

    def test_macro_codifica_a_mensagem_e_escapa_o_rotulo(self):
        macros = self._macros()
        texto = 'Olá! A&B <b>"x"</b> #7 ?x=1/2 100% +ok'
        html = str(macros.link_whatsapp(NUMERO, texto, "<Rótulo>", classe=None, icone=None, descrito_por="aviso"))
        [link] = _Pagina(html).whatsapp()
        self.assert_link(link, texto, "<Rótulo>", classe=None, descrito_por="aviso")
        self.assertIn("&lt;Rótulo&gt;", html)
        self.assertNotIn("<b>", html)
        self.assertNotIn("<i ", html)
        self.assertNotIn("<svg", html)

    def test_macro_icones_contexto_e_classe(self):
        macros = self._macros()
        portal = str(macros.link_whatsapp(NUMERO, "x", "Rótulo"))
        self.assertIn('<a class="btn-ghost" href="' + PREFIXO + 'x"', portal)
        self.assertIn('<i class="bi bi-whatsapp" aria-hidden="true"></i> Rótulo', portal)
        landing = str(macros.link_whatsapp(NUMERO, "x", "Rótulo", classe="btn-nexo", icone="svg", contexto="o plano OURO"))
        self.assertIn('<svg class="inline-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">', landing)
        self.assertNotIn("bi-whatsapp", landing)
        [link] = _Pagina(landing).whatsapp()
        self.assert_link(link, "x", "Rótulo", classe="btn-nexo", nome=f"Rótulo o plano OURO {NOVA_ABA}")

    def test_numero_invalido_nao_desenha_nada(self):
        macros = self._macros()
        self.assertEqual(str(macros.numero_whatsapp(NUMERO)), NUMERO)
        self.assertEqual(str(macros.numero_whatsapp("556298888777")), "556298888777")
        for invalido in ("", None, "62 9999-abc", "+5562988887777", "5562-98888-7777", "５５６２９８８８８７７７７",
                         "123456789", "5562988887777123", 5562988887777, True, ["5562988887777"],
                         # Sem o DDI 55, o wa.me leria outro país (62... é a Indonésia; 1... os EUA).
                         "62988887777", "6298888777", "1202555010099", "55629888877"):
            with self.subTest(numero=invalido):
                self.assertEqual(str(macros.numero_whatsapp(invalido)), "")
                self.assertEqual(str(macros.link_whatsapp(invalido, "x", "Rótulo")), "")

    def test_textos_fixos_sem_dado_do_cliente(self):
        textos = self._macros().whatsapp_textos
        self.assertEqual(textos, {
            **MENSAGENS,
            "contratar_plano": "Olá! Quero contratar o plano %s do NEXO Faturamento Inteligente.",
            "chamado": "Olá! Preciso de ajuda com o chamado #%s no NEXO Faturamento Inteligente.",
        })


class FluxosSoPorEmailTests(unittest.TestCase):
    def test_cadastro_e_recuperacao_de_senha_continuam_sem_whatsapp(self):
        """O cadastro inicial e a recuperação de senha com token seguem só por e-mail."""
        templates = ROOT / "templates"
        alvos = sorted(templates.joinpath("emails").rglob("*.*")) + [
            templates / "auth" / nome for nome in ("recuperar_senha.html", "resetar_senha.html",
                                                    "primeiro_acesso.html", "primeiro_acesso_token.html")]
        self.assertGreater(len(alvos), 10)
        self.assertTrue(all(caminho.is_file() for caminho in alvos))
        for caminho in alvos:
            conteudo = caminho.read_text(encoding="utf-8")
            for termo in ("wa.me", "link_whatsapp", "whatsapp_textos", "WHATSAPP_EMPRESA_NUMERO"):
                self.assertNotIn(termo, conteudo, caminho.relative_to(ROOT))


if __name__ == "__main__":
    unittest.main()
