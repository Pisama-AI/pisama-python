"""CLI command to start the local Pisama MCP server."""

from __future__ import annotations

import click


@click.command("mcp-server")
@click.option(
    "--log-level",
    default="WARNING",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Logging level (default: WARNING -- keeps stdio clean for MCP transport).",
)
def mcp_server_cmd(log_level: str) -> None:
    """Start the local Pisama MCP server (stdio transport).

    Runs the full local pisama-core detector suite.  No API key, no backend
    connection required.  Configure your MCP host to run:

        pisama mcp-server
    """
    try:
        from pisama.mcp.server import run_local_server
    except ImportError as exc:
        raise click.ClickException(
            f"The 'mcp' extra is required: pip install pisama[mcp]\n({exc})"
        ) from exc

    run_local_server(log_level=log_level)
