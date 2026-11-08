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
            except (ValueError, UnicodeError):
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


def _inteiro(valor, minimo=0, maximo=None) -> bool:
    return type(valor) is int and valor >= minimo and (maximo is None or valor <= maximo)


def _numero(valor, minimo=None, maximo=None) -> bool:
    return (type(valor) in (int, float) and math.isfinite(valor)
            and (minimo is None or valor >= minimo) and (maximo is None or valor <= maximo))


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
    from avaliacao_motor import (ANO_MAXIMO_DO_BANCO, ANO_MINIMO, CHAVES_META, CHAVES_QUALIDADE,
                                 CHAVES_SITUACAO_CONTAGENS, CHAVES_SITUACAO_VALORES)
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
    return all(_inteiro(valor) if chave in CHAVES_SITUACAO_CONTAGENS else _numero(valor, 0)
               for chave, valor in situacao.items())


def _raio_x_valido(raio) -> bool:
    """O que o processo pai lê do Raio-X do lead: período, índice de saúde e o que
    raio_x.recomendar usa para indicar o plano."""
    periodo, saude, recuperavel = raio.get("periodo"), raio.get("saude"), raio.get("recuperavel")
    if not all(isinstance(parte, dict) for parte in (periodo, saude, recuperavel)):
        return False
    if not (_data_iso(periodo.get("inicio")) and _data_iso(periodo.get("fim"))):
        return False
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
    nem derruba a gravação com erro 500."""
    from etl_inputs import ETLInputError
    from avaliacao_motor import (ANO_MAXIMO_DO_BANCO, ANO_MINIMO, CHAVES_RESULTADO as CHAVES_AVALIACAO, COLUNAS,
                                 LIMITE_DESCRICAO, VERSAO_REGRAS_LEAD)
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
    if (not isinstance(linhas, dict) or set(linhas) != {"colunas", "valores"}
            or linhas["colunas"] != list(COLUNAS) or not isinstance(linhas["valores"], list)):
        raise invalido()
    anos_do_envio = {int(ano) for ano in result["meta"]["arquivos_por_ano"]}
    marcas = {"variacao", "conflito_cadastral", "estoque_negativo"}
    vistos = set()
    for linha in linhas["valores"]:
        if not isinstance(linha, list) or len(linha) != len(COLUNAS):
            raise invalido()
        for nome, valor in zip(COLUNAS, linha):
            if nome == "ano_exercicio":
                valido = _inteiro(valor, ANO_MINIMO, ANO_MAXIMO_DO_BANCO) and valor in anos_do_envio
            elif nome in ("linhas", "meses"):
                valido = _inteiro(valor)
            elif nome == "chave_produto":
                valido = isinstance(valor, str) and _CHAVE_PRODUTO.fullmatch(valor) is not None
            elif nome == "descricao_produto":
                valido = isinstance(valor, str) and 0 < len(valor) <= LIMITE_DESCRICAO and bool(valor.strip())
            elif nome in marcas:
                valido = type(valor) is bool
            elif nome == "codigo_produto":
                valido = valor is None or (isinstance(valor, str) and 0 < len(valor) <= 60 and bool(valor.strip()))
            else:
                valido = _numero(valor, 0)
            if not valido:
                raise invalido()
        # Uma linha por produto e ano: a unicidade da staging (lead, ano, produto).
        par = (linha[COLUNAS.index("ano_exercicio")], linha[COLUNAS.index("chave_produto")])
        if par in vistos:
            raise invalido()
        vistos.add(par)
    return result


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
