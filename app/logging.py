"""Logging setup (#2, extended by #5): one place that decides format and level.

Token redaction: any string matching ``acms_<hex>_<secret>`` is replaced with
``acms_abc…***`` so that tokens never appear in logs.
"""

from __future__ import annotations

import logging
import re

from app.config import Settings

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s"

_TOKEN_RE = re.compile(r"acms_[0-9a-f]{6}[0-9a-f]*_[A-Za-z0-9_-]{20,}")


class _RequestIdFilter(logging.Filter):
    """Ensure ``%(request_id)s`` always resolves, even outside a request."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


class _TokenRedactionFilter(logging.Filter):
    """Redact API tokens (``acms_*``) in log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN_RE.sub(_redact_match, record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _TOKEN_RE.sub(_redact_match, v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _TOKEN_RE.sub(_redact_match, v) if isinstance(v, str) else v for v in record.args
                )
        return True


def _redact_match(match: re.Match[str]) -> str:
    """Replace a token match with ``acms_abc…***``."""
    full = match.group(0)
    prefix = full[:10]  # "acms_abcde" (prefix + 5 hex of actor_id)
    return f"{prefix}…***"


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_RequestIdFilter())
    handler.addFilter(_TokenRedactionFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Attach redaction filter to the root logger itself so it applies even
    # when pytest's caplog or other handlers capture records directly.
    root.addFilter(_TokenRedactionFilter())

    # SQLAlchemy echoes SQL on its own logger; keep it out of the app logger.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if settings.db_echo else logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING if settings.is_test else level)
