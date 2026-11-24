"""NEXO-SEC-004: lixeira transacional sem alterar a condição administrativa.
NEXO-AJ-06.1: o cancelamento comercial revoga sessões pelo mesmo protocolo.

Os helpers não confirmam nem revertem a transação. O chamador deve fazer
commit/rollback e só depois notificar ou desconectar conexões locais.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from models import Empresa, Usuario
from session_security import STATUS_CONTA_SEM_ACESSO, revogar_sessoes_usuario

# Mesmo domínio do CHECK de empresa.status_conta (models.py).
STATUS_CONTA = ("ATIVA", "SUSPENSA", STATUS_CONTA_SEM_ACESSO)


@dataclass(frozen=True)
class ResultadoLixeira:
    empresa: Empresa
    usuarios: tuple[Usuario, ...]

    @property
    def contas_bloqueadas(self) -> int:
        return sum(not usuario.ativo for usuario in self.usuarios)


def bloquear_empresa(sessao, id_empresa: int) -> Empresa | None:
    """Primeiro lock de escrita: serializa o ciclo e inserções via sua FK."""
    with sessao.no_autoflush:
        return sessao.scalar(select(Empresa).where(
            Empresa.id_empresa == id_empresa,
        ).with_for_update().execution_options(populate_existing=True))


def bloquear_usuarios_empresa(sessao, id_empresa: int) -> tuple[Usuario, ...]:
    """Após bloquear a empresa, bloquear seus CLIENTE em ordem de PK.

    Atualizar a geração de todos os usuários na mesma transação revoga as
    sessões deles: a próxima página ou consulta do sininho já é negada.
    """
    with sessao.no_autoflush:
        return tuple(sessao.scalars(select(Usuario).where(
            Usuario.id_empresa == id_empresa,
            Usuario.role == "CLIENTE",
        ).order_by(Usuario.id_usuario).with_for_update().execution_options(
            populate_existing=True,
        )))


def excluir_empresa(sessao, id_empresa: int) -> ResultadoLixeira | None:
    """Bloqueia pela empresa e revoga sessões, preservando ativo e credencial."""
    empresa = bloquear_empresa(sessao, id_empresa)
    if empresa is None or empresa.deletado_em is not None:
        return None
    usuarios = bloquear_usuarios_empresa(sessao, id_empresa)
    agora = datetime.now(timezone.utc)
    empresa.deletado_em = agora
    empresa.data_atualizacao = agora
    for usuario in usuarios:
        usuario.geracao_autorizacao += 1
        usuario.data_atualizacao = agora
        # SEC-001: as sessões registradas caem na mesma transação da lixeira.
        revogar_sessoes_usuario(sessao, usuario.id_usuario, "empresa")
    return ResultadoLixeira(empresa, usuarios)


def restaurar_empresa(sessao, id_empresa: int) -> ResultadoLixeira | None:
    """Retira da lixeira sem ativar contas nem reutilizar gerações revogadas."""
    empresa = bloquear_empresa(sessao, id_empresa)
    if empresa is None or empresa.deletado_em is None:
        return None
    usuarios = bloquear_usuarios_empresa(sessao, id_empresa)
    empresa.deletado_em = None
    empresa.data_atualizacao = datetime.now(timezone.utc)
    return ResultadoLixeira(empresa, usuarios)


def aplicar_status_conta(sessao, empresa: Empresa, novo_status: str) -> tuple[Usuario, ...]:
    """Troca o status comercial; entrar ou sair de CANCELADA revoga as sessões dos CLIENTE.

    A empresa já deve estar bloqueada por bloquear_empresa. Como na lixeira,
    os CLIENTE são bloqueados depois da empresa e a geração sobe na mesma
    transação: rollback não revoga nada e emissões anteriores terminam antes.
    Revogar também ao sair garante novo login após a reativação, mesmo que o
    cancelamento tenha sido gravado fora desta função (script ou processo).
    """
    revogados = ()
    if (novo_status == STATUS_CONTA_SEM_ACESSO) != (empresa.status_conta == STATUS_CONTA_SEM_ACESSO):
        revogados = bloquear_usuarios_empresa(sessao, empresa.id_empresa)
        agora = datetime.now(timezone.utc)
        for usuario in revogados:
            usuario.geracao_autorizacao += 1
            usuario.data_atualizacao = agora
            revogar_sessoes_usuario(sessao, usuario.id_usuario, "empresa")
    empresa.status_conta = novo_status
    return revogados
