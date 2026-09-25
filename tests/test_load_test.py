"""Load-test maths and CLI contract (#24, WS3 load drill).

The maths is exercised as pure functions (percentiles, error budget, status
breakdown) and the CLI is exercised end-to-end against a throwaway HTTP server
on 127.0.0.1, so the numbers a drill reports are the numbers the code computes
rather than a second, hand-rolled implementation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from scripts.load_test import LevelSpec, Sample, parse_levels, percentile, summarize

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- maths


def test_percentile_matches_the_textbook_r7_definition() -> None:
    # rank = (n - 1) * p / 100, linearly interpolated -- the same definition
    # numpy/k6 use, so a drill number is comparable with an outside tool.
    values = [float(i) for i in range(1, 101)]
    assert percentile(values, 0) == pytest.approx(1.0)
    assert percentile(values, 50) == pytest.approx(50.5)
    assert percentile(values, 95) == pytest.approx(95.05)
    assert percentile(values, 99) == pytest.approx(99.01)
    assert percentile(values, 100) == pytest.approx(100.0)


def test_percentile_handles_degenerate_input() -> None:
    assert percentile([], 95) == 0.0
    assert percentile([42.0], 50) == pytest.approx(42.0)
    assert percentile([10.0, 20.0], 50) == pytest.approx(15.0)
    # p is clamped, so a typo cannot silently return the wrong tail.
    assert percentile([10.0, 20.0], 150) == pytest.approx(20.0)
    assert percentile([10.0, 20.0], -5) == pytest.approx(10.0)


def test_percentile_does_not_assume_sorted_input() -> None:
    shuffled = [30.0, 10.0, 40.0, 20.0]
    assert percentile(shuffled, 50) == pytest.approx(25.0)


def _samples() -> list[Sample]:
    return [
        Sample(path="/a", status=200, latency_ms=10.0, offset_s=0.0),
        Sample(path="/a", status=200, latency_ms=20.0, offset_s=0.1),
        Sample(path="/a", status=200, latency_ms=30.0, offset_s=0.2),
        Sample(path="/b", status=404, latency_ms=40.0, offset_s=0.3),
        Sample(path="/b", status=500, latency_ms=50.0, offset_s=0.4),
        Sample(path="/b", status=0, latency_ms=10000.0, offset_s=0.5),
    ]


def test_summarize_separates_dropped_from_client_errors() -> None:
    summary = summarize(_samples(), elapsed_s=1.0, error_budget_ratio=0.01)
    # "dropped" keeps the historical meaning: no response, or a 5xx.
    assert summary["requests"] == 6
    assert summary["dropped"] == 2
    assert summary["server_errors"] == 1
    assert summary["client_errors"] == 1
    assert summary["ok"] == 4
    assert summary["status_counts"] == {"200": 3, "404": 1, "500": 1, "0": 1}


def test_summarize_reports_latency_percentiles_for_all_and_for_ok_only() -> None:
    summary = summarize(_samples(), elapsed_s=1.0, error_budget_ratio=0.01)
    assert summary["latency_ms"]["p50"] == pytest.approx(35.0)
    assert summary["latency_ms"]["max"] == pytest.approx(10000.0)
    # A timeout must not be allowed to hide the tail of *served* latency.  The
    # 404 is a served request, so it belongs here; rank = (4-1)*0.95 = 2.85.
    assert summary["latency_ms_ok"]["p95"] == pytest.approx(38.5)
    assert summary["latency_ms_ok"]["max"] == pytest.approx(40.0)


def test_summarize_turns_error_rate_into_budget_consumed() -> None:
    summary = summarize(_samples(), elapsed_s=2.0, error_budget_ratio=0.01)
    assert summary["error_ratio"] == pytest.approx(2 / 6)
    assert summary["error_budget_ratio"] == pytest.approx(0.01)
    assert summary["budget_consumed_ratio"] == pytest.approx((2 / 6) / 0.01)
    assert summary["throughput_rps"] == pytest.approx(3.0)


def test_summarize_treats_a_zero_budget_as_fully_consumed_on_any_error() -> None:
    clean = summarize(_samples()[:3], elapsed_s=1.0, error_budget_ratio=0.0)
    assert clean["budget_consumed_ratio"] == 0.0
    dirty = summarize(_samples(), elapsed_s=1.0, error_budget_ratio=0.0)
    assert dirty["budget_consumed_ratio"] == float("inf")


def test_summarize_breaks_errors_down_per_path() -> None:
    summary = summarize(_samples(), elapsed_s=1.0, error_budget_ratio=0.01)
    assert summary["per_path"]["/a"]["dropped"] == 0
    assert summary["per_path"]["/a"]["requests"] == 3
    assert summary["per_path"]["/b"]["dropped"] == 2
    # rank = (3-1)*0.95 = 1.9 between the 50ms 500 and the 10000ms timeout.
    assert summary["per_path"]["/b"]["p95"] == pytest.approx(9005.0)


# --------------------------------------------------------------------------- args


def test_parse_levels_reads_concurrency_and_optional_rate() -> None:
    assert parse_levels("10@100, 20,40@200") == [
        LevelSpec(concurrency=10, rps=100.0),
        LevelSpec(concurrency=20, rps=None),
        LevelSpec(concurrency=40, rps=200.0),
    ]


@pytest.mark.parametrize("bad", ["", "  ", "x", "0", "10@0", "10@abc", "10@", "-3"])
def test_parse_levels_rejects_nonsense(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_levels(bad)


# --------------------------------------------------------------------------- CLI


class _Config:
    mode = "ok"
    expected_auth = ""
    seen_auth: ClassVar[list[str]] = []
    inflight = 0
    max_inflight = 0
    lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        cfg = _Config
        cfg.seen_auth.append(self.headers.get("Authorization", ""))
        with cfg.lock:
            cfg.inflight += 1
            cfg.max_inflight = max(cfg.max_inflight, cfg.inflight)
            peak = cfg.inflight
        try:
            status = 200
            if cfg.mode == "boom":
                status = 500
            elif cfg.mode == "pool1" and peak > 1:
                # Emulates a single-connection pool: concurrency is the fault.
                status = 500
            elif cfg.mode == "auth" and self.headers.get("Authorization") != f"Bearer {cfg.expected_auth}":
                status = 401
            time.sleep(0.01 if cfg.mode == "pool1" else 0.002)
            body = b'{"ok":true}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            with cfg.lock:
                cfg.inflight -= 1

    def log_message(self, *args: Any) -> None:  # keep pytest output readable
        return


@pytest.fixture()
def server() -> Any:
    _Config.mode = "ok"
    _Config.expected_auth = ""
    _Config.seen_auth = []
    _Config.inflight = 0
    _Config.max_inflight = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.load_test", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_reports_percentiles_and_writes_machine_readable_output(server: str, tmp_path: Path) -> None:
    out = tmp_path / "raw.json"
    proc = _run(
        "--url", server, "--path", "/ok", "--requests", "40", "--concurrency", "4", "--json-out", str(out)
    )
    assert proc.returncode == 0, proc.stderr
    assert "p95=" in proc.stdout and "p99=" in proc.stdout and "err=" in proc.stdout
    payload = json.loads(out.read_text())
    level = payload["levels"][0]
    summary = level["summary"]
    assert summary["requests"] == 40
    assert summary["dropped"] == 0
    assert summary["latency_ms"]["p50"] <= summary["latency_ms"]["p95"] <= summary["latency_ms"]["p99"]
    assert payload["target"]["url"] == server
    assert level["started_at_utc"].endswith("Z")


def test_cli_exit_code_still_means_zero_dropped_requests(server: str, tmp_path: Path) -> None:
    out = tmp_path / "raw.json"
    _Config.mode = "boom"
    proc = _run(
        "--url", server, "--path", "/boom", "--requests", "12", "--concurrency", "3", "--json-out", str(out)
    )
    assert proc.returncode == 1
    assert "FAILED" in proc.stderr
    summary = json.loads(out.read_text())["levels"][0]["summary"]
    assert summary["dropped"] == 12
    assert summary["error_ratio"] == pytest.approx(1.0)


def test_cli_loads_a_path_that_needs_a_bearer_token(server: str) -> None:
    _Config.mode = "auth"
    _Config.expected_auth = "cap_blog_token-with-dash_9"
    proc = _run(
        "--url",
        server,
        "--path",
        "/v1/posts/pre-backup-marker",
        "--auth-token",
        "cap_blog_token-with-dash_9",
        "--requests",
        "10",
        "--concurrency",
        "2",
    )
    assert proc.returncode == 0, proc.stderr
    assert set(_Config.seen_auth) == {"Bearer cap_blog_token-with-dash_9"}


def test_cli_duration_mode_runs_for_the_requested_window(server: str, tmp_path: Path) -> None:
    out = tmp_path / "raw.json"
    proc = _run(
        "--url", server, "--path", "/ok", "--duration", "0.4", "--concurrency", "4", "--json-out", str(out)
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(out.read_text())["levels"][0]["summary"]
    assert summary["elapsed_s"] >= 0.35
    assert summary["requests"] > 4


def test_cli_sweep_reports_the_ceiling_and_only_the_sla_level_decides_the_exit_code(
    server: str, tmp_path: Path
) -> None:
    _Config.mode = "pool1"
    out = tmp_path / "raw.json"
    levels = "1@20,4@200,8@400"
    proc = _run(
        "--url",
        server,
        "--path",
        "/ok",
        "--levels",
        levels,
        "--duration",
        "0.4",
        "--stop-on-error-ratio",
        "0.01",
        "--json-out",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    payload = json.loads(out.read_text())
    ran = [level["spec"] for level in payload["levels"]]
    assert ran[0] == "1c@20rps"
    assert len(ran) < 3, f"the sweep should stop at the first broken level, ran {ran}"
    assert payload["ceiling"] is not None
    # Beyond the SLA level the run may (and here must) be red without failing the drill.
    assert payload["levels"][1]["summary"]["dropped"] > 0

    proc = _run(
        "--url",
        server,
        "--path",
        "/ok",
        "--levels",
        levels,
        "--duration",
        "0.4",
        "--stop-on-error-ratio",
        "0.01",
        "--sla-level",
        "2",
    )
    assert proc.returncode == 1


def test_cli_rejects_malformed_levels_with_the_usage_exit_code(server: str) -> None:
    proc = _run("--url", server, "--levels", "nope", "--duration", "0.1")
    assert proc.returncode == 2
    assert "--levels" in proc.stderr
