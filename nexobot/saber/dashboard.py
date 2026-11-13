"""Assunto K — a Visão Geral (dashboard) e seus componentes.

Fontes: blueprints/cliente.py (dashboard, _abc_chart, _historico_temporal,
_auditoria), templates/cliente/dashboard.html, templates/_auditoria.html,
templates/_macros.html, templates/base.html (gráficos Plotly).
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.dashboard.visao_geral",
        titulo="O que mostra a Visão Geral?",
        categoria="Visão Geral do painel",
        resumo=(
            "A Visão Geral é a primeira página do portal: mostra os dados da sua empresa, a análise publicada "
            "mais recente (indicadores, semáforos, gráficos e Curva ABC), o histórico recente, as análises em "
            "andamento e um card de contato."
        ),
        explicacao=(
            "De cima para baixo: barra de origem dos dados; aviso de conta suspensa (quando for o caso); "
            "cartão da empresa (razão social, CNPJ, plano e status); cards de faturamento total, total "
            "comprado e Indicador de Pressão de Estoque; síntese executiva em semáforos; cards de produto "
            "mais vendido, maior faturamento e maior saldo estimado parado; gráficos Compras x Vendas e "
            "Indicador de Pressão de Estoque; card do relatório estratégico com botão para a análise "
            "completa; gráfico de Pareto da Curva ABC; histórico comparativo; tabela de histórico recente; "
            "tabela de análises em andamento; e o card Contato NEXO.\n\n"
            "Sem análise publicada, a página mostra “Sua primeira análise está em preparação”."
        ),
        perguntas=(
            "O que é a Visão Geral?", "O que tem no dashboard?", "O que aparece na página inicial?",
            "O que é o painel?", "O que mostra a tela inicial?", "Como é a Visão Geral?",
        ),
        termos="visao geral dashboard painel pagina inicial tela inicial home componentes mostra",
        relacionados=("nexo.dashboard.cards", "nexo.dashboard.graficos", "nexo.dashboard.sem_analise"),
        destino="dashboard",
        fontes=("blueprints/cliente.py", "templates/cliente/dashboard.html"),
    ),
    registro(
        id="nexo.dashboard.cards",
        titulo="O que são os cards de indicadores?",
        categoria="Visão Geral do painel",
        resumo=(
            "São os blocos numéricos da Visão Geral: faturamento total, total comprado e Indicador de Pressão "
            "de Estoque (em reais), e produto mais vendido (unidades), maior faturamento (reais) e maior saldo "
            "estimado parado (unidades)."
        ),
        explicacao=(
            "Abaixo dos três primeiros cards há a nota fixa do portal: “Sinalização gerencial estimada. Não "
            "representa lucro, margem, CMV nem estoque físico real.” O card de saldo parado mostra “Não "
            "calculado” quando o relatório de Compras não traz produto e quantidade."
        ),
        perguntas=(
            "O que são os cards do painel?", "O que significa cada card?", "Quais números aparecem nos cards?",
            "O que é o card de maior saldo parado?",
        ),
        termos="cards blocos kpi numeros painel tiles valores",
        relacionados=("nexo.indicadores.quais", "nexo.indicadores.saldo_parado"),
        destino="dashboard",
        fontes=("templates/cliente/dashboard.html", "templates/_macros.html"),
    ),
    registro(
        id="nexo.dashboard.graficos",
        titulo="Quais gráficos existem no portal e como são gerados?",
        categoria="Visão Geral do painel",
        resumo=(
            "Quatro gráficos: Compras x Vendas (barras), Indicador de Pressão de Estoque (número com cor "
            "conforme o sinal), Pareto da Curva ABC (barras com linha de percentual acumulado, 10 primeiros "
            "produtos) e histórico comparativo Vendas x Compras (linhas, a partir da 2ª análise publicada)."
        ),
        explicacao=(
            "Os gráficos são desenhados no navegador a partir dos indicadores já calculados; eles não "
            "recalculam nada. Na página de uma análise individual aparecem o comparativo e o Pareto daquela "
            "análise, Na página de uma análise individual aparecem o comparativo e o Pareto daquela análise, além da tabela da Curva ABC com filtro por classe, que lista todos os produtos do período, do maior para o menor faturamento; o gráfico de Pareto mostra os 10 primeiros.."
        ),
        perguntas=(
            "Quais gráficos existem?", "Como os gráficos são gerados?", "O que são os gráficos do painel?",
            "Por que o gráfico de histórico não aparece?", "Como leio os gráficos?",
        ),
        termos="graficos grafico barras linhas pareto historico comparativo plotly gerados",
        relacionados=("nexo.indicadores.comparativo", "nexo.indicadores.pareto", "nexo.dashboard.historico_comparativo"),
        destino="dashboard",
        fontes=("templates/cliente/dashboard.html", "templates/base.html", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.dashboard.historico_comparativo",
        titulo="O que é o histórico comparativo Vendas x Compras?",
        categoria="Visão Geral do painel",
        resumo=(
            "É o gráfico de linhas que mostra o faturamento e o total comprado de cada análise publicada, em "
            "ordem cronológica. Ele só aparece a partir da segunda análise publicada, porque com uma única "
            "análise não há evolução para mostrar."
        ),
        perguntas=(
            "O que é o histórico comparativo?", "Por que o histórico comparativo está vazio?",
            "Como vejo a evolução das vendas ao longo dos meses?", "O gráfico de linhas mostra o quê?",
        ),
        termos="historico comparativo evolucao meses linhas segunda analise tempo",
        relacionados=("nexo.dashboard.graficos", "nexo.historico.quais_aparecem"),
        destino="dashboard",
        fontes=("blueprints/cliente.py", "templates/cliente/dashboard.html"),
    ),
    registro(
        id="nexo.dashboard.informacoes_empresa",
        titulo="Que informações da empresa aparecem no painel?",
        categoria="Visão Geral do painel",
        resumo=(
            "O cartão no topo da Visão Geral mostra razão social, CNPJ, plano (Bronze, Prata ou Ouro) e "
            "status da conta (Ativa, Suspensa ou Cancelada). Ao lado do seu nome, no topo, aparece o perfil "
            "(Cliente)."
        ),
        perguntas=(
            "Onde vejo o nome da minha empresa?", "O que é o cartão da empresa?", "Onde aparece o status da conta?",
        ),
        termos="informacoes empresa cartao razao social cnpj plano status perfil",
        relacionados=("nexo.empresas.dados_cadastrais", "nexo.empresas.status_conta"),
        destino="dashboard",
        fontes=("templates/cliente/dashboard.html", "templates/base.html"),
    ),
    registro(
        id="nexo.dashboard.auditoria",
        titulo="O que é a barra de origem dos dados?",
        categoria="Visão Geral do painel",
        resumo=(
            "É a faixa no topo da Visão Geral e de cada análise que informa com que base a análise foi feita: "
            "a empresa, o período dos dados e os arquivos importados de Vendas e Compras."
        ),
        explicacao=(
            "Quando o processamento consegue ler o período no cabeçalho do relatório do PDV, a barra mostra "
            "essas datas com um ícone indicando que foram lidas automaticamente; caso contrário, mostra o "
            "período declarado da análise. Serve para você conferir se o arquivo certo foi usado."
        ),
        perguntas=(
            "O que é a barra de auditoria?", "O que significa período dos dados?", "O que é base de dados no topo da página?",
            "O que é arquivo importado?", "O que é o Motor ETL Nexo?",
        ),
        termos="auditoria origem dados barra periodo arquivos importados base motor etl",
        relacionados=("nexo.indicadores.de_onde_vem", "nexo.analises.periodo_referencia"),
        destino="dashboard",
        fontes=("blueprints/cliente.py", "templates/_auditoria.html"),
    ),
    registro(
        id="nexo.dashboard.sem_analise",
        titulo="Por que a Visão Geral diz que minha primeira análise está em preparação?",
        categoria="Visão Geral do painel",
        resumo=(
            "Porque ainda não existe nenhuma análise publicada para a sua empresa. Os indicadores e gráficos "
            "só aparecem depois que a equipe NEXO processa os relatórios e publica a devolutiva."
        ),
        explicacao=(
            "Enquanto isso, a tabela “Análises em andamento” mostra o status do período aberto. Se você ainda "
            "não enviou os relatórios, comece por Enviar Relatórios."
        ),
        perguntas=(
            "Por que meu painel está vazio?", "Primeira análise em preparação, o que significa?",
            "Por que não vejo indicadores?", "Quando o painel vai mostrar dados?",
        ),
        termos="painel vazio primeira analise preparacao sem dados indicadores nao aparecem",
        relacionados=("nexo.analises.nao_aparece", "nexo.uploads.como_enviar"),
        destino="upload",
        fontes=("templates/cliente/dashboard.html", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.dashboard.analise_completa",
        titulo="Como consulto a análise completa?",
        categoria="Visão Geral do painel",
        tipo="procedimento",
        resumo=(
            "Na Visão Geral, clique em “Abrir análise completa” no card do relatório estratégico, ou abra o "
            "Histórico e clique em “Abrir análise” na linha do período desejado."
        ),
        explicacao=(
            "A página da análise traz: barra de origem dos dados, indicadores do período, gráficos, a tabela "
            "da Curva ABC com filtro por classe, a síntese executiva em semáforos, a devolutiva (resumo "
            "executivo e conclusão estratégica) e os botões Exportar PDF, Histórico e Voltar ao painel."
        ),
        perguntas=(
            "Como abro a análise completa?", "Onde vejo a devolutiva inteira?", "Como vejo todos os produtos da curva ABC?",
            "Onde está o botão abrir análise?",
        ),
        termos="analise completa abrir detalhe pagina da analise devolutiva inteira tabela",
        relacionados=("nexo.historico.abrir", "nexo.historico.pdf_exportar"),
        destino="historico",
        fontes=("templates/cliente/dashboard.html", "templates/cliente/analise.html", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.dashboard.interpretar_valores",
        titulo="Como interpreto os valores em reais e em unidades do painel?",
        categoria="Visão Geral do painel",
        resumo=(
            "Faturamento, total comprado, Indicador de Pressão de Estoque e maior faturamento são valores em "
            "reais (R$) do período. Produto mais vendido e saldo estimado parado são quantidades em unidades. "
            "Percentuais da Curva ABC são fatias do faturamento."
        ),
        explicacao=(
            "Não misture as duas medidas: um produto pode ser o mais vendido em unidades sem ser o de maior "
            "faturamento, e o saldo parado em unidades não se converte automaticamente em reais. Nos cards e "
            "nas tabelas, os valores usam separador de milhar com ponto e decimais com vírgula, no padrão "
            "brasileiro. Nos gráficos, os eixos, o número do Indicador de Pressão de Estoque e algumas dicas ao "
            "passar o mouse podem aparecer no formato internacional (vírgula para milhar e ponto para "
            "decimais)."
        ),
        perguntas=(
            "Os valores são em reais?", "O que é unidade no painel?", "Como leio os números do painel?",
            "O saldo parado é em reais ou unidades?",
        ),
        termos="valores reais unidades un r$ percentual interpretar formato numeros",
        relacionados=("nexo.indicadores.interpretar", "nexo.indicadores.saldo_parado"),
        destino="dashboard",
        fontes=("templates/cliente/dashboard.html", "templates/_macros.html"),
    ),
    registro(
        id="nexo.dashboard.contato",
        titulo="O que é o card Contato NEXO no fim da Visão Geral?",
        categoria="Visão Geral do painel",
        resumo=(
            "É um lembrete de que dúvidas sobre a análise podem ser tratadas com a equipe NEXO. Dentro do portal, "
            "o canal oficial é a página Suporte, onde você abre chamados e acompanha respostas. Quando a equipe "
            "publica o WhatsApp comercial, o card mostra também o link Atendimento pelo WhatsApp, que abre a "
            "conversa com uma mensagem pronta; sem ele, use o canal externo informado no onboarding."
        ),
        perguntas=(
            "Qual é o WhatsApp da NEXO?", "Qual é o e-mail de contato?", "Como falo pelo card de contato?",
            "O que é o card contato nexo?",
        ),
        termos="contato card whatsapp email canal externo onboarding",
        relacionados=("nexo.suporte.falar_com_equipe", "nexo.suporte.abrir_chamado"),
        destino="suporte",
        fontes=("templates/cliente/dashboard.html",),
    ),
)
