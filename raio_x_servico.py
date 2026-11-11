"""Raio-X de entrada no portal: recebe os arquivos, grava o resultado e decide o que cada
plano vê. O cálculo fica em raio_x.py, no processo isolado do ETL (etl_runtime.run_raio_x).

  - Os arquivos vão para um diretório temporário com o mesmo teto de tamanho do upload,
    são validados e lidos no processo isolado e somem com o diretório: só o resultado
    (agregados e listas resumidas) fica no banco, uma linha por empresa.
  - visao() devolve uma cópia do resultado só com o que o plano libera. O dado protegido
    vira None, então nem o HTML nem o JSON levam o valor: o desfoque da tela é desenho
    sobre um marcador, não um filtro sobre o número verdadeiro.
"""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

from werkzeug.utils import secure_filename

from etl_inputs import ETLInputError
from etl_limits import get_limits
import etl_runtime
from extensions import db
from horario import agora_utc
from models import DiagnosticoEntrada
import raio_x
import security_gatekeeper

logger = logging.getLogger(__name__)

_BLOCO = 64 * 1024
# Formatos do Raio-X e da Avaliação de Cenário: os arquivos são lidos no processo isolado e descartados.
# O .xls é o do Excel 97-2003 e o .xml, a Planilha XML 2003 (security_gatekeeper, leitura direta).
EXTENSOES_ACEITAS = ("csv", "xlsx", "xls", "xml")
CAMPOS_MARGEM = ("custo_total", "custo_medio", "margem_valor", "margem_percentual")
CAMPOS_MARGEM_LIQUIDA = ("margem_liquida_valor", "margem_liquida_percentual")
CAMPOS_CMV_LIQUIDO = ("cmv_liquido", "custo_bonificacoes", "custo_devolucoes")
CAMPOS_FATIA = ("faturamento", "itens", "faturamento_sem_custo", "pct_faturamento_sem_custo", "custo_total",
                "margem_valor", "margem_percentual", "perda_abaixo_custo", "vazamento_preco", "recuperavel",
                "devolucoes")
# Indicadores executivos (indicadores_executivos.py): a funcionalidade do plano que libera cada um. None: todo
# plano com Raio-X vê (faturamento, venda, volume, estoque em unidades e o mapa de calor de vendas). Margem,
# CMV, giro e custo seguem as funcionalidades da auditoria do Raio-X; a recompra, a curva de clientes do Ouro.
FUNCIONALIDADE_DO_INDICADOR = {
    "faturamento": None, "ticket_medio": None, "margem": "margem_bruta", "giro_estoque": "diagnostico_cmv",
    "mix": None, "itens_por_venda": None, "recompra": "curva_abc_clientes", "saving": "diagnostico_cmv",
    "evolucao_custo": "alerta_precificacao", "cmv": "diagnostico_cmv", "curva_abc_faturamento": "curva_abc_vendas",
    "curva_abc_volume": "curva_abc_vendas", "curva_abc_lucro": "margem_bruta",
    "margem_negativa": "auditoria_abaixo_custo", "margem_abaixo_minimo": "margem_bruta", "indice_descontos": None,
    "ruptura": "produtos_parados_ruptura", "encalhados": "produtos_parados_ruptura",
    "cobertura_estoque": "produtos_parados_ruptura", "gmroi": "margem_bruta", "mapa_calor": None,
}
# Sem a funcionalidade, o indicador vira {"status": "protegido", "funcionalidade": ...}, com estes campos ainda
# abertos: as contagens de margem negativa e de custo a confirmar, que todo plano vê na auditoria.
_CAMPOS_ABERTOS = {"margem_negativa": ("itens", "itens_custo_a_confirmar")}


def _indicadores_visiveis(indicadores, liberadas: frozenset, corte: date | None):
    """Os indicadores executivos com o que o plano libera; os meses (AAAA-MM) anteriores ao corte do
    histórico saem da margem negativa por mês e do mapa por produto e mês, como as fatias. Raio-X gravado
    antes da raio-x-2026-11.6: None."""
    if not isinstance(indicadores, dict) or not isinstance(indicadores.get("indicadores"), dict):
        return None
    itens = indicadores["indicadores"]
    for chave, valor in list(itens.items()):
        funcionalidade = FUNCIONALIDADE_DO_INDICADOR.get(chave)
        if funcionalidade is not None and funcionalidade not in liberadas and isinstance(valor, dict):
            abertos = {campo: valor.get(campo) for campo in _CAMPOS_ABERTOS.get(chave, ())}
            itens[chave] = {**abertos, "status": "protegido", "funcionalidade": funcionalidade}
    if "diagnostico_cmv" not in liberadas:
        # Valores a custo são do diagnóstico de CMV, também dentro de indicadores de outra funcionalidade: o
        # estoque parado em unidades é do Bronze; a venda com custo dos itens a confirmar (que, com a perda
        # desconsiderada da auditoria, daria o custo deles) e o estoque médio a custo do GMROI, também.
        for chave, campos in (("encalhados", ("valor_a_custo",)), ("margem_negativa", ("faturamento_custo_a_confirmar",)),
                              ("gmroi", ("estoque_medio_a_custo",))):
            indicador = itens.get(chave)
            if isinstance(indicador, dict) and indicador.get("status") != "protegido":
                for campo in campos:
                    if campo in indicador:
                        indicador[campo] = None
        encalhados = itens.get("encalhados")
        if isinstance(encalhados, dict) and encalhados.get("status") != "protegido":
            for item in encalhados.get("lista") or []:
                item["valor_a_custo"] = None
    if corte is None:
        return indicadores
    limite = f"{corte.year:04d}-{corte.month:02d}"
    negativa = itens.get("margem_negativa")
    if isinstance(negativa, dict) and negativa.get("status") == "calculado":
        negativa["por_mes"] = [mes for mes in negativa.get("por_mes") or [] if mes["mes"] >= limite]
        matriz = []
        for linha in negativa.get("matriz") or []:
            meses = [mes for mes in linha["meses"] if mes["mes"] >= limite]
            if meses:
                matriz.append({**linha, "meses": meses, "perda_total": round(sum(mes["perda"] for mes in meses), 2)})
        # Na ordem dos meses que o plano mostra (a perda oculta não pode decidir quem vem primeiro).
        matriz.sort(key=lambda linha: -linha["perda_total"])
        negativa["matriz"] = matriz
        negativa["perda_q4"] = round(sum(mes["perda"] for mes in negativa["por_mes"] if mes["impacto_q4"]), 2)
        if "perda_nos_meses" in negativa:
            negativa["perda_nos_meses"] = round(sum(mes["perda"] for mes in negativa["por_mes"]), 2)
    calor = itens.get("mapa_calor")
    if isinstance(calor, dict) and calor.get("tipo") == "sku_mes" and calor.get("meses"):
        manter = [posicao for posicao, mes in enumerate(calor["meses"]) if mes >= limite]
        calor["meses"] = [calor["meses"][posicao] for posicao in manter]
        calor["impacto_q4"] = [calor["impacto_q4"][posicao] for posicao in manter]
        linhas = []
        for linha in calor.get("linhas") or []:
            valores = [linha["valores"][posicao] for posicao in manter]
            if any(valores):
                linhas.append({**linha, "valores": valores})
        linhas.sort(key=lambda linha: -sum(linha["valores"]))
        calor["linhas"] = linhas
    return indicadores


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------
def _gravar_limitado(arquivo, caminho: Path, limits, numero: int) -> str:
    """Copia no máximo o teto + 1 byte; o Content-Length do cliente não é autoridade."""
    digest, tamanho = hashlib.sha256(), 0
    teto = security_gatekeeper.teto_bytes(limits)
    with caminho.open("wb") as destino:
        caminho.chmod(0o600)
        while True:
            bloco = arquivo.read(min(_BLOCO, teto + 1 - tamanho))
            if not bloco:
                break
            if not isinstance(bloco, bytes):
                raise ETLInputError("upload_io", f"Arquivo {numero}: não foi possível ler o arquivo enviado.")
            tamanho += len(bloco)
            if tamanho > teto:
                raise ETLInputError("file_size", f"Arquivo {numero}: acima do tamanho permitido para processamento.")
            digest.update(bloco)
            destino.write(bloco)
    if not tamanho:
        raise ETLInputError("empty_file", f"Arquivo {numero}: está vazio.")
    return digest.hexdigest()


def processar_envio(arquivos, limits=None, *, executar=None, origem: str = "raio_x") -> tuple[dict, list]:
    """(resultado do Raio-X, nomes seguros dos arquivos), na ordem em que foram enviados.

    `executar` troca a operação do processo isolado (padrão: etl_runtime.run_raio_x; a Avaliação
    de Cenário passa o executor dela, também de etl_runtime) e `origem` marca o log da Baia. O resto da
    entrada (teto, assinatura, recusa de arquivo repetido e diretório temporário) é o mesmo."""
    executar = etl_runtime.run_raio_x if executar is None else executar
    arquivos = [arquivo for arquivo in arquivos if arquivo is not None and arquivo.filename]
    if not arquivos:
        raise ETLInputError("raio_x_vazio", "Selecione ao menos um relatório de vendas (CSV, XLSX ou XLS).")
    if len(arquivos) > raio_x.MAX_ARQUIVOS:
        raise ETLInputError("raio_x_arquivos", f"Envie no máximo {raio_x.MAX_ARQUIVOS} relatórios por vez: "
                            "até 3 anos, um por mês.")
    try:
        limits = get_limits() if limits is None else limits
    except ValueError:
        raise ETLInputError("invalid_limits", "Configuração de processamento inválida.") from None
    nomes, vistos = [], set()
    try:
        with tempfile.TemporaryDirectory(prefix="nexo-raio-x-") as diretorio:
            caminhos = []
            for numero, arquivo in enumerate(arquivos, 1):
                nome = secure_filename(arquivo.filename) or f"arquivo-{numero}"
                extensao = os.path.splitext(nome)[1].lower().lstrip(".")
                if extensao not in EXTENSOES_ACEITAS:
                    raise ETLInputError("unsupported_format", f"Arquivo {numero}: formato não suportado. Envie "
                                        "CSV, XLSX, XLS ou Planilha XML 2003 (.xml).")
                caminho = Path(diretorio) / f"arquivo_{numero:02d}.{extensao}"
                digest = _gravar_limitado(arquivo, caminho, limits, numero)
                # Baia de Segurança: assinatura e extensão antes do processo isolado. Os arquivos
                # do Raio-X não são gravados, então não há cópia neutralizada a liberar; a
                # leitura do motor já neutraliza as células (etl_inputs.read_raw). Por isso a
                # leitura direta aceita também o .xls verdadeiro e a Planilha XML 2003.
                try:
                    security_gatekeeper.inspecionar_arquivo(caminho, extensao, rotulo=f"Arquivo {numero}",
                                                            leitura_direta=True)
                except security_gatekeeper.SecurityGatekeeperError as erro:
                    security_gatekeeper.registrar_recusa(erro, sha256=digest, tamanho=caminho.stat().st_size,
                                                         extensao=extensao, origem=origem)
                    raise
                if digest in vistos:
                    raise ETLInputError("raio_x_repetido", f"Arquivo {numero}: é igual a outro arquivo enviado.")
                vistos.add(digest)
                caminhos.append(str(caminho))
                nomes.append(nome[:80])
            resultado = executar(caminhos, limits)
    except OSError:
        raise ETLInputError("upload_io", "Não foi possível preparar os arquivos enviados. Tente novamente.") from None
    return resultado, nomes


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------
def salvar(id_empresa: int, id_usuario: int, resultado: dict, recomendacao: dict, nomes: list) -> DiagnosticoEntrada:
    """Grava (sem commit) o Raio-X da empresa no lugar do anterior."""
    linha = db.session.get(DiagnosticoEntrada, id_empresa, with_for_update=True, populate_existing=True)
    if linha is None:
        linha = DiagnosticoEntrada(id_empresa=id_empresa)
        db.session.add(linha)
    periodo = resultado["periodo"]
    indice = resultado["saude"]["indice"]
    linha.id_usuario = id_usuario
    linha.gerado_em = agora_utc()
    linha.versao_regras = resultado["versao"]
    # Os arquivos enviados (desde a raio-x-2026-11.6, "arquivos" do resultado traz um relatório por mês do arquivo
    # fatiado).
    linha.arquivos = len(nomes) or len(resultado["arquivos"])
    linha.periodo_inicio = date.fromisoformat(periodo["inicio"]) if periodo["inicio"] else None
    linha.periodo_fim = date.fromisoformat(periodo["fim"]) if periodo["fim"] else None
    linha.indice_saude = None if indice is None else Decimal(str(indice)).quantize(Decimal("0.01"))
    linha.valor_recuperavel = Decimal(str(resultado["recuperavel"]["total"]))
    linha.plano_recomendado = recomendacao["plano"]
    linha.resultado = json.dumps({**resultado, "recomendacao": recomendacao, "nomes_arquivos": nomes},
                                 ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return linha


def ultimo(id_empresa: int) -> tuple[DiagnosticoEntrada, dict] | None:
    """(linha, resultado) do último Raio-X da empresa; None sem Raio-X ou com JSON ilegível."""
    linha = db.session.get(DiagnosticoEntrada, id_empresa)
    if linha is None:
        return None
    try:
        dados = json.loads(linha.resultado)
    except ValueError:
        logger.warning("Raio-X da empresa %s ilegível: ignorado.", id_empresa)
        return None
    # O Raio-X gravado antes da raio-x-2026-11.6 não tem os indicadores executivos e continua valendo.
    if not isinstance(dados, dict) or not raio_x.CHAVES_OBRIGATORIAS <= set(dados):
        return None
    return linha, dados


# ---------------------------------------------------------------------------
# O que cada plano vê
# ---------------------------------------------------------------------------
def _corte_historico(fatias: list, limite_meses: int | None) -> date | None:
    """Primeiro dia do mês mais antigo que o plano mostra (contado do mês mais recente enviado)."""
    fins = [date.fromisoformat(fatia["fim"]) for fatia in fatias if fatia.get("fim")]
    if not limite_meses or not fins:
        return None
    recente = max(fins)
    meses = recente.year * 12 + recente.month - 1 - (limite_meses - 1)
    return date(meses // 12, meses % 12 + 1, 1)


def mostra_todas_as_fatias(fatias: list, limite_meses: int | None) -> bool:
    """O plano com esse limite de histórico mostra todos os relatórios datados enviados."""
    corte = _corte_historico(fatias, limite_meses)
    return corte is None or all(not fatia.get("fim") or date.fromisoformat(fatia["fim"]) >= corte
                                for fatia in fatias)


def visao(dados: dict, liberadas: frozenset, limite_meses: int | None) -> dict:
    """Cópia do resultado sem o que o plano não libera (valores trocados por None)."""
    resultado = copy.deepcopy(dados)
    margem = "margem_bruta" in liberadas
    if not margem:
        for item in resultado["scanner"]["itens"]:
            for campo in CAMPOS_MARGEM:
                if campo in item:
                    item[campo] = None
        for fatia in resultado["fatias"]:
            for campo in ("custo_total", "margem_valor", "margem_percentual"):
                fatia[campo] = None
    auditoria = resultado["auditoria"]
    # Raio-X gravado antes do saneamento de 11/2026 (raio-x-2026-11.3) não tem os campos líquidos:
    # eles entram como nulos, a mesma forma do que o plano não libera, e a tela e a API não quebram.
    for campo in CAMPOS_MARGEM_LIQUIDA + CAMPOS_CMV_LIQUIDO:
        auditoria.setdefault(campo, None)
    if "auditoria_abaixo_custo" not in liberadas:
        auditoria["itens_abaixo_custo"] = None
    if not margem:
        auditoria["margem_por_item"] = None
        auditoria["margem_valor"] = auditoria["margem_percentual"] = None
        for campo in CAMPOS_MARGEM_LIQUIDA:
            auditoria[campo] = None
    if "diagnostico_cmv" not in liberadas:
        auditoria["custo_total"] = None
        for campo in CAMPOS_CMV_LIQUIDO:
            auditoria[campo] = None
    if "alerta_precificacao" not in liberadas:
        auditoria["precificacao"] = None
    # Custo a confirmar (unificação de 11/2026): os itens de custo implausível, fora da perda e do
    # recuperável. Raio-X gravado antes (até raio-x-2026-11.4) não tem o bloco: entra como nulo. A contagem e
    # a perda desconsiderada são totais como a perda abaixo do custo, que todo plano vê; a lista, com o
    # custo de cada item, é da auditoria de custos, como a das vendas abaixo do custo; e o custo e a venda
    # com custo do bloco (que com a perda dariam o custo) são do diagnóstico de CMV, como o custo total.
    a_confirmar = auditoria.get("custo_a_confirmar")
    if not isinstance(a_confirmar, dict):
        auditoria["custo_a_confirmar"] = None
    else:
        if "auditoria_abaixo_custo" not in liberadas:
            a_confirmar["lista"] = None
        if "diagnostico_cmv" not in liberadas:
            a_confirmar["custo_total"] = a_confirmar["faturamento_com_custo"] = None

    corte = _corte_historico(resultado["fatias"], limite_meses)
    visiveis, ocultas = [], []
    for fatia in resultado["fatias"]:
        if corte is not None and fatia.get("fim") and date.fromisoformat(fatia["fim"]) < corte:
            ocultas.append(fatia)
        else:
            visiveis.append(fatia)
    resultado["fatias"] = visiveis
    # A lista de relatórios também perde os meses ocultos (o arquivo fatiado traz um item por mês, com as linhas
    # de venda de cada um): fatias e arquivos usam o mesmo índice.
    indices_ocultos = {fatia["indice"] for fatia in ocultas}
    resultado["arquivos"] = [arquivo for arquivo in resultado.get("arquivos") or []
                             if arquivo.get("indice") not in indices_ocultos]
    # A faixa oculta pelas datas, em mês/ano: um relatório anual oculto não vira "A a B a C".
    de = min((date.fromisoformat(f["inicio"] or f["fim"]) for f in ocultas), default=None)
    ate = max((date.fromisoformat(f["fim"]) for f in ocultas), default=None)
    resultado["historico"] = {"limite_meses": limite_meses, "ocultas": len(ocultas),
                              "primeira_oculta": f"{de.month:02d}/{de.year}" if de else None,
                              "ultima_oculta": f"{ate.month:02d}/{ate.year}" if ate else None}
    resultado["indicadores_executivos"] = _indicadores_visiveis(resultado.get("indicadores_executivos"), liberadas,
                                                                corte)
    resultado["protegido"] = {
        "margem": not margem,
        "auditoria": "auditoria_abaixo_custo" not in liberadas,
        "cmv": "diagnostico_cmv" not in liberadas,
        "precificacao": "alerta_precificacao" not in liberadas,
    }
    return resultado
