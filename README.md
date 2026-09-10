# Pisama

**Find and fix failures in AI agent systems. No LLM calls required.**

[![PyPI](https://img.shields.io/pypi/v/pisama?color=blue)](https://pypi.org/project/pisama/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Pisama ships heuristic detectors that apply across frameworks including n8n, LangGraph, Dify and OpenClaw, with per-platform gating (for example `coordination` runs only on multi-agent platforms). They run locally with zero LLM cost on the heuristic tier.

## Install

```bash
pip install pisama
```

## Hosted first diagnosis

The basic Usage example below runs locally. A founder-issued Pisama Cloud
API key is for the hosted service, not a requirement for offline `analyze()`.
If you arrived here after redeeming an invitation, use this section first.

1. Save the one-time API key in your secret manager. Invitation redemption does
   not create dashboard access. Never paste the key into an issue, shared trace,
   source file, or chat.
2. Export one representative run as OpenTelemetry JSON (`resourceSpans`) and
   remove credentials and data you are not authorized to upload. Hosted ingestion
   stores the run and counts toward your agreed usage allowance.
3. Save the following as `hosted_first_diagnosis.py` and run
   `python3 hosted_first_diagnosis.py`. It uses only Python's standard library,
   prompts for the key without echoing it, and never prints the bearer token.
   It sends your selected file to Pisama Cloud; it is not an offline example.

```python
import getpass
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://api.pisama.ai/api/v1"


def post(path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as error:
        # Do not dump request headers or response bodies containing private data.
        raise SystemExit(f"{path}: HTTP {error.code}; stop and check access/format.")
    except URLError:
        raise SystemExit(f"{path}: network failure; do not blindly retry ingestion.")


trace_path = Path(input("Path to your redacted OTLP JSON file: ").strip())
trace = json.loads(trace_path.read_text(encoding="utf-8"))
if not isinstance(trace, dict) or not isinstance(trace.get("resourceSpans"), list):
    raise SystemExit("Expected an OTLP JSON object containing resourceSpans.")

key = getpass.getpass("One-time Pisama Cloud API key: ").strip()
auth = post("/auth/token", {"api_key": key, "scope": "full"})
del key
token = auth.get("access_token")
if not isinstance(token, str) or not token:
    raise SystemExit("No bearer token returned; stop and contact Pisama.")

ingest = post("/traces/ingest", trace, token)
print("Ingestion:", {name: ingest.get(name) for name in ("accepted", "rejected", "traces")})
if not ingest.get("accepted") or ingest.get("rejected"):
    raise SystemExit("Ingestion was empty or partial; inspect the export before continuing.")

result = post("/diagnose/why-failed", {
    "content": json.dumps(trace), "format": "otel", "include_fixes": False,
}, token)
del token
print("Failure signals:", result.get("failure_count"))
for finding in result.get("all_detections", []):
    print(json.dumps({name: finding.get(name) for name in (
        "category", "mistake_agent", "affected_spans", "evidence", "suggested_fix",
    )}, indent=2))
```

Ingestion accepts work asynchronously. The diagnosis request above analyzes the
submitted content; it does not prove that background analysis of the stored run
has completed. Review the reported agent, spans, evidence and next action against
your run. Zero signals is not proof of success, and a suggested fix is not proof
that the task will work after a change. `include_fixes=False` avoids requesting
optional generated fixes; it does not promise that every hosted detector is free
of model calls or usage charges. Hosted scope, retention and pricing are defined
in your founder-led agreement, not this package's MIT license.

For 401, obtain a fresh token using an active key. For 403, check your invitation,
scope and entitlement with Pisama. For 429, stop and check quota or rate limits.
Do not repeatedly upload the same run to work around an error. Contact
[team@pisama.ai](mailto:team@pisama.ai) if the flow cannot produce an inspectable
finding. Do not send the key or an unredacted trace by email.

## Usage

Local `analyze()` accepts a nonempty Pisama native trace (`spans`), an ATIF
trajectory, or native span JSONL. OTLP `resourceSpans` exports are not a local
input format; use the hosted workflow above for those exports. Unsupported or
empty inputs raise an error rather than reporting a clean analysis.
JSONL may contain native span rows, or exactly one native trace envelope; mixing
envelopes with other rows is rejected rather than dropping later evidence.

```python
from pisama import analyze

result = analyze("trace.json")  # also accepts dicts and JSON strings

for issue in result.issues:
    print(f"[{issue.type}] {issue.summary} (severity: {issue.severity})")
    print(f"  Fix: {issue.recommendation}")
```

## CLI

`AnalyzeResult.detector_assessments` and JSON CLI output preserve explicit
detector coverage. `abstained` is not a passed check; older core versions that
provide no assessment are marked `unknown`. Terminal output separates explicit
contract passes, abstentions, errors and unspecified coverage. Detector execution
counts are not counts of validated requirements. No findings is not proof of task
success. Coverage metadata does not turn heuristic confidence into calibration.

```bash
pisama analyze trace.json          # Analyze a trace
pisama watch python my_agent.py    # Watch a live agent (pip install "pisama[auto]")
pisama replay <trace-id>           # Re-run detection on stored traces
pisama smoke-test --last 50        # Batch test recent traces
pisama detectors                   # List all core detectors
pisama mcp-server                  # Start MCP server (pip install pisama[mcp])
```

## MCP Server

Works in Cursor, Claude Desktop, and Windsurf. No API key is needed:

```json
{
  "mcpServers": {
    "pisama": { "command": "pisama", "args": ["mcp-server"] }
  }
}
```

## Optional extras

The base `pisama` install has zero-cost heuristic detection covered. Two extras
add opt-in functionality on top.

### Auto-instrumentation: `pisama[auto]`

Zero-code tracing for LLM calls. `init()` patches supported clients (Anthropic,
OpenAI) so every call after it emits an OTEL trace Pisama can analyze, no
manual instrumentation needed.

```bash
pip install "pisama[auto]"
```

```python
import pisama.auto

pisama.auto.init(api_key="ps_...")

# All subsequent LLM calls are automatically traced
import anthropic
client = anthropic.Anthropic()
response = client.messages.create(...)  # traced automatically
```

This used to require the standalone `pisama-auto` package. That package still
works and stays fully supported for existing installs; `pisama[auto]` is the
same code, folded into the base package so there is one less dependency to
track. New projects should install it this way.

### Agent hooks and tools: `pisama[agents]`

Real-time hooks, tools, and self-check utilities for agent runtimes (built for
the Claude Agent SDK), wired to Pisama's detection infrastructure for
in-loop failure prevention rather than after-the-fact analysis.

```bash
pip install "pisama[agents]"
```

```python
from pisama.agents import pre_tool_use_hook, post_tool_use_hook

agent.hooks.pre_tool_use = pre_tool_use_hook
agent.hooks.post_tool_use = post_tool_use_hook
```

Active self-check is available the same way:

```python
from pisama.agents import check

result = await check(
    output="The server is healthy based on the metrics.",
    context={"query": "Is auth-service down?", "sources": [...]},
)
if not result["passed"]:
    ...  # revise output based on result["issues"]
```

This used to require the standalone `pisama-agent-sdk` package. That package
still works and stays fully supported for existing installs; `pisama[agents]`
is the recommended path for new projects, one package instead of two.

## Detectors

Core detectors, gated per platform (n8n, LangGraph, Dify, OpenClaw and others). A representative selection:

| Detector | What It Catches |
|----------|----------------|
| `loop` | Infinite loops, retry storms, stuck patterns |
| `coordination` | Deadlocked handoffs, message storms |
| `hallucination` | Factual errors, fabricated tool results |
| `injection` | Prompt injection, jailbreak attempts |
| `corruption` | State corruption, type drift |
| `persona_drift` | Persona drift, role confusion |
| `derailment` | Task deviation, goal drift |
| `context` | Context neglect, ignored instructions |
| `specification` | Output vs. requirement mismatch |
| `communication` | Inter-agent message breakdown |
| `decomposition` | Poor task breakdown, circular dependencies |
| `workflow` | Unreachable nodes, missing error handling |
| `completion` | Premature completion, unfinished work |
| `withholding` | Suppressed findings, hidden errors |
| `convergence` | Metric plateau, regression, thrashing |
| `overflow` | Context window exhaustion |
| `propagation` | Silent error propagation across steps |
| `citation` | Fabricated citations and source misattribution |
| `routing` | Inputs misrouted to the wrong specialist agent |
| `mcp_protocol` | MCP tool-communication failures |

## Links

- [Offline usage](#usage)
- [Hosted first diagnosis](#hosted-first-diagnosis)
- [GitHub](https://github.com/Pisama-AI/pisama-python)
- [Platform](https://pisama.ai)

## License

MIT

## Source boundary

This repository is the public source for the MIT-licensed `pisama` Python
package. It does not contain the Pisama Cloud backend, dashboard, calibration
data, managed detection tiers, or paid automation.
