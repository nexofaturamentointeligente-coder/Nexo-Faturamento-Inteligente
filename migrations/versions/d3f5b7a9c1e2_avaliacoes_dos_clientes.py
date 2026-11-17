"""Avaliações dos clientes (COM-002): as quatro tabelas do módulo, criadas do zero ou adotadas.

Revision ID: d3f5b7a9c1e2
Revises: c8e2a4f6b0d1

Decisão do dono em 16/11/2026: reativar o "Deixar Avaliação" da landing (só para cliente logado, num modal com
estrelas e comentário). O módulo (avaliacoes.py e blueprints/avaliacoes.py) usa quatro tabelas, sem mexer em
nenhuma das existentes:
  - avaliacao_cliente: uma avaliação por usuário CLIENTE (nota de 1 a 5, comentário público, autorização de
    publicação com a versão do aviso, moderação, publicação e retirada, com controle de versão);
  - sugestao_melhoria: a sugestão privada, que só a equipe lê; sai com a avaliação (ON DELETE CASCADE);
  - evento_avaliacao: a trilha de auditoria, só de inclusão; sai com a avaliação (ON DELETE CASCADE);
  - solicitacao_avaliacao: o estado do pedido automático dos 60 dias, por usuário; sai com ele (ON DELETE CASCADE).

A estrutura é IDÊNTICA à da revisão c5e8a1f4b7d2 (02/11/2026), que saiu da cadeia mas já criou estas quatro
tabelas no banco do .env. Antes de qualquer DDL a revisão olha o banco e segue um de dois caminhos:
  - nenhuma das quatro existe: cria as quatro, com as restrições e os índices de models.py;
  - as quatro existem: confere cada uma contra o que models.py espera (colunas, tipos, nulidade, padrões do
    servidor, chave primária e a sequência dela, chaves estrangeiras com o ON DELETE, restrições únicas, CHECKs
    pelo nome e índices) e as ADOTA, acrescentando só o que falta sem risco: índice ausente, RLS e políticas.
Só parte das quatro, qualquer divergência ou uma política com o nome de uma das daqui e outra definição
interrompem a revisão com a lista do que diverge, antes de qualquer mudança: nada é apagado nem recriado (nunca
DROP de tabela, nunca create_all) e as linhas que já existem ficam como estão. Índice a mais, sem ser único, não
atrapalha.

A única definição diferente aceita é a deny_api_access PERMISSIVA de negação pura: FOR ALL, para exatamente os papéis
da Data API presentes (anon e authenticated), USING (false) e WITH CHECK ausente (vale o USING) ou (false). É a que o
banco do .env tinha nas quatro tabelas em 17/11/2026, criada fora das migrações (a c5e8a1f4b7d2 não cria políticas).
Ela já nega à Data API o mesmo que a restritiva; a revisão a troca pela restritiva na mesma transação (DROP POLICY
seguido do CREATE POLICY do bloco abaixo), sem intervalo visível sem política. Qualquer outra variação (outro papel,
USING ou WITH CHECK diferente de false, outro comando) continua sendo divergência.

No PostgreSQL as quatro ficam com RLS ligado, como a da d9e4b7a2c6f1: onde existirem os papéis da Data API do
Supabase (anon, authenticated), os privilégios deles são revogados e a política restritiva deny_api_access lhes
nega tudo; postgres e service_role, onde existirem, recebem a política acesso_servidor. Num PostgreSQL sem esses
papéis (testes, restauração isolada) as políticas não são criadas: a aplicação entra como dona das tabelas e o
RLS não a afeta. Tabela adotada com FORCE ROW LEVEL SECURITY é divergência (a aplicação deixaria de enxergá-la).

`models` não é importado: o esperado está descrito aqui (o teste de metadados compara com models.py). Na prévia
offline (flask db upgrade --sql) não há banco para conferir: sai o caminho da criação.

O downgrade recusa apagar dados: só remove as quatro tabelas quando estão vazias.
"""

from alembic import op
import sqlalchemy as sa


revision = "d3f5b7a9c1e2"
down_revision = "c8e2a4f6b0d1"
branch_labels = None
depends_on = None

# Na ordem de criação: sugestão e evento apontam para a avaliação.
_TABELAS = ("avaliacao_cliente", "sugestao_melhoria", "evento_avaliacao", "solicitacao_avaliacao")
# Chave primária numerada pelo banco (SERIAL no PostgreSQL); a de solicitacao_avaliacao é o próprio usuário.
_SEQUENCIAIS = {"avaliacao_cliente": "id_avaliacao", "sugestao_melhoria": "id_sugestao",
                "evento_avaliacao": "id_evento"}
_INDICES = (
    ("avaliacao_cliente", "ix_avaliacao_cliente_empresa", ("id_empresa",)),
    ("avaliacao_cliente", "ix_avaliacao_cliente_moderacao", ("status_moderacao", "data_atualizacao")),
    ("avaliacao_cliente", "ix_avaliacao_cliente_publicacao", ("status_moderacao", "autoriza_publicacao",
                                                              "publicada_em")),
    ("sugestao_melhoria", "ix_sugestao_status", ("status_acompanhamento", "data_atualizacao")),
    ("evento_avaliacao", "ix_evento_avaliacao", ("id_avaliacao", "ocorrido_em")),
)
# Nome -> (tipo, papéis previstos, USING, WITH CHECK), como pg_policies os mostra.
_POLITICAS = {
    "deny_api_access": ("RESTRICTIVE", ("anon", "authenticated"), "false", "false"),
    "acesso_servidor": ("PERMISSIVE", ("postgres", "service_role"), "true", "true"),
}

# Sem "%" no texto: a prévia offline (flask db upgrade --sql) sai igual ao que roda no banco. Idempotente: na
# adoção, uma política com o mesmo nome já foi conferida antes (mesma definição) e não é criada de novo.
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
    FOREACH tabela IN ARRAY ARRAY['avaliacao_cliente', 'sugestao_melhoria', 'evento_avaliacao',
                                  'solicitacao_avaliacao'] LOOP
        EXECUTE 'ALTER TABLE ' || quote_ident(tabela) || ' ENABLE ROW LEVEL SECURITY';
        IF papeis_api IS NOT NULL THEN
            EXECUTE 'REVOKE ALL ON TABLE ' || quote_ident(tabela) || ' FROM ' || papeis_api;
            IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                           AND tablename = tabela AND policyname = 'deny_api_access') THEN
                EXECUTE 'CREATE POLICY deny_api_access ON ' || quote_ident(tabela)
                     || ' AS RESTRICTIVE FOR ALL TO ' || papeis_api || ' USING (false) WITH CHECK (false)';
            END IF;
        END IF;
        IF papeis_servidor IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema()
                                                       AND tablename = tabela AND policyname = 'acesso_servidor') THEN
            EXECUTE 'CREATE POLICY acesso_servidor ON ' || quote_ident(tabela)
                 || ' AS PERMISSIVE FOR ALL TO ' || papeis_servidor || ' USING (true) WITH CHECK (true)';
        END IF;
    END LOOP;
END
$$;
"""


def _instante(nome, nulo=True):
    return sa.Column(nome, sa.DateTime(timezone=True), nullable=nulo)


def _definicao():
    """Colunas e restrições de cada tabela, iguais às de models.py (objetos novos a cada chamada)."""
    return {
        "avaliacao_cliente": [
            sa.Column("id_avaliacao", sa.Integer(), nullable=False),
            sa.Column("id_usuario", sa.Integer(), nullable=False),
            sa.Column("id_empresa", sa.Integer(), nullable=False),
            sa.Column("nota", sa.Integer(), nullable=False),
            sa.Column("comentario_publico", sa.Text(), nullable=False),
            sa.Column("nome_exibicao", sa.String(60), nullable=True),
            sa.Column("autoriza_publicacao", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("autoriza_nome_empresa", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("consentimento_versao", sa.String(20), nullable=True),
            _instante("consentimento_em"),
            _instante("consentimento_retirado_em"),
            sa.Column("status_moderacao", sa.String(20), nullable=False),
            sa.Column("motivo_rejeicao", sa.Text(), nullable=True),
            _instante("moderada_em"),
            sa.Column("id_usuario_moderador", sa.Integer(), nullable=True),
            _instante("publicada_em"),
            _instante("retirada_em"),
            sa.Column("retirada_motivo", sa.Text(), nullable=True),
            sa.Column("versao", sa.Integer(), server_default="1", nullable=False),
            _instante("data_criacao", nulo=False),
            _instante("data_atualizacao", nulo=False),
            sa.PrimaryKeyConstraint("id_avaliacao"),
            sa.ForeignKeyConstraint(["id_usuario"], ["usuario.id_usuario"]),
            sa.ForeignKeyConstraint(["id_empresa"], ["empresa.id_empresa"]),
            sa.ForeignKeyConstraint(["id_usuario_moderador"], ["usuario.id_usuario"]),
            sa.UniqueConstraint("id_usuario", name="uq_avaliacao_cliente_usuario"),
            sa.CheckConstraint("nota BETWEEN 1 AND 5", name="ck_avaliacao_nota"),
            sa.CheckConstraint("length(trim(comentario_publico)) > 0 AND length(comentario_publico) <= 1000",
                               name="ck_avaliacao_comentario"),
            sa.CheckConstraint("status_moderacao IN ('PENDENTE', 'APROVADA', 'REJEITADA')",
                               name="ck_avaliacao_status"),
            sa.CheckConstraint("autoriza_publicacao = false OR (consentimento_em IS NOT NULL "
                               "AND consentimento_versao IS NOT NULL AND consentimento_retirado_em IS NULL)",
                               name="ck_avaliacao_consentimento"),
            sa.CheckConstraint("autoriza_nome_empresa = false OR autoriza_publicacao = true",
                               name="ck_avaliacao_nome_empresa"),
            sa.CheckConstraint("status_moderacao = 'PENDENTE' OR "
                               "(moderada_em IS NOT NULL AND id_usuario_moderador IS NOT NULL)",
                               name="ck_avaliacao_moderacao"),
            sa.CheckConstraint("status_moderacao <> 'REJEITADA' OR motivo_rejeicao IS NOT NULL",
                               name="ck_avaliacao_rejeicao"),
            sa.CheckConstraint("publicada_em IS NULL OR retirada_em IS NOT NULL OR "
                               "(status_moderacao = 'APROVADA' AND autoriza_publicacao = true)",
                               name="ck_avaliacao_publicacao"),
            sa.CheckConstraint("retirada_em IS NULL OR publicada_em IS NOT NULL", name="ck_avaliacao_retirada"),
            sa.CheckConstraint("versao >= 1", name="ck_avaliacao_versao"),
        ],
        "sugestao_melhoria": [
            sa.Column("id_sugestao", sa.Integer(), nullable=False),
            sa.Column("id_avaliacao", sa.Integer(), nullable=False),
            sa.Column("texto", sa.Text(), nullable=False),
            sa.Column("status_acompanhamento", sa.String(20), nullable=False),
            _instante("data_criacao", nulo=False),
            _instante("data_atualizacao", nulo=False),
            _instante("status_alterado_em"),
            sa.Column("id_usuario_admin_status", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("id_sugestao"),
            sa.ForeignKeyConstraint(["id_avaliacao"], ["avaliacao_cliente.id_avaliacao"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["id_usuario_admin_status"], ["usuario.id_usuario"]),
            sa.UniqueConstraint("id_avaliacao", name="uq_sugestao_avaliacao"),
            sa.CheckConstraint("length(trim(texto)) > 0 AND length(texto) <= 2000", name="ck_sugestao_texto"),
            sa.CheckConstraint("status_acompanhamento IN ('NOVA', 'EM_ANALISE', 'CONCLUIDA')",
                               name="ck_sugestao_status"),
        ],
        "evento_avaliacao": [
            sa.Column("id_evento", sa.Integer(), nullable=False),
            sa.Column("id_avaliacao", sa.Integer(), nullable=False),
            sa.Column("tipo", sa.String(40), nullable=False),
            _instante("ocorrido_em", nulo=False),
            sa.Column("id_usuario_responsavel", sa.Integer(), nullable=False),
            sa.Column("justificativa", sa.Text(), nullable=True),
            sa.Column("consentimento_versao", sa.String(20), nullable=True),
            sa.PrimaryKeyConstraint("id_evento"),
            sa.ForeignKeyConstraint(["id_avaliacao"], ["avaliacao_cliente.id_avaliacao"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["id_usuario_responsavel"], ["usuario.id_usuario"]),
            sa.CheckConstraint("tipo IN ('CRIADA', 'EDITADA', 'APROVADA', 'REJEITADA', 'PUBLICADA', 'RETIRADA', "
                               "'CONSENTIMENTO_CONCEDIDO', 'CONSENTIMENTO_RETIRADO', 'SUGESTAO_ATUALIZADA')",
                               name="ck_evento_avaliacao_tipo"),
        ],
        "solicitacao_avaliacao": [
            sa.Column("id_usuario", sa.Integer(), autoincrement=False, nullable=False),
            _instante("ultima_exibicao_em"),
            _instante("adiada_em"),
            _instante("proxima_exibicao_em"),
            sa.Column("qtd_adiamentos", sa.Integer(), server_default="0", nullable=False),
            sa.PrimaryKeyConstraint("id_usuario"),
            sa.ForeignKeyConstraint(["id_usuario"], ["usuario.id_usuario"], ondelete="CASCADE"),
            sa.CheckConstraint("qtd_adiamentos >= 0", name="ck_solicitacao_adiamentos"),
        ],
    }


# ---------------------------------------------------------------------------
# Conferência das tabelas existentes (adoção)
# ---------------------------------------------------------------------------
def _nome_do_tipo(tipo, dialeto) -> str:
    try:
        return str(tipo.compile(dialect=dialeto))
    except Exception:  # noqa: BLE001 - só o texto da mensagem
        return type(tipo).__name__


def _mesmo_tipo(esperado, refletido, postgresql: bool) -> bool:
    """Mesma família e, onde importa, o mesmo tamanho; no PostgreSQL o instante precisa ter fuso (timestamptz)."""
    if isinstance(esperado, sa.Boolean):
        return isinstance(refletido, sa.Boolean)
    if isinstance(esperado, sa.Integer):
        return isinstance(refletido, sa.Integer) and not isinstance(refletido, (sa.BigInteger, sa.SmallInteger))
    if isinstance(esperado, sa.Text):
        return isinstance(refletido, sa.Text)
    if isinstance(esperado, sa.String):
        return (isinstance(refletido, sa.String) and not isinstance(refletido, sa.Text)
                and refletido.length == esperado.length)
    if isinstance(esperado, sa.DateTime):
        return isinstance(refletido, sa.DateTime) and (not postgresql or bool(getattr(refletido, "timezone", False)))
    return False


def _padrao(texto, booleano: bool):
    """Padrão do servidor sem aspas, parênteses nem conversão de tipo ('1', "'1'" e '1::integer' são iguais)."""
    if texto is None:
        return None
    valor = str(texto).strip().lower().split("::", 1)[0].strip()
    while len(valor) >= 2 and valor[0] == "(" and valor[-1] == ")":
        valor = valor[1:-1].strip()
    valor = valor.strip("'\"")
    if booleano:
        return {"0": "false", "f": "false", "1": "true", "t": "true"}.get(valor, valor)
    return valor


def _padrao_esperado(coluna, dialeto):
    if coluna.server_default is None:
        return None
    argumento = coluna.server_default.arg
    texto = argumento if isinstance(argumento, str) else str(argumento.compile(dialect=dialeto))
    return _padrao(texto, isinstance(coluna.type, sa.Boolean))


def _assinatura_fk(colunas, tabela, referidas, ondelete):
    acao = (ondelete or "").strip().upper()
    return (tuple(colunas), tabela, tuple(referidas), None if acao in ("", "NO ACTION") else acao)


def _texto_fk(assinatura) -> str:
    colunas, tabela, referidas, acao = assinatura
    return f"({', '.join(colunas)}) -> {tabela}({', '.join(referidas)})" + (f" ON DELETE {acao}" if acao else "")


def _divergencias_da_tabela(inspetor, tabela, dialeto, indices_ausentes) -> list:
    nome = tabela.name
    postgresql = dialeto.name == "postgresql"
    erros = []

    refletidas = {coluna["name"]: coluna for coluna in inspetor.get_columns(nome)}
    for coluna in tabela.columns:
        refletida = refletidas.pop(coluna.name, None)
        if refletida is None:
            erros.append(f"{nome}.{coluna.name}: coluna ausente")
            continue
        if not _mesmo_tipo(coluna.type, refletida["type"], postgresql):
            erros.append(f"{nome}.{coluna.name}: tipo {_nome_do_tipo(refletida['type'], dialeto)} "
                         f"(esperado {_nome_do_tipo(coluna.type, dialeto)})")
        if bool(refletida["nullable"]) != bool(coluna.nullable):
            erros.append(f"{nome}.{coluna.name}: {'aceita' if refletida['nullable'] else 'recusa'} nulo "
                         f"(esperado: {'aceita' if coluna.nullable else 'recusa'})")
        esperado = _padrao_esperado(coluna, dialeto)
        if esperado is not None:
            atual = _padrao(refletida.get("default"), isinstance(coluna.type, sa.Boolean))
            if atual != esperado:
                erros.append(f"{nome}.{coluna.name}: padrão do servidor {refletida.get('default')!r} "
                             f"(esperado {esperado})")
        if postgresql and _SEQUENCIAIS.get(nome) == coluna.name:
            if "nextval(" not in str(refletida.get("default") or "").lower() and not refletida.get("identity"):
                erros.append(f"{nome}.{coluna.name}: sem a sequência que numera as linhas")
    erros += [f"{nome}.{sobra}: coluna que models.py não tem" for sobra in refletidas]

    chave = list(inspetor.get_pk_constraint(nome).get("constrained_columns") or [])
    if chave != [coluna.name for coluna in tabela.primary_key.columns]:
        erros.append(f"{nome}: chave primária ({', '.join(chave) or 'nenhuma'})")

    esperadas = {_assinatura_fk(fk.column_keys, fk.elements[0].target_fullname.split(".")[0],
                                [elemento.target_fullname.split(".")[1] for elemento in fk.elements], fk.ondelete)
                 for fk in tabela.foreign_key_constraints}
    atuais = {_assinatura_fk(fk["constrained_columns"], fk["referred_table"], fk["referred_columns"],
                             (fk.get("options") or {}).get("ondelete"))
              for fk in inspetor.get_foreign_keys(nome)}
    erros += [f"{nome}: falta a chave estrangeira {_texto_fk(fk)}" for fk in sorted(esperadas - atuais, key=str)]
    erros += [f"{nome}: chave estrangeira que models.py não tem {_texto_fk(fk)}"
              for fk in sorted(atuais - esperadas, key=str)]

    esperadas = {restricao.name: tuple(coluna.name for coluna in restricao.columns)
                 for restricao in tabela.constraints if isinstance(restricao, sa.UniqueConstraint)}
    atuais = {restricao["name"]: tuple(restricao["column_names"]) for restricao in inspetor.get_unique_constraints(nome)}
    if atuais != esperadas:
        erros.append(f"{nome}: restrições únicas {sorted(atuais.items(), key=str)} "
                     f"(esperado {sorted(esperadas.items(), key=str)})")

    esperados = {restricao.name for restricao in tabela.constraints if isinstance(restricao, sa.CheckConstraint)}
    atuais = {restricao["name"] for restricao in inspetor.get_check_constraints(nome)}
    erros += [f"{nome}: falta o CHECK {nome_check}" for nome_check in sorted(esperados - atuais)]
    erros += [f"{nome}: CHECK que models.py não tem ({nome_check or 'sem nome'})"
              for nome_check in sorted(atuais - esperados, key=str)]

    nome_chave = inspetor.get_pk_constraint(nome).get("name")
    atuais = {indice["name"]: (tuple(indice["column_names"]), bool(indice.get("unique")))
              for indice in inspetor.get_indexes(nome)
              if not indice.get("duplicates_constraint") and indice["name"] != nome_chave}
    for tabela_indice, nome_indice, colunas in _INDICES:
        if tabela_indice != nome:
            continue
        atual = atuais.pop(nome_indice, None)
        if atual is None:
            indices_ausentes.append((tabela_indice, nome_indice, colunas))
        elif atual != (colunas, False):
            erros.append(f"{nome}: índice {nome_indice} em {atual[0]}{' (único)' if atual[1] else ''} "
                         f"(esperado em {colunas})")
    erros += [f"{nome}: índice único {nome_indice} que models.py não tem"
              for nome_indice, (_colunas, unico) in sorted(atuais.items()) if unico]
    return erros


def _negacao_permissiva(nome, tipo, comando, roles, usando, checando, papeis_api) -> bool:
    """A deny_api_access permissiva de negação pura (ver o cabeçalho): troca-se pela restritiva, sem divergência."""
    return (nome == "deny_api_access" and bool(papeis_api) and tipo == "PERMISSIVE" and comando == "ALL"
            and roles == papeis_api and usando == "false" and checando in (None, "false"))


def _divergencias_do_rls(conexao) -> tuple:
    """(divergências, tabelas cuja deny_api_access é a permissiva de negação pura): FORCE ligado ou política com o
    nome de uma das daqui e outra definição."""
    erros, a_normalizar = [], []
    forcadas = conexao.execute(sa.text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relkind = 'r' AND c.relforcerowsecurity "
        "AND c.relname = ANY(:tabelas) ORDER BY c.relname"), {"tabelas": list(_TABELAS)}).scalars().all()
    erros += [f"{tabela}: FORCE ROW LEVEL SECURITY ligado" for tabela in forcadas]
    papeis = set(conexao.execute(sa.text(
        "SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated', 'postgres', 'service_role')"
    )).scalars().all())
    politicas = conexao.execute(sa.text(
        "SELECT tablename, policyname, permissive, cmd, "
        "array_to_string(ARRAY(SELECT unnest(roles) ORDER BY 1), ','), qual, with_check "
        "FROM pg_policies WHERE schemaname = current_schema() AND tablename = ANY(:tabelas) "
        "AND policyname = ANY(:nomes) ORDER BY tablename, policyname"),
        {"tabelas": list(_TABELAS), "nomes": list(_POLITICAS)}).all()
    papeis_api = ",".join(sorted(papeis & set(_POLITICAS["deny_api_access"][1])))
    for tabela, nome, tipo, comando, roles, usando, checando in politicas:
        tipo_esperado, previstos, usando_esperado, checando_esperado = _POLITICAS[nome]
        esperado = (tipo_esperado, "ALL", ",".join(sorted(papeis & set(previstos))), usando_esperado,
                    checando_esperado)
        if (tipo, comando, roles, usando, checando) == esperado:
            continue
        if tabela in _TABELAS and _negacao_permissiva(nome, tipo, comando, roles, usando, checando, papeis_api):
            a_normalizar.append(tabela)
            continue
        erros.append(f"{tabela}: a política {nome} já existe com outra definição "
                     f"({tipo} {comando} para {roles or 'ninguém'})")
    return erros, a_normalizar


def _conferir(conexao):
    """(divergências, índices ausentes, tabelas com a deny_api_access permissiva a trocar) das quatro tabelas
    existentes contra models.py."""
    inspetor = sa.inspect(conexao)
    esperado = sa.MetaData()
    erros, indices_ausentes, a_normalizar = [], [], []
    for nome, itens in _definicao().items():
        erros += _divergencias_da_tabela(inspetor, sa.Table(nome, esperado, *itens), conexao.dialect,
                                         indices_ausentes)
    if conexao.dialect.name == "postgresql":
        erros_rls, a_normalizar = _divergencias_do_rls(conexao)
        erros += erros_rls
    return erros, indices_ausentes, a_normalizar


def _criar():
    definicao = _definicao()
    for nome in _TABELAS:
        op.create_table(nome, *definicao[nome])
    for tabela, nome_indice, colunas in _INDICES:
        op.create_index(nome_indice, tabela, list(colunas), unique=False)


def upgrade():
    if op.get_context().as_sql:
        _criar()
    else:
        conexao = op.get_bind()
        inspetor = sa.inspect(conexao)
        presentes = [nome for nome in _TABELAS if inspetor.has_table(nome)]
        if not presentes:
            _criar()
        elif len(presentes) < len(_TABELAS):
            ausentes = [nome for nome in _TABELAS if nome not in presentes]
            raise RuntimeError(
                "Revisão d3f5b7a9c1e2 interrompida: só parte das tabelas das avaliações existe (presentes: "
                + ", ".join(presentes) + "; ausentes: " + ", ".join(ausentes) + "). Nada foi alterado. Confira o "
                "banco e faça backup antes de decidir como seguir: esta revisão não apaga nem recria tabelas."
            )
        else:
            divergencias, indices_ausentes, a_normalizar = _conferir(conexao)
            if divergencias:
                raise RuntimeError(
                    "Revisão d3f5b7a9c1e2 interrompida: as tabelas das avaliações já existem, mas não batem com "
                    "models.py: " + "; ".join(divergencias) + ". Nada foi alterado. Confira a estrutura e faça "
                    "backup antes de decidir como seguir: esta revisão não apaga nem recria tabelas."
                )
            for tabela, nome_indice, colunas in indices_ausentes:
                op.create_index(nome_indice, tabela, list(colunas), unique=False)
            for tabela in a_normalizar:
                # A permissiva sai e a restritiva entra no bloco das políticas logo abaixo, na mesma transação.
                op.execute(f"DROP POLICY deny_api_access ON {tabela}")
    if op.get_context().dialect.name == "postgresql":
        op.execute(_POLITICAS_POSTGRESQL)


def downgrade():
    conexao = op.get_bind()
    if conexao.dialect.name == "postgresql":
        # Uma escrita em andamento não aparece na contagem e seria apagada pelo DROP: espera por ela.
        # usuario e empresa vêm primeiro, na ordem das requisições; com tráfego, repita se abortar.
        op.execute("LOCK TABLE usuario, empresa, " + ", ".join(_TABELAS) + " IN ACCESS EXCLUSIVE MODE")
    com_dados = [nome for nome in _TABELAS
                 if conexao.execute(sa.text(f"SELECT COUNT(*) FROM {nome}")).scalar()]
    if com_dados:
        raise RuntimeError(
            "Downgrade das avaliações recusado: há dados de clientes em " + ", ".join(com_dados)
            + ". Faça backup e decida o destino desses dados antes de remover as tabelas."
        )
    for nome in reversed(_TABELAS):
        op.drop_table(nome)
