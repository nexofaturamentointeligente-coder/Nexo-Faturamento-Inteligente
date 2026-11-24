# NEXO-SEC-003: revogação HTTP e Socket.IO

> **Atualização de 24/11/2026 (Fase 1 da auditoria de estresse):** o Socket.IO saiu do produto. O sininho passou a consultar `GET /notificacoes/novas` a cada 30 segundos, e cada consulta passa pela mesma validação de sessão das páginas (credencial, sessão registrada, `BOOT_ID`, empresa acessível). A revogação vale na consulta seguinte, sem varredura nem salas. As seções sobre sockets, salas e varredura abaixo são históricas. Banco indisponível também deixou de apagar o cookie: a requisição recebe 503 e a sessão volta a valer quando o banco volta (`falhas_banco.py`).

## Diagnóstico

Na base `255957d`, o login gravava apenas o identificador Flask-Login e BOOT_ID.
A SEC-002 invalidava links após alterar a senha, mas não os cookies já emitidos.
Socket.IO conferia BOOT_ID/conta somente no handshake e entrava na sala estável
`user_<id>`. A única emissão de dados privados estava em `notifications.py`;
um socket aberto antes da recuperação continuava nessa sala.

## Sessões HTTP

`session_security.py` calcula HMAC-SHA256 do ID e da credencial com o contexto
exclusivo `nexo-session-credential-v1`, distinto dos tokens de senha. A sessão
assinada guarda somente essa impressão, nunca a senha nem seu hash completo.

O login captura a impressão da credencial cuja senha foi verificada. Cada nova
requisição autenticada recarrega o usuário e confere BOOT_ID, atividade, ID e
impressão em tempo constante. Cookie anterior à troca ou sem impressão é limpo
e precisa de login. Erro SQL na verificação nega acesso com resposta genérica
503 e limpeza da autenticação. As proteções de primeiro acesso, CSRF, cookies,
rate limiting e reinício do servidor são mantidas.

Após primeiro acesso autenticado confirmado, somente a sessão daquele request
é renovada. A impressão vem do novo hash que esse UPDATE confirmou, sem refresh
que pudesse adotar silenciosamente uma credencial posterior. Recuperação e
ativação por link não renovam uma sessão antiga. Rollback não revoga uma
credencial que não mudou. O mecanismo e o CAS da SEC-002 são preservados.

## Socket.IO e notificações privadas

- Handshake consulta o banco atual sob FOR SHARE e valida a sessão, incluindo
  bloqueio de CLIENTE em primeiro acesso. Registro e entrada na sala acontecem
  antes de liberar o lock.
- Salas incluem ID e geração HMAC da credencial. Emissão nunca usa a sala legada.
- Notificações são enfileiradas como payloads simples e publicadas somente após
  confirmação da transação de negócio; rollback descarta a fila. A entrega usa
  sessão independente, consulta o estado atual sob FOR SHARE e mantém o lock
  até enfileirar o pacote. O UPDATE de senha é serializado com essa autorização
  no PostgreSQL READ COMMITTED.
- `after_commit` apenas marca confirmação; `after_transaction_end` externo
  despacha depois que a conexão original foi devolvida ao pool. Isso evita
  esgotamento quando a entrega abre outra sessão, inclusive com pool de uma
  conexão. SAVEPOINT não publica cedo; rollback interno descarta somente sua
  parte da fila, e rollback externo/close descarta tudo.
- Após mudança da senha, o callback desconecta a geração anterior dos SIDs
  locais. Callback atrasado não desconecta uma geração posterior legítima.
- Uma varredura por processo reconsulta o banco a cada segundo para desconectar
  conexões antigas após alterações externas, desativação ou erro SQL. Em TESTING
  a varredura é explícita, para testes determinísticos e sem threads órfãs.
- O registro local contém apenas roteamento SID/ID/impressão/BOOT_ID. Ele não é
  fonte de autorização; o banco e a geração da sala impedem novas entregas
  privadas mesmo antes da desconexão física periódica.

`guarded_socket_event` é obrigatório em novos eventos autenticados. Ele revalida
o banco sob lock até o fim do handler. Dados privados devem ser emitidos dentro
do handler enquanto o lock está mantido; retornos ACK aceitam apenas None/bool,
pois a biblioteca os enfileira depois do retorno. Eventos desconhecidos não
executam ações de negócio. O repositório não possuía eventos de negócio recebidos
do cliente. Handlers protegidos não devem antecipar commit/rollback nem alterar
a credencial enquanto mantêm esse lock de leitura.

## Garantias e limites

A revogação impede novas autorizações com a credencial anterior e novas
emissões privadas depois do commit. Bytes autorizados/enfileirados antes do
commit podem continuar em trânsito; não é possível retirá-los da rede. A
desconexão física por varredura tem intervalo de até um segundo, além do tempo
de consulta/agendamento; a proteção de dados não depende desse intervalo.

PostgreSQL fornece o lock FOR SHARE usado para serialização. SQLite ignora esse
lock: testes sequenciais SQLite não substituem a prova concorrente PostgreSQL.

O Render versionado usa um worker com threads. BOOT_ID, Socket.IO e rate limiting
continuam compatíveis com essa arquitetura. Escalar para vários workers ou
instâncias exige desenho compartilhado de BOOT_ID, broker/roteamento Socket.IO,
limitação de requisições e testes próprios. A fonte de autorização no banco não
transforma o registro local de salas em um broker distribuído.

Não há nova tabela, coluna, migração, dependência de produção ou segredo.
Na implantação futura, cookies sem impressão precisam de novo login; drenar
workers antigos evita que políticas divergentes continuem aceitando sessões.
Não voltar à versão sem revogação por compatibilidade. Nenhum deploy foi feito.

## Reproduzir

```bash
.venv/bin/python -B -m unittest tests.test_session_revocation_security -v
NEXO_TEST_POSTGRES_BIN=/caminho/local/bin \
  .venv/bin/python -B -m unittest tests.postgresql_session_revocation -v
```

Somente usuários sintéticos, SQLite temporário e cluster PostgreSQL privado.
Nenhuma URL ou credencial de produção é aceita pelos helpers PostgreSQL.

## Evidências da implementação

| Execução | Total/aprovados | Skips/falhas | Tempo |
| --- | ---: | ---: | ---: |
| SEC-003 SQLite + cinco handshakes existentes | 41/41 | 0 | 5,236 s |
| SEC-003 PostgreSQL 17.11 privado | 41/41 | 0 | 8,376 s |
| Regressão completa, navegador e backup habilitados | 315/315 | 0 | 64,130 s |
| Regressão dos tokens no PostgreSQL | 37/37 | 0 | 6,451 s |

Há 36 cenários novos comuns e cinco handshakes existentes preservados. A suíte
PostgreSQL reutiliza os 36 e acrescenta quatro corridas e pool de uma conexão.
As corridas cobrem evento/UPDATE, notificação/UPDATE, login concorrente e
primeiro acesso concorrente. As duas primeiras observam `pg_stat_activity` e
`pg_blocking_pids` para provar o UPDATE realmente esperando FOR SHARE; conferem
a senha persistida e ausência de dados privados após revogação. Erro de lock,
timeout ou falha de conexão não satisfazem os testes.

Também foram exercitados cookies antigos/legados/malformados, duas sessões,
nova senha, BOOT_ID, desativação/exclusão, falha de banco, isolamento de usuário
e empresa, conexão já aberta, reconexão, rollback, pool sem overflow e três
casos SAVEPOINT. Os 12 skips da linha de base ficaram habilitados na regressão
completa. SEC-001, SEC-002, e-mails e funcionalidades existentes passaram.
