"""Avaliação de Cenário: lead captado pelo site (Raio-X público), com origem e o que o visitante informou.

Revision ID: e7a1c3f5b9d2
Revises: d9e4b7a2c6f1

Decisão do dono em 15/11/2026 (Docs/PRE_CADASTRO_LANDING.md): o Raio-X público grava o lead pelo mesmo
caminho do consultor (avaliacao_cenario.registrar), atrás do interruptor AVALIACAO_PUBLICA_ATIVA. Expand em
lead_avaliacao, sem tocar nas demais tabelas: só ADD COLUMN (no PostgreSQL não reescreve a tabela), cada uma
com o próprio CHECK nomeado, como a e4c004a26b01; nenhuma linha existente muda de valor.

  - origem (VARCHAR(20) NOT NULL DEFAULT 'consultor'): "consultor" (POST /admin/leads) ou "site". Os leads de
    antes ficam "consultor", que é de onde eles vieram. registrado_por nulo não servia para isso: ele também
    fica nulo quando o consultor é excluído (ON DELETE SET NULL).
  - plano_interesse e faixa_faturamento (VARCHAR(20), anuláveis): códigos fixos (models.PLANOS_DE_INTERESSE e
    models.FAIXAS_DE_FATURAMENTO). Sem FK nova para plano_nivel (regra G.1.5): CHECK com a lista.
  - dificuldade (VARCHAR(400), anulável): texto livre do visitante, com CHECK de tamanho.
  - versao_autorizacao (VARCHAR(40), anulável): a versão do texto de autorização que o visitante marcou.

O e-mail continua obrigatório (NOT NULL e CHECK de antes): mudar coluna existente fere a regra G.1.5, e o
e-mail é o que localiza o titular num pedido da LGPD. O RLS e as políticas da f4c2a8e1d9b6 valem para a tabela
inteira: as colunas novas já nascem protegidas. Como na c5f8a1d3e7b9, lead_avaliacao (tabela-mãe da staging,
ON DELETE CASCADE) recebe ADD/DROP COLUMN direto, nunca o modo batch, que no SQLite recriaria a tabela e
apagaria as linhas de PDV em cascata. O downgrade trava usuario e as duas tabelas no PostgreSQL, na ordem das
requisições, e recusa enquanto houver lead do site ou com algum dos campos novos preenchido.
"""

from alembic import op
import sqlalchemy as sa


revision = "e7a1c3f5b9d2"
down_revision = "d9e4b7a2c6f1"
branch_labels = None
depends_on = None

# Iguais às de models.py (o teste de metadados compara as colunas; os CHECKs são conferidos pelos testes).
_COLUNAS = (
    ("origem", sa.String(20), "origem IN ('consultor', 'site')", "ck_lead_avaliacao_origem", False),
    ("plano_interesse", sa.String(20),
     "plano_interesse IS NULL OR plano_interesse IN ('BRONZE', 'PRATA', 'OURO', 'CUSTOM')",
     "ck_lead_avaliacao_plano_interesse", True),
    ("faixa_faturamento", sa.String(20),
     "faixa_faturamento IS NULL OR faixa_faturamento IN ('ATE_50K', 'DE_50K_A_200K', 'ACIMA_200K')",
     "ck_lead_avaliacao_faixa_faturamento", True),
    ("dificuldade", sa.String(400), "dificuldade IS NULL OR length(dificuldade) <= 400",
     "ck_lead_avaliacao_dificuldade", True),
    ("versao_autorizacao", sa.String(40), "versao_autorizacao IS NULL OR length(versao_autorizacao) <= 40",
     "ck_lead_avaliacao_versao_autorizacao", True),
)


def upgrade():
    for nome, tipo, regra, nome_do_check, anulavel in _COLUNAS:
        # CHECK na própria coluna: ADD COLUMN no SQLite e no PostgreSQL sem reconstruir a tabela.
        opcoes = {} if anulavel else {"server_default": sa.text("'consultor'")}
        op.add_column("lead_avaliacao", sa.Column(nome, tipo, sa.CheckConstraint(regra, name=nome_do_check),
                                                  nullable=anulavel, **opcoes))


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # usuario primeiro, na ordem das requisições (como a c5f8a1d3e7b9).
        op.execute("LOCK TABLE usuario, lead_avaliacao, brainstorming_dados_pdv IN ACCESS EXCLUSIVE MODE")
    leads = conexao.execute(sa.text(
        "SELECT COUNT(*) FROM lead_avaliacao WHERE origem <> 'consultor' OR plano_interesse IS NOT NULL "
        "OR faixa_faturamento IS NOT NULL OR dificuldade IS NOT NULL OR versao_autorizacao IS NOT NULL")).scalar()
    if leads:
        raise RuntimeError(
            "Downgrade da captação pelo site recusado: há leads do site ou com plano de interesse, faixa de "
            "faturamento, dificuldade ou versão da autorização preenchidos. Faça backup e, se decidir seguir, "
            "apague esses leads antes de remover as colunas."
        )
    for nome, *_ in reversed(_COLUNAS):
        if conexao.dialect.name == "sqlite":
            # DROP COLUMN direto (o CHECK é da própria coluna): o modo batch apagaria a staging em cascata.
            op.execute(f"ALTER TABLE lead_avaliacao DROP COLUMN {nome}")
        else:
            op.drop_column("lead_avaliacao", nome)
