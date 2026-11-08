"""Avaliação de Cenário: staging ampliada, triagem versionada e registro do consultor.

Revision ID: 5e1b9c3a7d20
Revises: f4c2a8e1d9b6

Expand nas duas tabelas da revisão f4c2a8e1d9b6, sem tocar nas demais (regras G.1 de
Docs/REESTRUTURACAO_COMERCIAL/00_02_PROTECAO_BANCO_MIGRACOES_ETAPA_0_2_PR22.md). Nenhum código
gravava nessas tabelas antes desta revisão: as colunas NOT NULL novas recebem padrão no
servidor (ou exigem tabela vazia, no caso de chave_produto), e nenhuma linha é reescrita.

  - brainstorming_dados_pdv ganha o que permite refazer as regras do Raio-X sem os arquivos, que
    não são guardados: a chave estável do produto entre os anos (chave_produto), as somas do
    produto no ano (quantidade e faturamento das vendas com custo, custo total, perda abaixo do
    custo, vazamento de preço e recuperável), as linhas e os meses com venda, as marcas de variação
    de preço, conflito de cadastro e estoque negativo, o bloco da triagem (urgencia, atencao ou
    eletiva) e a versão das regras. A unicidade (lead, ano, produto) impede que um reprocessamento
    duplique linhas.
  - lead_avaliacao ganha a versão das regras e da triagem, o período e o número de relatórios, os
    metadados do envio (só contagens, sem nome de produto), as contagens de itens com venda sem
    custo e abaixo do custo, o consultor que registrou o lead (registrado_por, que fica nulo se o
    usuário for excluído) e o instante em que ele confirmou a autorização do lead.

O RLS e as políticas da f4c2a8e1d9b6 valem para a tabela inteira: as colunas novas já nascem
protegidas. O downgrade recusa apagar dados, como o da f4c2a8e1d9b6.
"""

from alembic import op
import sqlalchemy as sa


revision = "5e1b9c3a7d20"
down_revision = "f4c2a8e1d9b6"
branch_labels = None
depends_on = None

_SOMAS = (
    ("quantidade_com_custo", sa.Numeric(14, 3)),
    ("faturamento_com_custo", sa.Numeric(14, 2)),
    ("custo_total", sa.Numeric(14, 2)),
    ("perda", sa.Numeric(14, 2)),
    ("vazamento_preco", sa.Numeric(14, 2)),
    ("recuperavel", sa.Numeric(14, 2)),
)
_CONTAGENS = ("linhas", "meses")
_MARCAS = ("variacao", "conflito_cadastral", "estoque_negativo")
_COLUNAS_LEAD = (
    ("versao_regras", sa.String(length=40)),
    ("versao_triagem", sa.String(length=40)),
    ("periodo_inicio", sa.Date()),
    ("periodo_fim", sa.Date()),
    ("arquivos", sa.Integer()),
    ("meta_json", sa.Text()),
    ("itens_venda_sem_custo", sa.Integer()),
    ("itens_venda_abaixo_custo", sa.Integer()),
    ("registrado_por", sa.Integer()),
    ("consentimento_em", sa.DateTime(timezone=True)),
)


def upgrade():
    with op.batch_alter_table("brainstorming_dados_pdv", schema=None) as batch_op:
        batch_op.add_column(sa.Column("chave_produto", sa.String(length=16), nullable=False))
        for nome, tipo in _SOMAS:
            batch_op.add_column(sa.Column(nome, tipo, server_default=sa.text("0"), nullable=False))
        for nome in _CONTAGENS:
            batch_op.add_column(sa.Column(nome, sa.Integer(), server_default=sa.text("0"), nullable=False))
        for nome in _MARCAS:
            batch_op.add_column(sa.Column(nome, sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column("bloco_triagem", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("versao_regras", sa.String(length=40), nullable=True))
        batch_op.create_check_constraint("ck_brainstorming_dados_pdv_chave", "length(chave_produto) > 0")
        batch_op.create_check_constraint(
            "ck_brainstorming_dados_pdv_somas",
            "quantidade_com_custo >= 0 AND faturamento_com_custo >= 0 AND custo_total >= 0 AND perda >= 0 "
            "AND vazamento_preco >= 0 AND recuperavel >= 0 AND linhas >= 0 AND meses >= 0",
        )
        batch_op.create_check_constraint(
            "ck_brainstorming_dados_pdv_bloco",
            "bloco_triagem IS NULL OR bloco_triagem IN ('urgencia', 'atencao', 'eletiva')",
        )
        batch_op.create_unique_constraint("uq_brainstorming_dados_pdv_lead_ano_produto",
                                          ["lead_id", "ano_exercicio", "chave_produto"])

    with op.batch_alter_table("lead_avaliacao", schema=None) as batch_op:
        for nome, tipo in _COLUNAS_LEAD:
            batch_op.add_column(sa.Column(nome, tipo, nullable=True))
        batch_op.create_foreign_key("fk_lead_avaliacao_registrado_por", "usuario", ["registrado_por"],
                                    ["id_usuario"], ondelete="SET NULL")
        batch_op.create_check_constraint("ck_lead_avaliacao_arquivos", "arquivos IS NULL OR arquivos >= 1")
        batch_op.create_check_constraint(
            "ck_lead_avaliacao_itens",
            "(itens_venda_sem_custo IS NULL OR itens_venda_sem_custo >= 0) "
            "AND (itens_venda_abaixo_custo IS NULL OR itens_venda_abaixo_custo >= 0)",
        )
        batch_op.create_check_constraint(
            "ck_lead_avaliacao_periodo",
            "periodo_inicio IS NULL OR periodo_fim IS NULL OR periodo_inicio <= periodo_fim",
        )
    op.create_index("ix_lead_avaliacao_registrado_por", "lead_avaliacao", ["registrado_por"], unique=False)


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma escrita em andamento não aparece na contagem: espera por ela, na ordem da aplicação.
        op.execute("LOCK TABLE lead_avaliacao, brainstorming_dados_pdv IN ACCESS EXCLUSIVE MODE")
    leads = conexao.execute(sa.text("SELECT COUNT(*) FROM lead_avaliacao")).scalar()
    dados = conexao.execute(sa.text("SELECT COUNT(*) FROM brainstorming_dados_pdv")).scalar()
    if leads or dados:
        raise RuntimeError(
            "Downgrade da Avaliação de Cenário recusado: há leads ou dados de PDV gravados. Faça backup e "
            "decida o que fazer com esses dados pessoais antes de remover as estruturas."
        )
    op.drop_index("ix_lead_avaliacao_registrado_por", table_name="lead_avaliacao")
    with op.batch_alter_table("lead_avaliacao", schema=None) as batch_op:
        for nome in ("ck_lead_avaliacao_periodo", "ck_lead_avaliacao_itens", "ck_lead_avaliacao_arquivos"):
            batch_op.drop_constraint(nome, type_="check")
        batch_op.drop_constraint("fk_lead_avaliacao_registrado_por", type_="foreignkey")
        for nome, _ in reversed(_COLUNAS_LEAD):
            batch_op.drop_column(nome)
    with op.batch_alter_table("brainstorming_dados_pdv", schema=None) as batch_op:
        batch_op.drop_constraint("uq_brainstorming_dados_pdv_lead_ano_produto", type_="unique")
        for nome in ("ck_brainstorming_dados_pdv_bloco", "ck_brainstorming_dados_pdv_somas",
                     "ck_brainstorming_dados_pdv_chave"):
            batch_op.drop_constraint(nome, type_="check")
        for nome in ("versao_regras", "bloco_triagem", *reversed(_MARCAS), *reversed(_CONTAGENS),
                     *(nome for nome, _ in reversed(_SOMAS)), "chave_produto"):
            batch_op.drop_column(nome)
