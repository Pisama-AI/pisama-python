# Pisama

**Find and fix failures in AI agent systems. No LLM calls required.**

[![PyPI](https://img.shields.io/pypi/v/pisama?color=blue)](https://pypi.org/project/pisama/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Pisama ships 32 core heuristic detectors. They apply across frameworks including n8n, LangGraph, Dify and OpenClaw, with per-platform gating (for example `coordination` runs only on multi-agent platforms). They run locally with zero LLM cost on the heuristic tier. An archived, in-distribution run on the [TRAIL benchmark](https://arxiv.org/abs/2505.08638) reports 59.9% joint accuracy (span and category). It is not a held-out result: 144 of the 148 traces appeared in calibration material, and the published archive does not contain the prediction-level data needed to recompute joint accuracy. The public confusion counts do reproduce the reported macro-F1 (0.7535) and micro-F1 (0.7463), but those F1 values carry no precision information: the archive recorded no false positives, so they reduce to recall (micro-recall 0.5953). See the [benchmark evidence and its reproducibility boundary](https://github.com/Pisama-AI/pisama-detectors/tree/main/benchmarks).

## Install

```bash
pip install pisama
```

## Usage

```python
from pisama import analyze

result = analyze("trace.json")  # also accepts dicts and JSON strings

for issue in result.issues:
    print(f"[{issue.type}] {issue.summary} (severity: {issue.severity})")
    print(f"  Fix: {issue.recommendation}")
```

## CLI

```bash
pisama analyze trace.json          # Analyze a trace
pisama watch python my_agent.py    # Watch a live agent (pip install "pisama[auto]")
pisama replay <trace-id>           # Re-run detection on stored traces
pisama smoke-test --last 50        # Batch test recent traces
pisama detectors                   # List all 32 core detectors
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

32 core detectors, gated per platform (n8n, LangGraph, Dify, OpenClaw and others). A representative selection:

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

## Benchmark Results

**TRAIL** (trace-level failure detection, 148 traces). Archived April 2026 run, **in-distribution and not held out**: 144 of 148 traces appeared in calibration material.

| Method | Joint Accuracy |
|--------|---------------|
| Pisama archived run | 59.9% |

Reproducible from the archive: macro-F1 0.7535, micro-F1 0.7463 (`benchmarks/verify_report.py`).

**Read those F1 figures as a recall measurement.** The archive scored only annotated errors,
so no false positive was recordable: `fp = 0` in 14 of 14 categories, and `prediction_count`
equals `mapped_annotations` (813). Precision is therefore 1.000 by construction rather than by
measurement, and F1 collapses to `2R / (1 + R)`, carrying no information beyond recall. The
informative figure is micro-recall 0.5953: the heuristic tier alone missed roughly 40% of the
labelled failures. The same boundary is recorded in
[`benchmarks/evidence.json`](https://github.com/Pisama-AI/pisama-detectors/blob/main/benchmarks/evidence.json)
and in the [`pisama-detectors` README](https://github.com/Pisama-AI/pisama-detectors#archived-trail-platform-benchmark).

Joint accuracy is **not** recomputable from the public archive, which lacks prediction-level
data. LLM-judge baselines are omitted here because
[`trail_llm_baselines.json`](https://github.com/Pisama-AI/pisama-detectors/blob/main/benchmarks/trail_llm_baselines.json)
carries confusion counts but `"result": null` for every model, so no comparable joint-accuracy
figure exists in the artifact. See the
[reproducibility boundary](https://github.com/Pisama-AI/pisama-detectors/tree/main/benchmarks).

**Who&When** (ICML 2025, multi-agent attribution, 58 hand-crafted cases):

| Method | Agent Accuracy | Step Accuracy |
|--------|---------------|---------------|
| GPT-5.4 Mini | 60.3% | 22.4% |
| **Pisama + Sonnet 4** | **60.3%** | **24.1%** |

## Links

- [Documentation](https://docs.pisama.ai)
- [GitHub](https://github.com/Pisama-AI/pisama-python)
- [Platform](https://pisama.ai)

## License

MIT

## Source boundary

This repository is the public source for the MIT-licensed `pisama` Python
package. It does not contain the Pisama Cloud backend, dashboard, calibration
data, managed detection tiers, or paid automation.
