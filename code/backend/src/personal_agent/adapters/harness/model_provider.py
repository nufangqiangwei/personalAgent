"""Supplier-side Messages client.

Only this module holds the provider endpoint and credential. The runtime and the
CLI never see the supplier key; they receive a loopback-only gateway token. A
failure is reported as an unknown outcome without echoing provider text.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from personal_agent.adapters.harness.model_gateway import ProviderOutcome
from personal_agent.domain.common import AppError, ErrorCategory, stable_hash
from personal_agent.ports.contracts import ModelRequest, Usage

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
API_KEY_ENV = "PERSONAL_AGENT_MODEL_API_KEY"
BASE_URL_ENV = "PERSONAL_AGENT_MODEL_BASE_URL"


def _unknown(code: str, message: str, *, status: int) -> ProviderOutcome:
    return ProviderOutcome(
        status="unknown",
        error=AppError(
            code=code, message=message,
            category=ErrorCategory.UNKNOWN_OUTCOME, retryable=False, status=status,
        ),
    )


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def usage_from_payload(payload: dict[str, Any]) -> Usage:
    """Total input tokens include both cache buckets, as the gateway accounts them."""
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return Usage(model_requests=1)
    inputs = (
        _count(raw.get("input_tokens"))
        + _count(raw.get("cache_read_input_tokens"))
        + _count(raw.get("cache_creation_input_tokens"))
    )
    return Usage(
        input_tokens=inputs, output_tokens=_count(raw.get("output_tokens")), model_requests=1
    )


class AnthropicMessagesProvider:
    """One Messages request per call; no SDK retry, no streaming to the supplier."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("the supplier credential must not be empty")
        self._url = base_url.rstrip("/") + "/v1/messages"
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout_seconds

    @classmethod
    def from_environment(cls) -> AnthropicMessagesProvider:
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            raise AppError(
                code="MODEL_CREDENTIAL_MISSING",
                message="live mode requires the supplier credential in the gateway process",
                category=ErrorCategory.DEPENDENCY,
                status=503,
            )
        return cls(os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL), api_key)

    async def complete(self, body: dict[str, Any], spec: ModelRequest) -> ProviderOutcome:
        # A streamed CLI request becomes one bounded, non-streamed supplier call;
        # the proxy rebuilds the event stream from the verified reply.
        request_body = {key: value for key, value in body.items() if key != "stream"}
        request_body["stream"] = False
        headers = {
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.post(self._url, json=request_body, headers=headers)
        except httpx.TimeoutException:
            return ProviderOutcome(
                status="unknown",
                error=AppError(
                    code="MODEL_TIMEOUT",
                    message="the supplier did not answer within the gateway timeout",
                    category=ErrorCategory.TIMEOUT,
                    retryable=False,
                    status=504,
                ),
            )
        except httpx.HTTPError:
            return _unknown(
                "MODEL_TRANSPORT_ERROR",
                "the gateway could not reach the supplier",
                status=502,
            )
        if response.status_code >= 400:
            # Status only: no provider body, headers or credential may leak.
            return _unknown(
                "MODEL_PROVIDER_REJECTED",
                f"the supplier rejected the request with status {response.status_code}",
                status=502,
            )
        try:
            payload = response.json()
        except ValueError:
            return _unknown(
                "MODEL_PROVIDER_INVALID", "the supplier reply was not valid JSON", status=502
            )
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return _unknown(
                "MODEL_PROVIDER_INVALID",
                "the supplier reply was not a Messages payload",
                status=502,
            )
        usage = usage_from_payload(payload)
        evidence = "provider:" + stable_hash(
            {
                "id": payload.get("id"),
                "model": payload.get("model"),
                "stop_reason": payload.get("stop_reason"),
                "usage": usage.as_dict(),
                "content": payload.get("content"),
                "request": spec.request_id,
            }
        )
        return ProviderOutcome(
            status="completed",
            payload=payload,
            usage=usage,
            completion_evidence_ref=evidence,
        )
