"""
NEXO - Faturamento Inteligente | Camada de armazenamento de arquivos
====================================================================
PONTO ÚNICO de acesso ao storage dos relatórios ERP (Vendas/Compras) e das
imagens do Guia. Nenhuma rota lê ou grava arquivo diretamente: todas falam só
com esta interface (salvar / resolver / remover / obter / url_midia).

ONDE OS ARQUIVOS FICAM: no próprio Postgres (tabela `arquivo_armazenado`).
Motivo: em host gratuito (Render Free) o disco é EFÊMERO — o que é gravado no
filesystem some a cada deploy, restart ou hibernação. No banco, o arquivo
sobrevive e ainda entra na MESMA transação do registro que o referencia.

REFERÊNCIAS PERSISTIDAS (ex.: upload_relatorio.caminho_arquivo):
  - 'db:<chave>'  → arquivo guardado no banco (formato atual);
  - qualquer outro valor → caminho LEGADO no filesystem (uploads feitos antes
    desta mudança). Continuam legíveis; migrar_arquivos_para_db.py os copia
    para o banco.
"""

import logging
import os
import re
import stat
from contextlib import contextmanager
import mimetypes
import tempfile
from pathlib import Path

from flask import current_app, url_for
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from etl_erros import ETLInputError

from extensions import db
from models import ArquivoArmazenado, UploadRelatorio
import pos_commit

PREFIXO_DB = "db:"
_REMOCAO_LEGADA = "arquivos_legados"
logger = logging.getLogger(__name__)

# Forma das chaves geradas por salvar(): um nome plano (ou 'guia/<nome>'), sem
# separadores de caminho nem bytes de controle. Os nomes migrados do disco
# (migrar_arquivos_para_db.py) são basenames quaisquer, por isso só a FORMA é
# restringida. Uma chave nunca vira caminho no disco: o ETL lê de uma cópia
# exclusiva com nome fixo (materializar_para_etl).
_NOME_CHAVE = re.compile(r"^(?:guia/)?(?P<nome>[^/\\\x00-\x1f\x7f]{1,200})$")


def chave_valida(chave) -> bool:
    if not isinstance(chave, str):
        return False
    forma = _NOME_CHAVE.match(chave)
    return bool(forma) and forma.group("nome") not in (".", "..")


def _chave(referencia: str) -> str:
    return referencia[len(PREFIXO_DB):]


def salvar(conteudo: bytes, nome_final: str) -> str:
    """Grava os bytes no banco sob a chave `nome_final` e devolve a referência
    persistida ('db:<nome_final>'). NÃO comita: entra na transação do chamador,
    que comita junto com o registro que cita o arquivo (rollback desfaz os dois).
    Regravar a mesma chave substitui o conteúdo (re-upload idêntico)."""
    registro = db.session.get(ArquivoArmazenado, nome_final)
    if registro is None:
        registro = ArquivoArmazenado(chave=nome_final)
        db.session.add(registro)
    registro.conteudo = conteudo
    registro.tamanho = len(conteudo)
    registro.content_type = mimetypes.guess_type(nome_final)[0]
    return PREFIXO_DB + nome_final


def obter(chave: str):
    """Registro ArquivoArmazenado da chave (ou None). Usado para servir mídia."""
    if not chave_valida(chave):
        return None
    return db.session.get(ArquivoArmazenado, chave)


def resolver(caminho_arquivo: str) -> str:
    """Resolve uma referência LEGADA para um caminho absoluto no disco: relativiza
    ao UPLOAD_FOLDER quando o valor persistido não existe como caminho direto.

    'db:<chave>' não tem caminho no disco e levanta ValueError: os bytes saem de
    obter(chave).conteudo, e a cópia para o ETL, de materializar_para_etl (pasta
    temporária exclusiva, apagada ao sair do contexto).
    """
    if caminho_arquivo.startswith(PREFIXO_DB):
        raise ValueError("Referência 'db:' não tem caminho no disco: use storage.obter ou "
                         "storage.materializar_para_etl.")
    return _caminho_legado(caminho_arquivo)


def _pasta_uploads() -> str:
    return os.path.realpath(str(current_app.config["UPLOAD_FOLDER"]))


def _dentro_dos_uploads(caminho: str) -> bool:
    pasta = _pasta_uploads()
    real = os.path.realpath(caminho)
    return real != pasta and real.startswith(pasta + os.sep)


def _caminho_legado(caminho_arquivo: str) -> str:
    """Caminho legado confinado ao UPLOAD_FOLDER (SEC-004).

    O valor persistido só é usado se já aponta para dentro da pasta de
    uploads; caso contrário vale o nome do arquivo dentro dela. Vale também
    para caminho ABSOLUTO obsoleto (projeto que mudou de pasta ou de máquina)
    e para paths do Windows lidos em Linux (basename não separa por "\\" fora
    do Windows). Um valor adulterado no banco nunca leva a ler ou apagar
    arquivos fora da pasta de uploads.
    """
    caminho = caminho_arquivo
    if not os.path.isabs(caminho):
        caminho = os.path.join(_pasta_uploads(), caminho)
    if _dentro_dos_uploads(caminho) and os.path.exists(caminho):
        return caminho
    nome = os.path.basename(caminho_arquivo.replace("\\", "/"))
    if not nome or nome in (".", ".."):
        nome = "arquivo-inexistente"
    return os.path.join(_pasta_uploads(), nome)



@contextmanager
def materializar_para_etl(referencia: str, max_bytes: int):
    """Cópia exclusiva e limitada para ETL; nunca remove a fonte.

    O tamanho real do BYTEA é verificado no servidor e novamente no SELECT que
    carrega os bytes. O campo tamanho não é autoridade.
    Referências legadas mantêm a resolução anterior, mas são copiadas com limite
    para arquivo próprio, regular e privado, removido ao sair do contexto.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ETLInputError("invalid_limits", "Configuração de processamento inválida.")
    if not isinstance(referencia, str) or not referencia:
        raise ETLInputError("storage_unavailable", "Arquivo armazenado indisponível. Reenvie o relatório.")
    extension = Path(referencia).suffix.lower()
    if extension not in {".csv", ".xlsx"}:
        raise ETLInputError("unsupported_format", "Formato armazenado não suportado. Reenvie em CSV ou XLSX.")
    payload = None
    source_fd = None
    try:
        if referencia.startswith(PREFIXO_DB):
            # A chave é só a chave primária da consulta (nunca um caminho): a cópia
            # exclusiva recebe um nome fixo, qualquer que seja o valor persistido.
            chave = _chave(referencia)
            actual_length = func.length(ArquivoArmazenado.conteudo)
            with db.session.no_autoflush:
                size = db.session.scalar(select(actual_length).where(ArquivoArmazenado.chave == chave))
                if size is None:
                    raise ETLInputError("storage_unavailable", "Arquivo armazenado indisponível. Reenvie o relatório.")
                if size > max_bytes:
                    raise ETLInputError("file_size", "Arquivo armazenado acima do tamanho permitido para processamento.")
                payload = db.session.scalar(select(ArquivoArmazenado.conteudo).where(
                    ArquivoArmazenado.chave == chave, actual_length <= max_bytes,
                ))
            # A segunda condição também protege alterações concorrentes do blob.
            if payload is None:
                raise ETLInputError("storage_unavailable", "Arquivo armazenado indisponível. Reenvie o relatório.")
            if not payload:
                raise ETLInputError("empty_file", "Arquivo armazenado vazio. Reenvie o relatório.")
            if len(payload) > max_bytes:
                raise ETLInputError("file_size", "Arquivo armazenado acima do tamanho permitido para processamento.")
        else:
            # NONBLOCK permite recusar FIFO/dispositivo antes de qualquer leitura.
            source_fd = os.open(resolver(referencia), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            info = os.fstat(source_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ETLInputError("storage_unavailable", "Arquivo armazenado indisponível. Reenvie o relatório.")
            if info.st_size > max_bytes:
                raise ETLInputError("file_size", "Arquivo armazenado acima do tamanho permitido para processamento.")
        with tempfile.TemporaryDirectory(prefix="nexo-etl-materialized-") as directory:
            destination = Path(directory) / ("arquivo" + extension)
            with destination.open("wb") as output:
                destination.chmod(0o600)
                if payload is not None:
                    output.write(payload)
                else:
                    total = 0
                    with os.fdopen(source_fd, "rb") as source:
                        source_fd = None
                        while True:
                            chunk = source.read(min(64 * 1024, max_bytes + 1 - total))
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > max_bytes:
                                raise ETLInputError("file_size", "Arquivo armazenado acima do tamanho permitido para processamento.")
                            output.write(chunk)
                    if total == 0:
                        raise ETLInputError("empty_file", "Arquivo armazenado vazio. Reenvie o relatório.")
            # Não reter o BYTEA enquanto o processo isolado calcula os KPIs.
            payload = None
            yield str(destination)
    except OSError:
        raise ETLInputError("storage_unavailable", "Não foi possível preparar o arquivo armazenado. Reenvie o relatório.") from None
    finally:
        if source_fd is not None:
            os.close(source_fd)


def remover(caminho_arquivo: str) -> None:
    """Remove o arquivo referenciado, sempre subordinado ao COMMIT do chamador.

    - 'db:<chave>': apaga a linha na transação do chamador (efetivada no commit
      dele).
    - Legado (SEC-004): o arquivo físico NÃO é apagado aqui. A remoção é
      agendada e só acontece depois de o commit ser confirmado (pos_commit);
      rollback descarta o agendamento. A exclusão é idempotente, confinada ao
      UPLOAD_FOLDER e registra falhas sem expor o caminho.
    """
    if not caminho_arquivo:
        return
    if caminho_arquivo.startswith(PREFIXO_DB):
        chave = _chave(caminho_arquivo)
        if not chave_valida(chave):
            logger.warning("Remoção recusada: chave de arquivo inválida.")
            return
        registro = obter(chave)
        if registro is not None:
            db.session.delete(registro)
        return
    pos_commit.agendar(db.session, _REMOCAO_LEGADA, _caminho_legado(caminho_arquivo))


def _legado_ainda_referenciado(caminho: str) -> bool:
    """Após o COMMIT: outro upload (de qualquer empresa) ainda aponta para o arquivo?"""
    with Session(bind=db.engine) as consulta:
        referencias = consulta.scalars(select(UploadRelatorio.caminho_arquivo).where(
            UploadRelatorio.caminho_arquivo.not_like(PREFIXO_DB + "%"),
        ))
        return any(_caminho_legado(referencia) == caminho for referencia in referencias)


def _remover_legado_confirmado(caminho: str) -> None:
    """Executado após o COMMIT: apaga só arquivo regular dentro de UPLOAD_FOLDER,
    e só quando nenhum upload restante (inclusive de outra empresa) o referencia."""
    try:
        if not _dentro_dos_uploads(caminho):
            logger.warning("Remoção de arquivo legado recusada: fora da pasta de uploads.")
            return
        if _legado_ainda_referenciado(caminho):
            logger.info("Arquivo legado preservado: ainda referenciado por outro upload.")
            return
        if os.path.lexists(caminho) and not os.path.isfile(caminho):
            logger.warning("Remoção de arquivo legado recusada: não é arquivo regular.")
            return
        os.remove(caminho)
    except FileNotFoundError:
        pass  # idempotente: já removido (ou nunca existiu neste disco)
    except OSError as erro:
        logger.warning("Remoção de arquivo legado pendente falhou (%s).", type(erro).__name__)


pos_commit.registrar_processador(_REMOCAO_LEGADA, _remover_legado_confirmado)


def url_midia(referencia: str) -> str:
    """URL pública (para <img src>) de uma mídia do Guia. Referências 'db:' são
    servidas pela rota /midia/; as legadas continuam vindo de static/."""
    if not referencia:
        return ""
    if referencia.startswith(PREFIXO_DB):
        chave = _chave(referencia)
        return url_for("midia", chave=chave) if chave_valida(chave) else ""
    return url_for("static", filename=referencia)
