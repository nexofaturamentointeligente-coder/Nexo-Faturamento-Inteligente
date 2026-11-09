"""Regressão de overflow, cortes e sobreposições em navegador real (Chromium via Playwright).

O que só o navegador mostra, medido pelas posições reais dos elementos (não só por
scrollWidth > clientWidth): uma tabela pode rolar certinho e ainda ter uma coluna
encoberta por outra fixa, um KPI pode "caber" porque a fonte encolheu demais e um
botão flutuante pode estar em cima de um controle.

Cenários, nas larguras 320, 360, 390, 768, 1024, 1366, 1536 e 1920 px (e no zoom
de 150% sobre 1366 px), com dados fictícios propositalmente extensos (razão social
e e-mail longos, produtos com nomes compridos, faturamento de nove dígitos):

  1. página sem rolagem horizontal e sem elemento visível fora da tela;
  2. tabelas: nenhuma célula fica encoberta por célula fixa; em modo tabela,
     o contêiner que rola recebe foco (setas rolam) e todas as colunas são
     alcançáveis; em modo cartões, cada valor leva o rótulo da coluna; os botões
     de cada linha continuam presentes e clicáveis pelo teclado;
  3. KPIs: nunca estouram o cartão e a fonte não desce de 18px;
  4. NexoBot: o botão flutuante não cobre nenhum controle nem título, no topo e
     no fim da página, e some com a gaveta do menu aberta;
  5. sidebar: o ponto do item ativo não encosta no rótulo ("Enviar relatórios");
  6. gráficos: rótulos sem reticências, sem corte e sem sobreposição, com
     alternativa textual (role="img" + aria-label);
  7. envio de relatórios: o campo de arquivo é alcançado pelo Tab, o foco fica
     visível na dropzone e enviar sem arquivo é barrado antes do POST;
  8. 404 dentro do portal: página do NEXO em português com link de volta
     (e JSON em /api/).

Rede: somente o servidor local; a CDN é respondida localmente com as cópias
íntegras de NEXO_TEST_CDN_DIR (hash SRI conferido, como em test_contratos_navegador).
Sem o Bootstrap real a classe de layout é pulada com o motivo registrado (medir a
grade sem a folha de estilo não diz nada sobre o produto); a CI baixa as cópias.
Plotly só é carregado quando NEXO_TEST_CDN_DIR/plotly.min.js existir; sem ele os
gráficos não são desenhados e as medições de gráfico não se aplicam. Habilitado
por NEXO_TEST_BROWSER=1, como na CI.
"""

import base64
import hashlib
import os
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.contratos_support import ContratosCase
from tests.test_contratos_navegador import (_BOOTSTRAP_MINIMO, _CDN, _CDN_INTEGRO, _copias_locais_da_cdn,
                                            _liberar_origem_socketio, _servir_fonte)
from tests.test_nexobot_navegador import _CONECTAR_LOCAL

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # dependência opcional de desenvolvimento
    sync_playwright = None


_BROWSER = os.environ.get("NEXO_TEST_BROWSER") == "1" and sync_playwright is not None
_PLOTLY_URL = _CDN + "plotly.js-dist-min@2.35.2/plotly.min.js"
# sha384 do plotly.min.js 2.35.2 publicado no jsdelivr (a cópia local só é servida se conferir).
_PLOTLY_SRI = "cCVCZkAjYNxaYKbM8lsArLznDF/SvMFr1jcZrvOpSTCa0W40ZAdLzHCEulnUa5i7"


def _plotly_local():
    pasta = os.environ.get("NEXO_TEST_CDN_DIR")
    arquivo = Path(pasta) / "plotly.min.js" if pasta else None
    if not arquivo or not arquivo.is_file():
        return None
    resumo = base64.b64encode(hashlib.sha384(arquivo.read_bytes()).digest()).decode()
    return str(arquivo) if resumo == _PLOTLY_SRI else None

LARGURAS = ((320, 568), (360, 780), (390, 844), (768, 1024), (1024, 768), (1366, 768), (1536, 864), (1920, 1080))
ZOOM_150 = (911, 512, 1.5)  # 1366 px a 150%: viewport CSS menor, escala maior
NOME_LONGO = "Distribuidora Internacional de Materiais Elétricos, Hidráulicos e Ferragens do Vale do Paraíba Ltda"
FANTASIA_LONGA = "Distribuidora Internacional Vale do Paraíba (unidade matriz)"
EMAIL_LONGO = "financeiro.contabilidade.administrativo@distribuidora-internacional-materiais.example.invalid"
# Categorias do lead extenso no limite de 80 caracteres que a staging guarda (matriz por categoria).
CATEGORIAS_LONGAS = [
    "Presentes, cestas e kits sazonais de fim de ano com embalagem especial e laço",
    "Mercearia fina, cafés especiais de origem controlada e bebidas quentes premium",
    "Utilidades domésticas, canecas térmicas, garrafas e acessórios de cozinha inox",
]
assert all(len(nome) <= 80 for nome in CATEGORIAS_LONGAS)
PRODUTOS_LONGOS = [
    "Kit presente premium de café especial torrado e moído com chocolate artesanal 70% cacau e caneca personalizada",
    "Caneca térmica inox 450 ml com tampa antivazamento e cabo emborrachado edição limitada",
    "Cesta de Natal grande com panetone artesanal, espumante e seleção de frutas secas importadas",
    # Par "gêmeo": nomes iguais nos 100 primeiros caracteres (diferem só na cor). O
    # Pareto encurta rótulos a 3 linhas, e os dois precisam continuar sendo duas barras.
    "Café especial torra média 500 g lote único da fazenda experimental da serra embalagem a vácuo presente azul",
    "Café especial torra média 500 g lote único da fazenda experimental da serra embalagem a vácuo presente verde",
    "Biscoito amanteigado artesanal lata decorativa 400 g",
    "Geleia de frutas vermelhas sem açúcar 250 g",
    "Mel orgânico de flores silvestres 500 g",
    "Granola premium com castanhas 1 kg",
    "Água mineral com gás 500 ml",
]

def _linhas_necessarias(texto, por_linha):
    """Mesma quebra gulosa por palavras de NEXO.quebrarRotulo (templates/base.html)."""
    linhas, atual = [], ""
    for palavra in texto.split():
        if not atual:
            atual = palavra
        elif len(atual) + 1 + len(palavra) <= por_linha:
            atual += " " + palavra
        else:
            linhas.append(atual)
            atual = palavra
    if atual:
        linhas.append(atual)
    return len(linhas)


_MEDIR = """() => {
    const vw = innerWidth, vh = innerHeight;
    const rect = el => { const b = el.getBoundingClientRect(); return {x0: b.left, y0: b.top, x1: b.right, y1: b.bottom, w: b.width, h: b.height}; };
    const inter = (a, b) => Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
    const desc = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '');
    const visivel = el => { const cs = getComputedStyle(el); return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0'; };
    const tabelas = [...document.querySelectorAll('.table-responsive-x')].map(caixa => {
        const cr = rect(caixa);
        const fixas = [...caixa.querySelectorAll('th, td')].filter(c => getComputedStyle(c).position === 'sticky');
        let encobertas = 0;
        for (const fixa of fixas) {
            const fr = rect(fixa);
            for (const outra of caixa.querySelectorAll('th, td')) {
                if (outra === fixa || getComputedStyle(outra).position === 'sticky') continue;
                const o = rect(outra);
                const vis = {x0: Math.max(o.x0, cr.x0), x1: Math.min(o.x1, cr.x1), y0: o.y0, y1: o.y1};
                if (vis.x1 > vis.x0 && inter(vis, fr) > 4 * (o.y1 - o.y0)) encobertas++;
            }
        }
        const cartoes = caixa.classList.contains('em-cartoes');
        const ths = [...caixa.querySelectorAll('thead th')];
        const ultimaColuna = ths.length ? rect(ths[ths.length - 1]) : null;
        const semRotulo = cartoes ? [...caixa.querySelectorAll('tbody tr:not([style*="none"]) td:not([colspan])')]
            .filter(td => td.textContent.trim() && !td.classList.contains('celula-acoes') && !td.getAttribute('data-label')).length : 0;
        const botoesForaDoCartao = cartoes ? [...caixa.querySelectorAll('tbody a, tbody button')].filter(b => { const r = rect(b); const tr = rect(b.closest('tr')); return r.w && (r.x1 > tr.x1 + 1 || r.x0 < tr.x0 - 1); }).length : 0;
        // Dica "Deslize…": o texto inteiro fica fora da faixa esmaecida da máscara (72 px à direita).
        const dica = caixa.querySelector('.dica-rolagem');
        let dicaEsmaecida = false;
        if (dica && visivel(dica) && !cartoes) {
            const faixa = cr.x1 - 72;
            const textos = [...dica.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim());
            for (const t of textos) { const r = document.createRange(); r.selectNodeContents(t); const tb = r.getBoundingClientRect(); if (tb.right > faixa + 1) dicaEsmaecida = true; }
        }
        return {rola: caixa.scrollWidth > caixa.clientWidth + 1, cartoes, encobertas, fixas: fixas.length, dicaEsmaecida,
                focavel: caixa.tabIndex === 0, colunas: ths.length, semRotulo, botoesForaDoCartao,
                ultimaAlcancavel: !ultimaColuna || cartoes || (ultimaColuna.x1 - cr.x0) <= caixa.scrollWidth + 1,
                largura_caixa: cr.w, dentroDaTela: cr.x0 >= -1 && cr.x1 <= vw + 1};
    });
    const kpis = [...document.querySelectorAll('.kpi-value')].map(el => ({
        texto: el.textContent.replace(/\\s+/g, ' ').trim(), fonte: parseFloat(getComputedStyle(el).fontSize),
        estoura: el.scrollWidth > el.clientWidth + 1}));
    const launcher = document.getElementById('nexobotLauncher');
    let nexobot = null;
    if (launcher && visivel(launcher)) {
        const lr = rect(launcher);
        const sobre = [...document.querySelectorAll('a, button, input, select, textarea, h1, h2, h3, .kpi-value, .sc-title')]
            .filter(el => el !== launcher && !el.closest('#nexobot') && visivel(el))
            .map(el => ({el, b: rect(el)})).filter(o => o.b.w && o.b.h && inter(o.b, lr) > 0)
            .map(o => desc(o.el) + ' "' + o.el.textContent.replace(/\\s+/g, ' ').trim().slice(0, 30) + '"');
        nexobot = {sobre, dentroDaTela: lr.x1 <= vw + 1 && lr.y1 <= vh + 1};
    }
    const ativo = document.querySelector('.sidebar .nav-item.active');
    let sidebar = null;
    if (ativo) {
        const textos = [...ativo.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim());
        const rg = document.createRange(); rg.selectNodeContents(textos[textos.length - 1]);
        const tb = rg.getBoundingClientRect();
        const cs = getComputedStyle(ativo, '::after');
        const ar = rect(ativo);
        let folga = null;
        if (cs.content !== 'none' && parseFloat(cs.width) > 0 && cs.position === 'absolute') {
            folga = (ar.x1 - (parseFloat(cs.right) || 0) - parseFloat(cs.width)) - tb.right;
        }
        sidebar = {folga, fluxo: cs.position !== 'absolute', rotulo: ativo.textContent.trim(), texto_direita: tb.right, item_direita: ar.x1};
    }
    const fora = [];
    for (const el of document.querySelectorAll('main.page *')) {
        if (el.closest('script, style, template, svg')) continue;
        if (!visivel(el)) continue;
        const b = el.getBoundingClientRect(); if (!b.width || !b.height) continue;
        // Limite visível do elemento: a viewport ou o ancestral mais próximo que recorta
        // (overflow-x diferente de visible). Um controle além desse limite é inalcançável.
        let lim = {left: 0, right: vw};
        for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
            if (getComputedStyle(p).overflowX !== 'visible') { const pr = p.getBoundingClientRect(); lim = {left: pr.left, right: pr.right, rola: p.scrollWidth > p.clientWidth + 1}; break; }
        }
        const controle = el.matches('a, button, input, select, textarea');
        if (b.left < lim.left - 1 && (controle || lim.right === vw)) fora.push(desc(el) + ' x=' + Math.round(b.left));
        else if (b.right > lim.right + 1 && !lim.rola && (controle || lim.right === vw)) fora.push(desc(el) + ' x1=' + Math.round(b.right));
    }
    const graficos = [...document.querySelectorAll('.chart-box')].map(c => {
        const cr = rect(c);
        const ticks = [...c.querySelectorAll('.xtick text, .ytick text')].map(t => ({texto: t.textContent, b: rect(t),
            eixo: t.closest('.ytick') ? 'y' : 'x', girado: /rotate/.test(t.getAttribute('transform') || '')}));
        const cortados = ticks.filter(t => t.b.x0 < cr.x0 - 1 || t.b.x1 > cr.x1 + 1 || t.b.y1 > cr.y1 + 1).map(t => t.texto);
        // Eixo X: rótulos na mesma linha não podem se interceptar (rótulos girados têm
        // caixas que se cruzam sem os glifos se tocarem, por isso ficam de fora).
        const xt = ticks.filter(t => t.texto && t.eixo === 'x' && !t.girado).sort((a, b) => a.b.x0 - b.b.x0);
        let sobrepostos = 0;
        for (let i = 1; i < xt.length; i++) if (xt[i].b.x0 < xt[i - 1].b.x1 - 1 && Math.abs(xt[i].b.y0 - xt[i - 1].b.y0) < 2 && Math.abs(xt[i].b.x0 - xt[i-1].b.x0) > 0) sobrepostos++;
        // Eixo Y (nomes de 3–4 linhas empilhados): um rótulo começa abaixo de onde o anterior termina.
        const yt = ticks.filter(t => t.texto && t.eixo === 'y').sort((a, b) => a.b.y0 - b.b.y0);
        for (let i = 1; i < yt.length; i++) if (yt[i].b.y0 < yt[i - 1].b.y1 - 0.5) sobrepostos++;
        const svg = c.querySelector('.main-svg');
        const vaza = svg ? svg.getBoundingClientRect().bottom > cr.y1 + 2 : false;
        return {id: c.id, w: Math.round(cr.w), desenhado: !!c.querySelector('.main-svg'), truncados: ticks.filter(t => /…/.test(t.texto)).length,
                porLinha: parseInt(c.getAttribute('data-por-linha') || '0', 10), vaza,
                largo: !!(c._fullLayout && c._fullLayout.xaxis2),
                cortados: cortados.slice(0, 5), sobrepostos, papel: c.getAttribute('role'), rotulo: (c.getAttribute('aria-label') || '').length,
                barras: c.querySelectorAll('.barlayer .point').length};
    });
    return {rolagem_x: document.documentElement.scrollWidth > vw + 1, tabelas, kpis, nexobot, sidebar, fora: fora.slice(0, 10), graficos};
}"""


@unittest.skipUnless(_BROWSER, "Defina NEXO_TEST_BROWSER=1 e instale o Playwright para os testes de navegador.")
class NavegadorCase(ContratosCase):
    def setUp(self):
        super().setUp()
        self.dados_extensos()
        from werkzeug.serving import make_server
        for alvo, original in _CONECTAR_LOCAL.items():
            self.stack.enter_context(patch(alvo, original))
        self.servidor = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.base = f"http://127.0.0.1:{self.servidor.server_port}"
        _liberar_origem_socketio(self, self.base)
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.addCleanup(self._parar_servidor)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.navegador = self.pw.chromium.launch(executable_path=os.environ.get("NEXO_TEST_CHROMIUM") or None)
        self.addCleanup(self.navegador.close)
        self.cdn_local = _copias_locais_da_cdn()
        self.plotly_local = _plotly_local()
        self.erros_js = []
        if len(self.cdn_local) < len(_CDN_INTEGRO) or not self.plotly_local:
            # Medir layout sem o bootstrap.min.css real (grade, utilitários, modais) ou
            # sem os gráficos desenhados não diz nada sobre o produto; a CI baixa as
            # cópias e confere o hash SRI. A omissão fica registrada como "skipped".
            self.skipTest("Bootstrap 5.3.3 e Plotly 2.35.2 íntegros indisponíveis (NEXO_TEST_CDN_DIR)")

    def _parar_servidor(self):
        self.servidor.shutdown()
        self.servidor.server_close()

    def tearDown(self):
        self.assertEqual(self.erros_js, [], "Erros de JavaScript no console")

    # ------------------------------------------------------------ dados
    def dados_extensos(self):
        """Nomes, e-mails e produtos longos; faturamento de nove dígitos; uma empresa
        na lixeira; dois chamados; duas análises publicadas (histórico com 2 pontos)."""
        from models import (Analise, ChamadoSuporte, Empresa, MensagemSuporte, Plano, ProdutoCurvaABC, Segmento,
                            UploadRelatorio, ValidacaoPdv)
        s = self.db.session
        plano = s.scalar(self.db.select(Plano))
        segmento = Segmento(nome_segmento="MATERIAIS_DE_CONSTRUCAO_E_ACABAMENTOS")
        s.add(segmento)
        s.flush()
        longa = Empresa(id_segmento=segmento.id_segmento, id_plano_atual=plano.id_plano, cnpj="99000000000199",
                        razao_social=NOME_LONGO, nome_fantasia=FANTASIA_LONGA, email_contato=EMAIL_LONGO,
                        data_contratacao=date(2026, 1, 1))
        lixo = Empresa(id_segmento=segmento.id_segmento, id_plano_atual=plano.id_plano, cnpj="99000000000198",
                       razao_social="Empresa fictícia removida com razão social bastante extensa para testes Ltda ME",
                       nome_fantasia="Removida Demo", email_contato="removida@example.invalid",
                       data_contratacao=date(2026, 1, 1), deletado_em=datetime(2026, 2, 20, 15, 30))
        s.add_all([longa, lixo])
        s.flush()
        self.contrato_longo_id = self.contratar(empresa_id=longa.id_empresa).id_contrato
        # Referência sem espaços (140 caracteres): no modo cartões ela precisa quebrar
        # dentro do cartão, não vazar dele.
        self.pagar(date(2026, 1, 1), date(2026, 1, 31), empresa_id=longa.id_empresa,
                   referencia="Comprovante-fictício-" + "X" * 120)
        # Análise do cliente: indicadores grandes, devolutiva e Curva ABC com nomes longos.
        self.publicar(self.analise_id, inicio=date(2026, 1, 1), fim=date(2026, 1, 31))
        anterior = self.nova_analise(date(2025, 12, 1), date(2025, 12, 31))
        self.publicar(anterior.id_analise, inicio=date(2025, 12, 1), fim=date(2025, 12, 31))
        analise = s.get(Analise, self.analise_id)
        ind = analise.indicador
        ind.faturamento_total = Decimal("123456789.12")
        ind.total_comprado = Decimal("98765432.10")
        ind.saldo_estimado_compras_vendas = Decimal("-24691357.02")
        ind.produto_mais_vendido_nome = PRODUTOS_LONGOS[0]
        ind.produto_mais_vendido_quantidade = Decimal("1234567")
        ind.produto_maior_faturamento_nome = PRODUTOS_LONGOS[1]
        ind.produto_maior_faturamento_valor = Decimal("12345678.90")
        ind.produto_maior_saldo_parado_nome = PRODUTOS_LONGOS[2]
        total = Decimal("123456789.12")
        acumulado = Decimal("0")
        for pos, nome in enumerate(PRODUTOS_LONGOS, 1):
            fat = (total * Decimal(11 - pos) / Decimal(55)).quantize(Decimal("0.01"))
            perc = (fat / total * 100).quantize(Decimal("0.01"))
            acumulado += perc
            s.add(ProdutoCurvaABC(id_analise=analise.id_analise, posicao_ranking=pos, produto_nome=nome,
                                  quantidade=Decimal(1000 * (11 - pos)), faturamento=fat,
                                  percentual_individual=perc, percentual_acumulado=min(acumulado, Decimal("100")),
                                  classe_abc="A" if acumulado <= 80 else ("B" if acumulado <= 95 else "C")))
        # Seis meses publicados antes dos dois já existentes: o histórico comparativo
        # fica com oito períodos (no celular os rótulos do eixo precisam ser espaçados).
        for mes in range(6, 12):
            fim = date(2025, mes + 1, 1) - timedelta(days=1)
            self.publicar(self.nova_analise(date(2025, mes, 1), fim).id_analise, inicio=date(2025, mes, 1), fim=fim)
        # A análise com a Curva ABC de nomes longos é a mais recente. As demais foram
        # publicadas com a mesma data_conclusao, e o desempate por data_criacao
        # (CURRENT_TIMESTAMP, 1 s no SQLite) dependia de o setUp cruzar a virada de um segundo.
        analise.data_conclusao = datetime(2026, 2, 5, 13)
        # Período em aberto: a página "Enviar relatórios" mostra as dropzones; o
        # relatório de vendas já foi recebido (dropzone no estado "Arquivo recebido").
        aberta = self.nova_analise(date(2026, 2, 1), date(2026, 2, 28))
        s.add(UploadRelatorio(id_analise=aberta.id_analise, id_usuario_admin=None, tipo_relatorio="VENDAS",
                              nome_arquivo_original="vendas-fevereiro-2026-loja-matriz-e-filiais-consolidado.csv",
                              caminho_arquivo="/tmp/inexistente.csv", extensao_arquivo="CSV", tamanho_arquivo=12,
                              hash_arquivo="0" * 64, status_processamento="PENDENTE"))
        # A análise em aberto diverge do PDV: o card, com valores de nove dígitos, entra na medição.
        s.add(ValidacaoPdv(id_analise=aberta.id_analise, valor_pdv_informado=Decimal("123456789.12"),
                           faturamento_bruto_calculado=Decimal("987654321.98"), diferenca=Decimal("864197532.86"),
                           valor_devolucoes=Decimal("1234567.89"), valor_cancelado=Decimal("7654321.00"),
                           status_validacao="DIVERGENTE_PDV", id_usuario_informante=self.cliente_id,
                           informado_em=datetime(2026, 2, 10, 13, 0, tzinfo=timezone.utc),
                           validado_em=datetime(2026, 2, 11, 13, 0, tzinfo=timezone.utc)))
        for i, (assunto, status) in enumerate([
                ("Erro ao enviar planilha de compras em formato XLSX com várias abas e colunas mescladas", "ABERTO"),
                ("Dúvida sobre o indicador de pressão de estoque do último período", "RESOLVIDO")]):
            chamado = ChamadoSuporte(id_empresa=self.empresa_id, id_usuario_cliente=self.cliente_id, assunto=assunto,
                                     categoria="ERRO_INTEGRACAO" if i == 0 else "DUVIDA_TECNICA", status_chamado=status,
                                     prioridade="MEDIA",
                                     data_abertura=datetime(2026, 2, 10 + i, 14, 20, tzinfo=timezone.utc),
                                     data_atualizacao=datetime(2026, 2, 11 + i, 9, 5, tzinfo=timezone.utc))
            s.add(chamado)
            s.flush()
            s.add(MensagemSuporte(id_chamado=chamado.id_chamado, id_usuario_remetente=self.cliente_id,
                                  mensagem="Mensagem fictícia.",
                                  data_envio=datetime(2026, 2, 10 + i, 14, 20, tzinfo=timezone.utc)))
        s.commit()
        self.empresa_longa_id = longa.id_empresa
        self._raio_x_extenso()
        self._lead_extenso()

    def _raio_x_extenso(self):
        """Raio-X do PDV com nomes longos e totais de nove dígitos (o mesmo teto dos cards do
        painel), em oito meses: o cliente BRONZE vê as colunas de custo desfocadas e o aviso
        dos meses fora do histórico."""
        import tempfile
        import raio_x
        import raio_x_servico
        from tests.raio_x_support import executar_como_o_worker, relatorio_do_mes
        with tempfile.TemporaryDirectory(prefix="raio-x-navegador-") as pasta:
            caminhos = []
            for mes in range(5, 13):
                linhas = [f"{i};{nome};1500;1234,56;{1_800_000 + 100 * i + mes},00;1900000,00"
                          for i, nome in enumerate(PRODUTOS_LONGOS, 1)]
                caminho = Path(pasta) / f"arquivo_{mes:02d}.csv"
                caminho.write_bytes(relatorio_do_mes(2025, mes, linhas))
                caminhos.append(str(caminho))
            resultado = executar_como_o_worker(caminhos)
        raio_x_servico.salvar(self.empresa_id, self.cliente_id, resultado, raio_x.recomendar(resultado, 24),
                              [f"vendas-por-produto-loja-matriz-{mes:02d}-2025.csv" for mes in range(5, 13)])
        self.db.session.commit()

    def _lead_extenso(self):
        """Avaliação de Cenário com nomes e categorias longos e valores de nove dígitos, em dois anos,
        para as telas do consultor: a lista e o detalhe com o resumo executivo do caixa resgatável, a
        matriz de triagem, a matriz por categoria, o estoque parado e excedente, o simulador, a
        auditoria de preço, os blocos da triagem e as tabelas de itens (estoque de nove dígitos).
        Novembro e dezembro dos dois anos (mês em comum: a queda vale); os produtos de número múltiplo
        de 3 param de vender em 2025, então há Urgência com estoque (simulador) e estoque parado."""
        from calendar import monthrange
        import tempfile
        import avaliacao_cenario
        from tests.raio_x_support import avaliar_como_o_worker
        with tempfile.TemporaryDirectory(prefix="lead-navegador-") as pasta:
            caminhos = []
            for ano, mes in ((2024, 11), (2024, 12), (2025, 11), (2025, 12)):
                linhas = []
                for i, nome in enumerate(PRODUTOS_LONGOS, 1):
                    if ano == 2025 and i % 3 == 0:
                        continue  # parou de vender: estoque parado e, com o recuperável, Urgência
                    queda = ano == 2025 and i % 2 == 1  # venda média cai 80% de um ano para o outro
                    quantidade, valor = (300, 360_000) if queda else (1500, 1_800_000 + 100 * i)
                    custo = "" if i == 2 else ("1900000,00" if i % 2 == 1 else "1000000,00")
                    categoria = CATEGORIAS_LONGAS[i % len(CATEGORIAS_LONGAS)]
                    estoque = 123_456_789 + i
                    linhas.append(f"{i};{nome};{categoria};{quantidade};1234,56;{valor},00;{custo};{estoque}")
                fim = monthrange(ano, mes)[1]
                texto = (f"Relatorio de vendas por produto\nPeriodo: 01/{mes:02d}/{ano} a {fim:02d}/{mes:02d}/{ano}\n"
                         "Codigo;Produto;Categoria;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque\n"
                         + "".join(linha + "\n" for linha in linhas))
                caminho = Path(pasta) / f"arquivo_{ano}_{mes:02d}.csv"
                caminho.write_bytes(texto.encode("utf-8"))
                caminhos.append(str(caminho))
            resultado = avaliar_como_o_worker(caminhos)
        lead = avaliacao_cenario.registrar(resultado, contato={
            "nome_contato": "Maria Aparecida dos Santos Oliveira Pereira da Silva Albuquerque",
            "email": "maria.aparecida.santos.oliveira@supermercado-ficticio-irmaos-albuquerque.example.invalid",
            "whatsapp": "5511912345678",
            "nome_loja": "Supermercado Fictício Irmãos Albuquerque & Filhos Comércio de Alimentos Ltda – Filial Centro",
        }, registrado_por=self.admin_id, meses_prata=24)
        self.db.session.commit()
        self.lead_id = lead.id

    # ------------------------------------------------------------ navegador
    def _rota(self, rota):
        requisicao = rota.request
        url = requisicao.url
        if url.startswith(self.base + "/"):
            rota.continue_()
        elif url in self.cdn_local:
            caminho, tipo = self.cdn_local[url]
            rota.fulfill(status=200, path=caminho, content_type=tipo)
        elif url == _PLOTLY_URL and self.plotly_local:
            rota.fulfill(status=200, path=self.plotly_local, content_type="application/javascript")
        elif requisicao.resource_type == "font":
            _servir_fonte(rota, url)
        elif url == _CDN + "bootstrap@5.3.3/dist/css/bootstrap.min.css":
            rota.fulfill(status=200, content_type="text/css", body=_BOOTSTRAP_MINIMO)
        elif requisicao.resource_type == "stylesheet":
            rota.fulfill(status=200, content_type="text/css", body="")
        elif requisicao.resource_type == "script":
            rota.fulfill(status=200, content_type="application/javascript", body="")
        else:
            rota.abort()

    def abrir(self, largura, altura, escala=1):
        contexto = self.navegador.new_context(viewport={"width": largura, "height": altura}, device_scale_factor=escala,
                                              reduced_motion="reduce", locale="pt-BR", timezone_id="America/Sao_Paulo")
        self.addCleanup(contexto.close)
        pagina = contexto.new_page()
        pagina.on("pageerror", lambda erro: self.erros_js.append(f"{largura}px: {erro}"))
        pagina.on("console", lambda msg: self.erros_js.append(f"{largura}px console: {msg.text}")
                  if msg.type == "error" and "404 (NOT FOUND)" not in msg.text else None)
        pagina.route("**/*", self._rota)
        return pagina

    def entrar(self, pagina, email):
        pagina.goto(self.base + "/auth/login", wait_until="load")
        pagina.fill("#email", email)
        pagina.fill("#senha", self.password)
        with pagina.expect_navigation(wait_until="load"):
            pagina.click("button[type=submit]")

    def ir(self, pagina, caminho, status=200):
        resposta = pagina.goto(self.base + caminho, wait_until="load")
        self.assertEqual(resposta.status, status, caminho)
        pagina.wait_for_timeout(150)  # o ajuste de layout do shell roda após as fontes
        return resposta

    def medir(self, pagina):
        return pagina.evaluate(_MEDIR)

    # ------------------------------------------------------------ asserções
    def assert_layout(self, pagina, largura, contexto):
        m = self.medir(pagina)
        self.assertFalse(m["rolagem_x"], f"{contexto}: a página rola na horizontal")
        self.assertEqual(m["fora"], [], f"{contexto}: elementos fora da tela")
        for t in m["tabelas"]:
            self.assertEqual(t["encobertas"], 0, f"{contexto}: célula encoberta por célula fixa ({t})")
            self.assertFalse(t["dicaEsmaecida"], f"{contexto}: dica de rolagem esmaecida pela máscara ({t})")
            self.assertEqual(t["fixas"], 0, f"{contexto}: ainda há célula fixa ({t})")
            self.assertTrue(t["dentroDaTela"], f"{contexto}: tabela fora da tela ({t})")
            if t["rola"] and not t["cartoes"]:
                self.assertTrue(t["focavel"], f"{contexto}: tabela que rola sem foco pelo teclado ({t})")
                self.assertTrue(t["ultimaAlcancavel"], f"{contexto}: última coluna inalcançável ({t})")
            if t["cartoes"]:
                self.assertEqual(t["semRotulo"], 0, f"{contexto}: valor sem rótulo nos cartões ({t})")
                self.assertEqual(t["botoesForaDoCartao"], 0, f"{contexto}: botão fora do cartão ({t})")
        for k in m["kpis"]:
            self.assertFalse(k["estoura"], f"{contexto}: KPI estourando ({k})")
            self.assertGreaterEqual(k["fonte"], 18, f"{contexto}: KPI ilegível ({k})")
        if m["nexobot"]:
            self.assertTrue(m["nexobot"]["dentroDaTela"], f"{contexto}: NexoBot fora da tela")
        if m["sidebar"] and m["sidebar"]["folga"] is not None:
            self.assertGreaterEqual(m["sidebar"]["folga"], 6, f"{contexto}: indicador da sidebar no rótulo ({m['sidebar']})")
        # Nomes de produto cabem em até 3 linhas (de ~18 caracteres no celular, ~26 no
        # computador, quebradas nas palavras como faz NEXO.quebrarRotulo); só os que
        # precisariam de mais linhas podem terminar em reticências, e o nome completo
        # fica na tooltip, na descrição acessível e na tabela ABC.
        for g in m["graficos"]:
            if not g["desenhado"]:
                continue
            # O gráfico decide os caracteres por linha pela própria largura e pela fonte
            # efetiva (data-por-linha); sem o atributo, vale o teto do modo (< 560 px = estreito).
            por_linha = g["porLinha"] or (18 if g["w"] < 560 else 26)
            truncaveis = sum(1 for nome in PRODUTOS_LONGOS if _linhas_necessarias(nome, por_linha) > 3)
            permitidos = truncaveis if "Abc" in g["id"] else 0
            self.assertLessEqual(g["truncados"], permitidos, f"{contexto}: rótulo truncado em {g['id']}")
            if "Abc" in g["id"]:
                # Uma barra por produto, mesmo quando dois nomes encurtados coincidem.
                self.assertEqual(g["barras"], len(PRODUTOS_LONGOS), f"{contexto}: barras fundidas em {g['id']}")
            self.assertEqual(g["cortados"], [], f"{contexto}: rótulo cortado em {g['id']}")
            self.assertEqual(g["sobrepostos"], 0, f"{contexto}: rótulos sobrepostos em {g['id']}")
            self.assertFalse(g["vaza"], f"{contexto}: SVG do gráfico mais alto que a caixa em {g['id']}")
            if "Abc" in g["id"]:
                self.assertEqual(g["largo"], g["w"] >= 560, f"{contexto}: Pareto no modo errado para {g['w']}px ({g['id']})")
            self.assertEqual(g["papel"], "img", f"{contexto}: gráfico sem alternativa textual ({g['id']})")
            self.assertGreater(g["rotulo"], 20, f"{contexto}: descrição vazia em {g['id']}")
        return m

    def assert_fim_da_pagina(self, pagina, contexto):
        """No fim da página o NexoBot não cobre nada (há espaço reservado); no meio da
        página o que fica sob o botão sai de baixo dele com a rolagem, como em qualquer
        botão flutuante, então a medição do topo só vale para páginas que não rolam."""
        rola = pagina.evaluate("document.documentElement.scrollHeight > innerHeight + 1")
        if not rola:
            m = self.medir(pagina)
            if m["nexobot"]:
                self.assertEqual(m["nexobot"]["sobre"], [], f"{contexto} (página sem rolagem): NexoBot sobre controle/título")
        pagina.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        pagina.wait_for_timeout(100)
        m = self.medir(pagina)
        if m["nexobot"]:
            self.assertEqual(m["nexobot"]["sobre"], [], f"{contexto} (fim da página): NexoBot sobre controle/título")
        pagina.evaluate("window.scrollTo(0, 0)")

    # ------------------------------------------------------------ cenários
    def paginas_admin(self):
        base = f"/admin/empresas/{self.empresa_longa_id}"
        return ["/admin/dashboard", "/admin/empresas", "/admin/analises", "/admin/lixeira", "/admin/contratos",
                "/admin/tickets", "/admin/analises/gerenciar", f"/admin/analises/gerenciar/{self.empresa_longa_id}",
                f"{base}/detalhes",
                # Formulários com linha de botões "Voltar / Confirmar": os dois precisam caber.
                f"{base}/contratos/{self.contrato_longo_id}/prorrogar", f"{base}/contratos/{self.contrato_longo_id}/cobertura",
                f"{base}/regras-contratuais/ativar",
                # Avaliação de Cenário: formulário de envio, lista e detalhe com as tabelas da triagem.
                "/admin/leads", f"/admin/leads/{self.lead_id}",
                # Com o markup do segmento, a auditoria de preço lista produtos abaixo da referência.
                f"/admin/leads/{self.lead_id}?markup_informado=0.9"]

    def paginas_cliente(self):
        return ["/cliente/dashboard", f"/cliente/analise/{self.analise_id}", "/cliente/historico", "/cliente/upload",
                "/cliente/tickets", "/cliente/guia", "/onboarding/diagnostico", "/onboarding/planos"]



class OverflowNavegadorTests(NavegadorCase):
    """Cenários de overflow, responsividade e acessibilidade (ver docstring do módulo)."""

    def test_admin_sem_cortes_em_todas_as_larguras(self):
        for largura, altura in LARGURAS:
            pagina = self.abrir(largura, altura)
            self.entrar(pagina, "admin-test@example.invalid")
            for caminho in self.paginas_admin():
                self.ir(pagina, caminho)
                self.assert_layout(pagina, largura, f"{caminho} @ {largura}px")
            pagina.context.close()

    def test_lead_extenso_desenha_as_tabelas_da_inteligencia(self):
        # O assert_layout só mede as tabelas que existem: aqui se confere que as mais largas existem.
        pagina = self.abrir(1280, 800)
        self.entrar(pagina, "admin-test@example.invalid")
        self.ir(pagina, f"/admin/leads/{self.lead_id}?markup_informado=0.9")
        html = pagina.content()
        # O base.html acrescenta role="columnheader" a todo th: o cabeçalho é procurado pelo localizador.
        for texto in ("Resumo executivo: caixa resgatável", "Matriz de triagem", "Maiores capitais parados",
                      "Maiores diferenças por produto", "123.456.79"):
            self.assertIn(texto, html)
        self.assertGreaterEqual(pagina.locator("thead th", has_text="Desconto").count(), 1)
        # O simulador com produto (não só os quatro cenários de R$ 0,00).
        import re
        itens = re.search(r"([\d.]+) produto\(s\) da Urgência com estoque", html)
        self.assertIsNotNone(itens)
        self.assertGreater(int(itens.group(1).replace(".", "")), 0)
        pagina.context.close()

    def test_cliente_sem_cortes_em_todas_as_larguras(self):
        for largura, altura in LARGURAS:
            pagina = self.abrir(largura, altura)
            self.entrar(pagina, "cliente-test@example.invalid")
            for caminho in self.paginas_cliente():
                self.ir(pagina, caminho)
                self.assert_layout(pagina, largura, f"{caminho} @ {largura}px")
                self.assert_fim_da_pagina(pagina, f"{caminho} @ {largura}px")
            pagina.context.close()

    def test_zoom_150_por_cento(self):
        largura, altura, escala = ZOOM_150
        pagina = self.abrir(largura, altura, escala)
        self.entrar(pagina, "admin-test@example.invalid")
        for caminho in ("/admin/analises", "/admin/lixeira", "/admin/empresas"):
            self.ir(pagina, caminho)
            self.assert_layout(pagina, largura, f"{caminho} @ zoom 150%")

    def test_tabela_de_analises_mostra_todas_as_colunas(self):
        """A 1366 px (notebook) e 1920 px: as nove colunas de Análises existem, nenhuma
        fica encoberta e, se a tabela rolar, a última coluna chega à vista pelo teclado."""
        for largura, altura in ((1366, 768), (1920, 1080)):
            pagina = self.abrir(largura, altura)
            self.entrar(pagina, "admin-test@example.invalid")
            self.ir(pagina, "/admin/analises")
            m = self.assert_layout(pagina, largura, f"/admin/analises @ {largura}px")
            tabela = m["tabelas"][0]
            self.assertEqual(tabela["colunas"], 9, tabela)
            rotulos = pagina.locator("#tabelaAnalises thead th").all_text_contents()
            self.assertEqual([r.strip() for r in rotulos],
                             ["Empresa", "Período", "Tipo", "Quinzena", "Status", "Uploads", "Criação", "Publicado", "Ações"])
            if tabela["rola"] and not tabela["cartoes"]:
                caixa = pagina.locator(".table-responsive-x").first
                caixa.focus()
                for _ in range(40):
                    pagina.keyboard.press("ArrowRight")
                visivel = pagina.evaluate("""() => { const c = document.querySelector('.table-responsive-x');
                    const th = c.querySelector('thead th:last-child').getBoundingClientRect(); const cr = c.getBoundingClientRect();
                    return th.right <= cr.right + 1 && th.left >= cr.left - 1; }""")
                self.assertTrue(visivel, "a coluna Ações não chegou à vista com as setas")
            # O botão "Abrir" da primeira linha é alcançado pelo Tab e abre a análise.
            pagina.locator("#tabelaAnalises tbody tr").first.locator("a.btn-ghost").first.focus()
            self.assertEqual(pagina.evaluate("document.activeElement.textContent.trim()"), "Abrir")
            pagina.context.close()

    def test_lixeira_data_de_remocao_legivel_e_acoes_separadas(self):
        for largura, altura in ((1366, 768), (390, 844)):
            pagina = self.abrir(largura, altura)
            self.entrar(pagina, "admin-test@example.invalid")
            self.ir(pagina, "/admin/lixeira")
            self.assert_layout(pagina, largura, f"/admin/lixeira @ {largura}px")
            celula = pagina.locator(".nexo-table tbody tr").first.locator("td").nth(5)
            self.assertIn("20/02/2026 15:30", celula.text_content())
            caixa = celula.evaluate("td => { const r = td.getBoundingClientRect(); const c = td.closest('.table-responsive-x').getBoundingClientRect(); return [r.left, r.right, c.left, c.right, c.scrollWidth]; }")
            # A data cabe por inteiro na área visível ou é alcançável pela rolagem (nunca encoberta).
            self.assertGreaterEqual(caixa[1] - caixa[0], 100, caixa)
            botoes = pagina.locator(".nexo-table tbody tr").first.locator(".acoes button")
            self.assertEqual(botoes.count(), 2)
            r1, r2 = (botoes.nth(i).evaluate("b => { const r = b.getBoundingClientRect(); return [r.left, r.right, r.top, r.bottom]; }") for i in (0, 1))
            separados = r1[1] + 4 <= r2[0] or r2[1] + 4 <= r1[0] or r1[3] + 4 <= r2[2] or r2[3] + 4 <= r1[2]
            self.assertTrue(separados, (r1, r2))
            botoes.nth(1).click()
            pagina.wait_for_selector("#confirmPurgeModal.show")
            self.assertIn("Removida Demo", pagina.locator("#confirmPurgeNome").text_content())
            pagina.keyboard.press("Escape")
            pagina.wait_for_selector("#confirmPurgeModal", state="hidden")
            pagina.context.close()

    def test_empresas_filtro_por_empresa_vindo_da_lista(self):
        pagina = self.abrir(1440, 900)
        self.entrar(pagina, "admin-test@example.invalid")
        self.ir(pagina, f"/admin/analises?empresa={self.empresa_id}")
        self.assertEqual(pagina.locator("#filtroEmpresa").input_value(), str(self.empresa_id))
        visiveis = pagina.evaluate("[...document.querySelectorAll('#tabelaAnalises tbody tr[data-empresa]')].filter(r => getComputedStyle(r).display !== 'none').map(r => r.dataset.empresa)")
        self.assertTrue(visiveis and all(v == str(self.empresa_id) for v in visiveis), visiveis)
        self.ir(pagina, "/admin/empresas")
        href = pagina.locator("#tabelaEmpresas tbody tr").first.locator("a", has_text="Análises").get_attribute("href")
        self.assertIn("/admin/analises?empresa=", href)
        # Busca: filtra pelos dados, não pelos rótulos dos botões; "Limpar busca" volta ao normal.
        pagina.fill("#buscaEmpresa", "análises")
        self.assertTrue(pagina.locator("#semResultados").is_visible())
        pagina.click("#limparBusca")
        self.assertFalse(pagina.locator("#semResultados").is_visible())
        self.assertEqual(pagina.locator("#buscaEmpresa").input_value(), "")

    def test_sidebar_item_longo_nao_encosta_no_indicador(self):
        """O rótulo mais largo de cada menu (admin: "Avaliação de Cenário", desde 11/2026; cliente:
        "Enviar relatórios") nunca encosta no ponto do item ativo."""
        for email, caminho, rotulo in (("admin-test@example.invalid", "/admin/leads", "Avaliação de Cenário"),
                                       ("cliente-test@example.invalid", "/cliente/upload", "Enviar relatórios")):
            pagina = self.abrir(1440, 900)
            self.entrar(pagina, email)
            self.ir(pagina, caminho)
            if email.startswith("admin"):
                # Pré-condição: a premissa "Avaliação de Cenário é o rótulo mais largo" só vale com estes itens.
                self.assertEqual([r.strip() for r in pagina.locator(".sidebar-nav .nav-item").all_text_contents()],
                                 ["Dashboard", "Empresas", "Contratos", "Lixeira", "Análises", "Avaliação de Cenário",
                                  "Suporte", "Configurações"])
            m = self.medir(pagina)
            self.assertEqual(m["sidebar"]["rotulo"], rotulo, m["sidebar"])
            self.assertTrue(m["sidebar"]["fluxo"], "o indicador deve ser um item do flex, não absoluto")
            self.assertLessEqual(m["sidebar"]["texto_direita"] + 6 + 12, m["sidebar"]["item_direita"] + 1, m["sidebar"])
            pagina.context.close()

    def test_menu_administrativo_sem_base_de_conhecimento(self):
        pagina = self.abrir(1366, 768)
        self.entrar(pagina, "admin-test@example.invalid")
        self.ir(pagina, "/admin/dashboard")
        rotulos = pagina.locator(".sidebar-nav .nav-item").all_text_contents()
        rotulos = [r.strip() for r in rotulos]
        self.assertNotIn("Base de Conhecimento", rotulos, rotulos)
        self.assertEqual(rotulos, ["Dashboard", "Empresas", "Contratos", "Lixeira", "Análises", "Avaliação de Cenário",
                                   "Suporte", "Configurações"])

    def test_upload_acessivel_pelo_teclado_e_sem_envio_vazio(self):
        pagina = self.abrir(1366, 768)
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, "/cliente/upload")
        campos = pagina.locator("input[type=file][data-dz-input]")
        self.assertGreaterEqual(campos.count(), 2)
        # Tab a partir do início do conteúdo chega ao campo de arquivo em poucas paradas.
        pagina.focus("#conteudo")
        alcancou = False
        for _ in range(12):
            pagina.keyboard.press("Tab")
            if pagina.evaluate("document.activeElement.matches('input[type=file][data-dz-input]')"):
                alcancou = True
                break
        self.assertTrue(alcancou, "o campo de arquivo não entrou na ordem de Tab")
        foco_visivel = pagina.evaluate("""() => { const dz = document.activeElement.closest('.dropzone');
            const cs = getComputedStyle(dz); return cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 2; }""")
        self.assertTrue(foco_visivel, "o foco no campo de arquivo não aparece na dropzone")
        # O nome acessível é formado pelos textos visíveis (título do slot, estado e nome
        # do arquivo), não por um aria-label que os substitua (WCAG 2.5.3).
        nome = pagina.evaluate("""() => { const el = document.activeElement;
            const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
            const texto = ids.map(id => (document.getElementById(id) || {}).textContent || '').join(' ');
            const dz = el.closest('.dropzone');
            return {nome: texto.replace(/\\s+/g, ' ').trim(), rotulo: dz.querySelector('.dz-title').textContent.trim(),
                    estado: dz.querySelector('.dz-estado').textContent.trim(),
                    slot: dz.closest('.upload-slot').querySelector('.upload-slot-head .title').textContent.trim(),
                    ariaLabel: el.getAttribute('aria-label')}; }""")
        self.assertIsNone(nome["ariaLabel"])
        self.assertIn(nome["slot"], nome["nome"])
        self.assertIn(nome["rotulo"], nome["nome"])
        if nome["estado"]:
            self.assertIn(nome["estado"], nome["nome"])

    def test_pareto_acompanha_o_redimensionamento(self):
        """Girar o celular ou redimensionar a janela: a caixa do gráfico mantém a altura
        do SVG (nada vaza sobre o conteúdo abaixo) e o Pareto troca de modo
        (estreito ↔ largo) conforme a largura da caixa, sem rótulos sobrepostos."""
        pagina = self.abrir(1366, 768)
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, "/cliente/dashboard")
        for largura, altura in ((360, 740), (1366, 768), (844, 390), (390, 844)):
            pagina.set_viewport_size({"width": largura, "height": altura})
            pagina.wait_for_timeout(700)
            m = self.assert_layout(pagina, largura, f"/cliente/dashboard redimensionado @ {largura}px")
            pareto = [g for g in m["graficos"] if g["id"] == "chartAbcCliente"]
            self.assertTrue(pareto and pareto[0]["desenhado"], f"{largura}px: Pareto não desenhado")

    def test_filtro_abc_como_botoes_de_alternancia(self):
        """Os filtros da Curva ABC são botões de alternância (aria-pressed) num grupo
        nomeado, não "abas" sem o comportamento de abas; funcionam por teclado."""
        pagina = self.abrir(1366, 768)
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, f"/cliente/analise/{self.analise_id}")
        grupo = pagina.locator("#abcFilter")
        self.assertEqual(grupo.get_attribute("role"), "group")
        self.assertTrue(grupo.get_attribute("aria-label"))
        self.assertEqual(pagina.locator('#abcFilter [role="tab"], #abcFilter [aria-selected]').count(), 0)
        todos = pagina.locator("#abcFilter .abc-pill").first
        self.assertEqual(todos.get_attribute("aria-pressed"), "true")
        classe_a = pagina.locator('#abcFilter .abc-pill[data-abc-filter="A"]')
        classe_a.focus()
        pagina.keyboard.press("Enter")
        self.assertEqual(classe_a.get_attribute("aria-pressed"), "true")
        self.assertEqual(todos.get_attribute("aria-pressed"), "false")
        visiveis = pagina.evaluate("""() => [...document.querySelectorAll('#abcTable tbody tr[data-classe]')]
            .filter(tr => getComputedStyle(tr).display !== 'none').map(tr => tr.getAttribute('data-classe'))""")
        self.assertTrue(visiveis)
        self.assertEqual(set(visiveis), {"A"})

    def test_upload_sem_arquivo_nao_faz_post(self):
        pagina = self.abrir(1366, 768)
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, "/cliente/upload")
        posts = []
        pagina.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
        # O total do PDV é obrigatório: vazio, o navegador barra o envio antes da dropzone.
        pagina.locator("input[name=valor_pdv_informado]").first.fill("150.000,00")
        pagina.locator("form:has(input[type=file]) button[type=submit]").first.click()
        pagina.wait_for_timeout(300)
        self.assertEqual(posts, [])
        erro = pagina.locator(".dropzone.is-erro .dz-erro").first
        self.assertTrue(erro.is_visible())
        self.assertIn("Selecione um arquivo", erro.text_content())
        # Arquivo inválido é recusado antes do envio; arquivo válido aparece como selecionado.
        campo = pagina.locator("input[type=file][data-dz-input]").first
        campo.set_input_files({"name": "relatorio.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4"})
        self.assertIn("Formato não aceito", pagina.locator(".dropzone .dz-erro").first.text_content())
        campo.set_input_files({"name": "vendas-janeiro.csv", "mimeType": "text/csv", "buffer": b"produto;qtd\n"})
        self.assertIn("vendas-janeiro.csv", pagina.locator(".dropzone .dz-title").first.text_content())
        self.assertIn("Selecionado para envio", pagina.locator(".dropzone .dz-estado").first.text_content())
        # O navegador limpa o campo (reabrir o seletor e cancelar): a dropzone volta ao
        # estado inicial em vez de continuar afirmando que há um arquivo selecionado.
        campo.set_input_files([])
        self.assertNotIn("Selecionado", pagina.locator(".dropzone .dz-estado").first.text_content())
        self.assertFalse(pagina.locator(".dropzone").first.evaluate("e => e.classList.contains('is-selecionado')"))

    def test_upload_ja_recebido_volta_ao_estado_recebido(self):
        """Slot com arquivo recebido: escolher um arquivo inválido ou cancelar a seleção
        devolve "Arquivo recebido" (texto, ícone e borda), sem estado misto."""
        pagina = self.abrir(1366, 768)
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, "/cliente/upload")
        recebidos = pagina.locator(".dropzone.is-recebido")
        self.assertEqual(recebidos.count(), 1, "o relatório de vendas já recebido não aparece como recebido")
        # Localizador estável (por posição): a classe is-recebido sai e volta durante o teste.
        indice = pagina.locator(".dropzone").evaluate_all("els => els.findIndex(e => e.classList.contains('is-recebido'))")
        dz = pagina.locator(".dropzone").nth(indice)
        nome_recebido = dz.locator(".dz-title").text_content().strip()
        self.assertIn(".csv", nome_recebido)
        campo = dz.locator("input[type=file]")
        campo.set_input_files({"name": "novo.csv", "mimeType": "text/csv", "buffer": b"a;b\n"})
        self.assertFalse(dz.evaluate("e => e.classList.contains('is-recebido')"))
        campo.set_input_files({"name": "errado.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4"})
        self.assertIn("Formato não aceito", dz.locator(".dz-erro").text_content())
        self.assertTrue(dz.evaluate("e => e.classList.contains('is-recebido') && !e.classList.contains('is-selecionado')"))
        self.assertEqual(dz.locator(".dz-estado").text_content().strip(), "Arquivo recebido")
        self.assertEqual(dz.locator(".dz-title").text_content().strip(), nome_recebido)
        campo.set_input_files({"name": "novo.csv", "mimeType": "text/csv", "buffer": b"a;b\n"})
        campo.set_input_files([])
        self.assertTrue(dz.evaluate("e => e.classList.contains('is-recebido')"))
        self.assertEqual(dz.locator(".dz-title").text_content().strip(), nome_recebido)

    def test_pagina_nao_encontrada_dentro_do_portal(self):
        pagina = self.abrir(1366, 768)
        self.entrar(pagina, "cliente-test@example.invalid")
        resposta = pagina.goto(self.base + "/cliente/analise/999999", wait_until="load")
        self.assertEqual(resposta.status, 404)
        self.assertEqual(pagina.locator("html").get_attribute("lang"), "pt-BR")
        self.assertIn("Página não encontrada", pagina.locator("h1").text_content())
        self.assertTrue(pagina.locator("a", has_text="Voltar ao painel").is_visible())
        self.assertNotIn("Not Found", pagina.content())
        self.erros_js = [e for e in self.erros_js if "404" not in e]

    def test_paginas_publicas_sem_rolagem_horizontal(self):
        """Landing, login e "Como aderir": só regressão de layout (sem reformulação)."""
        for largura, altura in ((320, 568), (390, 844), (1366, 768)):
            pagina = self.abrir(largura, altura)
            for caminho in ("/", "/auth/login", "/como-aderir"):
                self.ir(pagina, caminho)
                pagina.wait_for_timeout(300)
                medida = pagina.evaluate("""() => ({rola: document.documentElement.scrollWidth > innerWidth + 1,
                    fora: [...document.querySelectorAll('a, button, input, h1, h2')].filter(el => {
                        const b = el.getBoundingClientRect(); const cs = getComputedStyle(el);
                        return b.width && cs.visibility !== 'hidden' && (b.left < -1 || b.right > innerWidth + 1); }).length})""")
                self.assertFalse(medida["rola"], f"{caminho} @ {largura}px rola na horizontal")
                self.assertEqual(medida["fora"], 0, f"{caminho} @ {largura}px: controle fora da tela")
            pagina.context.close()

    def test_nexobot_some_com_a_gaveta_aberta(self):
        pagina = self.abrir(390, 844)
        self.entrar(pagina, "cliente-test@example.invalid")
        self.ir(pagina, "/cliente/dashboard")
        pagina.click("#navToggle")
        pagina.wait_for_timeout(400)
        escondido = pagina.evaluate("getComputedStyle(document.getElementById('nexobotLauncher')).visibility === 'hidden'")
        self.assertTrue(escondido, "o NexoBot deve sumir com a gaveta aberta")
        self.assertTrue(pagina.locator(".sidebar .nav-logout").is_visible())
        pagina.keyboard.press("Escape")
        pagina.wait_for_timeout(400)
        self.assertFalse(pagina.evaluate("getComputedStyle(document.getElementById('nexobotLauncher')).visibility === 'hidden'"))


class PaginasDeErroTests(ContratosCase):
    """Sem navegador: as páginas de erro do portal e o JSON das chamadas /api/."""

    def _token(self, cliente, caminho="/auth/login"):
        import re
        pagina = cliente.get(caminho).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', pagina).group(1)

    def _entrar(self, cliente, email):
        resposta = cliente.post("/auth/login", data={"csrf_token": self._token(cliente), "email": email, "senha": self.password})
        self.assertEqual(resposta.status_code, 302, resposta.get_data(as_text=True)[:300])

    def test_404_em_portugues_com_link_de_volta(self):
        cliente = self.app.test_client()
        self._entrar(cliente, "cliente-test@example.invalid")
        resposta = cliente.get("/cliente/analise/999999")
        self.assertEqual(resposta.status_code, 404)
        corpo = resposta.get_data(as_text=True)
        self.assertIn("Página não encontrada", corpo)
        self.assertIn('lang="pt-BR"', corpo)
        self.assertIn("Voltar ao painel", corpo)
        self.assertNotIn("Not Found", corpo)
        # Sem sessão: link para o início e para o login.
        anonimo = self.app.test_client().get("/pagina-que-nao-existe")
        self.assertEqual(anonimo.status_code, 404)
        self.assertIn("Entrar no portal", anonimo.get_data(as_text=True))

    def test_api_devolve_json(self):
        cliente = self.app.test_client()
        self._entrar(cliente, "cliente-test@example.invalid")
        resposta = cliente.get("/api/rota-inexistente")
        self.assertEqual(resposta.status_code, 404)
        self.assertEqual(resposta.get_json()["fonte"], "local")

    def test_erro_500_com_falha_de_banco_e_usuario_logado(self):
        """Erro de banco no meio da requisição com usuário logado: a página de erro do
        NEXO sai mesmo assim (o sininho, que roda em toda página, não derruba o handler
        quando a consulta falha; no PostgreSQL a transação abortada é desfeita antes)."""
        from sqlalchemy.exc import OperationalError
        app = self.app
        app.config["PROPAGATE_EXCEPTIONS"] = False  # usa os errorhandlers, como em produção

        def _falha_de_banco():
            raise OperationalError("SELECT 1", None, Exception("conexão perdida"))

        try:
            app.add_url_rule("/__teste/erro-500", "teste_erro_500", _falha_de_banco)
        except AssertionError:  # aplicação já atendeu uma requisição
            from werkzeug.routing import Rule
            app.url_map.add(Rule("/__teste/erro-500", endpoint="teste_erro_500"))
            app.view_functions["teste_erro_500"] = _falha_de_banco
        cliente = app.test_client()
        self._entrar(cliente, "cliente-test@example.invalid")

        def _sininho_quebrado(*_a, **_k):
            raise OperationalError("SELECT notificacao", None, Exception("current transaction is aborted"))

        with patch("notifications.select", side_effect=_sininho_quebrado):
            resposta = cliente.get("/__teste/erro-500")
            self.assertEqual(resposta.status_code, 500)
            corpo = resposta.get_data(as_text=True)
            self.assertIn("Algo deu errado", corpo)
            self.assertIn("Voltar ao painel", corpo)
            self.assertNotIn("Internal Server Error", corpo)
            # Numa página comum, o sininho em falha também não derruba a página.
            painel = cliente.get("/cliente/dashboard")
            self.assertEqual(painel.status_code, 200)
            self.assertIn("Visão Geral", painel.get_data(as_text=True))

    def test_pagina_de_erro_sem_banco_nenhum(self):
        """Banco inacessível durante a renderização (context processors falhando):
        a página de erro sai mesmo assim, sem o link do painel, e nunca a página
        branca do servidor."""
        from sqlalchemy.exc import OperationalError
        app = self.app
        app.config["PROPAGATE_EXCEPTIONS"] = False

        def _sem_banco():
            raise OperationalError("SELECT 1", None, Exception("could not connect to server"))

        try:
            app.add_url_rule("/__teste/sem-banco", "teste_sem_banco", _sem_banco)
        except AssertionError:
            from werkzeug.routing import Rule
            app.url_map.add(Rule("/__teste/sem-banco", endpoint="teste_sem_banco"))
            app.view_functions["teste_sem_banco"] = _sem_banco
        cliente = app.test_client()
        self._entrar(cliente, "admin-test@example.invalid")
        # Todo context processor que toque o banco falha, inclusive o do próprio
        # Flask-Login (usuário expirado) — simulado por um processor que lança.
        app.template_context_processors[None].append(lambda: (_ for _ in ()).throw(
            OperationalError("SELECT usuario", None, Exception("server closed the connection"))))
        try:
            resposta = cliente.get("/__teste/sem-banco")
            self.assertEqual(resposta.status_code, 500)
            corpo = resposta.get_data(as_text=True)
            self.assertIn("Algo deu errado", corpo)
            self.assertNotIn("Internal Server Error", corpo)
            # Caminho de contingência (sem contexto da sessão): link para o início, não
            # para o painel, mesmo com o usuário logado.
            self.assertIn("Voltar ao início", corpo)
            self.assertNotIn("Voltar ao painel", corpo)
            nao_encontrada = cliente.get("/pagina-que-nao-existe")
            self.assertEqual(nao_encontrada.status_code, 404)
            self.assertIn("Página não encontrada", nao_encontrada.get_data(as_text=True))
        finally:
            app.template_context_processors[None].pop()

    def test_email_volta_preenchido_apos_senha_errada(self):
        cliente = self.app.test_client()
        resposta = cliente.post("/auth/login", data={"csrf_token": self._token(cliente),
                                                     "email": "alguem@example.invalid", "senha": "errada"})
        corpo = resposta.get_data(as_text=True)
        self.assertIn('value="alguem@example.invalid"', corpo)
        self.assertIn("E-mail ou senha incorretos", corpo)
