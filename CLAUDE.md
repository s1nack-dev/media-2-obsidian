# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A personal automation pipeline that turns media into an Obsidian note: it gets a transcript (existing subtitles/captions, or local speech-to-text via Parakeet/MLX when none exist), summarizes it via the Claude Code CLI, commits the transcript to a GitHub subtitles repo, and commits a markdown note to an Obsidian vault GitHub repo.

**Requires an Apple Silicon Mac** for the transcription/summarization backend (`host_bridge.py`) — Parakeet needs MLX (Metal/Neural Engine, no Linux/Windows/Intel-Mac support), and the Claude CLI's subscription OAuth is tied to this Mac's keychain/session. Everything else (`pipeline.py`, `fetch_playlist.py`, `server.py`) can run natively OR in Docker containers — see "Two deployment modes" below. The `systemd/` unit files predate the MLX requirement and assume a Linux deployment; they're stale until ported to `launchd`.

Five entry points, all in `youtube-obsidian-pipeline/`:
- `fetch_playlist.py` — polls a private YouTube playlist for new videos (scheduled via cron, or `--loop` in a container), then calls into `pipeline.py` per video.
- `pipeline.py` — processes one input at a time (local file, YouTube URL, or any other link yt-dlp can handle); runnable standalone via `--input`, or imported as a library by `fetch_playlist.py`/`server.py`. Has no MLX/parakeet or claude-CLI dependency itself — talks to `host_bridge.py` over HTTP for both (see `bridge_client.py`), which is what makes it containerizable.
- `server.py` — optional webhook mode: a stdlib-only HTTP server exposing `POST /process` behind a Cloudflare Tunnel (`cloudflared`, see `docker/`). Requests are auth-token-gated, queued, and processed one at a time by a background worker thread calling `process_input()`.
- `host_bridge.py` — **must run natively on the Mac** (not in Docker). Exposes transcription (Parakeet/MLX, via `transcribe_backend.py`) and summarization/tagging (Claude CLI, via `core.py`) over HTTP on `127.0.0.1`, auth-token-gated. `pipeline.py`/`bridge_client.py` call this instead of doing either in-process — that split is what lets `pipeline.py`, `fetch_playlist.py`, and `server.py` run in a Linux container despite the pipeline's two host-tied dependencies.
- `youtube_auth.py` — one-time OAuth setup, unrelated to the above.

The root `main.py` referenced in git status does not exist yet.

## Two deployment modes

**Native** (simplest, no Docker): `fetch_playlist.py`/`pipeline.py`/`server.py` run directly on the Mac alongside `host_bridge.py`, all talking to each other over `127.0.0.1`. This is what steps 1-8 in README.md set up.

**Containerized** (optional, README step 9): `pipeline.py`/`fetch_playlist.py`/`server.py` run in Docker (see `Dockerfile`, `docker/docker-compose.yml`) alongside `cloudflared`; only `host_bridge.py` stays native, reached via `host.docker.internal`. Same `config.yaml` works for both — `bridge.url` is the only value that differs (`http://127.0.0.1:8081` native, `http://host.docker.internal:8081` containerized), and the containerized config.yaml currently has it set to the container value, which the native path doesn't use since `pipeline.py`/`server.py` aren't meant to run both ways simultaneously off one config.

## Running the pipeline

```bash
cd youtube-obsidian-pipeline
uv sync --extra mlx              # host: installs parakeet-mlx too (needed for host_bridge.py)
uv sync                          # container/base: skips parakeet-mlx (no Linux wheels, and pipeline.py doesn't need it)
op run -- uv run python host_bridge.py --config config.yaml          # must be running before anything below works
op run -- uv run python fetch_playlist.py --config config.yaml       # playlist mode (scheduled)
op run -- uv run python pipeline.py --config config.yaml --input <path-or-url>   # one-off mode
```

First-time auth (needs a browser — run on a machine with one, copy token to server):
```bash
uv run python youtube_auth.py --config config.yaml
```

## Architecture

- `core.py` — shared, source-agnostic helpers used everywhere: `load_config`/`load_state`/`save_state`, `resolve_secret(env_var, op_ref)` (prefers an already-resolved env var over calling `op_read()` - lets the same call sites work natively, where `op` is authenticated via the desktop app, and in a container, where docker-compose has already injected the value resolved on the host via `op run --env-file`, with no branching or 1Password Service Account needed), `notify()` (best-effort Slack webhook + SMTP email, never raises), `run_git`/`ensure_repo`/`commit_and_push` (git helpers; `ensure_repo()` hard-resets the local clone to `origin/<branch>` before every write, and checks for a real `.git` dir rather than just path existence, since Docker bind-mounts auto-create an empty host directory before a container's first run ever gets a chance to clone into it — checking `path.exists()` alone would wrongly take the "update existing clone" branch on a fresh container deployment), `srt_to_plain_text()`, `summarize_with_claude()` (shells out to `claude -p "..."` with a decision-focused meeting-summary prompt, transcript truncated to 150k chars; retries once if the response is missing the expected `## SUMMARY` heading, which happens if an ambient hook/plugin injects meta-commentary instead of a real response — see `host_bridge.py`, this only runs there now), `generate_tags_with_claude()` (separate `claude -p "..."` call asking for 3-8 lowercase/hyphenated content tags; sanitized via `_sanitize_tag()`, rejects sentence-like lines as a defense against the same meta-commentary pollution), `slugify()`, `build_note()`.
- `transcribe_backend.py` — host-only, wraps `parakeet-mlx`. `transcribe_audio()` returns `(srt_text, plain_text)`; model cached per `model_id` so a multi-video run doesn't reload it each time. `get_model()` flips `huggingface_hub.constants.HF_HUB_OFFLINE` directly (not `os.environ` — that constant is read once at import time and setting the env var later has no effect) for the load attempt, falling back to a real download only the first time a given model isn't cached. Used exclusively by `host_bridge.py`.
- `host_bridge.py` — host-native HTTP service: `POST /transcribe?model_id=...` (raw audio bytes, calls `transcribe_backend.py`), `POST /summarize` / `POST /tags` (JSON `{"transcript": ...}`, calls `core.py`'s claude functions), `GET /healthz`. Single-threaded/synchronous on purpose (no queue) — the caller (`pipeline.py`, inside the container) is already the one blocking on it as part of its own single-item processing. Auth-token-gated (`bridge.auth_token_op_ref`), separate token from `server.py`'s webhook (different trust boundary — this one's only reachable from the Docker network, not the public internet).
- `bridge_client.py` — container-safe HTTP client for `host_bridge.py`: `transcribe_audio()`, `summarize()`, `generate_tags()`. No MLX/parakeet or claude-CLI dependency — this is what `pipeline.py` actually calls.
- `pipeline.py` — `process_input(raw_input, cfg, item_hint=None, github_token=None, bridge_token=None)` is the core single-item pipeline: detects input type (`detect_input_type()`: existing local path / `youtube.com`|`youtu.be` URL / other http(s) link), gets a transcript (yt-dlp subtitles first, falling back to `bridge_client.transcribe_audio()` when there are none), summarizes via `bridge_client`, then commits the transcript file and the note. Raises `NoTranscriptAvailableError` when nothing could be transcribed at all (maps to "skip, don't retry" for callers); any other exception is treated as transient/retryable. `overcast.fm` links get special-cased via `resolve_overcast_episode()`: it scrapes the RSS feed link + episode title off the Overcast page, matches the title against the feed's `<item>` titles, and downloads the matched `<enclosure>` mp3 directly (falls back to normal generic-link handling if any step fails).
- `fetch_playlist.py` — YouTube-API-specific: `get_youtube_service()`/`get_playlist_items()` (OAuth token in `token.json`, refreshed automatically), diffs the playlist against `state.json`, then calls `process_input()` per new video inside a retry/notify loop (`run_once()`). `main()` either calls `run_once()` once (default) or loops it forever on `--interval` seconds (`--loop`, for containerized deployment where nothing else can schedule it) — a single failed pass inside the loop is caught, notified, and retried next interval rather than killing the process.
- **State** (`state.json`, owned by `fetch_playlist.py` only) — tracks `processed_video_ids` (set) and `failed_attempts` (dict). Videos are retried up to `max_retries` times, then permanently marked processed and a notification is sent. `pipeline.py`'s one-off `--input` runs and `server.py`'s webhook jobs are both stateless (no dedup, no retry-tracking — `server.py` just notifies on success/failure per job).

## Config and secrets

- `config.yaml` (git-ignored) — copy from `config.example.yaml`; controls repo URLs, paths, branch names, Claude command, transcription model, retry count, webhook/bridge settings, and notification settings
- `transcription.model` — Hugging Face repo id for the Parakeet model (default: `mlx-community/parakeet-tdt-0.6b-v3`); read by `pipeline.py` (picks the model) and `host_bridge.py`/`transcribe_backend.py` (runs it)
- `claude.command` — only read by `host_bridge.py` now, not `pipeline.py`/`server.py`/`fetch_playlist.py` directly
- `bridge.url` / `bridge.port` / `bridge.auth_token_op_ref` — `host_bridge.py`'s own address and auth as seen by `pipeline.py`
- `webhook.port` / `webhook.auth_token_op_ref` — only used by `server.py`; a separate random shared secret from the bridge token, sent as `Authorization: Bearer <token>`
- Secrets are injected via 1Password (`op read op://...`, see `op_read()`/`resolve_secret()` in `core.py`) — GitHub token, Google OAuth client id/secret, webhook auth token, bridge auth token. Natively, `op_read()` shells out directly (desktop-app CLI integration handles auth). **In containers**, there's no `op` binary at all — `GITHUB_TOKEN`/`BRIDGE_AUTH_TOKEN`/`WEBHOOK_AUTH_TOKEN` env vars (in `.env`, holding the *same* `op://` refs as `config.yaml`) get resolved on the host via `op run --env-file` before `docker compose up` ever runs, then passed into the containers as plain env vars that `resolve_secret()` picks up instead of calling `op_read()`. No 1Password Service Account needed.
- `SMTP_PASSWORD` env var — only needed if email notifications are enabled
- `TUNNEL_TOKEN` env var (in `.env`, git-ignored) — only needed for `docker/docker-compose.yml` (cloudflared)
- `SUBTITLES_REPO_HOST_PATH` / `VAULT_REPO_HOST_PATH` env vars (in `.env`) — must exactly match `config.yaml`'s `github.subtitles_repo_path`/`vault_repo_path`; bind-mounted into the containers at the same absolute path so `config.yaml` needs no container-specific path overrides
- `token.json` — Google OAuth refresh token, produced by `youtube_auth.py`; git-ignored

## Scheduling

Native: `fetch_playlist.py` is scheduled via cron; `pipeline.py --input` is for manual one-off runs. `host_bridge.py` is deliberately run ad hoc, not as a persistent/managed service (no launchd job) — start it manually (`op run -- uv run python host_bridge.py --config config.yaml &`) before using `pipeline.py`/`fetch_playlist.py`/`server.py` (native or containerized), stop it whenever.

Containerized: `docker-compose.yml` runs `pipeline-server` (server.py) and `pipeline-fetch` (`fetch_playlist.py --loop`) as always-on services (`restart: unless-stopped`), so neither needs external scheduling. `host_bridge.py` still has to be started manually first (see above) - the containers will fail every job with a connection error to `host.docker.internal` until it's up.

`systemd/` files are stale regardless of mode (Linux-only, predate the MLX requirement).

## Containerized deployment details

`Dockerfile` builds a single image used by both `pipeline-server` and `pipeline-fetch` (docker-compose overrides `command:` per service). Base Python deps only — `pyproject.toml`'s `mlx` extra (`parakeet-mlx`) is deliberately excluded from the image, since MLX has no Linux wheels and the container never needs it (talks to `host_bridge.py` over HTTP instead). No 1Password CLI in the image either - see `resolve_secret()` above.

`docker/docker-compose.yml` bind-mounts `config.yaml` (read-only), `state.json`, and `token.json` from the parent directory — one shared copy, no drift between `host_bridge.py` and the containers. The two GitHub repo clone directories are bind-mounted at the *same absolute host paths* named in `config.yaml` (via `SUBTITLES_REPO_HOST_PATH`/`VAULT_REPO_HOST_PATH`), for the same reason.

`cloudflared` and the pipeline containers share a compose network; the Cloudflare tunnel's Public Hostname should point at `http://pipeline-server:8080` (the compose service name), not `host.docker.internal` — that hostname is only meaningful for the containers-to-host-bridge hop.
