#!/usr/bin/env bash
# Dev-only: run ruff + pytest. Not deployed to rip machines.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> ruff check"
uv run --with ruff ruff check scripts/ tests/

echo "==> ruff format --check"
uv run --with ruff ruff format --check scripts/ tests/

echo "==> pytest"
uv run --with pytest --with rapidfuzz --with beautifulsoup4 --with requests \
  --with typer --with tqdm \
  pytest tests/ "$@"
