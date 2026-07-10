import json
from pathlib import Path
import bridge_client


class Response:
    def __init__(self, data): self.data = json.dumps(data).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return self.data


def test_auth_headers():
    assert bridge_client._auth_headers("secret") == {"Authorization": "Bearer secret"}


def test_summarize_builds_request(monkeypatch):
    seen = {}
    def fake(req, timeout):
        seen.update(url=req.full_url, body=json.loads(req.data), auth=req.get_header("Authorization"))
        return Response({"summary": "ok"})
    monkeypatch.setattr(bridge_client, "urlopen", fake)
    assert bridge_client.summarize("hello", "http://bridge/", "tok") == "ok"
    assert seen == {"url": "http://bridge/summarize", "body": {"transcript": "hello"}, "auth": "Bearer tok"}


def test_transcribe_audio(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"; audio.write_bytes(b"audio")
    monkeypatch.setattr(bridge_client, "urlopen", lambda req, timeout: Response({"srt_text": "s", "plain_text": "p"}))
    assert bridge_client.transcribe_audio(audio, "model", "http://b", "t") == ("s", "p")


def test_generate_tags(monkeypatch):
    monkeypatch.setattr(bridge_client, "urlopen", lambda req, timeout: Response({"tags": ["one", "two"]}))
    assert bridge_client.generate_tags("text", "http://b/", "t") == ["one", "two"]
