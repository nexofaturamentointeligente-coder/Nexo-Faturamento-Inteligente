"""
NEXO - Faturamento Inteligente | Notificações por WhatsApp (Evolution API)
==========================================================================
Camada ATIVA de comunicação: mensagens automáticas ao cliente por uma API REST
compatível com a Evolution API (POST {EVOLUTION_API_URL}/message/sendText/{instância},
cabeçalho "apikey" e corpo {"number": "55DDDNÚMERO", "text": "..."}). A camada
PASSIVA são os links wa.me das telas (macro link_whatsapp em templates/_macros.html).

Configuração (config.py, lida do ambiente; no .env local e no painel da hospedagem):
    EVOLUTION_API_URL        endereço da API, em https (http só em localhost)
    EVOLUTION_API_KEY        chave de autenticação (cabeçalho "apikey"): a global ou,
                             melhor, o token da própria instância
    EVOLUTION_INSTANCE_NAME  nome da instância conectada ao WhatsApp
    WHATSAPP_EMPRESA_NUMERO  número comercial da NEXO com DDI (só os links wa.me)

Regras:
  - Fail-safe: nenhum disparo levanta exceção nem trava o fluxo do usuário. O POST
    roda numa fila de fundo deste processo (uma thread, em ordem), com timeout de 5 s
    por operação de rede (a resolução do nome do host fica fora desse prazo, por isso
    a requisição nunca a espera) e dentro de try/except; API fora do ar, recusa ou
    configuração ausente viram um logger.warning, e a tela, o relatório ou o chamado
    seguem como se nada fosse.
  - Privacidade: o log nunca leva a chave, o texto da mensagem nem o telefone
    inteiro (só os 4 últimos dígitos). A fila só faz HTTP: não toca no banco.
  - O telefone é higienizado antes do disparo (só dígitos, com o DDI 55); na dúvida
    (ramal, número estrangeiro, número de exemplo), o aviso não sai.
  - Gatilhos: relatório pronto, abertura do envio mensal e chamado atualizado.
    A validação do cadastro inicial e a recuperação de senha com token continuam
    exclusivamente por e-mail: este módulo não tem gatilho para elas.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import queue
import re
import threading
import urllib.error
import urllib.request
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import quote, urlsplit

from flask import current_app, has_app_context

logger = logging.getLogger(__name__)

# Prazo de cada operação de rede do disparo (conexão e resposta).
TIMEOUT_SEGUNDOS = 5
# Variáveis da camada ativa, na ordem do .env.example (só os nomes vão para o log).
VARIAVEIS = ("EVOLUTION_API_URL", "EVOLUTION_API_KEY", "EVOLUTION_INSTANCE_NAME")
# Rótulos dos status de ChamadoSuporte (os mesmos das telas de suporte).
ROTULOS_STATUS = {"ABERTO": "Aberto", "EM_ATENDIMENTO": "Em atendimento", "RESOLVIDO": "Resolvido"}

_USER_AGENT = "NEXO-Faturamento-Inteligente/1.0 (WhatsApp)"
_HOSTS_LOCAIS = frozenset({"localhost", "127.0.0.1", "::1"})
# Hosts de exemplo (o do .env.example e os reservados): copiados para o ambiente, mandariam a
# chave e os dados do cliente para um endereço que qualquer um pode registrar.
_HOSTS_DE_EXEMPLO = frozenset({"example.com", "example.net", "example.org"})
_SO_TELEFONE = re.compile(r"\+?[\d\s().\-]+")
_SEPARADORES = re.compile(r"[\s().\-]+")
# Avisos pendentes por processo: acima disso, o aviso novo é descartado com um warning.
_LIMITE_DA_FILA = 200
_LIMITE_TEXTO = 1000
_LIMITE_NOME = 60
_LIMITE_EMPRESA = 80
_LIMITE_STATUS = 40
_MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
          "setembro", "outubro", "novembro", "dezembro")
# DDDs em uso no Brasil (plano de numeração da Anatel).
_DDDS = frozenset({
    11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 24, 27, 28, 31, 32, 33, 34, 35, 37, 38,
    41, 42, 43, 44, 45, 46, 47, 48, 49, 51, 53, 54, 55, 61, 62, 63, 64, 65, 66, 67, 68, 69,
    71, 73, 74, 75, 77, 79, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92, 93, 94, 95, 96, 97, 98, 99,
})


# ---------------------------------------------------------------------------
# Higienização e formatação
# ---------------------------------------------------------------------------
def higienizar_telefone(valor) -> str | None:
    """Telefone brasileiro só com dígitos e o DDI 55: "(62) 98888-7766" -> "5562988887766".

    Na dúvida, não envia: um aviso que se perde é melhor que um aviso no telefone de outra
    pessoa. Por isso:
      - só dígitos e os separadores de telefone (espaço, parênteses, hífen, ponto), com no
        máximo um "+" no começo; letra ("ramal", "Tel:"), barra ou vírgula (dois números)
        deixam o número ambíguo;
      - um grupo final de 1 a 3 dígitos ("3222-1234 21") é ramal ou sobra;
      - com "+" ou "00", o DDI tem de ser o 55 (número estrangeiro não sai), com o 0 de
        longa distância opcional antes do DDD ("+55 (062) ...");
      - sem prefixo internacional, o tamanho decide e não o começo: 10 ou 11 dígitos (o
        DDD 55 do RS inclusive) são nacionais, e 12 ou 13 começando por 55 já trazem o DDI;
      - o DDD tem de existir, o celular de 9 dígitos começa por 9, e o assinante com um só
        dígito repetido ("99999-9999") é o número de exemplo, não um telefone.
    Valor que não é texto nem número também devolve None.
    """
    if isinstance(valor, bool) or not isinstance(valor, (str, int)):
        return None
    texto = str(valor).strip()
    if not _SO_TELEFONE.fullmatch(texto):
        return None
    grupos = [grupo for grupo in _SEPARADORES.split(texto.lstrip("+")) if grupo]
    if len(grupos) > 1 and len(grupos[-1]) < 4:
        return None
    digitos = "".join(grupos)
    if texto.startswith("+") or digitos.startswith("00"):
        digitos = digitos[2:] if digitos.startswith("00") else digitos
        if not digitos.startswith("55"):
            return None
        nacional = digitos[2:]
        nacional = nacional[1:] if nacional.startswith("0") else nacional
    else:
        nacional = digitos[1:] if digitos.startswith("0") else digitos
        if len(nacional) in (12, 13) and nacional.startswith("55"):
            nacional = nacional[2:]
    if len(nacional) not in (10, 11) or int(nacional[:2]) not in _DDDS:
        return None
    assinante = nacional[2:]
    if (len(assinante) == 9 and assinante[0] != "9") or len(set(assinante)) == 1:
        return None
    return "55" + nacional


def mascarar_telefone(numero) -> str:
    """Só os 4 últimos dígitos, para o log ("***9999"); nunca o número inteiro."""
    digitos = re.sub(r"\D", "", numero if isinstance(numero, str) else "")
    return f"***{digitos[-4:]}" if len(digitos) >= 4 else "***"


def texto_seguro(valor, limite: int) -> str:
    """Texto de uma linha: sem caracteres de controle nem quebras, espaços colapsados e
    truncado com reticências. Nome ou status cadastrado não muda a forma da mensagem."""
    if valor is None or isinstance(valor, bool):
        return ""
    limpo = " ".join("".join(ch if ch.isprintable() else " " for ch in str(valor)).split())
    if len(limpo) > limite:
        limpo = limpo[: limite - 1].rstrip() + "…"
    return limpo


def _centavos(valor) -> Decimal | None:
    """O valor em centavos (meio para cima); None se não for número finito representável."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = valor if isinstance(valor, Decimal) else Decimal(str(valor))
        if not numero.is_finite():
            return None
        return numero.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None


def formatar_reais(valor) -> str | None:
    """"R$ 12.345,67" (centavos arredondados meio para cima); None se não for número finito."""
    centavos = _centavos(valor)
    if centavos is None:
        return None
    inteiro, _, decimais = f"{abs(centavos):,.2f}".partition(".")
    return f"{'-' if centavos < 0 else ''}R$ {inteiro.replace(',', '.')},{decimais}"


def mes_por_extenso(referencia) -> str | None:
    """"outubro de 2026" a partir de date/datetime, "2026-10", "2026-10-01" ou "10/2026"."""
    if isinstance(referencia, (date, datetime)):
        ano, mes = referencia.year, referencia.month
    elif isinstance(referencia, str):
        texto = referencia.strip()
        casamento = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", texto)
        if casamento:
            ano, mes = int(casamento.group(1)), int(casamento.group(2))
        else:
            casamento = re.fullmatch(r"(\d{1,2})/(\d{4})", texto)
            if not casamento:
                return None
            mes, ano = int(casamento.group(1)), int(casamento.group(2))
    else:
        return None
    if not 1 <= mes <= 12 or not 2000 <= ano <= 2100:
        return None
    return f"{_MESES[mes - 1]} de {ano}"


def rotulo_status(status) -> str:
    """Rótulo das telas para o código do status ("EM_ATENDIMENTO" -> "Em atendimento")."""
    texto = texto_seguro(status, _LIMITE_STATUS)
    return ROTULOS_STATUS.get(texto.upper().replace(" ", "_"), texto)


def _saudacao(nome_cliente) -> str:
    nome = texto_seguro(nome_cliente, _LIMITE_NOME)
    return f"Olá, {nome}!" if nome else "Olá!"


# ---------------------------------------------------------------------------
# Textos das mensagens automáticas (gatilhos)
# ---------------------------------------------------------------------------
def texto_relatorio_pronto(nome_cliente, empresa, valor_recuperavel, *, acesso_ao_portal: bool = True) -> str:
    """Relatório pronto. O valor só entra quando é um número positivo: sem ele (a análise
    mensal não calcula potencial recuperável), a mensagem avisa só que o relatório saiu.
    Quem não tem acesso ao portal recebe o convite da equipe no lugar do link."""
    nome_empresa = texto_seguro(empresa, _LIMITE_EMPRESA)
    alvo = f"O relatório de {nome_empresa}" if nome_empresa else "O seu relatório"
    partes = [_saudacao(nome_cliente), f"{alvo} está pronto no NEXO Faturamento Inteligente."]
    centavos = _centavos(valor_recuperavel)
    if centavos is not None and centavos > 0:  # R$ 0,004 não vira "R$ 0,00 recuperável"
        partes.append(f"Encontramos {formatar_reais(centavos)} em potencial recuperável.")
    partes.append("Acesse o portal para ver os detalhes." if acesso_ao_portal
                  else "A equipe NEXO vai entrar em contato para apresentar os resultados.")
    return " ".join(partes)


def texto_abertura_mensal(nome_cliente, mes_referencia, *, quinzena=None) -> str:
    """Abertura do envio dos relatórios do mês de referência (mês ilegível: "do mês"); na análise
    quinzenal, a quinzena que abriu ("da 1ª quinzena de outubro de 2026")."""
    mes = mes_por_extenso(mes_referencia)
    if quinzena in (1, 2) and not isinstance(quinzena, bool):
        periodo = f"dos relatórios da {quinzena}ª quinzena " + (f"de {mes}" if mes else "do mês")
    else:
        periodo = f"dos relatórios de {mes}" if mes else "dos relatórios do mês"
    return " ".join((
        _saudacao(nome_cliente),
        f"O envio {periodo} está aberto no NEXO Faturamento Inteligente.",
        "Envie os arquivos pelo portal para receber a análise do mês.",
    ))


def texto_atualizacao_ticket(nome_cliente, ticket_id, novo_status) -> str:
    """Chamado de suporte respondido ou com o status alterado."""
    if isinstance(ticket_id, int) and not isinstance(ticket_id, bool):
        numero = f" #{ticket_id}"
    else:
        identificador = texto_seguro(ticket_id, 20).lstrip("#")
        numero = f" #{identificador}" if identificador else ""
    status = rotulo_status(novo_status)
    partes = [_saudacao(nome_cliente), f"O seu chamado{numero} no NEXO Faturamento Inteligente foi atualizado."]
    if status:
        partes.append(f"Situação atual: {status}.")
    partes.append("Acesse o portal para ver os detalhes.")
    return " ".join(partes)


# ---------------------------------------------------------------------------
# Transporte HTTP (urllib, como o envio de e-mail pelo Brevo)
# ---------------------------------------------------------------------------
class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    """Redirecionamento vira erro (HTTPError 3xx): o aviso nunca segue para outro endereço."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - API do urllib
        return None


_ABRIDOR = urllib.request.build_opener(_SemRedirecionar)


def _abrir(pedido: urllib.request.Request, timeout: float):
    """Único ponto de rede do módulo (os testes trocam este abridor)."""
    return _ABRIDOR.open(pedido, timeout=timeout)


def _host_de_exemplo(host: str) -> bool:
    """O host dos exemplos ("sua-api.onrender.com", do .env.example) e os nomes reservados para
    documentação (RFC 2606), que ninguém deve receber a chave."""
    rotulos = host.lower().rstrip(".").split(".")
    return rotulos[0] == "sua-api" or ".".join(rotulos[-2:]) in _HOSTS_DE_EXEMPLO or rotulos[-1] == "example"


def _url_aceita(url: str) -> bool:
    """https com host, ou http em localhost (Evolution local de desenvolvimento). Sem
    usuário/senha, consulta, fragmento, espaço ou porta inválida (o diagnóstico de
    inicialização acusa o erro em vez de dizer "ativos") e sem host de exemplo: a chave
    nunca vai por canal aberto nem para um endereço copiado da documentação."""
    try:
        if any(ch.isspace() or not ch.isprintable() for ch in url):
            return False
        partes = urlsplit(url)
        host = partes.hostname
        if not host or partes.username or partes.password or partes.query or partes.fragment:
            return False
        partes.port  # noqa: B018 - levanta ValueError com porta não numérica ou fora de 0-65535
        if _host_de_exemplo(host):
            return False
        return partes.scheme == "https" or (partes.scheme == "http" and host in _HOSTS_LOCAIS)
    except ValueError:
        return False


def _configuracao(nome: str) -> str:
    """Valor do app.config dentro do app (o config.py já leu o ambiente: None ali é "não
    configurado", sem segunda consulta ao ambiente) ou do ambiente fora dele, como no
    processo isolado do ETL. Sem espaços nas pontas; vazio vale como ausente."""
    valor = current_app.config.get(nome) if has_app_context() else os.environ.get(nome)
    return valor.strip() if isinstance(valor, str) else ""


class _FilaDeEnvio:
    """Fila de fundo do processo: UMA thread daemon, em ordem de chegada (dois avisos do mesmo
    chamado chegam na ordem em que saíram), com limite de itens. Daemon: o encerramento do
    processo (deploy, reinício, o Render adormecendo) não espera os avisos pendentes, que se
    perdem como um e-mail não enviado. Fila cheia recusa o aviso novo (queue.Full), e o
    notificador registra o warning."""

    def __init__(self, limite: int = _LIMITE_DA_FILA):
        self._fila: queue.Queue = queue.Queue(maxsize=limite)
        self._thread = threading.Thread(target=self._rodar, name="nexo-whatsapp", daemon=True)
        self._thread.start()

    def submit(self, funcao, *args) -> None:
        self._fila.put_nowait((funcao, args))

    def aguardar(self) -> None:
        """Espera a fila esvaziar (testes e roteiros; as rotas nunca esperam)."""
        self._fila.join()

    def _rodar(self) -> None:
        while True:
            funcao, args = self._fila.get()
            try:
                funcao(*args)
            except Exception:  # noqa: BLE001 - a função já registra o próprio aviso
                pass
            finally:
                self._fila.task_done()


_trava_executor = threading.Lock()
_executor: _FilaDeEnvio | None = None
_executor_pid: int | None = None


def _executor_do_processo() -> _FilaDeEnvio:
    """Fila de fundo deste processo, criada no primeiro aviso. Depois de um fork (gunicorn),
    o processo filho cria a dele: threads não atravessam o fork."""
    global _executor, _executor_pid
    with _trava_executor:
        if _executor is None or _executor_pid != os.getpid():
            _executor = _FilaDeEnvio()
            _executor_pid = os.getpid()
        return _executor


# ---------------------------------------------------------------------------
# Notificador
# ---------------------------------------------------------------------------
class WhatsAppNotificador:
    """Disparos ativos pela Evolution API. Nenhum método público levanta exceção.

    Sem argumentos, lê EVOLUTION_API_URL, EVOLUTION_API_KEY e EVOLUTION_INSTANCE_NAME
    do app.config (ou do ambiente). Os métodos notificar_* montam o texto, conferem
    telefone e configuração agora (o motivo de não enviar sai no log desta requisição)
    e entregam o POST à fila de fundo; com assincrono=False, enviam na hora e devolvem
    se a API aceitou. Devolvem False quando nada foi disparado.
    """

    def __init__(self, api_url=None, api_key=None, instancia=None, *, assincrono: bool = True, executor=None):
        if api_url is None and api_key is None and instancia is None:
            api_url, api_key, instancia = (_configuracao(nome) for nome in VARIAVEIS)
        self._api_url = (api_url or "").strip().rstrip("/") if isinstance(api_url, str) else ""
        self._api_key = (api_key or "").strip() if isinstance(api_key, str) else ""
        self._instancia = (instancia or "").strip() if isinstance(instancia, str) else ""
        self.assincrono = assincrono
        self._executor = executor

    def __repr__(self) -> str:
        # Nunca a chave nem a URL: o repr pode parar num log ou num traceback.
        return f"<WhatsAppNotificador configurado={self.configurado} assincrono={self.assincrono}>"

    @property
    def pendencias(self) -> tuple[str, ...]:
        """Variáveis que faltam ou são inválidas (só os nomes, nunca os valores)."""
        faltam = []
        if not self._api_url or not _url_aceita(self._api_url):
            faltam.append("EVOLUTION_API_URL")
        if not self._api_key:
            faltam.append("EVOLUTION_API_KEY")
        if not self._instancia:
            faltam.append("EVOLUTION_INSTANCE_NAME")
        return tuple(faltam)

    @property
    def configurado(self) -> bool:
        return not self.pendencias

    # -- gatilhos -----------------------------------------------------------
    def notificar_relatorio_pronto(self, telefone, nome_cliente, empresa, valor_recuperavel, *,
                                   acesso_ao_portal: bool = True) -> bool:
        """Avaliação/Raio-X concluído ou análise publicada."""
        return self._notificar("relatorio_pronto", telefone, lambda: texto_relatorio_pronto(
            nome_cliente, empresa, valor_recuperavel, acesso_ao_portal=acesso_ao_portal))

    def notificar_abertura_mensal(self, telefone, nome_cliente, mes_referencia, *, quinzena=None) -> bool:
        """Abertura do período mensal (ou da quinzena) de envio dos relatórios."""
        return self._notificar("abertura_mensal", telefone,
                               lambda: texto_abertura_mensal(nome_cliente, mes_referencia, quinzena=quinzena))

    def notificar_atualizacao_ticket(self, telefone, nome_cliente, ticket_id, novo_status) -> bool:
        """Chamado de suporte respondido ou atualizado pela equipe."""
        return self._notificar("chamado_atualizado", telefone,
                               lambda: texto_atualizacao_ticket(nome_cliente, ticket_id, novo_status))

    # -- envio --------------------------------------------------------------
    def enviar_texto(self, telefone, texto, *, gatilho: str = "mensagem") -> bool:
        """POST síncrono de um texto livre. True só quando a API respondeu 2xx."""
        try:
            numero = self._preparar(telefone, gatilho)
            if numero is None:
                return False
            return self._postar(numero, self._texto_final(texto), gatilho)
        except Exception as erro:  # noqa: BLE001 - fail-safe: nada sobe para o fluxo do usuário
            logger.warning("WhatsApp: falha inesperada no aviso '%s' (%s).", gatilho, type(erro).__name__)
            return False

    def _notificar(self, gatilho: str, telefone, montar) -> bool:
        try:
            numero = self._preparar(telefone, gatilho)
            if numero is None:
                return False
            texto = self._texto_final(montar())
            if not self.assincrono:
                return self._postar(numero, texto, gatilho)
            executor = self._executor if self._executor is not None else _executor_do_processo()
            executor.submit(self._postar_sem_levantar, numero, texto, gatilho)
            return True
        except Exception as erro:  # noqa: BLE001 - fail-safe: fila cheia/encerrada, texto, etc.
            logger.warning("WhatsApp: aviso '%s' não enviado (%s).", gatilho, type(erro).__name__)
            return False

    @staticmethod
    def _texto_final(texto) -> str:
        texto = (texto if isinstance(texto, str) else str(texto or "")).strip()
        if not texto:
            raise ValueError("mensagem vazia")
        return texto[:_LIMITE_TEXTO]

    def _preparar(self, telefone, gatilho: str) -> str | None:
        """O número pronto para o POST, ou None com o motivo no log. Sem telefone
        cadastrado não é falha: o cliente só não recebe o aviso por WhatsApp."""
        if telefone is None or (isinstance(telefone, str) and not telefone.strip()):
            logger.debug("WhatsApp: sem telefone cadastrado; aviso '%s' não se aplica.", gatilho)
            return None
        numero = higienizar_telefone(telefone)
        if numero is None:
            logger.warning("WhatsApp: o telefone cadastrado não é um número brasileiro válido; "
                           "aviso '%s' não enviado.", gatilho)
            return None
        if not self.configurado:
            logger.warning("WhatsApp desligado (configure %s): aviso '%s' para %s não enviado.",
                           ", ".join(self.pendencias), gatilho, mascarar_telefone(numero))
            return None
        return numero

    def _postar_sem_levantar(self, numero: str, texto: str, gatilho: str) -> bool:
        # Na fila de fundo uma exceção ficaria muda dentro do Future: vira aviso no log.
        try:
            return self._postar(numero, texto, gatilho)
        except Exception as erro:  # noqa: BLE001
            logger.warning("WhatsApp: falha inesperada no aviso '%s' (%s).", gatilho, type(erro).__name__)
            return False

    def _postar(self, numero: str, texto: str, gatilho: str) -> bool:
        destino = mascarar_telefone(numero)
        url = f"{self._api_url}/message/sendText/{quote(self._instancia, safe='')}"
        corpo = json.dumps({"number": numero, "text": texto}, ensure_ascii=False).encode("utf-8")
        pedido = urllib.request.Request(url, data=corpo, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        })
        # "Unredirected": a chave nunca acompanharia um redirecionamento (que já vira erro).
        pedido.add_unredirected_header("apikey", self._api_key)
        try:
            with _abrir(pedido, TIMEOUT_SEGUNDOS) as resposta:
                status = getattr(resposta, "status", None)
        except urllib.error.HTTPError as erro:
            # O corpo da recusa pode trazer o número e o texto: só o status vai para o log.
            codigo = erro.code
            erro.close()
            logger.warning("WhatsApp: a Evolution API recusou o aviso '%s' para %s (HTTP %s).",
                           gatilho, destino, codigo)
            return False
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as erro:
            logger.warning("WhatsApp: Evolution API indisponível; aviso '%s' para %s não enviado (%s).",
                           gatilho, destino, type(erro).__name__)
            return False
        if not isinstance(status, int) or not 200 <= status < 300:
            logger.warning("WhatsApp: resposta inesperada da Evolution API no aviso '%s' para %s (HTTP %s).",
                           gatilho, destino, status)
            return False
        logger.info("WhatsApp: aviso '%s' aceito pela Evolution API para %s.", gatilho, destino)
        return True


def notificador(**opcoes) -> WhatsAppNotificador:
    """Notificador com a configuração do app (ou do ambiente). Os gatilhos das rotas
    passam por aqui: os testes trocam esta fábrica para observar os disparos."""
    return WhatsAppNotificador(**opcoes)


def configurar(app) -> None:
    """Na factory: WHATSAPP_EMPRESA_NUMERO fica só com dígitos e o DDI 55 (inválido vira vazio,
    o que esconde os botões wa.me), e o log de inicialização diz o estado dos disparos."""
    bruto = app.config.get("WHATSAPP_EMPRESA_NUMERO")
    numero = higienizar_telefone(bruto) or ""
    app.config["WHATSAPP_EMPRESA_NUMERO"] = numero
    app.config["WHATSAPP_EMPRESA_NUMERO_INVALIDO"] = isinstance(bruto, str) and bool(bruto.strip()) and not numero
    registrar_diagnostico_inicial(app)


def registrar_diagnostico_inicial(app) -> None:
    """Uma linha no log de inicialização, sem segredo: o envio ligado, desligado (nada
    configurado) ou a configuração incompleta, que é o caso de aviso."""
    with app.app_context():
        pendencias = WhatsAppNotificador().pendencias
        configuradas = [nome for nome in VARIAVEIS if _configuracao(nome)]
    if not pendencias:
        logger.info("WhatsApp: disparos ativos pela Evolution API (instância configurada).")
    elif not configuradas:
        # Desligado de propósito: linha neutra (INFO em produção, DEBUG fora dela).
        logger.log(logging.INFO if app.config.get("PRODUCAO_HTTP") else logging.DEBUG,
                   "WhatsApp: disparos desligados (EVOLUTION_API_URL, EVOLUTION_API_KEY e "
                   "EVOLUTION_INSTANCE_NAME vazias); os gatilhos só registram aviso.")
    else:
        logger.warning("WhatsApp: configuração incompleta ou inválida (%s); os disparos ficam desligados.",
                       ", ".join(pendencias))
    if app.config.get("WHATSAPP_EMPRESA_NUMERO_INVALIDO"):
        logger.warning("WhatsApp: WHATSAPP_EMPRESA_NUMERO não é um telefone brasileiro válido com DDD; "
                       "os botões de WhatsApp das telas ficam escondidos.")
