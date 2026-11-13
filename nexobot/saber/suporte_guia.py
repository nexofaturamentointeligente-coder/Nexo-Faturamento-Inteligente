"""Assuntos O e P — Central de Suporte (chamados) e Guia (Base de Conhecimento).

Fontes: blueprints/cliente.py (tickets, ticket_detalhe, CATEGORIAS_TICKET, guia),
blueprints/admin.py (tickets, ticket_detalhe, guia CMS), models.py
(ChamadoSuporte, MensagemSuporte, GuiaTopico), templates/cliente/tickets.html,
ticket_detalhe.html e guia.html, nexobot/conhecimento.py.
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.suporte.abrir_chamado",
        titulo="Como abro um chamado para a equipe NEXO?",
        categoria="Suporte",
        tipo="procedimento",
        resumo=(
            "Em Suporte, clique em “Novo chamado”, informe um assunto, escolha a categoria (Financeiro, Dúvida "
            "Técnica ou Erro de Integração), descreva a sua dúvida e envie. A equipe NEXO é avisada e responde "
            "dentro do próprio chamado."
        ),
        passos=(
            "No menu lateral, abra Suporte.",
            "Clique em “Novo chamado”.",
            "Preencha o assunto (um resumo curto, até 160 caracteres).",
            "Escolha a categoria: Financeiro, Dúvida Técnica ou Erro de Integração.",
            "Descreva a dúvida ou o problema com detalhes (período, arquivo, mensagem de erro).",
            "Clique em “Abrir chamado”. Você é levado à conversa do chamado, onde acompanha as respostas.",
        ),
        perguntas=(
            "Como abro um chamado?", "Como abro um ticket?", "Como peço ajuda para a equipe?",
            "Quero abrir um chamado.", "Como registro um problema?", "Como crio um chamado de suporte?",
            "Onde abro chamado?",
        ),
        termos="abrir chamado ticket novo chamado suporte pedir ajuda registrar problema criar",
        relacionados=("nexo.suporte.assunto_categoria", "nexo.suporte.acompanhar", "nexo.suporte.bot_vs_humano"),
        proximo="nexo.suporte.acompanhar",
        destino="suporte",
        fontes=("blueprints/cliente.py", "templates/cliente/tickets.html"),
    ),
    registro(
        id="nexo.suporte.assunto_categoria",
        titulo="Como preencho o assunto e qual categoria escolho no chamado?",
        categoria="Suporte",
        resumo=(
            "O assunto é o título do chamado: resuma o tema em poucas palavras. As categorias são três: "
            "Financeiro (plano, contrato, cobrança), Dúvida Técnica (uso do portal, interpretação de "
            "indicadores) e Erro de Integração (problemas com arquivos, envio ou processamento)."
        ),
        perguntas=(
            "Quais categorias existem no chamado?", "Que categoria escolho?", "O que é erro de integração?",
            "O que é dúvida técnica?", "Como escrevo o assunto do chamado?",
        ),
        termos="assunto categoria categorias financeiro duvida tecnica erro integracao escolher preencher",
        relacionados=("nexo.suporte.abrir_chamado",),
        destino="suporte",
        fontes=("blueprints/cliente.py", "models.py", "templates/cliente/tickets.html"),
    ),
    registro(
        id="nexo.suporte.enviar_mensagem",
        titulo="Como envio uma mensagem em um chamado já aberto?",
        categoria="Suporte",
        tipo="procedimento",
        resumo=(
            "Abra o chamado em Suporte, escreva no campo de mensagem no fim da conversa e clique em Enviar. "
            "A equipe NEXO recebe um aviso. Suas mensagens aparecem à direita e as da equipe, à esquerda."
        ),
        perguntas=(
            "Como respondo no chamado?", "Como mando outra mensagem no ticket?", "Como continuo a conversa do chamado?",
        ),
        termos="enviar mensagem responder chamado conversa continuar ticket",
        relacionados=("nexo.suporte.acompanhar", "nexo.suporte.reabrir"),
        destino="suporte",
        fontes=("blueprints/cliente.py", "templates/cliente/ticket_detalhe.html"),
    ),
    registro(
        id="nexo.suporte.acompanhar",
        titulo="Como acompanho as respostas dos meus chamados?",
        categoria="Suporte",
        resumo=(
            "Em Suporte você vê os seus chamados e as respostas da equipe NEXO. Um chamado resolvido pode ser "
            "reaberto: basta enviar uma nova mensagem nele."
        ),
        explicacao=(
            "A lista mostra número, assunto, categoria, status e data de abertura; clique em “Abrir” para ver a "
            "conversa completa. Quando a equipe responde ou resolve, você recebe um aviso no sininho."
        ),
        perguntas=(
            "Como acompanho uma resposta?", "Onde vejo a resposta do meu chamado?", "Como vejo meus tickets?",
            "Como sei se responderam?", "Onde ficam meus chamados?",
        ),
        termos="chamado chamados ticket tickets acompanhar status resposta respondido resolvido reabrir aberto atendimento",
        relacionados=("nexo.suporte.status_chamado", "nexo.notificacoes.tickets"),
        destino="suporte",
        fontes=("blueprints/cliente.py", "templates/cliente/tickets.html"),
    ),
    registro(
        id="nexo.suporte.status_chamado",
        titulo="O que significam os status do chamado?",
        categoria="Suporte",
        resumo=(
            "Aberto: criado e ainda sem resposta da equipe. Em atendimento: a equipe respondeu ou está "
            "tratando. Resolvido: a equipe encerrou o atendimento; você pode reabrir enviando uma nova mensagem."
        ),
        explicacao=(
            "Quem muda o status é a equipe NEXO. A primeira resposta da equipe leva o chamado para Em "
            "atendimento; marcar como Resolvido registra a data de fechamento e envia um aviso (e e-mail, "
            "quando ativo). Se você responder a um chamado resolvido, ele volta automaticamente para Em "
            "atendimento."
        ),
        perguntas=(
            "O que significa chamado em atendimento?", "O que é chamado resolvido?", "Quais são os status do chamado?",
            "O que quer dizer aberto no ticket?",
        ),
        termos="status chamado aberto em atendimento resolvido significa estados ticket",
        relacionados=("nexo.suporte.resolucao", "nexo.suporte.reabrir"),
        destino="suporte",
        fontes=("models.py", "blueprints/admin.py", "blueprints/cliente.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.suporte.resolucao",
        titulo="Como funciona a resolução de um chamado?",
        categoria="Suporte",
        resumo=(
            "Quando a equipe NEXO considera a dúvida atendida, marca o chamado como Resolvido: a conversa "
            "mostra um aviso verde, você recebe uma notificação (e e-mail, quando ativo) e o chamado fica "
            "guardado na sua lista para consulta."
        ),
        perguntas=(
            "Quem resolve o chamado?", "Posso marcar meu chamado como resolvido?", "O que acontece quando resolvem meu chamado?",
        ),
        termos="resolucao resolver resolvido encerrar fechar chamado quem",
        relacionados=("nexo.suporte.status_chamado", "nexo.suporte.reabrir"),
        destino="suporte",
        fontes=("blueprints/admin.py", "templates/cliente/ticket_detalhe.html"),
    ),
    registro(
        id="nexo.suporte.reabrir",
        titulo="Quando e como posso reabrir um chamado?",
        categoria="Suporte",
        tipo="procedimento",
        resumo=(
            "A qualquer momento depois de resolvido: abra o chamado em Suporte e envie uma nova mensagem. O "
            "status volta para Em atendimento e a equipe NEXO é avisada. Não há botão separado de reabertura."
        ),
        perguntas=(
            "Como reabro um chamado?", "Posso reabrir um ticket resolvido?", "Meu problema voltou, abro outro chamado?",
        ),
        termos="reabrir reabertura chamado resolvido nova mensagem voltar atendimento",
        relacionados=("nexo.suporte.status_chamado", "nexo.suporte.enviar_mensagem"),
        destino="suporte",
        fontes=("blueprints/cliente.py", "templates/cliente/ticket_detalhe.html"),
    ),
    registro(
        id="nexo.suporte.falar_com_equipe",
        titulo="Como falo com a equipe NEXO?",
        categoria="Suporte",
        tipo="roteiro",
        resumo=(
            "Pelo portal, o canal é a página Suporte: abra um chamado descrevendo a dúvida e acompanhe a "
            "resposta na própria conversa. Para assuntos comerciais ou se não conseguir entrar no portal, use "
            "o WhatsApp comercial, quando a equipe o publica (o botão aparece no card Contato NEXO, na página "
            "Suporte e em Como aderir), ou o canal externo combinado no onboarding."
        ),
        passos=(
            "Abra Suporte no menu lateral e clique em “Novo chamado”.",
            "Informe assunto e categoria e descreva o que precisa.",
            "Envie e aguarde: a equipe responde no chamado e você recebe um aviso no sininho.",
        ),
        perguntas=(
            "Quero falar com alguém.", "Como falo com um atendente?", "Quero falar com uma pessoa.",
            "Como entro em contato com a equipe?", "Tem atendimento humano?", "Quero falar com o consultor.",
            "Como falo com a NEXO?",
        ),
        termos="falar equipe atendente humano pessoa contato consultor atendimento conversar",
        relacionados=("nexo.suporte.abrir_chamado", "nexo.suporte.bot_vs_humano"),
        proximo="nexo.suporte.abrir_chamado",
        destino="suporte",
        fontes=("blueprints/cliente.py", "templates/cliente/dashboard.html"),
    ),
    registro(
        id="nexo.suporte.bot_vs_humano",
        titulo="Qual é a diferença entre o NexoBot e o atendimento humano?",
        categoria="Suporte",
        resumo=(
            "O NexoBot responde na hora sobre como o portal e o NEXO funcionam e abre páginas do portal, mas "
            "não vê os seus números, não altera dados e não abre chamados. A equipe NEXO, pelo Suporte, "
            "trata do seu caso concreto: análises, arquivos, cadastro e plano."
        ),
        explicacao=(
            "Use o NexoBot para orientação imediata e para navegar; use o Suporte para pedidos que exigem ação "
            "da equipe ou conhecimento da sua situação específica. O NexoBot nunca pede senha nem dados "
            "sensíveis."
        ),
        perguntas=(
            "Qual a diferença entre o bot e o suporte?", "O NexoBot é uma pessoa?", "Quando devo abrir chamado em vez de perguntar ao bot?",
            "O bot resolve meu problema?", "Estou falando com um robô?",
        ),
        termos="nexobot bot humano atendimento diferenca robo pessoa suporte quando",
        relacionados=("nexo.suporte.bot_cria_chamado", "nexo.tecnologia.nexobot_ia"),
        fontes=("nexobot/servico.py", "blueprints/api.py"),
    ),
    registro(
        id="nexo.suporte.bot_cria_chamado",
        titulo="O NexoBot pode abrir um chamado por mim?",
        categoria="Suporte",
        tipo="regra",
        resumo=(
            "Não. O NexoBot apenas orienta e abre a página Suporte; o chamado é aberto por você, no formulário, "
            "para que o assunto e a descrição fiquem registrados em seu nome."
        ),
        perguntas=(
            "Abra um chamado para mim.", "O bot cria tickets?", "Você pode registrar meu chamado?",
        ),
        termos="bot abrir chamado por mim criar ticket automaticamente nao",
        relacionados=("nexo.suporte.abrir_chamado",),
        destino="suporte",
        fontes=("nexobot/servico.py", "nexobot/respostas.py"),
    ),
    registro(
        id="nexo.guia.o_que_e",
        titulo="O que é o Guia do portal?",
        categoria="Guia",
        resumo=(
            "O Guia é a página de tutoriais do portal: tópicos de perguntas frequentes com respostas em passo a "
            "passo, organizados por categoria e mantidas pela equipe NEXO na Base de Conhecimento. Os mesmos "
            "conteúdos alimentam as respostas do NexoBot."
        ),
        perguntas=(
            "O que é o Guia?", "Para que serve o Guia?", "O que tem no Guia?", "O que é a base de conhecimento?",
            "O que é o manual do portal?",
        ),
        termos="guia manual tutoriais perguntas frequentes faq base conhecimento wiki",
        relacionados=("nexo.guia.consultar", "nexo.guia.alimenta_bot"),
        destino="guia",
        fontes=("blueprints/cliente.py", "templates/cliente/guia.html", "models.py"),
    ),
    registro(
        id="nexo.guia.consultar",
        titulo="Como consulto os tutoriais do Guia?",
        categoria="Guia",
        tipo="procedimento",
        resumo=(
            "Abra Guia no menu lateral. Os tópicos ficam agrupados por categoria; clique no título de uma "
            "pergunta para expandir a resposta. Alguns tópicos trazem imagens ilustrativas."
        ),
        perguntas=(
            "Como uso o Guia?", "Onde fica o Guia?", "Como abro um tutorial?", "Como leio o passo a passo do Guia?",
        ),
        termos="consultar guia abrir tutorial expandir categoria imagem passo a passo",
        relacionados=("nexo.guia.categorias", "nexo.guia.passo_a_passo"),
        destino="guia",
        fontes=("templates/cliente/guia.html",),
    ),
    registro(
        id="nexo.guia.categorias",
        titulo="Como encontro as categorias e os assuntos do Guia?",
        categoria="Guia",
        resumo=(
            "Cada categoria aparece como um título na página do Guia, com as perguntas listadas logo abaixo. "
            "As categorias são definidas pela equipe NEXO ao cadastrar os tópicos, por exemplo Upload, Senhas "
            "e PDV; a lista exata depende do que está publicado no momento."
        ),
        perguntas=(
            "Quais categorias existem no Guia?", "Como acho um assunto no Guia?", "O Guia tem busca?",
        ),
        termos="categorias assuntos guia encontrar buscar lista",
        relacionados=("nexo.guia.consultar", "nexo.guia.admin_gerencia"),
        destino="guia",
        fontes=("blueprints/cliente.py", "templates/cliente/guia.html"),
    ),
    registro(
        id="nexo.guia.passo_a_passo",
        titulo="Como utilizo as instruções passo a passo do Guia?",
        categoria="Guia",
        resumo=(
            "Os tópicos costumam começar com um resumo de uma linha seguido de passos numerados (é o formato "
            "recomendado à equipe ao cadastrar), mas o texto é livre e alguns tópicos podem trazer só uma "
            "explicação. Siga a ordem dos passos quando houver, mantendo o portal aberto em outra aba se "
            "precisar executar a ação enquanto lê. Siga a ordem, mantendo o portal aberto em outra aba se "
            "precisar executar a ação enquanto lê. Aqui no NexoBot você também pode pedir “passo a passo” dos "
            "assuntos que têm procedimento (envio de relatórios, abertura de chamado, acesso etc.); para "
            "conceitos, o bot avisa que não há passo a passo e oferece explicar melhor."
        ),
        perguntas=(
            "Como sigo as instruções do Guia?", "O Guia tem passo a passo?", "Como uso os tutoriais?",
        ),
        termos="instrucoes passo a passo seguir tutorial ordem",
        relacionados=("nexo.guia.consultar",),
        destino="guia",
        fontes=("templates/admin/guia_form.html", "templates/cliente/guia.html"),
    ),
    registro(
        id="nexo.guia.admin_gerencia",
        titulo="Como a equipe NEXO gerencia o conteúdo do Guia?",
        categoria="Guia",
        resumo=(
            "Na área administrativa, no CMS do Guia (página /admin/guia, sem item no menu), a equipe cria, "
            "edita, ativa ou desativa tópicos "
            "(categoria, pergunta, resposta e imagem opcional). Só tópicos ativos aparecem no Guia e nas "
            "respostas do NexoBot; as mudanças valem imediatamente."
        ),
        perguntas=(
            "Quem escreve o Guia?", "Como o administrador atualiza o Guia?", "Posso sugerir um tópico para o Guia?",
            "Quem mantém a base de conhecimento?",
        ),
        termos="gerencia administra atualiza cms base conhecimento topicos ativo editar criar",
        relacionados=("nexo.guia.alimenta_bot", "nexo.admin.guia"),
        fontes=("blueprints/admin.py", "templates/admin/guia_form.html", "nexobot/conhecimento.py"),
    ),
    registro(
        id="nexo.guia.alimenta_bot",
        titulo="Como o Guia alimenta as respostas do NexoBot?",
        categoria="Guia",
        resumo=(
            "O NexoBot procura a sua pergunta nos tópicos ativos do Guia e na sua própria base de conhecimento "
            "sobre o NEXO. Quando um tópico do Guia responde com segurança, ele é apresentado; quando a "
            "correspondência é parcial, o bot sinaliza que encontrou algo parecido ou pede que você reformule."
        ),
        explicacao=(
            "O conteúdo do Guia é tratado como dado: o bot não executa instruções escritas nos tópicos nem "
            "abre endereços citados neles; só abre as páginas do catálogo do portal."
        ),
        perguntas=(
            "De onde o NexoBot tira as respostas?", "O bot usa o Guia?", "O NexoBot aprende com o Guia?",
            "Como o bot escolhe a resposta?",
        ),
        termos="alimenta respostas bot guia base busca origem fonte",
        relacionados=("nexo.tecnologia.nexobot_ia", "nexo.guia.o_que_e"),
        fontes=("nexobot/conhecimento.py", "nexobot/servico.py"),
    ),
    registro(
        id="nexo.guia.ajuda_adicional",
        titulo="Como obtenho ajuda adicional se o Guia não resolver?",
        categoria="Guia",
        resumo=(
            "Pergunte aqui ao NexoBot com outras palavras ou abra um chamado em Suporte: o botão “Abrir "
            "chamado” fica no topo e no fim da página do Guia."
        ),
        perguntas=(
            "Não achei no Guia, o que faço?", "O Guia não resolveu minha dúvida.", "Onde consigo mais ajuda?",
        ),
        termos="ajuda adicional nao achei nao resolveu mais ajuda chamado",
        relacionados=("nexo.suporte.abrir_chamado", "nexo.suporte.bot_vs_humano"),
        destino="suporte",
        fontes=("templates/cliente/guia.html",),
    ),
)
