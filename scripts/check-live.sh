#!/usr/bin/env bash
# Live checks against the installed plugin: needs a running omarchy-shell.
set -euo pipefail
ID="io.github.ferc10110.senpai"
DIR="$HOME/.config/omarchy/plugins/$ID"
HELPER="$DIR/bin/senpai"
say() { printf '\n== %s\n' "$*"; }

say "helper: home"
python3 "$HELPER" home | head -c 400; echo
say "helper: search"
python3 "$HELPER" search "one piece" | head -c 400; echo
say "helper: status"
python3 "$HELPER" status
say "shell: rescan + toggle"
omarchy-shell shell rescanPlugins
omarchy-shell shell toggle "$ID" '{}'
sleep 1
omarchy-shell shell call "$ID" stateJson ""
echo
say "shell: type a query"
omarchy-shell shell call "$ID" setQuery "frieren"
sleep 4
omarchy-shell shell call "$ID" stateJson ""
echo
say "shell: a newer query supersedes a slower one"
omarchy-shell shell call "$ID" setQuery "one"
sleep 0.6
omarchy-shell shell call "$ID" setQuery "one punch man"
sleep 6
omarchy-shell shell call "$ID" stateJson ""
echo
omarchy-shell shell call "$ID" setQuery "frieren"
sleep 4
say "shell: open episodes"
omarchy-shell shell call "$ID" pressKey "enter"
sleep 4
omarchy-shell shell call "$ID" stateJson ""
echo
if command -v grim >/dev/null 2>&1; then
  say "screenshot"
  grim "${1:-/tmp/senpai-preview.png}" && echo "saved ${1:-/tmp/senpai-preview.png}"
fi
say "shell: close"
omarchy-shell shell call "$ID" pressKey "escape"
omarchy-shell shell call "$ID" pressKey "escape"
omarchy-shell shell call "$ID" pressKey "escape"
echo "done"
