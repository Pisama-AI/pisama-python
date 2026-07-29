"""Pisama -- Multi-agent failure detection for production AI systems.

Example:
    import pisama

    result = pisama.analyze("trace.json")
    for issue in result.issues:
        print(f"[{issue.type}] {issue.summary}")
"""

try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("pisama")
except PackageNotFoundError:  # running from a source checkout without an install
    __version__ = "0.6.0"

from pisama._analyze import AnalyzeResult, Issue, analyze, async_analyze
from pisama._http import PisamaAuthError
from pisama._loader import load_trace
from pisama.client import AsyncClient, Client, Detection, PlatformResult
from pisama.scrubber import scrub_file, scrub_trace

__all__ = [
    "__version__",
    "analyze",
    "async_analyze",
    "load_trace",
    "scrub_file",
    "scrub_trace",
    "AnalyzeResult",
    "Issue",
    "Client",
    "AsyncClient",
    "Detection",
    "PlatformResult",
    "PisamaAuthError",
]
