import sys

import pytest

import claude_client

# sandbox-exec is macOS-only (Seatbelt). CI runs on Linux, where it doesn't
# exist, so _run_claude always takes the safe-mode fallback there regardless
# of what these tests assert. Skip rather than fake shutil.which - these
# tests should verify real host behavior, not a simulated one.
requires_sandbox_exec = pytest.mark.skipif(
    sys.platform != "darwin", reason="sandbox-exec (Seatbelt) is macOS-only"
)


@requires_sandbox_exec
def test_summarize_retries_polluted_response(monkeypatch):
    responses = [
        type("R", (), {"returncode": 0, "stdout": "planning...", "stderr": ""})(),
        type(
            "R",
            (),
            {"returncode": 0, "stdout": "prefix\n## SUMMARY\n- done", "stderr": ""},
        )(),
    ]
    calls = []
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(claude_client.subprocess, "run", run)
    assert claude_client.summarize_with_claude(
        "claude", "transcript", "configured-token"
    ).startswith("## SUMMARY")
    for command, kwargs in calls:
        assert command[0].endswith("sandbox-exec")
        assert command[1] == "-p"
        profile = command[2]
        assert "(deny default)" in profile
        assert "(deny network*)" in profile
        assert '(allow network-outbound (remote tcp "*:443"))' in profile
        assert '(subpath "/private/tmp/claude-bridge-' in profile
        assert "--safe-mode" in command
        assert command[command.index("--tools") + 1] == ""
        assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        assert kwargs["cwd"] == "/"
        assert {
            "PATH",
            "HOME",
            "USER",
            "TMPDIR",
            "CLAUDE_CODE_TMPDIR",
        }.issubset(kwargs["env"])
        assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "configured-token"
        assert kwargs["env"]["CLAUDE_CODE_TMPDIR"].startswith(
            kwargs["env"]["TMPDIR"] + "/"
        )


def test_summarize_failure(monkeypatch):
    result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
    monkeypatch.setattr(claude_client.subprocess, "run", lambda *a, **k: result)
    assert (
        claude_client.summarize_with_claude("claude", "text")
        == "_Summary generation failed._"
    )


@requires_sandbox_exec
def test_summarize_falls_back_when_macos_rejects_sandbox(monkeypatch):
    responses = [
        type(
            "R",
            (),
            {
                "returncode": 71,
                "stdout": "",
                "stderr": "sandbox-exec: sandbox_apply: Operation not permitted",
            },
        )(),
        type(
            "R", (), {"returncode": 0, "stdout": "## SUMMARY\n- done", "stderr": ""}
        )(),
    ]
    calls = []

    monkeypatch.setattr(claude_client, "_OS_SANDBOX_REJECTED", False)

    def run(command, **kwargs):
        calls.append(command)
        return responses.pop(0)

    monkeypatch.setattr(claude_client.subprocess, "run", run)
    assert claude_client.summarize_with_claude("claude", "transcript").startswith(
        "## SUMMARY"
    )
    assert calls[0][0].endswith("sandbox-exec")
    assert calls[1][0].endswith("/claude")
    assert "--safe-mode" in calls[1]
    assert calls[1][calls[1].index("--mcp-config") + 1] == '{"mcpServers":{}}'


def test_claude_failure_detail_reports_login_state_without_output():
    result = type(
        "R",
        (),
        {
            "returncode": 1,
            "stdout": "Not logged in · Please run /login",
            "stderr": "",
        },
    )()
    assert "run `claude login`" in claude_client._claude_failure_detail(result)


def test_tag_sanitization_and_filtering(monkeypatch):
    monkeypatch.setattr(
        claude_client.subprocess,
        "run",
        lambda *a, **k: type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": "- Machine Learning\nnoise!\nMachine Learning\n",
            },
        )(),
    )
    assert claude_client.generate_tags_with_claude("claude", "text") == [
        "machine-learning"
    ]


def test_tags_cli_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(
        claude_client.subprocess,
        "run",
        lambda *a, **k: type(
            "R", (), {"returncode": 1, "stdout": "", "stderr": "bad"}
        )(),
    )
    assert claude_client.generate_tags_with_claude("claude", "text") == []
