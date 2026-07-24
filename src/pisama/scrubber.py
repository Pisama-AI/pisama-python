"""PII scrubber for production traces.

When a design partner shares real traces so Pisama can calibrate on
non-synthetic data, they almost always need PII stripped first. This
module does a best-effort regex scrub in-process so the trace never
leaves the partner's environment with identifying data attached.

This is defense-in-depth, not a compliance tool. Real redaction for
regulated data should still go through the partner's own DLP pipeline.

Scrubbed patterns:
    - email addresses                   -> <email>
    - phone numbers (E.164 + NA)        -> <phone>
    - SSN (XXX-XX-XXXX)                 -> <ssn>
    - credit card numbers (13-19 digit) -> <card>
    - AWS/GCP/generic API tokens        -> <token>
    - bearer/Basic auth headers         -> <auth>
    - IPv4 addresses                    -> <ip>
    - JWT tokens (3-segment b64)        -> <jwt>
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("<email>", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("<jwt>", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("<auth>", re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=_\-\.]{16,}\b", re.IGNORECASE)),
    ("<token>", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("<token>", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("<token>", re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),
    ("<token>", re.compile(r"\bxox[aboprs]-[A-Za-z0-9-]{10,}\b")),
    ("<card>", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("<ssn>", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("<phone>", re.compile(r"\+\d{1,3}[ -]?\(?\d{1,4}\)?[ -]?\d{3,4}[ -]?\d{3,4}\b")),
    ("<phone>", re.compile(r"\b\(?\d{3}\)?[ -.]\d{3}[ -.]\d{4}\b")),
    ("<ip>", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)

_SENSITIVE_KEYS: Tuple[str, ...] = (
    "authorization",
    "x-api-key",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "cookie",
    "set-cookie",
)


@dataclass
class ScrubReport:
    """Summary of what the scrubber replaced."""

    replacements: Dict[str, int] = field(default_factory=dict)
    sensitive_keys_redacted: int = 0

    def record(self, placeholder: str, count: int) -> None:
        if count > 0:
            self.replacements[placeholder] = self.replacements.get(placeholder, 0) + count

    @property
    def total(self) -> int:
        return sum(self.replacements.values()) + self.sensitive_keys_redacted


def scrub_text(text: str, report: ScrubReport | None = None) -> str:
    """Scrub PII patterns from a single string."""
    if not text:
        return text
    out = text
    for placeholder, pattern in _PATTERNS:
        out, count = pattern.subn(placeholder, out)
        if report is not None:
            report.record(placeholder, count)
    return out


def scrub_trace(data: Any, report: ScrubReport | None = None) -> Any:
    """Recursively scrub a trace (dict, list, or string).

    Sensitive keys (authorization, api_key, password, ...) have their values
    replaced entirely with <redacted> regardless of content.

    The input is not mutated; a scrubbed copy is returned.
    """
    if report is None:
        report = ScrubReport()
    return _walk(data, report)


def _walk(node: Any, report: ScrubReport) -> Any:
    if isinstance(node, dict):
        return {k: _walk_value(k, v, report) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, report) for v in node]
    if isinstance(node, str):
        return scrub_text(node, report)
    return node


def _walk_value(key: str, value: Any, report: ScrubReport) -> Any:
    if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS and value not in (None, ""):
        report.sensitive_keys_redacted += 1
        return "<redacted>"
    return _walk(value, report)


def scrub_file(src: Path, dst: Path) -> ScrubReport:
    """Read a .json or .jsonl trace from src, write a scrubbed copy to dst."""
    report = ScrubReport()
    text = src.read_text(encoding="utf-8")

    if src.suffix == ".jsonl":
        out_lines: List[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            scrubbed = scrub_trace(data, report)
            out_lines.append(json.dumps(scrubbed, ensure_ascii=False))
        dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        return report

    data = json.loads(text)
    scrubbed = scrub_trace(data, report)
    dst.write_text(json.dumps(scrubbed, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def format_report(report: ScrubReport) -> str:
    """Human-readable summary of a scrub report."""
    if report.total == 0:
        return "No PII patterns matched."
    parts: List[str] = []
    for placeholder, count in sorted(report.replacements.items()):
        parts.append(f"{placeholder}: {count}")
    if report.sensitive_keys_redacted:
        parts.append(f"<redacted> keys: {report.sensitive_keys_redacted}")
    return "Scrubbed " + ", ".join(parts)


__all__ = [
    "ScrubReport",
    "scrub_text",
    "scrub_trace",
    "scrub_file",
    "format_report",
]
