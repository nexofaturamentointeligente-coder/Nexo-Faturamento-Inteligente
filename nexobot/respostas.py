"""Redação das respostas locais, por intenção, nível de profundidade e confiança.

Toda resposta é uma `Montagem`: o texto puro (contrato antigo, `resposta`),
os blocos estruturados que o widget renderiza com componentes DOM seguros
(`blocos`) e as sugestões de próximas perguntas (`sugestoes`). Nenhum bloco
contém HTML: títulos, parágrafos, passos e avisos são apenas texto.

As variações evitam repetir sempre a mesma frase, mas são escolhidas de forma
determinística (hash da mensagem normalizada): a mesma pergunta recebe a mesma
resposta, o que mantém testes e diagnóstico reproduzíveis. Nenhum texto
inventa valores, prazos ou funcionalidades: os fatos vêm do catálogo
(`navegacao.py`), do Guia ou da base de conhecimento (`saber/`).
"""

from dataclasses import dataclass, field
import hashlib
import re

from . import saber
from .navegacao import DESTINOS, ROTULOS, lista_rotulos

MAXIMO_SUGESTOES = 4
TIPOS_BLOCO = ("cabecalho", "texto", "passos", "secao", "exemplo", "aviso")


@dataclass
class Montagem:
    texto: str
    blocos: list = field(default_factory=list)
    sugestoes: list = field(default_factory=list)


def variante(opcoes, semente: str) -> str:
    indice = int(hashlib.sha256(semente.encode("utf-8")).hexdigest()[:8], 16) % len(opcoes)
    return opcoes[indice]


PAGINAS = lista_rotulos()


# --- Blocos ----------------------------------------------------------------
def b_cabecalho(texto: str) -> dict:
    return {"tipo": "cabecalho", "texto": texto}


def b_texto(texto: str) -> dict:
    return {"tipo": "texto", "texto": texto}


def b_passos(itens, titulo: str = "Passo a passo") -> dict:
    return {"tipo": "passos", "titulo": titulo, "itens": list(itens)}


def b_secao(titulo: str, texto: str) -> dict:
    return {"tipo": "secao", "titulo": titulo, "texto": texto}


_PREFIXO_EXEMPLO = re.compile(r"^\s*exemplo\s+hipot[ée]tico\s*[:\-–—]\s*", re.IGNORECASE)


def b_exemplo(texto: str) -> dict:
    """Bloco de exemplo; o rótulo já diz que é hipotético, então o prefixo do texto é removido."""
    corpo = _PREFIXO_EXEMPLO.sub("", texto or "")
    if corpo and corpo != texto:
        corpo = corpo[0].upper() + corpo[1:]
    return {"tipo": "exemplo", "titulo": "Exemplo (hipotético)", "texto": corpo or texto}


def b_aviso(texto: str) -> dict:
    return {"tipo": "aviso", "texto": texto}


def texto_de(blocos) -> str:
    """Renderização em texto puro (só **negrito** e quebras de linha)."""
    partes = []
    for b in blocos:
        tipo = b["tipo"]
        if tipo == "cabecalho":
            partes.append(f"**{b['texto']}**")
        elif tipo == "texto":
            partes.append(b["texto"])
        elif tipo == "passos":
            itens = "\n".join(f"{i}. {p}" for i, p in enumerate(b["itens"], 1))
            partes.append(f"**{b['titulo']}**\n{itens}" if b.get("titulo") else itens)
        elif tipo == "secao":
            partes.append(f"**{b['titulo']}**\n{b['texto']}")
        elif tipo == "exemplo":
            partes.append(f"**{b['titulo']}:** {b['texto']}")
        elif tipo == "aviso":
            partes.append(f"Atenção: {b['texto']}")
    return "\n\n".join(p for p in partes if p)


def montar(blocos, sugestoes=()) -> Montagem:
    return Montagem(texto_de(blocos), list(blocos), list(sugestoes)[:MAXIMO_SUGESTOES])


def simples(texto: str, sugestoes=()) -> Montagem:
    return Montagem(texto, [b_texto(texto)], list(sugestoes)[:MAXIMO_SUGESTOES])


# --- Sugestões (chips) -----------------------------------------------------
def sugestao(rotulo: str, mensagem: str | None = None) -> dict:
    return {"rotulo": rotulo[:60], "mensagem": (mensagem or rotulo)[:200]}


def _rotulo_curto(registro) -> str:
    """Rótulo do chip a partir do título do registro (sem o ponto de interrogação)."""
    titulo = registro.titulo.strip()
    if len(titulo) <= 48:
        return titulo
    return titulo[:46].rstrip(" ,;:") + "…"


def sugestoes_relacionadas(registro, excluir=(), limite=MAXIMO_SUGESTOES) -> list:
    itens = []
    for rid in registro.relacionados:
        alvo = saber.por_id(rid)
        if alvo is None or rid in excluir or alvo.id == registro.id:
            continue
        itens.append(sugestao(_rotulo_curto(alvo), alvo.titulo))
        if len(itens) >= limite:
            break
    return itens


SUGESTOES_INICIAIS = [sugestao(rotulo, saber.POR_ID[rid].titulo if rotulo.endswith("?") else rotulo)
                      for rotulo, rid in saber.SUGESTOES_INICIAIS]


# --- Conhecimento em níveis ----------------------------------------------
def partes_do_nivel(registro, nivel: int) -> frozenset:
    """Partes do registro apresentadas em cada nível (acumulativas por conteúdo, não por número).

    1: resumo · 2: resumo + explicação · 3: resumo + passos (ou explicação, sem passos)
    4: tudo (resumo, explicação, passos, detalhes, exemplo).
    """
    if nivel <= 0:
        return frozenset()
    partes = {"resumo"}
    if nivel == 2 or (nivel == 3 and not registro.passos) or nivel >= 4:
        if registro.explicacao:
            partes.add("explicacao")
    if nivel >= 3 and registro.passos:
        partes.add("passos")
    if nivel >= 4:
        if registro.detalhes:
            partes.add("detalhes")
        if registro.exemplo:
            partes.add("exemplo")
    return frozenset(partes)


def conhecimento(registro, nivel: int, ja_mostrado: int = 0, modo: str | None = None,
                 compacto: bool = False) -> Montagem:
    """Monta a resposta de um registro no nível pedido.

    `ja_mostrado` é o nível já apresentado na conversa: as partes já vistas são
    omitidas (quem pede "explique melhor" recebe só o que é novo).
    `modo`: "exemplo" (só o exemplo), "simplificar" (resumo + exemplo),
    "resumo" (só o resumo) ou None.
    `compacto`: título + resumo (usado quando há mais de uma pergunta).
    """
    nivel = max(1, min(4, int(nivel)))
    blocos, chips = [], []
    tem_mais = registro.nivel_maximo() > max(nivel, ja_mostrado)

    if modo == "exemplo":
        if registro.exemplo:
            blocos.append(b_exemplo(registro.exemplo))
        else:
            blocos.append(b_texto("Não tenho um exemplo pronto para esse assunto, mas posso mostrar o "
                                  "passo a passo ou explicar com mais detalhes."
                                  if registro.passos else
                                  "Não tenho um exemplo pronto para esse assunto. Posso explicar com "
                                  "outras palavras ou seguir para um assunto relacionado."))
    elif modo == "resumo":
        blocos.append(b_texto(registro.resumo))
    elif modo == "simplificar":
        blocos.append(b_texto("Vou explicar de outra forma."))
        blocos.append(b_texto(registro.resumo))
        if registro.exemplo:
            blocos.append(b_exemplo(registro.exemplo))
    elif compacto:
        blocos.append(b_cabecalho(registro.titulo))
        blocos.append(b_texto(registro.resumo))
    else:
        novas = partes_do_nivel(registro, nivel) - partes_do_nivel(registro, ja_mostrado)
        if "resumo" in novas and nivel >= 2:
            blocos.append(b_cabecalho(registro.titulo))
        if "resumo" in novas:
            blocos.append(b_texto(registro.resumo))
        if "explicacao" in novas:
            for paragrafo in registro.explicacao.split("\n\n"):
                blocos.append(b_texto(paragrafo))
        if "passos" in novas:
            blocos.append(b_passos(registro.passos))
        if "detalhes" in novas:
            blocos.append(b_secao("Em detalhe", registro.detalhes))
        if "exemplo" in novas:
            blocos.append(b_exemplo(registro.exemplo))
        if not blocos:
            blocos.append(b_texto("Já mostrei tudo o que tenho sobre esse assunto. Posso seguir para um "
                                  "assunto relacionado ou abrir a página correspondente."))
        elif nivel == 3 and ja_mostrado == 0 and registro.tipo in ("visao_geral", "roteiro") and registro.relacionados:
            blocos.append(b_texto("Posso detalhar qualquer etapa: toque em uma sugestão abaixo ou pergunte "
                                  "com as suas palavras."))
    if registro.aviso and modo != "exemplo":
        blocos.append(b_aviso(registro.aviso))

    mostrado = max(nivel, ja_mostrado) if modo is None and not compacto else max(ja_mostrado, 1)
    vistas = partes_do_nivel(registro, mostrado) if modo is None and not compacto else partes_do_nivel(registro, 1)
    if registro.passos and "passos" not in vistas and modo != "exemplo":
        chips.append(sugestao("Passo a passo", "Me mostra o passo a passo"))
    if tem_mais and partes_do_nivel(registro, 4) - vistas - {"exemplo"}:
        chips.append(sugestao("Explique melhor", "Explique melhor"))
    if registro.exemplo and modo != "exemplo" and mostrado < 4:
        chips.append(sugestao("Me dê um exemplo", "Me dê um exemplo"))
    if registro.proximo and saber.por_id(registro.proximo):
        chips.append(sugestao("E depois?", "E depois?"))
    chips.extend(sugestoes_relacionadas(registro, limite=MAXIMO_SUGESTOES))
    return montar(blocos, chips)


def comparacao(pares) -> Montagem:
    """Compara registros citados numa pergunta: uma seção curta por termo."""
    nomes = [termo for termo, _ in pares]
    titulo = "Diferença entre " + (", ".join(nomes[:-1]) + " e " + nomes[-1] if len(nomes) > 1 else nomes[0])
    blocos = [b_cabecalho(titulo[0].upper() + titulo[1:])]
    avisos, chips = [], []
    for termo, registro in pares:
        blocos.append(b_secao(termo[0].upper() + termo[1:], registro.resumo))
        if registro.aviso and registro.aviso not in avisos:
            avisos.append(registro.aviso)
        chips.append(sugestao(_rotulo_curto(registro), registro.titulo))
    for aviso in avisos:
        blocos.append(b_aviso(aviso))
    return montar(blocos, chips)


def varias(montagens) -> Montagem:
    blocos, chips = [], []
    for m in montagens:
        blocos.extend(m.blocos)
        for chip in m.sugestoes:
            if chip not in chips:
                chips.append(chip)
    return montar(blocos, chips)


def com_prefixo(montagem: Montagem, prefixo: str) -> Montagem:
    """Antepõe uma frase (ex.: "Olá! ") sem perder a estrutura."""
    blocos = list(montagem.blocos)
    if blocos and blocos[0]["tipo"] == "texto":
        blocos[0] = b_texto(prefixo + blocos[0]["texto"])
    else:
        blocos.insert(0, b_texto(prefixo.strip()))
    return Montagem(prefixo + montagem.texto, blocos, montagem.sugestoes)


# --- Saudação e ajuda (curtas, curadas; nunca listam categorias do banco) -----
def saudacao(semente: str) -> Montagem:
    abertura = variante(("Olá! Sou o NexoBot, seu assistente do NEXO.",
                         "Oi! Aqui é o NexoBot, assistente do NEXO."), semente)
    texto = (f"{abertura} Posso explicar como a plataforma funciona, orientar sobre o envio de relatórios "
             "e ajudar você a encontrar as funcionalidades do portal. Sobre o que você quer saber?")
    return simples(texto, SUGESTOES_INICIAIS)


def ajuda_geral() -> Montagem:
    texto = (
        "Posso ajudar de três formas: explicar como o NEXO funciona (análises, envio de relatórios, "
        "indicadores, Curva ABC, devolutivas, suporte); orientar onde fica cada funcionalidade; e abrir as "
        f"páginas do portal ({PAGINAS}) quando você pedir, por exemplo “abra o histórico”.\n"
        "Não executo envios, exclusões nem alterações, e não tenho acesso aos valores das suas análises por "
        "esta conversa."
    )
    return simples(texto, SUGESTOES_INICIAIS)


def despedida(semente: str) -> Montagem:
    return simples(variante((
        "Por nada! Se precisar, é só chamar.",
        "Disponha! Estou por aqui se surgir outra dúvida.",
        "Imagina! Qualquer coisa, é só escrever.",
    ), semente))


VAZIA = "Pode escrever sua dúvida sobre o NEXO ou sobre o portal que eu te ajudo."
SEM_TEXTO = ("Recebi só símbolos ou emojis e não consegui identificar uma pergunta. "
             "Escreva em poucas palavras o que você precisa, por exemplo: “onde envio meus relatórios?”.")
NEGADA = "Tudo bem. Vou manter você na página atual."
RECUSA = "Tudo bem! Se precisar de outra coisa, é só chamar."
CONFIRMACAO_SEM_CONTEXTO = (f"Certo! Me diga o que você precisa ou qual página deseja abrir: {PAGINAS}.")
REFERENCIA_SEM_CONTEXTO = f"Qual página você deseja abrir: {PAGINAS}?"
PERGUNTA_INCOMPLETA = ("Sobre qual parte do NEXO é a sua dúvida? Posso explicar, por exemplo, como funciona a "
                       "plataforma, como enviar relatórios, o que significam os indicadores ou como consultar "
                       "análises anteriores.")
APROFUNDAR_SEM_ASSUNTO = ("Sobre qual assunto você quer que eu aprofunde? Escolha uma sugestão abaixo ou "
                          "pergunte com suas palavras.")
LUGAR_GENERICO = f"Você já está no portal NEXO. Qual página deseja abrir: {PAGINAS}?"
PAGINA_INEXISTENTE = ("Essa página não faz parte das áreas que posso abrir. "
                      f"As disponíveis são: {PAGINAS}. Qual delas você deseja?")
SUPORTE_HUMANO = ("Para falar com a equipe NEXO, abra um chamado na página Suporte: "
                  "lá você descreve a sua dúvida e acompanha as respostas. Quer ir até lá?")
SOBRE_O_BOT = (
    "Quando você me pede diretamente — por exemplo, “abra o histórico” — eu abro a página na hora. "
    "Perguntas como “onde fica o histórico?” recebem uma explicação e um botão, sem sair da página. "
    f"Só abro as páginas do portal do cliente: {PAGINAS}."
)
ACESSO_URL = (f"Por segurança, não abro endereços, links ou caminhos digitados. "
              f"Posso abrir somente as páginas do portal: {PAGINAS}.")
ACESSO_ADMIN = ("A área administrativa é exclusiva da equipe NEXO e não pode ser aberta por aqui. "
                "Posso explicar o que a equipe faz, e, se precisar falar com ela, use a página Suporte.")
INJECAO = ("Não posso mudar as minhas regras de funcionamento. Sigo ajudando com o uso do portal NEXO: "
           "dúvidas sobre a plataforma e acesso às páginas do cliente.")
CODIGO = ("Não executo comandos, códigos ou consultas enviados pela conversa. "
          "Posso orientar sobre o uso do portal e abrir as páginas do cliente.")
OUTRA_EMPRESA = ("Não tenho acesso a dados de empresas por esta conversa — nem da sua, nem de outras. "
                 "Cada cliente vê apenas as análises da própria empresa, dentro do portal.")
DADOS = ("Não tenho acesso aos valores das suas análises por esta conversa. Você pode consultar os "
         "indicadores disponíveis na Visão Geral e, para análises anteriores, no Histórico.")
DADOS_NAO_CALCULADOS = (" O portal também não apresenta lucro, margem ou CMV: os indicadores são "
                        "sinalizações gerenciais estimadas.")
FORA_ESCOPO = (
    "Sou o assistente do portal NEXO e só consigo ajudar com o uso do portal: envio de relatórios, "
    "análises publicadas, indicadores, Guia, Suporte e Configurações. Para outros assuntos, não tenho como responder.",
    "Esse assunto foge do que eu consigo fazer: sou focado no NEXO e no uso do portal. Posso ajudar com "
    "envio de relatórios, histórico de análises, indicadores, senha de acesso ou abrir um chamado.",
)
INVESTIMENTO = ("Não dou recomendações de investimento ou de compra de ações. Posso ajudar com o uso "
                "do portal NEXO, como enviar relatórios ou consultar suas análises.")
DESCONHECIDA = (
    "Não tenho certeza se entendi. Você pode reformular em poucas palavras? Posso explicar como o NEXO "
    f"funciona, como enviar relatórios, o que significam os indicadores, ou abrir {PAGINAS}.",
    "Não encontrei uma orientação para essa mensagem. Tente descrever o que você quer fazer ou entender "
    "— por exemplo, “como funciona a Curva ABC?” ou “onde vejo minhas análises anteriores?”.",
)
SEM_INFORMACAO = ("Não encontrei uma orientação específica para isso. Você pode reformular com outras "
                  "palavras, consultar o Guia ou abrir um chamado para a equipe NEXO.")
PARTE_SEM_INFORMACAO = ("Sobre a outra dúvida, não encontrei uma orientação específica. "
                        "Você pode consultar o Guia ou abrir um chamado para a equipe NEXO.")
GUIA_INDISPONIVEL = ("No momento não consegui consultar o Guia do portal, mas sigo respondendo com a minha "
                     "base sobre o NEXO. Se for urgente, abra um chamado em Suporte.")
SEM_PAGINA_DO_ASSUNTO = ("Esse assunto não tem uma página específica no portal do cliente. "
                         f"As páginas disponíveis são: {PAGINAS}.")


def navegando(destino: str) -> str:
    return f"Abrindo {ROTULOS[destino]}…"


def pagina_atual(destino: str) -> str:
    return f"Você já está em {ROTULOS[destino]}."


def orientacao(destino: str, semente: str, oferecer: bool = True) -> str:
    texto = DESTINOS[destino].orientacao
    if not oferecer:
        return texto
    convite = variante(("Quer acessar essa seção?", "Posso abrir para você.",
                        "Use o botão abaixo para ir até lá."), semente)
    return f"{texto} {convite}"


def orientacao_varios(destinos) -> str:
    return "\n\n".join(DESTINOS[d].orientacao for d in destinos if d in DESTINOS)


def esclarecer_destinos(opcoes, modo: str | None) -> str:
    nomes = lista_rotulos(opcoes)
    if modo == "navegar":
        return f"Você deseja abrir {nomes}? Escolha uma das páginas."
    return f"Sua dúvida é sobre {nomes}? Escolha uma opção."


def esclarecer_relatorio() -> str:
    return "Você quer enviar um relatório novo ou consultar um relatório anterior?"


def topico(trecho, nivel: str) -> Montagem:
    """Tópico do Guia (CMS): título em destaque e o texto do administrador como dado."""
    blocos = []
    if nivel == "media":
        blocos.append(b_texto("Encontrei esta orientação, que parece ser o que você procura:"))
    blocos.append(b_cabecalho(trecho.pergunta))
    blocos.append(b_texto(trecho.resposta))
    return montar(blocos)


def topicos_ambiguos(trechos) -> Montagem:
    itens = "\n".join(f"• {t.pergunta}" for t in trechos[:3])
    texto = ("Encontrei mais de um assunto parecido no Guia:\n" + itens +
             "\nQual deles você procura? Pode responder com outras palavras.")
    chips = [sugestao(t.pergunta[:60], t.pergunta) for t in trechos[:3]]
    return simples(texto, chips)


def sem_informacao() -> Montagem:
    return simples(SEM_INFORMACAO, SUGESTOES_INICIAIS)


def desconhecida(semente: str) -> Montagem:
    return simples(variante(DESCONHECIDA, semente), SUGESTOES_INICIAIS)


# --- Operações recusadas ----------------------------------------------------
_OPERACOES = (
    (r"^(?:exclu|apag|delet|remov|limp|zer)", "excluir ou apagar dados",
     "Exclusões são tratadas pela equipe NEXO: abra um chamado em Suporte ou, para os dados da empresa, use "
     "“Solicitar Exclusão de Dados” em Configurações.",
     "suporte"),
    (r"^(?:publiq|public|despubliq|despublic)", "publicar ou despublicar análises",
     "A publicação é feita pela equipe NEXO depois da validação, e você é avisado no sininho "
     "quando ela acontece.", None),
    (r"^(?:execut|rod|process|reprocess|homolog|aprov|dispar|recalcul)", "processar arquivos ou executar rotinas",
     "O processamento é feito pela equipe NEXO depois que os dois arquivos são enviados e validados.",
     "upload"),
    (r"^(?:envi|mand|anex|import|reenvi)", "enviar arquivos ou mensagens por você",
     "Você mesmo pode anexar os arquivos de Vendas e Compras em Enviar Relatórios.", "upload"),
    (r"^(?:cri|cadastr|ger)", "criar registros ou chamados por você",
     "Para falar com a equipe NEXO, você pode abrir um chamado na página Suporte.", "suporte"),
    (r"^(?:baix|transfer|pag)", "baixar arquivos ou fazer pagamentos por você",
     "Os PDFs das análises publicadas ficam disponíveis no Histórico.", "historico"),
    (r"^(?:cancel|bloque|desbloque|ativ|desativ|restaur|fech|encerr|reabr|respond)",
     "alterar contas, status ou chamados",
     "Pedidos desse tipo são tratados pela equipe NEXO: abra um chamado em Suporte.", "suporte"),
    (r"^(?:alter|edit|modific|mud|troc|atualiz|corrig|ajust|reset|redefin)", "alterar dados ou configurações",
     "Nome, telefone e e-mail de acesso você altera em Configurações; os demais dados do cadastro são "
     "tratados pela equipe NEXO: abra um chamado em Suporte.", "suporte"),
)
_SENHA = re.compile(r"\bsenhas?\b")
_VERBO_DE_ALTERACAO = re.compile(r"^(?:alter|mud|troc|reset|redefin|atualiz|edit|modific|corrig|ger|cri)")
_EMAIL = re.compile(r"\b(?:email|emails|mensagem|mensagens)\b")
_DADOS_DA_CONTA = re.compile(
    r"\b(?:telefones?|celular|meu nome|nome do responsavel|meu e-?mail|e-?mail de acesso|meu perfil|perfil$|"
    r"meus dados(?! d[eoa]s? (?!conta\b|contato\b|acesso\b|login\b|cadastro\b|usuario\b)"
    r"| n[oa]s? (?!conta\b|portal\b|sistema\b)| em (?!configuracoes\b))|dados pessoais)\b"
)
_OBJETO_DA_CONTA = re.compile(
    r"^(?:o |a |de |meu |minha )?(?:nome|e-?mail)"
    r"(?: de (?:usuario|login|entrada)| d[ao] (?:minha |meu )?(?:conta|login)| do meu usuario| cadastrado)?$"
)
_VERBO_DA_CONTA = re.compile(r"^(?:alter|mud|troc|reset|redefin|atualiz|edit|modific|corrig)")
_TEMA = re.compile(r"\b(?:tema(?: (?:para (?:o )?)?(?:claro|escuro)\b|(?: d[oa] (?:portal|tela|site|sistema))?$)|modo (?:claro|escuro)\b)")
_AVISOS = re.compile(
    r"\b(?:lembretes?|notificacoes|notificacao|(?<!\bde )(?:alertas?|avisos?)(?: d(?:e|os?) (?:chamados?|renovacao)| por email|$))"
)
_VERBO_DE_PREFERENCIA = re.compile(r"^(?:ativ|desativ|alter|mud|troc|edit|modific|ajust)")
_MEUS_DADOS = re.compile(r"\b(?:meus dados|dados da (?:minha )?empresa)\b")
_ANALISE = re.compile(r"\banalises?\b")
_PEDIDO_DE_MUDANCA = re.compile(r"\b(?:alter|mud|troc|atualiz|corrig|edit|modific)\w*\b")
_FECHO_DA_FRASE = re.compile(r"(?: ?(?:[.!?;:,]|por favor|obrigad[oa]))+$")


def pede_alterar_a_conta(texto: str) -> bool:
    return bool(_PEDIDO_DE_MUDANCA.search(texto) and _DADOS_DA_CONTA.search(texto) and not _SENHA.search(texto))


def operacao(verbos, texto: str) -> tuple[str, str | None]:
    """Texto da recusa e página sugerida para a parte que o cliente pode fazer."""
    texto = _FECHO_DA_FRASE.sub("", texto)  # os padrões ancorados no fim não veem pontuação nem cortesia
    descricoes, orientacoes, destino = [], [], None
    for verbo in verbos:
        for padrao, descricao, orientar, sugestao_destino in _OPERACOES:
            if re.match(padrao, verbo):
                if _SENHA.search(texto) and _VERBO_DE_ALTERACAO.match(verbo):
                    descricao = "alterar a sua senha"
                    orientar = ("Para trocar a senha, abra Configurações e informe a senha atual. Se não "
                                "lembra a senha, use “Esqueci minha senha” na tela de login: enviamos um "
                                "link de redefinição válido por 15 minutos.")
                    sugestao_destino = "configuracoes"
                elif _VERBO_DA_CONTA.match(verbo) and (_DADOS_DA_CONTA.search(texto) or any(
                        _OBJETO_DA_CONTA.match(resto) for resto in re.findall(rf"\b{verbo} (.+)$", texto))):
                    descricao = "alterar os dados da sua conta"
                    orientar = "Nome, telefone e e-mail de acesso você altera em Configurações, no menu lateral."
                    sugestao_destino = "configuracoes"
                elif _TEMA.search(texto) and _VERBO_DE_PREFERENCIA.match(verbo):
                    descricao = "mudar o tema"
                    orientar = "O tema claro ou escuro é escolhido no botão de sol ou lua, no topo da tela."
                    sugestao_destino = None
                elif _AVISOS.search(texto) and _VERBO_DE_PREFERENCIA.match(verbo):
                    descricao = "alterar os seus avisos"
                    orientar = ("Os lembretes de renovação e os alertas de chamados você liga ou desliga em "
                                "Configurações, na seção Preferências.")
                    sugestao_destino = "configuracoes"
                elif _MEUS_DADOS.search(texto) and verbo.startswith("baix"):
                    descricao = "baixar os dados por você"
                    orientar = "A cópia dos dados da conta e da empresa você baixa em Configurações, na exportação de dados."
                    sugestao_destino = "configuracoes"
                elif padrao.startswith("^(?:envi") and _EMAIL.search(texto):
                    descricao, orientar, sugestao_destino = "enviar e-mails ou mensagens", "", None
                elif padrao.startswith("^(?:cri") and re.search(r"\b(?:chamados?|tickets?)\b", texto):
                    descricao = "criar chamados por você"
                elif padrao.startswith("^(?:cri") and _ANALISE.search(texto):
                    descricao = "criar análises"
                    orientar = ("A criação de análises é uma ação da equipe NEXO, feita na área "
                                "administrativa; para pedir uma, abra um chamado em Suporte.")
                if descricao not in descricoes:
                    descricoes.append(descricao)
                if orientar and orientar not in orientacoes:
                    orientacoes.append(orientar)
                destino = destino or sugestao_destino
                break
        else:
            if "executar ações no portal" not in descricoes:
                descricoes.append("executar ações no portal")
    lista = " nem ".join(descricoes)
    texto_final = (f"Não posso {lista} — eu apenas oriento e abro páginas do portal, sem alterar dados.")
    if orientacoes:
        texto_final += " " + " ".join(orientacoes)
    return texto_final, destino
