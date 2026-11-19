# NEXO - Faturamento Inteligente | Proteção do Banco e Preparação das Migrações

**Etapa 0.2 — Proteção do banco de dados e preparação das migrações**

> **Nota de integração (26/10/2026).** Registro da Etapa 0.2 executado no GitHub (PR #22,
> commit `49b07d0`, base `c919b1d`), preservado com o conteúdo original. Na branch
> `feature/reestruturacao-comercial-nexo` ele ocupava o caminho `00_02_PROTECAO_BANCO_MIGRACOES.md`;
> na integração com a `main`, esse caminho passou a abrigar a versão da Etapa 0.2A, produzida
> localmente e citada pelos demais documentos (por exemplo, "documento 00_02, seção G"). As duas
> auditorias foram feitas em paralelo em 24/10/2026, com estruturas de seções diferentes:
> referências internas deste arquivo ("A.1", "G.4", "Apêndice A") dizem respeito a ele próprio.
> Ver [00_02 — Etapa 0.2A](00_02_PROTECAO_BANCO_MIGRACOES.md) e o
> [relatório da integração](../INTEGRACOES/RELATORIO_INTEGRACAO_GITHUB_ZIP_2026_10_26.md). Fato
> posterior a ambas: o HTTP 500 do login e a migração `e4c004a26b01` pendente no Supabase estão em
> [RELATORIO_CORRECAO_MIGRACAO_SUPABASE_NEXO.md](../../RELATORIO_CORRECAO_MIGRACAO_SUPABASE_NEXO.md).
>
> **Estado após a integração.** A constatação de que só a lixeira bloqueia o CLIENTE (B.5, H.3, I.5)
> foi corrigida pela NEXO-AJ-06.1, integrada junto: CLIENTE de empresa `CANCELADA` é negado por todas as
> vias; `SUSPENSA` e a lixeira mantêm o comportamento. S14 (`.gitignore`) e S15 (caminho local em
> `OPERACAO_BACKUP_RESTAURACAO.md`) foram resolvidos; os testes de prontidão e de ensaio passaram a 18 e 8.
> As instruções de backup real deste registro (F.2, Apêndice B) divergem das da 0.2A e do PR #23; ver a
> nota ao fim de [00_02_PROTECAO_BANCO_MIGRACOES.md](00_02_PROTECAO_BANCO_MIGRACOES.md).

Este documento prepara o NEXO para futuras alterações estruturais no banco PostgreSQL/Supabase.
Ele registra o inventário do esquema conforme o código e as migrações, a auditoria da cadeia
Alembic, a auditoria do sistema de backup existente, os testes locais executados, o procedimento
para o backup real do Supabase (a ser executado depois, com autorização) e a estratégia para as
migrações das próximas etapas da reestruturação comercial.

Complementa `Docs/REESTRUTURACAO_COMERCIAL/00_01_BASELINE_GITHUB.md` (Etapa 0.1) e não o substitui.

## Escopo, limites e convenções

**O que esta etapa fez:** leitura do código, das migrações e da documentação; execução de testes
locais com PostgreSQL privado e dados exclusivamente sintéticos; consulta à documentação oficial do
Supabase, do PostgreSQL e do GitHub; criação deste documento e um ajuste preventivo no `.gitignore`.

**O que esta etapa NÃO fez:**

- Não acessou o Supabase de produção nem qualquer banco real. Nenhuma credencial real foi usada.
- Não executou `flask db upgrade`, `downgrade`, `stamp` ou `current` contra banco real; não alterou
  `alembic_version`.
- Não gerou backup real, não exportou dados de clientes, não restaurou nada no Supabase.
- Não acessou nem alterou o Render (painel, variáveis, deploy).
- Não alterou configurações administrativas do GitHub (proteção de branch, Actions).
- Não alterou modelos, migrações, autenticação, regras de planos, funcionalidades ou dependências.
- Não executou `criar_cliente_demo.py` nem testou a conta de demonstração.

**Classificação das evidências** (usada em todo o documento):

| Marca | Significado |
|---|---|
| **[VALIDADO LOCALMENTE]** | Executado nesta etapa, em ambiente local isolado, com dados sintéticos |
| **[CÓDIGO]** | Verificado por leitura do código versionado (`arquivo:linha`) |
| **[MIGRAÇÕES]** | Previsto pelas revisões Alembic (o que um banco migrado até a head deve conter) |
| **[DOC]** | Verificado em documentação (do repositório ou oficial externa, com a fonte indicada) |
| **[PENDENTE NO SUPABASE REAL]** | Só pode ser confirmado no projeto real, com autorização; nada é afirmado sobre ele |
| **[PENDENTE NO RENDER]** / **[PENDENTE]** | Depende de acesso autorizado (Render, GitHub ou Supabase) ou de decisão do proprietário; nada é afirmado sobre o estado real |

Nenhum valor de segredo, credencial, e-mail, telefone ou nome de pessoa é reproduzido. Quando um
dado sensível existe no repositório, registra-se apenas a sua localização.

---

## A. Identificação

### A.1 Dados da execução

| Item | Valor |
|---|---|
| Projeto | NEXO - Faturamento Inteligente |
| Repositório | https://github.com/Helio965/Nexo---Faturamento-Inteligente---Render (privado) |
| Branch principal | `main` |
| Branch de integração | `feature/reestruturacao-comercial-nexo` |
| Branch de trabalho desta etapa | `feature/nexo-etapa-0-2-protecao-banco` |
| Commit de referência | `c919b1d17e6cbf4bf9dac09e29cefca67d908d06` (merge da PR #19, Etapa 0.1) |
| Commit da Etapa 0.1 | `ca166cfb59052fa1596d69782a5703eb457d78a7` |
| Commit de referência da Etapa 0.1 (`origin/main` à época; merge da PR #18, SEC-005) | `8233c5bddbd1f6c67a4d95c7b0a2a470724738c8` |
| Data da execução | 2026-10-24 |

### A.2 Situação do GitHub (Subetapa 0.2.1)

| Verificação | Resultado | Evidência |
|---|---|---|
| Diretório pertence ao repositório oficial | Sim | `git remote -v`: `origin` fetch/push para o repositório da tabela A.1 |
| Referências atualizadas | Sim | `git fetch origin --prune` |
| Commit atual da `main` | `c919b1d17e6cbf4bf9dac09e29cefca67d908d06` (merge da PR #19), o mesmo identificado ao fim da Etapa 0.1. Desde o commit de referência da Etapa 0.1 (`8233c5b`), a `main` avançou apenas por esse merge (`git rev-list 8233c5b..c919b1d` = `ca166cf`, `c919b1d`) | `git log origin/main`; API do GitHub |
| Branch de integração antes da sincronização | Em `ca166cf`: 0 commits exclusivos, 1 commit atrás da `main` (o merge da PR #19) | `git rev-list --left-right --count origin/feature/reestruturacao-comercial-nexo...origin/main` = `0 1` |
| Sincronização da branch de integração | **Fast-forward** para `c919b1d` e push simples (sem `--force`) | `git merge --ff-only origin/main`; push `ca166cf..c919b1d` |
| Documento da Etapa 0.1 | Presente em `main` e na integração (mesmo blob) | `git ls-tree` |
| Alterações locais não commitadas | Nenhuma no início | `git status`, `git stash list`, `git worktree list` |
| Branch de trabalho | `feature/nexo-etapa-0-2-protecao-banco` criada da integração sincronizada (`c919b1d`), sem tracking | `git switch --no-track -c` |
| GitHub Actions recentes | "NEXO Security CI" **success** no push de `c919b1d` (run 40099609684) e na PR #19 (run 40099603218); runs anteriores na `main` também verdes | API `actions/runs` |
| Proteção da `main` | **Inexistente**: `protected: false` | API `list_branches` (2026-10-24) |
| `main` ao fim desta etapa | Avançou para `4b7073bc15e2b13dfc76f371e399b85e325fe046` (merge da PR #21, COM-001, landing comercial), feito pelo proprietário durante a execução, fora desta etapa. Não altera `models.py`, `migrations/`, `scripts/`, `tests/test_backup_restore.py`, `tests/postgresql_support.py`, `Docs/OPERACAO_BACKUP_RESTAURACAO.md`, `.gitignore` nem `criar_cliente_demo.py`; altera `.github/workflows/nexo-security-ci.yml` (navegador e diagnóstico do job `security-sqlite-browser`). A integração e esta branch continuam em `c919b1d`; **as referências `arquivo:linha` deste documento valem para `c919b1d`** (as do workflow mudaram de linha na `main`) | `git fetch origin main`; `git diff --stat c919b1d origin/main` |

### A.3 Proteção da `main`: diagnóstico e proposta

**Diagnóstico [DOC]:**

- A `main` não tem proteção (`protected: false`). Um merge ou push na `main` pode disparar deploy
  se o Auto-Deploy estiver ativo no Render, conforme relato registrado em
  `Docs/PREPARACAO_RENDER.md:81` (estado real **[PENDENTE NO RENDER]**).
- A auditoria anterior registrou `GET /rulesets` com HTTP 403 "Upgrade to GitHub Pro or make this
  repository public to enable this feature" e leitura da proteção clássica negada à integração
  (`Docs/PLANO_PUBLICACAO_SEGURA.md:9-17`).
- Pela documentação do GitHub, branches protegidas e rulesets estão disponíveis em repositórios
  **públicos** no GitHub Free, e em repositórios **privados** apenas com GitHub Pro, Team ou
  Enterprise. A restrição de quem pode fazer push vale só para repositórios de organização.
  Fontes: [Managing protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches),
  [GitHub's plans](https://docs.github.com/get-started/learning-about-github/githubs-products),
  [About rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets),
  [About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).

**Proposta para aplicação pelo proprietário** (nenhuma configuração foi alterada nesta etapa):

1. **Decisão de plano.** Manter o repositório privado. Tornar o repositório público **não** é
   recomendado: o histórico contém uma credencial de demonstração (seção H.2) e código comercial.
   A proteção técnica para repositório privado exige GitHub Pro (conta pessoal) ou superior.
2. **Regra para `main`**, quando disponível (branch protection clássica ou ruleset):
   - exigir Pull Request antes do merge;
   - exigir os checks obrigatórios `security-sqlite-browser` e `security-postgresql` (nomes exatos
     dos jobs em `.github/workflows/nexo-security-ci.yml:33, 63`), com branch atualizada;
   - exigir resolução das conversas;
   - bloquear force push e exclusão da branch;
   - **não** exigir histórico linear (o projeto usa merge commits);
   - aprovações: com um único mantenedor, uma aprovação obrigatória bloquearia todo merge (o autor
     não aprova a própria PR). Definir 0 aprovações obrigatórias enquanto não houver segundo
     revisor, mantendo PR e checks obrigatórios; elevar para 1 quando houver.
3. **Merges automáticos:** manter desativado "Allow auto-merge" nas configurações do repositório;
   não habilitar merge queue.
4. **Recuperação administrativa:** manter o proprietário como administrador com possibilidade de
   editar/desativar a regra (bypass de administrador) e registrar, em local privado, quem é o
   responsável substituto e o procedimento de recuperação. Se optar por "Do not allow bypassing",
   documentar antes como desfazer a regra.
5. **Render:** desligar o Auto-Deploy no painel (coerente com `autoDeployTrigger: 'off'` em
   `render.yaml:18`) para que um merge na `main` nunca implante código antes da migração.
6. **Enquanto não houver proteção técnica:** política manual obrigatória — nenhum push direto na
   `main`; todo merge via PR com CI verde; nenhuma PR com migração mesclada sem o procedimento da
   seção G.4. Essa política **não equivale** a proteção técnica.

---

## B. Estado do banco no código

### B.1 Fontes de esquema e contagens

| Fonte | Tabelas | Papel | Evidência |
|---|---|---|---|
| `models.py` | **14** modelos (`db.Model`), confirmados diretamente no código | Metadata da aplicação | 14 `__tablename__` em `models.py:38-549` [CÓDIGO] |
| `migrations/versions/` | 14 tabelas + `alembic_version` (criada pelo Alembic) | Única fonte válida para criar o esquema PostgreSQL | 12 `create_table` na raiz, mais `guia_topico` e `arquivo_armazenado` [MIGRAÇÕES] |
| Banco PostgreSQL 16.15 local migrado até a head | **15** tabelas em `public` (14 + `alembic_version`) | Prova de que as migrações produzem o esquema esperado | [VALIDADO LOCALMENTE] — catálogo lido após `flask_migrate.upgrade` (seção E) |
| `schema_nexo_mvp_v4.sql` (raiz) | 14 (12 em comum + `avaliacao_analise` e `fatura_cobranca`) | DDL SQLite histórico; **não** cria nem restaura o banco | [CÓDIGO] |
| `Docs/schema_nexo_mvp_v4 (1).sql` | 12 | DDL SQLite mais antigo | [CÓDIGO] |
| `init_db.py` | 14 via `create_all` | Só SQLite de desenvolvimento; recusa PostgreSQL; não grava `alembic_version` | `init_db.py:44-55` [CÓDIGO] |

Totais do esquema produzido pelas migrações [VALIDADO LOCALMENTE em PostgreSQL 16.15]:
142 colunas de aplicação (143 com `alembic_version.version_num`), 15 PK, 16 FK, 8 UNIQUE,
20 CHECK, 5 índices nomeados, 28 índices no total (incluindo os de PK/UNIQUE) e 13 sequências.

### B.2 Inventário por tabela

PK inteira = `Integer` com sequência `<tabela>_<pk>_seq` no PostgreSQL [VALIDADO LOCALMENTE].
"Default só ORM" = valor aplicado pelo SQLAlchemy, sem `DEFAULT` no banco [MIGRAÇÕES].

| Tabela (modelo) | PK | FKs | UNIQUE | CHECK | Índices nomeados | Observações |
|---|---|---|---|---|---|---|
| `plano` (`Plano`) | `id_plano` | — | `nome_plano` | 5: `nome_plano` ∈ {BRONZE, PRATA, OURO}; `tipo_analise_permitida`; `nivel_entrega_analise`; `nivel_dashboard`; `nivel_atendimento` | — | Campos de nível existem mas não são lidos pela aplicação (Etapa 0.1, C.4); `ativo` default só ORM |
| `segmento` (`Segmento`) | `id_segmento` | — | `nome_segmento` | — | — | `ativo` default só ORM |
| `empresa` (`Empresa`) | `id_empresa` | `id_segmento` → segmento; `id_plano_atual` → plano | `cnpj` | `status_conta` ∈ {ATIVA, SUSPENSA, CANCELADA} | — | `deletado_em` = soft delete (lixeira); `status_conta` default só ORM |
| `usuario` (`Usuario`) | `id_usuario` | `id_empresa` → empresa (anulável) | `email` (sensível a maiúsculas) | `role` ∈ {ADMIN, CLIENTE}; `ck_usuario_role_empresa`; `ck_usuario_geracao_autorizacao` | — | `senha_hash`; `primeiro_acesso` DEFAULT false no banco, mas default ORM `True` (inserção fora do ORM não força a troca de senha); `geracao_autorizacao` DEFAULT 0; `ativo` default só ORM; `ultimo_acesso` nunca é gravado |
| `analise` (`Analise`) | `id_analise` | `id_empresa` → empresa; `id_plano_referencia` → plano; `id_usuario_admin_responsavel` → usuario | — | `mes_referencia` 1..12; `tipo_analise`; `status_analise`; `ck_analise_tipo_quinzena` | — | Cascade ORM `all, delete-orphan` para 4 filhos (`models.py:219-241`); `status_analise` default só ORM (`AGUARDANDO_RELATORIO`) |
| `upload_relatorio` (`UploadRelatorio`) | `id_upload` | `id_analise` → analise; `id_usuario_admin` → usuario (anulável) | `uq_upload_analise_tipo` (id_analise, tipo_relatorio) | `tipo_relatorio`; `extensao_arquivo`; `status_processamento` | — | `caminho_arquivo` = `db:<chave>` sem FK para `arquivo_armazenado`; `hash_arquivo` SHA-256; `status_processamento` default só ORM (`PENDENTE`) |
| `indicador_analise` (`IndicadorAnalise`) | `id_indicador` | `id_analise` → analise | `id_analise` (1:1) | — | — | KPIs `Numeric`; `periodo_base_inicio/fim`; `versao_processamento` default só ORM |
| `relatorio_analise` (`RelatorioAnalise`) | `id_relatorio` | `id_analise` → analise | `id_analise` (1:1) | — | — | `publicado` default só ORM; `gerado_por_ia` DEFAULT false |
| `produto_curva_abc` (`ProdutoCurvaABC`) | `id_produto_abc` | `id_analise` → analise | `uq_abc_analise_posicao` | `classe_abc` ∈ {A, B, C} | `ix_abc_analise` | Top 20 por análise |
| `chamado_suporte` (`ChamadoSuporte`) | `id_chamado` | `id_empresa` → empresa; `id_usuario_cliente` → usuario | — | `categoria`; `status_chamado`; `prioridade` | `ix_chamado_empresa` | Defaults de status/prioridade só ORM |
| `mensagem_suporte` (`MensagemSuporte`) | `id_mensagem` | `id_chamado` → chamado_suporte; `id_usuario_remetente` → usuario | — | — | `ix_mensagem_chamado` | Sem cascade; `lida` default só ORM |
| `notificacao` (`Notificacao`) | `id_notificacao` | `id_usuario` → usuario | — | — | `ix_notificacao_usuario` | `link_destino` textual; `lida` default só ORM |
| `guia_topico` (`GuiaTopico`) | `id` | — | — | — | `ix_guia_topico_categoria` | `imagem_url` = `db:guia/<hash>` sem FK; `ativo` DEFAULT true |
| `arquivo_armazenado` (`ArquivoArmazenado`) | `chave` `String(255)` (natural, sem sequência) | — | — | — | — | `conteudo` `LargeBinary` (BYTEA) `deferred`; planilhas dos clientes e imagens do Guia |

Inserções fora do ORM (SQL direto ou `sa.table` em migração de dados) devem informar explicitamente
todas as colunas NOT NULL com default só ORM, sob pena de falha por NOT NULL ou de estado incorreto.

Colunas FK **sem índice** [MIGRAÇÕES; VALIDADO LOCALMENTE]: `empresa.id_segmento`,
`empresa.id_plano_atual`, `usuario.id_empresa`, `analise.id_empresa`, `analise.id_plano_referencia`,
`analise.id_usuario_admin_responsavel`, `upload_relatorio.id_usuario_admin`,
`chamado_suporte.id_usuario_cliente`, `mensagem_suporte.id_usuario_remetente`. O PostgreSQL não cria
índice automático para FK. Os `idx_*` do `.sql` histórico nunca entraram nas migrações.

### B.3 Nomes das constraints no PostgreSQL

`extensions.py:20-26` define a `Base` declarativa **sem** `naming_convention`. Só 5 constraints têm
nome explícito no código: `ck_usuario_role_empresa`, `ck_usuario_geracao_autorizacao`,
`ck_analise_tipo_quinzena`, `uq_upload_analise_tipo`, `uq_abc_analise_posicao`.

**[VALIDADO LOCALMENTE]** As migrações versionadas, aplicadas a um PostgreSQL 16.15 vazio, geram estes
nomes para as constraints sem nome explícito:

- **CHECK (17):** `plano_nome_plano_check`, `plano_tipo_analise_permitida_check`,
  `plano_nivel_entrega_analise_check`, `plano_nivel_dashboard_check`, `plano_nivel_atendimento_check`,
  `empresa_status_conta_check`, `usuario_role_check`, `analise_mes_referencia_check`,
  `analise_tipo_analise_check`, `analise_status_analise_check`, `upload_relatorio_tipo_relatorio_check`,
  `upload_relatorio_extensao_arquivo_check`, `upload_relatorio_status_processamento_check`,
  `chamado_suporte_categoria_check`, `chamado_suporte_status_chamado_check`,
  `chamado_suporte_prioridade_check`, `produto_curva_abc_classe_abc_check`.
- **UNIQUE (6):** `plano_nome_plano_key`, `segmento_nome_segmento_key`, `empresa_cnpj_key`,
  `usuario_email_key`, `indicador_analise_id_analise_key`, `relatorio_analise_id_analise_key`.
- **FK (16), todas `ON DELETE NO ACTION`:** padrão `<tabela>_<coluna>_fkey` (ex.:
  `usuario_id_empresa_fkey`, `analise_id_empresa_fkey`, `upload_relatorio_id_analise_fkey`).
- **PK (15):** padrão `<tabela>_pkey`; `alembic_version_pkc` para o Alembic.
- **RLS:** `relrowsecurity = false` em todas as 15 tabelas.

**[PENDENTE NO SUPABASE REAL]** Os nomes reais em produção só são iguais a estes se o banco de
produção tiver sido criado por estas migrações. Antes de qualquer migração que altere uma constraint
existente (por exemplo, o CHECK de `nome_plano`), os nomes devem ser lidos em `pg_constraint` do
banco real com consulta somente leitura (Apêndice A, consulta A-2).

### B.4 Relacionamentos

```
segmento 1 ──< N empresa              (empresa.id_segmento NOT NULL)
plano    1 ──< N empresa              (empresa.id_plano_atual NOT NULL)
plano    1 ──< N analise              (analise.id_plano_referencia NOT NULL — snapshot do plano)
empresa  1 ──< 0..N usuario           (usuario.id_empresa; CHECK: CLIENTE exige, ADMIN proíbe)
empresa  1 ──< 0..N analise
empresa  1 ──< 0..N chamado_suporte
usuario  1 ──< 0..N analise           (admin responsável; "é ADMIN" só a aplicação garante)
usuario  1 ──< 0..N upload_relatorio  (id_usuario_admin anulável: NULL = enviado pelo cliente)
usuario  1 ──< 0..N chamado_suporte / mensagem_suporte / notificacao
analise  1 ──< 0..2 upload_relatorio  [cascade ORM]
analise  1 ──  0..1 indicador_analise [cascade ORM]
analise  1 ──  0..1 relatorio_analise [cascade ORM]
analise  1 ──< 0..N produto_curva_abc [cascade ORM]
chamado_suporte 1 ──< 0..N mensagem_suporte (sem cascade)
upload_relatorio.caminho_arquivo ┄┄ 'db:<chave>' (SEM FK) ┄┄> arquivo_armazenado.chave
guia_topico.imagem_url           ┄┄ 'db:guia/<hash>' (SEM FK) ┄┄> arquivo_armazenado.chave
```

Invariantes garantidas só pela aplicação [CÓDIGO]: admin responsável é ADMIN; cliente do chamado
pertence à empresa do chamado; `status_analise = 'CONCLUIDO'` implica `relatorio_analise.publicado =
true` (os dois mudam juntos só em `blueprints/admin.py:820-822, 836-837`). O dashboard do cliente
(`blueprints/cliente.py:99-106, 144-159`) filtra só CONCLUIDO e depende desse acoplamento; histórico,
detalhe e PDF (`blueprints/cliente.py:229-236, 251-259, 272-283`) conferem os dois. Nenhuma migração
de dados pode alterar um sem o outro.

### B.5 Mecanismos de exclusão [CÓDIGO]

| Mecanismo | Tabelas | Onde | Reversível |
|---|---|---|---|
| Soft delete (lixeira) | `empresa.deletado_em`; incrementa `usuario.geracao_autorizacao` dos CLIENTE | `company_security.py:49-61`; rota em `blueprints/admin.py:447-483` | Sim (`restaurar_empresa`, `company_security.py:64-72`, sem reativar contas) |
| Flags | `plano.ativo`, `segmento.ativo`, `usuario.ativo`, `guia_topico.ativo`, `empresa.status_conta` | `models.py` | Sim |
| Cascade ORM | 4 filhos de `analise` | `models.py:219-241` | Não |
| Exclusão permanente de empresa | Enumeração manual: curva ABC, uploads + arquivos, indicador, relatório, análises, mensagens, chamados, notificações, usuários CLIENTE, empresa — uma transação, rollback em erro para as linhas do banco (incluindo `arquivo_armazenado`); arquivos **legados** em disco são apagados antes do commit (`admin.py:566-567` → `storage.py:208-213`) e não voltam em caso de rollback | `blueprints/admin.py:523-612` | **Não** |
| Exclusão de análise | `analise` + filhos via cascade ORM + arquivos (legados em disco apagados antes do commit, `admin.py:993-994`); aceita análise publicada; **sem teste automatizado** | `blueprints/admin.py:961-1010` | **Não** |
| Substituição de upload / reprocessamento ETL | `upload_relatorio`, `arquivo_armazenado`, `produto_curva_abc` | `blueprints/cliente.py:352-386`; `blueprints/admin.py:667-700`; `etl_processor.py` | Não |
| `ON DELETE` no banco | **Nenhum** (16 FKs `NO ACTION`) | [VALIDADO LOCALMENTE] catálogo | — |

Consequência: excluir um pai com filhos remanescentes falha com `IntegrityError` (falha segura).
Toda nova tabela filha exige atualizar a enumeração manual e/ou o cascade ORM (seção G).

### B.6 Armazenamento de arquivos [CÓDIGO]

- Os bytes das planilhas dos clientes e das imagens do Guia ficam em `arquivo_armazenado.conteudo`
  (BYTEA), via `storage.py`: `salvar` (sem commit; sobrescreve a mesma chave), `obter`, `resolver`,
  `materializar_para_etl` (cópia privada 0600 com limite de bytes), `remover`, `url_midia`.
- Referências `db:<chave>` em `upload_relatorio.caminho_arquivo` e `guia_topico.imagem_url` não têm
  FK: o banco não impede órfãos nem referências quebradas.
- Referências **legadas** (sem prefixo `db:`) apontam para disco (`UPLOAD_FOLDER` ou `static/`).
  O disco do Render é efêmero; esses arquivos **não entram** num dump do banco.
  `migrar_arquivos_para_db.py` copia legados para o banco (dry-run por padrão, `--apply` grava).
- Não há cache local dos arquivos do banco: `storage.resolver()` recusa referência `db:`, e a cópia
  do ETL (`storage.materializar_para_etl()`) é exclusiva e apagada ao sair do contexto.
- `/midia/<chave>` exige login e só serve o prefixo `guia/` (`app.py:261-277`).

**Cobertura do backup [VALIDADO LOCALMENTE]:** o ensaio isolado comprovou que um `pg_dump` custom
preserva integralmente o BYTEA de `arquivo_armazenado`: bytes, tamanho, MIME e SHA-256 dos uploads
conferidos após a restauração, e ETL recalculado sobre os bytes restaurados (seção E).

### B.7 Estratégia de criação e atualização do esquema [CÓDIGO]

- Flask-Migrate/Alembic é a **única autoridade** de esquema em PostgreSQL: `Migrate()` sem argumentos
  (`extensions.py:29`), `migrate.init_app(app, db)` (`app.py:114`).
- `flask db upgrade` usa a engine da própria app. A URL direta (`DIRECT_URL`) só é usada com
  `NEXO_DB_DIRECT=1` (`app.py:93-97`). Se `DIRECT_URL` estiver vazia, `config.py:72-75` cai na
  `DATABASE_URL`, o que só é recusado em produção (`app.py:83-84`). Nada impede, tecnicamente, migrar
  pela `DATABASE_URL` (pooler) se `NEXO_DB_DIRECT` for esquecida (qualquer modo) ou, fora de produção,
  se `DIRECT_URL` estiver vazia.
- O build do Render **não** migra (`render.yaml:15-17`). O procedimento manual está em
  `Docs/PREPARACAO_RENDER.md:52-88`.
- `db.create_all()` só aparece em `init_db.py` (SQLite) e em suítes de teste de autenticação
  (`tests/test_auth_tokens_security.py`, `tests/postgresql_auth_tokens.py`). Não deve substituir
  migrações.
- Listener global de conexão aplica `PRAGMA foreign_keys=ON` em SQLite (`app.py:31-57`).

### B.8 Tabelas: código × migrações × produção

| Tabela | Descrita no código | Prevista nas migrações | Existente em produção |
|---|---|---|---|
| 14 tabelas da aplicação (B.2) | Sim | Sim | **[PENDENTE NO SUPABASE REAL]** — não comprovado |
| `alembic_version` | Não (Alembic) | Implícita (criada pelo Alembic) | **[PENDENTE]** — e qual revisão registra |
| `avaliacao_analise`, `fatura_cobranca` | Não (só citadas em docstring) | Não | **[PENDENTE]** — só existem no `.sql` histórico; verificar se alguém o executou |
| Objetos gerenciados pelo Supabase (`auth`, `storage`, `realtime`, `extensions`, `graphql`, `vault`…) | Não | Não | **[PENDENTE]** — o NEXO não os usa |

Os modelos e as migrações coincidem (seção C.4), mas isso **não** valida a estrutura de produção.

---

## C. Estado das migrações

### C.1 Cadeia e head

[CÓDIGO] e [VALIDADO LOCALMENTE] (`ScriptDirectory` offline: `heads = ['e4c004a26b01']`,
`bases = ['d87a6fbc2fbe']`; e `alembic_version = e4c004a26b01` no PostgreSQL local migrado):

```
d87a6fbc2fbe (raiz: schema inicial, 12 tabelas)
└─ c7189696a9cd (indicador_analise.periodo_base_inicio/fim)
   └─ f958d38a7f19 (relatorio_analise.gerado_por_ia)
      └─ 91a2ecf56a41 (usuario.primeiro_acesso)
         └─ d61b5e68082c (guia_topico)
            └─ a3f1c9b27d04 (empresa.deletado_em)
               └─ b7c4e2a9f031 (arquivo_armazenado)
                  └─ e4c004a26b01 (usuario.geracao_autorizacao + UPDATE de dados)  ← HEAD
```

- 8 revisões, cadeia **linear**, raiz única, **head única `e4c004a26b01`** (confirmada como head).
- Sem revisões duplicadas, sem merges, sem `branch_labels` nem `depends_on`.
- **[PENDENTE NO SUPABASE REAL]** A revisão aplicada em produção não está confirmada
  (`Docs/RELATORIO_NEXO_SEC005_ETL_SEGURO.md:16-17`). O código atual mapeia
  `usuario.geracao_autorizacao`, criada só pela head: se produção estiver em `b7c4e2a9f031`, as
  consultas a `Usuario` falham com o código atual.

### C.2 Infraestrutura de migração (`migrations/env.py`) [CÓDIGO]

| Aspecto | Comportamento | Implicação |
|---|---|---|
| Engine | Usa a engine da app (`get_engine()`); exige contexto Flask | URL decidida por `NEXO_DB_DIRECT`/`DIRECT_URL` |
| URL no Config | `render_as_string(hide_password=False)` (`env.py:27-32`), gravada no Config do Alembic por `config.set_main_option` (`env.py:39`) | Senha em memória; o modo online (`env.py:97`) não precisa dela |
| `compare_type` / `render_as_batch` | Defaults do Flask-Migrate 4.0.7 (ligados) | Autogenerate emite `batch_alter_table` |
| `compare_server_default` | Desligado | Mudança de `server_default` não é detectada |
| `transaction_per_migration` | Desligado | No PostgreSQL, todas as revisões pendentes rodam numa única transação e acumulam locks |
| `lock_timeout` / `statement_timeout` | Ausentes | Um `ALTER` pode ficar na fila e bloquear a aplicação |
| Logging | `fileConfig` reconfigura o logging global | Rodar migrações via CLI em processo separado |
| Depreciação | `get_engine` será removido no Flask-SQLAlchemy 3.2 (`env.py:21`); o aviso aparece nos logs de `tests.test_company_restore_migration` e `tests.postgresql_company_restore_migration` (em `tests.test_deploy` e no ensaio de backup o stderr é redirecionado) | Hoje fixado em 3.1.1 (`requirements.txt`); tratar antes de atualizar |

### C.3 Mapa das migrações

| Revisão | Anterior | Descrição | Tabelas | Alterações | Atualiza dados? | Reversão (downgrade) | Riscos | Coerente com `models.py` |
|---|---|---|---|---|---|---|---|---|
| `d87a6fbc2fbe` | — | Esquema inicial | 12 tabelas (todas, exceto `guia_topico` e `arquivo_armazenado`) | `create_table` ×12; 4 índices; 19 CHECK (2 nomeados); 8 UNIQUE; 16 FK sem nome e sem ON DELETE; `server_default CURRENT_TIMESTAMP` nas colunas de criação/registro (`data_criacao`, `data_abertura`, `data_envio`, `data_geracao`, `data_upload`) | Não | **Destrutiva total**: remove as 12 tabelas | Constraints anônimas com nomes que dependem do banco | Sim |
| `c7189696a9cd` | `d87a6fbc2fbe` | Período da base | `indicador_analise` | `add_column` ×2 (`Date`, anuláveis) | Não | Remove 2 colunas (perde valores) | Baixo | Sim |
| `f958d38a7f19` | `c7189696a9cd` | `gerado_por_ia` | `relatorio_analise` | `add_column` Boolean NOT NULL `server_default 'false'` | Implícito (linhas existentes recebem o default) | Remove a coluna | `'false'` vira texto no SQLite | Sim |
| `91a2ecf56a41` | `f958d38a7f19` | `primeiro_acesso` | `usuario` | `add_column` Boolean NOT NULL `server_default 'false'` | Implícito (todos `false` no PostgreSQL; no SQLite o literal `'false'` é gravado como texto, como em `f958d38a7f19`) | Remove a coluna; um re-upgrade zera a troca obrigatória de senha | Downgrade em SQLite recria `usuario` (FK/CHECK) | Sim |
| `d61b5e68082c` | `91a2ecf56a41` | CMS do Guia | `guia_topico` | `create_table` + `ix_guia_topico_categoria`; `ativo` DEFAULT true | Não | Remove a tabela (perde o conteúdo do CMS) | Baixo | Sim |
| `a3f1c9b27d04` | `d61b5e68082c` | Soft delete | `empresa` | `add_column` `deletado_em` (anulável) | Não | Remove a coluna: empresas da lixeira voltam a parecer ativas | Semântica de segurança | Sim |
| `b7c4e2a9f031` | `a3f1c9b27d04` | Arquivos no banco | `arquivo_armazenado` | `create_table` (BYTEA) | Não | **Remove todos os arquivos**; referências `db:` ficam órfãs | Crescimento do banco | Sim |
| `e4c004a26b01` (HEAD) | `b7c4e2a9f031` | SEC-004: geração de autorização | `usuario` (lê `empresa`) | `add_column` Integer NOT NULL DEFAULT 0 com CHECK de coluna nomeado; `UPDATE` que define `ativo=false` e `geracao_autorizacao=1` para CLIENTE de empresas na lixeira | **Sim, UPDATE explícito** | Remove a coluna e o histórico de revogação; **não restaura `ativo`** | Lock ACCESS EXCLUSIVE; exige janela com escritores parados (docstring `:6-13`) | Sim |

A última coluna indica que as operações da revisão coincidem com as definições atuais de `models.py`;
um banco parado numa revisão anterior à head **não** atende o código atual (C.1).

**Conclusões:** nenhum downgrade é rollback seguro em produção; o caminho de retorno é restaurar
backup verificado (`Docs/OPERACAO_BACKUP_RESTAURACAO.md:229-240`). A `e4c004a26b01` é a única com
atualização explícita de dados e é irreversível quanto a esses dados.

### C.4 Compatibilidade modelos × migrações

- **[VALIDADO LOCALMENTE]** `tests/test_deploy.py::test_migrations_empty_database_repeat_upgrade_and_metadata`
  aprovado: SQLite vazio migrado até a head, upgrade repetido sem efeito, conjunto de tabelas igual
  ao metadata e `compare_metadata == []`.
- **[VALIDADO LOCALMENTE]** No PostgreSQL 16.15 local, as migrações produzem as 14 tabelas +
  `alembic_version`, e o ensaio de backup confirma tabelas e revisão iguais à head.
- **[CÓDIGO]** Comparação estática coluna a coluna: 14 tabelas, 142 colunas, tipos, nulidade, 20 CHECK
  com texto idêntico, 8 UNIQUE, 16 FK e 5 índices coincidem. Diferenças só de forma:
  - D1: `geracao_autorizacao` usa `server_default=sa.text("0")` na migração e `"0"` no modelo
    (equivalentes);
  - D2: `ck_usuario_geracao_autorizacao` é CHECK de coluna na migração e de tabela no modelo (mesmo
    objeto no PostgreSQL; DDL diferente no SQLite).
- **Lacunas da verificação atual** [CÓDIGO]: o `compare_metadata` roda **só em SQLite** e só com
  `compare_type`. Não compara CHECK, `server_default`, nomes de constraints nem `ondelete` em
  PostgreSQL. Uma mudança em `nome_plano IN (...)` no modelo sem migração passaria no teste. Só a
  transição `b7c4e2a9f031 → e4c004a26b01` tem teste com dados pré-existentes.

### C.5 Riscos das migrações

| ID | Risco | Tipo | Gravidade |
|---|---|---|---|
| M1 | Revisão aplicada em produção desconhecida; `upgrade head` às cegas aplicaria a SEC-004 junto com revisões novas | Potencial | Alta |
| M2 | Upgrade da head sobrescreve `ativo` de forma irreversível | Comprovado | Alta |
| M3 | Todos os downgrades destroem dados | Comprovado | Alta |
| M4 | Constraints sem nome: alterar CHECK de plano/status exige o nome real no PostgreSQL e recriação de tabela no SQLite | Comprovado | Média |
| M5 | Batch recreate no SQLite descarta CHECK anônimo e tende a falhar com `foreign_keys=ON` em tabela pai | Potencial | Média |
| M6 | Sem `lock_timeout`/`statement_timeout`; todas as revisões numa transação | Potencial | Média |
| M7 | Paridade verificada só em SQLite; CHECK/defaults não comparados | Comprovado | Média |
| M8 | SQLAlchemy e Alembic sem pin direto (localmente resolveram para 2.1.6 e 1.20.1) | Comprovado | Baixa |
| M9 | Sem `NEXO_DB_DIRECT=1`, `flask db upgrade` usaria a `DATABASE_URL` (pooler) | Potencial | Baixa |

---

## D. Sistema de backup existente

### D.1 Componentes analisados

| Componente | Função | Acessa banco externo? |
|---|---|---|
| `Docs/OPERACAO_BACKUP_RESTAURACAO.md` | Roteiro de backup lógico, criptografia, restauração isolada e rollback | n/a (documento) |
| `scripts/verificar_prontidao_backup.py` | Verifica ferramentas (`--version`), cabeçalho `PGDMP`, SHA-256 e `pg_restore --list` de um arquivo local | **Não**: não importa a app, não lê `.env`, ambiente filho filtrado (`PGPASSFILE`/`PGSERVICEFILE` = `/dev/null`) |
| `tests/test_backup_readiness.py` | 13 testes do verificador, com `subprocess` simulado | **Não** |
| `tests/test_backup_restore.py` | Ensaio PostgreSQL completo (opt-in via `NEXO_TEST_POSTGRES_BIN`) + 4 testes de segurança do ensaio | **Não**: cluster próprio em `127.0.0.1`, porta efêmera, sem socket Unix, SCRAM com usuário/senha aleatórios; ambiente filtrado sem `DATABASE_URL`/`DIRECT_URL`/`PG*` herdados (define apenas `PG*` sintéticos do próprio cluster, `:392-394`); recusa URL em `NEXO_TEST_POSTGRES_BIN` |
| `tests/postgresql_support.py` | Cluster privado (socket Unix 0700, sem TCP) das suítes `postgresql_*` | **Não**: recusa DSN; `DROP DATABASE` só no namespace `nexo_security_test_*` |
| CI (`nexo-security-ci.yml`) | Executa o ensaio no job `security-sqlite-browser` a cada PR e push na `main` | **Não** (variáveis sintéticas) |

Os mecanismos estão corretos e **foram preservados sem alteração** nesta etapa.

### D.2 Respostas às verificações da Subetapa 0.2.4

| # | Verificação | Resultado |
|---|---|---|
| 1 | Planejamento | Dump lógico `--format=custom`, cifrado, fora do GitHub e do Render, restauração isolada; frequência diária e retenção 7/4/3 apenas **recomendadas, não implantadas** (`Docs/OPERACAO_BACKUP_RESTAURACAO.md:136, 142`). Não há backup automatizado no repositório (CI sem `schedule`) |
| 2 | Ferramentas | `pg_dump`, `pg_restore`, `psql`; no ensaio também `initdb`, `pg_ctl`, `createdb`; SHA-256 via `hashlib`; `gpg` documentado |
| 3 | Acesso externo | Nenhum script ou teste auditado acessa banco externo (D.1) |
| 4 | Segurança das operações | Ensaio sem `DROP`, sem `--clean`/`--create`; restore em banco novo com `--single-transaction --exit-on-error --no-owner --no-privileges`; diretórios temporários; credenciais sintéticas 0600 |
| 5 | Verificação do arquivo | Tamanho > 0, cabeçalho `PGDMP`, `pg_restore --list` com `TABLE DATA public <tabela>` para cada tabela e `alembic_version`; arquivo truncado é rejeitado |
| 6 | Integridade | Todas as linhas e colunas (incluindo BYTEA) iguais entre origem e destino; tabelas = metadata + `alembic_version`; sequências acima do máximo; FK aplicada (inserção inválida falha) |
| 7 | Restauração de teste | `initdb` → `pg_ctl start` → `createdb` origem/destino → migrações reais → dados sintéticos → `pg_dump` → SHA-256 → TOC → CLI de prontidão → `pg_restore` → validação → `pg_ctl stop` → remoção do diretório |
| 8 | Hashes | SHA-256 do dump (gerado, conferido, recalculado após o restore) e dos uploads (`hash_arquivo`) |
| 9 | Arquivos dos clientes | BYTEA entra no dump e é conferido byte a byte; **legados em disco ficam fora** |
| 10 | Exclusão/sobrescrita | Risco baixo no mecanismo; restauração nunca sobre produção (proibição explícita no roteiro) |

### D.3 Ensaio isolado × 12 requisitos da Subetapa 0.2.6

| # | Requisito | Atendido | Evidência |
|---|---|---|---|
| 1 | Banco fictício | Sim | `initdb` + `createdb nexo_restore_source` (`tests/test_backup_restore.py:395-410`) |
| 2 | Migrações versionadas | Sim | `flask_migrate.upgrade` (`:423`), após verificação da engine privada (`:422`) |
| 3 | Dados de teste | Sim | `fixture()` (`:213-279`): plano, segmento, 2 empresas, admin, 2 clientes, 2 análises, 4 uploads BYTEA, chamados, notificações, guia |
| 4 | Backup | Sim | `pg_dump --format=custom` (`:430-433`) |
| 5 | Integridade | Sim | `PGDMP`, SHA-256, TOC, CLI de prontidão, rejeição de truncado (`:434-455`) |
| 6 | Destino separado | Sim | `nexo_restore_target` e app separada (`:409-410, 460-461`) |
| 7 | Restauração | Sim | `pg_restore --single-transaction --exit-on-error --no-owner --no-privileges` (`:457-458`) |
| 8 | Tabelas | Sim | Conjunto igual e 15 tabelas (`:293, 97`) |
| 9 | Relacionamentos | **Parcial** | Relações ORM, FK negativa e referências `db:` dos registros sintéticos (`:297-324, 349-354`); não compara estruturalmente FK/UNIQUE/CHECK/índices entre origem e destino |
| 10 | Integridade dos dados | Sim | Snapshot integral, hashes, ETL, sequências, rotas (`:296-370`) |
| 11 | Revisão Alembic | Sim | Igual à origem e à head dos scripts (`:294-295`) |
| 12 | Encerramento e limpeza | Sim | `pg_ctl stop` no worker e no pai; `TemporaryDirectory` (`:66, 77-88, 470-478`) — confirmado nesta execução: nenhum processo `postgres` nem diretório temporário remanescente |

Conforme exigido, **não foi criado mecanismo paralelo**: a infraestrutura existente atende aos
requisitos. Melhorias recomendadas (não aplicadas): comparação estrutural de constraints entre origem
e destino; fixture com empresa na lixeira, usuário inativo, `geracao_autorizacao > 0` e referência
legada; verificação da identidade do servidor após `pg_ctl start`; criação dos arquivos de credencial
sintética já com 0600.

### D.4 O que um dump do banco NÃO protege

| Item | Consequência |
|---|---|
| Arquivos legados em disco (referências sem `db:`) | Não estão no dump nem no Git; o disco do Render é efêmero |
| Segredos e variáveis do Render (`SECRET_KEY` gerada pelo Render, `DATABASE_URL`, `DIRECT_URL`, `BREVO_API_KEY` e, se existirem, `ANTHROPIC_API_KEY`/`HF_API_TOKEN`) | Precisam de inventário privado próprio. Perder a `SECRET_KEY` invalida sessões e tokens em trânsito, sem perda de dados (senhas usam hash próprio); vazá-la exige rotação |
| Roles globais e senhas | `pg_dump` não exporta roles ([backup-dump](https://www.postgresql.org/docs/current/backup-dump.html)) |
| Configuração do projeto Supabase (Data API, Exposed schemas, Auth, SSL enforcement, PITR, pooler) | Fica no painel; não está no dump |
| Objetos do Supabase Storage | Não usados pelo NEXO; backups do Supabase não incluem os objetos ([backups](https://supabase.com/docs/guides/platform/backups)) |
| Logs, cache, estado em memória (rate limit, Socket.IO) | Regeneráveis; não são fonte de recuperação |
| Contas externas (Brevo, Anthropic, Hugging Face) e configuração do GitHub | Fora do banco |

### D.5 Correções documentais identificadas (não aplicadas nesta etapa)

O roteiro existente está correto em método, mas contém fatos desatualizados. Para não alterar
mecanismos sem necessidade, as correções ficam registradas aqui e devem ser aplicadas em PR própria:

| Local | Desatualização | Correção |
|---|---|---|
| `Docs/OPERACAO_BACKUP_RESTAURACAO.md:16` | "sete revisões" e head `b7c4e2a9f031` | 8 revisões, head `e4c004a26b01` |
| `Docs/OPERACAO_BACKUP_RESTAURACAO.md` (seção de rollback) | Não cita que o downgrade da head perde o histórico de revogação e não restaura `ativo` | Incluir a ressalva da `e4c004a26b01` |
| `Docs/OPERACAO_BACKUP_RESTAURACAO.md:29` | Inventário de `usuario` não cita `geracao_autorizacao` (criada pela `e4c004a26b01`) | Incluir a geração de autorização entre os dados que devem sobreviver |
| `Docs/OPERACAO_BACKUP_RESTAURACAO.md:259` | Contém caminho local de máquina com nome de usuário do sistema operacional | Substituir por marcador genérico |
| `Docs/AUDITORIA_RENDER.md:76` (e `:17`, `:27`) | "Cadeia única de 7 revisões; head `b7c4e2a9f031`" | 8 revisões, head `e4c004a26b01` |
| `Docs/OPERACAO_BACKUP_RESTAURACAO.md:98` | Admite a exportação complementar limitada ao schema da aplicação sem avisar que `pg_dump --schema=public` inclui `CREATE SCHEMA public` e falha ao ser restaurado num banco novo com `--exit-on-error` | Registrar a preparação do destino validada na E.2, item 12 (`--use-list` sem a entrada `SCHEMA - public`) |
| `Docs/CI_SEGURANCA.md:48` | Afirma, sem distinguir os jobs, que a versão do PostgreSQL é registrada; só o job `security-postgresql` executa `postgres --version` (`nexo-security-ci.yml:82`); o job `security-sqlite-browser`, que roda o ensaio de backup, não a imprime | Registrar `postgres --version` também no job `security-sqlite-browser` ou restringir a frase ao job `security-postgresql` |

---

## E. Resultados dos testes

### E.1 Ambiente de execução [VALIDADO LOCALMENTE]

| Item | Valor |
|---|---|
| Sistema | Container Linux isolado, sem acesso a Supabase/Render; sem `.env`; nenhuma variável `DATABASE_URL`, `DIRECT_URL`, `SECRET_KEY` ou `PG*` definida |
| Python | 3.12.3, em virtualenv **fora do repositório** (Render fixa 3.12.10; CI usa 3.12 flutuante) |
| Dependências | Instaladas pelo instalador verificado do próprio projeto: `python -I tools/install_dependencies.py --requirements requirements-dev.txt` (pip 26.2.2 com SHA-256 conferido; `pip check` sem conflitos). Nenhum arquivo de dependência foi alterado |
| Versões transitivas resolvidas | SQLAlchemy 2.1.6, Alembic 1.20.1 (sem pin no repositório) |
| PostgreSQL | 16.15 (pacote Ubuntu): `pg_dump`, `pg_restore`, `psql`, `initdb`, `pg_ctl` 16.15. O cluster de sistema do pacote permaneceu parado e não foi usado |
| Usuário | Testes PostgreSQL executados como usuário de sistema não-root (`initdb` recusa root), com `env -i` (ambiente limpo) |

### E.2 Comandos executados e resultados

| # | Comando | Resultado |
|---|---|---|
| 1 | `python -B scripts/verificar_prontidao_backup.py --dry-run` | **Código 0.** `pg_dump`, `pg_restore`, `psql` localizados; nenhum subprocesso; "Restauração: não executada" |
| 2 | `python -B scripts/verificar_prontidao_backup.py` | **Código 0.** As três ferramentas responderam a `--version` |
| 3 | `python -B -m unittest tests.test_backup_readiness -v` | **13/13 aprovados** |
| 4 | `NEXO_TEST_POSTGRES_BIN=/usr/lib/postgresql/16/bin python -B -m unittest tests.test_backup_restore -v` | **5/5 aprovados** (6,1 s). Relatório do ensaio: `status: ok`, `tables: 15`, `uploads: 4`, `files: 5`, `production_access: false`, 17 verificações (`migrations`, `custom_dump`, `sha256`, `pg_restore_list`, `readiness_cli`, `invalid_archive_rejected`, `restore`, `all_rows`, `relationships`, `binary_files`, `sequences`, `foreign_keys`, `etl`, `health`, `csrf_login`, `dashboard`, `company_isolation`) |
| 5 | `python -B -m unittest tests.test_deploy tests.test_company_restore_migration -v` | **18/18 aprovados** (SQLite: paridade modelos × migrações, seed idempotente, transição SEC-004 com dados sintéticos) |
| 6 | `NEXO_TEST_POSTGRES_BIN=... python -B -m unittest tests.postgresql_company_restore_migration -v` | **6/6 aprovados** (PostgreSQL privado, incluindo falha atômica: coluna, dados e revisão revertidos juntos) |
| 7 | `NEXO_TEST_POSTGRES_BIN=... python -B -m unittest discover -s tests -t . -v` (mesmo comando do job 1 do CI, `nexo-security-ci.yml:60`, mas sem navegador, sem as variáveis sintéticas do workflow (`:17-29`) e com usuário não-root que não é dono do checkout) | 531 executados: **518 aprovados, 11 ignorados, 2 falhas ambientais** (ver E.3). O ensaio de backup foi executado novamente dentro da suíte e aprovado |
| 8 | `python -B -m unittest tests.test_configuration_deploy -v` (como dono do checkout) | **10/10 aprovados**, incluindo os 2 que falharam no item 7 |
| 9 | `NEXO_TEST_POSTGRES_BIN=... python -B -m unittest tests.postgresql_auth_tokens tests.postgresql_session_revocation tests.postgresql_company_restore_security tests.postgresql_company_restore_migration tests.postgresql_etl_resource_security -v` (job 2 do CI) | **108/108 aprovados** |
| 10 | Inspeção de catálogo: migrações aplicadas em PostgreSQL privado vazio (reutilizando `tests/postgresql_support.PrivatePostgreSQL`, script auxiliar fora do repositório) e leitura de `pg_class`, `pg_constraint`, `pg_indexes`, `pg_sequences`, `information_schema.columns` | `alembic_version = e4c004a26b01`; 15 tabelas; nomes de constraints da seção B.3; 16 FK `NO ACTION`; RLS desligada em todas; banco removido e cluster encerrado ao final |
| 11 | `ScriptDirectory('migrations')` (Alembic offline, sem banco) | `heads = ['e4c004a26b01']`, `bases = ['d87a6fbc2fbe']`, 8 revisões lineares |
| 12 | Ensaio de escopo (script auxiliar fora do repositório, reutilizando `tests/postgresql_support.PrivatePostgreSQL`; PostgreSQL 16.15 privado, migrações reais, 1 segmento sintético): `pg_dump --format=custom --schema=public` e `pg_dump --format=custom` sem filtro, cada um restaurado com `pg_restore --no-owner --no-privileges --single-transaction --exit-on-error` em banco novo | Dump com `--schema=public`: o TOC contém a entrada `SCHEMA - public` (o dump sem filtro não a contém). **Restauração num banco novo: código 1**, `ERROR: schema "public" already exists`; a transação inteira foi desfeita. **Funcionaram (código 0, 15 tabelas, `alembic_version = e4c004a26b01`):** (a) `pg_restore --use-list` com o TOC sem a entrada `SCHEMA - public`; (b) `DROP SCHEMA public` no banco de destino recém-criado e vazio antes do `pg_restore`. O dump sem filtro restaurou num banco novo com código 0 e 15 tabelas. Bancos removidos e cluster encerrado |
| 13 | Ensaio dos Apêndices A e B (script auxiliar fora do repositório; PostgreSQL 16.15 privado, migrações reais, dados sintéticos): o bloco bash do Apêndice B executado como está no documento, com os placeholders trocados por caminhos do cluster descartável (service file + passfile 0600 pelo socket local, sem TLS; chave GnuPG efêmera); sessão `REPEATABLE READ READ ONLY` com `pg_export_snapshot()` e A-14 mantida aberta durante os dumps, com uma inserção sintética concorrente feita depois do snapshot; o bloco SQL do Apêndice A executado via `psql` dentro do envoltório READ ONLY, na origem e na cópia | **Código 0** em todo o Apêndice B: artefatos B e A gerados, SHA-256 conferidos, TOC listado, CLI de prontidão aprovada nos dois, cifragem, descriptografia de teste com hash igual ao original e restauração do artefato A com `--use-list` (15 tabelas, `alembic_version = e4c004a26b01`). Contagens A-14 (15 linhas) da cópia **iguais às do snapshot**; a inserção concorrente ficou fora da cópia (origem com 2 segmentos, cópia com 1). Apêndice A: **código 0** na origem e na cópia, sem erros, nas 20 consultas; as roles do Supabase não existem localmente, por isso A-3, A-9, A-9b, A-9c e A-10 só tiveram a sintaxe e o papel `public` conferidos. Bancos removidos; cluster e `gpg-agent` encerrados |

**Consolidado local no commit `c919b1d`:** 531 testes gerais. Na execução do item 7: 518 aprovados,
11 ignorados e 2 reprovados por causa ambiental; esses 2 foram aprovados na reexecução separada do
item 8, o que dá 520 aprovados **somando as duas execuções**. Nenhuma falha é atribuível ao código.
108/108 testes PostgreSQL (item 9). A quantidade de testes (531 e 108) coincide com a registrada na
SEC-005 (`Docs/RELATORIO_NEXO_SEC005_ETL_SEGURO.md:245-253`); lá, porém, os 531 foram aprovados com
Chromium habilitado (0 falhas, erros ou ignorados), enquanto aqui 11 foram ignorados por falta de
navegador e 2 só passaram na reexecução do item 8.

### E.3 Testes reprovados, não executados e justificativas

| Item | Situação | Justificativa |
|---|---|---|
| `tests.test_configuration_deploy.ConfigurationTests.test_development_import_without_production_secret` e `tests.test_configuration_deploy.ConfigurationTests.test_wsgi_production_import_and_liveness_without_database_connection` | Reprovados no item 7, **aprovados** no item 8 | Causa ambiental comprovada: com o usuário não-root, a app não pôde criar `<repo>/instance` (checkout pertencente a outro usuário) — `PermissionError` em `app.py:105`. No CI, o checkout pertence ao próprio usuário do runner (inferência coerente com os 531 aprovados registrados na SEC-005). Não é defeito do código |
| 11 testes `test_nexobot_navegador` | Não executados (ignorados) | Exigem `NEXO_TEST_BROWSER=1` e navegador Chromium do Playwright; fora do escopo de banco desta etapa. O CI os executa |
| Qualquer verificação no Supabase real | Não executada | Proibida nesta etapa; exige autorização e credenciais (seções F e I) |
| Backup real, restauração de backup real | Não executados | Idem |
| Restauração de um dump completo de banco Supabase (artefato B de F.2) | Não ensaiada | O cluster local não tem os schemas, roles e extensões gerenciados do Supabase; o ensaio exige ambiente compatível (F.2, passo 11) |

**RESTAURAÇÃO LOCAL EXECUTADA COM SUCESSO** (dados sintéticos, PostgreSQL 16.15). Isso demonstra a
**capacidade técnica** de backup e restauração do esquema do NEXO nessa major, mas **não** comprova
que o banco de produção esteja protegido nem que o procedimento funcione na major do servidor real,
que ainda não foi confirmada (F.1, implicação 2; I.4, item 1). A proteção operacional só poderá ser
declarada após um backup real autorizado e uma restauração isolada validada na mesma major (seção J).

### E.4 Limpeza do ambiente

Nenhum processo `postgres` nem diretório temporário dos ensaios permaneceu ativo (conferido também
após os itens 12 e 13). Os diretórios `.audit_tmp/` e `instance/` criados pelos testes, ignorados
pelo Git, foram removidos ao final. Os scripts auxiliares e o virtualenv ficaram fora do repositório.

---

## F. Estratégia de proteção do Supabase

### F.1 Fatos da documentação oficial consultada em 2026-10-24 [DOC]

| Tema | Fato | Fonte |
|---|---|---|
| Backups gerenciados | Backup diário automático só nos planos Pro (7 dias), Team (14) e Enterprise; **Free: não incluído** — a recomendação é exportar regularmente e manter cópia off-site | [backups](https://supabase.com/docs/guides/platform/backups); [pricing](https://supabase.com/pricing) |
| PITR | Add-on dos planos pagos; exige compute Small ou maior; ao ativar, os backups diários deixam de ser feitos | [backups](https://supabase.com/docs/guides/platform/backups) |
| Backup físico | Projetos em Postgres 15.8.1.079 ou mais novo usam backup físico; o arquivo lógico baixável deixa de ser gerado; alternativa: `supabase db dump` ou `pg_dump` | [backups](https://supabase.com/docs/guides/platform/backups); [download-logical-backups](https://supabase.com/docs/guides/troubleshooting/download-logical-backups) |
| Conexão para `pg_dump` | Conexão direta (`db.<ref>.supabase.co:5432`, IPv6, ou IPv4 com add-on) recomendada para "pg_dump, backup and restore"; em rede só IPv4 sem add-on, Session pooler (Supavisor, porta 5432, usuário `postgres.<ref>`), que o guia de migração recomenda para pg_dump/restore; o Transaction pooler (6543) perde o estado de sessão entre transações, e por isso este documento o descarta para o backup (inferência, não proibição explícita da fonte) | [connecting-to-postgres](https://supabase.com/docs/guides/database/connecting-to-postgres); [migrating-to-supabase/postgres](https://supabase.com/docs/guides/platform/migrating-to-supabase/postgres) |
| Compatibilidade | `pg_dump` não faz dump de servidor de major mais nova que a sua; a saída não tem garantia de carga em major mais antiga | [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html) |
| Limite do dump lógico | Fora de casos simples, o `pg_dump` geralmente não é a escolha certa para backups regulares de produção (Capítulo 25 da documentação). No NEXO, no plano Free, é o único mecanismo acessível; isso reforça avaliar plano pago/PITR (I.5, item 1) | [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html) |
| Dump de schema específico | `-n/--schema` seleciona o próprio schema e todo o seu conteúdo; não há garantia de que um dump de schema específico se restaure sozinho num banco limpo. Comportamento confirmado localmente na E.2, item 12 | [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html) |
| Versão no Supabase | Postgres 17 é o padrão atual da plataforma (afirmação feita no changelog 46080, que trata do self-hosted); projetos hospedados em Postgres 15 seguem suportados até o fim de vida do PG 15 na plataforma, sem data publicada | [changelog 46080](https://supabase.com/changelog/46080-self-hosted-supabase-upgrading-from-pg-15-to-17-breaking-change); [upgrading](https://supabase.com/docs/guides/platform/upgrading) |
| Credenciais | `.pgpass` (0600) ou service file; `PGPASSWORD` não recomendado | [libpq-pgpass](https://www.postgresql.org/docs/current/libpq-pgpass.html); [libpq-envars](https://www.postgresql.org/docs/current/libpq-envars.html) |
| TLS | Mínimo `sslmode=require`; para verificar o servidor, `verify-full` com o certificado raiz do painel | [connecting-to-postgres](https://supabase.com/docs/guides/database/connecting-to-postgres); [libpq-ssl](https://www.postgresql.org/docs/current/libpq-ssl.html) |
| CLI do Supabase | `supabase db dump` roda `pg_dump` em contêiner e ignora `auth`, `storage` e os schemas criados por extensões; a conexão vem do projeto vinculado (`supabase link` + `--linked`, senha por `-p/--password`) ou de `--db-url` com string percent-encoded. As duas formas põem a senha em argumento de processo, o que contraria F.2, passo 6 | [supabase-db-dump](https://supabase.com/docs/reference/cli/supabase-db-dump) |
| Dumps em texto plano | CVE-2025-8714 (injeção via meta-comandos do psql) corrigido em 17.6, 16.10, 15.14 | [CVE-2025-8714](https://www.postgresql.org/support/security/CVE-2025-8714/) |
| Restauração | Restaurar um dump faz o destino executar código arbitrário escolhido pelos superusuários da origem, e dumps ou restaurações parciais não limitam isso; se eles não forem confiáveis, inspecionar o SQL antes (`pg_restore --file=<saida>.sql <dump>`). Restaurar só em ambiente isolado é decisão deste documento | [pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html) |
| Data API | O schema `public` é exposto na Data API por padrão; tabela em schema exposto sem RLS é legível e gravável por qualquer role com grant; lint `0013_rls_disabled_in_public` (ERROR); alternativa: remover `public` dos Exposed schemas ou desligar a Data API | [using-custom-schemas](https://supabase.com/docs/guides/api/using-custom-schemas); [row-level-security](https://supabase.com/docs/guides/database/postgres/row-level-security); [database-advisors](https://supabase.com/docs/guides/database/database-advisors); [hardening-data-api](https://supabase.com/docs/guides/database/hardening-data-api) |
| Mudança anunciada | Tabelas novas deixam de ser expostas automaticamente na Data API; aplicação a projetos existentes anunciada para 2026-10-30; **tabelas existentes mantêm os grants atuais** | [changelog 45329](https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically) |

**Implicações para o NEXO:**

1. Flask-Migrate/Alembic continua a única autoridade de esquema: **não** introduzir
   `supabase migrations`, `supabase db push` nem o schema `supabase_migrations`.
2. O ambiente local desta etapa tem `pg_dump` 16.15. **Se o servidor do Supabase for Postgres 17, esse
   cliente não serve** para o backup real; será preciso `pg_dump`/`pg_restore`/`psql` 17.x (≥ 17.6).
3. **Não reaproveitar `DATABASE_URL`** (pooler da aplicação) para o backup; o SQLAlchemy também
   reescreve o esquema da URL para `postgresql+psycopg2`, inválido para libpq.
4. Se o plano do projeto for Free, o dump lógico cifrado e off-site é a **única** cópia de
   recuperação.

### F.2 Procedimento do backup real (preparado; NÃO executado)

Cada passo indica o estado da verificação.

| # | Passo | Como | Estado |
|---|---|---|---|
| 0 | Autorização | Registro escrito do proprietário: janela, responsável, destino do arquivo, custódia da chave de criptografia, política de retenção | **[PENDENTE]** |
| 1 | Identificar o banco correto | Copiar host, porta e usuário do painel **Connect** do projeto de produção (nunca de exemplos da documentação); confirmar o *project ref* com o proprietário; conferir que não é projeto de desenvolvimento | **[PENDENTE NO SUPABASE REAL]** |
| 2 | Identificar a versão | Sessão somente leitura: `SHOW server_version_num; SELECT version();` (Apêndice A, A-1) | **[PENDENTE]** — procedimento [DOC] |
| 3 | Verificar permissões | Consultas A-3/A-5 (role atual, `rolbypassrls`, dono das tabelas, RLS) | **[PENDENTE]** — consultas preparadas [CÓDIGO/DOC] |
| 4 | Definir o escopo | Inventário somente leitura dos schemas e de onde está `alembic_version` (A-4, A-15). Artefato **B (principal)**: dump do banco completo (`pg_dump -Fc`, sem `--schema`), conforme `Docs/OPERACAO_BACKUP_RESTAURACAO.md:37, 98`; se falhar por permissão ou por objetos gerenciados, o backup **não está aprovado** até o procedimento ser ajustado com o provedor, sem excluir `auth`, `storage` ou `realtime` e sem rebaixar privilégios. Artefato **A (complementar)**: `pg_dump -Fc --schema=public` (14 tabelas + `alembic_version`), para a restauração isolada da aplicação; não substitui B. Qualquer decisão do proprietário de aceitar só A é registrada por escrito como exceção ao roteiro e incluída na D.5. O proprietário aprova o escopo por escrito | Dump custom do banco local validado [VALIDADO LOCALMENTE]; `--schema=public` ensaiado localmente: exige preparar o destino (E.2, item 12; passo 11); escopo real **[PENDENTE]** |
| 5 | Ambiente seguro | Máquina controlada pelo responsável (não o Render, não o CI, nem qualquer ambiente compartilhado), disco cifrado, `umask 077`, diretório novo por execução fora do checkout; cliente PostgreSQL da **mesma major do servidor** (≥ 17.6 se o servidor for 17) | **[PENDENTE]** |
| 6 | Credenciais | Service file exclusivo do backup + `.pgpass` 0600 fora do repositório; `sslmode=verify-full` com certificado raiz do painel (validar no pooler; se falhar só no pooler, preferir conexão direta a rebaixar o TLS); nunca `PGPASSWORD`, senha em argumento ou `DATABASE_URL` da app | Mecanismo [DOC]; execução **[PENDENTE]** |
| 7 | Dump consistente | Para cada artefato: `pg_dump --dbname="service=<backup>" --no-password --format=custom [--schema=public] --file=<dir>/<nome>.dump.partial`, depois `mv -n` para o nome final; acrescentar `--snapshot=<id>` quando as contagens forem tiradas no mesmo snapshot (passo 12). O `pg_dump` exporta um snapshot consistente sem bloquear usuários | Formato custom, service file com passfile, `--snapshot` e `--schema=public` ensaiados em cluster local [VALIDADO LOCALMENTE] (E.2, itens 4, 12 e 13); TLS e conexão ao Supabase não ensaiados; real **[PENDENTE]** |
| 8 | Validar integridade | `sha256sum` → manifesto; `pg_restore --list`; `python -B scripts/verificar_prontidao_backup.py --backup <arquivo> --sha256 <hash> --list` | CLI validada no ensaio e nos dois artefatos do Apêndice B [VALIDADO LOCALMENTE] (E.2, itens 4 e 13); real **[PENDENTE]** |
| 9 | Proteger o arquivo | Cifrar imediatamente para chave pública verificada (`gpg --encrypt --recipient <fingerprint>`) ou `age -r`; hash também do arquivo cifrado; manter o arquivo em claro só no diretório privado até concluir a descriptografia de teste, a conferência do hash original e a restauração do passo 11; só então removê-lo, conforme a política aprovada (`Docs/OPERACAO_BACKUP_RESTAURACAO.md:132`) | [DOC]; comandos `gpg` ensaiados com chave efêmera local [VALIDADO LOCALMENTE] (E.2, item 13); chave e destino reais **[PENDENTE]** |
| 10 | Armazenar fora do GitHub | Destino privado, separado da infraestrutura de produção, com retenção definida e cópia independente; nunca no checkout (o `.gitignore` é rede de segurança, não mecanismo de sigilo) | **[PENDENTE]** — decisão do proprietário |
| 11 | Restaurar em ambiente isolado | PostgreSQL vazio, novo, da mesma major, sem rede para produção: `pg_restore --no-owner --no-privileges --single-transaction --exit-on-error`; depois `ANALYZE`. **Artefato A:** o dump contém `CREATE SCHEMA public` e falha num banco novo; restaurar com `--use-list` de um TOC sem a entrada `SCHEMA - public` (preferível, porque não executa DDL destrutivo) ou, só no banco recém-criado e vazio, `DROP SCHEMA public` sem `CASCADE` antes do `pg_restore`. **Artefato B:** exige ambiente compatível com os schemas, roles e extensões gerenciados do Supabase (`Docs/OPERACAO_BACKUP_RESTAURACAO.md:190, 202`); falha de extensão ou de permissão não é sucesso. Nunca sobre o Supabase, nunca `stamp`/`upgrade`/seed na cópia | Artefato A: restauração e as duas preparações do destino validadas [VALIDADO LOCALMENTE] (E.2, itens 4, 12 e 13); artefato B não ensaiado; real **[PENDENTE]** |
| 12 | Conferir os dados restaurados | Conjunto de tabelas igual ao inventariado na origem (esperado: 15); `alembic_version` registrada; contagens das 15 tabelas (A-14) iguais às da origem, tiradas **no mesmo snapshot do dump**: numa sessão `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT pg_export_snapshot();`, rodar A-14 e manter a sessão aberta enquanto o `pg_dump --snapshot=<id>` roda (conexão direta ou Session pooler), ou parar os escritores durante o dump. Na cópia restaurada: referências `db:` sem órfãos e SHA-256 dos uploads conferidos (A-7 a A-7d **apenas na cópia restaurada**; A-6 na origem ou na cópia); login e páginas da aplicação apontando para a cópia isolada | Método de snapshot ensaiado em cluster local [VALIDADO LOCALMENTE] (E.2, item 13); real **[PENDENTE]** |
| 13 | Registrar evidências | Data, versão do servidor e do cliente, SHA-256, tamanho, contagens agregadas, revisão Alembic, resultado da restauração, responsável — **sem** URLs, senhas, nomes de clientes ou amostras de dados; guardar o registro em local privado | **[PENDENTE]** |

### F.3 Requisitos para a operação real

- Autorização específica e registrada do proprietário (passo 0).
- Credenciais com o menor privilégio suficiente para leitura de todas as tabelas, fornecidas fora do
  repositório e revogáveis.
- Cliente PostgreSQL compatível com a versão confirmada do servidor.
- Destino cifrado aprovado e chave de criptografia sob custódia definida.
- Ambiente de restauração isolado disponível (PostgreSQL local ou projeto separado).
- Janela sem alterações de esquema em andamento.
- Proibições mantidas: nada de restauração sobre produção, alteração de configurações do projeto,
  exportação de dados para o repositório ou divulgação de evidências com dados sensíveis.

---

## G. Planejamento das migrations

### G.1 Padrão de evolução

1. **Criação de revisões:** gerar sempre em SQLite descartável sob `.audit_tmp/`, exportando
   explicitamente `FLASK_ENV=development`, `DATABASE_URL=sqlite:///<.audit_tmp/arquivo>.db`,
   `DIRECT_URL=` e `NEXO_DB_DIRECT=0` (o `config.py` carrega `.env` e usaria um `DATABASE_URL`
   configurado nele). Sequência: `flask --app app db upgrade` → editar `models.py` →
   `flask --app app db migrate -m "E<n>: <descrição>"` → **revisão manual obrigatória** → apagar o
   banco descartável. Revisões só de dados: `flask db revision`.
2. **Encadeamento:** `down_revision = 'e4c004a26b01'` (head atual) e head única. Nunca editar revisão
   já mergeada; corrigir para frente; sem `stamp`.
3. **Compatibilidade PostgreSQL:** tipos já usados (`Integer` PK, `String`, `Text`, `Numeric`,
   `DateTime`, `Boolean`); evitar `sa.Enum` (preferir `String` + CHECK nomeado); limites de texto via
   CHECK nomeado `length(col) <= n` e validação na aplicação; nomes ≤ 63 caracteres.
4. **Booleanos novos:** `server_default=sa.false()`/`sa.true()` com `op.add_column` **fora de batch**
   (o literal `'false'` vira texto no SQLite).
5. **Tabelas existentes:** somente `op.add_column` fora de batch (anulável ou com default constante,
   opcionalmente com CHECK de coluna nomeado, como fez a `e4c004a26b01`) e `create_index`. Nada de FK
   nova, alteração de CHECK, `alter_column`, rename ou drop em tabela existente na mesma release.
6. **FKs e constraints só em tabelas novas**, todas **nomeadas** (`ck_<tabela>_<regra>`,
   `uq_<tabela>_<colunas>`, `fk_<tabela>_<coluna>`, `ix_<tabela>_<colunas>`), no modelo **e** na
   migração; não usar `unique=True`/`index=True` em colunas novas.
7. **`naming_convention`:** **não** adotar globalmente nesta fase — faria os 6 UNIQUE implícitos
   ganharem nome no metadata, quebraria o `compare_metadata` e proporia recriação de tabelas pai no
   SQLite. A normalização de nomes legados fica para etapa própria, após inventário de `pg_constraint`
   em produção.
8. **Exclusões:** manter `NO ACTION` (falha segura com rollback). Filhos de análise via relationship
   com `cascade="all, delete-orphan"` em `Analise`; filhos de empresa/usuário por passo explícito na
   exclusão permanente (`blueprints/admin.py:523-612`). **Nenhum `ON DELETE CASCADE` nesta fase**
   (apagaria em silêncio dados novos e auditoria). Exceção a decidir: `SET NULL` em FK anulável de
   auditoria.
9. **Índices:** indexar toda FK nova usada em filtro ou exclusão. Em tabelas novas, `CREATE INDEX`
   comum dentro da transação; `CONCURRENTLY` só em tabela existente grande, em revisão isolada.
10. **Migrações de dados:** idempotentes (INSERT … WHERE NOT EXISTS por chave natural; UPDATE com
    WHERE que exclua o já convertido), usando `sa.table`/`sa.column` locais (nunca importar `models`),
    sem IDs de produção e **sem inventar estado** (nada de backfill de avaliações ou autoria antiga).

### G.2 Estratégia de testes

- **Suíte OLD_HEAD/NEW_HEAD por revisão**, no modelo de `tests/test_company_restore_migration.py`:
  esquema construído pelas revisões reais até `OLD_HEAD = "e4c004a26b01"`, dados sintéticos inseridos
  por tabelas refletidas, snapshot de constraints e contagens, upgrade para `NEW_HEAD`, verificação de
  contagens, constraints antigas preservadas, novas aplicadas, defaults em linhas novas, upgrade
  repetido sem efeito e downgrade apenas em banco descartável. Acrescentar `ondelete` à comparação de
  FKs. Não alterar as constantes da suíte da SEC-004.
- **Cenário encadeado** a partir de `b7c4e2a9f031` (estado possível de produção): dados em
  `b7c4e2a9f031` → `e4c004a26b01` → nova head.
- **PostgreSQL privado obrigatório** (`tests/postgresql_<etapa>_migration.py` reutilizando
  `PrivatePostgreSQL`) para revisões que alterem tabela existente, migrem dados, criem FK para tabela
  com dados ou mudem semântica de exclusão; incluir cenário de falha atômica; **adicionar o módulo
  explicitamente** ao job `security-postgresql` do CI (os `postgresql_*.py` não são descobertos pelo
  padrão `test*.py`).
- **Paridade em PostgreSQL:** criar teste de `compare_metadata` no PostgreSQL privado e comparação
  normalizada de CHECKs refletidos com o metadata (lacuna C.4).
- **SQLite** continua útil para paridade e regras simples, mas não prova atomicidade, locks nem
  semântica de `FOR UPDATE`.
- **Não usar `db.create_all()`** como substituto: testes de esquema das etapas futuras devem rodar
  sobre migrações reais (`DeployTestCase`) e sobre as suítes OLD_HEAD/NEW_HEAD.

### G.3 Preservação de dados

- Base sintética mínima representando o estado anterior: três planos (BRONZE, PRATA, OURO — hoje as
  fixtures criam só BRONZE), empresas ATIVA, SUSPENSA, CANCELADA e na lixeira, ADMIN e CLIENTE ativos
  e inativos, análises nos quatro status, MENSAL e QUINZENAL, relatório publicado e rascunho, uploads
  com `arquivo_armazenado`, chamados com mensagens, notificações e Guia.
- `seed.py` só insere o que falta e **não** atualiza linhas existentes: mudanças em planos de produção
  exigem migração de dados revisada (ou tela administrativa), nunca o seed.
- Atualizar junto com cada nova tabela: `tests/test_backup_restore.py` (contagem `tables`, fixture
  populando **toda** tabela nova — uma tabela vazia com PK inteira faz a verificação de sequências
  falhar —, relacionamentos e isolamento); fixtures de `tests/deploy_support.py`; testes da exclusão
  permanente de empresa; **criar** teste para a exclusão de análise (hoje inexistente); inventário de
  tabelas em `Docs/OPERACAO_BACKUP_RESTAURACAO.md`.

### G.4 Compatibilidade com produção: migrar antes de liberar o código

O build não migra e o Auto-Deploy pode estar ativo. Código que mapeia coluna inexistente quebra todas
as consultas do modelo. Padrão **expand → migrate → deploy → contract**:

1. **PR-A (expand):** somente modelos de **tabelas novas**, sem colunas novas em `Usuario`, `Empresa`,
   `Analise` ou `Plano` e sem relationships novas em modelos existentes; revisão Alembic; testes de
   migração SQLite e PostgreSQL; ensaio de backup atualizado. Esse código não consulta as tabelas
   novas e é inofensivo antes ou depois do upgrade.
2. **Operador (janela autorizada):** backup verificado e restauração isolada (seção F) →
   `flask db current` com `NEXO_DB_DIRECT=1` (somente leitura) → se estiver em `b7c4e2a9f031`, aplicar
   **primeiro** a `e4c004a26b01` na janela própria da SEC-004 (escritores parados) → aplicar cada nova
   revisão **pelo ID** (`flask db upgrade <rev>`), nunca `head` às cegas, com `lock_timeout` e
   `statement_timeout` na sessão → conferir o esquema resultante.
3. **PR-B (feature):** rotas, templates, relationships/cascade, passos novos na exclusão permanente e
   testes. Merge só depois do passo 2 confirmado.
4. **Contract:** remoções apenas em release posterior, após confirmado o desuso.

Alternativa: janela controlada única (suspender tráfego, migrar, implantar, homologar, reabrir), como
na SEC-004, quando código e esquema estiverem acoplados (por exemplo, coluna nova em modelo existente).

### G.5 Impacto das funcionalidades futuras sobre o esquema (análise; nada foi criado)

| Funcionalidade | Tabelas existentes afetadas | Tabelas novas prováveis | Riscos específicos |
|---|---|---|---|
| E4: controle por plano | `plano` (CHECK `plano_nome_plano_check` sem nome no código; campos `nivel_*` sem uso), `empresa.id_plano_atual`, `analise.id_plano_referencia` | Funcionalidade por plano (FK nomeada para `plano`, UNIQUE nomeada); opcional override por empresa | Não alterar CHECKs de `plano`; decidir plano atual × snapshot da análise; não remover recursos de clientes existentes; dados por migração idempotente por `nome_plano` |
| E5: avaliações de clientes | `analise`, `usuario`, `relatorio_analise.publicado` | Avaliação (FK `analise`, `usuario`, opcional `empresa`; nota com CHECK nomeado) | Sem cascade em `Analise`, a exclusão de análise e a exclusão permanente falham por FK; isolamento entre empresas; limite de texto e rate limit |
| E5: sugestões privadas | `empresa`, `usuario` | Sugestão (texto, status com CHECK nomeado) | Passo novo na exclusão permanente antes de apagar usuários; nunca expor publicamente; LGPD |
| E6/E7: moderação de depoimentos | `empresa`, `usuario`, avaliação | Depoimento (consentimento e data, status, moderador, publicação) | A landing (`/`) passa a depender do banco; filtrar `empresa.deletado_em IS NULL` e status aprovado; restauração da empresa re-expõe; consentimento registrado |
| Registros de publicação | `relatorio_analise` (despublicar apaga `data_publicacao`), `analise` | Evento append-only gravado na mesma transação | FK `NO ACTION` bloqueia exclusão da análise; cascade perde a auditoria — decidir (`SET NULL` ou snapshot sem FK) |

Novas tabelas criadas em `public` de projetos existentes podem receber grants padrão para as roles da
Data API; a mudança anunciada pelo Supabase para 2026-10-30 altera isso apenas para tabelas novas
(F.1). Qualquer política de RLS/REVOKE decidida na seção H deverá ser replicada nas migrações das
novas tabelas, com guarda de dialeto (PostgreSQL apenas).

### G.6 Checklist de PR de migração

1. Head única, `down_revision` correto, nenhuma revisão antiga editada.
2. Revisão aditiva; sem drop, rename ou alteração de CHECK/FK em tabela existente; em tabela
   existente, apenas `op.add_column` e `op.create_index` **fora** de `batch_alter_table` (G.1.4–G.1.5);
   converter na revisão manual todo `batch_op.add_column` emitido pelo autogenerate.
3. Constraints e índices nomeados; FKs indexadas; `ondelete` igual no modelo e na migração.
4. Migração de dados idempotente, sem importar `models`, sem IDs de produção, sem inventar estado.
5. Suíte OLD_HEAD/NEW_HEAD em SQLite + módulo PostgreSQL privado incluído no CI + falha atômica.
6. `test_backup_restore` (contagem e fixtures), exclusão permanente, exclusão de análise e
   documentação de backup atualizados.
7. Downgrade testado só em banco descartável e documentado como destrutivo.
8. Plano de implantação: backup, `flask db current`, aplicação por ID, PR-A/PR-B ou janela, Auto-Deploy
   confirmado desligado.

---

## H. Riscos de segurança

### H.1 Consolidado

| ID | Problema | Gravidade | Tipo | Evidência | Recomendação |
|---|---|---|---|---|---|
| S1 | Credencial fixa de um CLIENTE de demonstração versionada | **Alta** | Comprovado | `criar_cliente_demo.py:17-19` (valores não reproduzidos); versionada desde o 2º commit do repositório e presente em todas as branches | Remover o script (preferencial) ou endurecê-lo; tratar a credencial como comprometida (H.2) |
| S1' | Conta de demonstração existente em produção com a senha conhecida (tomada de conta e acesso aos dados da empresa vinculada) | **Crítica** | Potencial | Script vincula à primeira empresa e reseta senha; uso documentado com `NEXO_DB_DIRECT=1` | Verificação somente leitura autorizada (Apêndice A, A-8); se existir, desativar e incrementar a geração de autorização |
| S2 | Tabelas da aplicação em `public` possivelmente expostas pela Data API sem RLS (inclui `usuario.senha_hash` e BYTEA de planilhas) | **Crítica** | Potencial | Migrações sem GRANT/REVOKE/RLS; RLS desligada no esquema gerado localmente; documentação oficial (F.1) | Após o backup, consultas A-5, A-9 a A-12 e Security Advisor; com autorização, desligar a Data API ou retirar `public` dos Exposed schemas; avaliar RLS deny-all + REVOKE só após confirmar que a role da app é dona/tem BYPASSRLS (sem `FORCE`) |
| S3 | Revisão Alembic de produção desconhecida | Alta | Potencial | C.1 | `flask db current` somente leitura antes de qualquer revisão nova |
| S4 | Auto-Deploy possivelmente ativo + `main` sem proteção: merge pode implantar código antes da migração | Alta | Potencial | `Docs/PREPARACAO_RENDER.md:81`; A.2 | A.3 (proteção e Auto-Deploy desligado); G.4 |
| S5 | Sem backup real comprovado; plano do Supabase desconhecido (Free não tem backup gerenciado) | Alta | Potencial | D.2; F.1 | Executar F.2 com autorização |
| S6 | Arquivos legados em disco fora do dump | Alta | Potencial | B.6 | Inventário somente leitura de referências sem `db:` (A-6) |
| S7 | Sem separação de role de runtime e de migração (runtime possivelmente com privilégio de DDL) | Média | Potencial | `config.py:54-75`; uso de uma role por URL | Confirmar com A-3/A-5; planejar role de runtime só com DML |
| S8 | SSL não imposto pelo código | Média | Potencial | `config.py:32-45` | Confirmar `sslmode` nas URLs e "Enforce SSL" no painel |
| S9 | Separação de ambientes só por variáveis; scripts em modo development aceitam PostgreSQL; `.env` carregado automaticamente | Média | Comprovado | `config.py:23, 194-196`; `criar_cliente_demo.py`, `seed.py`, `seed_guia.py`, `migrar_arquivos_para_db.py` | Projetos Supabase distintos por ambiente; scripts recusam PostgreSQL por padrão |
| S10 | Isolamento entre empresas só na aplicação (sem RLS); várias tabelas derivam a empresa por FK | Média | Potencial | H.3 | Regras de H.3 para novas tabelas |
| S11 | Lacunas de teste de acesso cruzado: `/cliente/tickets/<id>`, `/notificacoes/<id>/abrir`, `/midia` com chave fora de `guia/` ou anônima | Média | Comprovado | Busca em `tests/` | Adicionar testes antes das novas tabelas |
| S12 | Sem trilha de login (`ultimo_acesso` nunca gravado) nem autoria do upload do cliente | Média | Comprovado | `models.py:144, 270` | Registrar em etapa futura |
| S13 | `migrations/env.py` renderiza a URL com senha em memória | Baixa | Potencial | `migrations/env.py:27-32` | Ajuste futuro revisado |
| S14 | Lacunas no `.gitignore` para outros formatos de dump e chaves locais | Baixa | Comprovado | H.5 | **Corrigido nesta branch** (preventivo) |
| S15 | Caminho local com nome de usuário do sistema em documento versionado | Baixa | Comprovado | `Docs/OPERACAO_BACKUP_RESTAURACAO.md:259` | Sanitizar (D.5) |

### H.2 Script de demonstração `criar_cliente_demo.py`

**Não executado. Credenciais não reproduzidas. Conta não testada.**

Comportamento [CÓDIGO]:

- Executa tudo no nível do módulo (sem `main()` nem guarda `if __name__ == "__main__"`): qualquer
  importação grava no banco.
- Usa `create_app("development")`, que **ignora `FLASK_ENV` e pula as travas de produção**
  (`app.py:76-102`), mas continua lendo `DATABASE_URL`/`DIRECT_URL` do ambiente e do `.env`.
- Vincula a conta nova (ou a existente sem empresa, por exemplo um ADMIN convertido) à **primeira
  empresa** (`ORDER BY id_empresa`), sem filtrar lixeira ou status; conta existente já vinculada
  mantém a empresa.
- Se a conta existe: força papel CLIENTE, **reativa** (`ativo=True`), **redefine a senha** para o
  valor fixo e rearma `primeiro_acesso`.
- Imprime a senha em texto claro e não desliga o `SQLALCHEMY_ECHO=True` do modo development (SQL com
  parâmetros no console).
- Nenhum módulo o importa e nenhum teste o cobre.

Recomendação:

1. **Preferencial:** remover o script e suas referências (`README.md:159, 302`;
   `Docs/ARQUIVOS_PUBLICACAO.txt:28`; `Docs/INVENTARIO_ANTES_RENDER.json:147`), em PR própria.
2. Se a demonstração for mantida: `main(argv)` com guarda `__main__`; recusar backend diferente de
   SQLite, `FLASK_ENV=production`, `NEXO_DB_DIRECT=1` e variáveis do Render; e-mail por argumento
   (domínio `.invalid`); senha aleatória (`secrets`) entregue pelo link de primeiro acesso de uso
   único já existente, **nunca impressa**; desligar o echo; exigir `--id-empresa` (recusando empresa
   na lixeira) e `--apply` (dry-run por padrão); não alterar papel nem reativar conta existente;
   teste no estilo de `tests/test_maintenance.py`.
3. **Rotação:** a credencial está no histórico Git e deve ser considerada comprometida e nunca
   reutilizada. Verificar em sessão somente leitura autorizada (Apêndice A, A-8) se a conta existe em
   produção:
   - não existe → basta a correção do script;
   - existe com `primeiro_acesso = true` → desativar (`ativo=false`) e incrementar
     `geracao_autorizacao` em operação autorizada; depois decidir a exclusão;
   - existe com `primeiro_acesso = false` e `data_atualizacao` preenchida → alguém concluiu o primeiro
     acesso: tratar como **possível incidente** (desativar, revisar atividade agregada, avaliar
     notificação com o responsável).

### H.3 Isolamento de dados

Mecanismos existentes [CÓDIGO]:

- `cliente_required` / `admin_required` (`blueprints/cliente.py:61-69`; `blueprints/admin.py:45-53`).
- CHECK `ck_usuario_role_empresa` (CLIENTE exige empresa; ADMIN proíbe).
- `_guard_sessao` em todo request: conta ativa, papel válido, empresa fora da lixeira, `BOOT_ID` e
  HMAC da credencial (`app.py:159-197`; `session_security.py:33-115`).
- Cada rota de `blueprints/cliente.py` filtra `id_empresa == current_user.empresa.id_empresa` e
  responde **404** para recurso de outra empresa; `/notificacoes/<id>/abrir` filtra pelo dono da
  notificação e redireciona ao destino padrão (`blueprints/notificacoes.py:56-65`); o upload revalida a
  análise com `FOR UPDATE` (`blueprints/cliente.py:347`).
- Notificações e Socket.IO: salas por usuário, entrega após commit com revalidação.
- `/midia` só serve conteúdo global do Guia.

Cobertura de testes: análise, PDF, upload e NexoBot cobertos [VALIDADO LOCALMENTE — inclusive o
`company_isolation` do ensaio de backup]; lacunas em S11.

Regras obrigatórias para avaliações, sugestões e depoimentos:

1. `id_empresa NOT NULL` com FK nomeada, autor (`usuario`) e, se couber, `id_analise`; considerar FKs
   compostas `(id_analise, id_empresa)` e `(id_usuario, id_empresa)` para coerência no banco.
2. `id_empresa` sempre derivado de `current_user`, nunca do formulário; `id_analise` revalidado
   (empresa, CONCLUIDO, publicado) com `FOR UPDATE` quando houver corrida.
3. Toda listagem filtrada pela empresa do usuário; detalhe alheio → **404**.
4. Rotas do cliente com `@cliente_required`; moderação com `@admin_required`; POST com CSRF e rate
   limit explícito; sem `csrf.exempt`.
5. Decisão explícita sobre empresas SUSPENSA/CANCELADA (hoje só a lixeira bloqueia o acesso do
   cliente; status diferente de ATIVA apenas impede novas análises, `blueprints/admin.py:230-231`).
6. Depoimento público só com consentimento registrado e aprovação do ADMIN, dados mínimos, filtro de
   lixeira e status aprovado.
7. Notificações apenas pelas funções existentes de `notifications.py` (despacho após commit).
8. Incluir as novas tabelas na exclusão permanente de empresa e no ensaio de backup.
9. Testes de acesso cruzado obrigatórios para cada rota nova (detalhe alheio → 404; POST com id de
   outra empresa → 404 sem efeito; listagem sem itens alheios; moderação negada a CLIENTE → 403).

### H.4 Verificações futuras de segurança do Supabase

Preparadas no Apêndice A (consultas somente leitura, **NÃO EXECUTADAS NO SUPABASE**; sintaxe
conferida localmente na E.2, item 13) e no painel:

- Data API habilitada? Quais schemas estão em Exposed schemas? GraphQL ativo?
- Grants de `anon`/`authenticated`/`PUBLIC` nas tabelas, colunas sensíveis e sequências de `public`.
- RLS por tabela (`relrowsecurity`, `relforcerowsecurity`) e políticas (`pg_policies`).
- Atributos das roles (`rolbypassrls`, `rolsuper`) e dono das tabelas.
- Default privileges que tabelas futuras herdarão.
- Security Advisor (lints `0013_rls_disabled_in_public` e `0023_sensitive_columns_exposed`).
- Cadastro no Supabase Auth habilitado? "Enforce SSL" ligado?
- Projetos separados por ambiente (desenvolvimento, homologação, produção).

Nenhuma permissão ou política foi alterada. Ordem recomendada: (1) backup autorizado; (2) inventário
somente leitura; (3) decisão do proprietário; (4) mudança autorizada com janela própria.

### H.5 Proteção dos backups no `.gitignore`

Já protegidos antes desta etapa: `.env` e variantes (exceto `.env.example`), `instance/`, `*.db`,
`uploads/*`, `.audit_tmp/`, `backups/`, `backup/`, `*.dump`, `*.dump.*`, `*.backup`, `*.sql.gz`,
`*.sql.gpg`, `*.age`, `*.pgpass`, `pg_service.conf`, `*.log`, `*.zip`.

Lacuna comprovada (verificada com `git check-ignore --no-index` contra o `.gitignore` do commit
`c919b1d`, sem criar arquivos no checkout): não eram ignorados `dumps/`, `dump/`, `dump.sql`,
`*.sql.*` (ex.: `.zst`), `*.custom`, `*.tar`, `*.tar.*`, `*.tgz`, o formato diretório do `pg_dump`
(`toc.dat`, `NNNN.dat.gz`), chaves (`*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`),
`credentials.json`, `secrets/` e `private/`.

**Ajuste aplicado nesta branch** (estritamente preventivo): bloco adicionado à seção de backups do
`.gitignore` com `dumps/`, `dump/`, `dump.sql`, `*.sql.*`, `*.custom`, `*.tar`, `*.tar.*`, `*.tgz`,
`*.dat`, `*.dat.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`, `credentials.json`,
`secrets/`, `private/` (padrões sem âncora valem em qualquer diretório). Verificações: `git ls-files -ci --exclude-standard` vazio (nenhum arquivo rastreado
passou a ser ignorado); `schema_nexo_mvp_v4.sql` e `Docs/schema_nexo_mvp_v4 (1).sql` continuam
versionáveis (não foi usado `*.sql` global). Nenhum código, teste ou workflow lê o `.gitignore`.

---

## I. Pendências

### I.1 Dependem de acesso ao Supabase real (com autorização, somente leitura)

1. Plano do projeto (Free/Pro), backups gerenciados e PITR.
2. Versão do servidor (`server_version_num`) — define o cliente `pg_dump` necessário.
3. Revisão em `alembic_version` (`b7c4e2a9f031` ou `e4c004a26b01`) e o schema onde ela está.
4. Nomes reais das constraints sem nome; paridade do esquema real com as migrações; ausência de
   `avaliacao_analise`, `fatura_cobranca` e índices `idx_*` do `.sql` histórico.
5. Objetos em `public` criados fora das migrações; schemas com objetos da aplicação.
6. Data API, Exposed schemas, RLS, grants, default privileges, roles, Security Advisor.
7. Existência e estado da conta de demonstração.
8. Referências legadas sem `db:` e órfãos em `arquivo_armazenado`; volume de BYTEA.
9. Execução do backup real (F.2) e da restauração isolada.

### I.2 Dependem de acesso ao Render

1. Estado real do Auto-Deploy e de Pull Request Previews (que copiariam variáveis de produção).
2. Commit implantado, comandos efetivos, número de instâncias.
3. Se `DIRECT_URL` aponta para conexão direta ou Session pooler; `sslmode` nas URLs.
4. Variáveis não declaradas no YAML (`ANTHROPIC_API_KEY`, `HF_API_TOKEN`, `RATELIMIT_STORAGE_URI`,
   `SOCKETIO_CORS_ORIGINS`) e versões efetivas de SQLAlchemy/Alembic instaladas.
5. Inventário privado dos segredos (incluindo a `SECRET_KEY`) e responsáveis.

### I.3 Dependem de credenciais autorizadas

1. Credencial de leitura para backup (service file + `.pgpass`), fora do repositório.
2. Certificado raiz do Supabase para `sslmode=verify-full`.
3. Chave pública de criptografia dos backups e custódia da chave privada.
4. Permissão administrativa no GitHub para aplicar a proteção da `main` (e plano compatível).

### I.4 Dependem de infraestrutura de testes

1. Cliente PostgreSQL da mesma major do servidor real (provavelmente 17.x ≥ 17.6) para o backup real e
   para repetir na mesma major os ensaios locais (E.2, itens 4, 12 e 13) antes do backup real.
2. Ambiente compatível com os schemas, roles e extensões gerenciados do Supabase para restaurar o
   artefato B (F.2, passo 11).
3. Ambiente isolado para restaurar o backup real.
4. Navegador (Playwright/Chromium) para os 11 testes de navegador, se forem exigidos localmente (o CI
   já os executa).
5. Teste de `compare_metadata` em PostgreSQL e de constraints refletidas (G.2), a criar em etapa
   futura.

### I.5 Dependem de decisões do proprietário

1. Escopo do backup (artefato B completo como principal e artefato A `public` como complementar, ou
   exceção registrada) e destino/retenção/RPO/RTO; avaliação de plano pago/PITR.
2. Remoção ou endurecimento de `criar_cliente_demo.py`; tratamento da credencial.
3. Desligar o Auto-Deploy; plano do GitHub para proteção da `main`.
4. Data API: desligar ou restringir; política de RLS.
5. Regras de negócio: SUSPENSA/CANCELADA; plano atual × snapshot da análise; avaliação por análise ou
   por usuário; consentimento de depoimentos; destino da auditoria na exclusão.
6. Fixar versões de SQLAlchemy e Alembic antes da primeira revisão das etapas.
7. Aplicação das correções documentais de D.5.
8. Sincronização da branch de integração com a `main`, que recebeu a PR #21 (A.2) depois do início
   desta etapa, antes da Etapa 0.3.

---

## J. Critérios de liberação

Antes de iniciar a **primeira alteração estrutural no banco de produção**, todos os itens abaixo devem
estar satisfeitos e registrados (evidências sem dados sensíveis, em local privado quando necessário):

1. **Backup real:** dump autorizado do Supabase gerado conforme F.2 (artefatos B e A, ou exceção
   aprovada e registrada), com SHA-256, `pg_restore --list` e cópia cifrada armazenada fora do GitHub
   e do Render.
2. **Restauração validada:** o dump real restaurado em ambiente isolado da mesma major (F.2, passo
   11), com o mesmo conjunto de tabelas inventariado na origem (esperado: 15 — 14 da aplicação +
   `alembic_version`; divergência registrada e explicada), `alembic_version` esperada, contagens
   iguais às da origem no mesmo snapshot (A-14), referências `db:` sem órfãos e hashes de uploads
   conferidos (A-7 a A-7d).
3. **Revisão conhecida:** `alembic_version` de produção confirmada; se estiver em `b7c4e2a9f031`, a
   `e4c004a26b01` aplicada antes, em janela própria, com o procedimento da SEC-004.
4. **Esquema real conhecido:** nomes de constraints e paridade com as migrações conferidos; objetos
   fora das migrações inventariados.
5. **Exposição avaliada:** Data API, RLS e grants verificados; decisão registrada (e aplicada, se for
   o caso) antes de criar tabelas com dados de clientes.
6. **Deploy controlado:** Auto-Deploy confirmado desligado (ou janela com tráfego suspenso definida) e
   ordem "migrar antes de liberar o código" (G.4) acordada.
7. **Proteção da `main`:** regra técnica aplicada ou, na impossibilidade, política manual registrada.
8. **Conta de demonstração:** inexistência confirmada ou conta desativada; script removido ou
   endurecido.
9. **Revisão candidata pronta:** PR com a nova migração aprovada no CI, incluindo suíte OLD_HEAD/NEW_HEAD
   em SQLite e PostgreSQL privado, falha atômica, ensaio de backup atualizado e checklist G.6 cumprido.
10. **Plano de recuperação:** responsável, janela, `lock_timeout`/`statement_timeout`, critério de
    abortar e caminho de retorno por restauração (nunca downgrade) definidos.

**Estado em 2026-10-24:** a preparação técnica e documental desta etapa está concluída. Os critérios
1 a 8 e 10 dependem de autorização, acesso ou decisão do proprietário e estão **pendentes**. O
critério 9 depende da primeira revisão das próximas etapas, que ainda não existe. **Nenhum critério
está cumprido.** A proteção operacional do banco de produção **não** pode ser declarada concluída até
que os critérios 1 e 2 sejam cumpridos.

---

## Apêndice A — Consultas somente leitura preparadas (NÃO EXECUTADAS NO SUPABASE)

Executar apenas em etapa operacional autorizada, em sessão `psql` privada (service file), sem
registrar URLs ou credenciais, sempre dentro de:

```sql
\set ON_ERROR_STOP on
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '15s';
SET LOCAL lock_timeout = '2s';
-- consultas
ROLLBACK;
```

Um erro aborta a transação READ ONLY: executar `ROLLBACK` e reabrir o bloco. A sintaxe das consultas
foi conferida num cluster local descartável com o esquema das migrações (E.2, item 13), onde as roles
do Supabase não existem; nenhuma foi executada no Supabase.

```sql
-- A-1 Versão e contexto
SELECT current_setting('server_version_num') AS versao_num, version(),
       current_user, current_database(), current_setting('search_path') AS search_path;

-- A-2 Constraints reais da aplicação (comparar com a seção B.3)
SELECT cl.relname AS tabela, con.conname AS nome, con.contype AS tipo,
       pg_get_constraintdef(con.oid) AS definicao
FROM pg_constraint con
JOIN pg_class cl ON cl.oid = con.conrelid
JOIN pg_namespace n ON n.oid = cl.relnamespace
WHERE n.nspname = 'public'
ORDER BY 1, 3, 2;

-- A-3 Atributos das roles relevantes
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolbypassrls
FROM pg_roles
WHERE rolname IN (current_user, 'postgres', 'anon', 'authenticated', 'service_role', 'authenticator')
ORDER BY rolname;

-- A-4 Revisão Alembic e localização da tabela de versão
SELECT table_schema FROM information_schema.tables WHERE table_name = 'alembic_version';
-- Executar a consulta abaixo só se a anterior retornar 'public'; caso contrário, qualificar com o
-- schema retornado.
SELECT version_num FROM public.alembic_version;

-- A-5 Inventário de public: dono e RLS
SELECT c.relkind, c.relname, pg_get_userbyid(c.relowner) AS dono,
       c.relrowsecurity AS rls, c.relforcerowsecurity AS rls_forcada
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f','S')
ORDER BY 1, 2;

-- A-6 Referências legadas (sem prefixo db:) — somente contagens; pode rodar em sessão somente
--     leitura autorizada no Supabase ou na cópia restaurada
SELECT (SELECT count(*) FROM public.upload_relatorio
         WHERE caminho_arquivo IS NOT NULL AND caminho_arquivo NOT LIKE 'db:%') AS uploads_legados,
       (SELECT count(*) FROM public.guia_topico
         WHERE imagem_url IS NOT NULL AND imagem_url NOT LIKE 'db:%') AS imagens_legadas;

-- A-7 a A-7d: executar APENAS NA CÓPIA RESTAURADA
-- A-7 Uploads com referência db: sem arquivo e volume
SELECT count(*) AS referencias_sem_arquivo
FROM public.upload_relatorio u
WHERE u.caminho_arquivo LIKE 'db:%'
  AND NOT EXISTS (SELECT 1 FROM public.arquivo_armazenado a WHERE a.chave = substr(u.caminho_arquivo, 4));
SELECT count(*) AS arquivos, pg_size_pretty(sum(length(conteudo))::bigint) AS volume
FROM public.arquivo_armazenado;

-- A-7b Imagens do Guia com referência db: sem arquivo
SELECT count(*) AS imagens_sem_arquivo
FROM public.guia_topico g
WHERE g.imagem_url LIKE 'db:%'
  AND NOT EXISTS (SELECT 1 FROM public.arquivo_armazenado a WHERE a.chave = substr(g.imagem_url, 4));

-- A-7c Arquivos sem nenhuma referência
SELECT count(*) AS arquivos_sem_referencia
FROM public.arquivo_armazenado a
WHERE NOT EXISTS (SELECT 1 FROM public.upload_relatorio u WHERE u.caminho_arquivo = 'db:' || a.chave)
  AND NOT EXISTS (SELECT 1 FROM public.guia_topico g WHERE g.imagem_url = 'db:' || a.chave);

-- A-7d Uploads com SHA-256 divergente (somente contagem; esperado 0)
SELECT count(*) AS hashes_divergentes
FROM public.upload_relatorio u
JOIN public.arquivo_armazenado a ON a.chave = substr(u.caminho_arquivo, 4)
WHERE u.caminho_arquivo LIKE 'db:%' AND u.hash_arquivo IS NOT NULL
  AND u.hash_arquivo <> encode(sha256(a.conteudo), 'hex');

-- A-8 Conta de demonstração: o operador lê o e-mail em criar_cliente_demo.py:17 e define
--     \set email_demo '...' sem registrá-lo em relatório ou log
SELECT u.id_usuario, u.role, u.ativo, u.primeiro_acesso, u.geracao_autorizacao,
       u.data_criacao, u.data_atualizacao, (e.deletado_em IS NOT NULL) AS empresa_na_lixeira
FROM public.usuario u LEFT JOIN public.empresa e ON e.id_empresa = u.id_empresa
WHERE lower(u.email) = lower(:'email_demo');

-- A-9 Privilégios efetivos de anon/authenticated/PUBLIC por tabela
SELECT c.relname AS tabela, r.papel,
       has_table_privilege(r.papel, c.oid, 'SELECT') AS sel,
       has_table_privilege(r.papel, c.oid, 'INSERT') AS ins,
       has_table_privilege(r.papel, c.oid, 'UPDATE') AS upd,
       has_table_privilege(r.papel, c.oid, 'DELETE') AS del
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN (SELECT rolname AS papel FROM pg_roles WHERE rolname IN ('anon','authenticated')
            UNION ALL SELECT 'public') r
WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m')
ORDER BY 1, 2;

-- A-9b Privilégio de leitura em colunas sensíveis
SELECT c.relname AS tabela, a.attname AS coluna, r.papel,
       has_column_privilege(r.papel, c.oid, a.attnum, 'SELECT') AS sel
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
CROSS JOIN (SELECT rolname AS papel FROM pg_roles WHERE rolname IN ('anon','authenticated')
            UNION ALL SELECT 'public') r
WHERE n.nspname = 'public'
  AND (c.relname, a.attname) IN (('usuario','senha_hash'), ('arquivo_armazenado','conteudo'))
ORDER BY 1, 2, 3;

-- A-9c Privilégios em sequências
SELECT c.relname AS sequencia, r.papel,
       has_sequence_privilege(r.papel, c.oid, 'USAGE') AS uso,
       has_sequence_privilege(r.papel, c.oid, 'UPDATE') AS upd
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN (SELECT rolname AS papel FROM pg_roles WHERE rolname IN ('anon','authenticated')
            UNION ALL SELECT 'public') r
WHERE n.nspname = 'public' AND c.relkind = 'S'
ORDER BY 1, 2;

-- A-10 Tabelas acessíveis por anon/authenticated sem RLS (esperado: nenhuma linha)
SELECT c.relname AS tabela_exposta_sem_rls
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p') AND NOT c.relrowsecurity
  AND EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname IN ('anon','authenticated')
              AND has_table_privilege(r.rolname, c.oid, 'SELECT, INSERT, UPDATE, DELETE'))
ORDER BY 1;

-- A-11 Políticas RLS existentes
SELECT schemaname, tablename, policyname, permissive, roles, cmd
FROM pg_policies WHERE schemaname = 'public' ORDER BY 2, 3;

-- A-12 Default privileges herdados por tabelas futuras
SELECT pg_get_userbyid(d.defaclrole) AS criador, n.nspname AS schema, d.defaclobjtype AS tipo,
       CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS beneficiario,
       a.privilege_type
FROM pg_default_acl d
LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
CROSS JOIN LATERAL aclexplode(d.defaclacl) a
ORDER BY 1, 2, 3, 4, 5;

-- A-13 SSL da sessão atual
SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid();

-- A-14 Contagens agregadas de todas as tabelas de public (sem dados de clientes)
SELECT 'plano' AS tabela, count(*) FROM public.plano
UNION ALL SELECT 'segmento', count(*) FROM public.segmento
UNION ALL SELECT 'empresa', count(*) FROM public.empresa
UNION ALL SELECT 'usuario', count(*) FROM public.usuario
UNION ALL SELECT 'analise', count(*) FROM public.analise
UNION ALL SELECT 'indicador_analise', count(*) FROM public.indicador_analise
UNION ALL SELECT 'relatorio_analise', count(*) FROM public.relatorio_analise
UNION ALL SELECT 'produto_curva_abc', count(*) FROM public.produto_curva_abc
UNION ALL SELECT 'upload_relatorio', count(*) FROM public.upload_relatorio
UNION ALL SELECT 'arquivo_armazenado', count(*) FROM public.arquivo_armazenado
UNION ALL SELECT 'chamado_suporte', count(*) FROM public.chamado_suporte
UNION ALL SELECT 'mensagem_suporte', count(*) FROM public.mensagem_suporte
UNION ALL SELECT 'notificacao', count(*) FROM public.notificacao
UNION ALL SELECT 'guia_topico', count(*) FROM public.guia_topico
UNION ALL SELECT 'alembic_version', count(*) FROM public.alembic_version;

-- A-15 Inventário de schemas (somente contagens por tipo de objeto)
SELECT n.nspname AS schema, c.relkind, count(*)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
GROUP BY 1, 2
ORDER BY 1, 2;
```

## Apêndice B — Esboço de comandos do backup real (placeholders; NÃO executar sem autorização)

Sequência ensaiada num cluster local descartável com dados sintéticos (E.2, item 13), com
`sslmode` desligado no socket local; TLS, conexão ao Supabase e restauração do artefato B não foram
ensaiados.

```bash
set -euo pipefail
umask 077
export PGSERVICEFILE="<arquivo-de-servico-privado>" PGPASSFILE="<passfile-privado-0600>"
CHECKOUT="<checkout-do-nexo>"
BACKUP_DIR="<diretorio-novo-fora-do-checkout>"
ENSAIO_DIR="<outro-diretorio-novo-para-o-ensaio>"
mkdir "$BACKUP_DIR" "$ENSAIO_DIR"
cd "$BACKUP_DIR"

# 0) Versões do cliente (devem ser da mesma major do servidor confirmado em A-1)
pg_dump --version; pg_restore --version; psql --version

# 1) Snapshot comum (F.2, passo 12): em OUTRA sessão psql "service=<servico_backup>", manter aberta
#      BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT pg_export_snapshot();
#    rodar A-14 nessa sessão e encerrá-la com ROLLBACK só depois dos dois dumps.
SNAPSHOT="<id-retornado-por-pg_export_snapshot>"

# 2) Artefato B (principal): banco completo
pg_dump --dbname="service=<servico_backup>" --no-password --format=custom --snapshot="$SNAPSHOT" \
  --file=nexo_completo.dump.partial 2>pg_dump_completo.log
mv -n nexo_completo.dump.partial nexo_completo.dump

# 3) Artefato A (complementar): schema da aplicação
pg_dump --dbname="service=<servico_backup>" --no-password --format=custom --schema=public \
  --snapshot="$SNAPSHOT" --file=nexo_public.dump.partial 2>pg_dump_public.log
mv -n nexo_public.dump.partial nexo_public.dump

# 4) Integridade de cada artefato
for f in nexo_completo.dump nexo_public.dump; do
  sha256sum "$f" > "$f.sha256"
  sha256sum --check "$f.sha256"
  pg_restore --list "$f" > "$f.toc"
  python -B "$CHECKOUT/scripts/verificar_prontidao_backup.py" --backup "$f" \
    --sha256 "$(cut -d' ' -f1 "$f.sha256")" --list
done

# 5) Criptografia para chave pública verificada + hash do cifrado (o claro fica até o passo 8)
for f in nexo_completo.dump nexo_public.dump; do
  gpg --output "$f.gpg" --encrypt --recipient "<fingerprint-verificada>" "$f"
  sha256sum "$f.gpg" > "$f.gpg.sha256"
done

# 6) Descriptografia de teste em diretório novo e conferência do hash original
gpg --output "$ENSAIO_DIR/nexo_public.dump" --decrypt nexo_public.dump.gpg
test "$(sha256sum < "$ENSAIO_DIR/nexo_public.dump" | cut -d' ' -f1)" = \
     "$(cut -d' ' -f1 nexo_public.dump.sha256)"

# 7) Restauração do artefato A em PostgreSQL ISOLADO da mesma major (nunca o Supabase), num banco
#    recém-criado e vazio, com o TOC sem a entrada "SCHEMA - public" (E.2, item 12)
pg_restore --list "$ENSAIO_DIR/nexo_public.dump" | grep -v ' SCHEMA - public ' \
  > "$ENSAIO_DIR/restauracao.list"
pg_restore --dbname="service=<servico_restauracao_isolada>" --no-password --no-owner \
  --no-privileges --single-transaction --exit-on-error --use-list="$ENSAIO_DIR/restauracao.list" \
  "$ENSAIO_DIR/nexo_public.dump"
psql "service=<servico_restauracao_isolada>" -X -c "ANALYZE;"
#    Na cópia: A-7 a A-7d e A-14, comparando com as contagens do snapshot.
#    Artefato B: somente em ambiente compatível com o Supabase (F.2, passo 11).

# 8) Só depois do passo 7 aprovado: remover as cópias em claro, conforme a política aprovada
```

---

*Documento gerado na Etapa 0.2 da reestruturação comercial do NEXO. Referência:
`c919b1d17e6cbf4bf9dac09e29cefca67d908d06`, 2026-10-24.*
