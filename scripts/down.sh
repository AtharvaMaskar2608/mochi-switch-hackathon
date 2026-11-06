#!/usr/bin/env bash
# Stop what scripts/up.sh started: chief, standin-data, Switch core, then Switch's containers.
set -euo pipefail
cd "$(dirname "$0")/.."
SWITCH_DIR=${SWITCH_DIR:-$PWD/.switch}

killtree() { local c; for c in $(pgrep -P "$1" || true); do killtree "$c"; done; kill "$1" 2>/dev/null || true; }
for name in chief standin-data switch; do
  f=.run/$name.pid
  if [ -f "$f" ]; then killtree "$(cat "$f")"; rm -f "$f"; echo "stopped $name"; fi
done
if [ -d "$SWITCH_DIR" ]; then (cd "$SWITCH_DIR" && just down); fi
