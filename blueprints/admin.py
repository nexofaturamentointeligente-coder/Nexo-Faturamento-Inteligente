import os
import hashlib
import secrets
from io import BytesIO
from contextlib import ExitStack
from datetime import datetime, timezone
from functools import wraps

from PIL import Image, UnidentifiedImageError
from flask import Blueprint, render_template, redirect, url_for, flash, current_app, request, abort, make_response
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from blueprints.auth import corrigir_email_pelo_admin, limite_reautenticacao, trocar_senha_autenticada
import configuracoes_conta
from extensions import chave_limite_usuario, db, limiter
from horario import agora_utc
from models import (
    Analise, UploadRelatorio, Empresa, Plano, Segmento, Usuario, RelatorioAnalise,
    ChamadoSuporte, MensagemSuporte, GuiaTopico, ProdutoCurvaABC, Notificacao,
    CoberturaPaga, Contrato, EventoContratual, RegimeContratual,
)
from etl_processor import processar_arquivos_analise, ResultadoETL, STATUS_PDV_DIVERGENTE
import validacao_pdv
from etl_inputs import ETLInputError
from etl_limits import get_limits
from etl_uploads import ler_upload_validado, limitar_upload, limitar_processamento
from notifications import notificar_admins, notificar_clientes_empresa
from emails import (
    CATEGORIAS, CATEGORIAS_RESULTADO_INCERTO, EVENTOS_FALHA_ENTREGA, EstadoEnvio, consultar_entrega_brevo,
    diagnostico_configuracao, email_analise_publicada, email_boas_vindas, email_lembrete_aceite,
    email_teste_configuracao, email_ticket_resolvido, envio_disponivel, historico_envios, ultima_conexao_smtp,
    verificar_conexao_smtp,
)
import termos_legais
from contas_email import email_em_uso, email_valido, normalizar_email
from sqlalchemy.exc import IntegrityError, DataError, SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError
from company_security import (
    STATUS_CONTA, aplicar_status_conta, bloquear_empresa, bloquear_usuarios_empresa,
    excluir_empresa as excluir_empresa_segura,
    restaurar_empresa as restaurar_empresa_segura,
)
from session_security import STATUS_CONTA_SEM_ACESSO, usuario_pode_acessar
from origem_publica import url_publica
from contract_policy import (
    contrato_da_vigencia, esquema_contratual_disponivel, motivo_bloqueio_nova_analise,
    obter_contrato_aplicavel,
    obter_situacao_contratual, pode_consultar_analise, regime_contratual_ativo,
)
import storage

# Categorias de ticket (token persistido -> rótulo exibido) — espelha cliente.py.
CATEGORIAS_TICKET = {
    "FINANCEIRO": "Financeiro",
    "DUVIDA_TECNICA": "Dúvida Técnica",
    "ERRO_INTEGRACAO": "Erro de Integração",
}
STATUS_CHAMADO = ("ABERTO", "EM_ATENDIMENTO", "RESOLVIDO")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(f):
    """Decorator: bloqueia rota para qualquer não-ADMIN (espelha cliente_required)."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


@admin_bp.route("/dashboard")
@admin_required
def dashboard():
    # --- Contagem de empresas por status_conta (ATIVA/SUSPENSA/CANCELADA) ---
    contagem_status = dict(
        db.session.execute(
            select(Empresa.status_conta, func.count(Empresa.id_empresa))
            .where(Empresa.deletado_em.is_(None))
            .group_by(Empresa.status_conta)
        ).all()
    )
    ativas = int(contagem_status.get("ATIVA", 0))
    suspensas = int(contagem_status.get("SUSPENSA", 0))
    canceladas = int(contagem_status.get("CANCELADA", 0))
    total_emp = ativas + suspensas + canceladas

    # --- Receita mensal estimada das empresas ATIVAS: valor CONTRATADO quando
    #     há contrato ATIVO registrado; senão, preço de catálogo do plano atual. ---
    contratual = esquema_contratual_disponivel()
    base_receita = (
        select(func.coalesce(func.sum(Plano.valor_mensal), 0))
        .select_from(Empresa)
        .join(Plano, Empresa.id_plano_atual == Plano.id_plano)
        .where(Empresa.status_conta == "ATIVA")
        .where(Empresa.deletado_em.is_(None))
    )
    if contratual:
        base_receita = base_receita.where(~select(Contrato.id_contrato).where(
            Contrato.id_empresa == Empresa.id_empresa, Contrato.situacao == "ATIVO",
        ).exists())
    receita = db.session.execute(base_receita).scalar() or 0
    alertas_contratuais = None
    if contratual:
        receita += db.session.execute(
            select(func.coalesce(func.sum(Contrato.valor_mensal_contratado), 0))
            .join(Empresa, Empresa.id_empresa == Contrato.id_empresa)
            .where(Contrato.situacao == "ATIVO")
            .where(Empresa.status_conta == "ATIVA")
            .where(Empresa.deletado_em.is_(None))
        ).scalar() or 0
        from contract_admin import visao_geral_contratual
        painel = visao_geral_contratual(db.session)
        # Prazos e reconciliação valem só no regime contratual. A suspensão de uma
        # empresa ainda no legado, já vencida, é pendência de conciliação ("atenção").
        expiradas_legado = sum(1 for i in painel["suspensoes_expiradas"] if not i["contratual"])
        alertas_contratuais = {
            "suspensoes_expiradas": len(painel["suspensoes_expiradas"]) - expiradas_legado,
            "suspensoes_terminando": sum(1 for i in painel["suspensoes_terminando"] if i["contratual"]),
            "vencendo": len(painel["vencendo"]),
            "atencao": (len(painel["vencidos"]) + len(painel["inconsistentes"])
                        + len(painel["sem_contrato"]) + expiradas_legado),
            "legado": len(painel["legado"]),
        }

    stats = {
        "total": total_emp,
        "ativas": ativas,
        "suspensas": suspensas,
        "canceladas": canceladas,
        "receita_estimada": float(receita),
    }

    # --- Empresas por plano (para o gráfico) ---
    contagem_plano = dict(
        db.session.execute(
            select(Plano.nome_plano, func.count(Empresa.id_empresa))
            .select_from(Empresa)
            .join(Plano, Empresa.id_plano_atual == Plano.id_plano)
            .where(Empresa.deletado_em.is_(None))
            .group_by(Plano.nome_plano)
        ).all()
    )

    # --- Análises por status (para o gráfico) ---
    contagem_analises = dict(
        db.session.execute(
            select(Analise.status_analise, func.count(Analise.id_analise))
            .group_by(Analise.status_analise)
        ).all()
    )

    dashboard_data = {
        "empresas_status": {
            "labels": ["Ativas", "Suspensas", "Canceladas"],
            "values": [ativas, suspensas, canceladas],
        },
        "empresas_plano": {
            "labels": ["BRONZE", "PRATA", "OURO"],
            "values": [int(contagem_plano.get(k, 0)) for k in ("BRONZE", "PRATA", "OURO")],
        },
        "analises_status": {
            "labels": ["Aguardando relatório", "Relatório recebido", "Em análise", "Concluído"],
            "values": [
                int(contagem_analises.get(k, 0))
                for k in ("AGUARDANDO_RELATORIO", "RELATORIO_RECEBIDO", "EM_ANALISE", "CONCLUIDO")
            ],
        },
    }

    # --- Últimas análises (com empresa carregada para evitar N+1 no template) ---
    analises = db.session.execute(
        select(Analise)
        .order_by(Analise.data_criacao.desc(), Analise.id_analise.desc())
        .limit(6)
    ).scalars().all()

    # --- Fila de homologação: análises com anexos enviados pelo CLIENTE
    #     (id_usuario_admin IS NULL) ainda PENDENTES de validação/ETL. ---
    ids_pendentes = db.session.execute(
        select(UploadRelatorio.id_analise)
        .where(UploadRelatorio.id_usuario_admin.is_(None))
        .where(UploadRelatorio.status_processamento == "PENDENTE")
        .group_by(UploadRelatorio.id_analise)
        .order_by(UploadRelatorio.id_analise)  # Postgres não garante ordem no GROUP BY
    ).scalars().all()

    homologacoes = []
    for id_a in ids_pendentes:
        analise_p = db.session.get(Analise, id_a)
        if analise_p is None:
            continue
        up_v = _buscar_upload(id_a, "VENDAS")
        up_c = _buscar_upload(id_a, "COMPRAS")
        homologacoes.append({
            "analise": analise_p,
            "tem_vendas": up_v is not None,
            "tem_compras": up_c is not None,
            "pronto": up_v is not None and up_c is not None,
        })

    return render_template(
        "admin/dashboard.html",
        stats=stats,
        analises=analises,
        dashboard_data=dashboard_data,
        homologacoes=homologacoes,
        alertas_contratuais=alertas_contratuais,
        divergencias_pdv=validacao_pdv.divergentes(),
        termos_atrasados=termos_legais.contar_pendencias_atrasadas(),
        termos_dias_alerta=current_app.config["TERMOS_ALERTA_PENDENCIA_DIAS"],
    )


@admin_bp.route("/empresas")
@admin_required
def empresas():
    """Listagem da carteira de empresas clientes ATIVAS (exclui a lixeira)."""
    rows = db.session.execute(
        select(Empresa)
        .where(Empresa.deletado_em.is_(None))
        .order_by(Empresa.razao_social.asc())
    ).scalars().all()
    # Contagem para o badge do link "Lixeira" na própria tela.
    na_lixeira = db.session.execute(
        select(func.count(Empresa.id_empresa)).where(Empresa.deletado_em.is_not(None))
    ).scalar() or 0
    return render_template("admin/empresas.html", empresas=rows, na_lixeira=na_lixeira)


@admin_bp.route("/analises")
@admin_required
def analises():
    """Listagem global de análises (todas as empresas, ordenadas por data_criacao desc)."""
    # Tiebreak por id_analise para ordem determinística mesmo quando
    # várias análises foram criadas no mesmo segundo (resolução de CURRENT_TIMESTAMP).
    rows = db.session.execute(
        select(Analise).order_by(Analise.data_criacao.desc(), Analise.id_analise.desc())
    ).scalars().all()
    empresas_filtro = db.session.execute(
        select(Empresa)
        .where(Empresa.deletado_em.is_(None))
        .order_by(Empresa.razao_social.asc())
    ).scalars().all()
    # ?empresa=<id> (botão "Análises" da lista de empresas) pré-seleciona o filtro
    # da página; o filtro em si continua no navegador, sem consulta extra.
    empresa_selecionada = request.args.get("empresa", "")
    if not empresa_selecionada.isdigit():
        empresa_selecionada = ""
    return render_template("admin/analises.html", analises=rows, empresas=empresas_filtro,
                           empresa_selecionada=empresa_selecionada)


@admin_bp.route("/analises/nova", methods=["GET", "POST"])
@admin_required
def analise_nova():
    """
    Abertura de nova análise. Regras de negócio:
      - Apenas empresas ATIVAS podem receber novas análises.
      - QUINZENAL exige plano OURO + quinzena_referencia em {1, 2}.
      - MENSAL exige quinzena_referencia = NULL.
      - id_plano_referencia é congelado a partir do plano atual da empresa
        (snapshot histórico — futuras trocas de plano não afetam esta análise).
      - status_analise inicia em AGUARDANDO_RELATORIO (default do schema).
    """
    empresas = db.session.execute(
        select(Empresa)
        .where(Empresa.deletado_em.is_(None))
        .order_by(Empresa.razao_social.asc())
    ).scalars().all()

    if request.method == "POST":
        form = request.form

        try:
            id_empresa = int(form["id_empresa"])
        except (KeyError, ValueError):
            flash("Selecione uma empresa válida.", "danger")
            return redirect(url_for("admin.analise_nova"))

        # Lock da empresa até o COMMIT: suspensão ou cancelamento concorrente
        # espera esta decisão (mesma ordem Empresa -> filhos dos demais fluxos).
        empresa = bloquear_empresa(db.session, id_empresa)
        if empresa is None:
            flash("Empresa não encontrada.", "danger")
            return redirect(url_for("admin.analise_nova"))
        if empresa.status_conta != "ATIVA":
            flash(f"Empresa em status {empresa.status_conta} — novas análises bloqueadas.", "danger")
            return redirect(url_for("admin.analise_nova"))
        situacao = obter_situacao_contratual(empresa, carregar_coberturas=False)

        tipo_analise = form.get("tipo_analise", "").strip().upper()
        if tipo_analise not in ("MENSAL", "QUINZENAL"):
            flash("Tipo de análise inválido.", "danger")
            return redirect(url_for("admin.analise_nova"))

        quinzena_ref = None
        if tipo_analise == "QUINZENAL":
            plano_nome = empresa.plano_atual.nome_plano if empresa.plano_atual else None
            # No regime contratual vale o plano do contrato que rege o período
            # (conferido depois das datas); no legado, o plano atual.
            if plano_nome != "OURO" and not situacao.contratual:
                flash("Análises QUINZENAIS estão disponíveis apenas para empresas do plano OURO.", "danger")
                return redirect(url_for("admin.analise_nova"))
            try:
                quinzena_ref = int(form.get("quinzena_referencia", ""))
            except ValueError:
                flash("Quinzena de referência inválida.", "danger")
                return redirect(url_for("admin.analise_nova"))
            if quinzena_ref not in (1, 2):
                flash("Quinzena de referência deve ser 1 ou 2.", "danger")
                return redirect(url_for("admin.analise_nova"))

        try:
            periodo_inicio = datetime.strptime(form["periodo_inicio"].strip(), "%Y-%m-%d").date()
            periodo_fim = datetime.strptime(form["periodo_fim"].strip(), "%Y-%m-%d").date()
        except (KeyError, ValueError):
            flash("Datas do período inválidas (use o seletor de data).", "danger")
            return redirect(url_for("admin.analise_nova"))
        if periodo_inicio > periodo_fim:
            flash("Período inválido: início é posterior ao fim.", "danger")
            return redirect(url_for("admin.analise_nova"))
        # Regime contratual: empresa ATIVA e período na vigência efetiva de um
        # contrato não cancelado; o plano de referência é o desse contrato.
        bloqueio = motivo_bloqueio_nova_analise(situacao, periodo_inicio, periodo_fim)
        if bloqueio:
            flash(bloqueio, "danger")
            return redirect(url_for("admin.analise_nova"))
        plano_referencia = empresa.id_plano_atual
        if situacao.contratual:
            contrato_periodo = contrato_da_vigencia(id_empresa, periodo_inicio, periodo_fim)
            plano_referencia = contrato_periodo.id_plano
            if tipo_analise == "QUINZENAL" and contrato_periodo.plano.nome_plano != "OURO":
                flash("Análises QUINZENAIS estão disponíveis apenas para empresas do plano OURO.", "danger")
                return redirect(url_for("admin.analise_nova"))

        try:
            mes_ref = int(form["mes_referencia"])
            ano_ref = int(form["ano_referencia"])
        except (KeyError, ValueError):
            flash("Mês/ano de referência inválidos.", "danger")
            return redirect(url_for("admin.analise_nova"))
        if not (1 <= mes_ref <= 12):
            flash("Mês de referência deve estar entre 1 e 12.", "danger")
            return redirect(url_for("admin.analise_nova"))
        if not (2000 <= ano_ref <= 2100):
            flash("Ano de referência deve estar entre 2000 e 2100.", "danger")
            return redirect(url_for("admin.analise_nova"))

        nova = Analise(
            id_empresa=id_empresa,
            # Snapshot do plano: o do contrato que rege o período no regime
            # contratual, senão o plano atual da empresa (comportamento legado).
            id_plano_referencia=plano_referencia,
            id_usuario_admin_responsavel=current_user.id_usuario,
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_fim,
            mes_referencia=mes_ref,
            ano_referencia=ano_ref,
            tipo_analise=tipo_analise,
            quinzena_referencia=quinzena_ref,
            # status_analise: default 'AGUARDANDO_RELATORIO' (DB)
        )
        try:
            db.session.add(nova)
            db.session.commit()
        except (IntegrityError, DataError) as e:
            db.session.rollback()
            current_app.logger.warning("Falha ao criar análise: %s", type(e.orig or e).__name__)
            flash("Não foi possível criar a análise (dados inválidos ou duplicados). Revise e tente novamente.", "danger")
            return redirect(url_for("admin.analise_nova"))

        flash(
            f"Análise #{nova.id_analise} criada para {empresa.nome_fantasia or empresa.razao_social}.",
            "success",
        )
        return redirect(url_for("admin.analise_upload", id_analise=nova.id_analise))

    return render_template("admin/analise_form.html", empresas=empresas)


def _planos_segmentos():
    """Listas auxiliares para os <select> do formulário de empresa."""
    segmentos = db.session.execute(
        select(Segmento).where(Segmento.ativo).order_by(Segmento.nome_segmento)
    ).scalars().all()
    planos = db.session.execute(
        select(Plano).where(Plano.ativo).order_by(Plano.valor_mensal)
    ).scalars().all()
    return planos, segmentos


def _revogacoes_socket(usuarios):
    """Antes do COMMIT: impressão nova e a da geração anterior de cada revogado."""
    from session_security import impressao_sessao, impressao_credencial_sessao
    return [
        (usuario.id_usuario, impressao_sessao(usuario), impressao_credencial_sessao(
            usuario.id_usuario, usuario.senha_hash, usuario.geracao_autorizacao - 1,
        )) for usuario in usuarios
    ]


def _desconectar_revogados(revogacoes, operacao: str) -> None:
    # Fechar conexões locais é best-effort após COMMIT. Geração, autorização
    # pelo banco e salas continuam protegendo os demais processos/conexões.
    try:
        from realtime import revogar_conexoes_usuario
        for id_usuario, impressao_atual, impressao_anterior in revogacoes:
            revogar_conexoes_usuario(
                id_usuario, impressao_atual, impressao_revogada=impressao_anterior,
            )
    except Exception as erro:
        current_app.logger.warning("Revogação Socket.IO após %s indisponível (%s).", operacao, type(erro).__name__)


# Grupos de falha para as telas de operação. Só se afirma "ajuste de
# configuração" quando a causa é mesmo de configuração; o resto é neutro e
# aponta o Diagnóstico de e-mail (a causa técnica fica lá e no log).
_FALHAS_DO_DESTINATARIO = frozenset({"sem_destinatario", "destinatario_invalido", "smtp_destinatario_recusado"})
_FALHAS_TEMPORARIAS = frozenset({"provedor_indisponivel", "rede", "conexao_sem_resposta",
                                 "smtp_login_temporario"})
_FALHAS_DE_CONFIGURACAO = frozenset({
    "transporte_desativado", "remetente_invalido", "configuracao_invalida", "credencial_rejeitada",
    "ip_nao_autorizado", "conta_em_validacao", "conta_sem_permissao", "creditos_insuficientes",
    "remetente_nao_autorizado", "dominio_nao_autenticado", "tls", "smtp_autenticacao", "smtp_senha_de_app",
    "smtp_remetente_recusado", "smtp_conexao", "smtp_nao_suportado", "smtp_valor_invalido",
})


def _flash_envio_email(envio, descricao: str, *, orientar_reenvio: bool = False, tecnico: bool = False) -> None:
    """Mensagem VERDADEIRA ao admin sobre um envio de e-mail.

    Distingue aceite do provedor, agendamento, serviço desativado, resultado
    incerto e falha. Nas telas de operação (cadastro, reenvio) o texto é
    profissional, sem variáveis de ambiente nem detalhes do provedor: a causa
    técnica fica no Diagnóstico de e-mail e no log, ligados pela referência.
    `tecnico=True` (envio de teste no próprio Diagnóstico) mostra a causa.
    Nunca mostra link, token, chave ou mensagem do provedor.
    """
    estado = getattr(envio, "estado", None)
    if estado is None:  # retorno booleano legado (ex.: dublês de teste)
        estado = EstadoEnvio.AGENDADO if envio else EstadoEnvio.FALHA_NO_ENVIO
    correlacao = getattr(envio, "correlacao", None)
    categoria = getattr(envio, "categoria", None)
    ref = f" Ref. {correlacao}." if correlacao else ""
    inicial = descricao[:1].upper() + descricao[1:]
    causa = getattr(envio, "descricao_categoria", None) or "falha no envio"
    if estado == EstadoEnvio.ACEITO_PELO_PROVEDOR:
        transporte = f" ({envio.rotulo_transporte})" if tecnico else ""
        flash(f"{inicial} foi aceito pelo serviço de e-mail{transporte}. O aceite ainda não confirma a chegada "
              f"à caixa de entrada: se a mensagem não aparecer em alguns minutos, peça que o destinatário "
              f"verifique a pasta de spam.{ref}", "success")
    elif estado == EstadoEnvio.AGENDADO:
        flash(f"{inicial} entrou na fila de envio; a confirmação do serviço de e-mail ainda não chegou "
              f"(acompanhe no Diagnóstico de e-mail).{ref}", "info")
    elif estado == EstadoEnvio.DESATIVADO:
        motivo = causa if tecnico else ("o envio de e-mails está indisponível no momento (detalhes no "
                                        "Diagnóstico de e-mail)")
        flash(f"{inicial} não foi enviado: {motivo}.{ref}", "warning")
    elif categoria in CATEGORIAS_RESULTADO_INCERTO:
        # Pode ter sido aceito: não afirmar "não enviado" nem induzir duplicata.
        detalhe = f": {causa}" if tecnico else (
            ": a confirmação do serviço de e-mail não chegou. Confira o Diagnóstico de e-mail antes de reenviar")
        flash(f"{inicial} pode não ter sido enviado{detalhe}.{ref}", "warning")
    elif tecnico:
        flash(f"{inicial} não foi enviado: {causa}.{ref}", "danger")
    elif categoria in _FALHAS_DO_DESTINATARIO:
        flash(f"{inicial} não foi enviado: o endereço de e-mail do destinatário é inválido ou não foi aceito "
              f"(confira o endereço cadastrado).{ref}", "danger")
        return
    elif categoria == "limite_excedido":
        flash(f"{inicial} não foi enviado: o limite de envios do serviço de e-mail foi atingido. Tente "
              f"novamente mais tarde.{ref}", "danger")
    elif categoria in _FALHAS_TEMPORARIAS:
        flash(f"{inicial} não foi enviado: o serviço de e-mail está temporariamente indisponível. Tente "
              f"novamente em alguns minutos.{ref}", "danger")
    elif categoria in _FALHAS_DE_CONFIGURACAO:
        flash(f"{inicial} não foi enviado: o serviço de e-mail precisa de ajuste de configuração (detalhes no "
              f"Diagnóstico de e-mail).{ref}", "danger")
    else:
        flash(f"{inicial} não foi enviado (a causa está no Diagnóstico de e-mail).{ref}", "danger")
    if orientar_reenvio and estado != EstadoEnvio.ACEITO_PELO_PROVEDOR:
        flash("Quando o envio estiver normalizado, reenvie o link em Detalhes da empresa → Acesso ao portal, ou "
              "peça ao cliente que use “Esqueci minha senha” na tela de login.", "info")


def _parse_decimal_opcional(valor: str):
    """Aceita string vazia / pt-BR / en-US para faturamento_base_mensal (opcional)."""
    if not valor or not valor.strip():
        return None
    s = valor.strip().replace(".", "").replace(",", ".") if "," in valor else valor.strip()
    try:
        return float(s)
    except ValueError:
        return None


@admin_bp.route("/empresas/nova", methods=["GET", "POST"])
@admin_required
def empresa_nova():
    """Cadastro de empresa cliente + usuário CLIENTE inicial vinculado."""
    planos, segmentos = _planos_segmentos()

    if request.method == "POST":
        form = request.form
        try:
            data_contratacao = datetime.strptime(form.get("data_contratacao", "").strip(), "%Y-%m-%d").date()
        except ValueError:
            flash("Data de contratação inválida (use o seletor de data).", "danger")
            return redirect(url_for("admin.empresa_nova"))

        # E-mails gravados na forma canônica (sem espaços, minúsculas): login e
        # recuperação de senha encontram a conta com qualquer combinação de
        # maiúsculas, e o mesmo endereço não vira duas contas.
        email_contato = normalizar_email(form.get("email_contato"))
        usuario_email = normalizar_email(form.get("usuario_email"))
        if not email_valido(email_contato) or not email_valido(usuario_email):
            flash("Informe e-mails válidos para o contato da empresa e para o usuário de acesso.", "danger")
            return redirect(url_for("admin.empresa_nova"))
        try:
            ja_cadastrado = email_em_uso(db.session, usuario_email)
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Falha ao conferir e-mail do novo usuário: %s", type(erro).__name__)
            flash("Não foi possível salvar agora (indisponibilidade do banco). Tente novamente.", "danger")
            return redirect(url_for("admin.empresa_nova"))
        if ja_cadastrado:
            db.session.rollback()
            flash("Já existe um usuário com este e-mail de acesso (a comparação ignora maiúsculas e minúsculas). "
                  "Use outro endereço.", "danger")
            return redirect(url_for("admin.empresa_nova"))

        nova_emp = Empresa(
            id_segmento=int(form["id_segmento"]),
            id_plano_atual=int(form["id_plano_atual"]),
            cnpj=form["cnpj"].strip(),
            razao_social=form["razao_social"].strip(),
            nome_fantasia=(form.get("nome_fantasia") or "").strip() or None,
            email_contato=email_contato,
            telefone_contato=(form.get("telefone_contato") or "").strip() or None,
            data_contratacao=data_contratacao,
            faturamento_base_mensal=_parse_decimal_opcional(form.get("faturamento_base_mensal", "")),
            status_conta=form.get("status_conta", "ATIVA"),
        )
        novo_user = Usuario(
            nome=form["usuario_nome"].strip(),
            email=usuario_email,
            role="CLIENTE",
            ativo=True,
        )
        # Sem senha provisória: o cliente DEFINE a senha pelo link de ativação.
        # Geramos um hash aleatório e inutilizável (senha_hash é NOT NULL) e
        # mantemos primeiro_acesso=True (default) — ninguém loga até ativar.
        novo_user.set_senha(secrets.token_urlsafe(32))
        novo_user.primeiro_acesso = True

        try:
            db.session.add(nova_emp)
            db.session.flush()
            novo_user.id_empresa = nova_emp.id_empresa
            db.session.add(novo_user)
            db.session.commit()
        except (IntegrityError, DataError) as e:
            db.session.rollback()
            current_app.logger.warning("Falha ao cadastrar empresa/usuário: %s", type(e.orig or e).__name__)
            flash("Não foi possível salvar: CNPJ ou e-mail já cadastrados (ou dados inválidos).", "danger")
            return redirect(url_for("admin.empresa_nova"))

        if nova_emp.status_conta == STATUS_CONTA_SEM_ACESSO:
            # NEXO-AJ-06.1: empresa CANCELADA não recebe token de ativação.
            flash(f"Empresa '{nova_emp.razao_social}' cadastrada como CANCELADA.", "success")
            flash("O e-mail de ativação não foi enviado: os usuários só poderão ativar o acesso depois "
                  "que a empresa for reativada, solicitando um link pela recuperação de senha ou pelo "
                  "botão “Reenviar link de ativação” em Detalhes da empresa.", "info")
            return redirect(url_for("admin.empresas"))

        # Insert confirmado → e-mail de boas-vindas com LINK SEGURO de definição
        # de senha (token de 15 min), DIRETO pelo Flask (sem webhook externo).
        # Envio SÍNCRONO (com prazo): o admin vê na hora se o provedor aceitou,
        # recusou ou se o serviço está desativado. Falha nunca desfaz o cadastro.
        razao_social = nova_emp.razao_social
        try:
            from blueprints.auth import gerar_token_definir_senha
            token = gerar_token_definir_senha(novo_user)
            # SEC-005: origem canônica configurada, nunca o Host da requisição.
            link_definir = url_publica("auth.ativar_acesso", token=token)
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("Link de ativação não gerado para o novo cadastro: %s", type(e).__name__)
            flash(f"Empresa '{razao_social}' cadastrada.", "success")
            flash("O e-mail de ativação não foi enviado: não foi possível gerar o link seguro de definição de "
                  "senha. O cliente pode solicitar um novo link em “Esqueci minha senha”.", "warning")
            return redirect(url_for("admin.empresas"))
        current_app.logger.info("Link de ativação gerado para o novo cadastro.")
        envio = email_boas_vindas(novo_user, link_definir, sincrono=True)

        flash(f"Empresa '{razao_social}' cadastrada.", "success")
        _flash_envio_email(envio, "o e-mail de ativação", orientar_reenvio=True)
        return redirect(url_for("admin.empresas"))

    return render_template("admin/empresa_form.html", empresa=None, planos=planos, segmentos=segmentos)


@admin_bp.route("/empresas/<int:id_empresa>/editar", methods=["GET", "POST"])
@admin_required
def empresa_editar(id_empresa):
    """Atualização cadastral serializada com o ciclo da empresa; CNPJ imutável."""
    try:
        empresa = (bloquear_empresa(db.session, id_empresa) if request.method == "POST"
                   else db.session.get(Empresa, id_empresa))
        if not empresa:
            db.session.rollback()
            flash("Empresa não encontrada.", "danger")
            return redirect(url_for("admin.empresas"))
        planos, segmentos = _planos_segmentos()
        # Com contrato vigente ou regras ativas, status e plano mudam só pelas
        # operações contratuais (histórico auditável), nunca por este formulário.
        governada = esquema_contratual_disponivel() and (
            regime_contratual_ativo(empresa.id_empresa)
            or obter_contrato_aplicavel(empresa.id_empresa) is not None
        )

        if request.method == "POST":
            form = request.form
            if governada and (
                    form.get("status_conta", empresa.status_conta) != empresa.status_conta
                    or form.get("id_plano_atual", str(empresa.id_plano_atual)) != str(empresa.id_plano_atual)):
                db.session.rollback()
                flash("Status e plano desta empresa são geridos pelos contratos: use Detalhes → Contratos.", "warning")
                return redirect(url_for("contratos.empresa_detalhe", id_empresa=id_empresa))
            try:
                data_contratacao = datetime.strptime(form.get("data_contratacao", "").strip(), "%Y-%m-%d").date()
            except ValueError:
                db.session.rollback()
                flash("Data de contratação inválida.", "danger")
                return redirect(url_for("admin.empresa_editar", id_empresa=id_empresa))

            email_contato = normalizar_email(form.get("email_contato"))
            if not email_valido(email_contato):
                db.session.rollback()
                flash("Informe um e-mail de contato válido.", "danger")
                return redirect(url_for("admin.empresa_editar", id_empresa=id_empresa))
            # CNPJ é readonly no formulário; ignoramos qualquer valor vindo dele.
            empresa.id_segmento = int(form["id_segmento"])
            empresa.id_plano_atual = int(form["id_plano_atual"])
            empresa.razao_social = form["razao_social"].strip()
            empresa.nome_fantasia = (form.get("nome_fantasia") or "").strip() or None
            empresa.email_contato = email_contato
            empresa.telefone_contato = (form.get("telefone_contato") or "").strip() or None
            empresa.data_contratacao = data_contratacao
            # NEXO-AJ-06.1: campo ausente preserva o status; valor fora do domínio
            # ou formulário exibido antes de outra troca de status não grava nada.
            novo_status = form.get("status_conta", empresa.status_conta)
            exibido = form.get("status_conta_exibido")
            if novo_status not in STATUS_CONTA:
                db.session.rollback()
                flash("Status da conta inválido.", "danger")
                return redirect(url_for("admin.empresa_editar", id_empresa=id_empresa))
            if exibido is not None and exibido != empresa.status_conta and novo_status != empresa.status_conta:
                db.session.rollback()
                flash("O status desta empresa foi alterado por outra operação. Recarregue a página e confira antes de salvar.", "warning")
                return redirect(url_for("admin.empresa_editar", id_empresa=id_empresa))
            empresa.faturamento_base_mensal = _parse_decimal_opcional(form.get("faturamento_base_mensal", ""))
            # Entrar ou sair de CANCELADA revoga as sessões dos CLIENTE na mesma
            # transação; as conexões locais só caem depois do COMMIT.
            revogados = aplicar_status_conta(db.session, empresa, novo_status)
            empresa.data_atualizacao = datetime.now(timezone.utc)
            revogacoes = _revogacoes_socket(revogados)
            db.session.commit()
            _desconectar_revogados(revogacoes, "troca de status")
            flash("Empresa atualizada com sucesso.", "success")
            return redirect(url_for("admin.empresas"))

        return render_template("admin/empresa_form.html", empresa=empresa, planos=planos,
                               segmentos=segmentos, governada=governada)
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Falha ao editar empresa %s: %s", id_empresa, type(erro).__name__)
        flash("Não foi possível salvar as alterações (dados inválidos ou indisponibilidade do banco).", "danger")
        return redirect(url_for("admin.empresas"))


@admin_bp.route("/usuarios/<int:id_usuario>/reenviar-ativacao", methods=["POST"])
@admin_required
# Um e-mail por clique, para um usuário específico: sem disparo em massa.
@limiter.limit("10 per hour", methods=["POST"], key_func=chave_limite_usuario)
def usuario_reenviar_ativacao(id_usuario):
    """Reenvia o link de ativação a um CLIENTE que ainda não definiu a senha.

    Mesmo token e mesma elegibilidade do cadastro (empresa fora da lixeira,
    não cancelada e com vínculo contratual). Envio síncrono: o admin vê se o
    provedor aceitou a mensagem. Útil depois de corrigir a configuração.
    """
    try:
        usuario = db.session.get(Usuario, id_usuario, populate_existing=True)
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Reenvio de ativação indisponível (%s).", type(erro).__name__)
        flash("Não foi possível consultar o usuário agora. Tente novamente.", "danger")
        return redirect(url_for("admin.empresas"))
    if usuario is None or usuario.role != "CLIENTE" or usuario.id_empresa is None:
        flash("Usuário não encontrado.", "danger")
        return redirect(url_for("admin.empresas"))
    destino = url_for("contratos.empresa_detalhe", id_empresa=usuario.id_empresa)
    if not usuario.primeiro_acesso:
        flash("Este usuário já ativou o acesso. Para trocar a senha, ele usa “Esqueci minha senha” no login.", "info")
        return redirect(destino)
    try:
        from blueprints.auth import gerar_token_definir_senha
        token = gerar_token_definir_senha(usuario)
        link_definir = url_publica("auth.ativar_acesso", token=token)
    except ValueError:
        flash("O link de ativação não foi gerado: a conta não está elegível (usuário desativado ou empresa "
              "cancelada, na lixeira ou sem contrato vigente).", "warning")
        return redirect(destino)
    except Exception as erro:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.warning("Link de ativação não gerado no reenvio (%s).", type(erro).__name__)
        flash("Não foi possível gerar o link de ativação agora. Tente novamente.", "danger")
        return redirect(destino)
    current_app.logger.info("Reenvio de ativação solicitado pelo administrador | usuario=%s", usuario.id_usuario)
    _flash_envio_email(email_boas_vindas(usuario, link_definir, sincrono=True), "o e-mail de ativação")
    return redirect(destino)


@admin_bp.route("/usuarios/<int:id_usuario>/corrigir-email", methods=["POST"])
@admin_required
# Step-up por tentativa, como na exclusão permanente: esta ação decide quem recupera a conta.
@limiter.limit("5 per 15 minutes", methods=["POST"], key_func=chave_limite_usuario)
def usuario_corrigir_email(id_usuario):
    """Corrige o e-mail de acesso de um CLIENTE e devolve a conta ao dono (ver auth.corrigir_email_pelo_admin)."""
    try:
        usuario = db.session.get(Usuario, id_usuario, populate_existing=True)
        acessivel = usuario_pode_acessar(usuario)
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Correção de e-mail indisponível (%s).", type(erro).__name__)
        flash("Não foi possível consultar o usuário agora. Tente novamente.", "danger")
        return redirect(url_for("admin.empresas"))
    if usuario is None or usuario.role != "CLIENTE" or usuario.id_empresa is None:
        flash("Usuário não encontrado.", "danger")
        return redirect(url_for("admin.empresas"))
    destino = url_for("contratos.empresa_detalhe", id_empresa=usuario.id_empresa)
    if not acessivel:
        flash("Esta conta está sem acesso ao portal (usuário desativado, empresa na lixeira, cancelada ou sem "
              "contrato vigente). Restaure ou reative antes de corrigir o e-mail.", "warning")
        return redirect(destino)
    if not current_user.check_senha(request.form.get("senha_confirmacao") or ""):
        flash("Confirme sua senha de administrador para corrigir o e-mail de acesso.", "danger")
        return redirect(destino)
    id_alvo, id_admin = usuario.id_usuario, current_user.id_usuario
    erro, envio = corrigir_email_pelo_admin(usuario, request.form.get("novo_email"), request.form.get("confirmacao_novo_email"))
    if erro:
        flash(erro, "danger")
        return redirect(destino)
    current_app.logger.info("E-mail de acesso corrigido pelo administrador | admin=%s usuario=%s", id_admin, id_alvo)
    flash("E-mail de acesso corrigido. As sessões do usuário foram encerradas e a senha anterior deixou de valer.", "success")
    if envio is None:
        flash("O link para definir a senha não foi enviado: peça ao usuário para usar “Esqueci minha senha”.", "warning")
    else:
        _flash_envio_email(envio, "o link para definir a senha")
    return redirect(destino)


@admin_bp.route("/empresa/excluir/<int:id_empresa>", methods=["POST"])
@admin_required
def empresa_excluir(id_empresa):
    """Move para a lixeira sem sobrescrever ativo; revoga a geração persistida."""
    try:
        resultado = excluir_empresa_segura(db.session, id_empresa)
        if resultado is None:
            db.session.rollback()
            flash("Empresa não encontrada (ou já está na lixeira).", "danger")
            return redirect(url_for("admin.empresas"))
        nome_emp = resultado.empresa.nome_fantasia or resultado.empresa.razao_social
        revogacoes = _revogacoes_socket(resultado.usuarios)
        db.session.commit()
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Falha ao mover empresa %s para a lixeira: %s", id_empresa, type(erro).__name__)
        flash("Não foi possível mover a empresa para a lixeira. Tente novamente.", "danger")
        return redirect(url_for("admin.empresas"))

    _desconectar_revogados(revogacoes, "exclusão")
    flash(f"Cliente '{nome_emp}' movido para a lixeira com sucesso.", "success")
    return redirect(url_for("admin.empresas"))


@admin_bp.route("/lixeira")
@admin_required
def lixeira():
    """Empresas que estão na lixeira (soft-deleted): deletado_em IS NOT NULL."""
    rows = db.session.execute(
        select(Empresa)
        .where(Empresa.deletado_em.is_not(None))
        .order_by(Empresa.deletado_em.desc())
    ).scalars().all()
    return render_template("admin/lixeira.html", empresas=rows)


@admin_bp.route("/empresa/restaurar/<int:id_empresa>", methods=["POST"])
@admin_required
def empresa_restaurar(id_empresa):
    """Restaura a empresa e seu histórico sem ativar contas ou sessões antigas."""
    try:
        resultado = restaurar_empresa_segura(db.session, id_empresa)
        if resultado is None:
            db.session.rollback()
            flash("Empresa não encontrada na lixeira.", "danger")
            return redirect(url_for("admin.lixeira"))
        nome_emp = resultado.empresa.nome_fantasia or resultado.empresa.razao_social
        contas_bloqueadas = resultado.contas_bloqueadas
        continua_cancelada = resultado.empresa.status_conta == STATUS_CONTA_SEM_ACESSO
        db.session.commit()
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Falha ao restaurar empresa %s: %s", id_empresa, type(erro).__name__)
        flash("Não foi possível restaurar a empresa. Tente novamente.", "danger")
        return redirect(url_for("admin.lixeira"))

    flash(f"Cliente '{nome_emp}' restaurado com sucesso. Usuários autorizados devem realizar novo login.", "success")
    if continua_cancelada:
        flash("A empresa continua CANCELADA: os usuários seguem sem acesso até a reativação.", "warning")
    if contas_bloqueadas:
        flash("Contas bloqueadas permaneceram bloqueadas e exigem revisão administrativa antes de qualquer reativação.", "warning")
    return redirect(url_for("admin.lixeira"))


@admin_bp.route("/empresa/excluir-permanente/<int:id_empresa>", methods=["POST"])
@admin_required
# Reautenticação por tentativa (senha errada também conta): um cookie
# sequestrado não apaga clientes em série.
@limiter.limit("5 per 15 minutes", methods=["POST"], key_func=chave_limite_usuario)
def empresa_excluir_permanente(id_empresa):
    """
    HARD DELETE — exclusão física DEFINITIVA e IRREVERSÍVEL.

    Só age sobre empresas que JÁ estão na lixeira (deletado_em IS NOT NULL),
    garantindo o duplo passo (soft → hard). Como os relationships não têm
    cascade (controle explícito do que sai do banco), removemos os filhos em
    ordem topológica, tudo na MESMA transação (segurança transacional):

      1) Por análise: ProdutoCurvaABC, UploadRelatorio (+arquivos físicos),
         IndicadorAnalise (1:1), RelatorioAnalise (1:1) → depois a Analise;
      2) Por chamado: MensagemSuporte → depois o ChamadoSuporte;
      3) Notificacao dos usuários da empresa e as dos admins que citam a empresa
         (nome e assunto digitado pelo cliente, com link para o que vai sair);
      4) Usuario(s) da empresa (sessões, preferências e aceites saem por
         ON DELETE CASCADE);
      5) a própria Empresa (o último Raio-X do PDV sai por ON DELETE CASCADE).

    Empresa com histórico contratual (contratos, períodos pagos, eventos ou
    regime) NÃO é excluída: contratos, valores e pagamentos são registros
    financeiros e de auditoria, e a política de retenção ainda depende de
    decisão (Docs/GESTAO_CONTRATUAL/05, NEXO-AJ-07.3).

    Qualquer falha de integridade → rollback atômico (nada é apagado) + aviso.
    """
    # Step-up: ação irreversível exige a senha do administrador logado, não só
    # o cookie de sessão (que pode ter sido deixado aberto ou sequestrado).
    if not current_user.check_senha(request.form.get("senha_confirmacao") or ""):
        flash("Confirme sua senha de administrador para excluir definitivamente um cliente.", "danger")
        return redirect(url_for("admin.lixeira"))
    nome_emp = "empresa"
    try:
        empresa = bloquear_empresa(db.session, id_empresa)
        if not empresa or empresa.deletado_em is None:
            db.session.rollback()
            flash("A exclusão permanente só é possível para empresas que estão na lixeira.", "danger")
            return redirect(url_for("admin.lixeira"))
        usuarios = bloquear_usuarios_empresa(db.session, id_empresa)
        nome_emp = empresa.nome_fantasia or empresa.razao_social
        if esquema_contratual_disponivel() and any(
                db.session.scalar(select(modelo.id_empresa).where(modelo.id_empresa == id_empresa).limit(1))
                is not None for modelo in (Contrato, CoberturaPaga, EventoContratual, RegimeContratual)):
            db.session.rollback()
            flash(f"'{nome_emp}' possui histórico contratual (contratos, pagamentos ou eventos). Para "
                  "preservar os registros financeiros e de auditoria, a exclusão permanente está "
                  "bloqueada; a empresa continua na lixeira.", "warning")
            return redirect(url_for("admin.lixeira"))

        # 1) Análises e tudo que depende delas.
        analises = db.session.execute(
            select(Analise).where(Analise.id_empresa == id_empresa)
        ).scalars().all()
        links_da_empresa = [url_for("admin.analise_upload", id_analise=a.id_analise) for a in analises]
        for analise in analises:
            db.session.execute(
                ProdutoCurvaABC.__table__.delete().where(
                    ProdutoCurvaABC.id_analise == analise.id_analise
                )
            )
            uploads = db.session.execute(
                select(UploadRelatorio).where(UploadRelatorio.id_analise == analise.id_analise)
            ).scalars().all()
            for up in uploads:
                # 'db:' sai nesta transação; legado em disco só após o commit (SEC-004).
                storage.remover(up.caminho_arquivo)
                db.session.delete(up)
            # A cascata leva indicador e relatório. Apagá-los também aqui faria o relatório
            # sair duas vezes, e o segundo DELETE (com controle de versão) derrubaria a exclusão.
            db.session.delete(analise)

        # 2) Chamados de suporte e suas mensagens.
        chamados = db.session.execute(
            select(ChamadoSuporte).where(ChamadoSuporte.id_empresa == id_empresa)
        ).scalars().all()
        for ch in chamados:
            links_da_empresa.append(url_for("admin.ticket_detalhe", id_chamado=ch.id_chamado))
            db.session.execute(
                MensagemSuporte.__table__.delete().where(
                    MensagemSuporte.id_chamado == ch.id_chamado
                )
            )
            db.session.delete(ch)

        # 3/4) Notificações dos usuários da empresa + os próprios usuários.
        if links_da_empresa:
            db.session.execute(
                Notificacao.__table__.delete().where(Notificacao.link_destino.in_(links_da_empresa))
            )
        ids_usuarios = [u.id_usuario for u in usuarios]
        if ids_usuarios:
            db.session.execute(
                Notificacao.__table__.delete().where(
                    Notificacao.id_usuario.in_(ids_usuarios)
                )
            )
        for u in usuarios:
            db.session.delete(u)

        # 5) A própria empresa.
        db.session.delete(empresa)

        db.session.commit()
    except SQLAlchemyError as erro:
        db.session.rollback()
        flash(
            f"Não foi possível excluir permanentemente '{nome_emp}': ainda há dados vinculados.",
            "danger",
        )
        current_app.logger.warning("Falha no hard delete da empresa %s: %s", id_empresa, type(erro).__name__)
        return redirect(url_for("admin.lixeira"))

    flash(f"Cliente '{nome_emp}' e todos os seus dados foram excluídos permanentemente.", "success")
    return redirect(url_for("admin.lixeira"))


def _buscar_upload(id_analise: int, tipo: str):
    """Consulta o upload diretamente no banco (não confia em coleção carregada em memória)."""
    return db.session.execute(
        select(UploadRelatorio).where(
            UploadRelatorio.id_analise == id_analise,
            UploadRelatorio.tipo_relatorio == tipo,
        )
    ).scalar_one_or_none()


def _resolver_caminho(upload: UploadRelatorio) -> str:
    """Caminho absoluto do arquivo salvo. Delega à camada de storage (ponto
    único para futura migração a object storage — ver storage.py)."""
    return storage.resolver(upload.caminho_arquivo)


# GET exibe a página de upload; POST recebe o arquivo. As submissões são
# protegidas por CSRF (token no form) e restritas a ADMIN (@admin_required).
@admin_bp.route("/analise/<int:id_analise>/upload", methods=["GET", "POST"])
@admin_required
@limitar_upload
def analise_upload(id_analise):
    """Upload limitado/prevalidado; a mesma trava do processamento serializa troca."""
    try:
        analise = db.session.get(Analise, id_analise)
        if not analise:
            flash("Análise não encontrada.", "danger")
            return redirect(url_for("admin.dashboard"))
        if request.method != "POST":
            return render_template("admin/upload.html", analise=analise,
                                   pdv_disponivel=validacao_pdv.esquema_validacao_pdv_disponivel(),
                                   validacao=validacao_pdv.obter(id_analise))

        tipo = (request.form.get("tipo_relatorio") or "").strip().upper()
        if tipo not in ("VENDAS", "COMPRAS"):
            flash("Tipo de relatório inválido (esperado VENDAS ou COMPRAS).", "danger")
            return redirect(url_for("admin.analise_upload", id_analise=id_analise))
        if _relatorio_publicado(analise):
            # SEC-003: os arquivos de uma análise publicada são a base do que o
            # cliente vê; a troca passa por despublicar primeiro.
            flash("Esta análise tem devolutiva publicada. Despublique-a antes de substituir os relatórios.", "warning")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
        arquivo = request.files.get("arquivo")
        if not arquivo or not arquivo.filename:
            flash("Selecione um arquivo para enviar.", "warning")
            return redirect(url_for("admin.analise_upload", id_analise=id_analise))
        nome_seguro = secure_filename(arquivo.filename)
        ext = os.path.splitext(nome_seguro)[1].lower().lstrip(".")
        if ext not in current_app.config["ALLOWED_EXTENSIONS"]:
            flash(
                f"Extensão .{ext} não suportada. Aceitos: {sorted(current_app.config['ALLOWED_EXTENSIONS'])}.",
                "danger",
            )
            return redirect(url_for("admin.analise_upload", id_analise=id_analise))
        # Trava do total do PDV: sem o valor, a troca de arquivo pelo consultor a contornaria.
        valor_pdv = None
        if tipo == "VENDAS" and validacao_pdv.esquema_validacao_pdv_disponivel():
            valor_pdv = validacao_pdv.interpretar_valor_digitado(request.form.get("valor_pdv_informado"))
        # Nenhum efeito persistente antes da leitura limitada e do preflight.
        conteudo, sha = ler_upload_validado(arquivo, ext)
        analise = db.session.get(Analise, id_analise, with_for_update=True, populate_existing=True)
        if analise is None:
            db.session.rollback()
            flash("Análise não encontrada.", "danger")
            return redirect(url_for("admin.dashboard"))
        # Publicada durante a validação do arquivo: agora, com o lock, a publicação não corre mais.
        if _publicado_no_banco(id_analise):
            db.session.rollback()
            flash("Esta análise tem devolutiva publicada. Despublique-a antes de substituir os relatórios.", "warning")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
        nome_final = f"analise_{id_analise}_{tipo}_{sha[:8]}.{ext}"
        existente = _buscar_upload(id_analise, tipo)
        caminho_final = storage.salvar(conteudo, nome_final)
        if existente and existente.caminho_arquivo and existente.caminho_arquivo != caminho_final:
            # Não remover uma referência ainda usada por outra análise.
            compartilhado = db.session.scalar(select(func.count()).select_from(UploadRelatorio).where(
                UploadRelatorio.caminho_arquivo == existente.caminho_arquivo,
                UploadRelatorio.id_upload != existente.id_upload,
            ))
            if not compartilhado:
                # 'db:' sai nesta transação; o legado em disco só depois do
                # commit válido (agendado por storage.remover, SEC-004).
                storage.remover(existente.caminho_arquivo)
        if existente:
            db.session.delete(existente)
            db.session.flush()
        db.session.add(UploadRelatorio(
            id_analise=id_analise, id_usuario_admin=current_user.id_usuario,
            tipo_relatorio=tipo, nome_arquivo_original=nome_seguro,
            caminho_arquivo=caminho_final, extensao_arquivo=ext.upper(),
            tamanho_arquivo=len(conteudo), hash_arquivo=sha,
            status_processamento="PENDENTE",
        ))
        if valor_pdv is not None:
            validacao_pdv.registrar_valor_informado(id_analise, valor_pdv, current_user.id_usuario)
        outro = _buscar_upload(id_analise, "COMPRAS" if tipo == "VENDAS" else "VENDAS")
        if outro and analise.status_analise == "AGUARDANDO_RELATORIO":
            analise.status_analise = "RELATORIO_RECEBIDO"
        db.session.commit()
        flash(f"Relatório de {tipo} enviado com sucesso.", "success")
        return redirect(url_for("admin.analise_upload", id_analise=id_analise))
    except ETLInputError as erro:
        db.session.rollback()
        flash(erro.safe_message, "danger")
        return redirect(url_for("admin.analise_upload", id_analise=id_analise))
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Upload administrativo indisponível (%s).", type(erro).__name__)
        flash("Não foi possível salvar o relatório. Tente novamente.", "danger")
        return redirect(url_for("admin.analise_upload", id_analise=id_analise))


def _publicado_no_banco(id_analise) -> bool:
    """Leitura direta, sob o lock da análise: o relacionamento pode estar em cache na sessão."""
    return bool(db.session.scalar(select(RelatorioAnalise.publicado).where(RelatorioAnalise.id_analise == id_analise)))


@admin_bp.route("/analise/<int:id_analise>/total-pdv", methods=["POST"])
@admin_required
def corrigir_total_pdv(id_analise):
    """Corrige só o total do PDV, para o erro de digitação: o portal não devolve o arquivo
    enviado, e sem isto a divergência exigiria pedi-lo de novo. Fica registrado quem digitou."""
    destino = url_for("admin.analise_upload", id_analise=id_analise)
    try:
        valor = validacao_pdv.interpretar_valor_digitado(request.form.get("valor_pdv_informado"))
        analise = db.session.get(Analise, id_analise, with_for_update=True, populate_existing=True)
        if analise is None:
            db.session.rollback()
            flash("Análise não encontrada.", "danger")
            return redirect(url_for("admin.dashboard"))
        if not validacao_pdv.esquema_validacao_pdv_disponivel():
            db.session.rollback()
            flash("A conferência do total do PDV ainda não foi instalada neste banco.", "danger")
            return redirect(destino)
        if _publicado_no_banco(id_analise):
            db.session.rollback()
            flash("Esta análise tem devolutiva publicada. Despublique-a antes de corrigir o total.", "warning")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
        if _buscar_upload(id_analise, "VENDAS") is None:
            db.session.rollback()
            flash("Envie o relatório de Vendas com o total.", "warning")
            return redirect(destino)
        validacao_pdv.registrar_valor_informado(id_analise, valor, current_user.id_usuario)
        db.session.commit()
    except ETLInputError as erro:
        db.session.rollback()
        flash(erro.safe_message, "danger")
        return redirect(destino)
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Correção do total do PDV indisponível (%s).", type(erro).__name__)
        flash("Não foi possível salvar o total. Tente novamente.", "danger")
        return redirect(destino)
    flash("Total do PDV atualizado. Processe a análise de novo para conferir.", "success")
    return redirect(destino)


def _versao_rascunho_confere(form, rel) -> bool:
    """O formulário foi aberto sobre a versão atual do rascunho (SEC-003)."""
    valor = (form.get("versao_rascunho") or "").strip()
    return valor.isdecimal() and int(valor) == rel.versao_rascunho


def _confirmar_relatorio(id_analise) -> bool:
    """COMMIT do editor: nada parcial chega ao banco (SEC-003 invariante 8).

    StaleDataError: outra sessão gravou entre a leitura e o UPDATE (controle
    otimista do version_id_col). Qualquer falha reverte texto, cópia publicada
    e status juntos; o chamador só notifica o cliente após True.
    """
    try:
        db.session.commit()
        return True
    except StaleDataError:
        db.session.rollback()
        flash("O relatório foi alterado em outra sessão enquanto você editava. "
              "Recarregue a página, revise o texto atual e tente novamente.", "warning")
    except SQLAlchemyError as e:
        db.session.rollback()
        current_app.logger.warning("Falha ao salvar relatório da análise %s: %s", id_analise,
                                   type(getattr(e, "orig", None) or e).__name__)
        flash("Não foi possível salvar o relatório. Verifique os dados e tente novamente.", "danger")
    return False


@admin_bp.route("/analise/<int:id_analise>/relatorio", methods=["GET", "POST"])
@admin_required
def analise_relatorio(id_analise):
    """
    Editor da devolutiva estratégica.

    Ações (campo `acao` do form):
      - rascunho     → upsert do relatorio_analise; mantém publicado=False; NÃO mexe no status_analise.
      - publicar     → exige análise em EM_ANALISE (pós-ETL) + todos os 6 textos; marca publicado=True
                       e dispara a ÚNICA transição válida: status_analise → CONCLUIDO + data_conclusao.
      - despublicar  → exige análise em CONCLUIDO; volta publicado=False e status_analise → EM_ANALISE.

    Esta rota é o único ponto do sistema onde CONCLUIDO pode ser aplicado (nunca o ETL).

    SEC-003: o cliente lê a CÓPIA publicada (models.RelatorioAnalise.publicada).
    Rascunho e geração por IA alteram só o texto em edição; "publicar" copia o
    rascunho revisado para a cópia, na mesma transação da mudança de status.
    O formulário devolve `versao_rascunho`: salvar por cima de uma edição mais
    nova é recusado (controle otimista, também no UPDATE via version_id_col).
    """
    analise = db.session.get(Analise, id_analise)
    if not analise:
        flash("Análise não encontrada.", "danger")
        return redirect(url_for("admin.analises"))

    if request.method == "POST":
        form = request.form
        acao = form.get("acao", "").strip().lower()
        destino = url_for("admin.analise_relatorio", id_analise=id_analise)

        if analise.relatorio is not None and not _versao_rascunho_confere(form, analise.relatorio):
            flash("O relatório foi alterado em outra sessão depois que esta tela foi aberta. "
                  "Recarregue a página, revise o texto atual e tente novamente.", "warning")
            return redirect(destino)

        # --- Geração automatizada por IA (sem digitação manual) ---
        # Preenche resumo_executivo + conclusao_estrategica a partir dos números.
        # Não depende dos campos do form e não publica — o admin revisa e publica.
        if acao == "gerar_ia":
            if not analise.indicador:
                flash("Gere os indicadores (Processar) antes de gerar a devolutiva por IA.", "warning")
                return redirect(destino)
            if validacao_pdv.conferencia_em_aberto(id_analise):
                # Os números estão sob suspeita: nem saem para o provedor de IA, nem viram rascunho.
                flash("Geração por IA bloqueada: o faturamento calculado ainda não confere com o total "
                      "informado no PDV.", "warning")
                return redirect(destino)
            from ai_devolutiva import gerar_devolutiva_ia
            ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
            rel = analise.relatorio
            if rel is None:
                rel = RelatorioAnalise(
                    id_analise=id_analise, titulo=f"Análise {ref}",
                    conclusao_estrategica="(gerando...)",
                )
                db.session.add(rel)
            else:
                rel.preservar_publicacao_legada()  # o cliente segue vendo o texto publicado
            gerado = gerar_devolutiva_ia(analise)
            rel.resumo_executivo = gerado["resumo_executivo"]
            rel.conclusao_estrategica = gerado["conclusao_estrategica"]
            rel.gerado_por_ia = (gerado["fonte"] == "ia")
            if not rel.titulo:
                rel.titulo = f"Análise {ref}"
            analise.data_atualizacao = datetime.now(timezone.utc)
            if not _confirmar_relatorio(id_analise):
                return redirect(destino)
            if gerado["fonte"] == "ia":
                flash("Devolutiva gerada por Inteligência Artificial Nexo. Revise e publique.", "success")
            else:
                flash("Rascunho gerado pelo motor local (configure ANTHROPIC_API_KEY para a geração por IA completa). Revise e publique.", "warning")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

        if acao not in ("rascunho", "publicar", "despublicar"):
            flash("Ação inválida.", "danger")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

        titulo = (form.get("titulo") or "").strip()
        resumo = (form.get("resumo_executivo") or "").strip() or None
        pontos_pos = (form.get("pontos_positivos") or "").strip() or None
        pontos_alerta = (form.get("pontos_de_alerta") or "").strip() or None
        recomendacoes = (form.get("recomendacoes") or "").strip() or None
        conclusao = (form.get("conclusao_estrategica") or "").strip()

        if not titulo:
            flash("Título é obrigatório.", "danger")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
        if not conclusao:
            flash("Conclusão estratégica é obrigatória (campo NOT NULL do relatório).", "danger")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

        # Trava do total do PDV. O mesmo lock do processamento: sem ele, uma publicação em
        # andamento gravaria CONCLUIDO por cima de uma divergência recém-apurada. Corrigir
        # o texto de uma devolutiva já publicada não muda números: reprocessar exige despublicar.
        if acao == "publicar":
            try:
                analise = db.session.get(Analise, id_analise, with_for_update=True, populate_existing=True)
                if analise is None:
                    db.session.rollback()
                    flash("Análise não encontrada.", "danger")
                    return redirect(url_for("admin.analises"))
                # O relatório pode ter nascido enquanto este pedido esperava o lock.
                if analise.relatorio is not None and not _versao_rascunho_confere(form, analise.relatorio):
                    db.session.rollback()
                    flash("O relatório foi alterado em outra sessão depois que esta tela foi aberta. "
                          "Recarregue a página, revise o texto atual e tente novamente.", "warning")
                    return redirect(destino)
                bloqueio = None if _relatorio_publicado(analise) else validacao_pdv.motivo_bloqueio_publicacao(id_analise)
            except SQLAlchemyError as erro:
                db.session.rollback()
                current_app.logger.warning("Conferência do PDV indisponível ao publicar (%s).", type(erro).__name__)
                flash("Não foi possível conferir o total do PDV. Tente novamente.", "danger")
                return redirect(destino)
            if bloqueio:
                db.session.rollback()
                current_app.logger.warning("Publicação da análise %s recusada pela trava do PDV.", id_analise)
                flash(bloqueio, "danger")
                return redirect(destino)

        rel = analise.relatorio  # uselist=False
        if rel is None:
            rel = RelatorioAnalise(id_analise=id_analise, titulo=titulo, conclusao_estrategica=conclusao)
            db.session.add(rel)
        else:
            # Relatório publicado antes de existir a cópia: congela o texto que
            # o cliente já vê ANTES de o rascunho mudar (SEC-003).
            rel.preservar_publicacao_legada()

        # Sempre atualizamos o conteúdo a partir do form (rascunho ou publicação).
        rel.titulo = titulo
        rel.resumo_executivo = resumo
        rel.pontos_positivos = pontos_pos
        rel.pontos_de_alerta = pontos_alerta
        rel.recomendacoes = recomendacoes
        rel.conclusao_estrategica = conclusao

        agora = datetime.now(timezone.utc)

        if acao == "publicar":
            # Pré-condições estritas: só publica análise que passou pelo ETL.
            if analise.status_analise not in ("EM_ANALISE", "CONCLUIDO"):
                flash(
                    "Só é possível publicar análises em EM_ANALISE ou re-publicar análises já CONCLUIDAS. "
                    f"Status atual: {analise.status_analise}.", "danger",
                )
                db.session.rollback()
                return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
            # O cliente vê apenas Resumo Executivo + Conclusão (os demais campos
            # viraram semáforos automáticos). Logo, publicar exige só esses dois —
            # ambos preenchíveis pela geração por IA, sem digitação manual.
            faltando = [n for n, v in [
                ("resumo executivo", resumo),
            ] if not v]
            if faltando:
                flash(f"Para publicar é necessário preencher: {', '.join(faltando)}.", "danger")
                db.session.rollback()
                return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
            # SEC-003/SEC-006: texto gerado por IA só é entregue ao cliente com
            # revisão humana declarada no próprio formulário de publicação.
            if rel.gerado_por_ia and form.get("revisao_confirmada") != "1":
                flash("O texto atual foi gerado por IA: confirme a revisão humana (caixa de "
                      "seleção) antes de publicar.", "danger")
                db.session.rollback()
                return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

            rel.congelar_publicacao()  # a cópia publicada nasce do rascunho revisado
            rel.publicado = True
            rel.data_publicacao = agora
            analise.status_analise = "CONCLUIDO"
            analise.data_conclusao = agora
            # NÃO disparamos notificação/e-mail aqui: efeitos colaterais externos
            # (WebSocket + SMTP) só acontecem APÓS o commit ter sucesso, para não
            # avisar o cliente de uma publicação que pode sofrer rollback.

        elif acao == "despublicar":
            if analise.status_analise != "CONCLUIDO":
                flash(
                    "Só é possível despublicar análises em CONCLUIDO. "
                    f"Status atual: {analise.status_analise}.", "danger",
                )
                db.session.rollback()
                return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
            rel.publicado = False
            rel.data_publicacao = None
            analise.status_analise = "EM_ANALISE"
            analise.data_conclusao = None

        else:  # rascunho: NÃO mexe em publicado nem em status_analise
            pass

        analise.data_atualizacao = agora

        if not _confirmar_relatorio(id_analise):
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

        # Dispatch-after-commit: só agora que a transação foi confirmada disparamos
        # os efeitos colaterais externos (sininho em tempo real + e-mail ao cliente).
        # Aviso só quando a política permite ao cliente abrir a análise: empresa
        # com acesso ao portal e, no regime contratual, período pago. Sem isso
        # o e-mail anunciaria conteúdo indisponível (ou a conta encerrada).
        avisar_cliente = False
        if acao == "publicar":
            # A publicação já está confirmada: falha ao consultar a política não
            # pode virar erro 500 (o cliente apenas não é avisado agora).
            try:
                avisar_cliente = pode_consultar_analise(obter_situacao_contratual(analise.empresa), analise)
            except Exception as e:  # noqa: BLE001
                db.session.rollback()
                current_app.logger.warning("Acesso do cliente à análise %s não verificado após publicar: %s",
                                           id_analise, type(e).__name__)
                avisar_cliente = None
        if acao == "publicar" and avisar_cliente is None:
            flash("A devolutiva foi publicada, mas não foi possível confirmar agora o acesso do cliente; "
                  "ele não foi avisado. Tente publicar novamente em instantes.", "warning")
        elif acao == "publicar" and not avisar_cliente:
            flash("A devolutiva foi publicada, mas o cliente não foi avisado: a empresa está sem "
                  "acesso ao portal ou o período da análise não tem cobertura paga registrada.", "warning")
        elif acao == "publicar":
            ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
            link_interno = url_for("cliente.analise", id_analise=analise.id_analise)
            try:
                notificar_clientes_empresa(
                    analise.id_empresa,
                    f"Seu Relatório Estratégico de {ref} já está disponível! Clique para ver.",
                    link_interno,
                )
                db.session.commit()  # persiste as notificações criadas
                email_analise_publicada(
                    analise,
                    url_publica("cliente.analise", id_analise=analise.id_analise),
                )
            except Exception as e:  # noqa: BLE001 — efeito colateral nunca derruba o fluxo
                db.session.rollback()
                current_app.logger.warning("Falha ao notificar publicação da análise %s: %s", id_analise, type(e).__name__)

        msg_acao = {"rascunho": "Rascunho salvo.", "publicar": "Devolutiva publicada para o cliente.",
                    "despublicar": "Devolutiva despublicada (análise voltou a EM_ANALISE)."}[acao]
        flash(msg_acao, "success")
        return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

    return render_template(
        "admin/relatorio_editor.html", analise=analise, validacao=validacao_pdv.obter(id_analise),
        bloqueio_pdv=None if _relatorio_publicado(analise) else validacao_pdv.motivo_bloqueio_publicacao(id_analise),
    )


def _relatorio_publicado(analise) -> bool:
    """A análise tem devolutiva publicada (o cliente vê indicadores e texto)."""
    return analise.relatorio is not None and bool(analise.relatorio.publicado)


@admin_bp.route("/analise/<int:id_analise>/processar", methods=["POST"])
@admin_required
@limitar_processamento
def processar_analise(id_analise):
    """Preserva o lock da análise até confirmar todo o resultado, inclusive erro."""
    try:
        analise = db.session.get(Analise, id_analise, with_for_update=True, populate_existing=True)
        if not analise:
            db.session.rollback()
            flash("Análise não encontrada.", "danger")
            return redirect(url_for("admin.dashboard"))
        if _relatorio_publicado(analise):
            # SEC-003: reprocessar trocaria os indicadores que o cliente já vê
            # como publicados. O fluxo explícito é despublicar → processar → publicar.
            # Verificado antes dos uploads: vale mesmo se um relatório foi removido.
            db.session.rollback()
            flash("Esta análise tem devolutiva publicada. Despublique-a antes de reprocessar os relatórios.", "warning")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
        upload_vendas = _buscar_upload(id_analise, "VENDAS")
        upload_compras = _buscar_upload(id_analise, "COMPRAS")
        if not upload_vendas or not upload_compras:
            db.session.rollback()
            flash("Ambos os relatórios (VENDAS e COMPRAS) precisam ser enviados antes de processar.", "warning")
            return redirect(url_for("admin.dashboard"))

        # Sem commit/rollback intermediário: uploads usam o mesmo lock e não
        # podem substituir os arquivos sobre os quais estes KPIs são calculados.
        analise.status_analise = "EM_ANALISE"
        agora = datetime.now(timezone.utc)
        try:
            limits = get_limits()
            with ExitStack() as materiais:
                caminho_vendas = materiais.enter_context(storage.materializar_para_etl(
                    upload_vendas.caminho_arquivo, limits.max_file_bytes,
                ))
                caminho_compras = materiais.enter_context(storage.materializar_para_etl(
                    upload_compras.caminho_arquivo, limits.max_file_bytes,
                ))
                resultado = processar_arquivos_analise(
                    caminho_vendas, caminho_compras, id_analise, db.session,
                )
        except ETLInputError as erro:
            resultado = ResultadoETL(False, erro.safe_message, {}, {}, [])
        except ValueError:
            resultado = ResultadoETL(False, "Configuração de processamento inválida.", {}, {}, [])
        if not db.session.is_active:
            # Uma falha SQL não tratada por SAVEPOINT exige rollback completo.
            # Nunca readquirir e marcar uploads que outro pedido pode substituir.
            raise SQLAlchemyError("Transação de processamento indisponível.")
        if resultado.sucesso:
            for upload in (upload_vendas, upload_compras):
                upload.status_processamento = "PROCESSADO"
                upload.data_processamento = agora
                upload.mensagem_erro = None
            # Na mesma transação dos KPIs: número novo nunca convive com conferência antiga.
            resultado = validacao_pdv.conferir_e_registrar(id_analise, resultado)
        else:
            # O orquestrador reverte apenas seu SAVEPOINT; o lock externo segue
            # mantido e os indicadores/Curva ABC anteriores permanecem intactos.
            analise.status_analise = "RELATORIO_RECEBIDO"
            for upload in (upload_vendas, upload_compras):
                upload.status_processamento = "ERRO"
                upload.mensagem_erro = resultado.mensagem
        db.session.commit()
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Processamento indisponível (%s).", type(erro).__name__)
        flash("Não foi possível confirmar o processamento. Tente novamente.", "danger")
        return redirect(url_for("admin.dashboard"))

    if not resultado.sucesso:
        flash(f"Falha no processamento: {resultado.mensagem}", "danger")
        return redirect(url_for("admin.dashboard"))
    # Apenas sucesso já confirmado pode gerar notificações de processamento.
    ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
    conferencia = resultado.telemetria.get("conferencia_pdv")
    # Sem valores: o sininho não passa pela cobertura contratual.
    aviso = (f"Encontramos uma diferença no total de vendas informado para {validacao_pdv.referencia(analise)}. "
             "A equipe NEXO está revisando."
             if conferencia and conferencia["status"] == STATUS_PDV_DIVERGENTE else
             f"Recebemos e processamos seus relatórios de {ref}. Sua devolutiva está em preparação.")
    try:
        notificar_clientes_empresa(analise.id_empresa, aviso, url_for("cliente.dashboard"))
        db.session.commit()
    except Exception as erro:
        db.session.rollback()
        current_app.logger.warning("Falha ao notificar processamento da análise %s: %s", id_analise, type(erro).__name__)
    return render_template("admin/processar_resultado.html", analise=analise, resultado=resultado,
                           pdv_disponivel=validacao_pdv.esquema_validacao_pdv_disponivel(),
                           validacao=validacao_pdv.obter(id_analise))


# =====================================================================
# Gerenciar análises por empresa (Editar → empresa → análises → exclusão)
# =====================================================================
def _empresa_gerenciavel(id_empresa):
    """Empresa ativa (fora da lixeira) ou 404: a lixeira tem o seu próprio fluxo."""
    empresa = db.session.get(Empresa, id_empresa)
    if empresa is None or empresa.deletado_em is not None:
        abort(404)
    return empresa


def _excluir_analise(analise):
    """
    Exclui UMA análise e tudo que depende dela — e nada além disso.

    A remoção das linhas-filhas (uploads, indicador, relatório e Curva ABC) é
    automática via cascade="all, delete-orphan" (models.py). O aviso aos admins sobre
    a planilha sai junto: ele cita a empresa e apontaria para o que não existe mais,
    e a exclusão permanente da empresa não teria como achá-lo depois. Arquivos dos
    uploads: os guardados no banco ('db:<chave>') saem na MESMA transação do
    commit; os legados em disco são agendados por storage.remover e só saem
    DEPOIS do commit confirmado (SEC-004), para uma falha no banco não deixar
    registros sem arquivo (falha parcial). A exclusão é definitiva (não há
    lixeira para análises).

    Devolve (ok, mensagem).
    """
    id_str = f"#{analise.id_analise}"
    periodo = f"{analise.periodo_inicio.strftime('%d/%m/%Y')} – {analise.periodo_fim.strftime('%d/%m/%Y')}"
    caminhos = [up.caminho_arquivo for up in analise.uploads if up.caminho_arquivo]
    try:
        for caminho in caminhos:
            storage.remover(caminho)  # 'db:' na transação; legado só após o commit
        link = current_app.url_map.bind("localhost").build("admin.analise_upload", {"id_analise": analise.id_analise})
        db.session.execute(Notificacao.__table__.delete().where(Notificacao.link_destino == link))
        db.session.delete(analise)  # cascata ORM apaga todos os filhos na ordem certa
        db.session.commit()
    except (IntegrityError, SQLAlchemyError) as e:
        db.session.rollback()
        current_app.logger.warning("Falha ao excluir análise %s: %s", id_str, type(getattr(e, "orig", None) or e).__name__)
        return False, f"Não foi possível excluir a análise {id_str} ({periodo}): ainda há dados vinculados. Nada foi alterado."
    return True, f"Análise {id_str} ({periodo}) excluída. As demais análises da empresa foram preservadas."


@admin_bp.route("/analises/gerenciar")
@admin_required
def analises_gerenciar():
    """Gerenciar análises, 1ª tela: seleção da empresa em cartões (sem exclusão aqui)."""
    empresas = db.session.execute(
        select(Empresa)
        .where(Empresa.deletado_em.is_(None))
        .order_by(Empresa.razao_social.asc())
    ).scalars().all()
    contagem = dict(
        db.session.execute(
            select(Analise.id_empresa, func.count(Analise.id_analise)).group_by(Analise.id_empresa)
        ).all()
    )
    return render_template("admin/analises_gerenciar.html", empresas=empresas, contagem=contagem)


@admin_bp.route("/analises/gerenciar/<int:id_empresa>")
@admin_required
def analises_empresa(id_empresa):
    """Gerenciar análises, 2ª tela: só as análises da empresa selecionada, com exclusão por linha."""
    empresa = _empresa_gerenciavel(id_empresa)
    analises = db.session.execute(
        select(Analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .order_by(Analise.periodo_inicio.desc(), Analise.id_analise.desc())
    ).scalars().all()
    return render_template("admin/analises_empresa.html", empresa=empresa, analises=analises)


@admin_bp.route("/analises/gerenciar/<int:id_empresa>/excluir/<int:id_analise>", methods=["POST"])
@admin_required
def analise_excluir(id_empresa, id_analise):
    """
    Exclui a análise <id_analise> SOMENTE se ela pertencer à empresa <id_empresa>
    selecionada no fluxo de gerenciamento. POST-only (CSRF global), admin-only,
    confirmação explícita no formulário (campo confirmar=sim). Um identificador de
    outra empresa, de empresa na lixeira ou inexistente é recusado (404) sem tocar
    em nada. Não existe exclusão em massa: uma análise por requisição.
    """
    empresa = _empresa_gerenciavel(id_empresa)
    analise = db.session.get(Analise, id_analise)
    if analise is None or analise.id_empresa != empresa.id_empresa:
        current_app.logger.warning(
            "Exclusão recusada: análise %s não pertence à empresa %s (admin %s).",
            id_analise, id_empresa, current_user.id_usuario,
        )
        abort(404)
    if request.form.get("confirmar") != "sim":
        abort(400)
    ok, mensagem = _excluir_analise(analise)
    if ok:
        # Ação irreversível: fica registrada (quem, qual análise, de qual empresa).
        current_app.logger.info(
            "Análise %s da empresa %s excluída pelo admin %s.",
            id_analise, id_empresa, current_user.id_usuario,
        )
    flash(mensagem, "success" if ok else "danger")
    return redirect(url_for("admin.analises_empresa", id_empresa=id_empresa))


# =====================================================================
# Suporte: painel central de chamados (consultor)
# =====================================================================
@admin_bp.route("/suporte")
@admin_bp.route("/tickets")  # endereço anterior: continua válido
@admin_required
def tickets():
    """
    Painel central de suporte: todos os chamados de todas as empresas,
    agrupados por status (ABERTO / EM_ATENDIMENTO / RESOLVIDO).
    """
    todos = db.session.execute(
        select(ChamadoSuporte)
        .options(selectinload(ChamadoSuporte.mensagens), selectinload(ChamadoSuporte.empresa))
        .order_by(ChamadoSuporte.data_abertura.desc(), ChamadoSuporte.id_chamado.desc())
    ).scalars().all()

    grupos = {s: [] for s in STATUS_CHAMADO}
    for c in todos:
        grupos.setdefault(c.status_chamado, []).append(c)

    return render_template(
        "admin/tickets.html",
        grupos=grupos, categorias=CATEGORIAS_TICKET, total=len(todos),
    )


@admin_bp.route("/tickets/<int:id_chamado>", methods=["GET", "POST"])
@admin_required
def ticket_detalhe(id_chamado):
    """
    Conversa de um chamado. Ações (campo `acao`):
      - responder      → adiciona mensagem do consultor (+ EM_ATENDIMENTO se ABERTO)
                         e notifica os clientes da empresa.
      - alterar_status → muda status_chamado (ABERTO/EM_ATENDIMENTO/RESOLVIDO).
    """
    chamado = db.session.get(ChamadoSuporte, id_chamado)
    if chamado is None:
        flash("Chamado não encontrado.", "danger")
        return redirect(url_for("admin.tickets"))

    if request.method == "POST":
        acao = (request.form.get("acao") or "").strip().lower()

        if acao == "responder":
            texto = (request.form.get("mensagem") or "").strip()
            if not texto:
                flash("Digite uma resposta.", "warning")
                return redirect(url_for("admin.ticket_detalhe", id_chamado=id_chamado))
            # Uma leitura do relógio: a resposta é a mensagem e a atualização do chamado.
            instante = agora_utc()
            db.session.add(MensagemSuporte(
                id_chamado=id_chamado,
                id_usuario_remetente=current_user.id_usuario,
                mensagem=texto,
                data_envio=instante,
            ))
            if chamado.status_chamado == "ABERTO":
                chamado.status_chamado = "EM_ATENDIMENTO"
            chamado.data_atualizacao = instante
            notificar_clientes_empresa(
                chamado.id_empresa,
                f"A equipe NEXO respondeu seu chamado “{chamado.assunto}”.",
                url_for("cliente.ticket_detalhe", id_chamado=id_chamado),
                aviso="alerta_chamado",
            )
            db.session.commit()
            flash("Resposta enviada ao cliente.", "success")

        elif acao == "alterar_status":
            novo = (request.form.get("status_chamado") or "").strip().upper()
            if novo not in STATUS_CHAMADO:
                flash("Status inválido.", "danger")
                return redirect(url_for("admin.ticket_detalhe", id_chamado=id_chamado))
            status_anterior = chamado.status_chamado
            chamado.status_chamado = novo
            # Uma leitura do relógio: atualização e fechamento no mesmo instante.
            instante = agora_utc()
            chamado.data_atualizacao = instante
            # Só a TRANSIÇÃO para RESOLVIDO avisa o cliente (sininho e e-mail):
            # salvar de novo um chamado já resolvido não duplica o aviso nem
            # muda a data de fechamento registrada.
            resolvido = novo == "RESOLVIDO" and status_anterior != "RESOLVIDO"
            if resolvido:
                chamado.data_fechamento = instante
                notificar_clientes_empresa(
                    chamado.id_empresa,
                    f"Seu chamado “{chamado.assunto}” foi marcado como resolvido.",
                    url_for("cliente.ticket_detalhe", id_chamado=id_chamado),
                    aviso="alerta_chamado",
                )
            elif novo != "RESOLVIDO":
                chamado.data_fechamento = None
            try:
                db.session.commit()
            except SQLAlchemyError as erro:
                db.session.rollback()
                current_app.logger.warning("Alteração de status do chamado não persistida (%s).", type(erro).__name__)
                flash("Não foi possível alterar o status do chamado. Tente novamente.", "danger")
                return redirect(url_for("admin.ticket_detalhe", id_chamado=id_chamado))
            # O e-mail só sai depois de o status estar confirmado no banco; uma
            # falha no aviso nunca vira erro da operação já confirmada.
            if resolvido:
                try:
                    email_ticket_resolvido(
                        chamado,
                        url_publica("cliente.ticket_detalhe", id_chamado=id_chamado),
                    )
                except Exception as erro:  # noqa: BLE001
                    current_app.logger.warning("Aviso por e-mail do chamado resolvido não preparado (%s).",
                                               type(erro).__name__)
            flash("Status do chamado atualizado.", "success")
        else:
            flash("Ação inválida.", "danger")

        return redirect(url_for("admin.ticket_detalhe", id_chamado=id_chamado))

    return render_template(
        "admin/ticket_detalhe.html",
        chamado=chamado, categorias=CATEGORIAS_TICKET, status_opcoes=STATUS_CHAMADO,
    )


# =====================================================================
# CMS — Base de Conhecimento (Guia) gerenciável pelo Admin
# =====================================================================
_GUIA_IMG_EXT = {"png", "jpg", "jpeg", "webp", "gif"}


def _categorias_existentes():
    """Lista de categorias já usadas (para o datalist do formulário)."""
    return [c for (c,) in db.session.execute(
        select(GuiaTopico.categoria).distinct().order_by(GuiaTopico.categoria)
    ).all()]


def _salvar_imagem_guia(arquivo):
    """
    Salva com segurança a imagem enviada (via storage.py, no banco) e devolve a
    referência a gravar em imagem_url (ex.: 'db:guia/abc123.png'), ou None.
    Valida o conteúdo e a extensão; nomeia pelo hash (evita path traversal).
    """
    if not arquivo or not arquivo.filename:
        return None
    ext = os.path.splitext(secure_filename(arquivo.filename))[1].lower().lstrip(".")
    if ext not in _GUIA_IMG_EXT:
        flash(f"Imagem .{ext} não suportada. Use: {sorted(_GUIA_IMG_EXT)}.", "warning")
        return None
    conteudo = arquivo.read()
    if not conteudo:
        return None
    formatos = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP", "gif": "GIF"}
    try:
        with Image.open(BytesIO(conteudo)) as imagem:
            if imagem.format != formatos[ext]:
                raise ValueError("O formato da imagem não corresponde à extensão.")
            imagem.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        flash("Imagem inválida ou corrompida. Envie uma imagem PNG, JPEG, WebP ou GIF válida.", "warning")
        return None
    nome = f"{hashlib.sha256(conteudo).hexdigest()[:16]}.{ext}"
    return storage.salvar(conteudo, f"guia/{nome}")


def _descartar_imagem_guia(referencia, id_topico):
    """Apaga do storage uma imagem que o tópico deixou de usar — só se nenhum
    OUTRO tópico usa a mesma imagem (nomes por hash: imagens iguais são
    compartilhadas). Referências legadas em static/ não são tocadas."""
    if not referencia or not referencia.startswith(storage.PREFIXO_DB):
        return
    em_uso = db.session.execute(
        select(func.count(GuiaTopico.id))
        .where(GuiaTopico.imagem_url == referencia)
        .where(GuiaTopico.id != id_topico)
    ).scalar()
    if not em_uso:
        storage.remover(referencia)


@admin_bp.route("/guia")
@admin_required
def guia():
    """Listagem (CMS) de todas as soluções da Base de Conhecimento."""
    topicos = db.session.execute(
        select(GuiaTopico).order_by(GuiaTopico.categoria, GuiaTopico.id)
    ).scalars().all()
    # Tópicos com sinais de conteúdo de teste (ver nexobot/higiene.py): apenas
    # sinalizados para revisão; a desativação/exclusão continua manual, pelo CMS.
    from nexobot.higiene import motivo_teste
    suspeitos = {t.id: motivo for t in topicos
                 if (motivo := motivo_teste(t.categoria, t.pergunta, t.resposta))}
    return render_template("admin/guia.html", topicos=topicos, total=len(topicos), suspeitos=suspeitos)


@admin_bp.route("/guia/novo", methods=["GET", "POST"])
@admin_required
def guia_novo():
    """Cria um novo tópico da Base de Conhecimento."""
    if request.method == "POST":
        categoria = (request.form.get("categoria") or "").strip()
        pergunta = (request.form.get("pergunta") or "").strip()
        resposta = (request.form.get("resposta") or "").strip()
        if not (categoria and pergunta and resposta):
            flash("Categoria, pergunta e resposta são obrigatórias.", "danger")
            return redirect(url_for("admin.guia_novo"))

        topico = GuiaTopico(categoria=categoria, pergunta=pergunta, resposta=resposta)
        img = _salvar_imagem_guia(request.files.get("imagem"))
        if img:
            topico.imagem_url = img
        db.session.add(topico)
        db.session.commit()
        flash("Tópico criado na Base de Conhecimento.", "success")
        return redirect(url_for("admin.guia"))

    return render_template(
        "admin/guia_form.html", topico=None, categorias=_categorias_existentes(),
    )


@admin_bp.route("/guia/editar/<int:id>", methods=["GET", "POST"])
@admin_required
def guia_editar(id):
    """Edita um tópico existente (conteúdo e/ou imagem)."""
    topico = db.session.get(GuiaTopico, id)
    if topico is None:
        flash("Tópico não encontrado.", "danger")
        return redirect(url_for("admin.guia"))

    if request.method == "POST":
        categoria = (request.form.get("categoria") or "").strip()
        pergunta = (request.form.get("pergunta") or "").strip()
        resposta = (request.form.get("resposta") or "").strip()
        if not (categoria and pergunta and resposta):
            flash("Categoria, pergunta e resposta são obrigatórias.", "danger")
            return redirect(url_for("admin.guia_editar", id=id))

        topico.categoria = categoria
        topico.pergunta = pergunta
        topico.resposta = resposta
        topico.ativo = bool(request.form.get("ativo"))
        imagem_anterior = topico.imagem_url
        if request.form.get("remover_imagem"):
            topico.imagem_url = None
        img = _salvar_imagem_guia(request.files.get("imagem"))
        if img:
            topico.imagem_url = img
        if imagem_anterior and imagem_anterior != topico.imagem_url:
            _descartar_imagem_guia(imagem_anterior, topico.id)
        topico.data_atualizacao = datetime.now(timezone.utc)
        db.session.commit()
        flash("Tópico atualizado.", "success")
        return redirect(url_for("admin.guia"))

    return render_template(
        "admin/guia_form.html", topico=topico, categorias=_categorias_existentes(),
    )


@admin_bp.route("/guia/<int:id>/deletar", methods=["POST"])
@admin_required
def guia_deletar(id):
    """Remove um tópico da Base de Conhecimento (POST-only)."""
    topico = db.session.get(GuiaTopico, id)
    if topico is None:
        flash("Tópico não encontrado.", "danger")
        return redirect(url_for("admin.guia"))
    _descartar_imagem_guia(topico.imagem_url, topico.id)
    db.session.delete(topico)
    db.session.commit()
    flash("Tópico removido da Base de Conhecimento.", "success")
    return redirect(url_for("admin.guia"))


# =====================================================================
# Configurações: senha do admin, documentos legais, SMTP e cópia de chamados
# =====================================================================
@admin_bp.route("/configuracoes")
@admin_required
def configuracoes():
    resposta = make_response(render_template("admin/configuracoes.html", **configuracoes_conta.contexto_admin()))
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@admin_bp.route("/configuracoes/senha", methods=["POST"])
@admin_required
@limite_reautenticacao
def configuracoes_senha():
    erro = trocar_senha_autenticada(current_user._get_current_object(), request.form.get("senha_atual"),
                                    request.form.get("nova_senha"), request.form.get("confirmacao_nova_senha"))
    flash(erro or "Senha alterada. As outras sessões abertas foram encerradas.", "danger" if erro else "success")
    return redirect(url_for("admin.configuracoes"))


@admin_bp.route("/configuracoes/documentos", methods=["POST"])
@admin_required
@limiter.limit("30 per hour", key_func=chave_limite_usuario)
def configuracoes_documento():
    """Publica a versão seguinte dos Termos de Uso ou da Política de Privacidade.

    Se a publicação for recusada, a página volta com o texto digitado: um conflito de
    versão não pode custar ao admin o documento que acabou de escrever."""
    tipo = request.form.get("tipo")
    id_admin = current_user.id_usuario
    try:
        documento = configuracoes_conta.publicar_documento(
            tipo, request.form.get("conteudo"), request.form.get("versao_exibida"),
            current_user._get_current_object(), request.form.get("alteracao"),
        )
        versao = documento.rotulo_versao if documento else None
        db.session.commit()
    except (configuracoes_conta.DadoInvalido, IntegrityError) as erro:
        db.session.rollback()
        flash(str(erro) if isinstance(erro, configuracoes_conta.DadoInvalido) else
              "Outra versão foi publicada ao mesmo tempo. Confira o texto atual e publique de novo.", "danger")
        return render_template("admin/configuracoes.html", rascunho_tipo=tipo,
                               rascunho_conteudo=request.form.get("conteudo") or "",
                               rascunho_alteracao=request.form.get("alteracao"),
                               **configuracoes_conta.contexto_admin()), 400
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Documento legal não publicado (%s).", type(erro).__name__)
        flash("Não foi possível publicar o documento. Tente novamente.", "danger")
        return redirect(url_for("admin.configuracoes"))
    rotulo = configuracoes_conta.TIPOS_DOCUMENTO[tipo]
    if versao is None:
        flash(f"{rotulo}: o texto não mudou, nenhuma versão nova foi criada.", "info")
    else:
        current_app.logger.info("Documento legal publicado | tipo=%s versao=%s admin=%s", tipo, versao, id_admin)
        flash(f"{rotulo}: versão {versao} publicada. Os clientes precisam aceitá-la no próximo acesso.", "success")
    return redirect(url_for("admin.configuracoes"))


@admin_bp.route("/termos-aceites")
@admin_required
def termos_aceites():
    """Auditoria do aceite: status de cada cliente, versão aceita, data e IP, e a adesão."""
    resposta = make_response(render_template("admin/termos_aceites.html", **termos_legais.auditoria()))
    # IP e e-mail de clientes na página: nada em cache.
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@admin_bp.route("/termos-aceites/<int:id_usuario>/lembrete", methods=["POST"])
@admin_required
# Um e-mail por clique, para uma pessoa: sem disparo em massa.
@limiter.limit("30 per hour", methods=["POST"], key_func=chave_limite_usuario)
def termos_aceites_lembrete(id_usuario):
    """Lembrete por e-mail a um CLIENTE com aceite pendente. Síncrono: o admin vê se o
    provedor aceitou. A pendência é conferida de novo agora, não na hora em que a tabela abriu."""
    destino = url_for("admin.termos_aceites")
    try:
        usuario = db.session.get(Usuario, id_usuario, populate_existing=True)
        if usuario is None or usuario.role != "CLIENTE":
            flash("Usuário não encontrado.", "danger")
            return redirect(destino)
        if not usuario.ativo or usuario.primeiro_acesso or not usuario_pode_acessar(usuario):
            flash("Este usuário não tem acesso ao portal agora: o lembrete não foi enviado.", "warning")
            return redirect(destino)
        pendencias = termos_legais.pendentes(usuario.id_usuario) if termos_legais.esquema_configuracoes_disponivel() else []
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Lembrete de aceite indisponível (%s).", type(erro).__name__)
        flash("Não foi possível consultar o usuário agora. Tente novamente.", "danger")
        return redirect(destino)
    if not pendencias:
        flash("Este usuário já aceitou as versões vigentes: nenhum lembrete foi enviado.", "info")
        return redirect(destino)
    current_app.logger.info("Lembrete de aceite solicitado pelo administrador | usuario=%s", usuario.id_usuario)
    _flash_envio_email(email_lembrete_aceite(usuario, pendencias, url_publica("cliente.dashboard")),
                       "o lembrete de aceite")
    return redirect(destino)


@admin_bp.route("/configuracoes/copia-suporte", methods=["POST"])
@admin_required
# Step-up: a caixa recebe o texto de todo chamado novo e continua recebendo depois que a sessão cai.
@limite_reautenticacao
def configuracoes_copia_suporte():
    """Caixa que recebe em cópia oculta (BCC) cada novo chamado; vazio desliga."""
    if not current_user.check_senha(request.form.get("senha_confirmacao") or ""):
        flash("Confirme sua senha de administrador para alterar a cópia de chamados.", "danger")
        return redirect(url_for("admin.configuracoes"))
    id_admin = current_user.id_usuario
    for ultima_tentativa in (False, True):
        try:
            anterior = configuracoes_conta.email_copia_suporte()
            endereco = configuracoes_conta.salvar_email_copia_suporte(
                request.form.get("email_copia"), current_user._get_current_object(),
            )
            if endereco != anterior:
                notificar_admins("A caixa que recebe a cópia dos novos chamados foi alterada. Confira em Configurações.",
                                 url_for("admin.configuracoes"))
            db.session.commit()
        except IntegrityError:
            # Dois envios na primeira gravação: quem perde repete e encontra a linha do outro.
            db.session.rollback()
            if not ultima_tentativa:
                continue
            flash("Não foi possível salvar. Tente novamente.", "danger")
        except configuracoes_conta.DadoInvalido as erro:
            db.session.rollback()
            flash(str(erro), "danger")
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Cópia de chamados não salva (%s).", type(erro).__name__)
            flash("Não foi possível salvar. Tente novamente.", "danger")
        else:
            if endereco != anterior:
                current_app.logger.info("Caixa de cópia de chamados alterada | admin=%s", id_admin)
            if endereco and not envio_disponivel(current_app.config):
                flash("Caixa salva, mas o envio de e-mail está indisponível: nenhuma cópia sai até ele voltar. "
                      "Confira o Diagnóstico de e-mail.", "warning")
            else:
                flash("Novos chamados serão copiados para este e-mail." if endereco
                      else "Cópia de novos chamados desligada.", "success")
        break
    return redirect(url_for("admin.configuracoes"))


# =====================================================================
# Diagnóstico de e-mail (configuração, envios recentes e teste controlado)
# =====================================================================
@admin_bp.route("/diagnostico-email")
@admin_required
def diagnostico_email():
    """Situação do envio de e-mails sem segredos: transporte, servidor SMTP,
    remetente, formato da chave (só pelo prefixo), origem dos links, pendências,
    último desfecho da conexão SMTP e os envios recentes deste processo (estado,
    categoria, correlação)."""
    return render_template(
        "admin/diagnostico_email.html",
        diagnostico=diagnostico_configuracao(), envios=historico_envios(),
        conexao_smtp=ultima_conexao_smtp(),
    )


@admin_bp.route("/diagnostico-email/conexao", methods=["POST"])
@admin_required
# Cada pedido abre uma conexão e faz login no servidor SMTP: limite por usuário
# para o botão não virar tentativas de login em série na conta de e-mail.
@limiter.limit("30 per hour", methods=["POST"], key_func=chave_limite_usuario)
def diagnostico_email_conexao():
    """Testa a conexão SMTP configurada (TLS e login), sem enviar nenhuma mensagem."""
    resultado = verificar_conexao_smtp()
    if resultado["ok"]:
        flash("Conexão SMTP confirmada: o servidor aceitou a conexão e o login. Nenhum e-mail foi enviado; "
              "use o envio de teste para confirmar a mensagem.", "success")
    elif resultado["origem"] == "verificacao" and resultado["categoria"] in (
            "transporte_desativado", "smtp_nao_usado", "smtp_suprimido"):
        flash(f"A conexão SMTP não foi testada: {resultado['descricao']}.", "warning")
    else:
        flash(f"A conexão SMTP falhou: {resultado['descricao'] or 'erro inesperado na conexão'}.", "danger")
    return redirect(url_for("admin.diagnostico_email"))


@admin_bp.route("/diagnostico-email/teste", methods=["POST"])
@admin_required
# Destinatário fixo (o próprio administrador) e limite por usuário: não é um
# canal de disparo para terceiros nem em massa.
@limiter.limit("5 per hour", methods=["POST"], key_func=chave_limite_usuario)
def diagnostico_email_teste():
    """Envia, de forma síncrona, um e-mail de teste ao próprio administrador."""
    envio = email_teste_configuracao(current_user)
    _flash_envio_email(envio, "o e-mail de teste", tecnico=True)
    return redirect(url_for("admin.diagnostico_email"))


@admin_bp.route("/diagnostico-email/entrega/<correlacao>", methods=["POST"])
@admin_required
# Somente leitura no provedor, só para envios do histórico deste processo, com
# limite por usuário (a API de eventos do Brevo aceita 300 consultas/hora).
@limiter.limit("20 per hour", methods=["POST"], key_func=chave_limite_usuario)
def diagnostico_email_entrega(correlacao):
    """Consulta no Brevo os eventos de entrega de um envio recente (teste controlado)."""
    envio = next((e for e in historico_envios() if e.correlacao == correlacao), None)
    if envio is None or envio.transporte != "brevo" or not envio.id_mensagem:
        flash("Este envio não está mais no histórico recente ou não tem identificação no Brevo.", "warning")
        return redirect(url_for("admin.diagnostico_email"))
    resultado = consultar_entrega_brevo(envio)
    ref = f"Ref. {envio.correlacao}"
    if not resultado["ok"]:
        # A chave e o IP valem para a consulta como para o envio; nos demais
        # casos a consulta só não foi concluída — o aceite do envio não muda.
        if resultado["categoria"] in ("credencial_rejeitada", "ip_nao_autorizado"):
            motivo = CATEGORIAS[resultado["categoria"]]
        else:
            motivo = "o Brevo não concluiu a consulta agora (o aceite do envio continua valendo)"
        flash(f"Não foi possível consultar a entrega no Brevo ({ref}): {motivo}. Tente de novo em instantes.",
              "warning")
    elif not resultado["eventos"]:
        flash(f"O Brevo ainda não registrou eventos para este envio ({ref}). Os registros podem levar alguns "
              "minutos: consulte de novo em instantes.", "info")
    else:
        nomes = {e["evento"] for e in resultado["eventos"]}
        rotulos = ", ".join(dict.fromkeys(e["rotulo"] for e in resultado["eventos"]))
        if "delivered" in nomes:
            flash(f"O Brevo registrou a entrega ao servidor do destinatário ({ref}): {rotulos}. A chegada à "
                  "caixa de entrada só o destinatário confirma (verifique também o spam).", "success")
        elif nomes & EVENTOS_FALHA_ENTREGA:
            flash(f"O Brevo registrou um problema na entrega ({ref}): {rotulos}. Veja o detalhe em Brevo → "
                  "Transacional → Logs.", "danger")
        else:
            flash(f"O Brevo registrou o envio, mas ainda não a entrega ({ref}): {rotulos}. Consulte de novo em "
                  "instantes.", "info")
    return redirect(url_for("admin.diagnostico_email"))
