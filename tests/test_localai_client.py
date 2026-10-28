import json

import localai_client


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size=-1):
        return self.body


def test_transcribe_audio_uses_openai_compatible_multipart_endpoint(
    monkeypatch, tmp_path
):
    audio = tmp_path / "talk.mp3"
    audio.write_bytes(b"audio-bytes")
    seen = {}

    def fake(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["content_type"] = request.get_header("Content-type")
        seen["body"] = request.data
        return Response(b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")

    monkeypatch.setattr(localai_client._OPENER, "open", fake)
    assert localai_client.transcribe_audio(
        audio, "parakeet-110m", "https://localai", "secret"
    ) == (
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        "Hello",
    )
    assert seen["url"] == "https://localai/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer secret"
    assert "multipart/form-data" in seen["content_type"]
    assert b'name="model"' in seen["body"]
    assert b"parakeet-110m" in seen["body"]
    assert b'name="response_format"' in seen["body"]
    assert b'name="response_format"\r\n\r\nsrt\r\n' in seen["body"]


def test_chat_completion_uses_openai_compatible_endpoint(monkeypatch):
    seen = {}

    def fake(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        return Response(
            json.dumps({"choices": [{"message": {"content": " Done "}}]}).encode()
        )

    monkeypatch.setattr(localai_client._OPENER, "open", fake)
    assert (
        localai_client.chat_completion("Hello", "chat-model", "https://localai")
        == "Done"
    )
    assert seen == {
        "url": "https://localai/v1/chat/completions",
        "body": {
            "model": "chat-model",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    }


def test_transport_error_becomes_localai_request_error(monkeypatch):
    import pytest
    from urllib.error import URLError

    def fail(request, timeout):
        raise URLError("connection refused")

    monkeypatch.setattr(localai_client._OPENER, "open", fail)
    with pytest.raises(localai_client.LocalAIRequestError):
        localai_client.chat_completion("p", "m", "http://127.0.0.1:8082")


def test_chat_response_without_choices_raises(monkeypatch):
    import pytest

    monkeypatch.setattr(
        localai_client._OPENER, "open", lambda request, timeout: Response(b"{}")
    )
    with pytest.raises(localai_client.LocalAIRequestError):
        localai_client.chat_completion("p", "m", "http://127.0.0.1:8082")


def test_redirects_are_refused():
    assert (
        localai_client._NoRedirect().redirect_request(
            None, None, 302, "", {}, "http://x"
        )
        is None
    )


def test_plain_http_only_allowed_for_loopback():
    import pytest

    localai_client._validate_url("http://127.0.0.1:8082")
    localai_client._validate_url("https://host.tailnet.ts.net")
    with pytest.raises(ValueError):
        localai_client._validate_url("http://100.64.0.1:8082")


def test_http_error_becomes_localai_request_error(monkeypatch):
    import io

    import pytest
    from urllib.error import HTTPError

    def fail(request, timeout):
        raise HTTPError(request.full_url, 503, "busy", {}, io.BytesIO(b"model loading"))

    monkeypatch.setattr(localai_client._OPENER, "open", fail)
    with pytest.raises(localai_client.LocalAIRequestError, match="503.*model loading"):
        localai_client.chat_completion("p", "m", "http://127.0.0.1:8082")


def test_non_http_scheme_rejected():
    import pytest

    with pytest.raises(ValueError):
        localai_client._validate_url("ftp://127.0.0.1")


def test_oversized_response_rejected(monkeypatch):
    import pytest

    monkeypatch.setattr(localai_client, "MAX_RESPONSE_BYTES", 4)
    monkeypatch.setattr(
        localai_client._OPENER, "open", lambda request, timeout: Response(b"123456")
    )
    with pytest.raises(localai_client.LocalAIRequestError, match="exceeds"):
        localai_client.chat_completion("p", "m", "http://127.0.0.1:8082")
