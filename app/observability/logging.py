"""JSON logs on stdout, one object per line, for our code and for uvicorn, gunicorn and asyncpg."""

import logging
import sys

import orjson
import structlog

_SHARED_PROCESSORS: list[structlog.typing.Processor] = [
    structlog.contextvars.merge_contextvars,  # request_id and friends, bound per request
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
]


def _component_from_stdlib_logger(
    _: object, __: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Label uvicorn/gunicorn/asyncpg lines the same way our loggers label theirs."""
    record = event_dict.get("_record")
    if record is not None:
        event_dict["component"] = record.name
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    level_no = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.processors.dict_tracebacks,
            structlog.processors.EventRenamer("msg"),
            structlog.processors.JSONRenderer(serializer=orjson.dumps),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        logger_factory=structlog.BytesLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route standard-library loggers through the same JSON shape.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[*_SHARED_PROCESSORS, _component_from_stdlib_logger],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.dict_tracebacks,
                structlog.processors.EventRenamer("msg"),
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level_no)

    for name in ("uvicorn", "uvicorn.error", "gunicorn", "gunicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # Our middleware writes one richer line per request instead.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("gunicorn.access").disabled = True
