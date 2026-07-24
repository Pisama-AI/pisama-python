"""In-memory session store for the local MCP server.

Keeps a ring buffer of recent analysis results so the ``pisama_status``
tool can return a useful summary without any persistent storage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionStore:
    """In-memory ring buffer for recent analysis results."""

    max_entries: int = 100
    _results: list[dict[str, Any]] = field(default_factory=list)

    def add(self, result: dict[str, Any]) -> None:
        """Append a result, evicting the oldest if at capacity."""
        self._results.append(result)
        if len(self._results) > self.max_entries:
            self._results = self._results[-self.max_entries :]

    def summary(self) -> dict[str, Any]:
        """Return aggregate stats across all stored results."""
        total = len(self._results)
        if total == 0:
            return {
                "total_analyses": 0,
                "issues_by_type": {},
                "severity_distribution": {"low": 0, "medium": 0, "high": 0, "critical": 0},
                "total_issues": 0,
            }

        issues_by_type: Counter[str] = Counter()
        severity_dist = {"low": 0, "medium": 0, "high": 0, "critical": 0}
        total_issues = 0

        for entry in self._results:
            for det in entry.get("detection_results", []):
                if det.get("detected"):
                    total_issues += 1
                    issues_by_type[det.get("detector_name", "unknown")] += 1
                    sev = det.get("severity", 0)
                    if sev >= 80:
                        severity_dist["critical"] += 1
                    elif sev >= 60:
                        severity_dist["high"] += 1
                    elif sev >= 40:
                        severity_dist["medium"] += 1
                    else:
                        severity_dist["low"] += 1

        return {
            "total_analyses": total,
            "issues_by_type": dict(issues_by_type),
            "severity_distribution": severity_dist,
            "total_issues": total_issues,
        }

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        """Return the *n* most recent results."""
        return self._results[-n:]

    def clear(self) -> None:
        """Drop all stored results."""
        self._results.clear()
