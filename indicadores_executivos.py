"""Indicadores executivos do Raio-X: o nó "indicadores_executivos" do resultado, com 21 indicadores.

Roda no processo isolado do ETL, como o Raio-X (raio_x._combinar chama calcular): não importa banco,
Flask nem config e devolve só números, rótulos fixos e, nas listas, o nome e o código do produto. Parte
do que o Raio-X já leu e somou (as somas por produto do envio e por produto e relatório, e os resumos dos
relatórios). O que só existe linha a linha (número da venda, CPF/CNPJ do cliente, desconto, data e hora)
é somado por resumo_das_linhas, que raio_x._resumir chama para cada relatório ou fatia mensal; do
processo isolado só sai o agregado. O CPF/CNPJ vira um resumo criptográfico ali mesmo e só serve para
contar clientes: nem ele nem o resumo saem do processo.

Modos (campo "modo"):
  - "analitico": todo relatório do envio é venda item a item (raio_x._e_venda_item_a_item). Habilita os
    indicadores por venda, que pedem também o número da venda em todas as linhas (ticket médio, itens
    por venda e recompra), e o mapa de calor por dia da semana (e hora, quando o relatório traz);
  - "acumulado": há relatório resumido por produto. Os indicadores por venda saem com o status
    "requer_item_a_item"; os de estoque, cadastro e margem valem nos dois modos, quando o relatório
    traz a coluna.

Regras comuns:
  - Custo a confirmar (custo a partir de raio_x.RAZAO_CUSTO_IMPLAUSIVEL vezes a venda com custo, a mesma
    conta em centavos do Raio-X) é erro de cadastro, não prejuízo: o item fica fora de margem, CMV, giro,
    GMROI, curva ABC por margem, margem negativa, margem abaixo do mínimo e evolução do custo, e aparece
    só como "custo a confirmar" (Saúde do Cadastro). O CMV traz o custo dele à parte.
  - Cada indicador traz "status": "calculado" ou o motivo de não sair (STATUS). Sem a coluna ou o dado,
    o indicador não sai: nenhum número é estimado.
  - Margem bruta: venda com custo menos o custo informado (não é lucro: não desconta despesas, impostos
    nem taxas). A margem líquida pede a alíquota de impostos, que ainda não é parâmetro do sistema: sai
    com "requer_parametro". É outra coisa que a margem líquida da auditoria do Raio-X
    (auditoria.margem_liquida_*), que é a margem com bonificações e devoluções.
  - Evolução do custo (indicador 9): o custo unitário registrado nas vendas, por produto, entre o
    primeiro e o último mês com custo. É o custo do cadastro do PDV, não o preço da nota de compra; o
    saving (8) pede o relatório de compras e sai com "requer_relatorio_de_compras".
  - Meses (AAAA-MM) só dos relatórios mensais (início e fim no mesmo mês). O mapa por produto e mês e a
    margem negativa por mês marcam "impacto_q4" nos meses 10, 11 e 12.
  - Listas com no máximo LIMITE_LISTA itens; o mapa por produto e mês, LIMITE_SKUS_DO_MAPA produtos.

O processo pai confere o nó inteiro antes de gravar (valido: chaves fixas, status conhecidos, números
finitos e texto livre só no nome e no código do produto).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
import math
import re

import numpy as np
import pandas as pd

from etl_processor import _classificar_curva_abc, _converter_serie_monetaria, data_da_linha

VERSAO = "indicadores-2026-11.1"
CHAVES = (
    "faturamento", "ticket_medio", "margem", "giro_estoque", "mix", "itens_por_venda", "recompra", "saving",
    "evolucao_custo", "cmv", "curva_abc_faturamento", "curva_abc_volume", "curva_abc_lucro", "margem_negativa",
    "margem_abaixo_minimo", "indice_descontos", "ruptura", "encalhados", "cobertura_estoque", "gmroi", "mapa_calor",
)
STATUS = frozenset({
    "calculado", "sem_dados", "requer_item_a_item", "requer_custo", "requer_estoque", "requer_cpf_cnpj",
    "requer_parametro", "requer_relatorio_de_compras", "requer_mais_de_um_mes", "requer_periodo",
    "requer_coluna_de_desconto", "requer_cadastro_de_produtos",
})
MODOS = frozenset({"analitico", "acumulado"})
MARGEM_MINIMA = 0.15
DIAS_ENCALHE = 60
LIMITE_LISTA = 50
LIMITE_SKUS_DO_MAPA = 20
MESES_Q4 = frozenset({10, 11, 12})
LIMIAR_VARIACAO_CUSTO = 0.05
# Faixas de dias de cobertura (até o limite, inclusive).
FAIXAS_DE_COBERTURA = (("ate_15", 15), ("de_16_a_60", 60), ("de_61_a_180", 180), ("acima_de_180", None))
LIMITE_NOME = 120
# Os indicadores que dependem do estoque (sem_dados quando dois arquivos com estoque cobrem o mesmo período).
_INDICADORES_DE_ESTOQUE = ("giro_estoque", "ruptura", "encalhados", "cobertura_estoque", "gmroi")
LIMITE_CODIGO = 60


# ---------------------------------------------------------------------------
# Números e textos de saída
# ---------------------------------------------------------------------------
def _dinheiro(valor) -> float:
    numero = float(valor)
    return round(numero, 2) if math.isfinite(numero) else 0.0


def _fracao(parte, todo):
    parte, todo = float(parte), float(todo)
    if not (math.isfinite(parte) and math.isfinite(todo)) or todo <= 0:
        return None
    return round(parte / todo, 4)


def _razao(parte, todo, casas=4):
    parte, todo = float(parte), float(todo)
    if not (math.isfinite(parte) and math.isfinite(todo)) or todo <= 0:
        return None
    return round(parte / todo, casas)


def _quantidade(valor) -> float:
    numero = float(valor)
    return round(numero, 3) if math.isfinite(numero) else 0.0


def _texto(valor, limite):
    """Uma linha só, sem caractere de controle, com teto de tamanho (ou None)."""
    if valor is None or (isinstance(valor, float) and not math.isfinite(valor)):
        return None
    texto = " ".join("".join(c for c in str(valor) if c.isprintable() or c.isspace()).split())
    return texto[:limite].strip() or None


def _centavos(valor) -> int:
    return int(round(float(valor) * 100))


def _mes(resumo):
    """"AAAA-MM" do relatório mensal (início e fim no mesmo mês) ou None."""
    inicio, fim = resumo.get("inicio"), resumo.get("fim")
    if inicio is None or fim is None or (inicio.year, inicio.month) != (fim.year, fim.month):
        return None
    return f"{inicio.year:04d}-{inicio.month:02d}"


def _q4(mes: str) -> bool:
    return int(mes[5:7]) in MESES_Q4


# ---------------------------------------------------------------------------
# Linha a linha (dentro do processo isolado)
# ---------------------------------------------------------------------------
_RE_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z./-]*")
_RE_HORA = re.compile(r"(?:^|[ T])(\d{1,2}):(\d{2})")
_RE_INTEIRO_DO_XLSX = re.compile(r"\d+\.0+")


def numero_da_venda(valor):
    """O número da venda da linha: o primeiro trecho da célula que tem algarismo ("P9-00001" em
    "P9-00001  NFC9-00001", o pedido antes da nota; "123" em "NF 123"). Sem algarismo: None."""
    if valor is None or (isinstance(valor, float) and not math.isfinite(valor)):
        return None
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    texto = str(valor).strip()
    if _RE_INTEIRO_DO_XLSX.fullmatch(texto):
        texto = texto.split(".")[0]
    for trecho in _RE_TOKEN.findall(texto):
        if any(caractere.isdigit() for caractere in trecho):
            return trecho.upper()[:40]
    return None


def _cpf_valido(digitos: str) -> bool:
    if len(digitos) != 11 or digitos == digitos[0] * 11:
        return False
    for posicao in (9, 10):
        soma = sum(int(digitos[i]) * (posicao + 1 - i) for i in range(posicao))
        if soma * 10 % 11 % 10 != int(digitos[posicao]):
            return False
    return True


_PESOS_CNPJ = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def _cnpj_valido(caracteres: str) -> bool:
    """CNPJ de 14 posições com os dois dígitos verificadores certos. Vale também para o CNPJ alfanumérico (IN
    RFB 2.229/2024, emitido desde julho de 2026): cada posição vale o código ASCII menos 48 (o algarismo vale
    ele mesmo), com os mesmos pesos; os dois verificadores continuam algarismos."""
    if (len(caracteres) != 14 or caracteres == caracteres[0] * 14
            or not _RE_CNPJ.fullmatch(caracteres)):
        return False
    valores = [ord(caractere) - 48 for caractere in caracteres]
    for posicao in (12, 13):
        soma = sum(valores[i] * _PESOS_CNPJ[i + 13 - posicao] for i in range(posicao))
        resto = soma % 11
        if (0 if resto < 2 else 11 - resto) != valores[posicao]:
            return False
    return True


_RE_CNPJ = re.compile(r"[0-9A-Z]{12}[0-9]{2}")
# A forma do CNPJ alfanumérico, com ou sem a pontuação ("12.ABC.345/01DE-35", "12ABC34501DE35"). O texto sem essa
# forma segue para os algarismos: "CPF 315.942.594-02" também tem 14 posições com as letras, e o mesmo CPF, com e
# sem o prefixo, tem de ser o mesmo cliente.
# Aceita também o espaço nos separadores e o traço tipográfico antes dos verificadores ("12 ABC 345 01DE 35",
# "12.ABC.345/01DE–35"); o prefixo de letras ("CPF 315...") continua fora.
_RE_FORMA_DO_CNPJ = re.compile(r"[0-9A-Z]{2}[.\s]?[0-9A-Z]{3}[.\s]?[0-9A-Z]{3}[/.\s]?[0-9A-Z]{4}\s*"
                               r"[-.\u2010-\u2015\u2212]?\s*[0-9]{2}", re.IGNORECASE)


def cliente_identificado(valor):
    """Um resumo (sha256) do CPF ou CNPJ válido da linha, ou None: vazio, "consumidor final", dígitos
    verificadores errados ou números repetidos ("000.000.000-00") não identificam cliente. O número da
    célula numérica do XLSX que perdeu o zero à esquerda volta a ter 11 ou 14 dígitos; o CNPJ alfanumérico
    vale como está. Só serve para contar clientes dentro do processo isolado."""
    if valor is None:
        return None
    if isinstance(valor, float):
        if not math.isfinite(valor) or not valor.is_integer():
            return None
        valor = int(valor)
    texto = str(valor).strip()
    if _RE_INTEIRO_DO_XLSX.fullmatch(texto):
        texto = texto.split(".")[0]
    # O CNPJ alfanumérico, só com a forma dele; o resto segue para os algarismos.
    if _RE_FORMA_DO_CNPJ.fullmatch(texto):
        alfanumerico = re.sub(r"[^0-9A-Za-z]", "", texto).upper()
        if not alfanumerico.isdigit() and _cnpj_valido(alfanumerico):
            return hashlib.sha256(alfanumerico.encode("ascii")).hexdigest()[:24]
    digitos = re.sub(r"\D", "", texto)
    if not digitos or len(digitos) > 14:
        return None
    if len(digitos) <= 11 and _cpf_valido(digitos.zfill(11)):
        digitos = digitos.zfill(11)
    elif _cnpj_valido(digitos.zfill(14)):
        digitos = digitos.zfill(14)
    else:
        return None
    return hashlib.sha256(digitos.encode("ascii")).hexdigest()[:24]


def hora_da_celula(valor):
    """A hora (0 a 23) da célula: "14:35", "2025-10-03 14:35:00", "03/10/2025 14:35" ou a fração do dia da
    célula de hora do Excel. Sem hora legível: None."""
    if valor is None:
        return None
    if isinstance(valor, float):
        if math.isfinite(valor) and 0 <= valor < 1:
            return int(valor * 24 + 1e-9) % 24
        return None
    achado = _RE_HORA.search(str(valor).strip())
    if achado is None:
        return None
    hora, minuto = int(achado.group(1)), int(achado.group(2))
    return hora if hora < 24 and minuto < 60 else None


def _loja(valor) -> str:
    """A loja da linha como texto comparável (o mesmo critério de raio_x._texto_da_unidade)."""
    if valor is None or (isinstance(valor, float) and not math.isfinite(valor)):
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    texto = " ".join(str(valor).split()).casefold()
    return texto[:-2] if texto.endswith(".0") and texto[:-2].isdigit() else texto


def _varias_lojas(df: pd.DataFrame) -> bool:
    """O relatório traz duas lojas ou mais com nome (raio_x._marcar_as_lojas decide no arquivo inteiro, antes das
    fatias mensais; sem a marca, vale o próprio quadro)."""
    marca = df.attrs.get("varias_lojas")
    if marca is not None:
        return bool(marca)
    if "_unidade" not in df.columns:
        return False
    lojas = df["_unidade"].map(_loja)
    if lojas[lojas != ""].nunique() >= 2:
        return True
    # A loja do cupom guarda também a do primeiro item que saiu da venda (o cancelado que trazia a loja).
    if "_loja_do_cupom" in df.columns:
        do_cupom = df["_loja_do_cupom"].astype(str)
        return do_cupom[do_cupom != ""].nunique() >= 2
    return False


def resumo_das_linhas(df: pd.DataFrame, venda: pd.Series, faturamento: pd.Series, *, csv: bool,
                      datas_validas: bool = True) -> dict:
    """Agregados por linha de um relatório (ou fatia mensal) para os indicadores, com chaves começadas por
    "_" (raio_x não as serializa). `venda`: as vendas analisadas; `faturamento`: o valor de cada uma, em
    centavos, no índice delas. `datas_validas`: a coluna de data é a data de cada venda (venda item a item, com
    toda data legível e dentro do período; raio_x._datas_validas). Sem isso, a data não entra em nada: num
    resumo por produto, "Data" é outra coisa (o cadastro, a última compra)."""
    linhas = df.loc[venda]
    validas = ~df["_devolucao"] if "_devolucao" in df.columns else pd.Series(True, index=df.index)
    saida = {
        "_listados_sem_venda": frozenset(df.loc[validas & (df["quantidade"] == 0), "_prod_norm"]),
        "_documentos": None, "_clientes": None,
        "_linhas_com_cliente": 0, "_tem_coluna_cliente": "_cliente" in df.columns, "_desconto": None,
        "_calor": None, "_hora_disponivel": False, "_ultima_venda": None, "_tem_data": False,
    }
    if linhas.empty:
        return saida
    documentos = None
    if "_documento" in df.columns:
        numeros = linhas["_documento"].map(numero_da_venda)
        if numeros.notna().all():
            # Cada loja tem a sua numeração: com duas lojas ou mais no arquivo, a venda é o número naquela loja, e a
            # célula da loja em branco logo abaixo do mesmo número (a loja só no primeiro item do cupom) é a do
            # cupom, também quando esse item saiu da venda (etl_processor._loja_do_cupom, no quadro inteiro). Com
            # uma loja só, a loja não entra: a célula em branco ou mesclada é dela.
            if "_unidade" in df.columns and _varias_lojas(df):
                if "_loja_do_cupom" in linhas.columns:
                    lojas = linhas["_loja_do_cupom"].astype(str)
                else:
                    lojas = linhas["_unidade"].map(_loja)
                    trecho = (~numeros.eq(numeros.shift())).cumsum()
                    lojas = lojas.mask(lojas == "").groupby(trecho).ffill().fillna("")
                numeros = lojas + "|" + numeros.astype(str)
            documentos = numeros
            saida["_documentos"] = frozenset(numeros)
    datas = None
    if "_data" in df.columns and datas_validas:
        lidas = {texto: data_da_linha(texto) for texto in linhas["_data"].unique()}
        convertidas = linhas["_data"].map(lidas)
        if convertidas.notna().all():
            datas = convertidas
            saida["_tem_data"] = True
            saida["_ultima_venda"] = datas.groupby(linhas["_prod_norm"]).max()
    if datas is not None:
        if "_hora" in df.columns:
            horas = linhas["_hora"].map(hora_da_celula)
            hora_disponivel = bool(horas.notna().all())
        else:
            horas = linhas["_data"].map(hora_da_celula)
            # A data da planilha vem com 00:00:00: hora zero em todas as linhas não é hora da venda.
            hora_disponivel = bool(horas.notna().all() and (horas != 0).any())
        tabela = pd.DataFrame({
            "dia": datas.map(lambda dia: dia.weekday()).astype(int),
            "hora": horas.astype(int) if hora_disponivel else -1,
            "faturamento": faturamento.astype(float),
        }, index=linhas.index)
        somas = tabela.groupby(["dia", "hora"])["faturamento"].sum()
        vendas = documentos.groupby([tabela["dia"], tabela["hora"]]).nunique() if documentos is not None else None
        saida["_calor"] = {(int(dia), int(hora)): [float(valor), int(vendas.get((dia, hora), 0)) if vendas is not None
                                                   else None]
                           for (dia, hora), valor in somas.items()}
        saida["_hora_disponivel"] = hora_disponivel
    if "_cliente" in df.columns and (documentos is not None or datas is not None):
        # Em lista, e não Series.map: o pandas trocaria o None por NaN, que viraria um "cliente".
        identificados = [cliente_identificado(valor) for valor in linhas["_cliente"].tolist()]
        transacoes = documentos.tolist() if documentos is not None else [dia.isoformat() for dia in datas.tolist()]
        clientes: dict = defaultdict(set)
        for cliente, transacao in zip(identificados, transacoes):
            if cliente is not None:
                clientes[cliente].add(transacao)
        saida["_clientes"] = dict(clientes)
        saida["_linhas_com_cliente"] = sum(1 for cliente in identificados if cliente is not None)
    if "_desconto" in df.columns:
        # Desconto que não é valor em reais ("10%") ou absurdo derruba só este indicador (sem_dados), e não o Raio-X.
        qualidade = Counter()
        descontos = _converter_serie_monetaria(linhas["_desconto"], qualidade, csv).fillna(0.0).abs().round(2)
        valores = descontos.to_numpy(float)
        if qualidade.get("invalidos") or not np.isfinite(valores).all() or (valores.size and valores.max() >= _TETO_DO_DESCONTO):
            saida["_desconto_invalido"] = True
        else:
            saida["_desconto"] = float(descontos.sum())
    return saida


# Maior desconto aceito numa linha: acima disto a célula é lixo (e o nó passaria do teto do contrato).
_TETO_DO_DESCONTO = 1e9


# ---------------------------------------------------------------------------
# Do envio
# ---------------------------------------------------------------------------
def _em_ordem(resumos: list) -> list:
    """Em ordem cronológica; o relatório sem período, de data desconhecida, vem primeiro (o mais antigo, como em
    raio_x._ler_envio): o saldo dele não passa por cima do saldo do mês mais recente."""
    return sorted(resumos, key=lambda r: (r["inicio"] is not None, r["inicio"] or date.min, r["indice"]))


def _estoque_do_envio(resumos: list):
    """(último estoque informado por produto ou None sem a coluna, nomes dos produtos com estoque, fotos).

    O estoque de cada relatório (raio_x: o último saldo de cada produto e loja, somado por produto, acumulado nas
    fatias do arquivo) atualiza o estado do envio em ordem cronológica. Cada relatório com a coluna é uma foto do
    estado depois dele: a foto leva também o saldo de quem não apareceu naquele relatório."""
    estado, nomes, fotos, tem = {}, {}, [], False
    for resumo in _em_ordem(resumos):
        tabela = resumo.get("_estoques")
        if tabela is None:
            continue
        tem = True
        for produto, valor in tabela.items():
            if valor is not None and math.isfinite(float(valor)):
                estado[produto] = float(valor)
        nomes.update(resumo.get("_nomes_estoque") or {})
        fotos.append(dict(estado))
    return (pd.Series(estado, dtype=float) if tem else None), nomes, fotos


def _estoques_sobrepostos(resumos: list) -> bool:
    """Dois arquivos com estoque cobrem o mesmo período (duas lojas enviadas em arquivos separados, por exemplo):
    o faturamento e o CMV somam os dois, mas o estoque da rede não é conhecido."""
    datados = sorted((r for r in resumos if r.get("_estoques") is not None and r["inicio"] is not None
                      and r["fim"] is not None), key=lambda r: (r["inicio"], r["indice"]))
    dono = None
    for atual in datados:
        if (dono is not None and atual["inicio"] <= dono["fim"]
                and atual.get("arquivo", atual["indice"]) != dono.get("arquivo", dono["indice"])):
            return True
        if dono is None or atual["fim"] > dono["fim"]:
            dono = atual
    return False


def _intervalos_cobertos(resumos: list, fim_ref) -> list:
    """[[início, fim], ...] dos períodos dos relatórios, cortados em fim_ref, com os contíguos e os sobrepostos
    unidos (o mesmo critério de avaliacao_motor._periodos_cobertos). O mês sem relatório fica de fora."""
    pares = sorted((r["inicio"], min(r["fim"], fim_ref)) for r in resumos
                   if r["inicio"] is not None and r["fim"] is not None and r["inicio"] <= fim_ref)
    unidos: list = []
    for inicio, fim in pares:
        if unidos and (inicio - unidos[-1][1]).days <= 1:
            unidos[-1][1] = max(unidos[-1][1], fim)
        else:
            unidos.append([inicio, fim])
    return unidos


def _dias(intervalos: list) -> int:
    return sum((fim - inicio).days + 1 for inicio, fim in intervalos)


def _dias_cobertos_depois(intervalos: list, dia) -> int:
    """Dias cobertos por relatório depois de `dia` (sem contar o próprio dia)."""
    total = 0
    for inicio, fim in intervalos:
        if fim > dia:
            total += (fim - max(inicio, dia + timedelta(days=1))).days + 1
    return total


def _custo_unitario(plausiveis: pd.DataFrame) -> pd.Series:
    """Custo médio por unidade de cada produto com custo (custo / quantidade com custo)."""
    base = plausiveis[(plausiveis["custo_total"] > 0) & (plausiveis["quantidade_com_custo"] > 0)]
    return base["custo_total"] / base["quantidade_com_custo"]


def _item(produtos: pd.DataFrame, produto, nomes_extra=None) -> dict:
    if produto in produtos.index:
        nome, codigo = produtos.at[produto, "nome"], produtos.at[produto, "codigo"]
    else:
        nome, codigo = (nomes_extra or {}).get(produto) or produto, None
    return {"produto": _texto(nome, LIMITE_NOME) or _texto(produto, LIMITE_NOME),
            "codigo": _texto(codigo, LIMITE_CODIGO) if isinstance(codigo, str) else None}


def _faturamento(produtos, resumos) -> dict:
    valor = float(produtos["faturamento"].sum())
    devolucoes = sum(float(r.get("devolucoes", 0.0)) for r in resumos)
    return {"status": "calculado", "valor": _dinheiro(valor), "devolucoes": _dinheiro(devolucoes),
            "liquido_de_devolucoes": _dinheiro(valor - devolucoes)}


def _transacoes(resumos, modo):
    """Os números de venda do envio, ou None quando o envio não é analítico ou algum relatório com venda
    não traz o número em todas as linhas."""
    if modo != "analitico":
        return None
    com_venda = [r for r in resumos if r.get("_linhas_venda", 0) > 0]
    if not com_venda or any(r.get("_documentos") is None for r in com_venda):
        return None
    # A venda é o número no arquivo: dois arquivos (duas lojas, ou períodos sobrepostos, que o Raio-X do cliente
    # soma com aviso) contam vendas diferentes, como somam o faturamento.
    return frozenset((r.get("arquivo", r["indice"]), documento) for r in com_venda for documento in r["_documentos"])


def _ticket(produtos, transacoes) -> dict:
    if not transacoes:
        return {"status": "requer_item_a_item", "valor": None, "transacoes": None}
    return {"status": "calculado", "valor": _razao(produtos["faturamento"].sum(), len(transacoes), 2),
            "transacoes": len(transacoes)}


def _itens_por_venda(produtos, transacoes) -> dict:
    if not transacoes:
        return {"status": "requer_item_a_item", "valor": None, "linhas_por_venda": None, "transacoes": None}
    return {"status": "calculado", "valor": _razao(produtos["quantidade"].sum(), len(transacoes), 3),
            "linhas_por_venda": _razao(produtos["linhas"].sum(), len(transacoes), 3), "transacoes": len(transacoes)}


def _margem(plausiveis, a_confirmar_itens) -> dict:
    com_custo = float(plausiveis["faturamento_com_custo"].sum())
    custo = float(plausiveis["custo_total"].sum())
    base = {"liquida_status": "requer_parametro", "liquida_valor": None, "liquida_percentual": None,
            "aliquota_impostos": None, "itens_custo_a_confirmar": a_confirmar_itens}
    if custo <= 0:
        return {"status": "requer_custo", "bruta_valor": None, "bruta_percentual": None,
                "faturamento_com_custo": None, **base}
    return {"status": "calculado", "bruta_valor": _dinheiro(com_custo - custo),
            "bruta_percentual": _fracao(com_custo - custo, com_custo), "faturamento_com_custo": _dinheiro(com_custo),
            **base}


def _cmv(plausiveis, implausiveis) -> dict:
    custo = float(plausiveis["custo_total"].sum())
    fora = float(implausiveis["custo_total"].sum())
    if custo <= 0:
        # Sem custo plausível, o CMV não sai (calcular troca por sem_dados quando a coluna existe); o custo dos
        # itens a confirmar continua à parte.
        return {"status": "requer_custo", "valor": None, "custo_a_confirmar": _dinheiro(fora) if fora > 0 else None,
                "itens_custo_a_confirmar": int(implausiveis.shape[0])}
    return {"status": "calculado", "valor": _dinheiro(custo), "custo_a_confirmar": _dinheiro(fora),
            "itens_custo_a_confirmar": int(implausiveis.shape[0])}


def _giro(plausiveis, custo_unitario, estoques, dias) -> dict:
    """CMV / estoque atual a custo; anualizado pelos dias cobertos por relatório (`dias`)."""
    vazio = {"valor": None, "anualizado": None, "cmv": None, "estoque_a_custo": None, "dias_do_periodo": dias,
             "itens_com_estoque_sem_custo": None}
    if estoques is None:
        return {"status": "requer_estoque", **vazio}
    cmv = float(plausiveis["custo_total"].sum())
    if cmv <= 0:
        return {"status": "requer_custo", **vazio}
    positivos = estoques[estoques > 0]
    com_custo = positivos.index.intersection(custo_unitario.index)
    estoque_a_custo = float((positivos[com_custo] * custo_unitario[com_custo]).sum())
    if estoque_a_custo <= 0:
        return {"status": "sem_dados", **vazio}
    giro = cmv / estoque_a_custo
    return {"status": "calculado", "valor": round(giro, 4),
            "anualizado": round(giro * 365 / dias, 4) if dias else None, "cmv": _dinheiro(cmv),
            "estoque_a_custo": _dinheiro(estoque_a_custo), "dias_do_periodo": dias,
            "itens_com_estoque_sem_custo": int(len(positivos) - len(com_custo))}


def _mix(produtos, resumos) -> dict:
    vendidos = int(produtos.shape[0])
    # Só o relatório que lista produto sem venda (o "vendas x estoque", com quantidade 0 e o saldo) dá a base
    # dos cadastrados: os vendidos mais os listados sem venda nenhuma no envio. A linha só de devolução não conta.
    sem_venda = frozenset().union(*(r.get("_listados_sem_venda") or frozenset() for r in resumos))
    sem_venda = sem_venda - frozenset(produtos.index)
    if sem_venda:
        cadastrados = vendidos + len(sem_venda)
        return {"status": "calculado", "skus_vendidos": vendidos, "skus_cadastrados": cadastrados,
                "percentual_sobre_cadastrados": _fracao(vendidos, cadastrados), "cadastrados_status": "calculado"}
    return {"status": "calculado", "skus_vendidos": vendidos, "skus_cadastrados": None,
            "percentual_sobre_cadastrados": None, "cadastrados_status": "requer_cadastro_de_produtos"}


def _recompra(resumos, modo) -> dict:
    vazio = {"taxa": None, "clientes_identificados": None, "clientes_recorrentes": None,
             "pct_linhas_identificadas": None}
    if modo != "analitico":
        return {"status": "requer_item_a_item", **vazio}
    com_venda = [r for r in resumos if r.get("_linhas_venda", 0) > 0]
    # Falta o CPF/CNPJ em algum relatório com venda: é o que pedir ao lojista (e não a venda item a item).
    if not com_venda or any(not r.get("_tem_coluna_cliente") for r in com_venda):
        return {"status": "requer_cpf_cnpj", **vazio}
    if any(r.get("_clientes") is None for r in com_venda):
        return {"status": "requer_item_a_item", **vazio}
    clientes: dict = defaultdict(set)
    for resumo in com_venda:
        arquivo = resumo.get("arquivo", resumo["indice"])
        for cliente, transacoes in resumo["_clientes"].items():
            clientes[cliente] |= {(arquivo, transacao) for transacao in transacoes}
    linhas = sum(r.get("_linhas_venda", 0) for r in com_venda)
    if not clientes:
        return {"status": "sem_dados", **vazio, "pct_linhas_identificadas": _fracao(0, linhas)}
    recorrentes = sum(1 for transacoes in clientes.values() if len(transacoes) > 1)
    return {"status": "calculado", "taxa": _fracao(recorrentes, len(clientes)), "clientes_identificados": len(clientes),
            "clientes_recorrentes": recorrentes,
            "pct_linhas_identificadas": _fracao(sum(r.get("_linhas_com_cliente", 0) for r in com_venda), linhas)}


def _evolucao_custo(produtos, por_arquivo, resumos, a_confirmar_idx) -> dict:
    vazio = {"meses_comparados": None, "itens_comparados": None, "itens_em_alta": None, "itens_em_queda": None,
             "variacao_ponderada": None, "lista": None}
    meses = {r["indice"]: _mes(r) for r in resumos if r.get("tem_custo")}
    meses = {indice: mes for indice, mes in meses.items() if mes is not None}
    base = por_arquivo[por_arquivo["arquivo"].isin(list(meses)) & (por_arquivo["quantidade_com_custo"] > 0)
                       & (por_arquivo["custo_total"] > 0) & ~por_arquivo["produto"].isin(a_confirmar_idx)].copy()
    if base.empty:
        if not any(r.get("tem_custo") for r in resumos):
            return {"status": "requer_custo", **vazio}
        return {"status": "sem_dados" if len(set(meses.values())) >= 2 else "requer_mais_de_um_mes", **vazio}
    base["mes"] = base["arquivo"].map(meses)
    distintos = sorted(base["mes"].unique())
    if len(distintos) < 2:
        return {"status": "requer_mais_de_um_mes", **vazio}
    mensal = base.groupby(["produto", "mes"])[["custo_total", "quantidade_com_custo"]].sum().reset_index()
    mensal = mensal.sort_values(["produto", "mes"])
    primeiro = mensal.groupby("produto").head(1).set_index("produto")
    ultimo = mensal.groupby("produto").tail(1).set_index("produto")
    comparaveis = ultimo.index[ultimo["mes"] != primeiro.loc[ultimo.index, "mes"]]
    if not len(comparaveis):
        return {"status": "sem_dados", **vazio, "meses_comparados": len(distintos)}
    inicial = primeiro.loc[comparaveis, "custo_total"] / primeiro.loc[comparaveis, "quantidade_com_custo"]
    final = ultimo.loc[comparaveis, "custo_total"] / ultimo.loc[comparaveis, "quantidade_com_custo"]
    quantidade_final = ultimo.loc[comparaveis, "quantidade_com_custo"]
    # Arredondada como sai na lista: 12,00 para 11,40 é -5,00% (e não -4,9999...%) e conta como queda.
    variacao = (final / inicial - 1).round(4)
    impacto = (final - inicial) * quantidade_final
    ordem = impacto.abs().sort_values(ascending=False, kind="stable").index[:LIMITE_LISTA]
    lista = [{**_item(produtos, produto), "mes_inicial": primeiro.at[produto, "mes"], "mes_final": ultimo.at[produto, "mes"],
              "custo_unitario_inicial": _dinheiro(inicial[produto]), "custo_unitario_final": _dinheiro(final[produto]),
              "variacao": round(float(variacao[produto]), 4), "impacto": _dinheiro(impacto[produto])}
             for produto in ordem]
    return {"status": "calculado", "meses_comparados": len(distintos), "itens_comparados": int(len(comparaveis)),
            "itens_em_alta": int((variacao >= LIMIAR_VARIACAO_CUSTO).sum()),
            "itens_em_queda": int((variacao <= -LIMIAR_VARIACAO_CUSTO).sum()),
            "variacao_ponderada": _razao((final * quantidade_final).sum() - (inicial * quantidade_final).sum(),
                                         (inicial * quantidade_final).sum()),
            "lista": lista}


def _curva(produtos, valores: pd.Series, base: str, fora: int = 0) -> dict:
    """Curva ABC 80/15/5 (A até 80% do acumulado, B até 95%; o líder é sempre A), a mesma regra da
    análise mensal (etl_processor._classificar_curva_abc), sobre os valores positivos."""
    positivos = valores[valores > 0]
    vazio = {"base": base, "total": None, "classes": None, "itens": None, "itens_fora_da_curva": fora}
    if positivos.empty:
        return {"status": "sem_dados", **vazio}
    grupo = pd.DataFrame({"_prod_norm": positivos.index, "fat": positivos.to_numpy(float),
                          "nome": positivos.index})
    curva = _classificar_curva_abc(grupo)
    if not curva["produtos"]:
        return {"status": "sem_dados", **vazio}
    # A soma de cada classe sai dos valores exatos (a da análise mensal é dinheiro, com 2 casas; a quantidade a
    # granel tem 3).
    soma_da_classe: dict = defaultdict(float)
    for linha in curva["produtos"]:
        soma_da_classe[linha["classe"]] += float(positivos[linha["nome"]])
    classes = [{"classe": linha["classe"], "itens": linha["qtd_produtos"],
                "valor": (_quantidade(soma_da_classe[linha["classe"]]) if base == "quantidade"
                          else _dinheiro(soma_da_classe[linha["classe"]])),
                "participacao": round(linha["perc_faturamento"] / 100, 4)} for linha in curva["resumo"]]
    itens = []
    for linha in curva["produtos"][:LIMITE_LISTA]:
        valor = linha["faturamento"]
        itens.append({**_item(produtos, linha["nome"]), "classe": linha["classe"],
                      "valor": _quantidade(valor) if base == "quantidade" else _dinheiro(valor),
                      "participacao": round(linha["perc_individual"] / 100, 4),
                      "acumulado": round(linha["perc_acumulado"] / 100, 4)})
    total = float(positivos.sum())
    return {"status": "calculado", "base": base, "total": _quantidade(total) if base == "quantidade" else _dinheiro(total),
            "classes": classes, "itens": itens, "itens_fora_da_curva": fora + int((valores <= 0).sum())}


def _margem_negativa(produtos, plausiveis, implausiveis, por_arquivo, resumos, a_confirmar_idx) -> dict:
    """Duas leituras, com nomes diferentes: `itens`, `perda` e `lista` olham o produto no envio inteiro (custo
    acima da venda somando todos os meses, como a auditoria do Raio-X); `por_mes`, `matriz`, `perda_q4` e
    `perda_nos_meses` olham produto e mês. O produto negativo num mês e positivo no envio entra só na segunda:
    `perda_q4` nunca passa de `perda_nos_meses`, mas pode passar de `perda`."""
    com_custo = plausiveis[plausiveis["custo_total"] > 0]
    custo_c = (com_custo["custo_total"] * 100).round()
    venda_c = (com_custo["faturamento_com_custo"] * 100).round()
    negativos = com_custo[custo_c > venda_c]
    base_a_confirmar = {"itens_custo_a_confirmar": int(implausiveis.shape[0]),
                        "faturamento_custo_a_confirmar": _dinheiro(implausiveis["faturamento_com_custo"].sum())}
    if com_custo.empty:
        return {"status": "requer_custo", "itens": None, "faturamento_com_custo": None, "custo_total": None,
                "perda": None, "lista": None, "por_mes": None, "matriz": None, "perda_q4": None, "perda_nos_meses": None,
                "relatorios_nao_mensais": None, **base_a_confirmar}
    perda = negativos["custo_total"] - negativos["faturamento_com_custo"]
    ordem = perda.sort_values(ascending=False, kind="stable").index[:LIMITE_LISTA]
    lista = [{**_item(produtos, produto), "faturamento_com_custo": _dinheiro(negativos.at[produto, "faturamento_com_custo"]),
              "custo_total": _dinheiro(negativos.at[produto, "custo_total"]), "perda": _dinheiro(perda[produto])}
             for produto in ordem]
    # Produto x mês (refatoração 5): a venda de cada mês com custo acima da venda, fora os itens a confirmar.
    meses = {r["indice"]: _mes(r) for r in resumos}
    nao_mensais = sum(1 for mes in meses.values() if mes is None)
    base = por_arquivo[por_arquivo["arquivo"].map(meses).notna() & (por_arquivo["quantidade_com_custo"] > 0)
                       & ~por_arquivo["produto"].isin(a_confirmar_idx)].copy()
    por_mes, matriz, perda_q4 = [], [], 0.0
    if not base.empty:
        base["mes"] = base["arquivo"].map(meses)
        base = base[(base["custo_total"] * 100).round() > (base["faturamento_com_custo"] * 100).round()]
    if not base.empty:
        base["perda_mes"] = base["custo_total"] - base["faturamento_com_custo"]
        celulas = base.groupby(["produto", "mes"])["perda_mes"].sum().reset_index()
        for mes, grupo in celulas.groupby("mes", sort=True):
            q4 = _q4(mes)
            por_mes.append({"mes": mes, "impacto_q4": q4, "itens": int(grupo.shape[0]),
                            "perda": _dinheiro(grupo["perda_mes"].sum())})
            if q4:
                perda_q4 += float(grupo["perda_mes"].sum())
        totais = celulas.groupby("produto")["perda_mes"].sum().sort_values(ascending=False, kind="stable")
        for produto in totais.index[:LIMITE_LISTA]:
            do_produto = celulas[celulas["produto"] == produto].sort_values("mes")
            matriz.append({**_item(produtos, produto), "perda_total": _dinheiro(totais[produto]),
                           "meses": [{"mes": mes, "perda": _dinheiro(valor), "impacto_q4": _q4(mes)}
                                     for mes, valor in zip(do_produto["mes"], do_produto["perda_mes"])]})
    return {"status": "calculado", "itens": int(negativos.shape[0]),
            "faturamento_com_custo": _dinheiro(negativos["faturamento_com_custo"].sum()),
            "custo_total": _dinheiro(negativos["custo_total"].sum()), "perda": _dinheiro(perda.sum()), "lista": lista,
            "por_mes": por_mes, "matriz": matriz, "perda_q4": _dinheiro(perda_q4),
            "perda_nos_meses": _dinheiro(sum(mes["perda"] for mes in por_mes)),
            "relatorios_nao_mensais": nao_mensais, **base_a_confirmar}


def _margem_abaixo_minimo(produtos, plausiveis) -> dict:
    com_custo = plausiveis[(plausiveis["custo_total"] > 0) & (plausiveis["faturamento_com_custo"] > 0)]
    if com_custo.empty:
        return {"status": "requer_custo", "itens": None, "faturamento_com_custo": None, "margem_minima": MARGEM_MINIMA,
                "lista": None}
    venda_c = (com_custo["faturamento_com_custo"] * 100).round().astype("int64")
    custo_c = (com_custo["custo_total"] * 100).round().astype("int64")
    minimo = int(round(MARGEM_MINIMA * 100))
    # Margem de 0% até abaixo do mínimo (a negativa é o indicador 14), em inteiros: 100 x margem < mínimo x venda.
    abaixo = com_custo[(custo_c <= venda_c) & (100 * (venda_c - custo_c) < minimo * venda_c)]
    ordem = abaixo["faturamento_com_custo"].sort_values(ascending=False, kind="stable").index[:LIMITE_LISTA]
    lista = [{**_item(produtos, produto), "faturamento_com_custo": _dinheiro(abaixo.at[produto, "faturamento_com_custo"]),
              "margem_percentual": _fracao(abaixo.at[produto, "faturamento_com_custo"] - abaixo.at[produto, "custo_total"],
                                           abaixo.at[produto, "faturamento_com_custo"])}
             for produto in ordem]
    return {"status": "calculado", "itens": int(abaixo.shape[0]),
            "faturamento_com_custo": _dinheiro(abaixo["faturamento_com_custo"].sum()), "margem_minima": MARGEM_MINIMA,
            "lista": lista}


def _descontos(resumos) -> dict:
    vazio = {"valor": None, "descontos": None, "faturamento_bruto": None}
    if any(r.get("_desconto_invalido") for r in resumos):
        # A coluna existe, mas traz percentual ("10%") ou valor absurdo: o índice não sai.
        return {"status": "sem_dados", **vazio}
    com_coluna = [r for r in resumos if r.get("_desconto") is not None]
    if not com_coluna:
        return {"status": "requer_coluna_de_desconto", **vazio}
    desconto = sum(r["_desconto"] for r in com_coluna)
    liquido = sum(float(r["faturamento"]) for r in com_coluna)
    return {"status": "calculado", "valor": _fracao(desconto, liquido + desconto), "descontos": _dinheiro(desconto),
            "faturamento_bruto": _dinheiro(liquido + desconto)}


def _ruptura(produtos, estoques) -> dict:
    if estoques is None:
        return {"status": "requer_estoque", "itens": None, "faturamento": None, "lista": None}
    vendidos = estoques.index.intersection(produtos.index)
    zerados = [produto for produto in vendidos if estoques[produto] <= 0]
    tabela = produtos.loc[zerados]
    ordem = tabela["faturamento"].sort_values(ascending=False, kind="stable").index[:LIMITE_LISTA]
    lista = [{**_item(produtos, produto), "estoque": _quantidade(estoques[produto]),
              "faturamento": _dinheiro(tabela.at[produto, "faturamento"]),
              "quantidade": _quantidade(tabela.at[produto, "quantidade"])} for produto in ordem]
    return {"status": "calculado", "itens": len(zerados), "faturamento": _dinheiro(tabela["faturamento"].sum()),
            "lista": lista}


def _ultima_venda(por_arquivo, resumos, fim_ref) -> tuple[dict, bool]:
    """(última venda conhecida de cada produto, se é a data exata em todos os relatórios). Sem a data da
    venda, vale o fim do relatório em que o produto vendeu: os dias sem venda são um piso."""
    fins = {r["indice"]: (min(r["fim"], fim_ref) if r["fim"] is not None else None) for r in resumos}
    com_data = {r["indice"]: r.get("_ultima_venda") is not None for r in resumos}
    exata = all(com_data[r["indice"]] for r in resumos if r.get("_linhas_venda", 0) > 0)
    ultima: dict = {}
    for resumo in resumos:
        tabela = resumo.get("_ultima_venda")
        if tabela is not None:
            for produto, dia in tabela.items():
                if dia is not None and (produto not in ultima or dia > ultima[produto]):
                    ultima[produto] = dia
    sem_data = por_arquivo[~por_arquivo["arquivo"].map(com_data).fillna(False).astype(bool)]
    for produto, indice in zip(sem_data["produto"], sem_data["arquivo"]):
        dia = fins.get(indice)
        if dia is not None and (produto not in ultima or dia > ultima[produto]):
            ultima[produto] = dia
    return ultima, exata


def _encalhados(produtos, por_arquivo, resumos, estoques, nomes_estoque, custo_unitario, inicio, fim_ref,
                intervalos) -> dict:
    """Estoque > 0 e mais de DIAS_ENCALHE dias cobertos por relatório sem venda: só os dias que algum relatório
    cobre contam (no mês sem relatório o produto pode ter vendido), e o encalhe é sempre certo."""
    vazio = {"itens": None, "valor_a_custo": None, "itens_sem_custo": None, "dias_sem_venda_minimo": DIAS_ENCALHE,
             "data_de_referencia": None, "base_dos_dias": None, "lista": None}
    if estoques is None:
        return {"status": "requer_estoque", **vazio}
    if fim_ref is None or inicio is None:
        return {"status": "requer_periodo", **vazio}
    ultima, exata = _ultima_venda(por_arquivo, resumos, fim_ref)
    # Produto que vendeu num relatório sem período: a última venda dele é desconhecida (pode ser a mais recente),
    # e ele fica de fora, mesmo com venda datada em outro relatório.
    sem_periodo = {r["indice"] for r in resumos if r["inicio"] is None}
    vendeu_sem_periodo = frozenset(por_arquivo.loc[por_arquivo["arquivo"].isin(list(sem_periodo)), "produto"])
    encalhados = []
    for produto, estoque in estoques.items():
        if estoque <= 0 or produto in vendeu_sem_periodo:
            continue
        if produto in ultima:
            dias = _dias_cobertos_depois(intervalos, ultima[produto])
            sem_venda_no_periodo = False
        else:
            dias = _dias(intervalos)  # nenhuma venda nos dias cobertos do envio
            sem_venda_no_periodo = True
        if dias > DIAS_ENCALHE:
            unitario = custo_unitario.get(produto)
            encalhados.append((produto, estoque, dias, sem_venda_no_periodo,
                               float(estoque * unitario) if unitario is not None and unitario > 0 else None))
    # Do mais parado para o menos, e não pelo valor a custo: a ordem (e o corte em LIMITE_LISTA) não pode
    # revelar o custo a quem o plano não mostra o valor (raio_x_servico.visao).
    encalhados.sort(key=lambda item: (-item[2], -item[1], str(item[0])))
    lista = [{**_item(produtos, produto, nomes_estoque), "estoque": _quantidade(estoque), "dias_sem_venda": int(dias),
              "sem_venda_no_periodo": sem_venda, "valor_a_custo": _dinheiro(valor) if valor is not None else None}
             for produto, estoque, dias, sem_venda, valor in encalhados[:LIMITE_LISTA]]
    return {"status": "calculado", "itens": len(encalhados),
            "valor_a_custo": _dinheiro(sum(item[4] for item in encalhados if item[4] is not None)),
            "itens_sem_custo": sum(1 for item in encalhados if item[4] is None),
            "dias_sem_venda_minimo": DIAS_ENCALHE, "data_de_referencia": fim_ref.isoformat(),
            "base_dos_dias": "data_da_venda" if exata else "fim_do_relatorio", "lista": lista}


def _cobertura(produtos, estoques, dias) -> dict:
    vazio = {"itens": None, "mediana_dias": None, "dias_do_periodo": dias, "faixas": None, "lista": None}
    if estoques is None:
        return {"status": "requer_estoque", **vazio}
    if not dias:
        return {"status": "requer_periodo", **vazio}
    comuns = estoques.index.intersection(produtos.index)
    base = pd.DataFrame({"estoque": estoques[comuns], "quantidade": produtos.loc[comuns, "quantidade"]})
    base = base[(base["estoque"] > 0) & (base["quantidade"] > 0)]
    if base.empty:
        return {"status": "sem_dados", **vazio}
    base["vmd"] = base["quantidade"] / dias
    base["dias"] = base["estoque"] / base["vmd"]
    faixas, anterior = {}, 0.0
    for nome, limite in FAIXAS_DE_COBERTURA:
        if limite is None:
            faixas[nome] = int((base["dias"] > anterior).sum())
        else:
            faixas[nome] = int(((base["dias"] > anterior) & (base["dias"] <= limite)).sum())
            anterior = float(limite)
    ordem = base["dias"].sort_values(ascending=False, kind="stable").index[:LIMITE_LISTA]
    lista = [{**_item(produtos, produto), "estoque": _quantidade(base.at[produto, "estoque"]),
              "venda_media_diaria": round(float(base.at[produto, "vmd"]), 4), "dias": round(float(base.at[produto, "dias"]), 1)}
             for produto in ordem]
    return {"status": "calculado", "itens": int(base.shape[0]), "mediana_dias": round(float(base["dias"].median()), 1),
            "dias_do_periodo": dias, "faixas": faixas, "lista": lista}


def _gmroi(plausiveis, custo_unitario, fotos) -> dict:
    """Margem bruta / estoque médio a custo: a média das fotos do estoque (_estoque_do_envio) quando cada foto
    traz todo produto com estoque e custo do estado final; senão (relatório que só lista quem vendeu, arquivo
    fatiado), o estoque final, com `base` dizendo qual valeu."""
    vazio = {"valor": None, "percentual": None, "margem_bruta": None, "estoque_medio_a_custo": None, "base": None,
             "fotos_de_estoque": None}
    if not fotos:
        return {"status": "requer_estoque", **vazio}
    com_custo = float(plausiveis["faturamento_com_custo"].sum())
    custo = float(plausiveis["custo_total"].sum())
    if custo <= 0:
        return {"status": "requer_custo", **vazio}

    def a_custo(foto):
        return sum(saldo * float(custo_unitario[produto]) for produto, saldo in foto.items()
                   if saldo > 0 and produto in custo_unitario.index)

    final = fotos[-1]
    valorizados = [produto for produto, saldo in final.items() if saldo > 0 and produto in custo_unitario.index]
    completas = len(fotos) > 1 and all(all(produto in foto for produto in valorizados) for foto in fotos)
    valores = [a_custo(foto) for foto in fotos] if completas else [a_custo(final)]
    medio = sum(valores) / len(valores)
    if medio <= 0:
        return {"status": "sem_dados", **vazio, "fotos_de_estoque": len(fotos)}
    razao = (com_custo - custo) / medio
    return {"status": "calculado", "valor": round(razao, 4), "percentual": round(razao * 100, 2),
            "margem_bruta": _dinheiro(com_custo - custo), "estoque_medio_a_custo": _dinheiro(medio),
            "base": "estoque_medio" if completas else "estoque_final", "fotos_de_estoque": len(fotos)}


def _mapa_calor(produtos, por_arquivo, resumos, modo) -> dict:
    vazio = {"tipo": None, "hora_disponivel": None, "celulas": None, "meses": None, "impacto_q4": None, "linhas": None}
    com_venda = [r for r in resumos if r.get("_linhas_venda", 0) > 0]
    if modo == "analitico" and com_venda and all(r.get("_calor") is not None for r in com_venda):
        hora = all(r.get("_hora_disponivel") for r in com_venda)
        somas: dict = defaultdict(lambda: [0.0, 0])
        vendas_conhecidas = all(valor[1] is not None for r in com_venda for valor in r["_calor"].values())
        for resumo in com_venda:
            for (dia, hora_da_celula_), (valor, vendas) in resumo["_calor"].items():
                chave = (dia, hora_da_celula_ if hora else -1)
                somas[chave][0] += valor
                somas[chave][1] += vendas or 0
        celulas = [{"dia_semana": dia, "hora": hora_da_celula_ if hora else None, "faturamento": _dinheiro(valor),
                    "vendas": vendas if vendas_conhecidas else None}
                   for (dia, hora_da_celula_), (valor, vendas) in sorted(somas.items())]
        return {"status": "calculado", **vazio, "tipo": "dia_semana_hora" if hora else "dia_semana",
                "hora_disponivel": hora, "celulas": celulas}
    meses = {r["indice"]: _mes(r) for r in resumos}
    # O eixo traz todo mês coberto por relatório mensal, também o mês sem venda (como as fatias).
    todos = sorted({mes for mes in meses.values() if mes is not None})
    base = por_arquivo[por_arquivo["arquivo"].map(meses).notna()].copy()
    if base.empty or len(todos) < 2:
        return {"status": "requer_mais_de_um_mes", **vazio}
    base["mes"] = base["arquivo"].map(meses)
    tabela = base.groupby(["produto", "mes"])["faturamento"].sum().unstack(fill_value=0.0).reindex(columns=todos,
                                                                                                fill_value=0.0)
    ordem = tabela.sum(axis=1).sort_values(ascending=False, kind="stable").index[:LIMITE_SKUS_DO_MAPA]
    linhas = [{**_item(produtos, produto), "valores": [_dinheiro(valor) for valor in tabela.loc[produto].tolist()]}
              for produto in ordem]
    return {"status": "calculado", **vazio, "tipo": "sku_mes", "meses": todos,
            "impacto_q4": [_q4(mes) for mes in todos], "linhas": linhas}


def calcular(produtos: pd.DataFrame, a_confirmar: pd.Series, por_arquivo: pd.DataFrame, resumos: list, inicio, fim,
             *, hoje: date | None = None) -> dict:
    """O nó indicadores_executivos do Raio-X. `produtos`: as somas por produto do envio (raio_x._combinar);
    `a_confirmar`: a marca de custo a confirmar de cada produto; `por_arquivo` e `resumos`: os de
    raio_x._ler_envio; `inicio` e `fim`: o período do envio (ou None)."""
    hoje = date.today() if hoje is None else hoje
    fim_ref = min(fim, hoje) if fim is not None else None
    # Os dias cobertos por relatório (sem os meses que nenhum relatório cobre), base da venda média diária, da
    # anualização do giro e dos dias sem venda.
    intervalos = _intervalos_cobertos(resumos, fim_ref) if fim_ref is not None else []
    dias = _dias(intervalos) or None
    modo = "analitico" if resumos and all(r.get("_item_a_item") for r in resumos) else "acumulado"
    a_confirmar = a_confirmar.reindex(produtos.index, fill_value=False).astype(bool)
    plausiveis, implausiveis = produtos[~a_confirmar], produtos[a_confirmar]
    a_confirmar_idx = list(implausiveis.index)
    custo_unitario = _custo_unitario(plausiveis)
    # O produto sem venda no envio não tem custo nas vendas: o custo das tabelas auxiliares da ingestão em lote
    # (raio_x._completar_ancora) valoriza o saldo dele.
    auxiliar = {}
    for resumo in resumos:
        auxiliar.update(resumo.get("_custo_unitario_auxiliar") or {})
    if auxiliar:
        extra = pd.Series(auxiliar, dtype=float)
        custo_unitario = custo_unitario.combine_first(extra[~extra.index.isin(produtos.index)])
    estoques, nomes_estoque, fotos = _estoque_do_envio(resumos)
    transacoes = _transacoes(resumos, modo)
    margem_por_produto = plausiveis["faturamento_com_custo"] - plausiveis["custo_total"]
    margem_por_produto = margem_por_produto[plausiveis["custo_total"] > 0]
    indicadores = {
        "faturamento": _faturamento(produtos, resumos),
        "ticket_medio": _ticket(produtos, transacoes),
        "margem": _margem(plausiveis, int(implausiveis.shape[0])),
        "giro_estoque": _giro(plausiveis, custo_unitario, estoques, dias),
        "mix": _mix(produtos, resumos),
        "itens_por_venda": _itens_por_venda(produtos, transacoes),
        "recompra": _recompra(resumos, modo),
        "saving": {"status": "requer_relatorio_de_compras", "valor": None},
        "evolucao_custo": _evolucao_custo(produtos, por_arquivo, resumos, a_confirmar_idx),
        "cmv": _cmv(plausiveis, implausiveis),
        "curva_abc_faturamento": _curva(produtos, produtos["faturamento"], "faturamento"),
        "curva_abc_volume": _curva(produtos, produtos["quantidade"], "quantidade"),
        "curva_abc_lucro": (_curva(produtos, margem_por_produto, "margem_bruta", fora=int(implausiveis.shape[0]))
                            if not margem_por_produto.empty else
                            {"status": "requer_custo", "base": "margem_bruta", "total": None, "classes": None,
                             "itens": None, "itens_fora_da_curva": int(implausiveis.shape[0])}),
        "margem_negativa": _margem_negativa(produtos, plausiveis, implausiveis, por_arquivo, resumos, a_confirmar_idx),
        "margem_abaixo_minimo": _margem_abaixo_minimo(produtos, plausiveis),
        "indice_descontos": _descontos(resumos),
        "ruptura": _ruptura(produtos, estoques),
        "encalhados": _encalhados(produtos, por_arquivo, resumos, estoques, nomes_estoque, custo_unitario, inicio,
                                  fim_ref, intervalos),
        "cobertura_estoque": _cobertura(produtos, estoques, dias),
        "gmroi": _gmroi(plausiveis, custo_unitario, fotos),
        "mapa_calor": _mapa_calor(produtos, por_arquivo, resumos, modo),
    }
    # "requer_custo" é a falta da coluna de custo; com a coluna em algum relatório, mas sem custo que entre na
    # conta (coluna em branco, ou custo só no item a confirmar), o indicador sai "sem_dados".
    if any(r.get("tem_custo") for r in resumos):
        for valor in indicadores.values():
            if valor.get("status") == "requer_custo":
                valor["status"] = "sem_dados"
    # Dois arquivos com estoque no mesmo período (duas lojas em arquivos separados): o faturamento e o CMV somam
    # as lojas, mas o estoque da rede não é conhecido. Os indicadores de estoque não saem.
    if estoques is not None and _estoques_sobrepostos(resumos):
        for chave in _INDICADORES_DE_ESTOQUE:
            indicadores[chave] = {**{campo: None for campo in indicadores[chave]}, "status": "sem_dados"}
    # Um indicador com número fora do que o contrato aceita (uma célula absurda numa coluna acessória) sai
    # "sem_dados", em vez de derrubar o nó inteiro, e com ele o Raio-X ou o lead.
    for chave, valor in list(indicadores.items()):
        if not _seguro(valor, 1, [0], None):
            indicadores[chave] = {**{campo: None for campo in valor}, "status": "sem_dados"}
    return {"versao": VERSAO, "modo": modo,
            "parametros": {"margem_minima": MARGEM_MINIMA, "dias_encalhe": DIAS_ENCALHE, "aliquota_impostos": None,
                           "limite_lista": LIMITE_LISTA},
            "indicadores": indicadores}


# ---------------------------------------------------------------------------
# Conferência no processo pai (contrato)
# ---------------------------------------------------------------------------
_RE_CHAVE = re.compile(r"[a-z0-9_]{1,40}")
_RE_ROTULO = re.compile(r"[A-Za-z0-9_.-]{1,40}")
# Só estes campos levam texto do relatório (nome e código do produto); os demais textos são rótulos fixos.
_TEXTO_DO_RELATORIO = {"produto": LIMITE_NOME, "codigo": LIMITE_CODIGO}
_MAXIMO_DE_NOS = 250_000
_MAXIMO_DA_LISTA = 3_000
_MAIOR_NUMERO = 1e15


def valido(no) -> bool:
    """O nó tem a forma que calcular devolve: chaves fixas, os 21 indicadores com status conhecido,
    números finitos, rótulos curtos e texto livre só no nome e no código do produto. Um processo isolado
    com defeito (ou comprometido) não grava texto qualquer. Qualquer exceção na conferência também é
    recusa (False), e não erro 500."""
    try:
        return _valido(no)
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        return False


def _valido(no) -> bool:
    if not isinstance(no, dict) or set(no) != {"versao", "modo", "parametros", "indicadores"}:
        return False
    if (no["versao"] != VERSAO or not isinstance(no["modo"], str) or no["modo"] not in MODOS
            or not isinstance(no["parametros"], dict)):
        return False
    indicadores = no["indicadores"]
    if not isinstance(indicadores, dict) or set(indicadores) != set(CHAVES):
        return False
    if not all(isinstance(valor, dict) and isinstance(valor.get("status"), str) and valor["status"] in STATUS
               for valor in indicadores.values()):
        return False
    return _seguro(no, 0, [0], None)


def _seguro(valor, profundidade: int, contador: list, chave) -> bool:
    contador[0] += 1
    if contador[0] > _MAXIMO_DE_NOS or profundidade > 8:
        return False
    if valor is None or type(valor) is bool:
        return True
    if type(valor) is int:
        return abs(valor) <= 2 ** 53
    if type(valor) is float:
        return math.isfinite(valor) and abs(valor) < _MAIOR_NUMERO
    if isinstance(valor, str):
        if chave in _TEXTO_DO_RELATORIO:
            return (0 < len(valor) <= _TEXTO_DO_RELATORIO[chave] and bool(valor.strip())
                    and all(caractere.isprintable() for caractere in valor))
        return _RE_ROTULO.fullmatch(valor) is not None
    if isinstance(valor, list):
        return len(valor) <= _MAXIMO_DA_LISTA and all(_seguro(item, profundidade + 1, contador, chave) for item in valor)
    if isinstance(valor, dict):
        return all(isinstance(nome, str) and _RE_CHAVE.fullmatch(nome) is not None
                   and _seguro(item, profundidade + 1, contador, nome) for nome, item in valor.items())
    return False
