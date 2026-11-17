"""Configuração do gunicorn do microsserviço, lida sozinha da pasta onde ele roda (o diretório raiz no Render).

Log de acesso sem dado pessoal. O Render liga o log de acesso por padrão nos serviços Python
(GUNICORN_CMD_ARGS="--preload --access-logfile - ..."), e o formato padrão do gunicorn leva o IP do visitante (já
trocado pelo ProxyFix), o User-Agent, o Referer e a query string. Aqui ficam só o instante, o método, o caminho
sem a query, o status e a duração. O ambiente não fixa o formato, então este vale; uma opção na linha de comando
venceria os dois.

Conferência da subida no worker. Com o --preload do Render, o wsgi.py é importado no processo mestre, antes do
fork do worker, e uma thread viva no fork (a conferência abre conexão com o banco e com o Storage) pode deixar
travas presas no processo filho. Por isso a conferência começa aqui, no worker, depois do fork, com ou sem
--preload. Ela só escreve o resultado no log e não segura o /ping.
"""

import threading

access_log_format = '%(t)s "%(m)s %(U)s" %(s)s %(L)s'


def post_worker_init(worker):
    from app import conferir_dependencias

    threading.Thread(target=conferir_dependencias, args=(worker.wsgi,), name="conferencia-da-subida",
                     daemon=True).start()
