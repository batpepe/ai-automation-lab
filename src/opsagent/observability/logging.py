"""structlog wiring.

Configured once at process start. Everything else in the codebase calls
`structlog.get_logger()` and never touches handlers or formatters, so log shape
is decided in exactly one place.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import Processor


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Configure structlog for the whole process.

    Args:
        level: Standard logging level name, case insensitive.
        json_output: Render JSON lines for log shipping. When false, render the
            human readable console format used during local work.
    """
    numeric_level = logging.getLevelNamesMapping()[level.upper()]

    # stdlib logging still matters: third party libraries (httpx, kubernetes,
    # sqlalchemy) log through it, and without this their records never appear.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        # Reconfiguring mid-process is a mistake rather than a use case, so the
        # bound logger is cached. Tests assert on the configuration itself.
        cache_logger_on_first_use=True,
    )
