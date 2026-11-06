# Helpers sourced by up.sh and down.sh (and tests/test_scripts.sh). Needs RUN (the .run directory).
# Each function ends on an explicit status, so a false test never leaks out under `set -e`.

die() { echo "up.sh: $*" >&2; exit 1; }

wait_for() {  # wait_for URL SECONDS WHAT: 0 once URL answers; a dot every 10 s; dies on timeout
  local i
  for i in $(seq "$2"); do
    if curl -fsS "$1" >/dev/null 2>&1; then
      if [ "$i" -gt 10 ]; then echo; fi
      return 0
    fi
    if [ $((i % 10)) = 0 ]; then printf .; fi
    sleep 1
  done
  echo
  die "$3 did not come up at $1 (logs in $RUN)"
}

# A pidfile is trusted only if that process, or one it started, runs the expected command.
marker() {
  case $1 in
    chief) echo '(^|[ /])chief( |$)' ;;
    standin-data) echo 'connectors\.standin_data' ;;
    switch) echo 'switch_core\.main' ;;
  esac
}
tree() { echo "$1"; local c; for c in $(pgrep -P "$1" 2>/dev/null || true); do tree "$c"; done; }
ours() {  # ours NAME: 0 if .run/NAME.pid is live and ours; a stale pidfile is removed
  local f=$RUN/$1.pid p
  if [ ! -f "$f" ]; then return 1; fi
  for p in $(tree "$(cat "$f")"); do
    if ps -o command= -p "$p" 2>/dev/null | grep -qE "$(marker "$1")"; then return 0; fi
  done
  rm -f "$f"
  return 1
}
killtree() { local c; for c in $(pgrep -P "$1" 2>/dev/null || true); do killtree "$c"; done; kill "$1" 2>/dev/null || true; }

start() {  # start NAME CMD...: run in the background, log to .run/NAME.log
  local name=$1; shift
  if ours "$name"; then echo "$name already running"; return 0; fi
  nohup "$@" > "$RUN/$name.log" 2>&1 &
  echo $! > "$RUN/$name.pid"
  echo "started $name (log: .run/$name.log)"
}
