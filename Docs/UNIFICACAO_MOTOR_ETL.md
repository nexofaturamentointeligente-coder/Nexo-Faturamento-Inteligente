# Unificação do motor ETL e da Avaliação de Cenário (11/2026)

Situação em 10/11/2026: implementado no ramo `motor-unificado-xls`, sobre o commit `464de3f`. **Sem migração de banco.** Nada foi publicado nem aplicado em produção.

Pedido (diretriz "Engrenagem única"): a Avaliação de Cenário e o motor principal lerem os relatórios com a mesma engrenagem, nos formatos em que os ERPs exportam (`.xls`, `.xlsx`, `.csv` e `.xml`); resolver as falhas que a auditoria de cobertura mediu (`Docs/AUDITORIA_COBERTURA_MOTOR_ETL.md`); e deixar verdes os oito testes que a auditoria tinha deixado como falha esperada.

## Resumo

**O que o motor passou a fazer:**

1. **Lê o `.xls` do Excel 97-2003 e a Planilha XML 2003 direto**, sem conversão, no Raio-X e na Avaliação de Cenário. São dois leitores próprios (`etl_biff.py` e `etl_planilha_xml.py`), só com a biblioteca padrão do Python: nenhuma dependência nova. Do arquivo saem apenas valores de célula.
2. **Lê o relatório paginado de ERP**, em que o título de cada coluna fica numa coluna e o dado em outra (a venda item a item).
3. **Fatia por mês, em memória, o arquivo que traz a data de cada venda** (só na Avaliação de Cenário). O arquivo de 36 meses vira um relatório por mês, sem o consultor exportar 36 arquivos.
4. **Fecha o faturamento com o total do relatório, no centavo**, quando o ERP informa o total da linha com fração de centavo.
5. **Deixa de responder errado em silêncio** nos casos que a auditoria achou (seção 4), e separa o item de custo implausível da perda do Raio-X (seção 5).

**O número que a diretriz pediu.** Com o relatório real de venda item a item, no `.xls` em que o ERP exporta (15,3 MB, 36 meses), o Raio-X e a Avaliação de Cenário devolvem **R$ 710.718,23**, 21.172 linhas de venda e 904 produtos. A Avaliação de Cenário separa 37 relatórios mensais (novembro de 2023 a novembro de 2026). Neste computador, a leitura inteira leva 6 segundos e ocupa cerca de 40 MB a mais que o processo já ocupava (seção 9).

**O que não mudou.** O envio das análises mensais continua aceitando só `.csv` e `.xlsx` (seção 2.4). Os testes de referência do motor passam sem nenhum valor alterado: as únicas mudanças aprovadas são a versão das regras do Raio-X (`raio-x-2026-11.5`) e o campo novo `custo_a_confirmar` (seção 11).

**O que ficou de fora**, com o motivo, está na seção 10. O principal: não existe como tirar a contagem em dobro (pedido e nota) do relatório **resumido** por produto; o faturamento certo sai da venda item a item.

**Revisões independentes antes do commit.** A mudança inteira passou por uma revisão adversarial, que achou defeitos de verdade: o mais sério fazia o motor recusar relatórios `.csv` legítimos que ele lia antes. As correções passaram, por sua vez, por duas verificações independentes, e cada uma achou regressões nas correções da rodada anterior. O que se sustentou como defeito foi corrigido, com teste; os limites que ficaram estão listados na seção 10. **As correções da última rodada não tiveram revisão independente**: o limite semanal de uso da conta acabou no meio da terceira verificação, e eu as conferi sozinho, com os roteiros e os sorteios que os revisores deixaram. A seção 10.1 conta o que cada revisão cobriu, o que achou e o que nenhuma cobriu.

## 1. Cinco premissas da diretriz que o código não confirmou

A diretriz descrevia o repositório de um jeito que a varredura do Passo 1 não encontrou. O trabalho seguiu o que o código é, e não o que o texto supunha:

| A diretriz dizia | O que há no código | O que foi feito |
|---|---|---|
| Existem os leitores `leitor_excel`, `leitor_csv` e `leitor_xml` | Não existem. A leitura está em `etl_inputs.py` (formatos e limites) e `etl_processor.py` (cabeçalho, dicionário de colunas, rodapés, moedas) | As regras foram mapeadas nesses dois arquivos e estendidas neles |
| A Avaliação de Cenário tem uma leitura separada, a unificar | Já usava a mesma: `avaliacao_motor` → `raio_x._ler_envio` → `etl_processor._vendas_do_bruto` | Nada a unificar na leitura; a unificação foi de regras (seções 5, 6 e 8) |
| O `etl_processor` já lê `.xls` e `.xml` | Lia só `.csv` e `.xlsx`; o `.xls` era recusado de propósito | Os dois formatos foram acrescentados ao motor (seção 2) |
| Há uma regra de deduplicação entre pedido e nota a levar para o lead | Não há, e no relatório resumido ela é impossível: o arquivo traz uma linha por produto, já somada | O faturamento de R$ 710.718,23 sai da venda item a item, que agora é lida (seções 3 e 7) |
| O expurgo de macros e objetos OLE é feito com `xlrd` e `openpyxl` | O `xlrd` não é dependência do projeto | Leitor próprio de `.xls`, que só abre o fluxo da pasta de trabalho: macro e objeto embutido ficam sem abrir (seção 2.2) |

## 2. Leitura: quatro formatos, uma matriz

Todo arquivo vira a mesma coisa antes de chegar aos cálculos: uma matriz de textos (`etl_inputs.read_raw`), já com as células neutralizadas contra fórmula. Os cálculos não sabem de que formato o arquivo veio.

| Formato | Quem lê | Orçamento de células |
|---|---|---|
| `.csv` (texto delimitado) | `etl_inputs._csv_input` | linhas × colunas |
| `.xlsx` | `etl_inputs._xlsx_input` (conferência do pacote, `openpyxl` e `pandas`) | área da aba (linhas × colunas, vazias incluídas) |
| `.xls` do Excel 97-2003 (BIFF8) | `etl_biff.ler` | **células com valor** |
| `.xml`, Planilha XML 2003 do Excel | `etl_planilha_xml.ler` | **células com valor** |

### 2.1 A mesma convenção de texto nos três formatos de planilha

`etl_inputs._texto_tipado` escreve o valor tipado do `.xls` e da Planilha XML como o leitor do `.xlsx` já escrevia: inteiro sem ".0", decimal como o Python o escreve (com um zero a mais quando parece milhar em português, "19.4310"), data como "2023-11-09 00:00:00", vazio como ausente. Há teste que grava os oito relatórios fictícios nos três formatos e exige a mesma matriz (`FormatosTests`).

O texto do `.xls` perde o NUL (U+0000), como no leitor de `.csv`. O `.xls` é o único dos formatos de planilha que guarda esse caractere, e com um NUL antes do `=` a fórmula escapava da neutralização (seção 10, revisão final).

### 2.2 O leitor de `.xls` (`etl_biff.py`)

- Abre o contêiner OLE2 e lê **só o fluxo "Workbook"**, uma vez, registro a registro. Projeto de macros, objeto embutido e propriedades do documento não são abertos. É o "expurgo em memória" que a diretriz pediu: nada além de valores de célula sai do arquivo, e ele não é gravado em lugar nenhum.
- De uma célula com fórmula vale o **valor guardado**; a expressão não é lida.
- Recusa, com mensagem fixa: o que não é OLE2, Excel 5.0/95 e anteriores, arquivo com senha, arquivo truncado, cadeia de setores em ciclo ou cruzada, registro maior que o formato permite.
- Limites aplicados durante a leitura: abas, linhas, colunas, células com valor e caracteres por célula, mais o prazo do processo isolado.
- Não conhece banco, Flask nem configuração.

### 2.3 O leitor de Planilha XML (`etl_planilha_xml.py`)

- Lê em fluxo, com o `expat`, uma passada.
- Recusa `DOCTYPE`, entidade e notação no primeiro sinal: não existe entidade externa nem bomba de entidades.
- Recusa o XML que não é planilha e diz o que ele é: página HTML, documento fiscal (NF-e, NFC-e, CT-e), imagem SVG ou parte de um `.xlsx` desmontado.
- **Não havia amostra real desse formato.** O leitor foi escrito pela especificação e testado com arquivos montados pelos testes. O primeiro arquivo real de um cliente merece conferência.

### 2.4 Onde cada formato é aceito

| Fluxo | O arquivo é guardado? | Formatos |
|---|---|---|
| Envio das análises mensais (`etl_uploads`) | sim, depois da triagem | `.csv` e `.xlsx`, como antes |
| Raio-X do cliente e Avaliação de Cenário (`raio_x_servico.processar_envio`) | não: lido no processo isolado e descartado | `.csv`, `.xlsx`, `.xls` e `.xml` |

O `.xls` e o `.xml` ficaram fora do envio das análises mensais porque lá a Baia de Segurança **grava** o arquivo liberado (o próprio ou a cópia só com valores), e ela só sabe gravar `.csv` e `.xlsx`. Habilitar os dois lá é uma decisão de produto à parte (seção 10).

Na Baia (`security_gatekeeper.inspecionar_arquivo`), o parâmetro novo `leitura_direta=True` é o que libera os dois formatos. Sem ele, tudo fica como estava. Com ele:
- `.xls` precisa ser um OLE2 com o fluxo da pasta de trabalho;
- `.xml` precisa começar como XML, trazer o elemento `Workbook` no espaço de nomes da planilha e não ter `DOCTYPE` nem entidade;
- o conteúdo continua tendo de bater com a extensão: HTML com extensão `.xls` (a "exportação para Excel" de muitos sistemas web) continua em quarentena. A recusa do arquivo `.xls` que a Baia não reconhece como planilha do Excel 97-2003 (a página HTML, o texto delimitado, o `.xlsx` renomeado, outro documento do Office) agora diz o que fazer: abrir no Excel, salvar como `.xlsx` e enviar de novo. A frase não afirma que o arquivo não é planilha, porque a Baia também não reconhece uma planilha verdadeira montada de um jeito incomum (seção 10).

### 2.5 Células com valor, e não a área

O relatório paginado espalha poucas células por muitas colunas. A venda item a item real tem 26.928 linhas × 18 colunas, mas só 157.444 células com valor. No `.xls` e na Planilha XML, o teto de 500.000 células por aba conta as células com valor. Como a matriz devolvida é retangular, a área também tem teto: quatro vezes o orçamento de células (`etl_inputs._AREA_POR_CELULA`). O leitor confere esse teto **antes** de montar a matriz: um arquivo de poucos bytes com duas células distantes é recusado sem ocupar a memória da área, na validação e na leitura. O `.xlsx` continua contando a área, como antes.

## 3. Leiaute paginado: título numa coluna, dado em outra

No relatório de venda item a item, as mesclas do ERP deixam o título "Produto" numa coluna e os nomes em outra. `etl_processor._alinhar_relatorio_paginado` resolve assim:

1. acha a forma mais comum das linhas de dado (as colunas preenchidas), somada às formas que só diferem dela por uma célula em branco (o vendedor ou o documento vazio em parte das linhas);
2. só segue se a coluna do título do produto não tem dado nenhum no corpo, se há **tantas colunas com título quanto colunas com dado** e se cada título fica entre a coluna de dado anterior e a seguinte;
3. passa cada dado para a coluna do título correspondente, pela ordem;
4. faz a prova de leitura: em ao menos 90% das linhas de item, o produto tem de ser texto e a quantidade e o valor têm de ser números (o texto que começa por letra, como "CX 12", não é número);
5. esvazia as linhas a que falta o produto, a quantidade ou o valor: o cliente do bloco, os rodapés, os totais e o frete, e também a linha de item com a quantidade ou o valor em branco.

Se qualquer conferência falha, o motor não adivinha: a matriz volta como veio e o relatório é recusado, como antes. Relatório em que título e dado já estão na mesma coluna não é tocado: no sorteio de 4.100 arquivos fictícios da revisão final, nenhum relatório de vendas que era lido certo mudou de número.

**Limite conhecido.** As conferências pegam a coluna a menos compensada por outra a mais quando isso põe um número no lugar do nome, ou um texto que começa por letra no lugar da quantidade ou do valor. Não pegam o caso em que o que cai em cada lugar tem o tipo esperado: uma coluna com título e sem dado em quase todas as linhas (o vendedor sempre em branco) e, ao mesmo tempo, uma coluna sem título ao lado. Se ela é de **texto** e fica ao lado do nome do produto (a unidade, a marca), o texto vira o nome do produto: o total do relatório sai certo, e a lista de produtos, errada. Se ela é de **número** e fica ao lado da quantidade ou do valor, o número vira a quantidade ou o valor, e com ele o que sai da quantidade e do valor (o texto que começa por dígito, como "12 UN" ou "14:35", também conta como número). Os títulos e os dados ficam em mesmo número e na mesma ordem. A matriz lida não traz as mesclas do relatório, e só elas diriam qual dado é de qual título. A revisão final montou os casos sobre o relatório fictício; no relatório real medido, os sete títulos têm dado e não há coluna sem título (seção 10).

## 4. Proteções: o que era aceito e errado

| Caso | Antes | Agora |
|---|---|---|
| Relatório de vendas abaixo do custo, com dois títulos "Valor Unitário" (compra e vendido) | o de compra virava o preço de venda; no arquivo real, R$ 8,2 milhões em vez de R$ 3,9 milhões | recusado, com o motivo na tela: "Arquivo N: o relatório tem dois títulos de coluna iguais para um valor...". Coluna de percentual ("% Valor Total", da curva ABC) não conta como título repetido |
| "Preço Compra" à esquerda de "Preço Venda" (o mesmo dano, em outra grafia), ou um segundo título de custo ("Valor Total Custo") à esquerda do total | o título de compra ficava com o preço unitário, e o faturamento saía de quantidade × preço de compra; o segundo custo ficava com o total da venda | título de compra ou de custo não é o preço nem o total da venda. A coluna de compra fica sem campo (não é lida como custo), e a de venda é a que vale |
| Coluna de percentual à esquerda da coluna do valor ("% Valor Total", "Faturamento (%)") | ficava com o valor da venda; o faturamento saía da soma dos percentuais (100,00) | a coluna de percentual não fica com campo nenhum. É de percentual o título com um "%" que não vem depois de um número; o "%" depois de um número é parâmetro do título ("Valor Total (-5% desc.)", "Preço c/ ICMS 18%"), e a coluna continua sendo a do valor, como era lida |
| Coluna de data com palavra de valor no título ("Data Faturamento", "Dt. Últ. Custo") | ficava com o valor da venda ou com o custo, e as vendas saíam zeradas | a coluna cujo título começa por "Data" ou "Dt.", em qualquer das linhas do cabeçalho, não fica com campo numérico. Na linha de cima de um cabeçalho de duas linhas, o rótulo com o valor dele não é título de coluna: é a emissão ou o período do relatório, com a data ou a hora na própria célula ("Data: 05/02/2026") ou na próxima célula preenchida ("Data de emissão" e, ao lado, "05/02/2026") |
| Relatório de vendas enviado no campo de compras da análise mensal | aceito, com "total comprado" que não existe | recusado quando o título é de relatório de vendas ("Giro de vendas por fornecedor", "Vendas por produto") ou quando há coluna que só existe em vendas ("Qtde Vend", "Total Venda") e nenhuma de compras. O relatório que se diz de compras ou de entradas nunca é recusado por isso |
| Relatório por forma de pagamento na Baia de Segurança | um aviso do rodapé e a linha de baixo eram lidos como cabeçalho e o arquivo era liberado | quarentena: frase com mais de 8 palavras, sozinha na linha, não é título de coluna. Título comprido ao lado de outros títulos continua valendo. No cabeçalho de duas linhas, a frase sozinha vale quando a outra linha tem três células ou mais: é o título do relatório sobre a linha de títulos, que já era lido assim (o limite dessa exceção está na seção 10) |
| `.csv` com nome de produto com vírgulas | o separador do arquivo inteiro virava a vírgula, porque valia a linha de mais colunas | vale a largura típica das linhas, e a vírgula só fica com o arquivo quando nenhum outro separador organiza a tabela (seção 4.1) |
| `.csv` lido pela vírgula com `;`, tabulação ou barra vertical dentro de uma célula da linha de títulos | o cabeçalho partido ainda podia dar produto, quantidade e valor, e os números saíam de pedaços das células | recusado, com a orientação de abrir no Excel e salvar como `.xlsx` (seção 4.1) |
| Percentual numa coluna de quantidade ou de estoque | "-99,97 %" entrava como -99,97 | não é número |
| Item de custo implausível no Raio-X | a "perda abaixo do custo" passava do faturamento inteiro | o item sai da perda e do recuperável (seção 5) |
| Período do cabeçalho diferente das datas das vendas | não era conferido | na Avaliação de Cenário, o arquivo de vários anos com venda fora do período do cabeçalho é recusado com o motivo; o de um ano só continua lido como antes (seção 7) |

### 4.1 O separador do `.csv`

Cada separador candidato (`;` `,` TAB `|`) tem uma **largura típica**: a maior quantidade de campos que mais de um quinto das linhas com mais de um campo alcança, e nunca uma linha sozinha quando há mais de uma. As linhas de texto que o separador não divide ficam fora da conta. Vence a maior largura típica; no empate, quem a alcança em mais linhas e, depois, a ordem de sempre (`etl_inputs._typical_width` e `_choose_separator`).

A vírgula tem uma condição a mais. Num relatório brasileiro ela é quase sempre a vírgula decimal: cada número com decimais dá à linha uma "coluna" a mais pela vírgula, e uma vírgula no nome do produto ("ESMALTE 3,6L") dá outra. Numa tabela em que todas as colunas, menos a do nome, têm vírgula decimal, a vírgula empata com o separador de verdade, ou passa dele por uma coluna. Por isso ela perde para outro separador em dois casos:

- ele tem a mesma largura típica (ou, numa tabela de três colunas ou mais, uma coluna a menos) e a alcança em ao menos metade das linhas que a vírgula alcança;
- ele alcança a largura típica dele em mais que o dobro das linhas da vírgula, com qualquer largura (um bloco grande de "rótulo;valor" antes de uma tabela de dois produtos).

Quando o outro separador só aparece em poucas linhas (um `;` no nome de dois fornecedores de um `.csv` separado por vírgula) ou organiza bem menos colunas (um `;` dentro de um campo de texto), a vírgula fica.

Nos dois casos, o outro separador tem de organizar ao menos um registro melhor do que a vírgula (`_SeparatorShape.beats_comma`):

- um registro que ele separa, em dois campos ou mais, e que não tem vírgula nenhuma; ou
- uma linha inteira da tabela dele (com a largura típica dele, ou mais) que ele separa em mais campos do que a vírgula separaria, e que a vírgula não leva à largura típica dela.

Num relatório com vírgula decimal, esse registro é a linha de títulos: "Produto;Qtde;Valor Total", ou "Produto;Qtde, Un;Valor Total", com uma vírgula dentro de um título. Num `.csv` separado por vírgula de verdade, o `;`, a `|` ou a tabulação de dentro de um campo de texto ("organico;torrado;moido", "CAMISETA | AZUL | M") aparece em registros que a vírgula separa em tantos campos quanto ele, ou em mais, e a vírgula fica com o arquivo.

**Conferência depois da leitura.** Quando o arquivo fica com a vírgula e a linha de títulos tem `;`, tabulação ou `|` dentro de uma célula, o separador de verdade é esse outro: é o relatório com `;` em que os títulos têm tantas vírgulas quanto as linhas de dado. Lido pela vírgula, o cabeçalho partido ainda podia dar produto, quantidade e valor em colunas diferentes, e os números saíam de pedaços das células, sem aviso (isso já acontecia antes da unificação). A leitura recusa (`etl_processor._recusar_separador_duvidoso`), nas vendas e nas compras, com a orientação de abrir o arquivo no Excel e salvar como `.xlsx`. Num `.csv` separado por vírgula de verdade, o outro separador fica nos dados, e não nos títulos.

A regra foi corrigida três vezes. A primeira versão desempatava pela contagem de linhas, e a vírgula decimal ganhava de relatórios legítimos com `;`, TAB ou `|`: foi a regressão mais séria que a revisão final achou. A segunda, sem a condição do registro, tirava da vírgula o `.csv` separado por vírgula com outro separador dentro de um campo (verificação das correções). A terceira só aceitava o registro sem vírgula nenhuma, e devolvia à vírgula o relatório com `;` em que um título tem vírgula (verificação da terceira rodada, seção 10.1). Conferência da regra final, com a leitura de antes da unificação como referência:

| Sorteio de arquivos fictícios | Certos antes | Certos agora | Certos antes e errados agora |
|---|---|---|---|
| Revisão final: 1.500 tabelas estreitas (3 a 6 colunas, quase tudo com vírgula decimal) | 1.042 | 1.298 | 0 |
| Revisão final: 1.000 relatórios gerais em `.csv` | 795 | 892 | 0 |
| Verificação: 20.000 relatórios com ponto e vírgula, tabulação ou barra vertical e vírgula decimal | 14.134 | 19.791 | 15 |
| Verificação: 6.000 `.csv` separados por vírgula, com outro separador dentro de campos | 5.762 | 5.904 | 5 |
| Verificação: 12.000 arquivos de 1 a 5 linhas | 10.201 | 11.828 | 7 |
| Terceira verificação: 4.500 relatórios com ponto e vírgula, tabulação ou barra vertical e vírgula decimal, dois terços deles com vírgula em algum título | 3.255 | 4.233 | 7 |
| Terceira verificação: 3.000 relatórios de PDV, metade deles com vírgula num título | 2.423 | 3.000 | 0 |
| Terceira verificação: 2.000 relatórios com ponto e vírgula, tabulação ou barra vertical, sem vírgula nos títulos | 1.297 | 1.888 | 0 |
| Terceira verificação: 4.500 `.csv` separados por vírgula, com outro separador dentro de campos | 4.299 | 4.417 | 8 |
| Terceira verificação: 2.000 `.csv` separados por vírgula, com uma linha curta com o outro separador | 1.584 | 1.582 | 45 |

Nos sorteios da revisão final e da terceira verificação, a medida é a leitura inteira (linhas, quantidade e valor iguais à verdade do gerador); nos três da verificação, o separador escolhido pelo leitor. Os sorteios das verificações foram montados para forçar a regra, e os arquivos que eram lidos e deixaram de ser têm as formas descritas na seção 10: são recusas, e nenhuma leitura com número errado vem do separador. Na primeira versão da regra, 70 arquivos do primeiro sorteio que eram lidos tinham passado a ser recusados; na segunda, 382 do quarto; na terceira, 24 do sexto.

## 5. Custo a confirmar

A triagem do lead já tratava como erro de cadastro o produto cujo custo é de 3 vezes a venda para cima (embalagem cadastrada como unidade, item genérico vendido por valor). O Raio-X não tinha a regra. Agora tem, com a mesma razão (`raio_x.RAZAO_CUSTO_IMPLAUSIVEL`, e um teste que a mantém igual à padrão da triagem) e a mesma conta, em centavos inteiros: em ponto flutuante, o custo de exatamente 3 vezes a venda podia ficar de fora no Raio-X e dentro na triagem.

- o item sai de `auditoria.perda_abaixo_custo`, da lista de vendas abaixo do custo, de `recuperavel` e da perda de cada fatia;
- entra em `auditoria.custo_a_confirmar`: quantos itens, a venda com custo e o custo deles, a perda desconsiderada e a lista;
- o custo total, a margem e a contagem de itens com margem negativa **continuam com o item**;
- um aviso diz quantos produtos ficaram de fora e pede a conferência do cadastro;
- a tela de auditoria de custos mostra a contagem, a perda desconsiderada e a lista.

`raio_x_servico.visao` protege o bloco como o resto. A lista, com o custo de cada item, é da auditoria de custos, como a lista de vendas abaixo do custo: sem ela no plano, a lista não sai. Os totais de custo e de venda com custo do bloco são do diagnóstico de CMV, como o custo total da auditoria. A contagem e a perda desconsiderada saem para todos, como a perda abaixo do custo. Raio-X gravado antes desta versão abre normalmente, sem o bloco.

No relatório resumido real, 8 itens ficam a confirmar e R$ 1.100.573,76 deixam de ser apresentados como perda; a perda que sobra é de R$ 126,48.

**Uma diferença que ficou.** A contagem de itens abaixo do custo do lead (`triagem_cenario`) continua incluindo os itens de custo implausível. No Raio-X eles saíram de `itens_venda_abaixo_custo`. Vale a conta: contagem do lead = `itens_venda_abaixo_custo` + `custo_a_confirmar.itens` do Raio-X do mesmo envio. Igualar as duas mexe na triagem gravada dos leads e pede versão nova da triagem; ficou para decisão sua.

## 6. Fechamento dos centavos

O Raio-X guarda cada venda em centavos. O ERP dos relatórios reais informa o total de cada item com quatro casas ("3,8081"), porque rateia o desconto do pedido entre os itens. Arredondar cada linha dava R$ 710.717,09: R$ 1,14 abaixo do total do relatório e do que a análise mensal calcula, que soma sem arredondar a linha.

`raio_x._fechar_centavos` resolve sem abandonar os centavos por linha:

- só vale para o total **informado** pelo relatório; valor calculado pelo motor (quantidade × preço unitário) continua arredondado por linha, como o PDV cobra;
- cada linha é arredondada como sempre;
- a diferença entre a soma das linhas arredondadas e a soma do relatório é distribuída, um centavo por linha, só entre as linhas que o arredondamento tinha levado para o outro lado: nenhuma fica a um centavo ou mais do valor informado;
- entre elas, vêm primeiro as linhas em que um centavo não muda o preço por unidade além da tolerância do scanner (quantidade de ao menos 1, ou valor de ao menos R$ 2,00); depois, a de maior resto; no empate, a linha de cima;
- a linha de um centavo só devolve o centavo quando não há outra. Com zero centavo ela continua sendo venda analisada, mas passaria a contar também como bonificação, e o custo dela entraria duas vezes no CMV líquido;
- sem fração de centavo, ou sem diferença, nada muda. Por isso os testes de referência não mudaram.

A conta é feita em milionésimos de real, com inteiros. Com fatias mensais, o fechamento é do arquivo, antes de fatiar.

**Limites conhecidos.** O total do relatório fica sempre certo; o que pode mudar é o centavo de uma linha:

- o fechamento é do arquivo, e não de cada produto. O centavo que sobra do arredondamento de um produto pode ir para a linha de outro, ou sair dela. Dois valores informados a exatamente 1 centavo um do outro (3,8149 e 3,8049) podem ficar a 2 centavos (3,82 e 3,80) e aparecer no scanner como variação de preço; um item vendido exatamente ao custo pode ficar 1 centavo abaixo dele e contar como margem negativa. Numa simulação da revisão final com 5 relatórios de cerca de 6.000 linhas, a diferença foi de 0 a 1 produto e de R$ 0,00 a R$ 0,44 no vazamento de preço;
- num relatório só com vendas de balança de valor baixo (0,2 kg por R$ 0,7651, várias vezes), não há linha "segura" para receber o centavo: algumas linhas iguais ficam com R$ 0,76 e outras com R$ 0,77. O arredondamento por linha de antes tinha o mesmo efeito entre pesos diferentes;
- a linha com valor **informado** abaixo de meio centavo e com custo já contava como venda analisada e como bonificação antes da unificação, e continua assim.

## 7. Fatias mensais na Avaliação de Cenário

**Quando fatia** (`raio_x._fatias_mensais`), só com `cadastro=True`, que é o modo da Avaliação de Cenário:
- o relatório tem coluna de data da venda: "Data" ou "Dt.", sozinho ou seguido de venda, emissão, movimento, pedido, documento, NF, nota, cupom, lançamento ou faturamento, ou "Emissão". Outras datas ("Data Validade", "Dt. Última Compra") não contam, e com duas colunas de data vale a primeira;
- o período do cabeçalho passa de um mês;
- ao menos um quinto das linhas de venda repete o produto (é venda item a item, e não um resumo com uma data qualquer). Repetir é ter o mesmo nome, o mesmo código, quando o relatório traz o código do produto, e a mesma loja, quando traz a loja; a linha de devolução não conta. Assim o resumo com uma linha por código e o mesmo nome em todos os tamanhos (a grade de uma confecção) e o resumo por produto e loja não são fatiados, e a venda item a item de catálogo largo, em que a maior parte dos produtos vende uma vez só, é. A coluna lida como código só entra na conta quando é mesmo a do produto: menos de um quinto dos códigos com mais de um nome (o código do pedido, à esquerda do produto, tem vários);
- ao menos uma linha tem data legível.

**O que sai:** um relatório por mês do calendário do período do cabeçalho, cortado pelo período, **com venda ou sem**. O mês sem venda continua sendo período coberto: os dias dele contam no ritmo, e outro arquivo do mesmo mês continua sendo sobreposição. Só ficam de fora os meses sem venda que ainda não começaram (hoje é 10 de fevereiro e o cabeçalho vai até março). Para o resto do motor, cada fatia é um relatório mensal como os de sempre: ritmo, giro, mesmos meses e cobertura funcionam sem mudança.

**Data inconsistente** (alguma linha de venda sem data legível, com as outras datadas; venda com data fora do período do cabeçalho; ou venda com data em mês que ainda não começou, como um pedido programado ou uma data digitada errada):
- no arquivo de **um ano só**, que já era lido como um relatório, nada muda: ele não é fatiado e continua um relatório;
- no arquivo que **cruza o ano**, que a Avaliação de Cenário já recusava, a recusa passa a dizer o motivo (código `raio_x_datas`).

A venda do mês corrente com data depois de hoje não é inconsistência: o arquivo é fatiado, e ela conta no mês corrente.

**Tetos.** Continuam valendo 36 arquivos por envio. As fatias têm teto próprio, `raio_x.MAX_FATIAS`: 60 (5 anos de meses) na unificação e **120 (10 anos) desde os indicadores executivos** (`Docs/INDICADORES_EXECUTIVOS.md`, refatoração 2). O contrato do processo isolado (`etl_runtime._meta_valida`) aceita até esse teto; as restrições do banco já aceitavam.

**Um envio que era aceito e passou a ser recusado.** Antes, cada arquivo de um ano civil era um relatório, e cabiam até 36. Agora o arquivo anual de venda item a item vira 12 relatórios mensais, e o envio em que os relatórios somam mais de 60 depois de fatiados é recusado: seis arquivos anuais somavam 72. A recusa dizia "Envie no máximo 5 anos por vez"; com o teto de 120, são onze arquivos anuais (132), e a recusa diz "Envie no máximo 10 anos por vez". O resumo anual sem data por linha continua valendo como um relatório. Fora esse caso, não conheço arquivo que era aceito e passou a ser recusado por causa das fatias.

**O que o consultor vê.** As mensagens continuam com o número do arquivo enviado ("Arquivo 1"), e não o da fatia. A tela do lead mostra "37 relatório(s)" para um arquivo de 37 meses, com a explicação ao passar o cursor.

**O Raio-X do cliente não fatiava.** Na unificação, um arquivo continuava uma fatia, e o "vazamento de preço" do Raio-X do cliente dependia de como o arquivo foi fatiado (seção 10). **Desde os indicadores executivos (raio-x-2026-11.6), ele também fatia por mês** a venda item a item com a data de cada venda, como a Avaliação de Cenário, mas sem recusar a data inconsistente: o arquivo com data ilegível, fora do período ou com mais de 120 meses continua um relatório só, como sempre foi aceito (`Docs/INDICADORES_EXECUTIVOS.md`, refatoração 4).

## 8. O que os motores compartilham

| Regra | Análise mensal | Raio-X do cliente | Avaliação de Cenário |
|---|---|---|---|
| Formatos `.csv` e `.xlsx` | sim | sim | sim |
| Formatos `.xls` e `.xml` | o motor lê; o envio não aceita (seção 2.4) | sim | sim |
| Leiaute paginado, títulos repetidos, percentual, título de compra, de custo e de data, separador do `.csv`, frase que não é título | sim | sim | sim |
| Texto neutralizado contra fórmula na leitura | sim | sim | sim, e de novo depois da limpeza do texto que vai para a staging (descrição, código e categoria) |
| Total que fecha com o relatório | sim (soma sem arredondar) | sim (`_fechar_centavos`) | sim (`_fechar_centavos`) |
| Custo implausível | não se aplica (a análise mensal não calcula margem) | sim, a partir de 3 vezes a venda | sim: 3 vezes no resumo do Raio-X do lead; na triagem, a razão configurável dos limiares |
| Fatias mensais pela data da venda | não | não | sim |

As três leem pelo mesmo caminho: `etl_inputs.read_raw` e `etl_processor._vendas_do_bruto`. As definições de coluna (código, EAN, produto, quantidade, valor, custo, estoque) são as do dicionário `ALIAS_VENDAS`, um só.

## 9. Conferência com os relatórios reais

Os oito relatórios "Giro de Vendas" da auditoria, no `.xls` original, lidos pelo motor depois da unificação. Só números agregados; nenhum nome de cliente, loja, produto ou documento entrou no repositório.

| Relatório | Linhas × colunas | Células com valor | Resultado |
|---|---|---|---|
| Venda item a item | 26.928 × 18 | 157.444 | lido: R$ 710.718,23, 21.172 linhas, 904 produtos; 37 relatórios mensais e 1.950 linhas produto-ano na Avaliação de Cenário |
| Resumido por produto | 2.049 × 18 | 8.578 | o Raio-X lê: R$ 1.460.035,77 (o número contado por documento, ver seção 10), 912 produtos, 8 itens a confirmar. A Avaliação de Cenário recusa: período de vários anos sem data por linha |
| Vendas abaixo do custo | 128 × 18 | 257 | recusado, com a orientação dos títulos repetidos |
| Por fornecedor | 150 × 17 | 138 | recusado: não é relatório de vendas por produto |
| Por grupo de produto | 232 × 17 | 429 | recusado: idem |
| Por grupo de cliente | 59 × 16 | 31 | recusado: idem |
| Por forma de pagamento | 65 × 19 | 47 | recusado: idem |
| Ranking de clientes | 58 × 19 | 45 | recusado: idem |

**Tempo e memória da venda item a item**, neste computador: validação 0,8 s; Raio-X 5,6 s; Avaliação de Cenário 6,6 s. O pico de memória do processo subiu de 75 MB (só com as bibliotecas carregadas) para 117 MB depois das três leituras.

**O que isso não prova.** O limite de produção é de 384 MB de espaço de endereçamento (`RLIMIT_AS`) e 120 segundos, num servidor Linux mais lento que este computador. Aqui é Windows, onde o processo isolado não roda com esses limites. A folga medida é grande (6 s e 40 MB a mais), mas a medida no Render não foi feita.

## 10. O que ficou de fora e por quê

| Item | Por quê |
|---|---|
| Tirar a contagem em dobro do relatório **resumido** por produto | O arquivo traz uma linha por produto, já somada com o pedido e cada nota. Não há documento por linha para deduplicar. O resumido sozinho continua lido com R$ 1.460.035,77, sem aviso: o motor não tem como saber. A orientação é enviar a venda item a item |
| Aviso automático de "resumido inflado" | Exigiria ler o número do documento e cruzar os dois relatórios. Fica para a fase seguinte da proposta da auditoria |
| `.xls` e `.xml` no envio das análises mensais | A Baia grava o arquivo liberado, e só sabe gravar `.csv` e `.xlsx`. É decisão de produto, com mudança no Guia e no NexoBot |
| HTML com extensão `.xls` | É outro formato (uma página web). Continua em quarentena, agora com a orientação de abrir no Excel e salvar como `.xlsx` (a recusa vinha só com a mensagem padrão) |
| Fatias mensais no Raio-X do cliente | Feito nos indicadores executivos (raio-x-2026-11.6): o histórico por plano e a linha do tempo do cliente passam a ser contados em meses |
| Custo e estoque do relatório por produto ligados à venda item a item | Feito na ingestão em lote (11/2026, `Docs/INDICADORES_EXECUTIVOS.md`, seção 13): com a venda item a item com data no envio, o relatório por produto do mesmo período vira tabela auxiliar, ligada pelo código ou, sem ele, pela descrição normalizada, e não soma no faturamento |
| Cliente, vendedor, documento e forma de pagamento | Campos novos, contrato novo e telas: fases 4 e 5 da proposta da auditoria |
| Orçamento de células do `.xlsx` | Continua pela área. O relatório detalhado convertido para `.xlsx` pode passar do teto; o `.xls` original não passa |
| Igualar a contagem de "abaixo do custo" do lead à do Raio-X | Mexe na triagem gravada (seção 5) |
| Medida no Render | Só em produção ou num Linux com os mesmos limites (seção 9) |

**Limites que as revisões acharam e ficaram registrados**, sem correção nesta entrega. Os que já existiam antes da unificação estão marcados:

| Limite | O que acontece |
|---|---|
| Relatório de **compras** enviado no campo de **vendas** (já existia) | Não há a proteção espelho da seção 4. Com títulos neutros ("Quantidade", "Vlr Unitário"), o relatório de compras por item é lido como vendas. O relatório de compras com a coluna "Dt. Faturamento" saía com as vendas zeradas, porque a data ficava com o valor da venda: o Raio-X e a Avaliação de Cenário recusavam por falta de venda, e a análise mensal calculava faturamento 0,00. Com a coluna de data fora dos campos numéricos, ele cai nesta regra e é lido como vendas, com um número plausível |
| Total com título de compra, sem outro total | "Valor Total da Compra" não fica com o total da venda (seção 4). Quando ele é o único total do relatório e há preço unitário, o faturamento sai de quantidade × preço unitário, sem aviso de que o total informado ficou de fora; antes da unificação, a coluna era lida como o total da venda. Recusar esse relatório, em vez de calcular, é decisão de produto, e ficou para decisão sua. O título que diz venda e custo ("Preço Venda (Custo + Margem)") também fica sem o campo da venda: o relatório que só tem essa coluna de valor é recusado |
| Título de valor com "%" | É de percentual, e fica sem campo, o título com um "%" que não vem depois de um número. O título de valor escrito assim ("Valor Total c/ Desc. (%)") fica sem o campo: o faturamento sai de quantidade × preço unitário, quando há, ou o relatório é recusado. No sentido contrário, a coluna de percentual com um número antes do "%" ("Custo s/ Venda 100%", à esquerda de "Custo") é lida como a coluna do valor, como antes da unificação. A coluna de percentual não fica com campo nenhum, nem o do produto: quando o "%" está no título do relatório, sozinho na linha de cima de um cabeçalho de duas linhas ("Curva ABC de produtos (% do faturamento)" sobre "Item;Qtde;Valor Total"), o "produto" que vinha dele se perde, e o relatório que era lido é recusado |
| Rótulo de data do topo sem o valor ao lado | Na linha de cima de um cabeçalho de duas linhas, a célula que começa por "Data" ou "Dt." só é tratada como rótulo quando a data ou a hora está nela ou na próxima célula preenchida. "Data Inicial" seguida de "Data Final", ou o rótulo sem valor nenhum na linha, continua valendo como título de data, e a coluna de baixo fica sem campo numérico: o relatório é recusado, ou o faturamento sai de quantidade × preço unitário, ou o custo deixa de ser lido. Só acontece quando o cabeçalho é reconhecido pelo par de linhas (a coluna do nome se chama "Item" ou "Nome", e o "produto" vem do título do relatório); antes da unificação esse relatório era lido |
| Frase com a palavra "produto" colada a uma linha de três células (já existia) | A exceção da frase (seção 4) só conta as células da outra linha do par. Um aviso, ou o título do relatório, com a palavra "produto", sozinho na linha e colado a uma linha com títulos de quantidade e de valor, completa o cabeçalho, e a coluna em que a frase está vira a do produto: o relatório por grupo ou por vendedor é lido como se fosse por produto, e com "Cód." e "Item" sob o título do relatório os códigos viram os nomes. É o que a leitura de antes da unificação fazia; a regra mais estrita recusava relatórios legítimos, e foi a regressão que a revisão final achou |
| Separador do `.csv`: formas que a regra não resolve | Ficam com a vírgula, e o relatório é recusado: (1) o relatório pequeno com `;` em que as linhas de texto com vírgulas, no topo, passam do dobro das linhas da tabela; (2) a tabela com `;` em que a última coluna só vem em 20% a 50% das linhas e os nomes têm vírgula em mais que o dobro delas; (3) a tabela em que a metade dos nomes, ou mais, tem duas vírgulas ou mais, quando isso dá à vírgula duas colunas ou mais além do separador de verdade (esta já existia, salvo quando a linha de títulos é tão larga quanto as linhas pela vírgula: aí a leitura de antes da unificação acertava); (4) o relatório com `;` em que os títulos têm tantas vírgulas quanto as linhas de dado e nenhum registro fica sem vírgula, recusado pela conferência da linha de títulos (antes da unificação era lido quando as larguras empatavam, e saía com número errado quando a vírgula tinha mais colunas). No sentido contrário, o `.csv` separado por vírgula com uma linha de topo que só o outro separador divide ("Loja: 01; Caixa: 02; Operador: ADMIN"), num arquivo de poucas linhas ou com o outro separador, duas vezes ou mais, em metade dos nomes ou mais, é lido pelo outro separador e recusado. Nos sorteios da verificação das correções, montados para forçar a regra, são 27 arquivos em 38.000 que a leitura de antes da unificação acertava (seção 4.1) |
| Relatório de vendas no campo de compras com título fora da lista (já existia) | A recusa da seção 4 conhece "Relatório de Vendas", "Vendas por..." e parecidos. Com "Relatório Analítico de Vendas", "Curva ABC de Vendas" ou "Faturamento por Produto" no título e colunas que servem aos dois, o arquivo continua aceito como compras |
| Dois títulos iguais quando o total da venda é único | A recusa vale também para dois "Preço" ou dois "Custo" ao lado de um único "Valor Total". O faturamento sairia certo; o preço de tabela ou o custo, não. É recusa por segurança, com a orientação de enviar o relatório por produto |
| Coluna de preço de compra | Deixou de ser lida como preço de venda, mas não é lida como custo: o arquivo fica "sem coluna de custo" |
| Resumo com coluna "Data" que as fatias não reconhecem como resumo | As fatias reconhecem o resumo por produto (com ou sem código) e o resumo por produto e loja. Ainda seria fatiado, por uma data que não é a da venda: o resumo por produto e por outra dimensão (vendedor, ou a loja numa coluna chamada "Empresa"), e o resumo sem coluna de código em que um quinto das linhas ou mais repete o nome. O total do envio sai certo; a divisão por mês, não |
| Venda item a item em que menos de um quinto das linhas repete o produto | Não é fatiada: o arquivo de um ano só é lido como um relatório (sem a divisão por mês), e o que cruza o ano é recusado com a orientação de enviar por mês ou por ano civil. É o que acontecia antes das fatias. Acontece no negócio de peça única (joalheria, móveis sob medida, veículos) e no arquivo curto de um catálogo muito largo; a frase da tela do lead ("pode vir num arquivo só de vários anos: o NEXO separa por mês") não vale para esses arquivos |
| Coluna de data com outro começo de título | "Últ. Faturamento" ou "Atualização do Custo", que não começam por "Data" nem por "Dt.", continuam ficando com o valor da venda ou com o custo, como antes da unificação |
| Leiaute paginado com coluna de texto sem título | O limite descrito na seção 3 |
| Centavo de uma linha | Os limites descritos na seção 6 |
| Onze arquivos anuais de venda item a item | Passam do teto de 120 relatórios (seção 7; era 60, com seis arquivos) |
| Neutralização de fórmula: tempo e pilha (já existia, em todos os formatos) | Uma célula montada de propósito com milhares de tabulações seguidas leva segundos para ser neutralizada (o tempo cresce com o quadrado do tamanho da célula), e uma célula com mil restos de BOM derruba a leitura com erro genérico. O processo isolado é cortado aos 120 segundos e a vaga volta; há limite de envios por hora por usuário; nenhum relatório real dispara. A correção mexe na regra da Baia de Segurança e pede testes próprios |
| Número absurdo (1e308) em valor ou custo (já existia) | Erro genérico do processamento, em vez de recusa com mensagem |
| `.xls` com a pasta de trabalho fora do primeiro setor do diretório (já existia) | A Baia olha só o primeiro setor do diretório do contêiner. Os gravadores testados (o provedor ACE da Microsoft e o LibreOffice 26.2) põem a pasta de trabalho lá; um arquivo montado de outro jeito é recusado, agora com orientação |
| Planilha XML fora do comum | A Baia decide pelos primeiros 8 KiB do arquivo. É recusada, com a orientação de salvar como `.xlsx`, embora o leitor a lesse, a Planilha XML que não começa pela declaração XML nem pelo elemento `Workbook` (começa pela instrução `<?mso-application?>` ou por um comentário), a que tem mais de 8 KiB antes do elemento `Workbook`, a que traz o texto `<!DOCTYPE` num comentário ou num bloco CDATA, e a que está em UTF-16 com muitos caracteres fora do alfabeto latino. O Excel não grava assim |
| VERDADEIRO e 1 na mesma coluna de um `.xlsx` (já existia) | O leitor do `.xlsx` dá um texto só para os dois, e o número 1 pode ser lido como "True". Os leitores de `.xls` e de Planilha XML convertem célula a célula e não têm o problema |

### 10.1 As revisões independentes

**A primeira revisão, durante a implementação.** Dois agentes escreveram os leitores de `.xls` e de Planilha XML, com as suítes deles, e um terceiro fez a revisão adversarial da mudança no motor. O limite semanal de uso da conta interrompeu os três. Os leitores já estavam entregues e com todos os testes passando. A revisão tinha achado quatro regressões (separador do `.csv`, título de percentual, compras legítimas recusadas e título comprido) e deixado prontos os roteiros de três frentes (alinhamento, centavos, fatias e custo a confirmar), que rodei: eles acusaram mais sete problemas, e a correção deles mostrou mais um (o arquivo sem venda válida e com coluna de estoque derrubava o processo). Os doze estão corrigidos e têm teste. Ficou sem um revisor independente a integração dos dois leitores com o motor e com a Baia de Segurança (ponto 7 do roteiro da revisão), que tem os testes de `FormatosTests` e os da Baia, escritos por mim. Na conferência final dessa integração, que fiz sozinho, apareceu mais um ponto, o décimo terceiro: na leitura do motor (`etl_inputs.read_raw`), o teto da área só era conferido depois de o leitor montar a matriz. Chamada direto, a leitura de um `.xml` de 559 bytes com duas células distantes ocupava 104 MB antes da recusa (68 MB num `.xls` de 6 KB). Pelo portal isso não acontecia: o processo isolado valida cada arquivo antes de lê-lo (`etl_worker`), e a validação não monta a matriz. Mesmo assim, o leitor passou a conferir a área antes de montar a matriz, para o limite não depender da ordem das chamadas, com teste nos dois leitores e na leitura do motor. Essa integração foi coberta depois, pela revisão final.

**A revisão final, antes do commit.** Com os agentes de volta, a mudança inteira passou por uma revisão adversarial independente, em quatro frentes. Cada revisor tinha de reproduzir o que relatasse. Cinco dos achados mais graves passaram por um verificador encarregado de tentar derrubá-los (três se sustentaram); os do teste de mutação eu conferi rodando os testes que o revisor deixou.

| Frente | O que fez |
|---|---|
| Leitores de `.xls` e de Planilha XML, com a Baia e o motor | 21.600 arquivos `.xls` corrompidos de propósito e 3.685 arquivos com valores hostis: nenhuma exceção crua nos leitores. 181.683 células de arquivos `.xls` gravados pelo provedor ACE da Microsoft comparadas com a leitura do próprio provedor: nenhuma diferença além das de propósito (o texto "NA" vira vazio e a fórmula ganha o apóstrofo). O verificador gravou seis `.xls` com o LibreOffice: todos passam pela Baia e pelo portal |
| Antes e depois | 4.100 arquivos fictícios lidos pelo código de antes da unificação e pelo novo, com cada leitura comparada à verdade do gerador |
| Cálculo | 300 envios fatiados conferidos contra uma conta independente, sem diferença; 1.500 relatórios no fechamento dos centavos, sempre com a soma do relatório |
| Teste de mutação | 145 trocas de uma regra do motor por vez. 71 faziam algum teste falhar, 13 não mudavam o resultado e **61 passavam sem nenhum teste perceber** |

Ela relatou 43 pontos (2 de gravidade alta, 17 média e 24 baixa, na conta dos revisores). Corrigidos, cada um com teste:

1. o separador do `.csv` (seção 4.1), a regressão mais séria: relatório legítimo com `;` que era lido passava a ser recusado. A correção resolveu também um caso que já era recusado antes da unificação (muitos nomes com uma vírgula, sem a coluna de código);
2. o NUL antes da fórmula no `.xls`, que escapava da neutralização (seção 2.1);
3. "Data Movimento", que nunca era reconhecida como data da venda, e "Data Faturamento", que ficava com o valor da venda (seções 4 e 7);
4. a linha com data em mês que ainda não começou, que fazia recusar o arquivo inteiro com a mensagem apontando o cabeçalho (seção 7);
5. o resumo por produto com um nome repetido e uma coluna "Data", que era fatiado pela data errada (seção 7);
6. a prova de leitura do alinhamento, que recusava os 90% exatos (seção 3);
7. o custo de exatamente 3 vezes a venda, decidido em ponto flutuante (seção 5);
8. a linha de um centavo que o fechamento zerava e que passava a contar também como bonificação (seção 6);
9. a coluna de percentual à esquerda da coluna do valor e o título de compra ou de custo à esquerda do de venda, que ficavam com o valor da venda. Os dois já existiam antes da unificação, e o segundo é o dano do relatório de vendas abaixo do custo em outra grafia (seção 4);
10. o título do relatório, comprido, em cima da linha de títulos, que derrubava um cabeçalho que era lido (seção 4);
11. o texto que a Avaliação de Cenário grava na staging, que podia voltar a começar por `=` depois da limpeza (seção 8). Também já existia;
12. a recusa do `.xls` que é página HTML, que vinha sem orientação (seção 2.4).

Ficaram registrados, sem correção, os limites da tabela acima.

**Os testes que faltavam.** Os 44 testes que o revisor do teste de mutação deixou prontos entraram na suíte, cinco deles adaptados às regras corrigidas, ao lado dos testes dos defeitos. O módulo `tests/test_cobertura_leiautes_erp.py` foi de 73 para 133 testes.

**A verificação das correções.** As doze correções mexeram em regras do motor. Antes do commit, passaram por uma verificação adversarial independente, em três frentes (leitura, cálculo e documento): cada revisor comparou o código corrigido com o de antes das correções e com o de antes da unificação, e tinha de reproduzir o que relatasse. Ela relatou 16 pontos. Oito dos mais graves passaram por um verificador encarregado de tentar derrubá-los, e quatro se sustentaram como defeito a corrigir. Os mais importantes eram regressões das próprias correções, ou casos que elas não alcançavam:

1. a regra do percentual (item 9) tirava o campo de todo título com "%", inclusive do título de valor com a taxa ou o desconto ("Valor Total (-5% desc.)"), e o faturamento passava a sair de quantidade × preço unitário, sem aviso. Agora o "%" depois de um número é parâmetro do título (seção 4);
2. a regra do separador (item 1) tirava da vírgula o `.csv` separado por vírgula com `;` ou `|` dentro de um campo de texto, e o relatório passava a ser recusado. A correção foi exigir que o outro separador separe um registro sem vírgula nenhuma, e ela mesma foi corrigida na rodada seguinte (seção 4.1);
3. o critério das fatias (item 5), que era a metade das linhas com o nome repetido, deixava sem fatiar a venda item a item de catálogo largo e fatiava o resumo de uma confecção, com o mesmo nome em todos os tamanhos. Agora é um quinto das linhas, com o código do produto e a loja na conta (seção 7);
4. a regra da coluna de data (item 3) falhava com a linha do período logo acima de "Data Faturamento", e a emissão do relatório no topo ("Data: 05/02/2026") tirava o campo da coluna de baixo. A exceção do rótulo também foi corrigida na rodada seguinte (seção 4);
5. a prova de leitura do alinhamento (item 6) aceitava "CX 12" como número. Agora o texto que começa por letra não é número (seção 3);
6. a orientação do `.xls` (item 12) não valia para o texto delimitado nem para o `.xlsx` com a extensão `.xls`, e afirmava que o arquivo não é planilha (seção 2.4);
7. afirmações deste documento e do documento da Baia de Segurança que não batiam com o código.

Cada correção tem teste. O que a verificação achou e não foi corrigido está na tabela de limites: o total com título de compra, a frase colada a uma linha de três células, as formas do separador que a regra não resolve, o resumo que as fatias não reconhecem e a venda item a item com pouca repetição. Sobre o primeiro deles, o revisor sugeriu recusar o relatório e o verificador respondeu que isso é decisão de produto: ficou para decisão sua.

**A verificação da terceira rodada.** As correções da verificação passaram por mais uma, em duas frentes (código e documento), com as mesmas regras. Ela relatou 15 pontos. Quatro iam para um verificador; o limite semanal de uso da conta interrompeu dois deles, e os dois que terminaram sustentaram os achados, com gravidade baixa. O que ela achou de mais importante:

1. a condição do registro sem vírgula (item 2 da lista acima) devolvia à vírgula o relatório com `;` em que um título tem vírgula ("Qtde, Un", "Cidade, UF"). O relatório que era lido passava a ser recusado; quando os títulos tinham tantas vírgulas quanto as linhas de dado, o cabeçalho partido ainda dava as colunas, e saíam números feitos de pedaços das células, sem aviso. Agora vale também a linha da tabela que o outro separador divide em mais campos do que a vírgula, e o `.csv` lido pela vírgula com outro separador na linha de títulos é recusado (seção 4.1). A regra adotada não é a que o revisor sugeriu: o verificador mostrou que aquela reabria a regressão do `.csv` separado por vírgula, e a regra final junta as condições dos dois;
2. a exceção do rótulo de data (item 4) só valia para a célula com dois-pontos ou com dígito. O rótulo com a data na célula ao lado ("Data de emissão" e "05/02/2026") continuava tirando o campo da coluna de baixo, e o título de data de verdade com dois-pontos ou com número ("Data Faturamento:") voltava a ficar com o valor da venda. Agora decide a data ou a hora, na própria célula ou na célula ao lado (seção 4);
3. imprecisões deste documento e do documento da Baia de Segurança: os limiares da linha do separador na tabela de limites, a definição da largura típica, a descrição do motivo `documento_ole`.

**O que a quarta rodada de correções não teve.** Com o limite semanal esgotado, as correções desta última rodada não passaram por revisão independente. Eu as conferi sozinho, com os roteiros e os sorteios que os revisores deixaram (abaixo). Eles cobrem as formas que os revisores acharam; uma forma nova, que nenhum deles sorteou, não seria pega. A regra do separador foi corrigida três vezes, e em cada vez a revisão seguinte achou uma regressão da correção anterior: vale conferir os primeiros arquivos `.csv` reais que derem recusa.

**Conferência final, sobre o código que vai para o commit:**

- o sorteio de 4.100 arquivos da revisão final: nenhum arquivo de vendas que era lido certo antes da unificação deixou de ser (os únicos recusados a mais são 43 arquivos do sorteio de compras, pela proteção da seção 4 e por linhas de título postas de propósito pelo gerador);
- os sorteios do separador das duas verificações, 54.000 arquivos lidos pelo leitor de verdade: a tabela da seção 4.1. Nenhum arquivo que a rodada anterior lia certo deixou de ser lido, e nenhuma leitura com número errado vem do separador;
- a triagem do envio mensal em 11.285 desses arquivos, com uma fórmula a neutralizar: a cópia neutralizada é relida com o mesmo separador em todos;
- o sorteio de 3.000 relatórios com rótulo de data no topo, do verificador: resultado idêntico ao da regra que ele ensaiou e recomendou. Dos relatórios que a leitura de antes da unificação acertava, ficam sem leitura certa os 48 do limite registrado (o rótulo sem o valor ao lado);
- os oito relatórios reais: os mesmos resultados da seção 9, número a número;
- o teste de mutação, repetido sobre o código final: 226 trocas (as da revisão que ainda se aplicam e as novas, sobre o código das correções). 214 fazem algum teste falhar. As 12 restantes são as que a revisão já tinha conferido como equivalentes: não mudam o resultado.

**O que nenhuma revisão cobriu.** O processo isolado de verdade, que só roda em Linux, com os limites de memória e de tempo: aqui ele é emulado pelos testes. Uma Planilha XML 2003 real. O `.xls` gravado por outros programas (o próprio Excel, geradores de relatório de ERP). As telas no navegador, além do que os testes de navegador já exercitam.

## 11. Testes

- `tests/test_cobertura_leiautes_erp.py`: os oito testes de lacuna da auditoria perderam o `expectedFailure` e passam (`LacunasResolvidasTests`). O módulo tem ainda as garantias, as recusas que continuam e as regras novas: leiaute paginado (com os casos em que ele não pode alinhar), fechamento dos centavos, fatias mensais, formatos, separador do `.csv` e os relatórios legítimos que as proteções não podem recusar. Depois da revisão final ganhou um teste para cada defeito corrigido e os testes que o teste de mutação pediu, e depois de cada verificação, os testes das correções dela: são 144.
- `tests/test_leads_postgresql.py`: uma etapa nova grava no PostgreSQL real o lead do `.xls` de venda item a item, fatiado por mês.
- `tests/test_etl_biff.py` e `tests/test_etl_planilha_xml.py`: os dois leitores, com valores, leiaute, limites, recusas e arquivos corrompidos de propósito. Os arquivos são montados pelos próprios testes (`tests/xls_support.py` e `tests/planilha_xml_support.py`), sem biblioteca de planilha.
- `tests/leiautes_erp_support.py`: os relatórios fictícios agora também em `.xls` e em Planilha XML.
- `tests/test_raio_x_regression.py` e `tests/test_cadastro_regression.py`: os conjuntos históricos saem idênticos. Mudanças aprovadas, cada uma com o motivo no teste: a versão `raio-x-2026-11.5` e o campo `auditoria.custo_a_confirmar`, que vem zerado em todos os conjuntos.
- `tests/test_planos_raio_x.py`: o custo a confirmar (com a ordem e o limite da lista, e os totais que não podem sair como `-0.0`), o que cada plano vê dele, o Raio-X antigo sem o bloco e o envio em `.xls` e em Planilha XML pelo portal.
- `tests/test_avaliacao_cenario.py`: o `.xls` de vários anos enviado pela tela do lead e o teto de relatórios do contrato (120 desde os indicadores executivos).
- `tests/test_security_gatekeeper.py`: a leitura direta (o que passa e o que continua recusado), cada condição da Planilha XML recusando sozinha e a orientação do `.xls` que não é planilha.
- `tests/test_etl_resource_limits.py`: o teste que guardava o `.xls` desabilitado passou a guardar que só o `.xls` verdadeiro entra.

## 12. Implantação

- **Banco:** nenhuma migração. As restrições `ck_lead_avaliacao_arquivos` e `ck_diagnostico_entrada_arquivos` só exigem ao menos 1 arquivo.
- **Dependências:** nenhuma nova.
- **Versão das regras:** `raio-x-2026-11.5`. O Raio-X gravado com versões anteriores continua abrindo.
- **Arquivos novos do motor:** `etl_biff.py` e `etl_planilha_xml.py`, na raiz, ao lado do `etl_inputs.py`. O processo isolado os importa de lá.
- **Telas:** os campos de arquivo do Raio-X e do lead aceitam `.csv`, `.xlsx`, `.xls` e `.xml`.
- **Leads já avaliados:** não mudam. Um reenvio usa as regras novas.
