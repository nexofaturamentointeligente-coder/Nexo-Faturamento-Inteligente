"""
NEXO - Faturamento Inteligente | Horário oficial de Brasília
============================================================
Convenção única dos horários do suporte e do sininho de notificações:

  GRAVAÇÃO       instante absoluto em UTC. As colunas legadas do suporte e
                 do sininho são `timestamp without time zone`; nelas o valor
                 é o relógio UTC sem fuso (models.InstanteUTCSemFuso). Quem
                 converte é o Python, não o TimeZone da sessão do banco.
  EXIBIÇÃO       sempre America/Sao_Paulo, formatada no servidor por este
                 módulo. Jinja e a resposta do polling do sininho usam a mesma função.
  TRANSFERÊNCIA  ISO 8601 com fuso ("2026-10-29T17:34:00+00:00") no atributo
                 datetime de <time> e no payload; o JavaScript só exibe o
                 texto já formatado, sem converter horários.

O fuso do container (TZ), o do navegador e o TimeZone da sessão do PostgreSQL
não mudam nenhum horário exibido. O horário de verão (Brasília o teve até
2019) vem das regras IANA do tzdata, nunca de um deslocamento fixo.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
BRASILIA = ZoneInfo("America/Sao_Paulo")
ROTULO_BRASILIA = "Horário de Brasília"


def agora_utc() -> datetime:
    """Instante atual de um evento do suporte ou do sininho.

    Usa o relógio único do portal (contract_policy.agora, controlado nos
    testes por NEXO_RELOGIO) e o lê de novo a cada chamada: cada chamado,
    mensagem e notificação recebe o seu próprio instante.
    """
    from contract_policy import agora  # import tardio: contract_policy importa models
    return agora(nova_leitura=True)


def instante_utc(instante: datetime) -> datetime:
    """Normaliza para UTC. Valor sem fuso é recusado: não se presume UTC."""
    if not isinstance(instante, datetime) or instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError("Horário sem fuso explícito: não há como saber a que instante se refere.")
    return instante.astimezone(UTC)


def horario_brasilia(instante: datetime | None, formato: str = "completo", *, rotulo: bool = False) -> str:
    """Instante no horário de Brasília; "—" quando não há instante.

    formato: "completo" (29/10/2026 às 14:34), "curto" (29/10 14:34, o do
    sininho) ou "data" (29/10/2026). rotulo=True acrescenta
    "(Horário de Brasília)". O "às" fica fora do strftime: o formato passado a
    ele é só ASCII, e a localidade do servidor não interfere.
    """
    if instante is None:
        return "—"
    local = instante_utc(instante).astimezone(BRASILIA)
    if formato == "completo":
        texto = f"{local:%d/%m/%Y} às {local:%H:%M}"
    elif formato == "curto":
        texto = f"{local:%d/%m %H:%M}"
    elif formato == "data":
        texto = f"{local:%d/%m/%Y}"
    else:
        raise ValueError(f"Formato de horário desconhecido: {formato!r}.")
    return f"{texto} ({ROTULO_BRASILIA})" if rotulo else texto


def iso_utc(instante: datetime | None) -> str:
    """ISO 8601 em UTC com o fuso explícito; "" quando não há instante."""
    if instante is None:
        return ""
    return instante_utc(instante).isoformat(timespec="seconds")
