#!/usr/bin/env bash
# Create a development virtualenv and install OpenADB in editable mode.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
echo "Done. Run:  source .venv/bin/activate && python -m openadb"
