"""Léxico do NexoBot: abreviações, vocabulário do domínio e sinônimos.

Todos os termos estão normalizados: minúsculos e sem acentos. As listas são
dados estáticos; nenhuma regra de decisão vive aqui. Termos novos devem ser
acrescentados com um teste que demonstre o ganho (ver tests/test_nexobot_*).
"""

# ---------------------------------------------------------------------------
# Abreviações e grafias de chat → forma canônica (pode gerar mais de um token).
# ---------------------------------------------------------------------------
ABREVIACOES = {
    "vc": "voce", "vcs": "voces",
    "q": "que", "oq": "o que", "oque": "o que", "pq": "por que", "porq": "por que",
    "tb": "tambem", "tbm": "tambem", "tmb": "tambem", "tbem": "tambem",
    "n": "nao", "nn": "nao", "naum": "nao", "nau": "nao", "ss": "sim",
    "msg": "mensagem", "msgs": "mensagens",
    "obg": "obrigado", "obgd": "obrigado", "obgda": "obrigada", "brigado": "obrigado",
    "brigada": "obrigada", "vlw": "valeu", "vleu": "valeu", "tmj": "valeu",
    "blz": "ok", "beleza": "ok", "okay": "ok", "okey": "ok", "oks": "ok",
    "td": "tudo", "hj": "hoje", "cmg": "comigo",
    "qdo": "quando", "qnd": "quando", "qndo": "quando", "qto": "quanto", "qnto": "quanto",
    "dps": "depois", "agr": "agora", "mto": "muito", "mt": "muito",
    "pfv": "por favor", "pf": "por favor", "pfvr": "por favor", "pls": "por favor",
    "plz": "por favor", "porfavor": "por favor",
    "kd": "cade", "ond": "onde", "aonde": "onde",
    "ta": "esta", "to": "estou", "tou": "estou", "tava": "estava", "tamo": "estamos",
    "pra": "para", "pro": "para o", "pros": "para os", "pras": "para as", "prum": "para um",
    "praquela": "para aquela", "praquele": "para aquele",
    "dash": "dashboard", "hist": "historico", "config": "configuracoes",
    "info": "informacao", "infos": "informacoes",
    "upar": "enviar", "upei": "enviei", "upa": "envia", "upe": "envie",
    "uploadar": "enviar", "subir": "enviar", "suba": "envie", "sobe": "envia",
    "eae": "e ai", "eai": "e ai", "iae": "e ai",
    "hello": "ola", "hi": "oi", "hey": "oi", "salve": "oi",
    "xau": "tchau", "flw": "falou", "falow": "falou",
    "help": "ajuda", "socorro": "ajuda", "logar": "entrar",
}

# Risadas e interjeições sem conteúdo semântico (descartadas da análise).
RISADAS = ("kk", "haha", "hehe", "rsrs", "huehue", "kkk")

# ---------------------------------------------------------------------------
# Palavras de uso corrente que NUNCA devem ser "corrigidas" para um termo do
# domínio. Sem esta lista, "obra" viraria "abra" e "papel" viraria "painel".
# ---------------------------------------------------------------------------
PALAVRAS_COMUNS = set("""
a o as os um uma uns umas de da do das dos em na no nas nos ao aos para por pelo pela
com sem sob sobre entre ate apos desde contra perante e ou mas porem entao logo pois
que se como quando onde quanto qual quais quem cujo cuja porque por isso isto aquilo
eu tu ele ela nos vos eles elas me te se lhe nos vos lhes mim ti si comigo contigo
meu minha meus minhas teu tua seu sua seus suas nosso nossa nossos nossas dele dela
este esta estes estas esse essa esses essas aquele aquela aqueles aquelas
nesse nessa neste nesta naquele naquela daquele daquela deste desta desse dessa
la ali aqui ai ca la aqui acola
ser sou es eh era foi fui sao somos estar estou esta estao estava estive estamos
ter tenho tem tinha tive temos tem haver ha havia houve fazer faco faz fiz fez
ir vou vai vamos foi fomos ia poder posso pode podem pude podia poderia
querer quero quer queria quis quisesse dar dou deu dava dizer digo diz disse
ver vejo ve vi viu vendo ler leio leu saber sei sabe sabia achar acho achei
precisar preciso precisa precisava gostar gosto gostaria gostei desejar desejo
conseguir consigo consegue consegui conseguiu conseguiria tentar tento tentei
olhar olho olhei usar uso usei usa pegar pego peguei deixar deixo deixei
muito pouco mais menos tudo nada algo alguem ninguem todo toda todos todas cada
outro outra outros outras mesmo mesma tal tanto tanta bem mal sim nao talvez
agora hoje ontem amanha ja ainda sempre nunca jamais depois antes cedo tarde
dia dias mes meses ano anos semana semanas hora horas minuto minutos vez vezes
coisa coisas jeito forma modo parte lugar local area tela pagina aba secao menu
obra papel porta parede casa carro mesa livro filme musica jogo festa bolo
historia historias poema poesia piada texto carta codigo programa receita
capital pais cidade estado governo presidente politica eleicao futebol time
tempo chuva sol frio calor clima previsao noticia noticias horoscopo
acao acoes bolsa investir investimento investimentos dolar euro bitcoin cripto
amor saude remedio medico doenca dieta comida viagem praia
bom boa otimo otima legal certo errado novo nova velho velha grande pequeno
primeiro primeira segundo segunda terceiro terceira ultimo ultima proximo proxima
anterior anteriores antigo antiga antigos antigas passado passada passados passadas
obrigado obrigada valeu ola oi tchau beleza ok claro pronto isso exato
favor gentileza desculpa desculpe perdao licenca
empresa empresas cliente clientes usuario usuarios equipe pessoa pessoas
valor valores numero numeros total dado dados resultado resultados
expressao impressao sessao missao comissao permissao admissao profissao opiniao regiao
versao conversao conexao reuniao questao gestao sugestao nocao funcao posicao
mensagem palavra frase exemplo comando pergunta resposta conversa assunto
historiador historica historicas historicos guiar guiado manuais
comprar compro comprei comprou compramos vender vendo vendi vendeu vendemos vendido vendida
enviado enviada recebido recebida processada aberto aberta abertos abertas fechado fechada
consultar consulto consulte consulta consultei ajudar ajude ajudo ajudou ajudaria
acompanhar acompanho acompanhe visualizar visualizo verificar verifico checar conferir confiro
encontrar encontro localizar localizo entender entendo funcionar funcionando
mostrar mostrou mostrando abrindo abriu acessei acessou entrei entrou cliquei clicou
coloco coloquei colocou ponho pus boto botei subo subi insiro inseri carrego carreguei
mando mandei mandou envio enviou anexo anexei anexou importo importei submeto submeti
janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro
cadastro cadastros processo processos tema temas porte portas linha linhas extra extras
vinda vindas curta curto combo tocar live serao deseja desejam gera gerado gerada editor
exigir podera humana sistemas existem conseguem enviamos enviam mostram acessam abrem
chamada chamadas concluido concluidos geral gerais
excluo apago deleto removo publico processo altero edito modifico mudo troco cancelo atualizo
crio gero envio mando anexo baixo corrijo ajusto reenvio redefino restauro desativo ativo
""".split())

# ---------------------------------------------------------------------------
# Vocabulário-alvo da correção ortográfica: termos cujo erro de digitação
# muda a interpretação (destinos, verbos de comando, palavras interrogativas).
# ---------------------------------------------------------------------------
VOCABULARIO_DOMINIO = set("""
dashboard painel visao geral inicial principal inicio home indicador indicadores
grafico graficos curva abc semaforo semaforos faturamento pressao estoque devolutiva devolutivas
upload uploads envio envios enviar envia envie enviei enviado enviados enviada enviadas
relatorio relatorios planilha planilhas arquivo arquivos anexo anexos anexar anexe
compras vendas compra venda documento documentos importar
historico analise analises anteriores antigas antigos passadas passados publicadas publicada
publicados publicado concluidas concluida
guia manual tutorial tutoriais conhecimento perguntas frequentes
suporte chamado chamados ticket tickets atendimento atendente atendentes humano consultor
configuracoes configuracao preferencias ajustes perfil
senha senhas login acesso acessos entrar recuperar recupero redefinir alterar trocar
esqueci esqueceu exportar exportacao sistema pdv erp portal sininho notificacao notificacoes
email emails mensagem mensagens processado processamento processada validacao homologacao
abra abre abrir acesse acessa acessar leve leva levar direcione direciona direcionar
redirecione redireciona redirecionar mostre mostra mostrar exiba exibe exibir navegue navegar
coloque coloca colocar mande manda mandar volte volta voltar entre entra
quero queria gostaria preciso precisava desejo consegue conseguiria poderia
onde como quando qual quais quanto porque explique explica ensine ensina existe
exclua excluir apague apagar delete deletar remova remover publique publicar execute executar
processe processar altere alterar edite editar modifique modificar cancele cancelar
atualize atualizar cadastre cadastrar crie criar
obrigado obrigada ajuda duvida duvidas problema problemas erro erros
pagina paginas parte tela area secao lugar local aba menu naquela naquele aquela aquele
nessa nesse essa esse minhas minha meus sobre vamos levar mostrar acessar abrir inicial
""".split())

# Verbos de operações que o NexoBot NUNCA executa (formas usadas em pedidos).
VERBOS_OPERACAO = (
    "exclua|exclui|excluir|apague|apaga|apagar|delete|deleta|deletar|remova|remove|remover|"
    "limpe|limpa|limpar|publique|publica|publicar|despublique|despublica|despublicar|dispare|"
    "dispara|disparar|execute|executa|executar|rode|roda|rodar|processe|processa|processar|"
    "reprocesse|reprocessa|reprocessar|homologue|homologa|homologar|aprove|aprova|aprovar|"
    "altere|altera|alterar|edite|edita|editar|modifique|modifica|modificar|mude|muda|mudar|"
    "troque|troca|trocar|cancele|cancela|cancelar|atualize|atualiza|atualizar|cadastre|"
    "cadastra|cadastrar|crie|cria|criar|gere|gera|gerar|envie|envia|enviar|mande|manda|mandar|"
    "anexe|anexa|anexar|resete|reseta|resetar|redefina|redefine|redefinir|bloqueie|bloqueia|"
    "bloquear|desbloqueie|desbloquear|ative|ativa|ativar|desative|desativa|desativar|restaure|"
    "restaura|restaurar|baixe|baixa|baixar|transfira|transferir|pague|pagar|faca|faz|importe|"
    "importa|importar|zere|zera|zerar|recalcule|recalcula|recalcular|corrija|corrige|corrigir|"
    "ajuste|ajusta|ajustar|reenvie|reenvia|reenviar|aprove|responda|feche|fecha|fechar|"
    "encerre|encerra|encerrar|reabra|reabrir"
)

# Formas do mesmo verbo: um empate entre elas é resolvido pelo infinitivo
# ("levra" está a 1 erro de "levar" e de "leva": ambos significam levar).
LEMAS = {
    "abrir": ("abra", "abre", "abrir", "abri"),
    "levar": ("leve", "leva", "levar"),
    "acessar": ("acesse", "acessa", "acessar"),
    "mostrar": ("mostre", "mostra", "mostrar"),
    "direcionar": ("direcione", "direciona", "direcionar"),
    "redirecionar": ("redirecione", "redireciona", "redirecionar"),
    "colocar": ("coloque", "coloca", "colocar"),
    "mandar": ("mande", "manda", "mandar"),
    "voltar": ("volte", "volta", "voltar"),
    "navegar": ("navegue", "navega", "navegar"),
    "enviar": ("envie", "envia", "enviar", "enviei"),
    "exibir": ("exiba", "exibe", "exibir"),
}

# ---------------------------------------------------------------------------
# Recuperação de conhecimento (Guia).
# ---------------------------------------------------------------------------
# Palavras funcionais: não carregam assunto e não pontuam na busca.
STOPWORDS = set("""
a o as os um uma uns umas de da do das dos em na no nas nos ao aos para por pelo pela
com sem sobre e ou mas que se como quando onde qual quais quem porque eu tu ele ela
voce voces me mim te se lhe nos meu minha meus minhas seu sua seus suas
este esta estes estas esse essa esses essas isso isto aquele aquela aquilo
ser sou eh era foi sao estar estou esta estao ter tenho tem tinha ha fazer faco faz
ir vou vai poder posso pode podem querer quero queria gostaria preciso precisa
consigo consegue conseguir saber sei sabe ver vejo ja ainda so muito mais menos
aqui la ali ai entao tambem nao sim por favor obrigado obrigada ola oi
nexobot bot assistente o que algum alguma alguns algumas todo toda todos todas
sobre fazer dizer diga diz explique explica ensine ensina duvida
""".split())

# Termos genéricos do portal: casam com quase todo tópico e, sozinhos,
# não comprovam relação com um assunto (recebem peso reduzido).
GENERICOS = set("""
portal nexo sistema pagina tela area aba secao menu parte site plataforma usar uso
funciona funcionamento acessar acesso abrir clique clicar botao opcao passo
geral assunto assuntos questao informacao informacoes coisa coisas pergunta ajuda problema
""".split())

# Grupos de sinônimos usados para expandir a consulta. Cada grupo representa
# um conceito; a expansão recebe peso menor que o termo digitado.
GRUPOS_SINONIMOS = (
    {"upload", "enviar", "envio", "envia", "envie", "enviei", "mandar", "mando", "anexar", "anexo",
     "subir", "importar", "carregar", "colocar", "coloco"},
    {"relatorio", "relatorios", "planilha", "planilhas", "arquivo", "arquivos", "documento",
     "documentos", "xlsx", "csv", "excel"},
    {"senha", "senhas", "credencial", "credenciais"},
    {"recuperar", "recupero", "esqueci", "esqueceu", "redefinir", "redefinicao", "resetar",
     "trocar", "troco", "alterar", "altero", "mudar", "mudo", "nova"},
    {"login", "entrar", "logar", "acessar", "acesso", "logon", "autenticar"},
    {"exportar", "exporto", "exportacao", "extrair", "gerar", "tirar", "baixar"},
    {"pdv", "erp", "caixa", "sistema"},
    {"historico", "anteriores", "antigas", "antigos", "passadas", "anterior"},
    {"analise", "analises", "devolutiva", "devolutivas", "relatorio"},
    {"chamado", "chamados", "ticket", "tickets", "suporte", "atendimento"},
    {"publicada", "publicado", "publicacao", "disponivel", "pronta", "concluida", "liberada"},
    {"processado", "processada", "processamento", "processar", "validacao", "homologacao",
     "validado", "status"},
    {"indicador", "indicadores", "kpi", "kpis", "metrica", "metricas"},
    {"notificacao", "notificacoes", "aviso", "avisos", "sininho", "alerta", "alertas"},
    {"dashboard", "painel"},
    {"erro", "errada", "errado", "incorreto", "incorreta", "equivocado", "substituir", "trocar"},
)
