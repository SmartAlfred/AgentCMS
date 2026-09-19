#!/bin/bash
# scripts/check_doc_urls.sh
#
# Extract documented URLs from markdown files and verify they return 2xx.
# This ensures documentation doesn't contain dead links.
#
# Usage: scripts/check_doc_urls.sh [--fail-fast] [--timeout SECONDS]
#
# Exit codes:
#   0 - all URLs return 2xx
#   1 - one or more URLs failed
#   2 - usage error

set -euo pipefail

FAIL_FAST=false
TIMEOUT=10
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Extract and verify documented URLs from markdown files.

Options:
    --fail-fast       Stop on first failing URL
    --timeout SEC     HTTP timeout in seconds (default: 10)
    -h, --help        Show this help

The script searches for URLs in markdown files that appear to be "live" references
(i.e., URLs in prose, code blocks, or commands that are presented as working endpoints).
It skips obvious placeholders like example.com, localhost, your-domain.com, etc.
EOF
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --fail-fast) FAIL_FAST=true; shift ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

# Patterns to identify placeholder/example URLs that should be skipped
PLACEHOLDER_PATTERNS=(
    'example\.com'
    'your-domain\.com'
    'your-cms-domain\.com'
    'cms\.example\.com'
    'your-instance\.example\.com'
    'localhost'
    '127\.0\.0\.1'
    '0\.0\.0\.0'
    'your-org'
    'your-app'
    '<.*>'
    '\$\{.*\}'
    'fonts\.googleapis\.com'
    'fonts\.gstatic\.com'
)

# Build grep pattern to exclude placeholders
EXCLUDE_PATTERN=$(IFS='|'; echo "${PLACEHOLDER_PATTERNS[*]}")

# Find all markdown files (excluding .venv, .pytest_cache, node_modules, .git)
MD_FILES=$(find "$REPO_ROOT" -name "*.md" -type f \
    ! -path "*/.venv/*" \
    ! -path "*/.pytest_cache/*" \
    ! -path "*/node_modules/*" \
    ! -path "*/.git/*" \
    | sort)

# Extract URLs from markdown files
# Matches: https://... URLs in prose, code blocks, and commands
# Uses a more precise regex that handles markdown link syntax
URLS=()
while IFS= read -r file; do
    # Extract URLs, handling markdown syntax like [text](url) and bare URLs
    # Match http(s):// followed by valid URL chars, stopping at common delimiters
    grep -oE 'https?://[^][()<>"\s`]{2,}' "$file" 2>/dev/null | \
    grep -vE "$EXCLUDE_PATTERN" | \
    while IFS= read -r url; do
        # Clean up trailing punctuation that's not part of the URL
        url="${url%.}"
        url="${url%,}"
        url="${url%)}"
        url="${url%]}"
        url="${url%>}"
        url="${url%\"}"
        url="${url%\'}"
        URLS+=("$url")
    done
done <<< "$MD_FILES"

# Deduplicate
IFS=$'\n' URLS=($(sort -u <<<"${URLS[*]}"))
unset IFS

if [[ ${#URLS[@]} -eq 0 ]]; then
    echo "No live URLs found to check."
    exit 0
fi

echo "Checking ${#URLS[@]} unique documented URLs..."
echo

FAILED=0
for url in "${URLS[@]}"; do
    echo -n "Checking $url ... "
    if http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$url" 2>/dev/null); then
        if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
            echo "OK ($http_code)"
        else
            echo "FAILED ($http_code)"
            FAILED=$((FAILED + 1))
            if [[ "$FAIL_FAST" == "true" ]]; then
                break
            fi
        fi
    else
        echo "FAILED (connection error)"
        FAILED=$((FAILED + 1))
        if [[ "$FAIL_FAST" == "true" ]]; then
            break
        fi
    fi
done

echo
if [[ $FAILED -eq 0 ]]; then
    echo "All ${#URLS[@]} URLs returned 2xx."
    exit 0
else
    echo "$FAILED out of ${#URLS[@]} URLs failed."
    exit 1
fi