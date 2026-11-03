"""Trava do total do PDV: conferência entre o valor informado e o faturamento calculado.

Revision ID: e2f7a94c1d35
Revises: c7d41e9a2b60

Somente uma tabela nova (expand): nenhuma coluna é adicionada a analise,
upload_relatorio ou indicador_analise, nenhum CHECK existente muda e o código
funciona antes desta migração (ver validacao_pdv.py). Nenhuma linha é criada:
análise anterior fica sem conferência registrada.

A linha sai com a análise por ON DELETE CASCADE, sem relationship em Analise: as
exclusões de análise e de empresa não consultam esta tabela, exista ela ou não.

No PostgreSQL a tabela nasce com RLS ligado e sem política: a aplicação entra como
dona da tabela e não é afetada, e a API pública que o Supabase monta sobre o schema
public não lê nem grava nela.

O downgrade recusa-se a apagar dados: só remove a estrutura com a tabela vazia.
"""

from alembic import op
import sqlalchemy as sa


revision = "e2f7a94c1d35"
down_revision = "c7d41e9a2b60"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "validacao_pdv",
        sa.Column("id_analise", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("valor_pdv_informado", sa.Numeric(14, 2), nullable=False),
        sa.Column("faturamento_bruto_calculado", sa.Numeric(), nullable=True),
        sa.Column("diferenca", sa.Numeric(), nullable=True),
        sa.Column("valor_devolucoes", sa.Numeric(), nullable=True),
        sa.Column("valor_cancelado", sa.Numeric(), nullable=True),
        sa.Column("status_validacao", sa.String(length=20), nullable=False),
        sa.Column("id_usuario_informante", sa.Integer(), nullable=True),
        sa.Column("informado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validado_em", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("valor_pdv_informado >= 0", name="ck_validacao_pdv_valor"),
        sa.CheckConstraint(
            "status_validacao IN ('PENDENTE', 'CONCLUIDO', 'DIVERGENTE_PDV')",
            name="ck_validacao_pdv_status",
        ),
        sa.CheckConstraint(
            "(status_validacao = 'PENDENTE' AND faturamento_bruto_calculado IS NULL "
            "AND diferenca IS NULL AND validado_em IS NULL) OR "
            "(status_validacao <> 'PENDENTE' AND faturamento_bruto_calculado IS NOT NULL "
            "AND diferenca IS NOT NULL AND diferenca >= 0 AND validado_em IS NOT NULL)",
            name="ck_validacao_pdv_resultado",
        ),
        sa.ForeignKeyConstraint(
            ["id_analise"], ["analise.id_analise"], name="fk_validacao_pdv_analise", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["id_usuario_informante"], ["usuario.id_usuario"], name="fk_validacao_pdv_informante",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id_analise"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE validacao_pdv ENABLE ROW LEVEL SECURITY")


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma escrita em andamento não aparece na contagem e seria apagada pelo DROP: espera por ela.
        # Na ordem de quem começa uma requisição (usuario, analise, validacao_pdv). Depois de um
        # commit a ordem pode inverter: rode com o serviço parado, ou repita se abortar por deadlock.
        op.execute("LOCK TABLE usuario, analise, validacao_pdv IN ACCESS EXCLUSIVE MODE")
    if conexao.execute(sa.text("SELECT COUNT(*) FROM validacao_pdv")).scalar():
        raise RuntimeError(
            "Downgrade da trava do PDV recusado: há dados em validacao_pdv. "
            "Faça backup e decida o que fazer com eles antes de remover a estrutura."
        )
    op.drop_table("validacao_pdv")
