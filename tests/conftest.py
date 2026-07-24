"""Shared fixtures backed by a captured Omnigent multi-agent session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pisama_core.ingestion.omnigent_events import events_to_atif

OMNIGENT_FIXTURES = Path(__file__).parent / "fixtures" / "omnigent"


@pytest.fixture(scope="session")
def captured_omnigent_trajectory() -> dict[str, Any]:
    """Convert the committed real Omnigent capture into its ATIF trajectory."""
    metadata = json.loads((OMNIGENT_FIXTURES / "meta.json").read_text(encoding="utf-8"))
    parent_events = [
        json.loads(line)
        for line in (OMNIGENT_FIXTURES / "parent_stream.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    child_events = {
        child_id: [
            json.loads(line)
            for line in (OMNIGENT_FIXTURES / f"child_{child_id}_stream.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        for child_id in metadata["child_session_ids"]
    }
    trajectory = events_to_atif(
        parent_events,
        child_streams=child_events,
        agent_name=metadata["agent"],
        agent_version="omnigent:claude-sdk",
        session_id=metadata["parent_session_id"],
    )
    assert trajectory is not None
    return trajectory
