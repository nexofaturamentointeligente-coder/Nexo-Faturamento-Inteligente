# Pré-cadastro da landing e Raio-X público

Decisões do dono em 15/11/2026, implementadas na mesma data. Este documento descreve o que mudou na
landing, o pré-cadastro, o Raio-X público e o que falta decidir antes de ligá-lo.

## 1. Decisões do dono

| Tema | Decisão |
| :--- | :--- |
| Nome | NEXO ("Next" foi erro de digitação no pedido). |
| Botões dos planos | Um CONTRATAR único abaixo da grade, no lugar dos três "Contrate Agora". Os cards só apresentam os planos. |
| Etapa 1 (termos) | HTML, CSS e lógica prontos, mas a etapa fica oculta (classe `hidden`) e o modal começa na Etapa 2. Sem tabela nem rota de termos nesta entrega. |
| Dados na mensagem | A mensagem do WhatsApp leva os dados que o visitante digitou; nada é gravado no site nas Etapas 1 a 3. |
| Raio-X | Uma página pública de autocadastro: o visitante envia os relatórios, vê 1 produto real por bloco e os demais desfocados, e desbloqueia o relatório completo pelo WhatsApp. O relatório completo fica no banco, para o consultor. |
| Ativação | Construído **desligado**, atrás de `AVALIACAO_PUBLICA_ATIVA`. |
| Banco | Migração aditiva `e7a1c3f5b9d2`; o e-mail continua obrigatório para quem faz o Raio-X. |
| Autorização | "Autorizo o uso destes relatórios nesta avaliação." (o texto da tela do consultor, em 1ª pessoa), com o instante e a versão gravados. Precisa de revisão jurídica antes de ligar a rota. |
| Resultado | Em claro: quantos produtos há em cada bloco, o potencial recuperável total e, só com 3 produtos ou mais no bloco, o faturamento e o recuperável dele. |
| Planos | Os cards continuam "Sob consulta": a imagem dos planos não será usada. |

Depois das revisões, o dono decidiu, em 15/11/2026:
- manter o potencial recuperável total sempre visível, mesmo que ele permita deduzir o R$ de um bloco pequeno (seção 4);
- ligar o Raio-X público só quando a infraestrutura estiver pronta (seção 5);
- deixar no backlog a recusa de nome de arquivo só com caracteres não latinos (`Docs/BACKLOG_MELHORIAS.md`, BM-01).

## 2. Landing: o CONTRATAR

- `templates/landing.html`, seção `#planos`: os cards BRONZE, PRATA e OURO ficam como estavam, sem botão.
- Abaixo da grade, um único CONTRATAR, desenhado pela macro `link_whatsapp` (sem alteração), com a mensagem `whatsapp_textos.contratar` e o número de `WHATSAPP_EMPRESA_NUMERO`.
- Sem JavaScript, ele leva direto ao WhatsApp; com JavaScript, abre o pré-cadastro.
- Sem número válido, sai desabilitado, com o aviso de sempre ("A contratação ainda não está disponível nesta página"), e a página não menciona o WhatsApp em lugar nenhum (`tests/test_whatsapp_telas.py`).

## 3. O pré-cadastro (diálogo da landing)

Um `<dialog>` nativo dentro da seção de planos, desenhado só com número válido, e controlado por `static/js/landing-precadastro.js`. Ele fica num arquivo separado do `landing.js` de propósito: uma falha no pré-cadastro não derruba o menu, os depoimentos nem os gráficos. O diálogo fecha por Esc, pelo botão de fechar ou com um clique fora, e devolve o foco ao CONTRATAR.

- **Etapa 1, aceite dos termos (oculta).** As duas caixas e o texto do dono estão no HTML, com a classe `hidden`. Os links ficam sem `href` de propósito: `/termos` e `/privacidade` não existem. Um destino local do diálogo que não responde reprova `tests/test_avaliacao_publica.py`, e um link com "termos" ou "privacidade" reprova `tests/test_whatsapp_telas.py`. Para reativar:
  1. publicar os textos revisados (Admin › Configurações › Documentos Legais) e criar as páginas públicas que os mostram;
  2. pôr esses endereços no `href` dos dois links;
  3. trocar `ETAPA_TERMOS_ATIVA` para `true` no script, que então tira a classe `hidden` e abre o modal por esta etapa;
  4. decidir como registrar o aceite no servidor (hoje a caixa marcada no navegador não fica gravada) e atualizar os testes dos links legais e do aviso do rodapé.
- **Etapa 2, qualificação.** Nome do responsável, empresa, WhatsApp e plano de interesse (Bronze, Prata, Ouro ou Custom / Enterprise), obrigatórios; e-mail, faixa de faturamento e principal dificuldade, opcionais.
  - Nada vai ao servidor: o script cancela o envio do formulário e só monta a mensagem.
  - O diálogo só abre com JavaScript. Num navegador sem suporte a `<dialog>`, o CSS o mantém escondido.
  - Nome e empresa só com espaços não passam.
  - O WhatsApp segue as regras do envio pela Evolution (`whatsapp_service.higienizar_telefone`), mais estritas no que é ambíguo, para o número nunca virar outro:
    - aceita o número colado com o DDI ("+55 11 98765-4321", como o WhatsApp copia), com o 00 internacional, com o zero de tronco ou o "0xx" da longa distância ("(0xx62) 3222-1234") ou com um rótulo antes ("Meu WhatsApp: +55 ..."); fica só DDD e número, na máscara;
    - caracteres invisíveis de formatação não atrapalham (o Contatos do Mac envolve o número copiado em marcas de direção), e os traços tipográficos ("‑", "–", "−") contam como hífen;
    - com "+" ou "00", só passa o DDI 55 seguido de DDD e número: número estrangeiro ("+1 ...") e DDI sem DDD ("+55 99999-8877") não passam;
    - sem "+" ou "00", o 55 do começo não vira DDI: com 10 ou 11 dígitos é o DDD do Rio Grande do Sul ("55 99999-1234" vira "(55) 99999-1234"), e 12 ou 13 dígitos ficam como vieram, inválidos (como "(55) 99276-54321", o DDD 55 com um dígito a mais, que viraria um fixo do DDD 99);
    - exige DDD em uso, fixo de 8 dígitos começando por 2 a 5 ou celular de 9 começando por 9, e recusa o número de exemplo (um só dígito repetido), dois números no mesmo campo e o ramal, com a palavra ou como um grupo final de 1 a 3 dígitos ("3222-1234 21", ou o DDD escrito depois: "98917-1218 (99)"), que viraria outro número. O grupo curto que a própria máscara deixa quando o visitante edita no meio do número não conta;
    - o que não passa fica no campo como foi digitado, e o avanço é barrado com "Informe um WhatsApp do Brasil com DDD: 10 ou 11 números.";
    - com o cursor no meio do número, a máscara espera o campo perder o foco para remontar, sem jogar o cursor para o fim.
  - O Raio-X público usa o mesmo bloco de script, e o servidor dele aplica a mesma regra. A única diferença é o rótulo antes do número: o servidor não o aceita, mas o script o tira antes do envio.
- **Etapa 3, Raio-X.** Três saídas:
  - "REALIZAR RAIO-X" leva à página pública (seção 4), só com o interruptor ligado. Desligado, o botão some.
  - "PRECISO DE AJUDA COM OS RELATÓRIOS" e "FINALIZAR SEM RAIO-X" abrem o WhatsApp da equipe em nova aba, com a mensagem abaixo.

Formato da mensagem (o do pedido do dono):

```
*NOVO CONTATO COMERCIAL | NEXO*

Nome: …
Empresa: …
WhatsApp: …
Plano de interesse: …
E-mail: …                  (se preenchido)
Faixa de faturamento: …    (se preenchida)
Principal necessidade: …   (se preenchida)

Situação do Raio-X: Não realizado | Não solicitado | Realizado (protocolo XXXXXXXX)
Solicitou auxílio: Sim, cliente solicitou auxílio com relatórios | Não
Origem: Landing Page da NEXO
```

O número do WhatsApp nunca é fixo no script: vem só do atributo `data-numero`, desenhado pelo servidor a partir de `WHATSAPP_EMPRESA_NUMERO`. A mensagem com dados do visitante é uma exceção registrada à regra dos textos pré-preenchidos (`Docs/WHATSAPP_EVOLUTION.md`).

## 4. Raio-X público (`/avaliacao-inicial`)

Código em `blueprints/avaliacao_publica.py`, telas em `templates/avaliacao_publica/` e script em `static/js/avaliacao-publica.js`.

| Rota | O que faz |
| :--- | :--- |
| `GET /avaliacao-inicial` | Formulário: contato, plano de interesse, relatórios e autorização. Vem preenchido com o que o visitante digitou no pré-cadastro (sessionStorage, só na aba, apagado depois da leitura). |
| `POST /avaliacao-inicial` | Lê os relatórios no processo isolado e grava o lead. |
| `GET /avaliacao-inicial/resultado` | A vitrine do Raio-X do lead desta sessão. |
| `POST /avaliacao-inicial/sair` | Esquece o resultado nesta sessão. |

**Interruptor.** Com `AVALIACAO_PUBLICA_ATIVA` desligado (o padrão), as quatro rotas respondem 404 e o botão da Etapa 3 some. A exceção é o POST sem token CSRF (ou com token inválido), que recebe antes o 400 do CSRF: a proteção é global e roda antes do 404 do blueprint.

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

**Resultado.**
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

## 5. Antes de ligar o interruptor (pendências do dono)

Decisão do dono (15/11/2026): o Raio-X público nasce desligado e só será ligado quando a infraestrutura estiver pronta. Isso inclui o microsserviço de processamento (capacidade, item 4), a LGPD do lead (item 2) e a sanitização pesada de arquivos (item 3). Os itens abaixo são o caminho técnico até lá.

1. **Migração `e7a1c3f5b9d2` no Supabase, com backup, ANTES do deploy deste código.** O deploy do Render não migra sozinho. Sem a migração, `/admin/leads` mostra o aviso de migração pendente no lugar da lista, e o Raio-X público responde "indisponível". Comando, como nas anteriores (`Docs/PLANOS_E_RAIO_X.md`, seção 7): `flask db upgrade e7a1c3f5b9d2`.
2. **Revisão jurídica** do texto da autorização e definição da **LGPD do lead**: base legal, aviso de privacidade, retenção do contato e da dificuldade (texto livre, que fica como o contato, sem prazo; o expurgo de 30 dias leva só os dados de PDV) e atendimento a pedido de exclusão (`Docs/BAIA_DE_SEGURANCA_E_LEADS.md`, seção 5).
3. **Endurecimento da neutralização** (`Docs/BAIA_DE_SEGURANCA_E_LEADS.md`, seção 5): com envio anônimo, uma célula montada de propósito vira forma de ocupar a vaga. A correção já está descrita e precisa entrar antes de abrir a rota.
4. **Capacidade no Render.** O plano free tem 0,1 CPU e uma vaga única de processamento, a mesma do envio mensal e do Raio-X dos clientes. Medir antes de abrir; plano pago ou fila são decisão do dono.
5. **Captcha (opcional).** Exige um provedor com chave e mudança deliberada da CSP (`seguranca_http.py`) e dos testes de inventário de SRI e CSP.
6. **Expurgo agendado** (`pg_cron` ou cron externo): com captação pública, o volume de leads cresce.

Para ligar: `AVALIACAO_PUBLICA_ATIVA=1` no painel do Render, depois dos itens 1 a 4.

## 6. Arquivos e testes

| Arquivo | Papel |
| :--- | :--- |
| `templates/landing.html`, `static/css/landing.css` (seções 18 e 19) | CONTRATAR, diálogo e telas públicas |
| `static/js/landing-precadastro.js` | Pré-cadastro |
| `static/js/avaliacao-publica.js` | Formulário do Raio-X público |
| `blueprints/avaliacao_publica.py`, `templates/avaliacao_publica/` | Rotas e telas do Raio-X público |
| `diagnostico_cenario.py` (`visao_publica`) | Vitrine por lista branca; a checagem de situação e a triagem foram extraídas do `montar_payload`, sem mudar o comportamento dele |
| `models.py`, `migrations/versions/e7a1c3f5b9d2_captacao_publica_do_lead.py` | Colunas novas do lead |
| `blueprints/leads.py`, `templates/admin/lead(s).html` | Esquema exigido, origem e busca pelo protocolo na tela do consultor; o reenvio, a retriagem e o expurgo descartam a vitrine guardada |
| `avaliacao_cenario.py` | `processar` aceita o executor (o do Raio-X público conta o teto do dia); no reenvio do consultor, `versao_autorizacao` fica vazia |
| `extensions.py` (`chave_limite_ip`), `config.py` (`AVALIACAO_PUBLICA_ATIVA`), `app.py` | Infraestrutura |

| Teste | O que cobre |
| :--- | :--- |
| `tests/test_avaliacao_publica.py` | Rotas, gravação, vitrine sem vazamento (com o diagnóstico completo do consultor como gabarito), sessão, recusas, tetos, limite por IP, migração e o caminho completo no navegador |
| `tests/test_whatsapp_telas.py`, `tests/test_landing_commercial.py`, `tests/test_whatsapp_navegador.py` | O CONTRATAR único e o pré-cadastro (inclusive no navegador, em 320 e 1366 px) |
| `tests/test_isolamento_rotas.py` | A exceção deliberada: a varredura roda com o Raio-X ligado e as rotas públicas ficam fixas, sem parâmetro |
| `tests/test_leads_avaliacao.py` (SQLite) e `tests/test_leads_postgresql.py` (PostgreSQL real, opcional) | Colunas, as 5 CHECKs no SQLite (no PostgreSQL, origem, plano e faixa; os tamanhos já são limitados pelo VARCHAR), os códigos válidos (inclusive CUSTOM) e o downgrade |
