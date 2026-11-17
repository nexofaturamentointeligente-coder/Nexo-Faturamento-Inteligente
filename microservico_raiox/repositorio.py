"""Gravação do lead na tabela de leads da aplicação principal (lead_avaliacao), pelo DATABASE_URL.

Operações curtas, cada uma na sua transação, com prazo de 10 s no servidor e keepalive de TCP no cliente (uma
conexão que some sem aviso não prende a thread), com SSL exigido quando a URL não diz outra coisa. O esquema
é o da aplicação principal, com as migrações f2b6d8a4c1e9 e c8e2a4f6b0d1 aplicadas: o lead nasce com origem
"site", status "pendente", o interesse, a situação dos arquivos, o JSON dos caminhos no Storage e o resumo do
aceite dos termos (termos_aceitos_em e termos_versao, colunas próprias, que o reenvio do consultor não troca). O
e-mail e o nome da loja são obrigatórios na tabela; os dados de avaliação ficam nulos até o consultor processar.
A prova do aceite é uma linha por documento em aceite_documento_lead: a versão exata (documento_legal), o
instante (o now() do banco, o mesmo do lead) e o IP do visitante.

O teto diário é conferido de novo dentro da transação do INSERT, sob uma trava consultiva: envios simultâneos
não passam juntos do teto. Na mesma transação, depois do teto, documento_legal é travada em SHARE MODE antes de
ler as versões vigentes: uma publicação em andamento termina antes da leitura (e a versão nova é vista), e a
que chega depois espera o COMMIT do lead. Assim o aceite gravado é sempre da versão vigente no instante do
COMMIT; se o visitante leu outra, nada é gravado. A trava só disputa com a publicação de um documento (a
leitura e o aceite do cliente do portal não esperam por ela) e dura o INSERT do lead e dos aceites.
"""

import re
from typing import NamedTuple
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import psycopg2
import psycopg2.extensions

APLICACAO = "nexo_microservico_raiox"
# Chave fixa da trava consultiva do teto diário (só este serviço a usa).
TRAVA_DO_TETO = 7_204_513_881
# Os dois documentos que o visitante aceita na landing (o tipo em documento_legal), com a sigla que vai para
# termos_versao do lead e o nome que vai para o log.
SIGLAS_DOS_DOCUMENTOS = {"TERMOS_USO": "TU", "POLITICA_PRIVACIDADE": "PP"}
NOMES_DOS_DOCUMENTOS = {"TERMOS_USO": "Termos de Uso", "POLITICA_PRIVACIDADE": "Política de Privacidade"}
# O CHECK de lead_avaliacao.termos_versao.
TERMOS_VERSAO_MAXIMO = 40
# Parâmetros que o Supabase e o Prisma acrescentam à URL e que a libpq não conhece (a aplicação principal
# também os tira, em config.py).
_PARAMETROS_FORA_DA_LIBPQ = {"pgbouncer", "connection_limit", "pool_timeout", "schema"}
_ESQUEMAS = {"postgres", "postgresql", "postgresql+psycopg2", "postgres+psycopg2"}
# A URL como a aplicação principal a lê, pelo SQLAlchemy (sqlalchemy/engine/url.py, _parse_url): o usuário até
# o ":" ou a "/", a senha até o "@" (com "/" e "%" sem codificar), o host (ou o IPv6 entre colchetes), a porta,
# o banco e a consulta.
_URL_DO_BANCO = re.compile(r"""
    (?P<esquema>[\w+]+)://
    (?:(?P<usuario>[^:/]*)(?::(?P<senha>[^@]*))?@)?
    (?:(?:\[(?P<ipv6>[^/?]+)\]|(?P<host>[^/:?]+))?(?::(?P<porta>[^/?]*))?)?
    (?:/(?P<banco>[^?]*))?
    (?:\?(?P<consulta>.*))?
""", re.X)
_COLUNAS = ("id", "nome_contato", "email", "whatsapp", "nome_loja", "interesse", "status_arquivo",
            "arquivos_storage")
_INSERIR = (
    "INSERT INTO lead_avaliacao (id, nome_contato, email, whatsapp, nome_loja, status, origem, interesse, "
    "status_arquivo, arquivos_storage, termos_aceitos_em, termos_versao, criado_em, atualizado_em) "
    "VALUES (%(id)s::uuid, %(nome_contato)s, %(email)s, %(whatsapp)s, %(nome_loja)s, 'pendente', 'site', "
    "%(interesse)s, %(status_arquivo)s, %(arquivos_storage)s, now(), %(termos_versao)s, now(), now())"
)
_CONTAR_DESDE = "SELECT count(*) FROM lead_avaliacao WHERE interesse IS NOT NULL AND criado_em >= %s"
_EXISTE = "SELECT count(*) FROM lead_avaliacao WHERE id = %s::uuid"
# A vigente de cada documento é a de maior `versao` do tipo (documento_legal não tem marca de "ativa").
_VIGENTES = ("SELECT DISTINCT ON (tipo) tipo, id_documento, versao_maior, versao_menor FROM documento_legal "
             "WHERE tipo IN (" + ", ".join(f"'{tipo}'" for tipo in SIGLAS_DOS_DOCUMENTOS) + ") "
             "ORDER BY tipo, versao DESC")
_TRAVAR_DOCUMENTOS = "LOCK TABLE documento_legal IN SHARE MODE"
_INSERIR_ACEITE = ("INSERT INTO aceite_documento_lead (id_lead, id_documento, aceito_em, ip) "
                   "VALUES (%(id)s::uuid, %(id_documento)s, now(), %(ip)s)")


class ErroDeBanco(RuntimeError):
    """O banco recusou a operação ou não respondeu (a mensagem nunca traz a URL nem a senha)."""


class TetoDoDiaAtingido(RuntimeError):
    """O teto diário do serviço foi atingido na hora de gravar."""


class TermosIndisponiveis(RuntimeError):
    """Falta a versão publicada de algum dos documentos: não há o que aceitar."""


class TermosDesatualizados(RuntimeError):
    """O visitante leu uma versão que não é a vigente (publicada outra durante a leitura, ou id forjado)."""


class Vigente(NamedTuple):
    """A versão vigente de um documento: o id em documento_legal e o rótulo exibido (v1.0, v1.1, v2.0)."""
    id_documento: int
    rotulo: str


def conferir_termos(vigentes: dict, lidos: dict) -> None:
    """TermosIndisponiveis se falta a versão publicada de algum dos dois documentos; TermosDesatualizados se o
    visitante leu outra (`lidos`, {tipo: id}, não é o id vigente de cada tipo)."""
    if any(tipo not in vigentes for tipo in SIGLAS_DOS_DOCUMENTOS):
        raise TermosIndisponiveis()
    if any(lidos.get(tipo) != vigentes[tipo].id_documento for tipo in SIGLAS_DOS_DOCUMENTOS):
        raise TermosDesatualizados()


def versao_dos_termos(vigentes: dict) -> str:
    """O resumo das versões aceitas que vai para termos_versao do lead, "TU-v1.0 PP-v1.0", cortado no CHECK da
    coluna (40 caracteres; só um rótulo com números enormes chegaria lá). A prova é aceite_documento_lead."""
    texto = " ".join(f"{sigla}-{vigentes[tipo].rotulo}" for tipo, sigla in SIGLAS_DOS_DOCUMENTOS.items())
    return texto[:TERMOS_VERSAO_MAXIMO]


def _ler_vigentes(cursor) -> dict:
    cursor.execute(_VIGENTES)
    return {tipo: Vigente(id_documento, f"v{maior}.{menor}")
            for tipo, id_documento, maior, menor in cursor.fetchall()}


def parametros_de_conexao(database_url: str, timeout: int = 10) -> dict:
    """Os parâmetros do psycopg2.connect, tirados da URL como a aplicação principal a lê: usuário, senha e banco
    decodificados; a consulta por parse_qsl ("+" e "%20" viram espaço, e o parâmetro vazio sai), por cima dos
    demais, como no SQLAlchemy, e sem os parâmetros que a libpq não conhece. A libpq confere os nomes; a porta
    tem de ser um número. Depois, as opções deste serviço que a URL não fixou."""
    # Como a _limpar_url_pg da aplicação principal (config.py), antes do SQLAlchemy: o urlsplit tira tabulação e
    # quebra de linha de qualquer ponto, e a consulta é refeita pelo urlencode (um "@" num valor vira %40 e não
    # corta a URL noutro lugar).
    try:
        partes = urlsplit(re.sub(r"[\t\r\n]", "", database_url).strip())
    except ValueError:
        raise ErroDeBanco("DATABASE_URL inválida") from None
    consulta_refeita = urlencode([(nome, valor) for nome, valor in parse_qsl(partes.query, keep_blank_values=True)
                                  if nome.lower() not in _PARAMETROS_FORA_DA_LIBPQ])
    achado = _URL_DO_BANCO.fullmatch(urlunsplit((partes.scheme, partes.netloc, partes.path, consulta_refeita,
                                                 partes.fragment)))
    if achado is None or achado["esquema"].lower() not in _ESQUEMAS:
        raise ErroDeBanco("DATABASE_URL inválida")
    parametros = {chave: unquote(achado[grupo]) for chave, grupo in
                  (("user", "usuario"), ("password", "senha"), ("dbname", "banco")) if achado[grupo]}
    if achado["ipv6"] or achado["host"]:
        parametros["host"] = achado["ipv6"] or achado["host"]
    if achado["porta"]:
        # A porta lida como o SQLAlchemy a lê (int()), conferida na faixa de uma porta TCP.
        try:
            porta = int(achado["porta"])
        except ValueError:
            raise ErroDeBanco("DATABASE_URL inválida") from None
        if not 0 < porta < 65536:
            raise ErroDeBanco("DATABASE_URL inválida")
        parametros["port"] = str(porta)
    consulta = [(nome, valor) for nome, valor in parse_qsl(achado["consulta"] or "")
                if nome.lower() not in _PARAMETROS_FORA_DA_LIBPQ]
    # Host e porta na consulta (o vários hosts do dialeto do SQLAlchemy) e parâmetro repetido não são aceitos: a
    # leitura deles na aplicação principal é outra, e o serviço poderia conectar noutro lugar.
    nomes = [nome.lower() for nome, _valor in consulta]
    if len(set(nomes)) != len(nomes) or set(nomes) & {"host", "hostaddr", "port"}:
        raise ErroDeBanco("DATABASE_URL inválida")
    parametros.update(consulta)
    try:
        parametros = psycopg2.extensions.parse_dsn(psycopg2.extensions.make_dsn(**parametros))
    except (psycopg2.Error, ValueError, TypeError):
        raise ErroDeBanco("DATABASE_URL inválida") from None
    parametros.setdefault("sslmode", "require")
    parametros.setdefault("connect_timeout", str(timeout))
    parametros.setdefault("keepalives", "1")
    parametros.setdefault("keepalives_idle", "10")
    parametros.setdefault("keepalives_interval", "5")
    parametros.setdefault("keepalives_count", "3")
    parametros.setdefault("tcp_user_timeout", "20000")
    parametros["application_name"] = APLICACAO
    return parametros


class RepositorioDeLeads:
    def __init__(self, database_url: str, timeout: int = 10):
        self._parametros = parametros_de_conexao(database_url, timeout)

    def __repr__(self) -> str:
        return "RepositorioDeLeads()"

    def _transacao(self, trabalho):
        try:
            conexao = psycopg2.connect(**self._parametros)
        except psycopg2.Error as erro:
            raise ErroDeBanco(f"o banco não respondeu ({type(erro).__name__})") from None
        try:
            with conexao, conexao.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                return trabalho(cursor)
        except psycopg2.Error as erro:
            raise ErroDeBanco(f"o banco recusou a operação ({type(erro).__name__})") from None
        finally:
            conexao.close()

    def leads_desde(self, instante) -> int:
        """Leads gravados pelo microsserviço desde o instante (com fuso)."""
        def contar(cursor):
            cursor.execute(_CONTAR_DESDE, (instante,))
            return cursor.fetchone()[0]
        return self._transacao(contar)

    def primeira_conferencia(self, desde) -> tuple:
        """Antes do trabalho pesado, numa transação só e sem trava: (leads do microsserviço desde `desde`, versões
        vigentes {tipo: Vigente}). Só adianta a recusa; a conferência que vale é a da gravação."""
        def conferir(cursor):
            cursor.execute(_CONTAR_DESDE, (desde,))
            leads = cursor.fetchone()[0]
            return leads, _ler_vigentes(cursor)
        return self._transacao(conferir)

    def gravar(self, lead: dict, teto: int, desde, lidos: dict, ip=None) -> None:
        """Grava o lead e um aceite por documento, numa transação. TetoDoDiaAtingido se o teto do dia (leads do
        microsserviço desde `desde`) já foi atingido; TermosIndisponiveis ou TermosDesatualizados se as versões que
        o visitante leu (`lidos`, {tipo: id}) não são as vigentes lidas aqui, com documento_legal travada: em
        qualquer recusa, nada é gravado. `ip` é o do visitante, já normalizado (validacao.ip_do_visitante), ou None."""
        valores = {coluna: lead.get(coluna) for coluna in _COLUNAS}
        valores["id"] = str(lead["id"])

        def inserir(cursor):
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (TRAVA_DO_TETO,))
            cursor.execute(_CONTAR_DESDE, (desde,))
            if cursor.fetchone()[0] >= teto:
                raise TetoDoDiaAtingido()
            cursor.execute(_TRAVAR_DOCUMENTOS)
            vigentes = _ler_vigentes(cursor)
            conferir_termos(vigentes, lidos)
            cursor.execute(_INSERIR, {**valores, "termos_versao": versao_dos_termos(vigentes)})
            for tipo in SIGLAS_DOS_DOCUMENTOS:
                cursor.execute(_INSERIR_ACEITE, {"id": valores["id"], "id_documento": vigentes[tipo].id_documento,
                                                 "ip": ip})
        self._transacao(inserir)

    def existe(self, lead_id) -> bool:
        """Se o lead ficou gravado (para quando o COMMIT foi aplicado e a resposta se perdeu)."""
        def conferir(cursor):
            cursor.execute(_EXISTE, (str(lead_id),))
            return cursor.fetchone()[0] == 1
        return self._transacao(conferir)

    def conferir_tabela(self) -> dict:
        """Na subida: conecta, lê as tabelas pelas colunas que o serviço usa (lead_avaliacao e aceite_documento_lead
        sem nenhuma linha; de documento_legal, as versões vigentes, que devolve) e trava documento_legal como a
        gravação trava (o papel do banco precisa poder). ErroDeBanco se algo falha."""
        def conferir(cursor):
            cursor.execute("SELECT interesse, status_arquivo, arquivos_storage, termos_aceitos_em, termos_versao "
                           "FROM lead_avaliacao LIMIT 0")
            cursor.execute("SELECT id_lead, id_documento, aceito_em, ip FROM aceite_documento_lead LIMIT 0")
            cursor.execute(_TRAVAR_DOCUMENTOS)
            return _ler_vigentes(cursor)
        return self._transacao(conferir)
