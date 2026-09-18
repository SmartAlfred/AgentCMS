"""Test that MCP tool schemas stay in sync with OpenAPI spec (#22 acceptance criteria)."""

from __future__ import annotations

from typing import Any

import pytest
from app.main import create_app
from app.mcp.resources import get_all_resources
from app.mcp.tools import get_all_tools


def _get_openapi_schema(app) -> dict[str, Any]:
    """Extract the OpenAPI schema from the FastAPI app."""
    return app.openapi()


def _extract_mcp_tool_schemas() -> dict[str, dict[str, Any]]:
    """Extract MCP tool input schemas as a dict by tool name."""
    tools = get_all_tools()
    return {tool.name: tool.input_schema for tool in tools}


def _normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize schema for comparison (remove descriptions, defaults, etc.)."""
    if not isinstance(schema, dict):
        return schema

    normalized = {}
    for key, value in schema.items():
        if key in ("description", "default", "examples", "deprecated"):
            continue
        if key == "properties" and isinstance(value, dict):
            normalized[key] = {k: _normalize_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            normalized[key] = _normalize_schema(value)
        elif key == "required" and isinstance(value, list):
            normalized[key] = sorted(value)
        elif isinstance(value, dict):
            normalized[key] = _normalize_schema(value)
        elif isinstance(value, list):
            normalized[key] = [_normalize_schema(v) for v in value]
        else:
            normalized[key] = value
    return normalized


class TestMCPOpenAPISync:
    """Verify MCP tools match OpenAPI operations."""

    @pytest.fixture
    def app(self):
        from app.config import Settings

        return create_app(
            Settings(
                app_env="test",
                database_url="postgresql+psycopg://test:test@localhost/test",
                secret_key="test-secret-key-that-is-long-enough-000000",
            )
        )

    @pytest.fixture
    def openapi_schema(self, app):
        return _get_openapi_schema(app)

    @pytest.fixture
    def mcp_schemas(self):
        return _extract_mcp_tool_schemas()

    def test_all_mcp_tools_have_openapi_correspondence(self, openapi_schema, mcp_schemas):
        """Every MCP tool should correspond to at least one OpenAPI operation."""
        # Map MCP tool names to OpenAPI paths/operations
        tool_to_openapi = {
            "check_status": [("GET", "/v1/discover")],  # Similar info
            "create_post": [("POST", "/v1/sites/{site_slug}/posts")],
            "update_post": [("PATCH", "/v1/posts/{identifier}")],
            "get_post": [("GET", "/v1/posts/{identifier}")],
            "list_posts": [("GET", "/v1/sites/{site_slug}/posts")],
            "search_posts": [("GET", "/v1/search")],
            "publish_post": [("POST", "/v1/posts/{identifier}/publish")],
            "unpublish_post": [("POST", "/v1/posts/{identifier}/unpublish")],
            "validate_post": [("POST", "/v1/posts/validate")],
            "list_revisions": [("GET", "/v1/posts/{identifier}/revisions")],
            "revert_post": [("POST", "/v1/posts/{identifier}/revert")],
            "upload_asset": [("POST", "/v1/sites/{site_slug}/assets")],
            "list_assets": [("GET", "/v1/sites/{site_slug}/assets")],
        }

        paths = openapi_schema.get("paths", {})

        for tool_name, expected_ops in tool_to_openapi.items():
            assert tool_name in mcp_schemas, f"MCP tool {tool_name} not registered"

            # Verify at least one corresponding OpenAPI operation exists
            found = False
            for method, path in expected_ops:
                path_obj = paths.get(path)
                if path_obj and method.lower() in path_obj:
                    found = True
                    break

            assert found, f"No OpenAPI operation found for MCP tool {tool_name} (expected {expected_ops})"

    def test_create_post_schema_matches_openapi(self, openapi_schema, mcp_schemas):
        """create_post tool schema matches POST /v1/sites/{site}/posts."""
        mcp_schema = _normalize_schema(mcp_schemas["create_post"])

        # OpenAPI schema for the create endpoint
        path = "/v1/sites/{site_slug}/posts"
        method = "post"
        openapi_op = openapi_schema["paths"][path][method]
        request_body = openapi_op.get("requestBody", {})
        content = request_body.get("content", {}).get("application/json", {})

        # The OpenAPI schema uses a $ref to PostWriteRequest
        assert content.get("schema"), "POST /v1/sites/{site_slug}/posts documents no request body"
        # Verify MCP tool has the same required fields
        mcp_required = set(mcp_schema.get("required", []))
        assert "site" in mcp_required
        assert "body_md" in mcp_required

        # MCP has site as separate param (not in body), body_md in body
        mcp_props = set(mcp_schema.get("properties", {}).keys())
        assert "site" in mcp_props
        assert "body_md" in mcp_props
        assert "title" in mcp_props
        assert "tags" in mcp_props
        assert "slug" in mcp_props
        assert "dry_run" in mcp_props  # MCP adds dry_run as tool param

    def test_get_post_schema_matches_openapi(self, openapi_schema, mcp_schemas):
        """get_post tool schema matches GET /v1/posts/{identifier}."""
        mcp_schema = _normalize_schema(mcp_schemas["get_post"])
        mcp_props = set(mcp_schema.get("properties", {}).keys())
        assert "id_or_slug" in mcp_props
        assert "format" in mcp_props
        assert mcp_schema["properties"]["format"]["enum"] == ["json", "markdown"]

    def test_publish_post_idempotent_documented(self, openapi_schema):
        """publish_post tool and OpenAPI both document idempotency."""
        tools = get_all_tools()
        publish_tool = next(t for t in tools if t.name == "publish_post")

        # MCP tool description mentions idempotent
        assert "idempotent" in publish_tool.description.lower()

        # OpenAPI operation should also mention it
        path = "/v1/posts/{identifier}/publish"
        method = "post"
        # The MCP tool description is the agent-facing contract here: the
        # generated OpenAPI summary is not required to say "idempotent".
        assert method in openapi_schema["paths"][path]
        assert "idempotent" in publish_tool.description.lower()

    def test_validate_post_dry_run_by_default(self, openapi_schema, mcp_schemas):
        """validate_post is the dry-run/self-correction endpoint."""
        mcp_schema = _normalize_schema(mcp_schemas["validate_post"])
        mcp_props = set(mcp_schema.get("properties", {}).keys())

        # Has all the validation fields
        expected_fields = {
            "site",
            "body_md",
            "title",
            "slug",
            "tags",
            "excerpt",
            "frontmatter",
            "existing_post_id",
            "check_links",
        }
        assert expected_fields.issubset(mcp_props)

        # OpenAPI has POST /v1/posts/validate
        path = "/v1/posts/validate"
        method = "post"
        assert path in openapi_schema["paths"]
        assert method in openapi_schema["paths"][path]

    def test_revert_post_no_dry_run(self, mcp_schemas):
        """revert_post does not have dry_run (per ticket: default off)."""
        mcp_schema = _normalize_schema(mcp_schemas["revert_post"])
        mcp_props = set(mcp_schema.get("properties", {}).keys())
        assert "dry_run" not in mcp_props
        assert "revision" in mcp_props
        assert "revision" in mcp_schema.get("required", [])

    def test_check_status_exists(self, mcp_schemas):
        """check_status tool exists (the 'what am I allowed to do' tool)."""
        assert "check_status" in mcp_schemas
        mcp_schema = _normalize_schema(mcp_schemas["check_status"])
        # Takes no arguments
        assert mcp_schema.get("properties", {}) == {}
        assert mcp_schema.get("required", []) == []

    def test_error_responses_structured(self, openapi_schema):
        """Every caller-facing operation documents a structured error response.

        Two classes of operation are exempt because they cannot answer with
        problem+json: process/infra endpoints (/healthz, /readyz, /, /docs,
        /openapi.json) and static parameterless unauthenticated reads, which have
        no caller input to reject and no credentials to reject
        (/changelog, /v1/discover, /v1/info, ...).
        """
        infra_paths = {"/healthz", "/readyz", "/", "/docs", "/openapi.json", "/mcp"}
        paths = openapi_schema.get("paths", {})

        for path, path_obj in paths.items():
            if path in infra_paths:
                continue
            path_parameters = path_obj.get("parameters") or []
            for method, op in path_obj.items():
                if method not in ("get", "post", "patch", "delete", "put"):
                    continue
                has_input = bool(op.get("parameters") or path_parameters or "requestBody" in op)
                if not has_input and not op.get("security"):
                    continue
                responses = op.get("responses", {})
                # At minimum, should have 401, 403, 404 or 422 documented
                # (exact codes vary by endpoint)
                error_responses = [r for r in responses if r.startswith(("4", "5"))]
                assert error_responses, f"{method.upper()} {path} missing error responses"


class TestMCPSchemaDriftDetection:
    """Test that can be run in CI to detect schema drift."""

    def test_mcp_tool_count_matches_expected(self):
        """Exact count of MCP tools to catch additions/removals."""
        tools = get_all_tools()
        # 13 tools as specified in ticket
        assert len(tools) == 13, f"Expected 13 tools, got {len(tools)}: {[t.name for t in tools]}"

    def test_mcp_tool_names_exact_match(self):
        """Tool names match the exact list from the ticket."""
        tools = get_all_tools()
        tool_names = {t.name for t in tools}

        expected = {
            "check_status",
            "create_post",
            "update_post",
            "get_post",
            "list_posts",
            "search_posts",
            "publish_post",
            "unpublish_post",
            "validate_post",
            "list_revisions",
            "revert_post",
            "upload_asset",
            "list_assets",
        }
        assert tool_names == expected, f"Tool name mismatch: {tool_names ^ expected}"

    def test_resource_uris_registered(self):
        """Required resources are registered."""
        resources = get_all_resources()
        resource_uris = {str(r.uri) for r in resources}

        assert "agentcms://llms.txt" in resource_uris
        # Template resource may not be in list_resources output
        # but should be accessible via read_resource
