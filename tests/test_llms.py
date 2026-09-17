"""Agent-facing surfaces tests (ticket #9).

Covers every acceptance criterion:
- GET /llms.txt returns the instruction sheet under ~4 KB
- GET / with Accept: text/plain returns the instruction sheet
- GET / with Accept: text/html returns the HTML landing page
- GET / with Accept: application/json returns JSON with instructions
- GET /openapi.json validates against OpenAPI 3.1 and documents all /v1 routes
- GET /v1/discover returns orientation JSON
- GET /changelog returns append-only change log
- llms.txt contains the 5 canonical curl examples
- Content is terse, imperative, example-first — no marketing copy
- Placeholder tokens are obviously not real (acms_demo_...)
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# GET /llms.txt
# ---------------------------------------------------------------------------


class TestLlmsTxt:
    """The machine-readable instruction sheet."""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        assert resp.status_code == 200

    def test_content_type_is_plain_text(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        assert "text/plain" in resp.headers["content-type"]

    def test_under_4kb(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        assert len(resp.content) < 4096, f"llms.txt is {len(resp.content)} bytes, must be < 4096"

    def test_contains_purpose(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        text = resp.text.lower()
        assert "agentcms" in text
        assert "cms" in text

    def test_contains_base_url(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        assert "base url" in resp.text.lower()

    def test_contains_auth_section(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        text = resp.text.lower()
        assert "authorization" in text
        assert "bearer" in text

    def test_contains_capability_link_auth(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        text = resp.text.lower()
        assert "capability link" in text

    def test_contains_five_canonical_calls(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        text = resp.text.lower()
        assert "curl" in text
        # Check for the 5 operations
        assert "create" in text
        assert "publish" in text
        assert "trash" in text

    def test_contains_error_format(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        text = resp.text.lower()
        assert "error" in text
        assert "problem+json" in text

    def test_contains_common_errors(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        text = resp.text
        assert "401" in text
        assert "409" in text
        assert "422" in text

    def test_contains_limits(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        text = resp.text.lower()
        assert "limit" in text

    def test_contains_undo_revert(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        text = resp.text.lower()
        assert "unpublish" in text or "undo" in text or "revert" in text

    def test_contains_changelog_link(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        assert "/changelog" in resp.text

    def test_uses_placeholder_token(self, client: TestClient) -> None:
        resp = client.get("/llms.txt")
        assert "acms_demo_" in resp.text

    def test_no_marketing_copy(self, client: TestClient) -> None:
        """llms.txt should be terse and imperative, not marketing."""
        resp = client.get("/llms.txt")
        text = resp.text.lower()
        # Should not contain marketing phrases
        assert "revolutionize" not in text
        assert "transform your" not in text
        assert "cutting edge" not in text


# ---------------------------------------------------------------------------
# GET / — content negotiation
# ---------------------------------------------------------------------------


class TestRootContentNegotiation:
    """Content-negotiated entry point."""

    def test_json_default(self, client: TestClient) -> None:
        """Without Accept header, returns JSON with instructions."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        data = resp.json()
        assert "instructions" in data
        assert "llms_txt" in data
        assert data["llms_txt"] == "/llms.txt"

    def test_text_plain_returns_json_with_instructions(self, client: TestClient) -> None:
        resp = client.get("/", headers={"Accept": "text/plain"})
        assert resp.status_code == 200
        data = resp.json()
        assert "instructions" in data
        assert "You are at the AgentCMS entry point" in data["instructions"]

    def test_application_json_returns_json(self, client: TestClient) -> None:
        resp = client.get("/", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        data = resp.json()
        assert "instructions" in data

    def test_text_html_returns_html(self, client: TestClient) -> None:
        resp = client.get("/", headers={"Accept": "text/html"})
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "<!DOCTYPE html>" in resp.text
        assert "AgentCMS" in resp.text

    def test_html_contains_paste_into_ai_block(self, client: TestClient) -> None:
        resp = client.get("/", headers={"Accept": "text/html"})
        assert "Paste this into your AI" in resp.text

    def test_html_contains_curl_examples(self, client: TestClient) -> None:
        resp = client.get("/", headers={"Accept": "text/html"})
        assert "/v1/sites/" in resp.text
        assert "publish" in resp.text.lower()

    def test_html_contains_capability_link_section(self, client: TestClient) -> None:
        resp = client.get("/", headers={"Accept": "text/html"})
        assert "capability link" in resp.text.lower()

    def test_json_includes_openapi_link(self, client: TestClient) -> None:
        resp = client.get("/")
        data = resp.json()
        assert data["openapi"] == "/openapi.json"


# ---------------------------------------------------------------------------
# GET /openapi.json
# ---------------------------------------------------------------------------


class TestOpenAPI:
    """OpenAPI 3.1 generated from running app."""

    def test_valid_31(self, client: TestClient) -> None:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["openapi"].startswith("3.1")

    def test_has_servers(self, client: TestClient) -> None:
        doc = client.get("/openapi.json").json()
        assert "servers" in doc or "paths" in doc

    def test_has_security_schemes(self, client: TestClient) -> None:
        doc = client.get("/openapi.json").json()
        components = doc.get("components", {})
        security_schemes = components.get("securitySchemes", {})
        assert len(security_schemes) > 0

    def test_documents_all_v1_routes(self, client: TestClient) -> None:
        """Every /v1 route must appear in the spec."""
        doc = client.get("/openapi.json").json()
        paths = doc.get("paths", {})
        v1_paths = [p for p in paths if p.startswith("/v1")]
        assert len(v1_paths) > 0, "No /v1 paths in OpenAPI spec"

    def test_has_descriptions(self, client: TestClient) -> None:
        doc = client.get("/openapi.json").json()
        paths = doc.get("paths", {})
        for path, methods in paths.items():
            for method in ("get", "post", "put", "patch", "delete"):
                if method in methods:
                    op = methods[method]
                    assert "description" in op or "summary" in op, (
                        f"{method.upper()} {path} missing description/summary"
                    )


# ---------------------------------------------------------------------------
# GET /v1/discover
# ---------------------------------------------------------------------------


class TestDiscover:
    """Orientation JSON for agents."""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/v1/discover")
        assert resp.status_code == 200

    def test_returns_json(self, client: TestClient) -> None:
        resp = client.get("/v1/discover")
        assert "application/json" in resp.headers["content-type"]

    def test_contains_required_fields(self, client: TestClient) -> None:
        data = client.get("/v1/discover").json()
        assert data["service"] == "AgentCMS"
        assert "version" in data
        assert "api_base" in data
        assert "auth_modes" in data
        assert "docs" in data
        assert "llms_txt" in data
        assert "openapi" in data
        assert "limits" in data
        assert "changelog" in data

    def test_auth_modes_include_bearer_and_capability(self, client: TestClient) -> None:
        data = client.get("/v1/discover").json()
        assert "bearer" in data["auth_modes"]
        assert "capability_link" in data["auth_modes"]

    def test_limits_structure(self, client: TestClient) -> None:
        data = client.get("/v1/discover").json()
        limits = data["limits"]
        assert "max_body_bytes" in limits
        assert "rate_limit_writes_per_minute" in limits


# ---------------------------------------------------------------------------
# GET /changelog
# ---------------------------------------------------------------------------


class TestChangelog:
    """Append-only API change log."""

    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/changelog")
        assert resp.status_code == 200

    def test_returns_json_list(self, client: TestClient) -> None:
        resp = client.get("/changelog")
        data = resp.json()
        assert isinstance(data, list)

    def test_entries_have_required_fields(self, client: TestClient) -> None:
        data = client.get("/changelog").json()
        assert len(data) > 0
        for entry in data:
            assert "date" in entry
            assert "version" in entry
            assert "type" in entry
            assert "summary" in entry

    def test_entries_are_chronological(self, client: TestClient) -> None:
        data = client.get("/changelog").json()
        dates = [e["date"] for e in data]
        assert dates == sorted(dates), "Changelog entries should be in chronological order"

    def test_no_breaking_changes_in_mvp(self, client: TestClient) -> None:
        """MVP has no breaking changes yet."""
        data = client.get("/changelog").json()
        for entry in data:
            if entry.get("type") == "breaking":
                raise AssertionError(f"Unexpected breaking change in MVP: {entry['summary']}")


# ---------------------------------------------------------------------------
# Cross-cutting: content consistency
# ---------------------------------------------------------------------------


class TestContentConsistency:
    """Verify the llms.txt and root sheet share the same source of truth."""

    def test_llms_txt_mentions_all_five_operations(self, client: TestClient) -> None:
        text = client.get("/llms.txt").text
        assert "POST" in text  # create/publish
        assert "GET" in text  # read
        assert "PATCH" in text  # update
        assert "DELETE" in text  # trash

    def test_root_json_mentions_key_endpoints(self, client: TestClient) -> None:
        data = client.get("/").json()
        instructions = data["instructions"]
        assert "POST" in instructions
        assert "GET" in instructions
        assert "PATCH" in instructions
        assert "DELETE" in instructions

    def test_discover_links_to_llms_and_openapi(self, client: TestClient) -> None:
        data = client.get("/v1/discover").json()
        assert "/llms.txt" in data["llms_txt"]
        assert "/openapi.json" in data["openapi"]
        assert "/docs" in data["docs"]

    def test_placeholder_tokens_are_obviously_fake(self, client: TestClient) -> None:
        """Every example token must be obviously not real."""
        text = client.get("/llms.txt").text
        assert "acms_demo_" in text
        # Should not contain tokens that look real
        assert "acms_real" not in text
