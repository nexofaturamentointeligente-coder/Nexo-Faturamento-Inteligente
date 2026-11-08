"""Planos por nível de inteligência: níveis, funcionalidades, matriz e o Raio-X de entrada.

Revision ID: b3e9d6c1f7a2
Revises: a8d3f6b91c24

Expand com quatro tabelas novas. Nenhuma coluna, FK ou CHECK muda em `plano` ou
`empresa` (regras G.1.5 e E4 de Docs/REESTRUTURACAO_COMERCIAL/
00_02_PROTECAO_BANCO_MIGRACOES_ETAPA_0_2_PR22.md):
  - plano_nivel: BRONZE, PRATA, OURO e CUSTOM, com a hierarquia, o modelo de
    atendimento e o limite de histórico. Liga-se ao plano comercial pelo código
    (plano.nome_plano = plano_nivel.codigo); o plano da empresa continua sendo
    empresa.id_plano_atual, e a situação da assinatura continua vindo de
    contract_policy (status_conta, contrato e coberturas).
  - funcionalidade e plano_funcionalidade: o catálogo de recursos e a matriz
    cumulativa (cada nível libera também o que o nível abaixo libera).
  - diagnostico_entrada: o último Raio-X de cada empresa, só com agregados.

Os dados de referência (níveis, funcionalidades e matriz) entram por INSERT ... WHERE
NOT EXISTS pela chave natural: repetir a carga não duplica nada e nenhum dado de
cliente é criado. `models` não é importado: os valores ficam congelados nesta revisão.

O código funciona antes desta migração (entitlements.esquema_planos_disponivel): sem as
tabelas, os recursos novos ficam ocultos, as rotas deles respondem 503 e o portal segue
igual.

No PostgreSQL as tabelas nascem com RLS ligado. Onde existirem os papéis da Data API do
Supabase (anon, authenticated), os privilégios deles são revogados e a política
restritiva deny_api_access lhes nega tudo; postgres e service_role, onde existirem,
recebem a política acesso_servidor. Num PostgreSQL sem esses papéis (testes, restauração
isolada) as políticas não são criadas: a aplicação entra como dona das tabelas e o RLS
não a afeta.

O downgrade recusa apagar dados: só remove as estruturas com diagnostico_entrada vazia e
o catálogo idêntico ao que esta revisão gravou.
"""

from alembic import op
import sqlalchemy as sa


revision = "b3e9d6c1f7a2"
down_revision = "a8d3f6b91c24"
branch_labels = None
depends_on = None

_TABELAS = ("plano_nivel", "funcionalidade", "plano_funcionalidade", "diagnostico_entrada")

# (codigo, nome, modelo de atendimento, atendimento, nível, meses de histórico; None = sem limite)
_NIVEIS = (
    ("BRONZE", "Bronze", "Self-Service", "100% digital, sem atendimento consultivo.", 1, 6),
    ("PRATA", "Prata", "Tech-Touch", "Onboarding remoto de 30 minutos e suporte prioritário.", 2, 24),
    ("OURO", "Ouro", "Assisted", "Reunião estratégica mensal de 30 minutos.", 3, None),
    ("CUSTOM", "Custom / Enterprise", "High-Touch", "Consultor dedicado e onboarding VIP.", 4, None),
)

# (chave, nome, descrição, código do nível que passa a liberar a funcionalidade)
_FUNCIONALIDADES = (
    ("curva_abc_vendas", "Curva ABC de vendas",
     "Ranking dos produtos por quantidade e faturamento bruto, com as classes A, B e C.", "BRONZE"),
    ("produtos_parados_ruptura", "Diagnóstico de produtos parados e ruptura",
     "Saldo estimado parado entre compras e vendas e os semáforos de estoque do período.", "BRONZE"),
    ("checklist_acoes", "Checklist de ações operacionais",
     "Semáforos com o que fazer e por quê, a partir dos indicadores do período.", "BRONZE"),
    ("raio_x_entrada", "Raio-X do PDV",
     "Saúde do cadastro, itens com variação de preço e potencial recuperável, a partir dos "
     "relatórios de vendas enviados.", "BRONZE"),
    ("auditoria_abaixo_custo", "Auditoria de vendas abaixo do custo",
     "Itens vendidos abaixo do custo informado pelo PDV, com a perda estimada de cada um.", "PRATA"),
    ("margem_bruta", "Margem bruta (R$ e %)",
     "Margem bruta estimada sobre o custo informado no relatório de vendas. Não é lucro.", "PRATA"),
    ("diagnostico_cmv", "Diagnóstico de CMV",
     "Custo das vendas segundo o custo informado pelo PDV. O CMV contábil depende de inventário.", "PRATA"),
    ("alerta_precificacao", "Alertas de precificação desatualizada",
     "Itens cujo custo subiu mais que o preço praticado entre o primeiro e o último mês enviados.", "PRATA"),
    ("curva_abc_clientes", "Curva ABC de clientes e matriz RFM",
     "Clientes por recência, frequência e valor de compra.", "OURO"),
    ("taxas_meios_pagamento", "Inteligência de taxas de meios de pagamento",
     "Custo das taxas por meio de pagamento e bandeira.", "OURO"),
    ("recompra_whatsapp", "Automação de recompra via WhatsApp",
     "Lembretes de recompra para clientes recorrentes.", "OURO"),
    ("dre_consolidada", "DRE consolidada e por unidade",
     "Demonstração de resultado da rede e de cada loja.", "CUSTOM"),
    ("analise_vendedores", "Análise de vendedores, comissões e descontos",
     "Desempenho, comissões e descontos concedidos por vendedor.", "CUSTOM"),
    ("seletor_multifilial", "Seletor multifilial",
     "Troca de loja no cabeçalho do portal para redes com várias unidades.", "CUSTOM"),
    ("parsers_sob_medida", "Parsers de ETL sob medida",
     "Leitura de exportações próprias do ERP do cliente.", "CUSTOM"),
)


def _matriz():
    """Pares (código do nível, chave): cada nível libera o que o nível abaixo libera."""
    posicao = {codigo: nivel for codigo, _, _, _, nivel, _ in _NIVEIS}
    return {
        (codigo, chave)
        for codigo, _, _, _, nivel, _ in _NIVEIS
        for chave, _, _, minimo in _FUNCIONALIDADES
        if posicao[minimo] <= nivel
    }


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
    FOREACH tabela IN ARRAY ARRAY['plano_nivel', 'funcionalidade', 'plano_funcionalidade', 'diagnostico_entrada'] LOOP
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


def upgrade():
    op.create_table(
        "plano_nivel",
        sa.Column("codigo", sa.String(length=20), nullable=False),
        sa.Column("nome", sa.String(length=60), nullable=False),
        sa.Column("modelo_atendimento", sa.String(length=60), nullable=False),
        sa.Column("atendimento", sa.String(length=255), nullable=False),
        sa.Column("nivel_hierarquico", sa.Integer(), nullable=False),
        sa.Column("limite_historico_meses", sa.Integer(), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(codigo) BETWEEN 2 AND 20", name="ck_plano_nivel_codigo"),
        sa.CheckConstraint("nivel_hierarquico >= 1", name="ck_plano_nivel_hierarquia"),
        sa.CheckConstraint("limite_historico_meses IS NULL OR limite_historico_meses >= 1",
                           name="ck_plano_nivel_historico"),
        sa.PrimaryKeyConstraint("codigo"),
        sa.UniqueConstraint("nivel_hierarquico", name="uq_plano_nivel_hierarquia"),
    )
    op.create_table(
        "funcionalidade",
        sa.Column("id_funcionalidade", sa.Integer(), nullable=False),
        sa.Column("chave", sa.String(length=60), nullable=False),
        sa.Column("nome", sa.String(length=120), nullable=False),
        sa.Column("descricao", sa.String(length=500), nullable=False),
        sa.CheckConstraint("length(chave) BETWEEN 3 AND 60", name="ck_funcionalidade_chave"),
        sa.PrimaryKeyConstraint("id_funcionalidade"),
        sa.UniqueConstraint("chave", name="uq_funcionalidade_chave"),
    )
    op.create_table(
        "plano_funcionalidade",
        sa.Column("codigo_plano", sa.String(length=20), nullable=False),
        sa.Column("id_funcionalidade", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["codigo_plano"], ["plano_nivel.codigo"], name="fk_plano_funcionalidade_plano"),
        sa.ForeignKeyConstraint(["id_funcionalidade"], ["funcionalidade.id_funcionalidade"],
                                name="fk_plano_funcionalidade_funcionalidade"),
        sa.PrimaryKeyConstraint("codigo_plano", "id_funcionalidade"),
    )
    op.create_index("ix_plano_funcionalidade_funcionalidade", "plano_funcionalidade", ["id_funcionalidade"],
                    unique=False)
    op.create_table(
        "diagnostico_entrada",
        sa.Column("id_empresa", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("id_usuario", sa.Integer(), nullable=True),
        sa.Column("gerado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("versao_regras", sa.String(length=40), nullable=False),
        sa.Column("arquivos", sa.Integer(), nullable=False),
        sa.Column("periodo_inicio", sa.Date(), nullable=True),
        sa.Column("periodo_fim", sa.Date(), nullable=True),
        sa.Column("indice_saude", sa.Numeric(5, 2), nullable=True),
        sa.Column("valor_recuperavel", sa.Numeric(), nullable=False),
        sa.Column("plano_recomendado", sa.String(length=20), nullable=False),
        sa.Column("resultado", sa.Text(), nullable=False),
        sa.CheckConstraint("arquivos >= 1", name="ck_diagnostico_entrada_arquivos"),
        sa.CheckConstraint("indice_saude IS NULL OR (indice_saude >= 0 AND indice_saude <= 100)",
                           name="ck_diagnostico_entrada_indice"),
        sa.CheckConstraint("valor_recuperavel >= 0", name="ck_diagnostico_entrada_recuperavel"),
        sa.CheckConstraint("periodo_inicio IS NULL OR periodo_fim IS NULL OR periodo_inicio <= periodo_fim",
                           name="ck_diagnostico_entrada_periodo"),
        sa.ForeignKeyConstraint(["id_empresa"], ["empresa.id_empresa"], name="fk_diagnostico_entrada_empresa",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["id_usuario"], ["usuario.id_usuario"], name="fk_diagnostico_entrada_usuario",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["plano_recomendado"], ["plano_nivel.codigo"], name="fk_diagnostico_entrada_plano"),
        sa.PrimaryKeyConstraint("id_empresa"),
    )
    op.create_index("ix_diagnostico_entrada_usuario", "diagnostico_entrada", ["id_usuario"], unique=False)

    # Valores presos à própria instrução (bindparams + op.execute): no modo offline o Alembic os
    # escreve no SQL gerado; parâmetros passados à conexão ficariam de fora da prévia.
    for codigo, nome, modelo, atendimento, nivel, meses in _NIVEIS:
        op.execute(sa.text(
            "INSERT INTO plano_nivel (codigo, nome, modelo_atendimento, atendimento, nivel_hierarquico, "
            "limite_historico_meses, criado_em) "
            "SELECT CAST(:codigo AS VARCHAR(20)), CAST(:nome AS VARCHAR(60)), CAST(:modelo AS VARCHAR(60)), "
            "CAST(:atendimento AS VARCHAR(255)), CAST(:nivel AS INTEGER), CAST(:meses AS INTEGER), "
            "CURRENT_TIMESTAMP "
            "WHERE NOT EXISTS (SELECT 1 FROM plano_nivel WHERE codigo = :codigo)"
        ).bindparams(sa.bindparam("codigo", codigo), sa.bindparam("nome", nome), sa.bindparam("modelo", modelo),
                     sa.bindparam("atendimento", atendimento), sa.bindparam("nivel", nivel),
                     sa.bindparam("meses", meses, type_=sa.Integer())))
    for chave, nome, descricao, _ in _FUNCIONALIDADES:
        op.execute(sa.text(
            "INSERT INTO funcionalidade (chave, nome, descricao) "
            "SELECT CAST(:chave AS VARCHAR(60)), CAST(:nome AS VARCHAR(120)), CAST(:descricao AS VARCHAR(500)) "
            "WHERE NOT EXISTS (SELECT 1 FROM funcionalidade WHERE chave = :chave)"
        ).bindparams(chave=chave, nome=nome, descricao=descricao))
    for codigo, chave in sorted(_matriz()):
        op.execute(sa.text(
            "INSERT INTO plano_funcionalidade (codigo_plano, id_funcionalidade) "
            "SELECT CAST(:codigo AS VARCHAR(20)), f.id_funcionalidade FROM funcionalidade f "
            "WHERE f.chave = :chave AND NOT EXISTS (SELECT 1 FROM plano_funcionalidade pf "
            "WHERE pf.codigo_plano = :codigo AND pf.id_funcionalidade = f.id_funcionalidade)"
        ).bindparams(codigo=codigo, chave=chave))

    if op.get_bind().dialect.name == "postgresql":
        for tabela in _TABELAS:
            op.execute(f"ALTER TABLE {tabela} ENABLE ROW LEVEL SECURITY")
        op.execute(_POLITICAS_POSTGRESQL)


def _catalogo_como_gravado(conexao) -> bool:
    """O catálogo ainda é exatamente o desta revisão: removê-lo não perde nada."""
    niveis = {tuple(linha) for linha in conexao.execute(sa.text(
        "SELECT codigo, nome, modelo_atendimento, atendimento, nivel_hierarquico, limite_historico_meses "
        "FROM plano_nivel"))}
    funcionalidades = {tuple(linha) for linha in conexao.execute(sa.text(
        "SELECT chave, nome, descricao FROM funcionalidade"))}
    matriz = {tuple(linha) for linha in conexao.execute(sa.text(
        "SELECT pf.codigo_plano, f.chave FROM plano_funcionalidade pf "
        "JOIN funcionalidade f ON f.id_funcionalidade = pf.id_funcionalidade"))}
    return (niveis == set(_NIVEIS)
            and funcionalidades == {(chave, nome, descricao) for chave, nome, descricao, _ in _FUNCIONALIDADES}
            and matriz == _matriz())


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma escrita em andamento não aparece na contagem e seria apagada pelo DROP: espera por ela.
        # usuario e empresa vêm primeiro, na ordem das requisições; com tráfego, repita se abortar.
        op.execute("LOCK TABLE usuario, empresa, " + ", ".join(_TABELAS) + " IN ACCESS EXCLUSIVE MODE")
    diagnosticos = conexao.execute(sa.text("SELECT COUNT(*) FROM diagnostico_entrada")).scalar()
    if diagnosticos or not _catalogo_como_gravado(conexao):
        raise RuntimeError(
            "Downgrade dos planos por nível recusado: há Raio-X gravado em diagnostico_entrada ou o catálogo "
            "de níveis e funcionalidades foi alterado depois da migração. Faça backup e decida o que fazer "
            "com esses dados antes de remover as estruturas."
        )
    op.drop_index("ix_diagnostico_entrada_usuario", table_name="diagnostico_entrada")
    op.drop_table("diagnostico_entrada")
    op.drop_index("ix_plano_funcionalidade_funcionalidade", table_name="plano_funcionalidade")
    op.drop_table("plano_funcionalidade")
    op.drop_table("funcionalidade")
    op.drop_table("plano_nivel")
