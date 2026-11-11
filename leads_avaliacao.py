"""Avaliação de Cenário: retenção dos dados de PDV dos leads (TTL de 30 dias).

    flask leads expurgar-pdv              # simulação: conta o que sairia e desfaz tudo
    flask leads expurgar-pdv --aplicar    # apaga as linhas e marca os leads expirados
    flask leads expurgar-pdv --dias 45    # outro prazo (mínimo de 1 dia)
    flask leads retriar <id do lead>      # refaz marcas e triagem a partir dos dados gravados
    flask leads retriar <id> --limiares '{"margem_baixa": "0.08"}'   # com limiares trocados
    flask leads agendar-expurgo           # agenda (ou reagenda) o expurgo diário no pg_cron

Regra: as linhas de brainstorming_dados_pdv com mais de `dias` dias, de leads que não
converteram, saem; o lead que fica sem nenhuma linha passa a 'expirado' e perde também os
indicadores executivos do envio (indicadores_executivos_json, que trazem nome de produto). O resumo
em lead_avaliacao (score, vazamento total, plano recomendado e prioridades) fica.

No PostgreSQL quem expurga é a função expurgar_brainstorming_pdv da revisão f4c2a8e1d9b6; nos
demais bancos (o SQLite de testes e de desenvolvimento), a mesma regra em SQLAlchemy.

Agendamento: a revisão 6a8f2d4c0e13 agenda a função todo dia às 03:15 de Brasília no pg_cron do
próprio banco, onde a extensão estiver habilitada (no Supabase, Database › Extensions). Se ela for
habilitada depois da migração, `flask leads agendar-expurgo` agenda o mesmo job, sem downgrade. Sem
pg_cron, `expurgar-pdv` é o caminho (um cron externo pode chamá-lo). Rodar contra produção só com
autorização. A saída mostra só contagens, nunca dados pessoais.
"""

from dataclasses import dataclass
from datetime import timedelta
import json

import click
from sqlalchemy import delete, exists, select, text, update

from extensions import db
from horario import agora_utc
from models import BrainstormingDadosPdv, LeadAvaliacao

RETENCAO_DIAS = 30
_LOTE = 500
# Os mesmos da revisão 6a8f2d4c0e13 (agendar com o mesmo nome atualiza o job).
NOME_DO_JOB_DE_EXPURGO = "nexo_expurgo_leads_pdv"
AGENDA_DO_EXPURGO = "15 6 * * *"


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
        # Os indicadores executivos do envio trazem nome de produto: saem com as linhas (revisão c5f8a1d3e7b9).
        sessao.execute(
            update(LeadAvaliacao)
            .where(LeadAvaliacao.id.in_(lote), LeadAvaliacao.status != "convertido",
                   LeadAvaliacao.indicadores_executivos_json.is_not(None), antigas, ~recentes)
            .values(indicadores_executivos_json=None)
            .execution_options(synchronize_session=False)
        )
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

    @leads.command("agendar-expurgo")
    def agendar_expurgo():
        """Agenda (ou reagenda) no pg_cron o expurgo diário de 30 dias, como a revisão 6a8f2d4c0e13."""
        from cli_contratos import _destino_banco
        click.echo(f"Destino: {_destino_banco()}")
        if db.session.get_bind().dialect.name != "postgresql":
            raise click.ClickException("O agendamento usa o pg_cron do PostgreSQL; este banco não é PostgreSQL.")
        tem_cron = db.session.execute(text(
            "SELECT to_regprocedure('cron.schedule(text,text,text)') IS NOT NULL")).scalar()
        if not tem_cron:
            db.session.rollback()
            raise click.ClickException("pg_cron ausente: habilite a extensão (no Supabase: Database > Extensions "
                                       "> pg_cron) e rode o comando de novo. Nada foi agendado.")
        try:
            db.session.execute(text(
                f"SELECT cron.schedule('{NOME_DO_JOB_DE_EXPURGO}', '{AGENDA_DO_EXPURGO}', "
                "'SELECT * FROM ' || quote_ident(current_schema()) || '.expurgar_brainstorming_pdv(30)')"))
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        click.echo(f"Expurgo agendado: job {NOME_DO_JOB_DE_EXPURGO}, todo dia às 06:15 UTC (03:15 em Brasília).")

    @leads.command("retriar")
    @click.argument("lead_id")
    @click.option("--limiares", "limiares_json", default=None,
                  help='Limiares a trocar, em JSON: {"margem_baixa": "0.08", "relatorios_para_giro_critico": 6}. '
                       "Sem a opção, valem os gravados no lead.")
    def retriar(lead_id, limiares_json):
        """Refaz as marcas e a triagem de um lead a partir dos dados de PDV gravados (sem os arquivos)."""
        import uuid
        import avaliacao_cenario
        from models import LeadAvaliacao
        try:
            identificador = uuid.UUID(lead_id)
        except ValueError:
            raise click.BadParameter("Identificador de lead inválido.", param_hint="LEAD_ID") from None
        limiares = None
        if limiares_json is not None:
            lead_atual = db.session.get(LeadAvaliacao, identificador)
            if lead_atual is None:
                raise click.ClickException("Lead inexistente.")
            try:
                ajustes = json.loads(limiares_json)
                # A base são os limiares gravados (ou os padrão atuais, se os gravados forem de outra versão).
                base, _aviso = avaliacao_cenario.limiares_para_retriagem(lead_atual)
                limiares = base.com_ajustes(ajustes)
            except (ValueError, RecursionError) as erro:
                raise click.BadParameter(f"Limiares inválidos: {erro}", param_hint="--limiares") from None
        try:
            lead = avaliacao_cenario.retriar(identificador, limiares)
            db.session.commit()
        except avaliacao_cenario.LeadIndisponivel as erro:
            db.session.rollback()
            raise click.ClickException(str(erro)) from None
        except Exception:
            db.session.rollback()
            raise
        blocos = {nome: json.loads(getattr(lead, f"resumo_{nome}_json"))["itens"]
                  for nome in ("urgencia", "atencao", "eletiva")}
        click.echo(f"Triagem refeita ({lead.versao_triagem}): urgência {blocos['urgencia']}, atenção "
                   f"{blocos['atencao']}, eletiva {blocos['eletiva']}; itens com venda sem custo "
                   f"{lead.itens_venda_sem_custo}, abaixo do custo {lead.itens_venda_abaixo_custo}, com omissão "
                   f"no ERP {lead.itens_omissao_erp}.")
        if getattr(lead, "aviso_limiares", None):
            click.echo(f"Aviso: {lead.aviso_limiares}")
