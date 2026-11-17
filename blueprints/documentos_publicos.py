"""
NEXO - Documentos legais públicos (Termos de Uso e Política de Privacidade)
==========================================================================
Rotas sem login, liberadas na trava de aceite (termos_legais.ENDPOINTS_LIBERADOS):
  GET /termos-de-uso               → a versão vigente dos Termos de Uso
  GET /politica-de-privacidade     → a versão vigente da Política de Privacidade
  GET /documentos-legais/vigentes  → JSON com a vigente de cada documento (a landing o lê ao abrir o aceite)

Regras:
  - O texto é lido do banco a cada requisição (configuracoes_conta.documento_vigente) e nada fica em
    cache (Cache-Control: no-store): a versão que o admin acabou de salvar vale na requisição seguinte.
  - Texto puro: a página o escapa (Jinja) e mantém as quebras de linha pelo CSS (white-space: pre-wrap); o
    JSON o entrega como string, e quem o mostra usa textContent, nunca innerHTML.
  - Nenhum texto jurídico vem pronto: sem versão publicada, a página avisa que o documento ainda não foi
    publicado (200) e o JSON devolve {"documentos": []}.
  - Banco fora ou esquema incompleto (revisões c7d41e9a2b60 e a8d3f6b91c24): a página avisa que o
    documento está indisponível (503) e o JSON responde 503 {"documentos": [], "erro": "indisponivel"};
    nunca 500. No log, só o tipo do erro.
  - Contrato do JSON com a landing e o microsserviço do Raio-X: cada item traz "tipo", "id"
    (documento_legal.id_documento, o que o lead envia como documento_termos e documento_privacidade),
    "rotulo" (v1.0), "publicado_em" (ISO 8601 em UTC) e "conteudo", só a vigente de cada tipo, na ordem
    TERMOS_USO, POLITICA_PRIVACIDADE.
"""

import logging

from flask import Blueprint, jsonify, make_response, render_template, url_for
from sqlalchemy.exc import SQLAlchemyError

from configuracoes_conta import TIPOS_DOCUMENTO, documento_vigente
from extensions import db
from horario import iso_utc
from preferencias_usuario import esquecer_esquema, esquema_configuracoes_disponivel

logger = logging.getLogger(__name__)

documentos_publicos_bp = Blueprint("documentos_publicos", __name__)

# Página pública de cada documento, na ordem de TIPOS_DOCUMENTO (as abas da página e os links das telas
# do admin e do cliente).
PAGINAS = {"TERMOS_USO": "documentos_publicos.termos_de_uso",
           "POLITICA_PRIVACIDADE": "documentos_publicos.politica_de_privacidade"}


class _Indisponivel(Exception):
    """Banco fora ou esquema incompleto: a resposta é o aviso de indisponível (503)."""


def _vigentes(tipos) -> dict:
    """{tipo: DocumentoLegal ou None}: a versão vigente de cada tipo, lida agora do banco."""
    try:
        if not esquema_configuracoes_disponivel():
            raise _Indisponivel()
        return {tipo: documento_vigente(tipo) for tipo in tipos}
    except SQLAlchemyError as erro:
        try:
            db.session.rollback()
        except SQLAlchemyError:
            pass
        # Uma coluna removida com o processo no ar (downgrade) também cai aqui: a presença do esquema é
        # conferida de novo na próxima requisição, como na trava de aceite.
        esquecer_esquema()
        logger.warning("Documentos legais públicos indisponíveis (%s).", type(erro).__name__)
        raise _Indisponivel() from None


def _sem_cache(resposta):
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


def _pagina(tipo: str):
    try:
        documento = _vigentes((tipo,))[tipo]
    except _Indisponivel:
        documento, estado, status = None, "indisponivel", 503
    else:
        estado, status = ("publicado" if documento is not None else "nao_publicado"), 200
    corpo = render_template("documentos_publicos/documento.html", titulo=TIPOS_DOCUMENTO[tipo], documento=documento,
                            estado=estado, paginas=[(PAGINAS[chave], TIPOS_DOCUMENTO[chave]) for chave in PAGINAS])
    return _sem_cache(make_response(corpo, status))


@documentos_publicos_bp.route("/termos-de-uso")
def termos_de_uso():
    return _pagina("TERMOS_USO")


@documentos_publicos_bp.route("/politica-de-privacidade")
def politica_de_privacidade():
    return _pagina("POLITICA_PRIVACIDADE")


@documentos_publicos_bp.route("/documentos-legais/vigentes")
def vigentes():
    try:
        documentos = _vigentes(TIPOS_DOCUMENTO)
    except _Indisponivel:
        return _sem_cache(make_response(jsonify(documentos=[], erro="indisponivel"), 503))
    return _sem_cache(make_response(jsonify(documentos=[
        {"tipo": tipo, "id": documento.id_documento, "rotulo": documento.rotulo_versao,
         "publicado_em": iso_utc(documento.publicado_em), "conteudo": documento.conteudo}
        for tipo, documento in documentos.items() if documento is not None
    ])))


@documentos_publicos_bp.app_template_global("url_documento_publico")
def url_documento_publico(tipo: str) -> str:
    """Endereço da página pública de um documento (link nas telas do admin e do cliente)."""
    return url_for(PAGINAS[tipo])
