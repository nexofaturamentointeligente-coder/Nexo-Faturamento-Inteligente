# Auditoria de cobertura do motor ETL contra relatórios reais de um ERP

Situação em 10/11/2026: estudo concluído sobre o commit `464de3f`. **Nenhuma linha do motor foi alterada.** Entraram no repositório este documento, o anexo (`Docs/AUDITORIA_COBERTURA_MOTOR_ETL_ANEXOS.md`) e um módulo de testes com fixtures fictícias (seção 10). Nada foi commitado, publicado nem aplicado em banco.

**Atualização de 10/11/2026: a unificação do motor.** Depois deste estudo, a diretriz de unificação implementou parte da proposta da seção 9, e o que está descrito aqui como "hoje" é o motor de antes dela. O relato está em `Docs/UNIFICACAO_MOTOR_ETL.md`. Em resumo: as proteções da fase 1 (seção 5.4), a leitura do `.xls` e do leiaute paginado da fase 2 e, da fase 3, a data da venda e as fatias mensais na Avaliação de Cenário. A venda item a item real passou a ser lida do `.xls` original, com R$ 710.718,23. Ficaram de fora a conferência da contagem por documento, a ligação com o relatório por produto e as fases 4 e 5. Os testes da seção 10 foram reescritos: as oito lacunas passam e as recusas que deixaram de existir viraram testes do comportamento novo.

Pedido: ler relatórios reais de 36 meses do ERP de uma loja de varejo, listar todos os indicadores possíveis (Passo 1), comparar com o que o motor faz hoje (Passo 2) e propor a expansão do código dentro dos limites do processo isolado, que são 384 MB de memória e 120 segundos (Passo 3).

## Resumo

**O que foi lido.** Oito relatórios "Giro de Vendas", em PDF (802 páginas) e na planilha `.xls` do mesmo relatório. Todos são de vendas: não vieram relatórios de compras, de posição de estoque nem de perdas.

**Os cinco achados que mais pesam:**

1. **O motor não lê nenhum dos oito arquivos como vieram.** PDF e `.xls` são recusados na entrada. Convertidos para `.xlsx` ou `.csv`, só o relatório resumido por produto é lido, e é lido certo.
2. **O único relatório que o motor lê traz o faturamento inflado em cerca de duas vezes.** Os relatórios resumidos do ERP contam o pedido e cada nota fiscal como vendas separadas: somam R$ 1,46 milhão, contra R$ 710.718,23 da venda item a item. O motor não tem como perceber, porque o arquivo é coerente com ele mesmo.
3. **O relatório mais rico, a venda item a item, não é lido de jeito nenhum.** Ele é o único com data, pedido, nota, vendedor e cliente. Passa do teto de células, tem o título de cada coluna numa coluna e o dado em outra, e o motor não tem campo para nenhuma dessas informações.
4. **O motor aceita alguns arquivos e devolve número errado sem avisar** (seção 5.4). O mais grave: o relatório de vendas abaixo do custo, lido com o valor de compra no lugar do preço de venda.
5. **Dos 237 indicadores catalogados nas duas primeiras frentes (comercial e margem), o motor calcula 14 e chega perto em 37.** Os outros 186 dependem de ler a venda item a item e de dimensões que hoje não existem no motor: data da venda, cliente, vendedor, documento, forma de pagamento e desconto.

**O caminho proposto**, em uma frase: ler o `.xls` direto no processo isolado (custa de 1 a 2 segundos e pouca memória), transformar o relatório paginado numa tabela de itens de venda, fatiar por mês em memória e só então entregar ao motor de hoje, com conferências que barram a contagem em dobro, o item genérico e o custo implausível. A seção 9 traz as fases.

**O que depende de você:** aceitar ou não o formato `.xls` (hoje a Baia de Segurança o recusa por decisão de produto), e pedir ao ERP os relatórios que faltam (seção 11).

**O que não ficou provado** está na seção 12. Os dois pontos principais: os tempos foram medidos neste computador e projetados para o Render com uma regra de multiplicar por 10, que é estimativa; e a proposta da seção 9 não passou pela revisão independente que estava planejada, porque o limite de uso da conta interrompeu os agentes de revisão.

## 1. O que foi lido e como

**Os arquivos.** Oito relatórios "Giro de Vendas" do ERP de uma loja de varejo, todos com o período `09/11/23 á 09/11/26` (36 meses num arquivo só) e uma filial. Cada relatório veio em duas formas: o PDF que foi anexado e a planilha `.xls` do mesmo relatório, que estava na pasta vizinha.

| Relatório | Páginas do PDF | Linhas da planilha | O que traz |
|---|---|---|---|
| Por cliente, detalhado por produto | 741 | 26.930 | a venda item a item: data, vendedor, pedido e nota, produto, valor unitário, quantidade e valor total, em blocos por cliente |
| Resumido por produto | 42 | 2.051 | por produto: código, nome, quantidade, venda, custo, "margem de lucro %", lucro e estoque atual |
| Vendas abaixo do custo | 4 | 130 | produtos vendidos abaixo do preço de custo |
| Resumido por grupo de produto | 5 | 234 | por grupo: quantidade, venda e lucro |
| Resumido por fornecedor | 4 | 152 | por fornecedor: valor vendido |
| Resumido por forma de pagamento | 2 | 67 | por forma de pagamento: valor vendido |
| Resumido por grupo de cliente | 2 | 61 | por grupo de cliente: valor vendido e lucro |
| Ranking de clientes | 2 | 60 | por cliente: código, nome, CPF ou CNPJ, telefones, e-mail e total (veio com o período de **um dia**) |
| **Total** | **802** | **29.685** | |

**Só há relatórios de vendas.** O pedido cita Vendas, Compras, Estoque Atual e Perdas/Ajustes. Na pasta não há relatório de compras, de posição de estoque nem de perdas ou ajustes. O estoque aparece só como uma coluna do relatório por produto. A seção 5.5 diz o que o motor faria com esses três tipos, e a seção 11 lista o que pedir ao ERP.

**Os PDFs são a impressão das planilhas.** O metadado dos oito PDFs traz "Google Sheets" como programa de origem: alguém abriu cada `.xls` no Google Planilhas e imprimiu em PDF. Por isso o PDF tem os mesmos registros da planilha, com menos precisão (valores arredondados para a tela) e com defeitos de impressão: campos sobrepostos no cabeçalho, palavras coladas e, num dos modos de extração de texto, valores na linha errada. A fonte fiel é o `.xls`. Os dois foram lidos por inteiro e confrontados: no relatório detalhado, 21.172 itens nas duas fontes, na mesma ordem, com data, vendedor, pedido, nota e valor unitário iguais em todos; as diferenças são só de arredondamento e de espaços.

**Como foi conferido.**

- Cada relatório foi lido por script, cobrindo 100% das linhas: toda linha recebeu um tipo (dado, cabeçalho, filtro, rodapé, total) e nenhuma ficou sem classificação.
- O relatório detalhado foi lido por dois leitores independentes, um pelo PDF e outro pela planilha, e um terceiro refez as contas dos dois.
- As somas dos itens fecham com os rodapés do próprio relatório: no detalhado, a soma de cada um dos 294 blocos de cliente é igual ao rodapé do bloco, e a soma geral é igual ao total do relatório.
- O motor do NEXO foi executado de verdade sobre os arquivos, numa cópia do repositório sem o `.env`, pelo mesmo caminho que o processo isolado usa.
- Cada afirmação sobre o código foi conferida no arquivo e na linha, e as de comportamento foram reproduzidas por execução.

**Sigilo.** Os relatórios trazem dados pessoais de terceiros (nome, CPF ou CNPJ, telefone e e-mail de clientes da loja). Nenhum dado real foi copiado para o repositório. Este documento não traz nome de cliente, de loja nem de fornecedor; traz somas e contagens da loja, sem identificá-la. O material de trabalho (textos extraídos, tabelas e scripts) ficou numa pasta temporária fora do repositório.

## 2. Passo 1: o que há em cada relatório

Todos os relatórios têm o mesmo desenho de página do ERP, que a planilha reproduz como uma grade de posicionamento, e não como uma tabela:

- um título, a data e a hora de emissão e a paginação;
- a linha `Período de  : dd/mm/aa á dd/mm/aa - Andamento: Todas vendas` (ano com dois dígitos e `á` com acento agudo);
- oito linhas de filtro (`Produtos ....:  -Todos  até  -Todos` e as de clientes, vendedores, grupos, fornecedores e filiais);
- em alguns, um marcador do modelo do relatório que ficou visível (`L_FiltroProdutoXXXX...`, `Label60`);
- o cabeçalho das colunas, em uma ou em duas linhas, **repetido a cada página**;
- as linhas de dado, com uma linha em branco entre uma e outra;
- o rodapé do fabricante do ERP ao fim de cada página;
- no fim, os totais, o aviso `Movimentação produto: "PRODUTO GENERICO" ...`, o total de fretes e o aviso `Atenção .: Total das vendas não esta considerando "produto genérico".`

Na planilha, os números são números (o negativo só aparece entre parênteses na tela), as datas são datas e os textos estão em UTF-16. As células ficam espalhadas por 19 colunas estreitas, unidas por milhares de mesclas, e **em 7 dos 8 relatórios o título de uma coluna não fica na mesma coluna do dado**.

### 2.1 Venda detalhada (item a item)

É o único relatório com data, documento, vendedor e cliente. Não traz código de produto, custo, grupo nem fornecedor.

| Coluna (título real) | O que é | Como vem na planilha |
|---|---|---|
| Data | data da venda | data de verdade, exibida `dd/mm/aa`; 903 dias distintos |
| Ven. | código do vendedor, sem o nome | número; 8 códigos |
| Nr. Doc. | o pedido e a nota fiscal **na mesma célula**: `P2-47259  NFC2-34448` | texto; 10.808 pedidos, 10.337 NFC-e e 163 notas de outra série; 583 itens sem nota e 61 com duas |
| Produto | descrição do produto | texto; 904 descrições; 413 têm vírgula dentro (`0,9L`) |
| Valor Unit. | preço unitário já líquido do desconto: o total dividido pela quantidade, truncado em 2 casas | número com 2 casas |
| Qtde | quantidade | número; a tela mostra 1 casa, a planilha guarda até 3 |
| Valor Total | valor líquido da linha | número com até 4 casas |
| `*** Cliente :` | nome do cliente dos itens abaixo | linha de bloco, não é coluna; 294 blocos |
| `Descontos >>>` | descontos do cliente no período | rodapé do bloco |
| `Valor Total para este cliente >>>` | quantidade e valor do bloco | rodapé do bloco |
| Total geral, fretes e produto genérico | fechamento do relatório | linhas finais |

A aba tem 26.930 linhas, das quais 21.172 são itens. As outras 5.758 são linhas em branco (3.011), cabeçalhos repetidos (1.992), rodapés de cliente (294), rodapés de página (447) e título, filtros e totais (14).

### 2.2 Resumido por produto

| Coluna (título real) | O que é | Como vem na planilha |
|---|---|---|
| Codigo | código do produto | texto de 7 dígitos, com zero à esquerda |
| Nome do produto | descrição | texto; 912 nomes, nenhum repetido |
| Qtde Vend | quantidade vendida no período | número com até 3 casas |
| Total Venda | venda líquida de desconto | número com 2 casas |
| Total Custo | custo das vendas que têm custo apurado | número; vazio em 3 linhas e zero em 11 |
| Marg. de Lucro % | **markup sobre o custo** (lucro dividido pelo custo), e não margem sobre a venda | número, com o `%` numa célula separada |
| Lucro | venda das linhas com custo menos o custo | número; 11 negativos |
| Estoque Atual | saldo na data de emissão | número; 346 negativos |
| `Totais: Descontos:` | somas do relatório e o desconto total | linha final |

O cabeçalho vem em duas linhas ("Qtde" em cima e "Vend" embaixo; "Total" sobre "Venda"; "Total" sobre "Custo") e se repete 25 vezes.

### 2.3 Vendas abaixo do custo

| Coluna (título real) | O que é | Como vem na planilha |
|---|---|---|
| Cód. Produto | o mesmo código do relatório por produto, em outra máscara (`000001234.56` equivale a `0123456`) | texto |
| Descrição Produto | descrição | texto |
| Qtde Vendida | quantidade das linhas vendidas abaixo do custo | número |
| Valor Unitário Compra | **soma** dos custos unitários dessas linhas, sem multiplicar pela quantidade | número com até 4 casas |
| Valor Unitário Vendido | **soma** dos preços unitários dessas linhas | número com até 7 casas |
| Diferença | uma soma menos a outra | número |
| `Totais Gerais >>>` | soma das quatro colunas | linha final |

Os dois títulos "Valor Unitário" são iguais na primeira linha do cabeçalho; só a segunda linha ("Compra" e "Vendido") os distingue.

### 2.4 Os cinco relatórios resumidos por dimensão

| Relatório | Colunas (títulos reais) | Linhas de dado |
|---|---|---|
| Por grupo de produto | Nome do grupo, Qtde venda, Valor venda, Lucro | 98 grupos |
| Por fornecedor | Fornecedor, Valor Total (é a venda dos produtos do fornecedor, não a compra) | 56 fornecedores |
| Por forma de pagamento | Forma de pagamento, Valor Venda | 11 formas |
| Por grupo de cliente | Grupo de cliente, Valor vendido, Lucro valor | 2 grupos |
| Ranking de clientes | Cod., Cliente, CPF/CNPJ, Fone 1, Fone 2, E-Mail, Total Vr Liq | 2 clientes (um dia) |

Nenhum desses cinco traz produto nem data. Quatro deles não trazem quantidade.

## 3. Passo 1: o que os dados dizem antes de qualquer indicador

Oito fatos mudam o sentido dos números. Um indicador calculado sem levá-los em conta sai errado.

**1. Os relatórios resumidos contam o pedido e cada nota fiscal como vendas separadas.** O relatório detalhado soma **R$ 710.718,23** nos 36 meses, e esse valor fecha com os rodapés de cada cliente. Os resumidos por produto, por grupo e por fornecedor somam **R$ 1,46 milhão** (de R$ 1.460.035,77 a R$ 1.460.039,33), isto é, 2,05 vezes mais; o de forma de pagamento soma R$ 1.405.825,95. A quantidade passa de 30.434,7 para 61.626,3. A prova está no ranking de clientes, que veio com um dia só: o total de cada cliente naquele dia é exatamente "duas vezes cada venda com nota, mais uma vez a venda sem nota". Em 669 dos 904 produtos a quantidade do resumo é exatamente o dobro. Como o fator varia de 1 a mais de 3 conforme o produto, **não dá para corrigir dividindo por dois**. As proporções (preço médio, margem percentual) resistem à contagem em dobro; os valores absolutos, não. Uma parte da diferença (R$ 53.490,32, ou 3,7% do total do resumo) não tem linha correspondente no detalhado e ficou sem identificação.

**2. Um item genérico torna negativo o lucro do relatório inteiro.** O relatório por produto mostra lucro de R$ -396.747,48. Quase tudo vem de um único item genérico (do tipo `DIVERSOS ...`), usado para vender por valor: ele tem custo médio de R$ 608,74 por unidade para um preço de R$ 5,54. Sem esse item o mesmo relatório dá lucro positivo. Oito itens de custo implausível, juntos, levam a margem do período de +45,6% para -38,2%.

**3. O custo disponível é a média de 36 meses, e há duas bases de custo.** O relatório por produto traz o custo somado no período, não o custo de cada venda. O custo muda no tempo: num produto conferido, R$ 228,91 em 2025 e R$ 296,14 em 2026. O relatório de vendas abaixo do custo usa outra base (o custo de compra do cadastro), e os dois não concordam: 5 dos 11 produtos com prejuízo num relatório não aparecem no outro.

**4. O relatório de vendas abaixo do custo não mede prejuízo.** As colunas "Valor Unitário" somam valores unitários sem multiplicar pela quantidade, e a linha de totais soma essas somas. O prejuízo real dessas vendas, calculado pelo relatório detalhado, é de R$ 2.297,80, dos quais R$ 1.997,44 são do item genérico.

**5. O cliente é uma linha de bloco, e o cabeçalho repetido engana.** O nome do cliente vem numa linha acima dos itens. No topo de cada página o ERP repete essa linha, mas em 55 casos com outro nome (o texto digitado na venda). Quem abre um cliente novo a cada linha `*** Cliente :` atribui errado 2.915 itens (R$ 96.393,43). O limite confiável do bloco é o rodapé `Descontos >>>`. O cliente genérico `CONSUMIDOR` responde por 80,5% do faturamento.

**6. O valor unitário não é o preço de tabela.** Ele já vem líquido do desconto rateado e truncado em 2 casas; multiplicar pela quantidade não reproduz o total em mais de 11 mil itens. O valor confiável da linha é o "Valor Total". O desconto só existe somado por cliente, no rodapé do bloco (R$ 21.548,11 no período).

**7. Os relatórios não se ligam entre si.** O detalhado e o por produto só se ligam pela descrição do produto (as 904 descrições casam por texto idêntico neste envio, mas a chave é frágil). Não existe ligação de produto para grupo ou fornecedor, de venda para forma de pagamento, de cliente para grupo de cliente, nem de item para o custo na data da venda. Dos 28 pares possíveis de relatórios, só 4 têm chave de registro.

**8. O cadastro tem falhas que pesam.** 346 dos 912 produtos (37,9%) estão com estoque negativo. 14 produtos não têm custo e respondem por cerca de 9% do faturamento. 7 produtos foram vendidos a preço simbólico (até R$ 0,10 por unidade).

## 4. Passo 1: os indicadores que esses relatórios permitem

O catálogo completo está no anexo A, um indicador por linha, com a definição, o relatório de origem, se dá para calcular e o que o motor faz hoje. Foram três frentes:

- **Comercial e cliente** (113 indicadores) e **margem, custo e preço** (124): cada indicador foi comparado com o código do motor, com o arquivo e a linha.
- **Estoque, mix e cadastro** (127 indicadores levantados pelos leitores dos relatórios): a comparação linha a linha com o motor não foi concluída (seção 12); a situação por família está logo abaixo.

| Família | Indicadores | O motor calcula | Em parte | Não calcula | Não dá com estes relatórios |
|---|---|---|---|---|---|
| **Comercial e cliente** | **113** | | | | |
| Faturamento e evolução | 13 | 1 | 4 | 8 | 1 |
| Sazonalidade e calendário | 8 | 0 | 0 | 8 | 1 |
| Pedidos e tíquete | 12 | 0 | 1 | 11 | 0 |
| Clientes: consumidor genérico e identificação | 4 | 0 | 0 | 4 | 0 |
| Clientes: carteira e concentração | 6 | 0 | 0 | 6 | 0 |
| Clientes: recorrência e recência | 10 | 0 | 0 | 10 | 0 |
| Grupo de cliente | 5 | 0 | 0 | 5 | 1 |
| Vendedor | 6 | 0 | 0 | 6 | 1 |
| Forma de pagamento | 12 | 0 | 0 | 12 | 3 |
| Documento fiscal | 10 | 1 | 0 | 9 | 1 |
| Descontos | 10 | 0 | 1 | 9 | 1 |
| Fretes e acréscimos | 3 | 0 | 0 | 3 | 1 |
| Conferência entre relatórios | 7 | 0 | 3 | 4 | 0 |
| Cruzamento com o custo (cliente, vendedor, documento e desconto) | 7 | 0 | 1 | 6 | 0 |
| **Margem, custo e preço** | **124** | | | | |
| Margem e lucro do período | 13 | 3 | 4 | 6 | 2 |
| Margem por produto | 15 | 2 | 3 | 10 | 0 |
| Margem por grupo de produto | 7 | 0 | 2 | 5 | 1 |
| Margem por fornecedor | 2 | 0 | 0 | 2 | 2 |
| Margem por cliente | 5 | 0 | 0 | 5 | 0 |
| Margem no tempo | 5 | 0 | 4 | 1 | 1 |
| Margem por vendedor | 1 | 0 | 0 | 1 | 0 |
| Margem por pedido | 5 | 0 | 0 | 5 | 1 |
| Markup | 4 | 0 | 1 | 3 | 0 |
| Custo: qualidade e bases | 15 | 2 | 3 | 10 | 2 |
| Venda abaixo do custo | 12 | 1 | 3 | 8 | 0 |
| Preço praticado | 20 | 3 | 2 | 15 | 1 |
| Descontos | 13 | 0 | 1 | 12 | 1 |
| Potencial recuperável | 5 | 1 | 4 | 0 | 0 |
| Conferência de margem e preço | 2 | 0 | 0 | 2 | 0 |
| **Total das duas lentes** | **237** | **14** | **37** | **186** | **21** |

"Não dá com estes relatórios" quer dizer que falta um dado que o ERP tem e não exportou (seção 11). Exemplos: margem por fornecedor (não há ligação de produto para fornecedor), forma de pagamento por venda e custo na data da venda.

**Estoque, mix e cadastro: situação por família.**

| Família | Exemplos de indicador | O motor calcula hoje? |
|---|---|---|
| Estoque atual | capital em estoque ao custo, estoque negativo, concentração do capital | em parte: só na Avaliação de Cenário do lead, e só quando o estoque vem como coluna de relatórios mensais |
| Estoque parado e excedente | parado por idade real (pela data da última venda), cobertura em dias, excesso sobre a cobertura-alvo | em parte: a inteligência de estoque do lead faz as três contas, mas mede a idade pelo fim do último relatório mensal com venda; com um arquivo de 36 meses ela não roda |
| Giro e cauda longa | giro mensal por produto, regularidade, cauda longa, lançamentos e descontinuados | em parte: a triagem tem giro e cauda longa, com relatórios mensais |
| Curva ABC | de faturamento, de quantidade e de lucro; ABC contra situação do estoque | em parte: a análise mensal do cliente tem a curva de faturamento; a de lucro e os cruzamentos não existem |
| Mix por grupo | participação, margem e concentração por grupo | em parte: a matriz por categoria do lead, quando a categoria vem como coluna do relatório de vendas; do relatório resumido por grupo, nada |
| Mix por fornecedor | participação, dependência do maior fornecedor, cadastros duplicados | não |
| Itens genéricos | peso do item genérico na venda e no prejuízo | não |
| Qualidade do cadastro | produto sem custo, custo parcial, conflito de código e nome, NCM e categoria ausentes | calcula (são as marcas e os motivos de cadastro da triagem); qualidade do código e do nome, não |
| Conferência entre relatórios | contagem por documento, totais que não fecham, sequência de pedidos e de notas | não: a conferência de hoje compara a leitura com o total do mesmo arquivo |

Indicadores calculados com os dados reais, para dar a medida do que os relatórios permitem (todos pela venda item a item, em escala real):

- faturamento de R$ 710.718,23 em 21.172 itens e 10.808 pedidos, com a série completa dos 37 meses do calendário;
- margem bruta de 43,82% sobre 88,93% da receita (a parte com custo confiável), entre 41,35% e 46,18% por mês;
- capital em estoque de R$ 177.932,45, com 37,45% parado há mais de 180 dias pela data real da última venda;
- cobertura mediana de estoque de 630 dias (506 pela conta feita com a quantidade inflada do relatório resumido);
- 25,12% da receita dos últimos 12 meses em produtos com estoque negativo.

## 5. Passo 2: o que o motor faz hoje com esses arquivos (o ensaio)

O motor foi executado sobre os oito relatórios reais, numa cópia do repositório sem o `.env`, pelas mesmas funções que o processo isolado chama. Foram testados os três caminhos de entrada: a análise mensal do cliente, o Raio-X do cliente e a Avaliação de Cenário do lead. Como o motor só aceita `.csv` e `.xlsx`, cada planilha também foi convertida para os dois formatos, célula a célula (166.969 valores conferidos, nenhum diferente).

### 5.1 Como os arquivos vieram: nenhum entra

| Arquivo | O que acontece | Mensagem ao usuário |
|---|---|---|
| Os 8 `.xls` | recusados em cinco pontos: na rota do portal, na cópia do envio, na entrada do Raio-X e do lead, na Baia de Segurança e no leitor | "Arquivo fora do padrão aceito para auditoria. Salve a planilha como .xlsx ou .csv e envie novamente." |
| Os 8 `.pdf` | recusados nos mesmos pontos, pela extensão e pela assinatura do conteúdo | "Formato não suportado. Envie CSV ou XLSX." |
| Os mesmos, com a extensão trocada | recusados: o conteúdo não bate com a extensão | a mesma recusa: o conteúdo é reconhecido apesar da extensão |

Não há leitor de `.xls` nem de PDF entre as dependências do projeto.

### 5.2 Convertidos para `.xlsx` e `.csv`: um relatório é lido, sete não

| Relatório | Leitura de vendas e Raio-X do cliente | Avaliação de Cenário (lead) |
|---|---|---|
| Resumido por produto | **Lido inteiro e certo**: 912 produtos, venda de R$ 1.460.035,77 e custo de R$ 1.824.372,98, iguais à linha de totais do próprio relatório; 14 produtos sem custo e 346 com estoque negativo | Recusado: "o período (09/11/2023 a 09/11/2026) passa de um ano para o outro" |
| Venda detalhada | Recusado antes de ler: a aba passa do teto de células. Sem a coluna vazia que causa isso, o cabeçalho é achado e **nenhuma linha é lida** | Recusado pelos mesmos motivos |
| Vendas abaixo do custo | Recusado: "sem nomes de produto válidos" (título e dado em colunas diferentes) | Recusado |
| Por grupo de produto, por fornecedor, por forma de pagamento, por grupo de cliente e ranking | Recusados: não há coluna de produto; em quatro deles não há quantidade | Recusados |

Dois pontos sobre o único relatório lido:

- **O número lido é o do relatório, e o relatório está inflado.** O motor lê R$ 1.460.035,77 porque é o que o arquivo diz. O faturamento real do período, pela venda item a item, é R$ 710.718,23 (seção 3, fato 1). O motor não tem como saber: a conferência do total do PDV compara a leitura com o total do próprio arquivo, e os dois batem.
- **Os 36 meses viram uma fatia só.** O Raio-X registra "37 meses" num único período. Não há linha do tempo, o alerta de precificação não dispara (ele precisa de dois arquivos) e a recomendação de plano sai pelo número de meses.

### 5.3 A cadeia de bloqueios

Cada vez que o motor recusou ou leu errado, o arquivo convertido foi corrigido artificialmente, um obstáculo por vez, até ele ler certo. Nenhuma linha do motor foi alterada. Os obstáculos, na ordem em que aparecem:

| Nº | Bloqueio | Relatórios afetados | Onde no código |
|---|---|---|---|
| B1 | O formato original (`.xls` e `.pdf`) não é lido | os 8 | `config.py` (extensões aceitas), `security_gatekeeper.py`, `etl_inputs.py` |
| B2 | O teto de 500.000 células por aba conta a área (linhas x colunas, vazias incluídas): a detalhada tem 511.670 de área e 157.444 preenchidas. O teto não sobe por configuração | venda detalhada | `etl_limits.py`, `etl_inputs.py` |
| B3 | O `.xlsx` gravado por um programa de planilha carrega as linhas desenhadas do relatório (49,8 MB) e passa do teto de 32 MiB de expansão | venda detalhada | `etl_limits.py`, `etl_inputs.py` |
| B4 | O título e o dado ficam em colunas diferentes por causa das mesclas, e o motor procura o dado embaixo do título | 6 dos 8 (no ranking de clientes o desalinhamento também existe, mas ele é barrado antes, pelo B5) | `etl_processor.py` (mapa do cabeçalho) |
| B5 | Não há coluna de produto: a dimensão é outra (grupo, fornecedor, forma de pagamento, cliente) | os 5 resumidos por dimensão | `etl_processor.py` (campos obrigatórios) |
| B6 | Não há coluna de quantidade | 4 dos 5 resumidos | `etl_processor.py` |
| B7 | Com dois títulos "Valor Unitário", o primeiro (o de compra) vira o preço de venda | vendas abaixo do custo | `etl_processor.py` (dicionário de colunas) |
| B8 | "Cód." sozinho não é reconhecido como código | vendas abaixo do custo | `etl_processor.py` |
| B9 | Período que passa de um ano para o outro (só a Avaliação de Cenário recusa) | todos os de 36 meses | `avaliacao_motor.py` |
| B10 | A coluna de data não é lida: o período vem só do cabeçalho e um arquivo é sempre uma fatia | venda detalhada | `etl_processor.py`, `raio_x.py` |
| B11 | No máximo 36 arquivos por envio, e o período real toca 37 meses do calendário | venda detalhada fatiada por mês | `raio_x.py`, `raio_x_servico.py` |
| B12 | A leitura do `.xlsx` percorre a mesma aba 6 vezes: 39 s neste computador contra 4 s do `.csv` | venda detalhada | `etl_worker.py`, `etl_inputs.py` |

Com os obstáculos B2, B4 e B10 a B12 removidos à mão (títulos alinhados e o arquivo fatiado por mês fora do motor), a venda detalhada é lida certo: 36 fatias mensais, com o número de produtos e a venda de cada mês iguais aos do relatório.

### 5.4 Aceito e errado: o motor responde sem avisar

Estes casos preocupam mais que as recusas, porque produzem um número errado em silêncio.

| Caso | O que sai | Tamanho do erro |
|---|---|---|
| Vendas abaixo do custo, com os títulos alinhados | o faturamento é calculado com o valor unitário de **compra** | R$ 8.248.038,55 em vez de R$ 3.933.089,92 |
| Um relatório de vendas enviado no campo de **compras** da análise mensal | aceito, com o total comprado igual a R$ 0,00, ao dobro do custo ou ao total vendido, conforme o arquivo | até R$ 3.648.745,96 de "compras" que não existem; com zero, o sinal sai "Giro de estoque saudável" |
| Relatório por forma de pagamento em `.xlsx` | a Baia de Segurança aceita o arquivo, porque lê dois avisos do rodapé como cabeçalho | arquivo de outro tipo liberado para o processamento |
| Venda detalhada com o período do cabeçalho trocado | os 36 meses são registrados como um mês: o motor não confere o cabeçalho com as datas das linhas | 36 meses lidos como outubro de 2026 |
| "Vazamento de preço" do Raio-X | depende de como o arquivo foi fatiado, porque o preço de referência é um por arquivo | R$ 20.495,42 num arquivo único contra R$ 5.513,62 em 36 mensais |
| "Perda abaixo do custo" do Raio-X | o Raio-X do cliente não tem a regra de custo implausível que o lead tem | R$ 1.100.700,24, dos quais 99,8% vêm de um item genérico |
| Linha de frete e cabeçalho da segunda página | viram "produto" com valores zerados | um produto a mais na contagem |
| `.csv` com nome de produto com duas ou mais vírgulas | o separador do arquivo inteiro passa a ser a vírgula | 9 dos 912 nomes reais têm duas ou mais vírgulas |

### 5.5 Compras, estoque e perdas: o que o motor já suportaria

- **Compras.** O motor lê um relatório de compras na análise mensal do cliente (data, fornecedor, produto, quantidade, custo unitário e valor da nota) e cruza com as vendas. O Raio-X e a Avaliação de Cenário recebem só vendas. Não foi possível ensaiar com dado real: não há relatório de compras entre os arquivos.
- **Estoque.** Não existe leitura de um relatório de posição de estoque. O estoque entra só como coluna opcional do relatório de vendas (é o caso do "Estoque Atual" do relatório por produto, lido certo).
- **Perdas e ajustes.** Não existe leitura em nenhum fluxo; o próprio código declara a ausência (as perdas operacionais saem nulas no diagnóstico do lead).

### 5.6 Tempo e memória medidos

Medidas neste computador, um processo por medida. O Render gratuito tem 0,1 CPU; a regra de pior caso usada foi multiplicar o tempo por 10, **o que é uma estimativa, não uma medida**.

| O que foi medido | Tempo aqui | Pior caso estimado | Cabe em 120 s? |
|---|---|---|---|
| Raio-X do relatório por produto, `.xlsx` | 2,2 s | cerca de 45 s | sim |
| Raio-X da venda detalhada legível, `.csv` | 4,2 s | cerca de 65 s | sim |
| Raio-X da venda detalhada legível, `.xlsx` | 39,1 s | cerca de 400 s | não |
| Envio de 36 arquivos mensais, `.csv` | 6,6 a 7,3 s | 90 a 97 s | sim, com pouca folga |
| Envio de 36 arquivos mensais, `.xlsx` | 42,9 a 44,5 s | cerca de 450 s | não |
| Ler o `.xls` original direto (extrair e agregar os 21.172 itens) | 1,1 a 2,0 s | 34 a 43 s | sim |
| Extrair o texto das 741 páginas do PDF com biblioteca Python | 62 a 270 s | mais de 600 s | não |

A memória não é o problema: o maior pico foi de 152,5 MB de memória do processo, contra o limite de 384 MB. O que separa os formatos é o tempo.

## 6. Passo 2: as colunas que o motor ignora

Os oito relatórios têm 39 colunas de dado, fora os campos de bloco (cliente, desconto e total do cliente). Num arquivo que o motor consegue ler, **6 são lidas**, todas do relatório por produto. A tabela completa, coluna a coluna, está no anexo B.

| Relatório | Colunas lidas | Colunas ignoradas ou lidas errado |
|---|---|---|
| Resumido por produto | Codigo, Nome do produto, Qtde Vend, Total Venda, Total Custo, Estoque Atual | **Marg. de Lucro %** (é detectada como "lucro" e apagada) e **Lucro** (ignorada); o desconto total da linha de totais |
| Venda detalhada | nenhuma (o relatório não é lido). Com os títulos alinhados à mão: Produto, Qtde e Valor Total | **Data, Ven., Nr. Doc.** (pedido e nota) e **Valor Unit.**: não há campo no motor; o **cliente** (linha de bloco) e o **desconto por cliente** (rodapé) |
| Vendas abaixo do custo | nenhuma (não é lido). Com os títulos alinhados: Descrição e Qtde Vendida | **Valor Unitário Compra** (lido como preço de venda), **Valor Unitário Vendido**, **Diferença** e o **Cód.** |
| Por grupo de produto | nenhuma (falta coluna de produto) | Nome do grupo, Lucro |
| Por fornecedor | nenhuma | Fornecedor |
| Por forma de pagamento | nenhuma | Forma de pagamento |
| Por grupo de cliente | nenhuma | Grupo de cliente, Valor vendido, Lucro valor |
| Ranking de clientes | nenhuma | Cod., Cliente, CPF/CNPJ, telefones, e-mail, Total |

O dicionário de colunas de vendas do motor tem oito campos: código, produto, estoque, custo, valor de venda, preço unitário, quantidade e lucro (este, apagado). **Não existe campo para data da venda, cliente, vendedor, documento, forma de pagamento, fornecedor, desconto nem margem.** A data e o fornecedor existem só no dicionário de compras.

## 7. Passo 2: as formatações reais e o que cada uma exige dos parsers

O que já funciona não precisa de ajuste; fica na tabela para registrar que foi testado com a forma real.

| Formatação real | O que o motor faz hoje | Ajuste necessário |
|---|---|---|
| Arquivo `.xls` (BIFF8, textos em UTF-16) | recusa | ler o `.xls` no processo isolado (decisão sua; seção 9) |
| Arquivo PDF | recusa | nenhum: ler PDF no processo isolado não é viável (seção 9.7) |
| `Período de  : 09/11/23 á 09/11/26` (ano com 2 dígitos, `á` com acento) | **lê certo**: 09/11/2023 a 09/11/2026 | nenhum na leitura; falta tratar o período de vários anos (fatiar pela data da venda) |
| Data da venda em cada linha (data de verdade na planilha; `dd/mm/aa` em texto) | chega como texto e é ignorada: não há campo de data em vendas | campo novo de data e a conversão |
| Dinheiro com milhar e vírgula (`1.460.035,77`) | lê certo | nenhum |
| Negativo entre parênteses (`(396.747,48)`) | lê certo | nenhum |
| Percentual com espaço (`-99,97 %`, `60,21 %`) | numa coluna de dinheiro vira R$ 0,00 com alerta; numa de quantidade ou de estoque entra como número, **sem alerta** | barrar percentual em quantidade e estoque; campo próprio para a margem |
| Número com 3 casas na planilha (`19,431`) | lê certo, graças a uma segunda leitura da aba que desfaz a confusão com milhar | nenhum; ler os tipos direto do `.xls` dispensa a segunda leitura |
| Número com 4 casas (`3,8081`) | lê certo | nenhum |
| Quantidade exibida com 1 casa e guardada com 3 | no `.csv` salvo "como exibido" perde precisão (61.626,3 em vez de 61.626,278) | preferir a planilha, que guarda o valor inteiro |
| `.csv` em cp1252 ou UTF-8 | lê certo, com reparo de acentuação célula a célula | nenhum |
| `.csv` em UTF-16 | lê por acaso; caractere fora do Latin-1 vira lixo, sem aviso | reconhecer UTF-16 |
| Separador do `.csv` com vírgula decimal | escolhe o separador que der mais colunas em **qualquer** linha: um nome de produto com duas vírgulas troca o separador do arquivo inteiro | escolher pelo separador consistente entre as linhas |
| Título e dado em colunas diferentes (mesclas) | lê o dado embaixo do título e não acha nada (7 dos 8 leiautes) | casar título e dado pela faixa da mescla, ou por um perfil do leiaute |
| Cabeçalho em duas linhas ("Qtde" sobre "Vend") | **funciona** | usar a segunda linha também para distinguir títulos repetidos ("Valor Unitário" de compra e de venda) |
| Cabeçalho repetido a cada página, linha em branco entre os dados, título e filtros antes do cabeçalho | **funciona** (até 100 linhas antes do cabeçalho) | nenhum |
| Cliente como linha de bloco (`*** Cliente :`) com rodapé | ignorado | ler o bloco e fechá-lo pelo rodapé, não pelo cabeçalho repetido |
| Pedido e nota na mesma célula (`P2-47259  NFC2-34448`) | ignorado | separar o pedido e as notas |
| Código com zeros à esquerda e com máscara (`000001234.56`) | lido como texto no relatório por produto (certo); o título "Cód." sozinho não é reconhecido | reconhecer "Cód."; normalizar a máscara para cruzar relatórios |
| Aba com muita área vazia e células em branco formatadas | recusa a detalhada: 511.670 de área para 157.444 células com valor | contar as células com valor |
| Linhas desenhadas do relatório dentro do `.xlsx` convertido | recusa: 66 MB expandidos contra o teto de 32 MiB | ler o `.xls` direto evita a conversão |
| Rodapés deste ERP (`Valor Total para este cliente >>>`, `Valor total dos fretes`, `Atenção .:`, `Label60`) | na posição real são descartados; se caírem na coluna do produto, 33 de 52 textos testados viram produto e somam | incluir esses textos na regra explícita de rodapés |

## 8. Passo 2: diagnósticos e cruzamentos novos para a Matriz de Triagem e os painéis

**O que a triagem de hoje faria com estes dados.** Ela foi ensaiada com os dados reais, depois de contornar os bloqueios à mão. Com o relatório agregado por produto, só 8 dos 23 motivos podem disparar (os de custo e de cadastro), porque não há meses. Com a venda item a item fatiada por mês, 9 disparam (ritmo, giro e preço) e os 14 de custo, estoque e cadastro ficam mudos, porque esse relatório não traz custo nem estoque. Em nenhum formato um produto cai na Urgência, e 80% do catálogo cai na Eletiva. A matriz só fica completa quando os dois relatórios são lidos juntos.

### 8.1 Conferências de integridade, antes da triagem

Estas cinco conferências mudam todos os outros números. Devem rodar antes de qualquer indicador.

| Conferência | Regra | O que fazer quando dispara | Nos dados reais |
|---|---|---|---|
| Contagem por documento | quando a venda item a item e um relatório resumido vêm juntos, comparar os totais; diferença acima de 2% (valor inicial, a calibrar) | usar os valores absolutos só da venda item a item e avisar; do resumido, só as proporções | fator de 2,05 |
| Relatório resumido sozinho | sem a venda item a item não há como conferir a escala | avisar que os valores não foram conferidos e pedir a venda detalhada | é o caso de hoje |
| Item genérico e custo implausível | custo médio a partir de 3 vezes o preço médio (a regra que a triagem do lead já tem), aplicada também ao Raio-X | tirar o item dos totais de margem e de perda e listá-lo "a confirmar" | 8 itens; um deles responde por 99,8% da perda |
| Soma dos itens contra os rodapés | a soma de cada bloco de cliente tem de fechar com o rodapé, e a soma geral com o total do relatório | recusar a leitura: é sinal de leiaute lido errado | fecha em 294 de 294 blocos |
| Período do cabeçalho contra as datas dos itens | toda data de item dentro do período declarado | recusar | hoje o motor aceita 36 meses como um mês |

### 8.2 Motivos novos para a matriz

São candidatos. Os limiares precisam ser calibrados com os dados antes de valer, como os 23 de hoje; a medição nos dados reais, que diria quantos produtos cada um marcaria, não foi concluída (seção 12).

| Motivo candidato | Bloco | Regra | De onde vem o dado |
|---|---|---|---|
| Estoque parado pela data real | Urgência ou Eletiva, conforme o capital | estoque positivo e última venda há 180 dias ou mais, pela data do item | detalhada e por produto |
| Venda abaixo do custo recorrente, por data | Atenção | venda com preço efetivo abaixo do custo médio em vários meses do último ano | detalhada e por produto (custo médio, com ressalva) |
| Preço praticado disperso | Atenção | o mesmo produto com preço efetivo muito diferente entre clientes ou entre meses | detalhada |
| Produto sem custo na classe A | Atenção | produto sem custo entre os que fazem 80% do faturamento | detalhada e por produto (10 dos 14 sem custo estão na classe A) |
| Estoque negativo com venda recente | Atenção | saldo negativo e venda nos últimos 90 dias: entrada de compra não lançada | detalhada e por produto |
| Item genérico relevante | Atenção | item genérico acima de uma fatia do faturamento | detalhada |
| Desconto que consome a margem | Eletiva | desconto do cliente acima de uma fatia da compra dele | detalhada (o desconto só existe por cliente) |
| Venda sem documento fiscal | Atenção, só como aviso | item sem nota ligada ao pedido | detalhada (583 itens; parte pode ser nota emitida e não ligada) |

Dois grupos de diagnóstico **não cabem na matriz**, que é por produto, e pedem painel próprio: os de cliente (concentração da carteira, cliente que parou de comprar, novos e recorrentes) e os de calendário (sazonalidade, dia da semana).

### 8.3 O que muda nos motivos de hoje

Com a venda item a item e a data de cada venda, os motivos de ritmo, giro, janela dos mesmos meses e margem negativa recorrente deixam de depender de o consultor exportar um arquivo por mês: os meses saem da própria data. O "estoque parado por idade" passa a usar a data real da última venda. O "vazamento de preço" deixa de variar com o jeito de fatiar o arquivo. Os motivos de custo continuam dependendo do custo médio do período, até o ERP entregar o custo de cada venda.

### 8.4 Painéis

Séries que os dados sustentam, no padrão das visualizações que o diagnóstico do lead já entrega (título, rótulos e séries com unidade):

| Painel | De que dado sai | Ressalva |
|---|---|---|
| Faturamento, pedidos e tíquete por mês (37 meses) | detalhada | nenhuma |
| Sazonalidade e dia da semana | detalhada | nenhuma |
| Concentração de clientes e peso do consumidor genérico | detalhada | cliente como pseudônimo |
| Margem por mês | detalhada e por produto | custo médio do período |
| Estoque parado por idade real e cobertura | detalhada e por produto | o estoque é o de hoje |
| Participação por forma de pagamento, por grupo e por fornecedor | relatórios resumidos | só a proporção vale; o valor está contado por documento |

### 8.5 Dados pessoais

Os clientes do lead são terceiros. A proposta é não gravar nome, CPF, CNPJ, telefone nem e-mail: o cliente entra como um pseudônimo (um código derivado do nome, com um segredo por lead), e só os agregados ficam no resumo. As linhas com o pseudônimo expiram com as linhas de PDV do lead, em 30 dias. O ranking de clientes, que traz documento, telefone e e-mail em colunas, não precisa ser lido: a venda item a item dá a mesma informação sem os dados pessoais.

## 9. Passo 3: proposta de expansão do código

**Atualização de 10/11/2026:** as fases 1 e 2 e parte da fase 3 foram implementadas pela unificação do motor (`Docs/UNIFICACAO_MOTOR_ETL.md`), com duas diferenças de caminho: o título e o dado são casados pela ordem das colunas, e não pela faixa da mescla, e não foi criada uma tabela de itens separada (as fatias mensais saem da própria leitura). O texto abaixo é o da proposta, como foi escrita.

Esta seção é uma proposta: nada dela foi implementado. Ela foi escrita sobre as medidas dos ensaios (seção 5.6), mas **não passou pela revisão independente planejada** (seção 12). Trate os nomes de módulo e de função como ponto de partida.

### 9.1 Princípios

1. **Nenhum número errado em silêncio.** Na dúvida, o motor recusa e diz o que enviar.
2. **O que funciona hoje não muda de resultado.** Os testes de referência (`tests/test_raio_x_regression.py` e `tests/test_cadastro_regression.py`) e os envios em `.csv` e `.xlsx` continuam idênticos.
3. **Adaptar na borda, antes do motor.** O relatório paginado vira uma tabela comum antes de chegar aos cálculos, que mudam o mínimo.
4. **Ler o que o ERP entrega.** Pedir ao consultor que converta o arquivo é o caminho que hoje falha: a conversão do relatório detalhado estoura dois limites e, quando passa, leva dez vezes mais tempo.

### 9.2 As camadas

| Camada | O que faz | Hoje | Proposta |
|---|---|---|---|
| 1. Ler o formato | abre o arquivo e entrega células com tipo | `.csv` e `.xlsx`, tudo como texto, a aba percorrida até 6 vezes | acrescentar o `.xls`, lido em uma passada, com números e datas como vieram; contar células com valor, e não a área |
| 2. Normalizar o leiaute | transforma o relatório paginado numa tabela | supõe o dado embaixo do título | casar título e dado pela faixa da mescla; tirar cabeçalhos repetidos, filtros e rodapés; fechar blocos de cliente pelo rodapé; separar pedido e nota; conferir as somas contra os rodapés |
| 3. Tabela de itens | uma linha por item vendido, com data, pedido, notas, vendedor, cliente (pseudônimo), produto, quantidade, unitário e total | não existe: a unidade é o arquivo | nova; a venda detalhada entra por aqui, e o relatório por produto entra como tabela de custo e estoque |
| 4. Conferências | integridade antes do cálculo (seção 8.1) | só o total do mesmo arquivo | contagem por documento, custo implausível, somas e período |
| 5. Cálculo | Raio-X, Avaliação de Cenário, triagem e inteligência | recebe um arquivo por mês | recebe as fatias mensais tiradas da tabela de itens, sem regravar arquivo |
| 6. Saída | resultado até 4 MiB, conferido campo a campo | matriz produto x ano e meta | o mesmo, mais as séries mensais e as dimensões novas, só com números e pseudônimos |

### 9.3 Como cada bloqueio fica resolvido

| Bloqueio ou erro (seções 5.3 e 5.4) | Solução | Fase |
|---|---|---|
| B1: `.xls` recusado | leitor de `.xls` no processo isolado, com a Baia de Segurança inspecionando o arquivo antes (seção 9.6) | 2 |
| B1: PDF recusado | continua recusado, com a orientação de enviar a planilha | — |
| B2: teto de células pela área | contar as células com valor | 2 |
| B3: desenhos no `.xlsx` convertido | deixa de existir quando o `.xls` é lido direto | 2 |
| B4: título e dado em colunas diferentes | casar pela faixa da mescla | 2 |
| B5 e B6: relatórios sem produto ou sem quantidade | lidos como tabelas de proporção por dimensão (grupo, fornecedor, forma de pagamento), nunca como vendas | 4 |
| B7: dois títulos "Valor Unitário" | usar a segunda linha do cabeçalho; título repetido sem distinção vira recusa | 1 |
| B8: "Cód." não reconhecido | acrescentar ao dicionário | 1 |
| B9: período de vários anos | fatiar pela data da venda; sem data por linha, a recusa de hoje continua | 3 |
| B10: a data não é lida | campo novo de data | 3 |
| B11: teto de 36 arquivos | contar meses do calendário das fatias de um mesmo arquivo; o teto de arquivos enviados não muda | 3 |
| B12: seis passadas no `.xlsx` | uma validação e uma leitura | 2 |
| Compras aceitam relatório de vendas | erro quando nenhuma linha é válida; tirar a linha de totais também nas compras | 1 |
| A Baia aceita arquivo pelos rodapés | a estrutura mínima não pode casar avisos nem rodapés | 1 |
| Perda dominada por custo implausível | a regra que o lead já tem, aplicada ao Raio-X | 1 |
| Separador do `.csv` | escolher pelo separador consistente entre as linhas | 1 |
| Frete e rodapés que viram produto | incluir os textos deste ERP na regra explícita | 1 |
| Período do cabeçalho não conferido | conferir contra as datas dos itens | 3 |
| Vazamento de preço que muda com o fatiamento | deixa de existir: as fatias passam a ser sempre mensais | 3 |
| Faturamento inflado do relatório resumido | conferência da contagem por documento; aviso quando o resumido vem sozinho | 3 |

### 9.4 Campos novos

| Campo | De onde vem | Observação |
|---|---|---|
| Data da venda | coluna "Data" da detalhada | base dos meses, do calendário e da idade do estoque |
| Pedido e notas | coluna "Nr. Doc." (uma célula com os dois) | o pedido identifica a venda; a nota mede a cobertura fiscal |
| Vendedor | coluna "Ven." | só o código |
| Cliente | linha de bloco `*** Cliente :`, fechada pelo rodapé | gravado como pseudônimo |
| Desconto do cliente | rodapé `Descontos >>>` | só existe por cliente no período |
| Valor unitário efetivo | "Valor Total" dividido por "Qtde" | o "Valor Unit." do relatório vem truncado |
| Margem e lucro do ERP | "Marg. de Lucro %" e "Lucro" do relatório por produto | só para conferir contra a margem que o motor calcula |
| Custo médio e estoque atual | relatório por produto, ligado pela descrição | a ligação exige casamento exato; quantidades e valores desse relatório não são usados, porque estão inflados |

### 9.5 Um arquivo de 36 meses vira meses

A tabela de itens é fatiada por mês do calendário em memória, e cada fatia entra no motor como hoje entra um relatório mensal. O consultor não precisa exportar 36 arquivos. O período real toca 37 meses (de novembro de 2023 a novembro de 2026): a proposta é contar os meses das fatias de um mesmo arquivo com teto próprio, e manter o teto de 36 para arquivos enviados separadamente. O mês parcial entra com os dias que cobre, como a Avaliação de Cenário já faz com o mês corrente.

### 9.6 O `.xls`: o que se ganha, o que se arrisca e a alternativa

**O que se ganha.** É o formato que o ERP exporta. Lido direto, custa de 1,1 a 2,0 segundos para os 21.172 itens, contra 26 a 39 segundos do `.xlsx` convertido, que além disso estoura dois limites.

**O que se arrisca.** O `.xls` é um contêiner que pode trazer macro e objeto embutido; é por isso que a Baia de Segurança o recusa hoje. Nos oito arquivos reais não há macro, fórmula nem objeto embutido: 62% dos bytes são linhas desenhadas do relatório.

**Duas formas de ler**, as duas medidas:

| Caminho | Tempo (extrair e agregar) | Memória a mais | Prós e contras |
|---|---|---|---|
| Leitor próprio em fluxo, só com a biblioteca padrão | 1,1 s | cerca de 4 MB | lê só os registros de célula e ignora desenhos, macros e objetos; código novo para manter (o protótipo tem 585 linhas e foi conferido célula a célula contra a biblioteca nos 8 arquivos) |
| Biblioteca `xlrd` | 2,0 s | cerca de 45 MB | pronta e conhecida; é dependência nova, carrega o arquivo inteiro e falhou com 8 tipos de exceção em arquivos corrompidos de propósito |

**Recomendação conservadora:** leitor próprio, aceitando `.xls` primeiro só na Avaliação de Cenário (quem envia é o consultor), atrás de uma configuração, com a Baia recusando arquivo cifrado, com macro ou com objeto embutido. **A alternativa** é manter a recusa e orientar o consultor a salvar como `.csv`: funciona para o relatório por produto, mas a venda detalhada continua exigindo a camada 2 e a contagem de células com valor.

### 9.7 O que não fazer

- **Ler PDF no processo isolado.** Leva de 62 a 270 segundos para as 741 páginas com biblioteca Python; o extrator rápido não existe no servidor, e o processo isolado recusa abrir outro programa. Além disso, estes PDFs são impressões da planilha, com valores arredondados e texto embaralhado.
- **Corrigir o relatório resumido dividindo por dois.** O fator vai de 1 a mais de 3 por produto.
- **Tratar o `.xlsx` convertido como caminho principal.** Dois limites e dez vezes o tempo.
- **Subir os tetos do processo isolado para fazer caber.** Os limites existem para proteger o servidor.

### 9.8 Orçamento de tempo e de memória

| Etapa | Medido neste computador | Observação |
|---|---|---|
| Carregar o motor (Python e bibliotecas) | 2,2 a 2,4 s; 88 MB | conta dentro do prazo |
| Ler o `.xls` de 15 MB e extrair os itens | 1,1 a 2,0 s; 4 a 45 MB a mais | conforme o leitor |
| Calcular sobre 36 fatias mensais | 6,6 a 7,3 s | medido com 36 arquivos `.csv`, o que inclui validar e reler cada arquivo; em memória deve custar menos, mas **isso não foi medido** |
| **Total** | **cerca de 10 a 12 s** | |
| Pior caso estimado (x 10) | 100 a 117 s | cabe em 120 s, **com pouca folga** |
| Memória | maior pico medido: 152,5 MB | limite de 384 MB; no teto do formato (65.536 linhas) a leitura sobe para 28 MB (leitor próprio) ou 130 MB (`xlrd`) |

Duas consequências:

1. **A folga de tempo é pequena e a regra do "vezes 10" não foi medida.** A primeira tarefa da fase 3 é medir no Render; se não couber, a saída é processar em dois passos (ler e normalizar; depois calcular), e não subir o prazo.
2. **O tamanho do arquivo tem teto antes do tempo.** O `.xls` deste desenho chega aos 20 MB do envio com 36.838 linhas, cerca de 49 meses desta loja, e a aba do formato comporta 65.536 linhas. Uma loja maior precisa exportar por ano; o fatiamento por data aceita vários arquivos.

### 9.9 Fases

Cada fase é entregável e testável sozinha.

| Fase | O que entrega | O que destrava | Risco | Depende de você |
|---|---|---|---|---|
| 1. Proteções | as correções de "aceito e errado" (seção 9.3), cada uma com teste | nenhum indicador novo; o motor deixa de responder errado | baixo; a regra do separador do `.csv` e a estrutura mínima da Baia mexem em caminho usado hoje e pedem os testes de referência | não |
| 2. Ler o `.xls` e o leiaute paginado | leitor de `.xls`, contagem de células com valor, casamento de título e dado, leitura em menos passadas | os relatórios por produto e de vendas abaixo do custo entram como o ERP exporta | médio: formato novo na entrada | **sim: aceitar `.xls`** |
| 3. Tabela de itens, fatias e conferências | campos novos, fatias mensais em memória, as cinco conferências, a ligação com o relatório por produto | a venda detalhada; o faturamento real; a triagem completa (ritmo, giro, custo e estoque juntos); o estoque parado pela data real | médio: ponto de entrada novo no motor; medir no Render | não |
| 4. Dimensões e painéis | cliente (pseudônimo), vendedor, documento e calendário; relatórios resumidos como proporção; motivos novos; painéis | os indicadores comerciais e de cliente | médio: contrato da saída e telas; pode pedir migração para gravar as séries | **sim: limiares dos motivos novos, a migração e o que o cliente final verá** |
| 5. Compras, estoque e perdas | leitura dos três tipos | cruzamento de compras com vendas, inventário e perdas | não dá para estimar sem os arquivos | **sim: obter os relatórios do ERP** |

**Arquivos do motor que cada fase toca** (para quem for implementar):

| Fase | Arquivos e funções |
|---|---|
| 1 | `etl_processor.py` (`carregar_compras`, `_mapear_cabecalho`, o dicionário `ALIAS_VENDAS`, a regra explícita de rodapés), `etl_inputs.py` (escolha do separador em `_csv_input`), `security_gatekeeper.py` (estrutura mínima), `raio_x.py` (custo implausível em `_combinar`) |
| 2 | módulo novo para o leitor de `.xls`; `etl_inputs.py` (`validate_input` e `read_raw`: ramo do `.xls`, contagem de células, menos passadas); `security_gatekeeper.py` (inspeção do contêiner); `config.py` (extensão aceita atrás de configuração); módulo novo para o normalizador de leiaute |
| 3 | `etl_processor.py` (campos de data, documento e vendedor; `_vendas_do_bruto`), `raio_x.py` (`_ler_envio`: fatias em memória; a regra dos 36), `avaliacao_motor.py` (`avaliar_cenario`: período de vários anos quando há data por linha), módulo novo para as conferências |
| 4 | `avaliacao_motor.py` (meta com séries e dimensões), `etl_runtime.py` (`validar_avaliacao`: as chaves novas), `triagem_cenario.py` (motivos e limiares), `diagnostico_cenario.py` (visualizações), telas do consultor |

## 10. Testes e fixtures

**Atualização de 10/11/2026:** com a unificação do motor (`Docs/UNIFICACAO_MOTOR_ETL.md`, seção 11), `tests/test_cobertura_leiautes_erp.py` foi reescrito. Os oito testes de lacuna perderam o `expectedFailure` e passam; `RecusasAtuaisTests` virou `RecusasTests`, só com as recusas que continuam; e entraram os testes das regras novas. O texto abaixo descreve os testes como a auditoria os entregou.

Entraram dois arquivos em `tests/`. Nenhum teste existente foi alterado.

**`tests/leiautes_erp_support.py`: as fixtures.** Gera, em diretório temporário, os oito relatórios **no leiaute real** do ERP: o título, o período com ano de dois dígitos, as linhas de filtro, os marcadores de modelo, o cabeçalho em duas linhas repetido por página, a linha em branco entre os dados, o bloco de cliente com o rodapé, os totais, os avisos e as células nas mesmas colunas da planilha real, com as mesclas. **Todos os dados são inventados** (produtos, clientes, fornecedores, loja, documentos e valores) e coerentes entre si, para os testes conferirem somas. Grava em `.xlsx` e em `.csv`.

**`tests/test_cobertura_leiautes_erp.py`: 19 testes em três grupos.**

| Grupo | Testes | O que afirma |
|---|---|---|
| `GarantiasTests` | 6 | o que o motor já lê certo nesse leiaute e não pode regredir: o relatório por produto lido inteiro e fechando com a linha de totais; cabeçalho repetido, filtros, totais e rodapés fora dos produtos; o período com ano de dois dígitos; milhar, negativo entre parênteses e número com 3 casas |
| `RecusasAtuaisTests` | 5 | as recusas de hoje que a expansão vai mudar (B1, B2, B4, B5, B6 e B9), cada uma com o código do erro e a mensagem; a docstring diz a fase que a resolve |
| `LacunasConhecidasTests` | 8 | o comportamento **desejado** nos casos em que o motor aceita e erra ou não lê o que deveria; cada um leva `expectedFailure` |

Os oito testes de lacuna falham hoje pelo motivo esperado (conferido rodando o módulo sem o marcador): a venda detalhada não lida; o arquivo de vários anos recusado; o unitário de compra usado como preço; compras aceitando relatório de vendas; a Baia liberando o relatório por forma de pagamento; o separador do `.csv` trocado por um nome com vírgulas; a perda do Raio-X dominada por um custo implausível; e o percentual aceito como quantidade.

**Como os testes acompanham a expansão.** Quando uma fase corrigir um desses casos, o teste correspondente passa a "sucesso inesperado", a suíte acusa, e o marcador sai: a lacuna vira garantia. Quando uma recusa de `RecusasAtuaisTests` deixar de existir, o teste é reescrito para o comportamento novo. Cada fase da seção 9.9 acrescenta os próprios testes sobre as mesmas fixtures.

**Execução.** O módulo roda em cerca de 3 segundos, numa cópia do repositório sem o `.env`: 19 testes, 11 aprovados e 8 falhas esperadas. A suíte completa não foi rodada de novo, porque nenhum arquivo do motor mudou. Os testes não passaram pela revisão independente nem por testes de mutação, que estavam planejados (seção 12).

## 11. O que pedir ao ERP

Em ordem do que mais destrava.

| Pedido | Para quê |
|---|---|
| Tirar de novo os relatórios resumidos com outra opção de "Andamento" e conferir se o total passa a bater com a venda detalhada | confirmar a causa da contagem em dobro e saber qual opção usar |
| A venda detalhada com o código do produto, o custo unitário e o lucro de cada item | ligar os relatórios pelo código; margem pela data da venda |
| O cadastro de produtos (código, descrição, grupo, subgrupo, fornecedor, unidade, NCM, custo e preço) | ligar produto a grupo e a fornecedor |
| Compras por nota de entrada, item a item (data, fornecedor, produto, quantidade, custo unitário) | o cruzamento de compras com vendas |
| Posição de estoque completa, com custo e data da última entrada | hoje só aparece o estoque dos produtos que venderam |
| Movimentação de estoque por tipo (ajuste, perda, devolução, transferência) | perdas e ajustes |
| Vendas por documento com a forma de pagamento e as parcelas | forma de pagamento por venda |
| A relação de documentos fiscais com situação e pedido de origem | identificar os R$ 53.490,32 que só os resumidos contam |
| O ranking de clientes no período inteiro (veio de um dia) e o código do cliente na venda detalhada | carteira de clientes sem depender do nome |
| Perguntar ao fabricante se o ERP exporta `.csv` e o que ele faz quando o relatório passa de 65.536 linhas | o formato mais leve e o limite de tamanho |

## 12. O que esta auditoria não provou

- **O processo isolado de verdade.** Ele só roda em Linux. As mesmas funções foram executadas no próprio processo, como os testes do projeto fazem; a memória medida é a do Windows, não o limite de endereçamento do Linux.
- **O tempo no Render.** Os tempos são deste computador. O "vezes 10" é uma regra de pior caso, não uma medida.
- **A conversão pelo Excel.** Não havia Excel na máquina. A conversão foi simulada célula a célula e cruzada com a do LibreOffice.
- **Compras, estoque e perdas.** Não há esses relatórios; o que o motor faria com eles foi lido no código, não ensaiado.
- **Os R$ 53.490,32** que os relatórios resumidos contam e a venda detalhada não lista.
- **A síntese final.** Os agentes de verificação foram interrompidos pelo limite de uso da conta. Ficaram sem a segunda verificação independente: o catálogo de indicadores do anexo A, a tabela de colunas e formatos do anexo B e a proposta da seção 9. A comparação linha a linha com o motor não foi feita para a frente de estoque, mix e cadastro, e os motivos candidatos da seção 8.2 não foram medidos nos dados. Os fatos das seções 1 a 3 e o ensaio da seção 5 foram conferidos antes da interrupção, por leitores independentes e por execução.
- **O protótipo de ponta a ponta** (ler, normalizar, fatiar e calcular num processo só) não foi concluído: o orçamento da seção 9.8 soma etapas medidas em separado.

## 13. Material de trabalho

Os textos extraídos dos PDFs, os despejos das planilhas, as tabelas derivadas, os relatórios de cada leitor e os scripts ficaram numa pasta temporária da sessão, fora do repositório, porque contêm dados reais. Os arquivos originais estão nas duas pastas da área de trabalho de onde foram lidos. Nada disso deve ser copiado para o repositório.
