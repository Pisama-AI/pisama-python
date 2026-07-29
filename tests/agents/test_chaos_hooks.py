"""Tests for SDK-level chaos hooks."""

import sys
from pathlib import Path

# Add src to path so we can import chaos module without full SDK deps.
# This file lives at tests/agents/, two levels below the repo root (the
# original pisama-agent-sdk had tests/ directly at repo root).
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import importlib.util

# Load chaos modules directly from file paths to avoid triggering
# pisama.agents's __init__.py (which requires pisama_core).
_repo_root = Path(__file__).parent.parent.parent
_src = _repo_root / "src" / "pisama" / "agents" / "chaos"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_config_mod = _load("chaos_config", _src / "config.py")
_exp_mod = _load("chaos_experiments", _src / "experiments.py")

ChaosConfig = _config_mod.ChaosConfig
ChaosExperiment = _exp_mod.ChaosExperiment
ChaosResult = _exp_mod.ChaosResult
ToolFailure = _exp_mod.ToolFailure
LatencyInjection = _exp_mod.LatencyInjection
ErrorInjection = _exp_mod.ErrorInjection
OutputCorruption = _exp_mod.OutputCorruption
ContextTruncation = _exp_mod.ContextTruncation


class TestToolFailure:
    def test_blocks_targeted_tool(self):
        exp = ToolFailure(tools=["search"], probability=1.0)
        result = exp.apply_pre("search", {"query": "test"})
        assert result.applied
        assert result.block
        assert "search" in result.message

    def test_does_not_block_non_targeted_tool(self):
        exp = ToolFailure(tools=["search"], probability=1.0)
        assert not exp.matches("database_query")

    def test_matches_all_when_no_tools_specified(self):
        exp = ToolFailure(probability=1.0)
        assert exp.matches("anything")
        assert exp.matches("search")

    def test_matches_targeted_agent(self):
        exp = ToolFailure(agents=["researcher"], probability=1.0)
        assert exp.matches("search", agent_name="researcher")
        assert not exp.matches("search", agent_name="planner")


class TestLatencyInjection:
    def test_returns_delay(self):
        exp = LatencyInjection(min_ms=100, max_ms=200, probability=1.0)
        result = exp.apply_pre("search", {})
        assert result.applied
        assert 100 <= result.delay_ms <= 200
        assert not result.block

    def test_no_block(self):
        exp = LatencyInjection(min_ms=500, max_ms=500, probability=1.0)
        result = exp.apply_pre("search", {})
        assert result.delay_ms == 500
        assert not result.block


class TestErrorInjection:
    def test_blocks_with_error(self):
        exp = ErrorInjection(error_code=503, probability=1.0)
        result = exp.apply_pre("search", {})
        assert result.applied
        assert result.block
        assert "503" in result.message


class TestOutputCorruption:
    def test_truncates_output(self):
        exp = OutputCorruption(corruption="truncate", probability=1.0)
        original = "This is a long search result with lots of content"
        result = exp.apply_post("search", original)
        assert result.applied
        assert result.modified_output is not None
        assert len(result.modified_output) < len(original)

    def test_empties_output(self):
        exp = OutputCorruption(corruption="empty", probability=1.0)
        result = exp.apply_post("search", "Some output")
        assert result.modified_output == ""

    def test_json_break(self):
        exp = OutputCorruption(corruption="json_break", probability=1.0)
        result = exp.apply_post("search", '{"data": "valid"}')
        assert '{"incomplete": true' in result.modified_output


class TestContextTruncation:
    def test_truncates_long_strings(self):
        exp = ContextTruncation(truncation_pct=0.5, probability=1.0)
        long_input = {"query": "x" * 200, "short_field": "hi"}
        result = exp.apply_pre("search", long_input)
        assert result.applied
        assert len(result.modified_input["query"]) == 100  # 50% of 200
        assert result.modified_input["short_field"] == "hi"  # short strings unchanged

    def test_preserves_non_strings(self):
        exp = ContextTruncation(truncation_pct=0.5, probability=1.0)
        result = exp.apply_pre("search", {"count": 42, "flag": True})
        assert result.modified_input["count"] == 42
        assert result.modified_input["flag"] is True


class TestChaosConfig:
    def test_is_active_when_enabled(self):
        config = ChaosConfig(experiments=[ToolFailure()], enabled=True)
        assert config.is_active

    def test_not_active_when_disabled(self):
        config = ChaosConfig(experiments=[ToolFailure()], enabled=False)
        assert not config.is_active

    def test_safety_limit_disables(self):
        config = ChaosConfig(experiments=[ToolFailure()], safety_max_affected=3)
        assert config.is_active
        config.record_affected()
        config.record_affected()
        config.record_affected()
        assert not config.is_active  # 3 >= safety_max_affected

    def test_reset_re_enables(self):
        config = ChaosConfig(experiments=[ToolFailure()], safety_max_affected=1)
        config.record_affected()
        assert not config.is_active
        config.reset()
        assert config.is_active


class TestProbability:
    def test_zero_probability_never_triggers(self):
        exp = ToolFailure(probability=0.0)
        triggered = sum(1 for _ in range(100) if exp.should_trigger())
        assert triggered == 0

    def test_one_probability_always_triggers(self):
        exp = ToolFailure(probability=1.0)
        triggered = sum(1 for _ in range(100) if exp.should_trigger())
        assert triggered == 100


class TestChaosExperimentBase:
    def test_default_apply_pre_is_noop(self):
        exp = ChaosExperiment()
        result = exp.apply_pre("tool", {})
        assert not result.applied

    def test_default_apply_post_is_noop(self):
        exp = ChaosExperiment()
        result = exp.apply_post("tool", "output")
        assert not result.applied
