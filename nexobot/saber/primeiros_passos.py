"""Assunto B — primeiros passos de quem nunca usou o portal.

Fontes: blueprints/admin.py (empresa_nova), blueprints/auth.py (ativação, primeiro
acesso), app.py (_guard_primeiro_acesso), templates/base.html (menu),
templates/cliente/upload.html e dashboard.html.
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.primeiros_passos.roteiro",
        titulo="Como começo a usar o NEXO?",
        categoria="Primeiros passos",
        tipo="roteiro",
        resumo=(
            "Comece definindo a sua senha pelo link do e-mail de boas-vindas, entre no portal, confira a "
            "Visão Geral e, em Enviar Relatórios, anexe os arquivos de Vendas e Compras da análise aberta e "
            "informe o total de vendas. Depois acompanhe o status e aguarde o aviso de publicação."
        ),
        explicacao=(
            "Não existe autocadastro: a equipe NEXO cadastra a empresa e cria o seu usuário. Você recebe um "
            "e-mail com um link para definir a senha (válido por 15 minutos). Com a senha definida, o login é "
            "feito com e-mail e senha. A partir daí o seu trabalho no portal é enviar os relatórios de cada "
            "período e consultar os resultados publicados."
        ),
        passos=(
            "Abra o e-mail de boas-vindas e clique no link para definir a sua senha (ele vale por 15 minutos; "
            "se expirar, use “Esqueci minha senha” na tela de login para receber outro).",
            "Entre no portal com o seu e-mail e a senha definida.",
            "Na Visão Geral, confira os dados da empresa, o plano e as análises em andamento.",
            "Abra Enviar Relatórios no menu lateral e localize a análise aberta para o período.",
            "Exporte do seu PDV o relatório de Vendas e o de Compras do período (.csv ou .xlsx) e anote o total "
            "de vendas que o PDV mostra. Informe esse total no espaço de Vendas e anexe cada arquivo no seu campo.",
            "Acompanhe o status: “AGUARDANDO VALIDAÇÃO” enquanto a equipe confere; depois “PROCESSADO”.",
            "Quando a devolutiva for publicada, você recebe um aviso no sininho e a análise aparece na Visão "
            "Geral e no Histórico.",
            "Em caso de dúvida, consulte o Guia, pergunte aqui ao NexoBot ou abra um chamado em Suporte.",
        ),
        perguntas=(
            "Como começo?", "Como começo a usar o sistema?", "Por onde eu começo?", "Sou novo aqui, o que faço?",
            "Nunca usei o NEXO.", "Primeiros passos no portal.", "Como dar os primeiros passos?",
            "O que fazer primeiro?", "Como iniciar no NEXO?", "Guia rápido para iniciantes.",
        ),
        termos="comecar inicio iniciante novo primeiros passos roteiro onboarding tutorial primeira vez",
        relacionados=("nexo.uploads.primeiro_envio", "nexo.primeiros_passos.navegar_menu",
                      "nexo.primeiros_passos.acompanhar"),
        proximo="nexo.uploads.primeiro_envio",
        destino="upload",
        fontes=("blueprints/auth.py", "blueprints/admin.py", "blueprints/cliente.py", "templates/base.html"),
    ),
    registro(
        id="nexo.primeiros_passos.como_recebo_acesso",
        titulo="Como recebo o meu acesso ao portal?",
        categoria="Primeiros passos",
        tipo="procedimento",
        resumo=(
            "A equipe NEXO cadastra a sua empresa e o seu usuário; você recebe um e-mail de boas-vindas com um "
            "link seguro para definir a senha. Não há autocadastro no portal."
        ),
        explicacao=(
            "O cadastro é feito pela equipe depois do alinhamento comercial (contato, reunião e escolha do "
            "plano). O link de definição de senha vale por 15 minutos. Se ele expirar ou o e-mail não chegar, "
            "use “Esqueci minha senha” na tela de login informando o seu e-mail: enquanto a conta ainda não "
            "tiver senha definida, o portal reenvia o link de ativação."
        ),
        passos=(
            "Aguarde o e-mail de boas-vindas enviado após o cadastro pela equipe NEXO.",
            "Clique no link e defina uma senha com pelo menos 12 caracteres, confirmando-a.",
            "Volte à tela de login e entre com o e-mail cadastrado e a nova senha.",
            "Se o link expirou, abra a tela de login, clique em “Esqueci minha senha” e informe o e-mail para "
            "receber um novo link.",
        ),
        perguntas=(
            "Quem cadastra minha empresa?", "Como consigo acesso?", "Como faço meu cadastro?",
            "Não recebi o e-mail de acesso.", "Como recebo minhas credenciais?", "Como ter acesso ao portal?",
            "Posso me cadastrar sozinho?", "Como crio minha conta?", "Como aderir ao NEXO?",
        ),
        termos="acesso cadastro conta credenciais email boas vindas ativacao link autocadastro aderir",
        relacionados=("nexo.primeiros_passos.primeiro_login", "nexo.acesso.recuperar_senha",
                      "nexo.empresas.cadastro"),
        fontes=("blueprints/admin.py", "blueprints/auth.py", "emails.py", "templates/como_aderir.html"),
    ),
    registro(
        id="nexo.primeiros_passos.primeiro_login",
        titulo="Como funciona o primeiro login e por que preciso definir a senha?",
        categoria="Primeiros passos",
        tipo="procedimento",
        resumo=(
            "No primeiro acesso você define a sua própria senha: ninguém da equipe conhece a senha do cliente. "
            "Enquanto a senha pessoal não for definida, o portal bloqueia as demais páginas."
        ),
        explicacao=(
            "Contas novas são criadas sem senha utilizável; a definição acontece pelo link do e-mail de "
            "boas-vindas. Se a sua conta foi criada com uma senha temporária (contas antigas), o portal "
            "obriga a troca logo no primeiro login, antes de liberar a Visão Geral. A senha precisa ter ao "
            "menos 12 caracteres e ser confirmada."
        ),
        passos=(
            "Abra o link de definição de senha ou entre com a senha temporária, se foi esse o seu caso.",
            "Digite a nova senha (mínimo de 12 caracteres) e repita no campo de confirmação.",
            "Salve. Se você veio pelo link do e-mail, o portal volta à tela de login para você entrar com o e-mail e a nova senha; se entrou com a senha temporária, a Visão Geral e as demais páginas são liberadas na hora.",
        ),
        perguntas=(
            "Como faço o primeiro login?", "Por que preciso trocar a senha no primeiro acesso?",
            "O portal está pedindo para definir senha.", "Primeiro acesso ao NEXO.",
            "Por que não consigo ver o painel antes de trocar a senha?", "Como é o primeiro acesso?",
        ),
        termos="primeiro acesso login senha temporaria definir trocar obrigatorio bloqueio",
        relacionados=("nexo.acesso.recuperar_senha", "nexo.acesso.cuidados"),
        fontes=("blueprints/auth.py", "app.py", "templates/auth/primeiro_acesso.html"),
    ),
    registro(
        id="nexo.primeiros_passos.navegar_menu",
        titulo="Como localizo as páginas do portal?",
        categoria="Primeiros passos",
        tipo="procedimento",
        resumo=(
            "Tudo fica no menu lateral: Visão Geral, Enviar relatórios, Histórico, Guia, Suporte e Configurações. No celular, "
            "toque no botão de três linhas no canto superior esquerdo para abrir o menu."
        ),
        explicacao=(
            "No topo há o sininho de notificações e o seu nome. O item “Sair” fica no fim do menu lateral. "
            "Se preferir, peça aqui ao NexoBot: “abra o histórico” abre a página; “onde fica o upload?” "
            "explica e mostra um botão."
        ),
        perguntas=(
            "Onde ficam as páginas?", "Como navego no portal?", "Onde está o menu?", "Como abro o menu no celular?",
            "Quais itens tem o menu?", "Como encontro as funcionalidades?", "Onde fica cada coisa?",
        ),
        termos="menu lateral navegacao paginas celular botao tres linhas sininho sair localizar",
        relacionados=("nexo.visao_geral.funcionalidades", "nexo.notificacoes.sininho"),
        destino="dashboard",
        fontes=("templates/base.html", "static/css/nexo.css"),
    ),
    registro(
        id="nexo.primeiros_passos.analises_disponiveis",
        titulo="Como sei quais análises estão disponíveis para envio?",
        categoria="Primeiros passos",
        tipo="procedimento",
        resumo=(
            "Em Enviar Relatórios aparecem apenas as análises que ainda aceitam arquivos (status Aguardando "
            "relatório ou Relatório recebido). Se não houver nenhuma, a página avisa que a equipe ainda não "
            "abriu um período para a sua empresa."
        ),
        explicacao=(
            "A Visão Geral também lista as “Análises em andamento” da empresa, com período, tipo (mensal ou "
            "quinzenal) e status. Quem abre um novo período é a equipe NEXO; se você precisa de uma análise "
            "que não aparece, fale com a equipe pelo Suporte."
        ),
        perguntas=(
            "Como identifico as análises disponíveis?", "Onde vejo as análises abertas?",
            "Não aparece nenhuma análise para enviar.", "Como sei se tem análise aberta?",
            "Quais análises estão em andamento?", "Onde vejo as próximas entregas?",
        ),
        termos="analises disponiveis abertas andamento periodo aberto enviar listar",
        relacionados=("nexo.analises.status", "nexo.uploads.bloqueado"),
        destino="upload",
        fontes=("blueprints/cliente.py", "templates/cliente/upload.html", "templates/cliente/dashboard.html"),
    ),
    registro(
        id="nexo.primeiros_passos.acompanhar",
        titulo="Como acompanho as etapas da minha análise?",
        categoria="Primeiros passos",
        tipo="procedimento",
        resumo=(
            "Acompanhe pelo status da análise (Aguardando relatório, Relatório recebido, Em análise, Concluído), "
            "pelo status de cada arquivo em Enviar Relatórios e pelos avisos do sininho."
        ),
        explicacao=(
            "Os arquivos enviados ficam “AGUARDANDO VALIDAÇÃO” até a equipe processar; depois ficam "
            "“PROCESSADO” ou “ERRO”. Quando o processamento termina com sucesso, você recebe o aviso “Recebemos "
            "e processamos seus relatórios” ou, se os totais não baterem, o aviso de diferença; se o arquivo ficar com status “ERRO”, não há aviso no sininho — o "
            "status aparece apenas em Enviar Relatórios, e você pode reenviar o arquivo.; quando a devolutiva é "
            "publicada, recebe outro aviso e a análise passa a aparecer na Visão Geral e no Histórico."
        ),
        perguntas=(
            "Como acompanho o andamento?", "Em que etapa está minha análise?", "Como vejo o progresso?",
            "Como saber o status da análise?", "Como acompanho minha entrega?",
        ),
        termos="acompanhar etapas andamento progresso status entrega situacao",
        relacionados=("nexo.analises.status", "nexo.uploads.status", "nexo.notificacoes.eventos"),
        destino="upload",
        fontes=("templates/cliente/upload.html", "templates/cliente/dashboard.html", "blueprints/admin.py"),
    ),
    registro(
        id="nexo.primeiros_passos.saber_publicada",
        titulo="Como sei quando há uma análise publicada?",
        categoria="Primeiros passos",
        tipo="procedimento",
        resumo=(
            "Você recebe uma notificação no sininho (“Seu Relatório Estratégico de MM/AAAA já está "
            "disponível”) e, com o envio de e-mails ativo, um e-mail. A análise aparece na Visão Geral e no "
            "Histórico com o status Concluído."
        ),
        perguntas=(
            "Como sei que a análise foi publicada?", "Vou ser avisado quando a análise ficar pronta?",
            "Como descubro se tem resultado novo?", "Onde vejo se saiu a devolutiva?",
        ),
        termos="publicada pronta disponivel aviso notificacao email resultado novo",
        relacionados=("nexo.devolutivas.publicacao", "nexo.historico.abrir"),
        destino="historico",
        fontes=("blueprints/admin.py", "notifications.py", "emails.py"),
    ),
)
