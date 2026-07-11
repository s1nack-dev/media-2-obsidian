from pathlib import Path
import pytest
import pipeline


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://example.com/a", "generic_link"),
    ],
)
def test_detect_input_type_urls(value, expected):
    assert pipeline.detect_input_type(value) == expected


def test_detect_input_type_local_and_invalid(tmp_path):
    assert pipeline.detect_input_type(str(tmp_path)) == "local_file"
    with pytest.raises(ValueError):
        pipeline.detect_input_type("not a url")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://youtu.be/abc", "abc"),
        ("https://youtube.com/shorts/xyz", "xyz"),
        ("https://youtube.com/watch?v=q", "q"),
    ],
)
def test_extract_youtube_video_id(url, expected):
    assert pipeline.extract_youtube_video_id(url) == expected


def test_find_sidecar_subtitle(tmp_path):
    media = tmp_path / "episode.mp3"
    media.touch()
    assert pipeline.find_sidecar_subtitle(media) is None
    sidecar = tmp_path / "episode.srt"
    sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    assert pipeline.find_sidecar_subtitle(media) == sidecar


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://x.test/a.mp3", True),
        ("https://x.test/a", False),
        ("https://x.test/a.MP4", True),
    ],
)
def test_direct_media_detection(url, expected):
    assert pipeline.is_direct_media_url(url) is expected


def test_process_local_sidecar_end_to_end(tmp_path, monkeypatch):
    media = tmp_path / "Talk.mp3"
    media.write_bytes(b"audio")
    (tmp_path / "Talk.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello world\n"
    )
    subs, vault = tmp_path / "subs", tmp_path / "vault"
    cfg = {
        "bridge": {"url": "http://bridge", "auth_token_op_ref": "ref"},
        "transcription": {},
        "youtube": {"subtitle_languages": ["en"]},
        "github": {
            "subtitles_repo_url": "https://github.com/me/subs.git",
            "subtitles_repo_path": str(subs),
            "subtitles_branch": "main",
            "subtitles_dir_in_repo": "transcripts",
            "vault_repo_url": "https://github.com/me/vault.git",
            "vault_repo_path": str(vault),
            "vault_branch": "main",
            "vault_notes_dir": "notes",
            "commit_author_name": "Test",
            "commit_author_email": "test@example.com",
        },
    }
    monkeypatch.setattr(
        pipeline,
        "ensure_repo",
        lambda *args: Path(args[1]).mkdir(parents=True, exist_ok=True) or Path(args[1]),
    )
    monkeypatch.setattr(pipeline, "commit_and_push", lambda *args: None)
    monkeypatch.setattr(pipeline.bridge_client, "summarize", lambda *args: "summary")
    monkeypatch.setattr(
        pipeline.bridge_client, "generate_tags", lambda *args: ["testing"]
    )
    result = pipeline.process_input(
        str(media), cfg, github_token="token", bridge_token="bridge"
    )
    assert result["title"] == "Talk"
    assert result["note_path"].read_text().find("summary") >= 0


def test_ytdlp_subtitle_download_prefers_requested_language(tmp_path, monkeypatch):
    class R:
        returncode = 0
        stderr = ""

    def run(cmd, **kwargs):
        (tmp_path / "vid.en.srt").write_text("subtitle")
        return R()

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    assert pipeline._ytdlp_download_subs(
        "https://example", "vid", ["en", "de"], tmp_path
    ) == (tmp_path / "vid.en.srt", "en")


def test_ytdlp_subtitle_download_falls_back_to_any_language(tmp_path, monkeypatch):
    class R:
        returncode = 1
        stderr = "failed"

    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: R())
    (tmp_path / "vid.fr.srt").write_text("subtitle")
    assert (
        pipeline._ytdlp_download_subs("https://example", "vid", ["en"], tmp_path)[1]
        == "fr"
    )


def test_ytdlp_subtitle_timeout_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            pipeline.subprocess.TimeoutExpired("yt-dlp", 1)
        ),
    )
    assert (
        pipeline._ytdlp_download_subs("https://example", "vid", ["en"], tmp_path)
        is None
    )


def test_audio_download_and_title(monkeypatch, tmp_path):
    class R:
        returncode = 0
        stdout = "A title\nextra"
        stderr = ""

    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: R())
    assert pipeline.fetch_title_via_ytdlp("https://example") == "A title"
    (tmp_path / "audio.mp3").write_bytes(b"x")
    assert (
        pipeline.download_audio_via_ytdlp("https://example", tmp_path).name
        == "audio.mp3"
    )


def test_download_direct_file_validates_and_writes(monkeypatch, tmp_path):
    class Response:
        status = 200
        reason = "OK"
        done = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            if self.done:
                return b""
            self.done = True
            return b"payload"

        def getheader(self, name):
            return None

    monkeypatch.setattr(
        pipeline, "_open_pinned", lambda url, timeout: (Response(), "example")
    )
    path = pipeline.download_direct_file("https://example/a.mp3", tmp_path)
    assert path.read_bytes() == b"payload"


def test_find_sidecar_supports_common_extensions(tmp_path):
    media = tmp_path / "episode.m4a"
    media.touch()
    for suffix in (".vtt", ".txt"):
        sidecar = media.with_suffix(suffix)
        sidecar.write_text("caption")
        assert pipeline.find_sidecar_subtitle(media) is None
        sidecar.unlink()


def test_resolve_overcast_episode(monkeypatch):
    page = b'<a href="https://feed.example/rss"><img src="/img/badge-rss.svg"></a><h2 class="title">Episode One</h2>'
    rss = b'<rss><channel><item><title>Episode One</title><enclosure url="https://cdn.example/e.mp3" /></item></channel></rss>'
    responses = iter((page, rss))
    monkeypatch.setattr(pipeline, "validate_public_url", lambda url: None)
    monkeypatch.setattr(
        pipeline, "_safe_urlopen_with_validation", lambda url, timeout: next(responses)
    )
    assert pipeline.resolve_overcast_episode("https://overcast.fm/+abc") == (
        "Episode One",
        "https://cdn.example/e.mp3",
    )


def test_download_direct_file_revalidates_each_redirect(monkeypatch, tmp_path):
    class Response:
        reason = "OK"

        def __init__(self, status, location=None, body=b""):
            self.status = status
            self.location = location
            self.body = body
            self.done = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def getheader(self, name):
            return self.location if name == "Location" else None

        def read(self, *args):
            if self.done:
                return b""
            self.done = True
            return self.body

    opened = []

    def open_pinned(url, timeout):
        opened.append(url)
        if len(opened) == 1:
            return Response(302, "/final.mp3"), "first.example"
        return Response(200, body=b"payload"), "first.example"

    monkeypatch.setattr(pipeline, "_open_pinned", open_pinned)
    path = pipeline.download_direct_file("https://first.example/audio.mp3", tmp_path)

    assert opened == [
        "https://first.example/audio.mp3",
        "https://first.example/final.mp3",
    ]
    assert path.read_bytes() == b"payload"


def test_download_rejects_declared_oversize_without_creating_file(
    monkeypatch, tmp_path
):
    class Response:
        status = 200
        reason = "OK"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def getheader(self, name):
            return (
                str(pipeline.MAX_MEDIA_BYTES + 1) if name == "Content-Length" else None
            )

    monkeypatch.setattr(
        pipeline, "_open_pinned", lambda url, timeout: (Response(), "example")
    )
    dest = tmp_path / "large.mp3"

    with pytest.raises(ValueError, match="exceeds"):
        pipeline._download_with_redirect_validation("https://example/large.mp3", dest)

    assert not dest.exists()


def test_download_removes_partial_file_on_streamed_overflow(monkeypatch, tmp_path):
    class Response:
        status = 200
        reason = "OK"

        def __init__(self):
            self.chunks = iter((b"1234", b"5678"))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def getheader(self, name):
            return None

        def read(self, size):
            return next(self.chunks, b"")

    monkeypatch.setattr(pipeline, "MAX_MEDIA_BYTES", 6)
    monkeypatch.setattr(
        pipeline, "_open_pinned", lambda url, timeout: (Response(), "example")
    )
    dest = tmp_path / "large.mp3"

    with pytest.raises(ValueError, match="exceeds"):
        pipeline._download_with_redirect_validation("https://example/large.mp3", dest)

    assert not dest.exists()


def test_safe_urlopen_revalidates_each_redirect(monkeypatch):
    class Response:
        reason = "OK"

        def __init__(self, status, location=None, body=b""):
            self.status = status
            self.location = location
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def getheader(self, name):
            return self.location if name == "Location" else None

        def read(self):
            return self.body

    opened = []

    def open_pinned(url, timeout):
        opened.append(url)
        if len(opened) == 1:
            return Response(302, "https://feed.example/rss"), "overcast.fm"
        return Response(200, body=b"feed"), "feed.example"

    monkeypatch.setattr(pipeline, "_open_pinned", open_pinned)
    assert pipeline._safe_urlopen_with_validation("https://overcast.fm/+abc") == b"feed"
    assert opened == ["https://overcast.fm/+abc", "https://feed.example/rss"]


@pytest.mark.parametrize(
    "url", ["https://example.com/.", "https://example.com/..", "https://example.com/"]
)
def test_download_direct_file_uses_safe_default_filename(monkeypatch, tmp_path, url):
    monkeypatch.setattr(
        pipeline,
        "_download_with_redirect_validation",
        lambda source, dest: dest.write_bytes(b"x"),
    )
    path = pipeline.download_direct_file(url, tmp_path)
    assert path == tmp_path / "download"
    assert path.read_bytes() == b"x"


def test_process_input_raises_when_no_audio_or_subtitles(tmp_path, monkeypatch):
    media = tmp_path / "empty.mp3"
    media.write_bytes(b"x")
    cfg = {
        "bridge": {"url": "http://bridge", "auth_token_op_ref": "ref"},
        "youtube": {"subtitle_languages": ["en"]},
    }
    monkeypatch.setattr(
        pipeline.bridge_client,
        "transcribe_audio",
        lambda *args: (_ for _ in ()).throw(RuntimeError("down")),
    )
    with pytest.raises(RuntimeError):
        pipeline.process_input(str(media), cfg, github_token="t", bridge_token="b")
