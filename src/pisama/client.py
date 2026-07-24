"""Pisama platform client: API key -> JWT -> ingest -> detections.

Closes the one path the offline SDK and the TypeScript SDK don't cover: a Python
caller with an API key sending a real trace to api.pisama.ai and getting
calibrated detections back as objects. The detection surface (`Detection`)
mirrors the offline `pisama.Issue` so `analyze()` and `Client.ingest()` feel the
same.

    from pisama import Client

    client = Client()                       # reads PISAMA_API_KEY
    result = client.ingest("trace.json")    # ingest + wait for detections
    for d in result.detections:
        print(d.type, d.severity, d.summary)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from pisama_core.traces.models import Trace

from pisama._http import AsyncPlatformSession, PisamaAuthError, PlatformSession
from pisama._otel import trace_to_resource_spans

__all__ = ["Client", "Detection", "PlatformResult", "PisamaAuthError"]

logger = logging.getLogger("pisama.client")

DEFAULT_BASE_URL = "https://api.pisama.ai"
_TERMINAL_STATUSES = ("complete", "partial", "failed")

TraceInput = Union[str, dict, Trace]


@dataclass
class Detection:
    """A single platform detection. Mirrors :class:`pisama.Issue` so the offline
    and platform surfaces line up."""

    type: str
    confidence: float  # 0.0-1.0
    summary: str
    severity: int  # 0-100
    evidence: list
    recommendation: Optional[str]
    id: Optional[str] = None
    validated: bool = False
    details: dict = field(default_factory=dict)

    @classmethod
    def _from_api(cls, d: dict) -> "Detection":
        details = d.get("details") or {}
        conf_int = d.get("confidence") or 0
        evidence = details.get("evidence")
        raw_severity = details.get("severity", conf_int) or 0
        _sev_map = {"low": 25, "medium": 50, "high": 75, "critical": 100}
        if isinstance(raw_severity, str):
            severity = _sev_map.get(raw_severity.lower(), 50)
        else:
            severity = int(raw_severity)
        return cls(
            type=d.get("detection_type", "unknown"),
            confidence=round((conf_int or 0) / 100.0, 4),
            summary=details.get("summary") or d.get("explanation") or d.get("detection_type", ""),
            severity=severity,
            evidence=evidence if isinstance(evidence, list) else [],
            recommendation=d.get("suggested_fix") or d.get("suggested_action"),
            id=str(d["id"]) if d.get("id") else None,
            validated=bool(d.get("validated", False)),
            details=details,
        )


@dataclass
class PlatformResult:
    """Result of ingesting a trace into the platform."""

    session_id: str
    detections: list[Detection]
    accepted: int
    submitted: int
    rejected: int = 0
    backend_trace_id: Optional[str] = None
    detection_status: str = "pending"

    @property
    def has_detections(self) -> bool:
        return len(self.detections) > 0

    @property
    def critical_detections(self) -> list[Detection]:
        return [d for d in self.detections if d.severity >= 60]


class Client:
    """Synchronous Pisama platform client.

    Args:
        api_key: defaults to ``PISAMA_API_KEY``.
        base_url: defaults to ``PISAMA_BASE_URL`` or ``https://api.pisama.ai``.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        timeout: float = 120.0,
        max_retries: int = 5,
        scope: str = "full",
    ):
        key = api_key or os.getenv("PISAMA_API_KEY")
        if not key:
            raise ValueError(
                "No Pisama API key. Pass api_key=... or set PISAMA_API_KEY. "
                "Generate one at https://pisama.ai/settings/api-keys"
            )
        self.base_url = (base_url or os.getenv("PISAMA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._session = PlatformSession(
            self.base_url, key, timeout=timeout, max_retries=max_retries, scope=scope
        )

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def tenant_id(self) -> str:
        self._session.ensure_auth()
        if not self._session.tenant_id:
            raise PisamaAuthError("Authenticated but no tenant_id in token claims.")
        return self._session.tenant_id

    # --- ingest ---

    def submit(self, trace: TraceInput) -> PlatformResult:
        """Ingest a trace without waiting for detections. Returns immediately
        with the accepted/rejected/submitted span counts."""
        payload, session_id = trace_to_resource_spans(trace)
        # Tenant-scoped ingest: the keyless /traces/ingest alias binds tenant_id
        # from a query param (via the quota dependency), so use the path form.
        resp = self._session.request(
            "POST", f"/api/v1/tenants/{self.tenant_id}/traces/ingest", json=payload
        )
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        accepted = int(body.get("accepted", 0))
        submitted = int(body.get("submitted", accepted))
        rejected = int(body.get("rejected", 0))
        if accepted < submitted:
            logger.warning(
                "Pisama ingest accepted %d of %d spans (%d rejected) for session %s",
                accepted,
                submitted,
                rejected,
                session_id,
            )
        return PlatformResult(
            session_id=session_id,
            detections=[],
            accepted=accepted,
            submitted=submitted,
            rejected=rejected,
        )

    def ingest(
        self,
        trace: TraceInput,
        *,
        wait: bool = True,
        poll_timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> PlatformResult:
        """Ingest a trace and (by default) wait for the async detection pass to
        finish, returning the detections. Set ``wait=False`` to return as soon
        as the spans are accepted."""
        result = self.submit(trace)
        if not wait:
            return result
        deadline = time.monotonic() + poll_timeout
        while True:
            trace_row = self._find_trace(result.session_id)
            if trace_row is not None:
                result.backend_trace_id = str(trace_row["id"])
                result.detection_status = trace_row.get("detection_status", "pending")
                if result.detection_status in _TERMINAL_STATUSES:
                    result.detections = self._fetch_detections(result.backend_trace_id)
                    return result
            if time.monotonic() >= deadline:
                # Timed out: return whatever exists rather than hanging. The
                # background pass may simply be slow; the caller can re-poll
                # via get_detections(session_id).
                if result.backend_trace_id:
                    result.detections = self._fetch_detections(result.backend_trace_id)
                return result
            time.sleep(poll_interval)

    # --- reads ---

    def get_detections(self, session_id: str) -> list[Detection]:
        """Fetch detections for a previously-submitted session id."""
        trace_row = self._find_trace(session_id)
        if trace_row is None:
            return []
        return self._fetch_detections(str(trace_row["id"]))

    def get_trace(self, session_id: str) -> Optional[dict]:
        """Fetch the backend trace row for a session id, or None."""
        return self._find_trace(session_id)

    def _find_trace(self, session_id: str) -> Optional[dict]:
        data = self._session.get_json(
            f"/api/v1/tenants/{self.tenant_id}/traces", params={"per_page": 100}
        )
        for t in data.get("traces", []):
            if t.get("session_id") == session_id:
                return t
        return None

    def _fetch_detections(self, backend_trace_id: str) -> list[Detection]:
        data = self._session.get_json(
            f"/api/v1/tenants/{self.tenant_id}/detections",
            params={"trace_id": backend_trace_id, "per_page": 100},
        )
        return [Detection._from_api(d) for d in data.get("items", [])]


class AsyncClient:
    """Asynchronous twin of :class:`Client`."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        timeout: float = 120.0,
        max_retries: int = 5,
        scope: str = "full",
        transport: Any = None,
    ):
        key = api_key or os.getenv("PISAMA_API_KEY")
        if not key:
            raise ValueError(
                "No Pisama API key. Pass api_key=... or set PISAMA_API_KEY. "
                "Generate one at https://pisama.ai/settings/api-keys"
            )
        self.base_url = (base_url or os.getenv("PISAMA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._session = AsyncPlatformSession(
            self.base_url,
            key,
            timeout=timeout,
            max_retries=max_retries,
            scope=scope,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._session.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _tenant_id(self) -> str:
        await self._session.ensure_auth()
        if not self._session.tenant_id:
            raise PisamaAuthError("Authenticated but no tenant_id in token claims.")
        return self._session.tenant_id

    async def submit(self, trace: TraceInput) -> PlatformResult:
        payload, session_id = trace_to_resource_spans(trace)
        tid = await self._tenant_id()
        resp = await self._session.request(
            "POST", f"/api/v1/tenants/{tid}/traces/ingest", json=payload
        )
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        accepted = int(body.get("accepted", 0))
        submitted = int(body.get("submitted", accepted))
        rejected = int(body.get("rejected", 0))
        if accepted < submitted:
            logger.warning(
                "Pisama ingest accepted %d of %d spans (%d rejected) for session %s",
                accepted,
                submitted,
                rejected,
                session_id,
            )
        return PlatformResult(
            session_id=session_id,
            detections=[],
            accepted=accepted,
            submitted=submitted,
            rejected=rejected,
        )

    async def ingest(
        self,
        trace: TraceInput,
        *,
        wait: bool = True,
        poll_timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> PlatformResult:
        result = await self.submit(trace)
        if not wait:
            return result
        deadline = time.monotonic() + poll_timeout
        while True:
            trace_row = await self._find_trace(result.session_id)
            if trace_row is not None:
                result.backend_trace_id = str(trace_row["id"])
                result.detection_status = trace_row.get("detection_status", "pending")
                if result.detection_status in _TERMINAL_STATUSES:
                    result.detections = await self._fetch_detections(result.backend_trace_id)
                    return result
            if time.monotonic() >= deadline:
                if result.backend_trace_id:
                    result.detections = await self._fetch_detections(result.backend_trace_id)
                return result
            await asyncio.sleep(poll_interval)

    async def get_detections(self, session_id: str) -> list[Detection]:
        trace_row = await self._find_trace(session_id)
        if trace_row is None:
            return []
        return await self._fetch_detections(str(trace_row["id"]))

    async def _find_trace(self, session_id: str) -> Optional[dict]:
        tid = await self._tenant_id()
        data = await self._session.get_json(
            f"/api/v1/tenants/{tid}/traces", params={"per_page": 100}
        )
        for t in data.get("traces", []):
            if t.get("session_id") == session_id:
                return t
        return None

    async def _fetch_detections(self, backend_trace_id: str) -> list[Detection]:
        tid = await self._tenant_id()
        data = await self._session.get_json(
            f"/api/v1/tenants/{tid}/detections",
            params={"trace_id": backend_trace_id, "per_page": 100},
        )
        return [Detection._from_api(d) for d in data.get("items", [])]
