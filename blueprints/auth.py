import logging
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app, has_request_context, make_response
from werkzeug.exceptions import HTTPException
from werkzeug.routing import RequestRedirect
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadData
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import chave_limite_conta, chave_limite_usuario, db, limiter
from models import Empresa, Usuario
from contas_email import buscar_usuario_por_email, email_em_uso, email_valido, normalizar_email
from emails import (
    email_boas_vindas, email_email_alterado, email_recuperacao_senha, email_senha_alterada, envio_disponivel,
    registrar_envio_suprimido,
)
from contract_policy import obter_situacao_contratual
from origem_publica import url_publica
from politica_senha import validar_nova_senha
from session_security import (
    CHAVE_CREDENCIAL, CHAVE_SESSAO, carregar_usuario, condicoes_empresa_acessivel,
    carimbar_sessao, hash_token_sessao, impressao_sessao, impressao_credencial_sessao,
    mesma_impressao, registrar_sessao, revogar_sessao_por_hash, revogar_sessoes_usuario,
    usuario_pode_acessar, vincular_geracao_autorizacao,
)

logger = logging.getLogger(__name__)

# Criação do Blueprint de Autenticação
auth_bp = Blueprint("auth", __name__)

# Reautenticação nas Configurações: por usuário e somando as rotas (senha errada
# também conta), para um cookie sequestrado não testar senhas em série alternando
# entre a troca de senha e a de e-mail.
limite_reautenticacao = limiter.shared_limit("10 per 15 minutes", scope="reautenticacao-configuracoes",
                                             key_func=chave_limite_usuario)


def _destino_seguro(alvo: str) -> bool:
    """
    True se `alvo` é um caminho LOCAL seguro para redirect (mitiga open redirect).
    Rejeita URLs absolutas e protocol-relative (//evil.com, /\\evil.com), que o
    navegador trataria como destino externo.
    """
    if not alvo:
        return False
    # O navegador descarta tabulações e quebras de linha e trata "\" como "/":
    # "/<TAB>/evil.com" viraria "//evil.com". Nenhum destino legítimo os contém.
    if "\\" in alvo or any(ord(caractere) < 32 or ord(caractere) == 127 for caractere in alvo):
        return False
    parsed = urlparse(alvo)
    # Sem esquema e sem netloc = caminho relativo ao próprio site.
    return (
        not parsed.scheme
        and not parsed.netloc
        and alvo.startswith("/")
        and not alvo.startswith("//")
        and not alvo.startswith("/\\")
    )

def _destino_abre_por_get(alvo: str) -> bool:
    """O login volta ao destino com GET. Uma ação só POST (sessão expirada no meio de
    um envio de formulário) viraria a página de "método não permitido"."""
    try:
        current_app.url_map.bind("").match(urlparse(alvo).path, method="GET")
    except RequestRedirect:
        return True
    except HTTPException:
        return False
    return True


# --- Tokens de senha v2: finalidade, credencial e consumo atômico ---
_ATIVACAO = "activation"
_RECUPERACAO = "recovery"
RESET_MAX_AGE = 900  # 15 minutos, em segundos


@dataclass(frozen=True)
class _CredencialEsperada:
    """Snapshot para o UPDATE; nunca publicar ou registrar o hash interno."""
    id_usuario: int
    senha_hash: str = field(repr=False)
    role: str
    primeiro_acesso: bool
    geracao_autorizacao: int
    id_empresa: int | None
    usuario: Usuario = field(repr=False, compare=False)


def _estado_credencial(usuario: Usuario) -> _CredencialEsperada:
    return _CredencialEsperada(
        usuario.id_usuario, usuario.senha_hash, usuario.role,
        bool(usuario.primeiro_acesso), usuario.geracao_autorizacao,
        usuario.id_empresa, usuario,
    )


def _serializer(finalidade: str) -> URLSafeTimedSerializer:
    """Assina, sem criptografar. Namespaces v2 não aceitam links antigos."""
    if finalidade not in (_ATIVACAO, _RECUPERACAO):
        raise ValueError("Finalidade de token inválida.")
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"], salt=f"nexo-password-{finalidade}-v2",
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def _impressao_credencial(usuario) -> str:
    """HMAC padrão; o token não contém senha, e-mail ou hash de senha."""
    chave = current_app.config["SECRET_KEY"]
    if isinstance(chave, str):
        chave = chave.encode("utf-8")
    mensagem = b"nexo-password-credential-v2\0" + (
        f"{usuario.id_usuario}\0{usuario.senha_hash}".encode("utf-8")
    )
    mensagem = vincular_geracao_autorizacao(mensagem, usuario.geracao_autorizacao)
    return hmac.new(chave, mensagem, hashlib.sha256).hexdigest()


def _permite_finalidade(usuario, finalidade: str) -> bool:
    if usuario.role not in ("ADMIN", "CLIENTE"):
        return False
    pendente = usuario.role == "CLIENTE" and bool(usuario.primeiro_acesso)
    if finalidade == _ATIVACAO:
        return pendente
    # ADMIN pode conservar a flag legada sem precisar de ativação de cliente.
    return finalidade == _RECUPERACAO and not pendente


def _emitir_token(usuario: Usuario, finalidade: str) -> str:
    if (type(usuario.id_usuario) is not int or usuario.id_usuario <= 0
            or not isinstance(usuario.senha_hash, str) or not usuario.senha_hash):
        raise ValueError("Usuário persistido com credencial é obrigatório.")
    if not usuario_pode_acessar(usuario) or not _permite_finalidade(usuario, finalidade):
        raise ValueError("Conta não elegível para esta operação de senha.")
    return _serializer(finalidade).dumps({
        "v": 2, "uid": usuario.id_usuario, "purpose": finalidade,
        "credential": _impressao_credencial(usuario),
    })


def gerar_token_definir_senha(usuario: Usuario) -> str:
    """Emite exclusivamente ativação para CLIENTE persistido e pendente."""
    return _emitir_token(usuario, _ATIVACAO)


def gerar_token_recuperacao_senha(usuario: Usuario) -> str:
    """Emite recuperação para conta ativa que não aguarda ativação de cliente."""
    return _emitir_token(usuario, _RECUPERACAO)


def _falha_banco_senha(erro: SQLAlchemyError) -> None:
    db.session.rollback()
    # Não registrar SQL/params, tokens, hashes, senhas ou dados do usuário.
    logger.warning("Operação de senha indisponível (%s).", type(erro).__name__)


def _ler_token_senha(token: str, finalidade: str):
    if not isinstance(token, str) or not token or len(token) > 2048:
        return None
    try:
        dados = _serializer(finalidade).loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadData):
        return None
    if not isinstance(dados, dict) or set(dados) != {"v", "uid", "purpose", "credential"}:
        return None
    if (type(dados["v"]) is not int or dados["v"] != 2
            or type(dados["uid"]) is not int or dados["uid"] <= 0
            or dados["purpose"] != finalidade):
        return None
    impressao = dados["credential"]
    if (not isinstance(impressao, str) or len(impressao) != 64
            or any(c not in "0123456789abcdef" for c in impressao)):
        return None
    try:
        usuario = db.session.get(Usuario, dados["uid"], populate_existing=True)
        if (not usuario_pode_acessar(usuario)
                or not _permite_finalidade(usuario, finalidade)):
            return None
    except SQLAlchemyError as erro:
        _falha_banco_senha(erro)
        return None
    if not hmac.compare_digest(impressao, _impressao_credencial(usuario)):
        return None
    return _estado_credencial(usuario)


def _ler_token_reset(token: str):
    return _ler_token_senha(token, _RECUPERACAO)


def _ler_token_ativacao(token: str):
    return _ler_token_senha(token, _ATIVACAO)


def _conta_segue_acessivel():
    """Condição dos CAS de credencial: ADMIN sem empresa ou CLIENTE de empresa acessível."""
    return or_(
        and_(Usuario.role == "ADMIN", Usuario.id_empresa.is_(None)),
        and_(Usuario.role == "CLIENTE", select(Empresa.id_empresa).where(
            Empresa.id_empresa == Usuario.id_empresa,
            *condicoes_empresa_acessivel(),
        ).exists()),
    )


def _atualizar_credencial(estado: _CredencialEsperada, nova_senha: str, *, renovar_sessao=False) -> bool:
    """CAS transacional: só uma requisição substitui a credencial esperada.

    O hash salgado muda mesmo ao escolher a mesma senha. No PostgreSQL em
    READ COMMITTED, um concorrente espera o lock e reavalia o WHERE: zero
    linhas após o primeiro commit. Rollback não consome o token.
    """
    novo_hash = generate_password_hash(nova_senha)
    comando = update(Usuario).where(
        Usuario.id_usuario == estado.id_usuario,
        Usuario.senha_hash == estado.senha_hash,
        Usuario.geracao_autorizacao == estado.geracao_autorizacao,
        Usuario.id_empresa == estado.id_empresa,
        Usuario.ativo.is_(True),
        _conta_segue_acessivel(),
        Usuario.role == estado.role,
        Usuario.primeiro_acesso.is_(estado.primeiro_acesso),
    ).values(
        senha_hash=novo_hash, primeiro_acesso=False,
        data_atualizacao=datetime.now(timezone.utc),
    ).execution_options(synchronize_session=False)
    try:
        # Nunca alterar o objeto ORM antes do CAS: autoflush quebraria a condição.
        with db.session.no_autoflush:
            resultado = db.session.execute(comando)
        if resultado.rowcount != 1:
            db.session.rollback()
            return False
        # SEC-001: revogação global. Toda sessão registrada do usuário cai
        # junto com a senha, na mesma transação do CAS; a sessão renovada do
        # primeiro acesso nasce nessa mesma transação (um único COMMIT).
        revogar_sessoes_usuario(db.session, estado.id_usuario, "troca_senha")
        renovar_cliente = (
            renovar_sessao and has_request_context()
            and session.get("_user_id") == str(estado.id_usuario)
            and session.get("boot_id") == current_app.config["BOOT_ID"]
            and mesma_impressao(impressao_sessao(estado), session.get(CHAVE_CREDENCIAL))
        )
        token_renovado = registrar_sessao(db.session, estado.id_usuario)[0] if renovar_cliente else None
        db.session.commit()
    except SQLAlchemyError as erro:
        _falha_banco_senha(erro)
        return False

    # Usar hash e geração que esta operação confirmou, nunca refresh de uma
    # senha ou exclusão/restauração posterior entre commit e renovação.
    impressao_atual = impressao_credencial_sessao(
        estado.id_usuario, novo_hash, estado.geracao_autorizacao,
    )
    if token_renovado is not None:
        carimbar_sessao(token_renovado, impressao_atual)
    try:
        from realtime import revogar_conexoes_usuario
        revogar_conexoes_usuario(
            estado.id_usuario, impressao_atual,
            impressao_revogada=impressao_sessao(estado),
        )
    except Exception as erro:
        # Senha já confirmada: falha de transporte não muda seu resultado.
        # Salas por geração continuam negando dados aos sockets antigos.
        logger.warning("Revogação Socket.IO indisponível (%s).", type(erro).__name__)
    return True


_HASH_FICTICIO = None


def _conferir_senha_ficticia(senha: str) -> None:
    """E-mail inexistente custa o mesmo hash de uma conta real (anti-enumeração por tempo)."""
    global _HASH_FICTICIO
    if _HASH_FICTICIO is None:
        _HASH_FICTICIO = generate_password_hash("nexo-login-sem-conta")
    check_password_hash(_HASH_FICTICIO, senha)


def _conta_encerrada(usuario) -> bool:
    """Senha correta de CLIENTE ativo cuja empresa perdeu o vínculo contratual.

    Só depois de a senha ser validada; usuário inativo e empresa na lixeira
    continuam com a mensagem genérica. Falha de banco falha fechado.
    """
    if not usuario.ativo or usuario.role != "CLIENTE" or usuario.id_empresa is None:
        return False
    try:
        with db.session.no_autoflush:
            empresa = db.session.scalar(select(Empresa).where(
                Empresa.id_empresa == usuario.id_empresa,
            ).execution_options(populate_existing=True))
        return empresa is not None and obter_situacao_contratual(
            empresa, carregar_coberturas=False,
        ).conta_encerrada
    except SQLAlchemyError as erro:
        db.session.rollback()
        logger.warning("Situação contratual indisponível no login (%s).", type(erro).__name__)
        return False


def _validar_nova_senha(nova: str, confirma: str, estado: _CredencialEsperada | None = None, **regras):
    """Política de senha (politica_senha.py) aplicada à credencial que será
    substituída: a senha não pode conter o e-mail/nome nem repetir a atual."""
    usuario = estado.usuario if estado is not None else None
    return validar_nova_senha(
        nova, confirma,
        email=getattr(usuario, "email", None), nome=getattr(usuario, "nome", None),
        hash_atual=estado.senha_hash if estado is not None else None, **regras,
    )


def _senha_atual_confere(estado: _CredencialEsperada, senha) -> bool:
    # Sem teto, um formulário de 20 MB viraria trabalho de hash a cada tentativa.
    return isinstance(senha, str) and len(senha) <= 1024 and check_password_hash(estado.senha_hash, senha)


def trocar_senha_autenticada(usuario: Usuario, senha_atual, nova, confirmacao) -> str | None:
    """Troca feita pelo próprio dono, logado. Devolve a mensagem de erro ou None.

    A senha atual reautentica (um cookie sozinho não troca a credencial) e o CAS de
    _atualizar_credencial derruba as demais sessões e renova esta na mesma transação.
    """
    estado = _estado_credencial(usuario)
    if not _senha_atual_confere(estado, senha_atual):
        return "A senha atual está incorreta."
    erro = _validar_nova_senha(nova or "", confirmacao or "", estado, exigir_letras_e_numeros=True)
    if erro:
        return erro
    if not _atualizar_credencial(estado, nova, renovar_sessao=True):
        return "Não foi possível alterar a senha. Atualize a página e tente novamente."
    logger.info("Senha alterada nas Configurações | usuario=%s", estado.id_usuario)
    # Senha já trocada: falha no aviso nunca vira erro da operação.
    try:
        email_senha_alterada(estado.usuario, url_publica("auth.recuperar_senha"), nas_configuracoes=True)
    except Exception as erro_aviso:  # noqa: BLE001
        logger.warning("Aviso de senha alterada não preparado (%s).", type(erro_aviso).__name__)
    return None


_EMAIL_INDISPONIVEL = "Este e-mail não pode ser usado. Informe outro."


def _recusa_novo_email(novo: str, email_anterior: str, id_usuario: int) -> str | None:
    if not email_valido(novo):
        return "Informe um e-mail válido."
    if novo == normalizar_email(email_anterior):
        return "Este já é o e-mail de acesso da conta."
    try:
        em_uso = email_em_uso(db.session, novo, exceto_id=id_usuario)
    except SQLAlchemyError as erro:
        _falha_banco_senha(erro)
        return "Não foi possível alterar o e-mail. Tente novamente."
    return _EMAIL_INDISPONIVEL if em_uso else None


def _gravar_novo_email(estado: _CredencialEsperada, email_anterior: str, novo: str, *, renovar_sessao: bool,
                       novo_hash: str | None = None) -> str | None:
    """CAS da troca de e-mail. Devolve a mensagem de erro ou None.

    O e-mail é por onde a senha se recupera, então a troca vale como troca de
    credencial: a geração de autorização sobe na mesma transação. Com isso as sessões
    abertas e os links de senha já enviados ao endereço antigo deixam de valer.
    """
    proxima_geracao = estado.geracao_autorizacao + 1
    hash_final = novo_hash or estado.senha_hash
    comando = update(Usuario).where(
        Usuario.id_usuario == estado.id_usuario,
        Usuario.email == email_anterior,
        Usuario.senha_hash == estado.senha_hash,
        Usuario.geracao_autorizacao == estado.geracao_autorizacao,
        Usuario.ativo.is_(True),
        _conta_segue_acessivel(),
    ).values(
        email=novo, senha_hash=hash_final, geracao_autorizacao=proxima_geracao,
        data_atualizacao=datetime.now(timezone.utc),
    ).execution_options(synchronize_session=False)
    token_renovado = None
    try:
        with db.session.no_autoflush:
            resultado = db.session.execute(comando)
        if resultado.rowcount != 1:
            db.session.rollback()
            return "Não foi possível alterar o e-mail. Atualize a página e tente novamente."
        revogar_sessoes_usuario(db.session, estado.id_usuario, "revogacao_global")
        if renovar_sessao:
            token_renovado = registrar_sessao(db.session, estado.id_usuario)[0]
        db.session.commit()
    except IntegrityError:
        # Outra conta tomou o endereço entre a conferência e o UPDATE.
        db.session.rollback()
        return _EMAIL_INDISPONIVEL
    except SQLAlchemyError as erro:
        _falha_banco_senha(erro)
        return "Não foi possível alterar o e-mail. Tente novamente."

    impressao_atual = impressao_credencial_sessao(estado.id_usuario, hash_final, proxima_geracao)
    if token_renovado is not None:
        carimbar_sessao(token_renovado, impressao_atual)
    try:
        from realtime import revogar_conexoes_usuario
        revogar_conexoes_usuario(estado.id_usuario, impressao_atual,
                                 impressao_revogada=impressao_sessao(estado))
    except Exception as erro:  # noqa: BLE001
        # E-mail já confirmado: as salas por geração negam dados aos sockets antigos.
        logger.warning("Revogação Socket.IO indisponível (%s).", type(erro).__name__)
    return None


def trocar_email_autenticado(usuario: Usuario, senha_atual, novo_email, confirmacao) -> str | None:
    """Troca do e-mail de acesso pelo próprio dono. Devolve a mensagem de erro ou None.

    Esta sessão continua, renovada, entrando pelo novo endereço. O aviso vai para o
    endereço antigo, o único que pode denunciar uma troca que o dono não fez.
    """
    estado = _estado_credencial(usuario)
    if not _senha_atual_confere(estado, senha_atual):
        return "A senha atual está incorreta."
    email_anterior = usuario.email
    # Lidos antes do commit: o aviso não pode depender do banco, que pode cair logo depois da gravação.
    dono = SimpleNamespace(id_usuario=estado.id_usuario, nome=usuario.nome)
    novo = normalizar_email(novo_email)
    # Sem confirmação por link, um erro de digitação entregaria a recuperação da conta a outra pessoa.
    if novo != normalizar_email(confirmacao):
        return "A confirmação não é igual ao novo e-mail."
    erro = _recusa_novo_email(novo, email_anterior, estado.id_usuario) or _gravar_novo_email(
        estado, email_anterior, novo, renovar_sessao=True,
    )
    if erro:
        return erro
    logger.info("E-mail de acesso alterado nas Configurações | usuario=%s", estado.id_usuario)
    try:
        email_email_alterado(dono, email_anterior, novo)
    except Exception as erro_aviso:  # noqa: BLE001
        logger.warning("Aviso de e-mail alterado não preparado (%s).", type(erro_aviso).__name__)
    return None


def corrigir_email_pelo_admin(usuario: Usuario, novo_email, confirmacao) -> tuple:
    """Suporte: devolve a conta ao dono depois de um e-mail errado ou de uma conta tomada.

    Além do e-mail, invalida a senha atual: quem a conhece não entra mais, e o dono
    define outra pelo link enviado ao endereço corrigido. O endereço anterior recebe um
    aviso sem link: se o suporte tiver sido enganado, é ele quem pode denunciar. Devolve
    (erro, envio); o envio é None se o link não pôde ser preparado depois de a correção
    já estar gravada.
    """
    estado = _estado_credencial(usuario)
    email_anterior = usuario.email
    # Lidos antes do commit: o aviso não pode depender do banco, que pode cair logo depois da gravação.
    dono = SimpleNamespace(id_usuario=estado.id_usuario, nome=usuario.nome)
    novo = normalizar_email(novo_email)
    # O link sai na hora para o endereço digitado: um erro de digitação entregaria a conta a outra pessoa.
    if novo != normalizar_email(confirmacao):
        return "A confirmação não é igual ao novo e-mail.", None
    erro = _recusa_novo_email(novo, email_anterior, estado.id_usuario) or _gravar_novo_email(
        estado, email_anterior, novo, renovar_sessao=False,
        # Hash de um segredo descartado: nenhuma senha confere até o dono definir outra.
        novo_hash=generate_password_hash(secrets.token_urlsafe(32)),
    )
    if erro:
        return erro, None
    envio = None
    try:
        conta = db.session.get(Usuario, estado.id_usuario, populate_existing=True)
        if _permite_finalidade(conta, _ATIVACAO):
            link = url_publica("auth.ativar_acesso", token=gerar_token_definir_senha(conta))
            envio = email_boas_vindas(conta, link, sincrono=True)
        else:
            link = url_publica("auth.resetar_senha", token=gerar_token_recuperacao_senha(conta))
            envio = email_recuperacao_senha(conta, link, pela_equipe=True)
    except Exception as erro_link:  # noqa: BLE001
        db.session.rollback()
        logger.warning("Link de senha não preparado depois da correção de e-mail (%s).", type(erro_link).__name__)
    try:
        email_email_alterado(dono, email_anterior, novo, pela_equipe=True, convite=estado.primeiro_acesso)
    except Exception as erro_aviso:  # noqa: BLE001
        db.session.rollback()
        logger.warning("Aviso de e-mail alterado não preparado (%s).", type(erro_aviso).__name__)
    return None, envio


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])  # trava brute force de senha
# Por conta alvo (hash do e-mail), independentemente do IP de origem. Orçamento
# em config (LIMITE_LOGIN_CONTA): acima do teto por IP × janela, para que um só
# IP não tranque o e-mail alvo; lido a cada requisição (ajustável sem reinício).
@limiter.limit(lambda: current_app.config["LIMITE_LOGIN_CONTA"], methods=["POST"], key_func=chave_limite_conta)
def login():
    """Gere o ecrã e a lógica de login do utilizador."""
    # Se já estiver autenticado, vai DIRETO ao dashboard do perfil correto.
    # (Antes redirecionava para "index", mas "index" agora serve a landing
    # institucional — voltar pra lá seria UX hostil pra quem já está logado.)
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("cliente.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        senha = request.form.get("senha", "")

        # Procura o utilizador pelo e-mail: texto exato e, se não houver, a
        # única conta com o mesmo e-mail sem diferenciar maiúsculas
        # (cadastros antigos guardavam o endereço como foi digitado).
        busca = buscar_usuario_por_email(db.session, email)
        if busca.ambigua:
            logger.warning("Login sem conta escolhida: mais de um cadastro tem este e-mail com maiúsculas "
                           "diferentes (resolver o cadastro duplicado).")
        usuario = busca.usuario

        # Valida o utilizador e a senha (usando o método check_senha definido no Model)
        if usuario and usuario.check_senha(senha):
            # Capturar a credencial cuja senha foi verificada antes de criar
            # a sessão; nunca adotar um hash posterior por refresh do ORM.
            impressao_login = impressao_sessao(usuario)
            if not usuario_pode_acessar(usuario):
                if _conta_encerrada(usuario):
                    # Página própria, sem login_user, flash ou redirect: nenhuma
                    # sessão autenticada nem dado do portal é entregue.
                    resposta = make_response(render_template("auth/conta_encerrada.html"))
                    resposta.headers["Cache-Control"] = "no-store"
                    return resposta
                flash("Esta conta está desativada. Contacte o administrador.", "danger")
                return render_template("auth/login.html", email_digitado=email)

            # Efetua o login na sessão (sem "remember": cookie morre ao fechar o
            # navegador). Carimba a sessão com o BOOT_ID atual para que um
            # restart do servidor invalide o login automaticamente.
            # SEC-001: a sessão anônima é descartada (anti-fixação) e o login
            # só vale com a sessão registrada no banco (fail-closed).
            session.clear()
            login_user(usuario, remember=False)
            try:
                token_sessao = registrar_sessao(db.session, usuario.id_usuario)[0]
                usuario.ultimo_acesso = datetime.now(timezone.utc)
                db.session.commit()
            except SQLAlchemyError as erro:
                db.session.rollback()
                logout_user()
                session.clear()
                logger.warning("Registro de sessão indisponível (%s).", type(erro).__name__)
                resposta = make_response(render_template(
                    "erro_simples.html", titulo="Serviço indisponível", codigo=503,
                    mensagem="Não foi possível iniciar a sessão. Tente novamente em instantes.",
                ), 503)
                resposta.headers["Cache-Control"] = "no-store"
                return resposta
            carimbar_sessao(token_sessao, impressao_login)

            # Trata o redirecionamento caso o utilizador tenha tentado aceder a uma
            # página protegida antes. Só aceitamos destinos LOCAIS (anti open redirect).
            next_page = request.args.get("next")
            if _destino_seguro(next_page) and _destino_abre_por_get(next_page):
                return redirect(next_page)

            # Redirecionamento padrão com base no perfil (Role)
            if usuario.is_admin:
                return redirect(url_for("admin.dashboard"))
            return redirect(url_for("cliente.dashboard"))
        
        if usuario is None:
            _conferir_senha_ficticia(senha)
        # Mensagem genérica por motivos de segurança (não revelar se o erro foi o e-mail ou a senha)
        flash("E-mail ou senha incorretos.", "danger")

    # Depois de uma tentativa errada, o e-mail digitado volta preenchido (só a senha é redigitada).
    return render_template("auth/login.html", email_digitado=request.form.get("email", "").strip())


@auth_bp.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    """Termina a sessão ATUAL do utilizador (POST + CSRF).

    SEC-002: GET apenas mostra a confirmação; nenhum link, imagem ou prefetch
    encerra a sessão. SEC-001: a linha registrada desta sessão é revogada no
    banco antes de o cookie ser descartado, de modo que uma cópia do cookie
    deixa de autorizar HTTP e Socket.IO em qualquer processo. As demais
    sessões do mesmo usuário (outros navegadores) continuam válidas.
    """
    if request.method == "GET":
        resposta = make_response(render_template("auth/logout.html"))
        resposta.headers["Cache-Control"] = "no-store"
        return resposta
    hash_atual = hash_token_sessao(session.get(CHAVE_SESSAO))
    try:
        # FOR UPDATE no usuário: entregas em andamento (FOR SHARE) terminam
        # antes do COMMIT e as seguintes já veem a sessão revogada.
        usuario = carregar_usuario(current_user.id_usuario, escrita=True)
        if usuario is not None and hash_atual is not None:
            revogar_sessao_por_hash(db.session, usuario.id_usuario, hash_atual, "logout")
        db.session.commit()
    except SQLAlchemyError as erro:
        # Não afirmar que a sessão terminou quando o servidor não a revogou.
        db.session.rollback()
        logger.warning("Logout indisponível (%s).", type(erro).__name__)
        resposta = make_response(render_template(
            "erro_simples.html", titulo="Serviço indisponível", codigo=503,
            mensagem="Não foi possível encerrar a sessão no servidor. Tente novamente em instantes.",
        ), 503)
        resposta.headers["Cache-Control"] = "no-store"
        return resposta
    logout_user()
    session.clear()
    try:
        from realtime import revogar_conexoes_sessao
        revogar_conexoes_sessao(hash_atual)
    except Exception as erro:
        # Já revogada no banco: a varredura e as salas por sessão completam.
        logger.warning("Desconexão Socket.IO no logout indisponível (%s).", type(erro).__name__)
    flash("Sessão terminada com sucesso.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/primeiro-acesso", methods=["GET", "POST"])
@login_required
def primeiro_acesso():
    """
    Troca obrigatória de senha no primeiro acesso do cliente.
    O guard em app.py manda o cliente para cá enquanto primeiro_acesso=True.
    Ao salvar, baixa a flag e libera o portal.
    """
    # Só CLIENTE com senha provisória. Aqui a senha atual não é pedida: um ADMIN com a
    # flag antiga trocaria a credencial só com o cookie, sem aviso nenhum.
    if not (current_user.is_cliente and getattr(current_user, "primeiro_acesso", False)):
        destino = "admin.dashboard" if current_user.is_admin else "cliente.dashboard"
        return redirect(url_for(destino))

    if request.method == "POST":
        nova = request.form.get("nova_senha", "")
        confirma = request.form.get("confirma_senha", "")
        estado = _estado_credencial(current_user)
        erro = _validar_nova_senha(nova, confirma, estado)
        if erro:
            flash(erro, "danger")
            return render_template("auth/primeiro_acesso.html")

        if not _atualizar_credencial(estado, nova, renovar_sessao=True):
            flash("Não foi possível definir a senha. Faça login ou solicite um novo link.", "danger")
            return redirect(url_for("auth.login"))
        flash("Senha definida com sucesso. Bem-vindo(a) ao NEXO!", "success")
        return redirect(url_for("cliente.dashboard"))

    return render_template("auth/primeiro_acesso.html")


@auth_bp.route("/recuperar-senha", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])  # evita abuso do envio de e-mail
# Por conta alvo (LIMITE_RECUPERACAO_CONTA): limita o envio de e-mails a um alvo a
# partir de vários IPs sem permitir que poucos pedidos bloqueiem a autorrecuperação.
@limiter.limit(lambda: current_app.config["LIMITE_RECUPERACAO_CONTA"], methods=["POST"], key_func=chave_limite_conta)
def recuperar_senha():
    """
    Solicita recuperação ou reenvio de ativação, conforme o estado da conta.
    A resposta é genérica mesmo em DEBUG e não expõe o link ao solicitante.
    """
    if request.method == "POST":
        if not envio_disponivel(current_app.config):
            # Envio de e-mail impossível no sistema todo (sem transporte ou com
            # pendência que bloqueia qualquer envio): o estado não depende da
            # conta (mesma resposta para qualquer endereço, sem consulta ao
            # banco), então dizê-lo não revela cadastros, e prometer um link
            # que nunca sai seria uma confirmação falsa.
            logger.warning("Recuperação de senha sem envio: a configuração atual não permite enviar e-mails "
                           "(veja as pendências no Diagnóstico de e-mail).")
            registrar_envio_suprimido("recuperacao_senha")  # histórico e aviso aos admins
            flash("No momento não é possível enviar o link de redefinição por e-mail. Tente novamente mais "
                  "tarde ou fale com o suporte NEXO.", "warning")
            return redirect(url_for("auth.recuperar_senha"))
        email = request.form.get("email", "")
        try:
            # Texto exato e, se não houver, a única conta com o mesmo e-mail sem
            # diferenciar maiúsculas: "Cliente@Empresa.com" cadastrado e
            # "cliente@empresa.com" digitado agora encontram a mesma conta.
            busca = buscar_usuario_por_email(db.session, email)
            usuario = busca.usuario
            if busca.ambigua:
                logger.warning("Recuperação de senha sem envio: mais de um cadastro tem este e-mail com "
                               "maiúsculas diferentes (resolver o cadastro duplicado).")
            elif usuario is None:
                logger.info("Recuperação de senha sem envio: nenhuma conta com o e-mail informado.")
            elif not usuario_pode_acessar(usuario):
                logger.info("Recuperação de senha sem envio: conta não elegível (usuário inativo ou empresa "
                            "cancelada, na lixeira ou sem vínculo contratual) | usuario=%s", usuario.id_usuario)
            else:
                # SEC-005: o link sai da origem canônica configurada, nunca do
                # cabeçalho Host recebido (um Host forjado levaria o token ao
                # domínio do atacante).
                if _permite_finalidade(usuario, _ATIVACAO):
                    token = gerar_token_definir_senha(usuario)
                    link = url_publica("auth.ativar_acesso", token=token)
                    envio = email_boas_vindas(usuario, link)
                else:
                    token = gerar_token_recuperacao_senha(usuario)
                    link = url_publica("auth.resetar_senha", token=token)
                    envio = email_recuperacao_senha(usuario, link)
                # O resultado detalhado (agendado, aceito pelo provedor ou falha
                # com a categoria) sai na linha de log de emails.py com a mesma
                # correlação; a resposta pública continua genérica.
                logger.info("Solicitação de recuperação de senha processada | usuario=%s estado=%s correlacao=%s",
                            usuario.id_usuario, getattr(envio, "estado", None) or "-",
                            getattr(envio, "correlacao", None) or "-")
        except SQLAlchemyError as erro:
            _falha_banco_senha(erro)
        except Exception as erro:  # noqa: BLE001 — nunca um 500 que revele a existência da conta
            try:
                db.session.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("Solicitação de recuperação de senha não processada (%s).", type(erro).__name__)

        # Mensagem genérica sempre (anti-enumeração de e-mails).
        # Futuro, não passado: o envio sai em segundo plano e, neste instante,
        # está só agendado (o desfecho fica no log e no Diagnóstico de e-mail).
        flash("Se o e-mail estiver cadastrado, enviaremos um link de redefinição válido por 15 minutos. "
              "Confira também a caixa de spam.", "success")
        return redirect(url_for("auth.recuperar_senha"))

    return render_template("auth/recuperar_senha.html")


@auth_bp.route("/resetar-senha/<token>", methods=["GET", "POST"])
def resetar_senha(token):
    """
    Valida o token (assinatura + expiração de 15 min). Token adulterado ou
    expirado → 'Token inválido ou expirado'. Válido → permite definir nova senha.
    """
    estado = _ler_token_reset(token)
    if estado is None:
        flash("Token inválido ou expirado. Solicite um novo link de redefinição.", "danger")
        return redirect(url_for("auth.recuperar_senha"))

    if request.method == "POST":
        nova = request.form.get("nova_senha", "")
        confirma = request.form.get("confirma_senha", "")
        erro = _validar_nova_senha(nova, confirma, estado)
        if erro:
            flash(erro, "danger")
            return render_template("auth/resetar_senha.html", token=token)

        if not _atualizar_credencial(estado, nova):
            flash("Token inválido ou expirado. Solicite um novo link de redefinição.", "danger")
            return redirect(url_for("auth.recuperar_senha"))
        # Aviso ao dono da conta (sem link de token): uma redefinição que ele
        # não fez vira sinal de comprometimento, com o caminho para reagir.
        # A senha já foi trocada: falha no aviso nunca vira erro 500.
        try:
            email_senha_alterada(estado.usuario, url_publica("auth.recuperar_senha"))
        except Exception as erro:  # noqa: BLE001
            logger.warning("Aviso de senha alterada não preparado (%s).", type(erro).__name__)
        flash("Senha redefinida com sucesso. Faça login com a nova senha.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/resetar_senha.html", token=token)


@auth_bp.route("/primeiro-acesso/<token>", methods=["GET", "POST"])
def ativar_acesso(token):
    """
    Ativação de conta via link do e-mail de boas-vindas (anônimo). Valida o token
    (15 min). Expirado/adulterado → tela amigável para solicitar novo link
    (recuperar-senha). Válido → o cliente define a senha e a conta é ativada.
    """
    estado = _ler_token_ativacao(token)
    if estado is None:
        flash(
            "Seu link de definição de senha é inválido ou expirou (validade de 15 minutos). "
            "Informe seu e-mail abaixo para receber um novo link.", "danger",
        )
        return redirect(url_for("auth.recuperar_senha"))

    usuario = estado.usuario

    if request.method == "POST":
        nova = request.form.get("nova_senha", "")
        confirma = request.form.get("confirma_senha", "")
        erro = _validar_nova_senha(nova, confirma, estado)
        if erro:
            flash(erro, "danger")
            return render_template("auth/primeiro_acesso_token.html", token=token, usuario=usuario)

        if not _atualizar_credencial(estado, nova):
            flash("Seu link de definição de senha é inválido ou expirou. Solicite um novo link.", "danger")
            return redirect(url_for("auth.recuperar_senha"))
        flash("Senha definida com sucesso! Já pode acessar o portal.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/primeiro_acesso_token.html", token=token, usuario=usuario)
