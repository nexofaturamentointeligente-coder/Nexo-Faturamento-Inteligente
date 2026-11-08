# E-mails transacionais do NEXO — funcionamento, configuração e diagnóstico

Guia operacional dos e-mails de **ativação de conta (boas-vindas)**, **recuperação
de senha**, **aviso de senha alterada**, **análise publicada** e **chamado
resolvido**. Código: `emails.py` (envio), `contas_email.py` (busca de contas por
e-mail), `blueprints/auth.py` e `blueprints/admin.py` (gatilhos), templates em
`templates/emails/`.

## 1. Como o NEXO envia e-mails

1. Um evento de negócio é **confirmado no banco** (cadastro, publicação, chamado
   resolvido, troca de senha). O e-mail só é solicitado depois do `COMMIT`.
2. O gatilho monta o link sensível a partir da **origem pública canônica**
   (`NEXO_PUBLIC_URL` ou, sem ela, `RENDER_EXTERNAL_URL`) — nunca do cabeçalho
   `Host` da requisição (`origem_publica.py`).
3. `emails.enviar_email` renderiza o template, cria a versão em texto simples e
   entrega a mensagem ao transporte:
   - **API HTTPS do Brevo** (porta 443) quando `BREVO_API_KEY` está definida;
   - **SMTP do Flask-Mail** quando há apenas `MAIL_USERNAME`/`MAIL_PASSWORD`
     (por exemplo, o Gmail fora do Render Free: seção 10);
   - **nenhum** (envio desativado) quando não há credenciais.

   Com a chave do Brevo **e** credenciais SMTP, vale o Brevo e `MAIL_PASSWORD`
   fica sem uso; com `MAIL_DEFAULT_SENDER` vazio, porém, `MAIL_USERNAME`
   continua sendo o **remetente** — defina `MAIL_DEFAULT_SENDER` antes de
   removê-la (o Diagnóstico de e-mail avisa).
4. Cada solicitação devolve um `EnvioEmail` com um **estado operacional**:

| Estado | Significado | O que NÃO significa |
| --- | --- | --- |
| `DESATIVADO` | Não há transporte configurado; nada saiu. | — |
| `SEM_DESTINATARIO` | Não há destinatário elegível (ex.: empresa cancelada). | — |
| `AGENDADO` | Thread de envio iniciada; resultado ainda desconhecido. | Não significa enviado. |
| `ACEITO_PELO_PROVEDOR` | A API do Brevo (HTTP 201) ou o servidor SMTP aceitou a mensagem. | Não comprova chegada à caixa de entrada. |
| `FALHA_NO_ENVIO` | Falhou no preparo, no agendamento ou foi recusada pelo provedor; vem com uma **categoria**. | — |

5. **Modos de envio**
   - *Assíncrono* (thread): recuperação de senha, aviso de senha alterada,
     análise publicada e chamado resolvido. Na recuperação, isso mantém a
     resposta pública genérica e com tempo independente do provedor (proteção
     contra enumeração de contas).
   - *Síncrono*, com prazo: cadastro de empresa, reenvio de ativação e envio de
     teste (ações do administrador), para mostrar na hora se o provedor aceitou.
6. **Confiabilidade**: cada tentativa tem prazo (`NEXO_EMAIL_TIMEOUT`, padrão
   15 s, também aplicado ao SMTP, que o Flask-Mail abria sem prazo). Uma única
   nova tentativa é feita quando o pedido comprovadamente não foi processado
   (HTTP 429 com espera curta — até 5 s, lida do cabeçalho
   `x-sib-ratelimit-reset` que o Brevo documenta, ou do `Retry-After` —,
   500/503 ou falha de conexão antes do envio, inclusive prazo esgotado na
   conexão: `conexao_sem_resposta`). Prazo
   esgotado na resposta, conexão caída **depois** do envio (na API ou já na
   transação SMTP) e 502/504 de gateway não são repetidos (evita e-mail
   duplicado): ficam como falha de resultado incerto (`tempo_esgotado`,
   `conexao_interrompida`, `gateway_sem_resposta`), e o admin lê "pode não
   ter sido enviado". Outros 5xx (501, 507…) também não são repetidos. A
   chave da API nunca acompanha um redirecionamento, e uma resposta que não
   seja a do envio (201/202) não conta como aceite. No encerramento do
   processo (deploy/reinício), envios em andamento são aguardados por até
   ~20 s; os que não terminarem ficam registrados em log como
   **interrompidos**.
7. Falha de e-mail **nunca desfaz** o cadastro, a publicação, a resolução do
   chamado nem a troca de senha.

### Por que não há fila persistente

Avaliamos uma fila em banco (outbox) e ela **não foi introduzida**: as causas
encontradas eram de configuração/normalização, não de perda de threads; todos
os fluxos têm reenvio pelo usuário ou pelo admin (recuperação de senha, botão
"Reenviar link de ativação"); análise e chamado também avisam pelo sininho
(persistido). Uma outbox exigiria migração de esquema e um processo de
reenvio contínuo, que o Render Free (instância que hiberna) não sustenta bem.
Se o volume crescer ou houver exigência de garantia de entrega, a evolução
natural é uma tabela de envios pendentes com reprocessamento periódico.

## 2. Transporte em produção (Render Free)

O **Render Free bloqueia a saída SMTP nas portas 25, 465 e 587** desde
26/09/2025 ([changelog do Render](https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports)).
Em produção no Render use **somente a API HTTPS do Brevo** (porta 443). Se a
aplicação subir com SMTP no Render, o log de inicialização e o Diagnóstico de
e-mail mostram uma pendência que **bloqueia o envio** — e a recuperação de
senha mostra a todos que o envio está indisponível, em vez de prometer um link.
Instâncias **pagas** do Render liberam as portas 465 e 587 (a 25 continua
bloqueada); como o código não distingue o plano, nelas defina
`NEXO_SMTP_PERMITIDO=1` para o SMTP deixar de ser tratado como bloqueado.

## 3. Variáveis de ambiente

| Variável | Obrigatória no Render | Descrição |
| --- | --- | --- |
| `BREVO_API_KEY` | **Sim** | Chave **de API** do Brevo (começa com `xkeysib-`). A chave **SMTP** (`xsmtpsib-`) não funciona na API e é recusada com HTTP 401. Espaços/quebra de linha nas pontas são ignorados; valor vazio = ausente. |
| `MAIL_DEFAULT_SENDER` | **Sim** | Remetente **verificado** no Brevo, no formato `Nome <email@dominio>`. Vazio cai no padrão do projeto. |
| `NEXO_PUBLIC_URL` | Recomendada | Origem `https://` dos links nos e-mails. Sem ela (ou em branco), vale `RENDER_EXTERNAL_URL` (injetada pelo Render). Produção exige `https`, sem caminho. |
| `BREVO_API_URL` | Não | Padrão `https://api.brevo.com/v3/smtp/email`. Precisa ser `https` (a chave nunca é enviada sem TLS). |
| `NEXO_EMAIL_TIMEOUT` | Não | Prazo, em segundos, de cada tentativa (1 a 60; padrão 15). |
| `NEXO_EMAIL_ALERTA_ADMIN` | Não | `1` (padrão em produção) cria aviso no sininho dos administradores quando o provedor não aceita um e-mail; `0` desliga. No máximo um aviso por categoria a cada 30 minutos. O envio desativado (ou bloqueado pela configuração) também gera aviso, sem dados de conta. |
| `NEXO_SMTP_PERMITIDO` | Não | `1` só em instância **paga** do Render com SMTP: o Diagnóstico deixa de tratar o SMTP no Render como bloqueado. No plano Free, não use (as portas SMTP estão fechadas). |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USE_SSL`, `MAIL_USERNAME`, `MAIL_PASSWORD` | Não (só fora do Render Free) | Alternativa SMTP (Gmail: seção 10). O certificado TLS do servidor SMTP é verificado. Com `MAIL_USE_SSL` ligado, vale o SSL direto e o STARTTLS é ignorado. |

> Uma variável declarada no `render.yaml` com `sync: false` **não** tem valor
> até alguém preenchê-la no painel. Confira em *Render → serviço → Environment*.

## 4. Configuração no Brevo (procedimento manual)

Estes passos dependem do painel do Brevo e não podem ser feitos pelo código.

1. **Chave de API**: *SMTP e API → Chaves de API → Gerar nova chave*. Copie o
   valor inteiro **no momento da criação** (depois o Brevo só mostra a chave
   mascarada) e cole em `BREVO_API_KEY` no Render. Não use a aba *SMTP*.
2. **Remetente**: *Remetentes, domínios e IPs dedicados → Remetentes → Adicionar
   remetente* com o mesmo endereço de `MAIL_DEFAULT_SENDER` e confirme o código
   de 6 dígitos enviado a esse endereço. Remetente não verificado é recusado
   (categoria `remetente_nao_autorizado`).
3. **Domínio (recomendado)**: o remetente atual do projeto é um endereço
   `@gmail.com`. Domínios gratuitos **não podem ser autenticados** no Brevo:
   segundo a central de ajuda do Brevo, ele envia mesmo assim, mas troca o
   endereço do remetente por um endereço de domínio do próprio Brevo (nos
   transacionais, algo como `…@<id>.t-sender-sib.com`), o que o destinatário
   pode não reconhecer e favorece a caixa de spam. Para entregabilidade, use
   um domínio próprio e autentique-o em *Remetentes, domínios e IPs dedicados →
   Domínios* (registros DNS do Brevo: código de verificação, DKIM e DMARC);
   depois troque `MAIL_DEFAULT_SENDER` para um endereço desse domínio. Se o
   Brevo recusar o envio por DMARC, a categoria é `dominio_nao_autenticado`.
4. **IPs autorizados**: com o bloqueio ativo, o Brevo recusa chamadas de API de
   IPs não autorizados **mesmo com a chave válida** (HTTP 401; categoria
   `ip_nao_autorizado`) — ver
   [IP security](https://developers.brevo.com/docs/ip-security). A central de
   ajuda do Brevo descreve uma fase de aprendizado: no primeiro uso da chave os
   IPs de origem são autorizados (faixa /24) e, depois de 30 dias sem IPs
   novos, o bloqueio liga sozinho; autorizar um IP manualmente também liga o
   bloqueio para todos os outros. Os IPs de saída do Render são **faixas
   compartilhadas** da região (*serviço → Connect → Outbound*), mudam de tempos
   em tempos (houve troca entre 27/10 e 01/12/2025) e, em workspaces anteriores
   a 23/01/2022, a região Oregon não tem faixa fixa. Escolha uma opção em
   *Configurações → Segurança → IPs autorizados*:
   - autorizar **todas** as faixas de saída do serviço de uma só vez (aceita
     CIDR) e revisar quando o Render anunciar mudança; ou
   - desativar o bloqueio só para a API (*Blocking unauthorized IP addresses →
     Deactivate for API*). O próprio Brevo orienta dispensar a restrição de IP
     quando o servidor não tem IP fixo; a proteção passa a ser o sigilo da
     chave.
   Se o Brevo enviar ao dono da conta um e-mail sobre IP desconhecido, use o
   link de autorização dele.
5. **Conta**: uma conta em validação ou sem créditos é recusada (categorias
   `conta_em_validacao`, `creditos_insuficientes`, `conta_sem_permissao`).
   Contas novas podem precisar que o suporte do Brevo ative o envio
   transacional. O plano gratuito permite 300 e-mails por dia.

## 5. Como identificar falhas

- **Painel do admin → Dashboard**: não exibe alertas técnicos de
  infraestrutura; o atalho *Diagnóstico de e-mail* fica no topo da página.
- **Admin → Diagnóstico de e-mail** (`/admin/diagnostico-email`): transporte
  ativo, remetente, formato da chave (só pelo prefixo; o valor nunca é exibido),
  endpoint, origem dos links, pendências e os **envios recentes** deste processo
  (estado, categoria, HTTP/código do provedor, correlação). A lista fica em
  memória: reinício do servidor (inclusive a hibernação do plano gratuito) a
  zera — logs e avisos do sininho permanecem.
- **Sininho dos administradores** (produção): aviso persistido quando o provedor
  não aceita um e-mail, com a categoria e a referência (`Ref.`). O aviso é gravado
  pela thread de envio (ou pela ação do admin), nunca pela requisição pública de
  recuperação — assim o tempo de resposta não revela se a conta existe.
- **Mensagens do cadastro de empresa e do reenvio**: dizem, sem jargão técnico,
  se o e-mail foi aceito pelo serviço de e-mail, se o envio está indisponível,
  se o resultado é incerto ou se falhou (endereço do destinatário, limite de
  envios, instabilidade, ajuste de configuração ou "a causa está no
  Diagnóstico"), sempre com a referência. A causa técnica da mesma referência
  aparece no Diagnóstico de e-mail e no log.
- **Recuperação de senha (tela pública)**: quando a configuração impede
  qualquer envio (sem chave, chave SMTP no lugar da de API, URL sem https,
  remetente inválido, SMTP no Render sem `NEXO_SMTP_PERMITIDO`), todos veem
  "No momento não é possível enviar o link…", sem consulta ao banco (não revela
  contas). O pedido entra em *Envios recentes* como suprimido e gera aviso no
  sininho.
- **Logs do Render** (*serviço → Logs*): uma linha por desfecho, por exemplo

```text
E-mail transacional não enviado (falha) | tipo=recuperacao_senha transporte=brevo estado=FALHA_NO_ENVIO categoria=ip_nao_autorizado http=401 codigo=unauthorized erro=RuntimeError tentativas=1 correlacao=3fa9c1d2e4b5 referencia=usuario:12 id_mensagem=- instante=2026-10-31T23:59:59+00:00
```

  Procure por `correlacao=<Ref.>` para ligar a mensagem do admin, o aviso do
  sininho e o log. Na inicialização há um resumo (`Configuração de e-mail |
  transporte=...`) e cada pendência aparece como WARNING. Uma linha
  `estado=AGENDADO` sem linha final da mesma correlação indica envio
  interrompido. Nunca são registrados destinatário, assunto, corpo, link,
  token, senha, chave nem a mensagem de erro do provedor.

| Categoria | Causa provável | Ação |
| --- | --- | --- |
| `transporte_desativado` | `BREVO_API_KEY` ausente/vazia | Preencher no Render e reiniciar o serviço |
| `credencial_rejeitada` | Chave inválida, revogada, mascarada ou chave SMTP | Gerar chave de API (`xkeysib-`) |
| `ip_nao_autorizado` | IP de saída do Render não autorizado no Brevo | Seção 4, passo 4 |
| `remetente_nao_autorizado` / `remetente_invalido` | Remetente não verificado ou mal escrito | Seção 4, passo 2 |
| `dominio_nao_autenticado` | O Brevo exige domínio autenticado (DMARC) | Seção 4, passo 3 |
| `requisicao_rejeitada` | O Brevo recusou outro parâmetro do pedido (HTTP 400) | Conferir o evento no Brevo e o código no Diagnóstico |
| `envio_bloqueado` | A configuração impede qualquer envio (pendência bloqueante) | Corrigir a pendência apontada no Diagnóstico |
| `conta_em_validacao` / `creditos_insuficientes` / `conta_sem_permissao` | Situação da conta Brevo | Painel/suporte do Brevo |
| `limite_excedido` | Cota diária (Brevo gratuito: 300/dia; Gmail: ~500/dia, código SMTP `550/5.4.5`) ou rajada | Aguardar; revisar o plano |
| `provedor_indisponivel` / `rede` / `conexao_sem_resposta` | Instabilidade do Brevo ou da rede (pedido não processado) | Conferir status do Brevo; reenviar depois |
| `tempo_esgotado` / `conexao_interrompida` / `gateway_sem_resposta` | Resposta perdida depois do envio: **o e-mail pode ter sido aceito** | Conferir o evento no Brevo antes de reenviar |
| `configuracao_invalida` | `BREVO_API_URL` errada, sem https ou redirecionada | Voltar ao padrão `https://api.brevo.com/v3/smtp/email` |
| `smtp_conexao` | SMTP bloqueado (Render Free) ou servidor fora | Usar o Brevo |
| `smtp_senha_de_app` | Gmail exige senha de app (`534/5.7.9`) | Seção 10 |
| `smtp_autenticacao` | Usuário ou senha SMTP recusados (Gmail: `535/5.7.8`) | Conferir `MAIL_USERNAME`/`MAIL_PASSWORD` |
| `destinatario_invalido` / `sem_destinatario` | E-mail cadastrado inválido ou ninguém elegível | Corrigir o cadastro |

## 6. Aceite × entrega

`ACEITO_PELO_PROVEDOR` quer dizer que o Brevo recebeu a mensagem e devolveu um
`messageId` (registrado em `id_mensagem=`). A entrega depende do provedor do
destinatário. Para confirmar:

1. Brevo → *Transacional → Logs* (ou *Estatísticas → Logs* conforme a versão do
   painel): filtre pela etiqueta `nexo-<tipo>` (por exemplo
   `nexo-recuperacao_senha`, `nexo-boas_vindas`, `nexo-teste`) e confira o
   evento: *Entregue*, *Adiado*, *Soft bounce*, *Hard bounce*, *Bloqueado*,
   *Spam*, *Inválido* ou *Erro*. Cada mensagem também leva o cabeçalho
   `X-Nexo-Correlacao` com a correlação do log.
2. **No próprio NEXO**: em *Diagnóstico de e-mail → Envios recentes*, cada envio
   aceito pelo Brevo mostra o **ID no Brevo** (`messageId`) e o botão
   **Consultar entrega**, que lê
   `GET /v3/smtp/statistics/events?messageId=<id>` (somente leitura, só para
   envios do histórico deste processo, limite de 20 consultas por hora por
   admin). Aparecem só o tipo e o horário dos eventos **de entrega** (recebido,
   entregue, adiado, devolvido, bloqueado, inválido, erro) — nunca abertura,
   clique, descadastro, destinatário, motivo, IP ou assunto. "Entregue" quer
   dizer entregue ao **servidor** do destinatário. Se a consulta falhar, a linha
   diz que ela não foi concluída (o aceite do envio continua valendo).
3. Caixa de entrada **e pasta de spam** do destinatário.

## 7. Testes controlados

Automatizados (sem rede e sem envio real): `python -B -m unittest
tests.test_emails_transacionais tests.test_disparador_emails
tests.test_auth_email_flows tests.test_email_logging -v`.

Teste real (somente com autorização e destinatário controlado). Antes, confirme
que o deploy está na versão certa (seção 12). Registre cada etapa
separadamente — uma não comprova a seguinte:

| Etapa | Evidência |
| --- | --- |
| 1. A aplicação iniciou o envio | Linha em *Envios recentes* e, no log, `correlacao=<Ref.>` |
| 2. O provedor aceitou a mensagem | Estado *Aceito pelo provedor* e o *ID no Brevo*; no log, `estado=ACEITO_PELO_PROVEDOR id_mensagem=<id>` |
| 3. O provedor registrou a entrega | *Consultar entrega* com o evento *Entregue ao servidor do destinatário* (ou Brevo → Transacional → Logs, filtrando pelo ID) |
| 4. O destinatário recebeu | O próprio destinatário abre a caixa de entrada (ou o spam) e confirma |

1. **Configuração**: Admin → Diagnóstico de e-mail sem pendências bloqueantes.
2. **Envio de teste**: botão "Enviar e-mail de teste para mim" (vai apenas para
   o e-mail do administrador logado; limite de 5 por hora). A mensagem mostra o
   resultado do provedor e a referência; depois de um ou dois minutos, use
   *Consultar entrega* na linha do envio e confira a sua caixa de entrada.
3. **Recuperação de senha**: com uma conta de teste controlada, use "Esqueci
   minha senha" uma única vez; confira nos logs a linha `tipo=recuperacao_senha`
   (`estado=ACEITO_PELO_PROVEDOR`), no Brevo o evento da etiqueta
   `nexo-recuperacao_senha`, a caixa de entrada e o spam; abra o link (válido por
   15 minutos e de uso único) e defina a nova senha.
4. **Cadastro de empresa**: cadastre uma empresa de teste com e-mail controlado;
   a mensagem do cadastro informa o aceite; confira a etiqueta
   `nexo-boas_vindas` no Brevo e conclua a ativação pelo link.
5. Registre a data, a referência (`Ref.`), o `messageId` e o evento final do
   Brevo.

## 8. Quando o Brevo estiver indisponível

- Nada é perdido no banco: cadastros, publicações e chamados continuam
  confirmados; o sininho segue funcionando.
- Clientes pendentes de ativação recebem novo link por *Detalhes da empresa →
  Acesso ao portal → Reenviar link de ativação* ou pelo "Esqueci minha senha".
- Recuperação de senha: o cliente repete o pedido depois (os links antigos
  expiram em 15 minutos).
- Avisos de análise publicada/chamado resolvido que falharam não são
  reenviados automaticamente (o cliente vê o aviso no sininho).

## 9. E-mails com maiúsculas e minúsculas

Cadastros novos gravam o e-mail em minúsculas e recusam um endereço que já
exista com outra combinação de maiúsculas. Salvar a edição de uma empresa grava
o e-mail de contato também na forma normalizada (ação explícita do admin).
Login e recuperação procuram o texto exato e, se não houver, a conta única que
coincide sem diferenciar maiúsculas.
**Nenhum registro existente é alterado.** Para conferir conflitos legados
(somente leitura, no SQL Editor do Supabase):

```sql
SELECT lower(email) AS email_normalizado, count(*) AS contas
FROM usuario GROUP BY lower(email) HAVING count(*) > 1;
```

Se houver resultado, decida manualmente qual conta manter: enquanto o conflito
existir, o pedido de recuperação com uma grafia diferente das gravadas não
envia nada (e registra um aviso no log).

## 10. Gmail SMTP fora do Render Free

O projeto original (versão anterior ao Render) enviava pelo Gmail SMTP com
Flask-Mail, e esse caminho continua disponível onde a saída SMTP é permitida:
máquina local, servidor próprio ou instância **paga** do Render. No Render Free
ele não funciona (seção 2).

```dotenv
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USE_SSL=false
MAIL_USERNAME=<conta>@gmail.com
MAIL_PASSWORD=<senha de app de 16 caracteres>
MAIL_DEFAULT_SENDER=          # vazio = a própria conta
BREVO_API_KEY=                # vazio, senão o Brevo tem prioridade
```

- A conta precisa da **verificação em duas etapas** e de uma **senha de app**
  (Conta Google → Segurança → Senhas de app). A senha normal da conta é
  recusada: código `534/5.7.9` → categoria `smtp_senha_de_app`. Usuário ou senha
  errados: `535/5.7.8` → `smtp_autenticacao`. Trocar a senha da conta Google
  revoga as senhas de app.
- Use STARTTLS na 587 (como acima) ou SSL direto na 465 (`MAIL_PORT=465`,
  `MAIL_USE_SSL=true`). O Diagnóstico aponta combinações trocadas de porta e
  modo. Numa instância paga do Render, defina também `NEXO_SMTP_PERMITIDO=1`.
- O remetente deve ser a própria conta (ou um alias verificado dela).
- Limite do Gmail pessoal: cerca de 500 e-mails por dia; acima disso, código
  `550/5.4.5` → `limite_excedido`.
- O log mostra só o código numérico da recusa (`codigo=534/5.7.9`), nunca o
  texto do servidor.

## 11. Gmail pela API HTTPS (avaliado, não implementado)

A API do Gmail (`users.messages.send`, HTTPS 443) funcionaria no Render Free e
manteria o remetente `@gmail.com` autenticado pelo próprio Gmail. Ela **não foi
implementada** porque o Brevo já resolve o bloqueio de SMTP e a API do Gmail
traria custos operacionais e riscos:

- **Autorização OAuth 2.0 com refresh token da conta pessoal**, obtido uma vez
  num navegador (Cloud Console: projeto, API do Gmail ativada, tela de
  consentimento *External* publicada *In production*, cliente OAuth e OAuth
  Playground ou script local). Senha de app não serve para a API.
- O token **para de funcionar** com a troca de senha da conta, revogação, 6
  meses sem uso ou após 7 dias se o app ficar em *Testing*; o cliente OAuth
  ocioso por 6 meses é apagado. Até alguém refazer o consentimento,
  recuperação e ativação param — o servidor não resolve sozinho.
- `gmail.send` é escopo **sensível**: sem verificação do Google, o app fica
  restrito a uso pessoal (até 100 usuários, aviso de app não verificado). A
  verificação exige domínio próprio, política de privacidade e vídeo.
- Limite de ~500 envios por dia, dividido com o uso manual da caixa, e sem
  registros de entrega, webhooks ou lista de supressão como os do Brevo.

Se o dono decidir manter o remetente `@gmail.com` sem domínio próprio, esta é a
alternativa documentada, como transporte opcional (variáveis próprias, nunca no
lugar do Brevo sem configuração explícita). A recomendação continua sendo um
domínio próprio autenticado no Brevo.

## 12. Antes de testar em produção: confirme o deploy

O `render.yaml` declara **deploy automático desligado** (`autoDeployTrigger:
'off'`), mas o painel pode estar diferente ([PREPARACAO_RENDER.md](PREPARACAO_RENDER.md),
seção do Auto-Deploy): confirme a configuração efetiva antes de mesclar. Com o
deploy automático desligado, a correção mesclada só entra no ar depois de um
*Manual Deploy*. Para conferir a versão no ar (o `/healthz` é a fonte de
verdade):

1. *Render → serviço → Events*: o último deploy deve estar **live** com o commit
   esperado da `main`.
2. `GET https://<serviço>/healthz` devolve `versao`; compare com o commit da
   `main`.
3. Se houver **domínio próprio verificado** no Render, ele precisa constar de
   `NEXO_PUBLIC_URL` ou `NEXO_TRUSTED_HOSTS` (com e sem `www`, se os dois
   existirem). O health check do Render usa esse domínio no cabeçalho `Host`;
   um host não confiável recebe 400, o deploy é cancelado e a versão anterior
   continua no ar.
4. Depois, *Admin → Diagnóstico de e-mail* sem pendências bloqueantes e o teste
   real da seção 7.

## 13. Ambiente local: SMTP pelo `.env`

Em desenvolvimento (`FLASK_ENV=development`) o envio usa o SMTP definido no
`.env` **da pasta de onde o servidor roda**, ao lado do `config.py`. O *Admin →
Diagnóstico de e-mail* mostra o caminho exato do arquivo lido. Uma variável já
definida no terminal tem prioridade sobre o arquivo, e o `.env` só é lido na
inicialização: depois de alterá-lo, **reinicie o servidor**.

```dotenv
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=True
MAIL_USE_SSL=False
MAIL_USERNAME=<conta>@<dominio>
MAIL_PASSWORD=<senha de app ou token>
MAIL_DEFAULT_SENDER=NEXO <<conta>@<dominio>>
BREVO_API_KEY=
```

| Provedor | `MAIL_SERVER` | Observação |
| --- | --- | --- |
| Gmail / Google Workspace | `smtp.gmail.com` | Exige senha de app (seção 10). |
| Zoho Mail | `smtp.zoho.com` | Contas de domínio próprio podem usar `smtppro.zoho.com`. |
| cPanel | `mail.<seu-dominio>` | Use o nome coberto pelo certificado, senão a verificação TLS falha. |

- **Porta 465:** é SSL direto. Sem `MAIL_USE_SSL` no `.env`, o padrão acompanha
  a porta; com `MAIL_USE_SSL=true` explícito, o STARTTLS é ignorado.
- **`BREVO_API_KEY`:** opcional. Definida, tem prioridade sobre o SMTP.
- **Log de inicialização:** só uma linha, em nível DEBUG: `Transporte local
  ativo` ou `Credenciais de e-mail não configuradas no .env`. As pendências
  ficam no Diagnóstico de e-mail. Em produção o resumo e as pendências continuam
  no log, como na seção 5.
- **Links dos e-mails:** sem `NEXO_PUBLIC_URL`, ativação e recuperação de senha
  usam `http://127.0.0.1:5000`, e os links só abrem na própria máquina. Em outra
  porta, defina `NEXO_PUBLIC_URL`. Produção continua exigindo `https`.
- **Conferir a conexão:** o botão *Testar conexão SMTP* abre a conexão, negocia
  a criptografia e faz o login, sem enviar mensagem. O *Envio de teste* entrega
  uma mensagem real ao e-mail do administrador logado. O status mostrado é o da
  última tentativa desde o início do servidor, seja por esse botão ou por um
  envio. Uma recusa depois do login (remetente, destinatário ou mensagem) não
  altera o status: a conexão e o login funcionaram. Com `MAIL_USE_TLS` e
  `MAIL_USE_SSL` desligados, o teste faz o login sem criptografia.
- **Valores do `.env`:** o SMTP só aceita ASCII em `MAIL_SERVER`, `MAIL_USERNAME`
  e `MAIL_PASSWORD`; o espaço especial que acompanha a senha de app copiada do
  Google vira espaço comum. Outro caractere acentuado aparece no painel como
  valor inválido, sem mostrar o valor.

Em Configurações, cada novo chamado pode gerar uma cópia oculta (BCC) para a caixa
escolhida pelo administrador. O destinatário visível é o próprio remetente do NEXO e
a caixa não aparece nos cabeçalhos; sem caixa configurada, nenhum e-mail sai. Se o
servidor recusar só a caixa da cópia, o envio conta como falha, com a categoria
`smtp_destinatario_recusado`. A troca do e-mail de acesso avisa o endereço antigo e o
lembrete de renovação sai pelo botão em Contratos ou pelo comando
`flask contratos lembretes-renovacao`. O lembrete de aceite dos Termos de Uso e da
Política de Privacidade (`lembrete_aceite`) sai pelo botão “Enviar Lembrete por E-mail”
em `/admin/termos-aceites`, só para quem ainda está pendente, com os nomes e as versões
pendentes e o link do portal, nunca o texto. Os avisos de senha e de e-mail alterados e o
lembrete de aceite não têm opção de desligar; o de chamado resolvido e o lembrete de
renovação respeitam as preferências do usuário.

A detecção da hospedagem que bloqueia SMTP fica só em `config.py`
(`HOSPEDAGEM_BLOQUEIA_SMTP`); o módulo de e-mails lê esse indicador neutro e, na
máquina local, ele é sempre falso.
