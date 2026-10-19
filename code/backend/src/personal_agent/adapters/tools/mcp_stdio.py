"""Managed stdio MCP server for the restricted Python tools.

Only a loopback/stdio transport is supported. The server requires a handshake
token supplied by the gateway, exposes exactly the four registered tools, and
delegates every call to ``ToolService`` so scope token, epoch, stop flag, budget
and argument validation are applied again at the tool boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from personal_agent.adapters.tools.mcp_service import ToolService
from personal_agent.domain.common import AppError

PROTOCOL_VERSION = "2024-11-05"
EXPOSED_TOOLS = [
    {
        "name": "search.web",
        "description": "Search public web sources within the allowed domains.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "http.fetch",
        "description": "Fetch a public page with validated redirects.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "browser.read",
        "description": "Render a public page in an isolated browser context.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "content.extract",
        "description": "Pure text extraction from provided HTML.",
        "inputSchema": {
            "type": "object",
            "properties": {"html": {"type": "string"}, "schema": {"type": "object"}},
            "required": ["html"],
            "additionalProperties": False,
        },
    },
]


class McpStdioServer:
    def __init__(
        self,
        tool_service: ToolService,
        *,
        handshake_token: str,
        write: Callable[[str], None] | None = None,
    ) -> None:
        self._tools = tool_service
        self._handshake_token = handshake_token
        self._write = write or (lambda line: print(line, flush=True))
        self._initialized = False

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method", ""))
        request_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            token = str(params.get("handshake_token", ""))
            if not token or token != self._handshake_token:
                return _error(request_id, "HANDSHAKE_REJECTED", "invalid handshake token")
            self._initialized = True
            return _result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": {"name": "personal-agent-tools", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            )
        if not self._initialized:
            return _error(request_id, "NOT_INITIALIZED", "initialize is required first")
        if method == "tools/list":
            return _result(request_id, {"tools": EXPOSED_TOOLS})
        if method == "tools/call":
            return await self._call(request_id, params)
        if method == "shutdown":
            self._initialized = False
            return _result(request_id, {})
        return _error(request_id, "METHOD_NOT_FOUND", f"unsupported method {method!r}")

    async def _call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        scope_token = str(params.get("scope_token", ""))
        run_id = str(params.get("run_id", ""))
        attempt_id = str(params.get("attempt_id", ""))
        epoch = int(params.get("epoch", 0))
        if not name or not scope_token or not run_id:
            return _error(request_id, "INVALID_CALL", "name, scope_token and run_id are required")

        from personal_agent.ports.contracts import ToolRequest

        request = ToolRequest(
            operation_id=str(params.get("operation_id", "")),
            run_id=run_id,
            attempt_id=attempt_id,
            epoch=epoch,
            tool_name=name,
            args=arguments,
            scope_token=scope_token,
            deadline=datetime.now(UTC) + timedelta(seconds=60),
        )
        try:
            result = await self._tools.execute(request)
        except AppError as error:
            return _error(request_id, error.code, error.message)
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(result.result_ref or "")}],
                "isError": result.status != "ok",
                "source_refs": list(result.source_refs),
            },
        )

    def serve(self, stream: Any = None) -> int:
        source = stream or sys.stdin
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._write(json.dumps(_error(None, "PARSE_ERROR", "invalid JSON")))
                continue
            response = asyncio.run(self.handle(message))
            if response is not None:
                self._write(json.dumps(response, ensure_ascii=False))
        return 0


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def main() -> int:  # pragma: no cover - process entry
    token = os.environ.get("PERSONAL_AGENT_MCP_TOKEN", "")
    if not token:
        print("PERSONAL_AGENT_MCP_TOKEN is required", file=sys.stderr)
        return 2
    raise SystemExit(
        "Assemble this server with the managed tool handlers in the bootstrap runtime."
    )
