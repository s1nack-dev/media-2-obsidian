import json
from pathlib import Path

import pytest

import core


def test_srt_to_plain_text_removes_timing_and_deduplicates(tmp_path):
    p = tmp_path / "x.srt"
    p.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n\n2\n00:00:01,000 --> 00:00:02,000\nHello\nWorld\n")
    assert core.srt_to_plain_text(p) == "Hello World"


@pytest.mark.parametrize("title, expected", [("Hello, World!", "hello-world"), ("  !!! ", "untitled"), ("a" * 100, "a" * 80)])
def test_slugify(title, expected):
    assert core.slugify(title) == expected


def test_state_round_trip_and_defaults(tmp_path):
    path = tmp_path / "state.json"
    assert core.load_state(str(path)) == {"processed_video_ids": [], "failed_attempts": {}}
    path.write_text(json.dumps({"processed_video_ids": ["a"]}))
    assert core.load_state(str(path))["failed_attempts"] == {}
    core.save_state(str(path), {"processed_video_ids": ["b"]})
    assert json.loads(path.read_text())["processed_video_ids"] == ["b"]


def test_secret_prefers_environment(monkeypatch):
    monkeypatch.setenv("TOKEN", "resolved")
    monkeypatch.setattr(core, "op_read", lambda _: pytest.fail("op should not run"))
    assert core.resolve_secret("TOKEN", "op://ref") == "resolved"


def test_secret_falls_back_to_op(monkeypatch):
    monkeypatch.delenv("TOKEN", raising=False)
    monkeypatch.setattr(core, "op_read", lambda ref: "from-op")
    assert core.resolve_secret("TOKEN", "op://ref") == "from-op"


def test_load_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("name: demo\nitems:\n  - one\n")
    assert core.load_config(str(path)) == {"name": "demo", "items": ["one"]}


@pytest.mark.parametrize("url", ["file:///tmp/x", "http://127.0.0.1", "http://169.254.169.254"])
def test_validate_public_url_rejects_unsafe(monkeypatch, url):
    if url.startswith("http"):
        monkeypatch.setattr(core.socket, "getaddrinfo", lambda *a: [(None, None, None, None, (urlparse_host(url), 0))])
    with pytest.raises(ValueError):
        core.validate_public_url(url)


def test_validate_public_url_accepts_public(monkeypatch):
    monkeypatch.setattr(core.socket, "getaddrinfo", lambda *a: [(None, None, None, None, ("93.184.216.34", 0))])
    core.validate_public_url("https://example.com/page")


def test_summarize_retries_polluted_response(monkeypatch):
    responses = [type("R", (), {"returncode": 0, "stdout": "planning...", "stderr": ""})(), type("R", (), {"returncode": 0, "stdout": "prefix\n## SUMMARY\n- done", "stderr": ""})()]
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: responses.pop(0))
    assert core.summarize_with_claude("claude", "transcript").startswith("## SUMMARY")


def test_summarize_failure(monkeypatch):
    result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: result)
    assert core.summarize_with_claude("claude", "text") == "_Summary generation failed._"


def test_notify_webhook_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(core.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    core.notify({"notifications": {"webhook_url": "https://hooks.example"}}, "subject", "message")


def urlparse_host(url):
    """Extract the hostname from a URL.
    
    Parameters:
    	url (str): The URL to parse.
    
    Returns:
    	str or None: The URL's hostname, or `None` when no hostname is present.
    """
    from urllib.parse import urlparse
    return urlparse(url).hostname


def test_build_note_contains_metadata():
    note = core.build_note("Title", "youtube", "https://youtu.be/x", "x", "2024-01-02", "https://git/sub.srt", "summary", ["ai"])
    assert "# Title" in note and "youtube" in note and "ai" in note and "summary" in note


def test_tag_sanitization_and_filtering(monkeypatch):
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "- Machine Learning\nnoise!\nMachine Learning\n"})())
    assert core.generate_tags_with_claude("claude", "text") == ["machine-learning"]


def test_private_ip_classification():
    import ipaddress
    assert core._is_private_ip(ipaddress.ip_address("127.0.0.1"))
    assert core._is_private_ip(ipaddress.ip_address("169.254.169.254"))
    assert not core._is_private_ip(ipaddress.ip_address("8.8.8.8"))


def test_tags_cli_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": "bad"})())
    assert core.generate_tags_with_claude("claude", "text") == []


def test_run_git_redacts_token(monkeypatch, tmp_path):
    token = "sec" + "ret"
    credential_url = "https://x-access-token:" + token + "@github.com"
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": credential_url})())
    with pytest.raises(RuntimeError, match="REDACTED"):
        core.run_git(["push", credential_url], tmp_path)


def test_ensure_repo_clone_and_update(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run_git", lambda args, cwd: calls.append((args, cwd)))
    path = core.ensure_repo("https://github.com/me/repo.git", str(tmp_path / "repo"), "main", "tok")
    assert path.exists() and calls[0][0][0] == "clone"
    (path / ".git").mkdir()
    core.ensure_repo("https://github.com/me/repo.git", str(path), "main", "tok")
    assert any(c[0][0] == "reset" for c in calls)
