#!/usr/bin/env bash
# Checks scripts/lib.sh under `set -e`, the way up.sh and down.sh run it. Run by tests/test_scripts.py.
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=$(mktemp -d)
. scripts/lib.sh
fail() { echo "FAIL: $*" >&2; exit 1; }

# wait_for returns 0 (and the script carries on) when the URL answers at once
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$RUN" >/dev/null 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true; rm -rf "$RUN"' EXIT
for _ in $(seq 50); do curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break; sleep 0.1; done
wait_for "http://127.0.0.1:$PORT/" 5 "test server"
echo "wait_for ok"

# a URL that never answers: wait_for dies with a message
if (wait_for "http://127.0.0.1:1/" 1 "nothing" 2>/dev/null); then fail "wait_for should die on timeout"; fi
echo "wait_for timeout ok"

# a pidfile naming an unrelated live process is not ours: removed, process untouched
sleep 30 & OTHER=$!
echo "$OTHER" > "$RUN/chief.pid"
if ours chief; then fail "unrelated pid trusted"; fi
[ ! -f "$RUN/chief.pid" ] || fail "stale pidfile kept"
kill -0 "$OTHER" || fail "unrelated process was touched"
kill "$OTHER"
echo "ours stale ok"

# start replaces a stale pidfile (a dead pid) instead of skipping, and returns 0
echo 999999 > "$RUN/standin-data.pid"
start standin-data sleep 30 >/dev/null
NEW=$(cat "$RUN/standin-data.pid")
kill "$NEW"
[ "$NEW" != 999999 ] || fail "start trusted a dead pid"
echo "start ok"
echo "all ok"
