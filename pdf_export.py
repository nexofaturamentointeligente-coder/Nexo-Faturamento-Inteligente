"""
NEXO - Faturamento Inteligente | Exportação da Análise Executiva em PDF
======================================================================
Gera, com ReportLab (platypus), o PDF da análise publicada: cabeçalho da marca,
indicadores, Curva ABC (Top 5) com o gráfico de pizza da distribuição do
faturamento, síntese executiva em semáforos e a devolutiva do consultor.

Layout claro (fundo branco) — apropriado para impressão/anexo — com os acentos
de cor da identidade NEXO (roxo) e o mesmo código de cores da app para as
classes ABC e os semáforos. Mantém a regra: sem lucro/margem/CMV.

Paginação: o conteúdo ocupa exatamente a área entre as margens (quadro sem
recuo interno); nenhum título fica sozinho no fim de uma página — cada título
viaja com o primeiro bloco do seu conteúdo —, blocos curtos (indicadores,
tabela, gráfico com legenda, cada cartão) não se partem e os textos longos da
devolutiva quebram entre páginas normalmente. O rodapé de todas as páginas traz
"Página X de Y" e a data/hora de geração.

Data e hora de geração: sempre no horário oficial de Brasília
(America/Sao_Paulo), lidas no momento de cada exportação a partir de um instante
com fuso explícito. Não dependem do fuso configurado no servidor e não alteram
o período analisado nem nenhum dado gravado.

Função pública:
    gerar_pdf_analise(analise, semaforos, abc_top, *, gerado_em=None) -> bytes
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from io import BytesIO
import logging
import re
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Circle, Drawing, Rect
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate, CondPageBreak, Flowable, Frame, KeepTogether, PageTemplate, Paragraph,
    Spacer, Table, TableStyle,
)

log = logging.getLogger(__name__)

# ---- Paleta NEXO ----
ROXO = HexColor("#7c3aed")
LAVANDA = HexColor("#ede9fb")
TINTA = HexColor("#1f2330")
CINZA = HexColor("#6a7088")
LINHA = HexColor("#e6e8ef")
CONTORNO = HexColor("#c3c6d4")
FUNDO_SUAVE = HexColor("#f5f6fa")

CLASSE_COR = {"A": HexColor("#22c55e"), "B": HexColor("#f59e0b"), "C": HexColor("#9aa0b7")}
SEMAFORO_COR = {
    "critico": HexColor("#ef4444"),
    "atencao": HexColor("#f59e0b"),
    "oportunidade": HexColor("#22c55e"),
}
SEMAFORO_FUNDO = {
    "critico": HexColor("#fdecec"),
    "atencao": HexColor("#fdf4e3"),
    "oportunidade": HexColor("#e9f9ef"),
}
SEMAFORO_ROTULO = {"critico": "CRÍTICO", "atencao": "ATENÇÃO", "oportunidade": "OPORTUNIDADE"}

# Gráfico de pizza: rampa ordinal no matiz do roxo NEXO (mais escuro = maior
# participação; as fatias vêm em ordem de faturamento). Validada com o
# verificador de paletas: L monotônico, ΔL >= 0,06 entre vizinhos (cores
# distinguíveis também por daltônicos e na impressão em tons de cinza), um só
# matiz e contraste >= 2:1 no extremo claro. "Outros produtos" usa um cinza
# neutro claro (ΔE >= 16 contra todos os passos), com o contorno do círculo.
RAMPA_PIZZA = tuple(HexColor(c) for c in ("#5a06bb", "#7040ce", "#8765e0", "#9d83ee", "#b4a1fc"))
COR_OUTROS = HexColor("#d5d8e2")
TOP_N_PDF = 5                      # a tabela e o gráfico mostram os mesmos produtos
LIMITE_ROTULO_NA_FATIA = Decimal("12")   # % mínima para escrever o percentual dentro da fatia
CENTAVO = Decimal("0.01")
TOLERANCIA_PERCENTUAL = Decimal("0.01")  # o ETL grava a participação com 2 casas decimais
LINHAS_NOME_LEGENDA = 3                  # o nome completo fica na tabela da Curva ABC
LINHAS_PARAGRAFO_JUNTO = 8               # 1º parágrafo até 8 linhas viaja inteiro com o título

# ---- Geometria da página (A4, pontos) ----
MARGEM_X = 18 * mm
MARGEM_TOPO = 15 * mm
MARGEM_BASE = 24 * mm        # área reservada ao rodapé (régua em 17 mm, textos em 12,5 e 8,5 mm)
ALTURA_UTIL = A4[1] - MARGEM_TOPO - MARGEM_BASE
AVISO_RODAPE = ("Sinalização gerencial estimada (faturamento e Curva ABC). Não representa "
                "lucro, margem ou CMV.")

# ---- Horário oficial de Brasília ----
FUSO_HORARIO_OFICIAL = "America/Sao_Paulo"


@lru_cache(maxsize=1)
def fuso_brasilia() -> ZoneInfo:
    """Fuso IANA do horário oficial de Brasília (regras do banco de fusos)."""
    return ZoneInfo(FUSO_HORARIO_OFICIAL)


def _agora_utc() -> datetime:
    """Instante atual com fuso explícito. Único ponto de leitura do relógio
    (os testes o substituem para simular horários conhecidos)."""
    return datetime.now(timezone.utc)


def instante_de_geracao(gerado_em: datetime | None = None) -> datetime:
    """Instante da geração no horário oficial de Brasília.

    Sem argumento, lê o relógio agora — portanto cada exportação recebe o seu
    próprio horário. Um instante sem fuso é recusado: não há como saber a qual
    horário ele se refere (é exatamente o defeito do datetime.now() ingênuo).
    """
    instante = _agora_utc() if gerado_em is None else gerado_em
    if instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError("O instante de geração do PDF precisa ter fuso horário explícito.")
    return instante.astimezone(fuso_brasilia())


def formatar_data_hora(instante_local: datetime) -> str:
    """'29/10/2026 às 12:02' (o instante já deve estar no fuso de exibição).
    O "às" fica fora do strftime: o formato passado a ele é só ASCII, então
    nenhuma configuração de localidade do servidor impede a formatação."""
    return f"{instante_local:%d/%m/%Y} às {instante_local:%H:%M}"


def data_pdf(instante_local: datetime) -> str:
    """Data no formato dos metadados do PDF (CreationDate/ModDate), com o
    deslocamento do próprio instante: 'D:20261029120200-03'00''."""
    minutos = int(instante_local.utcoffset().total_seconds() // 60)
    sinal = "-" if minutos < 0 else "+"
    horas, resto = divmod(abs(minutos), 60)
    return f"D:{instante_local:%Y%m%d%H%M%S}{sinal}{horas:02d}'{resto:02d}'"


# ---- Formatação ----
def _texto_pdf(valor) -> str:
    """Escapa um valor externo uma única vez, na entrada de um Paragraph.

    O parser do ReportLab aceita marcação que pode carregar imagens e criar
    links. Dados comerciais são texto literal; somente a marcação fixa deste
    módulo é confiável. Não usar este escape em strings desenhadas no canvas
    ou em células de Table que não sejam Paragraphs.
    """
    return escape("" if valor is None else str(valor))


def _decimal(valor) -> Decimal | None:
    """Decimal finito a partir de Decimal/float/int/str; None se ausente ou inválido."""
    if valor is None:
        return None
    try:
        numero = valor if isinstance(valor, Decimal) else Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return numero if numero.is_finite() else None


def _brl(valor) -> str:
    """12345.6 -> '12.345,60' (sem o símbolo).

    Mesma regra do portal (filtro m.brl: float com 2 casas) e do PDF anterior,
    para o documento mostrar exatamente os números da tela."""
    try:
        v = float(valor or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"{v:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _pct(valor, casas: int = 2) -> str:
    """Participação calculada aqui (Decimal exato): 74.22871 -> '74,23%' (vírgula
    decimal, arredondamento meio para cima)."""
    numero = _decimal(valor) or Decimal(0)
    quantum = Decimal(1).scaleb(-casas)
    return f"{numero.quantize(quantum, ROUND_HALF_UP):.{casas}f}".replace(".", ",") + "%"


def _em_centavos(valor: Decimal) -> Decimal:
    """O valor como _brl o exibe (mesmo arredondamento), para as comparações de
    totais concordarem com o que o leitor vê."""
    return Decimal(f"{float(valor):.2f}")


def _pct_gravado(valor) -> str:
    """Percentual gravado pela Curva ABC, com a regra da página da análise
    ('%.1f' de float) e vírgula: 91.85 -> '91,8%' nos dois lugares."""
    try:
        v = float(valor or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"{v:.1f}".replace(".", ",") + "%"


def _num(valor) -> str:
    try:
        return f"{float(valor or 0):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def _tamanho_que_cabe(texto, fonte, tamanho, largura, minimo=8.0) -> float:
    """Maior corpo (até `tamanho`) em que `texto` cabe em `largura`."""
    while tamanho > minimo and stringWidth(texto, fonte, tamanho) > largura:
        tamanho -= 0.5
    return tamanho


def _encurtar(texto, fonte, tamanho, largura) -> str:
    """Corta com reticências para caber numa linha desenhada no canvas (busca
    binária pelo maior prefixo que cabe)."""
    texto = " ".join(str(texto or "").split())
    if stringWidth(texto, fonte, tamanho) <= largura:
        return texto
    cabe, nao_cabe = 0, len(texto)
    while nao_cabe - cabe > 1:
        meio = (cabe + nao_cabe) // 2
        if stringWidth(texto[:meio].rstrip() + "…", fonte, tamanho) <= largura:
            cabe = meio
        else:
            nao_cabe = meio
    return texto[:cabe].rstrip() + "…"


def _luminancia(cor) -> float:
    def canal(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * canal(cor.red) + 0.7152 * canal(cor.green) + 0.0722 * canal(cor.blue)


def _tinta_sobre(fundo):
    """Branco ou tinta escura, o que tiver mais contraste sobre o fundo."""
    lum = _luminancia(fundo)
    contraste_branco = 1.05 / (lum + 0.05)
    contraste_tinta = (lum + 0.05) / (_luminancia(TINTA) + 0.05)
    return white if contraste_branco >= contraste_tinta else TINTA


# ---- Cabeçalho e rodapé ----
class _BarraTopo(Flowable):
    """Faixa roxa de cabeçalho com a marca NEXO.

    Declara o próprio tamanho em wrap(): sem isso o Flowable mede 0 × 0 e a
    faixa é desenhada acima do quadro, fora da página (o defeito do cabeçalho
    cortado). Todo o desenho fica dentro de 0 <= y <= ALTURA.
    """
    ALTURA = 22 * mm
    RECUO = 10 * mm

    def __init__(self, largura, titulo, subtitulo):
        super().__init__()
        self.largura = largura
        self.altura = self.ALTURA
        self.titulo = titulo
        self.subtitulo = subtitulo
        self.width, self.height = largura, self.ALTURA

    def wrap(self, availWidth, availHeight):
        self.width = min(self.largura, availWidth)
        self.height = self.altura
        return self.width, self.height

    def draw(self):
        c = self.canv
        largura, altura = self.width, self.height
        c.saveState()
        c.setFillColor(ROXO)
        c.roundRect(0, 0, largura, altura, 6, stroke=0, fill=1)
        # Marca à esquerda (empilhada) e identificação do documento à direita,
        # ambas centradas verticalmente na faixa.
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(self.RECUO, 11.2 * mm, "NEXO")
        c.setFillColor(LAVANDA)
        c.setFont("Helvetica", 8.5)
        c.drawString(self.RECUO, 6.0 * mm, "Faturamento Inteligente")
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(largura - self.RECUO, 12.0 * mm, self.titulo)
        c.setFillColor(LAVANDA)
        c.setFont("Helvetica", 8.5)
        c.drawRightString(largura - self.RECUO, 6.6 * mm, self.subtitulo)
        c.restoreState()


def _desenhar_rodape(canvas, pagina, total, empresa, referencia, carimbo):
    """Rodapé de todas as páginas: aviso, 'Página X de Y', identificação e geração.
    Se faltar espaço, só o nome da empresa é encurtado: a referência fica sempre."""
    x0, x1 = MARGEM_X, A4[0] - MARGEM_X
    canvas.saveState()
    canvas.setStrokeColor(LINHA)
    canvas.setLineWidth(0.5)
    canvas.line(x0, 17 * mm, x1, 17 * mm)
    numeracao = f"Página {pagina} de {total}"
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.setFillColor(TINTA)
    canvas.drawRightString(x1, 12.5 * mm, numeracao)
    espaco = x1 - x0 - stringWidth(numeracao, "Helvetica-Bold", 7.5) - 6 * mm
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(CINZA)
    canvas.drawString(x0, 12.5 * mm, _encurtar(AVISO_RODAPE, "Helvetica", 7.5, espaco))
    geracao = f"Gerado em {carimbo} (horário de Brasília)"
    canvas.setFont("Helvetica", 7)
    canvas.drawRightString(x1, 8.5 * mm, geracao)
    espaco = x1 - x0 - stringWidth(geracao, "Helvetica", 7) - 6 * mm
    fixo = f"NEXO ·  · Referência {referencia}"
    nome = _encurtar(empresa, "Helvetica", 7, espaco - stringWidth(fixo, "Helvetica", 7))
    partes = ["NEXO", nome, f"Referência {referencia}"] if nome else ["NEXO", f"Referência {referencia}"]
    canvas.drawString(x0, 8.5 * mm, " · ".join(partes))
    canvas.restoreState()


def _canvas_com_rodape(empresa, referencia, carimbo, metadados_data):
    """Canvas que adia o rodapé até o fim do documento para conhecer o total de
    páginas. Os metadados de criação/modificação do arquivo usam o mesmo instante
    do carimbo (em vez do relógio local do servidor, que o ReportLab usaria)."""
    class _CanvasNexo(Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.setDateFormatter(lambda *_: metadados_data)
            self._paginas_pendentes = []

        def showPage(self):
            self._paginas_pendentes.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._paginas_pendentes)
            for estado in self._paginas_pendentes:
                self.__dict__.update(estado)
                _desenhar_rodape(self, self._pageNumber, total, empresa, referencia, carimbo)
                super().showPage()
            super().save()

    return _CanvasNexo


# ---- Estilos ----
def _estilos():
    ss = getSampleStyleSheet()
    base = ss["Normal"]
    base.fontName = "Helvetica"
    base.textColor = TINTA
    estilos = {
        "titulo": ParagraphStyle("titulo", parent=base, fontName="Helvetica-Bold", fontSize=16,
                                 leading=20, textColor=TINTA, spaceAfter=4),
        # Espaço entre seções como spaceBefore (o quadro o ignora no topo da página).
        "h2": ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold",
                             fontSize=12.5, leading=15, textColor=ROXO, spaceBefore=7 * mm, spaceAfter=6),
        "h3": ParagraphStyle("h3", parent=base, fontName="Helvetica-Bold",
                             fontSize=10.5, leading=13, textColor=TINTA, spaceBefore=2, spaceAfter=3),
        "h3_seguinte": ParagraphStyle("h3s", parent=base, fontName="Helvetica-Bold", fontSize=10.5,
                                      leading=13, textColor=TINTA, spaceBefore=2 + 2 * mm, spaceAfter=3),
        "sub": ParagraphStyle("sub", parent=base, fontSize=8.5, leading=11.5, textColor=CINZA, spaceAfter=0),
        "p": ParagraphStyle("p", parent=base, fontSize=9.5, leading=14, textColor=TINTA, spaceAfter=5,
                            allowWidows=0),
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=11, textColor=CINZA),
        "fechamento": ParagraphStyle("fech", parent=base, fontSize=8, leading=11, textColor=CINZA,
                                     spaceBefore=6 * mm),
        "aviso": ParagraphStyle("aviso", parent=base, fontSize=8, leading=11, textColor=TINTA),
        "kpi_label": ParagraphStyle("kl", parent=base, fontSize=7.5, leading=9, textColor=CINZA),
        "kpi_value": ParagraphStyle("kv", parent=base, fontName="Helvetica-Bold",
                                    fontSize=13, leading=16, textColor=TINTA),
        "sc_title": ParagraphStyle("sct", parent=base, fontName="Helvetica-Bold",
                                   fontSize=10, textColor=TINTA),
        "sc_key": ParagraphStyle("sck", parent=base, fontName="Helvetica-Bold",
                                 fontSize=7, textColor=CINZA),
        "sc_val": ParagraphStyle("scv", parent=base, fontSize=8.5, leading=12, textColor=TINTA),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9, leading=12),
        "cell_b": ParagraphStyle("cellb", parent=base, fontName="Helvetica-Bold", fontSize=9),
        "leg": ParagraphStyle("leg", parent=base, fontSize=8.5, leading=10.5, textColor=TINTA),
    }
    return estilos


def _paragrafos(texto, estilo):
    """Texto livre do consultor em Paragraphs, preservando a estrutura escrita
    (linha em branco = novo parágrafo; quebra simples = nova linha), como a
    página web faz com white-space: pre-line. Cada linha é escapada."""
    normalizado = str(texto or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    paragrafos = []
    for bloco in re.split(r"\n[ \t]*\n", normalizado):
        linhas = [linha.strip() for linha in bloco.split("\n") if linha.strip()]
        if linhas:
            paragrafos.append(Paragraph("<br/>".join(_texto_pdf(l) for l in linhas), estilo))
    return paragrafos


# ---- Blocos de conteúdo ----
def _kpis_flowable(ind, largura, est):
    rotulos = ("FATURAMENTO TOTAL", "TOTAL COMPRADO", "PRESSÃO DE ESTOQUE")
    valores = (ind.faturamento_total, ind.total_comprado, ind.saldo_estimado_compras_vendas)
    col = largura / 3.0
    recuo = 10
    base = est["kpi_value"]
    linha_valores = []
    for valor in valores:
        texto = f"R$ {_brl(valor)}"
        # Valores grandes reduzem o corpo para caber na célula (nunca saem dela).
        corpo = _tamanho_que_cabe(texto, base.fontName, base.fontSize, col - 2 * recuo)
        estilo = base if corpo == base.fontSize else ParagraphStyle(
            f"kv{corpo}", parent=base, fontSize=corpo, leading=corpo * 1.25)
        linha_valores.append(Paragraph(texto, estilo))
    dados = [[Paragraph(r, est["kpi_label"]) for r in rotulos], linha_valores]
    t = Table(dados, colWidths=[col, col, col])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), FUNDO_SUAVE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINHA),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINHA),
        ("LEFTPADDING", (0, 0), (-1, -1), recuo), ("RIGHTPADDING", (0, 0), (-1, -1), recuo),
        ("TOPPADDING", (0, 0), (-1, 0), 9), ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
        ("TOPPADDING", (0, 1), (-1, 1), 3), ("BOTTOMPADDING", (0, 1), (-1, 1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _abc_flowable(abc_top, largura, est):
    head = ["#", "Produto", "Classe", "Faturamento (R$)", "% acum."]
    larguras = [10 * mm, largura - 84 * mm, 18 * mm, 34 * mm, 22 * mm]
    linhas = [head]
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), ROXO),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("ALIGN", (3, 0), (4, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINHA),
        ("BOX", (0, 0), (-1, -1), 0.5, LINHA),
    ]
    util_valor = larguras[3] - 12
    for i, p in enumerate(abc_top[:TOP_N_PDF], start=1):
        valor = _brl(p.faturamento)
        linhas.append([
            str(p.posicao_ranking),
            Paragraph(_texto_pdf(p.produto_nome), est["cell"]),
            p.classe_abc,
            valor,
            _pct_gravado(p.percentual_acumulado),
        ])
        corpo = _tamanho_que_cabe(valor, "Helvetica", 9, util_valor, minimo=6.5)
        if corpo != 9:
            estilo.append(("FONTSIZE", (3, i), (3, i), corpo))
        # Cor da célula de classe + zebra
        cor = CLASSE_COR.get(p.classe_abc, CINZA)
        estilo.append(("TEXTCOLOR", (2, i), (2, i), cor))
        estilo.append(("FONTNAME", (2, i), (2, i), "Helvetica-Bold"))
        if i % 2 == 0:
            estilo.append(("BACKGROUND", (0, i), (-1, i), FUNDO_SUAVE))
    # splitInRow: um nome excepcionalmente longo quebra entre páginas em vez de
    # impedir a geração (o bloco continua inteiro em todos os casos normais).
    t = Table(linhas, colWidths=larguras, repeatRows=1, splitInRow=1)
    t.setStyle(TableStyle(estilo))
    return t


@dataclass(frozen=True)
class Fatia:
    nome: str
    classe: str | None
    faturamento: Decimal
    percentual: Decimal          # sobre a base, sem arredondar
    cor: object
    outros: bool = False


@dataclass(frozen=True)
class Distribuicao:
    fatias: tuple
    base: Decimal                # denominador das participações
    base_e_faturamento_total: bool
    avisos: tuple                # texto exibido no PDF ("Atenção: ...")
    inconsistencias: tuple = ()  # códigos para o log, sem valores comerciais


def calcular_distribuicao(faturamento_total, produtos) -> Distribuicao | None:
    """Participação individual de cada produto exibido na tabela Curva ABC.

    participação = faturamento do produto ÷ faturamento total do período × 100;
    "Outros produtos" = faturamento total − soma dos produtos exibidos (quando a
    diferença aparece em centavos). Os dados não são ajustados para a soma dos percentuais
    arredondados dar 100%. Inconsistências viram aviso explícito no PDF e código
    em `inconsistencias` (para o log), sem alterar os dados:
      - produto da tabela com faturamento zero, negativo ou inválido: fica fora
        do gráfico e a base passa a ser a soma dos exibidos (o valor dele não é
        confundido com "Outros");
      - faturamento total ausente ou não positivo, ou menor que a soma dos
        produtos exibidos: a base é a soma dos produtos, sem "Outros";
      - participação gravada pela Curva ABC diferente da calculada sobre o
        faturamento total (há valores negativos fora da tabela, p. ex. devoluções).
    Devolve None só quando não há produtos; sem nenhum faturamento positivo,
    devolve uma distribuição sem fatias, com o aviso.
    """
    produtos = list(produtos or [])[:TOP_N_PDF]
    if not produtos:
        return None
    avisos, codigos = [], []
    exibidos = []
    for p in produtos:
        valor = _decimal(p.faturamento)
        if valor is not None and valor > 0:
            exibidos.append((p, valor))
    ignorados = len(produtos) - len(exibidos)
    if not exibidos:
        return Distribuicao(fatias=(), base=Decimal(0), base_e_faturamento_total=False,
                            avisos=("Nenhum produto da tabela tem faturamento positivo: não há distribuição "
                                    "para exibir.",),
                            inconsistencias=("sem_faturamento_positivo",))
    if ignorados:
        codigos.append("produto_sem_faturamento_positivo")
        avisos.append(f"{ignorados} produto(s) da tabela com faturamento zero, negativo ou inválido "
                      "não entram no gráfico.")
    soma = sum((valor for _, valor in exibidos), Decimal(0))
    total = _decimal(faturamento_total)
    usa_total = False
    if total is None:
        codigos.append("total_ausente")
        avisos.append("Faturamento total do período não informado: as participações foram calculadas "
                      "sobre a soma dos produtos exibidos.")
    elif total <= 0:
        codigos.append("total_nao_positivo")
        avisos.append(f"Faturamento total do período não é positivo (R$ {_brl(total)}): as participações "
                      "foram calculadas sobre a soma dos produtos exibidos.")
    elif _em_centavos(soma) > _em_centavos(total):
        codigos.append("soma_maior_que_total")
        avisos.append(f"A soma dos produtos exibidos (R$ {_brl(soma)}) é maior que o faturamento total "
                      f"informado (R$ {_brl(total)}); as participações foram calculadas sobre a soma "
                      "dos produtos, sem a fatia Outros produtos.")
    elif ignorados:
        avisos.append("Com produto da tabela fora do gráfico, as participações foram calculadas sobre a "
                      "soma dos produtos exibidos, sem a fatia Outros produtos.")
    else:
        usa_total = True
    base = total if usa_total else soma
    fatias = [
        Fatia(nome=str(p.produto_nome or ""), classe=p.classe_abc, faturamento=valor,
              percentual=valor / base * 100, cor=RAMPA_PIZZA[min(i, len(RAMPA_PIZZA) - 1)])
        for i, (p, valor) in enumerate(exibidos)
    ]
    if usa_total:
        if _em_centavos(total) - _em_centavos(soma) >= CENTAVO:       # diferença visível
            restante = total - soma
            fatias.append(Fatia(nome="Outros produtos", classe=None, faturamento=restante,
                                percentual=restante / base * 100, cor=COR_OUTROS, outros=True))
        gravadas = [(fatia.percentual, _decimal(getattr(p, "percentual_individual", None)))
                    for fatia, (p, _) in zip(fatias, exibidos)]
        if any(g is not None and abs(calc - g) > TOLERANCIA_PERCENTUAL for calc, g in gravadas):
            codigos.append("base_abc_divergente")
            avisos.append(f"As participações acima usam o faturamento total do período (R$ {_brl(total)}) e "
                          "diferem do percentual individual da Curva ABC, que considera só os produtos com "
                          "faturamento positivo: o período tem valores negativos fora da tabela (por "
                          "exemplo, devoluções).")
    return Distribuicao(fatias=tuple(fatias), base=base, base_e_faturamento_total=usa_total,
                        avisos=tuple(avisos), inconsistencias=tuple(codigos))


def _grafico_pizza(distribuicao, diametro):
    """Pizza vetorial 2D (ReportLab Graphics): fatias proporcionais, separadas
    por um filete branco, começando às 12 h no sentido horário. Só as fatias
    grandes recebem o percentual dentro; as demais ficam na legenda."""
    folga = 2 * mm
    lado = diametro + 2 * folga
    desenho = Drawing(lado, lado)
    pizza = Pie()
    pizza.x = pizza.y = folga
    pizza.width = pizza.height = diametro
    pizza.data = [float(f.faturamento) for f in distribuicao.fatias]
    pizza.startAngle = 90
    pizza.direction = "clockwise"
    pizza.sideLabels = 0
    pizza.simpleLabels = 1
    pizza.labels = [_pct(f.percentual) if f.percentual >= LIMITE_ROTULO_NA_FATIA else ""
                    for f in distribuicao.fatias]
    pizza.slices.strokeColor = white
    pizza.slices.strokeWidth = 1.2
    pizza.slices.labelRadius = 0.62
    pizza.slices.fontName = "Helvetica-Bold"
    pizza.slices.fontSize = 8.5
    for i, fatia in enumerate(distribuicao.fatias):
        pizza.slices[i].fillColor = fatia.cor
        pizza.slices[i].fontColor = _tinta_sobre(fatia.cor)
    if len(distribuicao.fatias) == 1:
        pizza.slices[0].labelRadius = 0      # círculo inteiro: o percentual vai ao centro
    desenho.add(pizza)
    # Contorno fino do círculo: mantém visível a borda de fatias claras sobre o papel branco.
    desenho.add(Circle(folga + diametro / 2, folga + diametro / 2, diametro / 2,
                       fillColor=None, strokeColor=CONTORNO, strokeWidth=0.6))
    return desenho


def _amostra(cor):
    lado = 3.6 * mm
    desenho = Drawing(lado, lado)
    desenho.add(Rect(0, 0, lado, lado, rx=1.2, ry=1.2, fillColor=cor,
                     strokeColor=CONTORNO if cor == COR_OUTROS else None, strokeWidth=0.5))
    return desenho


def _nome_em_linhas(nome, estilo, largura, linhas=LINHAS_NOME_LEGENDA):
    """Nome do produto em no máximo `linhas` linhas da coluna, com reticências
    quando precisa cortar: a legenda tem altura limitada e cabe sempre ao lado
    do gráfico. O nome completo continua na tabela da Curva ABC."""
    texto = " ".join(str(nome or "").split())
    limite = linhas * estilo.leading + 0.5

    def cabe(t):
        return Paragraph(_texto_pdf(t), estilo).wrap(largura, ALTURA_UTIL)[1] <= limite

    if cabe(texto):
        return Paragraph(_texto_pdf(texto), estilo)
    cabe_ate, nao_cabe = 0, len(texto)
    while nao_cabe - cabe_ate > 1:
        meio = (cabe_ate + nao_cabe) // 2
        if cabe(texto[:meio].rstrip() + "…"):
            cabe_ate = meio
        else:
            nao_cabe = meio
    return Paragraph(_texto_pdf(texto[:cabe_ate].rstrip() + "…"), estilo)


def _legenda_flowable(distribuicao, largura, est):
    larguras = [7 * mm, largura - 70 * mm, 14 * mm, 28 * mm, 21 * mm]
    util_nome = larguras[1] - 7                 # recuos da célula: 3 + 4 pt
    util_valor = larguras[3] - 7
    cab = ParagraphStyle("legh", parent=est["leg"], fontName="Helvetica-Bold", fontSize=8, leading=10)
    linhas = [["", Paragraph("Produto", cab), "Classe", "Faturamento (R$)", "Participação"]]
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), FUNDO_SUAVE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("TEXTCOLOR", (0, 0), (-1, -1), TINTA),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("ALIGN", (3, 0), (4, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINHA),
        ("FONTNAME", (4, 1), (4, -1), "Helvetica-Bold"),
    ]
    for i, fatia in enumerate(distribuicao.fatias, start=1):
        nome = _nome_em_linhas(fatia.nome, est["leg"], util_nome)
        valor = _brl(fatia.faturamento)
        corpo = _tamanho_que_cabe(valor, "Helvetica", 8.5, util_valor, minimo=6.5)
        if corpo != 8.5:
            estilo.append(("FONTSIZE", (3, i), (3, i), corpo))
        if fatia.outros:
            classe = "—"
        else:
            classe = fatia.classe
            estilo.append(("TEXTCOLOR", (2, i), (2, i), CLASSE_COR.get(fatia.classe, CINZA)))
            estilo.append(("FONTNAME", (2, i), (2, i), "Helvetica-Bold"))
        linhas.append([_amostra(fatia.cor), nome, classe, valor, _pct(fatia.percentual)])
    rotulo_total = "Faturamento total" if distribuicao.base_e_faturamento_total else "Soma dos produtos exibidos"
    total = _brl(distribuicao.base)
    linhas.append(["", Paragraph(f"<b>{rotulo_total}</b>", est["leg"]), "", total, "100,00%"])
    estilo += [
        ("FONTNAME", (3, -1), (4, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.6, CONTORNO),
    ]
    corpo = _tamanho_que_cabe(total, "Helvetica-Bold", 8.5, util_valor, minimo=6.5)
    if corpo != 8.5:
        estilo.append(("FONTSIZE", (3, -1), (3, -1), corpo))
    t = Table(linhas, colWidths=larguras)
    t.setStyle(TableStyle(estilo))
    return t


def _secao_distribuicao(distribuicao, largura, est):
    """Título, subtítulo, gráfico (à esquerda) + legenda (à direita) e nota, num
    bloco que não se parte: se não couber depois da tabela, vai inteiro para a
    página seguinte."""
    cabeca = [
        Paragraph("Distribuição do faturamento por produto", est["h2"]),
        Paragraph("Participação dos principais produtos no faturamento do período.", est["sub"]),
    ]
    avisos = []
    for aviso in distribuicao.avisos:
        avisos += [Spacer(1, 1.5 * mm), Paragraph(f"<b>Atenção:</b> {_texto_pdf(aviso)}", est["aviso"])]
    if not distribuicao.fatias:              # nada a desenhar: título, subtítulo e o motivo
        return KeepTogether(cabeca + avisos)
    largura_grafico = 62 * mm
    grafico = _grafico_pizza(distribuicao, diametro=54 * mm)
    legenda = _legenda_flowable(distribuicao, largura - largura_grafico, est)
    corpo = Table([[grafico, legenda]], colWidths=[largura_grafico, largura - largura_grafico])
    corpo.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    nota = ("Participação individual = faturamento do produto ÷ faturamento total do período × 100."
            if distribuicao.base_e_faturamento_total else
            "Participação individual = faturamento do produto ÷ soma dos produtos exibidos × 100.")
    if any(f.outros for f in distribuicao.fatias):
        nota += f" “Outros produtos” reúne o faturamento dos itens fora do Top {TOP_N_PDF}."
    soma_arredondada = sum((Decimal(_pct(f.percentual)[:-1].replace(",", ".")) for f in distribuicao.fatias),
                           Decimal(0))
    if soma_arredondada != Decimal("100.00"):
        nota += (" Percentuais arredondados para duas casas decimais: a soma exibida pode diferir "
                 "de 100% por arredondamento.")
    return KeepTogether(cabeca + [
        Spacer(1, 3 * mm),
        corpo,
        Spacer(1, 2.5 * mm),
        Paragraph(_texto_pdf(nota), est["small"]),
    ] + avisos)


def _semaforo_flowable(card, largura, est):
    cor = SEMAFORO_COR.get(card["nivel"], CINZA)
    fundo = SEMAFORO_FUNDO.get(card["nivel"], FUNDO_SUAVE)
    rotulo = SEMAFORO_ROTULO.get(card["nivel"], card["nivel"].upper())
    interno = Table([
        [Paragraph(f'<b>{_texto_pdf(card["titulo"])}</b>  <font size="7" color="#6a7088">· {_texto_pdf(rotulo)}</font>', est["sc_title"])],
        [Paragraph("O QUE FAZER", est["sc_key"])],
        [Paragraph(_texto_pdf(card["o_que_fazer"]), est["sc_val"])],
        [Paragraph("POR QUE FAZER", est["sc_key"])],
        [Paragraph(_texto_pdf(card["por_que"]), est["sc_val"])],
    ], colWidths=[largura - 8 * mm])
    interno.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 1), (0, 1), 5),
    ]))
    wrap = Table([[interno]], colWidths=[largura])
    wrap.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fundo),
        ("LINEBEFORE", (0, 0), (0, -1), 3, cor),
        ("BOX", (0, 0), (-1, -1), 0.4, LINHA),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return wrap


def _semaforo_divisivel(card, largura, est):
    """Mesmo cartão numa tabela de uma coluna que pode se dividir entre páginas.
    Só é usado quando o cartão normal não cabe numa página inteira (texto
    excepcionalmente longo); assim a exportação nunca falha por tamanho."""
    original = _semaforo_flowable(card, largura, est)._cellvalues[0][0]
    linhas = [[linha[0]] for linha in original._cellvalues]
    t = Table(linhas, colWidths=[largura], splitInRow=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SEMAFORO_FUNDO.get(card["nivel"], FUNDO_SUAVE)),
        ("LINEBEFORE", (0, 0), (0, -1), 3, SEMAFORO_COR.get(card["nivel"], CINZA)),
        ("BOX", (0, 0), (-1, -1), 0.4, LINHA),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (0, 0), 9), ("TOPPADDING", (0, 1), (0, 1), 5),
        ("BOTTOMPADDING", (0, -1), (0, -1), 9),
    ]))
    return t


def _cartao(card, largura, est):
    """Cartão inteiro (não se parte); se nem uma página inteira o comporta, a
    versão divisível."""
    cartao = _semaforo_flowable(card, largura, est)
    if cartao.wrap(largura, ALTURA_UTIL)[1] > ALTURA_UTIL:
        return _semaforo_divisivel(card, largura, est)
    return cartao


def _com_titulos(titulos, paragrafo, largura, est):
    """Títulos + primeiro parágrafo, sem título isolado e sem buraco na página:
    um parágrafo curto viaja inteiro com os títulos; um longo começa logo abaixo
    deles (com espaço garantido para os títulos e três linhas) e quebra entre
    páginas normalmente (estilo sem linhas viúvas ou órfãs)."""
    altura = paragrafo.wrap(largura, ALTURA_UTIL)[1]
    if altura <= LINHAS_PARAGRAFO_JUNTO * paragrafo.style.leading + 0.5:
        return [KeepTogether(titulos + [paragrafo])]
    reserva = sum(t.wrap(largura, ALTURA_UTIL)[1] + t.getSpaceBefore() + t.getSpaceAfter() for t in titulos)
    return [CondPageBreak(reserva + 3 * paragrafo.style.leading)] + titulos + [paragrafo]


def _fechar_documento(elems, fechamento, largura):
    """A frase final viaja com o fim do último bloco e nunca fica sozinha numa
    página nova. Se o último bloco é um parágrafo longo, só as duas últimas
    linhas dele a acompanham (o restante segue o fluxo normal), para não empurrar
    o parágrafo inteiro para a página seguinte."""
    ultimo = elems.pop()
    if isinstance(ultimo, KeepTogether):
        # Um KeepTogether dentro de outro mede 0xffffff de altura e força quebra de
        # página mesmo com espaço sobrando: a frase final entra no mesmo bloco.
        elems.append(KeepTogether(list(ultimo._content) + [fechamento]))
        return
    if isinstance(ultimo, Paragraph):
        ultimo.wrap(largura, ALTURA_UTIL)
        linhas = len(getattr(ultimo.blPara, "lines", ()))
        if linhas > 3:
            partes = ultimo.split(largura, (linhas - 2) * ultimo.style.leading + 0.1)
            if len(partes) == 2:
                inicio, fim = partes
                inicio.style = ParagraphStyle(f"{inicio.style.name}_cont", parent=inicio.style, spaceAfter=0)
                elems.extend([inicio, KeepTogether([fim, fechamento])])
                return
    elems.append(KeepTogether([ultimo, fechamento]))


def _devolutiva(rel, est, largura):
    """Título da seção, subtítulos e textos do consultor, sem títulos isolados:
    'Devolutiva do consultor' + 'Resumo executivo' + o 1º parágrafo andam
    juntos, assim como 'Conclusão estratégica' + o seu 1º parágrafo."""
    titulo = Paragraph("Devolutiva do consultor", est["h2"])
    secoes = []
    for subtitulo, texto in (("Resumo executivo", rel.resumo_executivo),
                             ("Conclusão estratégica", rel.conclusao_estrategica)):
        paragrafos = _paragrafos(texto, est["p"])
        if paragrafos:
            secoes.append((subtitulo, paragrafos))
    if not secoes:
        return [KeepTogether([titulo, Paragraph("Sem texto registrado pelo consultor.", est["small"])])]
    elems = []
    for i, (subtitulo, paragrafos) in enumerate(secoes):
        titulos = ([titulo] if i == 0 else []) + [Paragraph(subtitulo, est["h3"] if i == 0 else est["h3_seguinte"])]
        elems.extend(_com_titulos(titulos, paragrafos[0], largura, est))
        elems.extend(paragrafos[1:])
    return elems


def _versao_publicada(relatorio):
    """SEC-003: o PDF reproduz a versão PUBLICADA do relatório, nunca o rascunho.

    Para o modelo persistido, a leitura é RelatorioAnalise.publicada (cópia
    congelada na publicação, ou o texto legado quando a cópia não existe).
    Objetos simples já representam o texto a imprimir e passam inalterados.
    """
    from models import RelatorioAnalise
    if isinstance(relatorio, RelatorioAnalise):
        return relatorio.publicada
    return relatorio


def gerar_pdf_analise(analise, semaforos, abc_top, *, gerado_em=None) -> bytes:
    """Monta o PDF da análise e devolve os bytes.

    `gerado_em` (opcional, com fuso) fixa o instante da geração; sem ele, o
    relógio é lido agora — cada chamada recebe o horário da própria exportação.
    """
    instante = instante_de_geracao(gerado_em)
    geracao = formatar_data_hora(instante)
    buf = BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGEM_X, rightMargin=MARGEM_X, topMargin=MARGEM_TOPO, bottomMargin=MARGEM_BASE,
        title=f"Analise NEXO #{analise.id_analise}", author="NEXO",
    )
    # Quadro sem recuo interno: a largura útil é exatamente a área entre as margens.
    quadro = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="conteudo",
                   leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="nexo", frames=[quadro])])
    largura = doc.width
    est = _estilos()
    emp = analise.empresa
    rel = _versao_publicada(analise.relatorio)
    ind = analise.indicador
    produtos = list(abc_top or [])[:TOP_N_PDF]

    elems = []
    ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
    elems.append(_BarraTopo(largura, "Análise Executiva", f"Referência {ref}"))
    elems.append(Spacer(1, 7 * mm))

    titulo = rel.titulo if rel else "Análise publicada"
    elems.append(Paragraph(_texto_pdf(titulo), est["titulo"]))
    periodo = f"{analise.periodo_inicio.strftime('%d/%m/%Y')} – {analise.periodo_fim.strftime('%d/%m/%Y')}"
    nome_empresa = emp.nome_fantasia or emp.razao_social
    elems.append(Paragraph(
        f"{_texto_pdf(nome_empresa)} &nbsp;·&nbsp; CNPJ {_texto_pdf(emp.cnpj)} &nbsp;·&nbsp; Período {_texto_pdf(periodo)}",
        est["small"]))

    # Indicadores
    if ind:
        elems.append(KeepTogether([Paragraph("Indicadores do período", est["h2"]),
                                   _kpis_flowable(ind, largura, est)]))

    # Curva ABC: título, tabela e explicação juntos; depois, o gráfico de pizza.
    if produtos:
        elems.append(KeepTogether([
            Paragraph(f"Curva ABC · Top {len(produtos)} {'produto' if len(produtos) == 1 else 'produtos'} "
                      "(Princípio de Pareto)", est["h2"]),
            _abc_flowable(produtos, largura, est),
            Spacer(1, 2 * mm),
            Paragraph("Classe A: até 80% do faturamento acumulado (estratégicos) · "
                      "B: 80–95% (táticos) · C: ~5% restantes (operacionais).", est["small"]),
        ]))
        distribuicao = calcular_distribuicao(ind.faturamento_total if ind else None, produtos)
        if distribuicao is not None:
            if distribuicao.inconsistencias:
                # Só códigos e o identificador: valores comerciais não vão para o log.
                log.warning("PDF da análise %s: dados do gráfico com inconsistência (%s).",
                            analise.id_analise, ", ".join(distribuicao.inconsistencias))
            elems.append(_secao_distribuicao(distribuicao, largura, est))

    # Síntese executiva (semáforos): o título viaja com o primeiro cartão.
    if semaforos:
        cartoes = [_cartao(card, largura, est) for card in semaforos]
        elems.append(KeepTogether([Paragraph("Síntese executiva", est["h2"]), cartoes[0]]))
        for cartao in cartoes[1:]:
            cartao.spaceBefore = 3 * mm          # ignorado no topo da página
            elems.append(KeepTogether([cartao]))

    # Devolutiva
    if rel:
        elems.extend(_devolutiva(rel, est, largura))

    fechamento = Paragraph(
        f"Documento gerado pelo portal NEXO em {geracao} (horário de Brasília).", est["fechamento"])
    _fechar_documento(elems, fechamento, largura)

    doc.build(elems, canvasmaker=_canvas_com_rodape(nome_empresa or "", ref, geracao, data_pdf(instante)))
    return buf.getvalue()
