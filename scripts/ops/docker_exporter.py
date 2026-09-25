#!/usr/bin/env python3
"""Docker container telemetry for the committed alert rules (#47).

Prometheus cannot see Docker's per-container ``RestartCount`` or the exit code of
a one-shot job such as ``migrate``: cAdvisor does not export them and
kube-state-metrics only exists for Kubernetes.  So this exporter reads them
straight from the Docker Engine API over the unix socket (mounted read-only) and
renders exactly the series the drift-checked rules in
``app/observability/alerts.py`` reference::

    agentcms_container_restart_count{container,project,service,state}
    agentcms_container_last_exit_code{container,project,service,state}
    agentcms_container_running{container,project,service}

Stdlib only (nothing to pip-install inside the container) and the Prometheus
text exposition format is written by hand.  ``EXPORT_PROJECT`` restricts the
output to a single compose project, so on a shared host a restart of *someone
else's* container does not page us.
"""

from __future__ import annotations

import http.client
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import quote

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
LISTEN_PORT = int(os.environ.get("EXPORTER_PORT", "9121"))
EXPORT_PROJECT = os.environ.get("EXPORT_PROJECT", "")
HELP = {
    "agentcms_container_restart_count": "Times Docker has restarted this container",
    "agentcms_container_last_exit_code": "Exit code of the container's last run",
    "agentcms_container_running": "1 when the container is running",
    "agentcms_container_last_restart_observed_timestamp_seconds": (
        "Epoch seconds when this exporter last observed RestartCount go up"
    ),
}
# Prometheus scrapes every 15s, which is enough for a crash *loop* but not for a
# container that restarts and then settles: `changes(RestartCount[10m])` sees a
# flat series and the page never fires (measured 2026-09-25: a container that
# restarted 3x in 12s was invisible to the rule).  So the exporter polls Docker
# itself, faster than any scrape, and records *when* it saw a restart happen.
POLL_INTERVAL_S = float(os.environ.get("EXPORT_POLL_INTERVAL_S", "5"))
SNAPSHOT_MAX_AGE_S = float(os.environ.get("EXPORT_SNAPSHOT_MAX_AGE_S", "60"))


class _UnixConnection(http.client.HTTPConnection):
    """HTTP over the Docker unix socket (chunked responses handled by http.client)."""

    def __init__(self, path: str, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def docker_get(path: str) -> Any:
    """GET a Docker Engine API path and return the decoded JSON body."""
    conn = _UnixConnection(DOCKER_SOCK)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f"docker API {path} -> HTTP {response.status}: {body[:200]!r}")
        import json

        return json.loads(body)
    finally:
        conn.close()


def inspect_containers() -> list[dict[str, Any]]:
    """Inspect every container (running and exited) Docker knows about."""
    out: list[dict[str, Any]] = []
    for item in docker_get("/containers/json?all=1"):
        cid = item.get("Id", "")
        if not cid:
            continue
        try:
            out.append(docker_get(f"/containers/{quote(cid)}/json"))
        except Exception as exc:  # container vanished between list and inspect
            print(f"skip {cid[:12]}: {exc}", file=sys.stderr)
    return out


def observe_restarts(
    containers: list[dict[str, Any]], seen: dict[str, tuple[int, float]], now: float
) -> None:
    """Record the wall-clock second at which each container's RestartCount went up.

    ``seen`` maps container name -> (last observed RestartCount, epoch seconds of
    the last observed increase).  A container first seen with
    ``RestartCount > 0`` is stamped with ``now``: the restart happened, we simply
    did not watch it happen, and staying silent about it is how a crash loop goes
    unnoticed.
    """
    for container in containers:
        name = (container.get("Name") or "").lstrip("/") or container.get("Id", "")[:12]
        count = int(container.get("RestartCount", 0) or 0)
        previous = seen.get(name)
        if previous is None:
            seen[name] = (count, now if count > 0 else 0.0)
        elif count > previous[0]:
            seen[name] = (count, now)
        else:
            seen[name] = (previous[0], previous[1])


def _labels(container: dict[str, Any]) -> dict[str, str]:
    return (container.get("Config") or {}).get("Labels") or {}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _line(name: str, labels: dict[str, str], value: float) -> str:
    rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
    return f"{name}{{{rendered}}} {value:g}"


def render_metrics(
    containers: list[dict[str, Any]],
    project: str = "",
    observed: dict[str, tuple[int, float]] | None = None,
) -> str:
    """Render the exposition text for ``containers`` (Docker inspect documents).

    ``observed`` is :func:`observe_restarts`' state; containers with an observed
    restart also emit ``agentcms_container_last_restart_observed_timestamp_seconds``.
    """
    observed = observed or {}
    blocks: list[list[str]] = []
    for container in containers:
        labels = _labels(container)
        if project and labels.get("com.docker.compose.project", "") != project:
            continue
        state = (container.get("State") or {}).get("Status", "unknown")
        common = {
            "container": (container.get("Name") or "").lstrip("/") or container.get("Id", "")[:12],
            "project": labels.get("com.docker.compose.project", ""),
            "service": labels.get("com.docker.compose.service", ""),
        }
        restart_labels = dict(common, state=state)
        exit_labels = dict(common, state=state)
        rows = [
            *(
                [
                    _line(
                        "agentcms_container_last_restart_observed_timestamp_seconds",
                        common,
                        observed[common["container"]][1],
                    )
                ]
                if observed.get(common["container"], (0, 0.0))[1] > 0
                else []
            ),
            _line(
                "agentcms_container_restart_count",
                restart_labels,
                float(container.get("RestartCount", 0)),
            ),
            _line(
                "agentcms_container_last_exit_code",
                exit_labels,
                float((container.get("State") or {}).get("ExitCode", 0)),
            ),
            _line(
                "agentcms_container_running",
                common,
                1.0 if state == "running" else 0.0,
            ),
        ]
        blocks.append(rows)
    if not blocks:
        return ""
    text: list[str] = []
    for metric, help_text in HELP.items():
        text.append(f"# HELP {metric} {help_text}")
        text.append(f"# TYPE {metric} gauge")
    for rows in blocks:
        text.extend(rows)
    return "\n".join(text) + "\n"


_LOCK = threading.Lock()
_SNAPSHOT: list[dict[str, Any]] = []
_SEEN: dict[str, tuple[int, float]] = {}
_REFRESHED_AT = 0.0


def refresh_snapshot() -> None:
    """Poll Docker once: refresh the cache and the restart observations."""
    global _REFRESHED_AT
    containers = inspect_containers()
    now = time.time()
    with _LOCK:
        observe_restarts(containers, _SEEN, now)
        _SNAPSHOT[:] = containers
        _REFRESHED_AT = now


def poll_forever() -> None:
    while True:
        try:
            refresh_snapshot()
        except Exception as exc:
            print(f"poll failed: {exc}", file=sys.stderr)
        time.sleep(POLL_INTERVAL_S)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path.split("?")[0] not in ("/metrics", "/metrics/"):
            self.send_error(404)
            return
        with _LOCK:
            age = time.time() - _REFRESHED_AT
            snapshot = list(_SNAPSHOT)
            observed = dict(_SEEN)
        try:
            if age > SNAPSHOT_MAX_AGE_S:  # stale cache: fail the scrape loudly
                raise RuntimeError(f"snapshot is {age:.0f}s old (Docker unreachable?)")
            body = render_metrics(snapshot, EXPORT_PROJECT, observed).encode()
            status = 200
        except Exception as exc:  # never return a half-scrape with a 200
            print(f"scrape failed: {exc}", file=sys.stderr)
            body, status = f"# scrape failed: {exc}\n".encode(), 500
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(fmt % args, file=sys.stderr)


def main() -> int:
    print(
        f"docker exporter on :{LISTEN_PORT} sock={DOCKER_SOCK} project={EXPORT_PROJECT or '*'}"
        f" poll={POLL_INTERVAL_S}s",
        file=sys.stderr,
    )
    refresh_snapshot()
    threading.Thread(target=poll_forever, daemon=True).start()
    HTTPServer(("0.0.0.0", LISTEN_PORT), _Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
