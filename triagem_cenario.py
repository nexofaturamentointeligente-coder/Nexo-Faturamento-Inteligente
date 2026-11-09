"""Triagem da Avaliação de Cenário: Urgência, Atenção e Eletiva, e as marcas por produto e ano.

Função pura (sem banco, Flask nem config), no processo pai, sobre as linhas da staging
(brainstorming_dados_pdv) já quantizadas como foram gravadas: refazê-la com os mesmos limiares dá o
mesmo resultado, e `flask leads retriar` a refaz durante a retenção de 30 dias, sem novo envio dos
relatórios. Etapa 2 (triagem-2026-11.3): limiares configuráveis (Limiares), omissão no ERP, margem
negativa recorrente, custo implausível, estoque, cauda longa e mix.

Marcas, por produto e ano (marcas()):
  - venda sem custo: o produto vendeu nos relatórios que trazem a coluna de custo e nenhuma dessas
    vendas tem custo (o mesmo critério de saude.itens_sem_custo do Raio-X). Num ano sem relatório com
    a coluna de custo, a marca não se aplica: a falta da coluna é do arquivo exportado, não do ERP;
  - venda abaixo do custo: alguma venda com custo teve receita abaixo do custo da linha (perda > 0,
    o mesmo critério de auditoria.itens_venda_abaixo_custo do Raio-X);
  - omissão no ERP (motivos_de_omissao()): conflito de código e nome no cadastro (ignorado quando algum
    relatório teve cabeçalho ambíguo), custo informado só em parte das vendas (custo_parcial), custo
    implausível (custo >= razao_custo_implausivel x a venda com custo), estoque negativo (entrada de
    compra não lançada), NCM ausente e categoria ausente (só nos anos em que o relatório traz a coluna);
  - vazamento estimado: o valor recuperável do produto no ano;
  - custo médio registrado: custo total / quantidade vendida com custo, com 4 casas.

Ritmo e giro (Cobertura, da meta do envio): o ritmo é a venda por DIA coberto pelos relatórios. A queda
compara o último ano com o ano anterior mais recente com venda e só vale quando o ritmo anterior prevê
ao menos `venda_esperada_minima` unidade no período comparado do último ano. Quando os dois últimos anos
do envio são mensais, a comparação usa só os meses do calendário que os dois cobrem (os completos do
último ano; o mês corrente, parcial, fica de fora): o panetone vendido em dezembro não "parou" quando o
último ano vai de janeiro a outubro. Sem mês em comum (12 meses de novembro a outubro), não há
comparação de ritmo. Com ano anterior não mensal, ou comparando com um ano mais antigo, valem os
períodos inteiros. O giro (parte dos relatórios do último ano com venda) só existe quando os relatórios
do último ano são mensais; o de um lançamento (sem venda nos anos anteriores) conta só os relatórios
desde a primeira venda dele, e o giro crítico exige `relatorios_para_giro_critico` relatórios nessa base.

Blocos, por produto e olhando todos os anos (triar()); cada produto fica em no máximo um, nesta ordem:
  1. Urgência, qualquer um:
     (a) potencial recuperável alto (entre os que somam `pareto_recuperavel` do recuperável do lead, sem
         os itens de custo implausível; ao menos `recuperavel_minimo_do_produto` do faturamento do
         produto e `impacto_minimo_do_lead` do faturamento do envio) E estagnação crítica: a venda
         parou, caiu muito ou girou pouco (venda_parou, venda_em_queda_forte, giro_critico); com
         estoque > 0, também "estoque_parado". Com estoque <= 0, a estagnação é "possivel_ruptura" e vai
         para a Atenção;
     (b) margem negativa recorrente: no último ano, ao menos `relatorios_minimos_margem_negativa`
         relatórios com custo, `fracao_relatorios_negativos` deles com margem negativa e o mais recente
         negativo (com um único relatório no último ano, o anual: o ano negativo), com o prejuízo do
         último ano entre os que somam `pareto_prejuizo`, ao menos `prejuizo_minimo_do_produto` da venda
         com custo do produto no último ano e `impacto_minimo_do_lead` do faturamento do último ano.
  Estoque parado (ResultadoTriagem.estagnados): todo produto com estagnação crítica e estoque > 0,
  em qualquer bloco; fora da Urgência, o motivo "estoque_parado" aparece quando ele cai na Eletiva só
  pela estagnação.
  2. Atenção, qualquer um: sem custo; vendas abaixo do custo (perda >= `abaixo_custo_minimo` da venda
     com custo); margem em queda (>= `queda_de_margem` entre os dois últimos anos com custo); margem
     baixa relativa (< `margem_baixa` e `distancia_margem_baixa` abaixo da margem da categoria, ou do lead
     sem categoria); vazamento de preço (>= `vazamento_relevante` do faturamento); custo parcial; custo
     implausível; conflito de cadastro; possível ruptura.
  3. Eletiva, qualquer um: ritmo em queda (>= `queda_moderada`); giro baixo (<= `giro_baixo`); cauda
     longa (na cauda de `corte_cauda_longa` do faturamento do último ano, com giro baixo e venda em ano
     anterior); margem abaixo do mix (>= `margem_baixa` e <= `fator_margem_mix` x a margem da categoria
     ou do lead, com participação >= `participacao_minima_mix`); estoque negativo; sem NCM; sem
     categoria; potencial recuperável menor (>= `recuperavel_residual_minimo` do faturamento). A venda que
     parou sem recuperável alto entra com o motivo "venda_parou" quando não tem nenhum outro motivo da
     Eletiva; com algum deles, entra com esses motivos.

Tudo em fração exata (Fraction) nas fronteiras. Os limiares padrão são parâmetros iniciais, para o dono
ajustar; o lead grava os usados (Limiares.para_dict). Os resumos dos blocos levam só contagens e
totais, nunca o nome de um produto: o resumo fica no lead depois do expurgo das linhas (30 dias).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
import re

VERSAO_TRIAGEM = "triagem-2026-11.3"
BLOCOS = ("urgencia", "atencao", "eletiva")

_ZERO = Decimal("0")
_QUATRO_CASAS = Decimal("0.0001")
# Categoria com menos itens com custo que isto não serve de referência de margem: vale a do lead.
ITENS_MINIMOS_DA_CATEGORIA = 3
# Limiar gravado: até 6 dígitos inteiros e 6 decimais. Milhares de dígitos deixariam cada visita
# à tela do lead dezenas de vezes mais lenta (a triagem compara em fração exata).
_RE_DECIMAL_SIMPLES = re.compile(r"\d{1,6}(?:\.\d{1,6})?")
_MAIOR_INTEIRO_DE_LIMIAR = 10 ** 6

# Frações (de 0 a 1), quantidades em unidades (>= 0), razões (> 1) e inteiros (>= 1) dos limiares.
_FRACOES = frozenset({
    "pareto_recuperavel", "recuperavel_minimo_do_produto", "impacto_minimo_do_lead", "queda_critica",
    "giro_critico", "pareto_prejuizo", "prejuizo_minimo_do_produto", "fracao_relatorios_negativos",
    "margem_baixa", "distancia_margem_baixa", "queda_de_margem", "vazamento_relevante", "abaixo_custo_minimo",
    "queda_moderada", "giro_baixo", "corte_cauda_longa", "fator_margem_mix", "participacao_minima_mix",
    "recuperavel_residual_minimo",
})
_QUANTIDADES = frozenset({"venda_esperada_minima"})
_RAZOES = frozenset({"razao_custo_implausivel"})
_INTEIROS = frozenset({"relatorios_para_giro_critico", "relatorios_minimos_margem_negativa"})


@dataclass(frozen=True)
class Limiares:
    """Os limiares da triagem, configuráveis por parâmetro (triar(..., limiares=Limiares(...))).

    Decimal nos valores (nunca float: Decimal(0.1) não é 0,1) e int nas contagens; a validação
    recusa tipo errado, fração fora de [0, 1] e relação incoerente (queda moderada acima da crítica,
    giro crítico acima do baixo)."""
    # Urgência (a): potencial recuperável alto e estagnação.
    pareto_recuperavel: Decimal = Decimal("0.80")
    recuperavel_minimo_do_produto: Decimal = Decimal("0.03")
    impacto_minimo_do_lead: Decimal = Decimal("0.001")
    queda_critica: Decimal = Decimal("0.50")
    giro_critico: Decimal = Decimal("0.25")
    relatorios_para_giro_critico: int = 4
    venda_esperada_minima: Decimal = Decimal("1")
    # Urgência (b): margem negativa recorrente.
    pareto_prejuizo: Decimal = Decimal("0.80")
    prejuizo_minimo_do_produto: Decimal = Decimal("0.03")
    relatorios_minimos_margem_negativa: int = 3
    fracao_relatorios_negativos: Decimal = Decimal("0.50")
    # Atenção.
    margem_baixa: Decimal = Decimal("0.10")
    distancia_margem_baixa: Decimal = Decimal("0.05")
    queda_de_margem: Decimal = Decimal("0.05")
    vazamento_relevante: Decimal = Decimal("0.03")
    abaixo_custo_minimo: Decimal = Decimal("0.01")
    razao_custo_implausivel: Decimal = Decimal("3")
    # Eletiva.
    queda_moderada: Decimal = Decimal("0.20")
    giro_baixo: Decimal = Decimal("0.50")
    corte_cauda_longa: Decimal = Decimal("0.95")
    fator_margem_mix: Decimal = Decimal("0.60")
    participacao_minima_mix: Decimal = Decimal("0.01")
    recuperavel_residual_minimo: Decimal = Decimal("0.01")

    def __post_init__(self):
        for campo in fields(self):
            valor = getattr(self, campo.name)
            if campo.name in _INTEIROS:
                if type(valor) is not int or not 1 <= valor <= _MAIOR_INTEIRO_DE_LIMIAR:
                    raise ValueError(f"Limiar {campo.name} precisa ser um inteiro de 1 a {_MAIOR_INTEIRO_DE_LIMIAR}.")
                continue
            if type(valor) is not Decimal or not valor.is_finite():
                raise ValueError(f"Limiar {campo.name} precisa ser um Decimal finito.")
            if not _RE_DECIMAL_SIMPLES.fullmatch(format(valor, "f")):
                raise ValueError(f"Limiar {campo.name} precisa ter até 6 dígitos inteiros e 6 decimais.")
            if campo.name in _FRACOES and not _ZERO <= valor <= 1:
                raise ValueError(f"Limiar {campo.name} precisa estar entre 0 e 1.")
            if campo.name in _QUANTIDADES and valor < 0:
                raise ValueError(f"Limiar {campo.name} não pode ser negativo.")
            if campo.name in _RAZOES and valor <= 1:
                raise ValueError(f"Limiar {campo.name} precisa ser maior que 1.")
        if self.queda_moderada > self.queda_critica or self.giro_critico > self.giro_baixo:
            raise ValueError("Limiares incoerentes: a queda moderada passa da crítica ou o giro crítico passa do baixo.")

    def para_dict(self) -> dict:
        """Os limiares para gravar no lead: Decimal como texto, inteiro como int, com a versão."""
        dados = {"versao": VERSAO_TRIAGEM}
        for campo in fields(self):
            valor = getattr(self, campo.name)
            dados[campo.name] = valor if campo.name in _INTEIROS else str(valor)
        return dados

    def com_ajustes(self, ajustes) -> "Limiares":
        """Estes limiares com alguns trocados ({nome: valor}, Decimal como texto simples e inteiro como
        int, como em para_dict). Nome desconhecido ou valor ilegível: ValueError."""
        nomes = {campo.name for campo in fields(self)}
        if not isinstance(ajustes, dict) or not set(ajustes) <= nomes:
            raise ValueError("Ajuste de limiar desconhecido.")
        base = self.para_dict()
        base.update({nome: valor for nome, valor in ajustes.items()})
        return type(self).de_dict(base)

    @classmethod
    def de_dict(cls, dados) -> "Limiares":
        """O contrário de para_dict, estrito: chaves exatas, Decimal como texto simples (sem NaN,
        infinito nem expoente) e inteiro como int. Qualquer defeito vira ValueError; limiares de outra
        versão da triagem, LimiaresDeOutraVersao (o chamador decide se usa os padrão atuais)."""
        nomes = {campo.name for campo in fields(cls)}
        if not isinstance(dados, dict) or "versao" not in dados:
            raise ValueError("Limiares gravados ilegíveis.")
        if dados["versao"] != VERSAO_TRIAGEM:
            raise LimiaresDeOutraVersao(str(dados["versao"])[:40])
        if set(dados) != nomes | {"versao"}:
            raise ValueError("Limiares gravados ilegíveis.")
        valores = {}
        for nome in nomes:
            valor = dados[nome]
            if nome in _INTEIROS:
                if type(valor) is not int:
                    raise ValueError(f"Limiar {nome} ilegível.")
                valores[nome] = valor
                continue
            if not isinstance(valor, str) or not _RE_DECIMAL_SIMPLES.fullmatch(valor):
                raise ValueError(f"Limiar {nome} ilegível.")
            try:
                valores[nome] = Decimal(valor)
            except InvalidOperation:
                raise ValueError(f"Limiar {nome} ilegível.") from None
        return cls(**valores)


class LimiaresDeOutraVersao(ValueError):
    """Limiares gravados por outra versão da triagem: o significado de cada um pode ter mudado."""


LIMIARES_PADRAO = Limiares()

ROTULOS_DOS_BLOCOS = {"urgencia": "Urgência", "atencao": "Atenção", "eletiva": "Eletiva"}
# Rótulo de cada motivo; "{nome}" é trocado pelo limiar usado (em %, ou o número).
ROTULOS_DOS_MOTIVOS = {
    # Urgência
    "recuperavel_alto": "Potencial recuperável alto",
    "estoque_parado": "Venda parada ou em queda com estoque",
    "venda_parou": "Sem venda no período do último ano",
    "venda_em_queda_forte": "Ritmo de venda caiu {queda_critica} ou mais",
    "giro_critico": "Vendeu em até {giro_critico} dos relatórios do último ano",
    "margem_negativa_recorrente": "Venda abaixo do custo recorrente: confirme se é promoção",
    # Atenção
    "sem_custo": "Sem custo em nenhuma venda do ano",
    "abaixo_do_custo": "Vendas abaixo do custo ({abaixo_custo_minimo} ou mais da venda com custo)",
    "margem_em_queda": "Margem caiu {queda_de_margem} ou mais",
    "margem_baixa_relativa": "Margem abaixo de {margem_baixa} e abaixo da categoria",
    "vazamento_de_preco": "Venda abaixo do preço de referência",
    "custo_parcial": "Custo informado só em parte das vendas",
    "custo_implausivel": "Custo implausível: confira a unidade ou a embalagem no cadastro",
    "conflito_de_cadastro": "Código ou nome em conflito no cadastro",
    "possivel_ruptura": "Venda parou ou caiu sem estoque: possível ruptura",
    # Eletiva
    "venda_em_queda": "Ritmo de venda caiu {queda_moderada} ou mais",
    "giro_baixo": "Vendeu em até {giro_baixo} dos relatórios do último ano",
    "cauda_longa": "Cauda longa com giro baixo",
    "margem_abaixo_do_mix": "Margem bem abaixo da categoria (mix)",
    "estoque_negativo": "Estoque negativo no PDV",
    "sem_ncm": "Sem NCM no cadastro",
    "sem_categoria": "Sem categoria no cadastro",
    "recuperavel_menor": "Potencial recuperável menor",
}
# Motivos de versões anteriores da triagem (os resumos de leads antigos ficam no banco): o código
# não muda de sentido; quando a regra mudou, o código novo é outro.
ROTULOS_HISTORICOS = {
    "venda_sem_custo": "Venda sem custo cadastrado",
    "venda_abaixo_do_custo": "Preço médio abaixo do custo médio",
    "perda_abaixo_do_custo": "Vendas abaixo do custo",
    "margem_baixa": "Margem abaixo de 10%",
    "recuperavel_residual": "Valor recuperável residual",
}
ROTULOS_DAS_OMISSOES = {
    "conflito_de_cadastro": "conflito de código e nome",
    "custo_parcial": "custo só em parte das vendas",
    "custo_implausivel": "custo implausível",
    "estoque_negativo": "estoque negativo (entrada de compra não lançada)",
    "sem_ncm": "sem NCM",
    "sem_categoria": "sem categoria",
}


def _percentual(valor: Decimal) -> str:
    texto = f"{valor * 100:.2f}".rstrip("0").rstrip(".")
    return texto.replace(".", ",") + "%"


def rotulo_do_motivo(codigo: str, limiares: Limiares | None = None) -> str:
    """O texto do motivo para a tela, com os limiares usados; motivo antigo, com o rótulo da época."""
    modelo = ROTULOS_DOS_MOTIVOS.get(codigo)
    if modelo is None:
        return ROTULOS_HISTORICOS.get(codigo, codigo)
    limiares = limiares or LIMIARES_PADRAO
    valores = {campo.name: (_percentual(getattr(limiares, campo.name)) if campo.name in _FRACOES
                            else str(getattr(limiares, campo.name))) for campo in fields(limiares)}
    return modelo.format(**valores)


def _valor(linha: dict, nome: str, padrao):
    """Coluna da staging; linha montada à mão (testes, versão anterior) cai no padrão neutro."""
    valor = linha.get(nome)
    return padrao if valor is None else valor


def marcas(linha: dict, limiares: Limiares | None = None, *, conflito_confiavel: bool = True) -> dict:
    """As marcas gravadas na linha (produto e ano) da staging."""
    limiares = limiares or LIMIARES_PADRAO
    com_custo, custo, quantidade = (linha["faturamento_com_custo"], linha["custo_total"],
                                    linha["quantidade_com_custo"])
    custo_medio = None
    if quantidade > 0 and custo > 0:
        custo_medio = (custo / quantidade).quantize(_QUATRO_CASAS, rounding=ROUND_HALF_UP)
    venda_com_coluna = _valor(linha, "faturamento_em_relatorio_com_custo", linha["valor_venda_bruto"])
    return {
        "flag_venda_sem_custo": venda_com_coluna > 0 and custo == 0,
        "flag_venda_abaixo_custo": linha["perda"] > 0,
        "flag_omissao_erp": bool(motivos_de_omissao(linha, limiares, conflito_confiavel=conflito_confiavel)),
        "vazamento_estimado_reais": linha["recuperavel"],
        "custo_medio_registrado": custo_medio,
    }


def custo_implausivel(linha: dict, limiares: Limiares | None = None) -> bool:
    """O custo do produto no ano é várias vezes a venda com custo (erro de unidade ou embalagem)."""
    limiares = limiares or LIMIARES_PADRAO
    com_custo = linha["faturamento_com_custo"]
    return com_custo > 0 and linha["custo_total"] >= limiares.razao_custo_implausivel * com_custo


def motivos_de_omissao(linha: dict, limiares: Limiares | None = None, *, conflito_confiavel: bool = True) -> tuple:
    """Por que a linha marca omissão no ERP (códigos de ROTULOS_DAS_OMISSOES), em ordem fixa."""
    venda_com_coluna = _valor(linha, "faturamento_em_relatorio_com_custo", linha["valor_venda_bruto"])
    motivos = []
    if conflito_confiavel and linha["conflito_cadastral"]:
        motivos.append("conflito_de_cadastro")
    if 0 < linha["faturamento_com_custo"] < venda_com_coluna:
        motivos.append("custo_parcial")
    if custo_implausivel(linha, limiares):
        motivos.append("custo_implausivel")
    if linha["estoque_negativo"]:
        motivos.append("estoque_negativo")
    if _valor(linha, "ncm_ausente", False):
        motivos.append("sem_ncm")
    if _valor(linha, "categoria_ausente", False):
        motivos.append("sem_categoria")
    return tuple(motivos)


@dataclass(frozen=True)
class Cobertura:
    """O que o lead enviou de cada ano: a base do ritmo de venda, do giro e de cada verificação.

    As contagens da Etapa 2 (relatórios com custo, NCM, categoria e estoque, os completos e os dias
    dos mesmos meses) ficam nulas quando não vieram (cobertura montada à mão): aí cada verificação
    vale como se o relatório tivesse a coluna, e a comparação de ritmo usa o ano inteiro."""
    relatorios: dict   # ano -> relatórios enviados
    dias: dict         # ano -> dias cobertos pelos períodos dos relatórios
    mensais: dict      # ano -> todos os relatórios do ano cabem num mesmo mês
    com_custo: dict | None = None
    com_ncm: dict | None = None
    com_categoria: dict | None = None
    com_estoque: dict | None = None
    completos: dict | None = None
    dias_mesmos_meses: dict | None = None
    cabecalho_ambiguo: int = 0

    @classmethod
    def do_envio(cls, meta: dict) -> "Cobertura":
        """A cobertura gravada na meta do envio (avaliacao_motor._meta). ValueError se faltar alguma
        contagem, se elas não tiverem os mesmos anos ou se um valor não fechar."""
        def por_ano(chave):
            return {int(ano): valor for ano, valor in meta[chave].items()}
        try:
            relatorios, dias = por_ano("arquivos_por_ano"), por_ano("dias_por_ano")
            mensais = por_ano("relatorios_mensais_por_ano")
            extras = {campo: por_ano(chave) for campo, chave in (
                ("com_custo", "relatorios_com_custo_por_ano"), ("com_ncm", "relatorios_com_ncm_por_ano"),
                ("com_categoria", "relatorios_com_categoria_por_ano"), ("com_estoque", "relatorios_com_estoque_por_ano"),
                ("completos", "relatorios_completos_por_ano"), ("dias_mesmos_meses", "dias_mesmos_meses_por_ano"))}
            ambiguo = meta["relatorios_cabecalho_ambiguo"]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("Cobertura do envio ausente ou inválida.") from None
        anos = set(relatorios)
        if (not relatorios or set(dias) != anos or set(mensais) != anos
                or any(set(valores) != anos for valores in extras.values())
                or any(type(n) is not int or n < 1 for n in relatorios.values())
                or any(type(n) is not int or not 1 <= n <= 366 for n in dias.values())
                or any(type(m) is not bool for m in mensais.values())
                or any(type(n) is not int or n < 0 for valores in extras.values() for n in valores.values())
                or any(extras[c][ano] > relatorios[ano] for c in extras if c != "dias_mesmos_meses" for ano in anos)
                or any(extras["dias_mesmos_meses"][ano] > dias[ano] for ano in anos)
                or type(ambiguo) is not int or ambiguo < 0):
            raise ValueError("Cobertura do envio ausente ou inválida.")
        return cls(relatorios, dias, mensais, cabecalho_ambiguo=ambiguo, **extras)

    def tem(self, contagem: dict | None, ano: int) -> bool:
        """Algum relatório do ano trouxe a coluna (cobertura sem a contagem: sim)."""
        return contagem is None or contagem.get(ano, 0) > 0


@dataclass(frozen=True)
class ResultadoTriagem:
    blocos: dict              # chave do produto -> "urgencia", "atencao", "eletiva" ou None
    motivos: dict             # chave do produto -> motivos (tupla de códigos)
    resumos: dict             # bloco -> resumo do bloco (só contagens e totais)
    itens_venda_sem_custo: int
    itens_venda_abaixo_custo: int
    itens_omissao_erp: int = 0
    limiares: Limiares = LIMIARES_PADRAO
    omissoes: dict | None = None      # chave do produto -> motivos de omissão no ERP (todos os anos)
    a_confirmar: frozenset = frozenset()  # produtos com custo implausível (fora dos Paretos e do impacto)
    margem_referencia: dict | None = None  # chave do produto -> margem da categoria ou do lead (Fraction)
    cauda: frozenset = frozenset()    # produtos na cauda do faturamento do último ano
    estagnados: frozenset = frozenset()  # produtos com estagnação crítica e estoque > 0 (estoque parado)
    sem_mes_em_comum: bool = False    # dois últimos anos mensais sem mês em comum: ritmo não comparado


def _pareto(valores: dict, corte: Decimal) -> set:
    """Produtos que, do maior para o menor, somam os primeiros `corte` do total (desempate pela chave)."""
    total = sum(valores.values(), _ZERO)
    if total <= 0:
        return set()
    escolhidos, acumulado = set(), _ZERO
    for chave, valor in sorted(valores.items(), key=lambda item: (-item[1], item[0])):
        if valor <= 0 or acumulado >= corte * total:
            break
        escolhidos.add(chave)
        acumulado += valor
    return escolhidos


def _cauda(valores: dict, corte: Decimal) -> set:
    """Produtos depois do acumulado `corte` do total, do maior para o menor (a Classe C da curva)."""
    total = sum(valores.values(), _ZERO)
    if total <= 0:
        return set()
    cauda, acumulado = set(), _ZERO
    for chave, valor in sorted(valores.items(), key=lambda item: (-item[1], item[0])):
        if acumulado >= corte * total:
            cauda.add(chave)
        acumulado += valor
    return cauda


def _ritmo(por_ano: dict, cobertura: Cobertura, ultimo: int, limiares: Limiares):
    """(queda do ritmo de venda até o último ano ou None, giro do último ano ou None, vendeu no
    último ano, relatórios da base do giro). O ritmo é a venda por dia coberto pelos relatórios. A
    queda compara com o ano anterior mais recente com venda e só existe quando o ritmo dele prevê ao
    menos `venda_esperada_minima` no período comparado do último ano:
      - os dois últimos anos do envio mensais (e o anterior é o do produto): só os meses em comum (os
        completos do último ano, sem o mês corrente parcial); sem mês em comum, não há comparação;
      - senão (ano anterior não mensal, ou o produto não vendeu no ano anterior do envio): os períodos
        inteiros.
    O giro (relatórios com venda / relatórios do ano) só existe quando os relatórios do último ano são
    mensais; o de um lançamento (sem venda nos anos anteriores) conta só os relatórios desde a primeira
    venda dele.

    Tudo em fração exata (Fraction): a queda de exatamente 50% e a venda esperada de exatamente
    1 unidade contam também em meses de 28, 30 ou 31 dias, sem o arredondamento de uma divisão."""
    vendeu = ultimo in por_ano and por_ano[ultimo]["quantidade"] > 0
    queda = None
    anteriores = [ano for ano in por_ano if ano < ultimo and por_ano[ano]["quantidade"] > 0]
    if anteriores:
        anterior = max(anteriores)
        anterior_do_envio = max((ano for ano in cobertura.relatorios if ano < ultimo), default=None)
        janela = cobertura.dias_mesmos_meses
        comparacao = None
        if (janela is not None and anterior == anterior_do_envio and cobertura.mensais.get(ultimo)
                and cobertura.mensais.get(anterior)):
            if janela.get(anterior, 0) > 0 and janela.get(ultimo, 0) > 0:
                atual = Fraction(_valor(por_ano[ultimo], "quantidade_mesmos_meses", _ZERO)) if ultimo in por_ano else 0
                comparacao = (Fraction(_valor(por_ano[anterior], "quantidade_mesmos_meses", _ZERO)) / janela[anterior],
                              Fraction(atual) / janela[ultimo], janela[ultimo])
        else:
            atual = Fraction(por_ano[ultimo]["quantidade"]) if vendeu else Fraction(0)
            comparacao = (Fraction(por_ano[anterior]["quantidade"]) / cobertura.dias[anterior],
                          atual / cobertura.dias[ultimo], cobertura.dias[ultimo])
        if comparacao is not None:
            base, atual, dias = comparacao
            if base > 0 and base * dias >= Fraction(limiares.venda_esperada_minima):
                queda = 1 - atual / base
    giro, relatorios_do_giro = None, cobertura.relatorios[ultimo]
    if vendeu and cobertura.mensais[ultimo]:
        if not anteriores:
            relatorios_do_giro = _valor(por_ano[ultimo], "relatorios_desde_primeira_venda", 0) or relatorios_do_giro
        giro = Fraction(por_ano[ultimo]["meses"], relatorios_do_giro)
    return queda, giro, vendeu, relatorios_do_giro


def _margem(fat_com_custo, custo):
    # Fração exata, como o ritmo: margem de exatamente 10% ou queda de exatamente 5 pontos contam.
    return Fraction(fat_com_custo - custo) / Fraction(fat_com_custo) if fat_com_custo > 0 else None


def _categoria(por_ano: dict):
    """A categoria do produto: a do ano mais recente que tiver uma (chave normalizada)."""
    for ano in sorted(por_ano, reverse=True):
        nome = por_ano[ano].get("categoria_produto")
        if isinstance(nome, str) and nome.strip():
            return chave_da_categoria(nome)
    return None


def chave_da_categoria(nome: str) -> str:
    """Agrupa "BEBIDAS", "Bebidas" e "BEBIDAS " numa categoria só."""
    import unicodedata
    texto = unicodedata.normalize("NFKD", nome)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.casefold().split())


def _estoque(por_ano: dict):
    """O último estoque informado do produto (do ano mais recente que tem), ou None."""
    for ano in sorted(por_ano, reverse=True):
        estoque = por_ano[ano].get("estoque_final")
        if estoque is not None:
            return estoque
    return None


def triar(linhas: list, cobertura: Cobertura, limiares: Limiares | None = None) -> ResultadoTriagem:
    """Blocos e motivos de cada produto, e os resumos do lead. `cobertura`: o que o lead mandou de
    cada ano (Cobertura.do_envio da meta do envio); `limiares`: os da triagem (padrão: LIMIARES_PADRAO)."""
    limiares = limiares or LIMIARES_PADRAO
    if not isinstance(limiares, Limiares):
        raise ValueError("Limiares inválidos.")
    ultimo = max(cobertura.relatorios)
    produtos: dict = {}
    for linha in linhas:
        ano = int(linha["ano_exercicio"])
        if ano not in cobertura.relatorios:
            raise ValueError("Há dados de um ano que não está na cobertura do envio.")
        produtos.setdefault(linha["chave_produto"], {})[ano] = linha
    conflito_confiavel = not cobertura.cabecalho_ambiguo

    # Totais do lead e referências (sem os itens de custo implausível).
    implausiveis = frozenset(chave for chave, anos in produtos.items()
                             if any(custo_implausivel(l, limiares) for l in anos.values()))
    recuperavel = {chave: sum((l["recuperavel"] for l in anos.values()), _ZERO) for chave, anos in produtos.items()}
    pareto_recuperavel = _pareto({c: v for c, v in recuperavel.items() if c not in implausiveis},
                                 limiares.pareto_recuperavel)
    faturamento_lead = sum((l["valor_venda_bruto"] for anos in produtos.values() for l in anos.values()), _ZERO)
    venda_ultimo = {chave: anos[ultimo]["valor_venda_bruto"] for chave, anos in produtos.items() if ultimo in anos}
    faturamento_lead_ultimo = sum(venda_ultimo.values(), _ZERO)
    prejuizo_ultimo = {chave: max(anos[ultimo]["custo_total"] - anos[ultimo]["faturamento_com_custo"], _ZERO)
                       for chave, anos in produtos.items()
                       if ultimo in anos and anos[ultimo]["faturamento_com_custo"] > 0 and chave not in implausiveis}
    pareto_prejuizo = _pareto(prejuizo_ultimo, limiares.pareto_prejuizo)
    cauda = _cauda(venda_ultimo, limiares.corte_cauda_longa)
    # Margem de referência: a da categoria (com ao menos ITENS_MINIMOS_DA_CATEGORIA itens com custo) ou a
    # do lead, sempre sem os itens de custo implausível.
    margem_por_categoria: dict = {}
    soma_lead = [_ZERO, _ZERO]
    for chave, anos in produtos.items():
        if chave in implausiveis:
            continue
        com_custo = sum((l["faturamento_com_custo"] for l in anos.values()), _ZERO)
        custo = sum((l["custo_total"] for l in anos.values()), _ZERO)
        if com_custo <= 0:
            continue
        soma_lead[0] += com_custo
        soma_lead[1] += custo
        categoria = _categoria(anos)
        if categoria is not None:
            soma = margem_por_categoria.setdefault(categoria, [_ZERO, _ZERO, 0])
            soma[0] += com_custo
            soma[1] += custo
            soma[2] += 1
    margem_lead = _margem(soma_lead[0], soma_lead[1])
    referencia = {categoria: _margem(soma[0], soma[1]) for categoria, soma in margem_por_categoria.items()
                  if soma[2] >= ITENS_MINIMOS_DA_CATEGORIA}

    blocos, motivos, omissoes, referencias = {}, {}, {}, {}
    estagnados: set = set()
    sem_custo = abaixo_custo = com_omissao = 0
    for chave, por_ano in produtos.items():
        anos = sorted(por_ano)
        fat = sum((por_ano[a]["valor_venda_bruto"] for a in anos), _ZERO)
        rec = recuperavel[chave]
        vazamento = sum((por_ano[a]["vazamento_preco"] for a in anos), _ZERO)
        perda = sum((por_ano[a]["perda"] for a in anos), _ZERO)
        com_custo = sum((por_ano[a]["faturamento_com_custo"] for a in anos), _ZERO)
        custo = sum((por_ano[a]["custo_total"] for a in anos), _ZERO)
        plausivel = chave not in implausiveis
        omissao = []
        for a in anos:
            for motivo in motivos_de_omissao(por_ano[a], limiares, conflito_confiavel=conflito_confiavel):
                if motivo not in omissao:
                    omissao.append(motivo)
        omissoes[chave] = tuple(omissao)
        # O motivo e a contagem do lead usam a mesma regra: sem custo em algum ano (o custo que sumiu no
        # último ano é o mais acionável). Com um ano só, é o saude.itens_sem_custo do Raio-X nos
        # relatórios com a coluna de custo; a venda abaixo do custo é o auditoria.itens_venda_abaixo_custo.
        teve_sem_custo = any(marcas(por_ano[a], limiares)["flag_venda_sem_custo"] for a in anos)
        sem_custo += teve_sem_custo
        abaixo_custo += perda > 0
        com_omissao += bool(omissao)

        queda, giro, vendeu, relatorios_do_giro = _ritmo(por_ano, cobertura, ultimo, limiares)
        estagnacao = []
        if not vendeu:
            # Sem venda no último ano: só conta quando o ritmo anterior previa venda no período.
            if queda is not None:
                estagnacao.append("venda_parou")
        elif queda is not None and queda >= Fraction(limiares.queda_critica):
            estagnacao.append("venda_em_queda_forte")
        if (giro is not None and relatorios_do_giro >= limiares.relatorios_para_giro_critico
                and giro <= Fraction(limiares.giro_critico)):
            estagnacao.append("giro_critico")
        estoque = _estoque(por_ano)
        ruptura = bool(estagnacao) and estoque is not None and estoque <= 0
        parado = bool(estagnacao) and estoque is not None and estoque > 0
        if parado:
            estagnados.add(chave)
        recuperavel_alto = (plausivel and chave in pareto_recuperavel and fat > 0
                            and rec >= limiares.recuperavel_minimo_do_produto * fat
                            and rec >= limiares.impacto_minimo_do_lead * faturamento_lead)

        urgencia = []
        if recuperavel_alto and estagnacao and not ruptura:
            urgencia = ["recuperavel_alto", *(["estoque_parado"] if parado else []), *estagnacao]
        linha_ultima = por_ano.get(ultimo)
        if linha_ultima is not None and chave in pareto_prejuizo:
            com_custo_ultimo = linha_ultima["faturamento_com_custo"]
            relatorios_com_custo = _valor(linha_ultima, "meses_com_custo", 0)
            negativos = _valor(linha_ultima, "meses_margem_negativa", 0)
            if cobertura.relatorios[ultimo] > 1:
                # Contagem dos relatórios com custo, mensais ou não (um semestre e meses avulsos também).
                recorrente = (relatorios_com_custo >= limiares.relatorios_minimos_margem_negativa
                              and Fraction(negativos, relatorios_com_custo) >= Fraction(limiares.fracao_relatorios_negativos)
                              and bool(_valor(linha_ultima, "ultimo_mes_negativo", False)))
            else:
                # Um único relatório no último ano (o anual): o ano inteiro negativo.
                recorrente = com_custo_ultimo > 0 and linha_ultima["custo_total"] > com_custo_ultimo
            prejuizo = prejuizo_ultimo.get(chave, _ZERO)
            if (recorrente and prejuizo > 0 and prejuizo >= limiares.prejuizo_minimo_do_produto * com_custo_ultimo
                    and prejuizo >= limiares.impacto_minimo_do_lead * faturamento_lead_ultimo):
                urgencia.append("margem_negativa_recorrente")

        atencao = []
        if teve_sem_custo:
            atencao.append("sem_custo")
        if perda > 0 and com_custo > 0 and perda >= limiares.abaixo_custo_minimo * com_custo:
            atencao.append("abaixo_do_custo")
        margem = _margem(com_custo, custo) if plausivel else None
        anos_com_custo = [a for a in anos if por_ano[a]["faturamento_com_custo"] > 0]
        if plausivel and len(anos_com_custo) >= 2:
            anterior, recente = anos_com_custo[-2], anos_com_custo[-1]
            queda_margem = (_margem(por_ano[anterior]["faturamento_com_custo"], por_ano[anterior]["custo_total"])
                            - _margem(por_ano[recente]["faturamento_com_custo"], por_ano[recente]["custo_total"]))
            if queda_margem >= Fraction(limiares.queda_de_margem):
                atencao.append("margem_em_queda")
        margem_ref = referencia.get(_categoria(por_ano), margem_lead)
        referencias[chave] = margem_ref
        if (margem is not None and margem < Fraction(limiares.margem_baixa) and margem_ref is not None
                and margem <= margem_ref - Fraction(limiares.distancia_margem_baixa)):
            atencao.append("margem_baixa_relativa")
        if vazamento > 0 and fat > 0 and vazamento >= limiares.vazamento_relevante * fat:
            atencao.append("vazamento_de_preco")
        for motivo in ("custo_parcial", "custo_implausivel", "conflito_de_cadastro"):
            if motivo in omissao:
                atencao.append(motivo)
        if ruptura:
            atencao.append("possivel_ruptura")

        eletiva = []
        if vendeu and queda is not None and queda >= Fraction(limiares.queda_moderada):
            eletiva.append("venda_em_queda")
        giro_baixo = giro is not None and giro <= Fraction(limiares.giro_baixo)
        if giro_baixo:
            eletiva.append("giro_baixo")
        if chave in cauda and giro_baixo and any(a < ultimo for a in anos):
            eletiva.append("cauda_longa")
        participacao = (venda_ultimo.get(chave, _ZERO) / faturamento_lead_ultimo) if faturamento_lead_ultimo > 0 else _ZERO
        if (margem is not None and margem_ref is not None and margem_ref > 0 and margem >= Fraction(limiares.margem_baixa)
                and margem <= Fraction(limiares.fator_margem_mix) * margem_ref
                and participacao >= limiares.participacao_minima_mix):
            eletiva.append("margem_abaixo_do_mix")
        for motivo in ("estoque_negativo", "sem_ncm", "sem_categoria"):
            if motivo in omissao:
                eletiva.append(motivo)
        if rec > 0 and fat > 0 and rec >= limiares.recuperavel_residual_minimo * fat:
            eletiva.append("recuperavel_menor")

        if urgencia:
            blocos[chave], motivos[chave] = "urgencia", tuple(urgencia)
        elif atencao:
            blocos[chave], motivos[chave] = "atencao", tuple(atencao)
        elif eletiva or estagnacao:
            blocos[chave], motivos[chave] = "eletiva", tuple(eletiva or [*(["estoque_parado"] if parado else []),
                                                                         *estagnacao])
        else:
            blocos[chave], motivos[chave] = None, ()

    resumos = {}
    for bloco in BLOCOS:
        chaves = [chave for chave, escolhido in blocos.items() if escolhido == bloco]
        contagem = Counter(motivo for chave in chaves for motivo in motivos[chave])
        resumos[bloco] = {
            "versao": VERSAO_TRIAGEM,
            "itens": len(chaves),
            "faturamento": str(sum((l["valor_venda_bruto"] for c in chaves for l in produtos[c].values()), _ZERO)),
            "recuperavel": str(sum((recuperavel[c] for c in chaves), _ZERO)),
            "motivos": dict(sorted(contagem.items())),
        }
    anterior_do_envio = max((ano for ano in cobertura.relatorios if ano < ultimo), default=None)
    sem_mes_em_comum = bool(
        anterior_do_envio is not None and cobertura.dias_mesmos_meses is not None
        and cobertura.mensais.get(ultimo) and cobertura.mensais.get(anterior_do_envio)
        and not (cobertura.dias_mesmos_meses.get(ultimo, 0) and cobertura.dias_mesmos_meses.get(anterior_do_envio, 0)))
    return ResultadoTriagem(blocos, motivos, resumos, sem_custo, abaixo_custo, com_omissao, limiares,
                            omissoes, implausiveis, referencias, frozenset(cauda), frozenset(estagnados),
                            sem_mes_em_comum)
