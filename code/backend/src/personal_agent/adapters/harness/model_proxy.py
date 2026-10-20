"""Loopback protocol proxy: the runtime's only model egress.

The pinned CLI is configured with this proxy as its Messages ``baseURL`` and with
a per-attempt gateway token as its API key. The proxy forwards one Messages
request to the supplier only after the persistent ModelGateway slot is occupied
for the bound owner/attempt/epoch+reservation. It never implements an Agent Loop
and never accepts an arbitrary proxy target.

The response is rebuilt as an Anthropic event stream from the verified reply, so
the supplier leg stays a single bounded, non-streamed call.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from personal_agent.adapters.harness.model_gateway import ModelGateway, ProviderOutcome
from personal_agent.domain.common import AppError, ErrorCategory, new_id
from personal_agent.ports.contracts import (
    Clock,
    ModelMessage,
    ModelRequest,
    Owner,
    ToolPort,
    ToolRequest,
)

LOOPBACK_HOST = "127.0.0.1"
MESSAGES_PATH = "/anthropic/v1/messages"
TOOLS_PATH = "/tools/call"
STREAM_LIMIT = 4 * 1024 * 1024
TOOL_CONTENT_LIMIT = 256 * 1024


class ProviderClient(Protocol):
    async def complete(self, body: dict[str, Any], spec: ModelRequest) -> ProviderOutcome: ...


@dataclass(frozen=True, slots=True)
class ProxyBinding:
    """One attempt's authority: owner, epoch, model and its budget reservation."""

    token: str
    owner: Owner
    epoch: int
    model_id: str
    reservation_id: str
    reserved_tokens: int
    output_limit: int
    timeout_seconds: float
    # Scope tokens issued by the app's ToolScopeRegistry, one per allowed tool.
    # They never leave the gateway process; the MCP client only holds the
    # attempt token and cannot name a tool outside this mapping.
    tool_scopes: Mapping[str, str] = field(default_factory=dict)


class ProxyTokenRegistry:
    """In-memory, per-attempt tokens. A token is worthless after revocation."""

    def __init__(self) -> None:
        self._bindings: dict[str, ProxyBinding] = {}

    def issue(
        self,
        *,
        owner: Owner,
        epoch: int,
        model_id: str,
        reservation_id: str,
        reserved_tokens: int,
        output_limit: int,
        timeout_seconds: float,
        tool_scopes: Mapping[str, str] | None = None,
    ) -> ProxyBinding:
        if epoch < 1:
            raise ValueError("a gateway token needs a positive epoch")
        if output_limit < 1 or reserved_tokens < output_limit or timeout_seconds <= 0:
            raise ValueError("a gateway token needs a bounded output budget")
        binding = ProxyBinding(
            token=secrets.token_urlsafe(32),
            owner=owner,
            epoch=epoch,
            model_id=model_id,
            reservation_id=reservation_id,
            reserved_tokens=reserved_tokens,
            output_limit=output_limit,
            timeout_seconds=timeout_seconds,
            tool_scopes=dict(tool_scopes or {}),
        )
        self._bindings[binding.token] = binding
        return binding

    def resolve(self, token: str | None) -> ProxyBinding | None:
        if not token:
            return None
        return self._bindings.get(token)

    def revoke(self, token: str) -> None:
        self._bindings.pop(token, None)

    def revoke_owner(self, owner: Owner) -> None:
        for token, binding in list(self._bindings.items()):
            if binding.owner == owner:
                self._bindings.pop(token, None)

    def clear(self) -> None:
        self._bindings.clear()


class ModelProxy:
    def __init__(
        self,
        gateway: ModelGateway,
        tokens: ProxyTokenRegistry,
        provider: ProviderClient,
        clock: Clock,
        *,
        tools: ToolPort | None = None,
        host: str = LOOPBACK_HOST,
    ) -> None:
        if host != LOOPBACK_HOST:
            raise ValueError("the model proxy is loopback-only")
        self._gateway = gateway
        self._tokens = tokens
        self._provider = provider
        self._clock = clock
        self._tools = tools
        self._host = host
        self._port = 0
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def base_url(self) -> str:
        if self._port == 0:
            raise RuntimeError("the model proxy has not started")
        return f"http://{self._host}:{self._port}"

    def url(self, path: str) -> str:
        return self.base_url + path

    async def start(self) -> str:
        if self._server is not None:
            return self.base_url
        app = Starlette(routes=[
            Route(MESSAGES_PATH, self._messages, methods=["POST"]),
            Route(TOOLS_PATH, self._call_tool, methods=["POST"]),
        ])
        config = uvicorn.Config(
            app,
            host=self._host,
            port=0,
            log_config=None,
            access_log=False,
            lifespan="off",
            # The proxy is loopback-only and already bound to this host.
            proxy_headers=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(1000):
            if self._server.started:
                break
            await asyncio.sleep(0.01)
        if not self._server.started:
            raise RuntimeError("the model proxy did not start")
        sockets = self._server.servers[0].sockets
        self._port = int(sockets[0].getsockname()[1])
        return self.base_url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self._server = None
        self._task = None
        self._port = 0

    # ------------------------------------------------------------------
    async def _messages(self, request: Request) -> Response:
        binding = self._tokens.resolve(_request_token(request))
        if binding is None:
            return _error(401, "authentication_error",
                          "a valid local gateway token is required")
        body = await _json_body(request)
        if body is None:
            return _error(400, "invalid_request_error", "the request body must be JSON")
        if body.get("model") != binding.model_id:
            return _error(403, "permission_error",
                          "the model is not authorized for this attempt")
        messages = _messages(body.get("messages"))
        if messages is None:
            return _error(422, "invalid_request_error", "the request is not a Messages payload")
        deadline = self._clock.now() + timedelta(seconds=binding.timeout_seconds)
        spec = ModelRequest(
            request_id=new_id(),
            owner=binding.owner,
            epoch=binding.epoch,
            model_id=binding.model_id,
            messages=messages,
            output_limit=binding.output_limit,
            deadline=deadline,
            reservation_id=binding.reservation_id,
        )
        try:
            outcome = await self._gateway.exchange(
                spec,
                reserved_tokens=binding.reserved_tokens,
                timeout_seconds=binding.timeout_seconds,
                send=lambda bound: self._provider.complete(body, bound),
            )
        except AppError as error:
            return _error(
                error.status if 400 <= error.status < 600 else 502,
                "api_error",
                "the model gateway refused the request",
            )
        if outcome.status != "completed" or outcome.payload is None:
            return _error(_error_status(outcome.error), "api_error",
                          "the model request did not complete verifiably")
        if body.get("stream") is True:
            payload = _anthropic_events(outcome.payload)
            return Response(
                payload,
                media_type="text/event-stream",
                headers={"cache-control": "no-store"},
            )
        return JSONResponse(outcome.payload)

    # ------------------------------------------------------------------
    async def _call_tool(self, request: Request) -> Response:
        """Managed MCP tool call: identity and scope come from the attempt token.

        The model/MCP client only names a tool and arguments; it cannot supply a
        scope token, run identity, epoch or an operation id. Those come from the
        binding issued by the application before the attempt started.
        """
        binding = self._tokens.resolve(_request_token(request))
        if binding is None:
            return _error(401, "authentication_error",
                          "a valid local gateway token is required")
        body = await _json_body(request)
        if body is None:
            return _error(400, "invalid_request_error", "the request body must be JSON")
        name = body.get("name")
        arguments = body.get("arguments") or {}
        if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
            return _error(422, "invalid_request_error",
                          "a tool name and object arguments are required")
        scope_token = binding.tool_scopes.get(name)
        if scope_token is None:
            return _error(403, "permission_error",
                          "the tool is not authorized for this attempt")
        if self._tools is None:
            return _error(503, "api_error", "the managed tool service is not mounted")
        request_id = new_id()
        try:
            result = await self._tools.execute(ToolRequest(
                operation_id=f"{request_id}:tool",
                run_id=binding.owner.run_id or "",
                attempt_id=binding.owner.attempt_id or "",
                epoch=binding.epoch,
                tool_name=name,
                args=dict(arguments),
                scope_token=scope_token,
                deadline=self._clock.now() + timedelta(seconds=binding.timeout_seconds),
            ))
        except AppError as error:
            # A refusal is a tool result, not an HTTP bypass; keep the code and
            # drop message details that could carry arguments or targets.
            return JSONResponse({
                "content": [{
                    "type": "text",
                    "text": json.dumps(
                        {"status": "refused", "code": error.code}, ensure_ascii=False
                    ),
                }],
                "isError": True,
                "source_refs": [],
            })
        content = result.content if result.content is not None else {"status": result.status}
        text = json.dumps(content, ensure_ascii=False, sort_keys=True)
        if len(text.encode("utf-8")) > TOOL_CONTENT_LIMIT:
            text = json.dumps(
                {"status": "truncated", "note": "the tool payload exceeded the local bound"},
                ensure_ascii=False,
            )
        return JSONResponse({
            "content": [{"type": "text", "text": text}],
            "isError": result.status != "ok",
            "source_refs": list(result.source_refs),
        })


def _request_token(request: Request) -> str | None:
    header = request.headers.get("x-api-key")
    if header:
        return header.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def _json_body(request: Request) -> dict[str, Any] | None:
    raw = await request.body()
    if not raw or len(raw) > STREAM_LIMIT:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _messages(value: Any) -> tuple[ModelMessage, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    messages: list[ModelMessage] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        role = item.get("role")
        if not isinstance(role, str) or not role:
            return None
        content = item.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = json.dumps(content, ensure_ascii=False, sort_keys=True)
        else:
            return None
        messages.append(ModelMessage(role=role, content=text))
    return tuple(messages)


def _error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"type": "error", "error": {"type": kind, "message": message}},
        status_code=status,
    )


def _error_status(error: AppError | None) -> int:
    if error is None:
        return 502
    if error.category == ErrorCategory.TIMEOUT:
        return 504
    if error.category == ErrorCategory.PERMISSION:
        return 403
    return 502


def _anthropic_events(message: Mapping[str, Any]) -> bytes:
    """Rebuild one complete Messages reply as the provider's event stream."""
    chunks: list[str] = []

    def event(name: str, data: dict[str, Any]) -> None:
        chunks.append(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

    raw_usage = message.get("usage")
    usage = raw_usage if isinstance(raw_usage, Mapping) else {}
    start_usage: dict[str, int] = {
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": 0,
    }
    for name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if name in usage:
            start_usage[name] = _int(usage.get(name))
    event("message_start", {
        "type": "message_start",
        "message": {
            "id": message.get("id") or "msg_local",
            "type": "message",
            "role": "assistant",
            "model": message.get("model"),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": start_usage,
        },
    })
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "text":
            event("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "text", "text": ""},
            })
            text = block.get("text")
            if isinstance(text, str) and text:
                event("content_block_delta", {
                    "type": "content_block_delta", "index": index,
                    "delta": {"type": "text_delta", "text": text},
                })
            event("content_block_stop", {"type": "content_block_stop", "index": index})
        elif kind == "tool_use":
            event("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {
                    "type": "tool_use", "id": block.get("id") or f"toolu_{index}",
                    "name": block.get("name") or "", "input": {},
                },
            })
            arguments = json.dumps(block.get("input") or {}, ensure_ascii=False)
            event("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": arguments},
            })
            event("content_block_stop", {"type": "content_block_stop", "index": index})
        # Reasoning/thinking blocks are never forwarded: the profile forbids
        # persisting or re-exposing internal reasoning.
    event("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": message.get("stop_reason") or "end_turn",
            "stop_sequence": message.get("stop_sequence"),
        },
        "usage": {"output_tokens": _int(usage.get("output_tokens"))},
    })
    event("message_stop", {"type": "message_stop"})
    return "".join(chunks).encode("utf-8")


def _int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0
