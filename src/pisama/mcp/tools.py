"""MCP tool definitions for the local Pisama server.

Each tool is a ``mcp.types.Tool`` with a name, description, and JSON Schema
for its input parameters.  The server registers these via ``@server.list_tools``.
"""

from __future__ import annotations

# Lazy import -- the ``mcp`` package is an optional dependency.
# Callers must ensure it is installed before importing this module.
from mcp.types import Tool

TOOLS: list[Tool] = [
    Tool(
        name="pisama_analyze",
        description=(
            "Run all Pisama detectors on a trace and return a full analysis. "
            "Accepts a trace object, a JSON string, or a path to a .json/.jsonl file."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "trace": {
                    "description": (
                        "The trace to analyze. Can be a JSON object with "
                        "'trace_id' and 'spans', a JSON string, or a file path "
                        "ending in .json/.jsonl."
                    ),
                },
            },
            "required": ["trace"],
        },
    ),
    Tool(
        name="pisama_detect",
        description=(
            "Run a single named detector on a trace. Use this when you know "
            "which failure type to check for (e.g. 'loop', 'hallucination')."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "detector": {
                    "type": "string",
                    "description": (
                        "Name of the detector to run, e.g. 'loop', 'hallucination', "
                        "'injection', 'corruption', 'persona_drift', etc."
                    ),
                },
                "trace": {
                    "description": (
                        "The trace to analyze. Can be a JSON object, a JSON string, "
                        "or a file path ending in .json/.jsonl."
                    ),
                },
            },
            "required": ["detector", "trace"],
        },
    ),
    Tool(
        name="pisama_status",
        description=(
            "Return a summary of the current MCP session: total analyses run, "
            "issues found by type, severity distribution, and recent results."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="pisama_explain",
        description=(
            "Return documentation about a Pisama failure type including "
            "description, common causes, and detection methodology. "
            "Use this to understand what a detection means."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "failure_type": {
                    "type": "string",
                    "description": (
                        "Name of a Pisama detector / failure type, e.g. 'loop', "
                        "'hallucination', 'injection', 'coordination', 'derailment'. "
                        "The authoritative set is the live detector registry; an "
                        "unknown name returns the complete 'available' list."
                    ),
                },
            },
            "required": ["failure_type"],
        },
    ),
]


def get_tool_map() -> dict[str, Tool]:
    """Return a name -> Tool mapping for quick lookup."""
    return {t.name: t for t in TOOLS}
