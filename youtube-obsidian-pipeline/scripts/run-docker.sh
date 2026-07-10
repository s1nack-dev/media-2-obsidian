#!/usr/bin/env bash
# Containerized deployment - host_bridge.py still runs ad hoc natively
# (Docker can't do Parakeet/MLX or the Claude CLI's subscription auth),
# but pipeline.py/fetch_playlist.py/server.py run in Docker, fronted by
# cloudflared for the public webhook.
#
# Usage:
#   scripts/run-docker.sh start   # (default) host_bridge.py + docker compose up
#   scripts/run-docker.sh stop    # docker compose down + stop host_bridge.py
#
# Requires .env filled in (see .env.example) and config.yaml's bridge.url
# left at its default (http://host.docker.internal:8081) - this script
# does NOT set BRIDGE_URL, unlike run-local.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

PID_FILE=".host_bridge.pid"
LOG_FILE="host_bridge.log"

require() {
    command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found on PATH" >&2; exit 1; }
}

bridge_port() {
    uv run python -c "
import yaml
print((yaml.safe_load(open('config.yaml')).get('bridge') or {}).get('port', 8081))
"
}

is_healthy() {
    curl -sf "http://127.0.0.1:${1}/healthz" >/dev/null 2>&1
}

start_host_bridge() {
    local port
    port="$(bridge_port)"

    if is_healthy "$port"; then
        echo "host_bridge.py already running and healthy on port ${port} - reusing it."
        return
    fi
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "error: $PID_FILE points at a live process but /healthz isn't responding - check $LOG_FILE" >&2
        exit 1
    fi
    echo "Starting host_bridge.py (native, ad hoc) on port ${port} - required even in Docker mode..."
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
}

stop_host_bridge() {
    if [ ! -f "$PID_FILE" ]; then
        echo "No $PID_FILE found - host_bridge.py wasn't started by this script, leaving it alone."
        return
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

cmd_start() {
    require op
    require uv
    require curl
    require docker
    [ -f config.yaml ] || { echo "error: config.yaml not found - copy from config.example.yaml first" >&2; exit 1; }
    [ -f .env ] || { echo "error: .env not found - copy from .env.example and fill in real values first" >&2; exit 1; }
    docker info >/dev/null 2>&1 || { echo "error: Docker daemon not running - start Docker Desktop first" >&2; exit 1; }

    start_host_bridge

    echo
    echo "Building and starting containers..."
    (cd docker && op run --env-file ../.env -- docker compose up -d --build)

    echo
    (cd docker && docker compose ps)
    echo
    echo "Ready. host_bridge.py + all containers are up."
}

cmd_stop() {
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        echo "Stopping containers..."
        (cd docker && docker compose down)
    else
        echo "Docker not available - skipping container teardown."
    fi
    stop_host_bridge
}

case "${1:-start}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    *) echo "usage: $0 [start|stop]" >&2; exit 1 ;;
esac
