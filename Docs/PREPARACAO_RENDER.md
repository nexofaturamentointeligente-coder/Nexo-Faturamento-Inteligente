# PREPARAÇÃO PARA HOSPEDAGEM NO RENDER - NEXO FATURAMENTO INTELIGENTE

Este documento detalha a configuração técnica necessária para publicar a aplicação no GitHub e hospedá-la no Render.

## 1. Requisitos Técnicos

- **Linguagem:** Python 3.12.10
- **Banco de Dados:** PostgreSQL (Recomendado: Supabase)
- **Servidor WSGI:** Gunicorn (com worker threading para suporte a WebSockets)
- **Infraestrutura:** Render Web Service (Plano Free)

## 2. Publicação no GitHub

1. Crie um repositório público ou privado no GitHub.
2. Certifique-se de que o `.gitignore` está configurado para NÃO enviar:
   - `.env`
   - `instance/`
   - `uploads/`
   - `__pycache__/`
   - `.audit_tmp/`
3. Publique em branch própria e abra uma PR para revisão. Merge e implantação exigem autorização separada.
4. **Repositório oficial e `README.md`:** o `origin` local aponta para `https://github.com/nexofaturamentointeligente-coder/Nexo-Faturamento-Inteligente.git`, e o envio é manual, feito pelo proprietário. Na publicação final, **mantenha o `README.md` original do repositório remoto**: o deste projeto não deve substituí-lo. Antes de enviar, rode `git fetch origin` e `git checkout origin/<branch-padrão> -- README.md`, e faça o commit dessa troca. Se o Git recusar o envio por históricos diferentes, não use `--force`: isso apagaria o histórico e o `README.md` do remoto.

5. **Trava do total do PDV:** aplique as migrações `c7d41e9a2b60` e `e2f7a94c1d35` antes de o código novo receber tráfego. Sem elas, nenhuma nova devolutiva é publicada. Ver `Docs/TRAVA_TOTAL_PDV.md`.
6. **Aceite dos Termos e da Política:** a migração `a8d3f6b91c24` vem depois delas (`flask db upgrade a8d3f6b91c24` aplica as três; `head` inclui também a `b3e9d6c1f7a2`, do item 7). Sem ela, nenhum aceite é exigido e os documentos ficam indisponíveis. Assim que um documento for publicado, todos os clientes veem o modal de aceite no acesso seguinte. Ver `Docs/CONFIGURACOES_DA_CONTA.md`.
7. **Planos por nível e Raio-X do PDV:** a migração `b3e9d6c1f7a2` vem depois da `a8d3f6b91c24`. Ela cria quatro tabelas, grava o catálogo de níveis e funcionalidades e, no Supabase, liga o RLS e as políticas `deny_api_access` e `acesso_servidor`. Sem ela, o portal segue igual, sem os itens novos do menu, e as rotas `/onboarding/*` respondem 503. O Raio-X roda no worker isolado do ETL, então só funciona no Linux do Render, como o processamento das análises. Ver `Docs/PLANOS_E_RAIO_X.md`.
8. **Avaliação de Cenário (leads):** as migrações `f4c2a8e1d9b6`, `5e1b9c3a7d20` e `6a8f2d4c0e13` vêm depois da `b3e9d6c1f7a2`, nessa ordem, e a última é a `head`. A primeira cria `lead_avaliacao` e `brainstorming_dados_pdv`, com RLS e as mesmas políticas, e a função `expurgar_brainstorming_pdv` (TTL de 30 dias). A segunda amplia a staging (somas por produto e ano, marcas, bloco da triagem, unicidade e registro do consultor). A terceira agenda o expurgo diário no pg_cron do próprio banco, se a extensão estiver habilitada no Supabase (Database › Extensions); sem ela, não faz nada e o comando `flask leads expurgar-pdv --aplicar` segue valendo. O código pode ir antes delas: a tela `/admin/leads` (só consultores) mostra que faltam as migrações e não lê arquivo nenhum. A avaliação roda no worker isolado do ETL, então só funciona no Linux do Render. Ver `Docs/AVALIACAO_DE_CENARIO.md`.
9. **Teto de células do ETL:** o limite de 500.000 células por aba passou a ser rígido. Antes de publicar, confira no painel do Render que a variável `ETL_MAX_CELLS_PER_SHEET`, se existir, vale no máximo 500000. Um valor acima disso (o teto antigo era 1.000.000) faz a configuração ser recusada, e o app não sobe.
10. **Baia de Segurança:** não depende de migração. Todo relatório enviado passa pela triagem antes do banco, e a validação do upload no worker isolado passou a ter o dobro do prazo do ETL (até 120 s), como o Raio-X. Arquivos com fórmula, macro, objeto incorporado ou qualquer parte além das de dados (comentários, configurações de impressão, vínculos) são gravados como cópia só com valores; os números lidos pelo ETL não mudam. Ver `Docs/BAIA_DE_SEGURANCA_E_LEADS.md`.

## 3. Configuração no Render

### A. Criação do Serviço
- **Tipo:** Web Service
- **Runtime:** Python
- **Branch:** `main`
- **Região:** Oregon (us-west-2) - *Mesma região do Supabase para menor latência.*

### B. Comandos de Build e Inicialização
- **Build Command:** `python -I tools/install_dependencies.py --requirements requirements.txt`
- **Start Command:** `gunicorn -w 1 --threads 50 --timeout 180 -b 0.0.0.0:$PORT wsgi:app`
- **Health Check Path:** `/healthz`

O build versionado prepara pip 26.2.2 verificado por SHA256 antes da aplicação e confirma a versão. O comando efetivo no painel pode divergir deste arquivo: sua adoção deve ser verificada pelo operador em etapa autorizada. A SEC-006B não acessa o painel nem corrige automaticamente builds/produção já existentes. Não são executados seed ou migrações pelo instalador.

### C. Variáveis de Ambiente (Env Vars)
| Variável | Obrigatória | Descrição |
| :--- | :---: | :--- |
| `FLASK_ENV` | Sim | Definir como `production` |
| `SECRET_KEY` | Sim | Chave aleatória segura (use `generateValue: true` no Render) |
| `DATABASE_URL` | Sim | URL de conexão do PostgreSQL (via Transaction Pooler - porta 6543) |
| `DIRECT_URL` | Sim | URL para migrações: conexão direta ou Session pooler (ambos na porta 5432), com SSL; nunca o Transaction pooler (6543). Segundo a documentação do Supabase, a conexão direta usa IPv6 (IPv4 só com add-on) e o Render só tem IPv4: a partir dele, use o Session pooler |
| `PROXY_FIX_HOPS` | Sim | Definir como `1` (Render usa 1 proxy reverso) |
| `BREVO_API_KEY` | Sim | Chave **de API** do Brevo (`xkeysib-…`, não a chave SMTP) para envio de e-mails via HTTPS (Render Free bloqueia SMTP). Sem valor no painel, o envio fica desativado |
| `MAIL_DEFAULT_SENDER` | Sim | E-mail remetente verificado no Brevo |
| `PYTHON_VERSION` | Sim | `3.12.10` |

Antes de liberar o serviço, autorize no Brevo as faixas de IP de saída do Render
(*serviço → Connect → Outbound*) e confira *Admin → Diagnóstico de e-mail* e o
envio de teste. Procedimento completo e tabela de falhas em
[EMAILS_TRANSACIONAIS.md](EMAILS_TRANSACIONAIS.md).

---

## 4. Procedimento de Primeira Instalação (Manual)

O Render executa apenas o build e o start. A preparação do banco de dados e do administrador deve ser feita manualmente na primeira vez.

### Passo 1: Migrações do Banco
Para criar as tabelas no Supabase/PostgreSQL, execute localmente (conectado ao banco de produção via `DIRECT_URL`) ou via terminal do Render (se disponível).
Em produção a aplicação exige `DATABASE_URL` mesmo no modo direto (sem ela, `create_app` falha com `RuntimeError: DATABASE_URL PostgreSQL é obrigatória em produção.`); com `NEXO_DB_DIRECT=1`, a migração usa a `DIRECT_URL`:
```bash
export FLASK_ENV=production
export NEXO_DB_DIRECT=1
export DATABASE_URL="sua_url_pooled_do_supabase_porta_6543"
export DIRECT_URL="sua_url_direta_do_supabase_porta_5432"
export SECRET_KEY="sua_chave_secreta"
flask db upgrade
```

Antes de executar, faça a verificação da seção 5 (revisão atual × head, backup verificado). Não digite segredos reais em comandos que fiquem no histórico do shell; carregue-os de arquivo protegido fora do repositório ou de um gerenciador de segredos e limpe as variáveis ao final.

### Passo 2: Criação do Administrador Inicial
Após as migrações, no mesmo terminal (o `seed.py` precisa das mesmas variáveis do Passo 1), execute o script de seed para criar a conta master:
```bash
export ADMIN_EMAIL="seu-email@admin.com"
export ADMIN_NOME="Nome do Administrador"
export ADMIN_SENHA="senha_forte_escolhida"
python seed.py
```

---

## 5. Atualizações e Deploys Posteriores

O YAML versionado declara `autoDeployTrigger: 'off'`, mas isso não comprova a configuração efetiva do painel. O proprietário informou Auto-Deploy **ativado** durante a SEC-006. Merge/push na `main` pode disparar implantação: verificar o painel e obter autorização antes de qualquer integração. Esta documentação não modifica a configuração real nem autoriza deploy.

Antes de alterar o schema, preparar backup autorizado, validar a restauração isolada e revisar o plano específico de recuperação conforme [Operação de backup, restauração e recuperação](OPERACAO_BACKUP_RESTAURACAO.md). Reverter o código não reverte o banco. Nenhum comando de migração ou publicação deste documento deve ser executado como teste de backup.

Para atualizar a aplicação (roteiro revisado na Etapa 0.2A; cada passo depende de autorização do responsável):
1. Trabalhe em branch própria e PR. Antes de qualquer merge ou push na `main`, confirme no painel o estado real do Auto-Deploy e desative-o, ou garanta outro bloqueio da publicação automática, até o banco estar na revisão exigida pelo código.
2. Compare sempre a revisão do banco com a do código, mesmo sem arquivos novos em `migrations/versions`: `flask db current` (somente leitura, com as variáveis do Passo 1) × `flask db heads` (local). A head é a impressa por `flask db heads` no checkout do SHA que será publicado (em 28/10/2026, na `main` `1018999`: `81287c74387a`, migração da gestão contratual; não use um valor fixo deste documento) e a revisão aplicada em produção não está confirmada; se forem diferentes, o código atual depende da migração.
3. Para migrar: backup autorizado e restauração isolada aprovados ([Operação de backup](OPERACAO_BACKUP_RESTAURACAO.md)); janela com tráfego e escritores antigos suspensos, como exige a `e4c004a26b01`, por mecanismo que o operador deve definir e conferir no Render; então `flask db upgrade REVISAO_APROVADA` (substituir pelo id da revisão aprovada).
4. Com o banco na revisão exigida, publique manualmente o SHA aprovado (**Manual Deploy**), sem usar "latest commit" sem conferir o SHA; valide `/healthz`, `/healthz/db` e os fluxos principais antes de reabrir o tráfego.

Critérios, riscos e pendências: [00_02 — Proteção do banco e preparação das migrações](REESTRUTURACAO_COMERCIAL/00_02_PROTECAO_BANCO_MIGRACOES.md).

**Migração pendente `b7c4e2a9f031` → `e4c004a26b01` (incidente HTTP 500 do login, 25/10/2026):** o procedimento de referência é o [RELATORIO_CORRECAO_MIGRACAO_SUPABASE_NEXO.md](../RELATORIO_CORRECAO_MIGRACAO_SUPABASE_NEXO.md), seções 9 e 10 (autorização, sonda, backup verificado, variáveis só da sessão, proibição de `stamp`/`downgrade`). Ele é posterior a este roteiro genérico e prevalece sobre ele nessa migração, inclusive quanto à necessidade de suspender o tráfego.

## 6. Diagnóstico de Erros Comuns

| Erro | Causa Provável | Solução |
| :--- | :--- | :--- |
| `RuntimeError: SECRET_KEY válida é obrigatória` | `SECRET_KEY` ausente ou igual ao padrão de dev. | Adicione a variável `SECRET_KEY` no painel do Render. |
| `RuntimeError: DATABASE_URL PostgreSQL é obrigatória` | `DATABASE_URL` não configurada. | Insira a URL de conexão do Supabase. |
| `503 Service Unavailable` em `/healthz/db` | App não consegue falar com o banco. | Verifique as credenciais e se o banco Supabase não está pausado. |
| `400 Bad Request` em formulários | Token CSRF expirado ou ausente. | Atualize a página e tente novamente. |
| Erro de upload de arquivo | Disco efêmero do Render. | Verifique se o código está usando `storage.py` (que salva no banco). |
