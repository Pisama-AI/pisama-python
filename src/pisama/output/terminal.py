"""Rich-based terminal output for Pisama results."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pisama._analyze import AnalyzeResult, Issue

console = Console()


def _severity_color(severity: int) -> str:
    """Map severity (0-100) to a Rich color name."""
    if severity >= 80:
        return "red bold"
    if severity >= 60:
        return "red"
    if severity >= 40:
        return "yellow"
    if severity >= 20:
        return "cyan"
    return "green"


def _severity_label(severity: int) -> str:
    """Human-readable severity label."""
    if severity >= 80:
        return "CRITICAL"
    if severity >= 60:
        return "HIGH"
    if severity >= 40:
        return "MEDIUM"
    if severity >= 20:
        return "LOW"
    return "INFO"


def display_analysis_result(result: AnalyzeResult) -> None:
    """Print a formatted analysis result to the terminal."""
    # Header
    if result.has_issues:
        issue_count = len(result.issues)
        critical_count = len(result.critical_issues)
        header_style = "red bold" if critical_count else "yellow bold"
        header_text = f"Found {issue_count} issue{'s' if issue_count != 1 else ''}"
        if critical_count:
            header_text += f" ({critical_count} critical)"
    else:
        header_style = "green bold"
        header_text = "No issues detected"

    console.print()
    console.print(
        Panel(
            Text(header_text, style=header_style),
            title="[bold]Pisama Analysis[/bold]",
            subtitle=f"trace={result.trace_id[:12]}  detectors={result.detectors_run}  "
            f"time={result.execution_time_ms:.0f}ms",
            border_style="dim",
        )
    )

    if not result.issues:
        return

    # Issues table
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Severity", width=10)
    table.add_column("Detector", width=18)
    table.add_column("Summary", ratio=1)
    table.add_column("Conf", width=5, justify="right")

    sorted_issues = sorted(result.issues, key=lambda i: -i.severity)
    for issue in sorted_issues:
        sev_style = _severity_color(issue.severity)
        sev_text = Text(
            f"{_severity_label(issue.severity):>8} {issue.severity:>3}",
            style=sev_style,
        )
        conf_text = Text(f"{issue.confidence:.0%}", style="dim")
        table.add_row(sev_text, issue.type, issue.summary, conf_text)

    console.print(table)

    # Recommendations
    recs = [i for i in sorted_issues if i.recommendation]
    if recs:
        console.print()
        console.print("[bold]Recommendations:[/bold]")
        for issue in recs:
            style = _severity_color(issue.severity)
            console.print(f"  [{style}]{issue.type}[/{style}]: {issue.recommendation}")

    console.print()


def display_detector_list(detectors: Sequence[Any]) -> None:
    """Print a formatted table of available detectors.

    Args:
        detectors: Sequence of BaseDetector instances (from the registry).
    """
    table = Table(
        title="Available Detectors",
        show_header=True,
        header_style="bold",
        box=None,
        pad_edge=False,
    )
    table.add_column("#", width=4, justify="right", style="dim")
    table.add_column("Name", width=20)
    table.add_column("Version", width=9)
    table.add_column("Description", ratio=1)
    table.add_column("Status", width=10, justify="center")

    sorted_dets = sorted(detectors, key=lambda d: d.name)
    for idx, det in enumerate(sorted_dets, 1):
        status_text = (
            Text("enabled", style="green") if det.enabled else Text("disabled", style="dim")
        )
        table.add_row(
            str(idx),
            det.name,
            det.version,
            det.description,
            status_text,
        )

    console.print()
    console.print(table)
    console.print(f"\n[dim]{len(sorted_dets)} detectors registered[/dim]\n")


# ---------------------------------------------------------------------------
# WatchDisplay -- real-time display for `pisama watch`
# ---------------------------------------------------------------------------


@dataclass
class _SpanInfo:
    """Lightweight span summary for display."""

    name: str
    kind: str
    status: str
    trace_id: str


class WatchDisplay:
    """Real-time terminal display for pisama watch.

    Uses Rich Live to show a continuously-updating status panel with
    recent spans and detected issues.

    Falls back to simple line-by-line printing when the terminal does not
    support rich Live (e.g. piped output, dumb terminal).
    """

    def __init__(self, min_severity: int = 0):
        self.min_severity = min_severity
        self._lock = threading.Lock()
        self._spans: list[_SpanInfo] = []
        self._issues: list[Issue] = []
        self._span_count = 0
        self._trace_count = 0
        self._live: Any = None
        self._console = Console(stderr=True)
        self._use_live = self._console.is_terminal

    def start(self) -> None:
        """Start the live display."""
        if self._use_live:
            try:
                from rich.live import Live

                self._live = Live(
                    self._render(),
                    console=self._console,
                    refresh_per_second=4,
                )
                self._live.start()
            except Exception:
                self._use_live = False

    def stop(self) -> None:
        """Stop the live display."""
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def add_span(self, span_name: str, kind: str, status: str, trace_id: str) -> None:
        """Record a new span arrival."""
        with self._lock:
            self._span_count += 1
            info = _SpanInfo(
                name=span_name,
                kind=kind,
                status=status,
                trace_id=trace_id[:12] if trace_id else "?",
            )
            self._spans.append(info)
            # Keep last 20 for display
            if len(self._spans) > 20:
                self._spans = self._spans[-20:]

        if self._use_live and self._live is not None:
            try:
                self._live.update(self._render())
            except Exception:
                pass
        elif not self._use_live:
            self._console.print(f"  [dim]span[/dim] {span_name} [{kind}] {status}")

    def set_trace_count(self, count: int) -> None:
        """Update trace count."""
        with self._lock:
            self._trace_count = count

    def add_issue(self, issue: Issue) -> None:
        """Record a detected issue."""
        if issue.severity < self.min_severity:
            return

        with self._lock:
            self._issues.append(issue)

        if self._use_live and self._live is not None:
            try:
                self._live.update(self._render())
            except Exception:
                pass
        else:
            sev_style = _severity_color(issue.severity)
            self._console.print(
                f"  [{sev_style}]{_severity_label(issue.severity)}[/{sev_style}] "
                f"{issue.type}: {issue.summary}"
            )

    def show_summary(self) -> None:
        """Print final summary after the subprocess exits."""
        out = Console()
        out.print()

        # Summary header
        issue_count = len(self._issues)
        if issue_count == 0:
            header_style = "green bold"
            header_text = "No issues detected"
        else:
            critical = sum(1 for i in self._issues if i.severity >= 60)
            header_style = "red bold" if critical else "yellow bold"
            header_text = f"Found {issue_count} issue{'s' if issue_count != 1 else ''}"
            if critical:
                header_text += f" ({critical} critical)"

        out.print(
            Panel(
                Text(header_text, style=header_style),
                title="[bold]Pisama Watch Summary[/bold]",
                subtitle=f"spans={self._span_count}  traces={self._trace_count}",
                border_style="dim",
            )
        )

        if not self._issues:
            return

        # Issues table
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Severity", width=10)
        table.add_column("Detector", width=18)
        table.add_column("Summary", ratio=1)
        table.add_column("Conf", width=5, justify="right")

        sorted_issues = sorted(self._issues, key=lambda i: -i.severity)
        for issue in sorted_issues:
            sev_style = _severity_color(issue.severity)
            sev_text = Text(
                f"{_severity_label(issue.severity):>8} {issue.severity:>3}",
                style=sev_style,
            )
            conf_text = Text(f"{issue.confidence:.0%}", style="dim")
            table.add_row(sev_text, issue.type, issue.summary, conf_text)

        out.print(table)

        # Recommendations
        recs = [i for i in sorted_issues if i.recommendation]
        if recs:
            out.print()
            out.print("[bold]Recommendations:[/bold]")
            for issue in recs:
                style = _severity_color(issue.severity)
                out.print(f"  [{style}]{issue.type}[/{style}]: {issue.recommendation}")

        out.print()

    def _render(self) -> Panel:
        """Build the live display panel."""
        with self._lock:
            spans = list(self._spans)
            issues = list(self._issues)
            span_count = self._span_count
            trace_count = self._trace_count

        # Build spans section
        lines: list[str] = []
        lines.append(
            f"[bold]Spans:[/bold] {span_count}  "
            f"[bold]Traces:[/bold] {trace_count}  "
            f"[bold]Issues:[/bold] {len(issues)}"
        )
        lines.append("")

        if spans:
            lines.append("[dim]Recent spans:[/dim]")
            for s in spans[-8:]:
                status_color = "green" if s.status == "ok" else "yellow"
                lines.append(
                    f"  [{status_color}]{s.status:>5}[/{status_color}] "
                    f"{s.name} [dim]({s.kind})[/dim]"
                )

        if issues:
            lines.append("")
            lines.append("[dim]Detected issues:[/dim]")
            for issue in issues[-5:]:
                sev_style = _severity_color(issue.severity)
                lines.append(
                    f"  [{sev_style}]{_severity_label(issue.severity)}[/{sev_style}] "
                    f"{issue.type}: {issue.summary}"
                )

        content = "\n".join(lines)
        return Panel(
            content,
            title="[bold]Pisama Watch[/bold]",
            border_style="blue",
        )


# ---------------------------------------------------------------------------
# Comparison display for `pisama replay --compare`
# ---------------------------------------------------------------------------


def display_comparison(
    trace_a_id: str,
    trace_b_id: str,
    fixed: list[str],
    improved: list[str],
    regressed: list[str],
    unchanged: list[str],
) -> None:
    """Print a formatted comparison between two trace analyses."""
    out = Console()
    out.print()

    # Header
    if regressed:
        header_style = "red bold"
        header_text = f"{len(regressed)} regression(s) detected"
    elif fixed:
        header_style = "green bold"
        header_text = f"{len(fixed)} issue(s) fixed"
    else:
        header_style = "dim"
        header_text = "No changes"

    out.print(
        Panel(
            Text(header_text, style=header_style),
            title="[bold]Pisama Comparison[/bold]",
            subtitle=f"{trace_a_id[:12]} vs {trace_b_id[:12]}",
            border_style="dim",
        )
    )

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Status", width=12)
    table.add_column("Detector", ratio=1)

    for name in fixed:
        table.add_row(Text("FIXED", style="green bold"), name)
    for name in improved:
        table.add_row(Text("IMPROVED", style="cyan"), name)
    for name in regressed:
        table.add_row(Text("REGRESSED", style="red bold"), name)
    for name in unchanged:
        table.add_row(Text("unchanged", style="dim"), name)

    out.print(table)
    out.print()


# ---------------------------------------------------------------------------
# Smoke test display for `pisama smoke-test`
# ---------------------------------------------------------------------------


def display_smoke_results(
    total_traces: int,
    traces_with_issues: int,
    per_detector_stats: dict[str, dict[str, Any]],
    critical_traces: list[str],
) -> None:
    """Print formatted smoke test results."""
    out = Console()
    out.print()

    pct = (traces_with_issues / total_traces * 100) if total_traces else 0
    if critical_traces:
        header_style = "red bold"
        header_text = (
            f"{traces_with_issues}/{total_traces} traces with issues "
            f"({pct:.0f}%) -- {len(critical_traces)} critical"
        )
    elif traces_with_issues:
        header_style = "yellow bold"
        header_text = f"{traces_with_issues}/{total_traces} traces with issues ({pct:.0f}%)"
    else:
        header_style = "green bold"
        header_text = f"All {total_traces} traces clean"

    out.print(
        Panel(
            Text(header_text, style=header_style),
            title="[bold]Pisama Smoke Test[/bold]",
            border_style="dim",
        )
    )

    if per_detector_stats:
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Detector", width=20)
        table.add_column("Hits", width=6, justify="right")
        table.add_column("Avg Severity", width=12, justify="right")
        table.add_column("Max Severity", width=12, justify="right")

        for name, stats in sorted(per_detector_stats.items(), key=lambda x: -x[1].get("count", 0)):
            count = stats.get("count", 0)
            avg_sev = stats.get("avg_severity", 0)
            max_sev = stats.get("max_severity", 0)
            sev_style = _severity_color(max_sev)
            table.add_row(
                name,
                str(count),
                f"{avg_sev:.0f}",
                Text(str(max_sev), style=sev_style),
            )

        out.print(table)

    if critical_traces:
        out.print()
        out.print("[bold]Critical traces:[/bold]")
        for tid in critical_traces[:10]:
            out.print(f"  [red]{tid}[/red]")
        if len(critical_traces) > 10:
            out.print(f"  [dim]... and {len(critical_traces) - 10} more[/dim]")

    out.print()
