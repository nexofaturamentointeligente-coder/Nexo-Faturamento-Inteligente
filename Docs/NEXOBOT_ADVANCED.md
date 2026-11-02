# NexoBot Advanced 2.0

Manual interativo e assistente de navegação do portal do cliente NEXO. Responde, em português do Brasil, perguntas sobre o que o NEXO é, como funciona e como usar cada parte do portal; aprofunda o assunto em foco ("explique melhor", "me dê um exemplo", "e depois?"); consulta o Guia cadastrado no CMS e uma base de conhecimento versionada, verificada no código; e abre somente páginas de um catálogo fechado. Funciona por completo sem serviços externos de IA.

O NexoBot não executa operações: não envia arquivos, não exclui, não publica, não altera senhas, planos ou cadastros e não cria chamados. Ele **explica** inclusive o trabalho da equipe administrativa (conhecer não é poder), mas nunca executa essas ações nem abre páginas restritas.

A geração de devolutivas com IA (`ai_devolutiva.py`) é uma funcionalidade separada e não passa por este código.

## O que mudou na versão 2.0

| Antes (1.x) | 2.0 |
| --- | --- |
| Conhecimento limitado ao Guia do CMS e a onze "fatos" fixos; "o que é o NEXO?" caía em desconhecida. | Base interna versionada (`nexobot/saber/`): 173 registros, 881 formulações, 18 assuntos, cada um com resumo, explicação, passo a passo e aprofundamento, com as fontes do código citadas. |
| Uma resposta por pergunta, sempre no mesmo tamanho. | Quatro níveis de resposta escolhidos pela formulação; aprofundamento sem repetir o que já foi dito; roteiros guiados ("e depois?"). |
| Saudação e "ajuda" concatenavam as categorias do banco, inclusive `ZZTEST-guia` e `ZZTEST-guia <i>XSS</i>`. | Saudação curta e curada, com sugestões clicáveis. Conteúdo com sinais de teste é detectado, rebaixado na busca, excluído das listas e sinalizado no CMS para revisão (nada é apagado). |
| Texto corrido com `**negrito**`. | Blocos estruturados (`cabecalho`, `texto`, `passos`, `secao`, `exemplo`, `aviso`) e sugestões (`sugestoes`) renderizados pelo widget sem HTML. |
| Widget com painel fixo de 360 px. | Widget redesenhado, mobile-first (tela cheia abaixo de 640 px, flutuante acima), com campo de texto multilinha, tratamento do teclado virtual e áreas seguras. |
| Perguntas sobre a administração eram bloqueadas como acesso restrito. | Perguntas são explicadas ("o que faz o administrador?", "o que é a lixeira?"); só pedidos de acesso ("me leve para a área admin") são recusados. |

## 1. Arquitetura

```
mensagem ──► A. normalizacao ──► B. classificacao ──► E. servico ──► JSON (resposta, blocos, sugestoes, acao)
                                   ▲        │             │  ▲
                        D. contexto┘        │             │  └─ providers (opcional, só redação de trechos do Guia)
                                            ▼             ▼
                                     navegacao      C. conhecimento ── Guia (GuiaTopico, CMS)
                                   (catálogo fechado)       └──────── saber (base interna versionada)
```

| Camada | Módulo | Responsabilidade |
| --- | --- | --- |
| A. Normalização | `nexobot/normalizacao.py` | Unicode NFKC, remoção de caracteres invisíveis, acentos, abreviações de chat, repetições, risadas, emojis, limites de tamanho, aspas (comandos citados), interrogação. Corrige erros de digitação apenas em direção ao vocabulário do domínio. As formulações da base interna passam pela mesma normalização ao serem indexadas, para que a comparação com a pergunta seja justa. |
| B. Classificação | `nexobot/classificacao.py` | Segmenta a mensagem em orações, identifica o ato de fala e o alvo de cada uma e resolve conflitos com regras ordenadas. Reconhece perguntas amplas sobre o produto (`sobre_o_nexo`), pedidos de aprofundamento do assunto em foco (`aprofundar`) e comparações entre termos do NEXO (`comparacao`). Um verbo de operação solto dentro de uma pergunta ("como a equipe publica ou despublica?") é parte da pergunta, não uma ordem. |
| C. Conhecimento | `nexobot/conhecimento.py`, `similaridade.py`, `saber/`, `higiene.py` | BM25F sobre os tópicos **ativos** do Guia e sobre a base interna, com sinônimos, aproximação ortográfica, tipo de pergunta ("por que", "quem", "quando", "como"...), correspondência exata de formulação e calibração de confiança. Política de prioridade entre Guia e base interna (seção 3). Detector conservador de conteúdo de teste. |
| D. Contexto | `nexobot/contexto.py` | Memória mínima da conversa mantida pelo widget e revalidada a cada requisição: página em foco, opções oferecidas, convite pendente, **assunto** (id da base interna) e **nível** já mostrado. |
| E. Decisão | `nexobot/servico.py`, `respostas.py` | Escolhe a fonte, o registro e o nível da resposta; monta blocos e sugestões; define a única ação possível (`navegar` ou `sugerir_navegacao`) a partir do catálogo, com URL gerada por `url_for()`. |
| Apoio | `navegacao.py`, `seguranca.py`, `providers.py`, `lexico.py`, `intencoes.py`, `portal.py` | Catálogo de destinos; detectores de URL, área administrativa, injeção e código; IA opcional; léxico; tipos. `portal.py` é um adaptador de compatibilidade: os antigos "fatos" agora apontam para registros da base interna. |

`suporte_bot.py` continua sendo a fachada usada pela API (`responder`).

### Intenções

`navegar`, `orientacao_navegacao`, `duvida_funcional`, `sobre_o_nexo`*, `aprofundar`*, `comparacao`*, `consulta_dados`, `suporte_humano`, `sobre_o_bot`, `ambiguidade`, `comando_negado`, `operacao_nao_permitida`, `acesso_restrito`, `pagina_inexistente`, `pagina_atual`, `fora_do_escopo`, `entrada_suspeita`, `saudacao`, `despedida`, `ajuda_geral`, `confirmacao`, `recusa`, `vazia`, `sem_texto` e `desconhecida` (* novas na 2.0).

Ordem de resolução: segurança (URL, injeção, código, dados de outras empresas, **pedido de acesso** à administração) > comando citado/hipótese > operação proibida > pedido de valores > aprofundamento do assunto em foco > negação > ordem de navegação > ajuda > assunto externo > confirmação/escolha > atendimento humano > comparação > pergunta ampla sobre o produto > pergunta/tarefa/tópico > cortesia > desconhecida.

Menções à administração só são recusadas quando a mensagem pede acesso ou navegação (verbos de acessar/entrar/abrir, senha, login, "virar admin"); perguntas sobre o que a equipe faz seguem para a base de conhecimento.

### Regras de navegação

| Regra | Exemplo | Comportamento |
| --- | --- | --- |
| Ordem explícita | "Abra meu histórico." | Navega, sem confirmação intermediária. |
| Pergunta de localização | "Onde fica meu histórico?" | Explica e oferece botão; não sai da página. |
| Pergunta de procedimento | "Como abro um chamado?" | Responde com o passo a passo da base (ou do Guia) e oferece o botão da página. |
| Contexto | "Me leve para aquela página." | Navega só se houver destino válido na conversa atual. |
| Negação | "Não quero abrir o dashboard." | Nunca navega. |
| Comando citado | "Se eu escrever abra o dashboard, o sistema vai funcionar?" | Explica como o bot funciona. |
| Múltiplos destinos | "Abra o histórico ou o dashboard." | Pede esclarecimento com botões de escolha. |
| Operação proibida | "Apague meus relatórios e abra o dashboard." | Recusa a operação, oferece a página como botão e liga a conversa à regra que explica o porquê. |
| Pergunta sobre operação | "Como a equipe publica ou despublica uma análise?" | Explica a operação (sem executar). |
| Página inexistente | "Abra a página de investimentos." | Informa que não é um destino disponível e lista os válidos. |
| Página atual | "Abra o histórico" estando no Histórico | Informa e não recarrega. |
| Área administrativa | "Me leve para a área administrativa." | Recusa, explica que é exclusiva da equipe e oferece saber o que a equipe faz. |
| Segurança | links, `javascript:`, caminhos | Bloqueia; nunca usa texto da mensagem como URL. |

Uma ordem só navega quando a página é **nomeada**. Assuntos apenas relacionados viram sugestão com botão.

### Catálogo fechado

| Chave | Endpoint | Rótulo |
| --- | --- | --- |
| `dashboard` | `cliente.dashboard` | Visão Geral |
| `upload` | `cliente.upload` | Enviar Relatórios |
| `historico` | `cliente.historico` | Histórico de Análises |
| `guia` | `cliente.guia` | Guia |
| `suporte` | `cliente.tickets` | Suporte (lista e formulário; não cria chamado) |
| `configuracoes` | `cliente.configuracoes` | Configurações |

Para acrescentar uma página: crie a rota GET com as permissões adequadas, acrescente o `Destino` e seus padrões em `nexobot/navegacao.py` e testes de navegação e de bloqueio. Tópicos do Guia e registros da base interna nunca criam destinos: `destino` de um registro só pode ser uma destas chaves (verificado em teste).

## 2. Base de conhecimento interna (`nexobot/saber/`)

### Modelo

Cada registro é um `Conhecimento` (`nexobot/saber/modelo.py`) com id estável (`nexo.<assunto>.<tema>`), título, categoria e quatro níveis de conteúdo:

| Nível | Campo | Quando aparece |
| --- | --- | --- |
| 1 | `resumo` | Sempre; sozinho em pedidos curtos ("resumidamente", "em uma frase"). |
| 2 | `explicacao` | Perguntas de entendimento ("o que é", "por que", "explique"). |
| 3 | `passos` | Perguntas de procedimento ("como faço", "passo a passo") e registros do tipo procedimento/roteiro; "como funciona" em registros de visão geral. |
| 4 | `detalhes` + `exemplo` | Pedidos aprofundados ("em detalhes", "tudo sobre") e "explique melhor" depois do nível 2. |

Campos de apoio: `aviso` (sempre exibido quando existe, por exemplo a regra do Indicador de Pressão de Estoque), `perguntas` (formulações equivalentes usadas na busca), `termos` (palavras-chave), `relacionados`, `proximo` (próximo passo de um roteiro), `destino` (página do catálogo), `tipo` (`conceito`, `procedimento`, `regra`, `roteiro`, `visao_geral`, `tecnico`) e `fontes` (arquivos do código que embasam o texto).

Regras editoriais, verificadas por `tests/test_nexobot_saber.py`: toda fonte citada existe no repositório; nenhum registro contém URL, HTML ou instruções; nenhum preço de plano; `.xls` só aparece como formato **não** aceito; lucro, margem e CMV só aparecem como o que o NEXO **não** calcula; exemplos são sempre hipotéticos e dizem isso; ids únicos; referências cruzadas e glossário apontam para ids existentes.

### Hierarquia de fontes e divergências

Ordem de verdade usada para escrever cada registro: **código** (rotas, modelos, templates, ETL) > Guia/seed > README e relatórios > textos de marketing. Quando um texto da interface diverge do código, o registro descreve o comportamento implementado e a divergência fica registrada no docstring do módulo. Divergências registradas (não corrigidas aqui, por serem regras de negócio ou texto de tela):

- O aviso de conta suspensa na Visão Geral diz que "novas entregas estão temporariamente bloqueadas"; o código bloqueia apenas a abertura de novas análises para empresas não ativas, e o envio para uma análise já aberta continua possível (`empresas_planos.py`).
- O editor da devolutiva tem seis campos, mas a página do cliente e o PDF exibem apenas resumo executivo e conclusão estratégica; pontos de alerta e recomendações chegam ao cliente como semáforos automáticos (`devolutivas_historico.py`).
- Arquivos acima de 20 MB são recusados pelo servidor com a página genérica de erro 413, não com uma mensagem na página de envio (`uploads.py`).
- O selo `PROCESSADO` dos arquivos só é visível à equipe: após o processamento com sucesso a análise sai de Enviar Relatórios e aparece como Em análise na Visão Geral (`processamento.py`, `uploads.py`).
- O diagnóstico detalhado de colunas não reconhecidas chega apenas à equipe; o cliente vê o selo `ERRO` e pode reenviar (`uploads.py`, `processamento.py`).

Todos os 172 registros passaram por uma verificação independente contra o código (48 achados corrigidos antes desta versão, entre eles: notificações por etapa, classificação ABC no limiar de 80 %, ordem fixa dos semáforos, usuário único por empresa, rótulos dos selos em caixa alta).

### Matriz de cobertura

| Assunto | Módulo | Registros | Formulações | Com passos | Com exemplo | Com aviso |
|---|---|---:|---:|---:|---:|---:|
| Visão Geral (o que é, para que serve, como funciona) | `visao_geral.py` | 8 | 74 | 1 | 0 | 0 |
| Primeiros passos | `primeiros_passos.py` | 7 | 47 | 3 | 0 | 0 |
| Acesso, senha e Configurações | `acesso.py` | 11 | 89 | 3 | 0 | 0 |
| Empresa e cadastro | `empresas_planos.py` | 7 | 35 | 2 | 0 | 0 |
| Planos | `empresas_planos.py` | 4 | 23 | 0 | 0 | 0 |
| Análises | `analises.py` | 14 | 66 | 1 | 0 | 0 |
| Envio de relatórios | `uploads.py` | 16 | 86 | 6 | 0 | 0 |
| Processamento (ETL) | `processamento.py` | 9 | 43 | 0 | 0 | 0 |
| Indicadores | `indicadores.py` | 14 | 69 | 1 | 1 | 4 |
| Curva ABC | `indicadores.py` | 8 | 34 | 0 | 2 | 0 |
| Visão Geral do painel | `dashboard.py` | 10 | 43 | 0 | 0 | 0 |
| Devolutiva | `devolutivas_historico.py` | 10 | 43 | 0 | 0 | 0 |
| Histórico e PDF | `devolutivas_historico.py` | 7 | 32 | 1 | 0 | 0 |
| Notificações | `notificacoes.py` | 7 | 30 | 0 | 0 | 0 |
| Suporte | `suporte_guia.py` | 10 | 45 | 2 | 0 | 0 |
| Guia | `suporte_guia.py` | 7 | 26 | 0 | 0 | 0 |
| Equipe NEXO (administração, explicada ao cliente) | `administracao.py` | 14 | 51 | 0 | 0 | 0 |
| Tecnologia | `tecnologia.py` | 10 | 45 | 0 | 0 | 0 |
| **Total** | 14 módulos | **173** | **881** | 20 | 3 | 4 |

Outros números: 61 arquivos do código citados como fonte; 119 termos no glossário; 14 encadeamentos `proximo`; 6 roteiros guiados (`nexo.primeiros_passos.roteiro`, `nexo.uploads.primeiro_envio`, `nexo.uploads.substituir`, `nexo.analises.nao_aparece`, `nexo.indicadores.interpretar`, `nexo.suporte.falar_com_equipe`). As perguntas obrigatórias e as variações linguísticas cobertas por teste estão em `tests/test_nexobot_saber.py` (`OBRIGATORIAS`, `VARIACOES`); todo título de registro é testado como pergunta.

### Glossário

`nexobot/saber/glossario.py` mapeia termos do NEXO ("faturamento", "classe A", "sininho", "supabase") ao registro que os define. Usos: responder "o que é X?" quando X é uma palavra que a busca lexical trataria como genérica, e montar comparações ("qual a diferença entre faturamento, compras e pressão de estoque?"). Termos ambíguos sozinhos (`_SO_EM_COMPARACAO`, como "cliente" ou "análise") só valem em comparações. O glossário decide quando a busca não tem resposta forte, quando aponta para o mesmo registro ou quando a pergunta é apenas a definição do termo; nos demais casos ("quem controla os planos?") a busca é mais específica e vence.

### Como acrescentar ou corrigir um registro

1. Verifique o comportamento no código e anote os arquivos (`fontes`).
2. Edite o módulo do assunto em `nexobot/saber/` usando `registro(...)`; id no formato `nexo.<assunto>.<tema>`; escreva `resumo` curto, `explicacao` e, se houver procedimento, `passos`; inclua formulações variadas em `perguntas` (como o cliente perguntaria) e palavras-chave em `termos`.
3. Se o termo tiver nome próprio, acrescente-o ao glossário; se fizer parte de um roteiro, ligue `proximo`.
4. Rode `python -m unittest tests.test_nexobot_saber` (integridade, regras editoriais, matriz de perguntas e títulos) e acrescente a pergunta à lista `OBRIGATORIAS` quando ela for parte do contrato de atendimento.

## 3. Guia (CMS) × base interna e higiene de conteúdo

- Fonte autorizada da equipe: tabela `guia_topico` (sem migrations novas). Somente tópicos `ativo = true` entram no índice. Cache por aplicação; inserção, edição, desativação, exclusão e operações em massa pelo ORM invalidam o índice no commit; alterações externas são refletidas em até 60 s (TTL). A parte da base interna do índice é construída uma vez por processo; a reconstrução a cada mudança do Guia custa poucos milissegundos.
- Prioridade (`BaseConhecimento._escolher`): um tópico do Guia com confiança alta vence, salvo quando a base interna é claramente mais confiante e trata de **outro** assunto; a base interna com confiança alta vence, salvo quando há um tópico do Guia sobre o **mesmo** assunto (títulos equivalentes) com confiança ao menos média. Assim a equipe sobrepõe a base interna escrevendo no CMS um tópico com título equivalente. Entre correspondências médias, o Guia vence, a não ser que a base seja claramente mais específica.
- Confiança calibrada: cobertura ponderada por IDF, reduzida quando só sinônimos casam ou quando o assunto não aparece no título. Abaixo de 0,45 nada é apresentado; entre 0,45 e 0,6 a resposta é sinalizada. Desempates: formulação mais parecida ou idêntica, tipo de pergunta em comum e pontuação BM25.
- **Higiene**: `nexobot/higiene.py` detecta, de forma conservadora, tópicos com sinais inequívocos de conteúdo de teste: prefixos `zztest`/`test_`/`tmp_`, categorias exatamente iguais a "teste", "sandbox", "debug" etc., HTML na categoria ou no título, "lorem ipsum", a palavra "xss". Categorias legítimas como "Testes de integração" ou "PDV" não são marcadas (coberto por teste). Esses tópicos não aparecem na saudação, na ajuda nem na lista de categorias; na busca têm a confiança reduzida à metade (só uma pergunta praticamente idêntica os alcança, com confiança média); são registrados no log na carga do índice; e o CMS (`/admin/guia`) exibe um aviso e o selo "Possível teste" ao lado deles. Nada é apagado automaticamente.
- Origem dos registros `ZZTEST-guia` e `ZZTEST-guia <i>XSS</i>` observados em produção: foram criados pelo CMS (rota `admin.guia_novo`, que aceita qualquer categoria), provavelmente ao testar a página na própria produção. Não vêm de `seed_guia.py` nem dos testes automatizados (que usam SQLite temporário). A lista para revisão administrativa é gerada por `python tools/auditar_guia.py` (somente leitura; `--json` para anexar a um chamado); a desativação ou exclusão é feita pela equipe no CMS, mediante autorização.
- O conteúdo dos tópicos e dos registros é tratado como dado: não é interpretado como instrução, não define URLs e só aponta para destinos do catálogo.
- Falha de banco ao ler o Guia: rollback, aviso no log (somente o tipo do erro) e resposta com a base interna; a navegação continua funcionando.

## 4. Conversa: níveis, aprofundamento, roteiros e comparações

| Pedido | Comportamento |
| --- | --- |
| "O que é a Curva ABC?" | Nível 2: cabeçalho, resumo e explicação; chips "Explique melhor", "Me dê um exemplo", "E depois?" e relacionados. |
| "Resumidamente, o que é a Curva ABC?" | Nível 1: só o resumo. |
| "Como envio meus relatórios?" | Nível 3: resumo e passo a passo numerado, com botão para Enviar Relatórios. |
| "Me explica a Curva ABC em detalhes." | Nível 4: tudo, inclusive seção de detalhes e exemplo hipotético. |
| "Explique melhor" (com assunto em foco) | Mostra apenas as partes ainda não vistas (nível 2 → 4). |
| "Me dê um exemplo" | Só o exemplo hipotético; se não houver, avisa e oferece aprofundar. |
| "Passo a passo" / "na prática" | Os passos; para conceitos sem procedimento, avisa que é um conceito. |
| "Pode resumir?" / "Não entendi" | Resumo curto / explicação de outra forma (resumo + exemplo), com o aviso quando existir. |
| "E depois?" / "Qual é o próximo passo?" | Segue o roteiro (`proximo`) com o prefixo "Próximo passo:"; no fim do roteiro, oferece outros temas. |
| "Onde fica isso?" | Orientação da página do assunto em foco, com botão. |
| "Como funciona isso?" | Com assunto em foco, aprofunda esse assunto; sem contexto, explica como o NEXO funciona. |
| "Como envio os relatórios e quando posso ver os resultados?" | Até duas perguntas respondidas na mesma mensagem, cada uma com cabeçalho. |
| "Qual a diferença entre faturamento, compras e pressão de estoque?" | Comparação com uma seção por termo (e o aviso do indicador). Quando um registro já contrapõe os termos ("mensal e quinzenal", "classes A, B e C", "NexoBot e atendimento humano"), ele responde. |
| "O NEXO calcula lucro?" | Pergunta sobre o produto (não pedido de dados): explica a regra do que o NEXO não calcula. |
| "Crie uma análise para mim." | Recusa explícita (`operacao_nao_permitida`), com chip para a regra que explica o porquê. |

Os níveis e o assunto em foco vivem no `contexto` devolvido ao widget (`assunto`, `nivel`), validados a cada requisição: só ids existentes na base interna e níveis de 0 a 4 são aceitos.

## 5. Contrato de `POST /api/suporte-bot`

Entrada (JSON, até 16 KB):

```json
{"mensagem": "explique melhor",
 "contexto": {"ultimo_destino": "dashboard", "destino_anterior": null, "opcoes": [], "modo_opcoes": null,
              "oferta": null, "assunto": "nexo.indicadores.pressao_estoque", "nivel": 2},
 "pagina_atual": "dashboard"}
```

`mensagem` é obrigatória como texto (`null` vira mensagem vazia). `contexto`, `pagina_atual` e o formato legado `ultimo_destino` são opcionais; qualquer valor fora do catálogo ou da base interna é descartado.

Saída:

| Campo | Descrição |
| --- | --- |
| `resposta` | Texto puro equivalente aos blocos (o widget interpreta apenas `**negrito**` e quebras de linha quando não há blocos). Em navegação direta: "Abrindo …". |
| `blocos` | Lista de blocos: `{"tipo": "cabecalho", "texto"}`, `{"tipo": "texto", "texto"}`, `{"tipo": "passos", "titulo", "itens": [str]}`, `{"tipo": "secao", "titulo", "texto"}`, `{"tipo": "exemplo", "texto"}`, `{"tipo": "aviso", "texto"}`. O widget ignora tipos desconhecidos e itens que não sejam texto. |
| `sugestoes` | Até quatro chips `{"rotulo", "mensagem"}`. Um chip apenas envia `mensagem` como nova mensagem do usuário; nunca navega nem carrega URL. |
| `fonte` | `local` ou `hf`. |
| `acao` | `null`, `navegar` ou `sugerir_navegacao`. |
| `destino` | Chave do catálogo ou `null`. |
| `url`, `rotulo_botao` | Presentes apenas com `acao`; a URL vem de `url_for()`. |
| `opcoes` | Botões de escolha em esclarecimentos: `[{destino, url, rotulo_botao}]`. |
| `contexto` | Contexto que o widget deve reenviar na próxima mensagem (inclui `assunto` e `nivel`). |
| `meta` | `intencao`, `confianca` (0–1), `origem` (`guia`, `portal` = base interna, `catalogo`, `sistema`, `ia`), `motivo`, `assunto`, `nivel`, `correlacao` (id para cruzar com o log) e `degradado` quando o Guia não pôde ser lido. |

Códigos: 200 (inclui mensagens desconhecidas), 400 (JSON malformado, tipo errado, CSRF ausente/inválido, sempre em JSON), 302 (sem sessão, primeiro acesso, conta desativada, sessão de outro boot), 403 (ADMIN), 413 (corpo grande), 429 (30 mensagens/minuto por usuário, em JSON), 500 (falha inesperada, registrada com traceback e `correlacao`). As respostas levam `Cache-Control: no-store` e `X-Content-Type-Options: nosniff`.

Compatibilidade: todos os campos da 1.x continuam presentes com o mesmo significado; `blocos` e `sugestoes` são adicionais e um cliente antigo pode ignorá-los.

## 6. Widget (`templates/base.html`, `static/js/nexobot.js`, `static/css/nexo.css`)

- Renderizado apenas para usuários CLIENTE. Cabeçalho com avatar, título e subtítulo honesto ("Assistente do portal NEXO · responde com a base de conhecimento do portal"); nenhuma promessa de IA externa.
- Saudação curta igual à do servidor, com cinco sugestões iniciais ("Como funciona o NEXO?", "Enviar relatórios", "Entender meus indicadores", "Ver minhas análises", "Preciso de ajuda").
- Blocos e chips são construídos com `createTextNode`/`textContent`; o JavaScript não usa `innerHTML`, `insertAdjacentHTML`, `eval` nem `Function`. Tipos de bloco desconhecidos são ignorados. Ações só são aceitas quando destino e URL coincidem exatamente com o catálogo renderizado pelo servidor e são da mesma origem. O contexto guardado na memória da aba aceita apenas chaves do catálogo, ids `nexo.x.y` e níveis 0–4.
- Campo de texto multilinha com crescimento automático (até 132 px), `Enter` envia, `Shift+Enter` quebra linha, botão de envio desabilitado sem texto, `maxlength=1000`, `enterkeyhint="send"`, fonte de 16 px (evita zoom automático no iOS).
- Mobile-first: abaixo de 640 px o painel ocupa a tela (`position: fixed; inset: 0; height: 100dvh`), o botão flutuante desaparece enquanto aberto e o `visualViewport` ajusta a altura quando o teclado virtual abre; `env(safe-area-inset-*)` nas bordas; sem `overflow: hidden` no `body`; sem rolagem horizontal em 320, 360, 390, 412, 768 e 1440 px (verificado em navegador real). A partir de 640 px o painel é flutuante (400 × 620 px; 420 × 660 px a partir de 1200 px); há ajuste para alturas até 480 px e para `prefers-reduced-motion`.
- Acessibilidade: `role="dialog"`, `aria-labelledby`/`aria-describedby`, `role="log"` com `aria-live="polite"`, `aria-expanded`, `aria-busy` durante o envio, `Escape` fecha e devolve o foco ao botão, `:focus-visible` em todos os controles.
- Identidade: apenas tokens do design system (`--grad`, `--primary-soft`, `--surface-2`, `--border`, `--text-muted`, `--warning-bg`, `--font-sans`), sem paleta concorrente; uma única animação leve.
- Evidências visuais (antes/depois, por largura) em `Docs/nexobot/evidencias/`.

## 7. Contexto da conversa

O servidor não guarda estado. O widget mantém, na memória da aba, e reenvia:

- `ultimo_destino` / `destino_anterior`: página da conversa ("me leva pra lá", "volta para a anterior").
- `oferta`: definido só quando a resposta terminou com um convite e botão; "sim" só navega logo depois de um convite.
- `opcoes` / `modo_opcoes`: escolha entre opções oferecidas ("o segundo", "o histórico").
- `assunto` / `nivel`: registro da base interna em foco e nível já mostrado, para aprofundar sem repetir e para "e depois?", "onde fica isso?", "como funciona isso?".
- Um destino nomeado na mensagem sempre prevalece sobre o contexto. Assuntos não relacionados limpam o contexto; cortesias o mantêm.

## 8. Integração opcional com IA

Com `HF_API_TOKEN` definido, o modelo apenas **redige** a resposta de perguntas respondidas por trechos do Guia (até três trechos já escolhidos localmente). Registros da base interna, navegação, operações, segurança, saudações e assuntos externos nunca passam pela IA. A pergunta enviada tem e-mails, telefones, documentos, valores, números longos e senhas explícitas removidos; tempo limite de 3 s para conectar e 10 s para ler; sem redirecionamentos. A saída é descartada se estiver vazia, longa demais, contiver links, caminhos, HTML, código, termos financeiros proibidos, alegação de ter executado ações, eco de instruções ou números ausentes dos trechos. O modelo nunca define `acao`, `destino` ou `url`. Endpoint: roteador `https://router.huggingface.co/v1/chat/completions`; modelo padrão `meta-llama/Llama-3.1-8B-Instruct`, configurável por `HF_MODEL`.

## 9. Entradas inesperadas

| Entrada | Comportamento |
| --- | --- |
| Vazia, só espaços, `null` | 200 com convite para escrever a dúvida e sugestões iniciais. |
| Só emojis, símbolos ou risadas | 200 pedindo uma frase. |
| Erros de digitação | Corrigidos para termos do domínio, sem corrigir palavras comuns. |
| Frases longas | Truncadas em 1000 caracteres; corpo HTTP acima de 16 KB recebe 413. |
| Várias perguntas | Até duas são respondidas na mesma mensagem. |
| Contraditórias ("abra o dashboard, não o histórico") | O destino negado é excluído. |
| Verbo de operação dentro de uma pergunta ("posso pedir para criar, excluir ou publicar algo?") | Tratado como pergunta; a regra é explicada. |
| JSON malformado, tipos errados | 400 em JSON, sem chamar o motor. |
| Contexto inválido (`assunto` inexistente, `nivel` fora de 0–4, destino fora do catálogo) | Descartado; nunca define destino nem assunto. |
| URLs, `javascript:`, `data:`, caminhos, IPs | Bloqueados (`acesso_restrito`). |
| HTML/JavaScript, SQL, Python, shell | `entrada_suspeita`; nada é executado; o texto é sempre exibido como texto. |
| Injeção de prompt | `entrada_suspeita`; regras inalteradas. |
| Dados de outras empresas | Recusa. |
| Pergunta sem resposta na base ("xptoqwe") | Pede reformulação, com sugestões; nunca inventa. |
| Assuntos externos, poemas, investimentos | Explica o escopo. |
| IA indisponível ou saída inválida | Resposta local; aviso no log. |
| Guia vazio ou indisponível | Base interna e orientação por página. |
| Falha inesperada | 500 com `correlacao`; traceback no log sem a mensagem do cliente. |

## 10. Segurança

Preservados sem alteração: Flask-Login, perfis ADMIN/CLIENTE (ADMIN recebe 403), CSRF (`X-CSRFToken`), primeiro acesso, invalidação por `boot_id`, conta desativada, isolamento por empresa (o bot não consulta dados de empresas), configurações de produção do Render, Supabase, `render.yaml`, `wsgi.py` e migrations.

Garantias: limite de 30 mensagens/minuto por usuário; respostas JSON para CSRF e 429 em `/api/`; limite de corpo; `no-store` e `nosniff`; widget sem APIs que interpretam HTML; ações aceitas pelo navegador apenas quando destino e URL coincidem com o catálogo do servidor e são da mesma origem; chips só enviam mensagens; contexto restrito a chaves do catálogo, ids da base interna e níveis válidos; logs sem o texto das mensagens; conteúdo do Guia e da base interna tratado como dado. Conhecer não é poder: as operações administrativas são explicadas, nunca executadas; a área administrativa nunca é aberta; o bot não pede senhas, tokens ou dados secretos e não cita configurações sensíveis.

Observação de auditoria mantida: no Render, clientes de empresas `CANCELADA` continuam podendo entrar no portal; o NexoBot segue as mesmas permissões das páginas do cliente.

**Atualização (26/10/2026, NEXO-AJ-06.1):** a observação acima descreve o código anterior. Com a AJ-06.1, o CLIENTE de empresa `CANCELADA` é negado no login, nas sessões já abertas, nas rotas do portal, na API do NexoBot e no Socket.IO (`session_security.py`); `SUSPENSA` mantém o acesso. Em produção isso só vale depois de a versão ser publicada com o banco na revisão `e4c004a26b01`.

## 11. Testes

```bash
python -m unittest discover -s tests -p "test_*.py" -v          # suíte completa
python -m unittest discover -s tests -p "test_nexobot*.py" -v   # somente NexoBot
NEXO_TEST_BROWSER=1 python -m unittest tests.test_nexobot_navegador -v   # Chromium real (Playwright)
# com um Chromium já instalado: NEXO_TEST_CHROMIUM=/caminho/do/chrome
```

| Arquivo | Conteúdo |
| --- | --- |
| `tests/nexobot_corpus.py` | Corpus de frases por comportamento esperado. |
| `test_nexobot_saber.py` | Integridade da base interna (schema, fontes, regras editoriais, referências); detector de higiene; matriz de 52 perguntas obrigatórias com fragmento esperado na resposta, 45 variações linguísticas e todos os títulos como pergunta; conversa (níveis, aprofundamento sem repetir, roteiro "e depois?", "como funciona isso?", "onde fica", multiassunto, comparações, contrato de blocos/chips, conhecer ≠ executar, contexto validado, desconhecido sem inventar); Guia com tópicos de teste (saudação limpa, log, CMS sinalizando). |
| `test_nexobot_linguagem.py` | Normalização, corpus de intenções, regras de navegação e contexto. |
| `test_nexobot_conhecimento.py` | Precisão e recusa da busca, ambiguidade, prioridade do Guia sobre a base interna (inclusive mesmo assunto), base grande, cache (ORM, operações em massa, rollback, TTL, CMS) e falha de banco. |
| `test_nexobot_api.py` | Contrato HTTP, conversas com contexto, autenticação, CSRF, JSON inválido, 413, 429, isolamento entre usuários, ausência de efeitos nos dados, IA simulada e concorrência. |
| `test_nexobot_regressao_original.py` | Todos os cenários do NexoBot do repositório original, no contrato atual. |
| `test_nexobot_robustez.py` | Fuzzing determinístico, mutações com erros de digitação (nunca navegar para destino diferente) e fuzzing da API. |
| `test_nexobot_desempenho.py` | Latência e memória medidas. |
| `test_nexobot_interface.py` | Verificações estáticas do widget: sem APIs de HTML, validação de blocos por tipo, chips só enviam mensagens, requisição segura, sessão expirada, contexto validado, textarea, teclado virtual, ARIA, CSS mobile-first e tokens do design system, saudação idêntica à do servidor. |
| `test_nexobot_navegador.py` | Chromium real: abertura/fechamento, saudação com chips que enviam mensagens, blocos estruturados (aviso e aprofundamento), textarea, ausência de rolagem horizontal em seis larguras (com capturas opcionais por `NEXO_TEST_SHOTS`), painel legível no fim de página longa, resposta incompreendida com sugestões, XSS em sugestões e blocos vindos da API exibido como texto, teclado, envio duplo e layout mobile. |

Os testes de navegador exigem Playwright (`requirements-dev.txt`), que não faz parte de `requirements.txt`. Variáveis do fuzzing: `NEXOBOT_FUZZ_SEED`, `NEXOBOT_FUZZ_N`, `NEXOBOT_FUZZ_API_N`.

## 12. Desempenho medido

Ambiente: contêiner Linux, Python 3.13, SQLite temporário, cliente de teste do Flask (sem rede). O plano gratuito do Render tem 0,1 CPU: espere tempos proporcionalmente maiores.

| Medida | Resultado |
| --- | --- |
| Classificação local (985 amostras) | p50 0,14 ms · p95 0,24 ms · máx. 0,31 ms |
| Resposta completa com índice em cache (591 amostras) | p50 0,29 ms · p95 2,09 ms · máx. 8,25 ms |
| HTTP ponta a ponta no cliente de teste (60 amostras) | p50 2,0 ms · p95 2,4 ms |
| Mensagem de 1000 caracteres | média 4,9 ms |
| Construção da parte interna do índice (173 registros, 881 formulações normalizadas) | ≈ 280 ms, uma vez por processo |
| Reconstrução a cada mudança do Guia (Guia do seed) | média 1,5 ms |
| Busca isolada na base interna | p50 0,56 ms · p95 0,91 ms |
| Memória após 3000 mensagens únicas | +1,0 MB (caches LRU com tamanho máximo) |

Sem IA externa, nenhuma mensagem gera chamada de rede. Nenhuma dependência nova foi adicionada.

## 13. Limitações conhecidas

- Reconhecimento baseado em regras, léxico e recuperação lexical (BM25F com sinônimos e formulações). Paráfrases muito distantes do vocabulário do portal podem pedir reformulação; o bot então oferece sugestões em vez de inventar.
- A base interna descreve o comportamento atual do código; mudanças nas telas ou nas regras exigem atualizar o registro correspondente (e seu teste).
- Tópicos do Guia não têm níveis: "explique melhor" sobre um tópico do CMS repete o texto do tópico; os níveis valem para a base interna.
- Erros de digitação em palavras curtas e combinações de vários erros podem não ser corrigidos; nesses casos o bot pede esclarecimento, não navega.
- O limite de 30 mensagens/minuto usa o armazenamento do Flask-Limiter configurado (`memory://` por padrão).
- O contexto vive na memória da aba: recarregar a página recomeça a conversa.
- A redação por IA não foi testada contra o serviço real neste ambiente; todos os modos de falha foram simulados.

## 14. Manutenção e depuração

- Resposta errada? Veja `meta` no JSON (ou o log `nexobot correlacao=… intencao=… origem=… assunto=…`): `intencao`/`motivo` dizem a rota tomada pelo classificador; `origem` diz se veio do Guia, da base interna ou do catálogo; `assunto` é o registro escolhido. Para inspecionar a busca: `BaseConhecimento._buscar(indice_fatos, palavras, tipo)` devolve (ordem, confiança, trecho) por candidato.
- Pergunta ampla não reconhecida: padrões em `_PRODUTO` (`classificacao.py`). Pedido de aprofundamento não reconhecido: `_APROFUNDAR`. Comparação: `_COMPARA` e `_COMPARACOES_CONHECIDAS` (`servico.py`).
- Nível errado: `nivel_pedido` em `servico.py` (`_PEDE_CURTO`, `_PEDE_PROFUNDO`, `_PEDE_PASSOS`) e `partes_do_nivel` em `respostas.py`.
- Novo sinônimo: `GRUPOS_SINONIMOS` em `lexico.py`. Palavra "corrigida" indevidamente: `PALAVRAS_COMUNS`. Nova forma de pedir uma página: `_PADROES` em `navegacao.py`. Novo verbo de operação proibida: `VERBOS_OPERACAO` em `lexico.py` e `_OPERACOES` em `respostas.py`.
- Calibração da busca: `LIMIAR_ALTO`, `LIMIAR_MEDIO`, `MARGEM_AMBIGUIDADE`, `VANTAGEM_FATO`, `PENALIDADE_TESTE` em `conhecimento.py`, sempre com `test_nexobot_conhecimento.py` e `test_nexobot_saber.py`.
- Higiene do Guia: `tools/auditar_guia.py` lista os tópicos suspeitos; a decisão é da equipe, pelo CMS. Novos sinais de teste: `nexobot/higiene.py`, com um caso legítimo correspondente em `HigieneTests`.
- Toda mudança deve manter as propriedades testadas: nenhuma variação de uma ordem pode navegar para um destino diferente; nenhum registro afirma preço, `.xls` aceito ou cálculo de lucro; a saudação nunca lista categorias do banco.
