#!/usr/bin/env bash
# One-time cutover (2026-09-22): copy the live runtime state from jarvis-demo
# into CYGNUS. jarvis-demo was the copy in daily use until today, so its memory,
# sessions, logs, registries and .env are newer.
#
# Every CYGNUS file that gets replaced is first copied to
# .recovery/cutover-2026-09-22/cygnus-before/ and checked byte for byte; the
# copy stops if any backup does not match. Tracked files (.gitignore,
# opencode.json) are not touched.
#
#   bash scripts/cutover_sync.sh            show what would change
#   bash scripts/cutover_sync.sh --apply    back up, then copy
set -euo pipefail

J=/c/Users/deped/Documents/jarvis-demo
C=/c/Users/deped/Documents/CYGNUS
B="$C/.recovery/cutover-2026-09-22/cygnus-before"
LIST="$(mktemp)"

if tasklist //FO CSV | grep -qi electron.exe; then
  echo "Cygnus desktop app is running. Quit it first." >&2; exit 1
fi

list() {  # files under $1, skipping code, dependencies and screenshots
  (cd "$1" && find . \( -path ./.venv -o -path './.venv.*' -o -path ./desktop/node_modules \
      -o -path ./.git -o -name __pycache__ -o -path ./models -o -path ./.recovery \
      -o -path ./.pytest_cache -o -path ./.agentloop -o -path ./playwright_shots \
      -o -path ./.preport-backup \) -prune -o -type f -print | sort)
}
list "$J" | while read -r f; do
  case "$f" in ./.gitignore|./opencode.json|./nul) continue ;; esac
  [ -e "$C/$f" ] && cmp -s "$J/$f" "$C/$f" && continue
  git -C "$C" ls-files --error-unmatch "${f#./}" >/dev/null 2>&1 && continue  # tracked: leave to git
  echo "$f"
done > "$LIST"

echo "$(wc -l < "$LIST") files differ or are missing in CYGNUS:"
sed 's/^/  /' "$LIST"
[ "${1:-}" = "--apply" ] || { echo; echo "Dry run. Re-run with --apply."; exit 0; }

while read -r f; do
  if [ -e "$C/$f" ]; then
    mkdir -p "$B/$(dirname "$f")"; cp -p "$C/$f" "$B/$f"
    cmp -s "$C/$f" "$B/$f" || { echo "backup failed for $f, nothing copied" >&2; exit 1; }
  fi
done < "$LIST"
echo "backup verified in $B"

while read -r f; do
  mkdir -p "$C/$(dirname "$f")"; cp -p "$J/$f" "$C/$f"
  cmp -s "$J/$f" "$C/$f" || { echo "copy failed for $f" >&2; exit 1; }
done < "$LIST"
echo "copied $(wc -l < "$LIST") files from jarvis-demo; all match"
