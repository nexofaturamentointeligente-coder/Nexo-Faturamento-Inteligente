"""Assuntos L e M — devolutivas estratégicas, Histórico e PDF.

Fontes: models.py (RelatorioAnalise), blueprints/admin.py (analise_relatorio),
ai_devolutiva.py, blueprints/cliente.py (historico, analise, analise_pdf),
pdf_export.py, templates/cliente/historico.html e analise.html.

Nota verificada: o editor da devolutiva tem seis campos (título, resumo
executivo, pontos positivos, pontos de alerta, recomendações e conclusão
estratégica), mas a página do cliente e o PDF exibem apenas o resumo executivo
e a conclusão estratégica; pontos de alerta e recomendações chegam ao cliente
como semáforos automáticos (insights.py).
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.devolutivas.o_que_e",
        titulo="O que é uma devolutiva estratégica?",
        categoria="Devolutiva",
        resumo=(
            "É o relatório escrito pela equipe NEXO que interpreta os indicadores do período: um resumo "
            "executivo com a leitura dos números e uma conclusão estratégica com a prioridade para o próximo "
            "período. Ela é publicada junto com os indicadores, os semáforos e a Curva ABC."
        ),
        explicacao=(
            "Enquanto os indicadores e os semáforos são gerados pelo sistema, a devolutiva é um texto revisado "
            "por pessoas, que contextualiza os números para a realidade da sua loja. É o produto final de "
            "cada análise e o que torna a análise visível para você."
        ),
        perguntas=(
            "O que é devolutiva?", "O que é a devolutiva estratégica?", "O que é o relatório estratégico?",
            "O que é a análise executiva?", "O que vem na devolutiva?", "O que é o relatório da consultoria?",
        ),
        termos="devolutiva estrategica relatorio estrategico analise executiva texto consultoria definicao",
        relacionados=("nexo.devolutivas.resumo_executivo", "nexo.devolutivas.conclusao_estrategica",
                      "nexo.devolutivas.quem_prepara"),
        destino="historico",
        fontes=("models.py", "blueprints/admin.py", "templates/cliente/analise.html"),
    ),
    registro(
        id="nexo.devolutivas.quem_prepara",
        titulo="Quem prepara e quem publica a devolutiva?",
        categoria="Devolutiva",
        resumo=(
            "A equipe NEXO. Depois do processamento, o consultor escreve o texto no editor da devolutiva, "
            "pode salvar rascunhos, revisa e então publica. A publicação é sempre uma decisão humana; o "
            "sistema nunca publica sozinho."
        ),
        explicacao=(
            "Para publicar, a análise precisa ter sido processada (status Em análise), sem divergência no total de "
            "vendas, e o resumo executivo e a "
            "conclusão estratégica precisam estar preenchidos. A equipe pode usar a geração assistida por IA "
            "para produzir um rascunho a partir dos números, mas revisa antes de publicar. Também pode "
            "despublicar para corrigir e publicar de novo."
        ),
        perguntas=("Quem escreve a devolutiva?",
                   "Quem publica o relatório?",
                   "A devolutiva é automática?",
                   "Quem faz a análise do meu negócio?",
                   "Quem publica a análise?"),
        termos="quem prepara escreve publica devolutiva consultor equipe humano revisao rascunho",
        relacionados=("nexo.devolutivas.ia", "nexo.devolutivas.publicacao", "nexo.admin.publicar"),
        fontes=("blueprints/admin.py", "ai_devolutiva.py"),
    ),
    registro(
        id="nexo.devolutivas.resumo_executivo",
        titulo="O que é o resumo executivo?",
        categoria="Devolutiva",
        resumo=(
            "É o parágrafo de abertura da devolutiva: a leitura dos números do período (faturamento, compras, "
            "Indicador de Pressão de Estoque, produtos de destaque e concentração da Classe A) em linguagem "
            "direta."
        ),
        perguntas=(
            "O que é resumo executivo?", "O que vem no resumo executivo?", "Para que serve o resumo executivo?",
        ),
        termos="resumo executivo paragrafo leitura numeros abertura",
        relacionados=("nexo.devolutivas.conclusao_estrategica",),
        destino="historico",
        fontes=("models.py", "ai_devolutiva.py", "templates/cliente/analise.html"),
    ),
    registro(
        id="nexo.devolutivas.conclusao_estrategica",
        titulo="O que é a conclusão estratégica?",
        categoria="Devolutiva",
        resumo=(
            "É o fechamento da devolutiva: a prioridade recomendada para o próximo período, como proteger o "
            "abastecimento dos produtos da Classe A ou ajustar o ritmo de compras ao de vendas. É o campo "
            "obrigatório de toda devolutiva publicada."
        ),
        perguntas=(
            "O que é conclusão estratégica?", "O que significa a conclusão da devolutiva?",
            "Qual é a parte mais importante da devolutiva?",
        ),
        termos="conclusao estrategica prioridade proximo periodo fechamento obrigatorio",
        relacionados=("nexo.devolutivas.resumo_executivo", "nexo.devolutivas.interpretar"),
        destino="historico",
        fontes=("models.py", "blueprints/admin.py", "templates/cliente/analise.html"),
    ),
    registro(
        id="nexo.devolutivas.pontos_e_recomendacoes",
        titulo="O que são pontos positivos, pontos de alerta e recomendações?",
        categoria="Devolutiva",
        resumo=(
            "São campos do editor da devolutiva usados pela equipe NEXO ao preparar a análise. No portal do "
            "cliente, os alertas e as recomendações chegam na forma dos semáforos automáticos (crítico, "
            "atenção e oportunidade) e o texto publicado mostra o resumo executivo e a conclusão estratégica."
        ),
        explicacao=(
            "Cada semáforo traz “O que fazer” (a recomendação) e “Por que fazer” (o ponto de alerta ou a "
            "oportunidade), derivados dos indicadores. Assim a leitura fica visual e acionável, e o texto da "
            "devolutiva concentra a interpretação da equipe."
        ),
        perguntas=(
            "O que são pontos positivos?", "O que são pontos de alerta?", "O que são as recomendações?",
            "Onde vejo as recomendações?", "Onde estão os pontos de alerta da minha análise?",
        ),
        termos="pontos positivos alerta recomendacoes campos semaforos onde vejo",
        relacionados=("nexo.indicadores.semaforos", "nexo.devolutivas.interpretar"),
        destino="historico",
        fontes=("models.py", "blueprints/admin.py", "insights.py", "templates/cliente/analise.html", "pdf_export.py"),
    ),
    registro(
        id="nexo.devolutivas.ia",
        titulo="A devolutiva é gerada por inteligência artificial?",
        categoria="Devolutiva",
        resumo=(
            "Pode ter apoio de IA, mas não é uma decisão automática. Quando a integração está configurada, a "
            "equipe NEXO pode gerar um rascunho do resumo executivo e da conclusão a partir dos indicadores; a "
            "equipe revisa e decide publicar. Quando esse apoio foi usado, a análise exibe o selo “Gerado por "
            "Inteligência Artificial Nexo”."
        ),
        explicacao=(
            "O rascunho usa somente os números da análise e é verificado automaticamente: se mencionar lucro, "
            "margem, CMV, rentabilidade ou prejuízo, é descartado. Sem a integração (ou se ela falhar), um "
            "gerador local escreve um texto a partir dos mesmos números. Em todos os casos a publicação é "
            "responsabilidade da equipe, e o texto não deve ser lido como uma decisão financeira validada."
        ),
        perguntas=(
            "A devolutiva é feita por IA?", "O que significa gerado por inteligência artificial nexo?",
            "Posso confiar no texto gerado por IA?", "O NEXO usa IA na devolutiva?", "O selo de IA significa o quê?",
        ),
        termos="ia inteligencia artificial gerado selo rascunho claude automatica confianca",
        relacionados=("nexo.devolutivas.quem_prepara", "nexo.tecnologia.nexo_usa_ia"),
        fontes=("ai_devolutiva.py", "blueprints/admin.py", "templates/cliente/analise.html"),
    ),
    registro(
        id="nexo.devolutivas.publicacao",
        titulo="O que acontece quando a minha análise é publicada?",
        categoria="Devolutiva",
        resumo=(
            "Quando a equipe NEXO publica a devolutiva, você recebe uma notificação no sininho (e um e-mail, "
            "quando o envio de e-mails está ativo). A análise passa a aparecer na Visão Geral e no Histórico, "
            "onde também pode ser exportada em PDF."
        ),
        explicacao=(
            "A publicação muda o status para Concluído e registra a data de publicação, exibida no Histórico. "
            "O aviso e o e-mail só são enviados depois que a publicação é confirmada, para não anunciar algo "
            "que não ficou gravado."
        ),
        perguntas=("O que acontece quando a análise é publicada?",
                   "Como sou avisado da publicação?",
                   "O que muda quando a devolutiva é publicada?",
                   "Vou receber e-mail quando publicar?",
                   "Vou receber e-mail quando a análise for publicada?",
                   "Quando a análise é publicada, recebo e-mail?"),
        termos="publicada publicacao publicar disponivel pronta concluida liberada devolutiva aviso notificacao email resultado",
        relacionados=("nexo.historico.abrir", "nexo.historico.pdf_exportar", "nexo.notificacoes.eventos"),
        destino="historico",
        fontes=("blueprints/admin.py", "notifications.py", "emails.py"),
    ),
    registro(
        id="nexo.devolutivas.quando_acesso",
        titulo="Quando consigo acessar a devolutiva?",
        categoria="Devolutiva",
        resumo=(
            "A partir da publicação pela equipe NEXO. Antes disso a análise existe, mas não é acessível: a "
            "página da análise só abre quando ela está Concluída e com a devolutiva publicada."
        ),
        perguntas=(
            "Quando posso ler a devolutiva?", "Por que não consigo abrir a devolutiva?", "A devolutiva já está disponível?",
        ),
        termos="acessar ler devolutiva quando disponivel abrir",
        relacionados=("nexo.analises.quando_vejo_resultados", "nexo.devolutivas.publicacao"),
        destino="historico",
        fontes=("blueprints/cliente.py",),
    ),
    registro(
        id="nexo.devolutivas.interpretar",
        titulo="Como interpreto as recomendações da devolutiva?",
        categoria="Devolutiva",
        resumo=(
            "Leia a conclusão estratégica como a prioridade do próximo período e Leia a conclusão estratégica "
            "como a prioridade do próximo período e os semáforos como ações diretas, cada um com sua cor de "
            "urgência (vermelho = crítico, amarelo = atenção, verde = oportunidade). Eles aparecem em ordem "
            "fixa de tema (Pressão de Estoque, estoque parado e Classe A), e não por urgência; comece pelos "
            "cards vermelhos, depois os amarelos. Confronte cada recomendação com o que você sabe da loja: "
            "estoque anterior, sazonalidade e negociações em curso.. Confronte cada recomendação com o que você "
            "sabe da loja: estoque anterior, sazonalidade e negociações em curso."
        ),
        explicacao=(
            "As recomendações se apoiam em sinalizações gerenciais estimadas, não em resultado contábil. Elas "
            "indicam onde olhar (ritmo de compras, produtos parados, Classe A), e a decisão final é sua. Dúvidas "
            "sobre uma recomendação específica podem ser levadas à equipe por um chamado em Suporte."
        ),
        perguntas=(
            "Como interpreto as recomendações?", "Devo seguir as recomendações ao pé da letra?",
            "O que fazer com a devolutiva?", "Como aplicar a devolutiva?",
        ),
        termos="interpretar recomendacoes aplicar decisao prioridade devolutiva seguir",
        relacionados=("nexo.indicadores.semaforos", "nexo.indicadores.usar_abc", "nexo.suporte.abrir_chamado"),
        fontes=("insights.py", "ai_devolutiva.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.devolutivas.despublicada",
        titulo="Uma análise que eu já via sumiu do Histórico. O que aconteceu?",
        categoria="Devolutiva",
        resumo=(
            "Provavelmente a equipe NEXO despublicou a devolutiva para corrigir algo: a análise volta ao status "
            "Em análise e sai do Histórico até ser publicada de novo. Se a dúvida persistir, abra um chamado."
        ),
        perguntas=(
            "Minha análise desapareceu do histórico.", "A devolutiva sumiu.", "Por que a análise voltou para em análise?",
        ),
        termos="sumiu desapareceu despublicada despublicar historico voltou",
        relacionados=("nexo.analises.status", "nexo.suporte.abrir_chamado"),
        destino="suporte",
        fontes=("blueprints/admin.py", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.historico.abrir",
        titulo="Como abro o Histórico de análises?",
        categoria="Histórico e PDF",
        tipo="procedimento",
        resumo=(
            "Clique em Histórico no menu lateral. A página lista todas as devolutivas publicadas para a sua "
            "empresa, com período, tipo, data de publicação, status e o botão “Abrir análise”."
        ),
        perguntas=(
            "Como abro o histórico?", "Onde fica o histórico?", "Como vejo minhas análises anteriores?",
            "Onde vejo as análises antigas?", "Como acesso o histórico de análises?",
        ),
        termos="historico abrir onde fica analises anteriores antigas lista",
        relacionados=("nexo.historico.quais_aparecem", "nexo.historico.pdf_exportar"),
        destino="historico",
        fontes=("blueprints/cliente.py", "templates/cliente/historico.html"),
    ),
    registro(
        id="nexo.historico.quais_aparecem",
        titulo="Quais análises aparecem no Histórico?",
        categoria="Histórico e PDF",
        resumo=(
            "Somente as análises da sua empresa com status Concluído e devolutiva publicada, da mais recente "
            "para a mais antiga. Análises em andamento não entram no Histórico: elas aparecem na tabela "
            "“Análises em andamento” da Visão Geral e, enquanto ainda aceitam arquivos (Aguardando relatório ou "
            "Relatório recebido), também em Enviar relatórios."
        ),
        perguntas=(
            "O que aparece no histórico?", "Por que o histórico mostra só algumas análises?",
            "Análises em andamento aparecem no histórico?",
        ),
        termos="historico quais aparecem publicadas concluidas ordem recentes",
        relacionados=("nexo.analises.processada_vs_publicada", "nexo.historico.nao_disponivel"),
        destino="historico",
        fontes=("blueprints/cliente.py",),
    ),
    registro(
        id="nexo.historico.selecionar_periodo",
        titulo="Como seleciono um período no Histórico?",
        categoria="Histórico e PDF",
        tipo="procedimento",
        resumo=(
            "Localize a linha com o período desejado (datas de início e fim) e clique em “Abrir análise”. O "
            "Histórico não tem filtro de busca: as análises ficam em ordem de publicação, da mais recente "
            "para a mais antiga."
        ),
        perguntas=(
            "Como escolho o mês no histórico?", "Como filtro o histórico por período?", "Como encontro a análise de março?",
        ),
        termos="selecionar periodo mes filtro buscar historico escolher",
        relacionados=("nexo.historico.abrir", "nexo.historico.consultar_anterior"),
        destino="historico",
        fontes=("templates/cliente/historico.html",),
    ),
    registro(
        id="nexo.historico.consultar_anterior",
        titulo="Posso consultar uma análise anterior?",
        categoria="Histórico e PDF",
        resumo=(
            "Sim. O Histórico lista todas as análises já publicadas para a sua empresa. Abra uma delas para "
            "ver os detalhes e use a exportação em PDF, se precisar."
        ),
        perguntas=(
            "Posso ver uma análise antiga?", "Como consulto análises passadas?", "Consigo rever a análise do mês passado?",
            "As análises antigas ficam guardadas?",
        ),
        termos="consultar ver rever acessar abrir analise anterior antiga passada historico meses anteriores",
        relacionados=("nexo.historico.abrir", "nexo.historico.pdf_exportar"),
        destino="historico",
        fontes=("blueprints/cliente.py",),
    ),
    registro(
        id="nexo.historico.pdf_exportar",
        titulo="Como exporto uma análise em PDF?",
        categoria="Histórico e PDF",
        tipo="procedimento",
        resumo=(
            "Abra a análise (pelo Histórico ou pelo botão “Abrir análise completa” da Visão Geral) e clique "
            "em “Exportar PDF”, no topo da página. O arquivo é gerado na hora e baixado pelo navegador."
        ),
        passos=(
            "Abra Histórico no menu lateral.",
            "Clique em “Abrir análise” na linha do período desejado.",
            "No topo da página da análise, clique em “Exportar PDF”.",
            "Salve o arquivo baixado; o nome inclui o ano, o mês de referência e o número da análise.",
        ),
        perguntas=(
            "Como exporto o PDF?", "Como baixo a análise em PDF?", "Onde está o botão de exportar?",
            "Posso salvar a devolutiva?", "Como gero o PDF da análise?", "Como imprimo a análise?",
        ),
        detalhes=(
            "Com a conta suspensa sob as regras contratuais, a exportação tem prazo próprio, informado no "
            "aviso da Visão Geral. Encerrado esse prazo, o botão dá lugar a um aviso e a análise autorizada "
            "continua disponível para consulta na tela."
        ),
        termos="pdf exportar baixar download salvar imprimir botao analise",
        relacionados=("nexo.historico.pdf_conteudo", "nexo.historico.abrir"),
        destino="historico",
        fontes=("blueprints/cliente.py", "templates/cliente/analise.html", "pdf_export.py",
                "contract_policy.py"),
    ),
    registro(
        id="nexo.historico.pdf_conteudo",
        titulo="O que o PDF da análise contém?",
        categoria="Histórico e PDF",
        resumo=(
            "O PDF traz o cabeçalho NEXO com a referência, o título da devolutiva, empresa, CNPJ e período, os "
            "indicadores do período, a Curva ABC com os 5 primeiros produtos, o gráfico de pizza com a "
            "participação de cada um no faturamento (e de “Outros produtos”), a síntese executiva em semáforos "
            "e a devolutiva (resumo executivo e conclusão estratégica)."
        ),
        explicacao=(
            "O layout é claro, para impressão, com as mesmas cores das classes ABC e dos semáforos do portal. "
            "O rodapé de cada página traz a numeração (Página X de Y), a data e a hora em que o arquivo foi "
            "gerado, no horário de Brasília, e a nota de que os valores são sinalizações gerenciais estimadas, "
            "sem lucro, margem ou CMV. Cada exportação gera um arquivo novo, com o horário daquela "
            "exportação. Só análises publicadas podem ser exportadas."
        ),
        perguntas=("O que vem no PDF?",
                   "O PDF tem a curva ABC?",
                   "O PDF tem gráfico de pizza?",
                   "Qual horário aparece no PDF?",
                   "O PDF inclui os semáforos?",
                   "Quais informações o arquivo contém?",
                   "O que tem no PDF?",
                   "O que está no PDF?"),
        termos="pdf conteudo contem inclui secoes layout impressao grafico pizza participacao horario brasilia rodape",
        relacionados=("nexo.historico.pdf_exportar", "nexo.devolutivas.o_que_e"),
        destino="historico",
        fontes=("pdf_export.py", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.historico.nao_disponivel",
        titulo="O que faço se uma análise ainda não está disponível no Histórico?",
        categoria="Histórico e PDF",
        resumo=(
            "Confira na Visão Geral, em “Análises em andamento”, o status dela: se ainda estiver Aguardando "
            "relatório, envie os arquivos; se estiver Relatório recebido ou Em análise, a equipe está "
            "trabalhando e você será avisado na publicação. Se algo parecer parado, abra um chamado."
        ),
        perguntas=(
            "A análise não está no histórico.", "Ainda não consigo abrir a análise do mês.",
            "O que fazer enquanto a análise não é publicada?",
        ),
        termos="nao disponivel ainda nao aparece historico esperar status",
        relacionados=("nexo.analises.nao_aparece", "nexo.analises.status"),
        destino="dashboard",
        fontes=("blueprints/cliente.py", "templates/cliente/dashboard.html"),
    ),
)
