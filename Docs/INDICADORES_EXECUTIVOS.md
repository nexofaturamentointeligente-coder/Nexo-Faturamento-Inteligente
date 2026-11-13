# Indicadores executivos do NEXO (21) e as cinco refatorações do motor

Este documento descreve a expansão do motor ETL pedida em 11/2026: 21 indicadores executivos, dois modos de leitura (analítico e acumulado) e cinco refatorações. Versões das regras: Raio-X `raio-x-2026-11.6`, triagem do lead `triagem-2026-11.4` e indicadores `indicadores-2026-11.2` (era a 11.1 até a auditoria matemática de 11/2026, seção 14). A migração nova é a `c5f8a1d3e7b9`, sobre a `9b3e5d7f1a28`.

## 1. Onde os indicadores ficam

O resultado do Raio-X ganhou o nó `indicadores_executivos`. O Raio-X do cliente e a Avaliação de Cenário do lead usam a mesma função, `raio_x._combinar`, e por isso recebem o mesmo nó.

```
"indicadores_executivos": {
  "versao": "indicadores-2026-11.2",
  "modo": "analitico" | "acumulado",
  "parametros": {"margem_minima": 0.15, "dias_encalhe": 60, "aliquota_impostos": null, "limite_lista": 50},
  "indicadores": { 21 chaves fixas, cada uma com "status" e os valores }
}
```

- **Cálculo.** O cálculo mora em `indicadores_executivos.py` e roda no processo isolado do ETL. O que só existe linha a linha (número da venda, CPF/CNPJ, desconto, data e hora) é somado ali dentro (`resumo_das_linhas`), e do processo isolado só sai o agregado.
- **Cliente.** O nó fica no JSON do Raio-X, em `DiagnosticoEntrada.resultado`. O Raio-X gravado antes da 11.6 continua valendo, sem o nó (`raio_x.CHAVES_OBRIGATORIAS`).
- **Lead.** O nó fica na coluna nova `lead_avaliacao.indicadores_executivos_json` e sai no expurgo de 30 dias (seção 8).

Cada indicador traz `status`. Quando falta coluna ou dado, o indicador não sai: nenhum número é estimado.

| Status | Quando |
|---|---|
| `calculado` | o indicador saiu |
| `requer_item_a_item` | o envio não é venda item a item, ou falta o número da venda em alguma linha |
| `requer_custo` | nenhum relatório traz a coluna de custo (com a coluna em branco, ou com custo só no item a confirmar, é `sem_dados`) |
| `requer_estoque` | nenhum relatório traz a coluna de estoque |
| `requer_cpf_cnpj` | algum relatório de venda item a item não traz o CPF/CNPJ do cliente |
| `requer_parametro` | a margem líquida pede a alíquota de impostos, que ainda não é parâmetro |
| `requer_relatorio_de_compras` | saving: os relatórios de venda não trazem o preço de compra |
| `requer_mais_de_um_mes` | evolução do custo e mapa por produto e mês pedem dois meses ou mais |
| `requer_periodo` | encalhados e cobertura pedem o período no cabeçalho |
| `requer_coluna_de_desconto` | o relatório não traz o desconto por linha |
| `requer_cadastro_de_produtos` | o percentual do mix pede um relatório que liste também o item sem venda |
| `sem_dados` | a coluna existe, mas não há valor que entre na conta: coluna de custo em branco, desconto em percentual ("10%") ou absurdo, estoque de dois arquivos do mesmo período (seção 3), ou um número fora do que o contrato aceita (o indicador sai `sem_dados`, e o resto do Raio-X fica) |

## 2. Modos

- **Analítico.** Todo relatório do envio é venda item a item, pelo detector de sempre (`raio_x._e_venda_item_a_item`: ao menos 1/5 das linhas repete o produto, com o mesmo código e a mesma loja).
  - Com o número da venda em todas as linhas, saem o ticket médio, os itens por venda e a recompra.
  - Com a data de cada linha, sai o mapa de calor por dia da semana, e por hora quando o relatório traz a hora.
- **Acumulado.** Há relatório resumido por produto no envio.
  - Ticket, itens por venda e recompra saem com `requer_item_a_item`.
  - Os indicadores de estoque, cadastro e margem valem nos dois modos, quando o relatório traz a coluna.

## 3. Os 21 indicadores

"Custo a confirmar" é o item com custo a partir de 3 vezes a venda com custo, a mesma conta em centavos da auditoria do Raio-X. Ele é erro de cadastro, não prejuízo: fica fora de todo indicador de custo e margem, e aparece só nas contagens `itens_custo_a_confirmar`.

| # | Chave | Definição | Precisa de | Plano (funcionalidade) |
|---|---|---|---|---|
| 1 | `faturamento` | Venda analisada (quantidade e valor maiores que zero, fora devolução), a mesma de `saude.faturamento_total`, fechada no centavo com o total do relatório; traz também as devoluções | — | todos |
| 2 | `ticket_medio` | Faturamento / vendas distintas (o número da venda dentro do arquivo e, com duas lojas ou mais no arquivo, dentro da loja: cada loja e cada arquivo numeram as suas; a linha com a loja em branco logo abaixo do mesmo número é da loja do cupom). Com a data de cada venda, a venda é o número naquela data: a numeração que recomeça (por dia, caixa ou série) não junta vendas de dias diferentes | modo analítico e número da venda | todos |
| 3 | `margem` | Bruta, a clássica: (receita líquida − CMV) / receita líquida, em R$ e em fração, nunca sobre o custo (isso é o markup). A receita é a venda com custo, já líquida do desconto da linha, menos as devoluções com custo; o CMV, o custo dessas vendas menos o custo das devoluções. Sem o custo a confirmar. Traz também a venda com custo bruta, as devoluções e o custo delas. Líquida (de impostos): `requer_parametro` | custo | `margem_bruta` |
| 4 | `giro_estoque` | Do período: CMV (líquido de devolução) / estoque atual a custo médio. Anualizado: o CMV dos relatórios com a coluna de custo e com período × 365 / os dias que eles cobrem (`dias_do_periodo`), sobre o mesmo estoque. O CMV de parte do ano não é anualizado pelo ano inteiro, e o relatório sem período, sem dias, não entra | custo e estoque | `diagnostico_cmv` |
| 5 | `mix` | Produtos vendidos; % sobre os cadastrados quando o relatório lista também o item sem venda (o "vendas x estoque") | — | todos |
| 6 | `itens_por_venda` | PPA: unidades / vendas distintas (e linhas / vendas). Com quantidade fracionada (o granel em kg ou litro), `quantidade_fracionada` avisa que a soma não é de peças, e `linhas_por_venda` é a leitura em itens | modo analítico e número da venda | todos |
| 7 | `recompra` | Clientes com mais de uma venda / clientes identificados por CPF ou CNPJ válido (também o CNPJ alfanumérico de 2026) | modo analítico e CPF/CNPJ em todo relatório | `curva_abc_clientes` |
| 8 | `saving` | Sempre `requer_relatorio_de_compras` | relatório de compras | `diagnostico_cmv` |
| 9 | `evolucao_custo` | Custo unitário de cada produto no primeiro e no último mês com custo; altas e quedas de 5% ou mais e a variação ponderada pela quantidade do último mês | custo e dois meses | `alerta_precificacao` |
| 10 | `cmv` | Custo das vendas menos o custo das devoluções (a mercadoria volta ao estoque), sem o custo a confirmar; o custo dele sai à parte (`custo_a_confirmar`), e o custo das vendas e o das devoluções também. A bonificação fica fora (é o CMV líquido da auditoria que a soma). Sem custo plausível nenhum, `sem_dados` | custo | `diagnostico_cmv` |
| 11 | `curva_abc_faturamento` | 80/15/5 pelo faturamento líquido de devolução de cada produto (a base, 100%, é a soma dos positivos), a regra da análise mensal (`_classificar_curva_abc`: A até 80% do acumulado, B até 95%, C até 100%, líder sempre A; no empate, a ordem do nome). A participação de cada item e de cada classe sai do valor: os empatados têm a mesma | — | `curva_abc_vendas` |
| 12 | `curva_abc_volume` | A mesma curva pela quantidade líquida de devolução | — | `curva_abc_vendas` |
| 13 | `curva_abc_lucro` | A mesma curva pela margem bruta positiva, líquida de devolução; a margem negativa ou zero e o custo a confirmar ficam fora da curva | custo | `margem_bruta` |
| 14 | `margem_negativa` | Sem o custo a confirmar, duas leituras: `itens`, `perda` e `lista` pelo produto no envio inteiro (custo acima da venda somando os meses, em centavos); `por_mes`, `matriz` (produto x mês com `impacto_q4`, refatoração 5), `perda_q4` e `perda_nos_meses` mês a mês. O produto negativo num mês e positivo no envio entra só na segunda | custo | `auditoria_abaixo_custo` (as contagens, todos) |
| 15 | `margem_abaixo_minimo` | Produtos com margem de 0% até abaixo de 15% (a negativa é o 14) | custo | `margem_bruta` |
| 16 | `indice_descontos` | Desconto / (faturamento + desconto), com o valor da linha já líquido do desconto: o desconto nunca sai duas vezes. Com o total bruto e o "Valor Líquido" no relatório, a venda é o líquido. O desconto em percentual ou absurdo, ou sobre o valor derivado do preço unitário (sem a coluna do total), dá `sem_dados` | coluna de desconto em reais | todos |
| 17 | `ruptura` | Produtos com venda no período e último estoque informado ≤ 0 | estoque | `produtos_parados_ruptura` |
| 18 | `encalhados` | Estoque > 0 e mais de 60 dias cobertos por relatório sem venda até o fim do envio; valor a custo médio. A lista vai do mais parado para o menos (e não pelo valor, que o Bronze não vê) | estoque e período | `produtos_parados_ruptura` (o valor a custo, `diagnostico_cmv`) |
| 19 | `cobertura_estoque` | Por produto, em unidades: estoque / venda média diária (a quantidade dos relatórios com período / os dias cobertos), que é o estoque a custo / o CMV médio diário do produto (o custo unitário se cancela): mediana, faixas (até 15, 16 a 60, 61 a 180, acima de 180 dias) e lista. Agregada da loja (`dias_agregado`): estoque a custo / (CMV / dias), na base do giro, o mesmo que 365 / giro anualizado. `itens_com_estoque_sem_venda` conta o item parado, que fica fora da mediana | estoque e período | `produtos_parados_ruptura` |
| 20 | `gmroi` | Margem bruta (a do indicador 3) / estoque médio a custo: a média das fotos do estoque (uma por relatório, cada uma com o último saldo de todo produto até ali), quando toda foto traz todo produto com estoque e custo do estado final; senão (o relatório que só lista quem vendeu, o arquivo fatiado), o estoque final. `base` diz qual valeu. `valor` é o do período; `anualizado`, a margem dos relatórios com custo e com período × 365 / os dias deles, sobre o mesmo estoque: o GMROI clássico, comparável entre envios de tamanhos diferentes | custo e estoque | `margem_bruta` (o estoque médio a custo, `diagnostico_cmv`) |
| 21 | `mapa_calor` | Analítico com data: faturamento e vendas por dia da semana (0 = segunda) e hora. Senão, faturamento por produto e mês (os 20 maiores), com todo mês coberto, também o sem venda, e `impacto_q4` nos meses 10, 11 e 12 | data por linha ou relatórios mensais | todos |

Mais regras:
- **Encalhados.** Com a data de cada venda, os dias sem venda contam da data exata (`base_dos_dias = data_da_venda`). Sem ela, contam do fim do relatório em que o produto vendeu (`fim_do_relatorio`). Só contam os dias que algum relatório cobre: no mês sem relatório o produto pode ter vendido, e o encalhe tem de ser certo. O item listado só com estoque, sem venda no envio, conta todos os dias cobertos. O que vendeu num relatório sem período tem a última venda desconhecida e fica de fora, mesmo com venda datada em outro relatório.
- **Estoque.** O estoque de cada relatório é o último saldo de cada produto e loja, somado por produto: o depósito que só traz o saldo, com quantidade 0, também soma. A soma por loja só vale quando as linhas com saldo do arquivo trazem duas lojas ou mais com nome, decidido no arquivo inteiro, antes das fatias (`raio_x._marcar_as_lojas`). Com uma loja só, a linha com a célula da loja em branco (mesclada, ou preenchida só na primeira linha do grupo ou do cupom) é dessa loja, e vale o último saldo do produto. No arquivo fatiado por mês, o saldo de cada loja vem acumulado das fatias anteriores. Entre relatórios, vale o mais recente; o sem período conta como o mais antigo. Com dois arquivos de estoque no mesmo período (duas lojas enviadas separadas), o estoque da rede não é conhecido, e giro, ruptura, encalhados, cobertura e GMROI saem `sem_dados`.
- **Data da venda.** A coluna de data só vale para os indicadores (última venda, mapa por dia da semana, recompra por dia) na venda item a item com toda data legível, dentro do período e em mês que já começou (`raio_x._datas_validas`). Num resumo por produto, "Data" é o cadastro ou a última compra, e não entra.
- **Listas.** Têm no máximo 50 itens, com nome e código do produto.
- **Meses.** Só contam os relatórios mensais (início e fim no mesmo mês). O arquivo de venda item a item com a data de cada venda vira relatórios mensais (refatoração 4).
- **Devolução.** A linha devolvida (pela coluna de situação; sem ela, a linha com quantidade e valor negativos) fica fora da venda e conta à parte. O faturamento (indicador 1) é a venda, com as devoluções ao lado; a margem, o CMV, o GMROI e as curvas ABC são líquidos de devolução.
- **Anualização.** O giro e o GMROI anualizados usam a mesma base no CMV (ou na margem) e nos dias: os relatórios com a coluna de custo e com período, e os dias que eles cobrem. A cobertura usa a quantidade dos relatórios com período, sobre os dias cobertos.

## 4. Leitura nova (só no modo de cadastro: o Raio-X do cliente e o lead; a análise mensal só muda no travessão sozinho)

Pelo título inteiro da coluna, depois da data e da situação (`etl_processor._dado_da_venda`):

| Dado | Títulos aceitos | Fica de fora |
|---|---|---|
| Número da venda | "Nr. Doc.", "Pedido", "Cupom", "Nº NF-e", "Nota Fiscal", "Documento", "COO"; "Nº Venda" e "ID Transação" com o marcador de número | "Venda" sozinho (costuma ser valor), "Ticket" |
| CPF/CNPJ do cliente | lista de permissão: o título é só o documento ou diz de quem é (cliente, consumidor, comprador, destinatário, tomador, "Cli.", "Dest."). "Só o documento" é conferido por palavras: tiradas as que só marcam ou completam o documento ("Nº", "do", "na", "ou", "Doc.", "Identificação", "Informado", "Nota", "NFC-e", "Cupom", "ID", "Estrangeiro"; "Fiscal" só depois de "Nota", "Cupom" e "Documento"), sobra só "CPF" ou "CNPJ" ("CPF no Cupom", "CPF ou CNPJ", "Nº do CPF", "CPF na Nota Fiscal", "Identificação (CPF/CNPJ)", "CPF/CNPJ/ID Estrangeiro"). Com os dois no relatório, vale o do cliente | o do operador, do caixa, do fiscal de caixa ("CPF Fiscal", "CPF do Fiscal"), do vendedor, do representante, da loja, da matriz, do intermediador; o da unidade de negócio continua sendo a loja |
| Desconto em reais | "Desconto", "Vlr. Desconto", "Desconto R$", "Total Desconto" | o de percentual ("Desc. %"); "Desc." sozinho, que também abrevia descrição |
| Hora | "Hora", "Hora da Venda", "Horário" | — |
| Data com hora | "Data/Hora" e "Data e Hora" passam a ser a data da venda | — |

- **Número da venda.** É o primeiro trecho da célula que tem algarismo: em "P9-00001  NFC9-00001" (pedido e nota juntos, como o ERP exporta), é o pedido.
- **CPF/CNPJ.** Só conta o número com dígito verificador válido, também o CNPJ alfanumérico (IN RFB 2.229/2024, emitido desde julho de 2026: cada posição vale o código ASCII menos 48), este só quando a célula tem a forma dele ("12.ABC.345/01DE-35", "12ABC34501DE35", com espaço nos separadores ou com o traço tipográfico antes dos verificadores, "12.ABC.345/01DE–35"): o resto segue para os algarismos, e "CPF 315.942.594-02" e "315.942.594-02" são o mesmo cliente. Números repetidos ("000.000.000-00"), vazio e "consumidor final" não identificam cliente. O número que perdeu o zero à esquerda na célula numérica volta a ter 11 ou 14 dígitos.
- **Desconto.** O valor que não é em reais ("10%") ou que é absurdo (R$ 1 bilhão ou mais numa linha) deixa o índice em `sem_dados`, sem mexer no resto do Raio-X. O travessão sozinho ("—", "‒", "―", como "-" e "–") é a célula vazia, em toda leitura de números (vendas e compras, inclusive a análise mensal): sem o alerta de valor inválido. Antes do número, o travessão não é sinal: "— 40,00" continua lido como era em e468866 (valor inválido, com o alerta), e "— 4" na quantidade continua 4.
- **LGPD.** O CPF/CNPJ vira um resumo sha256 dentro do processo isolado e só serve para contar clientes. Nem o número nem o resumo saem do processo: o teste `test_recompra_conta_clientes_sem_guardar_o_documento` confere o resultado inteiro.
- **Recompra sem número da venda.** Sem o número, a recompra conta cada dia de compra do cliente como uma venda.

## 5. As cinco refatorações

1. **Relatório de compras na rota de vendas** (`etl_processor._recusar_relatorio_de_compras`). A recusa vale na análise mensal, no Raio-X e no lead, só com sinal inequívoco, e **nunca** quando o topo traz:
   - título de relatório de vendas: "Relatório de Vendas por Cliente", "Rel. Vendas por Produto", "Giro de Vendas", "Vendas x Compras", "Produtos Vendidos", "Produtos Mais Vendidos", "Curva ABC de Produtos Vendidos", "Vendas de Produtos", "Vendas do Período", "Vendas Diárias", "Pedidos de Venda", "Faturamento por Produto", "Relação", "Demonstrativo" e "Consulta de Vendas";
   - ou a compra do cliente, que é a venda da loja: "Compras por Cliente", "Histórico de Compras do Cliente", e "Histórico de Compras" com o rótulo "Cliente:" no topo.

   A palavra "Venda" solta, no nome da loja ("Venda Nova Supermercado", "Venda do Zé"), não é título de vendas. O rótulo de um filtro também não é: "Vendas por:" | "Todas", "Venda por: Todas", "Vendas x Compras:", "Vendas no Período: R$ 1.234,00" e "Histórico de vendas considerado: 90 dias" são parâmetros. Contam o rótulo do título ("Relatório: Vendas por produto") e o título com dois-pontos que começa pelo nome do relatório ("Relatório de Vendas: Outubro"), ou por "Vendas por" e "Faturamento por" com o que vem depois ("Vendas por Produto: Outubro").

   O qualificador entre o nome do relatório e o assunto não muda nada: "Relatório Analítico de Vendas", "Resumo Diário de Vendas" e "Relatório Gerencial de Vendas" são título de vendas; "Relatório Sintético de Compras" e "Relatório Mensal de Entradas", de compras (geral, analítico, sintético, detalhado, resumido, consolidado, gerencial, mensal, diário).

   Fora isso, qualquer um dos quatro sinais recusa:
   - **Título de compras.** Uma célula do topo **começa** por título de relatório de compras ou de entradas: "Relatório de Compras", "Entradas de Mercadorias", "Compras por Fornecedor", "Nota Fiscal de Entrada", "NF-e de Entrada", "Curva ABC de Compras", "Ranking de Compras", "Recebimento de Mercadorias", "Compras do Mês", "Entradas no Período", "Pedidos de Compra", "Ordens de Compra", "Mercadorias Recebidas", "Compras Realizadas", "Compras/Entradas", "Entrada de NF-e", "Consulta de Notas de Entrada", "Demonstrativo de Compras". Depois do rótulo do título ("Relatório:", "Rel.:", "Título:", "Tipo de Relatório:", "Nome do Relatório:"), a célula é o título: "Relatório:" | "Compras por Fornecedor" recusa. Não é título:
     - a compra do cliente (acima);
     - o valor de um filtro: a célula logo depois de um rótulo com dois-pontos que não é o do título ("Grupo:" | "ENTRADAS", "Setor:" | "Compras por Fornecedor"), ou o filtro na mesma célula ("Comprador: TODOS");
     - "Compras" ou "Entradas" sozinha fora da primeira célula do arquivo e do lugar do título (um grupo de produtos, uma seção);
     - palavra solta no nome da loja ("Supermercado Boa Compra", "Compra Por Menos", "Compra Na Hora") ou no endereço ("Av. Entrada da Cidade"): "Compras por" e "Compras no/na" só recusam com o complemento de compras ("por Fornecedor", "por Produto", "no Período");
     - "Ranking", "Curva ABC" e "Giro" de "Entradas": no restaurante, "Entradas" é grupo de pratos. Só "de Compras" recusa;
     - "Produtos Comprados" e "Itens Comprados": no comércio eletrônico, costumam ser o do cliente.

     A célula do título que fala também de venda ou saída ("Movimento de Entradas e Saídas") não recusa. "Venda Nova" no endereço, noutra célula, não salva o "Relatório de Compras".
   - **Só o total da compra.** O único total é o da compra, dito com todas as letras ("Valor Total da Compra", "Total Compra"), sem coluna de total da venda. "Vlr. Compra" e "Valor de Compra" ao lado do preço de venda podem ser o custo da unidade, e não recusam.
   - **Exportação de compras sem título, pela quantidade.** A coluna lida como quantidade da venda é a quantidade comprada ("Qtde Comprada", "Qtd. Compra"), há uma coluna própria da compra (fornecedor, custo, data ou nota de entrada) e nenhuma coluna que só existe em vendas ("Qtde Vendida", "Quant. Vendida", "Total Venda", "Valor Total de Venda"). Com outra coluna de quantidade lida ("Qtde" ao lado de "Qtde Compra"), é a venda com a compra informada. Sem a coluna da compra, "Quantidade Comprada" é a do cliente (exportação de pedidos), e não recusa. O custo do envio, do frete ou da entrega e a "Entrada" do crediário não são coluna da compra.
   - **Exportação de compras sem título, pela entrada.** O cabeçalho traz o fornecedor e a data ou a nota de entrada ("Data Entrada", "NF Entrada", "Nota Fiscal de Entrada"), e nenhuma coluna que só existe em vendas.

   Desde a quarta revisão:
   - a data da venda ("Data Venda", "Dt. da Venda") é coluna que só existe em vendas: a venda consignada, com o fornecedor (o consignante) e a data de entrada da peça, não é recusada. "Acerto de Consignação" e "Peças Vendidas" são títulos de vendas;
   - a coluna que só existe em vendas é conferida antes do total da compra, e o total da compra só recusa quando nenhum título é o total da venda pelo nome ("Total da Venda", "Valor de Venda", "Venda Total");
   - no cabeçalho de duas linhas, a linha de cima também é topo: o título colado ao cabeçalho ("Relatório de Compras por Produto - período ..." sobre "Item;Qtde;Valor Total") recusa;
   - o nome do relatório pode vir antes do assunto, com o qualificador: "Relatório de Pedidos de Compra", "Relatório de Recebimento de Mercadorias", "Relatório Analítico de Notas Fiscais de Entrada" recusam; "Relatório de Faturamento por Produto", "Relatório de Produtos Mais Vendidos" e "Relatório de Pedidos de Venda" são títulos de vendas;
   - o título de vendas com o período ou a hora na mesma célula ("Vendas x Compras - Período: ...", "Produtos Vendidos - Emitido em 11/11/2026 10:35", "Relatório de Vendas De: ... Até: ...") continua título: o dois-pontos da hora não é rótulo, e conta o trecho antes do separador (" - ", " | ");
   - o parâmetro sem dois-pontos, com o valor em reais ou com casas decimais na célula ao lado ("Vendas no Período" | "R$ 1.234,00"), é rótulo, e não salva o relatório de compras;
   - o rótulo do cliente vale também sem a preposição ("Cód. Cliente:", "Código Cliente:").

   Quando nem o cabeçalho de vendas é achado, o título de compras, ou o total da compra sem nada de venda nas primeiras linhas, troca a mensagem genérica pela de compras. A mensagem no Raio-X e no lead é exatamente "Arquivo N: o relatório enviado é de compras/entradas. Por favor, envie o relatório de vendas por produto." O preço unitário de compra ao lado do total da venda não recusa: é a venda com custo.
2. **Teto de 120 relatórios mensais** (`raio_x.MAX_FATIAS`, era 60). A mensagem passou a ser "Envie no máximo 10 anos por vez". O contrato da meta acompanha.
3. **Triagem do lead** (`triagem_cenario`). O custo a confirmar vale **por ano**: no ano em que o custo do produto é implausível, a perda sai:
   - da contagem `itens_venda_abaixo_custo`;
   - do motivo "abaixo do custo" e da marca `flag_venda_abaixo_custo`;
   - do vazamento estimado e do recuperável, que ficam só com a variação de preço;
   - da matriz por categoria do consultor (`diagnostico_cenario`).

   O outro ano do mesmo produto, com custo plausível, conta normalmente: a perda real não some porque um ano teve erro de cadastro. O produto continua com o motivo "custo implausível" e na contagem de custo a confirmar. A regra mora em duas funções (`recuperavel_do_ano` e `perda_real_do_ano`), usadas pelas marcas, pela triagem, pela matriz por categoria e pelos itens de cada bloco: a soma do recuperável dos itens de um bloco fecha com o resumo do bloco.

   Os limiares gravados pela 11.3 continuam valendo (`VERSOES_DOS_LIMIARES`, escrito por extenso: a cada troca de versão, a nova sempre entra; se algum limiar mudou de sentido, as anteriores saem; um teste confere que a atual está nele). O lead com a triagem da 11.3 aparece como "de outra versão" até ser retriado; os indicadores da tela continuam os da Etapa 2 (`VERSOES_COM_MARCAS_DA_ETAPA_2`), e os textos que dizem que o ano de custo a confirmar fica de fora só aparecem na 11.4, na tela e nas definições do `diagnostico.json` (`diagnostico_cenario.definicoes_da_versao`). A definição e os rótulos das marcas seguem `VERSOES_COM_MARCAS_DA_ETAPA_2`: no lead de antes da Etapa 2 (11.2 ou sem versão), a marca é o preço médio abaixo do custo médio, sem a omissão no ERP, na tela e no JSON.

   A diferença para o Raio-X: o Raio-X decide o custo a confirmar pela soma do envio inteiro, e a triagem, ano a ano, como já lia as linhas da staging.
4. **Raio-X do cliente por mês.** `raio_x.diagnosticar` lê como o lead (`cadastro=True`, até hoje) e fatia por mês a venda item a item com a data de cada venda. A diferença para o lead é que nada que o cliente já enviava passa a ser recusado:
   - com data inconsistente, o arquivo continua um relatório só;
   - o envio que passaria do teto de 120 meses é lido de novo sem fatiar nenhum arquivo, como antes das fatias (o cliente envia até 36 arquivos, como sempre). No teto, o arquivo que não é fatiado conta como um relatório: 120 meses e mais um arquivo de um mês passam do teto em qualquer ordem. O resultado não depende da ordem dos arquivos.

   O que muda para o cliente no arquivo de venda item a item de vários meses:
   - A linha do tempo e o corte de histórico por plano contam meses.
   - O resultado é o mesmo de enviar um relatório por mês (`test_venda_item_a_item_fatiada_igual_a_relatorios_mensais`), inclusive o preço de referência do scanner e o potencial recuperável, que passam a ser de cada mês. Um produto vendido a R$ 10 em janeiro e a R$ 8 em fevereiro tinha variação de preço no arquivo inteiro e deixa de ter mês a mês.
   - A tela mostra quantos arquivos o cliente enviou ("1 arquivo(s) de vendas"), e a linha do tempo fala em períodos ("Uma linha por período", "2 períodos anteriores ficam fora do histórico"). O banco grava os arquivos enviados (`DiagnosticoEntrada.arquivos`). Na API, a lista `arquivos` traz um item por mês (como as fatias) e perde os meses ocultos pelo plano; `saude.arquivos_com_custo` conta relatórios mensais.
5. **Margem negativa por produto e mês, com o 4º trimestre.** A matriz `margem_negativa.matriz` traz, por produto, a perda de cada mês (`AAAA-MM`) com `impacto_q4` nos meses 10, 11 e 12. `por_mes` traz o total do mês, e `perda_q4` a soma do trimestre. O item de custo a confirmar fica fora. A matriz é montada uma vez, no Raio-X, e o lead a recebe no mesmo nó.

## 6. O que cada plano vê

- **Funcionalidades.** `raio_x_servico.visao` troca o indicador que o plano não libera por `{"status": "protegido", "funcionalidade": ...}`, com a funcionalidade da tabela da seção 3. As contagens de margem negativa e de custo a confirmar ficam abertas, como na auditoria.
- **Valor a custo.** O valor dos encalhados é do diagnóstico de CMV (a lista em unidades é do Bronze). Sem o diagnóstico de CMV, a venda com custo dos itens a confirmar e o estoque médio a custo do GMROI também saem nulos. Na matriz de planos padrão, a margem bruta e o diagnóstico de CMV vêm juntos no Prata, e nada mais vaza. Fora dela (margem bruta sem o diagnóstico de CMV), o custo pode ser refeito com o que o plano já vê: a auditoria do Raio-X, anterior aos indicadores, mostra o custo e o custo médio de cada item a quem tem margem bruta. Proteger isso de verdade pediria esconder também esses custos da auditoria, o que muda um comportamento que já existia: é decisão do dono.
- **Corte do histórico.** Os meses anteriores ao corte do plano saem da margem negativa por mês, da matriz e do mapa por produto e mês. A perda total da linha é refeita só com os meses visíveis, as linhas são reordenadas por eles, e a linha que ficou vazia sai. A escolha dos 50 produtos da matriz e dos 20 do mapa continua sendo a do histórico inteiro.
- **Totais do envio.** Os totais continuam sendo do envio inteiro, como `saude` e `auditoria` sempre foram. A evolução do custo também não é cortada.

## 7. Contrato

O processo pai confere o nó inteiro antes de gravar, no cliente (`etl_runtime.run_raio_x`) e no lead (`etl_runtime.validar_avaliacao`), com `indicadores_executivos.valido`:
- chaves fixas e os 21 indicadores com status conhecido;
- números finitos;
- rótulos curtos;
- texto do relatório só no nome (até 120 caracteres) e no código do produto (até 60), sem caractere de controle;
- listas de até 3.000 itens.

Tipo inesperado (lista no lugar do status, por exemplo) é recusa, e não erro 500.

## 8. Lead: coluna nova, expurgo e migração

- **Gravação.** `avaliacao_cenario.registrar` grava o nó em `lead_avaliacao.indicadores_executivos_json`.
- **Expurgo.** O nó traz nome de produto e sai no expurgo de 30 dias, com as linhas de PDV:
  - a função do banco `expurgar_brainstorming_pdv` foi trocada por `CREATE OR REPLACE`, com a mesma assinatura, o mesmo retorno e as mesmas permissões;
  - o caminho em SQLAlchemy (`leads_avaliacao._expurgar_em_sqlalchemy`) faz o mesmo;
  - só perde os indicadores o lead não convertido que fica sem nenhuma linha recente.
- **Downgrade.** O downgrade da `c5f8a1d3e7b9`:
  - trava `usuario`, `lead_avaliacao` e `brainstorming_dados_pdv` no PostgreSQL;
  - recusa enquanto houver lead com os indicadores preenchidos;
  - devolve a função de expurgo anterior.
- **Tela antes da migração.** A tela de leads só abre com a coluna nova no banco (`blueprints/leads.esquema_disponivel`). Antes da migração, ela mostra o aviso de migração pendente, e não erro 500.

**Antes de testar no navegador:** aplicar a migração ao banco de desenvolvimento (`flask db upgrade`). Em produção, só com autorização, depois de conferir o backup.

## 9. Ensaio com os relatórios reais

Os relatórios reais passaram pelo processo isolado de verdade, no Windows. Só os agregados abaixo foram lidos; nenhum nome saiu.

| Relatório | Resultado |
|---|---|
| Venda item a item (15 MB, 37 meses) | Modo analítico. Faturamento R$ 710.718,23, igual ao de antes. Ticket médio R$ 65,76 em 10.808 vendas e 2,816 itens por venda, os números da auditoria de cobertura. Mapa por dia da semana (sem hora no relatório). Recompra `requer_cpf_cnpj`, desconto `requer_coluna_de_desconto` (o ERP traz o desconto só no rodapé do cliente). Cerca de 11 s. |
| Resumo por produto | Modo acumulado. Margem bruta de 45,62%, sem os 8 itens de custo a confirmar (R$ 1.113.587,00 de custo isolado). Giro anualizado de 1,34 e cobertura mediana de 506 dias, os números da auditoria com a quantidade inflada. GMROI pelo estoque final. |
| Ranking, por fornecedor, grupos e plano de pagamento | Recusados como antes (não são venda por produto). Nenhum foi tomado por relatório de compras. |
| Vendas abaixo do custo | Recusado como antes (dois títulos "Valor Unitário"). |

## 10. Limites e decisões pendentes

- **Margem líquida.** Pede a alíquota de impostos. Ainda não há onde configurar (empresa, lead ou plano?), nem a regra (alíquota única sobre o faturamento?). Até lá, sai `requer_parametro`.
- **Saving.** Sem relatório de compras, não sai. A evolução do custo usa o custo registrado nas vendas do PDV, não o preço da nota de compra.
- **Dados que o ERP real não traz.**
  - O CPF/CNPJ não vem na venda item a item: o cliente vem em linha de bloco, só com o nome. A recompra fica `requer_cpf_cnpj`, e contar pelo nome do bloco seria outra definição, com outra análise de LGPD.
  - O desconto vem só em rodapé ou na linha de totais. Ler o rodapé seria um leitor próprio desse leiaute.
  - A hora da venda não vem: o mapa de calor real sai só por dia da semana.
- **Leituras com limite.**
  - "Cód. Venda" continua lido como código do produto (o dicionário de vendas pega "cod "). O número da venda vem só das colunas da seção 4.
  - O percentual do mix só sai com relatório que lista item sem venda. O real lista só quem vendeu.
- **Venda item a item e resumo no mesmo envio.** O envio vira modo acumulado. Ligar os dois relatórios pela descrição continua fora (UNIFICACAO, seção 10).
- **Telas.** Os indicadores estão no resultado (JSON) e no lead, mas as telas do cliente e do consultor ainda não os mostram. Desenhar essa parte é o próximo passo.
- **Corte de histórico.** Não se aplica aos totais do envio nem à evolução do custo (seção 6). Os 50 produtos da matriz e os 20 do mapa são escolhidos pelo histórico inteiro, antes do corte.
- **GMROI.** Com relatórios que só listam quem vendeu (o ERP real), as fotos do estoque saem incompletas, e o GMROI usa o estoque final (`base = estoque_final`). O estoque médio de verdade pede o relatório "vendas x estoque", que lista todo produto em todo mês.
- **Capacidade do lead.** O nó vai no mesmo resultado do lead, com teto de 4 MiB: num envio perto do teto, ele ocupa de 5% (60 meses) a 9% (120 meses), e o envio que antes cabia rente ao teto pode passar a ser recusado como `avaliacao_grande`. Compactar a matriz da margem negativa ficou para depois.
- **Valor a custo fora da matriz padrão de planos.** Ver a seção 6.
- **Loja em branco com duas lojas ou mais.** No arquivo com duas lojas ou mais com nome, a linha com a célula da loja em branco continua contando como uma loja "sem nome" no estoque, como antes dos indicadores: pode ser a mesma loja da linha de cima (célula mesclada) ou outra coisa (a venda sem loja, o comércio eletrônico). Preencher pela linha de cima mudaria o sentido da célula vazia, e é decisão à parte. A numeração das vendas já herda a loja da linha de cima do mesmo cupom, também quando o primeiro item do cupom, o que traz a loja, é cancelado, devolvido, bonificado ou tem quantidade 0 (`etl_processor._loja_do_cupom`, no quadro inteiro, antes de a linha cancelada sair).
- **Recusa de compras.** Os nomes de loja que começam por "Venda e" ou "Vendas por" ("VENDA E CIA", "VENDA POR ATACADO") salvam o relatório de compras, como antes; o "Extrato" e a "Consulta de Compras" do cliente sem o rótulo "Cliente:" são recusados. As regras da seção 5 reconhecem os títulos e os leiautes listados ali. Um título de compras ou de vendas fora dessas listas ainda pode passar ou ser recusado: a recusa só vale com sinal inequívoco, e cada título novo pede teste contra a venda.

## 11. Testes

`tests/test_indicadores.py` (163 testes) cobre:
- o gabarito de Administração (`GabaritoAdministrativoTests`, seção 14): os 21 indicadores de um envio pequeno contra as contas feitas à mão, e uma prova para cada ajuste da auditoria matemática;
- a leitura nova;
- as cinco refatorações;
- os dois modos, com os valores conferidos à mão em envios fictícios;
- o contrato;
- os planos e o corte de histórico;
- cada achado das quatro revisões independentes (seção 12), nas três rotas quando o achado é da leitura (análise mensal, Raio-X do cliente e lead);
- a ingestão em lote (seção 13): a âncora com o relatório por produto, a posição de estoque, a lista de preços, o resumo por pedido e o relatório de compras, a outra loja e o outro período, a trava contra a duplicação e as réplicas dos leiautes reais do ERP;
- a classificação prévia e os leiautes reais do lote (seção 13), nas réplicas `AUXILIARES` de `tests/leiautes_erp_support.py`: cada leiaute classificado antes da trava de vendas, o que cada um dá, o lote nas duas rotas, o envio com todos os relatórios da pasta, o saldo entre as tabelas de estoque, as regras sem a âncora e o relatório de vendas que continua recusado (sem a coluna da quantidade, ou ambíguo fora do período da âncora), e cada achado da quinta revisão e da verificação do código final (o filtro do topo, os relatórios corrompidos, as lojas, a data, o custo, as âncoras, o código como chave, o estoque com o preço da unidade, a venda sem a linha do período, o saldo do relatório mais recente, as lojas sem a loja na âncora, a filial com "Geral", o relatório misto com título de estoque, o estoque a preço de venda, o valor ao lado do custo unitário, o alinhamento e as guardas);
- a gravação no lead, o expurgo e o downgrade.

Na quinta revisão, o revisor de testes rodou 20 mutações: 7 morreram e 13 sobreviveram, e cada uma ganhou teste. Depois das correções, um teste de mutação de 34 alterações rodou contra as duas classes do lote: as 20, com as âncoras do código novo, e 14 sobre as próprias correções. Morreram 29. Os testes reforçados mataram 4 das 5 vivas e uma alteração nova (a palavra de compra no título), e o total ficou em 34 de 35. A que sobrevive tira o título de compras numa outra linha do topo: ele só decide quando a primeira linha é título de estoque ou de preços e o título de compras vem embaixo, e a guarda da palavra de compra no próprio título cobre o caso comum. Depois da verificação do código final, 11 mutações desfizeram as 8 correções dela (duas de dois jeitos) e recolocaram a guarda que saiu, uma por vez: os testes perceberam 11 (seção 12). As oito correções da verificação independente final também: 8 de 8, e a passada final das 19 mutações das duas verificações sobre o código final deu 19 de 19. Na quarta revisão e na ingestão em lote, o teste de mutação passou a 103 alterações: as 32 desta rodada (20 novas e 12 com a âncora trocada pelo código novo) foram todas percebidas pelos testes; as outras 71 são as da rodada anterior, não rodadas de novo sobre o código final. Antes disso, um teste de mutação com 83 alterações de propósito no código final (as contas, a leitura, a recusa de compras, a loja em branco, o teto, a triagem por ano, o corte por plano, o expurgo e cada correção das três revisões) mostrou que os testes percebem 82. As que sobreviveram na primeira passada ganharam teste: os dias cobertos do item nunca vendido, o valor de filtro com mais de uma palavra, a rede de proteção do contrato e, na terceira revisão, a decisão por arquivo do número da venda (o relatório ordenado por produto, com a loja só na primeira linha). A única que sobrevive tira, quando o cabeçalho de vendas nem é achado, a exigência de "nada de venda nas primeiras linhas": só muda a mensagem de um relatório que já era recusado.

Testes de outros arquivos que mudaram:
- `tests/test_raio_x_regression.py` e `tests/test_cadastro_regression.py`: o nó novo é aprovado como acréscimo, conferido pelo contrato, e a versão sobe para 11.6. Nos conjuntos desses testes, nenhum valor antigo mudou. Nenhum deles é venda item a item de vários meses com a data de cada venda: esse caso muda de propósito (refatoração 4).
- `tests/test_cobertura_leiautes_erp.py`: o teto de 120 e o Raio-X do cliente por mês.
- `tests/test_avaliacao_cenario.py`: o contrato da meta com 120.
- `tests/test_inteligencia_estoque.py`: o modo com a coluna de loja.
- `tests/test_planos_raio_x.py`: a versão, e o arquivo fatiado contado como um arquivo, com a API trazendo os indicadores do plano.
- `tests/test_leads_avaliacao.py`: a revisão nova e as colunas.
- `tests/test_leads_postgresql.py`: a função de expurgo, a gravação e o downgrade no PostgreSQL real.

## 12. Revisões independentes

### Primeira revisão

Um revisor independente leu o código e comprovou cada achado com um cenário executado. Todos foram corrigidos, cada um com teste em `tests/test_indicadores.py` (`AchadosDaRevisaoTests`, `TriagemPorAnoTests` e os casos novos de `RelatorioDeComprasTests`).

| Gravidade | Achado | Correção |
|---|---|---|
| Alta | A recusa de compras pegava relatório de venda legítimo: nome da loja com "compra", filtro "Comprador", endereço com "Entrada", "Vlr. Compra" ao lado do preço de venda. Valia também para a análise mensal | Só o título de relatório (pelo começo da célula) e o total da compra dito com todas as letras recusam (seção 5, item 1) |
| Baixa | A exportação de compras sem título passava como venda | A quantidade comprada, sem coluna só de vendas, recusa |
| Média | No arquivo fatiado, a foto de estoque do GMROI era só a do último mês | A foto do arquivo junta as fatias dele |
| Média | A coluna "Data" do resumo por produto virava a data da última venda | A data só vale na venda item a item com as datas válidas |
| Média | O mesmo número de venda em duas lojas (ou dois arquivos) contava uma venda só | A venda é o número dentro da loja e do arquivo |
| Média | Na triagem, o custo implausível de um ano escondia a perda real do outro | O custo a confirmar vale por ano (seção 5, item 3) |
| Média | O scanner do cliente passou a usar o preço de referência de cada mês, sem isso estar escrito | Documentado, com teste que iguala o arquivo fatiado aos relatórios mensais |
| Baixa | A margem negativa misturava o produto no envio e o produto no mês | Os dois ficam com nomes distintos, e `perda_nos_meses` foi acrescentada |
| Baixa | A queda de exatos 5% no custo não contava (ponto flutuante) | A variação é comparada arredondada |
| Baixa | O produto que vendeu só num relatório sem período saía encalhado | A última venda dele é desconhecida, e ele fica de fora |
| Baixa | Com a coluna de custo em branco, os indicadores diziam que a coluna faltava | Sai `sem_dados` |
| Baixa | A ordem da lista de encalhados revelava o custo a quem não o vê | A lista vai do mais parado para o menos |
| Baixa | O corte de histórico deixava a ordem do histórico inteiro e linhas vazias | Reordena e tira as linhas vazias |
| Baixa | Fora da matriz padrão de planos, dois valores a custo escapavam | Ficam com o diagnóstico de CMV |
| Baixa | Tipo inesperado no nó derrubava o envio com erro 500 | É recusa |
| Baixa | A tela do cliente contava meses como "relatórios" | Mostra os arquivos enviados |

### Segunda revisão

Quatro revisores, um por frente (contas, leitura e recusa de compras, Raio-X do cliente e planos, lead), com um verificador para cada um tentando derrubar os achados: 31 confirmados e 6 derrubados. Os 31 foram corrigidos ou documentados, com teste em `tests/test_indicadores.py` (`SegundaRevisaoContasTests`, `SegundaRevisaoLeituraTests` e casos em `TriagemPorAnoTests` e `LeadTests`) e em `tests/test_planos_raio_x.py`.

| Gravidade | Achado | Correção |
|---|---|---|
| Alta | GMROI: a foto de estoque de cada arquivo só trazia quem aparecia nele | Uma foto por relatório, com o último saldo de todo produto; com foto incompleta, vale o estoque final |
| Alta | A recusa de compras ignorava o título de vendas ("Relatório de Vendas por Cliente" com "Qtde Comprada", "Total Compra") | Com título de vendas no topo, nada recusa; a quantidade comprada só recusa com coluna própria da compra |
| Alta | O valor de um filtro em célula própria ("Grupo:" \| "ENTRADAS") virava título de compras | A célula depois do rótulo com dois-pontos não é título; "Compras" sozinha só vale na primeira célula |
| Alta | O item do bloco do consultor somava a perda contra o custo a confirmar como recuperável | A regra por ano mora em duas funções, usadas também nos itens |
| Média | Os dias do período contavam os meses sem relatório | Cobertura, giro anualizado e encalhados usam os dias cobertos |
| Média | Duas lojas em arquivos do mesmo período: o estoque não somava | Os indicadores de estoque saem `sem_dados` |
| Média | Na venda item a item de várias lojas fatiada, o saldo da loja que não vendeu no último mês se perdia | O saldo de cada produto e loja é acumulado nas fatias |
| Média | O estoque de várias lojas dependia da ordem das linhas quando só uma vendia | A soma por loja olha as linhas com saldo, e não só as de venda |
| Média | "Compras por Cliente" e "Histórico de Compras do Cliente" eram recusados | A compra do cliente não é título de compras |
| Média | "CNPJ Unidade de Negócio" virava o cliente | Lista de permissão para o CPF/CNPJ do cliente |
| Média | O CPF do operador, do caixa, da matriz contava como cliente | A mesma lista de permissão; com dois, vale o do cliente |
| Média | "Nota Fiscal de Entrada" e outros títulos óbvios de compras passavam (erro na expressão) | Expressão corrigida e títulos acrescentados |
| Média | O desconto em percentual saía como índice 0% | Sai `sem_dados` |
| Média | Relatório de vendas com coluna de compra passou a ser recusado | O mesmo do título de vendas |
| Baixa | Produto que vendeu também num relatório sem período entrava nos encalhados | Fica de fora |
| Baixa | O saldo do relatório sem período passava por cima do mais recente | O relatório sem período é o mais antigo |
| Baixa | A recompra pedia venda item a item quando faltava só o CPF num arquivo | Pede o CPF/CNPJ |
| Baixa | O mapa por produto e mês pulava o mês sem venda | Todo mês coberto entra no eixo |
| Baixa | O CMV saía 0 com custo só no item a confirmar | Sai `sem_dados` |
| Baixa | A classe da curva por volume perdia a terceira casa | Somada dos valores exatos |
| Baixa | "Venda Nova" no endereço salvava o "Relatório de Compras" | A palavra de venda só conta na célula do título |
| Baixa | O CNPJ alfanumérico não identificava o cliente | Aceito, com o dígito verificador da regra nova |
| Baixa | Fora da matriz padrão, o plano com margem refazia os valores a custo escondidos | Documentado (seção 6): a auditoria já mostrava o custo a quem tem margem; é decisão do dono |
| Baixa | Acima de 120 meses, o Raio-X do cliente dependia da ordem dos arquivos | Lê de novo sem fatiar nenhum arquivo |
| Baixa | A linha do tempo ainda chamava os meses de "relatórios" | Fala em períodos |
| Baixa | A lista de arquivos da API não passava pelo corte do histórico | Perde os meses ocultos |
| Baixa | Um desconto absurdo derrubava o Raio-X inteiro | O indicador fora do contrato sai `sem_dados` |
| Baixa | O lead triado pela 11.3 voltava aos rótulos de antes da Etapa 2 | Os rótulos seguem a semântica da versão gravada |
| Baixa | Os textos não diziam que o ano de custo a confirmar fica de fora | Rodapés e definições ajustados |
| Baixa | AVALIACAO_DE_CENARIO.md com a versão e a regra dos limiares antigas | Documento atualizado; o conjunto de versões é escrito por extenso |
| Baixa | O nó ocupa até 9% do teto do resultado do lead | Documentado (seção 10) |

Os 6 derrubados, por não se reproduzirem ou não serem defeito: o número da venda com a série antes, o CPF genérico com dígito válido, a coluna "Hora" toda em 00:00, duas mudanças de número que já estavam escritas, o tempo do Raio-X item a item e a exclusão do produto de custo a confirmar da Urgência (regra que já existia).

### Terceira revisão

Uma verificação das correções da segunda revisão, frente por frente, com cenários executados em cópias do código atual, do código anterior às correções e do `e468866`. Das 31 correções, 30 se confirmaram; a do achado 23 (o teto de 120 meses no Raio-X do cliente) ficou parcial, na borda de 121. A verificação achou 17 defeitos novos, todos confirmados, nenhum derrubado. Três deles são o mesmo (a loja em branco), achado por três revisores. Todos foram corrigidos, com teste em `tests/test_indicadores.py` (`TerceiraRevisaoLojasTests`, `TerceiraRevisaoComprasTests`, `TerceiraRevisaoLeituraTests` e casos em `TetoDeFatiasTests`, `TriagemPorAnoTests` e `LeadTests`). Os testes novos falham todos no código de antes das correções.

| Gravidade | Achado | Correção |
|---|---|---|
| Média | Numa loja só, a célula da loja em branco (mesclada no XLSX ou na Planilha XML, ou preenchida só na primeira linha) virava uma segunda loja, e o estoque somava duas vezes: giro, cobertura, GMROI e o capital em estoque do lead. No arquivo fatiado, o saldo antigo ficava no acúmulo | A soma por loja só vale com duas lojas ou mais com nome nas linhas com saldo, decidido no arquivo inteiro, antes das fatias (seção 3) |
| Média | Da mesma raiz: o número da venda com a loja só na primeira linha contava a venda duas vezes (o ticket caía) | Com uma loja só, a loja não entra no número da venda; com duas, a linha de baixo do mesmo cupom herda a loja |
| Média | A quantidade comprada recusava venda com "Qtde" e "Qtde Compra" ao lado de um custo ("Faturamento por Produto", "Giro de Estoque") | Só recusa quando a coluna lida como quantidade da venda é a comprada |
| Média | O título de compras depois do rótulo do título ("Relatório:" \| "Compras por Fornecedor", "Tipo de Relatório:" \| "Entradas de Mercadorias") passava como venda | Depois do rótulo do título, a célula é o título |
| Média | A isenção do título de vendas não alcançava títulos comuns ("Produtos Mais Vendidos", "Vendas de Produtos", "Pedidos de Venda", "Faturamento por Produto") nem a compra do cliente com "Total da Compra" | Títulos de vendas acrescentados, presos ao começo da célula, sem a palavra "Venda" solta; a compra do cliente vale como título de vendas |
| Média | Títulos óbvios de compras ("Pedidos de Compra", "Mercadorias Recebidas", "Compras/Entradas", "Entrada de NF-e") e a exportação com fornecedor e data de entrada passavam como venda | Títulos acrescentados e um quarto sinal: o fornecedor com a data ou a nota de entrada |
| Baixa | O custo do envio, o do frete e a "Entrada" do crediário contavam como coluna da compra (exportação de pedidos recusada) | Saem da coluna da compra |
| Baixa | Nome de loja ("Compra Por Menos", "Compra Na Hora") e grupo de pratos ("Ranking de Entradas") recusavam a venda | "Compras por/no/na" só com o complemento de compras; "Ranking", "Curva ABC" e "Giro" só de "Compras" |
| Baixa | A lista de permissão do CPF perdeu títulos do documento do cliente ("CPF no Cupom", "CPF ou CNPJ", "Nº do CPF", "CNPJ/CPF Dest.", "CPF/CNPJ Cli.") | Conferência por palavras (seção 4) |
| Baixa | O rótulo de filtro com "vendas" no topo ("Vendas por:" \| "Todas", "Histórico de vendas considerado: 90 dias") salvava o relatório de compras | O rótulo do filtro não é título de vendas |
| Baixa | O travessão ("—") no lugar do zero derrubava o índice de descontos | O travessão vale como o meia-risca; sozinho, é a célula vazia |
| Baixa | O CNPJ alfanumérico pegava o CPF com prefixo de letras ("CPF 315.942.594-02"): com e sem o prefixo, o mesmo cliente virava dois | O CNPJ alfanumérico só com a forma dele |
| Baixa | O achado 23, na borda: 120 meses e um arquivo de um mês davam 121 ou 2 fatias conforme a ordem | O arquivo não fatiado conta como um relatório no teto; a segunda leitura sai do bloco `except`, e o quadro da primeira é liberado antes |
| Baixa | O comentário de `VERSOES_DOS_LIMIARES` invertia a regra (a versão nova só entraria sem mudança de sentido) | Comentário corrigido; teste confere a versão atual no conjunto |
| Baixa | As definições do `diagnostico.json` diziam a regra da 11.4 também para o lead triado pela 11.3 | As definições seguem a versão gravada, como a tela |
| Baixa | As seções 4.2 e 4.3 do AVALIACAO_DE_CENARIO.md descreviam as marcas da 11.3 | Atualizadas (`perda_real_do_ano`, `recuperavel_do_ano`) |

Depois das correções, uma revisão própria das regras novas achou mais duas lacunas do mesmo tipo, corrigidas com teste:

| Gravidade | Achado | Correção |
|---|---|---|
| Média | O título com qualificador ("Relatório Analítico de Vendas", "Relatório Sintético de Compras", "Resumo Diário de Vendas") não era título de vendas nem de compras: o relatório de compras assim titulado passava como venda | O qualificador entre o nome do relatório e o assunto vale nas duas listas |
| Baixa | Com a regra do rótulo, o título com dois-pontos "Vendas por Produto: Outubro" deixava de ser título de vendas | Conta também o rótulo que começa por "Vendas por" e "Faturamento por" com o que vem depois; "Vendas no Período: R$ 1.234,00" continua parâmetro |

Ficam de fora, anteriores aos indicadores e documentados na seção 10: a linha sem loja no arquivo de duas lojas ou mais, e o limite das listas de títulos da recusa de compras.

### Quarta revisão

Uma verificação das correções da terceira revisão, em três frentes (leitura e lead, loja e teto, recusa de compras), com um revisor e um verificador por frente; a verificação da recusa de compras não chegou a rodar (a sessão terminou), e as correções do revisor dessa frente foram reescritas e conferidas por mim, com os 126 envios dele: as respostas batem uma a uma com as da cópia corrigida por ele. Das 12 apontadas, 11 foram corrigidas e 1 derrubada (o CNPJ alfanumérico com espaço nos separadores não contrariava a regra escrita; a tolerância entrou como melhoria). Os testes estão em `tests/test_indicadores.py` (`QuartaRevisaoTests` e casos em `LeadTests` e em `tests/test_cobertura_leiautes_erp.py`).

| Gravidade | Achado | Correção |
|---|---|---|
| Média | A regra do fornecedor com a data de entrada recusava a venda consignada ("Data Venda", "Valor Venda") | A data da venda é coluna só de vendas; "Acerto de Consignação" e "Peças Vendidas" são títulos de vendas |
| Média | O título de compras colado ao cabeçalho de duas linhas saía do topo, e o relatório de compras passava como venda | A linha de cima do cabeçalho de duas linhas também é topo |
| Média | O nome do relatório antes do assunto ("Relatório de Pedidos de Compra") não era título de compras nem de vendas | O nome do relatório, com o qualificador, antes do assunto |
| Baixa | O título de vendas com o período ou a hora na mesma célula deixou de proteger a venda | O dois-pontos da hora não é rótulo, e conta o trecho antes do separador |
| Baixa | O parâmetro "Vendas no Período" com o valor na célula ao lado salvava o relatório de compras | O rótulo com o valor ao lado é parâmetro |
| Baixa | O total da venda fora do dicionário não desligava a regra do total da compra | A coluna só de vendas vem antes, e o total da venda pelo nome desliga a regra |
| Baixa | O CPF do fiscal de caixa ("CPF Fiscal") era lido como o do cliente e tomava o lugar da coluna "CPF" | "Fiscal" só completa a nota, o cupom e o documento |
| Baixa | Duas lojas com a loja só no primeiro item do cupom: com o primeiro item fora da venda, os de baixo herdavam a loja de outro cupom | A loja do cupom, no quadro inteiro, antes de a linha cancelada sair |
| Baixa | No lead de antes da Etapa 2, o JSON definia e rotulava as marcas pela regra da Etapa 2 | A definição e os rótulos das marcas seguem a versão gravada |
| Baixa | O travessão mudava a leitura de todos os números (antes do número, virava sinal), e o documento falava só do desconto | O travessão sozinho é a célula vazia; antes do número, fica como era em e468866 |
| Baixa | AVALIACAO_DE_CENARIO.md descrevia a soma do estoque por loja da regra antiga | Texto da seção 2 atualizado |

### Quinta revisão (a classificação prévia e os leiautes reais do lote)

Uma revisão independente do lote com os leiautes reais, em três frentes: os leitores, as regras do envio e os testes, com teste de mutação. Cada achado passou por um verificador adversarial. Nenhum agente abriu os arquivos reais: só as réplicas e CSVs inventados.

Dos 26 achados, 25 foram confirmados e corrigidos, cada um com um teste que falha sem a correção. O que foi derrubado: o aviso do papel diz "o saldo e o custo de cada produto" mesmo quando a âncora já traz os dois. A âncora fica com os dela, e nenhum número muda; os avisos de onde vieram o custo e o estoque não aparecem, mas o do papel aparece, porque descreve o tipo da tabela, e não o que ela deu. Reescrevê-lo é polimento, decisão do dono. Os testes estão em `LeiautesReaisDoLoteTests`.

| Gravidade | Achado | Correção |
|---|---|---|
| Alta | Um filtro do topo ("Tabela de Preço: 1 - VAREJO", "Considerar estoque atual: Sim") ou o título genérico "Relação de Produtos" fazia um relatório de vendas virar lista de preços ou posição de estoque. A venda sumia do faturamento, ou a quantidade e o total viravam saldo e custo. Os três revisores acharam o mesmo defeito | Só o título do relatório decide, comparado pelo começo, e o parâmetro com dois-pontos nunca. O título de preços pede "preço", e a lista de preços não tem quantidade nem total. O título de compras e a palavra de compra no título decidem contra |
| Média | O relatório de vendas sem a quantidade escapava da trava como resumo por documento ("Data;Nr. Pedido;Produto;Valor Total"), lista de preços ou resumo sem produto ("Vendas por Vendedor" com "Produto;Total") | O resumo por documento não tem a coluna do produto. A conferência do produto usa os títulos de reserva. A posição de estoque lida pelas colunas não tem coluna de venda |
| Média | "Empresa:" acima de "Filial:" juntava duas filiais da mesma empresa. Com uma terceira tabela da loja A, o saldo da loja B sumia | A filial vem antes da empresa. As tabelas se agrupam por loja, e os saldos das lojas somam |
| Média | A data rotulada ("Emitido em:", "Data:") não era lida, e um inventário antigo vencia a posição atual | A data rotulada, a sozinha na célula e a do título |
| Média | O custo do estoque misturava a tabela que ficou fora do saldo | O custo vem das tabelas que deram o saldo |
| Média | As linhas de saldo ficavam na data do fim do período, mesmo no futuro. A avaliação recusava o envio ("mês que ainda não começou"), ou a âncora deixava de ser fatiada. Já acontecia antes desta rodada | A data de hoje, quando o fim do período ainda não chegou |
| Baixa | Com duas âncoras, o mês entre elas contava como coberto: o relatório por produto desse mês era absorvido, e o ambíguo, aceito | O período coberto é a união dos períodos das âncoras |
| Baixa | A coluna opcional em branco na maior parte das linhas (o fornecedor, o NCM) desfazia o alinhamento, e a tabela entrava sem saldo, com o aviso de que tinha entrado | Vale a forma de linha que traz a coluna, mesmo rara. A tabela sem saldo não entra |
| Baixa | O desconto em percentual gravado em fração (0,2 = 20%) saía 100 vezes menor | O desconto fica de fora |
| Baixa | A tabela de estoque com título ou coluna fora do conjunto curto era lida como venda. Já acontecia antes | Os títulos "Relatório de Estoque" e "Estoque de Produtos" e os saldos "Qtde Atual" e "Disponível" |
| Baixa | 13 mutações sobreviviam aos testes: o saldo entre as tabelas, os depósitos, a data, o ambíguo fora do período, a linha de parâmetros, a continuação do cabeçalho, as três guardas do alinhamento e o desconto acima de 100% | Um teste para cada uma |

**A verificação do código final.** Depois das correções, um verificador adversarial, de novo só com réplicas e CSVs inventados, procurou o que ainda quebrava no código final e achou 9 quebras. Oito ficaram corrigidas, cada uma com teste:

| Quebra | Correção |
|---|---|
| O relatório de vendas que não foi lido escapava da trava como tabela auxiliar quando o produto vinha em "Item", "Descrição" ou "Mercadoria" | As guardas conhecem os títulos curtos do produto |
| O mesmo, com o valor em "Valor", "Vlr" ou "Valor R$" | O valor sem dizer de quê, numa coluna que a tabela auxiliar não leu, recusa a tabela |
| "Estoque" sozinho no começo do título (a razão social, o módulo "Estoque - Curva ABC") fazia a venda virar posição de estoque | O título de estoque pede a posição, o saldo, o inventário ou o balanço, ou o estoque com o que ele é |
| O nome da loja na célula ao lado do rótulo não era lido, e as tabelas de duas lojas não somavam | A loja na célula seguinte |
| A tabela só com a empresa, ou com a filial "Todas", contava como loja e somava com a da filial: o estoque dobrava | A empresa e a filial consolidada não são loja |
| Com a âncora com código, a tabela de estoque sem o código ficava de fora inteira | O código só é a chave quando toda tabela o traz |
| Dois códigos com o mesmo nome davam o saldo de um só | Os saldos somam |
| O relatório por produto com lojas da âncora e de fora era absorvido, e a venda das outras lojas sumia | É recusado, pedindo o relatório de cada loja separado ou a venda item a item de todas |

A nona era o relatório de vendas com título de estoque e o preço de venda da unidade, lido como posição. A guarda que entrou para ela (o preço da unidade decide contra o título de estoque) sobreviveu ao teste de mutação: o título de estoque completo já barrava os casos do teste. Uma sonda com CSVs inventados mostrou o pior: a posição de estoque e o inventário de verdade com o preço da unidade ("Qtde;Preço Custo;Preço Venda", "Quantidade;Preço Unitário;Valor Total", "Qtd;Preço") viravam venda, com o valor do estoque no faturamento. A guarda saiu, e ficou o teste dessas tabelas (`test_estoque_com_preco_da_unidade_continua_estoque`). A mutação do título de estoque completo também sobrevivia, porque o modelo do teste trazia o preço de venda: o teste ganhou a razão social sem ele.

O teste de mutação final desfez cada correção, uma por vez, e recolocou a guarda que saiu: 11 alterações (duas correções desfeitas de dois jeitos), 11 percebidas pelos testes.

**A verificação independente final.** Depois disso, um revisor independente conferiu os Docs contra o código, rodando CSVs inventados no código final e no do commit anterior. O revisor da classificação e dois dos três verificadores pararam no meio, com o login expirado, e os achados foram reproduzidos à parte, nas duas cópias. Ele achou três textos errados, corrigidos acima: o limite da tabela sem o título na primeira célula (a posição de estoque com "Qtde" continua lida como venda, como antes), as regras de loja (só valem com a loja na âncora) e o achado derrubado da quinta revisão (o aviso aparece). Achou também quatro defeitos do código, todos de antes desta rodada (da ingestão em lote do commit anterior), corrigidos cada um com teste:

| Defeito | Correção |
|---|---|
| A venda item a item sem a linha do período, com a data de cada venda fora do período da âncora, era absorvida, e a venda sumia do faturamento, nas duas rotas | Sem o período no cabeçalho, ela vale pelas datas das linhas: a de outro mês continua sendo venda (o lead pede a linha do período, como pede a ela sozinha) |
| O saldo dos relatórios por produto vinha do último enviado, e não do de período mais recente | O do período mais recente |
| Sem a loja na âncora, o relatório por produto com a loja de cada venda era absorvido em silêncio | Continua absorvido (a âncora pode ser a da rede toda), com aviso |
| "Geral" em qualquer lugar do nome da filial ("003 - LOJA GERAL") fazia a tabela contar como o total, e não como loja | Só o valor que começa, depois do código, por "Todas", "Geral", "Total" ou "Consolidado" é o total |

O revisor da classificação parou antes de devolver o resultado, mas deixou os roteiros. Rodados à parte nas duas cópias, eles mostraram três regressões desta rodada e um caso a mais (a última linha), corrigidos cada um com teste:

| Defeito | Correção |
|---|---|
| O relatório da venda com o estoque, com título de estoque ("Posição de Estoque e Giro" com "Estoque Atual;Qtde;Valor Total"), era lido como posição de estoque: a venda de outro mês sumia, e a do mês da âncora dava o custo errado | Com título de estoque, a quantidade que não é a do saldo decide contra a tabela auxiliar |
| "Relatório de Estoque" com "Saídas por Produto" na linha de baixo era lido como posição de estoque, com a quantidade vendida no saldo | A linha de texto do topo que fala de venda ou de saída decide contra a tabela auxiliar |
| A posição de estoque a preço de venda ("Qtde;Preço Venda;Valor Total") dava o custo pela divisão do valor pelo saldo, e a margem saía zero | O valor a preço de venda não dá custo |
| Sem o título de estoque na primeira célula, a posição "Saldo Atual;Custo Médio;Valor Total" era recusada com a trava de vendas, e com a âncora recusava o envio inteiro | Ao lado do custo unitário, o "Valor Total" é o valor do estoque |

Um teste de mutação desfez cada uma das oito correções: os testes perceberam 8 de 8. No fim, as 19 mutações das duas verificações rodaram de novo, uma por vez, sobre o código final: os testes perceberam as 19.

## 13. Ingestão em lote: a âncora temporal e as tabelas auxiliares

Pedido de 11/2026: o cliente envia, de uma vez, a venda item a item com a data de cada venda (a Opção 27 do ERP, "Vendas por Cliente Detalhada por Produto", com a "Data Ven."), o resumo por pedido ou documento, a posição de estoque, o inventário, o relatório de compras por nota e a lista de preços. A lógica fica no processo isolado (`raio_x._ler_envio_uma_vez` e, no `etl_processor`, `ler_tabela_pelo_titulo` e `ler_tabela_auxiliar`); `raio_x_servico.processar_envio` já manda todos os arquivos juntos, e não mudou. A segunda rodada de 11/2026, feita com os arquivos reais desses tipos, acrescentou a classificação prévia e a leitura dos leiautes reais (abaixo).

- **A classificação prévia.** Na leitura de cadastro (o Raio-X do cliente e o lead), o título do relatório decide antes da leitura de vendas. A posição de estoque, o inventário e a lista de preços são lidos como tabela auxiliar quando o **título do relatório** diz isso (`_papel_pelo_titulo`). O título é o primeiro texto do topo que é título de tabela auxiliar, comparado pelo começo (`_titulo_do_relatorio`); sem nenhum, a primeira célula com letras. Contam a célula sem rótulo, o valor depois de "Relatório:" (na mesma célula ou na seguinte), o trecho antes do separador ("Posição de estoque - Emitido em: ...") e o título seguido de data ("Posição de estoque em: 09/11/2026"). O cabeçalho da empresa e da emissão em cima ("Empresa:", "Filial:", "Período:", a página) é pulado, e o primeiro parâmetro do relatório encerra a busca (seção 14). Não decidem a favor da tabela auxiliar:
  - **o parâmetro com dois-pontos**, como "Tabela de Preço: 1 - VAREJO" ou "Considerar estoque atual: Sim", comum em relatório de vendas, nunca é título;
  - **o título de preços sem "preço"**: ele pede essa palavra, porque "Relação de Produtos" pode ser venda;
  - **"Estoque" sozinho no começo do título**: a razão social ("ESTOQUE DE FÁBRICA CALÇADOS LTDA") e o módulo do ERP ("Estoque - Curva ABC de Produtos") começam assim. O título de estoque pede a posição, o saldo, o inventário ou o balanço, ou o estoque com o que ele é ("Estoque atual", "Estoque de produtos", "Relatório de estoque");
  - **o título de vendas ou de compras no topo, a palavra de venda ou de compra no título e a coluna que só existe em vendas** ("Qtde Vendida", "Total Venda") decidem contra ela, e o arquivo segue para a leitura de vendas;
  - **a linha de texto do topo que fala de venda ou de saída**, fora do título e dos parâmetros ("Saídas por Produto" embaixo de "Relatório de Estoque"), também: o relatório é do movimento, e a quantidade dele é a vendida;
  - **com título de estoque, a quantidade que não é a do saldo** também ("Estoque Atual;Qtde;Valor Total"): é o relatório da venda com o estoque ("Posição de Estoque e Giro"). Fora isso, nenhuma coluna recusa a tabela com título de estoque: o preço da unidade e o total ("Qtde;Preço Venda", "Quantidade;Preço Unitário;Valor Total") são os da posição ou do inventário.

  Depois, o arquivo que não é relatório de vendas por produto é reconhecido pelo que é:
  - o resumo por pedido ou documento, pelas colunas;
  - o relatório de compras, pela recusa de sempre;
  - o resumo de vendas sem produto: o título "Giro de vendas por fornecedor", "por grupo", "por cliente" ou "por forma de pagamento", sem título de coluna de produto;
  - o relatório de vendas de leiaute ambíguo (dois títulos iguais para um valor, ou o separador de colunas duvidoso).

  **A tabela auxiliar nunca passa pela trava "não foi possível identificar produto, quantidade e valor de venda".** Ela vale só para o relatório de vendas por produto que não foi lido (sem uma das colunas) e para o arquivo que não é nada disso. Com a âncora, o arquivo que nada leu e cujo período está no dela fica fora das contas, com aviso: a venda dele já está na âncora. Sem período, de outro período ou sem a âncora, a trava continua. O arquivo com título de tabela auxiliar que não pôde ser lido como ela é recusado dizendo o que falta, e o envio com venda o deixa fora das contas (seção 14). Esse relatório também não escapa dela pelas colunas:
  - o resumo por documento não tem a coluna do produto;
  - a lista de preços não tem a quantidade nem o total da venda;
  - a posição de estoque lida pelas colunas (sem o título) também não, a não ser o total ao lado do custo unitário ("Saldo Atual;Custo Médio;Valor Total"), que é o valor do estoque;
  - o resumo sem produto não tem título de coluna de produto, contados os títulos de reserva ("Total", "Vlr. Total");
  - o título de coluna do produto inclui os curtos ("Item", "Descrição", "Mercadoria"), e não o total nem o agrupamento ("Total Produtos", "Grupo de Produtos");
  - o valor sem dizer de quê ("Valor", "Vlr", "Valor R$") numa coluna que a tabela auxiliar não leu é o total da venda: a lista de preços e a posição de estoque lida pelas colunas que o trazem são recusadas.
- **A âncora.** É a venda item a item com a data de cada venda, com a linha do período no cabeçalho e toda data dentro dele (`raio_x._datas_validas`). Com ela no envio, os outros arquivos deixam de ser vendas e a completam, e a trava dos indicadores por venda (`requer_item_a_item`) cai: o modo é o analítico. O período coberto é a união dos períodos das âncoras (`raio_x._cobertura`). Com duas âncoras, o mês entre elas não é coberto: o relatório por produto desse mês continua sendo venda, e o de leiaute ambíguo é recusado.
- **Os papéis.**
  - O relatório de vendas que não é âncora (o por produto, sem data por linha, ou a venda item a item sem a linha do período), dentro do período da âncora, dá o custo unitário do período (custo das vendas / quantidade com custo), o último saldo e o cadastro, e **não soma no faturamento**. Sem a linha do período, a venda item a item vale pelas datas das linhas (`raio_x._periodo_das_datas`); o relatório sem período e sem data por linha é tomado como do período da âncora. O resumido do ERP conta o pedido e a nota em dobro (UNIFICACAO, seção 10): o custo unitário não muda com isso. O relatório de outro período continua sendo venda.
  - As regras de loja só valem quando a âncora traz a loja de cada venda. Aí, o relatório só de outras lojas continua sendo venda, e o que traz lojas da âncora e lojas que ela não tem é recusado: somar duplicaria a venda das primeiras, e absorver perderia a das outras. Sem a loja na âncora (a Opção 27 real não traz), o relatório por produto do período é absorvido, de uma loja ou de várias: a âncora é tomada como a venda da rede toda, e o Raio-X avisa quando o relatório traz a loja de cada venda.
  - A posição de estoque e o inventário dão o saldo e o custo de cada produto; a lista de preços, o cadastro (o mix sobre os cadastrados). O preço da lista não entra nas contas (ver os limites).
  - O resumo por pedido ou documento dá o desconto (na primeira linha de venda de cada pedido), o CPF/CNPJ do cliente e a hora, quando casa com ao menos 90% das vendas da âncora. O desconto em percentual vira reais por valor × p / (100 − p), porque o valor do documento já vem líquido. O percentual de 100 ou mais fica de fora. Quando todo percentual vai até 1, ele pode estar em fração (o 20% gravado como 0,2): sem como saber, o desconto fica de fora, e o índice pede a coluna, em vez de sair 100 vezes menor.
  - O relatório de compras, o resumo de vendas sem produto e o relatório de vendas de leiaute ambíguo do período da âncora entram no envio, mas ficam fora das contas, com aviso. O de leiaute ambíguo de fora do período da âncora é recusado, porque a venda dele faltaria.
- **Sem a âncora.** Cada relatório de vendas soma, como antes.
  - Com relatório de vendas no envio, a tabela auxiliar (posição de estoque, inventário, lista de preços, resumo por documento) e o resumo de vendas sem produto ficam fora das contas, com aviso: não há o que completar.
  - O relatório de compras pelo título (o resumo por nota) também fica fora das contas, com aviso (diretriz de 11/2026, "jogue tudo e processe"). O de compras só pelas colunas e o de leiaute ambíguo continuam recusados, porque a venda deles poderia faltar.
  - Sem relatório de vendas no envio, o primeiro arquivo auxiliar recusa o envio dizendo o que é ("o arquivo é uma posição de estoque, uma tabela auxiliar que só completa a venda item a item..."). O resumo de vendas sem produto recusa com a mensagem de sempre.
- **O cruzamento.** É um left join, com a âncora como tabela primária.
  - O produto casa pelo código quando a âncora e todas as tabelas o trazem (só letras e algarismos, sem os zeros à esquerda: "0700019" e "000007000.19" são o mesmo). Basta uma tabela sem o código para a chave ser o nome: pelo código, ela ficaria de fora inteira. Sem o código, casa pelo nome normalizado, a chave de produto do motor inteiro: a Opção 27 real não tem a coluna de código. Pelo código, dois códigos com o mesmo nome viram uma linha de saldo só, com os saldos somados, porque o motor lê o saldo pelo nome.
  - A venda casa pelo número (`numero_da_venda`, sem os zeros à esquerda de cada grupo: "P9-00001" e "P9-1").
  - O custo e o estoque só completam a âncora que não os traz; o desconto, o cliente e a hora, a âncora sem a coluna.
- **As prioridades.**
  - Custo: o do relatório por produto (do período), senão o das tabelas de estoque que deram o saldo do produto (ponderado pelo saldo), senão o da lista de preços. A tabela que ficou fora do saldo (o inventário antigo) só entra para o produto sem custo nas que valeram. O custo da tabela de estoque é o da coluna do custo, ou o valor do estoque dividido pelo saldo; o valor a preço de venda (o título dele diz venda, ou a tabela traz o preço de venda e o título do valor não diz custo) não dá custo: daria o preço de venda, e a margem zero.
  - Saldo: o das tabelas de estoque (`raio_x._saldo_das_tabelas_de_estoque`), senão o último do relatório por produto de período mais recente (no empate, o último enviado; sem período, ele conta como o mais antigo). Dentro de uma tabela, as linhas do mesmo produto somam (os depósitos).
  - **As tabelas se agrupam pela loja do topo.** A loja é a filial (ou a loja, a unidade), com o depósito junto. O nome vem na célula do rótulo ("Filial: 001 - LOJA A") ou na seguinte. A empresa não identifica a loja: é a mesma nas filiais, e a tabela só com ela pode ser a de uma filial ou o total de todas. A filial que começa, depois do código, por "Todas", "Geral", "Total" ou "Consolidado" também não; "003 - LOJA GERAL" é loja.
    - Com duas lojas ou mais com nome, cada loja dá o saldo dela, e os saldos das lojas somam. A tabela sem a loja no topo só completa o produto que nenhuma loja traz.
    - Com uma loja só, ou nenhuma com nome, as tabelas são da mesma loja, e somar dobraria o estoque: a posição de estoque e o inventário trazem o saldo dos mesmos produtos.
  - **Dentro da loja vale a tabela de data mais recente.** A data é, nesta ordem: o fim do período do cabeçalho; a data rotulada ("Data:", "Emitido em:", "Posição em:"); a data sozinha na célula (a emissão); a data num texto do topo (o título, "Impresso por ... em ..."). A tabela sem data conta como a mais antiga, como o relatório sem período. No empate, a posição vem antes do inventário, e depois o primeiro arquivo. As outras só completam o produto que ela não traz, e o Raio-X avisa qual valeu.
  - O saldo entra como linhas sem venda (quantidade 0), como o relatório "vendas x estoque" lista o item parado. A data delas é a do fim da âncora, ou a de hoje quando o fim do período ainda não chegou: um mês que ainda não começou faria a avaliação recusar o envio, ou o Raio-X deixar de fatiar a âncora. O custo do produto sem venda valoriza o saldo dele nos indicadores (o estoque parado, os encalhados).
- **A trava contra a duplicação.** Depois do cruzamento, as linhas da âncora e o faturamento têm de ser os de antes, no centavo. Se `abs(faturamento depois - faturamento da âncora) > 0,01`, ou se a chave repetida do lado das tabelas multiplicaria as linhas, o envio é recusado com "Falha de Contenção de Dados: Duplicação de Faturamento Detectada".
- **Os avisos.** O Raio-X diz:
  - o papel de cada arquivo do envio que não somou: se completou a âncora ou se ficou fora das contas;
  - de onde veio o custo ("as margens são estimadas");
  - a data do estoque e a tabela de estoque que valeu;
  - o casamento do resumo por documento;
  - a loja de cada venda no relatório absorvido, quando a âncora não traz a loja;
  - a amostra parcial: a venda item a item de 90 dias ou mais que vendeu menos de 15% dos produtos com saldo no estoque enviado (de um ano ou mais, menos de 30%; seção 14).
- **Desempenho.** O custo unitário dos produtos sem venda vai numa coluna das linhas acrescentadas (`_custo_auxiliar`), e não em `df.attrs`. O pandas 2 copia os attrs a cada operação, e com os milhares de produtos de uma posição de estoque real o lote levava três vezes mais.

### Os leiautes reais (11/2026)

Os arquivos reais do ERP são `.xls` (BIFF8), relatórios paginados exportados para planilha. Foram inspecionados pelo leitor do próprio motor, com as células de dado pessoal mascaradas.

| Relatório (título no arquivo) | Estrutura | Colunas lidas | Papel |
|---|---|---|---|
| "Giro de vendas por cliente detalhada por produto" (a Opção 27) | Blocos por cliente; cabeçalho em duas linhas, repetido | Data, Nr. Doc., Produto, Qtde e Valor Total. "Ven." e "Valor Unit." ficam de fora | Âncora |
| "Posição de estoque atual de produtos" | Filial no topo; blocos por grupo ("Grupo :"); cabeçalho em duas linhas, repetido; total de cada grupo | Código, Nome produto (título na coluna F, dado na D), Est. Atual (o saldo), Preço Custo (título na L, dado na M) e Valor Estoque (título na M, dado na N). Forn., Est. Mín. e Est. Máx. ficam de fora | Estoque |
| "Inventário de Estoque" | "Período : dd/mm/aaaa"; cabeçalho de página repetido; o título da descrição com as letras separadas por espaço | Cód. (título na A, dado na B), Descrição do produto (título na E, dado na D), Custo Unitário (título na H, dado na J), Saldo de Estoque e Valor do Estoque. O NCM fica de fora | Estoque |
| "Lista de preços" | Parâmetros no topo; blocos por grupo; o produto uma vez por grupo de preço | Cód. Prod. (título na A, dado na B), Nome e Preço (título na F, dado na G) | Preços (o cadastro) |
| "Giro de vendas por Cliente / documento" | Blocos por vendedor; cabeçalho em duas linhas; subtotais e total | Nr. Doc. (título na C, dado na A), Valor (líquido) e Total Desc. % (o desconto em percentual). Data, Cliente, Acres., Frete e % Méd Comi. ficam de fora | Documentos |
| "Posição das compras de produtos" | Resumo por nota; cabeçalho de página repetido | Data da Compra, Número NF, Série, Fornecedor, Vr Total Produtos e Valor Nf | Compras (fora das contas) |
| "Giro de vendas por fornecedor", "por grupo de produtos", "por grupo de clientes", "por forma de pagamento" e "Por cliente - Ranking" | Resumos sem produto | — | Resumo de vendas sem produto (fora das contas) |
| "Giro de vendas por Produto(s) Vendido(s) Abaixo do Pr. Custo" | Dois "Valor Unitário" (o de compra e o vendido) | — | Ambíguo (fora das contas no período da âncora) |
| "Giro de Vendas por Produto" (a Resumida) | Cabeçalho em duas linhas | Codigo, Nome do produto, Qtde Vend, Total Venda, Total Custo e Estoque Atual | Relatório por produto (absorvido pela âncora) |

Os leitores tratam o que esses arquivos têm:
- O título em duas linhas ("Est." sobre "Atual", "Total" sobre "Desc. %"). Sem isso, o "Total" de "Total Desc. %" ficava com o valor do documento.
- O título numa coluna e o dado em outra, alinhados pela ordem (`_alinhar_tabela_auxiliar`).
- As linhas de parâmetro do topo, que nunca são título de coluna. O "Grupo Preço inicial: 1" sobre o "Produto inicial: ..." parecia um cabeçalho com o produto e o preço.
- O título com as letras separadas (`_desespacar`).
- Os títulos curtos ("Cód.", "Nome", "Valor"), pelos títulos de reserva.
- "Posição das compras" como título de relatório de compras.
- A coluna opcional em branco na maior parte das linhas (o fornecedor, o NCM): a forma de linha que a traz, mesmo rara, diz onde ela fica. A geometria e a prova de leitura continuam valendo. A tabela de estoque sem saldo nenhum lido não entra.

**Ensaio com o lote real (11/2026, só agregados).** A Opção 27 que está agora na pasta é outra exportação: 20 clientes, 220 vendas, 418 linhas e R$ 15.872,67, de 01/01/2023 a 11/11/2026. A exportação anterior, da loja toda, dava R$ 710.718,23. O ensaio rodou com essa Opção 27 mais os cinco auxiliares (posição de estoque, inventário, lista de preços, resumo por documento e posição das compras), e também com os 13 arquivos da pasta juntos. Nos dois casos, as duas rotas (o Raio-X do cliente e o lead) aceitam o envio, e o faturamento fica R$ 15.872,67, o da Opção 27 sozinha, no centavo.
- **Resumo por documento:** casa com 220 das 220 vendas e dá R$ 453,74 de desconto, o mesmo "Vr. desctos" do rodapé do relatório e a soma dos "Descontos" da Opção 27. O índice de descontos fica em 2,78%.
- **Estoque:** a posição dá o saldo de 2.105 produtos, com 20.340,33 unidades, o "Total geral" do relatório. O inventário, da mesma data, traz os mesmos produtos e não soma.
- **Lista de preços:** 3.995 linhas de 2.105 produtos, dois preços por produto.
- **Custo:** com os cinco auxiliares, veio para 148 dos 189 produtos vendidos (o nome de 37 deles não está no cadastro atual). Com a Resumida por Produto junto, veio para 185.
- **Representatividade:** a venda é de 20 clientes e o estoque é o da loja toda. Por isso o giro, a cobertura, o GMROI e os encalhados deste ensaio não dizem nada da loja: ele confere a leitura, não o diagnóstico.
- **Tempo:** de 6 a 11 s por rota. Eram 33 s antes de o custo dos parados sair de `df.attrs`.
- **Pela rota HTTP de verdade:** `POST /admin/leads` com os 13 arquivos, a Baia de Segurança e o processo isolado do ETL, no app de teste com SQLite descartável. A resposta é HTTP 303, o lead é gravado e a tela e o `diagnostico.json` abrem. O Raio-X do cliente, pela função de serviço da rota (`raio_x_servico.processar_envio`), dá o mesmo faturamento.
- **Regressão:** com a Opção 27 sozinha, e com o par Opção 27 + Resumida por Produto, o resultado é idêntico ao do código anterior.

**Limites.**
- O preço da lista de preços não entra nas contas. O ERP lista o produto uma vez por grupo de preço, sem dizer de que grupo é cada linha, e usar esse preço como referência mudaria o vazamento do Raio-X (a venda abaixo do preço de referência). Fica para decisão do dono.
- O NCM do inventário não entra no cadastro do lead. Os produtos vendidos que não estão no cadastro atual (37 no ensaio) virariam "sem NCM" (omissão no ERP) sem ser.
- O grupo de produto dos blocos ("Grupo :") não vira categoria, pelo mesmo motivo e pela precedência das categorias, que o dono ainda decide.
- O relatório de compras por nota fica fora das contas: não traz o produto, e o saving pede o preço de compra por produto.
- O custo do relatório por produto é o médio do período inteiro: a margem de cada mês é estimada com ele. Sem esse relatório, o custo é o da posição de estoque, o de hoje.
- A inteligência de estoque do lead (o capital em estoque por produto) só valoriza o produto que vendeu no ano; o parado sem venda fica sem o valor a custo lá.
- No arquivo com duas lojas, o resumo por documento casa pelo número sem a loja.
- A empresa no topo não identifica a loja. Duas tabelas de estoque só com a empresa, sem a filial, contam como da mesma loja e não somam: vale a mais recente, com aviso.
- O título é o primeiro texto do topo que é título de tabela auxiliar: o cabeçalho da empresa e da emissão em cima (o nome da empresa, "Empresa:", "Filial:", "Período:", a emissão, a página, ou "Relatório:" com o título na célula seguinte) não o esconde (seção 14). O primeiro parâmetro do relatório ("Tabela de Preço: ...") encerra a busca. Sem título de tabela auxiliar no topo, a planilha é classificada pelas colunas: a tabela com título de saldo próprio ("Saldo", "Estoque Atual") é lida como estoque, e a que traz o saldo e o total ("Estoque;Valor Total") é recusada pela trava. A planilha com "Qtde" e "Valor Total" (ou "Preço Venda") sem título de estoque nenhum continua lida como relatório de vendas, como antes: sem título, ela é uma venda sem a coluna da data.
- O relatório que cobre só parte do período da âncora (pelo período do cabeçalho ou, sem ele, pelas datas das linhas) não é absorvido: no Raio-X do cliente ele soma, e a parte comum conta duas vezes; o lead recusa (a sobreposição, ou a falta do período). É a regra de sempre dos períodos que se sobrepõem.
- Sem a loja na âncora, a venda das lojas que ela não tem, num relatório por produto do período, não soma: o Raio-X avisa, e o cliente envia a venda item a item de todas as lojas.
- O desconto em percentual gravado em fração (0,2 = 20%) fica de fora, e a lista de preços com "Valor Total" é recusada como relatório de vendas.
- A posição de estoque valorizada com "Total Venda" ou "Valor Total Venda" ao lado do saldo ("Qtde", "Est. Atual", "Saldo") é lida como estoque, e o total a preço de venda não vira custo nem faturamento (seção 14). Só com os valores, sem o saldo, ela não é lida: com venda no envio, fica fora das contas com o aviso do que falta; sozinha, é recusada dizendo isso.
- A coluna opcional em branco em todas as linhas (nenhuma linha a traz) ainda desfaz o alinhamento. A tabela é então lida pelas colunas ou recusada, nunca com o dado na coluna errada: a prova de leitura impede.
- Os leiautes foram conferidos com uma exportação de cada tipo, de um ERP só. Outro ERP, ou outra versão do relatório, pode pedir títulos novos nos dicionários.

## 14. Auditoria matemática e pontas soltas (11/2026)

Diretriz de 11/2026 ("fechamento de pontas soltas e rigor matemático administrativo"): o cliente envia de uma vez todos os relatórios do ERP (até 36 meses), sem escolher arquivo, e os 21 indicadores e os textos da devolutiva conferem com as fórmulas clássicas de Administração Financeira e de varejo.

### As regras de ingestão fechadas

- **Posição de estoque valorizada com "Total Venda".** Com o saldo ("Qtde", "Est. Atual", "Saldo"), o total a preço de venda ao lado não decide contra o estoque (`etl_processor._coluna_so_da_venda`): a tabela é lida como estoque, e esse total não vira custo (`_valor_a_preco_de_venda`) nem faturamento. Só com os valores, sem o saldo, ela não é lida: com venda no envio, fica fora das contas com o aviso "tem o título de posição de estoque, mas não foi possível ler o saldo de cada produto"; sozinha, é recusada dizendo isso (`papel_anunciado_pelo_titulo`).
- **Posição sob o nome da empresa.** O título é o primeiro texto do topo que é título de tabela auxiliar (`_candidatos_a_titulo`). O nome da empresa, os rótulos "Empresa:", "Filial:", "Período:", a emissão e a página em cima não o escondem, e "Relatório:" com o título na célula seguinte também vale. A empresa nunca vira a filial (a loja vem só do rótulo da filial, seção 13), e o saldo em "Qtde" é lido pelo título de estoque.
- **Jogue tudo e processe.** Nenhum arquivo auxiliar derruba o lote. Com venda no envio, ficam fora das contas, cada um com o aviso do que é: a tabela auxiliar, o resumo de vendas sem produto, o relatório de compras pelo título e o arquivo com título de tabela auxiliar que não foi lido. Com a âncora, o arquivo que nada leu e cujo período está no dela também fica fora: a venda dele já está na âncora. Continuam recusados, porque a venda deles poderia faltar: o relatório de compras só pelas colunas, o de leiaute ambíguo fora do período da âncora e o arquivo que nada leu sem a âncora, sem período ou de outro período. A trava contra a duplicação do faturamento (diferença ≤ R$ 0,01) não mudou.
- **Amostra parcial diante do estoque inteiro.** Com o estoque das tabelas auxiliares (o da loja inteira), a venda item a item de 90 dias ou mais que vendeu menos de 15% dos produtos com saldo positivo (de um ano ou mais, menos de 30%; com 20 produtos ou mais) ganha o aviso "Amostra parcial: ...". O giro, a cobertura, o GMROI, a ruptura e os encalhados ganham `amostra_parcial: true` (`raio_x._aviso_de_amostra_parcial`). No lote real (a Opção 27 de 20 clientes contra o estoque da loja toda), a venda tem 113 dos 636 produtos com saldo (17,8%) em 1.411 dias, e o aviso aparece: em um ano ou mais, a loja inteira vende bem mais de 30% do que tem em estoque. Os dois limites foram aprovados pelo dono em 13/11/2026 (seção 15). A tela do lead ainda não mostra os indicadores: lá, a marca fica no JSON gravado.

### As fórmulas auditadas

Três auditores independentes conferiram cada indicador no código, com cenários calculados à mão rodados no próprio motor (só CSVs inventados); cada discrepância foi reproduzida à parte antes da correção.

| Fórmula pedida | No motor | Resultado |
|---|---|---|
| Margem bruta = (receita líquida − CMV) / receita líquida, nunca sobre o CMV | Sobre a venda com custo; o markup (preço / custo − 1) só existe na auditoria de preço do lead, separado | Confirmada a divisão pela receita. Ajuste: a receita e o CMV são líquidos de devolução (antes, brutos) |
| Cobertura (dias) = estoque a custo / CMV médio diário | Por produto, em unidades (o custo unitário se cancela: é a mesma conta) | Confirmada por produto. Ajustes: a agregada da loja (`dias_agregado`); a contagem do item com estoque e sem venda; a venda média diária sem o relatório sem período |
| Giro = CMV anualizado / estoque a custo, com os dias exatos | Giro × 365 / dias cobertos | Confirmada a fórmula. Ajuste: o CMV anualizado é o dos relatórios com custo e com período, sobre os dias deles. Antes, o CMV de meio ano era anualizado pelo ano inteiro, e o do relatório sem período, sem os dias dele |
| GMROI = lucro bruto anualizado / estoque a custo | Margem do período / estoque médio | Ajuste: `anualizado` (a margem × 365 / dias, na base do giro); `valor` continua o do período. O `percentual` em pontos saiu: era o único campo de razão fora da fração |
| Desconto sem dupla subtração | Índice = desconto / (faturamento líquido + desconto); o desconto nunca sai do valor da linha | Confirmado com a Opção 27 (o "Valor Total" já líquido) e com o resumo por documento (o percentual vira reais por valor × p / (100 − p)). Ajustes: com o total bruto e o "Valor Líquido" no relatório, a venda é o líquido (o bruto dobrava o desconto no índice); sobre o valor derivado do preço unitário, o índice sai `sem_dados`; o casamento do resumo por documento é medido pelo valor das vendas, e não pelas linhas |
| Encalhados = Σ saldo × custo unitário, com saldo > 0 e sem venda há mais de 60 dias, sem saldo negativo nem item sem custo | O mesmo | Confirmada: o item sem custo conta à parte (`itens_sem_custo`), sem valor |
| Curva ABC tripla, com A até 80%, B até 95% e C até 100%, sem a devolução distorcer a base | A regra da análise mensal | Confirmados a ordem e os cortes. Ajustes: as três curvas pelo valor líquido de devolução (a base, 100%, é a soma dos positivos); a participação de cada item sai do valor (os empatados ficam com a mesma) |
| Ticket médio = faturamento / vendas únicas; PPA = unidades / vendas únicas | O mesmo | Confirmadas. Ajustes: com a data de cada venda, a venda é o número naquela data (a numeração que recomeça não junta vendas); o PPA avisa a quantidade fracionada (o granel em kg não é peça) |
| Recompra = clientes com mais de uma compra / clientes identificados | O mesmo, pelo CPF ou CNPJ válido | Confirmada |
| Devolução | Só com a coluna de situação | Ajuste: sem a coluna de situação, a linha com quantidade e valor negativos é a devolução (antes, sumia do faturamento líquido de devolução e do CMV líquido) |
| O texto e o JSON batem com o cálculo | Os percentuais em fração, formatados nas telas pelo `pct` | Ajustes: o percentual do líder da curva ABC no texto de reserva da devolutiva sai com vírgula; o GMROI sem o campo em pontos |

### Os testes do gabarito

`GabaritoAdministrativoTests` (em `tests/test_indicadores.py`) usa um envio de três meses (01/08 a 31/10/2025) com número da venda, CPF, desconto, custo, estoque, uma devolução e um produto listado sem venda. Os 21 indicadores são conferidos contra as contas feitas à mão, escritas nos comentários. Por exemplo: margem (140 − 92) / 140 = 34,29%; giro anualizado 92 × 365 / 92 / 700,67 = 0,5209; GMROI anualizado 48 × 365 / 92 / 700,67 = 0,2718; cobertura agregada 700,7 dias = 365 / giro anualizado.

Há também uma prova para cada ajuste:
- a mesma loja em 3 e em 12 meses dá o mesmo GMROI anualizado;
- o semestre sem custo não entra na anualização;
- o relatório sem período fica fora dela;
- o "Valor Líquido" vale sobre o total bruto;
- o desconto sobre o valor derivado sai `sem_dados`;
- a venda é o número naquela data;
- o granel aparece no PPA;
- o casamento do resumo por documento é medido pelo valor;
- o percentual do texto da devolutiva sai com vírgula.

O teste de mutação desfez cada regra e cada ajuste desta diretiva, um por vez: os testes perceberam 25 de 25.

### Decisões e limites

- **Empate na curva ABC.** O empate continua desempatado pela ordem do nome, como na análise mensal. Com 20 mil itens iguais, agrupar os empatados poria todos na classe A. O dono aprovou o desempate pelo nome em 13/11/2026 (seção 15).
- **Duas margens com o mesmo nome.** A margem bruta do indicador 3 tira o custo a confirmar (erro de cadastro). A da auditoria do Raio-X e a "margem bruta estimada" do lead somam todo produto. No envio com custo a confirmar, os números diferem. O dono aprovou manter a separação em 13/11/2026 (seção 15): o indicador é a margem limpa, e a auditoria do Raio-X mostra a que inclui o custo a confirmar (as telas mostram a da auditoria).
- **Bonificação.** Fica fora do CMV do indicador. É o CMV líquido da auditoria que a soma.
- **Indicador protegido sem número.** O plano que não libera a funcionalidade vê "protegido" também no indicador que não saiu (o saving, a recompra sem CPF). Mostrar só o status de falta pede decisão do dono (seção 6).
- **Estoque parado do lead.** O lead guarda o saldo do último ano em que o produto vendeu, e não o último saldo do envio: o produto que parou de vender e foi zerado depois ainda aparece com o saldo antigo. Corrigir pede uma coluna nova no contrato das linhas do lead.
- **Desconto em percentual todo até 1%.** Pode estar em fração: fica de fora, e o índice pede a coluna.
- **Sobreposição parcial.** O relatório que cobre só parte do período da âncora soma no Raio-X do cliente (seção 13).

## 15. Motor financeiro consolidado (13/11/2026)

O dono aprovou o Relatório de Auditoria Matemática da seção 14 e as calibragens de negócio:

1. **Amostra parcial.** Ficam os limites de 15% (venda item a item de 90 dias a menos de um ano) e de 30% (um ano ou mais), com 20 produtos com saldo ou mais.
2. **Duas margens.** Fica a separação: a margem bruta do indicador 3 sem o custo a confirmar (a margem limpa), e a da auditoria do Raio-X e do lead com ele.
3. **Curva ABC.** Fica o desempate pelo nome, como na análise mensal.

Com isso, o motor principal (`etl_processor.py`, `raio_x.py`, `indicadores_executivos.py` e `diagnostico_cenario.py`) está finalizado no ramo `motor-unificado-xls`, na versão `indicadores-2026-11.2`, conferido pelo gabarito, pela mutação (25 de 25) e pela suíte completa (2.540 testes, sem falha nova). Seguem em aberto, para decisão do dono, os limites menores da seção 14 (a bonificação fora do CMV do indicador, o "protegido" no indicador que não saiu, o estoque parado do lead e o desconto em percentual todo até 1%), além do merge no `main` e da publicação.
