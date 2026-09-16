"""Logging setup (#2): one place that decides format and level."""

from __future__ import annotations

import logging

from app.config import Settings

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s"


class _RequestIdFilter(logging.Filter):
    """Ensure ``%(request_id)s`` always resolves, even outside a request."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_RequestIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # SQLAlchemy echoes SQL on its own logger; keep it out of the app logger.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if settings.db_echo else logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING if settings.is_test else level)
