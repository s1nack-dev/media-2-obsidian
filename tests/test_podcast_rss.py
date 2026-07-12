import json

import podcast_rss


def test_discover_feed_via_itunes_success(monkeypatch):
    payload = json.dumps(
        {"results": [{"feedUrl": "https://feed.example/rss"}]}
    ).encode()
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=15: (200, {}, payload)
    )
    assert podcast_rss.discover_feed_via_itunes("My Show") == "https://feed.example/rss"


def test_discover_feed_via_itunes_no_results(monkeypatch):
    payload = json.dumps({"results": []}).encode()
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=15: (200, {}, payload)
    )
    assert podcast_rss.discover_feed_via_itunes("Unknown Show") is None


def test_discover_feed_via_itunes_returns_none_on_error(monkeypatch):
    def boom(url, timeout=15):
        raise ValueError("network down")

    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(podcast_rss, "safe_fetch", boom)
    assert podcast_rss.discover_feed_via_itunes("My Show") is None


def test_discover_feed_via_itunes_empty_show_name_returns_none():
    assert podcast_rss.discover_feed_via_itunes("") is None


_FEED_XML = b"""<rss xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel>
  <item>
    <title>Episode One</title>
    <enclosure url="https://cdn.example/e1.mp3" />
    <podcast:transcript url="https://cdn.example/e1.srt" type="application/srt" />
  </item>
  <item>
    <title>Episode Two</title>
    <enclosure url="https://cdn.example/e2.mp3" />
  </item>
</channel>
</rss>"""


def test_match_episode_item_exact_match(monkeypatch):
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=30: (200, {}, _FEED_XML)
    )
    item = podcast_rss.match_episode_item("https://feed.example/rss", "Episode One")
    assert item is not None
    assert item.find("title").text == "Episode One"


def test_match_episode_item_close_match(monkeypatch):
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=30: (200, {}, _FEED_XML)
    )
    item = podcast_rss.match_episode_item("https://feed.example/rss", "Episode One!")
    assert item is not None
    assert item.find("title").text == "Episode One"


def test_match_episode_item_no_match(monkeypatch):
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=30: (200, {}, _FEED_XML)
    )
    assert (
        podcast_rss.match_episode_item("https://feed.example/rss", "Totally Different")
        is None
    )


def test_match_episode_item_malformed_feed_returns_none(monkeypatch):
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=30: (200, {}, b"<not-xml")
    )
    assert podcast_rss.match_episode_item("https://feed.example/rss", "x") is None


def test_match_episode_item_non_200_status_returns_none(monkeypatch):
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=30: (404, {}, b"")
    )
    assert podcast_rss.match_episode_item("https://feed.example/rss", "x") is None


def test_find_transcript_candidates_sorts_srt_before_json():
    root = podcast_rss.ET.fromstring(_FEED_XML)
    item = root.findall(".//item")[0]
    candidates = podcast_rss.find_transcript_candidates(
        item, "https://feed.example/rss"
    )
    assert candidates == [
        {"url": "https://cdn.example/e1.srt", "type": "application/srt"}
    ]


def test_find_transcript_candidates_resolves_relative_urls():
    xml = b"""<rss xmlns:podcast="https://podcastindex.org/namespace/1.0">
    <channel><item><title>E</title>
    <podcast:transcript url="/transcripts/e1.vtt" type="text/vtt" />
    </item></channel></rss>"""
    root = podcast_rss.ET.fromstring(xml)
    item = root.findall(".//item")[0]
    candidates = podcast_rss.find_transcript_candidates(
        item, "https://feed.example/rss/x"
    )
    assert candidates == [
        {"url": "https://feed.example/transcripts/e1.vtt", "type": "text/vtt"}
    ]


def test_find_transcript_candidates_empty_when_no_podcast_ns():
    root = podcast_rss.ET.fromstring(_FEED_XML)
    item = root.findall(".//item")[1]
    assert (
        podcast_rss.find_transcript_candidates(item, "https://feed.example/rss") == []
    )


def test_get_enclosure_url():
    root = podcast_rss.ET.fromstring(_FEED_XML)
    item = root.findall(".//item")[1]
    assert podcast_rss.get_enclosure_url(item) == "https://cdn.example/e2.mp3"


def test_get_enclosure_url_missing():
    xml = b"<rss><channel><item><title>E</title></item></channel></rss>"
    root = podcast_rss.ET.fromstring(xml)
    item = root.findall(".//item")[0]
    assert podcast_rss.get_enclosure_url(item) is None


def test_fetch_and_normalize_transcript_srt(monkeypatch):
    srt = "1\n00:00:00,000 --> 00:00:01,000\nHello world\n"
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=60: (200, {}, srt.encode())
    )
    ext, raw, plain = podcast_rss.fetch_and_normalize_transcript(
        {"url": "https://cdn.example/e.srt", "type": "application/srt"}
    )
    assert ext == "srt"
    assert raw == srt
    assert plain == "Hello world"


def test_fetch_and_normalize_transcript_vtt(monkeypatch):
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello VTT\n"
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=60: (200, {}, vtt.encode())
    )
    ext, raw, plain = podcast_rss.fetch_and_normalize_transcript(
        {"url": "https://cdn.example/e.vtt", "type": "text/vtt"}
    )
    assert ext == "srt"
    assert "00:00:00,000 --> 00:00:01,000" in raw
    assert plain == "Hello VTT"


def test_fetch_and_normalize_transcript_json(monkeypatch):
    payload = json.dumps(
        {
            "segments": [
                {"startTime": 0, "endTime": 1000, "body": "Hello"},
                {"startTime": 1000, "endTime": 2000, "body": "JSON"},
            ]
        }
    ).encode()
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=60: (200, {}, payload)
    )
    ext, raw, plain = podcast_rss.fetch_and_normalize_transcript(
        {"url": "https://cdn.example/e.json", "type": "application/json"}
    )
    assert ext == "srt"
    assert "Hello" in raw and "JSON" in raw
    assert plain == "Hello JSON"


def test_fetch_and_normalize_transcript_txt(monkeypatch):
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss,
        "safe_fetch",
        lambda url, timeout=60: (200, {}, b"Plain   text\ntranscript"),
    )
    ext, raw, plain = podcast_rss.fetch_and_normalize_transcript(
        {"url": "https://cdn.example/e.txt", "type": "text/plain"}
    )
    assert ext == "txt"
    assert raw == "Plain   text\ntranscript"
    assert plain == "Plain text transcript"


def test_fetch_and_normalize_transcript_unsupported_type_returns_none(monkeypatch):
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=60: (200, {}, b"binary")
    )
    assert (
        podcast_rss.fetch_and_normalize_transcript(
            {"url": "https://cdn.example/e.bin", "type": "application/octet-stream"}
        )
        is None
    )


def test_fetch_and_normalize_transcript_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=60: (403, {}, b"")
    )
    assert (
        podcast_rss.fetch_and_normalize_transcript(
            {"url": "https://cdn.example/e.srt", "type": "application/srt"}
        )
        is None
    )


def test_fetch_and_normalize_transcript_rejects_private_url(monkeypatch):
    def deny(url):
        raise ValueError("private IP")

    monkeypatch.setattr(podcast_rss, "validate_public_url", deny)
    assert (
        podcast_rss.fetch_and_normalize_transcript(
            {"url": "http://169.254.169.254/e.srt", "type": "application/srt"}
        )
        is None
    )


def test_resolve_episode_from_rss_transcript_found(monkeypatch):
    monkeypatch.setattr(
        podcast_rss, "discover_feed_via_itunes", lambda name: "https://feed.example/rss"
    )
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=30: (200, {}, _FEED_XML)
    )
    monkeypatch.setattr(
        podcast_rss,
        "fetch_and_normalize_transcript",
        lambda candidate, timeout=60: ("srt", "1\n...\nHi\n", "Hi"),
    )
    result = podcast_rss.resolve_episode_from_rss("My Show", "Episode One")
    assert result["feed_url"] == "https://feed.example/rss"
    assert result["transcript"] == ("srt", "1\n...\nHi\n", "Hi")
    assert result["enclosure_url"] == "https://cdn.example/e1.mp3"


def test_resolve_episode_from_rss_no_transcript_falls_back_to_enclosure(monkeypatch):
    monkeypatch.setattr(
        podcast_rss, "discover_feed_via_itunes", lambda name: "https://feed.example/rss"
    )
    monkeypatch.setattr(podcast_rss, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        podcast_rss, "safe_fetch", lambda url, timeout=30: (200, {}, _FEED_XML)
    )
    result = podcast_rss.resolve_episode_from_rss("My Show", "Episode Two")
    assert result["transcript"] is None
    assert result["enclosure_url"] == "https://cdn.example/e2.mp3"


def test_resolve_episode_from_rss_no_feed_found(monkeypatch):
    monkeypatch.setattr(podcast_rss, "discover_feed_via_itunes", lambda name: None)
    assert podcast_rss.resolve_episode_from_rss("Unknown Show", "Episode One") is None
