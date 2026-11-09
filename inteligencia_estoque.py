"""Inteligência de estoque e de preço da Avaliação de Cenário (Etapa 3).

Funções puras (sem banco, Flask nem config), no processo pai, sobre as linhas da staging de um lead
(brainstorming_dados_pdv, no formato de avaliacao_cenario.linha_da_staging), a triagem
(triagem_cenario.triar) e a meta do envio. Nada é lido de arquivo: o lead envia só relatórios de
VENDAS, então o estoque é o saldo que o próprio relatório de vendas imprime e as datas são as dos
períodos dos relatórios.

  1. Estoque parado por idade (estoque_parado_por_idade): itens com estoque e custo informados que
     estão há pelo menos 60, 90 ou 180 dias sem venda (faixas configuráveis) e o capital imobilizado
     neles (estoque x custo médio), que é o valor de oportunidade de liberação de capital. Os dias sem
     venda são um PISO: contados do fim do último relatório em que o produto vendeu até o fim da
     cobertura do envio, e só nos dias que algum relatório cobre (sem relatório de um mês, esse mês
     não conta: a venda pode ter acontecido nele). Com relatório anual, quem vendeu no ano tem piso 0.
  2. Estoque excedente (giro baixíssimo): itens que ainda vendem (menos de 60 dias sem venda) com
     estoque para mais de `cobertura_alvo_dias` dias no ritmo de venda do último ano; o capital acima
     da cobertura-alvo é o que se libera sem deixar o item faltar.
  3. Simulador de liberação de caixa (simular_liquidacao): para os itens da Urgência com estoque,
     custo e preço, a receita do estoque ao preço médio praticado no último ano com venda e o capital
     ao custo; cada cenário (desconto e fração do estoque vendida) dá o caixa e o resultado contra o
     custo. Os descontos padrão são uma tabela de sensibilidade, não uma sugestão.
  4. Auditoria de preço (auditar_precos): no último ano, o preço praticado médio contra o preço pelo
     markup de referência da categoria (o menor entre o markup agregado da categoria e a mediana dos
     markups dos itens dela, com ao menos `itens_minimos_markup` itens), ou pelo markup informado pelo
     consultor (o "markup do segmento" quando ele tiver um). Nenhuma referência de mercado é inventada:
     sem categoria com itens suficientes e sem markup informado, o item fica "sem referência".

O que NÃO é: o estoque não é inventário físico (é o saldo que o PDV imprimiu na última venda do
produto); o preço pelo markup não considera demanda, concorrência nem promoção; nada disto é lucro.
Itens de custo implausível (triagem.a_confirmar) ficam fora de tudo. Somas e razões em Decimal; a
saída arredonda dinheiro em centavos.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import math
import re
import statistics

import triagem_cenario

VERSAO_INTELIGENCIA = "inteligencia-lead-2026-11.1"
_ZERO = Decimal("0")
_UM = Decimal("1")
_CENTAVO = Decimal("0.01")
_RE_DECIMAL = re.compile(r"\d{1,6}(?:\.\d{1,6})?", re.ASCII)


@dataclass(frozen=True)
class ParametrosInteligencia:
    """Parâmetros da inteligência de estoque e de preço, configuráveis (como os Limiares da triagem).

    Decimal nos valores e int nas contagens; a validação recusa tipo errado e faixa incoerente."""
    faixas_dias: tuple = (60, 90, 180)
    cobertura_alvo_dias: int = 60
    descontos: tuple = (Decimal("0"), Decimal("0.10"), Decimal("0.20"), Decimal("0.30"))
    fracao_liquidada: Decimal = Decimal("1")
    itens_minimos_markup: int = 3
    markup_informado: Decimal | None = None
    tolerancia_preco: Decimal = Decimal("0.005")
    itens_listados: int = 30

    def __post_init__(self):
        faixas = self.faixas_dias
        if (not isinstance(faixas, tuple) or not 1 <= len(faixas) <= 5
                or not all(type(f) is int and 1 <= f <= 3650 for f in faixas)
                or list(faixas) != sorted(set(faixas))):
            raise ValueError("Faixas de dias: de 1 a 5 inteiros crescentes entre 1 e 3650.")
        for nome in ("cobertura_alvo_dias", "itens_minimos_markup", "itens_listados"):
            valor = getattr(self, nome)
            if type(valor) is not int or not 1 <= valor <= 3650:
                raise ValueError(f"Parâmetro {nome} precisa ser um inteiro de 1 a 3650.")
        descontos = self.descontos
        if (not isinstance(descontos, tuple) or not 1 <= len(descontos) <= 6
                or not all(_decimal_valido(d) and _ZERO <= d <= Decimal("0.95") for d in descontos)):
            raise ValueError("Descontos simulados: de 1 a 6 frações entre 0 e 0,95.")
        if not _decimal_valido(self.fracao_liquidada) or not _ZERO < self.fracao_liquidada <= _UM:
            raise ValueError("Fração liquidada precisa estar entre 0 (exclusivo) e 1.")
        if self.markup_informado is not None and (not _decimal_valido(self.markup_informado)
                                                  or not _ZERO <= self.markup_informado <= Decimal("10")):
            raise ValueError("Markup informado precisa estar entre 0 e 10 (1.000%).")
        if not _decimal_valido(self.tolerancia_preco) or not _ZERO <= self.tolerancia_preco <= Decimal("0.2"):
            raise ValueError("Tolerância de preço precisa estar entre 0 e 0,2.")

    def para_dict(self) -> dict:
        dados = {"versao": VERSAO_INTELIGENCIA}
        for campo in fields(self):
            valor = getattr(self, campo.name)
            if isinstance(valor, tuple):
                dados[campo.name] = [v if type(v) is int else str(v) for v in valor]
            elif isinstance(valor, Decimal):
                dados[campo.name] = str(valor)
            else:
                dados[campo.name] = valor
        return dados

    def com_ajustes(self, ajustes: dict) -> "ParametrosInteligencia":
        """Estes parâmetros com alguns trocados, a partir de texto (a consulta da rota): descontos
        separados por vírgula ("0.1,0.25"), frações e markup em texto decimal simples. Nome
        desconhecido ou valor ilegível: ValueError."""
        if not isinstance(ajustes, dict):
            raise ValueError("Ajustes ilegíveis.")
        valores = {}
        for nome, texto in ajustes.items():
            if nome == "descontos":
                partes = [p.strip() for p in str(texto).split(",") if p.strip()]
                valores[nome] = tuple(_decimal_de_texto(p) for p in partes)
            elif nome in ("fracao_liquidada", "markup_informado", "tolerancia_preco"):
                valores[nome] = _decimal_de_texto(texto)
            elif nome in ("cobertura_alvo_dias", "itens_minimos_markup", "itens_listados"):
                if not (str(texto).isascii() and str(texto).isdigit()) or len(str(texto)) > 4:
                    raise ValueError(f"Parâmetro {nome} ilegível.")
                valores[nome] = int(str(texto))
            else:
                raise ValueError(f"Parâmetro desconhecido: {str(nome)[:40]}.")
        base = {campo.name: getattr(self, campo.name) for campo in fields(self)}
        base.update(valores)
        return ParametrosInteligencia(**base)


def _decimal_valido(valor) -> bool:
    return type(valor) is Decimal and valor.is_finite() and bool(_RE_DECIMAL.fullmatch(format(valor, "f")))


def _decimal_de_texto(texto) -> Decimal:
    texto = str(texto).strip()
    if not _RE_DECIMAL.fullmatch(texto):
        raise ValueError("Valor decimal ilegível (use ponto e até 6 casas).")
    try:
        return Decimal(texto)
    except InvalidOperation:
        raise ValueError("Valor decimal ilegível.") from None


PARAMETROS_PADRAO = ParametrosInteligencia()


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------
def _dinheiro(valor) -> float:
    numero = float(Decimal(valor).quantize(_CENTAVO))
    return numero if math.isfinite(numero) else 0.0


def _fracao(parte, todo):
    parte, todo = Decimal(parte), Decimal(todo)
    if todo <= 0:
        return None
    numero = float(parte / todo)
    return round(numero, 4) if math.isfinite(numero) else None


def _data(valor):
    if valor is None or isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor))


def periodos_da_meta(meta: dict) -> list:
    """Os períodos cobertos do envio [(início, fim)], em ordem e sem sobreposição (avaliacao_motor
    grava em meta["periodos_cobertos"], com o fim cortado em hoje). Ausentes ou fora do formato:
    ValueError (o diagnóstico fica "desatualizado")."""
    brutos = meta.get("periodos_cobertos")
    if not isinstance(brutos, list) or not brutos:
        raise ValueError("A meta não traz os períodos cobertos.")
    periodos = []
    for par in brutos:
        if not isinstance(par, list) or len(par) != 2:
            raise ValueError("Período coberto fora do formato.")
        inicio, fim = date.fromisoformat(par[0]), date.fromisoformat(par[1])
        if inicio > fim or (periodos and inicio <= periodos[-1][1]):
            raise ValueError("Períodos cobertos fora de ordem.")
        periodos.append((inicio, fim))
    return periodos


def _dias_cobertos_depois(ultima_venda: date, periodos: list) -> int:
    """Dias que algum relatório cobre depois do dia `ultima_venda` (o piso dos dias sem venda)."""
    total = 0
    for inicio, fim in periodos:
        comeco = max(inicio, ultima_venda + timedelta(days=1))
        if fim >= comeco:
            total += (fim - comeco).days + 1
    return total


def _por_produto(linhas: list) -> dict:
    produtos: dict = {}
    for linha in linhas:
        produtos.setdefault(linha["chave_produto"], {})[int(linha["ano_exercicio"])] = linha
    return produtos


def _ultima_venda(por_ano: dict):
    datas = [_data(l.get("fim_ultimo_relatorio_com_venda")) for l in por_ano.values()]
    datas = [d for d in datas if d is not None]
    return max(datas) if datas else None


def _custo_medio(por_ano: dict):
    """Custo médio do ano mais recente com custo (custo total / quantidade com custo)."""
    for ano in sorted(por_ano, reverse=True):
        linha = por_ano[ano]
        if linha["quantidade_com_custo"] > 0 and linha["custo_total"] > 0:
            return linha["custo_total"] / linha["quantidade_com_custo"]
    return None


def _preco_medio(por_ano: dict):
    """Preço médio praticado no ano mais recente com venda (venda bruta / quantidade)."""
    for ano in sorted(por_ano, reverse=True):
        linha = por_ano[ano]
        if linha["quantidade"] > 0 and linha["valor_venda_bruto"] > 0:
            return linha["valor_venda_bruto"] / linha["quantidade"]
    return None


def _identidade(por_ano: dict, rotulos: dict) -> dict:
    anos = sorted(por_ano)
    recente = por_ano[anos[-1]]
    codigo = next((por_ano[a].get("codigo_produto") for a in reversed(anos) if por_ano[a].get("codigo_produto")), None)
    categoria = triagem_cenario._categoria(por_ano)
    return {"produto": recente["descricao_produto"], "codigo": codigo,
            "categoria": rotulos.get(categoria) if categoria is not None else None}


def _rotulos(produtos: dict) -> dict:
    escolhido: dict = {}
    for por_ano in produtos.values():
        for ano, linha in por_ano.items():
            nome = linha.get("categoria_produto")
            if isinstance(nome, str) and nome.strip():
                chave = triagem_cenario.chave_da_categoria(nome)
                atual = escolhido.get(chave)
                if atual is None or ano > atual[0] or (ano == atual[0] and nome < atual[1]):
                    escolhido[chave] = (ano, nome)
    return {chave: nome for chave, (_ano, nome) in escolhido.items()}


def _faixa(dias: int, faixas: tuple):
    """Índice da faixa (dias >= faixa[i] e < faixa[i+1]) ou None abaixo da primeira."""
    escolhida = None
    for posicao, minimo in enumerate(faixas):
        if dias >= minimo:
            escolhida = posicao
    return escolhida


def _rotulo_da_faixa(posicao: int, faixas: tuple, sufixo: str = "dias") -> str:
    minimo = faixas[posicao]
    if posicao + 1 < len(faixas):
        return f"{minimo} a {faixas[posicao + 1] - 1} {sufixo}"
    return f"{minimo} {sufixo} ou mais"


# ---------------------------------------------------------------------------
# 1 e 2. Estoque parado por idade e estoque excedente
# ---------------------------------------------------------------------------
def estoque_parado_por_idade(linhas: list, cobertura, triagem, meta: dict,
                             parametros: ParametrosInteligencia = PARAMETROS_PADRAO) -> dict:
    produtos = _por_produto(linhas)
    periodos = periodos_da_meta(meta)
    fim_da_cobertura = periodos[-1][1]
    ultimo = max(cobertura.relatorios)
    faixas = parametros.faixas_dias
    rotulos = _rotulos(produtos)
    por_faixa = [{"itens": 0, "capital": _ZERO} for _ in faixas]
    excedente_faixas = [{"itens": 0, "capital": _ZERO} for _ in faixas]
    itens_parados, itens_excedentes = [], []
    capital_total = _ZERO
    sem_custo = com_lacuna = sem_estoque_informado = 0
    for chave, por_ano in produtos.items():
        if chave in triagem.a_confirmar:
            continue
        estoque = triagem_cenario._estoque(por_ano)
        if estoque is None:
            sem_estoque_informado += 1
            continue
        if estoque <= 0:
            continue
        custo_medio = _custo_medio(por_ano)
        if custo_medio is None:
            sem_custo += 1
            continue
        capital = Decimal(estoque) * custo_medio
        capital_total += capital
        ultima = _ultima_venda(por_ano)
        if ultima is None:
            continue
        dias = _dias_cobertos_depois(ultima, periodos)
        corridos = max(0, (fim_da_cobertura - ultima).days)
        lacuna = dias < corridos
        posicao = _faixa(dias, faixas)
        if posicao is not None:
            com_lacuna += lacuna
            por_faixa[posicao]["itens"] += 1
            por_faixa[posicao]["capital"] += capital
            itens_parados.append((capital, chave, dias, lacuna, estoque, custo_medio, posicao))
            continue
        # Ainda vende: estoque para quantos dias no ritmo do último ano?
        linha_ultima = por_ano.get(ultimo)
        if linha_ultima is None or linha_ultima["quantidade"] <= 0:
            continue
        ritmo = Decimal(linha_ultima["quantidade"]) / Decimal(cobertura.dias[ultimo])
        dias_de_estoque = Decimal(estoque) / ritmo
        if dias_de_estoque <= parametros.cobertura_alvo_dias:
            continue
        excesso = (Decimal(estoque) - ritmo * parametros.cobertura_alvo_dias) * custo_medio
        posicao_excedente = _faixa(int(dias_de_estoque), faixas)
        if posicao_excedente is not None:
            excedente_faixas[posicao_excedente]["itens"] += 1
            excedente_faixas[posicao_excedente]["capital"] += excesso
        itens_excedentes.append((excesso, chave, dias_de_estoque, estoque, custo_medio, capital))

    def faixas_publicas(contagens, sufixo):
        return [{"faixa": _rotulo_da_faixa(i, faixas, sufixo), "dias_minimos": faixas[i],
                 "dias_maximos": faixas[i + 1] - 1 if i + 1 < len(faixas) else None,
                 "itens": contagens[i]["itens"], "capital": _dinheiro(contagens[i]["capital"])}
                for i in range(len(faixas))]

    acima_de = {}
    for i, minimo in enumerate(faixas):
        acima_de[str(minimo)] = {"itens": sum(f["itens"] for f in por_faixa[i:]),
                                 "capital": _dinheiro(sum((f["capital"] for f in por_faixa[i:]), _ZERO))}
    itens_parados.sort(key=lambda item: (-item[0], item[1]))
    itens_excedentes.sort(key=lambda item: (-item[0], item[1]))
    capital_parado = sum((f["capital"] for f in por_faixa), _ZERO)
    capital_excedente = sum((item[0] for item in itens_excedentes), _ZERO)
    return {
        "fim_da_cobertura": fim_da_cobertura.isoformat(),
        "capital_em_estoque": _dinheiro(capital_total),
        "parado": {
            "faixas": faixas_publicas(por_faixa, "dias sem venda"),
            "acima_de": acima_de,
            "itens": sum(f["itens"] for f in por_faixa),
            "capital": _dinheiro(capital_parado),
            "participacao_no_estoque": _fracao(capital_parado, capital_total),
            "itens_com_lacuna": com_lacuna,
            "lista": [{**_identidade(produtos[chave], rotulos), "bloco": triagem.blocos.get(chave),
                       "dias_sem_venda": dias, "com_lacuna": lacuna, "estoque": float(estoque),
                       "custo_medio": _dinheiro(custo_medio), "capital": _dinheiro(capital),
                       "faixa": _rotulo_da_faixa(posicao, faixas, "dias sem venda")}
                      for capital, chave, dias, lacuna, estoque, custo_medio, posicao
                      in itens_parados[:parametros.itens_listados]],
        },
        "excedente": {
            "cobertura_alvo_dias": parametros.cobertura_alvo_dias,
            "faixas": faixas_publicas(excedente_faixas, "dias de estoque"),
            "itens": len(itens_excedentes),
            "capital_liberavel": _dinheiro(capital_excedente),
            "lista": [{**_identidade(produtos[chave], rotulos), "bloco": triagem.blocos.get(chave),
                       "dias_de_estoque": int(dias_de_estoque), "estoque": float(estoque),
                       "custo_medio": _dinheiro(custo_medio), "capital_em_estoque": _dinheiro(capital),
                       "capital_liberavel": _dinheiro(excesso)}
                      for excesso, chave, dias_de_estoque, estoque, custo_medio, capital
                      in itens_excedentes[:parametros.itens_listados]],
        },
        "itens_sem_custo": sem_custo,
        "itens_sem_estoque_informado": sem_estoque_informado,
    }


# ---------------------------------------------------------------------------
# 3. Simulador de liberação de caixa (Urgência)
# ---------------------------------------------------------------------------
def simular_liquidacao(linhas: list, cobertura, triagem,
                       parametros: ParametrosInteligencia = PARAMETROS_PADRAO) -> dict:
    produtos = _por_produto(linhas)
    rotulos = _rotulos(produtos)
    receita = capital = _ZERO
    fora = {"a_confirmar": 0, "sem_estoque": 0, "sem_custo": 0, "sem_preco": 0}
    escolhidos = []
    for chave, por_ano in produtos.items():
        if triagem.blocos.get(chave) != "urgencia":
            continue
        if chave in triagem.a_confirmar:
            fora["a_confirmar"] += 1
            continue
        estoque = triagem_cenario._estoque(por_ano)
        if estoque is None or estoque <= 0:
            fora["sem_estoque"] += 1
            continue
        custo_medio, preco_medio = _custo_medio(por_ano), _preco_medio(por_ano)
        if custo_medio is None:
            fora["sem_custo"] += 1
            continue
        if preco_medio is None:
            fora["sem_preco"] += 1
            continue
        receita_item = Decimal(estoque) * preco_medio
        capital_item = Decimal(estoque) * custo_medio
        receita += receita_item
        capital += capital_item
        escolhidos.append((receita_item, chave, estoque, preco_medio, custo_medio, capital_item))
    escolhidos.sort(key=lambda item: (-item[0], item[1]))
    fracao = parametros.fracao_liquidada
    cenarios = []
    for desconto in parametros.descontos:
        caixa = receita * fracao * (_UM - desconto)
        cenarios.append({"desconto": float(desconto), "fracao_liquidada": float(fracao), "caixa": _dinheiro(caixa),
                         "resultado_contra_o_custo": _dinheiro(caixa - capital * fracao)})
    equilibrio = (_UM - capital / receita) if receita > 0 else None
    return {
        "escopo": "urgencia",
        "itens": len(escolhidos),
        "itens_fora": fora,
        "receita_a_preco_medio": _dinheiro(receita),
        "capital_ao_custo": _dinheiro(capital),
        "desconto_de_equilibrio": (round(float(equilibrio), 4) if equilibrio is not None else None),
        "cenarios": cenarios,
        "lista": [{**_identidade(produtos[chave], rotulos), "estoque": float(estoque),
                   "preco_medio": _dinheiro(preco_medio), "custo_medio": _dinheiro(custo_medio),
                   "receita_a_preco_medio": _dinheiro(receita_item), "capital_ao_custo": _dinheiro(capital_item)}
                  for receita_item, chave, estoque, preco_medio, custo_medio, capital_item
                  in escolhidos[:parametros.itens_listados]],
    }


# ---------------------------------------------------------------------------
# 4. Auditoria de preço pelo markup de referência
# ---------------------------------------------------------------------------
def auditar_precos(linhas: list, cobertura, triagem,
                   parametros: ParametrosInteligencia = PARAMETROS_PADRAO) -> dict:
    produtos = _por_produto(linhas)
    rotulos = _rotulos(produtos)
    ultimo = max(cobertura.relatorios)
    conflito_confiavel = not cobertura.cabecalho_ambiguo
    elegiveis = {}
    for chave, por_ano in produtos.items():
        linha = por_ano.get(ultimo)
        if linha is None or chave in triagem.a_confirmar:
            continue
        venda, custo, quantidade = linha["faturamento_com_custo"], linha["custo_total"], linha["quantidade_com_custo"]
        if quantidade <= 0 or custo <= 0 or venda <= 0:
            continue
        if conflito_confiavel and linha.get("conflito_cadastral"):
            continue
        elegiveis[chave] = (linha, triagem_cenario._categoria(por_ano))
    por_categoria: dict = {}
    for chave, (linha, categoria) in elegiveis.items():
        if categoria is not None:
            por_categoria.setdefault(categoria, []).append(linha)
    referencias = {}
    for categoria, membros in por_categoria.items():
        if len(membros) < parametros.itens_minimos_markup:
            continue
        agregado = (sum((l["faturamento_com_custo"] for l in membros), _ZERO)
                    / sum((l["custo_total"] for l in membros), _ZERO)) - _UM
        mediana = statistics.median([l["faturamento_com_custo"] / l["custo_total"] - _UM for l in membros])
        referencias[categoria] = (min(agregado, mediana), "agregado" if agregado <= mediana else "mediana",
                                  len(membros))
    informado = parametros.markup_informado
    total_diferenca = total_ja_contado = _ZERO
    abaixo, avaliados, acima = [], 0, 0
    sem_referencia = {"itens": 0, "venda": _ZERO}
    categorias_saida: dict = {}
    for chave, (linha, categoria) in elegiveis.items():
        if informado is not None:
            markup, origem = informado, "informado"
        elif categoria in referencias:
            markup, origem, _n = referencias[categoria]
        else:
            sem_referencia["itens"] += 1
            sem_referencia["venda"] += linha["faturamento_com_custo"]
            continue
        avaliados += 1
        venda, custo, quantidade = linha["faturamento_com_custo"], linha["custo_total"], linha["quantidade_com_custo"]
        preco = venda / quantidade
        custo_medio = custo / quantidade
        preco_markup = custo_medio * (_UM + markup)
        tolerancia = max(_CENTAVO, preco_markup * parametros.tolerancia_preco)
        if preco_markup - preco <= tolerancia:
            if preco > preco_markup:
                acima += 1
            continue
        diferenca = custo * (_UM + markup) - venda
        ja_contado = min(diferenca, linha["recuperavel"])
        total_diferenca += diferenca
        total_ja_contado += ja_contado
        soma = categorias_saida.setdefault(categoria, {"itens": 0, "diferenca": _ZERO, "markup": markup, "origem": origem})
        soma["itens"] += 1
        soma["diferenca"] += diferenca
        abaixo.append((diferenca, chave, preco, custo_medio, markup, origem, preco_markup, quantidade, ja_contado))
    abaixo.sort(key=lambda item: (-item[0], item[1]))
    return {
        "base": "informado" if informado is not None else "categoria",
        "ano": ultimo,
        "markup_informado": float(informado) if informado is not None else None,
        "itens_avaliados": avaliados,
        "itens_abaixo": len(abaixo),
        "itens_acima": acima,
        "diferenca_total": _dinheiro(total_diferenca),
        "ja_contado_no_recuperavel": _dinheiro(total_ja_contado),
        "adicional": _dinheiro(total_diferenca - total_ja_contado),
        "sem_referencia": {"itens": sem_referencia["itens"], "venda": _dinheiro(sem_referencia["venda"])},
        "categorias": sorted(
            ({"categoria": rotulos.get(c, c) if c is not None else None, "itens": s["itens"],
              "markup_referencia": round(float(s["markup"]), 4), "origem": s["origem"],
              "diferenca": _dinheiro(s["diferenca"])} for c, s in categorias_saida.items()),
            key=lambda item: (-item["diferenca"], str(item["categoria"]))),
        "lista": [{**_identidade(produtos[chave], rotulos), "quantidade": float(quantidade),
                   "preco_praticado": _dinheiro(preco), "custo_medio": _dinheiro(custo_medio),
                   "markup_item": round(float(preco / custo_medio - _UM), 4),
                   "markup_referencia": round(float(markup), 4), "origem_da_referencia": origem,
                   "preco_pelo_markup": _dinheiro(preco_markup), "diferenca_por_unidade": _dinheiro(preco_markup - preco),
                   "diferenca": _dinheiro(diferenca), "ja_contado_no_recuperavel": _dinheiro(ja_contado)}
                  for diferenca, chave, preco, custo_medio, markup, origem, preco_markup, quantidade, ja_contado
                  in abaixo[:parametros.itens_listados]],
    }


# ---------------------------------------------------------------------------
# Payload e resumo gravado
# ---------------------------------------------------------------------------
DEFINICOES = {
    "estoque_parado_por_idade": ("Itens com estoque e custo informados há pelo menos N dias sem venda (piso: dias "
                                 "cobertos por relatório depois da última venda, até o fim da cobertura). Capital = "
                                 "estoque x custo médio do ano mais recente com custo."),
    "estoque_excedente": ("Itens que ainda vendem com estoque para mais dias que a cobertura-alvo, no ritmo do último "
                          "ano; o capital liberável é o estoque acima da cobertura-alvo, ao custo. As faixas de dias "
                          "de estoque começam na primeira faixa de dias: o item entre a cobertura-alvo e ela entra "
                          "só no total."),
    "simulador": ("Itens da Urgência com estoque, custo e preço: receita do estoque ao preço médio praticado e capital ao "
                  "custo. Cada cenário aplica o desconto e a fração do estoque vendida; é uma projeção, não garantia de "
                  "venda nem sugestão de desconto."),
    "auditoria_de_preco": ("Último ano, vendas com custo: preço praticado médio contra o custo médio x (1 + markup de "
                           "referência). Referência: o menor entre o markup agregado da categoria e a mediana dos itens "
                           "dela, ou o markup informado pelo consultor. Não considera demanda, concorrência nem "
                           "promoção. Não é lucro; a parte já contada no potencial recuperável não se soma."),
    "estoque": "Saldo que o relatório de vendas imprimiu na última venda do produto: não é inventário físico.",
}


def calcular(linhas: list, cobertura, triagem, meta: dict,
             parametros: ParametrosInteligencia = PARAMETROS_PADRAO) -> dict:
    """A seção "inteligencia" do payload do consultor. Sem a coluna de estoque em nenhum relatório do
    envio, o estoque e o simulador ficam nulos (zero ali seria "nada parado", o que não se sabe)."""
    sem_estoque = cobertura.com_estoque is not None and not any(cobertura.com_estoque.values())
    return {
        "versao": VERSAO_INTELIGENCIA,
        "parametros": parametros.para_dict(),
        "estoque": None if sem_estoque else estoque_parado_por_idade(linhas, cobertura, triagem, meta, parametros),
        "simulador": None if sem_estoque else simular_liquidacao(linhas, cobertura, triagem, parametros),
        "auditoria_de_preco": auditar_precos(linhas, cobertura, triagem, parametros),
        "definicoes": DEFINICOES,
    }


def resumo(inteligencia: dict) -> dict:
    """O que fica gravado no lead (resumo_inteligencia_json): só números, nenhum nome de produto nem
    de categoria, porque fica depois do expurgo das linhas (30 dias). A lista de leads o mostra sem
    refazer nada. Sem a coluna de estoque, os campos de estoque e da Urgência ficam nulos."""
    estoque, simulador, auditoria = inteligencia["estoque"], inteligencia["simulador"], inteligencia["auditoria_de_preco"]
    parado = estoque["parado"]["acima_de"] if estoque is not None else None
    return {
        "versao": VERSAO_INTELIGENCIA,
        "com_estoque": estoque is not None,
        "capital_em_estoque": estoque["capital_em_estoque"] if estoque is not None else None,
        "capital_parado": {chave: valores["capital"] for chave, valores in parado.items()} if parado else None,
        "itens_parados": {chave: valores["itens"] for chave, valores in parado.items()} if parado else None,
        "capital_excedente_liberavel": estoque["excedente"]["capital_liberavel"] if estoque is not None else None,
        "urgencia_receita_a_preco_medio": simulador["receita_a_preco_medio"] if simulador is not None else None,
        "urgencia_capital_ao_custo": simulador["capital_ao_custo"] if simulador is not None else None,
        "urgencia_itens_com_estoque": simulador["itens"] if simulador is not None else None,
        "diferenca_de_preco_adicional": auditoria["adicional"],
        "itens_abaixo_do_preco_de_referencia": auditoria["itens_abaixo"],
    }
