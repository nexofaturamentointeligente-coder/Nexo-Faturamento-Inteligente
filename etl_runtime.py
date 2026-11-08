"""SEC-005: um filho por processo servidor, sem banco, com prazo e AS limitados."""

from dataclasses import asdict
from datetime import date
import json
import os
from pathlib import Path
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
