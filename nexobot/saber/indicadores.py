"""Assuntos I e J — indicadores, semáforos e Curva ABC.

Fontes: etl_processor.py (_calcular_kpis_local, _classificar_curva_abc,
ABC_LIMITE_A/B), insights.py (PRESSAO_CRITICA/ATENCAO),
models.py (IndicadorAnalise, ProdutoCurvaABC), templates/_macros.html
(nota_pressao, nota_abc), templates/cliente/dashboard.html e analise.html.

REGRA CRÍTICA: o Indicador de Pressão de Estoque é total comprado menos
faturamento. Não é lucro, margem, CMV, estoque físico nem resultado contábil.
"""

from .modelo import registro

AVISO_PRESSAO = (
    "O Indicador de Pressão de Estoque é uma sinalização gerencial estimada: não representa lucro, margem, "
    "CMV nem estoque físico real."
)

REGISTROS = (
    registro(
        id="nexo.indicadores.quais",
        titulo="Quais indicadores o NEXO calcula?",
        categoria="Indicadores",
        resumo=(
            "Faturamento total, total comprado, Indicador de Pressão de Estoque, produto mais vendido (em "
            "quantidade), produto de maior faturamento, produto com maior saldo estimado parado, a Curva ABC "
            "dos produtos e, a partir deles, os semáforos da síntese executiva."
        ),
        explicacao=(
            "Todos vêm dos dois relatórios do período: as vendas por produto geram o faturamento, os produtos "
            "de destaque e a Curva ABC; as compras geram o total comprado; a diferença entre os dois gera o "
            "Indicador de Pressão de Estoque; e o cruzamento das quantidades compradas e vendidas gera o saldo "
            "estimado parado, quando o relatório de compras traz produto e quantidade."
        ),
        detalhes=(
            "O que não existe por regra do produto: lucro, margem, CMV, rentabilidade, prejuízo e estoque "
            "físico real. Esses conceitos exigiriam dados contábeis e contagem física que o relatório do PDV "
            "não fornece."
        ),
        perguntas=(
            "Quais indicadores existem?", "O que o NEXO mede?", "Quais são os KPIs?", "Que indicadores vejo no painel?",
            "Quais métricas o sistema calcula?", "Quero entender meus indicadores.", "Me explica os indicadores.",
        ),
        termos="indicadores kpis metricas calcula mede lista quais painel",
        relacionados=("nexo.indicadores.faturamento_total", "nexo.indicadores.pressao_estoque",
                      "nexo.indicadores.curva_abc"),
        proximo="nexo.indicadores.pressao_estoque",
        destino="dashboard",
        fontes=("etl_processor.py", "models.py", "templates/cliente/dashboard.html"),
    ),
    registro(
        id="nexo.indicadores.faturamento_total",
        titulo="O que é o faturamento total?",
        categoria="Indicadores",
        resumo=(
            "É a soma, em reais, do valor de venda de todas as linhas de produto do relatório de Vendas do "
            "período. Representa quanto a loja vendeu, não quanto lucrou."
        ),
        explicacao=(
            "O valor de cada linha vem da coluna de total de venda do PDV ou, quando ela não existe, de "
            "quantidade vezes preço unitário. Linhas que não são produto (descontos, frete, troco, totais) "
            "ficam de fora. O percentual acumulado da Curva ABC também parte do faturamento por produto, considerando apenas os produtos com faturamento positivo; por isso, se o relatório tiver devoluções ou estornos, a base da Curva ABC pode diferir ligeiramente do faturamento total do card."
        ),
        perguntas=(
            "O que é faturamento?", "O que significa faturamento total?", "Como o faturamento é calculado?",
            "Faturamento é lucro?", "De onde vem o faturamento total?",
        ),
        termos="faturamento total vendas receita bruta soma valor vendido calculado",
        relacionados=("nexo.indicadores.total_comprado", "nexo.indicadores.pressao_estoque",
                      "nexo.indicadores.sem_lucro"),
        destino="dashboard",
        fontes=("etl_processor.py",),
    ),
    registro(
        id="nexo.indicadores.total_comprado",
        titulo="O que significa o total comprado?",
        categoria="Indicadores",
        resumo=(
            "É a soma, em reais, das compras do período registradas no relatório de Compras: o valor dos "
            "produtos das notas de fornecedores ou, se o relatório for por item, o total de cada item comprado."
        ),
        explicacao=(
            "Quando o relatório traz a coluna de total de produtos da nota, ela tem preferência sobre o total "
            "da nota (que pode incluir frete e impostos). Sem coluna de total, o valor é derivado de quantidade "
            "comprada vezes custo unitário. O total comprado é comparado ao faturamento no Indicador de "
            "Pressão de Estoque."
        ),
        perguntas=(
            "O que é total comprado?", "Como é calculado o total comprado?", "O que entra no total comprado?",
            "Total comprado inclui frete?", "O que significa volume de compras?",
        ),
        termos="total comprado compras volume entradas notas fornecedores soma calculado",
        relacionados=("nexo.indicadores.faturamento_total", "nexo.indicadores.pressao_estoque"),
        destino="dashboard",
        fontes=("etl_processor.py",),
    ),
    registro(
        id="nexo.indicadores.pressao_estoque",
        titulo="O que significa o Indicador de Pressão de Estoque?",
        categoria="Indicadores",
        resumo=(
            "É a diferença entre o total comprado e o faturamento do período: total comprado menos "
            "faturamento total. Valor positivo indica que entrou mais mercadoria (em reais) do que saiu; "
            "valor negativo indica que se vendeu mais do que se comprou no período."
        ),
        explicacao=(
            "O indicador aponta o descompasso entre compras e vendas em um mesmo período. Quando positivo e "
            "grande em relação ao faturamento, sugere capital parado em estoque; o portal classifica acima de "
            "15% do faturamento como crítico e entre 5% e 15% como atenção. Quando negativo, o portal rotula "
            "como “giro saudável”, porque as vendas superaram as compras no período; mas a leitura correta "
            "depende do estoque que a loja já tinha e da sazonalidade, então um valor negativo não garante, "
            "por si só, saúde de estoque."
        ),
        detalhes=(
            "O que o indicador NÃO é: não é lucro nem prejuízo (compras e vendas de um mês não são custo das "
            "mesmas unidades vendidas), não é margem de contribuição, não é CMV, não é o valor do estoque "
            "físico e não é resultado contábil. Ele compara valores em reais, não quantidades físicas: para "
            "quantidades por produto, veja o saldo estimado parado."
        ),
        exemplo=(
            "Exemplo hipotético: uma loja comprou R$ 60.000 e faturou R$ 50.000 no mês. O indicador é "
            "R$ 10.000 (positivo), 20% do faturamento, o que o portal sinaliza como crítico. Se tivesse "
            "comprado R$ 45.000, o indicador seria R$ -5.000 (negativo): vendeu mais do que comprou no período."
        ),
        aviso=AVISO_PRESSAO,
        perguntas=(
            "O que é pressão de estoque?", "O que significa pressão de estoque?", "Não entendi a pressão de estoque.",
            "Como é calculada a pressão de estoque?", "Pressão de estoque positiva é ruim?",
            "O que quer dizer o indicador de pressão de estoque?", "Pressão de estoque negativa é boa?",
            "Explique a pressão de estoque.", "O que é o saldo estimado entre compras e vendas?",
        ),
        termos="indicador pressao estoque significa significado saldo positivo negativo giro saudavel equilibrio descompasso",
        relacionados=("nexo.indicadores.pressao_nao_e_lucro", "nexo.indicadores.semaforos",
                      "nexo.indicadores.saldo_parado"),
        proximo="nexo.indicadores.curva_abc",
        destino="dashboard",
        fontes=("etl_processor.py", "insights.py", "templates/_macros.html", "templates/cliente/dashboard.html"),
    ),
    registro(
        id="nexo.indicadores.pressao_nao_e_lucro",
        titulo="Pressão de estoque é lucro ou prejuízo?",
        categoria="Indicadores",
        tipo="regra",
        resumo=(
            "Não. O Indicador de Pressão de Estoque compara o que foi comprado com o que foi vendido no período, "
            "em reais. Lucro, prejuízo, margem e CMV dependem do custo das unidades efetivamente vendidas e de "
            "dados contábeis que o relatório do PDV não traz; por isso o NEXO não os calcula."
        ),
        explicacao=(
            "Comprar mais do que vender em um mês pode ser estoque para a alta temporada; vender mais do que "
            "comprar pode ser consumo de estoque antigo. O indicador mostra o movimento do período, e a "
            "interpretação precisa considerar o contexto da loja."
        ),
        aviso=AVISO_PRESSAO,
        perguntas=(
            "Pressão de estoque é lucro?", "Esse indicador é meu lucro?", "Se o indicador é negativo eu tive prejuízo?",
            "Pressão de estoque mostra margem?", "O NEXO calcula o lucro?",
        ),
        termos="lucro prejuizo margem cmv rentabilidade nao eh pressao estoque diferenca",
        relacionados=("nexo.indicadores.sem_lucro", "nexo.indicadores.pressao_estoque"),
        fontes=("etl_processor.py", "insights.py", "README.md"),
    ),
    registro(
        id="nexo.indicadores.sem_lucro",
        titulo="O NEXO calcula lucro, margem ou CMV?",
        categoria="Indicadores",
        tipo="regra",
        resumo=(
            "Não, por regra do produto. O NEXO trabalha apenas com faturamento, total comprado, o "
            "descompasso entre eles (Indicador de Pressão de Estoque), produtos de destaque e Curva ABC. "
            "Lucro, margem, CMV, rentabilidade e estoque físico exigem dados que o relatório do PDV não sustenta."
        ),
        explicacao=(
            "Essa regra vale em todas as camadas: o processamento descarta colunas de lucro, Essa regra vale em "
            "todas as camadas: o processamento descarta colunas de lucro, a devolutiva gerada com apoio de IA é "
            "verificada automaticamente contra esses termos antes de ser aproveitada e os semáforos nunca "
            "afirmam rentabilidade. O texto final da devolutiva é revisado e publicado pela equipe NEXO, que "
            "segue a mesma regra. e os semáforos nunca afirmam rentabilidade. Se o seu PDV informa custo, o "
            "sistema pode apenas apontar à equipe que houve itens vendidos abaixo do custo informado, como "
            "observação do dado bruto."
        ),
        perguntas=(
            "O NEXO calcula lucro?", "Onde vejo minha margem?", "Qual é o meu CMV?", "O portal mostra rentabilidade?",
            "Por que não aparece o lucro?", "O NEXO mostra prejuízo?",
        ),
        termos="lucro margem cmv rentabilidade prejuizo nao calcula regra proibido",
        relacionados=("nexo.indicadores.pressao_nao_e_lucro", "nexo.visao_geral.o_que_nao_e"),
        fontes=("etl_processor.py", "ai_devolutiva.py", "insights.py"),
    ),
    registro(
        id="nexo.indicadores.produto_mais_vendido",
        titulo="O que é o produto mais vendido?",
        categoria="Indicadores",
        resumo=(
            "É o produto com a maior quantidade vendida no período, somando todas as linhas do relatório de "
            "Vendas com o mesmo nome padronizado. O card mostra o nome e a quantidade em unidades."
        ),
        perguntas=(
            "O que significa produto mais vendido?", "Como é definido o produto mais vendido?",
            "Produto mais vendido é por quantidade ou por valor?",
        ),
        termos="produto mais vendido quantidade unidades campeao volume",
        relacionados=("nexo.indicadores.produto_maior_faturamento", "nexo.indicadores.curva_abc"),
        destino="dashboard",
        fontes=("etl_processor.py", "templates/cliente/dashboard.html"),
    ),
    registro(
        id="nexo.indicadores.produto_maior_faturamento",
        titulo="O que é o produto de maior faturamento?",
        categoria="Indicadores",
        resumo=(
            "É o produto que mais somou valor de venda no período. Quando ele coincide com o produto mais "
            "vendido em quantidade, o card mostra o segundo colocado do ranking de faturamento, para não "
            "repetir o mesmo item em dois cards."
        ),
        explicacao=(
            "Essa escolha é apenas de apresentação: Essa escolha é apenas de apresentação: a Curva ABC continua "
            "mostrando o topo do ranking por faturamento (10 produtos no gráfico e todos na tabela da análise "
            "completa), com o líder em primeiro lugar.."
        ),
        perguntas=(
            "O que é maior faturamento?", "Por que o produto de maior faturamento é diferente do líder da Curva ABC?",
            "Como é escolhido o produto de maior faturamento?",
        ),
        termos="produto maior faturamento valor ranking segundo colocado card",
        relacionados=("nexo.indicadores.produto_mais_vendido", "nexo.indicadores.curva_abc"),
        destino="dashboard",
        fontes=("etl_processor.py",),
    ),
    registro(
        id="nexo.indicadores.saldo_parado",
        titulo="O que é o produto com maior saldo estimado parado?",
        categoria="Indicadores",
        resumo=(
            "É o produto cuja quantidade comprada no período mais superou a quantidade vendida no mesmo "
            "período (em unidades). É uma estimativa de mercadoria que entrou e ainda não girou, calculada "
            "apenas quando o relatório de Compras traz produto e quantidade."
        ),
        explicacao=(
            "Se o relatório de Compras vier por nota fiscal (fornecedor e valor), sem produto e quantidade, o "
            "card mostra “Não calculado”. O indicador compara unidades do período e não considera o estoque "
            "anterior, então não é o estoque físico atual."
        ),
        aviso="Estimativa em unidades compradas e não vendidas no período; não é contagem de estoque físico.",
        perguntas=(
            "O que é saldo estimado parado?", "O que significa maior saldo parado?", "Por que aparece não calculado?",
            "O que é estoque parado no painel?",
        ),
        termos="saldo estimado parado produto unidades nao calculado estoque parado",
        relacionados=("nexo.indicadores.pressao_estoque", "nexo.uploads.colunas_necessarias"),
        destino="dashboard",
        fontes=("etl_processor.py", "templates/cliente/dashboard.html"),
    ),
    registro(
        id="nexo.indicadores.comparativo",
        titulo="O que mostra o gráfico Compras x Vendas?",
        categoria="Indicadores",
        resumo=(
            "Duas barras em reais: o total comprado e o faturamento do período da análise mais recente. A "
            "diferença entre elas é exatamente o Indicador de Pressão de Estoque."
        ),
        explicacao=(
            "Na Visão Geral há também o histórico comparativo, uma linha de vendas e outra de compras ao "
            "longo das análises publicadas, que aparece a partir da segunda análise publicada."
        ),
        perguntas=(
            "O que é o gráfico compras x vendas?", "Como leio o comparativo de compras e vendas?",
            "O que significam as barras do painel?",
        ),
        termos="grafico comparativo compras vendas barras historico linha",
        relacionados=("nexo.indicadores.pressao_estoque", "nexo.dashboard.historico_comparativo"),
        destino="dashboard",
        fontes=("templates/cliente/dashboard.html", "blueprints/cliente.py"),
    ),
    registro(
        id="nexo.indicadores.de_onde_vem",
        titulo="De onde vêm os números dos meus indicadores?",
        categoria="Indicadores",
        resumo=(
            "Exclusivamente dos dois relatórios (Vendas e Compras) anexados à análise, enviados por você ou "
            "pela equipe NEXO em nome da sua empresa, e processados pela equipe NEXO. A barra de origem dos "
            "dados, no topo da Visão Geral e de cada análise, mostra a empresa, o período e os nomes dos "
            "arquivos usados."
        ),
        perguntas=(
            "De onde vêm os números?", "Qual a origem dos dados do painel?", "Os indicadores usam que arquivos?",
            "Como sei qual arquivo gerou a análise?",
        ),
        termos="origem numeros dados arquivos fonte auditoria base",
        relacionados=("nexo.dashboard.auditoria", "nexo.processamento.o_que_e_etl"),
        destino="dashboard",
        fontes=("blueprints/cliente.py", "templates/_auditoria.html"),
    ),
    registro(
        id="nexo.indicadores.limitacoes",
        titulo="Quais limitações os indicadores possuem?",
        categoria="Indicadores",
        resumo=(
            "Eles refletem apenas o que está nos relatórios do período: não consideram estoque anterior, "
            "não distinguem custo das unidades vendidas e dependem da qualidade da exportação do PDV. São "
            "sinalizações gerenciais, não números contábeis."
        ),
        explicacao=(
            "Produtos escritos de formas muito diferentes podem ser contados separadamente; relatórios de "
            "compras sem produto e quantidade impedem o saldo estimado parado; períodos exportados fora do "
            "intervalo distorcem o comparativo. Por isso vale conferir a barra de origem dos dados e a "
            "devolutiva da equipe, que contextualiza os números."
        ),
        perguntas=(
            "Quais são as limitações dos indicadores?", "Os números são exatos?", "Posso confiar nos indicadores?",
            "Por que os indicadores podem estar diferentes do meu PDV?",
        ),
        termos="limitacoes limites exatos confiar diferentes pdv qualidade sinalizacao",
        relacionados=("nexo.indicadores.pressao_nao_e_lucro", "nexo.visao_geral.beneficios_limitacoes"),
        fontes=("etl_processor.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.indicadores.semaforos",
        titulo="O que são os semáforos da síntese executiva?",
        categoria="Indicadores",
        resumo=(
            "São cards de risco e oportunidade gerados automaticamente a partir dos indicadores, com “O que "
            "fazer” e “Por que fazer”. Vermelho é crítico, amarelo é atenção e verde é oportunidade."
        ),
        explicacao=(
            "Três leituras alimentam os cards: o Indicador de Pressão de Estoque em relação ao faturamento "
            "(acima de 15% é crítico, entre 5% e 15% é atenção, abaixo disso é oportunidade de giro saudável); "
            "o produto com maior saldo estimado parado (atenção, quando existe); e a concentração do "
            "faturamento nos produtos da Classe A (oportunidade de blindar abastecimento e precificação)."
        ),
        perguntas=(
            "O que são os semáforos?", "O que é a síntese executiva?", "O que significa o card vermelho?",
            "O que é crítico, atenção e oportunidade?", "O que é 5W2H no painel?",
        ),
        termos="semaforos sintese executiva cards risco oportunidade critico atencao 5w2h o que fazer por que",
        relacionados=("nexo.indicadores.pressao_estoque", "nexo.indicadores.classe_a"),
        destino="dashboard",
        fontes=("insights.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.indicadores.curva_abc",
        titulo="O que é a Curva ABC?",
        categoria="Curva ABC",
        resumo=(
            "A Curva ABC ordena os produtos do maior para o menor faturamento e os classifica em três classes: "
            "Classe A, os estratégicos que somam até 80% do faturamento acumulado; Classe B, os táticos, de 80% "
            "a 95%; e Classe C, os operacionais, os cerca de 5% restantes (cauda longa)."
        ),
        explicacao=(
            "É a aplicação do Princípio de Pareto (regra 80/20) ao seu mix: poucos produtos costumam concentrar "
            "a maior parte das vendas. O portal mostra o gráfico de Pareto com os 10 primeiros e, na análise "
            "completa, a tabela com todos os produtos, com posição, classe, quantidade, faturamento, percentual "
            "individual e percentual acumulado, com filtro por classe."
        ),
        detalhes=(
            "Regras do cálculo: todos os produtos entram, sobre a receita líquida (vendas canceladas ficam fora e "
            "devoluções e trocas são deduzidas); o percentual acumulado é somado na ordem do ranking e fecha em "
            "100%; o líder é sempre Classe A, mesmo que sozinho ultrapasse o limiar. Produto com devoluções acima "
            "das vendas aparece no fim, com percentual negativo."
        ),
        exemplo=(
            "Exemplo hipotético: uma loja com 100 produtos descobre que 12 deles somam 80% do faturamento "
            "(Classe A), outros 25 somam os 15% seguintes (Classe B) e os 63 restantes somam apenas 5% (Classe C)."
        ),
        perguntas=(
            "O que é curva ABC?", "Como funciona a curva ABC?", "Explique a curva ABC.", "O que é a classificação ABC?",
            "O que é a Curva ABC de produtos?", "Como o sistema ordena os produtos?",
        ),
        termos="curva abc classe a b c classificacao produtos pareto estrategico tatico operacional ranking faturamento",
        relacionados=("nexo.indicadores.classes_abc", "nexo.indicadores.pareto", "nexo.indicadores.usar_abc"),
        proximo="nexo.indicadores.classes_abc",
        destino="dashboard",
        fontes=("etl_processor.py", "models.py", "templates/_macros.html", "templates/cliente/analise.html"),
    ),
    registro(
        id="nexo.indicadores.pareto",
        titulo="O que é o Princípio de Pareto e o gráfico de Pareto?",
        categoria="Curva ABC",
        resumo=(
            "O Princípio de Pareto (regra 80/20) diz que uma minoria das causas costuma gerar a maioria dos "
            "efeitos; no varejo, poucos produtos concentram a maior parte do faturamento. O gráfico de Pareto "
            "mostra as barras de faturamento por produto e uma linha com o percentual acumulado."
        ),
        explicacao=(
            "Leia o gráfico da esquerda para a direita: Leia o gráfico da esquerda para a direita: as barras "
            "caem (produtos em ordem de faturamento) e a linha de percentual acumulado sobe a cada produto. "
            "Como o portal exibe só os 10 primeiros, a linha termina no acumulado do 10º produto, que só chega "
            "a 100% quando a loja tem até 10 produtos com venda. O ponto em que a linha cruza 80% separa a "
            "Classe A; 95% separa a Classe B.. O ponto em que a linha cruza 80% separa a Classe A; 95% separa a "
            "Classe B. No portal o gráfico exibe os 10 primeiros produtos."
        ),
        perguntas=(
            "O que significa o gráfico de Pareto?", "O que é a regra 80/20?", "Como interpreto o gráfico de Pareto?",
            "O que é a linha amarela do gráfico?", "O que é Pareto?",
        ),
        termos="pareto principio 80 20 grafico linha acumulado barras regra",
        relacionados=("nexo.indicadores.curva_abc", "nexo.indicadores.faturamento_acumulado"),
        destino="dashboard",
        fontes=("templates/base.html", "blueprints/cliente.py", "etl_processor.py"),
    ),
    registro(
        id="nexo.indicadores.classes_abc",
        titulo="Qual é a diferença entre as classes A, B e C?",
        categoria="Curva ABC",
        resumo=(
            "Classe A: produtos estratégicos que, somados em ordem de faturamento, chegam a 80% do total. "
            "Classe B: produtos táticos, na faixa de 80% a 95% acumulados. Classe C: produtos operacionais, os "
            "últimos 5%, muitos itens com pouco faturamento cada."
        ),
        explicacao=(
            "A classe não mede qualidade do produto, e sim o peso dele no faturamento do período. Um item de "
            "Classe C pode ser importante para completar o mix; um item de Classe A exige atenção redobrada "
            "a abastecimento e preço, porque uma ruptura nele afeta diretamente o caixa."
        ),
        perguntas=(
            "Qual a diferença entre as classes A, B e C?", "O que é classe A, B e C?", "Como são definidas as classes?",
            "Classe C é ruim?", "O que diferencia A de B?",
        ),
        termos="classes a b c diferenca estrategico tatico operacional limiar 80 95",
        relacionados=("nexo.indicadores.classe_a", "nexo.indicadores.classe_b", "nexo.indicadores.classe_c"),
        destino="dashboard",
        fontes=("etl_processor.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.indicadores.classe_a",
        titulo="O que significa Classe A?",
        categoria="Curva ABC",
        resumo=(
            "Classe A são os produtos estratégicos: em ordem de faturamento, eles somam até 80% do total do "
            "período. São o núcleo do faturamento e aparecem em verde no portal."
        ),
        perguntas=("O que é classe A?", "Produto classe A significa o quê?", "Quais são os produtos estratégicos?"),
        termos="classe a estrategico nucleo 80 verde",
        relacionados=("nexo.indicadores.classes_abc", "nexo.indicadores.usar_abc"),
        fontes=("etl_processor.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.indicadores.classe_b",
        titulo="O que significa Classe B?",
        categoria="Curva ABC",
        resumo=(
            "Classe B são os produtos táticos: os que, acumulados após a Classe A, levam o faturamento de 80% "
            "até 95% do total. Têm faturamento intermediário e aparecem em âmbar no portal."
        ),
        perguntas=("O que é classe B?", "Produto classe B significa o quê?", "O que são produtos táticos?"),
        termos="classe b tatico intermediario 95 ambar",
        relacionados=("nexo.indicadores.classes_abc",),
        fontes=("etl_processor.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.indicadores.classe_c",
        titulo="O que significa Classe C?",
        categoria="Curva ABC",
        resumo=(
            "Classe C são os produtos operacionais: a cauda longa que responde pelos últimos cerca de 5% do "
            "faturamento. Costumam ser muitos itens com pouca venda cada; aparecem em cinza no portal."
        ),
        perguntas=("O que é classe C?", "Produto classe C significa o quê?", "O que é cauda longa?"),
        termos="classe c operacional cauda longa 5 cinza",
        relacionados=("nexo.indicadores.classes_abc",),
        fontes=("etl_processor.py", "templates/_macros.html"),
    ),
    registro(
        id="nexo.indicadores.faturamento_acumulado",
        titulo="Como interpreto o percentual acumulado da Curva ABC?",
        categoria="Curva ABC",
        resumo=(
            "O percentual individual é a fatia de cada produto no faturamento; o acumulado soma essas fatias na "
            "ordem do ranking. Quando o acumulado chega a 80%, acabou a Classe A; a 95%, acabou a Classe B."
        ),
        exemplo=(
            "Exemplo hipotético: o 1º produto tem 30% do faturamento (acumulado 30%), o 2º tem 25% (acumulado "
            "55%), o 3º tem 20% (acumulado 75%) e Exemplo hipotético: o 1º produto tem 30% do faturamento "
            "(acumulado 30%), o 2º tem 25% (acumulado 55%), o 3º tem 20% (acumulado 75%) e o 4º tem 8% "
            "(acumulado 83%). Os três primeiros formam a Classe A (acumulado até 80%); o 4º, que faz o "
            "acumulado passar de 80%, já é Classe B. A única exceção é o líder do ranking, que é sempre Classe "
            "A mesmo que sozinho ultrapasse 80%.."
        ),
        perguntas=(
            "O que é percentual acumulado?", "Como leio a coluna de % acumulado?", "O que é percentual individual?",
            "Como interpretar o faturamento acumulado?",
        ),
        termos="percentual acumulado individual porcentagem coluna tabela interpretar",
        relacionados=("nexo.indicadores.curva_abc", "nexo.indicadores.pareto"),
        destino="historico",
        fontes=("etl_processor.py", "templates/cliente/analise.html"),
    ),
    registro(
        id="nexo.indicadores.usar_abc",
        titulo="Como uso a Curva ABC na gestão da loja?",
        categoria="Curva ABC",
        resumo=(
            "Use a Classe A para blindar abastecimento, exposição e precificação (ruptura neles dói no caixa); "
            "acompanhe a Classe B como itens de reposição regular; e revise a Classe C para reduzir estoque "
            "parado e simplificar o mix, sem descartar itens que completam a venda dos demais."
        ),
        explicacao=(
            "Na análise completa, filtre a tabela por classe para listar os produtos de cada grupo. Compare "
            "a Classe A com o produto de maior saldo estimado parado: se um item comprado em excesso está "
            "fora da Classe A, a ação de escoamento é mais urgente. A devolutiva da equipe traz a leitura "
            "específica da sua loja."
        ),
        perguntas=(
            "Como uso a curva ABC?", "O que faço com a classificação ABC?", "Como identifico produtos relevantes?",
            "Como aplicar a curva ABC na prática?", "Para que serve saber a classe do produto?",
        ),
        termos="usar aplicar gestao pratica relevantes decisao abastecimento mix acao",
        relacionados=("nexo.indicadores.classes_abc", "nexo.indicadores.semaforos", "nexo.devolutivas.interpretar"),
        destino="historico",
        fontes=("insights.py", "templates/cliente/analise.html"),
    ),
    registro(
        id="nexo.indicadores.interpretar",
        titulo="Como interpreto os meus indicadores?",
        categoria="Indicadores",
        tipo="roteiro",
        resumo=(
            "Comece pelo faturamento e pelo total comprado; veja o sinal e o tamanho do Indicador de Pressão "
            "de Estoque em relação ao faturamento; confira os semáforos; depois olhe os produtos de destaque e "
            "a Curva ABC; por fim, leia a devolutiva, que contextualiza tudo."
        ),
        passos=(
            "Faturamento total e total comprado: quanto saiu e quanto entrou, em reais, no período.",
            "Indicador de Pressão de Estoque: positivo significa compras acima das vendas; compare com o "
            "faturamento (acima de 15% o portal marca como crítico).",
            "Semáforos: cada card diz o que fazer e por quê, com base nesses números.",
            "Produto mais vendido, maior faturamento e maior saldo estimado parado: onde está a força e onde "
            "há mercadoria que não girou.",
            "Curva ABC: quais produtos sustentam o faturamento (Classe A) e quais formam a cauda longa.",
            "Devolutiva estratégica: a leitura da equipe NEXO sobre o conjunto, com a prioridade do próximo período.",
        ),
        aviso=AVISO_PRESSAO,
        perguntas=(
            "Como interpreto meus indicadores?", "Como leio o painel?", "O que os números querem dizer?",
            "Como analisar os resultados?", "Me ajuda a entender meus resultados.", "Como interpretar o dashboard?",
        ),
        termos="interpretar ler entender resultados painel numeros roteiro analisar",
        relacionados=("nexo.indicadores.pressao_estoque", "nexo.indicadores.curva_abc", "nexo.devolutivas.interpretar"),
        proximo="nexo.indicadores.pressao_estoque",
        destino="dashboard",
        fontes=("templates/cliente/dashboard.html", "insights.py", "etl_processor.py"),
    ),
)
