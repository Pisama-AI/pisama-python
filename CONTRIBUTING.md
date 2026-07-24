# Contributing to pisama

Thanks for helping improve the `pisama` Python package.

## Good contribution areas

- trace loaders and framework adapters
- CLI and MCP usability
- redaction and privacy safeguards
- documentation and reproducible bug reports

Calibration datasets, hosted services, and managed automation are outside this
repository's scope.

## Development

```bash
git clone https://github.com/Pisama-AI/pisama-python.git
cd pisama-python
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,mcp]"
ruff check src tests
mypy src/pisama
pytest -q
```

Please include a focused test for behavior changes and verify a clean wheel
build with `python -m build`.

By submitting a contribution, you agree to license it under MIT.
