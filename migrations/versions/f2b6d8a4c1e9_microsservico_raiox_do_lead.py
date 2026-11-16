"""Avaliação de Cenário: o lead captado pelo microsserviço do Raio-X (microservico_raiox/).

Revision ID: f2b6d8a4c1e9
Revises: e7a1c3f5b9d2

Decisão do dono em 15/11/2026: o microsserviço da landing grava o lead na tabela de leads atual, com o que o
visitante escolheu na Etapa 3 do pré-cadastro, a situação dos relatórios depois da triagem de arquivos, onde
eles ficaram no Storage do Supabase e o aceite dos termos. Expand em lead_avaliacao, como a e7a1c3f5b9d2: só
ADD COLUMN, cada uma com o próprio CHECK nomeado quando há regra, anuláveis e sem padrão (os leads do consultor
e os de antes ficam com as cinco nulas); nenhuma linha existente muda.

  - interesse (VARCHAR(20)): "raio_x" (1 na landing: quer o Raio-X, com relatórios), "ajuda_relatorios"
    (2: quer ajuda com os relatórios) ou "so_contato" (3: só o contato, sem Raio-X).
  - status_arquivo (VARCHAR(20)): "sem_arquivo", "aprovado", "parcial" ou "recusado", pela triagem de
    arquivos do microsserviço.
  - arquivos_storage (TEXT): JSON com o caminho de cada relatório aprovado no bucket privado leads-raio-x e
    o que o identifica (nome exibido, tamanho, formato e SHA-256), com CHECK de tamanho.
  - termos_aceitos_em (TIMESTAMPTZ) e termos_versao (VARCHAR(40)): o instante e a versão dos termos que o
    visitante aceitou na landing. Ficam à parte de consentimento_em e versao_autorizacao, que são da
    autorização de uso dos relatórios e que o reenvio do consultor troca: o registro do aceite não se perde.

O e-mail e o nome da loja continuam obrigatórios: o microsserviço os exige no recebimento. O RLS e as
políticas da f4c2a8e1d9b6 valem para a tabela inteira: as colunas novas já nascem protegidas. No modelo, as
cinco ficam só na tabela, fora do mapeamento do ORM: a aplicação não as lê nem as grava e funciona com ou sem
esta revisão. ADD/DROP COLUMN direto, nunca o modo batch (no SQLite recriaria a tabela e apagaria a staging em
cascata). O downgrade trava usuario e as duas tabelas no PostgreSQL, na ordem das requisições, e recusa
enquanto houver lead com algum dos campos novos preenchido.
"""

from alembic import op
import sqlalchemy as sa


revision = "f2b6d8a4c1e9"
down_revision = "e7a1c3f5b9d2"
branch_labels = None
depends_on = None

# Iguais às de models.py (o teste de metadados compara as colunas; os CHECKs são conferidos pelos testes).
_COLUNAS = (
    ("interesse", sa.String(20),
     ("interesse IS NULL OR interesse IN ('raio_x', 'ajuda_relatorios', 'so_contato')",
      "ck_lead_avaliacao_interesse")),
    ("status_arquivo", sa.String(20),
     ("status_arquivo IS NULL OR status_arquivo IN ('sem_arquivo', 'aprovado', 'parcial', 'recusado')",
      "ck_lead_avaliacao_status_arquivo")),
    ("arquivos_storage", sa.Text(),
     ("arquivos_storage IS NULL OR length(arquivos_storage) <= 20000", "ck_lead_avaliacao_arquivos_storage")),
    ("termos_aceitos_em", sa.DateTime(timezone=True), None),
    ("termos_versao", sa.String(40),
     ("termos_versao IS NULL OR length(termos_versao) <= 40", "ck_lead_avaliacao_termos_versao")),
)


def upgrade():
    for nome, tipo, regra in _COLUNAS:
        # CHECK na própria coluna: ADD COLUMN no SQLite e no PostgreSQL sem reconstruir a tabela.
        restricoes = [sa.CheckConstraint(regra[0], name=regra[1])] if regra else []
        op.add_column("lead_avaliacao", sa.Column(nome, tipo, *restricoes, nullable=True))


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # usuario primeiro, na ordem das requisições (como a e7a1c3f5b9d2).
        op.execute("LOCK TABLE usuario, lead_avaliacao, brainstorming_dados_pdv IN ACCESS EXCLUSIVE MODE")
    leads = conexao.execute(sa.text(
        "SELECT COUNT(*) FROM lead_avaliacao WHERE interesse IS NOT NULL OR status_arquivo IS NOT NULL "
        "OR arquivos_storage IS NOT NULL OR termos_aceitos_em IS NOT NULL OR termos_versao IS NOT NULL")).scalar()
    if leads:
        raise RuntimeError(
            "Downgrade do microsserviço do Raio-X recusado: há leads com interesse, situação dos arquivos, "
            "caminhos no Storage ou aceite dos termos. Faça backup e, se decidir seguir, apague esses leads (e os "
            "arquivos deles no bucket leads-raio-x) antes de remover as colunas."
        )
    for nome, *_ in reversed(_COLUNAS):
        if conexao.dialect.name == "sqlite":
            # DROP COLUMN direto (o CHECK é da própria coluna): o modo batch apagaria a staging em cascata.
            op.execute(f"ALTER TABLE lead_avaliacao DROP COLUMN {nome}")
        else:
            op.drop_column("lead_avaliacao", nome)
