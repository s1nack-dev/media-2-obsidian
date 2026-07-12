"""
Shared helpers used by both pipeline.py (single-input processor) and
fetch_playlist.py (YouTube playlist polling + retry loop): config/state
I/O, notifications, git helpers, and Obsidian note building. Claude
summarization/tagging lives in claude_client.py.
"""

import contextlib
import fcntl
import http.client
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
from urllib.parse import urljoin, urlparse

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


def resolve_state_path(cfg: dict) -> str:
    """Return the configured state path, honoring a container-specific override."""
    return os.environ.get("PIPELINE_STATE_FILE", cfg["state_file"])


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


def resolve_lock_path(cfg: dict) -> str:
    """Return the configured lock path, honoring a container-specific override.

    ``PIPELINE_LOCK_FILE`` lets the compose services use one known shared bind
    mount without changing the user's native ``config.yaml``.
    """
    return os.environ.get("PIPELINE_LOCK_FILE", cfg.get("lock_file", "pipeline.lock"))


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


_DEFAULT_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0"}


def open_pinned(
    url: str,
    timeout: int,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
) -> tuple:
    """
    Validate url, resolve it to an approved public IP, and open a connection
    pinned to that IP (correct Host header preserved) without following
    redirects.

    Parameters:
        url (str): URL to request.
        timeout (int): Connection/read timeout in seconds.
        method (str): HTTP method.
        headers (dict | None): Extra headers, merged over the default
            User-Agent (never overrides the pinned Host header).
        body (bytes | None): Optional request body (e.g. for POST).

    Returns:
        tuple: (response, hostname) - response is the open HTTPResponse.
    """
    validated_ip, hostname = resolve_and_validate_url(url)

    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host_header = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"

    if parsed.scheme == "https":

        class PinnedHTTPSConnection(http.client.HTTPSConnection):
            def connect(self):
                sock = socket.create_connection((validated_ip, port), self.timeout)
                self.sock = self._context.wrap_socket(sock, server_hostname=hostname)

        conn = PinnedHTTPSConnection(
            hostname, port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(validated_ip, port, timeout=timeout)

    req_headers = {**_DEFAULT_FETCH_HEADERS, **(headers or {}), "Host": host_header}
    conn.request(method, target, body=body, headers=req_headers)
    return conn.getresponse(), hostname


def safe_fetch(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: int = 30,
    max_redirects: int = 5,
) -> tuple[int, dict[str, str], bytes]:
    """
    SSRF-safe HTTP request for small text/JSON payloads (RSS feeds, iTunes
    Search API, Spotify Web API): pins the resolved IP via open_pinned() and
    revalidates every redirect hop before following it, closing the
    DNS-rebinding TOCTOU gap. Response body is capped at MAX_MEDIA_BYTES.

    Unlike the large-media-file download path (see pipeline.py's
    download_direct_file/_download_with_redirect_validation, which streams
    straight to disk), this reads the full body into memory and returns the
    status code so callers can branch on 401/403/404 themselves instead of
    treating every non-2xx response as fatal.

    Returns:
        tuple[int, dict[str, str], bytes]: (status, response headers, body).

    Raises:
        ValueError: If a redirect is malformed, the redirect chain exceeds
            max_redirects, or the body exceeds MAX_MEDIA_BYTES.
    """
    current_url = url
    current_method = method
    current_body = body

    for _ in range(max_redirects + 1):
        resp, _hostname = open_pinned(
            current_url,
            timeout,
            method=current_method,
            headers=headers,
            body=current_body,
        )
        if resp.status in (301, 302, 303, 307, 308):
            with resp:
                location = resp.getheader("Location")
            if not location:
                raise ValueError(
                    f"Redirect response {resp.status} without Location header"
                )
            current_url = urljoin(current_url, location)
            if resp.status == 303:
                current_method, current_body = "GET", None
            continue

        resp_headers = dict(resp.getheaders())
        downloaded = bytearray()
        with resp:
            while chunk := resp.read(64 * 1024):
                downloaded.extend(chunk)
                if len(downloaded) > MAX_MEDIA_BYTES:
                    raise ValueError(f"Response exceeds {MAX_MEDIA_BYTES} byte limit")
        return resp.status, resp_headers, bytes(downloaded)

    raise ValueError(f"Too many redirects (>{max_redirects}) when fetching {url}")


# --------------------------------------------------------------------------
# Transcript text helpers
# --------------------------------------------------------------------------


def srt_text_to_plain_text(srt_text: str) -> str:
    """Convert in-memory SRT subtitle text into deduplicated plain text.

    Parameters:
        srt_text (str): SRT-formatted subtitle content.

    Returns:
        str: Subtitle text with indices, timestamps, blank lines, and consecutive duplicate lines removed.
    """
    lines = srt_text.splitlines()
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


def srt_to_plain_text(srt_path: Path) -> str:
    """Convert an SRT subtitle file into deduplicated plain text.

    Parameters:
        srt_path (Path): Path to the SRT subtitle file.

    Returns:
        str: Subtitle text with indices, timestamps, blank lines, and consecutive duplicate lines removed.
    """
    return srt_text_to_plain_text(srt_path.read_text(encoding="utf-8", errors="ignore"))


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
    cred_helper = f'!f() {{ test "$1" = get && echo "username=x-access-token" && echo "password={token}"; }}; f'

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
            [
                "-c",
                f"credential.helper={cred_helper}",
                "clone",
                "--branch",
                branch,
                repo_url,
                str(path),
            ],
            cwd=path.parent,
        )
    else:
        # Ensure origin is set to the clean URL (no embedded token)
        run_git(["remote", "set-url", "origin", repo_url], cwd=path)
        run_git(
            ["-c", f"credential.helper={cred_helper}", "fetch", "origin", branch],
            cwd=path,
        )
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
    cred_helper = f'!f() {{ test "$1" = get && echo "username=x-access-token" && echo "password={token}"; }}; f'
    run_git(
        ["-c", f"credential.helper={cred_helper}", "push", "origin", "HEAD"],
        cwd=repo_path,
    )


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
    extra_frontmatter: dict[str, str] | None = None,
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
        extra_frontmatter (dict[str, str] | None): Optional additional
            provider-specific frontmatter fields (e.g. a podcast's show name
            or RSS feed URL) rendered as extra `key: value` lines. Kept
            generic rather than provider-specific so new sources don't
            require changes here.

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
    for key, value in (extra_frontmatter or {}).items():
        if value:
            lines.append(f"{key}: {value}")
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
