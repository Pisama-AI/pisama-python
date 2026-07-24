"""Smoke tests for the Pisama MCP server — customer perspective.

Starts ``pisama mcp-server`` as a subprocess over stdio and exercises
every public tool.  Verifies the 4-tool surface and confirms that
paid-tier tools (suggest_fix) are NOT exposed.

Run:
    pytest packages/pisama/tests/test_mcp_smoke.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MCP_CMD = [str(Path(sys.executable).parent / "pisama"), "mcp-server"]

# JSON-RPC helpers
_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


def _jsonrpc(method: str, params: dict[str, Any] | None = None) -> bytes:
    """Build a JSON-RPC 2.0 request with MCP content-length framing."""
    msg = {"jsonrpc": "2.0", "id": _next_id(), "method": method}
    if params is not None:
        msg["params"] = params
    body = json.dumps(msg)
    return body.encode() + b"\n"


def _read_responses(proc: subprocess.Popen, timeout: float = 10.0) -> list[dict]:
    """Read all available JSON-RPC responses from stdout."""
    import select

    responses = []
    deadline = time.monotonic() + timeout
    buf = b""

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.5))
        if not ready:
            if responses:
                break
            continue

        chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk

        # Try to parse complete JSON objects from buffer
        while buf:
            buf = buf.lstrip()
            if not buf:
                break
            try:
                obj = json.loads(buf)
                responses.append(obj)
                break
            except json.JSONDecodeError:
                # Try to find a complete JSON object
                depth = 0
                in_string = False
                escape = False
                end = -1
                for i, c in enumerate(buf):
                    if isinstance(c, int):
                        c = chr(c)
                    if escape:
                        escape = False
                        continue
                    if c == "\\":
                        escape = True
                        continue
                    if c == '"' and not escape:
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                if end > 0:
                    try:
                        obj = json.loads(buf[:end])
                        responses.append(obj)
                        buf = buf[end:]
                        continue
                    except json.JSONDecodeError:
                        break
                else:
                    break

    return responses


# ---------------------------------------------------------------------------
# Fixture: MCP server subprocess
# ---------------------------------------------------------------------------


class McpClient:
    """Thin wrapper around the MCP server subprocess."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            MCP_CMD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Send initialize handshake
        self._send(
            _jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pisama-smoke-test", "version": "1.0.0"},
                },
            )
        )
        init_resp = self._recv(timeout=10.0)
        assert len(init_resp) > 0, "No response to initialize"

        # Send initialized notification
        notif = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
            ).encode()
            + b"\n"
        )
        self.proc.stdin.write(notif)
        self.proc.stdin.flush()
        time.sleep(0.3)

    def _send(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _recv(self, timeout: float = 10.0) -> list[dict]:
        return _read_responses(self.proc, timeout=timeout)

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> dict:
        """Send a JSON-RPC call and return the response."""
        self._send(_jsonrpc(method, params))
        responses = self._recv(timeout=timeout)
        # Return the last response with a result or error
        for resp in reversed(responses):
            if "result" in resp or "error" in resp:
                return resp
        if responses:
            return responses[-1]
        raise TimeoutError(f"No response for {method} within {timeout}s")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)


@pytest.fixture(scope="module")
def mcp() -> McpClient:
    client = McpClient()
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

LOOP_TRACE = {
    "trace_id": "smoke-test-001",
    "spans": [
        {
            "span_id": f"s{i}",
            "name": "call_summarize",
            "kind": "tool",
            "start_time": f"2026-01-01T10:00:0{i}Z",
            "end_time": f"2026-01-01T10:00:0{i + 1}Z",
            "input_data": "Summarize doc-42",
            "output_data": "Error: API rate limit exceeded",
        }
        # Five repetitions cross the full-analysis reporting threshold. Four
        # is intentionally only a low-severity warning in pisama-core and is
        # filtered from the default all-detector report.
        for i in range(5)
    ],
}


class TestToolListing:
    """Verify the public tool surface."""

    def test_lists_exactly_four_tools(self, mcp: McpClient) -> None:
        resp = mcp.call("tools/list")
        tools = resp["result"]["tools"]
        names = sorted(t["name"] for t in tools)
        assert names == [
            "pisama_analyze",
            "pisama_detect",
            "pisama_explain",
            "pisama_status",
        ]

    def test_suggest_fix_not_exposed(self, mcp: McpClient) -> None:
        """Paid-tier tool must not appear in OSS MCP."""
        resp = mcp.call("tools/list")
        names = [t["name"] for t in resp["result"]["tools"]]
        assert "pisama_suggest_fix" not in names


class TestPisamaStatus:
    def test_returns_detector_inventory(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_status",
                "arguments": {},
            },
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["detectors"]["total"] > 0
        assert isinstance(result["detectors"]["detectors"], list)

    def test_severity_distribution_starts_empty(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_status",
                "arguments": {},
            },
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        dist = result["severity_distribution"]
        assert all(v >= 0 for v in dist.values())


class TestPisamaExplain:
    def test_known_failure_type(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_explain",
                "arguments": {"failure_type": "loop"},
            },
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        assert "name" in result
        assert "common_causes" in result
        assert isinstance(result["common_causes"], list)

    def test_unknown_failure_type(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_explain",
                "arguments": {"failure_type": "nonexistent_xyz"},
            },
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        assert "error" in result
        assert "available" in result


class TestPisamaDetect:
    def test_loop_detected(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_detect",
                "arguments": {"detector": "loop", "trace": LOOP_TRACE},
            },
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["detected"] is True
        assert result["detector_name"] == "loop"
        assert result["severity"] > 0
        assert len(result["evidence"]) > 0

    def test_unknown_detector(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_detect",
                "arguments": {"detector": "nonexistent_xyz", "trace": LOOP_TRACE},
            },
        )
        # Should return an error (either in result or as isError)
        content = resp["result"]["content"][0]["text"]
        assert "unknown" in content.lower() or "error" in content.lower()


class TestPisamaAnalyze:
    def test_runs_all_detectors(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_analyze",
                "arguments": {"trace": LOOP_TRACE},
            },
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["total_detectors_run"] > 10
        assert isinstance(result["detection_results"], list)

    def test_finds_loop_in_full_analysis(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_analyze",
                "arguments": {"trace": LOOP_TRACE},
            },
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        loop_results = [r for r in result["detection_results"] if r["detector_name"] == "loop"]
        assert len(loop_results) == 1
        assert loop_results[0]["detected"] is True

    def test_issues_detected_count(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_analyze",
                "arguments": {"trace": LOOP_TRACE},
            },
        )
        result = json.loads(resp["result"]["content"][0]["text"])
        assert result["issues_detected"] >= 1


class TestSuggestFixBlocked:
    """Ensure calling the removed tool returns an error."""

    def test_suggest_fix_returns_error(self, mcp: McpClient) -> None:
        resp = mcp.call(
            "tools/call",
            {
                "name": "pisama_suggest_fix",
                "arguments": {
                    "detection": {
                        "detector_name": "loop",
                        "detected": True,
                        "severity": 25,
                    },
                },
            },
        )
        content = resp["result"]["content"][0]["text"]
        assert "error" in content.lower() or resp["result"].get("isError")
