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
CAMPOS_MARGEM = ("custo_total", "custo_medio", "margem_valor", "margem_percentual")
CAMPOS_MARGEM_LIQUIDA = ("margem_liquida_valor", "margem_liquida_percentual")
CAMPOS_CMV_LIQUIDO = ("cmv_liquido", "custo_bonificacoes", "custo_devolucoes")
CAMPOS_FATIA = ("faturamento", "itens", "faturamento_sem_custo", "pct_faturamento_sem_custo", "custo_total",
                "margem_valor", "margem_percentual", "perda_abaixo_custo", "vazamento_preco", "recuperavel",
                "devolucoes")


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
        raise ETLInputError("raio_x_vazio", "Selecione ao menos um relatório de vendas (CSV ou XLSX).")
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
                if extensao not in ("csv", "xlsx"):
                    raise ETLInputError("unsupported_format",
                                        f"Arquivo {numero}: formato não suportado. Envie CSV ou XLSX.")
                caminho = Path(diretorio) / f"arquivo_{numero:02d}.{extensao}"
                digest = _gravar_limitado(arquivo, caminho, limits, numero)
                # Baia de Segurança: assinatura e extensão antes do processo isolado. Os arquivos
                # do Raio-X não são gravados, então não há cópia neutralizada a liberar; a
                # leitura do motor já neutraliza as células (etl_inputs.read_raw).
                try:
                    security_gatekeeper.inspecionar_arquivo(caminho, extensao, rotulo=f"Arquivo {numero}")
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
    linha.arquivos = len(resultado["arquivos"])
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
    if not isinstance(dados, dict) or not raio_x.CHAVES_RESULTADO <= set(dados):
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

    corte = _corte_historico(resultado["fatias"], limite_meses)
    visiveis, ocultas = [], []
    for fatia in resultado["fatias"]:
        if corte is not None and fatia.get("fim") and date.fromisoformat(fatia["fim"]) < corte:
            ocultas.append(fatia)
        else:
            visiveis.append(fatia)
    resultado["fatias"] = visiveis
    # A faixa oculta pelas datas, em mês/ano: um relatório anual oculto não vira "A a B a C".
    de = min((date.fromisoformat(f["inicio"] or f["fim"]) for f in ocultas), default=None)
    ate = max((date.fromisoformat(f["fim"]) for f in ocultas), default=None)
    resultado["historico"] = {"limite_meses": limite_meses, "ocultas": len(ocultas),
                              "primeira_oculta": f"{de.month:02d}/{de.year}" if de else None,
                              "ultima_oculta": f"{ate.month:02d}/{ate.year}" if ate else None}
    resultado["protegido"] = {
        "margem": not margem,
        "auditoria": "auditoria_abaixo_custo" not in liberadas,
        "cmv": "diagnostico_cmv" not in liberadas,
        "precificacao": "alerta_precificacao" not in liberadas,
    }
    return resultado
