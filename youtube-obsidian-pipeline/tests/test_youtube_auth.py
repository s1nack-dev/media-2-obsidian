import json
import sys
import youtube_auth


def test_auth_main_writes_token(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"; config.write_text("youtube:\n  token_file: %s\n  client_id_op_ref: id\n  client_secret_op_ref: secret\n" % (tmp_path / "token.json"))
    monkeypatch.setattr(youtube_auth, "op_read", lambda ref: "value") if hasattr(youtube_auth, "op_read") else None
    class Creds:
        def to_json(self): return json.dumps({"token":"x"})
    class Flow:
        def run_local_server(self, port=0): """
Provide credentials for local-server authentication.

Parameters:
    port (int): Port requested for the local authentication server.

Returns:
    Creds: Authentication credentials.
"""
return Creds()
    monkeypatch.setattr(youtube_auth.InstalledAppFlow, "from_client_config", lambda config, scopes: Flow())
    monkeypatch.setattr(youtube_auth, "subprocess", type("S", (), {"run": staticmethod(lambda *a, **k: type("R", (), {"stdout":"value"})())}))
    monkeypatch.setattr(sys, "argv", ["youtube_auth.py", "--config", str(config)])
    youtube_auth.main()
    assert (tmp_path / "token.json").exists()
