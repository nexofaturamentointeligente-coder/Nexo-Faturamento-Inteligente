# Saneamento do motor ETL (Fase 1)

Situação em 08/11/2026: no commit `53020e6`. A Fase 1 não tem migração. A leitura do cadastro da Etapa 2 (seção 11) está pronta e testada, sem commit.

Fase 1 da diretriz de 11/2026: corrigir as falhas numéricas e de parsing legadas sem mudar o relatório de quem não tem esses defeitos. As falhas foram medidas em `Docs/DIAGNOSTICO_MOTOR_ETL.md`.

Como cada correção foi provada:
- **Caso próprio:** cada correção tem teste em `tests/test_etl_saneamento.py`.
- **Golden:** prova que os conjuntos históricos saem idênticos.
- **Revisões independentes, em três rodadas:** compararam o motor antigo (`91e5fb0`) com o novo em cenários feitos para quebrar a regra "relatório sem defeito sai igual". Toda regressão achada foi corrigida e virou teste (seção 9).

## 1. Golden de regressão (`tests/test_raio_x_regression.py`)

Foi escrito **antes** de qualquer mudança no motor. A referência `tests/fixtures/raio_x_regression_expected.json` foi capturada do motor do commit `91e5fb0`, intocado. A captura cobre três saídas:
- o Raio-X (`raio_x.diagnosticar`);
- a análise (`etl_processor._calcular_kpis_local`): KPIs, Curva ABC, alertas e telemetria;
- a leitura bruta.

Os conjuntos capturados são:
- os 22 pares sintéticos do golden SEC-005;
- os relatórios de janeiro e fevereiro do Raio-X;
- um lote de 6 meses no formato de um PDV real, com as compras de cada mês e o primeiro mês também em XLSX. O lote tem:
  - CSV em cp1252 com ";", título e período no topo;
  - acentos, granel e preço com variação;
  - devolução, cancelamento e conflito de código;
  - custo ausente, venda abaixo do custo e estoque negativo;
  - rodapé "TOTAL GERAL".

**Regra.** Todo valor capturado tem de continuar idêntico, inclusive o tipo (`1` ≠ `1.0`). Há só duas exceções, cada uma listada com o motivo:
- `ACRESCIMOS_APROVADOS`: campo novo. Hoje são os cinco campos de CMV líquido da seção 6.
- `VALORES_ALTERADOS_APROVADOS`: valor que muda de propósito. Hoje é só o rótulo `raio_x.*.resultado.versao`, de "raio-x-2026-11.3" para "raio-x-2026-11.4" (seção 6). Nenhum valor de relatório.

Para recapturar, só com motivo documentado e numa cópia sem `.env`, informando o commit do motor que está rodando:

```
python -B -m tests.test_raio_x_regression --capturar --commit <commit>
```

Depois, troque `COMMIT_DE_REFERENCIA` no teste para o mesmo commit. Sem `--commit`, a captura é recusada.

## 2. Rodapés, totais e títulos (antigo `_PADRAO_LIXO`)

**Antes.** `_PADRAO_LIXO` apagava toda linha que tivesse, sem acento e como palavra inteira, `filtro`, `software`, `impresso`, `resumo`, `periodo`, `pagina` ou `usuario`. Com isso:
- caíam produtos legítimos, como "Filtro de Café 103", "Software Antivírus", "Papel Impresso" e "Cigarro Sem Filtro";
- passavam como produto "Total do Dia", "Total Dia 01/03:", "Movimento do Dia" e "Total por Usuário", que somavam no faturamento.

**Agora** (`etl_processor._e_rodape`, usado por `_filtrar_linhas_validas`), em duas partes.

**Parte 1: o filtro de sempre é o piso.** A mesma expressão de antes (`_PADRAO_LIXO_ANTIGO`), aplicada ao texto cru, continua valendo: toda linha que ela tirava continua saindo. A exceção é o nome de produto com "filtro", "software" ou "impresso", os exemplos da diretriz (`_forma_de_produto`), e ela só vale quando tudo isto é verdade:
- **Uma dessas três palavras, uma vez só.** Com `resumo`, `periodo`, `pagina` ou `usuario` sem acento, ou com duas palavras do filtro antigo, mesmo a mesma repetida ("RESUMO NESTE RESUMO"), a linha continua rodapé. "total geral", "totais", "subtotal", "movimenta" e "labsofti" são sempre rodapé.
- **Outra palavra além de preposição.** "FILTRO" e "IMPRESSO NUMA" continuam rodapé.
- **Nenhuma palavra de rodapé.** Nada do vocabulário fechado `_VOCABULARIO_DE_RODAPE` (dia, mês, caixa, operador, loja, total, saldo, valor, vendas, todos, produto, fechamento, admin, gerente…), no singular ou no plural. Por isso "Filtro Loja 01", "FILTRO TODOS", "FILTRO DOS GRUPOS" e "FILTRO PRODUTO DE 000001 ATE 999999" continuam rodapé.
- **Nenhuma forma de rodapé da própria palavra:**
  - "Filtro:" e "Filtros aplicados";
  - "Software:", "pelo software", versão, "Ltda" e endereço web;
  - "Impresso:" e "impresso por", ou data e hora no texto.

Com isso voltam a contar "Filtro de Café 103", "Software Antivírus", "Software XYZ", "Papel Impresso", "Tecido Impresso em 2/3 Cores" e "Cigarro sem Filtro".

**Por que "resumo", "período", "página" e "usuário" ficaram sem resgate.** A primeira versão também resgatava "Resumo de Matemática" e "MANUAL DO USUARIO", mas a verificação adversarial mostrou que essas palavras começam rodapés e títulos de fechamento de PDV demais para um vocabulário fechado separar: "RESUMO DOS CAIXAS", "RESUMO DO FECHAMENTO", "RESUMO TEF", "RESUMO DA REDUCAO Z", "PERIODO ANALISADO", "NOME DO USUARIO", "QUEBRA DE PAGINA". Cada um desses voltaria a somar no faturamento e dobraria o total. Com elas, vale o filtro antigo: "MANUAL DO USUARIO", "PERIODO FERTIL TESTE" e "Resumo de Matemática" saem como sempre saíram. Escritas **com acento** ("Manual do Usuário", "Período Fértil", "Marcador de Página"), nunca foram apagadas, porque o filtro antigo comparava o texto cru, e continuam contando.

**Parte 2: os rodapés que a diretriz manda tirar** (`_e_rodape_explicito`), só em formas inequívocas.
- **No começo do texto:**
  - "Subtotal", "Totais", "Totalizador" e "Total Geral";
  - totais por agrupamento, com ou sem preposição ("Total do Dia", "Total Dia", "TOTAL DE COMPRAS", "Total Março"). A lista completa (`_AGRUPAMENTOS`): dia, diário, data, semana, semanal, quinzena, mês, mensal, trimestre, ano, anual, os nomes dos meses, período, página, pág., folha, turno, grupo, subgrupo, categoria, departamento, depto, seção, setor, família, marca, laboratório, fabricante, vendedor (e flexões), frentista, bico, bomba, loja, caixa, PDV, ECF, operador, usuário, filial, empresa, cliente, fornecedor, relatório, movimento, movimentação, venda(s), compra(s), comprado, entrada(s), itens, produtos, documento, cupom, cupons, NF, NFe, nota(s), anterior, desconto(s) e acréscimo(s);
  - totais por forma de pagamento: dinheiro, cartão, cartões, crédito, débito, PIX, cheque(s), crediário, convênio, vale, ticket, forma(s) de pagamento, a prazo e à vista ("Total Dinheiro", "Total em Cartão", "Total a Prazo");
  - "Valor Total do Dia", "Qtde Total Período" e os totais com adjetivo: líquido, bruto, vendido, faturado, recebido, parcial, acumulado e final;
  - "Total", "Total:" e "Total R$ 1.234,56": só com número depois. "Total 12 Clean Mint" e "Total 100% Integral" são produtos;
  - a abreviação "Tot." ou "Tot" vale como "Total" em todas essas formas ("TOT. GERAL", "TOT DIA", "TOT. CAIXA 01");
  - "Soma", "Soma:" e "Soma 1.234,56": só sozinha, com dois-pontos ou com número depois;
  - "Movimentação", "Movimento do Dia", "Vendas do Dia" e "Saldo Anterior";
  - "A transportar", "Transporte: 1.234,56" e "Transporte 1.234,56" (com dois-pontos ou valor em dinheiro). "Transporte", "Transporte 2" e "Transporte 01" são o serviço vendido e ficam;
  - "Resumo", "Resumo:", "Período: …", "Usuário: JOAO", "Operador: 01", "Filtro: Todos", "Filtros aplicados", "Impresso em 10/03" e "Emitido por";
  - página com o total ou com marcador: "Página 1 de 3", "Pág. 2/5", "Folha 1/3", "Página: 4" e "Página nº 1". "Folha 40" e "FL 2" sozinhos podem ser produto e ficam;
  - "Data: 01/03/2026".
- **Em qualquer ponto do texto:**
  - "Total Geral", "Subtotal" e "Página 2 de 5";
  - "Impresso em 10/03/2026";
  - "Usuário:" e "Operador:" com algo depois ("Caixa: 01 Usuário: JOAO");
  - "Desenvolvido por" e endereço web.
- **Rótulo sozinho.** O dois-pontos do fim sai junto com a decoração das pontas, então "Usuário:" e "Operador:" sem nada depois seguem como antes: o motor antigo também não os tirava, porque comparava o texto com acento e não tinha "operador". Escritos sem acento ("USUARIO:"), saem pela parte 1.
- **Linha que é só data**, com hora, intervalo ou dia da semana ("01/03/2026 - Segunda-feira", "01/03/2026 a 31/03/2026"). Ela é conferida por partes, sem expressão que retroceda. "10/12/14 Conjunto Infantil" é produto.

Regras comuns às duas partes:
- **Comparação.** O texto é comparado sem acento e em minúsculas. A decoração das pontas é retirada antes ("*** TOTAL GERAL ***"). A pontuação de dentro fica, porque é ela que separa "Filtro: Todos" de "Filtro de Café".
- **Teto de 400 caracteres.** As regras novas olham no máximo os primeiros 400 caracteres: rodapé é linha curta, e nenhuma célula grande custa mais do que isso. O filtro antigo, que é linear, continua lendo o texto inteiro.
- **Títulos de seção.** Só sai a linha com chave sem dígito e todas as colunas numéricas vazias ("BEBIDAS;;"). Produto sem venda com "-" ("Açúcar 1kg;-;-") fica, como sempre ficou.
- **Compras.** A coluna-chave (o fornecedor, ou o produto quando não há fornecedor) passa pelas duas partes, como antes passava pelo filtro antigo: "TOTAL DE COMPRAS", "TOTAL COMPRADO" e "TOTAL DAS ENTRADAS" saem do total comprado. Num relatório com fornecedor e produto, a coluna do produto passa só pela parte 2: "Distribuidora A;SUBTOTAL;…" sai, porque somaria a nota duas vezes, e "Filtro de Café" fica. O filtro antigo nunca olhou essa coluna.

**Paridade com o filtro antigo.** Um corpus gerado comparou os dois filtros:

| Corpus | Filtro antigo | Agora |
|---|---|---|
| 58.674 combinações geradas (prefixos × preposições × agrupamentos × sufixos) | tirava 12.474 | tira 39.429, inclusive as 12.474 |
| 93 rodapés reais | tirava 52 | tira 91 ("Transporte" e "FL 2" sozinhos ficam, de propósito) |
| 62 nomes de produto (61 distintos): 20 com palavras do filtro antigo e os demais com palavras parecidas com rodapé ("TOTAL GEL 50G", "CAIXA DE FOSFORO", "SALDO") | apagava 20 | apaga 9 (os sem acento com "resumo", "periodo", "pagina" ou "usuario", como antes) |
| Rodapés e títulos de fechamento do ataque ("RESUMO DOS CAIXAS", "RESUMO TEF", "PERIODO ANALISADO"…) mais 3.654 combinações de plural | tirava todos | tira todos |

Nenhum texto que o filtro antigo tirava volta a somar. Os testes repetem isso num gerador próprio, com mais de 3 mil textos, e conferem que ao menos 15 produtos voltam.

## 3. Quantidades (`_parse_valor`)

Vale só para quantidade e estoque. O dinheiro tem conversor próprio (seção 4).

| Entrada | Antes | Agora |
|---|---|---|
| "1/2", "1/2 kg" | 12 | 0,5 |
| "1 1/2 un(s)" | 112 | 1,5 |
| "(1/2)", "1/2-", "-1/2" | −12, 12, −12 | −0,5 |
| "2-", "150,00-" | 2, 150 | −2, −150 |
| "−2" (menos tipográfico) | 2 | −2 |
| "1E+03" | 103 | 1000 |
| "02/01/2026", "2026-01-02" | 2012026, 20260102 | nulo (data não é quantidade) |
| "1-2" (faixa ou código) | 12 | nulo |
| "1/0" | 10 | nulo |
| "3 CX/12", "1 CX C/12" | 312, 112 | 3, 1 |
| "2 UN/CX", "1,250 KG/UN", "1 P/ KG" | 2, 1,25, 1 | iguais: a barra da unidade não é fração |

Detalhes:
- **Apóstrofo.** O apóstrofo da neutralização de fórmula sai do texto inteiro antes da leitura: o original e a cópia neutralizada pela Baia dão sempre o mesmo número.
- **Fração grande demais.** "1000…0/1", de qualquer tamanho e na forma simples, vira infinito, como os dígitos colados viravam antes, e nunca vira erro interno. No Raio-X e na Avaliação de Cenário, a leitura recusa o arquivo com a mensagem de valor fora do intervalo. Na análise mensal, como antes, o processo isolado recusa o resultado com infinito, com a mensagem genérica.
- **Zeros à esquerda.** Acima de 4.300 dígitos (o limite do `int()` do Python), "000…0/1" vale 0 e "1/000…0" é nulo, e não infinito.
- **Teto.** Fração com unidade ("1/2 KG-") só é lida até 64 caracteres, contados depois de colapsar os espaços.
- **Espaços.** No ramo da barra, os espaços são colapsados antes das expressões, e a célula longa deixa de travar a leitura: 400 espaços levavam 40 s, e 50 mil levam menos de 1 ms. As expressões aceitam qualquer quantidade de espaço ali; o resultado só muda acima do teto de 64 caracteres, em que a fração com unidade e espaços demais passa a caber e ser lida.

## 4. Dinheiro com vírgula e 3 casas

**Antes.** "12,500" era sempre 12,5, sem alerta, mesmo numa coluna en-US ("1,234.56").

**Agora:**
- **CSV com coluna en-US pelo texto.** A coluna conta como en-US quando alguma célula tem ponto decimal ("12.50", "1,234.56") e nenhuma tem vírgula decimal que não possa ser milhar. Nela, "12,500" é milhar (12.500,00), com o alerta `virgula_como_milhar`.
- **XLSX: leitura de sempre.** As células chegam como texto, mas o número de verdade vira "32.9" e não mostra como o relatório escreve os valores. "5,899" numa coluna de XLSX continua 5,899, sem alerta.
- **Demais colunas.** "12,500" continua 12,5. O alerta `virgula_como_decimal` aparece só quando o arquivo tem alguma coluna en-US: num relatório todo brasileiro ("31,125" de custo), nada muda e nada é avisado.
- **Ponto com 3 casas** ("12.500"). A decisão continua exatamente a de antes.
- **Célula longa.** A expressão que confere o número (`_RE_DECIMAL`) aceita a mesma linguagem de sempre, escrita sem retrocesso quadrático: uma célula com 16 mil dígitos levava 3 s (já no motor antigo) e agora leva milissegundos.

## 5. Títulos de coluna comuns de ERP (reserva)

O diagnóstico mediu títulos comuns que o dicionário não reconhece. Eles foram acrescentados **só como reserva** (`RESERVA_VENDAS` e `RESERVA_COMPRAS` em `etl_processor.py`):

| Relatório | Canônico | Títulos de reserva (inteiros, já normalizados) |
|---|---|---|
| Vendas | quantidade | "Quant.", "Quant. Vendida", "Quant. Vend." |
| Vendas | valor de venda | "Vlr. Total", "Vl. Total", "Valor Líquido", "Vlr. Líquido", "Vl. Líquido", "Total", "Total R$" |
| Vendas | preço unitário | "Vr. Unit.", "Vlr. Unit.", "Vl. Unit.", "Vr. Unitário", "Vl. Unitário" |
| Vendas | custo | "CMV", "CMV Total" |
| Compras | valor | "Custo Total", "Vlr. Total", "Vl. Total", "Total", "Total R$" |
| Compras | custo unitário | "Vr. Unit.", "Vlr. Unit.", "Vl. Unit.", "Vl. Unitário" |
| Compras | quantidade comprada | "Quantidade", "Qtd", "Quant." |

Garantias:
- **Escolha da linha de cabeçalho.** A linha é escolhida só com o dicionário principal (linha simples e depois composta), como sempre. A reserva completa a linha escolhida e só entra na busca quando nada valida sem ela. Assim, um cabeçalho em duas linhas que o dicionário já lia ("Total" sobre "Líquido") não é trocado pela primeira linha sozinha.
- **Só o que falta.** A reserva só preenche o canônico que ficou sem coluna, e só com coluna que nenhum fragmento do dicionário reconhece.
- **Título inteiro.** A comparação é pelo título inteiro: "Quantia" não vira quantidade, e "Vlr Total Desconto" não vira venda.
- **Percentual fora.** Coluna com "%" no título ("% Total", "Total (%)", "% CMV") nunca entra pela reserva: a normalização a transformaria em "total" ou "cmv".
- **Título repetido fora.** Título de reserva repetido na mesma linha é ambíguo e não vale. Em "Quant." sobre "Pedida" e "Quant." sobre "Vendida", a linha simples não escolhe nenhuma das duas; o cabeçalho em duas linhas lê "Quant. Vendida" como a quantidade.
- **Mesma regra na Baia.** A Baia de Segurança usa a mesma detecção, então nada que o processamento aceita é recusado nela (há teste).

Ficaram de fora, por ambiguidade:

| Título | Motivo |
|---|---|
| "Valor" | pode ser o unitário |
| "Valor Bruto" | pode ser o valor sem desconto |
| "Receita", "Vendas" | — |
| "Un" | costuma ser a unidade de medida ("UN", "KG") |
| "Cód.", "ID" | o código não é usado no agrupamento |

Os falsos positivos por fragmento do dicionário principal ficaram como estão, porque corrigi-los mudaria o mapeamento de relatórios que já são lidos.

## 6. Compras líquidas, CMV e margem

**Análise** (`_consolidar_compras`, em `carregar_compras`). Só as marcas explícitas da coluna de situação da nota ("Status", "Situação", "Natureza da Operação"…) mudam alguma coisa, e cada uma tem forma conservadora:

| Marca | Efeito |
|---|---|
| NF cancelada, estornada ou inutilizada | sai do total comprado (pedido da diretriz) |
| Devolução **ao fornecedor**, com valor ou quantidade: "Devolução de compra", "Devolução ao (à, para o) fornecedor" ou o CFOP de devolução de compra escrito com a palavra CFOP ("CFOP 5.202"; também 5/6.201, 5/6.410 e 5/6.411) | desconta valor e quantidade |
| Bonificação (BONIFIC, sem "com" ou "c/" logo antes) numa linha que não diz "compra" nem "venda" | entra só na quantidade: a mercadoria chegou sem pagamento |

Cada marca só vale como fato da própria nota (`_marca_da_nota`). Seguem a regra de sempre:
- a marca com negação antes dela, em qualquer ponto do texto ("não", "sem", "nenhum", "nenhuma", "nunca"): "Não houve devolução ao fornecedor", "Nota não foi cancelada";
- a marca desfeita nas duas palavras seguintes (rejeitado, negado, recusado, indeferido, pendente, solicitado, aguardando, em análise, em andamento, parcial): "Cancelamento rejeitado", "Autorizada - cancelamento rejeitado", "Devolução ao fornecedor pendente";
- o cancelamento de outra coisa, dito na palavra logo antes (pagamento, título, boleto, parcela, duplicata, cobrança, saldo, pedido, item, frete, entrega, reserva, orçamento, cotação): "Pagamento estornado", "Atendido parcial - saldo cancelado".

Sem marca, vale a regra de sempre: entra a linha com valor positivo, e saem o negativo, o zero, o vazio e o inválido. Também seguem como sempre:
- a "devolução de venda", que é o cliente devolvendo à loja;
- o status "Devolvida" ou "Devolvida ao fornecedor" da NF original;
- a "troca" e o número de operação solto ("5202");
- "Compra com bonificação", "Compra c/ bonificação", "Compra bonificada" e, na natureza escrita pelo fornecedor, "Venda e bonificação", que são notas pagas;
- a coluna de situação do pedido, do orçamento ou da cotação: "saldo cancelado" no pedido não cancela a NF.

A telemetria ganha `compras_consolidacao`, e o alerta "Compras líquidas: ..." aparece só quando houve algum desses casos.

**Raio-X.** A auditoria ganha cinco campos novos:
- `custo_bonificacoes`: custo do item vendido com valor zero **informado** ("0,00": brinde ou bonificação). A célula em branco continua só como "venda sem valor", sem entrar no CMV;
- `custo_devolucoes`: custo das devoluções com custo, que voltaram ao estoque (o custo vale em módulo: "-4,00" desconta 4,00);
- `cmv_liquido`: custo total + bonificações − devoluções;
- `margem_liquida_valor`;
- `margem_liquida_percentual`: sobre a receita líquida das vendas com custo.

Os campos de sempre (`custo_total` e `margem_valor`) não mudam. O recorte por plano segue o dos campos irmãos:
- a margem líquida some sem a liberação de margem;
- o CMV líquido some sem `diagnostico_cmv`.

Na tela de auditoria, os valores líquidos aparecem embaixo dos de sempre, só quando há bonificação ou devolução.

**Versão das regras.** `raio_x.VERSAO_REGRAS` passou de "raio-x-2026-11.3" para "raio-x-2026-11.4". Ela separa o Raio-X gravado antes do saneamento do gravado depois dele. O Raio-X antigo continua abrindo: `raio_x_servico.visao` entrega os campos novos como nulos, com a mesma forma do que o plano não libera, e a tela mostra só os campos de sempre. Antes dessa correção, a auditoria de um Raio-X antigo dava erro 500.

**Decisão.** A análise mensal continua sem margem, lucro e CMV. É regra do produto (docstring do `etl_processor` e terminologia oficial NEXO/PI2): o CMV líquido entrou só no Raio-X, que já tinha margem por plano.

## 7. Codificação (UTF-8, cp1252 e latin1)

**Antes.** Um CSV em UTF-8 com um único byte de Windows-1252 colado de outro sistema não decodificava como UTF-8 e caía inteiro para cp1252, em silêncio. Os acentos certos viravam "Ã©": "Café" de um mês e "CafÃ©" de outro viravam dois produtos.

**Agora** (`etl_inputs._reparar_codificacao`):
- **Célula a célula.** Quando o arquivo cai para cp1252 ou latin1, cada sequência UTF-8 desfeita é refeita em cada célula ("CafÃ©" volta a ser "Café"). A decisão não depende da proporção do arquivo: o mesmo nome sai igual em todos os meses de um envio, mesmo no mês que tem mais acentos de Windows-1252 soltos do que sequências UTF-8.
- **Latin1.** O UTF-8 com um byte que o cp1252 não define ("Á" é C3 81, e 0x81 não existe no cp1252) cai para latin1 e também é refeito.
- **Só caractere plausível.** O reparo só aceita caractere típico de texto em português: letras Latin-1 e a pontuação do Windows-1252. Num CSV cp1252 legítimo, "CAFÉ" + NBSP, "NESCAFÉ®" e "MAÇÃ" + NBSP ficam como estão. "Ã" + NBSP depois de letra daria "à" no meio da palavra e também fica.
- **Todas as células.** O reparo vale para o título, o período e o cabeçalho, e não só para os nomes de produto. Num CSV desses, "PerÃ­odo:", "CÃ³digo" e "DescriÃ§Ã£o" voltam a ser "Período:", "Código" e "Descrição": o período, a coluna de código e a variação de preço passam a ser reconhecidos, e o arquivo cujo cabeçalho antes não era lido (recusado por falta da coluna do produto) passa a ser aceito.
- **O resto fica.** O byte estranho fica como o cp1252 o lê, e o BOM lido como texto fica intacto.
- **Aviso.** O número de células corrigidas vai para a telemetria (`celulas_recodificadas`), com alerta na análise e aviso no Raio-X.

## 8. O que muda para cliente atual

Um relatório sem nenhum dos casos abaixo sai idêntico: o golden prova isso nos conjuntos históricos, e as revisões, em cenários feitos para quebrar a regra. Reprocessar um relatório que tem um desses casos muda o resultado de propósito:

| Caso no relatório | Efeito |
|---|---|
| Produto com "filtro", "software" ou "impresso" no nome, sem nada de rodapé por perto | volta a contar |
| "Total do Dia", "Movimento do Dia", "Total por Usuário", "Total Dinheiro", "Total Descontos", "Tot. Geral", "Soma" e afins | sai do faturamento |
| Compras com "Total de Compras", "Total Comprado" ou "Total das Entradas" na coluna do fornecedor | sai do total comprado |
| Título de seção sem número ("BEBIDAS;;") | sai da Curva ABC (antes aparecia com R$ 0) |
| Quantidade com fração, sinal no fim, data, faixa, notação científica ou "3 CX/12" | número correto (ou nulo) |
| CSV com coluna en-US pelo texto e "12,500" | 12.500,00, com alerta |
| Coluna com título de reserva | passa a ser lida; "Total" ao lado de "Preço Unit." vira o total explícito, que vence o unitário × quantidade |
| Compras com NF cancelada, devolução ao fornecedor ou bonificação marcadas | total comprado líquido e alerta novo |
| CSV UTF-8 com byte de outra codificação | um produto só e alerta novo; título, período e cabeçalho também reparados (período, código e variação de preço reconhecidos; o arquivo cujo cabeçalho não era lido passa a ser aceito) |
| Raio-X | cinco campos novos de CMV líquido e a versão "raio-x-2026-11.4"; os campos antigos ficam iguais |

Análises e Raio-X já gravados não são recalculados: o banco guarda o resultado da época.

## 9. Revisões independentes e o que ficou de fora

**Primeira rodada.** As revisões acharam regressões contra o `91e5fb0` na primeira versão do saneamento, todas corrigidas e cobertas por teste:
- **Rodapés:** rodapés sem acento ("TOTAL PERIODO") que o filtro antigo tirava voltavam a somar.
- **Compras:**
  - "devolução de venda", "devolvida" e "troca" negavam a NF;
  - valor vazio virava bonificação.
- **Cabeçalho:** o cabeçalho em duas linhas era trocado pela reserva.
- **Codificação:** o reparo corrompia cp1252 legítimo ("CAFÉ" + NBSP).
- **Quantidades:** "2 UN/CX" virava nulo, e fração gigante gerava exceção.
- **Dinheiro:** surgia alerta novo em relatório brasileiro com 3 casas, e célula numérica do XLSX contava como prova de formato en-US.
- **Raio-X:** produto sem venda com "-" sumia, e o CMV líquido não aparecia na tela.

**Segunda rodada** (revisão por lentes e ataque com cenários rodados nos dois motores). Também tudo corrigido e coberto por teste:
- **Rodapés:**
  - a exceção de produto era aberta demais: 1.052 textos que o filtro antigo tirava voltavam a somar ("FATURAMENTO DO USUARIO", "RESUMO DO DIA"). Ela virou o vocabulário fechado da seção 2;
  - a mesma palavra do filtro repetida ("RESUMO NESTE RESUMO") passava por produto. Agora conta como duas.
- **Desempenho:**
  - a expressão de data retrocedia de forma exponencial e foi trocada pela conferência por partes, com o teto de 400 caracteres;
  - a expressão da unidade com barra retrocedia de forma polinomial e passou a receber os espaços colapsados.
- **Dinheiro:**
  - a regra en-US não funcionava no XLSX real e passou a valer só para CSV;
  - uma mudança no contador do ponto alteraria um alerta antigo e foi desfeita.
- **Compras:** textos reais de ERP, como "Compra com bonificação", "Devolvida ao fornecedor", o número de operação solto e o status do pedido, mudavam o total. As marcas ficaram com a forma conservadora da seção 6.
- **Codificação:** a decisão pela proporção do arquivo deixava o mesmo produto com dois nomes entre os meses de um envio. Passou a ser célula a célula.
- **Raio-X:** a auditoria de um Raio-X gravado antes do saneamento dava erro 500.
- **Quantidades:** a fração gigante acima do teto voltava nula. Voltou a ser infinito, como antes.

**Terceira rodada** (teste de mutação dos testes novos, documentos contra o código e classificação, caso a caso, das diferenças entre os dois motores nos cenários dos revisores). Corrigido e coberto por teste:
- **Compras:**
  - "Não houve devolução ao fornecedor" descontava a nota como devolução (a negação só era vista colada na marca). Agora a negação em qualquer ponto antes da marca desfaz as três marcas;
  - a natureza "VENDA E BONIFICACAO" (ou "VENDA/BONIFICACAO"), escrita pelo fornecedor, zerava uma nota paga, inclusive item por item. "Venda" passou a valer como "compra";
  - "Cancelamento rejeitado", "Autorizada - cancelamento rejeitado", "Pagamento estornado" e "saldo cancelado" (numa coluna só "Status") tiravam do total uma nota válida. A marca passou a valer só como fato da própria nota (seção 6).
- **Rodapés:**
  - "TRANSPORTE 2" e "TRANSPORTE 01" (o serviço vendido, com código e quantidade, e dentro do total do próprio PDV) saíam como acumulado da página. O transporte passou a exigir dois-pontos ou valor em dinheiro;
  - "FOLHA 40", produto nas compras, saía como número de página. A página passou a exigir o total ou um marcador;
  - o ataque achou rodapés que o filtro antigo tirava e voltavam a somar, dobrando o faturamento: plurais fora do vocabulário ("RESUMO DOS CAIXAS", "RESUMO DOS DIAS") e títulos de fechamento ("RESUMO DO FECHAMENTO", "RESUMO TEF", "PERIODO ANALISADO", "NOME DO USUARIO", "QUEBRA DE PAGINA", "FILTRO TODOS"). O resgate de produto ficou só com "filtro", "software" e "impresso", o vocabulário passou a valer no plural e as preposições que faltavam ("nesse", "pelos", "ao", "até") entraram.
- **Desempenho e bordas:** a célula de dinheiro com 16 mil dígitos levava 3 s (a expressão já existia no motor antigo, e o saneamento a chamava duas vezes); a fração com mais de 4.300 dígitos virava infinito mesmo valendo 0.
- **Cabeçalho:** com dois "Quant." sobre "Pedida" e "Vendida", a reserva lia a quantidade pedida. Título de reserva repetido deixou de valer.
- **Testes que não pegavam a regra:** nenhum teste quebrava sem o teto de 400 caracteres dos rodapés (sem ele, 16 mil dígitos depois de "transporte" levam 20 s) nem sem o expurgo no detalhe do lead. Os dois testes foram reforçados.
- **Documento:** a lista completa dos totais da parte 2 ("Soma", "Tot.", compras, entradas, descontos, laboratório, nomes dos meses), o rótulo sozinho ("Usuário:"), o que o filtro antigo comparava com acento e o reparo de codificação no título e no cabeçalho.

**Fora desta fase.** Medido no diagnóstico e não alterado aqui:
- formatos de período além de "dd/mm/aaaa";
- o código do produto ignorado no agrupamento;
- o KPI do segundo colocado;
- insights com faturamento ≤ 0;
- a mensagem que o worker mascara;
- os falsos positivos por fragmento do dicionário principal (seção 5);
- "Transporte", "Transporte 2", "Folha 40" e "FL 2", que seguem como produto, como no motor antigo;
- "Usuário:" e "Operador:" sozinhos, com acento e sem nada depois, que seguem como antes (seção 2);
- a devolução de venda dentro do relatório de compras, que continua somando como antes;
- o alerta de codificação na cópia neutralizada da Baia. A cópia já é gravada com os acentos corrigidos, então a análise feita sobre ela não repete o alerta.

## 10. Testes

- `tests/test_raio_x_regression.py` (4): Raio-X, análise e leitura bruta idênticos, e o lote exercita o que promete.
- `tests/test_etl_saneamento.py`:
  - **Rodapés:** paridade com o filtro antigo (inclusive a palavra repetida e "neste/desta"), produtos resgatados, linha longa sem travar (com o teto e os espaços colapsados), agrupado por dia e coluna do produto nas compras.
  - **Quantidades:** unidade com barra, fração com sinal, fração gigante e célula longa com espaços.
  - **Dinheiro:** vírgula com 3 casas, inclusive XLSX real, XLSX numérico, valor único e arquivo misto.
  - **Títulos de reserva:** cabeçalho em duas linhas, título repetido, percentual e a Baia.
  - **Compras líquidas:** marcas explícitas, textos de ERP (inclusive negação longe da marca e "Venda e bonificação"), devolução de venda, CFOP, duas colunas de situação e valor vazio.
  - **CMV líquido:** valor em branco, devolução com custo negativo, recorte por plano e Raio-X gravado antes do saneamento.
  - **Codificação:** cp1252 legítimo, caminho latin1, o mesmo produto nos meses de um envio e a cópia da Baia.
- `tests/test_cadastro_regression.py` (3): o golden complementar da seção 11.
- `tests/test_planos_raio_x.py`: a auditoria de um Raio-X do `91e5fb0` abre (página 200, CMV líquido nulo na API) e o Raio-X novo grava a versão 11.4.
- Seguem passando os testes de equivalência financeira, regras financeiras, Raio-X e planos, Baia, trava do PDV, upload e limites, fora as falhas já conhecidas deste Windows (o worker real, que exige Linux, e um teste de limite de registro do CSV que depende da quebra de linha).

## 11. Leitura do cadastro (Etapa 2 da Avaliação de Cenário)

A Avaliação de Cenário do lead passou a ler NCM, categoria e estoque dos relatórios de vendas (`Docs/AVALIACAO_DE_CENARIO.md`, seção 2). A leitura fica atrás de um parâmetro que só o motor do lead liga:
- `etl_processor._vendas_do_bruto(bruto, nome, *, cadastro=False)` e `raio_x._ler_arquivo` e `_ler_envio` com `cadastro=False` por padrão. Com `cadastro=True`, o df ganha as colunas `_ncm`, `_categoria` e `_estoque_informado` (prefixo "_", fora das colunas do relatório) e `attrs["cadastro"]`;
- a leitura nova não acrescenta nada ao dicionário de colunas de vendas (`ALIAS_VENDAS`). A coluna de NCM ou de grupo só vira código ou produto quando o próprio `ALIAS_VENDAS` já a escolhia, antes e depois desta etapa ("Cód. NCM" antes de "Código"): é o cabeçalho ambíguo, marcado em `attrs["cadastro"]["ambiguo"]` e coberto pelo golden;
- `normalizar_ncm` e `normalizar_categoria` são públicas e puras.

**Golden complementar** (`tests/test_cadastro_regression.py`). Antes da mudança, o Raio-X e a análise mensal foram capturados do motor do commit `53020e6` em relatórios com NCM (em texto, com pontos, vazio, só zeros e numérico no XLSX), Grupo, Departamento, Seção e Família, situação (devolução e cancelamento), bonificação, devolução, estoque negativo e cabeçalhos ambíguos ("Cód. NCM" antes de "Código", "Grupo do Produto" antes da descrição). A referência está em `tests/fixtures/cadastro_regression_expected.json` (14 casos) e continua idêntica com a leitura nova: o Raio-X do cliente e a análise mensal não mudam. Recaptura só com motivo documentado: `python -B -m tests.test_cadastro_regression --capturar --commit <commit>`.
