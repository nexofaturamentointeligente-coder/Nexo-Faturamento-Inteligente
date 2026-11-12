# Avaliação de Cenário (leads)

Situação em 09/11/2026: a Etapa 1 (saneamento, worker, triagem e infraestrutura) está no commit `53020e6`, a Etapa 2 (triagem com limiares configuráveis, marcas de omissão no ERP, matriz por categoria e diagnóstico em JSON) no `227f495`, a Etapa 3 (inteligência de estoque e de preço: estoque parado por idade, excedente, simulador de liberação de caixa e auditoria de preço) no `1085cc0` e a Etapa 4 (o diagnóstico no portal do consultor: resumo executivo do caixa resgatável, matriz de triagem, simulador na tela, séries para gráficos, cache do diagnóstico e o isolamento entre papéis, empresas e leads) no commit seguinte, no ramo `avaliacao-cenario-etapas-2-a-4`. **Nada foi aplicado no Supabase**: as migrações só entram com autorização do dono (seção 9).

O consultor envia os relatórios mensais de vendas de um lead, ou seja, de quem ainda não é cliente. O motor do Raio-X roda no processo isolado do ETL, numa passada única. O servidor grava a matriz produto × ano, as marcas, a triagem (Urgência, Atenção e Eletiva) e o resumo do lead. A tela do consultor e a rota JSON mostram a matriz por categoria e os itens de cada bloco, com motivo e impacto.

Os arquivos não são guardados. As linhas de PDV do lead expiram em 30 dias. O resumo e o contato ficam.

Base: Fases 2 a 4 da diretriz de 11/2026, a diretriz da Etapa 2 (evolução analítica) e `Docs/DIAGNOSTICO_MOTOR_ETL.md`. As tabelas, o RLS e a função de expurgo vêm da revisão `f4c2a8e1d9b6` (`Docs/BAIA_DE_SEGURANCA_E_LEADS.md`, seção 2).

## 1. Fluxo

| Passo | Onde | O que faz |
|---|---|---|
| 1 | `blueprints/leads.py`, `POST /admin/leads` | Só consultor (`admin_required`) e com CSRF. Antes de abrir arquivo, confere o contato (nome, loja, e-mail válido, WhatsApp opcional) e a confirmação de que o lead autorizou o uso dos relatórios. |
| 2 | `etl_uploads.vaga_de_entrada` | Pega a mesma vaga única das análises e do Raio-X. Se estiver ocupada, responde **503 com `Retry-After: 30`** e a orientação de enviar de novo, sem ler arquivo. |
| 3 | `_avaliar` | Encerra a transação da requisição (`db.session.rollback()`) antes do processo isolado, que leva até 2 min. Assim, nada fica travado e nenhuma conexão do pooler fica presa. |
| 4 | `avaliacao_cenario.processar` | Chama `raio_x_servico.processar_envio(..., executar=etl_runtime.run_avaliacao_cenario, origem="lead")`. Reaproveita a Baia de Segurança (na leitura direta, que aceita também `.xls` e Planilha XML 2003), o teto de 36 arquivos, a assinatura, a recusa de arquivo repetido e o diretório temporário do Raio-X. |
| 5 | `etl_runtime.run_avaliacao_cenario` | Abre o processo isolado com prazo de `min(120, 2 × configurado)` segundos e memória (`RLIMIT_AS`) de `min(384, configurada)` MiB. Isso vale mesmo que `ETL_MEMORY_MB` suba a das outras operações. O retorno inteiro passa por `validar_avaliacao` (detalhes abaixo). |
| 6 | `etl_worker.py`, operação `avaliacao_cenario` | Recusa mais de `MAX_ARQUIVOS` arquivos, roda `validate_input` em cada um e chama `avaliacao_motor.avaliar_cenario`. |
| 7 | `avaliacao_motor.avaliar_cenario` | Usa `raio_x._ler_envio(..., cadastro=True)`, que lê cada arquivo uma vez só com `read_raw` e `_vendas_do_bruto` e, só aqui, também NCM, categoria e estoque (seção 2). O arquivo que traz a data de cada venda e cobre mais de um mês vira um relatório por mês, em memória (`Docs/UNIFICACAO_MOTOR_ETL.md`, seção 7). Aplica as regras do lead, monta a matriz, confere os tetos e roda `raio_x._combinar`. Antes de devolver, confere o tamanho. |
| 8 | `avaliacao_cenario.registrar` | Num SAVEPOINT, quantiza as linhas como o banco as guarda e calcula marcas e triagem com os limiares (os padrão, ou os já gravados no lead no reenvio). Confere os tetos das somas arredondadas. Grava o lead e as linhas com **um único `criado_em`**: o expurgo leva o lote inteiro junto. Grava também o plano recomendado (`raio_x.recomendar`, com o histórico do PRATA) e os limiares usados. O commit é feito pela rota. |
| 9 | `GET /admin/leads/<id>` | Mostra o resumo, os indicadores (inclusive as três marcas), o **resumo executivo do caixa resgatável**, a **matriz de triagem** (Urgência, Atenção e Eletiva com produtos, faturamento, potencial recuperável e os motivos mais frequentes), a matriz por categoria, o estoque parado e excedente, o **simulador** (com os parâmetros da consulta, seção 5), a auditoria de preço e até 30 itens por bloco, com motivos e impacto, mais a data em que os dados de PDV expiram. |

Se o processo isolado falhar, nada é gravado, porque um lead sem linhas nunca expiraria. Mensagens do motor (`ETLInputError`) voltam ao consultor como estão. Erros de banco viram uma mensagem genérica, e o log guarda só o tipo do erro.

**Conferência do retorno** (`etl_runtime.validar_avaliacao`). Um processo isolado com defeito, ou comprometido, não grava texto livre nem derruba a gravação com erro 500: uma exceção inesperada na própria conferência (um inteiro de 400 dígitos, um tipo trocado) também vira recusa. A conferência cobre:
- **Estrutura:** as chaves e a versão exatas (`VERSAO_REGRAS_LEAD`).
- **Meta:** as chaves fixas de `avaliacao_motor.CHAVES_META`, só com contagens, versões e valores agregados (sem texto livre):
  - anos de 4 dígitos;
  - relatórios por ano somando o total;
  - dias entre 1 e 366, e os dias dos mesmos meses até os dias do ano;
  - marca de mensal booleana;
  - relatórios com custo, NCM, categoria, estoque e completos, por ano, de 0 até os relatórios do ano;
  - relatórios com cabeçalho ambíguo, de 0 até o total;
  - ajustes de produto sem venda por ano com exatamente as três somas, não negativas e abaixo de 10¹² (o teto do dinheiro);
  - qualidade e situação só com as chaves conhecidas, e o dinheiro da situação abaixo de 10¹².
- **Raio-X:** os campos que a gravação e a recomendação do plano leem (período em data ISO com início até o fim, índice de 0 a 100, faturamento, custo e recuperável).
- **Linhas:** exatamente `colunas`, `valores` e `categorias`.
  - categorias únicas, cada uma com 1 a 80 caracteres imprimíveis e algo além de espaços;
  - ano dentro do envio;
  - chave do produto com 16 hexadecimais;
  - descrição com texto além de espaços, e código com até 60 caracteres, os dois só com caracteres imprimíveis (sem NUL, controle nem surrogate solto, que o PostgreSQL recusa fora do tratamento de erro);
  - somas finitas, não negativas e abaixo do teto da coluna do banco (inteiro só até 2⁵³); estoque finito e com módulo abaixo do teto (pode ser negativo);
  - contagens inteiras até 2³¹ − 1 (o INTEGER do banco), marcas booleanas, índice de categoria dentro da lista;
  - coerência dentro da linha: 1 ≤ relatórios com venda ≤ linhas; relatórios com venda ≤ relatórios desde a primeira venda ≤ relatórios do ano; relatórios negativos ≤ relatórios com custo ≤ relatórios com venda; relatórios completos ≤ relatórios com venda; "último relatório negativo" só com relatório negativo; venda com custo ≤ venda nos relatórios com a coluna de custo ≤ venda; quantidade nos mesmos meses ≤ quantidade; NCM, categoria, estoque e custo só nos anos em que algum relatório traz a coluna; categoria e "categoria ausente" nunca juntas;
  - uma linha por produto e ano;
  - coluna sem regra de conferência nunca passa.

Se mesmo assim algo não fechar na gravação, o resultado é recusado como "Resultado inválido do processamento isolado." (422), e nada é gravado.

Outras rotas e comandos, todos só para consultores:

| Rota ou comando | Faz |
|---|---|
| `GET /admin/leads/<id>/diagnostico.json` | O diagnóstico do lead em JSON (seção 5). |
| `POST /admin/leads/<id>/reenviar` | Exige de novo a autorização do lead. Troca as linhas pelas do novo envio, com o lead travado, e mantém os limiares gravados. Recusa lead já convertido em cliente. |
| `POST /admin/leads/<id>/retriar` | Refaz marcas e triagem a partir da staging, sem os arquivos, com os limiares gravados no lead (seção 4). |
| `flask leads retriar <id>` | O mesmo pela linha de comando. A saída mostra só contagens. |
| `flask leads retriar <id> --limiares '{"margem_baixa": "0.08"}'` | Refaz com limiares trocados (JSON parcial; valores em texto decimal, contagens em inteiro) e grava os novos no lead. |
| `flask leads agendar-expurgo` | Agenda, ou reagenda, o expurgo diário no pg_cron, como a revisão `6a8f2d4c0e13` (seção 6). |

Limite: 20 envios por hora por consultor, somando novo envio e reenvio (`limite_envio`).

**Envio grande demais.** Acima do `MAX_CONTENT_LENGTH`, o envio e o reenvio respondem 413 com a página do portal, em português: "Os relatórios enviados passam de N MB somados. Nada foi gravado: envie menos meses por vez." Nada é lido nem gravado.

**Lista.** Para cada lead: saúde, a **triagem** (produtos em U, A e E, do resumo gravado), as **marcas** (sem custo, abaixo do custo, omissão no ERP) e o **caixa resgatável** (o potencial recuperável e, com estoque informado, o capital parado há 60 dias ou mais, do resumo gravado da inteligência, com "(parcial)" quando algum produto com custo e sem saldo informado já está há 60 dias ou mais sem venda e falta na soma). Mostra as 100 avaliações mais recentes, em ordem da coluna "Avaliado em": o instante da autorização do lead (`consentimento_em`, renovado a cada reenvio) ou, sem ele, a criação do lead. Um lead antigo reavaliado volta para o topo. Quando há mais avaliações, a lista avisa: "Mostrando as 100 avaliações mais recentes de N".

Banco sem as migrações: a lista mostra o aviso de migrações pendentes; as demais telas redirecionam com o mesmo aviso e a rota JSON responde 503 (`{"erro": "esquema_pendente"}`, com `Retry-After`), sem ler arquivo nem consultar as tabelas de lead. A checagem é por engine e olha a coluna `fim_ultimo_relatorio_com_venda`, da revisão `9b3e5d7f1a28` (Etapa 3). A presença fica gravada; a ausência é reavaliada a cada 30 s, então a migração aplicada passa a valer sem reiniciar o processo.

## 2. Regras do motor do lead (`avaliacao_motor.py`)

Versão: `VERSAO_REGRAS_LEAD = "lead-2026-11.4"`, sobre as regras do Raio-X `raio-x-2026-11.5` (a da unificação do motor, `Docs/UNIFICACAO_MOTOR_ETL.md`: formatos `.xls` e Planilha XML, leiaute paginado, fechamento dos centavos, custo a confirmar e fatias mensais pela data da venda; a versão do lead não mudou, porque a matriz e a meta têm as mesmas chaves). A 11.3 acrescentou a leitura do cadastro (NCM, categoria e estoque), os ajustes de bonificação e devolução por produto e as contagens de que a triagem da Etapa 2 precisa. A 11.4 (Etapa 3) acrescentou a data da última venda de cada produto no ano, os períodos cobertos do envio e a leitura do relatório com várias lojas. O motor não importa banco, Flask nem configuração; há teste que garante isso.

- **Ano de exercício.** Cada relatório precisa do período no cabeçalho ("Período: dd/mm/aaaa a dd/mm/aaaa"), dentro de um mesmo ano civil, entre 2000 e o ano corrente, e começando até hoje. A recusa traz o número do arquivo:
  - `avaliacao_sem_periodo`: o relatório não tem período;
  - `avaliacao_periodo_entre_anos`: o período cruza a virada do ano;
  - `avaliacao_ano`: o ano está fora da faixa ("99" no cabeçalho vira 2099);
  - `avaliacao_periodo_futuro`: o período começa depois de hoje.
- **Sobreposição.** Dois relatórios com um dia em comum que seja são recusados (`avaliacao_sobreposicao`), e também o período dentro de outro. Períodos vizinhos ("01/01 a 31/01" e "01/02 a 28/02") passam. No Raio-X do cliente a sobreposição é só um aviso. Aqui, a matriz somaria os mesmos dias duas vezes.
- **Chave do produto.** `chave_produto` é o sha256 do nome normalizado do ETL (sem acento, pontuação nem caixa), com 16 hexadecimais. É estável entre anos e entre grafias com e sem acento.
- **Leitura do cadastro** (`etl_processor._vendas_do_bruto(..., cadastro=True)`). Só o lead pede; o Raio-X do cliente e a análise mensal leem exatamente como antes (golden `tests/test_cadastro_regression.py`, capturado do `53020e6`). Nada entra no dicionário de colunas de vendas.
  - **NCM:** a coluna cujo título fala em NCM. Ficam só os dígitos ("0901.21.00" vale 09012100); o "4012010.0" que o XLSX entrega como número perde o ".0" e ganha o zero à esquerda; só zeros ou outro tamanho valem como ausente.
  - **Categoria:** a coluna de nível mais alto entre categoria, departamento (depto), seção, grupo, família e subgrupo. Título com código, NCM, termos fiscais (tributário, ICMS, IPI, PIS, COFINS, CST, CFOP), preço, cliente, fornecedor, pagamento, situação ou status não conta. Marcadores de categoria não configurada ("SEM GRUPO", "Geral", "Padrão", "Não definido", "999", célula vazia) valem como ausente; "Diversos" e "Outros" ficam como categoria. Até 80 caracteres.
  - **Estoque:** o último valor informado do produto no ano, lido de qualquer linha dele no relatório, inclusive com quantidade 0 (o relatório "vendas x estoque" lista o item parado com o saldo); pode ser negativo. Só entra quando o produto vendeu no ano: o item que aparece num ano só com estoque, sem venda, não ganha linha (limitação, seção 10).
  - **Loja ou filial (11.4):** a coluna fora do dicionário cujo título fala em loja, filial, estabelecimento ou unidade de negócio. "Unidade" sozinha fica de fora (no PDV é a unidade de medida: UN, KG, CX), e também o título de número da loja ("Estoque Loja", "Margem Loja", "Meta Filial": preço, valor, custo, quantidade, estoque, saldo, total, margem, venda, desconto, meta, data) e o de categoria ("Departamento Loja" segue como categoria). Com duas lojas ou mais com nome nas linhas com saldo do arquivo ("Centro" e "CENTRO " são a mesma; o código 1 e o 1.0 do XLSX também; decidido no arquivo inteiro, antes das fatias mensais, e o depósito que só traz o saldo, com quantidade 0, também conta), o estoque do produto é a **soma do último saldo de cada loja**, acumulado nas fatias (o da última linha seria o de uma loja só). Com uma loja só, a linha com a célula da loja em branco (mesclada, ou preenchida só na primeira linha do grupo ou do cupom) é dessa loja, e vale o último saldo. A meta conta em `relatorios_varias_unidades` os relatórios (ou fatias mensais) com duas lojas ou mais nas vendas: o estoque pode vir somado com a contagem em 0 (o depósito, ou cada loja vendendo num mês). A matriz não separa as lojas (seção 10).
  - **Coluna de loja no lugar do produto (11.4):** quando a coluna que o dicionário leu como código ou descrição do produto é de loja ("Cód. Loja" ou "Descrição Loja" antes das colunas do produto), os "produtos" seriam as lojas: o envio é recusado (`avaliacao_cabecalho_loja`), com a orientação de exportar as colunas de loja depois das do produto, ou sem elas. Título com "produto", "mercadoria" ou "item" ("Produto Loja") segue como produto, mas o relatório conta como cabeçalho ambíguo.
  - **Cabeçalho ambíguo:** quando o título que o Raio-X leu como código ou nome do produto tem palavra de cadastro ("Cód. NCM" antes de "Código", "Grupo do Produto" antes de "Descrição"; na 11.4, também loja, filial, estabelecimento, empresa e CNPJ), NCM e categoria desse relatório não são avaliados, e o conflito de cadastro não é avaliado **no envio inteiro** (a linha da staging soma o produto no ano e não sabe de que relatório veio o conflito). A meta conta o relatório em `relatorios_cabecalho_ambiguo`.
- **Matriz produto × ano.** Tem uma linha por produto e ano, com 32 colunas (`avaliacao_motor.COLUNAS`):
  - `ano_exercicio`, `chave_produto`, `codigo_produto` e `descricao_produto`, com o nome e o código do mês mais recente, limpos de caractere de controle (o espaço especial vira espaço; o código fica com até 60 caracteres). Nome só com espaços vira a chave normalizada do produto ("CAFE"), porque a coluna exige texto;
  - `quantidade`, `valor_venda_bruto`, `quantidade_com_custo`, `faturamento_com_custo`, `custo_total`, `perda`, `vazamento_preco` e `recuperavel`;
  - `linhas` e `meses`. `meses` conta os relatórios com venda no ano: são meses quando os relatórios são mensais;
  - as marcas `variacao`, `conflito_cadastral` e `estoque_negativo`. As duas últimas valem para o produto **no ano**, como no Raio-X. Quando o produto não teve venda no ano em que a marca apareceu (só bonificação ou devolução), a marca vai para as linhas dele nos outros anos;
  - **novas na 11.3:**

    | Coluna | O que é |
    |---|---|
    | `faturamento_em_relatorio_com_custo` | venda do produto nos relatórios que trazem a coluna de custo |
    | `meses_com_custo` | relatórios com venda com custo |
    | `meses_margem_negativa`, `ultimo_mes_negativo` | relatórios com venda abaixo do custo (comparação em centavos) e se o mais recente com custo foi negativo |
    | `meses_completos` | relatórios com venda cujo período já terminou (fim antes de hoje), seja qual for a duração |
    | `quantidade_mesmos_meses` | quantidade nos relatórios da janela dos mesmos meses (abaixo); 0 fora dela |
    | `relatorios_desde_primeira_venda` | relatórios do ano desde a primeira venda do produto no ano (a base do giro de um lançamento) |
    | `estoque_final` | o último estoque informado (de qualquer linha do produto); nulo sem a coluna |
    | `categoria` | índice na lista `linhas["categorias"]`; nulo sem categoria |
    | `ncm_ausente`, `categoria_ausente` | o relatório traz a coluna e o produto veio sem o dado |
    | `custo_bonificacoes`, `custo_devolucoes`, `devolucoes_com_custo` | custo das bonificações, custo e receita das devoluções com custo, pelas mesmas máscaras da auditoria do Raio-X |
  - **nova na 11.4:** `fim_ultimo_relatorio_com_venda`, o fim do último relatório do ano em que o produto vendeu, cortado no dia do envio (o mês corrente exportado inteiro, lido em 09/11, vale 09/11), em data ISO. O relatório em que o produto só aparece com o saldo (quantidade 0) não conta como venda. É a base dos dias sem venda da seção 5.1;
- **Cobertura de cada ano** (meta). Para cada ano, a meta guarda:
  - `arquivos_por_ano`, `dias_por_ano` (contados até hoje: o mês corrente exportado inteiro, lido em 08/11, cobre só 8 dias) e `relatorios_mensais_por_ano`;
  - `relatorios_com_custo_por_ano`, `relatorios_com_ncm_por_ano`, `relatorios_com_categoria_por_ano`, `relatorios_com_estoque_por_ano` e `relatorios_completos_por_ano`;
  - `dias_mesmos_meses_por_ano`: os dias da **janela dos mesmos meses** em cada ano. A janela são os meses do calendário que os dois últimos anos do envio cobrem, só quando os relatórios dos dois são todos mensais, e no último ano só com os relatórios completos (o mês corrente, parcial, fica de fora dos dois lados). Os anos mais antigos e o envio de um ano só ficam com 0. Envio de 12 meses de novembro a outubro não tem mês em comum: janela 0 nos dois anos;
  - `ajustes_sem_venda_por_ano`: o custo de bonificação e de devolução de produto sem venda no ano, sem nome de produto (a matriz não tem linha para ele, e o total da matriz por categoria precisa fechar com o Raio-X);
  - **na 11.4:** `periodos_cobertos`, os períodos dos relatórios em ordem, com o fim cortado em hoje e os vizinhos unidos (`[["2025-09-01", "2025-11-09"]]`; um mês sem relatório separa dois períodos), e `relatorios_varias_unidades`, os relatórios com duas lojas ou mais. Só datas e contagens, sem texto do PDV.

  É a base do ritmo de venda e das contagens da triagem.
- **Mesmas regras por linha do Raio-X.** Leitura, limpeza, custo unitário ou total, perda abaixo do custo, vazamento de preço, recuperável, bonificação e devolução são os do Raio-X, sem cópia. `EquivalenciaTests` confere que a staging somada reproduz o Raio-X, e a rota JSON tem teste que fecha os totais com a auditoria do Raio-X dos mesmos arquivos.
- **Tetos** (`avaliacao_valor`). Um valor que a coluna do banco não comporta é recusado com mensagem, em vez de derrubar a gravação. O motor confere antes de devolver, o contrato confere de novo, e a gravação confere as somas já arredondadas (o arredondamento pode levar um valor logo abaixo do teto até ele):

  | Valores | Teto |
  |---|---|
  | Quantidades e estoque (em módulo) | menos de 10¹¹ |
  | Dinheiro | menos de 10¹² |
  | Custo médio e vazamento estimado (derivados no processo pai) | menos de 10¹⁰ |
  | Vazamento total do lead | menos de 10¹⁰ |

  O SQLite não impõe `NUMERIC(p, s)`, por isso a conferência é feita no código.
- **Saída.** O JSON é colunar: `{"colunas": [...], "valores": [[...], ...], "categorias": [...]}`. Número inteiro sai como inteiro, para o JSON ficar menor. O resultado fica abaixo do teto rígido de 4 MiB do worker, com 4 KiB de folga para o envelope; acima disso, a recusa `avaliacao_grande` explica o motivo. Medido com 24 relatórios mensais de 2.000 produtos (nome de 60 caracteres, código de 12 dígitos, NCM, categoria e estoque): **253 bytes por linha produto × ano na 11.3, contra 196 na 11.2** com os mesmos arquivos. Cabem perto de **16.400 linhas** (eram 21.100). A data da 11.4 acrescenta 13 bytes por linha: num segundo ensaio com o mesmo formato (departamento como categoria), 219 bytes na 11.3 contra 232 na 11.4, cerca de 6% a mais; aplicado à primeira medida, o teto fica perto de **15.600 linhas**. O prazo de 120 s do processo isolado continua sendo o limite que chega antes (seção 10).
- **Meta.** Guarda só contagens, versões e valores agregados, sem nome de produto nem texto livre: as versões das regras do lead e do Raio-X, arquivos por ano, a cobertura, arquivos com custo, linhas lidas, qualidade dos valores, situação (quantidade de linhas e valor em R$ dos cancelamentos e das devoluções), células recodificadas e as contagens e ajustes acima. Ao gravar, o processo pai acrescenta em `meta_json` os motivos da recomendação do plano (`motivos_recomendacao`). As categorias, que são texto do relatório, vão só para a staging, que expira; nunca para a meta.

## 3. Staging e lead

### Revisão `5e1b9c3a7d20`

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

Restrições: `ck_brainstorming_dados_pdv_chave` (chave não vazia), `ck_brainstorming_dados_pdv_somas` (somas não negativas), `ck_brainstorming_dados_pdv_bloco` (bloco válido) e `uq_brainstorming_dados_pdv_lead_ano_produto` (unicidade de lead, ano e produto, para que um reprocessamento não duplique linhas).

**`lead_avaliacao`** ganha, todas anuláveis: `versao_regras` e `versao_triagem`; `periodo_inicio` e `periodo_fim`; `arquivos`; `meta_json`; `itens_venda_sem_custo` e `itens_venda_abaixo_custo`; `registrado_por` (FK para `usuario`, `ON DELETE SET NULL`, com índice `ix_lead_avaliacao_registrado_por`); `consentimento_em`. Os CHECKs são `ck_lead_avaliacao_arquivos`, `ck_lead_avaliacao_itens` e `ck_lead_avaliacao_periodo`.

O downgrade trava `usuario` e as duas tabelas e recusa se houver qualquer lead ou linha ("Downgrade da Avaliação de Cenário recusado..."). `usuario` vem primeiro, na ordem das requisições, como na `c7d41e9a2b60`: remover a FK de `registrado_por` trava `usuario`, e travá-lo depois das tabelas de lead podia entrar em deadlock com uma requisição em andamento (os testes `postgresql_configuracoes` de downgrade concorrente falhavam por isso desde a Etapa 1).

### Revisão `7c4e2a9b5d31` (Etapa 2)

Também só expand nas duas tabelas: ADD COLUMN com padrão constante (no PostgreSQL 11 ou mais novo, não reescreve a tabela) e CHECKs. Nenhuma linha é reescrita; as linhas gravadas antes recebem o padrão do servidor, e a `versao_regras` as separa.

**`brainstorming_dados_pdv`**

| Coluna | Tipo |
|---|---|
| `faturamento_em_relatorio_com_custo`, `custo_bonificacoes`, `custo_devolucoes`, `devolucoes_com_custo` | `NUMERIC(14,2)` NOT NULL, padrão 0 |
| `quantidade_mesmos_meses` | `NUMERIC(14,3)` NOT NULL, padrão 0 |
| `meses_com_custo`, `meses_margem_negativa`, `meses_completos`, `relatorios_desde_primeira_venda` | `INTEGER` NOT NULL, padrão 0 |
| `ultimo_mes_negativo`, `ncm_ausente`, `categoria_ausente` | `BOOLEAN` NOT NULL, padrão `false` |
| `estoque_final` | `NUMERIC(14,3)`, anulável |
| `categoria_produto` | `VARCHAR(80)`, anulável (texto do relatório: expira com a linha em 30 dias) |

Restrições: `ck_brainstorming_dados_pdv_etapa2_somas` (somas novas não negativas), `ck_brainstorming_dados_pdv_etapa2_meses` (negativos ≤ com custo ≤ meses; completos ≤ meses; relatórios desde a primeira venda ≥ 0) e `ck_brainstorming_dados_pdv_categoria` (categoria nula ou com texto).

**`lead_avaliacao`** ganha `itens_omissao_erp` (`INTEGER`, anulável, com `ck_lead_avaliacao_omissao` ≥ 0) e `limiares_triagem_json` (`TEXT`, anulável: só números, os limiares usados na triagem). Nulas nos leads de antes.

Cuidados da revisão:
- `lead_avaliacao` é a tabela-mãe da staging (`ON DELETE CASCADE`). No SQLite, o modo batch recriaria a tabela-mãe e apagaria as linhas de PDV em cascata; por isso a revisão usa ADD e DROP COLUMN direto nela, e a CHECK do lead fica só no PostgreSQL (o SQLite de testes a recebe quando a tabela nasce dos modelos). Há teste que prova que o upgrade não apaga linha de PDV.
- O downgrade trava `usuario` e as duas tabelas no PostgreSQL (`usuario` primeiro, porque a `5e1b9c3a7d20`, logo abaixo na mesma transação, o trava) e **recusa** enquanto houver linha gravada pelas regras `lead-2026-11.3` ou lead com as colunas novas preenchidas ("Downgrade da Etapa 2 da Avaliação de Cenário recusado..."). O expurgo de 30 dias apaga as linhas, mas não as colunas do lead: para descer, depois do backup, é preciso apagar esses leads ou anular `itens_omissao_erp` e `limiares_triagem_json` deles. Sem esses dados, remove colunas e CHECKs e mantém as linhas antigas.

### Revisão `9b3e5d7f1a28` (Etapa 3)

Expand nas duas tabelas, de novo sem reescrever linha: ADD COLUMN anulável sem padrão e uma CHECK.

| Tabela | Coluna | Tipo |
|---|---|---|
| `brainstorming_dados_pdv` | `fim_ultimo_relatorio_com_venda` | `DATE`, anulável (nula nas linhas de antes), com `ck_brainstorming_dados_pdv_ultima_venda` (entre 2000-01-01 e 2100-12-31) |
| `lead_avaliacao` | `resumo_inteligencia_json` | `TEXT`, anulável: o resumo numérico da inteligência (seção 5.1), sem nome de produto nem de categoria, porque fica depois do expurgo |

Como na `7c4e2a9b5d31`, o lead recebe ADD e DROP COLUMN direto (o modo batch, no SQLite, recriaria a tabela-mãe e apagaria as linhas de PDV em cascata). O downgrade trava `usuario` e as duas tabelas no PostgreSQL, na mesma ordem, e **recusa** enquanto houver linha das regras `lead-2026-11.4`, linha com a data preenchida ou lead com o resumo novo ("Downgrade da Etapa 3 da Avaliação de Cenário recusado..."). Sem esses dados, remove as colunas e a CHECK e mantém as linhas e os leads.

`models.py` (`LeadAvaliacao` e `BrainstormingDadosPdv`) espelha as migrações, e testes comparam os dois no SQLite e no PostgreSQL.

Gravação (`avaliacao_cenario.registrar`):
- dinheiro com 2 casas e quantidade e estoque com 3, em `Decimal` (`quantizar`); a categoria sai do índice para o texto e a data da última venda vira `date`. Marcas e triagem saem sempre do valor gravado, então refazê-las dá o mesmo resultado;
- o resumo da inteligência (`resumo_inteligencia_json`) é calculado das mesmas linhas, na gravação e na retriagem. Ele é auxiliar: um dado que ele não consegue ler deixa o resumo nulo, sem impedir a gravação do lead;
- no reenvio, o lead é travado (`FOR UPDATE`) antes das linhas, na mesma ordem do expurgo.

**Ingestão em lote (11/2026).** Com a venda item a item com a data de cada venda no envio (a âncora), os outros arquivos deixam de ser relatórios de vendas e a completam: o relatório por produto do mesmo período dá o custo, o saldo e o cadastro, sem somar no faturamento; a posição de estoque, o inventário, a lista de preços e o resumo por pedido completam o resto; o relatório de compras, o resumo de vendas sem produto e o relatório de leiaute ambíguo do período entram fora das contas (`Docs/INDICADORES_EXECUTIVOS.md`, seção 13, com os leiautes reais do ERP e a classificação prévia, que lê a tabela auxiliar pelo título antes da leitura de vendas). Por isso o arquivo absorvido não conta na sobreposição nem na regra do período (ele não vira relatório), e o lead aceita o resumido do ERP ao lado da venda item a item do mesmo período. Quando a âncora traz a loja de cada venda, o relatório por produto com lojas dela e lojas que ela não tem é recusado (o cliente envia o de cada loja separado); sem a loja na âncora, ele é absorvido, com aviso. Sem a linha do período, a venda item a item vale pelas datas das linhas. Sem a âncora, a tabela auxiliar e o resumo de vendas sem produto ficam fora das contas quando o envio tem relatório de vendas. Quando não tem, a tabela auxiliar recusa o envio dizendo o que é (nunca com a recusa de produto, quantidade e valor de venda), e o resumo sem produto recusa com a mensagem de sempre, que pede o relatório de vendas por produto; o relatório de compras e o de leiaute ambíguo continuam recusados.

## 4. Triagem e marcas (`triagem_cenario.py`)

Função pura no processo pai, sobre as linhas da staging e a cobertura do envio (`Cobertura.do_envio(meta)`). Versão: `VERSAO_TRIAGEM = "triagem-2026-11.4"` (desde os indicadores executivos, `Docs/INDICADORES_EXECUTIVOS.md`: o ano de custo a confirmar fica fora da venda abaixo do custo e do recuperável, numa regra só, `recuperavel_do_ano` e `perda_real_do_ano`). Tudo em fração exata (`fractions.Fraction`) nas fronteiras: cada limiar vale exatamente no valor configurado (a queda de exatamente 50% entre um fevereiro de 28 dias e um abril de 30 dias é queda forte; a margem de exatamente 10% não é baixa).

### 4.1 Limiares configuráveis

`triar(linhas, cobertura, limiares=None)` recebe um `Limiares` (dataclass congelada); sem ele, valem os padrão (`LIMIARES_PADRAO`). Valores em `Decimal` e contagens em `int`: a validação recusa float, booleano, NaN, fração fora de [0, 1], razão até 1, inteiro abaixo de 1 e relação incoerente (queda moderada acima da crítica, giro crítico acima do baixo). `Limiares.de_dict` lê a gravação de forma estrita (todas as chaves, nenhuma a mais, a versão da triagem, decimal sem expoente) e `com_ajustes` troca só as chaves pedidas.

O lead grava os limiares usados em `limiares_triagem_json`. A retriagem e o reenvio sem limiares usam os gravados; gravação ilegível faz a retriagem recusar, e o diagnóstico fica "desatualizado", sem cair nos padrão em silêncio.

| Limiar | Padrão | Uso |
|---|---|---|
| `pareto_recuperavel` | 0,80 | produtos que somam 80% do recuperável do lead |
| `recuperavel_minimo_do_produto` | 0,03 | recuperável ≥ 3% do faturamento do produto |
| `impacto_minimo_do_lead` | 0,001 | o item precisa valer ao menos 0,1% do faturamento para ser Urgência: do envio inteiro na Urgência por recuperável, do último ano na margem negativa recorrente |
| `queda_critica` | 0,50 | queda do ritmo de venda (Urgência) |
| `giro_critico` / `relatorios_para_giro_critico` | 0,25 / 4 | vendeu em ≤ 25% dos relatórios mensais, com ao menos 4 |
| `venda_esperada_minima` | 1 | unidades que o ritmo anterior precisa prever para a queda valer |
| `pareto_prejuizo` | 0,80 | produtos que somam 80% do prejuízo do último ano |
| `prejuizo_minimo_do_produto` | 0,03 | prejuízo ≥ 3% da venda com custo do produto |
| `relatorios_minimos_margem_negativa` / `fracao_relatorios_negativos` | 3 / 0,50 | ao menos 3 relatórios com custo, metade negativos |
| `margem_baixa` / `distancia_margem_baixa` | 0,10 / 0,05 | margem abaixo de 10% e 5 pontos abaixo da categoria |
| `queda_de_margem` | 0,05 | queda de 5 pontos entre os dois últimos anos com custo |
| `vazamento_relevante` | 0,03 | vazamento de preço ≥ 3% do faturamento |
| `abaixo_custo_minimo` | 0,01 | vendas abaixo do custo ≥ 1% da venda com custo (motivo da Atenção) |
| `razao_custo_implausivel` | 3 | custo ≥ 3 × a venda com custo: unidade ou embalagem trocada |
| `queda_moderada` | 0,20 | queda do ritmo de venda (Eletiva) |
| `giro_baixo` | 0,50 | vendeu em ≤ metade dos relatórios mensais |
| `corte_cauda_longa` | 0,95 | itens fora dos 95% do faturamento do último ano |
| `fator_margem_mix` / `participacao_minima_mix` | 0,60 / 0,01 | margem ≤ 60% da margem de referência (a da categoria, ou a do lead), com ao menos 1% do faturamento do último ano |
| `recuperavel_residual_minimo` | 0,01 | recuperável ≥ 1% do faturamento (Eletiva) |

São parâmetros iniciais: a especificação não trazia números, e o dono deve validá-los comercialmente. Cada limiar tem até 6 dígitos inteiros e 6 decimais (as contagens, de 1 a 10⁶): milhares de dígitos deixariam cada visita à tela dezenas de vezes mais lenta.

Para mudar o padrão para todos os leads, troque o valor e a `VERSAO_TRIAGEM`. Os leads gravados antes ficam "desatualizados". A retriagem e o reenvio deles usam os limiares gravados quando a versão deles está em `VERSOES_DOS_LIMIARES` (as versões cujos limiares têm o mesmo sentido dos atuais: hoje a 11.3 e a 11.4, escritas por extenso). A cada troca de versão, a nova sempre entra no conjunto; se algum limiar mudou de sentido, as anteriores saem. Um teste confere que a versão atual está nele. Fora dela, usam os **padrão atuais** (o sentido de um limiar pode ter mudado) e avisam isso na tela e no comando. A tela escolhe os rótulos dos indicadores pela semântica da versão gravada (`VERSOES_COM_MARCAS_DA_ETAPA_2`), e não pela igualdade com a atual; os rodapés da tela e as definições do `diagnostico.json` (`diagnostico_cenario.definicoes_da_versao`) só dizem que o ano de custo a confirmar fica de fora para as versões de `VERSOES_COM_CUSTO_A_CONFIRMAR_POR_ANO` (hoje, a 11.4), e a definição e os rótulos das marcas seguem `VERSOES_COM_MARCAS_DA_ETAPA_2` (no lead de antes da Etapa 2, o preço médio abaixo do custo médio, sem a omissão no ERP). Na troca de versão, a nova entra nesses dois conjuntos se mantiver a semântica deles. Limiares personalizados de um lead precisam ser reaplicados com `flask leads retriar <id> --limiares`, que parte dos gravados ou, se forem de outra versão, dos padrão atuais.

### 4.2 Marcas por produto e ano (`marcas()`)

| Marca | Regra |
|---|---|
| `flag_venda_sem_custo` | o produto vendeu nos relatórios que trazem a coluna de custo e **nenhuma** dessas vendas tem custo. É parecido com `saude.itens_sem_custo` do Raio-X, mas por ano e só nos relatórios com a coluna: os números podem diferir dos do Raio-X dos mesmos arquivos (o Raio-X conta o produto vendido só em relatório sem a coluna, e olha o envio inteiro). Num ano sem relatório com a coluna de custo, a marca não se aplica: a falta da coluna é do arquivo exportado, não do ERP |
| `flag_venda_abaixo_custo` | `perda_real_do_ano > 0`: alguma venda com custo teve receita abaixo do custo da linha, fora o ano de custo a confirmar (custo implausível no ano). É o fato, sem materialidade; o motivo da Atenção exige 1% da venda com custo. O Raio-X (`auditoria.itens_venda_abaixo_custo`) decide o custo a confirmar pelo envio inteiro, e não por ano: as contagens podem diferir |
| `flag_omissao_erp` | alguma destas omissões (`motivos_de_omissao()`): conflito de código e nome no cadastro (ignorado quando algum relatório do envio teve cabeçalho ambíguo); custo informado só em parte das vendas nos relatórios com a coluna; custo implausível; estoque negativo (entrada de compra não lançada); NCM ausente; categoria ausente (só nos anos em que o relatório traz a coluna) |
| `vazamento_estimado_r$` | `recuperavel_do_ano`: o recuperável do produto no ano; no ano de custo a confirmar, só a variação de preço (`vazamento_preco`). Difere do Raio-X, que decide o custo a confirmar pelo envio inteiro |
| `custo_medio_registrado` | `custo_total / quantidade_com_custo`, com 4 casas; nulo sem custo |

O lead recebe a contagem de produtos que tiveram a marca em algum ano: `itens_venda_sem_custo`, `itens_venda_abaixo_custo` e `itens_omissao_erp`. A diretriz cita "descontinuidade de dados de compra" como omissão: o lead não envia compras, então essa parte não existe; a entrada de compra não lançada aparece pelo estoque negativo.

### 4.3 Blocos (`triar()`)

O ritmo de venda é a venda **por dia coberto** pelos relatórios: anos de durações diferentes não viram queda falsa. A queda compara o último ano com o anterior mais recente com venda e só vale quando o ritmo anterior prevê ao menos `venda_esperada_minima` no período comparado do último ano:
- **dois últimos anos mensais** (e o ano anterior do produto é o ano anterior do envio): só a janela dos mesmos meses, nos dois lados. O panetone vendido em dezembro não "parou" quando o último ano vai de janeiro a outubro, e o mês corrente parcial não vira queda. **Sem mês em comum** (12 meses de novembro a outubro), a queda não é avaliada, e o diagnóstico avisa: a decisão conservadora é não apresentar sazonalidade como parada; o item que de fato parou aparece no estoque parado;
- **ano anterior não mensal, ou o produto não vendeu no ano anterior do envio:** os períodos inteiros (o relatório anual não separa os meses).

O giro (parte dos relatórios do último ano com venda) só existe com relatórios mensais. O de um **lançamento** (sem venda nos anos anteriores) conta só os relatórios desde a primeira venda: o produto que vende todo mês desde outubro não tem "giro crítico". O giro crítico exige ao menos 4 relatórios nessa base.

Cada produto fica em no máximo um bloco, nesta precedência:

1. **Urgência** — impacto financeiro imediato, qualquer um:
   - **potencial recuperável alto e estagnação crítica:** entre os que somam 80% do recuperável do lead (sem os itens de custo implausível), com recuperável ≥ 3% do próprio faturamento e ≥ 0,1% do faturamento do envio; e venda parada, em queda forte ou com giro crítico. Com estoque positivo, entra também "estoque parado". Com estoque zero ou negativo, a parada é "possível ruptura" e vai para a Atenção;
   - **margem negativa recorrente:** no último ano, ao menos 3 relatórios com custo, metade deles abaixo do custo e o mais recente negativo, contados por relatório, mensais ou não (com um único relatório no último ano, o anual: o ano negativo); o prejuízo do último ano entre os 80% do prejuízo do lead, ≥ 3% da venda com custo do produto no último ano e ≥ 0,1% do faturamento do último ano. O rótulo pede para confirmar se é promoção.
2. **Atenção** — margem degradada ou divergência de CMV: sem custo; vendas abaixo do custo (≥ 1% da venda com custo, fora o ano de custo a confirmar); margem em queda (≥ 5 pontos); margem baixa relativa (abaixo de 10% e 5 pontos abaixo da margem da categoria, ou do lead quando a categoria tem menos de 3 itens com custo); vazamento de preço; custo parcial; custo implausível; conflito de cadastro; possível ruptura.
3. **Eletiva** — otimização de estoque, giro de cauda longa e mix: ritmo em queda (≥ 20%); giro baixo; cauda longa (fora dos 95% do faturamento do último ano, com giro baixo e venda em ano anterior: lançamento e item que vende todo mês não entram); margem abaixo do mix (margem ≥ 10% mas ≤ 60% da margem da categoria, com ≥ 1% do faturamento); estoque negativo; sem NCM; sem categoria; potencial recuperável menor (≥ 1% do faturamento). A estagnação sem recuperável alto (venda parada, queda forte ou giro crítico) entra com os motivos dela quando não tem outro motivo da Eletiva, e com "estoque parado" na frente quando há estoque positivo.

**Estoque parado** (`ResultadoTriagem.estagnados`): todo produto com estagnação crítica e estoque positivo, em qualquer bloco. É a base do estoque parado da matriz e do impacto (seção 5).

**Custo implausível** (custo ≥ 3 × a venda com custo, típico de embalagem cadastrada como unidade) sai dos Paretos e do impacto e aparece "a confirmar": um erro de cadastro de R$ 82 mil não empurra os itens legítimos para fora da Urgência. Desde a 11.4, o ano de custo a confirmar sai também do motivo "abaixo do custo" e do recuperável: fica só com a variação de preço, e o motivo é o custo implausível. A perda real do mesmo produto noutro ano continua contando.

**Códigos de motivo.** Quando uma regra muda de sentido, o código muda (`sem_custo`, `abaixo_do_custo`, `margem_baixa_relativa`, `recuperavel_menor`). Os códigos das versões anteriores ficam em `ROTULOS_HISTORICOS`, porque os resumos de leads antigos continuam no banco. `rotulo_do_motivo(codigo, limiares)` monta o texto com os limiares usados ("Ritmo de venda caiu 40% ou mais" com `queda_critica` 0,40).

Os resumos dos blocos (`resumo_urgencia_json`, `resumo_atencao_json` e `resumo_eletiva_json`) guardam a versão, o número de itens, o faturamento, o recuperável e a contagem de motivos. **Nunca guardam nome de produto**, porque ficam no lead depois do expurgo das linhas.

**Retriagem.** "Refazer a triagem" na tela ou `flask leads retriar <id>`:
- só funciona dentro dos 30 dias contados do envio, mesmo antes de o expurgo rodar (o mesmo critério do diagnóstico); a tela esconde o botão quando os dados já expiraram;
- recusa o lead expirado, o lead sem linhas, as linhas gravadas por outra `VERSAO_REGRAS_LEAD`, o lead sem a cobertura do envio na meta e o lead com limiares gravados ilegíveis; com limiares de outra versão, usa os padrão atuais e avisa;
- grava em lote, pela chave primária, e grava os limiares usados.

## 5. Diagnóstico: matriz por categoria e payload (`diagnostico_cenario.py`)

Função pura, sem banco nem Flask, sobre as linhas da staging e o resumo do lead. Versão: `VERSAO_DIAGNOSTICO = "diagnostico-lead-2026-11.3"` (a 11.2 acrescentou a seção `inteligencia`, seção 5.1; a 11.3, as `visualizacoes` e o `resumo_inteligencia` gravado). A tela do consultor e a rota JSON usam o mesmo payload.

**O que o lead não tem.** O fluxo do lead recebe só relatórios de **vendas**. Por isso:
- **Compras** não entram no cruzamento: não há relatório de compras do lead;
- **Perdas operacionais** (quebra, validade, avaria, furto) ficam nulas, com o motivo: exigem relatório de perdas ou inventário. O que existe são as vendas abaixo do custo;
- margem e CMV são os do Raio-X, sobre o custo informado pelo PDV nas vendas: não são a margem de lucro nem o CMV contábil.

**Matriz por categoria** (`matriz_por_categoria`). A categoria do produto é a do ano mais recente que tiver uma; grafias diferentes do mesmo nome ("Bebidas" e "BEBIDAS ") somam juntas, com o rótulo mais recente. Para cada categoria e para o total:

| Campo | Pedido da diretriz | Definição |
|---|---|---|
| `faturamento`, `faturamento_ultimo_ano`, `participacao_ultimo_ano` | Faturamento Total | venda bruta do envio e do último ano |
| `faturamento_venda_continua` | Faturamento Recorrente | venda do último ano dos itens que venderam em ao menos 75% dos relatórios mensais completos (período já terminado); nulo sem relatórios mensais no último ano ou com menos de 2 completos |
| `venda_com_custo`, `custo_das_vendas` | — | venda com custo informado e o custo dela |
| `custo_bonificacoes`, `custo_devolucoes`, `cmv_liquido` | CMV Líquido | custo das vendas + bonificações − devoluções |
| `margem_bruta_estimada` | Margem de Contribuição Bruta | venda com custo − custo das vendas (valor e %) |
| `margem_com_bonificacoes_e_devolucoes` | Margem de Contribuição Líquida | (venda com custo − receita devolvida) − CMV líquido (valor e %) |
| `perdas_operacionais` | Perdas operacionais | nulo (ver acima) |
| `vendas_abaixo_do_custo`, `vazamento_de_preco`, `potencial_recuperavel` | Caixa resgatável | o que se recupera no preço (retroativo; inclui promoção intencional) |
| `estoque_parado` | Caixa resgatável | estoque × custo médio dos itens com estagnação crítica (venda parada, queda forte ou giro crítico) e estoque positivo, em qualquer bloco; nulo sem estoque informado (a coluna falta ou vem vazia em todas as linhas: o critério da inteligência) |
| `impacto`, `impacto_acumulado` | ordenação | soma do impacto dos itens (abaixo) |
| `itens_por_bloco`, `marcas`, `cauda`, `a_confirmar` | — | contagens |

O potencial recuperável e o estoque parado ficam separados porque não se somam: um é retroativo, o outro é capital parado hoje. O total soma também os ajustes de produto sem venda no ano e fecha com a auditoria do Raio-X dos mesmos arquivos (há teste). A matriz só é publicada com categoria em ao menos metade do faturamento; "Sem categoria" vai por último, fora da ordenação. As categorias saem em **ordem decrescente de impacto** no último ano (depois o acumulado e o faturamento).

**Impacto de cada item** (`impacto_do_produto`): por ano, o maior entre o potencial recuperável, a venda sem custo exposta, a oportunidade de margem (só com motivo de margem: distância até a margem de referência × venda com custo) e, no último ano, o estoque parado. Item de custo implausível tem impacto zero e aparece "a confirmar". Os itens de cada bloco saem em ordem decrescente de impacto do último ano, depois o acumulado e o nome.

**Payload** (`montar_payload`, audiência "consultor"):
- `GET /admin/leads/<id>/diagnostico.json`, só `admin_required`, com `Cache-Control: no-store`; o log da leitura leva só os identificadores;
- **nunca** leva o contato do lead (nome, e-mail, WhatsApp, loja) nem quem o registrou;
- `situacao`:
  - `atual`: linhas e triagem da versão atual, dentro da retenção. Vêm a cobertura, os totais, as categorias e os itens por bloco;
  - `desatualizado`: linhas de outra `VERSAO_REGRAS_LEAD`, triagem de outra versão, meta ou limiares ilegíveis. Só o resumo gravado;
  - `expirado`: lead expirado, sem linhas ou com as linhas além dos 30 dias (pela data do envio, mesmo antes de o expurgo rodar). Só o resumo gravado;
- itens por bloco até `?limite=` (padrão 30, entre 1 e 200; um limite que não é número vale o padrão);
- `definicoes` com o texto de cada campo e `avisos` (matriz não publicada, cabeçalho ambíguo, retenção; na 11.2, relatórios sem estoque, relatório com várias lojas e inteligência que não pôde ser calculada);
- a seção `inteligencia` (seção 5.1), com os parâmetros do simulador e da auditoria de preço trocáveis pela consulta: `?descontos=0.1,0.25&fracao_liquidada=0.5&markup_informado=0.35&cobertura_alvo_dias=45`. Valores com até 6 casas e só dígitos ASCII, com ponto ou vírgula decimal (`0.5` ou `0,5`); os descontos separados por vírgula com ponto decimal (`0.1,0.25`) ou por ponto e vírgula com vírgula decimal (`0,1;0,25`); dias em inteiro. Parâmetro fora da faixa ou ilegível responde **400**, com a mensagem que cita o nome do campo como a tela o mostra e o exemplo daquele campo (`{"erro": "parametros_invalidos", "mensagem": ...}`, com `Cache-Control: no-store`), sem calcular nada; outros nomes na consulta são ignorados;
- números finitos: JSON gravado com `NaN`, `Infinity`, número que estoura (`1e999`), inteiro com mais de 18 dígitos ou aninhado sem fim é ignorado; meta, limiares ou resumo fora do formato deixam o lead "desatualizado", sem erro 500;
- toda linha precisa ser do próprio lead (`lead_id`): linha de outro lead faz o montador recusar (`ValueError`), para um filtro esquecido nunca misturar leads;
- até 60 categorias na matriz (as de maior impacto); as demais saem somadas em "Demais categorias (N)";
- `resumo_inteligencia`: o resumo numérico gravado no lead (seção 5.1), que fica depois do expurgo, só se for da versão atual da inteligência e estiver no formato (números não negativos abaixo de 10¹², faixas por dias); senão, nulo. As telas, a lista e as `visualizacoes` usam a mesma validação (`diagnostico_cenario.resumo_inteligencia_valido` e `resumo_de_bloco_valido`), e um resumo fora do formato vale nada, sem erro 500;
- `visualizacoes` (11.3): séries prontas para gráficos interativos, tiradas do próprio payload, sem cálculo novo. Cada uma tem `titulo`, `tipo` (`barras` ou `linhas`), `rotulos` e `series` (`nome`, `unidade` em `BRL` ou `itens` e `valores`, nulos onde não há dado):
  - `triagem`: produtos, faturamento e potencial recuperável por bloco;
  - `marcas`: produtos com venda sem custo, venda abaixo do custo e omissão no ERP;
  - `caixa_resgatavel`: potencial recuperável, estoque parado (primeira faixa), estoque excedente e o estoque da Urgência a preço médio, com a `natureza` de cada parcela (retroativo, capital parado, capital liberável, projeção): **não se somam**. Sem o cálculo atual (lead expirado, de outra versão ou com a inteligência ilegível), sai do resumo gravado válido, e a tela diz que os valores são os do envio; com o cálculo atual, só ele vale (sem estoque informado, as parcelas de estoque ficam nulas, diga o resumo gravado o que disser);
  - `estoque_por_idade`, `categorias_por_impacto` (até 10), `simulador` (desconto × caixa e resultado) e `auditoria_de_preco` (até 10 categorias), quando há o dado.

**Desempenho da tela e da rota.** As linhas do lead saem numa consulta só, sem montar objetos do ORM (`avaliacao_cenario.linhas_do_lead`), e o diagnóstico fica num **cache por processo** (`blueprints/leads._CacheDoDiagnostico`, até 8 diagnósticos). A chave leva o lead, a assinatura do conteúdo das linhas (o sha256 do conteúdo em texto, que muda com qualquer valor de qualquer linha), os campos do lead que o payload usa, o limite, os parâmetros e a versão do diagnóstico: o reenvio, a retriagem com outros limiares, o expurgo e qualquer mudança de dado geram outra chave, e a mesma leitura não refaz a triagem, a matriz e a inteligência. A entrada vale `LEADS_DIAGNOSTICO_CACHE_SEGUNDOS` (padrão 600; 0 desliga) e **nunca é servida depois da retenção** das linhas. As vencidas saem da memória a cada pedido ao processo, de qualquer tela (`before_app_request`, no máximo 8 comparações), também depois de um expurgo feito pelo pg_cron ou pelo comando. O reenvio, a retriagem e o expurgo pela tela tiram as entradas antigas na hora, e uma **geração por lead** impede que uma leitura concorrente, que carregou as linhas antes do descarte, guarde o dado de antes dele. Num processo sem nenhum pedido, a entrada vencida fica até o próximo pedido ou até o processo parar (no plano gratuito do Render, ele dorme depois de 15 minutos sem tráfego). Cada leitura, pela tela ou pela rota JSON, vai para o log só com os identificadores do lead e do consultor, mesmo vinda do cache. Medido com 8.000 linhas produto × ano neste computador, com outros testes rodando: a primeira leitura leva cerca de 2 s (0,6 s de carga e 1,4 s de triagem, matriz e inteligência); a repetida, cerca de 0,6 s, só a carga que confere o conteúdo. No Render (0,1 CPU), a proporção deve se manter, com tudo perto de 10 vezes mais lento.

**Tela do consultor.** O resumo executivo do caixa resgatável (as quatro parcelas com barra e a natureza de cada uma), a matriz de triagem, a mesma matriz por categoria (com a linha de total), os indicadores das três marcas, o estoque parado por faixa com os maiores capitais, o excedente, o simulador (formulário com descontos, fração vendida, markup do segmento e cobertura-alvo, que recarrega a tela com os parâmetros; inválido, a tela avisa e usa os padrão, sem erro), a auditoria de preço e os itens de cada bloco, com categoria, impacto e motivos, e o atalho "Diagnóstico em JSON". Lead expirado ou de outra versão: o resumo executivo sai do resumo gravado, com a data do envio, sem as seções que dependem das linhas. O capital parado é descrito como o que é: o último saldo informado nos relatórios, ao custo, e não inventário. Com linhas ou triagem de outra versão, a tela lista os itens no **bloco gravado**, sem motivos, impacto nem matriz, e avisa a versão.

**Dashboard do cliente.** Não foi aberto: o lead ainda não é cliente nem tem acesso ao portal. O payload já separa a audiência; a exposição ao cliente depende da conversão do lead e do recorte por plano (seção 10).

### 5.1 Inteligência de estoque e de preço (Etapa 3, `inteligencia_estoque.py`)

Funções puras no processo pai, sobre as linhas da staging, a triagem e a meta. Versão: `VERSAO_INTELIGENCIA = "inteligencia-lead-2026-11.2"` (na Etapa 4, a coluna de estoque vazia passou a valer como estoque não informado e o resumo gravado ganhou as contagens de produtos sem saldo; o resumo da 11.1 vale nada). Somas em `Decimal`; dinheiro arredondado em centavos na saída. O lead envia só relatórios de **vendas**: o estoque é o saldo que o relatório de vendas imprime e as datas são as dos períodos dos relatórios. Itens de custo implausível ("a confirmar") ficam fora de tudo.

**Parâmetros** (`ParametrosInteligencia`, como os limiares da triagem): faixas de dias `(60, 90, 180)`, cobertura-alvo de 60 dias, descontos simulados `0, 10%, 20% e 30%`, fração do estoque vendida 1, mínimo de 3 itens por categoria para a referência de markup, markup informado (nenhum), tolerância de preço de 0,5% e 30 itens por lista. A validação recusa float, booleano, NaN, faixa fora de ordem e valor fora da faixa.

1. **Estoque parado por idade e cauda longa** (`estoque_parado_por_idade`). Itens com estoque e custo informados há pelo menos 60, 90 ou 180 dias sem venda, e o capital imobilizado neles (estoque × custo médio do ano mais recente com custo): é o valor de oportunidade de liberar capital. As faixas saem separadas (60 a 89, 90 a 179, 180 ou mais) e acumuladas (`acima_de`: 60 ou mais, 90 ou mais, 180 ou mais), com a participação no capital em estoque e os 30 itens de maior capital. Os **dias sem venda são um piso**: contados do fim do último relatório em que o produto vendeu até o fim da cobertura do envio, **só nos dias que algum relatório cobre**. Um mês sem relatório não conta, porque a venda pode ter acontecido nele; o item fica com a marca `com_lacuna`. Com relatório anual, quem vendeu no ano tem piso 0 (a venda pode ter sido no último dia).
2. **Estoque excedente** (giro baixíssimo de quem ainda vende). Itens com menos de 60 dias sem venda e estoque para mais dias que a cobertura-alvo, no ritmo do último ano (quantidade ÷ dias cobertos). O capital liberável é o estoque acima da cobertura-alvo, ao custo: o que se libera sem deixar o item faltar. As faixas de dias de estoque começam na primeira faixa (60); o item entre a cobertura-alvo e ela entra só no total.
3. **Simulador de liberação de caixa** (`simular_liquidacao`). Para os itens da **Urgência** com estoque, custo e preço: a receita do estoque ao preço médio praticado no último ano com venda e o capital ao custo. Cada cenário aplica o desconto e a fração do estoque vendida e dá o caixa e o resultado contra o custo; `desconto_de_equilibrio` é o desconto em que o caixa empata com o custo (negativo: nem sem desconto o estoque paga o custo). Os itens fora saem contados (a confirmar, sem estoque, sem custo, sem preço). A marca `abaixo_do_custo_sem_desconto` diz, pela comparação exata, quando nem sem desconto o estoque paga o custo (o equilíbrio arredondado perto de zero nunca sai como -0,0%); a tela diz então quanto acima do preço médio seria preciso vender. É uma tabela de sensibilidade, não uma sugestão de desconto nem uma garantia de venda.
4. **Auditoria de preço** (`auditar_precos`). No último ano, nas vendas com custo, o preço praticado médio contra custo médio × (1 + markup de referência). A referência é a da **categoria**: o menor entre o markup agregado dela (venda ÷ custo − 1) e a mediana dos markups dos itens, com ao menos 3 itens; o menor dos dois é a escolha conservadora. O consultor pode informar o **markup do segmento** (`markup_informado`), que vale para todos os itens. Nenhuma referência de mercado é inventada: sem categoria com itens suficientes e sem markup informado, o item fica "sem referência". Item com conflito de cadastro fica de fora (a não ser com cabeçalho ambíguo, quando o conflito não é confiável). A diferença de cada item abaixo da referência (fora da tolerância) se separa em `ja_contado_no_recuperavel` (o que o potencial recuperável da Etapa 2 já conta) e `adicional`, para não somar duas vezes. Não considera demanda, concorrência nem promoção, e não é lucro.

**No payload** (`inteligencia`): `versao`, `parametros`, `estoque` (parado e excedente), `simulador`, `auditoria_de_preco` e `definicoes`. Sem estoque informado (a coluna falta em todos os relatórios ou vem vazia em todas as linhas), `estoque` e `simulador` ficam nulos (zero diria "nada parado", o que não se sabe), com aviso, e o estoque parado da matriz por categoria também (um critério só, `estoque_informado`). Com estoque informado só em parte dos produtos, as somas de estoque podem ser parciais: a tela avisa quando algum produto sem saldo tem custo (sem custo, ele ficaria fora das somas com saldo ou sem), e a lista marca o parado como parcial só quando algum desses já está na primeira faixa sem venda (quem ainda vende não entraria na soma com saldo nenhum). No simulador, os itens fora separam o estoque não informado do estoque zerado ou negativo. Um dado que a inteligência não consegue ler tira só a seção dela, com aviso: a matriz e os blocos continuam.

**Resumo gravado** (`resumo_inteligencia_json`): só números (capital em estoque, capital e itens parados acima de cada faixa, capital excedente liberável, produtos sem estoque informado (no total, os com custo e os com custo já na primeira faixa sem venda), receita e capital da Urgência, diferença de preço adicional e itens abaixo da referência), sem nome de produto nem de categoria, porque fica depois do expurgo das linhas. Sem estoque informado, os campos de estoque e da Urgência ficam nulos (`com_estoque: false`). Com estoque informado, as três contagens de produtos sem saldo são obrigatórias (a falta de uma diria que a soma está completa): sem elas, o resumo inteiro vale nada.

**Memória.** O cálculo roda no processo da aplicação, sobre as linhas já carregadas, em uma passada por produto; as listas saem cortadas em 30 itens. Com 20 mil produtos em dois anos, o pico medido pelo `tracemalloc` fica abaixo de 64 MiB (há teste). O motor (no processo isolado de 384 MiB) só ganhou a data e os períodos.

## 6. Retenção e expurgo (revisão `6a8f2d4c0e13`)

- **Com pg_cron.** Onde a extensão estiver habilitada (no Supabase: Database › Extensions › pg_cron), a migração agenda o job `nexo_expurgo_leads_pdv` para todo dia às 06:15 UTC (03:15 em Brasília). O job roda `SELECT * FROM <esquema>.expurgar_brainstorming_pdv(30)`, a mesma função do comando `flask leads expurgar-pdv --aplicar`. Ele roda dentro do banco: não depende do servidor web, que dorme no plano gratuito do Render, nem de cron pago.
- **Sem pg_cron.** No PostgreSQL sem a extensão, a revisão não agenda nada e emite um `WARNING` ("pg_cron ausente: o expurgo diário dos leads NÃO foi agendado..."). O aviso vai para o log do PostgreSQL e **não aparece na saída do `flask db upgrade`**: a conferência é o `SELECT` em `cron.job` da seção 9. No SQLite, a revisão não faz nada e não avisa. Nos dois casos, o expurgo segue pelo comando, que um cron externo pode chamar, e pela própria tela (abaixo).
- **Extensão habilitada depois.** `flask leads agendar-expurgo` agenda o mesmo job, sem downgrade. Fora do PostgreSQL, ou sem o pg_cron, o comando recusa sem tocar em nada.
- **Pela tela.** A lista, o detalhe e a rota JSON dos leads rodam a mesma regra ao serem abertos (`blueprints/leads.expurgo_oportunista`), no máximo uma vez por hora por processo. Além disso, o diagnóstico confere a data do envio: linha além dos 30 dias não aparece nem antes de o expurgo rodar. Um erro de banco no expurgo vai para o log só com o tipo e nunca derruba a página.
- **Reagendar e downgrade.** Reagendar com o mesmo nome atualiza o job. O downgrade da `6a8f2d4c0e13` remove o job, se ele existir. Nenhum dado é tocado.
- **O que a função faz.** Ela não mudou e segue na `BAIA_DE_SEGURANCA_E_LEADS.md`, seção 2. Apaga as linhas de PDV com mais de 30 dias de leads não convertidos e marca como `expirado` o lead que ficou sem linhas. Mantém o resumo e o contato. A matriz por categoria e os itens não são guardados: saem com as linhas.
- **Na tela.** O detalhe do lead mostra quando os dados de PDV expiram: o `criado_em` do lote mais 30 dias.
- **Fora do backup.** O job fica no esquema `cron`, fora do `pg_dump` da aplicação (`Docs/OPERACAO_BACKUP_RESTAURACAO.md`). Depois de uma restauração, rode `flask leads agendar-expurgo`.

## 7. Segurança e concorrência

- **Acesso.** Só consultores (`admin_required` em todas as rotas, inclusive a JSON). Tem CSRF em todos os formulários e limite de 20 envios por hora.
- **Isolamento (Etapa 4).** Um teste percorre o `url_map` e confere que **toda** rota sob `/admin`, por GET e por POST com CSRF válido, manda o visitante para o login e responde 403 ao cliente, sem dado de lead na resposta (rota nova entra sozinha). O cliente não abre a análise nem o PDF publicados de outra empresa (e abre os da própria, o controle do teste). Outra varredura do `url_map` passa por **toda** rota fora de `/admin` (só a saída e, por POST, o pedido de exclusão da conta ficam de fora), por todos os métodos, com o id real do lead em todo parâmetro que não é número e as telas do cliente com conteúdo (análise publicada, chamado e notificação dele e o Raio-X da empresa dele, num plano que libera a auditoria), mais o assistente com uma pergunta de verdade. Ela confere que nenhuma resposta traz produto, categoria ou contato de lead, que nenhuma quebra (5xx ou exceção) e, com um ouvinte do SQLAlchemy, que nenhum comando SQL desses pedidos cita as tabelas de lead. Um teste estrutural confere que nenhum módulo do projeto fora dos da Avaliação de Cenário (e do processo isolado, que despacha a operação dela) cita as tabelas ou os módulos de lead; o serviço de envio, que também monta o Raio-X do cliente, fica no teste. O diagnóstico de um lead só traz as linhas dele (o montador recusa linha de outro lead), inclusive com dois leads de relatórios iguais, com o cache ativo e com os leads já sem linhas, quando só a identidade do lead separa as chaves. No PostgreSQL, além do RLS das tabelas de lead (que nega a Data API mesmo com GRANT), o ensaio confere a carga por lead e o cache no banco real.
- **Cache do diagnóstico.** Fica na memória de cada processo da aplicação, como a resposta que ele evita refazer, e só consultores o leem. A entrada nunca é servida depois da retenção das linhas; as vencidas saem da memória a cada pedido ao processo (de qualquer tela), e o reenvio, a retriagem e o expurgo pela tela tiram as entradas antigas na hora, inclusive a que uma leitura concorrente terminaria de calcular. O dado só fica além disso num processo sem nenhum pedido, até o próximo ou até o processo parar.
- **Arquivos.** Nenhum arquivo do lead é guardado, nem o nome. O processamento acontece no diretório temporário do Raio-X, que some ao fim.
- **Dados do PDV.** Nome, código e categoria dos produtos só aparecem enquanto as linhas existem e estão dentro da retenção; a meta, os resumos e os limiares gravados no lead não levam texto do PDV.
- **Logs.** Erros de banco vão para o log só com o tipo do erro. Os comandos mostram só contagens. A leitura do diagnóstico, na tela de detalhe e na rota JSON, registra só os identificadores do lead e do consultor.
- **Concorrência.** Há duas vagas únicas por processo:
  - a de entrada (`etl_uploads._UPLOAD_ADMISSION`), compartilhada com as análises e o Raio-X;
  - a do processo isolado (`etl_runtime._ADMISSION`).

  Com qualquer uma ocupada, a resposta é 503 com `Retry-After: 30`. Nenhum arquivo é processado e nada é gravado, e o consultor vê a mensagem de enviar de novo. A API do Raio-X do cliente (`/api/onboarding/diagnostico`) segue a mesma regra.
- **Tempo da requisição.** O gunicorn do Render roda com `--timeout 180` (`render.yaml`), acima do prazo de 120 s do processo isolado.
- **Transação.** Nenhuma transação fica aberta durante o processo isolado. A gravação é curta e acontece num SAVEPOINT.
- **Trava de células.** Cada aba tem no máximo 500.000 células (`etl_limits._HARD_CAPS["max_cells_per_sheet"]`), padrão e teto iguais, então nenhuma configuração sobe esse número. Isso vale para CSV e XLSX, antes da leitura completa.
- **Lead convertido.** Não aceita reenvio. O lead expirado não aceita retriagem.
- **Retriagem.** Um valor acima do teto vira aviso na tela, e não erro 500.

## 8. Testes

**SQLite e processo próprio** (rodam neste Windows):
- `tests/test_triagem_cenario.py` (54 testes, puros):
  - **`LimiaresTests`:** padrão, ida e volta pelo JSON exata, validação de tipo, faixa e relação, leitura estrita, ajustes, resultado que muda com os limiares e rótulos com limiares e históricos;
  - **`MarcasTests`:** sem custo com e sem a coluna, custo parcial, perda de R$ 0,01 (marca sem o motivo), cada fonte de omissão, custo médio e contagens do lead (inclusive com cabeçalho ambíguo);
  - **`BlocosTests`, `UrgenciaTests`, `AtencaoTests`, `EletivaTests`:** o cenário de referência, a ordem das linhas, a precedência, cada motivo sozinho, estoque parado e possível ruptura, piso de impacto do lead, custo implausível fora dos Paretos, margem negativa recorrente (2 de 6, recência, poucos relatórios, prejuízo pequeno, relatório anual), margem estrutural da categoria, mix, cauda longa e um catálogo de 1.000 itens que não inunda a Eletiva;
  - **`RitmoTests` e `FronteirasDaTriagemTests`:** anos de durações diferentes, item sazonal nos mesmos meses, giro só com relatórios mensais, meta inválida e as fronteiras exatas;
  - **`DiagnosticoTests`:** matriz por categoria (grafias, ordem por impacto, CMV líquido e margens, ajuste sem venda só no total), publicação com metade do faturamento, venda contínua, impacto e "a confirmar", payload sem contato, limite de itens, vencido, expirado, desatualizado e limiares gravados.
- `tests/test_avaliacao_cenario.py` (58 testes):
  - **`MotorTests`:** ano, sobreposição, ano corrente, período futuro, mês corrente até hoje, matriz, cobertura, marcas, nome só com espaços, tetos, leitura do cadastro (NCM, categoria, estoque, bonificação e devolução por produto fechando com o Raio-X), cabeçalho ambíguo e XLSX;
  - **`EquivalenciaTests`:** a staging reproduz o Raio-X;
  - **`ContratoTests`:** o formato do retorno, com 27 variantes antigas e 36 da Etapa 2 recusadas (categorias, índices, coerência, tetos, meta), o motor sem banco nem config, a operação do worker, o prazo de 120 s e a memória de 384 MiB;
  - **`QuantizacaoTests`:** dinheiro com 2 casas, quantidade e estoque com 3 e a categoria do índice;
  - **`GravacaoTests`:** gravação (com as três contagens e os limiares), reenvio, recusas, retriagem e reenvio com os limiares gravados, comandos (`retriar --limiares`, `agendar-expurgo` fora do PostgreSQL), resultado malformado sem erro 500, tetos (inclusive a soma que o arredondamento leva ao teto);
  - **`RotasTests`:** só consultor em todas as rotas (inclusive a JSON), diagnóstico em JSON (cabeçalhos, sem contato, triagem refeita igual à gravada, totais iguais à auditoria do Raio-X, limite, 404), desatualizado e vencido só com o resumo, 503 sem esquema, autorização, 503 com `Retry-After`, 413, expurgo pela tela, lista, erro do motor, 422, retriagem, triagem de versão anterior (itens no bloco gravado, sem motivos), CSRF e 404;
  - **`TravaDeCelulasTests`:** CSV e XLSX com 500.004 células são recusados.
- `tests/test_leads_avaliacao.py` (12): esquema, restrições, unicidade, índice, downgrade até a revisão que recusa, downgrade da Etapa 2 recusado com dados 11.3, o upgrade da Etapa 2 sem apagar linha de PDV, o downgrade da Etapa 3 recusado em cada caso (linha 11.4, data preenchida, resumo no lead) e aceito sem eles, sem perder linha nem lead, e a CHECK da data.
- `tests/test_inteligencia_estoque.py` (47, Etapa 3, mais os casos da Etapa 4: a coluna de estoque vazia, também na matriz, e os produtos sem saldo (com custo e já parados); cada uma de 21 mutações do código, das faixas ao contrato, derruba ao menos um teste):
  - **`ParametrosTests`:** padrão, gravação só com texto e inteiros, validação e ajustes a partir do texto da consulta (vírgula decimal, expoente, dígito não ASCII, nome desconhecido);
  - **`PeriodosTests`:** períodos da meta, dias cobertos com e sem lacuna e as fronteiras de cada faixa;
  - **`EstoqueParadoTests`:** faixas, acumulados, capital, lista, excedente e cobertura-alvo, lacuna, relatório anual, venda só no ano anterior, a fronteira exata de 60 dias, faixas configuráveis e a ordem das linhas;
  - **`SimuladorTests`:** receita, capital, cenários, equilíbrio (inclusive negativo), itens fora, fração e desconto configuráveis;
  - **`AuditoriaTests`:** referência pelo agregado e pela mediana, conflito de cadastro com e sem cabeçalho ambíguo, markup informado, tolerância, a parte já contada limitada à diferença e mínimo de itens;
  - **`ResumoEPayloadTests`:** resumo só com números, campos nulos sem estoque, a seção no payload com os parâmetros, falha isolada (data ilegível, meta sem os períodos) e os avisos;
  - **`MotorTests`, `ContratoTests` e `VariasLojasTests`** (motor real): data da última venda cortada em hoje, saldo de mês sem venda, períodos com lacuna, a inteligência de ponta a ponta, 18 variantes do contrato recusadas (data, períodos, lojas) e a data de outro ano dentro dos períodos, soma do estoque por loja, a mesma loja escrita de jeitos diferentes, "Unidade" e medidas da loja fora da detecção, a recusa da coluna de loja no lugar do produto e o Raio-X do cliente igual com e sem a coluna de loja;
  - **`GravacaoTests` e `RotaTests`:** o resumo gravado igual ao recalculado das linhas, a retriagem refazendo o resumo, o resumo que falha sem impedir a gravação, os parâmetros pela consulta, o 400 e o acesso só do consultor;
  - **`MemoriaTests`:** 20 mil produtos em dois anos abaixo de 64 MiB.
- `tests/test_cadastro_regression.py` (3): o Raio-X e a análise mensal com NCM, categoria, situação, bonificação, devolução e estoque, iguais aos do `53020e6`.
- Bordas da Etapa 2, escritas depois da verificação adversarial (as lacunas que o teste de mutação achou e as correções):
  - `tests/test_etapa2_triagem_bordas.py` (40, puros): a janela dos mesmos meses (parcial, vazia, ano anterior mais antigo, ano não mensal), o giro de lançamento, o estoque parado em qualquer bloco e no impacto, as fronteiras da margem negativa recorrente, o estoque informado, a ordem por impacto, a margem de referência, as fronteiras exatas e os 23 limiares padrão, o diagnóstico com dado gravado fora do formato (meta, ajustes, limiares, resumos, números não finitos), as linhas de outro lead e o teto de categorias;
  - `tests/test_etapa2_motor_bordas.py` (19, motor real): a janela com ano inteiro contra janeiro a outubro, doze meses sem mês em comum, só outubro em comum e o mês corrente parcial; o lançamento; o relatório sem a coluna de custo; a margem negativa por relatório; o estoque (zero, célula vazia, linha sem venda no arquivo e saldo de um mês sem venda); o cabeçalho ambíguo; a categoria com espaço especial; o teto do dinheiro da meta; o nome com caractere de controle;
  - `tests/test_etapa2_contrato_gravacao.py` (21): o contrato com cada campo trocado (contagens, texto não imprimível, inteiro enorme, período invertido, teto da meta, JSON aninhado demais vindo do processo isolado), a ida e volta da staging, o cabeçalho ambíguo na gravação e na retriagem, a retenção de 30 dias na retriagem (função, tela e comando), os limiares de outra versão ou ilegíveis, o teto do vazamento total, o log só com identificadores, a lista sem o esquema, o resumo e a meta fora do formato e o downgrade com lead expirado que guarda as colunas da Etapa 2.

- Etapa 4:
  - `tests/test_portal_diagnostico.py` (35): a carga sem ORM igual à `linha_da_staging` e a assinatura que muda com qualquer valor (inclusive de -1 para -2, que o `hash()` do Python iguala); o cache (segunda leitura sem recalcular, outra chave com limite, parâmetros, limiares e linha trocados, a fronteira exata da retenção no lead atual e no de outra versão contra o fim calculado por SQL, o TTL 0, o limite de entradas, a varredura das vencidas em qualquer pedido, o descarte no reenvio, na retriagem e no expurgo, a leitura concorrente que não guarda o dado de antes do descarte, com o descarte no cálculo ou na carga das linhas, a rota que lê do cache e registra cada leitura); as visualizações (formato, naturezas do caixa, só números, do resumo gravado depois do expurgo, resumo gravado fora do formato, com faixa que só a checagem de dígitos ou de tamanho recusa, ou de outra versão (inclusive a 11.1), ou sem uma das contagens de produtos sem saldo, fora do detalhe e do JSON e sem erro 500); as telas (detalhe com o resumo executivo, a matriz de triagem, as marcas, o estoque, o simulador e a auditoria; simulador pela consulta, com vírgula decimal e com parâmetro inválido avisado pelo nome do campo; equilíbrio negativo, inclusive perto de zero e arredondado a zero, estoque fracionário e barras decorativas; a coluna de estoque vazia (sem estoque, não zero, também na matriz) e o estoque parcial avisado na tela (só com produto sem saldo e com custo) e, só quando falta no parado, na lista; o caixa só do cálculo atual quando ele existe, e do resumo gravado, com a nota, quando a inteligência falha; o empate exato com o custo no simulador; a leitura da tela no log; as mensagens com o exemplo de cada campo; o resumo gravado com a data do envio; resumo com número gigante sem erro 500; lead expirado; lista com triagem, marcas e caixa). Cada uma de 84 mutações do código da Etapa 4 (22 da primeira versão e 62 das correções das revisões) derruba ao menos um teste, menos uma equivalente (a conversão para `Decimal` de um valor que o banco já devolve como `Decimal`);
  - `tests/test_isolamento_rotas.py` (8): a matriz papel × rota de toda a área `/admin` (visitante e cliente, GET e POST com CSRF), a análise e o PDF publicados de outra empresa (com a própria como controle), a varredura de toda rota fora de `/admin` com o id real do lead, conteúdo do cliente (inclusive o Raio-X e a auditoria), o assistente e o ouvinte de SQL, o teste estrutural de todo o projeto e os leads que não se misturam (relatórios iguais, cache ativo, leads sem linhas).

**PostgreSQL real** (cluster temporário; precisa de `NEXO_TEST_POSTGRES_BIN`):
- `tests/test_leads_postgresql.py`, com 33 verificações. Inclui o agendamento, a gravação pelo serviço (com as contagens e os limiares), as CHECKs da Etapa 2 (cada recusa com o nome da restrição) e o downgrade da `7c4e2a9b5d31`: recusado com dados 11.3, aceito sem eles mantendo as linhas antigas, e o upgrade de novo com os padrões do servidor. Da Etapa 3: a data e o resumo gravados pelo serviço, a CHECK da data e o downgrade da `9b3e5d7f1a28` recusado com dados da 11.4 (tudo desfeito na mesma transação) e aceito sem eles, sem perder linha nem lead. Da Etapa 4: as linhas de cada lead carregadas sem ORM só do próprio lead, com os tipos do PostgreSQL, e o diagnóstico do cache sem misturar dois leads com o mesmo envio.
- **Ressalva:** o PostgreSQL local não tem pg_cron. O ensaio usa um esquema `cron` simulado, com `schedule`, `unschedule` e a tabela `job`. Isso prova a migração, mas não o pg_cron real do Supabase.

**Só no Linux:** o worker real, que precisa do módulo `resource`, roda em `ContratoTests.test_worker_real_devolve_o_contrato`. Neste Windows, esse teste é pulado.

**Navegador:** `tests/test_overflow_navegador.py` cobre `/admin/leads` e o detalhe do lead, com a matriz por categoria e categorias de 80 caracteres, sem estouro horizontal nas oito larguras. Na Etapa 4, o lead extenso ganhou estoque de nove dígitos: o resumo executivo, a matriz de triagem, o estoque parado e excedente, o simulador e a auditoria de preço passam pelas mesmas medidas.

## 9. Aplicação (só com autorização)

Ordem: `f4c2a8e1d9b6` → `5e1b9c3a7d20` → `6a8f2d4c0e13` → `7c4e2a9b5d31` → `9b3e5d7f1a28`, que é a `head`. Todas vêm depois da `b3e9d6c1f7a2`.

1. **Backup.** Faça o backup do banco e confira que dá para voltar (`Docs/OPERACAO_BACKUP_RESTAURACAO.md`).
   - Antes de publicar o código, confira no painel do Render que `ETL_MAX_CELLS_PER_SHEET`, se existir, vale no máximo 500000. O teto passou a ser rígido, e um valor acima dele (o teto antigo era 1.000.000) impede o app de subir.
2. **pg_cron.** Opcional, mas recomendado: habilite a extensão no Supabase **antes** da `6a8f2d4c0e13`. Se for habilitada depois, rode `flask leads agendar-expurgo`.
3. **Prévia.** Rode `flask db upgrade b3e9d6c1f7a2:9b3e5d7f1a28 --sql` (ou só as Etapas 2 e 3, se as anteriores já estiverem aplicadas: `flask db upgrade 6a8f2d4c0e13:9b3e5d7f1a28 --sql`).
4. **Aplicação.** Rode `flask db upgrade`.
5. **Conferência.**
   - Estrutura:
     - as colunas novas e as restrições da seção 3;
     - RLS ligado nas duas tabelas;
     - `SELECT jobname, schedule, username FROM cron.job`, com o pg_cron ativo. O `username` deve ser o papel dono das tabelas (`postgres` no Supabase); com outro papel, o job roda sob o RLS e pode apagar zero linhas sem erro.
   - Funcionamento, com uma avaliação de teste:
     - a tela `/admin/leads` faz a avaliação;
     - o detalhe mostra a matriz e os blocos;
     - `/admin/leads/<id>/diagnostico.json` responde com `"situacao": "atual"`.

O código pode ser publicado antes das migrações: a tela mostra o aviso e não lê arquivo. Leads gravados antes da Etapa 2 (regras 11.2) ou da Etapa 3 (regras 11.3) aparecem como "desatualizados" até um novo envio: os blocos e os itens gravados ficam, sem matriz, motivos novos nem inteligência. A avaliação roda no worker isolado do ETL e só funciona no Linux do Render.

## 10. Limitações e pendências do dono

- **LGPD do lead.** Faltam definir:
  - a base legal e o aviso de privacidade para quem ainda não é cliente;
  - o texto da autorização que o consultor confirma;
  - o prazo de retenção do próprio `lead_avaliacao`, já que o expurgo só apaga as linhas de PDV;
  - o atendimento a pedido de exclusão: apagar o lead leva as linhas por CASCADE.

  Nada disso foi inventado aqui. A tela registra só o instante da confirmação (`consentimento_em`) e quem a fez (`registrado_por`).
- **Limiares da triagem.** Os padrão da seção 4.1 são provisórios e precisam de validação comercial.
- **Categoria.** Decisões a confirmar: "Diversos" e "Outros" contam como categoria (só os marcadores claramente vazios, como "SEM GRUPO", valem como ausentes); a precedência entre categoria, departamento, seção, grupo, família e subgrupo; a matriz só sai com categoria em metade do faturamento.
- **Promoção intencional.** A venda abaixo do custo recorrente pode ser promoção planejada. O rótulo pede para confirmar, mas não há como marcar o produto como promoção: o potencial recuperável inclui esses casos.
- **Matriz depois dos 30 dias.** A matriz por categoria e os itens saem com as linhas de PDV. Se o dono quiser a matriz depois da retenção, é preciso gravar um resumo por categoria no lead (com o texto da categoria, que é dado do PDV) e decidir o prazo dele.
- **Compras e perdas.** O lead envia só vendas: o cruzamento com compras e as perdas operacionais pedidos na diretriz dependem de aceitar relatórios de compras e de perdas ou inventário no fluxo do lead.
- **Dashboard do cliente.** O payload é só do consultor. Abrir ao cliente depende da conversão do lead e do recorte por plano (o que cada nível vê). Na Etapa 4, a decisão conservadora foi manter a separação: o lead não tem acesso ao portal, e nenhuma tela do cliente lê as tabelas de lead (há teste). As `visualizacoes` do payload já servem a um dashboard futuro, quando o dono decidir o fluxo de conversão e o recorte.
- **Gráficos.** A tela usa barras simples (Bootstrap) no resumo executivo e tabelas; as séries das `visualizacoes` ficam prontas para uma biblioteca de gráficos interativos, que não foi acrescentada ao portal (decisão do dono, junto com o dashboard do cliente).
- **Cache por processo.** Cada processo do gunicorn tem o próprio cache (até 8 diagnósticos, cerca de 0,1 a 1 MB cada): a primeira leitura em cada processo ainda calcula. Com várias instâncias, ou se a primeira leitura pesar no Render, o próximo passo seria gravar o diagnóstico pronto, com o mesmo critério de chave.
- **Capacidade.** O diagnóstico mediu:
  - XLSX com 36 meses × 5.000 produtos levou 173 s neste computador, acima do prazo de 120 s;
  - CSV com o mesmo volume levou cerca de 35 s;
  - o plano gratuito do Render tem 0,1 CPU, o que pode deixar o processamento perto de 10 vezes mais lento. Nesse cenário, até 36 × 2.000 em CSV passaria do prazo;
  - a saída da 11.3 cabe em cerca de 16.400 linhas produto × ano (seção 2), acima do que o prazo deixa processar. Os indicadores executivos (raio-x-2026-11.6) vão no mesmo resultado e ocupam de 5% (60 meses) a 9% (120 meses) do teto de 4 MiB num envio perto dele: o envio que antes cabia rente ao teto pode passar a ser recusado como `avaliacao_grande`.

  Por isso, recomende enviar por ano ou usar CSV. Medir no Render antes de usar com leads reais. Outras medidas: o detalhe do lead e a rota JSON refazem a carga das linhas, a triagem, a matriz e os itens a cada visita, de 2 a 4 s com 8 a 16 mil linhas em CPU cheia (no Render, de 15 a 25 s para os maiores leads que cabem no prazo do processo isolado); a gravação usa cerca de 90 MB de memória transitória para 24,6 mil linhas; o `validate_input` antes da leitura repete uma passada do XLSX dentro dos 120 s.
- **Produto renomeado entre os anos.** A chave do produto é o nome normalizado. Quando o ERP troca a descrição de um código entre os anos ("CAFE PILAO 500G" passa a "CAFE PILAO TRADICIONAL 500G"), o nome antigo aparece como parado e o novo como lançamento, sem conflito de cadastro: o conflito é apurado dentro de cada relatório, como no Raio-X. Decisão do dono: tratar a troca de descrição de um mesmo código entre relatórios como inconsistência cadastral (código reaproveitado para outro produto ou produto renomeado).
- **Item só com estoque.** O produto listado num ano só com estoque, sem venda, não ganha linha nesse ano: a triagem usa o estoque do último ano em que ele vendeu. O produto que **nunca vendeu** no envio não tem linha e não aparece no estoque parado.
- **Estoque não é inventário.** O estoque da inteligência é o saldo que o relatório de vendas imprimiu; os dias sem venda são um piso (só os dias cobertos por relatório). Conferir com o inventário antes de liquidar.
- **"Há mais de 60 dias".** As faixas contam a partir do número da faixa (60 dias ou mais). Para "mais de 60" no sentido estrito, use as faixas `(61, 91, 181)`.
- **Markup do segmento.** Não há tabela de markup por segmento no sistema, e nenhuma foi inventada: a referência é a da própria categoria do lead (o menor entre o agregado e a mediana) ou o markup que o consultor informar na consulta. Decisão do dono: se o markup do segmento deve vir de uma tabela (fonte, segmentos, revisão), ela precisa ser definida e versionada.
- **Várias lojas.** O relatório com várias lojas é lido como a rede: estoque somado, matriz e auditoria sem separação por loja, com aviso (a variação de preço entre lojas pode ser preço regional). Separar por loja (ou PDV) multiplicaria as linhas da staging pelo número de lojas, perto do teto de 4 MiB do processo isolado e do prazo de 120 s; ficou para decisão do dono, com a medida no Render. Caixas e terminais do mesmo PDV não são lojas.
- **Simulador.** É uma projeção sobre o preço médio praticado, não uma recomendação de desconto: não considera elasticidade, validade nem custo de oportunidade do espaço.
- **Captação pública.** A rota pública ficou de fora. A fase 1 é só para o consultor, e a abertura depende da LGPD, de anti-abuso (captcha e limite por IP) e da capacidade.
- **Conversão do lead em cliente.** O status `convertido` existe e bloqueia o reenvio, mas ainda não existe o fluxo que converte o lead.
- **Envio em lotes.** Mandar um ano por vez e juntar os anos num mesmo lead ficou fora. Hoje, cada envio substitui o anterior.
