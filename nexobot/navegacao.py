"""Catálogo fechado de destinos do NexoBot e reconhecimento de menções.

O catálogo é a ÚNICA fonte de páginas que o assistente pode abrir. As URLs
vêm de `url_for()` sobre endpoints GET existentes do blueprint do cliente;
nada do texto do usuário, do Guia ou de um modelo de linguagem vira URL.
Acrescentar uma página exige editar este arquivo (rota, termos e orientação)
e revisar suas permissões — tópicos do Guia não criam destinos.
"""

from dataclasses import dataclass
import re

from flask import url_for

FORTE = 1.0
FRACA = 0.5


@dataclass(frozen=True)
class Destino:
    chave: str
    endpoint: str
    rotulo: str
    orientacao: str        # onde fica / para que serve (fatos da interface)
    verbo_acao: str        # "enviar seus relatórios", usado em respostas


DESTINOS = {
    "dashboard": Destino(
        "dashboard", "cliente.dashboard", "Visão Geral",
        "A Visão Geral, primeiro item do menu lateral, mostra a devolutiva mais recente "
        "publicada para a sua empresa, com os indicadores, a Curva ABC e os alertas de risco.",
        "acompanhar os indicadores da sua empresa",
    ),
    "upload": Destino(
        "upload", "cliente.upload", "Enviar Relatórios",
        "Em Enviar Relatórios você escolhe a análise aberta e anexa os arquivos de "
        "Vendas e de Compras (.csv ou .xlsx). A página mostra o status de cada arquivo "
        "e avisa quando não há análise disponível para envio.",
        "enviar os arquivos de compras e vendas",
    ),
    "historico": Destino(
        "historico", "cliente.historico", "Histórico de Análises",
        "O Histórico, no menu lateral, lista as análises já publicadas para a sua "
        "empresa. Cada uma pode ser aberta e, enquanto o contrato permitir, exportada em PDF.",
        "consultar as análises anteriores",
    ),
    "guia": Destino(
        "guia", "cliente.guia", "Guia",
        "O Guia reúne o passo a passo das dúvidas mais comuns sobre o portal, "
        "cadastrado pela equipe NEXO. Ele fica no menu lateral.",
        "ver o passo a passo do portal",
    ),
    "suporte": Destino(
        "suporte", "cliente.tickets", "Suporte",
        "Em Suporte você abre um chamado para a equipe NEXO e acompanha as respostas "
        "dos chamados já abertos.",
        "falar com a equipe NEXO",
    ),
    "configuracoes": Destino(
        "configuracoes", "cliente.configuracoes", "Configurações",
        "Em Configurações, no menu lateral, você altera o nome do responsável, o telefone de contato, a "
        "senha e o e-mail de acesso, exporta os dados da empresa e escolhe os avisos que quer receber.",
        "alterar seus dados de acesso e preferências",
    ),
}

ROTULOS = {chave: d.rotulo for chave, d in DESTINOS.items()}
TIPOS_ACAO = frozenset({"navegar", "sugerir_navegacao"})

_OUTRO_DESTINO = (r"(?:suporte|chamados?|tickets?|historico|uploads?|envio|guia|ajuda|configuracoes|"
                  r"atendimento|administracao|admin\w*|analises|relatorios)")

# Padrões sobre o texto canônico (minúsculo, sem acento, typos corrigidos).
# FORTE: nomeia a página. FRACA: assunto relacionado, que sozinho não basta
# para navegar sem confirmação (vira sugestão com botão).
_PADROES = {
    "dashboard": (
        (FORTE, r"\bdashboards?\b"),
        (FORTE, rf"\bpainel\b(?! (?:de |do |da |dos |das )?{_OUTRO_DESTINO}\b)"),
        (FORTE, r"\bvisao geral\b"),
        (FORTE, r"\b(?:tela|pagina|parte|area|secao|menu) (?:inicial|principal)\b"),
        (FORTE, r"\b(?:tela|pagina) (?:de )?inicio\b|\bhome\b"),
        (FRACA, r"\bindicador(?:es)?\b|\bkpis?\b|\bcurva abc\b|\bgraficos?\b|\bsemaforos?\b"),
        (FRACA, r"\bpressao de estoque\b|\bfaturamento\b"),
        (FRACA, r"\b(?:meus|os|nossos) dados\b"),
    ),
    "upload": (
        (FORTE, r"\buploads?\b"),
        (FORTE, r"\benviar relatorios\b"),
        (FORTE, r"\benvios? (?:de |dos |das )?(?:arquivos?|relatorios?|planilhas?)\b"),
        (FORTE, r"\b(?:pagina|tela|area|aba|secao|parte) (?:de |do |dos |das |de meus |de minhas )?"
                r"(?:envios?|relatorios|arquivos|planilhas)\b"
                r"(?! (?:anterior(?:es)?|antig[oa]s?|passad[oa]s?|publicad[oa]s?|velh[oa]s?|concluid[oa]s?)\b)"),
        (FORTE, r"\b(?:enviar|envio|envia|envie|enviei|mandar|mando|mande|manda|mandei|anexar|anexo|"
                r"anexe|anexa|anexei|importar|importo|importe|carregar|carrego|carregue|colocar|coloco|"
                r"coloca|coloque|coloquei|ponho|boto|subo|subi|submeter|submeto|inserir|insiro|"
                r"jogar|jogo)\b(?: \w+){0,4} (?:arquivos?|planilhas?|relatorios?|compras|vendas|"
                r"documentos?|xlsx|csv)\b"),
        (FRACA, r"\bplanilhas?\b|\barquivos?\b|\bcompras\b|\bvendas\b|\bcsv\b|\bxlsx\b|\banexos?\b"),
    ),
    "historico": (
        (FORTE, r"\bhistoricos?\b"),
        (FORTE, r"\b(?:analises?|relatorios?|devolutivas?|resultados?|entregas?) "
                r"(?:\w+ )?(?:anterior(?:es)?|antig[oa]s?|passad[oa]s?|publicad[oa]s?|velh[oa]s?|"
                r"concluid[oa]s?|de antes|(?:de|dos) meses anteriores)\b"),
        (FRACA, r"\banalises\b|\bdevolutivas?\b|\bpdf\b"),
    ),
    "guia": (
        (FORTE, r"\bguia\b|\bmanual\b|\bbase de conhecimento\b|\btutoria(?:l|is)\b"),
        (FORTE, r"\bperguntas frequentes\b|\bfaq\b|\bduvidas (?:comuns|frequentes)\b"),
        (FRACA, r"\bpasso a passo\b"),
    ),
    "suporte": (
        (FORTE, r"\bsuporte\b|\bchamados?\b|\btickets?\b|\batendimento\b|\batendentes?\b"),
        (FORTE, r"\bfalar com (?:a |o |um |uma |alguem (?:da |do )?)?(?:equipe|pessoa|humano|"
                r"atendente|consultor|alguem)\b|\bfale conosco\b"),
        (FRACA, r"\bhumano\b|\bconsultor(?:es)?\b|\bequipe nexo\b|\bcontato\b"),
    ),
    "configuracoes": (
        (FORTE, r"\bconfiguracoes\b|\bpreferencias\b|\bmeu perfil\b|\b(?:pagina|tela) de perfil\b"),
        (FRACA, r"\bconfiguracao\b|\bajustes\b"),
    ),
}
_COMPILADOS = {
    chave: tuple((forca, re.compile(p)) for forca, p in padroes)
    for chave, padroes in _PADROES.items()
}


@dataclass(frozen=True)
class Mencao:
    destino: str
    forca: float
    inicio: int  # posição (caractere) no texto analisado
    fim: int


def mencoes(texto: str) -> list[Mencao]:
    """Todas as menções a destinos no texto canônico, por posição."""
    achadas = []
    for chave, padroes in _COMPILADOS.items():
        for forca, padrao in padroes:
            for m in padrao.finditer(texto):
                achadas.append(Mencao(chave, forca, m.start(), m.end()))
    achadas.sort(key=lambda m: (m.inicio, -m.forca))
    return achadas


def destinos_em(texto: str, minimo: float = FRACA) -> dict:
    """Destino -> maior força encontrada no texto."""
    resultado = {}
    for m in mencoes(texto):
        if m.forca >= minimo:
            resultado[m.destino] = max(resultado.get(m.destino, 0.0), m.forca)
    return resultado


def validar_destino(destino) -> str | None:
    return destino if isinstance(destino, str) and destino in DESTINOS else None


def urls_destinos() -> dict:
    """Mapa permitido para o widget: identificador -> URL interna do endpoint."""
    return {chave: url_for(d.endpoint) for chave, d in DESTINOS.items()}


def acao_navegacao(tipo: str, destino: str) -> dict:
    """Monta a ação a partir de um identificador do catálogo; qualquer outro valor anula."""
    destino = validar_destino(destino)
    if destino is None or tipo not in TIPOS_ACAO:
        return {"acao": None, "destino": None}
    item = DESTINOS[destino]
    acao = {"acao": tipo, "destino": destino, "url": url_for(item.endpoint)}
    if tipo == "sugerir_navegacao":
        acao["rotulo_botao"] = f"Ir para {item.rotulo}"
    return acao


def opcao_navegacao(destino: str) -> dict | None:
    """Botão de escolha usado em esclarecimentos (sempre exige clique)."""
    destino = validar_destino(destino)
    if destino is None:
        return None
    item = DESTINOS[destino]
    return {"destino": destino, "url": url_for(item.endpoint), "rotulo_botao": item.rotulo}


def lista_rotulos(chaves=None) -> str:
    chaves = list(chaves or DESTINOS)
    rotulos = [DESTINOS[c].rotulo for c in chaves if c in DESTINOS]
    if len(rotulos) <= 1:
        return "".join(rotulos)
    return ", ".join(rotulos[:-1]) + " ou " + rotulos[-1]
