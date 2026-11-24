"""SEC-005: configuração finita e limites reais do processo ETL isolado.

Todos os arquivos e subprocessos pertencem ao teste. Nenhuma requisição, banco
existente, credencial real ou carga volumosa é utilizada.
"""

from dataclasses import FrozenInstanceError, asdict, fields, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from flask import Flask

from etl_limits import ETLLimits, get_limits, limits_from_environment


class ETLLimitConfigurationTests(unittest.TestCase):
    def test_defaults_are_finite_and_preserve_current_upload_ceiling(self):
        limits = ETLLimits()
        self.assertEqual(limits.max_file_bytes, 20 * 1024 * 1024)
        self.assertEqual(limits.timeout_seconds, 60)
        self.assertEqual(limits.memory_mb, 384)
        self.assertTrue(all(type(value) is int and value > 0 for value in asdict(limits).values()))

    def test_zero_negative_boolean_float_and_text_cannot_disable_any_budget(self):
        for item in fields(ETLLimits):
            for invalid in (0, -1, True, 1.5, "10", None):
                with self.subTest(field=item.name, invalid_type=type(invalid).__name__):
                    with self.assertRaises(ValueError):
                        ETLLimits.from_mapping({item.name: invalid})

    def test_excessive_values_cannot_remove_resource_protections(self):
        for item in fields(ETLLimits):
            with self.subTest(field=item.name):
                with self.assertRaises(ValueError):
                    ETLLimits.from_mapping({item.name: 2 ** 63})
        with self.assertRaises(ValueError):
            ETLLimits(memory_mb=127)

    def test_unknown_fields_and_non_mapping_configuration_fail_predictably(self):
        for invalid in ({"unknown": 1}, [], "unlimited", 1):
            with self.subTest(kind=type(invalid).__name__):
                with self.assertRaises(ValueError):
                    ETLLimits.from_mapping(invalid)

    def test_partial_configuration_keeps_every_other_default(self):
        custom = ETLLimits.from_mapping({"max_rows": 12, "max_columns": 7})
        expected = replace(ETLLimits(), max_rows=12, max_columns=7)
        self.assertEqual(custom, expected)
        with self.assertRaises(FrozenInstanceError):
            custom.max_rows = 100

    def test_environment_configuration_accepts_only_finite_decimal_values(self):
        with patch.dict(os.environ, {"ETL_MAX_ROWS": "12", "ETL_TIMEOUT_SECONDS": "3"}, clear=True):
            self.assertEqual(limits_from_environment().max_rows, 12)
            self.assertEqual(get_limits().timeout_seconds, 3)
        for value in ("0", "-1", "1.5", " 2", "inf", "９", "2" * 1000):
            with self.subTest(kind=value[:8]):
                with patch.dict(os.environ, {"ETL_MAX_ROWS": value}, clear=True):
                    with self.assertRaises(ValueError):
                        limits_from_environment()

    def test_configuration_errors_never_echo_environment_values(self):
        sentinel = "SYNTHETIC_PRIVATE_CONFIGURATION_SENTINEL"
        with patch.dict(os.environ, {"ETL_MAX_ROWS": sentinel}, clear=True):
            with self.assertRaises(ValueError) as failure:
                limits_from_environment()
        self.assertNotIn(sentinel, str(failure.exception))

    def test_flask_configuration_is_validated_without_reading_application_secrets(self):
        app = Flask(__name__)
        app.config["ETL_LIMITS"] = {"max_rows": 9}
        with app.app_context():
            self.assertEqual(get_limits().max_rows, 9)
            app.config["ETL_LIMITS"] = ETLLimits(max_rows=11)
            self.assertEqual(get_limits().max_rows, 11)
            app.config["ETL_LIMITS"] = {"max_rows": 0}
            with self.assertRaises(ValueError):
                get_limits()


class ETLIsolatedRuntimeTests(unittest.TestCase):
    def setUp(self):
        import etl_runtime
        from etl_inputs import ETLInputError
        self.runtime, self.error = etl_runtime, ETLInputError
        # O _run_job real varreria as pastas órfãs da pasta temporária do sistema, fora da área do teste; a
        # varredura de verdade é coberta por ETLTemporariosOrfaosTests, numa pasta do teste.
        varredura = patch.object(etl_runtime, "varrer_temporarios_orfaos", return_value=0)
        varredura.start()
        self.addCleanup(varredura.stop)
        self.work = tempfile.TemporaryDirectory(prefix="nexo-sec005-runtime-test-")
        self.addCleanup(self.work.cleanup)
        self.directory = Path(self.work.name)
        self.input = self.directory / "synthetic.csv"
        self.input.write_text("produto;quantidade;faturamento\nSintético;1;10,00\n")
        self.started = []
        self.popen = subprocess.Popen

    def _script(self, body):
        path = self.directory / "owned_test_worker.py"
        path.write_text("import json, os, sys, time\nfrom pathlib import Path\n"
                        "request = json.loads(Path(sys.argv[1]).read_text())\n"
                        "output = Path(sys.argv[2])\n" + body + "\n")
        return path

    def _start(self, *args, **kwargs):
        process = self.popen(*args, **kwargs)
        self.started.append((process, args, kwargs))
        return process

    def _assert_reaped_and_temporary_resources_removed(self):
        self.assertTrue(self.started)
        for process, args, kwargs in self.started:
            self.assertIsNotNone(process.poll())
            self.assertIsNotNone(process.returncode)
            self.assertFalse(Path(args[0][-2]).parent.exists())
            self.assertFalse(Path(args[0][-1]).exists())

    def _run_owned(self, body, *, limits=None, calculation=False):
        with patch.object(self.runtime, "_WORKER_PATH", self._script(body)):
            with patch.object(self.runtime.subprocess, "Popen", side_effect=self._start):
                if calculation:
                    return self.runtime.run_calculation(self.input, self.input, limits)
                return self.runtime.run_validation(self.input, ".csv", limits)

    def test_timeout_terminates_and_reaps_the_owned_child_before_returning(self):
        before = time.monotonic()
        with self.assertRaises(self.error) as failure:
            self._run_owned("time.sleep(10)", limits=ETLLimits(timeout_seconds=1))
        self.assertEqual(failure.exception.code, "timeout")
        self.assertLess(time.monotonic() - before, 5)
        self._assert_reaped_and_temporary_resources_removed()

    def test_parent_cancellation_still_terminates_the_owned_child_and_cleans_resources(self):
        def start_with_cancelled_wait(*args, **kwargs):
            process = self._start(*args, **kwargs)
            actual_wait = process.wait
            first = True
            def cancelled_wait(*wait_args, **wait_kwargs):
                nonlocal first
                if first:
                    first = False
                    raise KeyboardInterrupt
                return actual_wait(*wait_args, **wait_kwargs)
            process.wait = cancelled_wait
            return process

        with patch.object(self.runtime, "_WORKER_PATH", self._script("time.sleep(10)")):
            with patch.object(self.runtime.subprocess, "Popen", side_effect=start_with_cancelled_wait):
                with self.assertRaises(KeyboardInterrupt):
                    self.runtime.run_validation(self.input, ".csv")
        self._assert_reaped_and_temporary_resources_removed()

    def test_abrupt_child_exit_is_reported_without_stderr_or_private_paths(self):
        sentinel = "SYNTHETIC_PRIVATE_WORKER_STDERR"
        with self.assertRaises(self.error) as failure:
            self._run_owned(f"sys.stderr.write({sentinel!r})\nsys.stderr.flush()\nos._exit(9)")
        self.assertNotIn(sentinel, str(failure.exception))
        self.assertNotIn(str(self.directory), str(failure.exception))
        self._assert_reaped_and_temporary_resources_removed()

    def test_invalid_json_is_not_accepted_or_reflected_to_the_user(self):
        sentinel = "SYNTHETIC_PRIVATE_INVALID_JSON"
        with self.assertRaises(self.error) as failure:
            self._run_owned(f"output.write_text({sentinel!r})")
        self.assertNotIn(sentinel, str(failure.exception))
        self._assert_reaped_and_temporary_resources_removed()

    def test_oversized_result_is_rejected_before_unbounded_materialization(self):
        with self.assertRaises(self.error):
            self._run_owned("output.write_bytes(b'x' * 4097)", limits=ETLLimits(max_output_bytes=4096))
        self._assert_reaped_and_temporary_resources_removed()

    def test_invalid_envelopes_fail_closed(self):
        for envelope in ([], {"ok": 1, "result": {}}, {"ok": True, "result": []},
                         {"ok": False, "code": 1, "message": "synthetic"}):
            with self.subTest(envelope_kind=type(envelope).__name__):
                with self.assertRaises(self.error):
                    self._run_owned(f"output.write_text(json.dumps({envelope!r}))")
        self._assert_reaped_and_temporary_resources_removed()

    def test_successful_envelope_from_a_failing_process_is_not_accepted(self):
        with self.assertRaises(self.error):
            self._run_owned("output.write_text(json.dumps({'ok': True, 'result': {}}))\nsys.exit(7)")
        self._assert_reaped_and_temporary_resources_removed()

    def test_worker_environment_filters_application_secrets_and_preserves_network_controls(self):
        poisoned = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8",
                    "DATABASE_URL": "synthetic-no-connect", "SECRET_KEY": "synthetic-secret",
                    "GH_TOKEN": "synthetic-token", "PIP_INDEX_URL": "synthetic-no-index",
                    "PYTHONPATH": "synthetic-no-pythonpath", "HTTPS_PROXY": "http://synthetic.invalid:8123",
                    "SSL_CERT_FILE": "/synthetic-ca-path"}
        script = """result = {
    'secret_present': any(name in os.environ for name in ('DATABASE_URL', 'SECRET_KEY', 'GH_TOKEN', 'PIP_INDEX_URL', 'PYTHONPATH')),
    'proxy_preserved': os.environ.get('HTTPS_PROXY') == 'http://synthetic.invalid:8123',
    'ca_preserved': os.environ.get('SSL_CERT_FILE') == '/synthetic-ca-path',
    'blas_single_thread': all(os.environ.get(name) == '1' for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS')),
}
output.write_text(json.dumps({'ok': True, 'result': result}))"""
        with patch.dict(os.environ, poisoned, clear=True):
            result = self._run_owned(script)
        self.assertFalse(result["secret_present"])
        self.assertTrue(result["proxy_preserved"])
        self.assertTrue(result["ca_preserved"])
        self.assertTrue(result["blas_single_thread"])
        command, kwargs = self.started[0][1][0], self.started[0][2]
        self.assertEqual(command[1:3], ["-I", "-B"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertNotIn("preexec_fn", kwargs)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["start_new_session"], os.name == "posix")
        self._assert_reaped_and_temporary_resources_removed()

    def test_no_windows_a_pasta_do_job_nao_e_o_diretorio_atual_do_filho(self):
        """No Windows o python.exe do venv é um lançador que abre o interpretador num neto: com a pasta do job
        como diretório atual, o neto, que termina depois do lançador no estouro do prazo, travava a remoção da
        pasta, e o "timeout" virava um erro genérico. No POSIX o filho continua dentro da pasta do job."""
        script = self._script("output.write_text(json.dumps({'ok': True, 'result': {'pronto': True}}))")
        with patch.object(self.runtime, "_WORKER_PATH", script):
            with patch.object(self.runtime.subprocess, "Popen", side_effect=self._start):
                self.assertEqual(self.runtime._run_job("validate", [self.input], extension=".csv"),
                                 {"pronto": True})
        command, kwargs = self.started[0][1][0], self.started[0][2]
        pasta_do_job, diretorio = Path(command[-2]).parent, Path(kwargs["cwd"])
        if os.name == "posix":
            self.assertEqual(diretorio, pasta_do_job)
        else:
            self.assertNotEqual(diretorio, pasta_do_job)
            self.assertNotIn(pasta_do_job, diretorio.parents)
        self._assert_reaped_and_temporary_resources_removed()

    def test_busy_admission_refuses_another_job_without_spawning_or_allocating_files(self):
        self.assertTrue(self.runtime._ADMISSION.acquire(blocking=False))
        try:
            with patch.object(self.runtime.subprocess, "Popen") as spawn:
                with self.assertRaises(self.error) as failure:
                    self.runtime.run_validation(self.input, ".csv")
                self.assertEqual(failure.exception.code, "busy")
                spawn.assert_not_called()
        finally:
            self.runtime._ADMISSION.release()

    def test_uma_linha_de_log_por_processamento_so_com_numeros_e_rotulos_fixos(self):
        """Operação, quantidade de arquivos, duração, pico de memória do processo isolado (o ru_maxrss que o worker
        põe no envelope), código de saída e desfecho, também nas recusas; nunca o nome do arquivo nem o caminho."""
        def envelope(**campos):
            return f"output.write_text(json.dumps({campos!r}))"
        casos = {
            "ok": (envelope(ok=True, result={}, runtime={"peak_rss_kib_linux": 12345}), None,
                   "pico_rss_kib=12345 codigo_saida=0 desfecho=ok"),
            "memória": (envelope(ok=False, code="memory", message="Sintética.", runtime={"peak_rss_kib_linux": 400000}),
                        None, "pico_rss_kib=400000 codigo_saida=0 desfecho=memoria"),
            "recusa": (envelope(ok=False, code="data", message="Sintética.", runtime={"peak_rss_kib_linux": True}),
                       None, "pico_rss_kib=None codigo_saida=0 desfecho=recusa"),
            "sem resultado": ("os._exit(9)", None, "pico_rss_kib=None codigo_saida=9 desfecho=sem_resultado"),
            "prazo": ("time.sleep(10)", ETLLimits(timeout_seconds=1), "pico_rss_kib=None codigo_saida="),
        }
        for caso, (corpo, limites, esperado) in casos.items():
            with self.subTest(caso=caso), patch.object(self.runtime.logger, "disabled", False), \
                    self.assertLogs("etl_runtime", "INFO") as registro:
                try:
                    self._run_owned(corpo, limits=limites)
                except self.error:
                    self.assertNotEqual(caso, "ok")
            linhas = [linha for linha in registro.output if "operacao=" in linha]
            self.assertEqual(len(linhas), 1)
            self.assertRegex(linhas[0], r"ETL: operacao=validate arquivos=1 duracao_s=[0-9]+\.[0-9]{2} ")
            self.assertIn(esperado, linhas[0])
            if caso == "prazo":
                self.assertTrue(linhas[0].endswith("desfecho=prazo"))
            for privado in (str(self.directory), self.directory.name, "synthetic.csv", "owned_test_worker"):
                self.assertNotIn(privado, linhas[0])
        self._assert_reaped_and_temporary_resources_removed()

    def test_a_linha_de_log_sai_como_falha_se_a_pasta_do_job_nao_sai_depois_do_ok(self):
        real = tempfile.TemporaryDirectory

        class PastaQueNaoSai:
            def __init__(self, *args, **kwargs):
                self.pasta = real(*args, **kwargs)

            def __enter__(self):
                return self.pasta.__enter__()

            def __exit__(self, *info):
                self.pasta.__exit__(*info)
                raise OSError("remoção sintética recusada")

        with patch.object(self.runtime.tempfile, "TemporaryDirectory", PastaQueNaoSai), \
                patch.object(self.runtime.logger, "disabled", False), \
                self.assertLogs("etl_runtime", "INFO") as registro, self.assertRaises(self.error) as falha:
            self._run_owned("output.write_text(json.dumps({'ok': True, 'result': {}}))")
        # Falha do servidor (a pasta do job não sai), não do arquivo: código de infraestrutura.
        self.assertEqual(falha.exception.code, "infra")
        linhas = [linha for linha in registro.output if "operacao=" in linha]
        self.assertEqual(len(linhas), 1)
        self.assertTrue(linhas[0].endswith("codigo_saida=0 desfecho=falha"))
        self._assert_reaped_and_temporary_resources_removed()

    def test_o_job_varre_as_pastas_orfas_depois_de_obter_a_vaga(self):
        with patch.object(self.runtime, "varrer_temporarios_orfaos", return_value=0) as varredura:
            self._run_owned("output.write_text(json.dumps({'ok': True, 'result': {}}))")
            varredura.assert_called_once_with()
            varredura.reset_mock()
            self.assertTrue(self.runtime._ADMISSION.acquire(blocking=False))
            try:
                with self.assertRaises(self.error):
                    self.runtime.run_validation(self.input, ".csv")
            finally:
                self.runtime._ADMISSION.release()
            varredura.assert_not_called()

    def test_real_worker_validates_a_small_csv_inside_default_resource_limits(self):
        with patch.object(self.runtime.subprocess, "Popen", side_effect=self._start):
            result = self.runtime.run_validation(self.input, ".csv")
        self.assertIsInstance(result, dict)
        self.assertTrue(result)
        self._assert_reaped_and_temporary_resources_removed()

    @unittest.skipUnless(os.name == "posix", "RLIMIT_AS é exigido pelo worker POSIX")
    def test_reduced_real_address_space_budget_fails_closed_without_large_fixture(self):
        # O import de pandas/NumPy/openpyxl já ultrapassa 128MiB de espaço
        # virtual neste runtime. O mesmo CSV é aceito no teste de 384MiB.
        # Não se aloca arquivo gigante nem se consome memória sem limite.
        with patch.object(self.runtime.subprocess, "Popen", side_effect=self._start):
            with self.assertRaises(self.error) as failure:
                self.runtime.run_validation(self.input, ".csv", ETLLimits(memory_mb=128))
        self.assertNotIn(str(self.directory), str(failure.exception))
        self._assert_reaped_and_temporary_resources_removed()

    def test_calculation_restores_dates_and_rejects_incomplete_payloads(self):
        result = {"sucesso": True, "mensagem": "Sintético", "kpis": {
            "periodo_base_inicio": "2026-01-01", "periodo_base_fim": "2026-01-31"},
            "telemetria": {}, "alertas": []}
        computed = self._run_owned(f"output.write_text(json.dumps({dict(ok=True, result=result)!r}))",
                                   calculation=True)
        self.assertEqual(computed.kpis["periodo_base_inicio"].isoformat(), "2026-01-01")
        for bad_result in ({"sucesso": True}, dict(result, kpis={"periodo_base_inicio": "bad-date"})):
            with self.subTest(fields=len(bad_result)):
                with self.assertRaises(self.error):
                    self._run_owned(f"output.write_text(json.dumps({dict(ok=True, result=bad_result)!r}))",
                                    calculation=True)
        self._assert_reaped_and_temporary_resources_removed()



    def test_real_worker_reports_applied_os_limits_and_no_database_modules(self):
        import json
        import sys
        purchases = self.directory / "synthetic_purchases.csv"
        purchases.write_text("produto;qtde_comprada;total_custo\nSintético;3;30,00\n")
        request, output = self.directory / "request.json", self.directory / "result.json"
        policy = ETLLimits()
        request.write_text(json.dumps({"operation": "calculate", "paths": [str(self.input), str(purchases)],
                                       "extension": None, "limits": asdict(policy)}))
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(self.runtime._WORKER_PATH), str(request), str(output)],
            env=self.runtime._worker_environment(), cwd=self.directory, timeout=15,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        envelope = json.loads(output.read_bytes())
        self.assertTrue(envelope["ok"])
        diagnostic = envelope["runtime"]
        self.assertFalse(diagnostic["database_modules_loaded"])
        if os.name == "posix":
            self.assertEqual(diagnostic["address_space_bytes"], policy.memory_mb * 1024 * 1024)
            self.assertEqual(diagnostic["cpu_seconds"], policy.timeout_seconds)
            self.assertGreater(diagnostic["peak_rss_kib_linux"], 0)
        else:
            # Windows (desenvolvimento): sem o módulo resource, o worker processa sem os limites do sistema
            # operacional e diz isso no diagnóstico (etl_worker._limitar_recursos).
            self.assertEqual((diagnostic["address_space_bytes"], diagnostic["cpu_seconds"],
                              diagnostic["peak_rss_kib_linux"]), (None, None, None))


class ETLWorkerPlatformTests(unittest.TestCase):
    """Os limites do sistema operacional no processo isolado (etl_worker._limitar_recursos): exigidos num
    sistema POSIX, que é o da produção; no Windows, que não tem o módulo resource, o processamento de
    desenvolvimento segue sem eles. Nada aqui chama o setrlimit de verdade: o módulo resource é falso."""

    DESENVOLVIMENTO = {"FLASK_ENV": "development", "FLASK_DEBUG": "1", "DEBUG": "True"}

    def setUp(self):
        import etl_worker
        self.worker = etl_worker
        self.limits = ETLLimits()

    @staticmethod
    def _resource_falso(falha=None):
        chamadas = []

        def setrlimit(recurso, valores):
            if falha is not None:
                raise falha
            chamadas.append((recurso, valores))
        return SimpleNamespace(RLIMIT_AS="AS", RLIMIT_CPU="CPU", RLIMIT_FSIZE="FSIZE", RLIMIT_CORE="CORE",
                               setrlimit=setrlimit), chamadas

    def test_so_a_plataforma_decide_e_nunca_o_ambiente(self):
        for ambiente in ({}, self.DESENVOLVIMENTO, {"FLASK_ENV": "production"}):
            with self.subTest(ambiente=sorted(ambiente)), patch.dict(os.environ, ambiente):
                self.assertEqual(self.worker._exige_limites(), os.name == "posix")

    def test_posix_sem_o_modulo_resource_recusa_mesmo_em_desenvolvimento(self):
        with patch.dict(sys.modules, {"resource": None}), patch.dict(os.environ, self.DESENVOLVIMENTO), \
                patch.object(self.worker, "_exige_limites", return_value=True):
            with self.assertRaises(OSError):
                self.worker._limitar_recursos(self.limits, 4096)

    def test_windows_sem_o_modulo_resource_segue_sem_os_limites_do_sistema(self):
        with patch.dict(sys.modules, {"resource": None}), \
                patch.object(self.worker, "_exige_limites", return_value=False):
            self.assertIsNone(self.worker._limitar_recursos(self.limits, 4096))

    def test_com_o_modulo_resource_os_quatro_limites_sao_sempre_aplicados(self):
        for exige in (True, False):
            with self.subTest(exige_limites=exige):
                modulo, chamadas = self._resource_falso()
                with patch.dict(sys.modules, {"resource": modulo}), \
                        patch.object(self.worker, "_exige_limites", return_value=exige):
                    self.assertIs(self.worker._limitar_recursos(self.limits, 4096), modulo)
                self.assertEqual(chamadas, [("AS", (384 * 1024 * 1024,) * 2), ("CPU", (60, 61)),
                                            ("FSIZE", (4096, 4096)), ("CORE", (0, 0))])

    def test_limite_que_o_sistema_recusa_nao_e_ignorado(self):
        for falha in (OSError("synthetic"), ValueError("synthetic")):
            with self.subTest(falha=type(falha).__name__):
                modulo, _chamadas = self._resource_falso(falha)
                with patch.dict(sys.modules, {"resource": modulo}):
                    with self.assertRaises(type(falha)):
                        self.worker._limitar_recursos(self.limits, 4096)

    def test_main_recusa_sem_ler_o_arquivo_quando_os_limites_faltam_num_sistema_posix(self):
        """O ramo de recusa do etl_worker.main, num processo à parte (o audit hook do worker nunca entra no
        processo dos testes): num sistema POSIX sem o módulo resource, ou com um limite que o sistema recusa,
        mesmo depois de outros aplicados, nada é lido e a resposta é "platform"."""
        import etl_runtime
        raiz = Path(self.worker.__file__).resolve().parent
        casos = {"sem o módulo resource": "None",
                 "primeiro limite recusado": "falso(1, OSError)",
                 "segundo limite recusado": "falso(2, ValueError)",
                 "último limite recusado": "falso(4, PermissionError)"}
        with tempfile.TemporaryDirectory(prefix="nexo-sec005-platform-") as diretorio:
            pasta = Path(diretorio)
            entrada = pasta / "vendas.csv"
            entrada.write_text("produto;quantidade;faturamento\nSintetico;1;10,00\n")
            request = pasta / "request.json"
            request.write_text(json.dumps({"operation": "validate", "paths": [str(entrada)], "extension": ".csv",
                                           "limits": asdict(ETLLimits())}))
            for rotulo, modulo in casos.items():
                with self.subTest(caso=rotulo):
                    output = pasta / "result.json"
                    output.unlink(missing_ok=True)
                    envoltorio = (
                        "import json, sys, types\n"
                        f"sys.path.insert(0, {str(raiz)!r})\n"
                        "def falso(chamada, erro):\n"
                        "    feitas = []\n"
                        "    def setrlimit(recurso, valores):\n"
                        "        feitas.append(recurso)\n"
                        "        if len(feitas) == chamada:\n"
                        "            raise erro('synthetic')\n"
                        "    return types.SimpleNamespace(RLIMIT_AS=1, RLIMIT_CPU=2, RLIMIT_FSIZE=3, RLIMIT_CORE=4,\n"
                        "                                 setrlimit=setrlimit)\n"
                        f"sys.modules['resource'] = {modulo}\n"
                        "import etl_worker\n"
                        "etl_worker._exige_limites = lambda: True\n"
                        f"sys.argv = ['etl_worker.py', {str(request)!r}, {str(output)!r}]\n"
                        "codigo = etl_worker.main()\n"
                        "lidos = [m for m in ('etl_inputs', 'pandas', 'security_gatekeeper') if m in sys.modules]\n"
                        "print(json.dumps({'codigo': codigo, 'lidos': lidos}))\n")
                    feito = subprocess.run([sys.executable, "-I", "-B", "-c", envoltorio], capture_output=True,
                                           text=True, timeout=60, cwd=pasta, env=etl_runtime._worker_environment(),
                                           stdin=subprocess.DEVNULL, check=False)
                    self.assertEqual(feito.returncode, 0)
                    self.assertEqual(json.loads(feito.stdout.strip().splitlines()[-1]), {"codigo": 2, "lidos": []})
                    envelope = json.loads(output.read_text())
                    self.assertEqual((envelope["ok"], envelope["code"]), (False, "platform"))

    def test_o_servidor_avisa_uma_vez_quando_roda_sem_os_limites_do_sistema(self):
        import etl_runtime
        # Na suíte inteira, o fileConfig do Alembic (nas migrações de outros testes) desliga os loggers já criados.
        ligado = patch.object(etl_runtime.logger, "disabled", False)
        with ligado, patch.object(etl_runtime, "_LIMITES_DO_SISTEMA", False), \
                patch.object(etl_runtime, "_SEM_LIMITES_AVISADO", False):
            with self.assertLogs("etl_runtime", "WARNING") as registro:
                etl_runtime._avisar_sem_limites()
                etl_runtime._avisar_sem_limites()
            self.assertEqual(len(registro.records), 1)
        with patch.object(etl_runtime.logger, "disabled", False), \
                patch.object(etl_runtime, "_LIMITES_DO_SISTEMA", True), \
                patch.object(etl_runtime, "_SEM_LIMITES_AVISADO", False), \
                self.assertNoLogs("etl_runtime", "WARNING"):
            etl_runtime._avisar_sem_limites()


class ETLTemporariosOrfaosTests(unittest.TestCase):
    """A varredura das pastas temporárias que uma morte dura do processo web deixa no disco
    (etl_runtime.varrer_temporarios_orfaos), sobre uma pasta do teste no lugar da pasta temporária do sistema."""

    PREFIXOS = ("nexo-etl-upload-", "nexo-etl-job-", "nexo-etl-materialized-", "nexo-raio-x-")

    def setUp(self):
        import etl_runtime
        self.runtime = etl_runtime
        pasta = tempfile.TemporaryDirectory(prefix="nexo-sec005-orfaos-test-")
        self.addCleanup(pasta.cleanup)
        self.pasta = Path(pasta.name)
        self.velho = time.time() - 3600
        ligado = patch.object(etl_runtime.logger, "disabled", False)  # o fileConfig do Alembic, na suíte inteira
        ligado.start()
        self.addCleanup(ligado.stop)

    def _pasta(self, nome, *, velha=True):
        caminho = self.pasta / nome
        caminho.mkdir()
        (caminho / "arquivo_01.csv").write_text("relatorio sintetico")
        if velha:
            os.utime(caminho, (self.velho, self.velho))
        return caminho

    def test_os_prefixos_sao_os_das_pastas_que_o_codigo_cria(self):
        raiz = Path(self.runtime.__file__).resolve().parent
        criadores = {"nexo-etl-upload-": "etl_uploads.py", "nexo-etl-job-": "etl_runtime.py",
                     "nexo-etl-materialized-": "storage.py", "nexo-raio-x-": "raio_x_servico.py"}
        for prefixo, arquivo in criadores.items():
            with self.subTest(prefixo=prefixo):
                self.assertIn(f'prefix="{prefixo}"', (raiz / arquivo).read_text(encoding="utf-8"))
                # O nome que o tempfile dá de verdade (o prefixo e os 8 caracteres sorteados).
                nome = Path(tempfile.mkdtemp(prefix=prefixo, dir=self.pasta)).name
                self.assertIsNotNone(self.runtime._PASTA_TEMPORARIA.fullmatch(nome), nome)
        self.assertIn('prefix="nexo-raio-x-"', (raiz / "blueprints" / "leads.py").read_text(encoding="utf-8"))

    def test_apaga_so_as_pastas_velhas_com_os_nomes_exatos(self):
        velhas = [self._pasta(prefixo + "ab12_x9z") for prefixo in self.PREFIXOS]
        recente = self._pasta("nexo-etl-job-recente1", velha=False)
        # Prefixos vizinhos, inclusive os das pastas dos testes, e nomes que o tempfile não daria.
        vizinhas = [self._pasta(nome) for nome in (
            "nexo-etl-upload-source-test-ab12x9z1", "nexo-upload-source-test-ab12x9z1", "nexo-raio-xy-ab12x9z1",
            "nexo-etl-job-ab12x9z", "nexo-etl-job-ab12x9z12", "nexo-etl-job-AB12X9Z1", "outro-nexo-etl-job-ab12x9z1",
            "nexo-sec005-runtime-test-ab12x9z1", "nexo-painel-ab12x9z1", "nexo_arquivos")]
        arquivo = self.pasta / "nexo-etl-job-arquivo1"
        arquivo.write_text("relatorio sintetico")
        os.utime(arquivo, (self.velho, self.velho))
        with self.assertLogs("etl_runtime", "INFO") as registro:
            self.assertEqual(self.runtime.varrer_temporarios_orfaos(pasta=self.pasta), 4)
        self.assertEqual([pasta.exists() for pasta in velhas], [False] * 4)
        self.assertTrue(recente.exists())
        self.assertEqual([pasta.name for pasta in vizinhas if not pasta.exists()], [])
        self.assertTrue(arquivo.is_file())
        self.assertEqual(registro.output, ["INFO:etl_runtime:ETL: 4 pasta(s) temporária(s) órfã(s) apagada(s)."])
        with self.assertNoLogs("etl_runtime", "INFO"):
            self.assertEqual(self.runtime.varrer_temporarios_orfaos(pasta=self.pasta), 0)

    def test_link_simbolico_e_juncao_nao_sao_seguidos(self):
        alvo = self._pasta("fora-da-varredura")
        links = []
        try:
            os.symlink(alvo, self.pasta / "nexo-etl-job-simbolic", target_is_directory=True)
            links.append(self.pasta / "nexo-etl-job-simbolic")
        except (OSError, NotImplementedError):
            pass  # Windows sem permissão de criar link simbólico
        if os.name == "nt":
            import _winapi
            try:
                _winapi.CreateJunction(str(alvo), str(self.pasta / "nexo-raio-x-juncao01"))
                links.append(self.pasta / "nexo-raio-x-juncao01")
            except OSError:
                pass
        if not links:
            self.skipTest("o sistema não deixa criar link simbólico nem junção aqui")
        # Idade negativa: toda entrada conta como velha, e só o tipo decide.
        self.assertEqual(self.runtime.varrer_temporarios_orfaos(-3600, pasta=self.pasta), 0)
        self.assertTrue(all(os.path.lexists(link) for link in links))
        self.assertTrue((alvo / "arquivo_01.csv").is_file())

    def test_no_posix_so_as_pastas_do_mesmo_usuario(self):
        velha = self._pasta("nexo-etl-job-outrouid")
        with patch.object(self.runtime.os, "getuid", return_value=velha.stat().st_uid + 1, create=True):
            self.assertEqual(self.runtime.varrer_temporarios_orfaos(pasta=self.pasta), 0)
        self.assertTrue(velha.exists())
        # O usuário de verdade (no Windows, sem os.getuid, a conferência não existe).
        self.assertEqual(self.runtime.varrer_temporarios_orfaos(pasta=self.pasta), 1)
        self.assertFalse(velha.exists())

    def test_falha_na_varredura_nunca_levanta(self):
        with self.assertLogs("etl_runtime", "WARNING") as registro:
            self.assertEqual(self.runtime.varrer_temporarios_orfaos(pasta=self.pasta / "inexistente"), 0)
        self.assertIn("(FileNotFoundError)", registro.output[0])
        self.assertNotIn(self.pasta.name, registro.output[0])
        velha = self._pasta("nexo-etl-job-presa001")
        with patch.object(self.runtime.shutil, "rmtree", side_effect=PermissionError("sintético")):
            self.assertEqual(self.runtime.varrer_temporarios_orfaos(pasta=self.pasta), 0)
        self.assertTrue(velha.exists())


@unittest.skipUnless(os.name == "posix", "Gunicorn exige POSIX")
class ETLGunicornThreadedRuntimeTests(unittest.TestCase):
    def test_real_gunicorn_one_worker_fifty_threads_executes_isolated_etl(self):
        """O thread WSGI real cria/aguarda o filho sem preexec_fn ou banco."""
        import json
        import signal
        import socket
        import sys
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="nexo-sec005-gunicorn-") as directory:
            work = Path(directory)
            sales, purchases = work / "sales.csv", work / "purchases.csv"
            sales.write_text("produto;quantidade;faturamento\nProduto sintético;2;20,00\n")
            purchases.write_text("produto;qtde_comprada;total_custo\nProduto sintético;3;30,00\n")
            module = work / "synthetic_etl_wsgi.py"
            module.write_text(f"""import os, sys, threading
sys.path.insert(0, {str(root)!r})
from flask import Flask, jsonify
from etl_runtime import run_calculation, run_validation
app = Flask(__name__)
@app.get('/ping')
def ping():
    return jsonify(status='ok')
@app.get('/etl')
def etl():
    value = run_calculation({str(sales)!r}, {str(purchases)!r})
    checked = run_validation({str(sales)!r}, '.csv')
    return jsonify(success=value.sucesso, sales=value.kpis['faturamento_total'],
                   purchases=value.kpis['total_comprado'], validated=isinstance(checked, dict),
                   thread=threading.current_thread().name, process=os.getpid())
""")
            environment = {key: value for key, value in os.environ.items()
                           if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TZ"}}
            environment.update(PYTHONDONTWRITEBYTECODE="1", OPENBLAS_NUM_THREADS="1",
                               OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1",
                               DATABASE_URL="synthetic-no-existing-database",
                               SECRET_KEY="synthetic-sec005-gunicorn-only")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(8)
                process = subprocess.Popen(
                    [sys.executable, "-B", "-m", "gunicorn", "--workers", "1", "--threads", "50",
                     "--timeout", "20", "--bind", f"fd://{listener.fileno()}",
                     "--access-logfile", os.devnull, "--error-logfile", str(work / "gunicorn.log"),
                     "synthetic_etl_wsgi:app"], cwd=work, env=environment,
                    pass_fds=(listener.fileno(),), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                )
                try:
                    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    ready = False
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail("Gunicorn sintético encerrou; logs privados omitidos.")
                        try:
                            with opener.open(base + "/ping", timeout=0.5) as response:
                                ready = response.status == 200
                            if ready:
                                break
                        except (OSError, urllib.error.URLError):
                            time.sleep(0.05)
                    self.assertTrue(ready, "Gunicorn não confirmou inicialização no prazo.")
                    with opener.open(base + "/etl", timeout=15) as response:
                        result = json.loads(response.read(65536))
                    self.assertTrue(result["success"])
                    self.assertTrue(result["validated"])
                    self.assertEqual(result["sales"], 20.0)
                    self.assertEqual(result["purchases"], 30.0)
                    self.assertNotEqual(result["thread"], "MainThread")
                    self.assertNotEqual(result["process"], process.pid)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
