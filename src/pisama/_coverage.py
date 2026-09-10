"""Strict, content-free projection of explicit response-contract accounting."""

from typing import Any


def validate_response_coverage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if type(value.get("version")) is not int or value["version"] != 1:
        return None
    if (
        value.get("scope") != "eligible_captured_response_pairs"
        or value.get("business_semantics_assessed") is not False
    ):
        return None
    counts = (
        "trace_span_count",
        "considered_count",
        "checked_count",
        "unsupported_count",
        "outside_scope_count",
    )
    if any(type(value.get(key)) is not int or value[key] < 0 for key in counts):
        return None
    rows = value.get("records")
    if not isinstance(rows, list) or len(rows) != value["trace_span_count"]:
        return None
    reasons = {
        "satisfied": {"explicit_contract_checked"},
        "violated": {"explicit_contract_checked"},
        "unsupported": {
            "missing_attributable_pair",
            "ambiguous_parent_identity",
            "unsupported_or_ambiguous_contract",
        },
        "outside_scope": {"span_kind_outside_response_scope"},
    }
    safe = []
    seen: set[int] = set()
    totals = dict.fromkeys(reasons, 0)
    for row in rows:
        if not isinstance(row, dict):
            return None
        index, status = row.get("span_index"), row.get("status")
        if type(index) is not int or index < 0 or index >= len(rows) or index in seen:
            return None
        if not isinstance(status, str) or status not in reasons:
            return None
        reason, kind = row.get("reason"), row.get("contract_kind")
        if not isinstance(reason, str) or reason not in reasons[status]:
            return None
        if status in {"satisfied", "violated"}:
            if not isinstance(kind, str) or kind not in {"literal", "json"}:
                return None
            if row.get("relationship") not in ("captured_input_output", "explicit_message_parent"):
                return None
        elif kind is not None:
            return None
        if type(row.get("identity_ambiguous")) is not bool:
            return None
        seen.add(index)
        totals[status] += 1
        safe.append(
            {
                "span_index": index,
                "status": status,
                "contract_kind": kind,
                "reason": reason,
                "identity_ambiguous": row["identity_ambiguous"],
            }
        )
    expected = {
        "trace_span_count": len(rows),
        "considered_count": len(rows) - totals["outside_scope"],
        "checked_count": totals["satisfied"] + totals["violated"],
        "unsupported_count": totals["unsupported"],
        "outside_scope_count": totals["outside_scope"],
    }
    if any(value[key] != count for key, count in expected.items()):
        return None
    return {
        "version": 1,
        "scope": "eligible_captured_response_pairs",
        **expected,
        "records": sorted(safe, key=lambda item: item["span_index"]),
        "business_semantics_assessed": False,
    }
