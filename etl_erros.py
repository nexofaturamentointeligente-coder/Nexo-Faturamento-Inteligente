"""Exceções do ETL, só com a biblioteca padrão.

O processo web (rotas, envio, storage, trava do PDV, Raio-X e Avaliação de Cenário) recusa e trata os erros
do ETL importando daqui: etl_inputs e etl_processor carregam pandas, numpy e openpyxl, que só o processo
isolado do ETL (etl_worker) usa. Os dois reexportam estas classes, então isinstance e except valem pelos
dois caminhos.
"""

import errno as _errno


class ETLInputError(Exception):
    """Somente código e mensagem estáticos são apropriados para a interface."""

    def __init__(self, code, safe_message):
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


# Falhas do servidor, não do arquivo enviado: processo isolado interrompido ou sem resultado,
# resultado incompleto, disco, motor que não carrega, prazo esgotado, vaga ocupada, plataforma sem
# o isolamento exigido. Nelas o arquivo e o último resultado válido ficam como estavam: nada vira
# ERRO, e a mensagem pede outra tentativa, não outro arquivo.
CODIGO_INFRAESTRUTURA = "infra"
CODIGOS_DE_INFRAESTRUTURA = frozenset({CODIGO_INFRAESTRUTURA, "timeout", "busy", "platform", "invalid_limits"})
# Falta de espaço ou de recurso do sistema (gravação ou leitura de temporários): do servidor.
ERRNOS_DE_DISCO = frozenset(filter(None, (getattr(_errno, nome, None) for nome in ("ENOSPC", "EDQUOT", "EFBIG"))))
MENSAGEM_INFRAESTRUTURA = ("Falha temporária do servidor ao processar o arquivo. Nada foi alterado: "
                           "tente novamente em instantes.")


def eh_falha_de_infraestrutura(erro_ou_codigo) -> bool:
    """True para um ETLInputError (ou um código) de falha do servidor (CODIGOS_DE_INFRAESTRUTURA)."""
    codigo = erro_ou_codigo if isinstance(erro_ou_codigo, str) else getattr(erro_ou_codigo, "code", None)
    return codigo in CODIGOS_DE_INFRAESTRUTURA


class ETLValidationError(ETLInputError):
    """Mantém o tipo público anterior e a identificação segura de limites."""

    def __init__(self, mensagem, code="data"):
        super().__init__(code, mensagem)
