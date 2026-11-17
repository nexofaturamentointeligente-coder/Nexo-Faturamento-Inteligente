# NEXO-COM-002 — Avaliações e depoimentos

**Situação em 16/11/2026: reativada no código (frente R do redesenho da landing), sem migração aplicada em
banco real.** Pedido do dono: "Reative o botão 'Deixar Avaliação' com o seguinte fluxo: exige estar logado ->
abre modal com seleção de estrelas e campo de texto". As regras e as telas de 02/11/2026 (`avaliacoes.py`,
`blueprints/avaliacoes.py` e os templates do portal) foram mantidas; o que faltava para elas rodarem (modelos,
migração, configuração, macros, registro do blueprint) foi refeito à mão, sem copiar a pasta de guarda. Abaixo
do resumo fica o registro original de requisitos, de 24/10/2026.

## Como funciona agora

1. **Landing, seção #avaliacoes.** A rota `/` chama `avaliacoes.contexto_da_landing(current_user)` e a landing
   inclui `templates/_avaliacao_landing.html`, com `static/css/landing-avaliacao.css` e
   `static/js/landing-avaliacao.js` no `<head>`. O que aparece depende de quem visita:
   - visitante: "Deixar Avaliação" leva a `/avaliar` (funciona sem JavaScript);
   - cliente que pode avaliar: o botão abre o modal; sem JavaScript, leva ao formulário do portal;
   - quem já avaliou: "Ver minha avaliação";
   - empresa sem conta ativa ou termos atualizados a aceitar: o motivo, e no segundo caso o caminho do aceite;
   - administrador: "Ver as avaliações recebidas";
   - banco fora ou tabelas ausentes: o botão desabilitado e o aviso. A landing nunca cai por causa das
     avaliações: a falha é aberta e só o tipo do erro vai para o log.
2. **Login.** `/avaliar` manda o visitante para `/auth/login?next=/avaliar`. O formulário de login agora preserva
   o `next`, e o servidor só o segue se for um caminho local que abra por GET. Depois do login, `/avaliar` leva o
   cliente que pode avaliar a `/?avaliar=1#avaliacoes`, onde o modal abre sozinho; quem já avaliou vai para
   "Minha avaliação"; quem tem termos a aceitar passa antes pelo aceite e volta.
3. **Modal.** Estrelas de 1 a 5 (rádios nativos, pelo teclado), comentário de 5 a 1000 caracteres com contador,
   a caixa de autorização de publicação desmarcada (o `TEXTO_CONSENTIMENTO` de sempre, versão 2026-11-01) e o
   token CSRF. O foco fica preso no diálogo e volta ao botão; Esc fecha. O envio é por fetch para
   `/cliente/avaliacao`, e a resposta JSON nunca usa flash: 200 recebida ("Recebemos sua avaliação. Ela será
   publicada depois da revisão da equipe."; sem a autorização, avisa que ela fica só com a equipe); 409 já
   avaliou; 422 erros por campo; 429 muitas tentativas (10 por hora); redirecionamento quando a sessão venceu ou
   há termos a aceitar; 503 indisponível.
4. **Moderação.** Toda avaliação nasce PENDENTE, com os eventos CRIADA e, com a autorização, CONSENTIMENTO_CONCEDIDO
   (com a versão do aviso). Os administradores recebem o aviso no sininho, com o link da própria avaliação, e o
   menu deles ganhou "Avaliações" (`/admin/avaliacoes`). Aprovar publica só com a autorização do cliente; rejeitar
   exige motivo (3 a 500 caracteres); retirar tira do carrossel; toda decisão vale para a versão que foi lida.
5. **Carrossel.** `landing_reviews` traz só o DTO mínimo: texto, autor (ou "Cliente NEXO"), empresa só com
   autorização, nota de 1 a 5 e data. O template escapa tudo; e-mail, ids e sugestão privada nunca saem.
6. **Exclusão permanente de empresa.** Leva as avaliações da empresa e dos usuários dela, com as sugestões, os
   eventos, o pedido dos 60 dias e o aviso do sininho que cita a empresa.

## Banco: revisão d3f5b7a9c1e2 (depois da c8e2a4f6b0d1)

- As quatro tabelas (`avaliacao_cliente`, `sugestao_melhoria`, `evento_avaliacao` e `solicitacao_avaliacao`) têm
  a estrutura exata da antiga c5e8a1f4b7d2, que já as criou no banco do .env.
- Sem as tabelas, a revisão as cria. Com as quatro, confere colunas, tipos, nulidade, padrões, chaves,
  restrições e índices contra `models.py` e as adota, acrescentando só índice ausente, RLS e políticas. Só parte
  delas, qualquer divergência, uma política de mesmo nome com outra definição ou FORCE ROW LEVEL SECURITY
  interrompem a revisão antes de qualquer mudança. Nunca DROP de tabela nem recriação.
- A única definição diferente aceita é a `deny_api_access` permissiva de negação pura (`FOR ALL TO anon,
  authenticated USING (false)`, sem `WITH CHECK` ou com `WITH CHECK (false)`): a revisão a troca pela restritiva
  na mesma transação. Qualquer outra variação continua interrompendo.
- No PostgreSQL: RLS ligado, privilégios de anon e authenticated revogados, `deny_api_access` (restritiva) e
  `acesso_servidor`, como na d9e4b7a2c6f1.
- O downgrade recusa enquanto houver linha.
- **Sondagem do banco do .env em 17/11/2026 (só leitura):** as quatro tabelas existem, vazias, com RLS ligado e
  a `deny_api_access` permissiva de negação pura (criada fora das migrações, como em cerca de 30 tabelas mais
  antigas, entre elas `documento_legal`), sem `acesso_servidor` e com todos os privilégios de anon e authenticated.
  A versão anterior da revisão pararia nessa política; a atual adota as quatro, sem criar índice. O deploy do
  Render não migra sozinho.
- **Aplicada no banco do .env em 17/11/2026**, junto com a c8e2a4f6b0d1, numa transação só, depois de backup
  verificado (`nexo_backups_privados/2026-11-17_antes_c8e2a4f6b0d1`, restauração isolada com as contagens do
  snapshot) e de um ensaio sobre o dump real. Conferência posterior: as quatro com RLS sem FORCE,
  `deny_api_access` restritiva e `acesso_servidor`, sem privilégio da Data API, restrições e índices iguais aos de
  antes e nenhuma linha mudada.

## Testes

`tests/test_avaliacoes_clientes.py`: os estados da landing e do parcial, `/avaliar` e o login com `next`, o envio
(200, 422, 400, 409, 403, 429 e 503), a moderação, o DTO escapado, a migração em SQLite (criação, adoção do SQL
da c5e8a1f4b7d2, divergências, esquema parcial, downgrade e prévia offline) e a exclusão permanente. Com
`NEXO_TEST_POSTGRES_BIN`, o mesmo em PostgreSQL real, com RLS, políticas, privilégios e envios simultâneos. Com
`NEXO_TEST_BROWSER=1`, o modal no navegador: teclado, foco, Esc, 320 a 1366 px e sem JavaScript.

## Pendências do dono

- **Texto jurídico.** O `TEXTO_CONSENTIMENTO` cita a Política de Privacidade, que ainda não tem página publicada.
  O modal avisa que o link aparecerá quando houver, e ele aparece sozinho quando a página pública existir.
- **Conteúdo do modal.** Hoje só nota, comentário e autorização; publicada, a avaliação aparece como "Cliente
  NEXO". Nome de exibição e nome da empresa ficam na edição pelo portal. Decidir se entram no modal.
- **Pedido automático dos 60 dias.** Continua desligado: o painel não o mostra. Os valores das chaves
  `AVALIACAO_*` vieram da versão de 02/11 (60, 30, 3 e 24).
- **Cache da landing.** Com avaliações publicadas, a página `/` precisa de Cache-Control (sugestão: no-store), para
  uma avaliação retirada não continuar em cache.
- **Retenção** de avaliações rejeitadas, sugestões e evidências de consentimento (seção "Privacidade" abaixo).
- **Uma avaliação por usuário**, não por empresa: uma empresa com vários usuários pode ter vários depoimentos.

## Registro original de requisitos (24/10/2026)

Registro de requisitos em 24/10/2026 (America/Sao_Paulo), quando a COM-001 preparou a apresentação da landing
sem criar sistema de avaliações, tabelas, migrações ou gatilho de 60 dias. Os itens abaixo continuam valendo
como critérios; o que mudou desde então está no resumo acima.

### O que a COM-001 entrega

- Carrossel reutilizável em `templates/landing.html`, `static/js/landing.js` e
  `static/css/landing.css`, com anterior/próximo, indicadores, operação por
  teclado, transição curta e respeito à preferência de movimento reduzido.
- Estado vazio explícito quando não há depoimentos reais autorizados. Não há
  avaliações publicadas, empresa ou resultado financeiro inventado.
- Botão “Deixar Avaliação” desabilitado, acompanhado da informação de que o envio
  ainda não está disponível. Não encaminha ao login prometendo formulário inexistente.
- Contrato de apresentação opcional `landing_reviews`: o template considera
  `approved` e `publication_consent` e escapa `quote`, `author` e `company`.
  A rota atual não fornece uma coleção real de avaliações.

Essa filtragem de template não substitui controles de autorização ou consentimento
no backend futuro. Nenhum dado privado deverá chegar ao contexto da landing.
Os exemplos utilizados nos testes existem apenas durante a execução dos testes.
Alternatintas é uma possibilidade de case mencionada pelo proprietário, sem
conteúdo ou autorização verificável para publicação nesta entrega.

### Fluxo a implementar na COM-002

1. Cliente autenticado e elegível recebe solicitação de avaliação após 60 dias
   de uso ativo. A notificação deve permitir fechar ou adiar e não bloquear o portal.
2. Formulário acessível contém nota obrigatória de 1 a 5 estrelas, comentário
   destinado à publicação e campo separado de sugestão privada.
3. Explicar antes do envio quais dados poderão ser públicos, quem acessará a
   sugestão privada e qual será a finalidade do tratamento. Não prometer anonimato
   se houver associação identificável à conta ou empresa.
4. Consentimento de publicação explícito e separado, sem caixa pré-marcada nem
   condicionamento do serviço. Identificação do autor/empresa, marca e eventual
   citação de resultados exigem autorização correspondente e verificável.
5. Avaliação enviada permanece privada e pendente. A equipe autorizada visualiza,
   aprova ou rejeita a publicação. Aprovação não substitui consentimento.
6. Somente conteúdo aprovado, com consentimento válido e sem revogação, integra
   o carrossel público. Não publicar automaticamente por atingir nota mínima.
7. Sugestão privada fica disponível à equipe autorizada em área integrada ao
   suporte. Nunca deve ser copiada automaticamente para comentário público.

### Definição de “60 dias de uso ativo” pendente

A COM-002 deve definir e documentar a regra antes da implementação: evento inicial,
contagem de atividade, períodos de suspensão/cancelamento ou lixeira, fuso horário,
elegibilidade por usuário ou empresa e intervalo entre novos pedidos.
`criado_em`, ausência de login ou permanência de um cookie não comprovam, por si,
60 dias de uso ativo. Não inferir histórico inexistente nem habilitar o gatilho
para todos os clientes legados sem uma política aprovada.

A regra precisa ser persistente e idempotente, funcionar após reinício e impedir
solicitações duplicadas em abas, processos e requisições concorrentes. Adiamento,
recusa, envio e revogação de consentimento devem ter tratamento explícito.

### Administração e moderação

- Acesso restrito ao papel administrativo já autorizado, com verificação no
  servidor em cada operação de consulta, aprovação, rejeição e retirada.
- Listas separadas de pendentes, aprovadas/publicadas e rejeitadas/retiradas;
  visualizar nota, comentário e consentimento sem expor sugestões no conteúdo público.
- Não alterar silenciosamente o sentido de uma avaliação. Mudança material de
  texto ou identificação exige nova revisão e, quando aplicável, nova autorização.
- Registrar de forma proporcional a autoria administrativa, decisão e data para
  auditoria. Não armazenar tokens, credenciais ou conteúdo privado em logs públicos.
- Aprovação, retirada e alteração de consentimento devem ser transacionais;
  uma publicação concorrente não pode restaurar conteúdo já retirado.
- Definir limites de tamanho, frequência e duplicidade. Renderizar texto com
  escape, sem HTML/JavaScript fornecido pelo usuário e sem links ativos arbitrários.

### Privacidade, consentimento e LGPD

Documentos e avisos deverão ser revisados pelo responsável antes de disponibilizar
o fluxo real. Este planejamento não constitui política jurídica publicada.

- Minimizar dados: não pedir documentos, dados financeiros de clientes ou dados
  sensíveis para deixar uma avaliação. Orientar o cliente a não incluí-los no texto.
- Separar autorização de publicar nome, empresa, marca e comentário do acesso ao
  serviço. Não usar a sugestão privada como consentimento de publicidade.
- Registrar evidência suficiente da versão do aviso, finalidade, manifestação,
  data e escopo do consentimento, com acesso restrito e retenção definida.
- Oferecer canal funcional para retirar autorização e corrigir identificação.
  A revogação deve retirar a publicação e invalidar caches que a exibam, conforme
  procedimento e prazo aprovados; não afirmar remoção de cópias de terceiros.
- Definir retenção e descarte de avaliações rejeitadas, sugestões e evidências,
  inclusive tratamento em backups, exportação e exclusão de empresa/usuário.
- Revisar o uso de nomes, marcas e resultados: consentimento não comprova a
  veracidade de uma alegação financeira. Não criar promessa de resultado garantido.

### Isolamento entre empresas e segurança

- Cliente pode consultar ou modificar somente registros permitidos da própria
  empresa. Derivar a empresa da sessão/usuário no banco, sem confiar em IDs enviados
  pelo navegador; testar troca de IDs e acesso direto a endpoints.
- Autenticação, sessões revogadas, primeiro acesso e empresa na lixeira devem
  continuar sujeitos às SEC-002/003/004. Não criar exceção de autorização para avaliar.
- Todas as operações de alteração exigem método apropriado, CSRF válido e validação
  no servidor. GET não deve enviar, aprovar ou retirar avaliações.
- Sugestões privadas permanecem no suporte autenticado; não enviar em contexto
  público, APIs públicas, salas Socket.IO compartilhadas, métricas ou logs.
- Reutilizar autorização pelo banco e salas privadas existentes em eventual
  notificação. Não permitir leitura cruzada entre empresas ou contas revogadas.
- Aplicar limites de entrada e frequência compatíveis com os mecanismos existentes;
  validar concorrência e evitar cópias não autorizadas em buscas e exportações.

### Integração futura com a landing

O backend da COM-002 deverá construir um DTO público mínimo e verificado para
`landing_reviews`, contendo somente texto e identificação cuja publicação seja
permitida. Excluir sugestões, e-mail, IDs internos e metadados de moderação.
A seleção deve consultar aprovação e consentimento vigentes no banco; o template
é defesa complementar. Definir política de cache e retirada antes de publicar.

Ativar “Deixar Avaliação” apenas depois de existir um destino funcional autenticado.
Após autenticar, preservar a política de redirecionamento seguro e verificar
a elegibilidade no servidor. Não permitir que parâmetros de retorno causem
redirecionamento aberto ou contornem a autorização.

### Critérios de aceitação da COM-002

- Testes de elegibilidade nos limites de 59/60/61 dias com relógio controlado,
  contas legadas, períodos inativos e regra aprovada de uso ativo.
- Estrelas operáveis por teclado e leitor de tela, rótulos e erros acessíveis,
  foco controlado no pop-up e opção de fechar sem prejudicar uso do portal.
- Nota limitada a 1–5 e campos limitados/validados; textos potencialmente maliciosos
  nunca executados no portal, administração ou landing.
- Testes de autorização, CSRF, sessões antigas, empresas distintas e permissões
  administrativas em todas as operações; sugestões ausentes de respostas públicas.
- Consentimento ausente ou revogado impede publicação, mesmo após aprovação;
  rejeição e retirada permanecem efetivas sob concorrência e cache.
- Nenhuma empresa, nome, logo ou depoimento publicado sem conteúdo e autorização
  verificáveis. Testes sintéticos não devem abastecer o ambiente real.
- Migração futura, se necessária, revisada e validada em bancos descartáveis,
  sem assumir autorização para executar em Supabase de produção.
- Publicação em branch e PR próprias, regressão completa e GitHub Actions verificado,
  sem merge/deploy automático. Liberação depende de revisão específica do proprietário.

### Dependências de conteúdo e decisão

Permanecem pendentes: regra de uso ativo; avisos e documentos jurídicos revisados;
responsáveis pela moderação/suporte; política de retenção e retirada; canal para
solicitações de privacidade; primeiros depoimentos autorizados e evidência de
consentimento. Nenhuma dessas pendências foi preenchida com dados fictícios.
