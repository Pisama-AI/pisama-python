"""Smoke test runner -- batch detection across multiple traces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pisama_core.traces import Trace

from pisama._analyze import async_analyze


@dataclass
class DetectorStats:
    """Aggregate statistics for a single detector."""

    count: int = 0
    total_severity: int = 0
    max_severity: int = 0
    trace_ids: list[str] = field(default_factory=list)

    @property
    def avg_severity(self) -> float:
        """Average severity across all detections."""
        return self.total_severity / self.count if self.count else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "count": self.count,
            "avg_severity": round(self.avg_severity, 1),
            "max_severity": self.max_severity,
            "trace_ids": self.trace_ids,
        }


@dataclass
class SmokeTestResult:
    """Result of a smoke test run."""

    total_traces: int = 0
    traces_with_issues: int = 0
    per_detector_stats: dict[str, DetectorStats] = field(default_factory=dict)
    critical_traces: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "total_traces": self.total_traces,
            "traces_with_issues": self.traces_with_issues,
            "per_detector_stats": {k: v.to_dict() for k, v in self.per_detector_stats.items()},
            "critical_traces": self.critical_traces,
            "errors": self.errors,
        }


class SmokeRunner:
    """Run detection across multiple traces and aggregate results."""

    async def run(
        self,
        traces: list[Trace],
        detectors: Optional[list[str]] = None,
    ) -> SmokeTestResult:
        """Run detection on all traces and aggregate.

        Args:
            traces: List of traces to analyze.
            detectors: Optional list of detector names to restrict to.

        Returns:
            SmokeTestResult with aggregate statistics.
        """
        result = SmokeTestResult(total_traces=len(traces))

        for trace in traces:
            try:
                analysis = await async_analyze(trace)
            except Exception as exc:
                result.errors.append(f"trace {trace.trace_id[:12]}: {exc}")
                continue

            # Filter by requested detectors if specified
            issues = analysis.issues
            if detectors:
                issues = [i for i in issues if i.type in detectors]

            if issues:
                result.traces_with_issues += 1

            has_critical = False
            for issue in issues:
                # Update per-detector stats
                if issue.type not in result.per_detector_stats:
                    result.per_detector_stats[issue.type] = DetectorStats()

                stats = result.per_detector_stats[issue.type]
                stats.count += 1
                stats.total_severity += issue.severity
                if issue.severity > stats.max_severity:
                    stats.max_severity = issue.severity
                stats.trace_ids.append(trace.trace_id)

                if issue.severity >= 60:
                    has_critical = True

            if has_critical:
                result.critical_traces.append(trace.trace_id)

        return result
