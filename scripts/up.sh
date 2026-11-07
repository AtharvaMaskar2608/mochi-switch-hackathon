#!/usr/bin/env bash
# Judge path: Switch + its bundled Mattermost + Chief, ready for story 1.
# usage: bash scripts/up.sh      (stop everything with: bash scripts/down.sh)
# env:   SWITCH_DIR     Switch checkout (default ./.switch, cloned if missing)
#        SWITCH_REF     Switch commit to check out on a fresh clone (default: the commit Chief was tested on)
#        SWITCH_DB_PORT host port for Switch's Postgres. Set it (e.g. SWITCH_DB_PORT=5433) when a local
#                       Postgres already holds port 5432; up.sh moves Switch's DB_PORT and compose port to it.
# Never prints a secret: logins live in Switch's .env and .local-test-users.json (mode 600).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
SWITCH_DIR=${SWITCH_DIR:-$ROOT/.switch}
SWITCH_REF=${SWITCH_REF:-a425936d615a153dc24ee0d309ca515572bfff48}  # Switch moves fast: pin what was tested
RUN=$ROOT/.run
mkdir -p "$RUN"

. scripts/lib.sh  # die, wait_for, start, pidfile checks
for tool in docker just uv node git openssl curl perl; do
  command -v "$tool" >/dev/null || die "missing prerequisite: $tool"
done
docker info >/dev/null 2>&1 || die "docker is not running"

# --- keys ------------------------------------------------------------------------------
# read in subshells: the keys go only to chief and standin-data, never into Switch's environment
[ -f .env ] || cp .env.example .env
has_key() { (set -a; . ./.env; [ -n "${!1:-}" ]); }
has_key ANTHROPIC_API_KEY || die "put ANTHROPIC_API_KEY in $ROOT/.env"
has_key TYPESAFE_API_KEY || echo "note: no TYPESAFE_API_KEY, so Chief gates with Claude instead of Jev (if it offers, reply yes)"

# --- Switch checkout and its .env -----------------------------------------------------
if [ ! -d "$SWITCH_DIR/.git" ]; then
  git clone -q https://github.com/sandbox-quantum/switch "$SWITCH_DIR"
  git -C "$SWITCH_DIR" checkout -q "$SWITCH_REF" || die "Switch has no commit $SWITCH_REF"
elif [ "$(git -C "$SWITCH_DIR" rev-parse HEAD)" != "$(git -C "$SWITCH_DIR" rev-parse "$SWITCH_REF^{commit}" 2>/dev/null)" ]; then
  echo "note: $SWITCH_DIR is not at SWITCH_REF $SWITCH_REF; using it as it is"
fi
cd "$SWITCH_DIR"
[ -f .env ] || just init-env >/dev/null  # its output includes the admin password
set_env() {  # set_env KEY VALUE: replace the line in Switch's .env, or append it
  if grep -q "^$1=" .env; then K=$1 V=$2 perl -pi -e 's/^\Q$ENV{K}\E=.*/$ENV{K}=$ENV{V}/' .env
  else printf '%s=%s\n' "$1" "$2" >> .env; fi
}
set_env FEATURE_FLAGS_ENABLED agent_management,service_connections
grep -q '^CONTROLLER_TOKEN_SECRET=.' .env || set_env CONTROLLER_TOKEN_SECRET "$(openssl rand -hex 32)"
if [ -n "${SWITCH_DB_PORT:-}" ]; then
  set_env DB_PORT "$SWITCH_DB_PORT"
  # the compose file pins the host port, so move it too
  P=$SWITCH_DB_PORT perl -pi -e 's/:\d+:5432"/:$ENV{P}:5432"/' deploy/local/docker-compose.yml
fi
port() { grep "^$1=" .env | cut -d= -f2; }
API_PORT=$(port API_HOST_PORT || true); API="http://localhost:${API_PORT:-8000}"
MM_PORT=$(port MATTERMOST_HOST_PORT || true); MM="http://localhost:${MM_PORT:-8065}"

# --- Switch: services, migrations, core ------------------------------------------------
if curl -fsS "$API/health" >/dev/null 2>&1; then
  echo "Switch is already running at $API"
else
  echo "starting Switch services (Postgres, Mattermost); the first build takes a few minutes"
  touch "$RUN/started-switch"  # down.sh stops Switch only if this script started it
  just up > "$RUN/switch-up.log" 2>&1 || die "just up failed (see .run/switch-up.log)"
  for i in $(seq 30); do
    just migrate > "$RUN/switch-migrate.log" 2>&1 && break
    if [ "$i" = 30 ]; then die "just migrate failed (see .run/switch-migrate.log)"; fi
    sleep 2
  done
  start switch just run
  wait_for "$API/health" 600 "Switch core"
fi
if ! curl -fsS "$MM/api/v4/system/ping" >/dev/null 2>&1; then  # e.g. Mattermost crashed under emulation
  just up >> "$RUN/switch-up.log" 2>&1 || die "just up failed (see .run/switch-up.log)"
fi
wait_for "$MM/api/v4/system/ping" 300 "Mattermost"

# --- users, agents, org, addressing, stand-in db --------------------------------------
cd "$ROOT"
uv sync -q
SWITCH_DIR="$SWITCH_DIR" uv run python scripts/setup_demo.py

# --- Chief and the stand-in data agent ------------------------------------------------
(
  set -a; . ./.env; set +a  # the project keys, for these two processes only
  export SWITCH_URL=$API CHIEF_DATA_AGENT=standin-data
  start standin-data uv run python -m connectors.standin_data
  start chief uv run chief
)

cat <<EOF

Ready. Open Mattermost: $MM
Log in as ravi. The password is "ravi.password" in $ROOT/.local-test-users.json.
Then:
  1. In the product channel, type:  @chief where is the refunds data and what did it do last month?
  2. Chief either opens a data-question room or offers to. If it offers, reply in its thread:  yes
  3. Open the new work-data-question-... channel: Chief's brief, then standin-data's answer table.
  4. Back in product: Chief posts the table in your thread, restyled for ravi.
Logs: .run/   Stop everything: bash scripts/down.sh
EOF
