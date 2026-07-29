# Changelog

All notable changes to the `pisama` meta-package are documented here. The package follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.6.0] - 2026-07-29

### Added

- `pisama.auto` -- consolidated in-package original of the standalone
  `pisama-auto` distribution (zero-code auto-instrumentation for the
  Anthropic/OpenAI SDKs, tracing to either the Pisama platform or a
  self-hosted OTLP collector). New `auto` extra:
  `pip install "pisama[auto]"`.
- `pisama.agents` -- consolidated in-package original of the standalone
  `pisama-agent-sdk` distribution (real-time hooks, tools, and a
  `pisama-openhands-monitor` console script for Claude Agent SDK / Harbor
  integrations). New `agents` and `telemetry` extras:
  `pip install "pisama[agents]"`.
- `pisama.agents.__version__`, aliased to `pisama.__version__` since this
  code no longer releases independently.

Both submodules keep publishing as standalone distributions
(`pisama-auto`, `pisama-agent-sdk`) for this release; those packages become
thin shims re-exporting this module in a later release. A bare
`pip install pisama` pulls in no new required dependencies -- `pisama.auto`
and `pisama.agents` stay behind their respective extras.

## [0.5.7] - 2026-07-29

### Changed

- Disclose the precision boundary of the archived TRAIL run beside the F1
  numbers in the README. The archive scored only annotated errors, so no false
  positive was recordable (`fp = 0` in 14 of 14 categories, `prediction_count`
  equals `mapped_annotations` at 813). Precision is 1.000 by construction rather
  than by measurement, F1 reduces to `2R / (1 + R)`, and the informative figure
  is micro-recall 0.5953. The macro-F1 0.7535 and micro-F1 0.7463 values remain
  arithmetically reproducible; they simply carry no precision information. This
  caveat already shipped in the sibling `pisama-detectors` README and in
  `benchmarks/evidence.json`, and is now carried by the package people install.

## [0.5.6] - 2026-07-28

### Fixed

- Pin `mcp>=1.0.0,<2` so `pip install "pisama[mcp]"` stops resolving mcp 2.0.0,
  which removed the 1.x decorator API that `pisama mcp-server` uses.
- Correct the published README: drop four detectors that do not exist
  (`delegation`, `grounding`, `retrieval_quality`, `compaction_quality`), drop a
  false framework-specific-detectors claim, and correct the TRAIL table.

## [0.5.5] - 2026-07-25

### Changed

- Publish releases through an isolated, tag-verified trusted-publishing
  workflow with complete package tests before the PyPI job can start.

## [0.5.4] - 2026-07-23

### Added

- Add CodeQL, dependency review, and a full-package coverage regression gate.
- Raise full-package test coverage from 23.46% to more than 60% with captured
  Omnigent workflow tests covering ATIF loading, local detection, MCP, OTLP
  collection, replay storage, CI checks, and terminal output.

### Changed

- Require `pisama-core` 1.8.2 so installs include the latest security and
  ingestion-contract hardening.
- Update pinned CI and trusted-publishing actions.

### Fixed

- Preserve named OTLP error statuses when collecting Pisama's own exports.
- Preserve native `error_message` fields when replaying JSONL traces.
- Keep an explicitly selected replay directory isolated from unrelated user
  stores and safely sort mixed timezone-aware and legacy naive timestamps.

## [0.5.3] - 2026-07-23

### Changed

- Published the MIT package source in its own public repository.
- Required `pisama-core` 1.8.1, which contains the Omnigent ingestion API and
  parses standard `Z` timestamps consistently across every supported Python
  version.
- Added standalone CI, type checking, clean-wheel verification, and community
  governance files.
- Corrected the archived TRAIL benchmark figure and linked to its public
  evidence.

## [0.5.2] - 2026-07-23

### Fixed

- Restored compatibility with the published `pisama-core` 1.7.3 package. The
  Omnigent command is registered when its core 1.8 ingestion module is
  available, while all other CLI commands and ATIF platform mapping remain
  usable on core 1.7.3.
- Added an installed-wheel CLI smoke test to prevent a package from passing
  source tests while failing immediately for PyPI users.

### Changed

- Declared Python 3.13 support and corrected the package changelog and issue
  tracker links.

## [0.3.0] - 2026-05-04

### Added

- **`pisama check <path>` CLI command.** Walks a directory or single file, runs the public `pisama._analyze.analyze` API on every saved trace it finds, exits non-zero when any finding meets the configured severity threshold. Useful as a CI gate.
  - Options: `--fail-on {info, warning, error, critical, never}` (default `warning`), `--json` for machine-readable output, `--quiet` to suppress per-file output.
  - Auto-skips noisy directories (`.git`, `.venv`, `node_modules`, `__pycache__`, `.next`, `dist`, `build`).
  - Supports OTEL, Langfuse, Phoenix, and raw JSON trace formats via the existing `pisama._analyze` plumbing.

### Notes

- The richer build-time static analysis surface (parsing n8n workflow JSON or LangGraph Python AST directly) lives in the backend repo today and is not exposed via `pip install pisama`. Targeted for a future release once the architectural extraction into `pisama-core` lands.

## [0.2.0] - earlier

- Initial public release: `pisama analyze`, `pisama detectors`, `pisama mcp-server`, `pisama scrub`, `pisama watch`, `pisama replay`, `pisama smoke` CLI commands.
