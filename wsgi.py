"""
NEXO - Faturamento Inteligente | Entrypoint WSGI de produção
============================================================
Usado pelo gunicorn no deploy (ver render.yaml):

    gunicorn -w 1 --threads 50 --timeout 180 -b 0.0.0.0:$PORT wsgi:app

Por que 1 worker com threads: o rate limit (memory://) fica na memória do
processo. Com mais de um worker, cada processo teria a sua contagem. As threads
atendem as requisições concorrentes; como o sininho usa polling leve (uma
requisição curta a cada 30 s por aba), nenhuma conexão longa prende thread.

`python app.py` continua sendo o jeito de subir em desenvolvimento local.
"""

import os

from app import create_app

app = create_app(os.environ.get("FLASK_ENV", "production"))
