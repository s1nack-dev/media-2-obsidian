import json

import pytest

import spotify_client


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://open.spotify.com/episode/abc123", "abc123"),
        ("https://open.spotify.com/intl-de/episode/abc123", "abc123"),
        ("https://open.spotify.com/episode/abc123?si=xyz", "abc123"),
        ("https://open.spotify.com/show/abc123", None),
        ("https://example.com/episode/abc123", None),
    ],
)
def test_extract_episode_id(url, expected):
    assert spotify_client.extract_episode_id(url) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://open.spotify.com/episode/abc123", True),
        ("https://open.spotify.com/show/abc123", False),
        ("https://open.spotify.com/track/abc123", False),
        ("https://youtube.com/watch?v=x", False),
    ],
)
def test_is_spotify_episode_url(url, expected):
    assert spotify_client.is_spotify_episode_url(url) is expected


def test_is_spotify_show_url():
    assert spotify_client.is_spotify_show_url("https://open.spotify.com/show/abc123")
    assert not spotify_client.is_spotify_show_url(
        "https://open.spotify.com/episode/abc123"
    )


def test_get_access_token_returns_none_when_unconfigured():
    assert spotify_client.get_access_token({"spotify": {}}) is None
    assert spotify_client.get_access_token({}) is None


def test_get_access_token_success(monkeypatch):
    cfg = {
        "spotify": {
            "client_id_op_ref": "op://x/id",
            "client_secret_op_ref": "op://x/secret",  # pragma: allowlist secret
        }
    }
    monkeypatch.setattr(
        spotify_client,
        "resolve_secret",
        lambda env, ref: "cid" if "id" in ref else "sec",
    )
    monkeypatch.setattr(
        spotify_client,
        "safe_fetch",
        lambda url, method="GET", headers=None, body=None, timeout=15: (
            200,
            {},
            json.dumps({"access_token": "tok123"}).encode(),
        ),
    )
    assert spotify_client.get_access_token(cfg) == "tok123"


def test_get_access_token_failure_status_returns_none(monkeypatch):
    cfg = {
        "spotify": {
            "client_id_op_ref": "op://x/id",
            "client_secret_op_ref": "op://x/secret",  # pragma: allowlist secret
        }
    }
    monkeypatch.setattr(spotify_client, "resolve_secret", lambda env, ref: "x")
    monkeypatch.setattr(
        spotify_client,
        "safe_fetch",
        lambda url, method="GET", headers=None, body=None, timeout=15: (401, {}, b""),
    )
    assert spotify_client.get_access_token(cfg) is None


def test_fetch_episode_metadata_success(monkeypatch):
    payload = json.dumps(
        {
            "name": "Episode Title",
            "show": {"name": "Show Name"},
            "description": "desc",
            "release_date": "2026-07-10",
        }
    ).encode()
    monkeypatch.setattr(
        spotify_client,
        "safe_fetch",
        lambda url, headers=None, timeout=15: (200, {}, payload),
    )
    metadata = spotify_client.fetch_episode_metadata("abc123", "tok")
    assert metadata == {
        "title": "Episode Title",
        "show_name": "Show Name",
        "description": "desc",
        "release_date": "2026-07-10",
    }


@pytest.mark.parametrize("status", [401, 403, 404])
def test_fetch_episode_metadata_private_or_missing_returns_none(monkeypatch, status):
    monkeypatch.setattr(
        spotify_client,
        "safe_fetch",
        lambda url, headers=None, timeout=15: (status, {}, b""),
    )
    assert spotify_client.fetch_episode_metadata("abc123", "tok") is None


def test_scrape_episode_page_metadata(monkeypatch):
    page = (
        b"<html><head>"
        b'<meta property="og:title" content="Episode Title">'
        b'<meta property="og:description" content="Episode description">'
        b"<title>Episode Title - Show Name | Podcast on Spotify</title>"
        b"</head></html>"
    )
    monkeypatch.setattr(spotify_client, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        spotify_client, "safe_fetch", lambda url, timeout=15: (200, {}, page)
    )
    metadata = spotify_client.scrape_episode_page_metadata(
        "https://open.spotify.com/episode/abc123"
    )
    assert metadata == {
        "title": "Episode Title",
        "show_name": "Show Name",
        "description": "Episode description",
    }


def test_scrape_episode_page_metadata_no_title_returns_none(monkeypatch):
    monkeypatch.setattr(spotify_client, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        spotify_client,
        "safe_fetch",
        lambda url, timeout=15: (200, {}, b"<html></html>"),
    )
    assert (
        spotify_client.scrape_episode_page_metadata(
            "https://open.spotify.com/episode/abc123"
        )
        is None
    )


def test_scrape_episode_page_metadata_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(spotify_client, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        spotify_client, "safe_fetch", lambda url, timeout=15: (404, {}, b"")
    )
    assert (
        spotify_client.scrape_episode_page_metadata(
            "https://open.spotify.com/episode/abc123"
        )
        is None
    )


def test_resolve_episode_metadata_prefers_api(monkeypatch):
    monkeypatch.setattr(spotify_client, "get_access_token", lambda cfg: "tok")
    monkeypatch.setattr(
        spotify_client,
        "fetch_episode_metadata",
        lambda episode_id, token: {"title": "API Title", "show_name": "API Show"},
    )
    called = []
    monkeypatch.setattr(
        spotify_client,
        "scrape_episode_page_metadata",
        lambda url: called.append(url) or {"title": "Scraped"},
    )
    result = spotify_client.resolve_episode_metadata(
        "https://open.spotify.com/episode/abc123", {}
    )
    assert result == {"title": "API Title", "show_name": "API Show"}
    assert called == []


def test_resolve_episode_metadata_falls_back_to_scrape_when_api_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(spotify_client, "get_access_token", lambda cfg: "tok")
    monkeypatch.setattr(
        spotify_client, "fetch_episode_metadata", lambda episode_id, token: None
    )
    monkeypatch.setattr(
        spotify_client,
        "scrape_episode_page_metadata",
        lambda url: {"title": "Scraped Title", "show_name": "Scraped Show"},
    )
    result = spotify_client.resolve_episode_metadata(
        "https://open.spotify.com/episode/abc123", {}
    )
    assert result == {"title": "Scraped Title", "show_name": "Scraped Show"}


def test_resolve_episode_metadata_no_credentials_uses_scrape(monkeypatch):
    monkeypatch.setattr(spotify_client, "get_access_token", lambda cfg: None)
    monkeypatch.setattr(
        spotify_client,
        "scrape_episode_page_metadata",
        lambda url: {"title": "Scraped Title", "show_name": None},
    )
    result = spotify_client.resolve_episode_metadata(
        "https://open.spotify.com/episode/abc123", {}
    )
    assert result == {"title": "Scraped Title", "show_name": None}


def test_resolve_episode_metadata_non_episode_url_returns_none():
    assert (
        spotify_client.resolve_episode_metadata(
            "https://open.spotify.com/show/abc123", {}
        )
        is None
    )
