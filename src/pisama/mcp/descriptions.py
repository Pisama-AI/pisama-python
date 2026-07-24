"""Curated long-form docs for a subset of Pisama failure types.

Used by the ``pisama_explain`` MCP tool to return human-readable documentation
without a backend connection. This dict holds hand-written prose for the
common detectors; it is NOT the authoritative list of what Pisama detects —
that comes from the live detector registry via ``list_failure_types()``. A real
detector without curated prose still resolves through ``get_failure_description``
(registry fallback), so the tool never calls a real detector "unknown".
"""

from __future__ import annotations

from typing import Any

FAILURE_TYPES: dict[str, dict[str, Any]] = {
    "loop": {
        "name": "Loop Detection",
        "description": (
            "Detects infinite or near-infinite loops in agent state sequences. "
            "Covers exact-match loops (identical states), structural loops "
            "(same shape with different values), and semantic loops (paraphrased "
            "repetitions detected via embeddings)."
        ),
        "common_causes": [
            "Agent retries the same failed tool call without changing parameters",
            "Two agents hand off to each other in a cycle",
            "LLM regenerates the same plan after each rejection",
            "Missing exit condition in a stateful workflow",
        ],
        "detection_methodology": (
            "Tier 1: hash comparison of serialized states. "
            "Tier 2: structural diff of state deltas. "
            "Tier 3: cosine similarity of state embeddings."
        ),
        "severity_range": "40-90",
    },
    "corruption": {
        "name": "State Corruption",
        "description": (
            "Identifies invalid state transitions between agent steps. "
            "Detects fields that change when they should be immutable, missing "
            "required fields, type mismatches, and schema violations."
        ),
        "common_causes": [
            "Tool output overwrites unrelated state fields",
            "Concurrent agents modify shared state without locking",
            "Deserialization error corrupts nested objects",
            "Partial write during a checkpoint save",
        ],
        "detection_methodology": (
            "Compares consecutive state snapshots field-by-field. "
            "Validates against the expected schema and flags unexpected mutations."
        ),
        "severity_range": "50-100",
    },
    "persona_drift": {
        "name": "Persona Drift",
        "description": (
            "Detects when an agent deviates from its assigned role, tone, or "
            "behavioral boundaries. Measures drift between the system prompt "
            "persona and the actual output."
        ),
        "common_causes": [
            "Long conversations dilute the system prompt influence",
            "User prompt injection overrides the persona",
            "Context window truncation drops the system prompt",
            "Multi-agent handoff loses persona context",
        ],
        "detection_methodology": (
            "Compares agent output against the declared persona description "
            "using keyword overlap and allowed-action validation."
        ),
        "severity_range": "30-70",
    },
    "coordination": {
        "name": "Coordination Failure",
        "description": (
            "Detects breakdowns in multi-agent handoffs and message passing. "
            "Covers dropped messages, out-of-order delivery, role confusion, "
            "and failed acknowledgments."
        ),
        "common_causes": [
            "Asynchronous message queue drops messages under load",
            "Agent A sends to agent B but B is not listening",
            "Handoff metadata (task context) is lost between agents",
            "Race condition when two agents claim the same subtask",
        ],
        "detection_methodology": (
            "Analyzes message sequences between agent IDs for gaps, duplicates, "
            "and ordering violations."
        ),
        "severity_range": "40-80",
    },
    "hallucination": {
        "name": "Hallucination",
        "description": (
            "Detects factual inaccuracies and unsupported claims in agent output. "
            "Compares generated text against provided source documents or known facts."
        ),
        "common_causes": [
            "Agent generates facts not present in retrieved documents",
            "LLM confabulates statistics, dates, or names",
            "Retrieval returns irrelevant documents and agent trusts them",
            "Agent extrapolates beyond the evidence in its context",
        ],
        "detection_methodology": (
            "Cross-references output claims against source documents. "
            "Uses word overlap and claim extraction to find unsupported statements."
        ),
        "severity_range": "40-90",
    },
    "injection": {
        "name": "Prompt Injection",
        "description": (
            "Detects prompt injection attempts and adversarial inputs designed "
            "to override agent instructions or exfiltrate data."
        ),
        "common_causes": [
            "User input contains embedded system-prompt overrides",
            "Tool output includes adversarial text (indirect injection)",
            "Jailbreak patterns attempt to bypass safety guardrails",
            "Encoded payloads (base64, unicode) hide malicious instructions",
        ],
        "detection_methodology": (
            "Pattern matching for known injection signatures, "
            "role-switching keywords, and encoding-based obfuscation."
        ),
        "severity_range": "60-100",
    },
    "overflow": {
        "name": "Context Overflow",
        "description": (
            "Detects context window exhaustion and token budget overruns. "
            "Flags when an agent is approaching or exceeding its token limit, "
            "which causes truncation and information loss."
        ),
        "common_causes": [
            "Accumulated conversation history exceeds the context window",
            "Large tool outputs (e.g. file contents) consume most of the budget",
            "Agent includes full retrieved documents instead of summaries",
            "Recursive self-reflection inflates the context",
        ],
        "detection_methodology": (
            "Tracks token counts across spans and compares against configured budget thresholds."
        ),
        "severity_range": "30-80",
    },
    "derailment": {
        "name": "Task Derailment",
        "description": (
            "Detects when an agent loses focus on its assigned task and pursues "
            "tangential or unrelated goals."
        ),
        "common_causes": [
            "User asks a follow-up question that shifts the topic",
            "Tool output introduces a red herring the agent follows",
            "Agent misinterprets an intermediate result as the final goal",
            "Multi-step plan loses coherence after an error recovery",
        ],
        "detection_methodology": (
            "Compares agent output against the original task description "
            "using semantic similarity and keyword overlap."
        ),
        "severity_range": "30-70",
    },
    "context": {
        "name": "Context Neglect",
        "description": (
            "Detects when an agent fails to use relevant prior context in its "
            "response, ignoring information that was previously provided."
        ),
        "common_causes": [
            "Agent does not reference earlier conversation turns",
            "Context window truncation drops relevant earlier messages",
            "Agent treats each turn as independent (stateless behavior)",
            "Retrieval pipeline fails to surface relevant prior context",
        ],
        "detection_methodology": (
            "Measures overlap between available context and agent output. "
            "Flags when key context elements are absent from the response."
        ),
        "severity_range": "30-70",
    },
    "communication": {
        "name": "Communication Breakdown",
        "description": (
            "Detects failures in inter-agent communication where messages are "
            "misunderstood, ignored, or lost between agents."
        ),
        "common_causes": [
            "Sender message format does not match receiver expectations",
            "Agent ignores or misinterprets a request from another agent",
            "Message routing sends to the wrong recipient",
            "Acknowledgment messages are missing or delayed",
        ],
        "detection_methodology": (
            "Analyzes sender-receiver message pairs for semantic alignment and response relevance."
        ),
        "severity_range": "30-70",
    },
    "specification": {
        "name": "Specification Mismatch",
        "description": (
            "Detects when agent output does not match the task specification "
            "or user intent, including missing requirements and format violations."
        ),
        "common_causes": [
            "Agent produces output in wrong format (JSON vs prose)",
            "Required fields are missing from structured output",
            "Agent addresses a different interpretation of the spec",
            "Partial completion where some requirements are met but not all",
        ],
        "detection_methodology": (
            "Compares agent output against the task specification and user intent "
            "for requirement coverage and format compliance."
        ),
        "severity_range": "30-80",
    },
    "decomposition": {
        "name": "Decomposition Failure",
        "description": (
            "Detects poor task breakdown where subtasks are incomplete, "
            "overlapping, have missing dependencies, or are improperly ordered."
        ),
        "common_causes": [
            "Agent creates subtasks that do not cover the full task",
            "Subtask dependencies are missing or circular",
            "Decomposition is too fine-grained or too coarse",
            "Agent skips decomposition and attempts the task monolithically",
        ],
        "detection_methodology": (
            "Validates subtask structure against the parent task description. "
            "Checks for coverage, dependency completeness, and ordering."
        ),
        "severity_range": "30-70",
    },
    "workflow": {
        "name": "Workflow Execution Failure",
        "description": (
            "Detects issues in workflow execution including node failures, "
            "incorrect ordering, skipped steps, and timeout violations."
        ),
        "common_causes": [
            "A workflow node fails but the workflow continues without handling it",
            "Steps execute out of the defined order",
            "Conditional branches evaluate incorrectly",
            "Workflow timeout causes premature termination",
        ],
        "detection_methodology": (
            "Compares actual execution trace against the workflow definition "
            "for ordering, completeness, and error handling."
        ),
        "severity_range": "40-80",
    },
    "withholding": {
        "name": "Information Withholding",
        "description": (
            "Detects when an agent omits critical information from its output "
            "that is present in its internal state or retrieved documents."
        ),
        "common_causes": [
            "Agent summarizes too aggressively, dropping key details",
            "Safety filters suppress relevant but sensitive information",
            "Agent prioritizes brevity over completeness",
            "Internal reasoning reaches a conclusion but the output omits the evidence",
        ],
        "detection_methodology": (
            "Compares agent output against internal state to find significant "
            "information present internally but absent from the response."
        ),
        "severity_range": "30-70",
    },
    "completion": {
        "name": "Completion Signaling",
        "description": (
            "Detects premature or delayed task completion signaling where the "
            "agent declares done too early or continues working after the task "
            "is actually complete."
        ),
        "common_causes": [
            "Agent declares success before verifying all requirements",
            "Agent continues iterating after all criteria are met",
            "Ambiguous success criteria lead to premature termination",
            "Error recovery loop prevents the agent from ever declaring done",
        ],
        "detection_methodology": (
            "Evaluates agent output against success criteria and subtask "
            "completion status to detect premature or delayed completion."
        ),
        "severity_range": "30-70",
    },
    "convergence": {
        "name": "Convergence Failure",
        "description": (
            "Detects metric-level issues in iterative processes: plateau "
            "(metrics stop improving), regression (metrics worsen), thrashing "
            "(metrics oscillate), and divergence (metrics move away from target)."
        ),
        "common_causes": [
            "Learning rate is too high causing oscillation",
            "Optimization target conflicts with constraints",
            "Feedback loop introduces instability",
            "Insufficient data for reliable metric computation",
        ],
        "detection_methodology": (
            "Analyzes a window of metric values for trend, variance, "
            "and direction relative to the optimization goal."
        ),
        "severity_range": "30-70",
    },
}


def _registry_detectors() -> dict[str, str]:
    """Real detector name -> short description, from the live detector registry.

    This is the source of truth for what Pisama can actually detect. The MCP
    used to advertise a hand-maintained list that had drifted: it named two
    detectors that do not exist (retrieval_quality, grounding) and omitted 16
    that do. Deriving from the registry keeps the tool honest. Falls back to the
    curated set only if pisama_core is somehow unavailable at runtime.
    """
    try:
        from pisama_core.detection.detectors import _BUILTIN_DETECTORS

        return {d.name: getattr(d, "description", "") for d in _BUILTIN_DETECTORS}
    except Exception:
        return {k: v.get("description", "") for k, v in FAILURE_TYPES.items()}


def get_failure_description(failure_type: str) -> dict[str, Any] | None:
    """Curated docs if we have them, else a minimal entry for any REAL detector.

    Returns None only for a genuinely unknown name (e.g. a typo), so the MCP
    never calls a real detector "unknown" just because its long-form docs are
    not written yet.
    """
    if failure_type in FAILURE_TYPES:
        return FAILURE_TYPES[failure_type]
    registry = _registry_detectors()
    if failure_type in registry:
        return {
            "name": failure_type,
            "description": registry[failure_type] or f"The {failure_type} detector.",
            "common_causes": [],
            "detection_methodology": "See the detector registry; long-form docs pending.",
            "severity_range": "varies",
            "curated": False,
        }
    return None


def list_failure_types() -> list[str]:
    """Every failure type Pisama can actually detect, from the live registry."""
    return sorted(_registry_detectors().keys())
