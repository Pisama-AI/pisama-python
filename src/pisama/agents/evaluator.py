"""Pisama Evaluator — drop-in evaluator for multi-agent harnesses.

Usage:
    from pisama.agents import PisamaEvaluator

    evaluator = PisamaEvaluator(api_key="psk_...", base_url="https://api.pisama.ai")

    result = evaluator.evaluate(
        specification={"text": "Build a login page with OAuth"},
        output={"text": generator_output},
    )
    if not result.passed:
        for failure in result.failures:
            print(f"{failure.detector}: {failure.description}")
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


@dataclass
class EvalFailure:
    detector: str
    confidence: float
    severity: str
    title: str
    description: str
    suggested_fix: Optional[str] = None


@dataclass
class EvalResult:
    passed: bool
    score: float
    failures: List[EvalFailure]
    suggestions: List[str]
    detectors_run: List[str]
    evaluation_time_ms: int


class PisamaEvaluator:
    """Client for the Pisama evaluation API.

    Args:
        api_key: Pisama API key (psk_...)
        base_url: Backend URL (default: https://api.pisama.ai)
        timeout: Request timeout in seconds
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.pisama.ai",
        timeout: float = 30.0,
    ):
        if not _HTTPX_AVAILABLE:
            raise ImportError(
                "httpx is required for PisamaEvaluator: `pip install pisama[agents]`"
            )

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def evaluate(
        self,
        specification: Dict[str, Any],
        output: Dict[str, Any],
        agent_role: str = "generator",
        detectors: Optional[List[str]] = None,
        context_limit: Optional[int] = None,
    ) -> EvalResult:
        """Evaluate generator output against a specification.

        Args:
            specification: Sprint contract or task spec.
            output: Generator output to evaluate.
            agent_role: Role of the producing agent (generator/evaluator/planner).
            detectors: Specific detectors to run (default: auto-select).
            context_limit: Model context window for pressure detection.

        Returns:
            EvalResult with pass/fail verdict and failure details.
        """
        payload: Dict[str, Any] = {
            "specification": specification,
            "output": output,
            "agent_role": agent_role,
        }
        if detectors:
            payload["detectors"] = detectors
        if context_limit:
            payload["context_limit"] = context_limit

        response = self._client.post("/api/v1/evaluate", json=payload)
        response.raise_for_status()
        data = response.json()

        failures = [
            EvalFailure(
                detector=f["detector"],
                confidence=f["confidence"],
                severity=f["severity"],
                title=f["title"],
                description=f["description"],
                suggested_fix=f.get("suggested_fix"),
            )
            for f in data.get("failures", [])
        ]

        return EvalResult(
            passed=data["passed"],
            score=data["score"],
            failures=failures,
            suggestions=data.get("suggestions", []),
            detectors_run=data.get("detectors_run", []),
            evaluation_time_ms=data.get("evaluation_time_ms", 0),
        )

    async def evaluate_async(
        self,
        specification: Dict[str, Any],
        output: Dict[str, Any],
        agent_role: str = "generator",
        detectors: Optional[List[str]] = None,
        context_limit: Optional[int] = None,
    ) -> EvalResult:
        """Async version of evaluate()."""
        payload: Dict[str, Any] = {
            "specification": specification,
            "output": output,
            "agent_role": agent_role,
        }
        if detectors:
            payload["detectors"] = detectors
        if context_limit:
            payload["context_limit"] = context_limit

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        ) as client:
            response = await client.post("/api/v1/evaluate", json=payload)
            response.raise_for_status()
            data = response.json()

        failures = [
            EvalFailure(
                detector=f["detector"],
                confidence=f["confidence"],
                severity=f["severity"],
                title=f["title"],
                description=f["description"],
                suggested_fix=f.get("suggested_fix"),
            )
            for f in data.get("failures", [])
        ]

        return EvalResult(
            passed=data["passed"],
            score=data["score"],
            failures=failures,
            suggestions=data.get("suggestions", []),
            detectors_run=data.get("detectors_run", []),
            evaluation_time_ms=data.get("evaluation_time_ms", 0),
        )

    def close(self):
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
