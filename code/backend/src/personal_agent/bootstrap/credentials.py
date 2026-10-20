"""Credential health without ever reading a secret back to the caller.

Environment variables are checked, never written. The OS credential store is only
offered when a store is actually available; otherwise the API reports
``CREDENTIAL_UNAVAILABLE`` instead of pretending to save something. Responses contain
an irreversible fingerprint prefix, never the value.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from personal_agent.domain.common import AppError, iso_z
from personal_agent.ports.contracts import Clock

PROVIDER_ENV: dict[str, str] = {
    "search": "PERSONAL_AGENT_BRAVE_API_KEY",
    "model": "PERSONAL_AGENT_MODEL_API_KEY",
}


def _fingerprint(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


class CredentialService:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def _os_store_available(self) -> bool:
        try:
            import keyring  # type: ignore[import-not-found]  # optional dependency
        except Exception:
            return False
        return bool(getattr(keyring, "get_keyring", lambda: None)())

    def secret(self, provider: str) -> str | None:
        env_name = PROVIDER_ENV.get(provider)
        if env_name is None:
            return None
        return os.environ.get(env_name) or None

    def health(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for provider, env_name in PROVIDER_ENV.items():
            secret = os.environ.get(env_name)
            items.append(
                {
                    "provider": provider,
                    "configured": bool(secret),
                    "source": "ENV" if secret else None,
                    "fingerprint": _fingerprint(secret) if secret else None,
                    "last_checked_at": iso_z(self._clock.now()),
                    "error_code": None if secret else "CREDENTIAL_UNAVAILABLE",
                    "env_var": env_name,
                }
            )
        return {"items": items, "checked_at": iso_z(self._clock.now())}

    def put(
        self,
        provider: str,
        *,
        source: str,
        secret: str | None,
        explicit_save: bool,
    ) -> dict[str, Any]:
        if provider not in PROVIDER_ENV:
            raise AppError(
                code="CREDENTIAL_INVALID", message="unknown provider", status=422
            )
        if not explicit_save:
            raise AppError(
                code="CREDENTIAL_INVALID",
                message="saving a credential needs an explicit confirmation",
                status=422,
            )
        if source == "ENV":
            if secret is not None:
                raise AppError(
                    code="CREDENTIAL_INVALID",
                    message="the environment source does not accept a secret value",
                    status=422,
                )
            configured = bool(os.environ.get(PROVIDER_ENV[provider]))
            return {
                "provider": provider,
                "configured": configured,
                "source": "ENV",
                "fingerprint": _fingerprint(os.environ[PROVIDER_ENV[provider]])
                if configured
                else None,
                "last_checked_at": iso_z(self._clock.now()),
                "error_code": None if configured else "CREDENTIAL_UNAVAILABLE",
            }
        if source != "OS_CREDENTIAL_STORE":
            raise AppError(
                code="CREDENTIAL_INVALID", message="unknown credential source", status=422
            )
        if not secret:
            raise AppError(
                code="CREDENTIAL_INVALID", message="a secret value is required", status=422
            )
        if not self._os_store_available():
            raise AppError(
                code="CREDENTIAL_UNAVAILABLE",
                message="the system credential store is not available",
                status=503,
            )
        import keyring  # type: ignore[import-not-found]

        keyring.set_password("Personal-Agent", provider, secret)
        return {
            "provider": provider,
            "configured": True,
            "source": "OS_CREDENTIAL_STORE",
            "fingerprint": _fingerprint(secret),
            "last_checked_at": iso_z(self._clock.now()),
            "error_code": None,
        }

    def remove(self, provider: str, *, source: str, explicit_remove: bool) -> dict[str, Any]:
        if provider not in PROVIDER_ENV:
            raise AppError(
                code="CREDENTIAL_INVALID", message="unknown provider", status=422
            )
        if source != "OS_CREDENTIAL_STORE":
            raise AppError(
                code="CREDENTIAL_INVALID",
                message="only system credential store entries can be removed",
                status=422,
            )
        if not explicit_remove:
            raise AppError(
                code="CREDENTIAL_INVALID",
                message="removing a credential needs an explicit confirmation",
                status=422,
            )
        if not self._os_store_available():
            raise AppError(
                code="CREDENTIAL_UNAVAILABLE",
                message="the system credential store is not available",
                status=503,
            )
        import keyring  # type: ignore[import-not-found]

        keyring.delete_password("Personal-Agent", provider)
        return {"provider": provider, "configured": False, "source": source}


__all__ = ["PROVIDER_ENV", "CredentialService"]
