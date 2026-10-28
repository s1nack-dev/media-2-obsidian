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


def test_bridge_rejects_non_http_url(tmp_path):
    audio = tmp_path / "audio"
    audio.write_bytes(b"audio")
    with pytest.raises(ValueError, match="http or https"):
        bridge_client.transcribe_audio(audio, "model", "file:///tmp/bridge", "token")


def test_transcribe_audio_builds_authenticated_request(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    seen = {}

    def fake(request, timeout):
        seen.update(
            url=request.full_url,
            body=request.data,
            auth=request.get_header("Authorization"),
        )
        return Response({"srt_text": "s", "plain_text": "p"})

    monkeypatch.setattr(bridge_client, "urlopen", fake)
    assert bridge_client.transcribe_audio(audio, "model", "https://bridge/", "tok") == (
        "s",
        "p",
    )
    assert seen == {
        "url": "https://bridge/transcribe?model_id=model",
        "body": b"audio",
        "auth": "Bearer tok",
    }


def test_transcribe_audio_surfaces_bridge_error_detail(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")

    def fake(request, timeout):
        raise HTTPError(
            request.full_url,
            500,
            "Internal Server Error",
            {},
            Response({"error": "transcription failed: bad audio format"}),
        )

    monkeypatch.setattr(bridge_client, "urlopen", fake)
    with pytest.raises(bridge_client.BridgeRequestError, match="bad audio format"):
        bridge_client.transcribe_audio(audio, "model", "http://bridge", "token")


def test_transcribe_audio_surfaces_plain_text_error(monkeypatch, tmp_path):
    import io

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")

    def fake(request, timeout):
        raise HTTPError(
            request.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"upstream down")
        )

    monkeypatch.setattr(bridge_client, "urlopen", fake)
    with pytest.raises(bridge_client.BridgeRequestError, match="502|upstream down"):
        bridge_client.transcribe_audio(audio, "model", "http://bridge", "token")
