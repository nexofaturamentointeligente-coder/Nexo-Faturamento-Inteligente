# WhatsApp (Evolution API) e despertador do Render

Comunicação do NEXO pelo WhatsApp em duas camadas:
- a **ativa**, com mensagens automáticas ao cliente pela Evolution API (`whatsapp_service.py`);
- a **passiva**, com botões `wa.me` nas telas, que abrem uma conversa com o número comercial da NEXO.

Junto vem a rota `/ping`, que mantém o serviço do Render Free acordado.

A validação do cadastro inicial e a recuperação de senha com token continuam **exclusivamente por e-mail**. Nada aqui as toca.

## 1. Configuração

| Variável | Onde | Para quê |
| :--- | :--- | :--- |
| `EVOLUTION_API_URL` | `.env` local e painel do Render | Endereço da Evolution API, em `https`. `http` só é aceito em `localhost`. |
| `EVOLUTION_API_KEY` | idem (segredo) | Valor do cabeçalho `apikey`. A chave global funciona, mas o token da própria instância basta para o `sendText` e limita o estrago de um vazamento, porque a global administra o servidor inteiro. Confira na versão da Evolution em uso. |
| `EVOLUTION_INSTANCE_NAME` | idem | Nome da instância conectada ao WhatsApp da NEXO. |
| `WHATSAPP_EMPRESA_NUMERO` | idem | Número comercial com DDI e DDD (ex.: `5562988887766`), usado nos botões `wa.me`. |

- Sem a URL, a chave e a instância, os disparos ficam **desligados**: cada gatilho só registra um `logger.warning` e o fluxo segue.
- O log de inicialização diz se os disparos estão ligados ou o que falta, citando só os nomes das variáveis.
- A URL é recusada (e aparece como pendência no log) quando:
  - não usa TLS fora de `localhost`;
  - traz usuário ou senha, consulta, fragmento, espaço ou porta inválida;
  - aponta para um host de exemplo, como o `sua-api.onrender.com` do `.env.example` ou os reservados `example.com`, `.org` e `.net`. Copiado por engano, esse host mandaria a chave e os dados do cliente para um endereço que qualquer pessoa pode registrar.
- O número comercial passa a ter só dígitos com o 55 (`whatsapp_service.configurar`). Se for vazio ou inválido, os botões não aparecem e as telas mostram os textos de antes. A landing continua obedecendo a decisão COM-001: nenhum contato é publicado sem o número configurado.
- As quatro variáveis estão no `.env.example` e no `render.yaml`, com `sync: false`.

## 2. Mensagens automáticas (gatilhos)

Todo gatilho roda **depois do commit**, só quando há telefone cadastrado, e nunca muda a resposta da rota.

| Evento | Onde | Telefone | Regra | Mensagem |
| :--- | :--- | :--- | :--- | :--- |
| Relatório pronto: avaliação do lead | `blueprints/leads.py` (`avaliar`, `reenviar`; não em `retriar`) | WhatsApp do lead | Só com a caixa "O lead aceitou receber pelo WhatsApp o aviso de que a avaliação ficou pronta" marcada. Ela vem desmarcada, porque a autorização de uso dos relatórios não cobre esse contato. | "Olá, {contato}! A avaliação de {loja} está pronta.⏎⏎A equipe NEXO vai entrar em contato para apresentar os resultados.⏎⏎_Equipe NEXO Faturamento Inteligente_". Sem valor: o total retroativo fica na tela do lead, com o consultor. |
| Relatório pronto: Raio-X de entrada | `blueprints/onboarding.py` (página e API) | `Empresa.telefone_contato` | — | "Olá, {usuário}! O relatório de {empresa} está pronto.⏎⏎Acesse o portal NEXO para ver os detalhes.⏎⏎_Equipe NEXO Faturamento Inteligente_". Sem valor: o `recuperavel.total` fica só na tela do Raio-X. |
| Relatório pronto: análise publicada | `blueprints/admin.py` (`analise_relatorio`, publicar) | `Empresa.telefone_contato` | Só quando o cliente pode abrir a análise, como o e-mail. Republicar avisa de novo, como o e-mail e o sininho. | "Olá, {empresa}! O seu relatório está pronto.⏎⏎Acesse o portal NEXO para ver os detalhes.⏎⏎_Equipe NEXO Faturamento Inteligente_". |
| Abertura do envio do mês | `blueprints/admin.py` (`analise_nova`) | `Empresa.telefone_contato` | Só quando a política deixa o cliente enviar (`pode_enviar_relatorio`); empresa suspensa só recebe para período pago. Só para o mês corrente, o anterior ou um futuro: o lançamento retroativo de histórico, em que o admin sobe os arquivos, não avisa. | "Olá, {empresa}! Você já pode enviar os relatórios de *{mês de ano}*.⏎⏎Anexe o de vendas e o de compras em *Enviar relatórios*, no portal NEXO, para receber a sua análise.⏎⏎_Equipe NEXO Faturamento Inteligente_". Na análise quinzenal: "…da *1ª quinzena de {mês de ano}*…". |
| Chamado atualizado | `blueprints/admin.py` (`ticket_detalhe`): resposta da equipe e mudança real de status, inclusive a reabertura | `Empresa.telefone_contato` | Só com acesso ao portal e com "Alertas de chamados" ligado, como o sininho. | "Olá, {cliente}! O seu chamado #N foi atualizado.⏎⏎Situação atual: *{status}*⏎⏎Acesse o portal NEXO, em *Suporte*, para ver os detalhes e responder.⏎⏎_Equipe NEXO Faturamento Inteligente_". Resolvido: a ação vira "Se ainda precisar de ajuda, responda no próprio chamado, no portal NEXO, em *Suporte*." |

Regras do texto (`whatsapp_service.py`; na tabela, ⏎⏎ marca a linha em branco entre os blocos):
- A primeira linha traz o fato, que é o que a notificação do celular mostra. Depois vêm a situação (só no chamado), a ação com o nome do menu do portal e a assinatura, em itálico.
- Negrito (`*…*`) só em valores do sistema: mês, situação e menu. Nunca em nome digitado (cliente, empresa, loja).
- **Nenhuma mensagem leva valor em reais**, em caso nenhum: cliente, Raio-X ou lead (decisão do dono de 24/11/2026, por segurança e LGPD, porque o telefone de destino não é confirmado). As rotas continuam passando o valor, e `texto_relatorio_pronto` o descarta. A mensagem só diz que ficou pronto e qual é o próximo passo: o portal ou, para quem não tem acesso a ele, o contato da equipe NEXO.
- Nomes e status passam por `texto_seguro` (uma linha, sem caractere de controle, truncados): as quebras de linha vêm só dos blocos fixos. O texto final tem no máximo 1.000 caracteres; o maior caso possível fica abaixo de 300.

Não havia rotina de avisos no sistema. O cliente só envia arquivos para uma análise que o admin abriu, então o "período mensal de envio" de uma empresa abre quando o admin cria a análise do mês. Por isso o aviso de abertura sai nesse momento, e não num disparo em massa: uma mensagem a todas as empresas mandaria enviar arquivos quem ainda não tem análise aberta.

### Botões `wa.me` nas telas (camada passiva)

São desenhados pela macro `link_whatsapp` de `templates/_macros.html`, só com `WHATSAPP_EMPRESA_NUMERO` válido. Sem ele, cada tela sai idêntica à de antes, byte a byte (`tests/test_whatsapp_telas.py`).
- O link abre em nova aba, com `rel="noopener noreferrer"` e sem JavaScript.
- Rótulo do portal: "Falar pelo WhatsApp" (card Contato NEXO, Suporte, chamado e convite de upgrade com a assinatura inativa). Em Planos, "Conversar pelo WhatsApp", ao lado do pedido pelo formulário, porque o WhatsApp só abre a conversa e não leva o plano escolhido.
- A mensagem pré-preenchida nunca leva dado do cliente: nem nome, nem empresa, nem o assunto do chamado, só o número dele.
- **Exceção (decisão do dono, 15/11/2026): o pré-cadastro da landing.** Com JavaScript, o CONTRATAR abre um formulário (`static/js/landing-precadastro.js`), e a mensagem das saídas "Preciso de ajuda com os relatórios" e "Finalizar sem Raio-X" leva os dados que o próprio visitante digitou: nome, empresa, WhatsApp, plano de interesse e, se preenchidos, e-mail, faixa de faturamento e dificuldade. Nada disso vai ao servidor nessas saídas, e o visitante confere a mensagem no WhatsApp antes de enviá-la. O número continua vindo só de `WHATSAPP_EMPRESA_NUMERO`, entregue ao script pelo atributo `data-numero`. A LGPD do lead (logo abaixo) continua pendente. Detalhes em [PRE_CADASTRO_LANDING.md](PRE_CADASTRO_LANDING.md).

| Tela | Contexto da mensagem |
| :--- | :--- |
| Landing: o CONTRATAR único abaixo dos planos (desde 15/11/2026, no lugar dos três "Contrate Agora"), o "Falar com a equipe NEXO" e o rodapé "Contatos" | Contratar um plano (sem JavaScript) ou a mensagem do pré-cadastro; dúvidas comerciais |
| Raio-X público (`/avaliacao-inicial/resultado`, desligado por padrão): "Desbloquear Relatório Completo" | A mensagem do pré-cadastro com "Situação do Raio-X: Realizado" e o protocolo do lead (mesma exceção registrada acima). O autocadastro não tem gatilho ativo: nada é enviado ao lead pela Evolution. |
| Como aderir, que faz as vezes da tela de cadastro (não há autocadastro) | Contratar um plano; dúvidas comerciais |
| Planos e upgrade do cliente | Mudar de plano (ao lado do pedido por chamado, que continua); dúvidas comerciais; atendimento |
| Painel do cliente: card Contato NEXO e faixa de renovação | Atendimento; renovar o contrato |
| Suporte: lista e detalhe do chamado | Atendimento; ajuda com o chamado #N |

- **Login.** Fica como estava. O cartão precisa caber em 375×667 sem rolagem, e o "É novo por aqui?" já leva ao Como aderir, que tem os botões.
- **Fora dos botões.** Recuperação de senha, primeiro acesso e e-mails não citam o WhatsApp.
- **NexoBot.** As respostas sobre o contato com a equipe citam o botão "Falar pelo WhatsApp" do card Contato NEXO, da página Suporte e do Como obter acesso, sem número nem URL. As respostas sobre avisos e Configurações dizem, sempre de forma condicional ("quando a equipe NEXO liga os avisos por WhatsApp"), que o telefone de contato da empresa recebe as mensagens automáticas.

## 3. Fail-safe e privacidade

- **Nunca trava o fluxo.**
  - O POST (`/message/sendText/{instância}`, corpo `{"number", "text"}`) vai para uma fila de fundo do processo: **uma** thread daemon, em ordem de chegada (avisos do mesmo chamado chegam na ordem em que saíram), com no máximo 200 pendentes. Acima disso, o aviso novo é descartado com warning.
  - O encerramento do processo (deploy, reinício, Render adormecendo) não espera a fila: os pendentes se perdem, como um e-mail não enviado.
  - Cada operação de rede tem `timeout` de 5 s. A resolução do nome do host fica fora desse prazo, e por isso a requisição nunca espera a rede.
  - API fora do ar, recusa, redirecionamento, resposta inesperada ou configuração ausente viram `logger.warning`.
  - Nenhum método público levanta exceção.
  - A fila só faz HTTP e não toca no banco.
- **Transporte.** Usa `urllib`, como o e-mail pelo Brevo; não há dependência nova. A chave vai num cabeçalho que não acompanha redirecionamento, e redirecionamento é tratado como falha.
- **Log.** Nunca leva a chave, a URL, o texto da mensagem nem o telefone inteiro: só os 4 últimos dígitos, o status HTTP e o tipo do erro.
- **Telefone.** Na dúvida, não envia: um aviso que se perde é melhor que um aviso no telefone de outra pessoa.
  - Só dígitos e separadores de telefone, com no máximo um `+` no começo. Letra (ramal, "Tel:"), barra ou vírgula tornam o número ambíguo, e um grupo final de 1 a 3 dígitos é ramal.
  - Com `+` ou `00`, o DDI tem de ser o 55: número estrangeiro não sai. Sem prefixo, o tamanho decide (o DDD 55 sem DDI continua nacional).
  - O DDD é conferido contra a lista da Anatel. Celular de 9 dígitos que não começa por 9 e assinante com um só dígito repetido (`99999-9999`, o número de exemplo) não saem.
- **Texto.** Nomes e status entram numa linha só: sem quebra, sem caractere de controle e truncados.

## 4. Despertador `/ping` (Render Free)

`GET /ping` (ou `/health`) responde `OK` em texto puro, com status 200 e `Cache-Control: no-store`.
- Não exige login e não tem limite de taxa.
- Não consulta o banco nem com cookie de sessão: os guardas de sessão, de primeiro acesso e de termos pulam esse endpoint.
- Continua respondendo com o banco fora do ar.
- O `healthCheckPath` do Render continua `/healthz`.

**Como ligar:** num monitor externo (cron-job.org, UptimeRobot etc.), configure `GET https://<host público>/ping` a cada **10 a 14 minutos**. O Render Free adormece o serviço depois de 15 minutos sem tráfego de entrada. O host precisa ser um dos confiáveis: outro host recebe 400.

**Custos e limites que o dono precisa pesar:**
- **Horas do plano.** O Render Free dá **750 horas de instância por mês por workspace**. Um serviço acordado o mês inteiro gasta de 720 a 744 horas. Dois serviços Free acordados (por exemplo, o NEXO e uma Evolution API também no Render Free) passam do limite, e o Render **suspende todos os serviços Free** até o mês seguinte.
- **Supabase Free.** O projeto pausa depois de 7 dias sem consultas, e o `/ping` não consulta o banco. Mantenha também o `/healthz/db` (`SELECT 1`) no monitor, por exemplo uma vez por dia.
- **Evolution API no Render Free.** Ela também adormece, e sem disco persistente perde a sessão do WhatsApp a cada reinício (é preciso ler o QR code de novo). Para produção, use uma hospedagem com disco persistente ou um plano pago.

## 5. Pendências do dono

- **LGPD do lead.** Hoje o aviso ao lead depende da confirmação do consultor no formulário. Falta definir a base legal e o aviso de privacidade do contato por WhatsApp ([BAIA_DE_SEGURANCA_E_LEADS.md](BAIA_DE_SEGURANCA_E_LEADS.md)). O texto jurídico não foi inventado aqui.
- **Telefone sem confirmação.**
  - Problema: o `telefone_contato` da empresa é texto livre no cadastro do admin, e qualquer usuário cliente da empresa pode trocá-lo em Configurações. Com um dígito errado ou o número de um ex-funcionário, um terceiro recebe o nome da empresa e a situação do chamado.
  - Feito em 24/11/2026 (decisão do dono): as mensagens não levam mais o valor recuperável nem outro valor em reais.
  - Mitigações que continuam como decisão do dono:
    - confirmar o número com um código enviado pelo WhatsApp;
    - avisar por e-mail quando o telefone mudar.
- **Opt-out.** Só o "Alertas de chamados" vale para o WhatsApp. Relatório pronto e abertura do mês não têm preferência própria, e criar uma pede migration.
- **WhatsApp do cliente.** É o `telefone_contato` da empresa. Não há campo próprio de WhatsApp, e criar um pede migration.
  - Com os disparos ligados, a tela de Configurações diz que esse telefone recebe os avisos do NEXO e alerta quando o número cadastrado não serve para o WhatsApp, pela mesma regra do disparo (`higienizar_telefone`). A dica de "Alertas de chamados do suporte" avisa que a escolha vale também para o WhatsApp dos chamados.
  - O cadastro da empresa no admin traz a mesma informação na dica do campo "Telefone de contato".
- **Protocolo.** A Evolution API com o motor Baileys usa o protocolo do WhatsApp Web, que não é oficial, e há risco de banimento do número em volume alto. A alternativa oficial é a WhatsApp Cloud API da Meta, que a Evolution também suporta.

## 6. Testes

| Módulo | O que cobre |
| :--- | :--- |
| `tests/test_whatsapp_service.py` | Higienização (com os casos que mandariam o aviso a outra pessoa), textos, POST contra um abridor falso e contra um servidor HTTP local de verdade (aceite, recusa, redirecionamento recusado, prazo estourado, porta fechada), a fila (uma thread, em ordem, limite, daemon), configuração e log sem segredo |
| `tests/test_ping.py` | `/ping` e `/health` sem login, sem nenhum SQL (com e sem cookie, no primeiro acesso, com a trava dos termos) e com o banco fora do ar |
| `tests/test_whatsapp_gatilhos.py` | Os gatilhos pelas rotas, com o transporte trocado: o que dispara, o que não dispara, a ordem em relação ao commit e à vaga de upload, e as falhas que não mudam a rota |
| `tests/test_whatsapp_telas.py` e `tests/test_whatsapp_navegador.py` | Os botões `wa.me` com e sem número configurado |
