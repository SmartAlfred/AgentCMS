"""Logging setup (#2, extended by #5, #6, #24): one place that decides format and level.

Token redaction: any string matching ``acms_<hex>_<secret>`` is replaced with
``acms_abc…***`` and any string matching ``cap_<site>_<random>`` is replaced
with ``cap_<site>…***`` so that tokens never appear in logs.

#24 adds a JSON formatter: when ``LOG_FORMAT=json`` every log line is a single
JSON object, and the request middleware logs one structured line per request
(``ts, level, request_id, method, path_template, status, duration_ms,
actor_id, actor_label, source, ip, user_agent, bytes_in/out``).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from app.config import Settings

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s"

_TOKEN_RE = re.compile(r"acms_[0-9a-f]{6}[0-9a-f]*_[A-Za-z0-9_-]{20,}")
_CAP_TOKEN_RE = re.compile(r"cap_[a-zA-Z0-9_-]{1,128}_[A-Za-z0-9_-]{20,}")

# Structured fields the request middleware attaches to a record; the JSON
# formatter merges them into the output object.
_REQUEST_FIELDS_ATTR = "request_fields"


class _RequestIdFilter(logging.Filter):
    """Ensure ``%(request_id)s`` always resolves, even outside a request."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


class _TokenRedactionFilter(logging.Filter):
    """Redact API tokens (``acms_*`` and ``cap_*``) in log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN_RE.sub(_redact_match, record.msg)
            record.msg = _CAP_TOKEN_RE.sub(_redact_cap_match, record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact_string(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(_redact_string(v) if isinstance(v, str) else v for v in record.args)
        # Structured request fields may carry strings that embed tokens
        # (exported via the JSON formatter).  Redact them too.
        fields = getattr(record, _REQUEST_FIELDS_ATTR, None)
        if isinstance(fields, dict):
            setattr(
                record,
                _REQUEST_FIELDS_ATTR,
                {k: _redact_string(v) if isinstance(v, str) else v for k, v in fields.items()},
            )
        return True


def _redact_match(match: re.Match[str]) -> str:
    """Replace a token match with ``acms_abc…***``."""
    full = match.group(0)
    prefix = full[:10]  # "acms_abcde" (prefix + 5 hex of actor_id)
    return f"{prefix}…***"


def _redact_cap_match(match: re.Match[str]) -> str:
    """Replace a capability token match with ``cap_<site>…***``."""
    full = match.group(0)
    # Extract site slug: everything between "cap_" and the second "_"
    after_prefix = full[4:]  # remove "cap_"
    site_end = after_prefix.find("_")
    if site_end == -1:
        return "***"
    site_slug = after_prefix[:site_end]
    return f"cap_{site_slug}_…***"


def _redact_string(value: str) -> str:
    """Apply both acms_* and cap_* redaction to a string."""
    value = _TOKEN_RE.sub(_redact_match, value)
    value = _CAP_TOKEN_RE.sub(_redact_cap_match, value)
    return value


def _parse_datetime(record: logging.LogRecord) -> str:
    ts = getattr(record, "created", None)
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="milliseconds")
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line.

    Any structured fields attached as ``record.request_fields`` are merged
    into the top level of the object (they never reach ``message``), so a
    request's structured payload is *not* embedded in the human-readable
    message string and the redaction filter has already walked it.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": _parse_datetime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, _REQUEST_FIELDS_ATTR, None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class StandardFormatter(logging.Formatter):
    """Human-readable formatter used when ``LOG_FORMAT=text`` (local dev)."""

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = getattr(record, "request_id", "-")
        fields = getattr(record, _REQUEST_FIELDS_ATTR, None)
        if isinstance(fields, dict) and fields.get("method") and fields.get("path_template"):
            record.msg = (
                f"{record.msg}  | {fields['method']} {fields['path_template']} "
                f"-> {fields.get('status', '')} in {fields.get('duration_ms', '')}ms"
            )
        return super().format(record)


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    use_json = settings.log_format.lower() == "json"

    if use_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(StandardFormatter(_FORMAT))
    handler.addFilter(_TokenRedactionFilter())
    handler.addFilter(_RequestIdFilter())

    root = logging.getLogger()
    # Remove pre-existing handlers once so `create_app` never duplicates lines.
    root.handlers = [handler]
    root.setLevel(level)
    # Attach redaction filter to the root logger itself so it applies even
    # when pytest's caplog or other handlers capture records directly.
    root.addFilter(_TokenRedactionFilter())

    # SQLAlchemy echoes SQL on its own logger; keep it out of the app logger.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if settings.db_echo else logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING if settings.is_test else level)
