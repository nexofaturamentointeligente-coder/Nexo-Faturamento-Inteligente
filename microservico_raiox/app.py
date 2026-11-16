"""Microsserviço do Raio-X: captação de leads da landing da NEXO (no monorepo, independente da aplicação principal).

Rotas:
  GET  /  e  GET /ping        {"status": "online", "service": "nexo-raiox"}: keep-alive, sem banco nem Storage.
  POST /api/leads/raio-x      multipart/form-data com nome, empresa, whatsapp, email, interesse (1, 2 ou 3),
                              termos_aceitos e, só no interesse 1, os relatórios no campo "arquivos".
  OPTIONS /api/leads/raio-x   preflight do CORS: só as origens configuradas, só POST.

Ordem do envio: origem (403), limite por IP (429), configuração (503), formato do pedido (415), tamanho (413),
campo isca, aceite dos termos e campos (400), regra dos arquivos pelo interesse (400), teto diário do serviço
(429), triagem de cada relatório (uma por vez no processo, com no máximo dois envios na fila e prazo por envio;
fila cheia, 503 na hora), gravação dos aprovados no Storage e, por último, o lead no banco, onde o teto diário é
conferido de novo sob trava. Se o Storage ou o banco falham, os arquivos deste envio (inclusive o que estava a
caminho) são apagados e a resposta é 503; o que estava a caminho e não foi achado para apagar vai para o log,
porque o Storage pode terminar de gravá-lo depois. Se o banco não confirma a gravação, o serviço confere numa
conexão nova se o lead ficou gravado antes de apagar qualquer coisa; sem resposta nem assim, os arquivos ficam e
os caminhos vão para o log, para a conferência. A resposta 201 traz o protocolo (os 8 primeiros caracteres do id
do lead), a situação dos arquivos e o motivo de cada recusa. O log nunca leva nome, e-mail, WhatsApp, IP nem a
configuração.
"""

from collections import deque
from datetime import datetime, time as horario, timedelta, timezone
import json
import logging
import threading
import time
import uuid

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from armazenamento import ArmazenamentoSupabase, ErroDeArmazenamento
from configuracao import MEGABYTE, Configuracao
from limite import LimitePorIp
from repositorio import ErroDeBanco, RepositorioDeLeads, TetoDoDiaAtingido
import sanitizacao
import validacao

ROTA_DO_LEAD = "/api/leads/raio-x"
SERVICO = {"status": "online", "service": "nexo-raiox"}
# O Brasil não tem horário de verão desde 2019: o dia do teto diário é o de UTC-3.
BRASILIA = timezone(timedelta(hours=-3), "BRT")
# Os campos de texto do formulário ficam fora do teto dos relatórios.
FOLGA_DO_FORMULARIO = 64 * 1024
# Teto de memória do formulário no Werkzeug: vale para cada campo de texto e também para o buffer interno do
# decodificador de multipart, que lê blocos de 64 KB; abaixo de uns 128 KB, qualquer relatório maior que um bloco
# seria recusado com 413. Os campos de texto têm os próprios limites na validação.
MEMORIA_DO_FORMULARIO = 256 * 1024
# Uma triagem por vez no processo (a de pior caso medida passa pouco de 100 MB, somados aos relatórios do envio;
# a instância pequena tem 512 MB), em ordem de chegada. Na fila, no máximo dois envios com relatório esperam a
# vez: cada um prende uma das 8 threads do gunicorn, e sobram threads para o /ping do health check e para os
# envios sem relatório, que não entram na fila. O que passa disso recebe 503 na hora, com Retry-After. A espera
# cobre a triagem em curso e a da frente na fila, cada uma no seu prazo, com folga.
FILA_DA_TRIAGEM = 2
FOLGA_DA_ESPERA = 10
# As contagens de entradas do X-Forwarded-For que vão para o log (para conferir PROXIES_CONFIAVEIS no serviço
# real), cada uma na primeira vez: de 0 a 6, e "7 ou mais". Nenhum cliente enche o log nem gasta as vagas.
MAIOR_CONTAGEM_DE_PROXY = 7
CABECALHOS_PERMITIDOS = {"content-type", "accept", "accept-language"}
INDISPONIVEL = "Serviço indisponível no momento. Tente de novo em instantes."
TETO_ATINGIDO = "O limite de envios de hoje foi atingido. Tente de novo amanhã."
MENSAGENS_HTTP = {400: "Pedido inválido.", 404: "Endereço não encontrado.", 405: "Método não permitido.",
                  413: "O envio passa do tamanho permitido.", 415: "Formato de envio não aceito.",
                  429: "Muitos envios. Tente mais tarde.", 500: "Erro interno.", 503: INDISPONIVEL}

log = logging.getLogger("nexo_raiox")
# Sem configuração de log (testes, importação), nada vai para a saída padrão; o wsgi.py configura a da produção.
log.addHandler(logging.NullHandler())


def _erro(mensagem: str, status: int, campo: str = None):
    corpo = {"erro": mensagem}
    if campo:
        corpo["campo"] = campo
    return jsonify(corpo), status


def _inicio_do_dia() -> datetime:
    hoje = datetime.now(BRASILIA).date()
    return datetime.combine(hoje, horario.min, tzinfo=BRASILIA)


class FilaDaTriagem:
    """Uma triagem por vez, em ordem de chegada (um semáforo não garante a ordem, e quem chega na troca passaria à
    frente de quem já esperava), com no máximo `capacidade` envios dentro: o da triagem e os da fila."""

    def __init__(self, capacidade: int):
        self._capacidade = capacidade
        self._senhas = deque()
        self._condicao = threading.Condition()

    def __len__(self) -> int:
        with self._condicao:
            return len(self._senhas)

    def entrar(self):
        """A senha do envio, ou None com a fila cheia."""
        with self._condicao:
            if len(self._senhas) >= self._capacidade:
                return None
            senha = object()
            self._senhas.append(senha)
            return senha

    def esperar_a_vez(self, senha, espera: float) -> bool:
        with self._condicao:
            return self._condicao.wait_for(lambda: self._senhas[0] is senha, espera)

    def sair(self, senha) -> None:
        with self._condicao:
            self._senhas.remove(senha)
            self._condicao.notify_all()


def _indisponivel_por(segundos: int):
    resposta, status = _erro(INDISPONIVEL, 503)
    resposta.headers["Retry-After"] = str(max(segundos, 1))
    return resposta, status


def _situacao_dos_arquivos(interesse: str, resultados: list) -> str:
    if interesse != "raio_x":
        return "sem_arquivo"
    aprovados = sum(1 for resultado in resultados if resultado.aprovado)
    if aprovados == len(resultados):
        return "aprovado"
    return "parcial" if aprovados else "recusado"


def create_app(configuracao: Configuracao = None, repositorio=None, armazenamento=None, limite=None) -> Flask:
    configuracao = configuracao or Configuracao.de_ambiente()
    app = Flask(__name__)
    # Folga de partes para que o excesso de relatórios caia no 400 da contagem, com a mensagem certa.
    app.config.update(MAX_CONTENT_LENGTH=configuracao.max_bytes + FOLGA_DO_FORMULARIO,
                      MAX_FORM_MEMORY_SIZE=MEMORIA_DO_FORMULARIO, MAX_FORM_PARTS=3 * configuracao.max_arquivos + 20)
    app.json.ensure_ascii = False
    if configuracao.proxies_confiaveis:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=configuracao.proxies_confiaveis, x_proto=1)
    for problema in configuracao.problemas:
        log.error("Configuração incompleta: %s", problema)
    # Banco e Storage nascem mesmo com outra falta (a origem fica vazia até a LGPD), para a conferência da
    # subida. Um erro aqui não derruba o serviço: o /ping continua no ar e o envio responde 503.
    if repositorio is None and configuracao.database_url:
        try:
            repositorio = RepositorioDeLeads(configuracao.database_url)
        except ErroDeBanco as erro:
            log.error("Banco não configurado (%s): os envios respondem 503.", erro)
    storage_configurado = not any(problema.startswith(("SUPABASE_URL", "SUPABASE_KEY"))
                                  for problema in configuracao.problemas)
    if armazenamento is None and configuracao.supabase_url and configuracao.supabase_key and storage_configurado:
        try:
            armazenamento = ArmazenamentoSupabase(configuracao.supabase_url, configuracao.supabase_key,
                                                  configuracao.bucket)
        except ValueError:
            # Sem o texto da exceção: ele pode trazer o valor da variável.
            log.error("Storage não configurado: os envios respondem 503.")
    app.extensions["nexo_raiox_dependencias"] = (repositorio, armazenamento)
    limite = limite or LimitePorIp(configuracao.limite_por_hora, configuracao.limite_por_dia)
    fila = app.extensions["nexo_raiox_fila"] = FilaDaTriagem(1 + FILA_DA_TRIAGEM)
    app.config["NEXO_ESPERA_PELA_TRIAGEM"] = FILA_DA_TRIAGEM * max(configuracao.prazo_da_triagem, 0) + FOLGA_DA_ESPERA
    teto_em_mb = configuracao.max_bytes // MEGABYTE
    contagens_de_proxy, trava_das_contagens = set(), threading.Lock()

    @app.before_request
    def registrar_proxies():
        """Quantas entradas o X-Forwarded-For trouxe (nunca os IPs), cada contagem na primeira vez: com o serviço no
        ar, um GET /ping feito de fora mostra no log o valor de PROXIES_CONFIAVEIS que acha o IP do visitante."""
        entradas = sum(1 for parte in request.headers.get("X-Forwarded-For", "").split(",") if parte.strip())
        entradas = min(entradas, MAIOR_CONTAGEM_DE_PROXY)
        with trava_das_contagens:
            if entradas in contagens_de_proxy:
                return
            contagens_de_proxy.add(entradas)
        log.info("X-Forwarded-For com %s entrada(s); PROXIES_CONFIAVEIS=%s.",
                 f"{entradas} ou mais" if entradas == MAIOR_CONTAGEM_DE_PROXY else entradas,
                 configuracao.proxies_confiaveis)

    @app.after_request
    def cabecalhos(resposta):
        resposta.headers["X-Content-Type-Options"] = "nosniff"
        resposta.headers["Cache-Control"] = "no-store"
        resposta.headers["Referrer-Policy"] = "no-referrer"
        resposta.headers["X-Frame-Options"] = "DENY"
        resposta.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if request.path == ROTA_DO_LEAD:
            resposta.vary.add("Origin")
            origem = request.headers.get("Origin")
            if configuracao.origem_permitida(origem):
                resposta.headers["Access-Control-Allow-Origin"] = origem
                # A landing lê o Retry-After do 429 para dizer quando tentar de novo.
                resposta.headers["Access-Control-Expose-Headers"] = "Retry-After"
        return resposta

    @app.get("/")
    @app.get("/ping")
    def ping():
        return jsonify(SERVICO)

    def preflight():
        origem = request.headers.get("Origin")
        metodo = (request.headers.get("Access-Control-Request-Method") or "").strip().upper()
        pedidos = [nome.strip().lower() for nome in (request.headers.get("Access-Control-Request-Headers") or "")
                   .split(",") if nome.strip()]
        if not configuracao.origem_permitida(origem) or metodo != "POST" or not set(pedidos) <= CABECALHOS_PERMITIDOS:
            return _erro("Origem, método ou cabeçalho não permitido.", 403)
        resposta = app.response_class(status=204)
        resposta.headers["Access-Control-Allow-Methods"] = "POST"
        if pedidos:
            resposta.headers["Access-Control-Allow-Headers"] = ", ".join(pedidos)
        resposta.headers["Access-Control-Max-Age"] = "600"
        return resposta

    def desfazer(caminhos: list, em_curso=None) -> None:
        for caminho in caminhos:
            try:
                apagado = armazenamento.apagar(caminho)
            except Exception as erro:  # noqa: BLE001 - todo relatório que fica tem de aparecer no log
                motivo = erro if isinstance(erro, ErroDeArmazenamento) else type(erro).__name__
                log.error("Relatório órfão no Storage, apagar à mão: %s (%s).", caminho, motivo)
                continue
            if caminho == em_curso and apagado is False:
                # O envio falhou sem resposta (prazo, gateway) e o Storage pode terminar de gravar depois do DELETE.
                log.warning("Relatório a caminho não achado para apagar; o Storage pode gravá-lo depois: conferir e "
                            "apagar à mão: %s.", caminho)

    def triar(arquivos: list):
        """Os resultados da triagem, ou None se o envio passa do teto somado. Os relatórios só são lidos para a
        memória na vez do envio; até lá ficam nos arquivos temporários do Werkzeug. O prazo vale para o envio
        inteiro: o relatório que não termina a tempo é recusado, e o serviço não fica preso nele."""
        prazo = time.monotonic() + configuracao.prazo_da_triagem
        recebidos = [(arquivo.filename, arquivo.mimetype, arquivo.read(configuracao.max_bytes + 1))
                     for arquivo in arquivos]
        for arquivo in arquivos:
            arquivo.close()  # o arquivo temporário do Werkzeug sai já aqui, não só no fim do pedido
        if sum(len(conteudo) for _nome, _tipo, conteudo in recebidos) > configuracao.max_bytes:
            return None
        return [sanitizacao.triar(nome, tipo, conteudo, configuracao.max_bytes, prazo)
                for nome, tipo, conteudo in recebidos]

    @app.route(ROTA_DO_LEAD, methods=["POST", "OPTIONS"], provide_automatic_options=False)
    def receber_lead():
        if request.method == "OPTIONS":
            return preflight()
        if not configuracao.origem_permitida(request.headers.get("Origin")):
            log.warning("Envio recusado: origem não permitida.")
            return _erro("Origem não permitida.", 403)
        espera = limite.registrar(request.remote_addr or "desconhecido")
        if espera:
            resposta, status = _erro("Muitos envios deste endereço. Tente mais tarde.", 429)
            resposta.headers["Retry-After"] = str(espera)
            return resposta, status
        if not configuracao.pronta or repositorio is None or armazenamento is None:
            return _erro(INDISPONIVEL, 503)
        if request.mimetype != "multipart/form-data":
            return _erro("Envie o formulário como multipart/form-data.", 415)
        formulario = request.form
        if formulario.get("campo_isca"):
            log.info("Envio recusado pelo campo isca.")
            return _erro("Não foi possível enviar.", 400)
        try:
            validacao.termos_aceitos(formulario.get("termos_aceitos"))
            nome = validacao.texto_obrigatorio(formulario.get("nome"), "nome", "Informe o nome.")
            empresa = validacao.texto_obrigatorio(formulario.get("empresa"), "empresa", "Informe o nome da empresa.")
            email = validacao.email(formulario.get("email"))
            whatsapp = validacao.whatsapp(formulario.get("whatsapp"))
            interesse = validacao.interesse(formulario.get("interesse"))
        except validacao.DadoInvalido as erro:
            return _erro(str(erro), 400, erro.campo)

        if any(chave != "arquivos" for chave in request.files):
            return _erro("Envie os relatórios só no campo arquivos.", 400, "arquivos")
        arquivos = [arquivo for arquivo in request.files.getlist("arquivos") if arquivo and arquivo.filename]
        if interesse != "raio_x" and arquivos:
            return _erro("Relatórios só são aceitos no interesse 1 (Raio-X).", 400, "arquivos")
        if interesse == "raio_x" and not arquivos:
            return _erro("Envie ao menos um relatório para o Raio-X.", 400, "arquivos")
        if len(arquivos) > configuracao.max_arquivos:
            return _erro(f"Envie no máximo {configuracao.max_arquivos} relatórios por vez.", 400, "arquivos")

        # Antes do trabalho pesado; a conferência exata é a da gravação, sob trava.
        inicio_do_dia = _inicio_do_dia()
        try:
            if repositorio.leads_desde(inicio_do_dia) >= configuracao.teto_diario:
                log.warning("Envio recusado: teto diário do serviço atingido.")
                return _erro(TETO_ATINGIDO, 429)
        except ErroDeBanco as erro:
            log.warning("Teto diário não conferido (%s).", erro)
            return _erro(INDISPONIVEL, 503)

        resultados = []
        if arquivos:  # o envio sem relatório não entra na fila da triagem
            senha = fila.entrar()
            if senha is None:
                log.warning("Envio recusado: fila da triagem cheia.")
                return _indisponivel_por(configuracao.prazo_da_triagem)
            try:
                if not fila.esperar_a_vez(senha, app.config["NEXO_ESPERA_PELA_TRIAGEM"]):
                    log.warning("Envio recusado: a vez na triagem não chegou a tempo.")
                    return _indisponivel_por(configuracao.prazo_da_triagem)
                resultados = triar(arquivos)
            finally:
                fila.sair(senha)
        if resultados is None:
            return _erro(f"Os relatórios passam de {teto_em_mb} MB somados.", 413, "arquivos")

        lead_id = uuid.uuid4()
        protocolo = str(lead_id).split("-", 1)[0].upper()
        agora = datetime.now(timezone.utc)
        enviados, registros = [], []
        em_curso, concluido = None, False
        try:
            try:
                for indice, resultado in enumerate((r for r in resultados if r.aprovado), 1):
                    # Caminho sem nada que venha do visitante: mês, id do lead, ordem e o começo do SHA-256.
                    caminho = f"{agora:%Y/%m}/{lead_id}/{indice:02d}-{resultado.sha256[:16]}.{resultado.formato}"
                    # Antes do envio: o Storage pode gravar e a resposta se perder (prazo, 502 do gateway).
                    enviados.append(caminho)
                    em_curso = caminho
                    armazenamento.enviar(caminho, resultado.conteudo, resultado.tipo)
                    em_curso = None
                    registros.append({"caminho": caminho, "nome": resultado.nome, "bytes": resultado.tamanho,
                                      "formato": resultado.formato, "sha256": resultado.sha256})
            except ErroDeArmazenamento as erro:
                log.warning("Relatórios não gravados no Storage (%s); os deste envio serão apagados.", erro)
                return _erro("Não foi possível guardar os relatórios agora. Tente de novo em instantes.", 503)

            situacao = _situacao_dos_arquivos(interesse, resultados)
            lead = {"id": lead_id, "nome_contato": nome, "email": email, "whatsapp": whatsapp, "nome_loja": empresa,
                    "interesse": interesse, "status_arquivo": situacao,
                    "arquivos_storage": json.dumps(registros, ensure_ascii=False) if registros else None,
                    "termos_versao": configuracao.termos_versao}
            try:
                repositorio.gravar(lead, teto=configuracao.teto_diario, desde=inicio_do_dia)
            except TetoDoDiaAtingido:
                log.warning("Envio recusado na gravação: teto diário do serviço atingido.")
                return _erro(TETO_ATINGIDO, 429)
            except ErroDeBanco as erro:
                # O COMMIT pode ter sido aplicado e só a resposta ter se perdido: confere antes de apagar.
                try:
                    gravado = repositorio.existe(lead_id)
                except ErroDeBanco as erro_da_conferencia:
                    concluido = True  # sem como saber, nada é apagado
                    log.error("Lead %s sem confirmação (%s; %s); relatórios mantidos para conferir: %s.", protocolo,
                              erro, erro_da_conferencia, ", ".join(enviados) or "nenhum")
                    return _erro(INDISPONIVEL, 503)
                if not gravado:
                    log.warning("Lead não gravado (%s); os relatórios deste envio serão apagados.", erro)
                    return _erro(INDISPONIVEL, 503)
                log.warning("Lead %s gravado, confirmado depois de erro na resposta do banco (%s).", protocolo, erro)
            concluido = True
        finally:
            if not concluido:
                desfazer(enviados, em_curso)

        log.info("Lead gravado: protocolo %s, interesse %s, relatórios aprovados %s de %s.", protocolo, interesse,
                 len(registros), len(resultados))
        return jsonify({
            "status": "recebido", "protocolo": protocolo, "interesse": interesse, "status_arquivo": situacao,
            "arquivos": [{"nome": resultado.nome, "situacao": "aprovado" if resultado.aprovado else "recusado",
                          **({"motivo": resultado.motivo} if resultado.motivo else {})} for resultado in resultados],
        }), 201

    @app.errorhandler(RequestEntityTooLarge)
    def grande_demais(_erro_http):
        # Vale para o tamanho total, para um campo de texto enorme e para partes demais no formulário.
        return _erro(f"O envio passa do permitido: até {teto_em_mb} MB somados e {configuracao.max_arquivos} "
                     "relatórios por vez.", 413)

    @app.errorhandler(HTTPException)
    def erro_http(erro_http):
        return _erro(MENSAGENS_HTTP.get(erro_http.code, "Pedido inválido."), erro_http.code or 500)

    @app.errorhandler(Exception)
    def erro_inesperado(erro):
        log.error("Erro inesperado (%s).", type(erro).__name__)
        return _erro(MENSAGENS_HTTP[500], 500)

    return app


def conferir_dependencias(app: Flask) -> None:
    """Na subida (o wsgi.py chama numa thread à parte, para não atrasar o /ping): o banco, lendo a tabela pelas
    colunas da migração, e o bucket, que tem de ser privado. Só o resultado vai para o log, sem URL nem chave; o
    que não está configurado já apareceu como problema de configuração."""
    repositorio, armazenamento = app.extensions["nexo_raiox_dependencias"]
    if repositorio is not None:
        try:
            repositorio.conferir_tabela()
        except ErroDeBanco as erro:
            log.error("Conferência da subida: banco com problema (%s); os envios vão responder 503.", erro)
        else:
            log.info("Conferência da subida: banco ok, com as colunas da migração f2b6d8a4c1e9.")
    if armazenamento is not None:
        try:
            privado = armazenamento.conferir_bucket()
        except ErroDeArmazenamento as erro:
            log.error("Conferência da subida: bucket inacessível (%s); os envios com relatório vão responder 503.",
                      erro)
        else:
            if privado:
                log.info("Conferência da subida: bucket ok, privado.")
            else:
                log.error("Conferência da subida: o bucket NÃO é privado; torne-o privado antes de abrir a origem.")
