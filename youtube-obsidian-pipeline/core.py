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
import ssl
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import yaml

log = logging.getLogger("pipeline")

# Shared timeout for yt-dlp operations (subtitle download, audio download, etc.)
YTDLP_TIMEOUT_SECONDS = 1800
MAX_MEDIA_BYTES = 500 * 1024 * 1024


def op_read(ref: str) -> str:
    """Read a secret from 1Password using its reference.

    Parameters:
        ref (str): The 1Password secret reference.

    Returns:
        str: The secret value without surrounding whitespace.
    """
    try:
        result = subprocess.run(
            ["op", "read", ref], capture_output=True, text=True, check=True, timeout=30
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"1Password CLI command 'op read {ref}' timed out after 30 seconds"
        ) from e


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
    """
    Send an alert through the configured webhook and/or email notification channels.

    Parameters:
        cfg (dict): Configuration containing notification settings.
        subject (str): Notification subject.
        message (str): Notification body.
    """
    notif_cfg = cfg.get("notifications", {}) or {}

    webhook_url = notif_cfg.get("webhook_url")
    if webhook_url:
        try:
            if urlparse(webhook_url).scheme not in ("http", "https"):
                raise ValueError("Webhook URL scheme must be http or https")
            body = json.dumps({"text": f"*{subject}*\n{message}"}).encode()
            req = urllib.request.Request(
                webhook_url,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            # Disable automatic HTTP redirects to prevent redirect-based attacks
            class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
                def http_error_302(self, req, fp, code, msg, headers):
                    raise urllib.error.HTTPError(
                        req.full_url, code, "Redirects disabled", headers, fp
                    )

                http_error_301 = http_error_303 = http_error_307 = http_error_308 = (
                    http_error_302
                )

            opener = urllib.request.build_opener(NoRedirectHandler)
            opener.open(req, timeout=15)  # nosec B310 - scheme validated above
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
            context = ssl.create_default_context()
            with smtplib.SMTP(
                email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=20
            ) as server:
                server.starttls(context=context)
                if email_cfg.get("smtp_user"):
                    server.login(email_cfg["smtp_user"], password)
                server.sendmail(
                    email_cfg["from_addr"], [email_cfg["to_addr"]], msg.as_string()
                )
        except Exception as e:
            log.error("Email notification failed: %s", e)


# --------------------------------------------------------------------------
# Config / state
# --------------------------------------------------------------------------


def load_config(path: str) -> dict:
    """Load configuration data from a YAML file.

    Parameters:
        path (str): Path to the YAML configuration file.

    Returns:
        dict: Parsed configuration data.
    """
    with open(path) as f:
        return yaml.safe_load(f)


def load_state(path: str) -> dict:
    """
    Load persisted pipeline state from a JSON file.

    Parameters:
        path (str): Path to the state file.

    Returns:
        dict: The loaded state, including an empty `failed_attempts` mapping when absent, or default initial state when the file does not exist.
    """
    p = Path(path)
    if p.exists():
        state = json.loads(p.read_text())
        state.setdefault("failed_attempts", {})
        return state
    return {"processed_video_ids": [], "failed_attempts": {}}


def save_state(path: str, state: dict) -> None:
    """
    Save pipeline state as indented JSON at the specified path.

    Parameters:
        path (str): Destination file path.
        state (dict): State data to serialize.
    """
    Path(path).write_text(json.dumps(state, indent=2))


@contextlib.contextmanager
def pipeline_lock(lock_path: str):
    """
    Serialize access to shared state and repositories using a file-based inter-process lock.

    Parameters:
        lock_path (str): Path to the lock file.
    """
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
    """Determine whether an IP address belongs to a private or reserved address range.

    Parameters:
        ip (IPv4Address | IPv6Address): The IP address to classify.

    Returns:
        bool: `true` if the address is private, loopback, link-local, multicast, reserved, or a recognized cloud metadata address, `false` otherwise.
    """
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
    ):
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
    """
    Validate that a URL uses HTTP or HTTPS and resolves exclusively to public IP addresses.

    Raises:
        ValueError: If the URL has an unsupported scheme, lacks a hostname, cannot
            be resolved, or resolves to a private, reserved, or metadata IP address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    # Resolve hostname to IPs and check each one
    try:
        addr_info = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve hostname {hostname!r}: {e}") from e

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


def resolve_and_validate_url(url: str) -> tuple[str, str]:
    """
    Resolve a URL's hostname to a validated public IP and return both the IP and the hostname.

    This enables IP pinning to prevent DNS rebinding attacks (TOCTOU gap between
    validation and actual connection).

    Parameters:
        url (str): The URL to resolve and validate.

    Returns:
        tuple[str, str]: A tuple of (validated_ip, hostname) for use in IP-pinned connections.

    Raises:
        ValueError: If the URL is invalid or resolves to a private/reserved IP.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https, got {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    # Resolve hostname to IPs immediately before connection
    try:
        addr_info = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve hostname {hostname!r}: {e}") from e

    # Pick the first resolved IP and validate it
    if not addr_info:
        raise ValueError(f"No addresses resolved for hostname {hostname!r}")

    ip_str = addr_info[0][4][0]
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as e:
        raise ValueError(
            f"Invalid IP address {ip_str!r} for hostname {hostname!r}"
        ) from e

    if _is_private_ip(ip):
        raise ValueError(
            f"URL {url!r} resolves to private/reserved IP {ip_str} "
            "(loopback, private, link-local, multicast, cloud metadata, etc.) - rejected for SSRF protection"
        )

    return ip_str, hostname


# --------------------------------------------------------------------------
# Transcript text helpers
# --------------------------------------------------------------------------


def srt_to_plain_text(srt_path: Path) -> str:
    """Convert an SRT subtitle file into deduplicated plain text.

    Parameters:
        srt_path (Path): Path to the SRT subtitle file.

    Returns:
        str: Subtitle text with indices, timestamps, blank lines, and consecutive duplicate lines removed.
    """
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
_CLAUDE_UNTRUSTED_INPUT_ARGS = [
    "--safe-mode",
    "--strict-mcp-config",
    "--mcp-config",
    "{}",
    "--tools",
    "",
    "--disable-slash-commands",
    "--no-session-persistence",
]


def summarize_with_claude(claude_cmd: str, transcript_text: str) -> str:
    """
    Generate a transcript summary using the Claude CLI.

    Parameters:
        claude_cmd (str): Command used to invoke the Claude CLI.
        transcript_text (str): Transcript text to summarize.

    Returns:
        str: The generated summary beginning with the expected summary heading, or a failure message if generation fails.
    """
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
            [claude_cmd, *_CLAUDE_UNTRUSTED_INPUT_ARGS, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=600,
            env=clean_env,
        )
        if result.returncode != 0:
            log.error(
                "Claude CLI failed (attempt %d/%d): %s",
                attempt,
                _SUMMARIZE_ATTEMPTS,
                result.stderr[-1000:],
            )
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
                _SUMMARY_MARKER,
                attempt,
                _SUMMARIZE_ATTEMPTS,
                output[:200],
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
    """
    Convert raw text into a normalized tag.

    Parameters:
        raw (str): The text to normalize.

    Returns:
        str: A lowercase tag with whitespace replaced by hyphens and unsupported characters removed.
    """
    tag = re.sub(r"\s+", "-", raw.strip().lower())
    tag = re.sub(r"[^a-z0-9\-_/]", "", tag)
    return tag.strip("-")


def generate_tags_with_claude(claude_cmd: str, transcript_text: str) -> list[str]:
    """
    Generate content tags from a transcript using Claude.

    Parameters:
        claude_cmd (str): Command used to invoke the Claude CLI.
        transcript_text (str): Transcript from which to extract tags.

    Returns:
        list[str]: Ordered, unique, sanitized tags generated from the transcript, or an empty list if generation fails.
    """
    prompt = TAGS_PROMPT_TEMPLATE.format(transcription=transcript_text[:150000])

    # Harden the environment: same pattern as summarize_with_claude.
    clean_env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
    }

    result = subprocess.run(
        [claude_cmd, *_CLAUDE_UNTRUSTED_INPUT_ARGS, "-p", prompt],
        capture_output=True,
        text=True,
        timeout=600,
        env=clean_env,
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
    """Redact tokens embedded in Git URLs and credential-helper values."""
    text = re.sub(r"x-access-token:[^@]+@", "x-access-token:REDACTED@", text)
    text = re.sub(r"password=[^\s;'\"]+", "password=REDACTED", text)
    return re.sub(
        r"credential\.helper(?:=|\s+)[^\r\n]*", "credential.helper=REDACTED", text
    )


GIT_TIMEOUT_SECONDS = 300


def run_git(args: list[str], cwd: Path) -> None:
    """
    Run a Git command in the specified directory.

    Parameters:
        args (list[str]): Arguments to pass to Git.
        cwd (Path): Directory in which to run the command.

    Raises:
        RuntimeError: If Git exits with a nonzero status or times out.
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        # Sanitize command and any available output before raising
        sanitized_args = [_sanitize_git_output(arg) for arg in args]
        sanitized_stdout = _sanitize_git_output(
            e.stdout.decode("utf-8", errors="replace") if e.stdout else ""
        )
        sanitized_stderr = _sanitize_git_output(
            e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
        )
        raise RuntimeError(
            f"git {' '.join(sanitized_args)} timed out after {GIT_TIMEOUT_SECONDS} seconds. "
            f"stdout: {sanitized_stdout[:500]}, stderr: {sanitized_stderr[:500]}"
        )

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "(no output)").strip()
        sanitized_detail = _sanitize_git_output(detail)
        sanitized_args = [_sanitize_git_output(arg) for arg in args]
        raise RuntimeError(f"git {' '.join(sanitized_args)} failed: {sanitized_detail}")


def ensure_repo(repo_url: str, local_path: str, branch: str, token: str) -> Path:
    """
    Ensure that a local repository is cloned or synchronized with the specified branch.

    Parameters:
        repo_url (str): Repository URL (without embedded credentials).
        local_path (str): Local directory for the repository.
        branch (str): Branch to clone or synchronize.
        token (str): Authentication token, supplied ephemerally per operation.

    Returns:
        Path: Path to the local repository.
    """
    path = Path(local_path)

    # Build a credential helper that echoes the token for this one operation.
    # Git will invoke this helper when it needs authentication, and the token
    # never gets written to .git/config.
    cred_helper = (
        f'!f() {{ test "$1" = get && echo "username=x-access-token" && echo "password={token}"; }}; f'
    )

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
        run_git(
            ["-c", f"credential.helper={cred_helper}", "clone", "--branch", branch, repo_url, str(path)],
            cwd=path.parent,
        )
    else:
        # Ensure origin is set to the clean URL (no embedded token)
        run_git(["remote", "set-url", "origin", repo_url], cwd=path)
        run_git(["-c", f"credential.helper={cred_helper}", "fetch", "origin", branch], cwd=path)
        run_git(["checkout", branch], cwd=path)
        run_git(["reset", "--hard", f"origin/{branch}"], cwd=path)
    run_git(["config", "core.ignorecase", "false"], cwd=path)
    return path


def commit_and_push(
    repo_path: Path, files: list[Path], message: str, name: str, email: str, token: str
) -> None:
    """
    Commit staged changes in a repository and push them to its origin.

    Parameters:
        repo_path (Path): Local Git repository path.
        files (list[Path]): Files to stage for the commit.
        message (str): Commit message.
        name (str): Git author name.
        email (str): Git author email.
        token (str): Authentication token for push operation.
    """
    run_git(["config", "user.name", name], cwd=repo_path)
    run_git(["config", "user.email", email], cwd=repo_path)
    rel_files = [str(f.relative_to(repo_path)) for f in files]
    run_git(["add", "-f"] + rel_files, cwd=repo_path)
    run_git(["add", "-u"], cwd=repo_path)
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"git status --porcelain timed out after {GIT_TIMEOUT_SECONDS} seconds"
        ) from e
    if not status.stdout.strip():
        log.info("Nothing to commit in %s", repo_path)
        return
    run_git(["commit", "-m", message], cwd=repo_path)
    # Use ephemeral credential helper for authenticated push
    cred_helper = (
        f'!f() {{ test "$1" = get && echo "username=x-access-token" && echo "password={token}"; }}; f'
    )
    run_git(["-c", f"credential.helper={cred_helper}", "push", "origin", "HEAD"], cwd=repo_path)


# --------------------------------------------------------------------------
# Obsidian note
# --------------------------------------------------------------------------


def slugify(title: str) -> str:
    """
    Create a lowercase URL-friendly slug from a title.

    Parameters:
        title (str): Title to normalize.

    Returns:
        str: A slug limited to 80 characters, or "untitled" when the title contains no usable characters.
    """
    slug = re.sub(r"[^a-zA-Z0-9\- ]", "", title).strip().lower()
    slug = re.sub(r"\s+", " ", slug).replace(" ", "-")
    return slug[:80] or "untitled"


def build_note(
    title: str,
    source_type: str,
    source_url: str | None,
    video_id: str | None,
    published_at: str | None,
    subtitle_github_url: str,
    summary: str,
    content_tags: list[str] | None = None,
) -> str:
    """
    Build an Obsidian note with YAML frontmatter, source links, transcript metadata, and a summary.

    Parameters:
        title (str): Note title.
        source_type (str): Type of the source.
        source_url (str | None): Optional source URL.
        video_id (str | None): Optional video identifier.
        published_at (str | None): Optional publication timestamp.
        subtitle_github_url (str): URL to the subtitles on GitHub.
        summary (str): Summary content for the note.
        content_tags (list[str] | None): Optional additional tags.

    Returns:
        str: Complete Obsidian note in Markdown format.
    """
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
