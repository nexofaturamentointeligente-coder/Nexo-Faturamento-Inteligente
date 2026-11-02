"""Assunto H — processamento (ETL) em linguagem acessível.

Fontes: etl_processor.py, etl_inputs.py, etl_runtime.py, etl_limits.py,
etl_uploads.py, blueprints/admin.py (processar_analise), storage.py.
"""

from .modelo import registro

REGISTROS = (
    registro(
        id="nexo.processamento.o_que_e_etl",
        titulo="O que é o ETL do NEXO?",
        categoria="Processamento",
        resumo=(
            "ETL é a sigla de extrair, transformar e carregar: o motor do NEXO extrai os dados das planilhas "
            "de Vendas e Compras, transforma (limpa, padroniza e cruza) e carrega apenas os indicadores "
            "finais no banco. É o processamento que a equipe NEXO aciona depois de validar os arquivos."
        ),
        explicacao=(
            "Extração: leitura dos arquivos .csv ou .xlsx sem exigir um modelo fixo. Transformação: "
            "localização do cabeçalho, reconhecimento das colunas por nome, conversão de valores no formato "
            "brasileiro ou internacional, padronização dos nomes de produto, descarte de linhas que não são "
            "produto e cruzamento entre compras e vendas. Carga: gravação do faturamento, total comprado, "
            "Indicador de Pressão de Estoque, produtos de destaque, Curva ABC e período da base."
        ),
        perguntas=(
            "O que é ETL?", "O que significa ETL?", "O que é o processamento?", "O que é o motor de análise?",
            "O que é processamento de dados?", "Como os dados são processados?",
        ),
        termos="etl extrair transformar carregar processamento motor dados pipeline",
        relacionados=("nexo.processamento.extracao", "nexo.processamento.transformacao", "nexo.processamento.carga"),
        fontes=("etl_processor.py", "README.md"),
    ),
    registro(
        id="nexo.processamento.extracao",
        titulo="Como funciona a extração (leitura) das planilhas?",
        categoria="Processamento",
        resumo=(
            "O motor lê o arquivo inteiro como texto, sem assumir onde está o cabeçalho, testa os formatos de "
            "codificação e separadores mais comuns e, em planilhas com várias abas, usa a aba com mais dados. "
            "Depois procura, nas primeiras 100 linhas, a linha que melhor se parece com um cabeçalho."
        ),
        explicacao=(
            "As colunas são reconhecidas por nomes parecidos (por exemplo “Descrição do produto”, “Qtde vendida”, "
            "“Vr total”, “Fornecedor”, “Total NF”), sem depender da posição. Cabeçalhos quebrados em duas "
            "linhas também são tratados. Linhas e colunas totalmente vazias, linhas decorativas, totais, marcas "
            "d'água e cabeçalhos repetidos por quebra de página são descartados."
        ),
        perguntas=(
            "Como o sistema lê a planilha?", "Como funciona a extração?", "O sistema precisa de um modelo fixo?",
            "O cabeçalho pode estar em qualquer linha?", "Aceita várias abas?",
        ),
        termos="extracao leitura planilha cabecalho abas separador codificacao colunas reconhecimento",
        relacionados=("nexo.uploads.colunas_necessarias", "nexo.processamento.transformacao"),
        fontes=("etl_processor.py", "etl_inputs.py"),
    ),
    registro(
        id="nexo.processamento.transformacao",
        titulo="Como funciona a transformação: padronização de produtos e cruzamento de compras e vendas?",
        categoria="Processamento",
        resumo=(
            "Os nomes de produto são padronizados (maiúsculas, sem acentos e pontuação) para que o mesmo item "
            "escrito de formas diferentes seja somado junto; valores em formato brasileiro ou internacional "
            "são convertidos; e as quantidades compradas e vendidas de cada produto são cruzadas para estimar "
            "o saldo parado."
        ),
        explicacao=(
            "Linhas que não representam produto (descontos, frete, troco, taxas, totais) são removidas para não "
            "distorcer o ranking. Quando não há coluna de total, o valor da linha é derivado de quantidade "
            "vezes preço unitário, uma conta sobre dados reais, nunca um dado inventado. A coluna de lucro é "
            "reconhecida apenas para não confundir o mapeamento e descartada. A coluna de custo das vendas não "
            "entra em nenhum indicador: serve só para um alerta de qualidade (itens vendidos abaixo do custo "
            "informado pelo PDV) que a equipe vê no resultado do processamento. Nada de lucro, margem ou CMV é "
            "calculado."
        ),
        perguntas=(
            "Como os nomes dos produtos são tratados?", "Como funciona a normalização de produtos?",
            "Como o sistema cruza compras e vendas?", "O que é a transformação dos dados?",
            "O sistema soma produtos repetidos?", "Descontos entram no faturamento?",
        ),
        termos="transformacao normalizacao padronizacao produtos nomes cruzamento compras vendas descontos frete",
        relacionados=("nexo.indicadores.saldo_parado", "nexo.processamento.validacao"),
        fontes=("etl_processor.py",),
    ),
    registro(
        id="nexo.processamento.carga",
        titulo="O que é gravado no sistema depois do processamento?",
        categoria="Processamento",
        resumo=(
            "Apenas os resultados consolidados: faturamento total, total comprado, Indicador de Pressão de "
            "Estoque, produto mais vendido, produto de maior faturamento, produto com maior saldo estimado "
            "parado, o ranking completo da Curva ABC e o período lido do relatório."
        ),
        explicacao=(
            "As planilhas continuam guardadas para auditoria e reprocessamento, mas as linhas de venda e compra "
            "não são copiadas para as tabelas do sistema. Cada reprocessamento substitui os indicadores e o "
            "ranking anteriores da mesma análise e incrementa a versão do processamento."
        ),
        perguntas=(
            "O que é salvo após o processamento?", "O sistema guarda minhas vendas uma a uma?",
            "O que é a carga dos dados?", "Quais resultados ficam gravados?",
        ),
        termos="carga gravado salvo persistido resultados indicadores consolidados versao",
        relacionados=("nexo.indicadores.quais", "nexo.processamento.reprocessamento"),
        fontes=("etl_processor.py", "models.py"),
    ),
    registro(
        id="nexo.processamento.validacao",
        titulo="Como os dados são validados e o que acontece com linhas problemáticas?",
        categoria="Processamento",
        resumo=(
            "Antes de calcular, o motor exige as colunas essenciais; se não as reconhece, recusa o "
            "processamento com a mensagem “Não foi possível identificar os dados essenciais. Revise as colunas "
            "de vendas e compras.”, sem gravar nenhum indicador. (Na explicação: “ou é recusado com uma "
            "mensagem de erro padrão, sem detalhar quais colunas foram encontradas”.). Linhas sem produto, "
            "totais e cabeçalhos repetidos são ignorados; quantidades e valores ilegíveis viram zero e não "
            "entram nos indicadores."
        ),
        explicacao=(
            "O princípio é não inventar dados: ou o processamento tem sucesso com números reais, ou é recusado "
            "com um diagnóstico. Também há sinais de qualidade que a equipe vê no resultado do processamento, "
            "como produtos com estoque negativo informado pelo PDV ou itens vendidos abaixo do custo informado "
            "pelo PDV; esses alertas são observações sobre o dado bruto, não cálculos de margem."
        ),
        perguntas=(
            "Como os dados são validados?", "O que acontece com linhas erradas?", "O sistema corrige meus dados?",
            "O que acontece se a planilha tiver lixo?", "Linhas em branco atrapalham?",
        ),
        termos="validacao validar linhas problematicas erradas lixo recusa diagnostico qualidade",
        relacionados=("nexo.uploads.erros", "nexo.processamento.erros"),
        fontes=("etl_processor.py", "etl_inputs.py"),
    ),
    registro(
        id="nexo.processamento.quem_controla",
        titulo="Por que o processamento é controlado pela equipe NEXO?",
        categoria="Processamento",
        resumo=(
            "Porque o resultado alimenta uma devolutiva de consultoria: a equipe confere se os arquivos são os "
            "corretos antes de processar, avalia o resultado e só então escreve a devolutiva. O processamento "
            "não é automático e nunca publica nada sozinho."
        ),
        explicacao=(
            "A equipe aciona o processamento na área administrativa quando os dois arquivos estão presentes. "
            "Ele roda em um processo isolado, com limites de tempo (60 segundos), memória e tamanho, e trata "
            "uma análise por vez. Em caso de falha, a análise volta para Relatório recebido e os arquivos "
            "recebem o status “ERRO” com a mensagem de recusa do processamento."
        ),
        perguntas=(
            "Por que o processamento é manual?", "O processamento é automático?", "Quem processa meus arquivos?",
            "Quando a equipe processa?", "Por que a equipe precisa validar antes?",
        ),
        termos="processamento manual automatico quem processa equipe controla aciona limites",
        relacionados=("nexo.processamento.acompanhar_resultado", "nexo.tecnologia.automatico"),
        fontes=("blueprints/admin.py", "etl_runtime.py", "etl_limits.py", "etl_uploads.py"),
    ),
    registro(
        id="nexo.processamento.acompanhar_resultado",
        titulo="Como acompanho o resultado do processamento?",
        categoria="Processamento",
        tipo="procedimento",
        resumo=(
            "Quando o processamento termina com sucesso, a análise sai da página Enviar relatórios e passa a "
            "aparecer no seu painel, em “Análises em andamento”, com o status “Em análise”; você também recebe "
            "o aviso no sininho. (O status PROCESSADO dos arquivos é visível apenas para a equipe NEXO na área "
            "administrativa.), a análise passa para Em análise e você recebe no sininho o aviso “Recebemos e "
            "processamos seus relatórios de MM/AAAA. Sua devolutiva está em preparação.” (com o mês/ano da "
            "análise)."
        ),
        explicacao=(
            "Os números em si ainda não ficam visíveis nessa etapa: eles aparecem junto com a devolutiva, "
            "quando a equipe publica a análise. Se o processamento falhar, o arquivo aparece com o status "
            "“ERRO” em Enviar relatórios e pode ser reenviado."
        ),
        perguntas=(
            "Como sei que processou?", "Onde vejo o resultado do processamento?", "Já posso ver os números depois de processar?",
            "Recebi aviso de processamento, e agora?",
        ),
        termos="acompanhar resultado processamento processado aviso numeros ver",
        relacionados=("nexo.analises.status_em_analise", "nexo.analises.quando_vejo_resultados"),
        destino="upload",
        fontes=("blueprints/admin.py", "templates/cliente/upload.html"),
    ),
    registro(
        id="nexo.processamento.erros",
        titulo="O que acontece se o processamento falhar?",
        categoria="Processamento",
        resumo=(
            "Nada é gravado pela metade: a análise volta para Relatório recebido, os dois arquivos ficam com o "
            "status “ERRO” e a mensagem de recusa do processamento, e a equipe decide se pede um novo arquivo. "
            "Você pode reenviar o relatório corrigido no mesmo espaço."
        ),
        perguntas=(
            "O que acontece se o processamento der erro?", "Processamento falhou, o que faço?",
            "Meu arquivo ficou com erro depois do processamento.", "O sistema pode travar com arquivo grande?",
        ),
        termos="falha falhar erro processamento recusa reenviar status erro",
        relacionados=("nexo.uploads.erros", "nexo.uploads.substituir"),
        destino="upload",
        fontes=("blueprints/admin.py", "etl_processor.py"),
    ),
    registro(
        id="nexo.processamento.reprocessamento",
        titulo="Uma análise pode ser reprocessada?",
        categoria="Processamento",
        resumo=(
            "Sim, pela equipe NEXO. O reprocessamento recalcula os indicadores e a Curva ABC com os arquivos "
            "atuais da análise, substituindo os resultados anteriores. O cliente não aciona reprocessamentos: "
            "peça pelo Suporte."
        ),
        perguntas=(
            "Dá para reprocessar?", "Como peço um reprocessamento?", "Posso reprocessar minha análise?",
            "Se eu trocar o arquivo, recalcula?",
        ),
        termos="reprocessar reprocessamento recalcular refazer analise pedir",
        relacionados=("nexo.processamento.carga", "nexo.suporte.abrir_chamado"),
        destino="suporte",
        fontes=("etl_processor.py", "blueprints/admin.py"),
    ),
)
