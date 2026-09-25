#!/bin/sh
# Render the scrape token into a runtime Prometheus config, then exec Prometheus.
# Fail loudly when the token is missing: a scraper that silently gets 403 on
# /metrics would leave the whole alert set blind (the exact failure mode #47 is
# about).
set -eu

if [ -z "${METRICS_TOKEN:-}" ]; then
    echo "FATAL: METRICS_TOKEN is unset — /metrics is token-gated, refusing to start a blind scraper" >&2
    exit 1
fi

mkdir -p /etc/prometheus/run
umask 077
sed \
    -e "s|__METRICS_TOKEN__|${METRICS_TOKEN}|" \
    -e "s|__SCRAPE_INTERVAL__|${SCRAPE_INTERVAL:-15s}|" \
    -e "s|__EVALUATION_INTERVAL__|${EVALUATION_INTERVAL:-15s}|" \
    /etc/prometheus/prometheus.yml.tmpl > /etc/prometheus/run/prometheus.yml

exec /bin/prometheus \
    --config.file=/etc/prometheus/run/prometheus.yml \
    --storage.tsdb.path=/prometheus \
    --storage.tsdb.retention.time="${RETENTION:-15d}" \
    --web.enable-lifecycle
