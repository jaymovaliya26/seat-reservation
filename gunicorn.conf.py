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

# Gunicorn's runtime control socket is unused here, and the app user has no home directory to
# put it in.
control_socket_disable = True


def on_starting(server: object) -> None:
    """Runs once in the master, before any worker starts."""
    import shutil
    from pathlib import Path

    from app.observability.logging import configure_logging

    # The master's own lines (boot, worker exits, signals) in the same JSON as the app's.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    # Workers share counters through files in this directory. Start each boot from zero, so a
    # restart never mixes in counts from a previous run.
    metrics_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if metrics_dir:
        shutil.rmtree(metrics_dir, ignore_errors=True)
        Path(metrics_dir).mkdir(parents=True, exist_ok=True)


def child_exit(server: object, worker: object) -> None:
    """A worker exited: drop its live gauges (counters keep their totals)."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)  # type: ignore[attr-defined]
