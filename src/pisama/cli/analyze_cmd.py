"""pisama analyze <path> command."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import click
from rich.console import Console

from pisama._analyze import analyze
from pisama.output.terminal import display_analysis_result
from pisama.scrubber import format_report, scrub_file

console = Console(stderr=True)


@click.command("analyze")
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "--min-severity",
    type=int,
    default=0,
    help="Only show issues at or above this severity (0-100).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output raw JSON instead of formatted table.",
)
@click.option(
    "--scrub",
    is_flag=True,
    default=False,
    help=(
        "Scrub PII (emails, tokens, IPs, ...) in-memory before analysis. "
        "The on-disk file is not modified."
    ),
)
def analyze_cmd(path: str, min_severity: int, output_json: bool, scrub: bool) -> None:
    """Analyze a trace file for multi-agent failures.

    PATH is a .json or .jsonl trace file.
    """
    file_path = Path(path)
    if file_path.suffix not in (".json", ".jsonl"):
        console.print(
            f"[red]Error:[/red] Expected a .json or .jsonl file, got {file_path.suffix!r}"
        )
        sys.exit(1)

    analyze_path = str(file_path)
    tmp_scrubbed: Path | None = None
    if scrub:
        tmp_scrubbed = Path(tempfile.NamedTemporaryFile(suffix=file_path.suffix, delete=False).name)
        report = scrub_file(file_path, tmp_scrubbed)
        console.print(f"[dim]{format_report(report)}[/dim]")
        analyze_path = str(tmp_scrubbed)

    try:
        result = analyze(analyze_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    finally:
        if tmp_scrubbed is not None and tmp_scrubbed.exists():
            tmp_scrubbed.unlink()

    # Filter by severity
    if min_severity > 0:
        result.issues = [i for i in result.issues if i.severity >= min_severity]

    if output_json:
        _print_json(result)
    else:
        display_analysis_result(result)

    # Exit code: 1 if critical issues found, 0 otherwise
    if result.critical_issues:
        sys.exit(1)


def _print_json(result: object) -> None:
    """Print result as JSON to stdout."""
    from dataclasses import asdict

    click.echo(json.dumps(asdict(result), indent=2, default=str))  # type: ignore[call-overload]
