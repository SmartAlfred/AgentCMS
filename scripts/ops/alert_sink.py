#!/usr/bin/env python3
"""Alertmanager webhook sink + human channel delivery for the ops set (#47).

Alertmanager POSTs its alert payload here.  Every delivery is appended to
``$SINK_DATA/alerts.jsonl`` with a UTC ``received_at`` — *when the alert arrived
on the delivery path*, which is the timestamp the monitoring drill needs — and,
when ``NTFY_TOPIC`` is set, the same message is pushed to a free ntfy topic (or
a self-hosted ntfy) so a human actually sees it.

Ntfy needs no account on the public server: ``NTFY_SERVER=https://ntfy.sh`` plus
a topic name of your choosing.  Treat the topic name as a secret (it is the only
thing protecting the topic) and keep alert text free of credentials — the rules
only ever carry alert names, labels and the route that failed.

Endpoints: ``POST /alerts`` (Alertmanager), ``GET /healthz`` (readiness).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

SINK_PORT = int(os.environ.get("SINK_PORT", "9119"))
SINK_DATA = Path(os.environ.get("SINK_DATA", "/data"))
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
SINK_LABEL = os.environ.get("SINK_LABEL", "agentcms")
NTFY_TIMEOUT_S = float(os.environ.get("NTFY_TIMEOUT_S", "10"))


def utc_now() -> str:
    """UTC timestamp in the format every drill log uses."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def format_message(payload: dict[str, Any]) -> tuple[str, str]:
    """Alertmanager payload -> (ntfy title, plain-text body)."""
    alerts = payload.get("alerts") or []
    firing = [a for a in alerts if (a.get("status") or "firing") != "resolved"]
    resolved = [a for a in alerts if (a.get("status") or "firing") == "resolved"]
    names = sorted({(a.get("labels") or {}).get("alertname", "?") for a in alerts})
    state = "RESOLVED" if firing == [] and resolved else "FIRING"
    title = f"[{state}] {SINK_LABEL}: {', '.join(names) or 'alert'}"
    lines = [
        f"status: {state}",
        f"received_at: {utc_now()}",
        f"groupKey: {payload.get('groupKey', '')}",
    ]
    for alert in alerts:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        lines += [
            "",
            f"alertname: {labels.get('alertname', '?')}  [{alert.get('status', 'firing')}]",
            f"severity: {labels.get('severity', '')}",
            f"summary: {annotations.get('summary', '')}",
            f"startsAt: {alert.get('startsAt', '')}  endsAt: {alert.get('endsAt', '')}",
        ]
        extra = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()) if k != "alertname")
        if extra:
            lines.append(f"labels: {extra}")
        if alert.get("valueString"):
            lines.append(f"value: {alert['valueString']}")
    return title, "\n".join(lines)


def deliver_to_ntfy(title: str, body: str) -> dict[str, Any]:
    """POST the message to the configured ntfy topic; return a result record."""
    if not NTFY_TOPIC:
        return {"delivered": False, "reason": "NTFY_TOPIC unset"}
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers = {
        "Title": title.encode("ascii", "replace").decode("ascii"),
        "Priority": "high" if "FIRING" in title else "default",
        "Tags": "rotating_light" if "FIRING" in title else "white_check_mark",
        "Content-Type": "text/plain; charset=utf-8",
    }
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    request = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=NTFY_TIMEOUT_S) as response:
            return {
                "delivered": 200 <= response.status < 300,
                "http_status": response.status,
                "server": NTFY_SERVER,
                "topic": NTFY_TOPIC,
            }
    except urllib.error.HTTPError as exc:
        return {"delivered": False, "http_status": exc.code, "error": exc.reason}
    except Exception as exc:  # network down, DNS, timeout — record, do not crash
        return {"delivered": False, "error": f"{type(exc).__name__}: {exc}"}


def record(entry: dict[str, Any]) -> None:
    """Append one delivery to the JSONL transcript (the drill's raw evidence)."""
    SINK_DATA.mkdir(parents=True, exist_ok=True)
    with (SINK_DATA / "alerts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/alerts":
            self._respond(404, b"not found\n")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        received_at = utc_now()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            self._respond(400, f"bad json: {exc}\n".encode())
            return
        title, body = format_message(payload)
        delivery = deliver_to_ntfy(title, body)
        record(
            {
                "received_at": received_at,
                "title": title,
                "body": body,
                "alertmanager_payload": payload,
                "channel": delivery,
            }
        )
        print(f"{received_at} {title} -> {delivery}", file=sys.stderr)
        self._respond(200, b"ok\n")

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/healthz":
            self._respond(200, b"ok\n")
        else:
            self._respond(404, b"not found\n")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(fmt % args, file=sys.stderr)


def main() -> int:
    SINK_DATA.mkdir(parents=True, exist_ok=True)
    print(
        f"alert sink on :{SINK_PORT} data={SINK_DATA} ntfy={NTFY_SERVER}/{NTFY_TOPIC or '<unset>'}",
        file=sys.stderr,
    )
    HTTPServer(("0.0.0.0", SINK_PORT), _Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
