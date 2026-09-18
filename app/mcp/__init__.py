"""MCP (Model Context Protocol) server for AgentCMS (#22).

Provides typed tool calls for AI agents (Claude, ChatGPT, Cursor) to interact
with AgentCMS. Two transports:
- stdio (for npx @agentcms/mcp / pipx install agentcms-mcp)
- Streamable-HTTP at /mcp on the main service (for remote clients with capability tokens)
"""

from __future__ import annotations

from .resources import get_all_resources, handle_resource_read
from .server import create_mcp_server, run_stdio_server
from .tools import get_all_tools, handle_tool_call

__all__ = [
    "create_mcp_server",
    "get_all_resources",
    "get_all_tools",
    "handle_resource_read",
    "handle_tool_call",
    "run_stdio_server",
]
