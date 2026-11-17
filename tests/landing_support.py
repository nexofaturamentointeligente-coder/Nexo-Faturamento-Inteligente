"""Apoio dos testes de navegador da landing: rede só local e as respostas falsas dos serviços.

- CDN: o Bootstrap e o Plotly fixados por SRI vêm das cópias íntegras de NEXO_TEST_CDN_DIR (hash conferido);
  sem a cópia, o pedido segue como antes (a CI baixa as cópias), e nenhum outro endereço externo é chamado.
- /documentos-legais/vigentes (frente dos documentos legais): texto NEUTRO de teste, nunca texto jurídico.
- O microsserviço do Raio-X: cada teste responde o POST com o que quiser testar; aqui só a leitura do
  multipart enviado pela landing.
"""

import base64
import hashlib
import json
import os
from email.parser import BytesParser
from email.policy import default as POLITICA_PADRAO
from pathlib import Path

CDN = "https://cdn.jsdelivr.net/npm/"
COPIAS = {
    CDN + "bootstrap@5.3.3/dist/css/bootstrap.min.css": (
        "bootstrap.min.css", "text/css", "QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH"),
    CDN + "bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js": (
        "bootstrap.bundle.min.js", "application/javascript", "YvpcrYf0tY3lHB60NNkmXc5s9fDVZLESaAA55NDzOxhy9GkcIdslK1eN7N6jIeHz"),
    CDN + "plotly.js-dist-min@2.35.2/plotly.min.js": (
        "plotly.min.js", "application/javascript", "cCVCZkAjYNxaYKbM8lsArLznDF/SvMFr1jcZrvOpSTCa0W40ZAdLzHCEulnUa5i7"),
}
SERVICO = "https://nexo-externo.onrender.com"
ROTA_DO_LEAD = SERVICO + "/api/leads/raio-x"
PARAGRAFO_NEUTRO = "Texto neutro de teste, sem valor jurídico, só para ocupar o documento e exigir a rolagem."


def copia_local(url):
    """(caminho, tipo) da cópia íntegra da CDN, ou None."""
    pasta = os.environ.get("NEXO_TEST_CDN_DIR")
    if url not in COPIAS or not pasta:
        return None
    nome, tipo, sri = COPIAS[url]
    arquivo = Path(pasta) / nome
    if not arquivo.is_file():
        return None
    resumo = base64.b64encode(hashlib.sha384(arquivo.read_bytes()).digest()).decode()
    return (str(arquivo), tipo) if resumo == sri else None


def servir_bootstrap(requisicao):
    """O CSS do Bootstrap pela cópia local quando existe; senão, o pedido segue (como na CI)."""
    copia = copia_local(requisicao.request.url)
    if copia:
        requisicao.fulfill(status=200, path=copia[0], content_type=copia[1])
    else:
        requisicao.continue_()


def documentos(paragrafos_termos=14, paragrafos_privacidade=12, ids=(11, 12), rotulo="v1.0"):
    """O JSON de /documentos-legais/vigentes com textos neutros (paragrafos=1: cabe sem rolagem)."""
    def texto(quantos):
        return "\n\n".join(f"{numero}. {PARAGRAFO_NEUTRO}" for numero in range(1, quantos + 1))
    return {"documentos": [
        {"tipo": "TERMOS_USO", "id": ids[0], "rotulo": rotulo, "publicado_em": "2026-11-16T12:00:00Z",
         "conteudo": texto(paragrafos_termos)},
        {"tipo": "POLITICA_PRIVACIDADE", "id": ids[1], "rotulo": rotulo, "publicado_em": "2026-11-16T12:00:00Z",
         "conteudo": texto(paragrafos_privacidade)},
    ]}


def responder_json(requisicao, corpo, status=200, cabecalhos=None):
    requisicao.fulfill(status=status, content_type="application/json", headers=cabecalhos or {},
                       body=json.dumps(corpo, ensure_ascii=False))


def campos_multipart(pedido):
    """({campo: valor}, [(campo, nome_do_arquivo, bytes)]) do multipart que a landing enviou."""
    tipo = pedido.headers.get("content-type", "")
    corpo = pedido.post_data_buffer or b""
    mensagem = BytesParser(policy=POLITICA_PADRAO).parsebytes(
        b"Content-Type: " + tipo.encode("latin-1") + b"\r\n\r\n" + corpo)
    campos, arquivos = {}, []
    for parte in mensagem.iter_parts():
        nome = parte.get_param("name", header="content-disposition")
        arquivo = parte.get_filename()
        conteudo = parte.get_payload(decode=True) or b""
        if arquivo is None:
            campos[nome] = conteudo.decode("utf-8")
        else:
            arquivos.append((nome, arquivo, conteudo))
    return campos, arquivos
