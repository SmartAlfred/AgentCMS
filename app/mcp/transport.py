"""Streamable-HTTP transport for the AgentCMS MCP server (#22).

The transport itself is the official one from the ``mcp`` SDK
(:class:`mcp.server.streamable_http_manager.StreamableHTTPSessionManager`): we
deliberately do **not** hand-roll JSON-RPC framing, session headers or SSE, so
the endpoint stays compatible with real MCP clients (Claude Desktop, Cursor,
the ``mcp`` CLI) as the protocol evolves.

The app registers the returned ASGI callable as an exact route at ``/mcp``
(``app.mount`` would only match the trailing-slash form). The session manager's
task group is entered from the parent app's lifespan — see ``app.main.lifespan``.

The SDK forbids calling ``run()`` twice on one session manager, and an app object
outlives any single lifespan (a test suite or a reloading server enters the
lifespan many times), so a fresh manager is built for every lifespan and the
route dispatches to whichever one is currently running.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from mcp.server import Server


class _SessionManagerASGIApp:
    """ASGI shim forwarding to the transport's *currently running* manager.

    Registered as a plain ASGI route (Starlette treats a callable *object* as an
    ASGI app and a callable *function* as a request handler), which is what the
    Streamable-HTTP transport needs.
    """

    def __init__(self, transport: MCPHttpTransport) -> None:
        self._transport = transport

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        manager = self._transport.session_manager
        if manager is None:
            raise RuntimeError("MCP session manager is not running: the application lifespan was not entered")
        await manager.handle_request(scope, receive, send)


class MCPHttpTransport:
    """Streamable-HTTP transport bound to a low-level MCP :class:`Server`."""

    def __init__(self, server: Server[Any]) -> None:
        self.server = server
        # Set while the owning app's lifespan is running; created fresh each time
        # because StreamableHTTPSessionManager.run() is once-per-instance.
        self.session_manager: StreamableHTTPSessionManager | None = None
        self.app = _SessionManagerASGIApp(self)

    def _create_session_manager(self) -> StreamableHTTPSessionManager:
        return StreamableHTTPSessionManager(
            app=self.server,
            json_response=False,
            stateless=True,
            # The MCP endpoint is an internet-facing, capability-token
            # authenticated route served behind the app's own middleware, not a
            # localhost dev server, so DNS-rebinding protection (which pins the
            # Host header to localhost) would break real deployments.
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Run a session manager's task group for the life of the app."""
        self.session_manager = self._create_session_manager()
        try:
            async with self.session_manager.run():
                yield
        finally:
            self.session_manager = None


def create_streamable_http_transport(server: Server[Any]) -> MCPHttpTransport:
    """Create the Streamable-HTTP transport (mounted at ``/mcp``) for ``server``."""
    return MCPHttpTransport(server)
