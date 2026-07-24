"""Local Pisama MCP server -- runs detection via pisama-core directly.

No API key, no backend, no network required.  Works inside Cursor,
Claude Desktop, Windsurf, or any MCP-compatible host with zero config.

Usage (stdio transport, as configured in an MCP host):
    python -m pisama.mcp.server

Or via the CLI:
    pisama mcp-server
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from pisama.mcp.descriptions import get_failure_description, list_failure_types
from pisama.mcp.session import SessionStore
from pisama.mcp.tools import TOOLS

logger = logging.getLogger("pisama-mcp-local")


# ---------------------------------------------------------------------------
# Local analyzer
# ---------------------------------------------------------------------------


class LocalAnalyzer:
    """Runs detection using pisama-core directly -- no HTTP, no API key."""

    def __init__(self) -> None:
        # Trigger detector auto-registration
        import pisama_core.detection.detectors  # noqa: F401
        from pisama_core.detection.orchestrator import DetectionOrchestrator

        self.orchestrator = DetectionOrchestrator()
        self.session = SessionStore()

    # -- public API ---------------------------------------------------------

    async def analyze_trace(self, trace_data: Any) -> dict[str, Any]:
        """Run all detectors on a trace.

        Args:
            trace_data: dict, JSON string, or file path.

        Returns:
            Serialized AnalysisResult dict.
        """
        trace = self._load_trace(trace_data)
        result = await self.orchestrator.analyze(trace)
        result_dict = result.to_dict()
        self.session.add(result_dict)
        return result_dict

    async def run_detector(self, detector_name: str, trace_data: Any) -> dict[str, Any]:
        """Run a single named detector.

        Args:
            detector_name: Registry name of the detector (e.g. 'loop').
            trace_data: dict, JSON string, or file path.

        Returns:
            Serialized DetectionResult dict.
        """
        detector = self.orchestrator.registry.get(detector_name)
        if detector is None:
            available = sorted(d.name for d in self.orchestrator.registry.get_all())
            raise ValueError(
                f"Unknown detector: {detector_name!r}. Available: {', '.join(available)}"
            )

        trace = self._load_trace(trace_data)
        result = await detector.run(trace)
        result_dict = result.to_dict()

        # Wrap in analysis-like envelope so session tracking is consistent
        envelope = {
            "trace_id": trace.trace_id,
            "detection_results": [result_dict],
            "total_detectors_run": 1,
            "issues_detected": 1 if result.detected else 0,
            "max_severity": result.severity,
        }
        self.session.add(envelope)
        return result_dict

    async def get_status(self) -> dict[str, Any]:
        """Return session summary plus detector inventory."""
        summary = self.session.summary()
        summary["detectors"] = self.orchestrator.get_detector_status()
        summary["recent"] = self.session.recent(5)
        return summary

    async def explain_failure(self, failure_type: str) -> dict[str, Any]:
        """Return documentation about a failure type."""
        desc = get_failure_description(failure_type)
        if desc is None:
            return {
                "error": f"Unknown failure type: {failure_type!r}",
                "available": list_failure_types(),
            }
        return desc

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _load_trace(trace_data: Any) -> Any:
        """Convert raw input to a Trace object.

        Accepts dict, JSON string, or file path.
        """
        from pisama._loader import load_trace

        return load_trace(trace_data)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_local_server() -> Server:
    """Create the local MCP server with all tools wired up."""

    server = Server("pisama-local")
    analyzer = LocalAnalyzer()

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
        try:
            result = await _dispatch(analyzer, name, arguments or {})
            text = (
                json.dumps(result, indent=2, default=str)
                if isinstance(result, (dict, list))
                else str(result)
            )
            return [TextContent(type="text", text=text)]
        except (ValueError, TypeError, FileNotFoundError) as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}, indent=2))]
        except Exception as exc:
            logger.exception("Unexpected error in tool %s", name)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": "Internal MCP server error", "detail": str(exc)},
                        indent=2,
                    ),
                )
            ]

    return server


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


async def _dispatch(
    analyzer: LocalAnalyzer,
    name: str,
    args: Dict[str, Any],
) -> Any:
    """Route a tool call to the correct LocalAnalyzer method."""

    if name == "pisama_analyze":
        trace = args.get("trace")
        if trace is None:
            raise ValueError("'trace' is required")
        return await analyzer.analyze_trace(trace)

    if name == "pisama_detect":
        detector = args.get("detector")
        trace = args.get("trace")
        if not detector:
            raise ValueError("'detector' is required")
        if trace is None:
            raise ValueError("'trace' is required")
        return await analyzer.run_detector(detector, trace)

    if name == "pisama_status":
        return await analyzer.get_status()

    if name == "pisama_explain":
        failure_type = args.get("failure_type")
        if not failure_type:
            raise ValueError("'failure_type' is required")
        return await analyzer.explain_failure(failure_type)

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_local_server(*, log_level: str = "WARNING") -> None:
    """Start the local MCP server on stdio (blocking)."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    server = create_local_server()
    logger.info("Starting local Pisama MCP server (no backend required)")

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Local Pisama MCP server shut down")


if __name__ == "__main__":
    run_local_server()
