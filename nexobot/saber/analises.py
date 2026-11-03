"""Assunto F — análises: criação, período, tipos e estados.

Fontes: models.py (Analise), blueprints/admin.py (analise_nova, processar_analise,
analise_relatorio), blueprints/cliente.py (STATUS_ACEITA_UPLOAD, historico, analise),
templates/_macros.html (badge_analise).
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.analises.o_que_e",
        titulo="O que é uma análise no NEXO?",
        categoria="Análises",
        resumo=(
            "Uma análise é o ciclo de trabalho de um período (um mês ou uma quinzena) da sua empresa: ela "
            "recebe os relatórios de Vendas e Compras, gera os indicadores e termina com a devolutiva "
            "estratégica publicada."
        ),
        explicacao=(
            "Cada análise tem um período de referência (datas de início e fim), um mês e ano de referência, "
            "um tipo (mensal ou quinzenal), o plano vigente na abertura e um status que evolui de Aguardando "
            "relatório até Concluído. Aos resultados pertencem os indicadores, a Curva ABC e a devolutiva."
        ),
        perguntas=(
            "O que é análise?", "O que significa análise no portal?", "O que é uma análise estratégica?",
            "O que compõe uma análise?", "O que é a entrega do NEXO?",
        ),
        termos="analise ciclo periodo entrega definicao composicao",
        relacionados=("nexo.analises.como_e_criada", "nexo.analises.status"),
        destino="historico",
        fontes=("models.py", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.analises.como_e_criada",
        titulo="Como uma análise é criada e quem pode criá-la?",
        categoria="Análises",
        resumo=(
            "Somente a equipe NEXO cria análises, na área administrativa, escolhendo a empresa, o tipo "
            "(mensal ou quinzenal), o período e o mês/ano de referência. Ela nasce com o status Aguardando "
            "relatório e aparece para você em Enviar Relatórios."
        ),
        explicacao=(
            "Regras aplicadas na criação: a empresa precisa estar ATIVA; análises quinzenais exigem o plano "
            "OURO e a indicação da quinzena (1ª ou 2ª); o período não pode ter início depois do fim; e o plano "
            "atual da empresa fica congelado na análise como referência histórica. O cliente não cria "
            "análises e o NexoBot também não."
        ),
        perguntas=(
            "Como uma análise é criada?", "Quem cria a análise?", "Posso criar uma análise?",
            "Como peço uma nova análise?", "Como o administrador cria uma análise?",
            "Quem abre o período de análise?",
        ),
        termos="criar criada criacao abrir analise quem pode nova admin equipe",
        relacionados=("nexo.analises.periodo_referencia", "nexo.admin.criar_analises", "nexo.admin.nao_posso_executar"),
        fontes=("blueprints/admin.py", "models.py"),
    ),
    registro(
        id="nexo.analises.periodo_referencia",
        titulo="O que é o período de referência de uma análise?",
        categoria="Análises",
        resumo=(
            "É o intervalo de datas que a análise cobre, por exemplo de 01/03 a 31/03. Os relatórios do PDV "
            "devem ser exportados exatamente para esse intervalo, e o mês/ano de referência identifica a "
            "análise nas listas e nos avisos."
        ),
        explicacao=(
            "Quando o relatório importado traz o período no cabeçalho, o processamento lê essas datas e as "
            "mostra na barra de origem dos dados da análise (“Período dos dados”), permitindo conferir se o "
            "arquivo cobre o período esperado."
        ),
        perguntas=(
            "O que é período de referência?", "Qual período devo exportar?", "O que é mês de referência?",
            "Como sei o período da análise?", "O que significa ref. 03/2026?",
        ),
        termos="periodo referencia datas inicio fim mes ano intervalo",
        relacionados=("nexo.uploads.escolher_periodo", "nexo.dashboard.auditoria"),
        destino="upload",
        fontes=("models.py", "etl_processor.py", "templates/_auditoria.html"),
    ),
    registro(
        id="nexo.analises.mensal_quinzenal",
        titulo="Qual é a diferença entre análise mensal e quinzenal?",
        categoria="Análises",
        resumo=(
            "A mensal cobre um mês inteiro e está disponível em todos os planos. A quinzenal cobre metade do "
            "mês (1ª ou 2ª quinzena), gera duas análises por mês e só pode ser criada para empresas do plano OURO."
        ),
        explicacao=(
            "O processamento, os indicadores e a devolutiva são os mesmos nos dois tipos; o que muda é o "
            "tamanho do período e a frequência das entregas. As listas da Visão Geral, do Histórico e de Enviar "
            "Relatórios mostram o tipo de cada análise (Mensal ou Quinzenal). A barra de auditoria mostra a "
            "base de dados, o período dos dados e os arquivos importados."
        ),
        perguntas=(
            "Mensal ou quinzenal?", "O que é análise quinzenal?", "O que é análise mensal?",
            "Posso ter análise quinzenal?", "Quantas análises recebo por mês?", "Diferença entre mensal e quinzenal.",
        ),
        termos="mensal quinzenal quinzena tipo analise frequencia periodicidade",
        relacionados=("nexo.planos.diferenca", "nexo.analises.periodo_referencia"),
        fontes=("blueprints/admin.py", "models.py"),
    ),
    registro(
        id="nexo.analises.status",
        titulo="O que significa cada status de uma análise?",
        categoria="Análises",
        resumo=(
            "Aguardando relatório: faltam arquivos. Relatório recebido: os dois arquivos chegaram e aguardam a "
            "equipe. Em análise: processada, com a devolutiva em preparação. Concluído: devolutiva publicada e "
            "visível para você."
        ),
        explicacao=(
            "Os status seguem a ordem Aguardando relatório → Relatório recebido → Em análise → Concluído. O "
            "envio dos dois arquivos muda o status para Relatório recebido; o processamento pela equipe muda "
            "para Em análise (se o processamento falhar, volta para Relatório recebido); a publicação da "
            "devolutiva muda para Concluído. Se a equipe despublicar, a análise volta para Em análise e sai "
            "do Histórico até ser publicada novamente."
        ),
        perguntas=(
            "Quais são os status da análise?", "O que significam os status?", "Status aguardando relatório.",
            "O que é cada estado da análise?", "O que quer dizer o selo da análise?",
            "Em que ordem os status acontecem?",
        ),
        termos="status estado estados aguardando relatorio recebido em analise concluido selo badge ordem",
        relacionados=("nexo.analises.status_recebido", "nexo.analises.status_em_analise",
                      "nexo.analises.status_concluido"),
        destino="upload",
        fontes=("models.py", "blueprints/cliente.py", "blueprints/admin.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.analises.status_aguardando",
        titulo="O que significa “Aguardando relatório”?",
        categoria="Análises",
        resumo=(
            "A análise foi aberta pela equipe NEXO e ainda não recebeu os dois relatórios (Vendas e Compras). "
            "Ela aparece em Enviar Relatórios para você anexar os arquivos."
        ),
        perguntas=(
            "Aguardando relatório significa o quê?", "Por que minha análise está aguardando relatório?",
            "Status aguardando relatório.",
        ),
        termos="aguardando relatorio status significa faltam arquivos",
        relacionados=("nexo.uploads.como_enviar", "nexo.analises.status"),
        destino="upload",
        fontes=("models.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.analises.status_recebido",
        titulo="O que significa “Relatório recebido”?",
        categoria="Análises",
        resumo=(
            "Os dois arquivos (Vendas e Compras) chegaram e a análise aguarda a conferência e o processamento "
            "pela equipe NEXO. Enquanto estiver nesse status você ainda pode reenviar um arquivo."
        ),
        explicacao=(
            "Cada arquivo aparece com o selo “AGUARDANDO VALIDAÇÃO” em Enviar Relatórios. Se o processamento "
            "falhar, os dois arquivos passam a exibir o selo “ERRO” em Enviar Relatórios (com a mesma mensagem "
            "de erro) e a análise volta para Relatório recebido. Reenvie o relatório correto do tipo com "
            "problema: ele volta a “AGUARDANDO VALIDAÇÃO” e o outro continua como “ERRO” até a equipe processar "
            "novamente."
        ),
        perguntas=(
            "O que significa relatório recebido?", "Status relatório recebido.", "Relatório recebido e agora?",
            "Minha análise está em relatório recebido há dias.",
        ),
        termos="relatorio recebido status significa validacao aguardando equipe",
        relacionados=("nexo.uploads.status", "nexo.processamento.quem_controla"),
        destino="upload",
        fontes=("blueprints/cliente.py", "blueprints/admin.py", "templates/cliente/upload.html"),
    ),
    registro(
        id="nexo.analises.status_em_analise",
        titulo="O que significa “Em análise”?",
        categoria="Análises",
        resumo=(
            "Os arquivos foram processados com sucesso e os indicadores já existem; a equipe NEXO está "
            "preparando a devolutiva estratégica. Nesse status a análise ainda não aparece no Histórico."
        ),
        explicacao=(
            "Você recebe o aviso “Recebemos e processamos seus relatórios” quando a análise entra nessa fase, "
            "ou o aviso de diferença, se o total calculado não bater com o que você informou. "
            "A análise deixa de aceitar novos envios; se for preciso trocar um arquivo, fale com a equipe pelo "
            "Suporte."
        ),
        perguntas=(
            "O que significa em análise?", "Status em análise.", "Em análise quer dizer que já processou?",
            "Por que não vejo a análise que está em análise?",
        ),
        termos="em analise status significa processada devolutiva preparacao",
        relacionados=("nexo.analises.processada_vs_publicada", "nexo.devolutivas.quem_prepara"),
        fontes=("blueprints/admin.py", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.analises.status_concluido",
        titulo="O que significa “Concluído”?",
        categoria="Análises",
        resumo=(
            "A devolutiva foi publicada: a análise aparece na Visão Geral (se for a mais recente) e no "
            "Histórico, com indicadores, gráficos, Curva ABC, semáforos, texto da devolutiva e exportação em PDF."
        ),
        perguntas=(
            "O que significa concluído?", "Status concluído.", "Concluído é igual a publicado?",
        ),
        termos="concluido concluida status significa publicada finalizada",
        relacionados=("nexo.historico.abrir", "nexo.historico.pdf_exportar"),
        destino="historico",
        fontes=("blueprints/admin.py", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.analises.acompanhar_andamento",
        titulo="Como acompanho uma análise em andamento?",
        categoria="Análises",
        tipo="procedimento",
        resumo=(
            "Na Visão Geral, a tabela “Análises em andamento” mostra período, tipo e status de tudo que ainda "
            "não foi publicado; em Enviar Relatórios você vê o status de cada arquivo enquanto a análise ainda "
            "está em Aguardando relatório ou Relatório recebido (depois de processada, ela sai dessa tela e "
            "fica só na tabela da Visão Geral) enquanto a análise ainda está em Aguardando relatório ou "
            "Relatório recebido (depois de processada, ela sai dessa tela e fica só na tabela da Visão Geral); "
            "e o sininho avisa das mudanças."
        ),
        perguntas=(
            "Como vejo as análises em andamento?", "Onde acompanho a análise do mês?",
            "Como saber se a equipe já processou?", "Como vejo a situação da análise atual?",
        ),
        termos="acompanhar andamento situacao analise atual mes processou",
        relacionados=("nexo.analises.status", "nexo.notificacoes.eventos"),
        destino="dashboard",
        fontes=("templates/cliente/dashboard.html", "templates/cliente/upload.html"),
    ),
    registro(
        id="nexo.analises.antes_da_publicacao",
        titulo="O que acontece antes da publicação de uma análise?",
        categoria="Análises",
        resumo=(
            "Depois do processamento, a equipe NEXO escreve a devolutiva (resumo executivo e conclusão "
            "estratégica), pode salvar rascunhos e só publica quando o texto está revisado. Até lá a análise "
            "fica Em análise e invisível no Histórico."
        ),
        explicacao=(
            "A publicação exige que a análise tenha sido processada, sem divergência no total de vendas, e que o "
            "resumo executivo e a conclusão "
            "estratégica estejam preenchidos. A equipe pode usar a geração assistida por IA para um rascunho, "
            "mas a revisão e a decisão de publicar são humanas."
        ),
        perguntas=(
            "O que acontece antes de publicar?", "Por que demora para publicar?", "O que a equipe faz depois de processar?",
            "O que falta para a análise ser publicada?",
        ),
        termos="antes publicacao publicar rascunho revisao devolutiva preparacao demora",
        relacionados=("nexo.devolutivas.quem_prepara", "nexo.devolutivas.ia"),
        fontes=("blueprints/admin.py", "ai_devolutiva.py"),
    ),
    registro(
        id="nexo.analises.quando_vejo_resultados",
        titulo="Quando posso ver os resultados da minha análise?",
        categoria="Análises",
        resumo=(
            "Assim que a equipe NEXO publica a devolutiva. Você é avisado pelo sininho (e por e-mail, quando "
            "o envio está configurado) e a análise aparece na Visão Geral e no Histórico. Não há prazo "
            "automático: o tempo depende da conferência e da preparação pela equipe."
        ),
        perguntas=(
            "Quando vejo os resultados?", "Quando a análise fica pronta?", "Quanto tempo demora a análise?",
            "Quando sai a devolutiva?", "Em quanto tempo recebo o resultado?",
        ),
        termos="quando resultados pronta prazo demora tempo publicacao ver",
        relacionados=("nexo.devolutivas.publicacao", "nexo.analises.status"),
        destino="historico",
        fontes=("blueprints/admin.py", "notifications.py"),
    ),
    registro(
        id="nexo.analises.processada_vs_publicada",
        titulo="Qual é a diferença entre análise processada e análise publicada?",
        categoria="Análises",
        resumo=(
            "Processada significa que os arquivos foram lidos e os indicadores calculados (status Em análise). "
            "Publicada significa que a equipe liberou a devolutiva para você (status Concluído). Só a análise "
            "publicada aparece no Histórico."
        ),
        perguntas=(
            "Processada é a mesma coisa que publicada?", "Diferença entre processada e publicada.",
            "Já processou, por que não aparece?", "O que é análise publicada?",
        ),
        termos="processada publicada diferenca concluido em analise visivel",
        relacionados=("nexo.analises.status_em_analise", "nexo.analises.status_concluido"),
        fontes=("blueprints/cliente.py", "blueprints/admin.py"),
    ),
    registro(
        id="nexo.analises.nao_aparece",
        titulo="Minha análise ou meu relatório não aparece. O que pode ser?",
        categoria="Análises",
        tipo="roteiro",
        resumo=(
            "Normalmente é uma questão de etapa: a análise só aparece no Histórico depois de publicada. "
            "Confira o status na Visão Geral (Análises em andamento) e o status dos arquivos em Enviar "
            "Relatórios; se algo estiver com Erro ou parado há muito tempo, abra um chamado."
        ),
        passos=(
            "Abra a Visão Geral e veja a tabela “Análises em andamento”: se a análise estiver lá, ela ainda "
            "não foi publicada.",
            "Em Enviar Relatórios, confira se os dois arquivos constam como enviados e qual é o status de cada um.",
            "Se um arquivo estiver com o selo “ERRO”, reenvie o relatório correto do mesmo tipo (o botão passa a se chamar “Reenviar”; em Vendas, informe o total do PDV).",
            "Se o status for Em análise, a devolutiva está em preparação pela equipe; aguarde o aviso de publicação.",
            "Se nada aparecer em lugar nenhum ou a situação estiver parada há muito tempo, abra um chamado em "
            "Suporte informando o período.",
        ),
        perguntas=(
            "Não aparece meu relatório.", "Minha análise sumiu.", "Cadê minha análise?", "Não vejo a análise do mês.",
            "Enviei os arquivos e nada aparece.", "O histórico está vazio.", "Por que não aparece nada na Visão Geral?",
        ),
        termos="nao aparece sumiu cade vazio nada nenhuma analise relatorio desapareceu",
        relacionados=("nexo.analises.status", "nexo.uploads.status", "nexo.suporte.abrir_chamado"),
        proximo="nexo.suporte.abrir_chamado",
        destino="dashboard",
        fontes=("blueprints/cliente.py", "templates/cliente/dashboard.html", "templates/cliente/upload.html"),
    ),
)
