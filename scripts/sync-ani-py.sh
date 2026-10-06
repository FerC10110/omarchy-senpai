#!/usr/bin/env bash
# Copy ani_py.py from the development checkout into bin/, never downgrading.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:-$HOME/Projects/anime/ani-py/ani_py.py}"
TARGET="$ROOT/bin/ani_py.py"
version() { grep -m1 '^VERSION = ' "$1" | sed 's/^VERSION = "\(.*\)"/\1/'; }
[ -f "$SOURCE" ] || { echo "sync-ani-py: $SOURCE not found" >&2; exit 1; }
grep -q -- '--headless' "$SOURCE" || { echo "sync-ani-py: $SOURCE has no --headless mode" >&2; exit 1; }
if [ -f "$TARGET" ]; then
  old="$(version "$TARGET")"; new="$(version "$SOURCE")"
  if [ "$(printf '%s\n%s\n' "$old" "$new" | sort -V | tail -1)" != "$new" ]; then
    echo "sync-ani-py: refusing to go from $old down to $new" >&2; exit 1
  fi
fi
cp "$SOURCE" "$TARGET" && chmod +x "$TARGET"
python3 -m py_compile "$TARGET" && rm -rf "$ROOT/bin/__pycache__"
echo "bin/ani_py.py is now $(version "$TARGET")"
