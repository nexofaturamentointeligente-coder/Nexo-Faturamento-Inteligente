"""Camada A — normalização linguística defensiva.

Transforma qualquer valor recebido em um `TextoNormalizado` imutável, sem
lançar exceções para entradas de usuário. Preserva o que as camadas seguintes
precisam: pontuação que separa orações, trechos entre aspas (comandos citados),
marcas de pergunta e a forma "bruta" (casefold, com acentos e símbolos) usada
pelos detectores de URL/código em `seguranca.py`.
"""

from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata

from .lexico import ABREVIACOES, LEMAS, PALAVRAS_COMUNS, RISADAS, VERBOS_OPERACAO, VOCABULARIO_DOMINIO
from .similaridade import distancia, limite_edicao, radical

LIMITE_CARACTERES = 1000
LIMITE_TOKENS = 220
PONTUACAO_FORTE = frozenset(".!?;:")
PONTUACAO = PONTUACAO_FORTE | {","}

# Controles, formatação invisível (zero-width, bidi) e substitutos isolados.
_INVISIVEIS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f­​-‏‪-‮⁠-⁤"
    "⁦-⁩﻿￹-￻\ud800-\udfff]"
)
_ESPACOS = re.compile(r"\s+")
_ASPAS_PAR = {"“": "”", "«": "»", "„": "“", "‘": "’"}
_ASPAS_SIMETRICAS = {'"', "`", "'"}
_TOKEN = re.compile(
    r"(?P<aspa>[\"“”«»„`‘’]|(?<![^\W_])'|'(?![^\W_]))"
    r"|(?P<pont>[.!?;:,¿¡…])"
    r"|(?P<palavra>[^\W_]+(?:['’\-][^\W_]+)*)"
)
_REPETICAO = re.compile(r"([a-z])\1{2,}")
_DOBRADA = re.compile(r"([a-z])\1")


@dataclass(frozen=True)
class TextoNormalizado:
    original: str                 # texto recebido (limitado), para eco seguro/diagnóstico
    bruto: str                    # NFKC + casefold, com acentos e símbolos
    tokens: tuple                 # tokens canônicos (palavras e pontuação)
    citados: frozenset            # índices de tokens entre aspas
    corrigidos: tuple             # pares (digitado, corrigido)
    pergunta: bool                # houve "?" na mensagem
    vazio: bool
    so_simbolos: bool             # nenhuma letra/dígito (ex.: só emojis)
    tem_emoji: bool
    truncado: bool

    @property
    def canonico(self) -> str:
        return " ".join(self.tokens)

    @property
    def palavras(self) -> tuple:
        return tuple(t for t in self.tokens if t not in PONTUACAO)


def sem_acentos(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def _eh_emoji(caractere: str) -> bool:
    cp = ord(caractere)
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
            or 0x1F900 <= cp <= 0x1F9FF or cp in (0x200D, 0xFE0F, 0x2B50, 0x2B55))


_VOCABULARIO = frozenset(VOCABULARIO_DOMINIO)
_VOCABULARIO_ORDENADO = tuple(sorted(_VOCABULARIO))  # ordem estável entre processos
_CONHECIDAS = (_VOCABULARIO | PALAVRAS_COMUNS | frozenset(ABREVIACOES)
               | frozenset(VERBOS_OPERACAO.split("|")))
_LEMA_DE = {forma: lema for lema, formas in LEMAS.items() for forma in formas}


@lru_cache(maxsize=8192)
def corrigir_palavra(palavra: str) -> str:
    """Corrige erros de digitação apenas em direção ao vocabulário do domínio.

    Palavras conhecidas nunca mudam. Palavras curtas (< 4 letras) também não.
    Exige candidato único; empates mantêm a palavra original (sem chute).
    """
    if palavra in _CONHECIDAS or not palavra.isalpha():
        return palavra
    maximo = limite_edicao(palavra)
    if not maximo:
        return palavra
    melhores, melhor_dist = [], maximo + 1
    for candidato in _VOCABULARIO_ORDENADO:
        # Palavras curtas costumam errar no meio, não na primeira letra; e uma
        # correção de dois erros só é aceita se o início da palavra coincidir.
        mesma_inicial = candidato[0] == palavra[0]
        if len(palavra) < 6 and not mesma_inicial:
            continue
        d = distancia(palavra, candidato, maximo if mesma_inicial else 1)
        if d < melhor_dist:
            melhores, melhor_dist = [candidato], d
        elif d == melhor_dist and d <= maximo:
            melhores.append(candidato)
    if not melhores or melhor_dist > maximo:
        return palavra
    # Empate só é resolvido entre flexões da mesma palavra ("chamado"/"chamados",
    # "leva"/"levar"); entre palavras diferentes, mantém o que foi digitado.
    lemas = {_LEMA_DE.get(c) for c in melhores}
    if len(lemas) == 1 and None not in lemas and len(melhores) > 1:
        return lemas.pop()
    if len({radical(c) for c in melhores}) > 1:
        return palavra
    return min(melhores, key=lambda c: (abs(len(c) - len(palavra)), c))


def _reduzir_repeticoes(palavra: str) -> str:
    """'oiiii' → 'oi', 'historicoooo' → 'historico', preservando 'isso'/'carro'."""
    if palavra in _CONHECIDAS or not _DOBRADA.search(palavra):
        return palavra
    duas = _REPETICAO.sub(r"\1\1", palavra)
    if duas in _CONHECIDAS:
        return duas
    uma = _DOBRADA.sub(r"\1", duas)
    if uma in _CONHECIDAS or (uma != duas and corrigir_palavra(uma) != uma):
        return uma
    return duas


def _eh_risada(palavra: str) -> bool:
    return any(palavra.startswith(r) for r in RISADAS) and len(set(palavra)) <= 3


def _preparar(texto: str) -> str:
    """Substituições que dependem do acento ou do hífen, antes de removê-los."""
    texto = re.sub(r"\be[\s\-]?mails?\b", "email", texto)
    texto = re.sub(r"(?<![^\W_])é(?![^\W_])", " eh ", texto)        # verbo 'é' ≠ conjunção 'e'
    texto = re.sub(r"(?<![^\W_])ñ(?![^\W_])", " não ", texto)
    texto = re.sub(r"(?<![^\W_])à(?![^\W_])", " a ", texto)
    return texto


def normalizar(texto) -> TextoNormalizado:
    """Normaliza qualquer entrada. Tipos não textuais viram mensagem vazia."""
    if not isinstance(texto, str):
        texto = ""
    texto = texto.strip()
    truncado = len(texto) > LIMITE_CARACTERES
    texto = texto[:LIMITE_CARACTERES]
    texto = _INVISIVEIS.sub(" ", texto)
    bruto = _ESPACOS.sub(" ", unicodedata.normalize("NFKC", texto).casefold()).strip()
    tem_emoji = any(_eh_emoji(c) for c in bruto)
    so_simbolos = bool(bruto) and not any(c.isalnum() for c in bruto)
    pergunta = "?" in bruto or "¿" in bruto

    tokens: list[str] = []
    citados: set[int] = set()
    corrigidos: list[tuple[str, str]] = []
    abertura = None
    em_aspas = False
    for m in _TOKEN.finditer(_preparar(bruto)):
        if len(tokens) >= LIMITE_TOKENS:
            truncado = True
            break
        if m.group("aspa"):
            aspa = m.group("aspa")
            if not em_aspas:
                em_aspas, abertura = True, aspa
            elif aspa == _ASPAS_PAR.get(abertura, abertura) or (
                    aspa in _ASPAS_SIMETRICAS and abertura in _ASPAS_SIMETRICAS) or aspa in "”»’":
                em_aspas, abertura = False, None
            continue
        if m.group("pont"):
            # Sinais invertidos abrem a frase e não separam orações.
            if m.group("pont") in "¿¡":
                continue
            simbolo = "." if m.group("pont") == "…" else m.group("pont")
            if not tokens or tokens[-1] == simbolo:
                continue
            if tokens[-1] in PONTUACAO and simbolo in PONTUACAO_FORTE:
                # "?!", "!!?", ",." viram um único separador; a interrogação prevalece.
                if simbolo == "?" or tokens[-1] == ",":
                    tokens[-1] = simbolo
                continue
            tokens.append(simbolo)
            continue
        bruta = sem_acentos(m.group("palavra")).replace("'", "").replace("’", "")
        bruta = bruta.replace("-", "")
        if not bruta:
            continue
        palavra = _reduzir_repeticoes(bruta)
        if _eh_risada(palavra):
            continue
        expandidas = ABREVIACOES.get(palavra, palavra).split()
        for parte in expandidas:
            # "Raio X" com espaço é o "Raio-X" (que o hífen já junta em "raiox").
            if parte == "x" and tokens and tokens[-1] == "raio":
                tokens[-1] = "raiox"
                continue
            corrigida = corrigir_palavra(parte)
            if corrigida != parte:
                corrigidos.append((parte, corrigida))
            if em_aspas:
                citados.add(len(tokens))
            tokens.append(corrigida)

    # Aspas sem fechamento não comprovam citação: descarta a marcação.
    if em_aspas:
        citados.clear()
    sem_palavras = all(t in PONTUACAO for t in tokens)
    return TextoNormalizado(
        original=texto, bruto=bruto, tokens=tuple(tokens), citados=frozenset(citados),
        corrigidos=tuple(corrigidos), pergunta=pergunta, vazio=not bruto,
        # Só emojis, pontuação ou risadas: há conteúdo, mas nenhuma palavra.
        so_simbolos=bool(bruto) and (so_simbolos or sem_palavras), tem_emoji=tem_emoji,
        truncado=truncado,
    )
