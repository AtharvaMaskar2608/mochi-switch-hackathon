# Pidfile helpers sourced by up.sh and down.sh. Needs RUN (the .run directory).
# A pidfile is trusted only if that process, or one it started, runs the expected command.

marker() {
  case $1 in
    chief) echo '(^|[ /])chief( |$)' ;;
    standin-data) echo 'connectors\.standin_data' ;;
    switch) echo 'switch_core\.main' ;;
  esac
}
tree() { echo "$1"; local c; for c in $(pgrep -P "$1" 2>/dev/null || true); do tree "$c"; done; }
ours() {  # ours NAME: true if .run/NAME.pid is live and ours; a stale pidfile is removed
  local f=$RUN/$1.pid p
  [ -f "$f" ] || return 1
  for p in $(tree "$(cat "$f")"); do
    ps -o command= -p "$p" 2>/dev/null | grep -qE "$(marker "$1")" && return 0
  done
  rm -f "$f"
  return 1
}
killtree() { local c; for c in $(pgrep -P "$1" 2>/dev/null || true); do killtree "$c"; done; kill "$1" 2>/dev/null || true; }
