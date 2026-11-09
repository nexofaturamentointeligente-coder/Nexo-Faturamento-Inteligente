"""SEC-005: um filho por processo servidor, sem banco, com prazo e AS limitados."""

from dataclasses import asdict
from datetime import date
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading

from etl_limits import ETLLimits, get_limits

_WORKER_PATH = Path(__file__).resolve().with_name("etl_worker.py")
_ADMISSION = threading.BoundedSemaphore(1)


def _worker_environment():
    allowed = {"PATH", "HOME", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP",
               "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    result = {key: value for key, value in os.environ.items() if key in allowed}
    result.update({key: "1" for key in (
        "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")})
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


def _stop_owned_process(process):
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _run_job(operation, paths, *, extension=None, limits=None):
    from etl_inputs import ETLInputError
    policy = get_limits() if limits is None else limits
    if not isinstance(policy, ETLLimits):
        raise ValueError("Política ETL inválida.")
    if not _ADMISSION.acquire(blocking=False):
        raise ETLInputError("busy", "Há outro arquivo em validação ou processamento. Tente novamente em instantes.")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="nexo-etl-job-") as work:
            root = Path(work)
            request = root / "request.json"
            output = root / "result.json"
            data = json.dumps({"operation": operation, "paths": [os.fspath(path) for path in paths],
                               "extension": extension, "limits": asdict(policy)}, ensure_ascii=True)
            if len(data.encode()) > 16_384:
                raise ETLInputError("request", "Referência de arquivo inválida para processamento.")
            request.write_text(data)
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(_WORKER_PATH), str(request), str(output)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=root, env=_worker_environment(), start_new_session=(os.name == "posix"),
            )
            try:
                process.wait(timeout=policy.timeout_seconds)
            except subprocess.TimeoutExpired:
                _stop_owned_process(process)
                raise ETLInputError("timeout", "Tempo de processamento excedido. Reduza ou divida o relatório.") from None
            if not output.is_file() or output.stat().st_size > policy.max_output_bytes:
                raise ETLInputError("worker", "Processamento interrompido pelo limite de recursos ou por arquivo inválido.")
            with output.open("rb") as stream:
                payload = stream.read(policy.max_output_bytes + 1)
            if len(payload) > policy.max_output_bytes:
                raise ETLInputError("output", "Resultado excede o orçamento permitido.")
            try:
                envelope = json.loads(payload)
            except (ValueError, UnicodeError, RecursionError):
                raise ETLInputError("worker", "Não foi possível concluir o processamento com segurança.") from None
            if not isinstance(envelope, dict) or type(envelope.get("ok")) is not bool:
                raise ETLInputError("worker", "Resposta inválida do processamento isolado.")
            if not envelope["ok"]:
                code = envelope.get("code", "worker")
                message = envelope.get("message")
                if (not isinstance(code, str) or len(code) > 64 or not isinstance(message, str)
                        or not 1 <= len(message) <= 1024):
                    raise ETLInputError("worker", "Não foi possível processar o arquivo com segurança.")
                raise ETLInputError(code, message)
            if process.returncode != 0 or not isinstance(envelope.get("result"), dict):
                raise ETLInputError("worker", "Não foi possível concluir o processamento com segurança.")
            return envelope["result"]
    except ETLInputError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ETLInputError("worker", "Processamento temporariamente indisponível. Tente novamente.") from None
    finally:
        try:
            if process is not None and process.poll() is None:
                _stop_owned_process(process)
        finally:
            _ADMISSION.release()


def run_validation(path, extension, limits=None):
    """Triagem da Baia de Segurança no processo isolado (security_gatekeeper.triar). Quando há
    algo a neutralizar, o arquivo é lido duas vezes (o original e a cópia conferida): o prazo é
    o dobro do configurado, dentro do teto do ETLLimits (120 s), como no Raio-X."""
    from dataclasses import replace
    policy = get_limits() if limits is None else limits
    if isinstance(policy, ETLLimits):
        policy = replace(policy, timeout_seconds=min(120, policy.timeout_seconds * 2))
    return _run_job("validate", [path], extension=extension, limits=policy)


def run_raio_x(paths, limits=None):
    """Raio-X de entrada no processo isolado. Lê vários meses de uma vez: o prazo é o dobro
    do configurado, dentro do teto do ETLLimits (120 s)."""
    from dataclasses import replace
    from etl_inputs import ETLInputError
    from raio_x import CHAVES_RESULTADO
    policy = get_limits() if limits is None else limits
    policy = replace(policy, timeout_seconds=min(120, policy.timeout_seconds * 2))
    result = _run_job("raio_x", paths, limits=policy)
    if set(result) != CHAVES_RESULTADO:
        raise ETLInputError("worker", "Resultado inválido do processamento isolado.")
    return result


# Memória (RLIMIT_AS) da Avaliação de Cenário: a da instância do Render, mesmo que ETL_MEMORY_MB
# suba a das outras operações (o teto do ETLLimits é 512 MiB).
MEMORIA_AVALIACAO_MB = 384


def run_avaliacao_cenario(paths, limits=None):
    """Avaliação de Cenário (lead) no processo isolado: o Raio-X dos relatórios e a matriz
    produto x ano. Mesmo prazo do Raio-X (o dobro do configurado, até 120 s) e no máximo
    384 MiB de memória. O retorno é conferido inteiro antes de chegar ao banco
    (validar_avaliacao)."""
    from dataclasses import replace
    policy = get_limits() if limits is None else limits
    policy = replace(policy, timeout_seconds=min(120, policy.timeout_seconds * 2),
                     memory_mb=min(MEMORIA_AVALIACAO_MB, policy.memory_mb))
    return validar_avaliacao(_run_job("avaliacao_cenario", paths, limits=policy))


_CHAVE_PRODUTO = re.compile(r"[0-9a-f]{16}")
_ANO = re.compile(r"[0-9]{4}")
_DATA_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


# Maior contagem que as colunas INTEGER do banco guardam.
_INTEIRO_DO_BANCO = 2 ** 31 - 1
# Inteiro que o float representa sem perda: acima disso (até 4.300 dígitos o json.loads aceita),
# math.isfinite levantaria OverflowError e derrubaria a conferência com erro 500.
_INTEIRO_EXATO = 2 ** 53


def _inteiro(valor, minimo=0, maximo=None) -> bool:
    maximo = _INTEIRO_DO_BANCO if maximo is None else maximo
    return type(valor) is int and minimo <= valor <= maximo


def _numero(valor, minimo=None, maximo=None) -> bool:
    if type(valor) is int:
        if abs(valor) > _INTEIRO_EXATO:
            return False
    elif type(valor) is not float or not math.isfinite(valor):
        return False
    return (minimo is None or valor >= minimo) and (maximo is None or valor <= maximo)


def _texto_gravavel(valor, limite: int) -> bool:
    """Texto de 1 a `limite` caracteres, com algo além de espaços, só com caracteres imprimíveis (sem
    NUL, controle ou surrogate solto, que o PostgreSQL recusa com erro fora do tratamento)."""
    return (isinstance(valor, str) and 0 < len(valor) <= limite and bool(valor.strip())
            and all(caractere.isprintable() for caractere in valor))


def _data_iso(valor) -> bool:
    if valor is None:
        return True
    if not isinstance(valor, str) or not _DATA_ISO.fullmatch(valor):
        return False
    try:
        date.fromisoformat(valor)
    except ValueError:
        return False
    return True


def _meta_valida(meta, versao) -> bool:
    """A meta do envio (avaliacao_motor._meta): chaves fixas, só contagens, nenhum texto livre. Ela
    fica no lead depois do expurgo das linhas, então nada fora disto pode chegar ao banco."""
    from avaliacao_motor import (ANO_MAXIMO_DO_BANCO, ANO_MINIMO, CHAVES_AJUSTES, CHAVES_META, CHAVES_QUALIDADE,
                                 CHAVES_RELATORIOS_POR_ANO, CHAVES_SITUACAO_CONTAGENS, CHAVES_SITUACAO_VALORES,
                                 TETO_DINHEIRO_DA_META)
    from raio_x import MAX_ARQUIVOS, VERSAO_REGRAS
    if not isinstance(meta, dict) or set(meta) != CHAVES_META:
        return False
    if meta["versao_regras"] != versao or meta["versao_raio_x"] != VERSAO_REGRAS:
        return False
    arquivos = meta["arquivos"]
    if not _inteiro(arquivos, 1, MAX_ARQUIVOS):
        return False
    por_ano, dias, mensais = meta["arquivos_por_ano"], meta["dias_por_ano"], meta["relatorios_mensais_por_ano"]
    if not all(isinstance(contagem, dict) for contagem in (por_ano, dias, mensais)):
        return False
    anos = set(por_ano)
    if (not anos or set(dias) != anos or set(mensais) != anos
            or not all(isinstance(ano, str) and _ANO.fullmatch(ano)
                       and ANO_MINIMO <= int(ano) <= ANO_MAXIMO_DO_BANCO for ano in anos)):
        return False
    if (not all(_inteiro(quantos, 1, MAX_ARQUIVOS) for quantos in por_ano.values())
            or sum(por_ano.values()) != arquivos
            or not all(_inteiro(quantos, 1, 366) for quantos in dias.values())
            or not all(type(mensal) is bool for mensal in mensais.values())):
        return False
    if not _inteiro(meta["arquivos_com_custo"], 0, arquivos) or not _inteiro(meta["linhas_lidas"]):
        return False
    if not _inteiro(meta["celulas_recodificadas"]):
        return False
    qualidade, situacao = meta["qualidade"], meta["situacao"]
    if (not isinstance(qualidade, dict) or not set(qualidade) <= CHAVES_QUALIDADE
            or not all(_inteiro(valor) for valor in qualidade.values())):
        return False
    if not isinstance(situacao, dict) or not set(situacao) <= CHAVES_SITUACAO_CONTAGENS | CHAVES_SITUACAO_VALORES:
        return False
    if not all(_inteiro(valor) if chave in CHAVES_SITUACAO_CONTAGENS
               else _numero(valor, 0) and valor < TETO_DINHEIRO_DA_META for chave, valor in situacao.items()):
        return False
    # Etapa 2: a cobertura de cada verificação por ano (de 0 ao número de relatórios do ano), os dias da
    # janela dos mesmos meses e os ajustes de produto sem venda no ano (só valores, nunca texto).
    for chave in CHAVES_RELATORIOS_POR_ANO:
        contagem = meta[chave]
        if (not isinstance(contagem, dict) or set(contagem) != anos
                or not all(_inteiro(contagem[ano], 0, por_ano[ano]) for ano in anos)):
            return False
    janela = meta["dias_mesmos_meses_por_ano"]
    if (not isinstance(janela, dict) or set(janela) != anos
            or not all(_inteiro(janela[ano], 0, dias[ano]) for ano in anos)):
        return False
    if not _inteiro(meta["relatorios_cabecalho_ambiguo"], 0, arquivos):
        return False
    ajustes = meta["ajustes_sem_venda_por_ano"]
    return (isinstance(ajustes, dict) and set(ajustes) == anos
            and all(isinstance(ajustes[ano], dict) and set(ajustes[ano]) == set(CHAVES_AJUSTES)
                    and all(_numero(valor, 0) and valor < TETO_DINHEIRO_DA_META for valor in ajustes[ano].values())
                    for ano in anos))


def _raio_x_valido(raio) -> bool:
    """O que o processo pai lê do Raio-X do lead: período, índice de saúde e o que
    raio_x.recomendar usa para indicar o plano."""
    periodo, saude, recuperavel = raio.get("periodo"), raio.get("saude"), raio.get("recuperavel")
    if not all(isinstance(parte, dict) for parte in (periodo, saude, recuperavel)):
        return False
    if not (_data_iso(periodo.get("inicio")) and _data_iso(periodo.get("fim"))):
        return False
    if periodo.get("inicio") is not None and periodo.get("fim") is not None and periodo["inicio"] > periodo["fim"]:
        return False  # ck_lead_avaliacao_periodo recusaria no banco, com erro de gravação
    if periodo.get("meses") is not None and not _inteiro(periodo.get("meses")):
        return False
    inconsistencias = saude.get("inconsistencias")
    return (_numero(saude.get("indice"), 0, 100) and _inteiro(saude.get("arquivos_com_custo"))
            and (saude.get("pct_faturamento_sem_custo") is None or _numero(saude.get("pct_faturamento_sem_custo"), 0))
            and _numero(saude.get("faturamento_total")) and isinstance(inconsistencias, dict)
            and _inteiro(inconsistencias.get("itens_margem_negativa")) and _numero(recuperavel.get("total"), 0))


def validar_avaliacao(result):
    """O resultado da avaliação no formato exato que o processo pai grava; senão, recusa. Confere
    a estrutura inteira: um processo isolado com defeito (ou comprometido) não grava texto livre
    nem derruba a gravação com erro 500 (uma exceção na própria conferência também vira recusa)."""
    from etl_inputs import ETLInputError
    try:
        return _validar_avaliacao(result)
    except ETLInputError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, ArithmeticError, RecursionError):
        raise ETLInputError("worker", "Resultado inválido do processamento isolado.") from None


def _validar_avaliacao(result):
    from etl_inputs import ETLInputError
    from avaliacao_motor import (ANO_MAXIMO_DO_BANCO, ANO_MINIMO, CHAVES_RESULTADO as CHAVES_AVALIACAO, COLUNAS,
                                 COLUNAS_CONTAGEM, COLUNAS_DINHEIRO, COLUNAS_MARCA, COLUNAS_QUANTIDADE,
                                 LIMITE_DESCRICAO, TETOS, VERSAO_REGRAS_LEAD)
    from etl_processor import LIMITE_CATEGORIA
    from raio_x import CHAVES_RESULTADO

    def invalido():
        return ETLInputError("worker", "Resultado inválido do processamento isolado.")

    if not isinstance(result, dict) or set(result) != CHAVES_AVALIACAO:
        raise invalido()
    if result["versao"] != VERSAO_REGRAS_LEAD:
        raise invalido()
    if not isinstance(result["raio_x"], dict) or set(result["raio_x"]) != CHAVES_RESULTADO:
        raise invalido()
    if not _raio_x_valido(result["raio_x"]) or not _meta_valida(result["meta"], result["versao"]):
        raise invalido()
    linhas = result["linhas"]
    if (not isinstance(linhas, dict) or set(linhas) != {"colunas", "valores", "categorias"}
            or linhas["colunas"] != list(COLUNAS) or not isinstance(linhas["valores"], list)):
        raise invalido()
    # Categorias: texto do relatório (vai para a staging, que expira; nunca para a meta), cada uma uma vez.
    categorias = linhas["categorias"]
    if (not isinstance(categorias, list) or len(set(map(str, categorias))) != len(categorias)
            or not all(_texto_de_cadastro(nome, LIMITE_CATEGORIA) for nome in categorias)):
        raise invalido()
    meta = result["meta"]
    anos_do_envio = {int(ano) for ano in meta["arquivos_por_ano"]}
    posicao = {nome: i for i, nome in enumerate(COLUNAS)}
    vistos = set()
    for linha in linhas["valores"]:
        if not isinstance(linha, list) or len(linha) != len(COLUNAS):
            raise invalido()
        for nome, valor in zip(COLUNAS, linha):
            if nome == "ano_exercicio":
                valido = _inteiro(valor, ANO_MINIMO, ANO_MAXIMO_DO_BANCO) and valor in anos_do_envio
            elif nome in COLUNAS_CONTAGEM:
                valido = _inteiro(valor)
            elif nome == "chave_produto":
                valido = isinstance(valor, str) and _CHAVE_PRODUTO.fullmatch(valor) is not None
            elif nome == "descricao_produto":
                valido = _texto_gravavel(valor, LIMITE_DESCRICAO)
            elif nome in COLUNAS_MARCA:
                valido = type(valor) is bool
            elif nome == "codigo_produto":
                valido = valor is None or _texto_gravavel(valor, 60)
            elif nome == "categoria":
                valido = valor is None or _inteiro(valor, 0, len(categorias) - 1)
            elif nome == "estoque_final":
                # O estoque pode ser negativo; o teto vale para o módulo (NUMERIC(14,3)).
                valido = valor is None or (_numero(valor) and abs(valor) < TETOS[nome])
            elif nome in COLUNAS_DINHEIRO or nome in COLUNAS_QUANTIDADE:
                # O teto da coluna do banco (o motor já recusa acima dele com mensagem): um processo
                # isolado com defeito não derruba a gravação com estouro de NUMERIC.
                valido = _numero(valor, 0) and valor < TETOS.get(nome, 0)
            else:
                valido = False  # coluna sem regra de conferência: nunca passa calada
            if not valido:
                raise invalido()
        if not _linha_coerente(linha, posicao, meta):
            raise invalido()
        # Uma linha por produto e ano: a unicidade da staging (lead, ano, produto).
        par = (linha[posicao["ano_exercicio"]], linha[posicao["chave_produto"]])
        if par in vistos:
            raise invalido()
        vistos.add(par)
    return result


def _texto_de_cadastro(valor, limite: int) -> bool:
    """Texto de 1 a `limite` caracteres, com algo além de espaços e sem caractere de controle."""
    return (isinstance(valor, str) and 0 < len(valor) <= limite and bool(valor.strip())
            and all(caractere.isprintable() for caractere in valor))


# Folga de arredondamento entre somas de dinheiro com 2 casas (centavos de floats somados).
_FOLGA_CENTAVOS = 0.011


def _linha_coerente(linha: list, posicao: dict, meta: dict) -> bool:
    """As colunas da Etapa 2 batem entre si e com a cobertura do ano na meta."""
    def valor(nome):
        return linha[posicao[nome]]
    ano = str(valor("ano_exercicio"))
    relatorios = meta["arquivos_por_ano"][ano]
    if not (0 <= valor("meses_margem_negativa") <= valor("meses_com_custo") <= valor("meses") <= relatorios):
        return False
    # Toda linha tem venda em ao menos um relatório, com ao menos uma linha por relatório; o giro de um
    # lançamento conta os relatórios desde a primeira venda, que incluem os relatórios com venda.
    if not (1 <= valor("meses") <= valor("linhas")):
        return False
    if not (valor("meses") <= valor("relatorios_desde_primeira_venda") <= relatorios):
        return False
    if valor("meses_completos") > valor("meses") or (valor("ultimo_mes_negativo") and not valor("meses_margem_negativa")):
        return False
    # Venda com custo <= venda nos relatórios com a coluna de custo <= venda bruta.
    if not (valor("faturamento_com_custo") <= valor("faturamento_em_relatorio_com_custo") + _FOLGA_CENTAVOS
            and valor("faturamento_em_relatorio_com_custo") <= valor("valor_venda_bruto") + _FOLGA_CENTAVOS):
        return False
    if valor("quantidade_mesmos_meses") > valor("quantidade") + 0.0011:
        return False
    if valor("meses_com_custo") and not meta["relatorios_com_custo_por_ano"][ano]:
        return False
    # NCM ou categoria só faltam num ano em que algum relatório trouxe a coluna; o estoque, idem.
    if valor("ncm_ausente") and not meta["relatorios_com_ncm_por_ano"][ano]:
        return False
    if (valor("categoria_ausente") or valor("categoria") is not None) and not meta["relatorios_com_categoria_por_ano"][ano]:
        return False
    if valor("categoria_ausente") and valor("categoria") is not None:
        return False
    if valor("estoque_final") is not None and not meta["relatorios_com_estoque_por_ano"][ano]:
        return False
    return True


def run_calculation(path_vendas, path_compras, limits=None):
    from etl_processor import ResultadoETL
    from etl_inputs import ETLInputError
    result = _run_job("calculate", [path_vendas, path_compras], limits=limits)
    if (set(result) != set(ResultadoETL._fields) or result.get("sucesso") is not True
            or not isinstance(result.get("kpis"), dict) or not isinstance(result.get("telemetria"), dict)
            or not isinstance(result.get("alertas"), list)):
        raise ETLInputError("worker", "Resultado inválido do processamento isolado.")
    for name in ("periodo_base_inicio", "periodo_base_fim"):
        value = result["kpis"].get(name)
        if value is not None:
            try:
                result["kpis"][name] = date.fromisoformat(value)
            except (ValueError, TypeError):
                raise ETLInputError("worker", "Período inválido no resultado do processamento.") from None
    return ResultadoETL(**result)
