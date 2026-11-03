# Trava de validação do total do PDV

Impede publicar uma devolutiva cujo faturamento não bate com o total que o PDV mostra.

## Como funciona

1. No envio do relatório de **Vendas**, pelo cliente ou pelo consultor, a pessoa informa o total de vendas do período. Sem um valor válido, o envio é recusado no servidor. O valor fica em `validacao_pdv`, com status `PENDENTE`.
2. Ao processar, depois do cálculo e na mesma transação dos KPIs, o sistema compara o **faturamento bruto calculado** com o valor informado, em centavos:
   - diferença de até R$ 1,00: status `CONCLUIDO`;
   - diferença acima de R$ 1,00: status `DIVERGENTE_PDV`.
3. Ao publicar, o servidor relê a conferência sob o mesmo lock do processamento e recusa `DIVERGENTE_PDV` e `PENDENTE`.

Reenviar Vendas ou corrigir o total volta a conferência para `PENDENTE`, e aí é preciso processar de novo para publicar. Reenviar Compras não mexe na conferência, e a trava não barra a publicação nesse caso.

## O que é cada status

`CONCLUIDO` e `DIVERGENTE_PDV` são o resultado da **conferência**, e não o da análise. `Analise.status_analise` continua `EM_ANALISE` nos dois casos e só vira `CONCLUIDO` quando o consultor publica. Se o ETL gravasse `CONCLUIDO` na análise, o painel do cliente mostraria os KPIs sem revisão humana.

## O que é comparado

O faturamento bruto é a soma das vendas válidas: sem as linhas canceladas e antes de deduzir devoluções e trocas. O KPI `faturamento_total` continua líquido.

Isso depende de o relatório trazer coluna de status ou operação. Sem ela, nada identifica devolução, e o bruto é a soma direta das linhas, com as negativas já incluídas. Linhas que não são produto, como desconto e frete, ficam fora dos dois casos, e um PDV que as some ao total vai divergir.

## Quem vê o quê

| Tela | Conteúdo |
|---|---|
| Painel e telas da análise do consultor | Informado, calculado, divergência, devoluções e cancelamentos |
| Painel do cliente, período liberado pelo contrato | Informado, calculado e divergência, marcados como preliminares |
| Painel do cliente, período não pago | Só o total digitado por um usuário da própria empresa; se o consultor digitou, nenhum valor |
| Sininho do cliente | Aviso sem valores |

Durante a divergência ou com a conferência pendente, ficam bloqueados publicar e gerar texto por IA. Salvar rascunho segue liberado. O PDF, o histórico e a exportação de dados já exigem publicação.

## Como sair da divergência

Na tela da análise, o consultor corrige só o total, quando o erro foi de digitação, ou reenvia o relatório de Vendas. Depois, processa de novo. Fica registrado quem informou o total, mas não há histórico de valores anteriores.

Não existe liberação manual: se os dois totais divergem por um motivo legítimo, a análise fica presa, ou alguém precisa informar o valor calculado para liberá-la.

## Análises sem conferência

Análise enviada antes da trava não tem linha em `validacao_pdv`: ela processa e publica, e a tela de resultado avisa que não houve conferência. Para conferir, informe o total na tela da análise e processe de novo.

## Implantação

A revisão `e2f7a94c1d35` depende de `c7d41e9a2b60`. Aplicar a nova aplica as duas, e o `upgrade` é manual, como nas demais migrações.

Antes da migração, envio, processamento e painéis seguem funcionando, sem campo e sem card, e **toda nova publicação é recusada**: publicar sem a tabela desligaria a trava sem ninguém saber. Corrigir o texto de uma devolutiva já publicada continua liberado. Aplicada a migração, a trava passa a valer em até 30 segundos, sem reiniciar o processo.

O downgrade recusa apagar dados: só remove a tabela vazia. Rode-o com o serviço parado, porque com tráfego ele pode abortar por deadlock, e aí basta repetir. Depois de um downgrade com o processo no ar, reinicie-o, porque a presença da tabela fica guardada.

Os tópicos do Guia "Como fazer o upload dos relatórios?" e "Como exportar os relatórios do meu sistema PDV?", se já estiverem gravados, não mudam com o `seed_guia.py`, que só insere o que falta. Para citarem o total de vendas, ajuste-os em `/admin/guia`.

## Testes

`tests/test_trava_pdv.py` cobre as regras e as telas em SQLite. `tests/test_trava_pdv_postgresql.py` cobre a migração, o RLS e as corridas com lock de linha em um PostgreSQL temporário, e só roda com `NEXO_TEST_POSTGRES_BIN` definido, como na CI.
