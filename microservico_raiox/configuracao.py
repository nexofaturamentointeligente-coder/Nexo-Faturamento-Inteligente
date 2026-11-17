"""Configuração do microsserviço do Raio-X, só por variáveis de ambiente.

Credenciais (nunca no código nem no repositório):
  DATABASE_URL        conexão PostgreSQL do Supabase (o mesmo banco da aplicação principal), lida como a
                      aplicação principal a lê. Os parâmetros que o Supabase e o Prisma acrescentam (pgbouncer,
                      connection_limit...) são tirados.
  SUPABASE_KEY        chave secreta do projeto (sb_secret_...) ou a service_role legada, para gravar no bucket
                      privado do Storage. Só no servidor; a chave publicável (nova ou a anon legada) é recusada.
Demais:
  SUPABASE_URL        https://<projeto>.supabase.co. Sem ela, sai do DATABASE_URL (conexão direta, ou o usuário
                      <papel>.<projeto> do pooler).
  CORS_ORIGENS        origens da landing de produção, separadas por vírgula (https://host, sem caminho).
  PERMITIR_LOCALHOST  1 para aceitar também http://localhost e http://127.0.0.1, em qualquer porta (testes).
  SUPABASE_BUCKET     bucket privado dos relatórios (padrão: leads-raio-x).
  LIMITE_POR_HORA     envios por IP em 1 hora (padrão 5).
  LIMITE_POR_DIA      envios por IP em 24 horas (padrão 10).
  TETO_DIARIO         leads do microsserviço por dia de Brasília, somando todos os IPs (padrão 100).
  PROXIES_CONFIAVEIS  quantas entradas do X-Forwarded-For são de proxies confiáveis (padrão 2: no Render, a do
                      Cloudflare e a do próprio Render; 0 sem proxy). Conferir pelo log da subida (o serviço
                      registra quantas entradas o cabeçalho traz) antes de abrir a origem.
  PRAZO_DA_TRIAGEM    segundos de triagem por envio (padrão 30, até 300); o relatório que não termina a tempo é
                      recusado.

TERMOS_VERSAO não é mais usada: a versão aceita vem dos documentos publicados no banco (documento_legal), lida na
gravação do lead. No ambiente, ela é ignorada, com um aviso no log da subida (sem o valor).

Configuração incompleta não derruba o serviço: o /ping continua respondendo, o envio responde 503 (403 sem
CORS_ORIGENS, porque a origem é conferida antes) e cada problema vai para o log na subida, sem o valor.
"""

import base64
from dataclasses import dataclass
import json
import os
import re
from urllib.parse import urlsplit

from repositorio import ErroDeBanco, parametros_de_conexao

BUCKET_PADRAO = "leads-raio-x"
MEGABYTE = 1024 * 1024
MAX_BYTES = 15 * MEGABYTE
MAX_ARQUIVOS = 12
PRAZO_MAXIMO_DA_TRIAGEM = 300
AVISO_TERMOS_VERSAO = ("TERMOS_VERSAO não é mais usada e foi ignorada: a versão aceita vem dos documentos publicados "
                       "em Configurações › Documentos Legais da aplicação principal. Pode tirá-la do ambiente.")

_ORIGEM = re.compile(r"https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")
_ORIGEM_LOCAL = re.compile(r"http://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9_-]{2,62}")
_PROJETO = re.compile(r"[a-z0-9]{20}")
_INTEIRO = re.compile(r"[0-9]{1,9}")
_VERDADEIROS = {"1", "true", "sim", "yes", "on"}


def _booleano(valor) -> bool:
    return str(valor or "").strip().lower() in _VERDADEIROS


def _chave_do_servidor(chave: str) -> bool:
    """A chave secreta nova (sb_secret_...) ou a service_role legada, um JWT cuja carga diz role service_role (a
    carga é lida sem conferir a assinatura: só para não aceitar a anon legada, a chave pública, por engano)."""
    if chave.startswith("sb_"):
        return chave.startswith("sb_secret_")
    partes = chave.split(".")
    if len(partes) != 3:
        return False
    try:
        carga = json.loads(base64.urlsafe_b64decode(partes[1] + "=" * (-len(partes[1]) % 4)))
    except (ValueError, TypeError, RecursionError):
        # RecursionError: uma carga com listas aninhadas aos milhares (a chave fica inválida, sem derrubar a subida).
        return False
    return isinstance(carga, dict) and carga.get("role") == "service_role"


def _url_do_projeto(database_url: str) -> str:
    """https://<projeto>.supabase.co a partir da conexão do Supabase, lida como o serviço a lê: db.<projeto>.
    supabase.co (direta) ou o usuário <papel>.<projeto> do pooler, de qualquer papel. Vazio quando não é uma
    conexão do Supabase."""
    try:
        parametros = parametros_de_conexao(database_url)
    except ErroDeBanco:
        return ""
    host = str(parametros.get("host") or "").lower()
    usuario = str(parametros.get("user") or "")
    projeto = ""
    if host.startswith("db.") and host.endswith(".supabase.co"):
        projeto = host[len("db."):-len(".supabase.co")]
    elif host.endswith(".pooler.supabase.com") and "." in usuario:
        projeto = usuario.rsplit(".", 1)[1]
    return f"https://{projeto}.supabase.co" if _PROJETO.fullmatch(projeto) else ""


def _url_de_api_valida(url: str, permitir_local: bool) -> bool:
    try:
        partes = urlsplit(url)
    except ValueError:
        return False
    if partes.path not in ("", "/") or partes.query or partes.fragment or partes.username or partes.password:
        return False
    if partes.scheme == "https" and partes.hostname:
        return True
    return permitir_local and partes.scheme == "http" and partes.hostname in ("localhost", "127.0.0.1")


@dataclass(frozen=True)
class Configuracao:
    database_url: str = ""
    supabase_url: str = ""
    supabase_key: str = ""
    bucket: str = BUCKET_PADRAO
    origens: tuple = ()
    permitir_localhost: bool = False
    limite_por_hora: int = 5
    limite_por_dia: int = 10
    teto_diario: int = 100
    proxies_confiaveis: int = 2
    prazo_da_triagem: int = 30
    max_bytes: int = MAX_BYTES
    max_arquivos: int = MAX_ARQUIVOS
    problemas: tuple = ()
    # O que vai para o log na subida sem impedir o serviço de ficar pronto.
    avisos: tuple = ()

    def __repr__(self) -> str:
        # Sem as credenciais: a configuração pode aparecer num log ou num erro.
        return (f"Configuracao(bucket={self.bucket!r}, origens={self.origens!r}, "
                f"permitir_localhost={self.permitir_localhost}, problemas={len(self.problemas)})")

    @property
    def pronta(self) -> bool:
        return not self.problemas

    def origem_permitida(self, origem) -> bool:
        """Só a origem exata da landing (esquema, host e porta), ou o localhost quando liberado."""
        if not isinstance(origem, str) or not origem:
            return False
        if origem in self.origens:
            return True
        return self.permitir_localhost and _ORIGEM_LOCAL.fullmatch(origem) is not None

    @classmethod
    def de_ambiente(cls, ambiente=None) -> "Configuracao":
        ambiente = os.environ if ambiente is None else ambiente
        problemas = []

        def inteiro(nome, padrao, minimo, maximo=None):
            # Só os dígitos ASCII (o str.isdigit() aceita "²" e "①", que o int() recusa) e até 9 deles.
            bruto = str(ambiente.get(nome) or "").strip()
            if not bruto:
                return padrao
            if not _INTEIRO.fullmatch(bruto) or int(bruto) < minimo or (maximo is not None and int(bruto) > maximo):
                faixa = f"de {minimo} a {maximo}" if maximo is not None else f"a partir de {minimo}"
                problemas.append(f"{nome} precisa ser um número inteiro {faixa}.")
                return padrao
            return int(bruto)

        database_url = str(ambiente.get("DATABASE_URL") or "").strip()
        supabase_key = str(ambiente.get("SUPABASE_KEY") or "").strip()
        permitir_localhost = _booleano(ambiente.get("PERMITIR_LOCALHOST"))
        if not database_url:
            problemas.append("DATABASE_URL ausente.")
        else:
            try:
                parametros_de_conexao(database_url)
            except ErroDeBanco:
                problemas.append("DATABASE_URL inválida: use postgresql://usuario:senha@host:porta/banco.")
        if not supabase_key:
            problemas.append("SUPABASE_KEY ausente.")
        elif not _chave_do_servidor(supabase_key):
            problemas.append("SUPABASE_KEY não é a chave secreta (sb_secret_...) nem a service_role legada: a chave "
                             "publicável (sb_publishable_... ou a anon legada) não grava no bucket privado.")
        supabase_url = str(ambiente.get("SUPABASE_URL") or "").strip().rstrip("/") or _url_do_projeto(database_url)
        if not supabase_url:
            problemas.append("SUPABASE_URL ausente (e não deu para tirá-la do DATABASE_URL).")
        elif not _url_de_api_valida(supabase_url, permitir_localhost):
            problemas.append("SUPABASE_URL precisa ser https://<projeto>.supabase.co, sem caminho.")

        partes = [parte.strip().rstrip("/").lower() for parte in str(ambiente.get("CORS_ORIGENS") or "").split(",")
                  if parte.strip()]
        # Sem repetir o valor no log: uma credencial colada na variável errada iria junto.
        invalidas = [str(posicao) for posicao, parte in enumerate(partes, 1) if not _ORIGEM.fullmatch(parte)]
        if invalidas:
            problemas.append("CORS_ORIGENS aceita só https://host[:porta], sem caminho; inválida(s) na posição "
                             + ", ".join(invalidas) + " da lista.")
        origens = tuple(dict.fromkeys(parte for parte in partes if _ORIGEM.fullmatch(parte)))
        if not origens and not permitir_localhost:
            problemas.append("CORS_ORIGENS vazia: nenhuma landing pode enviar.")

        # A versão aceita vem do banco; o valor antigo da variável não é lido, nem conferido.
        avisos = [AVISO_TERMOS_VERSAO] if str(ambiente.get("TERMOS_VERSAO") or "").strip() else []
        bucket = str(ambiente.get("SUPABASE_BUCKET") or BUCKET_PADRAO).strip()
        if not _BUCKET.fullmatch(bucket):
            problemas.append("SUPABASE_BUCKET inválido.")
            bucket = BUCKET_PADRAO

        return cls(
            database_url=database_url, supabase_url=supabase_url, supabase_key=supabase_key, bucket=bucket,
            origens=origens, permitir_localhost=permitir_localhost,
            limite_por_hora=inteiro("LIMITE_POR_HORA", 5, 1), limite_por_dia=inteiro("LIMITE_POR_DIA", 10, 1),
            teto_diario=inteiro("TETO_DIARIO", 100, 1), proxies_confiaveis=inteiro("PROXIES_CONFIAVEIS", 2, 0, 5),
            prazo_da_triagem=inteiro("PRAZO_DA_TRIAGEM", 30, 1, PRAZO_MAXIMO_DA_TRIAGEM),
            problemas=tuple(problemas), avisos=tuple(avisos),
        )
