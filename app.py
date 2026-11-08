"""
NEXO - Faturamento Inteligente | Entry point Flask
===================================================
Application Factory pattern. A função `create_app()` é a única
forma de instanciar a aplicação — facilita testes, múltiplos ambientes
e evita import circular.

EVENT LISTENER CRÍTICO:
    O SQLite vem com foreign keys DESLIGADAS por padrão.
    O PRAGMA escrito no .sql só vale na sessão que executou.
    A função `_set_sqlite_pragma` abaixo é registrada via SQLAlchemy event
    e força `PRAGMA foreign_keys = ON` em TODA conexão nova.
    SEM ISSO, qualquer FK no schema é decorativa.
"""

import math
import os
import secrets
import logging
import time
from pathlib import Path

from flask import Flask, redirect, url_for, render_template, session, request, abort, make_response, g
from flask_login import current_user, logout_user, login_required
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from config import config_by_name
from extensions import db, login_manager, migrate, mail, socketio, csrf, limiter
from log_security import install_token_log_redaction


def _set_sqlite_pragma(dbapi_connection, connection_record):
    """
    Garante que CADA conexão SQLite seja aberta com:
      - foreign_keys = ON   → FKs validadas de verdade
      - journal_mode = WAL  → permite múltiplos leitores + 1 escritor (melhor no Flask)
      - synchronous = NORMAL → equilíbrio razoável entre durabilidade e velocidade

    Registrada globalmente abaixo via @event.listens_for(Engine, "connect").
    Aplica-se a TODOS os engines SQLite — tanto da app principal quanto de
    qualquer script auxiliar (init_db.py, seed.py) desde que importem este módulo.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.close()


# Registra o listener no nível do Engine SQLAlchemy.
# Atenção: tem que ser ANTES de qualquer conexão ser aberta.
@event.listens_for(Engine, "connect")
def _connect_listener(dbapi_connection, connection_record):
    # SQLAlchemy aceita engines de outros bancos (Postgres, MySQL).
    # Aplicar PRAGMA SQLite em Postgres explode. Por segurança, só roda
    # se a conexão for de fato SQLite.
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        _set_sqlite_pragma(dbapi_connection, connection_record)


def create_app(config_name: str = None) -> Flask:
    """
    Factory da aplicação. Use:
        from app import create_app
        app = create_app("development")
    """
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_by_name[config_name])
    # Politica finita validada antes de inicializar banco/servidor.
    from etl_limits import ETLLimits
    from dataclasses import asdict
    app.config["ETL_LIMITS"] = asdict(ETLLimits.from_mapping(app.config.get("ETL_LIMITS", {})))

    if config_name == "production":
        secret_key = app.config.get("SECRET_KEY")
        if not secret_key or not secret_key.strip() or secret_key == "dev-key-trocar-em-producao":
            raise RuntimeError("SECRET_KEY válida é obrigatória em produção.")
        # Chave curta/previsível assina cookies e tokens: exigir ao menos 32
        # caracteres (render.yaml gera 64 por generateValue). O valor nunca é logado.
        if len(secret_key.strip()) < 32:
            raise RuntimeError("SECRET_KEY em produção deve ter ao menos 32 caracteres aleatórios.")
        # O disco do Render é efêmero: não iniciar silenciosamente com SQLite.
        if not os.environ.get("DATABASE_URL", "").strip():
            raise RuntimeError("DATABASE_URL PostgreSQL é obrigatória em produção.")
        if os.environ.get("NEXO_DB_DIRECT") == "1" and not os.environ.get("DIRECT_URL", "").strip():
            raise RuntimeError("DIRECT_URL é obrigatória para migrações em produção.")

    # Identificador único deste BOOT do servidor. Como a SECRET_KEY é fixa, os
    # cookies de sessão sobreviveriam a um restart e re-logariam o usuário
    # automaticamente. Carimbamos a sessão com este BOOT_ID no login e, a cada
    # request, invalidamos qualquer sessão de um boot anterior (ver guard abaixo).
    # Resultado: derrubar/subir o servidor SEMPRE exige novo login.
    app.config["BOOT_ID"] = secrets.token_hex(8)

    # Modo DIRETO de banco: usado por DDL/migrações (Alembic) para falar com o
    # Postgres na conexão direta (5432) em vez do PgBouncer (6543). Ativado por
    #   NEXO_DB_DIRECT=1 flask db upgrade
    if os.environ.get("NEXO_DB_DIRECT") == "1":
        app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DIRECT_URI"]

    if config_name == "production":
        from sqlalchemy.engine import make_url
        if make_url(app.config["SQLALCHEMY_DATABASE_URI"]).get_backend_name() != "postgresql":
            raise RuntimeError("Produção exige PostgreSQL; SQLite é apenas para desenvolvimento/testes.")

    # SEC-005: origem pública canônica (links sensíveis) e hosts confiáveis,
    # validadas antes das extensões (o Socket.IO lê a lista de origens aqui).
    from origem_publica import configurar_origem_publica
    configurar_origem_publica(app, producao=config_name == "production")
    # SEC-010: cabeçalhos de segurança, CSP com nonce e cache (seguranca_http.py).
    app.config["PRODUCAO_HTTP"] = config_name == "production"
    from seguranca_http import instalar as instalar_cabecalhos_seguranca
    instalar_cabecalhos_seguranca(app)

    # Garante que a pasta instance/ exista (onde o SQLite vai morar).
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    # E a pasta de uploads.
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    # Inicializa extensões
    db.init_app(app)
    login_manager.init_app(app)
    # Flask-Migrate precisa enxergar os modelos — importados logo abaixo no
    # user_loader/blueprints; aqui basta registrar db + app.
    migrate.init_app(app, db)
    mail.init_app(app)
    # Socket.IO com origens restritas (anti-CSWSH) — ver config.SOCKETIO_CORS_ORIGINS.
    socketio.init_app(app, cors_allowed_origins=app.config["SOCKETIO_CORS_ORIGINS"])
    # Proteção CSRF global (todas as rotas mutáveis exigem token válido).
    csrf.init_app(app)
    # Rate limiting (limites explícitos por rota via decorator).
    limiter.init_app(app)
    # Registra os handlers de WebSocket (connect/disconnect → salas por usuário).
    import realtime
    realtime.inicializar_realtime(app)

    # Configura logging básico
    logging.basicConfig(
        level=logging.INFO if not app.debug else logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    install_token_log_redaction()
    # Resumo da configuração de e-mail no log de inicialização (sem segredos);
    # pendências como "envio desativado" ou "chave SMTP no lugar da de API"
    # aparecem como WARNING nos logs do Render (emails.py).
    from emails import registrar_diagnostico_inicial
    registrar_diagnostico_inicial(app)

    @app.after_request
    def _proteger_resposta_token(resposta):
        # O bearer fica no caminho da URL: não propagar como Referer nem cachear.
        # "strict-origin" envia SÓ a origem (https://host/), nunca o caminho com
        # o token. Não usar "no-referrer": sob HTTPS o CSRF do Flask-WTF
        # (WTF_CSRF_SSL_STRICT) exige o cabeçalho Referer, e sem ele o POST da
        # nova senha era recusado — o cliente não conseguia ativar a conta nem
        # redefinir a senha pelo link do e-mail.
        if request.path.startswith(("/auth/resetar-senha/", "/auth/primeiro-acesso/")):
            resposta.headers["Referrer-Policy"] = "strict-origin"
            resposta.headers["Cache-Control"] = "no-store"
        return resposta

    # Registra user loader do Flask-Login
    # (Necessário aqui porque depende do modelo Usuario)
    from models import Usuario
    from sqlalchemy.exc import SQLAlchemyError
    from session_security import carregar_usuario, sessao_valida

    @login_manager.user_loader
    def load_user(user_id: str):
        # Identificador inválido não deve causar 500. O snapshot do banco
        # é recarregado em cada request, sem confiar no identity map antigo.
        try:
            return carregar_usuario(user_id)
        except SQLAlchemyError as erro:
            db.session.rollback()
            g.falha_banco_autenticacao = True
            app.logger.warning("Autenticação indisponível (%s).", type(erro).__name__)
            return None

    @app.before_request
    def _guard_sessao():
        """
        Blindagem contra login automático:
          1) Sessão carimbada com um BOOT_ID diferente do atual = sobrou de um
             boot anterior do servidor (ou nunca foi carimbada) → desloga e
             limpa, forçando novo login após restart.
          2) Cookie órfão: aponta para um usuário que não existe mais no banco
             (ex.: banco resetado) → current_user vira anônimo, mas o resíduo
             _user_id é limpo da sessão.
        Não toca em requisições de assets estáticos (sem custo por arquivo).
        """
        if request.endpoint == "static":
            return
        autenticado = current_user.is_authenticated
        valida = False
        if autenticado and not getattr(g, "falha_banco_autenticacao", False):
            try:
                valida = sessao_valida(current_user)
            except SQLAlchemyError as erro:
                db.session.rollback()
                g.falha_banco_autenticacao = True
                app.logger.warning("Autorizacao indisponivel (%s).", type(erro).__name__)
        if getattr(g, "falha_banco_autenticacao", False):
            logout_user()
            session.clear()
            mensagem = "Autenticação temporariamente indisponível. Tente novamente."
            if request.path.startswith("/api/"):
                return {"erro": mensagem}, 503
            return render_template("erro_simples.html", titulo="Serviço indisponível",
                                   mensagem=mensagem), 503
        if autenticado:
            # BOOT_ID, conta ativa e fingerprint da credencial são conferidos
            # juntos. Cookie legado sem fingerprint também exige novo login.
            if not valida:
                logout_user()
                session.clear()
        elif "_user_id" in session:
            session.clear()

    @app.before_request
    def _guard_caractere_nulo():
        """
        Recusa (400) requisições com o caractere NUL (\\x00) em query string ou
        formulário. O Postgres não armazena NUL em colunas de texto e o psycopg2
        lança ValueError antes da query — sem esta barreira, qualquer campo de
        texto livre (login, tickets, cadastro, CMS) vira um erro 500.
        """
        valores = list(request.args.values())
        if request.method in ("POST", "PUT", "PATCH"):
            valores += list(request.form.values())
        if any("\x00" in v for v in valores):
            return render_template("erro_simples.html", titulo="Requisição inválida",
                                   mensagem="Os dados enviados contêm caracteres inválidos."), 400

    @app.before_request
    def _guard_primeiro_acesso():
        """
        Força a troca de senha no primeiro acesso: um CLIENTE logado com
        primeiro_acesso=True é interceptado e mandado para /auth/primeiro-acesso,
        bloqueando dashboard, suporte, upload etc. até definir a nova senha.
        Libera apenas: assets estáticos, a própria rota de troca e o logout.
        """
        if request.endpoint in ("static", "auth.primeiro_acesso", "auth.logout"):
            return
        if (
            current_user.is_authenticated
            and current_user.is_cliente
            and getattr(current_user, "primeiro_acesso", False)
        ):
            return redirect(url_for("auth.primeiro_acesso"))

    # Aceite obrigatório dos Termos de Uso e da Política de Privacidade (termos_legais.py):
    # depois da troca de senha, o CLIENTE com versão vigente sem aceite só vê o modal. No
    # login do admin, as pendências vencidas vão para o sininho dele.
    import termos_legais
    from flask_login import user_logged_in
    app.before_request(termos_legais.trava_de_aceite)
    user_logged_in.connect(termos_legais.ao_entrar, app)

    # Registra blueprints
    from blueprints.auth import auth_bp
    from blueprints.admin import admin_bp
    from blueprints.cliente import cliente_bp
    from blueprints.notificacoes import notificacoes_bp
    from blueprints.api import api_bp
    from blueprints.contratos import contratos_bp
    from blueprints.onboarding import onboarding_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(cliente_bp, url_prefix="/cliente")
    app.register_blueprint(notificacoes_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(contratos_bp)
    # Planos por nível e Raio-X de entrada (entitlements.py): rotas /onboarding/* e /api/onboarding/*.
    app.register_blueprint(onboarding_bp)

    # Gestão contratual: prazos exibidos no fuso da política (nunca no do
    # navegador) e comandos `flask contratos` / `flask planos` (simulação padrão).
    from contract_policy import formatar_instante
    app.jinja_env.filters["instante_local"] = formatar_instante
    # Suporte e sininho: horário de Brasília e ISO 8601 do instante (horario.py).
    from horario import horario_brasilia, iso_utc
    app.jinja_env.filters["horario_brasilia"] = horario_brasilia
    app.jinja_env.filters["iso_utc"] = iso_utc
    from cli_contratos import registrar_comandos
    registrar_comandos(app)

    # Context processor: injeta os dados do sininho (contagem + 5 recentes) em
    # TODA página renderizada, para o usuário autenticado. Mantém o template
    # base.html agnóstico de qual rota o serviu.
    from notifications import contexto_sininho, classificar_notificacao

    @app.context_processor
    def _injeta_sininho():
        return contexto_sininho(current_user)

    # Menu do cliente conforme o plano (itens de plano superior com cadeado e selo UPGRADE).
    # Falha de banco devolve o menu de sempre, sem os itens de plano.
    import entitlements

    @app.context_processor
    def _injeta_menu_cliente():
        return entitlements.contexto_menu(current_user)

    # Expõe o classificador de badge ao template (mesma lógica do payload do socket).
    app.jinja_env.globals["badge_notif"] = classificar_notificacao

    # URL das imagens do Guia (guardadas no banco via storage.py; referências
    # legadas continuam servidas de static/).
    import storage
    app.jinja_env.globals["url_midia"] = storage.url_midia

    @app.route("/midia/<path:chave>")
    @login_required
    def midia(chave):
        """Serve as imagens do Guia guardadas no banco. Restrito ao prefixo
        guia/ — planilhas de clientes NUNCA saem por aqui."""
        if not chave.startswith("guia/"):
            abort(404)
        registro = storage.obter(chave)
        if registro is None:
            abort(404)
        resp = make_response(registro.conteudo)
        resp.headers["Content-Type"] = registro.content_type or "application/octet-stream"
        # Nome = hash do conteúdo: o mesmo URL sempre devolve os mesmos bytes.
        resp.headers["Cache-Control"] = "private, max-age=86400"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return resp

    # Rota raiz: serve a landing page institucional (entrada comercial pública).
    # Usuários já autenticados também caem aqui — basta clicar em "Acessar portal"
    # que o auth.login os redireciona direto para o dashboard do perfil correto.
    @app.route("/")
    def index():
        return render_template("landing.html")

    # Onboarding comercial: explica como obter acesso (handoff via WhatsApp + reunião).
    # Linkado pelo login ("É novo por aqui?") e pelo footer da landing.
    @app.route("/como-aderir")
    def como_aderir():
        return render_template("como_aderir.html")

    # Health check simples para verificar se a app subiu. Inclui o commit
    # publicado (o Render injeta RENDER_GIT_COMMIT em cada deploy; fora dele o
    # campo diz "desconhecida"): permite conferir, sem login, qual versão está
    # no ar e comparar com a `main` do GitHub.
    versao_publicada = (os.environ.get("RENDER_GIT_COMMIT") or "").strip()[:7] or "desconhecida"

    @app.route("/healthz")
    def healthz():
        return {"status": "ok", "versao": versao_publicada}, 200

    # Health check do BANCO: faz um SELECT 1 no Postgres. Pensado para um
    # monitor externo (ex.: UptimeRobot a cada 5 min), que assim mantém o host
    # gratuito acordado E gera atividade no Supabase, cujo plano free pausa o
    # projeto após 7 dias sem consultas. 503 se o banco estiver fora.
    @app.route("/healthz/db")
    @limiter.limit("30 per minute")  # anônimo e consome conexão: sem abuso
    def healthz_db():
        try:
            db.session.execute(text("SELECT 1"))
            return {"status": "ok", "db": "ok"}, 200
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            app.logger.warning("Health check do banco falhou (%s).", type(e).__name__)
            return {"status": "erro", "db": "indisponivel"}, 503

    # Tratamento amigável de CSRF inválido/ausente (Flask-WTF) e rate limit (429).
    from flask_wtf.csrf import CSRFError

    def _erro_api(mensagem, status):
        """Chamadas fetch (/api/) recebem JSON, não um redirect ou página HTML."""
        from flask import jsonify
        return jsonify(erro=mensagem, resposta=mensagem, fonte="local", acao=None, destino=None), status

    @app.errorhandler(CSRFError)
    def _csrf_error(e):
        if request.path.startswith("/api/"):
            return _erro_api("Sua sessão expirou. Atualize a página e tente novamente.", 400)
        # Token ausente/expirado → página do portal com a mensagem: o navegador não segue
        # Location numa resposta 400. Evita expor o traceback de "The CSRF token is missing".
        return _pagina_erro(400, "Formulário expirado",
                            "Sua sessão expirou ou este formulário é antigo. Nada foi gravado: "
                            "abra a página de novo e repita.")

    @app.errorhandler(429)
    def _rate_limit(e):
        if request.path.startswith("/api/suporte-bot"):
            return _erro_api("Você enviou muitas mensagens em pouco tempo. "
                             "Aguarde um minuto e tente novamente.", 429)
        if request.path.startswith("/api/"):
            return _erro_api("Você fez muitas requisições em pouco tempo. "
                             "Aguarde alguns minutos e tente novamente.", 429)
        # O limite pode durar de um minuto a uma hora: o texto acompanha o que estourou.
        limite = limiter.current_limit
        minutos = max(1, math.ceil((limite.reset_at - 1 - time.time()) / 60)) if limite else 1
        espera = "um minuto" if minutos == 1 else f"{minutos} minutos"
        return render_template("erro_simples.html", titulo="Muitas tentativas", codigo=429,
                               painel_url=_painel_url(),
                               mensagem="Você fez muitas requisições em pouco tempo. "
                                        f"Aguarde {espera} e tente novamente."), 429

    # Páginas de erro na identidade do NEXO (antes: a página padrão do servidor,
    # branca e em inglês). O status HTTP não muda; chamadas /api/ recebem JSON.
    def _painel_url():
        """Link de volta ao painel do perfil logado; None sem sessão (ou se o banco
        estiver indisponível, caso do erro 500)."""
        try:
            if current_user.is_authenticated:
                return url_for("admin.dashboard" if current_user.is_admin else "cliente.dashboard")
        except Exception:  # noqa: BLE001
            pass
        return None

    def _pagina_erro(status, titulo, mensagem):
        if request.path.startswith("/api/"):
            return _erro_api(mensagem, status)
        contexto = {"titulo": titulo, "mensagem": mensagem, "painel_url": _painel_url(), "codigo": status}
        try:
            return render_template("erro_simples.html", **contexto), status
        except Exception:  # noqa: BLE001
            # Banco inacessível no meio da requisição: algum context processor
            # (sininho, catálogo do NexoBot, usuário expirado) falhou. A página de
            # erro é renderizada sem eles, em vez da página branca do servidor.
            app.logger.exception("Página de erro %s renderizada sem o contexto da sessão", status)
            try:
                db.session.rollback()
            except Exception:  # noqa: BLE001
                pass
            contexto["painel_url"] = None
            return app.jinja_env.get_template("erro_simples.html").render(**contexto), status

    from werkzeug.exceptions import SecurityError

    @app.errorhandler(SecurityError)
    def _host_nao_confiavel(e):
        # Host fora de TRUSTED_HOSTS (SEC-005): o adaptador de URLs não existe
        # nesta requisição, então nada de templates ou url_for. O valor
        # recebido não é registrado (é controlado pelo cliente).
        app.logger.warning("Requisição recusada: cabeçalho Host fora dos hosts confiáveis.")
        resposta = make_response("Requisição inválida: host não reconhecido.", 400)
        resposta.headers["Content-Type"] = "text/plain; charset=utf-8"
        resposta.headers["Cache-Control"] = "no-store"
        return resposta

    @app.errorhandler(404)
    def _nao_encontrado(e):
        return _pagina_erro(404, "Página não encontrada",
                            "O endereço não existe ou o item foi removido. Confira o link ou volte ao painel.")

    @app.errorhandler(403)
    def _acesso_negado(e):
        return _pagina_erro(403, "Acesso negado", "Você não tem permissão para abrir esta página.")

    @app.errorhandler(405)
    def _metodo_nao_permitido(e):
        # Também cobre /api/: JSON, não a página HTML do servidor.
        return _pagina_erro(405, "Método não permitido",
                            "Esta ação não pode ser feita por este tipo de requisição.")

    @app.errorhandler(413)
    def _grande_demais(e):
        # O corpo inteiro passou de MAX_CONTENT_LENGTH. Nada foi lido nem gravado; /api/ recebe
        # JSON, as páginas a identidade do portal. O Raio-X manda vários meses por envio.
        limite = app.config.get("MAX_CONTENT_LENGTH")
        teto = (f"{limite // 1048576} MB" if limite and limite >= 1048576
                else f"{limite // 1024} KB" if limite else "o limite do servidor")
        if request.endpoint in ("onboarding.diagnostico_enviar", "onboarding.api_diagnostico_enviar"):
            mensagem = f"Os relatórios enviados passam de {teto} somados. Nada foi gravado: envie menos meses por vez."
        else:
            mensagem = f"O envio passa de {teto}. Nada foi gravado: envie um arquivo menor."
        return _pagina_erro(413, "Envio grande demais", mensagem)

    @app.errorhandler(500)
    def _erro_interno(e):
        # Erro no meio de uma transação: no PostgreSQL ela fica abortada e qualquer
        # consulta feita pela própria página de erro (sininho, usuário) falharia e
        # devolveria a página branca do servidor. O rollback limpa a sessão antes.
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return _pagina_erro(500, "Algo deu errado",
                            "Ocorreu um erro inesperado. Tente novamente em instantes.")

    # Atrás de proxy reverso (Render, Railway, nginx...): confia nos cabeçalhos
    # X-Forwarded-For/Proto de N saltos (PROXY_FIX_HOPS). Sem isso, TODOS os
    # usuários aparecem com o IP do proxy (o rate limit do login viraria global)
    # e os links _external dos e-mails sairiam como http:// em vez de https://.
    # Desligado por padrão: fora de um proxy, esses cabeçalhos seriam forjáveis.
    hops = app.config.get("PROXY_FIX_HOPS", 0)
    if hops:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops)

    return app


# Entry point para `python app.py` (dev local).
# Em produção, use um servidor compatível com WebSocket (ex.: gunicorn com
# worker apropriado). O socketio.run sobe o servidor com suporte a WebSocket.
if __name__ == "__main__":
    config_name = os.environ.get("FLASK_ENV", "development")
    app = create_app(config_name)

    # SEGURANÇA: o debugger interativo do Werkzeug permite execução de código
    # (RCE via PIN) e jamais pode ficar exposto em produção. Só habilitamos o
    # debug — e o allow_unsafe_werkzeug — quando o ambiente é DESENVOLVIMENTO.
    is_dev = config_name == "development"
    if app.config.get("DEBUG") and not is_dev:
        raise RuntimeError(
            "DEBUG ligado fora de 'development' — recuse-se a subir o debugger "
            "em produção. Use gunicorn/uvicorn ou FLASK_ENV=development local."
        )

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    socketio.run(
        app,
        host=host,
        port=port,
        debug=is_dev and app.config.get("DEBUG", False),
        # allow_unsafe_werkzeug só em dev (o servidor de dev do Werkzeug não é
        # para produção). Em produção, sirva via gunicorn com worker WebSocket.
        allow_unsafe_werkzeug=is_dev,
    )
