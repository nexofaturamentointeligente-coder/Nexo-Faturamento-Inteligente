"""
NEXO - Faturamento Inteligente | E-mails transacionais (Brevo API / SMTP)
=========================================================================
Envio de e-mails HTML disparados por eventos de negócio: ativação de conta
(boas-vindas), recuperação de senha, aviso de senha e de e-mail alterados,
análise publicada, chamado resolvido, cópia de novo chamado e lembrete de
renovação. Princípios:

  - DOIS TRANSPORTES: sem BREVO_API_KEY, o envio sai pelo SMTP do Flask-Mail
    (MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD: Gmail, Zoho, cPanel
    ou servidor próprio); com ela, opcional, sai pela API HTTPS do Brevo (porta
    443, para redes e hospedagens que bloqueiam as portas SMTP 25/465/587).
  - ESTADOS DISTINTOS: cada solicitação devolve um EnvioEmail cujo `estado` é
    DESATIVADO (sem transporte configurado), SEM_DESTINATARIO, AGENDADO (thread
    iniciada, resultado ainda desconhecido), ACEITO_PELO_PROVEDOR (a API/SMTP
    aceitou a mensagem — isso NÃO comprova a chegada à caixa de entrada) ou
    FALHA_NO_ENVIO (com `categoria`: credencial recusada, IP não autorizado,
    remetente recusado, rede, prazo esgotado...). Agendar nunca é tratado como
    enviar.
  - FAIL-SAFE: nenhuma falha de e-mail derruba ou desfaz o fluxo que a
    disparou (cadastro, publicação, chamado, troca de senha).
  - LOGS SEM DADOS PRIVADOS: uma linha estruturada por desfecho (tipo,
    transporte, estado, categoria, HTTP/código do provedor, correlação,
    instante). Nunca são registrados destinatário, assunto, corpo, link,
    token, senha, chave de API nem a mensagem de erro devolvida pelo provedor.
  - VISÍVEL AO ADMIN: histórico recente em memória (Admin → Diagnóstico de
    e-mail) e, em produção, aviso no sininho dos administradores quando o
    provedor não aceita um e-mail.

Modos de envio:
  - assíncrono (padrão): thread em segundo plano — usado nos fluxos públicos
    (recuperação de senha), onde o tempo de resposta não pode depender do
    provedor nem revelar se a conta existe;
  - síncrono (`sincrono=True`): usado em ações do administrador (cadastro de
    empresa, reenvio de ativação, teste), que precisam mostrar na hora se o
    provedor aceitou a mensagem. Cada tentativa tem prazo (NEXO_EMAIL_TIMEOUT).

Threads em andamento quando o processo encerra (deploy ou reinício) são
aguardadas por um prazo limitado; as que não terminarem são registradas em log
como interrompidas (resultado desconhecido). Uma linha estado=AGENDADO sem a
linha final da mesma correlação também indica envio interrompido.

Guia operacional: Docs/EMAILS_TRANSACIONAIS.md.
"""

import atexit
import http.client
import json
import logging
import os
import re
import secrets
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from email.utils import parseaddr
from enum import StrEnum
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, urlunsplit

from flask import current_app, has_app_context, render_template
from flask_mail import Message

from extensions import ValorSMTPInvalido, mail

logger = logging.getLogger(__name__)


class EstadoEnvio(StrEnum):
    """Estado operacional de uma solicitação de e-mail (nunca "entregue")."""
    DESATIVADO = "DESATIVADO"
    SEM_DESTINATARIO = "SEM_DESTINATARIO"
    AGENDADO = "AGENDADO"
    ACEITO_PELO_PROVEDOR = "ACEITO_PELO_PROVEDOR"
    FALHA_NO_ENVIO = "FALHA_NO_ENVIO"


ESTADOS_FINAIS = frozenset({
    EstadoEnvio.DESATIVADO, EstadoEnvio.SEM_DESTINATARIO,
    EstadoEnvio.ACEITO_PELO_PROVEDOR, EstadoEnvio.FALHA_NO_ENVIO,
})

TIPOS = {
    "boas_vindas": "ativação de conta (boas-vindas)",
    "recuperacao_senha": "recuperação de senha",
    "senha_alterada": "aviso de senha alterada",
    "email_alterado": "aviso de e-mail de acesso alterado",
    "novo_chamado": "cópia de novo chamado",
    "lembrete_renovacao": "lembrete de renovação do contrato",
    "analise_publicada": "análise publicada",
    "ticket_resolvido": "chamado resolvido",
    "teste": "teste de configuração",
    "generico": "e-mail transacional",
}

TRANSPORTES = {
    "brevo": "API HTTPS do Brevo (porta 443)",
    "smtp": "SMTP (Flask-Mail)",
}

# Categoria -> explicação segura (sem valores de configuração) para o admin.
CATEGORIAS = {
    "transporte_desativado": ("o serviço de e-mail não está configurado (defina MAIL_USERNAME e MAIL_PASSWORD, ou "
                              "BREVO_API_KEY, no .env ou nas variáveis de ambiente do servidor)"),
    "smtp_nao_usado": "o envio está usando a API do Brevo, então a conexão SMTP não é usada",
    "smtp_suprimido": "o envio está suprimido neste ambiente (MAIL_SUPPRESS_SEND)",
    "envio_bloqueado": ("a configuração atual impede qualquer envio (veja as pendências que impedem o envio no "
                        "Diagnóstico de e-mail)"),
    "sem_destinatario": "não há destinatário elegível para esta mensagem",
    "destinatario_invalido": "o endereço de e-mail do destinatário é inválido",
    "remetente_invalido": "o remetente configurado em MAIL_DEFAULT_SENDER é inválido",
    "configuracao_invalida": "a configuração do provedor é inválida (confira BREVO_API_URL)",
    "template": "falha ao montar a mensagem",
    "agendamento": "falha ao iniciar o envio em segundo plano",
    "credencial_rejeitada": ("o Brevo recusou a chave de API (BREVO_API_KEY deve ser uma chave de API, "
                             "iniciada por xkeysib-, ativa e copiada por inteiro)"),
    "ip_nao_autorizado": ("o Brevo bloqueou o IP de saída do servidor (autorize-o em Brevo → Configurações → "
                          "Segurança → IPs autorizados)"),
    "conta_em_validacao": "a conta Brevo ainda está em validação e não pode enviar e-mails transacionais",
    "conta_sem_permissao": "a conta Brevo não tem permissão para envio transacional",
    "creditos_insuficientes": "a conta Brevo está sem créditos de envio ou aguarda ativação",
    "remetente_nao_autorizado": ("o Brevo recusou o remetente (valide o remetente em Remetentes e domínios "
                                 "ou autentique o domínio)"),
    "dominio_nao_autenticado": ("o Brevo exige um domínio de envio autenticado (política DMARC): autentique "
                                "um domínio próprio em Brevo → Remetentes, domínios e IPs → Domínios e use um "
                                "remetente desse domínio em MAIL_DEFAULT_SENDER"),
    "requisicao_rejeitada": "o Brevo rejeitou a requisição (parâmetros inválidos)",
    "limite_excedido": ("o limite de envios do provedor foi excedido (cota diária ou muitos envios em pouco "
                        "tempo); tente novamente mais tarde"),
    "provedor_indisponivel": "o provedor de e-mail está indisponível no momento",
    "gateway_sem_resposta": ("o gateway do provedor não respondeu (o aceite não foi confirmado; confira os "
                             "registros do provedor antes de reenviar)"),
    "conexao_interrompida": ("a conexão caiu depois de o pedido ser enviado (o aceite não foi confirmado; "
                             "confira os registros do provedor antes de reenviar)"),
    "tempo_esgotado": ("o provedor não respondeu dentro do prazo (o aceite não foi confirmado; confira os "
                       "registros do provedor antes de reenviar)"),
    "rede": "falha de rede ao contatar o provedor de e-mail",
    "conexao_sem_resposta": ("o provedor não aceitou a conexão dentro do prazo (o pedido não chegou a ser "
                             "enviado)"),
    "tls": "falha na verificação TLS da conexão com o provedor",
    "smtp_autenticacao": "o servidor SMTP recusou as credenciais (MAIL_USERNAME/MAIL_PASSWORD)",
    "smtp_senha_de_app": ("o Gmail exige uma senha de app: ative a verificação em duas etapas na conta Google "
                          "e use em MAIL_PASSWORD a senha de app de 16 caracteres, não a senha da conta"),
    "smtp_login_temporario": ("o servidor SMTP recusou o login temporariamente (muitas tentativas ou falha no servidor), sem dizer que a senha esteja errada; tente novamente mais tarde"),
    "smtp_remetente_recusado": ("o servidor SMTP recusou o remetente (MAIL_DEFAULT_SENDER precisa ser a conta de "
                                "MAIL_USERNAME ou um alias autorizado por ela)"),
    "smtp_destinatario_recusado": "o servidor SMTP recusou o destinatário",
    "smtp_conexao": ("não foi possível conectar ao servidor SMTP (confira MAIL_SERVER e MAIL_PORT; a rede ou a "
                     "hospedagem também pode bloquear as portas SMTP)"),
    "smtp_nao_suportado": ("o servidor SMTP não oferece STARTTLS ou login nesta porta (confira MAIL_PORT, "
                           "MAIL_USE_TLS e MAIL_USE_SSL)"),
    "smtp_erro": "o servidor SMTP devolveu um erro",
    "smtp_valor_invalido": ("MAIL_SERVER, MAIL_USERNAME ou MAIL_PASSWORD contém um caractere que o SMTP não aceita (letra acentuada, espaço especial colado junto da senha ou ponto duplicado no servidor): redigite o valor e reinicie o servidor"),
    "interrompido": "o processo do servidor encerrou antes do fim do envio (resultado desconhecido)",
    "desconhecida": "erro inesperado no envio",
}

# Tentativa extra só quando o provedor comprovadamente não processou o pedido:
# 429, 500/503 ou falha de conexão antes de a requisição ser enviada. 502/504
# vêm de um gateway que pode ter repassado o pedido: repetir duplicaria o e-mail.
_CATEGORIAS_TRANSITORIAS = frozenset({"limite_excedido", "provedor_indisponivel"})
# Desfecho desconhecido: o pedido pode ter sido aceito.
CATEGORIAS_RESULTADO_INCERTO = frozenset({"tempo_esgotado", "gateway_sem_resposta", "conexao_interrompida"})
_MAX_TENTATIVAS = 2
_ESPERA_NOVA_TENTATIVA = 1.5   # segundos (sem Retry-After utilizável)
_ESPERA_MAXIMA = 5.0
_LIMITE_CORPO_PROVEDOR = 4096  # bytes lidos da resposta, nunca registrados
_USER_AGENT = "NEXO-Faturamento-Inteligente/1.0 (e-mail transacional)"
_dormir = time.sleep  # pausa antes da nova tentativa (substituível em teste)

_EMAIL = re.compile(r"[^@\s<>()\[\],;:\"\\]+@[^@\s<>()\[\],;:\"\\]+\.[^@\s<>()\[\],;:\"\\.]+")
_CODIGO_PROVEDOR = re.compile(r"[a-z][a-z_]{0,47}")
# Códigos do Brevo em texto livre (enum oficial da resposta 400 de POST
# /v3/smtp/email). Só os desta lista entram no log, normalizados (ex.:
# dmarc_policy_requires_domain_authentication); outro texto do provedor serve
# apenas para classificar e nunca é registrado.
_CODIGOS_TEXTO_BREVO = frozenset({
    "api-key not found", "authentication failed", "insufficient credits",
    "dmarc policy requires domain authentication", "dns records not properly configured",
    "request already processed", "invalid parameters passed", "returned when invalid data posted",
    "domain does not exist",
})
# Status estendido da resposta SMTP (RFC 3463, ex.: 5.7.9): só no INÍCIO do
# texto (onde o RFC o coloca) e só os números — nunca dígitos de um IP ou de um
# endereço citado mais adiante na resposta.
_STATUS_SMTP_ESTENDIDO = re.compile(rb"\s*([245])(\.\d{1,3}\.\d{1,3})(?=\s|$)")
_ID_MENSAGEM = re.compile(r"<?[A-Za-z0-9._%+=@-]{1,200}>?")
_TIPO = re.compile(r"[a-z_]{1,32}")

# Domínios de e-mail gratuitos não podem ser autenticados no Brevo (SPF/DKIM do
# domínio): o remetente é reescrito e a entregabilidade tende a cair.
_DOMINIOS_GRATUITOS = frozenset({
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.com.br", "icloud.com", "me.com", "aol.com", "bol.com.br",
    "uol.com.br", "terra.com.br", "ig.com.br",
})


def email_valido(valor) -> bool:
    """Validação estrutural mínima (o provedor faz a definitiva). Só ASCII imprimível: caractere
    invisível imita outro endereço, e acento na parte local impede o envio por SMTP. O navegador
    já envia domínio com acento convertido para xn--."""
    return (isinstance(valor, str) and 3 <= len(valor) <= 254 and valor.isascii() and valor.isprintable()
            and bool(_EMAIL.fullmatch(valor)))


# ---------------------------------------------------------------------
# Resultado operacional e histórico recente (memória do processo)
# ---------------------------------------------------------------------
_TRAVA = threading.Lock()
_HISTORICO = deque(maxlen=50)
_EM_ANDAMENTO = {}            # correlação -> (thread, envio, prazo em s)
_ULTIMO_ALERTA = {}           # categoria -> time.monotonic() do último aviso ao admin
_INTERVALO_ALERTA = 30 * 60   # no máximo um aviso por categoria a cada 30 min
_CONEXAO_SMTP = None          # último desfecho da conexão SMTP (Diagnóstico de e-mail)
# Falhas que dizem algo sobre a conexão ou as credenciais; recusar um destinatário,
# por exemplo, acontece com a conexão funcionando e não muda esse status.
_CATEGORIAS_CONEXAO_SMTP = frozenset({"smtp_conexao", "smtp_autenticacao", "smtp_senha_de_app",
                                      "smtp_login_temporario", "smtp_nao_suportado", "tls",
                                      "smtp_valor_invalido"})


class EnvioEmail:
    """Uma solicitação de e-mail e o seu desfecho operacional.

    Guarda apenas metadados não sensíveis: nunca destinatário, assunto, corpo,
    link ou token. `bool(envio)` é True enquanto a solicitação não falhou
    (AGENDADO ou ACEITO_PELO_PROVEDOR) — compatível com o retorno booleano
    antigo; decisões novas devem consultar `estado`.
    """

    def __init__(self, tipo: str = "generico", *, referencia: str | None = None):
        self.tipo = tipo if isinstance(tipo, str) and _TIPO.fullmatch(tipo) else "generico"
        self.referencia = referencia
        self.correlacao = secrets.token_hex(6)
        self.transporte = None
        self.estado = None
        self.categoria = None
        self.http_status = None
        self.codigo_provedor = None
        self.erro = None              # nome da classe da exceção, nunca a mensagem
        self.id_mensagem = None       # messageId devolvido pelo Brevo (não sensível)
        self.eventos_provedor = None  # eventos de entrega consultados no Brevo (tipo e horário)
        self.eventos_consultados_em = None
        self.eventos_consulta_ok = None   # a última consulta foi concluída?
        self.tentativas = 0
        self.criado_em = datetime.now(timezone.utc)
        self.atualizado_em = self.criado_em

    def __bool__(self) -> bool:
        return self.estado in (EstadoEnvio.AGENDADO, EstadoEnvio.ACEITO_PELO_PROVEDOR)

    def __repr__(self) -> str:
        return f"<EnvioEmail {self.tipo} {self.estado} {self.correlacao}>"

    @property
    def aceito(self) -> bool:
        return self.estado == EstadoEnvio.ACEITO_PELO_PROVEDOR

    @property
    def rotulo_tipo(self) -> str:
        return TIPOS.get(self.tipo, TIPOS["generico"])

    @property
    def rotulo_transporte(self) -> str:
        return TRANSPORTES.get(self.transporte, "nenhum")

    @property
    def descricao_categoria(self) -> str | None:
        return CATEGORIAS.get(self.categoria) if self.categoria else None


_FRASES = {
    EstadoEnvio.AGENDADO: "agendado para envio em segundo plano",
    EstadoEnvio.ACEITO_PELO_PROVEDOR: ("aceito pelo transporte (aceite do provedor; não confirma a chegada "
                                       "à caixa de entrada)"),
    EstadoEnvio.FALHA_NO_ENVIO: "não enviado (falha)",
    EstadoEnvio.DESATIVADO: "suprimido: transporte de e-mail desativado",
    EstadoEnvio.SEM_DESTINATARIO: "suprimido: sem destinatário elegível",
}


def _mudar_estado(envio: EnvioEmail, estado: EstadoEnvio, **campos) -> None:
    with _TRAVA:
        for nome, valor in campos.items():
            setattr(envio, nome, valor)
        envio.estado = estado
        envio.atualizado_em = datetime.now(timezone.utc)
        if envio not in _HISTORICO:
            _HISTORICO.append(envio)
        if estado in ESTADOS_FINAIS:
            _EM_ANDAMENTO.pop(envio.correlacao, None)


def _registrar(envio: EnvioEmail, estado: EstadoEnvio, *, nivel: int = logging.INFO, **campos) -> None:
    """Muda o estado, atualiza o histórico e escreve UMA linha de log estruturada."""
    _mudar_estado(envio, estado, **campos)
    _escrever_log(envio, estado, nivel)


def _argumentos_log(envio: EnvioEmail, estado: EstadoEnvio) -> tuple:
    """Linha estruturada com os campos do instante (cópia, não referência)."""
    return (
        "E-mail transacional %s | tipo=%s transporte=%s estado=%s categoria=%s http=%s codigo=%s "
        "erro=%s tentativas=%s correlacao=%s referencia=%s id_mensagem=%s instante=%s",
        _FRASES[estado], envio.tipo, envio.transporte or "-", estado.value, envio.categoria or "-",
        envio.http_status or "-", envio.codigo_provedor or "-", envio.erro or "-", envio.tentativas,
        envio.correlacao, envio.referencia or "-", envio.id_mensagem or "-",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _escrever_log(envio: EnvioEmail, estado: EstadoEnvio, nivel: int = logging.INFO) -> None:
    logger.log(nivel, *_argumentos_log(envio, estado))


def historico_envios(limite: int = 50) -> list[EnvioEmail]:
    """Solicitações recentes deste processo, da mais nova para a mais antiga.

    Memória do processo: reinício (deploy ou hibernação do servidor) zera a
    lista. Os logs e os avisos no sininho dos administradores permanecem.
    """
    with _TRAVA:
        itens = list(_HISTORICO)
    return list(reversed(itens))[:max(0, limite)]


def _aguardar_envios_pendentes(prazo_maximo: float | None = None) -> None:
    """No encerramento do processo: espera (prazo limitado) envios em andamento.

    Registrado com atexit. Sem isto, as threads daemon seriam encerradas junto
    com o processo (deploy/reinício) e o e-mail se perderia sem rastro; o que
    não terminar no prazo fica registrado como interrompido.
    """
    with _TRAVA:
        pendentes = [(t, e, p) for t, e, p in _EM_ANDAMENTO.values() if isinstance(t, threading.Thread)]
    if not pendentes:
        return
    prazo = min(25.0, max(p for _, _, p in pendentes) + 5.0)
    if prazo_maximo is not None:
        prazo = min(prazo, max(0.0, prazo_maximo))
    limite = time.monotonic() + prazo
    for hilo, _envio, _prazo in pendentes:
        if hilo.is_alive():
            hilo.join(max(0.0, limite - time.monotonic()))
    for hilo, envio, _prazo in pendentes:
        if hilo.is_alive() and envio.estado == EstadoEnvio.AGENDADO:
            envio.categoria = "interrompido"
            logger.warning(
                "E-mail transacional interrompido no encerramento do processo (resultado desconhecido) | "
                "tipo=%s transporte=%s estado=%s categoria=interrompido correlacao=%s referencia=%s",
                envio.tipo, envio.transporte or "-", envio.estado.value, envio.correlacao,
                envio.referencia or "-",
            )


atexit.register(_aguardar_envios_pendentes)


# ---------------------------------------------------------------------
# Configuração e diagnóstico (sem expor segredos)
# ---------------------------------------------------------------------
def transporte_configurado(config) -> str | None:
    """"brevo", "smtp" ou None (envio desativado). MAIL_ATIVO é a chave mestra."""
    if not config.get("MAIL_ATIVO"):
        return None
    return "brevo" if config.get("BREVO_API_KEY") else "smtp"


def _remetente(valor) -> tuple[str, str]:
    if isinstance(valor, (tuple, list)) and len(valor) == 2:
        return str(valor[0] or ""), str(valor[1] or "")
    nome, email = parseaddr(valor or "")
    return nome, email


def formato_chave_brevo(chave) -> str | None:
    """Classifica a chave SÓ pelo prefixo público do Brevo; nunca a expõe."""
    if not chave:
        return None
    if chave.startswith("xkeysib-"):
        return "api"
    if chave.startswith("xsmtpsib-"):
        return "smtp"
    return "desconhecido"


def _seguranca_smtp(config) -> str:
    """Modo real da conexão: com os dois ligados, vale o SSL (extensions.py)."""
    if config.get("MAIL_USE_SSL"):
        return "SSL direto (MAIL_USE_SSL)"
    if config.get("MAIL_USE_TLS"):
        return "STARTTLS (MAIL_USE_TLS)"
    return "Sem criptografia"


def _origem_local(origem: str) -> bool:
    return (urlsplit(origem).hostname or "") in ("127.0.0.1", "localhost")


def diagnostico_configuracao(config=None) -> dict:
    """Situação da configuração de e-mail para o admin e o log de inicialização.

    Mostra só o que não é segredo (transporte, remetente — que já aparece em
    todo e-mail —, formato da chave pelo prefixo, origem dos links).
    """
    config = current_app.config if config is None else config
    transporte = transporte_configurado(config)
    nome, remetente = _remetente(config.get("MAIL_DEFAULT_SENDER"))
    dominio = remetente.rsplit("@", 1)[-1].lower() if "@" in remetente else ""
    chave = formato_chave_brevo(config.get("BREVO_API_KEY"))
    url_api = config.get("BREVO_API_URL") or ""
    partes_url = urlsplit(url_api) if url_api else None
    origem = config.get("PUBLIC_URL") or ""
    hospedagem_bloqueia = bool(config.get("HOSPEDAGEM_BLOQUEIA_SMTP"))
    faltando_smtp = [nome for nome in ("MAIL_USERNAME", "MAIL_PASSWORD") if not config.get(nome)]
    arquivo_env = config.get("ARQUIVO_ENV")
    pendencias = []

    def pendencia(texto, bloqueante):
        pendencias.append({"texto": texto, "bloqueante": bloqueante})

    if transporte is None:
        sem_valor = f" ({' e '.join(faltando_smtp)} sem valor)" if faltando_smtp else ""
        if hospedagem_bloqueia and not config.get("SMTP_PERMITIDO"):
            pendencia("Envio de e-mail DESATIVADO: credenciais de e-mail não configuradas. Defina BREVO_API_KEY "
                      "(API do Brevo) nas variáveis de ambiente do servidor: a hospedagem atual bloqueia as "
                      "portas SMTP.", True)
        else:
            pendencia(f"Envio de e-mail DESATIVADO: credenciais de e-mail não configuradas{sem_valor}. Defina "
                      "MAIL_USERNAME e MAIL_PASSWORD (SMTP) ou BREVO_API_KEY (API do Brevo) no .env ou nas "
                      "variáveis de ambiente do servidor.", True)
    elif transporte == "smtp" and hospedagem_bloqueia and not config.get("SMTP_PERMITIDO"):
        pendencia("O transporte ativo é SMTP, mas a hospedagem atual bloqueia as portas SMTP 25/465/587: "
                  "defina BREVO_API_KEY para usar a API HTTPS do Brevo. Se a hospedagem liberar as portas "
                  "465/587, defina NEXO_SMTP_PERMITIDO=1.", True)
    elif transporte == "smtp" and hospedagem_bloqueia:
        pendencia("SMTP liberado por NEXO_SMTP_PERMITIDO=1: confirme que a hospedagem permite as portas SMTP.",
                  False)
    if transporte == "brevo":
        if chave == "smtp":
            pendencia("BREVO_API_KEY parece ser uma chave SMTP (prefixo xsmtpsib-). Gere uma chave de "
                      "API (prefixo xkeysib-) em Brevo → SMTP e API → Chaves de API.", True)
        elif chave == "desconhecido":
            pendencia("BREVO_API_KEY não tem o prefixo esperado de uma chave de API do Brevo "
                      "(xkeysib-): confira se a chave foi copiada por inteiro.", False)
        if not partes_url or partes_url.scheme != "https" or not partes_url.hostname:
            pendencia("BREVO_API_URL precisa ser uma URL https (padrão: "
                      "https://api.brevo.com/v3/smtp/email).", True)
    if transporte == "brevo" and config.get("MAIL_USERNAME") and config.get("MAIL_PASSWORD"):
        if (not config.get("MAIL_DEFAULT_SENDER_EXPLICITO")
                and remetente.lower() == str(config.get("MAIL_USERNAME")).strip().lower()):
            pendencia("Com BREVO_API_KEY, o envio sai pela API HTTPS do Brevo e MAIL_PASSWORD fica sem uso; "
                      "MAIL_USERNAME continua sendo o remetente. Antes de remover MAIL_USERNAME, defina "
                      "MAIL_DEFAULT_SENDER com o remetente verificado no Brevo.", False)
        else:
            pendencia("As credenciais SMTP (MAIL_USERNAME/MAIL_PASSWORD) também estão definidas, mas não são "
                      "usadas: com BREVO_API_KEY, o envio sai pela API HTTPS do Brevo.", False)
    if transporte == "smtp":
        porta = config.get("MAIL_PORT")
        if config.get("MAIL_USE_SSL") and config.get("MAIL_USE_TLS"):
            pendencia("MAIL_USE_SSL e MAIL_USE_TLS estão ligados ao mesmo tempo: vale o SSL direto e o "
                      "STARTTLS é ignorado. Use SSL na porta 465 ou STARTTLS na 587.", False)
        if not config.get("MAIL_USE_SSL") and not config.get("MAIL_USE_TLS"):
            pendencia("MAIL_USE_TLS e MAIL_USE_SSL estão desligados: a senha e as mensagens trafegam sem "
                      "criptografia. Ligue MAIL_USE_TLS na 587 ou MAIL_USE_SSL na 465.", False)
        if config.get("MAIL_USE_SSL") and porta == 587:
            pendencia("MAIL_USE_SSL está ligado com a porta 587, que usa STARTTLS: use MAIL_USE_TLS=true e "
                      "MAIL_USE_SSL=false na 587, ou MAIL_USE_SSL=true na 465.", False)
        elif config.get("MAIL_USE_TLS") and not config.get("MAIL_USE_SSL") and porta == 465:
            pendencia("A porta 465 usa SSL direto: defina MAIL_USE_SSL=true na 465, ou use a porta 587 com "
                      "STARTTLS (MAIL_USE_TLS=true).", False)
    if transporte is not None and not email_valido(remetente):
        pendencia("MAIL_DEFAULT_SENDER não contém um endereço de e-mail válido.", True)
    elif transporte is not None and dominio in _DOMINIOS_GRATUITOS:
        if transporte == "brevo":
            pendencia(f"O remetente usa um domínio gratuito ({dominio}): o Brevo não autentica domínios "
                      "gratuitos e troca o endereço do remetente por um endereço de domínio do próprio "
                      "Brevo, o que favorece a caixa de spam. Recomenda-se um domínio próprio autenticado "
                      "(SPF/DKIM/DMARC). Confira também se o remetente está verificado no Brevo.", False)
        elif remetente.lower() != str(config.get("MAIL_USERNAME") or "").strip().lower():
            pendencia(f"O remetente usa um domínio gratuito ({dominio}): pelo SMTP ele precisa ser a própria "
                      "conta autenticada em MAIL_USERNAME (ou um alias verificado dela).", False)
    if origem and not origem.startswith("https://") and not _origem_local(origem):
        pendencia("A origem pública dos links (NEXO_PUBLIC_URL) não usa https.", False)
    return {
        "ativo": transporte is not None,
        "transporte": transporte,
        "transporte_rotulo": TRANSPORTES.get(transporte, "Nenhum (envio desativado)"),
        "smtp_servidor": (f"{config.get('MAIL_SERVER')}:{config.get('MAIL_PORT')}" if transporte == "smtp" else None),
        # O que o .env define para o SMTP, mesmo sem envio ativo: é o que falta preencher.
        "smtp_endereco": f"{config.get('MAIL_SERVER')}:{config.get('MAIL_PORT')}",
        "smtp_seguranca": _seguranca_smtp(config),
        "smtp_usuario": config.get("MAIL_USERNAME") or None,
        "smtp_senha_definida": bool(config.get("MAIL_PASSWORD")),
        "smtp_faltando": faltando_smtp,
        "ambiente_local": not config.get("PRODUCAO_HTTP"),
        "arquivo_env": arquivo_env,
        "arquivo_env_existe": bool(arquivo_env and os.path.isfile(arquivo_env)),
        "remetente_nome": nome or None,
        "remetente_email": remetente or None,
        "remetente_valido": email_valido(remetente),
        "remetente_dominio_gratuito": dominio in _DOMINIOS_GRATUITOS,
        "chave_presente": bool(config.get("BREVO_API_KEY")),
        "chave_formato": chave,
        "api_host": partes_url.hostname if partes_url else None,
        "api_https": bool(partes_url and partes_url.scheme == "https"),
        "origem_publica": origem or None,
        "origem_https": origem.startswith("https://"),
        "origem_local": bool(origem) and _origem_local(origem),
        "hospedagem_bloqueia_smtp": hospedagem_bloqueia,
        "prazo_segundos": config.get("EMAIL_TIMEOUT_SEGUNDOS", 15.0),
        "alerta_admin": bool(config.get("EMAIL_ALERTA_ADMIN")),
        "pendencias": pendencias,
        # Algo impede qualquer envio (o cliente nunca receberia a ativação).
        "bloqueante": any(p["bloqueante"] for p in pendencias),
    }


def registrar_diagnostico_inicial(app) -> None:
    """Na inicialização: uma linha neutra em ambiente local; em produção, o resumo
    da configuração e as pendências como WARNING."""
    try:
        diag = diagnostico_configuracao(app.config)
    except Exception as erro:  # noqa: BLE001 — diagnóstico nunca impede a subida
        logger.warning("Diagnóstico da configuração de e-mail indisponível (%s).", type(erro).__name__)
        return
    if not app.config.get("PRODUCAO_HTTP"):
        # O detalhe (pendências, servidor, remetente) fica no Diagnóstico de e-mail.
        logger.debug("Transporte local ativo" if diag["ativo"] else "Credenciais de e-mail não configuradas no .env")
        return
    remetente = diag["remetente_email"] or ""
    logger.info(
        "Configuração de e-mail | transporte=%s remetente_dominio=%s chave_formato=%s origem_https=%s "
        "prazo_s=%s pendencias=%s",
        diag["transporte"] or "desativado", remetente.rsplit("@", 1)[-1] if "@" in remetente else "-",
        diag["chave_formato"] or "-", "sim" if diag["origem_https"] else "nao", diag["prazo_segundos"],
        len(diag["pendencias"]),
    )
    for pendencia in diag["pendencias"]:
        logger.warning("Configuração de e-mail com pendência%s: %s",
                       " (bloqueia o envio)" if pendencia["bloqueante"] else "", pendencia["texto"])


# ---------------------------------------------------------------------
# Montagem da mensagem
# ---------------------------------------------------------------------
class _ExtratorTexto(HTMLParser):
    """Versão em texto simples do HTML (multipart/alternative)."""
    _BLOCOS = frozenset({"p", "div", "br", "tr", "h1", "h2", "h3", "li", "table"})
    _OCULTOS = frozenset({"style", "script", "head", "title"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes, self._links, self._oculto = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self._OCULTOS:
            self._oculto += 1
        elif tag == "a":
            self._links.append((dict(attrs).get("href"), len(self.partes)))
        elif tag in self._BLOCOS:
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in self._OCULTOS:
            self._oculto = max(0, self._oculto - 1)
        elif tag == "a":
            href, inicio = self._links.pop() if self._links else (None, len(self.partes))
            rotulo = "".join(self.partes[inicio:]).strip()
            if href and href.startswith(("https://", "http://")) and href != rotulo:
                self.partes.append(f": {href}")
        elif tag in self._BLOCOS:
            self.partes.append("\n")

    def handle_data(self, data):
        if not self._oculto:
            self.partes.append(data)


def _texto_simples(html: str) -> str:
    extrator = _ExtratorTexto()
    extrator.feed(html)
    extrator.close()
    linhas = (" ".join(linha.split()) for linha in "".join(extrator.partes).splitlines())
    texto, anterior_vazia = [], True
    for linha in linhas:
        if linha or not anterior_vazia:
            texto.append(linha)
        anterior_vazia = not linha
    return "\n".join(texto).strip()


def _destinatarios_validos(destinatarios) -> list[str]:
    """Remove vazios, inválidos e duplicados (sem diferenciar maiúsculas)."""
    if isinstance(destinatarios, str):
        destinatarios = [destinatarios]
    vistos, validos = set(), []
    for item in destinatarios or []:
        if not isinstance(item, str):
            continue
        endereco = item.strip()
        chave = endereco.lower()
        if email_valido(endereco) and chave not in vistos:
            vistos.add(chave)
            validos.append(endereco)
    return validos


# ---------------------------------------------------------------------
# Transportes
# ---------------------------------------------------------------------
def _falha_provedor(status: int, codigo: str | None, categoria: str, espera: float | None = None) -> RuntimeError:
    """Erro HTTP do provedor sem corpo/contexto: só status, código e categoria."""
    erro = RuntimeError(f"Brevo HTTP {status}")
    erro.nexo_http_status = status
    erro.nexo_codigo = codigo
    erro.nexo_categoria = categoria
    erro.nexo_espera = espera
    erro.nexo_reenvio = status in (429, 500, 503)
    return erro


def _categoria_http(status: int, codigo: str | None, mensagem: str) -> str:
    """Categoria a partir do status, do código (enum do Brevo) e de palavras da
    mensagem do provedor. A mensagem serve só para esta classificação: pode
    conter IP, endereço ou outros dados e nunca é registrada."""
    if codigo in ("dmarc_policy_requires_domain_authentication", "dns_records_not_properly_configured"):
        return "dominio_nao_autenticado"
    if codigo == "account_under_validation":
        return "conta_em_validacao"
    if codigo in ("not_enough_credits", "insufficient_credits"):
        return "creditos_insuficientes"
    if codigo == "permission_denied":
        return "conta_sem_permissao"
    if codigo == "duplicate_request":
        return "limite_excedido"
    if status == 401 or codigo in ("unauthorized", "api_key_not_found", "authentication_failed"):
        if any(p in mensagem for p in ("ip address", "authorised_ips", "authorized_ips", "unrecognised ip",
                                       "unrecognized ip", "not verified", "ip not authori")):
            return "ip_nao_autorizado"
        return "credencial_rejeitada"
    if status == 402:
        return "creditos_insuficientes"
    if status == 403:
        return "conta_sem_permissao"
    if status == 429:
        return "limite_excedido"
    if status in (502, 504):
        return "gateway_sem_resposta"
    if status >= 500:
        return "provedor_indisponivel"
    if 300 <= status < 400 or status in (404, 405, 406, 415):
        # Redirecionamento (não seguido: a chave não pode ir a outro destino)
        # ou endpoint errado: confira BREVO_API_URL.
        return "configuracao_invalida"
    if status == 400:
        if "dmarc" in mensagem or "domain authentication" in mensagem:
            return "dominio_nao_autenticado"
        if "sender" in mensagem or "remetente" in mensagem:
            return "remetente_nao_autorizado"
        if any(p in mensagem for p in (" in to", "recipient", "email is not valid", "invalid email",
                                       "destinat")):
            return "destinatario_invalido"
        return "requisicao_rejeitada"
    return "desconhecida"


def _codigo_brevo(valor) -> str | None:
    """Código do provedor seguro para o log: snake_case do enum ou um dos textos
    oficiais (normalizado). Qualquer outro valor fica fora do log."""
    if not isinstance(valor, str):
        return None
    if _CODIGO_PROVEDOR.fullmatch(valor):
        return valor
    texto = " ".join(valor.split()).lower()
    if texto in _CODIGOS_TEXTO_BREVO:
        return re.sub(r"[^a-z0-9]+", "_", texto).strip("_")
    return None


def _segundos(valor) -> float | None:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if 0 <= numero < float("inf") else None


def _ler_erro_http(erro: urllib.error.HTTPError) -> tuple[str | None, str, float | None]:
    """(código do provedor seguro, texto em minúsculas p/ classificação, espera em s).

    A espera vem do Retry-After ou, num 429, do x-sib-ratelimit-reset (o
    cabeçalho que o Brevo documenta; o Retry-After ele não documenta).
    """
    codigo, mensagem, espera = None, "", None
    try:
        bruto = erro.read(_LIMITE_CORPO_PROVEDOR)
        dados = json.loads(bruto.decode("utf-8", "replace")) if bruto else None
        if isinstance(dados, dict):
            valor = dados.get("code")
            codigo = _codigo_brevo(valor)
            partes = [valor[:120] if isinstance(valor, str) else ""]
            if isinstance(dados.get("message"), str):
                partes.append(dados["message"][:500])
            # Código e mensagem crus servem só para classificar; nunca vão ao log.
            mensagem = " ".join(partes).strip().lower()
    except Exception:  # noqa: BLE001 — corpo ausente ou ilegível: só o status conta
        pass
    try:
        cabecalhos = erro.headers
        if cabecalhos is not None:
            espera = _segundos(cabecalhos.get("Retry-After"))
            if espera is None and erro.code == 429:
                espera = _segundos(cabecalhos.get("x-sib-ratelimit-reset"))
    except Exception:  # noqa: BLE001
        espera = None
    try:
        erro.close()
    except Exception:  # noqa: BLE001
        pass
    return codigo, mensagem, espera


def _enviar_brevo(app, msg) -> str | None:
    """Envia pela API transacional do Brevo (HTTPS). Mesmo remetente,
    destinatários, assunto e HTML que o SMTP usaria. Devolve o messageId do
    aceite (ou None) e levanta erro sem o corpo da resposta do provedor."""
    url = app.config.get("BREVO_API_URL") or ""
    partes = urlsplit(url)
    if partes.scheme != "https" or not partes.hostname:
        # Nunca enviar a chave por um canal sem TLS ou para destino vazio.
        raise _falha_provedor(0, None, "configuracao_invalida")
    nome, email = _remetente(msg.sender)
    payload = {
        "sender": {"name": (nome or "NEXO")[:70], "email": email},
        "to": [{"email": d} for d in msg.recipients],
        "subject": msg.subject,
        "htmlContent": msg.html,
    }
    copia_oculta = getattr(msg, "bcc", None)
    if copia_oculta:
        payload["bcc"] = [{"email": d} for d in copia_oculta]
    texto = getattr(msg, "body", None)
    if isinstance(texto, str) and texto:
        payload["textContent"] = texto
    if email_valido(email):
        # O Brevo reescreve remetentes de domínio gratuito (ex.: @gmail.com):
        # sem Reply-To, uma resposta do cliente iria para o endereço reescrito.
        payload["replyTo"] = {"email": email, "name": (nome or "NEXO")[:70]}
    envio = getattr(msg, "nexo_envio", None)
    if isinstance(envio, EnvioEmail):
        # Etiqueta e cabeçalho não sensíveis: filtram os registros do Brevo
        # (Transacional → Logs) por tipo e ligam a mensagem à linha de log.
        payload["tags"] = [f"nexo-{envio.tipo}"]
        payload["headers"] = {"X-Nexo-Correlacao": envio.correlacao}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": _USER_AGENT,
        },
        method="POST",
    )
    # "Unredirected": se o endpoint redirecionar, o urllib não repassa a chave
    # ao novo destino (que poderia ser outro host ou até http).
    req.add_unredirected_header("api-key", app.config.get("BREVO_API_KEY") or "")
    try:
        with urllib.request.urlopen(req, timeout=app.config.get("EMAIL_TIMEOUT_SEGUNDOS", 15.0)) as resposta:
            status = getattr(resposta, "status", None)
            url_final = resposta.geturl() if callable(getattr(resposta, "geturl", None)) else None
            corpo = resposta.read(_LIMITE_CORPO_PROVEDOR)
    except urllib.error.HTTPError as e:
        # Respostas do provedor podem incluir destinatários, IP ou conteúdo
        # privado: apenas status/código/categoria seguem adiante, sem contexto.
        codigo, mensagem, espera = _ler_erro_http(e)
        raise _falha_provedor(e.code, codigo, _categoria_http(e.code, codigo, mensagem), espera) from None
    if (isinstance(url_final, str) and url_final != url) or (isinstance(status, int) and status not in (201, 202)):
        # Redirecionado (o pedido virou GET, sem a chave) ou resposta que não é
        # a do envio transacional: não há aceite a registrar.
        raise _falha_provedor(status if isinstance(status, int) else 0, None, "configuracao_invalida")
    try:
        dados = json.loads(corpo.decode("utf-8")) if isinstance(corpo, bytes) and corpo else None
    except ValueError:
        dados = None
    id_mensagem = dados.get("messageId") if isinstance(dados, dict) else None
    if isinstance(id_mensagem, str) and _ID_MENSAGEM.fullmatch(id_mensagem):
        return id_mensagem
    return None


# ---------------------------------------------------------------------
# Confirmação de entrega no provedor (teste controlado, sob demanda do admin)
# ---------------------------------------------------------------------
# Eventos de GET /v3/smtp/statistics/events e o rótulo mostrado ao admin.
EVENTOS_PROVEDOR = {
    "requests": "Recebido pelo Brevo",
    "delivered": "Entregue ao servidor do destinatário",
    "deferred": "Entrega adiada (o Brevo tentará de novo)",
    "softBounces": "Devolvido temporariamente (soft bounce)",
    "hardBounces": "Devolvido (hard bounce)",
    "bounces": "Devolvido",
    "blocked": "Bloqueado pelo Brevo",
    "invalid": "Endereço inválido",
    "error": "Erro no Brevo",
}
# Abertura, clique, descadastro e denúncia de spam são comportamento do
# destinatário, não entrega: ficam fora (nem guardados, nem exibidos, nem no log).
EVENTOS_FALHA_ENTREGA = frozenset({"softBounces", "hardBounces", "bounces", "blocked", "invalid", "error"})
_LIMITE_RESPOSTA_EVENTOS = 64 * 1024


def _data_evento(valor):
    if not isinstance(valor, str) or len(valor) > 40:
        return None
    try:
        data = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
        data = (data if data.tzinfo else data.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
    # Faixa plausível: uma data extrema quebraria a conversão de fuso na página.
    return data if 2000 <= data.year <= 2100 else None


def consultar_entrega_brevo(envio: EnvioEmail, app=None) -> dict:
    """Eventos que o Brevo registrou para o messageId deste envio.

    Sob demanda do admin (Diagnóstico de e-mail), somente leitura e na mesma
    origem de BREVO_API_URL. Guarda só o tipo e o horário de cada evento —
    nunca destinatário, motivo, IP, link ou assunto. "Entregue" é a entrega
    ao servidor do destinatário: a caixa de entrada só o destinatário confirma.
    Devolve {"ok", "eventos", "categoria", "http_status"} e nunca levanta.
    """
    app = app or current_app._get_current_object()
    resultado = {"ok": False, "eventos": [], "categoria": None, "http_status": None}
    chave = app.config.get("BREVO_API_KEY")
    partes = urlsplit(app.config.get("BREVO_API_URL") or "")
    if (envio.transporte != "brevo" or not isinstance(envio.id_mensagem, str) or not chave
            or partes.scheme != "https" or not partes.hostname):
        resultado["categoria"] = "configuracao_invalida"
        return resultado
    consulta = urlencode({"messageId": envio.id_mensagem, "days": 7, "limit": 100, "sort": "asc"})
    url = urlunsplit((partes.scheme, partes.netloc, "/v3/smtp/statistics/events", consulta, ""))
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": _USER_AGENT},
                                 method="GET")
    # Mesma proteção do envio: a chave nunca acompanha um redirecionamento.
    req.add_unredirected_header("api-key", chave)
    try:
        with urllib.request.urlopen(req, timeout=app.config.get("EMAIL_TIMEOUT_SEGUNDOS", 15.0)) as resposta:
            status = getattr(resposta, "status", None)
            url_final = resposta.geturl() if callable(getattr(resposta, "geturl", None)) else None
            corpo = resposta.read(_LIMITE_RESPOSTA_EVENTOS + 1)
    except urllib.error.HTTPError as e:
        codigo, mensagem, _espera = _ler_erro_http(e)
        resultado.update(categoria=_categoria_http(e.code, codigo, mensagem), http_status=e.code)
    except Exception as e:  # noqa: BLE001 — a consulta nunca derruba a página do admin
        resultado["categoria"] = _categoria_excecao(e, "brevo")[0]
    else:
        dados = None
        try:
            if (status == 200 and not (isinstance(url_final, str) and url_final != url)
                    and isinstance(corpo, bytes) and len(corpo) <= _LIMITE_RESPOSTA_EVENTOS):
                dados = json.loads(corpo.decode("utf-8")) if corpo else {}
            if isinstance(dados, dict):
                alvo = envio.id_mensagem.strip("<>")
                itens = dados.get("events")
                if itens is None:
                    itens = []           # o Brevo omite "events" quando não há nenhum
                if not isinstance(itens, list):
                    raise ValueError("lista de eventos fora do formato documentado")
                eventos = []
                for item in itens[:100]:
                    nome = item.get("event") if isinstance(item, dict) else None
                    if not isinstance(nome, str):
                        raise ValueError("evento fora do formato documentado")
                    if nome not in EVENTOS_PROVEDOR:
                        continue         # abertura, clique etc. ficam de fora
                    id_item = item.get("messageId")
                    if isinstance(id_item, str) and id_item.strip("<>") != alvo:
                        continue
                    eventos.append({"evento": nome, "rotulo": EVENTOS_PROVEDOR[nome],
                                    "data": _data_evento(item.get("date"))})
                resultado.update(ok=True, eventos=eventos)
        except Exception:  # noqa: BLE001 — corpo malformado (JSON profundo, tipos inesperados)
            resultado["ok"], resultado["eventos"] = False, []
        if not resultado["ok"]:
            resultado.update(categoria="configuracao_invalida", http_status=status if isinstance(status, int) else None)
    with _TRAVA:
        # Uma consulta que falhou não apaga eventos já obtidos nem vira "nenhum evento".
        if resultado["ok"]:
            envio.eventos_provedor = resultado["eventos"]
        envio.eventos_consulta_ok = resultado["ok"]
        envio.eventos_consultados_em = datetime.now(timezone.utc)
    logger.info("Consulta de entrega no provedor | correlacao=%s ok=%s eventos=%s categoria=%s http=%s",
                envio.correlacao, resultado["ok"], ",".join(e["evento"] for e in resultado["eventos"]) or "-",
                resultado["categoria"] or "-", resultado["http_status"] or "-")
    return resultado


def _resposta_smtp(erro) -> tuple[int | None, str | None]:
    """(código SMTP, status estendido "x.y.z") de uma recusa do servidor.

    Só números: o texto da resposta pode citar endereços e nunca é registrado.
    """
    codigo, texto = getattr(erro, "smtp_code", None), getattr(erro, "smtp_error", None)
    if isinstance(erro, smtplib.SMTPRecipientsRefused):
        recusas = list((getattr(erro, "recipients", None) or {}).values())
        codigo, texto = recusas[0] if recusas and isinstance(recusas[0], tuple) and len(recusas[0]) == 2 \
            else (None, None)
    if type(codigo) is not int or not 200 <= codigo <= 599:
        return None, None
    if isinstance(texto, str):
        texto = texto.encode("utf-8", "replace")
    achado = _STATUS_SMTP_ESTENDIDO.match(texto) if isinstance(texto, bytes) else None
    if achado is None or achado.group(1) != str(codigo)[0].encode("ascii"):
        return codigo, None
    return codigo, (achado.group(1) + achado.group(2)).decode("ascii")


def _codigo_smtp(erro) -> str | None:
    """Código da recusa SMTP para o log (ex.: 534/5.7.9), sem o texto do servidor."""
    codigo, estendido = _resposta_smtp(erro)
    if codigo is None:
        return None
    return f"{codigo}/{estendido}" if estendido else str(codigo)


def _servidor_gmail() -> bool:
    servidor = str(current_app.config.get("MAIL_SERVER") or "").lower() if has_app_context() else ""
    return servidor.endswith(("gmail.com", "googlemail.com"))


def _categoria_excecao(erro: BaseException, transporte: str) -> tuple[str, bool]:
    """(categoria, falhou antes de enviar a requisição) para erros não HTTP."""
    if isinstance(erro, urllib.error.URLError) and not isinstance(erro, urllib.error.HTTPError):
        motivo = erro.reason
        if isinstance(motivo, ssl.SSLError):
            return "tls", True
        if isinstance(motivo, TimeoutError):
            # Prazo na conexão ou no TLS: o pedido não saiu (falha certa).
            # "tempo_esgotado" fica para o prazo na leitura da resposta.
            return "conexao_sem_resposta", True
        return "rede", True
    if isinstance(erro, ValorSMTPInvalido):
        return "smtp_valor_invalido", False
    if isinstance(erro, smtplib.SMTPException):
        codigo_smtp, estendido = _resposta_smtp(erro)
        if estendido == "5.4.5":
            # Gmail: cota diária de envio da conta excedida.
            return "limite_excedido", False
        if isinstance(erro, smtplib.SMTPAuthenticationError):
            if (codigo_smtp, estendido) == (534, "5.7.9") and _servidor_gmail():
                # Gmail: a conta exige senha de app (verificação em duas etapas).
                return "smtp_senha_de_app", False
            if isinstance(codigo_smtp, int) and 400 <= codigo_smtp <= 499:
                return "smtp_login_temporario", False
    if isinstance(erro, smtplib.SMTPAuthenticationError):
        return "smtp_autenticacao", False
    if isinstance(erro, smtplib.SMTPSenderRefused):
        return "smtp_remetente_recusado", False
    if isinstance(erro, smtplib.SMTPRecipientsRefused):
        return "smtp_destinatario_recusado", False
    if isinstance(erro, smtplib.SMTPNotSupportedError):
        return "smtp_nao_suportado", False
    if isinstance(erro, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)):
        return "smtp_conexao", False
    if isinstance(erro, smtplib.SMTPException):
        return "smtp_erro", False
    if isinstance(erro, ssl.SSLError):
        return "tls", False
    if isinstance(erro, TimeoutError):
        return ("smtp_conexao" if transporte == "smtp" else "tempo_esgotado"), False
    if isinstance(erro, (socket.gaierror, ConnectionRefusedError)):
        return ("smtp_conexao" if transporte == "smtp" else "rede"), transporte != "smtp"
    if transporte != "smtp" and isinstance(erro, (OSError, http.client.HTTPException)):
        # Fora de URLError o pedido já tinha sido enviado (a resposta é que
        # falhou): o provedor pode ter aceitado a mensagem.
        return "conexao_interrompida", False
    if isinstance(erro, OSError):
        return "smtp_conexao", False
    if isinstance(erro, AssertionError):
        # Flask-Mail: mensagem sem remetente ou sem destinatário.
        return "configuracao_invalida", False
    return "desconhecida", False


def _conferir_copia_oculta(msg) -> None:
    """Caixa da cópia oculta recusada pelo servidor: o destinatário visível foi aceito e o
    smtplib não levanta exceção, mas a mensagem não chegou a quem deveria."""
    ocultos = {d.lower() for d in getattr(msg, "bcc", None) or ()}
    recusados = {endereco: resposta for endereco, resposta in (getattr(msg, "nexo_recusados", None) or {}).items()
                 if endereco.lower() in ocultos}
    if recusados:
        raise smtplib.SMTPRecipientsRefused(recusados)


def _executar_envio(app, msg, envio: EnvioEmail) -> EnvioEmail:
    """Entrega a mensagem ao transporte e registra o desfecho (uma linha de log)."""
    transporte = envio.transporte or ("brevo" if app.config.get("BREVO_API_KEY") else "smtp")
    envio.transporte = transporte
    while True:
        envio.tentativas += 1
        try:
            if transporte == "brevo":
                id_mensagem = _enviar_brevo(app, msg)
            else:
                mail.send(msg)
                id_mensagem = None
                _conferir_copia_oculta(msg)
        except Exception as e:  # noqa: BLE001 — nenhuma falha de e-mail sobe para o fluxo
            categoria = getattr(e, "nexo_categoria", None)
            if categoria is not None:
                antes_do_envio = bool(getattr(e, "nexo_reenvio", False))
            else:
                categoria, antes_do_envio = _categoria_excecao(e, transporte)
            if (transporte == "smtp" and categoria == "smtp_conexao"
                    and getattr(msg, "nexo_fase_smtp", None) == "envio"):
                # A conexão caiu (ou o prazo esgotou) já na transação SMTP: o
                # servidor pode ter aceitado a mensagem — resultado incerto.
                categoria = "conexao_interrompida"
            reenviar = (transporte == "brevo" and envio.tentativas < _MAX_TENTATIVAS and antes_do_envio
                        and categoria in (_CATEGORIAS_TRANSITORIAS | {"rede", "conexao_sem_resposta"}))
            espera = getattr(e, "nexo_espera", None)
            if reenviar and isinstance(espera, (int, float)) and espera > _ESPERA_MAXIMA:
                # O provedor pediu para esperar mais do que este fluxo pode:
                # repetir agora só consumiria cota e falharia de novo.
                reenviar = False
            if reenviar:
                espera = espera if isinstance(espera, (int, float)) and espera >= 0 else _ESPERA_NOVA_TENTATIVA
                _dormir(espera)
                continue
            _registrar(
                envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING, categoria=categoria,
                http_status=getattr(e, "nexo_http_status", None) or None,
                codigo_provedor=getattr(e, "nexo_codigo", None) or (
                    _codigo_smtp(e) if transporte == "smtp" else None),
                erro=type(e).__name__,
            )
            # Falhou antes da transação de envio: foi a conexão ou o login que não
            # se completou, seja qual for a categoria (STARTTLS 454, sem mecanismo...).
            if transporte == "smtp" and (categoria in _CATEGORIAS_CONEXAO_SMTP
                                         or getattr(msg, "nexo_fase_smtp", None) != "envio"):
                _anotar_conexao_smtp(False, "envio", categoria=categoria, codigo=_codigo_smtp(e),
                                     erro=type(e).__name__)
            _alertar_administradores(app, envio)
            return envio
        # Aceitação do SMTP/API não confirma a entrega ao destinatário.
        _registrar(envio, EstadoEnvio.ACEITO_PELO_PROVEDOR, categoria=None,
                   id_mensagem=id_mensagem if isinstance(id_mensagem, str) else None)
        if transporte == "smtp":
            _anotar_conexao_smtp(True, "envio")
        return envio


def _anotar_conexao_smtp(ok: bool, origem: str, *, categoria=None, codigo=None, erro=None) -> dict:
    """Guarda o último desfecho: nunca usuário, senha ou o texto da resposta do servidor."""
    global _CONEXAO_SMTP
    resultado = {
        "ok": ok, "origem": origem, "categoria": categoria,
        "descricao": CATEGORIAS.get(categoria) if categoria else None,
        "codigo": codigo, "erro": erro, "instante": datetime.now(timezone.utc),
    }
    with _TRAVA:
        _CONEXAO_SMTP = resultado
    return resultado


def ultima_conexao_smtp() -> dict | None:
    """Último desfecho conhecido neste processo; None enquanto não houver tentativa.

    Memória do processo, como o histórico: o .env só é lido na inicialização, então
    reiniciar para aplicar uma credencial nova também descarta o status antigo.
    """
    with _TRAVA:
        return _CONEXAO_SMTP


def verificar_conexao_smtp() -> dict:
    """Abre a conexão SMTP configurada (conexão, TLS e login) sem enviar mensagem.

    Sob demanda do admin (Diagnóstico de e-mail): confirma as credenciais do .env
    antes de qualquer envio real. Devolve {"ok", "origem", "categoria", "descricao",
    "codigo", "erro", "instante"} e nunca levanta. Fora do SMTP (Brevo em uso, envio
    desativado ou suprimido) não há conexão a testar nem status a guardar.
    """
    app = current_app._get_current_object()
    transporte = transporte_configurado(app.config)
    nao_testada = None
    if transporte is None:
        nao_testada = "transporte_desativado"
    elif transporte != "smtp":
        nao_testada = "smtp_nao_usado"
    elif app.extensions["mail"].suppress:
        nao_testada = "smtp_suprimido"
    if nao_testada:
        return {"ok": False, "origem": "verificacao", "categoria": nao_testada,
                "descricao": CATEGORIAS[nao_testada], "codigo": None, "erro": None,
                "instante": datetime.now(timezone.utc)}
    try:
        # A saída do bloco faz o QUIT; nada entra na transação de envio (MAIL/RCPT/DATA).
        with mail.connect():
            pass
    except Exception as e:  # noqa: BLE001 — a verificação nunca derruba a página do admin
        categoria = _categoria_excecao(e, "smtp")[0]
        resultado = _anotar_conexao_smtp(False, "verificacao", categoria=categoria, codigo=_codigo_smtp(e),
                                         erro=type(e).__name__)
        logger.warning("Verificação da conexão SMTP com falha | categoria=%s codigo=%s erro=%s",
                       categoria, resultado["codigo"] or "-", resultado["erro"])
        return resultado
    logger.info("Verificação da conexão SMTP concluída | ok=True")
    return _anotar_conexao_smtp(True, "verificacao")


def _enviar_async(app, msg, envio: EnvioEmail | None = None) -> None:
    """Alvo da thread de envio: contexto de aplicação próprio, nunca levanta."""
    # Sem "or": um EnvioEmail que falhou é falso em contexto booleano.
    if not isinstance(envio, EnvioEmail):
        envio = getattr(msg, "nexo_envio", None)
    if not isinstance(envio, EnvioEmail):
        envio = EnvioEmail()
    with app.app_context():
        try:
            _executar_envio(app, msg, envio)
        except Exception as e:  # noqa: BLE001 — defesa final: a thread nunca propaga
            _registrar(envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING,
                       categoria="desconhecida", erro=type(e).__name__)


# ---------------------------------------------------------------------
# Aviso aos administradores (sininho), sem dados privados
# ---------------------------------------------------------------------
def _caminho_diagnostico(app) -> str | None:
    try:
        return app.url_map.bind("localhost").build("admin.diagnostico_email")
    except Exception:  # noqa: BLE001 — rota ausente (apps de teste mínimos)
        return None


def _alertar_administradores(app, envio: EnvioEmail) -> None:
    """Notificação persistida para os ADMIN quando o PROVEDOR não aceita um e-mail.

    Chamada só de _executar_envio: na thread de envio (fluxos assíncronos) ou
    numa ação do próprio admin (modo síncrono) — nunca na requisição pública.
    Ligado em produção (EMAIL_ALERTA_ADMIN). No máximo um aviso por categoria a
    cada 30 minutos neste processo, para não inundar o sininho numa pane do
    provedor. Falha ao avisar só é registrada em log.
    """
    if envio.tipo == "teste" or not app.config.get("EMAIL_ALERTA_ADMIN"):
        return
    chave = envio.categoria or envio.estado
    agora = time.monotonic()
    with _TRAVA:
        ultimo = _ULTIMO_ALERTA.get(chave)
        if ultimo is not None and agora - ultimo < _INTERVALO_ALERTA:
            return
        _ULTIMO_ALERTA[chave] = agora
    # O prefixo "Falha no envio de e-mail" dá ao aviso o selo "Aberto" no
    # sininho (notifications.classificar_notificacao).
    texto = (f"Falha no envio de e-mail ({envio.rotulo_tipo}): "
             f"{envio.descricao_categoria or 'erro no envio'}. Ref. {envio.correlacao}.")
    try:
        # Gravado em thread própria: nunca dentro da requisição (sem segunda
        # sessão de banco presa à transação dela nem tempo extra na resposta).
        # Sem daemon: num comando de terminal, o processo espera o aviso ser gravado antes de sair.
        threading.Thread(target=_gravar_aviso_admins, args=(app, texto, chave), daemon=False).start()
    except Exception as erro:  # noqa: BLE001
        _liberar_janela_alerta(chave)
        logger.warning("Aviso de falha de e-mail aos administradores indisponível (%s).", type(erro).__name__)


def _liberar_janela_alerta(chave) -> None:
    with _TRAVA:
        _ULTIMO_ALERTA.pop(chave, None)


def _gravar_aviso_admins(app, texto: str, chave) -> None:
    try:
        with app.app_context():
            from extensions import db
            from notifications import notificar_admins
            notificar_admins(texto, _caminho_diagnostico(app))
            db.session.commit()
    except Exception as erro:  # noqa: BLE001
        # A janela de 30 min só vale para avisos efetivamente gravados.
        _liberar_janela_alerta(chave)
        logger.warning("Aviso de falha de e-mail aos administradores indisponível (%s).", type(erro).__name__)


# ---------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------
def enviar_email(destinatarios, assunto: str, template: str, *, tipo: str = "generico",
                 referencia: str | None = None, sincrono: bool = False, copia_oculta=None,
                 **ctx) -> EnvioEmail:
    """Renderiza templates/emails/<template> e entrega ao transporte.

    Assíncrono por padrão (thread); `sincrono=True` espera o desfecho (ações do
    admin). `copia_oculta` entra só no envelope, nunca nos cabeçalhos da mensagem.
    Devolve o EnvioEmail com o estado operacional — nunca levanta.
    """
    envio = EnvioEmail(tipo, referencia=referencia)
    try:
        app = current_app._get_current_object()
        validos = _destinatarios_validos(destinatarios)
        informados = _destinatarios_informados(destinatarios)
        if not informados:
            _registrar(envio, EstadoEnvio.SEM_DESTINATARIO, categoria="sem_destinatario")
            return envio
        transporte = transporte_configurado(app.config)
        if transporte is None:
            # Aviso no sininho (thread própria, no máx. 1 a cada 30 min): sem a
            # faixa no dashboard, é o sinal proativo ao admin. A recuperação
            # anônima não chega aqui (auth.recuperar_senha para antes, igual
            # para qualquer endereço), então o aviso não revela contas.
            nivel = logging.WARNING if app.config.get("PRODUCAO_HTTP") else logging.INFO
            _registrar(envio, EstadoEnvio.DESATIVADO, nivel=nivel, categoria="transporte_desativado")
            _alertar_administradores(app, envio)
            return envio
        envio.transporte = transporte
        if not validos:
            _registrar(envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING, categoria="destinatario_invalido")
            return envio
        html = render_template(f"emails/{template}", **ctx)
        # Assunto em uma linha: nada de CR/LF (injeção de cabeçalho) nem texto sem fim.
        assunto = " ".join(str(assunto).split())[:200]
        # Repetir um destinatário visível na cópia oculta só duplicaria a mensagem.
        visiveis = {d.lower() for d in validos}
        ocultos = [d for d in _destinatarios_validos(copia_oculta) if d.lower() not in visiveis]
        msg = Message(subject=assunto, recipients=validos, bcc=ocultos or None, html=html,
                      body=_texto_simples(html), extra_headers={"X-Nexo-Correlacao": envio.correlacao})
        msg.nexo_envio = envio
        if not email_valido(_remetente(msg.sender)[1]):
            # Pendência de configuração (bloqueante no Diagnóstico de e-mail).
            _registrar(envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING, categoria="remetente_invalido")
            return envio
    except Exception as erro:  # noqa: BLE001
        # Falha de preparo também é fail-safe; não publicar conteúdo/URLs.
        _registrar(envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING, categoria="template",
                   erro=type(erro).__name__)
        return envio

    if sincrono:
        try:
            return _executar_envio(app, msg, envio)
        except Exception as erro:  # noqa: BLE001 — defesa final: o fluxo do admin segue
            _registrar(envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING, categoria="desconhecida",
                       erro=type(erro).__name__)
            return envio

    try:
        # O estado vira AGENDADO antes do start(): a thread pode terminar antes
        # desta linha seguinte e o desfecho dela não pode ser sobrescrito.
        _mudar_estado(envio, EstadoEnvio.AGENDADO)
        linha_agendado = _argumentos_log(envio, EstadoEnvio.AGENDADO)
        hilo = threading.Thread(target=_enviar_async, args=(app, msg, envio), daemon=True)
        with _TRAVA:
            _EM_ANDAMENTO[envio.correlacao] = (hilo, envio, float(app.config.get("EMAIL_TIMEOUT_SEGUNDOS", 15.0)))
        hilo.start()
    except Exception as erro:  # noqa: BLE001
        # A falha de agendamento também é fail-safe; não publicar conteúdo/URLs.
        _registrar(envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING, categoria="agendamento",
                   erro=type(erro).__name__)
        return envio
    logger.info(*linha_agendado)
    return envio


def envio_disponivel(config) -> bool:
    """Há transporte e nenhuma pendência que impeça qualquer envio (chave SMTP
    no lugar da de API, URL sem https, remetente inválido, SMTP em hospedagem que
    bloqueia as portas, sem NEXO_SMTP_PERMITIDO...). Só lê a configuração: nunca
    depende de conta."""
    return transporte_configurado(config) is not None and not diagnostico_configuracao(config)["bloqueante"]


def registrar_envio_suprimido(tipo: str) -> EnvioEmail:
    """Registra um e-mail que não pôde sair porque a configuração do sistema
    impede o envio (histórico, log e aviso aos admins). Não depende de conta:
    usado pela recuperação de senha antes de qualquer consulta ao banco."""
    app = current_app._get_current_object()
    envio = EnvioEmail(tipo)
    categoria = "transporte_desativado" if transporte_configurado(app.config) is None else "envio_bloqueado"
    nivel = logging.WARNING if app.config.get("PRODUCAO_HTTP") else logging.INFO
    _registrar(envio, EstadoEnvio.DESATIVADO, nivel=nivel, categoria=categoria)
    _alertar_administradores(app, envio)
    return envio


def _destinatarios_informados(destinatarios) -> list[str]:
    """Destinatários informados (não vazios), válidos ou não — distingue
    "ninguém a avisar" de "endereço cadastrado inválido"."""
    if isinstance(destinatarios, str):
        destinatarios = [destinatarios]
    return [d for d in (destinatarios or []) if isinstance(d, str) and d.strip()]


def _emails_da_empresa(empresa) -> list[str]:
    """Coleta e-mails de contato da empresa + dos usuários CLIENTE ativos, sem
    repetir o mesmo endereço escrito com maiúsculas diferentes.
    Empresa na lixeira ou cancelada não recebe mais comunicações do portal."""
    if empresa is None or getattr(empresa, "deletado_em", None) is not None \
            or getattr(empresa, "status_conta", "ATIVA") == "CANCELADA":
        return []
    usuarios = list(getattr(empresa, "usuarios", []) or [])
    # O contato da empresa que é o e-mail de um usuário DESATIVADO não recebe:
    # desativar a pessoa encerra também as comunicações do portal para ela.
    desativados = {u.email.strip().lower() for u in usuarios
                   if u.role == "CLIENTE" and not u.ativo and isinstance(u.email, str)}
    contato = getattr(empresa, "email_contato", None)
    candidatos = [contato] if isinstance(contato, str) and contato.strip().lower() not in desativados else []
    candidatos += [u.email for u in usuarios if u.role == "CLIENTE" and u.ativo and u.email]
    vistos, emails = set(), []
    for endereco in candidatos:
        if not isinstance(endereco, str) or not endereco.strip():
            continue
        chave = endereco.strip().lower()
        if chave not in vistos:
            vistos.add(chave)
            emails.append(endereco.strip())
    return emails


def _sem_aviso_desligado(enderecos: list[str], empresa, aviso: str) -> list[str]:
    """Tira os endereços de quem desligou este aviso em Configurações. O contato da
    empresa sai só se for o e-mail de uma dessas pessoas: ele não pertence a um usuário.
    """
    from preferencias_usuario import ids_com_aviso_desligado
    usuarios = list(getattr(empresa, "usuarios", []) or [])
    desligados = ids_com_aviso_desligado(aviso, [getattr(u, "id_usuario", None) for u in usuarios])
    if not desligados:
        return enderecos
    fora = {u.email.strip().lower() for u in usuarios
            if u.id_usuario in desligados and isinstance(u.email, str)}
    return [e for e in enderecos if e.strip().lower() not in fora]


def _empresa_com_acesso(id_empresa) -> bool:
    """Mesmo critério do sininho (notificar_clientes_empresa): fora da lixeira,
    não cancelada e, no regime contratual, com contrato vigente."""
    from sqlalchemy import select

    from extensions import db
    from models import Empresa
    from session_security import condicoes_empresa_acessivel
    return db.session.scalar(select(Empresa.id_empresa).where(
        Empresa.id_empresa == id_empresa, *condicoes_empresa_acessivel(),
    )) is not None


def _preparo_falhou(tipo: str, referencia: str | None, erro: Exception) -> EnvioEmail:
    """Gatilho que falhou antes de chamar enviar_email (ex.: dado indisponível)."""
    envio = EnvioEmail(tipo, referencia=referencia)
    _registrar(envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING, categoria="template",
               erro=type(erro).__name__)
    return envio


# ---------------------------------------------------------------------
# Gatilhos de alto nível (chamados nos blueprints)
# ---------------------------------------------------------------------
def email_analise_publicada(analise, link_url: str) -> EnvioEmail:
    """E-mail ao cliente quando a devolutiva estratégica é publicada."""
    referencia = f"empresa:{getattr(analise, 'id_empresa', '-')}"
    try:
        ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
        destinatarios = _emails_da_empresa(analise.empresa)
    except Exception as erro:  # noqa: BLE001
        return _preparo_falhou("analise_publicada", referencia, erro)
    return enviar_email(
        destinatarios,
        f"📊 Sua Análise Executiva NEXO de {ref} está disponível",
        "analise_publicada.html",
        tipo="analise_publicada", referencia=referencia,
        analise=analise, ref=ref, link_url=link_url,
    )


def email_ticket_resolvido(chamado, link_url: str) -> EnvioEmail:
    """E-mail ao cliente quando um chamado é marcado como RESOLVIDO."""
    referencia = f"empresa:{getattr(chamado, 'id_empresa', '-')}"
    try:
        # Conta encerrada no regime contratual não recebe o aviso (o link do
        # e-mail levaria a um portal que ela não pode mais abrir).
        destinatarios = (_sem_aviso_desligado(_emails_da_empresa(chamado.empresa), chamado.empresa,
                                              "alerta_chamado")
                         if _empresa_com_acesso(chamado.id_empresa) else [])
        assunto = f"✅ Chamado resolvido: “{chamado.assunto}”"
    except Exception as erro:  # noqa: BLE001
        return _preparo_falhou("ticket_resolvido", referencia, erro)
    return enviar_email(
        destinatarios, assunto, "ticket_resolvido.html",
        tipo="ticket_resolvido", referencia=referencia,
        chamado=chamado, link_url=link_url,
    )


def _referencia_usuario(usuario) -> str:
    return f"usuario:{getattr(usuario, 'id_usuario', '-')}"


def email_recuperacao_senha(usuario, link_url: str, *, pela_equipe: bool = False) -> EnvioEmail:
    """E-mail com o link seguro (15 min) para redefinição de senha. `pela_equipe`: a correção
    do e-mail pelo admin já invalidou a senha, e o texto comum seria falso."""
    try:
        destinatario = usuario.email
    except Exception as erro:  # noqa: BLE001
        return _preparo_falhou("recuperacao_senha", None, erro)
    return enviar_email(
        destinatario,
        "🔐 Redefinição de senha — NEXO",
        "recuperacao_senha.html",
        tipo="recuperacao_senha", referencia=_referencia_usuario(usuario),
        usuario=usuario, link_url=link_url, pela_equipe=pela_equipe,
    )


def email_senha_alterada(usuario, link_recuperacao: str, *, nas_configuracoes: bool = False) -> EnvioEmail:
    """Aviso ao dono da conta após uma troca de senha, por token ou nas Configurações:
    quem não a fez descobre o comprometimento e tem o caminho para retomar o acesso.
    Não carrega token nem senha."""
    try:
        destinatario = usuario.email
    except Exception as erro:  # noqa: BLE001
        return _preparo_falhou("senha_alterada", None, erro)
    return enviar_email(
        destinatario,
        "🔐 A senha da sua conta NEXO foi alterada",
        "senha_alterada.html",
        tipo="senha_alterada", referencia=_referencia_usuario(usuario),
        usuario=usuario, link_recuperacao=link_recuperacao, nas_configuracoes=nas_configuracoes,
    )


def _mascarar_email(endereco: str) -> str:
    local, _, dominio = endereco.partition("@")
    return f"{local[:2]}***@{dominio}"


def email_email_alterado(usuario, email_anterior: str, email_novo: str, *, pela_equipe: bool = False,
                         convite: bool = False) -> EnvioEmail:
    """Aviso ao endereço ANTIGO depois da troca do e-mail de acesso: é ele que descobre
    uma troca que o dono não fez. Mostra o novo endereço mascarado, sem token nem link
    de senha (a recuperação já passou a ir para o novo endereço). `convite`: a conta nunca
    foi ativada, e quem lê pode ser um estranho (cadastro digitado errado): sem o endereço
    novo e sem oferecer o acesso."""
    return enviar_email(
        email_anterior,
        "🔐 O convite de acesso ao NEXO foi cancelado" if convite else "🔐 O e-mail de acesso da sua conta NEXO foi alterado",
        "email_alterado.html",
        tipo="email_alterado", referencia=_referencia_usuario(usuario),
        usuario=usuario, email_novo_mascarado=None if convite else _mascarar_email(email_novo),
        pela_equipe=pela_equipe, convite=convite,
    )


def email_novo_chamado(chamado, copia_oculta: str, rotulo_categoria: str, link_url: str) -> EnvioEmail:
    """Cópia de um novo chamado para a caixa escolhida pelo admin em Configurações.

    Só existe quando há caixa configurada. Vai em cópia oculta, com o remetente do
    NEXO como destinatário visível: o provedor exige um e o endereço da caixa não
    aparece nos cabeçalhos."""
    referencia = f"empresa:{getattr(chamado, 'id_empresa', '-')}"
    try:
        remetente = _remetente(current_app.extensions["mail"].default_sender)[1]
        empresa = chamado.empresa
        nome_empresa = empresa.nome_fantasia or empresa.razao_social
        mensagem = chamado.mensagens[0].mensagem if chamado.mensagens else ""
    except Exception as erro:  # noqa: BLE001
        return _preparo_falhou("novo_chamado", referencia, erro)
    if transporte_configurado(current_app.config) is not None and not email_valido(remetente):
        # O remetente aqui também é o destinatário visível: registrar o problema verdadeiro.
        envio = EnvioEmail("novo_chamado", referencia=referencia)
        _registrar(envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING, categoria="remetente_invalido")
        return envio
    if not email_valido(copia_oculta):
        # Sem a caixa, a mensagem iria só para o próprio remetente.
        envio = EnvioEmail("novo_chamado", referencia=referencia)
        _registrar(envio, EstadoEnvio.FALHA_NO_ENVIO, nivel=logging.WARNING, categoria="destinatario_invalido")
        return envio
    return enviar_email(
        remetente,
        f"🎫 Novo chamado #{chamado.id_chamado} — {nome_empresa}",
        "novo_chamado.html",
        tipo="novo_chamado", referencia=referencia, copia_oculta=copia_oculta,
        chamado=chamado, nome_empresa=nome_empresa, rotulo_categoria=rotulo_categoria,
        mensagem=mensagem, link_url=link_url,
    )


def email_lembrete_renovacao(usuario, data_fim, link_url: str) -> EnvioEmail:
    """Lembrete de que a vigência do contrato está terminando. Síncrono: sai de um
    comando de terminal, cujo processo terminaria antes de uma thread de envio."""
    try:
        destinatario = usuario.email
    except Exception as erro:  # noqa: BLE001
        return _preparo_falhou("lembrete_renovacao", None, erro)
    return enviar_email(
        destinatario,
        f"📅 Seu contrato NEXO vence em {data_fim:%d/%m/%Y}",
        "lembrete_renovacao.html",
        tipo="lembrete_renovacao", referencia=_referencia_usuario(usuario), sincrono=True,
        usuario=usuario, data_fim=data_fim, link_url=link_url,
    )


def email_boas_vindas(usuario, link_definir: str, *, sincrono: bool = False) -> EnvioEmail:
    """
    E-mail de boas-vindas ao novo cliente, com o LINK SEGURO de definição de
    senha (token de 15 min) — sem senha em texto. Disparado direto pelo Flask
    após o cadastro (sem webhook externo). Traz a empresa do cadastro e o
    endereço do portal para o acesso depois da definição da senha.
    """
    try:
        destinatario = usuario.email
        empresa = getattr(usuario, "empresa", None)
        nome_empresa = (getattr(empresa, "nome_fantasia", None) or getattr(empresa, "razao_social", None)
                        if empresa is not None else None)
        from origem_publica import url_publica
        link_portal = url_publica("auth.login")
    except Exception as erro:  # noqa: BLE001
        return _preparo_falhou("boas_vindas", _referencia_usuario(usuario), erro)
    return enviar_email(
        destinatario,
        "👋 Bem-vindo(a) ao NEXO — defina sua senha de acesso",
        "boas_vindas.html",
        tipo="boas_vindas", referencia=_referencia_usuario(usuario), sincrono=sincrono,
        usuario=usuario, link_definir=link_definir, nome_empresa=nome_empresa, link_portal=link_portal,
    )


def email_teste_configuracao(usuario) -> EnvioEmail:
    """E-mail de teste (síncrono) para o próprio administrador, sem token nem
    dado de cliente: confirma se o provedor aceita mensagens com a configuração
    atual. Nunca aceita destinatário arbitrário."""
    try:
        destinatario = usuario.email
    except Exception as erro:  # noqa: BLE001
        return _preparo_falhou("teste", None, erro)
    return enviar_email(
        destinatario,
        "🧪 Teste de envio de e-mail — NEXO",
        "teste_configuracao.html",
        tipo="teste", referencia=_referencia_usuario(usuario), sincrono=True,
        usuario=usuario, instante=datetime.now(timezone.utc),
    )
