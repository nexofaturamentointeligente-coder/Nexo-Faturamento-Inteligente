"""Evolução & Conquistas: os retratos mensais do Raio-X do PDV.

Revision ID: d9e4b7a2c6f1
Revises: c5f8a1d3e7b9

Expand com uma tabela nova (Etapa 3, 14/11/2026). Nenhuma tabela existente muda (regras G.1 e G.4 de
Docs/REESTRUTURACAO_COMERCIAL/00_02_PROTECAO_BANCO_MIGRACOES_ETAPA_0_2_PR22.md):
  - diagnostico_fechamento: um retrato por empresa e mês do Raio-X (competencia, o dia 1 do mês no horário de
    Brasília), só com agregados em JSON (ritmos por mês, percentuais e contagens; nenhum nome de produto). O Raio-X
    refeito no mesmo mês substitui o retrato daquele mês (UNIQUE id_empresa + competencia, que também serve de
    índice da empresa), e o primeiro retrato é o Marco Zero com que os seguintes se comparam. A linha sai com a
    empresa por ON DELETE CASCADE, como a diagnostico_entrada.

`models` não é importado. O código funciona antes desta migração (raio_x_servico.fechamentos_disponiveis): sem a
tabela, o Raio-X grava igual, sem o retrato, e o painel Evolução & Conquistas mostra o estado inicial.

No PostgreSQL a tabela nasce com RLS ligado, como as da b3e9d6c1f7a2. Onde existirem os papéis da Data API do
Supabase (anon, authenticated), os privilégios deles são revogados e a política restritiva deny_api_access lhes nega
tudo; postgres e service_role, onde existirem, recebem a política acesso_servidor. Num PostgreSQL sem esses papéis
(testes, restauração isolada) as políticas não são criadas: a aplicação entra como dona da tabela e o RLS não a afeta.

O downgrade recusa apagar dados: só remove a tabela vazia.
"""

from alembic import op
import sqlalchemy as sa


revision = "d9e4b7a2c6f1"
down_revision = "c5f8a1d3e7b9"
branch_labels = None
depends_on = None

_TABELA = "diagnostico_fechamento"

# Sem "%" no texto: a prévia offline (flask db upgrade --sql) sai igual ao que roda no banco.
_POLITICAS_POSTGRESQL = """
DO $$
DECLARE
    tabela text;
    papeis_api text;
    papeis_servidor text;
BEGIN
    SELECT string_agg(quote_ident(rolname), ', ' ORDER BY rolname) INTO papeis_api
      FROM pg_roles WHERE rolname IN ('anon', 'authenticated');
    SELECT string_agg(quote_ident(rolname), ', ' ORDER BY rolname) INTO papeis_servidor
      FROM pg_roles WHERE rolname IN ('postgres', 'service_role');
    FOREACH tabela IN ARRAY ARRAY['diagnostico_fechamento'] LOOP
        IF papeis_api IS NOT NULL THEN
            EXECUTE 'REVOKE ALL ON TABLE ' || quote_ident(tabela) || ' FROM ' || papeis_api;
            EXECUTE 'CREATE POLICY deny_api_access ON ' || quote_ident(tabela)
                 || ' AS RESTRICTIVE FOR ALL TO ' || papeis_api || ' USING (false) WITH CHECK (false)';
        END IF;
        IF papeis_servidor IS NOT NULL THEN
            EXECUTE 'CREATE POLICY acesso_servidor ON ' || quote_ident(tabela)
                 || ' AS PERMISSIVE FOR ALL TO ' || papeis_servidor || ' USING (true) WITH CHECK (true)';
        END IF;
    END LOOP;
END
$$;
"""


def upgrade():
    op.create_table(
        _TABELA,
        sa.Column("id_fechamento", sa.Integer(), nullable=False),
        sa.Column("id_empresa", sa.Integer(), nullable=False),
        sa.Column("competencia", sa.Date(), nullable=False),
        sa.Column("gerado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("versao_regras", sa.String(length=40), nullable=False),
        sa.Column("indicadores", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["id_empresa"], ["empresa.id_empresa"], name="fk_diagnostico_fechamento_empresa",
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id_fechamento"),
        sa.UniqueConstraint("id_empresa", "competencia", name="uq_diagnostico_fechamento_empresa_competencia"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {_TABELA} ENABLE ROW LEVEL SECURITY")
        op.execute(_POLITICAS_POSTGRESQL)


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma escrita em andamento não aparece na contagem e seria apagada pelo DROP: espera por ela.
        # usuario e empresa vêm primeiro, na ordem das requisições; com tráfego, repita se abortar.
        op.execute(f"LOCK TABLE usuario, empresa, {_TABELA} IN ACCESS EXCLUSIVE MODE")
    if conexao.execute(sa.text(f"SELECT COUNT(*) FROM {_TABELA}")).scalar():
        raise RuntimeError(
            "Downgrade da evolução mensal recusado: há retratos do Raio-X gravados em diagnostico_fechamento. "
            "Faça backup e decida o que fazer com esses dados antes de remover a tabela."
        )
    op.drop_table(_TABELA)
