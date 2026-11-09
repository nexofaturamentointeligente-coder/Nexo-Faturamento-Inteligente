"""Avaliação de Cenário, Etapa 2: cobertura das verificações, cadastro, ajustes e limiares da triagem.

Revision ID: 7c4e2a9b5d31
Revises: 6a8f2d4c0e13

Expand nas duas tabelas da Avaliação de Cenário, sem tocar nas demais (regras G.1 de
Docs/REESTRUTURACAO_COMERCIAL/00_02_PROTECAO_BANCO_MIGRACOES_ETAPA_0_2_PR22.md): só ADD COLUMN com
padrão constante (no PostgreSQL 11+ não reescreve a tabela) e CHECKs; nenhuma linha é reescrita.

  - brainstorming_dados_pdv ganha o que a triagem da Etapa 2 (lead-2026-11.3) e a matriz por
    categoria precisam e os arquivos, que não são guardados, não refazem: a venda nos relatórios
    com a coluna de custo, os relatórios com custo, com margem negativa e completos, se o mais recente
    com custo foi negativo, a quantidade nos mesmos meses do último ano, o último estoque informado, a
    categoria do produto (texto do relatório, que expira com a linha em 30 dias), se faltaram NCM ou
    categoria e os ajustes de bonificação e devolução do produto. As linhas gravadas antes recebem o
    padrão do servidor; a versão das regras (versao_regras) as separa, e o diagnóstico não as usa.
  - lead_avaliacao ganha a contagem de produtos com omissão no ERP e os limiares usados na triagem
    (só números). Nulas nos leads de antes desta revisão.

O RLS e as políticas da f4c2a8e1d9b6 valem para a tabela inteira: as colunas novas já nascem
protegidas. O downgrade recusa enquanto houver linha gravada pelas regras lead-2026-11.3 ou lead com
as colunas novas preenchidas. O agendamento do expurgo no pg_cron não depende mais de downgrade: o
comando `flask leads agendar-expurgo` repete o da revisão 6a8f2d4c0e13.
"""

from alembic import op
import sqlalchemy as sa


revision = "7c4e2a9b5d31"
down_revision = "6a8f2d4c0e13"
branch_labels = None
depends_on = None

_DINHEIRO = ("faturamento_em_relatorio_com_custo", "custo_bonificacoes", "custo_devolucoes", "devolucoes_com_custo")
_CONTAGENS = ("meses_com_custo", "meses_margem_negativa", "meses_completos", "relatorios_desde_primeira_venda")
_MARCAS = ("ultimo_mes_negativo", "ncm_ausente", "categoria_ausente")
_CHECKS_STAGING = (
    ("ck_brainstorming_dados_pdv_etapa2_somas",
     "faturamento_em_relatorio_com_custo >= 0 AND quantidade_mesmos_meses >= 0 AND custo_bonificacoes >= 0 "
     "AND custo_devolucoes >= 0 AND devolucoes_com_custo >= 0"),
    ("ck_brainstorming_dados_pdv_etapa2_meses",
     "meses_margem_negativa >= 0 AND meses_margem_negativa <= meses_com_custo AND meses_com_custo <= meses "
     "AND meses_completos >= 0 AND meses_completos <= meses AND relatorios_desde_primeira_venda >= 0"),
    ("ck_brainstorming_dados_pdv_categoria", "categoria_produto IS NULL OR length(trim(categoria_produto)) > 0"),
)


def upgrade():
    with op.batch_alter_table("brainstorming_dados_pdv", schema=None) as batch_op:
        for nome in _DINHEIRO:
            batch_op.add_column(sa.Column(nome, sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False))
        batch_op.add_column(sa.Column("quantidade_mesmos_meses", sa.Numeric(14, 3), server_default=sa.text("0"),
                                      nullable=False))
        for nome in _CONTAGENS:
            batch_op.add_column(sa.Column(nome, sa.Integer(), server_default=sa.text("0"), nullable=False))
        for nome in _MARCAS:
            batch_op.add_column(sa.Column(nome, sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column("estoque_final", sa.Numeric(14, 3), nullable=True))
        batch_op.add_column(sa.Column("categoria_produto", sa.String(length=80), nullable=True))
        for nome, condicao in _CHECKS_STAGING:
            batch_op.create_check_constraint(nome, condicao)

    # lead_avaliacao é a tabela-mãe da staging (ON DELETE CASCADE): no SQLite, recriá-la (modo batch, que a
    # CHECK exigiria) apagaria em cascata as linhas de PDV. ADD COLUMN direto não recria; a CHECK fica só
    # no PostgreSQL (o SQLite de testes e de desenvolvimento a recebe quando a tabela nasce dos modelos).
    op.add_column("lead_avaliacao", sa.Column("itens_omissao_erp", sa.Integer(), nullable=True))
    op.add_column("lead_avaliacao", sa.Column("limiares_triagem_json", sa.Text(), nullable=True))
    if op.get_bind().dialect.name != "sqlite":
        op.create_check_constraint("ck_lead_avaliacao_omissao", "lead_avaliacao",
                                   "itens_omissao_erp IS NULL OR itens_omissao_erp >= 0")


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma escrita em andamento não aparece na contagem: espera por ela. usuario vem primeiro, na ordem
        # das requisições: o downgrade que segue na mesma transação (5e1b9c3a7d20) remove a FK de
        # registrado_por, que trava usuario; travá-lo depois das tabelas de lead poderia dar deadlock.
        op.execute("LOCK TABLE usuario, lead_avaliacao, brainstorming_dados_pdv IN ACCESS EXCLUSIVE MODE")
    linhas = conexao.execute(sa.text(
        "SELECT COUNT(*) FROM brainstorming_dados_pdv WHERE versao_regras = 'lead-2026-11.3'")).scalar()
    leads = conexao.execute(sa.text(
        "SELECT COUNT(*) FROM lead_avaliacao WHERE itens_omissao_erp IS NOT NULL "
        "OR limiares_triagem_json IS NOT NULL")).scalar()
    if linhas or leads:
        # O expurgo de 30 dias apaga as linhas, mas não as colunas do lead: esperar por ele não destrava.
        raise RuntimeError(
            "Downgrade da Etapa 2 da Avaliação de Cenário recusado: há linhas de PDV das regras lead-2026-11.3 "
            "ou leads com itens_omissao_erp ou limiares_triagem_json preenchidos. Faça backup e, se decidir "
            "seguir, apague esses leads ou anule as duas colunas deles antes de remover as colunas."
        )
    if conexao.dialect.name == "sqlite":
        # DROP COLUMN direto (SQLite 3.35+), sem recriar a tabela-mãe (ver upgrade).
        op.execute("ALTER TABLE lead_avaliacao DROP COLUMN limiares_triagem_json")
        op.execute("ALTER TABLE lead_avaliacao DROP COLUMN itens_omissao_erp")
    else:
        op.drop_constraint("ck_lead_avaliacao_omissao", "lead_avaliacao", type_="check")
        op.drop_column("lead_avaliacao", "limiares_triagem_json")
        op.drop_column("lead_avaliacao", "itens_omissao_erp")
    with op.batch_alter_table("brainstorming_dados_pdv", schema=None) as batch_op:
        for nome, _ in reversed(_CHECKS_STAGING):
            batch_op.drop_constraint(nome, type_="check")
        for nome in ("categoria_produto", "estoque_final", *reversed(_MARCAS), *reversed(_CONTAGENS),
                     "quantidade_mesmos_meses", *reversed(_DINHEIRO)):
            batch_op.drop_column(nome)
