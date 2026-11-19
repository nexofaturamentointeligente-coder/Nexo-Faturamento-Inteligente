"""Evolução & Conquistas mensais (Etapa 3, 14/11/2026): o retrato de cada Raio-X e a comparação com o Marco Zero.

O Raio-X do PDV da empresa é um só (diagnostico_entrada: o novo substitui o anterior). Para comparar os meses, cada
Raio-X gravado deixa um retrato, só com agregados (nenhum nome de produto), em diagnostico_fechamento: um por empresa e
mês do Raio-X, no horário de Brasília (o Raio-X refeito no mesmo mês substitui o retrato daquele mês, também no mês do
Marco Zero). O fechamento mensal é o Raio-X feito com o relatório do mês (todo dia 1º); o Marco Zero, o primeiro
retrato.

  - retrato(): os números de um Raio-X por mês. O ritmo é o total dividido pelos meses cobertos, sem o piso de 1 mês
    do Plano de Ação (lá ele protege a projeção de um Raio-X só; aqui poria 14 dias contra 30 como se fossem iguais):
    a perda abaixo do custo, pelos meses com a coluna de custo, como na Auditoria; o resto, pelos meses analisados.
  - comparar(): o Marco Zero, o último fechamento e o anterior a ele; os 5 indicadores, a economia mensal conquistada
    (a queda do dinheiro recuperável por mês, a "sangria" do Plano de Ação) e a projeção de 12 meses, os selos, o
    resumo, o foco do próximo mês e a linha do tempo com as metas do plano original.

Regras de comparação:
  - o número decide como a tela o mostra: percentual com uma casa, reais em centavos (melhorou, piorou, selos);
  - perda, itens e sobra bruta só se comparam quando o último fechamento mede a perda na mesma venda que a base, ou em
    mais venda: com mais venda sem custo (ou um relatório sem a coluna de custo no último fechamento), a perda escondida
    viraria "economia"; a base sem nenhuma venda com custo não mediu perda, e a conta vale;
  - as afirmações de zero ("Sanado", "nenhuma venda saiu sem custo") usam o número gravado: 0,04% aparece como "menos
    de 0,1%";
  - o estado inicial, que não compara, mostra o dinheiro por mês como a página do Raio-X e o Plano de Ação (abaixo de 1
    mês, o total); a comparação usa o ritmo real, com o lado estimado marcado;
  - um Raio-X com as regras de antes da raio-x-2026-11.5 (o custo implausível ainda dentro da perda) não vira Marco
    Zero de um Raio-X novo (raio_x_servico).

Este módulo não toca o banco (raio_x_servico grava e lê os retratos). Contas em Decimal com precisão de sobra, sem NaN,
infinito nem divisão por zero; com um retrato só, o estado inicial. O que o plano não libera (a sobra bruta, sem
margem_bruta) não entra na comparação.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
import math
import re

from horario import BRASILIA, instante_utc
import plano_acao
import raio_x_regras
import raio_x_apresentacao

VERSAO = "evolucao-2026-11.1"
# O retrato guarda os números de todos os planos (raio_x_servico.visao com TUDO): quem mudar de plano vê a evolução
# inteira, e a comparação esconde o que o plano atual não libera.
TUDO = frozenset({"margem_bruta", "auditoria_abaixo_custo", "diagnostico_cmv", "alerta_precificacao"})
MESES_DA_PROJECAO = 12
# Selos (regras do dono): venda sem custo abaixo de 2% do faturamento ou com queda de mais de 50% desde o Marco Zero;
# vazamento por variação de preço com queda de mais de 30% desde o Marco Zero.
LIMITE_CADASTRO_BLINDADO = Decimal("0.02")
QUEDA_CADASTRO_BLINDADO = Decimal("0.5")
QUEDA_BALCAO_TRAVADO = Decimal("0.3")
# A venda sem custo pode variar até 1 ponto entre dois Raio-X sem mudar a base em que a perda é medida.
TOLERANCIA_SEM_CUSTO = Decimal("0.01")
# Desde a raio-x-2026-11.5 (unificação do motor), o custo implausível sai da perda e do recuperável.
REGRAS_MINIMAS = (2026, 11, 5)

MENSAGEM_INICIAL = ("A comparação de evolução será ativada automaticamente no próximo fechamento mensal (todo dia 1º): "
                    "é só enviar o relatório de vendas do mês no Raio-X do PDV.")

STATUS = {
    "sanado": "Sanado",
    "evolucao": "Em evolução",
    "pendente": "Pendente",
    "partida": "Ponto de partida",
    "sem_dados": "Sem dados",
}

_AVISO_SEM_PERIODO = {
    "sem_datas": "vendas sem as datas do período",
    "periodo_futuro": "vendas com período depois da data do Raio-X",
    "sem_datas_e_futuro": "vendas sem as datas do período ou com período depois da data do Raio-X",
}
_PRECISAO = 400
_CENTAVO = Decimal("0.01")
# Nenhum valor de loja chega perto: acima disso, o número gravado é lixo (e as contas sairiam do alcance do float).
_LIMITE = 10 ** 15
# Venda sem custo que existe em reais e some no percentual gravado (4 casas): maior que zero, menor que qualquer
# número que a tela mostra.
_FRACAO_MINIMA = Decimal("0.00001")
_MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
          "novembro", "dezembro")


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------
def _decimal(valor) -> Decimal | None:
    """O número gravado em Decimal; None sem número (texto, booleano, NaN, infinito ou fora da escala de uma loja)."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    if isinstance(valor, float) and not math.isfinite(valor):
        return None
    if abs(valor) > _LIMITE:
        return None
    return Decimal(str(valor))


def _numero(valor) -> float | None:
    """O número para o JSON do retrato (float finito) ou None."""
    numero = _decimal(valor)
    return float(numero) if numero is not None else None


def _inteiro(valor) -> int | None:
    """Uma contagem gravada (inteiro não negativo, na escala de uma loja) ou None."""
    return valor if isinstance(valor, int) and not isinstance(valor, bool) and 0 <= valor <= _LIMITE else None


def _reais(valor: Decimal) -> float:
    with localcontext() as contexto:
        contexto.prec = _PRECISAO
        return float(valor.quantize(_CENTAVO, rounding=ROUND_HALF_UP))


def _dicionario(valor) -> dict:
    return valor if isinstance(valor, dict) else {}


def _data(texto) -> date | None:
    if not isinstance(texto, str):
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def _brl(valor) -> str:
    """R$ sem o símbolo, como a tela: 1.234,56."""
    return raio_x_regras._brl(float(valor))


def _pct(fracao) -> str:
    """0,123 -> "12,3%" (uma casa, sem a casa quando inteiro), como a tela."""
    return raio_x_apresentacao._pct(float(fracao))


def _fracao_exibida(valor) -> Decimal | None:
    """A fração como a tela a mostra (o percentual com uma casa, raio_x_apresentacao._pct): é ela que decide melhorou,
    piorou e os selos, para o painel nunca dizer "melhorou" de 6,1% para 6,1%."""
    numero = _decimal(valor)
    if numero is None:
        return None
    try:
        exibido = round(float(numero) * 100, 1)
    except (OverflowError, ValueError):
        return None
    return Decimal(str(exibido)) / 100 if math.isfinite(exibido) else None


def _plural(quantidade: int, singular: str, plural: str) -> str:
    return f"{quantidade} {singular if quantidade == 1 else plural}"


def _juntar(partes: list) -> str:
    """["a", "b", "c"] -> "a, b e c"."""
    if len(partes) <= 1:
        return "".join(partes)
    return ", ".join(partes[:-1]) + " e " + partes[-1]


def competencia(gerado_em: datetime) -> date:
    """O mês do retrato: o mês do Raio-X no horário de Brasília (dia 1). O fechamento de cada mês é o Raio-X feito
    nele; o Raio-X refeito no mesmo mês substitui o retrato daquele mês."""
    dia = instante_utc(gerado_em).astimezone(BRASILIA).date()
    return date(dia.year, dia.month, 1)


def rotulo_do_mes(mes: date) -> str:
    """date(2026, 11, 1) -> "novembro/2026"."""
    return f"{_MESES[mes.month - 1]}/{mes.year}"


def regras_comparaveis(versao) -> bool:
    """O Raio-X foi gravado com regras que a comparação aceita (raio-x-2026-11.5 em diante)."""
    achado = re.fullmatch(r"raio-x-(\d{4})-(\d{2})\.(\d+)", versao) if isinstance(versao, str) else None
    return bool(achado) and tuple(int(parte) for parte in achado.groups()) >= REGRAS_MINIMAS


# ---------------------------------------------------------------------------
# Retrato de um Raio-X
# ---------------------------------------------------------------------------
def _ritmo_real(valor, meses: Decimal | None) -> float | None:
    """O total dividido pelos meses cobertos, sem o piso de 1 mês (o retrato de meio mês compara com o de um mês
    cheio pelo ritmo de verdade). None sem número ou sem os meses."""
    total = _decimal(valor)
    if total is None or meses is None or meses <= 0:
        return None
    with localcontext() as contexto:
        contexto.prec = _PRECISAO
        return _reais(total / meses)


def retrato(completo: dict, bruto: dict, gerado_em: datetime) -> dict:
    """Os números de um Raio-X para a evolução. `completo`: o resultado gravado pela raio_x_servico.visao com TUDO
    (os campos calculados, nada escondido, sem corte de histórico); `bruto`: o resultado gravado; `gerado_em`: o
    instante do Raio-X (com fuso). Só agregados: nenhum nome de produto sai daqui.

    "mensal" é o ritmo por mês (None sem período: relatório sem datas ou com período depois da data do Raio-X);
    "total", o acumulado do período."""
    ate = instante_utc(gerado_em).astimezone(BRASILIA).date()
    completo, bruto = _dicionario(completo), _dicionario(bruto)
    pa = plano_acao.preparar(completo, bruto, ate)
    tempo, auditado = pa["horizonte"], pa["horizonte_auditado"]
    projetavel = bool(tempo["projetavel"])
    meses = tempo["meses"] if projetavel else None
    meses_auditados = auditado["meses"] if projetavel else None
    saude, scanner = _dicionario(completo.get("saude")), _dicionario(completo.get("scanner"))
    auditoria, recuperavel = _dicionario(completo.get("auditoria")), _dicionario(completo.get("recuperavel"))
    versao = bruto.get("versao")
    return {
        "versao": VERSAO,
        "versao_regras": versao if isinstance(versao, str) else None,
        "competencia": competencia(gerado_em).isoformat(),
        "gerado_em": instante_utc(gerado_em).isoformat(),
        "data": ate.isoformat(),
        "periodo": {
            "inicio": tempo["inicio"].isoformat() if tempo["inicio"] else None,
            "fim": tempo["fim"].isoformat() if tempo["fim"] else None,
            "texto": tempo["texto"],
            "aviso": None if projetavel else _AVISO_SEM_PERIODO.get(tempo["motivo"], _AVISO_SEM_PERIODO["sem_datas"]),
            "meses": float(meses) if meses is not None else None,
            "meses_auditados": float(meses_auditados) if meses_auditados is not None else None,
            "projetavel": projetavel,
        },
        "mensal": {
            "recuperavel": _ritmo_real(recuperavel.get("total"), meses),
            "perda": _ritmo_real(auditoria.get("perda_abaixo_custo"), meses_auditados),
            "vazamento": _ritmo_real(scanner.get("vazamento_preco"), meses),
            "faturamento": _ritmo_real(saude.get("faturamento_total"), meses),
        },
        "total": {
            "recuperavel": _numero(recuperavel.get("total")),
            "perda": _numero(auditoria.get("perda_abaixo_custo")),
            "vazamento": _numero(scanner.get("vazamento_preco")),
            "faturamento": _numero(saude.get("faturamento_total")),
            "venda_com_custo": _numero(auditoria.get("faturamento_com_custo")),
            # Em reais: o percentual gravado pelo motor tem 4 casas, e abaixo de 0,005% do faturamento ele vira zero.
            "venda_sem_custo": _numero(saude.get("faturamento_sem_custo")),
        },
        "pct_sem_custo": _numero(saude.get("pct_faturamento_sem_custo")),
        "itens": {
            "margem_negativa": _inteiro(auditoria.get("itens_margem_negativa_sem_a_confirmar")),
            "custo_a_confirmar": _inteiro(_dicionario(auditoria.get("custo_a_confirmar")).get("itens")) or 0,
            "sem_custo": _inteiro(saude.get("itens_sem_custo")),
            "vendidos": _inteiro(saude.get("itens_vendidos")),
            "estoque_negativo": _inteiro(_dicionario(saude.get("inconsistencias")).get("estoque_negativo")),
        },
        "margem_ajustada": _numero(auditoria.get("margem_sem_a_confirmar_percentual")),
        "variacao_visivel": scanner.get("variacao_visivel") is True,
        "arquivos_com_custo": _inteiro(saude.get("arquivos_com_custo")) or 0,
        "arquivos_sem_coluna": plano_acao.relatorios_sem_coluna_de_custo(completo.get("avisos")),
        "estoque": plano_acao.tem_estoque(bruto),
        "etapas": {etapa["id"]: etapa["estado"] for etapa in pa["plano"]["etapas"]},
    }


def valido(retrato_gravado) -> bool:
    """O retrato tem o mínimo para entrar na comparação (o JSON gravado vem do próprio retrato(), mas é lido de novo
    a cada tela: um retrato estranho fica de fora, sem derrubar a página)."""
    return (isinstance(retrato_gravado, dict)
            and isinstance(retrato_gravado.get("versao"), str) and retrato_gravado["versao"].startswith("evolucao-")
            and _data(retrato_gravado.get("competencia")) is not None
            and _data(retrato_gravado.get("data")) is not None
            and isinstance(retrato_gravado.get("gerado_em"), str))


# ---------------------------------------------------------------------------
# Os valores de cada retrato
# ---------------------------------------------------------------------------
def _tem_venda_com_custo(r: dict) -> bool:
    venda = _decimal(_dicionario(r.get("total")).get("venda_com_custo"))
    return bool(_inteiro(r.get("arquivos_com_custo"))) and venda is not None and venda > 0


def _por_mes(r: dict, chave: str, plano: bool = False) -> Decimal | None:
    """O ritmo por mês gravado; sem ritmo (período desconhecido), o total zero ainda é zero por mês. `plano`: como a
    página do Raio-X, a Auditoria e o Plano de Ação o mostram, com o piso deles de 1 mês (abaixo dele, o total)."""
    mensal = _decimal(_dicionario(r.get("mensal")).get(chave))
    total = _decimal(_dicionario(r.get("total")).get(chave))
    if plano and mensal is not None and total is not None:
        meses = _decimal(_dicionario(r.get("periodo")).get("meses_auditados" if chave == "perda" else "meses"))
        if meses is not None and meses < 1:
            return total
    if mensal is not None:
        return mensal
    return Decimal(0) if total is not None and total == 0 else None


def _perda(r: dict, plano: bool = False) -> Decimal | None:
    return _por_mes(r, "perda", plano) if _tem_venda_com_custo(r) else None


def _vazamento(r: dict, plano: bool = False) -> Decimal | None:
    return _por_mes(r, "vazamento", plano) if r.get("variacao_visivel") is True else None


def _sem_custo(r: dict) -> Decimal | None:
    """A venda sem custo como a tela a mostra: decide melhorou, piorou e as bordas dos selos."""
    return _fracao_exibida(r.get("pct_sem_custo"))


def _sem_custo_gravado(r: dict) -> Decimal | None:
    """A venda sem custo gravada: decide as afirmações de zero ("Sanado", "nenhuma venda saiu sem custo"). O motor grava
    o percentual com 4 casas: abaixo de 0,005% do faturamento ele vira zero, e a venda sem custo em reais (no retrato
    desde a Etapa 3) diz que ainda há produto a cadastrar, como o Plano de Ação do mesmo Raio-X."""
    gravada = _decimal(r.get("pct_sem_custo"))
    reais = _decimal(_dicionario(r.get("total")).get("venda_sem_custo"))
    if gravada is not None and gravada == 0 and reais is not None and reais > 0:
        return _FRACAO_MINIMA
    return gravada


def _texto_sem_custo(r: dict) -> str:
    """A venda sem custo para leigos: a que a tela mostraria como 0% aparece como "menos de 0,1%"."""
    exibida, gravada = _sem_custo(r), _sem_custo_gravado(r)
    if exibida is None:
        return "—"
    return "menos de 0,1%" if exibida == 0 and gravada is not None and gravada > 0 else _pct(exibida)


def _cadastro(r: dict) -> Decimal | None:
    """Itens com margem negativa (sem os de custo a confirmar) mais os alertas de cadastro (custo a confirmar)."""
    itens = _dicionario(r.get("itens"))
    negativa, alertas = _inteiro(itens.get("margem_negativa")), _inteiro(itens.get("custo_a_confirmar"))
    if not _tem_venda_com_custo(r) or negativa is None or alertas is None:
        return None
    return Decimal(negativa + alertas)


def _alertas(r: dict) -> int | None:
    return _inteiro(_dicionario(r.get("itens")).get("custo_a_confirmar"))


def _margem(r: dict) -> Decimal | None:
    return _fracao_exibida(r.get("margem_ajustada"))


def _recuperavel(r: dict, plano: bool = False) -> Decimal | None:
    return _por_mes(r, "recuperavel", plano)


def _estoque_negativo(r: dict) -> int | None:
    return _inteiro(_dicionario(r.get("itens")).get("estoque_negativo"))


def _parcial(r: dict) -> bool:
    """O retrato cobre menos de 1 mês: o ritmo dele é estimado pelos dias cobertos."""
    meses = _decimal(_dicionario(r.get("periodo")).get("meses"))
    return meses is not None and meses < 1


def _perda_estimada(r: dict) -> bool:
    """O retrato cobre 1 mês ou mais, mas os relatórios com a coluna de custo cobrem menos de 1: a perda por mês (pelos
    meses auditados, como na Auditoria) é estimada pelos dias cobertos."""
    auditados = _decimal(_dicionario(r.get("periodo")).get("meses_auditados"))
    return not _parcial(r) and _tem_venda_com_custo(r) and auditados is not None and auditados < 1


# ---------------------------------------------------------------------------
# A base em que a perda é medida (cobertura de custo)
# ---------------------------------------------------------------------------
def _cobertura(r: dict) -> str:
    """"sem_custo" (nenhuma venda com custo: a perda não é medida), "parcial" (um relatório sem a coluna de custo)
    ou "completa"."""
    if not _tem_venda_com_custo(r):
        return "sem_custo"
    return "parcial" if _inteiro(r.get("arquivos_sem_coluna")) else "completa"


def _comparacao_de_custo(base: dict, atual: dict) -> str:
    """Como a perda do `atual` é medida perto da `base`: "pior" (em menos venda: a venda com custo que sumiu, um
    relatório sem a coluna de custo no `atual` ou mais de 1 ponto a mais de venda sem custo; a perda escondida viraria
    "economia"), "melhor" (em mais venda: perdas que antes não apareciam agora entram na conta) ou "igual".

    A base sem nenhuma venda com custo não mediu perda: qualquer medida depois dela é "melhor". A base com um relatório
    sem a coluna já conta a venda dele como sem custo, e a venda sem custo decide (mais de 1 ponto a mais é "pior")."""
    cb, ca = _cobertura(base), _cobertura(atual)
    if cb == "sem_custo" or ca == "sem_custo":
        if cb == ca:
            return "igual"
        return "melhor" if cb == "sem_custo" else "pior"
    if ca == "parcial":
        return "pior"
    sb, sa = _sem_custo(base), _sem_custo(atual)
    if sb is not None and sa is not None:
        if sa > sb + TOLERANCIA_SEM_CUSTO:
            return "pior"
        if sa < sb - TOLERANCIA_SEM_CUSTO:
            return "melhor"
    return "igual"


def _custo_comparavel(base: dict, atual: dict) -> bool:
    return _comparacao_de_custo(base, atual) != "pior"


def _causa_do_pior(base: dict, atual: dict) -> str | None:
    """Por que a perda do `atual` não se compara com a da `base` (None quando se compara): "sem_coluna" (um relatório
    do `atual` veio sem a coluna de custo e a venda sem custo não subiu mais de 1 ponto: o texto não pode dizer "mais
    venda sem custo"), "sem_custo" (o `atual` não tem venda com custo) ou "mais_sem_custo"."""
    if _comparacao_de_custo(base, atual) != "pior":
        return None
    if _cobertura(atual) == "sem_custo":
        return "sem_custo"
    sb, sa = _sem_custo(base), _sem_custo(atual)
    subiu = sb is not None and sa is not None and sa > sb + TOLERANCIA_SEM_CUSTO
    return "sem_coluna" if _cobertura(atual) == "parcial" and not subiu else "mais_sem_custo"


@dataclass(frozen=True)
class Indicador:
    id: str
    rotulo: str
    unidade: str            # "brl_mes" (R$ por mês), "pct" (fração do faturamento ou da venda) ou "itens"
    menor_e_melhor: bool
    valor: object           # função(retrato) -> Decimal | None
    funcionalidade: str | None = None
    depende_do_custo: bool = False
    texto: object = None    # função(retrato) -> str, quando o texto não é o _formatar do valor


INDICADORES = (
    Indicador("perda", "Perdas nas vendas abaixo do custo", "brl_mes", True, _perda, depende_do_custo=True),
    Indicador("vazamento", "Vazamento por variação de preço", "brl_mes", True, _vazamento),
    Indicador("sem_custo", "Faturamento sem custo informado", "pct", True, _sem_custo, texto=_texto_sem_custo),
    Indicador("cadastro", "Itens com margem negativa e alertas de cadastro", "itens", True, _cadastro,
              depende_do_custo=True),
    Indicador("margem", "Sobra bruta ajustada", "pct", False, _margem, "margem_bruta", depende_do_custo=True),
)


def _situacao(antes: Decimal | None, depois: Decimal | None, menor_e_melhor: bool) -> str:
    if antes is None or depois is None:
        return "sem_dados"
    if depois == antes:
        return "igual"
    melhorou = depois < antes if menor_e_melhor else depois > antes
    return "melhorou" if melhorou else "piorou"


def _variacao(antes: Decimal | None, depois: Decimal | None, unidade: str) -> dict | None:
    """A variação desde `antes`: em % do valor (R$ por mês), em pontos percentuais (fração já arredondada como a
    tela) ou em itens. None sem os dois números; a variação em % sem base (antes zero) fica None, e a tela mostra só
    os valores."""
    if antes is None or depois is None:
        return None
    with localcontext() as contexto:
        contexto.prec = _PRECISAO
        if unidade == "brl_mes":
            if antes == 0:
                return {"tipo": "pct", "valor": None}
            # A razão exata: a tela a arredonda uma vez só (uma casa em %), como a queda dos selos e o resumo. Uma
            # base minúscula (centavo de centavo) daria uma razão sem tamanho: fica sem a variação em %.
            razao = float((depois - antes) / abs(antes))
            return {"tipo": "pct", "valor": razao if math.isfinite(razao) else None}
        if unidade == "pct":
            return {"tipo": "pp", "valor": float(((depois - antes) * 100).quantize(Decimal("0.1"),
                                                                                rounding=ROUND_HALF_UP))}
        return {"tipo": "itens", "valor": int(depois - antes)}


def _texto_da_variacao(variacao: dict | None) -> str | None:
    """A variação para leigos: "75% a menos", "6,1 pontos a menos", "1 item a mais", "sem mudança"."""
    if not variacao or variacao.get("valor") is None:
        return None
    valor = variacao["valor"]
    if valor == 0:
        return "sem mudança"
    lado = "a menos" if valor < 0 else "a mais"
    if variacao["tipo"] == "pct":
        if round(abs(valor) * 100, 1) == 0:
            return f"menos de 0,1% {lado}"
        return f"{_pct(abs(valor))} {lado}"
    if variacao["tipo"] == "pp":
        pontos = f"{abs(valor):.1f}".replace(".", ",").removesuffix(",0")
        return f"{pontos} {'ponto' if abs(valor) == 1 else 'pontos'} {lado}"
    return f"{_plural(abs(int(valor)), 'item', 'itens')} {lado}"


def _formatar(valor: Decimal | None, unidade: str) -> str:
    if valor is None:
        return "—"
    if unidade == "brl_mes":
        return f"R$ {_brl(valor)}/mês"
    if unidade == "pct":
        return _pct(valor)
    return _plural(int(valor), "item", "itens")


def _indicador(indicador: Indicador, marco: dict, anterior: dict, atual: dict) -> dict:
    retratos = {"marco": marco, "anterior": anterior, "atual": atual}
    valores = {nome: indicador.valor(r) for nome, r in retratos.items()}
    comparavel = {nome: not indicador.depende_do_custo or _custo_comparavel(r, atual)
                  for nome, r in (("marco", marco), ("anterior", anterior))}

    def situacao(nome):
        resultado = _situacao(valores[nome], valores["atual"], indicador.menor_e_melhor)
        return resultado if resultado == "sem_dados" or comparavel[nome] else "nao_comparavel"

    variacao_marco = _variacao(valores["marco"], valores["atual"], indicador.unidade) if comparavel["marco"] else None
    variacao_anterior = (_variacao(valores["anterior"], valores["atual"], indicador.unidade)
                         if comparavel["anterior"] else None)
    return {
        "id": indicador.id,
        "rotulo": indicador.rotulo,
        "unidade": indicador.unidade,
        "menor_e_melhor": indicador.menor_e_melhor,
        **{nome: (float(valor) if valor is not None else None) for nome, valor in valores.items()},
        **{f"{nome}_texto": (indicador.texto(retratos[nome]) if indicador.texto else _formatar(valor, indicador.unidade))
           for nome, valor in valores.items()},
        "vs_marco": situacao("marco"),
        "vs_anterior": situacao("anterior"),
        "variacao_marco": variacao_marco,
        "variacao_anterior": variacao_anterior,
        "variacao_marco_texto": _texto_da_variacao(variacao_marco),
        "variacao_anterior_texto": _texto_da_variacao(variacao_anterior),
    }


# ---------------------------------------------------------------------------
# Economia, selos, resumo e foco
# ---------------------------------------------------------------------------
def _economia(marco: dict, atual: dict) -> dict:
    """A economia mensal conquistada: o dinheiro recuperável por mês do Marco Zero menos o do último fechamento (a
    queda da sangria do Plano de Ação); a projeção de 12 meses é ela vezes 12. Negativa: a sangria cresceu.
    "motivo" diz por que não há conta: "cobertura" (o último fechamento mede a perda em menos venda que o Marco
    Zero) ou "periodo" (sem o ritmo dos dois: relatório sem datas ou com período depois da data do Raio-X)."""
    if not _custo_comparavel(marco, atual):
        return {"motivo": "cobertura"}
    antes, depois = _recuperavel(marco), _recuperavel(atual)
    if antes is None or depois is None:
        return {"motivo": "periodo"}
    with localcontext() as contexto:
        contexto.prec = _PRECISAO
        mensal = antes - depois
        anual = mensal * MESES_DA_PROJECAO
    return {"motivo": None, "mensal": _reais(mensal), "anual": _reais(anual), "positiva": mensal > 0,
            "negativa": mensal < 0, "marco_mensal": _reais(antes), "atual_mensal": _reais(depois),
            "mensal_absoluto": _reais(abs(mensal)), "anual_absoluto": _reais(abs(anual))}


def _queda(antes: Decimal, depois: Decimal) -> Decimal:
    """A queda relativa de `antes` para `depois` (antes > 0), como a tela a mostra (uma casa em %)."""
    with localcontext() as contexto:
        contexto.prec = _PRECISAO
        return _fracao_exibida(float((antes - depois) / antes)) or Decimal(0)


def _selo(id_selo: str, emoji: str, titulo: str, regra: str, conquistado: bool, texto: str,
          bloqueado: bool = False) -> dict:
    return {"id": id_selo, "emoji": emoji, "titulo": titulo, "regra": regra, "conquistado": conquistado,
            "texto": texto, "bloqueado": bloqueado}


def _texto_cobertura(base: str) -> str:
    """Por que a perda não se compara; `base` diz com qual fechamento ("no Marco Zero", "no mês anterior")."""
    return (f"mais venda saiu sem custo informado (ou um relatório veio sem a coluna de custo) do que {base}: a perda "
            "pode estar escondida. Cadastre o custo e envie os relatórios com a coluna de custo.")


def _selo_prejuizo_zero(marco: dict, atual: dict) -> dict:
    regra = "Conquistado ao reduzir as vendas abaixo do custo a R$ 0,00."
    perda_total = _decimal(_dicionario(atual.get("total")).get("perda"))
    if not _tem_venda_com_custo(atual) or perda_total is None:
        return _selo("prejuizo_zero", "🏆", "Prejuízo Zero no Caixa", regra, False,
                     "O último fechamento não tem venda com custo informado: envie o relatório com a coluna de custo.")
    # O relatório sem a coluna vem antes: o texto da venda sem custo diria "mais venda sem custo" mesmo com menos.
    if _cobertura(atual) == "parcial":
        return _selo("prejuizo_zero", "🏆", "Prejuízo Zero no Caixa", regra, False,
                     "No último fechamento, um relatório veio sem a coluna de custo: a perda dele não é medida. Envie "
                     "os relatórios com a coluna de custo.")
    if not _custo_comparavel(marco, atual):
        return _selo("prejuizo_zero", "🏆", "Prejuízo Zero no Caixa", regra, False,
                     "No último fechamento, " + _texto_cobertura("no Marco Zero"))
    if perda_total == 0:
        antes = _perda(marco)
        texto = "Nenhuma venda saiu abaixo do custo no último fechamento."
        if antes is not None and antes > 0:
            texto += f" No Marco Zero eram R$ {_brl(antes)} por mês."
        return _selo("prejuizo_zero", "🏆", "Prejuízo Zero no Caixa", regra, True, texto)
    mensal = _perda(atual)
    # O ritmo que arredonda a R$ 0,00 (centavos em vários meses) não aparece como zero: vale o total do período.
    resto = (f"R$ {_brl(mensal)} por mês" if mensal is not None and mensal > 0
             else f"R$ {_brl(perda_total)} no período")
    return _selo("prejuizo_zero", "🏆", "Prejuízo Zero no Caixa", regra, False,
                 f"Ainda há {resto} em vendas abaixo do custo: corrija o preço desses produtos.")


def _selo_cadastro_blindado(marco: dict, atual: dict) -> dict:
    regra = "Vendas sem custo abaixo de 2% do faturamento, ou reduzidas em mais de 50% desde o Marco Zero."
    agora, antes = _sem_custo(atual), _sem_custo(marco)
    if agora is None:
        return _selo("cadastro_blindado", "🛡️", "Cadastro Blindado", regra, False,
                     "O último fechamento não diz quanto da venda saiu sem custo.")
    if agora < LIMITE_CADASTRO_BLINDADO:
        if _sem_custo_gravado(atual) == 0:
            texto = "Nenhuma venda saiu sem custo informado no último fechamento."
        elif agora == 0:
            texto = "Menos de 0,1% da venda saiu sem custo informado no último fechamento."
        else:
            texto = f"Só {_pct(agora)} da venda saiu sem custo informado no último fechamento."
        return _selo("cadastro_blindado", "🛡️", "Cadastro Blindado", regra, True, texto)
    if antes is not None and antes > 0 and _queda(antes, agora) > QUEDA_CADASTRO_BLINDADO:
        return _selo("cadastro_blindado", "🛡️", "Cadastro Blindado", regra, True,
                     f"A venda sem custo informado caiu de {_pct(antes)} para {_pct(agora)} desde o Marco Zero.")
    texto = f"{_pct(agora)} da venda ainda sai sem custo informado: a meta é ficar abaixo de 2%"
    if antes is not None and antes > 0:
        texto += f" ou cair mais da metade desde o Marco Zero ({_pct(antes)})"
    return _selo("cadastro_blindado", "🛡️", "Cadastro Blindado", regra, False, texto + ".")


def _selo_balcao_travado(marco: dict, atual: dict) -> dict:
    regra = "Conquistado quando o vazamento por variação de preço cai mais de 30% desde o Marco Zero."
    agora, antes = _vazamento(atual), _vazamento(marco)
    if agora is None or antes is None:
        return _selo("balcao_travado", "🔒", "Balcão Travado", regra, False,
                     "Os relatórios não mostram o preço de cada venda (ou o período) no Marco Zero ou no último "
                     "fechamento: envie o relatório com o preço unitário e o valor de cada linha.")
    if antes == 0:
        if agora == 0:
            return _selo("balcao_travado", "🔒", "Balcão Travado", regra, True,
                         "Nenhuma venda abaixo do preço normal desde o Marco Zero.")
        return _selo("balcao_travado", "🔒", "Balcão Travado", regra, False,
                     f"Surgiu vazamento de R$ {_brl(agora)} por mês, que não havia no Marco Zero.")
    queda = _queda(antes, agora)
    if queda > QUEDA_BALCAO_TRAVADO:
        return _selo("balcao_travado", "🔒", "Balcão Travado", regra, True,
                     f"O vazamento caiu {_pct(queda)} desde o Marco Zero (de R$ {_brl(antes)} para "
                     f"R$ {_brl(agora)} por mês).")
    return _selo("balcao_travado", "🔒", "Balcão Travado", regra, False,
                 f"O vazamento está em R$ {_brl(agora)} por mês (era R$ {_brl(antes)} no Marco Zero): a meta é cair "
                 "mais de 30%.")


def _selo_margem_em_alta(marco: dict, anterior: dict, atual: dict, liberada: bool, rotulo_anterior: str | None) -> dict:
    regra = "Sobra bruta ajustada acima do mês anterior e do Marco Zero."
    if not liberada:
        return _selo("margem_em_alta", "📈", "Margem em Alta", regra, False, "", bloqueado=True)
    agora, antes, marco_valor = _margem(atual), _margem(anterior), _margem(marco)
    if agora is None or antes is None or marco_valor is None:
        return _selo("margem_em_alta", "📈", "Margem em Alta", regra, False,
                     "Sem venda com custo informado em algum dos fechamentos, não dá para comparar a sobra bruta.")
    if _cobertura(atual) == "parcial":
        return _selo("margem_em_alta", "📈", "Margem em Alta", regra, False,
                     "No último fechamento, um relatório veio sem a coluna de custo: a sobra bruta dele não é medida. "
                     "Envie os relatórios com a coluna de custo.")
    if not _custo_comparavel(marco, atual):
        return _selo("margem_em_alta", "📈", "Margem em Alta", regra, False,
                     "No último fechamento, " + _texto_cobertura("no Marco Zero"))
    if not _custo_comparavel(anterior, atual):
        return _selo("margem_em_alta", "📈", "Margem em Alta", regra, False,
                     "No último fechamento, " + _texto_cobertura(f"no {rotulo_anterior or 'fechamento anterior'}"))
    # Com dois fechamentos, o anterior é o próprio Marco Zero.
    bases = f"{rotulo_anterior}: {_pct(antes)}; Marco Zero: {_pct(marco_valor)}" if rotulo_anterior else \
        f"Marco Zero: {_pct(marco_valor)}"
    if agora > antes and agora > marco_valor:
        return _selo("margem_em_alta", "📈", "Margem em Alta", regra, True,
                     f"A sobra bruta ajustada subiu para {_pct(agora)} ({bases}).")
    meta = "superar os dois" if rotulo_anterior else "superar o Marco Zero"
    return _selo("margem_em_alta", "📈", "Margem em Alta", regra, False,
                 f"A sobra bruta ajustada está em {_pct(agora)} ({bases}): a meta é {meta}.")


def _foco(atual: dict, plano: bool = False) -> str:
    """A maior pendência do último fechamento: o dinheiro (perda ou vazamento por mês, o maior), depois a venda sem
    custo, os alertas de cadastro e o estoque negativo. `plano`: os valores por mês como o Plano de Ação os mostra (o
    estado inicial, que não compara ritmos)."""
    total = _dicionario(atual.get("total"))
    perda, vazamento = _perda(atual, plano), _vazamento(atual, plano)
    # Sem o ritmo por mês (período desconhecido), o total diz se há pendência.
    perda_total = _decimal(total.get("perda")) if _tem_venda_com_custo(atual) else None
    vazamento_total = _decimal(total.get("vazamento")) if atual.get("variacao_visivel") is True else None
    tem_perda = (perda if perda is not None else (perda_total or Decimal(0))) > 0
    tem_vazamento = (vazamento if vazamento is not None else (vazamento_total or Decimal(0))) > 0
    if tem_perda or tem_vazamento:
        if perda is not None and vazamento is not None:
            estancar = perda >= vazamento
        elif tem_perda != tem_vazamento:
            estancar = tem_perda
        else:
            estancar = (perda_total or 0) >= (vazamento_total or 0)
        if estancar:
            return "estancar as vendas abaixo do custo" + (f" (R$ {_brl(perda)} por mês)" if perda is not None
                                                           else "")
        return "travar os descontos no balcão" + (
            f" (R$ {_brl(vazamento)} por mês em vendas abaixo do preço normal)" if vazamento is not None else "")
    sem_custo = _sem_custo_gravado(atual)
    if sem_custo is not None and sem_custo > 0:
        return f"cadastrar o custo dos produtos vendidos sem custo ({_texto_sem_custo(atual)} da venda)"
    alertas = _alertas(atual)
    if alertas:
        return ("corrigir o custo do item com alerta de cadastro" if alertas == 1
                else f"corrigir o custo dos {alertas} itens com alerta de cadastro")
    negativo = _estoque_negativo(atual)
    if negativo:
        return ("regularizar as notas do produto com estoque negativo" if negativo == 1
                else f"regularizar as notas dos {negativo} produtos com estoque negativo")
    return "manter o controle e refazer o Raio-X todo mês"


def _melhora(indicador: dict) -> str | None:
    """A frase do resumo para o indicador que melhorou desde o Marco Zero."""
    variacao = indicador["variacao_marco"] or {}
    valor = variacao.get("valor") if variacao.get("tipo") == "pct" else None
    queda = f" em {_pct(-valor)}" if valor is not None and round(abs(valor) * 100, 1) > 0 else ""
    if indicador["id"] == "vazamento":
        return "zerou os vazamentos de preço" if indicador["atual"] == 0 else f"reduziu os vazamentos de preço{queda}"
    if indicador["id"] == "perda":
        return ("zerou as vendas abaixo do custo" if indicador["atual"] == 0
                else f"reduziu as perdas abaixo do custo{queda}")
    if indicador["id"] == "sem_custo":
        return (f"melhorou a qualidade do cadastro (venda sem custo de {indicador['marco_texto']} para "
                f"{indicador['atual_texto']})")
    if indicador["id"] == "cadastro":
        corrigidos = int(indicador["marco"] - indicador["atual"])
        return f"reduziu em {_plural(corrigidos, 'item', 'itens')} a lista de margem negativa e alertas de cadastro"
    if indicador["id"] == "margem":
        return f"aumentou a sobra bruta ajustada de {indicador['marco_texto']} para {indicador['atual_texto']}"
    return None


def _resumo(marco: dict, indicadores: list, economia: dict, foco: str, causa: str | None = None) -> str:
    data = _data(marco["data"])
    melhoras = [frase for frase in (_melhora(i) for i in indicadores if i["vs_marco"] == "melhorou") if frase]
    pioras = [i["rotulo"].lower() for i in indicadores if i["vs_marco"] == "piorou"]
    frases = [f"Comparado ao seu Raio-X inicial de {data:%d/%m/%Y}, "
              + (f"você {_juntar(melhoras)}." if melhoras else "os indicadores ainda não melhoraram.")]
    if pioras:
        frases.append(f"Atenção: {_juntar(pioras)} {'piorou' if len(pioras) == 1 else 'pioraram'} desde o Marco "
                      "Zero.")
    if economia["motivo"] == "cobertura" and causa == "sem_coluna":
        frases.append("No último fechamento, um relatório veio sem a coluna de custo, e a perda dele não é medida: não "
                      "dá para comparar o dinheiro que escapa por mês.")
    elif economia["motivo"] == "cobertura":
        frases.append("O último fechamento mede a perda em menos venda que o Marco Zero (mais venda sem custo ou um "
                      "relatório sem a coluna de custo): não dá para comparar o dinheiro que escapa por mês.")
    elif economia["motivo"] == "periodo":
        frases.append("Sem o período de algum dos Raio-X, não dá para projetar o ganho de 12 meses.")
    elif economia["positiva"]:
        frases.append(f"Mantendo esse ritmo, seu ganho projetado em 12 meses é de R$ {_brl(economia['anual'])}.")
    elif economia["negativa"]:
        frases.append(f"No ritmo atual, a loja deixa escapar R$ {_brl(economia['anual_absoluto'])} a mais em 12 meses "
                      "do que no Marco Zero.")
    else:
        frases.append("O dinheiro que escapa por mês está no mesmo ritmo do Marco Zero.")
    # "Excelente/Boa evolução" só com alguma melhora de verdade e o dinheiro escapando menos.
    if economia["motivo"] is None and economia["positiva"] and melhoras:
        abertura = "Excelente evolução" if not pioras else "Boa evolução"
        frases.append(f"{abertura} — para o próximo mês, o foco principal é {foco}.")
    else:
        frases.append(f"Para o próximo mês, o foco principal é {foco}.")
    return " ".join(frases)


def _rotulo_anterior(anterior: dict, atual: dict, quantidade: int) -> str | None:
    """Como chamar o penúltimo fechamento: None com dois retratos (é o próprio Marco Zero); "mês anterior" quando é do
    mês imediatamente antes do último; senão, "fechamento de abril/2026"."""
    if quantidade < 3:
        return None
    antes, depois = _data(anterior["competencia"]), _data(atual["competencia"])
    if (depois.year * 12 + depois.month) - (antes.year * 12 + antes.month) == 1:
        return "mês anterior"
    return f"fechamento de {rotulo_do_mes(antes)}"


# ---------------------------------------------------------------------------
# Linha do tempo (as metas do plano original, mês a mês)
# ---------------------------------------------------------------------------
def _status_par(agora: tuple, antes: tuple | None, sanado: bool) -> str:
    """Duas medidas de pendência (menor é melhor): sanado; em evolução (nenhuma pior que no Marco Zero e alguma
    menor); pendente."""
    if sanado:
        return "sanado"
    if antes is None or any(valor is None for valor in antes):
        return "pendente"
    if all(a <= b for a, b in zip(agora, antes)) and any(a < b for a, b in zip(agora, antes)):
        return "evolucao"
    return "pendente"


def _meta_estancar(r: dict, marco: dict) -> str:
    perda, alertas = _perda(r), _alertas(r)
    if perda is None or alertas is None:
        return "sem_dados"
    # Com a perda medida em menos venda que no Marco Zero, nem "Sanado" nem a comparação valem; a linha do próprio
    # Marco Zero é o ponto de partida.
    if r is not marco and not _custo_comparavel(marco, r):
        return "sem_dados"
    # Com um relatório sem a coluna de custo, a perda dele não é medida: o zero não é "Sanado".
    sanado = perda == 0 and alertas == 0 and _cobertura(r) == "completa"
    return _status_par((perda, alertas), (_perda(marco), _alertas(marco)), sanado)


def _meta_travar(r: dict, marco: dict) -> str:
    vazamento = _vazamento(r)
    if vazamento is None:
        return "sem_dados"
    antes = _vazamento(marco)
    return _status_par((vazamento,), (antes,), vazamento == 0)


def _meta_controle(r: dict, marco: dict) -> str:
    sem_custo, negativo = _sem_custo(r), _estoque_negativo(r)
    if sem_custo is None or negativo is None:
        return "sem_dados"
    antes = (_sem_custo(marco), _estoque_negativo(marco))
    # "Sanado" pelo número gravado: 0,04% de venda sem custo aparece como 0%, mas ainda há produto a cadastrar.
    sanado = _sem_custo_gravado(r) == 0 and negativo == 0 and r.get("estoque") is True
    return _status_par((sem_custo, Decimal(negativo)),
                       (antes[0], Decimal(antes[1]) if antes[1] is not None else None), sanado)


METAS = (
    (1, "Estancar prejuízo", "Vendas abaixo do custo e alertas de cadastro", _meta_estancar),
    (2, "Travar descontos", "Vazamento por variação de preço", _meta_travar),
    (3, "Controle", "Venda sem custo e estoque negativo", _meta_controle),
)


def _periodo(r: dict) -> str:
    """"vendas: 1 mês (01/10/2026 a 31/10/2026)" ou, sem período, o motivo ("vendas sem as datas do período")."""
    periodo = _dicionario(r.get("periodo"))
    texto, aviso = periodo.get("texto"), periodo.get("aviso")
    if isinstance(texto, str) and texto:
        return f"vendas: {texto}"
    return aviso if isinstance(aviso, str) else ""


def _cabecalho(r: dict) -> dict:
    mes, dia = _data(r["competencia"]), _data(r["data"])
    recuperavel, do_plano = _recuperavel(r), _recuperavel(r, plano=True)
    return {"competencia": r["competencia"], "mes": rotulo_do_mes(mes), "data": f"{dia:%d/%m/%Y}",
            "periodo": _periodo(r), "parcial": _parcial(r),
            "recuperavel_mensal": float(recuperavel) if recuperavel is not None else None,
            # O número do estado inicial: o mesmo da página do Raio-X e do Plano de Ação.
            "recuperavel_mensal_plano": float(do_plano) if do_plano is not None else None}


def _linha(r: dict, marco: dict, eh_marco: bool) -> dict:
    metas = []
    for numero, titulo, medida, funcao in METAS:
        status = funcao(r, marco)
        if eh_marco and status in ("pendente", "evolucao"):
            status = "partida"
        metas.append({"numero": numero, "titulo": titulo, "medida": medida, "status": status,
                      "rotulo": STATUS[status]})
    perda, vazamento, sem_custo = _perda(r), _vazamento(r), _sem_custo(r)
    return {**_cabecalho(r), "marco_zero": eh_marco,
            "perda_texto": _formatar(perda, "brl_mes"), "vazamento_texto": _formatar(vazamento, "brl_mes"),
            "sem_custo_texto": _formatar(sem_custo, "pct"),
            "recuperavel_texto": _formatar(_recuperavel(r), "brl_mes"), "metas": metas}


def _avisos(marco: dict, atual: dict, comparado: bool) -> list:
    """O que o cliente precisa saber para ler os números: ritmo estimado (retrato de menos de 1 mês, ou com menos de
    1 mês de relatórios com a coluna de custo) e a base de custo diferente entre o Marco Zero e o último fechamento. No
    estado inicial, os números são os do Plano de Ação, e o ritmo estimado vale para a comparação que vem."""
    avisos = []
    if _parcial(marco):
        avisos.append("O Marco Zero cobre menos de 1 mês: " + (
            "o ritmo por mês dele é estimado pelos dias cobertos." if comparado else
            "na comparação com o próximo fechamento, o ritmo por mês dele será estimado pelos dias cobertos."))
    elif _perda_estimada(marco):
        avisos.append("O Marco Zero tem menos de 1 mês de relatórios com a coluna de custo: " + (
            "a perda por mês dele é estimada pelos dias cobertos." if comparado else
            "na comparação com o próximo fechamento, a perda por mês dele será estimada pelos dias cobertos."))
    if comparado and _parcial(atual):
        avisos.append("O último fechamento cobre menos de 1 mês: o ritmo por mês dele é estimado pelos dias cobertos. "
                      "Para comparar meses inteiros, refaça o Raio-X no dia 1º com o relatório do mês que fechou.")
    elif comparado and _perda_estimada(atual):
        avisos.append("O último fechamento tem menos de 1 mês de relatórios com a coluna de custo: a perda por mês dele "
                      "é estimada pelos dias cobertos.")
    if comparado:
        comparacao = _comparacao_de_custo(marco, atual)
        if comparacao == "pior" and _causa_do_pior(marco, atual) == "sem_coluna":
            avisos.append("No último fechamento, um relatório veio sem a coluna de custo: a perda dele não é medida, e "
                          "a perda, os itens e a sobra bruta não são comparados com o Marco Zero. Envie os relatórios "
                          "com a coluna de custo.")
        elif comparacao == "pior":
            avisos.append("O último fechamento tem mais venda sem custo informado (ou um relatório sem a coluna de "
                          "custo) do que o Marco Zero: a perda, os itens e a sobra bruta não são comparados com o "
                          "Marco Zero, para a perda escondida não virar economia.")
        elif comparacao == "melhor":
            avisos.append("O último fechamento tem mais venda com custo informado do que o Marco Zero: perdas que "
                          "antes não apareciam agora entram na conta.")
    return avisos


# ---------------------------------------------------------------------------
# Comparação
# ---------------------------------------------------------------------------
def comparar(retratos, liberadas: frozenset) -> dict | None:
    """O painel Evolução & Conquistas a partir dos retratos da empresa (em qualquer ordem). None sem retrato válido.

    Um retrato só: o estado inicial (o Marco Zero e a mensagem da ativação no próximo fechamento). Dois ou mais: o
    Marco Zero (o primeiro mês), o atual (o último) e o anterior a ele (com dois retratos, o próprio Marco Zero).
    `liberadas`: as funcionalidades do plano atual (a sobra bruta e o selo Margem em Alta pedem margem_bruta)."""
    validos = sorted((r for r in (retratos or ()) if valido(r)), key=lambda r: (r["competencia"], r["gerado_em"]))
    if not validos:
        return None
    # O Raio-X refeito no mesmo mês substitui o retrato: na leitura, também fica um por mês (o mais recente).
    por_mes: dict = {}
    for r in validos:
        por_mes[r["competencia"]] = r
    validos = list(por_mes.values())
    liberada = "margem_bruta" in liberadas
    marco, atual = validos[0], validos[-1]
    linha_do_tempo = [_linha(r, marco, indice == 0) for indice, r in enumerate(validos)]
    linha_do_tempo.reverse()
    base = {"marco": _cabecalho(marco), "atual": _cabecalho(atual), "quantidade": len(validos),
            "linha_do_tempo": linha_do_tempo, "margem_liberada": liberada, "metas": [
                {"numero": numero, "titulo": titulo, "medida": medida} for numero, titulo, medida, _ in METAS]}
    if len(validos) < 2:
        return {**base, "estado": "inicial", "mensagem": MENSAGEM_INICIAL, "foco": _foco(marco, plano=True),
                "avisos": _avisos(marco, marco, False)}
    anterior = validos[-2]
    rotulo_anterior = _rotulo_anterior(anterior, atual, len(validos))
    indicadores = [_indicador(i, marco, anterior, atual) for i in INDICADORES
                   if i.funcionalidade is None or i.funcionalidade in liberadas]
    economia = _economia(marco, atual)
    causa = _causa_do_pior(marco, atual)
    selos = [_selo_prejuizo_zero(marco, atual), _selo_cadastro_blindado(marco, atual),
             _selo_balcao_travado(marco, atual),
             _selo_margem_em_alta(marco, anterior, atual, liberada, rotulo_anterior)]
    foco = _foco(atual)
    # Onde houve regressão: contra o fechamento anterior (com três ou mais) e contra o Marco Zero; um indicador entra
    # uma vez.
    regressoes = []
    for indicador in indicadores:
        if rotulo_anterior and indicador["vs_anterior"] == "piorou":
            regressoes.append({"rotulo": indicador["rotulo"], "contra": "anterior",
                               "contra_texto": f"desde o {rotulo_anterior}",
                               "antes": indicador["anterior_texto"], "depois": indicador["atual_texto"]})
        elif indicador["vs_marco"] == "piorou":
            regressoes.append({"rotulo": indicador["rotulo"], "contra": "marco", "contra_texto": "desde o Marco Zero",
                               "antes": indicador["marco_texto"], "depois": indicador["atual_texto"]})
    return {
        **base,
        "estado": "comparado",
        "anterior": _cabecalho(anterior),
        "rotulo_anterior": rotulo_anterior,
        "indicadores": indicadores,
        "economia": economia,
        # Por que a perda não se compara com o Marco Zero (None quando se compara): escolhe o texto do card.
        "causa_cobertura": causa,
        "regressoes": regressoes,
        "selos": selos,
        "conquistados": sum(1 for selo in selos if selo["conquistado"]),
        "foco": foco,
        "avisos": _avisos(marco, atual, True),
        "resumo": _resumo(marco, indicadores, economia, foco, causa),
    }
