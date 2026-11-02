"""
NEXO - Faturamento Inteligente | Política de senhas (hardening complementar)
============================================================================
Ponto único de validação de senha nova (primeiro acesso, ativação por link e
redefinição por token). Segue o espírito do NIST SP 800-63B: comprimento
mínimo real, sem regras de composição arbitrárias, bloqueio de senhas
previsíveis (lista local) e, opcionalmente, de senhas já vazadas.

Regras (mensagens em pt-BR, prontas para o flash):
  1. 12 a 128 caracteres (espaços internos são permitidos: frases-senha);
  2. as duas digitações coincidem;
  3. não é uma senha comum (lista local, comparada sem maiúsculas/dígitos
     finais) nem uma sequência/repetição trivial;
  4. não contém o próprio e-mail (parte local) nem partes do nome;
  5. é diferente da senha atual (quando o hash atual é conhecido);
  6. (opcional, NEXO_SENHAS_VAZADAS=1) não aparece em vazamentos conhecidos,
     consultados por k-anonimato (só 5 caracteres hex do SHA-1 saem do
     servidor; a senha nunca). Indisponibilidade do serviço NÃO bloqueia a
     troca: a verificação é registrada como não realizada.

A troca feita nas Configurações da conta soma uma regra de composição (letras
e números), a pedido do dono do produto; ela não reduz o comprimento mínimo.
"""

import hashlib
import logging
import os
import re
import unicodedata
from http.client import HTTPException
from urllib.error import URLError
from urllib.request import Request, urlopen

from werkzeug.security import check_password_hash

logger = logging.getLogger(__name__)

TAMANHO_MINIMO = 12
TAMANHO_MAXIMO = 128

# Senhas mais frequentes em vazamentos públicos + termos do próprio produto.
# Comparação pela "raiz" (minúsculas, sem dígitos/pontuação nas pontas).
SENHAS_COMUNS = frozenset({
    "password", "senha", "123456", "123456789", "1234567890", "12345678", "qwerty",
    "qwertyuiop", "abc123", "111111", "123123", "iloveyou", "admin", "administrador",
    "welcome", "bemvindo", "letmein", "monkey", "dragon", "football", "baseball",
    "princess", "sunshine", "master", "shadow", "superman", "michael", "jennifer",
    "charlie", "daniel", "trustno1", "passw0rd", "p@ssword", "secret", "segredo",
    "mudar123", "mudar", "trocar", "temporaria", "temporario", "provisoria", "brasil",
    "brazil", "flamengo", "corinthians", "palmeiras", "saopaulo", "gremio", "vasco",
    "nexo", "faturamento", "inteligente", "nexofaturamento", "faturamentointeligente",
    "empresa", "cliente", "usuario", "login", "acesso", "portal", "sistema", "teste",
    "default", "changeme", "qazwsx", "zaq12wsx", "asdfgh", "asdfghjkl", "zxcvbnm",
    "1q2w3e4r", "1qaz2wsx", "abcd1234", "senha1234", "password1", "password123",
})


def _raiz(senha: str) -> str:
    texto = unicodedata.normalize("NFKD", senha.casefold())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"^[\W_]+|[\W\d_]+$", "", texto)


def _sequencial(senha: str) -> bool:
    """Repetição de um único caractere ou sequência crescente/decrescente."""
    texto = senha.casefold()
    if len(set(texto)) <= 2:
        return True
    if texto.isdecimal():  # 1234567890 12… / 0987654321 09… (sequências cíclicas)
        passos = {(int(b) - int(a)) % 10 for a, b in zip(texto, texto[1:])}
        return passos in ({1}, {9})
    passos = {ord(b) - ord(a) for a, b in zip(texto, texto[1:])}
    return passos in ({1}, {-1})


def _partes_pessoais(email, nome):
    partes = set()
    if email and "@" in email:
        local = email.split("@", 1)[0].casefold()
        partes.add(local)
        partes.update(p for p in re.split(r"[._\-+]", local) if len(p) >= 4)
    if nome:
        partes.update(p for p in re.split(r"\s+", nome.casefold()) if len(p) >= 4)
    return {p for p in partes if len(p) >= 4}


def senha_vazada(senha: str, *, timeout: float = 3.0):
    """True/False conforme o serviço de senhas vazadas; None quando a
    verificação não pôde ser feita (desligada, sem rede ou erro)."""
    if os.environ.get("NEXO_SENHAS_VAZADAS", "0") != "1":
        return None
    # SHA-1 é exigência do protocolo k-anonimato do serviço (não é hash de
    # segurança da senha): só os 5 primeiros hex do resumo saem do servidor.
    resumo = hashlib.sha1(senha.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    prefixo, sufixo = resumo[:5], resumo[5:]
    try:
        requisicao = Request(f"https://api.pwnedpasswords.com/range/{prefixo}",
                             headers={"User-Agent": "NEXO-politica-senha", "Add-Padding": "true"})
        with urlopen(requisicao, timeout=timeout) as resposta:  # noqa: S310  # nosec B310: URL https fixa
            corpo = resposta.read(1_000_000).decode("ascii", "replace")
    except (URLError, OSError, ValueError, HTTPException) as erro:
        logger.warning("Verificação de senha vazada indisponível (%s).", type(erro).__name__)
        return None
    for linha in corpo.splitlines():
        hash_sufixo, _, contagem = linha.strip().partition(":")
        if hash_sufixo == sufixo and contagem.strip() not in ("", "0"):
            return True
    return False


def validar_nova_senha(nova: str, confirma: str, *, email=None, nome=None, hash_atual=None,
                       exigir_letras_e_numeros: bool = False):
    """Mensagem de erro (pt-BR) ou None quando a senha é aceitável."""
    if not isinstance(nova, str) or len(nova) < TAMANHO_MINIMO:
        return f"A senha deve ter ao menos {TAMANHO_MINIMO} caracteres (uma frase curta é uma boa escolha)."
    if len(nova) > TAMANHO_MAXIMO:
        return f"A senha deve ter no máximo {TAMANHO_MAXIMO} caracteres."
    if nova != confirma:
        return "As senhas não coincidem."
    if exigir_letras_e_numeros and not (any(c.isalpha() for c in nova) and any(c.isdigit() for c in nova)):
        return "A senha deve ter letras e números."
    if _raiz(nova) in SENHAS_COMUNS or nova.casefold() in SENHAS_COMUNS or _sequencial(nova):
        return "Essa senha é muito comum ou previsível. Escolha outra, de preferência uma frase."
    baixo = nova.casefold()
    if any(parte in baixo for parte in _partes_pessoais(email, nome)):
        return "A senha não pode conter seu nome ou seu e-mail."
    if hash_atual and check_password_hash(hash_atual, nova):
        return "A nova senha deve ser diferente da senha atual."
    if senha_vazada(nova):
        return "Essa senha apareceu em vazamentos de dados conhecidos. Escolha outra."
    return None
