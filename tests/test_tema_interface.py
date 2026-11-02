"""Tema claro/escuro do portal: verificações sem navegador (folha clara, templates e HTML servido).

O tema escuro de nexo.css é o padrão e não muda; o claro fica em nexo-tema-claro.css, com todo
seletor preso a html[data-theme="light"], e só existe com sessão. O comportamento em navegador
real (persistência, primeira pintura, contraste, CSP) está em tests/test_tema_navegador.py.

Executar da raiz: python -m unittest tests.test_tema_interface -v
"""

import re
import unittest
from pathlib import Path

from tests.deploy_support import DeployTestCase

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
BASE = (TEMPLATES / "base.html").read_text(encoding="utf-8")
FOLHA = (ROOT / "static" / "css" / "nexo-tema-claro.css").read_text(encoding="utf-8")
ESCURO = (ROOT / "static" / "css" / "nexo.css").read_text(encoding="utf-8")
PREFIXO = 'html[data-theme="light"]'
_BOTAO = re.compile(r'<button\b[^>]*\bid="themeToggle"[^>]*>')
_ESTADO = re.compile(r":(hover|focus-visible|focus)\b")
_PINTURA = {"background": "fundo", "background-color": "fundo", "color": "cor", "border-color": "borda", "box-shadow": "sombra"}


def _regras(css):
    """(seletor, corpo) de toda regra, inclusive dentro de @media e @supports."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    regras, pos = [], 0
    while True:
        abre = css.find("{", pos)
        if abre < 0:
            return regras
        cabecalho = css[pos:abre].strip()
        if cabecalho.startswith("@"):
            profundidade, fim = 1, abre + 1
            while profundidade:
                profundidade += {"{": 1, "}": -1}.get(css[fim], 0)
                fim += 1
            regras += _regras(css[abre + 1:fim - 1])
            pos = fim
        else:
            fim = css.find("}", abre)
            regras.append((cabecalho, css[abre + 1:fim]))
            pos = fim + 1


def _seletores(cabecalho):
    """Separa a lista de seletores nas vírgulas de fora de parênteses e colchetes."""
    partes, nivel, atual = [], 0, ""
    for ch in cabecalho:
        nivel += 1 if ch in "([" else -1 if ch in ")]" else 0
        if ch == "," and nivel == 0:
            partes.append(atual.strip())
            atual = ""
        else:
            atual += ch
    return partes + [atual.strip()]


def _pintura(corpo):
    """O que a regra pinta, juntando as propriedades que pintam a mesma coisa."""
    nomes = (declaracao.split(":", 1)[0].strip() for declaracao in corpo.split(";") if ":" in declaracao)
    return {_PINTURA[nome] for nome in nomes if nome in _PINTURA}


def _composto(seletor):
    """Classes e atributos do último composto, sem estados nem pseudo-classes."""
    ultimo = re.split(r"[\s>+~]+", _ESTADO.sub("", seletor))[-1]
    return set(re.findall(r"\.[\w-]+|\[[^\]]+\]", re.sub(r":[\w-]+(?:\([^)]*\))?", "", ultimo)))


def _ancestrais(seletor):
    return set(re.findall(r"\.[\w-]+", " ".join(re.split(r"[\s>+~]+", seletor)[:-1])))


def _especificidade(seletor):
    """(classes, etiquetas); o argumento de :has() conta como parte do seletor."""
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]+\]|:(?!has\b|not\b)[\w-]+", seletor))
    return classes, len(re.findall(r"(?:^|[\s>+~(])[a-z][\w-]*", seletor))


def _vence(a, ordem_a, b, ordem_b):
    return (_especificidade(a), ordem_a) > (_especificidade(b), ordem_b)


def _rgb(cor):
    cor = cor.strip().lstrip("#")
    return tuple(int(cor[i:i + 2], 16) for i in (0, 2, 4))


def _luminancia(rgb):
    def canal(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * canal(rgb[0]) + 0.7152 * canal(rgb[1]) + 0.0722 * canal(rgb[2])


def _razao(a, b):
    x, y = _luminancia(_rgb(a)), _luminancia(_rgb(b))
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


class FolhaClaraTests(unittest.TestCase):
    def test_todo_seletor_comeca_pelo_atributo_do_tema(self):
        regras = _regras(FOLHA)
        self.assertGreater(len(regras), 100)
        for cabecalho, _corpo in regras:
            for seletor in _seletores(cabecalho):
                with self.subTest(seletor=seletor):
                    self.assertTrue(seletor.startswith(PREFIXO), seletor)

    def test_nao_estiliza_os_simbolos_da_marca(self):
        """Identidade aprovada: nenhuma regra alcança a logo, nem para pintar nem para recortar."""
        proibidos = re.compile(r"\.(?:auth-logo|brand-logo|brand-symbol|brand-text)(?![\w-])")
        for cabecalho, corpo in _regras(FOLHA):
            self.assertIsNone(proibidos.search(cabecalho), f"{cabecalho} {{{corpo.strip()}}}")

    def test_sidebar_continua_escura_e_quase_opaca(self):
        corpo = next(c for s, c in _regras(FOLHA) if s == f"{PREFIXO} .sidebar" and "--text:" in c)
        self.assertIn("--text:           #e8e9f0", corpo)
        fundo = re.search(r"background:\s*rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", corpo)
        self.assertIsNotNone(fundo, corpo)
        self.assertGreaterEqual(float(fundo.group(4)), 0.95)
        self.assertLess(_luminancia(tuple(int(fundo.group(i)) for i in (1, 2, 3))), 0.02)

    def test_texto_em_gradiente_vira_cor_solida(self):
        corpo = next(c for s, c in _regras(FOLHA) if s == f"{PREFIXO} .page-head h1")
        self.assertRegex(corpo, r"background:\s*none")
        self.assertRegex(corpo, r"(?<![-\w])background-clip:\s*border-box")
        self.assertRegex(corpo, r"(?<![-\w])color:\s*var\(--text\)")

    def test_tokens_passam_aa_sobre_as_superficies(self):
        tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", next(c for s, c in _regras(FOLHA) if s == PREFIXO)))
        superficies = ("--bg", "--surface", "--surface-2", "--surface-3", "--lilas-opaco")
        for texto in ("--text", "--text-muted", "--text-dim", "--primary-2", "--roxo-texto", "--roxo-forte",
                      "--verde-texto", "--ambar-texto", "--vermelho-texto", "--azul-texto"):
            for fundo in superficies:
                with self.subTest(texto=texto, fundo=fundo):
                    self.assertGreaterEqual(_razao(tokens[texto], tokens[fundo]), 4.5)
        for fundo in superficies:
            with self.subTest(borda=fundo):
                self.assertGreaterEqual(_razao(tokens["--campo-borda"], tokens[fundo]), 3)

    def test_ramos_sem_desfoque_e_com_transparencia_reduzida_ficam_claros(self):
        """Nesses ramos nexo.css pinta a topbar de escuro: a folha clara também os cobre."""
        self.assertRegex(FOLHA, r"@media \(prefers-reduced-transparency: reduce\) \{\s*html\[data-theme=\"light\"\] \.topbar \{ background: #ffffff")
        self.assertRegex(FOLHA, r"@supports not \(\(backdrop-filter: blur\(1px\)\).*\{\s*html\[data-theme=\"light\"\] \.topbar \{ background: #ffffff")

    def test_indicador_de_foco_dos_componentes_sobre_outros_fundos(self):
        """Medidos em pixels por tests/test_tema_navegador.py; aqui, só que as regras continuam na folha."""
        regras = dict(_regras(FOLHA))
        self.assertIn("outline-color: var(--roxo-forte)", regras[f"{PREFIXO} .sidebar-close:focus-visible"])
        self.assertIn("outline: 2px solid var(--primary-2)", regras[f"{PREFIXO} .app-shell .form-check-input:focus-visible"])
        pular = regras[f"{PREFIXO} .skip-link:focus"]
        self.assertIn("outline-color: #ffffff", pular)
        self.assertIn("0 0 0 4px var(--roxo-forte)", pular)

    def test_regra_de_repouso_nao_anula_o_estado(self):
        """O prefixo soma especificidade: a regra clara de repouso vence a de :hover/:focus de
        nexo.css, e dentro da folha a última vence no empate. Sem a regra de estado que vença o
        repouso, o retorno some sem erro nenhum. Fica de fora o que o escuro também anula."""
        escuro = [(s, _pintura(c)) for cabecalho, c in _regras(ESCURO) for s in _seletores(cabecalho)]
        claro = [(s, _pintura(c)) for cabecalho, c in _regras(FOLHA) for s in _seletores(cabecalho)]
        estados_claros = [(k, s, p) for k, (s, p) in enumerate(claro) if _ESTADO.search(s)]
        anulados = []
        for i, (estado, pinta) in enumerate(escuro):
            achado = _ESTADO.search(estado)
            if not achado or not _composto(estado):
                continue
            for k, (repouso, pinta_clara) in enumerate(claro):
                sem_prefixo = repouso[len(PREFIXO):].strip()
                comuns = pinta & pinta_clara
                if not comuns or _ESTADO.search(repouso) or not _composto(estado) <= _composto(repouso):
                    continue
                if not _ancestrais(estado) <= _ancestrais(sem_prefixo) | {".app-shell"}:
                    continue
                if estado.startswith("a.empresa-card") and "is-selecionada" in repouso:
                    continue  # a empresa selecionada é um <div>: o estado do link não a alcança
                if _especificidade(repouso) < _especificidade(estado):
                    continue  # o estado de nexo.css ainda vence
                gemeos = [j for j, (s, p) in enumerate(escuro) if s in (sem_prefixo, sem_prefixo.replace(".app-shell ", "")) and comuns <= p]
                if any(_vence(escuro[j][0], j, estado, i) for j in gemeos):
                    continue  # no escuro o repouso também vence o estado: mesmo comportamento
                cobertos = set()
                for ordem, outro, pinta_estado in estados_claros:
                    mesmo_componente = _composto(estado) <= _composto(outro) <= _composto(repouso)
                    if mesmo_componente and _ESTADO.search(outro).group(1) == achado.group(1) and _vence(outro, ordem, repouso, k):
                        cobertos |= pinta_estado
                if comuns - cobertos:
                    anulados.append(f"{estado} <- {sem_prefixo}: {sorted(comuns - cobertos)}")
        self.assertEqual(anulados, [])

    def test_tema_escuro_nao_depende_do_atributo_novo(self):
        self.assertNotIn("data-theme", ESCURO)
        self.assertNotIn("theme-toggle", ESCURO)


class TemplatesTests(unittest.TestCase):
    def test_html_nasce_no_escuro(self):
        self.assertIn('<html lang="pt-BR" data-bs-theme="dark" data-theme="dark">', BASE)

    def test_folha_clara_so_em_base_e_depois_de_nexo_css(self):
        for arquivo in TEMPLATES.rglob("*.html"):
            if arquivo.name != "base.html":
                self.assertNotIn("nexo-tema-claro", arquivo.read_text(encoding="utf-8"), str(arquivo))
        self.assertEqual(BASE.count("nexo-tema-claro.css"), 1)
        self.assertLess(BASE.index("css/nexo.css"), BASE.index("css/nexo-tema-claro.css"))

    def test_paginas_fora_do_portal_nao_tem_tema(self):
        for relativo in ("auth/_layout.html", "erro_simples.html", "landing.html"):
            conteudo = (TEMPLATES / relativo).read_text(encoding="utf-8")
            for termo in ("data-theme", "themeToggle", "localStorage"):
                self.assertNotIn(termo, conteudo, relativo)
        self.assertNotIn("data-theme", (ROOT / "static" / "css" / "landing.css").read_text(encoding="utf-8"))

    def test_script_do_head_roda_antes_das_folhas_de_estilo_e_so_com_sessao(self):
        cabeca = BASE[:BASE.index("</head>")]
        script = cabeca[cabeca.index("<script"):cabeca.index("</script>")]
        self.assertIn('nonce="{{ csp_nonce }}"', script)
        self.assertLess(cabeca.index("<script"), cabeca.index("<link"))
        self.assertLess(cabeca.rfind("{% if current_user.is_authenticated %}", 0, cabeca.index("<script")), cabeca.index("<script"))
        self.assertGreater(cabeca.rfind("{% if current_user.is_authenticated %}", 0, cabeca.index("<script")), -1)
        # Lê dentro de try/catch e só aceita os dois valores; nada do valor vai para o DOM sem o teste.
        self.assertRegex(script, r"try \{ tema = window\.localStorage\.getItem\('theme'\); \} catch")
        self.assertIn("if (tema === 'light' || tema === 'dark') {", script)
        self.assertIn("setAttribute('data-theme', tema)", script)
        self.assertIn("setAttribute('data-bs-theme', tema)", script)

    def test_nome_longo_cede_lugar_ao_titulo_da_topbar(self):
        """O limite acompanha a largura da janela: em 1366 px só os nomes enormes são cortados. O
        corte não tira as descendentes do nome que cabe, e o nome inteiro continua no title."""
        estilo = BASE[BASE.index("<style>"):BASE.index("</style>")]
        self.assertIn(".user-meta { max-width: calc(100vw - 810px); }", estilo)
        self.assertIn(".user-meta strong { margin: 0 -2px -2px; padding: 0 2px 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }", estilo)
        self.assertIn('<strong title="{{ current_user.nome }}">{{ current_user.nome }}</strong>', BASE)
        # Vale nos dois temas: não fica na folha clara.
        self.assertNotIn(".user-meta strong", FOLHA)

    def test_botao_antes_do_sino_e_sem_manipulador_inline(self):
        direita = BASE[BASE.index('<div class="topbar-right">'):BASE.index('<main class="page"')]
        botao = _BOTAO.search(direita)
        self.assertIsNotNone(botao)
        self.assertLess(botao.start(), direita.index('id="bellBtn"'))
        for trecho in ('type="button"', 'class="bell-btn theme-toggle"', 'aria-label="Ativar tema claro"'):
            self.assertIn(trecho, botao.group(0))
        self.assertIn('<i class="bi bi-sun" aria-hidden="true"></i>', direita)
        self.assertNotRegex(botao.group(0), r"\son\w+=")
        script = direita[direita.index("<script"):direita.index("</script>")]
        self.assertIn('nonce="{{ csp_nonce }}"', script)
        for trecho in ("'Ativar tema escuro'", "'bi-moon'", "'bi-sun'", "addEventListener('storage'", "'.js-plotly-plot'",
                       "window.location.reload()", "localStorage.setItem('theme', novo)", "NEXO.definirPaleta(tema)"):
            self.assertIn(trecho, script)
        # Sem armazenamento (exceção ao gravar) a página alterna sem recarregar.
        self.assertRegex(script, r"catch \(erro\) \{ gravado = false; \}")
        self.assertIn("if (graficos.length && gravado)", script)


class PaletaDosGraficosTests(unittest.TestCase):
    PALETA = BASE[BASE.index("NEXO.colors = NEXO.tema === 'light' ?"):BASE.index("/* Cores oficiais da Curva ABC")]
    CLARA, ESCURA = PALETA.split("} : {")

    def test_tema_lido_no_carregamento(self):
        self.assertIn("NEXO.definirPaleta(document.documentElement.getAttribute('data-theme'));", BASE)
        self.assertIn("NEXO.tema = tema === 'light' ? 'light' : 'dark';", BASE)

    def test_valores_do_tema_escuro_continuam_os_mesmos(self):
        """Os valores fixos de antes, agora nas chaves da paleta: o gráfico escuro não muda."""
        for valor in ("purple: '#a855f7'", "purpleSoft: '#7c3aed'", "lilac: '#c4a0fb'", "green: '#22c55e'", "amber: '#f59e0b'",
                      "red: '#ef4444'", "info: '#38bdf8'", "grid: 'rgba(255,255,255,0.07)'", "axis: 'rgba(255,255,255,0.12)'",
                      "text: '#aaa6c8'", "textStrong: '#ece9f8'", "textMax: '#ffffff'", "tooltip: 'rgba(17,13,32,0.96)'",
                      "tooltipBorder: 'rgba(196,160,251,0.55)'", "spike: 'rgba(196,160,251,0.6)'", "neutral: '#9aa0b7'",
                      "amberText: '#fbbf24'", "amberLine: '#fbbf24'", "amberGuide: 'rgba(251, 191, 36, 0.45)'",
                      "amberSoft: '#fbd07a'", "greenText: '#22c55e'", "markerLine: 'rgba(17,13,32,0.95)'",
                      "sliceLine: 'rgba(17,13,32,0.92)'", "barEdge: 'rgba(255,255,255,0.22)'",
                      "barEdgeStrong: 'rgba(255,255,255,0.28)'", "barAmber: 'rgba(245,158,11,0.9)'",
                      "barInfo: 'rgba(56,189,248,0.9)'", "barPurple: 'rgba(168,85,247,0.9)'", "barGreen: 'rgba(34,197,94,0.9)'",
                      "infoFade: 'rgba(56,189,248,0)'", "infoFill: 'rgba(56,189,248,0.24)'",
                      "abcA: 'rgba(168, 85, 247, 0.92)'", "abcB: 'rgba(168, 85, 247, 0.6)'", "abcC: 'rgba(168, 85, 247, 0.34)'",
                      "abcEdge: 'rgba(216, 194, 253, 0.55)'", "planBronze: 'rgba(224,168,120,0.92)'",
                      "planPrata: 'rgba(203,213,225,0.92)'", "planOuro: 'rgba(245,215,110,0.92)'"):
            with self.subTest(valor=valor):
                self.assertIn(valor, self.ESCURA)

    def test_as_duas_paletas_tem_as_mesmas_chaves(self):
        chave = re.compile(r"(\w+): '")
        self.assertEqual(sorted(chave.findall(self.CLARA)), sorted(chave.findall(self.ESCURA)))

    def test_texto_da_paleta_clara_passa_aa_sobre_o_cartao(self):
        for nome in ("text", "textStrong", "textMax", "neutral", "amberText", "amberSoft", "greenText", "lilac"):
            valor = re.search(rf"\b{nome}: '(#[0-9a-fA-F]{{6}})'", self.CLARA).group(1)
            with self.subTest(chave=nome):
                self.assertGreaterEqual(_razao(valor, "#ffffff"), 4.5)

    def test_codigo_dos_graficos_sem_cor_fixa(self):
        """Toda cor de texto e contorno vem de NEXO.colors; só o fundo transparente fica literal."""
        cor = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\((?!0,\s*0,\s*0,\s*0\))")
        graficos = BASE[BASE.index("/* Fonte única do tema dos gráficos"):BASE.index("Tempo real: sininho ao vivo")]
        self.assertEqual(cor.findall(graficos), [])
        for relativo in ("admin/dashboard.html", "cliente/dashboard.html", "cliente/analise.html"):
            conteudo = (TEMPLATES / relativo).read_text(encoding="utf-8")
            scripts = "".join(re.findall(r"<script\b[^>]*>(.*?)</script>", conteudo, re.S))
            with self.subTest(template=relativo):
                self.assertEqual(cor.findall(scripts), [])
                self.assertIn("NEXO.colors.", scripts)


class HtmlServidoTests(DeployTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures()

    def _pagina(self, caminho, status=200):
        resposta = self.client.get(caminho)
        self.assertEqual(resposta.status_code, status, caminho)
        return resposta

    def assert_sem_tema(self, html, caminho):
        with self.subTest(caminho=caminho):
            self.assertNotIn("localStorage", html)
            self.assertNotIn("themeToggle", html)
            self.assertNotIn("nexo-tema-claro", html)

    def test_sem_sessao_nada_do_tema_chega_ao_navegador(self):
        login = self._pagina("/auth/login").get_data(as_text=True)
        self.assert_sem_tema(login, "/auth/login")
        self.assertIn('data-bs-theme="dark" data-theme="dark"', login)
        for caminho, status in (("/", 200), ("/como-aderir", 200), ("/auth/recuperar-senha", 200), ("/rota-inexistente", 404)):
            self.assert_sem_tema(self._pagina(caminho, status).get_data(as_text=True), caminho)

    def test_com_sessao_script_com_nonce_folha_e_botao(self):
        for email, caminhos in (("admin-test@example.invalid", ("/admin/dashboard", "/admin/empresas", "/admin/configuracoes")),
                                ("cliente-test@example.invalid", ("/cliente/dashboard", "/cliente/tickets", "/cliente/configuracoes"))):
            self.client = self.app.test_client()
            self.login(email)
            for caminho in caminhos:
                with self.subTest(caminho=caminho):
                    resposta = self._pagina(caminho)
                    html = resposta.get_data(as_text=True)
                    nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", resposta.headers["Content-Security-Policy"]).group(1)
                    cabeca = html[:html.index("</head>")]
                    self.assertIn(f'<script nonce="{nonce}">', cabeca)
                    self.assertIn("localStorage.getItem('theme')", cabeca)
                    self.assertLess(cabeca.index("localStorage"), cabeca.index("<link"))
                    self.assertLess(cabeca.index("/static/css/nexo.css"), cabeca.index("/static/css/nexo-tema-claro.css"))
                    self.assertEqual(html.count('id="themeToggle"'), 1)
                    self.assertLess(html.index('id="themeToggle"'), html.index('id="bellBtn"'))
                    self.assertNotIn("<script>", html)
                    self.assertNotRegex(html, r"\son(click|submit|load|change|input)=")

    def test_arquivo_estatico_e_servido_como_css(self):
        resposta = self.client.get("/static/css/nexo-tema-claro.css")
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.mimetype, "text/css")
        resposta.close()


if __name__ == "__main__":
    unittest.main()
