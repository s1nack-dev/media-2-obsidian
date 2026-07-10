#!/usr/bin/env bash
# Local/native testing - no Docker at all. Starts host_bridge.py ad hoc
# (foreground service, not a launchd job) and points you at it via
# BRIDGE_URL for direct pipeline.py/fetch_playlist.py calls.
#
# Usage:
#   scripts/run-local.sh start   # (default) start host_bridge.py if not already up
#   scripts/run-local.sh stop    # stop the host_bridge.py this script started
#
# After start, in another terminal:
#   cd youtube-obsidian-pipeline
#   BRIDGE_URL=http://127.0.0.1:8081 op run -- uv run python pipeline.py --config config.yaml --input <path-or-url>
#   BRIDGE_URL=http://127.0.0.1:8081 op run -- uv run python fetch_playlist.py --config config.yaml
set -euo pipefail
cd "$(dirname "$0")/.."

PID_FILE=".host_bridge.pid"
LOG_FILE="host_bridge.log"

# require verifies that a command is available on PATH and exits with an error if it is missing.
require() {
    command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found on PATH" >&2; exit 1; }
}

# bridge_port reads the bridge port from config.yaml and outputs 8081 when no port is configured.
bridge_port() {
    uv run python -c "
import yaml
print((yaml.safe_load(open('config.yaml')).get('bridge') or {}).get('port', 8081))
"
}

# is_healthy checks whether the bridge health endpoint responds successfully for the specified port.
is_healthy() {
    curl -sf "http://127.0.0.1:${1}/healthz" >/dev/null 2>&1
}

# cmd_start starts or reuses a healthy host_bridge.py process and prints the command for connecting client scripts to it.
cmd_start() {
    require op
    require uv
    require curl
    [ -f config.yaml ] || { echo "error: config.yaml not found - copy from config.example.yaml first" >&2; exit 1; }

    local port
    port="$(bridge_port)"

    if is_healthy "$port"; then
        echo "host_bridge.py already running and healthy on port ${port} - reusing it."
    else
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "error: $PID_FILE points at a live process but /healthz isn't responding - check $LOG_FILE" >&2
            exit 1
        fi
        echo "Starting host_bridge.py (native, ad hoc) on port ${port}..."
        nohup op run -- uv run --extra mlx python host_bridge.py --config config.yaml \
            > "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"

        for _ in $(seq 1 30); do
            is_healthy "$port" && break
            sleep 1
        done
        if ! is_healthy "$port"; then
            echo "error: host_bridge.py didn't become healthy within 30s - check $LOG_FILE" >&2
            exit 1
        fi
        echo "host_bridge.py up (PID $(cat "$PID_FILE")), logging to $LOG_FILE."
    fi

    echo
    echo "Ready. Point pipeline.py/fetch_playlist.py at it with:"
    echo "  BRIDGE_URL=http://127.0.0.1:${port} op run -- uv run python pipeline.py --config config.yaml --input <path-or-url>"
}

# cmd_stop stops the host_bridge.py process recorded in the PID file and removes the PID file.
cmd_stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "No $PID_FILE found - nothing to stop (was host_bridge.py started by this script?)."
        exit 0
    fi
    local pid
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid"
        echo "Stopped host_bridge.py (PID $pid)."
    else
        echo "Process $pid not running (already stopped?)."
    fi
    rm -f "$PID_FILE"
}

case "${1:-start}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    *) echo "usage: $0 [start|stop]" >&2; exit 1 ;;
esac
