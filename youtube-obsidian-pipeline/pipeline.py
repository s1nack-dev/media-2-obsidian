#!/usr/bin/env python3
"""
Media (local file / YouTube URL / generic link) -> transcript + AI summary
-> GitHub + Obsidian pipeline.

Processes ONE input at a time:
  1. Figures out whether the input is a local file, a YouTube URL, or some
     other link.
  2. Gets a transcript: existing subtitles/captions if available (YouTube,
     or any site yt-dlp can extract from), a sidecar .srt file (local
     files), otherwise sends audio to host_bridge.py for transcription
     (Parakeet/MLX - can't run in this container, see bridge_client.py).
  3. Summarizes the transcript, also via host_bridge.py (claude -p, host
     subscription auth - also can't run in this container).
  4. Commits the transcript file to your subtitles GitHub repo.
  5. Commits a new Obsidian markdown note to your vault GitHub repo.

This module itself has no MLX/parakeet or claude-CLI dependency - it's
meant to run in the pipeline container. Only host_bridge.py (run natively
on the Mac) needs those. See CLAUDE.md's "Webhook mode" / "Containerized
deployment" sections for the full architecture.

Usable two ways:
  - Standalone CLI: `python pipeline.py --input <path-or-url> --config config.yaml`
  - As a library: `fetch_playlist.py` imports `process_input()` and calls it
    once per new video found in a YouTube playlist.

See README.md for full setup instructions.
"""

import argparse
import html
import http.client
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import traceback
import urllib.request
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import defusedxml.ElementTree as ET

import bridge_client
from core import (
    MAX_MEDIA_BYTES,
    YTDLP_TIMEOUT_SECONDS,
    build_note,
    commit_and_push,
    ensure_repo,
    load_config,
    resolve_and_validate_url,
    resolve_secret,
    slugify,
    srt_to_plain_text,
    validate_public_url,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pipeline")

YOUTUBE_HOST_RE = re.compile(r"(^|\.)(youtube\.com|youtu\.be)$")
OVERCAST_HOST_RE = re.compile(r"(^|\.)overcast\.fm$")
DIRECT_MEDIA_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
}
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}


class PipelineError(Exception):
    """Base class for pipeline-specific errors."""


class NoTranscriptAvailableError(PipelineError):
    """Raised when no subtitles/captions exist and transcription could not
    produce a result either (e.g. the source can't be downloaded at all)."""


# --------------------------------------------------------------------------
# Input-type detection
# --------------------------------------------------------------------------


def detect_input_type(raw_input: str) -> str:
    """
    Classifies an input as a local file, YouTube URL, or generic HTTP(S) link.

    Parameters:
        raw_input (str): Local path or HTTP(S) URL to classify.

    Returns:
        str: One of `"local_file"`, `"youtube"`, or `"generic_link"`.

    Raises:
        ValueError: If the input is neither an existing local path nor an HTTP(S) URL.
    """
    if Path(raw_input).exists():
        return "local_file"
    parsed = urlparse(raw_input)
    if parsed.scheme in ("http", "https"):
        host = (parsed.hostname or "").lower()
        if YOUTUBE_HOST_RE.search(host):
            return "youtube"
        return "generic_link"
    raise ValueError(
        f"Input {raw_input!r} is not an existing local file and not a valid http(s) URL."
    )


def extract_youtube_video_id(url: str) -> str:
    """
    Extract the video identifier from a YouTube URL.

    Parameters:
        url (str): YouTube URL containing a video identifier.

    Returns:
        str: The extracted video identifier.

    Raises:
        ValueError: If the URL does not contain a video identifier.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        return parsed.path.lstrip("/").split("/")[0]
    if "/shorts/" in parsed.path or "/embed/" in parsed.path:
        return parsed.path.rstrip("/").split("/")[-1]
    qs = parse_qs(parsed.query)
    if "v" in qs:
        return qs["v"][0]
    raise ValueError(f"Could not extract a video ID from {url!r}")


# --------------------------------------------------------------------------
# yt-dlp helpers
# --------------------------------------------------------------------------


def _ytdlp_download_subs(
    url: str, out_basename: str, languages: list[str], workdir: Path
) -> tuple[Path, str] | None:
    """
    Download available subtitles for a URL as an SRT file.

    Parameters:
        url (str): URL to process with yt-dlp.
        out_basename (str): Base name for the generated subtitle file.
        languages (list[str]): Preferred subtitle language codes.
        workdir (Path): Directory for downloaded subtitle files.

    Returns:
        tuple[Path, str] | None: The selected SRT path and its language code, or None if no subtitles are available.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(workdir / f"{out_basename}.%(ext)s")
    lang_arg = ",".join(languages)
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-subs",
        "--write-auto-sub",
        "--sub-langs",
        lang_arg,
        "--convert-subs",
        "srt",
        "-o",
        out_tmpl,
        url,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "yt-dlp subtitle download timed out for %s after %d seconds",
            url,
            YTDLP_TIMEOUT_SECONDS,
        )
        return None

    if result.returncode != 0:
        log.warning("yt-dlp failed for %s: %s", url, result.stderr[-500:])

    for lang in languages:
        candidate = workdir / f"{out_basename}.{lang}.srt"
        if candidate.exists():
            return candidate, lang

    matches = sorted(workdir.glob(f"{out_basename}.*.srt"))
    if matches:
        m = re.match(rf"{re.escape(out_basename)}\.([^.]+)\.srt", matches[0].name)
        return matches[0], (m.group(1) if m else "unknown")

    return None


def download_subtitles(
    video_id: str, languages: list[str], workdir: Path
) -> tuple[Path, str] | None:
    """Download a YouTube video's subtitles in the preferred languages.

    Parameters:
        video_id (str): YouTube video identifier.
        languages (list[str]): Subtitle language codes to prioritize.
        workdir (Path): Directory for downloaded subtitle files.

    Returns:
        tuple[Path, str] | None: The selected subtitle file and its language code, or `None` if no subtitles are available.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    return _ytdlp_download_subs(url, video_id, languages, workdir)


def fetch_title_via_ytdlp(url: str) -> str | None:
    """Fetch the media title reported by yt-dlp.

    Parameters:
        url (str): The media URL to inspect.

    Returns:
        str | None: The first non-empty title line, or `None` if yt-dlp cannot provide a title.
    """
    result = subprocess.run(
        ["yt-dlp", "--skip-download", "--print", "title", url],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[0]
    return None


def download_audio_via_ytdlp(url: str, workdir: Path) -> Path | None:
    """
    Download the best available audio for a URL and return the resulting local file.

    Parameters:
        url (str): Media URL to download.
        workdir (Path): Directory where the downloaded audio file is saved.

    Returns:
        Path | None: The downloaded audio file, or `None` if the download fails or produces no file.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(workdir / "audio.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "-o",
        out_tmpl,
        url,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT_SECONDS
    )
    if result.returncode != 0:
        log.warning(
            "yt-dlp audio download failed for %s: %s", url, result.stderr[-500:]
        )
        return None
    matches = sorted(workdir.glob("audio.*"))
    return matches[0] if matches else None


def is_direct_media_url(url: str) -> bool:
    """Determine whether a URL points to a directly downloadable media file.

    Parameters:
        url (str): URL to inspect.

    Returns:
        bool: `true` if the URL path ends with a recognized media extension, `false` otherwise.
    """
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DIRECT_MEDIA_EXTENSIONS)


def _sanitize_filename(raw_filename: str) -> str:
    """
    Sanitize a filename derived from a URL to prevent path traversal.

    Parameters:
        raw_filename (str): Raw filename from URL path.

    Returns:
        str: Sanitized filename safe for use in file paths, or a default if empty/unsafe.
    """
    # Take only the basename (no directory components)
    basename = os.path.basename(raw_filename)

    # Reject dangerous values
    if not basename or basename in (".", ".."):
        return "download"

    # Remove any remaining path separators and dangerous characters
    sanitized = basename.replace(os.sep, "_")
    if os.altsep:
        sanitized = sanitized.replace(os.altsep, "_")

    # Ensure it doesn't start with a dot (hidden file) or dash (could be interpreted as flag)
    sanitized = sanitized.lstrip(".-")

    # If sanitization left nothing useful, use default
    return sanitized if sanitized else "download"


def _open_pinned(url: str, timeout: int) -> tuple:
    """
    Validate url, resolve it to an approved public IP, and open a connection
    pinned to that IP (correct Host header preserved) without following
    redirects.

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

    conn.request("GET", target, headers={**_HTTP_HEADERS, "Host": host_header})
    return conn.getresponse(), hostname


def _download_with_redirect_validation(
    url: str, dest: Path, max_redirects: int = 5
) -> None:
    """
    Download a file while validating every redirect hop against SSRF protection.

    Parameters:
        url (str): Initial URL to download.
        dest (Path): Destination file path.
        max_redirects (int): Maximum number of redirects to follow.

    Raises:
        ValueError: If a redirect chain exceeds max_redirects or any hop fails validation.
    """
    current_url = url

    for _ in range(max_redirects + 1):
        resp, _hostname = _open_pinned(current_url, timeout=1800)
        if resp.status in (301, 302, 303, 307, 308):
            with resp:
                location = resp.getheader("Location")
            if not location:
                raise ValueError(
                    f"Redirect response {resp.status} without Location header"
                )
            if not location.startswith(("http://", "https://")):
                location = urllib.parse.urljoin(current_url, location)
            log.info("Following redirect from %s to %s", current_url, location)
            current_url = location
            continue
        if not 200 <= resp.status < 300:
            with resp:
                raise OSError(
                    f"HTTP request failed with status {resp.status} {resp.reason}"
                )

        # No redirect (validated, connected, and pinned) - download the response.
        content_length = resp.getheader("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > MAX_MEDIA_BYTES:
                with resp:
                    pass
                raise ValueError(f"Media download exceeds {MAX_MEDIA_BYTES} byte limit")

        downloaded = 0
        try:
            with resp, open(dest, "wb") as f:
                while chunk := resp.read(64 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_MEDIA_BYTES:
                        raise ValueError(
                            f"Media download exceeds {MAX_MEDIA_BYTES} byte limit"
                        )
                    f.write(chunk)
            return
        except Exception:
            dest.unlink(missing_ok=True)
            raise

    raise ValueError(f"Too many redirects (>{max_redirects}) when downloading {url}")


def download_direct_file(url: str, workdir: Path) -> Path:
    """
    Download a direct media resource to the working directory.

    Parameters:
        url (str): URL of the media resource.
        workdir (Path): Directory where the downloaded file is saved.

    Returns:
        Path: Path to the downloaded media file.
    """
    workdir.mkdir(parents=True, exist_ok=True)

    # Extract and sanitize filename to prevent path traversal
    raw_filename = Path(urlparse(url).path).name or "download"
    filename = _sanitize_filename(raw_filename)

    dest = workdir / filename

    # Download with per-redirect validation
    _download_with_redirect_validation(url, dest)

    return dest


# Overcast episode pages don't host the audio directly - they link out to
# the podcast's own RSS feed. These patterns are matched against the raw
# page HTML (verified against a live overcast.fm episode page).
_OVERCAST_RSS_LINK_RE = re.compile(
    r'href="([^"]+)"\s*><img src="/img/badge-rss\.svg"', re.DOTALL
)
_OVERCAST_EPISODE_TITLE_RE = re.compile(r'<h2[^>]*class="title"[^>]*>([^<]+)</h2>')


def _safe_urlopen_with_validation(
    url: str, timeout: int = 30, max_redirects: int = 5
) -> bytes:
    """
    Open a URL with IP pinning, revalidating and repinning every redirect hop.

    Parameters:
        url (str): URL to fetch.
        timeout (int): Request timeout in seconds.

    Returns:
        bytes: Response body.

    Raises:
        ValueError: If validation fails or the redirect limit is exceeded.
    """
    current_url = url
    for _ in range(max_redirects + 1):
        resp, _hostname = _open_pinned(current_url, timeout=timeout)
        if resp.status in (301, 302, 303, 307, 308):
            with resp:
                location = resp.getheader("Location")
            if not location:
                raise ValueError(
                    f"Redirect response {resp.status} without Location header"
                )
            current_url = urllib.parse.urljoin(current_url, location)
            continue
        if not 200 <= resp.status < 300:
            with resp:
                raise OSError(
                    f"HTTP request failed with status {resp.status} {resp.reason}"
                )
        with resp:
            return resp.read()

    raise ValueError(f"Too many redirects (>{max_redirects}) when fetching {url}")


def resolve_overcast_episode(url: str) -> tuple[str, str] | None:
    """
    Resolve an Overcast episode page to its title and audio enclosure URL.

    Parameters:
        url (str): Overcast episode page URL.

    Returns:
        tuple[str, str] | None: Episode title and validated enclosure URL, or None
        if the page, feed, or matching episode cannot be resolved.
    """
    try:
        page_html = _safe_urlopen_with_validation(url, timeout=30).decode(
            "utf-8", errors="ignore"
        )

        title_match = _OVERCAST_EPISODE_TITLE_RE.search(page_html)
        rss_match = _OVERCAST_RSS_LINK_RE.search(page_html)
        if not title_match or not rss_match:
            log.warning("Overcast page %s has no recognizable title/RSS link.", url)
            return None

        episode_title = html.unescape(title_match.group(1)).strip()
        feed_url = html.unescape(rss_match.group(1)).strip()

        feed_xml = _safe_urlopen_with_validation(feed_url, timeout=30)
        root = ET.fromstring(feed_xml)

        episodes = {}
        for item in root.findall(".//item"):
            item_title_el = item.find("title")
            enclosure_el = item.find("enclosure")
            if item_title_el is None or enclosure_el is None:
                continue
            item_title = (item_title_el.text or "").strip()
            mp3_url = enclosure_el.get("url")
            if item_title and mp3_url:
                episodes[item_title] = mp3_url

        if episode_title in episodes:
            mp3_url = episodes[episode_title]
            validate_public_url(mp3_url)
            return episode_title, mp3_url

        close = get_close_matches(episode_title, episodes.keys(), n=1, cutoff=0.8)
        if close:
            mp3_url = episodes[close[0]]
            validate_public_url(mp3_url)
            return episode_title, mp3_url

        log.warning(
            "Overcast: no matching feed item for %r in %s.", episode_title, feed_url
        )
        return None
    except Exception as e:
        log.warning("Overcast resolution failed for %s: %s", url, e)
        return None


def find_sidecar_subtitle(local_path: Path) -> Path | None:
    """Find an SRT subtitle file beside a local media file.

    Parameters:
        local_path (Path): Path to the local media file.

    Returns:
        Path | None: The matching SRT sidecar path, or `None` if it does not exist.
    """
    candidate = local_path.with_suffix(".srt")
    return candidate if candidate.exists() else None


# --------------------------------------------------------------------------
# Generalized single-input processor
# --------------------------------------------------------------------------


def process_input(
    raw_input: str,
    cfg: dict,
    item_hint: dict | None = None,
    github_token: str | None = None,
    bridge_token: str | None = None,
) -> dict:
    """
    Process a media input through transcription, summarization, and GitHub publication.

    Parameters:
        raw_input (str): Local media path or media URL.
        cfg (dict): Pipeline, transcription, and GitHub configuration.
        item_hint (dict | None): Optional known metadata such as ``title``,
            ``published_at``, or ``video_id``.
        github_token (str | None): Optional pre-resolved GitHub authentication token.
        bridge_token (str | None): Optional pre-resolved bridge service authentication token.

    Returns:
        dict: Metadata containing ``title``, ``source_type``, ``note_path``, and
        ``subtitle_path``.

    Raises:
        NoTranscriptAvailableError: If no subtitles or transcribable audio is available.
    """
    item_hint = item_hint or {}
    source_type = detect_input_type(raw_input)

    trans_cfg = cfg.get("transcription", {}) or {}
    model_id = trans_cfg.get("model", "mlx-community/parakeet-tdt-0.6b-v3")

    # BRIDGE_URL env var overrides config.yaml's bridge.url, same
    # env-first pattern as resolve_secret() - lets a single config.yaml
    # work for both native testing (127.0.0.1) and containerized use
    # (host.docker.internal, the config.yaml default) without editing
    # the file to switch between them.
    bridge_url = os.environ.get("BRIDGE_URL") or cfg["bridge"]["url"]
    if bridge_token is None:
        bridge_token = resolve_secret(
            "BRIDGE_AUTH_TOKEN", cfg["bridge"]["auth_token_op_ref"]
        )

    title = item_hint.get("title")
    published_at = item_hint.get("published_at")
    video_id = None
    source_url = None
    raw_srt_body = None
    transcript_text = None
    lang = None

    with tempfile.TemporaryDirectory(prefix="pipeline-") as tmp:
        workdir = Path(tmp)

        if source_type == "local_file":
            local_path = Path(raw_input)
            if title is None:
                title = local_path.stem

            sidecar = find_sidecar_subtitle(local_path)
            if sidecar is not None:
                raw_srt_body = sidecar.read_text(encoding="utf-8", errors="ignore")
                transcript_text = srt_to_plain_text(sidecar)
                lang = "sidecar"
            else:
                raw_srt_body, transcript_text = bridge_client.transcribe_audio(
                    local_path,
                    model_id,
                    bridge_url,
                    bridge_token,
                )
                lang = "parakeet"

        elif source_type == "youtube":
            video_id = item_hint.get("video_id") or extract_youtube_video_id(raw_input)
            source_url = f"https://www.youtube.com/watch?v={video_id}"
            if title is None:
                title = fetch_title_via_ytdlp(source_url) or video_id

            subs_result = download_subtitles(
                video_id, cfg["youtube"]["subtitle_languages"], workdir
            )
            if subs_result is not None:
                srt_path, lang = subs_result
                raw_srt_body = srt_path.read_text(encoding="utf-8", errors="ignore")
                transcript_text = srt_to_plain_text(srt_path)
            else:
                # No captions at all - fall back to downloading audio and
                # transcribing it locally instead of giving up.
                audio_path = download_audio_via_ytdlp(source_url, workdir)
                if audio_path is not None:
                    raw_srt_body, transcript_text = bridge_client.transcribe_audio(
                        audio_path,
                        model_id,
                        bridge_url,
                        bridge_token,
                    )
                    lang = "parakeet"

        else:  # generic_link
            source_url = raw_input
            host = (urlparse(raw_input).hostname or "").lower()

            overcast_resolved = (
                resolve_overcast_episode(raw_input)
                if OVERCAST_HOST_RE.search(host)
                else None
            )

            if overcast_resolved is not None:
                # Podcasts have no video component and no synced subtitles -
                # go straight to the real mp3 enclosure and transcribe it.
                resolved_title, mp3_url = overcast_resolved
                if title is None:
                    title = resolved_title
                media_path = download_direct_file(mp3_url, workdir)
                raw_srt_body, transcript_text = bridge_client.transcribe_audio(
                    media_path,
                    model_id,
                    bridge_url,
                    bridge_token,
                )
                lang = "parakeet"
            else:
                validate_public_url(raw_input)

                if title is None:
                    title = fetch_title_via_ytdlp(raw_input)

                subs_result = _ytdlp_download_subs(
                    raw_input,
                    "transcript",
                    cfg["youtube"]["subtitle_languages"],
                    workdir,
                )
                if subs_result is not None:
                    srt_path, lang = subs_result
                    raw_srt_body = srt_path.read_text(encoding="utf-8", errors="ignore")
                    transcript_text = srt_to_plain_text(srt_path)
                else:
                    if is_direct_media_url(raw_input):
                        media_path = download_direct_file(raw_input, workdir)
                    else:
                        media_path = download_audio_via_ytdlp(raw_input, workdir)
                    if media_path is not None:
                        raw_srt_body, transcript_text = bridge_client.transcribe_audio(
                            media_path,
                            model_id,
                            bridge_url,
                            bridge_token,
                        )
                        lang = "parakeet"
                        if title is None:
                            title = media_path.stem

            if title is None:
                title = raw_input[:80]

        if raw_srt_body is None or transcript_text is None:
            raise NoTranscriptAvailableError(
                f"No subtitles or transcribable audio for {raw_input!r}"
            )

        header = f"{title}\n{source_url or raw_input}\n\n"
        final_srt_text = header + raw_srt_body

    # temp dir (and any scratch download/audio file) is gone past this point;
    # everything needed is captured in local variables above.

    log.info("Got transcript for %r via %s (%s)", title, source_type, lang)

    summary = bridge_client.summarize(transcript_text, bridge_url, bridge_token)
    content_tags = bridge_client.generate_tags(
        transcript_text, bridge_url, bridge_token
    )

    if github_token is None:
        github_token = resolve_secret("GITHUB_TOKEN", cfg["github"]["token_op_ref"])

    subs_repo = ensure_repo(
        cfg["github"]["subtitles_repo_url"],
        cfg["github"]["subtitles_repo_path"],
        cfg["github"]["subtitles_branch"],
        github_token,
    )
    vault_repo = ensure_repo(
        cfg["github"]["vault_repo_url"],
        cfg["github"]["vault_repo_path"],
        cfg["github"]["vault_branch"],
        github_token,
    )

    slug = slugify(title)
    subs_dir = subs_repo / cfg["github"]["subtitles_dir_in_repo"]
    subs_dir.mkdir(parents=True, exist_ok=True)
    subtitle_path = subs_dir / f"{slug}.{lang}.srt"
    subtitle_path.write_text(final_srt_text, encoding="utf-8")

    commit_and_push(
        subs_repo,
        [subtitle_path],
        f"Add transcript for {source_type}: {title}",
        cfg["github"]["commit_author_name"],
        cfg["github"]["commit_author_email"],
        github_token,
    )

    subs_owner_repo = re.sub(
        r"^https://github\.com/|\.git$", "", cfg["github"]["subtitles_repo_url"]
    )
    subtitle_rel_path = subtitle_path.relative_to(subs_repo)
    subtitle_github_url = (
        f"https://github.com/{subs_owner_repo}/blob/"
        f"{cfg['github']['subtitles_branch']}/{subtitle_rel_path}"
    )

    note_dir = vault_repo / cfg["github"]["vault_notes_dir"]
    note_dir.mkdir(parents=True, exist_ok=True)
    note_path = note_dir / f"{slug}.md"
    note_path.write_text(
        build_note(
            title,
            source_type,
            source_url,
            video_id,
            published_at,
            subtitle_github_url,
            summary,
            content_tags,
        ),
        encoding="utf-8",
    )

    commit_and_push(
        vault_repo,
        [note_path],
        f"Add video summary note: {title}",
        cfg["github"]["commit_author_name"],
        cfg["github"]["commit_author_email"],
        github_token,
    )

    return {
        "title": title,
        "source_type": source_type,
        "note_path": note_path,
        "subtitle_path": subtitle_path,
    }


# --------------------------------------------------------------------------
# Main (standalone CLI)
# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", required=True, help="Local file path, YouTube URL, or generic link."
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)

    try:
        result = process_input(args.input, cfg)
        log.info("Done: %s -> %s", result["title"], result["note_path"])
    except NoTranscriptAvailableError as e:
        log.error("No transcript available: %s", e)
        sys.exit(2)
    except Exception:
        log.error("Failed:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
