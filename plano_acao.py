"""Plano de Ação (12 meses) e o horizonte de tempo dos números do Raio-X do PDV e da Auditoria de custos
(Etapa 2, pedida pelo dono em 14/11/2026).

Monta, a partir do Raio-X gravado, o que as duas telas mostram além dele:
  - o horizonte de tempo: quantos meses os relatórios cobrem e o ritmo por mês e por ano do dinheiro
    recuperável (card do Raio-X) e do prejuízo (cards da Auditoria);
  - o selo de amostra parcial, lido do aviso do motor (raio_x._aviso_de_amostra_parcial), pelo mesmo modelo
    que a tela dos avisos usa (raio_x_apresentacao.CATALOGO_DE_AVISOS);
  - o Plano de Ação: o comparativo de 12 meses, o roteiro em 3 etapas com os produtos do cliente e as etapas
    do checklist.

Regras:
  - Os valores vêm do resultado já filtrado pelo plano (raio_x_servico.visao): o que o plano esconde chega como
    None e continua escondido. A lista some (fica a contagem, que todo plano vê), e a tela mostra o cadeado.
    Do resultado gravado sem filtro (`bruto`) só saem as datas das fatias, inclusive as que o plano esconde no
    Mês a mês (elas entram nos totais), quais relatórios trazem a coluna de custo e se trazem o estoque.
  - Contas em Decimal sobre os valores gravados (já em centavos), arredondando só no fim, metade para cima, com
    precisão de sobra: um valor absurdo que o motor aceita (uma chave de NF-e na coluna de custo) não derruba a tela.
  - Meses analisados: os dias cobertos pelos relatórios datados (a união dos períodos, cortada na data do Raio-X:
    o mês que ainda não começou não conta), mês a mês, pela fração dos dias de cada mês. O relatório do mês
    fechado vale 1; o de 01/09 a 14/11, 2,47 (mostrado como 2,5 meses). É a régua dos indicadores (dias
    cobertos), e a do dono:
    meses_efetivos = max(1, meses analisados). Com um relatório sem datas no envio, o total soma vendas de período
    desconhecido: não há ritmo nem projeção (nunca se divide por um período inventado).
  - Os valores a custo da Auditoria (prejuízo direto e produtos no vermelho) dividem só pelos meses dos relatórios
    com a coluna de custo: nos outros, nada foi auditado.
  - Ritmo mensal = total / meses_efetivos; projeção de 12 meses = ritmo x 12 (o ritmo não é arredondado antes).
  - Custo da inação (12 meses) = projeção do dinheiro recuperável (a sangria anualizada); "no escuro" = projeção
    da venda sem custo informado.
  - Retorno projetado (12 meses): conservador = 70% do custo da inação; otimizado = o custo da inação inteiro
    mais 1,5% da projeção da venda dos produtos com variação de preço ou sem custo
    (scanner.faturamento_variacao_ou_sem_custo, gravado desde a raio-x-2026-11.7; no Raio-X anterior, o
    otimizado fica sem essa parcela, e a tela diz por quê).
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
import math
import re

import raio_x
import raio_x_apresentacao

MESES_DO_PLANO = 12
FRACAO_CONSERVADORA = Decimal("0.70")
GANHO_DE_MARGEM = Decimal("0.015")
ITENS_POR_ETAPA = 5
_CENTAVO = Decimal("0.01")
_DECIMO = Decimal("0.1")
# Precisão de sobra para qualquer float finito, como etl_processor.em_centavos: no contexto padrão (28 dígitos), o
# quantize de um custo de 1e29 (ou "1E+100") levantaria decimal.InvalidOperation e derrubaria a página.
_PRECISAO = 400

# O aviso de amostra parcial casado pelo modelo da tela dos avisos: um texto só para os dois lugares.
_MODELO_DA_AMOSTRA = next(modelo for modelo in raio_x_apresentacao.CATALOGO_DE_AVISOS if modelo.id == "amostra_parcial")

# As três etapas do roteiro: (id, número, prazo, título), nos termos do pedido do dono.
ETAPAS = (
    ("estancar", 1, "Semana 1", "Estancar prejuízo imediato"),
    ("travar", 2, "Semanas 2 e 3", "Travar descontos no balcão"),
    ("controle", 3, "Mês 1 em diante", "Controle e previsibilidade"),
)

ORIENTACOES = {
    "estancar": ("Corrija no PDV o preço de venda dos produtos vendidos abaixo do custo e confira, nos alertas de "
                 "cadastro, a unidade ou a embalagem do custo cadastrado."),
    "travar": ("Defina no sistema um preço mínimo para estes produtos e trave o desconto no balcão: só quem você "
               "autorizar vende abaixo do preço normal."),
    # As metas de custo e de estoque abaixo dela dizem o que fazer em cada caso (cadastrar, conferir, exportar com a
    # coluna de custo, lançar as notas, enviar o estoque): a orientação não pode contradizê-las.
    "controle": "Cumpra as metas abaixo e refaça o Raio-X todo mês para acompanhar.",
}

# Mensagem da etapa sem lista: (etapa, estado, motivo).
MENSAGENS = {
    ("estancar", "regularizada", None): ("Etapa já regularizada: nenhuma venda abaixo do custo nem custo distorcido "
                                         "nos relatórios enviados."),
    ("estancar", "regularizada", "parcial"): ("Etapa já regularizada nas vendas com custo informado: nenhuma saiu "
                                              "abaixo do custo nem com custo distorcido. As vendas sem custo informado "
                                              "ficam para a Etapa 3."),
    ("estancar", "sem_dados", "sem_coluna"): ("Os relatórios enviados não trazem a coluna de custo: sem ela, não dá "
                                              "para achar venda abaixo do custo. Se o seu sistema tem o custo dos "
                                              "produtos, exporte o relatório com a coluna de custo e refaça o Raio-X; "
                                              "se não tem, cadastre o custo (Etapa 3)."),
    ("estancar", "sem_dados", "sem_venda_com_custo"): ("Nenhuma venda dos relatórios veio com custo informado: sem "
                                                       "ele, não dá para achar venda abaixo do custo. Cadastre o custo "
                                                       "dos produtos no seu sistema (Etapa 3) e refaça o Raio-X."),
    ("travar", "regularizada", None): "Etapa já regularizada: nenhuma venda abaixo do preço normal nos relatórios enviados.",
    ("travar", "sem_dados", None): ("Seus relatórios não mostram o preço de cada venda, então não dá para ver os "
                                    "descontos no balcão. Envie o relatório com o preço unitário e o valor total de cada "
                                    "linha (ou uma linha por venda) e refaça o Raio-X."),
    ("controle", "regularizada", None): ("Etapa já regularizada: todo produto vendido teve custo informado e nenhum "
                                         "aparece com estoque negativo nos relatórios."),
}

# Por que não há ritmo nem projeção (horizonte()["motivo"]): relatório sem as datas do período, relatório com período
# (e venda) depois da data do Raio-X, ou os dois. As vendas deles entram nos totais.
SEM_PROJECAO = {
    "sem_datas": ("Um ou mais relatórios enviados não trazem as datas do período, e as vendas deles entram nos "
                  "totais: sem o período, não dá para calcular o ritmo por mês nem projetar 12 meses. Envie os "
                  "relatórios com o período no topo e refaça o Raio-X."),
    "periodo_futuro": ("Um ou mais relatórios enviados trazem um período depois da data do Raio-X, e as vendas deles "
                       "entram nos totais: assim, não dá para calcular o ritmo por mês nem projetar 12 meses. Confira "
                       "o período no topo desses relatórios e refaça o Raio-X."),
    "sem_datas_e_futuro": ("Um ou mais relatórios enviados não trazem as datas do período, e um ou mais trazem um "
                           "período depois da data do Raio-X; as vendas deles entram nos totais: assim, não dá para "
                           "calcular o ritmo por mês nem projetar 12 meses. Envie com o período no topo os relatórios "
                           "que não o trazem, confira o período dos demais e refaça o Raio-X."),
}
# A segunda linha do card do dinheiro recuperável, sem ritmo, e a janela da Auditoria, pelo mesmo motivo.
_SEM_RITMO = {
    "sem_datas": "um ou mais relatórios não trazem as datas do período",
    "periodo_futuro": "um ou mais relatórios trazem um período depois da data do Raio-X",
    "sem_datas_e_futuro": "há relatórios sem as datas do período e com período depois da data do Raio-X",
}
_SEM_PERIODO = {
    "sem_datas": "os relatórios não trazem as datas",
    "periodo_futuro": "os relatórios trazem um período depois da data do Raio-X",
    "sem_datas_e_futuro": "os relatórios não trazem as datas ou trazem um período depois da data do Raio-X",
}

EXPLICACAO_DO_RETORNO = ("Esse valor vai direto para o lucro líquido da empresa, pois as despesas fixas da loja já "
                         "estão pagas.")


def _pct(fracao: Decimal) -> str:
    """0,7 -> "70%"; 0,015 -> "1,5%"."""
    return raio_x_apresentacao._pct(float(fracao))


PERCENTUAL_CONSERVADOR = _pct(FRACAO_CONSERVADORA)
PERCENTUAL_GANHO_DE_MARGEM = _pct(GANHO_DE_MARGEM)


# ---------------------------------------------------------------------------
# Números
# ---------------------------------------------------------------------------
def _decimal(valor) -> Decimal | None:
    """O número gravado em Decimal; None sem número (texto, booleano, NaN ou infinito num JSON editado)."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor):
        return None
    return Decimal(str(valor))


def _arredondar(valor: Decimal, passo: Decimal) -> float:
    with localcontext() as contexto:
        contexto.prec = _PRECISAO
        return float(valor.quantize(passo, rounding=ROUND_HALF_UP))


def _reais(valor: Decimal) -> float:
    return _arredondar(valor, _CENTAVO)


def _contagem(valor) -> int:
    """Uma contagem gravada (inteiro não negativo); 0 sem número."""
    return valor if isinstance(valor, int) and not isinstance(valor, bool) and valor > 0 else 0


def _dicionario(valor) -> dict:
    return valor if isinstance(valor, dict) else {}


def _lista(valor) -> list | None:
    return valor if isinstance(valor, list) else None


def _data(texto) -> date | None:
    if not isinstance(texto, str):
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def _meses_do_calendario(inicio: date, fim: date) -> set:
    """(ano, mês) de cada mês do calendário de `inicio` a `fim`, inclusive."""
    primeiro, ultimo = inicio.year * 12 + inicio.month - 1, fim.year * 12 + fim.month - 1
    return {(mes // 12, mes % 12 + 1) for mes in range(primeiro, ultimo + 1)}


def _plural(quantidade: int, singular: str, plural: str) -> str:
    return f"{quantidade} {singular if quantidade == 1 else plural}"


# ---------------------------------------------------------------------------
# Horizonte de tempo
# ---------------------------------------------------------------------------
def texto_dos_meses(meses) -> str:
    """2 -> "2 meses"; 2,47 -> "2,5 meses"; 2,95 -> "3 meses"; 1 -> "1 mês"; 1,5 -> "1,5 mês"; abaixo de 1 ->
    "menos de 1 mês"."""
    if meses is None:
        return ""
    meses = Decimal(meses)
    if meses < 1:
        return "menos de 1 mês"
    with localcontext() as contexto:
        contexto.prec = _PRECISAO
        decimo = meses.quantize(_DECIMO, rounding=ROUND_HALF_UP)
    numero = str(int(decimo)) if decimo == decimo.to_integral_value() else f"{decimo:f}".replace(".", ",")
    return f"{numero} {'mês' if decimo < 2 else 'meses'}"


def _sem_venda(fatia: dict) -> bool:
    """A fatia sem nenhuma venda: o motor cria a do mês que já começou pelo relógio dele (UTC), mesmo vazia."""
    return not _decimal(fatia.get("faturamento")) and not _contagem(fatia.get("itens"))


def _motivo(sem_data: int, futuro: int) -> str | None:
    if sem_data and futuro:
        return "sem_datas_e_futuro"
    return "sem_datas" if sem_data else ("periodo_futuro" if futuro else None)


def horizonte(fatias, ate: date | None = None) -> dict:
    """Os meses que os relatórios do envio cobrem, a partir das fatias gravadas (todas, também as que o plano
    esconde no Mês a mês). `ate`: a data do Raio-X; o período que passa dela é cortado nela (o mês que ainda não
    começou não conta). A fatia que começa depois dela e tem venda conta à parte (o cabeçalho traz um período errado,
    e as vendas dela entram nos totais); a que começa depois dela sem venda é ignorada (o motor, em UTC, cria a do mês
    seguinte nas últimas horas do mês). Devolve:
      - "meses": os meses cobertos (Decimal): a união dos dias dos relatórios datados, mês a mês, pela fração dos
        dias de cada mês (o mês fechado vale 1); None sem relatório datado;
      - "inicio" e "fim": a primeira e a última data cobertas (date), já cortadas;
      - "lacunas": os meses do calendário no intervalo sem nenhum dia de relatório;
      - "sem_data" e "futuro": quantos relatórios (fatias) não têm datas utilizáveis e quantos trazem período depois
        da data do Raio-X;
      - "projetavel": há ritmo e projeção (nenhum dos dois casos acima);
      - "motivo": None, "sem_datas", "periodo_futuro" ou "sem_datas_e_futuro";
      - "meses_texto" e "texto": "2,5 meses" e "2,5 meses (01/09/2026 a 14/11/2026)", com as lacunas e os
        relatórios sem datas ou com período futuro ditos, para o subtítulo do plano;
      - "linha_periodo" e "linha_sem_ritmo": as duas linhas do card do dinheiro recuperável."""
    intervalos, sem_data, futuro = [], 0, 0
    for fatia in fatias or ():
        if not isinstance(fatia, dict):
            continue
        inicio, fim = _data(fatia.get("inicio")), _data(fatia.get("fim"))
        if inicio is None or fim is None or fim < inicio:
            sem_data += 1
            continue
        if ate is not None and inicio > ate:
            if not _sem_venda(fatia):
                futuro += 1
            continue
        intervalos.append((inicio, min(fim, ate) if ate is not None else fim))
    motivo = _motivo(sem_data, futuro)
    if not intervalos:
        problema = _SEM_PERIODO.get(motivo or "sem_datas")
        return {"meses": None, "inicio": None, "fim": None, "lacunas": 0, "sem_data": sem_data, "futuro": futuro,
                "projetavel": False, "motivo": motivo or "sem_datas", "meses_texto": "", "texto": "",
                "linha_periodo": f"Período analisado: {problema}",
                "linha_sem_ritmo": f"Sem ritmo por mês: {_SEM_RITMO[motivo or 'sem_datas']}."}
    # A união dos períodos: o mesmo dia em dois relatórios conta uma vez.
    intervalos.sort()
    unidos = [list(intervalos[0])]
    for inicio, fim in intervalos[1:]:
        if inicio <= unidos[-1][1] + timedelta(days=1):
            unidos[-1][1] = max(unidos[-1][1], fim)
        else:
            unidos.append([inicio, fim])
    dias_por_mes: dict = {}
    for inicio, fim in unidos:
        dia = inicio
        while dia <= fim:
            ultimo = date(dia.year, dia.month, monthrange(dia.year, dia.month)[1])
            ate_o_fim = min(fim, ultimo)
            dias_por_mes[(dia.year, dia.month)] = dias_por_mes.get((dia.year, dia.month), 0) + (ate_o_fim - dia).days + 1
            dia = ate_o_fim + timedelta(days=1)
    meses = sum((Decimal(dias) / Decimal(monthrange(ano, mes)[1]) for (ano, mes), dias in dias_por_mes.items()),
                Decimal(0))
    inicio, fim = unidos[0][0], unidos[-1][1]
    lacunas = len(_meses_do_calendario(inicio, fim)) - len(dias_por_mes)
    meses_texto = texto_dos_meses(meses)
    texto = f"{meses_texto} ({inicio:%d/%m/%Y} a {fim:%d/%m/%Y})"
    if lacunas:
        texto += f", sem relatório em {_plural(lacunas, 'mês', 'meses')} desse intervalo"
    texto += _resto_sem_periodo(sem_data, futuro)
    return {"meses": meses, "inicio": inicio, "fim": fim, "lacunas": lacunas, "sem_data": sem_data, "futuro": futuro,
            "projetavel": motivo is None, "motivo": motivo, "meses_texto": meses_texto, "texto": texto,
            "linha_periodo": f"Acumulado no período analisado: {texto}",
            "linha_sem_ritmo": f"Sem ritmo por mês: {_SEM_RITMO[motivo]}." if motivo else ""}


def _resto_sem_periodo(sem_data: int, futuro: int) -> str:
    """", mais 1 relatório sem datas", ", mais 2 relatórios com período depois da data do Raio-X"."""
    texto = ""
    if sem_data:
        texto += f", mais {_plural(sem_data, 'relatório', 'relatórios')} sem datas"
    if futuro:
        texto += f", mais {_plural(futuro, 'relatório', 'relatórios')} com período depois da data do Raio-X"
    return texto


def _ritmo_exato(valor, tempo: dict) -> tuple[Decimal, Decimal] | None:
    """(mensal, anual) sem arredondar; None sem número ou sem projeção."""
    total = _decimal(valor)
    if total is None or not tempo.get("projetavel") or not tempo.get("meses"):
        return None
    with localcontext() as contexto:
        contexto.prec = _PRECISAO
        mensal = total / max(Decimal(1), Decimal(tempo["meses"]))
        return mensal, mensal * MESES_DO_PLANO


def ritmo(valor, tempo: dict) -> dict | None:
    """{"total", "mensal", "anual"} em reais (centavos, metade para cima); None sem número ou sem projeção."""
    exato = _ritmo_exato(valor, tempo)
    if exato is None:
        return None
    return {"total": _reais(_decimal(valor)), "mensal": _reais(exato[0]), "anual": _reais(exato[1])}


def janela(tempo_auditado: dict, tempo_enviado: dict, media: dict | None) -> str:
    """A janela de tempo de um card da Auditoria: "Nos 2 meses auditados (média de R$ 5,00/mês)". Auditados são os
    meses dos relatórios com a coluna de custo; quando há outros, "de N enviados" diz quantos vieram."""
    if not tempo_enviado.get("projetavel"):
        if tempo_enviado.get("meses"):
            resto = _resto_sem_periodo(_contagem(tempo_enviado.get("sem_data")), _contagem(tempo_enviado.get("futuro")))
            return f"Em {tempo_enviado['meses_texto']} com datas{resto}: sem média por mês"
        if tempo_enviado.get("motivo") == "periodo_futuro":
            return "Relatórios com período depois da data do Raio-X: sem média por mês"
        if tempo_enviado.get("motivo") == "sem_datas_e_futuro":
            return "Relatórios sem as datas do período ou com período depois da data do Raio-X: sem média por mês"
        return "Relatórios sem as datas do período: sem média por mês"
    if not tempo_auditado.get("meses"):
        return "Sem custo informado nos relatórios: sem média por mês"
    meses = tempo_auditado["meses"]
    texto = tempo_auditado["meses_texto"]
    if meses < 1:
        frase = "Em menos de 1 mês auditado"
    elif texto.endswith(" mês"):
        frase = f"No {texto} auditado"
    else:
        frase = f"Nos {texto} auditados"
    # "de N enviados" só quando os números mostrados diferem (a comparação exata daria "1 mês de 1 mês enviados").
    if tempo_enviado["meses_texto"] != texto:
        enviado = tempo_enviado["meses_texto"]
        frase += f" de {enviado} {'enviado' if enviado.endswith(' mês') else 'enviados'}"
    if media is not None:
        frase += f" (média de R$ {raio_x._brl(media['mensal'])}/mês)"
    return frase


def amostra_parcial(avisos) -> dict | None:
    """{"percentual", "avisos"} quando o motor avisou que a venda parece filtrada diante do estoque inteiro: a
    parte dos produtos com saldo que vendeu (o número do aviso). Com mais de um aviso (mais de uma venda item a
    item no envio), o maior percentual: o selo nunca exagera o quanto a amostra é parcial."""
    percentuais = []
    for aviso in avisos or ():
        if isinstance(aviso, str):
            achado = re.fullmatch(_MODELO_DA_AMOSTRA.regex, aviso)
            if achado:
                percentuais.append(int(achado.group("percentual")))
    if not percentuais:
        return None
    return {"percentual": max(percentuais), "avisos": len(percentuais)}


def tem_estoque(bruto: dict) -> bool | None:
    """Os relatórios trazem o estoque? Pelo indicador de ruptura do resultado gravado sem filtro (o plano pode
    escondê-lo, mas o formato do relatório não é dado protegido). None no Raio-X sem os indicadores."""
    indicadores = _dicionario(_dicionario(_dicionario(bruto).get("indicadores_executivos")).get("indicadores"))
    ruptura = indicadores.get("ruptura")
    if not isinstance(ruptura, dict) or not isinstance(ruptura.get("status"), str):
        return None
    return ruptura["status"] != "requer_estoque"


def _fatias_com_custo(bruto: dict) -> list | None:
    """As fatias dos relatórios que trazem a coluna de custo (arquivos e fatias usam o mesmo índice); None quando o
    resultado não diz quais trazem (então valem todas)."""
    arquivos, fatias = _lista(bruto.get("arquivos")), _lista(bruto.get("fatias"))
    if arquivos is None or fatias is None:
        return None
    com_custo = {arquivo.get("indice") for arquivo in arquivos if isinstance(arquivo, dict) and arquivo.get("tem_custo")}
    if not all(isinstance(arquivo, dict) and isinstance(arquivo.get("tem_custo"), bool) for arquivo in arquivos):
        return None
    return [fatia for fatia in fatias if isinstance(fatia, dict) and fatia.get("indice") in com_custo]


# ---------------------------------------------------------------------------
# Roteiro em 3 etapas
# ---------------------------------------------------------------------------
def _por_unidade(valor, quantidade) -> float | None:
    numerador, divisor = _decimal(valor), _decimal(quantidade)
    if numerador is None or divisor is None or divisor <= 0:
        return None
    with localcontext() as contexto:
        contexto.prec = _PRECISAO
        return _reais(numerador / divisor)


def _etapa(id_etapa: str, estado: str, motivo: str | None = None, **campos) -> dict:
    numero, prazo, titulo = next((n, p, t) for i, n, p, t in ETAPAS if i == id_etapa)
    return {"id": id_etapa, "numero": numero, "prazo": prazo, "titulo": titulo, "estado": estado, "motivo": motivo,
            "mensagem": MENSAGENS.get((id_etapa, estado, motivo), ""), "orientacao": ORIENTACOES[id_etapa],
            "concluida_sozinha": estado == "regularizada", **campos}


def _sem_coluna_de_custo(saude: dict) -> bool:
    return not _contagem(saude.get("arquivos_com_custo"))


def _etapa_estancar(resultado: dict) -> dict:
    """Etapa 1: os produtos vendidos abaixo do custo (maior perda primeiro) e os alertas de cadastro (custo
    distorcido), até ITENS_POR_ETAPA de cada. Lista None: o plano esconde, fica só a contagem. Sem venda com custo
    informado (coluna ausente ou vazia), a etapa não se avalia; com parte da venda sem custo, ela só vale para as
    vendas com custo, e a tela diz isso."""
    saude, auditoria = _dicionario(resultado.get("saude")), _dicionario(resultado.get("auditoria"))
    abaixo = _lista(auditoria.get("itens_abaixo_custo"))
    quantos_abaixo = _contagem(auditoria.get("itens_venda_abaixo_custo"))
    bloco = _dicionario(auditoria.get("custo_a_confirmar"))
    quantos_alertas = _contagem(bloco.get("itens"))
    lista_alertas = _lista(bloco.get("lista"))
    produtos = []
    for item in abaixo or ():
        if not isinstance(item, dict) or (_decimal(item.get("perda")) or 0) <= 0:
            continue
        produtos.append({"produto": item.get("produto"), "codigo": item.get("codigo"), "perda": item.get("perda"),
                         "preco_unitario": _por_unidade(item.get("faturamento_abaixo"), item.get("quantidade_abaixo")),
                         "custo_unitario": _por_unidade(item.get("custo_abaixo"), item.get("quantidade_abaixo"))})
        if len(produtos) == ITENS_POR_ETAPA:
            break
    alertas = []
    for item in lista_alertas or ():
        if not isinstance(item, dict):
            continue
        venda, custo = _decimal(item.get("faturamento_com_custo")), _decimal(item.get("custo_total"))
        vezes = None
        if custo is not None and venda is not None and venda > 0:
            with localcontext() as contexto:
                contexto.prec = _PRECISAO
                vezes = _arredondar(custo / venda, _DECIMO)
        alertas.append({"produto": item.get("produto"), "codigo": item.get("codigo"),
                        "custo_unitario": (item.get("custo_medio") if _decimal(item.get("custo_medio")) is not None
                                           else _por_unidade(item.get("custo_total"), item.get("quantidade_com_custo"))),
                        "preco_unitario": _por_unidade(item.get("faturamento_com_custo"), item.get("quantidade_com_custo")),
                        # Quantas vezes o custo passa da venda com custo (1 casa; 3 ou mais por definição).
                        "vezes": vezes})
        if len(alertas) == ITENS_POR_ETAPA:
            break
    venda_com_custo = _decimal(auditoria.get("faturamento_com_custo"))
    pct_sem_custo = _decimal(saude.get("pct_faturamento_sem_custo"))
    # Sem venda com custo pelo valor gravado; o percentual (arredondado a 4 casas pelo motor: 0,005% vira 100%) só
    # vale sem esse valor. Com produto listado abaixo do custo ou de custo distorcido, houve venda com custo.
    if venda_com_custo is not None:
        sem_venda_com_custo = venda_com_custo <= 0
    else:
        sem_venda_com_custo = pct_sem_custo is not None and pct_sem_custo >= 1
    motivo = None
    if _sem_coluna_de_custo(saude):
        estado, motivo = "sem_dados", "sem_coluna"
    elif sem_venda_com_custo and not quantos_abaixo and not quantos_alertas:
        estado, motivo = "sem_dados", "sem_venda_com_custo"
    elif not quantos_abaixo and not quantos_alertas:
        estado = "regularizada"
        motivo = "parcial" if pct_sem_custo is not None and pct_sem_custo > 0 else None
    else:
        estado = "pendente"

    def _resto(quantidade: int, lista: list | None, mostrados: int) -> dict:
        # A Auditoria lista até raio_x.LIMITE_ITENS_LISTA: o "e mais" conta só o que ela mostra, e o total vem à parte.
        if lista is None:
            return {"restantes": 0, "na_lista": 0, "alem_da_lista": False}
        na_lista = min(quantidade, len(lista))
        return {"restantes": max(0, na_lista - mostrados), "na_lista": na_lista, "alem_da_lista": quantidade > len(lista)}

    return _etapa("estancar", estado, motivo,
                  abaixo={"quantidade": quantos_abaixo, "perda": auditoria.get("perda_abaixo_custo"),
                          "itens": produtos, "bloqueado": quantos_abaixo > 0 and abaixo is None,
                          **_resto(quantos_abaixo, abaixo, len(produtos))},
                  alertas={"quantidade": quantos_alertas, "itens": alertas,
                           "bloqueado": quantos_alertas > 0 and lista_alertas is None,
                           **_resto(quantos_alertas, lista_alertas, len(alertas))})


def _etapa_travar(resultado: dict) -> dict:
    """Etapa 2: os produtos com mais dinheiro deixado na mesa (vendas abaixo do preço normal), até
    ITENS_POR_ETAPA, na ordem do scanner (raio_x: o maior vazamento primeiro). A lista do scanner é aberta a
    todo plano (o plano esconde só a margem de cada item)."""
    scanner = _dicionario(resultado.get("scanner"))
    vazamento = _decimal(scanner.get("vazamento_preco")) or Decimal(0)
    itens = []
    for item in _lista(scanner.get("itens")) or ():
        if not isinstance(item, dict) or (_decimal(item.get("vazamento")) or 0) <= 0:
            continue
        itens.append({"produto": item.get("produto"), "codigo": item.get("codigo"), "vazamento": item.get("vazamento"),
                      "preco_min": item.get("preco_min"), "preco_referencia": item.get("preco_referencia"),
                      "referencia_tabela": bool(item.get("referencia_tabela"))})
        if len(itens) == ITENS_POR_ETAPA:
            break
    if not scanner.get("variacao_visivel"):
        estado = "sem_dados"
    elif vazamento <= 0:
        estado = "regularizada"
    else:
        estado = "pendente"
    return _etapa("travar", estado, vazamento=_reais(vazamento), itens=itens,
                  com_variacao=_contagem(scanner.get("itens_com_variacao")))


_MODELO_SEM_COLUNA = next(modelo for modelo in raio_x_apresentacao.CATALOGO_DE_AVISOS
                          if modelo.id == "arquivo_sem_coluna_de_custo")


def arquivos_sem_coluna_de_custo(avisos) -> list[int]:
    """Os números dos arquivos enviados sem a coluna de custo, pelos avisos do motor ("O arquivo N não traz a coluna
    de custo"), em ordem e cada um uma vez (o arquivo fatiado por mês é um arquivo só). São os números que os avisos
    da página do Raio-X mostram (a ordem do envio)."""
    numeros = set()
    for aviso in avisos or ():
        if isinstance(aviso, str):
            achado = re.fullmatch(_MODELO_SEM_COLUNA.regex, aviso)
            if achado:
                numeros.add(int(achado.group("arquivo")))
    return sorted(numeros)


def relatorios_sem_coluna_de_custo(avisos) -> int:
    """Quantos arquivos enviados vieram sem a coluna de custo."""
    return len(arquivos_sem_coluna_de_custo(avisos))


def _etapa_controle(resultado: dict, estoque: bool | None, com_custo: list | None = None) -> dict:
    """Etapa 3: as metas de custo e de estoque.
      - custo: "sem_coluna" (os relatórios não trazem a coluna: exportar com ela ou, se o sistema não tem o custo,
        cadastrar), "cadastrar" (produtos vendidos sem custo em nenhuma venda), "conferir" (todo produto teve custo em
        alguma venda, mas parte das vendas saiu sem custo informado) ou "ok";
      - parte dos arquivos sem a coluna (`arquivos_sem_coluna`, os números deles): uma linha a mais pede esses
        relatórios com a coluna (ou o cadastro, se o sistema não tem o custo), e a meta de custo olha só os
        relatórios que trazem a coluna (`com_custo`, as fatias deles; a venda sem custo de cada fatia não é valor que
        o plano esconda): "cadastrar_nos_com_coluna" (venda sem custo informado neles: o custo falta no sistema, e
        não na exportação) ou "ok_nos_com_coluna". Sem saber quais relatórios trazem a coluna (`com_custo` None), a
        meta segue a regra de todo o envio;
      - estoque: "negativo" (produtos com estoque negativo nos relatórios), "sem_estoque" (os relatórios não trazem
        o estoque), "desconhecido" (Raio-X anterior aos indicadores, que não diz se havia estoque) ou "ok".
    A etapa só sai regularizada com as duas metas "ok" e nenhum arquivo sem a coluna."""
    saude = _dicionario(resultado.get("saude"))
    sem_custo = _contagem(saude.get("itens_sem_custo"))
    negativo = _contagem(_dicionario(saude.get("inconsistencias")).get("estoque_negativo"))
    venda_sem_custo = _decimal(saude.get("faturamento_sem_custo")) or Decimal(0)
    sem_coluna = arquivos_sem_coluna_de_custo(resultado.get("avisos"))
    venda_nos_com_coluna = None
    if _sem_coluna_de_custo(saude):
        # Nenhum relatório traz a coluna: a própria meta pede a coluna, sem a linha a mais.
        meta_custo, sem_coluna = "sem_coluna", []
    elif sem_coluna and com_custo is not None:
        with localcontext() as contexto:
            contexto.prec = _PRECISAO
            venda_nos_com_coluna = sum((_decimal(fatia.get("faturamento_sem_custo")) or Decimal(0)
                                        for fatia in com_custo), Decimal(0))
        meta_custo = "cadastrar_nos_com_coluna" if venda_nos_com_coluna > 0 else "ok_nos_com_coluna"
    elif sem_custo:
        meta_custo = "cadastrar"
    elif venda_sem_custo > 0:
        meta_custo = "conferir"
    else:
        meta_custo = "ok"
    if negativo:
        meta_estoque = "negativo"
    elif estoque is False:
        meta_estoque = "sem_estoque"
    elif estoque is None:
        meta_estoque = "desconhecido"
    else:
        meta_estoque = "ok"
    estado = "regularizada" if meta_custo == "ok" and meta_estoque == "ok" and not sem_coluna else "pendente"
    return _etapa("controle", estado, meta_custo=meta_custo, meta_estoque=meta_estoque, sem_custo=sem_custo,
                  estoque_negativo=negativo, itens_vendidos=_contagem(saude.get("itens_vendidos")),
                  venda_sem_custo=_reais(venda_sem_custo), pct_sem_custo=saude.get("pct_faturamento_sem_custo"),
                  arquivos_sem_coluna=sem_coluna, relatorios_sem_coluna=len(sem_coluna),
                  # Com o nome, como a página do Raio-X ("2 (jan.csv)"): a Auditoria e o PDF não mostram os avisos.
                  arquivos_sem_coluna_rotulos=[raio_x_apresentacao._arquivo_por_extenso(str(numero),
                                                                                       resultado.get("nomes_arquivos"))
                                               for numero in sem_coluna],
                  venda_sem_custo_nos_com_coluna=_reais(venda_nos_com_coluna) if venda_nos_com_coluna is not None
                  else None)


# ---------------------------------------------------------------------------
# Tudo o que as duas telas usam
# ---------------------------------------------------------------------------
def preparar(resultado: dict, bruto: dict, ate: date | None = None) -> dict:
    """O horizonte dos cards e o Plano de Ação. `resultado`: o da raio_x_servico.visao (filtrado pelo plano);
    `bruto`: o resultado gravado, de onde saem só as datas de todas as fatias, quais relatórios trazem a coluna de
    custo e se trazem o estoque; `ate`: a data do Raio-X (o período que passa dela é cortado nela)."""
    resultado, bruto = _dicionario(resultado), _dicionario(bruto)
    fatias = _lista(bruto.get("fatias"))
    if fatias is None:
        fatias = _lista(resultado.get("fatias")) or []
    tempo = horizonte(fatias, ate)
    com_custo = _fatias_com_custo(bruto)
    tempo_auditado = horizonte(com_custo, ate) if com_custo is not None else tempo
    saude, scanner = _dicionario(resultado.get("saude")), _dicionario(resultado.get("scanner"))
    auditoria, recuperavel = _dicionario(resultado.get("auditoria")), _dicionario(resultado.get("recuperavel"))
    indicadores = _dicionario(_dicionario(resultado.get("indicadores_executivos")).get("indicadores"))
    negativa = _dicionario(indicadores.get("margem_negativa"))

    sangria = _ritmo_exato(recuperavel.get("total"), tempo)
    escuro = _ritmo_exato(saude.get("faturamento_sem_custo"), tempo)
    base_gravada = scanner.get("faturamento_variacao_ou_sem_custo")
    base = _ritmo_exato(base_gravada, tempo)
    retorno = None
    if sangria is not None:
        anual = sangria[1]
        with localcontext() as contexto:
            contexto.prec = _PRECISAO
            ganho = GANHO_DE_MARGEM * base[1] if base is not None else None
            retorno = {"conservador": _reais(FRACAO_CONSERVADORA * anual),
                       "otimizado": _reais(anual + (ganho or Decimal(0))),
                       "sangria_anual": _reais(anual),
                       "base_anual": _reais(base[1]) if base is not None else None,
                       "ganho_margem": _reais(ganho) if ganho is not None else None,
                       # Raio-X gravado antes da raio-x-2026-11.7: sem a venda dos produtos com variação ou sem custo.
                       "sem_base": base_gravada is None}
    # Cards da Auditoria: o prejuízo direto (vendas abaixo do custo) e o dos produtos que fecharam o período no
    # vermelho (indicador 14, desde a raio-x-2026-11.6; o plano que abre a Auditoria o libera), pelos meses com custo.
    prejuizo = ritmo(auditoria.get("perda_abaixo_custo"), tempo_auditado) if tempo["projetavel"] else None
    perda_produtos = negativa.get("perda") if negativa.get("status") == "calculado" else None
    prejuizo_produtos = ritmo(perda_produtos, tempo_auditado) if perda_produtos is not None and tempo["projetavel"] \
        else None
    return {
        "horizonte": tempo,
        "horizonte_auditado": tempo_auditado,
        "sem_projecao": SEM_PROJECAO.get(tempo["motivo"], SEM_PROJECAO["sem_datas"]) if not tempo["projetavel"] else "",
        "amostra": amostra_parcial(resultado.get("avisos")),
        "recuperavel": ritmo(recuperavel.get("total"), tempo),
        "prejuizo": prejuizo,
        "prejuizo_produtos": prejuizo_produtos,
        "janela_prejuizo": janela(tempo_auditado, tempo, prejuizo),
        "janela_produtos": janela(tempo_auditado, tempo, prejuizo_produtos),
        "plano": {
            "inacao": ({"anual": _reais(sangria[1]), "mensal": _reais(sangria[0]),
                        "escuro_anual": _reais(escuro[1]) if escuro is not None else None,
                        "escuro_mensal": _reais(escuro[0]) if escuro is not None else None,
                        "pct_sem_custo": saude.get("pct_faturamento_sem_custo")}
                       if sangria is not None else None),
            "retorno": retorno,
            "etapas": [_etapa_estancar(resultado), _etapa_travar(resultado),
                       _etapa_controle(resultado, tem_estoque(bruto), com_custo)],
        },
        "percentual_conservador": PERCENTUAL_CONSERVADOR,
        "percentual_ganho_de_margem": PERCENTUAL_GANHO_DE_MARGEM,
        "explicacao_do_retorno": EXPLICACAO_DO_RETORNO,
        "meses_do_plano": MESES_DO_PLANO,
        "razao_a_confirmar": raio_x.RAZAO_CUSTO_IMPLAUSIVEL,
        "limite_da_lista": raio_x.LIMITE_ITENS_LISTA,
    }
