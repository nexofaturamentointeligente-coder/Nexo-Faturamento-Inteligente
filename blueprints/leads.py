"""
NEXO - Avaliação de Cenário (blueprint `leads`, só consultores)
================================================================
Rotas (todas admin_required: na fase 1 o consultor envia em nome do lead; a captação pública
fica para depois da LGPD do lead, do anti-abuso e da capacidade, ver Docs/AVALIACAO_DE_CENARIO.md):
  GET  /admin/leads                  → leads avaliados e o formulário de nova avaliação
  POST /admin/leads                  → lê os relatórios do lead e grava a avaliação
  GET  /admin/leads/<id>             → resumo, triagem e os itens de cada bloco
  POST /admin/leads/<id>/reenviar    → troca os dados do lead pelos de um novo envio
  POST /admin/leads/<id>/retriar     → refaz marcas e triagem a partir dos dados gravados

Regras:
  - Mesma vaga única de upload/processamento das análises e do Raio-X (etl_uploads): ocupada,
    a resposta é 503 com Retry-After e a orientação de enviar de novo; nenhum arquivo é lido.
    O processo isolado tem vaga própria (etl_runtime): ocupada, a resposta é a mesma.
  - Antes do processo isolado (até 2 minutos), a transação da requisição é encerrada: nada fica
    travado nem segura conexão do pooler enquanto os arquivos são lidos.
  - 20 envios por hora por consultor, somando novo envio e reenvio.
"""

from datetime import timedelta
from decimal import Decimal
import json
import threading
import time
from weakref import WeakKeyDictionary

from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import SQLAlchemyError

import avaliacao_cenario
import avaliacao_motor
from blueprints.admin import admin_required
from configuracoes_conta import DadoInvalido, limpar_telefone
from contas_email import normalizar_email
from emails import email_valido
import entitlements
from etl_inputs import ETLInputError
from etl_uploads import vaga_de_entrada
from extensions import chave_limite_usuario, db, limiter
import leads_avaliacao
from leads_avaliacao import RETENCAO_DIAS
from models import BrainstormingDadosPdv, LeadAvaliacao
import raio_x
import triagem_cenario

leads_bp = Blueprint("leads", __name__, url_prefix="/admin/leads")

limite_envio = limiter.shared_limit("20 per hour", scope="avaliacao-lead", key_func=chave_limite_usuario)

MENSAGEM_OCUPADO = ("Há outro envio ou processamento em andamento. Aguarde alguns segundos e envie de novo: "
                    "nenhum arquivo foi lido.")
MENSAGEM_SEM_ESQUEMA = ("A Avaliação de Cenário depende das migrações 5e1b9c3a7d20 e 6a8f2d4c0e13, ainda não "
                        "aplicadas neste banco: nenhum arquivo foi lido.")
ESPERA_SEGUNDOS = 30
# Código do etl_runtime quando a vaga do processo isolado (_ADMISSION) está ocupada.
CODIGO_OCUPADO = "busy"
ITENS_POR_BLOCO = 30
LEADS_NA_LISTA = 100
_REVERIFICAR_ESQUEMA_SEGUNDOS = 30.0
_esquema_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()


def esquema_disponivel() -> bool:
    """True quando a staging ampliada (revisão 5e1b9c3a7d20) existe neste banco.

    O mesmo critério de validacao_pdv: a presença fica fixa para o engine, e a ausência é
    reavaliada a cada 30 s, para a migração aplicada valer sem reiniciar o processo. Vem antes de
    qualquer consulta às tabelas de lead: código publicado antes da migração mostra um aviso, e não
    erro 500.
    """
    engine = db.session.get_bind()
    estado = _esquema_por_engine.get(engine)
    if estado is True:
        return True
    if estado is not None and time.monotonic() - estado < _REVERIFICAR_ESQUEMA_SEGUNDOS:
        return False
    inspetor = inspect(db.session.connection())
    disponivel = (inspetor.has_table(BrainstormingDadosPdv.__tablename__) and "chave_produto" in
                  {coluna["name"] for coluna in inspetor.get_columns(BrainstormingDadosPdv.__tablename__)})
    _esquema_por_engine[engine] = True if disponivel else time.monotonic()
    return disponivel


def _sem_esquema():
    flash(MENSAGEM_SEM_ESQUEMA, "warning")
    return redirect(url_for("leads.lista"), 303)


# O expurgo de 30 dias também roda pela própria tela: sem o pg_cron (revisão 6a8f2d4c0e13), ou com
# o servidor dormindo na hora do job, a lista e o detalhe não mostram dado de PDV vencido. No
# máximo uma vez por hora por processo, a mesma regra de `flask leads expurgar-pdv --aplicar`; erro
# de banco nunca derruba a página.
EXPURGO_A_CADA_SEGUNDOS = 3600.0
_expurgo_trava = threading.Lock()
_expurgo_ultimo = {"instante": None}


def expurgo_oportunista() -> None:
    if not _expurgo_trava.acquire(blocking=False):
        return
    try:
        agora = time.monotonic()
        ultimo = _expurgo_ultimo["instante"]
        if ultimo is not None and agora - ultimo < EXPURGO_A_CADA_SEGUNDOS:
            return
        _expurgo_ultimo["instante"] = agora
        try:
            resultado = leads_avaliacao.expurgar_dados_pdv(db.session)
            db.session.commit()
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Expurgo dos dados de PDV dos leads não rodou (%s).", type(erro).__name__)
            return
        if resultado.linhas_removidas or resultado.leads_expirados:
            current_app.logger.info("Expurgo dos leads: %s linha(s) de PDV removidas, %s lead(s) expirados.",
                                    resultado.linhas_removidas, resultado.leads_expirados)
    finally:
        _expurgo_trava.release()


# =====================================================================
# Contexto
# =====================================================================
def _contexto_lista(formulario=None) -> dict:
    # Ordem da coluna "Avaliado em": a última autorização (renovada a cada reenvio) ou a criação.
    avaliado_em = func.coalesce(LeadAvaliacao.consentimento_em, LeadAvaliacao.criado_em)
    leads = db.session.scalars(select(LeadAvaliacao).order_by(avaliado_em.desc(), LeadAvaliacao.criado_em.desc())
                               .limit(LEADS_NA_LISTA)).all()
    return {"leads": leads, "formulario": formulario or {}, "max_arquivos": raio_x.MAX_ARQUIVOS,
            "rotulos_blocos": triagem_cenario.ROTULOS_DOS_BLOCOS, "retencao_dias": RETENCAO_DIAS,
            "disponivel": True,
            "total_leads": db.session.scalar(select(func.count()).select_from(LeadAvaliacao))}


def _objeto_json(texto):
    """O JSON gravado se for um objeto; qualquer outra coisa (lista, texto, JSON inválido) vale nada."""
    try:
        valor = json.loads(texto) if texto else None
    except ValueError:
        return None
    return valor if isinstance(valor, dict) else None


def _resumo(lead: LeadAvaliacao, bloco: str):
    return _objeto_json(getattr(lead, f"resumo_{bloco}_json"))


def _contexto_detalhe(lead: LeadAvaliacao) -> dict:
    """Resumo gravado e, enquanto os dados de PDV existirem, os itens de cada bloco com os motivos
    (a mesma triagem refeita sobre as linhas, sem gravar nada)."""
    registros = list(db.session.scalars(
        select(BrainstormingDadosPdv).where(BrainstormingDadosPdv.lead_id == lead.id)
        .order_by(BrainstormingDadosPdv.ano_exercicio, BrainstormingDadosPdv.chave_produto)
    ))
    meta = _objeto_json(lead.meta_json) or {}
    itens = {bloco: [] for bloco in triagem_cenario.BLOCOS}
    expira_em = None
    # Os motivos são a triagem refeita sobre as linhas, sem gravar: só batem com o bloco gravado
    # quando a triagem do lead é a da versão atual. Senão, a tela pede "Refazer a triagem".
    triagem_atual = (lead.versao_triagem == triagem_cenario.VERSAO_TRIAGEM
                     and lead.versao_regras == avaliacao_motor.VERSAO_REGRAS_LEAD)
    if registros:
        linhas = [avaliacao_cenario.linha_da_staging(r) for r in registros]
        motivos = {}
        if triagem_atual:
            try:
                motivos = triagem_cenario.triar(linhas, triagem_cenario.Cobertura.do_envio(meta)).motivos
            except ValueError:
                triagem_atual = False
        por_produto = {}
        for registro in registros:
            item = por_produto.setdefault(registro.chave_produto, {
                "nome": registro.descricao_produto, "codigo": registro.codigo_produto, "anos": [],
                "faturamento": Decimal("0"), "recuperavel": Decimal("0"), "bloco": registro.bloco_triagem,
                "motivos": [triagem_cenario.ROTULOS_DOS_MOTIVOS.get(m, m)
                            for m in motivos.get(registro.chave_produto, ())],
            })
            # Linhas em ordem de ano: o nome e o código ficam os do ano mais recente.
            item["nome"], item["codigo"] = registro.descricao_produto, registro.codigo_produto or item["codigo"]
            item["anos"].append(registro.ano_exercicio)
            item["faturamento"] += Decimal(registro.valor_venda_bruto)
            item["recuperavel"] += Decimal(registro.vazamento_estimado_reais)
        for item in por_produto.values():
            if item["bloco"] in itens:
                itens[item["bloco"]].append(item)
        for bloco in itens:
            itens[bloco].sort(key=lambda item: (-item["recuperavel"], -item["faturamento"], item["nome"]))
            itens[bloco] = itens[bloco][:ITENS_POR_BLOCO]
        expira_em = min(r.criado_em for r in registros) + timedelta(days=RETENCAO_DIAS)
    return {
        "lead": lead, "meta": meta, "itens": itens, "linhas_pdv": len(registros), "expira_em": expira_em,
        "triagem_atual": triagem_atual, "versao_triagem_atual": triagem_cenario.VERSAO_TRIAGEM,
        "resumos": {bloco: _resumo(lead, bloco) for bloco in triagem_cenario.BLOCOS},
        "rotulos_blocos": triagem_cenario.ROTULOS_DOS_BLOCOS, "rotulos_motivos": triagem_cenario.ROTULOS_DOS_MOTIVOS,
        "max_arquivos": raio_x.MAX_ARQUIVOS, "itens_por_bloco": ITENS_POR_BLOCO, "retencao_dias": RETENCAO_DIAS,
    }


def _ocupado(template: str, contexto: dict):
    """503 com Retry-After: a vaga única está com outro envio ou processamento."""
    flash(MENSAGEM_OCUPADO, "warning")
    resposta = make_response(render_template(template, **contexto), 503)
    resposta.headers["Retry-After"] = str(ESPERA_SEGUNDOS)
    return resposta


# =====================================================================
# Formulário
# =====================================================================
def _contato_do_formulario(formulario) -> dict:
    nome = " ".join((formulario.get("nome_contato") or "").split())
    loja = " ".join((formulario.get("nome_loja") or "").split())
    if not nome or not loja:
        raise DadoInvalido("Informe o nome do contato e o nome da loja.")
    if len(nome) > 120 or len(loja) > 120:
        raise DadoInvalido("O nome do contato e o da loja têm até 120 caracteres.")
    email = normalizar_email(formulario.get("email"))
    if not email_valido(email):
        raise DadoInvalido("Informe um e-mail válido.")
    telefone = limpar_telefone(formulario.get("whatsapp") or "")
    whatsapp = None
    if telefone:
        whatsapp = ("+" if telefone.startswith("+") else "") + "".join(c for c in telefone if c.isdigit())
    _exigir_autorizacao(formulario)
    return {"nome_contato": nome, "email": email, "whatsapp": whatsapp, "nome_loja": loja}


def _exigir_autorizacao(formulario) -> None:
    if formulario.get("autorizacao") != "1":
        raise DadoInvalido("Confirme que o lead autorizou o uso destes relatórios na avaliação.")


def _avaliar(arquivos, *, contato=None, lead_id=None) -> LeadAvaliacao:
    """Lê os relatórios no processo isolado e grava a avaliação (com commit)."""
    prata = entitlements.catalogo().nivel("PRATA")
    meses_prata = prata.limite_historico_meses if prata is not None else None
    consultor = current_user.id_usuario
    # Até 2 minutos no processo isolado: a transação de leitura termina antes, sem trava nem
    # conexão parada no pooler. A gravação abre outra, curta, depois.
    db.session.rollback()
    resultado = avaliacao_cenario.processar(arquivos)
    lead = avaliacao_cenario.registrar(resultado, contato=contato, lead_id=lead_id, registrado_por=consultor,
                                       meses_prata=meses_prata)
    db.session.commit()
    return lead


# =====================================================================
# Rotas
# =====================================================================
@leads_bp.route("")
@admin_required
def lista():
    if not esquema_disponivel():
        return render_template("admin/leads.html", disponivel=False, retencao_dias=RETENCAO_DIAS,
                               max_arquivos=raio_x.MAX_ARQUIVOS, leads=[], formulario={})
    expurgo_oportunista()
    return render_template("admin/leads.html", **_contexto_lista())


@leads_bp.route("", methods=["POST"])
@admin_required
@limite_envio
def avaliar():
    if not esquema_disponivel():
        return _sem_esquema()
    try:
        contato = _contato_do_formulario(request.form)
    except DadoInvalido as erro:
        flash(str(erro), "danger")
        return render_template("admin/leads.html", **_contexto_lista(request.form)), 422
    with vaga_de_entrada() as livre:
        if not livre:
            return _ocupado("admin/leads.html", _contexto_lista(request.form))
        try:
            lead = _avaliar(request.files.getlist("arquivos"), contato=contato)
        except ETLInputError as erro:
            db.session.rollback()
            if erro.code == CODIGO_OCUPADO:
                return _ocupado("admin/leads.html", _contexto_lista(request.form))
            flash(erro.safe_message, "danger")
            return render_template("admin/leads.html", **_contexto_lista(request.form)), 422
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Avaliação de lead não gravada (%s).", type(erro).__name__)
            flash("Não foi possível gravar a avaliação. Tente novamente.", "danger")
            return render_template("admin/leads.html", **_contexto_lista(request.form)), 503
    flash(f"Avaliação concluída: {lead.arquivos} relatório(s) de vendas.", "success")
    return redirect(url_for("leads.detalhe", lead_id=lead.id), 303)


@leads_bp.route("/<uuid:lead_id>")
@admin_required
def detalhe(lead_id):
    if not esquema_disponivel():
        return _sem_esquema()
    expurgo_oportunista()
    lead = db.session.get(LeadAvaliacao, lead_id)
    if lead is None:
        abort(404)
    return render_template("admin/lead.html", **_contexto_detalhe(lead))


@leads_bp.route("/<uuid:lead_id>/reenviar", methods=["POST"])
@admin_required
@limite_envio
def reenviar(lead_id):
    if not esquema_disponivel():
        return _sem_esquema()
    lead = db.session.get(LeadAvaliacao, lead_id)
    if lead is None:
        abort(404)
    try:
        _exigir_autorizacao(request.form)
    except DadoInvalido as erro:
        flash(str(erro), "danger")
        return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)
    with vaga_de_entrada() as livre:
        if not livre:
            return _ocupado("admin/lead.html", _contexto_detalhe(lead))
        try:
            lead = _avaliar(request.files.getlist("arquivos"), lead_id=lead_id)
        except (ETLInputError, avaliacao_cenario.LeadIndisponivel) as erro:
            db.session.rollback()
            if getattr(erro, "code", None) == CODIGO_OCUPADO:
                return _ocupado("admin/lead.html", _contexto_detalhe(lead))
            flash(getattr(erro, "safe_message", None) or str(erro), "danger")
            return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Reenvio de lead não gravado (%s).", type(erro).__name__)
            flash("Não foi possível gravar a avaliação. Tente novamente.", "danger")
            return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)
    flash(f"Avaliação refeita com {lead.arquivos} relatório(s) de vendas.", "success")
    return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)


@leads_bp.route("/<uuid:lead_id>/retriar", methods=["POST"])
@admin_required
def retriar(lead_id):
    if not esquema_disponivel():
        return _sem_esquema()
    try:
        avaliacao_cenario.retriar(lead_id)
        db.session.commit()
    except avaliacao_cenario.LeadIndisponivel as erro:
        db.session.rollback()
        flash(str(erro), "warning")
    except ETLInputError as erro:
        db.session.rollback()
        flash(erro.safe_message, "warning")
    except SQLAlchemyError as erro:
        db.session.rollback()
        current_app.logger.warning("Triagem de lead não refeita (%s).", type(erro).__name__)
        flash("Não foi possível refazer a triagem. Tente novamente.", "danger")
    else:
        flash("Triagem refeita com as regras atuais.", "success")
    return redirect(url_for("leads.detalhe", lead_id=lead_id), 303)
