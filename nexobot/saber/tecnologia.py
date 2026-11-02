"""Assunto R — arquitetura e funcionamento técnico, no nível do cliente.

Sem credenciais, nomes internos privados, segredos ou detalhes de infraestrutura
além do que a documentação pública do projeto (README, relatório técnico) já diz.

Fontes: README.md, Docs/RELATORIO_TECNICO.md, config.py, storage.py,
ai_devolutiva.py, nexobot/providers.py, nexobot/conhecimento.py, render.yaml.
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.tecnologia.python_flask",
        titulo="O NEXO é feito em Python? O que é Flask?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "Sim. O NEXO é escrito em Python e usa o Flask, um framework para aplicações web: ele recebe os "
            "seus cliques e formulários, consulta o banco de dados e monta as páginas que o navegador exibe."
        ),
        explicacao=(
            "O processamento das planilhas usa a biblioteca pandas, também em Python, e os gráficos são "
            "desenhados no navegador. O NexoBot que está falando com você também roda em Python, dentro do "
            "mesmo portal."
        ),
        perguntas=(
            "O sistema usa Python?", "O que é Flask?", "Em que linguagem o NEXO foi feito?", "Qual a tecnologia do portal?",
        ),
        termos="python flask linguagem tecnologia framework web feito",
        relacionados=("nexo.tecnologia.banco_dados", "nexo.tecnologia.processamento_dados"),
        fontes=("README.md", "Docs/RELATORIO_TECNICO.md", "app.py"),
    ),
    registro(
        id="nexo.tecnologia.banco_dados",
        titulo="Para que serve o banco de dados e o que é o Supabase?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "O banco de dados guarda empresas, usuários, análises, indicadores, Curva ABC, devolutivas, chamados, "
            "notificações, o Guia e os arquivos enviados. O NEXO usa um banco PostgreSQL hospedado no "
            "Supabase, um serviço de banco de dados em nuvem."
        ),
        explicacao=(
            "O Supabase é apenas o lugar onde o banco PostgreSQL do NEXO fica hospedado: a aplicação Flask se "
            "conecta a ele para ler e gravar informações. As vendas e compras linha a linha não são gravadas "
            "em tabelas; apenas os indicadores consolidados e o arquivo original."
        ),
        perguntas=(
            "Para que serve o banco de dados?", "O que é o Supabase?", "Onde ficam guardados os dados?",
            "Qual banco de dados o NEXO usa?", "O que é PostgreSQL?",
        ),
        termos="banco dados supabase postgresql postgres armazenamento nuvem guardados",
        relacionados=("nexo.tecnologia.onde_ficam_arquivos", "nexo.tecnologia.render"),
        fontes=("README.md", "config.py", "models.py"),
    ),
    registro(
        id="nexo.tecnologia.render",
        titulo="O que é o Render?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "O Render é o serviço de nuvem onde a aplicação do NEXO fica publicada e acessível pela internet, "
            "com conexão segura (HTTPS). É quem mantém o servidor do portal no ar."
        ),
        explicacao=(
            "Em planos gratuitos desse serviço o servidor pode hibernar após um período sem acessos; o "
            "primeiro acesso depois disso demora cerca de um minuto, e um reinício do servidor encerra as "
            "sessões abertas, exigindo novo login."
        ),
        perguntas=(
            "O que é Render?", "Onde o portal está hospedado?", "Por que o portal demora para abrir às vezes?",
            "Em que servidor o NEXO roda?",
        ),
        termos="render hospedagem servidor nuvem hospedado demora abrir hibernar https",
        relacionados=("nexo.acesso.sessao_expirada", "nexo.tecnologia.banco_dados"),
        fontes=("README.md", "render.yaml"),
    ),
    registro(
        id="nexo.tecnologia.onde_ficam_arquivos",
        titulo="Onde ficam armazenados os relatórios que eu envio?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "Dentro do próprio banco de dados do NEXO, em uma tabela de arquivos, e não no disco do servidor. "
            "Assim o arquivo sobrevive a reinícios e fica no mesmo lugar que o registro da análise que o cita."
        ),
        explicacao=(
            "Quando a equipe processa a análise, o motor faz uma cópia temporária do arquivo para a leitura e a "
            "descarta em seguida. Depois do envio, o portal não exibe nem permite baixar as planilhas: elas só "
            "são lidas pelo motor de processamento, quando a equipe NEXO aciona a análise. O NexoBot não os lê."
        ),
        perguntas=(
            "Onde ficam armazenados os relatórios?", "Meus arquivos ficam no servidor?", "Onde o NEXO guarda as planilhas?",
        ),
        termos="armazenados arquivos relatorios planilhas banco disco servidor guardados onde",
        relacionados=("nexo.uploads.dados_seguros", "nexo.tecnologia.banco_dados"),
        fontes=("storage.py", "models.py", "README.md"),
    ),
    registro(
        id="nexo.tecnologia.processamento_dados",
        titulo="O que é processamento de dados no NEXO?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "É a leitura das planilhas de Vendas e Compras pela biblioteca pandas, em um processo isolado com "
            "limites de tempo e memória, para calcular os indicadores. Ele é acionado pela equipe NEXO; não "
            "roda sozinho quando você envia o arquivo."
        ),
        perguntas=(
            "O que é processamento de dados?", "O que é pandas?", "Como os dados são processados?",
            "O processamento é feito onde?",
        ),
        termos="processamento dados pandas motor etl isolado limites",
        relacionados=("nexo.processamento.o_que_e_etl", "nexo.tecnologia.automatico"),
        fontes=("etl_processor.py", "etl_runtime.py", "README.md"),
    ),
    registro(
        id="nexo.tecnologia.graficos",
        titulo="Como os gráficos são gerados?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "Os gráficos são desenhados no seu navegador por uma biblioteca de gráficos interativos "
            "(Plotly), a partir dos indicadores já calculados e gravados. Por isso eles respondem ao passar do "
            "mouse e se ajustam ao tamanho da tela."
        ),
        perguntas=(
            "Como os gráficos são feitos?", "Os gráficos são imagens?", "Por que o gráfico não carregou?",
            "Que biblioteca de gráficos o NEXO usa?",
        ),
        termos="graficos gerados plotly navegador interativos biblioteca carregou",
        relacionados=("nexo.dashboard.graficos",),
        fontes=("templates/base.html", "templates/cliente/dashboard.html"),
    ),
    registro(
        id="nexo.tecnologia.nexobot_ia",
        titulo="O NexoBot é uma inteligência artificial generativa?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "Não no sentido de inventar respostas: o NexoBot entende a sua pergunta com regras de linguagem e "
            "busca a resposta em uma base de conhecimento verificada sobre o NEXO e nos tópicos do Guia. Ele "
            "não acessa os seus dados e não altera nada no portal: a única ação que executa é abrir páginas do "
            "portal do cliente, de uma lista fixa."
        ),
        explicacao=(
            "Opcionalmente, a equipe pode ligar um serviço externo que apenas reescreve, em outras palavras, "
            "trechos do Guia já escolhidos; mesmo assim a escolha da resposta, as páginas que podem ser "
            "abertas e as regras de segurança continuam sendo locais, e o texto reescrito é verificado antes "
            "de ser exibido. Sem esse serviço, o NexoBot funciona completamente."
        ),
        perguntas=(
            "O NexoBot é uma IA?", "Você é um ChatGPT?", "O bot usa inteligência artificial?",
            "Como o NexoBot entende minhas perguntas?", "O NexoBot inventa respostas?", "Você é uma IA generativa?",
        ),
        termos="nexobot ia inteligencia artificial generativa chatgpt entende inventa regras base",
        relacionados=("nexo.guia.alimenta_bot", "nexo.suporte.bot_vs_humano", "nexo.tecnologia.nexo_usa_ia"),
        fontes=("nexobot/servico.py", "nexobot/conhecimento.py", "nexobot/providers.py"),
    ),
    registro(
        id="nexo.tecnologia.nexo_usa_ia",
        titulo="O NEXO utiliza inteligência artificial?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "Em um ponto específico e sempre com revisão humana: a equipe pode gerar um rascunho da devolutiva "
            "com apoio de IA a partir dos indicadores. Os indicadores em si, a Curva ABC e os semáforos são "
            "cálculos determinísticos, sem IA. O NexoBot usa regras e uma base de conhecimento."
        ),
        explicacao=(
            "Quando a integração de IA não está disponível, um gerador local escreve o rascunho com os mesmos "
            "números. Em todos os casos a devolutiva é revisada e publicada por uma pessoa da equipe NEXO. O "
            "rascunho gerado por IA é verificado contra termos proibidos (lucro, margem, CMV) antes de ser "
            "aceito: se contiver algum, é descartado e o gerador local é usado em seu lugar."
        ),
        perguntas=("O NEXO usa IA?",
                   "Onde a inteligência artificial é usada no NEXO?",
                   "Os indicadores são calculados por IA?",
                   "A análise é feita por robô?",
                   "O NEXO usa inteligência artificial?",
                   "O sistema usa IA?"),
        termos="nexo ia inteligencia artificial usa onde calculos deterministicos revisao humana",
        relacionados=("nexo.devolutivas.ia", "nexo.tecnologia.nexobot_ia"),
        fontes=("ai_devolutiva.py", "insights.py", "etl_processor.py"),
    ),
    registro(
        id="nexo.tecnologia.automatico",
        titulo="Os dados são processados automaticamente quando envio o arquivo?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "Não. No envio o portal só valida formato, tamanho e integridade e guarda o arquivo. O "
            "processamento é acionado pela equipe NEXO depois da conferência, e a publicação da devolutiva é "
            "uma decisão humana."
        ),
        perguntas=(
            "O processamento é automático?", "Os dados são processados automaticamente?",
            "Quando envio, já calcula?", "O sistema processa sozinho?",
        ),
        termos="automatico automaticamente sozinho processa envio calcula manual",
        relacionados=("nexo.processamento.quem_controla", "nexo.uploads.depois_do_envio"),
        fontes=("blueprints/cliente.py", "blueprints/admin.py", "etl_uploads.py"),
    ),
    registro(
        id="nexo.tecnologia.seguranca",
        titulo="Como o portal protege os meus dados e o meu acesso?",
        categoria="Tecnologia",
        tipo="tecnico",
        resumo=(
            "Acesso por HTTPS, senha guardada de forma protegida (nunca em texto puro), sessão que termina ao "
            "fechar o navegador, isolamento por empresa (cada cliente vê só os próprios dados), proteção "
            "contra envio de formulários forjados e limite de tentativas de login."
        ),
        explicacao=(
            "Links de senha valem por 15 minutos e são de uso único. Trocar a senha em Configurações encerra as "
            "outras sessões abertas; redefinir pelo link ou ter a conta desativada encerra todas. O NexoBot não armazena a conversa no servidor, não tem acesso aos "
            "seus números e só abre páginas de uma lista fixa do portal."
        ),
        perguntas=(
            "O portal é seguro?", "Como meus dados são protegidos?", "A conexão é criptografada?",
            "Alguém pode ver meus dados?", "O NexoBot guarda a conversa?",
        ),
        termos="seguro seguranca protege criptografia https senha sessao isolamento privacidade conversa",
        relacionados=("nexo.acesso.cuidados", "nexo.acesso.paginas_restritas", "nexo.uploads.dados_seguros"),
        fontes=("config.py", "app.py", "session_security.py", "nexobot/contexto.py", "README.md"),
    ),
)
