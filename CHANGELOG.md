# Changelog

All notable changes to the `pisama` meta-package are documented here. The package follows [Semantic Versioning](https://semver.org/).

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
