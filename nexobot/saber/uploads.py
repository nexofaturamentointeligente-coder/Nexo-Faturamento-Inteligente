"""Assunto G — envio de relatórios (upload).

Fontes: blueprints/cliente.py (upload, STATUS_ACEITA_UPLOAD), config.py
(ALLOWED_EXTENSIONS), etl_uploads.py, etl_inputs.py, etl_limits.py,
etl_processor.py (ALIAS_VENDAS/ALIAS_COMPRAS, validadores), storage.py,
templates/cliente/upload.html, seed_guia.py.
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.uploads.como_enviar",
        titulo="Como envio os relatórios de Vendas e Compras?",
        categoria="Envio de relatórios",
        tipo="procedimento",
        resumo=(
            "Em Enviar Relatórios, escolha a análise aberta do período, anexe o arquivo de Vendas em um campo "
            "e o de Compras no outro (.csv ou .xlsx) e confirme cada envio. No envio de Vendas, informe também "
            "o total de vendas que o PDV mostra para o período. A equipe NEXO valida e processa."
        ),
        explicacao=(
            "Cada análise tem dois espaços: Relatório de Vendas (giro de vendas por produto) e Relatório de "
            "Compras (posição de compras do período). O envio é feito um arquivo por vez. Depois do envio o "
            "arquivo fica “AGUARDANDO VALIDAÇÃO”; quando os dois chegam, a análise passa para Relatório "
            "recebido e a equipe é avisada."
        ),
        passos=(
            "No menu lateral, abra Enviar Relatórios.",
            "Localize o cartão da análise do período correto (mês/ano de referência e datas).",
            "No espaço Relatório de Vendas, informe o total de vendas que o PDV mostra para o período, clique "
            "ou arraste o arquivo .csv ou .xlsx exportado do PDV e clique em Enviar Vendas.",
            "Repita no espaço Relatório de Compras e clique em Enviar Compras.",
            "Confira o status de cada arquivo (“AGUARDANDO VALIDAÇÃO”) e a mensagem de que os dois relatórios "
            "foram recebidos.",
        ),
        perguntas=(
            "Como envio meus arquivos?", "Como faço para enviar relatórios?", "Como faço o upload dos relatórios?",
            "Onde anexo as planilhas?", "Como mando os relatórios do PDV?", "Como subo os arquivos?",
            "Quero enviar meus relatórios.", "Como enviar a planilha de vendas?", "Como anexar o relatório de compras?",
        ),
        termos="enviar envio upload anexar subir mandar arquivos planilhas relatorios vendas compras passo a passo",
        relacionados=("nexo.uploads.formatos", "nexo.uploads.depois_do_envio", "nexo.uploads.exportar_pdv"),
        proximo="nexo.uploads.depois_do_envio",
        destino="upload",
        fontes=("blueprints/cliente.py", "templates/cliente/upload.html"),
    ),
    registro(
        id="nexo.uploads.primeiro_envio",
        titulo="Nunca enviei um relatório. Como faço o primeiro envio?",
        categoria="Envio de relatórios",
        tipo="roteiro",
        resumo=(
            "Exporte do seu PDV dois relatórios do período da análise, um de Vendas e um de Compras, em "
            ".csv ou .xlsx, e anexe cada um no cartão da análise em Enviar Relatórios. Se nenhuma análise "
            "aparecer lá, a equipe ainda não abriu o período."
        ),
        passos=(
            "Abra Enviar Relatórios e veja se há uma análise aberta; anote o período que ela cobre.",
            "No seu PDV ou ERP, gere o relatório de Vendas por produto desse período (precisa ter produto, "
            "quantidade e valor) e exporte em .xlsx ou .csv.",
            "Gere também o relatório de Compras do mesmo período (fornecedor e valor da nota, ou produto, "
            "quantidade e valor) e exporte no mesmo formato.",
            "Anote também o total de vendas que o PDV mostra nesse relatório.",
            "Volte ao portal, informe esse total e anexe o arquivo de Vendas no espaço de Vendas; confirme com "
            "Enviar Vendas.",
            "Anexe o arquivo de Compras no espaço de Compras; confirme com Enviar Compras.",
            "Aguarde: os arquivos ficam “AGUARDANDO VALIDAÇÃO” até a equipe processar. Você será avisado no sininho.",
        ),
        detalhes=(
            "Arquivos .xls (Excel antigo) não são aceitos: salve como .xlsx ou .csv. O limite de tamanho é de "
            "20 MB por arquivo. Se enviar o arquivo errado, basta enviar o correto no mesmo espaço, informando "
            "de novo o total no caso de Vendas: ele "
            "substitui o anterior enquanto a análise aceitar envios."
        ),
        perguntas=(
            "Nunca enviei um relatório.", "É minha primeira vez enviando.", "Primeiro upload, como faço?",
            "Não sei por onde começar o envio.", "Como faço meu primeiro envio de planilha?",
        ),
        termos="primeiro envio primeira vez nunca enviei comecar upload roteiro",
        relacionados=("nexo.uploads.exportar_pdv", "nexo.uploads.formatos", "nexo.uploads.colunas_necessarias"),
        proximo="nexo.uploads.depois_do_envio",
        destino="upload",
        fontes=("blueprints/cliente.py", "config.py", "etl_limits.py", "etl_processor.py"),
    ),
    registro(
        id="nexo.uploads.formatos",
        titulo="Quais formatos de arquivo são aceitos?",
        categoria="Envio de relatórios",
        tipo="regra",
        resumo=(
            "Apenas .csv e .xlsx, com até 20 MB por arquivo. O formato .xls (Excel antigo) não é aceito: "
            "salve a planilha como .xlsx ou .csv antes de enviar."
        ),
        explicacao=(
            "O portal verifica a extensão e também o conteúdo: um .xlsx precisa ser uma planilha válida e um "
            ".csv precisa ser legível em um dos formatos de separador comuns (ponto e vírgula, vírgula, "
            "tabulação ou barra vertical). Planilhas com várias abas são aceitas; a aba com mais dados é usada."
        ),
        perguntas=(
            "Quais formatos são aceitos?", "Posso enviar xls?", "Aceita Excel?", "Aceita CSV?", "Aceita PDF?",
            "Qual o tamanho máximo do arquivo?", "Que tipo de arquivo posso enviar?", "Posso mandar planilha do Google?",
        ),
        termos="formatos aceitos extensao csv xlsx xls excel pdf tamanho maximo mb tipo arquivo",
        relacionados=("nexo.uploads.exportar_pdv", "nexo.uploads.erros"),
        destino="upload",
        fontes=("config.py", "etl_uploads.py", "etl_inputs.py", "etl_limits.py", "requirements.txt"),
    ),
    registro(
        id="nexo.uploads.exportar_pdv",
        titulo="Como exporto os relatórios do meu sistema PDV?",
        categoria="Envio de relatórios",
        tipo="procedimento",
        resumo=(
            "No seu PDV ou ERP, gere o relatório de Vendas por produto e o relatório de Compras do período da "
            "análise e exporte cada um em Excel (.xlsx) ou CSV. O NEXO reconhece as colunas pelo nome, sem "
            "exigir um modelo fixo."
        ),
        explicacao=(
            "Cada PDV tem o seu caminho de exportação, mas o que importa é o conteúdo: o relatório de Vendas "
            "precisa listar produto, quantidade vendida e valor (total da linha ou preço unitário); o de "
            "Compras precisa ter fornecedor ou produto e o valor (total da nota ou custo unitário com "
            "quantidade). Linhas decorativas, totais e cabeçalhos repetidos são ignorados pelo processamento."
        ),
        passos=(
            "No PDV, abra o relatório de Vendas por produto e filtre o período da análise.",
            "Exporte em .xlsx ou .csv.",
            "Faça o mesmo com o relatório de Compras (entradas ou notas de compra) do mesmo período.",
            "Dê nomes claros aos arquivos, por exemplo “vendas março 2026” e “compras março 2026”.",
            "Anote o total de vendas que o PDV mostra nesse relatório e anexe os dois em Enviar Relatórios.",
        ),
        perguntas=(
            "Como exportar os relatórios do PDV?", "Como tiro o relatório do meu sistema?", "Como gero a planilha de vendas?",
            "Como exporto do ERP?", "Qual relatório do PDV devo usar?", "Como extraio os dados do caixa?",
        ),
        termos="exportar pdv erp sistema caixa gerar relatorio planilha extrair excel csv",
        relacionados=("nexo.uploads.colunas_necessarias", "nexo.uploads.formatos"),
        destino="upload",
        fontes=("etl_processor.py", "seed_guia.py", "templates/cliente/upload.html"),
    ),
    registro(
        id="nexo.uploads.colunas_necessarias",
        titulo="Quais colunas os relatórios precisam ter?",
        categoria="Envio de relatórios",
        tipo="regra",
        resumo=(
            "Vendas: nome do produto, quantidade vendida e valor total da linha (ou preço unitário, que o "
            "sistema multiplica pela quantidade). Compras: fornecedor ou produto e o valor total (ou custo "
            "unitário mais quantidade comprada)."
        ),
        explicacao=(
            "O processamento procura o cabeçalho nas primeiras linhas e reconhece as colunas por nomes "
            "parecidos (por exemplo “Descrição do produto”, “Qtde vendida”, “Vr total”, “Fornecedor”, "
            "“Total NF”). A posição das colunas não importa. Se faltar uma coluna essencial, o processamento "
            "é recusado com a lista do que foi reconhecido, e a equipe pode pedir um novo arquivo."
        ),
        detalhes=(
            "No relatório de Vendas, a coluna de lucro, quando existe, é apenas reconhecida e descartada, e a "
            "coluna de custo não entra em nenhum indicador: serve só para um alerta interno à equipe sobre "
            "itens vendidos abaixo do custo informado pelo PDV. O NEXO não calcula lucro, margem ou CMV. Já no "
            "relatório de Compras, colunas como “Total Custo” ou “Custo unitário” são usadas como o valor "
            "comprado.: o NEXO não usa lucro, margem ou CMV. Para o indicador de saldo estimado parado, o "
            "relatório de Compras precisa trazer produto e quantidade comprada; se vier apenas por nota fiscal "
            "(fornecedor e valor), esse indicador fica como “não calculado”."
        ),
        perguntas=(
            "Quais colunas o arquivo precisa ter?", "Que informações o relatório de vendas deve conter?",
            "O relatório de compras precisa de quê?", "Preciso seguir um modelo de planilha?",
            "O sistema aceita qualquer layout?", "As colunas precisam estar em ordem?",
        ),
        termos="colunas layout modelo campos produto quantidade valor fornecedor nota cabecalho",
        relacionados=("nexo.processamento.extracao", "nexo.indicadores.saldo_parado"),
        fontes=("etl_processor.py",),
    ),
    registro(
        id="nexo.uploads.vendas_vs_compras",
        titulo="Qual é a diferença entre o relatório de Vendas e o de Compras?",
        categoria="Envio de relatórios",
        resumo=(
            "O relatório de Vendas lista o que a loja vendeu por produto (quantidade e valor) e alimenta o "
            "faturamento, os produtos de destaque e a Curva ABC. O relatório de Compras lista o que a loja "
            "comprou (notas de fornecedores ou itens) e alimenta o total comprado e o Indicador de Pressão "
            "de Estoque."
        ),
        perguntas=(
            "Diferença entre relatório de vendas e de compras.", "O que é o relatório de vendas?",
            "O que é o relatório de compras?", "Qual arquivo é de vendas e qual é de compras?",
        ),
        termos="vendas compras diferenca relatorio tipo arquivo entradas saidas",
        relacionados=("nexo.uploads.por_que_dois", "nexo.indicadores.quais"),
        fontes=("etl_processor.py", "templates/cliente/upload.html"),
    ),
    registro(
        id="nexo.uploads.por_que_dois",
        titulo="Por que preciso enviar os dois relatórios, de Compras e de Vendas?",
        categoria="Envio de relatórios",
        resumo=(
            "Os dois arquivos alimentam a análise: o de Vendas gera o faturamento e a Curva ABC dos produtos, "
            "e o de Compras é comparado com as vendas no Indicador de Pressão de Estoque. Por isso a análise "
            "só segue para validação quando os dois foram recebidos."
        ),
        explicacao=(
            "Sem o relatório de Compras não existe o total comprado nem o descompasso compras x vendas; sem o "
            "de Vendas não existem faturamento, produtos de destaque nem Curva ABC. O botão de processar só "
            "fica disponível para a equipe quando os dois estão presentes."
        ),
        perguntas=(
            "Por que preciso enviar dois relatórios?", "Posso enviar só o de vendas?", "Preciso mesmo do relatório de compras?",
            "É obrigatório mandar compras e vendas?", "Por que dois arquivos?",
        ),
        termos="porque motivo razao precisa necessario obrigatorio dois ambos compras vendas arquivos relatorios",
        relacionados=("nexo.uploads.vendas_vs_compras", "nexo.indicadores.pressao_estoque"),
        destino="upload",
        fontes=("etl_processor.py", "blueprints/admin.py"),
    ),
    registro(
        id="nexo.uploads.escolher_periodo",
        titulo="Como identifico o período correto para o envio?",
        categoria="Envio de relatórios",
        tipo="procedimento",
        resumo=(
            "Cada cartão em Enviar Relatórios mostra o mês/ano de referência e as datas de início e fim da "
            "análise. Exporte do PDV exatamente esse intervalo e anexe no cartão correspondente."
        ),
        explicacao=(
            "Se houver mais de uma análise aberta (por exemplo, duas quinzenas), confira as datas antes de "
            "anexar. Depois que a análise é publicada, a barra de origem dos dados na Visão Geral e na página "
            "da análise mostra o período lido do cabeçalho do próprio arquivo (quando o relatório traz essa "
            "informação; caso contrário, exibe o período declarado da análise), o que permite verificar se o "
            "relatório cobriu o intervalo certo."
        ),
        perguntas=(
            "Qual período devo enviar?", "Como sei em qual análise anexar?", "Enviei no período errado.",
            "Como escolho a análise certa para o upload?",
        ),
        termos="periodo correto escolher analise certa datas mes referencia quinzena",
        relacionados=("nexo.analises.periodo_referencia", "nexo.uploads.substituir"),
        destino="upload",
        fontes=("templates/cliente/upload.html", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.uploads.depois_do_envio",
        titulo="O que acontece depois que envio os meus arquivos?",
        categoria="Envio de relatórios",
        resumo=(
            "O arquivo fica guardado com segurança e marcado como “AGUARDANDO VALIDAÇÃO”; a equipe NEXO é "
            "avisada. Com os dois arquivos recebidos, a equipe confere e aciona o processamento; depois "
            "prepara a devolutiva e, ao publicar, você é avisado e a análise aparece no portal."
        ),
        passos=(
            "O envio é validado na hora (formato, tamanho e integridade da planilha) e o arquivo é armazenado.",
            "Quando os dois arquivos estão presentes, a análise passa a Relatório recebido e a equipe recebe "
            "uma notificação de homologação.",
            "A equipe confere os arquivos e executa o processamento: os arquivos ficam “PROCESSADO” (ou “ERRO”) "
            "e você recebe o aviso “Recebemos e processamos seus relatórios”. Se o total calculado não bater com "
            "o total que você informou, o aviso é outro e a devolutiva fica bloqueada até a conferência.",
            "A análise entra em Em análise enquanto a equipe prepara a devolutiva estratégica.",
            "Na publicação, você recebe o aviso no sininho (e e-mail, quando configurado) e a análise aparece "
            "na Visão Geral e no Histórico.",
        ),
        perguntas=(
            "O que acontece depois que envio?", "Enviei os arquivos, e agora?", "O que acontece após o upload?",
            "Depois do envio, o que a equipe faz?", "Qual é o próximo passo depois de enviar?",
        ),
        termos="depois envio apos upload proximo passo acontece fluxo",
        relacionados=("nexo.processamento.quem_controla", "nexo.analises.status", "nexo.devolutivas.publicacao"),
        proximo="nexo.analises.quando_vejo_resultados",
        destino="upload",
        fontes=("blueprints/cliente.py", "blueprints/admin.py", "notifications.py"),
    ),
    registro(
        id="nexo.uploads.substituir",
        titulo="Enviei a planilha errada. Como substituo o arquivo?",
        categoria="Envio de relatórios",
        tipo="roteiro",
        resumo=(
            "Enquanto a análise estiver aberta para envio, mande novamente o arquivo do mesmo tipo (Vendas ou "
            "Compras) em Enviar Relatórios: o novo arquivo substitui o anterior. Se a análise já não aparecer "
            "para envio, abra um chamado em Suporte para a equipe NEXO avaliar."
        ),
        passos=(
            "Abra Enviar Relatórios e veja se o cartão da análise ainda está lá (status Aguardando relatório "
            "ou Relatório recebido).",
            "No espaço do tipo errado (Vendas ou Compras), anexe o arquivo correto e clique em Reenviar. Em "
            "Vendas, informe de novo o total do PDV.",
            "Confira o nome do arquivo exibido no cartão: ele deve ser o do novo envio.",
            "Se o cartão não aparecer mais (análise Em análise ou Concluída), abra um chamado em Suporte "
            "explicando o período e o arquivo a substituir.",
        ),
        perguntas=(
            "Enviei a planilha errada.", "Mandei o arquivo errado, e agora?", "Posso reenviar uma planilha?",
            "Como troco o arquivo enviado?", "Enviei vendas no lugar de compras.", "Como corrijo um envio?",
        ),
        termos="errada errado enganado engano incorreto trocar substituir reenviar corrigir arquivo planilha de novo",
        relacionados=("nexo.uploads.quando_reenviar", "nexo.suporte.abrir_chamado"),
        proximo="nexo.suporte.abrir_chamado",
        destino="upload",
        fontes=("blueprints/cliente.py", "templates/cliente/upload.html"),
    ),
    registro(
        id="nexo.uploads.quando_reenviar",
        titulo="Quando o reenvio de um arquivo é permitido?",
        categoria="Envio de relatórios",
        tipo="regra",
        resumo=(
            "Enquanto a análise estiver em Aguardando relatório ou Relatório recebido, inclusive depois de um "
            "arquivo marcado como “ERRO”. Depois que a equipe processa (Em análise) ou publica (Concluído), "
            "a análise sai de Enviar Relatórios e só a equipe pode intervir."
        ),
        perguntas=(
            "Posso reenviar depois de processado?", "Até quando posso trocar o arquivo?",
            "Quando posso reenviar?", "O reenvio é permitido em qual status?",
        ),
        termos="reenvio reenviar permitido quando status prazo trocar",
        relacionados=("nexo.uploads.substituir", "nexo.analises.status"),
        destino="upload",
        fontes=("blueprints/cliente.py", "blueprints/admin.py"),
    ),
    registro(
        id="nexo.uploads.bloqueado",
        titulo="Por que o envio pode ser bloqueado?",
        categoria="Envio de relatórios",
        resumo=(
            "Os motivos mais comuns: não há análise aberta para a sua empresa; a análise já está Em análise "
            "ou Concluída; o arquivo tem formato ou tamanho fora do permitido; ou outro envio ou "
            "processamento está em andamento no servidor naquele instante."
        ),
        explicacao=(
            "Quando a conta está suspensa, a equipe não abre novos períodos. Se a sua empresa já está sob as "
            "regras contratuais, o envio fica restrito às análises de períodos já pagos; caso contrário, o "
            "envio para a análise já aberta segue liberado. Com a conta cancelada, o acesso ao portal é "
            "bloqueado. O envio de Vendas também é recusado sem o total de vendas em um formato válido, como "
            "150.000,00. O portal "
            "aceita um envio por vez: se aparecer a "
            "mensagem de que há outro upload em andamento, aguarde alguns instantes e tente de novo."
        ),
        perguntas=(
            "Por que não consigo enviar?", "O envio está bloqueado.", "Não aparece o botão de enviar.",
            "Por que o upload não funciona?", "Deu erro ao enviar o arquivo.",
        ),
        termos="bloqueado nao consigo enviar upload nao funciona erro envio motivo",
        relacionados=("nexo.uploads.erros", "nexo.primeiros_passos.analises_disponiveis"),
        destino="upload",
        fontes=("blueprints/cliente.py", "etl_uploads.py", "blueprints/admin.py", "session_security.py"),
    ),
    registro(
        id="nexo.uploads.erros",
        titulo="Como interpreto os erros de envio e de processamento?",
        categoria="Envio de relatórios",
        resumo=(
            "Erros no envio aparecem na hora, no topo da página (formato não suportado, arquivo vazio, acima "
            "do tamanho, planilha ilegível). Erros no processamento aparecem depois, com o arquivo marcado como "
            "“ERRO”, e normalmente indicam colunas essenciais não reconhecidas."
        ),
        explicacao=(
            "Mensagens típicas do envio: “Extensão não suportada” (use .csv ou .xlsx), “Arquivo vazio”, "
            "Arquivos acima de 20 MB são recusados pelo servidor antes do envio ser processado: nesse caso "
            "aparece uma página de erro genérica (“Request Entity Too Large”), não uma mensagem no topo da "
            "página de envio. Reduza o período ou as colunas do relatório e tente novamente., “Não foi possível "
            "interpretar o CSV” ou Mensagens típicas do envio: “Extensão .xls não suportada” (use .csv ou "
            ".xlsx), “Arquivo vazio — nada foi salvo”, “Não foi possível interpretar o CSV com os formatos "
            "suportados” ou “A estrutura compactada do XLSX é inválida”.. No processamento, a recusa mais comum "
            "é “Não consegui identificar as colunas essenciais”, acompanhada da lista das colunas reconhecidas. "
            "Esse diagnóstico fica visível para a equipe NEXO, que o repassa a você (pelo chamado de Suporte ou "
            "outro canal) para ajustar a exportação no PDV; no portal, você vê apenas o arquivo marcado como "
            "“ERRO” e pode reenviar o arquivo corrigido."
        ),
        passos=(
            "Leia a mensagem exibida no topo da página de envio ou o status “ERRO” no cartão do arquivo.",
            "Se for formato ou tamanho, converta para .csv ou .xlsx e reduza o período ou as colunas.",
            "Se for coluna não reconhecida, confira se o relatório é tabular e tem produto, quantidade e valor "
            "(Vendas) ou fornecedor/produto e valor (Compras).",
            "Reenvie o arquivo corrigido no mesmo espaço (em Vendas, informando o total do PDV). Em dúvida, abra "
            "um chamado e descreva a mensagem.",
        ),
        perguntas=(
            "O que acontece se houver erro?", "Deu erro no meu arquivo.", "O que significa status erro?",
            "Como corrijo um arquivo com erro?", "Por que meu relatório foi recusado?", "O que é extensão não suportada?",
        ),
        termos="erro erros recusado invalido mensagem corrigir status erro colunas extensao tamanho",
        relacionados=("nexo.uploads.colunas_necessarias", "nexo.uploads.substituir", "nexo.processamento.erros"),
        destino="upload",
        fontes=("blueprints/cliente.py", "etl_inputs.py", "etl_uploads.py", "etl_processor.py"),
    ),
    registro(
        id="nexo.uploads.status",
        titulo="Como sei se o meu relatório foi processado?",
        categoria="Envio de relatórios",
        resumo=(
            "Em Enviar Relatórios, o arquivo aparece como “AGUARDANDO VALIDAÇÃO” enquanto a equipe NEXO "
            "confere. Quando a equipe processa com sucesso, você recebe no sininho o aviso “Recebemos e "
            "processamos seus relatórios” (ou o aviso de diferença, se o total calculado não bater com o que você "
            "informou), a análise sai de Enviar Relatórios e passa a aparecer como Em "
            "análise na tabela Análises em andamento da Visão Geral. Se o processamento falhar, o cartão "
            "continua em Enviar Relatórios com o arquivo marcado “ERRO” e você pode reenviar. Quando a análise "
            "é publicada, você recebe um aviso no sininho e ela passa a aparecer na Visão Geral e no Histórico."
        ),
        perguntas=(
            "Como sei se foi processado?", "Qual o status do meu arquivo?", "O que é aguardando validação?",
            "Meu arquivo foi validado?", "Como confiro se a equipe recebeu?",
        ),
        termos="status arquivo enviado processado processamento validado validacao aguardando recebido erro andamento conferir",
        relacionados=("nexo.analises.status", "nexo.processamento.acompanhar_resultado"),
        destino="upload",
        fontes=("templates/cliente/upload.html", "blueprints/admin.py"),
    ),
    registro(
        id="nexo.uploads.quem_valida",
        titulo="Quem valida os meus arquivos?",
        categoria="Envio de relatórios",
        resumo=(
            "A equipe NEXO. O envio pelo cliente fica na fila de homologação do consultor, que confere os "
            "arquivos e aciona o processamento. O sistema faz apenas verificações automáticas de formato, "
            "tamanho e integridade no momento do envio."
        ),
        perguntas=(
            "Quem valida meus arquivos?", "Quem confere os relatórios?", "O que é homologação?",
            "A validação é automática?",
        ),
        termos="valida validacao homologacao homologa confere equipe consultor fila",
        relacionados=("nexo.processamento.quem_controla", "nexo.admin.validar_relatorios"),
        fontes=("blueprints/cliente.py", "blueprints/admin.py", "etl_uploads.py"),
    ),
    registro(
        id="nexo.uploads.dados_seguros",
        titulo="Meus arquivos ficam seguros depois do envio?",
        categoria="Envio de relatórios",
        resumo=(
            "Os arquivos são guardados no banco de dados do NEXO, Os arquivos são guardados no banco de dados "
            "do NEXO e não ficam disponíveis para download no portal: você vê apenas o nome do arquivo enviado, "
            "e o conteúdo é lido somente pelo motor de processamento acionado pela equipe NEXO. O processamento "
            "usa as planilhas somente para calcular os indicadores: as vendas e compras linha a linha não são "
            "copiadas para as tabelas do sistema.. O processamento usa as planilhas somente para calcular os "
            "indicadores: as vendas e compras linha a linha não são copiadas para as tabelas do sistema."
        ),
        perguntas=(
            "Onde ficam meus arquivos?", "Meus dados ficam seguros?", "Quem vê minhas planilhas?",
            "Os relatórios ficam armazenados?",
        ),
        termos="seguros seguranca armazenados guardados privacidade quem ve planilhas lgpd",
        relacionados=("nexo.tecnologia.onde_ficam_arquivos", "nexo.acesso.paginas_restritas"),
        fontes=("storage.py", "README.md", "etl_processor.py"),
    ),
)
