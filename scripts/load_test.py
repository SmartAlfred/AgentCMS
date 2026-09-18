"""Small load test for the zero-dropped-request deploy drill (#24).

Hammer a service while a rolling deploy is in progress and report how many
requests were *dropped* (connection errors, timeouts, or 5xx).  A clean
migration-gated rolling deploy must finish with ``dropped=0``.

Usage:
    python -m scripts.load_test --url http://127.0.0.1:8000 \\
        --requests 500 --concurrency 10 --metrics-token "$METRICS_TOKEN"

Exit codes:
    0  zero dropped requests (target for a rolling deploy)
    1  any dropped / 5xx response
    2  usage / target error
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

_PATHS = ("/healthz", "/readyz", "/status")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="base URL of the service")
    parser.add_argument("--requests", type=int, default=500, help="total requests")
    parser.add_argument("--concurrency", type=int, default=10, help="parallel workers")
    parser.add_argument("--metrics-token", default="", help="optional X-Metrics-Token for /metrics")
    return parser.parse_args()


def _one(client: httpx.Client, url: str, path: str) -> tuple[int, float]:
    start = time.perf_counter()
    try:
        resp = client.get(url + path)
        return resp.status_code, (time.perf_counter() - start) * 1000
    except httpx.HTTPError:
        return 0, (time.perf_counter() - start) * 1000


def main() -> int:
    args = _parse_args()
    if args.requests <= 0 or args.concurrency <= 0:
        print("--requests and --concurrency must be positive", file=sys.stderr)
        return 2

    statuses: list[tuple[str, int, float]] = []
    start = time.perf_counter()

    def worker(path: str, n: int) -> None:
        with httpx.Client(timeout=10.0) as client:
            for _ in range(n):
                code, ms = _one(client, args.url, path)
                statuses.append((path, code, ms))

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = []
        per_path = max(1, args.requests // len(_PATHS))
        for path in _PATHS:
            futures.append(pool.submit(worker, path, per_path))
        for future in futures:
            future.result()

    elapsed = time.perf_counter() - start
    errors = [(p, c) for p, c, _ in statuses if c == 0 or c >= 500]
    oks = len(statuses) - len(errors)
    total_ms = sum(ms for _, _, ms in statuses)
    avg_ms = total_ms / max(1, len(statuses))

    print(
        f"requests={len(statuses)} ok={oks} dropped={len(errors)} "
        f"avg={avg_ms:.1f}ms elapsed={elapsed:.1f}s "
        f"rps={len(statuses) / elapsed:.0f}"
    )
    for path, code in errors[:20]:
        print(f"  DROPPED {path}: status={code if code else 'no-response'}", file=sys.stderr)

    if errors:
        print("load test FAILED: zero-dropped-request target not met", file=sys.stderr)
        return 1
    print("load test OK: zero dropped requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
