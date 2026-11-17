# Pré-cadastro, cadastro do lead e Raio-X público

Decisões do dono em 15/11/2026 (pré-cadastro e Raio-X público) e em 16/11/2026 (redesenho da landing, cadastro do
lead frio e envio ao microsserviço). Este documento descreve a landing, os dois diálogos de captação, o envio ao
microsserviço do Raio-X, o Raio-X público e o que falta decidir.

## 1. Decisões do dono

| Tema | Decisão |
| :--- | :--- |
| Nome | NEXO ("Next" foi erro de digitação no pedido). |
| Topo (16/11) | "Entrar" (link de texto) leva ao login do portal; "Cadastre-se" (botão de destaque) abre o cadastro do lead frio. |
| Botões dos planos | Os cards só apresentam os planos (BRONZE, PRATA, OURO e PERSONALIZADO / ENTERPRISE, todos "Sob consulta"). Um CONTRATAR único, logo abaixo do título do CTA final (16/11). |
| Etapa 1 (termos) | **Ligada** desde 16/11: os textos vigentes vêm de `/documentos-legais/vigentes` e cada "Li e aceito" só libera no fim da rolagem do texto. |
| Dados | Desde 16/11, as três saídas da Etapa 3 **gravam o lead** no microsserviço (interesse 1, 2 ou 3). O e-mail passou a ser obrigatório. Plano, faixa de faturamento e dificuldade continuam só na mensagem do WhatsApp. |
| Raio-X no pré-cadastro (16/11) | "REALIZAR RAIO-X" abre, no próprio diálogo, o envio dos relatórios (.csv e .xlsx) da Avaliação de Cenário Externo; o resultado mostra o protocolo e o motivo de cada recusa. |
| Lead frio (16/11) | Diálogo próprio (`#cadastro-lead`): termos, nome, empresa, e-mail e telefone da empresa, os relatórios opcionais e o resultado. Existe com ou sem o número de WhatsApp. |
| Raio-X público | Uma página pública de autocadastro (`/avaliacao-inicial`), construída **desligada**, atrás de `AVALIACAO_PUBLICA_ATIVA`. Desde 16/11 a landing não leva mais a ela. |
| Banco | Migração aditiva `e7a1c3f5b9d2` (Raio-X público). O lead do microsserviço fica na tabela do próprio serviço (`microservico_raiox/README.md`). |
| Planos | "Sob consulta", sem preço: a imagem dos planos não será usada. |

Depois das revisões, o dono decidiu, em 15/11/2026:
- manter o potencial recuperável total sempre visível, mesmo que ele permita deduzir o R$ de um bloco pequeno (seção 7);
- ligar o Raio-X público só quando a infraestrutura estiver pronta (seção 8);
- deixar no backlog a recusa de nome de arquivo só com caracteres não latinos (`Docs/BACKLOG_MELHORIAS.md`, BM-01).

## 2. Landing: topo, CONTRATAR e contatos

- Topo: "Entrar" (`/auth/login`, o único link de login do cabeçalho) e "Cadastre-se" (`href="#contato"`, com
  `data-cadastro-gatilho`). Com JavaScript, o "Cadastre-se" abre o `#cadastro-lead`; sem JavaScript (ou sem suporte
  a `<dialog>`), leva aos contatos do rodapé.
- CONTRATAR: logo abaixo do título "Pronto para transformar seu negócio? Deixe a NEXO fazer parte disso!", desenhado
  pela macro `link_whatsapp` (sem alteração) com a mensagem `whatsapp_textos.contratar` e o número de
  `WHATSAPP_EMPRESA_NUMERO`. Sem JavaScript, ele leva direto ao WhatsApp; com JavaScript, abre o pré-cadastro.
- Sem número válido, o CONTRATAR sai desabilitado, com o aviso "A contratação ainda não está disponível nesta
  página", e a página não menciona o WhatsApp em lugar nenhum (`tests/test_whatsapp_telas.py`), nem no cadastro do
  lead: o campo de telefone se chama `telefone` e só o script o envia no campo `whatsapp` do serviço.
- Rodapé (de ponta a ponta): e-mail de suporte (`EMAIL_SUPORTE`, padrão `suportenexocolab@gmail.com`), "Fale
  diretamente pelo WhatsApp com a nossa equipe" (só com número), Instagram, o crédito "Desenvolvido por João César
  N. S. Castro" e o CNPJ só com `CNPJ_EMPRESA` válida (vazia: nada no HTML). Links externos em nova aba, com
  `rel="noopener noreferrer"`. Os links dos documentos legais entram no marcador `{# LINKS_LEGAIS #}`.
- Número: sem a variável `WHATSAPP_EMPRESA_NUMERO` no ambiente, vale o oficial (`5561999055282`); presente e vazia,
  os botões somem (é o que os testes usam).

## 3. O pré-cadastro (diálogo do CONTRATAR)

Um `<dialog id="precadastro">` desenhado só com número válido e controlado por `static/js/landing-precadastro.js`,
que usa as peças de `static/js/landing-captacao.js` (`window.NexoCaptacao`). Os dois ficam fora do `landing.js` de
propósito: uma falha neles não derruba o menu, os depoimentos nem os gráficos. Sem o `landing-captacao.js`, o
CONTRATAR continua levando direto ao WhatsApp. O diálogo fecha por Esc, pelo botão de fechar ou com um clique fora,
e devolve o foco ao CONTRATAR; durante um envio, ele não fecha.

- **Etapa 1, aceite dos termos.** Ao abrir, o script busca `/documentos-legais/vigentes` (sem cache) e mostra cada
  texto (por `textContent`, com as quebras de linha) num contêiner com rolagem, com o rótulo e a data da versão.
  - As caixas "Li e aceito os Termos de Uso." e "Li e estou ciente da Política de Privacidade e das condições de
    tratamento dos meus dados." nascem desabilitadas e só liberam quando `scrollTop + clientHeight >= scrollHeight - 2`
    (rolando com o mouse, o dedo ou o teclado: o contêiner recebe o foco) ou na hora, se o texto cabe sem rolagem.
  - O "Continuar" só libera com as duas marcadas. Os ids exibidos vão no envio (`documento_termos` e
    `documento_privacidade`).
  - Sem os dois documentos publicados (lista vazia ou 404), o diálogo avisa que os termos ainda não foram
    publicados; com 503 ou falha de rede, que não foi possível carregá-los (com "Tentar de novo"). Nos dois casos,
    oferece o WhatsApp (com a mensagem de contratação) e o e-mail de suporte, e nada é enviado ao serviço.
- **Etapa 2, qualificação.** Nome do responsável, empresa, WhatsApp, plano de interesse (Bronze, Prata, Ouro ou
  Personalizado / Enterprise) e e-mail, obrigatórios; faixa de faturamento e principal dificuldade, opcionais.
  - Nome e empresa só com espaços não passam; o e-mail segue a mesma regra do serviço.
  - O WhatsApp segue as regras do envio pela Evolution (`whatsapp_service.higienizar_telefone`), mais estritas no que é ambíguo, para o número nunca virar outro:
    - aceita o número colado com o DDI ("+55 11 98765-4321", como o WhatsApp copia), com o 00 internacional, com o zero de tronco ou o "0xx" da longa distância ("(0xx62) 3222-1234") ou com um rótulo antes ("Meu WhatsApp: +55 ..."); fica só DDD e número, na máscara;
    - caracteres invisíveis de formatação não atrapalham (o Contatos do Mac envolve o número copiado em marcas de direção), e os traços tipográficos ("‑", "–", "−") contam como hífen;
    - com "+" ou "00", só passa o DDI 55 seguido de DDD e número: número estrangeiro ("+1 ...") e DDI sem DDD ("+55 99999-8877") não passam;
    - sem "+" ou "00", o 55 do começo não vira DDI: com 10 ou 11 dígitos é o DDD do Rio Grande do Sul ("55 99999-1234" vira "(55) 99999-1234"), e 12 ou 13 dígitos ficam como vieram, inválidos (como "(55) 99276-54321", o DDD 55 com um dígito a mais, que viraria um fixo do DDD 99);
    - exige DDD em uso, fixo de 8 dígitos começando por 2 a 5 ou celular de 9 começando por 9, e recusa o número de exemplo (um só dígito repetido), dois números no mesmo campo e o ramal, com a palavra ou como um grupo final de 1 a 3 dígitos ("3222-1234 21", ou o DDD escrito depois: "98917-1218 (99)"), que viraria outro número. O grupo curto que a própria máscara deixa quando o visitante edita no meio do número não conta;
    - o que não passa fica no campo como foi digitado, e o avanço é barrado com "Informe um WhatsApp do Brasil com DDD: 10 ou 11 números.";
    - com o cursor no meio do número, a máscara espera o campo perder o foco para remontar, sem jogar o cursor para o fim.
  - O mesmo bloco de máscara está em `static/js/landing-captacao.js` (os dois diálogos) e em
    `static/js/avaliacao-publica.js` (Raio-X público); o microsserviço e o servidor do Raio-X público aplicam a
    mesma regra.
- **Etapa 3, três saídas, todas gravam o lead no microsserviço:**
  - "REALIZAR RAIO-X" abre a etapa 4 no próprio diálogo: o painel da Avaliação de Cenário Externo, com o texto de
    apoio, o link "Precisa de ajuda para exportar seus relatórios do sistema? Fale conosco no WhatsApp" e os
    relatórios (.csv e .xlsx); "Enviar para a avaliação" grava o **interesse 1** com os arquivos.
  - "PRECISO DE AJUDA COM OS RELATÓRIOS" grava o **interesse 2**; "FINALIZAR SEM RAIO-X", o **interesse 3**.
- **Etapa 5, resultado.** O protocolo, a situação de cada relatório (com o motivo da recusa) e o botão "Abrir o
  WhatsApp", com a mensagem abaixo mais a linha do protocolo. Depois de um envio, ninguém abre o WhatsApp sozinho
  (o navegador bloquearia a janela): o visitante clica.
- **Falha no envio.** A etapa mostra o motivo e ainda oferece o WhatsApp com a mensagem (sem protocolo): o contato
  não se perde. O 400 de um campo volta à Etapa 2 com o campo marcado; o 409 volta à Etapa 1 (seção 5).

Formato da mensagem (o do pedido do dono, mais o protocolo):

```
*NOVO CONTATO COMERCIAL | NEXO*

Nome: …
Empresa: …
WhatsApp: …
Plano de interesse: …
E-mail: …
Faixa de faturamento: …    (se preenchida)
Principal necessidade: …   (se preenchida)

Situação do Raio-X: Relatórios enviados pelo site | Não realizado | Não solicitado
                    | Envio dos relatórios pelo site não concluído
Solicitou auxílio: Sim, cliente solicitou auxílio com relatórios | Não
Protocolo: XXXXXXXX        (só quando o lead foi gravado)
Origem: Landing Page da NEXO
```

O número do WhatsApp nunca é fixo no script: vem só do atributo `data-numero`, desenhado pelo servidor a partir de `WHATSAPP_EMPRESA_NUMERO`. A mensagem com dados do visitante é uma exceção registrada à regra dos textos pré-preenchidos (`Docs/WHATSAPP_EVOLUTION.md`).

## 4. O cadastro do lead frio (diálogo do "Cadastre-se")

Um `<dialog id="cadastro-lead">`, desenhado com ou sem o número de WhatsApp e controlado pelo fim de
`static/js/landing-captacao.js`. Mesmas regras de abrir, fechar e foco do pré-cadastro.

1. **Termos**: a mesma Etapa 1 do pré-cadastro (mesma marcação, macro `aceite_dos_termos` do `landing.html`).
2. **Seus dados**: nome, nome da empresa, e-mail e telefone da empresa, todos obrigatórios, com a máscara e a regra
   do telefone do pré-cadastro (a mensagem diz "telefone", e não "WhatsApp"). Com número, a dica sugere usar o
   número com WhatsApp. O `name` do campo é `telefone`; o script envia o valor no campo `whatsapp` do serviço.
3. **Avaliação de Cenário Externo (opcional)**: o mesmo painel de relatórios do pré-cadastro (macro
   `painel_dos_relatorios`), com o link de ajuda pelo WhatsApp só com número. "Enviar para a avaliação" grava o
   interesse 1 (exige ao menos um relatório); "Agora não, só quero contato", o interesse 3.
4. **Resultado**: o protocolo, a situação de cada relatório com o motivo e os próximos passos.

Se o envio falhar, o aviso dá o motivo e oferece o WhatsApp (com os dados digitados) ou, sem número, o e-mail de
suporte. Fechado com o resultado na tela, o próximo cadastro começa do zero.

## 5. Envio ao microsserviço

- **Endereço**: `MICROSSERVICO_RAIOX_URL` (padrão `https://nexo-externo.onrender.com`; só `https://host[:porta]`,
  sem caminho; um valor inválido desliga o envio) + `/api/leads/raio-x`. A URL vai ao HTML no atributo
  `data-servico` dos diálogos; a origem entra no `connect-src` da CSP **só da landing** (endpoint `index`,
  `seguranca_http.py`), conferida de novo antes de ir ao cabeçalho. O `form-action` continua `'self'`.
- **Pedido**: `fetch` POST com `FormData` (o navegador põe o `multipart/form-data` e o boundary), `mode: 'cors'`,
  sem credenciais, sem seguir redirecionamento e com prazo de 90 s (`AbortController`). Campos: `nome`, `empresa`,
  `email`, `whatsapp`, `interesse` (1, 2 ou 3), `termos_aceitos=true`, `documento_termos`, `documento_privacidade`,
  `campo_isca` (campo oculto que fica vazio) e, no interesse 1, `arquivos` (um por relatório).
- **Conferido no navegador antes de enviar** (o serviço conta toda tentativa no limite de 5 envios por hora por
  IP): nome e empresa numa linha, até 120 caracteres; e-mail com a forma que o serviço aceita; telefone do Brasil;
  os termos lidos; e, no interesse 1, só `.csv` e `.xlsx` (o `.xlsm` é recusado com o motivo), nenhum arquivo
  vazio, até 12 arquivos e 15 MB somados. Os arquivos recusados aparecem na lista com o motivo e precisam sair
  antes do envio; os escolhidos em vezes diferentes se somam.
- **Enquanto envia**: os botões ficam desabilitados (um envio só), o diálogo não fecha, `aria-busy` marca a etapa e
  o aviso diz "Enviando… A conferência dos relatórios pode levar até 1 minuto." (sem relatórios, "Enviando os seus
  dados…").
- **Respostas**:

| Resposta | Na tela |
| :--- | :--- |
| 201 | Resultado: protocolo e situação de cada relatório (nome e motivo por `textContent`). |
| 400 com campo `nome`, `empresa`, `email` ou `whatsapp` | Volta aos dados, com o campo marcado e a mensagem do serviço. |
| 400 com campo `termos_aceitos` e 409 | Volta aos termos: busca de novo os documentos vigentes e pede a leitura e o aceite outra vez. |
| 400 de `arquivos` ou sem campo, 413 e 415 | O motivo, na etapa do envio. |
| 429 | O motivo e, com `Retry-After`, quando tentar de novo ("em cerca de 2 minutos"). |
| 503, 500 e outros | Serviço indisponível (a mensagem do serviço, quando vem), com o WhatsApp ou o e-mail. |
| Erro de rede ou de CORS | "Não foi possível falar com o serviço de cadastro agora", com o WhatsApp ou o e-mail. |
| Prazo de 90 s | O envio pode ter chegado: o visitante é orientado a falar com a equipe antes de tentar de novo. |

- **Despertar**: ao abrir um dos diálogos, um `GET /ping` sem CORS (resposta opaca, uma vez por página) adianta a
  subida do serviço no Render, que dorme sem uso.
- **Para funcionar em produção** (pendências fora da landing): o serviço precisa liberar a origem da landing em
  `CORS_ORIGENS` e aceitar `documento_termos` e `documento_privacidade` (contrato da junção); sem a origem liberada,
  o navegador recusa a resposta e a landing mostra "serviço indisponível" com o WhatsApp.

## 6. Raio-X público (`/avaliacao-inicial`)

Código em `blueprints/avaliacao_publica.py`, telas em `templates/avaliacao_publica/` e script em `static/js/avaliacao-publica.js`.

Desde 16/11/2026 a landing não leva mais a esta página: o "REALIZAR RAIO-X" do pré-cadastro envia os relatórios ao
microsserviço no próprio diálogo. O preenchimento do formulário pelo `sessionStorage` (`nexo.precadastro`), que o
pré-cadastro gravava, ficou sem origem (pendência: remover ou religar quando a página for ligada).

| Rota | O que faz |
| :--- | :--- |
| `GET /avaliacao-inicial` | Formulário: contato, plano de interesse, relatórios e autorização. Lê o `sessionStorage` (`nexo.precadastro`) se houver, só na aba, e o apaga depois da leitura. |
| `POST /avaliacao-inicial` | Lê os relatórios no processo isolado e grava o lead. |
| `GET /avaliacao-inicial/resultado` | A vitrine do Raio-X do lead desta sessão. |
| `POST /avaliacao-inicial/sair` | Esquece o resultado nesta sessão. |

**Interruptor.** Com `AVALIACAO_PUBLICA_ATIVA` desligado (o padrão), as quatro rotas respondem 404. A exceção é o POST sem token CSRF (ou com token inválido), que recebe antes o 400 do CSRF: a proteção é global e roda antes do 404 do blueprint.

**Gravação.** O lead nasce pelo mesmo caminho do consultor (`avaliacao_cenario.processar` e `registrar`), com:
- `origem = 'site'` e `registrado_por` nulo;
- `consentimento_em` (o instante do envio) e `versao_autorizacao` (a versão do texto marcado);
- `plano_interesse`, `faixa_faturamento` e `dificuldade`, o que o visitante informou.

O consultor vê o lead em `/admin/leads` com o selo "Site" e o protocolo (os 8 primeiros caracteres do id), e o detalhe completo, como o de qualquer lead. A lista tem uma busca pelo protocolo, que acha o lead mesmo fora dos 100 mais recentes. Nenhum WhatsApp ou e-mail é disparado: o "Desbloquear Relatório Completo" é um link `wa.me` que o próprio visitante abre. Se o consultor reenviar os relatórios de um lead do site, a autorização que passa a valer é a dele: `consentimento_em` vira o instante da confirmação do consultor, `registrado_por` o consultor, e `versao_autorizacao` fica vazia.

**Proteções.** Um pedido inválido não lê arquivo e não toca nas tabelas de lead. As checagens vêm nesta ordem:
1. CSRF;
2. limite por IP;
3. campo isca;
4. tempo mínimo desde a abertura do formulário. Sem a marca na sessão (por exemplo, uma aba aberta antes de outro envio), a recusa devolve a marca, e o reenvio passa;
5. campos e autorização. O WhatsApp segue a regra da seção 3 e é gravado só com DDD e número;
6. de 1 a 12 arquivos e até 8 MB, pelo tamanho real dos arquivos recebidos, com o mesmo teto por arquivo no processo isolado. O `Content-Length` do cliente não é autoridade (e corpo chunked não o traz). Ele conta também o envelope do formulário, então só recusa acima do teto mais uma folga de 64 KB (`FOLGA_DO_ENVELOPE`);
7. formato pela extensão (CSV, XLSX, XLS ou Planilha XML 2003, a mesma lista do consultor), antes da vaga. Nome de arquivo sem nenhuma letra ou número sem acento antes da extensão (como "продажи.csv") é recusado como formato não suportado. É o comportamento antigo de todos os envios do portal (`Docs/DIAGNOSTICO_MOTOR_ETL_INVENTARIO.md`, ENT-F23), e renomear o arquivo resolve. A correção está no backlog (`Docs/BACKLOG_MELHORIAS.md`, BM-01);
8. esquema do banco;
9. vaga única;
10. teto diário, dentro da vaga: conta os leads do site gravados hoje e, na memória do processo, as tentativas que chegaram ao processo isolado, com sucesso ou não. Um envio que falha no processo isolado também gasta o teto, senão ocuparia a vaga sem limite. As recusas do processo web (formato, Baia de Segurança, arquivo vazio ou repetido) não gastam;
11. processo isolado.

No navegador, o envio confere o WhatsApp antes de sair, como o pré-cadastro. Durante o envio, o botão "REALIZAR RAIO-X" fica desabilitado. Ele volta a funcionar quando a navegação é interrompida (Parar ou Esc, pela Navigation API) ou quando a página volta do cache do navegador. Em navegador sem a Navigation API, um prazo de reserva de 200 s o solta, e cada envio cancela o prazo do anterior.

O prazo não marca o fim do envio. Com `--threads`, o gunicorn usa o worker gthread, que não derruba o pedido aos 180 s do `--timeout`. O servidor fica limitado pelos 60 s do processo isolado, mas o upload de até 8 MB numa conexão muito lenta pode passar de 3 minutos. Por isso o prazo só existe onde não há outro jeito de saber que a navegação parou. Nesses navegadores, quem clicar de novo depois dele interrompe o envio em curso, e o primeiro lead fica gravado sem o visitante ver o resultado.

Os números aprovados pelo dono, fixados por `tests/test_avaliacao_publica.py`:

| Proteção | Padrão | Onde muda |
| :--- | :--- | :--- |
| Envios por IP (chave HMAC, sem o IP no log; IPv6 pela rede /64) | 3 por hora e 5 por dia | Constante `LIMITE_DE_ENVIOS` no código |
| Teto global do site (dia de Brasília) | 20 por dia | `AVALIACAO_PUBLICA_TETO_DIARIO` no `app.config` |
| Arquivos e tamanho somado | 12 arquivos, 8 MB | `AVALIACAO_PUBLICA_MAX_ARQUIVOS` e `AVALIACAO_PUBLICA_MAX_MB` no `app.config` |
| Tempo mínimo entre abrir o formulário e enviar | 5 s | `AVALIACAO_PUBLICA_TEMPO_MINIMO_SEGUNDOS` no `app.config` |
| Validade do resultado na sessão | 8 horas | `AVALIACAO_PUBLICA_RESULTADO_HORAS` no `app.config` |
| Prazo do processo isolado | 60 s (o consultor tem 120 s) | Constante `PRAZO_DO_PROCESSO_SEGUNDOS` (30, que o executor dobra) |
| Vaga ocupada | 503 com Retry-After, sem fila: o envio público nunca espera nem passa na frente do cliente | — |
| Leituras do resultado por IP | 60 por hora | Constante `LIMITE_DE_LEITURAS` no código |

O `app.config` vem só do `config.py` (`from_object`): os valores marcados assim mudam editando o `config.py`, não por variável de ambiente no Render. Só o interruptor `AVALIACAO_PUBLICA_ATIVA` vem do ambiente.

## 7. O resultado do Raio-X público

- Fica preso à sessão assinada: o id do lead e o instante, nunca na URL. Vale até 8 horas e sai com `Cache-Control: no-store` e `noindex`.
- A vitrine é calculada uma única vez, no envio, ainda dentro da vaga, e guardada por lead na memória do processo. O GET do resultado só a desenha, sem banco. Depois de um reinício (inclusive quando o Render free dorme), o primeiro GET refaz a vitrine, um pedido por vez: os outros recebem 503 com Retry-After. A conexão com o banco é devolvida antes da triagem, e a vitrine é guardada de novo.
- O reenvio e a retriagem feitos pela tela do consultor descartam a vitrine guardada daquele lead, junto com o cache do diagnóstico. O expurgo feito pela tela, quando remove algo, descarta todas. Uma geração, como a do cache do diagnóstico, impede que um cálculo em andamento guarde de novo a vitrine de antes do descarte. O próximo GET a refaz com os dados atuais.
- A retriagem pela linha de comando (`flask leads retriar`), o expurgo do `pg_cron` e o `flask leads expurgar-pdv` rodam em outro processo e não descartam a vitrine do processo web. Para um lead do site enviado há menos de 8 horas, faça a retriagem pela tela. Senão, o visitante continua vendo a triagem antiga até a sessão vencer ou o processo reiniciar. O expurgo não muda o que é servido: ele só apaga linhas que passaram da retenção de 30 dias, e a vitrine vale 8 horas.
- As vitrines vencidas saem da memória a cada pedido ao processo, de qualquer tela, sem esperar uma leitura da mesma chave. "Encerrar esta visualização" tira a do visitante na hora.
- A vitrine (`diagnostico_cenario.visao_publica`) é montada por lista branca. Por bloco da triagem (Urgência, Atenção e Eletiva), mostra:
  - quantos produtos há;
  - o produto de maior impacto (o mesmo da primeira linha da tela do consultor), com nome, categoria, motivos e impacto;
  - o faturamento e o recuperável do bloco, só com 3 produtos ou mais.
- Dos outros produtos não sai nenhum campo. A tela desenha até 3 linhas com texto fixo desfocado ("Conteúdo bloqueado para leads", "R$ 0.000,00") e a frase "Mais N produtos neste bloco no relatório completo". Inspecionar o elemento mostra só o texto fixo.
- O potencial recuperável total aparece sempre. Num lead em que um bloco com produto oculto tem menos de 3 produtos (e por isso não mostra o R$), a conta "total menos o R$ dos outros blocos" revela o recuperável desse bloco: o valor exato, quando nenhum produto fica fora dos blocos, ou um teto. **Decisão do dono (15/11/2026): mantido.** São dados dos relatórios do próprio lead, só na sessão dele, sem problema de LGPD, e a barreira visual cumpre o papel de isca.

## 8. Antes de ligar o interruptor do Raio-X público (pendências do dono)

Decisão do dono (15/11/2026): o Raio-X público nasce desligado e só será ligado quando a infraestrutura estiver pronta. Isso inclui o microsserviço de processamento (capacidade, item 4), a LGPD do lead (item 2) e a sanitização pesada de arquivos (item 3). Os itens abaixo são o caminho técnico até lá.

1. **Migração `e7a1c3f5b9d2` no Supabase, com backup, ANTES do deploy deste código.** O deploy do Render não migra sozinho. Sem a migração, `/admin/leads` mostra o aviso de migração pendente no lugar da lista, e o Raio-X público responde "indisponível". Comando, como nas anteriores (`Docs/PLANOS_E_RAIO_X.md`, seção 7): `flask db upgrade e7a1c3f5b9d2`.
2. **Revisão jurídica** do texto da autorização e definição da **LGPD do lead**: base legal, aviso de privacidade, retenção do contato e da dificuldade (texto livre, que fica como o contato, sem prazo; o expurgo de 30 dias leva só os dados de PDV) e atendimento a pedido de exclusão (`Docs/BAIA_DE_SEGURANCA_E_LEADS.md`, seção 5).
3. **Endurecimento da neutralização** (`Docs/BAIA_DE_SEGURANCA_E_LEADS.md`, seção 5): com envio anônimo, uma célula montada de propósito vira forma de ocupar a vaga. A correção já está descrita e precisa entrar antes de abrir a rota.
4. **Capacidade no Render.** O plano free tem 0,1 CPU e uma vaga única de processamento, a mesma do envio mensal e do Raio-X dos clientes. Medir antes de abrir; plano pago ou fila são decisão do dono.
5. **Captcha (opcional).** Exige um provedor com chave e mudança deliberada da CSP (`seguranca_http.py`) e dos testes de inventário de SRI e CSP.
6. **Expurgo agendado** (`pg_cron` ou cron externo): com captação pública, o volume de leads cresce.

Para ligar: `AVALIACAO_PUBLICA_ATIVA=1` no painel do Render, depois dos itens 1 a 4.

## 9. Arquivos e testes

| Arquivo | Papel |
| :--- | :--- |
| `templates/landing.html`, `static/css/landing.css` (seções 18 e 19) | Landing, os dois diálogos e as telas públicas |
| `static/js/landing-captacao.js` | Cliente do microsserviço (termos, relatórios, envio e respostas) e o cadastro do lead frio |
| `static/js/landing-precadastro.js` | Pré-cadastro do CONTRATAR |
| `config.py` (`WHATSAPP_EMPRESA_NUMERO`, `EMAIL_SUPORTE`, `CNPJ_EMPRESA`, `MICROSSERVICO_RAIOX_URL`), `seguranca_http.py` | Contatos, endereço do serviço e a CSP da landing |
| `static/js/avaliacao-publica.js` | Formulário do Raio-X público |
| `blueprints/avaliacao_publica.py`, `templates/avaliacao_publica/` | Rotas e telas do Raio-X público |
| `diagnostico_cenario.py` (`visao_publica`) | Vitrine por lista branca; a checagem de situação e a triagem foram extraídas do `montar_payload`, sem mudar o comportamento dele |
| `models.py`, `migrations/versions/e7a1c3f5b9d2_captacao_publica_do_lead.py` | Colunas novas do lead |
| `blueprints/leads.py`, `templates/admin/lead(s).html` | Esquema exigido, origem e busca pelo protocolo na tela do consultor; o reenvio, a retriagem e o expurgo descartam a vitrine guardada |
| `avaliacao_cenario.py` | `processar` aceita o executor (o do Raio-X público conta o teto do dia); no reenvio do consultor, `versao_autorizacao` fica vazia |
| `extensions.py` (`chave_limite_ip`), `config.py` (`AVALIACAO_PUBLICA_ATIVA`), `app.py` | Infraestrutura |

| Teste | O que cobre |
| :--- | :--- |
| `tests/test_landing_captacao.py` | Configuração (número padrão, e-mail, CNPJ, origem https), topo, hero, benefícios, planos, cadastro do lead, rodapé, marcadores da junção e os scripts |
| `tests/test_landing_captacao_navegador.py` | Regra da rolagem, envio interceptado (201, 400, 409, 429, 503, erro de rede), arquivos conferidos no navegador, estado "enviando" e o pré-cadastro com relatórios |
| `tests/test_whatsapp_telas.py`, `tests/test_landing_commercial.py`, `tests/test_whatsapp_navegador.py` | O CONTRATAR único, os trechos que mudam com o número e o pré-cadastro (inclusive no navegador, em 320 e 1366 px) |
| `tests/test_http_hardening_security.py` | A origem do microsserviço só no `connect-src` da landing, conferida de novo |
| `tests/test_avaliacao_publica.py` | Rotas, gravação, vitrine sem vazamento (com o diagnóstico completo do consultor como gabarito), sessão, recusas, tetos, limite por IP, migração e o caminho no navegador |
| `tests/test_isolamento_rotas.py` | A exceção deliberada: a varredura roda com o Raio-X ligado e as rotas públicas ficam fixas, sem parâmetro |
| `tests/test_leads_avaliacao.py` (SQLite) e `tests/test_leads_postgresql.py` (PostgreSQL real, opcional) | Colunas, as 5 CHECKs no SQLite (no PostgreSQL, origem, plano e faixa; os tamanhos já são limitados pelo VARCHAR), os códigos válidos (inclusive CUSTOM) e o downgrade |
