"""
NEXO - Faturamento Inteligente | Configuração da aplicação
==========================================================
Configuração centralizada lida do .env via python-dotenv.
Por que centralizar: facilita troca de ambiente (dev/prod) e teste,
e evita espalhar os.environ.get() pelo código.
"""

import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from dotenv import load_dotenv
from dataclasses import asdict
from etl_limits import limits_from_environment

# Diretório raiz do projeto (pasta que contém app.py)
BASE_DIR = Path(__file__).resolve().parent

# Carrega variáveis do .env localizado na raiz do projeto.
# Em produção, as variáveis virão direto do ambiente do servidor.
load_dotenv(BASE_DIR / ".env")


# Query params que alguns provedores (Prisma/Supabase) anexam à URL mas que o
# psycopg2/SQLAlchemy NÃO reconhecem — passá-los adiante quebra a conexão com
# "invalid connection option". São removidos antes de entregar a URL ao engine.
_PARAMS_PG_INCOMPATIVEIS = {"pgbouncer", "connection_limit", "pool_timeout", "schema"}


def _limpar_url_pg(url: str) -> str:
    """Remove de uma URL Postgres os query params que o psycopg2 não aceita."""
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.scheme.startswith("postgres"):
        return url
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _PARAMS_PG_INCOMPATIVEIS
    ]
    # Explicita o driver instalado: SQLAlchemy 2.1 passou a preferir psycopg 3.
    scheme = "postgresql+psycopg2" if parts.scheme in {"postgres", "postgresql"} else parts.scheme
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _resolve_database_uri() -> str:
    """
    Resolve a URI do banco (runtime da app). Para SQLite relativo, transforma em
    absoluto baseado em BASE_DIR. Para Postgres, remove params incompatíveis
    (ex.: ?pgbouncer=true) antes de entregar ao SQLAlchemy.
    """
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        # Sem env: usa o default absoluto.
        return f"sqlite:///{(BASE_DIR / 'instance' / 'nexo_mvp.db').as_posix()}"
    if raw == "sqlite:///:memory:":
        return raw
    # Se a URI SQLite for relativa, transforma em absoluta.
    if raw.startswith("sqlite:///") and not raw.startswith("sqlite:////"):
        caminho_relativo = raw.replace("sqlite:///", "", 1)
        caminho_abs = (BASE_DIR / caminho_relativo).resolve()
        return f"sqlite:///{caminho_abs.as_posix()}"
    return _limpar_url_pg(raw)


def _env_texto(nome: str, padrao=None):
    """Valor textual do ambiente sem espaços nas pontas; vazio vale como ausente.

    No painel da hospedagem (e num .env copiado do .env.example) é comum a variável
    existir com valor vazio ou com espaço/quebra de linha colada junto: tratar
    isso como "não configurado" evita, por exemplo, remetente vazio, URL vazia
    ou chave de API com espaço (que o provedor recusaria como credencial).
    """
    valor = os.environ.get(nome)
    if valor is None:
        return padrao
    valor = valor.strip()
    return valor or padrao


def _env_booleano(nome: str, padrao: bool) -> bool:
    """1/true/yes/on/sim liga; 0/false/no/off/nao desliga; vazio ou outro valor usa o padrão."""
    valor = (_env_texto(nome) or "").lower()
    if valor in ("1", "true", "yes", "on", "sim"):
        return True
    if valor in ("0", "false", "no", "off", "nao", "não"):
        return False
    return padrao


def _env_segundos(nome: str, padrao: float, minimo: float, maximo: float) -> float:
    """Prazo em segundos dentro de [minimo, maximo]; valor inválido usa o padrão."""
    try:
        valor = float(_env_texto(nome, padrao))
    except (TypeError, ValueError):
        return padrao
    if valor != valor:  # NaN
        return padrao
    return min(max(valor, minimo), maximo)


def _resolve_direct_uri() -> str:
    """
    URI de conexão DIRETA (porta 5432) usada para DDL/migrações (Alembic).
    No Supabase, migrações devem usar a conexão direta, não o PgBouncer (6543).
    Cai na URI principal se DIRECT_URL não estiver definida.
    """
    raw = os.environ.get("DIRECT_URL", "").strip()
    return _limpar_url_pg(raw) if raw else _resolve_database_uri()


class Config:
    """Configuração base. Use para dev local."""

    # === Flask ===
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-key-trocar-em-producao")

    # === Sessão (segurança / anti login-automático) ===
    # SESSION_PERMANENT=False -> cookie de sessão (sem Expires): expira quando o
    # navegador fecha. PERMANENT_SESSION_LIFETIME só vale se algo marcar a sessão
    # como permanente; deixamos curto por defesa.
    SESSION_PERMANENT = False
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    SESSION_COOKIE_HTTPONLY = True          # JS não lê o cookie de sessão
    SESSION_COOKIE_SAMESITE = "Lax"         # mitiga CSRF em navegação cross-site
    # SESSION_COOKIE_SECURE é ligado só em produção (HTTPS) — ver ProductionConfig.

    # === CSRF (Flask-WTF) ===
    # Token sem expiração por tempo (a sessão já é curta e morre ao fechar o
    # navegador); evita falsos "token expirado" em formulários longos.
    WTF_CSRF_TIME_LIMIT = None

    # === Rate limiting (Flask-Limiter) ===
    # Storage em memória para dev/instância única. Em produção multi-instância,
    # aponte RATELIMIT_STORAGE_URI para um Redis compartilhado.
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    # Limites por CONTA alvo (somados aos limites por IP das rotas). Um orçamento por
    # conta menor do que o teto por IP × janela seria esgotável de um único IP,
    # trancando o e-mail alvo (inclusive o do administrador); os padrões exigem
    # esforço sustentado de vários IPs e ainda limitam a adivinhação on-line.
    LIMITE_LOGIN_CONTA = os.environ.get("NEXO_LIMITE_LOGIN_CONTA", "200 per 15 minutes")
    LIMITE_RECUPERACAO_CONTA = os.environ.get("NEXO_LIMITE_RECUPERACAO_CONTA", "10 per hour")

    # === Tempo real (Socket.IO) ===
    # Origens permitidas no handshake do WebSocket. NUNCA "*" com cookie de
    # sessão (evita Cross-Site WebSocket Hijacking). Lista separada por vírgula
    # no .env; default cobre o dev local.
    SOCKETIO_CORS_ORIGINS = [
        o.strip() for o in os.environ.get(
            "SOCKETIO_CORS_ORIGINS",
            "http://127.0.0.1:5000,http://localhost:5000",
        ).split(",") if o.strip()
    ]
    # No Render, a URL pública (https://<servico>.onrender.com) chega pela
    # variável RENDER_EXTERNAL_URL, definida automaticamente pela plataforma.
    if os.environ.get("RENDER_EXTERNAL_URL"):
        SOCKETIO_CORS_ORIGINS.append(os.environ["RENDER_EXTERNAL_URL"].rstrip("/"))

    # === Origem pública canônica (SEC-005) ===
    # Links sensíveis enviados por e-mail (ativação, recuperação de senha,
    # chamados, publicação) são montados SEMPRE a partir desta origem, nunca
    # do cabeçalho Host da requisição (ver origem_publica.py). Domínio próprio
    # em NEXO_PUBLIC_URL; sem ela, vale a URL que o Render injeta. A factory
    # valida o valor (produção: https obrigatório) e, sem nenhuma das duas,
    # usa http://127.0.0.1:5000 apenas em desenvolvimento.
    # Vazio ou só com espaços vale como ausente (_env_texto): um NEXO_PUBLIC_URL
    # em branco no painel não pode esconder a RENDER_EXTERNAL_URL e impedir o
    # boot em produção.
    PUBLIC_URL = _env_texto("NEXO_PUBLIC_URL") or _env_texto("RENDER_EXTERNAL_URL") or ""
    PUBLIC_URL_PLATAFORMA = _env_texto("RENDER_EXTERNAL_URL") or ""
    # Hospedagem cujas portas SMTP ficam bloqueadas: a plataforma injeta RENDER=true
    # e, na máquina local, é sempre False. emails.py só lê este indicador neutro.
    HOSPEDAGEM_BLOQUEIA_SMTP = bool(os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL"))
    # A hospedagem liberou as portas 465/587: com SMTP configurado, o diagnóstico
    # deixa de tratar o SMTP como bloqueado.
    SMTP_PERMITIDO = _env_booleano("NEXO_SMTP_PERMITIDO", False)
    # Hosts adicionais aceitos nas requisições (CSV; ".exemplo.com" aceita os
    # subdomínios). O host da origem pública e o da plataforma entram sempre
    # em produção; em desenvolvimento, sem a variável, não há restrição.
    TRUSTED_HOSTS_EXTRAS = [
        h.strip() for h in os.environ.get("NEXO_TRUSTED_HOSTS", "").split(",") if h.strip()
    ]

    # === Cabeçalhos de segurança (SEC-010, seguranca_http.py) ===
    # 1 = emite a CSP em modo somente-relatório (Content-Security-Policy-
    # Report-Only) para observar violações sem bloquear; padrão: aplicada.
    CSP_REPORT_ONLY = os.environ.get("NEXO_CSP_REPORT_ONLY", "0") == "1"

    # === Proxy reverso ===
    # Nº de proxies confiáveis à frente da app (Render = 1). 0 = sem proxy (dev).
    # Ver ProxyFix em app.py.
    PROXY_FIX_HOPS = int(os.environ.get("PROXY_FIX_HOPS", 0))

    # === SQLAlchemy ===
    SQLALCHEMY_DATABASE_URI = _resolve_database_uri()
    # URI direta (5432) para Alembic/Flask-Migrate. A app usa a pooled (6543);
    # as migrações usam esta quando create_app roda em modo direto.
    SQLALCHEMY_DIRECT_URI = _resolve_direct_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Mostra SQL gerado no console — útil em dev, ruim em prod.
    SQLALCHEMY_ECHO = False
    # Robustez com o PgBouncer do Supabase (porta 6543, transaction pooling): o
    # pooler pode fechar conexões ociosas, então validamos a conexão antes de
    # usar (pre_ping) e reciclamos antes do timeout típico. Inócuo para SQLite.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 280}
    # Pool do PostgreSQL coerente com o servidor (gunicorn -w 1 --threads 50):
    # sem teto explícito, 50 threads disputam 15 conexões e esperam 30 s cada
    # (esgotamento). pool_timeout curto falha rápido; tamanho e overflow são
    # ajustáveis pelo ambiente conforme o limite do pooler (Supabase).
    if SQLALCHEMY_DATABASE_URI.startswith("postgresql"):
        SQLALCHEMY_ENGINE_OPTIONS.update({
            "pool_size": int(os.environ.get("NEXO_DB_POOL_SIZE", 5)),
            "max_overflow": int(os.environ.get("NEXO_DB_MAX_OVERFLOW", 10)),
            "pool_timeout": int(os.environ.get("NEXO_DB_POOL_TIMEOUT", 10)),
        })

    # === Admin Master (seed) ===
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@nexo.com.br")
    ADMIN_NOME = os.environ.get("ADMIN_NOME", "Administrador NEXO")
    ADMIN_SENHA = os.environ.get("ADMIN_SENHA")  # sem default — quebra se faltar

    # === Uploads ===
    UPLOAD_FOLDER = BASE_DIR / os.environ.get("UPLOAD_FOLDER", "uploads")
    MAX_CONTENT_LENGTH = int(
        os.environ.get("MAX_CONTENT_LENGTH_MB", 20)
    ) * 1024 * 1024  # MB → bytes

    # === E-mail (Flask-Mail / SMTP) ===
    # Valores vazios ou só com espaços valem como ausentes (_env_texto): uma
    # variável criada vazia no painel não pode virar remetente "" ou chave " ".
    MAIL_SERVER = _env_texto("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(_env_texto("MAIL_PORT", "587"))
    MAIL_USE_TLS = _env_booleano("MAIL_USE_TLS", True)
    # A porta 465 é SSL direto: sem a variável, o padrão acompanha a porta (um SMTP
    # sem TLS nela só esgotaria o prazo). O valor explícito no .env sempre vale.
    MAIL_USE_SSL = _env_booleano("MAIL_USE_SSL", MAIL_PORT == 465)
    MAIL_USERNAME = _env_texto("MAIL_USERNAME")
    # Senha de app copiada da página do Google traz espaço especial (U+00A0) entre
    # os blocos: o smtplib não o codifica, e o espaço comum funciona no login.
    MAIL_PASSWORD = (os.environ.get("MAIL_PASSWORD") or "").replace("\u00a0", " ") or None
    MAIL_DEFAULT_SENDER = (
        _env_texto("MAIL_DEFAULT_SENDER") or MAIL_USERNAME or "nexofaturamentointeligente@gmail.com"
    )
    # O remetente foi escolhido no .env, ou é só o MAIL_USERNAME por falta dele?
    MAIL_DEFAULT_SENDER_EXPLICITO = bool(_env_texto("MAIL_DEFAULT_SENDER"))
    # SEGURANÇA: por padrão o Flask-Mail herda app.debug e, em modo debug, o
    # smtplib imprime toda a conversa SMTP no log — inclusive a linha AUTH em
    # base64, que contém a senha. Forçamos OFF para a credencial nunca vazar.
    MAIL_DEBUG = False
    # O .env que load_dotenv lê acima; o Diagnóstico de e-mail mostra o caminho
    # em ambiente local, porque é ali que as credenciais precisam estar.
    ARQUIVO_ENV = str(BASE_DIR / ".env")
    # Envio por API HTTPS do Brevo (porta 443), opcional. Serve a redes e hospedagens
    # que bloqueiam as portas SMTP 25/465/587. Com a chave definida, emails.py usa a
    # API em vez do SMTP; sem ela, segue o SMTP acima.
    # A chave é a de API (prefixo xkeysib-), não a chave SMTP (xsmtpsib-). O
    # remetente (MAIL_DEFAULT_SENDER) precisa estar verificado no Brevo e o IP de
    # saída do servidor autorizado (Docs/EMAILS_TRANSACIONAIS.md).
    BREVO_API_KEY = _env_texto("BREVO_API_KEY")
    BREVO_API_URL = _env_texto("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email")
    # Prazo (s) de cada tentativa no provedor: conexão e resposta da API HTTPS e
    # do SMTP (o Flask-Mail, sozinho, abriria o SMTP sem prazo nenhum).
    EMAIL_TIMEOUT_SEGUNDOS = _env_segundos("NEXO_EMAIL_TIMEOUT", 15.0, 1.0, 60.0)
    # Alerta no sininho dos administradores quando o provedor não aceita um
    # e-mail (credencial, IP, remetente, rede...) ou quando o envio está
    # desativado — no máximo 1 aviso por categoria a cada 30 min. Ligado em
    # produção (ProductionConfig); o dashboard não exibe alertas técnicos.
    EMAIL_ALERTA_ADMIN = _env_texto("NEXO_EMAIL_ALERTA_ADMIN", "0") == "1"
    # Quando não há credenciais, o envio é suprimido pela camada emails.py
    # (sem quebrar fluxos). Flag derivada para checagem rápida nos gatilhos.
    MAIL_ATIVO = bool((MAIL_USERNAME and MAIL_PASSWORD) or BREVO_API_KEY)
    # Formatos aceitos no envio das análises mensais, em que o arquivo é guardado. O .xls e a
    # Planilha XML são lidos só no Raio-X e na Avaliação de Cenário, em que o arquivo é lido e
    # descartado (Docs/UNIFICACAO_MOTOR_ETL.md): aqui continuam de fora.
    ALLOWED_EXTENSIONS = {"csv", "xlsx"}

    # SEC-005: limites finitos; valores invalidos impedem inicializacao.
    ETL_LIMITS = asdict(limits_from_environment())

    # === Gestão contratual (contract_policy.py) ===
    # Convenções PROPOSTAS, pendentes de homologação comercial (ver
    # Docs/GESTAO_CONTRATUAL/05_PENDENCIAS_DE_DECISAO_COMERCIAL.md). São
    # aplicadas no instante da suspensão e gravadas no contrato: mudar estes
    # valores não altera prazos de suspensões já registradas.
    CONTRATO_SUSPENSAO_MESES = 3          # meses de calendário, não 90 dias
    CONTRATO_EXPORTACAO_PDF_DIAS = 14     # dias corridos desde a suspensão
    CONTRATO_FUSO_HORARIO = "America/Sao_Paulo"
    CONTRATO_ALERTA_VENCIMENTO_DIAS = 30  # visão geral: vigência terminando
    CONTRATO_ALERTA_SUSPENSAO_DIAS = 15   # visão geral: suspensão terminando
    CONTRATO_AVISO_RENOVACAO_DIAS = 14    # lembrete ao cliente: vigência terminando
    TERMOS_ALERTA_PENDENCIA_DIAS = 7      # aviso ao admin: aceite pendente depois de publicar

    # === IA (Claude / Anthropic) — geração automatizada da devolutiva ===
    # Sem a chave, o motor cai no fallback determinístico local (não quebra).
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or None
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")

    # === NexoBot (suporte) — redação opcional via Hugging Face Inference Providers ===
    # O NexoBot decide tudo localmente (intenção, página, resposta do Guia). Com
    # o token, um modelo apenas REDIGE respostas a partir de trechos do Guia, pelo
    # roteador https://router.huggingface.co/v1 (o endpoint antigo foi descontinuado).
    # Uso cobrado por consumo na conta Hugging Face; sem token, nada é enviado.
    HF_API_TOKEN = os.environ.get("HF_API_TOKEN") or None
    HF_MODEL = os.environ.get("HF_MODEL", "meta-llama/Llama-3.1-8B-Instruct")

    # === WhatsApp (Evolution API) — whatsapp_service.py ===
    # Disparos ativos ao cliente (relatório pronto, abertura do envio do mês, chamado
    # atualizado) por uma API REST compatível com a Evolution API. Sem a URL (https;
    # http só em localhost), a chave e a instância, o envio fica DESLIGADO: cada gatilho
    # só registra um aviso no log, sem travar o fluxo. Cadastro inicial e recuperação
    # de senha continuam só por e-mail.
    EVOLUTION_API_URL = _env_texto("EVOLUTION_API_URL")
    EVOLUTION_API_KEY = _env_texto("EVOLUTION_API_KEY")
    EVOLUTION_INSTANCE_NAME = _env_texto("EVOLUTION_INSTANCE_NAME")
    # Número comercial da NEXO com DDI (ex.: 5562999999999), dos botões wa.me das
    # telas. A factory o normaliza (só dígitos, com o 55); vazio ou inválido esconde
    # os botões, e as telas mostram os textos de sempre.
    WHATSAPP_EMPRESA_NUMERO = _env_texto("WHATSAPP_EMPRESA_NUMERO") or ""


class DevelopmentConfig(Config):
    DEBUG = True
    # Echo do SQL só sob demanda: com ele ligado, os parâmetros (inclusive o
    # hash da senha em INSERT/UPDATE de usuario) vão para o log de desenvolvimento.
    SQLALCHEMY_ECHO = os.environ.get("NEXO_SQL_ECHO", "0") == "1"


class ProductionConfig(Config):
    DEBUG = False
    # Em produção, SECRET_KEY DEVE vir do ambiente. Sem fallback.
    # A factory valida somente ao iniciar produção, sem impedir imports de dev.
    SECRET_KEY = os.environ.get("SECRET_KEY")
    # HTTPS em produção: cookie de sessão só trafega em conexão segura.
    SESSION_COOKIE_SECURE = True
    # Falha de e-mail em produção vira aviso no sininho dos administradores
    # (desligável com NEXO_EMAIL_ALERTA_ADMIN=0).
    EMAIL_ALERTA_ADMIN = _env_texto("NEXO_EMAIL_ALERTA_ADMIN", "1") == "1"


# Dicionário usado pela factory para escolher config por nome.
config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
}
