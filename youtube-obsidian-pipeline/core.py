"""
Shared helpers used by both pipeline.py (single-input processor) and
fetch_playlist.py (YouTube playlist polling + retry loop): config/state
I/O, notifications, git helpers, summarization, and Obsidian note building.
"""
import contextlib
import fcntl
import ipaddress
import json
import logging
import os
import re
import smtplib
import socket
import subprocess
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import yaml

log = logging.getLogger("pipeline")

# Shared timeout for yt-dlp operations (subtitle download, audio download, etc.)
YTDLP_TIMEOUT_SECONDS = 1800


def op_read(ref: str) -> str:
    result = subprocess.run(["op", "read", ref], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def resolve_secret(env_var: str, op_ref: str) -> str:
    """Prefers an already-resolved environment variable over calling
    op_read() directly. Lets the same call sites work in two deployment
    modes without branching: natively (env_var unset, falls through to
    op_read() - the host's 1Password CLI is already authenticated via the
    desktop app's CLI integration) and in a container (env_var set by
    docker-compose from a value resolved on the host via `op run
    --env-file` before the container ever started - no 1Password Service
    Account or `op` binary needed inside the container at all)."""
    value = os.environ.get(env_var)
    if value:
        return value
    return op_read(op_ref)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify(cfg: dict, subject: str, message: str) -> None:
    """Best-effort alert via webhook and/or email. Never raises."""
    notif_cfg = cfg.get("notifications", {}) or {}

    webhook_url = notif_cfg.get("webhook_url")
    if webhook_url:
        try:
            body = json.dumps({"text": f"*{subject}*\n{message}"}).encode()
            req = urllib.request.Request(
                webhook_url, data=body,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            log.error("Webhook notification failed: %s", e)

    email_cfg = notif_cfg.get("email") or {}
    if email_cfg.get("enabled"):
        try:
            password = os.environ.get(email_cfg["smtp_password_env_var"], "")
            msg = MIMEText(message)
            msg["Subject"] = subject
            msg["From"] = email_cfg["from_addr"]
            msg["To"] = email_cfg["to_addr"]
            with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=20) as server:
                server.starttls()
                if email_cfg.get("smtp_user"):
                    server.login(email_cfg["smtp_user"], password)
                server.sendmail(email_cfg["from_addr"], [email_cfg["to_addr"]], msg.as_string())
        except Exception as e:
            log.error("Email notification failed: %s", e)


# --------------------------------------------------------------------------
# Config / state
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        state = json.loads(p.read_text())
        state.setdefault("failed_attempts", {})
        return state
    return {"processed_video_ids": [], "failed_attempts": {}}


def save_state(path: str, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2))


@contextlib.contextmanager
def pipeline_lock(lock_path: str):
    """File-based inter-process lock for serializing process_input() calls
    across containers. Use around any code that modifies state.json or the
    git repos to prevent races between pipeline-server and pipeline-fetch."""
    lock_file = Path(lock_path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# SSRF protection
# --------------------------------------------------------------------------

def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Returns True if the IP is private, loopback, link-local, multicast,
    reserved, or a cloud metadata address."""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return True
    # Cloud metadata endpoints
    if isinstance(ip, ipaddress.IPv4Address):
        # AWS, Azure, Google Cloud metadata
        if ip in ipaddress.IPv4Network("169.254.169.254/32"):
            return True
    elif isinstance(ip, ipaddress.IPv6Address):
        # IPv6 metadata (fd00:ec2::254 for AWS)
        if ip in ipaddress.IPv6Network("fd00:ec2::/32"):
            return True
    return False


def validate_public_url(url: str) -> None:
    """Validates that a URL is safe to fetch: http(s) only, hostname resolves
    to public IPs only (not loopback, private, link-local, multicast,
    reserved, or cloud metadata addresses). Raises ValueError if invalid.
    Call this before fetching any webhook-provided or user-supplied URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    # Resolve hostname to IPs and check each one
    try:
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve hostname {hostname!r}: {e}")

    resolved_ips = {info[4][0] for info in addr_info}
    for ip_str in resolved_ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            # Skip if not a valid IP (shouldn't happen, but be defensive)
            continue
        if _is_private_ip(ip):
            raise ValueError(
                f"URL {url!r} resolves to private/reserved IP {ip_str} "
                "(loopback, private, link-local, multicast, cloud metadata, etc.) - rejected for SSRF protection"
            )


# --------------------------------------------------------------------------
# Transcript text helpers
# --------------------------------------------------------------------------

def srt_to_plain_text(srt_path: Path) -> str:
    """Strips index numbers/timestamps, collapses to plain paragraph text."""
    lines = srt_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    text_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        text_lines.append(line)
    # de-duplicate consecutive repeated lines (common in auto-captions)
    deduped = []
    for line in text_lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return " ".join(deduped)


# --------------------------------------------------------------------------
# Summarization via Claude Code CLI (uses your subscription, not API billing)
# --------------------------------------------------------------------------

SUMMARY_PROMPT_TEMPLATE = """You are an expert executive assistant and strategic analyst.
Your task is to transform a raw meeting transcription into a structured, decision-focused meeting summary suitable for leadership review and archival.

Carefully analyze the transcription for:

- Objectives and meeting context
- Key decisions and their rationale
- Tradeoffs discussed
- Risks and constraints
- Metrics, targets, or KPIs mentioned
- Stakeholder positions or disagreements
- Dependencies and blockers
- Explicit and implicit action items

Generate the report using the following Markdown format structure:
## SUMMARY
- 4 to 8 concise but information-dense sentences.
- Clearly state: purpose of the meeting, major outcomes, critical decisions, unresolved issues, and overall direction.
- Focus on impact and implications, not just restating content.
- Avoid filler language.

## KEY DISCUSSION POINTS
- 1 to 10 structured bullet points.
- Group related points logically.
- Each bullet must capture:
- What was discussed
- Why it matters
- Any decision made
- Tradeoffs, risks, or constraints (if mentioned)
- Quantitative data when available

- Prioritize signal over noise.
- Do NOT repeat trivial conversational content.
- If NO meaningful key discussion points, write: No key discussion points.

## DECISIONS MADE
- List only explicit decisions.
- If no decisions were made, write: No formal decisions were made.

## OPEN QUESTIONS / RISKS
- List unresolved issues, concerns, or risks.
- If none exist, write: No major open risks identified.

## ACTION ITEMS
- 3 to 15 bullet points if present.
- Format: Action – Owner (if mentioned) – Deadline (if mentioned)
- Only include owner/date when explicitly stated.
- If none action items, write: No action items.
- Do NOT invent assignments.

IMPORTANT RULES:
- Use the same language as the transcription.
- Return ONLY markdown text.
- Maintain strict transcript accuracy. ***NO EXTERNAL ADDITIONS***.
- Eliminate filler, repetition, and small talk.

Transcription:
{transcription}"""


_SUMMARY_MARKER = "## SUMMARY"
_SUMMARIZE_ATTEMPTS = 2


def summarize_with_claude(claude_cmd: str, transcript_text: str) -> str:
    prompt = SUMMARY_PROMPT_TEMPLATE.format(transcription=transcript_text[:150000])

    # Harden the environment: clear inherited env vars to prevent leaking
    # sensitive data via tools/MCP/plugins, and minimize filesystem access.
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
    }

    for attempt in range(1, _SUMMARIZE_ATTEMPTS + 1):
        result = subprocess.run(
            [claude_cmd, "-p", prompt],
            capture_output=True, text=True, timeout=600, env=clean_env,
        )
        if result.returncode != 0:
            log.error("Claude CLI failed (attempt %d/%d): %s", attempt, _SUMMARIZE_ATTEMPTS, result.stderr[-1000:])
            continue

        output = result.stdout.strip()
        # Some claude CLI setups (e.g. with certain plugins/hooks enabled)
        # prepend stray meta-commentary - or replace the response entirely
        # with meta-commentary and no real summary at all (e.g. "I'm in
        # plan mode..."). Our template always asks for "## SUMMARY" as the
        # first heading, so require it: strip anything before it if
        # present, or retry/fail if it's missing altogether rather than
        # committing the meta-commentary as if it were the summary.
        idx = output.find(_SUMMARY_MARKER)
        if idx == -1:
            log.warning(
                "Claude CLI response missing the expected '%s' heading (attempt %d/%d) - "
                "likely polluted by an ambient hook/plugin. Response started with: %r",
                _SUMMARY_MARKER, attempt, _SUMMARIZE_ATTEMPTS, output[:200],
            )
            continue
        return output[idx:]

    return "_Summary generation failed._"


TAGS_PROMPT_TEMPLATE = """Analyze the following transcription and generate 3 to 8 relevant tags describing its content.

Rules:
- Tags must reflect specific topics, themes, people, organizations, or domains actually discussed - not generic words like "video", "podcast", "transcript", or "summary".
- Each tag must be lowercase, using hyphens instead of spaces (e.g. "machine-learning", not "Machine Learning").
- Return ONLY the tags, one per line, no numbering, no bullets, no other text.

Transcription:
{transcription}"""


_MAX_TAG_LENGTH = 40


def _sanitize_tag(raw: str) -> str:
    tag = re.sub(r"\s+", "-", raw.strip().lower())
    tag = re.sub(r"[^a-z0-9\-_/]", "", tag)
    return tag.strip("-")


def generate_tags_with_claude(claude_cmd: str, transcript_text: str) -> list[str]:
    prompt = TAGS_PROMPT_TEMPLATE.format(transcription=transcript_text[:150000])

    # Harden the environment: same pattern as summarize_with_claude.
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
    }

    result = subprocess.run(
        [claude_cmd, "-p", prompt],
        capture_output=True, text=True, timeout=600, env=clean_env,
    )
    if result.returncode != 0:
        log.error("Claude CLI failed to generate tags: %s", result.stderr[-1000:])
        return []

    tags = []
    for line in result.stdout.splitlines():
        raw_line = line.lstrip("-*• ").strip()
        # Some claude CLI setups (e.g. with certain plugins/hooks enabled)
        # prepend a stray meta-commentary sentence before the actual tags.
        # Real tags are short and punctuation-free - reject anything that
        # reads like a sentence rather than a tag.
        if not raw_line or any(c in raw_line for c in ".!?:;"):
            continue
        tag = _sanitize_tag(raw_line)
        if tag and len(tag) <= _MAX_TAG_LENGTH and tag not in tags:
            tags.append(tag)
    return tags


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------

def _sanitize_git_output(text: str) -> str:
    """Redacts x-access-token:<token>@ patterns from git command output."""
    return re.sub(r"x-access-token:[^@]+@", "x-access-token:REDACTED@", text)


def run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "(no output)").strip()
        sanitized_detail = _sanitize_git_output(detail)
        sanitized_args = [_sanitize_git_output(arg) for arg in args]
        raise RuntimeError(f"git {' '.join(sanitized_args)} failed: {sanitized_detail}")


def ensure_repo(repo_url: str, local_path: str, branch: str, token: str) -> Path:
    path = Path(local_path)
    authed_url = repo_url.replace("https://", f"https://x-access-token:{token}@")
    if not (path / ".git").exists():
        # Not yet a real clone. Deliberately checking for .git rather than
        # path.exists(): a Docker bind mount auto-creates the host
        # directory (empty) the moment the container starts, before this
        # function ever runs - path.exists() would be true on a
        # container's very first run and wrongly take the "update
        # existing clone" branch below, which fails since there's no
        # git repo there yet. `git clone` accepts an existing empty
        # directory as its target, so this doesn't need special-casing
        # beyond picking the right branch here.
        path.mkdir(parents=True, exist_ok=True)
        run_git(["clone", "--branch", branch, authed_url, str(path)], cwd=path.parent)
    else:
        run_git(["remote", "set-url", "origin", authed_url], cwd=path)
        run_git(["fetch", "origin", branch], cwd=path)
        run_git(["checkout", branch], cwd=path)
        run_git(["reset", "--hard", f"origin/{branch}"], cwd=path)
    run_git(["config", "core.ignorecase", "false"], cwd=path)
    return path


def commit_and_push(repo_path: Path, files: list[Path], message: str, name: str, email: str) -> None:
    run_git(["config", "user.name", name], cwd=repo_path)
    run_git(["config", "user.email", email], cwd=repo_path)
    rel_files = [str(f.relative_to(repo_path)) for f in files]
    run_git(["add", "-f"] + rel_files, cwd=repo_path)
    run_git(["add", "-u"], cwd=repo_path)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_path, capture_output=True, text=True)
    if not status.stdout.strip():
        log.info("Nothing to commit in %s", repo_path)
        return
    run_git(["commit", "-m", message], cwd=repo_path)
    run_git(["push", "origin", "HEAD"], cwd=repo_path)


# --------------------------------------------------------------------------
# Obsidian note
# --------------------------------------------------------------------------

def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\- ]", "", title).strip().lower()
    slug = re.sub(r"\s+", " ", slug).replace(" ", "-")
    return slug[:80] or "untitled"


def build_note(title: str, source_type: str, source_url: str | None, video_id: str | None,
                published_at: str | None, subtitle_github_url: str, summary: str,
                content_tags: list[str] | None = None) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tags = ["video-summary", source_type]
    for tag in content_tags or []:
        if tag not in tags:
            tags.append(tag)

    lines = ["---", f'title: "{title}"', f"source_type: {source_type}"]
    if source_url:
        lines.append(f"source_url: {source_url}")
    if video_id:
        lines.append(f"video_id: {video_id}")
    if published_at:
        lines.append(f"published_at: {published_at}")
    lines += [
        f"processed_at: {today}",
        f"subtitles: {subtitle_github_url}",
        f"tags: [{', '.join(tags)}]",
        "---",
        "",
        f"# {title}",
        "",
    ]
    if source_url:
        lines.append(f"- **Source:** [{source_url}]({source_url})")
    lines.append(f"- **Transcript:** [View on GitHub]({subtitle_github_url})")
    lines += ["", "## Summary", "", summary, ""]
    return "\n".join(lines)
