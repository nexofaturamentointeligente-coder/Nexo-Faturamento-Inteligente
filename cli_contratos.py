"""Comandos Flask da gestão contratual (simulação por padrão).

    flask contratos reconciliar            # lista suspensões vencidas, nada grava
    flask contratos reconciliar --aplicar  # cancela as vencidas (idempotente)
    flask contratos lembretes-renovacao            # lista quem seria avisado, nada grava
    flask contratos lembretes-renovacao --aplicar  # avisa no sininho e por e-mail (uma vez por pessoa)
    flask planos atualizar-precos          # compara o banco com o catálogo
    flask planos atualizar-precos --aplicar --confirmo-janela-autorizada

Nenhum comando roda sozinho: não há agendador configurado. Contra produção,
só numa janela de implantação autorizada (Docs/GESTAO_CONTRATUAL/04_...).
A saída não mostra credenciais nem dados pessoais, apenas identificadores.
"""

import click
from flask import current_app
from sqlalchemy.engine import make_url

import configuracoes_conta
import contract_admin
import plan_catalog
from contract_policy import agora, formatar_instante
from emails import envio_disponivel
from extensions import db


def _destino_banco() -> str:
    url = make_url(current_app.config["SQLALCHEMY_DATABASE_URI"])
    return f"{url.get_backend_name()} / banco {url.database or '?'}"


def registrar_comandos(app):
    @app.cli.group("contratos")
    def contratos():
        """Gestão contratual."""

    @contratos.command("reconciliar")
    @click.option("--aplicar", is_flag=True, help="Grava os cancelamentos (padrão: simulação).")
    def reconciliar(aplicar):
        """Cancela suspensões vencidas de empresas no regime contratual."""
        click.echo(f"Destino: {_destino_banco()}")
        try:
            relatorio = contract_admin.reconciliar_vencimentos(db.session, aplicar=aplicar)
            if aplicar:
                db.session.commit()
            else:
                db.session.rollback()
        except Exception:
            db.session.rollback()
            raise
        modo = "APLICAÇÃO" if aplicar else "SIMULAÇÃO (nada foi gravado)"
        click.echo(f"Modo: {modo} — instante {formatar_instante(relatorio.instante)}")
        if relatorio.bloqueada:
            click.echo("Outra reconciliação está em andamento; nada foi feito.")
            raise SystemExit(2)
        for item in relatorio.candidatos:
            click.echo(f"  empresa {item['id_empresa']} contrato {item['id_contrato']} "
                       f"limite {formatar_instante(item['suspensao_limite'])}")
        click.echo(f"Vencidos: {len(relatorio.candidatos)}; cancelados: {len(relatorio.cancelados)}; "
                   f"sessões revogadas: {len(relatorio.revogados)}")

    @contratos.command("lembretes-renovacao")
    @click.option("--aplicar", is_flag=True, help="Cria os avisos e envia os e-mails (padrão: simulação).")
    def lembretes_renovacao(aplicar):
        """Avisa os clientes cujo contrato termina dentro de CONTRATO_AVISO_RENOVACAO_DIAS."""
        click.echo(f"Destino: {_destino_banco()}")
        if aplicar and not envio_disponivel(current_app.config):
            click.echo("O envio de e-mail está indisponível: nada foi gravado. Confira o Diagnóstico de e-mail.")
            raise SystemExit(1)
        try:
            relatorio = configuracoes_conta.enviar_lembretes_renovacao(aplicar=aplicar)
            if not aplicar:
                db.session.rollback()
        except Exception:
            db.session.rollback()
            raise
        modo = "APLICAÇÃO" if aplicar else "SIMULAÇÃO (nada foi gravado)"
        click.echo(f"Modo: {modo} — instante {formatar_instante(relatorio.instante)}")
        for item in relatorio.candidatos:
            click.echo(f"  empresa {item['id_empresa']} contrato {item['id_contrato']} "
                       f"vence {item['data_fim']:%d/%m/%Y}: {item['pendentes']} pessoa(s) a avisar")
        click.echo(f"Contratos na janela de {relatorio.janela_dias} dias: {len(relatorio.candidatos)}; "
                   f"pessoas avisadas: {relatorio.notificados}; e-mails aceitos: {relatorio.emails_aceitos}")
        if relatorio.emails_recusados:
            click.echo(f"Endereços recusados em definitivo: {relatorio.emails_recusados}. Essas pessoas ficam só com o sininho; corrija o e-mail delas.")
            for item in relatorio.recusados:
                click.echo(f"  empresa {item['id_empresa']} usuário {item['id_usuario']}")
        if relatorio.emails_nao_aceitos:
            click.echo(f"E-mails não aceitos: {relatorio.emails_nao_aceitos}. A próxima execução tenta de novo; se continuar, confira o Diagnóstico de e-mail.")
        if relatorio.emails_recusados or relatorio.emails_nao_aceitos:
            raise SystemExit(1)

    @app.cli.group("planos")
    def planos():
        """Catálogo comercial."""

    @planos.command("atualizar-precos")
    @click.option("--aplicar", is_flag=True, help="Grava os novos preços (padrão: simulação).")
    @click.option("--confirmo-janela-autorizada", "confirmado", is_flag=True,
                  help="Obrigatório para gravar: confirma backup e janela aprovada.")
    def atualizar_precos(aplicar, confirmado):
        """Alinha plano.valor_mensal ao catálogo, com histórico; contratos não mudam."""
        click.echo(f"Destino: {_destino_banco()}")
        if aplicar and not confirmado:
            raise click.UsageError("Para gravar, informe também --confirmo-janela-autorizada.")
        try:
            if aplicar:
                alteracoes = plan_catalog.aplicar_atualizacao_precos(db.session, instante=agora())
                db.session.commit()
            else:
                alteracoes = plan_catalog.planejar_atualizacao_precos(db.session)
                db.session.rollback()
        except Exception:
            db.session.rollback()
            raise
        click.echo("Modo: " + ("APLICAÇÃO" if aplicar else "SIMULAÇÃO (nada foi gravado)"))
        if not alteracoes:
            click.echo("Catálogo já alinhado; nenhuma alteração necessária.")
        for item in alteracoes:
            click.echo(f"  {item['plano'].nome_plano}: {item['anterior']} -> {item['novo']}")
