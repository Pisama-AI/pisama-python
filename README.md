# Pisama

**Find and fix failures in AI agent systems. No LLM calls required.**

[![PyPI](https://img.shields.io/pypi/v/pisama?color=blue)](https://pypi.org/project/pisama/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Pisama ships 32 core heuristic detectors plus framework-specific detectors for n8n, LangGraph, Dify, and OpenClaw. They run locally with zero LLM cost on the heuristic tier. An archived run on the [TRAIL benchmark](https://arxiv.org/abs/2505.08638) reports **59.9% joint accuracy** (span and category), compared with 11.9% for the best general-purpose LLM judge tested. The public confusion counts reproduce the reported macro-F1 and micro-F1. See the [benchmark evidence and its reproducibility boundary](https://github.com/Pisama-AI/pisama-detectors/tree/main/benchmarks).

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
pisama watch python my_agent.py    # Watch a live agent (pip install pisama[auto])
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

## Detectors

32 core detectors plus framework-specific detectors for n8n, LangGraph, Dify, and OpenClaw. A representative selection:

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
| `delegation` | Delegation quality and task handoff failures |
| `grounding` | Claims not supported by source documents |
| `retrieval_quality` | Poor retrieval relevance or coverage |
| `compaction_quality` | Information loss during context compaction |

## Benchmark Results

**TRAIL** (trace-level failure detection, 148 traces):

| Method | Joint Accuracy |
|--------|---------------|
| GPT-5.4 | 11.9% |
| Gemini 3.1 Pro | 6.8% |
| **Pisama archived run** | **59.9%** |

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
