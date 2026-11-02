"""Configurações da conta: preferências de aviso, documentos legais, aceites e cópia de chamados.

Revision ID: c7d41e9a2b60
Revises: b6935e8de9d4

Somente tabelas novas (expand): nenhuma coluna é adicionada a usuario, empresa
ou chamado_suporte, e o código funciona antes desta migração (as funções que
dependem das tabelas ficam indisponíveis; ver preferencias_usuario.py). Nenhum
documento, aceite ou preferência é criado: o texto jurídico vem do administrador
e um aceite só existe quando o próprio usuário o registra.

No PostgreSQL as tabelas nascem com RLS ligado e sem política: a aplicação entra
como dona das tabelas e não é afetada, e a API pública que o Supabase monta sobre
o schema public não lê nem grava nelas.

O downgrade recusa-se a apagar dados: só remove as estruturas quando todas as
tabelas novas estão vazias.
"""

from alembic import op
import sqlalchemy as sa


revision = "c7d41e9a2b60"
down_revision = "b6935e8de9d4"
branch_labels = None
depends_on = None

_TABELAS = ("preferencia_usuario", "documento_legal", "aceite_documento", "configuracao_sistema")


def upgrade():
    op.create_table(
        "preferencia_usuario",
        sa.Column("id_usuario", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("lembrete_renovacao", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("alerta_chamado", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("data_atualizacao", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["id_usuario"], ["usuario.id_usuario"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id_usuario"),
    )
    op.create_table(
        "documento_legal",
        sa.Column("id_documento", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.String(length=30), nullable=False),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("conteudo", sa.Text(), nullable=False),
        sa.Column("publicado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id_usuario_admin", sa.Integer(), nullable=False),
        sa.CheckConstraint("tipo IN ('TERMOS_USO', 'POLITICA_PRIVACIDADE')", name="ck_documento_tipo"),
        sa.CheckConstraint("versao >= 1", name="ck_documento_versao"),
        sa.CheckConstraint("length(trim(conteudo)) > 0", name="ck_documento_conteudo"),
        sa.ForeignKeyConstraint(["id_usuario_admin"], ["usuario.id_usuario"]),
        sa.PrimaryKeyConstraint("id_documento"),
        sa.UniqueConstraint("tipo", "versao", name="uq_documento_tipo_versao"),
    )
    op.create_table(
        "aceite_documento",
        sa.Column("id_aceite", sa.Integer(), nullable=False),
        sa.Column("id_usuario", sa.Integer(), nullable=False),
        sa.Column("id_documento", sa.Integer(), nullable=False),
        sa.Column("aceito_em", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["id_documento"], ["documento_legal.id_documento"]),
        sa.ForeignKeyConstraint(["id_usuario"], ["usuario.id_usuario"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id_aceite"),
        sa.UniqueConstraint("id_usuario", "id_documento", name="uq_aceite_usuario_documento"),
    )
    op.create_index("ix_aceite_documento", "aceite_documento", ["id_documento"], unique=False)
    op.create_table(
        "configuracao_sistema",
        sa.Column("chave", sa.String(length=60), nullable=False),
        sa.Column("valor", sa.Text(), nullable=True),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id_usuario_admin", sa.Integer(), nullable=False),
        sa.CheckConstraint("chave IN ('suporte_bcc_email')", name="ck_configuracao_chave"),
        sa.ForeignKeyConstraint(["id_usuario_admin"], ["usuario.id_usuario"]),
        sa.PrimaryKeyConstraint("chave"),
    )
    if op.get_bind().dialect.name == "postgresql":
        for tabela in _TABELAS:
            op.execute(f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY")


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma escrita em andamento não aparece na contagem e seria apagada pelo DROP: espera por ela.
        # usuario vem primeiro, na ordem das requisições (e o DROP já o travaria, pela FK): sem deadlock.
        op.execute("LOCK TABLE usuario, " + ", ".join(_TABELAS) + " IN ACCESS EXCLUSIVE MODE")
    com_dados = [
        tabela for tabela in _TABELAS
        if conexao.execute(sa.text(f"SELECT COUNT(*) FROM {tabela}")).scalar()
    ]
    if com_dados:
        raise RuntimeError(
            "Downgrade das Configurações da conta recusado: há dados em "
            + ", ".join(com_dados)
            + ". Faça backup e decida o que fazer com eles antes de remover as estruturas."
        )
    op.drop_table("configuracao_sistema")
    op.drop_index("ix_aceite_documento", table_name="aceite_documento")
    op.drop_table("aceite_documento")
    op.drop_table("documento_legal")
    op.drop_table("preferencia_usuario")
