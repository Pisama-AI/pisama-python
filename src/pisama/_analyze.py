"""High-level analyze API wrapping the DetectionOrchestrator."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Union

from pisama_core.traces.models import Trace

from pisama._loader import load_trace


class UnknownDetectorError(ValueError):
    """Raised when a requested detector name is not registered."""

    def __init__(self, unknown: list[str], available: list[str]) -> None:
        self.unknown = unknown
        self.available = available
        super().__init__(
            f"Unknown detector(s): {', '.join(unknown)}. Available: {', '.join(available)}"
        )


def available_detectors() -> list[str]:
    """Sorted names of all registered built-in detectors."""
    # Import triggers detector auto-registration on first use
    from pisama_core.detection.detectors import __all__ as _detectors_loaded  # noqa: F401
    from pisama_core.detection.registry import registry as global_registry

    return sorted(d.name for d in global_registry.get_all())


@dataclass
class Issue:
    """A single detected issue."""

    type: str
    summary: str
    severity: int
    confidence: float
    evidence: list[dict[str, Any]]
    recommendation: Optional[str]


@dataclass
class AnalyzeResult:
    """Result of running all detectors on a trace."""

    issues: list[Issue]
    trace_id: str
    detectors_run: int
    execution_time_ms: float

    @property
    def has_issues(self) -> bool:
        """Whether any issues were detected."""
        return len(self.issues) > 0

    @property
    def critical_issues(self) -> list[Issue]:
        """Issues with severity >= 60."""
        return [i for i in self.issues if i.severity >= 60]


def analyze(
    input_data: Union[str, dict[str, Any], Trace],
    detectors: Optional[Sequence[str]] = None,
) -> AnalyzeResult:
    """Analyze a trace for multi-agent failures.

    Synchronous wrapper around async_analyze(). Handles the case where an
    event loop is already running (e.g. Jupyter notebooks) by spawning a
    background thread.

    Args:
        input_data: A file path, JSON string, dict, or Trace object.
        detectors: Optional subset of detector names to run. Defaults to
            all registered detectors. Unknown names raise
            UnknownDetectorError.

    Returns:
        AnalyzeResult with detected issues.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # Already inside an event loop (Jupyter, async REPL, etc.).
        # Run in a new thread with its own loop.
        result_container: list[Union[AnalyzeResult, BaseException]] = []

        def _run() -> None:
            try:
                result_container.append(asyncio.run(async_analyze(input_data, detectors=detectors)))
            except BaseException as exc:
                result_container.append(exc)

        thread = threading.Thread(target=_run)
        thread.start()
        thread.join()

        if isinstance(result_container[0], BaseException):
            raise result_container[0]
        return result_container[0]

    return asyncio.run(async_analyze(input_data, detectors=detectors))


async def async_analyze(
    input_data: Union[str, dict[str, Any], Trace],
    detectors: Optional[Sequence[str]] = None,
) -> AnalyzeResult:
    """Analyze a trace for multi-agent failures (async).

    Args:
        input_data: A file path, JSON string, dict, or Trace object.
        detectors: Optional subset of detector names to run. Defaults to
            all registered detectors. Unknown names raise
            UnknownDetectorError.

    Returns:
        AnalyzeResult with detected issues.
    """
    start = time.perf_counter()

    trace = load_trace(input_data)

    # Import here to trigger detector auto-registration on first use
    from pisama_core.detection.detectors import __all__ as _detectors_loaded  # noqa: F401
    from pisama_core.detection.orchestrator import DetectionOrchestrator
    from pisama_core.detection.registry import DetectorRegistry
    from pisama_core.detection.registry import registry as global_registry

    scoped_registry: Optional[DetectorRegistry] = None
    if detectors is not None:
        # Build a per-call registry so the shared global singleton is never
        # mutated (the MCP server shares that module in-process).
        available = {d.name: d for d in global_registry.get_all()}
        unknown = sorted(set(detectors) - set(available))
        if unknown:
            raise UnknownDetectorError(unknown, sorted(available))
        scoped_registry = DetectorRegistry()
        for name in detectors:
            scoped_registry.register(available[name])

    orchestrator = DetectionOrchestrator(registry=scoped_registry)
    analysis = await orchestrator.analyze(trace)

    issues = _convert_issues(analysis)
    elapsed_ms = (time.perf_counter() - start) * 1000

    return AnalyzeResult(
        issues=issues,
        trace_id=trace.trace_id,
        detectors_run=analysis.total_detectors_run,
        execution_time_ms=elapsed_ms,
    )


def _convert_issues(
    analysis: Any,
) -> list[Issue]:
    """Convert DetectionResult objects into Issue dataclasses."""
    issues: list[Issue] = []
    for result in analysis.detection_results:
        if not result.detected:
            continue
        rec_text: Optional[str] = None
        if result.recommendation is not None:
            rec_text = result.recommendation.instruction
        issues.append(
            Issue(
                type=result.detector_name,
                summary=result.summary,
                severity=result.severity,
                confidence=result.confidence,
                evidence=[e.to_dict() for e in result.evidence],
                recommendation=rec_text,
            )
        )
    return issues
