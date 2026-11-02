"""Escolhas de aviso do usuário e disponibilidade das tabelas das Configurações.

Separado de configuracoes_conta.py porque notifications.py e emails.py precisam
destas consultas e configuracoes_conta.py depende de ambos.

Implantação expand -> migrate -> deploy, como a gestão contratual: antes da
migração c7d41e9a2b60 o portal segue funcionando, ninguém desligou aviso nenhum e
as telas informam que aqueles ajustes ainda não estão disponíveis.
"""

from dataclasses import dataclass
import logging
import time
from weakref import WeakKeyDictionary

from sqlalchemy import inspect, select

from extensions import db
from horario import agora_utc
from models import PreferenciaUsuario

logger = logging.getLogger(__name__)

TABELAS_CONFIGURACOES = ("preferencia_usuario", "documento_legal", "aceite_documento", "configuracao_sistema")
_REVERIFICAR_ESQUEMA_SEGUNDOS = 30.0
_esquema_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()

_COLUNAS_AVISO = {
    "lembrete_renovacao": PreferenciaUsuario.lembrete_renovacao,
    "alerta_chamado": PreferenciaUsuario.alerta_chamado,
}


def esquema_configuracoes_disponivel(sessao=None) -> bool:
    """True quando as tabelas da revisão c7d41e9a2b60 existem neste banco.

    Confirmada a presença, o resultado fica fixo para o engine; a ausência é
    reavaliada a cada 30 s, para a migração aplicada valer sem reiniciar o processo.
    Nenhuma decisão de acesso depende disto: esquema parcial só fica indisponível.
    """
    sessao = sessao or db.session
    engine = sessao.get_bind()
    estado = _esquema_por_engine.get(engine)
    if estado is True:
        return True
    if estado is not None and time.monotonic() - estado < _REVERIFICAR_ESQUEMA_SEGUNDOS:
        return False
    # A conexão da própria sessão: sem segunda conexão nem reset de pool.
    inspetor = inspect(sessao.connection())
    presentes = {tabela for tabela in TABELAS_CONFIGURACOES if inspetor.has_table(tabela)}
    if presentes == set(TABELAS_CONFIGURACOES):
        _esquema_por_engine[engine] = True
        return True
    if presentes:
        logger.warning("Esquema das Configurações parcial: confira a migração c7d41e9a2b60.")
    _esquema_por_engine[engine] = time.monotonic()
    return False


@dataclass(frozen=True)
class Preferencias:
    lembrete_renovacao: bool = True
    alerta_chamado: bool = True


def obter_preferencias(id_usuario: int) -> Preferencias:
    if not esquema_configuracoes_disponivel():
        return Preferencias()
    linha = db.session.get(PreferenciaUsuario, id_usuario)
    if linha is None:
        return Preferencias()
    return Preferencias(linha.lembrete_renovacao, linha.alerta_chamado)


def salvar_preferencias(id_usuario: int, *, lembrete_renovacao: bool, alerta_chamado: bool) -> None:
    """Grava (sem commit) as escolhas do usuário."""
    linha = db.session.get(PreferenciaUsuario, id_usuario)
    if linha is None:
        linha = PreferenciaUsuario(id_usuario=id_usuario)
        db.session.add(linha)
    linha.lembrete_renovacao = lembrete_renovacao
    linha.alerta_chamado = alerta_chamado
    linha.data_atualizacao = agora_utc()


def ids_com_aviso_desligado(aviso: str, ids) -> set[int]:
    """Os usuários, entre `ids`, que desligaram este aviso. Sem a tabela, nenhum."""
    ids = [identificador for identificador in ids if identificador is not None]
    if not ids or not esquema_configuracoes_disponivel():
        return set()
    return set(db.session.scalars(select(PreferenciaUsuario.id_usuario).where(
        PreferenciaUsuario.id_usuario.in_(ids), _COLUNAS_AVISO[aviso].is_(False),
    )))
