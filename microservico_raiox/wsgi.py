"""Ponto de entrada do gunicorn: gunicorn -w 1 --threads 8 --timeout 60 -b 0.0.0.0:$PORT wsgi:app"""

import logging
import threading

from app import conferir_dependencias, create_app

if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()
# O banco e o bucket conferidos uma vez na subida, sem segurar o /ping do health check.
threading.Thread(target=conferir_dependencias, args=(app,), name="conferencia-da-subida", daemon=True).start()
