# Baia de Segurança e Avaliação de Cenário

Duas entregas da mesma etapa (novembro de 2026):

1. **Baia de Segurança** (`security_gatekeeper.py`): triagem de todo relatório enviado, antes do motor de leitura e do banco.
2. **Avaliação de Cenário** (revisão `f4c2a8e1d9b6`): tabelas da captação de leads e dos dados de PDV do lead, com RLS e expurgo em 30 dias.

Nada disso foi aplicado no Supabase nem publicado. A migração só vai ao banco com autorização (seção 4).

## 1. Baia de Segurança

### 1.1 Onde ela roda

O processo do servidor não interpreta planilha (SEC-005), então a triagem tem duas metades:

| Etapa | Onde | O que faz |
|---|---|---|
| Cópia com teto | processo do servidor | Lê em blocos de 64 KiB até o teto + 1 byte. O teto é o menor entre o `ETLLimits` (hoje 20 MiB) e 50 MiB (`TETO_ABSOLUTO_BYTES`). O `Content-Length` não é autoridade. |
| Assinatura | processo do servidor | Lê só os primeiros 8 KiB (`inspecionar_arquivo`). Passam XLSX (ZIP), XLS (OLE2 com o fluxo `Workbook`) e texto delimitado (CSV/TXT), e o conteúdo precisa bater com a extensão. |
| Triagem profunda | worker isolado, operação `validate` (`triar`) | Validação de sempre do `etl_inputs`, leitura pelo motor, neutralização de fórmula, estrutura mínima de PDV, inventário de conteúdo ativo do XLSX e, se preciso, a cópia só com valores. |
| Liberação | processo do servidor | Grava no `storage` o arquivo original, quando não havia nada a neutralizar, ou a cópia neutralizada (`ler_liberado`, que confere de novo teto e assinatura e não segue link). |

Passam pela Baia o envio do cliente (`/cliente/upload`), o do consultor (`/admin/analise/<id>/upload`) e o Raio-X (`/onboarding/diagnostico` e a API). No Raio-X os arquivos não são gravados: valem o teto, a assinatura e a neutralização feita na leitura. Além disso, **toda leitura do motor** (`etl_inputs.read_raw`, usada no processamento e no Raio-X) sai com as células neutralizadas, inclusive a de arquivos gravados antes da Baia.

### 1.2 O que é recusado

A mensagem ao usuário é sempre `Arquivo fora do padrão aceito para auditoria.`, às vezes seguida de uma orientação fixa. Não há caminho, nome de arquivo nem detalhe do servidor na mensagem. No Raio-X ela leva o número do arquivo (`Arquivo 2: arquivo fora do padrão…`).

| Motivo (no código `quarentena:<motivo>` e no log) | Exemplos |
|---|---|
| `executavel_pe`, `executavel_elf`, `executavel_macho`, `executavel_wasm`, `atalho_windows` | .exe, .dll, binários Linux/macOS, WebAssembly, .lnk renomeados |
| `script` | shebang (`#!`), lote do Windows (`@echo`) |
| `marcacao` | HTML, XML, SVG e a planilha XML do Excel 2003 (muito PDV web exporta “.xls” assim) |
| `pdf`, `rtf`, `imagem`, `midia`, `compactado`, `binario` | PDF, RTF, PNG/JPEG/GIF/TIFF, RIFF, gzip/rar/7z/xz/zstd, qualquer conteúdo com controles demais |
| `documento_ole` | Word, MSI e outros documentos OLE que não são planilha |
| `xls_binario`, `xls_disfarcado` | Excel 97-2003 verdadeiro: reconhecido, mas o motor isolado não lê BIFF. Orientação: salvar como .xlsx ou .csv |
| `disfarce_<conteúdo>_como_<extensão>` | CSV renomeado para .xlsx, XLSX renomeado para .csv etc. Orientação: o conteúdo não corresponde à extensão |
| `estrutura_ausente` | Sem colunas legíveis de produto e quantidade ou valor (vendas), nem fornecedor/produto e valor (compras) |
| `neutralizacao_inviavel` | A cópia só com valores não reproduziu exatamente o que o motor leria do original (seção 1.4) |
| `copia_acima_do_teto` | A cópia passaria do tamanho que o motor aceita (perto do teto, cada célula neutralizada soma um byte). Orientação: dividir o relatório por período |

O texto UTF-16 (o “Texto Unicode” do Excel) continua aceito: o leitor do ETL já o lia descartando os NUL.

As recusas que já existiam no `etl_inputs` (CSV inválido, ZIP malformado, limites de linhas, colunas, expansão etc.) mantêm as próprias mensagens.

### 1.3 Neutralização de fórmula (CSV/Excel injection)

`neutralizar_celula` segue a recomendação OWASP:

- Começo em `=` ou `@`: ganha o apóstrofo (`'=cmd|' /C calc'!A0`).
- TAB, CR e LF iniciais viram espaço. Se depois deles vier fórmula, ela também ganha o apóstrofo.
- Começo em `+` ou `-`: só ganha o apóstrofo se o resto tiver algo que só fórmula tem (operador, `|`, `!`, `"`, `[ ]`, `:`, `;`, `&` ou chamada de função como `SOMA(`). O `-` final é o sinal contábil do PDV e não conta.
- A mesma regra vale depois de um resto de BOM que o leitor não consumiu (UTF-8 lido como cp1252, UTF-16) e depois de cada separador (`;` `,` TAB `|`) dentro da célula: quem abrir o arquivo reconhecendo o BOM, ou separando por outro caractere, veria ali o começo de uma célula (`ok;'=cmd…`).

**Nenhum número lido pelo ETL muda.** Valores com sinal comuns (`-12,50`, `-1 UN`, `+5%`, `-R$ 3,00`, `-1,5E+03`, `10,00-`) nem recebem o apóstrofo. Quando um valor com sinal recebe (`-2 un(s)`, `-3*`, `-1,5 kg/un`), os dois leitores do ETL o ignoram:
- a leitura de dinheiro (`_limpar_monetario`) já o ignorava;
- a de quantidade e estoque (`etl_processor._parse_valor`) passou a ignorá-lo nesta etapa. Antes ela lia o sinal pelo primeiro caractere, e a devolução virava venda.

A chave do produto ignora pontuação. Os testes conferem isso valor a valor e comparam os KPIs de um relatório com a leitura de antes da Baia (`tests/test_security_gatekeeper.py`, `NeutralizacaoTests` e `test_numeros_iguais_aos_da_leitura_de_antes_da_baia`).

Efeito colateral da mudança no `_parse_valor`: um `'-5` gravado literalmente no arquivo passa a valer -5, como o dinheiro já valia. Antes valia +5.

O efeito visível é só no nome exibido de um produto que comece assim, por exemplo `'=== PROMO ===`.

### 1.4 Macros, objetos e fórmulas do XLSX

`inventariar_xlsx` usa uma **lista do que é permitido**. O próprio arquivo só é liberado quando valem as quatro condições:
- toda parte do pacote é de dados: tipos de conteúdo, relações da raiz e da pasta, propriedades, pasta de trabalho, abas `sheetN.xml`, estilos, textos compartilhados e tema;
- as relações são internas e de dados;
- o parser XML (que segue a codificação declarada, inclusive UTF-16) não acha em **parte nenhuma** fórmula (`f`, `formula`), nome definido, objeto OLE, controle ou vínculo DDE. Toda parte é lida, e não só as abas: uma relação de aba pode apontar para o nome de uma parte passiva (tema, estilos, propriedades), e o pacote a lê como aba de lá;
- o tipo de conteúdo não é de macro.

Qualquer outra parte, em qualquer caminho, impede: macros VBA e XLM, ActiveX, OLE, vínculos externos, conexões, interface personalizada, binários (inclusive `printerSettings`), comentários e relações das abas. O pacote acha as partes pelas relações, então uma aba renomeada ou um objeto fora de `xl/embeddings/` também conta. O log diz o que havia pelos caminhos de costume (`macro_vba`, `objeto_ole` etc.) ou como `parte_nao_reconhecida`.

Qualquer achado, ou alguma célula neutralizada, faz a Baia gravar uma **cópia só com valores**:

- **XLSX:** uma aba e seis partes (tipos de conteúdo, as duas relações, pasta de trabalho, aba e textos compartilhados). Nenhuma fórmula, estilo, macro, objeto ou vínculo. As fórmulas viram o valor que o Excel tinha gravado. Número que o motor lê igual vai como célula numérica, e a linha e a célula seguinte não levam coordenada: a cópia não fica maior que o original. O texto com cara de milhar (`12.125`), o zero à esquerda (`007`) e o resto vão como texto.
- **CSV:** na codificação e no separador do original, com LF, para crescer só pelos apóstrofos. A cópia passa de novo pela assinatura antes de ser aceita: sem as aspas do original, um título como `"<< RELATÓRIO >>"` a faria parecer HTML, e a Baia tenta a forma com aspas em todos os campos.

A cópia só é liberada se a **releitura pelo mesmo motor** der exatamente a matriz do original já neutralizada. Se não der, o arquivo é recusado (`neutralizacao_inviavel`): nada com conteúdo ativo é gravado e nenhum número muda em silêncio.

Arquivo limpo é gravado byte a byte, como antes. Quando a cópia é gravada:

- `hash_arquivo` e `tamanho_arquivo` passam a ser os da cópia (o SHA-256 do original fica no log);
- `nome_arquivo_original` e a extensão continuam os do envio.

### 1.5 Prazo do worker

A cópia exige ler o arquivo duas vezes (o original e a conferência), então a operação `validate` passou a ter o dobro do prazo configurado, dentro do teto de 120 s do `ETLLimits`, como o Raio-X. Num XLSX de 450 mil células com fórmulas, a validação antiga levava 17 s neste computador e a triagem completa leva cerca de 63 s.

Só essa operação pode gravar um arquivo do tamanho da entrada (`RLIMIT_FSIZE`), porque é ela que grava a cópia. As demais continuam limitadas ao tamanho do resultado.

### 1.6 Quarentena e log

O arquivo recusado nunca chega ao `storage`. Ele fica só no diretório temporário da requisição e some com ele; a Baia não guarda arquivo suspeito.

O log (`security_gatekeeper`, nível WARNING) registra origem (`upload` ou `raio_x`), motivo, extensão, tamanho e SHA-256. Nunca registra conteúdo, nome do arquivo ou caminho. A cópia liberada gera um registro INFO com os achados e os dois hashes.

Ao rodar migrações no mesmo processo, o `fileConfig()` do Alembic desliga os loggers já criados (os testes religam). Em produção o processo web só roda o gunicorn, então os registros saem normalmente.

## 2. Avaliação de Cenário (revisão `f4c2a8e1d9b6`)

Expand com duas tabelas novas, sem tocar nas existentes. Os nomes das colunas de vazamento seguem a especificação, com cifrão (`vazamento_total_r$`, `vazamento_estimado_r$`): PostgreSQL e SQLite os aceitam sem aspas. Nos modelos, os atributos Python são `vazamento_total_reais` e `vazamento_estimado_reais`.

**`lead_avaliacao`** (`LeadAvaliacao`):

- chave `id` UUID, gerado pela aplicação e, no PostgreSQL, também por `gen_random_uuid()`;
- contato: `nome_contato`, `email`, `whatsapp` (opcional) e `nome_loja`;
- resumo: `score_saude_cadastral` (0 a 100, nulo até a avaliação terminar), `vazamento_total_r$` (NUMERIC 12,2, nulo até terminar) e `plano_recomendado_codigo` (FK para `plano_nivel.codigo`);
- prioridades em JSON: `resumo_urgencia_json`, `resumo_atencao_json` e `resumo_eletiva_json`;
- `status`: `pendente` (padrão), `em_processamento`, `concluido`, `processamento_manual`, `convertido` ou `expirado`;
- `criado_em` e `atualizado_em`;
- CHECKs: status, faixa do score, vazamento não negativo, nomes não vazios, e-mail com arroba e tamanho do WhatsApp;
- índices: plano, status e e-mail (o e-mail serve para localizar o titular num pedido da LGPD).

**`brainstorming_dados_pdv`** (`BrainstormingDadosPdv`):

- chave `id` UUID;
- `lead_id`, FK com `ON DELETE CASCADE` e índice;
- `ano_exercicio` (2000 a 2100);
- produto: `codigo_produto` e `descricao_produto`;
- números: `quantidade` (NUMERIC 14,3), `valor_venda_bruto` (14,2) e `custo_medio_registrado` (14,4, nulo = sem custo);
- marcas `flag_venda_sem_custo`, `flag_venda_abaixo_custo` e `flag_omissao_erp`, todas `false` por padrão;
- `vazamento_estimado_r$` (NUMERIC 12,2, padrão 0);
- `criado_em`, com índice para o expurgo.

**RLS:** igual ao da revisão `b3e9d6c1f7a2`. RLS ligado, sem FORCE. Onde existirem `anon` e `authenticated`, os privilégios deles são revogados e a política restritiva `deny_api_access` lhes nega tudo, mesmo com um GRANT posterior. `postgres` e `service_role` recebem `acesso_servidor`. A aplicação entra como dona das tabelas.

**Expurgo (TTL de 30 dias):** a função `expurgar_brainstorming_pdv(dias integer DEFAULT 30)`:

- apaga as linhas com mais de `dias` dias de leads que não converteram;
- marca como `expirado` o lead que ainda tinha linha vencida a apagar e ficou sem nenhuma (uma linha recente mantém o status). Trava, atualização e exclusão são comandos separados: o que quem segurava o lead gravou enquanto o expurgo esperava a trava (linha nova, linhas apagadas, status) vale;
- não toca no resumo em `lead_avaliacao`;
- roda como `SECURITY INVOKER`, com `search_path` fixo no esquema da migração e `pg_temp` por último (uma tabela temporária homônima não toma o lugar das tabelas);
- tem o EXECUTE revogado de `PUBLIC`, `anon` e `authenticated`, então a Data API não a chama;
- trava os leads antes das linhas, na ordem da aplicação.

O comando usa a mesma função no PostgreSQL e a mesma regra em SQLAlchemy no SQLite (`leads_avaliacao.py`):

```
flask leads expurgar-pdv              # simulação: conta e desfaz
flask leads expurgar-pdv --aplicar    # grava
flask leads expurgar-pdv --dias 45
```

A saída mostra só contagens.

O agendamento diário veio na revisão `6a8f2d4c0e13`: o job `nexo_expurgo_leads_pdv` do `pg_cron`, no próprio banco, às 06:15 UTC. Sem a extensão, a revisão não agenda nada e emite um `WARNING`, que vai para o log do PostgreSQL e não aparece na saída do `flask db upgrade`; o expurgo segue pelo comando e pela própria tela `/admin/leads`, que roda a mesma regra ao ser aberta, no máximo uma vez por hora. Ver `Docs/AVALIACAO_DE_CENARIO.md`, seção 5.

**Downgrade:** trava as duas tabelas e recusa se houver qualquer lead ou linha. Vazias, remove função, índices e tabelas.

Esta revisão é só o esquema, a retenção e o expurgo. O restante está em `Docs/AVALIACAO_DE_CENARIO.md`:
- o processamento no worker isolado;
- a staging ampliada (revisão `5e1b9c3a7d20`);
- as colunas da Etapa 2: cadastro, ajustes, contagens da triagem e limiares (revisão `7c4e2a9b5d31`);
- as colunas da Etapa 3: a data da última venda na staging e o resumo numérico da inteligência no lead (revisão `9b3e5d7f1a28`);
- a triagem, a matriz por categoria e o diagnóstico em JSON;
- a tela do consultor (`/admin/leads`).

## 3. Testes

- `tests/test_security_gatekeeper.py` (40): assinaturas e disfarces, BOM não reconhecido, fórmula depois de separador interno, cópia XLSX compacta, recusa por tamanho com orientação própria, XLS verdadeiro, UTF-16, neutralização sem mudar número (inclusive quantidade com sinal e unidade) nem chave de produto, KPIs iguais aos da leitura de antes da Baia, idempotência, cópia neutralizada de CSV e XLSX conferida pelo motor, macros/ActiveX/OLE/vínculos descartados, onze variantes com conteúdo ativo fora dos caminhos de costume (aba renomeada, aba com outra extensão, aba em UTF-16, aba no lugar do tema, dos estilos em UTF-16 ou das propriedades, OLE fora de xl/embeddings, OLE com nome de printerSettings, relação externa, DDE fora de xl/externalLinks e nome definido com DDE), título que começa com `<`, cópia perto do teto, estrutura mínima, recusa sem deixar arquivo, teto de 50 MiB em blocos, log sem conteúdo, envio do cliente e do consultor pelo portal, Raio-X e o worker real (só no Linux).
- `tests/test_leads_avaliacao.py` (10, SQLite): colunas e índices, cifrão, padrões, restrições, CASCADE, regra do expurgo, comando (simulação e aplicação), downgrade, downgrade da Etapa 2 recusado com dados novos e upgrade da Etapa 2 sem apagar linha de PDV.
- `tests/test_leads_postgresql.py` (31 verificações em PostgreSQL real): RLS, políticas, privilégios, UUID e padrões do banco, cifrão pelo ORM e por SQL, restrições, política restritiva com uma linha gravada, GRANT e uma política permissiva intrusa, gravação pelo serviço da Avaliação de Cenário, função endurecida e fora do alcance da Data API, mesmo resultado da função e da regra em SQLAlchemy (com o resumo mantido), expurgo concorrente com quem grava o lead, comando, CASCADE, downgrade recusado e aceito, novo upgrade, o job do expurgo num `pg_cron` simulado (agendado no upgrade, removido no downgrade e agendado de novo) a Etapa 2 (CHECKs novas e o downgrade da `7c4e2a9b5d31` recusado com dados 11.3 e aceito sem eles) e a Etapa 3 (a data e o resumo gravados pelo serviço, a CHECK da data e o downgrade da `9b3e5d7f1a28` recusado com dados 11.4 e aceito sem eles).
- Ajustados: `tests/test_backup_restore.py` (32 tabelas e uma linha de cada tabela nova) e `tests/test_contratos_postgresql.py` (tabelas posteriores).

## 4. Aplicação (só com autorização)

1. Backup do banco e conferência de que dá para voltar (`Docs/OPERACAO_BACKUP_RESTAURACAO.md`).
2. Prévia: `flask db upgrade b3e9d6c1f7a2:f4c2a8e1d9b6 --sql`.
3. Aplicar: `flask db upgrade f4c2a8e1d9b6`.
4. Conferir: as duas tabelas, RLS ligado, 4 políticas, nenhum privilégio de `anon`/`authenticated`, a função e `has_function_privilege('anon', 'expurgar_brainstorming_pdv(integer)', 'EXECUTE') = false`.

O código da Baia não depende da migração e funciona antes dela. A tela `/admin/leads` depende também das revisões `5e1b9c3a7d20` (staging ampliada), `7c4e2a9b5d31` (Etapa 2) e `9b3e5d7f1a28` (Etapa 3); antes delas, a tela mostra o aviso de migrações pendentes e não lê arquivo nenhum. A `6a8f2d4c0e13`, entre as duas primeiras, só agenda o expurgo no `pg_cron`. Aplique as quatro, nessa ordem; a `9b3e5d7f1a28` é a `head`. Ver `Docs/AVALIACAO_DE_CENARIO.md`, seção 9.

## 5. Pendências do dono

- **LGPD dos leads:** base legal e aviso de privacidade para coletar nome, e-mail, WhatsApp e loja de quem ainda não é cliente; consentimento registrado; prazo de retenção do próprio `lead_avaliacao` (o expurgo só apaga os dados de PDV, o resumo e o contato ficam); atendimento a pedido de exclusão (apagar o lead leva os dados por CASCADE). Nada disso foi inventado aqui.
- **Agendamento do expurgo:** a revisão `6a8f2d4c0e13` agenda no `pg_cron`. Falta decidir entre duas opções:
  - habilitar a extensão no Supabase (Database › Extensions), antes de aplicar a revisão ou depois, com `flask leads agendar-expurgo`;
  - manter a execução manual ou um cron externo.
- **Avaliação de Cenário:** as pendências da tela do consultor estão em `Docs/AVALIACAO_DE_CENARIO.md`, seção 10:
  - LGPD do lead;
  - limiares da triagem (provisórios);
  - regras de categoria ("Diversos" e "Outros", precedência dos níveis);
  - promoção intencional na venda abaixo do custo;
  - matriz por categoria depois dos 30 dias;
  - compras e perdas no fluxo do lead;
  - dashboard do cliente;
  - capacidade no Render;
  - captação pública;
  - conversão do lead.
- **TXT e XLS no portal:** a Baia já reconhece os dois, mas o portal aceita só .csv e .xlsx. Habilitar TXT exige mudar `ALLOWED_EXTENSIONS`, os campos de arquivo, o Guia e o NexoBot. Habilitar XLS exige um leitor BIFF (por exemplo `xlrd`), dependência nova a avaliar.
- **Análises antigas:** ao reprocessar, um produto cujo nome comece com fórmula passa a aparecer com o apóstrofo. Os números não mudam.
