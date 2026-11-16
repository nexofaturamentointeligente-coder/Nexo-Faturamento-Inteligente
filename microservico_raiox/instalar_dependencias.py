"""Instalação verificada das dependências do microsserviço (a mesma proteção da SEC-006B da aplicação principal,
tools/install_dependencies.py: mesmo pip fixado, mesmo SHA-256, mesmas variáveis de ambiente preservadas).

Só biblioteca padrão; não lê ambiente da aplicação. O pip da imagem não processa os requisitos: baixa-se o pip
fixado abaixo, de files.pythonhosted.org, por HTTPS, conferido pelo SHA-256; ele se instala a partir da própria
wheel, a versão é confirmada pelos metadados e pelo módulo importado, e só então ele instala o requirements.txt
desta pasta, sem cache, sem configuração herdada e sem netrc.

Uso (build no Render, com o diretório raiz microservico_raiox): python -I instalar_dependencias.py
"""

import hashlib
import http.client
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

PASTA = Path(__file__).resolve().parent
PIP_VERSION = "26.2.2"
PIP_WHEEL = "pip-26.2.2-py3-none-any.whl"
PIP_URL = ("https://files.pythonhosted.org/packages/3f/06/"
           "3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/" + PIP_WHEEL)
PIP_SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
PYPI_INDEX = "https://pypi.org/simple"
MAX_WHEEL_BYTES = 16 * 1024 * 1024
# As mesmas da SEC-006B: o sistema, o proxy e os certificados da rede do build; nada da aplicação nem do pip.
AMBIENTE_SEGURO = {
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG",
    "LC_ALL", "LC_CTYPE", "VIRTUAL_ENV", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
    "https_proxy", "all_proxy", "no_proxy",
}
COMUNS = ["--isolated", "--disable-pip-version-check", "--no-cache-dir", "--no-input"]


class FalhaNaInstalacao(RuntimeError):
    pass


class _SoOrigemOficial(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        alvo = urllib.parse.urlsplit(newurl)
        if (alvo.scheme != "https" or alvo.hostname != "files.pythonhosted.org" or alvo.port not in {None, 443}
                or alvo.username or alvo.password):
            raise FalhaNaInstalacao("Redirecionamento fora da origem HTTPS oficial recusado.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def baixar_wheel_conferida(pasta: Path) -> Path:
    destino = Path(pasta) / PIP_WHEEL
    soma, tamanho = hashlib.sha256(), 0
    abridor = urllib.request.build_opener(_SoOrigemOficial())
    with abridor.open(PIP_URL, timeout=30) as resposta, destino.open("wb") as saida:  # nosec B310 - URL https fixa
        while bloco := resposta.read(64 * 1024):
            tamanho += len(bloco)
            if tamanho > MAX_WHEEL_BYTES:
                raise FalhaNaInstalacao("A wheel do pip passa do tamanho esperado.")
            soma.update(bloco)
            saida.write(bloco)
    if soma.hexdigest() != PIP_SHA256:
        raise FalhaNaInstalacao("O SHA-256 do pip não confere; instalação interrompida.")
    return destino


def ambiente_do_instalador(netrc) -> dict:
    """Só as variáveis do sistema, do proxy e dos certificados; o pip sem arquivo de configuração nem netrc."""
    ambiente = {chave: valor for chave, valor in os.environ.items() if chave in AMBIENTE_SEGURO}
    ambiente.update(PIP_CONFIG_FILE=os.devnull, NETRC=str(netrc), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return ambiente


def conferir_pip(ambiente: dict) -> None:
    """A versão do pip que vai instalar os requisitos, pelos metadados e pelo módulo importado."""
    sonda = ("import importlib.metadata, pip; atual = importlib.metadata.version('pip'); "
             f"assert atual == {PIP_VERSION!r}; assert pip.__version__ == {PIP_VERSION!r}; print('pip=' + atual)")
    conferencia = subprocess.run([sys.executable, "-I", "-B", "-c", sonda], cwd=PASTA, env=ambiente,
                                 capture_output=True, text=True, check=False)
    if conferencia.returncode or conferencia.stdout.strip() != "pip=" + PIP_VERSION:
        raise FalhaNaInstalacao("Versão do pip não confirmada; requisitos não instalados.")


def instalar() -> None:
    with tempfile.TemporaryDirectory(prefix="nexo-raiox-instalador-") as temporaria:
        wheel = baixar_wheel_conferida(Path(temporaria))
        netrc = Path(temporaria) / "netrc-vazio"
        netrc.touch(mode=0o600)
        ambiente = ambiente_do_instalador(netrc)
        lancador = ("import runpy,sys; wheel=sys.argv.pop(1); "
                    "sys.path.insert(0,wheel); runpy.run_module('pip',run_name='__main__')")
        subprocess.run([sys.executable, "-I", "-B", "-c", lancador, str(wheel), *COMUNS, "install", "--no-index",
                        "--no-deps", "--force-reinstall", str(wheel)], cwd=PASTA, env=ambiente, check=True)
        conferir_pip(ambiente)
        subprocess.run([sys.executable, "-I", "-B", "-m", "pip", *COMUNS, "install", "--index-url", PYPI_INDEX,
                        "--keyring-provider", "disabled", "-r", str(PASTA / "requirements.txt")],
                       cwd=PASTA, env=ambiente, check=True)
        subprocess.run([sys.executable, "-I", "-B", "-m", "pip", *COMUNS, "check"], cwd=PASTA, env=ambiente,
                       check=True)


if __name__ == "__main__":
    try:
        instalar()
    except FalhaNaInstalacao as erro:
        sys.exit(str(erro))
    except (OSError, ValueError, http.client.HTTPException, subprocess.CalledProcessError):
        # Sem a exceção de rede, de protocolo ou de ambiente, que pode trazer a URL de um proxy.
        sys.exit("A instalação verificada falhou; nenhuma etapa posterior foi executada.")
