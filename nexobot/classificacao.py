"""Camada B — classificação de intenções por evidências.

Pipeline:
  1. Segmenta a mensagem normalizada em orações (pontuação e conjunções que
     iniciam um novo verbo), marcando trechos citados.
  2. Analisa cada oração: ato de fala (ordem, pedido, desejo, pergunta,
     negação, operação, relato, cortesia...), alvo do comando e menções a
     destinos do catálogo, com a força de cada menção.
  3. Registra evidências pontuadas por intenção e resolve conflitos com regras
     explícitas e ordenadas (segurança > citação > operação > negação >
     comando > pergunta > cortesia), sem forçar uma classificação fraca.

O resultado (`Interpretacao`) não executa nem autoriza nada.
"""

from dataclasses import dataclass, field
import re

from . import saber, seguranca
from .contexto import Contexto
from .lexico import VERBOS_OPERACAO as _OP_VERBOS
from .intencoes import Evidencia, Intencao, Interpretacao
from .navegacao import FORTE, FRACA, mencoes
from .normalizacao import PONTUACAO, TextoNormalizado

# ---------------------------------------------------------------------------
# Léxico de atos de fala (texto canônico: sem acentos, "pra" → "para").
# ---------------------------------------------------------------------------
_NAV_INF = (r"(?:abrir|abre|acessar|acessa|ir|levar|leva|mostrar|mostra|direcionar|direciona|"
            r"redirecionar|encaminhar|mandar|colocar|coloca|entrar|voltar|navegar|exibir|carregar)")
_NAV_DESEJO = (r"(?:ir|acessar|acessa|abrir|abra|entrar|ver|consultar|voltar|navegar|visitar|conferir|"
               r"olhar|levar|leve|leva|direcionar|direcione|redirecionar|redirecione|mandar|mande|"
               r"colocar|coloque|mostrar|mostre|encaminhar|encaminhe)")
_ME_VERBO = re.compile(
    r"\bme (?:leve|leva|levar|levem|direcione|direciona|direcionar|redirecione|redireciona|"
    r"redirecionar|encaminhe|encaminha|mande|manda|coloque|coloca|ponha|poe|bota|bote|joga|jogue|"
    r"mostre|mostra|mostrar)\b"
)
_IMPERATIVO = re.compile(
    r"^(?:abra|abre|abrir|acesse|acessa|acessar|exiba|exibe|exibir|carregue|carrega|navegue|"
    r"navega|navegar|direcione|direciona|direcionar|redirecione|redireciona|redirecionar|"
    r"encaminhe|encaminha|leve|leva|levar|mostre|mostra|mostrar|coloque|coloca)\b"
)
_MOVIMENTO = re.compile(
    r"^(?:va|vai|volte|volta|voltar|ir|vamos|bora|partiu|entra|entre|entrar|segue|siga|manda|"
    r"mande|mandar)\b(?= (?:para|ate|ao|aos|a|no|na|nos|nas|em|la|ali)\b)(?! para mim\b)"
)
# "Quero o histórico": desejo cujo objeto é a própria página.
_DESEJO_OBJETO = re.compile(
    r"^(?:(?:nao|nunca) )?(?:quero|queria|preciso|precisava|gostaria|desejo)(?: de| do| da| dos| das)?\b"
    r"(?= (?:o|a|os|as|meu|minha|meus|minhas|esse|essa|aquele|aquela|historico|upload|dashboard|"
    r"painel|guia|suporte|manual|visao)\b)"
)
_MANDAR_PARA = re.compile(r"\b(?:mandar|manda|mande) (?:para|ate|ao|a)\b(?! mim\b)")
_DESEJO_NAV = re.compile(
    r"\b(?:quero|queria|gostaria|preciso|precisava|desejo|tenho que|quer|querendo) (?:de )?"
    rf"(?:que (?:voce |vc )?)?(?:me )?{_NAV_DESEJO}\b"
)
_POLIDO_NAV = re.compile(
    r"\b(?:pode|poderia|podia|consegue|conseguiria|tem como|teria como|da para|daria para|"
    r"seria possivel|eh possivel) (?:me |voce |por favor )?(?:me )?" + _NAV_INF + r"\b"
)
_ELIPTICO = re.compile(
    r"^(?:(?:e|entao|vamos|bora|so) )?(?:para|ate) (?:o |a )?(?:la|ali|ai|essa|esse|esta|este|aquela|"
    r"aquele)(?: (?:pagina|tela|parte|aba|area|secao|local|lugar|opcao))?$"
)
_INFORMATIVA = re.compile(
    r"\b(?:como|onde|cade|qual|quais|quando|quanto|quantos|quantas|por que|para que|o que|"
    r"explique|explica|explicar|ensine|ensina|ensinar|me diga|me diz|me fala|me conta|sabe|"
    r"saberia|duvida|existe|existem|significa|significado|funciona|funcionam|serve|servem|"
    r"posso|podemos|devo|quero saber|queria saber|gostaria de saber|preciso saber|diferenca|entender|"
    r"entenda|compreender|quero entender|queria entender|preciso entender)\b"
    r"|^porque\b"
    r"|\b(?:tem|ha|teria) (?:uma |um |alguma |algum )?(?:pagina|area|tela|lugar|local|aba|secao|"
    r"opcao|jeito|forma|parte|menu|botao|link|como (?:eu|ver|saber|consultar|recuperar))\b"
)
_VERBO_DE_VER = re.compile(r"(?:ver|consultar|olhar|conferir|mostrar|mostre|mostra)$")
_PERGUNTA_SEGUINTE = re.compile(r" (?:como|onde|qual|quais|o que|por que|se|quando)\b")
_NEGACAO = re.compile(r"\b(?:nao|nunca|jamais|nem)\b|\bsem (?:abrir|ir|navegar|acessar|levar|sair)\b")
_NEGACAO_COGNITIVA = re.compile(
    r"\b(?:nao|nunca) (?:\w+ )?(?:sei|consigo|consegui|conseguimos|encontro|encontrei|acho|achei|"
    r"vejo|vi|lembro|entendi|entendo|sabia|estou conseguindo|aparece|apareceu|aparecem|funciona|"
    r"funcionou|recebi|recebo|chegou|chega|localizo|localizei|carregou|abriu|entrou|deixa|deixou)\b"
)
_PROBLEMA = re.compile(
    r"\b(?:erro|errado|errada|problema|travou|trava|bugou|bug|falhou|falha|parou|nao carrega|"
    r"nao abre|nao entra|esqueci|perdi|bloqueado|bloqueada|expirou|expirado|invalido|invalida|"
    r"desativad[oa]|suspens[oa]|cancelad[oa]|inativ[oa]|sumiu|sumiram|desapareceu|desapareceram)\b"
)
_TAREFA_PESSOAL = re.compile(
    r"\b(?:quero|queria|gostaria de|gostaria|preciso|precisava|desejo|vou|tenho que|tentando|tentei|"
    r"tento) (?:de )?(?:fazer (?:o |um |uma )?)?(?:enviar|envia|envie|mandar|manda|anexar|anexa|"
    r"upload|criar|cria|abrir|abra|recuperar|redefinir|trocar|alterar|mudar|exportar|baixar|"
    r"corrigir|substituir|reenviar|falar)\b"
)
_CHAMADO_OBJETO = re.compile(r"^(?: ?(?:um|uma|novo|nova|meu|o|outro))* ?(?:chamado|ticket)s?\b")
_AUTOMACAO = re.compile(r"\b(?:automaticamente|automatic[oa]s?|sozinh[oa]|por mim|no meu lugar)\b")

_OP_IMPERATIVO = re.compile(rf"^(?:{_OP_VERBOS})\b")
_OP_POLIDO = re.compile(
    rf"\b(?:pode|poderia|podia|consegue|conseguiria) (?:me |por favor |voce )?(?:{_OP_VERBOS})\b"
)
_OP_SUBJUNTIVO = (
    "exclua|apague|delete|remova|limpe|publique|despublique|dispare|execute|rode|processe|"
    "reprocesse|homologue|aprove|altere|edite|modifique|mude|troque|cancele|atualize|cadastre|"
    "crie|gere|envie|mande|anexe|resete|redefina|bloqueie|desbloqueie|ative|desative|restaure|"
    "baixe|transfira|pague|faca|importe|zere|recalcule|corrija|ajuste|reenvie|responda|feche|"
    "encerre|reabra"
)
_OP_DELEGADA = re.compile(
    rf"\b(?:quero|queria|gostaria|preciso|desejo|peco|pode ser) que (?:voce |vc |o bot |o nexobot |"
    rf"o sistema )?(?:me )?(?:{_OP_SUBJUNTIVO})\b"
)
_OP_DESEJO_DESTRUTIVO = re.compile(
    r"\b(?:quero|queria|gostaria de|gostaria|preciso|precisava|desejo|vou) (?:de )?(?:excluir|apagar|"
    r"deletar|remover|limpar|publicar|despublicar|disparar|executar|rodar|processar|reprocessar|"
    r"homologar|aprovar|cancelar|zerar|recalcular|bloquear|desativar|editar|modificar)\b"
)
_OBJETO_CRIATIVO = re.compile(
    r"\b(?:poemas?|poesias?|textos?|historias?|historinhas?|musicas?|cancao|letras?|piadas?|"
    r"redacao|redacoes|cartas?|codigos?|programas?|scripts?|artigos?|versos?|rimas?|contos?|"
    r"ensaios?|resumos?|traducao|desenho)\b"
)
_CRIATIVO = re.compile(
    r"\b(?:escreva|escreve|escrever|crie|cria|criar|faca|faz|fazer|componha|compor|gere|gera|"
    r"gerar|invente|inventa|conte|conta|contar|redija|desenhe)\b(?: \w+){0,3} " + _OBJETO_CRIATIVO.pattern
)
_CONHECIMENTO_GERAL = re.compile(
    r"\bcapital d[aeo]s?\b|\bquem (?:foi|eh|descobriu|inventou|ganhou|escreveu)\b"
    r"|\bqual (?:eh )?a (?:altura|populacao|distancia|idade|moeda|lingua|cor)\b"
    r"|\bprevisao do tempo\b|\bque horas\b|\bque dia eh hoje\b|\bquanto eh \d|\b\d+ ?(?:mais|menos|vezes) ?\d+"
    r"|\btraduz\w*\b|\bcomo se diz\b|\bsignificado da palavra\b"
)
_INVESTIMENTO = re.compile(
    r"\b(?:acao|acoes) (?:da bolsa|na bolsa|para (?:comprar|investir)|de empresas?)\b"
    r"|\b(?:qual|quais|que) (?:acao|acoes) (?:\w+ )?(?:devo|eu devo|vale a pena|compro|comprar)\b"
    r"|\b(?:comprar|vender) acoes\b|\bbolsa de valores\b|\binvestir\b|\binvestimentos? (?:em|para)\b"
    r"|\b(?:bitcoin|criptomoedas?|cripto|tesouro direto|renda fixa|cdb|poupanca|fundo imobiliario|"
    r"day trade|cotacao do dolar)\b|\bonde (?:aplicar|investir)\b"
)
_ASSUNTO_GERAL = re.compile(
    r"\b(?:futebol|campeonato|filmes?|series?|novela|receita de (?:bolo|comida|\w+ caseir\w)|"
    r"culinaria|eleicao|eleicoes|horoscopo|signo|namorad[oa]|clima|temperatura|"
    r"noticias?|jogo do|time do|copa do mundo)\b"
)
# "Política" é assunto externo, salvo se o texto apontar para o documento do portal ou para a regra de senha.
_POLITICA = re.compile(r"\bpolitica\b")
_POLITICA_DO_PORTAL = re.compile(
    r"\bpolitica (?:(?:de|da) )?(?:privacidade|(?:troca de )?senhas?)\b|\btermos?(?: de uso)? e (?:a )?politica\b"
    r"|\bpolitica e (?:os )?termos?\b"
    r"|\baceit\w* (?:de novo )?(?:a |da |esta |essa )?(?:nova )?politica\b|\bpolitica (?:[\w,]+ ){0,5}aceit\w*\b"
)
_DADOS = re.compile(
    r"\b(?:qual|quais|quanto|quantos|quantas|me (?:diga|diz|fala|passa|informe|mostre|mostra)|"
    r"mostre|mostra|informe|calcule|calcula|some|soma|liste|lista)\b(?: \w+){0,5} "
    r"(?:faturamento|faturei|faturou|faturamos|vendi|vendeu|vendemos|lucro|lucrei|margem|margens|"
    r"cmv|receita|custos?|despesas?|saldo|ticket medio|valor(?:es)? (?:total|vendido|comprado|do indicador)|"
    r"total (?:vendido|comprado|de vendas|de compras)|produtos? mais vendidos?|ranking|top \d+|"
    r"rentabilidade|prejuizo|resultado financeiro|estoque atual)\b"
)
_DEFINICAO = re.compile(r"\b(?:significa|significado|quer dizer|definicao|conceito|o que eh|calculad[oa]|como eh)\b")
# "O NEXO calcula lucro?": pergunta sobre o produto, não pedido de valores.
_PRODUTO_CALCULA = re.compile(
    r"\b(?:o |a )?(?:nexo|sistema|portal|plataforma|ferramenta|voces|voce|vc) (?:calcula|calculam|mostra|mostram|"
    r"informa|informam|gera|geram|fornece|fornecem|apresenta|apresentam|exibe|exibem|tem|possui|da|entrega|"
    r"entregam|mede|medem|considera|trabalha com|usa|utiliza)\b"
)
# Pedido de ACESSO à administração (recusa) — perguntas sobre ela vão ao conhecimento.
_ACESSO_ADMIN = re.compile(
    r"\b(?:acess\w*|entrar|entro|entra|entrei|entre (?:na|no|em|nas|nos|la)|abr\w*|lev\w*|direcion\w*|redirecion\w*|"
    r"naveg\w*|mostr\w*|ir|va|vai|logar|login|senha|credencia\w*|permiss\w*|liber\w*|"
    r"(?:virar|tornar|ser|quero ser) (?:um |uma |o |a )?(?:admin|administrador\w*|superusuario)|"
    r"me (?:da|de|coloca|coloque|torne|torna|poe|ponha))\b"
)
_SAUDACAO = re.compile(
    r"^(?:oi|ola|e ai|bom dia|boa tarde|boa noite|boas|opa|fala|tudo bem|tudo bom|como vai|"
    r"como voce esta|tudo certo|eae)\b"
)
_DESPEDIDA = re.compile(
    r"\b(?:tchau|ate (?:logo|mais|breve|a proxima|amanha|depois)|falou|obrigad[oa]|valeu|agradeco|"
    r"grat[oa]|era (?:so|apenas) isso|so isso|nada mais|encerrar conversa|boa noite e obrigado)\b"
)
_AJUDA = re.compile(
    r"^(?:ajuda|me ajuda|me ajude|preciso de ajuda|socorro|menu|opcoes|help|comandos)$"
    r"|\bo que (?:voce|vc|o nexobot|o bot) (?:faz|pode fazer|sabe fazer|consegue fazer|sabe)\b"
    r"|\bcomo (?:voce|vc) (?:funciona|pode me ajudar|me ajuda)\b"
    r"|\bquem (?:eh|e) (?:voce|vc|o nexobot|o bot)\b|\bo que eh (?:o )?nexobot\b"
    r"|\bpara que (?:voce|vc) serve\b|\bcom o que (?:voce|vc) (?:pode )?(?:me )?ajuda\b"
    r"|\b(?:em que|como) (?:voce|vc) pode (?:me )?ajudar\b"
)
_HUMANO = re.compile(
    r"\bfalar com (?:a |o |um |uma |alguem (?:da |do )?)?(?:equipe|pessoa|humano|atendente|"
    r"consultor|alguem|gente)\b|\b(?:atendente|atendimento) humano\b|\bpessoa de verdade\b"
)
_ANTERIOR = re.compile(r"\b(?:anterior|anteriormente|de antes|que eu estava|onde eu estava)\b")
_CONFIRMACAO = frozenset({
    "sim", "claro", "pode", "pode sim", "pode ser", "quero", "quero sim", "isso", "isso mesmo",
    "exato", "ok", "bora", "vamos", "manda", "manda ver", "uhum", "aham", "positivo", "certo",
    "perfeito", "fechado", "com certeza", "yes", "demorou", "vai", "esta bom", "vamos la",
    "bora la", "sim quero", "sim pode", "pode ir", "pode abrir", "abre", "abra", "leva",
    "me leva", "me leve", "por favor", "sim por favor", "quero ir", "gostaria", "seria otimo",
})
_RECUSA = frozenset({
    "nao", "nao obrigado", "nao obrigada", "agora nao", "deixa", "deixa para la", "esquece",
    "nao precisa", "dispenso", "nada", "negativo", "nao quero", "cancela", "deixa quieto",
    "melhor nao", "ainda nao", "nao valeu", "nao vlw",
})

# Palavras permitidas entre o verbo de navegação e o destino nomeado.
_PREFIXO_DESTINO = frozenset("""
para ate a ao aos o os as na nas no nos em de da do das dos meu minha meus minhas seu sua seus
suas nosso nossa essa esse esta este nessa nesse nesta neste naquela naquele aquela aquele um uma
pagina tela local lugar parte aba secao area menu modulo opcao link botao inicial principal cliente
clientes portal nexo sistema site novo nova disponivel abrir ver consultar acessar entrar ir
falar com equipe onde eu posso pode por favor gentileza agora ja so somente logo rapidinho
rapido direto diretamente mim me voce entao tambem la ali aqui mesmo mesma tela minha nova
""".split())
# Palavras permitidas num pedido puramente contextual ("me leva pra lá").
_PALAVRAS_CONTEXTO = frozenset("""
entao agora ja sim por favor gentileza obrigado obrigada pode poderia consegue conseguiria
eu quero queria gostaria preciso desejo de que voce voces me mim leve leva levar direcione
direciona direcionar redirecione redireciona redirecionar mande manda mandar coloca coloque
colocar ponha poe abre abra abrir acesse acessa acessar vai va ir entrar entre ver consultar
voltar volta volte navegar mostrar mostre mostra para ate a ao em na no o essa esse esta este nessa
nesse nesta neste aquela aquele naquela naquele pagina tela local parte aba lugar secao area
la ali ai aqui direto diretamente rapidinho logo mesmo mesma tem como da seria possivel eh
bora vamos encaminhe encaminha encaminhar exiba exibe exibir
""".split())
_ANAFORAS = frozenset("la ali ai essa esse esta este nessa nesse nesta neste aquela aquele naquela naquele "
                      "mesma mesmo isso".split())
_LUGARES_GENERICOS = frozenset("portal sistema site nexo plataforma app aplicativo".split())
_ABERTURAS = frozenset("""
entao agora ja ok certo bom ah ahh opa ei oi ola olha nexobot bot assistente eu so tambem e mas
alias pois bem show perfeito isso sim beleza tipo assim bem depois
""".split())
_ABERTURAS_DUPLAS = (("por", "favor"), ("por", "gentileza"), ("na", "verdade"), ("na", "real"),
                     ("tudo", "bem"), ("sendo", "assim"))
_CORTESIA = frozenset("""
nexobot bot assistente ah ahh opa ei olha bom por favor gentileza obrigado obrigada
entao ok certo hum hmm tipo bem e
""".split())
_ELIPSE_PERGUNTA = frozenset("""
como onde cade qual e o a faco fazer faz vejo ver fica ficam acesso acessar entro entrar chego
chegar encontro encontrar acho achar funciona uso usar mexo eu isso agora ai ali la entao
que para posso pode consigo vou devo ficaria
""".split())
_VERBOS_SEPARADORES = re.compile(
    rf"^(?:{_OP_VERBOS}|abra|abre|abrir|acesse|acessa|acessar|me|va|vai|volte|volta|quero|queria|"
    r"preciso|gostaria|pode|poderia|consegue|como|onde|qual|quais|quando|quanto|o que|por que|para que|"
    r"existe|posso|tem como|da para|depois|entao|nao)\b"
)
_CONECTIVOS = frozenset({"e", "mas", "ou", "porem", "depois", "entao", "tambem"})
_CORTE_ESCOPO = re.compile(
    r"\b(?:porque|pois|lembra|lembrando|sobre|em vez de|em vez do|em vez da|ao inves de|estou|"
    r"estava|para (?:ver|saber|entender|aprender|conferir|olhar|tirar|resolver|falar)|"
    r"que eu (?:nao|ja)|so que|mas)\b"
)
_RELATIVO = frozenset({"onde", "que", "qual", "quais", "cujo", "cuja"})
_ANAFORA_DE_PAGINA = re.compile(
    r"\b(?:essa|esse|esta|este|nessa|nesse|nesta|neste|aquela|aquele|naquela|naquele|dessa|desse|"
    r"daquela|daquele) (?:pagina|tela|area|secao|aba|parte)\b"
    r"|\b(?:funciona|tem|existe|serve|faco|vejo) (?:la|ali|nela|nele)\b"
)
# Hipóteses e menções: "se eu disser ...", "você respondeu ...", "a expressão ...".
_META_MENSAGEM = re.compile(
    r"\b(?:se|caso|e se) (?:eu |voce |vc |alguem |o usuario |um cliente )?(?:disser|digo|diga|falar|falo|"
    r"fale|digitar|digito|digite|escrever|escrevo|escreva|pedir|peco|peca|perguntar|mandar|mande)\b"
    r"|\b(?:voce|vc|o bot|nexobot|o nexobot) (?:disse|respondeu|falou|escreveu|entende|entendeu|"
    r"reconhece|aceita|entenderia|reconheceria)\b"
    r"|\b(?:frase|expressao|exemplo|comando|palavra|mensagem)\b"
)
# Qualquer verbo de navegação, mesmo fora da gramática de ordem ("NÃO ABRA...").
_NAV_DIRETO = re.compile(r"\b(?:abr\w*|acess\w*|lev\w*|direcion\w*|redirecion\w*|ir|va|vai|"
                         r"mand\w* para|entr\w*|volt\w*|naveg\w*|mostr\w*)\b")
_VERBO_ACAO = re.compile(rf"\b(?:{_OP_VERBOS}|abr\w*|acess\w*|lev\w*|direcion\w*|redirecion\w*|"
                         r"mostr\w*|naveg\w*|volt\w*)\b")


# ---------------------------------------------------------------------------
# Perguntas amplas sobre o produto, aprofundamento de um assunto e comparações.
# ---------------------------------------------------------------------------
_SUJEITO_PRODUTO = (r"(?:o |a |esse |essa |este |esta |um |uma |do |da |desse |dessa |deste |desta |no |na |"
                    r"sobre o |sobre a )?(?:nexo|sistema|plataforma|portal|servico|ferramenta|faturamento inteligente|"
                    r"site|app|aplicativo|isso aqui|isso|isto|aqui|tudo isso)(?! (?:de|do|da|dos|das) \w)")
_PRODUTO = (
    (re.compile(rf"\bo que (?:eh|e|seria|significa) {_SUJEITO_PRODUTO}\b"), "nexo.visao_geral.o_que_e"),
    (re.compile(r"\b(?:quem (?:eh|e|sao) (?:voces|a nexo|o nexo)|o que voces fazem|o que a nexo faz|"
                r"me (?:fala|fale|conta|conte) (?:sobre|do|da) (?:o )?nexo|me explica o nexo|explique o nexo|"
                r"explica o nexo|me explique o nexo|o que (?:eh|e) (?:o )?faturamento inteligente)\b"),
     "nexo.visao_geral.o_que_e"),
    (re.compile(rf"\b(?:para|pra) que (?:serve|servem|existe) {_SUJEITO_PRODUTO}\b"
                r"|\bqual (?:eh |e )?(?:o |a )?(?:objetivo|finalidade|funcao|utilidade|proposito) (?:d[oa] |dess[ae] |da |do )?"
                r"(?:nexo|sistema|plataforma|portal|isso|ferramenta)\b"
                rf"|\b(?:que|qual) problema {_SUJEITO_PRODUTO} resolve\b"
                rf"|\bcomo {_SUJEITO_PRODUTO} (?:me )?ajuda\b|\bcomo isso (?:me )?ajuda\b"
                r"|\bpor que usar o nexo\b|\bvale a pena usar o nexo\b|\bo que (?:eu )?ganho com o nexo\b"),
     "nexo.visao_geral.para_que_serve"),
    (re.compile(r"\b(?:quais|que) (?:sao )?(?:as )?(?:funcionalidades|recursos|funcoes|servicos|modulos)\b"
                r"|\bo que (?:da|dah|tem) (?:para|pra) fazer\b"
                r"|\bo que (?:posso|consigo|eu posso|eu consigo) fazer (?:no|aqui|nesse|neste|nessa|nesta|com)\b"
                r"|\bo que (?:o |a )?(?:sistema|portal|plataforma|nexo) (?:faz|oferece|tem|permite|disponibiliza)\b"
                r"|\bo que (?:tem|existe|encontro|vejo|acho) (?:no|neste|nesse|nessa|nesta) (?:portal|sistema|plataforma)\b"
                r"|\bo que (?:eu )?faco aqui\b|\b(?:quais|que) (?:servicos|recursos) voces oferecem\b"),
     "nexo.visao_geral.funcionalidades"),
    (re.compile(r"\bcomo (?:eu )?(?:comeco|comecar|inicio|iniciar) (?:a )?(?:usar|utilizar|mexer)?\b"
                r"|\bpor onde (?:eu )?comeco\b|\bprimeiros passos\b|\b(?:sou|estou) nov[oa]\b|\bnunca usei\b"
                r"|\bo que (?:eu )?faco primeiro\b|\bcomo dar os primeiros passos\b|\bprimeira vez (?:aqui|no portal|no nexo)\b"),
     "nexo.primeiros_passos.roteiro"),
    (re.compile(rf"\bcomo {_SUJEITO_PRODUTO} funciona\b|\bcomo funciona {_SUJEITO_PRODUTO}\b"
                r"|\b(?:como (?:eh|e) )?o funcionamento\b|\bfuncionamento (?:do|da|completo|geral|inteiro|todo)\b"
                r"|\b(?:quais|como) (?:sao )?(?:as )?etapas\b|\bqual (?:eh |e )?(?:o )?(?:fluxo|processo) (?:do|da|dessa|desse)\b"
                r"|\bcomo (?:eh|e) o processo\b|\bcomo (?:minha|a minha) empresa (?:usa|utiliza|vai usar|pode usar)\b"
                r"|\bcomo (?:eu )?(?:uso|utilizo|usar|utilizar) (?:o |a )?(?:nexo|sistema|plataforma|portal)\b"
                r"|\b(?:entender|conhecer|saber|explica|explique|explicar) (?:todo o |o |melhor o |sobre o |como funciona o )?"
                r"(?:funcionamento|nexo|sistema|plataforma)\b"),
     "nexo.visao_geral.funcionamento"),
)
_COMO_FUNCIONA_SOLTO = re.compile(r"^(?:e |mas |entao |ok |ta |certo )*como (?:isso |isto |ele |ela )?funciona(?: isso| isto)?\s*[?!.]*$")
_APROFUNDAR = (
    ("exemplo", re.compile(r"\b(?:exemplo|exemplos|exemplifica|exemplifique|me mostra um caso|como seria na pratica)\b")),
    ("passos", re.compile(r"\b(?:passo a passo|na pratica|como (?:eu )?(?:faco|fazer|faz) (?:isso|isto|esse|essa|isso na pratica)|"
                          r"como faco|como fazer|quais (?:sao )?os passos|me ensina|como proceder|como executo|como eu uso isso)\b")),
    ("resumir", re.compile(r"\b(?:resum\w*|mais curto|menos texto|simplifica|de forma simples|em poucas palavras|em uma frase|"
                           r"versao curta|so o essencial)\b")),
    ("nao_entendi", re.compile(r"\b(?:nao entendi|nao compreendi|nao ficou claro|ficou confuso|confuso|nao entendo|"
                               r"pode explicar de novo|explica de novo|de outra forma|de outro jeito|nao captei|nao saquei)\b")),
    ("proximo", re.compile(r"^(?:e |ok |certo |entendi |beleza |ta |esta |show |legal |otimo |perfeito )*(?:e )?"
                           r"(?:depois|e depois|e agora|e ai|proximo passo|qual (?:eh |e )?(?:o )?proximo passo|o que mais|"
                           r"o que vem depois|e na sequencia|continua|continue|prossiga|segue|e entao|o que vem agora|"
                           r"qual o proximo|proxima etapa|proximo)\s*[?!.]*$")),
    ("onde", re.compile(r"\b(?:onde (?:fica|faco|vejo|encontro|acho|esta|eh|e) (?:isso|isto|essa|esse|essa parte|"
                        r"essa pagina|essa funcionalidade|essa opcao|essa tela)?|me (?:mostra|mostre) onde (?:fica|esta|faco|vejo)|"
                        r"em que (?:pagina|tela|lugar) (?:fica|faco|vejo) isso)\s*[?!.]*$")),
    ("explicar", re.compile(r"\b(?:explique melhor|explica melhor|explique mais|explica mais|me explica melhor|mais detalhes|"
                            r"detalhe mais|detalha|aprofund\w*|me conta mais|fala mais|quero saber mais|me diga mais|conte mais|"
                            r"pode detalhar|como assim|elabora|desenvolve|explica isso|explique isso|me explica isso|"
                            r"explica direito|explique melhor isso)\b"
                            r"|^(?:e |mas )?(?:por que|porque|por qual motivo)\s*[?!.]*$")),
)
_COMPARA = re.compile(r"\b(?:diferenca|diferencas|diferente|diferentes|diferem|diferenciar|comparar|comparacao|comparativo|"
                      r"versus|vs|distingu\w*|distincao)\b|\b(?:qual|quais) (?:a |as )?(?:relacao|relacoes) entre\b"
                      r"|\bo que (?:muda|difere) (?:entre|de)\b")
_DEFINICAO_INICIAL = re.compile(r"^(?:\w+ ){0,2}(?:o que (?:eh|e|sao|significa)|qual (?:eh |e )?o significado)\b")


def pergunta_sobre_o_produto(canonico: str) -> str | None:
    """Id do registro da visão geral quando a pergunta é ampla sobre o NEXO."""
    for padrao, registro_id in _PRODUTO:
        if padrao.search(canonico):
            return registro_id
    return None


def pedido_de_aprofundamento(canonico: str) -> str | None:
    """Rótulo do pedido ("exemplo", "passos", "proximo"...) quando há um assunto em foco."""
    if _COMO_FUNCIONA_SOLTO.match(canonico):
        return "explicar"
    for rotulo, padrao in _APROFUNDAR:
        if padrao.search(canonico):
            return rotulo
    return None


@dataclass
class Oracao:
    tokens: tuple
    citada: bool
    pergunta: bool
    texto: str = ""
    tipo: str = "outro"
    destinos: dict = field(default_factory=dict)      # destino -> força (oração inteira)
    alvos: dict = field(default_factory=dict)         # destino -> força (alvo do comando)
    desconhecidas: tuple = ()                         # conteúdo não reconhecido no alvo
    contextual: bool = False                          # alvo é anafórico/elíptico
    anterior: bool = False                            # alvo é "a anterior"
    lugar_generico: bool = False                      # "me leve para o portal"
    operacao: str | None = None
    operacao_imperativa: bool = False                 # veio de um verbo solto no início da oração
    separador: str = ""                               # pontuação/conectivo que a separou da anterior
    meta: bool = False

    def __post_init__(self):
        self.texto = " ".join(self.tokens)


# ---------------------------------------------------------------------------
# Segmentação
# ---------------------------------------------------------------------------
def segmentar(normalizado: TextoNormalizado) -> list[Oracao]:
    oracoes, atual, citados_atual = [], [], []
    separador = [""]

    def fechar(pergunta=False, proximo=""):
        if atual:
            citada = sum(citados_atual) > len(atual) / 2
            oracoes.append(Oracao(tuple(atual), citada, pergunta, separador=separador[0]))
        atual.clear()
        citados_atual.clear()
        separador[0] = proximo

    tokens = normalizado.tokens
    for i, token in enumerate(tokens):
        if token in PONTUACAO:
            fechar(pergunta=token == "?", proximo=token)
            continue
        if (token in _CONECTIVOS and atual and i + 1 < len(tokens)
                and i not in normalizado.citados
                and _VERBOS_SEPARADORES.match(" ".join(tokens[i + 1:i + 3]))):
            fechar(proximo=token)
            continue
        atual.append(token)
        citados_atual.append(i in normalizado.citados)
    fechar(pergunta=normalizado.pergunta and not oracoes)
    if normalizado.pergunta and oracoes and not any(o.pergunta for o in oracoes):
        oracoes[-1].pergunta = True
    return oracoes


def _sem_abertura(tokens: tuple) -> tuple:
    """Remove interjeições, vocativos e cortesias do início da oração."""
    i = 0
    while i < len(tokens):
        if i + 1 < len(tokens) and (tokens[i], tokens[i + 1]) in _ABERTURAS_DUPLAS:
            i += 2
        elif tokens[i] in _ABERTURAS:
            i += 1
        else:
            break
    return tokens[i:]


# ---------------------------------------------------------------------------
# Análise de uma oração
# ---------------------------------------------------------------------------
def _alvo_do_comando(escopo: str, oracao: Oracao) -> None:
    """Lê o destino pedido depois do verbo, sem usar o que veio antes dele."""
    corte = _CORTE_ESCOPO.search(escopo)
    if corte:
        escopo = escopo[:corte.start()]
    escopo = escopo.strip()
    palavras = escopo.split()
    achadas = mencoes(escopo)
    if achadas:
        primeira = achadas[0]
        prefixo = escopo[:primeira.inicio].split()
        relativo = next((i for i, p in enumerate(prefixo) if p in _RELATIVO), None)
        antes_relativo = prefixo if relativo is None else prefixo[:relativo]
        desconhecidas = tuple(p for p in antes_relativo if p not in _PREFIXO_DESTINO)
        if desconhecidas:
            oracao.desconhecidas = desconhecidas
            return
        alvos = {}
        for m in achadas:
            alvos[m.destino] = max(alvos.get(m.destino, 0.0), m.forca)
        # Um destino nomeado prevalece sobre assuntos apenas relacionados.
        if any(f >= FORTE for f in alvos.values()):
            alvos = {d: f for d, f in alvos.items() if f >= FORTE}
        oracao.alvos = alvos
        return
    if _ANTERIOR.search(escopo):
        oracao.anterior = True
        oracao.contextual = True
        return
    if set(palavras) <= _PALAVRAS_CONTEXTO and (not palavras or set(palavras) & _ANAFORAS
                                                 or set(palavras) <= {"para", "me", "mim", "a", "o"}):
        oracao.contextual = True
        return
    restantes = [p for p in palavras if p not in _PALAVRAS_CONTEXTO and p not in _PREFIXO_DESTINO]
    if not restantes and set(palavras) & _LUGARES_GENERICOS:
        oracao.lugar_generico = True
        return
    oracao.desconhecidas = tuple(p for p in palavras if p not in _PREFIXO_DESTINO
                                 and p not in _PALAVRAS_CONTEXTO) or tuple(palavras)


def _operacao(texto: str, nucleo: str) -> tuple[str | None, bool]:
    """Pedido para o bot executar algo: (verbo da operação, veio de imperativo solto)."""
    for padrao, alvo in ((_OP_DELEGADA, texto), (_OP_POLIDO, texto),
                         (_OP_DESEJO_DESTRUTIVO, texto), (_OP_IMPERATIVO, nucleo)):
        m = padrao.search(alvo)
        if m:
            return m.group(0).split()[-1], padrao is _OP_IMPERATIVO
    return None, False


def fundir_fragmentos(oracoes: list) -> list:
    """Verbo de operação solto após vírgula/"ou"/"e" dentro de uma pergunta.

    "Como a equipe publica ou despublica uma análise?" e "Posso pedir ao NexoBot
    para criar, excluir, publicar ou alterar algo?" são perguntas sobre essas
    operações, não ordens ao bot: o fragmento volta para a oração interrogativa
    anterior, que é analisada de novo por inteiro.
    """
    resultado = []
    for o in oracoes:
        anterior = resultado[-1] if resultado else None
        if (anterior is not None and anterior.tipo == "pergunta" and o.tipo == "operacao"
                and o.operacao_imperativa and o.separador in (",", "ou", "e") and not anterior.citada):
            conectivo = (o.separador,) if o.separador in ("ou", "e") else ()
            fundida = Oracao(anterior.tokens + conectivo + o.tokens, anterior.citada,
                             anterior.pergunta or o.pergunta, separador=anterior.separador)
            resultado[-1] = analisar_oracao(fundida)
            continue
        resultado.append(o)
    return resultado


def analisar_oracao(oracao: Oracao) -> Oracao:
    tokens = _sem_abertura(oracao.tokens)
    nucleo = " ".join(tokens)
    texto = oracao.texto
    oracao.destinos = {}
    for m in mencoes(texto):
        oracao.destinos[m.destino] = max(oracao.destinos.get(m.destino, 0.0), m.forca)
    if oracao.citada:
        oracao.meta = True
    if not nucleo:
        oracao.tipo = "cortesia"
        return oracao
    if set(tokens) <= _CORTESIA:
        oracao.tipo = "cortesia"
        return oracao

    # Pedido de valores ("me mostra o faturamento") não é ordem de navegação.
    # Comparar termos ou perguntar o que o NEXO calcula é dúvida, não pedido de dados.
    pede_dados = bool(_DADOS.search(texto)) and not _DEFINICAO.search(texto) and not _COMPARA.search(texto) \
        and not _PRODUTO_CALCULA.search(texto)
    if pede_dados and not _NEGACAO.search(texto):
        oracao.tipo = "dados"
        return oracao

    # Ordem de navegação (imperativo, "me leve", movimento, desejo ou pedido polido).
    candidatos = []
    for padrao, alvo, deslocamento in (
        (_IMPERATIVO, nucleo, len(texto) - len(nucleo)),
        (_MOVIMENTO, nucleo, len(texto) - len(nucleo)),
        (_ME_VERBO, texto, 0), (_DESEJO_NAV, texto, 0), (_POLIDO_NAV, texto, 0),
    ):
        m = padrao.search(alvo)
        if m:
            candidatos.append((m.start() + deslocamento, m.end() + deslocamento, padrao))
    comando = min(candidatos, key=lambda c: c[0]) if candidatos else None
    eliptico = bool(_ELIPTICO.match(nucleo))
    desejo = _DESEJO_OBJETO.match(nucleo)
    if not comando and desejo:
        # "Quero o histórico": só vale se o objeto for uma página nomeada.
        teste = Oracao((), False, False)
        inicio = len(texto) - len(nucleo)
        _alvo_do_comando(texto[inicio + desejo.end():], teste)
        if teste.alvos and max(teste.alvos.values()) >= FORTE and not teste.desconhecidas:
            comando = (inicio + desejo.start(), inicio + desejo.end(), _DESEJO_OBJETO)

    informativa = _INFORMATIVA.search(texto)
    if comando and comando[2] in (_DESEJO_NAV, _POLIDO_NAV):
        # "Quero abrir um chamado" / "pode abrir um chamado por mim?": trata-se do
        # chamado em si (tarefa ou dúvida), não da página Suporte.
        resto = texto[comando[1]:]
        if texto[comando[0]:comando[1]].endswith(("abrir", "abra")) and _CHAMADO_OBJETO.match(resto):
            comando = None
    if comando and _VERBO_DE_VER.search(texto[comando[0]:comando[1]]) and _PERGUNTA_SEGUINTE.match(
            texto[comando[1]:]):
        # "Quero ver como funciona o upload": pede explicação, não navegação.
        informativa = _PERGUNTA_SEGUINTE.match(texto[comando[1]:])
        comando = None
    if comando and informativa and informativa.start() < comando[0]:
        comando = None
    if comando and _META_MENSAGEM.search(texto[:comando[0]]):
        oracao.meta = True

    # Operações: o bot nunca executa; desejos de autoatendimento são orientados.
    op, imperativa = _operacao(texto, nucleo)
    if op and (_MOVIMENTO.match(nucleo) or (op in ("mandar", "manda", "mande")
                                            and _MANDAR_PARA.search(texto))):
        op = None  # "manda para o upload" / "pode me mandar para lá" é navegação
    if op and _CRIATIVO.search(texto):
        oracao.tipo = "fora_escopo"
        return oracao
    tarefa = _TAREFA_PESSOAL.search(texto)
    if op and tarefa and not _OP_DELEGADA.search(texto) and not _OP_DESEJO_DESTRUTIVO.search(texto):
        if not _OP_IMPERATIVO.match(nucleo) or tarefa.start() == 0:
            op = None
    if op and informativa and informativa.start() < texto.find(op) and not _OP_DELEGADA.search(texto):
        op = None  # "como excluo uma análise?" é uma dúvida
    if not op and tarefa and _AUTOMACAO.search(texto):
        op, imperativa = tarefa.group(0).split()[-1], False
    if tarefa and informativa and informativa.start() < tarefa.start():
        tarefa = None  # "por que preciso mandar compras?" é uma pergunta
    if op:
        oracao.tipo = "operacao"
        oracao.operacao = op
        oracao.operacao_imperativa = imperativa
        return oracao

    negada = _NEGACAO.search(texto)
    # "Não sei onde fica", "não consigo entrar": dificuldade relatada, não recusa.
    relato = bool(_NEGACAO_COGNITIVA.search(texto))
    if negada and relato and _NEGACAO_COGNITIVA.search(texto).start() == negada.start():
        negada = None
    if negada and comando and negada.start() > comando[0]:
        negada = None  # "me leva para uma página que não existe": a ordem não foi negada
    if negada and any(m.inicio < negada.start() for m in mencoes(texto)) and not (
            comando and comando[0] < negada.start()):
        # "O upload não abre": relato de problema sobre a página, não uma recusa.
        negada, relato = None, True
    if negada and (comando or eliptico or _NAV_DIRETO.search(texto)):
        oracao.tipo = "negacao"
        escopo = texto[comando[1]:] if comando else texto
        oracao.alvos = {d: f for d, f in oracao.destinos.items()}
        oracao.contextual = not oracao.alvos and bool(set(escopo.split()) & _ANAFORAS)
        return oracao

    if (comando or eliptico) and not relato:
        oracao.tipo = "comando"
        if eliptico and not comando:
            oracao.contextual = True
        else:
            _alvo_do_comando(texto[comando[1]:], oracao)
        return oracao

    if pede_dados:
        oracao.tipo = "dados"
        return oracao
    if _HUMANO.search(texto) and not _COMPARA.search(texto):
        oracao.tipo = "humano"
        return oracao
    if tarefa and not relato:
        oracao.tipo = "tarefa"
        return oracao
    if (not informativa and not relato and oracao.destinos
            and set(tokens) <= (_PREFIXO_DESTINO | _CORTESIA | texto_de_destinos(oracao))):
        oracao.tipo = "topico"  # "E o histórico?", "Upload?"
        return oracao
    if informativa or relato or oracao.pergunta:
        oracao.tipo = "pergunta"
        restantes = set(tokens) - _ELIPSE_PERGUNTA
        # "Onde vejo?" (elipse) ou "o que tem naquela página?" (anáfora a uma página).
        oracao.contextual = not oracao.destinos and (
            not restantes or bool(_ANAFORA_DE_PAGINA.search(texto)))
        return oracao
    if _PROBLEMA.search(texto):
        oracao.tipo = "pergunta"
        return oracao
    oracao.tipo = "outro"
    return oracao


def texto_de_destinos(oracao: Oracao) -> set:
    """Palavras que pertencem às menções de destino da oração."""
    palavras = set()
    for m in mencoes(oracao.texto):
        palavras.update(oracao.texto[m.inicio:m.fim].split())
    return palavras


# ---------------------------------------------------------------------------
# Classificação da mensagem
# ---------------------------------------------------------------------------
def _resultado(intencao, confianca, evidencias, **kwargs) -> Interpretacao:
    return Interpretacao(intencao=intencao, confianca=round(min(max(confianca, 0.0), 1.0), 2),
                         evidencias=evidencias, **kwargs)


def _unico_destino(destinos: dict):
    """(destino, força) quando há exatamente um candidato após priorizar os fortes."""
    if not destinos:
        return None, 0.0
    fortes = {d: f for d, f in destinos.items() if f >= FORTE}
    escolha = fortes or destinos
    if len(escolha) != 1:
        return None, 0.0
    destino, forca = next(iter(escolha.items()))
    return destino, forca


def _destinos_ambiguos_relatorio(texto: str) -> bool:
    """'Relatório?' sozinho: enviar um novo ou consultar um anterior?"""
    return bool(re.fullmatch(r"(?:e |o |os |meu |meus |sobre )*(?:relatorios?|analises?)", texto))


def classificar(normalizado: TextoNormalizado, contexto: Contexto | None = None) -> Interpretacao:
    contexto = contexto or Contexto()
    ev: list[Evidencia] = []
    canonico = normalizado.canonico
    bruto = normalizado.bruto

    if normalizado.vazio:
        return _resultado(Intencao.VAZIA, 1.0, ev, motivo="mensagem_vazia")
    if normalizado.so_simbolos:
        return _resultado(Intencao.SEM_TEXTO, 1.0, ev, motivo="sem_palavras")

    # 1) Segurança: URLs/caminhos, área administrativa, injeção e código.
    if seguranca.contem_codigo(bruto):
        ev.append(Evidencia(Intencao.ENTRADA_SUSPEITA, 3.0, "codigo_ou_sql"))
        return _resultado(Intencao.ENTRADA_SUSPEITA, 0.95, ev, motivo="codigo")
    if seguranca.contem_url(bruto):
        ev.append(Evidencia(Intencao.ACESSO_RESTRITO, 3.0, "url_ou_caminho"))
        return _resultado(Intencao.ACESSO_RESTRITO, 0.99, ev, motivo="url")
    if seguranca.menciona_admin(canonico) and (_ACESSO_ADMIN.search(canonico)
                                              or not _INFORMATIVA.search(canonico)):
        # Pedido de acesso/navegação à administração: recusa. Perguntas sobre o
        # que a equipe faz ("o que é a lixeira?") seguem para a base de conhecimento.
        ev.append(Evidencia(Intencao.ACESSO_RESTRITO, 3.0, "area_administrativa"))
        return _resultado(Intencao.ACESSO_RESTRITO, 0.97, ev, motivo="admin")
    if seguranca.tentativa_injecao(canonico, bruto):
        ev.append(Evidencia(Intencao.ENTRADA_SUSPEITA, 3.0, "injecao_de_instrucoes"))
        return _resultado(Intencao.ENTRADA_SUSPEITA, 0.95, ev, motivo="injecao")
    if seguranca.pede_outra_empresa(canonico):
        ev.append(Evidencia(Intencao.CONSULTA_DADOS, 3.0, "dados_de_outra_empresa"))
        return _resultado(Intencao.CONSULTA_DADOS, 0.95, ev, motivo="outra_empresa")

    oracoes = [analisar_oracao(o) for o in segmentar(normalizado)]
    oracoes = fundir_fragmentos([o for o in oracoes if o.tokens])
    saudacao = bool(oracoes) and bool(_SAUDACAO.match(" ".join(_sem_abertura(oracoes[0].tokens))
                                                     or oracoes[0].texto))
    # Cumprimentos ("oi", "tudo bem?") e vocativos não são o assunto da mensagem.
    substantivas = [o for o in oracoes if o.tipo != "cortesia"
                    and not (o.tipo in ("outro", "pergunta") and set(o.tokens) <= _PALAVRAS_SAUDACAO)]
    for o in oracoes:
        ev.append(Evidencia(_INTENCAO_DO_TIPO.get(o.tipo, Intencao.DESCONHECIDA),
                            _PESO_DO_TIPO.get(o.tipo, 0.2), f"oracao:{o.tipo}"))
    base = dict(saudacao=saudacao)
    destinos_mencionados = {}
    for o in substantivas:
        for d, f in o.destinos.items():
            destinos_mencionados[d] = max(destinos_mencionados.get(d, 0.0), f)

    # 2) Comandos citados, hipóteses e menções: falam SOBRE o bot, não são ordens.
    meta = _META_MENSAGEM.search(canonico)
    verbo = _VERBO_ACAO.search(canonico)
    if (meta and verbo and meta.start() < verbo.start()) or any(
            o.meta and o.tipo in ("comando", "negacao", "operacao") for o in oracoes) or any(
            o.citada and _VERBO_ACAO.search(o.texto) for o in oracoes):
        ev.append(Evidencia(Intencao.SOBRE_O_BOT, 2.5, "comando_citado"))
        return _resultado(Intencao.SOBRE_O_BOT, 0.85, ev, motivo="comando_citado", **base)

    # 3) Operações: recusa explícita, sem ignorar a parte navegável.
    operacoes = tuple(dict.fromkeys(o.operacao for o in oracoes if o.tipo == "operacao"))
    if operacoes:
        sugestao = _destino_sugerivel(oracoes, contexto)
        return _resultado(Intencao.OPERACAO_NAO_PERMITIDA, 0.95, ev, operacoes=operacoes,
                          destino=sugestao, forca_destino=FRACA if sugestao else 0.0,
                          consulta=canonico, motivo="operacao", **base)

    if any(o.tipo == "fora_escopo" for o in oracoes) or (
            not destinos_mencionados and _CRIATIVO.search(canonico)):
        return _resultado(Intencao.FORA_DO_ESCOPO, 0.9, ev, motivo="pedido_criativo", **base)

    # 4) Pedidos de valores financeiros: sem acesso pela conversa.
    if any(o.tipo == "dados" for o in oracoes):
        return _resultado(Intencao.CONSULTA_DADOS, 0.9, ev, destino="dashboard",
                          forca_destino=FRACA, consulta=canonico, motivo="valores", **base)

    # 4b) Aprofundar o assunto em foco ("explique melhor", "exemplo", "e depois?").
    if contexto.registro is not None and not destinos_mencionados:
        pedido = pedido_de_aprofundamento(canonico)
        if pedido:
            return _resultado(Intencao.APROFUNDAR, 0.85, ev, consulta=canonico, motivo=pedido,
                              detalhes={"assunto": contexto.assunto}, **base)

    comandos = [o for o in oracoes if o.tipo == "comando"]
    negadas = [o for o in oracoes if o.tipo == "negacao"]
    excluidos = {d for o in negadas for d in o.alvos}
    if negadas and contexto.ultimo_destino and any(o.contextual for o in negadas):
        excluidos.add(contexto.ultimo_destino)

    # 5) Negação: nunca navega para o destino negado.
    if negadas and not comandos:
        return _resultado(Intencao.COMANDO_NEGADO, 0.95, ev, motivo="negacao", **base)

    # 6) Comandos de navegação.
    if comandos:
        return _decidir_comando(comandos, oracoes, excluidos, contexto, ev, base)

    texto_total = " ".join(o.texto for o in substantivas)
    if not destinos_mencionados and (_AJUDA.search(texto_total) or _AJUDA.search(
            " ".join(_sem_abertura(tuple(texto_total.split()))))):
        return _resultado(Intencao.AJUDA_GERAL, 0.9, ev, motivo="ajuda", **base)

    # 7) Assuntos externos ao portal (antes das perguntas genéricas).
    if not any(f >= FORTE for f in destinos_mencionados.values()) and (
            _CONHECIMENTO_GERAL.search(canonico) or _INVESTIMENTO.search(canonico)
            or _ASSUNTO_GERAL.search(canonico)
            or (_POLITICA.search(canonico) and not _POLITICA_DO_PORTAL.search(canonico))):
        return _resultado(Intencao.FORA_DO_ESCOPO, 0.85, ev, motivo="assunto_externo", **base)

    # 8) Confirmação/recusa de uma oferta anterior.
    nucleo = " ".join(_sem_abertura(tuple(t for o in substantivas for t in o.tokens)))
    if nucleo in _RECUSA:
        return _resultado(Intencao.RECUSA, 0.9, ev, motivo="recusa", **base)
    if nucleo in _CONFIRMACAO or (not nucleo and any(o.texto in _CONFIRMACAO for o in oracoes)):
        if contexto.opcoes:
            return _resultado(Intencao.AMBIGUIDADE, 0.7, ev, opcoes=contexto.opcoes,
                              modo_opcoes=contexto.modo_opcoes, motivo="confirmacao_com_opcoes", **base)
        if contexto.oferta:
            # "Sim" só aceita o convite feito na resposta imediatamente anterior.
            return _confirmar_navegacao(contexto.oferta, contexto, ev, base, 0.85, "confirmacao")
        return _resultado(Intencao.CONFIRMACAO, 0.6, ev, motivo="confirmacao_sem_contexto", **base)

    # 9) Escolha entre opções oferecidas num esclarecimento anterior.
    if contexto.opcoes:
        escolhido = _escolha_de_opcao(substantivas, contexto)
        if escolhido:
            if contexto.modo_opcoes == "navegar":
                return _confirmar_navegacao(escolhido, contexto, ev, base, 0.85, "escolha_de_opcao")
            return _resultado(Intencao.ORIENTACAO_NAVEGACAO, 0.85, ev, destino=escolhido,
                              forca_destino=FORTE, consulta=canonico, motivo="escolha_de_opcao", **base)

    if any(o.tipo == "humano" for o in oracoes):
        return _resultado(Intencao.SUPORTE_HUMANO, 0.9, ev, destino="suporte", forca_destino=FORTE,
                          consulta=canonico, motivo="pedido_humano", **base)

    # 9b) Comparações entre termos do NEXO ("diferença entre faturamento e pressão de estoque").
    # "O que é o histórico comparativo?" define um nome: fica para a busca.
    comparacao = _COMPARA.search(canonico)
    if comparacao and not (_DEFINICAO_INICIAL.match(canonico)
                           and comparacao.group(0).startswith("compara")):
        pares = saber.termos_citados(canonico)
        ids = list(dict.fromkeys(rid for _, rid in pares))
        if ids:
            return _resultado(Intencao.COMPARACAO, 0.85, ev, consulta=canonico, motivo="comparacao",
                              detalhes={"pares": pares}, **base)

    # 9c) Perguntas amplas sobre o produto ("o que é o NEXO?", "como funciona?").
    if not any(f >= FORTE for f in destinos_mencionados.values()):
        if _COMO_FUNCIONA_SOLTO.match(canonico):
            return _resultado(Intencao.SOBRE_O_NEXO, 0.8, ev, consulta=canonico, motivo="funcionamento",
                              detalhes={"registro": "nexo.visao_geral.funcionamento"}, **base)
        registro_id = pergunta_sobre_o_produto(canonico)
        if registro_id:
            return _resultado(Intencao.SOBRE_O_NEXO, 0.9, ev, consulta=canonico, motivo="sobre_o_produto",
                              detalhes={"registro": registro_id}, **base)

    # 10) Perguntas, tarefas pessoais e tópicos: orientação (nunca navegação automática).
    informativas = [o for o in substantivas if o.tipo in ("pergunta", "tarefa", "topico")]
    if informativas:
        return _decidir_informativa(informativas, substantivas, contexto, ev, base, canonico)

    # 11) Cortesias e mensagens sem assunto reconhecido.
    if _DESPEDIDA.search(canonico) and len(normalizado.palavras) <= 10:
        return _resultado(Intencao.DESPEDIDA, 0.9, ev, motivo="despedida", **base)
    if not substantivas or (saudacao and len(normalizado.palavras) <= 6):
        return _resultado(Intencao.SAUDACAO, 0.85, ev, motivo="saudacao", **base)
    if not destinos_mencionados and _destinos_ambiguos_relatorio(" ".join(_sem_abertura(
            tuple(texto_total.split())))):
        return _resultado(Intencao.AMBIGUIDADE, 0.7, ev, opcoes=("upload", "historico"),
                          modo_opcoes="orientar", motivo="relatorio_ambiguo", **base)
    if destinos_mencionados:
        destino, forca = _unico_destino(destinos_mencionados)
        if destino:
            return _resultado(Intencao.ORIENTACAO_NAVEGACAO, 0.6 if forca >= FORTE else 0.45, ev,
                              destino=destino, forca_destino=forca, consulta=canonico,
                              motivo="mencao_de_destino", **base)
    return _resultado(Intencao.DESCONHECIDA, 0.2, ev, consulta=canonico, motivo="sem_evidencia", **base)


_PALAVRAS_SAUDACAO = frozenset("oi ola e ai bom boa dia tarde noite boas opa fala tudo bem bom como vai "
                               "voce esta certo eae nexobot bot assistente".split())
_INTENCAO_DO_TIPO = {
    "comando": Intencao.NAVEGAR, "negacao": Intencao.COMANDO_NEGADO,
    "operacao": Intencao.OPERACAO_NAO_PERMITIDA, "pergunta": Intencao.DUVIDA_FUNCIONAL,
    "tarefa": Intencao.ORIENTACAO_NAVEGACAO, "topico": Intencao.ORIENTACAO_NAVEGACAO,
    "dados": Intencao.CONSULTA_DADOS, "humano": Intencao.SUPORTE_HUMANO,
    "fora_escopo": Intencao.FORA_DO_ESCOPO,
}
_PESO_DO_TIPO = {"operacao": 2.0, "negacao": 1.5, "comando": 1.0, "dados": 1.0,
                 "pergunta": 0.8, "tarefa": 0.7, "topico": 0.5, "humano": 0.9, "fora_escopo": 1.0}


def _destino_sugerivel(oracoes, contexto) -> str | None:
    """Parte navegável de uma mensagem recusada: vira botão, nunca navegação."""
    alvos = {}
    for o in oracoes:
        if o.tipo == "comando":
            alvos.update(o.alvos)
            if o.contextual and contexto.ultimo_destino:
                alvos.setdefault(contexto.ultimo_destino, FORTE)
    destino, _ = _unico_destino(alvos)
    return destino


def _escolha_de_opcao(oracoes, contexto) -> str | None:
    texto = " ".join(o.texto for o in oracoes)
    ordinais = {"primeiro": 0, "primeira": 0, "1": 0, "segundo": 1, "segunda": 1, "2": 1,
                "terceiro": 2, "terceira": 2, "3": 2, "ultimo": -1, "ultima": -1}
    for palavra, indice in ordinais.items():
        if re.search(rf"\b{palavra}\b", texto):
            try:
                return contexto.opcoes[indice]
            except IndexError:
                return None
    citados = {d: f for o in oracoes for d, f in o.destinos.items() if d in contexto.opcoes}
    destino, _ = _unico_destino(citados)
    if destino:
        return destino
    if set(contexto.opcoes) == {"upload", "historico"}:
        if re.search(r"\b(?:enviar|envio|novo|nova|mandar|anexar|upload)\b", texto):
            return "upload"
        if re.search(r"\b(?:consultar|ver|anterior|anteriores|antigo|antigos|antiga|publicad\w*)\b", texto):
            return "historico"
    return None


def _confirmar_navegacao(destino, contexto, ev, base, confianca, motivo) -> Interpretacao:
    if destino == contexto.pagina_atual:
        return _resultado(Intencao.PAGINA_ATUAL, confianca, ev, destino=destino,
                          forca_destino=FORTE, motivo="pagina_atual", contextual=True, **base)
    return _resultado(Intencao.NAVEGAR, confianca, ev, destino=destino, forca_destino=FORTE,
                      contextual=True, motivo=motivo, **base)


def _decidir_comando(comandos, oracoes, excluidos, contexto, ev, base) -> Interpretacao:
    if any(o.desconhecidas for o in comandos):
        return _resultado(Intencao.PAGINA_INEXISTENTE, 0.85, ev, motivo="destino_desconhecido", **base)

    alvos: dict = {}
    contextuais = [o for o in comandos if not o.alvos]
    for o in comandos:
        for d, f in o.alvos.items():
            alvos[d] = max(alvos.get(d, 0.0), f)
    # Enumeração após a ordem: "abra o upload, o histórico ou o guia".
    indice = oracoes.index(comandos[-1])
    for o in oracoes[indice + 1:]:
        if o.tipo == "topico":
            for d, f in o.destinos.items():
                alvos[d] = max(alvos.get(d, 0.0), f)
    alvos = {d: f for d, f in alvos.items() if d not in excluidos}

    if alvos:
        fortes = {d: f for d, f in alvos.items() if f >= FORTE}
        disputa = fortes if fortes else alvos
        if len(disputa) > 1:
            opcoes = tuple(disputa)
            ev.append(Evidencia(Intencao.AMBIGUIDADE, 2.0, "multiplos_destinos"))
            return _resultado(Intencao.AMBIGUIDADE, 0.9, ev, opcoes=opcoes, modo_opcoes="navegar",
                              motivo="multiplos_destinos", **base)
        destino, forca = next(iter(disputa.items()))
        contextual = False
    else:
        generico = any(o.lugar_generico for o in comandos)
        destino, forca, contextual = None, 0.0, True
        if any(o.anterior for o in contextuais):
            destino = contexto.destino_anterior
        elif contexto.opcoes:
            return _resultado(Intencao.AMBIGUIDADE, 0.8, ev, opcoes=contexto.opcoes,
                              modo_opcoes="navegar", motivo="referencia_com_opcoes", **base)
        elif not generico:
            destino = contexto.ultimo_destino
        if destino in excluidos:
            destino = None
        if not destino:
            return _resultado(Intencao.AMBIGUIDADE, 0.8, ev, motivo="referencia_sem_contexto", **base)
        forca = FORTE

    # Uma pergunta separada na mesma mensagem é respondida antes de sair da página.
    perguntas = [o for o in oracoes if o.tipo == "pergunta" and o not in comandos]
    if perguntas:
        return _resultado(Intencao.ORIENTACAO_NAVEGACAO, 0.7, ev, destino=destino, forca_destino=forca,
                          consulta=" ".join(o.texto for o in perguntas),
                          motivo="comando_com_pergunta", **base)
    if forca < FORTE:
        # Assunto relacionado, mas a página não foi nomeada: oferecer, não abrir.
        return _resultado(Intencao.ORIENTACAO_NAVEGACAO, 0.6, ev, destino=destino, forca_destino=forca,
                          consulta=" ".join(o.texto for o in comandos),
                          motivo="destino_inferido", **base)
    if destino == contexto.pagina_atual:
        return _resultado(Intencao.PAGINA_ATUAL, 0.9, ev, destino=destino, forca_destino=forca,
                          contextual=contextual, motivo="pagina_atual", **base)
    ev.append(Evidencia(Intencao.NAVEGAR, 1.5, "ordem_com_destino_unico"))
    return _resultado(Intencao.NAVEGAR, 0.9 if not contextual else 0.85, ev, destino=destino,
                      forca_destino=forca, contextual=contextual, motivo="ordem_explicita", **base)


def _decidir_informativa(informativas, substantivas, contexto, ev, base, canonico) -> Interpretacao:
    destinos: dict = {}
    for o in substantivas:
        for d, f in o.destinos.items():
            destinos[d] = max(destinos.get(d, 0.0), f)
    consulta = " ".join(o.texto for o in informativas)

    if not destinos and any(_destinos_ambiguos_relatorio(o.texto) for o in informativas):
        return _resultado(Intencao.AMBIGUIDADE, 0.75, ev, opcoes=("upload", "historico"),
                          modo_opcoes="orientar", motivo="relatorio_ambiguo", **base)

    fortes = {d: f for d, f in destinos.items() if f >= FORTE}
    if len(fortes) > 1:
        return _resultado(Intencao.ORIENTACAO_NAVEGACAO, 0.7, ev, opcoes=tuple(fortes),
                          modo_opcoes="orientar", consulta=consulta, motivo="varios_destinos", **base)
    destino, forca = _unico_destino(destinos)
    contextual = False
    if not destino and all(o.contextual for o in informativas if o.tipo == "pergunta") and any(
            o.tipo == "pergunta" for o in informativas):
        if contexto.ultimo_destino:
            destino, forca, contextual = contexto.ultimo_destino, FORTE, True
        else:
            return _resultado(Intencao.AMBIGUIDADE, 0.6, ev, motivo="pergunta_incompleta", **base)

    # Várias perguntas independentes na mesma mensagem: cada uma é respondida.
    partes = []
    for o in informativas:
        if o.tipo != "pergunta" or o.contextual:
            continue
        d, f = _unico_destino(o.destinos)
        partes.append({"consulta": o.texto, "destino": d, "forca": f,
                       "localizacao": bool(_LOCALIZACAO.search(o.texto))})
    detalhes = {"partes": partes[:2]} if len(partes) >= 2 else {}

    intencao = Intencao.DUVIDA_FUNCIONAL
    if destino and any(_LOCALIZACAO.search(o.texto) for o in informativas):
        intencao = Intencao.ORIENTACAO_NAVEGACAO
    elif destino and all(o.tipo in ("topico", "tarefa") for o in informativas):
        intencao = Intencao.ORIENTACAO_NAVEGACAO
    return _resultado(intencao, 0.75 if destino else 0.55, ev, destino=destino, forca_destino=forca,
                      contextual=contextual, consulta=consulta, motivo="pergunta", detalhes=detalhes, **base)


_LOCALIZACAO = re.compile(
    r"\b(?:onde|cade|aonde)\b|\bcomo (?:eu )?(?:acesso|acessar|entro|entrar|chego|chegar|abro|abrir|"
    r"encontro|encontrar|acho|achar|vejo|ver|consulto|consultar|vou)\b|\bexiste\b|\bfica\b"
)
