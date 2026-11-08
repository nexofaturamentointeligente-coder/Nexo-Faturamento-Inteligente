"""Aceite obrigatório dos Termos de Uso e da Política de Privacidade.

O texto e as versões ficam em configuracoes_conta.py. Aqui ficam a trava de navegação do
CLIENTE, o aceite com IP, a auditoria do admin e o alerta de pendência.

Regras:
  - Nenhum texto jurídico vem pronto: sem documento publicado, não há o que aceitar e
    ninguém é barrado. Toda versão nova pede novo aceite, seja revisão ou nova versão.
  - A trava falha aberta: sem as tabelas, ou com erro de banco, a página segue.
  - Com aceite pendente continuam abertos sair, aceitar e os direitos do titular (LGPD,
    art. 18): exportar os dados e pedir a exclusão não dependem de concordar com o texto.
  - O instante é gravado em UTC e exibido no horário de Brasília. O IP vem da requisição,
    já corrigido pelo ProxyFix, e nunca entra no log.
  - As funções de escrita não fazem commit: a rota confirma ou reverte.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import ipaddress
import logging

from flask import after_this_request, current_app, flash, g, has_request_context, jsonify, make_response, redirect, \
    render_template, request, url_for
from flask_login import current_user
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from configuracoes_conta import TIPOS_DOCUMENTO, DadoInvalido, _inteiro, exigir_esquema, rotulo_versao
from extensions import db
from horario import agora_utc
from models import AceiteDocumento, DocumentoLegal, Empresa, Notificacao, Usuario
from notifications import criar_notificacao
from preferencias_usuario import esquecer_esquema, esquema_configuracoes_disponivel
from session_security import condicoes_empresa_acessivel

logger = logging.getLogger(__name__)

# Abertos com aceite pendente. O blueprint auth inteiro também (login, senha, ativação):
# não mostra dado do portal, e o primeiro acesso vem antes do aceite.
ENDPOINTS_LIBERADOS = frozenset({
    # como_aderir fica de fora: com sessão, ela é desenhada dentro do portal (base.html).
    "static", "index", "healthz", "healthz_db",
    "cliente.aceitar_termos", "cliente.configuracoes_aceitar_documento",
    "cliente.configuracoes_exportar_dados", "cliente.configuracoes_solicitar_exclusao",
})
MENSAGEM_API = "Para continuar, aceite os Termos de Uso e a Política de Privacidade no portal."
_ORDEM_TIPOS = {tipo: indice for indice, tipo in enumerate(TIPOS_DOCUMENTO)}
_TAMANHO_MAXIMO_IP = 45


@dataclass(frozen=True)
class Vigente:
    id_documento: int
    tipo: str
    rotulo_versao: str
    publicado_em: datetime

    @property
    def rotulo(self) -> str:
        return TIPOS_DOCUMENTO[self.tipo]


def _vigentes(*condicoes, id_usuario=None):
    """Versão vigente de cada documento publicado, sem o texto. Com `id_usuario`, só as
    que esta pessoa ainda não aceitou. Uma consulta."""
    ultima = (select(DocumentoLegal.tipo, func.max(DocumentoLegal.versao).label("versao"))
              .group_by(DocumentoLegal.tipo).subquery())
    consulta = (select(DocumentoLegal.id_documento, DocumentoLegal.tipo, DocumentoLegal.versao_maior,
                       DocumentoLegal.versao_menor, DocumentoLegal.publicado_em)
                .join(ultima, and_(DocumentoLegal.tipo == ultima.c.tipo, DocumentoLegal.versao == ultima.c.versao)))
    if id_usuario is not None:
        consulta = consulta.outerjoin(AceiteDocumento, and_(
            AceiteDocumento.id_documento == DocumentoLegal.id_documento, AceiteDocumento.id_usuario == id_usuario,
        )).where(AceiteDocumento.id_aceite.is_(None))
    linhas = db.session.execute(consulta.where(*condicoes)).all()
    return sorted((Vigente(linha.id_documento, linha.tipo, rotulo_versao(linha.versao_maior, linha.versao_menor),
                           linha.publicado_em) for linha in linhas),
                  key=lambda vigente: _ORDEM_TIPOS.get(vigente.tipo, len(_ORDEM_TIPOS)))


def vigentes() -> list[Vigente]:
    return _vigentes()


def pendentes(id_usuario: int) -> list[Vigente]:
    """As versões vigentes que este usuário ainda não aceitou."""
    return _vigentes(id_usuario=id_usuario)


def pendentes_da_sessao() -> list[Vigente]:
    """Pendências do usuário da sessão, uma consulta por requisição. Sem cache entre
    requisições: a versão que o admin acabou de publicar vale no clique seguinte."""
    if "_nexo_termos_pendentes" not in g:
        g._nexo_termos_pendentes = pendentes(current_user.id_usuario) if esquema_configuracoes_disponivel() else []
    return g._nexo_termos_pendentes


def textos(pendencias) -> list[dict]:
    """Os documentos completos, só para desenhar o modal (texto puro, autoescapado)."""
    ids = [pendencia.id_documento for pendencia in pendencias]
    documentos = {documento.id_documento: documento for documento in db.session.scalars(
        select(DocumentoLegal).where(DocumentoLegal.id_documento.in_(ids)))}
    return [{"id_documento": documento.id_documento, "rotulo": TIPOS_DOCUMENTO[documento.tipo],
             "rotulo_versao": documento.rotulo_versao, "publicado_em": documento.publicado_em,
             "conteudo": documento.conteudo}
            for documento in (documentos.get(identificador) for identificador in ids) if documento is not None]


# ---------------------------------------------------------------------------
# Trava de navegação
# ---------------------------------------------------------------------------
def trava_de_aceite():
    """before_request do app (depois do primeiro acesso): o CLIENTE com versão vigente
    sem aceite recebe só o modal, no lugar de qualquer página do portal."""
    endpoint = request.endpoint
    if endpoint is None or endpoint in ENDPOINTS_LIBERADOS or request.blueprint == "auth":
        return None  # sem rota casada (404/405) segue para o erro de sempre
    if request.method == "OPTIONS" or not current_user.is_authenticated or not current_user.is_cliente:
        return None
    if getattr(current_user, "primeiro_acesso", False):
        return None  # a troca de senha vem antes (_guard_primeiro_acesso)
    try:
        pendencias = pendentes_da_sessao()
    except SQLAlchemyError as erro:
        # Sem esta saída, um banco com a coluna recém-removida (downgrade com o processo
        # no ar) derrubaria todas as páginas do cliente: reconfere o esquema na próxima.
        db.session.rollback()
        esquecer_esquema()
        g._nexo_termos_pendentes = []
        logger.warning("Trava de aceite dos termos indisponível (%s).", type(erro).__name__)
        return None
    if not pendencias:
        return None
    return _resposta_da_trava(pendencias)


def _resposta_da_trava(pendencias):
    if request.path.startswith("/api/"):
        # O NexoBot mostra o campo "resposta" de um JSON 4xx; 401 e 403 ele lê como sessão
        # expirada (static/js/nexobot.js). 428: falta uma condição, o aceite.
        return jsonify(erro=MENSAGEM_API, resposta=MENSAGEM_API, fonte="local", acao=None, destino=None), 428
    if request.endpoint == "midia":
        return "", 403
    if request.method not in ("GET", "HEAD"):
        # O formulário enviado se perde: a página do aceite vem no lugar, com o aviso.
        flash("Sua última ação não foi concluída: aceite os documentos e faça-a de novo.", "warning")
        return redirect(url_for("cliente.dashboard"), 303)
    proximo = request.full_path.rstrip("?")
    if request.endpoint == "cliente.analise_pdf":
        # Voltar a um download deixaria o modal na tela: volta à página da análise.
        proximo = url_for("cliente.analise", id_analise=request.view_args["id_analise"])
    resposta = make_response(render_template(
        "cliente/termos_pendentes.html", bloqueio_termos=True, termos_documentos=textos(pendencias),
        termos_proximo=proximo,
    ))
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


# ---------------------------------------------------------------------------
# Aceite
# ---------------------------------------------------------------------------
def ip_do_cliente(endereco) -> str | None:
    """IP normalizado de request.remote_addr (o ProxyFix já escolheu o salto confiável).
    Nunca lê X-Forwarded-For: seria forjável. Inválido ou vazio vira None."""
    try:
        ip = ipaddress.ip_address((endereco or "").strip())
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    texto = ip.compressed
    return texto if len(texto) <= _TAMANHO_MAXIMO_IP else None


def aceitar_pendentes(usuario: Usuario, ids_exibidos, ip: str | None) -> int:
    """Grava, sem commit, o aceite de todas as versões pendentes, com o mesmo instante e IP.

    `ids_exibidos` são os documentos que estavam no modal. Se o admin publicou outra
    versão enquanto a pessoa lia, a nova não estava na tela: nada é gravado.
    Devolve quantos aceites foram criados (0 se já não havia pendência).
    """
    exigir_esquema()
    exibidos = set()
    for valor in ids_exibidos:
        identificador = _inteiro(valor)
        if identificador is None:
            raise DadoInvalido("Documento inválido.")
        exibidos.add(identificador)
    pendencias = pendentes(usuario.id_usuario)
    if not {pendencia.id_documento for pendencia in pendencias} <= exibidos:
        raise DadoInvalido("Os documentos foram atualizados enquanto você lia. Leia a versão atual e confirme de novo.")
    instante = agora_utc()
    for pendencia in pendencias:
        db.session.add(AceiteDocumento(id_usuario=usuario.id_usuario, id_documento=pendencia.id_documento,
                                       aceito_em=instante, ip_usuario=ip))
    return len(pendencias)


# ---------------------------------------------------------------------------
# Auditoria e alerta
# ---------------------------------------------------------------------------
def _clientes_elegiveis(instante):
    """Quem passa pela trava hoje: CLIENTE ativo, com a senha definida, de empresa com
    acesso ao portal. Os demais não conseguem aceitar e ficam fora da adesão."""
    return (Usuario.role == "CLIENTE", Usuario.ativo.is_(True), Usuario.primeiro_acesso.is_(False),
            select(Empresa.id_empresa).where(Empresa.id_empresa == Usuario.id_empresa,
                                             *condicoes_empresa_acessivel(instante)).exists())


def _situacao(usuario: Usuario, empresa_acessivel: bool) -> str | None:
    if not usuario.ativo:
        return "Usuário desativado"
    if usuario.primeiro_acesso:
        return "Ainda não definiu a senha"
    if not empresa_acessivel:
        return "Empresa sem acesso ao portal"
    return None


@dataclass
class LinhaAuditoria:
    usuario: Usuario
    empresa: Empresa
    situacao: str | None
    aceites: dict = field(default_factory=dict)
    pendentes: list = field(default_factory=list)
    pendente_desde: datetime | None = None
    dias_pendente: int | None = None
    atrasado: bool = False

    @property
    def elegivel(self) -> bool:
        return self.situacao is None

    @property
    def status(self) -> str:
        return "PENDENTE" if self.pendentes else "ACEITO"


def auditoria(instante: datetime | None = None) -> dict:
    """Status de cada CLIENTE diante das versões vigentes, com a adesão por documento.

    Só leitura: a página é GET. Pendência conta da publicação da versão, e passa a
    atrasada depois de TERMOS_ALERTA_PENDENCIA_DIAS.
    """
    instante = instante or agora_utc()
    dias_alerta = current_app.config["TERMOS_ALERTA_PENDENCIA_DIAS"]
    if not esquema_configuracoes_disponivel():
        return {"disponivel": False, "vigentes": [], "linhas": [], "adesao": [], "dias_alerta": dias_alerta,
                "elegiveis": 0, "atrasados": 0}
    atuais = vigentes()
    usuarios = db.session.execute(
        select(Usuario, Empresa).join(Empresa, Empresa.id_empresa == Usuario.id_empresa)
        .where(Usuario.role == "CLIENTE")
        .order_by(Empresa.razao_social, Usuario.nome, Usuario.id_usuario)
    ).all()
    acessiveis = set(db.session.scalars(select(Empresa.id_empresa).where(*condicoes_empresa_acessivel(instante))))
    # O aceite mais recente de cada pessoa por documento: a maior versão aceita.
    recentes = {}
    for aceite in db.session.execute(
            select(AceiteDocumento.id_usuario, AceiteDocumento.id_documento, AceiteDocumento.aceito_em,
                   AceiteDocumento.ip_usuario, DocumentoLegal.tipo, DocumentoLegal.versao_maior,
                   DocumentoLegal.versao_menor)
            .join(DocumentoLegal, DocumentoLegal.id_documento == AceiteDocumento.id_documento)
            .join(Usuario, Usuario.id_usuario == AceiteDocumento.id_usuario)
            .where(Usuario.role == "CLIENTE")
            .order_by(DocumentoLegal.versao, AceiteDocumento.aceito_em)):
        recentes[(aceite.id_usuario, aceite.tipo)] = {
            "id_documento": aceite.id_documento, "aceito_em": aceite.aceito_em, "ip": aceite.ip_usuario,
            "rotulo": rotulo_versao(aceite.versao_maior, aceite.versao_menor),
        }
    linhas = []
    for usuario, empresa in usuarios:
        linha = LinhaAuditoria(usuario=usuario, empresa=empresa,
                               situacao=_situacao(usuario, empresa.id_empresa in acessiveis))
        for vigente in atuais:
            aceite = recentes.get((usuario.id_usuario, vigente.tipo))
            linha.aceites[vigente.tipo] = None if aceite is None else {
                **aceite, "vigente": aceite["id_documento"] == vigente.id_documento}
            if aceite is None or aceite["id_documento"] != vigente.id_documento:
                linha.pendentes.append(vigente)
        if linha.pendentes:
            linha.pendente_desde = min(pendencia.publicado_em for pendencia in linha.pendentes)
            linha.dias_pendente = max((instante - linha.pendente_desde).days, 0)
            linha.atrasado = instante - linha.pendente_desde > timedelta(days=dias_alerta)
        linhas.append(linha)
    elegiveis = [linha for linha in linhas if linha.elegivel]
    adesao = []
    for vigente in atuais:
        aceitaram = sum(1 for linha in elegiveis if vigente not in linha.pendentes)
        adesao.append({"vigente": vigente, "aceitaram": aceitaram, "total": len(elegiveis),
                       "percentual": round(100 * aceitaram / len(elegiveis)) if elegiveis else None})
    return {"disponivel": True, "vigentes": atuais, "linhas": linhas, "adesao": adesao, "dias_alerta": dias_alerta,
            "elegiveis": len(elegiveis), "atrasados": sum(1 for linha in elegiveis if linha.atrasado)}


def _algum_sem_aceite(documentos):
    """A pessoa (Usuario da consulta externa) não aceitou ao menos um destes documentos."""
    return or_(*[~select(AceiteDocumento.id_aceite).where(
        AceiteDocumento.id_usuario == Usuario.id_usuario,
        AceiteDocumento.id_documento == documento.id_documento).exists() for documento in documentos])


def _vencidas(instante: datetime) -> list[Vigente]:
    limite = instante - timedelta(days=current_app.config["TERMOS_ALERTA_PENDENCIA_DIAS"])
    return [vigente for vigente in vigentes() if vigente.publicado_em < limite]


def contar_pendencias_atrasadas(instante: datetime | None = None) -> int:
    """Clientes que já deviam ter aceitado (faixa do painel do admin). Só leitura; uma
    falha de banco some com a faixa, não com o painel."""
    try:
        if not esquema_configuracoes_disponivel():
            return 0
        instante = instante or agora_utc()
        vencidas = _vencidas(instante)
        if not vencidas:
            return 0
        return db.session.scalar(select(func.count()).select_from(Usuario).where(
            *_clientes_elegiveis(instante), _algum_sem_aceite(vencidas)))
    except SQLAlchemyError as erro:
        db.session.rollback()
        esquecer_esquema()
        logger.warning("Contagem de aceites pendentes indisponível (%s).", type(erro).__name__)
        return 0


def texto_alerta(vigente: Vigente, dias: int) -> str:
    """Estável por versão: é o registro de que o admin já foi avisado."""
    return (f"Aceite pendente há mais de {dias} dias: {vigente.rotulo} {vigente.rotulo_versao}. "
            "Veja quem falta em Termos e aceites.")


def alertar_pendencias_atrasadas(id_admin: int, instante: datetime | None = None) -> int:
    """Um aviso no sininho deste admin por versão com cliente pendente além do prazo.

    Sem dado pessoal no texto. Sem commit. Rodar de novo não repete: o texto da
    notificação, estável por versão, é o registro.
    """
    if not esquema_configuracoes_disponivel():
        return 0
    instante = instante or agora_utc()
    dias = current_app.config["TERMOS_ALERTA_PENDENCIA_DIAS"]
    link = current_app.url_map.bind("localhost").build("admin.termos_aceites")
    criados = 0
    for vigente in _vencidas(instante):
        pendente = db.session.scalar(select(Usuario.id_usuario).where(
            *_clientes_elegiveis(instante), _algum_sem_aceite([vigente])).limit(1))
        if pendente is None:
            continue
        texto = texto_alerta(vigente, dias)
        if db.session.scalar(select(Notificacao.id_notificacao).where(
                Notificacao.id_usuario == id_admin, Notificacao.texto == texto).limit(1)) is not None:
            continue
        criar_notificacao(id_admin, texto, link)
        criados += 1
    return criados


def ao_entrar(_app, user, **_extra):
    """Sinal user_logged_in: ao entrar, o admin recebe no sininho as pendências vencidas.

    Roda depois da resposta do login, que já confirmou a sessão; qualquer falha aqui só
    fica no log e nunca impede a entrada.
    """
    if not has_request_context() or not getattr(user, "is_admin", False):
        return
    id_admin = user.id_usuario

    @after_this_request
    def _avisar(resposta):
        try:
            if current_user.is_authenticated and current_user.id_usuario == id_admin \
                    and alertar_pendencias_atrasadas(id_admin):
                db.session.commit()
        except Exception as erro:  # noqa: BLE001
            db.session.rollback()
            logger.warning("Aviso de aceite pendente não gerado (%s).", type(erro).__name__)
        return resposta
