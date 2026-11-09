# Relatório da execução pernoturna (Etapas 2, 3 e 4)

Execução autônoma de 08 para 09/11/2026, pela diretriz "Execução autônoma pernoturna (Prompts 3 e 4 + autodeploy)". Tudo ficou **local**: nenhum push, nenhum deploy e nada aplicado no Supabase.

## Para levar ao `main`

Os três commits estão no ramo local `avaliacao-cenario-etapas-2-a-4`, criado a partir do `main` (`53020e6`). O `main` não foi tocado.

```bash
git checkout main
git merge --ff-only avaliacao-cenario-etapas-2-a-4
```

Antes de publicar, siga a seção 9 de `Docs/AVALIACAO_DE_CENARIO.md`: backup, prévia com `--sql` e as migrações `7c4e2a9b5d31` e `9b3e5d7f1a28`, só com sua autorização. Na publicação, mantenha o `README.md` do repositório remoto.

## Commits

| Commit | Etapa | Mensagem |
|---|---|---|
| `227f495` | 2 | feat(leads): triagem com limiares configuraveis, flags de omissao no erp, matriz por categoria e diagnostico do consultor |
| `1085cc0` | 3 | feat(etl): implementa algoritmos avançados de giro, cauda longa e simulador de caixa |
| `c077b39` | 4 | feat(portal): integra motor de triagem e inteligência aos dashboards do cliente e consultor |

Autor de todos: o do `git config` local (JoaoCesarDev22), sem assinatura, sem coautor e sem menção a ferramenta. Este relatório ficou fora dos commits.

## Etapa 2 (validada e commitada antes das Etapas 3 e 4)

Triagem com limiares configuráveis, as três marcas (venda sem custo, venda abaixo do custo e omissão no ERP), matriz por categoria e o diagnóstico do consultor em JSON, com a migração `7c4e2a9b5d31`. Antes do commit, quatro divergências achadas pelos testes de borda foram corrigidas, cada uma com teste.

## Etapa 3: inteligência de estoque e giro

Módulo novo `inteligencia_estoque.py` (funções puras), regras do lead `lead-2026-11.4` e a migração `9b3e5d7f1a28`.

| Pedido | Entrega |
|---|---|
| Cauda longa e estagnação (60, 90 e 180 dias) | **Estoque parado por idade**: itens com estoque e custo informados há 60, 90 ou 180 dias ou mais sem venda, por faixa e acumulado, com o capital imobilizado (estoque × custo médio) e a participação no capital em estoque. Os dias sem venda são um piso, contados só nos dias que algum relatório cobre; para isso o motor passou a gravar o fim do último relatório com venda de cada produto e os períodos cobertos do envio. **Estoque excedente**: itens que ainda vendem com estoque acima da cobertura-alvo (60 dias), com o capital liberável. |
| Simulador de liberação de caixa (Urgência) | Receita do estoque da Urgência ao preço médio praticado, capital ao custo, cenários de desconto (0, 10, 20 e 30%) e fração vendida configuráveis, e o desconto de equilíbrio. |
| Agrupamento por categoria, PDV e filial dentro dos 384 MB | A matriz por categoria segue completa. O relatório com várias lojas é detectado: o estoque da rede soma o último saldo de cada loja, e o diagnóstico avisa. A coluna de loja lida no lugar do produto faz o envio ser recusado, com orientação. Com 20 mil produtos em dois anos, o cálculo fica abaixo de 64 MiB (há teste). |
| Auditoria de margem pelo markup do segmento | Preço praticado contra custo médio × (1 + markup de referência): o menor entre o markup agregado da categoria e a mediana dela, ou o markup informado pelo consultor. A parte já contada no potencial recuperável não é somada de novo. |
| `tests/test_inteligencia_estoque.py` | 44 testes no commit da Etapa 3 (47 depois da Etapa 4). Cada uma de 21 mutações do código derruba ao menos um deles. |

## Etapa 4: portal, governança e experiência

| Pedido | Entrega |
|---|---|
| Rota de diagnóstico rápida e JSON para gráficos | As linhas saem numa consulta só, sem objetos do ORM. O diagnóstico fica num cache por processo, com chave pelo conteúdo das linhas (sha256), e a leitura repetida não refaz triagem, matriz e inteligência: com 8.000 linhas, cerca de 2 s na primeira leitura e 0,6 s nas seguintes, neste computador. Qualquer mudança gera outra chave; a entrada nunca é servida depois da retenção de 30 dias, sai da memória no primeiro pedido depois de vencer e é descartada na hora no reenvio, na retriagem e no expurgo pela tela. O payload (versão `diagnostico-lead-2026-11.3`) ganhou `visualizacoes`: séries prontas para gráficos (triagem, marcas, caixa resgatável, estoque por idade, categorias, simulador e auditoria). |
| `lead.html` e `leads.html` | No detalhe: o **resumo executivo do caixa resgatável** (quatro parcelas com barra e a natureza de cada uma, que não se somam), a **matriz de triagem** (Urgência, Atenção e Eletiva), as três marcas, o estoque parado e excedente, o **simulador** com formulário (descontos, fração vendida, markup do segmento e cobertura-alvo, com ponto ou vírgula decimal) e a auditoria de preço. Na lista: triagem (U, A, E), marcas e caixa resgatável de cada lead. |
| Isolamento | Um teste percorre **todas** as rotas `/admin` (GET e POST com CSRF): o visitante vai para o login e o cliente recebe 403. Outro percorre todas as rotas fora de `/admin`, com o id real do lead nos parâmetros e o conteúdo do cliente publicado (análise, chamado, notificação e Raio-X), mais o assistente: nenhuma resposta traz dado de lead, nenhuma quebra e nenhum comando SQL desses pedidos toca as tabelas de lead (ouvinte do SQLAlchemy). Um teste estrutural confere que nenhum módulo fora da Avaliação de Cenário cita as tabelas de lead. Dois leads, mesmo com os mesmos relatórios ou já sem linhas, não se misturam. No PostgreSQL real, o ensaio confere a carga por lead e o cache. |

### Revisão adversarial da Etapa 4

Antes do commit, a Etapa 4 passou por seis rodadas de revisão adversarial (workflows com revisores por dimensão e verificadores que tentavam refutar cada achado, reproduzindo em cópias sem `.env`):

| Rodada | Achados | Confirmados | Severidade |
|---|---|---|---|
| 1 | 22 | 21 | nenhum alto; 4 médios em testes e 1 médio na tela |
| 2 (conferência das correções e olhos novos) | 14 | 13 | 2 médios (um teste de navegador que falharia sempre e um oráculo de retenção dependente do próprio código) |
| 3 | 6 | 5 | 2 médios (a coluna de estoque vazia aparecia como R$ 0,00; o Raio-X do cliente fora da varredura) |
| 4 (conferência das correções) | 3, mais uma lacuna de teste | 4 | todos baixos (estoque não informado mostrado como R$ 0,00 na matriz e por um resumo gravado antigo; "(parcial)" na lista sem produto faltando no parado; empate exato do simulador sem teste) |
| 5 (conferência das correções) | 1 | 1 | baixo ("(parcial)" e a nota de soma parcial contavam também o produto sem saldo e sem custo, que fica fora das somas com saldo ou sem) |
| 6 (conferência da correção) | 1 | 1 | baixo (um resumo da versão 11.2 gravado por um código intermediário desta noite, nunca commitado, seria aceito sem uma das contagens novas; as três contagens passaram a ser obrigatórias com estoque informado) |

A correção da sexta rodada só endurece uma validação: foi conferida por testes, por 3 mutações e pelo portão completo, sem uma sétima rodada com agente.

Todos os confirmados foram corrigidos, cada um com teste. As principais correções de comportamento: o cache varre as entradas vencidas a cada pedido e não guarda dado de antes de um descarte; os resumos gravados são validados num lugar só (versão, formato, teto e, com estoque informado, as três contagens de produtos sem saldo, sem erro 500); estoque não informado não vira zero, nem na matriz por categoria (um critério só, `estoque_informado`); com o cálculo atual, o caixa resgatável não usa o resumo gravado; a lista marca o parado como parcial só quando falta nele produto com custo e sem saldo, e a nota de soma parcial da tela conta só os produtos sem saldo e com custo; o equilíbrio negativo do simulador não aparece como desconto; a tela registra a leitura no log como a rota JSON. Testes de mutação: 84 mutações do código da Etapa 4, todas detectadas, menos uma equivalente.

## Suítes (portão de cada commit)

| Etapa | Suíte completa | PostgreSQL real | Navegador |
|---|---|---|---|
| 2 | 1928 testes; 14 failures e 42 errors, todos já conhecidos (52 nomes); nenhuma falha nova | 175 OK | 21 OK |
| 3 | 1974 testes; as mesmas falhas conhecidas; nenhuma nova | 175 OK (leads com 31 verificações) | 21 OK |
| 4 | 2021 testes; as mesmas falhas conhecidas (52 nomes); nenhuma nova | 175 OK (leads com 33 verificações) | 22 OK |

As falhas conhecidas são deste Windows (geração e leitura de PDF sem `pdftotext`, limites do ETL em processo e logs HTTP sensíveis) e existiam antes da execução; a comparação foi feita pelo nome de cada teste.

## Decisões conservadoras (impasses resolvidos sem parar)

1. **Commits num ramo local**, a partir do `main`, que ficou intacto. A diretriz autorizou commits locais; levar ao `main` é um passo seu (comando no início).
2. **`flask db upgrade` não rodou no banco do `.env`.** Ele aponta para o Supabase, que é remoto e não é "banco local". As migrações `7c4e2a9b5d31` e `9b3e5d7f1a28` foram validadas em clusters PostgreSQL 18 temporários (upgrade, CHECKs, downgrade recusado com dados e aceito sem eles, upgrade de novo) e no SQLite dos testes. Aplicar no Supabase continua dependendo da sua autorização.
3. **Sem push e sem deploy.** "Autodeploy" aparece só no título da diretriz; o texto não autoriza, e as regras do projeto exigem autorização explícita.
4. **Várias lojas no mesmo relatório:** leitura como a rede (estoque somado, com aviso), sem separar por loja, e recusa quando a coluna de loja é lida no lugar do produto. Separar por loja multiplicaria as linhas e chegaria perto do teto de 4 MiB e do prazo de 120 s do processo isolado.
5. **"Há mais de 60, 90 e 180 dias"** foi implementado como "60 dias ou mais" (faixas de envelhecimento usuais). As faixas são configuráveis: `(61, 91, 181)` dá o sentido estrito.
6. **Markup do segmento:** não há tabela de markup por segmento no sistema, e nenhuma foi inventada. A referência é a da própria categoria (o menor entre agregado e mediana) ou a informada pelo consultor.
7. **Dias sem venda como piso**, só nos períodos cobertos por relatório, com a marca de lacuna.
8. **Sem estoque informado** (coluna ausente ou vazia), estoque e simulador ficam nulos, e o estoque parado da matriz também: zero diria "nada parado", o que não se sabe.
9. **Versão do resumo da inteligência trocada (11.2).** O resumo gravado pela 11.1 (Etapa 3) podia trazer R$ 0,00 onde o estoque não foi informado e não trazia a contagem de produtos sem saldo. Ele passa a valer nada: a lista mostra o lead sem o parado até um reenvio ou "Refazer a triagem" (dentro dos 30 dias); a tela usa o cálculo atual enquanto as linhas existem e, depois do expurgo, mostra "—". Só afeta bancos de desenvolvimento: a migração `9b3e5d7f1a28`, que cria a coluna, não foi aplicada em produção.
10. **Dashboard do cliente não foi aberto ao lead.** O lead não tem acesso ao portal, e a separação lead/cliente ficou mantida (há teste). As `visualizacoes` já servem a um dashboard futuro, depois do fluxo de conversão e do recorte por plano.
11. **Sem biblioteca nova de gráficos**: barras do Bootstrap e tabelas, sem mexer nos scripts liberados pela CSP.
12. **Cache por conteúdo**, nunca só por tempo: dado mudado nunca é servido velho, e nada é servido depois da retenção.
13. **Falhas já conhecidas deste Windows**: as mesmas 52 de antes, nenhuma nova; não foram mexidas.
14. Da Etapa 2 (já documentadas): sem mês em comum entre os dois anos, a queda de ritmo não é avaliada; limiares gravados por outra versão dão lugar aos padrão atuais, com aviso.

## Pendências para você

- Autorizar (ou não) as migrações `7c4e2a9b5d31` e `9b3e5d7f1a28` no Supabase, depois do backup (seção 9 do documento).
- Decidir: tabela de markup por segmento; separação por loja; dashboard do cliente e o fluxo de conversão do lead; biblioteca de gráficos; limiares da triagem (provisórios); LGPD do lead.
- Apagar o arquivo `C:\JOOCSA~1\nao_usar.txt` e a pasta `C:\JOOCSA~1`, criados por engano numa etapa anterior. A trava de segurança do ambiente não me deixou apagá-los.
- Revisar este relatório e, se quiser, commitá-lo junto com o merge.
