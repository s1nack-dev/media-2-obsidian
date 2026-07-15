#!/bin/zsh
# Installed outside the repository by the macOS deployment runbook. The
# service account owns this script, bridge.env, model cache, and logs.
set -euo pipefail

SERVICE_HOME="__SERVICE_HOME__"
REPO_PATH="__REPO_PATH__"
PROFILE="$SERVICE_HOME/transcription.sb"
ENV_FILE="$SERVICE_HOME/bridge.env"
PYTHON_BIN="$REPO_PATH/.venv/bin/python"

if [[ ! -r "$ENV_FILE" || ! -r "$PROFILE" || ! -x "$PYTHON_BIN" ]]; then
  print -u2 "Bridge environment, Seatbelt profile, or project virtualenv is missing. Refusing to start."
  exit 78
fi

set -a
source "$ENV_FILE"
set +a

if [[ -z "${BRIDGE_AUTH_TOKEN:-}" ]]; then
  print -u2 "BRIDGE_AUTH_TOKEN is missing. Refusing to start."
  exit 78
fi

export HOME="$SERVICE_HOME"
export TMPDIR="$SERVICE_HOME/tmp"
export HF_HOME="$SERVICE_HOME/huggingface"
mkdir -p "$TMPDIR" "$HF_HOME"

exec /usr/bin/sandbox-exec -f "$PROFILE" \
  "$PYTHON_BIN" "$REPO_PATH/src/host_bridge.py" \
  --config "$REPO_PATH/config.yaml" --host 127.0.0.1 --port 8081
