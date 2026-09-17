"""Operational endpoints (#2): /healthz, /readyz, problem+json shape, request ids."""

from __future__ import annotations

from app.domain.errors import DatabaseUnavailableError
from fastapi.testclient import TestClient


def test_healthz_is_unauthenticated(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "AgentCMS"
    assert body["env"] == "test"


def test_healthz_never_touches_the_database(client: TestClient, monkeypatch) -> None:
    def explode() -> float:  # pragma: no cover - must not be called
        raise AssertionError("/healthz must not query the database")

    monkeypatch.setattr("app.api.health.check_database", explode)
    assert client.get("/healthz").status_code == 200


def test_readyz_reports_database_latency(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["status"] == "ok"
    assert isinstance(body["checks"]["database"]["latency_ms"], (int, float))


def test_readyz_is_503_problem_json_when_the_database_is_down(client: TestClient, monkeypatch) -> None:
    def explode() -> float:
        raise DatabaseUnavailableError("The database is not reachable: OperationalError.")

    monkeypatch.setattr("app.api.health.check_database", explode)
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "2"
    body = response.json()
    assert body["code"] == "database-unavailable"
    assert body["status"] == 503
    assert body["type"] == "https://agentcms.dev/problems/database-unavailable"
    assert body.get("hint")
    assert body["instance"] == "/readyz"
    assert body["request_id"]


def test_request_id_is_echoed_and_reused_in_problem_bodies(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "req-abc-123"})
    assert response.headers["X-Request-ID"] == "req-abc-123"

    problem = client.get("/nope-not-a-route", headers={"X-Request-ID": "req-def-456"})
    assert problem.status_code == 404
    assert problem.headers["content-type"] == "application/problem+json"
    assert problem.json()["request_id"] == "req-def-456"


def test_unknown_route_returns_actionable_problem(client: TestClient) -> None:
    body = client.get("/v1/does-not-exist").json()
    assert body["title"] == "Endpoint not found"
    assert body["code"] == "endpoint-not-found"
    assert "/openapi.json" in body["hint"]


def test_root_pointer_lists_the_agent_facing_surfaces(client: TestClient) -> None:
    body = client.get("/").json()
    assert "instructions" in body
    assert body["llms_txt"] == "/llms.txt"
    assert body["openapi"] == "/openapi.json"
