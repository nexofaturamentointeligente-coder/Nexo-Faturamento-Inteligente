# Integração contínua de segurança

O workflow `.github/workflows/nexo-security-ci.yml` valida alterações em ambiente
descartável. Configuração versionada e validação estática não comprovam uma
execução no GitHub: registrar o link, o SHA e o resultado real dos dois jobs
antes de declarar a integração contínua operacional.

## Eventos e verificações

O workflow executa em Pull Requests abertas, atualizadas, reabertas ou retiradas
do rascunho; em pushes para `main`; e por acionamento manual. PRs em rascunho
também executam os testes. O acionamento manual pelo GitHub exige que o workflow
esteja disponível na branch padrão; sua presença somente em uma PR não garante
que a opção manual já apareça.

Os nomes estáveis dos checks são:

| Check | Verificações |
| --- | --- |
| `security-sqlite-browser` | Suíte completa, incluindo autenticação, tokens, SEC-003, segurança PDF, configuração e regressões; 11 cenários de navegador Chromium; backup/restore PostgreSQL descartável. |
| `security-postgresql` | Tokens, consumo único, concorrência, erros transacionais, revogação de sessões/notificações, restauração segura de empresas, migração SEC-004 e limites/atomicidade/concorrência do ETL SEC-005 em PostgreSQL real. |

Ambos usam Ubuntu 24.04, Python 3.12 e as dependências declaradas em
`requirements-dev.txt`. Playwright 1.56.0 é exclusivamente de desenvolvimento.
O instalador compartilhado `tools/install_dependencies.py` prepara pip 26.2.2 a partir do wheel oficial com SHA256 fixo, confirma a versão efetiva e só então instala os requisitos. Configurações pip herdadas e índices adicionais são excluídos; proxy e TLS permanecem ativos. Chromium é instalado no
runner pelo comando oficial do Playwright. `poppler-utils` habilita a inspeção
do texto PDF; o APT fornece os binários PostgreSQL da distribuição.

## Banco e isolamento

Os testes usuais criam arquivos SQLite temporários com usuários e dados
sintéticos. A variável global de fallback é SQLite em memória, nunca uma conexão
externa. As fixtures substituem esse fallback pelos bancos isolados de cada caso.

O serviço PostgreSQL é criado pelo próprio teste usando `initdb` e `pg_ctl`:
cluster temporário exclusivo, socket Unix privado com permissões 0700, sem
interface TCP, usuário efêmero e remoção ao final. Os testes não reutilizam o
servidor PostgreSQL padrão do runner. Nenhum contêiner `services` adicional é
necessário para um servidor que já possui esse ciclo de vida isolado.

`NEXO_TEST_POSTGRES_BIN` aceita somente diretório absoluto de binários locais.
Não é uma URL nem aceita DSN. O helper não escolhe banco usando `DATABASE_URL`
ou `DIRECT_URL`; variáveis de conexão e credenciais do ambiente são excluídas dos
utilitários. Os módulos PostgreSQL são executados explicitamente e falham quando
os binários obrigatórios não estão disponíveis. O ensaio de backup possui seu
próprio cluster descartável.

A versão PostgreSQL é registrada na execução. A homologação local desta tarefa
usou 17.11; o APT do runner pode fornecer outra versão suportada, conforme sua
distribuição. Não confundir essa execução com acesso ao Supabase de produção.

## Permissões e integridade

- O token do workflow tem somente `contents: read`.
- O checkout usa `persist-credentials: false`; os testes não recebem token
  administrativo ou segredos de produção.
- Não há `pull_request_target`, acesso a segredos, comandos de merge, publicação,
  migração de produção ou alteração de configurações externas.
- Chaves e variáveis declaradas no workflow são exclusivamente sintéticas.
- As Actions oficiais estão fixadas a commits completos, conferidos no GitHub:
  checkout v7.0.1 em `3d3c42e5aac5ba805825da76410c181273ba90b1` e setup-python
  v7.0.0 em `5fda3b95a4ea91299a34e894583c3862153e4b97`, ambas com Node 24.
- Atualizar esses pins exige nova revisão das versões e da execução real.

Pull Requests externas podem exigir aprovação do proprietário para iniciar
Actions. Falta de permissão, aprovação ou disponibilidade da conta deve ser
registrada como bloqueio, sem converter uma execução ausente em aprovação.

## Reproduzir os testes

Em uma virtualenv de teste, sem credenciais de produção:

```bash
python -I tools/install_dependencies.py --requirements requirements-dev.txt
python -m pip check
python -m playwright install chromium
export NEXO_TEST_POSTGRES_BIN=/caminho/absoluto/postgresql/bin
NEXO_TEST_BROWSER=1 python -B -m unittest discover -s tests -t . -v
python -B -m unittest tests.postgresql_auth_tokens -v
python -B -m unittest tests.postgresql_configuracoes -v
python -B -m unittest tests.postgresql_session_revocation -v
python -B -m unittest tests.postgresql_company_restore_security -v
python -B -m unittest tests.postgresql_company_restore_migration -v
python -B -m unittest tests.postgresql_etl_resource_security -v
```

Instalar os binários PostgreSQL e `pdftotext` conforme o sistema local. Em Linux,
dependências de execução do Chromium podem exigir instalação adicional; em um
runner descartável, o workflow usa `python -m playwright install --with-deps
chromium`. Um Chromium de teste já instalado pode ser indicado explicitamente
por `NEXO_TEST_CHROMIUM`.

Os 12 skips conhecidos da linha de base correspondem ao ensaio PostgreSQL e aos
11 cenários opcionais de navegador. Na CI ambos são habilitados. Qualquer skip
restante deve ser identificado e registrado separadamente; não conta como teste
aprovado.

## Confirmar a primeira execução

Após abrir a PR, consultar sua aba Checks e a aba Actions do repositório. Conferir
que o SHA pertence à branch esperada, que ambos os jobs terminaram com sucesso e
que as suítes PostgreSQL e navegador foram realmente executadas. Investigar
falhas antes de repetir e registrar os links e as contagens no relatório final.

Validar os nomes de checks efetivamente registrados antes de preparar regras
obrigatórias da `main`. A existência deste workflow não configura proteção de
branch nem autoriza merge ou deploy.

Primeira execução real confirmada:
[39705086856](https://github.com/Helio965/Nexo---Faturamento-Inteligente---Render/actions/runs/39705086856),
PR #12, head `bb8578818cedf7da5bfc48a8c4469fc7143e1e8b`: ambos os checks
concluíram com sucesso. PostgreSQL levou 76 s e SQLite/browser 135 s. O relatório
de conclusão registra os detalhes. Após a pausa, os merges externos das PRs
#12/#11/#10 incorporaram o workflow à main. A execução de push
[39705823783](https://github.com/Helio965/Nexo---Faturamento-Inteligente---Render/actions/runs/39705823783)
no SHA `d6f0a9e20ccf0879919a10e0e6223c8290f5a8c2` também terminou com sucesso:
ambos os jobs e todas as etapas de teste aprovados. Isso comprova a operação
na main integrada; não configura proteção de branch nem autoriza deploy.

Nesta sessão, a API confirmou jobs/etapas aprovados, mas baixar os logs retornou
403 Forbidden. A origem desse 403 não foi comprovada por cabeçalhos; nenhum
controle foi contornado. Contagens locais e PostgreSQL 17.11 local são evidências
separadas: a contagem/versão específica do runner não foi extraída dos logs.
