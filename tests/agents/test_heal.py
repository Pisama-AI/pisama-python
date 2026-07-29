"""Tests for HealingResult convenience accessors (no network)."""

from pisama.agents import HealingResult


class TestIdePatchProperty:
    """HealingResult.ide_patch surfaces the server-rendered IDE patch."""

    def test_returns_patch_dict_when_present(self):
        patch = {
            "target_files": ["CLAUDE.md", ".cursorrules"],
            "apply_mode": "suggested",
            "framework": None,
            "instructions": "## Add retry limit\n...",
            "verification": "pytest -q\n",
        }
        result = HealingResult(
            applied=True,
            escalated=False,
            risk_level="safe",
            fix={"id": "fix_1", "ide_patch": patch},
        )
        assert result.ide_patch == patch

    def test_none_when_no_fix(self):
        result = HealingResult(applied=False, escalated=False, risk_level="unknown")
        assert result.ide_patch is None

    def test_none_when_fix_lacks_patch(self):
        result = HealingResult(
            applied=True,
            escalated=False,
            risk_level="safe",
            fix={"id": "fix_1"},
        )
        assert result.ide_patch is None

    def test_none_when_patch_not_a_dict(self):
        result = HealingResult(
            applied=True,
            escalated=False,
            risk_level="safe",
            fix={"id": "fix_1", "ide_patch": "oops"},
        )
        assert result.ide_patch is None
