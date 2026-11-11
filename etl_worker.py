"""Entrypoint privado SEC-005: stdin/rede/banco não fazem parte do protocolo."""

import json
import os
from pathlib import Path
import sys


def _exige_limites():
    """Os limites do sistema operacional (módulo resource) são exigidos num sistema POSIX, que é o da
    produção (Linux): lá o processamento nunca roda sem eles. A decisão é só da plataforma, e nenhuma
    variável de ambiente (FLASK_ENV, DEBUG) a muda: o processo isolado nem as recebe (etl_runtime)."""
    return os.name == "posix"


def _limitar_recursos(limits, escrita):
    """Aplica os limites do sistema operacional ao processo isolado, antes de importar pandas/openpyxl, e
    devolve o módulo resource. No Windows, que não tem o módulo, devolve None: o processamento de
    desenvolvimento segue sem os limites de memória, CPU e gravação do sistema operacional, com o resto do
    isolamento valendo (processo à parte, prazo do processo pai, ambiente filtrado, recusa de rede e de
    processos, limites do próprio ETL). Num sistema POSIX, sem o módulo ou sem os limites, levanta OSError
    ou ValueError, e o processamento é recusado."""
    try:
        import resource
    except ImportError:
        if _exige_limites():
            raise OSError("O módulo resource é exigido nesta plataforma.") from None
        return None
    resource.setrlimit(resource.RLIMIT_AS, (limits.memory_mb * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (limits.timeout_seconds, limits.timeout_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (escrita,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return resource


def main():
    request_path, output_path = map(Path, sys.argv[1:3])
    if request_path.stat().st_size > 16_384:
        return 2
    request = json.loads(request_path.read_bytes())
    # -I não inclui a raiz do projeto; somente esta origem fixa é adicionada.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from etl_limits import ETLLimits, using_limits
    limits = ETLLimits.from_mapping(request["limits"])
    # A triagem do upload (operação validate) pode gravar a cópia neutralizada do arquivo. Ela
    # só é liberada até o tamanho aceito na entrada (a releitura e ler_liberado conferem), então
    # esse é o teto de gravação; as demais operações só gravam o resultado.
    escrita = limits.max_output_bytes
    if request.get("operation") == "validate":
        escrita = max(limits.max_output_bytes, limits.max_file_bytes)
    # Limites são aplicados antes de importar pandas/openpyxl.
    try:
        resource = _limitar_recursos(limits, escrita)
    except (OSError, ValueError):
        output_path.write_text(json.dumps({"ok": False, "code": "platform", "message":
            "A plataforma não oferece o isolamento de recursos exigido para processar arquivos."}))
        return 2

    # Rede e qualquer primitiva de execução/forja de processo (não só Popen):
    # a leitura da planilha nunca precisa delas. Os eventos de ctypes ficam de
    # fora porque o próprio import do numpy/pandas dispara ctypes.dlopen.
    _EVENTOS_RECUSADOS = frozenset({
        "socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg",
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
        "os.fork", "os.forkpty", "os.startfile",
    })

    def refuse_external(event, args):
        if event in _EVENTOS_RECUSADOS:
            raise PermissionError("Operação externa não permitida no ETL isolado.")
    sys.addaudithook(refuse_external)
    envelope = None
    try:
        from etl_inputs import validate_input
        with using_limits(limits):
            if request["operation"] == "validate" and len(request["paths"]) == 1:
                # Baia de Segurança: validação, neutralização, estrutura mínima e, se preciso,
                # a cópia só com valores ao lado do arquivo (security_gatekeeper.triar).
                from security_gatekeeper import triar
                result = triar(request["paths"][0], request["extension"], limits)
            elif request["operation"] == "calculate" and len(request["paths"]) == 2:
                from etl_processor import _calcular_kpis_local
                calculated = _calcular_kpis_local(*request["paths"])
                result = calculated._asdict()
            elif request["operation"] == "raio_x" and request["paths"]:
                # Raio-X de entrada: cada relatório passa pela mesma validação do upload antes
                # de ser lido; a extensão vem do nome que o processo pai deu ao arquivo.
                from raio_x import MAX_ARQUIVOS, diagnosticar
                if len(request["paths"]) > MAX_ARQUIVOS:
                    raise ValueError("Operação inválida.")
                for path in request["paths"]:
                    validate_input(path, Path(path).suffix.lower(), limits)
                result = diagnosticar(request["paths"])
            elif request["operation"] == "avaliacao_cenario" and request["paths"]:
                # Avaliação de Cenário (lead): a mesma validação e a mesma leitura do Raio-X, mais a
                # matriz produto x ano (avaliacao_motor). Quem grava é o processo pai.
                from avaliacao_motor import avaliar_cenario
                from raio_x import MAX_ARQUIVOS
                if len(request["paths"]) > MAX_ARQUIVOS:
                    raise ValueError("Operação inválida.")
                for path in request["paths"]:
                    validate_input(path, Path(path).suffix.lower(), limits)
                result = avaliar_cenario(request["paths"])
            else:
                raise ValueError("Operação inválida.")
        envelope = {"ok": True, "result": result}
    except MemoryError:
        envelope = {"ok": False, "code": "memory", "message":
                    "Arquivo excede o orçamento de memória. Reduza ou divida o relatório."}
    except Exception as error:
        code = getattr(error, "code", "worker")
        message = getattr(error, "safe_message", "Não foi possível interpretar o arquivo com segurança.")
        if code == "data":
            message = "Não foi possível identificar os dados essenciais. Revise as colunas de vendas e compras."
        envelope = {"ok": False, "code": code, "message": message}
    # Sem os limites do sistema operacional (Windows, desenvolvimento), os três primeiros vêm vazios.
    envelope["runtime"] = {
        "address_space_bytes": None if resource is None else resource.getrlimit(resource.RLIMIT_AS)[0],
        "cpu_seconds": None if resource is None else resource.getrlimit(resource.RLIMIT_CPU)[0],
        "peak_rss_kib_linux": None if resource is None else resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "database_modules_loaded": "models" in sys.modules or "config" in sys.modules,
    }
    from datetime import date, datetime
    def encode(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        raise TypeError("Tipo de resultado inválido.")
    try:
        payload = json.dumps(envelope, ensure_ascii=True, allow_nan=False, default=encode).encode()
        if len(payload) > limits.max_output_bytes:
            payload = b'{"ok":false,"code":"output","message":"Resultado excede o orcamento permitido."}'
        output_path.write_bytes(payload)
    except (OSError, ValueError, TypeError, MemoryError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
