"""Assunto N — notificações (sininho) e e-mails transacionais.

Fontes: notifications.py, blueprints/notificacoes.py, realtime.py, emails.py,
config.py (MAIL_ATIVO, BREVO_API_KEY), templates/base.html (sininho),
blueprints/admin.py e blueprints/cliente.py (gatilhos).
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.notificacoes.sininho",
        titulo="Para que serve o sininho e como recebo as notificações?",
        categoria="Notificações",
        resumo=(
            "O sininho, no topo da página, reúne os avisos do portal: recebimento e processamento de "
            "relatórios, publicação de análises, movimentações nos seus chamados e o lembrete de renovação do "
            "contrato, quando a equipe NEXO o dispara. Ele mostra a contagem de "
            "não lidas e as 5 mais recentes; clique em um aviso para abrir o item relacionado."
        ),
        explicacao=(
            "Os avisos chegam em tempo real enquanto o portal está aberto (aparece também um alerta no canto "
            "da tela) e ficam guardados para quando você entrar de novo. “Marcar todas como lidas” zera a "
            "contagem. Cada aviso traz um selo: Nova Análise, Resolvido, Aberto, Contrato ou Aviso."
        ),
        perguntas=(
            "Para que serve o sininho?", "Como recebo as notificações?", "Onde vejo meus avisos?",
            "O que é o sino no topo?", "Como funcionam as notificações?", "O que significa o número no sininho?",
        ),
        termos="notificacao notificacoes aviso avisos sininho sino alerta alertas receber avisado contagem",
        relacionados=("nexo.notificacoes.eventos", "nexo.notificacoes.nao_lidas", "nexo.notificacoes.email"),
        fontes=("templates/base.html", "notifications.py", "realtime.py"),
    ),
    registro(
        id="nexo.notificacoes.eventos",
        titulo="Quais acontecimentos geram avisos no sininho?",
        categoria="Notificações",
        resumo=(
            "Para o cliente: relatórios processados pela equipe (“Recebemos e processamos seus relatórios” ou, se "
            "o total calculado não bater com o informado, “Encontramos uma diferença no total de vendas informado”), "
            "análise publicada (“Seu Relatório Estratégico de MM/AAAA já está disponível”), resposta da "
            "equipe em um chamado, chamado marcado como resolvido e o lembrete de renovação do contrato, quando "
            "a equipe NEXO o dispara."
        ),
        explicacao=(
            "A equipe NEXO, por sua vez, é avisada quando você envia um relatório, abre um chamado ou responde "
            "em um chamado. Os avisos só são enviados depois que a ação foi confirmada no sistema."
        ),
        perguntas=(
            "Quando recebo uma notificação?", "Que avisos o portal envia?", "Quais eventos geram notificação?",
            "Serei avisado quando responderem meu chamado?", "O portal avisa quando a equipe processa?",
        ),
        termos="eventos acontecimentos gatilhos avisos quando recebo notificacao lista",
        relacionados=("nexo.notificacoes.recebimento_relatorio", "nexo.notificacoes.tickets",
                      "nexo.devolutivas.publicacao"),
        fontes=("blueprints/admin.py", "blueprints/cliente.py", "notifications.py"),
    ),
    registro(
        id="nexo.notificacoes.recebimento_relatorio",
        titulo="Como acompanho o recebimento dos meus relatórios?",
        categoria="Notificações",
        resumo=(
            "O envio em si é confirmado na hora pela mensagem na página e pelo status “AGUARDANDO VALIDAÇÃO” no "
            "cartão do arquivo. O aviso no sininho vem na etapa seguinte, quando a equipe processa os "
            "relatórios (“Recebemos e processamos seus relatórios” ou, se os totais não baterem, o aviso de diferença)."
        ),
        perguntas=(
            "Recebo aviso quando envio o arquivo?", "Como sei que a equipe recebeu meu relatório?",
            "Tem notificação de recebimento?",
        ),
        termos="recebimento relatorio confirmacao envio aviso processamos",
        relacionados=("nexo.uploads.status", "nexo.processamento.acompanhar_resultado"),
        destino="upload",
        fontes=("blueprints/cliente.py", "blueprints/admin.py", "templates/cliente/upload.html"),
    ),
    registro(
        id="nexo.notificacoes.nao_lidas",
        titulo="Como vejo as notificações não lidas e as marco como lidas?",
        categoria="Notificações",
        tipo="procedimento",
        resumo=(
            "Clique no sininho: as não lidas aparecem destacadas e a contagem fica no ícone. Clicar em um aviso "
            "o marca como lido e abre o item; o botão “Marcar todas como lidas” limpa a contagem de uma vez."
        ),
        perguntas=(
            "Como marco as notificações como lidas?", "Como limpo o sininho?", "Como vejo as não lidas?",
            "Onde ficam as notificações antigas?",
        ),
        termos="nao lidas marcar lidas limpar contagem sininho antigas",
        relacionados=("nexo.notificacoes.sininho",),
        fontes=("blueprints/notificacoes.py", "templates/base.html"),
    ),
    registro(
        id="nexo.notificacoes.tickets",
        titulo="Como funcionam os avisos de chamados?",
        categoria="Notificações",
        resumo=(
            "Quando a equipe NEXO responde o seu chamado, você recebe o aviso “A equipe NEXO respondeu seu "
            "chamado” com o assunto; quando o chamado é marcado como resolvido, recebe outro aviso e, com o "
            "e-mail ativo, uma mensagem por e-mail. Clique no aviso para abrir a conversa."
        ),
        explicacao=(
            "Você pode desligar esses alertas em Configurações. O e-mail de contato da empresa, se for outro, "
            "continua recebendo o aviso de chamado resolvido."
        ),
        perguntas=(
            "Vou ser avisado da resposta do chamado?", "Como sei que responderam meu ticket?",
            "Recebo e-mail quando o chamado é resolvido?",
        ),
        termos="avisos chamados ticket resposta resolvido notificacao email",
        relacionados=("nexo.suporte.acompanhar", "nexo.suporte.status_chamado"),
        destino="suporte",
        fontes=("blueprints/admin.py", "emails.py"),
    ),
    registro(
        id="nexo.notificacoes.email",
        titulo="Que e-mails o NEXO envia e qual é o papel deles?",
        categoria="Notificações",
        resumo=(
            "E-mails transacionais: boas-vindas com o link de definição de senha, link de recuperação de senha, "
            "avisos de senha e de e-mail alterados, aviso de análise publicada, aviso de chamado resolvido e "
            "lembrete de renovação do contrato. Eles complementam o sininho; o envio depende da integração de "
            "e-mail configurada no servidor."
        ),
        explicacao=(
            "Quando a integração não está configurada ou o serviço de e-mail está indisponível, o portal "
            "continua funcionando e os avisos do sininho continuam chegando, mas o e-mail não é entregue. "
            "A aceitação pelo serviço de e-mail também não garante a entrega na sua caixa de entrada: verifique "
            "a pasta de spam e, se precisar de um link de senha, peça outro em “Esqueci minha senha”."
        ),
        perguntas=("Quais e-mails o NEXO envia?",
                   "Não recebi o e-mail.",
                   "O portal manda e-mail?",
                   "Para que serve o e-mail transacional?",
                   "Por que não chegou o e-mail da análise?",
                   "Recebo e-mail quando a análise é publicada?"),
        termos="email emails transacional envia recebi nao chegou spam integracao",
        relacionados=("nexo.acesso.recuperar_senha", "nexo.notificacoes.sininho"),
        fontes=("emails.py", "config.py", "templates/emails/boas_vindas.html"),
    ),
    registro(
        id="nexo.notificacoes.tempo_real",
        titulo="As notificações chegam em tempo real?",
        categoria="Notificações",
        resumo=(
            "Sim: com o portal aberto, o aviso aparece no sininho e como alerta no canto da tela no momento "
            "em que a equipe confirma a ação. Com o portal fechado, o aviso fica guardado e aparece no próximo "
            "acesso."
        ),
        perguntas=(
            "As notificações são instantâneas?", "Preciso atualizar a página para ver avisos?",
            "O aviso aparece na hora?",
        ),
        termos="tempo real instantaneo atualizar pagina websocket alerta canto",
        relacionados=("nexo.notificacoes.sininho",),
        fontes=("realtime.py", "notifications.py", "templates/base.html"),
    ),
)
