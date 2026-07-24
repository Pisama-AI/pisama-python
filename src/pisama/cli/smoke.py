"""pisama smoke-test -- batch detection on recent traces."""

from __future__ import annotations

import asyncio
import json
import sys

import click
from rich.console import Console

from pisama.cli.main import main

console = Console(stderr=True)


@main.command("smoke-test")
@click.option(
    "--last",
    default=50,
    type=int,
    help="Number of recent traces to analyze.",
)
@click.option(
    "--framework",
    default=None,
    help="Filter by framework (claude_code, langgraph, etc.).",
)
@click.option(
    "--detectors",
    default=None,
    help="Comma-separated list of detectors to run.",
)
@click.option(
    "--fail-on-regression",
    is_flag=True,
    default=False,
    help="Exit with code 1 if any critical issues found.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON.",
)
def smoke_test(
    last: int,
    framework: str | None,
    detectors: str | None,
    fail_on_regression: bool,
    as_json: bool,
) -> None:
    """Run detection on recent traces and report aggregate stats.

    Loads traces from local storage (SQLite and JSONL in ~/.pisama/ and
    ~/.claude/pisama/) and runs all detectors against them.

    Useful for:
    - Verifying no regressions after agent code changes
    - Getting a baseline of agent health
    - CI/CD smoke tests
    """
    asyncio.run(_smoke_async(last, framework, detectors, fail_on_regression, as_json))


async def _smoke_async(
    last: int,
    framework: str | None,
    detectors: str | None,
    fail_on_regression: bool,
    as_json: bool,
) -> None:
    """Async implementation of smoke-test command."""
    import os

    from pisama.replay.smoke_runner import SmokeRunner
    from pisama.replay.trace_fetcher import TraceFetcher

    api_key = os.environ.get("PISAMA_API_KEY")
    # PISAMA_BASE_URL is the canonical name (matches the SDK Client); the older
    # PISAMA_API_URL stays a fallback so existing setups keep working.
    api_url = os.environ.get("PISAMA_BASE_URL") or os.environ.get("PISAMA_API_URL")

    fetcher = TraceFetcher(api_key=api_key, api_url=api_url)

    console.print(f"[dim]Loading up to {last} recent traces...[/dim]")
    traces = await fetcher.get_recent(n=last, framework=framework)

    if not traces:
        console.print("[yellow]No traces found.[/yellow]")
        console.print(
            "[dim]Traces are stored in ~/.pisama/traces/ and ~/.claude/pisama/traces/[/dim]"
        )
        sys.exit(0)

    console.print(f"[dim]Running detection on {len(traces)} trace(s)...[/dim]")

    detector_list = None
    if detectors:
        detector_list = [d.strip() for d in detectors.split(",")]

    runner = SmokeRunner()
    result = await runner.run(traces, detectors=detector_list)

    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        from pisama.output.terminal import display_smoke_results

        display_smoke_results(
            total_traces=result.total_traces,
            traces_with_issues=result.traces_with_issues,
            per_detector_stats={k: v.to_dict() for k, v in result.per_detector_stats.items()},
            critical_traces=result.critical_traces,
        )

    if result.errors:
        console.print(f"[yellow]{len(result.errors)} error(s) during analysis[/yellow]")
        for err in result.errors[:5]:
            console.print(f"  [dim]{err}[/dim]")

    if fail_on_regression and result.critical_traces:
        sys.exit(1)
