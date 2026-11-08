"""Assuntos D e E — empresas, cadastros, status da conta e planos.

Fontes: models.py (Empresa, Plano, Segmento, Usuario), blueprints/admin.py
(empresa_nova, empresa_editar, analise_nova, lixeira), seed.py,
templates/cliente/dashboard.html.

Gestão contratual (contract_policy.py): empresas no regime contratual, com
a conta suspensa, consultam só análises de períodos efetivamente pagos, por
prazo limitado, e exportam PDF por prazo próprio; os prazos aparecem no aviso
da Visão Geral. Empresas ainda no regime legado mantêm o comportamento
anterior. Os textos não citam prazos nem valores comerciais (regra editorial).
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.empresas.cadastro",
        titulo="Como uma empresa entra no NEXO?",
        categoria="Empresa e cadastro",
        resumo=(
            "A empresa é cadastrada pela equipe NEXO, com CNPJ, razão social, segmento, plano e um usuário "
            "CLIENTE inicial. O cliente recebe por e-mail o link para definir a senha."
        ),
        explicacao=(
            "O caminho começa fora do portal, com o contato comercial e uma reunião de alinhamento para "
            "definir a frequência de análise e o plano. Em seguida a equipe faz o cadastro na área "
            "administrativa. Não existe formulário de autocadastro."
        ),
        passos=(
            "Contato com a equipe NEXO e reunião de alinhamento.",
            "A equipe cadastra a empresa (CNPJ, razão social, nome fantasia, segmento, plano, e-mail e "
            "telefone de contato, data de contratação).",
            "A equipe cria o usuário CLIENTE vinculado à empresa; o e-mail de boas-vindas traz o link de "
            "definição de senha.",
            "A equipe abre o primeiro período de análise e você passa a enviar os relatórios.",
        ),
        perguntas=(
            "Como cadastro minha empresa?", "Quem cadastra a empresa?", "Como é feito o cadastro?",
            "Posso cadastrar minha empresa sozinho?", "O que a equipe cadastra?", "Como entro como cliente?",
        ),
        termos="empresa cadastro cadastrar cnpj razao social nome fantasia contratacao equipe",
        relacionados=("nexo.primeiros_passos.como_recebo_acesso", "nexo.empresas.usuarios", "nexo.planos.quais"),
        fontes=("blueprints/admin.py", "models.py", "templates/como_aderir.html"),
    ),
    registro(
        id="nexo.empresas.dados_cadastrais",
        titulo="Quais informações da minha empresa posso consultar no portal?",
        categoria="Empresa e cadastro",
        resumo=(
            "Na Visão Geral aparecem a razão social, o CNPJ, o plano atual e o status da conta. Os demais "
            "dados cadastrais (segmento, e-mail de contato, data de contratação) ficam com a equipe NEXO."
        ),
        explicacao=(
            "Em Configurações você altera o nome do responsável, o telefone de contato e o e-mail de acesso. Se "
            "outro dado estiver errado, abra um chamado em Suporte informando a correção."
        ),
        perguntas=(
            "Onde vejo os dados da minha empresa?", "Onde aparece meu CNPJ?", "Como vejo meu cadastro?",
            "Onde consulto meus dados cadastrais?", "Tem página de perfil?",
        ),
        termos="dados cadastrais empresa cnpj razao social plano status consultar perfil",
        relacionados=("nexo.empresas.alterar_cadastro", "nexo.dashboard.informacoes_empresa"),
        destino="dashboard",
        fontes=("templates/cliente/dashboard.html", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.empresas.alterar_cadastro",
        titulo="Como altero os dados da minha empresa ou do meu usuário?",
        categoria="Empresa e cadastro",
        tipo="procedimento",
        resumo=(
            "O nome do responsável, o telefone de contato e o e-mail de acesso você altera em Configurações. Os "
            "demais dados são alterados pela equipe NEXO: abra um chamado em Suporte descrevendo o que precisa "
            "mudar. O CNPJ não pode ser alterado depois do cadastro."
        ),
        passos=(
            "Abra Suporte no menu lateral e clique em Novo chamado.",
            "Escolha um assunto claro (por exemplo, “Atualizar e-mail de contato”) e a categoria.",
            "Descreva a alteração desejada e envie. A equipe responde no próprio chamado.",
        ),
        perguntas=(
            "Como mudo o e-mail da empresa?", "Como altero o e-mail de contato?", "Como altero o nome fantasia?", "Como atualizo meu cadastro?",
            "Posso alterar o CNPJ?", "Como corrijo dados cadastrais?",
        ),
        termos="alterar atualizar corrigir cadastro email telefone nome fantasia cnpj mudar",
        relacionados=("nexo.suporte.abrir_chamado",),
        destino="suporte",
        fontes=("blueprints/admin.py", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.empresas.segmento",
        titulo="O que é o segmento da empresa?",
        categoria="Empresa e cadastro",
        resumo=(
            "Segmento é a classificação do ramo da loja usada no cadastro, como loja de tintas, material de "
            "construção ou ferragem. Ele é definido pela equipe NEXO e não altera os cálculos dos indicadores."
        ),
        perguntas=(
            "O que significa segmento?", "Quais segmentos existem?", "Para que serve o segmento no cadastro?",
        ),
        termos="segmento ramo classificacao loja tintas construcao ferragem",
        relacionados=("nexo.visao_geral.para_quem",),
        fontes=("models.py", "seed.py"),
    ),
    registro(
        id="nexo.empresas.usuarios",
        titulo="Qual é a diferença entre o cadastro da empresa e o cadastro de usuários?",
        categoria="Empresa e cadastro",
        resumo=(
            "A empresa é a conta cliente (CNPJ, plano, status, análises). O usuário é a pessoa que entra no "
            "portal com e-mail e senha; todo usuário CLIENTE pertence a uma única empresa."
        ),
        explicacao=(
            "As análises, os chamados e as notificações são ligados à empresa e aos seus usuários. O cadastro "
            "feito pela equipe NEXO cria um único usuário CLIENTE por empresa. Hoje o portal não tem função "
            "para adicionar outros usuários à mesma empresa; se você precisar de mais um acesso, consulte a "
            "equipe NEXO pelo Suporte. O status da conta é da empresa; O status da conta é da empresa; o acesso "
            "de cada usuário tem um indicador próprio (ativo/inativo) que impede o login quando inativo, mas "
            "esse indicador não é alterado por nenhuma tela do portal."
        ),
        perguntas=(
            "Posso ter mais de um usuário?", "Como adiciono outro usuário?", "Qual a relação entre usuário e empresa?",
            "Meu sócio pode ter acesso?", "Como vinculo um usuário à empresa?",
        ),
        termos="usuario empresa vinculo cadastro outro usuario socio funcionario acesso",
        relacionados=("nexo.empresas.cadastro", "nexo.acesso.perfis"),
        fontes=("models.py", "blueprints/admin.py"),
    ),
    registro(
        id="nexo.empresas.status_conta",
        titulo="O que significa conta ativa, suspensa ou cancelada?",
        categoria="Empresa e cadastro",
        resumo=(
            "ATIVA é a situação normal. SUSPENSA e CANCELADA são definidas pela equipe NEXO conforme o "
            "contrato. Com a conta SUSPENSA a equipe não abre novas análises; o histórico publicado continua "
            "acessível, com limites de período e de prazo quando a empresa já está sob as regras "
            "contratuais. Com a conta CANCELADA os usuários da empresa não conseguem mais entrar "
            "no portal e o login mostra a tela de conta encerrada."
        ),
        explicacao=(
            "O status aparece na Visão Geral, ao lado do plano. Com a conta suspensa, o portal exibe um aviso "
            "no topo da Visão Geral. Para regularizar a situação, fale com a equipe pelo Suporte. O status da "
            "conta pertence à empresa. Quando a conta é cancelada, as sessões abertas são encerradas e o login "
            "é recusado até a equipe reativar a conta; depois da reativação é preciso entrar de novo. Além do "
            "status da empresa, cada usuário tem um indicador próprio (ativo/inativo), que também bloqueia o "
            "login quando inativo; nenhuma tela do portal altera esse indicador."
        ),
        detalhes=(
            "Regra implementada: a abertura de uma nova análise exige empresa ATIVA. Quando a empresa já está "
            "sob as regras contratuais, a suspensão libera só as análises de períodos efetivamente pagos, por "
            "prazo limitado, e a exportação de PDF tem prazo próprio; os prazos aparecem no aviso da Visão "
            "Geral. Terminado o prazo da suspensão, ou com a conta CANCELADA, login, sessões, PDFs, "
            "notificações e conexões em tempo real dos usuários da empresa ficam bloqueados."
        ),
        perguntas=(
            "O que é conta suspensa?", "O que é conta cancelada?", "Quais são os status da conta?",
            "O que muda com a conta suspensa?", "Minha conta está cancelada, o que acontece?",
            "O que significa status da conta?",
        ),
        termos="status conta ativa suspensa cancelada situacao empresa bloqueio regularizar",
        relacionados=("nexo.empresas.conta_suspensa", "nexo.admin.status_contas"),
        destino="dashboard",
        fontes=("models.py", "blueprints/admin.py", "templates/cliente/dashboard.html", "session_security.py",
                "company_security.py", "notifications.py", "contract_policy.py"),
    ),
    registro(
        id="nexo.empresas.conta_suspensa",
        titulo="Minha conta está suspensa. O que posso fazer?",
        categoria="Empresa e cadastro",
        resumo=(
            "Com a conta suspensa a equipe NEXO não abre novos períodos de análise. Se a sua empresa já está "
            "sob as regras contratuais, o aviso no topo da Visão Geral informa até quando você pode consultar "
            "o histórico dos períodos pagos e exportar PDFs; caso contrário, o histórico publicado continua "
            "acessível. Para regularizar, fale com a equipe pelo Suporte."
        ),
        perguntas=(
            "Conta suspensa, e agora?", "Por que minha conta foi suspensa?", "Como regularizo a conta suspensa?",
            "Posso enviar relatórios com a conta suspensa?",
        ),
        termos="suspensa suspensao conta bloqueada entregas bloqueadas regularizar",
        relacionados=("nexo.empresas.status_conta", "nexo.suporte.abrir_chamado"),
        destino="suporte",
        fontes=("templates/cliente/dashboard.html", "blueprints/admin.py", "contract_policy.py"),
    ),
    registro(
        id="nexo.planos.quais",
        titulo="Quais são os planos do NEXO?",
        categoria="Planos",
        resumo=(
            "O NEXO tem três planos: BRONZE, PRATA e OURO. O seu plano atual aparece na Visão Geral e é "
            "definido no cadastro pela equipe NEXO. Projetos sob medida para redes (Custom / Enterprise) "
            "são combinados diretamente com a equipe."
        ),
        explicacao=(
            "Os planos BRONZE e PRATA trabalham com análises mensais; o plano OURO é o único que permite "
            "análises quinzenais (duas por mês). Condições comerciais, valores e prazos de cobrança são "
            "tratados diretamente com a equipe NEXO, fora do portal."
        ),
        perguntas=("Quais planos existem?",
                   "Que planos o NEXO oferece?",
                   "O que é o plano Bronze?",
                   "O que é o plano Prata?",
                   "O que é o plano Ouro?",
                   "Quais são os tipos de plano?",
                   "Quais são os planos?",
                   "Quais planos o NEXO tem?"),
        termos="planos plano bronze prata ouro tipos assinatura",
        relacionados=("nexo.planos.diferenca", "nexo.planos.meu_plano"),
        destino="dashboard",
        fontes=("models.py", "seed.py", "blueprints/admin.py"),
    ),
    registro(
        id="nexo.planos.diferenca",
        titulo="Qual é a diferença entre os planos Bronze, Prata e Ouro?",
        categoria="Planos",
        resumo=(
            "O portal aplica a frequência e os recursos de cada nível. Bronze e Prata recebem análises mensais; "
            "Ouro pode receber análises quinzenais. O Raio-X do PDV vale para todos, e a Auditoria de custos e a "
            "margem bruta estimada começam no Prata. A página Planos do portal mostra o que cada nível libera; "
            "o atendimento e as condições são combinados com a equipe NEXO."
        ),
        explicacao=(
            "No cadastro, cada plano traz descrições de nível de entrega, de dashboard e de atendimento, mas "
            "o que o sistema efetivamente verifica ao abrir uma análise é o tipo: uma análise QUINZENAL só pode "
            "ser criada para empresas do plano OURO. O plano vigente no momento em que a análise é aberta fica "
            "registrado nela, então uma mudança de plano não altera análises anteriores."
        ),
        detalhes=(
            "O NexoBot não informa preços, descontos, datas de cobrança ou condições contratuais: essas "
            "informações não estão no portal do cliente e devem ser confirmadas com a equipe NEXO."
        ),
        perguntas=(
            "Qual a diferença entre os planos?", "Bronze ou Ouro, qual escolher?", "O que muda de um plano para outro?",
            "Quanto custa cada plano?", "O plano Ouro tem o quê a mais?", "Por que meu plano não permite quinzenal?",
            "Comparativo dos planos.",
        ),
        termos="diferenca planos comparar mensal quinzenal ouro prata bronze preco valor custo regra",
        relacionados=("nexo.analises.mensal_quinzenal", "nexo.planos.meu_plano"),
        fontes=("blueprints/admin.py", "models.py", "seed.py", "entitlements.py", "templates/onboarding/planos.html"),
    ),
    registro(
        id="nexo.planos.meu_plano",
        titulo="Como vejo o meu plano e como mudo de plano?",
        categoria="Planos",
        tipo="procedimento",
        resumo=(
            "O plano aparece na Visão Geral, no cartão da empresa. A troca de plano é feita pela equipe NEXO: "
            "na página Planos, o pedido abre um chamado com o plano escolhido; também dá para abrir o chamado "
            "em Suporte ou falar pelo canal combinado."
        ),
        perguntas=(
            "Qual é o meu plano?", "Onde vejo meu plano?", "Como troco de plano?", "Quero mudar para o plano Ouro.",
            "Como faço upgrade de plano?",
        ),
        termos="meu plano ver trocar mudar upgrade downgrade onde aparece",
        relacionados=("nexo.planos.quais", "nexo.suporte.abrir_chamado"),
        destino="dashboard",
        fontes=("templates/cliente/dashboard.html", "blueprints/admin.py"),
    ),
    registro(
        id="nexo.planos.quem_controla",
        titulo="Quem controla os planos?",
        categoria="Planos",
        resumo=(
            "A equipe NEXO. O plano é definido no cadastro da empresa e pode ser alterado pela equipe na área "
            "administrativa; o cliente apenas consulta o plano atual na Visão Geral."
        ),
        perguntas=(
            "Quem define o plano?", "Quem gerencia os planos?", "Posso mudar meu plano sozinho?",
        ),
        termos="controla gerencia define plano equipe admin",
        relacionados=("nexo.planos.meu_plano", "nexo.admin.planos"),
        fontes=("blueprints/admin.py",),
    ),
)
