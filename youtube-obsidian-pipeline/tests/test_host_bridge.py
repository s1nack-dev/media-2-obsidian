import importlib
import logging
import sys
import types
from io import BytesIO


if "transcribe_backend" not in sys.modules:
    fake = types.ModuleType("transcribe_backend")
    fake.transcribe_audio = lambda path, model: ("srt", "plain")
    fake._format_timestamp = lambda seconds: "00:00:00,000"
    fake._sentences_to_srt = lambda sentences: ""
    sys.modules["transcribe_backend"] = fake
host_bridge = importlib.import_module("host_bridge")


def _handler(path, body=b"", auth="Bearer token", claude_oauth_token=None):
    """
    Create a simulated HTTP handler configured for testing.

    Parameters:
        path: The request path.
        body: The request body.
        auth: The request's Authorization header value.

    Returns:
        A handler instance with simulated request and response streams.
    """
    H = host_bridge.make_handler(
        {"claude": {"command": "claude"}}, "token", claude_oauth_token
    )

    class F:
        def send_response(self, s):
            self.status = s

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

    h = object.__new__(H)
    h.path = path
    h.headers = {"Authorization": auth, "Content-Length": str(len(body))}
    h.rfile = BytesIO(body)
    h.wfile = BytesIO()
    h.status = None
    h.send_response = lambda status: setattr(h, "status", status)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    return h


def test_bridge_auth_and_health():
    h = _handler("/healthz")
    h.do_GET()
    assert h.status == 200
    h = _handler("/other")
    h.do_GET()
    assert h.status == 404
    h = _handler("/summarize", b"{}", "Bearer wrong")
    h.do_POST()
    assert h.status == 401


def test_bridge_json_validation():
    for path in ("/summarize", "/tags"):
        for body in (b"bad", b"[]", b'"text"', b"1", b"null"):
            h = _handler(path, body)
            h.do_POST()
            assert h.status == 400
    h = _handler("/summarize", b"")
    h.do_POST()
    assert h.status == 400


def test_bridge_transcript_validation(monkeypatch):
    called = False

    def summarize(*args):
        nonlocal called
        called = True
        return "summary"

    monkeypatch.setattr(host_bridge, "summarize_with_claude", summarize)
    monkeypatch.setattr(host_bridge, "generate_tags_with_claude", lambda *args: ["tag"])
    for path in ("/summarize", "/tags"):
        for body in (
            b'{"transcript":"\\u0000\\u0000"}',
            b'{"transcript":"   "}',
            b'{"transcript":123}',
        ):
            h = _handler(path, body)
            h.do_POST()
            assert h.status == 400
    assert not called


def test_bridge_summary_and_tags(monkeypatch):
    monkeypatch.setattr(
        host_bridge, "summarize_with_claude", lambda cmd, text, token=None: "summary"
    )
    monkeypatch.setattr(
        host_bridge,
        "generate_tags_with_claude",
        lambda cmd, text, token=None: ["tag"],
    )
    for path, expected in [("/summarize", 200), ("/tags", 200)]:
        h = _handler(path, b'{"transcript":"hello"}')
        h.do_POST()
        assert h.status == expected


def test_bridge_passes_configured_claude_oauth_token(monkeypatch):
    received_tokens = []
    monkeypatch.setattr(
        host_bridge,
        "summarize_with_claude",
        lambda cmd, text, token: received_tokens.append(token) or "summary",
    )
    h = _handler(
        "/summarize", b'{"transcript":"hello"}', claude_oauth_token="test-token"
    )
    h.do_POST()
    assert h.status == 200
    assert received_tokens == ["test-token"]


def test_bridge_logs_summary_progress(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="host_bridge")
    monkeypatch.setattr(
        host_bridge, "summarize_with_claude", lambda cmd, text, token=None: "summary"
    )
    h = _handler("/summarize", b'{"transcript":"hello"}')
    h.do_POST()
    messages = [record.getMessage() for record in caplog.records]
    assert any("Received /summarize request" in message for message in messages)
    assert any("Summary generation started" in message for message in messages)
    assert any("Summary generation completed" in message for message in messages)


def test_bridge_transcribe_validation_and_success(monkeypatch):
    h = _handler("/transcribe", b"audio")
    h.do_POST()
    assert h.status == 400
    h = _handler("/transcribe?model_id=m", b"")
    h.do_POST()
    assert h.status == 400
    h = _handler("/transcribe?model_id=m", b"audio")
    h.headers["Content-Length"] = "5"
    monkeypatch.setattr(
        host_bridge, "transcribe_audio", lambda path, model: ("srt", "plain")
    )
    h.do_POST()
    assert h.status == 200


def test_bridge_unknown_post_and_transcribe_failure(monkeypatch):
    h = _handler("/unknown", b"x")
    h.do_POST()
    assert h.status == 404
    h = _handler("/transcribe?model_id=m", b"audio")
    monkeypatch.setattr(
        host_bridge,
        "transcribe_audio",
        lambda *args: (_ for _ in ()).throw(RuntimeError("bad")),
    )
    h.do_POST()
    assert h.status == 500
