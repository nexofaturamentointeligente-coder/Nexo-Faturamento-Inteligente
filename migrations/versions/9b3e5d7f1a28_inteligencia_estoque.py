"""Avaliação de Cenário, Etapa 3: data da última venda por produto e o resumo da inteligência de estoque.

Revision ID: 9b3e5d7f1a28
Revises: 7c4e2a9b5d31

Expand nas duas tabelas da Avaliação de Cenário, sem tocar nas demais: só ADD COLUMN anulável sem
padrão (no PostgreSQL não reescreve a tabela) e uma CHECK; nenhuma linha é reescrita.

  - brainstorming_dados_pdv ganha fim_ultimo_relatorio_com_venda (DATE): o fim, cortado no dia do
    envio, do último relatório do ano em que o produto vendeu (regras lead-2026-11.4). É a base dos
    dias sem venda da inteligência de estoque (inteligencia_estoque.py). Nula nas linhas de antes.
  - lead_avaliacao ganha resumo_inteligencia_json (TEXT): só números (capital parado por faixa de
    dias, capital excedente, Urgência a preço médio e ao custo, diferença de preço), sem nome de
    produto nem de categoria, porque fica depois do expurgo das linhas, como os resumos dos blocos.

O RLS e as políticas da f4c2a8e1d9b6 valem para a tabela inteira: as colunas novas já nascem
protegidas. Como na 7c4e2a9b5d31, lead_avaliacao (tabela-mãe da staging, ON DELETE CASCADE) recebe
ADD/DROP COLUMN direto, nunca o modo batch, que no SQLite recriaria a tabela e apagaria as linhas de
PDV em cascata. O downgrade trava usuario e as duas tabelas no PostgreSQL, na ordem das requisições,
e recusa enquanto houver linha das regras lead-2026-11.4 ou lead com o resumo novo preenchido.
"""

from alembic import op
import sqlalchemy as sa


revision = "9b3e5d7f1a28"
down_revision = "7c4e2a9b5d31"
branch_labels = None
depends_on = None

_CHECK_DATA = ("ck_brainstorming_dados_pdv_ultima_venda",
               "fim_ultimo_relatorio_com_venda IS NULL OR fim_ultimo_relatorio_com_venda "
               "BETWEEN '2000-01-01' AND '2100-12-31'")


def upgrade():
    with op.batch_alter_table("brainstorming_dados_pdv", schema=None) as batch_op:
        batch_op.add_column(sa.Column("fim_ultimo_relatorio_com_venda", sa.Date(), nullable=True))
        batch_op.create_check_constraint(*_CHECK_DATA)
    op.add_column("lead_avaliacao", sa.Column("resumo_inteligencia_json", sa.Text(), nullable=True))


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # usuario primeiro, na ordem das requisições: a 5e1b9c3a7d20, mais abaixo, remove a FK de
        # registrado_por, que trava usuario.
        op.execute("LOCK TABLE usuario, lead_avaliacao, brainstorming_dados_pdv IN ACCESS EXCLUSIVE MODE")
    linhas = conexao.execute(sa.text(
        "SELECT COUNT(*) FROM brainstorming_dados_pdv WHERE versao_regras = 'lead-2026-11.4' "
        "OR fim_ultimo_relatorio_com_venda IS NOT NULL")).scalar()
    leads = conexao.execute(sa.text(
        "SELECT COUNT(*) FROM lead_avaliacao WHERE resumo_inteligencia_json IS NOT NULL")).scalar()
    if linhas or leads:
        raise RuntimeError(
            "Downgrade da Etapa 3 da Avaliação de Cenário recusado: há linhas de PDV das regras lead-2026-11.4 "
            "ou leads com resumo_inteligencia_json preenchido. Faça backup e, se decidir seguir, apague esses "
            "leads ou anule a coluna deles antes de remover as colunas."
        )
    if conexao.dialect.name == "sqlite":
        op.execute("ALTER TABLE lead_avaliacao DROP COLUMN resumo_inteligencia_json")
    else:
        op.drop_column("lead_avaliacao", "resumo_inteligencia_json")
    with op.batch_alter_table("brainstorming_dados_pdv", schema=None) as batch_op:
        batch_op.drop_constraint(_CHECK_DATA[0], type_="check")
        batch_op.drop_column("fim_ultimo_relatorio_com_venda")
