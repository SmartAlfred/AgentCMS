"""MCP Server tests (#22)."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest
from app.mcp.resources import get_all_resources, handle_resource_read
from app.mcp.server import create_mcp_server
from app.mcp.tools import get_all_tools, handle_tool_call
from sqlalchemy.engine import make_url


@pytest.fixture(autouse=True)
def _suite_database(database_url: str) -> Iterator[None]:
    """Bind every test in this file to the suite's database.

    The MCP tools and resources below drive the app's global engine directly and ask for
    no fixture of their own, so they used to inherit whatever binding the process
    happened to hold: CI's dev default (``.../agentcms``, a database no test creates) and
    then the backup/restore drill's binding to a database it had just dropped.  That is
    run 36130902330 -- these three tests died with ``FATAL: database "agentcms" does not
    exist`` while 802 others passed, because the surface they cover had no fixture and
    rode on ambient state.
    """
    from app.config import reset_settings_cache
    from app.db.session import dispose_engine

    os.environ["DATABASE_URL"] = database_url
    reset_settings_cache()
    dispose_engine()
    yield


def test_these_tests_bind_the_suite_database_themselves() -> None:
    """Regression guard, deliberately asking for no fixture (run 36130902330).

    The autouse fixture above must already have pointed the app at the suite's database
    before this body runs; a test that relies on the ambient default only passes when
    some earlier file happened to leak a usable binding into the process.
    """
    from app.config import DEV_DATABASE_URL, get_settings

    database = get_settings().database_url
    assert database != DEV_DATABASE_URL, (
        "the MCP tests are riding on the app's dev default instead of binding their own database"
    )
    assert make_url(database).database not in ("agentcms", None), (
        f"the MCP tests are pointed at {make_url(database).database!r}, which no test creates"
    )


class TestMCPServerCreation:
    """Test MCP server creation and basic structure."""

    def test_create_mcp_server(self):
        """Server can be created with tools and resources registered."""
        server = create_mcp_server()
        assert server.name == "agentcms"

    def test_server_has_tools_registered(self):
        """All expected tools are registered via get_all_tools."""
        tools = get_all_tools()
        tool_names = {t.name for t in tools}

        expected_tools = {
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
        assert expected_tools.issubset(tool_names), f"Missing tools: {expected_tools - tool_names}"

    def test_server_has_resources_registered(self):
        """All expected resources are registered via get_all_resources."""
        resources = get_all_resources()
        resource_uris = {str(r.uri) for r in resources}

        assert "agentcms://llms.txt" in resource_uris
        assert "agentcms://site/{site}/posts" in resource_uris


class TestMCPTools:
    """Test MCP tool schemas and basic execution."""

    @pytest.fixture
    def tools(self):
        return get_all_tools()

    @pytest.mark.asyncio
    async def test_check_status_tool_schema(self, tools):
        """check_status tool has correct schema."""
        check_status = next(t for t in tools if t.name == "check_status")

        assert check_status.name == "check_status"
        assert (
            "permissions" in check_status.description.lower() or "scopes" in check_status.description.lower()
        )
        # Should accept no arguments
        assert check_status.input_schema["type"] == "object"
        assert check_status.input_schema["properties"] == {}

    @pytest.mark.asyncio
    async def test_create_post_tool_schema(self, tools):
        """create_post tool has correct schema with all parameters."""
        create_post = next(t for t in tools if t.name == "create_post")

        assert create_post.name == "create_post"
        props = create_post.input_schema["properties"]
        assert "site" in props
        assert "body_md" in props
        assert "title" in props
        assert "tags" in props
        assert "slug" in props
        assert "dry_run" in props
        # Required fields
        assert "site" in create_post.input_schema["required"]
        assert "body_md" in create_post.input_schema["required"]

    @pytest.mark.asyncio
    async def test_get_post_tool_schema(self, tools):
        """get_post tool has format parameter."""
        get_post = next(t for t in tools if t.name == "get_post")

        props = get_post.input_schema["properties"]
        assert "id_or_slug" in props
        assert "format" in props
        assert props["format"]["enum"] == ["json", "markdown"]

    @pytest.mark.asyncio
    async def test_validate_post_tool_schema(self, tools):
        """validate_post tool has all validation parameters."""
        validate_post = next(t for t in tools if t.name == "validate_post")

        props = validate_post.input_schema["properties"]
        assert "site" in props
        assert "body_md" in props
        assert "title" in props
        assert "slug" in props
        assert "tags" in props
        assert "excerpt" in props
        assert "frontmatter" in props
        assert "existing_post_id" in props
        assert "check_links" in props
        assert "site" in validate_post.input_schema["required"]

    @pytest.mark.asyncio
    async def test_revert_post_tool_schema(self, tools):
        """revert_post tool marked as destructive."""
        revert_post = next(t for t in tools if t.name == "revert_post")

        # Should mention destructive/irreversible in description
        desc = revert_post.description.lower()
        assert "destructive" in desc or "irreversible" in desc or "caution" in desc
        # Should have required revision parameter
        assert "revision" in revert_post.input_schema["required"]

    @pytest.mark.asyncio
    async def test_all_tools_have_descriptions(self, tools):
        """Every tool has a non-empty description written for a model."""
        for tool in tools:
            assert tool.description, f"Tool {tool.name} missing description"
            assert len(tool.description) > 20, f"Tool {tool.name} description too short"


class TestMCPResources:
    """Test MCP resource registration and reading."""

    @pytest.mark.asyncio
    async def test_llms_txt_resource(self):
        """llms.txt resource returns the instruction sheet."""
        from app.docs_content import LLMS_TXT

        result = await handle_resource_read("agentcms://llms.txt")
        assert result.contents[0].mime_type == "text/plain"
        assert result.contents[0].text == LLMS_TXT

    @pytest.mark.asyncio
    async def test_site_posts_resource_unknown_site(self):
        """site posts resource returns error for unknown site."""
        result = await handle_resource_read("agentcms://site/nonexistent/posts")
        assert result.contents[0].mime_type == "application/json"
        data = json.loads(result.contents[0].text)
        assert "error" in data
        assert "not found" in data["error"].lower()


class TestMCPTransport:
    """The Streamable-HTTP transport, exercised through its real ASGI surface.

    ``app.mcp.transport`` delegates JSON-RPC framing, session headers and SSE to
    the official ``mcp`` SDK session manager, so these tests speak HTTP to the
    endpoint (the same path a real client — Claude Desktop, Cursor, the ``mcp``
    CLI — takes) instead of poking at a private request-processor.
    """

    @pytest.fixture
    def transport(self):
        from app.mcp.transport import create_streamable_http_transport

        server = create_mcp_server()
        return create_streamable_http_transport(server)

    @staticmethod
    def _asgi_app(transport):
        """Wrap the transport app exactly as app.main.register_mcp_routes does."""
        from starlette.applications import Starlette
        from starlette.routing import Route

        return Starlette(routes=[Route("/mcp", transport.app, methods=["GET", "POST", "DELETE"])])

    @staticmethod
    def _rpc_message(text: str) -> dict:
        """Decode the JSON-RPC message the transport frames as an SSE event."""
        frames = [ln[len("data: ") :] for ln in text.splitlines() if ln.startswith("data: ")]
        assert frames, f"no SSE data frame in transport response: {text!r}"
        return json.loads(frames[0])

    @pytest.mark.asyncio
    async def test_initialize_request(self, transport):
        """initialize returns the protocol version, server info and capabilities."""
        import httpx

        async with (
            transport.lifespan(),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._asgi_app(transport)),
                base_url="http://mcp.test",
            ) as client,
        ):
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )

        assert response.status_code == 200, response.text
        message = self._rpc_message(response.text)
        assert message["jsonrpc"] == "2.0"
        assert message["id"] == 1
        result = message["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "agentcms"
        assert "tools" in result["capabilities"]
        assert "resources" in result["capabilities"]

    @pytest.mark.asyncio
    async def test_tools_list_request(self, transport):
        """tools/list returns every registered tool, with input schemas."""
        import httpx

        async with (
            transport.lifespan(),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._asgi_app(transport)),
                base_url="http://mcp.test",
            ) as client,
        ):
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )

        assert response.status_code == 200, response.text
        message = self._rpc_message(response.text)
        assert message["id"] == 2
        tools = message["result"]["tools"]
        assert len(tools) >= 13  # our 13 tools
        names = {tool["name"] for tool in tools}
        assert "check_status" in names
        assert all("inputSchema" in tool for tool in tools)

    @pytest.mark.asyncio
    async def test_invalid_method(self, transport):
        """Unknown method returns a JSON-RPC method-not-found error (-32601)."""
        import httpx

        async with (
            transport.lifespan(),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._asgi_app(transport)),
                base_url="http://mcp.test",
            ) as client,
        ):
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 3, "method": "unknown/method", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )

        assert response.status_code == 200, response.text
        message = self._rpc_message(response.text)
        assert message["id"] == 3
        assert "result" not in message
        assert message["error"]["code"] == -32601


class TestMCPToolExecution:
    """Test actual tool execution."""

    @pytest.mark.asyncio
    async def test_check_status_execution(self):
        """check_status executes and returns structured result (no DB needed)."""
        result = await handle_tool_call("check_status", {})
        assert isinstance(result, dict)
        assert result["service"] == "AgentCMS"
        assert "available_tools" in result
        assert "rate_limits" in result

    @pytest.mark.asyncio
    async def test_create_post_returns_site_error(self):
        """create_post returns site-not-found error without site in DB.

        The slug is randomised on purpose: other tests in the suite leave a
        site called ``blog`` behind, so a fixed slug made this assertion
        depend on test ordering (it only held when the file ran alone).
        """
        from app.domain.errors import SiteNotFoundError

        missing_site = f"missing-{uuid.uuid4().hex[:12]}"
        try:
            result = await handle_tool_call("create_post", {"site": missing_site, "body_md": "# Test"})
            assert isinstance(result, dict)
            assert "error" in result
        except SiteNotFoundError:
            # Exception is raised, which is also acceptable behavior
            pass

    @pytest.mark.asyncio
    async def test_get_post_returns_post_error(self):
        """get_post returns post-not-found error without post in DB."""
        from app.domain.errors import PostNotFoundError

        try:
            result = await handle_tool_call("get_post", {"id_or_slug": "nonexistent"})
            assert isinstance(result, dict)
            assert "error" in result
        except PostNotFoundError:
            # Exception is raised, which is also acceptable behavior
            pass


class TestMCPToolSchemaValidation:
    """Validate tool schemas match expectations from ticket."""

    @pytest.fixture
    def tools(self):
        return get_all_tools()

    def test_dry_run_available_on_destructive_tools(self, tools):
        """Dry-run available on create_post, update_post."""
        tool_map = {t.name: t for t in tools}

        # These should have dry_run parameter
        for name in ["create_post", "update_post"]:
            tool = tool_map[name]
            props = tool.input_schema["properties"]
            assert "dry_run" in props, f"{name} missing dry_run parameter"
            assert props["dry_run"]["type"] == "boolean"

    def test_revert_post_no_dry_run(self, tools):
        """revert_post does NOT have dry_run (per ticket: default off, prominently described)."""
        tool_map = {t.name: t for t in tools}
        revert_post = tool_map["revert_post"]
        props = revert_post.input_schema["properties"]
        assert "dry_run" not in props, "revert_post should not have dry_run"
        # Description should prominently warn
        desc = revert_post.description.lower()
        assert "destructive" in desc or "irreversible" in desc

    def test_check_status_first_tool_promoted(self, tools):
        """check_status description encourages calling first."""
        check_status = next(t for t in tools if t.name == "check_status")
        desc = check_status.description.lower()
        assert "first" in desc or "start" in desc or "before" in desc

    def test_tool_descriptions_mention_side_effects(self, tools):
        """Tool descriptions mention side effects."""
        tool_map = {t.name: t for t in tools}

        # create_post should mention draft, not publish
        create_desc = tool_map["create_post"].description.lower()
        assert "draft" in create_desc
        assert "publish" in create_desc  # mentions it doesn't publish

        # publish_post should mention idempotent
        publish_desc = tool_map["publish_post"].description.lower()
        assert "idempotent" in publish_desc

    def test_validate_post_self_correction_loop(self, tools):
        """validate_post described as self-correction loop."""
        validate_post = next(t for t in tools if t.name == "validate_post")
        desc = validate_post.description.lower()
        assert "self-correction" in desc or "self correction" in desc or "dry-run" in desc
