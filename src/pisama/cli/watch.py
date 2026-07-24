"""pisama watch -- observe a running agent process for failures."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

from pisama.cli.main import main

console = Console(stderr=True)


@main.command()
@click.argument("command", nargs=-1, required=True)
@click.option(
    "--min-severity",
    default=0,
    type=int,
    help="Only show issues at or above this severity (0-100).",
)
def watch(command: tuple[str, ...], min_severity: int) -> None:
    """Watch a Python process for agent failures.

    Launches COMMAND as a subprocess with automatic OTEL instrumentation,
    receives spans via a local OTLP collector, and runs Pisama detection
    in real time.

    Requires pisama-auto to be installed in the target environment for
    auto-instrumentation. Without it the subprocess runs normally but
    no spans are collected.

    Usage:

      pisama watch python my_agent.py

      pisama watch -- python -m my_module --flag
    """
    import asyncio

    from pisama._analyze import async_analyze
    from pisama.collector.local_collector import LocalCollector
    from pisama.output.terminal import WatchDisplay

    # Locate the bootstrap script
    bootstrap_path = Path(__file__).parent.parent / "collector" / "_bootstrap.py"
    if not bootstrap_path.exists():
        console.print(f"[red]Error:[/red] Could not find bootstrap script at {bootstrap_path}")
        sys.exit(1)

    # Set up display
    display = WatchDisplay(min_severity=min_severity)

    def on_span(span: object) -> None:
        """Called for each span received by the collector."""
        display.add_span(
            span_name=getattr(span, "name", "?"),
            kind=str(getattr(span, "kind", "?")),
            status=str(getattr(span, "status", "?")),
            trace_id=getattr(span, "trace_id", "") or "",
        )

    def on_trace(trace: object) -> None:
        """Called when a trace gets a new span."""
        display.set_trace_count(len(collector.get_traces()))

    # Start local collector
    collector = LocalCollector(
        on_span=on_span,
        on_trace_complete=on_trace,
    )
    port = collector.start()

    console.print(f"[bold blue]Pisama Watch[/bold blue] collector listening on localhost:{port}")

    # Build subprocess environment
    env = os.environ.copy()
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{port}"
    env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/json"

    # Set PYTHONSTARTUP for auto-instrumentation, but preserve any existing
    existing_startup = env.get("PYTHONSTARTUP", "")
    env["PYTHONSTARTUP"] = str(bootstrap_path)
    if existing_startup:
        env["_PISAMA_ORIGINAL_PYTHONSTARTUP"] = existing_startup

    # Check if pisama-auto is importable
    try:
        import importlib.util

        if importlib.util.find_spec("pisama_auto") is None:
            console.print(
                "[yellow]Warning:[/yellow] pisama-auto is not installed. "
                "Auto-instrumentation will not work.\n"
                "Install with: [bold]pip install pisama[auto][/bold]"
            )
    except Exception:
        pass

    console.print(f"[dim]Running: {' '.join(command)}[/dim]")
    console.print()

    # Start live display
    display.start()

    # Launch user command
    try:
        proc = subprocess.Popen(
            list(command),
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        proc.wait()
    except FileNotFoundError:
        display.stop()
        console.print(f"[red]Error:[/red] Command not found: {command[0]}")
        collector.stop()
        sys.exit(1)
    except KeyboardInterrupt:
        display.stop()
        console.print("\n[yellow]Interrupted[/yellow]")
        proc.terminate()
        proc.wait(timeout=5)
    finally:
        display.stop()

    # Run detection on all collected traces
    traces = collector.get_traces()
    collector.stop()

    if traces:
        console.print(f"\n[dim]Running detection on {len(traces)} trace(s)...[/dim]")
        for trace in traces:
            try:
                result = asyncio.run(async_analyze(trace))
                for issue in result.issues:
                    display.add_issue(issue)
            except Exception as exc:
                console.print(
                    f"[yellow]Warning:[/yellow] Detection failed for "
                    f"trace {trace.trace_id[:12]}: {exc}"
                )

    # Show summary
    display.show_summary()

    # Exit with code from subprocess (or 1 if critical issues)
    exit_code = proc.returncode if proc.returncode else 0
    critical = [i for i in display._issues if i.severity >= 60]
    if critical and exit_code == 0:
        exit_code = 1
    sys.exit(exit_code)
