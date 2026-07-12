#!/usr/bin/env python3
"""
Media (local file / YouTube URL / Spotify podcast episode / any other
yt-dlp-supported link) -> transcript + AI summary -> GitHub + Obsidian
pipeline.

Processes ONE input at a time:
  1. Figures out whether the input is a local file, a YouTube URL, a Spotify
     podcast episode, or some other link.
  2. Gets a transcript: existing subtitles/captions if available (YouTube,
     or any site yt-dlp can extract from), an RSS/Podcasting-2.0 transcript
     link (Spotify episodes, via podcast_rss.py), a sidecar .srt file (local
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
import logging
import os
import re
import subprocess
import sys
import tempfile
import traceback
import urllib.request
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import defusedxml.ElementTree as ET

import bridge_client
import podcast_rss
import spotify_client
from core import (
    MAX_MEDIA_BYTES,
    YTDLP_TIMEOUT_SECONDS,
    build_note,
    commit_and_push,
    ensure_repo,
    load_config,
    resolve_secret,
    safe_filename,
    slugify,
    srt_to_plain_text,
    validate_public_url,
)
from core import open_pinned as _open_pinned

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
    Classifies an HTTP(S) URL as YouTube, Overcast, a Spotify podcast
    episode, or a generic link (any other yt-dlp-supported URL).

    Parameters:
        raw_input (str): HTTP(S) URL to classify.

    Returns:
        str: `"youtube"`, `"overcast"`, `"spotify"`, or `"generic_link"`.

    Raises:
        ValueError: If the input is not a hosted HTTP(S) URL.
    """
    parsed = urlparse(raw_input)
    if parsed.scheme in ("http", "https"):
        if not parsed.hostname:
            raise ValueError(
                f"Input {raw_input!r} has an HTTP(S) scheme but no hostname."
            )
        host = parsed.hostname.lower()
        if YOUTUBE_HOST_RE.search(host):
            return "youtube"
        if OVERCAST_HOST_RE.search(host):
            return "overcast"
        # Only Spotify *episode* URLs get provider-specific handling - show
        # pages and music tracks fall through to generic_link, where yt-dlp
        # (which doesn't support Spotify) will simply fail to find anything,
        # a clear enough "unsupported" outcome without special-casing them.
        if spotify_client.is_spotify_episode_url(raw_input):
            return "spotify"
        return "generic_link"
    raise ValueError(f"Input {raw_input!r} is not a valid hosted http(s) URL.")


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


def _subtitle_languages(cfg: dict) -> list[str]:
    """
    Preferred subtitle language codes, source-neutral.

    Prefers cfg["media"]["subtitle_languages"] if present, otherwise falls
    back to the legacy cfg["youtube"]["subtitle_languages"] so existing
    config.yaml files keep working unchanged.
    """
    media_cfg = cfg.get("media") or {}
    if "subtitle_languages" in media_cfg:
        return media_cfg["subtitle_languages"]
    return cfg["youtube"]["subtitle_languages"]


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


def fetch_published_at_via_ytdlp(url: str) -> str | None:
    """Fetch the upload/release date yt-dlp reports for a URL.

    Parameters:
        url (str): The media URL to inspect.

    Returns:
        str | None: An ISO `YYYY-MM-DD` date, or `None` if yt-dlp can't
        provide one (unsupported site, no date in the source metadata, etc.)
    """
    try:
        result = subprocess.run(
            ["yt-dlp", "--skip-download", "--print", "%(upload_date)s", url],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp published_at fetch timed out for %s after 60 seconds", url)
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None
    raw = result.stdout.strip().splitlines()[0].strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
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


def resolve_overcast_episode(url: str) -> tuple[str, str, str | None] | None:
    """
    Resolve an Overcast episode page to its title, audio enclosure URL, and
    (if the feed's matched item has one) publish date.

    Parameters:
        url (str): Overcast episode page URL.

    Returns:
        tuple[str, str, str | None] | None: Episode title, validated
        enclosure URL, and an ISO `YYYY-MM-DD` publish date (or `None` if
        the feed item has no `<pubDate>`) - or `None` entirely if the page,
        feed, or matching episode cannot be resolved.
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

        items_by_title = {}
        for item in root.findall(".//item"):
            item_title_el = item.find("title")
            enclosure_el = item.find("enclosure")
            if item_title_el is None or enclosure_el is None:
                continue
            item_title = (item_title_el.text or "").strip()
            mp3_url = enclosure_el.get("url")
            if item_title and mp3_url:
                items_by_title[item_title] = item

        matched_item = items_by_title.get(episode_title)
        if matched_item is None:
            close = get_close_matches(
                episode_title, items_by_title.keys(), n=1, cutoff=0.8
            )
            matched_item = items_by_title[close[0]] if close else None

        if matched_item is not None:
            mp3_url = matched_item.find("enclosure").get("url")
            validate_public_url(mp3_url)
            return episode_title, mp3_url, podcast_rss.get_published_at(matched_item)

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
# Provider-neutral resolution
#
# Each _resolve_* function below handles exactly one source_type and returns
# a ProviderResolution - process_input() itself no longer has any
# source-specific branching beyond picking which one to call, so a future
# source only needs a new _resolve_* function plus one dispatch line.
# --------------------------------------------------------------------------


@dataclass
class ProviderResolution:
    """Normalized result of resolving one input to a transcript.

    transcript_ext defaults to "srt" (every existing source produces real
    SRT); an RSS-sourced plain-text transcript (no timing info available)
    is the one case that overrides it to "txt".
    """

    title: str
    source_url: str | None = None
    redirect_url: str | None = None
    video_id: str | None = None
    published_at: str | None = None
    raw_transcript_body: str | None = None
    transcript_text: str | None = None
    transcript_ext: str = "srt"
    lang: str | None = None
    extra_frontmatter: dict[str, str] | None = None


def _resolve_local_file(
    local_path: Path,
    item_hint: dict,
    bridge_url: str,
    bridge_token: str,
    model_id: str,
) -> ProviderResolution:
    title = item_hint.get("title") or local_path.stem

    sidecar = find_sidecar_subtitle(local_path)
    if sidecar is not None:
        raw = sidecar.read_text(encoding="utf-8", errors="ignore")
        text = srt_to_plain_text(sidecar)
        lang = "sidecar"
    else:
        raw, text = bridge_client.transcribe_audio(
            local_path, model_id, bridge_url, bridge_token
        )
        lang = "parakeet"

    return ProviderResolution(
        title=title,
        published_at=item_hint.get("published_at"),
        raw_transcript_body=raw,
        transcript_text=text,
        lang=lang,
    )


def _resolve_youtube(
    raw_input: str,
    item_hint: dict,
    cfg: dict,
    workdir: Path,
    bridge_url: str,
    bridge_token: str,
    model_id: str,
) -> ProviderResolution:
    video_id = item_hint.get("video_id") or extract_youtube_video_id(raw_input)
    source_url = f"https://www.youtube.com/watch?v={video_id}"
    title = item_hint.get("title") or fetch_title_via_ytdlp(source_url) or video_id
    published_at = item_hint.get("published_at") or fetch_published_at_via_ytdlp(
        source_url
    )

    raw = text = lang = None
    subs_result = download_subtitles(video_id, _subtitle_languages(cfg), workdir)
    if subs_result is not None:
        srt_path, lang = subs_result
        raw = srt_path.read_text(encoding="utf-8", errors="ignore")
        text = srt_to_plain_text(srt_path)
    else:
        # No captions at all - fall back to downloading audio and
        # transcribing it locally instead of giving up.
        audio_path = download_audio_via_ytdlp(source_url, workdir)
        if audio_path is not None:
            raw, text = bridge_client.transcribe_audio(
                audio_path, model_id, bridge_url, bridge_token
            )
            lang = "parakeet"

    return ProviderResolution(
        title=title,
        source_url=source_url,
        video_id=video_id,
        published_at=published_at,
        raw_transcript_body=raw,
        transcript_text=text,
        lang=lang,
    )


def _resolve_generic_link(
    raw_input: str,
    item_hint: dict,
    cfg: dict,
    workdir: Path,
    bridge_url: str,
    bridge_token: str,
    model_id: str,
) -> ProviderResolution:
    """Any yt-dlp-supported URL that isn't YouTube, Overcast, or a Spotify
    episode.

    Tries embedded/manual/automatic subtitles first (same as YouTube), then
    falls back to downloading audio and transcribing it.
    """
    source_url = raw_input
    title = item_hint.get("title")

    validate_public_url(raw_input)

    published_at = item_hint.get("published_at") or fetch_published_at_via_ytdlp(
        raw_input
    )

    if title is None:
        title = fetch_title_via_ytdlp(raw_input)

    raw = text = lang = None
    subs_result = _ytdlp_download_subs(
        raw_input, "transcript", _subtitle_languages(cfg), workdir
    )
    if subs_result is not None:
        srt_path, lang = subs_result
        raw = srt_path.read_text(encoding="utf-8", errors="ignore")
        text = srt_to_plain_text(srt_path)
    else:
        media_path = (
            download_direct_file(raw_input, workdir)
            if is_direct_media_url(raw_input)
            else download_audio_via_ytdlp(raw_input, workdir)
        )
        if media_path is not None:
            raw, text = bridge_client.transcribe_audio(
                media_path, model_id, bridge_url, bridge_token
            )
            lang = "parakeet"
            if title is None:
                title = media_path.stem

    if title is None:
        title = raw_input[:80]

    return ProviderResolution(
        title=title,
        source_url=source_url,
        published_at=published_at,
        raw_transcript_body=raw,
        transcript_text=text,
        lang=lang,
    )


def _resolve_overcast(
    raw_input: str,
    item_hint: dict,
    cfg: dict,
    workdir: Path,
    bridge_url: str,
    bridge_token: str,
    model_id: str,
) -> ProviderResolution:
    """Overcast episode page: resolve_overcast_episode() finds the real RSS
    mp3 enclosure (Overcast doesn't host audio itself), then transcribes it
    through the same Parakeet path as any other audio source. Falls back to
    generic yt-dlp handling if Overcast scraping fails."""
    title = item_hint.get("title")
    published_at = item_hint.get("published_at")

    overcast_resolved = resolve_overcast_episode(raw_input)
    if overcast_resolved is None:
        log.warning(
            "Could not resolve Overcast episode %s; falling back to yt-dlp", raw_input
        )
        return _resolve_generic_link(
            raw_input, item_hint, cfg, workdir, bridge_url, bridge_token, model_id
        )

    resolved_title, mp3_url, resolved_published_at = overcast_resolved
    if title is None:
        title = resolved_title
    if published_at is None:
        published_at = resolved_published_at

    media_path = download_direct_file(mp3_url, workdir)
    raw, text = bridge_client.transcribe_audio(
        media_path, model_id, bridge_url, bridge_token
    )
    return ProviderResolution(
        title=title,
        source_url=raw_input,
        redirect_url=mp3_url,
        published_at=published_at,
        raw_transcript_body=raw,
        transcript_text=text,
        lang="parakeet",
    )


def _resolve_spotify(
    raw_input: str,
    item_hint: dict,
    cfg: dict,
    workdir: Path,
    bridge_url: str,
    bridge_token: str,
    model_id: str,
) -> ProviderResolution:
    """Spotify podcast episode: metadata via spotify_client.py, transcript
    or audio via podcast_rss.py's RSS/Podcasting-2.0 discovery.

    Never downloads Spotify-streamed audio - if RSS resolution can't find a
    transcript OR the original enclosure, this returns with no transcript at
    all and process_input()'s existing NoTranscriptAvailableError check
    reports it, same as any other source that comes up empty.
    """
    title = item_hint.get("title")
    published_at = item_hint.get("published_at")

    metadata = spotify_client.resolve_episode_metadata(raw_input, cfg)
    if metadata is None:
        log.warning(
            "Spotify episode %s is unsupported, private, or unavailable "
            "(no metadata via API or page scrape).",
            raw_input,
        )
        return ProviderResolution(title=title or raw_input[:80], source_url=raw_input)

    if title is None:
        title = metadata.get("title") or raw_input[:80]
    show_name = metadata.get("show_name")
    # RSS's <pubDate> (below) is preferred - a consistently full date, unlike
    # Spotify's release_date whose precision varies by show - but fall back
    # to it now in case RSS resolution fails entirely.
    published_at = published_at or metadata.get("release_date")

    rss_result = (
        podcast_rss.resolve_episode_from_rss(show_name, title) if show_name else None
    )
    if rss_result is None:
        log.warning(
            "Could not locate an RSS feed/episode match for Spotify show %r "
            "episode %r.",
            show_name,
            title,
        )
        return ProviderResolution(
            title=title, source_url=raw_input, published_at=published_at
        )

    published_at = (
        item_hint.get("published_at") or rss_result.get("published_at") or published_at
    )
    extra_frontmatter = {"podcast_feed_url": rss_result["feed_url"]}
    transcript = rss_result["transcript"]
    if transcript is not None:
        ext, raw, text = transcript
        return ProviderResolution(
            title=title,
            source_url=raw_input,
            redirect_url=rss_result.get("transcript_url"),
            published_at=published_at,
            raw_transcript_body=raw,
            transcript_text=text,
            transcript_ext=ext,
            lang="rss",
            extra_frontmatter=extra_frontmatter,
        )

    enclosure_url = rss_result["enclosure_url"]
    if enclosure_url is None:
        log.warning("No transcript or audio enclosure found in RSS feed for %r.", title)
        return ProviderResolution(
            title=title,
            source_url=raw_input,
            published_at=published_at,
            extra_frontmatter=extra_frontmatter,
        )

    # No published transcript - fall back to the podcast's own original RSS
    # audio (not Spotify's stream) through the same local Parakeet path
    # every other source uses.
    media_path = download_direct_file(enclosure_url, workdir)
    raw, text = bridge_client.transcribe_audio(
        media_path, model_id, bridge_url, bridge_token
    )
    return ProviderResolution(
        title=title,
        source_url=raw_input,
        redirect_url=enclosure_url,
        published_at=published_at,
        raw_transcript_body=raw,
        transcript_text=text,
        lang="parakeet",
        extra_frontmatter=extra_frontmatter,
    )


# --------------------------------------------------------------------------
# Generalized single-input processor
# --------------------------------------------------------------------------


def process_input(
    raw_input: str | Path,
    cfg: dict,
    item_hint: dict | None = None,
    github_token: str | None = None,
    bridge_token: str | None = None,
) -> dict:
    """
    Process a media input through transcription, summarization, and GitHub publication.

    Parameters:
        raw_input (str | Path): Hosted media URL, or a local media `Path`.
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
    local_path = raw_input if isinstance(raw_input, Path) else None
    if local_path is not None and not local_path.is_file():
        raise ValueError(
            f"Local input {local_path!r} is not an existing file (got directory or non-existent path)."
        )
    source_type = (
        "local_file" if local_path is not None else detect_input_type(raw_input)
    )

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

    with tempfile.TemporaryDirectory(prefix="pipeline-") as tmp:
        workdir = Path(tmp)

        if source_type == "local_file":
            assert local_path is not None
            resolution = _resolve_local_file(
                local_path, item_hint, bridge_url, bridge_token, model_id
            )
        elif source_type == "youtube":
            resolution = _resolve_youtube(
                raw_input, item_hint, cfg, workdir, bridge_url, bridge_token, model_id
            )
        elif source_type == "overcast":
            resolution = _resolve_overcast(
                raw_input, item_hint, cfg, workdir, bridge_url, bridge_token, model_id
            )
        elif source_type == "spotify":
            resolution = _resolve_spotify(
                raw_input, item_hint, cfg, workdir, bridge_url, bridge_token, model_id
            )
        else:  # generic_link
            resolution = _resolve_generic_link(
                raw_input, item_hint, cfg, workdir, bridge_url, bridge_token, model_id
            )

        title = resolution.title
        source_url = resolution.source_url
        redirect_url = resolution.redirect_url
        video_id = resolution.video_id
        published_at = resolution.published_at
        raw_srt_body = resolution.raw_transcript_body
        transcript_text = resolution.transcript_text
        transcript_ext = resolution.transcript_ext
        lang = resolution.lang
        extra_frontmatter = resolution.extra_frontmatter

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
    subtitle_path = subs_dir / f"{slug}.{lang}.{transcript_ext}"
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
    note_path = note_dir / f"{safe_filename(title)}.md"
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
            extra_frontmatter=extra_frontmatter,
            redirect_url=redirect_url,
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
        "--input",
        required=True,
        help=(
            "Local file path, YouTube URL, Spotify podcast episode URL, or "
            "any other yt-dlp-supported link."
        ),
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)

    try:
        parsed_input = urlparse(args.input)
        if parsed_input.scheme in ("http", "https"):
            cli_input = args.input
        else:
            local_file = Path(args.input)
            if not local_file.is_file():
                log.error(
                    "Input %r is not an existing local file and not a valid http(s) URL.",
                    args.input,
                )
                sys.exit(1)
            cli_input = local_file
        result = process_input(cli_input, cfg)
        log.info("Done: %s -> %s", result["title"], result["note_path"])
    except NoTranscriptAvailableError as e:
        log.error("No transcript available: %s", e)
        sys.exit(2)
    except Exception:
        log.error("Failed:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
