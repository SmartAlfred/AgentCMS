"""The self-hosted ops set is a tested surface, not a folder of YAML (#47).

Two production blockers in this repo shipped because a surface had zero tests
(the human dashboard, the deploy smoke script).  The monitoring stack is the
next such surface, so what a self-hoster runs is asserted here:

- the observability compose file parses, pins every image by digest, publishes
  nothing outside loopback, and attaches to the app stack's network;
- Prometheus scrapes the token-gated ``/metrics`` with ``X-Metrics-Token``
  rendered from an env var at container start (never committed) and mounts the
  rules file that ``tests/test_alerts.py`` keeps in parity with the code;
- every ``job="..."`` a rule selects on exists as a scrape job in
  ``deploy/compose/observability/prometheus.yml`` (rule/scrape drift is a test
  failure, not a silent no-data alert);
- every metric name the exporter rules reference is really rendered by
  ``scripts/ops/docker_exporter.py``;
- the Alertmanager route delivers to the sink with ``send_resolved`` and the
  sink formats firing *and* resolved payloads and posts to the ntfy topic;
- Grafana's provisioned datasource uid matches what the committed dashboard
  references, so the dashboard is not broken on import.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from app.observability.alerts import ALERTS
from scripts.ops import alert_sink, docker_exporter

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/compose/docker-compose.observability.yml"
OBS_DIR = ROOT / "deploy/compose/observability"
PROM_CONFIG = OBS_DIR / "prometheus.yml"
ALERTMANAGER = OBS_DIR / "alertmanager.yml"
GRAFANA_DATASOURCE = OBS_DIR / "grafana-provisioning/datasources/prometheus.yml"
DASHBOARD = ROOT / "docs/ops/grafana-dashboard.json"


def _compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())


class TestObservabilityCompose:
    def test_parses_and_pins_every_image_by_digest(self) -> None:
        services = _compose()["services"]
        assert services, "no services in the observability compose file"
        for name, service in services.items():
            image = service.get("image", "")
            assert "@sha256:" in image, f"{name} image is not digest-pinned: {image!r}"
            assert not image.endswith(":latest"), f"{name} uses a floating tag"

    def test_every_published_port_is_loopback_only(self) -> None:
        for name, service in _compose()["services"].items():
            for mapping in service.get("ports", []):
                assert str(mapping).startswith("127.0.0.1:"), f"{name} publishes {mapping!r} beyond loopback"

    def test_attaches_to_the_app_stack_network_and_requires_its_secrets(self) -> None:
        compose = _compose()
        assert compose["networks"]["default"]["external"] is True
        assert "AGENTCMS_NETWORK" in compose["networks"]["default"]["name"]
        prometheus_env = compose["services"]["prometheus"]["environment"]
        assert "METRICS_TOKEN" in prometheus_env["METRICS_TOKEN"]
        grafana_env = compose["services"]["grafana"]["environment"]
        assert "GRAFANA_ADMIN_PASSWORD" in grafana_env["GF_SECURITY_ADMIN_PASSWORD"]

    def test_mounts_the_committed_rules_dashboard_and_exporter(self) -> None:
        volumes = " ".join(
            str(volume)
            for service in _compose()["services"].values()
            for volume in service.get("volumes", [])
        )
        assert "docs/ops/prometheus-rules.yml:/etc/prometheus/rules/agentcms.yml" in volumes
        assert "docs/ops/grafana-dashboard.json:/etc/grafana/dashboards/agentcms-ops.json" in (volumes)
        assert "scripts/ops/docker_exporter.py" in volumes
        assert "scripts/ops/alert_sink.py" in volumes


class TestPrometheusConfig:
    def parse(self) -> dict[str, Any]:
        # The token/interval placeholders are substituted at container start by
        # prometheus-entrypoint.sh, so the committed file is a template.
        raw = PROM_CONFIG.read_text()
        for placeholder in ("__METRICS_TOKEN__", "__SCRAPE_INTERVAL__", "__EVALUATION_INTERVAL__"):
            assert placeholder in raw, f"{placeholder} missing: the entrypoint has nothing to render"
        return yaml.safe_load(
            raw.replace("__METRICS_TOKEN__", "token")
            .replace("__SCRAPE_INTERVAL__", "15s")
            .replace("__EVALUATION_INTERVAL__", "15s")
        )

    def test_scrapes_the_token_gated_metrics_endpoint(self) -> None:
        config = self.parse()
        jobs = {job["job_name"]: job for job in config["scrape_configs"]}
        api = jobs["agentcms-api"]
        assert api["metrics_path"] == "/metrics"
        token = api["http_headers"]["X-Metrics-Token"]
        # `secrets` is sent but redacted from /api/v1/status/config.
        assert token["secrets"] == ["token"], token

    def test_rules_are_loaded_from_the_committed_file(self) -> None:
        config = self.parse()
        assert "/etc/prometheus/rules/agentcms.yml" in config["rule_files"]
        assert config["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == ["alertmanager:9093"]

    def test_probes_both_healthz_and_readyz_from_outside_the_app(self) -> None:
        config = self.parse()
        targets = {
            (job["static_configs"][0]["labels"].get("probe"), job["static_configs"][0]["targets"][0])
            for job in config["scrape_configs"]
            if job["job_name"].startswith("agentcms-")
        }
        assert ("readyz", "http://api:8000/readyz") in targets
        assert ("healthz", "http://api:8000/healthz") in targets

    def test_every_job_a_rule_selects_on_exists(self) -> None:
        jobs = {job["job_name"] for job in self.parse()["scrape_configs"]}
        referenced = set()
        for rule in ALERTS.values():
            referenced.update(re.findall(r'job="([^"]+)"', rule.promql))
        missing = referenced - jobs
        assert not missing, f"rules select on scrape jobs that do not exist: {sorted(missing)}"


class TestDockerExporter:
    @staticmethod
    def container(
        name: str = "ws3-drill-api-1",
        *,
        project: str = "ws3-drill",
        service: str = "api",
        state: str = "running",
        exit_code: int = 0,
        restarts: int = 0,
    ) -> dict[str, Any]:
        return {
            "Id": "0123456789abcdef",
            "Name": f"/{name}",
            "RestartCount": restarts,
            "State": {"Status": state, "ExitCode": exit_code},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": project,
                    "com.docker.compose.service": service,
                }
            },
        }

    def test_renders_restart_count_and_exit_code(self) -> None:
        text = docker_exporter.render_metrics(
            [
                self.container(restarts=3),
                self.container(name="ws3-drill-migrate-1", service="migrate", state="exited", exit_code=1),
            ]
        )
        assert (
            'agentcms_container_restart_count{container="ws3-drill-api-1",'
            'project="ws3-drill",service="api",state="running"} 3' in text
        )
        assert (
            'agentcms_container_last_exit_code{container="ws3-drill-migrate-1",'
            'project="ws3-drill",service="migrate",state="exited"} 1' in text
        )
        assert "# TYPE agentcms_container_restart_count gauge" in text

    def test_export_project_keeps_other_stacks_out_of_our_alerts(self) -> None:
        text = docker_exporter.render_metrics(
            [
                self.container(),
                self.container(name="other-api-1", project="not-ours"),
            ],
            project="ws3-drill",
        )
        assert "ws3-drill-api-1" in text
        assert "other-api-1" not in text

    def test_a_restart_that_settles_between_scrapes_is_still_observed(self) -> None:
        """The rule must not depend on catching a crash loop mid-flight.

        A container that restarted 3x in 12s and then settled is invisible to
        ``changes(RestartCount[10m])`` at a 15s scrape interval (measured
        2026-09-25), so the exporter records *when* it saw the increase.
        """
        seen: dict[str, tuple[int, float]] = {}
        docker_exporter.observe_restarts([self.container(restarts=0)], seen, now=100.0)
        docker_exporter.observe_restarts([self.container(restarts=3)], seen, now=105.0)
        text = docker_exporter.render_metrics([self.container(restarts=3)], observed=seen)
        assert (
            "agentcms_container_last_restart_observed_timestamp_seconds"
            '{container="ws3-drill-api-1",project="ws3-drill",service="api"} 105' in text
        )
        # A container that has never restarted emits nothing, so the rule is silent.
        assert "last_restart_observed_timestamp_seconds{" not in docker_exporter.render_metrics(
            [self.container()]
        )

    def test_a_container_already_restarted_when_watching_began_is_not_silent(self) -> None:
        seen: dict[str, tuple[int, float]] = {}
        docker_exporter.observe_restarts([self.container(restarts=2)], seen, now=7.0)
        assert seen["ws3-drill-api-1"] == (2, 7.0)

    def test_rules_only_reference_metrics_the_exporter_emits(self) -> None:
        emitted = docker_exporter.render_metrics(
            [self.container(restarts=1)],
            observed={"ws3-drill-api-1": (1, 1790344000.0)},
        )
        for rule in ALERTS.values():
            for metric in (
                "agentcms_container_restart_count",
                "agentcms_container_last_exit_code",
                "agentcms_container_last_restart_observed_timestamp_seconds",
            ):
                if metric in rule.promql:
                    assert metric in emitted, f"{rule.name} references {metric}, which is never emitted"


class TestAlertmanagerRoute:
    def test_routes_everything_to_the_sink_with_resolved_notifications(self) -> None:
        config = yaml.safe_load(ALERTMANAGER.read_text())
        receiver = config["route"]["receiver"]
        assert config["route"]["group_wait"] == "10s"
        webhook = next(r for r in config["receivers"] if r["name"] == receiver)["webhook_configs"][0]
        assert webhook["url"] == "http://alert-sink:9119/alerts"
        assert webhook["send_resolved"] is True


_ALERT_PAYLOAD: dict[str, Any] = {
    "version": "4",
    "groupKey": '{}:{alertname="ReadyzNotOk"}',
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "ReadyzNotOk", "severity": "page", "probe": "readyz"},
            "annotations": {"summary": "/readyz is not 200"},
            "startsAt": "2026-09-25T15:00:00.000Z",
            "endsAt": "0001-01-01T00:00:00Z",
        }
    ],
}


class TestAlertSink:
    def test_formats_a_firing_payload_for_a_human(self) -> None:
        title, body = alert_sink.format_message(_ALERT_PAYLOAD)
        assert title == "[FIRING] agentcms: ReadyzNotOk"
        assert "summary: /readyz is not 200" in body
        assert re.search(r"received_at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", body)

    def test_formats_a_resolved_payload_distinctly(self) -> None:
        resolved = dict(_ALERT_PAYLOAD)
        resolved["alerts"] = [dict(_ALERT_PAYLOAD["alerts"][0], status="resolved")]
        title, body = alert_sink.format_message(resolved)
        assert title.startswith("[RESOLVED]")
        assert "endsAt" in body

    def test_ntfy_delivery_posts_to_the_configured_topic(self, monkeypatch: Any) -> None:
        calls: dict[str, Any] = {}

        class _Response:
            status = 200

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

        def fake_urlopen(request: Any, timeout: float | None = None) -> _Response:
            calls["url"] = request.full_url
            calls["headers"] = request.header_items()
            calls["body"] = request.data
            return _Response()

        monkeypatch.setattr(alert_sink, "NTFY_TOPIC", "agentcms-drill-unit")
        monkeypatch.setattr(alert_sink.urllib.request, "urlopen", fake_urlopen)
        result = alert_sink.deliver_to_ntfy("t", "b")
        assert result["delivered"] is True
        assert calls["url"].endswith("/agentcms-drill-unit")
        assert any(str(k).lower() == "title" for k, _value in calls["headers"])

    def test_without_a_topic_it_reports_not_delivered_rather_than_pretending(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(alert_sink, "NTFY_TOPIC", "")
        result = alert_sink.deliver_to_ntfy("t", "b")
        assert result["delivered"] is False
        assert "NTFY_TOPIC" in result["reason"]


class TestGrafanaProvisioning:
    def test_datasource_uid_matches_what_the_dashboard_references(self) -> None:
        provisioned = yaml.safe_load(GRAFANA_DATASOURCE.read_text())["datasources"][0]
        dashboard = DASHBOARD.read_text()
        referenced = set(re.findall(r"\$\{(DS_[A-Z_]+)\}", dashboard))
        assert referenced, "dashboard no longer references a template datasource"
        assert {provisioned["uid"]} >= referenced
        assert provisioned["isDefault"] is True
        assert provisioned["url"] == "http://prometheus:9090"

    def test_dashboard_json_still_parses_and_is_the_ops_dashboard(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text())
        assert dashboard["uid"] == "agentcms-ops"
        assert dashboard["panels"]
