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
| Relatório pronto: avaliação do lead | `blueprints/leads.py` (`avaliar`, `reenviar`; não em `retriar`) | WhatsApp do lead | Só com a caixa "O lead aceitou receber pelo WhatsApp o aviso de que a avaliação ficou pronta" marcada. Ela vem desmarcada, porque a autorização de uso dos relatórios não cobre esse contato. | "Olá, {contato}! O relatório de {loja} está pronto… Encontramos R$ X em potencial recuperável. A equipe NEXO vai entrar em contato para apresentar os resultados." O valor é o do KPI da tela do lead, o total retroativo. |
| Relatório pronto: Raio-X de entrada | `blueprints/onboarding.py` (página e API) | `Empresa.telefone_contato` | — | "Olá, {usuário}! O relatório de {empresa} está pronto… Encontramos R$ X… Acesse o portal…". O valor é o `recuperavel.total` da tela, que o plano não esconde. |
| Relatório pronto: análise publicada | `blueprints/admin.py` (`analise_relatorio`, publicar) | `Empresa.telefone_contato` | Só quando o cliente pode abrir a análise, como o e-mail. Republicar avisa de novo, como o e-mail e o sininho. | "Olá, {empresa}! O seu relatório está pronto… Acesse o portal…". Sai sem valor, porque a análise mensal não calcula potencial recuperável. |
| Abertura do envio do mês | `blueprints/admin.py` (`analise_nova`) | `Empresa.telefone_contato` | Só quando a política deixa o cliente enviar (`pode_enviar_relatorio`); empresa suspensa só recebe para período pago. Só para o mês corrente, o anterior ou um futuro: o lançamento retroativo de histórico, em que o admin sobe os arquivos, não avisa. | "Olá, {empresa}! O envio dos relatórios de {mês de ano} está aberto…". Na análise quinzenal: "…da 1ª quinzena de {mês de ano}…". |
| Chamado atualizado | `blueprints/admin.py` (`ticket_detalhe`): resposta da equipe e mudança real de status, inclusive a reabertura | `Empresa.telefone_contato` | Só com acesso ao portal e com "Alertas de chamados" ligado, como o sininho. | "Olá, {cliente}! O seu chamado #N… foi atualizado. Situação atual: {status}…" |

Não havia rotina de avisos no sistema. O cliente só envia arquivos para uma análise que o admin abriu, então o "período mensal de envio" de uma empresa abre quando o admin cria a análise do mês. Por isso o aviso de abertura sai nesse momento, e não num disparo em massa: uma mensagem a todas as empresas mandaria enviar arquivos quem ainda não tem análise aberta.

### Botões `wa.me` nas telas (camada passiva)

São desenhados pela macro `link_whatsapp` de `templates/_macros.html`, só com `WHATSAPP_EMPRESA_NUMERO` válido. Sem ele, cada tela sai idêntica à de antes, byte a byte (`tests/test_whatsapp_telas.py`).
- O link abre em nova aba, com `rel="noopener noreferrer"` e sem JavaScript.
- A mensagem pré-preenchida nunca leva dado do cliente: nem nome, nem empresa, nem o assunto do chamado, só o número dele.

| Tela | Contexto da mensagem |
| :--- | :--- |
| Landing: os três "Contrate Agora" (deixam de ser botões desabilitados), o "Falar com a equipe NEXO" e o rodapé "Contatos" | Contratar o plano BRONZE, PRATA ou OURO; dúvidas comerciais |
| Como aderir, que faz as vezes da tela de cadastro (não há autocadastro) | Contratar um plano; dúvidas comerciais |
| Planos e upgrade do cliente | Mudar de plano (ao lado do pedido por chamado, que continua); dúvidas comerciais; atendimento |
| Painel do cliente: card Contato NEXO e faixa de renovação | Atendimento; renovar o contrato |
| Suporte: lista e detalhe do chamado | Atendimento; ajuda com o chamado #N |

- **Login.** Fica como estava. O cartão precisa caber em 375×667 sem rolagem, e o "É novo por aqui?" já leva ao Como aderir, que tem os botões.
- **Fora dos botões.** Recuperação de senha, primeiro acesso e e-mails não citam o WhatsApp.
- **NexoBot.** As respostas sobre o contato com a equipe citam o botão "quando a equipe publica o WhatsApp comercial", sem número nem URL.

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
  - Problema: o `telefone_contato` da empresa é texto livre no cadastro do admin, e qualquer usuário cliente da empresa pode trocá-lo em Configurações. Com um dígito errado ou o número de um ex-funcionário, um terceiro recebe o nome da empresa, a situação do chamado ou o valor recuperável do Raio-X.
  - Mitigações possíveis, cada uma é decisão do dono:
    - confirmar o número com um código enviado pelo WhatsApp;
    - avisar por e-mail quando o telefone mudar;
    - tirar o valor das mensagens.
- **Opt-out.** Só o "Alertas de chamados" vale para o WhatsApp. Relatório pronto e abertura do mês não têm preferência própria, e criar uma pede migration.
- **WhatsApp do cliente.** É o `telefone_contato` da empresa. Não há campo próprio de WhatsApp, e criar um pede migration.
- **Protocolo.** A Evolution API com o motor Baileys usa o protocolo do WhatsApp Web, que não é oficial, e há risco de banimento do número em volume alto. A alternativa oficial é a WhatsApp Cloud API da Meta, que a Evolution também suporta.

## 6. Testes

| Módulo | O que cobre |
| :--- | :--- |
| `tests/test_whatsapp_service.py` | Higienização (com os casos que mandariam o aviso a outra pessoa), textos, POST contra um abridor falso e contra um servidor HTTP local de verdade (aceite, recusa, redirecionamento recusado, prazo estourado, porta fechada), a fila (uma thread, em ordem, limite, daemon), configuração e log sem segredo |
| `tests/test_ping.py` | `/ping` e `/health` sem login, sem nenhum SQL (com e sem cookie, no primeiro acesso, com a trava dos termos) e com o banco fora do ar |
| `tests/test_whatsapp_gatilhos.py` | Os gatilhos pelas rotas, com o transporte trocado: o que dispara, o que não dispara, a ordem em relação ao commit e à vaga de upload, e as falhas que não mudam a rota |
| `tests/test_whatsapp_telas.py` e `tests/test_whatsapp_navegador.py` | Os botões `wa.me` com e sem número configurado |
