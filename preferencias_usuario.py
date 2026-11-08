"""Escolhas de aviso do usuário e disponibilidade das tabelas das Configurações.

Separado de configuracoes_conta.py porque notifications.py e emails.py precisam
destas consultas e configuracoes_conta.py depende de ambos.

Implantação expand -> migrate -> deploy, como a gestão contratual: antes das
migrações c7d41e9a2b60 e a8d3f6b91c24 o portal segue funcionando, ninguém desligou
aviso nenhum, nenhum aceite é exigido e as telas informam que aqueles ajustes ainda
não estão disponíveis.
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
# Colunas da revisão a8d3f6b91c24: os modelos já as mapeiam, e consultar sem elas falharia.
COLUNAS_CONFIGURACOES = {"documento_legal": ("versao_maior", "versao_menor"), "aceite_documento": ("ip_usuario",)}
_REVERIFICAR_ESQUEMA_SEGUNDOS = 30.0
_esquema_por_engine: "WeakKeyDictionary" = WeakKeyDictionary()

_COLUNAS_AVISO = {
    "lembrete_renovacao": PreferenciaUsuario.lembrete_renovacao,
    "alerta_chamado": PreferenciaUsuario.alerta_chamado,
}


def esquema_configuracoes_disponivel(sessao=None) -> bool:
    """True quando as tabelas da revisão c7d41e9a2b60 e as colunas da a8d3f6b91c24
    existem neste banco.

    Confirmada a presença, o resultado fica fixo para o engine; a ausência é
    reavaliada a cada 30 s, para a migração aplicada valer sem reiniciar o processo.
    A trava de aceite dos termos depende disto e falha aberta: esquema ausente ou
    parcial só deixa os ajustes indisponíveis, e ninguém é barrado.
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
    completo = presentes == set(TABELAS_CONFIGURACOES) and all(
        set(colunas) <= {coluna["name"] for coluna in inspetor.get_columns(tabela)}
        for tabela, colunas in COLUNAS_CONFIGURACOES.items()
    )
    if completo:
        _esquema_por_engine[engine] = True
        return True
    if presentes:
        logger.warning("Esquema das Configurações parcial: confira as migrações c7d41e9a2b60 e a8d3f6b91c24.")
    _esquema_por_engine[engine] = time.monotonic()
    return False


def esquecer_esquema(sessao=None) -> None:
    """Uma consulta às tabelas falhou (ex.: downgrade com o processo no ar): a próxima
    chamada de esquema_configuracoes_disponivel confere o banco de novo."""
    _esquema_por_engine.pop((sessao or db.session).get_bind(), None)


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
