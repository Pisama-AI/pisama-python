"""pisama check <path> command.

Walks a directory (or analyzes a single file), runs the public ``analyze``
API on every saved trace it finds, and exits non-zero when any finding
meets a configurable severity threshold. Drops into a GitHub Action or
pytest run to gate PRs on agent-failure detection.

This is a thin wrapper around ``pisama._analyze.analyze`` — no backend
modules are imported, so the command works wherever ``pip install pisama``
works.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import click
from rich.console import Console

from pisama._analyze import AnalyzeResult, analyze, available_detectors

console = Console(stderr=True)

#: Version of the --json payload shape. Bump when the structure changes so
#: CI consumers can pin against it.
JSON_SCHEMA_VERSION = 2

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".next", "dist", "build"}
TRACE_SUFFIXES = {".json", ".jsonl"}

SEVERITY_THRESHOLDS = {
    "info": 0,
    "warning": 30,
    "error": 60,
    "critical": 80,
    "never": 101,
}


@dataclass
class _CheckReport:
    """Accumulate a stable per-file result set and aggregate summary."""

    files_total: int
    fail_on: str
    severity_threshold: int
    results: list[dict[str, object]] = field(default_factory=list)
    files_analyzed: int = 0
    files_clean: int = 0
    files_with_issues: int = 0
    files_failed: int = 0
    analysis_errors: int = 0
    discovery_errors: int = 0
    parse_errors: int = 0
    issues_total: int = 0
    issues_at_or_above_threshold: int = 0
    failed: bool = False

    def record_error(self, path: Path, exc: FileNotFoundError | ValueError) -> None:
        """Record a file or parse failure as a failed candidate."""
        is_parse_error = isinstance(exc, ValueError)
        self.analysis_errors += 1
        self.parse_errors += int(is_parse_error)
        self.files_failed += 1
        self.failed = True
        self.results.append(
            {
                "file": str(path),
                "status": "parse_error" if is_parse_error else "file_error",
                "failed": True,
                "trace_id": None,
                "detectors_run": 0,
                "execution_time_ms": None,
                "issues": [],
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )

    def record_discovery_error(self, path: Path, exc: ValueError) -> None:
        """Record a Harbor job that cannot be safely enumerated."""
        self.analysis_errors += 1
        self.discovery_errors += 1
        self.files_failed += 1
        self.failed = True
        self.results.append(
            {
                "file": str(path),
                "status": "discovery_error",
                "failed": True,
                "trace_id": None,
                "detectors_run": 0,
                "execution_time_ms": None,
                "issues": [],
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        )

    def record_result(self, path: Path, result: AnalyzeResult) -> None:
        """Record one successful analysis, including a clean result."""
        triggered = [issue for issue in result.issues if issue.severity >= self.severity_threshold]
        failed_threshold = bool(triggered)
        self.files_analyzed += 1
        self.issues_total += len(result.issues)
        self.issues_at_or_above_threshold += len(triggered)
        self.files_with_issues += int(result.has_issues)
        self.files_clean += int(not result.has_issues)
        self.files_failed += int(failed_threshold)
        self.failed = self.failed or failed_threshold
        self.results.append(
            {
                "file": str(path),
                "status": "issues" if result.has_issues else "clean",
                "failed": failed_threshold,
                "trace_id": result.trace_id,
                "detectors_run": result.detectors_run,
                "execution_time_ms": result.execution_time_ms,
                "issues": [asdict(issue) for issue in result.issues],
                "error": None,
            }
        )

    def payload(self) -> dict[str, object]:
        """Build the versioned machine-readable check result."""
        summary = {
            "files_total": self.files_total,
            "files_analyzed": self.files_analyzed,
            "files_clean": self.files_clean,
            "files_with_issues": self.files_with_issues,
            "files_failed": self.files_failed,
            "analysis_errors": self.analysis_errors,
            "discovery_errors": self.discovery_errors,
            "parse_errors": self.parse_errors,
            "issues_total": self.issues_total,
            "issues_at_or_above_threshold": self.issues_at_or_above_threshold,
            "fail_on": self.fail_on,
            "severity_threshold": self.severity_threshold,
            "passed": not self.failed,
        }
        return {
            "schema_version": JSON_SCHEMA_VERSION,
            "summary": summary,
            "results": self.results,
        }


@click.command("check")
@click.argument("path", type=click.Path(exists=True, file_okay=True, dir_okay=True))
@click.option(
    "--fail-on",
    type=click.Choice(list(SEVERITY_THRESHOLDS.keys())),
    default="warning",
    show_default=True,
    help="Exit non-zero when any finding meets this severity threshold.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON to stdout instead of formatted output.",
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress per-file output. Summary + exit code only.",
)
@click.option(
    "--detectors",
    "detector_names",
    default=None,
    metavar="NAME[,NAME...]",
    help=("Run only these detectors (comma-separated). Run `pisama detectors` for the live list."),
)
@click.option(
    "--exclude-detectors",
    "excluded_names",
    default=None,
    metavar="NAME[,NAME...]",
    help="Run all detectors except these (comma-separated).",
)
def check_cmd(
    path: str,
    fail_on: str,
    output_json: bool,
    quiet: bool,
    detector_names: str | None,
    excluded_names: str | None,
) -> None:
    """Walk PATH, find saved traces, run detectors against each.

    PATH is a directory (walked recursively) or a single .json / .jsonl
    trace file. Saved traces in OTEL, Langfuse, Phoenix, or raw JSON
    format are auto-detected.

    Exit code is 1 when any finding meets the --fail-on threshold (so the
    command can gate CI) and 2 on usage errors such as an unknown
    detector name.
    """
    selected = _resolve_detector_selection(detector_names, excluded_names)
    target = Path(path)
    threshold = SEVERITY_THRESHOLDS[fail_on]
    try:
        candidates = _discover_traces(target)
    except ValueError as exc:
        if output_json:
            report = _CheckReport(1, fail_on, threshold)
            report.record_discovery_error(target, exc)
            click.echo(json.dumps(report.payload(), indent=2, default=str))
        else:
            console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    if not candidates:
        if output_json:
            click.echo(json.dumps(_CheckReport(0, fail_on, threshold).payload(), indent=2))
        else:
            console.print(f"[yellow]No trace files found under {path}[/yellow]")
        return

    report = _CheckReport(len(candidates), fail_on, threshold)

    for trace_path in candidates:
        try:
            result = analyze(str(trace_path), detectors=selected)
        except (FileNotFoundError, ValueError) as exc:
            # An unparseable / missing trace must fail the command, not be
            # silently skipped — otherwise `pisama check` false-greens a CI gate
            # when every trace it was handed is garbage.
            if not quiet:
                console.print(f"[red]Skip {trace_path}: {exc}[/red]")
            report.record_error(trace_path, exc)
            continue

        report.record_result(trace_path, result)
        _render_file_result(trace_path, result, threshold, quiet, output_json)

    if output_json:
        click.echo(json.dumps(report.payload(), indent=2, default=str))

    if report.failed:
        sys.exit(1)


def _resolve_detector_selection(
    detector_names: str | None, excluded_names: str | None
) -> list[str] | None:
    """Turn --detectors / --exclude-detectors into an explicit name list.

    Returns None when no selection was requested (run everything). Unknown
    names abort with a UsageError (exit code 2) and the valid list, so a
    typo can never silently produce a green gate.
    """
    if detector_names is not None and excluded_names is not None:
        raise click.UsageError("--detectors and --exclude-detectors are mutually exclusive.")
    if detector_names is None and excluded_names is None:
        return None

    valid = available_detectors()
    if detector_names is not None:
        return _parse_detector_names(detector_names, valid)
    assert excluded_names is not None
    excluded = set(_parse_detector_names(excluded_names, valid))
    return [name for name in valid if name not in excluded]


def _parse_detector_names(raw: str, valid: list[str]) -> list[str]:
    """Parse and validate one comma-separated detector selection."""
    requested = [name.strip() for name in raw.split(",") if name.strip()]
    if not requested:
        raise click.UsageError("Detector list is empty.")

    unknown = sorted(set(requested) - set(valid))
    if unknown:
        raise click.UsageError(
            f"Unknown detector(s): {', '.join(unknown)}. Valid names: {', '.join(valid)}"
        )
    return requested


def _discover_traces(target: Path) -> list[Path]:
    """Walk target, return list of trace-file candidates."""
    if target.is_file():
        return _discover_trace_file(target)

    # Harbor trial and job directories contain several JSON metadata/artifact
    # files beside the ATIF payload. Prefer exact trajectory.json files before
    # the generic recursive fallback so config.json, result.json, and verifier
    # artifacts are not treated as traces.
    trial_trajectory = target / "agent" / "trajectory.json"
    if trial_trajectory.is_file():
        return _expand_continuations([trial_trajectory], selection_boundary=target)

    harbor_trajectories = _harbor_trajectories(target)
    if harbor_trajectories:
        return _expand_continuations(harbor_trajectories, selection_boundary=target)

    return _generic_trace_candidates(target)


def _discover_trace_file(target: Path) -> list[Path]:
    if target.suffix not in TRACE_SUFFIXES:
        return []
    return (
        _expand_continuations([target], selection_boundary=target.parent)
        if target.suffix == ".json"
        else [target]
    )


def _harbor_trajectories(target: Path) -> list[Path]:
    return sorted(
        child
        for child in target.rglob("trajectory.json")
        if child.is_file() and not any(part in SKIP_DIRS for part in child.parts)
    )


def _generic_trace_candidates(target: Path) -> list[Path]:
    candidates: list[Path] = []
    for child in target.rglob("*"):
        if not child.is_file() or child.suffix not in TRACE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in child.parts):
            continue
        candidates.append(child)
    return sorted(candidates)


def _expand_continuations(
    roots: list[Path], *, selection_boundary: Path | None = None
) -> list[Path]:
    """Return each root and every explicitly linked continuation in run order.

    Pisama analyzes both sides of a compaction so failures before the handoff
    are retained. Only ``continued_trajectory_ref`` links are followed; sibling
    summarization helper trajectories are never globbed into the job.
    """
    expanded: list[Path] = []
    emitted: set[Path] = set()
    for root in sorted(roots):
        for segment in _continuation_chain(root, selection_boundary=selection_boundary):
            resolved = segment.resolve()
            if resolved in emitted:
                continue
            expanded.append(segment)
            emitted.add(resolved)
    return expanded


def _continuation_chain(root: Path, *, selection_boundary: Path | None = None) -> list[Path]:
    boundary = root.parent.resolve()
    selected_root = (selection_boundary or root.parent).resolve()
    chain: list[Path] = []
    visited: set[Path] = set()
    current = root
    while True:
        resolved = current.resolve()
        try:
            resolved.relative_to(selected_root)
            resolved.relative_to(boundary)
        except ValueError as exc:
            raise ValueError(
                f"ATIF trajectory resolves outside selected directory: {current}"
            ) from exc
        if resolved in visited:
            raise ValueError(f"ATIF continuation cycle detected at {current}")
        if not current.is_file():
            raise ValueError(f"ATIF continuation file not found: {current}")
        visited.add(resolved)
        chain.append(current)

        reference = _continued_trajectory_ref(current)
        if reference is None:
            return chain
        current = _safe_continuation_path(current, reference, boundary)


def _continued_trajectory_ref(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("continued_trajectory_ref") is None:
        return None
    reference = data["continued_trajectory_ref"]
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError(f"Invalid ATIF continued_trajectory_ref in {path}")
    return reference


def _safe_continuation_path(current: Path, reference: str, boundary: Path) -> Path:
    relative = Path(reference)
    if relative.is_absolute() or relative.suffix != ".json":
        raise ValueError(f"Unsafe ATIF continuation reference {reference!r} in {current}")
    candidate = current.parent / relative
    try:
        candidate.resolve().relative_to(boundary)
    except ValueError as exc:
        raise ValueError(
            f"ATIF continuation reference escapes trajectory directory: {reference!r}"
        ) from exc
    return candidate


def _render_file_result(
    trace_path: Path,
    result: AnalyzeResult,
    threshold: int,
    quiet: bool,
    output_json: bool,
) -> None:
    """Render one human-readable result when JSON or quiet mode is not active."""
    if quiet or output_json:
        return
    if result.has_issues:
        _print_file_report(trace_path, result, threshold)
    else:
        console.print(f"[green]OK[/green] {trace_path.name}: clean")


def _print_file_report(trace_path: Path, result: AnalyzeResult, threshold: int) -> None:
    """Render a per-file finding summary to stderr."""
    triggered_count = sum(1 for i in result.issues if i.severity >= threshold)
    marker_color = "red" if triggered_count > 0 else "yellow"
    console.print(
        f"[{marker_color}]F[/{marker_color}] {trace_path}: "
        f"{len(result.issues)} issue(s) "
        f"({triggered_count} at/above {_severity_label(threshold)})"
    )
    for issue in result.issues:
        sev_label = _severity_label(issue.severity)
        if issue.severity >= 60:
            sev_color = "red"
        elif issue.severity >= 30:
            sev_color = "yellow"
        else:
            sev_color = "blue"
        console.print(
            f"   [{sev_color}]{sev_label:>8}[/{sev_color}]  {issue.type}  ·  {issue.summary}"
        )


def _severity_label(sev: int) -> str:
    if sev >= 80:
        return "critical"
    if sev >= 60:
        return "error"
    if sev >= 30:
        return "warning"
    return "info"
