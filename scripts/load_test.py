"""Load test with percentiles, error budget and machine-readable output (#24, WS3).

Two questions, two modes:

* **Deploy drill** (default, closed loop): hammer a service while a rolling
  deploy is in progress and report how many requests were *dropped*
  (connection errors, timeouts or 5xx).  A clean migration-gated rolling deploy
  must finish with ``dropped=0`` -- that is the exit-code contract and it is
  unchanged.
* **Capacity drill** (``--levels`` + ``--duration``, open loop): run a ladder of
  concurrency/rate levels for at least 60 s each and report p50/p95/p99, error
  rate, status breakdown, achieved throughput and *error budget consumed*
  relative to the 1% budget the ``HighErrorRatio5xx`` alert pages on.  The
  ladder stops at the first level that trips ``--stop-on-error-ratio`` or
  ``--stop-on-p95-ms``, which is the measured ceiling.

Exit codes:
    0  zero dropped requests at or below the SLA level (default: the first level)
    1  dropped / 5xx responses at or below the SLA level
    2  usage / target error

Usage:
    python -m scripts.load_test --url http://127.0.0.1:8000 \\
        --requests 500 --concurrency 10 --metrics-token "$METRICS_TOKEN"

    python -m scripts.load_test --url http://127.0.0.1:8033 \\
        --path /v1/posts/pre-backup-marker --auth-token "$CAP" \\
        --levels "1@25,20@100,40@200,80@400" --duration 60 \\
        --sla-level 2 --json-out load-2026-09-25.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_PATHS = ("/healthz", "/readyz", "/status")
# The paging threshold of the HighErrorRatio5xx alert rule -- an error budget is
# only meaningful relative to the threshold that actually pages someone.
DEFAULT_ERROR_BUDGET_RATIO = 0.01
# The paging threshold of the HighLatencyP95 alert rule.
DEFAULT_STOP_P95_MS = 500.0
DEFAULT_TIMEOUT_S = 10.0
_IDLE_TIMEOUT_S = 300.0  # a level must never hang forever on a black-holed target


@dataclass(frozen=True)
class Sample:
    """One HTTP attempt.  ``status == 0`` means no response at all."""

    path: str
    status: int
    latency_ms: float
    offset_s: float


@dataclass(frozen=True)
class LevelSpec:
    """One rung of the ladder: ``concurrency`` workers, optionally paced to ``rps``."""

    concurrency: int
    rps: float | None = None

    @property
    def label(self) -> str:
        return f"{self.concurrency}c@{self.rps:g}rps" if self.rps else f"{self.concurrency}c@open"


def parse_levels(spec: str) -> list[LevelSpec]:
    """Parse ``"10@100, 20,40@200"`` into level specs (raises ``ValueError``)."""

    levels: list[LevelSpec] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise ValueError(f"--levels: empty level in {spec!r}")
        conc_text, at, rps_text = chunk.partition("@")
        try:
            concurrency = int(conc_text)
        except ValueError as exc:
            raise ValueError(f"--levels: concurrency in {chunk!r} must be an integer") from exc
        if concurrency <= 0:
            raise ValueError(f"--levels: concurrency in {chunk!r} must be positive")
        rps: float | None = None
        if at:
            try:
                rps = float(rps_text)
            except ValueError as exc:
                raise ValueError(f"--levels: rate in {chunk!r} must be a number") from exc
            if rps <= 0:
                raise ValueError(f"--levels: rate in {chunk!r} must be positive")
        levels.append(LevelSpec(concurrency=concurrency, rps=rps))
    if not levels:
        raise ValueError("--levels: no levels given")
    return levels


def percentile(values: Sequence[float], p: float) -> float:
    """Linear-interpolated percentile (R-7, numpy's default), ``p`` clamped to 0..100."""

    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    clamped = min(100.0, max(0.0, p))
    rank = (len(ordered) - 1) * clamped / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def _is_dropped(sample: Sample) -> bool:
    return sample.status == 0 or sample.status >= 500


def _latency_block(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return dict.fromkeys(("min", "mean", "p50", "p75", "p90", "p95", "p99", "max"), 0.0)
    return {
        "min": min(values),
        "mean": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p75": percentile(values, 75),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
    }


def summarize(
    samples: Sequence[Sample],
    *,
    elapsed_s: float,
    error_budget_ratio: float = DEFAULT_ERROR_BUDGET_RATIO,
) -> dict[str, Any]:
    """Turn raw samples into the numbers a drill log reports."""

    requests = len(samples)
    dropped = [s for s in samples if _is_dropped(s)]
    server_errors = [s for s in samples if s.status >= 500]
    client_errors = [s for s in samples if 400 <= s.status < 500]
    ok = [s for s in samples if not _is_dropped(s)]
    error_ratio = len(dropped) / requests if requests else 0.0
    if error_budget_ratio > 0:
        budget_consumed: float = error_ratio / error_budget_ratio
    else:
        budget_consumed = float("inf") if error_ratio > 0 else 0.0

    status_counts: dict[str, int] = {}
    for sample in samples:
        key = str(sample.status)
        status_counts[key] = status_counts.get(key, 0) + 1

    per_path: dict[str, dict[str, Any]] = {}
    for path in dict.fromkeys(s.path for s in samples):
        group = [s for s in samples if s.path == path]
        per_path[path] = {
            "requests": len(group),
            "dropped": sum(1 for s in group if _is_dropped(s)),
            "p95": percentile([s.latency_ms for s in group], 95),
        }

    return {
        "requests": requests,
        "ok": len(ok),
        "dropped": len(dropped),
        "server_errors": len(server_errors),
        "client_errors": len(client_errors),
        "error_ratio": error_ratio,
        "error_budget_ratio": error_budget_ratio,
        "budget_consumed_ratio": budget_consumed,
        "elapsed_s": elapsed_s,
        "throughput_rps": requests / elapsed_s if elapsed_s > 0 else 0.0,
        # All attempts, so a timeout cannot be hidden by excluding it ...
        "latency_ms": _latency_block([s.latency_ms for s in samples]),
        # ... and the successes alone, so the tail of served traffic stays visible.
        "latency_ms_ok": _latency_block([s.latency_ms for s in ok]),
        "status_counts": status_counts,
        "per_path": per_path,
    }


@dataclass
class LevelResult:
    spec: LevelSpec
    started_at_utc: str
    ended_at_utc: str
    summary: dict[str, Any]
    samples: list[Sample]

    def as_json(self, *, include_samples: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "spec": self.spec.label,
            "concurrency": self.spec.concurrency,
            "rps_tested": self.spec.rps,
            "started_at_utc": self.started_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "summary": self.summary,
        }
        if include_samples:
            payload["samples"] = [s.__dict__ for s in self.samples]
        return payload


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_level(
    url: str,
    spec: LevelSpec,
    *,
    paths: Sequence[str],
    duration_s: float,
    requests: int | None,
    headers: dict[str, str],
    timeout_s: float,
    error_budget_ratio: float,
    verify_tls: bool = True,
) -> LevelResult:
    """Run one level and return its raw samples plus the summary of them."""

    samples: list[Sample] = []
    lock = threading.Lock()
    issued = 0
    started_at = _utc_now()
    t0 = time.perf_counter()
    deadline = t0 + duration_s
    budget = requests if requests is not None else 1 << 62

    def worker(index: int) -> None:
        nonlocal issued
        local: list[Sample] = []
        seq = 0
        with httpx.Client(timeout=timeout_s, headers=headers, verify=verify_tls) as client:
            while True:
                if time.perf_counter() >= deadline:
                    break
                with lock:
                    if issued >= budget:
                        break
                    slot = issued
                    issued += 1
                if spec.rps:
                    due = t0 + (seq * spec.concurrency + index) / spec.rps
                    delay = due - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    if time.perf_counter() >= deadline:
                        break
                path = paths[slot % len(paths)]
                start = time.perf_counter()
                try:
                    response = client.get(url + path)
                    status = response.status_code
                except httpx.HTTPError:
                    status = 0
                local.append(
                    Sample(
                        path=path,
                        status=status,
                        latency_ms=(time.perf_counter() - start) * 1000.0,
                        offset_s=start - t0,
                    )
                )
                seq += 1
        with lock:
            samples.extend(local)

    with ThreadPoolExecutor(max_workers=spec.concurrency) as pool:
        futures = [pool.submit(worker, index) for index in range(spec.concurrency)]
        for future in futures:
            future.result()

    elapsed = time.perf_counter() - t0
    summary = summarize(samples, elapsed_s=elapsed, error_budget_ratio=error_budget_ratio)
    return LevelResult(
        spec=spec,
        started_at_utc=started_at,
        ended_at_utc=_utc_now(),
        summary=summary,
        samples=samples,
    )


def _trip_reason(
    summary: dict[str, Any], stop_error_ratio: float | None, stop_p95_ms: float | None
) -> str | None:
    if stop_error_ratio is not None and summary["error_ratio"] > stop_error_ratio:
        return f"error_ratio {summary['error_ratio']:.2%} > {stop_error_ratio:.2%}"
    if stop_p95_ms is not None and summary["latency_ms"]["p95"] > stop_p95_ms:
        return f"p95 {summary['latency_ms']['p95']:.0f}ms > {stop_p95_ms:.0f}ms"
    return None


_HEADER = (
    "level",
    "requests",
    "ok",
    "dropped",
    "err%",
    "budget%",
    "ach/s",
    "p50",
    "p95",
    "p99",
    "max",
    "trip",
)


def _print_table(results: Sequence[LevelResult], trips: Sequence[str | None]) -> None:
    rows = []
    for result, trip in zip(results, trips, strict=False):
        s = result.summary
        lat = s["latency_ms"]
        budget = s["budget_consumed_ratio"]
        rows.append(
            (
                result.spec.label,
                str(s["requests"]),
                str(s["ok"]),
                str(s["dropped"]),
                f"{s['error_ratio'] * 100:.2f}",
                "inf" if budget == float("inf") else f"{budget * 100:.1f}",
                f"{s['throughput_rps']:.1f}",
                f"{lat['p50']:.0f}ms",
                f"{lat['p95']:.0f}ms",
                f"{lat['p99']:.0f}ms",
                f"{lat['max']:.0f}ms",
                trip or "",
            )
        )
    widths = [
        max(len(_HEADER[i]), *(len(r[i]) for r in rows)) if rows else len(_HEADER[i])
        for i in range(len(_HEADER))
    ]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(_HEADER)))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load test with percentiles, error budget and JSON output.", add_help=True
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="base URL of the service")
    parser.add_argument("--requests", type=int, default=500, help="total requests (count mode)")
    parser.add_argument("--concurrency", type=int, default=10, help="parallel workers (count/duration mode)")
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        default=None,
        help="path to load (repeatable; default: /healthz /readyz /status)",
    )
    parser.add_argument("--auth-token", default="", help="Bearer token, e.g. a capability token")
    parser.add_argument("--metrics-token", default="", help="optional X-Metrics-Token for /metrics")
    parser.add_argument(
        "--duration", type=float, default=None, help="seconds to load (>=60 for a real drill)"
    )
    parser.add_argument("--levels", default="", help='ladder, e.g. "10@100,20@200"; open loop by default')
    parser.add_argument("--sla-level", type=int, default=1, help="1-based level whose errors fail the run")
    parser.add_argument("--stop-on-error-ratio", type=float, default=None, help="stop the ladder above this")
    parser.add_argument(
        "--stop-on-p95-ms",
        type=float,
        default=None,
        help=f"stop the ladder above this p95 (default in sweep mode: {DEFAULT_STOP_P95_MS:g}ms)",
    )
    parser.add_argument("--error-budget-ratio", type=float, default=DEFAULT_ERROR_BUDGET_RATIO)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-request timeout (s)")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification (a self-hosted stack serves Caddy's internal CA on localhost)",
    )
    parser.add_argument("--json-out", default="", help="write the raw result as JSON")
    parser.add_argument("--json-samples", action="store_true", help="include every sample in --json-out")
    parser.add_argument("--env-note", default="", help="free-text environment pin recorded in --json-out")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    paths: tuple[str, ...] = tuple(args.paths) if args.paths else DEFAULT_PATHS
    sweep = bool(args.levels)
    try:
        specs = parse_levels(args.levels) if sweep else [LevelSpec(concurrency=args.concurrency)]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.concurrency <= 0 and not sweep:
        print("--concurrency must be positive", file=sys.stderr)
        return 2
    if args.requests <= 0 and not sweep and args.duration is None:
        print("--requests must be positive", file=sys.stderr)
        return 2
    if sweep and args.duration is None:
        print("--levels needs --duration (use >=60 for a real capacity level)", file=sys.stderr)
        return 2
    if sweep and not 1 <= args.sla_level <= len(specs):
        print(f"--sla-level must be between 1 and {len(specs)}", file=sys.stderr)
        return 2
    if any(path == "/metrics" for path in paths) and not args.metrics_token:
        print("--metrics-token is required to load /metrics", file=sys.stderr)
        return 2

    headers: dict[str, str] = {}
    if args.auth_token:
        headers["Authorization"] = f"Bearer {args.auth_token}"
    if args.metrics_token:
        headers["X-Metrics-Token"] = args.metrics_token

    count_mode = args.duration is None
    duration = args.duration if args.duration is not None else 0.0
    per_level_requests = None
    if count_mode:
        per_level_requests = max(1, args.requests)
        duration = max(_IDLE_TIMEOUT_S, 1.0)  # count mode ends on the request budget
    stop_error_ratio = args.stop_on_error_ratio
    stop_p95_ms = (
        args.stop_on_p95_ms if args.stop_on_p95_ms is not None else (DEFAULT_STOP_P95_MS if sweep else None)
    )

    run_started = _utc_now()
    results: list[LevelResult] = []
    trips: list[str | None] = []
    ceiling: str | None = None

    for index, spec in enumerate(specs):
        result = run_level(
            args.url,
            spec,
            paths=paths,
            duration_s=duration,
            requests=per_level_requests,
            headers=headers,
            timeout_s=args.timeout,
            error_budget_ratio=args.error_budget_ratio,
            verify_tls=not args.insecure,
        )
        results.append(result)
        reason = _trip_reason(result.summary, stop_error_ratio, stop_p95_ms)
        trips.append(reason)
        if reason is not None and ceiling is None:
            ceiling = f"{spec.label} ({reason})"
        print(
            f"[{index + 1}/{len(specs)}] {spec.label} {result.started_at_utc} -> {result.ended_at_utc} "
            f"requests={result.summary['requests']} dropped={result.summary['dropped']} "
            f"p50={result.summary['latency_ms']['p50']:.0f}ms "
            f"p95={result.summary['latency_ms']['p95']:.0f}ms "
            f"p99={result.summary['latency_ms']['p99']:.0f}ms "
            f"err={result.summary['error_ratio'] * 100:.2f}%",
            flush=True,
        )
        if sweep and reason is not None:
            print(f"    ceiling: stopping the ladder -- {reason}", file=sys.stderr)
            break

    _print_table(results, trips)

    # The exit-code contract: only levels at or below the SLA level decide it.
    sla_results = results[: args.sla_level]
    failed = [r for r in sla_results if r.summary["dropped"] > 0]
    payload = {
        "target": {
            "url": args.url,
            "paths": list(paths),
            "authenticated": bool(args.auth_token),
            "tls_verified": not args.insecure,
            "environment": args.env_note,
            "mode": "sweep" if sweep else ("duration" if not count_mode else "count"),
        },
        "started_at_utc": run_started,
        "ended_at_utc": _utc_now(),
        "error_budget_ratio": args.error_budget_ratio,
        "sla_level": args.sla_level,
        "sla_met": not failed,
        "ceiling": ceiling,
        "levels": [r.as_json(include_samples=args.json_samples) for r in results],
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2) + "\n")
        print(f"raw result: {args.json_out}")

    errors = [(s.path, s.status) for r in sla_results for s in r.samples if _is_dropped(s)]
    for path, code in errors[:20]:
        print(f"  DROPPED {path}: status={code if code else 'no-response'}", file=sys.stderr)
    if ceiling:
        print(f"measured ceiling: {ceiling}", file=sys.stderr)

    if failed:
        print("load test FAILED: zero-dropped-request target not met", file=sys.stderr)
        return 1
    print("load test OK: zero dropped requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
