"""``pisama-openhands-monitor`` CLI.

Wraps :class:`pisama.agents.OpenHandsEventStreamAdapter` for batch
analysis of a completed session directory. Looks for the Harbor-emitted
``agent/trajectory.json`` (or ``trajectory.json`` at session root),
POSTs it to the Pisama backend, prints a concise diagnosis.

Usage::

    pisama-openhands-monitor <session-dir>
    pisama-openhands-monitor <session-dir> --api-url https://api.pisama.ai
    pisama-openhands-monitor <session-dir> --json   # full diagnosis as JSON

Exit codes:
  0 — analysis succeeded, no failures detected
  1 — analysis succeeded, at least one failure detected
  2 — usage / runtime error (e.g., session_dir missing trajectory.json)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from ..openhands_adapter import OpenHandsEventStreamAdapter


def _format_human_summary(result) -> str:
    lines: list[str] = []
    lines.append(f"trace_id: {result.trace_id}")
    if result.source_path:
        lines.append(f"source:   {result.source_path}")
    lines.append(f"spans:    {result.span_count}")
    lines.append(f"tokens:   {result.total_tokens}")
    lines.append(
        f"score:    {result.trajectory_score:.3f}  "
        f"(1.0 = clean; lower = worse)"
    )
    lines.append("")
    if not result.has_failures:
        lines.append("✓ No failures detected.")
        if result.detectors_run:
            lines.append(f"  ran {len(result.detectors_run)} detectors: "
                         f"{', '.join(sorted(result.detectors_run))}")
        return "\n".join(lines)

    lines.append(f"✗ {result.failure_count} failure(s) detected:")
    for d in result.failures:
        lines.append(
            f"  [{d.severity.upper():<8}] {d.detector:<24} "
            f"conf={d.confidence:.2f}  {d.title}"
        )
        if d.description:
            lines.append(f"     {d.description}")
    if result.detectors_failed:
        lines.append("")
        lines.append("Detectors that raised during the run:")
        for name, err in result.detectors_failed.items():
            lines.append(f"  - {name}: {err}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pisama-openhands-monitor",
        description=(
            "Analyze a completed OpenHands session through the Pisama "
            "backend. Reads the Harbor-emitted trajectory.json from the "
            "session dir; expects the trajectory in ATIF format."
        ),
    )
    parser.add_argument(
        "session_dir",
        type=Path,
        help="Path to the completed session directory.",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default=None,
        help="Override the Pisama backend URL (default: https://api.pisama.ai).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--project-id",
        type=str,
        default=None,
        help="Optional Pisama project id (ps_...) to correlate the run with.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full diagnosis as JSON instead of the human summary.",
    )
    args = parser.parse_args(argv)

    if not args.session_dir.exists():
        print(f"error: session_dir does not exist: {args.session_dir}", file=sys.stderr)
        return 2

    adapter = OpenHandsEventStreamAdapter(
        api_url=args.api_url,
        mode="batch",
        project_id=args.project_id,
        timeout_seconds=args.timeout,
    )
    try:
        result = adapter.on_session_complete(args.session_dir)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result.raw, indent=2, default=str))
    else:
        print(_format_human_summary(result))

    return 1 if result.has_failures else 0


if __name__ == "__main__":
    sys.exit(main())
