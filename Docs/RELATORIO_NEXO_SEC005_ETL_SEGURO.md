# NEXO-SEC-005 — ETL CSV/XLSX com orçamento de recursos

Data: 24/10/2026, America/Sao_Paulo. Relatório técnico pré-publicação.
Este snapshot registra a implementação e os testes locais. O relatório final
externo e a descrição da PR registrarão os fatos posteriores de publicação/CI.
Nenhum dado comercial real ou serviço de produção foi usado nos testes.

## A. Identificação

- Projeto: NEXO: Faturamento Inteligente.
- Repositório: `Helio965/Nexo---Faturamento-Inteligente---Render`.
- Base remota: `ff148796d9b579418b3782d472eb44fb0025fe6f`.
- Branch exclusiva: `security/sec-005-etl-resource-limits`.
- Ambiente local: Linux, Python 3.12.14, PostgreSQL privado 17.11, Chromium e
  Gunicorn 23.0.0. Dependências corrigidas nas SEC-006/006B preservadas.
- SEC-004: integrada externamente na PR #17; migração `e4c004a26b01` em produção
  não confirmada. Não foi aplicada nem alterada nesta tarefa.

Antes das alterações, o checkout estava limpo na branch SEC-004. A main remota
foi conferida por fetch/API; nenhuma implementação/PR exclusiva SEC-005 foi
encontrada. A branch nova partiu da main atual, preservando as correções anteriores.

## B. Diagnóstico

Fluxo: upload autenticado/CSRF → storage → homologação administrativa →
materialização do arquivo → leitura → cabeçalhos → normalização → KPIs/ABC →
persistência na transação da análise. Clientes não executam o ETL comercial.

`_ler_bruto` carregava todas as abas com `sheet_name=None`, materializava texto
CSV integral e listas de linhas para vários separadores. Não havia orçamento
de expansão, campos, células ou processamento. A detecção de período podia
reler o arquivo; há também cópias durante normalização/agrupamento pandas.
Essas operações permanecem limitadas, sem alterar a matemática comercial.

Uploads admin/cliente usavam `arquivo.read()` ilimitado. O BYTEA recuperado pelo
storage era carregado inteiro antes de qualquer limite e usava cache comum.
O lock de processamento da análise não era respeitado pelos uploads. Na falha,
um rollback completo soltava esse lock antes de marcar uploads como ERRO,
permitindo atingir um arquivo recém-substituído. Mensagens de exceção podiam
expor caminhos e detalhes internos à interface.

## C. Solução implementada

Leitura e validação compartilhadas em `etl_inputs.py`, política finita em
`etl_limits.py`, upload limitado em `etl_uploads.py` e execução em filho novo
por `etl_runtime.py`/`etl_worker.py`. O filho somente valida ou calcula; não
recebe sessão SQLAlchemy, credenciais, aplicação Flask ou configuração de banco.
Os imports de modelos foram movidos para a persistência, executada no pai.

As assinaturas públicas `calcular_kpis_em_memoria(vendas, compras)` e
`processar_arquivos_analise(vendas, compras, id_analise, db_session)` permanecem.
O cálculo comercial original foi preservado em `_calcular_kpis_local`; a função
pública executa-o sob isolamento. Dados completos não são persistidos; somente
KPIs finais e TOP_N da Curva ABC seguem para o banco.

Política padrão e tetos absolutos (inteiros positivos; memória mínima 128 MiB):

| Campo | Padrão | Teto de configuração |
| --- | ---: | ---: |
| `max_file_bytes` | 20,971,520 | 20,971,520 |
| `max_sheets` | 16 | 64 |
| `max_zip_entries` | 256 | 1,024 |
| `max_uncompressed_bytes` | 33,554,432 | 67,108,864 |
| `max_zip_ratio` | 200 | 1,000 |
| `max_rows` | 100,000 | 200,000 |
| `max_columns` | 128 | 256 |
| `max_cells_per_sheet` | 500,000 | 500,000 (trava rígida desde 11/2026) |
| `max_cells_per_file` | 1,000,000 | 2,000,000 |
| `max_field_chars` | 16,384 | 32,768 |
| `max_record_chars` | 131,072 | 1,048,576 |
| `timeout_seconds` | 60 | 120 |
| `memory_mb` | 384 | 512 |
| `max_output_bytes` | 4,194,304 | 4,194,304 |

Variáveis `ETL_<NOME_DO_CAMPO_EM_MAIÚSCULAS>` configuram os valores; por exemplo,
`ETL_MAX_ROWS`. `Config.ETL_LIMITS` contém a política validada. Overrides parciais
mantêm os demais padrões. Zero, negativos, booleanos, floats, desconhecidos e
valores excessivos são recusados. Configuração inválida impede inicialização;
se alterada em runtime, a operação falha sem desativar as proteções.

Justificativas: 20 MiB mantém o limite Flask existente; metadados ZIP e expansão
são limitados separadamente. As fixtures cobrem até 74 linhas, 8 colunas,
3 abas e 5.956 bytes; a amostra média mede CSV de 10.001×6 e XLSX de 4 abas com
72.030 células por arquivo e até 4.371.644 bytes expandidos. Os padrões oferecem
folga finita para esses cenários, sem afirmar que todo arquivo comercial cabe.
16 abas, 100 mil linhas e demais máximos não foram submetidos a teste de carga
até o teto; testes de fronteira usam limites reduzidos e entradas pequenas.
384 MiB de AS considera o baseline de imports medido (~176 MiB de espaço
virtual) e margem para estruturas pandas; não equivale a RAM total do Render.
60 s fica abaixo do timeout Gunicorn versionado de 180 s, permitindo limpeza.
Os valores são uma política conservadora revisável em homologação, não SLA.

## D. Segurança XLSX

1. Arquivo regular, existente, não vazio e no limite antes do parser.
2. Contagem REAL do diretório central em streaming antes de `ZipFile`, sem
   confiar apenas no total EOCD. São recusados diretórios inconsistentes,
   truncamento, sobreposição, caminhos inseguros, duplicados, links simbólicos,
   criptografia e compressão diferente de STORED/DEFLATE.
3. DEFLATE validado diretamente em blocos, com limites no volume efetivamente
   expandido, razão, CRC, EOF e correspondência com metadados locais/centrais.
   Um file_size falsificado não permite validar só o prefixo do conteúdo.
4. XML em streaming com proibição de DTD/entidades, profundidade, nós, texto,
   linhas, coordenadas, células reais e retângulos. Partes OPC renomeadas são
   identificadas pelo conteúdo, inclusive BOM dividido entre blocos, não apenas
   pela extensão; somatório observado nas abas fornece uma defesa adicional.
5. openpyxl `read_only=True`, `data_only=True`, `keep_links=False` percorre as
   abas sem DataFrames simultâneos. A contagem reproduz valores ausentes do
   pandas, erros, strings NA e fórmulas cacheadas; empate mantém a primeira aba.
6. Apenas a vencedora é materializada pelo mesmo parser pandas/dtype anterior.
   O workbook é fechado em `finally`. Não há extração arbitrária do ZIP.

A preflight percorre também shared strings/styles e outras partes XML dentro
do orçamento expandido. O teto de memória/tempo do filho complementa os
limites estruturais caso a biblioteca aloque internamente antes de retornar.

## E. Segurança CSV

Leitura incremental em passagens limitadas, mantendo encodings
`utf-8-sig`, `cp1252`, `utf-8`, `latin1` e separadores `;`, `,`, TAB, `|`.
O separador é escolhido pelo máximo de colunas observado no arquivo inteiro,
não por amostra inicial. Empates mantêm a ordem anterior. Arquivos binários
com assinaturas conhecidas disfarçados como CSV são recusados.

Depois do SEC-005, o saneamento de 11/2026 acrescentou o reparo de codificação:
o CSV que cai para `cp1252` ou `latin1` tem o UTF-8 desfeito refeito célula a
célula (`Docs/SANEAMENTO_MOTOR_ETL.md`, seção 7).

Registros físicos/lógicos com aspas e quebras de linha têm limite de caracteres;
campos, linhas, colunas e retângulo são validados antes de materializar. O CSV
usa o menor orçamento de células de tabela/arquivo. Candidatos de separador
não são guardados simultaneamente, nem `csv.field_size_limit` global é alterado.
Só o separador escolhido define os campos, evitando recusar linhas legítimas
porque outro separador combina tudo num campo. NUL é removido como antes.

Linhas irregulares continuam preenchidas com None; a matriz retangular só é
construída após verificar sua dimensão. Vazio, incompatível, excessivo ou
malformado gera recusa segura, nunca KPIs inventados.

## F. Uploads

Ambos os caminhos conservam autenticação, ADMIN/CLIENTE, CSRF, vínculo/status,
SHA-256, storage e transições. A leitura em blocos impõe máximo+1 sem depender
de Content-Length. Arquivo vazio, extensão/estrutura incompatível ou excesso
é recusado ANTES de salvar ou substituir o upload anterior. A preflight isolada
valida recursos e formato; não calcula KPIs no upload do cliente.

Uma admissão compartilhada por processo cobre toda POST de upload OU
processamento, incluindo bytes, materialização, espera por locks e confirmação.
Se ocupada, a requisição recebe mensagem para tentar novamente, sem ler o
conteúdo nem alterar status. GET e os guards de autenticação/CSRF não são
relaxados. Isso evita que 50 threads retenham arquivos grandes simultaneamente.
O framework ainda pode preparar/spoolar requisições antes do decorator; essa
admissão não constitui um limite global de tráfego ou disco do proxy/Flask.

Uploads adquirem `Analise FOR UPDATE` e recarregam vínculo/status antes da
substituição. O processamento usa o mesmo lock até sucesso OU marcação de erro.
O orquestrador usa SAVEPOINT para KPIs/ABC; uma falha reverte somente suas
escritas, mantendo dados anteriores e a trava externa. Erro que invalide a
transação inteira causa rollback/retorno genérico, sem readquirir e marcar um
upload novo de outra requisição. Nenhum sucesso parcial/CONCLUIDO automático.

Blobs novos e registros participam da transação. Fontes legadas não são apagadas
antes do commit; referências compartilhadas são preservadas. Rejeição não
produz notificação de processamento bem-sucedido. Arquivos .xls não são
habilitados: continuam recusados com orientação de conversão para CSV/XLSX.

## G. Memória e processamento

O filho recebe RLIMIT_AS de 384 MiB por padrão, antes de importar pandas, e
RLIMIT_CORE=0. Esse é limite de espaço virtual do processo isolado, não medição
nem teto RSS global da aplicação/container. A operação `avaliacao_cenario`
(Avaliação de Cenário de leads, ver `Docs/AVALIACAO_DE_CENARIO.md`) fica sempre em
no máximo 384 MiB, mesmo que `ETL_MEMORY_MB` suba o limite das outras operações. Há um filho ativo por processo
chamador e uma entrada de upload/processamento por processo servidor.

JSON de requisição ≤16 KiB e resultado ≤4 MiB; sem pickle. BLAS/OMP/MKL/NumExpr
usam uma thread. Ambiente filtrado exclui segredos do aplicativo e configurações
Python herdadas, mantendo controles de sistema/TLS/proxy. Não há shell ou
preexec_fn inseguro em servidor com threads. Rede e novos comandos externos
não fazem parte das operações permitidas do filho.

`storage.materializar_para_etl` consulta length real do BYTEA e carrega somente
com predicado length<=limite, ignorando tamanho declarado como autoridade.
Escreve em diretório privado próprio e libera o payload antes do cálculo.
Legado é copiado incrementalmente de arquivo regular, com limite real; FIFO é
recusado. Toda cópia própria é limpa sem apagar a fonte/cache utilizado por
outras operações. O cache compartilhado e `storage.resolver` continuam para
outros consumidores; problemas gerais de integridade pertencem à SEC-007.

## H. Tempo de execução

O pai impõe prazo de parede com `Popen.wait(timeout)`. Ao vencer ou cancelar,
encerra apenas o grupo do filho que criou e aguarda sua coleta; não tenta
cancelar apenas uma thread nem mata processos do servidor. Há também limite
CPU no filho e verificações cooperativas nos loops. A resposta segura distingue
excesso de tempo, recursos e formato; não imprime exceções internas.

Testes reais usam filho controlado que dormiria 10 s e comprovam encerramento,
coleta e limpeza após prazo de 1 s, além de cancelamento. Worker real confirma
AS/CPU via `getrlimit` e ausência dos módulos de banco/configuração. Gunicorn
real com 1 worker/50 threads executou validação e cálculo por HTTP de loopback.
Plataforma sem os limites POSIX exigidos recusa processamento, sem fingir uma
garantia equivalente. Kernel/host e permissões reais do Render precisam de
homologação; o teste local não certifica recursos do serviço de produção.

## I. Indicadores comerciais

Antes de alterar o leitor, a fonte original da base foi preservada fora do Git,
SHA256 `ceaed906a577e980519ad5a7906a8be92b5b838fd48bbdbda10c0c6218b2c7f6`.
Expected versionado contém somente resultados sintéticos produzidos por essa
fonte: 22 pares C1 Hemorragia, C2 Refém, C3 Ruptura, C4 Equilíbrio ABC e variações,
mais 47 matrizes cruas. Comparam todos os campos KPI/Curva ABC, período,
telemetria e alertas, além de valores/ausências do leitor.

Valores discretos exigem igualdade exata. Nos testes de floats, abs_tol=1e-9 e
rel_tol=1e-12 cobrem somente ruído IEEE, sem arredondamento financeiro excessivo.
As duas amostras médias apresentaram igualdade EXATA entre baseline, cálculo
local novo e função pública isolada. No CSV médio: faturamento 664316.0,
comprado 801915.0 e Pressão de Estoque 137599.0, antes e depois. No XLSX médio:
199107.5, 240455.25 e 41347.75, respectivamente, antes e depois.

São preservados aliases, normalização, composto/deslocado/decorativo/rodapés,
total explícito antes de unitário, quantidade×preço/custo quando necessário,
compras NF/item-level, rankings, exceção de coincidência do maior faturamento,
percentuais/classes A/B/C, TOP_N, produto/saldo parado e período real. Nenhum
KPI de lucro, margem, CMV ou rentabilidade foi criado.

Depois do SEC-005, o saneamento de 11/2026 revisou de propósito os rodapés, as
frações, o sinal no fim, as compras líquidas e a vírgula com 3 casas, com golden
de regressão contra o `91e5fb0` (`Docs/SANEAMENTO_MOTOR_ETL.md`). A análise
mensal continua sem lucro, margem e CMV.

Arquivos comerciais da homologação citada na documentação não estão disponíveis
nas fixtures do checkout. Não foram abertos dados reais; os cenários são
representações sintéticas. Não se declara homologação de todo PDV/ERP existente.

Medições controladas em processos novos, RSS em KiB incluindo imports:

| Amostra | Leitura antes/depois (s) | Cálculo local antes/depois (s) | Público novo (s) | RSS cálculo antes/local novo | RSS pai/filho público |
| --- | --- | --- | --- | --- | --- |
| csv_medium | 0.0468 / 0.1621 | 0.2655 / 0.4270 | 0.7572 | 132748 / 108228 | 92368 / 99128 |
| xlsx_four_sheets | 1.2293 / 2.0492 | 2.4887 / 4.0814 | 4.3137 | 124748 / 99468 | 92220 / 92220 |

CSV: 10.001 linhas incluindo cabeçalho×6 por arquivo, 531.690/694.317 bytes.
XLSX: quatro abas por arquivo, 72.030 células, 303.647/330.974 bytes comprimidos
e 4.179.576/4.371.644 expandidos; vencedora 3.002×6. Não foi medido o teto de 16
abas. Tempos de importação e VMS estão no JSON de evidências. Uma rodada por
operação, cache de SO não controlado, regressão concorrente. A fonte antiga
importava ORM/modelos; a nova não. Portanto não atribuir queda de RSS somente
ao leitor nem anunciar redução percentual, aceleração ou SLA. O custo temporal
adicional é esperado pelas passagens de validação e pelo isolamento.

## J. Testes

| Execução | Aprovados | Falhas/erros/ignorados | Tempo |
| --- | ---: | --- | ---: |
| Leitor + equivalência financeira + runtime/Gunicorn | 105 | 0/0/0 | 11,522 s |
| Uploads/storage/admissão | 28 | 0/0/0 | 9,539 s |
| Suíte geral final, Chromium e backup privado habilitados | **531** | **0/0/0** | **116,065 s** |
| PostgreSQL combinado SEC-002/003/004/005 e migração | **108** | **0/0/0** | **26,870 s** |

**Total da regressão final: 639 testes aprovados (531 + 108), zero falhas,
erros ou ignorados.** Os focados já estão incluídos nesses totais. A suíte
atual contém 68 casos de recursos do leitor, 15 de equivalência, 22 de runtime
e 28 de upload, além de nove casos PostgreSQL SEC-005. O teste adicional de
BOM dividido foi incluído após a primeira regressão (530); a suíte geral foi
executada novamente com o arquivo final (531).

Comandos efetivamente executados:

```bash
/tmp/nexo-sec006b-validation/bin/python -B -m unittest tests.test_etl_resource_limits tests.test_etl_financial_equivalence tests.test_etl_runtime_limits -v
/tmp/nexo-sec006b-validation/bin/python -B -m unittest tests.test_etl_upload_security -v
NEXO_TEST_POSTGRES_BIN=/tmp/nexo-pg17-tools/usr/lib/postgresql/17/bin NEXO_TEST_BROWSER=1 NEXO_TEST_CHROMIUM=/usr/bin/chromium /tmp/nexo-sec006b-validation/bin/python -B -m unittest discover -s tests -t . -v
NEXO_TEST_POSTGRES_BIN=/tmp/nexo-pg17-tools/usr/lib/postgresql/17/bin /tmp/nexo-sec006b-validation/bin/python -B -m unittest tests.postgresql_auth_tokens tests.postgresql_session_revocation tests.postgresql_company_restore_security tests.postgresql_company_restore_migration tests.postgresql_etl_resource_security -v
/tmp/nexo-sec006b-validation/bin/python -m pip check
git -c core.whitespace=cr-at-eol diff --check
```

A regressão habilita Chromium e backup privado e inclui WSGI/Gunicorn, PDF,
autenticação/tokens/sessões/Socket.IO, lixeira SEC-004, instalação SEC-006B,
dashboards, ETL, storage, uploads e NexoBot. Testes focados já incluídos na suíte
completa não são somados novamente. Falhas preliminares de desenvolvimento não
foram utilizadas como resultados aprovados. Nenhum teste foi removido/desativado.

## K. PostgreSQL

Cluster novo, privado, usuário aleatório, socket Unix 0700 e sem TCP, por meio
do helper existente. Nenhum DSN/servidor existente ou Supabase real é aceito.
Nove casos SEC-005 verificam KPIs/ABC anteriores, rejeição sem sucesso parcial,
reprocessamento, erro REAL do servidor dentro do SAVEPOINT e transação externa
utilizável, tamanho BYTEA real com metadados falsos, autorização e isolamento.

Três corridas observam `pg_blocking_pids` de conexões reais: processamento da
mesma análise, upload que espera e revalida status, e falha que não marca um
upload substituto. A admissão por processo é simulada independentemente por
thread apenas nesses casos para representar workers distintos; SQL, locks,
CSRF/autenticação e cálculo/persistência são reais. Testes HTTP separados
verificam o semáforo compartilhado real, sem esse ajuste de fronteira.

O workflow preserva os checks existentes e adiciona a execução obrigatória
`tests.postgresql_etl_resource_security`, sem skips silenciosos ou continue-on-error.

## L. Arquivos modificados

| Arquivo | Finalidade |
| --- | --- |
| `.github/workflows/nexo-security-ci.yml` | Nova etapa PostgreSQL obrigatória SEC-005. |
| `Docs/CI_SEGURANCA.md` | Comando e escopo da validação ETL. |
| `Docs/PENDENCIAS_SEGURANCA_SEC004_SEC005.md` | Estado local SEC-005 e pendência real de produção SEC-004. |
| `Docs/RELATORIO_NEXO_SEC005_ETL_SEGURO.md` | Relatório técnico A–R e evidências locais. |
| `app.py` | Validação da política finita na inicialização. |
| `blueprints/admin.py` | Uploads limitados, trava compartilhada e persistência/erro atômicos. |
| `blueprints/cliente.py` | Upload com preflight, limite e revalidação sob lock. |
| `config.py` | Configuração ETL validada, sem alterar dependências. |
| `etl_inputs.py` | Leitores CSV/XLSX e validação ZIP/XML incremental. |
| `etl_limits.py` | Defaults, tetos e contexto da política. |
| `etl_processor.py` | Leitor limitado, cálculo isolado, erros seguros e SAVEPOINT. |
| `etl_runtime.py` | Admissão do filho, protocolo JSON, prazo e coleta. |
| `etl_uploads.py` | Leitura limitada, preflight e admissão compartilhada de entrada. |
| `etl_worker.py` | Limites AS/CPU antes dos imports e cálculo sem banco. |
| `storage.py` | Materialização ETL exclusiva, limitada e limpa. |
| `tests/etl_equivalence_support.py` | Fixtures/resultados financeiros somente sintéticos. |
| `tests/fixtures/sec005_financial_expected.json` | Fixtures/resultados financeiros somente sintéticos. |
| `tests/postgresql_etl_resource_security.py` | Testes dedicados de limites, transação, recursos e integração. |
| `tests/test_etl_financial_equivalence.py` | Fixtures/resultados financeiros somente sintéticos. |
| `tests/test_etl_resource_limits.py` | Testes dedicados de limites, transação, recursos e integração. |
| `tests/test_etl_runtime_limits.py` | Testes dedicados de limites, transação, recursos e integração. |
| `tests/test_etl_upload_security.py` | Testes dedicados de limites, transação, recursos e integração. |
| `tools/measure_etl_resources.py` | Medições reproduzíveis somente sintéticas. |

Sem alteração de dependências, modelos/schema, migrações, build/start do Render,
SEC-001/002/003/004/006/006B, NexoBot, vídeos, identidade visual, fórmulas
comerciais ou arquivos reais. Nenhum arquivo preexistente excluído. Bancos,
.env, ambientes virtuais, dados comerciais, temporários e logs ficam fora do Git.

## M. GitHub

Este é um snapshot anterior ao commit/push/PR da implementação. Publicação está
autorizada exclusivamente na branch de segurança, PR em rascunho para main e
sem merge/auto-merge. A entrega final externa registrará os SHAs, URL e estado
verificado, sem confundir esses eventos com implantação.

## N. GitHub Actions

Ainda não executada para esta publicação no instante deste snapshot. A entrega
final e a descrição da PR registrarão o run do commit exato e o resultado real
de `security-sqlite-browser` e `security-postgresql`, incluindo o novo módulo
PG e os testes gerais SEC-005. YAML/local aprovado não substitui CI remota.

## O. Render e Supabase

Nenhum acesso, deploy, migração de produção, alteração de configuração ou
credenciais foi realizado pelo assistente. A SEC-005 não adiciona migração.
A aplicação exige o schema da SEC-004 já presente na base, cuja aplicação no
Supabase permanece não confirmada. Não foi corrigido ou inspecionado o ambiente
real. O Auto-Deploy foi anteriormente informado como ativado pelo proprietário.

Futura implantação, somente após autorização: revisar/homologar PR, conferir
schema/backup e situação SEC-004 com operador autorizado; confirmar e controlar
Auto-Deploy antes de qualquer integração; testar POSIX/rlimits/subprocessos no
serviço, medir memória total pai+filho/conexões/tráfego e ajustar política
finita conforme relatórios legítimos. Não presumir que merge aplica migrações.
Homologar upload, rejeição, reprocessamento, concorrência, timeout, KPIs e
notificações antes de liberar tráfego. Não automatizar esses passos nesta PR.

Rollback futuro: suspender tráfego/processamento, preservar uploads e
indicadores, investigar e preferir correção adiante. A SEC-005 não muda schema;
reverter somente seu código não desfaz a migração SEC-004 e não pode remover
suas proteções. Código anterior reintroduz consumo ilimitado, portanto exigir
medidas operacionais e decisão explícita. Não apagar arquivos/dados reais.

## P. Riscos residuais

- Limite AS/CPU é do filho; não é cap RSS global do container nem promessa de
  caber em todo plano Render. O pai e framework alocam buffers/metadados.
- Admissão é local por processo; múltiplos workers/instâncias têm orçamentos
  próprios. O Blueprint declara um worker, mas o painel real não foi lido.
- Upload multipart pode ser preparado antes do gate; não foi criado controle
  global de tráfego/disco, proxy ou novas filas/serviços externos.
- Os máximos não foram testados sob carga de produção nem com todo arquivo
  comercial real. Windows/plataforma sem rlimits exigidos recusa ETL.
- Processamento continua sob a trava da análise, por um prazo finito; novos
  desenhos para soltar locks exigiriam testes próprios. CPU restrita pode
  levar arquivos legítimos grandes ao timeout: revisar política em homologação.
- Medições descritivas, cache/host concorrente e imports diferentes impedem
  afirmar ganho causal de memória ou desempenho universal.
- Cache compartilhado fora do ETL e integridade geral permanecem SEC-007;
  não se expandiu o escopo para corrigir toda a camada de armazenamento.

## Q. Resultado

Implementação e validação local concluídas; publicação e CI remota ainda
pendentes neste snapshot. A classificação final exigida será registrada após
confirmar esses eventos, sem alegar correção implantada em produção.

## R. Resumo para ChatGPT

**RESUMO PARA CHATGPT: NEXO-SEC-005**

- Base `ff148796d9b579418b3782d472eb44fb0025fe6f`; branch
  `security/sec-005-etl-resource-limits`.
- Corrigidas leitura integral CSV/múltiplas abas, expansão sem orçamento,
  uploads/storage sem limite e risco de persistência parcial/substituição.
- Limites finitos de bytes/ZIP/expansão/abas/linhas/colunas/células/campos/registro,
  JSON, prazo/CPU/AS e admissão; valores e tetos na seção C.
- XLSX incremental + pandas somente na aba vencedora; CSV incremental com
  escolha completa e determinística de separador/encoding.
- Cálculo em filho sem banco, prazo com encerramento/coleta, AS384MiB e CPU60s;
  memória do serviço completo permanece escopo separado de homologação.
- KPIs/ABC/período/telemetria/alertas preservados no baseline sintético e nas
  amostras médias; comparativos, medições e limitações nas seções I/J.
- PostgreSQL real privado: integridade, SAVEPOINT, tamanho real e concorrência;
  comando obrigatório no workflow. Arquivos e resultados nas seções J/K/L.
- GitHub/CI: fatos posteriores serão consolidados na entrega e na descrição da
  PR; não realizar merge/auto-merge ou deploy mesmo após aprovação.
- Render/Supabase não acessados; nenhuma nova migração; SEC-004 requer schema
  previamente aprovado e sua migração em produção ainda precisa ser verificada.
- Próxima etapa: revisão/homologação da PR e preparação autorizada de implantação,
  sem efetuar integração ou produção nesta tarefa.
