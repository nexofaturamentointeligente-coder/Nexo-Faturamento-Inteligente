"""Assunto A — o que é o NEXO, para que serve e como funciona.

Fontes principais: README.md, Docs/RELATORIO_TECNICO.md, templates/landing.html,
templates/como_aderir.html, blueprints/cliente.py e blueprints/admin.py.
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.visao_geral.o_que_e",
        titulo="O que é o NEXO Faturamento Inteligente?",
        categoria="Visão Geral",
        tipo="visao_geral",
        resumo=(
            "O NEXO Faturamento Inteligente é uma plataforma de consultoria de dados para o varejo: "
            "ela transforma os relatórios de Vendas e de Compras exportados do seu PDV em indicadores "
            "gerenciais, gráficos e uma devolutiva estratégica preparada pela equipe NEXO."
        ),
        explicacao=(
            "Você envia pelo portal os relatórios que o seu sistema de caixa (PDV ou ERP) já gera. "
            "A equipe NEXO confere os arquivos e executa o processamento; o sistema calcula o faturamento, "
            "o total comprado, o Indicador de Pressão de Estoque, os produtos de destaque e a Curva ABC. "
            "Em seguida a equipe prepara e publica uma devolutiva estratégica, que você consulta na Visão "
            "Geral, no Histórico e pode exportar em PDF.\n\n"
            "O NEXO não substitui o seu PDV nem é um sistema contábil: ele é uma camada de leitura "
            "estratégica sobre dados que a sua loja já possui."
        ),
        detalhes=(
            "O projeto nasceu como MVP acadêmico voltado a lojas de tintas e pequenos comércios de "
            "construção, reforma e pintura, e evoluiu para uma aplicação com várias empresas clientes "
            "(cada uma vê somente os próprios dados). O trabalho é mediado por consultores: nenhuma "
            "análise é publicada automaticamente."
        ),
        perguntas=(
            "O que é o NEXO?", "O que é esse sistema?", "O que é essa plataforma?", "O que é o portal NEXO?",
            "Me explica o NEXO.", "Qual é a função dessa plataforma?", "O que vocês fazem?",
            "Quem é a NEXO?", "O que significa NEXO Faturamento Inteligente?", "Me fala sobre o NEXO.",
            "O que é faturamento inteligente?",
        ),
        termos="nexo plataforma sistema portal consultoria varejo inteligencia dados faturamento definicao",
        relacionados=("nexo.visao_geral.funcionamento", "nexo.visao_geral.para_que_serve",
                      "nexo.visao_geral.funcionalidades"),
        proximo="nexo.visao_geral.funcionamento",
        destino="dashboard",
        fontes=("README.md", "Docs/RELATORIO_TECNICO.md", "templates/landing.html"),
    ),
    registro(
        id="nexo.visao_geral.funcionamento",
        titulo="Como o NEXO funciona?",
        categoria="Visão Geral",
        tipo="visao_geral",
        resumo=(
            "O NEXO funciona por etapas: a equipe NEXO cadastra a sua empresa e abre um período de análise; "
            "você envia os relatórios de Vendas e Compras; a equipe valida e processa; o sistema calcula os "
            "indicadores; a equipe publica a devolutiva; e você consulta tudo no portal."
        ),
        explicacao=(
            "Cada ciclo começa com uma análise aberta pela equipe NEXO para um período (mensal ou quinzenal). "
            "Enquanto ela está aberta, você anexa os dois relatórios do PDV em Enviar Relatórios. Quando os "
            "dois arquivos foram recebidos, a equipe confere e aciona o processamento, que lê as planilhas e "
            "consolida os indicadores. Depois a equipe escreve e revisa a devolutiva estratégica e a publica. "
            "Só então a análise aparece para você como concluída, com indicadores, gráficos, Curva ABC, "
            "síntese executiva e o texto da devolutiva."
        ),
        passos=(
            "A equipe NEXO cadastra a empresa, cria o seu usuário e abre o período de análise.",
            "Você envia os relatórios de Compras e de Vendas pelo portal, em Enviar Relatórios.",
            "A equipe NEXO confere os arquivos e executa o processamento.",
            "O sistema calcula faturamento, total comprado, Indicador de Pressão de Estoque, produtos de "
            "destaque e a Curva ABC.",
            "A equipe prepara, revisa e publica a devolutiva estratégica.",
            "Você consulta os resultados na Visão Geral, acompanha o Histórico e pode exportar a análise em PDF.",
        ),
        detalhes=(
            "Cada análise passa por quatro estados: Aguardando relatório, Relatório recebido, Em análise e "
            "Concluído. O processamento é acionado pela equipe (não é automático) e a publicação é uma decisão "
            "humana: uma análise processada só fica visível para você depois de publicada. O portal avisa pelo "
            "sininho quando os seus relatórios são processados e quando a devolutiva é publicada (além das "
            "respostas em chamados). Quando o envio de e-mails está configurado, a publicação da devolutiva "
            "também gera um e-mail; as demais etapas não geram e-mail."
        ),
        perguntas=(
            "Como funciona o sistema?", "Como funciona essa plataforma?", "Como funciona o portal?",
            "Quero saber como o sistema funciona.", "Me explica todo o funcionamento.",
            "Como funciona o NEXO?", "Como o NEXO funciona?", "Explique o funcionamento do NEXO.",
            "Como funciona isso aqui?", "Como funciona?", "Quais são as etapas de uma análise?",
            "Como é o processo do NEXO?", "Qual é o fluxo do sistema?", "Como o NEXO ajuda minha empresa?",
            "Como minha empresa utiliza a plataforma?", "Como uso o NEXO?",
        ),
        termos="funcionamento etapas fluxo processo ciclo passos como funciona visao geral completa",
        relacionados=("nexo.uploads.como_enviar", "nexo.indicadores.quais", "nexo.historico.consultar_anterior"),
        proximo="nexo.uploads.como_enviar",
        destino="dashboard",
        fontes=("blueprints/admin.py", "blueprints/cliente.py", "etl_processor.py", "models.py"),
    ),
    registro(
        id="nexo.visao_geral.para_que_serve",
        titulo="Para que serve o NEXO?",
        categoria="Visão Geral",
        tipo="visao_geral",
        resumo=(
            "O NEXO serve para enxergar, a partir dos dados do seu PDV, o descompasso entre compras e vendas, "
            "quais produtos sustentam o faturamento e onde há capital parado em estoque, para decidir melhor "
            "o que comprar e o que priorizar."
        ),
        explicacao=(
            "O problema que ele ataca é comum no varejo: o PDV gera relatórios longos de vendas e compras, mas "
            "o gestor raramente consegue transformá-los em decisão. O NEXO organiza esses dados em poucos "
            "indicadores honestos (faturamento, total comprado, Indicador de Pressão de Estoque, produtos de "
            "destaque e Curva ABC), apresenta semáforos de risco e oportunidade e entrega uma devolutiva "
            "preparada, revisada e publicada pela consultoria (o texto pode ser gerado por IA a partir dos "
            "indicadores; quando isso ocorre, a análise exibe o selo correspondente).."
        ),
        detalhes=(
            "O que o NEXO não faz: não calcula lucro, margem, CMV nem estoque físico real, porque o relatório "
            "do PDV não sustenta esses conceitos sem reconciliação contábil e contagem física. Também não "
            "emite notas fiscais nem controla o caixa: isso continua no seu PDV."
        ),
        perguntas=(
            "Para que serve esse sistema?", "Para que serve essa plataforma?", "Qual problema o NEXO resolve?",
            "Qual é o objetivo do NEXO?", "Como isso ajuda meu negócio?", "O que eu ganho com o NEXO?",
            "Por que usar o NEXO?", "Para que serve isso?", "Qual a utilidade do portal?",
            "Que tipo de decisão o NEXO ajuda a tomar?",
        ),
        termos="serve objetivo problema beneficio utilidade decisao negocio ajuda ganho",
        relacionados=("nexo.visao_geral.beneficios_limitacoes", "nexo.indicadores.pressao_estoque",
                      "nexo.indicadores.curva_abc"),
        destino="dashboard",
        fontes=("README.md", "templates/landing.html", "insights.py"),
    ),
    registro(
        id="nexo.visao_geral.para_quem",
        titulo="Para quais empresas o NEXO foi feito?",
        categoria="Visão Geral",
        tipo="visao_geral",
        resumo=(
            "O NEXO foi projetado inicialmente para lojas de tintas e pequenos comércios de construção, "
            "reforma e pintura, e atende micro e pequenas empresas do varejo que usam um PDV ou ERP capaz "
            "de exportar relatórios de vendas e compras."
        ),
        explicacao=(
            "Cada empresa é cadastrada pela equipe NEXO com um segmento (por exemplo, loja de tintas, "
            "material de construção ou ferragem) e um plano. Os usuários da empresa só enxergam as análises "
            "da própria empresa."
        ),
        perguntas=(
            "Para quem é o NEXO?", "Que tipo de empresa usa o NEXO?", "O NEXO serve para a minha loja?",
            "O NEXO é para que segmento?", "Quais negócios podem usar o portal?",
            "O NEXO foi feito para que tipo de comércio?",
        ),
        termos="empresas segmento varejo loja tintas construcao reforma pintura micro pequena publico alvo",
        relacionados=("nexo.empresas.segmento", "nexo.empresas.cadastro"),
        fontes=("templates/landing.html", "seed.py", "models.py"),
    ),
    registro(
        id="nexo.visao_geral.papeis",
        titulo="Qual é o papel da equipe NEXO e qual é o meu papel?",
        categoria="Visão Geral",
        tipo="visao_geral",
        resumo=(
            "Você, como cliente, envia os relatórios do PDV, acompanha o andamento e lê os resultados. "
            "A equipe NEXO cadastra a empresa, abre as análises, valida e processa os arquivos, escreve a "
            "devolutiva e publica os resultados."
        ),
        explicacao=(
            "O portal tem dois perfis. O perfil CLIENTE acessa a Visão Geral, Enviar Relatórios, Histórico, "
            "Guia, Suporte e Configurações da própria empresa. O perfil ADMIN (equipe NEXO) administra empresas (incluindo o "
            "plano contratado por cada uma), análises, processamento, devolutivas, chamados e a Base de "
            "Conhecimento. Nenhuma dessas operações administrativas pode ser feita pelo cliente ou pelo "
            "NexoBot."
        ),
        perguntas=(
            "O que a equipe NEXO faz?", "O que eu preciso fazer no portal?", "Qual é a minha parte?",
            "Quem faz o quê no NEXO?", "Qual é o papel do consultor?", "O que é responsabilidade do cliente?",
            "O que a consultoria faz por mim?",
        ),
        termos="papel responsabilidade cliente equipe consultor admin consultoria quem faz",
        relacionados=("nexo.admin.o_que_faz", "nexo.acesso.perfis"),
        fontes=("blueprints/cliente.py", "blueprints/admin.py", "templates/base.html"),
    ),
    registro(
        id="nexo.visao_geral.funcionalidades",
        titulo="Quais são as funcionalidades do portal do cliente?",
        categoria="Visão Geral",
        tipo="visao_geral",
        resumo=(
            "O portal do cliente tem estas áreas: Visão Geral (indicadores e devolutiva mais recente), "
            "Enviar Relatórios, Histórico de análises (com exportação em PDF), Raio-X do PDV, Auditoria de "
            "custos (a partir do plano Prata), Guia, Suporte (chamados) e Configurações. "
            "Há ainda o sininho de notificações e este assistente, o NexoBot."
        ),
        explicacao=(
            "Visão Geral: dados da empresa, cards de indicadores, síntese executiva em semáforos, gráficos "
            "(Compras x Vendas, Indicador de Pressão de Estoque, Pareto da Curva ABC e histórico comparativo), "
            "histórico recente e análises em andamento.\n\n"
            "Enviar Relatórios: anexo dos arquivos de Vendas e Compras para cada análise aberta, com o status "
            "de cada arquivo.\n\n"
            "Histórico: todas as análises publicadas; cada uma abre a análise completa, com indicadores, "
            "tabela da Curva ABC, semáforos e devolutiva, e o botão Exportar PDF.\n\n"
            "Raio-X do PDV: a partir dos relatórios de vendas de vários meses, mostra a saúde do cadastro, os "
            "itens vendidos com preços diferentes no mesmo mês, o potencial recuperável e o plano sugerido. "
            "Auditoria de custos: vendas abaixo do custo informado, margem bruta por item e preços que não "
            "acompanharam o custo; aparece com cadeado no menu de quem ainda não tem o plano Prata.\n\n"
            "Guia: tutoriais mantidos pela equipe NEXO. Suporte: abertura e acompanhamento de chamados. "
            "Sininho: avisos de processamento dos relatórios, publicação da devolutiva, respostas da equipe em chamados e chamados marcados como resolvidos."
        ),
        perguntas=(
            "Quais são as funcionalidades?", "O que dá para fazer no portal?", "O que o sistema faz?",
            "Quais recursos existem?", "O que eu encontro no portal?", "Quais páginas existem?",
            "Quais serviços vocês oferecem?", "O que tem no portal do cliente?", "O que faço aqui?",
        ),
        termos="funcionalidades recursos paginas areas menu modulos servicos o que faz",
        relacionados=("nexo.dashboard.visao_geral", "nexo.uploads.como_enviar", "nexo.historico.abrir",
                      "nexo.suporte.abrir_chamado"),
        destino="dashboard",
        fontes=("templates/base.html", "blueprints/cliente.py", "blueprints/onboarding.py", "entitlements.py"),
    ),
    registro(
        id="nexo.visao_geral.o_que_nao_e",
        titulo="O NEXO emite notas fiscais, é um ERP ou substitui o meu PDV?",
        categoria="Visão Geral",
        tipo="regra",
        resumo=(
            "Não. O NEXO não emite notas fiscais, não é um ERP nem um sistema contábil e não substitui o "
            "seu PDV: ele lê os relatórios que o seu PDV exporta e devolve indicadores e uma devolutiva."
        ),
        explicacao=(
            "O seu PDV continua responsável por registrar vendas, compras, caixa, estoque físico e documentos "
            "fiscais. O NEXO trabalha depois disso, com os relatórios exportados, e produz leitura gerencial: "
            "faturamento, total comprado, Indicador de Pressão de Estoque, produtos de destaque, Curva ABC, "
            "semáforos e devolutiva. Por regra do produto, as análises nunca apresentam lucro, margem, CMV ou "
            "rentabilidade; a margem bruta estimada sobre o custo informado aparece só no Raio-X do PDV, a "
            "partir do plano Prata."
        ),
        perguntas=(
            "O NEXO emite notas fiscais?", "O NEXO é um ERP?", "O NEXO substitui meu PDV?",
            "O NEXO é um sistema contábil?", "Posso emitir nota pelo NEXO?", "O NEXO faz o controle do caixa?",
            "O NEXO controla meu estoque?", "O NEXO é um sistema de gestão?", "O NEXO faz contabilidade?",
        ),
        termos="nota fiscal nf erp pdv contabil contabilidade caixa estoque fisico substitui nao eh",
        relacionados=("nexo.visao_geral.o_que_e", "nexo.indicadores.sem_lucro"),
        fontes=("README.md", "etl_processor.py", "ai_devolutiva.py"),
    ),
    registro(
        id="nexo.visao_geral.beneficios_limitacoes",
        titulo="Quais são os benefícios e as limitações do NEXO?",
        categoria="Visão Geral",
        tipo="visao_geral",
        resumo=(
            "Benefícios: leitura rápida e honesta dos seus dados de vendas e compras, com indicadores, Curva ABC, "
            "semáforos e devolutiva da consultoria. Limitações: depende da qualidade dos relatórios do PDV, "
            "não calcula lucro ou estoque real e os resultados chegam no ritmo da equipe, não em tempo real."
        ),
        explicacao=(
            "Entre os benefícios estão a visão do descompasso entre compras e vendas, a identificação dos "
            "produtos que concentram o faturamento (Classe A), o histórico comparativo ao longo das análises "
            "e a exportação em PDF para compartilhar.\n\n"
            "Entre as limitações: os indicadores usam apenas o que o relatório do PDV informa (se o relatório "
            "de compras não traz produto e quantidade, por exemplo, o saldo estimado parado não é calculado); "
            "o processamento e a publicação são feitos pela equipe NEXO; e o Indicador de Pressão de Estoque "
            "é uma sinalização estimada, não um número contábil."
        ),
        perguntas=(
            "Quais são as vantagens do NEXO?", "Quais são as limitações do sistema?", "O que o NEXO não faz?",
            "Quais os limites da plataforma?", "Vale a pena usar o NEXO?", "O que o NEXO não consegue calcular?",
        ),
        termos="beneficios vantagens limitacoes limites restricoes nao faz nao calcula",
        relacionados=("nexo.indicadores.limitacoes", "nexo.visao_geral.o_que_nao_e"),
        fontes=("README.md", "etl_processor.py", "insights.py"),
    ),
)
