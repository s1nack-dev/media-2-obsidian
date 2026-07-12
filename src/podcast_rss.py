#!/usr/bin/env python3
"""
RSS/Podcasting 2.0 transcript discovery, used by spotify_client.py (Spotify
exposes episode/show names via its Web API but never the underlying RSS feed
or a transcript) and reusable by any future provider that only has a show
name + episode title to go on.

Two-step resolution, mirroring pipeline.py's existing resolve_overcast_episode()
pattern (page -> RSS feed -> matched <item> by title) but starting one step
earlier since there's no page to scrape a feed link out of:
  1. discover_feed_via_itunes() - looks up the podcast's RSS feed URL from
     its show name via Apple's free, unauthenticated iTunes Search API
     (no credentials, no rate-limit key - the same technique most
     "podcast app" integrations use when only a show name is known).
  2. match_episode_item() - fetches that feed and finds the <item> whose
     <title> matches the episode title (exact, falling back to a close
     match - same difflib approach as Overcast resolution).

From there, find_transcript_candidates() looks for Podcasting 2.0
<podcast:transcript> links on the matched item (SRT/VTT/JSON/TXT), and
get_enclosure_url() falls back to the plain <enclosure> audio URL when no
transcript is published - both call sites feed into pipeline.py's existing
subtitle/audio/transcription flow unchanged.
"""

import json
import logging
import re
from difflib import get_close_matches
from urllib.parse import quote, urljoin

import defusedxml.ElementTree as ET

from core import safe_fetch, srt_text_to_plain_text, validate_public_url

log = logging.getLogger("pipeline")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
PODCAST_NS = "{https://podcastindex.org/namespace/1.0}"

# Preference order when a feed publishes the same transcript in multiple
# formats - SRT/VTT carry timing info we can store directly, JSON (Podcasting
# 2.0's segment format) we convert to SRT, plain text has no timing at all.
_TRANSCRIPT_TYPE_RANK = {
    "application/srt": 0,
    "application/x-subrip": 0,
    "text/srt": 0,
    "text/vtt": 1,
    "application/json": 2,
    "text/plain": 3,
}


def discover_feed_via_itunes(show_name: str, timeout: int = 15) -> str | None:
    """
    Look up a podcast's RSS feed URL by show name via the iTunes Search API.

    Parameters:
        show_name (str): Podcast show name.
        timeout (int): Request timeout in seconds.

    Returns:
        str | None: The feed URL of the best-matching show, or None if the
        API call fails or returns no results.
    """
    if not show_name:
        return None
    url = f"{ITUNES_SEARCH_URL}?media=podcast&entity=podcast&limit=1&term={quote(show_name)}"
    try:
        validate_public_url(url)
        status, _headers, body = safe_fetch(url, timeout=timeout)
        if status != 200:
            log.warning(
                "iTunes Search API returned status %d for %r", status, show_name
            )
            return None
        payload = json.loads(body)
        results = payload.get("results") or []
        if not results:
            return None
        return results[0].get("feedUrl")
    except Exception as e:
        log.warning("iTunes feed discovery failed for %r: %s", show_name, e)
        return None


def _fetch_feed_root(feed_url: str, timeout: int = 30):
    """Fetch and parse an RSS feed, validating the URL first (SSRF)."""
    validate_public_url(feed_url)
    status, _headers, body = safe_fetch(feed_url, timeout=timeout)
    if status != 200:
        raise ValueError(f"RSS feed {feed_url!r} returned status {status}")
    return ET.fromstring(body)


def match_episode_item(feed_url: str, episode_title: str, timeout: int = 30):
    """
    Fetch an RSS feed and find the <item> whose title matches episode_title.

    Parameters:
        feed_url (str): Podcast RSS feed URL.
        episode_title (str): Episode title to match.
        timeout (int): Request timeout in seconds.

    Returns:
        Element | None: The matched <item> element, or None if the feed
        can't be fetched/parsed or no item matches closely enough.
    """
    try:
        root = _fetch_feed_root(feed_url, timeout=timeout)
    except Exception as e:
        log.warning("Failed to fetch/parse RSS feed %s: %s", feed_url, e)
        return None

    items_by_title = {}
    for item in root.findall(".//item"):
        title_el = item.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        if title:
            items_by_title[title] = item

    if episode_title in items_by_title:
        return items_by_title[episode_title]

    close = get_close_matches(episode_title, items_by_title.keys(), n=1, cutoff=0.8)
    if close:
        return items_by_title[close[0]]

    log.warning("No matching feed item for %r in %s", episode_title, feed_url)
    return None


def find_transcript_candidates(item, feed_url: str) -> list[dict]:
    """
    Extract Podcasting 2.0 <podcast:transcript> links from a matched item,
    best format first.

    Parameters:
        item: The matched RSS <item> element.
        feed_url (str): The feed's URL, used to resolve relative transcript URLs.

    Returns:
        list[dict]: Candidates with `url` and `type`, sorted by format preference.
    """
    candidates = []
    for el in item.findall(f"{PODCAST_NS}transcript"):
        raw_url = el.get("url")
        mime_type = (el.get("type") or "").strip().lower()
        if not raw_url:
            continue
        candidates.append({"url": urljoin(feed_url, raw_url), "type": mime_type})
    candidates.sort(key=lambda c: _TRANSCRIPT_TYPE_RANK.get(c["type"], 99))
    return candidates


def get_enclosure_url(item) -> str | None:
    """Extract the plain audio enclosure URL from a matched item, if any."""
    enclosure = item.find("enclosure")
    if enclosure is None:
        return None
    return enclosure.get("url") or None


# --------------------------------------------------------------------------
# Transcript normalization - every format below is converted into the same
# (file_extension, raw_body_text, plain_text) shape pipeline.py already
# expects from yt-dlp/Parakeet-sourced transcripts.
# --------------------------------------------------------------------------

_VTT_TIMING_RE = re.compile(
    r"^(\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->\s*(\d{2}:)?\d{2}:\d{2}[.,]\d{3}"
)


def _vtt_to_srt(vtt_text: str) -> str:
    """Convert WEBVTT text into SRT-formatted text (numbered cues, comma decimals)."""
    lines = vtt_text.splitlines()
    cues, current = [], []
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("WEBVTT") or stripped.startswith("NOTE"):
            continue
        if not stripped:
            if current:
                cues.append(current)
                current = []
            continue
        current.append(line)
    if current:
        cues.append(current)

    out = []
    index = 1
    for cue in cues:
        timing_idx = next(
            (i for i, ln in enumerate(cue) if _VTT_TIMING_RE.match(ln.strip())), None
        )
        if timing_idx is None:
            continue
        timing = cue[timing_idx].strip().replace(".", ",")
        # Drop VTT cue settings after the timing line (e.g. "align:start position:0%")
        timing = re.sub(r"(-->\s*[\d:,]+)\s.*$", r"\1", timing)
        text_lines = cue[timing_idx + 1 :]
        if not text_lines:
            continue
        out.append(str(index))
        out.append(timing)
        out.extend(text_lines)
        out.append("")
        index += 1
    return "\n".join(out)


def _ms_to_srt_timestamp(ms: float) -> str:
    total_ms = int(ms)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _podcast2_json_to_srt(payload: dict) -> str:
    """Convert a Podcasting 2.0 JSON transcript ({"segments": [...]}) to SRT."""
    segments = payload.get("segments") or []
    out = []
    for i, seg in enumerate(segments, start=1):
        body = (seg.get("body") or "").strip()
        if not body:
            continue
        # Podcasting 2.0's JSON transcript spec uses milliseconds; some
        # generators emit fractional seconds instead - treat values under
        # 1000 as certainly-seconds-not-ms would be unreliable, so we only
        # handle the documented millisecond form here.
        start_ms = seg.get("startTime", 0)
        end_ms = seg.get("endTime", start_ms)
        out.append(str(i))
        out.append(
            f"{_ms_to_srt_timestamp(start_ms)} --> {_ms_to_srt_timestamp(end_ms)}"
        )
        out.append(body)
        out.append("")
    return "\n".join(out)


def fetch_and_normalize_transcript(
    candidate: dict, timeout: int = 60
) -> tuple[str, str, str] | None:
    """
    Fetch a transcript candidate and normalize it into (extension, raw_body, plain_text).

    Parameters:
        candidate (dict): A candidate from find_transcript_candidates() (`url`, `type`).
        timeout (int): Request timeout in seconds.

    Returns:
        tuple[str, str, str] | None: (file extension without dot, raw stored
        body text, deduplicated plain text), or None if the format is
        unsupported or the fetch/parse fails.
    """
    url, mime_type = candidate["url"], candidate.get("type", "")
    try:
        validate_public_url(url)
        status, _headers, body = safe_fetch(url, timeout=timeout)
        if status != 200:
            log.warning("Transcript fetch %s returned status %d", url, status)
            return None
        text = body.decode("utf-8", errors="ignore")

        if mime_type in (
            "application/srt",
            "application/x-subrip",
            "text/srt",
        ) or url.lower().endswith(".srt"):
            return "srt", text, srt_text_to_plain_text(text)

        if mime_type == "text/vtt" or url.lower().endswith(".vtt"):
            srt_text = _vtt_to_srt(text)
            return "srt", srt_text, srt_text_to_plain_text(srt_text)

        if mime_type == "application/json" or url.lower().endswith(".json"):
            payload = json.loads(body)
            srt_text = _podcast2_json_to_srt(payload)
            if not srt_text:
                return None
            return "srt", srt_text, srt_text_to_plain_text(srt_text)

        if mime_type == "text/plain" or url.lower().endswith(".txt"):
            plain = " ".join(text.split())
            return "txt", text, plain

        log.warning("Unsupported transcript type %r at %s", mime_type, url)
        return None
    except Exception as e:
        log.warning("Failed to fetch/normalize transcript %s: %s", url, e)
        return None


def resolve_episode_from_rss(show_name: str, episode_title: str) -> dict | None:
    """
    End-to-end RSS resolution for an episode known only by show + title:
    discover the feed via iTunes, find the matching item, and prefer a
    published transcript over the plain audio enclosure.

    Returns:
        dict | None: {"feed_url", "transcript": (ext, raw_body, plain_text) | None,
        "enclosure_url": str | None}, or None if the feed/episode couldn't be
        found at all.
    """
    feed_url = discover_feed_via_itunes(show_name)
    if not feed_url:
        log.warning("No RSS feed found via iTunes for show %r", show_name)
        return None

    item = match_episode_item(feed_url, episode_title)
    if item is None:
        return None

    transcript = None
    for candidate in find_transcript_candidates(item, feed_url):
        transcript = fetch_and_normalize_transcript(candidate)
        if transcript is not None:
            break

    return {
        "feed_url": feed_url,
        "transcript": transcript,
        "enclosure_url": get_enclosure_url(item),
    }
