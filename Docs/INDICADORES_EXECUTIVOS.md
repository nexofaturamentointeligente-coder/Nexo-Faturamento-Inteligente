# Indicadores executivos do NEXO (21) e as cinco refatorações do motor

Este documento descreve a expansão do motor ETL pedida em 11/2026: 21 indicadores executivos, dois modos de leitura (analítico e acumulado) e cinco refatorações. Versões das regras: Raio-X `raio-x-2026-11.6`, triagem do lead `triagem-2026-11.4` e indicadores `indicadores-2026-11.1`. A migração nova é a `c5f8a1d3e7b9`, sobre a `9b3e5d7f1a28`.

## 1. Onde os indicadores ficam

O resultado do Raio-X ganhou o nó `indicadores_executivos`. O Raio-X do cliente e a Avaliação de Cenário do lead usam a mesma função, `raio_x._combinar`, e por isso recebem o mesmo nó.

```
"indicadores_executivos": {
  "versao": "indicadores-2026-11.1",
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
| 2 | `ticket_medio` | Faturamento / vendas distintas (o número da venda dentro do arquivo e, com duas lojas ou mais no arquivo, dentro da loja: cada loja e cada arquivo numeram as suas; a linha com a loja em branco logo abaixo do mesmo número é da loja do cupom) | modo analítico e número da venda | todos |
| 3 | `margem` | Bruta: venda com custo − custo, em R$ e em % da venda com custo, sem o custo a confirmar. Líquida: `requer_parametro` | custo | `margem_bruta` |
| 4 | `giro_estoque` | CMV / estoque atual a custo médio; anualizado = giro × 365 / dias cobertos (`dias_do_periodo`: os dias que algum relatório cobre, sem o mês que nenhum cobre) | custo e estoque | `diagnostico_cmv` |
| 5 | `mix` | Produtos vendidos; % sobre os cadastrados quando o relatório lista também o item sem venda (o "vendas x estoque") | — | todos |
| 6 | `itens_por_venda` | Unidades / vendas distintas (e linhas / vendas) | modo analítico e número da venda | todos |
| 7 | `recompra` | Clientes com mais de uma venda / clientes identificados por CPF ou CNPJ válido (também o CNPJ alfanumérico de 2026) | modo analítico e CPF/CNPJ em todo relatório | `curva_abc_clientes` |
| 8 | `saving` | Sempre `requer_relatorio_de_compras` | relatório de compras | `diagnostico_cmv` |
| 9 | `evolucao_custo` | Custo unitário de cada produto no primeiro e no último mês com custo; altas e quedas de 5% ou mais e a variação ponderada pela quantidade do último mês | custo e dois meses | `alerta_precificacao` |
| 10 | `cmv` | Custo das vendas sem o custo a confirmar; o custo dele sai à parte (`custo_a_confirmar`). Sem custo plausível nenhum, `sem_dados` | custo | `diagnostico_cmv` |
| 11 | `curva_abc_faturamento` | 80/15/5 pelo faturamento, a regra da análise mensal (`_classificar_curva_abc`: A até 80% do acumulado, B até 95%, líder sempre A) | — | `curva_abc_vendas` |
| 12 | `curva_abc_volume` | A mesma curva pela quantidade | — | `curva_abc_vendas` |
| 13 | `curva_abc_lucro` | A mesma curva pela margem bruta positiva; a margem negativa e o custo a confirmar ficam fora da curva | custo | `margem_bruta` |
| 14 | `margem_negativa` | Sem o custo a confirmar, duas leituras: `itens`, `perda` e `lista` pelo produto no envio inteiro (custo acima da venda somando os meses, em centavos); `por_mes`, `matriz` (produto x mês com `impacto_q4`, refatoração 5), `perda_q4` e `perda_nos_meses` mês a mês. O produto negativo num mês e positivo no envio entra só na segunda | custo | `auditoria_abaixo_custo` (as contagens, todos) |
| 15 | `margem_abaixo_minimo` | Produtos com margem de 0% até abaixo de 15% (a negativa é o 14) | custo | `margem_bruta` |
| 16 | `indice_descontos` | Desconto / (faturamento + desconto), supondo o valor da linha já líquido do desconto; o desconto em percentual ou absurdo dá `sem_dados` | coluna de desconto em reais | todos |
| 17 | `ruptura` | Produtos com venda no período e último estoque informado ≤ 0 | estoque | `produtos_parados_ruptura` |
| 18 | `encalhados` | Estoque > 0 e mais de 60 dias cobertos por relatório sem venda até o fim do envio; valor a custo médio. A lista vai do mais parado para o menos (e não pelo valor, que o Bronze não vê) | estoque e período | `produtos_parados_ruptura` (o valor a custo, `diagnostico_cmv`) |
| 19 | `cobertura_estoque` | Estoque / venda média diária (quantidade / dias cobertos): mediana, faixas (até 15, 16 a 60, 61 a 180, acima de 180 dias) e lista | estoque e período | `produtos_parados_ruptura` |
| 20 | `gmroi` | Margem bruta / estoque médio a custo: a média das fotos do estoque (uma por relatório, cada uma com o último saldo de todo produto até ali), quando toda foto traz todo produto com estoque e custo do estado final; senão (o relatório que só lista quem vendeu, o arquivo fatiado), o estoque final. `base` diz qual valeu | custo e estoque | `margem_bruta` (o estoque médio a custo, `diagnostico_cmv`) |
| 21 | `mapa_calor` | Analítico com data: faturamento e vendas por dia da semana (0 = segunda) e hora. Senão, faturamento por produto e mês (os 20 maiores), com todo mês coberto, também o sem venda, e `impacto_q4` nos meses 10, 11 e 12 | data por linha ou relatórios mensais | todos |

Mais regras:
- **Encalhados.** Com a data de cada venda, os dias sem venda contam da data exata (`base_dos_dias = data_da_venda`). Sem ela, contam do fim do relatório em que o produto vendeu (`fim_do_relatorio`). Só contam os dias que algum relatório cobre: no mês sem relatório o produto pode ter vendido, e o encalhe tem de ser certo. O item listado só com estoque, sem venda no envio, conta todos os dias cobertos. O que vendeu num relatório sem período tem a última venda desconhecida e fica de fora, mesmo com venda datada em outro relatório.
- **Estoque.** O estoque de cada relatório é o último saldo de cada produto e loja, somado por produto: o depósito que só traz o saldo, com quantidade 0, também soma. A soma por loja só vale quando as linhas com saldo do arquivo trazem duas lojas ou mais com nome, decidido no arquivo inteiro, antes das fatias (`raio_x._marcar_as_lojas`). Com uma loja só, a linha com a célula da loja em branco (mesclada, ou preenchida só na primeira linha do grupo ou do cupom) é dessa loja, e vale o último saldo do produto. No arquivo fatiado por mês, o saldo de cada loja vem acumulado das fatias anteriores. Entre relatórios, vale o mais recente; o sem período conta como o mais antigo. Com dois arquivos de estoque no mesmo período (duas lojas enviadas separadas), o estoque da rede não é conhecido, e giro, ruptura, encalhados, cobertura e GMROI saem `sem_dados`.
- **Data da venda.** A coluna de data só vale para os indicadores (última venda, mapa por dia da semana, recompra por dia) na venda item a item com toda data legível, dentro do período e em mês que já começou (`raio_x._datas_validas`). Num resumo por produto, "Data" é o cadastro ou a última compra, e não entra.
- **Listas.** Têm no máximo 50 itens, com nome e código do produto.
- **Meses.** Só contam os relatórios mensais (início e fim no mesmo mês). O arquivo de venda item a item com a data de cada venda vira relatórios mensais (refatoração 4).

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

`tests/test_indicadores.py` (125 testes) cobre:
- a leitura nova;
- as cinco refatorações;
- os dois modos, com os valores conferidos à mão em envios fictícios;
- o contrato;
- os planos e o corte de histórico;
- cada achado das quatro revisões independentes (seção 12), nas três rotas quando o achado é da leitura (análise mensal, Raio-X do cliente e lead);
- a ingestão em lote (seção 13): a âncora com o relatório por produto, a posição de estoque, a lista de preços, o resumo por pedido e o relatório de compras, a outra loja e o outro período, a trava contra a duplicação e as réplicas dos leiautes reais do ERP;
- a gravação no lead, o expurgo e o downgrade.

Na quarta revisão e na ingestão em lote, o teste de mutação passou a 103 alterações: as 32 desta rodada (20 novas e 12 com a âncora trocada pelo código novo) foram todas percebidas pelos testes; as outras 71 são as da rodada anterior, não rodadas de novo sobre o código final. Antes disso, um teste de mutação com 83 alterações de propósito no código final (as contas, a leitura, a recusa de compras, a loja em branco, o teto, a triagem por ano, o corte por plano, o expurgo e cada correção das três revisões) mostrou que os testes percebem 82. As que sobreviveram na primeira passada ganharam teste: os dias cobertos do item nunca vendido, o valor de filtro com mais de uma palavra, a rede de proteção do contrato e, na terceira revisão, a decisão por arquivo do número da venda (o relatório ordenado por produto, com a loja só na primeira linha). A única que sobrevive tira, quando o cabeçalho de vendas nem é achado, a exigência de "nada de venda nas primeiras linhas": só muda a mensagem de um relatório que já era recusado.

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

## 13. Ingestão em lote: a âncora temporal e as tabelas auxiliares

Pedido de 11/2026: o cliente envia, de uma vez, a venda item a item com a data de cada venda (a Opção 27 do ERP, "Vendas por Cliente Detalhada por Produto", com a "Data Ven."), o resumo por pedido ou documento, a posição de estoque a custo, o relatório de compras por nota e a lista de preços. A lógica fica no processo isolado (`raio_x._ler_envio_uma_vez` e `etl_processor.ler_tabela_auxiliar`); `raio_x_servico.processar_envio` já manda todos os arquivos juntos, e não mudou.

- **A âncora.** É a venda item a item com a data de cada venda (`raio_x._datas_validas`). Com ela no envio, os outros arquivos deixam de ser vendas e a completam, e a trava dos indicadores por venda (`requer_item_a_item`) cai: o modo é o analítico. Sem a âncora, nada muda: cada arquivo é um relatório de vendas, e a tabela auxiliar é recusada como antes.
- **Os papéis.**
  - O relatório de vendas por produto sem data por linha, dentro do período da âncora (ou sem período), dá o custo unitário do período (custo das vendas / quantidade com custo), o último saldo e o cadastro, e **não soma no faturamento**. O resumido do ERP conta o pedido e a nota em dobro (UNIFICACAO, seção 10): o custo unitário não muda com isso. O relatório de outra loja (com loja nomeada que a âncora não tem) ou de outro período continua sendo venda.
  - A posição de estoque dá o saldo e o custo de cada produto; a lista de preços, o cadastro (o mix sobre os cadastrados).
  - O resumo por pedido ou documento dá o desconto (na primeira linha de venda de cada pedido), o CPF/CNPJ do cliente e a hora, quando casa com ao menos 90% das vendas da âncora.
  - O relatório de compras entra no envio, mas fica fora das contas: o resumo por nota não traz o produto (o saving continua pedindo o preço de compra por produto).
- **O cruzamento.** É um left join, com a âncora como tabela primária. O produto casa pelo código quando a âncora e as tabelas o trazem (só letras e algarismos, sem os zeros à esquerda: "0700019" e "000007000.19" são o mesmo) e, sem ele, pelo nome normalizado, a chave de produto do motor inteiro: a Opção 27 real não tem a coluna de código. A venda casa pelo número (`numero_da_venda`, sem os zeros à esquerda de cada grupo: "P9-00001" e "P9-1"). O custo e o estoque só completam a âncora que não os traz; o desconto, o cliente e a hora, a âncora sem a coluna.
- **As prioridades.** Custo: o do relatório por produto (do período), senão o da posição de estoque (ponderado pelo saldo), senão o da lista de preços. Saldo: o da posição de estoque (somado entre arquivos e linhas), senão o último do relatório por produto mais recente. O saldo entra como linhas sem venda (quantidade 0) na data do fim da âncora, como o relatório "vendas x estoque" lista o item parado; o custo do produto sem venda valoriza o saldo dele nos indicadores (o estoque parado, os encalhados).
- **A trava contra a duplicação.** Depois do cruzamento, as linhas da âncora e o faturamento são os de antes, no centavo: se `abs(faturamento depois - faturamento da âncora) > 0,01`, ou a chave repetida do lado das tabelas multiplicaria as linhas, o envio é recusado com "Falha de Contenção de Dados: Duplicação de Faturamento Detectada".
- **Os avisos.** O Raio-X diz o papel de cada arquivo auxiliar, de onde veio o custo ("as margens são estimadas": o custo médio do período aplicado às vendas do período inteiro), a data do estoque e o casamento do resumo por documento.

**Ensaio com os relatórios reais** (a Opção 27 e a Resumida por Produto, só agregados): o faturamento fica R$ 710.718,23, o da Opção 27 sozinha, no centavo; o ticket, as 10.808 vendas e os 2,816 itens por venda não mudam. Passam a sair a margem bruta (45,48%, contra 45,62% da Resumida sozinha), o CMV (R$ 349.614,39), o giro (anualizado 0,66, estoque a custo de R$ 176.226,80), o GMROI (1,65), os encalhados (R$ 104.203,53 a custo), a cobertura e o mix (904 vendidos de 912 cadastrados). O custo veio para 893 dos 904 produtos vendidos. As duas ordens dão o mesmo resultado, e o lead, que recusava a Resumida (o período passa de um ano para o outro), aceita o par. Cerca de 16 s, contra 10 s da Opção 27 sozinha.

**Limites.**
- Os leiautes da posição de estoque, da lista de preços, do resumo por pedido e do relatório de compras por nota são os títulos comuns de ERP: nenhum arquivo real desses tipos foi recebido até 11/2026. O primeiro de cada um deve ser conferido.
- A posição de estoque com "Qtde" e "Valor Total" (sem "Saldo" nem "Estoque" no título da coluna) é lida como relatório de vendas, e entra como relatório por produto: o saldo dela não é lido.
- O custo do relatório por produto é o médio do período inteiro: a margem de cada mês é estimada com ele.
- A inteligência de estoque do lead (o capital em estoque por produto) só valoriza o produto que vendeu no ano; o parado sem venda fica sem o valor a custo lá.
- No arquivo com duas lojas, o resumo por documento casa pelo número sem a loja.
