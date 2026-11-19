"""SEC-005: medição reproduzível com relatórios exclusivamente sintéticos.

Executar em ambiente de teste. Cada leitura/cálculo recebe um processo novo;
RSS inclui Python e bibliotecas, não é uma medida exclusiva de DataFrames.
Nenhum banco, aplicação Flask, .env ou serviço externo é inicializado.

Também mede o Raio-X e a Avaliação de Cenário pelo próprio etl_worker (a
validação de cada relatório e a leitura do motor, com os limites e o audit hook
do processo isolado), sobre relatórios mensais sintéticos ou os indicados em
--report. No Linux a memória vem do ru_maxrss e do /proc/self/status; no
Windows, que não tem o módulo resource, do psapi (Working Set e memória privada).
"""

import argparse
import csv
from datetime import date, datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

try:
    import resource
except ImportError:  # Windows: a memória vem do psapi (_memoria_windows).
    resource = None


ROOT = Path(__file__).resolve().parents[1]
WORKER_OPERATIONS = ("raio_x", "avaliacao_cenario")


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def _memoria_windows():
    """Working Set e memória privada (PagefileUsage) deste processo, atuais e de pico, em KiB, pelo psapi."""
    import ctypes
    from ctypes import wintypes

    class Contadores(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    contadores = Contadores(cb=ctypes.sizeof(Contadores))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Contadores), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(contadores), contadores.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {campo: getattr(contadores, campo) // 1024
            for campo in ("PeakWorkingSetSize", "WorkingSetSize", "PeakPagefileUsage", "PagefileUsage")}


def _memoria():
    status = Path("/proc/self/status").read_text().splitlines() if sys.platform == "linux" else []
    return {
        "max_rss_kib": None if resource is None else resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "child_max_rss_kib": None if resource is None else resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        "memory_proc_status_kib": {line.split(":", 1)[0]: int(line.split()[1]) for line in status
                                   if line.startswith(("VmSize:", "VmPeak:", "VmRSS:", "VmHWM:"))},
        "memory_windows_kib": _memoria_windows() if os.name == "nt" else None,
    }


def _pelo_worker(operation, paths):
    """O Raio-X ou a Avaliação de Cenário pelo etl_worker.main, neste processo novo (a memória medida é a dele),
    com os limites de etl_runtime.run_raio_x / run_avaliacao_cenario. No POSIX o worker aplica os limites do
    sistema operacional a este processo, como no processo isolado."""
    from dataclasses import asdict, replace
    from etl_limits import ETLLimits
    import etl_runtime
    import etl_worker
    policy = ETLLimits()
    policy = replace(policy, timeout_seconds=min(120, policy.timeout_seconds * 2))
    if operation == "avaliacao_cenario":
        policy = replace(policy, memory_mb=min(etl_runtime.MEMORIA_AVALIACAO_MB, policy.memory_mb))
    with tempfile.TemporaryDirectory(prefix="nexo-sec005-measure-worker-") as directory:
        request, output = Path(directory) / "request.json", Path(directory) / "result.json"
        request.write_text(json.dumps({"operation": operation, "paths": [str(Path(path).resolve()) for path in paths],
                                       "extension": None, "limits": asdict(policy)}))
        sys.argv = [str(ROOT / "etl_worker.py"), str(request), str(output)]
        exit_code = etl_worker.main()
        envelope = json.loads(output.read_bytes())
        return {"worker_exit": exit_code, "ok": envelope["ok"], "code": envelope.get("code"),
                "runtime": envelope.get("runtime"), "result_bytes": output.stat().st_size, "files": len(paths)}


def _probe(options):
    sys.path.insert(0, str(ROOT))
    if options.operation in WORKER_OPERATIONS:
        before = time.perf_counter()
        result = _pelo_worker(options.operation, options.paths)
        print(json.dumps({"operation": options.operation, "source": "candidate", "import_seconds": None,
                          "processing_seconds": time.perf_counter() - before, **_memoria(), "result": result},
                         ensure_ascii=False, allow_nan=False))
        return
    before_import = time.perf_counter()
    if options.source:
        spec = importlib.util.spec_from_file_location("sec005_original_measurement", options.source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        import etl_processor as module
    import_seconds = time.perf_counter() - before_import
    before = time.perf_counter()
    if options.operation == "read":
        frames = [module._ler_bruto(options.sales), module._ler_bruto(options.purchases)]
        result = {"frames": [{"rows": len(frame), "columns": len(frame.columns),
                              "cells": int(frame.size),
                              "filled": int(frame.notna().to_numpy().sum())}
                             for frame in frames]}
    else:
        calculate = (getattr(module, "_calcular_kpis_local", module.calcular_kpis_em_memoria)
                     if options.operation == "calculate" else module.calcular_kpis_em_memoria)
        result = _plain(calculate(options.sales, options.purchases)._asdict())
    processing_seconds = time.perf_counter() - before
    print(json.dumps({"operation": options.operation, "source": "baseline" if options.source else "candidate",
                      "import_seconds": import_seconds, "processing_seconds": processing_seconds,
                      **_memoria(), "result": result}, ensure_ascii=False, allow_nan=False))


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        csv.writer(stream, delimiter=";", lineterminator="\n").writerows(rows)


def _fixtures(folder, csv_rows, xlsx_rows):
    from openpyxl import Workbook

    sales_header = ["nome_produto", "quantidade", "faturamento", "preco_venda", "estoque", "categoria"]
    purchase_header = ["nome_produto", "qtde_comprada", "total_custo", "fornecedor", "data_nf", "codigo"]

    def sales(count):
        yield sales_header
        for index in range(count):
            yield [f"Produto sintético {index % 200:03d}", str(index % 7 + 1),
                   f"{(index % 43 + 1) * 3},50", "9999,00", "15", "Sintética"]

    def purchases(count):
        yield purchase_header
        for index in range(count):
            yield [f"Produto sintético {index % 200:03d}", str(index % 11 + 1),
                   f"{(index % 31 + 1) * 5},25", "Fornecedor sintético", "10/01/2026", str(index)]

    pairs = []
    sv, pc = folder / "medium_sales.csv", folder / "medium_purchases.csv"
    _write_csv(sv, sales(csv_rows))
    _write_csv(pc, purchases(csv_rows))
    pairs.append(("csv_medium", sv, pc, 1))
    for kind, rows in (("sales", sales), ("purchases", purchases)):
        book = Workbook(write_only=True)
        for sheet_index in range(4):
            sheet = book.create_sheet(f"Aba sintética {sheet_index + 1}")
            # A última aba tem um registro adicional, permitindo conferir a
            # seleção sem manter quatro DataFrames na implementação candidata.
            for row in rows(xlsx_rows + (sheet_index == 3)):
                sheet.append(row)
        book.save(folder / f"medium_{kind}.xlsx")
        book.close()
    pairs.append(("xlsx_four_sheets", folder / "medium_sales.xlsx", folder / "medium_purchases.xlsx", 4))
    return pairs


def _relatorios_mensais(folder, rows):
    """Quatro relatórios de vendas no leiaute do Raio-X (o período no cabeçalho): novembro e dezembro de 2024 e de
    2025, os mesmos meses nos dois anos, como a Avaliação de Cenário compara."""
    def reais(centavos):
        return f"{centavos // 100},{centavos % 100:02d}"

    paths = []
    for year, month, last_day in ((2024, 11, 30), (2024, 12, 31), (2025, 11, 30), (2025, 12, 31)):
        lines = ["Relatorio de vendas por produto", f"Periodo: 01/{month:02d}/{year} a {last_day}/{month:02d}/{year}",
                 "Codigo;Produto;Qtde;Preco Unitario;Valor Total;Total Custo;Estoque"]
        for index in range(rows):
            quantity, price = index % 9 + 1, (index % 47 + 3) * 100 + 50
            lines.append(f"{index + 1};Produto sintetico {index:05d};{quantity};{reais(price)};"
                         f"{reais(quantity * price)};{reais(quantity * price * 7 // 10)};{index % 30}")
        path = folder / f"vendas_{year}_{month:02d}.csv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def _metadata(path, sheets):
    result = {"bytes": path.stat().st_size, "sheets": sheets, "uncompressed_bytes": None}
    if path.suffix == ".xlsx":
        with zipfile.ZipFile(path) as archive:
            result["uncompressed_bytes"] = sum(member.file_size for member in archive.infolist())
    return result


def _run_probe(arguments, directory, environment):
    # -X utf8: o -I ignora o PYTHONUTF8, e o Windows escreveria o JSON na página de código local.
    command = [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), "--probe", *arguments]
    completed = subprocess.run(command, env=environment, capture_output=True, encoding="utf-8",
                               check=True, timeout=300, cwd=directory)
    return json.loads(completed.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--csv-rows", type=int, default=10000)
    parser.add_argument("--xlsx-rows", type=int, default=3000)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--monthly-rows", type=int, default=2000,
                        help="produtos por relatório mensal sintético do Raio-X e da Avaliação de Cenário")
    parser.add_argument("--report", type=Path, action="append", default=[],
                        help="relatório de vendas para o Raio-X e a Avaliação de Cenário (repetível), no lugar dos "
                             "mensais sintéticos")
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", help=argparse.SUPPRESS)
    parser.add_argument("--sales", help=argparse.SUPPRESS)
    parser.add_argument("--purchases", help=argparse.SUPPRESS)
    parser.add_argument("--paths", nargs="+", help=argparse.SUPPRESS)
    parser.add_argument("--operation", choices=("read", "calculate", "public_calculate", *WORKER_OPERATIONS),
                        help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.probe:
        _probe(options)
        return
    if not options.baseline or not options.output:
        parser.error("--baseline e --output são obrigatórios")
    if (not 1 <= options.csv_rows <= 20000 or not 1 <= options.xlsx_rows <= 5000
            or not 1 <= options.monthly_rows <= 20000):
        parser.error("medição aceita somente amostras pequenas e controladas")
    if any(not path.is_file() for path in options.report):
        parser.error("--report precisa apontar para arquivos existentes")
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TZ", "TEMP", "TMP", "TMPDIR"}}
    environment.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
                       NUMEXPR_NUM_THREADS="1")
    report = {"method": "processo novo por leitura/cálculo e por operação do worker; RSS inclui imports; "
                        "Linux: ru_maxrss e /proc/self/status em KiB; Windows: psapi em KiB",
              "synthetic_only": not options.report, "csv_data_rows": options.csv_rows,
              "xlsx_data_rows_per_sheet": options.xlsx_rows, "cases": []}
    with tempfile.TemporaryDirectory(prefix="nexo-sec005-measure-") as directory:
        for name, sales, purchases, sheets in _fixtures(Path(directory), options.csv_rows, options.xlsx_rows):
            case = {"name": name, "sales": _metadata(sales, sheets),
                    "purchases": _metadata(purchases, sheets), "runs": []}
            # Caminho absoluto: a sonda roda com a pasta temporária como diretório atual.
            sources = [("baseline", str(options.baseline.resolve()))]
            if not options.baseline_only:
                sources.append(("candidate", None))
            for label, source in sources:
                operations = ["read", "calculate"]
                if label == "candidate":
                    operations.append("public_calculate")
                for operation in operations:
                    arguments = ["--operation", operation, "--sales", str(sales), "--purchases", str(purchases)]
                    if source:
                        arguments += ["--source", source]
                    case["runs"].append(_run_probe(arguments, directory, environment))
            if not options.baseline_only:
                original = next(run["result"] for run in case["runs"]
                                if run["source"] == "baseline" and run["operation"] == "calculate")
                candidate = next(run["result"] for run in case["runs"]
                                 if run["source"] == "candidate" and run["operation"] == "calculate")
                case["financial_exact_equal"] = original == candidate
                assert original == candidate, "A medição encontrou divergência no resultado sintético."
            report["cases"].append(case)
        if not options.baseline_only:
            # O Raio-X e a Avaliação de Cenário só existem na versão candidata (o motor atual).
            # Caminho absoluto, como no --baseline: a sonda roda com a pasta temporária como diretório atual.
            reports = [path.resolve() for path in options.report] or _relatorios_mensais(Path(directory),
                                                                                        options.monthly_rows)
            case = {"name": "raio_x_e_avaliacao_cenario",
                    "monthly_rows": None if options.report else options.monthly_rows,
                    "reports": [_metadata(Path(path), None) for path in reports], "runs": []}
            for operation in WORKER_OPERATIONS:
                run = _run_probe(["--operation", operation, "--paths", *map(str, reports)], directory, environment)
                # Uma recusa do worker não é medição do processamento.
                assert run["result"]["ok"], f"{operation}: o worker recusou ({run['result']['code']})"
                case["runs"].append(run)
            report["cases"].append(case)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                              encoding="utf-8")
    print(json.dumps({"output": str(options.output), "cases": len(report["cases"]),
                      "baseline_only": options.baseline_only}))


if __name__ == "__main__":
    main()
