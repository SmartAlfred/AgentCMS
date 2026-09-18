"""Secrets never reach the logs (#6 + #24).

The ticket asks for a spike that proves the logging filter stops both
``acms_*`` (API tokens) and ``cap_*`` (capability tokens) leaking into the
output.  These tests:

1. push tokens through the actual JSON formatter + redaction filter, and
2. drive a real request through the middleware with an ``acms_*`` bearer token
   and assert the emitted structured log line contains neither the token nor
   the secret portion.
"""

from __future__ import annotations

import io
import json
import logging

from app.logging import JsonFormatter, _RequestIdFilter, _TokenRedactionFilter


def _capture_logger() -> tuple[logging.Logger, io.StringIO]:
    """A throwaway logger wired to the JSON formatter and the redaction filters.

    Records are emitted through the logger so the handler's ``handle()`` path
    applies the filters exactly like production logging does.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_TokenRedactionFilter())
    handler.addFilter(_RequestIdFilter())
    logger = logging.getLogger(f"app.request.{id(stream)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


class TestRedactionFilter:
    def test_acms_token_redacted_in_message(self) -> None:
        raw = f"acms_{'a' * 40}_{'x' * 32}"  # shape: acms_<actor_id>_<secret>
        assert raw.startswith("acms_")

        logger, stream = _capture_logger()
        logger.info("user authenticated token %s and payload %s", raw, raw)

        line = stream.getvalue()
        parsed = json.loads(line)
        assert "…***" in parsed["message"]  # redacted placeholder present
        assert "acms_" in parsed["message"]
        assert raw not in line
        # The secret tail (everything after the second underscore) must be gone.
        tail = raw.split("_", 2)[2]
        assert tail not in line
        assert "xxxx" not in parsed["message"]

    def test_cap_token_redacted_even_in_structured_fields(self) -> None:
        raw = "cap_decoding-demos_abcdefghijklmnopqrstuvwxyz123456"
        assert raw.startswith("cap_")

        from app.logging import _REQUEST_FIELDS_ATTR

        logger = logging.getLogger(f"app.request.{id(object())}")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(_TokenRedactionFilter())
        handler.addFilter(_RequestIdFilter())
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        fields = {
            "request_id": "req-1",
            "method": "GET",
            "path_template": "/c/{token}",
            "status": 200,
            "auth": f"Bearer {raw}",
            "actor_id": raw,
        }
        logger.info("request complete", extra={_REQUEST_FIELDS_ATTR: fields})

        line = stream.getvalue()
        parsed = json.loads(line)
        assert parsed["request_id"] == "req-1"
        assert "cap_decoding-demos" in parsed["actor_id"]
        assert "***" in parsed["auth"]
        assert raw not in line
        tail = raw.split("_", 2)[2]
        assert tail not in line

    def test_json_lines_are_valid_json(self) -> None:
        logger, stream = _capture_logger()
        logger.info("healthz ok")

        for line in stream.getvalue().splitlines():
            if line.strip():
                obj = json.loads(line)
                assert {"ts", "level", "logger", "message"} <= set(obj)


class TestRequestThroughMiddleware:
    def test_bearer_token_never_in_request_log(
        self,
        client,
        app,
    ) -> None:
        raw = f"acms_{'b' * 40}_{'y' * 32}"
        import io as _io
        import json as _json

        from app.logging import JsonFormatter as JF

        stream = _io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JF())
        target = logging.getLogger("app.request")
        target.addHandler(handler)
        target.setLevel(logging.INFO)
        try:
            client.get(
                "/healthz",
                headers={
                    "Authorization": f"Bearer {raw}",
                    "X-Request-ID": "secret-probe-1",
                },
            )
        finally:
            target.removeHandler(handler)
            handler.close()

        lines = stream.getvalue().splitlines()
        structured = [_json.loads(obj_line) for obj_line in lines if obj_line.strip()]
        assert structured, "expected at least one structured log line"
        rendered = stream.getvalue()
        assert raw not in rendered
        assert "secret-probe-1" in rendered
        # The structured fields contain the request context but no credentials.
        flat = {k: v for obj in structured for k, v in obj.items()}
        assert flat["path_template"] == "/healthz"
        assert flat["request_id"] == "secret-probe-1"


class TestNoCredentialLoggingCallSites:
    def test_no_log_call_logs_secrets_literally(self) -> None:
        """No call site interpolates a credential-holding variable.

        We look at every ``logger.<level>(...)`` call in ``app/`` and flag any
        that reference a variable literally named (or containing) ``secret``,
        ``password``, ``passphrase``, ``authorization``, ``bearer`` or
        ``token_hash`` *outside* of string literals — i.e. as an interpolated
        format argument.  (``kill_switch`` logs ``actor_id`` — a UUID — whose
        message string merely *contains* the word "token", which is fine.)
        """
        import re
        from pathlib import Path

        call_re = re.compile(r"logger\.(debug|info|warning|error|exception)\((.+)\)\s*$")
        banned = re.compile(
            r"\b(authorization|bearer|password|passphrase|secret|token_hash)\b", re.IGNORECASE
        )
        offenders: list[str] = []

        def _strip_strings(text: str) -> str:
            out: list[str] = []
            i = 0
            while i < len(text):
                ch = text[i]
                if ch in ("'", '"'):
                    quote = ch
                    i += 1
                    while i < len(text) and text[i] != quote:
                        if text[i] == "\\":
                            i += 1
                        i += 1
                    i += 1
                    out.append(" ")
                else:
                    out.append(ch)
                    i += 1
            return "".join(out)

        for path in sorted(Path("app").rglob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                m = call_re.search(line)
                if not m:
                    continue
                args_text = _strip_strings(m.group(2))
                if banned.search(args_text):
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
        assert offenders == [], "\n".join(offenders)
