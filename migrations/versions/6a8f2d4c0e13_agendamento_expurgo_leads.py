"""Avaliação de Cenário: agendamento diário do expurgo de 30 dias no próprio banco (pg_cron).

Revision ID: 6a8f2d4c0e13
Revises: 5e1b9c3a7d20

Onde a extensão pg_cron está habilitada (no Supabase: Database › Extensions › pg_cron), agenda o
job 'nexo_expurgo_leads_pdv' para rodar todo dia às 06:15 UTC (03:15 em Brasília) a função
expurgar_brainstorming_pdv(30) da revisão f4c2a8e1d9b6, a mesma do comando
`flask leads expurgar-pdv --aplicar`. O job roda dentro do banco: não depende do servidor web (que
dorme no plano gratuito do Render) nem de um serviço de cron pago. A função continua com o
EXECUTE revogado de PUBLIC, anon e authenticated; o job roda com o papel que aplicou a migração.

Sem pg_cron (PostgreSQL sem a extensão, SQLite), a revisão só avisa (WARNING) e o expurgo segue
pelo comando, que um cron externo pode chamar, e pela própria tela /admin/leads, que roda a mesma
regra no máximo uma vez por hora ao ser aberta (blueprints/leads.expurgo_oportunista). O pg_cron
roda os jobs no banco de cron.database_name (no Supabase, "postgres", o mesmo da aplicação).

Reagendar com o mesmo nome atualiza o job. O downgrade remove o job, se existir. Nenhum dado é
tocado.
"""

from alembic import op


revision = "6a8f2d4c0e13"
down_revision = "5e1b9c3a7d20"
branch_labels = None
depends_on = None

NOME_DO_JOB = "nexo_expurgo_leads_pdv"
AGENDA = "15 6 * * *"

# Sem "%" no texto: a prévia offline (flask db upgrade --sql) sai igual ao que roda no banco. O IF
# aninhado evita citar cron.job onde o esquema cron não existe.
_AGENDAR = f"""
DO $bloco$
BEGIN
    IF to_regprocedure('cron.schedule(text,text,text)') IS NULL THEN
        RAISE WARNING 'pg_cron ausente: o expurgo diário dos leads NÃO foi agendado. Habilite a extensão e reaplique esta revisão, ou rode flask leads expurgar-pdv --aplicar por um cron externo; a tela /admin/leads também expurga ao ser aberta.';
        RETURN;
    END IF;
    PERFORM cron.schedule('{NOME_DO_JOB}', '{AGENDA}',
                          'SELECT * FROM ' || quote_ident(current_schema()) || '.expurgar_brainstorming_pdv(30)');
END
$bloco$;
"""

_DESAGENDAR = f"""
DO $bloco$
BEGIN
    IF to_regprocedure('cron.unschedule(text)') IS NOT NULL AND to_regclass('cron.job') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM cron.job WHERE jobname = '{NOME_DO_JOB}') THEN
            PERFORM cron.unschedule('{NOME_DO_JOB}');
        END IF;
    END IF;
END
$bloco$;
"""


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_AGENDAR)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_DESAGENDAR)
