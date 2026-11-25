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

O navegador busca as novas pela rota GET /notificacoes/novas (polling leve, a
cada 30 s com a aba visível). A notificação só existe para os outros depois do
COMMIT de quem a criou, e cada consulta passa pela validação de sessão.
"""

import logging

from sqlalchemy import select

from extensions import db
from horario import agora_utc, horario_brasilia, iso_utc
from models import Empresa, Notificacao, Usuario
from preferencias_usuario import ids_com_aviso_desligado
from session_security import condicoes_empresa_acessivel

logger = logging.getLogger(__name__)


def classificar_notificacao(texto: str) -> dict:
    """
    Classifica uma notificação em um TIPO visual de badge, a partir do texto.
    Fonte única de verdade usada tanto no render do servidor (Jinja) quanto na
    resposta do polling do sininho (JS) — assim a badge é idêntica nos dois caminhos.

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
    # Avisos com começo fixo: o selo repete o que a tela mostra, e o texto livre entre aspas (assunto do
    # chamado, nome da empresa) não decide o selo ("indisponível" no assunto virava "Nova Análise", e
    # "Erro não resolvido", "Resolvido").
    if t.startswith("recebemos e processamos"):
        return {"tipo": "analise", "rotulo": "Em análise"}
    if t.startswith("a equipe nexo respondeu seu chamado"):
        return {"tipo": "aberto", "rotulo": "Resposta"}
    if t.startswith("seu chamado “") and t.endswith("foi marcado como resolvido."):
        return {"tipo": "resolvido", "rotulo": "Resolvido"}
    if t.startswith(("novo chamado de ", "nova resposta de ", "nova planilha de ", "nova avaliação de ")):
        return {"tipo": "aberto", "rotulo": "Aberto"}
    if t.startswith("sua avaliação"):
        return {"tipo": "analise", "rotulo": "Avaliação"}
    if "resolvid" in t:
        return {"tipo": "resolvido", "rotulo": "Resolvido"}
    if ("dispon" in t) or ("relatório estrat" in t) or ("relatorio estrat" in t) or ("processamos" in t):
        return {"tipo": "analise", "rotulo": "Nova Análise"}
    return {"tipo": "aberto", "rotulo": "Aberto"}


def payload_notificacao(n: Notificacao) -> dict:
    """Item do sininho em JSON, sem ORM, para a resposta do polling.

    O horário é o data_criacao gravado, formatado no horário de Brasília pela
    mesma função do template: o item que chega pelo polling e o da página
    recarregada mostram o mesmo texto. "instante" leva o ISO 8601 com fuso; o
    navegador não converte horários.
    """
    badge = classificar_notificacao(n.texto)
    return {
        "id": n.id_notificacao, "texto": n.texto,
        "link": n.link_destino or "",
        "hora": horario_brasilia(n.data_criacao, "curto"),
        "hora_completa": horario_brasilia(n.data_criacao, rotulo=True),
        "instante": iso_utc(n.data_criacao),
        "badge": badge["tipo"], "badge_rotulo": badge["rotulo"],
    }


# Teto de itens por consulta: o sininho mostra 5; o restante entra na contagem.
LIMITE_NOVAS_POR_CONSULTA = 5


def novas_do_usuario(id_usuario: int, desde: int) -> dict:
    """Não lidas do usuário com id maior que `desde` (as mais novas, até o teto),
    em ordem crescente de id, mais o total de não lidas e o maior id já visto.

    Só lê o que já foi confirmado no banco. Quem chama já validou a sessão.
    """
    nao_lidas = db.session.execute(
        select(db.func.count(Notificacao.id_notificacao)).where(
            Notificacao.id_usuario == id_usuario,
            Notificacao.lida.is_(False),
        )
    ).scalar() or 0
    novas = db.session.execute(
        select(Notificacao).where(
            Notificacao.id_usuario == id_usuario,
            Notificacao.lida.is_(False),
            Notificacao.id_notificacao > desde,
        ).order_by(Notificacao.id_notificacao.desc()).limit(LIMITE_NOVAS_POR_CONSULTA)
    ).scalars().all()
    novas.reverse()
    ultimo = max([desde, *(n.id_notificacao for n in novas)])
    return {
        "nao_lidas": int(nao_lidas),
        "novas": [payload_notificacao(n) for n in novas],
        "ultimo": ultimo,
    }


def criar_notificacao(id_usuario: int, texto: str, link_destino: str | None = None) -> Notificacao:
    """
    Cria UMA notificação. Ela só aparece no sininho depois do commit de quem
    chama; o flush garante o id_notificacao para quem precisar dele.
    """
    n = Notificacao(id_usuario=id_usuario, texto=texto, link_destino=link_destino)
    db.session.add(n)
    db.session.flush()  # garante o PK
    return n


def notificar_admins(texto: str, link_destino: str | None = None, *, agrupar_em=None, agora=None) -> int:
    """Notifica TODOS os usuários ADMIN ativos. Retorna a quantidade gerada.

    `agrupar_em` (timedelta): o admin que já tem um aviso igual (mesmo texto e mesmo destino), ainda não
    lido e criado nessa janela até `agora`, não recebe outro. As respostas do cliente em sequência num
    chamado viram um aviso só por admin (chamados_suporte); as mensagens continuam todas na conversa.
    `agora` é o instante da resposta, para não ler o relógio de novo."""
    admins = db.session.execute(
        select(Usuario.id_usuario).where(Usuario.role == "ADMIN", Usuario.ativo.is_(True))
    ).scalars().all()
    if agrupar_em is not None and admins:
        agora = agora if agora is not None else agora_utc()
        avisados = set(db.session.execute(
            select(Notificacao.id_usuario).where(
                Notificacao.id_usuario.in_(admins),
                Notificacao.lida.is_(False),
                Notificacao.texto == texto,
                Notificacao.link_destino == link_destino,
                Notificacao.data_criacao >= agora - agrupar_em,
                Notificacao.data_criacao <= agora,
            )
        ).scalars())
        admins = [uid for uid in admins if uid not in avisados]
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
    Dados do sininho para o usuário autenticado: contagem de não-lidas, as
    5 notificações mais recentes e o maior id entre elas (ponto de partida do
    polling, que só traz as de id maior). Usado pelo context processor (toda página).
    """
    vazio = {"notif_nao_lidas": 0, "notif_recentes": [], "notif_ultimo_id": 0}
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
        # Ponto de partida desconhecido: o polling não trata as não lidas antigas
        # como novas (sem alertas flutuantes na primeira consulta).
        return {**vazio, "notif_ultimo_id": None}

    return {"notif_nao_lidas": int(nao_lidas), "notif_recentes": recentes,
            "notif_ultimo_id": max((n.id_notificacao for n in recentes), default=0)}
