"""Avaliação de Cenário: retenção dos dados de PDV dos leads (TTL de 30 dias).

    flask leads expurgar-pdv              # simulação: conta o que sairia e desfaz tudo
    flask leads expurgar-pdv --aplicar    # apaga as linhas e marca os leads expirados
    flask leads expurgar-pdv --dias 45    # outro prazo (mínimo de 1 dia)

Regra: as linhas de brainstorming_dados_pdv com mais de `dias` dias, de leads que não
converteram, saem; o lead que fica sem nenhuma linha passa a 'expirado'. O resumo em
lead_avaliacao (score, vazamento total, plano recomendado e prioridades) fica.

No PostgreSQL quem expurga é a função expurgar_brainstorming_pdv da revisão f4c2a8e1d9b6, a
mesma que um agendador dentro do banco chamaria; nos demais bancos (o SQLite de testes e de
desenvolvimento), a mesma regra em SQLAlchemy. Nenhum agendador está configurado: rodar
contra produção só com autorização. A saída mostra só contagens, nunca dados pessoais.
"""

from dataclasses import dataclass
from datetime import timedelta

import click
from sqlalchemy import delete, exists, select, text, update

from extensions import db
from horario import agora_utc
from models import BrainstormingDadosPdv, LeadAvaliacao

RETENCAO_DIAS = 30
_LOTE = 500


@dataclass(frozen=True)
class ResultadoExpurgo:
    leads_expirados: int
    linhas_removidas: int


def expurgar_dados_pdv(sessao, dias: int = RETENCAO_DIAS, *, agora=None) -> ResultadoExpurgo:
    """Expurga (sem commit) os dados de PDV vencidos dos leads não convertidos.

    `agora` existe para os testes: com ele, também no PostgreSQL a regra roda em SQLAlchemy,
    no relógio informado, em vez da função do banco (que usa o relógio do banco).
    """
    if type(dias) is not int or dias < 1:
        raise ValueError("O prazo de retenção precisa ser de pelo menos 1 dia.")
    if agora is None and sessao.get_bind().dialect.name == "postgresql":
        leads, linhas = sessao.execute(
            text("SELECT leads_expirados, linhas_removidas FROM expurgar_brainstorming_pdv(:dias)"),
            {"dias": dias},
        ).one()
        return ResultadoExpurgo(int(leads), int(linhas))
    return _expurgar_em_sqlalchemy(sessao, dias, agora or agora_utc())


def _expurgar_em_sqlalchemy(sessao, dias: int, agora) -> ResultadoExpurgo:
    corte = agora - timedelta(days=dias)
    antigas = exists().where(BrainstormingDadosPdv.lead_id == LeadAvaliacao.id,
                             BrainstormingDadosPdv.criado_em < corte)
    recentes = exists().where(BrainstormingDadosPdv.lead_id == LeadAvaliacao.id,
                              BrainstormingDadosPdv.criado_em >= corte)
    # Trava os leads antes dos dados, como a função do banco e a gravação da aplicação.
    alvos = list(sessao.scalars(
        select(LeadAvaliacao.id).where(LeadAvaliacao.status != "convertido", antigas)
        .order_by(LeadAvaliacao.id).with_for_update()
    ))
    expirados = removidas = 0
    for inicio in range(0, len(alvos), _LOTE):
        lote = alvos[inicio:inicio + _LOTE]
        expirados += sessao.execute(
            update(LeadAvaliacao)
            .where(LeadAvaliacao.id.in_(lote), LeadAvaliacao.status != "expirado", antigas, ~recentes)
            .values(status="expirado", atualizado_em=agora)
            .execution_options(synchronize_session=False)
        ).rowcount
        removidas += sessao.execute(
            delete(BrainstormingDadosPdv)
            .where(BrainstormingDadosPdv.lead_id.in_(lote), BrainstormingDadosPdv.criado_em < corte)
            .execution_options(synchronize_session=False)
        ).rowcount
    return ResultadoExpurgo(expirados, removidas)


def registrar_comandos(app):
    @app.cli.group("leads")
    def leads():
        """Avaliação de Cenário (leads)."""

    @leads.command("expurgar-pdv")
    @click.option("--dias", type=click.IntRange(min=1), default=RETENCAO_DIAS, show_default=True,
                  help="Idade mínima, em dias, das linhas de PDV que saem.")
    @click.option("--aplicar", is_flag=True, help="Grava o expurgo (padrão: simulação, que desfaz tudo).")
    def expurgar_pdv(dias, aplicar):
        """Apaga os dados de PDV vencidos de leads não convertidos; o resumo do lead fica."""
        from cli_contratos import _destino_banco
        click.echo(f"Destino: {_destino_banco()}")
        try:
            resultado = expurgar_dados_pdv(db.session, dias)
            if aplicar:
                db.session.commit()
            else:
                db.session.rollback()
        except Exception:
            db.session.rollback()
            raise
        click.echo("Modo: " + ("APLICAÇÃO" if aplicar else "SIMULAÇÃO (nada foi gravado)"))
        click.echo(f"Linhas de PDV com mais de {dias} dias {'removidas' if aplicar else 'a remover'}: "
                   f"{resultado.linhas_removidas}; leads {'expirados' if aplicar else 'a expirar'}: "
                   f"{resultado.leads_expirados}")
