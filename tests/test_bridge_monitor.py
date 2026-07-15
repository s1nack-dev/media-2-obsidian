import json

import bridge_monitor


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return b'{"status":"ok"}'


def _cfg():
    return {
        "bridge": {"url": "https://transcriber.tailnet.ts.net"},
        "bridge_monitor": {},
    }


def test_check_bridge_uses_private_health_endpoint(monkeypatch):
    seen = {}

    def fake(request, timeout):
        seen["url"] = getattr(request, "full_url", request)
        return Response()

    monkeypatch.setattr(bridge_monitor, "urlopen", fake)
    assert bridge_monitor.check_bridge(_cfg())[0] is True
    assert seen["url"] == "https://transcriber.tailnet.ts.net/healthz"


def test_monitor_notifies_only_on_health_transitions(monkeypatch, tmp_path):
    results = iter([(False, "down"), (False, "down"), (True, "up")])
    notices = []
    monkeypatch.setattr(bridge_monitor, "check_bridge", lambda cfg: next(results))
    monkeypatch.setattr(
        bridge_monitor,
        "notify",
        lambda cfg, subject, body: notices.append((subject, body)),
    )
    state = tmp_path / "monitor.json"

    assert bridge_monitor.run_once(_cfg(), state) is False
    assert bridge_monitor.run_once(_cfg(), state) is False
    assert bridge_monitor.run_once(_cfg(), state) is True
    assert notices == [
        ("Pipeline: transcription bridge unavailable", "down"),
        ("Pipeline: transcription bridge recovered", "up"),
    ]
    assert json.loads(state.read_text()) == {"healthy": True}
