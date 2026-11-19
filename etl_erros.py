"""Exceções do ETL, só com a biblioteca padrão.

O processo web (rotas, envio, storage, trava do PDV, Raio-X e Avaliação de Cenário) recusa e trata os erros
do ETL importando daqui: etl_inputs e etl_processor carregam pandas, numpy e openpyxl, que só o processo
isolado do ETL (etl_worker) usa. Os dois reexportam estas classes, então isinstance e except valem pelos
dois caminhos.
"""


class ETLInputError(Exception):
    """Somente código e mensagem estáticos são apropriados para a interface."""

    def __init__(self, code, safe_message):
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


class ETLValidationError(ETLInputError):
    """Mantém o tipo público anterior e a identificação segura de limites."""

    def __init__(self, mensagem, code="data"):
        super().__init__(code, mensagem)
