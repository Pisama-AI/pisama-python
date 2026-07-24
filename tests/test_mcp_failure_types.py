"""Section G: the MCP pisama_explain failure-type list must track the real
detector registry. It had drifted — advertising 2 nonexistent detectors
(grounding, retrieval_quality) and omitting 16 real ones. These tests fail if
that drift reappears.
"""

from __future__ import annotations

from pisama_core.detection.detectors import _BUILTIN_DETECTORS

from pisama.mcp.descriptions import (
    FAILURE_TYPES,
    get_failure_description,
    list_failure_types,
)


def _registry_names() -> set[str]:
    return {d.name for d in _BUILTIN_DETECTORS}


def test_no_phantom_curated_types() -> None:
    # Every curated entry must correspond to a detector that actually exists.
    phantom = set(FAILURE_TYPES) - _registry_names()
    assert not phantom, f"curated docs name nonexistent detectors: {sorted(phantom)}"


def test_known_phantoms_removed() -> None:
    assert "grounding" not in FAILURE_TYPES
    assert "retrieval_quality" not in FAILURE_TYPES


def test_list_failure_types_matches_registry() -> None:
    # The advertised set is the real registry, not a stale hand-list.
    assert set(list_failure_types()) == _registry_names()


def test_real_uncurated_detector_is_explainable() -> None:
    # A real detector without long-form prose must still resolve, not 404.
    uncurated = sorted(_registry_names() - set(FAILURE_TYPES))
    assert uncurated, "expected some registry detectors without curated docs"
    desc = get_failure_description(uncurated[0])
    assert desc is not None
    assert desc["name"] == uncurated[0]


def test_unknown_type_returns_none() -> None:
    assert get_failure_description("definitely_not_a_detector") is None
