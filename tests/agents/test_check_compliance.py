"""Tests for the gated check_compliance SDK helper."""

from __future__ import annotations

# Tests need the module object, not the function. ``from pisama.agents
# import check_compliance`` above imports the function, so we re-resolve
# the module via importlib for unambiguous patching.
import importlib
import io
import json
from unittest.mock import patch

import pytest

from pisama.agents import (
    BehavioralRule,
    ComplianceResult,
    PisamaFeatureNotEnabledError,
    Violation,
    check_compliance,
)

check_compliance_module = importlib.import_module(
    "pisama.agents.check_compliance"
)


_FLAG = "PISAMA_ENABLE_CHECK_COMPLIANCE"


# ---------------------------------------------------------------------------
# Feature-flag gating
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    """check_compliance must refuse to run unless the flag is set truthy."""

    @pytest.mark.asyncio
    async def test_flag_unset_raises(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        with pytest.raises(PisamaFeatureNotEnabledError) as exc:
            await check_compliance("system prompt", [])
        # Error message should tell the caller exactly how to enable it.
        assert _FLAG in str(exc.value)

    @pytest.mark.asyncio
    async def test_flag_empty_raises(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "")
        with pytest.raises(PisamaFeatureNotEnabledError):
            await check_compliance("system prompt", [])

    @pytest.mark.asyncio
    async def test_flag_zero_raises(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "0")
        with pytest.raises(PisamaFeatureNotEnabledError):
            await check_compliance("system prompt", [])

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
    def test_flag_truthy_values_enabled(self, monkeypatch, value):
        monkeypatch.setenv(_FLAG, value)
        assert check_compliance_module._is_compliance_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "no", "false", "off"])
    def test_flag_falsy_values_disabled(self, monkeypatch, value):
        monkeypatch.setenv(_FLAG, value)
        assert check_compliance_module._is_compliance_enabled() is False


# ---------------------------------------------------------------------------
# Bridge / HTTP path with the flag enabled
# ---------------------------------------------------------------------------


def _fake_http_response(payload: dict):
    """Return a context-manager-shaped object that urlopen() expects."""
    return io.BytesIO(json.dumps(payload).encode())


class TestCheckComplianceCall:
    """When the flag is set, check_compliance hits the backend endpoint."""

    @pytest.mark.asyncio
    async def test_passes_through_to_backend(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = request.data
            captured["timeout"] = timeout
            return _fake_http_response({
                "detected": True,
                "confidence": 0.9,
                "violations": [
                    {
                        "rule_id": "no_drop_table",
                        "evidence": "tool_call: drop_table()",
                        "explanation": "Trace contains forbidden action.",
                        "confidence": 0.9,
                    }
                ],
                "extracted_rules": [
                    {
                        "rule_id": "no_drop_table",
                        "description": "Never call drop_table.",
                        "trigger": "always",
                        "required_action": None,
                        "forbidden_action": "call_tool: drop_table",
                        "severity": "critical",
                    }
                ],
                "tokens_used": 1234,
                "cost_usd": 0.0042,
            })

        with patch.object(check_compliance_module, "urlopen", fake_urlopen):
            result = await check_compliance(
                system_prompt="You are a careful assistant. Never call drop_table.",
                trace_events=[{"type": "tool_call", "name": "drop_table", "args": {}}],
            )

        # URL is correct
        assert captured["url"].endswith("/api/v1/evaluate/detect/specification-compliance")
        # Body shape matches the analyzer's signature
        body = json.loads(captured["body"].decode())
        assert body["system_prompt"].startswith("You are a careful assistant")
        assert body["trace_events"] == [
            {"type": "tool_call", "name": "drop_table", "args": {}}
        ]

        # Result deserialization
        assert isinstance(result, ComplianceResult)
        assert result.detected is True
        assert result.confidence == pytest.approx(0.9)
        assert result.tokens_used == 1234
        assert result.cost_usd == pytest.approx(0.0042)

        assert len(result.violations) == 1
        v = result.violations[0]
        assert isinstance(v, Violation)
        assert v.rule_id == "no_drop_table"
        assert v.evidence == "tool_call: drop_table()"

        assert len(result.extracted_rules) == 1
        r = result.extracted_rules[0]
        assert isinstance(r, BehavioralRule)
        assert r.severity == "critical"
        assert r.forbidden_action == "call_tool: drop_table"

    @pytest.mark.asyncio
    async def test_handles_no_violations(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")

        def fake_urlopen(request, timeout=None):
            return _fake_http_response({
                "detected": False,
                "confidence": 0.5,
                "violations": [],
                "extracted_rules": [],
                "tokens_used": 200,
                "cost_usd": 0.0001,
            })

        with patch.object(check_compliance_module, "urlopen", fake_urlopen):
            result = await check_compliance("be careful", [])

        assert result.detected is False
        assert result.violations == []
        assert result.extracted_rules == []

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty_result(self, monkeypatch):
        """Bad JSON should not crash the agent — we return a safe default."""
        monkeypatch.setenv(_FLAG, "1")

        def fake_urlopen(request, timeout=None):
            return io.BytesIO(b"not json at all")

        with patch.object(check_compliance_module, "urlopen", fake_urlopen):
            result = await check_compliance("sp", [])

        assert isinstance(result, ComplianceResult)
        assert result.detected is False
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_uses_pisama_api_url_env(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv("PISAMA_API_URL", "https://api.example.test")

        # Reset any previously configured _api_url on the check module
        from pisama.agents import check as _check_module
        monkeypatch.setattr(_check_module, "_api_url", None, raising=False)

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return _fake_http_response({
                "detected": False, "confidence": 0.0,
                "violations": [], "extracted_rules": [],
                "tokens_used": 0, "cost_usd": 0.0,
            })

        with patch.object(check_compliance_module, "urlopen", fake_urlopen):
            await check_compliance("sp", [])

        assert captured["url"] == "https://api.example.test/api/v1/evaluate/detect/specification-compliance"
