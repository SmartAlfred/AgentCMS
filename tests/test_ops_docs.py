"""Committed ops artifacts for #24 stay present, valid and wired.

Covered:
- runbook exists with the six drills + timing table
- Grafana dashboard JSON parses
- Prometheus rules render byte-identical (parity is asserted in test_alerts)
- deploy / rollback / preflight scripts exist, are executable and reference
  the right calls
- every alert in ALERTS is a real committed rule (see test_alerts too)
- ``make help`` surfaces the new #24 targets
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestRunbookDocument:
    DRILLS = (
        "1. Secret-in-logs",
        "2. Alert / profiling",
        "3. Backup + restore-drill",
        "4. Migration-gated deploy + rollback",
        "5. Request-id trace drill",
        "6. Escalation / P-R",
    )
    MEASURED = "Measured runtimes"

    def test_runbook_lists_all_six_drills(self) -> None:
        runbook = (ROOT / "docs/ops/runbook.md").read_text()
        for drill in self.DRILLS:
            assert drill in runbook, f"runbook missing drill: {drill}"
        assert self.MEASURED in runbook

    def test_grafana_dashboard_is_valid_json(self) -> None:
        dash = json.loads((ROOT / "docs/ops/grafana-dashboard.json").read_text())
        assert dash["title"] == "AgentCMS Operations"
        assert dash["uid"] == "agentcms-ops"
        exprs = " ".join(
            panel.get("targets", [{}])[0].get("expr", "")
            for panel in dash["panels"]
            if panel.get("type") in ("stat", "timeseries", "gauge")
        )
        assert "http_requests_total" in exprs
        assert "agentcms_outbox_backlog" in exprs
        assert "agentcms_backup_last_success_timestamp" in exprs

    def test_prometheus_rules_rendered_file_present(self) -> None:
        raw = (ROOT / "docs/ops/prometheus-rules.yml").read_text()
        assert "groups:" in raw and "agentcms-alerts" in raw


class TestDeployTooling:
    def test_deploy_scripts_exist_and_are_executable(self) -> None:
        for name in ("deploy.sh", "deploy_preflight.sh"):
            path = ROOT / "scripts" / name
            assert path.exists(), f"missing {name}"
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode & stat.S_IXUSR, f"{name} not executable (chmod +x)"
            content = path.read_text()
            assert content.startswith("#!/usr/bin/env bash")
            if name == "deploy.sh":
                assert "--rollback" in content
                assert "readyz" in content
                assert "migrate" in content

    def test_compose_prod_defines_migrate_and_api_healthcheck(self) -> None:
        compose = (ROOT / "compose.prod.yml").read_text()
        assert "migrate:" in compose
        assert "alembic" in compose
        assert "healthcheck:" in compose
        assert "/readyz" in compose

    def test_make_help_lists_ops_targets(self) -> None:
        out = subprocess.run(["make", "help"], capture_output=True, text=True, check=False, cwd=ROOT)
        assert out.returncode == 0
        for target in ("backup", "restore-drill", "deploy", "rollback", "alert-rules"):
            assert target in out.stdout, f"make help missing target dril {target}"
