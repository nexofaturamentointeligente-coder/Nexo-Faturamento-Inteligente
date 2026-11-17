# Plano de Ação Comercial da Landing NEXO — COM-001

Requisitos fornecidos integralmente pelo proprietário em 24/10/2026
(America/Sao_Paulo), substituindo a dependência do PDF externo. Base verificada:
`8233c5bddbd1f6c67a4d95c7b0a2a470724738c8`. Este registro precede a implementação.
Antes da publicação, a branch incorporou por fast-forward a main documental
`c919b1d17e6cbf4bf9dac09e29cefca67d908d06` (PR #19), sem alterar o escopo.

## 1. Limpeza e identidade

- **1.1** Remover referências acadêmicas da interface comercial e avisos públicos
  ou internos desnecessários, preservando autoria e histórico em documentação.
  Não inventar maturidade, funcionalidades, resultados ou informações empresariais.
- **1.2** Simplificar o cabeçalho: remover o botão duplicado “Ver Demo do Painel”,
  manter “Acessar Portal” como ação distinta de aquisição; demonstração continua
  acessível na navegação. Linguagem legível, sem excesso de travessões longos.

## 2. Hero

- **2.1** Benefício imediato, inspirado em “Aumente seu faturamento através de
  decisões assertivas com o NEXO!” e “O NEXO interpreta relatórios complexos do
  seu sistema e entrega os indicadores essenciais”, sem garantir crescimento.
  Comunicar clareza, oportunidades, reposição e praticidade; reduzir sigla PDV e
  texto técnico. Preservar os gráficos demonstrativos existentes.
- **2.2** Remover do hero “Sem cadastro automático”, “Atendimento consultivo” e
  “Dados processados com Pandas”; manter informação material do fluxo em local adequado.

## 3. Proposta de valor e solução

- **3.1** Apresentar oportunidades antes dos problemas: relatórios de compras e
  vendas apoiam resultados, capital de giro e decisões de reposição. Não garantir
  aumento de faturamento ou eliminação de estoque encalhado. O sistema não mede
  estoque físico; sinais de descasamento são estimativas, sem lucro/margem/CMV.
- **3.2** Apresentar o NEXO como interpretação inteligente de relatórios, sem
  promessa absoluta nem título longo “com portal digital”. Evitar Pandas,
  pipeline, Plotly.js e jargão nos textos comerciais. Usar “Parecer Estratégico
  do Sistema” preservando a informação de revisão/elaboração pela equipe.
- **R1–R4** “Envie seus relatórios”; “Organização dos dados”; “Indicadores
  estratégicos”; “Parecer estratégico”. Descrever o processo ao empresário.

## 4. Prova social e avaliações

- **4.1** Preparar carrossel acessível e responsivo, transições suaves,
  anterior/próximo e indicadores. Publicar somente depoimentos autorizados e
  aprovados. Alternatintas é referência potencial, não autorização de uso.
  Remover depoimento demonstrativo atribuído a cliente. Sem conteúdo autorizado,
  entregar estrutura e estado vazio honestos; não inventar nomes, empresas ou resultados.
- **4.1-CTA** “Deixar Avaliação” só pode ser ativado quando existir fluxo real
  após autenticação do cliente. Nesta fase preparar o componente e documentar a
  integração, sem formulário fictício nem link que prometa avaliação disponível.
- **4.2 / COM-002** Solicitação após 60 dias de uso ativo; nota 1–5, comentário
  público e sugestão privada; moderação/aprovação/rejeição e consentimento de
  publicação; sugestões em suporte administrativo; privacidade e isolamento por
  empresa. Documentar em `PENDENCIAS_NEXO_COM002_AVALIACOES.md`. Sem tabelas,
  migrações, gatilho de 60 dias ou gravação de avaliações nesta COM-001.

## 5. Indicadores, vídeo e planos

- **5.1** “Indicadores Estratégicos” substitui “KPIs do MVP”. Remover linguagem
  que desvaloriza o produto e o box “Aviso de Transparência”; preservar explicação
  discreta e acessível da estimativa do Indicador de Pressão de Estoque e todos
  os indicadores oficiais, sem transformá-lo em estoque real ou rentabilidade.
- **5.2** Título de demonstração “Seja um cliente NEXO e tenha acesso ao portal.”;
  método “Mais gráficos, menos textos e dados soltos.”; explicação curta do áudio
  e “Valores ilustrativos” visível. Preservar MP4 e player, reprodução, loop,
  mudo/ativação por gesto quando exigido, tela cheia, erro e compatibilidade móvel.
  Não reescrever o player nem enfraquecer controle de áudio/visibilidade.
  Validar acessibilidade e reexecutar cenários do player documentados.
- **5.3** Apresentar BRONZE/PRATA/OURO com “Contrate Agora” ou “Assinar Plano”,
  somente para canal real autorizado. Verificar checkout/atendimento/WhatsApp/URL;
  não inventar preço, URL ou cobrança nem usar login como checkout. Se não houver
  canal confirmado, registrar pendência e não simular contratação funcional.

## 6. CTA final e rodapé

- **6.1** Encerrar com “Pronto para transformar seu negócio? Deixe a NEXO fazer
  parte disso!” e “Falar com a equipe NEXO”. WhatsApp Business só após confirmação;
  não usar telefone inventado/pessoal sem autorização.
- **6.2** Quatro colunas: institucional (logo, proposta, razão social/CNPJ reais);
  navegação (início, como funciona, indicadores, planos, login); compliance/LGPD
  (privacidade, termos, cancelamento reais/revisados); contatos oficiais (e-mail,
  WhatsApp e redes autorizadas). `suporte@nexofaturamento.com.br` é referência
  não confirmada; não publicar como canal ativo sem comprovação. Sem links quebrados,
  dados empresariais ou políticas fictícias. Dependências oficiais ficam documentadas.

## Restrições e validação

Preservar Flask/Jinja e identidade visual, autenticação/sessões, uploads/ETL,
KPIs/Curva ABC, PDFs, NexoBot, notificações/Socket.IO, PostgreSQL e Render.
Sem alteração financeira ou de segurança, checkout/pagamentos, tabelas ou migrações.

Testes específicos: landing/header/headline/CTAs/âncoras/login, links, ausência
acadêmica e de prova social/dados falsos, indicadores/valores ilustrativos,
vídeo/controles, teclado/foco/contraste. Larguras 320, 375, 390, 430, 768, 1024 e
1440 px em Playwright. Executar regressão geral e confirmar CI real.

Responder no relatório as 14 perguntas de validação comercial do proprietário:
entendimento em segundos; benefício; objetivos dos CTAs; contratação válida;
diferenciação de planos; indicadores corretos; vídeo; portal; mobile; promessas;
prova social fictícia; contatos confirmados; documentos jurídicos revisados;
clareza para solicitar contratação. Respostas negativas não serão ocultadas.

Documentar A–T em `RELATORIO_NEXO_COM001_LANDING_COMERCIAL.md`, incluindo antes/depois,
arquivos, testes, limitações, conteúdo oficial pendente e COM-002.

Publicação autorizada: branch `feature/com-001-landing-page-comercial`, commit,
push, PR em rascunho, acompanhamento e correções da CI. **Sem merge/auto-merge,
deploy ou acesso/alteração no Supabase de produção.** Auto-Deploy pode estar ativo.

## Matriz de rastreabilidade

A situação abaixo descreve a implementação da COM-001. A existência dos testes
citados é evidência de cobertura planejada e executável; os resultados efetivamente
obtidos e as limitações de navegador/CI constam do relatório técnico. Nenhum canal
comercial ou documento jurídico foi considerado oficial por aparecer como exemplo.

| ID | Requisito | Situação | Arquivos | Evidência |
| -- | --------- | -------- | -------- | --------- |
| 1.1 | Identidade comercial sem referências acadêmicas desnecessárias | Implementado | `templates/landing.html`; `templates/como_aderir.html`; `templates/cliente/dashboard.html` | Texto comercial; histórico técnico preservado; `test_academic_labels_and_technical_sales_copy_are_absent` |
| 1.2 | Cabeçalho simplificado, portal distinto da demonstração e menu móvel | Implementado | `templates/landing.html`; `static/js/landing.js`; `static/css/landing.css` | Um CTA de portal no cabeçalho, âncora de demonstração, menu por teclado; testes de cabeçalho/menu |
| 2.1 | Headline de benefício e gráficos demonstrativos preservados | Implementado | `templates/landing.html`; `static/js/landing.js` | “Decisões mais claras para fazer sua loja crescer”; três conjuntos de dados e alternativas textuais consultáveis |
| 2.2 | Remover três badges do hero sem omitir o fluxo | Implementado | `templates/landing.html` | Badges removidos; revisão pela equipe explicada na seção do processo e do método |
| 3.1 | Oportunidades, compras/vendas e capital de giro sem garantia de estoque/lucro | Implementado | `templates/landing.html` | Copy orientada à decisão e nota explícita sobre estimativa; `test_official_indicators_and_estimate_explanation_are_preserved` |
| 3.2 | Solução e Parecer Estratégico do Sistema sem jargão ou automação fictícia | Implementado | `templates/landing.html`; `templates/como_aderir.html` | Revisão/elaboração pela equipe informada, sem prometer devolutiva inteiramente automática |
| R1–R4 | Quatro cards explicam o processo ao empresário | Implementado | `templates/landing.html` | Envio, organização, indicadores e parecer; `test_landing_has_clear_benefit_headline_and_commercial_process` |
| 4.1-estrutura | Carrossel reutilizável, navegação e posição acessíveis | Implementado | `templates/landing.html`; `static/js/landing.js`; `static/css/landing.css` | Estado vazio real; anterior/próximo e indicadores para conteúdo aprovado; teste de navegação com conteúdo somente de teste |
| 4.1-conteúdo | Depoimentos reais aprovados e autorizados | Pendente de conteúdo oficial | `templates/landing.html`; `Docs/PENDENCIAS_NEXO_COM002_AVALIACOES.md` | Nenhum depoimento publicado; não há autorização verificável de Alternatintas ou outro cliente |
| 4.1-CTA | “Deixar Avaliação” preparado sem anunciar fluxo inexistente | Implementado parcialmente | `templates/landing.html`; `Docs/PENDENCIAS_NEXO_COM002_AVALIACOES.md` | Botão desabilitado e indisponibilidade informada; ativação depende da COM-002 |
| 4.2 | Avaliação após 60 dias, estrelas, comentário, sugestão e moderação | Planejado para COM-002 | `Docs/PENDENCIAS_NEXO_COM002_AVALIACOES.md` | Requisitos de privacidade, consentimento, isolamento e suporte registrados; sem backend/tabelas/migrações nesta fase |
| 5.1 | Indicadores Estratégicos e estimativa explicada discretamente | Implementado | `templates/landing.html` | Indicadores preservados; removido o box antigo; nota de que não mede estoque físico, lucro ou margem |
| 5.2 | Copy da demonstração, valores ilustrativos e player preservado | Implementado | `templates/landing.html`; `static/js/landing-video.js`; `static/css/landing.css` | MP4/poster preservados; controles de som/tela cheia/erro mantidos, adicionadas pausa/mudo explícitos e preferência de movimento reduzido; validação e limites no relatório |
| 5.3-apresentação | Diferenciar BRONZE/PRATA/OURO e CTAs comerciais | Implementado | `templates/landing.html`; `static/css/landing.css` | Frequências e características preservadas, “Sob consulta” e “Contrate Agora”; login não funciona como checkout |
| 5.3-contratação | Canal real de contratação, checkout ou atendimento autorizado | Pendente de conteúdo oficial | `templates/landing.html`; `templates/como_aderir.html` | Não há canal confirmado; três botões desabilitados e página informativa honesta, sem cobrança nova |
| 6.1-copy | CTA final e objetivo de contato claros | Implementado | `templates/landing.html` | Título solicitado e “Falar com a equipe NEXO”, com aviso de canal ainda não publicado |
| 6.1-canal | Direcionamento ao WhatsApp Business oficial | Pendente de conteúdo oficial | `templates/landing.html`; `templates/como_aderir.html` | Sem número oficial confirmado; contato desabilitado e placeholder anterior removido |
| 6.2-estrutura | Rodapé institucional, navegação, privacidade e contatos | Implementado | `templates/landing.html`; `static/css/landing.css` | Quatro colunas, navegação e login reais, avisos para documentos/canais não publicados |
| 6.2-identificação | Razão social e CNPJ confirmados | Pendente de conteúdo oficial | `templates/landing.html` | Dados desconhecidos omitidos; nenhum identificador empresarial inventado |
| 6.2-jurídico | Privacidade, termos de uso e cancelamento revisados | Pendente de conteúdo oficial | `templates/landing.html` | Sem links jurídicos fictícios; ausência de documentos informada |
| 6.2-contatos | E-mail, WhatsApp e redes oficiais ativos/autorizados | Pendente de conteúdo oficial | `templates/landing.html`; `templates/como_aderir.html` | Referência de e-mail não tratada como canal confirmado; sem telefone/mailto fictício |
| V1 | Responsividade, âncoras, teclado, foco e alternativas aos gráficos | Implementado | `tests/test_landing_commercial.py`; `static/css/landing.css`; `static/js/landing.js` | Cobertura para 320/375/390/430/768/1024/1440 px, menu, skip link, foco e fallback; resultados reais no relatório |
| V2 | Testes, regressão, documentação e publicação para revisão | Implementado parcialmente | `tests/test_landing_commercial.py`; `tests/test_landing_video_commercial.py`; `Docs/RELATORIO_NEXO_COM001_LANDING_COMERCIAL.md` | 666 testes locais aprovados (558 gerais + 108 PostgreSQL), [PR #21](https://github.com/Helio965/Nexo---Faturamento-Inteligente---Render/pull/21) em rascunho; relatório registra falha inicial da CI e ajuste de navegador; resultado da nova execução na PR |

A ausência de conteúdo autorizado não é falha a ser preenchida com exemplos.
A ativação de aquisição e a publicação de documentos/depoimentos dependem de dados
e aprovação do proprietário. A COM-002 continua planejada e não implementada.

## Redesenho de 16/11/2026 (decisões do dono que atualizam a COM-001)

O dono pediu o redesenho da landing em três frentes (visual, navegação com cadastro do lead frio e o envio da
Avaliação de Cenário Externo ao microsserviço) e confirmou os contatos oficiais. O que mudou em relação aos
itens acima; os testes da landing acompanharam cada mudança (`Docs/PRE_CADASTRO_LANDING.md`, seção 9).

| ID | Antes (COM-001) | Agora (decisão do dono, 16/11/2026) | Onde |
| -- | --------------- | ----------------------------------- | ---- |
| 1.2 | "Acessar Portal" como única ação do cabeçalho | "Entrar" (link de texto para o login) e "Cadastre-se" (botão de destaque que abre o cadastro do lead frio; sem JavaScript, leva aos contatos do rodapé) | `templates/landing.html`; `static/js/landing-captacao.js` |
| 2.1 | Gráficos demonstrativos preservados no hero | Hero centralizado, sem a tag, sem a frase de público (tintas), sem a nota de baixo dos botões e sem o painel da direita; o texto vale para qualquer varejo | `templates/landing.html`; `static/css/landing.css`; `static/js/landing.js` |
| 3.1 | Cards de oportunidades com nota de não garantia | Quatro benefícios financeiros (vender abaixo do custo, descontos fora do preço normal, capital parado, dinheiro recuperável e Plano de Ação), cada um com ícone próprio, só com o que o sistema mostra e sem prometer resultado; sem a tag e sem a nota | `templates/landing.html` |
| 3.2 | Revisão pela equipe informada também abaixo dos passos | A nota de baixo dos quatro passos saiu; a revisão pela equipe continua no subtítulo do Método | `templates/landing.html` |
| 4.1-CTA | "Deixar Avaliação" desabilitado | Continua desabilitado até a junção com o módulo de avaliações (marcador `AVALIACOES_LANDING`); a tag virou "Avaliações", um pouco maior, e os cards mostram a nota em estrelas quando o DTO a trouxer | `templates/landing.html`; `static/css/landing.css` |
| 5.1 | Explicação longa da estimativa do Indicador de Pressão de Estoque | O texto longo saiu; os itens estimados levam o selo "Estimativa". A lista cresceu para o que já tem tela no portal (Raio-X do caixa, Auditoria de custos com o selo "Prata em diante", Plano de Ação e Evolução & Conquistas); os indicadores executivos calculados sem tela não são anunciados | `templates/landing.html` |
| 5.2 | Painel demonstrativo | Vídeo e player intactos; o painel ganhou a cara do portal atual (janela, menu lateral, quatro KPIs) e produtos genéricos de varejo, com os mesmos valores ilustrativos | `templates/landing.html`; `static/js/landing.js` |
| 5.3 | Três planos e o CONTRATAR abaixo da grade | Quatro cards (BRONZE, PRATA, OURO "Mais completo" e PERSONALIZADO / ENTERPRISE), todos "Sob consulta", sem botão, com as entregas reais; o "Contrate pelo WhatsApp..." e o "Como obter acesso ao NEXO" de baixo dos cards saíram | `templates/landing.html` |
| 6.1 | Título, subtítulo, "Falar com a equipe NEXO" e "Como obter acesso" | Só o título e, logo abaixo, o CONTRATAR, que abre o pré-cadastro | `templates/landing.html` |
| 6.2 | Contatos e CNPJ pendentes | Rodapé de ponta a ponta com o e-mail de suporte confirmado (`suportenexocolab@gmail.com`), o WhatsApp oficial (só com número configurado), o Instagram, o crédito do desenvolvedor e o CNPJ só com `CNPJ_EMPRESA` válida. Os documentos legais entram no marcador `{# LINKS_LEGAIS #}` quando as páginas públicas existirem | `templates/landing.html`; `config.py` |

Continuam valendo: nada de preço inventado, depoimento fictício ou texto jurídico escrito pela equipe técnica;
sem número de WhatsApp, a página não menciona o WhatsApp; CDN só com versão exata e SRI; CSP sem script inline
sem nonce. A CSP da landing passou a liberar só a origem do microsserviço do Raio-X no `connect-src`
(`seguranca_http.py`).
