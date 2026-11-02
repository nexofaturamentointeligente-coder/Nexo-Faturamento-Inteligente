"""
NEXO - COM-002 (AUD-LANDING-001) | Avaliações dos clientes e prova social
=========================================================================
Regras do módulo; as rotas (blueprints/avaliacoes.py) só leem o formulário,
confirmam a transação e escolhem a resposta. Como em company_security e
contract_admin, nada aqui faz commit: o chamador confirma ou reverte tudo.

Princípios:
  - Identidade e empresa vêm SEMPRE do usuário da sessão; nenhum id enviado
    pelo navegador é lido nas operações do cliente.
  - Publicar exige aprovação E consentimento vigente, e cada operação de
    escrita bloqueia a linha (FOR UPDATE) antes de decidir: uma aprovação
    concorrente não restaura o que a retirada acabou de tirar do ar.
  - O moderador decide sobre a versão que leu (`versao`): se o cliente editou
    depois, a decisão é recusada e o texto novo precisa ser lido.
  - A sugestão privada nunca entra no DTO público nem em logs ou eventos.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time as hora_do_dia, timedelta
from time import monotonic
from weakref import WeakKeyDictionary

from flask import current_app
from sqlalchemy import case, delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from contract_policy import ESTADO_ATIVA, agora, data_local, obter_situacao_contratual
from extensions import db
from horario import BRASILIA, UTC, agora_utc, horario_brasilia, iso_utc
from models import (
    COMENTARIO_PUBLICO_MAX, NOME_EXIBICAO_MAX, STATUS_MODERACAO, STATUS_SUGESTAO, SUGESTAO_PRIVADA_MAX,
    Analise, AvaliacaoCliente, Empresa, EventoAvaliacao, RelatorioAnalise, SolicitacaoAvaliacao,
    SugestaoMelhoria, Usuario,
)
from session_security import condicoes_empresa_acessivel

ROTULOS_NOTA = {
    1: "Muito insatisfeito",
    2: "Insatisfeito",
    3: "Regular",
    4: "Satisfeito",
    5: "Muito satisfeito",
}
COMENTARIO_PUBLICO_MIN = 5
MOTIVO_MIN = 3
MOTIVO_MAX = 500
IDENTIFICACAO_ANONIMA = "Cliente NEXO"
DEPOIMENTOS_NO_CARROSSEL = 12
POR_PAGINA = 20

# Texto e versão do aviso exibido no formulário. Mudou o texto, muda a versão:
# a versão fica gravada junto do consentimento como evidência do que foi lido.
TEXTO_CONSENTIMENTO = (
    "Autorizo a NEXO a publicar meu comentário e minha avaliação em seus canais "
    "oficiais, conforme a Política de Privacidade."
)
VERSAO_AVISO_CONSENTIMENTO = "2026-11-01"

MENSAGEM_SUCESSO = "Obrigado pela sua avaliação! Sua opinião é muito importante para a evolução da NEXO."

ROTULOS_MODERACAO = {"PENDENTE": "Pendente", "APROVADA": "Aprovada", "REJEITADA": "Rejeitada"}
ROTULOS_SUGESTAO = {"NOVA": "Nova", "EM_ANALISE": "Em análise", "CONCLUIDA": "Concluída"}
ROTULOS_EVENTO = {
    "CRIADA": "Avaliação enviada", "EDITADA": "Avaliação editada e reenviada à moderação",
    "APROVADA": "Aprovada na moderação", "REJEITADA": "Rejeitada na moderação",
    "PUBLICADA": "Publicada na landing", "RETIRADA": "Retirada da publicação",
    "CONSENTIMENTO_CONCEDIDO": "Publicação autorizada pelo cliente",
    "CONSENTIMENTO_RETIRADO": "Autorização retirada pelo cliente",
    "SUGESTAO_ATUALIZADA": "Acompanhamento da sugestão alterado",
}
ORDENS_AVALIACAO = {
    "recentes": "Mais recentes", "antigas": "Mais antigas",
    "maior_nota": "Maior nota", "menor_nota": "Menor nota",
}

_TABELAS_AVALIACOES = ("avaliacao_cliente", "sugestao_melhoria", "evento_avaliacao", "solicitacao_avaliacao")
_REVERIFICAR_ESQUEMA_SEGUNDOS = 30.0
_esquema_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()

# Controles, marcas invisíveis e sobrescritas de direção (‍ fica: compõe emojis).
_CARACTERES_REMOVIDOS = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​‌‎‏‪-‮⁠-⁩﻿]"
)


class ErroModeracao(ValueError):
    """Decisão recusada: a mensagem é segura para exibir ao administrador."""


class EsquemaAvaliacoesInconsistente(RuntimeError):
    """Só parte das tabelas da revisão c5e8a1f4b7d2 existe neste banco."""


def esquema_avaliacoes_disponivel(sessao=None) -> bool:
    """True quando as tabelas da revisão c5e8a1f4b7d2 existem (expand -> migrate -> deploy).

    Mesmo protocolo de contract_policy.esquema_contratual_disponivel: confirmada
    a presença, fica fixo para o engine; a ausência é reavaliada a cada 30 s,
    para que a migração aplicada valha sem reiniciar o processo.
    """
    sessao = sessao or db.session
    engine = sessao.get_bind()
    estado = _esquema_por_engine.get(engine)
    if estado is True:
        return True
    if estado is not None and monotonic() - estado < _REVERIFICAR_ESQUEMA_SEGUNDOS:
        return False
    inspetor = inspect(sessao.connection())
    presentes = {tabela for tabela in _TABELAS_AVALIACOES if inspetor.has_table(tabela)}
    if presentes == set(_TABELAS_AVALIACOES):
        _esquema_por_engine[engine] = True
        return True
    if presentes:
        raise EsquemaAvaliacoesInconsistente("Esquema de avaliações parcial: confira a migração c5e8a1f4b7d2.")
    _esquema_por_engine[engine] = monotonic()
    return False


def url_politica_privacidade() -> str | None:
    """Endereço publicado da Política de Privacidade; None enquanto não houver."""
    url = (current_app.config.get("POLITICA_PRIVACIDADE_URL") or "").strip()
    if url.startswith("https://") or (url.startswith("/") and not url.startswith(("//", "/\\"))):
        return url
    return None


# ---------------------------------------------------------------------------
# Leitura e validação do formulário
# ---------------------------------------------------------------------------
def normalizar_texto(valor) -> str:
    """Texto livre pronto para gravar: NFC, sem controles nem marcas invisíveis.

    Não remove HTML: o conteúdo é texto e todo template o escapa. Quebras de
    linha simples são preservadas; três ou mais viram uma linha em branco.
    """
    texto = unicodedata.normalize("NFC", valor or "").replace("\r\n", "\n").replace("\r", "\n")
    texto = _CARACTERES_REMOVIDOS.sub("", texto).replace("\t", " ")
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def _texto_de_uma_linha(valor) -> str:
    return " ".join(normalizar_texto(valor).split())


@dataclass(frozen=True)
class DadosAvaliacao:
    nota: int
    comentario: str
    sugestao: str | None
    autoriza_publicacao: bool
    nome_exibicao: str | None
    autoriza_nome_empresa: bool


def _marcado(form, nome) -> bool:
    return form.get(nome) in ("1", "on", "true")


def ler_formulario(form) -> tuple[DadosAvaliacao | None, dict, dict]:
    """Devolve (dados, erros, valores para reexibir). Nunca lê ids do navegador."""
    erros = {}
    notas = form.getlist("nota")
    # Só os cinco textos exatos: recusa 0, 6, 3.5, " 4", "٣" e valores repetidos.
    nota = int(notas[0]) if len(notas) == 1 and notas[0] in ("1", "2", "3", "4", "5") else None
    if nota is None:
        erros["nota"] = "Selecione uma nota de 1 a 5 estrelas."

    # O teto bruto evita normalizar um corpo enorme só para recusá-lo depois.
    comentario_bruto = form.get("comentario") or ""
    comentario = normalizar_texto(comentario_bruto[:COMENTARIO_PUBLICO_MAX * 4])
    if not comentario:
        erros["comentario"] = "Conte como foi sua experiência."
    elif len(comentario) < COMENTARIO_PUBLICO_MIN:
        erros["comentario"] = f"Escreva pelo menos {COMENTARIO_PUBLICO_MIN} caracteres."
    elif len(comentario) > COMENTARIO_PUBLICO_MAX or len(comentario_bruto) > COMENTARIO_PUBLICO_MAX * 4:
        erros["comentario"] = f"O comentário pode ter até {COMENTARIO_PUBLICO_MAX} caracteres."

    sugestao_bruta = form.get("sugestao") or ""
    sugestao = normalizar_texto(sugestao_bruta[:SUGESTAO_PRIVADA_MAX * 4])
    if len(sugestao) > SUGESTAO_PRIVADA_MAX or len(sugestao_bruta) > SUGESTAO_PRIVADA_MAX * 4:
        erros["sugestao"] = f"A sugestão pode ter até {SUGESTAO_PRIVADA_MAX} caracteres."

    autoriza = _marcado(form, "autoriza_publicacao")
    # A identificação é subordinada à autorização: sem ela, nada é guardado.
    nome_bruto = form.get("nome_exibicao") or ""
    nome = _texto_de_uma_linha(nome_bruto[:NOME_EXIBICAO_MAX * 4]) if autoriza else ""
    if len(nome) > NOME_EXIBICAO_MAX or len(nome_bruto) > NOME_EXIBICAO_MAX * 4:
        erros["nome_exibicao"] = f"O nome de exibição pode ter até {NOME_EXIBICAO_MAX} caracteres."
    autoriza_empresa = autoriza and _marcado(form, "autoriza_nome_empresa")

    valores = {
        "nota": nota, "comentario": comentario, "sugestao": sugestao,
        "autoriza_publicacao": autoriza, "nome_exibicao": nome,
        "autoriza_nome_empresa": autoriza_empresa,
    }
    if erros:
        return None, erros, valores
    return DadosAvaliacao(nota, comentario, sugestao or None, autoriza, nome or None, autoriza_empresa), {}, valores


def valores_da_avaliacao(avaliacao) -> dict:
    """Valores atuais para preencher o formulário de edição."""
    return {
        "nota": avaliacao.nota, "comentario": avaliacao.comentario_publico,
        "sugestao": avaliacao.sugestao.texto if avaliacao.sugestao else "",
        "autoriza_publicacao": bool(avaliacao.autoriza_publicacao),
        "nome_exibicao": avaliacao.nome_exibicao or "",
        "autoriza_nome_empresa": bool(avaliacao.autoriza_nome_empresa),
    }


def ler_motivo(valor) -> str:
    return normalizar_texto((valor or "")[:MOTIVO_MAX * 4])


# ---------------------------------------------------------------------------
# Elegibilidade do CLIENTE
# ---------------------------------------------------------------------------
def motivo_inelegivel(usuario) -> str | None:
    """Mensagem ao cliente que não pode enviar nem editar; None quando pode.

    Sessão, conta desativada e empresa cancelada ou na lixeira já caem antes,
    nas guardas da aplicação; a regra é reavaliada para nenhuma rota depender
    só delas. Suspensa acessa o portal, mas não registra novas avaliações. Os
    60 dias valem só para o pedido automático, não para a avaliação voluntária.
    """
    if not usuario.is_cliente or not usuario.ativo or usuario.primeiro_acesso:
        return "O envio de avaliações é exclusivo de clientes com acesso ativo ao portal."
    situacao = obter_situacao_contratual(usuario.empresa, carregar_coberturas=False)
    if situacao.estado != ESTADO_ATIVA:
        return ("O envio de avaliações fica disponível para empresas com conta ativa. "
                "Você continua podendo consultar sua avaliação e retirar a autorização de publicação.")
    return None


def obter_avaliacao_usuario(id_usuario: int, *, bloquear: bool = False):
    """Avaliação do próprio usuário; o id vem sempre da sessão."""
    comando = select(AvaliacaoCliente).where(AvaliacaoCliente.id_usuario == id_usuario)
    if bloquear:
        comando = comando.with_for_update().execution_options(populate_existing=True)
    return db.session.scalar(comando)


# ---------------------------------------------------------------------------
# Operações do CLIENTE (sem commit)
# ---------------------------------------------------------------------------
def _registrar_evento(sessao, avaliacao, tipo, responsavel, instante, *, justificativa=None, versao_aviso=None):
    sessao.add(EventoAvaliacao(
        id_avaliacao=avaliacao.id_avaliacao, tipo=tipo, ocorrido_em=instante,
        id_usuario_responsavel=responsavel.id_usuario, justificativa=justificativa,
        consentimento_versao=versao_aviso,
    ))


def _gravar_sugestao(sessao, avaliacao, texto, instante) -> None:
    """Cria, altera ou remove a sugestão; texto novo volta a NOVA para a equipe."""
    atual = avaliacao.sugestao
    if not texto:
        if atual is not None:
            sessao.delete(atual)
        return
    if atual is None:
        sessao.add(SugestaoMelhoria(
            id_avaliacao=avaliacao.id_avaliacao, texto=texto,
            status_acompanhamento="NOVA", data_criacao=instante, data_atualizacao=instante,
        ))
    elif atual.texto != texto:
        atual.texto = texto
        atual.status_acompanhamento = "NOVA"
        atual.status_alterado_em = None
        atual.id_usuario_admin_status = None
        atual.data_atualizacao = instante


def _conceder_consentimento(avaliacao, instante) -> None:
    avaliacao.autoriza_publicacao = True
    avaliacao.consentimento_em = instante
    avaliacao.consentimento_versao = VERSAO_AVISO_CONSENTIMENTO
    avaliacao.consentimento_retirado_em = None


def _retirar_do_ar(sessao, avaliacao, responsavel, instante, motivo=None) -> bool:
    """Marca a retirada sem apagar a data de publicação (histórico)."""
    if avaliacao.publicada_em is None or avaliacao.retirada_em is not None:
        return False
    avaliacao.retirada_em = instante
    avaliacao.retirada_motivo = motivo
    _registrar_evento(sessao, avaliacao, "RETIRADA", responsavel, instante, justificativa=motivo)
    return True


def criar_avaliacao(sessao, usuario, dados: DadosAvaliacao, *, instante=None):
    """Inclui a avaliação PENDENTE. Duplicidade concorrente propaga IntegrityError
    de uq_avaliacao_cliente_usuario para a rota tratar sem dizer que gravou."""
    instante = instante or agora_utc()
    avaliacao = AvaliacaoCliente(
        id_usuario=usuario.id_usuario, id_empresa=usuario.id_empresa,
        nota=dados.nota, comentario_publico=dados.comentario,
        nome_exibicao=dados.nome_exibicao, autoriza_nome_empresa=dados.autoriza_nome_empresa,
        status_moderacao="PENDENTE", data_criacao=instante, data_atualizacao=instante,
    )
    if dados.autoriza_publicacao:
        _conceder_consentimento(avaliacao, instante)
    sessao.add(avaliacao)
    sessao.flush()
    _registrar_evento(sessao, avaliacao, "CRIADA", usuario, instante)
    if dados.autoriza_publicacao:
        _registrar_evento(sessao, avaliacao, "CONSENTIMENTO_CONCEDIDO", usuario, instante,
                          versao_aviso=VERSAO_AVISO_CONSENTIMENTO)
    _gravar_sugestao(sessao, avaliacao, dados.sugestao, instante)
    return avaliacao


def atualizar_avaliacao(sessao, usuario, dados: DadosAvaliacao, *, instante=None) -> tuple:
    """Edita a avaliação do próprio usuário; devolve (avaliacao, reenviada).

    Mudança no que seria público (nota, texto, identificação) ou autorização
    nova voltam a PENDENTE e saem do ar: nenhuma versão não lida é publicada.
    Mudar só a sugestão não mexe na moderação. Retirar a autorização pelo
    formulário tem o mesmo efeito imediato de retirar_consentimento.
    """
    instante = instante or agora_utc()
    avaliacao = obter_avaliacao_usuario(usuario.id_usuario, bloquear=True)
    if avaliacao is None:
        return None, False

    if avaliacao.autoriza_publicacao and not dados.autoriza_publicacao:
        _aplicar_retirada_consentimento(sessao, avaliacao, usuario, instante)
    autorizacao_nova = dados.autoriza_publicacao and not avaliacao.autoriza_publicacao
    mudou_publico = (
        avaliacao.nota != dados.nota
        or avaliacao.comentario_publico != dados.comentario
        or (dados.autoriza_publicacao and (
            avaliacao.nome_exibicao != dados.nome_exibicao
            or bool(avaliacao.autoriza_nome_empresa) != dados.autoriza_nome_empresa
        ))
        or avaliacao.id_empresa != usuario.id_empresa
    )
    reenviada = mudou_publico or autorizacao_nova

    avaliacao.nota = dados.nota
    avaliacao.comentario_publico = dados.comentario
    avaliacao.id_empresa = usuario.id_empresa
    avaliacao.nome_exibicao = dados.nome_exibicao
    avaliacao.autoriza_nome_empresa = dados.autoriza_nome_empresa
    if autorizacao_nova:
        _conceder_consentimento(avaliacao, instante)
        _registrar_evento(sessao, avaliacao, "CONSENTIMENTO_CONCEDIDO", usuario, instante,
                          versao_aviso=VERSAO_AVISO_CONSENTIMENTO)
    if reenviada:
        _voltar_para_moderacao(avaliacao)
        _registrar_evento(sessao, avaliacao, "EDITADA", usuario, instante)
    _gravar_sugestao(sessao, avaliacao, dados.sugestao, instante)
    avaliacao.versao += 1
    avaliacao.data_atualizacao = instante
    return avaliacao, reenviada


def _voltar_para_moderacao(avaliacao) -> None:
    avaliacao.status_moderacao = "PENDENTE"
    avaliacao.motivo_rejeicao = None
    avaliacao.moderada_em = None
    avaliacao.id_usuario_moderador = None
    avaliacao.publicada_em = None
    avaliacao.retirada_em = None
    avaliacao.retirada_motivo = None


def _aplicar_retirada_consentimento(sessao, avaliacao, usuario, instante) -> None:
    _retirar_do_ar(sessao, avaliacao, usuario, instante, "Autorização retirada pelo cliente")
    avaliacao.autoriza_publicacao = False
    avaliacao.autoriza_nome_empresa = False
    avaliacao.nome_exibicao = None
    avaliacao.consentimento_retirado_em = instante
    _registrar_evento(sessao, avaliacao, "CONSENTIMENTO_RETIRADO", usuario, instante)


def retirar_consentimento(sessao, usuario, *, instante=None) -> bool:
    """Retira a autorização do próprio usuário; False se não havia o que retirar."""
    instante = instante or agora_utc()
    avaliacao = obter_avaliacao_usuario(usuario.id_usuario, bloquear=True)
    if avaliacao is None or not avaliacao.autoriza_publicacao:
        return False
    _aplicar_retirada_consentimento(sessao, avaliacao, usuario, instante)
    avaliacao.versao += 1
    avaliacao.data_atualizacao = instante
    return True


# ---------------------------------------------------------------------------
# Moderação (ADMIN, sem commit)
# ---------------------------------------------------------------------------
def _avaliacao_para_moderar(sessao, id_avaliacao: int, versao_lida) -> AvaliacaoCliente:
    with sessao.no_autoflush:
        avaliacao = sessao.scalar(select(AvaliacaoCliente).where(
            AvaliacaoCliente.id_avaliacao == id_avaliacao,
        ).with_for_update().execution_options(populate_existing=True))
    if avaliacao is None:
        raise ErroModeracao("Avaliação não encontrada.")
    if type(versao_lida) is not int or versao_lida != avaliacao.versao:
        raise ErroModeracao("Esta avaliação mudou depois que a tela foi aberta. Confira a versão atual e decida de novo.")
    return avaliacao


def _concluir_moderacao(avaliacao, instante) -> None:
    avaliacao.versao += 1
    avaliacao.data_atualizacao = instante


def aprovar_avaliacao(sessao, id_avaliacao, moderador, versao_lida, *, instante=None) -> AvaliacaoCliente:
    """Aprova; com consentimento vigente, publica (também republica o que foi retirado)."""
    instante = instante or agora_utc()
    avaliacao = _avaliacao_para_moderar(sessao, id_avaliacao, versao_lida)
    if avaliacao.no_ar:
        raise ErroModeracao("Esta avaliação já está aprovada e publicada.")
    avaliacao.status_moderacao = "APROVADA"
    avaliacao.motivo_rejeicao = None
    avaliacao.moderada_em = instante
    avaliacao.id_usuario_moderador = moderador.id_usuario
    _registrar_evento(sessao, avaliacao, "APROVADA", moderador, instante)
    if avaliacao.autoriza_publicacao:
        avaliacao.publicada_em = instante
        avaliacao.retirada_em = None
        avaliacao.retirada_motivo = None
        _registrar_evento(sessao, avaliacao, "PUBLICADA", moderador, instante)
    _concluir_moderacao(avaliacao, instante)
    return avaliacao


def rejeitar_avaliacao(sessao, id_avaliacao, moderador, versao_lida, motivo, *, instante=None) -> AvaliacaoCliente:
    """Rejeita com justificativa obrigatória, sem alterar o texto do cliente."""
    instante = instante or agora_utc()
    motivo = ler_motivo(motivo)
    if not MOTIVO_MIN <= len(motivo) <= MOTIVO_MAX:
        raise ErroModeracao(f"Informe a justificativa da rejeição ({MOTIVO_MIN} a {MOTIVO_MAX} caracteres).")
    avaliacao = _avaliacao_para_moderar(sessao, id_avaliacao, versao_lida)
    _retirar_do_ar(sessao, avaliacao, moderador, instante, motivo)
    avaliacao.status_moderacao = "REJEITADA"
    avaliacao.motivo_rejeicao = motivo
    avaliacao.moderada_em = instante
    avaliacao.id_usuario_moderador = moderador.id_usuario
    _registrar_evento(sessao, avaliacao, "REJEITADA", moderador, instante, justificativa=motivo)
    _concluir_moderacao(avaliacao, instante)
    return avaliacao


def retirar_publicacao(sessao, id_avaliacao, moderador, versao_lida, motivo=None, *, instante=None) -> AvaliacaoCliente:
    """Tira do carrossel preservando o registro; a aprovação permanece."""
    instante = instante or agora_utc()
    motivo = ler_motivo(motivo)
    if len(motivo) > MOTIVO_MAX:
        raise ErroModeracao(f"A justificativa pode ter até {MOTIVO_MAX} caracteres.")
    avaliacao = _avaliacao_para_moderar(sessao, id_avaliacao, versao_lida)
    if not avaliacao.no_ar:
        raise ErroModeracao("Esta avaliação não está publicada.")
    _retirar_do_ar(sessao, avaliacao, moderador, instante, motivo or None)
    _concluir_moderacao(avaliacao, instante)
    return avaliacao


def atualizar_status_sugestao(sessao, id_sugestao, novo_status, responsavel, *, instante=None) -> SugestaoMelhoria:
    if novo_status not in STATUS_SUGESTAO:
        raise ErroModeracao("Status de acompanhamento inválido.")
    instante = instante or agora_utc()
    with sessao.no_autoflush:
        sugestao = sessao.scalar(select(SugestaoMelhoria).where(
            SugestaoMelhoria.id_sugestao == id_sugestao,
        ).with_for_update().execution_options(populate_existing=True))
    if sugestao is None:
        raise ErroModeracao("Sugestão não encontrada.")
    if sugestao.status_acompanhamento != novo_status:
        anterior = sugestao.status_acompanhamento
        sugestao.status_acompanhamento = novo_status
        sugestao.status_alterado_em = instante
        sugestao.id_usuario_admin_status = responsavel.id_usuario
        sugestao.data_atualizacao = instante
        _registrar_evento(sessao, sugestao.avaliacao, "SUGESTAO_ATUALIZADA", responsavel, instante,
                          justificativa=f"{ROTULOS_SUGESTAO[anterior]} → {ROTULOS_SUGESTAO[novo_status]}")
    return sugestao


def excluir_avaliacoes_empresa(sessao, id_empresa: int) -> None:
    """Exclusão definitiva da empresa: eventos e sugestões caem pela FK em CASCADE."""
    sessao.execute(delete(AvaliacaoCliente).where(AvaliacaoCliente.id_empresa == id_empresa))


# ---------------------------------------------------------------------------
# Consulta pública (landing): só o DTO mínimo, nunca a entidade
# ---------------------------------------------------------------------------
def depoimentos_publicos(limite: int = DEPOIMENTOS_NO_CARROSSEL) -> list[dict]:
    """Depoimentos aprovados e autorizados, consultados a cada exibição.

    Exige também usuário ativo e empresa com acesso ao portal: quem não consegue
    mais entrar não teria como retirar a autorização pelo canal oferecido, então
    o conteúdo sai do ar enquanto durar essa situação. As colunas são listadas
    uma a uma: e-mail, ids, moderação e sugestão nem são lidas.
    """
    if not esquema_avaliacoes_disponivel():
        return []
    linhas = db.session.execute(
        select(
            AvaliacaoCliente.nota, AvaliacaoCliente.comentario_publico, AvaliacaoCliente.nome_exibicao,
            AvaliacaoCliente.autoriza_nome_empresa, AvaliacaoCliente.publicada_em,
            Empresa.nome_fantasia, Empresa.razao_social,
        )
        .join(Empresa, Empresa.id_empresa == AvaliacaoCliente.id_empresa)
        .join(Usuario, Usuario.id_usuario == AvaliacaoCliente.id_usuario)
        .where(
            AvaliacaoCliente.status_moderacao == "APROVADA",
            AvaliacaoCliente.autoriza_publicacao.is_(True),
            AvaliacaoCliente.publicada_em.is_not(None),
            AvaliacaoCliente.retirada_em.is_(None),
            Usuario.ativo.is_(True),
            *condicoes_empresa_acessivel(),
        )
        .order_by(AvaliacaoCliente.publicada_em.desc(), AvaliacaoCliente.id_avaliacao.desc())
        .limit(limite)
    ).all()
    return [{
        "approved": True, "publication_consent": True,
        "quote": linha.comentario_publico,
        "author": linha.nome_exibicao or IDENTIFICACAO_ANONIMA,
        "company": (linha.nome_fantasia or linha.razao_social) if linha.autoriza_nome_empresa else "",
        "nota": linha.nota,
        "data": horario_brasilia(linha.publicada_em, "data"),
        "data_iso": iso_utc(linha.publicada_em),
    } for linha in linhas]


# ---------------------------------------------------------------------------
# Pedido automático após 60 dias (verificação leve no acesso ao portal)
# ---------------------------------------------------------------------------
def deve_solicitar_avaliacao(usuario, situacao, *, instante=None) -> bool:
    """Regra aprovada na AUD-LANDING-001, em dias corridos de Brasília.

    CLIENTE ativo de empresa ATIVA, ao menos 60 dias desde Empresa.data_contratacao
    (data contratual cadastrada), ao menos uma análise publicada e nenhuma avaliação
    do próprio usuário. Respeita o adiamento e a pausa pós-exibição gravados no banco.
    """
    if (not usuario.is_cliente or not usuario.ativo or usuario.primeiro_acesso
            or situacao.estado != ESTADO_ATIVA):
        return False
    if not esquema_avaliacoes_disponivel():
        return False
    instante = instante or agora()
    config = current_app.config
    if (data_local(instante) - usuario.empresa.data_contratacao).days < config["AVALIACAO_DIAS_GATILHO"]:
        return False
    if db.session.scalar(select(AvaliacaoCliente.id_avaliacao).where(
            AvaliacaoCliente.id_usuario == usuario.id_usuario)) is not None:
        return False
    estado = db.session.get(SolicitacaoAvaliacao, usuario.id_usuario, populate_existing=True)
    if estado is not None and (
            estado.qtd_adiamentos >= config["AVALIACAO_MAX_ADIAMENTOS"]
            or (estado.proxima_exibicao_em is not None and estado.proxima_exibicao_em > instante)):
        return False
    return db.session.scalar(
        select(Analise.id_analise)
        .join(RelatorioAnalise, RelatorioAnalise.id_analise == Analise.id_analise)
        .where(
            Analise.id_empresa == usuario.id_empresa,
            Analise.status_analise == "CONCLUIDO",
            RelatorioAnalise.publicado.is_(True),
        ).limit(1)
    ) is not None


def _estado_da_solicitacao(sessao, id_usuario: int) -> SolicitacaoAvaliacao:
    """Linha bloqueada do usuário; dois inserts concorrentes convergem para a mesma."""
    comando = select(SolicitacaoAvaliacao).where(
        SolicitacaoAvaliacao.id_usuario == id_usuario,
    ).with_for_update().execution_options(populate_existing=True)
    estado = sessao.scalar(comando)
    if estado is not None:
        return estado
    try:
        with sessao.begin_nested():
            estado = SolicitacaoAvaliacao(id_usuario=id_usuario, qtd_adiamentos=0)
            sessao.add(estado)
    except IntegrityError:
        estado = sessao.scalar(comando)
    return estado


def registrar_exibicao_solicitacao(sessao, usuario, *, instante=None) -> None:
    """O pedido foi exibido: não volta antes da pausa, mesmo sem resposta do cliente."""
    instante = instante or agora_utc()
    estado = _estado_da_solicitacao(sessao, usuario.id_usuario)
    pausa = instante + timedelta(hours=current_app.config["AVALIACAO_HORAS_REEXIBICAO"])
    estado.ultima_exibicao_em = instante
    if estado.proxima_exibicao_em is None or estado.proxima_exibicao_em < pausa:
        estado.proxima_exibicao_em = pausa


def adiar_solicitacao(sessao, usuario, *, instante=None) -> None:
    """"Mais tarde": idempotente entre abas (um adiamento por janela de espera)."""
    instante = instante or agora_utc()
    espera = timedelta(days=current_app.config["AVALIACAO_DIAS_ADIAMENTO"])
    estado = _estado_da_solicitacao(sessao, usuario.id_usuario)
    if estado.adiada_em is not None and estado.adiada_em + espera > instante:
        return
    estado.adiada_em = instante
    estado.proxima_exibicao_em = instante + espera
    estado.qtd_adiamentos += 1


# ---------------------------------------------------------------------------
# Listagens administrativas
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Filtros:
    status: str | None = None
    estrelas: int | None = None
    id_empresa: int | None = None
    inicio: date | None = None
    fim: date | None = None
    ordem: str = "recentes"
    pagina: int = 1

    def como_parametros(self) -> dict:
        """Parâmetros de URL dos filtros ativos, para a paginação preservá-los."""
        parametros = {
            "status": self.status, "estrelas": self.estrelas, "empresa": self.id_empresa,
            "inicio": self.inicio.isoformat() if self.inicio else None,
            "fim": self.fim.isoformat() if self.fim else None,
            "ordem": self.ordem if self.ordem != "recentes" else None,
        }
        return {nome: valor for nome, valor in parametros.items() if valor is not None}


def _data_filtro(valor) -> date | None:
    try:
        return datetime.strptime((valor or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _inteiro_filtro(valor, maximo: int = 2147483647) -> int | None:
    valor = (valor or "").strip()
    if not valor.isascii() or not valor.isdecimal() or len(valor) > 10:
        return None
    numero = int(valor)
    return numero if 0 < numero <= maximo else None


def ler_filtros(args, status_validos) -> Filtros:
    """Filtro inválido é ignorado, nunca vira erro nem chega ao SQL."""
    status = args.get("status")
    estrelas = _inteiro_filtro(args.get("estrelas"), 5)
    ordem = args.get("ordem")
    return Filtros(
        status=status if status in status_validos else None,
        estrelas=estrelas,
        id_empresa=_inteiro_filtro(args.get("empresa")),
        inicio=_data_filtro(args.get("inicio")),
        fim=_data_filtro(args.get("fim")),
        ordem=ordem if ordem in ORDENS_AVALIACAO else "recentes",
        pagina=_inteiro_filtro(args.get("pagina")) or 1,
    )


def _instante_local(dia: date, *, proximo_dia: bool = False) -> datetime:
    """Meia-noite de Brasília do dia (ou do seguinte) em UTC: o período é o que o admin lê."""
    alvo = dia + timedelta(days=1) if proximo_dia else dia
    return datetime.combine(alvo, hora_do_dia.min, tzinfo=BRASILIA).astimezone(UTC)


@dataclass(frozen=True)
class Pagina:
    itens: list
    total: int
    pagina: int
    paginas: int


def _paginar(comando, contagem, pagina: int) -> Pagina:
    total = db.session.scalar(contagem) or 0
    paginas = max(1, -(-total // POR_PAGINA))
    pagina = min(max(1, pagina), paginas)
    itens = db.session.scalars(comando.limit(POR_PAGINA).offset((pagina - 1) * POR_PAGINA)).unique().all()
    return Pagina(itens, total, pagina, paginas)


def _condicoes_periodo(coluna, filtros: Filtros) -> list:
    condicoes = []
    if filtros.inicio:
        condicoes.append(coluna >= _instante_local(filtros.inicio))
    if filtros.fim:
        condicoes.append(coluna < _instante_local(filtros.fim, proximo_dia=True))
    return condicoes


def listar_avaliacoes(filtros: Filtros) -> Pagina:
    condicoes = _condicoes_periodo(AvaliacaoCliente.data_criacao, filtros)
    if filtros.status:
        condicoes.append(AvaliacaoCliente.status_moderacao == filtros.status)
    if filtros.estrelas:
        condicoes.append(AvaliacaoCliente.nota == filtros.estrelas)
    if filtros.id_empresa:
        condicoes.append(AvaliacaoCliente.id_empresa == filtros.id_empresa)
    ordem = {
        "recentes": (AvaliacaoCliente.data_atualizacao.desc(), AvaliacaoCliente.id_avaliacao.desc()),
        "antigas": (AvaliacaoCliente.data_atualizacao.asc(), AvaliacaoCliente.id_avaliacao.asc()),
        "maior_nota": (AvaliacaoCliente.nota.desc(), AvaliacaoCliente.id_avaliacao.desc()),
        "menor_nota": (AvaliacaoCliente.nota.asc(), AvaliacaoCliente.id_avaliacao.desc()),
    }[filtros.ordem]
    return _paginar(
        select(AvaliacaoCliente).where(*condicoes).order_by(*ordem).options(
            joinedload(AvaliacaoCliente.usuario), joinedload(AvaliacaoCliente.empresa),
        ),
        select(func.count(AvaliacaoCliente.id_avaliacao)).where(*condicoes),
        filtros.pagina,
    )


def contagem_por_status() -> dict:
    """Totais da fila de moderação, para os atalhos do topo da página."""
    linhas = db.session.execute(
        select(AvaliacaoCliente.status_moderacao, func.count(AvaliacaoCliente.id_avaliacao))
        .group_by(AvaliacaoCliente.status_moderacao)
    ).all()
    contagens = dict.fromkeys(STATUS_MODERACAO, 0)
    contagens.update({status: total for status, total in linhas})
    return contagens


def empresas_com_avaliacao() -> list[tuple[int, str]]:
    """Opções do filtro de empresa: só as que têm avaliação, sem listar a carteira."""
    linhas = db.session.execute(
        select(Empresa.id_empresa, Empresa.nome_fantasia, Empresa.razao_social)
        .where(select(AvaliacaoCliente.id_avaliacao).where(
            AvaliacaoCliente.id_empresa == Empresa.id_empresa).exists())
        .order_by(func.coalesce(Empresa.nome_fantasia, Empresa.razao_social))
    ).all()
    return [(linha.id_empresa, linha.nome_fantasia or linha.razao_social) for linha in linhas]


def obter_avaliacao_detalhada(id_avaliacao: int):
    return db.session.scalar(select(AvaliacaoCliente).where(
        AvaliacaoCliente.id_avaliacao == id_avaliacao,
    ).options(
        joinedload(AvaliacaoCliente.usuario), joinedload(AvaliacaoCliente.empresa),
        joinedload(AvaliacaoCliente.sugestao), joinedload(AvaliacaoCliente.moderador),
        selectinload(AvaliacaoCliente.eventos).joinedload(EventoAvaliacao.responsavel),
    ))


def listar_sugestoes(filtros: Filtros) -> Pagina:
    condicoes = _condicoes_periodo(SugestaoMelhoria.data_atualizacao, filtros)
    if filtros.status:
        condicoes.append(SugestaoMelhoria.status_acompanhamento == filtros.status)
    if filtros.id_empresa:
        condicoes.append(AvaliacaoCliente.id_empresa == filtros.id_empresa)
    # Na fila, primeiro as ainda não tratadas; dentro de cada grupo, as mais recentes.
    ordem_status = case(
        (SugestaoMelhoria.status_acompanhamento == "NOVA", 0),
        (SugestaoMelhoria.status_acompanhamento == "EM_ANALISE", 1),
        else_=2,
    )
    return _paginar(
        select(SugestaoMelhoria).join(SugestaoMelhoria.avaliacao).where(*condicoes)
        .order_by(ordem_status, SugestaoMelhoria.data_atualizacao.desc(), SugestaoMelhoria.id_sugestao.desc())
        .options(
            joinedload(SugestaoMelhoria.avaliacao).joinedload(AvaliacaoCliente.usuario),
            joinedload(SugestaoMelhoria.avaliacao).joinedload(AvaliacaoCliente.empresa),
        ),
        select(func.count(SugestaoMelhoria.id_sugestao)).join(SugestaoMelhoria.avaliacao).where(*condicoes),
        filtros.pagina,
    )


def contagem_sugestoes_por_status() -> dict:
    linhas = db.session.execute(
        select(SugestaoMelhoria.status_acompanhamento, func.count(SugestaoMelhoria.id_sugestao))
        .group_by(SugestaoMelhoria.status_acompanhamento)
    ).all()
    contagens = dict.fromkeys(STATUS_SUGESTAO, 0)
    contagens.update({status: total for status, total in linhas})
    return contagens
