"""
NEXO - Faturamento Inteligente | Regras dos chamados de suporte
================================================================
Tamanho, repetição e avisos dos chamados (cliente e equipe NEXO):

  - Mensagem: até 5.000 caracteres, contados depois de normalizar as quebras de linha e tirar os espaços das
    pontas. O navegador conta cada quebra como 1 no maxlength do formulário (que tem o mesmo teto) e envia CRLF,
    2 caracteres: contar o texto cru recusaria uma mensagem que o formulário aceitou. A mensagem é gravada já
    normalizada.
  - Corpo do pedido: até 128 KiB nas rotas de chamado, aplicado antes de o formulário ser lido, inclusive o
    token CSRF (aplicar_teto_do_corpo, registrado antes do CSRFProtect). O pedido maior é recusado com 413
    sem ser processado. Uma mensagem de 5.000 caracteres de 4 bytes, codificada no formulário (%XX), fica
    abaixo de 60 KiB.
  - Clique duplo: na abertura, o mesmo cliente com o mesmo assunto, categoria e mensagem em até 120 s, com o
    chamado anterior ainda não resolvido, recebe o chamado que já existe em vez de um segundo. Na resposta, a
    mesma mensagem do mesmo autor em até 60 s é desconsiderada só quando ela ainda é a última do chamado (sem
    mensagem de outro participante no meio). A empresa fica bloqueada até o commit nas duas gravações
    (company_security.bloquear_empresa, a ordem Empresa -> filhos dos demais fluxos): o segundo pedido de um
    clique duplo só confere depois de o primeiro gravar.
  - Avisos aos admins: as respostas do cliente em sequência viram um aviso só enquanto o anterior, igual e não
    lido, tiver até 10 minutos (notifications.notificar_admins com agrupar_em). As mensagens ficam todas.
"""

from datetime import timedelta

from flask import request
from sqlalchemy import select

from extensions import db
from models import ChamadoSuporte, MensagemSuporte

LIMITE_MENSAGEM = 5000
TETO_DO_CORPO = 128 * 1024
JANELA_ABERTURA_REPETIDA = timedelta(seconds=120)
JANELA_RESPOSTA_REPETIDA = timedelta(seconds=60)
JANELA_AVISO_AGRUPADO = timedelta(minutes=10)
# As rotas que gravam chamado ou mensagem (a do admin também muda o status).
ENDPOINTS_DE_SUPORTE = frozenset({"cliente.tickets", "cliente.ticket_detalhe", "admin.ticket_detalhe"})

LIMITE_POR_EXTENSO = f"{LIMITE_MENSAGEM:,}".replace(",", ".")
MENSAGEM_LONGA = (f"A mensagem passa de {LIMITE_POR_EXTENSO} caracteres. Nada foi enviado: divida o texto em "
                  "mais de uma mensagem.")
MENSAGEM_CORPO_GRANDE = (f"A mensagem passa do tamanho permitido no suporte (até {LIMITE_POR_EXTENSO} caracteres "
                         "por mensagem). Nada foi gravado: encurte o texto e envie de novo.")
MENSAGEM_CHAMADO_REPETIDO = "Este chamado já foi aberto há instantes. Nada foi duplicado: a conversa é esta."
MENSAGEM_RESPOSTA_REPETIDA = "Esta mensagem já foi enviada há instantes. Nada foi duplicado."


def normalizar_mensagem(texto) -> str:
    """Quebras de linha como LF (CRLF e CR do navegador viram LF) e sem espaços nas pontas."""
    if not isinstance(texto, str):
        return ""
    return texto.replace("\r\n", "\n").replace("\r", "\n").strip()


def mensagem_longa_demais(texto: str) -> bool:
    return len(texto) > LIMITE_MENSAGEM


def aplicar_teto_do_corpo() -> None:
    """before_request das rotas de chamado: o teto do corpo vale para este pedido antes de qualquer leitura do
    formulário (o Werkzeug recusa com 413 o Content-Length maior e corta o corpo sem ele). Fora dessas rotas, o
    teto continua o MAX_CONTENT_LENGTH do app (os envios de planilha)."""
    if request.method == "POST" and request.endpoint in ENDPOINTS_DE_SUPORTE:
        request.max_content_length = TETO_DO_CORPO


def chamado_repetido(id_usuario: int, assunto: str, categoria: str, mensagem: str, agora):
    """O id do chamado que o mesmo cliente acabou de abrir com o mesmo assunto, categoria e mensagem (o clique
    duplo), ainda não resolvido, ou None. Quem chama já bloqueou a empresa."""
    candidatos = db.session.execute(
        select(ChamadoSuporte.id_chamado).where(
            ChamadoSuporte.id_usuario_cliente == id_usuario,
            ChamadoSuporte.assunto == assunto,
            ChamadoSuporte.categoria == categoria,
            ChamadoSuporte.status_chamado != "RESOLVIDO",
            ChamadoSuporte.data_abertura >= agora - JANELA_ABERTURA_REPETIDA,
            ChamadoSuporte.data_abertura <= agora,
        ).order_by(ChamadoSuporte.id_chamado.desc())
    ).scalars().all()
    for id_chamado in candidatos:
        primeira = db.session.scalar(
            select(MensagemSuporte.mensagem).where(MensagemSuporte.id_chamado == id_chamado)
            .order_by(MensagemSuporte.id_mensagem).limit(1))
        if primeira is not None and normalizar_mensagem(primeira) == mensagem:
            return id_chamado
    return None


def resposta_repetida(id_chamado: int, id_usuario: int, mensagem: str, agora) -> bool:
    """A mesma mensagem, do mesmo autor, enviada há até 60 s e ainda a última do chamado (nenhuma de outro
    participante no meio)? Quem chama já bloqueou a empresa."""
    ultima = db.session.execute(
        select(MensagemSuporte.id_usuario_remetente, MensagemSuporte.mensagem, MensagemSuporte.data_envio)
        .where(MensagemSuporte.id_chamado == id_chamado)
        .order_by(MensagemSuporte.id_mensagem.desc()).limit(1)
    ).first()
    if ultima is None or ultima.id_usuario_remetente != id_usuario:
        return False
    if normalizar_mensagem(ultima.mensagem) != mensagem:
        return False
    return agora - JANELA_RESPOSTA_REPETIDA <= ultima.data_envio <= agora
