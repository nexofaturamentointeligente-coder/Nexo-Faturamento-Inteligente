"""SEC-005: origem pública canônica para links sensíveis e hosts confiáveis.

Links enviados por e-mail (ativação de conta, recuperação de senha, chamados
resolvidos, devolutiva publicada) nunca são montados a partir do cabeçalho
Host da requisição: um Host forjado numa solicitação de recuperação de senha
faria o link, e com ele o token, apontar para um domínio do atacante. A
origem vem da configuração (PUBLIC_URL), é validada na inicialização e, em
produção, só pode ser https. TRUSTED_HOSTS entrega ao Flask/Werkzeug a lista
de hosts aceitos: um Host inesperado recebe 400 antes de qualquer rota.

O domínio real do sistema não está neste repositório: em produção ele vem de
NEXO_PUBLIC_URL (domínio próprio) ou de RENDER_EXTERNAL_URL (injetada pela
plataforma). Sem nenhuma das duas, a produção não sobe.
"""

import re
from urllib.parse import urlsplit

from flask import current_app

# Host DNS (RFC 1123, minúsculas); IPv4 literal também passa. IPv6 literal e
# nomes internacionalizados não normalizados são recusados: configure o nome
# punycode ou o domínio ASCII.
_HOST_VALIDO = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
ORIGEM_DESENVOLVIMENTO = "http://127.0.0.1:5000"


class OrigemInvalida(ValueError):
    """Valor de origem pública que não pode ser usado para montar links."""


def normalizar_origem(valor, *, exigir_https=False) -> str:
    """Devolve "esquema://host[:porta]" em minúsculas ou levanta OrigemInvalida.

    Só esquema e host (com porta opcional) são aceitos: caminho, query,
    fragmento e credenciais na URL são recusados para que o link resultante
    seja previsível. A mensagem de erro nunca reproduz o valor recebido.
    """
    if not isinstance(valor, str) or not valor.strip():
        raise OrigemInvalida("origem vazia")
    partes = urlsplit(valor.strip())
    esquema = partes.scheme.lower()
    if esquema not in ("http", "https"):
        raise OrigemInvalida("esquema deve ser http ou https")
    if exigir_https and esquema != "https":
        raise OrigemInvalida("produção exige https")
    if partes.username is not None or partes.password is not None:
        raise OrigemInvalida("credenciais na URL não são aceitas")
    if partes.path not in ("", "/") or partes.query or partes.fragment:
        raise OrigemInvalida("informe apenas esquema e host, sem caminho")
    host = (partes.hostname or "").lower()
    if not _HOST_VALIDO.match(host):
        raise OrigemInvalida("host inválido")
    try:
        porta = partes.port
    except ValueError as erro:
        raise OrigemInvalida("porta inválida") from erro
    return f"{esquema}://{host}" if porta is None else f"{esquema}://{host}:{porta}"


def normalizar_host_confiavel(valor) -> str:
    """Entrada de TRUSTED_HOSTS: host exato ou ".dominio" (aceita subdomínios)."""
    if not isinstance(valor, str):
        raise OrigemInvalida("host confiável inválido")
    host = valor.strip().lower()
    sufixo = host.startswith(".")
    if not _HOST_VALIDO.match(host[1:] if sufixo else host):
        raise OrigemInvalida("host confiável inválido")
    return host


def host_da_origem(origem: str) -> str:
    return urlsplit(origem).hostname


def configurar_origem_publica(app, *, producao: bool) -> None:
    """Valida PUBLIC_URL e deriva TRUSTED_HOSTS.

    Chamado pela factory antes de inicializar as extensões. Em produção a
    origem é obrigatória e https; em desenvolvimento, sem configuração, vale
    o endereço padrão do servidor local. Hosts confiáveis só são impostos em
    produção ou quando NEXO_TRUSTED_HOSTS é informada (os testes e o servidor
    de desenvolvimento respondem em qualquer host).
    """
    bruto = (app.config.get("PUBLIC_URL") or "").strip()
    if bruto:
        try:
            origem = normalizar_origem(bruto, exigir_https=producao)
        except OrigemInvalida as erro:
            raise RuntimeError(f"NEXO_PUBLIC_URL inválida ({erro}).") from None
    elif producao:
        raise RuntimeError(
            "NEXO_PUBLIC_URL (ou RENDER_EXTERNAL_URL) com https é obrigatória em produção."
        )
    else:
        origem = ORIGEM_DESENVOLVIMENTO
    app.config["PUBLIC_URL"] = origem

    extras = app.config.get("TRUSTED_HOSTS_EXTRAS") or []
    if not producao and not extras:
        app.config["TRUSTED_HOSTS"] = None
        return
    hosts = [host_da_origem(origem)]
    plataforma = (app.config.get("PUBLIC_URL_PLATAFORMA") or "").strip()
    if plataforma:
        try:
            hosts.append(host_da_origem(normalizar_origem(plataforma)))
        except OrigemInvalida as erro:
            raise RuntimeError(f"RENDER_EXTERNAL_URL inválida ({erro}).") from None
    for extra in extras:
        try:
            hosts.append(normalizar_host_confiavel(extra))
        except OrigemInvalida as erro:
            raise RuntimeError(f"NEXO_TRUSTED_HOSTS inválida ({erro}).") from None
    app.config["TRUSTED_HOSTS"] = list(dict.fromkeys(hosts))


def url_publica(endpoint: str, **values) -> str:
    """URL absoluta de `endpoint` na origem pública canônica.

    Independe do cabeçalho Host, dos cabeçalhos X-Forwarded-* e do contexto
    de requisição: o adaptador de rotas é ligado à origem configurada.
    Substitui url_for(..., _external=True) em todo link que sai por e-mail.
    """
    values.pop("_external", None)
    values.pop("_scheme", None)
    ancora = values.pop("_anchor", None)
    origem = current_app.config["PUBLIC_URL"]
    partes = urlsplit(origem)
    adaptador = current_app.url_map.bind(
        partes.netloc, script_name=current_app.config.get("APPLICATION_ROOT") or "/",
        url_scheme=partes.scheme,
    )
    url = adaptador.build(endpoint, values, force_external=True)
    return f"{url}#{ancora}" if ancora else url
