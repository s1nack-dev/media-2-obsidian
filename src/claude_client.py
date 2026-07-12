"""
Summarization and tagging via the Claude Code CLI (uses your subscription,
not API billing). Host-only: needs the Claude CLI's subscription OAuth
session, tied to this Mac's keychain/session, so this module is only ever
imported by host_bridge.py - never bundled into the container image.
"""

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("pipeline")

_ASSETS_DIR = Path(__file__).resolve().parent.parent
SUMMARY_PROMPT_TEMPLATE = (_ASSETS_DIR / "prompts" / "summary_prompt.txt").read_text()
TAGS_PROMPT_TEMPLATE = (_ASSETS_DIR / "prompts" / "tags_prompt.txt").read_text()
_SANDBOX_PROFILE_TEMPLATE = (_ASSETS_DIR / "sandbox" / "claude.sb.tmpl").read_text()

_SUMMARY_MARKER = "## SUMMARY"
_SUMMARIZE_ATTEMPTS = 2
_CLAUDE_UNTRUSTED_INPUT_ARGS = [
    "--safe-mode",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--tools",
    "",
    "--disable-slash-commands",
    "--no-session-persistence",
]
_OS_SANDBOX_REJECTED = False


def _sbpl_quote(value: str | Path) -> str:
    """Quote a filesystem path for use in a Seatbelt profile."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _claude_sandbox_profile(home: Path, executable: str, temp_dir: str | Path) -> str:
    """Build the restrictive macOS Seatbelt profile used for Claude calls.

    The CLI needs its own installation, its OAuth session state, and
    temporary storage. Everything else in the user's home directory and the
    pipeline checkout remains inaccessible. Inbound and non-TLS network access
    are denied; the sole outbound exception is HTTPS for the Claude API. Note
    this does not include macOS Keychain access - see the profile template
    for why. Set `claude.oauth_token_op_ref` to authenticate via
    `CLAUDE_CODE_OAUTH_TOKEN` instead of ambient Keychain login.
    """
    executable_dir = Path(executable).expanduser().resolve().parent
    temp_dir = Path(temp_dir).expanduser().resolve()
    home = home.expanduser().resolve()

    return _SANDBOX_PROFILE_TEMPLATE.format(
        executable_dir=_sbpl_quote(executable_dir),
        temp_dir=_sbpl_quote(temp_dir),
        home_local=_sbpl_quote(home / ".local"),
        home_claude=_sbpl_quote(home / ".claude"),
        home_claude_json=_sbpl_quote(home / ".claude.json"),
    )


def _sandbox_is_unavailable(result) -> bool:
    """Return whether macOS rejected the sandbox-exec wrapper itself."""
    return result.returncode == 71 and "sandbox_apply" in (result.stderr or "")


def _claude_failure_detail(result) -> str:
    """Build a useful failure message without logging model output."""
    stderr = (result.stderr or "").strip()
    if stderr:
        return f"exit code {result.returncode}: {stderr[-1000:]}"
    stdout = (result.stdout or "").strip().casefold()
    if "not logged in" in stdout or "please run /login" in stdout:
        return (
            f"exit code {result.returncode}: Claude Code is not logged in; "
            "run `claude login` on the host Mac."
        )
    return f"exit code {result.returncode} (no stderr)"


def _run_claude_process(
    command: list[str], env: dict[str, str], mode: str, operation: str
):
    """Run Claude and log privacy-safe launch and completion progress."""
    log.info("Claude %s launch started (%s).", operation, mode)
    started_at = time.monotonic()
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
        cwd="/",
    )
    log.info(
        "Claude %s launch finished (%s; exit=%d; elapsed=%.1fs; stdout_chars=%d; stderr_chars=%d).",
        operation,
        mode,
        result.returncode,
        time.monotonic() - started_at,
        len(getattr(result, "stdout", "") or ""),
        len(getattr(result, "stderr", "") or ""),
    )
    return result


def _run_claude(
    claude_cmd: str,
    prompt: str,
    oauth_token: str | None = None,
    operation: str = "request",
):
    """Run Claude with an OS sandbox when available, otherwise CLI safe mode.

    Some macOS environments cannot nest ``sandbox-exec`` (including app-
    sandboxed terminals). The Claude CLI flags below still disable tools, MCP,
    skills, hooks, and session persistence, so use that constrained fallback
    instead of failing every bridge request.
    """
    global _OS_SANDBOX_REJECTED

    command = shlex.split(claude_cmd)
    if not command:
        raise ValueError("claude.command must not be empty")

    executable = shutil.which(command[0]) or command[0]
    sandbox_exec = shutil.which("sandbox-exec")
    # Claude Code creates a per-UID directory beneath CLAUDE_CODE_TMPDIR with
    # a non-idempotent mkdir. Give every invocation its own parent directory
    # so retries and separate summary/tag calls cannot collide with stale
    # directories left by earlier Claude sessions.
    # nosec B108 - randomly-named, 0700-perm dir (not a predictable hardcoded
    # path); pinned to /tmp rather than the ambient TMPDIR so the Seatbelt
    # profile's file-write grant below is a fixed, known location.
    with tempfile.TemporaryDirectory(prefix="claude-bridge-", dir="/tmp") as temp_dir:  # nosec B108
        claude_tmpdir = str(Path(temp_dir) / "claude")
        # Claude Code can depend on session-related environment state in
        # addition to its Keychain credential. Keep the bridge process's
        # environment so subscription OAuth works the same way it does in the
        # native terminal that launched the bridge. The CLI flags below still
        # disable tools, MCP, skills, hooks, and session persistence, so the
        # untrusted transcript cannot access those ambient capabilities.
        claude_env = os.environ.copy()
        claude_env.update(
            {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", str(Path.home())),
                "USER": os.environ.get("USER", ""),
                "TMPDIR": temp_dir,
                "CLAUDE_CODE_TMPDIR": claude_tmpdir,
            }
        )
        if oauth_token:
            claude_env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
        claude_command = [
            executable,
            *command[1:],
            *_CLAUDE_UNTRUSTED_INPUT_ARGS,
            "-p",
            prompt,
        ]
        if sandbox_exec and not _OS_SANDBOX_REJECTED:
            sandboxed_command = [
                sandbox_exec,
                "-p",
                _claude_sandbox_profile(Path(claude_env["HOME"]), executable, temp_dir),
                *claude_command,
            ]
            result = _run_claude_process(
                sandboxed_command, claude_env, "macOS sandbox", operation
            )
            if not _sandbox_is_unavailable(result):
                return result
            _OS_SANDBOX_REJECTED = True
            log.warning(
                "macOS rejected sandbox-exec; falling back to Claude CLI safe mode: %s",
                _claude_failure_detail(result),
            )
        elif not sandbox_exec and not _OS_SANDBOX_REJECTED:
            _OS_SANDBOX_REJECTED = True
            log.warning("sandbox-exec is unavailable; using Claude CLI safe mode.")

        return _run_claude_process(claude_command, claude_env, "safe mode", operation)


def summarize_with_claude(
    claude_cmd: str, transcript_text: str, oauth_token: str | None = None
) -> str:
    """
    Generate a transcript summary using the Claude CLI.

    Parameters:
        claude_cmd (str): Command used to invoke the Claude CLI.
        transcript_text (str): Transcript text to summarize.

    Returns:
        str: The generated summary beginning with the expected summary heading, or a failure message if generation fails.
    """
    prompt = SUMMARY_PROMPT_TEMPLATE.format(transcription=transcript_text[:150000])

    for attempt in range(1, _SUMMARIZE_ATTEMPTS + 1):
        log.info(
            "Claude summary attempt %d/%d started (transcript_chars=%d).",
            attempt,
            _SUMMARIZE_ATTEMPTS,
            min(len(transcript_text), 150000),
        )
        try:
            result = _run_claude(claude_cmd, prompt, oauth_token, "summary")
        except (FileNotFoundError, OSError, ValueError, subprocess.TimeoutExpired) as e:
            log.error("Claude CLI could not start or complete: %s", e)
            return "_Summary generation failed._"
        if result.returncode != 0:
            log.error(
                "Claude CLI failed (attempt %d/%d): %s",
                attempt,
                _SUMMARIZE_ATTEMPTS,
                _claude_failure_detail(result),
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
        summary = output[idx:]
        log.info(
            "Claude summary attempt %d/%d succeeded (summary_chars=%d).",
            attempt,
            _SUMMARIZE_ATTEMPTS,
            len(summary),
        )
        return summary

    return "_Summary generation failed._"


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


def generate_tags_with_claude(
    claude_cmd: str, transcript_text: str, oauth_token: str | None = None
) -> list[str]:
    """
    Generate content tags from a transcript using Claude.

    Parameters:
        claude_cmd (str): Command used to invoke the Claude CLI.
        transcript_text (str): Transcript from which to extract tags.

    Returns:
        list[str]: Ordered, unique, sanitized tags generated from the transcript, or an empty list if generation fails.
    """
    prompt = TAGS_PROMPT_TEMPLATE.format(transcription=transcript_text[:150000])
    log.info(
        "Claude tag generation started (transcript_chars=%d).",
        min(len(transcript_text), 150000),
    )

    try:
        result = _run_claude(claude_cmd, prompt, oauth_token, "tag generation")
    except (FileNotFoundError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        log.error("Claude CLI could not start or complete: %s", e)
        return []
    if result.returncode != 0:
        log.error(
            "Claude CLI failed to generate tags: %s", _claude_failure_detail(result)
        )
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
    log.info("Claude tag generation succeeded (tag_count=%d).", len(tags))
    return tags
