"""Ponto de entrada do gunicorn: gunicorn -w 1 --threads 8 --timeout 60 -b 0.0.0.0:$PORT wsgi:app

O gunicorn.conf.py desta pasta, que o gunicorn lê sozinho, tira os dados pessoais do log de acesso e começa a
conferência da subida (banco e bucket) no worker, depois do fork: com o --preload que o Render usa por padrão,
este módulo é importado no processo mestre, e nenhuma thread pode estar viva no fork."""

import logging

from app import create_app

if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()
