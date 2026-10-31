"""Structured logging for the API and the worker.

Every line is a structlog event. Inside a request, the request middleware binds `request_id`, `method`, `path`
and (once authenticated) `user_id`, so all lines of one request can be followed together. Local development
renders readable, coloured lines; production renders one JSON object per line for the log platform.
"""

import logging
import sys
from typing import Literal

import structlog

LogFormat = Literal["console", "json"]

# Third-party loggers that are noisy at INFO. httpx would also print every provider URL.
_QUIET = ("httpx", "httpcore", "uvicorn.access", "multipart")


def configure_logging(level: str, fmt: LogFormat = "json") -> None:
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        if fmt == "console"
        else structlog.processors.JSONRenderer()
    )
    final: list[structlog.typing.Processor] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if fmt == "json":
        final.append(structlog.processors.format_exc_info)  # the console renderer prints tracebacks itself
    final.append(renderer)

    structlog.configure(
        processors=[structlog.stdlib.filter_by_level, *shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,  # keeps loggers reconfigurable (tests capture events)
    )

    # Route stdlib logging (uvicorn, sqlalchemy, …) through the same processors so all output looks alike.
    handler = logging.StreamHandler()
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(foreign_pre_chain=shared, processors=final))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
