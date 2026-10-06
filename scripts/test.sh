#!/usr/bin/env bash
# Everything that can run without the shell: Python helper tests, JS model tests, manifest checks.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
echo "[1/3] helper and manifest tests"
python3 -m unittest discover -s tests -v 2>&1 | tail -3
echo "[2/3] model tests"
node --test tests/
echo "[3/3] plugin validation"
if command -v omarchy-plugin-validate >/dev/null 2>&1; then omarchy-plugin-validate "$ROOT"; else echo "omarchy-plugin-validate not installed; skipped"; fi
rm -rf tests/__pycache__ bin/__pycache__
echo "All checks passed."
