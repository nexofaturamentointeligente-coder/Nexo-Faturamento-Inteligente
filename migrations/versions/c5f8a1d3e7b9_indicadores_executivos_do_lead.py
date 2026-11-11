"""Avaliação de Cenário: os indicadores executivos do envio do lead, apagados no expurgo de 30 dias.

Revision ID: c5f8a1d3e7b9
Revises: 9b3e5d7f1a28

Expand em lead_avaliacao, sem tocar nas demais tabelas: só ADD COLUMN anulável sem padrão (no PostgreSQL
não reescreve a tabela); nenhuma linha é reescrita.

  - lead_avaliacao ganha indicadores_executivos_json (TEXT): o nó indicadores_executivos do Raio-X do envio
    (indicadores_executivos.py), conferido pelo contrato do processo isolado (etl_runtime). Ao contrário
    dos resumos dos blocos, ele traz nome de produto (curvas ABC, ruptura, margem negativa por mês); por
    isso sai junto com as linhas de PDV no expurgo de 30 dias.
  - A função expurgar_brainstorming_pdv(dias) da f4c2a8e1d9b6 é trocada (CREATE OR REPLACE, que mantém o
    dono e as permissões) pela mesma regra mais um passo: o lead que perde as linhas vencidas e não fica com
    nenhuma recente perde também os indicadores. A assinatura e o retorno não mudam (o job do pg_cron da
    6a8f2d4c0e13 continua chamando a mesma função). O caminho em SQLAlchemy (leads_avaliacao.py) faz o mesmo.

O RLS e as políticas da f4c2a8e1d9b6 valem para a tabela inteira: a coluna nova já nasce protegida. Como
na 9b3e5d7f1a28, lead_avaliacao (tabela-mãe da staging, ON DELETE CASCADE) recebe ADD/DROP COLUMN direto,
nunca o modo batch, que no SQLite recriaria a tabela e apagaria as linhas de PDV em cascata. O downgrade
trava usuario e as duas tabelas no PostgreSQL, na ordem das requisições, recusa enquanto houver lead com os
indicadores preenchidos e devolve a função de expurgo da f4c2a8e1d9b6.
"""

from alembic import op
import sqlalchemy as sa


revision = "c5f8a1d3e7b9"
down_revision = "9b3e5d7f1a28"
branch_labels = None
depends_on = None


def _funcao_de_expurgo(com_indicadores: bool) -> str:
    """O bloco que (re)cria a função no esquema corrente, como a f4c2a8e1d9b6: esquema fixo no search_path,
    pg_temp por último, leads travados antes das linhas e cada comando separado (READ COMMITTED)."""
    indicadores = """
    UPDATE lead_avaliacao l
       SET indicadores_executivos_json = NULL
     WHERE l.id = ANY (alvos)
       AND l.status <> 'convertido'
       AND l.indicadores_executivos_json IS NOT NULL
       AND EXISTS (SELECT 1 FROM brainstorming_dados_pdv b WHERE b.lead_id = l.id AND b.criado_em < corte)
       AND NOT EXISTS (SELECT 1 FROM brainstorming_dados_pdv b WHERE b.lead_id = l.id AND b.criado_em >= corte);""" \
        if com_indicadores else ""
    return """
DO $bloco$
DECLARE
    esquema text := quote_ident(current_schema());
BEGIN
    EXECUTE 'CREATE OR REPLACE FUNCTION ' || esquema || $cria$.expurgar_brainstorming_pdv(dias integer DEFAULT 30)
RETURNS TABLE (leads_expirados integer, linhas_removidas integer)
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, $cria$ || esquema || $cria$, pg_temp
AS $corpo$
DECLARE
    corte timestamptz;
    alvos uuid[];
BEGIN
    IF dias IS NULL OR dias < 1 THEN
        RAISE EXCEPTION 'O prazo de retenção precisa ser de pelo menos 1 dia.';
    END IF;
    corte := now() - make_interval(days => dias);
    alvos := ARRAY(
        SELECT l.id
          FROM lead_avaliacao l
         WHERE l.status <> 'convertido'
           AND EXISTS (SELECT 1 FROM brainstorming_dados_pdv b WHERE b.lead_id = l.id AND b.criado_em < corte)
         ORDER BY l.id
           FOR UPDATE);""" + indicadores + """
    UPDATE lead_avaliacao l
       SET status = 'expirado', atualizado_em = now()
     WHERE l.id = ANY (alvos)
       AND l.status NOT IN ('expirado', 'convertido')
       AND EXISTS (SELECT 1 FROM brainstorming_dados_pdv b WHERE b.lead_id = l.id AND b.criado_em < corte)
       AND NOT EXISTS (SELECT 1 FROM brainstorming_dados_pdv b WHERE b.lead_id = l.id AND b.criado_em >= corte);
    GET DIAGNOSTICS leads_expirados = ROW_COUNT;
    DELETE FROM brainstorming_dados_pdv b
     USING lead_avaliacao l
     WHERE b.lead_id = l.id
       AND l.id = ANY (alvos)
       AND l.status <> 'convertido'
       AND b.criado_em < corte;
    GET DIAGNOSTICS linhas_removidas = ROW_COUNT;
    RETURN NEXT;
END
$corpo$$cria$;
    EXECUTE 'REVOKE ALL ON FUNCTION ' || esquema || '.expurgar_brainstorming_pdv(integer) FROM PUBLIC';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION ' || esquema || '.expurgar_brainstorming_pdv(integer) FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION ' || esquema || '.expurgar_brainstorming_pdv(integer) FROM authenticated';
    END IF;
END
$bloco$;
"""


def upgrade():
    op.add_column("lead_avaliacao", sa.Column("indicadores_executivos_json", sa.Text(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_funcao_de_expurgo(com_indicadores=True))


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # usuario primeiro, na ordem das requisições (como a 9b3e5d7f1a28).
        op.execute("LOCK TABLE usuario, lead_avaliacao, brainstorming_dados_pdv IN ACCESS EXCLUSIVE MODE")
    leads = conexao.execute(sa.text(
        "SELECT COUNT(*) FROM lead_avaliacao WHERE indicadores_executivos_json IS NOT NULL")).scalar()
    if leads:
        raise RuntimeError(
            "Downgrade dos indicadores executivos do lead recusado: há leads com indicadores_executivos_json "
            "preenchido. Faça backup e, se decidir seguir, apague esses leads ou anule a coluna deles antes de "
            "remover a coluna."
        )
    if conexao.dialect.name == "postgresql":
        op.execute(_funcao_de_expurgo(com_indicadores=False))
    if conexao.dialect.name == "sqlite":
        op.execute("ALTER TABLE lead_avaliacao DROP COLUMN indicadores_executivos_json")
    else:
        op.drop_column("lead_avaliacao", "indicadores_executivos_json")
