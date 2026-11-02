"""
NEXO - Blueprint da gestão contratual (ADMIN)
=============================================
Administração → Empresas → Detalhes → Contratos. Integra-se à área
administrativa existente (mesmo admin_required, layout e componentes) sem
duplicar o cadastro de empresas.

Rotas (todas exigem ADMIN; mutações só por POST com CSRF):
  /admin/empresas/<id>/detalhes                    → dados, situação, contratos,
                                                     coberturas e histórico
  /admin/empresas/<id>/contratos/novo              → contratação
  /admin/empresas/<id>/contratos/<c>/renovar       → renovação
  /admin/empresas/<id>/contratos/<c>/alterar-plano → alteração de plano
  /admin/empresas/<id>/contratos/<c>/<acao>        → suspender, prorrogar,
                                                     reativar, cancelar
  /admin/empresas/<id>/contratos/<c>/cobertura     → período pago
  /admin/empresas/<id>/coberturas/<k>/revogar      → revogação auditada
  /admin/empresas/<id>/regras-contratuais/<acao>   → ativar / desativar
  /admin/contratos                                 → visão geral
  /admin/contratos/reconciliar                     → vencimentos (simulação/aplicação)
  /admin/contratos/lembretes-renovacao             → lembrete de vigência terminando

As regras ficam em contract_admin/contract_policy; aqui só há leitura do
formulário, transação e mensagens.
"""

import json
from datetime import date, datetime

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import configuracoes_conta
import contract_admin as operacoes
from blueprints.admin import _desconectar_revogados, _revogacoes_socket, admin_required
from contract_policy import (
    EsquemaContratualIndisponivel, esquema_contratual_disponivel, formatar_instante,
    obter_coberturas_pagas, obter_contrato_aplicavel, obter_situacao_contratual, politica_prazos, utc,
)
from emails import envio_disponivel
from extensions import chave_limite_usuario, db, limiter
from models import CoberturaPaga, Contrato, Empresa, EventoContratual, Plano, Usuario
from session_security import condicoes_empresa_acessivel

contratos_bp = Blueprint("contratos", __name__, url_prefix="/admin")

ACOES_CONTRATO = {
    "suspender": "Suspender contrato",
    "prorrogar": "Prorrogar suspensão",
    "reativar": "Reativar contrato",
    "cancelar": "Cancelar contrato",
}
ACOES_REGRAS = {"ativar": "Ativar regras contratuais", "desativar": "Desativar regras contratuais"}
ROTULOS_EVENTO = {
    "CONTRATACAO": "Contratação", "RENOVACAO": "Renovação", "ALTERACAO_PLANO": "Alteração de plano",
    "SUSPENSAO": "Suspensão", "PRORROGACAO": "Prorrogação da suspensão", "REATIVACAO": "Reativação",
    "CANCELAMENTO": "Cancelamento", "COBERTURA_CONFIRMADA": "Período pago confirmado",
    "COBERTURA_REVOGADA": "Período pago revogado", "REGRAS_ATIVADAS": "Regras contratuais ativadas",
    "REGRAS_DESATIVADAS": "Regras contratuais desativadas",
}
_ROTULOS_DETALHES = {
    "id_plano": "Plano", "valor_mensal": "Valor mensal", "data_inicio": "Início", "data_fim": "Término",
    "status_conta_anterior": "Status anterior da conta", "contrato_anterior": "Contrato anterior",
    "plano_anterior": "Plano anterior", "valor_anterior": "Valor anterior",
    "inicio_anterior": "Início anterior", "fim_anterior": "Término anterior",
    "suspenso_em": "Início da suspensão", "suspensao_limite": "Limite da suspensão",
    "suspensao_limite_original": "Limite original", "suspensao_limite_anterior": "Limite anterior",
    "suspensao_limite_novo": "Novo limite", "pdf_limite": "Exportação de PDF até",
    "pdf_limite_anterior": "PDF até (anterior)", "pdf_limite_novo": "PDF até (vigente)",
    "ampliacao_pdf": "Ampliação de PDF autorizada", "meses_suspensao": "Meses de suspensão",
    "dias_exportacao_pdf": "Dias para PDF", "fuso": "Fuso horário", "suspensao_motivo": "Motivo da suspensão",
    "periodo_inicio": "Período pago de", "periodo_fim": "Período pago até", "referencia": "Referência",
    "coberturas_vigentes": "Coberturas vigentes", "ativado_em": "Regras ativadas em",
    "ativado_por": "Ativadas pelo usuário nº",
    "acionado_por": "Reconciliação acionada pelo usuário nº",
}


# ---------------------------------------------------------------------------
# Leitura de formulário
# ---------------------------------------------------------------------------
def _data(nome, obrigatoria=False):
    bruto = (request.form.get(nome) or "").strip()
    if not bruto:
        if obrigatoria:
            raise operacoes.ErroContratual("Preencha todas as datas obrigatórias.")
        return None
    try:
        return datetime.strptime(bruto, "%Y-%m-%d").date()
    except ValueError:
        raise operacoes.ErroContratual("Data inválida (use o seletor de data).") from None


def _instante_local(nome, obrigatorio=False):
    """datetime-local interpretado no fuso da política, nunca no do navegador."""
    bruto = (request.form.get(nome) or "").strip()
    if not bruto:
        if obrigatorio:
            raise operacoes.ErroContratual("Informe a data e a hora.")
        return None
    try:
        ingenuo = datetime.strptime(bruto, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise operacoes.ErroContratual("Data e hora inválidas (use o seletor).") from None
    if not operacoes.DATA_MINIMA.year <= ingenuo.year <= operacoes.DATA_MAXIMA.year:
        raise operacoes.ErroContratual("Data e hora fora do intervalo permitido.")
    return utc(ingenuo.replace(tzinfo=politica_prazos().fuso))


def _inteiro(nome, obrigatorio=False):
    bruto = (request.form.get(nome) or "").strip()
    if not bruto:
        if obrigatorio:
            raise operacoes.ErroContratual("Campo numérico obrigatório ausente.")
        return None
    if not bruto.isdecimal() or len(bruto) > 9:
        raise operacoes.ErroContratual("Valor numérico inválido.")
    return int(bruto)


def _versao():
    return _inteiro("versao", obrigatorio=True)


def _dados_contrato():
    return operacoes.DadosContrato(
        id_plano=_inteiro("id_plano", obrigatorio=True),
        valor_mensal=operacoes.valor_monetario(request.form.get("valor_mensal")),
        data_inicio=_data("data_inicio", obrigatoria=True),
        data_fim=_data("data_fim"),
        data_formalizacao=_data("data_formalizacao"),
        referencia_formalizacao=request.form.get("referencia_formalizacao"),
        meses_suspensao=_inteiro("meses_suspensao"),
        observacoes=request.form.get("observacoes"),
    )


# ---------------------------------------------------------------------------
# Execução transacional comum
# ---------------------------------------------------------------------------
def _executar(funcao, mensagem_sucesso, operacao_log):
    """Executa a operação, confirma e só então derruba sockets revogados.

    Retorna (True, resultado) ou (False, mensagem de erro segura).
    """
    try:
        resultado = funcao()
        revogacoes = _revogacoes_socket(getattr(resultado, "revogados", ()) or ())
        db.session.commit()
    except operacoes.ErroContratual as erro:
        db.session.rollback()
        return False, str(erro)
    except EsquemaContratualIndisponivel as erro:
        db.session.rollback()
        return False, str(erro)
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Operação contratual (%s) recusada pelo banco: %s",
                                   operacao_log, type(erro).__name__)
        return False, ("Não foi possível salvar: os dados foram recusados pelo banco ou alterados "
                       "por outra operação. Recarregue a página e tente novamente.")
    _desconectar_revogados(revogacoes, operacao_log)
    flash(mensagem_sucesso, "success")
    return True, resultado


def _empresa_ou_404(id_empresa):
    empresa = db.session.get(Empresa, id_empresa)
    if empresa is None:
        abort(404)
    return empresa


def _contrato_ou_404(empresa, id_contrato):
    contrato = db.session.scalar(select(Contrato).where(
        Contrato.id_contrato == id_contrato, Contrato.id_empresa == empresa.id_empresa,
    ))
    if contrato is None:
        abort(404)
    return contrato


def _exigir_esquema():
    if not esquema_contratual_disponivel():
        flash("A gestão contratual depende da migração 81287c74387a, ainda não aplicada neste "
              "banco. As empresas seguem no regime legado.", "warning")
        return False
    return True


def _planos():
    return db.session.scalars(select(Plano).where(Plano.ativo).order_by(Plano.valor_mensal)).all()


def _valor_legivel(chave, valor, planos):
    if isinstance(valor, bool):
        return "sim" if valor else "não"
    if chave in ("id_plano", "plano_anterior") and valor in planos:
        return planos[valor]
    if chave in ("valor_mensal", "valor_anterior"):
        return f"R$ {valor}"
    if isinstance(valor, str) and len(valor) == 10:
        try:
            return date.fromisoformat(valor).strftime("%d/%m/%Y")
        except ValueError:
            return valor
    if isinstance(valor, str) and "T" in valor:
        try:
            instante = datetime.fromisoformat(valor)
        except ValueError:
            return valor
        if instante.tzinfo is not None:
            return formatar_instante(instante)
    return str(valor)


def _detalhes_legiveis(bruto, planos):
    try:
        dados = json.loads(bruto) if bruto else {}
    except ValueError:
        return []
    if not isinstance(dados, dict):
        return []
    return [(_ROTULOS_DETALHES.get(chave, chave), _valor_legivel(chave, valor, planos))
            for chave, valor in dados.items()]


# ---------------------------------------------------------------------------
# Detalhes da empresa
# ---------------------------------------------------------------------------
@contratos_bp.route("/empresas/<int:id_empresa>/detalhes")
@admin_required
def empresa_detalhe(id_empresa):
    empresa = _empresa_ou_404(id_empresa)
    disponivel = esquema_contratual_disponivel()
    contexto = {"empresa": empresa, "esquema_disponivel": disponivel, "politica": politica_prazos(),
                "envio_ativo": envio_disponivel(current_app.config),
                "conta_acessivel": db.session.scalar(select(Empresa.id_empresa).where(
                    Empresa.id_empresa == empresa.id_empresa, *condicoes_empresa_acessivel())) is not None}
    if disponivel:
        situacao = obter_situacao_contratual(empresa)
        planos = {p.id_plano: p.nome_plano for p in db.session.scalars(select(Plano))}
        eventos = db.session.scalars(select(EventoContratual).where(
            EventoContratual.id_empresa == empresa.id_empresa,
        ).order_by(EventoContratual.ocorrido_em.desc(), EventoContratual.id_evento.desc())
          .limit(200)).all()
        contexto.update(
            situacao=situacao,
            # No regime legado a situação não carrega o contrato: as ações e o
            # resumo usam sempre o contrato vigente registrado.
            contrato_vigente=situacao.contrato or obter_contrato_aplicavel(empresa.id_empresa),
            contratos=db.session.scalars(select(Contrato).where(
                Contrato.id_empresa == empresa.id_empresa,
            ).order_by(Contrato.id_contrato.desc())).all(),
            coberturas=obter_coberturas_pagas(empresa.id_empresa, incluir_revogadas=True),
            eventos=[(e, _detalhes_legiveis(e.detalhes, planos)) for e in eventos],
            rotulos_evento=ROTULOS_EVENTO,
        )
    return render_template("admin/empresa_detalhe.html", **contexto)


# ---------------------------------------------------------------------------
# Contratação, renovação e alteração de plano
# ---------------------------------------------------------------------------
def _form_contrato(empresa, modo, contrato=None):
    if not _exigir_esquema():
        return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
    if request.method == "POST":
        def operar():
            dados = _dados_contrato()
            justificativa = request.form.get("justificativa")
            if modo == "novo":
                return operacoes.criar_contrato(db.session, empresa.id_empresa, dados,
                                                responsavel=current_user, justificativa=justificativa)
            return operacoes.renovar_contrato(
                db.session, empresa.id_empresa, contrato.id_contrato, _versao(), dados,
                responsavel=current_user, justificativa=justificativa,
                alteracao_plano=(modo == "plano"),
            )
        mensagens = {"novo": "Contrato registrado.", "renovar": "Renovação registrada; o contrato anterior foi preservado como encerrado.",
                     "plano": "Alteração de plano registrada; o contrato anterior foi preservado como encerrado."}
        ok, resultado = _executar(operar, mensagens[modo], f"contrato {modo}")
        if ok:
            return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
        flash(resultado, "danger")
    return render_template("admin/contrato_form.html", empresa=empresa, contrato=contrato,
                           modo=modo, planos=_planos(), form=request.form,
                           politica=politica_prazos())


@contratos_bp.route("/empresas/<int:id_empresa>/contratos/novo", methods=["GET", "POST"])
@admin_required
def contrato_novo(id_empresa):
    return _form_contrato(_empresa_ou_404(id_empresa), "novo")


@contratos_bp.route("/empresas/<int:id_empresa>/contratos/<int:id_contrato>/renovar", methods=["GET", "POST"])
@admin_required
def contrato_renovar(id_empresa, id_contrato):
    empresa = _empresa_ou_404(id_empresa)
    return _form_contrato(empresa, "renovar", _contrato_ou_404(empresa, id_contrato))


@contratos_bp.route("/empresas/<int:id_empresa>/contratos/<int:id_contrato>/alterar-plano", methods=["GET", "POST"])
@admin_required
def contrato_alterar_plano(id_empresa, id_contrato):
    empresa = _empresa_ou_404(id_empresa)
    return _form_contrato(empresa, "plano", _contrato_ou_404(empresa, id_contrato))


# ---------------------------------------------------------------------------
# Suspensão, prorrogação, reativação e cancelamento
# ---------------------------------------------------------------------------
@contratos_bp.route("/empresas/<int:id_empresa>/contratos/<int:id_contrato>/<acao>", methods=["GET", "POST"])
@admin_required
def contrato_acao(id_empresa, id_contrato, acao):
    if acao not in ACOES_CONTRATO:
        abort(404)
    empresa = _empresa_ou_404(id_empresa)
    contrato = _contrato_ou_404(empresa, id_contrato)
    if not _exigir_esquema():
        return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
    if request.method == "POST":
        def operar():
            versao = _versao()
            texto = request.form.get("justificativa")
            if acao == "suspender":
                return operacoes.suspender_contrato(
                    db.session, empresa.id_empresa, contrato.id_contrato, versao, motivo=texto,
                    responsavel=current_user, suspenso_em=_instante_local("suspenso_em"),
                )
            if acao == "prorrogar":
                return operacoes.registrar_prorrogacao(
                    db.session, empresa.id_empresa, contrato.id_contrato, versao,
                    novo_limite=_instante_local("novo_limite", obrigatorio=True),
                    novo_limite_pdf=_instante_local("novo_limite_pdf"),
                    autorizacao_pdf=request.form.get("autorizacao_pdf") == "1",
                    justificativa=texto, responsavel=current_user,
                )
            if acao == "reativar":
                return operacoes.reativar_contrato(
                    db.session, empresa.id_empresa, contrato.id_contrato, versao,
                    justificativa=texto, responsavel=current_user,
                )
            if request.form.get("confirmacao") != "CANCELAR":
                raise operacoes.ErroContratual("Digite CANCELAR para confirmar o cancelamento.")
            return operacoes.cancelar_contrato(
                db.session, empresa.id_empresa, contrato.id_contrato, versao,
                motivo=texto, responsavel=current_user,
            )
        mensagens = {
            "suspender": "Suspensão registrada; os prazos foram calculados pela política vigente.",
            "prorrogar": "Prorrogação registrada no histórico.",
            "reativar": "Contrato reativado.",
            "cancelar": "Contrato cancelado; as sessões dos usuários da empresa foram revogadas.",
        }
        ok, resultado = _executar(operar, mensagens[acao], f"contrato {acao}")
        if ok:
            return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
        flash(resultado, "danger")
    return render_template("admin/contrato_acao.html", empresa=empresa, contrato=contrato,
                           acao=acao, titulo=ACOES_CONTRATO[acao], form=request.form,
                           politica=politica_prazos())


# ---------------------------------------------------------------------------
# Coberturas pagas
# ---------------------------------------------------------------------------
@contratos_bp.route("/empresas/<int:id_empresa>/contratos/<int:id_contrato>/cobertura", methods=["GET", "POST"])
@admin_required
def cobertura_nova(id_empresa, id_contrato):
    empresa = _empresa_ou_404(id_empresa)
    contrato = _contrato_ou_404(empresa, id_contrato)
    if not _exigir_esquema():
        return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
    if request.method == "POST":
        def operar():
            return operacoes.registrar_cobertura(
                db.session, empresa.id_empresa, contrato.id_contrato,
                periodo_inicio=_data("periodo_inicio", obrigatoria=True),
                periodo_fim=_data("periodo_fim", obrigatoria=True),
                referencia=request.form.get("referencia"), responsavel=current_user,
            )
        ok, resultado = _executar(operar, "Período pago registrado.", "cobertura paga")
        if ok:
            return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
        flash(resultado, "danger")
    return render_template("admin/cobertura_form.html", empresa=empresa, contrato=contrato,
                           form=request.form)


@contratos_bp.route("/empresas/<int:id_empresa>/coberturas/<int:id_cobertura>/revogar", methods=["GET", "POST"])
@admin_required
def cobertura_revogar(id_empresa, id_cobertura):
    empresa = _empresa_ou_404(id_empresa)
    if not _exigir_esquema():
        return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
    cobertura = db.session.scalar(select(CoberturaPaga).where(
        CoberturaPaga.id_cobertura == id_cobertura, CoberturaPaga.id_empresa == empresa.id_empresa,
    ))
    if cobertura is None:
        abort(404)
    if request.method == "POST":
        def operar():
            return operacoes.revogar_cobertura(
                db.session, empresa.id_empresa, cobertura.id_cobertura,
                motivo=request.form.get("justificativa"), responsavel=current_user,
            )
        ok, resultado = _executar(operar, "Cobertura revogada; o registro foi preservado no histórico.",
                                  "revogação de cobertura")
        if ok:
            return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
        flash(resultado, "danger")
    return render_template("admin/contrato_acao.html", empresa=empresa, contrato=None,
                           cobertura=cobertura, acao="revogar_cobertura",
                           titulo="Revogar cobertura paga", form=request.form,
                           politica=politica_prazos())


# ---------------------------------------------------------------------------
# Habilitação gradual das regras
# ---------------------------------------------------------------------------
@contratos_bp.route("/empresas/<int:id_empresa>/regras-contratuais/<acao>", methods=["GET", "POST"])
@admin_required
def regras_contratuais(id_empresa, acao):
    if acao not in ACOES_REGRAS:
        abort(404)
    empresa = _empresa_ou_404(id_empresa)
    if not _exigir_esquema():
        return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
    if request.method == "POST":
        funcao = (operacoes.ativar_regras_contratuais if acao == "ativar"
                  else operacoes.desativar_regras_contratuais)
        ok, resultado = _executar(
            lambda: funcao(db.session, empresa.id_empresa,
                           justificativa=request.form.get("justificativa"), responsavel=current_user),
            "Regras contratuais ativadas para esta empresa." if acao == "ativar"
            else "Regras contratuais desativadas: a empresa voltou ao regime legado.",
            f"regras {acao}",
        )
        if ok:
            return redirect(url_for("contratos.empresa_detalhe", id_empresa=empresa.id_empresa))
        flash(resultado, "danger")
    return render_template("admin/contrato_acao.html", empresa=empresa, contrato=None,
                           acao=f"regras_{acao}", titulo=ACOES_REGRAS[acao], form=request.form,
                           politica=politica_prazos())


# ---------------------------------------------------------------------------
# Visão geral e reconciliação
# ---------------------------------------------------------------------------
@contratos_bp.route("/contratos")
@admin_required
def visao_geral():
    if not esquema_contratual_disponivel():
        return render_template("admin/contratos.html", esquema_disponivel=False)
    painel = operacoes.visao_geral_contratual(db.session)
    simulacao = operacoes.reconciliar_vencimentos(db.session, aplicar=False)
    lembretes = configuracoes_conta.enviar_lembretes_renovacao(aplicar=False)
    db.session.rollback()  # leitura pura; nada a confirmar
    return render_template("admin/contratos.html", esquema_disponivel=True, painel=painel,
                           simulacao=simulacao, lembretes=lembretes)


@contratos_bp.route("/contratos/lembretes-renovacao", methods=["POST"])
@admin_required
# Cada execução pode enviar um e-mail por pessoa, de forma síncrona: sem clique em série.
@limiter.limit("10 per hour", key_func=chave_limite_usuario)
def lembretes_renovacao():
    """Mesma regra de `flask contratos lembretes-renovacao --aplicar`: uma vez por pessoa e por
    data de término, respeitando quem desligou o aviso. Não há agendador configurado."""
    if not esquema_contratual_disponivel():
        return redirect(url_for("contratos.visao_geral"))
    if not envio_disponivel(current_app.config):
        flash("O envio de e-mail está indisponível: nenhum lembrete foi registrado. Confira o Diagnóstico de e-mail.", "warning")
        return redirect(url_for("contratos.visao_geral"))
    try:
        relatorio = configuracoes_conta.enviar_lembretes_renovacao(aplicar=True)
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Lembretes de renovação indisponíveis: %s", type(erro).__name__)
        flash("Não foi possível concluir o envio dos lembretes. Tente novamente.", "danger")
        return redirect(url_for("contratos.visao_geral"))
    flash(f"Lembretes de renovação: {relatorio.notificados} pessoa(s) avisada(s).", "success")
    if relatorio.emails_recusados:
        # Até 10 nomes curtos: a mensagem viaja no cookie de sessão. A rodada já terminou: falha aqui não vira erro.
        try:
            nomes = list(db.session.scalars(select(Usuario.nome).where(
                Usuario.id_usuario.in_([item["id_usuario"] for item in relatorio.recusados[:10]])).order_by(Usuario.id_usuario)))
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Nomes dos endereços recusados indisponíveis: %s", type(erro).__name__)
            nomes = []
        quem = ", ".join(nome if len(nome) <= 40 else f"{nome[:39]}…" for nome in nomes)
        resto = f" e mais {len(relatorio.recusados) - 10}" if len(relatorio.recusados) > 10 else ""
        flash(f"{relatorio.emails_recusados} endereço(s) recusado(s) em definitivo: essas pessoas ficam só com o sininho. "
              f"Corrija o e-mail delas em Empresas, Detalhes{f': {quem}{resto}' if quem else ''}.", "warning")
    if relatorio.emails_nao_aceitos:
        flash(f"{relatorio.emails_nao_aceitos} e-mail(s) não foram aceitos: a próxima execução tenta de novo. Se continuar, confira o Diagnóstico de e-mail.", "warning")
    return redirect(url_for("contratos.visao_geral"))


@contratos_bp.route("/contratos/reconciliar", methods=["POST"])
@admin_required
def reconciliar():
    if not _exigir_esquema():
        return redirect(url_for("contratos.visao_geral"))
    try:
        relatorio = operacoes.reconciliar_vencimentos(db.session, aplicar=True, acionado_por=current_user)
        revogacoes = _revogacoes_socket(relatorio.revogados)
        db.session.commit()
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Reconciliação contratual indisponível: %s", type(erro).__name__)
        flash("Não foi possível concluir a reconciliação. Nenhuma alteração foi gravada.", "danger")
        return redirect(url_for("contratos.visao_geral"))
    if relatorio.bloqueada:
        flash("Outra reconciliação está em andamento. Tente novamente em instantes.", "warning")
    else:
        _desconectar_revogados(revogacoes, "reconciliação contratual")
        flash(f"Reconciliação concluída: {len(relatorio.cancelados)} contrato(s) cancelado(s) "
              f"de {len(relatorio.candidatos)} vencido(s).", "success")
    return redirect(url_for("contratos.visao_geral"))
