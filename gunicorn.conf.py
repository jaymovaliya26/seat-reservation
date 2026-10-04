"""Gunicorn supervises the processes; each worker is a Uvicorn event loop running the ASGI app."""

import os

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "4"))
worker_class = "uvicorn_worker.UvicornWorker"

# Connections the kernel queues before a worker accepts them. During an on-sale burst
# thousands of clients connect at once; they should wait here, not be refused.
backlog = 4096
keepalive = 5

# On SIGTERM (deploy or restart) workers stop accepting and get this long to finish
# in-flight requests before the connection pool is closed.
graceful_timeout = 20

# A worker that stops responding to the master for this long is killed and replaced.
timeout = 60

# The app writes its own JSON line per request.
accesslog = None
