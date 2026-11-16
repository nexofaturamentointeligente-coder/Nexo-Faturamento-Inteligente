"""Cria (ou confere) o bucket privado dos relatórios no Storage do Supabase.

Uso, com SUPABASE_KEY (a chave secreta, sb_secret_..., ou a service_role legada) e SUPABASE_URL no ambiente (o
DATABASE_URL só é preciso quando falta o SUPABASE_URL, para tirá-la dele; as demais variáveis do serviço não são
lidas aqui):
    python preparar_bucket.py

Idempotente: se o bucket já existe, só confere que é privado e mostra os limites dele. O bucket nasce privado,
com o teto de 15 MB por arquivo e só os tipos de .csv e .xlsx. Nunca imprime a chave. Sai com 0 só com o
bucket privado.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from armazenamento import cabecalhos_da_chave
from configuracao import Configuracao
from sanitizacao import TIPOS


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_argumentos, **_opcoes):
        return None


def _pedir(configuracao, metodo, caminho, corpo=None):
    partes = urllib.parse.urlsplit(configuracao.supabase_url)
    if partes.scheme != "https":
        raise SystemExit("SUPABASE_URL precisa ser https.")
    pedido = urllib.request.Request(f"{configuracao.supabase_url}/storage/v1/{caminho}", method=metodo,
                                    data=json.dumps(corpo).encode() if corpo is not None else None)
    for nome, valor in cabecalhos_da_chave(configuracao.supabase_key).items():
        pedido.add_header(nome, valor)
    pedido.add_header("Content-Type", "application/json")
    abridor = urllib.request.build_opener(_SemRedirecionar)
    try:
        with abridor.open(pedido, timeout=30) as resposta:  # nosec B310 - esquema https conferido acima
            return resposta.status, json.loads(resposta.read(65536) or b"null")
    except urllib.error.HTTPError as erro:
        try:
            detalhe = json.loads(erro.read(65536) or b"null")
        except ValueError:
            detalhe = None
        return erro.code, detalhe


def problemas_do_bucket(configuracao, ambiente) -> list:
    """Só o que impede falar com o Storage; o DATABASE_URL conta apenas quando o SUPABASE_URL não foi dado."""
    precisa_do_banco = not str(ambiente.get("SUPABASE_URL") or "").strip()
    return [problema for problema in configuracao.problemas
            if problema.startswith("SUPABASE_") or (precisa_do_banco and problema.startswith("DATABASE_URL"))]


def principal(ambiente=None) -> int:
    ambiente = os.environ if ambiente is None else ambiente
    configuracao = Configuracao.de_ambiente(ambiente)
    faltando = problemas_do_bucket(configuracao, ambiente)
    if faltando:
        print("Configuração incompleta:", *faltando, sep="\n  ")
        return 2
    bucket = configuracao.bucket
    status, resposta = _pedir(configuracao, "POST", "bucket", {
        "id": bucket, "name": bucket, "public": False, "file_size_limit": configuracao.max_bytes,
        "allowed_mime_types": sorted(TIPOS.values()),
    })
    if status in (200, 201):
        print(f"Bucket {bucket} criado, privado.")
    else:
        print(f"Bucket {bucket} não criado agora (resposta {status}); conferindo o que existe.")
    status, dados = _pedir(configuracao, "GET", f"bucket/{urllib.parse.quote(bucket, safe='')}")
    if status != 200 or not isinstance(dados, dict):
        print(f"Não foi possível ler o bucket {bucket} (resposta {status}).")
        return 1
    print(f"Bucket {bucket}: público = {dados.get('public')}, teto por arquivo = {dados.get('file_size_limit')}, "
          f"tipos = {dados.get('allowed_mime_types')}")
    if dados.get("public") is not False:
        print("ATENÇÃO: o bucket não é privado. Torne-o privado no painel do Supabase antes de usar o serviço.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(principal())
