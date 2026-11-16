"""Cliente mínimo do Storage do Supabase (API REST), só com a biblioteca padrão.

Grava e apaga objetos num bucket privado com a chave do servidor (SUPABASE_KEY): por HTTPS (HTTP só no
localhost, para testes), sem seguir redirecionamento (a chave nunca vai para outro endereço), sem sobrescrever
objeto existente e sem pôr a chave em mensagem de erro. A chave secreta nova (sb_secret_...) vai só no
cabeçalho apikey, como o Supabase pede (no Authorization ela seria lida como JWT e recusada); a service_role
legada, que é um JWT, vai nos dois. Qualquer falha de rede ou de protocolo (inclusive resposta cortada ou
malformada) vira ErroDeArmazenamento; apagar um objeto que não existe não é erro.
"""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

RESPOSTA_MAXIMA = 64 * 1024


class ErroDeArmazenamento(RuntimeError):
    """O Storage recusou o pedido ou não respondeu."""

    def __init__(self, mensagem: str, status: int = None, objeto_ausente: bool = False):
        super().__init__(mensagem)
        self.status = status
        self.objeto_ausente = objeto_ausente


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_argumentos, **_opcoes):
        return None


def cabecalhos_da_chave(chave: str) -> dict:
    """Os cabeçalhos de autenticação: a chave nova (sb_...) só no apikey; a service_role legada (JWT) também no
    Authorization."""
    if chave.startswith("sb_"):
        return {"apikey": chave}
    return {"Authorization": f"Bearer {chave}", "apikey": chave}


def _objeto_ausente(status: int, corpo: bytes) -> bool:
    """Se a resposta de erro diz que o objeto não existe (o Storage responde 404, ou 400 com statusCode 404)."""
    if status == 404:
        return True
    if status != 400:
        return False
    try:
        dados = json.loads(corpo or b"null")
    except ValueError:
        return False
    if not isinstance(dados, dict):
        return False
    return str(dados.get("statusCode")) == "404" or str(dados.get("error", "")).lower() in ("not_found", "not found")


class ArmazenamentoSupabase:
    def __init__(self, url_base: str, chave: str, bucket: str, timeout: int = 30):
        try:
            partes = urllib.parse.urlsplit(url_base)
            host = partes.hostname
        except ValueError:
            # A mensagem do urlsplit traz o endereço; a desta exceção, não.
            raise ValueError("O Storage precisa de um endereço https.") from None
        local = partes.scheme == "http" and host in ("localhost", "127.0.0.1")
        if not (partes.scheme == "https" or local) or not host:
            raise ValueError("O Storage precisa de um endereço https.")
        if not chave or not bucket:
            raise ValueError("Faltam a chave ou o bucket do Storage.")
        self._base = url_base.rstrip("/")
        self._chave = chave
        self._bucket = bucket
        self._timeout = timeout
        self._abridor = urllib.request.build_opener(_SemRedirecionar)

    def __repr__(self) -> str:
        return f"ArmazenamentoSupabase(bucket={self._bucket!r})"

    def _url(self, caminho: str) -> str:
        return (f"{self._base}/storage/v1/object/{urllib.parse.quote(self._bucket, safe='')}/"
                f"{urllib.parse.quote(caminho, safe='/')}")

    def _pedir(self, metodo: str, url: str, corpo: bytes = None, cabecalhos: dict = None) -> bytes:
        """O corpo da resposta de sucesso (até 64 KB); ErroDeArmazenamento em qualquer falha."""
        pedido = urllib.request.Request(url, data=corpo, method=metodo)
        for nome, valor in {**cabecalhos_da_chave(self._chave), **(cabecalhos or {})}.items():
            pedido.add_header(nome, valor)
        try:
            # O esquema foi conferido no construtor (https, ou http só no localhost).
            with self._abridor.open(pedido, timeout=self._timeout) as resposta:  # nosec B310
                return resposta.read(RESPOSTA_MAXIMA)
        except urllib.error.HTTPError as erro:
            try:
                detalhe = erro.read(RESPOSTA_MAXIMA)
            except (http.client.HTTPException, OSError, ValueError):
                detalhe = b""
            raise ErroDeArmazenamento(f"o Storage respondeu {erro.code}", erro.code,
                                      _objeto_ausente(erro.code, detalhe)) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as erro:
            # HTTPException: resposta cortada, linha de status ou cabeçalho malformado (não são OSError).
            raise ErroDeArmazenamento(f"o Storage não respondeu ({type(erro).__name__})") from None

    def enviar(self, caminho: str, conteudo: bytes, tipo: str) -> None:
        self._pedir("POST", self._url(caminho), conteudo,
                    {"Content-Type": tipo, "x-upsert": "false", "Cache-Control": "no-store"})

    def apagar(self, caminho: str) -> bool:
        """Apaga o objeto: True se apagou, False se ele não existia (o envio não chegou a gravar, ou ainda não
        terminou de gravar: o Storage cria o registro do objeto só no fim)."""
        try:
            self._pedir("DELETE", self._url(caminho))
        except ErroDeArmazenamento as erro:
            if not erro.objeto_ausente:
                raise
            return False
        return True

    def conferir_bucket(self) -> bool:
        """Na subida: se o bucket existe e é privado (True) ou público (False); ErroDeArmazenamento se a chave
        não serve, o bucket não existe ou o Storage não responde."""
        corpo = self._pedir("GET", f"{self._base}/storage/v1/bucket/{urllib.parse.quote(self._bucket, safe='')}")
        try:
            dados = json.loads(corpo or b"null")
        except ValueError:
            raise ErroDeArmazenamento("o Storage respondeu fora do formato") from None
        if not isinstance(dados, dict):
            raise ErroDeArmazenamento("o Storage respondeu fora do formato")
        return dados.get("public") is False
