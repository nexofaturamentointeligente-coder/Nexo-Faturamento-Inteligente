"""
NEXO - Blueprint de Notificações (sininho)
===========================================
Rotas compartilhadas por ADMIN e CLIENTE (qualquer usuário autenticado):
  POST /notificacoes/marcar-lidas   → marca todas as não-lidas como lidas.
  POST /notificacoes/<id>/abrir      → marca aquela como lida e redireciona
                                       para o link_destino (ou dashboard).
  GET  /notificacoes/<id>/abrir      → só redireciona (SEC-002: nenhum GET
                                       altera estado; links antigos seguem
                                       funcionando sem marcar como lida).
  GET  /notificacoes/novas?desde=<id> → polling do sininho: JSON com as não
                                       lidas de id maior que `desde` e a
                                       contagem; 401 sem sessão válida.
A leitura/contagem para o sininho é injetada em todas as páginas pelo
context processor (ver app.create_app).
"""

from urllib.parse import urlparse

from flask import Blueprint, jsonify, redirect, url_for, request
from flask_login import login_required, current_user
from sqlalchemy import update

from extensions import db, limiter
from models import Notificacao
from notifications import novas_do_usuario

notificacoes_bp = Blueprint("notificacoes", __name__, url_prefix="/notificacoes")


def _destino_padrao() -> str:
    """Dashboard do perfil do usuário logado (fallback de redirecionamento)."""
    return url_for("admin.dashboard") if current_user.is_admin else url_for("cliente.dashboard")


def _local_ou_padrao(alvo: str) -> str:
    """
    Devolve `alvo` se for um caminho LOCAL seguro; senão, o dashboard padrão.
    Defesa em profundidade contra open redirect (referrer/link_destino externos),
    mesmo que hoje esses valores sejam sempre internos (gerados por url_for).
    """
    if alvo:
        p = urlparse(alvo)
        if not p.scheme and not p.netloc and alvo.startswith("/") and not alvo.startswith(("//", "/\\")):
            return alvo
    return _destino_padrao()


def _chave_limite_sininho() -> str:
    """Limite por usuário (várias abas somam); sem sessão, pelo IP."""
    usuario = current_user.get_id() if current_user.is_authenticated else None
    return f"sininho:u{usuario}" if usuario else f"sininho:ip{request.remote_addr}"


def _id_desde(valor) -> int:
    """Último id que o navegador já tem; qualquer valor estranho vale 0."""
    if isinstance(valor, str) and valor.isascii() and valor.isdecimal() and len(valor) <= 10:
        return int(valor)
    return 0


@notificacoes_bp.route("/novas", methods=["GET"])
# A página consulta a cada 30 s por aba; o teto só barra um cliente em laço.
@limiter.limit("30 per minute", key_func=_chave_limite_sininho)
def novas():
    """Polling leve do sininho (substitui o WebSocket).

    Cada consulta é uma requisição curta: passa pelos mesmos guardas de sessão de
    qualquer página (credencial, sessão registrada, BOOT_ID, empresa acessível,
    primeiro acesso, termos) e libera a thread em milissegundos. Sem sessão
    válida: 401 em JSON, e o navegador para de consultar.
    """
    if not current_user.is_authenticated:
        resposta = jsonify(erro="Sessão encerrada. Entre novamente.")
        return resposta, 401
    return jsonify(novas_do_usuario(current_user.id_usuario, _id_desde(request.args.get("desde"))))


@notificacoes_bp.route("/marcar-lidas", methods=["POST"])
@login_required
def marcar_lidas():
    db.session.execute(
        update(Notificacao)
        .where(Notificacao.id_usuario == current_user.id_usuario, Notificacao.lida.is_(False))
        .values(lida=True)
    )
    db.session.commit()
    # Volta para a página de origem quando possível (UX do dropdown), validando
    # que o referrer é local.
    return redirect(_local_ou_padrao(request.referrer))


@notificacoes_bp.route("/<int:id_notificacao>/abrir", methods=["GET", "POST"])
@login_required
def abrir(id_notificacao):
    notif = db.session.get(Notificacao, id_notificacao)
    # Só o dono da notificação pode abri-la; senão, cai no dashboard.
    if notif is None or notif.id_usuario != current_user.id_usuario:
        return redirect(_destino_padrao())
    # SEC-002: a mutação exige POST (+ CSRF). O item do sininho é um
    # formulário; um GET (prefetch, histórico, link copiado) apenas navega.
    if request.method == "POST" and not notif.lida:
        notif.lida = True
        db.session.commit()
    return redirect(_local_ou_padrao(notif.link_destino))
