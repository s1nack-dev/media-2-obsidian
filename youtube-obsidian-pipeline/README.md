# Media → Transcript + AI Summary → GitHub + Obsidian

Two ways to use this:

- **Playlist mode** (`fetch_playlist.py`): add a video to a private YouTube
  playlist. Run on a schedule (cron/systemd) on your own server, it picks
  up new videos automatically.
- **One-off mode** (`pipeline.py --input ...`): manually process a single
  local video/audio file, a YouTube URL, or pretty much any other link
  (podcast page, Vimeo, direct `.mp4`/`.mp3` link, etc.).

Either way, the pipeline gets a transcript (existing subtitles/captions if
available, otherwise downloads audio and transcribes it locally with
Parakeet via MLX), summarizes it with Claude, commits the transcript to a
GitHub repo, and adds a note to your Obsidian vault (also a GitHub repo)
with the source link, a link to the transcript, and the summary.

## What you need

- **An Apple Silicon Mac (M1/M2/M3/M4)** with Python 3.11+, `git`, and
  internet access. Local transcription uses MLX, which only runs on
  Apple Silicon — this pipeline can no longer run on a Linux server or
  Intel Mac (the `systemd/` unit files assume a Linux deployment and are
  stale now; use `launchd` or plain cron on macOS instead).
- [`uv`](https://docs.astral.sh/uv/) for dependency management:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- [`ffmpeg`](https://ffmpeg.org/) — used by `yt-dlp` to convert subtitles to
  SRT and extract audio for transcription:
  ```bash
  brew install ffmpeg
  ```
- [1Password CLI (`op`)](https://developer.1password.com/docs/cli/get-started/) — used to inject secrets at runtime. Install and sign in once:
  ```bash
  brew install 1password-cli
  op signin
  ```
- The private YouTube playlist you'll add videos to.
- Two GitHub repos: one to hold raw subtitle files, one that is your
  Obsidian vault (or a folder within it). These can be the same repo if you
  prefer — just point both config entries at it with different subfolders.
- A Claude Pro/Max subscription (used via Claude Code CLI, no separate API
  billing) or an Anthropic API key if you'd rather pay per-token instead —
  see step 5 for both options.
- Only for [containerized deployment / webhook mode](#9-optional-containerized-deployment--webhook-mode)
  (step 9, optional): [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  and a [Cloudflare](https://dash.cloudflare.com/sign-up) account with a
  domain on it (for the tunnel). No 1Password Service Account needed —
  secrets are resolved on the host and passed into the containers as
  plain env vars (see step 9).

Total setup time: ~20-30 minutes, one-time.

## Development security checks

The repository uses pre-commit to run Bandit against Python files,
Hadolint and Trivy against Docker configuration, and both detect-secrets
and TruffleHog against potential credentials. TruffleHog verification is
disabled so candidate credentials are never sent to external services during
a commit. Hadolint and Trivy run in pinned containers, so Docker Desktop must
be running when Docker-related
files are checked. The first run also downloads the pinned hook environments
and scanner images.

Install and enable the hooks from the repository root:

```bash
brew install pre-commit
pre-commit install
pre-commit run --all-files
```

Run `detect-secrets scan --baseline .secrets.baseline` only when deliberately
refreshing the reviewed secret baseline. Never baseline a real credential.

---

## 1. Google Cloud: enable the YouTube Data API

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and
   create a new project (or reuse one).
2. Go to **APIs & Services > Library**, search "YouTube Data API v3", click
   **Enable**.
3. Go to **APIs & Services > OAuth consent screen**.
   - User type: **External** (unless you have a Workspace account).
   - App name: anything, e.g. "YouTube Pipeline".
   - Add your own Google account under **Test users** (this keeps the app
     in "Testing" mode, which is fine — no Google review needed for
     personal use).
4. Go to **APIs & Services > Credentials > Create Credentials > OAuth
   client ID**.
   - Application type: **Desktop app**.
   - Download the JSON and open it — you need two values from it:
     `client_id` and `client_secret` (inside the `"installed"` key).
   - Store both as fields in a 1Password item, e.g. "youtube-2-obsidian".
   - Set `youtube.client_id_op_ref` and `youtube.client_secret_op_ref` in
     `config.yaml` to point at those fields (e.g.
     `op://Private/youtube-2-obsidian/client id`).

## 2. Get your playlist ID

Create (or pick) a private playlist in YouTube, e.g. "To Summarize". Open
it — the URL looks like:

```
https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxxxxxxxxxx
```

The `list=` value is your `playlist_id`.

## 3. Authorize the app (one-time)

This has to happen somewhere with a web browser, so if your server is
headless, run this step on your laptop instead, then copy the resulting
`token.json` to the server.

```bash
uv sync
op run -- uv run python youtube_auth.py --config config.yaml
```

A browser window opens, asks you to log in and approve read-only access to
your YouTube account, then writes `token.json`. Copy `token.json` to the
server if you ran this step elsewhere. The refresh token inside it keeps
working indefinitely — the pipeline refreshes it automatically on each run.

## 4. GitHub repos + token

1. Create (or pick) two repos: e.g. `youtube-subtitles` and your Obsidian
   vault repo (e.g. `obsidian-vault`).
2. Create a token: **GitHub Settings > Developer settings > Personal
   access tokens > Fine-grained tokens**. Grant it **Contents:
   Read and write** permission scoped to those two repos.
3. Store the token in 1Password and set `github.token_op_ref` in
   `config.yaml` to point at it (e.g.
   `op://Private/youtube-2-obsidian/github fine grain`).

## 5. Claude Code CLI (for summarization)

Each processed item makes two `claude -p` calls: one for the summary
(decision-focused meeting-summary format — SUMMARY, KEY DISCUSSION POINTS,
DECISIONS MADE, OPEN QUESTIONS/RISKS, ACTION ITEMS) and one that generates
3-8 content-specific tags (e.g. `oauth2`, `product-roadmap`), which get
merged into the note's `tags:` frontmatter alongside the base
`video-summary`/`{source_type}` tags.

Install and log in once, interactively:

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

Choose to log in with your Claude.ai account (Pro/Max subscription). This
stores credentials locally; after this, `claude -p "..."` runs headlessly
from cron using your subscription — no API key or extra billing needed.

(If you'd rather use the Anthropic API and pay per token instead, that's a
small code change in `summarize_with_claude()` in `core.py` — let me know
and I can swap it in.)

## 6. Install dependencies and configure

```bash
uv sync   # creates .venv and installs yt-dlp, parakeet-mlx, google auth libs, etc. from uv.lock
cp config.example.yaml config.yaml
```

Edit `config.yaml`:
- `youtube.playlist_id` — from step 2
- `youtube.client_id_op_ref` / `youtube.client_secret_op_ref` — from step 1
- `github.token_op_ref` — from step 4
- `github.subtitles_repo_url` / `github.vault_repo_url` — your two repos
- `github.vault_notes_dir` — subfolder inside the vault where notes land
- `transcription.model` — the Hugging Face repo id for the Parakeet model;
  the default (`mlx-community/parakeet-tdt-0.6b-v3`) works well out of the box.
- Everything else has sane defaults.

Note: the first time transcription actually runs for a given
`transcription.model`, it's downloaded (~2.3 GB for the default model)
from Hugging Face to its local cache — that first run will be slower and
needs network access. This isn't a hang. Every run after that loads the
model straight from the local cache with no network calls at all —
transcription itself always runs fully locally regardless (no audio is
ever sent anywhere).

## 7. Test it manually

Playlist mode:
```bash
op run -- uv run python fetch_playlist.py --config config.yaml
```

Add one video to your playlist first so there's something to process.

One-off mode, for a single input (no playlist/state involved):
```bash
op run -- uv run python pipeline.py --config config.yaml --input "https://www.youtube.com/watch?v=..."
op run -- uv run python pipeline.py --config config.yaml --input ./some-local-video.mp4
op run -- uv run python pipeline.py --config config.yaml --input "https://example.com/some-podcast-episode"
```

Check:
- A `.srt` transcript file shows up in your subtitles repo.
- A markdown note shows up in your vault repo with the source link,
  transcript link, and summary.
- (Playlist mode only) `state.json` now lists that video's ID so it won't
  be reprocessed.

If your vault repo is what Obsidian actually opens, pull the change into
your local Obsidian vault folder (`git pull`), or install the **Obsidian
Git** community plugin so it syncs automatically.

## 8. Schedule it

This schedules **playlist mode** (`fetch_playlist.py`) — one-off mode
(`pipeline.py --input ...`) is for manual runs and isn't meant to be
scheduled.

Pick one:

### Option A: cron

```bash
crontab -e
```

Add (runs every 30 minutes):

```
*/30 * * * * cd /path/to/youtube-obsidian-pipeline && /usr/local/bin/op run -- /root/.local/bin/uv run python fetch_playlist.py --config config.yaml >> pipeline.log 2>&1
```

Cron runs with a minimal environment, so use full paths for both `op` and
`uv` (find them with `which op` and `which uv`). `op run --` injects your
1Password secrets before the pipeline starts. Adjust the path and interval
to taste. Check `pipeline.log` after the first scheduled run.

### Option B: systemd service + timer (Linux only — not applicable now)

`systemd` doesn't exist on macOS, and local transcription now requires an
Apple Silicon Mac (see "What you need" above), so this option is stale
until it's ported to `launchd`. Use Option A (cron) on macOS for now.

Files are in `systemd/`. Steps (for a hypothetical Linux deployment):

1. Copy this whole project to somewhere permanent, e.g. `/opt/youtube-obsidian-pipeline`.
2. Ensure `op` is installed and signed in on the server (`op signin`).
3. Edit `systemd/youtube-pipeline.service` — check `WorkingDirectory` and
   `ExecStart` match where you actually put the project. Prefix the
   `ExecStart` command with the full path to `op run --` so secrets are
   injected at runtime.
4. Install and enable:
   ```bash
   sudo cp systemd/youtube-pipeline.service /etc/systemd/system/
   sudo cp systemd/youtube-pipeline.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now youtube-pipeline.timer
   ```
5. Check it:
   ```bash
   systemctl list-timers youtube-pipeline.timer   # confirm it's scheduled
   sudo systemctl start youtube-pipeline.service  # run once now, manually
   journalctl -u youtube-pipeline.service -f      # tail logs
   ```

The timer runs every 30 minutes by default (`OnUnitActiveSec=30min` in
`youtube-pipeline.timer`) — edit that file to change the interval.

---

## 9. Optional: containerized deployment + webhook mode

Everything except Parakeet and the Claude CLI can run in Docker:
`server.py` (webhook mode, `POST /process` triggers a single URL/file the
same way `pipeline.py --input` does) and `fetch_playlist.py --loop`
(playlist polling, containers have no external scheduler to reach them so
it polls itself in a loop instead of relying on cron) both run as
containers, alongside `cloudflared` for exposing `server.py` on a public
URL via [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

**Why not everything:** Parakeet needs direct Metal/Neural Engine access
(Docker Desktop's Linux VM can't provide that), and the Claude CLI's
subscription login is tied to this Mac's keychain/session (a container
can't share it). Both stay behind `host_bridge.py`, a small HTTP service
that runs natively on the Mac — the containers call it over HTTP via
`host.docker.internal` instead of doing either in-process. **Start
`host_bridge.py` before the containers** — they'll fail every job with a
connection error to `host.docker.internal` until it's up.

You don't need this section at all if you're happy with native
`fetch_playlist.py` (cron) + `pipeline.py --input` (manual) as set up in
steps 1-8 above — containerizing is only worth it if you specifically
want the webhook (a public URL you can hit to trigger processing) and
would rather manage that as Docker services than as more native
processes.

### Setup

1. Generate two random secrets and store them in 1Password (don't reuse
   one for both — different trust boundaries, see security notes):
   ```bash
   openssl rand -hex 32   # webhook.auth_token_op_ref - reachable from the public internet
   openssl rand -hex 32   # bridge.auth_token_op_ref - only reachable from the Docker network
   ```
   Point `webhook.auth_token_op_ref` and `bridge.auth_token_op_ref` in
   `config.yaml` at them.

2. Run `host_bridge.py` natively, ad hoc (start it manually before using
   the containers, stop it whenever — no `launchd`/persistent service):
   ```bash
   uv sync --extra mlx   # only needed here - the mlx extra isn't installed by default
   op run -- uv run python host_bridge.py --config config.yaml &
   curl http://127.0.0.1:8081/healthz   # confirm it's up before continuing
   ```

3. Create the tunnel in the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/):
   **Networks > Tunnels > Create a tunnel**, choose the **Docker**
   connector, and copy the token it gives you. Add a **Public Hostname**
   pointing at `http://pipeline-server:8080` — that's the Docker Compose
   service name (see `docker/docker-compose.yml`), resolved via Docker's
   internal network, not `host.docker.internal` (that's only for the
   *host_bridge* hop, not this one).

4. Copy `.env.example` to `.env` and fill in:
   - `TUNNEL_TOKEN` (from step 3)
   - `GITHUB_TOKEN` / `BRIDGE_AUTH_TOKEN` / `WEBHOOK_AUTH_TOKEN` — the
     *same* `op://` references already in `config.yaml`'s
     `github.token_op_ref` / `bridge.auth_token_op_ref` /
     `webhook.auth_token_op_ref`. No 1Password Service Account needed —
     `op run --env-file` (next step) resolves these on the host, already
     authenticated via the desktop app's CLI integration, and passes the
     resolved values into the containers as plain env vars.
   - `SUBTITLES_REPO_HOST_PATH`/`VAULT_REPO_HOST_PATH` (must exactly
     match `config.yaml`'s `github.subtitles_repo_path`/`vault_repo_path`
     — these get bind-mounted into the containers at the same absolute
     path so `config.yaml` doesn't need container-specific overrides).

5. Build and start everything. Everything in `.env` above is an `op://`
   reference, not a raw secret, so this needs to go through `op run`
   (which resolves them into the environment) rather than plain
   `docker compose --env-file` (which would pass the literal strings
   `"op://..."` through unresolved):
   ```bash
   cd docker
   op run --env-file ../.env -- docker compose up -d --build
   ```

6. Test the public URL:
   ```bash
   curl -X POST https://your-tunnel-hostname.example.com/process \
     -H "Authorization: Bearer <the webhook token from step 1>" \
     -d '{"input": "https://www.youtube.com/watch?v=..."}'
   ```

`server.py`'s request-handling behavior (queued, `202 Accepted`
immediately, actual work happens async, `notify()` alerts you when a job
finishes or fails, local file paths never accepted over the network)
works the same whether it's running natively or in a container — see its
docstring for details.

### Security notes

- Both auth tokens (webhook, bridge) are the only things standing between
  those services and whatever can reach them. Treat them like any other
  credential — 1Password-only, never committed, rotate if you suspect
  exposure. They're deliberately separate: the webhook token gates a
  public-internet-facing endpoint, the bridge token only gates traffic
  from your own Docker network, but there's no reason to let a leak of
  one compromise the other.
- Consider also putting the tunnel hostname behind
  [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
  for a second layer of auth (e.g. your own email/Google login) — the
  built-in token check is deliberately minimal.
- `server.py`'s endpoint fetches whatever URL you give it (subject to the
  same yt-dlp/direct-download/Overcast-RSS logic as the CLI). Only send
  it links you trust; there's no allowlist of sites, and URL fetches
  aren't restricted to public hosts (no SSRF protection).
- `.env` holds `op://` references, not raw secret values, but it's still
  worth keeping out of anywhere those references could be resolved by
  someone else — it's git-ignored for that reason, same as `config.yaml`.

---

## Failure notifications

Configure under `notifications:` in `config.yaml`:

- **Webhook** — set `webhook_url` to any endpoint that accepts a JSON POST
  of `{"text": "..."}`. A Slack "Incoming Webhook" URL works directly.
- **Email** — set `email.enabled: true` and fill in your SMTP details;
  the password is read from the `SMTP_PASSWORD` env var (put it in `.env`
  or export it in cron), never stored in `config.yaml`.

You'll get notified when (playlist mode only — one-off `pipeline.py --input`
runs just exit non-zero and log the error, since you're watching the
terminal):
- A run crashes entirely (bad config, auth failure, etc.).
- A specific video fails processing — retried on the next run, up to
  `max_retries` (default 3) times, then a final "giving up" alert and the
  video is marked processed so it stops retrying.
- A video has no subtitles/captions AND local transcription couldn't
  produce anything either (e.g. the video is private/deleted/geo-blocked)
  — one alert, marked processed immediately since retrying won't help.

---

## How it decides what's "new"

`state.json` tracks every video ID already processed. Each run fetches the
full current playlist and diffs it against that list — so you can add
multiple videos between runs and all of them get picked up, in the order
they were added to the playlist.

## Notes / limitations

- If a YouTube video has no subtitles/captions at all (manual or
  auto-generated), or a link doesn't have them either, the pipeline falls
  back to downloading audio and transcribing it locally with Parakeet
  (via MLX). Only if that also fails (source can't be downloaded at all)
  does it give up on that item.
- Local transcription runs on the Mac's GPU/Neural Engine via MLX — fast
  on Apple Silicon, but still real processing time on longer videos.
- For local files, only a same-basename `.srt` sidecar (e.g.
  `myvideo.mp4` + `myvideo.srt`) is picked up automatically; otherwise the
  file is transcribed from scratch.
- `overcast.fm` episode links (e.g. `https://overcast.fm/+AA2-B9jIzPM`) are
  special-cased: since Overcast doesn't host the audio itself, the pipeline
  reads the podcast's RSS feed link off the page and grabs the real mp3
  enclosure URL for that episode, then transcribes it directly (no video
  ever downloaded). If that lookup fails for any reason, it falls back to
  the normal generic-link handling.
- Very long transcripts are truncated to ~150k characters before
  summarization to stay within a safe prompt size; this covers several
  hours of typical speech.
- The script force-syncs both repos to `origin/<branch>` on every run
  (`git reset --hard`) before writing, so don't make manual edits directly
  in `subtitles_repo_path` / `vault_repo_path` on the server — those are
  working copies, not where you should edit by hand.
