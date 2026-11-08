"""Triagem da Avaliação de Cenário: Urgência, Atenção e Eletiva, e as marcas por produto e ano.

Função pura (sem banco, Flask nem config), no processo pai, sobre as linhas da staging
(brainstorming_dados_pdv) já quantizadas como foram gravadas: refazê-la dá o mesmo resultado, e
`flask leads retriar` a refaz durante a retenção de 30 dias, sem novo envio dos relatórios.

Marcas, por produto e ano (marcas()):
  - venda sem custo: parte do faturamento do ano saiu sem custo informado no PDV (produto vendido
    sem cadastro ou registro do custo de aquisição);
  - venda abaixo do custo: o preço médio das vendas com custo ficou abaixo do custo médio
    registrado (faturamento com custo menor que o custo total);
  - omissão no ERP: ainda sem regra; fica falsa (a variação de preço fica na coluna `variacao`);
  - vazamento estimado: o valor recuperável do produto no ano (o mesmo do Raio-X: o maior entre a
    venda abaixo do preço de referência e a venda abaixo do custo, por linha);
  - custo médio registrado: custo total / quantidade vendida com custo, com 4 casas.

Ritmo e giro (Cobertura, da meta do envio): os anos podem ter durações diferentes (um ano inteiro
e o semestre corrente, um relatório anual e outros mensais), então o ritmo é a venda média por
DIA coberto pelos períodos dos relatórios, e não por relatório. A queda compara o último ano com
o ano anterior mais recente com venda, e só vale quando o ritmo anterior prevê ao menos
VENDA_ESPERADA_MINIMA unidade no período do último ano: sem isso, não vender num mês curto não diz
nada de um item de giro lento. O giro (parte dos relatórios do último ano com venda) só existe
quando todos os relatórios do último ano são mensais.

Blocos, por produto e olhando todos os anos (triar()); cada produto fica em no máximo um, nesta
ordem:
  1. Urgência: valor recuperável alto e travado (o produto está entre os que somam 80% do
     recuperável do lead, e o recuperável dele é ao menos 3% do próprio faturamento) E curva de
     estagnação crítica (o ritmo de venda caiu ao menos 50% do ano anterior com venda para o
     último ano, ou zerou; ou o produto vendeu em até 25% dos relatórios mensais do último ano,
     quando ele tem ao menos 4).
  2. Atenção: margem degradada (margem abaixo de 10%, queda de ao menos 5 pontos entre os dois
     últimos anos com custo, ou vazamento de preço de ao menos 3% do faturamento) ou divergência
     de custo (venda sem custo, venda abaixo do custo, perda abaixo do custo ou conflito de
     cadastro entre código e nome).
  3. Eletiva: oportunidade secundária de giro (ritmo em queda de ao menos 20%, venda em até
     metade dos relatórios mensais do último ano ou estoque negativo no PDV) ou recuperável
     residual. A estagnação crítica sem recuperável alto também fica aqui: o produto que parou de
     vender no último ano (e cujo ritmo anterior previa venda) entra com o motivo "venda_parou"
     quando não tem nenhum outro motivo da Eletiva; com algum deles, entra com esses motivos.

Os limiares são as constantes abaixo: parâmetros iniciais, para o dono ajustar. A versão
(VERSAO_TRIAGEM) vai para o lead, e os resumos dos blocos levam só contagens e totais, nunca o
nome de um produto: o resumo fica no lead depois do expurgo das linhas (TTL de 30 dias).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

VERSAO_TRIAGEM = "triagem-2026-11.2"
BLOCOS = ("urgencia", "atencao", "eletiva")

PARETO_RECUPERAVEL = Decimal("0.80")
RECUPERAVEL_MINIMO_DO_PRODUTO = Decimal("0.03")
QUEDA_CRITICA = Decimal("0.50")
GIRO_CRITICO = Decimal("0.25")
RELATORIOS_PARA_GIRO_CRITICO = 4
MARGEM_BAIXA = Decimal("0.10")
QUEDA_DE_MARGEM = Decimal("0.05")
VAZAMENTO_RELEVANTE = Decimal("0.03")
QUEDA_MODERADA = Decimal("0.20")
GIRO_BAIXO = Decimal("0.50")
VENDA_ESPERADA_MINIMA = Decimal("1")

_ZERO = Decimal("0")
_QUATRO_CASAS = Decimal("0.0001")

ROTULOS_DOS_BLOCOS = {"urgencia": "Urgência", "atencao": "Atenção", "eletiva": "Eletiva"}
ROTULOS_DOS_MOTIVOS = {
    "recuperavel_alto": "Valor recuperável alto",
    "venda_parou": "Sem venda no período do último ano",
    "venda_em_queda_forte": "Ritmo de venda caiu 50% ou mais",
    "giro_critico": "Vendeu em até 25% dos relatórios do último ano",
    "margem_baixa": "Margem abaixo de 10%",
    "margem_em_queda": "Margem caiu 5 pontos ou mais",
    "vazamento_de_preco": "Venda abaixo do preço de referência",
    "venda_sem_custo": "Venda sem custo cadastrado",
    "venda_abaixo_do_custo": "Preço médio abaixo do custo médio",
    "perda_abaixo_do_custo": "Vendas abaixo do custo",
    "conflito_de_cadastro": "Código ou nome em conflito no cadastro",
    "venda_em_queda": "Ritmo de venda caiu 20% ou mais",
    "giro_baixo": "Vendeu em até metade dos relatórios do último ano",
    "estoque_negativo": "Estoque negativo no PDV",
    "recuperavel_residual": "Valor recuperável residual",
}


def marcas(linha: dict) -> dict:
    """Marcas e valores derivados de uma linha (produto e ano) da staging."""
    com_custo, custo, quantidade = (linha["faturamento_com_custo"], linha["custo_total"],
                                    linha["quantidade_com_custo"])
    custo_medio = None
    if quantidade > 0 and custo > 0:
        custo_medio = (custo / quantidade).quantize(_QUATRO_CASAS, rounding=ROUND_HALF_UP)
    return {
        "flag_venda_sem_custo": linha["valor_venda_bruto"] > com_custo,
        "flag_venda_abaixo_custo": quantidade > 0 and com_custo < custo,
        "flag_omissao_erp": False,
        "vazamento_estimado_reais": linha["recuperavel"],
        "custo_medio_registrado": custo_medio,
    }


@dataclass(frozen=True)
class Cobertura:
    """O que o lead enviou de cada ano: a base do ritmo de venda e do giro."""
    relatorios: dict   # ano -> relatórios enviados
    dias: dict         # ano -> dias cobertos pelos períodos dos relatórios
    mensais: dict      # ano -> todos os relatórios do ano cabem num mesmo mês

    @classmethod
    def do_envio(cls, meta: dict) -> "Cobertura":
        """A cobertura gravada na meta do envio (avaliacao_motor._meta). ValueError se faltar
        alguma das três contagens, se elas não tiverem os mesmos anos ou se um valor não fechar."""
        try:
            relatorios = {int(ano): quantos for ano, quantos in meta["arquivos_por_ano"].items()}
            dias = {int(ano): quantos for ano, quantos in meta["dias_por_ano"].items()}
            mensais = {int(ano): mensal for ano, mensal in meta["relatorios_mensais_por_ano"].items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("Cobertura do envio ausente ou inválida.") from None
        if (not relatorios or set(relatorios) != set(dias) or set(relatorios) != set(mensais)
                or any(type(n) is not int or n < 1 for n in relatorios.values())
                or any(type(n) is not int or not 1 <= n <= 366 for n in dias.values())
                or any(type(m) is not bool for m in mensais.values())):
            raise ValueError("Cobertura do envio ausente ou inválida.")
        return cls(relatorios, dias, mensais)


@dataclass(frozen=True)
class ResultadoTriagem:
    blocos: dict              # chave do produto -> "urgencia", "atencao", "eletiva" ou None
    motivos: dict             # chave do produto -> motivos (tupla de códigos)
    resumos: dict             # bloco -> resumo do bloco (só contagens e totais)
    itens_venda_sem_custo: int
    itens_venda_abaixo_custo: int


def _pareto_do_recuperavel(recuperavel: dict) -> set:
    """Produtos que, do maior para o menor, somam os primeiros 80% do recuperável do lead."""
    total = sum(recuperavel.values(), _ZERO)
    if total <= 0:
        return set()
    escolhidos, acumulado = set(), _ZERO
    for chave, valor in sorted(recuperavel.items(), key=lambda item: (-item[1], item[0])):
        if valor <= 0 or acumulado >= PARETO_RECUPERAVEL * total:
            break
        escolhidos.add(chave)
        acumulado += valor
    return escolhidos


def _ritmo(por_ano: dict, cobertura: Cobertura, ultimo: int):
    """(queda do ritmo de venda até o último ano ou None, giro do último ano ou None, vendeu no
    último ano). O ritmo é a venda por dia coberto pelos relatórios do ano. A queda compara com o
    ano anterior mais recente com venda e só existe quando o ritmo dele prevê ao menos
    VENDA_ESPERADA_MINIMA no período do último ano. O giro (relatórios com venda / relatórios do
    ano) só existe quando os relatórios do último ano são mensais.

    Tudo em fração exata (Fraction): a queda de exatamente 50% e a venda esperada de exatamente
    1 unidade contam também em meses de 28, 30 ou 31 dias, sem o arredondamento de uma divisão."""
    def por_dia(ano):
        return Fraction(por_ano[ano]["quantidade"]) / cobertura.dias[ano] if ano in por_ano else Fraction(0)

    vendeu = ultimo in por_ano
    queda = None
    anteriores = [ano for ano in por_ano if ano < ultimo]
    if anteriores:
        base = por_dia(max(anteriores))
        if base > 0 and base * cobertura.dias[ultimo] >= Fraction(VENDA_ESPERADA_MINIMA):
            queda = 1 - por_dia(ultimo) / base
    giro = None
    if vendeu and cobertura.mensais[ultimo]:
        giro = Fraction(por_ano[ultimo]["meses"], cobertura.relatorios[ultimo])
    return queda, giro, vendeu


def _margem(fat_com_custo, custo):
    # Fração exata, como o ritmo: margem de exatamente 10% ou queda de exatamente 5 pontos contam.
    return Fraction(fat_com_custo - custo) / Fraction(fat_com_custo) if fat_com_custo > 0 else None


def triar(linhas: list, cobertura: Cobertura) -> ResultadoTriagem:
    """Blocos e motivos de cada produto, e os resumos do lead. `cobertura`: o que o lead mandou de
    cada ano (Cobertura.do_envio da meta do envio), a base do ritmo e do giro."""
    ultimo = max(cobertura.relatorios)
    produtos: dict = {}
    for linha in linhas:
        ano = int(linha["ano_exercicio"])
        if ano not in cobertura.relatorios:
            raise ValueError("Há dados de um ano que não está na cobertura do envio.")
        produtos.setdefault(linha["chave_produto"], {})[ano] = linha
    recuperavel = {chave: sum((l["recuperavel"] for l in anos.values()), _ZERO) for chave, anos in produtos.items()}
    pareto = _pareto_do_recuperavel(recuperavel)

    blocos, motivos = {}, {}
    sem_custo = abaixo_custo = 0
    for chave, por_ano in produtos.items():
        anos = sorted(por_ano)
        fat = sum((por_ano[a]["valor_venda_bruto"] for a in anos), _ZERO)
        rec = recuperavel[chave]
        vazamento = sum((por_ano[a]["vazamento_preco"] for a in anos), _ZERO)
        perda = sum((por_ano[a]["perda"] for a in anos), _ZERO)
        com_custo = sum((por_ano[a]["faturamento_com_custo"] for a in anos), _ZERO)
        custo = sum((por_ano[a]["custo_total"] for a in anos), _ZERO)
        marcas_dos_anos = [marcas(por_ano[a]) for a in anos]
        teve_sem_custo = any(m["flag_venda_sem_custo"] for m in marcas_dos_anos)
        teve_abaixo = any(m["flag_venda_abaixo_custo"] for m in marcas_dos_anos)
        sem_custo += teve_sem_custo
        abaixo_custo += teve_abaixo

        queda, giro, vendeu = _ritmo(por_ano, cobertura, ultimo)
        estagnacao = []
        if not vendeu:
            # Sem venda no último ano: só conta quando o ritmo anterior previa venda no período.
            if queda is not None:
                estagnacao.append("venda_parou")
        elif queda is not None and queda >= Fraction(QUEDA_CRITICA):
            estagnacao.append("venda_em_queda_forte")
        if (giro is not None and cobertura.relatorios[ultimo] >= RELATORIOS_PARA_GIRO_CRITICO
                and giro <= Fraction(GIRO_CRITICO)):
            estagnacao.append("giro_critico")
        recuperavel_alto = chave in pareto and fat > 0 and rec >= RECUPERAVEL_MINIMO_DO_PRODUTO * fat

        atencao = []
        margem = _margem(com_custo, custo)
        if margem is not None and margem < Fraction(MARGEM_BAIXA):
            atencao.append("margem_baixa")
        anos_com_custo = [a for a in anos if por_ano[a]["faturamento_com_custo"] > 0]
        if len(anos_com_custo) >= 2:
            anterior, recente = anos_com_custo[-2], anos_com_custo[-1]
            queda_margem = (_margem(por_ano[anterior]["faturamento_com_custo"], por_ano[anterior]["custo_total"])
                            - _margem(por_ano[recente]["faturamento_com_custo"], por_ano[recente]["custo_total"]))
            if queda_margem >= Fraction(QUEDA_DE_MARGEM):
                atencao.append("margem_em_queda")
        if vazamento > 0 and fat > 0 and vazamento >= VAZAMENTO_RELEVANTE * fat:
            atencao.append("vazamento_de_preco")
        if teve_sem_custo:
            atencao.append("venda_sem_custo")
        if teve_abaixo:
            atencao.append("venda_abaixo_do_custo")
        if perda > 0:
            atencao.append("perda_abaixo_do_custo")
        if any(por_ano[a]["conflito_cadastral"] for a in anos):
            atencao.append("conflito_de_cadastro")

        eletiva = []
        if vendeu and queda is not None and queda >= Fraction(QUEDA_MODERADA):
            eletiva.append("venda_em_queda")
        if giro is not None and giro <= Fraction(GIRO_BAIXO):
            eletiva.append("giro_baixo")
        if any(por_ano[a]["estoque_negativo"] for a in anos):
            eletiva.append("estoque_negativo")
        if rec > 0:
            eletiva.append("recuperavel_residual")

        if recuperavel_alto and estagnacao:
            blocos[chave], motivos[chave] = "urgencia", tuple(["recuperavel_alto", *estagnacao])
        elif atencao:
            blocos[chave], motivos[chave] = "atencao", tuple(atencao)
        elif eletiva or estagnacao:
            blocos[chave], motivos[chave] = "eletiva", tuple(eletiva or estagnacao)
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
    return ResultadoTriagem(blocos, motivos, resumos, sem_custo, abaixo_custo)
