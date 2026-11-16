"""Validação dos campos que a landing envia (multipart/form-data).

  nome, empresa      obrigatórios, numa linha, até 120 caracteres
  email              obrigatório, até 254 caracteres, guardado em minúsculas
  whatsapp           obrigatório, do Brasil, com DDD; guardado só com DDD e número (10 ou 11 dígitos)
  interesse          1 (Raio-X, com relatórios), 2 (ajuda com os relatórios) ou 3 (só contato)
  termos_aceitos     tem de vir marcado (true, 1, on ou sim); falso ou ausente bloqueia o envio

O WhatsApp segue as regras do envio pela Evolution da aplicação principal, mais estritas no que é ambíguo,
para o número nunca virar o de outra pessoa: com "+" ou "00" só passa o DDI 55 seguido de DDD e número; sem
eles, o 55 do começo não vira DDI (12 ou 13 dígitos são recusados); DDD em uso; fixo de 8 dígitos começando
por 2 a 5 ou celular de 9 começando por 9; nada de ramal (grupo final de 1 a 3 dígitos), letra, dois números
nem o número de exemplo (um só dígito repetido). Caracteres invisíveis de formatação saem, traços
tipográficos contam como hífen e o "0xx" da longa distância conta como o zero de tronco.
"""

import re
import unicodedata

NOME_MAXIMO = 120
EMAIL_MAXIMO = 254
WHATSAPP_MAXIMO = 60
INTERESSES = {"1": "raio_x", "2": "ajuda_relatorios", "3": "so_contato"}
MENSAGEM_WHATSAPP = "Informe um WhatsApp do Brasil com DDD: 10 ou 11 números."
_VERDADEIROS = {"true", "1", "on", "sim"}
# DDDs em uso no Brasil (plano de numeração da Anatel), os mesmos da aplicação principal.
DDDS = frozenset({
    11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38,
    41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68, 69,
    71, 73, 74, 75, 77, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97, 98, 99,
})
_EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_SO_TELEFONE = re.compile(r"\+?[0-9 ().\-]+")
_SEPARADORES = re.compile(r"[ ().\-]+")
_NUMERO_BRASILEIRO = re.compile(r"[1-9][1-9](?:[2-5][0-9]{7}|9[0-9]{8})")
_ZERO_XX = re.compile(r"0 *[xX]{2}")
_MENOS = chr(0x2212)


class DadoInvalido(ValueError):
    """Campo inválido, com a mensagem para quem preencheu e o nome do campo."""

    def __init__(self, mensagem: str, campo: str):
        super().__init__(mensagem)
        self.campo = campo


def uma_linha(valor) -> str:
    """Texto numa linha: sem caracteres de formatação invisíveis, controles viram espaço, espaços colapsados."""
    if not isinstance(valor, str):
        return ""
    limpo = "".join(" " if unicodedata.category(caractere) in ("Cc", "Zl", "Zp") else caractere
                    for caractere in valor if unicodedata.category(caractere) != "Cf")
    return " ".join(limpo.split())


def texto_obrigatorio(valor, campo: str, mensagem: str) -> str:
    texto = uma_linha(valor)
    if not texto:
        raise DadoInvalido(mensagem, campo)
    if len(texto) > NOME_MAXIMO:
        raise DadoInvalido(f"Use até {NOME_MAXIMO} caracteres.", campo)
    return texto


def email(valor) -> str:
    texto = uma_linha(valor).lower()
    if not texto:
        raise DadoInvalido("Informe o e-mail.", "email")
    if len(texto) > EMAIL_MAXIMO or ".." in texto or not _EMAIL.fullmatch(texto):
        raise DadoInvalido("Informe um e-mail válido.", "email")
    return texto


def _telefone_normalizado(valor) -> str:
    """Sem invisíveis, com os traços tipográficos como hífen, o 0xx como zero e espaços colapsados."""
    if not isinstance(valor, str):
        return ""
    texto = "".join("-" if unicodedata.category(c) == "Pd" or c == _MENOS else c
                    for c in valor if unicodedata.category(c) != "Cf")
    return _ZERO_XX.sub("0", " ".join(texto.split()))


def whatsapp(valor) -> str:
    """DDD e número (10 ou 11 dígitos) de um WhatsApp do Brasil, ou DadoInvalido."""
    texto = _telefone_normalizado(valor)
    if not texto:
        raise DadoInvalido("Informe o WhatsApp com DDD.", "whatsapp")
    if len(texto) > WHATSAPP_MAXIMO or not _SO_TELEFONE.fullmatch(texto) or "+" in texto[1:]:
        raise DadoInvalido(MENSAGEM_WHATSAPP, "whatsapp")
    grupos = [grupo for grupo in _SEPARADORES.split(texto.lstrip("+")) if grupo]
    if len(grupos) > 1 and len(grupos[-1]) < 4:
        raise DadoInvalido(MENSAGEM_WHATSAPP, "whatsapp")  # ramal ou sobra
    digitos = "".join(grupos)
    if texto.startswith("+") or digitos.startswith("00"):
        digitos = digitos[2:] if digitos.startswith("00") else digitos
        if not digitos.startswith("55"):
            raise DadoInvalido(MENSAGEM_WHATSAPP, "whatsapp")  # estrangeiro
        nacional = digitos[2:]
        nacional = nacional[1:] if nacional.startswith("0") else nacional
    else:
        nacional = digitos[1:] if digitos.startswith("0") else digitos
    if (not _NUMERO_BRASILEIRO.fullmatch(nacional) or int(nacional[:2]) not in DDDS
            or len(set(nacional[2:])) == 1):
        raise DadoInvalido(MENSAGEM_WHATSAPP, "whatsapp")
    return nacional


def interesse(valor) -> str:
    codigo = INTERESSES.get(uma_linha(valor))
    if codigo is None:
        raise DadoInvalido("Escolha o interesse: 1, 2 ou 3.", "interesse")
    return codigo


def termos_aceitos(valor) -> bool:
    if uma_linha(valor).lower() not in _VERDADEIROS:
        raise DadoInvalido("É preciso aceitar os termos para enviar.", "termos_aceitos")
    return True
