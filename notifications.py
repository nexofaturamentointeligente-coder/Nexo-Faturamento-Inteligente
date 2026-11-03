"""
NEXO - Faturamento Inteligente | Notificações (sininho da navbar)
=================================================================
Helpers para CRIAR notificações dirigidas a usuários. Seguem a disciplina de
transação do projeto: apenas fazem `session.add(...)` — quem chama é responsável
pelo commit (normalmente já há um commit logo após o gatilho).

Gatilhos atuais:
  - cliente envia relatório (upload)      -> notifica ADMIN(s)
  - admin homologa/processa o ETL         -> notifica CLIENTE(s) da empresa
  - admin publica a devolutiva            -> notifica CLIENTE(s) da empresa
  - nova resposta em chamado de suporte   -> notifica a contraparte
"""

import logging

from flask import has_app_context
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from extensions import db, socketio
from horario import horario_brasilia, iso_utc
from models import Empresa, Notificacao, Usuario
from preferencias_usuario import ids_com_aviso_desligado
from session_security import (
    condicoes_empresa_acessivel, hashes_sessoes_ativas, impressao_sessao, usuario_pode_acessar,
)
from realtime import sala_da_sessao

logger = logging.getLogger(__name__)


def classificar_notificacao(texto: str) -> dict:
    """
    Classifica uma notificação em um TIPO visual de badge, a partir do texto.
    Fonte única de verdade usada tanto no render do servidor (Jinja) quanto no
    payload do Socket.IO (JS) — assim a badge é idêntica nos dois caminhos.

      resolvido -> verde   (chamado encerrado)
      analise   -> roxo    (nova análise / relatório disponível)
      aberto    -> vermelho/rosa (ação pendente: novo chamado, resposta, upload)
    """
    t = (texto or "").lower()
    # Aviso aos admins de e-mail recusado pelo provedor (emails.py): pendência
    # que exige ação, mesmo quando o tipo do e-mail fala em "resolvido" ou o
    # motivo em "indisponível".
    if t.startswith("falha no envio de e-mail"):
        return {"tipo": "aberto", "rotulo": "Aberto"}
    # Avisos que não são de chamado: "Aberto" se leria como dívida ou como chamado em aberto.
    if t.startswith("seu contrato com a nexo vence"):
        return {"tipo": "aberto", "rotulo": "Contrato"}
    if t.startswith(("a caixa que recebe a cópia", "encontramos uma diferença")):
        return {"tipo": "aberto", "rotulo": "Aviso"}
    if "resolvid" in t:
        return {"tipo": "resolvido", "rotulo": "Resolvido"}
    if ("dispon" in t) or ("relatório estrat" in t) or ("relatorio estrat" in t) or ("processamos" in t):
        return {"tipo": "analise", "rotulo": "Nova Análise"}
    return {"tipo": "aberto", "rotulo": "Aberto"}


def _emitir_tempo_real(id_usuario: int, n: Notificacao) -> None:
    """Enfileira payload sem ORM; só o COMMIT externo autoriza sua entrega.

    O horário é o data_criacao gravado (preenchido no flush), formatado no
    horário de Brasília pela mesma função do template: o item em tempo real
    e o da página recarregada mostram o mesmo texto. "instante" leva o ISO
    8601 com fuso; o navegador não converte horários.
    """
    badge = classificar_notificacao(n.texto)
    payload = {
        "id": n.id_notificacao, "texto": n.texto,
        "link": n.link_destino or "",
        "hora": horario_brasilia(n.data_criacao, "curto"),
        "hora_completa": horario_brasilia(n.data_criacao, rotulo=True),
        "instante": iso_utc(n.data_criacao),
        "badge": badge["tipo"], "badge_rotulo": badge["rotulo"],
    }
    db.session.info.setdefault("nexo_notificacoes_pendentes", []).append((id_usuario, payload))


def _entregar_tempo_real(bind, id_usuario, payload):
    # A transação que criou a notificação já terminou. A sessão independente
    # evita consultar uma sessão committed ou bloquear o próprio chamador.
    # FOR SHARE mantém credencial e geração até enfileirar o pacote. O UPDATE
    # de senha, a exclusão da empresa ou o logout (FOR UPDATE no usuário) só
    # confirmam depois, ou já são vistos aqui. A leitura da empresa é MVCC,
    # na mesma Session e sem lock invertido. SEC-001: só as sessões ainda
    # registradas e não revogadas são destinatárias (uma sala por sessão).
    with Session(bind=bind) as consulta:
        usuario = consulta.scalar(select(Usuario).where(
            Usuario.id_usuario == id_usuario,
        ).with_for_update(read=True))
        if (not usuario_pode_acessar(usuario, consulta=consulta)
                or (usuario.role == "CLIENTE" and usuario.primeiro_acesso)):
            return
        impressao = impressao_sessao(usuario)
        for hash_sessao in hashes_sessoes_ativas(id_usuario, consulta=consulta):
            socketio.emit(
                "nova_notificacao", payload,
                room=sala_da_sessao(hash_sessao, impressao), namespace="/",
            )


def _apos_commit_notificacoes(sessao):
    # after_commit ainda retém a conexão original. Apenas marcar o COMMIT;
    # a entrega precisa esperar after_transaction_end liberar essa conexão.
    if not sessao.in_nested_transaction():
        sessao.info["nexo_notificacoes_commit_confirmado"] = True


def _despachar_notificacoes(sessao, pendentes):
    if not pendentes:
        return
    if not has_app_context():
        logger.warning("Notificação em tempo real sem contexto de aplicação; entrega descartada.")
        return
    bind = sessao.get_bind()
    for id_usuario, payload in pendentes:
        try:
            _entregar_tempo_real(bind, id_usuario, payload)
        except Exception as erro:  # negócio já confirmado; não anunciar falha dele
            logger.warning("Entrega de notificação indisponível (%s).", type(erro).__name__)


def _inicio_transacao_notificacoes(sessao, transacao):
    if transacao.nested:
        # SAVEPOINT reverte só os payloads criados desde sua abertura.
        sessao.info.setdefault("nexo_notificacoes_checkpoints", {})[id(transacao)] = len(
            sessao.info.get("nexo_notificacoes_pendentes", []),
        )


def _apos_rollback_notificacoes(sessao):
    transacao = sessao.get_nested_transaction()
    if transacao is None:
        sessao.info.pop("nexo_notificacoes_pendentes", None)
        sessao.info.pop("nexo_notificacoes_checkpoints", None)
        sessao.info.pop("nexo_notificacoes_commit_confirmado", None)
        return
    inicio = sessao.info.get("nexo_notificacoes_checkpoints", {}).pop(id(transacao), 0)
    pendentes = sessao.info.get("nexo_notificacoes_pendentes", [])
    del pendentes[inicio:]


def _fim_transacao_notificacoes(sessao, transacao):
    if transacao.nested:
        sessao.info.get("nexo_notificacoes_checkpoints", {}).pop(id(transacao), None)
    elif transacao.parent is None:
        # A conexão original já foi devolvida ao pool. Abrir a sessão de
        # entrega aqui funciona também com pool_size=1 / max_overflow=0.
        # Session.close sem COMMIT apenas descarta os payloads pendentes.
        confirmado = sessao.info.pop("nexo_notificacoes_commit_confirmado", False)
        pendentes = sessao.info.pop("nexo_notificacoes_pendentes", [])
        sessao.info.pop("nexo_notificacoes_checkpoints", None)
        if confirmado:
            _despachar_notificacoes(sessao, pendentes)


# Importar o módulo repetidamente não registra callbacks adicionais.
if not event.contains(Session, "after_commit", _apos_commit_notificacoes):
    event.listen(Session, "after_commit", _apos_commit_notificacoes)
if not event.contains(Session, "after_rollback", _apos_rollback_notificacoes):
    event.listen(Session, "after_rollback", _apos_rollback_notificacoes)
if not event.contains(Session, "after_transaction_create", _inicio_transacao_notificacoes):
    event.listen(Session, "after_transaction_create", _inicio_transacao_notificacoes)
if not event.contains(Session, "after_transaction_end", _fim_transacao_notificacoes):
    event.listen(Session, "after_transaction_end", _fim_transacao_notificacoes)


def criar_notificacao(id_usuario: int, texto: str, link_destino: str | None = None) -> Notificacao:
    """
    Cria UMA notificação e agenda o evento para depois do commit externo.
    O flush garante o id_notificacao para o payload do socket.
    """
    n = Notificacao(id_usuario=id_usuario, texto=texto, link_destino=link_destino)
    db.session.add(n)
    db.session.flush()  # garante o PK para o payload
    _emitir_tempo_real(id_usuario, n)
    return n


def notificar_admins(texto: str, link_destino: str | None = None) -> int:
    """Notifica TODOS os usuários ADMIN ativos. Retorna a quantidade gerada."""
    admins = db.session.execute(
        select(Usuario.id_usuario).where(Usuario.role == "ADMIN", Usuario.ativo.is_(True))
    ).scalars().all()
    for uid in admins:
        criar_notificacao(uid, texto, link_destino)
    return len(admins)


def notificar_clientes_empresa(id_empresa: int, texto: str, link_destino: str | None = None, *,
                               aviso: str | None = None) -> int:
    """Notifica os CLIENTE ativos de empresa fora da lixeira e não cancelada.

    `aviso` nomeia a preferência que governa esta notificação (ex.: "alerta_chamado"):
    quem a desligou em Configurações fica de fora."""
    clientes = db.session.execute(
        select(Usuario.id_usuario).where(
            Usuario.role == "CLIENTE",
            Usuario.id_empresa == id_empresa,
            Usuario.ativo.is_(True),
            select(Empresa.id_empresa).where(
                Empresa.id_empresa == Usuario.id_empresa,
                *condicoes_empresa_acessivel(),
            ).exists(),
        )
    ).scalars().all()
    if aviso is not None:
        desligados = ids_com_aviso_desligado(aviso, clientes)
        clientes = [uid for uid in clientes if uid not in desligados]
    for uid in clientes:
        criar_notificacao(uid, texto, link_destino)
    return len(clientes)


def contexto_sininho(usuario) -> dict:
    """
    Dados do sininho para o usuário autenticado: contagem de não-lidas e as
    5 notificações mais recentes. Usado pelo context processor (toda página).
    """
    vazio = {"notif_nao_lidas": 0, "notif_recentes": []}
    if usuario is None:
        return vazio

    try:
        # is_authenticated pode consultar o banco (instância expirada após um
        # rollback): fica dentro da proteção, como as consultas abaixo.
        if not getattr(usuario, "is_authenticated", False):
            return vazio
        nao_lidas = db.session.execute(
            select(db.func.count(Notificacao.id_notificacao)).where(
                Notificacao.id_usuario == usuario.id_usuario,
                Notificacao.lida.is_(False),
            )
        ).scalar() or 0

        recentes = db.session.execute(
            select(Notificacao)
            .where(Notificacao.id_usuario == usuario.id_usuario)
            .order_by(Notificacao.data_criacao.desc(), Notificacao.id_notificacao.desc())
            .limit(5)
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        # Roda em toda página (context processor), inclusive nas páginas de erro:
        # com o banco indisponível ou a transação abortada, a página sai sem o
        # sininho em vez de virar a página branca do servidor.
        logger.warning("Sininho indisponível (%s): página renderizada sem notificações.", type(exc).__name__)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return vazio

    return {"notif_nao_lidas": int(nao_lidas), "notif_recentes": recentes}
