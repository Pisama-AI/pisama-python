"""Tests for the `pisama omnigent` CLI command group.

Uses a real captured Omnigent session committed in this repository. No
synthetic events or mocked mapper.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from pisama.cli.main import main
from pisama.cli.omnigent_cmd import SessionWatcher, watch_cmd

FIXTURES = Path(__file__).parent / "fixtures" / "omnigent"


def test_omnigent_group_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["omnigent", "--help"])
    assert result.exit_code == 0
    assert "watch" in result.output


def test_watch_requires_session_or_latest() -> None:
    runner = CliRunner()
    result = runner.invoke(watch_cmd, [])
    assert result.exit_code != 0
    assert "--session" in result.output


def test_watcher_assembles_fixture_trajectory() -> None:
    """Replaying the real captured session through the watcher's mapper
    produces a trajectory with both sub-agent sessions embedded."""
    from pisama_core.ingestion.omnigent_events import events_to_atif

    meta = json.loads((FIXTURES / "meta.json").read_text())
    parent = [
        json.loads(line)
        for line in (FIXTURES / "parent_stream.jsonl").read_text().splitlines()
        if line.strip()
    ]
    children = {
        cid: [
            json.loads(line)
            for line in (FIXTURES / f"child_{cid}_stream.jsonl").read_text().splitlines()
            if line.strip()
        ]
        for cid in meta["child_session_ids"]
    }

    watcher = SessionWatcher(
        server="http://localhost:6767",
        api_url="http://localhost:8000",
        session_id=meta["parent_session_id"],
    )
    watcher.parent_events = parent
    watcher.child_events = children

    trajectory = events_to_atif(
        watcher.parent_events,
        child_streams=watcher.child_events,
        agent_name=meta["agent"],
        agent_version="omnigent:claude-sdk",
        session_id=watcher.session_id,
    )
    assert trajectory is not None
    assert trajectory["session_id"] == meta["parent_session_id"]
    embedded = {t["trajectory_id"] for t in trajectory["subagent_trajectories"]}
    assert embedded == set(meta["child_session_ids"])
