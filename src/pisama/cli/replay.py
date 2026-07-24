"""pisama replay -- re-run detection on a stored trace."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict

import click
from rich.console import Console

from pisama.cli.main import main

console = Console(stderr=True)


@main.command()
@click.argument("trace_id")
@click.option(
    "--compare",
    default=None,
    help="Compare against another trace ID.",
)
@click.option(
    "--detectors",
    default=None,
    help="Comma-separated list of detectors to run.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON.",
)
def replay(
    trace_id: str,
    compare: str | None,
    detectors: str | None,
    as_json: bool,
) -> None:
    """Re-run detection on a stored trace.

    TRACE_ID can be a full trace ID or a prefix. Pisama searches local
    storage (SQLite and JSONL files in ~/.pisama/ and ~/.claude/pisama/)
    and the remote API if configured.

    Use --compare to diff detection results between two traces:

      pisama replay abc123 --compare def456
    """
    asyncio.run(_replay_async(trace_id, compare, detectors, as_json))


async def _replay_async(
    trace_id: str,
    compare_id: str | None,
    detectors: str | None,
    as_json: bool,
) -> None:
    """Async implementation of replay command."""
    import os

    from pisama._analyze import async_analyze
    from pisama.replay.trace_fetcher import TraceFetcher

    api_key = os.environ.get("PISAMA_API_KEY")
    # PISAMA_BASE_URL is the canonical name (matches the SDK Client); the older
    # PISAMA_API_URL stays a fallback so existing setups keep working.
    api_url = os.environ.get("PISAMA_BASE_URL") or os.environ.get("PISAMA_API_URL")

    fetcher = TraceFetcher(api_key=api_key, api_url=api_url)

    # Fetch main trace
    console.print(f"[dim]Fetching trace {trace_id}...[/dim]")
    trace = await fetcher.get_trace(trace_id)
    if trace is None:
        console.print(
            f"[red]Error:[/red] Trace not found: {trace_id}\n"
            "[dim]Searched local SQLite and JSONL files.[/dim]"
        )
        sys.exit(1)

    # Run detection
    console.print(f"[dim]Running detection on {trace.span_count} span(s)...[/dim]")
    result_a = await async_analyze(trace)

    # Filter by requested detectors
    if detectors:
        detector_list = [d.strip() for d in detectors.split(",")]
        result_a.issues = [i for i in result_a.issues if i.type in detector_list]

    if compare_id is None:
        # Simple replay -- display result
        if as_json:
            click.echo(json.dumps(asdict(result_a), indent=2, default=str))
        else:
            from pisama.output.terminal import display_analysis_result

            display_analysis_result(result_a)

        if result_a.critical_issues:
            sys.exit(1)
        return

    # Comparison mode
    console.print(f"[dim]Fetching comparison trace {compare_id}...[/dim]")
    trace_b = await fetcher.get_trace(compare_id)
    if trace_b is None:
        console.print(f"[red]Error:[/red] Comparison trace not found: {compare_id}")
        sys.exit(1)

    console.print(
        f"[dim]Running detection on comparison trace ({trace_b.span_count} spans)...[/dim]"
    )
    result_b = await async_analyze(trace_b)

    if detectors:
        detector_list = [d.strip() for d in detectors.split(",")]
        result_b.issues = [i for i in result_b.issues if i.type in detector_list]

    # Compare
    from pisama.replay.comparator import ComparisonResult

    comparison = ComparisonResult.compare(result_a, result_b)

    if as_json:
        click.echo(json.dumps(asdict(comparison), indent=2, default=str))
    else:
        from pisama.output.terminal import display_comparison

        display_comparison(
            trace_a_id=comparison.trace_a_id,
            trace_b_id=comparison.trace_b_id,
            fixed=comparison.fixed,
            improved=comparison.improved,
            regressed=comparison.regressed,
            unchanged=comparison.unchanged,
        )

    if comparison.has_regressions:
        sys.exit(1)
