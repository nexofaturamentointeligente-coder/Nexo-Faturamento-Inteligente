"""Assunto Q — o trabalho da equipe administrativa, explicado ao cliente.

Saber explicar não é poder executar: o NexoBot descreve estas operações para o
cliente entender o fluxo, mas nunca as executa nem abre páginas administrativas.

Fontes: blueprints/admin.py, company_security.py, models.py, templates/admin/*.html.
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.admin.o_que_faz",
        titulo="O que faz o administrador (equipe NEXO)?",
        categoria="Equipe NEXO",
        resumo=(
            "A equipe NEXO cadastra empresas e usuários, define planos, abre análises, valida e processa os "
            "relatórios, escreve e publica as devolutivas, responde chamados, mantém o Guia e controla o "
            "status das contas e a lixeira."
        ),
        explicacao=(
            "Tudo isso acontece na área administrativa, que o cliente não acessa. O painel da equipe mostra a "
            "carteira de empresas, as análises por status e a fila de homologação, com os envios dos clientes "
            "que aguardam conferência."
        ),
        perguntas=(
            "O que faz o administrador?", "O que o admin faz?", "Quais são as tarefas da equipe NEXO?",
            "O que a consultoria faz no sistema?", "Para que serve a área administrativa?",
        ),
        termos="administrador admin equipe tarefas funcoes area administrativa consultor faz",
        relacionados=("nexo.admin.cadastro_empresas", "nexo.admin.criar_analises", "nexo.admin.publicar"),
        fontes=("blueprints/admin.py", "templates/base.html"),
    ),
    registro(
        id="nexo.admin.cadastro_empresas",
        titulo="Como a equipe cadastra uma empresa?",
        categoria="Equipe NEXO",
        resumo=(
            "No formulário de nova empresa, a equipe informa CNPJ, razão social, nome fantasia, segmento, "
            "plano, e-mail e telefone de contato, data de contratação e status, e cria o usuário CLIENTE "
            "inicial. O cliente recebe o e-mail com o link de definição de senha."
        ),
        perguntas=(
            "Como o admin cadastra empresas?", "O que é preciso para cadastrar uma empresa?",
            "Como a equipe cria meu usuário?",
        ),
        termos="cadastro empresas admin formulario cnpj usuario inicial",
        relacionados=("nexo.empresas.cadastro", "nexo.admin.status_contas"),
        fontes=("blueprints/admin.py", "templates/admin/empresa_form.html"),
    ),
    registro(
        id="nexo.admin.planos",
        titulo="Como a equipe gerencia os planos?",
        categoria="Equipe NEXO",
        resumo=(
            "O plano é escolhido no cadastro da empresa. Quando a empresa tem contrato registrado, a troca de "
            "plano é feita em Detalhes → Contratos, preservando o contrato anterior. Os planos existentes "
            "(Bronze, Prata e Ouro) são registros do sistema; a regra aplicada é que análises quinzenais só "
            "podem ser abertas para o plano Ouro."
        ),
        perguntas=(
            "Como o admin muda o plano?", "Quem gerencia os planos?", "Como a equipe define o plano da empresa?",
        ),
        termos="gestao planos admin alterar plano empresa regra ouro quinzenal",
        relacionados=("nexo.planos.diferenca", "nexo.planos.quem_controla"),
        fontes=("blueprints/admin.py", "models.py"),
    ),
    registro(
        id="nexo.admin.criar_analises",
        titulo="Como a equipe cria uma análise?",
        categoria="Equipe NEXO",
        resumo=(
            "Em Análises, a equipe escolhe a empresa (que precisa estar ATIVA), o tipo (mensal ou quinzenal, "
            "este último só no plano Ouro), as datas do período e o mês/ano de referência. A análise nasce "
            "como Aguardando relatório e passa a aparecer para o cliente em Enviar Relatórios."
        ),
        perguntas=(
            "Como o administrador cria uma análise?", "Como a equipe abre um período?", "Quem cria as análises?",
        ),
        termos="criar analise admin abrir periodo empresa ativa tipo mensal quinzenal",
        relacionados=("nexo.analises.como_e_criada", "nexo.admin.nao_posso_executar"),
        fontes=("blueprints/admin.py",),
    ),
    registro(
        id="nexo.admin.validar_relatorios",
        titulo="Como a equipe recebe e valida os relatórios enviados?",
        categoria="Equipe NEXO",
        resumo=(
            "Os envios do cliente entram na fila de homologação do painel administrativo, com indicação de "
            "quais arquivos chegaram. A equipe confere os arquivos (e pode anexar ou substituir arquivos pela "
            "própria área) antes de acionar o processamento."
        ),
        perguntas=(
            "O que é a fila de homologação?", "Como a equipe valida meus arquivos?", "O admin pode enviar arquivos por mim?",
        ),
        termos="validar homologacao fila recebe confere arquivos admin",
        relacionados=("nexo.uploads.quem_valida", "nexo.admin.processar"),
        fontes=("blueprints/admin.py", "templates/admin/dashboard.html"),
    ),
    registro(
        id="nexo.admin.processar",
        titulo="Como a equipe processa uma análise?",
        categoria="Equipe NEXO",
        resumo=(
            "Com os dois arquivos presentes, a equipe clica em Processar. O motor calcula os indicadores em um "
            "processo isolado; em sucesso, os arquivos ficam Processado e a análise vai para Em análise; em "
            "falha, os arquivos ficam com Erro e a análise volta para Relatório recebido."
        ),
        perguntas=(
            "Como o admin processa?", "O que acontece quando a equipe clica em processar?",
            "Quem roda o processamento?",
        ),
        termos="processar admin botao motor indicadores sucesso falha",
        relacionados=("nexo.processamento.quem_controla", "nexo.admin.preparar_devolutiva"),
        fontes=("blueprints/admin.py", "etl_processor.py"),
    ),
    registro(
        id="nexo.admin.preparar_devolutiva",
        titulo="Como a equipe prepara a devolutiva?",
        categoria="Equipe NEXO",
        resumo=(
            "No editor da devolutiva, a equipe preenche título, resumo executivo, pontos positivos, pontos de "
            "alerta, recomendações e conclusão estratégica, podendo salvar rascunhos e gerar um rascunho "
            "assistido por IA a partir dos indicadores."
        ),
        perguntas=(
            "Como a equipe escreve a devolutiva?", "O que é o editor da devolutiva?", "Como o admin gera o texto por IA?",
        ),
        termos="preparar devolutiva editor rascunho campos ia admin",
        relacionados=("nexo.devolutivas.quem_prepara", "nexo.devolutivas.ia", "nexo.admin.publicar"),
        fontes=("blueprints/admin.py", "ai_devolutiva.py", "templates/admin/relatorio_editor.html"),
    ),
    registro(
        id="nexo.admin.publicar",
        titulo="Como a equipe publica (ou despublica) uma análise?",
        categoria="Equipe NEXO",
        resumo=(
            "No editor da devolutiva, a ação Publicar exige análise processada (Em análise) e o resumo "
            "executivo e a conclusão preenchidos; ela marca a análise como Concluído, avisa os clientes da "
            "empresa pelo sininho e por e-mail. Despublicar devolve a análise para Em análise."
        ),
        perguntas=(
            "Como o ADMIN publica uma análise?", "Como a equipe despublica?", "O que é preciso para publicar?",
            "Como a análise fica concluída?",
        ),
        termos="publicar despublicar admin concluido requisitos avisar",
        relacionados=("nexo.devolutivas.publicacao", "nexo.analises.status_concluido"),
        fontes=("blueprints/admin.py",),
    ),
    registro(
        id="nexo.admin.chamados",
        titulo="Como a equipe gerencia os chamados?",
        categoria="Equipe NEXO",
        resumo=(
            "O painel de suporte da equipe lista os chamados de todas as empresas agrupados por status. Em cada "
            "chamado a equipe responde (a primeira resposta a um chamado Aberto o leva para Em atendimento) e "
            "altera o status, inclusive para Resolvido, o que avisa o cliente. e altera o status, inclusive "
            "para Resolvido, o que avisa o cliente."
        ),
        perguntas=(
            "Como a equipe responde chamados?", "Quem atende meu chamado?", "Como o admin resolve um ticket?",
        ),
        termos="gestao chamados admin painel suporte responder status resolver",
        relacionados=("nexo.suporte.status_chamado", "nexo.suporte.resolucao"),
        fontes=("blueprints/admin.py", "templates/admin/tickets.html"),
    ),
    registro(
        id="nexo.admin.guia",
        titulo="Como a equipe administra o Guia (Base de Conhecimento)?",
        categoria="Equipe NEXO",
        resumo=(
            "No CMS do Guia (página /admin/guia da área administrativa, sem item no menu desde 28/10/2026), "
            "a equipe cria e edita tópicos com categoria, pergunta, resposta e imagem "
            "opcional, e pode ativar ou desativar cada tópico. Tópicos inativos somem do Guia e das respostas "
            "do NexoBot; a exclusão definitiva também é possível."
        ),
        explicacao=(
            "Conteúdos usados apenas para testes devem ficar inativos ou ser removidos: o NexoBot não lista "
            "categorias de teste como assuntos oficiais e sinaliza esses tópicos à equipe."
        ),
        perguntas=(
            "Como funciona a Base de Conhecimento?", "Como o admin edita o Guia?", "Quem cadastra os tópicos do Guia?",
            "Como desativar um tópico do Guia?",
        ),
        termos="base conhecimento cms admin topicos ativar desativar editar excluir guia",
        relacionados=("nexo.guia.admin_gerencia", "nexo.guia.alimenta_bot"),
        fontes=("blueprints/admin.py", "templates/admin/guia_form.html", "nexobot/higiene.py"),
    ),
    registro(
        id="nexo.admin.status_contas",
        titulo="Como a equipe controla o status das contas?",
        categoria="Equipe NEXO",
        resumo=(
            "Sem contrato registrado, a equipe define o status na edição da empresa (Ativa, Suspensa ou "
            "Cancelada); com contrato, suspensão, prorrogação, reativação e cancelamento são registrados em "
            "Detalhes → Contratos, com histórico. Apenas empresas Ativas recebem novas análises. Cancelar a "
            "conta encerra as sessões e bloqueia o login dos usuários da empresa até a reativação. A lixeira "
            "é um controle separado: suspender ou cancelar a conta não move a empresa para a lixeira, e "
            "vice-versa."
        ),
        perguntas=(
            "Quem muda o status da minha conta?", "Como a equipe suspende uma conta?", "Como reativar a conta?",
        ),
        termos="controle status contas ativa suspensa cancelada admin editar",
        relacionados=("nexo.empresas.status_conta", "nexo.admin.lixeira"),
        fontes=("blueprints/admin.py", "models.py", "company_security.py", "session_security.py"),
    ),
    registro(
        id="nexo.admin.lixeira",
        titulo="O que é a Lixeira e como funcionam as exclusões administrativas?",
        categoria="Equipe NEXO",
        resumo=(
            "A Lixeira é a exclusão em duas etapas de uma empresa: primeiro ela é movida para a lixeira (os "
            "acessos dos usuários são revogados, mas nada é apagado e a equipe pode restaurar); depois, e só a "
            "partir da lixeira, a exclusão permanente apaga a empresa com análises, arquivos, chamados, "
            "notificações e usuários."
        ),
        explicacao=(
            "Análises individuais também podem ser excluídas pela equipe, com todos os seus dados. Nenhuma "
            "exclusão é feita pelo cliente nem pelo NexoBot; o pedido é feito em Configurações (“Solicitar Exclusão "
            "de Dados”, que abre um chamado) ou pelo Suporte."
        ),
        perguntas=(
            "O que é a Lixeira?", "O que acontece quando uma empresa vai para a lixeira?",
            "A exclusão é definitiva?", "Como a equipe exclui uma análise?", "Posso excluir meus dados?",
        ),
        termos="lixeira exclusao excluir apagar restaurar permanente soft delete empresa analise",
        relacionados=("nexo.acesso.conta_desativada", "nexo.admin.nao_posso_executar"),
        fontes=("blueprints/admin.py", "company_security.py", "models.py"),
    ),
    registro(
        id="nexo.admin.nao_posso_executar",
        titulo="Posso pedir ao NexoBot para criar, excluir, publicar ou alterar algo?",
        categoria="Equipe NEXO",
        tipo="regra",
        resumo=(
            "Não. O NexoBot explica como essas operações funcionam, mas não as executa: criação de análises, "
            "exclusões, publicações, mudanças de plano e alterações de cadastro são ações da equipe NEXO. Para "
            "pedi-las, abra um chamado em Suporte. Nome, telefone e e-mail de acesso você altera em Configurações."
        ),
        explicacao=(
            "O assistente só faz duas coisas: responder perguntas e abrir páginas do portal do cliente. Ele não "
            "tem acesso aos seus números, não envia arquivos e não cria registros."
        ),
        perguntas=(
            "Crie uma análise para mim.", "Pode excluir minha análise?", "Publique meu relatório.",
            "Mude meu plano.", "Você pode alterar meu cadastro?", "O bot executa ações?",
        ),
        termos="executar acoes criar excluir publicar alterar bot nao pode permissao",
        relacionados=("nexo.suporte.abrir_chamado", "nexo.suporte.bot_vs_humano"),
        destino="suporte",
        fontes=("nexobot/servico.py", "nexobot/respostas.py", "blueprints/api.py"),
    ),
    registro(
        id="nexo.admin.area_restrita",
        titulo="Posso acessar a área administrativa?",
        categoria="Equipe NEXO",
        tipo="regra",
        resumo=(
            "Não. A área administrativa é exclusiva da equipe NEXO e fica bloqueada para usuários CLIENTE. O "
            "NexoBot também não abre páginas administrativas. O que você precisa está no portal do cliente e no "
            "Suporte."
        ),
        perguntas=(
            "Como entro na área do administrador?", "Me leve para o painel admin.", "Posso ver a área administrativa?",
        ),
        termos="area administrativa admin acesso restrito bloqueado painel",
        relacionados=("nexo.acesso.paginas_restritas", "nexo.acesso.perfis"),
        fontes=("blueprints/admin.py", "nexobot/seguranca.py"),
    ),
)
