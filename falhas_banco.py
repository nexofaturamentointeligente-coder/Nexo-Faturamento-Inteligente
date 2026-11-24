"""
NEXO - Faturamento Inteligente | Banco indisponível
====================================================
O que conta como queda do banco e a resposta 503 que não consulta o banco.

Usado pelo guarda de sessão (app._guard_sessao), pelo login e pelo errorhandler
global. Banco fora do ar não é sessão inválida nem defeito do sistema: a
requisição é recusada com 503 e uma mensagem amigável, sem apagar o cookie de
sessão, sem SQL na tela ou no log, e o usuário tenta de novo em instantes.
"""

import logging

from flask import current_app, jsonify, make_response, request
from sqlalchemy.exc import DBAPIError, DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolEsgotado

logger = logging.getLogger(__name__)

TITULO = "Sistema indisponível"
MENSAGEM = "Sistema indisponível no momento. Tente novamente em instantes."
# Sugestão de espera ao navegador e aos clientes de API (cabeçalho Retry-After).
RETRY_AFTER_S = 30
# Rotas chamadas por fetch, que recebem JSON: as APIs e o polling do sininho.
ROTAS_JSON = ("/api/", "/notificacoes/novas")
# Classes de SQLSTATE do PostgreSQL que indicam indisponibilidade passageira:
#   08 conexão; 53 recursos insuficientes (conexões demais, memória, disco);
#   57 intervenção do operador (tempo da consulta esgotado, desligamento, "cannot
#   connect now"); 40 conflito de transação (deadlock, serialização), que se
#   resolve repetindo.
CLASSES_PASSAGEIRAS = frozenset({"08", "53", "57", "40"})
# O errorhandler global recebe estas exceções; o que não for queda volta ao 500.
# DBAPIError inteira: o dialeto marca connection_invalidated em qualquer erro do
# driver (ex.: DatabaseError "SSL SYSCALL error: EOF detected" com o pooler).
EXCECOES_TRATADAS = (DBAPIError, PoolEsgotado, DisconnectionError)
# Causa provável da queda, para o log: categoria fixa, nunca o texto do driver
# (que traz host, usuário e SQL). Ordem: o primeiro trecho encontrado vale.
_MOTIVOS = (
    ("password authentication failed", "autenticacao_recusada"),
    ("does not exist", "banco_ou_papel_inexistente"),
    ("timeout expired", "prazo_de_conexao_esgotado"),
    ("could not translate host name", "host_nao_resolvido"),
    ("connection refused", "conexao_recusada"),
    ("too many", "conexoes_esgotadas"),
    ("max clients", "conexoes_esgotadas"),
    ("ssl", "ssl"),
    ("server closed the connection", "conexao_encerrada_pelo_servidor"),
    ("terminating connection", "conexao_encerrada_pelo_servidor"),
    ("connection already closed", "conexao_ja_fechada"),
    ("could not receive data", "rede"),
    ("could not send data", "rede"),
    ("no route to host", "rede"),
    ("network is unreachable", "rede"),
)


def eh_banco_indisponivel(erro) -> bool:
    """True para queda, conexão perdida, recurso esgotado ou pool cheio.

    Erro de programação (SQL errado, tabela inexistente, integridade) é False:
    ele segue para o 500, com o traceback no log.
    """
    if isinstance(erro, (PoolEsgotado, DisconnectionError)):
        return True
    if not isinstance(erro, DBAPIError):
        return False
    if erro.connection_invalidated:
        return True
    if not isinstance(erro, (OperationalError, InterfaceError)):
        return False
    original = erro.orig
    # Só o driver do PostgreSQL (psycopg2) traz pgcode. Sem o atributo (ex.: sqlite3
    # com tabela inexistente), é erro local, não queda.
    if not hasattr(original, "pgcode"):
        return False
    codigo = original.pgcode
    # Sem código: a falha nem chegou a uma resposta do servidor (conexão recusada,
    # prazo de conexão esgotado, conexão derrubada). Com código: só as classes
    # passageiras.
    return codigo is None or str(codigo)[:2] in CLASSES_PASSAGEIRAS


def descrever(erro) -> str:
    """Tipo, SQLSTATE e causa provável para o log. Nunca a mensagem do driver, que pode
    trazer SQL, host ou usuário: só uma categoria fixa (_MOTIVOS) tirada dela."""
    original = getattr(erro, "orig", None)
    partes = [type(erro).__name__]
    codigo = getattr(original, "pgcode", None)
    if codigo:
        partes.append(f"pgcode={codigo}")
    texto = str(original if original is not None else "").lower()
    motivo = next((nome for trecho, nome in _MOTIVOS if trecho in texto), None)
    if motivo:
        partes.append(f"motivo={motivo}")
    return ", ".join(partes)


def rollback_seguro() -> None:
    """Desfaz a transação da requisição sem deixar uma segunda falha virar 500.

    Com a conexão já perdida, o próprio rollback pode falhar; a resposta de
    indisponibilidade (503) precisa sair mesmo assim. O log leva só o tipo e a
    causa provável (descrever), nunca a mensagem do driver.
    """
    from extensions import db
    try:
        db.session.rollback()
    except Exception as erro:  # noqa: BLE001 - qualquer falha aqui não pode derrubar o 503
        logger.warning("Rollback indisponível (%s).", descrever(erro))


def rota_json() -> bool:
    return request.path.startswith(ROTAS_JSON)


def resposta_indisponivel(referencia=None, mensagem=MENSAGEM):
    """503 com a mensagem amigável (JSON nas rotas de fetch), sem tocar no banco.

    A página é renderizada sem os context processors (sininho, menu, NexoBot): com
    o banco fora, cada um tentaria uma conexão e somaria um prazo de conexão à
    resposta. `referencia` é o código para o suporte, o mesmo gravado no log.
    """
    if rota_json():
        extras = {"meta": {"correlacao": referencia}} if referencia else {}
        corpo = jsonify(erro=mensagem, resposta=mensagem, fonte="local", acao=None, destino=None, **extras)
    else:
        corpo = current_app.jinja_env.get_template("erro_simples.html").render(
            titulo=TITULO, mensagem=mensagem, codigo=503, painel_url=None, referencia=referencia,
        )
    resposta = make_response(corpo, 503)
    resposta.headers["Retry-After"] = str(RETRY_AFTER_S)
    resposta.headers["Cache-Control"] = "no-store"
    return resposta
