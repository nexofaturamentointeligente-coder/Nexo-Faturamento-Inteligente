"""SEC-010: cabeçalhos de segurança, CSP com nonce e cache das respostas.

Instalado pela factory (app.py). Toda resposta dinâmica recebe:
  - Content-Security-Policy (ou -Report-Only, com NEXO_CSP_REPORT_ONLY=1):
    scripts só da própria origem, das CDNs fixadas com SRI e dos blocos
    inline marcados com o nonce da requisição (csp_nonce nos templates);
    sem frames, sem objetos, formulários só para a própria origem.
  - X-Frame-Options, X-Content-Type-Options, Referrer-Policy,
    Permissions-Policy e Cross-Origin-Opener-Policy.
  - Cache-Control: no-store nas páginas autenticadas, nas rotas de
    autenticação e nas APIs; arquivos estáticos continuam cacheáveis.
  - Strict-Transport-Security em produção, só em resposta HTTPS (atrás do
    proxy com PROXY_FIX_HOPS). includeSubDomains/preload ficam para decisão
    de infraestrutura (domínio próprio), por isso não entram aqui.

Limites conhecidos (documentados no relatório): style-src mantém
'unsafe-inline' porque os templates e o Plotly usam atributos style; a CSP
não substitui a sanitização dos templates, apenas limita o impacto.

A landing (endpoint index) envia o cadastro do lead ao microsserviço do
Raio-X: só nela a origem de MICROSSERVICO_RAIOX_URL entra no connect-src
(o form-action continua 'self': o envio é por fetch, não por <form>).
"""

import secrets
from flask import g, request, session

from config import origem_https

# Fontes de CDN fixadas por CAMINHO exato, não por origem: liberar "https://cdn.jsdelivr.net"
# inteiro deixaria qualquer pacote publicado nesse CDN executar sob a política,
# contornando o nonce. Os quatro recursos são os mesmos fixados por SRI nos templates
# (atualizar a versão exige atualizar aqui, no SRI e na suíte de navegador).
_JSDELIVR = "https://cdn.jsdelivr.net/npm/"
CDN_SCRIPTS = (
    _JSDELIVR + "bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js",
    _JSDELIVR + "plotly.js-dist-min@2.35.2/plotly.min.js",
)
CDN_ESTILOS = (
    _JSDELIVR + "bootstrap@5.3.3/dist/css/bootstrap.min.css",
    _JSDELIVR + "bootstrap-icons@1.11.3/font/bootstrap-icons.min.css",
    "https://fonts.googleapis.com",  # URLs dinâmicas (famílias/pesos): origem
)
CDN_FONTES = (
    "https://fonts.gstatic.com",  # arquivos dinâmicos do Google Fonts: origem
    _JSDELIVR + "bootstrap-icons@1.11.3/font/fonts/",  # prefixo (a CSS acrescenta ?hash)
)
ROTAS_SEM_CACHE = ("/auth/", "/api/", "/notificacoes/", "/midia/")
# Páginas que conversam com o microsserviço do Raio-X (os diálogos de cadastro da landing).
ENDPOINTS_COM_MICROSSERVICO = frozenset({"index"})


def novo_nonce() -> str:
    return secrets.token_urlsafe(18)


def nonce_da_requisicao() -> str:
    nonce = getattr(g, "csp_nonce", None)
    if not nonce:
        nonce = g.csp_nonce = novo_nonce()
    return nonce


def origem_do_microsservico(app) -> str:
    """A origem de MICROSSERVICO_RAIOX_URL, conferida de novo (https://host[:porta]) porque vai para o
    cabeçalho: um valor posto direto no app.config não injeta diretiva nem libera outra origem."""
    return origem_https(app.config.get("MICROSSERVICO_RAIOX_URL"))


def politica_csp(app, nonce: str, conexoes_extras=()) -> str:
    # Sem WebSocket: o sininho consulta a própria origem por fetch (polling).
    conectar = " ".join(["'self'", *conexoes_extras])
    diretivas = [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' {' '.join(CDN_SCRIPTS)}",
        f"style-src 'self' 'unsafe-inline' {' '.join(CDN_ESTILOS)}",
        f"font-src 'self' {' '.join(CDN_FONTES)}",
        "img-src 'self' data: blob:",
        "media-src 'self'",
        f"connect-src {conectar}",
        "worker-src 'self' blob:",
        "frame-src 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "manifest-src 'self'",
    ]
    if app.config.get("PRODUCAO_HTTP"):
        diretivas.append("upgrade-insecure-requests")
    return "; ".join(diretivas)


def _sem_cache() -> bool:
    if request.path.startswith(ROTAS_SEM_CACHE):
        return True
    # Página de usuário autenticado (sem consultar o banco: só o cookie).
    return session.get("_user_id") is not None


def instalar(app) -> None:
    app.config.setdefault("CSP_REPORT_ONLY", False)

    @app.before_request
    def _preparar_nonce():
        if request.endpoint != "static":
            nonce_da_requisicao()

    @app.context_processor
    def _nonce_para_templates():
        return {"csp_nonce": nonce_da_requisicao()}

    @app.after_request
    def _cabecalhos_de_seguranca(resposta):
        if request.endpoint == "static":
            return resposta
        cabecalhos = resposta.headers
        nome_csp = "Content-Security-Policy-Report-Only" if app.config["CSP_REPORT_ONLY"] else "Content-Security-Policy"
        if "Content-Security-Policy" not in cabecalhos:
            extras = ()
            if request.endpoint in ENDPOINTS_COM_MICROSSERVICO:
                origem = origem_do_microsservico(app)
                extras = (origem,) if origem else ()
            cabecalhos[nome_csp] = politica_csp(app, nonce_da_requisicao(), extras)
        cabecalhos.setdefault("X-Content-Type-Options", "nosniff")
        cabecalhos.setdefault("X-Frame-Options", "DENY")
        cabecalhos.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        cabecalhos.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        cabecalhos.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if "Cache-Control" not in cabecalhos and _sem_cache():
            cabecalhos["Cache-Control"] = "no-store"
        if app.config.get("PRODUCAO_HTTP") and request.is_secure:
            cabecalhos.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resposta
