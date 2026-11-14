"""Apresentação do Raio-X do PDV e da Auditoria de custos para quem não é do ramo (14/11/2026).

Monta só o que as duas telas mostram, a partir do resultado já filtrado pelo plano
(raio_x_servico.visao):
  - os avisos do processamento, reescritos em português simples e agrupados pelo impacto
    (organizar_avisos), e o resumo de uma linha dos arquivos lidos;
  - as explicações do botão (i) de cada indicador (EXPLICACOES, no Raio-X; DICAS_DA_AUDITORIA, uma
    ou duas frases, nos cards da Auditoria);
  - a ordem de maior impacto, o texto da busca e as marcas do filtro rápido de cada tabela.

Nenhum número novo é calculado aqui (a margem e o custo sem o custo a confirmar moram em
raio_x_servico.margem_sem_custo_a_confirmar e custo_sem_custo_a_confirmar), e nada do que o plano
esconde é usado: a busca e as marcas do filtro saem só de campos abertos, e as marcas de margem só
existem quando a margem está liberada.

Os avisos chegam como texto (raio_x.py, no processo isolado, gravados no banco). O catálogo
abaixo casa cada texto com re.fullmatch, a partir dos mesmos modelos de raio_x.py; o aviso que
nenhum modelo casa aparece como veio, no grupo "O que afeta seus números" (na dúvida, o aviso
importa). tests/test_raio_x_apresentacao.py gera os avisos pelo próprio motor e confere que
todos casam.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

import indicadores_executivos
import raio_x

MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro")
LIMITE_DAS_TABELAS = 5
# Faixas do filtro rápido da tabela "Lucro bruto por produto" da Auditoria, pedidas pelo dono na Etapa 1 (14/11/2026):
# só separam as linhas na tela, não entram em conta nenhuma.
MARGEM_ALTA = 0.40
MARGEM_BAIXA = 0.20


def _pct(fracao: float) -> str:
    """0,1 -> "10%" (sem casa decimal quando é inteiro)."""
    valor = round(fracao * 100, 1)
    return (f"{valor:.0f}" if valor == int(valor) else f"{valor:.1f}".replace(".", ",")) + "%"


# ---------------------------------------------------------------------------
# Avisos
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModeloDeAviso:
    """Um texto de aviso do motor e a versão simples dele. `regex` casa o texto inteiro (re.fullmatch);
    os grupos nomeados entram nos {marcadores} de titulo, texto, porque e o_que_fazer. O grupo "arquivo"
    (ou "arquivo1" e "arquivo2") é o número do arquivo enviado, que a tela troca pelo número e pelo nome."""
    id: str
    regex: str
    grupo: str  # "afeta" ou "informativo"
    papel: str  # "auxiliar", "fora_das_contas" ou "nenhum": o papel do arquivo no lote, para o resumo
    titulo: str
    texto: str
    porque: str
    o_que_fazer: str = ""


# Convenções dos textos simples: {arquivo} (e {arquivo1}, {arquivo2}, {arquivos}, {completou}, {completaram},
# {fora_um}, {fora_varios}) vira o número e o nome do arquivo enviado, "3 (vendas_out.xls)"; {n|singular|plural}
# escolhe a palavra pelo número capturado em n. Os textos de antes da forma atual que ainda podem estar gravados (o
# lote de cc62a44 e 9c4f531) também casam: as partes que mudaram são opcionais na regex. Textos conferidos contra o
# código por revisão independente em 14/11/2026: o que o motor supõe (sem conferir) aparece como suposição.
_LISTA = r"\d+(?:, \d+)* e \d+"
_FIM_DO_SALDO = r": as tabelas de estoque da mesma loja \(ou sem a loja no topo\) não somam\."
_RESUMO = r"O resumo por documento casou com (?P<percentual>\d{1,3})% (?:do valor )?das vendas do arquivo (?P<arquivo>\d+): "
_SEM_ANCORA = (r" uma tabela auxiliar que só completa a venda item a item com a data de cada venda \(a Opção 27 do "
               r"ERP\): entrou no envio, mas fica fora das contas\.")
_SEM_A_PRINCIPAL = ("Ela só é usada junto com o relatório de vendas que mostra a data de cada venda, e nenhum arquivo do "
                    "envio foi reconhecido como ele.")
_ENVIE_A_PRINCIPAL = ("Para usar este arquivo, envie junto o relatório de vendas item por item, com o período no topo e a "
                      "data de cada venda em todas as linhas, e refaça o Raio-X.")

CATALOGO_DE_AVISOS: tuple[ModeloDeAviso, ...] = (
    # ---- raio_x._avisos: cada relatório somado ----
    ModeloDeAviso(
        "periodos_sobrepostos",
        r"Os arquivos (?P<arquivo1>\d+) e (?P<arquivo2>\d+) cobrem períodos que se sobrepõem: os totais somam os dois\.",
        "afeta", "nenhum", "Dois arquivos com dias em comum",
        "Os arquivos {arquivo1} e {arquivo2} têm dias em comum no período, e o Raio-X somou os dois. Se as mesmas vendas "
        "estão nos dois arquivos, elas foram contadas duas vezes.",
        "O Raio-X não tem como saber se os arquivos são de lojas diferentes ou se repetem as mesmas vendas, então soma "
        "os dois e avisa.",
        "Se os dois trazem as mesmas vendas, refaça o Raio-X sem um deles, com um arquivo só para cada período."),
    ModeloDeAviso(
        "arquivo_sem_periodo",
        r"O arquivo (?P<arquivo>\d+) não traz o período no cabeçalho: entra nos totais, mas fica fora da linha do "
        r"tempo\.",
        "afeta", "nenhum", "Arquivo sem o período das vendas",
        "O arquivo {arquivo} não mostra no topo o período das vendas (a data de início e a de fim). As vendas dele "
        "entram nos totais, mas sem mês: ficam fora do período analisado e da comparação de custo e preço entre meses, "
        "e aparecem no Mês a mês numa linha sem mês.",
        "Sem a data de início e a de fim, o Raio-X não sabe de que mês são essas vendas.",
        "Exporte o relatório de novo com a linha do período no topo (por exemplo, Período: 01/01/2026 a 31/01/2026) e "
        "refaça o Raio-X."),
    ModeloDeAviso(
        "arquivo_sem_coluna_de_custo",
        r"O arquivo (?P<arquivo>\d+) não traz a coluna de custo: as vendas dele contam como sem custo informado\.",
        "afeta", "nenhum", "Arquivo sem o custo dos produtos",
        "O arquivo {arquivo} veio sem a coluna de custo. As vendas dele contam como vendas sem custo, baixam a saúde do "
        "cadastro e ficam fora da margem bruta e da perda abaixo do custo.",
        "Sem o custo, não dá para saber quanto sobra de cada venda depois de pagar o produto.",
        "Se o seu sistema permitir, exporte esse relatório de novo com a coluna de custo e refaça o Raio-X."),
    ModeloDeAviso(
        "acentos_corrigidos",
        r"O arquivo (?P<arquivo>\d+) misturava codificações de texto: os acentos de (?P<celulas>\d+) célula\(s\) "
        r"foram corrigidos, para o mesmo produto não virar dois entre os meses\.",
        "informativo", "nenhum", "Acentos corrigidos no arquivo",
        "O arquivo {arquivo} veio com parte dos acentos trocados por símbolos estranhos (como CafÃ© no lugar de Café). "
        "Corrigimos {celulas} {celulas|célula|células} da planilha, para o mesmo produto não aparecer com dois nomes.",
        "Sem a correção, as vendas do mesmo produto poderiam ficar divididas entre dois nomes."),
    # ---- O papel de cada arquivo do lote (raio_x._aviso_do_papel) ----
    ModeloDeAviso(
        "papel_venda_por_produto_auxiliar",
        r"O arquivo (?P<arquivo>\d+) entrou como tabela auxiliar \(custo, estoque e cadastro por produto\) da venda "
        r"item a item e não soma no faturamento\.",
        "informativo", "auxiliar", "Relatório usado só para completar dados",
        "O arquivo {arquivo} traz vendas que consideramos já estar no seu relatório principal de vendas (o que mostra a "
        "data de cada venda). Por isso ele não foi somado de novo: serviu só para completar o que faltava no principal "
        "(custo, estoque e cadastro dos produtos).",
        "Somar os dois contaria as mesmas vendas duas vezes."),
    ModeloDeAviso(
        "papel_estoque_auxiliar",
        r"O arquivo (?P<arquivo>\d+) entrou como (?P<rotulo>posição de estoque|inventário de estoque) \(tabela "
        r"auxiliar(?:: o saldo e o custo de cada produto)?\)\.",
        "informativo", "auxiliar", "Tabela de estoque usada como apoio",
        "O arquivo {arquivo} é uma tabela de estoque ({rotulo}). Ela não entra na soma das vendas: só traz o estoque e o "
        "custo de cada produto quando o seu relatório principal de vendas não tem esses dados.",
        "Estoque não é venda: a tabela só completa o que falta no relatório principal."),
    ModeloDeAviso(
        "papel_lista_de_precos_auxiliar",
        r"O arquivo (?P<arquivo>\d+) entrou como lista de preços \(o cadastro dos produtos(?:; o preço não entra nas "
        r"contas)?\)\.",
        "informativo", "auxiliar", "Lista de preços usada no cadastro",
        "O arquivo {arquivo} é uma lista de preços. Ela só completa o cadastro dos seus produtos e, se o relatório "
        "principal de vendas vier sem custo e nenhum outro arquivo trouxer o custo de um produto, dá o custo dele. O "
        "preço da lista não entra em nenhuma conta.",
        "O Raio-X usa o preço que você realmente cobrou em cada venda, e não o da lista."),
    ModeloDeAviso(
        "papel_resumo_por_pedido_auxiliar",
        r"O arquivo (?P<arquivo>\d+) entrou como resumo por pedido ou documento \(tabela auxiliar\)\.",
        "informativo", "auxiliar", "Resumo por pedido usado como apoio",
        "O arquivo {arquivo} é um resumo por pedido. Ele não entra na soma das vendas: só traz o desconto, o cliente e a "
        "hora das vendas quando o seu relatório principal não tem esses dados, e para isso o principal precisa trazer o "
        "número de cada venda.",
        "Cada pedido é ligado às vendas pelo número da venda; sem esse número, não há como ligar."),
    ModeloDeAviso(
        "papel_relatorio_de_compras_fora",
        r"O arquivo (?P<arquivo>\d+) é um relatório de compras: entrou no envio, mas fica fora das contas \(o resumo "
        r"por nota não traz o produto\)\.",
        "informativo", "fora_das_contas", "Relatório de compras, fora das contas",
        "O arquivo {arquivo} é um relatório de compras, e não de vendas. Ele foi recebido, mas ficou fora de todas as "
        "contas.",
        "O Raio-X analisa as suas vendas, e o resumo de compras por nota não traz o produto."),
    ModeloDeAviso(
        "papel_resumo_de_vendas_sem_produto_fora",
        r"O arquivo (?P<arquivo>\d+) é um resumo de vendas sem produto \(por cliente, fornecedor, grupo ou forma de "
        r"pagamento\): entrou no envio, mas fica fora das contas\.",
        "informativo", "fora_das_contas", "Resumo de vendas sem os produtos",
        "O arquivo {arquivo} é um resumo de vendas por cliente, fornecedor, grupo ou forma de pagamento, sem a lista de "
        "produtos. Como o Raio-X olha produto por produto, ele ficou fora das contas.",
        "Sem o produto de cada venda, não dá para calcular custo, preço e margem de cada item."),
    ModeloDeAviso(
        "papel_venda_de_colunas_duvidosas_fora",
        r"O arquivo (?P<arquivo>\d+) é um relatório de vendas de leiaute ambíguo \(dois títulos de coluna iguais para "
        r"um valor, ou o separador duvidoso\), do período da venda item a item: entrou no envio, mas fica fora das "
        r"contas\.",
        "informativo", "fora_das_contas", "Relatório de vendas com colunas confusas",
        "O arquivo {arquivo} é um relatório de vendas com colunas confusas (dois títulos iguais para um valor, ou um "
        ".csv em que não dá para saber onde começa cada coluna). Ele ficou fora das contas: consideramos que as vendas "
        "dele já estão no seu relatório principal de vendas.",
        "Com colunas confusas, o Raio-X poderia somar o valor errado.",
        "Se ele traz vendas que não estão no relatório principal, confira o arquivo e envie de novo."),
    ModeloDeAviso(
        "papel_estoque_ilegivel_fora",
        r"O arquivo (?P<arquivo>\d+) tem o título de posição de estoque, mas não foi possível ler o saldo de cada "
        r"produto \(a coluna da quantidade em estoque\): entrou no envio, mas fica fora das contas\.",
        "afeta", "fora_das_contas", "Tabela de estoque sem a quantidade",
        "O arquivo {arquivo} tem título de posição de estoque (ou de inventário), mas não achamos nele a quantidade em "
        "estoque de cada produto. Por isso ele ficou fora das contas, e nada dele foi usado.",
        "Sem a quantidade de cada produto, não dá para saber quanto você tem em estoque.",
        "Exporte a posição de estoque de novo com o produto e a quantidade em estoque e refaça o Raio-X."),
    ModeloDeAviso(
        "papel_lista_de_precos_ilegivel_fora",
        r"O arquivo (?P<arquivo>\d+) tem o título de lista de preços, mas não foi possível ler o preço de cada "
        r"produto: entrou no envio, mas fica fora das contas\.",
        "afeta", "fora_das_contas", "Lista de preços sem o preço",
        "O arquivo {arquivo} tem título de lista de preços, mas não achamos nele o preço de cada produto. Por isso ele "
        "ficou fora das contas, e nada dele foi usado.",
        "Sem o produto e o preço lado a lado, o Raio-X não consegue ler a lista.",
        "Se quiser usar a lista, exporte de novo com o produto e o preço e refaça o Raio-X."),
    ModeloDeAviso(
        "papel_nao_reconhecido_fora",
        r"O arquivo (?P<arquivo>\d+) não foi reconhecido como relatório de vendas por produto \(não foi possível "
        r"identificar produto, quantidade e valor de venda\) nem como tabela auxiliar, e o período dele está no da "
        r"venda item a item com a data de cada venda, que já traz a venda dele: entrou no envio, mas fica fora das "
        r"contas\.",
        "informativo", "fora_das_contas", "Arquivo não reconhecido, fora das contas",
        "O arquivo {arquivo} não foi reconhecido: não achamos nele produto, quantidade e valor de venda, nem uma tabela "
        "de estoque, de preços ou de pedidos. Como o período dele está dentro do seu relatório principal de vendas, "
        "consideramos que as vendas dele já estão lá, e ele ficou fora das contas.",
        "Somar um arquivo que não foi entendido poderia contar vendas erradas ou repetidas.",
        "Se ele traz vendas que não estão no relatório principal (de outra loja, por exemplo), confira o arquivo e "
        "envie de novo."),
    ModeloDeAviso(
        "tabela_de_estoque_sem_relatorio_principal",
        r"O arquivo (?P<arquivo>\d+) é (?:uma|um) (?P<rotulo>posição de estoque|inventário de estoque)," + _SEM_ANCORA,
        "afeta", "fora_das_contas", "Tabela de estoque não usada",
        "O arquivo {arquivo} é uma tabela de estoque ({rotulo}). " + _SEM_A_PRINCIPAL + " Por isso o estoque e o custo "
        "dela ficaram fora das contas.",
        "O Raio-X só usa a tabela de estoque para completar o relatório que mostra cada venda com a data.",
        _ENVIE_A_PRINCIPAL),
    ModeloDeAviso(
        "lista_de_precos_sem_relatorio_principal",
        r"O arquivo (?P<arquivo>\d+) é uma lista de preços," + _SEM_ANCORA,
        "afeta", "fora_das_contas", "Lista de preços não usada",
        "O arquivo {arquivo} é uma lista de preços. " + _SEM_A_PRINCIPAL + " Por isso ela ficou fora das contas.",
        "O Raio-X só usa a lista para completar o cadastro e o custo do relatório que mostra cada venda com a data.",
        _ENVIE_A_PRINCIPAL),
    ModeloDeAviso(
        "resumo_por_pedido_sem_relatorio_principal",
        r"O arquivo (?P<arquivo>\d+) é um resumo por pedido ou documento," + _SEM_ANCORA,
        "informativo", "fora_das_contas", "Resumo por pedido não usado",
        "O arquivo {arquivo} é um resumo por pedido. Ele só é usado junto com o relatório de vendas que mostra a data e o "
        "número de cada venda, e nenhum arquivo do envio foi reconhecido como ele. Por isso ele ficou fora das contas.",
        "O resumo por pedido só serve para trazer o desconto, o cliente e a hora das vendas desse relatório.",
        "Para usar o resumo, envie junto o relatório de vendas item por item, com o período no topo e a data e o número "
        "de cada venda, e refaça o Raio-X."),
    ModeloDeAviso(
        "loja_fora_da_venda_principal",
        r"O arquivo (?P<arquivo>\d+) traz a loja de cada venda, e a venda item a item com a data de cada venda não "
        r"traz: ele foi tomado como da mesma venda, e não soma no faturamento\. Se a venda item a item é de uma loja "
        r"só, a das outras lojas ficou de fora: envie a venda item a item de todas as lojas\.",
        "afeta", "nenhum", "Outras lojas podem ter ficado de fora",
        "O arquivo {arquivo} mostra a loja de cada venda, e o seu relatório principal de vendas (o que mostra a data de "
        "cada venda) não mostra. Consideramos que os dois trazem as mesmas vendas, e ele não foi somado. Se o relatório "
        "principal for de uma loja só, as vendas das outras lojas ficaram fora das contas.",
        "Sem a loja no relatório principal, não dá para saber se ele já inclui todas as lojas.",
        "Se você tem mais de uma loja, envie o relatório principal de vendas com todas elas e refaça o Raio-X."),
    # ---- O que as tabelas auxiliares completaram no relatório principal (raio_x._completar_ancora) ----
    ModeloDeAviso(
        "custo_completado_por_outros_arquivos",
        r"O custo das vendas do arquivo (?P<arquivo>\d+) veio das tabelas auxiliares, para (?P<casados>\d+) de "
        r"(?P<total>\d+) produto\(s\) vendido\(s\): é o custo médio do período do relatório por produto ou o da "
        r"posição de estoque, aplicado às vendas do período inteiro\. As margens são estimadas\.",
        "afeta", "nenhum", "Custo vindo de outros arquivos",
        "O arquivo {arquivo}, o seu relatório principal de vendas, veio sem o custo. Usamos o custo por unidade que os "
        "outros arquivos trazem, o mesmo em todas as vendas de cada produto no período. Achamos custo para {casados} de "
        "{total} {total|produto vendido|produtos vendidos}; o que ficou sem custo entra nas vendas sem custo "
        "informado.",
        "Esse custo vem de outra tabela e é o mesmo no período inteiro, então a margem bruta e a perda abaixo do custo "
        "são estimativas.",
        "Se o seu sistema permitir, exporte o relatório principal com o custo de cada venda e refaça o Raio-X."),
    ModeloDeAviso(
        "estoque_completado_por_outros_arquivos",
        r"O estoque do arquivo (?P<arquivo>\d+) é o das tabelas auxiliares \((?P<produtos>\d+) produto\(s\)\), na data "
        r"de (?P<data>\d{2}/\d{2}/\d{4})\.",
        "afeta", "nenhum", "Estoque vindo de outros arquivos",
        "O arquivo {arquivo}, o seu relatório principal de vendas, veio sem o estoque. Usamos o estoque de {produtos} "
        "{produtos|produto|produtos} que veio dos outros arquivos, como se fosse o do dia {data}; é ele que conta nos "
        "produtos com estoque abaixo de zero.",
        "Se a tabela de estoque for de outro dia, esse estoque pode não bater com o do fim do período das vendas.",
        "Se puder, envie a posição de estoque do último dia do período das vendas."),
    ModeloDeAviso(
        "saldo_de_estoque_uma_loja",
        r"O saldo de estoque veio do arquivo (?P<arquivo>\d+)(?=; )(?:; (?:o arquivo (?P<completou>\d+) só "
        r"completou|os arquivos (?P<completaram>" + _LISTA + r") só completaram) os produtos que ele não traz)?(?:; "
        r"(?:o arquivo (?P<fora_um>\d+) traz|os arquivos (?P<fora_varios>" + _LISTA + r") trazem) o saldo dos "
        r"mesmos produtos)?" + _FIM_DO_SALDO,
        "afeta", "nenhum", "Estoque de uma tabela só",
        "O estoque usado veio do arquivo {arquivo}. As outras tabelas de estoque parecem ser da mesma loja (ou não dizem "
        "a loja no topo), então não foram somadas a ele: delas, só entraram os produtos que faltavam nele.",
        "Somar duas tabelas de estoque da mesma loja contaria o estoque dos mesmos produtos duas vezes.",
        "Se essas tabelas são de lojas diferentes, exporte cada uma com o nome da loja no topo e refaça o Raio-X."),
    ModeloDeAviso(
        "saldo_de_estoque_varias_lojas",
        r"O saldo de estoque soma as tabelas de (?P<lojas>\d+) lojas diferentes \(os arquivos (?P<arquivos>" + _LISTA
        + r")\)\.",
        "informativo", "nenhum", "Estoque somado de várias lojas",
        "O estoque é a soma das tabelas de {lojas} lojas diferentes (arquivos {arquivos}): cada loja entrou com o seu "
        "estoque.",
        "Com lojas diferentes, o estoque da rede é a soma do estoque de cada loja."),
    ModeloDeAviso(
        "saldo_de_estoque_varias_lojas_com_complemento",
        r"O saldo de estoque soma as tabelas de (?P<lojas>\d+) lojas diferentes \(os arquivos (?P<arquivos>" + _LISTA
        + r")\)(?=; )(?:; (?:o arquivo (?P<completou>\d+) só completou|os arquivos (?P<completaram>" + _LISTA
        + r") só completaram) os produtos que elas não trazem)?(?:; (?:o arquivo (?P<fora_um>\d+) traz|os arquivos "
        r"(?P<fora_varios>" + _LISTA + r") trazem) o saldo dos mesmos produtos)?" + _FIM_DO_SALDO,
        "afeta", "nenhum", "Estoque das lojas, sem tabelas repetidas",
        "O estoque é a soma das tabelas de {lojas} lojas diferentes (arquivos {arquivos}). As outras tabelas de estoque "
        "são de uma dessas lojas ou não dizem a loja no topo, então não foram somadas: delas, só entraram os produtos "
        "que faltavam.",
        "Somar duas tabelas de estoque da mesma loja contaria o estoque dos mesmos produtos duas vezes.",
        "Se alguma tabela sem a loja no topo for de outra loja, exporte-a com o nome da loja no topo e refaça o "
        "Raio-X."),
    ModeloDeAviso(
        "amostra_parcial",
        r"Amostra parcial: a venda do arquivo (?P<arquivo>\d+) tem (?P<vendidos>\d{1,3}(?:\.\d{3})*) dos "
        r"(?P<com_saldo>\d{1,3}(?:\.\d{3})*) produtos com saldo no estoque enviado \((?P<percentual>\d{1,3})%\), em "
        r"(?P<dias>\d{1,3}(?:\.\d{3})*) dias\. Ela parece filtrada \(por cliente, vendedor ou loja\), e o estoque é o "
        r"da loja inteira: o giro, a cobertura, o GMROI, a ruptura e os encalhados comparam essa venda parcial com o "
        r"estoque inteiro e não representam a loja\.",
        "afeta", "nenhum", "Venda que parece filtrada",
        "O arquivo {arquivo} mostra venda de só {vendidos} dos {com_saldo} produtos que têm estoque ({percentual}%) em "
        "{dias} dias: parece uma venda filtrada (por cliente, vendedor ou loja). Se for, os totais desta tela valem só "
        "para essa parte das vendas, e as contas de estoque (giro, cobertura, retorno do estoque, falta de produto e "
        "produtos parados) não mostram a loja como ela é.",
        "O estoque enviado é o da loja inteira, e comparar com ele a venda de só uma parte dos clientes faz o estoque "
        "parecer mais parado do que é.",
        "Envie a venda de todos os clientes, vendedores e lojas do mesmo período, sem filtro, e refaça o Raio-X."),
    ModeloDeAviso(
        "produtos_so_no_cadastro",
        r"(?P<produtos>\d+) produto\(s\) das tabelas auxiliares sem venda nem saldo entraram no cadastro \(o mix\)\.",
        "informativo", "nenhum", "Produtos sem venda entraram no cadastro",
        "O seu cadastro ganhou {produtos} {produtos|produto|produtos} que só {produtos|aparece|aparecem} nos outros "
        "arquivos, sem venda e sem estoque nas contas: {produtos|ele conta|eles contam} como "
        "{produtos|produto que não vendeu|produtos que não venderam} no período.",
        "Assim o Raio-X sabe quantos dos seus produtos cadastrados venderam no período."),
    ModeloDeAviso(
        "nomes_com_mais_de_um_codigo",
        r"(?P<nomes>\d+) nome\(s\) de produto com mais de um código nas tabelas auxiliares: o custo e o estoque saíram "
        r"juntos pelo nome\.",
        "afeta", "nenhum", "Mesmo nome com códigos diferentes",
        "Nos outros arquivos há {nomes} {nomes|nome de produto|nomes de produto} com mais de um código. Como os arquivos "
        "foram ligados pelo nome do produto, o custo e o estoque que vieram deles juntam esses códigos num produto só.",
        "Se forem produtos diferentes com o mesmo nome, o custo e o estoque deles ficam misturados.",
        "Confira no cadastro do seu sistema os produtos que têm o mesmo nome e códigos diferentes."),
    ModeloDeAviso(
        "resumo_por_pedido_sem_novidade",
        _RESUMO + r"ele não trouxe nada que a venda já não tivesse\.",
        "informativo", "nenhum", "Resumo por pedido sem dado novo",
        "O resumo por pedido bateu com {percentual}% do valor das vendas do seu relatório principal, o arquivo "
        "{arquivo}, mas não trouxe nenhum dado que ele já não tivesse.",
        "O desconto, o cliente e a hora que o resumo poderia trazer já estavam no relatório principal ou não estão no "
        "resumo."),
    ModeloDeAviso(
        "resumo_por_pedido_completou_um_dado",
        _RESUMO + r"(?P<dados>o desconto|o cliente|a hora) veio dele\.",
        "informativo", "nenhum", "Dado das vendas vindo do resumo",
        "O resumo por pedido bateu com {percentual}% do valor das vendas do seu relatório principal, o arquivo "
        "{arquivo}, e {dados} de cada pedido veio dele. As vendas que não bateram, se houver, ficaram sem essa "
        "informação.",
        "O relatório principal não trazia essa informação; ela vem do pedido de mesmo número."),
    ModeloDeAviso(
        "resumo_por_pedido_completou_varios_dados",
        _RESUMO + r"(?P<dados>o desconto, o cliente e a hora|o desconto e o cliente|o desconto e a hora|o cliente e a "
        r"hora) vieram dele\.",
        "informativo", "nenhum", "Dados das vendas vindos do resumo",
        "O resumo por pedido bateu com {percentual}% do valor das vendas do seu relatório principal, o arquivo "
        "{arquivo}, e {dados} de cada pedido vieram dele. As vendas que não bateram, se houver, ficaram sem essas "
        "informações.",
        "O relatório principal não trazia essas informações; elas vêm do pedido de mesmo número."),
    ModeloDeAviso(
        "resumo_por_pedido_bateu_pouco",
        r"O resumo por documento casou com só (?P<percentual>\d{1,3})% (?:do valor )?das vendas do arquivo "
        r"(?P<arquivo>\d+) \(o mínimo é (?P<minimo>\d{1,3})%\): ficou fora\.",
        "informativo", "nenhum", "Resumo por pedido não usado",
        "O resumo por pedido bateu com menos de {minimo}% do valor das vendas do seu relatório principal, o arquivo "
        "{arquivo}. Por isso nada dele foi usado.",
        "Ligado a poucas vendas, ele deixaria o desconto, o cliente e a hora sem resposta em boa parte do faturamento.",
        "Confira se o resumo por pedido é do mesmo período e das mesmas lojas do relatório principal, e se o número do "
        "pedido é o mesmo nos dois, e refaça o Raio-X."),
    # ---- raio_x._combinar ----
    ModeloDeAviso(
        "custo_a_confirmar",
        r"(?P<produtos>\d+) produto\(s\) com custo a partir de (?P<razao>\d+(?:[.,]\d+)?) vezes a venda ficaram fora da "
        r"perda e do potencial recuperável: confira no cadastro a unidade ou a embalagem do custo\.",
        "afeta", "nenhum", "Custo muito acima do valor vendido",
        "{produtos} {produtos|produto tem|produtos têm}, nas vendas com custo, custo de {razao} vezes o valor vendido ou "
        "mais, o que costuma ser erro de cadastro (o custo da caixa lançado como se fosse o da unidade, por exemplo). "
        "Por isso o que {produtos|ele parece|eles parecem} ter perdido não entra na perda abaixo do custo, "
        "{produtos|ele fica|eles ficam} fora do custo e da sobra bruta da Auditoria de custos e, no dinheiro "
        "recuperável, só conta o que faltou para o preço normal.",
        "Um custo tão acima da venda costuma ser erro de cadastro, e não prejuízo de verdade.",
        "Confira no cadastro do seu sistema a unidade ou a embalagem do custo "
        "{produtos|desse produto|desses produtos}."),
)

# Os grupos das regex com números de arquivo (um só ou a lista "2, 3 e 4"): cada número vira "3 (nome.xls)".
_GRUPOS_DE_ARQUIVO = ("arquivo", "arquivo1", "arquivo2", "completou", "fora_um", "arquivos", "completaram",
                      "fora_varios")
_MARCADOR = re.compile(r"\{(\w+)\}")
_PLURAL = re.compile(r"\{(\w+)\|([^|{}]*)\|([^|{}]*)\}")


def _arquivo_por_extenso(numero: str, nomes) -> str:
    """"3 (vendas_out.xls)", ou só "3" sem o nome (o texto já diz "o arquivo")."""
    try:
        posicao = int(numero)
    except (TypeError, ValueError):
        return str(numero)
    nome = nomes[posicao - 1] if isinstance(nomes, (list, tuple)) and 1 <= posicao <= len(nomes) else None
    return f"{posicao} ({nome})" if isinstance(nome, str) and nome else str(posicao)


def _arquivos_por_extenso(texto: str, nomes) -> str:
    return re.sub(r"\d+", lambda achado: _arquivo_por_extenso(achado.group(0), nomes), texto)


def _preencher(modelo: str, valores: dict, numeros: dict) -> str:
    """Troca {n|singular|plural} pela palavra certa para o número capturado em n, e os {marcadores} que existem em
    `valores`; os outros ficam como estão (nunca levanta). A primeira letra sai maiúscula."""
    def plural(achado):
        chave, singular, varios = achado.groups()
        try:
            quantidade = int(str(numeros.get(chave, "")).replace(".", ""))
        except ValueError:
            return varios
        return singular if quantidade == 1 else varios
    texto = _PLURAL.sub(plural, modelo)
    texto = _MARCADOR.sub(lambda achado: str(valores.get(achado.group(1), achado.group(0))), texto)
    return texto[:1].upper() + texto[1:] if texto else texto


def _casar(texto: str):
    for modelo in CATALOGO_DE_AVISOS:
        achado = re.fullmatch(modelo.regex, texto)
        if achado:
            return modelo, achado
    return None


def organizar_avisos(avisos, nomes_arquivos=None) -> dict:
    """Os avisos do Raio-X por impacto: {"afetam": [...], "informativos": [...], "papeis": {número: papel}}.
    Cada aviso: titulo, texto, porque, o_que_fazer (os três em português simples) e tecnico (o original)."""
    afetam, informativos, papeis = [], [], {}
    for original in avisos or ():
        if not isinstance(original, str) or not original.strip():
            continue
        casado = _casar(original)
        if casado is None:
            afetam.append({"id": None, "titulo": "Aviso do processamento", "texto": original, "porque": "",
                           "o_que_fazer": "", "tecnico": original})
            continue
        modelo, achado = casado
        numeros = {chave: valor for chave, valor in achado.groupdict().items() if valor is not None}
        valores = dict(numeros)
        if modelo.papel in ("auxiliar", "fora_das_contas") and numeros.get("arquivo", "").isdigit():
            papeis.setdefault(int(numeros["arquivo"]), modelo.papel)
        for grupo in _GRUPOS_DE_ARQUIVO:
            if grupo in valores:
                valores[grupo] = _arquivos_por_extenso(valores[grupo], nomes_arquivos)
        item = {"id": modelo.id, "tecnico": original,
                **{campo: _preencher(getattr(modelo, campo), valores, numeros)
                   for campo in ("titulo", "texto", "porque", "o_que_fazer")}}
        (afetam if modelo.grupo == "afeta" else informativos).append(item)
    return {"afetam": afetam, "informativos": informativos, "papeis": papeis}


def _plural(quantidade: int, singular: str, plural: str) -> str:
    return f"{quantidade} {singular if quantidade == 1 else plural}"


def resumo_dos_arquivos(total_enviados: int, papeis: dict) -> str:
    """A linha do card dos avisos: "13 arquivos lidos • 1 principal de vendas, 4 auxiliares e 8 fora da soma".
    Sem tabela auxiliar no envio, os relatórios de vendas não têm "principal": "3 arquivos lidos • 3 de vendas"."""
    total = max(int(total_enviados or 0), 0)
    auxiliares = sum(1 for papel in papeis.values() if papel == "auxiliar")
    fora = sum(1 for papel in papeis.values() if papel == "fora_das_contas")
    total = max(total, auxiliares + fora)
    vendas = total - auxiliares - fora
    partes = []
    if vendas:
        partes.append(_plural(vendas, "principal de vendas", "principais de vendas") if auxiliares
                      else _plural(vendas, "de vendas", "de vendas"))
    if auxiliares:
        partes.append(_plural(auxiliares, "auxiliar", "auxiliares"))
    if fora:
        partes.append(f"{fora} fora da soma")
    if len(partes) > 1:
        juntas = ", ".join(partes[:-1]) + " e " + partes[-1]
    else:
        juntas = partes[0] if partes else ""
    cabeca = _plural(total, "arquivo lido", "arquivos lidos")
    return f"{cabeca} • {juntas}" if juntas else cabeca


# ---------------------------------------------------------------------------
# Tabelas: ordem de impacto, busca e filtro rápido
# ---------------------------------------------------------------------------
def _numero(valor) -> float:
    return float(valor) if isinstance(valor, (int, float)) and not isinstance(valor, bool) else 0.0


def texto_do_periodo(fatia: dict) -> str:
    """O que a busca do Mês a mês procura: o rótulo e o mês por extenso ("01/2026 janeiro de 2026")."""
    rotulo = str(fatia.get("rotulo") or "")
    achado = re.fullmatch(r"(\d{2})/(\d{4})", rotulo)
    if achado and 1 <= int(achado.group(1)) <= 12:
        return f"{rotulo} {MESES[int(achado.group(1)) - 1]} de {achado.group(2)}"
    return rotulo


def preparar_fatias(fatias) -> list:
    """As linhas do Mês a mês, cada uma com a posição no calendário (a ordem que chega, a de raio_x.py) e a de
    maior impacto (o dinheiro a recuperar do mês; no empate, a perda abaixo do custo e depois o mais recente),
    as marcas do filtro rápido e o texto da busca. Com mais linhas que o limite, a tabela já sai na ordem de
    impacto (o dono pediu as 5 de maior impacto primeiro); com até o limite, na ordem do calendário.

    Marcas: "perda" (o mês tem dinheiro a recuperar: venda abaixo do custo ou abaixo do preço normal) e
    "alerta" (sem custo em ao menos raio_x.LIMIAR_SEM_CUSTO do faturamento, o limite que já pinta o card de
    vermelho). "atencao" junta as duas. Todos esses campos são abertos a qualquer plano."""
    linhas = []
    for posicao, fatia in enumerate(fatias or ()):
        perda = _numero(fatia.get("perda_abaixo_custo")) > 0 or _numero(fatia.get("vazamento_preco")) > 0 \
            or _numero(fatia.get("recuperavel")) > 0
        sem_custo = fatia.get("pct_faturamento_sem_custo")
        alerta = isinstance(sem_custo, (int, float)) and not isinstance(sem_custo, bool) \
            and sem_custo >= raio_x.LIMIAR_SEM_CUSTO
        marcas = [marca for marca, vale in (("perda", perda), ("alerta", alerta), ("atencao", perda or alerta))
                  if vale]
        linhas.append({"fatia": fatia, "pos_calendario": posicao, "marcas": " ".join(marcas),
                       "texto": texto_do_periodo(fatia)})
    por_impacto = sorted(linhas, key=lambda linha: (
        -_numero(linha["fatia"].get("recuperavel")), -_numero(linha["fatia"].get("perda_abaixo_custo")),
        -_numero(linha["fatia"].get("vazamento_preco")), -linha["pos_calendario"]))
    for posicao, linha in enumerate(por_impacto):
        linha["pos_impacto"] = posicao
    # A ordem dos mais recentes: o calendário ao contrário, com o relatório sem período ("Arquivo N", que
    # raio_x.py põe no fim) continuando no fim. Assim as 5 linhas visíveis são os 5 meses mais novos.
    datadas = [linha for linha in linhas if linha["fatia"].get("inicio")]
    sem_data = [linha for linha in linhas if not linha["fatia"].get("inicio")]
    for posicao, linha in enumerate(list(reversed(datadas)) + sem_data):
        linha["pos_recentes"] = posicao
    return por_impacto if len(linhas) > LIMITE_DAS_TABELAS else linhas


def _texto_do_produto(item: dict) -> str:
    return " ".join(str(parte) for parte in (item.get("produto"), item.get("codigo")) if parte)


def _negativa(item: dict) -> bool:
    valor = item.get("margem_valor")
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and valor < 0


def _a_confirmar_pela_linha(item: dict) -> bool:
    """A regra do motor (raio_x._combinar: custo a partir de RAZAO_CUSTO_IMPLAUSIVEL vezes a venda com custo, em
    centavos inteiros) com os campos da própria linha. Sem a venda com custo e o custo (a linha do scanner), a mesma
    regra pela margem: custo >= R x venda é margem <= 1 - R. Só usa campos que o plano já mostra na linha."""
    custo, venda = item.get("custo_total"), item.get("faturamento_com_custo")
    if _fracao_valida(custo) and _fracao_valida(venda):
        venda_c = round(venda * 100)
        return venda_c > 0 and round(custo * 100) >= raio_x.RAZAO_CUSTO_IMPLAUSIVEL * venda_c
    percentual = item.get("margem_percentual")
    return _fracao_valida(percentual) and percentual <= 1 - raio_x.RAZAO_CUSTO_IMPLAUSIVEL


def chaves_a_confirmar(auditoria: dict) -> frozenset:
    """(produto, código) dos itens de custo a confirmar que a lista do plano traz (None quando o plano não a
    libera): marcam as linhas das tabelas de margem, para o número distorcido não passar por prejuízo real."""
    bloco = auditoria.get("custo_a_confirmar") if isinstance(auditoria, dict) else None
    lista = bloco.get("lista") if isinstance(bloco, dict) else None
    return frozenset((item.get("produto"), item.get("codigo")) for item in lista or () if isinstance(item, dict))


def _fracao_valida(valor) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _pct_exibido(fracao: float) -> float:
    """A margem como a tela a mostra (macro pct de templates/_planos.html: uma casa, em %). As faixas do filtro e a
    cor do card comparam este número, e não a fração com quatro casas, para baterem com o que a pessoa lê: 0,4004
    aparece como 40,0% e não entra em "acima de 40%"; 0,1996 aparece como 20,0% e não entra em "abaixo de 20%"."""
    return float("%.1f" % (fracao * 100))


def _faixa(fracao: float) -> float:
    """MARGEM_ALTA e MARGEM_BAIXA em %, com uma casa, como o número exibido: o round evita o resto do ponto
    flutuante se a régua mudar (0,57 * 100 dá 56,99999999999999)."""
    return round(fracao * 100, 1)


def linhas_de_produto(itens, *, margem_visivel: bool, a_confirmar=frozenset(), pareto=None) -> list:
    """As linhas de uma tabela de produtos, na ordem recebida (a de maior impacto, de raio_x.py), com o texto da
    busca (nome e código) e as marcas do filtro rápido. Cada marca só sai de campo que a tabela tem:
      - "abaixo": vendido abaixo do preço normal (vazamento > 0);
      - "negativa" e "baixa": margem negativa, e margem abaixo de indicadores_executivos.MARGEM_MINIMA (15%, a
        margem mínima do motor; a negativa também é baixa), só quando o plano mostra a margem e fora os itens de
        custo a confirmar;
      - "a_confirmar": custo a confirmar (pela lista do plano);
      - "parado" e "subiu_pouco": na precificação, o preço médio parado ou em queda, ou subindo menos que o custo;
      - as de `pareto` (marcas_pareto)."""
    linhas = []
    pareto = list(pareto or ())
    for posicao, item in enumerate(itens or ()):
        marcas = []
        if _numero(item.get("vazamento")) > 0:
            marcas.append("abaixo")
        confirmar = (item.get("produto"), item.get("codigo")) in a_confirmar \
            or (margem_visivel and _a_confirmar_pela_linha(item))
        if margem_visivel and not confirmar:
            if _negativa(item):
                marcas.append("negativa")
            percentual = item.get("margem_percentual")
            if _fracao_valida(percentual) and percentual < indicadores_executivos.MARGEM_MINIMA:
                marcas.append("baixa")
        if confirmar:
            marcas.append("a_confirmar")
        preco = item.get("variacao_preco")
        if _fracao_valida(preco):
            marcas.append("parado" if preco <= 0 else "subiu_pouco")
        if posicao < len(pareto) and pareto[posicao]:
            marcas.append(pareto[posicao])
        linhas.append({"item": item, "texto": _texto_do_produto(item), "marcas": " ".join(marcas),
                       "a_confirmar": confirmar})
    return linhas


def linhas_do_lucro(itens, *, a_confirmar=frozenset(), separar: bool = True) -> list:
    """As linhas da tabela "Lucro bruto por produto" da Auditoria (Etapa 1, 14/11/2026), a partir da lista do motor
    (auditoria.margem_por_item, os produtos com custo que mais venderam):
      - sem os itens de custo a confirmar (pela lista do plano ou pela regra do motor na própria linha), que ficam fora
        da sobra bruta e têm a tabela própria, nos alertas de cadastro. Com `separar` falso (Raio-X gravado antes do
        bloco custo_a_confirmar, em que os cards também os contam), nenhum sai;
      - na ordem do maior lucro bruto em reais (o maior impacto); no empate, a do motor. "pos_piores" é a posição na
        ordem inversa (o maior prejuízo primeiro), a dos filtros de margem baixa e de margem negativa;
      - marcas do filtro rápido pela margem do produto, como a tela a mostra (_pct_exibido): "margem_alta" acima de
        MARGEM_ALTA, "margem_baixa" abaixo de MARGEM_BAIXA e "negativa" com o lucro em reais abaixo de zero.
    Só é chamada com a margem liberada: sem ela, raio_x_servico.visao troca a lista por None e a tabela nem existe."""
    linhas = []
    for posicao, item in enumerate(itens or ()):
        if not isinstance(item, dict):
            continue
        if separar and ((item.get("produto"), item.get("codigo")) in a_confirmar or _a_confirmar_pela_linha(item)):
            continue
        marcas = []
        percentual = item.get("margem_percentual")
        if _fracao_valida(percentual) and _pct_exibido(percentual) > _faixa(MARGEM_ALTA):
            marcas.append("margem_alta")
        if _fracao_valida(percentual) and _pct_exibido(percentual) < _faixa(MARGEM_BAIXA):
            marcas.append("margem_baixa")
        if _negativa(item):
            marcas.append("negativa")
        valor = item.get("margem_valor")
        valido = _fracao_valida(valor)
        linhas.append({"item": item, "texto": _texto_do_produto(item), "marcas": " ".join(marcas),
                       "a_confirmar": False, "_maior": (0, -valor, posicao) if valido else (1, 0, posicao),
                       "_piores": (0, valor, posicao) if valido else (1, 0, posicao)})
    for ordem, linha in enumerate(sorted(linhas, key=lambda linha: linha["_piores"])):
        linha["pos_piores"] = ordem
    linhas.sort(key=lambda linha: linha["_maior"])
    for linha in linhas:
        del linha["_maior"], linha["_piores"]
    return linhas


def marcas_pareto(itens, campo: str, total=None, fracao: float = 0.8) -> list:
    """Para cada item (na ordem recebida, a de maior impacto), "principais" se ele está entre os que somam
    `fracao` do total do campo (a regra 80/20); o total é `total` (o do envio inteiro, quando a lista é cortada)
    ou a soma da lista. Lista vazia ou total zero: nenhuma marca."""
    valores = [_numero(item.get(campo)) for item in itens or ()]
    soma = _numero(total) if total is not None else sum(valores)
    if soma <= 0:
        return ["" for _ in valores]
    marcas, acumulado = [], 0.0
    for valor in valores:
        marcas.append("principais" if acumulado < fracao * soma else "")
        acumulado += max(valor, 0.0)
    return marcas


def _cor_da_margem(percentual) -> str:
    """A cor do ícone do card da sobra bruta: vermelha abaixo de zero, âmbar abaixo de MARGEM_BAIXA (20%, a "margem
    baixa" do filtro da tabela de lucro bruto, na mesma tela), verde a partir dela. Sem número (o plano esconde a
    margem, ou não há custo), a cor neutra: a cor nunca diz o que o número esconde."""
    if not _fracao_valida(percentual):
        return "info"
    if percentual < 0:
        return "red"
    return "amber" if _pct_exibido(percentual) < _faixa(MARGEM_BAIXA) else "green"


def _itens_fora(auditoria: dict) -> int:
    """Quantos itens de custo a confirmar o Raio-X separou (0 sem o bloco, gravado até a raio-x-2026-11.4)."""
    bloco = auditoria.get("custo_a_confirmar")
    itens = bloco.get("itens") if isinstance(bloco, dict) else None
    return itens if isinstance(itens, int) and not isinstance(itens, bool) and itens > 0 else 0


def custo_do_card(auditoria: dict) -> dict:
    """O card "Custo dos produtos vendidos" da Auditoria (Etapa 1, 14/11/2026): o custo e a venda com custo sem os
    itens de custo a confirmar, as mesmas vendas da "Sobra bruta nas vendas" (campos "sem_a_confirmar" de
    raio_x_servico.visao). Devolve:
      - "valor": o custo sem esses itens; None quando o plano não libera o custo (diagnóstico de CMV) ou não há número;
      - "base": a venda com custo sem esses itens (as "vendas auditadas"); sem item a confirmar, a venda com custo de
        sempre, que todo plano vê; None quando o plano não a libera (com item a confirmar, ela daria a venda deles);
      - "itens_fora": quantos itens saíram;
      - "so_isolados": o custo aparece e todo ele era desses itens (sobra zero), o que pede uma linha no rodapé."""
    itens = _itens_fora(auditoria)
    valor = auditoria.get("custo_total_sem_a_confirmar")
    base = auditoria.get("faturamento_com_custo_sem_a_confirmar")
    if not itens:
        valor = valor if valor is not None else auditoria.get("custo_total")
        base = base if base is not None else auditoria.get("faturamento_com_custo")
    return {"valor": valor, "base": base, "itens_fora": itens, "so_isolados": bool(itens) and valor == 0}


def margem_do_card(auditoria: dict) -> dict:
    """O card "Sobra bruta nas vendas" da Auditoria: a margem sem os itens de custo a confirmar (os campos
    "sem_a_confirmar" de raio_x_servico.visao). Devolve "percentual", "valor", "itens_fora" (quantos itens saíram),
    "cor" e a situação que pede uma linha própria no rodapé:
      - "sem_custo_confiavel": todo o custo informado é de itens a confirmar (não há margem a mostrar);
      - "antigo": Raio-X gravado antes da raio-x-2026-11.5, sem o bloco de custo a confirmar (a margem é a de
        sempre, e os itens de custo absurdo não foram separados)."""
    antigo = not isinstance(auditoria.get("custo_a_confirmar"), dict)
    itens = _itens_fora(auditoria)
    percentual, valor = auditoria.get("margem_sem_a_confirmar_percentual"), auditoria.get("margem_sem_a_confirmar_valor")
    visivel = auditoria.get("margem_valor") is not None
    card = {"percentual": percentual, "valor": valor, "itens_fora": itens, "sem_custo_confiavel": False,
            "antigo": antigo}
    if percentual is None and itens and visivel:
        card["sem_custo_confiavel"] = True
    elif percentual is None:
        card.update(percentual=auditoria.get("margem_percentual"), valor=auditoria.get("margem_valor"), itens_fora=0)
    card["cor"] = _cor_da_margem(card["percentual"])
    return card


# ---------------------------------------------------------------------------
# Explicações do botão (i)
# ---------------------------------------------------------------------------
def _e(significa: str, calculo: str, se_ruim: str) -> dict:
    return {"significa": significa, "calculo": calculo, "se_ruim": se_ruim}


def _reais(valor: float) -> str:
    return f"R$ {valor:.2f}".replace(".", ",")


# Os números das regras saem das constantes do motor (raio_x.py), nunca escritos à mão: se a regra mudar, o texto
# acompanha. Textos conferidos contra o código por revisão independente em 14/11/2026 (raio_x._combinar e
# _resumir, raio_x_servico.visao); tests/test_raio_x_apresentacao.py confere as três partes de cada um.
_VEZES = raio_x.RAZAO_CUSTO_IMPLAUSIVEL
_FOLGA = f"{_reais(raio_x.TOLERANCIA_PRECO_MINIMA)} ou {_pct(raio_x.TOLERANCIA_PRECO_RELATIVA)}"

EXPLICACOES: dict = {
    "recuperavel": _e(
        "Estimativa de quanto a mais teria entrado no caixa se nenhuma venda saísse abaixo do custo nem abaixo do "
        "preço normal do produto. Não é lucro nem dinheiro garantido.",
        "Em cada linha de venda, pega a maior entre a perda abaixo do custo e o que faltou para o preço normal, e "
        "soma tudo. Nos produtos de custo a confirmar, só conta o que faltou para o preço normal.",
        "Veja qual parte pesa mais. Abaixo do custo: confira o custo e reajuste o preço. Abaixo do preço normal: "
        "controle os descontos no caixa e confira o preço cadastrado."),
    "saude_indice": _e(
        "De cada R$ 100 vendidos, quantos vieram com custo e sem confusão de código ou nome. Quanto maior, melhor, "
        "mas a saúde do cadastro não confere se o custo está certo.",
        "Soma as vendas com custo, menos as dos produtos que tiveram, em algum relatório ou mês, um código com dois "
        "nomes ou um nome com dois códigos (esses saem do período todo). Divide pelo faturamento analisado e "
        "multiplica por 100.",
        "Exporte o relatório com a coluna de custo, cadastre o custo dos mais vendidos e acerte códigos com dois "
        "nomes e produtos com dois códigos. Depois, refaça o Raio-X."),
    "faturamento_analisado": _e(
        "Quanto você vendeu nos relatórios enviados, sem as vendas canceladas. As devoluções e trocas não são "
        "descontadas. É a base da saúde do cadastro e da parte vendida sem custo.",
        "Soma cada linha com quantidade e valor acima de zero. Ficam fora: canceladas (quando o relatório marca), "
        "itens sem valor e linhas de devolução ou troca, que não descontam a venda. Relatórios de períodos "
        "cruzados somam os dois, com aviso.",
        "Se não bate com o seu sistema, lembre: aqui a devolução não é descontada. Confira se enviou todos os "
        "meses, sem repetir período, e se o relatório marca as canceladas."),
    "sem_custo": _e(
        "Parte do valor vendido que veio sem o custo do produto nos relatórios. Nessas vendas, o NEXO não calcula "
        "margem nem acha venda abaixo do custo.",
        "Soma o valor das vendas com custo vazio, zero ou negativo (e as de relatório sem coluna de custo) e divide "
        f"pelo faturamento analisado. A partir de {_pct(raio_x.LIMIAR_SEM_CUSTO)}, o card fica vermelho.",
        "Exporte o relatório com a coluna de custo e preencha no cadastro o custo dos produtos que estão sem, "
        "começando pelos que mais vendem."),
    "itens_vendidos": _e(
        "Quantos produtos diferentes você vendeu no período. O rodapé mostra quantos deles não tiveram custo em "
        "nenhuma venda.",
        "Conta os nomes de produto diferentes com ao menos uma venda no faturamento analisado. Maiúscula, acento e "
        "pontuação não contam: 'Café 500g' e 'CAFE 500G' são um só. O código não entra: um código com dois nomes "
        "vira dois.",
        "Se o número parece alto, o mesmo produto pode ter nomes diferentes: padronize no cadastro. E cadastre o "
        "custo dos que nunca vieram com custo."),
    "itens_variacao": _e(
        "Quantos produtos saíram, no mesmo mês, por preços diferentes entre si ou longe do preço normal. Pode ser "
        "desconto no caixa, promoção, reajuste ou erro de cadastro.",
        "Conta uma vez cada produto que, em algum mês (ou relatório), vendeu mais de 1 unidade e teve preço por "
        f"unidade variando, ou longe do preço normal, mais que {_FOLGA} do preço normal (o maior).",
        "Comece pelos primeiros da lista, os com mais dinheiro abaixo do preço normal. Veja se o desconto foi "
        "combinado; se não foi, revise o preço no cadastro e quem pode dar desconto."),
    "faturamento_variacao": _e(
        "Quanto os produtos com preço variando venderam no período. Mostra o tamanho do que está em jogo, não a "
        "perda.",
        "Soma todo o faturamento desses produtos no período inteiro, inclusive nos meses em que o preço deles não "
        "variou.",
        "Se for uma parte grande do faturamento, revise primeiro o preço desses produtos no cadastro e combine com a "
        "equipe a regra de desconto."),
    "abaixo_referencia": _e(
        "Quanto a mais você teria recebido se as vendas feitas abaixo do preço normal tivessem saído por ele. Entra "
        "no dinheiro recuperável.",
        "Soma (preço normal − preço cobrado) × quantidade das vendas abaixo do preço normal, fora diferenças "
        "mínimas, no mês em que o produto vendeu mais de 1 unidade. Preço normal: o de tabela ou, sem ele, o que "
        "mais saiu.",
        "Comece pelos produtos com mais dinheiro abaixo do preço normal na lista. Confira descontos no caixa, preço "
        "errado no cadastro e se houve reajuste no meio do mês."),
    # As dicas dos pontos do cadastro (uma frase em cada).
    "inconsistencias": {
        "codigo_com_varios_nomes": (
            "O mesmo código com nomes diferentes no mesmo mês: as vendas desses produtos saem da saúde do cadastro."),
        "nome_com_varios_codigos": (
            "O mesmo nome com códigos diferentes no mesmo mês: pode dividir estoque e custo entre dois cadastros."),
        "estoque_negativo": (
            "Saiu mais do que entrou no sistema (compra não lançada ou venda no código errado)."),
        "venda_sem_valor": (
            "Linhas vendidas com valor zero ou em branco: bonificação ou preço faltando no cadastro."),
        "itens_margem_negativa": (
            "Somando o período, o custo passou do valor das vendas com custo (sem os de custo a confirmar)."),
        "custo_a_confirmar": (
            f"Custo de {_VEZES} vezes a venda com custo, ou mais: quase sempre erro de cadastro. Ficam fora da perda, "
            "do custo e da sobra bruta da Auditoria de custos."),
    },
}


# O (i) dos quatro cards da Auditoria de custos (Etapa 1, 14/11/2026): uma ou duas frases simples, o que o número é
# e o que fazer se estiver ruim. Abre ao passar o ponteiro e ao clicar (templates/_raio_x.html, macro dica).
DICAS_DA_AUDITORIA: dict = {
    "custo": (
        "Quanto custaram os produtos que você vendeu, pelo custo que o seu sistema informa. Se ele estiver perto do "
        "valor vendido, sobra pouco: confira o custo cadastrado dos que mais vendem e negocie com os fornecedores."),
    "sobra": (
        "O que sobrou das vendas depois de pagar o custo dos produtos, antes de despesas e impostos (não é o lucro "
        "final). Se estiver baixa ou negativa, reveja o preço dos produtos de margem mais baixa na tabela de lucro "
        "bruto."),
    "prejuizo": (
        "Dinheiro perdido nas vendas feitas por menos do que o produto custou. Se aparecer valor, corrija o preço "
        "desses produtos na lista abaixo ou confira se o custo cadastrado está certo."),
    "produtos_prejuizo": (
        "Produtos em que, somando o período, o custo passou do valor vendido. Se houver algum, comece pela lista "
        "de produtos vendidos com prejuízo e reveja o preço ou o custo cadastrado deles."),
}


def nota_dos_isolados(itens) -> str:
    """A nota discreta do (i) da sobra bruta (o item 3.5 do pedido a põe só nele): quantos itens com erro grave de
    cadastro (custo a confirmar) ficaram fora da conta. Vazia sem item."""
    if not isinstance(itens, int) or isinstance(itens, bool) or itens <= 0:
        return ""
    regra = f"custo de {_VEZES} vezes o preço de venda ou mais"
    if itens == 1:
        return f"1 item com erro grave de cadastro ({regra}) foi isolado e não entra nesta conta."
    numero = f"{itens:,}".replace(",", ".")
    return f"{numero} itens com erro grave de cadastro ({regra}) foram isolados e não entram nesta conta."


# ---------------------------------------------------------------------------
# Tudo o que as duas telas usam
# ---------------------------------------------------------------------------
def preparar(resultado: dict, total_enviados=None) -> dict:
    """O que o Raio-X e a Auditoria mostram além do resultado: os avisos organizados e o resumo dos arquivos,
    as explicações (i) e as linhas das tabelas (ordem, busca e marcas do filtro). `resultado` é o da
    raio_x_servico.visao; `total_enviados`, quantos arquivos o cliente mandou (DiagnosticoEntrada.arquivos)."""
    nomes = resultado.get("nomes_arquivos")
    nomes = nomes if isinstance(nomes, list) else []
    avisos = organizar_avisos(resultado.get("avisos"), nomes)
    avisos["resumo"] = resumo_dos_arquivos(total_enviados or len(nomes) or len(resultado.get("arquivos") or ()),
                                           avisos["papeis"])
    auditoria = resultado.get("auditoria") if isinstance(resultado.get("auditoria"), dict) else {}
    protegido = resultado.get("protegido") if isinstance(resultado.get("protegido"), dict) else {}
    margem_visivel = protegido.get("margem") is False
    a_confirmar = chaves_a_confirmar(auditoria)
    scanner = resultado.get("scanner") if isinstance(resultado.get("scanner"), dict) else {}
    abaixo = auditoria.get("itens_abaixo_custo")
    precificacao = auditoria.get("precificacao")
    # A lista dos itens de custo a confirmar traz o custo de cada um (aberta com a auditoria); o total do bloco é do
    # diagnóstico de CMV: sem ele, os 80% saem da soma da lista.
    bloco_a_confirmar = auditoria.get("custo_a_confirmar")
    bloco_a_confirmar = bloco_a_confirmar if isinstance(bloco_a_confirmar, dict) else {}
    lista_a_confirmar = bloco_a_confirmar.get("lista")
    return {
        "explicacoes": EXPLICACOES,
        "avisos": avisos,
        "fatias": preparar_fatias(resultado.get("fatias")),
        # "meses" ou "períodos" nos rótulos do Mês a mês: só é mês quando toda linha é um mês ("MM/AAAA").
        "fatias_mensais": all(re.fullmatch(r"\d{2}/\d{4}", str(fatia.get("rotulo") or ""))
                              for fatia in resultado.get("fatias") or ()),
        "scanner": linhas_de_produto(scanner.get("itens"), margem_visivel=margem_visivel, a_confirmar=a_confirmar),
        "abaixo_custo": linhas_de_produto(abaixo, margem_visivel=False,
                                          pareto=marcas_pareto(abaixo, "perda", auditoria.get("perda_abaixo_custo"))),
        # Auditoria de custos (Etapa 1): a tabela de lucro bruto, os dois cards de custo e de sobra e o (i) curto.
        "lucro_por_produto": linhas_do_lucro(auditoria.get("margem_por_item"), a_confirmar=a_confirmar,
                                             separar=isinstance(auditoria.get("custo_a_confirmar"), dict))
        if margem_visivel else [],
        "custo_a_confirmar": linhas_de_produto(lista_a_confirmar, margem_visivel=False,
                                               pareto=marcas_pareto(lista_a_confirmar, "custo_total",
                                                                    bloco_a_confirmar.get("custo_total"))),
        "precificacao": linhas_de_produto(precificacao, margem_visivel=False,
                                          pareto=marcas_pareto(precificacao, "impacto")),
        "margem_card": margem_do_card(auditoria),
        "custo_card": custo_do_card(auditoria),
        "dicas_auditoria": DICAS_DA_AUDITORIA,
        "nota_isolados": nota_dos_isolados(_itens_fora(auditoria)),
        "margem_alta": _pct(MARGEM_ALTA),
        "margem_baixa": _pct(MARGEM_BAIXA),
        "limite": LIMITE_DAS_TABELAS,
        "limite_da_lista": raio_x.LIMITE_ITENS_LISTA,
        # A régua que pinta de vermelho o card de vendas sem custo, a mesma da recomendação de plano.
        "limiar_sem_custo": raio_x.LIMIAR_SEM_CUSTO,
        # As réguas do motor que os textos citam: os números saem das constantes, nunca escritos à mão.
        "alta_custo": _pct(raio_x.LIMIAR_ALTA_CUSTO),
        "margem_minima": _pct(indicadores_executivos.MARGEM_MINIMA),
        "razao_a_confirmar": raio_x.RAZAO_CUSTO_IMPLAUSIVEL,
    }
