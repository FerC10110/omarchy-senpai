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
say "shell: two quick Alt+Q presses cycle the quality twice (real keys)"
quality() { omarchy-shell shell call "$ID" stateJson "" | python3 -c 'import json,sys; print(json.load(sys.stdin)["settings"]["quality"])'; }
if command -v wtype >/dev/null 2>&1; then
  before=$(quality)
  wtype -M alt -k q -m alt; sleep 0.06; wtype -M alt -k q -m alt
  sleep 1.5
  echo "quality: $before -> $(quality)  (expected: two steps along best,1080,720,480,360,worst)"
  for _ in 1 2 3 4 5 6 7; do
    [ "$(quality)" = "$before" ] && break
    omarchy-shell shell call "$ID" pressKey "alt+q" >/dev/null; sleep 0.5
  done
  echo "quality restored: $(quality)"
else
  echo "wtype not installed: skipped"
fi
say "shell: Alt+S walks the subtitle presets"
sublang() { omarchy-shell shell call "$ID" stateJson "" | python3 -c 'import json,sys; print(json.load(sys.stdin)["settings"]["subLang"])'; }
before=$(sublang)
case "$before" in
  "latino,es,en"|"es,en"|"en")
    omarchy-shell shell call "$ID" pressKey "alt+s" >/dev/null; sleep 0.2
    echo "subLang: $before -> $(sublang)"
    for _ in 1 2 3; do
      [ "$(sublang)" = "$before" ] && break
      omarchy-shell shell call "$ID" pressKey "alt+s" >/dev/null; sleep 0.5
    done
    echo "subLang restored: $(sublang)" ;;
  *) echo "custom subLang '$before': skipped so it is not replaced" ;;
esac
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
