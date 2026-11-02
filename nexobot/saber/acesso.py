"""Assunto C — login, senhas, sessão e perfis.

Fontes: blueprints/auth.py, session_security.py, app.py (guards), config.py
(sessão), templates/auth/*.html.
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.acesso.login",
        titulo="Como faço login no portal?",
        categoria="Acesso e senha",
        tipo="procedimento",
        resumo=(
            "Na tela de entrada, informe o e-mail cadastrado e a sua senha. A mesma tela serve para clientes "
            "e para a equipe NEXO: o portal reconhece o perfil pelo cadastro."
        ),
        explicacao=(
            "Se o e-mail ou a senha estiverem errados, a mensagem é genérica por segurança. Após várias "
            "tentativas seguidas em um minuto, o portal pede que você aguarde antes de tentar de novo. Contas "
            "desativadas recebem um aviso específico e precisam falar com a equipe NEXO."
        ),
        passos=(
            "Abra a tela de login do portal.",
            "Digite o e-mail cadastrado e a senha (o ícone de olho mostra ou oculta a senha).",
            "Clique em Entrar. Clientes vão para a Visão Geral.",
        ),
        perguntas=(
            "Como entro no portal?", "Como faço login?", "Como acesso minha conta?", "Onde faço o login?",
            "Como me conecto ao NEXO?", "Como entrar no sistema?",
        ),
        termos="login entrar acessar conta email senha autenticacao tela de entrada",
        relacionados=("nexo.acesso.recuperar_senha", "nexo.acesso.conta_desativada"),
        fontes=("blueprints/auth.py", "templates/auth/login.html"),
    ),
    registro(
        id="nexo.acesso.nao_consigo_entrar",
        titulo="Não consigo entrar no portal. O que faço?",
        categoria="Acesso e senha",
        tipo="procedimento",
        resumo=(
            "Confira o e-mail e a senha digitados. Se esqueceu a senha, use “Esqueci minha senha” na tela de "
            "login: enviamos um link de redefinição válido por 15 minutos. Se aparecer a mensagem de conta "
            "desativada, fale com a equipe NEXO."
        ),
        explicacao=(
            "A mensagem “E-mail ou senha incorretos” é genérica por segurança. Depois de muitas tentativas em "
            "um minuto, o portal pede para aguardar. Se a sua conta é nova e você nunca definiu a senha, o "
            "caminho também é “Esqueci minha senha”: o portal reenvia o link de ativação."
        ),
        passos=(
            "Verifique se o e-mail é o cadastrado pela equipe NEXO e se a senha está correta (use o ícone de "
            "olho para conferir).",
            "Se não lembrar a senha, clique em “Esqueci minha senha”, informe o e-mail e abra o link recebido "
            "em até 15 minutos.",
            "Se o portal informar que a conta está desativada, contate a equipe NEXO pelo canal combinado.",
            "Se o problema persistir, tente outro navegador ou dispositivo e, se possível, abra um chamado.",
        ),
        perguntas=(
            "Não consigo entrar no portal.", "Não consigo fazer login.", "O login não funciona.",
            "Aparece e-mail ou senha incorretos.", "Minha senha não funciona.", "Erro ao entrar.",
            "Não estou conseguindo acessar.", "O portal não aceita minha senha.",
        ),
        termos="entrar login logar acessar acesso conta bloqueada desativada senha esqueci consigo entro portal erro incorretos",
        relacionados=("nexo.acesso.recuperar_senha", "nexo.acesso.conta_desativada", "nexo.acesso.sessao_expirada"),
        fontes=("blueprints/auth.py", "templates/auth/login.html"),
    ),
    registro(
        id="nexo.acesso.recuperar_senha",
        titulo="Como recupero ou troco a minha senha?",
        categoria="Acesso e senha",
        tipo="procedimento",
        resumo=(
            "Logado, você troca a senha em Configurações, informando a senha atual. Se esqueceu a senha, clique "
            "em “Esqueci minha senha” na tela de login e informe o seu e-mail: você recebe um link de "
            "redefinição válido por 15 minutos."
        ),
        explicacao=(
            "Por segurança a resposta é sempre a mesma, exista ou não o e-mail no cadastro. O link só pode ser "
            "usado uma vez e dentro do prazo. A nova senha precisa ter ao menos 12 caracteres; em Configurações, "
            "também letras e números. Depois de redefinir pelo link, as sessões antigas deixam de valer e é "
            "preciso entrar de novo; em Configurações, só as outras sessões são encerradas."
        ),
        passos=(
            "Logado: abra Configurações, informe a senha atual e a nova em Alterar senha.",
            "Esqueceu a senha: saia do portal e, na tela de login, clique em “Esqueci minha senha”.",
            "Informe o e-mail cadastrado e envie.",
            "Abra o e-mail recebido e clique no link (válido por 15 minutos).",
            "Digite a nova senha (mínimo de 12 caracteres), confirme e salve.",
            "Volte ao login e entre com a nova senha.",
        ),
        perguntas=(
            "Como recupero minha senha?", "Esqueci minha senha.", "Como troco minha senha?",
            "Como altero a senha de acesso?", "Quero mudar minha senha.", "Como redefinir a senha?",
            "Perdi minha senha, e agora?", "Não lembro a senha.", "Onde troco a senha?",
            "Qual a política de senha?", "Como funciona a política de senha?",
        ),
        termos="senha recuperar esqueci trocar alterar mudar redefinir resetar link email 15 minutos",
        relacionados=("nexo.acesso.redefinir_link", "nexo.acesso.sessao_expirada"),
        destino="configuracoes",
        fontes=("blueprints/auth.py", "templates/auth/recuperar_senha.html", "emails.py"),
    ),
    registro(
        id="nexo.acesso.configuracoes",
        titulo="O que posso fazer em Configurações?",
        categoria="Acesso e senha",
        tipo="procedimento",
        resumo=(
            "Em Configurações, no menu lateral, você altera o nome do responsável, o telefone de contato, a senha "
            "e o e-mail de acesso, exporta os dados da empresa, lê e aceita os Termos de Uso e a Política de "
            "Privacidade, pede a exclusão dos dados e escolhe os avisos que quer receber."
        ),
        explicacao=(
            "Trocar a senha ou o e-mail exige a senha atual e encerra as outras sessões abertas. A exportação gera "
            "um arquivo .json com os dados da conta e da empresa, os contratos, os chamados que você abriu e as "
            "análises concluídas do seu Histórico, enquanto o prazo de exportação estiver aberto. Solicitar a exclusão abre um chamado para a equipe NEXO: nada é apagado "
            "automaticamente.\n\n"
            "Os lembretes de renovação e os alertas de chamados podem ser desligados; os avisos de segurança, não. "
            "O tema claro ou escuro é escolhido no botão do topo da tela e fica guardado no navegador."
        ),
        perguntas=(
            "Onde ficam as configurações?", "Como altero meu nome?", "Como troco meu e-mail de acesso?",
            "Como mudo o telefone de contato?", "Como mudo meu telefone?",
            "Como exporto meus dados?", "Como baixo os dados da minha empresa?", "Como peço a exclusão dos meus dados?",
            "Como funciona a LGPD no portal?", "Onde vejo os termos de uso?", "Onde vejo a política de privacidade?",
            "Como desligo os lembretes de renovação?", "Como mudo para o tema claro?", "Tem modo escuro?",
            "Como desligo os alertas de chamados?", "Como desligo os avisos de chamados?", "Como desligo as notificações?",
            "O que eu posso alterar sozinho?", "Como mudo meu celular?",
            "O que vem na exportação de dados?",
            "Como pego uma cópia dos meus dados?", "Onde vejo os termos e a política?", "Como aceito a política?",
            "Preciso aceitar a política?", "Qual a versão da política que eu aceitei?",
            "Como desativo os lembretes?", "Como paro de receber os lembretes?",
        ),
        termos="configuracoes perfil conta nome telefone celular email exportar exportacao baixar copia dados lgpd exclusao termos "
               "privacidade politica aceite aceitar tema claro escuro avisos preferencias lembretes renovacao",
        relacionados=("nexo.acesso.recuperar_senha", "nexo.empresas.alterar_cadastro"),
        destino="configuracoes",
        fontes=("blueprints/cliente.py", "configuracoes_conta.py", "templates/cliente/configuracoes.html"),
    ),
    registro(
        id="nexo.acesso.redefinir_link",
        titulo="Como funciona o link de redefinição de senha?",
        categoria="Acesso e senha",
        resumo=(
            "O link carrega um código assinado pelo portal, válido por 15 minutos e para um único uso. Se "
            "expirar ou já tiver sido usado, o portal avisa e você pode pedir um novo."
        ),
        explicacao=(
            "O código não contém a sua senha nem o seu e-mail; ele identifica a conta e a credencial atual. "
            "Qualquer alteração na conta entre o envio e o uso invalida o link. Links de ativação (primeiro "
            "acesso) e de recuperação seguem a mesma regra de prazo."
        ),
        perguntas=(
            "O link de redefinição expirou.", "Por quanto tempo o link vale?", "Link inválido ou expirado.",
            "Posso usar o mesmo link duas vezes?", "Como funciona o token de senha?",
        ),
        termos="link token redefinicao expirado invalido validade 15 minutos uso unico ativacao",
        relacionados=("nexo.acesso.recuperar_senha",),
        fontes=("blueprints/auth.py",),
    ),
    registro(
        id="nexo.acesso.sessao_expirada",
        titulo="Por que minha sessão expirou?",
        categoria="Acesso e senha",
        resumo=(
            "A sessão termina quando você fecha o navegador, quando o servidor é reiniciado, quando a senha "
            "da conta é alterada ou quando a conta é desativada. Basta entrar de novo com e-mail e senha."
        ),
        explicacao=(
            "O portal não mantém login automático: a sessão vive só enquanto o navegador está aberto e é "
            "vinculada à inicialização atual do servidor, por isso um reinício (inclusive quando o serviço "
            "hiberna por inatividade) exige novo login. Trocar a senha em Configurações encerra as outras sessões "
            "abertas; redefinir pelo link ou ter a conta desativada encerra todas. Se o NexoBot avisar que a sessão "
            "expirou, atualize a página e entre novamente."
        ),
        perguntas=(
            "Por que fui deslogado?", "Minha sessão caiu.", "O portal pediu login de novo.",
            "Por que preciso entrar toda hora?", "Sessão expirada, o que faço?", "O sistema me desconectou.",
            "Por que o login não fica salvo?",
        ),
        termos="sessao expirou expirada deslogado desconectou login novamente navegador reinicio",
        relacionados=("nexo.acesso.login", "nexo.acesso.cuidados"),
        fontes=("app.py", "session_security.py", "config.py", "static/js/nexobot.js"),
    ),
    registro(
        id="nexo.acesso.sair",
        titulo="Como saio do portal?",
        categoria="Acesso e senha",
        tipo="procedimento",
        resumo="Use o item “Sair”, no fim do menu lateral. A sessão é encerrada e a tela de login reaparece.",
        perguntas=(
            "Como faço logout?", "Onde fica o botão de sair?", "Como encerro a sessão?", "Como deslogar?",
        ),
        termos="sair logout deslogar encerrar sessao botao",
        relacionados=("nexo.acesso.sessao_expirada",),
        fontes=("blueprints/auth.py", "templates/base.html"),
    ),
    registro(
        id="nexo.acesso.conta_desativada",
        titulo="Minha conta está desativada. O que faço?",
        categoria="Acesso e senha",
        resumo=(
            "Se o login mostra “Esta conta está desativada”, o acesso foi bloqueado pela equipe NEXO (por "
            "exemplo, quando a empresa foi movida para a lixeira). Fale com a equipe pelo canal combinado no "
            "onboarding para regularizar."
        ),
        explicacao=(
            "Sem login não é possível abrir chamados pelo portal; por isso o contato precisa ser feito por "
            "fora. A reativação é uma ação administrativa e exige revisão da equipe."
        ),
        perguntas=(
            "Conta desativada.", "Apareceu que minha conta foi desativada.", "Por que minha conta foi bloqueada?",
            "Como reativo minha conta?", "Não consigo entrar: conta inativa.",
        ),
        termos="conta desativada bloqueada inativa reativar acesso negado",
        relacionados=("nexo.empresas.status_conta", "nexo.admin.lixeira"),
        fontes=("blueprints/auth.py", "session_security.py", "company_security.py"),
    ),
    registro(
        id="nexo.acesso.perfis",
        titulo="Qual é a diferença entre os perfis CLIENTE e ADMIN?",
        categoria="Acesso e senha",
        resumo=(
            "CLIENTE é o usuário de uma empresa: vê e envia apenas os dados da própria empresa. ADMIN é a "
            "equipe NEXO: cadastra empresas, abre análises, processa, publica devolutivas, responde chamados "
            "e mantém o Guia."
        ),
        explicacao=(
            "Todo usuário CLIENTE está vinculado a exatamente uma empresa; usuários ADMIN não têm empresa. "
            "As páginas do cliente ficam em uma área própria e as administrativas em outra; cada perfil é "
            "barrado na área do outro. O NexoBot atende apenas o perfil CLIENTE e nunca abre páginas "
            "administrativas."
        ),
        perguntas=(
            "O que é o perfil ADMIN?", "O que é o perfil CLIENTE?", "Qual a diferença entre cliente e administrador?",
            "Quem é admin no NEXO?", "Posso ter perfil de administrador?", "Quais tipos de usuário existem?",
        ),
        termos="perfil cliente admin administrador role papel tipo usuario diferenca",
        relacionados=("nexo.acesso.paginas_restritas", "nexo.admin.o_que_faz"),
        fontes=("models.py", "blueprints/cliente.py", "blueprints/admin.py", "blueprints/api.py"),
    ),
    registro(
        id="nexo.acesso.paginas_restritas",
        titulo="Por que algumas páginas são restritas?",
        categoria="Acesso e senha",
        resumo=(
            "Cada empresa só enxerga as próprias análises e chamados, e a área administrativa é exclusiva da "
            "equipe NEXO. Tentar abrir algo de outra empresa ou da administração resulta em acesso negado ou "
            "página não encontrada."
        ),
        explicacao=(
            "Essa separação (isolamento por empresa e por perfil) protege os dados de todos os clientes. Uma "
            "análise só abre para você se pertencer à sua empresa, estiver concluída e tiver a devolutiva "
            "publicada; antes disso ela não é acessível, mesmo que exista."
        ),
        perguntas=(
            "Por que não consigo abrir a área administrativa?", "Por que a página dá acesso negado?",
            "Por que vejo só a minha empresa?", "Posso ver dados de outras empresas?",
            "Por que uma análise dá página não encontrada?",
        ),
        termos="restrita restrito acesso negado 403 404 isolamento empresa privacidade administrativa",
        relacionados=("nexo.acesso.perfis", "nexo.tecnologia.seguranca"),
        fontes=("blueprints/cliente.py", "blueprints/admin.py"),
    ),
    registro(
        id="nexo.acesso.cuidados",
        titulo="Que cuidados devo ter com as minhas informações de acesso?",
        categoria="Acesso e senha",
        tipo="regra",
        resumo=(
            "Não compartilhe a senha, use uma senha pessoal com 12 caracteres ou mais e desconfie de pedidos de "
            "senha: nem a equipe NEXO nem o NexoBot pedem a sua senha ou códigos de acesso."
        ),
        explicacao=(
            "Links de definição e redefinição de senha valem por 15 minutos e chegam apenas pelo e-mail "
            "cadastrado. Ao usar um computador compartilhado, clique em Sair ao terminar. Se suspeitar de uso "
            "indevido, redefina a senha (isso encerra as sessões abertas) e avise a equipe pelo Suporte."
        ),
        perguntas=(
            "Posso compartilhar minha senha?", "O NexoBot pede senha?", "Como proteger meu acesso?",
            "Dicas de segurança da conta.", "Acho que alguém usou minha conta.",
        ),
        termos="cuidados seguranca senha compartilhar proteger phishing suspeita indevido",
        relacionados=("nexo.acesso.recuperar_senha", "nexo.tecnologia.seguranca"),
        fontes=("blueprints/auth.py", "nexobot/seguranca.py"),
    ),
)
