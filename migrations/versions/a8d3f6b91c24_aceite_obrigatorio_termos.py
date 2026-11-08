"""Aceite obrigatório dos Termos de Uso e da Política de Privacidade: IP e rótulo da versão.

Revision ID: a8d3f6b91c24
Revises: e2f7a94c1d35

Expand com colunas, sem tabela nova: aceite_documento.ip_usuario (anulável, porque os
aceites anteriores não têm IP) e documento_legal.versao_maior e versao_menor, o rótulo
exibido (v1.0, v1.1, v2.0). A ordem e a vigência continuam pela sequência `versao`, e as
versões já publicadas viram v<versao>.0. Nenhuma linha é criada.

O código confere essas colunas antes de usar as Configurações (preferencias_usuario.py):
com a c7d41e9a2b60 aplicada e esta não, documentos, preferências e cópia de chamados
ficam indisponíveis com aviso, e o portal segue funcionando, sem a trava de aceite.

O downgrade recusa apagar dados: IP de aceite gravado, ou rótulo de revisão (v1.1) que
a sequência não reconstrói.
"""

from alembic import op
import sqlalchemy as sa


revision = "a8d3f6b91c24"
down_revision = "e2f7a94c1d35"
branch_labels = None
depends_on = None


def upgrade():
    # CHECK na própria coluna: ADD COLUMN sem reconstruir as tabelas nem as FKs que
    # apontam para documento_legal (precedente: e4c004a26b01).
    op.add_column("aceite_documento", sa.Column("ip_usuario", sa.String(length=45), nullable=True))
    op.add_column("documento_legal", sa.Column(
        "versao_maior", sa.Integer(), sa.CheckConstraint("versao_maior >= 1", name="ck_documento_versao_maior"),
        server_default=sa.text("1"), nullable=False,
    ))
    op.add_column("documento_legal", sa.Column(
        "versao_menor", sa.Integer(), sa.CheckConstraint("versao_menor >= 0", name="ck_documento_versao_menor"),
        server_default=sa.text("0"), nullable=False,
    ))
    op.execute("UPDATE documento_legal SET versao_maior = versao")
    op.create_index("uq_documento_tipo_rotulo", "documento_legal", ["tipo", "versao_maior", "versao_menor"],
                    unique=True)


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma escrita em andamento não aparece na contagem e seria apagada pelo DROP: espera por ela.
        # usuario vem primeiro, na ordem das requisições. Com tráfego, um deadlock aborta e basta repetir.
        op.execute("LOCK TABLE usuario, documento_legal, aceite_documento IN ACCESS EXCLUSIVE MODE")
    com_ip = conexao.execute(sa.text("SELECT COUNT(*) FROM aceite_documento WHERE ip_usuario IS NOT NULL")).scalar()
    revisoes = conexao.execute(sa.text(
        "SELECT COUNT(*) FROM documento_legal WHERE versao_menor <> 0 OR versao_maior <> versao"
    )).scalar()
    if com_ip or revisoes:
        raise RuntimeError(
            "Downgrade do aceite obrigatório recusado: há IP de aceite gravado ou versão com rótulo de revisão. "
            "Faça backup e decida o que fazer com esses dados antes de remover as colunas."
        )
    op.drop_index("uq_documento_tipo_rotulo", table_name="documento_legal")
    op.drop_column("documento_legal", "versao_menor")
    op.drop_column("documento_legal", "versao_maior")
    op.drop_column("aceite_documento", "ip_usuario")
