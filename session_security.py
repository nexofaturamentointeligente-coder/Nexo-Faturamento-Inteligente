"""SEC-003/004: autorização persistida e sessões ligadas à credencial atual.
SEC-001: cada login registra uma sessão revogável no servidor.

Uma sessão só autoriza quando (todos ao mesmo tempo): o usuário pode acessar
(estado administrativo e empresa), o cookie aponta para o usuário, o BOOT_ID
é o deste processo, a impressão da credencial (senha + geração) confere e o
identificador de sessão do cookie corresponde a uma linha de
sessao_autenticada não revogada e não expirada. O logout revoga a linha; a
troca de senha revoga todas as linhas do usuário.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from flask import current_app, session
from sqlalchemy import select
from sqlalchemy.orm import object_session

from contract_policy import condicao_sql_vinculo_contratual
from extensions import db
from horario import agora_utc
from models import MOTIVOS_REVOGACAO_SESSAO, Empresa, SessaoAutenticada, Usuario


CHAVE_CREDENCIAL = "credential_fingerprint"
CHAVE_SESSAO = "session_token"
_CONTEXTO_SESSAO = b"nexo-session-credential-v1\0"
# NEXO-AJ-06.1: CANCELADA bloqueia o CLIENTE como a lixeira. SUSPENSA mantém o
# acesso ao portal; o que ela pode consultar é decidido por contract_policy.
STATUS_CONTA_SEM_ACESSO = "CANCELADA"
# Identificador de sessão: 32 bytes aleatórios em base64 url-safe (43 caracteres).
_TAMANHO_TOKEN_SESSAO = 43
_ALFABETO_TOKEN = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def condicoes_empresa_acessivel(instante=None, *, sessao=None):
    """Critério único da empresa que libera CLIENTE: fora da lixeira, não cancelada
    e, no regime contratual, com contrato vigente cuja suspensão não expirou.

    O prazo é comparado ao relógio da política no próprio SQL: a expiração
    bloqueia login, sessão, tokens, Socket.IO e notificações antes mesmo de a
    reconciliação gravar o cancelamento. `sessao` é a Session que executará a
    consulta (entregas pós-commit usam Session própria, não db.session).
    """
    return (
        Empresa.deletado_em.is_(None),
        Empresa.status_conta != STATUS_CONTA_SEM_ACESSO,
        condicao_sql_vinculo_contratual(instante, sessao=sessao),
    )


def _id_valido(valor):
    if isinstance(valor, str):
        if not valor or len(valor) > 10 or not valor.isascii() or not valor.isdecimal():
            return None
        valor = int(valor)
    if type(valor) is not int or not 0 < valor <= 2147483647:
        return None
    return valor


def id_sessao(dados=None):
    dados = session if dados is None else dados
    return _id_valido(dados.get("_user_id"))


def usuario_pode_acessar(usuario, *, consulta=None) -> bool:
    """Estado administrativo, lixeira e cancelamento da empresa, sempre lidos do banco.

    Não usa o relacionamento ORM potencialmente desatualizado nem outra
    conexão: notificações/varreduras passam sua própria Session. O SELECT da
    empresa é MVCC, sem lock: os leitores Socket.IO já mantêm FOR SHARE no
    usuário, e a exclusão atualiza sua geração na mesma transação da empresa.
    Assim, a exclusão só confirma depois da emissão autorizada anterior, sem
    inverter a ordem Empresa -> Usuario dos locks de escrita.
    """
    if (usuario is None or not usuario.ativo
            or usuario.role not in ("ADMIN", "CLIENTE")):
        return False
    if usuario.role == "ADMIN":
        return usuario.id_empresa is None
    if usuario.id_empresa is None:
        return False
    consulta = object_session(usuario) if consulta is None else consulta
    if consulta is None:
        return False
    with consulta.no_autoflush:
        return consulta.scalar(select(Empresa.id_empresa).where(
            Empresa.id_empresa == usuario.id_empresa,
            *condicoes_empresa_acessivel(sessao=consulta),
        )) is not None


def vincular_geracao_autorizacao(mensagem: bytes, geracao: int) -> bytes:
    """Geração zero preserva a SEC-003; gerações posteriores nunca a reutilizam."""
    if type(geracao) is not int or geracao < 0:
        raise ValueError("Geração de autorização inválida.")
    if geracao == 0:
        return mensagem
    return (b"nexo-authorization-generation-v1\0" + str(geracao).encode("ascii")
            + b"\0" + mensagem)


def impressao_credencial_sessao(
        id_usuario: int, senha_hash: str, geracao_autorizacao: int = 0) -> str:
    """HMAC padrão, com contexto distinto dos tokens de ativação/recuperação."""
    chave = current_app.config["SECRET_KEY"]
    if isinstance(chave, str):
        chave = chave.encode("utf-8")
    mensagem = vincular_geracao_autorizacao(
        _CONTEXTO_SESSAO + f"{id_usuario}\0{senha_hash}".encode("utf-8"),
        geracao_autorizacao,
    )
    return hmac.new(chave, mensagem, hashlib.sha256).hexdigest()


def impressao_sessao(usuario) -> str:
    return impressao_credencial_sessao(
        usuario.id_usuario, usuario.senha_hash, usuario.geracao_autorizacao,
    )


def mesma_impressao(esperada, recebida) -> bool:
    # compare_digest(str, str) não aceita Unicode não ASCII.
    for valor in (esperada, recebida):
        if (not isinstance(valor, str) or len(valor) != 64
                or any(c not in "0123456789abcdef" for c in valor)):
            return False
    return hmac.compare_digest(esperada, recebida)


# --- SEC-001: sessões registradas no servidor -------------------------------

def hash_token_sessao(token) -> str | None:
    """SHA-256 (hex) do identificador do cookie; None para valor malformado.

    O banco guarda só o hash: ler a tabela não entrega um identificador
    utilizável, e o cookie assinado continua sendo o único portador.
    """
    if (not isinstance(token, str) or len(token) != _TAMANHO_TOKEN_SESSAO
            or any(c not in _ALFABETO_TOKEN for c in token)):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _hash_valido(valor) -> bool:
    return (isinstance(valor, str) and len(valor) == 64
            and all(c in "0123456789abcdef" for c in valor))


def duracao_sessao() -> timedelta:
    return current_app.config.get("PERMANENT_SESSION_LIFETIME") or timedelta(hours=8)


def sessao_registrada_valida(id_usuario: int, hash_sessao, *, consulta=None) -> bool:
    """Linha de sessao_autenticada do usuário, não revogada e dentro da validade."""
    if not _hash_valido(hash_sessao):
        return False
    consulta = db.session if consulta is None else consulta
    with consulta.no_autoflush:
        return consulta.scalar(select(SessaoAutenticada.id_sessao).where(
            SessaoAutenticada.identificador_hash == hash_sessao,
            SessaoAutenticada.id_usuario == id_usuario,
            SessaoAutenticada.revogada_em.is_(None),
            SessaoAutenticada.expira_em > agora_utc(),
        )) is not None


def hashes_sessoes_ativas(id_usuario: int, *, consulta=None) -> list[str]:
    """Sessões que ainda autorizam o usuário (destinatárias de eventos privados)."""
    consulta = db.session if consulta is None else consulta
    with consulta.no_autoflush:
        return list(consulta.scalars(select(SessaoAutenticada.identificador_hash).where(
            SessaoAutenticada.id_usuario == id_usuario,
            SessaoAutenticada.revogada_em.is_(None),
            SessaoAutenticada.expira_em > agora_utc(),
        )))


def registrar_sessao(sessao_db, id_usuario: int) -> tuple[str, str]:
    """Insere (SEM commit) a linha da nova sessão; devolve (token, hash).

    O chamador confirma a transação e só então carimba o cookie com
    carimbar_sessao: uma falha de banco propaga SQLAlchemyError e nenhum
    cookie autorizado é emitido (fail-closed). Login e renovação do primeiro
    acesso registram a sessão na mesma transação da operação de negócio.
    """
    token = secrets.token_urlsafe(32)
    hash_sessao = hash_token_sessao(token)
    agora = agora_utc()
    with sessao_db.no_autoflush:
        # Higiene: as linhas já expiradas ou revogadas deste usuário não servem
        # para nada e sairiam só com uma rotina externa.
        sessao_db.execute(SessaoAutenticada.__table__.delete().where(
            SessaoAutenticada.id_usuario == id_usuario,
            (SessaoAutenticada.revogada_em.is_not(None)) | (SessaoAutenticada.expira_em <= agora),
        ))
        sessao_db.add(SessaoAutenticada(
            id_usuario=id_usuario, identificador_hash=hash_sessao,
            criada_em=agora, expira_em=agora + duracao_sessao(),
        ))
        sessao_db.flush()
    return token, hash_sessao


def carimbar_sessao(token: str, impressao: str) -> None:
    """Após o COMMIT: credencial verificada, BOOT_ID e identificador no cookie.

    Login fornece o snapshot da credencial cuja senha foi verificada; nunca
    adotar um hash posterior por refresh do ORM.
    """
    session[CHAVE_CREDENCIAL] = impressao
    session["boot_id"] = current_app.config["BOOT_ID"]
    session[CHAVE_SESSAO] = token


def _revogar(sessao_db, condicoes, motivo: str) -> int:
    """Marca as linhas ativas que satisfazem `condicoes` (flush só no commit).

    Alteração pelo ORM, como as demais escritas de company_security: a
    transação do chamador continua sendo a única que confirma ou reverte, e
    um erro real do banco só aparece no commit, nunca dentro do helper.
    """
    if motivo not in MOTIVOS_REVOGACAO_SESSAO:
        raise ValueError("Motivo de revogação inválido.")
    agora = agora_utc()
    with sessao_db.no_autoflush:
        # Linha vencida já não autoriza nada e sai pela limpeza de registrar_sessao. Marcá-la
        # aqui faria o flush atualizar uma linha que essa limpeza acabou de apagar.
        linhas = sessao_db.scalars(select(SessaoAutenticada).where(
            SessaoAutenticada.revogada_em.is_(None), SessaoAutenticada.expira_em > agora, *condicoes,
        ).execution_options(populate_existing=True)).all()
    for linha in linhas:
        linha.revogada_em = agora
        linha.motivo_revogacao = motivo
    return len(linhas)


def revogar_sessoes_usuario(sessao_db, id_usuario: int, motivo: str, *, exceto_hash=None) -> int:
    """Revoga (sem commit) as sessões ativas do usuário; devolve quantas."""
    condicoes = [SessaoAutenticada.id_usuario == id_usuario]
    if exceto_hash is not None:
        condicoes.append(SessaoAutenticada.identificador_hash != exceto_hash)
    return _revogar(sessao_db, condicoes, motivo)


def revogar_sessao_por_hash(sessao_db, id_usuario: int, hash_sessao, motivo: str) -> int:
    """Revoga (sem commit) uma única sessão do usuário pelo hash do cookie."""
    if not _hash_valido(hash_sessao):
        return 0
    return _revogar(sessao_db, [
        SessaoAutenticada.id_usuario == id_usuario,
        SessaoAutenticada.identificador_hash == hash_sessao,
    ], motivo)


def sessao_valida(usuario, dados=None, *, permitir_primeiro_acesso=True, consulta=None,
                  hash_sessao=None) -> bool:
    """Todas as condições de autorização de um cookie (ou de um snapshot dele).

    `hash_sessao` permite validar um registro de conexão Socket.IO, que guarda
    o hash e não o identificador do cookie.
    """
    dados = session if dados is None else dados
    if (not usuario_pode_acessar(usuario, consulta=consulta)
            or id_sessao(dados) != usuario.id_usuario
            or dados.get("boot_id") != current_app.config["BOOT_ID"]):
        return False
    if (not permitir_primeiro_acesso and usuario.role == "CLIENTE"
            and usuario.primeiro_acesso):
        return False
    if not mesma_impressao(impressao_sessao(usuario), dados.get(CHAVE_CREDENCIAL)):
        return False
    if hash_sessao is None:
        hash_sessao = hash_token_sessao(dados.get(CHAVE_SESSAO))
    return sessao_registrada_valida(usuario.id_usuario, hash_sessao, consulta=consulta)


def carregar_usuario(id_usuario, *, bloquear=False, escrita=False):
    """FOR SHARE serializa envio/evento com senha e geração de autorização no PG.

    `escrita` pede FOR UPDATE: quem revoga uma sessão (logout) espera as
    entregas em andamento terminarem, e as seguintes já veem a revogação.
    """
    id_usuario = _id_valido(id_usuario)
    if id_usuario is None:
        return None
    comando = select(Usuario).where(Usuario.id_usuario == id_usuario)
    if escrita:
        comando = comando.with_for_update()
    elif bloquear:
        comando = comando.with_for_update(read=True)
    comando = comando.execution_options(populate_existing=True)
    with db.session.no_autoflush:
        return db.session.scalar(comando)
