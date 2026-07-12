#!/usr/bin/env python3
"""
Spotify podcast episode adapter - metadata only, never audio.

Spotify's Web API (https://developer.spotify.com/documentation/web-api)
explicitly prohibits downloading Spotify-streamed content and doesn't expose
episode transcripts or a show's underlying RSS feed URL through the
documented episode endpoint. So this module is deliberately narrow: it only
resolves an episode's title and show name, either through the official Web
API (if `spotify.client_id_op_ref`/`client_secret_op_ref` are configured) or
by reading the public episode page's metadata tags when no credentials are
configured. Once pipeline.py has (title, show_name), it hands off to
podcast_rss.py to find the real RSS feed/transcript/audio via iTunes Search +
Podcasting 2.0 - this module never touches Spotify's private web-player
transcript endpoints and never fetches Spotify-hosted audio streams.
"""

import base64
import html as html_module
import json
import logging
import re
from urllib.parse import urlparse

from core import resolve_secret, safe_fetch, validate_public_url

log = logging.getLogger("pipeline")

_EPISODE_RE = re.compile(
    r"^/(?:intl-[a-z]{2}(?:-[a-zA-Z]+)?/)?episode/([a-zA-Z0-9]+)/?$"
)
_SHOW_RE = re.compile(r"^/(?:intl-[a-z]{2}(?:-[a-zA-Z]+)?/)?show/([a-zA-Z0-9]+)/?$")

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_OG_DESCRIPTION_RE = re.compile(r'<meta property="og:description" content="([^"]*)"')
_TITLE_TAG_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)


def is_spotify_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "open.spotify.com" or host.endswith(".open.spotify.com")


def extract_episode_id(url: str) -> str | None:
    """Return the episode id if url is a Spotify episode page, else None."""
    if not is_spotify_host(url):
        return None
    match = _EPISODE_RE.match(urlparse(url).path)
    return match.group(1) if match else None


def is_spotify_show_url(url: str) -> bool:
    """True if url is a Spotify show (not episode) page."""
    if not is_spotify_host(url):
        return False
    return bool(_SHOW_RE.match(urlparse(url).path))


def is_spotify_episode_url(url: str) -> bool:
    return extract_episode_id(url) is not None


# --------------------------------------------------------------------------
# Official Web API (client-credentials flow) - metadata enrichment only,
# entirely optional. cfg["spotify"] may be absent or have blank op_refs, in
# which case get_access_token() returns None and callers fall back to
# scrape_episode_page_metadata().
# --------------------------------------------------------------------------


def get_access_token(cfg: dict, timeout: int = 15) -> str | None:
    """
    Obtain a Spotify Web API access token via the client-credentials flow.

    Parameters:
        cfg (dict): Pipeline config; reads the optional `spotify` section.
        timeout (int): Request timeout in seconds.

    Returns:
        str | None: A bearer token, or None if Spotify credentials aren't
        configured or the token request fails (never raises - Spotify
        support degrades to page-scrape metadata, not a hard failure).
    """
    spotify_cfg = cfg.get("spotify") or {}
    client_id_ref = spotify_cfg.get("client_id_op_ref")
    client_secret_ref = spotify_cfg.get("client_secret_op_ref")
    if not client_id_ref or not client_secret_ref:
        return None

    try:
        client_id = resolve_secret("SPOTIFY_CLIENT_ID", client_id_ref)
        client_secret = resolve_secret("SPOTIFY_CLIENT_SECRET", client_secret_ref)
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        status, _headers, body = safe_fetch(
            TOKEN_URL,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=b"grant_type=client_credentials",
            timeout=timeout,
        )
        if status != 200:
            log.warning("Spotify token request failed with status %d", status)
            return None
        return json.loads(body).get("access_token")
    except Exception:
        log.warning("Spotify token request failed")
        return None


def fetch_episode_metadata(
    episode_id: str, token: str, timeout: int = 15
) -> dict | None:
    """
    Fetch an episode's title/show name from the official Spotify Web API.

    Parameters:
        episode_id (str): Spotify episode id.
        token (str): Bearer access token from get_access_token().
        timeout (int): Request timeout in seconds.

    Returns:
        dict | None: {"title", "show_name", "description", "release_date"},
        or None if the episode is private/unavailable or the request fails.
        release_date is Spotify's own value (format varies with the show's
        release_date_precision - day/month/year) - RSS's <pubDate> is
        preferred where available since it's consistently a full date, this
        is only a fallback when RSS resolution didn't happen or had none.
    """
    url = f"{API_BASE}/episodes/{episode_id}"
    try:
        status, _headers, body = safe_fetch(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )
        if status == 200:
            payload = json.loads(body)
            return {
                "title": payload.get("name"),
                "show_name": (payload.get("show") or {}).get("name"),
                "description": payload.get("description"),
                "release_date": payload.get("release_date"),
            }
        if status in (401, 403, 404):
            log.warning(
                "Spotify episode %s unavailable via API (status %d) - private, "
                "region-restricted, or removed",
                episode_id,
                status,
            )
        else:
            log.warning("Spotify episode API returned status %d", status)
        return None
    except Exception as e:
        log.warning("Spotify episode metadata request failed: %s", e)
        return None


# --------------------------------------------------------------------------
# Public-page scrape fallback - used when no Spotify API credentials are
# configured. Only reads the page's own <meta>/<title> tags (the same class
# of static HTML scraping pipeline.py already does for Overcast), never the
# private web-player APIs the page's JS calls after load.
# --------------------------------------------------------------------------


def scrape_episode_page_metadata(url: str, timeout: int = 15) -> dict | None:
    """
    Best-effort episode/show name extraction from a public Spotify episode
    page's static <meta>/<title> tags (no API credentials required).

    Returns:
        dict | None: {"title", "show_name", "description"}, or None if the
        page can't be fetched or has no recognizable title.
    """
    try:
        validate_public_url(url)
        status, _headers, body = safe_fetch(url, timeout=timeout)
        if status != 200:
            log.warning("Spotify episode page %s returned status %d", url, status)
            return None
        page_html = body.decode("utf-8", errors="ignore")

        og_title = _OG_TITLE_RE.search(page_html)
        title = html_module.unescape(og_title.group(1)).strip() if og_title else None

        og_desc = _OG_DESCRIPTION_RE.search(page_html)
        description = (
            html_module.unescape(og_desc.group(1)).strip() if og_desc else None
        )

        # Spotify's <title> tag is typically "Episode Name - Show Name | Podcast
        # on Spotify" - og:site_name is always "Spotify", so this is the only
        # place the show name appears in static HTML. Best-effort only: if the
        # episode title itself contains " - ", this can misparse the show name.
        show_name = None
        title_tag = _TITLE_TAG_RE.search(page_html)
        if title_tag:
            raw = html_module.unescape(title_tag.group(1)).strip()
            raw = re.sub(
                r"\s*\|\s*Podcast on Spotify\s*$", "", raw, flags=re.IGNORECASE
            )
            if " - " in raw:
                parsed_title, parsed_show = raw.rsplit(" - ", 1)
                show_name = parsed_show.strip()
                if not title:
                    title = parsed_title.strip()

        if not title:
            log.warning("Spotify episode page %s has no recognizable title", url)
            return None

        return {"title": title, "show_name": show_name, "description": description}
    except Exception as e:
        log.warning("Spotify episode page scrape failed for %s: %s", url, e)
        return None


def resolve_episode_metadata(url: str, cfg: dict) -> dict | None:
    """
    Resolve a Spotify episode URL's title/show name: prefers the official
    Web API when `spotify.*` credentials are configured, falls back to
    scraping the public episode page otherwise.

    Returns:
        dict | None: {"title", "show_name", "description", "release_date"}
        (release_date is only present when the Web API path succeeds),
        or None if the episode is unsupported/private and no metadata
        could be found by either method.
    """
    episode_id = extract_episode_id(url)
    if episode_id is None:
        return None

    token = get_access_token(cfg)
    if token is not None:
        metadata = fetch_episode_metadata(episode_id, token)
        if metadata is not None:
            return metadata
        log.info(
            "Spotify API metadata unavailable for %s, falling back to page scrape", url
        )

    return scrape_episode_page_metadata(url)
