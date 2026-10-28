# syntax=docker/dockerfile:1
#
# Image for the containerized part of the pipeline: server.py (webhook
# mode) and fetch_playlist.py (playlist polling, run with --loop). Does
# not include Parakeet/MLX: LocalAI can provide transcription and chat, while
# the existing bridge and Claude CLI providers remain available during the
# migration.
#
# No 1Password CLI in this image, deliberately: secrets are resolved on
# the host (already authenticated via the desktop app's CLI integration)
# via `op run --env-file` before `docker compose up` ever runs, then
# passed into the containers as plain environment variables - see
# core.py's resolve_secret(). Simpler than a 1Password Service Account,
# and this image never needs `op` installed at all.
#
# Build:
#   docker build --build-arg CLAUDE_CODE_VERSION=<exact version> \
#     -t youtube-obsidian-pipeline -f Dockerfile .
# (build context is the repo root - see docker-compose.yml, which sets
#  `context: ..` from docker/)

FROM python:3.11-slim

ARG CLAUDE_CODE_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Pinned Claude CLI, installed as root before USER drops privileges. Update
# CLAUDE_CODE_VERSION deliberately after validating a new release.
RUN test -n "$CLAUDE_CODE_VERSION" \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps first (better layer caching). Base deps only - NOT the
# "mlx" extra (model inference happens in LocalAI or through the bridge).
ENV UV_NO_DEV=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked

COPY src/core.py src/pipeline.py src/fetch_playlist.py src/server.py src/localai_client.py src/bridge_client.py src/claude_client.py src/youtube_auth.py src/podcast_rss.py src/spotify_client.py ./src/
COPY prompts ./prompts/
COPY sandbox/claude.sb.tmpl ./sandbox/claude.sb.tmpl

ENV PATH="/app/.venv/bin:${PATH}" \
    DISABLE_AUTOUPDATER=1

# Run as an unprivileged user. Bind-mounted state, lock, token, and repository
# paths must be writable by this UID/GID (configurable at build time).
ARG PIPELINE_UID=1000
ARG PIPELINE_GID=1000
# Pre-create the pipeline-runtime mount point so it's owned by the pipeline
# user in the image - Docker copies a named volume's initial ownership from
# whatever's already at the mount point when it's first populated, and
# since this path is otherwise never created before that first mount, it
# would default to root:root (and the app can't write its lock file there).
RUN groupadd --gid "$PIPELINE_GID" pipeline \
    && useradd --uid "$PIPELINE_UID" --gid pipeline --create-home pipeline \
    && mkdir -p /app/.pipeline-runtime /app/data \
    && chown -R pipeline:pipeline /app
USER pipeline
ENV HOME=/home/pipeline

# Default: run the webhook server, bound to all interfaces (the compose
# network, not the host) since it's inside a container now. Override
# `command:` in docker-compose.yml for the fetch_playlist.py --loop
# service.
CMD ["python", "src/server.py", "--config", "config.yaml", "--host", "0.0.0.0"]
