"""
NEXO - Blueprint do CLIENTE
============================
Rotas:
  /cliente/dashboard  → visão geral com a última devolutiva publicada (CONCLUIDO),
                        histórico recente, análises em andamento, KPIs do Indicador
                        de Pressão de Estoque, Curva ABC e semáforos de risco.
  /cliente/historico  → lista de análises publicadas.
  /cliente/analise/<id> → detalhe de uma análise publicada.
  /cliente/upload     → self-service onboarding: o cliente anexa os relatórios
                        ERP (Vendas + Compras) das análises abertas para a sua
                        empresa; o consultor NEXO homologa e dispara o ETL.
  /cliente/configuracoes → perfil, senha, e-mail de acesso, privacidade (LGPD) e
                        preferências; regras em configuracoes_conta.py.

Regras:
  - Acesso restrito a Usuario.role == 'CLIENTE' (via cliente_required).
  - O que a empresa pode consultar, exportar e enviar é decidido por
    contract_policy (regime legado ou contratual, cobertura paga, prazos da
    suspensão), sempre no servidor: dados não autorizados não chegam ao
    HTML nem ao JavaScript.
  - "Análise publicada" = status_analise == 'CONCLUIDO' (transição feita só
    quando o ADMIN publica a devolutiva, nunca automaticamente pelo ETL).
  - KPIs vêm de indicador_analise; o ranking ABC, de produto_curva_abc.
  - Uploads do cliente nascem com id_usuario_admin = NULL (aguardando validação).
"""

import json
import os
from functools import wraps

from flask import (
    Blueprint, render_template, abort, request, redirect, url_for, flash, current_app,
    Response, g, make_response,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from etl_inputs import ETLInputError
from etl_uploads import ler_upload_validado, limitar_upload

from blueprints.auth import limite_reautenticacao, trocar_email_autenticado, trocar_senha_autenticada
import configuracoes_conta
from extensions import chave_limite_usuario, db, limiter
from horario import agora_utc
from models import (
    Analise, IndicadorAnalise, RelatorioAnalise, UploadRelatorio,
    ChamadoSuporte, MensagemSuporte, GuiaTopico,
)
from contract_policy import (
    agora, data_local, filtrar_analises_consultaveis, obter_situacao_contratual, pode_consultar_analise,
    pode_enviar_relatorio, pode_exportar_pdf,
)
from insights import gerar_semaforos
from notifications import notificar_admins
from preferencias_usuario import salvar_preferencias
from pdf_export import gerar_pdf_analise
import storage

cliente_bp = Blueprint("cliente", __name__)

# Estados em que uma análise ainda aceita anexos do cliente.
STATUS_ACEITA_UPLOAD = ("AGUARDANDO_RELATORIO", "RELATORIO_RECEBIDO")

# Categorias de ticket (token persistido -> rótulo exibido).
CATEGORIAS_TICKET = {
    "FINANCEIRO": "Financeiro",
    "DUVIDA_TECNICA": "Dúvida Técnica",
    "ERRO_INTEGRACAO": "Erro de Integração",
}


def cliente_required(f):
    """Decorator: bloqueia rota para qualquer não-CLIENTE."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_cliente:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


MENSAGEM_PDF_ENCERRADO = (
    "Seu prazo para exportação de relatórios foi encerrado. As análises históricas "
    "autorizadas continuam disponíveis para consulta durante o período de suspensão."
)


def _situacao_cliente():
    """Situação contratual da empresa do CLIENTE, calculada uma vez por requisição.

    A guarda de sessão já bloqueia empresas sem acesso ao portal; aqui a mesma
    política é reavaliada para que nenhuma rota dependa só dessa guarda.
    """
    if "_nexo_situacao_cliente" not in g:
        g._nexo_situacao_cliente = obter_situacao_contratual(current_user.empresa)
    situacao = g._nexo_situacao_cliente
    if not situacao.acesso_portal:
        abort(403)
    return situacao


def _acesso_restrito(titulo: str, mensagem: str):
    """Resposta 403 sem nenhum dado da análise solicitada."""
    return render_template("cliente/acesso_restrito.html", titulo=titulo, mensagem=mensagem), 403


# =====================================================================
# Helpers de montagem dos dados de gráfico (Plotly)
# =====================================================================
def _abc_chart(analise, limite: int = 10) -> dict | None:
    """
    Dados do Gráfico de Pareto da Curva ABC (Top-N por faturamento):
    barras (faturamento absoluto) + linha de % acumulado. O percentual
    acumulado é o do total geral (calculado no ETL via pandas cumsum),
    então a linha reflete a concentração real da receita.
    """
    if not analise or not analise.curva_abc:
        return None
    top = analise.curva_abc[:limite]  # já ordenado por posicao_ranking no relationship
    return {
        "labels": [p.produto_nome for p in top],
        "values": [float(p.faturamento or 0) for p in top],
        "classes": [p.classe_abc for p in top],
        "acumulado": [float(p.percentual_acumulado or 0) for p in top],
    }


def _historico_temporal(analises) -> dict | None:
    """
    Série temporal Vendas (faturamento) x Compras (volume comprado) ao longo das
    análises CONCLUIDAS que a política permite consultar — alimenta o gráfico
    de linha comparativo. Recebe só análises já autorizadas: nenhum período não
    pago entra no agregado. Precisa de >= 2 pontos para fazer sentido.
    """
    linhas = sorted(
        ((a, a.indicador) for a in analises if a.indicador is not None),
        key=lambda par: (par[0].ano_referencia, par[0].mes_referencia, par[0].id_analise),
    )
    if len(linhas) < 2:
        return None
    return {
        "labels": [f"{a.mes_referencia:02d}/{a.ano_referencia}" for a, _ in linhas],
        "vendas": [float(ind.faturamento_total or 0) for _, ind in linhas],
        "compras": [float(ind.total_comprado or 0) for _, ind in linhas],
    }


def _auditoria(analise) -> dict | None:
    """
    Metadados de origem dos dados de UMA análise: empresa, arquivos importados
    e período (lido do cabeçalho do relatório via pandas, persistido em
    indicador_analise; fallback no período declarado da análise).
    Usado tanto no dashboard quanto na tela de análise individual.
    """
    if not analise:
        return None
    emp = analise.empresa
    arquivos = {u.tipo_relatorio: u.nome_arquivo_original for u in analise.uploads}
    ind = analise.indicador
    derivado = bool(ind and ind.periodo_base_inicio)
    return {
        "empresa": (emp.nome_fantasia or emp.razao_social) if emp else "—",
        "arquivo_vendas": arquivos.get("VENDAS"),
        "arquivo_compras": arquivos.get("COMPRAS"),
        "periodo_inicio": (ind.periodo_base_inicio if derivado else analise.periodo_inicio),
        "periodo_fim": (ind.periodo_base_fim if derivado else analise.periodo_fim),
        "periodo_derivado": derivado,
    }


@cliente_bp.route("/dashboard")
@cliente_required
def dashboard():
    empresa = current_user.empresa  # garantido pelo CHECK do banco
    situacao = _situacao_cliente()

    # Análises publicadas (CONCLUIDO) que a política permite consultar: no
    # regime contratual, só as de períodos integralmente pagos. A primeira é a
    # "última devolutiva"; a lista alimenta o histórico recente e o gráfico.
    analises = filtrar_analises_consultaveis(situacao, db.session.execute(
        select(Analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise == "CONCLUIDO")
        .order_by(Analise.data_conclusao.desc().nullslast(), Analise.data_criacao.desc())
        .options(selectinload(Analise.indicador), selectinload(Analise.relatorio))
    ).scalars().all())
    analise = analises[0] if analises else None

    # Análises em andamento (não-CONCLUIDO) para a empresa.
    analises_em_andamento = db.session.execute(
        select(Analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise != "CONCLUIDO")
        .order_by(Analise.data_criacao.desc())
    ).scalars().all()

    # Dados para os gráficos Plotly e semáforos. Só montamos quando há indicador real.
    dashboard_data = None
    semaforos = []
    if analise and analise.indicador:
        ind: IndicadorAnalise = analise.indicador
        dashboard_data = {
            "comparativo": {
                "labels": ["Compras", "Vendas"],
                "values": [float(ind.total_comprado or 0), float(ind.faturamento_total or 0)],
            },
            "pressao_estoque": float(ind.saldo_estimado_compras_vendas or 0),
            "curva_abc": _abc_chart(analise),
            "historico": _historico_temporal(analises),
        }
        semaforos = gerar_semaforos(ind, analise.curva_abc)

    # Auditoria de origem dos dados (COM QUE BASE/ARQUIVO/PERÍODO foi gerado).
    auditoria = _auditoria(analise)

    return render_template(
        "cliente/dashboard.html",
        empresa=empresa,
        analise=analise,
        analises=analises,
        analises_em_andamento=analises_em_andamento,
        dashboard_data=dashboard_data,
        semaforos=semaforos,
        auditoria=auditoria,
        situacao=situacao,
        aviso_renovacao=configuracoes_conta.aviso_renovacao(current_user, situacao),
    )


# =====================================================================
# Configurações: sempre a conta e a empresa da sessão, nunca um id da requisição
# =====================================================================
@cliente_bp.route("/configuracoes")
@cliente_required
def configuracoes():
    """Perfil e segurança, privacidade (LGPD) e preferências da própria conta."""
    situacao = _situacao_cliente()
    resposta = make_response(render_template(
        "cliente/configuracoes.html", empresa=current_user.empresa, situacao=situacao,
        **configuracoes_conta.contexto_cliente(current_user),
    ))
    # Dados pessoais na página: o botão Voltar não os mostra depois do logout.
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


def _salvar_configuracao(operacao, sucesso: str):
    """Executa e confirma uma alteração; entrada recusada ou falha de banco viram aviso.

    Dois envios juntos na primeira gravação: quem perde a corrida repete e encontra a
    linha do outro, em vez de mostrar erro por uma gravação que valeu."""
    for ultima_tentativa in (False, True):
        try:
            operacao()
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            if not ultima_tentativa:
                continue
            flash("Não foi possível salvar. Tente novamente.", "danger")
        except configuracoes_conta.DadoInvalido as erro:
            db.session.rollback()
            flash(str(erro), "danger")
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Configuração do cliente não salva (%s).", type(erro).__name__)
            flash("Não foi possível salvar. Tente novamente.", "danger")
        else:
            flash(sucesso, "success")
        break
    return redirect(url_for("cliente.configuracoes"))


@cliente_bp.route("/configuracoes/perfil", methods=["POST"])
@cliente_required
@limiter.limit("30 per hour", key_func=chave_limite_usuario)
def configuracoes_perfil():
    """Nome do responsável e telefone de contato."""
    _situacao_cliente()
    usuario = current_user._get_current_object()
    return _salvar_configuracao(
        lambda: configuracoes_conta.salvar_perfil(usuario, request.form.get("nome"), request.form.get("telefone"),
                                                  request.form.get("telefone_exibido")),
        "Dados atualizados.",
    )


@cliente_bp.route("/configuracoes/senha", methods=["POST"])
@cliente_required
@limite_reautenticacao
def configuracoes_senha():
    _situacao_cliente()
    erro = trocar_senha_autenticada(current_user._get_current_object(), request.form.get("senha_atual"),
                                    request.form.get("nova_senha"), request.form.get("confirmacao_nova_senha"))
    flash(erro or "Senha alterada. As outras sessões abertas foram encerradas.", "danger" if erro else "success")
    return redirect(url_for("cliente.configuracoes"))


@cliente_bp.route("/configuracoes/email", methods=["POST"])
@cliente_required
@limite_reautenticacao
def configuracoes_email():
    _situacao_cliente()
    erro = trocar_email_autenticado(current_user._get_current_object(), request.form.get("senha_atual"),
                                    request.form.get("novo_email"), request.form.get("confirmacao_novo_email"))
    flash(erro or "E-mail de acesso alterado: use o novo endereço no próximo login. As outras sessões foram "
                  "encerradas.", "danger" if erro else "success")
    return redirect(url_for("cliente.configuracoes"))


@cliente_bp.route("/configuracoes/exportar-dados")
@cliente_required
@limiter.limit("10 per hour", key_func=chave_limite_usuario)
def configuracoes_exportar_dados():
    """Arquivo JSON com os dados da empresa da sessão (LGPD). Nenhum parâmetro da
    requisição escolhe de quem são os dados."""
    situacao = _situacao_cliente()
    empresa = current_user.empresa
    dados = configuracoes_conta.montar_exportacao(current_user, situacao)
    current_app.logger.info("Exportação de dados da conta | usuario=%s empresa=%s analises=%s",
                            current_user.id_usuario, empresa.id_empresa, len(dados["analises_concluidas"]))
    nome = f"nexo_dados_empresa_{empresa.id_empresa}_{data_local(agora()):%Y%m%d}.json"
    return Response(
        json.dumps(dados, ensure_ascii=False, indent=2, default=configuracoes_conta.valor_json),
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{nome}"',
            # Dado pessoal: nenhum cache (navegador, proxy ou CDN) pode guardar o arquivo.
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@cliente_bp.route("/configuracoes/aceitar-documento", methods=["POST"])
@cliente_required
@limiter.limit("30 per hour", key_func=chave_limite_usuario)
def configuracoes_aceitar_documento():
    """Registra o aceite da versão vigente de um documento, pelo próprio usuário."""
    _situacao_cliente()
    try:
        configuracoes_conta.registrar_aceite(current_user._get_current_object(), request.form.get("tipo"),
                                             request.form.get("versao"))
        db.session.commit()
    except configuracoes_conta.DadoInvalido as erro:
        db.session.rollback()
        flash(str(erro), "danger")
    except IntegrityError:
        db.session.rollback()  # dois envios juntos: o outro já registrou o aceite
        flash("Aceite registrado.", "success")
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Aceite de documento não salvo (%s).", type(erro).__name__)
        flash("Não foi possível registrar o aceite. Tente novamente.", "danger")
    else:
        flash("Aceite registrado.", "success")
    return redirect(url_for("cliente.configuracoes"))


@cliente_bp.route("/configuracoes/solicitar-exclusao", methods=["POST"])
@cliente_required
@limiter.limit("5 per hour", key_func=chave_limite_usuario)
def configuracoes_solicitar_exclusao():
    """Abre o chamado interno de expurgo (sem duplicar um já aberto); nada é apagado aqui."""
    _situacao_cliente()
    try:
        chamado, criado = configuracoes_conta.solicitar_exclusao_dados(current_user._get_current_object())
        id_chamado = chamado.id_chamado
        db.session.commit()
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Solicitação de exclusão não registrada (%s).", type(erro).__name__)
        flash("Não foi possível registrar a solicitação. Tente novamente.", "danger")
        return redirect(url_for("cliente.configuracoes"))
    if criado:
        configuracoes_conta.avisar_novo_chamado(id_chamado, CATEGORIAS_TICKET["DUVIDA_TECNICA"])
        flash(f"Solicitação registrada no chamado #{id_chamado}. Nenhum dado foi apagado ainda: a equipe NEXO "
              "fará a tratativa.", "success")
    else:
        flash(f"Já existe uma solicitação em andamento, no chamado #{id_chamado}.", "info")
    return redirect(url_for("cliente.configuracoes"))


@cliente_bp.route("/configuracoes/preferencias", methods=["POST"])
@cliente_required
@limiter.limit("60 per hour", key_func=chave_limite_usuario)
def configuracoes_preferencias():
    _situacao_cliente()
    id_usuario = current_user.id_usuario

    def salvar():
        configuracoes_conta.exigir_esquema()
        salvar_preferencias(id_usuario, lembrete_renovacao="lembrete_renovacao" in request.form,
                            alerta_chamado="alerta_chamado" in request.form)

    return _salvar_configuracao(salvar, "Preferências salvas.")


@cliente_bp.route("/guia")
@cliente_required
def guia():
    """
    Aba Guia/Wiki: dúvidas comuns lidas DINAMICAMENTE do banco (CMS), agrupadas
    por categoria. Mantém ordem estável (categoria, id).
    """
    topicos = db.session.execute(
        select(GuiaTopico)
        .where(GuiaTopico.ativo.is_(True))
        .order_by(GuiaTopico.categoria, GuiaTopico.id)
    ).scalars().all()

    # Agrupa por categoria preservando a ordem de aparição.
    grupos = {}
    for t in topicos:
        grupos.setdefault(t.categoria, []).append(t)

    return render_template("cliente/guia.html", grupos=grupos, total=len(topicos))


@cliente_bp.route("/historico")
@cliente_required
def historico():
    """
    Lista completa das análises CONCLUIDAS com devolutiva publicada para a empresa do cliente.
    Ordenada por data_conclusao desc com tiebreak determinístico em id_analise desc.
    """
    empresa = current_user.empresa
    situacao = _situacao_cliente()
    analises = filtrar_analises_consultaveis(situacao, db.session.execute(
        select(Analise)
        .join(RelatorioAnalise, RelatorioAnalise.id_analise == Analise.id_analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise == "CONCLUIDO")
        .where(RelatorioAnalise.publicado.is_(True))
        .order_by(Analise.data_conclusao.desc().nullslast(), Analise.id_analise.desc())
        .options(selectinload(Analise.indicador), selectinload(Analise.relatorio))
    ).scalars().all())
    return render_template("cliente/historico.html", empresa=empresa, analises=analises,
                           situacao=situacao)


@cliente_bp.route("/analise/<int:id_analise>")
@cliente_required
def analise(id_analise):
    """
    Visão detalhada de uma análise publicada (KPIs + Curva ABC + semáforos +
    texto da devolutiva).

    Guards de segurança/visibilidade:
      - A análise precisa pertencer à empresa do cliente logado.
      - Precisa estar em CONCLUIDO E ter relatorio.publicado=True. Senão → 404.
    """
    analise = db.session.get(Analise, id_analise)
    if (
        analise is None
        or analise.id_empresa != current_user.empresa.id_empresa
        or analise.status_analise != "CONCLUIDO"
        or analise.relatorio is None
        or not analise.relatorio.publicado
    ):
        abort(404)
    situacao = _situacao_cliente()
    if not pode_consultar_analise(situacao, analise):
        return _acesso_restrito(
            "Análise indisponível",
            "O período desta análise não está coberto pelos períodos contratados e efetivamente pagos.",
        )

    semaforos = gerar_semaforos(analise.indicador, analise.curva_abc) if analise.indicador else []
    abc_chart = _abc_chart(analise)
    return render_template(
        "cliente/analise.html",
        analise=analise,
        # SEC-003: o cliente lê a cópia publicada, nunca o rascunho em edição.
        rel=analise.relatorio.publicada,
        semaforos=semaforos,
        abc_chart=abc_chart,
        auditoria=_auditoria(analise),
        situacao=situacao,
        pode_exportar_pdf=pode_exportar_pdf(situacao, analise),
        mensagem_pdf_encerrado=MENSAGEM_PDF_ENCERRADO,
    )


@cliente_bp.route("/analise/<int:id_analise>/pdf")
@cliente_required
def analise_pdf(id_analise):
    """Exporta a Análise Executiva (indicadores, Curva ABC com gráfico de pizza,
    síntese executiva e devolutiva) em PDF (ReportLab). Cada pedido gera um
    documento novo, com a data e a hora daquela exportação no horário de Brasília."""
    analise = db.session.get(Analise, id_analise)
    if (
        analise is None
        or analise.id_empresa != current_user.empresa.id_empresa
        or analise.status_analise != "CONCLUIDO"
        or analise.relatorio is None
        or not analise.relatorio.publicado
    ):
        abort(404)
    # Autorização ANTES de gerar: fora do prazo nenhum byte do PDF é produzido.
    situacao = _situacao_cliente()
    if not pode_consultar_analise(situacao, analise):
        return _acesso_restrito(
            "Análise indisponível",
            "O período desta análise não está coberto pelos períodos contratados e efetivamente pagos.",
        )
    if not pode_exportar_pdf(situacao, analise):
        return _acesso_restrito("Exportação encerrada", MENSAGEM_PDF_ENCERRADO)

    semaforos = gerar_semaforos(analise.indicador, analise.curva_abc) if analise.indicador else []
    # Relógio único da requisição (o mesmo instante que autorizou a exportação):
    # cada pedido é uma nova leitura, convertida para Brasília no gerador.
    pdf_bytes = gerar_pdf_analise(analise, semaforos, analise.curva_abc, gerado_em=agora())
    nome = f"analise_nexo_{analise.ano_referencia}{analise.mes_referencia:02d}_{analise.id_analise}.pdf"
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{nome}"',
            # Documento privado e datado: nenhum cache (navegador, proxy ou CDN)
            # pode guardar nem devolver uma exportação anterior.
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# =====================================================================
# Self-service onboarding: upload de relatórios ERP pelo CLIENTE
# =====================================================================
def _buscar_upload(id_analise: int, tipo: str):
    return db.session.execute(
        select(UploadRelatorio).where(
            UploadRelatorio.id_analise == id_analise,
            UploadRelatorio.tipo_relatorio == tipo,
        )
    ).scalar_one_or_none()


@cliente_bp.route("/upload", methods=["GET", "POST"])
@cliente_required
# Por usuário: cada POST custa uma validação em processo isolado (CPU/memória).
@limiter.limit("30 per hour", methods=["POST"], key_func=chave_limite_usuario)
@limitar_upload
def upload():
    """Preflight limitado e substituição serializada, sem executar ETL comercial."""
    try:
        empresa = current_user.empresa
        situacao = _situacao_cliente()
        # Suspensa no regime contratual: só análises abertas de período pago.
        analises_abertas = [a for a in db.session.execute(
            select(Analise).where(Analise.id_empresa == empresa.id_empresa)
            .where(Analise.status_analise.in_(STATUS_ACEITA_UPLOAD))
            .order_by(Analise.data_criacao.desc())
        ).scalars().all() if pode_enviar_relatorio(situacao, a)]
        if request.method != "POST":
            return render_template("cliente/upload.html", empresa=empresa, analises=analises_abertas,
                                   situacao=situacao)
        try:
            id_analise = int(request.form.get("id_analise", ""))
        except ValueError:
            flash("Selecione uma análise válida.", "danger")
            return redirect(url_for("cliente.upload"))
        analise = db.session.get(Analise, id_analise)
        if (analise is None or analise.id_empresa != empresa.id_empresa
                or analise.status_analise not in STATUS_ACEITA_UPLOAD):
            abort(404)
        if not pode_enviar_relatorio(situacao, analise):
            return _acesso_restrito(
                "Envio indisponível",
                "Durante a suspensão, novos arquivos só podem ser enviados para análises de períodos já pagos.",
            )
        tipo = (request.form.get("tipo_relatorio") or "").strip().upper()
        if tipo not in ("VENDAS", "COMPRAS"):
            flash("Tipo de relatório inválido (esperado VENDAS ou COMPRAS).", "danger")
            return redirect(url_for("cliente.upload"))
        arquivo = request.files.get("arquivo")
        if not arquivo or not arquivo.filename:
            flash("Selecione um arquivo para enviar.", "warning")
            return redirect(url_for("cliente.upload"))
        nome_seguro = secure_filename(arquivo.filename)
        ext = os.path.splitext(nome_seguro)[1].lower().lstrip(".")
        if ext not in current_app.config["ALLOWED_EXTENSIONS"]:
            flash(
                f"Extensão .{ext} não suportada. Aceitos: {sorted(current_app.config['ALLOWED_EXTENSIONS'])}.", "danger",
            )
            return redirect(url_for("cliente.upload"))
        conteudo, sha = ler_upload_validado(arquivo, ext)
        # Refresh obrigatório: estado/vínculo podem mudar durante a validação.
        analise = db.session.get(Analise, id_analise, with_for_update=True, populate_existing=True)
        if (analise is None or analise.id_empresa != empresa.id_empresa
                or analise.status_analise not in STATUS_ACEITA_UPLOAD):
            db.session.rollback()
            abort(404)
        # A situação contratual pode ter mudado durante a validação do arquivo:
        # relê empresa, contrato e relógio antes de gravar.
        if not pode_enviar_relatorio(obter_situacao_contratual(empresa, atualizar=True), analise):
            db.session.rollback()
            return _acesso_restrito(
                "Envio indisponível",
                "Durante a suspensão, novos arquivos só podem ser enviados para análises de períodos já pagos.",
            )
        nome_final = f"analise_{id_analise}_{tipo}_{sha[:8]}.{ext}"
        existente = _buscar_upload(id_analise, tipo)
        caminho_final = storage.salvar(conteudo, nome_final)
        if existente and existente.caminho_arquivo and existente.caminho_arquivo != caminho_final:
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
            id_analise=id_analise, id_usuario_admin=None,
            tipo_relatorio=tipo, nome_arquivo_original=nome_seguro,
            caminho_arquivo=caminho_final, extensao_arquivo=ext.upper(),
            tamanho_arquivo=len(conteudo), hash_arquivo=sha,
            status_processamento="PENDENTE",
        ))
        outro = _buscar_upload(id_analise, "COMPRAS" if tipo == "VENDAS" else "VENDAS")
        if outro and analise.status_analise == "AGUARDANDO_RELATORIO":
            analise.status_analise = "RELATORIO_RECEBIDO"
        nome_emp = empresa.nome_fantasia or empresa.razao_social
        notificar_admins(
            f"Nova planilha de {tipo.capitalize()} enviada por {nome_emp} aguardando homologação.",
            url_for("admin.analise_upload", id_analise=id_analise),
        )
        db.session.commit()
        flash(f"Relatório de {tipo} enviado. A equipe NEXO fará a validação e o processamento.", "success")
        return redirect(url_for("cliente.upload"))
    except ETLInputError as erro:
        db.session.rollback()
        flash(erro.safe_message, "danger")
        return redirect(url_for("cliente.upload"))
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Upload do cliente indisponível (%s).", type(erro).__name__)
        flash("Não foi possível salvar o relatório. Tente novamente.", "danger")
        return redirect(url_for("cliente.upload"))


# =====================================================================
# Suporte: tickets (chamados) do cliente
# =====================================================================
@cliente_bp.route("/tickets", methods=["GET", "POST"])
@cliente_required
# Cada chamado novo pode gerar um e-mail de cópia: sem teto, um único usuário
# esgotaria a cota do provedor, a mesma dos links de senha.
@limiter.limit("30 per hour", methods=["POST"], key_func=chave_limite_usuario)
def tickets():
    """
    GET  → lista os chamados da empresa do cliente + formulário de novo ticket.
    POST → abre um novo chamado (assunto + categoria + 1ª mensagem) e notifica
           os admins.
    """
    empresa = current_user.empresa

    if request.method == "POST":
        # Uma linha curta: o assunto vai para notificações, e-mails e listagens.
        assunto = " ".join((request.form.get("assunto") or "").split())[:160]
        categoria = (request.form.get("categoria") or "").strip().upper()
        mensagem = (request.form.get("mensagem") or "").strip()

        if not assunto or not mensagem:
            flash("Informe um assunto e descreva a sua dúvida.", "warning")
            return redirect(url_for("cliente.tickets"))
        if categoria not in CATEGORIAS_TICKET:
            flash("Selecione uma categoria válida.", "danger")
            return redirect(url_for("cliente.tickets"))

        chamado = ChamadoSuporte(
            id_empresa=empresa.id_empresa,
            id_usuario_cliente=current_user.id_usuario,
            assunto=assunto,
            categoria=categoria,
            status_chamado="ABERTO",
        )
        db.session.add(chamado)
        db.session.flush()  # garante id_chamado para a mensagem
        db.session.add(MensagemSuporte(
            id_chamado=chamado.id_chamado,
            id_usuario_remetente=current_user.id_usuario,
            mensagem=mensagem,
        ))

        nome_emp = empresa.nome_fantasia or empresa.razao_social
        notificar_admins(
            f"Novo chamado de {nome_emp}: “{assunto}”.",
            url_for("admin.ticket_detalhe", id_chamado=chamado.id_chamado),
        )

        id_chamado = chamado.id_chamado
        db.session.commit()
        configuracoes_conta.avisar_novo_chamado(id_chamado, CATEGORIAS_TICKET[categoria])
        flash("Chamado aberto com sucesso. A equipe NEXO responderá em breve.", "success")
        return redirect(url_for("cliente.ticket_detalhe", id_chamado=id_chamado))

    chamados = db.session.execute(
        select(ChamadoSuporte)
        .where(ChamadoSuporte.id_empresa == empresa.id_empresa)
        .order_by(ChamadoSuporte.data_abertura.desc(), ChamadoSuporte.id_chamado.desc())
    ).scalars().all()
    return render_template(
        "cliente/tickets.html",
        empresa=empresa, chamados=chamados, categorias=CATEGORIAS_TICKET,
    )


@cliente_bp.route("/tickets/<int:id_chamado>", methods=["GET", "POST"])
@cliente_required
def ticket_detalhe(id_chamado):
    """Conversa de um chamado da empresa do cliente; POST envia nova mensagem."""
    chamado = db.session.get(ChamadoSuporte, id_chamado)
    if chamado is None or chamado.id_empresa != current_user.empresa.id_empresa:
        abort(404)

    if request.method == "POST":
        texto = (request.form.get("mensagem") or "").strip()
        if not texto:
            flash("Digite uma mensagem.", "warning")
            return redirect(url_for("cliente.ticket_detalhe", id_chamado=id_chamado))

        # Uma leitura do relógio: a resposta é a mensagem e a atualização do chamado.
        instante = agora_utc()
        db.session.add(MensagemSuporte(
            id_chamado=id_chamado,
            id_usuario_remetente=current_user.id_usuario,
            mensagem=texto,
            data_envio=instante,
        ))
        # Cliente respondeu → reabre se estava RESOLVIDO; notifica os admins.
        if chamado.status_chamado == "RESOLVIDO":
            chamado.status_chamado = "EM_ATENDIMENTO"
        chamado.data_atualizacao = instante
        nome_emp = current_user.empresa.nome_fantasia or current_user.empresa.razao_social
        notificar_admins(
            f"Nova resposta de {nome_emp} no chamado “{chamado.assunto}”.",
            url_for("admin.ticket_detalhe", id_chamado=id_chamado),
        )
        db.session.commit()
        return redirect(url_for("cliente.ticket_detalhe", id_chamado=id_chamado))

    return render_template(
        "cliente/ticket_detalhe.html",
        chamado=chamado, categorias=CATEGORIAS_TICKET,
    )
