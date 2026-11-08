# Avaliação de Cenário (leads)

Situação em 08/11/2026: código, migrações e testes prontos, **sem commit e sem nada aplicado no Supabase**. As migrações só entram com autorização do dono (seção 8).

O consultor envia os relatórios mensais de vendas de um lead, ou seja, de quem ainda não é cliente. O motor do Raio-X roda no processo isolado do ETL, numa passada única. O servidor grava a matriz produto × ano, as marcas, a triagem (Urgência, Atenção e Eletiva) e o resumo do lead.

Os arquivos não são guardados. As linhas de PDV do lead expiram em 30 dias. O resumo e o contato ficam.

Base: Fases 2 a 4 da diretriz de 11/2026 e `Docs/DIAGNOSTICO_MOTOR_ETL.md`. As tabelas, o RLS e a função de expurgo vêm da revisão `f4c2a8e1d9b6` (`Docs/BAIA_DE_SEGURANCA_E_LEADS.md`, seção 2).

## 1. Fluxo

| Passo | Onde | O que faz |
|---|---|---|
| 1 | `blueprints/leads.py`, `POST /admin/leads` | Só consultor (`admin_required`) e com CSRF. Antes de abrir arquivo, confere o contato (nome, loja, e-mail válido, WhatsApp opcional) e a confirmação de que o lead autorizou o uso dos relatórios. |
| 2 | `etl_uploads.vaga_de_entrada` | Pega a mesma vaga única das análises e do Raio-X. Se estiver ocupada, responde **503 com `Retry-After: 30`** e a orientação de enviar de novo, sem ler arquivo. |
| 3 | `_avaliar` | Encerra a transação da requisição (`db.session.rollback()`) antes do processo isolado, que leva até 2 min. Assim, nada fica travado e nenhuma conexão do pooler fica presa. |
| 4 | `avaliacao_cenario.processar` | Chama `raio_x_servico.processar_envio(..., executar=etl_runtime.run_avaliacao_cenario, origem="lead")`. Reaproveita a Baia de Segurança, o teto de 36 arquivos, a assinatura, a recusa de arquivo repetido e o diretório temporário do Raio-X. |
| 5 | `etl_runtime.run_avaliacao_cenario` | Abre o processo isolado com prazo de `min(120, 2 × configurado)` segundos e memória (`RLIMIT_AS`) de `min(384, configurada)` MiB. Isso vale mesmo que `ETL_MEMORY_MB` suba a das outras operações. O retorno inteiro passa por `validar_avaliacao` (detalhes abaixo). |
| 6 | `etl_worker.py`, operação `avaliacao_cenario` | Recusa mais de `MAX_ARQUIVOS` arquivos, roda `validate_input` em cada um e chama `avaliacao_motor.avaliar_cenario`. |
| 7 | `avaliacao_motor.avaliar_cenario` | Usa `raio_x._ler_envio`, que lê cada arquivo uma vez só com `read_raw` e `_vendas_do_bruto`. Aplica as regras do lead (seção 2), monta a matriz, confere os tetos e roda `raio_x._combinar`. Antes de devolver, confere o tamanho. |
| 8 | `avaliacao_cenario.registrar` | Num SAVEPOINT, quantiza as linhas como o banco as guarda e calcula marcas e triagem. Grava o lead e as linhas com **um único `criado_em`**: o expurgo leva o lote inteiro junto. Grava também o plano recomendado (`raio_x.recomendar`, com o histórico do PRATA). O commit é feito pela rota. |
| 9 | `GET /admin/leads/<id>` | Mostra o resumo, os indicadores e até 30 itens por bloco com os motivos, mais a data em que os dados de PDV expiram. |

Se o processo isolado falhar, nada é gravado, porque um lead sem linhas nunca expiraria. Mensagens do motor (`ETLInputError`) voltam ao consultor como estão. Erros de banco viram uma mensagem genérica, e o log guarda só o tipo do erro.

**Conferência do retorno** (`etl_runtime.validar_avaliacao`). Um processo isolado com defeito, ou comprometido, não grava texto livre nem derruba a gravação com erro 500. A conferência cobre:
- **Estrutura:** as chaves e a versão exatas (`VERSAO_REGRAS_LEAD`).
- **Meta:** as chaves fixas de `avaliacao_motor.CHAVES_META`, só com contagens, versões e valores agregados (sem texto livre):
  - anos de 4 dígitos;
  - relatórios por ano somando o total;
  - dias entre 1 e 366;
  - marca de mensal booleana;
  - qualidade e situação só com as chaves conhecidas.
- **Raio-X:** os campos que a gravação e a recomendação do plano leem (período em data ISO, índice de 0 a 100, faturamento, custo e recuperável).
- **Linhas:**
  - ano dentro do envio;
  - chave do produto com 16 hexadecimais;
  - descrição com texto além de espaços;
  - código com até 60 caracteres;
  - somas finitas e não negativas;
  - uma linha por produto e ano.

Se mesmo assim algo não fechar na gravação, o resultado é recusado como "Resultado inválido do processamento isolado." (422), e nada é gravado.

Outras rotas, todas `admin_required`:

| Rota | Faz |
|---|---|
| `POST /admin/leads/<id>/reenviar` | Exige de novo a autorização do lead. Troca as linhas pelas do novo envio, com o lead travado. Recusa lead já convertido em cliente. |
| `POST /admin/leads/<id>/retriar` | Refaz marcas e triagem a partir da staging, sem os arquivos (seção 4). |
| `flask leads retriar <id>` | O mesmo pela linha de comando. A saída mostra só contagens. |

Limite: 20 envios por hora por consultor, somando novo envio e reenvio (`limite_envio`).

**Envio grande demais.** Acima do `MAX_CONTENT_LENGTH`, o envio e o reenvio respondem 413 com a página do portal, em português: "Os relatórios enviados passam de N MB somados. Nada foi gravado: envie menos meses por vez." Nada é lido nem gravado.

**Lista.** Mostra as 100 avaliações mais recentes, em ordem da coluna "Avaliado em": o instante da autorização do lead (`consentimento_em`, renovado a cada reenvio) ou, sem ele, a criação do lead. Um lead antigo reavaliado volta para o topo. Quando há mais avaliações, a lista avisa: "Mostrando as 100 avaliações mais recentes de N".

Banco sem as migrações: a lista mostra o aviso de migrações pendentes, e as demais rotas redirecionam com o mesmo aviso, sem ler arquivo nem consultar as tabelas de lead. A checagem é por engine. A presença fica gravada; a ausência é reavaliada a cada 30 s, então a migração aplicada passa a valer sem reiniciar o processo.

## 2. Regras do motor do lead (`avaliacao_motor.py`)

Versão: `VERSAO_REGRAS_LEAD = "lead-2026-11.2"`, sobre as regras do Raio-X `raio-x-2026-11.4`. A 11.2 acrescentou três coisas: a cobertura de cada ano, as marcas por produto e ano, e a descrição sem espaços. O motor não importa banco, Flask nem configuração; há teste que garante isso.

- **Ano de exercício.** Cada relatório precisa do período no cabeçalho ("Período: dd/mm/aaaa a dd/mm/aaaa"), dentro de um mesmo ano civil, entre 2000 e o ano corrente, e começando até hoje. A recusa traz o número do arquivo:
  - `avaliacao_sem_periodo`: o relatório não tem período;
  - `avaliacao_periodo_entre_anos`: o período cruza a virada do ano;
  - `avaliacao_ano`: o ano está fora da faixa ("99" no cabeçalho vira 2099);
  - `avaliacao_periodo_futuro`: o período começa depois de hoje.
- **Sobreposição.** Dois relatórios com um dia em comum que seja são recusados (`avaliacao_sobreposicao`), e também o período dentro de outro. Períodos vizinhos ("01/01 a 31/01" e "01/02 a 28/02") passam. No Raio-X do cliente a sobreposição é só um aviso. Aqui, a matriz somaria os mesmos dias duas vezes.
- **Chave do produto.** `chave_produto` é o sha256 do nome normalizado do ETL (sem acento, pontuação nem caixa), com 16 hexadecimais. É estável entre anos e entre grafias com e sem acento.
- **Matriz produto × ano.** Tem uma linha por produto e ano, com 17 colunas (`avaliacao_motor.COLUNAS`):
  - `ano_exercicio`, `chave_produto`, `codigo_produto` e `descricao_produto`, com o nome e o código do mês mais recente. Nome só com espaços vira a chave normalizada do produto ("CAFE"), porque a coluna exige texto;
  - `quantidade`, `valor_venda_bruto`, `quantidade_com_custo`, `faturamento_com_custo`, `custo_total`, `perda`, `vazamento_preco` e `recuperavel`;
  - `linhas` e `meses`. `meses` conta os relatórios com venda no ano: são meses quando os relatórios são mensais;
  - as marcas `variacao`, `conflito_cadastral` e `estoque_negativo`. As duas últimas valem para o produto **no ano**, como no Raio-X: o arquivo em que o conflito aparece pode ter só bonificação do produto. Quando o produto não teve venda no ano em que a marca apareceu (só bonificação ou devolução), a marca vai para as linhas dele nos outros anos: o Raio-X do mesmo envio conta o produto, e a triagem também tem de contar.
- **Cobertura de cada ano** (meta). Para cada ano, a meta guarda:
  - `arquivos_por_ano`: os relatórios do ano;
  - `dias_por_ano`: os dias cobertos pelos períodos, **contados até hoje**. O mês corrente exportado com o mês inteiro no cabeçalho ("01/11 a 30/11", lido em 08/11) cobre só os 8 dias que já passaram;
  - `relatorios_mensais_por_ano`: se cada relatório do ano cabe num mesmo mês.

  É a base do ritmo de venda da triagem.
- **Mesmas regras por linha do Raio-X.** Leitura, limpeza, custo unitário ou total, perda abaixo do custo, vazamento de preço e recuperável são os do Raio-X, sem cópia. `EquivalenciaTests` confere que a staging somada reproduz o Raio-X.
- **Tetos** (`avaliacao_valor`). Um valor que a coluna do banco não comporta é recusado com mensagem, em vez de derrubar a gravação:

  | Valores | Teto |
  |---|---|
  | Quantidades | menos de 10¹¹ |
  | Dinheiro | menos de 10¹² |
  | Custo médio e vazamento estimado (derivados no processo pai) | menos de 10¹⁰ |
  | Vazamento total do lead | menos de 10¹⁰ |

  O SQLite não impõe `NUMERIC(p, s)`, por isso a conferência é feita no código.
- **Saída.** O JSON é colunar: `{"colunas": [...], "valores": [[...], ...]}`. Fica abaixo do teto rígido de 4 MiB do worker, com 4 KiB de folga para o envelope. Acima disso, a recusa `avaliacao_grande` explica o motivo. O worker trocaria o resultado grande por um erro genérico. Com cerca de 168 bytes por linha, cabem perto de 24.500 linhas produto × ano.
- **Meta.** Guarda só contagens, versões e valores agregados, sem nome de produto nem texto livre: as versões das regras do lead e do Raio-X, arquivos por ano, a cobertura, arquivos com custo, linhas lidas, qualidade dos valores, situação (quantidade de linhas e valor em R$ dos cancelamentos e das devoluções) e células recodificadas. Ao gravar, o processo pai acrescenta em `meta_json` os motivos da recomendação do plano (`motivos_recomendacao`): as frases de regra fixa de `raio_x.recomendar`, com contagens e valores, sem nome de produto.

## 3. Staging e lead (revisão `5e1b9c3a7d20`)

Expand nas duas tabelas da `f4c2a8e1d9b6`. Nenhuma outra tabela é tocada e nenhuma linha é reescrita. O RLS e as políticas da `f4c2a8e1d9b6` valem para a tabela inteira, então as colunas novas já nascem protegidas.

**`brainstorming_dados_pdv`**

| Coluna | Tipo | Observação |
|---|---|---|
| `chave_produto` | `VARCHAR(16)` NOT NULL | sem padrão: a tabela precisa estar vazia, e nenhum código gravava nela antes |
| `quantidade_com_custo` | `NUMERIC(14,3)` | padrão 0 |
| `faturamento_com_custo`, `custo_total`, `perda`, `vazamento_preco`, `recuperavel` | `NUMERIC(14,2)` | padrão 0 |
| `linhas`, `meses` | `INTEGER` | padrão 0 |
| `variacao`, `conflito_cadastral`, `estoque_negativo` | `BOOLEAN` | padrão `false` |
| `bloco_triagem` | `VARCHAR(10)` | `urgencia`, `atencao`, `eletiva` ou nulo |
| `versao_regras` | `VARCHAR(40)` | — |

Restrições:
- `ck_brainstorming_dados_pdv_chave`: chave não vazia;
- `ck_brainstorming_dados_pdv_somas`: somas não negativas;
- `ck_brainstorming_dados_pdv_bloco`: bloco válido;
- `uq_brainstorming_dados_pdv_lead_ano_produto`: unicidade de lead, ano e produto, para que um reprocessamento não duplique linhas.

**`lead_avaliacao`** ganha estas colunas, todas anuláveis:

| Coluna | Guarda |
|---|---|
| `versao_regras`, `versao_triagem` | versões das regras e da triagem |
| `periodo_inicio`, `periodo_fim` | período coberto |
| `arquivos` | número de relatórios |
| `meta_json` | metadados do envio: contagens, versões e valores agregados, sem nome de produto |
| `itens_venda_sem_custo`, `itens_venda_abaixo_custo` | contagens das marcas por produto |
| `registrado_por` | FK para `usuario`, `ON DELETE SET NULL`, com índice `ix_lead_avaliacao_registrado_por` |
| `consentimento_em` | quando o consultor confirmou a autorização do lead |

Os CHECKs são `ck_lead_avaliacao_arquivos`, `ck_lead_avaliacao_itens` e `ck_lead_avaliacao_periodo`.

O downgrade trava as duas tabelas e recusa se houver qualquer lead ou linha. A mensagem é "Downgrade da Avaliação de Cenário recusado...". `models.py` (`LeadAvaliacao` e `BrainstormingDadosPdv`) espelha a migração, e um teste compara os dois.

Gravação (`avaliacao_cenario.registrar`):
- dinheiro com 2 casas e quantidade com 3, em `Decimal` (`quantizar`). Marcas e triagem saem sempre do valor gravado, então refazê-las dá o mesmo resultado;
- no reenvio, o lead é travado (`FOR UPDATE`) antes das linhas, na mesma ordem do expurgo.

## 4. Triagem e marcas (`triagem_cenario.py`)

Função pura no processo pai, sobre as linhas da staging e a cobertura do envio (`Cobertura.do_envio(meta)`). Versão: `VERSAO_TRIAGEM = "triagem-2026-11.2"`.

**Ritmo de venda e giro.** Os anos podem ter durações diferentes: um ano inteiro e o semestre corrente, ou um relatório anual e outros mensais. Por isso, a triagem usa as seguintes medidas:
- **Ritmo.** É a venda média **por dia coberto** pelos períodos dos relatórios, e não por relatório.
- **Queda.** Compara o último ano com o ano anterior mais recente com venda. Só vale quando o ritmo anterior prevê ao menos `VENDA_ESPERADA_MINIMA` (1 unidade) no período do último ano, porque não vender num mês curto não diz nada de um item de giro lento.
- **Giro.** É a parte dos relatórios do último ano com venda. Só existe quando todos os relatórios do último ano são mensais.

**Marcas por produto e ano** (`marcas()`), gravadas em cada linha:

| Marca | Regra |
|---|---|
| `flag_venda_sem_custo` | parte do faturamento do ano saiu sem custo informado no PDV: `valor_venda_bruto > faturamento_com_custo` |
| `flag_venda_abaixo_custo` | vendido abaixo do custo médio registrado: há quantidade com custo e `faturamento_com_custo < custo_total` |
| `flag_omissao_erp` | **sem regra, fica `false`** (pendência do dono) |
| `vazamento_estimado_r$` | o recuperável do produto no ano, o mesmo do Raio-X |
| `custo_medio_registrado` | `custo_total / quantidade_com_custo`, com 4 casas; nulo sem custo |

O lead recebe a soma por produto: `itens_venda_sem_custo` e `itens_venda_abaixo_custo` contam produtos que tiveram a marca em algum ano.

**Blocos por produto** (`triar()`), olhando todos os anos. Cada produto fica em no máximo um bloco, nesta precedência:

1. **Urgência.** Valor recuperável alto e travado, **e** curva de estagnação crítica:
   - recuperável alto: o produto está entre os que somam os primeiros 80% do recuperável do lead, e o recuperável dele é ao menos 3% do próprio faturamento;
   - estagnação crítica, qualquer uma destas:
     - o ritmo de venda caiu 50% ou mais em relação ao ano anterior com venda;
     - o produto ficou sem venda no período do último ano, quando o ritmo anterior previa venda;
     - o produto vendeu em até 25% dos relatórios mensais do último ano, quando esse ano tem ao menos 4.
2. **Atenção.** Margem degradada ou divergência de custo:
   - margem degradada, qualquer uma destas:
     - margem abaixo de 10%;
     - queda de 5 pontos ou mais entre os dois últimos anos com custo;
     - vazamento de preço de ao menos 3% do faturamento;
   - divergência de custo, qualquer uma destas:
     - venda sem custo;
     - venda abaixo do custo;
     - perda abaixo do custo;
     - conflito de cadastro entre código e nome.
3. **Eletiva.** Otimização secundária de giro, qualquer uma destas:
   - ritmo de venda em queda de 20% ou mais;
   - venda em até metade dos relatórios mensais do último ano;
   - estoque negativo no PDV;
   - recuperável residual;
   - estagnação crítica sem recuperável alto: o produto que parou de vender no último ano (quando o ritmo anterior previa venda) e não tem nenhum dos motivos acima entra com o motivo "Sem venda no período do último ano". Com algum deles (o recuperável residual, por exemplo), entra com esses motivos.

**Fronteiras exatas.** Ritmo, queda, giro e margem são calculados em fração exata (`fractions.Fraction`), e não em ponto flutuante. Cada limiar vale exatamente no valor da constante:
- a queda de exatamente 50% entre um fevereiro (28 dias) e um abril (30 dias) é queda forte; em ponto flutuante a conta dá 0,4999…;
- a margem de exatamente 10% não é baixa, e a queda de exatamente 5 pontos conta;
- a venda esperada de exatamente 1 unidade conta.

Os limiares são constantes no topo do módulo, e o dono deve ajustá-los, porque a especificação não trazia números:

| Constante | Valor | Uso |
|---|---|---|
| `PARETO_RECUPERAVEL` | 0,80 | produtos que somam 80% do recuperável |
| `RECUPERAVEL_MINIMO_DO_PRODUTO` | 0,03 | recuperável ≥ 3% do faturamento do produto |
| `QUEDA_CRITICA` | 0,50 | queda do ritmo de venda (Urgência) |
| `GIRO_CRITICO` / `RELATORIOS_PARA_GIRO_CRITICO` | 0,25 / 4 | vendeu em ≤ 25% dos relatórios mensais, com ao menos 4 |
| `MARGEM_BAIXA` | 0,10 | margem abaixo de 10% |
| `QUEDA_DE_MARGEM` | 0,05 | queda de 5 pontos |
| `VAZAMENTO_RELEVANTE` | 0,03 | vazamento ≥ 3% do faturamento |
| `QUEDA_MODERADA` | 0,20 | queda do ritmo de venda (Eletiva) |
| `GIRO_BAIXO` | 0,50 | vendeu em ≤ metade dos relatórios mensais |
| `VENDA_ESPERADA_MINIMA` | 1 | unidades que o ritmo anterior precisa prever no último ano para a queda valer |

Os resumos dos blocos (`resumo_urgencia_json`, `resumo_atencao_json` e `resumo_eletiva_json`) guardam a versão, o número de itens, o faturamento, o recuperável e a contagem de motivos. **Nunca guardam nome de produto**, porque ficam no lead depois do expurgo das linhas.

**Retriagem.** Para mudar um limiar e aplicá-lo a leads antigos, troque a constante e a `VERSAO_TRIAGEM`. Depois use "Refazer a triagem" na tela ou `flask leads retriar <id>`. A retriagem:
- só funciona enquanto as linhas existirem (30 dias);
- recusa o lead expirado, o lead sem linhas, as linhas gravadas por outra `VERSAO_REGRAS_LEAD` e o lead sem a cobertura do envio na meta;
- grava em lote, pela chave primária.

**Detalhe do lead.** O bloco de cada produto é o gravado. Os motivos de cada item são a triagem refeita sobre as linhas, sem gravar nada, e só aparecem quando a triagem e as regras do lead são as da versão atual. Senão, a tela avisa "Triagem feita com regras anteriores", mostra a versão atual e a do lead, e mostra só os blocos e o resumo gravados. JSON gravado que não seja um objeto (resumo ou meta) é ignorado, sem erro 500.

## 5. Retenção e expurgo (revisão `6a8f2d4c0e13`)

- **Com pg_cron.** Onde a extensão estiver habilitada (no Supabase: Database › Extensions › pg_cron), a migração agenda o job `nexo_expurgo_leads_pdv` para todo dia às 06:15 UTC (03:15 em Brasília). O job roda `SELECT * FROM <esquema>.expurgar_brainstorming_pdv(30)`, a mesma função do comando `flask leads expurgar-pdv --aplicar`. Ele roda dentro do banco: não depende do servidor web, que dorme no plano gratuito do Render, nem de cron pago.
- **Sem pg_cron.** No PostgreSQL sem a extensão, a revisão não agenda nada e emite um `WARNING` ("pg_cron ausente: o expurgo diário dos leads NÃO foi agendado..."). O aviso vai para o log do PostgreSQL e **não aparece na saída do `flask db upgrade`**: a conferência é o `SELECT` em `cron.job` da seção 8, passo 5. No SQLite, a revisão não faz nada e não avisa. Nos dois casos, o expurgo segue pelo comando, que um cron externo pode chamar, e pela própria tela (abaixo).
- **Pela tela.** A lista e o detalhe dos leads rodam a mesma regra ao serem abertos (`blueprints/leads.expurgo_oportunista`), no máximo uma vez por hora por processo. Assim, sem o pg_cron, ou com o servidor dormindo na hora do job, a tela não mostra dado de PDV vencido. Um erro de banco no expurgo vai para o log só com o tipo e nunca derruba a página.
- **Reagendar e downgrade.** Reagendar com o mesmo nome atualiza o job. O downgrade remove o job, se ele existir. Nenhum dado é tocado.
- **O que a função faz.** Ela não mudou e segue na `BAIA_DE_SEGURANCA_E_LEADS.md`, seção 2. Apaga as linhas de PDV com mais de 30 dias de leads não convertidos e marca como `expirado` o lead que ficou sem linhas. Mantém o resumo e o contato.
- **Na tela.** O detalhe do lead mostra quando os dados de PDV expiram: o `criado_em` do lote mais 30 dias.
- **Fora do backup.** O job fica no esquema `cron`, fora do `pg_dump` da aplicação (`Docs/OPERACAO_BACKUP_RESTAURACAO.md`). Depois de uma restauração, reaplique a revisão ou agende de novo.

## 6. Segurança e concorrência

- **Acesso.** Só consultores (`admin_required` em todas as rotas). Tem CSRF em todos os formulários e limite de 20 envios por hora.
- **Arquivos.** Nenhum arquivo do lead é guardado, nem o nome. O processamento acontece no diretório temporário do Raio-X, que some ao fim.
- **Logs.** Erros de banco vão para o log só com o tipo do erro. Os comandos mostram só contagens.
- **Concorrência.** Há duas vagas únicas por processo:
  - a de entrada (`etl_uploads._UPLOAD_ADMISSION`), compartilhada com as análises e o Raio-X;
  - a do processo isolado (`etl_runtime._ADMISSION`).

  Com qualquer uma ocupada, a resposta é 503 com `Retry-After: 30`. Nenhum arquivo é processado e nada é gravado, e o consultor vê a mensagem de enviar de novo. A API do Raio-X do cliente (`/api/onboarding/diagnostico`) segue a mesma regra.
- **Tempo da requisição.** O gunicorn do Render roda com `--timeout 180` (`render.yaml`), acima do prazo de 120 s do processo isolado.
- **Transação.** Nenhuma transação fica aberta durante o processo isolado. A gravação é curta e acontece num SAVEPOINT.
- **Trava de células.** Cada aba tem no máximo 500.000 células (`etl_limits._HARD_CAPS["max_cells_per_sheet"]`), padrão e teto iguais, então nenhuma configuração sobe esse número. Isso vale para CSV e XLSX, antes da leitura completa.
- **Lead convertido.** Não aceita reenvio. O lead expirado não aceita retriagem.
- **Retriagem.** Um valor acima do teto vira aviso na tela, e não erro 500.

## 7. Testes

**SQLite e processo próprio** (rodam neste Windows):
- `tests/test_avaliacao_cenario.py` (68 testes):
  - **`MotorTests`:**
    - ano, sobreposição (um dia em comum, período dentro de outro, vizinhos aceitos), ano corrente e ano seguinte;
    - período futuro recusado e mês corrente contado até hoje;
    - matriz, cobertura de cada ano, marcas por produto e ano, marca de um ano sem venda levada aos outros anos;
    - nome só com espaços e tetos;
  - **`EquivalenciaTests`:** a staging reproduz o Raio-X;
  - **`ContratoTests`:**
    - o formato do retorno, com 27 variantes recusadas, entre elas meta vazia ou com texto livre, chave fora do formato, linha repetida e período ausente;
    - `run_avaliacao_cenario` confere o retorno do processo filho;
    - o motor sem banco nem config;
    - a operação do worker;
    - o prazo de 120 s e a memória de 384 MiB;
  - **`TriagemTests`:** os três blocos, a precedência e as marcas;
  - **`RitmoTests`:** anos de durações diferentes sem queda falsa, queda verdadeira, mês curto sem venda de item lento, giro só com relatórios mensais e cobertura inválida;
  - **`FronteirasDaTriagemTests`:** queda de exatamente 50% (28 e 30 dias), venda esperada de exatamente 1 unidade, margem de exatamente 10%, queda de margem de exatamente 5 pontos, corte de Pareto com empate, giro crítico com recuperável alto e três anos;
  - **`QuantizacaoTests`:** dinheiro com 2 casas e quantidade com 3, arredondando o meio para cima a partir do valor do JSON;
  - **`GravacaoTests`:** gravação, reenvio, recusas, retriagem, comando, resultado malformado sem erro 500, nome só com espaços e teto do custo médio;
  - **`RotasTests`:**
    - só consultor em todas as rotas (sem login vai para o login mesmo com CSRF válido; cliente recebe 403);
    - autorização, reenvio sem autorização, consentimento renovado no reenvio;
    - 503 com `Retry-After` (vaga de entrada e vaga do processo isolado, no envio e no reenvio);
    - 413 em português no envio e no reenvio;
    - expurgo pelo detalhe e pela lista no máximo uma vez por hora, e erro de banco no expurgo sem derrubar a página;
    - lista em ordem da última avaliação;
    - erro do motor, resultado malformado (422), retriagem, triagem de versão anterior, CSRF, 404 e banco sem as migrações;
  - **`TravaDeCelulasTests`:** CSV e XLSX com 500.004 células são recusados.
- `tests/test_leads_avaliacao.py`: esquema, restrições novas, unicidade, índice e downgrade até a revisão que recusa.

**PostgreSQL real** (cluster temporário; precisa de `NEXO_TEST_POSTGRES_BIN`):
- `tests/test_leads_postgresql.py`, com 24 verificações. Inclui o agendamento, a remoção no downgrade, o reagendamento no upgrade e a gravação pelo serviço.
- **Ressalva:** o PostgreSQL local não tem pg_cron. O ensaio usa um esquema `cron` simulado, com `schedule`, `unschedule` e a tabela `job`. Isso prova a migração, mas não o pg_cron real do Supabase.

**Só no Linux:** o worker real, que precisa do módulo `resource`, roda em `ContratoTests.test_worker_real_devolve_o_contrato`. Neste Windows, esse teste é pulado.

**Navegador:** `tests/test_overflow_navegador.py` cobre `/admin/leads` e o detalhe do lead sem estouro horizontal.

## 8. Aplicação (só com autorização)

Ordem: `f4c2a8e1d9b6` → `5e1b9c3a7d20` → `6a8f2d4c0e13`, que é a `head`. Todas vêm depois da `b3e9d6c1f7a2`.

1. **Backup.** Faça o backup do banco e confira que dá para voltar (`Docs/OPERACAO_BACKUP_RESTAURACAO.md`).
   - Antes de publicar o código, confira no painel do Render que `ETL_MAX_CELLS_PER_SHEET`, se existir, vale no máximo 500000. O teto passou a ser rígido, e um valor acima dele (o teto antigo era 1.000.000) impede o app de subir.
2. **pg_cron.** Opcional, mas recomendado: habilite a extensão no Supabase **antes** da `6a8f2d4c0e13`. Se for habilitada depois, reaplique só o agendamento (`flask db downgrade 5e1b9c3a7d20` e `flask db upgrade`). Isso não toca em dado.
3. **Prévia.** Rode `flask db upgrade b3e9d6c1f7a2:6a8f2d4c0e13 --sql`.
4. **Aplicação.** Rode `flask db upgrade`.
5. **Conferência.**
   - Estrutura:
     - as colunas novas e as restrições da seção 3;
     - RLS ligado nas duas tabelas;
     - `SELECT jobname, schedule, username FROM cron.job`, com o pg_cron ativo. O `username` deve ser o papel dono das tabelas (`postgres` no Supabase); com outro papel, o job roda sob o RLS e pode apagar zero linhas sem erro.
   - Funcionamento, com uma avaliação de teste:
     - a tela `/admin/leads` faz a avaliação;
     - o detalhe mostra os blocos.

O código pode ser publicado antes das migrações: a tela mostra o aviso e não lê arquivo. A avaliação roda no worker isolado do ETL e só funciona no Linux do Render.

## 9. Limitações e pendências do dono

- **LGPD do lead.** Faltam definir:
  - a base legal e o aviso de privacidade para quem ainda não é cliente;
  - o texto da autorização que o consultor confirma;
  - o prazo de retenção do próprio `lead_avaliacao`, já que o expurgo só apaga as linhas de PDV;
  - o atendimento a pedido de exclusão: apagar o lead leva as linhas por CASCADE.

  Nada disso foi inventado aqui. A tela registra só o instante da confirmação (`consentimento_em`) e quem a fez (`registrado_por`).
- **Especificação da triagem.** Os limiares da seção 4 são parâmetros iniciais e precisam de validação comercial.
- **`flag_omissao_erp`.** Não tem regra definida e fica falsa. A variação de preço segue na coluna `variacao`.
- **Capacidade.** O diagnóstico mediu:
  - XLSX com 36 meses × 5.000 produtos levou 173 s neste computador, acima do prazo de 120 s;
  - CSV com o mesmo volume levou cerca de 35 s;
  - o plano gratuito do Render tem 0,1 CPU, o que pode deixar o processamento perto de 10 vezes mais lento. Nesse cenário, até 36 × 2.000 em CSV passaria do prazo.

  Por isso, recomende enviar por ano ou usar CSV. Medir no Render antes de usar com leads reais. A revisão independente acrescentou três medidas:
  - o detalhe do lead refaz a triagem a cada visita, e com 24,6 mil linhas isso leva 0,33 s em CPU cheia;
  - a gravação usa cerca de 90 MB de memória transitória para 24,6 mil linhas;
  - o `validate_input` antes da leitura repete uma passada do XLSX dentro dos 120 s.
- **Captação pública.** A rota pública ficou de fora. A fase 1 é só para o consultor, e a abertura depende da LGPD, de anti-abuso (captcha e limite por IP) e da capacidade.
- **Conversão do lead em cliente.** O status `convertido` existe e bloqueia o reenvio, mas ainda não existe o fluxo que converte o lead.
- **Envio em lotes.** Mandar um ano por vez e juntar os anos num mesmo lead ficou fora. Hoje, cada envio substitui o anterior.
