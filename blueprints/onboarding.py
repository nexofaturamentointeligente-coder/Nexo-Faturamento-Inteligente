"""
NEXO - Planos e Raio-X de entrada (blueprint `onboarding`)
===========================================================
Rotas (todas exigem login de CLIENTE; o acesso por plano vem de entitlements):
  GET  /onboarding/diagnostico             → Raio-X do PDV: envio e último resultado (raio_x_entrada)
  POST /onboarding/diagnostico             → lê os relatórios e volta ao GET (raio_x_entrada)
  GET  /onboarding/diagnostico/auditoria   → auditoria de custos e de precificação (auditoria_abaixo_custo)
  GET  /onboarding/planos                  → níveis, funcionalidades e como mudar de plano
  GET  /api/onboarding/diagnostico         → último Raio-X em JSON (raio_x_entrada)
  POST /api/onboarding/diagnostico         → lê os relatórios e devolve o Raio-X em JSON, 201
  GET  /api/onboarding/diagnostico/auditoria → auditoria em JSON; 403 com o plano necessário

Regras:
  - O cálculo roda no processo isolado do ETL (raio_x.py); os arquivos não são guardados,
    só o resultado, um por empresa (raio_x_servico.py).
  - O que o plano não libera sai do resultado antes do template e do JSON
    (raio_x_servico.visao): o desfoque da tela é desenho sobre um marcador.
  - Mesma vaga única de upload/processamento das análises (etl_uploads.vaga_de_entrada) e
    limite de 10 envios por hora por usuário, somando a página e a API. Com a vaga ocupada
    (a de entrada ou a do processo isolado, etl_runtime), a API responde 503 com Retry-After
    e a página avisa para enviar de novo; nada é gravado.
"""

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import SQLAlchemyError

from blueprints.cliente import cliente_required
from contract_policy import ESTADO_ATIVA, obter_situacao_contratual
import entitlements
from entitlements import requer_funcionalidade
from etl_inputs import ETLInputError
from etl_uploads import vaga_de_entrada
from extensions import chave_limite_usuario, db, limiter
import raio_x
import raio_x_servico

onboarding_bp = Blueprint("onboarding", __name__)

# Página e API somam o mesmo limite: cada envio ocupa o processo isolado por até 2 minutos.
limite_raio_x = limiter.shared_limit("10 per hour", scope="raio-x", key_func=chave_limite_usuario)

MENSAGEM_OCUPADO = "Há outro envio ou processamento em andamento. Tente novamente em instantes."
ESPERA_SEGUNDOS = 30
# Código do etl_runtime quando a vaga do processo isolado (_ADMISSION) está ocupada: a resposta é
# a mesma da vaga de entrada (etl_uploads._UPLOAD_ADMISSION).
CODIGO_OCUPADO = "busy"


@onboarding_bp.after_request
def _cabecalhos_api(resposta):
    """JSON com dados da empresa: sem cache e sem MIME sniffing, como a API do NexoBot."""
    if request.path.startswith("/api/"):
        resposta.headers["Cache-Control"] = "no-store"
        resposta.headers["X-Content-Type-Options"] = "nosniff"
    return resposta


def _erro_json(mensagem: str, status: int, **extra):
    return jsonify(erro=mensagem, resposta=mensagem, fonte="local", acao=None, destino=None, **extra), status


def _ocupado_json():
    """503 com Retry-After: a fila de espera do cliente. A vaga costuma liberar em segundos
    (no máximo 2 minutos)."""
    resposta, status = _erro_json(MENSAGEM_OCUPADO, 503)
    resposta.headers["Retry-After"] = str(ESPERA_SEGUNDOS)
    return resposta, status


# =====================================================================
# Contexto comum
# =====================================================================
def _plano_info(nivel):
    return None if nivel is None else {"codigo": nivel.codigo, "nome": nivel.nome}


def _contexto_raio_x() -> dict:
    """O último Raio-X da empresa logada, só com o que o plano libera, e os planos envolvidos."""
    empresa = current_user.empresa
    dados = entitlements.catalogo()
    liberadas = entitlements.funcionalidades_da_empresa(empresa.id_empresa)
    atual = entitlements.nivel_da_empresa(empresa)
    limite = atual.limite_historico_meses if atual is not None else (
        dados.niveis[0].limite_historico_meses if dados.niveis else None)
    contexto = {
        "empresa": empresa, "plano_atual": atual, "raio_x": None, "linha_raio_x": None, "recomendacao": None,
        "plano_margem": dados.plano_necessario("margem_bruta"),
        "plano_auditoria": dados.plano_necessario("auditoria_abaixo_custo"),
        "plano_precificacao": dados.plano_necessario("alerta_precificacao"),
        "plano_cmv": dados.plano_necessario("diagnostico_cmv"),
        "plano_historico": None, "url_upgrade": entitlements.url_upgrade,
    }
    registro = raio_x_servico.ultimo(empresa.id_empresa)
    if registro is None:
        return contexto
    linha, bruto = registro
    visao = raio_x_servico.visao(bruto, liberadas, limite)
    recomendado = dados.nivel(bruto["recomendacao"]["plano"])
    upgrade = (recomendado is not None and atual is not None
               and recomendado.nivel_hierarquico > atual.nivel_hierarquico)
    contexto.update({
        "raio_x": visao, "linha_raio_x": linha,
        "recomendacao": {"plano": recomendado, "motivos": bruto["recomendacao"]["motivos"], "upgrade": upgrade,
                         "url": entitlements.url_upgrade(recomendado) if upgrade else None},
        # O menor plano acima do atual cujo corte, pelo calendário, não esconde nenhum relatório enviado.
        "plano_historico": next((n for n in dados.niveis if atual is not None
                                 and n.nivel_hierarquico > atual.nivel_hierarquico
                                 and raio_x_servico.mostra_todas_as_fatias(bruto["fatias"], n.limite_historico_meses)),
                                None),
    })
    return contexto


def _json_raio_x(contexto: dict) -> dict:
    linha, visao, recomendacao = contexto["linha_raio_x"], contexto["raio_x"], contexto["recomendacao"]
    return {
        "gerado_em": linha.gerado_em.isoformat(),
        "plano_atual": _plano_info(contexto["plano_atual"]),
        "recomendacao": {
            "plano": _plano_info(recomendacao["plano"]), "motivos": recomendacao["motivos"],
            "upgrade": recomendacao["upgrade"], "url_upgrade": recomendacao["url"],
        },
        "resultado": visao,
    }


def _executar_raio_x() -> int:
    """Lê os relatórios, confere de novo a assinatura e grava o resultado. Devolve quantos
    arquivos foram lidos. Erros de entrada saem como ETLInputError, com mensagem segura."""
    # Até 2 minutos no processo isolado: a transação de leitura da requisição termina antes,
    # sem segurar conexão do pooler; a gravação abre outra, curta, depois.
    db.session.rollback()
    resultado, nomes = raio_x_servico.processar_envio(request.files.getlist("arquivos"))
    empresa = current_user.empresa
    # A leitura pode levar até 2 minutos: a situação da empresa é relida antes de gravar.
    if obter_situacao_contratual(empresa, atualizar=True).estado != ESTADO_ATIVA:
        raise ETLInputError("assinatura_inativa", "A assinatura da empresa não está ativa: o Raio-X não foi "
                            "gravado. Fale com a equipe NEXO pelo Suporte.")
    prata = entitlements.catalogo().nivel("PRATA")
    recomendacao = raio_x.recomendar(resultado, prata.limite_historico_meses if prata is not None else None)
    raio_x_servico.salvar(empresa.id_empresa, current_user.id_usuario, resultado, recomendacao, nomes)
    db.session.commit()
    return len(nomes)


# =====================================================================
# Páginas
# =====================================================================
@onboarding_bp.route("/onboarding/diagnostico")
@cliente_required
@requer_funcionalidade("raio_x_entrada")
def diagnostico():
    """Raio-X do PDV: formulário de envio e o último resultado, no que o plano libera."""
    return render_template("onboarding/diagnostico.html", max_arquivos=raio_x.MAX_ARQUIVOS,
                           **_contexto_raio_x())


@onboarding_bp.route("/onboarding/diagnostico", methods=["POST"])
@cliente_required
@limite_raio_x
@requer_funcionalidade("raio_x_entrada")
def diagnostico_enviar():
    with vaga_de_entrada() as livre:
        if not livre:
            flash(MENSAGEM_OCUPADO, "warning")
            return redirect(url_for("onboarding.diagnostico"), 303)
        try:
            quantidade = _executar_raio_x()
        except ETLInputError as erro:
            db.session.rollback()
            if erro.code == CODIGO_OCUPADO:
                flash(MENSAGEM_OCUPADO, "warning")
            else:
                flash(erro.safe_message, "danger")
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Raio-X não gravado (%s).", type(erro).__name__)
            flash("Não foi possível gravar o Raio-X. Tente novamente.", "danger")
        else:
            flash(f"Raio-X concluído com {quantidade} relatório(s) de vendas.", "success")
    return redirect(url_for("onboarding.diagnostico"), 303)


@onboarding_bp.route("/onboarding/diagnostico/auditoria")
@cliente_required
@requer_funcionalidade("auditoria_abaixo_custo")
def auditoria():
    """Vendas abaixo do custo, margem por item e precificação desatualizada do último Raio-X."""
    return render_template("onboarding/auditoria.html", **_contexto_raio_x())


@onboarding_bp.route("/onboarding/planos")
@cliente_required
def planos():
    """Os níveis, o que cada um libera e como pedir a mudança de plano (sem preços)."""
    try:
        if not entitlements.esquema_planos_disponivel():
            return render_template("onboarding/planos.html", disponivel=False), 503
        empresa = current_user.empresa
        linhas = entitlements.catalogo_planos()
        atual = entitlements.nivel_da_empresa(empresa)
        assinatura = entitlements.assinatura_da_empresa(empresa)
    except SQLAlchemyError as erro:
        # Esquema removido com o processo no ar (o cache ainda o dava por presente) ou erro
        # passageiro do banco: confere de novo para não dizer "não ativados" a quem tem plano.
        entitlements.registrar_falha_de_banco(erro)
        try:
            ausente = not entitlements.esquema_planos_disponivel()
        except SQLAlchemyError as outro:
            entitlements.registrar_falha_de_banco(outro)
            ausente = False
        return render_template("onboarding/planos.html", disponivel=False,
                               mensagem=None if ausente else entitlements.MENSAGEM_VERIFICACAO), 503
    return render_template("onboarding/planos.html", disponivel=True, linhas=linhas, atual=atual,
                           assinatura=assinatura)


# =====================================================================
# API (JSON)
# =====================================================================
@onboarding_bp.route("/api/onboarding/diagnostico")
@cliente_required
@requer_funcionalidade("raio_x_entrada")
def api_diagnostico():
    contexto = _contexto_raio_x()
    if contexto["raio_x"] is None:
        return _erro_json("Nenhum Raio-X foi enviado ainda.", 404)
    return jsonify(_json_raio_x(contexto))


@onboarding_bp.route("/api/onboarding/diagnostico", methods=["POST"])
@cliente_required
@limite_raio_x
@requer_funcionalidade("raio_x_entrada")
def api_diagnostico_enviar():
    with vaga_de_entrada() as livre:
        if not livre:
            return _ocupado_json()
        try:
            _executar_raio_x()
        except ETLInputError as erro:
            db.session.rollback()
            if erro.code == CODIGO_OCUPADO:
                return _ocupado_json()
            return _erro_json(erro.safe_message, 422, codigo=erro.code)
        except SQLAlchemyError as erro:
            db.session.rollback()
            current_app.logger.warning("Raio-X não gravado (%s).", type(erro).__name__)
            return _erro_json("Não foi possível gravar o Raio-X. Tente novamente.", 503)
    return jsonify(_json_raio_x(_contexto_raio_x())), 201


@onboarding_bp.route("/api/onboarding/diagnostico/auditoria")
@cliente_required
@requer_funcionalidade("auditoria_abaixo_custo")
def api_auditoria():
    contexto = _contexto_raio_x()
    if contexto["raio_x"] is None:
        return _erro_json("Nenhum Raio-X foi enviado ainda.", 404)
    visao = contexto["raio_x"]
    return jsonify({
        "gerado_em": contexto["linha_raio_x"].gerado_em.isoformat(),
        "plano_atual": _plano_info(contexto["plano_atual"]),
        "auditoria": visao["auditoria"], "recuperavel": visao["recuperavel"], "protegido": visao["protegido"],
    })
