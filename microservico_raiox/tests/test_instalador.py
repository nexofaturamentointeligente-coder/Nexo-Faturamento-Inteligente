"""O build verificado (instalar_dependencias.py): origem, hash e ambiente antes de qualquer instalação.

Download e processos simulados: nada é baixado nem instalado.
"""

import contextlib
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request

import instalar_dependencias as instalador


def _abridor(conteudo: bytes):
    abridor = mock.Mock()
    abridor.open.return_value = io.BytesIO(conteudo)
    return abridor


class InstaladorTests(unittest.TestCase):
    def test_wheel_so_com_o_hash_certo(self):
        conteudo = b"wheel-sintetica"
        with tempfile.TemporaryDirectory(prefix="nexo-raiox-instalador-teste-") as pasta, \
                mock.patch.object(instalador.urllib.request, "build_opener", return_value=_abridor(conteudo)), \
                mock.patch.object(instalador, "PIP_SHA256", hashlib.sha256(conteudo).hexdigest()):
            wheel = instalador.baixar_wheel_conferida(Path(pasta))
            self.assertEqual((wheel.name, wheel.read_bytes()), (instalador.PIP_WHEEL, conteudo))

    def test_hash_errado_tamanho_demais_ou_redirecionamento_param_antes_de_qualquer_processo(self):
        recusado = mock.Mock()
        recusado.open.side_effect = instalador.FalhaNaInstalacao("Redirecionamento fora da origem HTTPS oficial.")
        casos = {
            "hash errado": (_abridor(b"wheel-adulterada"), {}),
            "tamanho demais": (_abridor(b"x" * 9), {"MAX_WHEEL_BYTES": 8}),
            "redirecionamento": (recusado, {}),
        }
        for rotulo, (abridor, trocas) in casos.items():
            with self.subTest(caso=rotulo), contextlib.ExitStack() as pilha:
                pilha.enter_context(mock.patch.object(instalador.urllib.request, "build_opener", return_value=abridor))
                for nome, valor in trocas.items():
                    pilha.enter_context(mock.patch.object(instalador, nome, valor))
                executar = pilha.enter_context(mock.patch.object(instalador.subprocess, "run"))
                with self.assertRaises(instalador.FalhaNaInstalacao):
                    instalador.instalar()
                executar.assert_not_called()

    def test_redirecionamento_so_para_a_origem_oficial(self):
        tratador = instalador._SoOrigemOficial()
        pedido = urllib.request.Request(instalador.PIP_URL)
        for alvo in ("https://pacotes.invalid/pip.whl", "https://files.pythonhosted.org.pacotes.invalid/pip.whl",
                     "http://files.pythonhosted.org/pip.whl", "https://files.pythonhosted.org:8443/pip.whl",
                     "https://usuario@files.pythonhosted.org/pip.whl", "ftp://files.pythonhosted.org/pip.whl"):
            with self.subTest(alvo=alvo), self.assertRaises(instalador.FalhaNaInstalacao):
                tratador.redirect_request(pedido, None, 302, "Found", {}, alvo)

    def test_ambiente_dos_processos_sem_segredo_e_com_proxy_e_certificados(self):
        sintetico = {
            "PATH": "/sintetico/bin", "HTTPS_PROXY": "https://proxy.invalid:8443", "SSL_CERT_FILE": "/sintetico/ca.pem",
            "REQUESTS_CA_BUNDLE": "/sintetico/ca.pem", "CURL_CA_BUNDLE": "/sintetico/ca.pem",
            "PIP_INDEX_URL": "https://indice-nao-aprovado.invalid/simple", "PIP_CONFIG_FILE": "/sintetico/pip.conf",
            "PYTHONPATH": "/sintetico/imports", "NETRC": "/sintetico/credenciais",
            "DATABASE_URL": "postgresql://sintetico.invalid/nao-conectar", "SUPABASE_KEY": "chave-sintetica",
        }
        with mock.patch.dict(os.environ, sintetico, clear=True):
            ambiente = instalador.ambiente_do_instalador("/sintetico/netrc-vazio")
        for chave in ("PATH", "HTTPS_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            self.assertEqual(ambiente[chave], sintetico[chave])
        self.assertEqual((ambiente["PIP_CONFIG_FILE"], ambiente["NETRC"]), (os.devnull, "/sintetico/netrc-vazio"))
        self.assertEqual({chave for chave in ambiente if chave.startswith("PIP_")}, {"PIP_CONFIG_FILE"})
        for chave in ("PYTHONPATH", "DATABASE_URL", "SUPABASE_KEY"):
            self.assertNotIn(chave, ambiente)
        # O mesmo conjunto da SEC-006B da aplicação principal.
        principal = Path(__file__).resolve().parents[2] / "tools" / "install_dependencies.py"
        if principal.is_file():
            texto = principal.read_text(encoding="utf-8")
            for chave in instalador.AMBIENTE_SEGURO:
                self.assertIn(f'"{chave}"', texto)

    def test_ordem_wheel_conferencia_requisitos_e_check(self):
        chamadas = []

        def executar(comando, **opcoes):
            chamadas.append(comando)
            if "-c" in comando and "importlib.metadata" in comando[comando.index("-c") + 1]:
                return subprocess.CompletedProcess(comando, 0, stdout=f"pip={instalador.PIP_VERSION}\n")
            return subprocess.CompletedProcess(comando, 0)

        with tempfile.TemporaryDirectory(prefix="nexo-raiox-instalador-teste-") as pasta, \
                mock.patch.object(instalador, "baixar_wheel_conferida",
                                  side_effect=lambda _p: Path(pasta) / instalador.PIP_WHEEL), \
                mock.patch.object(instalador.subprocess, "run", side_effect=executar):
            instalador.instalar()
        self.assertEqual(len(chamadas), 4)
        self.assertIn("--no-index", chamadas[0])
        self.assertIn("pip.__version__", chamadas[1][chamadas[1].index("-c") + 1])
        self.assertEqual(chamadas[2][chamadas[2].index("--index-url") + 1], "https://pypi.org/simple")
        self.assertEqual(chamadas[3][-1], "check")

    def test_versao_errada_para_antes_dos_requisitos(self):
        chamadas = []

        def executar(comando, **opcoes):
            chamadas.append(comando)
            return subprocess.CompletedProcess(comando, 1 if len(chamadas) == 2 else 0, stdout="")

        with tempfile.TemporaryDirectory(prefix="nexo-raiox-instalador-teste-") as pasta, \
                mock.patch.object(instalador, "baixar_wheel_conferida",
                                  side_effect=lambda _p: Path(pasta) / instalador.PIP_WHEEL), \
                mock.patch.object(instalador.subprocess, "run", side_effect=executar):
            with self.assertRaises(instalador.FalhaNaInstalacao):
                instalador.instalar()
        self.assertEqual(len(chamadas), 2)


if __name__ == "__main__":
    unittest.main()
