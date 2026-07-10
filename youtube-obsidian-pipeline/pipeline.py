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
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import bridge_client
from core import (
    build_note, commit_and_push, ensure_repo, load_config, resolve_secret, slugify, srt_to_plain_text,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pipeline")

YOUTUBE_HOST_RE = re.compile(r"(^|\.)(youtube\.com|youtu\.be)$")
OVERCAST_HOST_RE = re.compile(r"(^|\.)overcast\.fm$")
DIRECT_MEDIA_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".flac"}
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
    """Returns 'local_file' | 'youtube' | 'generic_link'."""
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

def _ytdlp_download_subs(url: str, out_basename: str, languages: list[str], workdir: Path) -> tuple[Path, str] | None:
    """Downloads subtitles (manual captions preferred, falls back to
    auto-generated) as .srt for any URL yt-dlp can extract from. Returns
    (path_to_srt, language_code), or None if no subtitles were found."""
    workdir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(workdir / f"{out_basename}.%(ext)s")
    lang_arg = ",".join(languages)
    cmd = [
        "yt-dlp", "--skip-download", "--write-subs", "--write-auto-sub",
        "--sub-langs", lang_arg, "--convert-subs", "srt", "-o", out_tmpl, url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
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


def download_subtitles(video_id: str, languages: list[str], workdir: Path) -> tuple[Path, str] | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    return _ytdlp_download_subs(url, video_id, languages, workdir)


def fetch_title_via_ytdlp(url: str) -> str | None:
    result = subprocess.run(
        ["yt-dlp", "--skip-download", "--print", "title", url],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[0]
    return None


def download_audio_via_ytdlp(url: str, workdir: Path) -> Path | None:
    workdir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(workdir / "audio.%(ext)s")
    cmd = [
        "yt-dlp", "-f", "bestaudio/best", "--extract-audio", "--audio-format", "mp3",
        "-o", out_tmpl, url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        log.warning("yt-dlp audio download failed for %s: %s", url, result.stderr[-500:])
        return None
    matches = sorted(workdir.glob("audio.*"))
    return matches[0] if matches else None


def is_direct_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in DIRECT_MEDIA_EXTENSIONS)


def download_direct_file(url: str, workdir: Path) -> Path:
    """Downloads a URL known to be a direct media file. Sends a normal
    browser-ish User-Agent since some CDNs (e.g. podcast hosts) reject
    Python's default urllib UA."""
    workdir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlparse(url).path).name or "download"
    dest = workdir / filename
    with urlopen(Request(url, headers=_HTTP_HEADERS), timeout=1800) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest


# Overcast episode pages don't host the audio directly - they link out to
# the podcast's own RSS feed. These patterns are matched against the raw
# page HTML (verified against a live overcast.fm episode page).
_OVERCAST_RSS_LINK_RE = re.compile(r'href="([^"]+)"\s*><img src="/img/badge-rss\.svg"', re.DOTALL)
_OVERCAST_EPISODE_TITLE_RE = re.compile(r'<h2[^>]*class="title"[^>]*>([^<]+)</h2>')


def resolve_overcast_episode(url: str) -> tuple[str, str] | None:
    """Resolves an overcast.fm episode page to (episode_title, mp3_url) by
    finding the podcast's RSS feed link on the page, then matching this
    episode's title against the feed's <item> titles to get the real
    <enclosure> (mp3) URL - always audio, never video.

    Returns None if any step fails (no RSS link found, feed unreachable,
    no matching item), so the caller can fall back to generic link
    handling instead of failing outright.
    """
    try:
        page_html = urlopen(Request(url, headers=_HTTP_HEADERS), timeout=30).read().decode("utf-8", errors="ignore")

        title_match = _OVERCAST_EPISODE_TITLE_RE.search(page_html)
        rss_match = _OVERCAST_RSS_LINK_RE.search(page_html)
        if not title_match or not rss_match:
            log.warning("Overcast page %s has no recognizable title/RSS link.", url)
            return None

        episode_title = html.unescape(title_match.group(1)).strip()
        feed_url = html.unescape(rss_match.group(1)).strip()

        feed_xml = urlopen(Request(feed_url, headers=_HTTP_HEADERS), timeout=30).read()
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
            return episode_title, episodes[episode_title]

        close = get_close_matches(episode_title, episodes.keys(), n=1, cutoff=0.8)
        if close:
            return episode_title, episodes[close[0]]

        log.warning("Overcast: no matching feed item for %r in %s.", episode_title, feed_url)
        return None
    except Exception as e:
        log.warning("Overcast resolution failed for %s: %s", url, e)
        return None


def find_sidecar_subtitle(local_path: Path) -> Path | None:
    """Only .srt sidecars are recognized (same basename, .srt extension) -
    a .vtt sidecar falls through to transcription instead, to avoid a
    timestamp-format conversion for a rare case."""
    candidate = local_path.with_suffix(".srt")
    return candidate if candidate.exists() else None


# --------------------------------------------------------------------------
# Generalized single-input processor
# --------------------------------------------------------------------------

def process_input(raw_input: str, cfg: dict, item_hint: dict | None = None,
                   github_token: str | None = None, bridge_token: str | None = None) -> dict:
    """
    Processes one media input end-to-end: resolve -> transcript -> summarize
    -> commit transcript -> commit note.

    item_hint: optional {"title": ..., "published_at": ..., "video_id": ...}
    with metadata the caller already knows (e.g. fetch_playlist.py, which
    already has this from the YouTube API and shouldn't need to re-derive
    it). Any key not supplied is derived internally.

    bridge_token: pre-resolved auth token for host_bridge.py. If not
    supplied, resolved once here via resolve_secret() (BRIDGE_AUTH_TOKEN
    env var if set, e.g. injected by docker-compose, else op_read()) -
    callers processing many items in a loop should resolve it once
    themselves and pass it in (same pattern as github_token) to avoid a
    1Password CLI call per item.

    Returns {"title", "source_type", "note_path", "subtitle_path"} on
    success. Raises NoTranscriptAvailableError if no transcript could be
    obtained by any means (maps to "skip permanently, don't retry" for
    callers). Raises any other Exception for transient/retryable failures.
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
        bridge_token = resolve_secret("BRIDGE_AUTH_TOKEN", cfg["bridge"]["auth_token_op_ref"])

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
                    local_path, model_id, bridge_url, bridge_token,
                )
                lang = "parakeet"

        elif source_type == "youtube":
            video_id = item_hint.get("video_id") or extract_youtube_video_id(raw_input)
            source_url = f"https://www.youtube.com/watch?v={video_id}"
            if title is None:
                title = fetch_title_via_ytdlp(source_url) or video_id

            subs_result = download_subtitles(video_id, cfg["youtube"]["subtitle_languages"], workdir)
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
                        audio_path, model_id, bridge_url, bridge_token,
                    )
                    lang = "parakeet"

        else:  # generic_link
            source_url = raw_input
            host = (urlparse(raw_input).hostname or "").lower()

            overcast_resolved = resolve_overcast_episode(raw_input) if OVERCAST_HOST_RE.search(host) else None

            if overcast_resolved is not None:
                # Podcasts have no video component and no synced subtitles -
                # go straight to the real mp3 enclosure and transcribe it.
                resolved_title, mp3_url = overcast_resolved
                if title is None:
                    title = resolved_title
                media_path = download_direct_file(mp3_url, workdir)
                raw_srt_body, transcript_text = bridge_client.transcribe_audio(
                    media_path, model_id, bridge_url, bridge_token,
                )
                lang = "parakeet"
            else:
                if title is None:
                    title = fetch_title_via_ytdlp(raw_input)

                subs_result = _ytdlp_download_subs(raw_input, "transcript", cfg["youtube"]["subtitle_languages"], workdir)
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
                            media_path, model_id, bridge_url, bridge_token,
                        )
                        lang = "parakeet"
                        if title is None:
                            title = media_path.stem

            if title is None:
                title = raw_input[:80]

        if raw_srt_body is None or transcript_text is None:
            raise NoTranscriptAvailableError(f"No subtitles or transcribable audio for {raw_input!r}")

        header = f"{title}\n{source_url or raw_input}\n\n"
        final_srt_text = header + raw_srt_body

    # temp dir (and any scratch download/audio file) is gone past this point;
    # everything needed is captured in local variables above.

    log.info("Got transcript for %r via %s (%s)", title, source_type, lang)

    summary = bridge_client.summarize(transcript_text, bridge_url, bridge_token)
    content_tags = bridge_client.generate_tags(transcript_text, bridge_url, bridge_token)

    if github_token is None:
        github_token = resolve_secret("GITHUB_TOKEN", cfg["github"]["token_op_ref"])

    subs_repo = ensure_repo(
        cfg["github"]["subtitles_repo_url"], cfg["github"]["subtitles_repo_path"],
        cfg["github"]["subtitles_branch"], github_token,
    )
    vault_repo = ensure_repo(
        cfg["github"]["vault_repo_url"], cfg["github"]["vault_repo_path"],
        cfg["github"]["vault_branch"], github_token,
    )

    slug = slugify(title)
    subs_dir = subs_repo / cfg["github"]["subtitles_dir_in_repo"]
    subs_dir.mkdir(parents=True, exist_ok=True)
    subtitle_path = subs_dir / f"{slug}.{lang}.srt"
    subtitle_path.write_text(final_srt_text, encoding="utf-8")

    commit_and_push(
        subs_repo, [subtitle_path],
        f"Add transcript for {source_type}: {title}",
        cfg["github"]["commit_author_name"], cfg["github"]["commit_author_email"],
    )

    subs_owner_repo = re.sub(r"^https://github\.com/|\.git$", "", cfg["github"]["subtitles_repo_url"])
    subtitle_rel_path = subtitle_path.relative_to(subs_repo)
    subtitle_github_url = (
        f"https://github.com/{subs_owner_repo}/blob/"
        f"{cfg['github']['subtitles_branch']}/{subtitle_rel_path}"
    )

    note_dir = vault_repo / cfg["github"]["vault_notes_dir"]
    note_dir.mkdir(parents=True, exist_ok=True)
    note_path = note_dir / f"{slug}.md"
    note_path.write_text(build_note(
        title, source_type, source_url, video_id, published_at, subtitle_github_url, summary,
        content_tags,
    ))

    commit_and_push(
        vault_repo, [note_path],
        f"Add video summary note: {title}",
        cfg["github"]["commit_author_name"], cfg["github"]["commit_author_email"],
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
    parser.add_argument("--input", required=True, help="Local file path, YouTube URL, or generic link.")
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
