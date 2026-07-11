import json
import threading

import fetch_playlist


class _Request:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Playlist:
    def __init__(self):
        self.calls = 0

    def list(self, **kwargs):
        """Return a paginated playlist response containing a video item on the first call.

        Returns:
                _Request: A response with the first playlist item and a continuation token on the first call, or an empty item list on subsequent calls.
        """
        self.calls += 1
        return _Request(
            {
                "items": [
                    {
                        "contentDetails": {"videoId": "v1"},
                        "snippet": {"title": "One", "publishedAt": "2024"},
                    }
                ],
                "nextPageToken": "next",
            }
            if self.calls == 1
            else {"items": []}
        )


class _Service:
    def __init__(self):
        self.p = _Playlist()

    def playlistItems(self):
        return self.p


def test_get_playlist_items_paginates():
    assert fetch_playlist.get_playlist_items(_Service(), "playlist") == [
        {"video_id": "v1", "title": "One", "published_at": "2024"}
    ]


def test_run_once_processes_and_persists(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    cfg = {
        "state_file": str(state),
        "youtube": {"playlist_id": "p"},
        "github": {"token_op_ref": "x"},
        "bridge": {"auth_token_op_ref": "y"},
        "lock_file": str(tmp_path / "lock"),
    }
    monkeypatch.setattr(fetch_playlist, "resolve_secret", lambda *args: "token")
    monkeypatch.setattr(fetch_playlist, "get_youtube_service", lambda cfg: _Service())
    monkeypatch.setattr(
        fetch_playlist, "process_input", lambda *args, **kwargs: {"title": "One"}
    )
    fetch_playlist.run_once(cfg)
    assert json.loads(state.read_text())["processed_video_ids"] == ["v1"]


def test_run_once_serializes_state_selection(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    cfg = {
        "state_file": str(state),
        "youtube": {"playlist_id": "p"},
        "github": {"token_op_ref": "x"},
        "bridge": {"auth_token_op_ref": "y"},
        "lock_file": str(tmp_path / "lock"),
    }
    first_processing = threading.Event()
    release_first = threading.Event()
    second_selected_state = threading.Event()
    process_calls = []
    state_loads = 0
    state_loads_guard = threading.Lock()
    original_load_state = fetch_playlist.load_state

    def load_state(path):
        nonlocal state_loads
        with state_loads_guard:
            state_loads += 1
            if state_loads == 2:
                second_selected_state.set()
        return original_load_state(path)

    def process_input(*args, **kwargs):
        process_calls.append(args[0])
        first_processing.set()
        assert release_first.wait(timeout=2)
        return {"title": "One"}

    monkeypatch.setattr(fetch_playlist, "resolve_secret", lambda *args: "token")
    monkeypatch.setattr(fetch_playlist, "get_youtube_service", lambda cfg: _Service())
    monkeypatch.setattr(fetch_playlist, "load_state", load_state)
    monkeypatch.setattr(fetch_playlist, "process_input", process_input)

    first = threading.Thread(target=fetch_playlist.run_once, args=(cfg,))
    first.start()
    assert first_processing.wait(timeout=2)

    second = threading.Thread(target=fetch_playlist.run_once, args=(cfg,))
    second.start()
    assert not second_selected_state.wait(timeout=0.2)

    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert process_calls == ["https://www.youtube.com/watch?v=v1"]


def test_run_once_retries_transient_failure(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    cfg = {
        "state_file": str(state),
        "youtube": {"playlist_id": "p"},
        "github": {"token_op_ref": "x"},
        "bridge": {"auth_token_op_ref": "y"},
        "lock_file": str(tmp_path / "lock"),
        "max_retries": 3,
    }
    monkeypatch.setattr(fetch_playlist, "resolve_secret", lambda *args: "token")
    monkeypatch.setattr(fetch_playlist, "get_youtube_service", lambda cfg: _Service())
    monkeypatch.setattr(
        fetch_playlist,
        "process_input",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("temporary")),
    )
    monkeypatch.setattr(fetch_playlist, "notify", lambda *args: None)
    fetch_playlist.run_once(cfg)
    assert json.loads(state.read_text())["failed_attempts"] == {"v1": 1}


def test_run_once_skips_already_processed(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"processed_video_ids": ["v1"], "failed_attempts": {}}))
    cfg = {
        "state_file": str(state),
        "youtube": {"playlist_id": "p"},
        "github": {"token_op_ref": "x"},
        "bridge": {"auth_token_op_ref": "y"},
    }
    monkeypatch.setattr(fetch_playlist, "resolve_secret", lambda *args: "token")
    monkeypatch.setattr(fetch_playlist, "get_youtube_service", lambda cfg: _Service())
    fetch_playlist.run_once(cfg)


def test_run_once_gives_up_after_max_retries(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"processed_video_ids": [], "failed_attempts": {"v1": 2}})
    )
    cfg = {
        "state_file": str(state),
        "youtube": {"playlist_id": "p"},
        "github": {"token_op_ref": "x"},
        "bridge": {"auth_token_op_ref": "y"},
        "max_retries": 3,
        "lock_file": str(tmp_path / "lock"),
    }
    monkeypatch.setattr(fetch_playlist, "resolve_secret", lambda *args: "token")
    monkeypatch.setattr(fetch_playlist, "get_youtube_service", lambda cfg: _Service())
    monkeypatch.setattr(
        fetch_playlist,
        "process_input",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("permanent")),
    )
    monkeypatch.setattr(fetch_playlist, "notify", lambda *args: None)
    fetch_playlist.run_once(cfg)
    saved = json.loads(state.read_text())
    assert (
        saved["processed_video_ids"] == ["v1"] and saved["failed_attempts"]["v1"] == 3
    )
