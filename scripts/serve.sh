#!/usr/bin/env bash
# Manage THIS project's dev server (uvicorn app.main:app).
#
# Scope/safety: tracks the server it starts via a PID file in the repo. The
# port-based fallback only kills a process whose command line contains
# "uvicorn app.main:app" (this project's server) — it will not touch unrelated
# processes or other projects' servers.
#
# Usage: scripts/serve.sh {start|stop|restart|status}
#        PORT=8123 scripts/serve.sh start   # override the port (default 8001)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

PORT="${PORT:-8001}"
PIDFILE="$REPO/.server.pid"
LOGFILE="$REPO/.server.log"
PY="$REPO/.venv/bin/python"
APP="app.main:app"

_running_pid() {
  # Echo a live PID for this app's server, or nothing.
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    cat "$PIDFILE"; return
  fi
  # Fallback: a uvicorn for THIS app on THIS port, started outside serve.sh.
  pgrep -f "uvicorn .*${APP}.*--port ${PORT}\b" 2>/dev/null | head -n1 || true
}

start() {
  local pid; pid="$(_running_pid)"
  if [ -n "$pid" ]; then echo "already running (pid $pid on :$PORT)"; exit 0; fi
  STOCKFISH_PATH="${STOCKFISH_PATH:-$(command -v stockfish || true)}" \
    nohup "$PY" -m uvicorn "$APP" --port "$PORT" >"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 1
  echo "started (pid $(cat "$PIDFILE") on :$PORT) — logs: .server.log"
}

stop() {
  local pid; pid="$(_running_pid)"
  if [ -z "$pid" ]; then echo "not running"; rm -f "$PIDFILE"; return; fi
  # Only ever kill this app's server (verified by command line above).
  kill "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "stopped (pid $pid)"
}

status() {
  local pid; pid="$(_running_pid)"
  if [ -n "$pid" ]; then echo "running (pid $pid on :$PORT)"; else echo "not running"; fi
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  *) echo "usage: scripts/serve.sh {start|stop|restart|status}"; exit 2 ;;
esac
