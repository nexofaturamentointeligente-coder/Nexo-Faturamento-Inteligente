"""Avaliação de Cenário: captação de leads e a matriz de dados do PDV do lead, com expurgo em 30 dias.

Revision ID: f4c2a8e1d9b6
Revises: b3e9d6c1f7a2

Expand com duas tabelas novas, sem tocar nas existentes (regras G.1 de
Docs/REESTRUTURACAO_COMERCIAL/00_02_PROTECAO_BANCO_MIGRACOES_ETAPA_0_2_PR22.md):
  - lead_avaliacao: o contato de quem pediu a avaliação (nome, e-mail, WhatsApp e loja), o
    resumo do resultado (score de saúde cadastral de 0 a 100, vazamento total em R$, plano
    recomendado e os três blocos de prioridade em JSON) e a situação do lead.
  - brainstorming_dados_pdv: uma linha por produto e ano de exercício do PDV enviado pelo
    lead, com as marcas de venda sem custo, abaixo do custo e omissão no ERP e o vazamento
    estimado da linha. Sai com o lead (ON DELETE CASCADE).

Os nomes `vazamento_total_r$` e `vazamento_estimado_r$` seguem a especificação. O PostgreSQL
e o SQLite aceitam o cifrão no meio do identificador; nos modelos os atributos Python são
vazamento_total_reais e vazamento_estimado_reais.

No PostgreSQL as duas tabelas nascem com RLS ligado e a mesma política condicional da revisão
b3e9d6c1f7a2: onde existirem anon e authenticated (Data API do Supabase), os privilégios
deles são revogados e a política restritiva deny_api_access lhes nega tudo; postgres e
service_role, onde existirem, recebem acesso_servidor. A aplicação entra como dona das
tabelas e o RLS não a afeta.

Expurgo (TTL de 30 dias): a função expurgar_brainstorming_pdv(dias) apaga as linhas de
brainstorming_dados_pdv com mais de `dias` dias de leads não convertidos e marca como
'expirado' o lead que ficou sem nenhuma linha; o resumo em lead_avaliacao fica. Roda com o
privilégio de quem chama (SECURITY INVOKER), com search_path fixo, e o EXECUTE é revogado de
PUBLIC, anon e authenticated. Nada a agenda: `flask leads expurgar-pdv --aplicar` chama a
mesma função (ver leads_avaliacao.py).

O downgrade recusa apagar dados: só remove as estruturas com as duas tabelas vazias.
"""

from alembic import op
import sqlalchemy as sa


revision = "f4c2a8e1d9b6"
down_revision = "b3e9d6c1f7a2"
branch_labels = None
depends_on = None

_TABELAS = ("lead_avaliacao", "brainstorming_dados_pdv")
_STATUS = ("pendente", "em_processamento", "concluido", "processamento_manual", "convertido", "expirado")

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
    FOREACH tabela IN ARRAY ARRAY['lead_avaliacao', 'brainstorming_dados_pdv'] LOOP
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

# A função nasce no esquema corrente da migração, com esse esquema fixo no search_path e o
# pg_temp por último: um objeto homônimo de outro esquema ou uma tabela temporária da sessão não
# é usado no lugar das tabelas. Os leads são travados antes de apagar as linhas, na ordem da
# aplicação (lead e depois dados), e um lead só vira 'expirado' quando ainda tem linha vencida a
# apagar e não sobra nenhuma recente. Trava, atualização e exclusão são comandos separados: em
# READ COMMITTED cada um enxerga o que foi confirmado antes dele, então o que quem segurava o
# lead gravou (linha nova, linhas apagadas, status) vale.
_FUNCAO_EXPURGO = """
DO $bloco$
DECLARE
    esquema text := quote_ident(current_schema());
BEGIN
    EXECUTE 'CREATE FUNCTION ' || esquema || $cria$.expurgar_brainstorming_pdv(dias integer DEFAULT 30)
RETURNS TABLE (leads_expirados integer, linhas_removidas integer)
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, $cria$ || esquema || $cria$, pg_temp
AS $corpo$
DECLARE
    corte timestamptz;
    alvos uuid[];
BEGIN
    IF dias IS NULL OR dias < 1 THEN
        RAISE EXCEPTION 'O prazo de retenção precisa ser de pelo menos 1 dia.';
    END IF;
    corte := now() - make_interval(days => dias);
    alvos := ARRAY(
        SELECT l.id
          FROM lead_avaliacao l
         WHERE l.status <> 'convertido'
           AND EXISTS (SELECT 1 FROM brainstorming_dados_pdv b WHERE b.lead_id = l.id AND b.criado_em < corte)
         ORDER BY l.id
           FOR UPDATE);
    UPDATE lead_avaliacao l
       SET status = 'expirado', atualizado_em = now()
     WHERE l.id = ANY (alvos)
       AND l.status NOT IN ('expirado', 'convertido')
       AND EXISTS (SELECT 1 FROM brainstorming_dados_pdv b WHERE b.lead_id = l.id AND b.criado_em < corte)
       AND NOT EXISTS (SELECT 1 FROM brainstorming_dados_pdv b WHERE b.lead_id = l.id AND b.criado_em >= corte);
    GET DIAGNOSTICS leads_expirados = ROW_COUNT;
    DELETE FROM brainstorming_dados_pdv b
     USING lead_avaliacao l
     WHERE b.lead_id = l.id
       AND l.id = ANY (alvos)
       AND l.status <> 'convertido'
       AND b.criado_em < corte;
    GET DIAGNOSTICS linhas_removidas = ROW_COUNT;
    RETURN NEXT;
END
$corpo$$cria$;
    EXECUTE 'REVOKE ALL ON FUNCTION ' || esquema || '.expurgar_brainstorming_pdv(integer) FROM PUBLIC';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION ' || esquema || '.expurgar_brainstorming_pdv(integer) FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION ' || esquema || '.expurgar_brainstorming_pdv(integer) FROM authenticated';
    END IF;
END
$bloco$;
"""


def upgrade():
    status = ", ".join(f"'{valor}'" for valor in _STATUS)
    op.create_table(
        "lead_avaliacao",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("nome_contato", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("whatsapp", sa.String(length=20), nullable=True),
        sa.Column("nome_loja", sa.String(length=120), nullable=False),
        sa.Column("score_saude_cadastral", sa.Numeric(5, 2), nullable=True),
        sa.Column("vazamento_total_r$", sa.Numeric(12, 2), nullable=True),
        sa.Column("status", sa.String(length=30), server_default=sa.text("'pendente'"), nullable=False),
        sa.Column("plano_recomendado_codigo", sa.String(length=20), nullable=True),
        sa.Column("resumo_urgencia_json", sa.Text(), nullable=True),
        sa.Column("resumo_atencao_json", sa.Text(), nullable=True),
        sa.Column("resumo_eletiva_json", sa.Text(), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"),
                  nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"),
                  nullable=False),
        sa.CheckConstraint(f"status IN ({status})", name="ck_lead_avaliacao_status"),
        sa.CheckConstraint("score_saude_cadastral IS NULL OR (score_saude_cadastral >= 0 "
                           "AND score_saude_cadastral <= 100)", name="ck_lead_avaliacao_score"),
        sa.CheckConstraint('"vazamento_total_r$" IS NULL OR "vazamento_total_r$" >= 0',
                           name="ck_lead_avaliacao_vazamento"),
        sa.CheckConstraint("length(trim(nome_contato)) > 0 AND length(trim(nome_loja)) > 0",
                           name="ck_lead_avaliacao_nomes"),
        sa.CheckConstraint("email LIKE '%_@_%'", name="ck_lead_avaliacao_email"),
        sa.CheckConstraint("whatsapp IS NULL OR length(whatsapp) BETWEEN 10 AND 20", name="ck_lead_avaliacao_whatsapp"),
        sa.ForeignKeyConstraint(["plano_recomendado_codigo"], ["plano_nivel.codigo"],
                                name="fk_lead_avaliacao_plano"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lead_avaliacao_plano", "lead_avaliacao", ["plano_recomendado_codigo"], unique=False)
    op.create_index("ix_lead_avaliacao_status", "lead_avaliacao", ["status"], unique=False)
    op.create_index("ix_lead_avaliacao_email", "lead_avaliacao", ["email"], unique=False)
    op.create_table(
        "brainstorming_dados_pdv",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("ano_exercicio", sa.Integer(), nullable=False),
        sa.Column("codigo_produto", sa.String(length=60), nullable=True),
        sa.Column("descricao_produto", sa.String(length=200), nullable=False),
        sa.Column("quantidade", sa.Numeric(14, 3), nullable=False),
        sa.Column("valor_venda_bruto", sa.Numeric(14, 2), nullable=False),
        sa.Column("custo_medio_registrado", sa.Numeric(14, 4), nullable=True),
        sa.Column("flag_venda_sem_custo", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("flag_venda_abaixo_custo", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("flag_omissao_erp", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("vazamento_estimado_r$", sa.Numeric(12, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("criado_em", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"),
                  nullable=False),
        sa.CheckConstraint("ano_exercicio BETWEEN 2000 AND 2100", name="ck_brainstorming_dados_pdv_ano"),
        sa.CheckConstraint("length(trim(descricao_produto)) > 0", name="ck_brainstorming_dados_pdv_descricao"),
        sa.CheckConstraint("custo_medio_registrado IS NULL OR custo_medio_registrado >= 0",
                           name="ck_brainstorming_dados_pdv_custo"),
        sa.CheckConstraint('"vazamento_estimado_r$" >= 0', name="ck_brainstorming_dados_pdv_vazamento"),
        sa.ForeignKeyConstraint(["lead_id"], ["lead_avaliacao.id"], name="fk_brainstorming_dados_pdv_lead",
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_brainstorming_dados_pdv_lead", "brainstorming_dados_pdv", ["lead_id"], unique=False)
    op.create_index("ix_brainstorming_dados_pdv_criado_em", "brainstorming_dados_pdv", ["criado_em"],
                    unique=False)

    if op.get_bind().dialect.name == "postgresql":
        # Os UUIDs também nascem no banco: um INSERT feito fora da aplicação não fica sem chave.
        for tabela in _TABELAS:
            op.execute(f"ALTER TABLE {tabela} ALTER COLUMN id SET DEFAULT gen_random_uuid()")
            op.execute(f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY")
        op.execute(_POLITICAS_POSTGRESQL)
        op.execute(_FUNCAO_EXPURGO)


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma escrita em andamento não aparece na contagem e seria apagada pelo DROP: espera por ela,
        # na ordem em que a aplicação grava (lead e depois dados).
        op.execute("LOCK TABLE lead_avaliacao, brainstorming_dados_pdv IN ACCESS EXCLUSIVE MODE")
    leads = conexao.execute(sa.text("SELECT COUNT(*) FROM lead_avaliacao")).scalar()
    dados = conexao.execute(sa.text("SELECT COUNT(*) FROM brainstorming_dados_pdv")).scalar()
    if leads or dados:
        raise RuntimeError(
            "Downgrade da Avaliação de Cenário recusado: há leads ou dados de PDV gravados. Faça backup e "
            "decida o que fazer com esses dados pessoais antes de remover as estruturas."
        )
    if conexao.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS expurgar_brainstorming_pdv(integer)")
    op.drop_index("ix_brainstorming_dados_pdv_criado_em", table_name="brainstorming_dados_pdv")
    op.drop_index("ix_brainstorming_dados_pdv_lead", table_name="brainstorming_dados_pdv")
    op.drop_table("brainstorming_dados_pdv")
    op.drop_index("ix_lead_avaliacao_email", table_name="lead_avaliacao")
    op.drop_index("ix_lead_avaliacao_status", table_name="lead_avaliacao")
    op.drop_index("ix_lead_avaliacao_plano", table_name="lead_avaliacao")
    op.drop_table("lead_avaliacao")
