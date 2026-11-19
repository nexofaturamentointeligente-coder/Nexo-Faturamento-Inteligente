# NEXO - Faturamento Inteligente | Proteção do Banco de Dados e Preparação das Migrações

**Etapa 0.2A — Auditoria, proteção do banco de dados e preparação das migrações**

Este documento é a referência da Etapa 0.2A. Registra a auditoria dos mecanismos de
persistência, migração, backup e recuperação do NEXO, os testes executados, as correções
locais feitas e o planejamento das alterações estruturais das próximas etapas. Continua a
[Etapa 0.1](00_01_BASELINE_GITHUB.md), cujos riscos foram reconferidos aqui (confirmados,
corrigidos ou complementados).

Nada nesta etapa tocou produção. Não houve acesso ao Supabase, ao painel ou às URLs do
Render, não houve migração em banco existente, commit, push, deploy nem envio de dados a
serviços externos. Toda execução com banco usou bancos **descartáveis** (SQLite em diretório
temporário ou cluster PostgreSQL 18 criado e destruído pelo próprio teste) com dados
**fictícios**.

Legenda de situação usada em todo o documento:

| Situação | Significado |
|---|---|
| **IMPLEMENTADO** | Alteração feita nos arquivos locais desta pasta. |
| **TESTADO LOCALMENTE** | Comprovado por execução nesta máquina: testes, banco descartável ou utilitário local. Não equivale a homologação de produção. |
| **DOCUMENTADO** | Registrado em documento; não depende de execução. |
| **PENDENTE DE VALIDAÇÃO** | Depende de acesso autorizado (Supabase, Render) ou de decisão do responsável. |
| **NÃO VERIFICADO** | Não foi possível comprovar nesta etapa. |

Nos riscos, **Comprovado** significa verificado no código (arquivo:linha) ou reproduzido por
experimento; **Potencial** depende de condição externa ou de execução não realizada.

---

## A. Identificação

| Item | Valor |
|---|---|
| Projeto | NEXO - Faturamento Inteligente |
| Etapa | 0.2A — Auditoria, proteção do banco de dados e preparação das migrações |
| Data da execução | 2026-10-24 |
| Pasta de trabalho | Pasta local extraída de ZIP, **sem `.git`**. Nenhum comando Git foi executado nesta pasta; apenas `git init` e `git check-ignore` em repositórios temporários fora dela (E12). Não houve clone, commit, push, PR nem alteração de branch ou `origin`. |
| Repositório de referência técnica e histórica | `https://github.com/Helio965/Nexo---Faturamento-Inteligente---Render` (commit histórico informado: `ff8c2d8009c4889648a357fb5488a2a004cb7fe8`; **não verificado** localmente, pois não há `.git`) |
| Repositório oficial de destino | `https://github.com/nexofaturamentointeligente-coder/Nexo-Faturamento-Inteligente` (o proprietário fará o commit manual) |
| Ambiente | Windows 11 Pro; PowerShell e Git Bash; Python 3.12.10 |
| Pacotes locais divergentes de `requirements.txt` | Flask 3.0.3 (fixado 3.1.3), Werkzeug 3.0.4 (3.1.9), Flask-SocketIO 5.4.1 (5.6.1), Pillow 12.2.0 (13.0.0), python-dotenv 1.0.1 (1.2.4); `gunicorn` (de `requirements.txt`) e `PyYAML`/`playwright` (de `requirements-dev.txt`) ausentes. SQLAlchemy 2.0.51, Alembic 1.18.4, Flask-Migrate 4.0.7, Flask-SQLAlchemy 3.1.1 e psycopg2-binary 2.9.9 instalados. Nada foi instalado ou atualizado. |
| PostgreSQL local | Binários 18.1 em `C:\Program Files\PostgreSQL\18\bin`, **fora do `PATH`**. Existe um serviço PostgreSQL 18 **preexistente** na porta 5432: não foi usado nem acessado. |
| Finais de linha | Os 231 arquivos de texto desta pasta (critério: UTF-8 sem byte nulo; 4 vazios), 232 com este documento, usam **LF** (0 arquivos CRLF). A Etapa 0.1 (C.3) descreve CRLF no repositório Git; isso não vale para esta cópia. Todas as edições preservaram LF. |
| Preservação do estado original | Antes de qualquer alteração foi gravado, fora da pasta, um inventário SHA-256 dos 258 arquivos, idêntico ao ZIP de origem, que permaneceu intacto. Os originais dos 7 arquivos alterados foram conferidos por hash; a comparação final com o inventário mostra só esses 7 arquivos modificados e este documento criado (seção F). |

**Método.** Leitura direta dos componentes centrais e auditoria paralela em seis
dimensões: modelos e exclusões, migrações, ferramentas de backup, arquivos armazenados,
vazamento de dados e consistência da documentação. Cada achado passou por verificação
adversarial independente, que tentou refutá-lo relendo o código e reproduzindo os
experimentos. Uma sétima frente levantou a documentação oficial do Supabase e do
PostgreSQL, com fonte e trecho citado (extraído por ferramenta que resume páginas; reconferir
na página antes da execução). Dos 90 achados, os verificadores refutaram nenhum, corrigiram
detalhes de 9 (parciais) e acrescentaram omissões. Depois das correções, uma segunda revisão
adversarial independente das alterações (código e testes, documentação e conformidade), com
verificação de cada achado, não encontrou achado de severidade alta. Os achados confirmados
foram corrigidos e revalidados: ramos novos sem teste, entrada do `PATH` sem letra de
unidade aceita pelo Python 3.12, restauração com políticas RLS, ordenação dependente da
collation e imprecisões de texto e de contagem neste documento. Uma verificação final
independente das correções, também com refutação adversarial, considerou todos esses
achados resolvidos e confirmou 7 ajustes menores de teste e de redação, igualmente
aplicados. A consolidação abaixo já incorpora as três rodadas.

**Inventário inicial dos arquivos a modificar (Subetapa 0.2A.1).** Planejados e efetivamente
alterados: `scripts/verificar_prontidao_backup.py`, `tests/test_backup_readiness.py`,
`tests/test_backup_restore.py`, `.gitignore`, `Docs/OPERACAO_BACKUP_RESTAURACAO.md`,
`Docs/PREPARACAO_RENDER.md` e `Docs/PLANO_PUBLICACAO_SEGURA.md`, além deste documento
(novo). A relação completa está na seção F.

---

## B. Diagnóstico do banco

### B.1 Modelos e tabelas (Comprovado)

`models.py` mapeia **14 modelos em 14 tabelas**; com `alembic_version`, o banco migrado tem
15 tabelas. A docstring de `models.py:1-16` ainda fala em "8 tabelas" e diz que
`chamado_suporte`/`mensagem_suporte` não têm modelo: é uma divergência interna (o arquivo
não pode ser alterado nesta etapa).

| # | Modelo → tabela | PK | FKs (alvo; nulidade) | UNIQUE / índices | CHECK | Observações |
|---|---|---|---|---|---|---|
| 1 | `Plano` → `plano` | `id_plano` | — | `nome_plano` | 5 sem nome (nome BRONZE/PRATA/OURO e níveis) | Campos de nível persistidos e não lidos pela aplicação (Etapa 0.1, F2.1) |
| 2 | `Segmento` → `segmento` | `id_segmento` | — | `nome_segmento` | — | |
| 3 | `Empresa` → `empresa` | `id_empresa` | `segmento`; `plano` (ambas NOT NULL) | `cnpj` | `status_conta` | `deletado_em` = lixeira (soft delete) |
| 4 | `Usuario` → `usuario` | `id_usuario` | `empresa` (nula para ADMIN) | `email` (sensível a maiúsculas) | `role`; `ck_usuario_geracao_autorizacao`; `ck_usuario_role_empresa` | `geracao_autorizacao` (SEC-004); `primeiro_acesso` |
| 5 | `Analise` → `analise` | `id_analise` | `empresa`, `plano` (snapshot), `usuario` responsável (todas NOT NULL) | — | 3 sem nome + `ck_analise_tipo_quinzena` | Único modelo com `cascade="all, delete-orphan"` (uploads, indicador, relatório, curva ABC) |
| 6 | `UploadRelatorio` → `upload_relatorio` | `id_upload` | `analise` (NOT NULL); `usuario` homologador (nula) | `uq_upload_analise_tipo` | 3 sem nome | `caminho_arquivo` = `'db:<chave>'`, sem FK |
| 7 | `IndicadorAnalise` → `indicador_analise` | `id_indicador` | `analise` (NOT NULL, UNIQUE: 1:1) | `id_analise` | — | KPIs e período da base |
| 8 | `RelatorioAnalise` → `relatorio_analise` | `id_relatorio` | `analise` (NOT NULL, UNIQUE: 1:1) | `id_analise` | — | `conclusao_estrategica` NOT NULL; `gerado_por_ia` |
| 9 | `ProdutoCurvaABC` → `produto_curva_abc` | `id_produto_abc` | `analise` (NOT NULL) | `uq_abc_analise_posicao`; `ix_abc_analise` (redundante com a coluna líder da UNIQUE) | `classe_abc` | Top 20 persistido |
| 10 | `ChamadoSuporte` → `chamado_suporte` | `id_chamado` | `empresa`; `usuario` cliente (ambas NOT NULL) | `ix_chamado_empresa` | 3 sem nome | Relação `mensagens` sem cascade |
| 11 | `MensagemSuporte` → `mensagem_suporte` | `id_mensagem` | `chamado_suporte`; `usuario` remetente (ambas NOT NULL) | `ix_mensagem_chamado` | — | |
| 12 | `Notificacao` → `notificacao` | `id_notificacao` | `usuario` (NOT NULL) | `ix_notificacao_usuario` | — | `link_destino` textual, sem FK |
| 13 | `GuiaTopico` → `guia_topico` | `id` | — | `ix_guia_topico_categoria` | — | `imagem_url` = `'db:guia/<hash>'` ou caminho legado |
| 14 | `ArquivoArmazenado` → `arquivo_armazenado` | `chave` (varchar 255) | — | — | — | `conteudo` BYTEA (`deferred`); referenciado só por convenção |

Totais verificados no DDL PostgreSQL gerado offline e em PostgreSQL 18.1 descartável
(TESTADO LOCALMENTE): 20 CHECK (17 sem nome), 16 FK (todas sem nome e **nenhuma com
`ON DELETE`**), 15 PK, 8 UNIQUE (6 sem nome), 5 índices explícitos, 13 PKs `SERIAL`. Nove
colunas de FK não têm índice (três de `analise`, duas de `empresa`,
`chamado_suporte.id_usuario_cliente`, `mensagem_suporte.id_usuario_remetente`,
`upload_relatorio.id_usuario_admin` e `usuario.id_empresa`). No PostgreSQL 18,
`pg_constraint` também lista 100 restrições NOT NULL (tipo `n`), que não existem nessa
forma em versões anteriores; contagens por tipo devem ser comparadas entre servidores da
mesma versão.

**Nomes gerados pelo PostgreSQL para as constraints sem nome** (TESTADO LOCALMENTE em PG
18.1 após `upgrade` até a head). Existem porque `extensions.py:20-26` cria
`db = SQLAlchemy(model_class=Base)` sem `MetaData(naming_convention=...)`; vale só o padrão
do SQLAlchemy para índices. Seguem os padrões `<tabela>_<coluna>_check|fkey|key` e
`<tabela>_pkey` (a PK de `alembic_version` chama-se `alembic_version_pkc`, nome dado pelo
Alembic), por exemplo `plano_nome_plano_check`, `empresa_status_conta_check`,
`usuario_role_check`, `analise_status_analise_check`, `upload_relatorio_tipo_relatorio_check`,
`chamado_suporte_status_chamado_check`, `empresa_cnpj_key`, `usuario_email_key`,
`analise_id_empresa_fkey` e `usuario_pkey`. Em produção os nomes são **PENDENTE DE
VALIDAÇÃO**: só coincidem se o schema foi criado por estas migrações.

### B.1.1 Campos obrigatórios por modelo (Comprovado)

Colunas NOT NULL além de PK e FK, lidas da metadata do SQLAlchemy (`models.py`), sem banco.
"Com valor padrão" indica default no ORM ou no banco.

| Modelo | Obrigatórios sem valor padrão | Obrigatórios com valor padrão |
|---|---|---|
| `Plano` | `nome_plano`, `valor_mensal`, `qtd_analises_mes`, `tipo_analise_permitida`, `nivel_entrega_analise`, `nivel_dashboard`, `nivel_atendimento` | `ativo`, `data_criacao` |
| `Segmento` | `nome_segmento` | `ativo`, `data_criacao` |
| `Empresa` | `cnpj`, `razao_social`, `email_contato`, `data_contratacao` | `status_conta`, `data_criacao` |
| `Usuario` | `nome`, `email`, `senha_hash`, `role` | `ativo`, `geracao_autorizacao`, `primeiro_acesso`, `data_criacao` |
| `Analise` | `periodo_inicio`, `periodo_fim`, `mes_referencia`, `ano_referencia`, `tipo_analise` | `status_analise`, `data_criacao` |
| `UploadRelatorio` | `tipo_relatorio`, `nome_arquivo_original`, `caminho_arquivo`, `extensao_arquivo` | `data_upload`, `status_processamento` |
| `IndicadorAnalise` | — | `versao_processamento`, `data_geracao` |
| `RelatorioAnalise` | `titulo`, `conclusao_estrategica` | `publicado`, `gerado_por_ia` |
| `ProdutoCurvaABC` | `posicao_ranking`, `produto_nome`, `faturamento`, `percentual_individual`, `percentual_acumulado`, `classe_abc` | `data_geracao` |
| `ChamadoSuporte` | `assunto`, `categoria` | `status_chamado`, `prioridade`, `data_abertura` |
| `MensagemSuporte` | `mensagem` | `data_envio`, `lida` |
| `Notificacao` | `texto` | `lida`, `data_criacao` |
| `GuiaTopico` | `categoria`, `pergunta`, `resposta` | `ativo`, `data_criacao` |
| `ArquivoArmazenado` | `conteudo`, `tamanho` | `data_criacao` |

São 71 colunas; somadas às 14 FKs NOT NULL e às 15 colunas de PK (com a de
`alembic_version`), dão as 100 restrições NOT NULL que o PostgreSQL 18 lista (B.1). Os
relacionamentos ORM acompanham as FKs da tabela B.1; coleções, `back_populates` e cascades
estão na B.2.

### B.2 Integridade dos vínculos principais

| Vínculo | Garantia no banco | Comportamento do ORM | Fluxos e riscos (Comprovado salvo indicação) |
|---|---|---|---|
| Empresa → Usuario | FK `usuario.id_empresa`, sem `ON DELETE`; `ck_usuario_role_empresa` exige empresa para CLIENTE e proíbe para ADMIN | `back_populates`, sem cascade de exclusão | A lixeira não altera usuários além da geração de autorização; a restauração não reativa contas; o hard delete apaga os CLIENTE explicitamente. O banco não impede que um ADMIN seja convertido em CLIENTE; `criar_cliente_demo.py` faz isso quando o e-mail coincide (reproduzido em SQLite descartável). |
| Empresa → Analise | FK NOT NULL, sem `ON DELETE` | sem cascade a partir de `Empresa` | O hard delete enumera as análises à mão. Com a empresa na lixeira, o ADMIN ainda cria análise, publica devolutiva e resolve chamado, com e-mail à empresa (amplia F4.6 da Etapa 0.1). |
| Analise → UploadRelatorio, IndicadorAnalise, RelatorioAnalise, ProdutoCurvaABC | FKs NOT NULL; 1:1 por UNIQUE para indicador e relatório | `cascade="all, delete-orphan"` (`models.py:224-241`) | A exclusão de análise depende desse cascade. No hard delete, indicador e relatório recebem DELETE duas vezes (enumeração manual + cascade), com `SAWarning`; se avisos virarem erro, a rota quebra. |
| ChamadoSuporte → MensagemSuporte | FK NOT NULL | relação sem cascade | O hard delete apaga as mensagens antes do chamado, inclusive as enviadas por ADMIN. |
| ArquivoArmazenado ↔ referências | **Nenhuma FK**: `upload_relatorio.caminho_arquivo` e `guia_topico.imagem_url` guardam `'db:'+chave` | `storage.py` é o ponto único | O banco aceita referência quebrada e blob órfão sem erro. Exclusões apagam o blob sem a checagem de compartilhamento que o upload faz. `migrar_arquivos_para_db.py` usa o nome-base como chave e pode sobrescrever bytes de outro upload. |

Qualquer FK nova (Etapas E4 a E7) sem tratamento explícito faz a exclusão de análise e a
exclusão permanente de empresa falharem por `IntegrityError`, com rollback total e
mensagem genérica (F6.2 da Etapa 0.1, agora reproduzido em SQLite descartável).

### B.3 Matriz real de exclusão e resíduos (Comprovado; DOCUMENTADO)

| Fluxo | Remove | Mantém | Atomicidade |
|---|---|---|---|
| Mover empresa para a lixeira (`company_security.py`) | nada | 100% dos dados, inclusive BYTEA; usuários com `ativo` preservado e geração +1 (sessões e tokens antigos inválidos) | transacional |
| Restaurar da lixeira | limpa `deletado_em` | contas bloqueadas continuam bloqueadas; geração não volta (novo login obrigatório) | transacional |
| Exclusão permanente (`blueprints/admin.py:523-612`) | curva ABC, uploads e seus blobs `db:`, indicador, relatório, análises, mensagens (inclusive de ADMIN), chamados, notificações dos usuários da empresa, CLIENTE e a empresa | notificações de **ADMIN** com nome fantasia e assunto do chamado no texto e links órfãos; e-mails já enviados; backups anteriores | banco transacional; arquivo **legado** é apagado do disco antes do commit (perda se o commit falhar) |
| Excluir análise (`blueprints/admin.py:963-1010`) | uploads e blobs, indicador, relatório, curva ABC e a análise | notificações que apontam para a análise | idem quanto a legados; captura só `IntegrityError` |
| Substituir upload pelo ADMIN ou pelo cliente (`blueprints/admin.py:671-700`; `blueprints/cliente.py:354-386`) | registro anterior e seu blob `db:`, se nenhum outro upload usa a mesma referência | blob compartilhado com outro upload; arquivo legado até o commit | transacional; o legado sai do disco após o commit, sem erro se a remoção falhar |
| Trocar/remover imagem ou excluir tópico do Guia (`blueprints/admin.py:1227-1234` e `1247-1254`) | tópico (na exclusão) e blob `guia/` que nenhum outro tópico usa | imagens legadas em `static/` | transacional |

Não existe registro das exclusões definitivas. Uma restauração de backup anterior traz de
volta empresas e usuários excluídos sem fonte para reaplicar a exclusão (ver G.10 e I).

### B.4 Riscos identificados nesta seção

| # | Risco | Tipo | Sev. | Situação |
|---|---|---|---|---|
| B4.1 | Exclusão permanente deixa notificações de ADMIN com dados da empresa (resíduo para LGPD; a mensagem de sucesso diz "todos os seus dados") | Comprovado | Média | DOCUMENTADO; correção de código pendente (arquivos congelados) |
| B4.2 | FK nova sem política de exclusão quebra exclusões (F6.2) | Comprovado | Média | DOCUMENTADO; diretriz em H |
| B4.3 | Escritas e e-mails para empresa na lixeira | Comprovado | Média | DOCUMENTADO; pendente |
| B4.4 | Lixeira sem prazo e sem trilha de exclusões; restauração ressuscita dados | Comprovado | Média | DOCUMENTADO; decisão pendente |
| B4.5 | Arquivo legado apagado antes do commit; exclusões sem checagem de chave compartilhada | Comprovado | Baixa | DOCUMENTADO; pendente |
| B4.6 | DELETE duplo de indicador/relatório no hard delete (`SAWarning`) | Comprovado | Baixa | DOCUMENTADO; pendente |
| B4.7 | Banco não garante coerência papel × empresa entre tabelas (responsável não ADMIN, chamado com cliente de outra empresa) | Comprovado | Baixa | DOCUMENTADO; consultas de detecção TESTADAS LOCALMENTE (G.13, E13) |
| B4.8 | Nove FKs sem índice; `idx_*` do DDL histórico inexistentes | Comprovado | Baixa | DOCUMENTADO; planejar em migração futura |
| B4.9 | Docstrings internas erradas (`models.py:1-16`; `admin.py` nega o cascade que usa) | Comprovado | Baixa | DOCUMENTADO; correção pendente (arquivos congelados) |

---

## C. Migrações

### C.1 Cadeia (Comprovado; TESTADO LOCALMENTE)

Confirmado pela API `ScriptDirectory` do Alembic, sem banco: **8 revisões, cadeia linear,
base `d87a6fbc2fbe`, head única `e4c004a26b01`**, sem `branch_labels` nem `depends_on`;
todo arquivo começa com o próprio revision id. A informação da análise preliminar está
correta.

| # | Revisão | `down_revision` | Mudança | Tipo | Downgrade |
|---|---|---|---|---|---|
| 1 | `d87a6fbc2fbe` (base) | — | 12 tabelas e 4 índices | aditiva | `DROP` de todas as tabelas: perda total |
| 2 | `c7189696a9cd` | `d87a6fbc2fbe` | `indicador_analise.periodo_base_inicio/fim` | aditiva | perde os períodos |
| 3 | `f958d38a7f19` | `c7189696a9cd` | `relatorio_analise.gerado_por_ia` (`server_default='false'`) | aditiva | perde a indicação de IA |
| 4 | `91a2ecf56a41` | `f958d38a7f19` | `usuario.primeiro_acesso` (`server_default='false'`) | aditiva | perde o estado de primeiro acesso; no SQLite com linhas filhas, a recriação de `usuario` falha |
| 5 | `d61b5e68082c` | `91a2ecf56a41` | `guia_topico` (CMS do Guia/NexoBot) | aditiva | `DROP TABLE guia_topico` |
| 6 | `a3f1c9b27d04` | `d61b5e68082c` | `empresa.deletado_em` | aditiva | perde a marcação da lixeira |
| 7 | `b7c4e2a9f031` | `a3f1c9b27d04` | `arquivo_armazenado` (BYTEA) | aditiva | `DROP TABLE`: perde **todos os bytes** dos documentos |
| 8 | `e4c004a26b01` (head) | `b7c4e2a9f031` | `usuario.geracao_autorizacao` + `UPDATE` que desativa clientes de empresas na lixeira | aditiva **e altera dados** | remove a coluna, perde o histórico de revogação e **não** reativa contas (irreversível por desenho) |

Nenhuma revisão é segura como mecanismo de rollback; a orientação de não usar
`downgrade` (`Docs/OPERACAO_BACKUP_RESTAURACAO.md`, §10) continua válida e foi completada.

### C.2 Compatibilidade com os modelos (TESTADO LOCALMENTE)

- SQLite descartável: `upgrade` até a head gera 15 tabelas; `compare_metadata` retorna
  vazio com `compare_type` padrão (no Alembic 1.18.4 já é `True`), `True`, `False` e com
  `compare_server_default`. Upgrade repetido, downgrade até a base e novo upgrade no mesmo
  arquivo vazio concluíram sem erro. O teste do projeto
  `tests.test_deploy...test_migrations_empty_database_repeat_upgrade_and_metadata` passou.
- PostgreSQL 18.1 descartável: o ensaio de backup aplica as 8 revisões, confere a head e o
  conjunto de tabelas; as consultas de G.11 a G.13 confirmaram constraints e sequências.
- DDL PostgreSQL offline (URL falsa, sem conexão): uma transação única `BEGIN/COMMIT`,
  13 `SERIAL`, `UPDATE` da `e4c004a26b01` com `ativo=false`.
- Limite: o Alembic não compara CHECK constraints; a paridade não detecta CHECK perdida.

### C.3 Riscos para futuras alterações (Comprovado por experimento, salvo indicação)

| # | Risco | Efeito | Situação |
|---|---|---|---|
| C3.1 | `server_default='false'/'true'` em string em colunas BOOLEAN (`91a2ecf56a41`, `f958d38a7f19`, `d61b5e68082c`) | No **SQLite**, linhas preexistentes ficam com o texto `'false'`; o ORM lê `True`, filtros `is_(False)` não as encontram e o CAS de troca de senha falha (CLIENTE legado sem saída em SQLite de desenvolvimento). No PostgreSQL o literal é convertido corretamente. | DOCUMENTADO; regra em H.2. Correção exige revisão nova (não criada agora). |
| C3.2 | Recriação `batch` no SQLite descarta CHECK sem nome (`render_as_batch=True` é padrão do Flask-Migrate) | Esquemas divergem entre SQLite e PostgreSQL sem que `compare_metadata` perceba | DOCUMENTADO; regra em H.2 |
| C3.3 | No SQLite com FK ativa (`app.py` liga `PRAGMA foreign_keys=ON` em toda conexão, inclusive na do Alembic), recriar tabela referenciada com linhas filhas falha | Downgrade para em revisão intermediária com tabela `_alembic_tmp_*` residual | DOCUMENTADO; downgrade em SQLite com dados não é suportado |
| C3.4 | Migrações não são atômicas no SQLite (`transactional_ddl=False`) | Falha no `UPDATE` da `e4c004a26b01` deixa a coluna criada e a versão antiga | DOCUMENTADO; ensaio de falha deve ser feito no PostgreSQL |
| C3.5 | `init_db.py` (README, opção A) usa `create_all` sem `alembic_version` | O `upgrade` seguinte falha (`table plano already exists`); a CHECK de geração fica em nível de tabela, divergindo da migração | DOCUMENTADO; pendente (README fora do escopo) |
| C3.6 | `NUMERIC` sem precisão vira afinidade REAL no SQLite | Valores monetários com artefatos de ponto flutuante nos ensaios SQLite | DOCUMENTADO; regra em H.2 |
| C3.7 | `migrations/env.py`: `render_as_string(hide_password=False)`, `get_engine()` depreciado no Flask-SQLAlchemy 3.1 (aviso em cada migração dos testes; o fallback cobre a remoção na 3.2), sem `lock_timeout`/`statement_timeout` em migrações PostgreSQL | Potencial vazamento de credencial em traceback (F1.11); risco de fila de lock em produção | DOCUMENTADO; alteração de `env.py` fica para etapa futura |
| C3.8 | Testes com contratos fixos: 15 tabelas e 4 uploads/5 arquivos no ensaio; `OLD_HEAD`/`NEW_HEAD` explícitos | Toda tabela nova exige atualizar `tests/test_backup_restore.py` | DOCUMENTADO |

### C.4 Revisão aplicada no Supabase — PENDENTE DE VALIDAÇÃO

A revisão de produção é **desconhecida**. O código atual mapeia
`usuario.geracao_autorizacao`; se o banco estiver em `b7c4e2a9f031`, toda consulta a
`Usuario` falha (reproduzido em SQLite descartável: `no such column:
usuario.geracao_autorizacao`). Como a `e4c004a26b01` já está no código, **qualquer**
publicação, mesmo sem arquivo novo em `migrations/versions`, depende de comparar
`flask db current` com `flask db heads` (procedimento em G.12 e no §5 de
`Docs/PREPARACAO_RENDER.md`).

---

## D. Backup

### D.1 Ferramentas existentes

| Ferramenta | Função | Situação |
|---|---|---|
| `scripts/verificar_prontidao_backup.py` | Verifica utilitários e um dump local (SHA-256, assinatura, `pg_restore --list`), sem conectar a banco | IMPLEMENTADO (F2 a F4); TESTADO LOCALMENTE |
| `tests/test_backup_readiness.py` | Testes do verificador com mocks | 18 testes (13 + 5 novos); TESTADO LOCALMENTE |
| `tests/test_backup_restore.py` | Ensaio opt-in: cluster PostgreSQL temporário, migrações, fixtures, `pg_dump` custom, SHA-256, TOC, CLI, `pg_restore` em banco novo, comparação completa, rotas Flask | IMPLEMENTADO (F1); TESTADO LOCALMENTE no Windows com PG 18.1 (8 testes), exceto a verificação do ETL, que exige Linux |
| `tests/postgresql_support.py` e `tests.postgresql_*` | Cluster privado por socket Unix para 5 suítes de segurança | NÃO VERIFICADO nesta máquina (exigem POSIX por desenho e falham no `setUpClass` no Windows; rodam no CI) |
| `Docs/OPERACAO_BACKUP_RESTAURACAO.md` | Roteiro operacional | IMPLEMENTADO (F7) |

### D.2 Avaliação de segurança (Subetapa 0.2A.4)

| Critério | Avaliação |
|---|---|
| Segurança dos comandos | `shell=False`, argumentos montados internamente, `stdin/stdout/stderr` controlados, timeout de 15 s (adequado: no formato custom o TOC fica no início; `--list` levou 21-60 ms em arquivos esparsos de até 4 GiB após a primeira abertura e 0,3-0,4 s num arquivo aleatório de 1 GiB; a primeira abertura, mais lenta por causa do antivírus, recai no cálculo do hash, fora do timeout). **Defeito corrigido:** no Windows, `shutil.which` consultava antes o diretório atual e aceitava `.bat/.cmd/.com`; um utilitário plantado na pasta de trabalho era executado e podia dar veredito falso, e um `.bat` permitia injeção de comando pelo nome do arquivo (`&`) via `cmd.exe`. Na revisão, a busca passou a usar `Path.is_absolute()`: no Python 3.12, `os.path.isabs` aceita `\dir` sem letra de unidade, que depende da unidade do diretório atual. |
| Proteção das credenciais | Ambiente filho filtrado (`PGPASSFILE`/`PGSERVICEFILE` apontando para `os.devnull`), argumentos inválidos sem eco, erros sem saída bruta. Adequado. |
| Tratamento dos arquivos | Leitura somente, em blocos. **Defeito corrigido:** o caminho ia no argv do `pg_restore`, convertido para a página ANSI no Windows; com best-fit, o catálogo podia ser lido de **outro** arquivo (falso positivo forjável) e caminhos com caracteres fora do cp1252 ou longos davam falso negativo. Agora o Python reabre o caminho já resolvido logo após o hash e entrega o conteúdo pelo stdin; falha na reabertura dá código 1. Limite: uma troca concorrente do arquivo entre o hash e o `--list` não é detectada (a mesma janela existia antes; ler pelo mesmo handle só a reduziria, pois a escrita no lugar continua possível). |
| Validação de hashes | SHA-256 completo, referência com 64 hex em qualquer caixa; divergência interrompe antes do catálogo. Adequado; o hash só tem valor comparado a manifesto confiável e separado. |
| Arquivos corrompidos | Rejeita vazio, sem assinatura ou truncado no cabeçalho; `--list` detecta TOC truncado. Limite documentado: `--list` não lê blocos de dados, então dados truncados só aparecem no restore. |
| Testes de recuperação | O ensaio restaura e compara todas as linhas, vínculos, sequências, FKs, bytes, hashes e rotas. **Defeito corrigido:** desde a SEC-005 ele falhava no Windows (ver F1). |
| Isolamento | Cluster próprio em diretório temporário, `127.0.0.1` em porta efêmera, SCRAM com credenciais geradas, `unix_socket_directories=''`, processo filho com ambiente filtrado e `dotenv` desativado, asserção de host/porta/usuário/banco antes de qualquer DDL. Nunca usa o serviço preexistente nem URLs externas. Comprovado por leitura e execução. |
| Limpeza | Cluster encerrado e diretório removido em todas as execuções do ensaio e das validações SQL desta etapa (conferido: nenhum `postgres.exe` novo, nenhum diretório novo). Potencial: `TemporaryDirectory` sem tolerância a arquivo ainda aberto no Windows; resta o diretório `%TEMP%\nexo-backup-drill-0ezil1f5` do ensaio de 20/10 (sem processo associado; remoção manual pendente). Resíduos das suítes desta etapa estão em J (ambiente local). |
| Prevenção de restauração acidental | O CLI não aceita URL, banco, `--clean` ou `--create`; o ensaio só restaura em banco recém-criado no próprio cluster. Adequado. |
| Compatibilidade com PostgreSQL | Ensaio validado com cliente e servidor 18.1. A versão do Supabase não é conhecida (G.2). **Defeito corrigido:** utilitário que falhava em `--version` continuava sendo usado e gerava código 1 ("backup não validado") em vez de 3. |

### D.3 Arquivos armazenados e backup (Subetapa 0.2A.6)

- **Armazenamento (Comprovado):** `storage.salvar()` grava os bytes em
  `arquivo_armazenado.conteudo` na **mesma transação** do registro que os cita e devolve
  `'db:'+chave`. O ETL lê o BYTEA direto do banco, com limite de tamanho, para cópia privada
  temporária; `/midia` serve só chaves `guia/` e também lê do banco.
- **Origem dos metadados (`etl_uploads.py`, `etl_runtime.py`):** `ler_upload_validado`
  (`etl_uploads.py:47-83`) lê no máximo o limite configurado, calcula o SHA-256 durante a
  leitura e valida o arquivo em subprocesso isolado (`run_validation`) **antes** de
  `storage.salvar` (`blueprints/cliente.py:345` e `354`; `blueprints/admin.py:663` e `671`).
  `hash_arquivo` e `tamanho_arquivo` vêm dessa leitura (`cliente.py:373`, `admin.py:692`);
  falha de validação não persiste bytes. No Windows a validação é recusada (SEC-005), então
  a persistência de uploads pela rota só é verificável em Linux (E5).
- **Backup:** o BYTEA é dado de tabela e entra no `TABLE DATA` de `arquivo_armazenado` de um
  dump completo; `-b/-B` (large objects) não se aplica. Não usar `--exclude-table-data`
  para reduzir o dump: isso quebra todas as referências `db:`. O restore padrão
  (`--single-transaction`) é serial e não combina com `-j`.
- **Integridade:** sem FK, o banco aceita referência quebrada, blob órfão e chave
  compartilhada. As consultas de G.13 detectam cada caso (TESTADO LOCALMENTE em PG 18.1 com
  anomalias injetadas).
- **Legados:** referências sem `db:` dependem de arquivos em disco fora do dump e do Git; o
  fallback por nome-base pode apontar para o arquivo de outro upload.
- **Cache local:** removido. `storage.resolver()` recusa referência `db:`; os bytes saem de
  `storage.obter()` e a cópia do ETL, de `storage.materializar_para_etl()` (pasta temporária
  exclusiva, apagada ao sair do contexto). Validar restaurações pelas consultas, que leem o
  banco.
- **Volume:** sem retenção dos arquivos brutos; regravar a mesma chave reescreve o BYTEA
  inteiro. Nenhum volume real foi medido (NÃO VERIFICADO).

### D.4 Proteção contra vazamento (Subetapa 0.2A.7)

- `.gitignore` testado com `git check-ignore` em repositório temporário (cópia idêntica do
  arquivo, ignore global neutralizado, `core.ignorecase` true e false). Já protegia `.env`,
  `.env.local`, `.env.*.local` e `.env.*` (preservando `.env.example`), `instance/`, `*.db`,
  `*.sqlite`, `*.sqlite3`, uploads, mídias do Guia, planilhas e PDF em `dados/`
  (`dados/**/*.xlsx`, `*.csv`, `*.xls`, `*.pdf`), `.audit_tmp/`, `.claude/`, `*.zip`, `*.log`
  e os artefatos do roteiro de backup do OPERACAO (`*.dump`, `*.dump.*`, `*.pgpass`,
  `pgpass.conf`, `pg_service.conf`, `backups/`).
- **Lacuna comprovada e corrigida (F6):** arquivos auxiliares do SQLite (`-wal`, `-shm`,
  `-journal`) não eram ignorados, embora `app.py` ative WAL em toda conexão e `config.py`
  aceite SQLite relativo à raiz. Com o app em execução, o `-wal` continha dados ainda
  ausentes do `.db`, e `git add -A` os prepararia para commit.
- Endurecimentos opcionais **não adotados**, por não serem lacunas comprovadas: dumps em
  texto (`*.sql` é preservado de propósito para os DDL de schema), tar/diretório,
  chaves/certificados, planilhas na raiz. O roteiro do OPERACAO gera dump custom fora do
  checkout; o conjunto da CLI (G.6 b) gera SQL em texto com dados de clientes, que o
  `.gitignore` não cobre. Por isso a seção G exige gerar e manter todos os artefatos só no
  volume privado, com caminhos absolutos, e nomeia as listas de TOC como `*.dump.*`.
- Varredura da pasta (somente nomes): não há `.env` real, banco SQLite, dump, backup, chave
  ou log. Varredura de padrões de segredo: nenhuma ocorrência real. Existe a credencial de
  demonstração já registrada (F4.1 da Etapa 0.1, `criar_cliente_demo.py`; valor não
  reproduzido). `.env.example` contém só placeholders vazios.
- Um ZIP da pasta contorna o `.gitignore` (levaria `.env`, bancos locais e uploads, se
  existirem): para compartilhar, preferir `git archive` ou lista de exclusão.

---

## E. Testes

Todos executados nesta máquina, sem acesso externo. Resultado por comando:

| # | Comando / verificação | Resultado |
|---|---|---|
| E1 | `python -B -m unittest tests.test_backup_readiness -v` (estado inicial) | **13 testes, 13 aprovados**, 0,248 s (confirma a análise anterior; relatado, sem log arquivado) |
| E2 | `python -B scripts/verificar_prontidao_backup.py --dry-run` (ambiente como está) | Código **3**: `pg_dump`, `pg_restore` e `psql` indisponíveis no `PATH`. Dependência ausente, comportamento documentado. |
| E3 | O mesmo, com o bin do PostgreSQL 18 acrescentado ao `PATH` só na sessão | `--dry-run`: código 0 (ferramentas localizadas, `--version` não executado). Padrão: código 0 (`--version` dos três concluído). |
| E4 | `tests.test_backup_restore -v` com `NEXO_TEST_POSTGRES_BIN=C:/Program Files/PostgreSQL/18/bin`, **antes** da correção | 5 testes: 4 aprovados, **1 falha** (36,3 s): "Ensaio isolado falhou na etapa comparação completa e relacionamentos (ETLValidationError)". Causa reproduzida sem banco: o ETL da SEC-005 recusa o Windows (`code="platform"`). Relatado, sem log arquivado. |
| E5 | Suíte completa `python -B -m unittest discover -s tests -t . -v`, **antes** de qualquer alteração (linha de base) | **561 testes; 13 falhas, 13 erros, 42 ignorados**, 514,8 s. Nenhuma falha causada por migração, persistência ou backup; a persistência de uploads pela rota real (`test_cliente_uploads_persist_in_database_and_survive_cache_removal`) ficou NÃO VERIFICADA no Windows, porque o upload depende da validação do ETL, recusada pela plataforma (executar em Linux/CI). A alteração F1 em `tests/test_backup_restore.py` começou durante esta execução, depois do carregamento dos módulos; o log mostra os 5 testes originais. Causas: ETL recusado no Windows (uploads e processamento), módulos ausentes (`gunicorn`, `yaml`), versões locais de Werkzeug/Flask/python-dotenv diferentes das fixadas e codificação cp1252. Ignorados: navegador (opt-in), POSIX/gunicorn, `pdftotext`, ensaio sem opt-in. |
| E6 | Módulos de banco/migração/backup (`test_deploy`, `test_company_restore_migration`, `test_company_restore_security`, `test_maintenance`, `test_configuration_deploy`, `test_backup_readiness`, `test_backup_restore`) | 87 testes, num estado intermediário: já com a versão de F1 em `tests/test_backup_restore.py` (7 testes, 2 novos) e com `tests/test_backup_readiness.py` ainda original (13). Migrações (paridade e SEC-004, 5/5), restauração de empresas (32/32), manutenção (7/7), configuração (10/10) aprovados; 1 falha e 2 erros apenas nos testes de `test_deploy` que dependem do ETL no Windows; 1 ignorado (ensaio sem opt-in). No estado final o mesmo comando executa 93 testes (E15 inclui todos). |
| E7 | `tests.test_backup_readiness` após as correções | **18 testes, 18 aprovados** (16 após a primeira rodada; mais 2 na revisão: reabertura do arquivo que falha e executável regular) |
| E8 | `tests.test_backup_restore` após as correções, sem opt-in | 8 testes: 7 aprovados, 1 ignorado com motivo (nenhum cluster iniciado) |
| E9 | O mesmo, com opt-in (PostgreSQL 18.1 local isolado) | **8 testes, 8 aprovados** (29,0 s; 30,3 s de duração total do processo). Relatório: `status ok`, 15 tabelas, 4 uploads, 5 arquivos, 16 verificações aprovadas e a do ETL **não executada** (`not_executed: ["etl"]`; exige Linux), `production_access: false`. O teste pai confere que nenhuma verificação não executada aparece entre as aprovadas e que as 17 estão contabilizadas. Nenhum processo ou diretório residual. |
| E10 | Mutação: cada defeito corrigido ou ramo novo reintroduzido numa cópia fora do projeto | Os 5 mutantes da primeira rodada (busca com diretório atual/`.bat`, caminho no argv, versão reprovada mantida, abertura para escrita, stdout herdado), reaplicados sobre o código final, e 14 novos: `os.path.isabs` sem unidade, entradas relativas aceitas (iniciadas por `.` ou não), reabertura sem tratamento de erro ou tratada como sucesso, sem `X_OK`, sem `isfile`, teste pai sem cada uma das três asserções, plataforma fixa, tolerância também no Linux, qualquer erro do ETL tolerado e, no ensaio real com opt-in, filtro de `checks` removido. **19 de 19 detectados**, cada um pelo teste previsto; versão sem mutação aprovada. No Linux, o mutante de `os.path.isabs` é equivalente ao código (J, cobertura de CI). |
| E11 | CLI corrigido com o `pg_restore.exe` 18.1 real e dumps sintéticos (reexecutado após a revisão) | Válido com espaços: 0. Válido em pasta com `Ω`: 0 (antes 1). TOC truncado em pasta `Ω2` vizinha de válido em pasta `O2` (colisão best-fit): 1 (antes 0, falso positivo). Caminho de 446 caracteres: 0 (antes 1). TOC truncado: 1. `pg_restore.bat` plantado no diretório atual, com `.` no `PATH`: não executado. Entrada do `PATH` sem letra de unidade: ignorada (código 3). |
| E12 | `.gitignore`: `git check-ignore` antes e depois | 8 nomes auxiliares do SQLite passam a ser ignorados (um 9º testado, `instance/nexo_mvp.db-wal`, já era coberto por `instance/`); nenhum dos 258 arquivos do projeto passa a ser ignorado; `.env.example`, os `.sql` de schema e os `.gitkeep` continuam versionáveis. |
| E13 | Consultas de G.11 a G.13 em PostgreSQL 18.1 descartável (dados fictícios, transação somente leitura) | **16/16 verificações**: zero no estado íntegro; cada anomalia injetada (blob órfão, referência quebrada, conteúdo alterado, chave compartilhada, legado, responsável não ADMIN, chamado/remetente de outra empresa, lixeira) detectada com a contagem exata; constraints por tipo iguais às de B.1; impressão digital SQL igual à calculada em Python. Reexecutado após a revisão, com a impressão digital ordenada por `COLLATE "C"`: 16/16. Num banco com collation ICU pt-BR, a versão sem `COLLATE "C"` divergia da ordem por bytes e gerava falso alarme; com ela, coincide. |
| E14 | `pg_dump --schema=public --format=custom` e restauração isolada (PG 18.1 descartável; reexecutado após a revisão) | TOC com `SCHEMA - public`; restauração direta com `--single-transaction --exit-on-error` **falha**; com lista de TOC sem essas entradas, **conclui**, e as 11 consultas de validação são idênticas entre origem e cópia. |
| E15 | Suíte completa após as alterações (mesmo comando de E5, pelo PowerShell) | **569 testes (561 + 8 novos); 13 falhas, 12 erros, 42 ignorados**, 162,4 s. **Nenhuma falha ou erro novo** em relação a E5: as 13 falhas são as mesmas nas duas execuções, inclusive os 4 subtestes de `SessionVaryCompatibilityTests` (Flask local 3.0.3 frente ao 3.1.3 fixado). Só não se repetiu o erro de `test_etl_runtime_limits...test_parent_cancellation_still_terminates_the_owned_child_and_cleans_resources`, que isolado passou 3 de 3 vezes (log arquivado); a linha de base rodou com a auditoria paralela consumindo CPU. A primeira rodada de correções (566 testes) teve o mesmo resultado. Pelo Git Bash, 3 testes de PDF que exigem `pdftotext` deixam de ser ignorados e falham com o `pdftotext` do Git (`ucrt64/bin`): efeito do ambiente, não das alterações (J). |

Não executados, com motivo: as 5 suítes `tests.postgresql_*` (exigem POSIX); testes de
navegador (opt-in `NEXO_TEST_BROWSER=1` e Playwright); testes que exigem `gunicorn`/POSIX; a
verificação do ETL dentro do ensaio de backup no Windows (SEC-005; registrada como não
executada). Nenhum teste ignorado nem verificação não executada foi contado como aprovado.

Evidências arquivadas fora do repositório, em pasta privada da etapa, sem dados reais:
logs de E5, E6, E7, E8, E9, E15 e da reexecução isolada citada em E15; saídas de E2, E3 e
E11 (cenários do CLI), E13, E14 e da validação de collation; resultados da mutação (E10).
E1 e E4 são relatados sem log arquivado (execuções anteriores às correções).

---

## F. Correções

Problemas realmente corrigidos, com a menor mudança segura. Todas as alterações de código
têm teste correspondente, conferido por mutação (E10).

| # | Arquivo | O que mudou | Por quê / problema resolvido | Validação | Situação |
|---|---|---|---|---|---|
| F1 | `tests/test_backup_restore.py` | `_etl_or_platform_refusal` tolera **somente** a recusa `code="platform"` do ETL; o relatório ganha `not_executed`; `_acceptable_not_executed` aceita apenas `["etl"]`, nunca no Linux, e recebe a plataforma na chamada; o teste pai confere que nenhuma verificação não executada aparece em `checks` e que as 17 estão contabilizadas; 3 testes novos | O ensaio documentado para Windows falhava desde a SEC-005, que recusa o ETL fora de POSIX por desenho (`Docs/RELATORIO_NEXO_SEC005_ETL_SEGURO.md:372`). A verificação passa a ser registrada como **não executada**, nunca como aprovada; bytes, hashes, restauração e rotas continuam obrigatórios; no Linux (CI) o ETL segue obrigatório. | E4 → E9; E10; reconstrução do original confirmou que só esses trechos mudaram | IMPLEMENTADO; TESTADO LOCALMENTE (ETL do ensaio NÃO VERIFICADO no Windows) |
| F2 | `scripts/verificar_prontidao_backup.py` | `locate_tool()`: busca só em diretórios absolutos do `PATH` (`Path.is_absolute()`, que no Windows exige letra de unidade ou UNC) e, no Windows, só `.exe`; `import shutil` removido | Utilitário plantado no diretório atual era executado; `.bat/.cmd` permitia injeção via `cmd.exe`; entrada `\dir` dependia da unidade atual | E10, E11, testes `test_locate_tool_ignores_current_directory_relative_entries_and_wrappers` e `test_locate_tool_requires_an_executable_regular_file` | IMPLEMENTADO; TESTADO LOCALMENTE |
| F3 | idem | `--list` reabre o caminho já resolvido e entrega o conteúdo ao `pg_restore` pelo stdin (`run_local_command(..., stdin=...)`); falha na reabertura dá código 1 | Argv ANSI/best-fit no Windows: catálogo de outro arquivo ou falso negativo. Limite documentado: troca concorrente do arquivo após o hash não é detectada | E10, E11, teste de invocação com conferência de `stdin`, `shell`, `DEVNULL` e timeout; `test_backup_unavailable_when_reopened_for_listing_is_not_validated` | IMPLEMENTADO; TESTADO LOCALMENTE |
| F4 | idem | Utilitário que falha em `--version` vira indisponível | Código 1 indevido ("backup não validado") em vez de 3 | Teste `test_failed_version_check_marks_tool_unavailable_and_skips_listing` | IMPLEMENTADO; TESTADO LOCALMENTE |
| F5 | `tests/test_backup_readiness.py` | Mocks passam de `shutil.which` para `locate_tool`; invocação do `--list` verificada por inteiro; testes novos de busca (entradas relativas efetivas, `PATH` sem unidade, executável regular), versão reprovada, reabertura que falha e arquivo somente leitura | Os testes antigos não detectavam stdout herdado no `--list`, busca no diretório atual nem abertura para escrita; a revisão mostrou ramos novos ainda sem teste | E7, E10 | IMPLEMENTADO; TESTADO LOCALMENTE |
| F6 | `.gitignore` | Bloco `*.db-wal`, `*.db-shm`, `*.db-journal`, `*.sqlite*-wal`, `*.sqlite*-shm`, `*.sqlite*-journal` | Lacuna real: páginas com dados do SQLite em WAL podiam ir ao controle de versão | E12 | IMPLEMENTADO; TESTADO LOCALMENTE |
| F7 | `Docs/OPERACAO_BACKUP_RESTAURACAO.md` | Nota datada de head/8 revisões; `geracao_autorizacao`; limites de `migrar_arquivos_para_db.py`; órfãos e vínculos semânticos; fallback por nome-base; cache; ACL no Windows; comportamento revisado do CLI, limite da reabertura e precedência dos códigos; políticas RLS na restauração; ETL e suítes POSIX no ensaio; downgrades destrutivos completos; descarte; reexecução de 24/10; caminho local anonimizado, com marca no texto | Divergências comprovadas com o código atual; registros históricos preservados, com notas datadas, exceto a anonimização do caminho local no registro de 20/10 (§11), marcada no próprio texto | Revisão contra o código e os testes desta etapa | IMPLEMENTADO (documentação) |
| F8 | `Docs/PREPARACAO_RENDER.md` | §5 com ordem segura (Auto-Deploy controlado, `current` × `heads` sempre, janela com escritores parados, SHA aprovado); descrição de `DIRECT_URL` (Session pooler/IPv4); higiene de segredos no Passo 1 | O roteiro anterior (push → upgrade → "latest commit") podia publicar código antes da migração e não considerava a exigência da `e4c004a26b01` | Revisão contra `render.yaml`, `app.py`, migração e documentação oficial do Supabase | IMPLEMENTADO (documentação) |
| F9 | `Docs/PLANO_PUBLICACAO_SEGURA.md` | Uma nota datada | O plano condicionava a publicação à ausência de migração e mantinha candidato superado | Revisão | IMPLEMENTADO (documentação) |
| F10 | `Docs/REESTRUTURACAO_COMERCIAL/00_02_PROTECAO_BANCO_MIGRACOES.md` | Novo | Documento de referência desta etapa | Revisão adversarial independente (seção A) | IMPLEMENTADO (documento novo) |

Problemas encontrados e **não** corrigidos por estarem fora do escopo autorizado estão na
seção J.

---

## G. Procedimento para backup real do Supabase e restauração isolada

**Situação: DOCUMENTADO — preparado, não executado.** Nenhum passo abaixo foi executado
contra o Supabase. Foram TESTADOS LOCALMENTE, em PostgreSQL 18.1 descartável com dados
fictícios, apenas: o dump custom do schema `public`, a restauração com lista de TOC (G.10),
as consultas de G.11 a G.13 e o verificador de prontidão (G.7). Fontes oficiais consultadas
em 24/10/2026 estão ao final da seção; onde houver inferência, ela está marcada.

Pré-condições: autorização registrada (responsável, janela, escopo), estação Windows
privada, volume criptografado fora do checkout e de pastas sincronizadas. Executar com o
diretório atual no volume privado da execução (nos exemplos, `D:/backups-nexo/EXECUCAO_UTC`)
todos os comandos que gravam artefatos (dump, SQL, TOC, lista, log), com caminho absoluto: o
`.gitignore` não cobre `*.sql`. O verificador (G.7) e os comandos `flask` (G.12) não gravam
artefatos e rodam a partir do checkout, também com caminhos absolutos para os artefatos.
Nunca usar o serviço PostgreSQL local da porta 5432, `DATABASE_URL`/`DIRECT_URL` da
aplicação nem o disco do Render.

**G.1 Identificar o projeto de origem.** Copiar host, usuário e porta pelo botão
**Connect** do painel; não montar o host do pooler à mão (o índice do cluster varia). O
*project ref* aparece em `db.<ref>.supabase.co` e no usuário `postgres.<ref>` do pooler.
Registrar organização, região e plano em local privado. Confirmar no painel se há buckets
do Storage: o código do NEXO não usa Storage (os arquivos estão no banco), mas o inventário
real é PENDENTE DE VALIDAÇÃO.

**G.2 Identificar a versão do PostgreSQL.** Já conectado: `SELECT version();`,
`SHOW server_version;` e `SHOW server_version_num;`. Registrar também extensões
(`SELECT extname, extversion FROM pg_extension ORDER BY 1;`). O Postgres 17 é o padrão
atual da plataforma; a versão do projeto do NEXO é NÃO VERIFICADO.

**G.3 Ferramentas.** `pg_dump`, `pg_restore` e `psql` com versão principal **igual ou maior**
que a do servidor (o `pg_dump` recusa servidor mais novo; a saída não tem garantia de
carregar em servidor mais antigo). Os binários 18.1 locais servem para dump de servidor 15
ou 17 e para restaurar em cluster local 18; para restaurar num projeto Supabase 17, gerar o
dump com cliente 17 ou pela CLI. Chamar sempre pelo caminho absoluto
(`& 'C:\Program Files\PostgreSQL\18\bin\pg_dump.exe' ...`). A Supabase CLI
(`supabase db dump`) executa o `pg_dump` em container (exige Docker) e exclui os schemas
gerenciados; é o caminho oficial para recuperar num **novo projeto Supabase**.

**G.4 Conexão apropriada.** Para `pg_dump`: conexão direta (`db.<ref>.supabase.co:5432`,
IPv6; IPv4 só com add-on) ou **Session pooler** (porta 5432, usuário `postgres.<ref>`,
IPv4). Os guias oficiais de backup usam o Session pooler por padrão. **Nunca** o modo
transação (6543): estado de sessão não sobrevive entre transações. TLS com
`sslmode=verify-full` e `sslrootcert` apontando para o certificado baixado em *Database
Settings → SSL Configuration*; `require` não impede ataque intermediário. Se houver
*Network Restrictions*, liberar o IP de saída.

**G.5 Proteger as credenciais.** Arquivo de serviço e passfile privados, fora do
repositório, apontados por `PGSERVICEFILE` e `PGPASSFILE` (padrões no Windows:
`%APPDATA%\postgresql\.pg_service.conf` e `%APPDATA%\postgresql\pgpass.conf`). No Windows a
libpq **não** verifica permissões do passfile: restringir com ACL NTFS (`icacls`, ver
`Docs/OPERACAO_BACKUP_RESTAURACAO.md`, §3 e §4). Não usar senha em argumento, `PGPASSWORD`,
histórico do PowerShell ou URL na linha de comando. Opcional e sujeito a autorização (cria
objeto em produção): papel dedicado de leitura (`backup_reader`, com `pg_read_all_data`,
`BYPASSRLS` e `default_transaction_read_only`), conforme o guia oficial; ele lê todos os
schemas, inclusive `auth` e `storage`.

Exemplo **fictício** de serviço (sem senha):

```ini
[nexo_origem]
host=HOST_COPIADO_DO_CONNECT
port=5432
dbname=postgres
user=postgres.PROJECT_REF
sslmode=verify-full
sslrootcert=D:/cofre-nexo/supabase-ca.crt
connect_timeout=15
```

**G.6 Gerar um backup consistente.** O `pg_dump` exporta um snapshot consistente do início da
execução sem bloquear leitores nem escritores; evitar DDL e migrações durante o dump. Dois
artefatos complementares:

- **(a) Schema da aplicação, formato custom** — para restauração isolada em PostgreSQL
  comum e validação dos BYTEA (inferência apoiada na documentação do `pg_dump`; testado
  localmente em PG 18):

  ```powershell
  $env:PGSERVICEFILE = 'D:/cofre-nexo/pg_service.conf'
  $env:PGPASSFILE = 'D:/cofre-nexo/pgpass.conf'
  & 'C:\Program Files\PostgreSQL\18\bin\pg_dump.exe' --no-password `
      --dbname='service=nexo_origem' --schema=public --format=custom `
      --file='D:/backups-nexo/EXECUCAO_UTC/nexo_public.dump'
  if ($LASTEXITCODE -ne 0) { throw 'pg_dump falhou' }
  ```

  Com `--schema`, o `pg_dump` não leva dependências de outros schemas. As 14 tabelas e
  `alembic_version` ficam em `public` (o código não define outro schema; confirmar no
  inventário real).

- **(b) Conjunto oficial da CLI** — para recuperação num novo projeto Supabase:
  `supabase db dump --db-url 'URL_DO_PROJETO' -f 'D:/backups-nexo/EXECUCAO_UTC/roles.sql' --role-only`,
  `supabase db dump --db-url 'URL_DO_PROJETO' -f 'D:/backups-nexo/EXECUCAO_UTC/schema.sql'` e
  `supabase db dump --db-url 'URL_DO_PROJETO' -f 'D:/backups-nexo/EXECUCAO_UTC/data.sql' --use-copy --data-only`
  (o guia atual também exclui `storage.buckets_vectors` e `storage.vector_indexes`). São SQL
  em texto com todas as linhas, inclusive `senha_hash` e BYTEA: nunca gerá-los no checkout. A
  URL com senha fica visível no histórico e na lista de processos: usar sessão efêmera ou
  `supabase link`.

Opcional: para comparar origem e cópia no mesmo instante, abrir uma sessão
`BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT pg_export_snapshot();`, rodar nela as
consultas de G.11 a G.13 e executar o `pg_dump --snapshot=ID_DO_SNAPSHOT` com a transação
aberta.

**G.7 Conferir a integridade do backup.** Exigir código 0 e log sem erro. Calcular o SHA-256
(`Get-FileHash -Algorithm SHA256`) e guardá-lo em manifesto separado e protegido. Gerar o
TOC sem conectar:
`& 'C:\Program Files\PostgreSQL\18\bin\pg_restore.exe' --list --file='D:/backups-nexo/EXECUCAO_UTC/nexo_public.dump.toc' 'D:/backups-nexo/EXECUCAO_UTC/nexo_public.dump'`
(`--file` evita a codificação do redirecionamento do PowerShell; o sufixo `.dump.toc` é
coberto pelo `.gitignore`). O TOC deve listar `TABLE DATA` das 14 tabelas e de
`alembic_version`, além de `SEQUENCE SET`, `CONSTRAINT`, `FK CONSTRAINT` e `INDEX`. O
verificador local faz hash, assinatura e `--list` sem conectar:
`python -B 'CAMINHO_DO_CHECKOUT/scripts/verificar_prontidao_backup.py' --backup 'D:/backups-nexo/EXECUCAO_UTC/nexo_public.dump' --sha256 HASH_SHA256_CONFIAVEL --list`,
com `C:\Program Files\PostgreSQL\18\bin` no `PATH` só desta sessão (o verificador procura os
utilitários apenas no `PATH` e, sem isso, devolve 3; OPERACAO §6).
Checksum e TOC **não** provam que o backup restaura; só G.10 a G.13 provam.

**G.8 Armazenar fora do GitHub.** Criptografar antes de sair da estação (por exemplo,
`gpg --encrypt` para chave pública com fingerprint verificada), guardar em destino privado
com versionamento, retenção imutável e segunda cópia independente; chave privada em cofre
separado; testar a descriptografia. O guia oficial de backup com GitHub Actions grava os
dumps no próprio repositório: **não adotar** no NEXO. Nunca anexar dump, TOC ou log a PR,
issue, CI ou e-mail. No plano Free não há backup automático nem PITR; nos planos pagos os
backups físicos diários não podem ser baixados, então a cópia externa depende sempre de
dump lógico próprio. O plano do NEXO é NÃO VERIFICADO.

**G.9 Proteger os dados dos clientes.** O dump contém hashes de senha, planilhas (BYTEA),
chamados e dados de empresas, inclusive das que estão na lixeira. Compressão do formato
custom não é criptografia. Acesso restrito ao responsável, estação sem compartilhamento,
nenhuma cópia em pastas sincronizadas sem proteção, descarte conforme política aprovada.

**G.10 Restaurar em ambiente isolado.**

- **PostgreSQL comum (testado localmente):** cluster novo em diretório próprio, porta livre
  diferente de 5432, `listen_addresses` em loopback, versão igual ou maior que a da origem,
  banco criado a partir de `template0`, serviço e passfile **só do destino**. O dump de
  `--schema=public` contém as entradas `SCHEMA - public` e `COMMENT - SCHEMA public`, que
  colidem com o `public` já existente: a restauração direta com `--exit-on-error` **falha**.
  Gerar a lista, comentar essas duas linhas com `;` e restaurar com ela:

  ```powershell
  Set-Location 'D:/backups-nexo/EXECUCAO_UTC'
  & 'C:\Program Files\PostgreSQL\18\bin\pg_restore.exe' --list --file=nexo_public.dump.list nexo_public.dump
  # editar nexo_public.dump.list: prefixar com ';' as linhas "SCHEMA - public" e "COMMENT - SCHEMA public"
  & 'C:\Program Files\PostgreSQL\18\bin\pg_restore.exe' --no-password `
      --dbname='service=nexo_restore_isolado' --use-list=nexo_public.dump.list `
      --single-transaction --exit-on-error --no-owner --no-privileges nexo_public.dump
  ```

  Depois, `ANALYZE`. `--no-owner --no-privileges` facilita o isolamento e não reproduz donos
  nem grants de produção; `--no-privileges` só ignora GRANT/REVOKE. Políticas RLS presentes
  no dump são restauradas e, se citarem papéis ou funções do Supabase inexistentes no destino
  (`anon`, `authenticated`, `auth.uid()`), a restauração falha. Nesse caso, acrescentar
  `--no-policies` (`pg_restore` 18) ou comentar na lista as entradas `POLICY`, e registrar em
  G.14 que a cópia não tem as políticas de produção. O ensaio local (E14) partiu de banco
  sem políticas e não cobre esse caso.

- **Novo projeto Supabase (oficial):**
  `psql --single-transaction --variable ON_ERROR_STOP=1 --file roles.sql --file schema.sql --command 'SET session_replication_role = replica' --file data.sql --dbname 'URL_DO_PROJETO_NOVO'`.
  Antes, avaliar `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;`
  (aviso oficial do `db dump`); depois, redefinir senhas de papéis com LOGIN e revisar
  grants e RLS antes de liberar qualquer acesso. O "restore to a new project" do painel só
  existe em planos pagos e não copia o Storage.

- Em qualquer opção: desligar integrações de saída (Brevo/SMTP, IA); não rodar seed,
  `create_all`, `stamp`, `upgrade` ou `downgrade` sobre a cópia; após a validação, reaplicar
  as exclusões definitivas posteriores ao backup antes de liberar acesso ou e-mail (não há
  trilha automática; ver I).

**G.11 Conferir as tabelas restauradas** (só números). Consultas validadas em PG 18.1:

```sql
-- Tabelas: esperado plano, segmento, empresa, usuario, analise, upload_relatorio,
-- indicador_analise, relatorio_analise, produto_curva_abc, chamado_suporte,
-- mensagem_suporte, notificacao, guia_topico, arquivo_armazenado e alembic_version.
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY 1;

-- Contagens por tabela: comparar com a origem no mesmo snapshot.
SELECT 'alembic_version' AS tabela, count(*) AS linhas FROM alembic_version
UNION ALL SELECT 'analise', count(*) FROM analise
UNION ALL SELECT 'arquivo_armazenado', count(*) FROM arquivo_armazenado
UNION ALL SELECT 'chamado_suporte', count(*) FROM chamado_suporte
UNION ALL SELECT 'empresa', count(*) FROM empresa
UNION ALL SELECT 'guia_topico', count(*) FROM guia_topico
UNION ALL SELECT 'indicador_analise', count(*) FROM indicador_analise
UNION ALL SELECT 'mensagem_suporte', count(*) FROM mensagem_suporte
UNION ALL SELECT 'notificacao', count(*) FROM notificacao
UNION ALL SELECT 'plano', count(*) FROM plano
UNION ALL SELECT 'produto_curva_abc', count(*) FROM produto_curva_abc
UNION ALL SELECT 'relatorio_analise', count(*) FROM relatorio_analise
UNION ALL SELECT 'segmento', count(*) FROM segmento
UNION ALL SELECT 'upload_relatorio', count(*) FROM upload_relatorio
UNION ALL SELECT 'usuario', count(*) FROM usuario
ORDER BY 1;

-- Constraints por tipo (comparar entre servidores da mesma versão principal).
SELECT c.contype AS tipo, count(*) AS quantidade
FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE n.nspname = 'public'
GROUP BY c.contype
ORDER BY 1;

-- Sequências: ultimo_valor_sequencia deve ser maior ou igual a maior_id.
WITH pk(tabela, coluna) AS (VALUES
  ('analise', 'id_analise'), ('chamado_suporte', 'id_chamado'), ('empresa', 'id_empresa'),
  ('guia_topico', 'id'), ('indicador_analise', 'id_indicador'), ('mensagem_suporte', 'id_mensagem'),
  ('notificacao', 'id_notificacao'), ('plano', 'id_plano'), ('produto_curva_abc', 'id_produto_abc'),
  ('relatorio_analise', 'id_relatorio'), ('segmento', 'id_segmento'), ('upload_relatorio', 'id_upload'),
  ('usuario', 'id_usuario'))
SELECT pk.tabela,
       (xpath('/row/m/text()', query_to_xml(
           format('SELECT max(%I) AS m FROM public.%I', pk.coluna, pk.tabela), false, true, '')))[1]::text::bigint
         AS maior_id,
       s.last_value AS ultimo_valor_sequencia
FROM pk
LEFT JOIN pg_sequences s
  ON format('%I.%I', s.schemaname, s.sequencename) = pg_get_serial_sequence('public.' || pk.tabela, pk.coluna)
ORDER BY 1;
```

**G.12 Verificar a revisão Alembic.** `SELECT version_num FROM alembic_version;` deve
retornar exatamente uma linha, igual à da origem no momento do snapshot. A head do código
é `e4c004a26b01`; produção pode estar atrás, porque o build do Render não migra. Antes de
qualquer publicação, comparar, a partir do checkout, `flask db current` (somente leitura,
contra o banco autorizado) com `flask db heads` (local). Nunca usar `flask db stamp` nem editar
`alembic_version` para esconder divergência.

**G.13 Validar os arquivos armazenados** (consultas somente leitura, saída numérica,
validadas em PG 18.1 com anomalias injetadas):

```sql
-- Blobs: tamanho_divergente e vazios devem ser 0.
SELECT count(*) AS arquivos,
       coalesce(sum(octet_length(conteudo)), 0) AS bytes_total,
       coalesce(max(octet_length(conteudo)), 0) AS maior_arquivo,
       count(*) FILTER (WHERE octet_length(conteudo) <> tamanho) AS tamanho_divergente,
       count(*) FILTER (WHERE octet_length(conteudo) = 0) AS vazios
FROM arquivo_armazenado;

-- Planilhas: refs_quebradas, sha256_divergente e tamanho_divergente devem ser 0.
SELECT count(*) AS refs_db,
       count(*) FILTER (WHERE a.chave IS NULL) AS refs_quebradas,
       count(*) FILTER (WHERE a.chave IS NOT NULL AND u.hash_arquivo IS NOT NULL
                          AND encode(sha256(a.conteudo), 'hex') <> u.hash_arquivo) AS sha256_divergente,
       count(*) FILTER (WHERE a.chave IS NOT NULL AND u.tamanho_arquivo IS NOT NULL
                          AND u.tamanho_arquivo <> octet_length(a.conteudo)) AS tamanho_divergente,
       count(*) FILTER (WHERE u.hash_arquivo IS NULL OR u.tamanho_arquivo IS NULL) AS sem_hash_ou_tamanho
FROM upload_relatorio u
LEFT JOIN arquivo_armazenado a ON a.chave = substr(u.caminho_arquivo, 4)
WHERE u.caminho_arquivo LIKE 'db:%';

-- Imagens do Guia: refs_quebradas deve ser 0.
SELECT count(*) AS refs_db,
       count(*) FILTER (WHERE a.chave IS NULL) AS refs_quebradas
FROM guia_topico g
LEFT JOIN arquivo_armazenado a ON a.chave = substr(g.imagem_url, 4)
WHERE g.imagem_url LIKE 'db:%';

-- Órfãos, compartilhados e legados: exigem justificativa registrada se forem maiores que 0.
SELECT
  (SELECT count(*) FROM arquivo_armazenado a
    WHERE NOT EXISTS (SELECT 1 FROM upload_relatorio u WHERE u.caminho_arquivo = 'db:' || a.chave)
      AND NOT EXISTS (SELECT 1 FROM guia_topico g WHERE g.imagem_url = 'db:' || a.chave)) AS blobs_orfaos,
  (SELECT count(*) FROM (SELECT caminho_arquivo FROM upload_relatorio WHERE caminho_arquivo LIKE 'db:%'
                         GROUP BY caminho_arquivo HAVING count(*) > 1) s) AS planilhas_compartilhadas,
  (SELECT count(*) FROM upload_relatorio WHERE caminho_arquivo NOT LIKE 'db:%') AS uploads_legados,
  (SELECT count(*) FROM guia_topico
    WHERE imagem_url IS NOT NULL AND imagem_url <> '' AND imagem_url NOT LIKE 'db:%') AS guia_legado;

-- Impressão digital: rodar na origem (mesmo snapshot) e na cópia e comparar só o hexadecimal.
-- COLLATE "C" fixa a ordem por bytes, sem depender da collation de cada banco.
SELECT encode(sha256(convert_to(coalesce(
         string_agg(chave || ':' || encode(sha256(conteudo), 'hex'), E'\n' ORDER BY chave COLLATE "C"), ''), 'UTF8')), 'hex')
       AS impressao_digital
FROM arquivo_armazenado;

-- Vínculos que o banco não garante: os quatro primeiros devem ser 0.
SELECT
  (SELECT count(*) FROM analise a JOIN usuario u ON u.id_usuario = a.id_usuario_admin_responsavel
    WHERE u.role <> 'ADMIN') AS responsavel_nao_admin,
  (SELECT count(*) FROM upload_relatorio r JOIN usuario u ON u.id_usuario = r.id_usuario_admin
    WHERE u.role <> 'ADMIN') AS homologador_nao_admin,
  (SELECT count(*) FROM chamado_suporte c JOIN usuario u ON u.id_usuario = c.id_usuario_cliente
    WHERE u.role <> 'CLIENTE' OR u.id_empresa IS DISTINCT FROM c.id_empresa) AS chamado_cliente_divergente,
  (SELECT count(*) FROM mensagem_suporte m
     JOIN chamado_suporte c ON c.id_chamado = m.id_chamado
     JOIN usuario u ON u.id_usuario = m.id_usuario_remetente
    WHERE NOT (u.role = 'ADMIN' OR (u.role = 'CLIENTE' AND u.id_empresa = c.id_empresa))) AS remetente_divergente,
  (SELECT count(*) FROM empresa WHERE deletado_em IS NOT NULL) AS empresas_na_lixeira;
```

Executar com `psql -X -v ON_ERROR_STOP=1` dentro de `BEGIN TRANSACTION READ ONLY; ...; ROLLBACK;`,
somente na cópia isolada; na origem, apenas com autorização. Calcular `sha256` de todos os
BYTEA lê todo o conteúdo: fazer fora do horário de uso. `sha256()` é nativa a partir do
PostgreSQL 11. Critério de aprovação: zeros onde indicado, impressão digital idêntica,
contagens iguais às da origem e leitura funcional pela aplicação isolada (em Linux, para
incluir o ETL). Referências legadas exigem cópia física separada e conferência do SHA-256
com `hash_arquivo`; antes de qualquer `migrar_arquivos_para_db.py --apply`, listar nomes-base
repetidos e hashes ausentes.

**G.14 Registrar evidências sem revelar informações privadas.** Registrar em local privado:
data/hora UTC, operador, autorização, região, versões do servidor e dos clientes, plano e
PITR, comandos com placeholders, nome, tamanho e SHA-256 do arquivo, contagem de entradas do
TOC por tipo, duração do dump e do restore (RTO medido), `version_num`, contagens por
tabela, resultados das consultas e a impressão digital. **Não** registrar senhas, URLs de
conexão, tokens, conteúdo de linhas, e-mails, CNPJ/CPF, chaves de arquivos (contêm ids de
análise), TOC ou logs brutos em local público nem o IP do operador; mascarar o *project
ref* em registros compartilhados. No repositório, somente agregados sintéticos.

**Fontes oficiais consultadas (24/10/2026):** Supabase — [Backups](https://supabase.com/docs/guides/platform/backups),
[Backup and restore using the CLI](https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore),
[Connecting to Postgres](https://supabase.com/docs/guides/database/connecting-to-postgres),
[IPv4 address](https://supabase.com/docs/guides/platform/ipv4-address),
[SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement),
[`supabase db dump`](https://supabase.com/docs/reference/cli/supabase-db-dump),
[pg_dump e RLS (backup_reader)](https://supabase.com/docs/guides/troubleshooting/pg_dump-fails-with-query-would-be-affected-by-row-level-security-policy-1d5783),
[Migrating to Supabase (Postgres)](https://supabase.com/docs/guides/platform/migrating-to-supabase/postgres),
[Tabelas novas não expostas automaticamente à Data API](https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically),
[Restrições em auth, storage e realtime](https://supabase.com/changelog/34270-restricting-access-on-auth-storage-and-realtime-schemas-on-april-21-2025).
PostgreSQL — [pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html),
[SQL Dump](https://www.postgresql.org/docs/current/backup-dump.html),
[passfile](https://www.postgresql.org/docs/current/libpq-pgpass.html),
[serviço de conexão](https://www.postgresql.org/docs/current/libpq-pgservice.html),
[SSL na libpq](https://www.postgresql.org/docs/current/libpq-ssl.html).
As páginas do Supabase não exibem data de atualização; reconferir antes da execução real.

---

## H. Planejamento das futuras migrations

**Situação: DOCUMENTADO.** Nenhuma tabela, coluna ou revisão foi criada; `models.py` não foi
alterado.

### H.1 Estruturas previstas e estratégia

| Necessidade (etapas futuras) | Estratégia recomendada |
|---|---|
| Controle de recursos dos planos Bronze, Prata e Ouro (E4) | Tabela **aditiva** de recursos por plano (por exemplo, `plano_recurso` com `id_plano`, código do recurso, habilitado e limite), preenchida por migração de dados idempotente. Evitar alterar a CHECK de `nome_plano` (nome gerado `plano_nome_plano_check` no PG; recriação batch no SQLite falha com dados). Não depender de `seed.py`, que não atualiza linhas existentes. Decidir explicitamente entre plano atual da empresa e plano de referência da análise. |
| Avaliações dos clientes (E5) | Tabela própria com FK para `analise` e para o usuário autor; UNIQUE explícita quando houver regra "uma por análise/usuário". |
| Comentários públicos e sugestões privadas (E5) | Separar por tipo/visibilidade com CHECK nomeada, ou tabelas distintas; nunca expor sugestão privada por rota pública. |
| Moderação administrativa (E6) | Estado de moderação com CHECK nomeada, moderador (FK para ADMIN), data e motivo; transições registradas. |
| Registro de consentimento | Tabela de consentimentos **append-only**: texto/versão do termo, finalidade, data UTC, origem e revogação. É evidência: não apagar em cascata com o usuário sem decisão jurídica; guardar o necessário para prova com dado mínimo. |
| Histórico de publicação | Tabela de eventos append-only (publicado, despublicado, editado), com autor e data; sem `UPDATE` destrutivo. |

A partir de 30/10/2026 (padrão em projetos novos desde 30/05/2026), o Supabase deixará de
expor automaticamente à Data API as tabelas novas do schema `public` também nos projetos
existentes; o NEXO usa conexão direta (SQLAlchemy) e não depende disso, mas o estado de RLS
e grants das tabelas existentes é PENDENTE DE VALIDAÇÃO.

### H.2 Regras para cada nova revisão Alembic

1. Encadear a partir da head única (hoje `e4c004a26b01`) e conferir `flask db heads` com uma
   só head; nunca editar revisões existentes.
2. Preferir mudanças **aditivas** (expandir → migrar dados → código tolerante aos dois
   estados → contrair em revisão posterior). Coluna nova NOT NULL só com `server_default`
   válido nos dois bancos ou em duas etapas.
3. Booleanos: `server_default=sa.false()`/`sa.true()` (gera `0`/`1` no SQLite e
   `false`/`true` no PG), nunca `'false'`/`'true'` em string; `op.add_column` sem `batch`.
4. Nomear **toda** constraint e índice novos (`ck_`, `fk_`, `uq_`, `ix_`). Adotar
   `naming_convention` global só com revisão dedicada e testada, porque altera a comparação
   das constraints existentes.
5. Não usar `batch` que recrie `plano`, `usuario`, `empresa` ou `analise` no SQLite com dados;
   se inevitável, escrever a migração por dialeto (PG: `ALTER ... CONSTRAINT` pelo nome real
   da seção B.1) e documentar o procedimento SQLite.
6. Valores monetários com `Numeric(precisão, escala)`; datas com política de fuso definida.
7. Toda FK nova com **política de exclusão explícita**: incluir a tabela na exclusão
   permanente de empresa e no cascade de `Analise` (ou `ON DELETE` com
   `passive_deletes=True`), com teste que exclua empresa e análise com dados nas tabelas
   novas. Para FK de autor (usuário) em dados que devem sobreviver ao usuário (consentimento,
   moderação, histórico), preferir coluna anulável ou anonimização a cascata.
8. Docstring da revisão com a perda de dados do downgrade; migrações de dados idempotentes e
   separadas do DDL quando possível.
9. Migrações PostgreSQL com `lock_timeout` e `statement_timeout` definidos na sessão
   (recomendação para quando `env.py` puder mudar) e janela de baixa atividade.

### H.3 Testes obrigatórios por revisão

- `upgrade` em SQLite vazio e paridade (`tests/test_deploy.py`, `compare_metadata == []`),
  acrescida de comparação de CHECK.
- `upgrade` a partir da head anterior **com linhas preexistentes inseridas por SQL bruto**
  (padrão de `tests/test_company_restore_migration.py`), lendo pelo ORM as colunas novas.
- Ensaio em PostgreSQL privado (`tests/test_backup_restore.py` no Windows ou Linux;
  `tests.postgresql_*` em Linux/CI), incluindo falha simulada no meio da migração.
- Atualizar o contrato de 15 tabelas, as fixtures e as consultas de G.11 a G.13 para as
  tabelas novas.
- Downgrade testado só em banco descartável; nunca usado como rollback em produção.

### H.4 Preservação de dados e recuperação

- **Antes** de cada migração em produção: backup verificado (G.6 e G.7), restauração
  isolada aprovada (G.10 a G.13), `flask db current` × `heads` registrado, plano de
  interrupção e responsável definidos.
- **Dados anteriores:** consultas de pré-validação que contem linhas que violariam a regra
  nova (NOT NULL, CHECK, UNIQUE) antes de aplicá-la; no PG, constraints grandes podem usar
  `NOT VALID` seguido de `VALIDATE CONSTRAINT`.
- **Arquivos armazenados:** não alterar `arquivo_armazenado` nem o formato das chaves; rodar
  G.13 antes e depois.
- **Conflitos de schema:** uma head só; não gerar `autogenerate` contra banco fora de
  sincronia; revisar à mão todo bloco `batch` gerado.
- **Ordem de implantação:** Auto-Deploy controlado; migração aditiva aplicada e conferida
  antes do código que a usa; publicação manual do SHA aprovado (§5 de
  `Docs/PREPARACAO_RENDER.md`).
- **Em caso de erro:** preferir correção para frente com revisão nova que preserve dados;
  voltar só o código se compatível com o schema; restaurar em **banco separado** e
  reconciliar gravações posteriores ao snapshot. Downgrade só após revisão do SQL, teste em
  cópia autorizada e aceitação explícita das perdas.

---

## I. Segurança

| # | Risco | Recomendação | Situação |
|---|---|---|---|
| I1 | Credencial de demonstração versionada em `criar_cliente_demo.py` (F4.1 da Etapa 0.1); o script também converte ADMIN em CLIENTE quando o e-mail coincide | Remover o literal, exigir senha por variável ou entrada interativa, travar contra PostgreSQL, verificar se a conta existe em produção e desativá-la/rotacioná-la | PENDENTE DE VALIDAÇÃO |
| I2 | RLS, grants e exposição das tabelas do `public` pela Data API do Supabase desconhecidos; tabelas com `senha_hash` e BYTEA | Inventário autorizado de RLS/grants; revogar `anon`/`authenticated` se não usados | PENDENTE DE VALIDAÇÃO |
| I3 | Resíduos de exclusão: notificações de ADMIN, lixeira sem prazo, e-mails a empresa na lixeira, sem trilha de exclusões | Registro operacional de exclusões definitivas fora do banco e do Git (data UTC, tipo, id, hash do CNPJ, operador, solicitação), reaplicado após restauração; decisão sobre prazo da lixeira e retenção de backups | PENDENTE DE VALIDAÇÃO (decisão do responsável) |
| I4 | ACL herdada no Windows: a raiz desta pasta concede leitura a `Usuários` e modificação a `Usuários autenticados`; um `.env` ou SQLite local herdaria isso | Manter o checkout sob o perfil do usuário ou restringir `.env` e `instance/` com `icacls /inheritance:r` | DOCUMENTADO |
| I5 | Segredos em comandos de shell (exemplos com `export` de valores reais) | Nota de higiene em `Docs/PREPARACAO_RENDER.md`; carregar de arquivo protegido ou gerenciador de segredos | IMPLEMENTADO (documentação) |
| I6 | `migrations/env.py` renderiza a URL com senha em memória (F1.11) | Usar `hide_password=True` onde não for necessária no modo offline (etapa futura) | DOCUMENTADO |
| I7 | ZIP da pasta contorna o `.gitignore` | `git archive` ou lista de exclusão ao compartilhar | DOCUMENTADO |
| I8 | Cache `tempfile/nexo_arquivos` com cópias em claro, se usado manualmente com dados reais | Cache removido do `storage.py`; `resolver()` recusa `db:` | IMPLEMENTADO |
| I9 | `tests/test_configuration_deploy.py` cria `instance/` na raiz e usa `postgresql://localhost/nexo_test`, que aponta para a porta padrão do serviço local preexistente (hoje nenhuma conexão ocorre) | Isolar `instance_path` e usar host inalcançável (etapa futura; teste fora do escopo) | DOCUMENTADO; correção pendente (teste fora do escopo) |
| I10 | Executável plantado e injeção via `.bat` no verificador de backup; entrada do `PATH` sem letra de unidade | Corrigido (F2) | IMPLEMENTADO; TESTADO LOCALMENTE |

---

## J. Pendências

**Dependem de acesso autorizado ao Supabase:** revisão aplicada (`alembic_version`);
versão do PostgreSQL, plano, PITR, extensões, buckets do Storage; RLS, grants e Data API;
nomes reais das constraints; inventário de dados (planos, empresas por plano e status,
legados, órfãos, volume de BYTEA, conta de demonstração); primeiro backup real e
restauração isolada com tempos medidos (RTO) e RPO aprovado.

**Dependem de acesso ao Render:** estado real do Auto-Deploy e de Pull Request Previews,
commit implantado, comandos efetivos, variáveis não declaradas no YAML, mecanismo de
suspensão de tráfego para a janela de migração, disponibilidade de shell no plano.

**Decisões do responsável:** RPO/RTO, frequência e retenção de backups, prazo da lixeira,
registro de exclusões, destino criptografado e cofre de chaves, responsáveis e substitutos.

**Problemas de código fora do escopo desta etapa (registrados com evidência):**
notificações de ADMIN no hard delete e mensagem de sucesso (`blueprints/admin.py:523-612`);
remoção de legados antes do commit e sem checagem de compartilhamento
(`blueprints/admin.py:567, 994`; `storage.py:188-213`); DELETE duplo no hard delete;
escritas e e-mails para empresa na lixeira; `migrar_arquivos_para_db.py` com chave por
nome-base e sobrescrita silenciosa; cache de `storage.py` sem conferência de conteúdo e sem
normalização da chave em `remover()`; `content_type` dependente do host para `.xlsx` e
`.webp`; docstrings de `models.py` e `admin.py`; índices em FKs; normalização dos booleanos
em texto em SQLite legado (exige revisão nova); `migrations/env.py` (`get_engine`
depreciado, `hide_password`, timeouts); `init_db.py` com `create_all`.

**Cobertura de CI (fora do escopo):** as proteções do verificador específicas do Windows (só
`.exe`, recusa de `.bat/.cmd/.com`, `PATH` sem letra de unidade) só são testadas no Windows;
o CI usa `ubuntu-24.04`, então uma regressão exclusiva do Windows passaria nele. Avaliar um
job Windows opcional.

**Pendências documentais fora do escopo:** `README.md` (13 modelos/entidades, links `docs/`
em minúsculas, deploy automático "desabilitado", opção A com `init_db.py`, opção B sem
`FLASK_ENV=production`, exemplo de ETL no Windows); `Docs/RELATORIO_TECNICO.md` (13
entidades, "6 migrações" — hoje são 8 —, migrações "reversíveis", lixeira que
"desativa/reativa"); `Docs/AUDITORIA_RENDER.md`
(registro histórico de 7 revisões); linha "YAML versionado" de
`Docs/PLANO_PUBLICACAO_SEGURA.md` (build anterior à SEC-006B, registro de 22/10);
`Docs/PENDENCIAS_SEGURANCA_SEC004_SEC005.md` (SEC-005 como branch exclusiva); referências
de linha de `tests/test_backup_restore.py` citadas na Etapa 0.1, deslocadas pelas mudanças
desta etapa.

**Ambiente local:** remover manualmente `%TEMP%\nexo-backup-drill-0ezil1f5` (resíduo do
ensaio de 20/10, credenciais fictícias); alinhar os pacotes locais a `requirements*.txt`
(decisão do proprietário) para reproduzir o CI. As execuções desta etapa criaram, e ao final
foram removidos depois de conferidos sem arquivos: `instance/` (vazio) e `.audit_tmp/` (só
com a subpasta vazia `config-uploads/`, criada por `tests/test_configuration_deploy.py:25`
via `app.py:107`) na raiz do projeto (criados pelas suítes de teste; ambos ignorados pelo
`.gitignore`) e
`%TEMP%\nexo-etl-job-x36s81t0` (criado durante a linha de base, provavelmente pelo erro do
teste de cancelamento do ETL). No Git Bash, o `PATH` inclui o `pdftotext` do Git
(`ucrt64/bin`), que falha ao ler da entrada padrão: rodar a suíte pelo PowerShell, como em
E15, ou retirar essa pasta do `PATH`; na linha de base (E5) o `pdftotext` também não estava
no `PATH`.

**Próxima atividade:** validação operacional **autorizada** do backup real (G) e da revisão
do banco de produção (C.4). A Etapa 0.3 (ambiente de homologação) só deve começar depois da
avaliação dessas pendências.

---

## K. Critérios de conclusão antes de alterar o banco de produção

Nenhuma alteração estrutural em produção deve ser autorizada sem que **todos** os itens
abaixo estejam cumpridos e registrados:

1. Autorização explícita, responsável e janela definidos.
2. Backup lógico real gerado conforme G.6, com SHA-256 em manifesto protegido, TOC
   conferido e cópia criptografada fora do GitHub (G.7 e G.8).
3. Restauração isolada aprovada: tabelas, contagens, constraints, sequências, revisão
   Alembic e todas as consultas de G.13 nos critérios, com tempos medidos.
4. Revisão de produção conhecida (`flask db current`) e comparada com `flask db heads`;
   divergência explicada antes de prosseguir.
5. RLS, grants e exposição pela Data API inventariados; conta de demonstração verificada.
6. Auto-Deploy controlado e mecanismo de suspensão de tráfego/escritores definido e testado
   no Render.
7. Revisão nova (se houver) aprovada com os testes de H.3, inclusive no PostgreSQL privado e
   com dados preexistentes, e com plano de recuperação de H.4.
8. Suíte de testes executada em ambiente equivalente ao CI (Linux, pacotes fixados),
   sem falhas não explicadas; ensaio de backup com o ETL executado (Linux).
9. Registro de exclusões definitivas preparado para reconciliação pós-restauração.

---

## L. Situação final

**Classificação: CONCLUÍDA LOCALMENTE.**

Justificativa:

- A auditoria de modelos, migrações, backup, arquivos armazenados, vazamento e documentação
  foi concluída, com verificação adversarial independente de cada achado. Uma segunda
  revisão adversarial das alterações não encontrou achado de severidade alta; os achados
  confirmados foram corrigidos e revalidados (E7 a E15).
- Os testes locais possíveis foram executados: verificador (13 → 18 aprovados), ensaio
  PostgreSQL isolado no Windows (falhava; 8/8 após as correções, com a verificação do ETL
  registrada como não executada — exige Linux; ver E9), testes de migração e persistência,
  mutação dos defeitos corrigidos e dos ramos novos, utilitários reais, validação das
  consultas (inclusive com collation ICU) e do restore do schema `public` em PostgreSQL 18.1
  descartável. A suíte completa tem falhas apenas fora do escopo, explicadas pela plataforma
  Windows e pelos pacotes locais; depois das alterações (569 testes) não surgiu nenhuma falha
  ou erro novo em relação à linha de base (561 testes).
- Os defeitos comprovados dentro do escopo foram corrigidos com a menor mudança e testes
  correspondentes; a documentação operacional divergente foi atualizada sem reescrever
  registros históricos, exceto a anonimização do caminho local no registro de 20/10 de
  `Docs/OPERACAO_BACKUP_RESTAURACAO.md`, declarada em F7 e marcada no texto.
- O procedimento do backup real e da restauração isolada está preparado (G), e os critérios
  para alterar produção estão definidos (K).

Esta classificação **não** equivale a homologação de produção. Não houve backup real,
restauração do Supabase, verificação da revisão aplicada em produção nem acesso ao Render;
esses itens estão em J e são pré-requisito da Etapa 0.3.

---

*Documento gerado na Etapa 0.2A da reestruturação comercial do NEXO, em 2026-10-24, sobre a
pasta local (sem `.git`). Destino do commit manual: repositório oficial
`nexofaturamentointeligente-coder/Nexo-Faturamento-Inteligente`.*

---

## Nota de integração (26/10/2026)

Acrescentada ao fim do arquivo para não deslocar as referências de linha usadas em `Docs/AJUSTES_NEXO_2026/`.

- **Versões.** Este é o documento da Etapa 0.2A, produzido localmente e recebido no ZIP do proprietário. É a versão citada pelos demais documentos (`OPERACAO_BACKUP_RESTAURACAO.md`, `PREPARACAO_RENDER.md`, `Docs/AJUSTES_NEXO_2026/`), inclusive por seção (B.1, E15, G, G.10, H, K) e por linha. Em paralelo, a Etapa 0.2 foi executada no GitHub (PR #22) com outra estrutura de seções; esse registro foi preservado em [00_02_PROTECAO_BANCO_MIGRACOES_ETAPA_0_2_PR22.md](00_02_PROTECAO_BANCO_MIGRACOES_ETAPA_0_2_PR22.md).
- **Repositório e base.** As menções a "pasta sem `.git`", "commit não verificado" e ao repositório `nexofaturamentointeligente-coder/Nexo-Faturamento-Inteligente` como destino descrevem a execução local de 24/10/2026. Na integração, o proprietário indicou como repositório oficial `Helio965/Nexo---Faturamento-Inteligente---Render`; a base `ff8c2d8` foi confirmada por conteúdo, e o `faa59d9` do comentário do ZIP não existe neste repositório (deve pertencer ao outro, com o mesmo conteúdo). Qual repositório o Render publica não foi verificado ([relatório](../INTEGRACOES/RELATORIO_INTEGRACAO_GITHUB_ZIP_2026_10_26.md)).
- **Revisão de produção.** "Revisão aplicada desconhecida" (C.4, J) é anterior ao incidente de 25/10/2026: o banco foi informado em `b7c4e2a9f031`, sem a coluna exigida pela `main`, e o login responde HTTP 500. Diagnóstico e procedimento da migração `e4c004a26b01` em [RELATORIO_CORRECAO_MIGRACAO_SUPABASE_NEXO.md](../../RELATORIO_CORRECAO_MIGRACAO_SUPABASE_NEXO.md).
- **Backup real: divergências entre os três documentos.** Esta 0.2A (seção G), a Etapa 0.2 do PR #22 (F.2 e Apêndice B) e o relatório do PR #23 (seção 10, Fase C) divergem quanto (1) à versão do cliente `pg_dump` (mesma major do servidor × igual ou maior), (2) ao artefato principal (dump completo `-Fc` × `--schema=public` mais o conjunto SQL da CLI) e (3) às entradas removidas do TOC antes do restore (`SCHEMA - public` × também `COMMENT - SCHEMA public`). Para a migração pendente, o procedimento mais recente e específico é o do PR #23; as divergências devem ser decididas pelo responsável antes do primeiro backup real. Nenhuma delas foi resolvida nesta integração.
- **`.gitignore`.** Na `main`, o arquivo passou a combinar as regras WAL/journal do SQLite (F6) com as regras de dumps e chaves do PR #22; a expressão "endurecimentos opcionais não adotados" de D.4 deixou de valer para tar/diretório e chaves.
