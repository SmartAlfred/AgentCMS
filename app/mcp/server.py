"""MCP server creation, tool/resource registration and the stdio entrypoint.

The HTTP surface lives at ``/mcp`` in the main app: ``app.main.register_mcp_routes``
binds ``app.mcp.transport.create_streamable_http_transport`` there, and ``app.main.lifespan``
owns the session manager's task group.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    TextContent,
)

from app.config import get_settings
from app.mcp.resources import get_all_resources, handle_resource_read
from app.mcp.tools import get_all_tools, handle_tool_call
from mcp.server import Server

logger = logging.getLogger("app.mcp")


def create_mcp_server() -> Server:
    """Create and configure the MCP server with all tools and resources."""
    server = Server("agentcms")

    # Get all tools and resources
    tools = get_all_tools()
    resources = get_all_resources()

    # Register handlers for tools
    async def handle_list_tools(
        ctx: ServerRequestContext[Any], params: PaginatedRequestParams
    ) -> ListToolsResult:
        return ListToolsResult(tools=tools)

    async def handle_call_tool(
        ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        try:
            result = await handle_tool_call(params.name, params.arguments or {})
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result, default=str))],
                is_error=False,
            )
        except Exception as e:
            logger.exception("Tool %s failed: %s", params.name, e)
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps({"error": str(e)}))],
                is_error=True,
            )

    # Register handlers for resources
    async def handle_list_resources(
        ctx: ServerRequestContext[Any], params: PaginatedRequestParams
    ) -> ListResourcesResult:
        return ListResourcesResult(resources=resources)

    async def handle_read_resource(
        ctx: ServerRequestContext[Any], params: ReadResourceRequestParams
    ) -> ReadResourceResult:
        try:
            result = await handle_resource_read(str(params.uri))
            return ReadResourceResult(contents=list(result.contents))
        except Exception as e:
            logger.exception("Resource %s failed: %s", params.uri, e)
            raise

    # Register with the server
    server.add_request_handler("tools/list", PaginatedRequestParams, handle_list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, handle_call_tool)
    server.add_request_handler("resources/list", PaginatedRequestParams, handle_list_resources)
    server.add_request_handler("resources/read", ReadResourceRequestParams, handle_read_resource)

    return server


async def run_stdio_server() -> None:
    """Run the MCP server over stdio (for npx/pipx)."""
    from mcp.server.stdio import stdio_server

    mcp_server = create_mcp_server()

    async with stdio_server() as (read_stream, write_stream):
        await mcp_server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="agentcms",
                server_version=get_settings().app_version,
                capabilities=mcp_server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_stdio_server())
