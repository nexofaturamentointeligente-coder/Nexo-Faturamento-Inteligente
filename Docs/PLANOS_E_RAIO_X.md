# Planos por nível de inteligência e Raio-X do PDV

Este documento descreve a arquitetura de planos (BRONZE, PRATA, OURO e CUSTOM), o controle de acesso por funcionalidade, a tela de Raio-X de entrada e os componentes de upgrade. Também registra as decisões tomadas e o que depende do proprietário.

## 1. Como o pedido foi mapeado para o sistema

| Pedido | Implementação | Motivo |
|---|---|---|
| Tabela `plano` (id, codigo, nome, nivel_hierarquico, limite_historico_meses, criado_em) | Tabela nova `plano_nivel`, ligada ao plano comercial pelo código (`plano.nome_plano = plano_nivel.codigo`) | `plano` já existe, com preço, frequência e contratos. As regras de migração do projeto (G.1.5 e E4 de `Docs/REESTRUTURACAO_COMERCIAL/00_02_PROTECAO_BANCO_MIGRACOES_ETAPA_0_2_PR22.md`) proíbem coluna nova, FK ou CHECK alterado em tabela existente. Coluna nova mapeada em `Plano` também quebraria o portal antes da migração e os testes que voltam o banco a revisões anteriores. |
| Tabelas `funcionalidade` e `plano_funcionalidade` | Criadas como pedido; a matriz liga o código do nível ao id da funcionalidade | — |
| `empresa.plano_id` | `empresa.id_plano_atual`, que já existe e é mantido em sincronia pelos contratos | Evita duas fontes para o mesmo dado. |
| `empresa.status_assinatura`, `empresa.data_expiracao_plano` | Derivados em `entitlements.assinatura_da_empresa`: a situação vem de `contract_policy.obter_situacao_contratual` e a expiração, do `data_fim` do contrato vigente | A situação já é aplicada no login, na sessão e em cada rota. Uma coluna nova divergiria dela. |
| RLS com `deny_api_access` para anon/authenticated e acesso total para postgres/service_role | Feito, só onde esses papéis existem (Supabase); os privilégios de anon e authenticated também são revogados | Num PostgreSQL sem esses papéis (testes, restauração isolada), `CREATE POLICY ... TO anon` falharia. |
| `app/services/entitlements.py` | `entitlements.py` na raiz | Um pacote `app/` esconderia o módulo `app.py`: quebraria `flask --app app`, o `gunicorn app:app` e os testes. |
| 403 em JSON com o plano necessário e a URL de upgrade | JSON em `/api/` ou quando o cliente pede JSON; nas páginas, a mesma informação numa tela do portal, também com status 403 | Uma página aberta no navegador não deve mostrar JSON cru. |
| Componentes React/Next.js (ou Jinja) | Macros Jinja em `templates/_planos.html` | O portal é renderizado no servidor. |
| Desfoque em CSS para o Bronze | O servidor troca o valor protegido por `None` antes do template e do JSON; o desfoque é desenhado sobre um marcador fixo | Um desfoque só em CSS entregaria o número verdadeiro no HTML. |

## 2. Níveis e funcionalidades

A matriz é cumulativa: cada nível libera o que o nível abaixo libera.

| Nível | Modelo | Atendimento (texto gravado) | Histórico | Acrescenta |
|---|---|---|---|---|
| BRONZE | Self-Service | 100% digital, sem atendimento consultivo. | 6 meses | Curva ABC de vendas; diagnóstico de produtos parados e ruptura; checklist de ações; **Raio-X do PDV** |
| PRATA | Tech-Touch | Onboarding remoto de 30 minutos e suporte prioritário. | 24 meses | Auditoria de vendas abaixo do custo; margem bruta (R$ e %); diagnóstico de CMV pelo custo informado; alertas de precificação desatualizada |
| OURO | Assisted | Reunião estratégica mensal de 30 minutos. | Sem limite | Curva ABC de clientes e RFM; taxas de meios de pagamento; recompra via WhatsApp (**em implantação**) |
| CUSTOM | High-Touch | Consultor dedicado e onboarding VIP. | Sem limite | DRE consolidada; vendedores, comissões e descontos; seletor multifilial; parsers sob medida (**em implantação**) |

Onde o pedido dava uma faixa, o histórico usa o limite maior (Bronze 3 a 6 meses → 6; Prata 12 a 24 → 24), para não esconder mais do que o necessário de quem já é cliente.

`entitlements.FUNCIONALIDADES_NO_PORTAL` lista o que o portal já entrega. As demais aparecem na página de planos com o selo "Em implantação" e nunca viram item de menu. Assim o portal não oferece como pronto o que ainda não existe.

## 3. Controle de acesso (`entitlements.py`)

- `empresa_tem_acesso(empresa_id, funcionalidade_chave) -> bool` é True quando o plano atual da empresa libera a funcionalidade e a assinatura está **ATIVA**. Ela nega (falha fechada) quando a empresa está suspensa, cancelada, na lixeira ou sem contrato, quando o esquema não existe, quando há erro de banco e quando a chave é desconhecida.
- `@requer_funcionalidade("chave")` protege uma view, sempre depois de `cliente_required`. As respostas de recusa são:
  - **403** com `codigo: "plano_insuficiente"` quando o plano não inclui a funcionalidade, com o plano necessário e `url_upgrade` na âncora do nível, na página de planos;
  - **403** com `codigo: "assinatura_inativa"` quando a assinatura não está ativa, com `url_upgrade` apontando para o Suporte;
  - **503** com `codigo: "recurso_indisponivel"` enquanto a migração não existir no banco, ou se as tabelas existirem sem o catálogo (criadas por `create_all`);
  - **503** com `codigo: "verificacao_indisponivel"` quando um erro de banco impede conferir o plano. Nesse caso não há convite de upgrade, só "tente novamente".

```json
{
  "erro": "Auditoria de vendas abaixo do custo está disponível a partir do plano Prata.",
  "resposta": "…", "fonte": "local", "acao": null, "destino": null,
  "codigo": "plano_insuficiente",
  "funcionalidade": {"chave": "auditoria_abaixo_custo", "nome": "Auditoria de vendas abaixo do custo"},
  "plano_atual": {"codigo": "BRONZE", "nome": "Bronze"},
  "plano_necessario": {"codigo": "PRATA", "nome": "Prata"},
  "url_upgrade": "/onboarding/planos#prata"
}
```

- Nenhuma rota que já existia foi restringida. Curva ABC, semáforos e histórico continuam como estavam para todos os planos.
- O menu do cliente sai de `entitlements.MENU_CLIENTE`. Os itens de sempre mantêm o mesmo HTML. Um item de plano superior aparece com cadeado e o selo UPGRADE e leva à página de planos. Sem o esquema, ou com a assinatura inativa, os itens de plano não aparecem. Uma falha de banco devolve o menu de sempre em vez de derrubar a página.

## 4. Raio-X do PDV

**Entrada.** São de 1 a 36 relatórios de Vendas por produto, o mesmo arquivo das análises mensais, um por mês. Os formatos aceitos são CSV ou XLSX, e o total da requisição vai até `MAX_CONTENT_LENGTH` (20 MB). Arquivos repetidos são recusados. A página confere extensão, quantidade e tamanho somado antes de enviar; acima do limite, o servidor responde 413 com a página "Envio grande demais" (JSON em `/api/`).

**Processamento.**
- Cada arquivo passa pela mesma validação do upload (`etl_inputs.validate_input`) e é lido pelas mesmas funções do ETL, no worker isolado do ETL (`etl_worker`, operação `raio_x`).
- O worker não carrega banco nem configuração e tem prazo de 120 s (o dobro do padrão, dentro do teto).
- O Raio-X ocupa a mesma vaga única de upload e processamento das análises.
- O limite é de 10 envios por hora por usuário, somando a página e a API.
- Os arquivos ficam num diretório temporário e somem com ele. O banco guarda só o resultado, uma linha por empresa (`diagnostico_entrada`), e o envio novo substitui o anterior.
- O worker exige Linux (módulo `resource`). Num Windows de desenvolvimento, o envio responde "A plataforma não oferece o isolamento…", como o processamento das análises.

**Definições** (as mesmas da tela; detalhes em `raio_x.py`, versão das regras `raio-x-2026-11.4`, a do saneamento de 11/2026 descrito em `Docs/SANEAMENTO_MOTOR_ETL.md`; Raio-X gravado com a `11.3` continua legível, sem os campos de CMV líquido):
- **Venda analisada:** linha válida com quantidade e valor maiores que zero. Canceladas saem; devoluções e trocas ficam fora das contas de preço e custo. Um envio sem nenhuma venda analisada é recusado, e nada é gravado.
- **Custo informado:** a coluna de custo do relatório. Coluna de custo por unidade (custo unitário, unid., und., ou custo médio e preço de custo sem "total" no título) é multiplicada pela quantidade. As demais, e as que já trazem a multiplicação no título ("Qtde x Custo"), valem como custo total da linha. Valor e custo de cada venda ficam em centavos, então a perda de cada linha da auditoria fecha com "custo dessas vendas − vendido por".
- **Índice de saúde cadastral (0 a 100):** parte do faturamento analisado que tem custo informado e cujo produto não aparece com código ou nome conflitante.
- **Inconsistências:** códigos com mais de um nome, nomes com mais de um código e produtos com estoque negativo, cada um contado uma vez no envio inteiro; vendas sem valor, contadas por linha; itens com margem negativa.
- **Preço de referência:** o preço de tabela do relatório (coluna de preço unitário), quando há; senão, o preço mais praticado do item no mês (por quantidade; no empate, o menor). A tela mostra a referência de cada item e de onde ela veio.
- **Scanner de omissão do ERP:**
  - Pega itens vendidos mais de uma vez abaixo do preço de referência ou com preços diferentes no mesmo arquivo. É o perfil que relatórios de margem de ERP costumam descartar ou diluir.
  - O NEXO não recebe o relatório do ERP: o scanner aponta os itens com esse perfil, e não afirma que o ERP os descartou.
  - Só diferenças acima de max(R$ 0,01; 0,5% da referência) contam, medidas no preço praticado sem arredondar.
  - Com uma linha por produto e sem preço de tabela (ou com o total calculado a partir dele), a variação não é visível, e a tela diz isso.
- **Potencial recuperável:** por linha, o maior entre (preço de referência × quantidade − valor vendido) e (custo informado − valor vendido). Assim a mesma diferença não é contada duas vezes.
- **Vendas abaixo do custo (auditoria):** por item, só as vendas abaixo do custo: quantidade, valor vendido, custo dessas vendas e a perda, que é a diferença entre os dois.
- **Margem bruta estimada:**
  - Faturamento menos o custo informado, só nas vendas com custo.
  - Não é lucro: não desconta despesas, impostos nem taxas.
  - Não é CMV contábil, que depende de inventário.
  - A regra "sem margem" de `etl_processor.py` continua valendo para os KPIs das análises, para o PDF e para a devolutiva.
- **Precificação desatualizada:** entre o primeiro e o último mês com custo, o custo unitário subiu ao menos 5% e o preço médio subiu menos que o custo menos 2 pontos.

**Recomendação de plano** (`raio_x.recomendar`):
- **PRATA** quando há custo ausente em ao menos 10% do faturamento (ou nenhum arquivo traz a coluna de custo), item com margem negativa ou potencial recuperável de ao menos 1% do faturamento.
- **OURO** quando, além disso, o histórico enviado passa dos 24 meses que o Prata mantém.
- **BRONZE** nos demais casos.
- O widget só oferece upgrade quando o plano sugerido está acima do atual.

**O que cada plano vê** (`raio_x_servico.visao`):

| Dado | Bronze | Prata em diante |
|---|---|---|
| Saúde cadastral, inconsistências, totais do scanner, potencial recuperável e recomendação | Sim | Sim |
| Itens com variação de preço: produto, quantidade, faturamento, preços e diferença | Sim | Sim |
| Custo médio, margem bruta e margem % por item e por mês | Marcador desfocado | Sim |
| Auditoria (vendas abaixo do custo por item, margem por item, precificação) | 403 com o plano necessário | Sim |
| Meses além do histórico do plano | Ocultos, com aviso e o menor plano que mostra todos os relatórios enviados (conta pelo calendário, não pelo número de arquivos) | Até 24 meses no Prata; todos no Ouro |

## 5. Rotas

| Rota | Métodos | Funcionalidade exigida |
|---|---|---|
| `/onboarding/diagnostico` | GET, POST | `raio_x_entrada` |
| `/onboarding/diagnostico/auditoria` | GET | `auditoria_abaixo_custo` |
| `/onboarding/planos` | GET | nenhuma (página informativa; o pedido de mudança abre um chamado no Suporte) |
| `/api/onboarding/diagnostico` | GET, POST | `raio_x_entrada` |
| `/api/onboarding/diagnostico/auditoria` | GET | `auditoria_abaixo_custo` |

Todas exigem login de CLIENTE e passam pela trava dos Termos (428 em `/api/`) e pela troca de senha do primeiro acesso. O POST da API exige o cabeçalho `X-CSRFToken`. A página de planos não mostra preços: a landing e o NexoBot também não mostram, por decisão pendente.

## 6. Banco (revisão `b3e9d6c1f7a2`)

- **Tabelas novas:** `plano_nivel` (chave `codigo`), `funcionalidade`, `plano_funcionalidade` (chave composta) e `diagnostico_entrada` (chave `id_empresa`). Todas as constraints têm nome.
- **FKs:**
  - `diagnostico_entrada.id_empresa` usa `ON DELETE CASCADE`: a exclusão permanente da empresa leva o Raio-X, como a `validacao_pdv` sai com a análise.
  - `id_usuario` usa `SET NULL`.
  - `plano_recomendado` aponta para `plano_nivel`.
- **Dados de referência:** níveis, 15 funcionalidades e 38 pares da matriz, gravados por `INSERT ... WHERE NOT EXISTS` pela chave natural, sem importar `models`.
- **PostgreSQL:**
  - RLS ligado, sem `FORCE`. A aplicação entra como dona das tabelas e não é afetada.
  - Onde `anon` e `authenticated` existem: `REVOKE ALL` e a política restritiva `deny_api_access` (`USING false`, `WITH CHECK false`).
  - Onde `postgres` e `service_role` existem: a política `acesso_servidor`.
- **Downgrade:** recusa quando há Raio-X gravado ou quando o catálogo foi alterado depois da migração. Nos outros casos, remove as quatro tabelas e as políticas.

## 7. Implantação

1. Backup verificado e restauração isolada, conforme `Docs/OPERACAO_BACKUP_RESTAURACAO.md`.
2. `flask db current` (somente leitura) deve mostrar `a8d3f6b91c24`.
3. Aplicar pela revisão: `flask db upgrade b3e9d6c1f7a2`.
4. Conferir no banco:
   - as 4 tabelas;
   - `SELECT codigo, nivel_hierarquico, limite_historico_meses FROM plano_nivel` deve devolver 4 linhas;
   - `SELECT count(*) FROM plano_funcionalidade` deve devolver 38;
   - `SELECT tablename, policyname FROM pg_policies WHERE tablename IN (...)` deve devolver 2 políticas por tabela no Supabase.
5. Publicar o SHA aprovado e conferir com um cliente Bronze:
   - o menu mostra "Raio-X do PDV" e "Auditoria de custos" com UPGRADE;
   - `/onboarding/planos` abre;
   - `/api/onboarding/diagnostico/auditoria` responde 403 com `plano_necessario: PRATA`.
6. A ordem inversa (código antes da migração) também funciona: o menu fica como antes e as rotas novas respondem 503 até a migração.
7. No SQLite local, `python init_db.py` agora monta o banco pelas migrações (antes, `create_all`), então o catálogo e o `alembic_version` vêm junto. A prévia offline (`flask db upgrade a8d3f6b91c24:b3e9d6c1f7a2 --sql`) sai com os valores reais do catálogo.

## 8. Testes

- `tests/test_planos_raio_x.py` (SQLite com migrações reais) cobre:
  - motor (contas conferidas à mão, variação por linha, empate, devoluções, ausência de custo, recomendação, mensagens seguras);
  - catálogo e matriz; `plano` e `empresa` sem colunas novas;
  - downgrade, recusas e o portal sem o esquema com o processo no ar;
  - entitlements; 403 em JSON e HTML; menu por plano e com falha de banco;
  - envio, API, CSRF, erros de envio, vaga ocupada, assinatura suspensa no meio da leitura;
  - isolamento entre empresas, histórico do Bronze, exclusão permanente, trava dos Termos;
  - página de planos e pedido de mudança.
- `tests/test_planos_navegador.py` (opcional, `NEXO_TEST_BROWSER=1`) cobre o convite sobre o dado desfocado (foco, Esc e ponteiro sobre o balão) e o envio de vários arquivos (formato barrado antes do envio, envio vazio que não descarta o seguinte e o botão ocupado durante a leitura). As páginas do Raio-X e de planos também entram nas medições de layout e de CSP de `tests/test_overflow_navegador.py` e `tests/test_csp_navegador.py`.
- `tests/test_planos_postgresql.py` (opcional, `NEXO_TEST_POSTGRES_BIN`) cobre, em PostgreSQL real:
  - RLS sem `FORCE` e as políticas por papel;
  - privilégios padrão revogados e a política restritiva negando mesmo depois de um `GRANT`;
  - catálogo, Raio-X em `NUMERIC`, downgrade e novo upgrade.
- Testes ajustados de propósito:
  - `tests/test_navegacao_admin.py`: os rótulos do menu do cliente Bronze;
  - `tests/test_backup_restore.py`: 30 tabelas e uma linha de Raio-X na fixture;
  - `tests/test_contratos_postgresql.py`: catálogo dos planos nasce com dados de referência.

## 9. Decisões que dependem do proprietário

1. **Regra "o NEXO nunca exibe margem".**
   - Este pedido a muda para o Raio-X a partir do Prata.
   - O `README.md` do repositório ainda diz que o sistema nunca exibe margem. Ele é mantido do remoto na publicação, então precisa de revisão do proprietário.
   - As respostas do NexoBot que diziam o contrário foram ajustadas (`indicadores.py`, `visao_geral.py`, `empresas_planos.py`, `respostas.py`).
   - Continuam sem margem: os avisos do painel e do PDF, que falam das análises.
2. **Plano CUSTOM.**
   - Existe como nível e na página de planos, mas nenhuma empresa pode ser colocada nele: o CHECK de `plano.nome_plano` aceita só BRONZE, PRATA e OURO.
   - Habilitá-lo exige uma migração própria, por dialeto, que altere esse CHECK (proibido nesta etapa pelas regras do projeto), e a definição de preço e contrato.
3. **Limite de histórico no Histórico de análises.**
   - Hoje vale só no Raio-X. Aplicá-lo ao Histórico esconderia de clientes Bronze atuais análises que eles já viam, e o NexoBot afirma que "o histórico publicado continua acessível".
   - Fica pendente de decisão e de comunicação aos clientes.
4. **WhatsApp.**
   - O pedido cita suporte prioritário pelo WhatsApp no Prata, mas não há canal oficial publicado (pendência 14 de `Docs/GESTAO_CONTRATUAL/05_PENDENCIAS_DE_DECISAO_COMERCIAL.md`).
   - O texto gravado diz "suporte prioritário" até o canal existir.
5. **Funcionalidades em implantação.** RFM, taxas de meios de pagamento, recompra via WhatsApp, DRE, vendedores, multifilial e parsers sob medida estão no catálogo, mas não no portal. Precisam de dados que os relatórios atuais não trazem (cliente, meio de pagamento, vendedor, loja) e de desenvolvimento próprio.
6. **Landing e "Como aderir".** Continuam descrevendo os planos pela frequência. A nova grade (e a quarta coluna, se houver) depende do texto comercial aprovado; o teste da landing exige exatamente três botões "Contrate Agora".
7. **Retenção do Raio-X.** Guarda-se só o último de cada empresa. A política de retenção geral continua pendente (pendência 17).
8. **Limiares da recomendação** (10% sem custo, 1% recuperável, 5% de alta de custo). São constantes em `raio_x.py`, ajustáveis com o histórico real dos clientes.
