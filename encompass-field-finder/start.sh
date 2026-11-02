#!/usr/bin/env bash
# Run Encompass Field Finder on this computer (macOS / Linux).
set -euo pipefail
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --quiet --upgrade pip
.venv/bin/python -m pip install --disable-pip-version-check --quiet -e ".[app,yaml]"
exec .venv/bin/python -m encompass_field_finder.app "$@"
