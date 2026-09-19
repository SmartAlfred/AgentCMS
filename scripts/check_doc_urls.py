#!/usr/bin/env python3
"""Extract and verify documented URLs from markdown files that are claimed to be live."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]

# Patterns to identify placeholder/example URLs that should be skipped
# Only exclude obvious template placeholders
PLACEHOLDER_PATTERNS = [
    r"example\.com",
    r"your-domain\.com",
    r"your-cms-domain\.com",
    r"cms\.example\.com",
    r"your-instance\.example\.com",
    r"your-cms\.com",
    r"yourdomain\.com",
    r"your-frontend\.com",
    r"your-otlp-endpoint",
    r"your-org",
    r"your-app",
    r"<.*?>",
    r"\$\{.*?\}",
    r"fonts\.googleapis\.com",
    r"fonts\.gstatic\.com",
    r"github\.com/.*/.*\.git",  # git URLs
    # Internal service names (not publicly resolvable)
    r"jaeger:",
    r"minio:",
    # Localhost and private IPs - these are example commands, not claimed live
    r"localhost",
    r"127\.0\.0\.1",
    r"0\.0\.0\.0",
]

EXCLUDE_RE = re.compile("|".join(PLACEHOLDER_PATTERNS))

# URL regex - matches http(s):// followed by valid URL characters
URL_RE = re.compile(r"https?://[^\s\]\)}>\"'`]+")


# Known URLs that documentation explicitly claims are live
# These are the ones we should verify
CLAIMED_LIVE_URLS: list[str] = [
    # Add any URLs that are explicitly documented as live here
    # (The GitHub Pages URL is documented as 404, so it's excluded)
]


def find_md_files() -> list[Path]:
    """Find all markdown files excluding vendor directories."""
    # Support test mode via environment variable
    test_file = os.environ.get("CHECK_DOC_URLS_TEST_FILE")
    if test_file:
        return [Path(test_file)]

    exclude_dirs = {".venv", ".pytest_cache", "node_modules", ".git", "__pycache__"}
    files = []
    for path in REPO_ROOT.rglob("*.md"):
        if not any(part in exclude_dirs for part in path.parts):
            files.append(path)
    return sorted(files)


def extract_claimed_live_urls_from_file(file: Path) -> list[str]:
    """Extract URLs that are in contexts claiming they are live."""
    content = file.read_text(encoding="utf-8")
    urls = []

    # Pattern 1: Explicit "live at" or "available at" claims
    # Using simpler patterns that work with real text
    live_patterns = [
        r"live\s+(?:site|demo|url)?\s+(?:is\s+)?at\s+(https?://\S+)",
        r"(?:the\s+)?(?:Pages|site|demo)\s+(?:URL|site)\s+(?:is|:)\s+(https?://\S+)",
        r"HTTP\s+200\s+at\s+(https?://\S+)",
    ]

    for pattern in live_patterns:
        for match in re.finditer(pattern, content, re.IGNORECASE):
            url = match.group(1).rstrip(".,;:)]}>\"'`")
            if not EXCLUDE_RE.search(url):
                urls.append(url)

    # Exclude URLs that are explicitly documented as NOT live (404, not available, etc.)
    exclude_patterns = [
        r"(?:404|not\s+(?:live|available|reachable|served)|private\s+repo|free\s+plan).*?(https?://[^\s\]\)}>\"'`]+)",
        r"(https?://[^\s\]\)}>\"'`]+).*?(?:404|not\s+(?:live|available|reachable|served)|private\s+repo|free\s+plan)",
    ]

    excluded = set()
    for pattern in exclude_patterns:
        for match in re.finditer(pattern, content, re.IGNORECASE):
            url = match.group(1).rstrip(".,;:)]}>\"'`")
            excluded.add(url)

    return [u for u in urls if u not in excluded]


def extract_all_urls_from_file(file: Path) -> list[str]:
    """Extract all valid-looking URLs from a markdown file (fallback)."""
    content = file.read_text(encoding="utf-8")
    urls = []
    for match in URL_RE.finditer(content):
        url = match.group(0)
        url = url.rstrip(".,;:)]}>\"'`")
        if not EXCLUDE_RE.search(url):
            try:
                parsed = urlparse(url)
                if parsed.scheme in ("http", "https") and parsed.netloc and not parsed.netloc.startswith("."):
                    urls.append(url)
            except Exception:
                continue
    return urls


def check_url(url: str, timeout: int) -> tuple[bool, str]:
    """Check if a URL returns 2xx. Returns (success, status_message)."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout), url],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
        http_code = result.stdout.strip()
        if http_code.isdigit() and 200 <= int(http_code) < 300:
            return True, f"OK ({http_code})"
        else:
            return False, f"FAILED ({http_code})"
    except subprocess.TimeoutExpired:
        return False, "FAILED (timeout)"
    except Exception as e:
        return False, f"FAILED ({e})"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check documented URLs claimed to be live")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on first failure")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP timeout in seconds")
    args = parser.parse_args()

    # Start with explicitly claimed live URLs
    all_urls: set[str] = set(CLAIMED_LIVE_URLS)

    # Also extract from markdown files where docs claim URLs are live
    md_files = find_md_files()
    for file in md_files:
        urls = extract_claimed_live_urls_from_file(file)
        all_urls.update(urls)

    # If no explicitly claimed URLs found, that's fine - no live URLs to verify
    if not all_urls:
        print("No explicitly claimed live URLs found in documentation.")
        print("No live URLs to verify.")
        return 0

    urls_sorted = sorted(all_urls)
    print(f"Checking {len(urls_sorted)} documented live URL(s)...")
    print()

    failed = 0
    for url in urls_sorted:
        print(f"Checking {url} ... ", end="", flush=True)
        success, msg = check_url(url, args.timeout)
        print(msg)
        if not success:
            failed += 1
            if args.fail_fast:
                break

    print()
    if failed == 0:
        print(f"All {len(urls_sorted)} claimed live URL(s) returned 2xx.")
        return 0
    else:
        print(f"{failed} out of {len(urls_sorted)} claimed live URL(s) failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
