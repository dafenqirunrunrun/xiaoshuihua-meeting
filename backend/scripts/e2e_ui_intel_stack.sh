#!/usr/bin/env bash
# Scratch environment for the UI-INTEL browser E2E suites (dev-only).
#
# Isolated ports so a developer's live :8000 / :5173 / :9222 are never touched.
# The three suites differ ONLY by port and by which backend harness module is
# loaded, so one launcher serves all of them:
#
#   variant   backend harness                          backend  vite   cdp
#   --------- ---------------------------------------- ------- ------ -----
#   intel1    scripts.e2e_ui_intel_app   (AGENT-1 面板)   :8001   :5174  :9222
#   intel2a   scripts.e2e_ui_intel_app   (2A 证据溯源)    :8001   :5175  :9223
#   intel2b   scripts.e2e_ui_intel_2b_app (V2 结构化证据) :8002   :5175  :9223
#
# intel1 and intel2a share the backend harness AND its port, so they are
# sequential, not concurrent: `up intel1` then the suite, then `up intel2a`
# (no teardown needed — same backend, same FakeProvider shape).
#
# Deliberately a dev-only launcher under scripts/ (NOT under tests/): it starts
# real servers and a real Chrome, so it has no business running in CI.
#
# usage:  bash backend/scripts/e2e_ui_intel_stack.sh up intel2b
#         bash backend/scripts/e2e_ui_intel_stack.sh down
set -u

VARIANT="${2:-intel2b}"

# `EXTRA_ENV` carries per-suite backend config that the suite itself assumes but
# cannot set. UI-INTEL-1's failure-path case (UI-I13) needs the agent's
# per-batch char guard reachable with the largest payload the ingress accepts
# (4000 chars), so it runs the backend with the bound lowered to 2000. That is a
# config knob, not an application change — but if it is missing, UI-I13/UI-I14
# fail with the state still ACTIVE, which looks like a product regression.
EXTRA_ENV=""

case "$VARIANT" in
  intel1)
    BACKEND_PORT=8001; APP_PORT=5174; CDP_PORT=9222; HARNESS=scripts.e2e_ui_intel_app
    EXTRA_ENV="MEETING_AGENT_MAX_BATCH_CHARS=2000"
    ;;
  intel2a)
    BACKEND_PORT=8001; APP_PORT=5175; CDP_PORT=9223; HARNESS=scripts.e2e_ui_intel_app
    ;;
  intel2b)
    BACKEND_PORT=8002; APP_PORT=5175; CDP_PORT=9223; HARNESS=scripts.e2e_ui_intel_2b_app
    ;;
  *) echo "usage: $0 {up|down} {intel1|intel2a|intel2b}" >&2; exit 2 ;;
esac

JOB_TMP="${CLAUDE_JOB_DIR:-/tmp}/tmp"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
PY="$BACKEND_DIR/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$BACKEND_DIR/.venv/bin/python"

mkdir -p "$JOB_TMP"
DB="$JOB_TMP/uiintel-$BACKEND_PORT.db"
CHROME_PROFILE="$JOB_TMP/chrome-uiintel-$CDP_PORT"
PID_PREFIX="$JOB_TMP/uiintel-$BACKEND_PORT"

# Kill whatever currently owns a port.
#
# Killing the recorded PIDs is not enough: on Windows vite/npm and Chrome
# re-parent their children, and a survivor keeps BOTH the port and (for Chrome)
# the profile lock. A surviving Chrome then makes the next `up` abort with
# "Lock file can not be created! Error code: 32" BEFORE it opens the debug port —
# which looks like a launcher bug but is really a leftover process. Port
# ownership is the reliable signal, so it is what we act on.
free_port() {
  for pid in $(netstat -ano 2>/dev/null | grep "LISTENING" | grep ":$1 " | awk '{print $NF}' | sort -u); do
    [ -n "$pid" ] && taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
  done
  return 0
}

up() {
  free_port "$BACKEND_PORT"
  free_port "$APP_PORT"
  free_port "$CDP_PORT"
  rm -f "$DB"
  rm -rf "$CHROME_PROFILE"
  mkdir -p "$CHROME_PROFILE"

  # --- backend (scratch DB, offline FakeProvider) ---
  (
    cd "$BACKEND_DIR" || exit 1
    # shellcheck disable=SC2086  # EXTRA_ENV is intentionally word-split
    env DATABASE_URL="sqlite:///$DB" \
      LLM_PROVIDER=fake \
      DEEPSEEK_API_KEY= \
      DEEPSEEK_BASE_URL= \
      $EXTRA_ENV \
      "$PY" -m uvicorn "$HARNESS:app" \
        --host 127.0.0.1 --port "$BACKEND_PORT" > "$PID_PREFIX-backend.log" 2>&1 &
    echo $! > "$PID_PREFIX-backend.pid"
  )
  # --- frontend (proxy -> the scratch backend) ---
  (
    cd "$FRONTEND_DIR" || exit 1
    VITE_API_BASE_URL="http://127.0.0.1:$BACKEND_PORT" \
      npm run dev -- --port "$APP_PORT" --strictPort > "$PID_PREFIX-vite.log" 2>&1 &
    echo $! > "$PID_PREFIX-vite.pid"
  )
  # --- Chrome with a dedicated profile + debug port ---
  # NOTE: on Windows the CDP endpoint only binds to localhost;
  # `--remote-debugging-address` is not supported, but 127.0.0.1 is exactly
  # what the suites dial.
  CHROME="${CHROME_BIN:-/c/Program Files/Google/Chrome/Application/chrome.exe}"
  "$CHROME" --headless=new --disable-gpu --remote-debugging-port="$CDP_PORT" \
    --user-data-dir="$(cygpath -w "$CHROME_PROFILE" 2>/dev/null || echo "$CHROME_PROFILE")" \
    --no-first-run --no-default-browser-check about:blank \
    > "$PID_PREFIX-chrome.log" 2>&1 &
  echo $! > "$PID_PREFIX-chrome.pid"

  # --- wait for readiness ---
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1 \
       && curl -sf "http://127.0.0.1:$APP_PORT/" >/dev/null 2>&1 \
       && curl -sf "http://127.0.0.1:$CDP_PORT/json/list" >/dev/null 2>&1; then
      echo "stack up [$VARIANT]: backend :$BACKEND_PORT ($HARNESS) | vite :$APP_PORT | cdp :$CDP_PORT"
      return 0
    fi
    sleep 1
  done
  echo "stack FAILED to come up; see $PID_PREFIX-*.log" >&2
  return 1
}

down() {
  # A variant's ports are derived from its name, so `down` must be given the
  # same variant (or just be run for each). Teardown is idempotent.
  free_port "$BACKEND_PORT"
  free_port "$APP_PORT"
  free_port "$CDP_PORT"
  for name in backend vite chrome; do
    f="$PID_PREFIX-$name.pid"
    [ -f "$f" ] && rm -f "$f"
  done
  sleep 1
  echo "stack down [$VARIANT] (:${BACKEND_PORT} :${APP_PORT} :${CDP_PORT})"
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 {up|down} {intel1|intel2a|intel2b}" >&2; exit 2 ;;
esac
