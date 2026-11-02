"""
NEXO - Configurações da conta (regras de negócio)
=================================================
Perfil, documentos legais e aceites, exportação de dados (LGPD), pedido de
exclusão, cópia de chamados e lembrete de renovação. As rotas ficam nos blueprints
cliente e admin; a troca de senha e de e-mail, em blueprints/auth.py, junto do
restante da credencial.

Regras:
  - Nenhuma função recebe identificador de empresa vindo da requisição: tudo parte
    do usuário da sessão, passado pela rota.
  - As funções de escrita não fazem commit: a rota confirma ou reverte.
  - Entrada recusada levanta DadoInvalido, cuja mensagem é segura para exibir.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
import logging
import re

from flask import current_app, url_for
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from company_security import bloquear_empresa
from contas_email import email_valido, normalizar_email
from contract_policy import (
    ESTADO_ATIVA, data_local, esquema_contratual_disponivel, filtrar_analises_consultaveis, pode_exportar_pdf,
)
from emails import (
    diagnostico_configuracao, email_lembrete_renovacao, email_novo_chamado, envio_disponivel, ultima_conexao_smtp,
)
from extensions import db
from horario import agora_utc
from models import (
    AceiteDocumento, Analise, ChamadoSuporte, ConfiguracaoSistema, Contrato, DocumentoLegal, Empresa,
    MensagemSuporte, Notificacao, RegimeContratual, RelatorioAnalise, Usuario,
)
from notifications import criar_notificacao, notificar_admins
from origem_publica import url_publica
from politica_senha import TAMANHO_MAXIMO, TAMANHO_MINIMO
from preferencias_usuario import (
    esquema_configuracoes_disponivel, ids_com_aviso_desligado, obter_preferencias,
)

logger = logging.getLogger(__name__)

TIPOS_DOCUMENTO = {"TERMOS_USO": "Termos de Uso", "POLITICA_PRIVACIDADE": "Política de Privacidade"}
TAMANHO_MAXIMO_DOCUMENTO = 200_000
CHAVE_COPIA_SUPORTE = "suporte_bcc_email"
ASSUNTO_EXCLUSAO_DADOS = "[LGPD] Solicitação de exclusão de dados"
_TAMANHO_MAXIMO_NOME = 120
_TELEFONE = re.compile(r"\+?[0-9()\-. ]{8,25}")
# A primeira palavra do nome abre os e-mails, que também saem para um endereço ainda não confirmado.
_NOME_COM_ENDERECO = re.compile(r"[/\\:@]|\w\.\w{2,}")
# Controles de texto (menos tabulação e quebra de linha, que fazem parte do documento).
_CONTROLES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class DadoInvalido(ValueError):
    """Entrada recusada; a mensagem é segura para exibir ao usuário."""


def exigir_esquema() -> None:
    if not esquema_configuracoes_disponivel():
        raise DadoInvalido("Este ajuste ainda não está disponível: a atualização do banco de dados não foi aplicada.")


# ---------------------------------------------------------------------------
# Perfil
# ---------------------------------------------------------------------------
def limpar_nome(valor) -> str:
    nome = " ".join(valor.split()) if isinstance(valor, str) else ""
    if not 2 <= len(nome) <= _TAMANHO_MAXIMO_NOME:
        raise DadoInvalido(f"Informe um nome de 2 a {_TAMANHO_MAXIMO_NOME} caracteres.")
    if not any(c.isalpha() for c in nome) or "<" in nome or ">" in nome or not nome.isprintable():
        raise DadoInvalido("O nome contém caracteres inválidos.")
    return nome


def limpar_telefone(valor) -> str | None:
    """Telefone em uma linha, ou None quando o campo veio vazio (remove o telefone)."""
    telefone = " ".join(valor.split()) if isinstance(valor, str) else ""
    if not telefone:
        return None
    digitos = sum(c.isdigit() for c in telefone)
    if not _TELEFONE.fullmatch(telefone) or not 10 <= digitos <= 15:
        raise DadoInvalido("Informe o telefone com DDD, usando apenas números, espaços, parênteses e traços.")
    return telefone


def salvar_perfil(usuario: Usuario, nome, telefone, telefone_exibido=None) -> None:
    """Nome do responsável (a conta) e telefone de contato (a empresa da própria conta).

    O telefone é o da empresa porque é nele que o cadastro o guarda: não há
    telefone por usuário. `telefone_exibido` é o que a pessoa tinha na tela.
    """
    nome = limpar_nome(nome)
    if nome != " ".join((usuario.nome or "").split()) and _NOME_COM_ENDERECO.search(nome):
        raise DadoInvalido("O nome não pode conter endereço de site ou de e-mail.")
    empresa = usuario.empresa
    telefone_atual = " ".join((empresa.telefone_contato or "").split())
    telefone_enviado = " ".join(telefone.split()) if isinstance(telefone, str) else ""
    exibido = " ".join(telefone_exibido.split()) if isinstance(telefone_exibido, str) else telefone_atual
    # Só valida o que mudou: o cadastro do admin aceita texto livre (ex.: ramal), e o
    # formulário reenvia o telefone mesmo quando a pessoa só quer corrigir o nome. Quem
    # não mexeu no campo também não desfaz o que um colega gravou com a tela já aberta.
    telefone_mudou = isinstance(telefone, str) and telefone_enviado not in (telefone_atual, exibido)
    telefone_novo = limpar_telefone(telefone_enviado) if telefone_mudou else None
    agora = agora_utc()
    usuario.nome = nome
    usuario.data_atualizacao = agora
    if telefone_mudou:
        empresa.telefone_contato = telefone_novo
        empresa.data_atualizacao = agora


# ---------------------------------------------------------------------------
# Termos de Uso e Política de Privacidade
# ---------------------------------------------------------------------------
def documento_vigente(tipo: str) -> DocumentoLegal | None:
    return db.session.scalars(
        select(DocumentoLegal).where(DocumentoLegal.tipo == tipo)
        .order_by(DocumentoLegal.versao.desc()).limit(1)
    ).first()


def _inteiro(valor) -> int | None:
    if not isinstance(valor, str) or not valor.isascii() or not valor.isdecimal() or len(valor) > 9:
        return None
    return int(valor)


def publicar_documento(tipo, conteudo, versao_exibida, admin: Usuario) -> DocumentoLegal | None:
    """Cria a versão seguinte. Devolve None quando o texto não mudou.

    `versao_exibida` é a versão que o admin tinha na tela: se outra foi publicada
    enquanto ele editava, salvar por cima apagaria o trabalho do colega sem aviso.
    """
    exigir_esquema()
    if tipo not in TIPOS_DOCUMENTO:
        raise DadoInvalido("Documento inválido.")
    texto = _CONTROLES.sub("", conteudo.replace("\r\n", "\n").replace("\r", "\n")).strip() if isinstance(conteudo, str) else ""
    if not texto:
        raise DadoInvalido("Escreva o texto do documento antes de publicar.")
    if len(texto) > TAMANHO_MAXIMO_DOCUMENTO:
        raise DadoInvalido(f"O documento pode ter no máximo {TAMANHO_MAXIMO_DOCUMENTO} caracteres.")
    vigente = documento_vigente(tipo)
    versao_atual = vigente.versao if vigente else 0
    exibida = _inteiro(versao_exibida)
    # Mesmo texto da versão vigente: nada a publicar nem conflito (ex.: segundo clique).
    if exibida is not None and vigente is not None and vigente.conteudo == texto:
        return None
    if exibida != versao_atual:
        raise DadoInvalido("Outra versão foi publicada enquanto você editava. Confira o texto atual e publique de novo.")
    documento = DocumentoLegal(tipo=tipo, versao=versao_atual + 1, conteudo=texto, publicado_em=agora_utc(),
                               id_usuario_admin=admin.id_usuario)
    db.session.add(documento)
    return documento


def registrar_aceite(usuario: Usuario, tipo, versao) -> bool:
    """Aceite da versão vigente, a que estava na tela. Devolve False se já existia."""
    exigir_esquema()
    if tipo not in TIPOS_DOCUMENTO:
        raise DadoInvalido("Documento inválido.")
    vigente = documento_vigente(tipo)
    if vigente is None or _inteiro(versao) != vigente.versao:
        raise DadoInvalido("O documento foi atualizado. Leia a versão atual antes de aceitar.")
    existente = db.session.scalar(select(AceiteDocumento.id_aceite).where(
        AceiteDocumento.id_usuario == usuario.id_usuario,
        AceiteDocumento.id_documento == vigente.id_documento,
    ))
    if existente is not None:
        return False
    db.session.add(AceiteDocumento(id_usuario=usuario.id_usuario, id_documento=vigente.id_documento,
                                   aceito_em=agora_utc()))
    return True


def situacao_documentos(id_usuario: int) -> list[dict]:
    """Por documento: a versão vigente e os aceites DESTE usuário (primeiro e mais recente)."""
    situacoes = []
    for tipo, rotulo in TIPOS_DOCUMENTO.items():
        aceites = db.session.execute(
            select(AceiteDocumento.aceito_em, DocumentoLegal.versao)
            .join(DocumentoLegal, DocumentoLegal.id_documento == AceiteDocumento.id_documento)
            .where(AceiteDocumento.id_usuario == id_usuario, DocumentoLegal.tipo == tipo)
            .order_by(AceiteDocumento.aceito_em, AceiteDocumento.id_aceite)
        ).all()
        vigente = documento_vigente(tipo)
        situacoes.append({
            "tipo": tipo, "rotulo": rotulo, "vigente": vigente,
            "aceite_inicial": aceites[0] if aceites else None,
            "aceite_recente": aceites[-1] if aceites else None,
            "aceitou_vigente": bool(vigente) and any(a.versao == vigente.versao for a in aceites),
        })
    return situacoes


# ---------------------------------------------------------------------------
# Cópia (BCC) de novos chamados
# ---------------------------------------------------------------------------
def email_copia_suporte() -> str | None:
    if not esquema_configuracoes_disponivel():
        return None
    linha = db.session.get(ConfiguracaoSistema, CHAVE_COPIA_SUPORTE)
    return (linha.valor or None) if linha else None


def salvar_email_copia_suporte(valor, admin: Usuario) -> str | None:
    """Grava a caixa da cópia; campo vazio desliga. Devolve o valor gravado."""
    exigir_esquema()
    endereco = normalizar_email(valor) or None
    if endereco is not None and not email_valido(endereco):
        raise DadoInvalido("Informe um e-mail válido, ou deixe o campo vazio para desligar a cópia.")
    linha = db.session.get(ConfiguracaoSistema, CHAVE_COPIA_SUPORTE)
    if linha is None:
        linha = ConfiguracaoSistema(chave=CHAVE_COPIA_SUPORTE)
        db.session.add(linha)
    linha.valor = endereco
    linha.atualizado_em = agora_utc()
    linha.id_usuario_admin = admin.id_usuario
    return endereco


def avisar_novo_chamado(id_chamado: int, rotulo_categoria: str) -> None:
    """Depois do commit: cópia do chamado, se o admin configurou a caixa. O chamado já
    existe e o sininho já avisou: uma falha aqui só fica no log."""
    try:
        copia = email_copia_suporte()
        chamado = db.session.get(ChamadoSuporte, id_chamado)
        if copia and chamado is not None:
            email_novo_chamado(chamado, copia, rotulo_categoria,
                               url_publica("admin.ticket_detalhe", id_chamado=id_chamado))
    except Exception as erro:  # noqa: BLE001
        db.session.rollback()
        logger.warning("Cópia do novo chamado não preparada (%s).", type(erro).__name__)


# ---------------------------------------------------------------------------
# Solicitação de exclusão de dados (LGPD)
# ---------------------------------------------------------------------------
def solicitacao_exclusao_aberta(id_empresa: int) -> ChamadoSuporte | None:
    return db.session.scalars(
        select(ChamadoSuporte).where(
            ChamadoSuporte.id_empresa == id_empresa,
            ChamadoSuporte.assunto == ASSUNTO_EXCLUSAO_DADOS,
            # O assunto é texto livre nos chamados comuns; a prioridade alta, o cliente não escolhe.
            ChamadoSuporte.prioridade == "ALTA",
            ChamadoSuporte.status_chamado != "RESOLVIDO",
        ).order_by(ChamadoSuporte.id_chamado.desc()).limit(1)
    ).first()


def solicitar_exclusao_dados(usuario: Usuario) -> tuple[ChamadoSuporte, bool]:
    """Abre o chamado interno de expurgo; nada é apagado aqui. Devolve (chamado, criado).

    A categoria usa um valor já aceito pelo banco, em vez de exigir migração de um
    CHECK de chamado_suporte: o pedido funciona antes das tabelas novas existirem.
    """
    # Lock na empresa: dois envios simultâneos não abrem dois chamados.
    empresa = bloquear_empresa(db.session, usuario.id_empresa)
    existente = solicitacao_exclusao_aberta(empresa.id_empresa)
    if existente is not None:
        return existente, False
    instante = agora_utc()
    chamado = ChamadoSuporte(
        id_empresa=empresa.id_empresa, id_usuario_cliente=usuario.id_usuario, assunto=ASSUNTO_EXCLUSAO_DADOS,
        categoria="DUVIDA_TECNICA", status_chamado="ABERTO", prioridade="ALTA", data_abertura=instante,
    )
    db.session.add(chamado)
    db.session.flush()
    db.session.add(MensagemSuporte(
        id_chamado=chamado.id_chamado, id_usuario_remetente=usuario.id_usuario, data_envio=instante,
        mensagem="Solicitação de exclusão de dados registrada pelo titular em Configurações > Privacidade e LGPD. "
                 "Nenhum dado foi apagado automaticamente: a equipe NEXO fará a tratativa do expurgo.",
    ))
    nome_emp = empresa.nome_fantasia or empresa.razao_social
    notificar_admins(f"Novo chamado de {nome_emp}: “{ASSUNTO_EXCLUSAO_DADOS}”.",
                     url_for("admin.ticket_detalhe", id_chamado=chamado.id_chamado))
    return chamado, True


# ---------------------------------------------------------------------------
# Exportação de dados (LGPD)
# ---------------------------------------------------------------------------
def valor_json(valor):
    """Só o que o relatório realmente usa; qualquer outro tipo é erro, não texto implícito."""
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, (date, datetime)):
        return valor.isoformat()
    raise TypeError(f"Tipo não exportável: {type(valor).__name__}")


def _analise_exportada(analise: Analise) -> dict:
    publicada = analise.relatorio.publicada  # a cópia publicada, nunca o rascunho em edição
    indicador = analise.indicador
    return {
        "id_analise": analise.id_analise,
        "tipo": analise.tipo_analise,
        "mes_referencia": analise.mes_referencia,
        "ano_referencia": analise.ano_referencia,
        "quinzena_referencia": analise.quinzena_referencia,
        "periodo_inicio": analise.periodo_inicio,
        "periodo_fim": analise.periodo_fim,
        "data_conclusao": analise.data_conclusao,
        "relatorio": {
            "titulo": publicada.titulo,
            "resumo_executivo": publicada.resumo_executivo,
            "conclusao_estrategica": publicada.conclusao_estrategica,
            "gerado_por_ia": publicada.gerado_por_ia,
            "data_publicacao": publicada.data_publicacao,
        },
        "indicadores": None if indicador is None else {
            "faturamento_total": indicador.faturamento_total,
            "total_comprado": indicador.total_comprado,
            "saldo_estimado_compras_vendas": indicador.saldo_estimado_compras_vendas,
            "produto_mais_vendido_nome": indicador.produto_mais_vendido_nome,
            "produto_mais_vendido_quantidade": indicador.produto_mais_vendido_quantidade,
            "produto_maior_faturamento_nome": indicador.produto_maior_faturamento_nome,
            "produto_maior_faturamento_valor": indicador.produto_maior_faturamento_valor,
            "produto_maior_saldo_parado_nome": indicador.produto_maior_saldo_parado_nome,
            "saldo_estimado_parado": indicador.saldo_estimado_parado,
            "periodo_base_inicio": indicador.periodo_base_inicio,
            "periodo_base_fim": indicador.periodo_base_fim,
        },
        "curva_abc": [{
            "posicao": item.posicao_ranking, "produto": item.produto_nome, "faturamento": item.faturamento,
            "quantidade": item.quantidade, "percentual_individual": item.percentual_individual,
            "percentual_acumulado": item.percentual_acumulado, "classe": item.classe_abc,
        } for item in analise.curva_abc],
    }


def _titular_exportado(usuario: Usuario) -> dict:
    preferencias = obter_preferencias(usuario.id_usuario)
    aceites = db.session.execute(
        select(DocumentoLegal.tipo, DocumentoLegal.versao, AceiteDocumento.aceito_em)
        .join(DocumentoLegal, DocumentoLegal.id_documento == AceiteDocumento.id_documento)
        .where(AceiteDocumento.id_usuario == usuario.id_usuario)
        .order_by(AceiteDocumento.aceito_em, AceiteDocumento.id_aceite)
    ).all() if esquema_configuracoes_disponivel() else []
    return {
        "nome": usuario.nome,
        "email": usuario.email,
        "preferencias": {
            "lembrete_renovacao": preferencias.lembrete_renovacao,
            "alerta_chamado": preferencias.alerta_chamado,
        },
        "aceites": [{"documento": a.tipo, "versao": a.versao, "aceito_em": a.aceito_em} for a in aceites],
    }


def _chamados_exportados(usuario: Usuario) -> list[dict]:
    """Os chamados que a própria pessoa abriu, com a conversa. O nome de quem respondeu pela
    NEXO não entra: o que importa ao titular é o que foi dito, e por qual lado."""
    chamados = db.session.scalars(
        select(ChamadoSuporte).where(
            ChamadoSuporte.id_empresa == usuario.id_empresa,
            ChamadoSuporte.id_usuario_cliente == usuario.id_usuario,
        ).order_by(ChamadoSuporte.data_abertura, ChamadoSuporte.id_chamado)
        .options(selectinload(ChamadoSuporte.mensagens).selectinload(MensagemSuporte.remetente))
    ).all()
    return [{
        "id_chamado": chamado.id_chamado,
        "assunto": chamado.assunto,
        "categoria": chamado.categoria,
        "situacao": chamado.status_chamado,
        "data_abertura": chamado.data_abertura,
        "data_fechamento": chamado.data_fechamento,
        "mensagens": [{
            "autor": "equipe_nexo" if mensagem.remetente.is_admin else "cliente",
            "data_envio": mensagem.data_envio,
            "mensagem": mensagem.mensagem,
        } for mensagem in chamado.mensagens],
    } for chamado in chamados]


def _contratos_exportados(id_empresa: int) -> list[dict]:
    if not esquema_contratual_disponivel():
        return []
    contratos = db.session.scalars(
        select(Contrato).where(Contrato.id_empresa == id_empresa).order_by(Contrato.data_inicio, Contrato.id_contrato)
    ).all()
    # Sem observações, referências e motivos: são anotações internas da equipe.
    return [{
        "id_contrato": contrato.id_contrato,
        "plano": contrato.plano.nome_plano,
        "valor_mensal_contratado": contrato.valor_mensal_contratado,
        "data_inicio": contrato.data_inicio,
        "data_fim": contrato.data_fim,
        "situacao": contrato.situacao,
        "suspenso_em": contrato.suspenso_em,
        "suspensao_limite": contrato.suspensao_limite,
        "encerrado_em": contrato.encerrado_em,
        "cancelado_em": contrato.cancelado_em,
    } for contrato in contratos]


def montar_exportacao(usuario: Usuario, situacao) -> dict:
    """Dados da empresa da sessão, por lista explícita de campos: um campo novo no
    modelo só entra aqui de propósito. A empresa vem de `usuario`, nunca da requisição.

    Análises seguem a regra do PDF (`pode_exportar_pdf`): com o prazo de exportação
    encerrado, o JSON não pode ser o caminho para levar os mesmos relatórios.
    """
    empresa = usuario.empresa
    consultaveis = filtrar_analises_consultaveis(situacao, db.session.scalars(
        select(Analise)
        .join(RelatorioAnalise, RelatorioAnalise.id_analise == Analise.id_analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise == "CONCLUIDO")
        .where(RelatorioAnalise.publicado.is_(True))
        .order_by(Analise.ano_referencia, Analise.mes_referencia, Analise.id_analise)
        .options(selectinload(Analise.indicador), selectinload(Analise.relatorio), selectinload(Analise.curva_abc))
    ).all())
    exportaveis = [analise for analise in consultaveis if pode_exportar_pdf(situacao, analise)]
    dados = {
        "exportacao": {"formato": "nexo-dados-da-conta", "versao": 1, "gerada_em": agora_utc()},
        "titular": _titular_exportado(usuario),
        "empresa": {
            "razao_social": empresa.razao_social,
            "nome_fantasia": empresa.nome_fantasia,
            "cnpj": empresa.cnpj,
            "email_contato": empresa.email_contato,
            "telefone_contato": empresa.telefone_contato,
            "data_contratacao": empresa.data_contratacao,
            "faturamento_base_mensal": empresa.faturamento_base_mensal,
            "status_conta": empresa.status_conta,
            "plano": empresa.plano_atual.nome_plano,
            "segmento": empresa.segmento.nome_segmento,
        },
        "contratos": _contratos_exportados(empresa.id_empresa),
        "analises_concluidas": [_analise_exportada(analise) for analise in exportaveis],
        "chamados": _chamados_exportados(usuario),
    }
    if len(exportaveis) != len(consultaveis):
        dados["aviso"] = "O prazo para exportar relatórios foi encerrado: as análises não entram neste arquivo."
    return dados


# ---------------------------------------------------------------------------
# Lembrete de renovação do contrato
# ---------------------------------------------------------------------------
def aviso_renovacao(usuario: Usuario, situacao) -> dict | None:
    """Prazo para a faixa da Visão Geral; None quando não há o que lembrar a esta pessoa."""
    contrato = situacao.contrato
    if (not situacao.contratual or situacao.estado != ESTADO_ATIVA or contrato is None
            or contrato.data_fim is None):
        return None
    dias = (contrato.data_fim - data_local(situacao.instante)).days
    if not 0 <= dias <= current_app.config["CONTRATO_AVISO_RENOVACAO_DIAS"]:
        return None
    if not obter_preferencias(usuario.id_usuario).lembrete_renovacao:
        return None
    return {"data_fim": contrato.data_fim, "dias": dias}


@dataclass
class RelatorioLembretes:
    instante: datetime
    janela_dias: int
    candidatos: list = field(default_factory=list)
    notificados: int = 0
    emails_aceitos: int = 0
    emails_nao_aceitos: int = 0
    emails_recusados: int = 0
    recusados: list = field(default_factory=list)


def _texto_lembrete(data_fim: date) -> str:
    return f"Seu contrato com a NEXO vence em {data_fim:%d/%m/%Y}. Fale com a equipe para renovar."


# Status que falam do destinatário (RFC 3463). 5.1.0 é genérico; 5.1.7 e 5.1.8 falam do remetente.
_STATUS_ENDERECO_INEXISTENTE = ("/5.1.1", "/5.1.2", "/5.1.3", "/5.1.6", "/5.1.10")


def _recusa_definitiva(envio) -> bool:
    """Endereço recusado de vez: inválido ou caixa inexistente. Repetir não adianta.
    Recusa de política ou de configuração (5.7.x, 5.1.0, 5.1.7, 5.1.8, 4xx, código sem status)
    continua pendente."""
    return envio.categoria == "destinatario_invalido" or (
        envio.categoria == "smtp_destinatario_recusado"
        and (envio.codigo_provedor or "").endswith(_STATUS_ENDERECO_INEXISTENTE))


def _lembrete_segue_valido(id_contrato: int, id_empresa: int, data_fim: date) -> bool:
    """Trava a empresa e confere de novo: a lista foi lida no início e o envio é síncrono."""
    empresa = bloquear_empresa(db.session, id_empresa)
    contrato = db.session.get(Contrato, id_contrato, populate_existing=True)
    return (empresa is not None and empresa.deletado_em is None and empresa.status_conta == "ATIVA"
            and contrato is not None and contrato.situacao == "ATIVO" and contrato.data_fim == data_fim
            and db.session.get(RegimeContratual, id_empresa, populate_existing=True) is not None)


def enviar_lembretes_renovacao(*, aplicar: bool = False, instante: datetime | None = None) -> RelatorioLembretes:
    """Contratos vigentes do regime contratual que terminam dentro da janela.

    Cada pessoa recebe um lembrete por data de término: o sininho é o registro, então
    rodar de novo não repete, e quem religar o aviso recebe na rodada seguinte. Commit
    por pessoa, com o e-mail logo depois dele: se a rodada parar, quem ainda não foi
    tentado continua pendente. Se o e-mail não for aceito, o registro sai e a pessoa volta
    a contar na próxima rodada, salvo endereço recusado de vez, que fica só com o sininho.
    """
    instante = instante or agora_utc()
    janela = current_app.config["CONTRATO_AVISO_RENOVACAO_DIAS"]
    relatorio = RelatorioLembretes(instante=instante, janela_dias=janela)
    if not esquema_contratual_disponivel():
        return relatorio
    hoje = data_local(instante)
    contratos = db.session.execute(
        select(Contrato.id_contrato, Contrato.id_empresa, Contrato.data_fim)
        .join(Empresa, Empresa.id_empresa == Contrato.id_empresa)
        .join(RegimeContratual, RegimeContratual.id_empresa == Contrato.id_empresa)
        .where(Contrato.situacao == "ATIVO", Contrato.data_fim.is_not(None),
               Contrato.data_fim.between(hoje, hoje + timedelta(days=janela)),
               Empresa.status_conta == "ATIVA", Empresa.deletado_em.is_(None))
        .order_by(Contrato.id_contrato)
    ).all()
    caminho = current_app.url_map.bind("localhost").build("cliente.dashboard")
    for id_contrato, id_empresa, data_fim in contratos:
        texto = _texto_lembrete(data_fim)
        # Duas rodadas ao mesmo tempo: a segunda espera e já enxerga o que a primeira gravou.
        if aplicar and not _lembrete_segue_valido(id_contrato, id_empresa, data_fim):
            db.session.rollback()
            continue
        ids = list(db.session.scalars(select(Usuario.id_usuario).where(
            Usuario.id_empresa == id_empresa, Usuario.role == "CLIENTE", Usuario.ativo.is_(True),
            Usuario.primeiro_acesso.is_(False),
        ).order_by(Usuario.id_usuario)))
        desligados = ids_com_aviso_desligado("lembrete_renovacao", ids)
        ja_lembrados = set(db.session.scalars(select(Notificacao.id_usuario).where(
            Notificacao.id_usuario.in_(ids), Notificacao.texto == texto,
        ))) if ids else set()
        pendentes = [uid for uid in ids if uid not in desligados and uid not in ja_lembrados]
        relatorio.candidatos.append({"id_empresa": id_empresa, "id_contrato": id_contrato, "data_fim": data_fim,
                                     "pendentes": len(pendentes)})
        if aplicar:
            db.session.rollback()  # solta a trava da empresa
        if not aplicar or not pendentes:
            continue
        link = url_publica("cliente.dashboard")
        for uid in pendentes:
            # A trava vai até o commit, nunca durante o envio.
            if not _lembrete_segue_valido(id_contrato, id_empresa, data_fim):
                db.session.rollback()
                break
            usuario = db.session.get(Usuario, uid, populate_existing=True)
            if (usuario is None or not usuario.ativo or usuario.primeiro_acesso
                    or ids_com_aviso_desligado("lembrete_renovacao", [uid])
                    or db.session.scalar(select(Notificacao.id_notificacao).where(
                        Notificacao.id_usuario == uid, Notificacao.texto == texto).limit(1)) is not None):
                db.session.rollback()  # a pessoa deixou de contar, ou outra rodada já a avisou
                continue
            id_notificacao = criar_notificacao(uid, texto, caminho).id_notificacao
            db.session.commit()
            envio = email_lembrete_renovacao(db.session.get(Usuario, uid), data_fim, link)
            if envio.aceito:
                relatorio.notificados += 1
                relatorio.emails_aceitos += 1
            elif _recusa_definitiva(envio):
                relatorio.notificados += 1
                relatorio.emails_recusados += 1
                relatorio.recusados.append({"id_empresa": id_empresa, "id_usuario": uid})
            else:
                relatorio.emails_nao_aceitos += 1
                db.session.execute(Notificacao.__table__.delete().where(
                    Notificacao.id_notificacao == id_notificacao,
                ))
                db.session.commit()
    return relatorio


# ---------------------------------------------------------------------------
# Dados das telas
# ---------------------------------------------------------------------------
def situacao_smtp() -> dict:
    """Resumo do Diagnóstico de e-mail para o cartão das Configurações, sem segredos."""
    diagnostico = diagnostico_configuracao()
    conexao = ultima_conexao_smtp()
    instante = None
    if not diagnostico["ativo"]:
        rotulo, classe, detalhe = "DESATIVADO", "b-red", "O envio de e-mail está desligado: faltam as credenciais."
    elif diagnostico["transporte"] != "smtp":
        rotulo, classe, detalhe = "NÃO USADO", "b-muted", "O envio sai pela API do Brevo, sem conexão SMTP."
    elif diagnostico["bloqueante"]:
        rotulo, classe, detalhe = "BLOQUEADO", "b-red", "Há uma pendência na configuração que impede o envio."
    elif conexao is None:
        rotulo, classe, detalhe = "NÃO VERIFICADA", "b-muted", "Nenhuma tentativa desde o último início do servidor."
    elif conexao["ok"]:
        rotulo, classe, detalhe = "CONECTADO", "b-green", "O servidor aceitou a conexão e o login."
        instante = conexao["instante"]
    else:
        rotulo, classe = "FALHA", "b-red"
        detalhe = conexao["descricao"] or "Erro inesperado na conexão."
        instante = conexao["instante"]
    return {"rotulo": rotulo, "classe": classe, "detalhe": detalhe, "instante": instante,
            "servidor": diagnostico["smtp_endereco"] if diagnostico["transporte"] != "brevo" else None,
            "transporte": diagnostico["transporte_rotulo"], "ativo": diagnostico["ativo"],
            "testavel": diagnostico["transporte"] == "smtp"}


def contexto_cliente(usuario: Usuario) -> dict:
    disponivel = esquema_configuracoes_disponivel()
    return {
        "esquema_disponivel": disponivel,
        "preferencias": obter_preferencias(usuario.id_usuario),
        "documentos": situacao_documentos(usuario.id_usuario) if disponivel else [],
        "solicitacao_exclusao": solicitacao_exclusao_aberta(usuario.id_empresa),
        "dias_aviso_renovacao": current_app.config["CONTRATO_AVISO_RENOVACAO_DIAS"],
        "senha_minima": TAMANHO_MINIMO, "senha_maxima": TAMANHO_MAXIMO,
        "envio_ativo": envio_disponivel(current_app.config),
    }


def contexto_admin() -> dict:
    disponivel = esquema_configuracoes_disponivel()
    return {
        "esquema_disponivel": disponivel,
        "documentos": [{"tipo": tipo, "rotulo": rotulo, "vigente": documento_vigente(tipo) if disponivel else None}
                       for tipo, rotulo in TIPOS_DOCUMENTO.items()],
        "email_copia": email_copia_suporte(),
        "smtp": situacao_smtp(),
        "tamanho_maximo_documento": TAMANHO_MAXIMO_DOCUMENTO,
        "senha_minima": TAMANHO_MINIMO, "senha_maxima": TAMANHO_MAXIMO,
        "envio_ativo": envio_disponivel(current_app.config),
    }
