"""ATIF trajectory analysis client.

Wraps the Pisama backend's ``POST /api/v1/atif/analyze`` endpoint so
notebook/script users can analyze Harbor-emitted ATIF trajectories
without going through the CLI.

Usage::

    from pisama.agents import analyze_atif

    result = analyze_atif("./trajectories/run-001.json")
    if not result.analysis_complete:
        raise RuntimeError("ATIF analysis was incomplete")
    elif result.has_failures:
        for d in result.failures:
            print(d.detector, d.severity, d.title)

For directories or many files::

    from pisama.agents import analyze_atif_batch

    results = analyze_atif_batch("./trajectories/")
    for r in results:
        print(r.trace_id, r.failure_count)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

try:
    import httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


DEFAULT_API_URL = "https://api.pisama.ai"
DEFAULT_TIMEOUT_SECONDS = 60.0


# Keep in lockstep with backend/app/ingestion/atif_models.py — bump when the
# vendor pin updates.
SUPPORTED_SCHEMA_VERSIONS = frozenset(
    {
        "ATIF-v1.0",
        "ATIF-v1.1",
        "ATIF-v1.2",
        "ATIF-v1.3",
        "ATIF-v1.4",
        "ATIF-v1.5",
        "ATIF-v1.6",
        "ATIF-v1.7",
    }
)


@dataclass
class AtifDetection:
    """One detection surfaced by the orchestrator."""

    detector: str
    confidence: float
    severity: str
    title: str
    description: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AtifDetection":
        # Backend's DetectionResult.to_dict() returns the detector under
        # `category` (the DetectionCategory enum value). Accept the older
        # `detector` / `detection_type` keys too in case the response
        # schema is normalized later.
        name = (
            data.get("category")
            or data.get("detector")
            or data.get("detection_type")
            or "unknown"
        )
        return cls(
            detector=str(name),
            confidence=float(data.get("confidence", 0.0)),
            severity=str(data.get("severity", "low")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
        )


@dataclass
class AtifAnalyzeResult:
    """One trajectory's diagnosis."""

    trace_id: str
    source_path: str | None
    span_count: int
    total_tokens: int
    total_duration_ms: int
    atif_schema_version: str
    atif_session_id: str | None
    has_failures: bool
    failure_count: int
    detection_status: str
    failures: list[AtifDetection] = field(default_factory=list)
    detectors_run: list[str] = field(default_factory=list)
    detectors_failed: dict[str, str] = field(default_factory=dict)
    # False when the backend could not see every trajectory segment referenced
    # by this ATIF document. Detector execution can still be complete over the
    # visible spans, so callers should use ``analysis_complete`` for a single
    # trustworthy success/failure gate.
    topology_complete: bool = True
    unresolved_trajectory_refs: list[str] = field(default_factory=list)
    # Subset of ``unresolved_trajectory_refs`` whose safe, explicit
    # continuation targets were submitted as separate documents in the same
    # batch. The backend fields above remain unchanged so callers can inspect
    # each response exactly as the server reported it.
    client_resolved_trajectory_refs: list[str] = field(default_factory=list)
    # Sum of tokens in spans parsed from the submitted document. This can be
    # lower than ``total_tokens`` when ATIF ``final_metrics`` includes external
    # subagent or continuation trajectories that were not submitted inline.
    span_token_total: int = 0
    # Scalar PROCESS-QUALITY score in [0.0, 1.0]; 1.0 = clean, lower = worse.
    #
    # NOT a task-success score. Pisama Bench v1 (May 2026, n=270 across 5
    # corpora) shows the same scalar correlates differently with task
    # outcome depending on corpus type:
    #   - multi-agent reasoning (m500):       Pearson r = +0.45 with correctness
    #   - role-play dialogue (sotopia):       r = -0.44 (process noise IS the goal)
    #   - single-agent web tasks (ARB):       r ≈ -0.03 (uncorrelated)
    #
    # If you need "did the agent solve the task?", combine this with an
    # outcome signal from your own evaluator (test pass, reward, completion
    # check). The Tier 2 follow-up adds a separate task_completion_score
    # scalar for the outcome axis.
    #
    # Multiplicative composite of orchestration_quality_overall and the max
    # severity-weighted detector penalty. See orchestrator.DetectionOrchestrator
    # ._compute_trajectory_score for the formula. Defaults to 1.0 for backwards
    # compat with older server versions that don't yet emit the field.
    trajectory_score: float = 1.0
    # Scalar OUTCOME-QUALITY score in [0.0, 1.0] — "did the agent solve the
    # user's task?". Orthogonal to trajectory_score (process quality): a run can
    # execute cleanly (high trajectory_score) yet fail the user's job (low
    # task_completion_score). This is the AgentRewardBench gap — web agents
    # whose runs are clean while cum_reward ≈ 0.11.
    #
    # Driven by the Tier 2 outcome detector family (task_failure /
    # silent_failure / objective_unmet); 1.0 when that family is disabled
    # server-side or none fire. Defaults to 1.0 for backwards compat with
    # older servers that don't yet emit the field. See orchestrator
    # ._compute_task_completion_score for the multiplicative formula.
    task_completion_score: float = 1.0
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining_unresolved_trajectory_refs(self) -> list[str]:
        """Server-reported references not reconciled by this client batch."""
        resolved = set(self.client_resolved_trajectory_refs)
        return [ref for ref in self.unresolved_trajectory_refs if ref not in resolved]

    @property
    def reconciled_topology_complete(self) -> bool:
        """Whether server topology plus safe batch submissions are complete."""
        if self.remaining_unresolved_trajectory_refs:
            return False
        return self.topology_complete or bool(self.unresolved_trajectory_refs)

    @property
    def analysis_complete(self) -> bool:
        """Whether detector execution and trajectory topology are complete."""
        return (
            self.reconciled_topology_complete
            and self.detection_status.lower() == "complete"
            and not self.detectors_failed
        )

    @classmethod
    def from_response(
        cls, body: dict[str, Any], source_path: str | Path | None = None
    ) -> "AtifAnalyzeResult":
        diag = body.get("diagnosis", {})
        trace = body.get("trace", {})
        failures = [AtifDetection.from_dict(d) for d in diag.get("all_detections", [])]
        total_tokens = int(trace.get("total_tokens", 0))
        unresolved_refs = trace.get("unresolved_trajectory_refs", [])
        if not isinstance(unresolved_refs, list):
            unresolved_refs = []
        return cls(
            trace_id=str(trace.get("trace_id", diag.get("trace_id", ""))),
            source_path=str(source_path) if source_path is not None else None,
            span_count=int(trace.get("span_count", 0)),
            total_tokens=total_tokens,
            total_duration_ms=int(trace.get("total_duration_ms") or 0),
            atif_schema_version=str(trace.get("atif_schema_version", "")),
            atif_session_id=trace.get("atif_session_id"),
            has_failures=bool(diag.get("has_failures", False)),
            failure_count=int(diag.get("failure_count", 0)),
            detection_status=str(diag.get("detection_status", "unknown")),
            failures=failures,
            detectors_run=list(diag.get("detectors_run", [])),
            detectors_failed=dict(diag.get("detectors_failed", {})),
            topology_complete=bool(trace.get("topology_complete", True)),
            unresolved_trajectory_refs=[str(ref) for ref in unresolved_refs],
            span_token_total=int(trace.get("span_token_total", total_tokens)),
            trajectory_score=float(diag.get("trajectory_score", 1.0)),
            task_completion_score=float(diag.get("task_completion_score", 1.0)),
            score_breakdown=dict(diag.get("score_breakdown", {})),
            raw=body,
        )


def _load_trajectory(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Resolve ``source`` to a trajectory dict.

    Accepts a path (``str``/``Path``) or an already-parsed ATIF dict so
    callers can pre-process the JSON if needed.
    """
    if isinstance(source, dict):
        traj = source
    else:
        path = Path(source)
        with path.open("r", encoding="utf-8") as f:
            traj = json.load(f)
    schema = traj.get("schema_version")
    if not schema or schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"Unsupported ATIF schema_version {schema!r}. "
            f"Expected one of: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    continued_ref = traj.get("continued_trajectory_ref")
    if isinstance(continued_ref, str) and Path(continued_ref).is_absolute():
        raise ValueError(
            f"ATIF continued_trajectory_ref must be relative: {continued_ref!r}"
        )
    return traj


def _require_httpx() -> None:
    if not _HTTPX_AVAILABLE:
        raise ImportError(
            "httpx is required for analyze_atif: `pip install pisama[agents]`"
        )


def _resolve_api_key(api_key: str | None) -> str | None:
    """Resolve an explicit API key or the SDK-wide environment fallback.

    An explicit empty string disables the environment fallback. This keeps
    unauthenticated local and test endpoints usable even when the parent
    process has ``PISAMA_API_KEY`` configured.
    """
    candidate = api_key if api_key is not None else os.getenv("PISAMA_API_KEY")
    if candidate is None:
        return None
    candidate = candidate.strip()
    return candidate or None


def _authorization_headers(
    client: Any,
    *,
    base_url: str,
    api_key: str | None,
) -> dict[str, str]:
    """Exchange an API key for the JWT accepted by authenticated endpoints."""
    resolved_key = _resolve_api_key(api_key)
    if resolved_key is None:
        return {}

    response = client.post(
        f"{base_url}/api/v1/auth/token",
        json={"api_key": resolved_key, "scope": "full"},
    )
    response.raise_for_status()
    token_body = response.json()
    access_token = (
        token_body.get("access_token") if isinstance(token_body, dict) else None
    )
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError(
            "Pisama authentication response did not include a valid access_token"
        )
    return {"Authorization": f"Bearer {access_token.strip()}"}


def _analyze_with_client(
    client: Any,
    trajectory: dict[str, Any],
    *,
    source_path: str | None,
    project_id: str | None,
    base_url: str,
    headers: dict[str, str],
) -> AtifAnalyzeResult:
    """Analyze one trajectory using an already-authenticated HTTP client."""
    payload: dict[str, Any] = {"trajectory": trajectory}
    if project_id is not None:
        payload["project_id"] = project_id

    response = client.post(
        f"{base_url}/api/v1/atif/analyze",
        json=payload,
        headers=headers,
    )
    response.raise_for_status()

    return AtifAnalyzeResult.from_response(response.json(), source_path=source_path)


def analyze_atif(
    source: str | Path | dict[str, Any],
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> AtifAnalyzeResult:
    """Analyze a single ATIF trajectory with Pisama's detectors.

    Args:
        source: Path to a ``.json`` ATIF trajectory file, or a pre-parsed
            trajectory dict.
        project_id: Optional Pisama project id (ps_...) to correlate the
            run with. Reserved for future persistence; not used in v0.
        api_url: Override the Pisama backend URL. Default
            ``https://api.pisama.ai``.
        api_key: Pisama API key. When omitted, ``PISAMA_API_KEY`` is used.
            The key is exchanged for a short-lived JWT before analysis. Pass
            an empty string to disable the environment fallback for an
            unauthenticated local or test endpoint.
        timeout: HTTP timeout in seconds.

    Returns:
        ``AtifAnalyzeResult`` with diagnosis details.

    Raises:
        ValueError: If the trajectory's ``schema_version`` is unsupported.
        ImportError: If httpx is not installed (`pip install pisama[agents]`).
        httpx.HTTPStatusError: If the backend returns a non-2xx response.
    """
    _require_httpx()
    trajectory = _load_trajectory(source)
    source_path = str(source) if isinstance(source, (str, Path)) else None
    base_url = (api_url or DEFAULT_API_URL).rstrip("/")

    with httpx.Client(timeout=timeout) as client:
        headers = _authorization_headers(
            client,
            base_url=base_url,
            api_key=api_key,
        )
        return _analyze_with_client(
            client,
            trajectory,
            source_path=source_path,
            project_id=project_id,
            base_url=base_url,
            headers=headers,
        )


def _discover_trajectory_files(root: Path) -> list[Path]:
    """Find trajectory JSON files under ``root``.

    Three modes, tried in order:
      1. Harbor trial dir: ``root/agent/trajectory.json``.
      2. Harbor job-output dir: recursive exact ``**/trajectory.json``.
      3. Flat dir of ``*.json`` trajectories.

    Harbor job roots contain top-level ``config.json``, ``lock.json``, and
    ``result.json`` files. Recursive trajectory discovery must therefore run
    before the flat-directory fallback, or those metadata files are mistaken
    for ATIF trajectories.

    Exact ``trajectory.json`` files are Harbor trial roots. Each root's
    explicit ``continued_trajectory_ref`` chain is followed inside that
    root's agent directory. Files such as
    ``trajectory.summarization-1-summary.json`` are therefore not submitted
    as independent trajectories.

    This ordering is the canonical Harbor discovery behavior for SDK clients:
    nested trajectory files take precedence over job-level metadata.
    """
    try:
        directory_boundary = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"ATIF directory could not be resolved: {root}") from exc
    if not directory_boundary.is_dir():
        raise ValueError(f"ATIF directory is not a directory: {root}")
    _reject_escaping_agent_symlinks(root, directory_boundary)

    trial_file = root / "agent" / "trajectory.json"
    if trial_file.is_file():
        return _expand_continuation_chains(
            [trial_file], directory_boundary=directory_boundary
        )
    recursive = sorted(root.rglob("trajectory.json"))
    if recursive:
        return _expand_continuation_chains(
            recursive, directory_boundary=directory_boundary
        )
    flat = sorted(
        path for path in root.glob("*.json") if not _is_harbor_helper_trajectory(path)
    )
    if flat:
        return _expand_continuation_chains(flat, directory_boundary=directory_boundary)
    return []


def _reject_escaping_agent_symlinks(root: Path, directory_boundary: Path) -> None:
    """Reject Harbor agent directory links that leave the selected tree."""
    for agent_dir in root.rglob("agent"):
        if not agent_dir.is_symlink():
            continue
        try:
            resolved = agent_dir.resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                f"ATIF agent directory symlink could not be resolved: {agent_dir}"
            ) from exc
        if not resolved.is_dir() or not _is_relative_to(resolved, directory_boundary):
            raise ValueError(
                f"ATIF agent directory symlink escapes selected directory: {agent_dir}"
            )


def _is_harbor_helper_trajectory(path: Path) -> bool:
    """Exclude Harbor's non-root context summarization trajectory files."""
    name = path.name.lower()
    return (
        ".summarization-" in name
        or ".trajectory.cont-" in name
        or name.startswith("trajectory.cont-")
    )


def _expand_continuation_chains(
    roots: Iterable[Path], *, directory_boundary: Path | None = None
) -> list[Path]:
    """Return roots plus their safe, explicit continuation chains."""
    discovered: list[Path] = []
    emitted: set[Path] = set()
    for root in roots:
        for path in _follow_continuation_chain(
            root, directory_boundary=directory_boundary
        ):
            canonical = path.resolve()
            if canonical not in emitted:
                discovered.append(path)
                emitted.add(canonical)
    return discovered


def _follow_continuation_chain(
    root: Path, *, directory_boundary: Path | None = None
) -> list[Path]:
    """Follow one ATIF continuation chain without leaving its agent directory."""
    agent_dir, current = _resolve_chain_root(root, directory_boundary)
    chain: list[Path] = []
    seen: set[Path] = set()

    while True:
        _validate_chain_member(current, agent_dir, seen)
        seen.add(current)
        chain.append(current)
        candidate = _next_continuation(current, agent_dir)
        if candidate is None:
            return chain
        current = candidate


def _resolve_chain_root(
    root: Path,
    directory_boundary: Path | None,
) -> tuple[Path, Path]:
    try:
        agent_dir = root.parent.resolve(strict=True)
        current = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"ATIF trajectory file could not be resolved: {root}") from exc

    if directory_boundary is not None and (
        not _is_relative_to(agent_dir, directory_boundary)
        or not _is_relative_to(current, directory_boundary)
    ):
        raise ValueError(
            f"ATIF trajectory root or agent directory escapes selected directory: "
            f"{root}"
        )
    return agent_dir, current


def _validate_chain_member(
    current: Path,
    agent_dir: Path,
    seen: set[Path],
) -> None:
    if not _is_relative_to(current, agent_dir):
        raise ValueError(
            f"ATIF continued_trajectory_ref escapes agent directory: {current}"
        )
    if current in seen:
        raise ValueError(
            f"ATIF continued_trajectory_ref cycle detected at: {current}"
        )
    if not current.is_file():
        raise ValueError(f"ATIF continuation is not a file: {current}")


def _next_continuation(current: Path, agent_dir: Path) -> Path | None:
    continued_ref = _read_continued_trajectory_ref(current)
    if continued_ref is None:
        return None
    if Path(continued_ref).is_absolute():
        raise ValueError(
            f"ATIF continued_trajectory_ref must be relative: "
            f"{continued_ref!r} in {current}"
        )

    try:
        candidate = (current.parent / continued_ref).resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Invalid ATIF continued_trajectory_ref {continued_ref!r} in {current}"
        ) from exc
    if not _is_relative_to(candidate, agent_dir):
        raise ValueError(
            f"ATIF continued_trajectory_ref escapes agent directory: "
            f"{continued_ref!r} in {current}"
        )
    if not candidate.exists():
        raise ValueError(
            f"Missing ATIF continued_trajectory_ref {continued_ref!r} in {current}"
        )
    return candidate.resolve(strict=True)


def _read_continued_trajectory_ref(path: Path) -> str | None:
    """Read the sole filesystem relationship clients are allowed to follow."""
    try:
        with path.open("r", encoding="utf-8") as f:
            trajectory = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read ATIF trajectory JSON: {path}") from exc

    if not isinstance(trajectory, dict):
        raise ValueError(f"ATIF trajectory must be a JSON object: {path}")
    continued_ref = trajectory.get("continued_trajectory_ref")
    if continued_ref is None:
        return None
    if not isinstance(continued_ref, str) or not continued_ref.strip():
        raise ValueError(
            f"ATIF continued_trajectory_ref must be a non-empty string: {path}"
        )
    return continued_ref


def _is_relative_to(path: Path, root: Path) -> bool:
    """Python 3.10-compatible containment check."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reconcile_submitted_continuations(
    submissions: list[tuple[str | Path | dict[str, Any], dict[str, Any]]],
    results: list[AtifAnalyzeResult],
) -> None:
    """Mark server-unresolved continuation refs submitted in this batch."""
    submitted_paths = {
        Path(source).resolve(strict=True)
        for source, _trajectory in submissions
        if isinstance(source, (str, Path))
    }

    for (source, trajectory), result in zip(submissions, results):
        if not isinstance(source, (str, Path)):
            continue
        continued_ref = trajectory.get("continued_trajectory_ref")
        if not isinstance(continued_ref, str):
            continue
        if continued_ref in _subagent_trajectory_ref_targets(trajectory):
            continue

        source_path = Path(source).resolve(strict=True)
        agent_dir = source_path.parent
        target = (agent_dir / continued_ref).resolve(strict=False)
        if (
            _is_relative_to(target, agent_dir)
            and target in submitted_paths
            and continued_ref in result.unresolved_trajectory_refs
        ):
            result.client_resolved_trajectory_refs.append(continued_ref)


def _subagent_trajectory_ref_targets(value: Any) -> set[str]:
    """Collect path and id targets from nested ATIF subagent references."""
    targets: set[str] = set()

    def collect(current: Any) -> None:
        if isinstance(current, dict):
            refs = current.get("subagent_trajectory_ref")
            if isinstance(refs, list):
                for ref in refs:
                    if not isinstance(ref, dict):
                        continue
                    for key in ("trajectory_path", "trajectory_id"):
                        target = ref.get(key)
                        if isinstance(target, str):
                            targets.add(target)
            for nested in current.values():
                collect(nested)
        elif isinstance(current, list):
            for nested in current:
                collect(nested)

    collect(value)
    return targets


def analyze_atif_batch(
    sources: str | Path | Iterable[str | Path | dict[str, Any]],
    *,
    project_id: str | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[AtifAnalyzeResult]:
    """Analyze many ATIF trajectories sequentially.

    Args:
        sources: One of:
            - A file path (string or ``Path``); its explicit continuation
              chain is included.
            - A directory: a single Harbor trial dir
              (``<dir>/agent/trajectory.json``), a flat dir of
              ``*.json`` trajectories, or a Harbor job-output dir (walked
              recursively for exact ``trajectory.json`` roots). Explicit
              continuation chains are included; summarization helpers are not.
            - An iterable of file paths and/or pre-parsed dicts.

    Other args mirror :func:`analyze_atif`. Returns one
    :class:`AtifAnalyzeResult` per source in input order.
    """
    paths_or_dicts: list[str | Path | dict[str, Any]]
    if isinstance(sources, (str, Path)):
        p = Path(sources)
        if p.is_dir():
            paths_or_dicts = list(_discover_trajectory_files(p))
        else:
            paths_or_dicts = list(_expand_continuation_chains([p]))
    else:
        paths_or_dicts = list(sources)

    if not paths_or_dicts:
        return []

    submissions = [(src, _load_trajectory(src)) for src in paths_or_dicts]
    _require_httpx()
    base_url = (api_url or DEFAULT_API_URL).rstrip("/")
    with httpx.Client(timeout=timeout) as client:
        headers = _authorization_headers(
            client,
            base_url=base_url,
            api_key=api_key,
        )
        results = [
            _analyze_with_client(
                client,
                trajectory,
                source_path=str(src) if isinstance(src, (str, Path)) else None,
                project_id=project_id,
                base_url=base_url,
                headers=headers,
            )
            for src, trajectory in submissions
        ]
    _reconcile_submitted_continuations(submissions, results)
    return results


__all__ = [
    "AtifAnalyzeResult",
    "AtifDetection",
    "DEFAULT_API_URL",
    "SUPPORTED_SCHEMA_VERSIONS",
    "analyze_atif",
    "analyze_atif_batch",
]
