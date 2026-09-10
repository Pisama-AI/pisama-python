"""High-level analyze API wrapping the DetectionOrchestrator."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
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
    detector_assessments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_detector_errors(self) -> bool:
        return any(item.get("assessment") == "error" for item in self.detector_assessments)

    @property
    def coverage_complete(self) -> bool:
        return (
            self.detectors_run > 0
            and len(self.detector_assessments) == self.detectors_run
            and all(
                item.get("assessment") in {"contract_satisfied", "contract_violated", "finding"}
                for item in self.detector_assessments
            )
        )

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
        detector_assessments=_convert_assessments(analysis),
    )


def _convert_assessments(analysis: Any) -> list[dict[str, Any]]:
    """Preserve explicit coverage without treating legacy silence as success."""
    assessments = []
    for result in analysis.detection_results:
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        assessment = metadata.get("assessment")
        if "error" in metadata:
            assessment = "error"
        elif not isinstance(assessment, str) or assessment not in {
            "abstained",
            "contract_satisfied",
            "contract_violated",
        }:
            assessment = "finding" if result.detected else "unknown"
        elif result.detected and assessment != "contract_violated":
            assessment = "finding"
        elif not result.detected and assessment == "contract_violated":
            assessment = "unknown"
        checked = metadata.get("checked_contracts")
        if assessment == "contract_satisfied" and (type(checked) is not int or checked < 1):
            assessment = "unknown"
        elif (
            assessment == "abstained"
            and checked is not None
            and (type(checked) is not int or checked != 0)
        ):
            assessment = "unknown"
        item = {"detector_name": result.detector_name, "assessment": assessment}
        # Expose coverage provenance, not arbitrary metadata/error strings that
        # could contain captured input or credentials.
        if type(checked) is int and checked >= 0:
            item["checked_contracts"] = checked
        basis = metadata.get("confidence_basis")
        if isinstance(basis, str) and basis == "uncalibrated contract heuristic":
            item["confidence_basis"] = basis
        assessments.append(item)
    return assessments


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
