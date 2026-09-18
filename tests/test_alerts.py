"""Tests for committed alert rules (#24).

Acceptance criteria covered here:
- every rule exists as committed code and in docs/ops/prometheus-rules.yml
  (parity), and
- the 5xx-ratio and outbox-backlog rules actually fire against live app
  state / the live database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.event_outbox import EventOutbox
from app.models.site import Site
from sqlalchemy.orm import Session

RULES_FILE = "docs/ops/prometheus-rules.yml"


def _create_site(session: Session, slug: str = "blog") -> Site:
    site = Site(id=uuid.uuid4(), slug=slug, name=f"Test {slug}", publish_mode="auto")
    session.add(site)
    session.commit()
    return site


def _observe_5xx(ratio: float, count: int = 200) -> None:
    """Push ``count`` requests into the 5-minute window at the given 5xx ratio."""
    from app.observability import metrics
    from app.observability_types import RequestObservation

    ok_count = int(count * (1 - ratio))
    error_count = count - ok_count
    for i in range(count):
        status = 500 if i < error_count else 200
        obs = RequestObservation(
            request_id=uuid.uuid4().hex,
            method="GET",
            path_template="/v1/posts/{post_id}",
            status=status,
            duration_ms=5.0,
            actor_id="",
            actor_label="",
            actor_kind="",
            source="api",
            ip="127.0.0.1",
            user_agent="test",
            bytes_in=0,
            bytes_out=len(b"{}"),
            bytes_err="",
        )
        metrics.observe_request(obs)


class TestRuleParity:
    """The committed rules file and the python predicates never diverge."""

    def test_every_rule_is_committed_as_promql(self) -> None:
        from app.observability.alerts import ALERTS, parse_rules_yaml, render_promql_rules

        with open(RULES_FILE) as fh:
            raw = fh.read()
        assert "HighErrorRatio5xx" in raw
        # Regenerating from code must reproduce the file byte-for-byte.
        assert render_promql_rules() == raw

        rendered = parse_rules_yaml(raw)
        assert set(rendered) == set(ALERTS)
        for name, rule in ALERTS.items():
            expr = " ".join(rule.promql.split())
            assert rendered[name] == expr, f"committed promql for {name} drifted"

    def test_every_rule_has_a_python_predicate(self) -> None:
        from app.observability.alerts import ALERTS

        for name, rule in ALERTS.items():
            assert rule.fires is not None, f"{name} lacks a staging-testable predicate"
            assert rule.severity in ("page", "warning")
            assert rule.summary


class TestRuleFiring:
    """Alerts must actually fire against live state — the staging-test criterion."""

    def test_high_error_ratio_5xx_fires(self) -> None:
        from app.config import get_settings
        from app.observability import metrics
        from app.observability.alerts import evaluate

        metrics.reset_recent_windows_for_test()
        _observe_5xx(ratio=0.05)  # 5% of requests are 5xx
        fired = evaluate(get_settings(), snapshot=None)
        assert "HighErrorRatio5xx" in fired

    def test_high_error_ratio_does_not_fire_when_healthy(self) -> None:
        from app.config import get_settings
        from app.observability import metrics
        from app.observability.alerts import evaluate

        metrics.reset_recent_windows_for_test()
        _observe_5xx(ratio=0.002)  # 0.2%
        fired = evaluate(get_settings(), snapshot=None)
        assert "HighErrorRatio5xx" not in fired

    def test_outbox_backlog_fires_against_real_db(self, db: Session, app: object) -> None:
        from app.config import get_settings
        from app.observability.alerts import evaluate

        _create_site(db)
        old = datetime.now(UTC) - timedelta(minutes=30)
        for i in range(1001):
            event = EventOutbox(
                id=uuid.uuid4(),
                event_type="post.created",
                site_slug="blog",
                payload={"i": i},
                dispatched=False,
                created_at=datetime.now(UTC) if i % 2 else old,
            )
            db.add(event)
        db.commit()

        fired = evaluate(get_settings(), db=db)
        assert "OutboxBacklog" in fired

    def test_outbox_backlog_silent_when_drained(self, db: Session, app: object) -> None:
        from app.config import get_settings
        from app.observability.alerts import evaluate

        _create_site(db)
        event = EventOutbox(
            id=uuid.uuid4(),
            event_type="post.created",
            site_slug="blog",
            payload={},
            dispatched=True,
            dispatched_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        db.add(event)
        db.commit()

        fired = evaluate(get_settings(), db=db)
        assert "OutboxBacklog" not in fired

    def test_backup_missed_fires_when_no_success_recorded(self, db: Session, app: object) -> None:
        from app.config import get_settings
        from app.observability import metrics
        from app.observability.alerts import evaluate

        _create_site(db)
        metrics.backup_last_success_timestamp.set(0.0)
        fired = evaluate(get_settings(), db=db)
        assert "BackupMissed" in fired
