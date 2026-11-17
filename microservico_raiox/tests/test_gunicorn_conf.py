"""O gunicorn.conf.py e o wsgi.py: log de acesso sem dado pessoal e conferência da subida só no worker.

O gunicorn não roda no Windows (fcntl), então o arquivo é lido como o gunicorn o lê, por execução, sem importá-lo.
"""

import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

PASTA = Path(__file__).resolve().parent.parent
FORMATO = '%(t)s "%(m)s %(U)s" %(s)s %(L)s'


class GunicornConfTests(unittest.TestCase):
    def setUp(self):
        self.conf = runpy.run_path(str(PASTA / "gunicorn.conf.py"))

    def test_log_de_acesso_sem_dado_pessoal(self):
        # O Render liga o log de acesso por padrão (GUNICORN_CMD_ARGS com --access-logfile -); o formato é daqui.
        formato = self.conf["access_log_format"]
        self.assertEqual(formato, FORMATO)
        # Só instante, método, caminho sem a query, status e duração: nem IP (h), nem query (q e a linha r), nem
        # Referer (f), nem User-Agent (a), nem usuário (u), nem cabeçalho, ambiente ou resposta ({...}i, e, o),
        # nem largura ou precisão que escondam um átomo proibido.
        atomos = [parte.split(")", 1)[0] for parte in formato.split("%(")[1:]]
        self.assertEqual(atomos, ["t", "m", "U", "s", "L"])
        self.assertNotIn("{", formato)

    def test_a_conferencia_da_subida_comeca_no_worker_sem_segurar_o_gancho(self):
        chamadas, solte = [], threading.Event()

        def conferencia_lenta(app):
            chamadas.append(app)
            solte.wait(10)

        worker = mock.Mock(wsgi=object())
        with mock.patch("app.conferir_dependencias", conferencia_lenta):
            inicio = time.monotonic()
            self.conf["post_worker_init"](worker)
            duracao = time.monotonic() - inicio
            # O gunicorn só aceita conexões depois do gancho: ele volta na hora, com a conferência ainda rodando.
            self.assertLess(duracao, 1)
            threads = [thread for thread in threading.enumerate() if thread.name == "conferencia-da-subida"]
            self.assertEqual(len(threads), 1)
            self.assertTrue(threads[0].is_alive())
            self.assertTrue(threads[0].daemon)
            solte.set()
            threads[0].join(5)
        self.assertEqual(chamadas, [worker.wsgi])

    def test_o_wsgi_nao_inicia_thread_na_importacao(self):
        # Com o --preload do Render, o wsgi.py é importado no processo mestre, antes do fork do worker: nenhuma
        # thread pode nascer nessa importação, nem uma que termine logo.
        codigo = ("import threading; iniciadas = []; original = threading.Thread.start; "
                  "threading.Thread.start = lambda thread: (iniciadas.append(thread.name), original(thread))[1]; "
                  "import wsgi; print(iniciadas)")
        ambiente = {chave: valor for chave, valor in os.environ.items()
                    if chave.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONUTF8")}
        resultado = subprocess.run([sys.executable, "-B", "-c", codigo], cwd=PASTA, env=ambiente,
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertEqual(resultado.stdout.strip().splitlines()[-1], "[]")


if __name__ == "__main__":
    unittest.main()
