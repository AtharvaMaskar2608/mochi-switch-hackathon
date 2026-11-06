#!/usr/bin/env bash
# Stop what scripts/up.sh started: chief, standin-data, Switch core, and Switch's containers
# only if up.sh brought them up (.run/started-switch). A Switch that was already running is left alone.
set -euo pipefail
cd "$(dirname "$0")/.."
SWITCH_DIR=${SWITCH_DIR:-$PWD/.switch}
RUN=$PWD/.run
. scripts/lib.sh

for name in chief standin-data switch; do
  if ours "$name"; then killtree "$(cat "$RUN/$name.pid")"; rm -f "$RUN/$name.pid"; echo "stopped $name"; fi
done
if [ -f "$RUN/started-switch" ]; then
  (cd "$SWITCH_DIR" && just down)
  rm -f "$RUN/started-switch"
fi
