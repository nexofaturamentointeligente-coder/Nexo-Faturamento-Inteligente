"""Aceite dos Termos de Uso e da Política de Privacidade pelo lead da landing (microsserviço do Raio-X).

Revision ID: c8e2a4f6b0d1
Revises: f2b6d8a4c1e9

Decisão do dono em 16/11/2026 (redesign da landing): o visitante lê na landing as versões vigentes dos dois
documentos (documento_legal), e o microsserviço do Raio-X (microservico_raiox/) grava, na mesma transação do
lead, uma linha por documento aceito: a versão exata, o instante e o IP do visitante. É a prova do aceite do lead
(Marco Civil), como aceite_documento é a do cliente do portal; aquela tabela não serve ao lead, porque
id_usuario é obrigatório nela.

Expand com uma tabela nova; nenhuma tabela existente muda (regras G.1 e G.4 de
Docs/REESTRUTURACAO_COMERCIAL/00_02_PROTECAO_BANCO_MIGRACOES_ETAPA_0_2_PR22.md):
  - aceite_documento_lead: id_aceite (chave); id_lead (FK lead_avaliacao.id, ON DELETE CASCADE: o aceite sai
    com o lead, como o do cliente sai com o usuário); id_documento (FK documento_legal.id_documento, sem cascata:
    a versão aceita não pode ser apagada); aceito_em (TIMESTAMPTZ, o now() do banco, o mesmo de
    lead_avaliacao.termos_aceitos_em); ip (VARCHAR(45), anulável: IP ausente ou inválido fica nulo).
    UNIQUE(id_lead, id_documento) e índice em id_documento, como em aceite_documento.

`models` não é importado; models.AceiteDocumentoLead espelha esta revisão (o compare_metadata dos testes confere).
A aplicação principal só lê a tabela, com falha aberta (o bloco "Aceite dos termos" na página do lead): funciona
com ou sem esta revisão. O microsserviço, não: sem a tabela, o envio responde 503 e nada é gravado. Por isso esta
revisão vai ao banco ANTES do deploy do microsserviço que grava o aceite (microservico_raiox/README.md).

No PostgreSQL a tabela nasce com RLS ligado, como as da f4c2a8e1d9b6. Onde existirem os papéis da Data API do
Supabase (anon, authenticated), os privilégios deles na tabela e na sequência da chave são revogados e a política
restritiva deny_api_access lhes nega tudo; postgres e service_role, onde existirem, recebem a política
acesso_servidor. Num PostgreSQL sem esses papéis (testes, restauração isolada) as políticas não são criadas: a
aplicação entra como dona da tabela e o RLS não a afeta.

O downgrade recusa apagar a prova do aceite: só remove a tabela vazia.
"""

from alembic import op
import sqlalchemy as sa


revision = "c8e2a4f6b0d1"
down_revision = "f2b6d8a4c1e9"
branch_labels = None
depends_on = None

_TABELA = "aceite_documento_lead"

# Sem "%" no texto: a prévia offline (flask db upgrade --sql) sai igual ao que roda no banco.
_POLITICAS_POSTGRESQL = """
DO $$
DECLARE
    tabela text := 'aceite_documento_lead';
    sequencia text;
    papeis_api text;
    papeis_servidor text;
BEGIN
    SELECT string_agg(quote_ident(rolname), ', ' ORDER BY rolname) INTO papeis_api
      FROM pg_roles WHERE rolname IN ('anon', 'authenticated');
    SELECT string_agg(quote_ident(rolname), ', ' ORDER BY rolname) INTO papeis_servidor
      FROM pg_roles WHERE rolname IN ('postgres', 'service_role');
    -- A sequência do SERIAL: o Supabase também dá a anon e authenticated as sequências novas.
    sequencia := pg_get_serial_sequence(quote_ident(tabela), 'id_aceite');
    IF papeis_api IS NOT NULL THEN
        EXECUTE 'REVOKE ALL ON TABLE ' || quote_ident(tabela) || ' FROM ' || papeis_api;
        IF sequencia IS NOT NULL THEN
            EXECUTE 'REVOKE ALL ON SEQUENCE ' || sequencia || ' FROM ' || papeis_api;
        END IF;
        EXECUTE 'CREATE POLICY deny_api_access ON ' || quote_ident(tabela)
             || ' AS RESTRICTIVE FOR ALL TO ' || papeis_api || ' USING (false) WITH CHECK (false)';
    END IF;
    IF papeis_servidor IS NOT NULL THEN
        EXECUTE 'CREATE POLICY acesso_servidor ON ' || quote_ident(tabela)
             || ' AS PERMISSIVE FOR ALL TO ' || papeis_servidor || ' USING (true) WITH CHECK (true)';
    END IF;
END
$$;
"""


def upgrade():
    op.create_table(
        _TABELA,
        sa.Column("id_aceite", sa.Integer(), nullable=False),
        sa.Column("id_lead", sa.Uuid(), nullable=False),
        sa.Column("id_documento", sa.Integer(), nullable=False),
        sa.Column("aceito_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.ForeignKeyConstraint(["id_lead"], ["lead_avaliacao.id"], name="fk_aceite_documento_lead_lead",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["id_documento"], ["documento_legal.id_documento"],
                                name="fk_aceite_documento_lead_documento"),
        sa.PrimaryKeyConstraint("id_aceite"),
        sa.UniqueConstraint("id_lead", "id_documento", name="uq_aceite_lead_documento"),
    )
    op.create_index("ix_aceite_documento_lead", _TABELA, ["id_documento"], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {_TABELA} ENABLE ROW LEVEL SECURITY")
        op.execute(_POLITICAS_POSTGRESQL)


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma gravação em andamento não aparece na contagem e seria apagada pelo DROP: espera por ela. usuario
        # primeiro, na ordem das requisições do portal (como nas outras revisões), e depois na ordem em que o
        # microsserviço trava (documento_legal, o lead e o aceite). Com tráfego, um deadlock aborta e basta repetir.
        op.execute(f"LOCK TABLE usuario, documento_legal, lead_avaliacao, {_TABELA} IN ACCESS EXCLUSIVE MODE")
    if conexao.execute(sa.text(f"SELECT COUNT(*) FROM {_TABELA}")).scalar():
        raise RuntimeError(
            "Downgrade do aceite dos termos do lead recusado: há aceites gravados em aceite_documento_lead (a prova "
            "de que o visitante leu e aceitou os documentos, com o instante e o IP). Faça backup e decida o que "
            "fazer com esses dados antes de remover a tabela."
        )
    op.drop_index("ix_aceite_documento_lead", table_name=_TABELA)
    op.drop_table(_TABELA)
