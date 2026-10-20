"""Managed stdio MCP server for the restricted Python tools.

Official MCP framing: newline-delimited JSON-RPC 2.0 over stdin/stdout. The
pinned CLI starts this process from the restricted patch and supplies the loopback
gateway endpoint and per-attempt token through the environment. Model-supplied
arguments can never select a run, epoch or scope token: those are bound by the
gateway from that token.

The server is a thin protocol adapter. It never reaches a provider, never reads a
supplier credential and never executes a tool locally; every call is forwarded to
the loopback gateway, which applies ToolService scope, epoch, argument and stop
checks again.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

PROTOCOL_VERSION = "2024-11-05"
GATEWAY_BASE_URL_ENV = "PERSONAL_AGENT_GATEWAY_BASE_URL"
GATEWAY_TOKEN_ENV = "PERSONAL_AGENT_GATEWAY_TOKEN"
MAX_REPLY_BYTES = 256 * 1024

EXPOSED_TOOLS = [
    {
        "name": "search_web",
        "description": "Search public web sources within the allowed domains.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "http_fetch",
        "description": "Fetch a public page with validated redirects.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_read",
        "description": "Render a public page in an isolated browser context.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "content_extract",
        "description": "Pure text extraction from provided HTML.",
        "inputSchema": {
            "type": "object",
            "properties": {"html": {"type": "string"}, "schema": {"type": "object"}},
            "required": ["html"],
            "additionalProperties": False,
        },
    },
]
# MCP names must satisfy the DeepSeek function-name contract, so the dots of the
# business tool names become underscores. The gateway only ever sees the
# canonical name and its per-tool scope grant.
CANONICAL_TOOLS = {
    "search_web": "search.web",
    "http_fetch": "http.fetch",
    "browser_read": "browser.read",
    "content_extract": "content.extract",
}
TOOL_NAMES = frozenset(tool["name"] for tool in EXPOSED_TOOLS)


@dataclass(frozen=True, slots=True)
class McpToolReply:
    content: list[dict[str, Any]]
    is_error: bool = False
    source_refs: list[str] = field(default_factory=list)


class McpDispatch(Protocol):
    async def __call__(
        self, name: str, arguments: Mapping[str, Any]
    ) -> McpToolReply: ...


class HttpToolDispatch:
    """Forward one authorized call to the loopback gateway tool boundary."""

    def __init__(
        self,
        base_url: str,
        gateway_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not gateway_token:
            raise ValueError("a gateway token is required")
        self._url = base_url.rstrip("/") + "/tools/call"
        self._token = gateway_token
        self._transport = transport
        self._timeout = timeout_seconds

    async def __call__(
        self, name: str, arguments: Mapping[str, Any]
    ) -> McpToolReply:
        body = {"name": name, "arguments": dict(arguments)}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout, trust_env=False
            ) as client:
                response = await client.post(
                    self._url, json=body, headers={"x-api-key": self._token}
                )
        except httpx.HTTPError:
            return _refusal("the local tool gateway is unreachable")
        if response.status_code >= 400:
            # Never echo the body: it can carry arguments or a target URL.
            return _refusal(f"the local tool gateway refused the call ({response.status_code})")
        try:
            payload = response.json()
        except ValueError:
            return _refusal("the local tool gateway returned an invalid reply")
        if not isinstance(payload, dict):
            return _refusal("the local tool gateway returned an invalid reply")
        raw_content = payload.get("content")
        content = [
            item for item in raw_content if isinstance(item, dict)
        ] if isinstance(raw_content, list) else []
        refs = payload.get("source_refs")
        return McpToolReply(
            content=content,
            is_error=payload.get("isError") is True,
            source_refs=[str(item) for item in refs] if isinstance(refs, list) else [],
        )


def _refusal(message: str) -> McpToolReply:
    return McpToolReply(
        content=[{"type": "text", "text": json.dumps({"status": "refused", "message": message})}],
        is_error=True,
    )


class McpStdioServer:
    def __init__(
        self,
        dispatch: McpDispatch,
        *,
        write: Any = None,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        self._dispatch = dispatch
        self._write = write or (lambda line: print(line, flush=True))
        self._protocol_version = protocol_version

    async def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        if "id" not in message:
            # notifications/* (initialized, cancelled, ...) never get a response.
            return None
        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if not isinstance(params, Mapping):
            return _error(request_id, -32602, "params must be an object")
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if isinstance(requested, str) and requested else (
                self._protocol_version
            )
            return _result(request_id, {
                "protocolVersion": version,
                "serverInfo": {"name": "personal-agent-tools", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            })
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": EXPOSED_TOOLS})
        if method == "shutdown":
            return _result(request_id, {})
        if method == "tools/call":
            return await self._call(request_id, params)
        return _error(request_id, -32601, "method not found")

    async def _call(
        self, request_id: Any, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
            return _error(request_id, -32602, "name and object arguments are required")
        if name not in TOOL_NAMES:
            return _error(request_id, -32602, "the tool is not exposed by this server")
        reply = await self._dispatch(CANONICAL_TOOLS[name], dict(arguments))
        text = json.dumps(reply.content, ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
            reply = _refusal("the tool reply exceeded the local bound")
        return _result(request_id, {
            "content": reply.content,
            "isError": reply.is_error,
        })

    def serve(self, stream: Any = None) -> int:
        source = stream or sys.stdin
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._write(json.dumps(_error(None, -32700, "parse error")))
                continue
            if not isinstance(message, dict):
                self._write(json.dumps(_error(None, -32600, "invalid request")))
                continue
            try:
                response = asyncio.run(self.handle(message))
            except Exception:  # never leak an internal traceback to the client
                response = _error(message.get("id"), -32603, "internal error")
            if response is not None:
                self._write(json.dumps(response, ensure_ascii=False))
        return 0


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def main() -> int:  # pragma: no cover - process entry
    base_url = os.environ.get(GATEWAY_BASE_URL_ENV, "")
    token = os.environ.get(GATEWAY_TOKEN_ENV, "")
    if not base_url or not token:
        # No secret is printed; the patch always supplies both through env.
        print("the gateway endpoint and token are required", file=sys.stderr)
        return 2
    server = McpStdioServer(HttpToolDispatch(base_url, token))
    return server.serve()


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
