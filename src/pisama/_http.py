"""HTTP session helpers for the Pisama platform client.

Wraps the api.pisama.ai auth + request flow that today is scattered across
pisama-synth-agents (auth + retry), pisama-claude-code (ingest), and
pisama.replay.trace_fetcher (reads). The one piece none of those have is JWT
caching with a single re-exchange on 401, which lives here.

Two parallel classes (sync + async) so the public Client can offer both
``ingest()`` and ``aingest()`` without forcing an event loop on offline users.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Optional

import httpx

# 429 (rate limited) and 503 (backpressure) are transient and safe to retry.
# Mirrors pisama_synth_agents.base._RETRY_STATUSES.
_RETRY_STATUSES = (429, 503)
_TOKEN_PATH = "/api/v1/auth/token"


def backoff_seconds(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retry ``attempt`` (0-indexed).

    Honors a numeric ``Retry-After`` header when present, else exponential
    (1s, 2s, 4s, ...) capped at 16s.
    """
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return min(2.0**attempt, 16.0)


def tenant_id_from_jwt(token: str) -> Optional[str]:
    """Read the ``tenant_id`` claim from a JWT without verifying the signature.

    The signature is the server's to verify; the client only needs the tenant
    id to build the tenant-scoped read paths. Returns None if the token is not a
    well-formed JWT.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        tid = payload.get("tenant_id")
        return str(tid) if tid else None
    except Exception:
        return None


class PisamaAuthError(RuntimeError):
    """Raised when the API key is rejected (bad/expired/wrong environment)."""


def _auth_error_from(resp: httpx.Response) -> PisamaAuthError:
    detail = ""
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        detail = resp.text[:200]
    return PisamaAuthError(
        f"API key rejected ({resp.status_code}). {detail} "
        "Check PISAMA_API_KEY, or generate a new key at "
        "https://pisama.ai/settings/api-keys"
    )


class PlatformSession:
    """Synchronous authenticated session against the Pisama platform."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 120.0,
        max_retries: int = 5,
        scope: str = "full",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.scope = scope
        self._jwt: Optional[str] = None
        self.tenant_id: Optional[str] = None
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PlatformSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _authenticate(self) -> None:
        resp = self._client.post(_TOKEN_PATH, json={"api_key": self.api_key, "scope": self.scope})
        if resp.status_code in (401, 403):
            raise _auth_error_from(resp)
        resp.raise_for_status()
        self._jwt = resp.json()["access_token"]
        self.tenant_id = tenant_id_from_jwt(self._jwt)

    def ensure_auth(self) -> None:
        """Force the token exchange so ``tenant_id`` is populated before any
        tenant-scoped read path is built."""
        if not self._jwt:
            self._authenticate()

    def _headers(self) -> dict:
        if not self._jwt:
            self._authenticate()
        return {"Authorization": f"Bearer {self._jwt}", "Content-Type": "application/json"}

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        reauthed = False
        for attempt in range(self.max_retries + 1):
            resp = self._client.request(method, path, headers=self._headers(), **kwargs)
            if resp.status_code == 401 and not reauthed:
                # JWT expired mid-session — re-exchange the API key once.
                reauthed = True
                self._jwt = None
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                time.sleep(backoff_seconds(resp, attempt))
                continue
            return resp
        return resp

    def get_json(self, path: str, **kwargs: Any) -> Any:
        resp = self.request("GET", path, **kwargs)
        resp.raise_for_status()
        return resp.json()


class AsyncPlatformSession:
    """Asynchronous twin of :class:`PlatformSession`."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 120.0,
        max_retries: int = 5,
        scope: str = "full",
        transport: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.scope = scope
        self._jwt: Optional[str] = None
        self.tenant_id: Optional[str] = None
        # `transport` is a test seam: pass an httpx ASGITransport to drive a
        # real in-process app without a network listener. Unused in production.
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncPlatformSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _authenticate(self) -> None:
        resp = await self._client.post(
            _TOKEN_PATH, json={"api_key": self.api_key, "scope": self.scope}
        )
        if resp.status_code in (401, 403):
            raise _auth_error_from(resp)
        resp.raise_for_status()
        self._jwt = resp.json()["access_token"]
        self.tenant_id = tenant_id_from_jwt(self._jwt)

    async def ensure_auth(self) -> None:
        """Force the token exchange so ``tenant_id`` is populated before any
        tenant-scoped read path is built."""
        if not self._jwt:
            await self._authenticate()

    async def _headers(self) -> dict:
        if not self._jwt:
            await self._authenticate()
        return {"Authorization": f"Bearer {self._jwt}", "Content-Type": "application/json"}

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        reauthed = False
        for attempt in range(self.max_retries + 1):
            resp = await self._client.request(method, path, headers=await self._headers(), **kwargs)
            if resp.status_code == 401 and not reauthed:
                reauthed = True
                self._jwt = None
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                await asyncio.sleep(backoff_seconds(resp, attempt))
                continue
            return resp
        return resp

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        resp = await self.request("GET", path, **kwargs)
        resp.raise_for_status()
        return resp.json()
