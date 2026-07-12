import json
from urllib.error import HTTPError

import pytest

import bridge_client


class Response:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.data

    def close(self):
        pass


def test_auth_headers():
    assert bridge_client._auth_headers("secret") == {"Authorization": "Bearer secret"}


def test_bridge_rejects_non_http_url():
    with pytest.raises(ValueError, match="http or https"):
        bridge_client.summarize("hello", "file:///tmp/bridge", "token")


def test_summarize_builds_request(monkeypatch):
    seen = {}

    def fake(req, timeout):
        seen.update(
            url=req.full_url,
            body=json.loads(req.data),
            auth=req.get_header("Authorization"),
        )
        return Response({"summary": "ok"})

    monkeypatch.setattr(bridge_client, "urlopen", fake)
    assert bridge_client.summarize("hello", "http://bridge/", "tok") == "ok"
    assert seen == {
        "url": "http://bridge/summarize",
        "body": {"transcript": "hello"},
        "auth": "Bearer tok",
    }


def test_transcribe_audio(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(
        bridge_client,
        "urlopen",
        lambda req, timeout: Response({"srt_text": "s", "plain_text": "p"}),
    )
    assert bridge_client.transcribe_audio(audio, "model", "http://b", "t") == ("s", "p")


def test_generate_tags(monkeypatch):
    monkeypatch.setattr(
        bridge_client,
        "urlopen",
        lambda req, timeout: Response({"tags": ["one", "two"]}),
    )
    assert bridge_client.generate_tags("text", "http://b/", "t") == ["one", "two"]


def test_transcribe_audio_surfaces_bridge_error_detail(monkeypatch, tmp_path):
    """host_bridge.py returns {"error": "..."} in its 500 body - urllib
    normally discards that behind a generic "HTTP Error 500" and only
    exposes it via HTTPError.read(); this must be surfaced instead."""
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")

    def fake(req, timeout):
        raise HTTPError(
            req.full_url,
            500,
            "Internal Server Error",
            {},
            Response({"error": "transcription failed: bad audio format"}),
        )

    monkeypatch.setattr(bridge_client, "urlopen", fake)
    with pytest.raises(bridge_client.BridgeRequestError, match="bad audio format"):
        bridge_client.transcribe_audio(audio, "model", "http://b", "t")


class _RawResponse:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.data

    def close(self):
        pass


def test_summarize_surfaces_non_json_error_body(monkeypatch):
    def fake(req, timeout):
        raise HTTPError(
            req.full_url, 502, "Bad Gateway", {}, _RawResponse(b"plain text failure")
        )

    monkeypatch.setattr(bridge_client, "urlopen", fake)
    with pytest.raises(bridge_client.BridgeRequestError, match="502"):
        bridge_client.summarize("text", "http://b/", "t")
