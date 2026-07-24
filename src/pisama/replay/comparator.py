"""Compare detection results between two trace analyses."""

from __future__ import annotations

from dataclasses import dataclass, field

from pisama._analyze import AnalyzeResult


@dataclass
class ComparisonResult:
    """Result of comparing two trace analyses.

    Categorizes each detector into one of:
    - fixed: was detected in A, clear in B
    - improved: severity decreased from A to B
    - regressed: new detection or severity increased in B
    - unchanged: same status in both
    """

    trace_a_id: str
    trace_b_id: str
    fixed: list[str] = field(default_factory=list)
    improved: list[str] = field(default_factory=list)
    regressed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def has_regressions(self) -> bool:
        """Whether any detectors regressed."""
        return len(self.regressed) > 0

    @property
    def has_improvements(self) -> bool:
        """Whether any detectors improved."""
        return len(self.fixed) > 0 or len(self.improved) > 0

    @classmethod
    def compare(cls, a: AnalyzeResult, b: AnalyzeResult) -> "ComparisonResult":
        """Compare two analysis results.

        Args:
            a: The baseline (before) analysis.
            b: The comparison (after) analysis.

        Returns:
            ComparisonResult categorizing each detector.
        """
        # Build maps: detector_type -> (severity, detected)
        a_map = _build_detector_map(a)
        b_map = _build_detector_map(b)

        all_detectors = sorted(set(a_map.keys()) | set(b_map.keys()))

        result = cls(
            trace_a_id=a.trace_id,
            trace_b_id=b.trace_id,
        )

        for det in all_detectors:
            a_sev = a_map.get(det, 0)
            b_sev = b_map.get(det, 0)

            if a_sev > 0 and b_sev == 0:
                # Was detected, now clear
                result.fixed.append(det)
            elif a_sev > b_sev and b_sev > 0:
                # Severity decreased
                result.improved.append(det)
            elif a_sev == 0 and b_sev > 0:
                # New detection
                result.regressed.append(det)
            elif b_sev > a_sev:
                # Severity increased
                result.regressed.append(det)
            else:
                # Same
                result.unchanged.append(det)

        return result


def _build_detector_map(result: AnalyzeResult) -> dict[str, int]:
    """Build a map of detector_type -> max severity from an AnalyzeResult."""
    detector_map: dict[str, int] = {}
    for issue in result.issues:
        existing = detector_map.get(issue.type, 0)
        if issue.severity > existing:
            detector_map[issue.type] = issue.severity
    return detector_map
