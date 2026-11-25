"""SEC-005: leitura limitada e preflight, sem calcular indicadores no upload."""

from collections import Counter, deque
from contextlib import contextmanager
import hashlib
from functools import wraps
import logging
import stat
import threading
import time

from flask import flash, g, redirect, request, url_for
from flask_login import current_user
from pathlib import Path
import tempfile

from etl_erros import ERRNOS_DE_DISCO, ETLInputError
from etl_limits import get_limits
from etl_runtime import run_validation
import falhas_banco
import security_gatekeeper

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024

# Sala de espera da vaga única de upload/processamento: um processamento isolado por vez, como
# sempre, mas em vez da recusa imediata o pedido espera a vez numa fila curta em memória, por
# ordem de chegada. No máximo 4 esperam ao mesmo tempo, um por empresa (o mesmo cliente com duas
# abas não ocupa dois lugares), cada um por até 20 s; depois disso, ou com a fila cheia, a
# recusa diz quando tentar de novo e não desconta a cota de envios (deduzir_cota). Quem espera
# não segura conexão do banco: a transação de leitura é encerrada antes (falhas_banco.rollback_seguro).
ESPERA_MAXIMA_S = 20
MAX_NA_ESPERA = 4
TENTAR_DE_NOVO_S = 30
MENSAGEM_OCUPADO = ("Há outro envio ou processamento em andamento e nada foi salvo. Tente novamente em cerca de "
                    f"{TENTAR_DE_NOVO_S} segundos.")


class _SalaDeEspera:
    """Vaga única com fila de espera limitada, por ordem de chegada.

    Também tem a interface de semáforo (acquire sem espera e release), que os testes usam para
    simular a vaga ocupada. Quando a própria thread já segura a vaga, entrar() recusa na hora:
    esperar por si mesma só terminaria no prazo."""

    def __init__(self, vagas=1, max_espera=MAX_NA_ESPERA):
        self._condicao = threading.Condition()
        self._vagas = vagas
        self._max_espera = max_espera
        self._ocupadas = 0
        self._fila = deque()
        self._chaves = set()
        self._donos = Counter()

    def _tomar(self, dono):
        self._ocupadas += 1
        self._donos[dono] += 1

    def acquire(self, blocking=False):
        if blocking:
            raise ValueError("A sala de espera não bloqueia por acquire(); use entrar().")
        with self._condicao:
            if self._ocupadas < self._vagas and not self._fila:
                self._tomar(threading.get_ident())
                return True
            return False

    def release(self):
        with self._condicao:
            if self._ocupadas <= 0:
                raise ValueError("Vaga liberada sem ter sido obtida.")
            self._ocupadas -= 1
            dono = threading.get_ident()
            if not self._donos[dono]:
                dono = next(iter(+self._donos), dono)
            self._donos[dono] -= 1
            self._donos = +self._donos
            self._condicao.notify_all()

    def na_espera(self) -> int:
        with self._condicao:
            return len(self._fila)

    def recusaria_agora(self, chave) -> bool:
        """entrar(chave, ...) recusaria já, sem esperar? (vaga ocupada e sem lugar na fila para ela)"""
        with self._condicao:
            if self._ocupadas < self._vagas and not self._fila:
                return False
            return bool(self._donos[threading.get_ident()] or len(self._fila) >= self._max_espera
                        or (chave is not None and chave in self._chaves))

    def entrar(self, chave, prazo_s, antes_de_esperar=None) -> bool:
        """True com a vaga obtida (agora ou depois da espera); False na recusa."""
        dono = threading.get_ident()
        with self._condicao:
            if self._ocupadas < self._vagas and not self._fila:
                self._tomar(dono)
                return True
            if (prazo_s <= 0 or self._donos[dono] or len(self._fila) >= self._max_espera
                    or (chave is not None and chave in self._chaves)):
                return False
            vez = object()
            self._fila.append(vez)
            if chave is not None:
                self._chaves.add(chave)
        try:
            if antes_de_esperar is not None:
                antes_de_esperar()
            limite = time.monotonic() + prazo_s
            with self._condicao:
                while True:
                    if self._fila[0] is vez and self._ocupadas < self._vagas:
                        self._fila.popleft()
                        self._tomar(dono)
                        return True
                    restante = limite - time.monotonic()
                    if restante <= 0:
                        return False
                    self._condicao.wait(restante)
        finally:
            with self._condicao:
                if vez in self._fila:
                    self._fila.remove(vez)
                if chave is not None:
                    self._chaves.discard(chave)
                self._condicao.notify_all()


_UPLOAD_ADMISSION = _SalaDeEspera()


def _chave_de_espera() -> str:
    """Um lugar na espera por empresa (cliente), por usuário (admin) e, sem sessão, por IP."""
    try:
        if current_user.is_authenticated:
            empresa = getattr(current_user, "id_empresa", None)
            return f"empresa:{empresa}" if empresa else f"usuario:{current_user.get_id()}"
    except Exception:  # noqa: BLE001 - sem usuário legível, a chave é o IP
        pass
    return f"ip:{request.remote_addr}"


def _obter_vaga() -> bool:
    """A vaga única, esperando a vez quando preciso. A recusa marca o pedido (deduzir_cota)."""
    sala = _UPLOAD_ADMISSION
    entrar = getattr(sala, "entrar", None)
    if entrar is None:  # substituída por um semáforo comum (testes de concorrência)
        obtida = sala.acquire(blocking=False)
    else:
        inicio = time.monotonic()
        obtida = entrar(_chave_de_espera(), ESPERA_MAXIMA_S, antes_de_esperar=falhas_banco.rollback_seguro)
        esperou = time.monotonic() - inicio
        if esperou >= 1:
            logger.info("Vaga de upload/processamento %s depois de %.1f s na espera.",
                        "obtida" if obtida else "recusada", esperou)
    if not obtida:
        g.nexo_vaga_ocupada = True
    return obtida


def vaga_recusaria_agora() -> bool:
    """A recusa é certa (vaga ocupada e nenhum lugar na fila para este pedido)? Serve a quem tem um
    preparo caro antes da vaga (o download do lote do lead): sem lugar, nada é baixado à toa."""
    recusaria = getattr(_UPLOAD_ADMISSION, "recusaria_agora", None)
    if recusaria is None or not recusaria(_chave_de_espera()):
        return False
    g.nexo_vaga_ocupada = True
    return True


def deduzir_cota(resposta) -> bool:
    """deduct_when dos limites de envio: o pedido recusado por vaga ocupada não gasta a cota."""
    return not g.get("nexo_vaga_ocupada", False)


def _limitar_entrada(funcao, destino_busy=None):
    """Uma entrada ETL/upload por processo, incluindo bytes e espera por DB."""
    @wraps(funcao)
    def protegido(*args, **kwargs):
        if request.method != "POST":
            return funcao(*args, **kwargs)
        if not _obter_vaga():
            flash(MENSAGEM_OCUPADO, "warning")
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
    """A mesma vaga única de upload/processamento (com a sala de espera), para a rota que monta a
    própria resposta de "ocupado" (JSON na API do Raio-X). Rende True quando a vaga foi obtida."""
    obtida = _obter_vaga()
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
    Enquanto o processo isolado roda, o arquivo fica só no disco: os bytes são lidos uma vez,
    depois da validação.
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
    tamanho = 0
    try:
        with tempfile.TemporaryDirectory(prefix="nexo-etl-upload-") as directory:
            path = Path(directory) / ("arquivo." + extensao)
            with path.open("wb") as destination:
                path.chmod(0o600)
                while True:
                    chunk = arquivo.read(min(_CHUNK_SIZE, teto + 1 - tamanho))
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ETLInputError("upload_io", "Não foi possível ler o arquivo enviado.")
                    if tamanho + len(chunk) > teto:
                        raise ETLInputError("file_size", "Arquivo acima do tamanho permitido para processamento.")
                    tamanho += len(chunk)
                    digest.update(chunk)
                    destination.write(chunk)
            if not tamanho:
                raise ETLInputError("empty_file", "Arquivo vazio — nada foi salvo.")
            try:
                security_gatekeeper.inspecionar_arquivo(path, extensao)
                relatorio = run_validation(str(path), "." + extensao, limits)
                liberado = security_gatekeeper.ler_liberado(path, extensao, teto)
            except ETLInputError as erro:
                security_gatekeeper.registrar_recusa(erro, sha256=digest.hexdigest(), tamanho=tamanho,
                                                     extensao=extensao, origem="upload")
                raise
            if liberado is None:
                content = _reler_recebido(path, tamanho, digest, teto)
            else:
                original = digest.hexdigest()
                content, digest = liberado, hashlib.sha256(liberado)
                security_gatekeeper.registrar_liberacao(relatorio, sha256_original=original,
                                                        sha256_liberado=digest.hexdigest(), origem="upload")
    except OSError as erro:
        if getattr(erro, "errno", None) in ERRNOS_DE_DISCO:
            raise  # disco do servidor cheio: 503 com Retry-After (app._disco_cheio), nada gravado
        raise ETLInputError("upload_io", "Não foi possível preparar o arquivo enviado. Tente novamente.") from None
    return content, digest.hexdigest()


def _reler_recebido(path: Path, tamanho: int, digest, teto: int) -> bytes:
    """O arquivo recebido, lido do disco depois da validação (o processo isolado já terminou): tem de ser
    um arquivo comum com o mesmo tamanho e o mesmo SHA-256 da leitura em blocos."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ETLInputError("upload_io", "Não foi possível preparar o arquivo enviado. Tente novamente.")
    with path.open("rb") as origem:
        conteudo = origem.read(teto + 1)
    if len(conteudo) != tamanho or hashlib.sha256(conteudo).digest() != digest.digest():
        raise ETLInputError("upload_io", "Não foi possível preparar o arquivo enviado. Tente novamente.")
    return conteudo
