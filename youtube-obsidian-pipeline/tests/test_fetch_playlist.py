import json

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
