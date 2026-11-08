"""SEC-005: leitura limitada e preflight, sem calcular indicadores no upload."""

from contextlib import contextmanager
import hashlib
from functools import wraps
import threading

from flask import flash, redirect, request, url_for
from pathlib import Path
import tempfile

from etl_inputs import ETLInputError
from etl_limits import get_limits
from etl_runtime import run_validation
import security_gatekeeper


_CHUNK_SIZE = 64 * 1024
_UPLOAD_ADMISSION = threading.BoundedSemaphore(1)


def _limitar_entrada(funcao, destino_busy=None):
    """Uma entrada ETL/upload por processo, incluindo bytes e espera por DB."""
    @wraps(funcao)
    def protegido(*args, **kwargs):
        if request.method != "POST":
            return funcao(*args, **kwargs)
        if not _UPLOAD_ADMISSION.acquire(blocking=False):
            flash("Há outro upload ou processamento em andamento. Tente novamente em instantes.", "warning")
            target = destino_busy or request.endpoint
            values = {} if destino_busy else (request.view_args or {})
            return redirect(url_for(target, **values))
        try:
            return funcao(*args, **kwargs)
        finally:
            _UPLOAD_ADMISSION.release()
    return protegido


def limitar_upload(funcao):
    return _limitar_entrada(funcao)


@contextmanager
def vaga_de_entrada():
    """A mesma vaga única de upload/processamento, para a rota que monta a própria resposta
    de "ocupado" (JSON na API do Raio-X). Rende True quando a vaga foi obtida."""
    obtida = _UPLOAD_ADMISSION.acquire(blocking=False)
    try:
        yield obtida
    finally:
        if obtida:
            _UPLOAD_ADMISSION.release()


def limitar_processamento(funcao):
    # O endpoint de processamento só aceita POST; não redirecionar para GET 405.
    return _limitar_entrada(funcao, "admin.dashboard")


def ler_upload_validado(arquivo, extensao: str, limits=None) -> tuple[bytes, str]:
    """Lê no máximo limite+1 e passa pela Baia de Segurança antes de persistir.

    O Content-Length do cliente não é usado como autoridade. A Baia confere a assinatura
    aqui e faz o resto em processo isolado (validação, neutralização de fórmula, estrutura
    mínima e inventário de conteúdo ativo). Devolve os bytes liberados e o SHA-256 deles: o
    próprio arquivo, ou a cópia só com valores quando havia algo a neutralizar. Apenas o
    diretório temporário criado aqui é removido; a validação não executa o ETL comercial.
    """
    try:
        limits = get_limits() if limits is None else limits
    except ValueError:
        raise ETLInputError("invalid_limits", "Configuração de processamento inválida.") from None
    extensao = extensao.lower().lstrip(".")
    if extensao not in {"csv", "xlsx"}:
        raise ETLInputError("unsupported_format", "Formato não suportado. Envie CSV ou XLSX.")
    teto = security_gatekeeper.teto_bytes(limits)
    digest = hashlib.sha256()
    content = bytearray()
    try:
        with tempfile.TemporaryDirectory(prefix="nexo-etl-upload-") as directory:
            path = Path(directory) / ("arquivo." + extensao)
            with path.open("wb") as destination:
                path.chmod(0o600)
                while True:
                    chunk = arquivo.read(min(_CHUNK_SIZE, teto + 1 - len(content)))
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ETLInputError("upload_io", "Não foi possível ler o arquivo enviado.")
                    if len(content) + len(chunk) > teto:
                        raise ETLInputError("file_size", "Arquivo acima do tamanho permitido para processamento.")
                    content.extend(chunk)
                    digest.update(chunk)
                    destination.write(chunk)
            if not content:
                raise ETLInputError("empty_file", "Arquivo vazio — nada foi salvo.")
            try:
                security_gatekeeper.inspecionar_arquivo(path, extensao)
                relatorio = run_validation(str(path), "." + extensao, limits)
                liberado = security_gatekeeper.ler_liberado(path, extensao, teto)
            except ETLInputError as erro:
                security_gatekeeper.registrar_recusa(erro, sha256=digest.hexdigest(), tamanho=len(content),
                                                     extensao=extensao, origem="upload")
                raise
            if liberado is not None:
                original = digest.hexdigest()
                content, digest = liberado, hashlib.sha256(liberado)
                security_gatekeeper.registrar_liberacao(relatorio, sha256_original=original,
                                                        sha256_liberado=digest.hexdigest(), origem="upload")
    except OSError:
        raise ETLInputError("upload_io", "Não foi possível preparar o arquivo enviado. Tente novamente.") from None
    return bytes(content), digest.hexdigest()
