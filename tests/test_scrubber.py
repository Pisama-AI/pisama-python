"""Tests for pisama.scrubber."""

from __future__ import annotations

import json

from pisama.scrubber import ScrubReport, format_report, scrub_file, scrub_text, scrub_trace


class TestScrubText:
    def test_email(self):
        out = scrub_text("contact alice@example.com for access")
        assert "alice@example.com" not in out
        assert "<email>" in out

    def test_aws_access_key(self):
        out = scrub_text("key=AKIAIOSFODNN7EXAMPLE more text")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "<token>" in out

    def test_openai_key(self):
        out = scrub_text("openai key sk-abc123def456ghi789jkl012")
        assert "sk-abc123def456ghi789jkl012" not in out
        assert "<token>" in out

    def test_jwt(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        out = scrub_text(f"auth {jwt} end")
        assert jwt not in out
        assert "<jwt>" in out

    def test_bearer_header(self):
        out = scrub_text("Authorization: Bearer abc123def456ghi789")
        assert "<auth>" in out

    def test_ssn(self):
        out = scrub_text("ssn 123-45-6789 recorded")
        assert "123-45-6789" not in out
        assert "<ssn>" in out

    def test_ip(self):
        out = scrub_text("request from 192.168.1.42 ok")
        assert "192.168.1.42" not in out
        assert "<ip>" in out

    def test_phone_na(self):
        out = scrub_text("call (415) 555-1234 tomorrow")
        assert "<phone>" in out

    def test_empty_string_passthrough(self):
        assert scrub_text("") == ""

    def test_no_match_unchanged(self):
        assert scrub_text("hello world") == "hello world"

    def test_report_counts(self):
        report = ScrubReport()
        scrub_text("a@b.com and c@d.com", report)
        assert report.replacements["<email>"] == 2


class TestScrubTrace:
    def test_nested_dict(self):
        data = {
            "user": {"email": "alice@example.com", "ssn": "111-22-3333"},
            "notes": ["call 555-123-4567", "ok"],
        }
        out = scrub_trace(data)
        assert out["user"]["email"] == "<email>"
        assert out["user"]["ssn"] == "<ssn>"
        assert "<phone>" in out["notes"][0]

    def test_sensitive_key_redacted(self):
        data = {"Authorization": "something", "api_key": "sk-xyz", "normal": "hello"}
        out = scrub_trace(data)
        assert out["Authorization"] == "<redacted>"
        assert out["api_key"] == "<redacted>"
        assert out["normal"] == "hello"

    def test_input_not_mutated(self):
        data = {"email": "alice@example.com"}
        scrub_trace(data)
        assert data["email"] == "alice@example.com"

    def test_non_string_values_passthrough(self):
        data = {"count": 42, "flag": True, "items": [1, 2, 3]}
        assert scrub_trace(data) == data


class TestScrubFile:
    def test_json_file(self, tmp_path):
        src = tmp_path / "trace.json"
        src.write_text(json.dumps({"user_email": "bob@example.com"}))
        dst = tmp_path / "trace.scrubbed.json"
        report = scrub_file(src, dst)
        out = json.loads(dst.read_text())
        assert out["user_email"] == "<email>"
        assert report.replacements["<email>"] == 1

    def test_jsonl_file(self, tmp_path):
        src = tmp_path / "trace.jsonl"
        src.write_text(
            json.dumps({"email": "a@b.com"}) + "\n" + json.dumps({"email": "c@d.com"}) + "\n"
        )
        dst = tmp_path / "trace.scrubbed.jsonl"
        report = scrub_file(src, dst)
        lines = [json.loads(line) for line in dst.read_text().splitlines() if line.strip()]
        assert len(lines) == 2
        assert lines[0]["email"] == "<email>"
        assert lines[1]["email"] == "<email>"
        assert report.replacements["<email>"] == 2


def test_format_report_empty():
    assert format_report(ScrubReport()) == "No PII patterns matched."


def test_format_report_with_counts():
    r = ScrubReport()
    r.record("<email>", 3)
    r.sensitive_keys_redacted = 1
    out = format_report(r)
    assert "<email>: 3" in out
    assert "<redacted>" in out
