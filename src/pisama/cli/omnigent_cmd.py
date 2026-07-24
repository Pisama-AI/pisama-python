"""pisama omnigent -- watch Omnigent sessions and analyze them with Pisama.

Tails the SSE event stream of an Omnigent session (plus any sub-agent child
sessions it spawns), assembles the events into an ATIF v1.7 trajectory via
``pisama_core.ingestion.omnigent_events.events_to_atif``, and posts it to
Pisama's public ``POST /api/v1/atif/analyze`` endpoint after every completed
turn. Re-posting is idempotent server-side (trace id derives from the
session id), so each post supersedes the last.

See ``docs/integrations/omnigent.md`` in the monorepo for the adapter design.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional

import click
import httpx
from pisama_core.ingestion.omnigent_events import events_to_atif
from rich.console import Console

from pisama._http import AsyncPlatformSession

console = Console(stderr=True)

ANALYZE_PATH = "/api/v1/atif/analyze"
# Long read timeout: tool calls legitimately hold the SSE stream open.
SSE_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0)


@click.group("omnigent")
def omnigent_group() -> None:
    """Watch Omnigent meta-harness sessions with Pisama detection."""


async def _sse_events(
    http: httpx.AsyncClient, base: str, session_id: str
) -> AsyncIterator[Dict[str, Any]]:
    """Yield parsed events from an Omnigent session's SSE stream."""
    async with http.stream("GET", f"{base}/v1/sessions/{session_id}/stream") as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload:
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue


class SessionWatcher:
    """Tails one Omnigent session (and its sub-agent children), analyzing
    the assembled trajectory with Pisama after every completed turn."""

    def __init__(
        self,
        *,
        server: str,
        api_url: str,
        session_id: str,
        api_key: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.session_id = session_id
        self.api_key = api_key
        self._platform: Optional[AsyncPlatformSession] = None
        self.project_id = project_id
        self.agent_name = "omnigent"
        self.harness: Optional[str] = None
        self.parent_events: List[Dict[str, Any]] = []
        self.child_events: Dict[str, List[Dict[str, Any]]] = {}
        self._child_tasks: Dict[str, asyncio.Task] = {}
        self._analyze_lock = asyncio.Lock()
        self._turns_completed = 0
        self._stopped = asyncio.Event()

    async def run(self, *, once: bool = False) -> None:
        async with httpx.AsyncClient(timeout=SSE_TIMEOUT) as http:
            await self._seed(http)
            console.print(
                f"[bold]watching[/bold] {self.session_id} "
                f"(agent: {self.agent_name}, harness: {self.harness or '?'}) "
                f"on {self.server}"
            )
            try:
                async for event in _sse_events(http, self.server, self.session_id):
                    await self._handle_parent_event(http, event)
                    if once and self._is_done(event):
                        break
            finally:
                self._stopped.set()
                for task in self._child_tasks.values():
                    task.cancel()
                await asyncio.gather(*self._child_tasks.values(), return_exceptions=True)
                if self._turns_completed:
                    await self._analyze(http, final=True)
                if self._platform is not None:
                    await self._platform.aclose()

    async def _seed(self, http: httpx.AsyncClient) -> None:
        """Seed agent/harness identity from the session snapshot."""
        try:
            resp = await http.get(f"{self.server}/v1/sessions/{self.session_id}", timeout=15)
            resp.raise_for_status()
            snap = resp.json()
            self.agent_name = snap.get("agent_name") or self.agent_name
            self.harness = snap.get("harness")
        except httpx.HTTPError as exc:
            raise click.ClickException(
                f"cannot read session {self.session_id} on {self.server}: {exc}"
            ) from exc

    def _is_done(self, event: Dict[str, Any]) -> bool:
        return (
            event.get("type") == "session.status"
            and event.get("status") == "idle"
            and self._turns_completed > 0
        )

    async def _handle_parent_event(self, http: httpx.AsyncClient, event: Dict[str, Any]) -> None:
        self.parent_events.append(event)
        etype = event.get("type")

        if etype == "session.created":
            child = event.get("child_session_id")
            if child and child not in self._child_tasks:
                console.print(f"  [dim]sub-agent session {child}[/dim]")
                self.child_events[child] = []
                self._child_tasks[child] = asyncio.create_task(self._tail_child(child))

        elif etype in ("turn.completed", "response.completed"):
            if etype == "turn.completed" or not any(
                e.get("type") == "turn.completed" for e in self.parent_events
            ):
                self._turns_completed += 1
                await self._analyze(http)

    async def _tail_child(self, child_id: str) -> None:
        """Accumulate a child session's stream until the watcher stops."""
        try:
            async with httpx.AsyncClient(timeout=SSE_TIMEOUT) as http:
                async for event in _sse_events(http, self.server, child_id):
                    self.child_events[child_id].append(event)
                    if self._stopped.is_set():
                        return
        except (httpx.HTTPError, asyncio.CancelledError):
            return

    async def _analyze(self, http: httpx.AsyncClient, *, final: bool = False) -> None:
        """Map accumulated events to ATIF and post to Pisama. Idempotent."""
        async with self._analyze_lock:
            trajectory = events_to_atif(
                list(self.parent_events),
                child_streams={k: list(v) for k, v in self.child_events.items()},
                agent_name=self.agent_name,
                agent_version=f"omnigent:{self.harness or 'unknown'}",
                session_id=self.session_id,
            )
            if trajectory is None:
                return
            body: Dict[str, Any] = {"trajectory": trajectory}
            if self.project_id:
                body["project_id"] = self.project_id
            try:
                if self.api_key:
                    # /atif/analyze is mounted with tenant auth (it runs the
                    # LLM-judge orchestrator): API key -> JWT via /auth/token.
                    if self._platform is None:
                        self._platform = AsyncPlatformSession(self.api_url, self.api_key)
                    resp = await self._platform.request("POST", ANALYZE_PATH, json=body)
                else:
                    resp = await http.post(f"{self.api_url}{ANALYZE_PATH}", json=body, timeout=120)
            except httpx.HTTPError as exc:
                console.print(f"[red]analyze failed:[/red] {exc}")
                return
            if resp.status_code != 200:
                hint = (
                    " (set PISAMA_API_KEY or --api-key)"
                    if resp.status_code in (401, 403) and not self.api_key
                    else ""
                )
                console.print(
                    f"[red]analyze failed:[/red] HTTP {resp.status_code} {resp.text[:300]}{hint}"
                )
                return
            self._print_diagnosis(resp.json(), final=final)

    def _print_diagnosis(self, payload: Dict[str, Any], *, final: bool) -> None:
        diagnosis = payload.get("diagnosis") or {}
        trace = payload.get("trace") or {}
        tag = "final" if final else f"turn {self._turns_completed}"
        detections = [d for d in (diagnosis.get("all_detections") or []) if d.get("detected")]
        console.print(
            f"[bold]analyzed[/bold] ({tag}): "
            f"{trace.get('span_count', '?')} spans, "
            f"{len(detections)} detection(s), "
            f"trace_id={trace.get('trace_id', '?')}"
        )
        for det in detections:
            agent = det.get("mistake_agent")
            console.print(
                f"  [yellow]{det.get('category')}[/yellow] "
                f"confidence={det.get('confidence', 0):.2f} "
                f"severity={det.get('severity')}" + (f" agent={agent}" if agent else "")
            )
        primary = diagnosis.get("primary_failure")
        if primary:
            console.print(
                f"  [bold red]primary:[/bold red] {primary.get('title')} "
                f"({primary.get('category')})"
            )


@omnigent_group.command("watch")
@click.option(
    "--server",
    default="http://localhost:6767",
    show_default=True,
    help="Omnigent server base URL.",
)
@click.option("--session", "session_id", default=None, help="Session id to watch.")
@click.option(
    "--latest",
    is_flag=True,
    default=False,
    help="Watch the most recently created non-archived session.",
)
@click.option(
    "--api-url",
    envvar="PISAMA_API_URL",
    default="https://api.pisama.ai",
    show_default=True,
    help="Pisama API base URL (env: PISAMA_API_URL).",
)
@click.option(
    "--api-key",
    envvar="PISAMA_API_KEY",
    default=None,
    help="Pisama API key (env: PISAMA_API_KEY). Required unless the target "
    "backend allows unauthenticated ATIF ingest.",
)
@click.option("--project-id", default=None, help="Pisama project id (ps_...).")
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Exit after the session goes idle (one final analysis).",
)
def watch_cmd(
    server: str,
    session_id: Optional[str],
    latest: bool,
    api_url: str,
    api_key: Optional[str],
    project_id: Optional[str],
    once: bool,
) -> None:
    """Watch an Omnigent session and analyze it with Pisama after each turn.

    Requires either --session <id> or --latest.
    """
    if not session_id and not latest:
        raise click.UsageError("pass --session <id> or --latest")

    async def _run() -> None:
        sid = session_id
        if sid is None:
            async with httpx.AsyncClient(timeout=15) as http:
                resp = await http.get(f"{server.rstrip('/')}/v1/sessions")
                resp.raise_for_status()
                payload = resp.json()
                sessions = payload.get("data") or payload.get("sessions") or []
                candidates = [s for s in sessions if not s.get("archived")]
                if not candidates:
                    raise click.ClickException("no sessions found on the server")
                candidates.sort(key=lambda s: s.get("created_at") or 0)
                sid = candidates[-1]["id"]
        watcher = SessionWatcher(
            server=server,
            api_url=api_url,
            session_id=sid,
            api_key=api_key,
            project_id=project_id,
        )
        await watcher.run(once=once)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("[dim]stopped[/dim]")
