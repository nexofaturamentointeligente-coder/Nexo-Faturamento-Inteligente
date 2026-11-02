"""
NEXO - Blueprint de avaliações dos clientes (COM-002 / AUD-LANDING-001)
=======================================================================
Fluxo: landing -> /avaliar -> login -> formulário -> banco -> moderação -> carrossel.

Rotas (mutações só por POST com CSRF):
  /avaliar                                   → porta de entrada pública (só redireciona)
  /cliente/avaliacao                         → CLIENTE: formulário e envio
  /cliente/minha-avaliacao                   → CLIENTE: consulta da própria avaliação
  /cliente/avaliacao/editar                  → CLIENTE: edição e reenvio à moderação
  /cliente/avaliacao/consentimento/retirar   → CLIENTE: retira a autorização
  /cliente/avaliacao/solicitacao/exibida     → CLIENTE: pausa o pedido dos 60 dias
  /cliente/avaliacao/solicitacao/adiar       → CLIENTE: "Mais tarde"
  /admin/avaliacoes                          → ADMIN: lista, filtros e paginação
  /admin/avaliacoes/<id>                     → ADMIN: detalhe, histórico e decisões
  /admin/avaliacoes/<id>/{aprovar,rejeitar,retirar}
  /admin/avaliacoes/sugestoes                → ADMIN: sugestões privadas
  /admin/avaliacoes/sugestoes/<id>/status    → ADMIN: acompanhamento

As rotas do cliente não recebem identificador nenhum: usuário e empresa vêm da
sessão, o que elimina IDOR por construção. As regras ficam em avaliacoes.py.
"""

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import avaliacoes as servico
from blueprints.admin import admin_required
from blueprints.cliente import cliente_required
from extensions import chave_limite_usuario, db, limiter
from models import COMENTARIO_PUBLICO_MAX, NOME_EXIBICAO_MAX, STATUS_MODERACAO, STATUS_SUGESTAO, SUGESTAO_PRIVADA_MAX
from notifications import criar_notificacao, notificar_admins

avaliacoes_bp = Blueprint("avaliacoes", __name__)

MENSAGEM_ESQUEMA_AUSENTE = "As avaliações estarão disponíveis em breve. Tente novamente mais tarde."
MENSAGEM_FALHA_GRAVACAO = "Não foi possível salvar sua avaliação. Nada foi registrado; tente novamente em instantes."
MENSAGEM_JA_REGISTRADA = "Sua avaliação já está registrada. Você pode consultá-la e editá-la aqui."


def _quer_json() -> bool:
    """Envio pelo avaliacao.js (fetch): recebe JSON em vez de redirect ou página."""
    return request.headers.get("X-Requested-With") == "fetch"


def _nome_empresa(usuario) -> str:
    return usuario.empresa.nome_fantasia or usuario.empresa.razao_social


def _indisponivel():
    """Código publicado antes da migração: avisa, sem tocar nas tabelas novas."""
    if _quer_json():
        return jsonify(ok=False, mensagem=MENSAGEM_ESQUEMA_AUSENTE, erros={}), 503
    return render_template("erro_simples.html", titulo="Avaliações indisponíveis",
                           mensagem=MENSAGEM_ESQUEMA_AUSENTE, codigo=503,
                           painel_url=url_for("cliente.dashboard" if current_user.is_cliente
                                              else "admin.dashboard")), 503


def _acesso_restrito(mensagem: str):
    if _quer_json():
        return jsonify(ok=False, mensagem=mensagem, erros={}), 403
    return render_template("cliente/acesso_restrito.html", titulo="Avaliação indisponível", mensagem=mensagem), 403


def _pagina_formulario(modo: str, valores: dict, erros: dict | None = None, status: int = 200):
    return render_template(
        "cliente/avaliacao_form.html",
        modo=modo, valores=valores, erros=erros or {},
        acao_url=url_for("avaliacoes.editar" if modo == "editar" else "avaliacoes.formulario"),
        rotulos_nota=servico.ROTULOS_NOTA,
        limites={"comentario_min": servico.COMENTARIO_PUBLICO_MIN, "comentario_max": COMENTARIO_PUBLICO_MAX,
                 "sugestao_max": SUGESTAO_PRIVADA_MAX, "nome_max": NOME_EXIBICAO_MAX},
        texto_consentimento=servico.TEXTO_CONSENTIMENTO,
        politica_url=servico.url_politica_privacidade(),
        nome_empresa=_nome_empresa(current_user),
        identificacao_anonima=servico.IDENTIFICACAO_ANONIMA,
    ), status


def _resposta_invalida(modo: str, erros: dict, valores: dict):
    mensagem = "Confira os campos destacados."
    if _quer_json():
        return jsonify(ok=False, mensagem=mensagem, erros=erros), 422
    flash(mensagem, "danger")
    return _pagina_formulario(modo, valores, erros, 422)


def _resposta_confirmada(mensagem: str):
    """Só depois do COMMIT: nunca há confirmação sem o banco ter gravado."""
    flash(mensagem, "success")
    destino = url_for("avaliacoes.minha_avaliacao")
    if _quer_json():
        return jsonify(ok=True, mensagem=mensagem, destino=destino)
    return redirect(destino)


def _resposta_ja_registrada():
    flash(MENSAGEM_JA_REGISTRADA, "info")
    destino = url_for("avaliacoes.minha_avaliacao")
    if _quer_json():
        return jsonify(ok=False, mensagem=MENSAGEM_JA_REGISTRADA, erros={}, destino=destino), 409
    return redirect(destino)


def _resposta_falha_gravacao(erro, modo: str, valores: dict):
    db.session.rollback()
    # Só o tipo do erro: comentário, sugestão e parâmetros SQL não vão para o log.
    current_app.logger.warning("Avaliação não persistida (%s).", type(erro).__name__)
    if _quer_json():
        return jsonify(ok=False, mensagem=MENSAGEM_FALHA_GRAVACAO, erros={}), 503
    flash(MENSAGEM_FALHA_GRAVACAO, "danger")
    return _pagina_formulario(modo, valores, None, 503)


# =====================================================================
# Porta de entrada pública
# =====================================================================
@avaliacoes_bp.route("/avaliar")
def avaliar():
    """Destino do botão "Deixar Avaliação". Não expõe nada: só encaminha."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=url_for("avaliacoes.avaliar")))
    if current_user.is_admin:
        flash("Administradores não registram avaliações em nome de clientes. "
              "As avaliações recebidas estão nesta página.", "info")
        return redirect(url_for("avaliacoes.admin_lista"))
    return redirect(url_for("avaliacoes.formulario"))


# =====================================================================
# Área do CLIENTE
# =====================================================================
@avaliacoes_bp.route("/cliente/avaliacao", methods=["GET", "POST"])
@cliente_required
@limiter.limit("10 per hour", methods=["POST"], key_func=chave_limite_usuario)
def formulario():
    if not servico.esquema_avaliacoes_disponivel():
        return _indisponivel()
    motivo = servico.motivo_inelegivel(current_user)
    if motivo:
        return _acesso_restrito(motivo)
    # Uma avaliação principal por usuário: quem já enviou edita em vez de duplicar.
    if servico.obter_avaliacao_usuario(current_user.id_usuario) is not None:
        return _resposta_ja_registrada() if request.method == "POST" else redirect(url_for("avaliacoes.minha_avaliacao"))
    if request.method == "GET":
        return _pagina_formulario("novo", {
            "nota": None, "comentario": "", "sugestao": "", "autoriza_publicacao": False,
            "nome_exibicao": "", "autoriza_nome_empresa": False,
        })

    dados, erros, valores = servico.ler_formulario(request.form)
    if erros:
        return _resposta_invalida("novo", erros, valores)
    try:
        servico.criar_avaliacao(db.session, current_user, dados)
        notificar_admins(f"Nova avaliação de {_nome_empresa(current_user)} aguardando moderação.",
                         url_for("avaliacoes.admin_lista", status="PENDENTE"))
        db.session.commit()
    except IntegrityError as erro:
        db.session.rollback()
        if "uq_avaliacao_cliente_usuario" in str(erro.orig) or "avaliacao_cliente.id_usuario" in str(erro.orig):
            return _resposta_ja_registrada()
        return _resposta_falha_gravacao(erro, "novo", valores)
    except SQLAlchemyError as erro:
        return _resposta_falha_gravacao(erro, "novo", valores)
    return _resposta_confirmada(servico.MENSAGEM_SUCESSO)


@avaliacoes_bp.route("/cliente/minha-avaliacao")
@cliente_required
def minha_avaliacao():
    if not servico.esquema_avaliacoes_disponivel():
        return _indisponivel()
    avaliacao = servico.obter_avaliacao_usuario(current_user.id_usuario)
    return render_template(
        "cliente/minha_avaliacao.html",
        avaliacao=avaliacao,
        sugestao=avaliacao.sugestao if avaliacao else None,
        rotulos_nota=servico.ROTULOS_NOTA,
        rotulos_sugestao=servico.ROTULOS_SUGESTAO,
        pode_editar=servico.motivo_inelegivel(current_user) is None,
        identificacao_anonima=servico.IDENTIFICACAO_ANONIMA,
        nome_empresa=_nome_empresa(current_user),
    )


@avaliacoes_bp.route("/cliente/avaliacao/editar", methods=["GET", "POST"])
@cliente_required
@limiter.limit("10 per hour", methods=["POST"], key_func=chave_limite_usuario)
def editar():
    if not servico.esquema_avaliacoes_disponivel():
        return _indisponivel()
    motivo = servico.motivo_inelegivel(current_user)
    if motivo:
        return _acesso_restrito(motivo)
    if request.method == "GET":
        avaliacao = servico.obter_avaliacao_usuario(current_user.id_usuario)
        if avaliacao is None:
            return redirect(url_for("avaliacoes.formulario"))
        return _pagina_formulario("editar", servico.valores_da_avaliacao(avaliacao))

    dados, erros, valores = servico.ler_formulario(request.form)
    if erros:
        return _resposta_invalida("editar", erros, valores)
    try:
        avaliacao, reenviada = servico.atualizar_avaliacao(db.session, current_user, dados)
        if avaliacao is None:
            db.session.rollback()
            if _quer_json():
                return jsonify(ok=False, mensagem="Você ainda não enviou uma avaliação.", erros={},
                               destino=url_for("avaliacoes.formulario")), 409
            return redirect(url_for("avaliacoes.formulario"))
        if reenviada:
            notificar_admins(f"Nova avaliação de {_nome_empresa(current_user)} aguardando moderação (editada).",
                             url_for("avaliacoes.admin_lista", status="PENDENTE"))
        db.session.commit()
    except SQLAlchemyError as erro:
        return _resposta_falha_gravacao(erro, "editar", valores)
    return _resposta_confirmada(
        "Avaliação atualizada e reenviada para revisão da equipe NEXO." if reenviada
        else "Avaliação atualizada."
    )


@avaliacoes_bp.route("/cliente/avaliacao/consentimento/retirar", methods=["POST"])
@cliente_required
# Generoso e sem exigir empresa ATIVA: retirar a autorização é direito do cliente.
@limiter.limit("30 per hour", methods=["POST"], key_func=chave_limite_usuario)
def retirar_consentimento():
    destino = redirect(url_for("avaliacoes.minha_avaliacao"))
    if not servico.esquema_avaliacoes_disponivel():
        return _indisponivel()
    try:
        retirado = servico.retirar_consentimento(db.session, current_user)
        db.session.commit()
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Retirada de autorização não persistida (%s).", type(erro).__name__)
        flash("Não foi possível retirar a autorização agora. Tente novamente em instantes.", "danger")
        return destino
    if retirado:
        flash("Autorização retirada. Sua avaliação não será mais exibida nos canais públicos da NEXO.", "success")
    else:
        flash("Sua avaliação não tem autorização de publicação ativa.", "info")
    return destino


def _registrar_solicitacao(operacao, descricao: str) -> bool:
    if not servico.esquema_avaliacoes_disponivel():
        return False
    try:
        operacao(db.session, current_user)
        db.session.commit()
        return True
    except SQLAlchemyError as erro:
        # Nunca atrapalha o portal: o pior caso é o pedido reaparecer depois.
        db.session.rollback()
        current_app.logger.warning("%s não persistido (%s).", descricao, type(erro).__name__)
        return False


@avaliacoes_bp.route("/cliente/avaliacao/solicitacao/exibida", methods=["POST"])
@cliente_required
@limiter.limit("60 per hour", methods=["POST"], key_func=chave_limite_usuario)
def solicitacao_exibida():
    """Chamada pelo avaliacao-solicitacao.js quando o pedido aparece na tela."""
    salvo = _registrar_solicitacao(servico.registrar_exibicao_solicitacao, "Exibição do pedido de avaliação")
    return jsonify(ok=salvo), 200 if salvo else 503


@avaliacoes_bp.route("/cliente/avaliacao/solicitacao/adiar", methods=["POST"])
@cliente_required
@limiter.limit("60 per hour", methods=["POST"], key_func=chave_limite_usuario)
def solicitacao_adiar():
    """Botão "Mais tarde": funciona como formulário comum, mesmo sem JavaScript."""
    salvo = _registrar_solicitacao(servico.adiar_solicitacao, "Adiamento do pedido de avaliação")
    if _quer_json():
        return jsonify(ok=salvo), 200 if salvo else 503
    if not salvo:
        flash("Não foi possível registrar o adiamento agora.", "warning")
    return redirect(url_for("cliente.dashboard"))


# =====================================================================
# Área ADMIN: moderação
# =====================================================================
def _versao_lida():
    bruto = request.form.get("versao") or ""
    return int(bruto) if bruto.isascii() and bruto.isdecimal() and len(bruto) <= 9 else None


def _moderar(id_avaliacao: int, operacao, mensagem_sucesso, avisar_cliente=None):
    """Executa uma decisão: transação única, com a notificação ao cliente junto."""
    detalhe = redirect(url_for("avaliacoes.admin_detalhe", id_avaliacao=id_avaliacao))
    if not servico.esquema_avaliacoes_disponivel():
        return _indisponivel()
    try:
        avaliacao = operacao()
        if avisar_cliente is not None:
            criar_notificacao(avaliacao.id_usuario, avisar_cliente(avaliacao),
                              url_for("avaliacoes.minha_avaliacao"))
        db.session.commit()
    except servico.ErroModeracao as erro:
        db.session.rollback()
        flash(str(erro), "warning")
        return detalhe
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Decisão de moderação não persistida (%s).", type(erro).__name__)
        flash("Não foi possível registrar a decisão. Tente novamente.", "danger")
        return detalhe
    current_app.logger.info("Moderação de avaliação registrada | avaliacao=%s admin=%s",
                            id_avaliacao, current_user.id_usuario)
    flash(mensagem_sucesso(avaliacao), "success")
    return detalhe


@avaliacoes_bp.route("/admin/avaliacoes")
@admin_required
def admin_lista():
    if not servico.esquema_avaliacoes_disponivel():
        return _indisponivel()
    filtros = servico.ler_filtros(request.args, STATUS_MODERACAO)
    return render_template(
        "admin/avaliacoes.html",
        pagina=servico.listar_avaliacoes(filtros), filtros=filtros,
        contagens=servico.contagem_por_status(),
        empresas=servico.empresas_com_avaliacao(),
        rotulos_status=servico.ROTULOS_MODERACAO, ordens=servico.ORDENS_AVALIACAO,
        total_sugestoes=sum(servico.contagem_sugestoes_por_status().values()),
    )


@avaliacoes_bp.route("/admin/avaliacoes/<int:id_avaliacao>")
@admin_required
def admin_detalhe(id_avaliacao):
    if not servico.esquema_avaliacoes_disponivel():
        return _indisponivel()
    avaliacao = servico.obter_avaliacao_detalhada(id_avaliacao)
    if avaliacao is None:
        flash("Avaliação não encontrada.", "danger")
        return redirect(url_for("avaliacoes.admin_lista"))
    return render_template(
        "admin/avaliacao_detalhe.html",
        avaliacao=avaliacao, rotulos_nota=servico.ROTULOS_NOTA, rotulos_evento=servico.ROTULOS_EVENTO,
        rotulos_sugestao=servico.ROTULOS_SUGESTAO, status_sugestao=STATUS_SUGESTAO,
        identificacao_anonima=servico.IDENTIFICACAO_ANONIMA,
        motivo_min=servico.MOTIVO_MIN, motivo_max=servico.MOTIVO_MAX,
    )


@avaliacoes_bp.route("/admin/avaliacoes/<int:id_avaliacao>/aprovar", methods=["POST"])
@admin_required
@limiter.limit("120 per hour", methods=["POST"], key_func=chave_limite_usuario)
def admin_aprovar(id_avaliacao):
    return _moderar(
        id_avaliacao,
        lambda: servico.aprovar_avaliacao(db.session, id_avaliacao, current_user, _versao_lida()),
        lambda a: ("Avaliação aprovada e publicada na landing." if a.no_ar
                   else "Avaliação aprovada internamente. Sem autorização do cliente, ela não será publicada."),
        lambda a: ("Sua avaliação foi aprovada e já aparece nos depoimentos da NEXO." if a.no_ar
                   else "Sua avaliação foi revisada pela equipe NEXO. Obrigado!"),
    )


@avaliacoes_bp.route("/admin/avaliacoes/<int:id_avaliacao>/rejeitar", methods=["POST"])
@admin_required
@limiter.limit("120 per hour", methods=["POST"], key_func=chave_limite_usuario)
def admin_rejeitar(id_avaliacao):
    return _moderar(
        id_avaliacao,
        lambda: servico.rejeitar_avaliacao(db.session, id_avaliacao, current_user, _versao_lida(),
                                           request.form.get("motivo")),
        lambda a: "Avaliação rejeitada. O registro e a justificativa foram mantidos.",
        lambda a: "Sua avaliação foi revisada e não será publicada. Você pode editá-la e reenviá-la.",
    )


@avaliacoes_bp.route("/admin/avaliacoes/<int:id_avaliacao>/retirar", methods=["POST"])
@admin_required
@limiter.limit("120 per hour", methods=["POST"], key_func=chave_limite_usuario)
def admin_retirar(id_avaliacao):
    return _moderar(
        id_avaliacao,
        lambda: servico.retirar_publicacao(db.session, id_avaliacao, current_user, _versao_lida(),
                                           request.form.get("motivo")),
        lambda a: "Avaliação retirada do carrossel. O registro histórico foi mantido.",
    )


# =====================================================================
# Área ADMIN: sugestões privadas
# =====================================================================
@avaliacoes_bp.route("/admin/avaliacoes/sugestoes")
@admin_required
def admin_sugestoes():
    if not servico.esquema_avaliacoes_disponivel():
        return _indisponivel()
    filtros = servico.ler_filtros(request.args, STATUS_SUGESTAO)
    return render_template(
        "admin/avaliacoes_sugestoes.html",
        pagina=servico.listar_sugestoes(filtros), filtros=filtros,
        contagens=servico.contagem_sugestoes_por_status(),
        empresas=servico.empresas_com_avaliacao(),
        rotulos_status=servico.ROTULOS_SUGESTAO, status_opcoes=STATUS_SUGESTAO,
        total_avaliacoes=sum(servico.contagem_por_status().values()),
    )


@avaliacoes_bp.route("/admin/avaliacoes/sugestoes/<int:id_sugestao>/status", methods=["POST"])
@admin_required
@limiter.limit("120 per hour", methods=["POST"], key_func=chave_limite_usuario)
def admin_sugestao_status(id_sugestao):
    destino = redirect(request.form.get("voltar_para") if _voltar_seguro() else url_for("avaliacoes.admin_sugestoes"))
    if not servico.esquema_avaliacoes_disponivel():
        return _indisponivel()
    try:
        servico.atualizar_status_sugestao(db.session, id_sugestao, request.form.get("status"), current_user)
        db.session.commit()
    except servico.ErroModeracao as erro:
        db.session.rollback()
        flash(str(erro), "warning")
        return destino
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Acompanhamento de sugestão não persistido (%s).", type(erro).__name__)
        flash("Não foi possível atualizar o acompanhamento. Tente novamente.", "danger")
        return destino
    flash("Acompanhamento da sugestão atualizado.", "success")
    return destino


def _voltar_seguro() -> bool:
    """Volta para a mesma lista filtrada, sem aceitar destino fora da própria área."""
    alvo = request.form.get("voltar_para") or ""
    return alvo.startswith("/admin/avaliacoes") and not any(c in alvo for c in "\\\r\n\t")
