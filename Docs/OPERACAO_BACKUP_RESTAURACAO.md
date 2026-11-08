# NEXO — operação de backup, restauração e recuperação (segurança 01/25)

Escopo: preparar a recuperação do sistema existente. Este documento não autoriza acesso, backup, restauração, migração ou publicação em produção. A validação operacional do Supabase real continua pendente de autorização. Nunca usar `init_db.py`, `db.drop_all()`, `flask db downgrade` indiscriminado ou restauração sobre produção como teste.

## 1. Ambiente e diagnóstico do código

Análise em 20/10/2026, a partir de `origin/main` no commit `9973ff2b708a689b390c8dff3d604f525842d117`, repositório oficial `Helio965/Nexo---Faturamento-Inteligente---Render`. O código usa Python 3.12, Flask, Flask-SQLAlchemy e psycopg2; o schema é versionado por Flask-Migrate/Alembic.

| Componente | Conteúdo e recuperação |
|---|---|
| GitHub | Código, templates, recursos estáticos versionados, `requirements.txt`, `render.yaml` e `migrations/`. Recuperar pelo commit revisado, preservando também uma cópia independente do repositório. Git não contém os dados de operação. |
| Render | Executa a aplicação. `render.yaml` aponta para `main`, Python 3.12.10, build sem migrações e `autoDeployTrigger: 'off'`. Configurações efetivas, domínio, acessos e segredos do painel exigem inventário privado separado; o YAML não comprova o estado atual do serviço. |
| Supabase/PostgreSQL | Dados e bytes de documentos. `DATABASE_URL` atende o runtime; `DIRECT_URL` é selecionada para migrações com `NEXO_DB_DIRECT=1`. O código usa conexão SQL, sem cliente Supabase Auth/Storage nem buckets identificados nos fontes. |
| Serviços externos | Brevo/SMTP, Anthropic e Hugging Face, quando configurados, exigem recuperação das contas, configurações e segredos. Não são restaurados por `pg_dump`. Não disparar e-mails ou chamadas de IA no ensaio. |

`create_app()` inicializa as extensões e exige PostgreSQL e chave válida em produção; não cria o schema automaticamente. As sete revisões existentes formam uma cadeia cujo head analisado é `b7c4e2a9f031`. O banco guarda `alembic_version`; os arquivos de migração ficam no Git. Um precisa corresponder ao outro. O arquivo SQL histórico e o cabeçalho de `models.py` não são inventários atuais completos.

**Atualização em 24/10/2026 (Etapa 0.2A):** a cadeia local tem agora **oito revisões lineares, com head única `e4c004a26b01`** (SEC-004: `usuario.geracao_autorizacao` e `UPDATE` irreversível de clientes de empresas na lixeira). A revisão aplicada no Supabase de produção **não foi verificada**. Inventário, riscos e roteiro de verificação: [00_02 — Proteção do banco e preparação das migrações](REESTRUTURACAO_COMERCIAL/00_02_PROTECAO_BANCO_MIGRACOES.md). **Fato posterior (25/10/2026):** o banco de produção foi informado em `b7c4e2a9f031`, sem a coluna exigida pela `main`, o que derruba o login com HTTP 500; diagnóstico e procedimento específico da migração em [RELATORIO_CORRECAO_MIGRACAO_SUPABASE_NEXO.md](../RELATORIO_CORRECAO_MIGRACAO_SUPABASE_NEXO.md).

Já existiam armazenamento transacional, hashes SHA-256 dos uploads, migrações e testes isolados SQLite de schema/arquivos. `migrar_arquivos_para_db.py` é idempotente, simula por padrão e preserva as fontes; cancela a transação em arquivos ausentes ou hashes divergentes. Limites comprovados na Etapa 0.2A, em banco descartável: a chave gravada é o nome-base do arquivo, então dois legados com o mesmo nome sobrescrevem um ao outro sem aviso; com `hash_arquivo` nulo a conferência é pulada; e o fallback por nome-base pode ler o arquivo de outro upload. Antes de qualquer `--apply`, inventariar nomes repetidos e hashes ausentes (documento 00_02, seção G). Mesmo em `--dry-run`, esse script consulta o banco configurado: não executá-lo em produção nesta etapa. `init_db.py` bloqueia PostgreSQL e recria SQLite local; não é ferramenta de recuperação.

A auditoria anterior registra cópia privada dos arquivos do projeto. Ela não comprova backup PostgreSQL. Não foi encontrado um procedimento completo de dump/restauração, um ensaio PostgreSQL de recuperação, nem política aprovada de frequência/retenção. Não foram consultados dados ou configurações reais dos clientes/provedores para este diagnóstico.

## 2. O que precisa ser protegido

Um dump lógico completo e bem-sucedido deve preservar todas as tabelas reais do banco, schema, dados, constraints, índices e estado das sequências. As 31 tabelas ORM atuais são (14 anteriores, 5 da gestão contratual, revisão `81287c74387a`, `sessao_autenticada`, 4 das Configurações da conta, revisão `c7d41e9a2b60`, `validacao_pdv`, revisão `e2f7a94c1d35`, 4 dos planos por nível, revisão `b3e9d6c1f7a2`, e 2 da Avaliação de Cenário, revisão `f4c2a8e1d9b6`):

| Tabelas | Dados e vínculos que devem sobreviver |
|---|---|
| `plano`, `segmento`, `empresa` | Planos, segmentos, cadastro/contatos, status e lixeira (`deletado_em`); empresa referencia plano e segmento. |
| `usuario` | Cadastro, hash de senha, role, ativo, primeiro acesso e geração de autorização (`geracao_autorizacao`, SEC-004, base da revogação de sessões e tokens); cliente pertence a uma empresa, administrador não. Hashes de senha também são dados sensíveis. |
| `analise` | Empresa, plano de referência, administrador responsável, período e status. |
| `upload_relatorio` | Vendas/compras, análise, administrador opcional, nome original, referência ao arquivo, extensão, tamanho, SHA-256, datas e processamento. |
| `indicador_analise`, `relatorio_analise`, `produto_curva_abc` | KPIs/períodos, devolutiva/publicação/indicador de IA e produtos ABC; relações com a análise, unicidade e posições. |
| `chamado_suporte`, `mensagem_suporte`, `notificacao` | Empresa, usuário do chamado, remetentes, histórico, destinatários e leitura. |
| `guia_topico` | Conteúdo e referência de mídia da base de conhecimento. |
| `arquivo_armazenado` | Chave, **bytes em `conteudo` (BYTEA)**, MIME, tamanho e data de criação. |
| `regime_contratual` | Empresas com as regras contratuais ativas (chave natural `id_empresa`, sem sequência), data e administrador da ativação. |
| `contrato`, `cobertura_paga` | Contratos com valor contratado (snapshot), vigência, situação e prazos da suspensão em `timestamptz`; períodos efetivamente pagos com referência da confirmação e revogação. FKs compostas garantem cobertura e contrato da mesma empresa; índice único parcial impede dois contratos vigentes. |
| `evento_contratual` | Trilha de auditoria (somente inclusão) de contratação, renovação, troca de plano, suspensão, prorrogação, reativação, cancelamento, coberturas e regras. |
| `historico_preco_plano` | Cada alteração de `plano.valor_mensal` feita pela atualização auditada do catálogo. |
| `sessao_autenticada` | Sessões revogáveis: hash do identificador, dono, validade e revogação. |
| `preferencia_usuario`, `documento_legal`, `aceite_documento`, `configuracao_sistema` | Avisos escolhidos, versões dos Termos e da Política, prova de aceite (com o IP de quem aceitou, dado pessoal, desde a revisão `a8d3f6b91c24`) e caixa da cópia de chamados. Têm RLS ligado: um papel de backup que não seja dono das tabelas precisa de `BYPASSRLS`, ou o `pg_dump` falha. |
| `validacao_pdv` | Trava do total do PDV: valor informado, faturamento bruto calculado, diferença e status da conferência por análise (chave natural `id_analise`, sem sequência). Revisão `e2f7a94c1d35`. Tem RLS ligado, com o mesmo requisito de `BYPASSRLS` das tabelas das Configurações. |
| `plano_nivel`, `funcionalidade`, `plano_funcionalidade` | Catálogo dos planos por nível: hierarquia, limite de histórico, funcionalidades e a matriz que diz o que cada nível libera. Nascem com os dados de referência gravados pela revisão `b3e9d6c1f7a2`; só `funcionalidade` tem sequência. |
| `diagnostico_entrada` | Último Raio-X do PDV de cada empresa (chave natural `id_empresa`, sem sequência): agregados, listas resumidas em JSON e o plano sugerido. Nenhum arquivo bruto. Sai com a empresa por `ON DELETE CASCADE`. |
| `lead_avaliacao`, `brainstorming_dados_pdv` | Avaliação de Cenário: contato de quem ainda não é cliente (nome, e-mail, WhatsApp e loja, **dados pessoais**), resumo do resultado e as linhas de PDV do lead por produto e ano (saem com o lead por `ON DELETE CASCADE`). Chaves UUID, sem sequência; as colunas `vazamento_total_r$` e `vazamento_estimado_r$` têm cifrão no nome. As linhas de PDV de leads não convertidos saem em 30 dias pela função `expurgar_brainstorming_pdv`, que também entra no dump. Ver `Docs/BAIA_DE_SEGURANCA_E_LEADS.md`. |

As quatro tabelas da revisão `b3e9d6c1f7a2` e as duas da `f4c2a8e1d9b6` têm RLS ligado e, no Supabase, as políticas `deny_api_access` (restritiva, para `anon` e `authenticated`) e `acesso_servidor` (para `postgres` e `service_role`). Restaurar o dump num PostgreSQL sem esses papéis falha nessas políticas e nos privilégios da função `expurgar_brainstorming_pdv`: use `--no-policies` e `--no-acl` (`pg_restore` 18) ou comente as entradas `POLICY` e `ACL` na lista do TOC, e registre a diferença, como já descrito na nota da Etapa 0.2A. Sem os `REVOKE` da função, confira depois que `PUBLIC`, `anon` e `authenticated` não a executam.

Incluir também `alembic_version` e quaisquer objetos adicionais encontrados no inventário autorizado do banco. Não limitar o backup a essa lista de nomes nem usar somente `--data-only`/`--schema-only`.

`storage.salvar()` guarda bytes e metadados na mesma transação do chamador. `LargeBinary`/BYTEA é dado de tabela: os arquivos entram no dump completo, inclusive com `deferred=True` no ORM. Não são objetos de um bucket. Uma exportação CSV dos relatórios ou só dos metadados não os preserva.

As referências `db:<chave>` de `upload_relatorio.caminho_arquivo` e `guia_topico.imagem_url` **não têm FK para `arquivo_armazenado`**. Além de restaurar constraints, verificar existência de cada chave, tamanho dos bytes, `tamanho_arquivo` e SHA-256 de uploads que tenham hash. Um banco pode aceitar uma referência quebrada sem violação SQL. Conferir também o inverso (blobs sem referência) e os vínculos que o banco não garante, como responsável de análise que não é ADMIN ou chamado com cliente de outra empresa; as consultas somente leitura, validadas em PostgreSQL 18 descartável, e o critério de aprovação estão no documento 00_02, seção G.

### Dependências fora do dump

- Uploads antigos sem prefixo `db:`: preservar o arquivo físico original, inclusive caminhos externos à raiz e os encontrados pelo fallback de basename em `UPLOAD_FOLDER`. O fallback pode apontar para o arquivo de outro upload com o mesmo nome: conferir o SHA-256 com `hash_arquivo` antes de tratá-lo como a fonte correta.
- Imagens antigas do guia sem prefixo `db:`: preservar o caminho relativo em `static/`, normalmente `static/uploads/guia/`. Arquivos ignorados pelo Git não serão recuperados por checkout.
- Código/recursos e versões das dependências, arquivos de migração e configurações de implantação.
- Segredos, papéis globais, senhas de roles, certificados, parâmetros de infraestrutura e configurações do projeto/integrações. `pg_dump` cobre um banco, não todos os objetos globais do cluster nem uma conta Supabase.
- Se um inventário autorizado revelar Supabase Storage fora deste código, copiar os objetos separadamente: backup do banco recupera metadados do Storage, não os bytes dos buckets.
- O job `nexo_expurgo_leads_pdv` do pg_cron (revisão `6a8f2d4c0e13`, expurgo diário dos dados de PDV dos leads) mora no esquema `cron` da extensão, fora das tabelas da aplicação. Depois de restaurar num banco com pg_cron, conferir `SELECT jobname, schedule FROM cron.job` e, se faltar, reagendar como a revisão faz; sem pg_cron, o expurgo segue pelo comando `flask leads expurgar-pdv --aplicar`.

O cache de `storage.py` em `tempfile.gettempdir()/nexo_arquivos` é regenerável e não é fonte de recuperação. Use um cache vazio na homologação para evitar mascarar arquivos ausentes. O cache só é preenchido quando `storage.resolver()` recebe uma referência `db:` (testes e verificações manuais; a aplicação lê os bytes direto do banco), é reutilizado apenas por existir, sem conferir conteúdo, e guarda cópias em claro. PDFs são gerados em memória a partir dos relatórios/KPIs/ABC e do código; cópias exportadas fora do sistema precisam de guarda própria se houver necessidade de preservá-las.

O Render usa filesystem efêmero por padrão; não há disco persistente declarado no Blueprint deste projeto. Não contar com `uploads/`, `static/uploads/` ou `/tmp` do serviço como cópia durável. Para legados, inventariar caminhos, bytes, tamanhos e hashes em registro privado e coordenar o ponto de captura com o dump. O snapshot consistente do PostgreSQL não sincroniza sozinho arquivos externos.

## 3. Preparação de um backup autorizado

Esta seção é um roteiro manual futuro, **não executado contra o Supabase nesta entrega**. O operador deve registrar autorização, responsável, ambiente, janela, escopo, commit e revisão Alembic, sem credenciais ou dados comerciais no Git/PR.

1. Em terminal administrativo privado, conferir o projeto e banco de origem no painel. Confirmar a versão do servidor, esquema real, extensões, permissões de leitura, espaço livre e destino externo ao checkout. Não imprimir URLs com senhas.
2. Usar `pg_dump`, `pg_restore` e `psql` da mesma versão principal do servidor quando possível. `pg_dump` não pode ser mais antigo que o servidor; testar compatibilidade do restore no destino, preferindo mesma versão principal. O ensaio local PostgreSQL 18 não confirma a versão do Supabase.
3. Para dump/restore, usar conexão direta ou pooler em **modo de sessão**, conforme conectividade e orientação atual do Supabase. Não reutilizar automaticamente a conexão transacional da aplicação (normalmente 6543). Não passar URLs SQLAlchemy com `+psycopg2` ou parâmetros específicos do runtime aos utilitários libpq.
4. Provisionar credenciais com privilégios suficientes para todo o escopo, sob acesso restrito. Não colocar senha em argumento, histórico, código ou variável exibida. Preferir serviço libpq em `pg_service.conf` sem senha e passfile privado, ou gerenciador de segredos. Não ativar rastreamento de shell (`set -x`).
5. Não executar DDL simultâneo ao backup. `pg_dump` fornece snapshot consistente para dados transacionais mesmo com escritas concorrentes; isso não é recuperação contínua/PITR nem cópia dos arquivos externos.

Exemplo de configuração **fictícia**, a adaptar somente em ambiente autorizado. Os arquivos de serviço e senha ficam fora do repositório:

```ini
# pg_service.conf privado, sem senha
[nexo_backup_autorizado]
host=HOST_CONFIRMADO_NO_PAINEL
port=5432
dbname=BANCO_CONFIRMADO
user=USUARIO_AUTORIZADO
sslmode=verify-full
sslrootcert=/volume-privado/certificados/ca-postgresql.crt
connect_timeout=15
```

Configurar CA/hostname válidos segundo o provedor; não desativar validação TLS para contornar falhas. O passfile usa `host:porta:banco:usuario:senha`, com escape libpq para `:` e `\`. Em Unix, aplicar `chmod 600` ao passfile e serviço; no Windows usar ACL NTFS limitada ao operador/conta de serviço e administradores autorizados. Configurar `PGSERVICEFILE` e `PGPASSFILE` com caminhos privados; não copiar exemplos para `.env` nem versionar os arquivos.

## 4. Gerar e conferir o dump

Exemplo Bash para uma estação/VM autorizada com volume privado criptografado. Os caminhos são ilustrativos. Cada execução deve usar um diretório novo, sem sobrescrever backups anteriores. Não executar no disco temporário do Render.

```bash
umask 077
export PGSERVICEFILE=/volume-privado/credenciais/pg_service.conf
export PGPASSFILE=/volume-privado/credenciais/pgpass.conf
BACKUP_DIR=/volume-privado/backups/nexo/EXECUCAO_UNICA_UTC
mkdir "$BACKUP_DIR" || exit 1
pg_dump --no-password --dbname='service=nexo_backup_autorizado' \
  --format=custom --file="$BACKUP_DIR/nexo.dump.partial" \
  2>"$BACKUP_DIR/pg_dump.log" || exit 1
```

Revisar avisos do log em local privado antes de aceitar o arquivo. Não enviar logs brutos ao GitHub. O formato customizado comprime o conteúdo; **compressão não é criptografia**. Não usar exclusões de tabelas/dados nem `--enable-row-security` para aceitar silenciosamente uma cópia parcial.

Um dump completo do banco Supabase pode conter schemas/objetos gerenciados. Se houver falta de permissão ou objetos incompatíveis, o backup não está aprovado: registrar o erro e ajustar o procedimento com o administrador/documentação do provedor. Não ignorar erros nem excluir `auth`, `storage` ou `realtime` e chamar o resultado de backup completo. Uma exportação explicitamente limitada ao schema da aplicação pode ser complementar, com escopo e dependências documentados, mas não substitui a recuperação do projeto. A restauração de schemas gerenciados exige procedimento específico do Supabase; não aplicar dump indiscriminadamente em projeto já provisionado.

Após saída zero e revisão de avisos:

```bash
mv -n "$BACKUP_DIR/nexo.dump.partial" "$BACKUP_DIR/nexo.dump"
cd "$BACKUP_DIR" || exit 1
sha256sum nexo.dump > nexo.dump.sha256 || exit 1
sha256sum --check nexo.dump.sha256 || exit 1
pg_restore --list nexo.dump > nexo.dump.toc || exit 1
```

Conferir no TOC as definições, `TABLE DATA` de todas as tabelas esperadas, `arquivo_armazenado`, `alembic_version`, sequências e constraints. Manter TOC e logs privados: podem revelar nomes/estrutura. `pg_restore --list` não conecta ao banco, mas lê apenas o catálogo do arquivo; não garante que todos os blocos de dados estejam íntegros. Cabeçalho `PGDMP`, tamanho não nulo e checksum calculado agora também não provam completude.

O SHA-256 só detecta alteração quando comparado a um valor anterior confiável. Guardar o manifesto com proteção contra alteração, assinatura ou controle de acesso independente; um atacante que substitui dump e hash pode forjar essa verificação. Conferir novamente após cópia/download. **Checksum e listagem não substituem restauração e validação funcional.**

No PowerShell, calcular SHA-256 com `Get-FileHash -Algorithm SHA256 -LiteralPath 'CAMINHO_PRIVADO'` e comparar ao manifesto privado. Não usar redirecionamento textual do PowerShell para gravar o dump binário; usar sempre `pg_dump --file=...`.

No Windows, `umask` e `chmod` (inclusive no Git Bash) não alteram a ACL NTFS, e pastas criadas fora do perfil do usuário costumam herdar leitura para `Usuários` e modificação para `Usuários autenticados`. Antes do `pg_dump`, criar o diretório em volume privado e remover a herança, por exemplo `icacls 'CAMINHO_PRIVADO' /inheritance:r /grant:r "${env:USERDOMAIN}\${env:USERNAME}:(OI)(CI)F"`, conferindo com `icacls` o diretório e, depois, dump, TOC, logs e manifesto.

## 5. Proteger, reter e descartar

Armazenar backups em destino privado, externo ao GitHub e separado da infraestrutura de produção, com criptografia em trânsito e em repouso. Recomenda-se uma cópia independente adicional e, quando disponível, retenção imutável contra exclusão/ransomware. Não presumir que o plano atual do Supabase inclui backup automático ou PITR: confirmar disponibilidade, retenção e possibilidade de exportação no painel autorizado.

Exemplo de criptografia de arquivo com GnuPG instalado e chave pública do destinatário previamente verificada por fingerprint independente:

```bash
# BACKUP_RECIPIENT_FINGERPRINT contém somente a identificação pública verificada.
gpg --output nexo.dump.gpg --encrypt \
  --recipient "$BACKUP_RECIPIENT_FINGERPRINT" nexo.dump || exit 1
sha256sum nexo.dump.gpg > nexo.dump.gpg.sha256 || exit 1
sha256sum --check nexo.dump.gpg.sha256 || exit 1
# Ensaio de descriptografia em diretório privado novo, nunca em stdout:
gpg --output /volume-privado/ensaio/nexo.dump --decrypt nexo.dump.gpg || exit 1
```

Guardar a chave privada/de recuperação em cofre separado, com acesso mínimo e cópia de contingência; não armazená-la junto do backup nem passar passphrase em argumento. Testar desbloqueio e descriptografia em volume privado com espaço suficiente, conferir o hash original e ensaiar a restauração. Só então eliminar cópias em claro conforme política aprovada. O formato customizado é binário sensível e permanece acessível a quem obtiver o arquivo sem criptografia.

Restringir leitura, escrita e exclusão por função; registrar acessos e falhas, revisar permissões e testar a recuperação das próprias chaves. Evitar estações compartilhadas, pastas sincronizadas sem proteção, anexos de e-mail, logs de CI e artefatos públicos. O `.gitignore` é uma barreira adicional, não um mecanismo de sigilo nem substituto da revisão dos arquivos staged.

Recomendação técnica inicial para decisão do responsável: backup lógico diário, cópia antes de alteração estrutural e ensaio de restauração mensal e após mudança relevante do schema/storage. Se perder um dia de trabalho for inaceitável, reduzir intervalo e avaliar PITR/WAL gerenciado. Isso é recomendação, não requisito contratual nem garantia de RPO/RTO. Alertar em falha ou ausência de backup e manter um responsável/substituto designados.

| Decisão pendente de aprovação | Como decidir |
|---|---|
| RPO (perda de dados tolerada) | Definir o máximo aceitável entre último ponto recuperável e falha. O dump representa seu snapshot, não o fim do comando. |
| RTO (tempo para recuperar) | Medir download, descriptografia, restore, validação e liberação; definir capacidade e equipe disponíveis. |
| Frequência e retenção | Confirmar volume, alterações, custo e obrigações aplicáveis; proposta a avaliar: 7 diárias, 4 semanais e 3 mensais, com revisão de minimização de dados. Não está implantada. |
| Guarda e descarte | Definir local, responsáveis, acesso às chaves, imutabilidade, expiração e exceções autorizadas. |

Expirar todas as réplicas e versões de acordo com a política e registros de descarte, considerando obrigações de preservação aplicáveis. Em SSD/nuvem, exclusão simples ou sobrescrita não garante eliminação física: usar mecanismos de expiração/eliminação criptográfica do provedor e gestão das chaves. Manter cópia válida substituta antes de descartar a última recuperação disponível. Uma restauração pode reintroduzir registros excluídos após o backup; reconciliar exclusões e controles de acesso antes de reabrir o sistema.

## 6. Ferramenta de prontidão (sem conexão com banco)

`scripts/verificar_prontidao_backup.py` usa a biblioteca padrão. Não importa a aplicação, não lê `.env`, não aceita URL/banco de destino, não executa dump/restore de dados e não cria/apaga bancos. Localiza utilitários no `PATH`, consulta somente versões e, opcionalmente, lê um arquivo local.

```powershell
python -B scripts/verificar_prontidao_backup.py --dry-run
python -B scripts/verificar_prontidao_backup.py
python -B scripts/verificar_prontidao_backup.py --backup 'CAMINHO_PRIVADO/nexo.dump' --sha256 HASH_SHA256_CONFIAVEL --list
```

O último comando é um modelo: substituir o hash por 64 caracteres hexadecimais do manifesto confiável. `--backup` calcula o checksum e verifica a assinatura `PGDMP` do cabeçalho; `--sha256` compara o valor esperado. `--list` solicita somente `pg_restore --list`, com saída bruta suprimida. Nenhuma dessas verificações declara o backup restaurável. `--dry-run` não inicia subprocessos; pode ler o arquivo explicitamente informado para conferir hash/cabeçalho. Um teste de checksum sem utilitários disponíveis não significa prontidão operacional.

Códigos de saída: `0` para as verificações solicitadas concluídas; `1` para arquivo, integridade ou catálogo inválido; `2` para argumentos inválidos; `3` para utilitários indisponíveis. Código zero em `--dry-run` não indica que versões/catálogo foram executados. Se os utilitários estiverem instalados fora do `PATH`, acrescentar sua pasta ao `PATH` somente na sessão de operação.

Revisão da Etapa 0.2A: os utilitários são procurados apenas em diretórios absolutos do `PATH` (no Windows, com letra de unidade ou UNC), ignorando o diretório atual e entradas relativas, e no Windows só executáveis `.exe` são aceitos (antes, um `pg_restore.bat` plantado na pasta de trabalho era executado, e um `.bat`/`.cmd` repassaria o caminho ao `cmd.exe`). Sem `PATH` definido, os utilitários são tratados como indisponíveis. No `--list`, o Python reabre o caminho já resolvido logo após o hash e entrega o conteúdo ao `pg_restore` pela entrada padrão, sem passar o caminho pelo argv ANSI; caminhos longos ou com caracteres fora da página ANSI não geram mais resultado falso. Uma troca ou alteração concorrente do arquivo entre o hash e o `--list` não é detectada, como antes; falha ao reabrir o arquivo dá código 1. Utilitário que falha em `--version` passa a contar como indisponível (código 3). Sem `--list`, apenas a assinatura `PGDMP` é conferida: qualquer arquivo com esse prefixo, como o `toc.dat` de um dump em diretório, recebe código 0 como não validado; a ferramenta analisa somente o formato custom. Precedência dos códigos: 2, depois 1, depois 3, depois 0. Em `--dry-run` sem utilitários o resultado é 3 mesmo com SHA-256 coincidente, e SHA-256 divergente sempre resulta em 1.

## 7. Ensaio automatizado com PostgreSQL isolado

`tests/test_backup_restore.py` contém ensaio opt-in usando `initdb`, `pg_ctl`, `createdb`, `pg_dump` e `pg_restore`. Não aceita conexões externas nem um dump real como entrada. Sem a variável de ativação, registra teste ignorado com motivo; isso não conta como ensaio executado.

Exemplo Windows (ajustar a pasta da instalação local):

```powershell
$env:NEXO_TEST_POSTGRES_BIN = 'C:/Program Files/PostgreSQL/18/bin'
python -B -m unittest tests.test_backup_restore -v
Remove-Item Env:NEXO_TEST_POSTGRES_BIN
```

Executar com usuário comum e PostgreSQL local instalado. Em Unix, não executar `initdb` como root. O ensaio cria seu próprio cluster temporário, escuta apenas loopback, usa credenciais fictícias geradas e bancos de origem/destino novos; não usa o serviço PostgreSQL instalado ou Supabase. Não há comando para remover banco existente. Ao terminar, encerra somente o cluster criado pelo teste e remove seus arquivos temporários.

Desde a SEC-005, o ETL roda em subprocesso com limites POSIX (`resource`) e recusa o Windows (`code="platform"`). No Windows o ensaio registra `"not_executed": ["etl"]` no relatório, sem contar essa verificação como aprovada, e reprova se isso ocorrer no Linux; a conferência do ETL com dados restaurados exige Linux, como no CI. As cinco suítes `tests.postgresql_*` exigem POSIX (socket Unix sem TCP) e falham no `setUpClass` no Windows: executá-las no CI ou em Linux/WSL2. No Windows, a proteção das credenciais fictícias do ensaio depende da ACL que o `tempfile` aplica a partir do Python 3.12.4.

Fluxo verificável:

1. Isolar ambiente/configuração e bloquear leitura do `.env`; preparar banco de origem apenas no cluster temporário.
2. Aplicar as migrações versionadas nesse banco fictício, sem executar migrações reais no Supabase.
3. Inserir empresas, usuários, análises, vendas/compras e documentos de teste com referências/metadados.
4. Gerar dump customizado, verificar SHA-256 e listar conteúdo com `pg_restore --list`.
5. Restaurar em outro banco novo e vazio com parada no primeiro erro, sem `--clean`/`--create`.
6. Conferir tabelas/revisão Alembic, vínculos, constraints, sequências, bytes/hashes e leitura pela aplicação usando cache novo.
7. Executar validação funcional Flask e registrar resultado agregado, sem dados/confidenciais em saída.

O ensaio comprova o caminho local para os dados sintéticos cobertos. Não mede RTO/RPO real, volume de produção, regras RLS/roles do projeto Supabase, extensões gerenciadas ou cobertura de legados que não foram fornecidos.

## 8. Restauração manual de um backup autorizado

1. Registrar incidente/ensaio, autorização, origem do arquivo, hash esperado, commit e revisão Alembic de origem. Preservar evidências e o estado posterior à falha antes de qualquer intervenção.
2. Provisionar PostgreSQL **novo, separado da produção**, com versão/extensões compatíveis, acesso restrito e espaço adequado. Desabilitar integrações de saída, automações e acesso público. Não copiar `DATABASE_URL`/`DIRECT_URL` reais para a homologação. Dados reais restaurados exigem ambiente autorizado com proteção equivalente e descarte definido; testes automatizados usam somente fictícios.
3. Descriptografar em volume protegido, validar hash contra manifesto confiável, inspecionar TOC e origem. Dumps podem conter SQL/funções executáveis: só restaurar fontes confiáveis, no isolamento. Confirmar destino vazio e identidade do host/porta/banco por inspeção privada independente.
4. Configurar outro serviço libpq (`nexo_restore_isolado`) e passfile com credenciais **somente do destino**. O serviço de backup/origem não deve ser reutilizado.
5. Para dump compatível da aplicação em destino vazio, restaurar com:

```bash
pg_restore --no-password --dbname='service=nexo_restore_isolado' \
  --single-transaction --exit-on-error --no-owner --no-privileges \
  /volume-privado/ensaio/nexo.dump \
  2>/volume-privado/ensaio/pg_restore.log
```

Checar saída zero e revisar log privado. `--no-owner --no-privileges` facilita isolamento com papel próprio; **não homologa os acessos de produção**. Recriar/revisar ownership, grants e RLS apropriados no destino antes de disponibilizá-lo. Objetos/roles globais não contidos no dump precisam de inventário protegido; não importar papéis gerenciados do Supabase cegamente. Para backup completo com schemas/objetos gerenciados, adaptar com o administrador ao procedimento oficial do provedor e testar antes; falhas de extensão/permissão não são sucesso aceitável. Nota da Etapa 0.2A: `--no-privileges` só ignora GRANT/REVOKE. Políticas RLS presentes no dump são restauradas e, se citarem papéis ou funções do Supabase inexistentes no destino (`anon`, `authenticated`, `auth.uid()`), a restauração falha; nesse caso, usar `--no-policies` (`pg_restore` 18) ou comentar as entradas `POLICY` numa lista de TOC, e registrar a diferença (documento 00_02, seção G.10).

6. Comparar inventário de tabelas, contagens de referência por tabela, FKs, unicidade, sequências e `alembic_version`; verificar vínculos empresa/usuário/análise, análise/uploads/KPI/devolutiva/ABC e suporte/notificações. Usar registros agregados privados e amostras autorizadas, sem expor conteúdo comercial.
7. Resolver **todas** as referências `db:` e verificar bytes, tamanho e hashes disponíveis. Recuperar separadamente os legados e testar a resolução dos caminhos sem acessar origem/produção. Manter cache novo. Não inventar um arquivo ausente para satisfazer o teste.
8. Iniciar uma instância isolada do NEXO com o commit correspondente ao snapshot, schema restaurado e integrações desativadas. Não rodar seed, `create_all`, `stamp`, upgrade/downgrade automaticamente sobre a restauração. Conferir `/healthz`, `/healthz/db`, autenticação fictícia/autorizada, painel, leitura dos documentos, ETL e geração de PDF conforme escopo do ensaio. A conferência do ETL exige Linux/POSIX (SEC-005); no Windows, registrá-la como não executada.
9. Registrar duração, versões, hashes, contagens esperadas/observadas, diferenças, falhas e decisão de aprovação do ensaio em local privado. Só registros sintéticos/agregados podem ir ao repositório. Check de saúde sozinho não valida integridade nem autorização entre empresas.
10. Recuperação real e mudança de tráfego/conexão são outra operação: exigem autorização explícita, plano de reconciliação dos dados posteriores e validação de acesso antes da liberação. Este roteiro não orienta sobrescrever produção. Descartar o ambiente de ensaio conforme política aprovada após preservar evidências necessárias. No descarte, incluir `%TEMP%\nexo_arquivos`, se `storage.resolver()` tiver sido usado, e as pastas `instance/` e de uploads da instância isolada.

## 9. Estratégia A — reversão do código por PR

Identificar uma versão estável por commit/tag, PR aprovado, testes e registro do último deploy bem-sucedido no Render. Não escolher apenas pelo horário ou assumir que `main` sempre representa o código em execução. Registrar o SHA de origem e o SHA desejado, versões das dependências e schema exigido. Confirmar no painel o auto-deploy efetivo antes de qualquer publicação; este trabalho não altera o Render.

Em checkout limpo separado, após `git fetch origin`, criar branch a partir de `origin/main` e examinar o diff e commits a reverter:

```bash
git switch -c recuperacao/reverter-alteracao origin/main
git log --oneline --decorate -n 20
git show SHA_DO_COMMIT_A_REVERTER
git revert SHA_DO_COMMIT_A_REVERTER
```

Os SHAs acima são placeholders. Para múltiplos commits, revisar dependências e reverter em ordem apropriada; para merge, revisar os pais antes de escolher `git revert -m 1 SHA_DO_MERGE`. Não usar `reset --hard`, force push ou sobrescrever trabalho local. Conflitos exigem resolução e nova revisão, não substituição indiscriminada do projeto.

Executar testes do comportamento afetado e `python -B -m unittest discover -s tests -v`, ensaio PostgreSQL se pertinente, `git diff --check` e revisão de segredos/dependências. Verificar compatibilidade do código anterior com o **schema atual** em ambiente isolado antes de aprovar a reversão. Abrir PR para `main` com motivo, riscos e evidências; nova publicação no Render somente após aprovação explícita e verificações manuais. Não realizar merge/deploy automaticamente.

## 10. Estratégia B — recuperação do banco

Um revert Git muda os arquivos, não os dados nem o schema que uma migração já alterou. Alembic registra revisões executadas; retirar um arquivo de migração ou voltar código não desfaz a execução. `downgrade` pode descartar informação irrecuperável: a revisão `b7c4e2a9f031` remove `arquivo_armazenado` (todos os bytes dos documentos), a `81287c74387a` remove as tabelas da gestão contratual (o downgrade dela se recusa a rodar se houver qualquer linha contratual), a `d61b5e68082c` remove `guia_topico`, a `e4c004a26b01` remove `geracao_autorizacao`, perdendo o histórico de revogação sem reativar as contas que bloqueou, outras removem colunas e a inicial remove tabelas. No SQLite, downgrades que recriam tabelas referenciadas falham com dados e podem deixar o banco numa revisão intermediária. Não usar downgrade como mecanismo genérico de rollback.

Antes de cada mudança estrutural futura, o PR deve conter plano específico:

1. Estado inicial (commit/schema/revisão), transformação proposta, compatibilidade com código anterior e volume de dados afetados; preferir expansão compatível antes de remover colunas/tabelas.
2. Backup prévio autorizado, hash, retenção e resultado de restore isolado, incluindo documentos e legados. Se usado, registrar ponto de recuperação/PITR e limites do plano contratado.
3. Caminho escolhido em falha: corrigir para frente com migração revisada preservando dados, voltar só o código se compatível, ou restaurar em banco separado. Eventual downgrade só após revisão de seu SQL, teste com cópia autorizada e aceitação explícita das perdas.
4. Procedimento de controle de escritas durante a intervenção e captura dos registros posteriores ao snapshot. **Restaurar o backup perde as gravações posteriores se não houver reconciliação**. A estimativa de perda e a estratégia de preservar/reaplicar uploads, análises, usuários, mensagens e demais mudanças devem ser aprovadas antes da troca.
5. Validações de schema, bytes, integridade, acessos/segregação entre empresas e funcionamento; responsáveis por executar, revisar e autorizar a liberação.
6. Novo endpoint/conexão protegido, troca controlada autorizada, monitoramento e preservação do ambiente anterior até o encerramento. Não substituir tabelas reais nem aplicar migração de recuperação nesta prioridade.

Não alterar `alembic_version` manualmente nem usar `flask db stamp` para ocultar divergências. Manter pareamento entre dump, inventário dos arquivos externos, commit, dependências e revisão Alembic. Restaurar também controles/exclusões posteriores necessários, evitando reativar acesso indevido.

## 11. Evidências desta entrega e pendências

Execuções em 20/10/2026, Windows, Python 3.12.10 e utilitários/servidor PostgreSQL 18.1 locais:

| Comando/verificação | Resultado observado |
|---|---|
| `python -B -m unittest discover -s tests -v` antes das alterações | 49 testes aprovados, 38,918 s. |
| `python -B -m unittest tests.test_backup_readiness -v` | 13 testes aprovados, 0,084 s. |
| `python -B -m unittest tests.test_backup_restore -v`, com opt-in PostgreSQL | Ensaio executado e aprovado; reexecução após correção de timeout e proteção do diretório: 4 testes aprovados, 35,168 s. A versão final recebeu mais um teste de rejeição de URL, coberto na suíte completa abaixo. |
| `python -B -m unittest tests.test_backup_restore -v`, sem opt-in, versão final | 5 testes descobertos: 4 aprovados e 1 ignorado intencionalmente, 0,013 s; nenhum cluster iniciado. |
| `python -B -m unittest discover -s tests -v`, com `NEXO_TEST_POSTGRES_BIN=C:/Program Files/PostgreSQL/18/bin` | **67 testes aprovados, nenhum ignorado, 69,997 s**, incluindo dump e restore PostgreSQL efetivos, somente fictícios. |
| Verificador real, padrão e `--dry-run`, com binários no `PATH` da sessão | Saída zero; três utilitários encontrados. Sem ajuste do `PATH`, a ausência é corretamente informada com código 3. |
| Verificador real com dump sintético, SHA-256 confiável e `--list` | Saída zero no ensaio; arquivo truncado rejeitado com código 1. |
| `git diff --check`, revisão dos arquivos e regras `git check-ignore` | Sem erros de whitespace; dumps, cópias criptografadas/sidecars e passfiles/serviços protegidos pelas regras. `.env`, bancos, dumps e credenciais não incluídos na entrega. |

O relatório agregado do ensaio confirmou **15 tabelas (14 modelos + `alembic_version`), 4 uploads e 5 arquivos**. Foram verificadas todas as linhas das tabelas ORM, relações, sequências, rejeição de FK inválida, bytes/hashes/tamanhos/MIME dos uploads, mídia do guia, cache novo, ETL, saúde do banco, login com CSRF, dashboard e recusa de acesso à análise de outra empresa. A suíte existente também validou geração de PDF e fluxos da aplicação. Não houve mudança no código da aplicação, dependências ou migrações.

O primeiro ensaio encontrou timeout de captura de pipes do `pg_ctl` no Windows; a saída foi redirecionada para `DEVNULL` e o encerramento ganhou proteção no processo pai. Os ensaios corrigidos concluíram e limparam seus próprios clusters. O cluster inicial também foi encerrado, mas a revisão automática de aprovação bloqueou a exclusão da pasta temporária original com a razão `blocked by policy`. Não houve tentativa de contornar o bloqueio; restam somente artefatos fictícios fora do Git em `%TEMP%\nexo-backup-drill-0ezil1f5` (caminho anonimizado em 24/10/2026), para limpeza manual. Não há serviço desse ensaio em execução.

Avisos já presentes na suíte inicial permaneceram: depreciação interna do ReportLab e `ResourceWarning` no teste existente de assets estáticos. Não causaram falha. Os resultados locais não homologam a versão, o volume, os privilégios ou qualquer backup real do Supabase.

Pendências de operação: aprovar RPO/RTO/frequência/retenção/responsáveis; inventariar legados e configurações reais em local privado; conferir plano/backup/PITR/versão/extensões e privilégios no Supabase; provisionar destino externo criptografado/cofre de chaves; executar primeiro backup autorizado e restore isolado protegido, medir tempos e homologar permissões/integrações. Não houve merge, deploy, consulta a clientes, dump/restauração ou migração no Supabase de produção nesta entrega.

**Reexecução em 24/10/2026 (Etapa 0.2A; Windows, Python 3.12.10, PostgreSQL 18.1 local).** A tabela acima é evidência histórica de 20/10, anterior à SEC-005. Hoje `tests.test_backup_readiness` tem 18 testes e `tests.test_backup_restore` tem 8. Antes da correção desta etapa, o ensaio opt-in falhava no Windows na etapa de comparação, pela recusa de plataforma do ETL; depois dela, 8 testes aprovados, com relatório de 15 tabelas, 4 uploads, 5 arquivos, 16 verificações aprovadas e `not_executed: ["etl"]` (a verificação do ETL exige Linux; o teste pai reprova se ela aparecer entre as aprovadas). Na suíte completa do Windows há falhas e erros alheios ao backup (ETL recusado pela plataforma e pacotes locais diferentes de `requirements.txt`), registrados no documento 00_02, seção E. O diretório remanescente do ensaio de 20/10 ainda existia em 24/10, sem processo associado; a remoção manual continua pendente. Nenhum backup ou restauração do Supabase real foi executado.

## Referências oficiais consultadas em 20/10/2026

- [PostgreSQL — pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html): snapshot lógico, formatos, limites e compatibilidade.
- [PostgreSQL — pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html): catálogo, restore transacional e opções de propriedade/acesso.
- [PostgreSQL — serviço de conexão](https://www.postgresql.org/docs/current/libpq-pgservice.html) e [passfile](https://www.postgresql.org/docs/current/libpq-pgpass.html): configuração sem senha na linha de comando.
- [GnuPG — criptografia e descriptografia](https://gnupg.org/documentation/manuals/gnupg/Operational-GPG-Commands.html).
- [Supabase — backups](https://supabase.com/docs/guides/platform/backups): cobertura, limitações e objetos de Storage.
- [Supabase — migração PostgreSQL e modos de conexão](https://supabase.com/docs/guides/platform/migrating-to-supabase/postgres): conexão de sessão/direta e compatibilidade.
- [Supabase — schemas Realtime gerenciados](https://supabase.com/changelog/realtime-schema-locked-down-against-modification): restrições que impedem assumir restore irrestrito dos schemas do provedor.
- [Render — discos e filesystem efêmero](https://render.com/docs/disks).
