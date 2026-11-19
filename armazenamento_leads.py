"""
NEXO - Planilhas dos leads da landing no Storage do Supabase (só leitura)
=========================================================================
O microsserviço do Raio-X (microservico_raiox/, serviço à parte) faz a triagem de segurança dos relatórios que o
visitante envia pela landing, grava os aprovados no bucket privado do Storage e registra no lead, na coluna
arquivos_storage, um JSON com o caminho, o nome exibido, o tamanho, o formato e o SHA-256 de cada um. Este módulo
é o lado da aplicação principal: só LÊ objetos, para o consultor baixar as planilhas e processá-las na Avaliação de
Cenário (blueprints/leads.py). Segue o cliente do microsserviço (microservico_raiox/armazenamento.py), sem importar
dele, só com a biblioteca padrão.

Configuração (config.py):
  SUPABASE_KEY     a chave do servidor: a secreta nova (sb_secret_..., que vai só no cabeçalho apikey) ou a
                   service_role legada (um JWT, que vai no apikey e no Authorization). A publicável não lê o bucket
                   privado e vale como ausente. Nunca vai para mensagem de erro, log, repr nem template.
  SUPABASE_BUCKET  o bucket privado (padrão: leads-raio-x, o mesmo do microsserviço).
  O endereço https://<projeto>.supabase.co sai da conexão do banco da aplicação (DATABASE_URL): a direta
  (db.<projeto>.supabase.co) ou o usuário <papel>.<projeto> do pooler. Sem conexão do Supabase (o SQLite do
  desenvolvimento e dos testes), sem a chave ou com bucket inválido, não há Storage (do_app devolve None) e a tela
  mostra os botões desabilitados com MENSAGEM_SEM_CONFIGURACAO.

Regras da leitura:
  - Só GET de objeto, por HTTPS (HTTP só no localhost, para os testes), sem seguir redirecionamento: a chave nunca
    vai para outro endereço.
  - Prazo total de 30 s por arquivo (não só por operação de rede: um servidor que pinga um byte por vez também
    para) e teto de 15 MB, o mesmo do microsserviço: acima dele, a leitura é recusada sem guardar o excedente.
  - O que vem do banco é conferido antes de qualquer pedido (planilhas_do_lead): JSON inválido, item fora do
    formato, caminho que não seja exatamente o de um relatório DESTE lead (AAAA/MM/<id>/NN-<16 hex>.<csv|xlsx>),
    formato fora de csv e xlsx, SHA-256 que não começa pelos 16 hexadecimais do caminho ou tamanho fora de 1 byte a
    15 MB tiram o item da lista, e ele nunca vira pedido ao Storage. O conteúdo baixado tem de bater com o tamanho e
    o SHA-256 registrados (baixar, e baixar_para, que grava direto num arquivo).
  - Nenhuma mensagem de erro leva a chave, o endereço ou o caminho do objeto: só o status ou o tipo da falha.
"""

import base64
from dataclasses import dataclass
import hashlib
import http.client
import io
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from werkzeug.utils import secure_filename

BUCKET_PADRAO = "leads-raio-x"
MEGABYTE = 1024 * 1024
# O teto de cada relatório no microsserviço (microservico_raiox/configuracao.py, MAX_BYTES).
MAX_BYTES = 15 * MEGABYTE
# Relatórios por envio no microsserviço (MAX_ARQUIVOS): mais que isso no JSON não veio dele.
MAX_PLANILHAS = 12
# O microsserviço recusa o envio que passa de 15 MB somados: um registro que soma mais não veio dele, e o excedente
# fica de fora (o .zip monta tudo na memória do processo web; o processamento grava as planilhas em disco).
TOTAL_MAXIMO = MAX_BYTES
PRAZO_SEGUNDOS = 30
# O CHECK da coluna arquivos_storage do lead (models.ARQUIVOS_STORAGE_MAXIMO): texto maior não é lido.
TEXTO_MAXIMO = 20000
NOME_MAXIMO = 100
_BLOCO = 64 * 1024
MENSAGEM_SEM_CONFIGURACAO = "Configure SUPABASE_KEY no serviço para baixar e processar as planilhas"
TIPOS = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}

_BUCKET = re.compile(r"[a-z0-9][a-z0-9_-]{2,62}")
_PROJETO = re.compile(r"[a-z0-9]{20}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Só dígitos ASCII (o str.isdigit() aceita "²", que o int() recusa).
_INTEIRO = re.compile(r"[0-9]{1,15}")
# Nomes que o Windows reserva para dispositivos: o arquivo do .zip não seria extraído com eles.
_NOMES_RESERVADOS = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                               *(f"LPT{n}" for n in range(1, 10))})
_HOSTS_LOCAIS = ("localhost", "127.0.0.1")


class ErroDeArmazenamento(RuntimeError):
    """O Storage recusou o pedido, não respondeu a tempo ou devolveu o que não confere com o registrado. A mensagem
    nunca traz a chave, o endereço nem o caminho do objeto."""

    def __init__(self, mensagem: str, status: int = None):
        super().__init__(mensagem)
        self.status = status


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_argumentos, **_opcoes):
        return None


def cabecalhos_da_chave(chave: str) -> dict:
    """Os cabeçalhos de autenticação: a chave nova (sb_...) só no apikey (no Authorization ela seria lida como JWT e
    recusada); a service_role legada, que é um JWT, também no Authorization."""
    if chave.startswith("sb_"):
        return {"apikey": chave}
    return {"Authorization": f"Bearer {chave}", "apikey": chave}


def chave_do_servidor(chave) -> bool:
    """A chave secreta nova (sb_secret_...) ou a service_role legada, um JWT cuja carga diz role service_role (lida
    sem conferir a assinatura: só para não aceitar por engano a publicável, que não lê o bucket privado)."""
    if not isinstance(chave, str) or not chave:
        return False
    if chave.startswith("sb_"):
        return chave.startswith("sb_secret_")
    partes = chave.split(".")
    if len(partes) != 3:
        return False
    try:
        carga = json.loads(base64.urlsafe_b64decode(partes[1] + "=" * (-len(partes[1]) % 4)))
    except (ValueError, TypeError, RecursionError):
        return False
    return isinstance(carga, dict) and carga.get("role") == "service_role"


def url_do_projeto(database_url) -> str:
    """https://<projeto>.supabase.co a partir da conexão do banco, lida pelo SQLAlchemy como a aplicação a lê: o host
    db.<projeto>.supabase.co (direta) ou o usuário <papel>.<projeto> do pooler (*.pooler.supabase.com). Vazio quando
    não é uma conexão PostgreSQL do Supabase."""
    if not isinstance(database_url, str) or not database_url.strip():
        return ""
    try:
        url = make_url(database_url.strip())
        backend = url.get_backend_name()
    except (ArgumentError, ValueError, TypeError):
        return ""
    if backend not in ("postgresql", "postgres"):
        return ""
    host = (url.host or "").lower()
    usuario = url.username or ""
    projeto = ""
    if host.startswith("db.") and host.endswith(".supabase.co"):
        projeto = host[len("db."):-len(".supabase.co")]
    elif host.endswith(".pooler.supabase.com") and "." in usuario:
        projeto = usuario.rsplit(".", 1)[1]
    return f"https://{projeto}.supabase.co" if _PROJETO.fullmatch(projeto) else ""


# =====================================================================
# As planilhas registradas no lead
# =====================================================================
@dataclass(frozen=True)
class Planilha:
    """Um relatório aprovado pelo microsserviço, já conferido (planilhas_do_lead). `indice` é o NN do caminho, a
    ordem do envio; `nome` é o nome que o visitante deu ao arquivo, limpo pelo microsserviço e aqui de novo, que a
    tela mostra escapado."""
    indice: int
    caminho: str
    nome: str
    formato: str
    tamanho: int
    sha256: str

    @property
    def tipo(self) -> str:
        return TIPOS[self.formato]

    @property
    def nome_para_baixar(self) -> str:
        """O nome do arquivo baixado: o nome original sem a extensão, pelo secure_filename (só ASCII, sem pasta), com
        a extensão do formato; sem nada aproveitável (ou com um nome que o Windows reserva), relatorio-NN.<formato>."""
        base = secure_filename(os.path.splitext(self.nome)[0])[:NOME_MAXIMO].strip("._-")
        if not base or base.split(".", 1)[0].upper() in _NOMES_RESERVADOS:
            base = f"relatorio-{self.indice:02d}"
        return f"{base}.{self.formato}"


def _padrao_do_caminho(lead_id: str):
    """O caminho que o microsserviço monta para os relatórios do lead (microservico_raiox/app.py): o mês do envio, o
    id do lead em minúsculas, a ordem (01 a 99) e o começo do SHA-256, com a extensão do formato."""
    return re.compile(r"20[0-9]{2}/(?:0[1-9]|1[0-2])/" + re.escape(lead_id)
                      + r"/(?P<indice>0[1-9]|[1-9][0-9])-(?P<prefixo>[0-9a-f]{16})\.(?P<formato>csv|xlsx)")


def _nome_exibido(nome: str, indice: int, formato: str) -> str:
    """O nome original sem caractere de controle ou invisível e com os espaços juntados, até 100 caracteres (o
    microsserviço já o limpa; o banco é a fonte, então a limpeza se repete)."""
    texto = "".join(caractere for caractere in nome if unicodedata.category(caractere) not in ("Cc", "Cf", "Zl", "Zp"))
    texto = " ".join(texto.split())[:NOME_MAXIMO]
    return texto or f"relatorio-{indice:02d}.{formato}"


def _planilha(item, padrao):
    if not isinstance(item, dict):
        return None
    caminho, nome, formato, sha256 = (item.get(campo) for campo in ("caminho", "nome", "formato", "sha256"))
    tamanho = item.get("bytes")
    if not all(isinstance(valor, str) for valor in (caminho, nome, formato, sha256)):
        return None
    # type() e não isinstance(): True e False não são tamanhos.
    if type(tamanho) is not int or not 1 <= tamanho <= MAX_BYTES:
        return None
    achado = padrao.fullmatch(caminho)
    if (achado is None or achado["formato"] != formato or not _SHA256.fullmatch(sha256)
            or not sha256.startswith(achado["prefixo"])):
        return None
    indice = int(achado["indice"])
    return Planilha(indice, caminho, _nome_exibido(nome, indice, formato), formato, tamanho, sha256)


def planilhas_do_lead(lead_id, texto) -> list:
    """As planilhas do JSON arquivos_storage de um lead que passam em todas as conferências, em ordem de índice (sem
    índice repetido, até MAX_PLANILHAS e até TOTAL_MAXIMO somados). Qualquer outra coisa (texto que não é JSON, JSON
    que não é lista, item fora do formato ou de outro lead) fica de fora, sem erro: a tela segue com o que sobrou."""
    if not isinstance(texto, str) or not texto or len(texto) > TEXTO_MAXIMO:
        return []
    try:
        identificador = str(lead_id if isinstance(lead_id, uuid.UUID) else uuid.UUID(str(lead_id)))
    except (ValueError, TypeError, AttributeError):
        return []
    try:
        dados = json.loads(texto)
    except (ValueError, RecursionError):
        return []
    if not isinstance(dados, list):
        return []
    padrao = _padrao_do_caminho(identificador)
    planilhas, indices, total = [], set(), 0
    for item in dados:
        planilha = _planilha(item, padrao)
        if planilha is None or planilha.indice in indices or total + planilha.tamanho > TOTAL_MAXIMO:
            continue
        indices.add(planilha.indice)
        total += planilha.tamanho
        planilhas.append(planilha)
        if len(planilhas) == MAX_PLANILHAS:
            break
    return sorted(planilhas, key=lambda planilha: planilha.indice)


def nomes_unicos(planilhas) -> list:
    """Os nomes de baixar das planilhas, sem repetir (sem diferenciar maiúsculas, como no Windows): o segundo
    "vendas.csv" vira "vendas-2.csv". Para o .zip."""
    vistos, nomes = set(), []
    for planilha in planilhas:
        nome = planilha.nome_para_baixar
        base, extensao = os.path.splitext(nome)
        candidato, numero = nome, 2
        while candidato.lower() in vistos:
            candidato = f"{base}-{numero}{extensao}"
            numero += 1
        vistos.add(candidato.lower())
        nomes.append(candidato)
    return nomes


def montar_zip(arquivos) -> bytes:
    """Um .zip em memória com os pares (nome, conteúdo), na ordem dada."""
    saida = io.BytesIO()
    with zipfile.ZipFile(saida, "w", compression=zipfile.ZIP_DEFLATED) as pacote:
        for nome, conteudo in arquivos:
            pacote.writestr(nome, conteudo)
    return saida.getvalue()


# =====================================================================
# O cliente do Storage
# =====================================================================
class ArmazenamentoLeads:
    def __init__(self, url_base: str, chave: str, bucket: str = BUCKET_PADRAO, timeout: float = PRAZO_SEGUNDOS,
                 max_bytes: int = MAX_BYTES):
        try:
            partes = urllib.parse.urlsplit(url_base)
            host = partes.hostname
        except (ValueError, TypeError, AttributeError):
            # A mensagem do urlsplit traz o endereço; a desta exceção, não.
            raise ValueError("O Storage precisa de um endereço https.") from None
        local = partes.scheme == "http" and host in _HOSTS_LOCAIS
        if not (partes.scheme == "https" or local) or not host:
            raise ValueError("O Storage precisa de um endereço https.")
        if not chave or not isinstance(chave, str) or not bucket or not _BUCKET.fullmatch(bucket):
            raise ValueError("Faltam a chave ou o bucket do Storage.")
        self._base = url_base.rstrip("/")
        self._chave = chave
        self._bucket = bucket
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._abridor = urllib.request.build_opener(_SemRedirecionar)

    def __repr__(self) -> str:
        return f"ArmazenamentoLeads(bucket={self._bucket!r})"

    @property
    def bucket(self) -> str:
        return self._bucket

    def _url(self, caminho: str) -> str:
        return (f"{self._base}/storage/v1/object/{urllib.parse.quote(self._bucket, safe='')}/"
                f"{urllib.parse.quote(caminho, safe='/')}")

    def ler(self, caminho: str, maximo: int = None) -> bytes:
        """O conteúdo do objeto, até `maximo` bytes (e nunca além do teto); ErroDeArmazenamento em qualquer falha:
        status de erro, redirecionamento, rede, resposta cortada ou malformada, prazo total vencido e objeto maior que
        o teto (pelo Content-Length ou pelo que chegou)."""
        return b"".join(self._blocos(caminho, maximo))

    def _blocos(self, caminho: str, maximo: int = None):
        """Os blocos do objeto (até 64 KiB cada), na ordem em que chegam, com as mesmas conferências e recusas do
        ler."""
        maximo = self._max_bytes if maximo is None else min(maximo, self._max_bytes)
        prazo = time.monotonic() + self._timeout
        pedido = urllib.request.Request(self._url(caminho), method="GET")
        for nome, valor in cabecalhos_da_chave(self._chave).items():
            pedido.add_header(nome, valor)
        try:
            # O esquema foi conferido no construtor (https, ou http só no localhost).
            with self._abridor.open(pedido, timeout=self._timeout) as resposta:  # nosec B310
                declarado = resposta.headers.get("Content-Length")
                if declarado is not None:
                    if not _INTEIRO.fullmatch(declarado.strip()):
                        raise ErroDeArmazenamento("o Storage respondeu fora do formato")
                    if int(declarado) > maximo:
                        raise ErroDeArmazenamento("o arquivo passa do tamanho permitido")
                lidos = 0
                while True:
                    if time.monotonic() > prazo:
                        raise ErroDeArmazenamento("o Storage não terminou de responder no prazo")
                    # read1: devolve o que já chegou (até o bloco), e o prazo total é conferido a cada parte.
                    bloco = resposta.read1(min(_BLOCO, maximo + 1 - lidos))
                    if not bloco:
                        break
                    lidos += len(bloco)
                    if lidos > maximo:
                        raise ErroDeArmazenamento("o arquivo passa do tamanho permitido")
                    yield bloco
        except ErroDeArmazenamento:
            raise
        except urllib.error.HTTPError as erro:
            status = erro.code
            erro.close()
            raise ErroDeArmazenamento(f"o Storage respondeu {status}", status) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as erro:
            # HTTPException: resposta cortada, linha de status ou cabeçalho malformado (não são OSError).
            raise ErroDeArmazenamento(f"o Storage não respondeu ({type(erro).__name__})") from None

    def baixar(self, planilha: Planilha) -> bytes:
        """O conteúdo da planilha, conferido com o tamanho e o SHA-256 registrados pelo microsserviço."""
        conteudo = self.ler(planilha.caminho, maximo=planilha.tamanho)
        if len(conteudo) != planilha.tamanho or hashlib.sha256(conteudo).hexdigest() != planilha.sha256:
            raise ErroDeArmazenamento("o arquivo do Storage não confere com o registrado")
        return conteudo

    def baixar_para(self, planilha: Planilha, destino) -> None:
        """Grava a planilha no arquivo novo `destino`, bloco a bloco (nada dela fica na memória do processo web), com
        as conferências do baixar: o mesmo teto e o tamanho e o SHA-256 registrados pelo microsserviço. Se não
        confere, ErroDeArmazenamento, e o arquivo gravado fica para quem chamou descartar (a pasta temporária dele)."""
        digest, tamanho = hashlib.sha256(), 0
        blocos = self._blocos(planilha.caminho, maximo=planilha.tamanho)
        try:
            with open(destino, "xb") as arquivo:
                for bloco in blocos:
                    tamanho += len(bloco)
                    digest.update(bloco)
                    arquivo.write(bloco)
        finally:
            blocos.close()
        if tamanho != planilha.tamanho or digest.hexdigest() != planilha.sha256:
            raise ErroDeArmazenamento("o arquivo do Storage não confere com o registrado")


def do_app(config) -> ArmazenamentoLeads:
    """O cliente do Storage pela configuração da aplicação (SUPABASE_KEY, SUPABASE_BUCKET e a conexão do banco), ou
    None quando falta algo: chave ausente ou que não é a do servidor, banco que não é o do Supabase ou bucket
    inválido."""
    chave = str(config.get("SUPABASE_KEY") or "").strip()
    bucket = str(config.get("SUPABASE_BUCKET") or BUCKET_PADRAO).strip()
    base = url_do_projeto(config.get("SQLALCHEMY_DATABASE_URI") or "")
    if not chave_do_servidor(chave) or not base or not _BUCKET.fullmatch(bucket):
        return None
    return ArmazenamentoLeads(base, chave, bucket)
