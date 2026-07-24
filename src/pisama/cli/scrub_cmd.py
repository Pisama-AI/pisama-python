"""pisama scrub <path> command.

Writes a PII-scrubbed copy of a trace file to --out (default: <name>.scrubbed.<ext>).
Regex-based; defense-in-depth, not a compliance tool.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console

from pisama.scrubber import format_report, scrub_file

console = Console(stderr=True)


@click.command("scrub")
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "--out",
    "out_path",
    type=click.Path(),
    default=None,
    help="Output path. Defaults to <name>.scrubbed.<ext> next to the input.",
)
def scrub_cmd(path: str, out_path: str | None) -> None:
    """Write a PII-scrubbed copy of a trace file.

    PATH is a .json or .jsonl trace file. The scrubbed copy replaces emails,
    phone numbers, SSNs, card numbers, API tokens, JWTs, IPs, and values of
    sensitive keys (authorization, api_key, password, ...) with placeholders.
    """
    src = Path(path)
    if src.suffix not in (".json", ".jsonl"):
        console.print(f"[red]Error:[/red] Expected a .json or .jsonl file, got {src.suffix!r}")
        sys.exit(1)

    if out_path is None:
        dst = src.with_suffix(f".scrubbed{src.suffix}")
    else:
        dst = Path(out_path)

    report = scrub_file(src, dst)
    console.print(f"[green]Wrote[/green] {dst}")
    console.print(format_report(report))
