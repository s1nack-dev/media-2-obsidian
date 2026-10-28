import json

import localai_client


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
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

    monkeypatch.setattr(localai_client, "urlopen", fake)
    assert localai_client.transcribe_audio(
        audio, "parakeet-110m", "http://localai", "secret"
    ) == (
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        "Hello",
    )
    assert seen["url"] == "http://localai/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer secret"
    assert "multipart/form-data" in seen["content_type"]
    assert b'name="model"' in seen["body"]
    assert b"parakeet-110m" in seen["body"]
    assert b'name="response_format"' in seen["body"]
    assert b"srt" in seen["body"]


def test_chat_completion_uses_openai_compatible_endpoint(monkeypatch):
    seen = {}

    def fake(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        return Response(
            json.dumps({"choices": [{"message": {"content": " Done "}}]}).encode()
        )

    monkeypatch.setattr(localai_client, "urlopen", fake)
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
