"""End-to-end local workflows using a captured multi-agent trace.

These tests exercise public loading, conversion, detection, collection, and
replay paths with the committed Omnigent capture. They do not replace networked
cloud integration tests, but they protect the full local user journey.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pisama_core.traces import SpanStatus, Trace

from pisama import analyze, async_analyze
from pisama._analyze import available_detectors
from pisama._atif import trace_from_atif
from pisama._loader import load_trace
from pisama._otel import trace_to_resource_spans
from pisama.cli.main import main
from pisama.collector.local_collector import LocalCollector
from pisama.collector.span_to_trace import (
    group_spans_to_traces,
    otel_span_to_pisama_span,
)
from pisama.mcp.server import LocalAnalyzer, _dispatch, create_local_server
from pisama.output.terminal import (
    WatchDisplay,
    display_analysis_result,
    display_comparison,
    display_detector_list,
    display_smoke_results,
)
from pisama.replay.comparator import ComparisonResult
from pisama.replay.smoke_runner import SmokeRunner
from pisama.replay.trace_fetcher import TraceFetcher


def _native_jsonl(trace: Trace) -> str:
    return "\n".join(json.dumps(span.to_dict()) for span in trace.spans)


def test_captured_atif_loads_consistently_from_all_public_inputs(
    captured_omnigent_trajectory: dict[str, Any],
    tmp_path: Path,
) -> None:
    expected = trace_from_atif(captured_omnigent_trajectory)
    atif_file = tmp_path / "captured-trajectory.json"
    atif_file.write_text(json.dumps(captured_omnigent_trajectory), encoding="utf-8")

    from_dict = load_trace(captured_omnigent_trajectory)
    from_json = load_trace(json.dumps(captured_omnigent_trajectory))
    from_file = load_trace(str(atif_file))
    same_object = load_trace(expected)

    assert same_object is expected
    assert {from_dict.trace_id, from_json.trace_id, from_file.trace_id} == {
        expected.trace_id
    }
    assert len(expected.spans) == 20
    assert len({span.span_id for span in expected.spans}) == 20
    assert sum(span.kind.value == "tool" for span in expected.spans) == 8
    assert {span.attributes["agent_name"] for span in expected.spans} == {
        "pisama_fixture_agent",
        "researcher",
    }


def test_native_trace_json_and_jsonl_round_trip_without_losing_errors(
    captured_omnigent_trajectory: dict[str, Any],
    tmp_path: Path,
) -> None:
    trace = trace_from_atif(captured_omnigent_trajectory)
    trace.spans[-1].status = SpanStatus.ERROR
    trace.spans[-1].error_message = "captured run terminated with a tool error"

    native_file = tmp_path / "trace.json"
    native_file.write_text(json.dumps(trace.to_dict()), encoding="utf-8")
    jsonl_file = tmp_path / "trace.jsonl"
    jsonl_file.write_text(_native_jsonl(trace), encoding="utf-8")

    native = load_trace(str(native_file))
    jsonl = load_trace(str(jsonl_file))
    assert native.trace_id == trace.trace_id
    assert len(jsonl.spans) == len(trace.spans)
    assert jsonl.spans[-1].status == SpanStatus.ERROR
    assert jsonl.spans[-1].error_message == "captured run terminated with a tool error"


def test_loader_rejects_ambiguous_or_invalid_inputs(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="Expected str, dict, or Trace"):
        load_trace(42)  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError, match="Trace file not found"):
        load_trace(str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="Trace JSON must contain an object"):
        load_trace("[1, 2, 3]")

    empty_jsonl = tmp_path / "empty.jsonl"
    empty_jsonl.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="JSONL file is empty"):
        load_trace(str(empty_jsonl))


@pytest.mark.asyncio
async def test_real_detector_pipeline_analyzes_captured_multi_agent_trace(
    captured_omnigent_trajectory: dict[str, Any],
) -> None:
    result = await async_analyze(captured_omnigent_trajectory)
    issue_types = {issue.type for issue in result.issues}

    assert result.detectors_run >= 30
    assert result.execution_time_ms > 0
    assert result.has_issues
    assert result.critical_issues
    assert {"context", "communication"}.issubset(issue_types)

    context_only = analyze(captured_omnigent_trajectory, detectors=["context"])
    assert context_only.detectors_run == 1
    assert {issue.type for issue in context_only.issues} <= {"context"}
    assert "context" in available_detectors()


@pytest.mark.asyncio
async def test_sync_analyze_is_safe_inside_an_active_event_loop(
    captured_omnigent_trajectory: dict[str, Any],
) -> None:
    result = analyze(captured_omnigent_trajectory, detectors=["communication"])
    assert result.detectors_run == 1
    assert result.trace_id


def test_cli_analyze_emits_machine_readable_results_for_captured_trace(
    captured_omnigent_trajectory: dict[str, Any],
    tmp_path: Path,
) -> None:
    path = tmp_path / "captured.json"
    path.write_text(json.dumps(captured_omnigent_trajectory), encoding="utf-8")

    result = CliRunner().invoke(main, ["analyze", str(path), "--json"])
    # The analyze command deliberately exits 1 when it finds issues, while
    # still emitting the complete JSON result for CI consumers.
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["trace_id"]
    assert payload["detectors_run"] >= 30
    assert any(issue["type"] == "context" for issue in payload["issues"])


def test_cli_check_and_detector_inventory_cover_the_ci_user_journey(
    captured_omnigent_trajectory: dict[str, Any],
    tmp_path: Path,
) -> None:
    trace_path = tmp_path / "captured.json"
    trace_path.write_text(json.dumps(captured_omnigent_trajectory), encoding="utf-8")
    ignored_dir = tmp_path / "node_modules"
    ignored_dir.mkdir()
    (ignored_dir / "not-a-trace.json").write_text("{}", encoding="utf-8")

    check = CliRunner().invoke(
        main,
        [
            "check",
            str(tmp_path),
            "--json",
            "--fail-on",
            "never",
            "--detectors",
            "context,communication",
        ],
    )
    assert check.exit_code == 0, check.output
    payload = json.loads(check.output)
    assert payload["schema_version"] == 2
    assert payload["summary"]["files_total"] == 1
    assert payload["summary"]["files_analyzed"] == 1
    assert payload["summary"]["passed"] is True
    assert {issue["type"] for issue in payload["results"][0]["issues"]} <= {
        "context",
        "communication",
    }

    detectors = CliRunner().invoke(main, ["detectors"])
    assert detectors.exit_code == 0
    assert "loop" in detectors.output
    assert "context" in detectors.output


@pytest.mark.asyncio
async def test_batch_smoke_comparison_and_terminal_views_use_real_results(
    captured_omnigent_trajectory: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured = trace_from_atif(captured_omnigent_trajectory)
    short_trace = Trace(
        trace_id=f"{captured.trace_id}-short",
        spans=[captured.spans[0]],
        metadata=captured.metadata,
    )
    full_result = await async_analyze(captured)
    short_result = await async_analyze(short_trace)

    comparison = ComparisonResult.compare(full_result, short_result)
    assert comparison.has_improvements
    assert {"context", "communication"}.intersection(comparison.fixed)
    reverse = ComparisonResult.compare(short_result, full_result)
    assert reverse.has_regressions

    smoke = await SmokeRunner().run([captured, short_trace], detectors=["context"])
    smoke_payload = smoke.to_dict()
    assert smoke.total_traces == 2
    assert smoke.traces_with_issues == 1
    assert smoke_payload["per_detector_stats"]["context"]["count"] == 1
    assert smoke_payload["per_detector_stats"]["context"]["max_severity"] >= 60

    display_analysis_result(full_result)
    display_detector_list(
        __import__(
            "pisama_core.detection.registry",
            fromlist=["registry"],
        ).registry.get_all()
    )
    display_comparison(
        comparison.trace_a_id,
        comparison.trace_b_id,
        comparison.fixed,
        comparison.improved,
        comparison.regressed,
        comparison.unchanged,
    )
    display_smoke_results(
        smoke.total_traces,
        smoke.traces_with_issues,
        {name: stats.to_dict() for name, stats in smoke.per_detector_stats.items()},
        smoke.critical_traces,
    )
    display = WatchDisplay(min_severity=30)
    display._use_live = False
    display.add_span(
        captured.spans[0].name,
        captured.spans[0].kind.value,
        captured.spans[0].status.value,
        captured.trace_id,
    )
    display.set_trace_count(1)
    display.add_issue(full_result.critical_issues[0])
    display.show_summary()

    terminal_output = capsys.readouterr().out
    assert "Pisama" in terminal_output
    assert "context" in terminal_output


@pytest.mark.asyncio
async def test_in_process_mcp_surface_uses_the_same_real_detector_pipeline(
    captured_omnigent_trajectory: dict[str, Any],
) -> None:
    analyzer = LocalAnalyzer()
    analysis = await analyzer.analyze_trace(captured_omnigent_trajectory)
    context = await analyzer.run_detector("context", captured_omnigent_trajectory)
    status = await analyzer.get_status()
    explanation = await analyzer.explain_failure("loop")
    unknown = await analyzer.explain_failure("not-a-detector")

    assert analysis["issues_detected"] >= 1
    assert context["detector_name"] == "context"
    assert context["detected"] is True
    assert status["total_analyses"] == 2
    assert status["total_issues"] >= 2
    assert status["detectors"]["total"] >= 30
    assert explanation["name"]
    assert "error" in unknown

    dispatched = await _dispatch(analyzer, "pisama_status", {})
    assert dispatched["total_analyses"] == 2
    with pytest.raises(ValueError, match="Unknown tool"):
        await _dispatch(analyzer, "pisama_not_real", {})
    with pytest.raises(ValueError, match="Unknown detector"):
        await analyzer.run_detector("not-a-detector", captured_omnigent_trajectory)

    server = create_local_server()
    assert server.name == "pisama-local"


def test_otel_export_collect_round_trip_preserves_failure_and_hierarchy(
    captured_omnigent_trajectory: dict[str, Any],
) -> None:
    trace = trace_from_atif(captured_omnigent_trajectory)
    failing = trace.spans[-1]
    failing.status = SpanStatus.ERROR
    failing.error_message = "captured failure"

    payload, exported_trace_id = trace_to_resource_spans(trace)
    otel_spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    converted = [otel_span_to_pisama_span(span) for span in otel_spans]
    regrouped = group_spans_to_traces(converted)

    assert len(regrouped) == 1
    assert regrouped[0].trace_id == exported_trace_id
    assert len(regrouped[0].spans) == len(trace.spans)
    assert converted[-1].status == SpanStatus.ERROR
    assert converted[-1].error_message == "captured failure"
    assert converted[1].parent_id == trace.spans[1].parent_id


def test_local_collector_accepts_real_otlp_http_payload(
    captured_omnigent_trajectory: dict[str, Any],
) -> None:
    trace = trace_from_atif(captured_omnigent_trajectory)
    payload, exported_trace_id = trace_to_resource_spans(trace)
    observed: list[str] = []
    collector = LocalCollector(on_span=lambda span: observed.append(span.span_id))
    port = collector.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/traces",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
            assert json.loads(response.read()) == {}

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            assert json.loads(response.read()) == {"status": "ok"}

        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/not-traces",
                    data=b"{}",
                    method="POST",
                ),
                timeout=3,
            )
        assert missing.value.code == 404
    finally:
        collector.stop()

    traces = collector.get_traces()
    assert collector.span_count == len(trace.spans)
    assert len(observed) == len(trace.spans)
    assert len(traces) == 1
    assert traces[0].trace_id == exported_trace_id


@pytest.mark.asyncio
async def test_replay_fetcher_reads_current_native_jsonl_exports(
    captured_omnigent_trajectory: dict[str, Any],
    tmp_path: Path,
) -> None:
    trace = trace_from_atif(captured_omnigent_trajectory)
    trace.spans[-1].status = SpanStatus.ERROR
    trace.spans[-1].error_message = "captured failure"
    (tmp_path / "traces-2026-07-23.jsonl").write_text(
        _native_jsonl(trace),
        encoding="utf-8",
    )

    fetcher = TraceFetcher(local_dir=tmp_path)
    by_prefix = await fetcher.get_trace(trace.trace_id[:12])
    recent = await fetcher.get_recent(n=5, framework="generic")

    assert by_prefix is not None
    assert by_prefix.trace_id == trace.trace_id
    assert by_prefix.spans[-1].status == SpanStatus.ERROR
    assert by_prefix.spans[-1].error_message == "captured failure"
    assert [item.trace_id for item in recent] == [trace.trace_id]


@pytest.mark.asyncio
async def test_replay_fetcher_reads_existing_sqlite_contract(
    captured_omnigent_trajectory: dict[str, Any],
    tmp_path: Path,
) -> None:
    trace = trace_from_atif(captured_omnigent_trajectory)
    db_path = tmp_path / "pisama.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE traces (
                trace_id TEXT, span_id TEXT, parent_id TEXT, timestamp TEXT,
                kind TEXT, status TEXT, tool_name TEXT, tool_input TEXT,
                tool_output TEXT, attributes TEXT, error TEXT
            )
            """
        )
        for span in trace.spans:
            connection.execute(
                "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace.trace_id,
                    span.span_id,
                    span.parent_id,
                    span.start_time.isoformat(),
                    span.kind.value,
                    span.status.value,
                    span.name,
                    json.dumps(span.input_data),
                    json.dumps(span.output_data),
                    json.dumps(span.attributes),
                    span.error_message,
                ),
            )

    fetcher = TraceFetcher(local_dir=tmp_path)
    loaded = await fetcher.get_trace(trace.trace_id)
    recent = await fetcher.get_recent(n=2, framework="claude_code")

    assert loaded is not None
    assert len(loaded.spans) == len(trace.spans)
    assert loaded.spans[0].start_time <= datetime.now(timezone.utc)
    assert len(recent) == 1
