import importlib
import sys
import types
from io import BytesIO


if "transcribe_backend" not in sys.modules:
    fake = types.ModuleType("transcribe_backend")
    fake.transcribe_audio = lambda path, model: ("srt", "plain")
    sys.modules["transcribe_backend"] = fake
host_bridge = importlib.import_module("host_bridge")

MODEL = "mlx-community/example"


def _handler(path, body=b"", auth="Bearer token"):
    handler_type = host_bridge.make_handler(
        {"transcription": {"model": MODEL}}, "token"
    )
    handler = object.__new__(handler_type)
    handler.path = path
    handler.headers = {"Authorization": auth, "Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.status = None
    handler.send_response = lambda status: setattr(handler, "status", status)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    return handler


def test_bridge_auth_and_health():
    handler = _handler("/healthz")
    handler.do_GET()
    assert handler.status == 200
    handler = _handler("/transcribe?model_id=" + MODEL, b"audio", "Bearer wrong")
    handler.do_POST()
    assert handler.status == 401


def test_bridge_has_no_llm_endpoints():
    for path in ("/summarize", "/tags"):
        handler = _handler(path, b"{}")
        handler.do_POST()
        assert handler.status == 404


def test_bridge_requires_exact_configured_model(monkeypatch):
    handler = _handler("/transcribe?model_id=other", b"audio")
    handler.do_POST()
    assert handler.status == 400

    handler = _handler("/transcribe", b"audio")
    handler.do_POST()
    assert handler.status == 400

    called = []
    monkeypatch.setattr(
        host_bridge,
        "transcribe_audio",
        lambda path, model: called.append(model) or ("srt", "plain"),
    )
    handler = _handler("/transcribe?model_id=" + MODEL, b"audio")
    handler.do_POST()
    assert handler.status == 200
    assert called == [MODEL]


def test_bridge_rejects_invalid_or_incomplete_body(monkeypatch):
    handler = _handler("/transcribe?model_id=" + MODEL, b"audio")
    handler.headers["Content-Length"] = "not-a-number"
    handler.do_POST()
    assert handler.status == 400

    handler = _handler("/transcribe?model_id=" + MODEL, b"")
    handler.do_POST()
    assert handler.status == 400

    handler = _handler("/transcribe?model_id=" + MODEL, b"x")
    handler.headers["Content-Length"] = "2"
    handler.do_POST()
    assert handler.status == 400


def test_bridge_transcription_failure(monkeypatch):
    monkeypatch.setattr(
        host_bridge,
        "transcribe_audio",
        lambda *args: (_ for _ in ()).throw(RuntimeError("bad audio")),
    )
    handler = _handler("/transcribe?model_id=" + MODEL, b"audio")
    handler.do_POST()
    assert handler.status == 500
