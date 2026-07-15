# Coolify pipeline with a private Mac transcription bridge

This deployment has three trust zones: `cloudflared` exposes only the
authenticated webhook; Coolify runs playlist polling, commits, and Claude Code;
and a dedicated macOS account runs MLX transcription on loopback. Tailscale
Serve is the only remote path to the Mac bridge.

Do not place Tailscale in Compose. Install it as a system service on the
Coolify host so its Docker networks route through the host tailnet.

## Tailscale policy and host setup

Create the tags before joining machines:

```json
{
  "tagOwners": {
    "tag:coolify": ["autogroup:admin"],
    "tag:transcriber": ["autogroup:admin"]
  },
  "grants": [
    {"src": ["tag:coolify"], "dst": ["tag:transcriber"], "ip": ["tcp:443"]}
  ]
}
```

On the Linux Coolify host:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled
sudo tailscale up --advertise-tags=tag:coolify
tailscale status
```

For unattended enrollment, use a one-time, pre-tagged auth key instead of a
browser login. Never place that enrollment key in Coolify after the host joins.
After deployment, verify from a pipeline container:

```bash
python -c 'from urllib.request import urlopen; print(urlopen("https://transcriber.<tailnet>.ts.net/healthz").read())'
```

On the Mac, join Tailscale as `tag:transcriber`, then expose loopback only:

```bash
sudo tailscale serve --bg http://127.0.0.1:8081
tailscale serve status
```

Use `BRIDGE_URL=https://transcriber.<tailnet>.ts.net` in Coolify. Never use
Tailscale Funnel. The bridge bearer token remains required in addition to the
Tailnet grant.

## Mac transcription account and sandbox

Create a fresh non-admin account such as `media-transcriber`; do not use a
personal account and do not grant it personal files, Keychain, GitHub, Claude,
or webhook credentials. Check out this repository in a service-owned path,
such as `/Users/media-transcriber/media-2-obsidian`.

Create service-owned paths and the bridge-only environment file:

```bash
sudo -u media-transcriber mkdir -p /Users/media-transcriber/{tmp,huggingface,logs}
sudo -u media-transcriber chmod 700 /Users/media-transcriber/{tmp,huggingface,logs}
sudo -u media-transcriber sh -c 'umask 077; printf "%s\\n" "BRIDGE_AUTH_TOKEN=<random-token>" > /Users/media-transcriber/bridge.env'
```

Copy `macos/run-transcription-bridge.sh` and
`macos/transcription.sb.tmpl` into the service home. Replace the service-home
and repository-path placeholders after running `uv sync --extra mlx` in that
repository (the runtime wrapper executes its project virtualenv directly).
Make the files service-account owned, and render
`macos/com.media2obsidian.transcription-bridge.plist.tmpl` into
`/Library/LaunchDaemons/com.media2obsidian.transcription-bridge.plist`.
Load it with `launchctl bootstrap system ...`.

Preload the configured model **outside** Seatbelt during a controlled
maintenance window:

```bash
sudo -u media-transcriber /path/to/uv run python src/preload_transcription_model.py --config config.yaml
```

Production loading is offline-only. To update the model: stop launchd, update
the model config, preload it, validate real audio inside Seatbelt, then restart
the service. If the profile is invalid or MLX cannot use it, leave the bridge
down; do not add an unsandboxed fallback.

The Seatbelt template permits only service paths, Python/MLX runtime, the
model cache, temporary workspace, logs, and the loopback listener. It denies
outbound networking. Seatbelt is deprecated, so the dedicated non-admin account
and Tailnet policy remain the primary boundaries; revalidate after macOS/MLX
upgrades.

## Coolify deployment

Deploy `docker/docker-compose.coolify.yml`. It uses these platform-managed
volumes:

- `pipeline-runtime`: Google OAuth token, playlist state, process lock, monitor state.
- `pipeline-data`: Git clones.

Edit `config.coolify.yaml` with non-secret playlist and repository values. In
Coolify configure:

```text
TUNNEL_TOKEN
GITHUB_TOKEN
BRIDGE_AUTH_TOKEN
WEBHOOK_AUTH_TOKEN
CLAUDE_CODE_OAUTH_TOKEN
CLAUDE_CODE_VERSION=<validated exact npm version>
BRIDGE_URL=https://transcriber.<tailnet>.ts.net
```

Seed `/app/.pipeline-runtime/token.json` with the browser-created Google OAuth
token before starting playlist polling. Configure the Cloudflare Tunnel hostname
to target `http://pipeline-server:8080`. Protect it with Cloudflare Access using
a service token; callers must also send `Authorization: Bearer
<WEBHOOK_AUTH_TOKEN>`.

`bridge-monitor` probes private `/healthz` and emits existing notifications only
on a health-state transition. It sends neither bridge credentials nor media.

## Acceptance checks

1. Workers retain named-volume state after a restart.
2. A pipeline container reaches the Mac health endpoint; an untagged Tailnet
   node cannot reach it.
3. Subtitle-less audio transcribes on the Mac and summarizes/tags in Coolify.
4. Mac `/summarize` and `/tags` return `404`; wrong model `400`; bad bridge
   token `401`.
5. The public webhook requires both Cloudflare Access and the application token.
