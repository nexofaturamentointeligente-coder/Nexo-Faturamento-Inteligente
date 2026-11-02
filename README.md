# NEXO — Faturamento Inteligente

![Prévia animada da apresentação do NEXO](static/video/nexo_demo_preview.gif)

*Os valores e indicadores apresentados no vídeo são ilustrativos.*

**Plataforma de Engenharia de Dados e Business Intelligence para o varejo de micro e pequenas empresas.**

| | |
|---|---|
| **Stack** | Python · Flask · SQLAlchemy 2.x · PostgreSQL (Supabase) / SQLite · Pandas · NumPy · Socket.IO · Bootstrap 5 · Plotly.js · Claude (Anthropic) |
| **Status** | Produção (TCC) — multi-tenant, com segurança hardened, tempo real e IA |

O **NEXO** diagnostica descasamentos operacionais e financeiros no varejo a partir dos dados brutos exportados pelo PDV do cliente. Em vez de competir com sistemas de ERP ou contabilidade, a plataforma entrega uma camada de **inteligência sobre dados que o lojista já possui**, mas raramente consegue interpretar de forma estratégica.

O projeto nasceu como MVP acadêmico (Projeto Integrador) e evoluiu, ao longo do desenvolvimento, para uma aplicação com arquitetura de nível de produção: migração para Postgres/Supabase, motor de ETL hardened contra *chaos engineering*, segurança (CSRF, rate limiting, anti open-redirect, revalidação de sessão), tempo real via WebSocket, devolutiva gerada por IA com *fail-safe* determinístico, exportação executiva em PDF e um assistente de suporte (NexoBot).

---

## 📊 Conceito de Negócio & Governança de Dados

O NEXO **não é um sistema contábil**. Não calcula Lucro Real, Margem Líquida, CMV nem Estoque Físico Real — esses conceitos exigem reconciliação fiscal e contagens físicas que estão fora do escopo da plataforma. Essa é uma regra de domínio **inegociável**, reforçada em todas as camadas (ETL, IA, semáforos): o sistema nunca exibe lucro, margem ou rentabilidade.

O indicador central da plataforma é o **Indicador de Pressão de Estoque**:

```
Indicador de Pressão de Estoque = Total Comprado − Faturamento Total
```

Esse saldo estimado revela o **descasamento financeiro/operacional** entre compras e vendas do período analisado: quando positivo, indica que entrou mais mercadoria do que saiu (pressão de estoque crescente); quando negativo, o oposto. Trata-se de um sinal estratégico para a consultoria, não de um número contábil auditável.

### 🔐 Governança de Dados & Privacidade

- **Sem parsing linha a linha para o banco.** O motor de ETL lê os relatórios brutos do PDV com Pandas, mas **nunca grava transações item a item** (vendas individuais, notas de compra) nas tabelas do sistema — apenas os **KPIs agregados** (`indicador_analise`) e o ranking consolidado da Curva ABC (`produto_curva_abc`).
- **Arquivos brutos** ficam retidos via `storage.py` (no próprio Postgres, tabela `arquivo_armazenado`) apenas para permitir reprocessamento/auditoria do upload original — não são lidos novamente linha a linha pela aplicação fora do pipeline do ETL.
- **Minimização de dados (LGPD):** o banco permanece enxuto, com apenas o necessário para o diagnóstico estratégico.

---

## 🧭 Arquitetura Geral

Aplicação Flask multi-tenant (Admin/Consultoria × Cliente/Empresa) organizada em *blueprints*, com persistência em PostgreSQL (Supabase) ou SQLite, tempo real via Socket.IO e dois pipelines assíncronos (IA e e-mail) que nunca bloqueiam a requisição HTTP.

```
Cliente (browser)
   │  HTTP + WebSocket (cookie de sessão)
   ▼
Flask App Factory (app.py)
   ├── auth        — login, recuperação de senha, ativação de conta, 1º acesso
   ├── admin       — empresas, análises, ETL, lixeira, suporte, CMS do guia
   ├── cliente     — upload, histórico, análise publicada, suporte
   ├── api         — endpoints auxiliares (ex.: dados para os gráficos Plotly)
   └── notificacoes— sininho (marcar lidas / abrir)
          │
          ├── etl_processor.py   → Pandas/NumPy: leitura adaptativa + KPIs + Curva ABC
          ├── insights.py        → Semáforos 5W2H derivados dos KPIs
          ├── ai_devolutiva.py   → Claude (Anthropic) com fallback determinístico
          ├── pdf_export.py      → ReportLab: PDF executivo da análise publicada
          ├── suporte_bot.py     → fachada do NexoBot (pacote nexobot/: NLP local + Guia)
          ├── realtime.py        → Salas Socket.IO por usuário (user_<id>)
          ├── notifications.py / emails.py → disparados SEMPRE depois do commit
          └── storage.py         → único ponto de I/O de arquivo (bytes no Postgres)
          │
          ▼
   SQLAlchemy 2.x (Mapped/mapped_column) — Flask-Migrate/Alembic
          │
          ▼
   PostgreSQL/Supabase (produção) — pooled (6543) p/ runtime, direto (5432) p/ migrações
   ou SQLite (dev local rápido)
```

Para o detalhamento completo da arquitetura (incluindo diagrama Mermaid do fluxo de dados e o DER com as 13 entidades), ver **[`docs/RELATORIO_TECNICO.md`](docs/RELATORIO_TECNICO.md)**.

---

## ✅ Funcionalidades Principais

**Núcleo de negócio**
- Cadastro multi-empresa/multi-usuário (Admin/Consultoria gerencia N empresas-cliente).
- Upload de relatórios VENDAS + COMPRAS (`.csv`/`.xlsx`) por análise, com motor de ETL resiliente (ver seção dedicada abaixo).
- Indicador de Pressão de Estoque, Curva ABC (Pareto) vetorizada com NumPy, semáforos executivos (5W2H).
- Devolutiva consultiva: gerada por IA (Claude) a partir dos KPIs reais, com fallback determinístico local quando não há `ANTHROPIC_API_KEY` — o fluxo **nunca quebra** por falta de IA.
- Exportação da análise publicada em **PDF executivo** (ReportLab).
- Fluxo de publicação com rascunho → homologação → publicação, visível ao cliente apenas após publicado.

**Operação e ciclo de vida**
- **Sistema de Lixeira (Soft Delete + Hard Delete):** exclusão de empresa é reversível (`deletado_em`) até a exclusão permanente explícita, que então **cascade-deleta** análises, uploads, indicadores, relatórios e Curva ABC via ORM (`cascade="all, delete-orphan"`).
- Primeiro acesso com senha temporária + ativação de conta por token assinado (`itsdangerous`, expira em 15 min).
- Recuperação de senha por e-mail (mesmo mecanismo de token assinado).

**Tempo real e suporte**
- Notificações via WebSocket (Socket.IO) em sala exclusiva por usuário — sininho da navbar, sem broadcast aberto.
- **NexoBot Advanced 2.0:** manual interativo do NEXO e assistente de navegação, em português do Brasil e sem depender de IA externa — base de conhecimento versionada (`nexobot/saber/`, 173 registros verificados no código, com resumo, explicação, passo a passo e aprofundamento), perguntas amplas ("o que é o NEXO?", "como funciona?"), aprofundamento por contexto ("explique melhor", "me dê um exemplo", "e depois?"), comparações, roteiros guiados, busca BM25F no Guia (CMS `guia_topico`, que tem prioridade), higiene de conteúdo de teste, respostas em blocos estruturados e navegação apenas por catálogo fechado de páginas do cliente. Redação opcional via Hugging Face, sempre validada. Detalhes em [`Docs/NEXOBOT_ADVANCED.md`](Docs/NEXOBOT_ADVANCED.md).
- Central de chamados de suporte (Admin ↔ Cliente).

**Segurança (hardened)**
- CSRF global (Flask-WTF) em todas as rotas mutáveis.
- Rate limiting (Flask-Limiter) em login e recuperação de senha.
- Proteção anti open-redirect em todos os redirecionamentos que aceitam parâmetro externo (`next`, `referrer`, `link_destino`).
- Sessão amarrada ao `BOOT_ID` do processo (restart do servidor invalida sessões antigas) + revalidação de `is_active` a cada request (usuário desativado é deslogado imediatamente).
- Origens de WebSocket restritas (`SOCKETIO_CORS_ORIGINS`) — mitiga Cross-Site WebSocket Hijacking.
- Debug do Werkzeug bloqueado fora de `development` (recusa explícita a subir com `DEBUG=True` em produção).
- Erros de banco nunca vazam para o usuário (mensagens genéricas + log interno via `current_app.logger`).
- Lock otimista (`with_for_update`) no reprocessamento de uma análise, evitando corrida entre dois disparos simultâneos do ETL.
- Notificações/e-mails sempre disparados **depois** do `commit()` (dispatch-after-commit) — nunca há notificação fantasma de uma transação que sofreu rollback.

---

## 🛠️ Motor de ETL — Resiliência e Cobertura

O motor (`etl_processor.py`) foi submetido a uma bateria de *chaos engineering* com layouts reais de PDV/ERP brasileiros (encodings legados, cabeçalhos quebrados, planilhas com abas de capa, separadores variados, colunas deslocadas, linhas irregulares) e segue um contrato de robustez explícito:

> **Para qualquer arquivo recebido, o motor garante um de dois desfechos — nunca um terceiro:** sucesso honesto (extrai e consolida os KPIs reais) ou recusa acionável (diagnóstico claro do que faltou). **Jamais** uma queda do servidor ou uma análise calculada sobre dados inventados.

Destaques técnicos:
- Leitura adaptativa: múltiplos *encodings* (`utf-8-sig`, `cp1252`, `latin-1`), múltiplos separadores (`;`, `,`, `\t`, `|`) e `.xlsx` multi-aba (escolhe a aba com mais dados). Uploads aceitos: `.csv` e `.xlsx`; `.xls` não faz parte das dependências desta versão.
- Mapeamento de colunas por *aliases* (independe de nome exato/posição), com resolução determinística do conflito `id_produto` × nome do produto.
- **Derivação honesta** de totais ausentes (ex.: `quantidade × preço unitário`) — uma operação aritmética sobre dados reais, nunca fabricação de coluna.
- Curva ABC vetorizada com NumPy (`np.select`), sem `iterrows()` no caminho de cálculo.
- Rede de segurança final em `processar_arquivos_analise`: qualquer exceção inesperada é capturada e convertida em falha segura, isolada da requisição web.

Cobertura validada (6 camadas de falha, 8 layouts reais, 11+ cenários adversariais) e as decisões deliberadas de escopo (o que **não** foi implementado e por quê) estão documentadas em **[`docs/RELATORIO_COBERTURA_ETL.md`](docs/RELATORIO_COBERTURA_ETL.md)**.

---

## 🗂️ Estrutura do Projeto

```text
nexo_mvp/
├── docs/                    # Relatório técnico + relatório de cobertura/resiliência do ETL (Mermaid)
├── migrations/              # Versionamento de schema (Flask-Migrate/Alembic)
├── blueprints/               # Rotas Flask por contexto
│   ├── auth.py               #   login, ativação de conta, recuperação de senha
│   ├── admin.py               #   empresas, análises, ETL, lixeira, suporte, CMS
│   ├── cliente.py             #   upload, histórico, análise publicada
│   ├── api.py                 #   endpoints auxiliares (gráficos)
│   └── notificacoes.py         #   sininho (marcar lidas / abrir)
├── templates/                # Views Bootstrap 5, isoladas por perfil (admin/cliente)
├── app.py                    # Application Factory + entrypoint (socketio.run)
├── config.py                 # Configuração por ambiente (Config / Dev / Production)
├── extensions.py             # Instâncias compartilhadas (db, login, mail, socketio, csrf, limiter)
├── models.py                 # 13 modelos SQLAlchemy 2.x (Mapped/mapped_column)
├── etl_processor.py          # Motor de ETL (leitura adaptativa → sanitização → KPIs → Curva ABC)
├── insights.py                # Semáforos executivos 5W2H
├── ai_devolutiva.py            # Devolutiva por IA (Claude) + fallback determinístico
├── suporte_bot.py              # Fachada do NexoBot (interface estável)
├── nexobot/                    # NexoBot Advanced 2.0: normalização, intenções, contexto, Guia, decisão
│   └── saber/                  #   base de conhecimento versionada sobre o NEXO (14 assuntos)
├── pdf_export.py               # Exportação executiva em PDF (ReportLab)
├── realtime.py                 # Salas Socket.IO por usuário
├── notifications.py / emails.py # Disparo de notificações/e-mails (dispatch-after-commit)
├── storage.py                  # Ponto único de I/O de arquivo (uploads guardados no Postgres)
├── wsgi.py                     # Entrypoint de produção (gunicorn)
├── render.yaml                 # Blueprint de deploy no Render (plano free)
├── migrar_arquivos_para_db.py  # Copia uploads legados do disco local para o banco
├── test_etl.py                 # Homologação isolada do motor de ETL
├── init_db.py                  # Cria as tabelas do zero (SÓ SQLite local; recusa Postgres)
├── seed.py                     # Popula planos, segmentos e o usuário admin master
├── seed_guia.py                  # Popula a base de conhecimento do NexoBot/Guia
├── criar_cliente_demo.py         # Cria/reseta um cliente de demonstração (fluxo 1º acesso)
├── requirements.txt            # Dependências do ecossistema Python
├── .env.example                 # Template de variáveis de ambiente
└── .gitignore                   # Filtros de proteção (uploads, dados, .env, .venv)
```

---

## 🧰 Stack Tecnológica

| Camada | Tecnologia |
|---|---|
| Web framework | Flask 3.x (Application Factory + Blueprints) |
| ORM / Migrações | SQLAlchemy 2.x (`Mapped`/`mapped_column`) + Flask-Migrate/Alembic |
| Banco de dados | PostgreSQL via Supabase (produção/homologação) ou SQLite (dev local) |
| Processamento de dados | Pandas + NumPy (ETL vetorizado) |
| Autenticação | Flask-Login + `werkzeug.security` (hash de senha) + `itsdangerous` (tokens assinados) |
| Segurança | Flask-WTF (CSRF) + Flask-Limiter (rate limiting) |
| Tempo real | Flask-SocketIO (`async_mode="threading"`) + `simple-websocket` |
| E-mail | API HTTPS do Brevo (produção) ou Flask-Mail/SMTP (dev) — ver [Docs/EMAILS_TRANSACIONAIS.md](Docs/EMAILS_TRANSACIONAIS.md) |
| PDF | ReportLab (platypus) |
| IA — devolutiva | SDK oficial `anthropic` (Claude) |
| IA — suporte | NexoBot local (NLP próprio + BM25 sobre o Guia); redação opcional via Hugging Face Inference Providers |
| Front-end | Bootstrap 5 + Plotly.js (via CDN) |

Lista completa e versões exatas em [`requirements.txt`](requirements.txt).

---

## 🎬 Produção audiovisual

O MP4 (40 s, 1920×1080), a prévia GIF, a capa original em PNG e a capa otimizada em JPEG ficam em [`static/video/`](static/video/). Na seção **Demonstração** da página inicial pública, o player ([`static/js/landing-video.js`](static/js/landing-video.js)) inicia o vídeo sem som por script quando ele se aproxima da área visível e o repete continuamente, inclusive fora da tela: a rolagem controla apenas o som. Não há controles nativos de pausa, avanço, volume, velocidade, Picture-in-Picture ou download; o único controle permanente é **Expandir vídeo** (tela cheia). O áudio só toca com 100% do vídeo visível — descontando o cabeçalho fixo, o zoom por pinça e contêineres que recortem o player; na tela cheia vale a área da tela — e é silenciado quando cerca de 1 px sai da área visível ou quando a aba fica oculta. Se o navegador exigir interação para liberar áudio, aparece o botão temporário **Ativar som da demonstração**, que some após a autorização. Não há links públicos de download; isso não impede recuperar o MP4 estático por ferramentas do navegador. Cada reprodução transfere cerca de 12 MB do Render e o player sem pausa não atende à WCAG 2.2 (critério 2.2.2): medições, limitações (iPhone, segundo plano) e alternativas estão em [`Docs/VALIDACAO_VIDEO_RENDER.md`](Docs/VALIDACAO_VIDEO_RENDER.md). A capa JPEG otimizada foi preservada.

O código-fonte do vídeo está em [`tools/nexo-motion/`](tools/nexo-motion/) (React, TypeScript e Remotion), isolado da aplicação Flask. Essa pasta não participa do build nem do runtime no Render: o build Python versionado usa o instalador verificado de dependências, e o start usa Gunicorn, sem Node.js, Remotion ou FFmpeg. Para editar o vídeo, em uma máquina local com Node.js 22+, npm, Python 3, FFmpeg e Chromium, renderize novamente a partir da raiz do repositório:

```bash
cd tools/nexo-motion
npm ci
npm run render
npm run verify
```

A renderização sobrescreve o MP4, o GIF e o PNG de `static/video/`. A capa JPEG não é gerada pela ferramenta; recrie-a a partir do PNG, na raiz do repositório e com o Pillow do `requirements.txt`, usando `python -c "from PIL import Image; Image.open('static/video/nexo_demo_poster.png').convert('RGB').save('static/video/nexo_demo_poster.jpg', quality=80, subsampling=0, optimize=True, progressive=True)"`. Os arquivos só chegam à produção depois de versionados e publicados por deploy manual. Consulte o [README da produção](tools/nexo-motion/README.md) para os requisitos e a [validação](tools/nexo-motion/VALIDATION.md) para as especificações conferidas.

---

## 🚀 Guia de Instalação e Execução (passo a passo)

Este guia cobre a configuração do zero em **qualquer computador** (Windows/Linux/macOS). Compatível com **PowerShell** e **Bash** — apenas a ativação do `venv` diverge por SO.

### 1. Pré-requisitos

- **Python 3.12** instalado e no `PATH` (3.12.10 é a versão validada e configurada no Render).
- **Git**.
- Uma conta **Supabase** (gratuita) se for usar PostgreSQL em nuvem — ou pule essa parte e use **SQLite local** para rodar o sistema rapidamente sem depender de nuvem.

### 2. Clonar o repositório

```bash
git clone <url-do-repositorio>
cd nexo_mvp
```

### 3. Criar e ativar o ambiente virtual

```bash
python -m venv .venv
```

```powershell
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

### 4. Instalar as dependências

```bash
python -I tools/install_dependencies.py --requirements requirements.txt
```

O script confere o SHA256 do wheel oficial do pip 26.2.2, executa essa cópia corrigida e confirma sua versão antes dos requisitos. Usa PyPI oficial com TLS ativo; não lê `.env`, não repassa segredos da aplicação aos instaladores e ignora configurações/índices pip herdados. Para testes, use `--requirements requirements-dev.txt` no mesmo ambiente virtual. O Python deve ser 3.12. Falha de hash ou versão interrompe a instalação.

### 5. Configurar as variáveis de ambiente

```bash
cp .env.example .env
```

Abra o `.env` e preencha pelo menos:
- `SECRET_KEY` — string aleatória forte (`python -c "import secrets; print(secrets.token_hex(32))"`).
- `ADMIN_EMAIL` / `ADMIN_NOME` / `ADMIN_SENHA` — credencial inicial do administrador master.

Todo o resto é **opcional com fail-safe**: sem `ANTHROPIC_API_KEY` a devolutiva usa o gerador local; sem `HF_API_TOKEN` o NexoBot responde integralmente com o motor local; sem `BREVO_API_KEY` e sem `MAIL_USERNAME`/`MAIL_PASSWORD` o envio de e-mail é suprimido (estado `DESATIVADO` no log e no painel *Admin → Diagnóstico de e-mail*, sem quebrar o fluxo). Veja a tabela completa de variáveis abaixo.

### 6. Escolher e preparar o banco de dados

**Opção A — SQLite local (mais rápido para testar)**

Deixe `DATABASE_URL` comentado/ausente no `.env` (ou aponte para `sqlite:///instance/nexo_mvp.db`) e rode:

```bash
python init_db.py
```

Isso cria o schema do zero localmente (não use em produção — é destrutivo).

**Opção B — PostgreSQL/Supabase (recomendado para produção/homologação)**

1. Crie um projeto no [Supabase](https://supabase.com) e copie a *Connection String* **pooled** (porta `6543`) para `DATABASE_URL` e a **direta** (porta `5432`) para `DIRECT_URL` no `.env`.
2. Aplique as migrações usando a conexão **direta** (DDL não passa pelo PgBouncer):

```bash
# Windows (PowerShell)
$env:NEXO_DB_DIRECT="1"; flask db upgrade
```

```bash
# Linux / macOS
NEXO_DB_DIRECT=1 flask db upgrade
```

### 7. Popular dados iniciais (seed)

```bash
python seed.py
```

Cria os planos, segmentos e o usuário ADMIN master a partir das credenciais do `.env`. É idempotente — pode rodar de novo sem duplicar.

Para popular a base de conhecimento do NexoBot/Guia (use `NEXO_DB_DIRECT=1` se estiver em Postgres):

```bash
python seed_guia.py
```

Para listar, sem alterar nada, os tópicos do Guia com sinais de conteúdo de teste (prefixo `ZZTEST`, HTML na categoria etc.) e revisá-los pelo CMS:

```bash
python tools/auditar_guia.py          # tabela legível
python tools/auditar_guia.py --json   # para anexar a um chamado
```

*(Opcional)* para criar um cliente de demonstração com o fluxo de primeiro acesso já armado — somente em ambiente local (o utilitário recusa `FLASK_ENV=production` e qualquer banco que não seja SQLite ou PostgreSQL em `localhost`); a senha temporária vem do ambiente e nunca é impressa:

```bash
NEXO_AMBIENTE_DEMO=1 NEXO_DEMO_SENHA='uma-senha-temporaria-longa' python criar_cliente_demo.py
```

### 8. (Opcional) Homologar o motor de ETL isoladamente

Valida parsing, sanitização e cálculo de KPIs contra arquivos reais — sem subir o Flask:

```bash
python test_etl.py .\dados\Vendas_2025.xlsx .\dados\Compras_2025.xlsx 1
```

### 9. Subir o servidor

```bash
python app.py
```

Acesse `http://127.0.0.1:5000` e autentique-se com o `ADMIN_EMAIL`/`ADMIN_SENHA` definidos no `.env`. O servidor sobe com suporte a WebSocket (`socketio.run`) — para produção, use o gunicorn via `wsgi.py` (ver seção de deploy abaixo), nunca o servidor de desenvolvimento do Werkzeug.

> Variáveis adicionais de runtime: `HOST` (default `127.0.0.1`) e `PORT` (default `5000`) sobrescrevem o bind do servidor; `FLASK_ENV` controla `development`/`production` (em `production`, `DEBUG` é forçado a `False` e o app se recusa a subir com debugger ativo).

> Redes que só liberam HTTP/HTTPS (ex.: Wi-Fi de faculdade/empresa) bloqueiam as portas do Supabase (5432/6543) e do SMTP (587): rodando localmente nelas, o banco não conecta. A versão hospedada não tem esse problema — quem acessa só precisa de HTTPS.

### 10. Preparação para Render + Supabase + Brevo

O Blueprint mantém o plano Free existente para avaliação. Consulte limites e adequação do plano antes de usar dados reais. A publicação e a configuração dos serviços são etapas manuais posteriores; veja [o roteiro de preparação](Docs/PREPARACAO_RENDER.md).

| Peça | Serviço (plano free) | Papel |
|---|---|---|
| Aplicação | **Render** Web Service | gunicorn + Flask-SocketIO, HTTPS público (`*.onrender.com`) |
| Banco | **Supabase** Postgres | dados + arquivos enviados (`arquivo_armazenado`) |
| E-mail | **Brevo** API transacional (300/dia) | o Render Free bloqueia SMTP (portas 25/465/587); a API usa HTTPS |
| Monitoramento opcional | Monitor HTTP externo | `/healthz` verifica o processo e `/healthz/db` verifica uma consulta ao banco; não garantem disponibilidade contínua. |

1. **Brevo:** crie a conta, verifique o remetente `nexofaturamentointeligente@gmail.com` (*Senders, Domains & Dedicated IPs → Senders*) e gere uma chave **de API** (`xkeysib-…`, não a chave SMTP) em *SMTP & API → API Keys*. Em *Brevo → Settings → Security → Authorized IPs*, autorize de uma só vez todas as faixas de IP de saída do Render (*serviço → Connect → Outbound*) ou desative o bloqueio para a API: com o bloqueio ativo, o Brevo recusa IPs não autorizados mesmo com a chave válida. Passo a passo, teste real em 4 etapas e diagnóstico em [Docs/EMAILS_TRANSACIONAIS.md](Docs/EMAILS_TRANSACIONAIS.md).
2. **Render, após autorização:** selecione o repositório em *New → Blueprint*. Preencha `DATABASE_URL`, `DIRECT_URL` e `BREVO_API_KEY`; confirme o remetente. O build instala somente dependências. Migrações e seeds precisam de execução manual revisada, antes de liberar o serviço. O start mantém `gunicorn -w 1 --threads 50 --timeout 180 -b 0.0.0.0:$PORT wsgi:app`. O Blueprint declara o deploy automático desabilitado, mas confirme a configuração efetiva no painel (ver [PREPARACAO_RENDER.md](Docs/PREPARACAO_RENDER.md)); depois de cada merge na `main`, confira em `GET /healthz` que `versao` é o commit esperado (com o deploy automático desligado, faça o *Manual Deploy*).
3. **UptimeRobot:** monitor HTTP(s) para `https://<seu-servico>.onrender.com/healthz/db`, intervalo de 5 minutos.
4. **Uploads antigos:** na máquina com os arquivos originais, `python migrar_arquivos_para_db.py --dry-run` apenas inspeciona. Após revisar o resultado, as referências e o backup do banco, `python migrar_arquivos_para_db.py --apply` aplica a migração. Se faltar um arquivo ou divergir o hash de uma planilha, a transação é cancelada. Não incluir esses arquivos no GitHub.

Limitações conhecidas do plano free: 512 MB de RAM e 0,1 CPU (ETL e PDF ficam mais lentos que localmente); se o monitor parar, o Render hiberna após 15 min sem acesso e o primeiro acesso leva ~1 min; qualquer reinício do servidor encerra as sessões abertas (proteção por `BOOT_ID`); o Brevo reescreve o endereço do remetente `@gmail.com` para um domínio próprio (o nome "NEXO" é mantido) — domínios gratuitos não podem ser autenticados, o que favorece o spam; para produção, autentique um domínio próprio (SPF/DKIM/DMARC).

---

## ⚙️ Variáveis de Ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `FLASK_ENV` | Não (default `development`) | `development` ou `production`. |
| `SECRET_KEY` | **Sim em produção** | Chave de assinatura de sessão/CSRF/tokens. |
| `DATABASE_URL` | **Sim em produção** | PostgreSQL em produção; SQLite local somente em desenvolvimento. URLs PostgreSQL sem driver explícito usam `psycopg2`. |
| `DIRECT_URL` | Só para migrações em Postgres | Conexão direta (5432) usada por `flask db upgrade`. |
| `NEXO_DB_DIRECT` | Não | `1` para forçar o uso da conexão direta (DDL/migrações). |
| `ADMIN_EMAIL` / `ADMIN_NOME` / `ADMIN_SENHA` | Sim (lidas pelo `seed.py`) | Credencial do administrador master inicial. |
| `UPLOAD_FOLDER` / `MAX_CONTENT_LENGTH_MB` | Não | Diretório e limite de tamanho dos uploads. |
| `SOCKETIO_CORS_ORIGINS` | Não | Origens permitidas no handshake WebSocket (CSV). |
| `RATELIMIT_STORAGE_URI` | Não (default `memory://`) | Backend do rate limiting; use Redis em multi-instância. |
| `MAIL_SERVER` / `MAIL_PORT` / `MAIL_USE_TLS` / `MAIL_USE_SSL` / `MAIL_USERNAME` / `MAIL_PASSWORD` / `MAIL_DEFAULT_SENDER` | Não (fail-safe) | SMTP para e-mails de ativação/recuperação/publicação (fora do Render Free, que bloqueia SMTP). Sem credenciais, o envio é suprimido. `MAIL_DEFAULT_SENDER` é também o remetente do Brevo e precisa estar verificado nele; valores vazios valem como ausentes. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Não (fail-safe) | Geração da devolutiva por IA. Sem chave, usa o fallback determinístico. |
| `HF_API_TOKEN` / `HF_MODEL` | Não (fail-safe) | Redação opcional das respostas do NexoBot via roteador da Hugging Face (uso cobrado pela HF). Sem token, o NexoBot funciona 100% local. |
| `BREVO_API_KEY` / `BREVO_API_URL` | Não (fail-safe); **sim no Render** | Envio de e-mail pela API HTTPS do Brevo (porta 443). Com a chave definida, substitui o SMTP (necessário no Render Free, que bloqueia SMTP). Use a chave de API (`xkeysib-`), não a SMTP; a URL precisa ser `https`. |
| `NEXO_EMAIL_TIMEOUT` / `NEXO_EMAIL_ALERTA_ADMIN` | Não (defaults `15` / `1` em produção) | Prazo, em segundos, de cada tentativa de envio (API e SMTP) e aviso no sininho dos administradores quando o provedor não aceita um e-mail. Ver [Docs/EMAILS_TRANSACIONAIS.md](Docs/EMAILS_TRANSACIONAIS.md). |
| `PROXY_FIX_HOPS` | Não (default `0`) | Nº de proxies reversos confiáveis à frente da app (Render = `1`): IP real do cliente no rate limit e links `https://` nos e-mails. |
| `RENDER_EXTERNAL_URL` | Automática no Render | URL pública do serviço; é adicionada às origens permitidas do WebSocket e, sem `NEXO_PUBLIC_URL`, é a origem canônica dos links sensíveis. |
| `NEXO_PUBLIC_URL` | Sim em produção (ou `RENDER_EXTERNAL_URL`) | Origem pública canônica (`https://dominio`) usada para montar os links de ativação, recuperação de senha, chamados e publicação — nunca o cabeçalho `Host` da requisição. Em produção precisa ser `https`. |
| `NEXO_TRUSTED_HOSTS` | Não | Hosts adicionais aceitos nas requisições (CSV; `.dominio` aceita subdomínios). Em produção, o host da origem pública e o da plataforma são aceitos sempre; qualquer outro `Host` recebe 400. |
| `NEXO_CSP_REPORT_ONLY` | Não (default `0`) | `1` envia a Content-Security-Policy em modo somente-relatório (observar violações sem bloquear) — útil num primeiro rollout; `0` impõe a política. |
| `NEXO_SENHAS_VAZADAS` | Não (default `0`) | `1` consulta vazamentos conhecidos ao definir/redefinir senha, por k-anonimato (só 5 caracteres do hash saem do servidor; a senha nunca). Indisponibilidade do serviço não bloqueia a troca (fail-open, registrado em log). |
| `NEXO_DB_POOL_SIZE` / `NEXO_DB_MAX_OVERFLOW` / `NEXO_DB_POOL_TIMEOUT` | Não (defaults `5` / `10` / `10`) | Pool de conexões do SQLAlchemy; só se aplica a PostgreSQL (ignorado em SQLite). |
| `NEXO_SQL_ECHO` | Não (default `0`) | `1` ecoa o SQL no log em desenvolvimento; sem efeito em produção. |
| `NEXO_LIMITE_LOGIN_CONTA` / `NEXO_LIMITE_RECUPERACAO_CONTA` | Não (defaults `200 per 15 minutes` / `10 per hour`) | Limites por **conta alvo** (hash do e-mail) no login e na recuperação de senha, somados aos limites por IP. Mantenha o do login acima de 10/min × janela: um orçamento menor seria esgotável de um único IP e trancaria o e-mail alvo (inclusive o do administrador). Lidos a cada requisição. |
| `HOST` / `PORT` | Não | Bind do servidor (`python app.py`). |

Template completo em [`.env.example`](.env.example).

---

## 🏛️ Matriz de Decisões de Engenharia

| Componente técnico | Escolha arquitetural | Justificativa de engenharia |
|---|---|---|
| **Padrão web** | Application Factory | Múltiplas instâncias por ambiente, testes isolados, sem imports circulares entre `app`, `models` e `extensions`. |
| **ORM** | SQLAlchemy 2.x com `Mapped`/`mapped_column` | Sintaxe moderna com *type hints* nativos; valida tipos em desenvolvimento e é a API recomendada atual. |
| **Banco** | PostgreSQL (Supabase) em produção, SQLite em dev | Postgres dá concorrência real e suporta múltiplas instâncias; SQLite permite rodar o projeto sem nenhuma dependência externa. |
| **Pool de conexão** | `pool_pre_ping=True` + `pool_recycle=280` | O PgBouncer do Supabase (modo *transaction pooling*) fecha conexões ociosas; a validação prévia e o reciclo evitam erros de conexão "morta". |
| **Exclusão em cascata** | Cascade no **ORM** (`cascade="all, delete-orphan"`), não `ON DELETE CASCADE` no banco | Evita depender de `passive_deletes` (que remove a rede de segurança das FKs) e de uma migração de schema de última hora; o controle de exclusão fica explícito no código Python. |
| **Lixeira (exclusão de empresa)** | Soft Delete (`deletado_em`) + Hard Delete explícito | Unifica reversibilidade operacional com a necessidade ocasional de expurgo definitivo, sem dois sistemas paralelos. |
| **Segurança de sessão** | Cookie amarrado a `BOOT_ID` + revalidação de `is_active` por request | Garante que reiniciar o servidor invalida sessões antigas e que desativar um usuário o desloga imediatamente, sem esperar expiração de cookie. |
| **CSRF/Rate limit** | Flask-WTF + Flask-Limiter | Mitigam CSRF em todas as rotas mutáveis e *brute force* em login/recuperação de senha, com mudança mínima de código (decorators). |
| **Tempo real** | Socket.IO com `cors_allowed_origins` restrito + sala por usuário | Mitiga Cross-Site WebSocket Hijacking e garante que notificações nunca sejam recebidas por outro usuário (sem broadcast aberto). |
| **Disparo de notificação/e-mail** | Sempre **depois** do `commit()` (dispatch-after-commit) | Evita notificações fantasmas referentes a uma transação que sofreu rollback. |
| **Curva ABC** | Vetorizada com NumPy (`np.select`) | Performance em catálogos grandes, sem `iterrows()` no caminho crítico de cálculo. |
| **Resiliência do ETL** | Contrato de robustez (sucesso honesto **ou** recusa acionável, nunca fabricação de dado) | Evita o pior cenário de produção: uma análise "bem-sucedida" calculada sobre dados inventados. Ver [`docs/RELATORIO_COBERTURA_ETL.md`](docs/RELATORIO_COBERTURA_ETL.md). |
| **Armazenamento de upload** | Abstração única em `storage.py`, bytes no Postgres (`arquivo_armazenado`) | Hosts gratuitos têm disco efêmero; no banco o arquivo sobrevive a deploys/reinícios e entra na mesma transação do registro que o cita. Continua sendo o ponto de troca único para *object storage* (Supabase Storage/S3) sem tocar nas rotas. |
| **IA (devolutiva e suporte)** | Sempre com *fallback* determinístico local | O sistema nunca depende de uma API externa para funcionar — IA é um realce, não um requisito. |
| **Persistência do ETL** | Apenas KPIs e ranking consolidados via ORM | Mantém o banco enxuto, alinhado à LGPD; dados transacionais brutos não são parseados linha a linha para tabelas do sistema. |

---

## 📚 Documentação Complementar

- **[`docs/RELATORIO_TECNICO.md`](docs/RELATORIO_TECNICO.md)** — relatório técnico completo (arquitetura, tecnologias, funcionalidades) com diagrama Mermaid de arquitetura/fluxo de dados e DER das 13 entidades. Referência principal para a defesa do TCC.
- **[`docs/RELATORIO_COBERTURA_ETL.md`](docs/RELATORIO_COBERTURA_ETL.md)** — contrato de robustez do motor de ETL, taxonomia de falhas em 6 camadas, matriz de testes (layouts reais + *chaos engineering*) e decisões deliberadas de escopo.

---

## 📄 Licença

Projeto acadêmico — direitos reservados aos autores.
