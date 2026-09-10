"""Validate accounting metadata without copying arbitrary trace content."""

import asyncio
import copy
import json

import pytest
from pisama_core.detection.detectors.communication import CommunicationDetector
from pisama_core.traces.models import Trace

from pisama import analyze
from pisama._coverage import validate_response_coverage


def valid():
    return {
        "version": 1,
        "scope": "eligible_captured_response_pairs",
        "trace_span_count": 1,
        "considered_count": 1,
        "checked_count": 1,
        "unsupported_count": 0,
        "outside_scope_count": 0,
        "business_semantics_assessed": False,
        "records": [
            {
                "span_index": 0,
                "span_id": "PRIVATE_SYNTHETIC_MARKER",
                "status": "satisfied",
                "contract_kind": "json",
                "reason": "explicit_contract_checked",
                "identity_ambiguous": False,
                "relationship": "captured_input_output",
            }
        ],
    }


def test_projection_strips_identifiers_and_arbitrary_fields():
    data = valid()
    data["request"] = "PRIVATE_SYNTHETIC_MARKER"
    output = validate_response_coverage(data)
    assert output is not None
    assert "PRIVATE_SYNTHETIC_MARKER" not in json.dumps(output)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("checked_count", True),
        ("checked_count", 2),
        ("trace_span_count", -1),
        ("business_semantics_assessed", True),
        ("scope", "whole_business_task"),
    ],
)
def test_invalid_counts_or_scope_rejected(field, value):
    data = valid()
    data[field] = value
    assert validate_response_coverage(data) is None


def test_duplicate_indices_rejected():
    data = valid()
    data["records"].append(copy.deepcopy(data["records"][0]))
    data.update(trace_span_count=2, considered_count=2, checked_count=2)
    assert validate_response_coverage(data) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("span_index", True),
        ("status", "PRIVATE_SYNTHETIC_MARKER"),
        ("reason", "PRIVATE_SYNTHETIC_MARKER"),
        ("contract_kind", "arbitrary_schema"),
        ("identity_ambiguous", "no"),
        ("relationship", "adjacency"),
    ],
)
def test_invalid_records_rejected(field, value):
    data = valid()
    data["records"][0][field] = value
    assert validate_response_coverage(data) is None


def test_real_paired_core_accounting_survives_projection():
    trace = {
        "spans": [
            {
                "kind": "llm",
                "span_id": "PRIVATE_SYNTHETIC_MARKER",
                "input_data": {"content": "Reply with OK."},
                "output_data": {"content": "OK"},
            },
            {
                "kind": "llm",
                "input_data": {"content": "If ready, reply with OK."},
                "output_data": {"content": "not ready"},
            },
        ]
    }
    raw = asyncio.run(CommunicationDetector().detect(Trace.from_dict(trace)))
    result = analyze(trace, detectors=["communication"])
    assessment = result.detector_assessments[0]
    if "response_contract_coverage" not in raw.metadata:
        assert "response_contract_coverage" not in assessment
    else:
        coverage = assessment["response_contract_coverage"]
        assert coverage["checked_count"] == 1
        assert coverage["unsupported_count"] == 1
        assert coverage["business_semantics_assessed"] is False
        assert "PRIVATE_SYNTHETIC_MARKER" not in json.dumps(coverage)
