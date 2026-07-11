import json
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer
import pytest
from io import BytesIO

import server


def _serve(handler):
    """
    Start a daemon-threaded HTTP server on an ephemeral loopback port.

    Parameters:
        handler: The request handler class or factory used by the HTTP server.

    Returns:
        HTTPServer: The running HTTP server instance.
    """
    try:
        httpd = HTTPServer(("127.0.0.1", 0), handler)
    except PermissionError:
        pytest.skip("network sockets are unavailable in this sandbox")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def test_server_health_and_auth_validation(monkeypatch):
    httpd = _serve(server.make_handler("secret"))
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with (
            urllib.request.urlopen(base + "/healthz") as response
        ):  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            assert json.loads(response.read())["status"] == "ok"
        req = urllib.request.Request(base + "/process", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(
                req
            )  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
    finally:
        httpd.shutdown()


def test_server_rejects_invalid_payload(monkeypatch):
    monkeypatch.setattr(server, "validate_public_url", lambda url: None)
    httpd = _serve(server.make_handler("secret"))
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        req = urllib.request.Request(
            base + "/process",
            data=b"not-json",
            method="POST",
            headers={"Authorization": "Bearer secret"},
        )
        try:
            urllib.request.urlopen(
                req
            )  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        httpd.shutdown()


def test_handler_validation_without_network(monkeypatch):
    Handler = server.make_handler("secret")

    class Fake:
        def __init__(self, path, body=b"", auth="Bearer secret"):
            self.path = path
            self.rfile = BytesIO(body)
            self.wfile = BytesIO()
            self.headers = {"Authorization": auth, "Content-Length": str(len(body))}
            self.responses = []

        def send_response(self, status):
            self.responses.append(status)

        def send_header(self, *args):
            pass

        def end_headers(self):
            pass

        def log_message(self, *args):
            pass

    h = object.__new__(Handler)
    f = Fake("/healthz")
    h.__dict__.update(f.__dict__)
    h.send_response = f.send_response
    h.send_header = f.send_header
    h.end_headers = f.end_headers
    h.do_GET()
    assert f.responses == [200]
    h = object.__new__(Handler)
    f = Fake("/process", b"{}", "Bearer wrong")
    h.__dict__.update(f.__dict__)
    h.send_response = f.send_response
    h.send_header = f.send_header
    h.end_headers = f.end_headers
    h.do_POST()
    assert f.responses == [401]
    h = object.__new__(Handler)
    f = Fake("/process", b"{}", "Bearer secret")
    h.__dict__.update(f.__dict__)
    h.send_response = f.send_response
    h.send_header = f.send_header
    h.end_headers = f.end_headers
    monkeypatch.setattr(
        server,
        "detect_input_type",
        lambda value: (_ for _ in ()).throw(ValueError("bad input")),
    )
    h.do_POST()
    assert f.responses == [400]


def test_handler_queues_valid_url(monkeypatch):
    Handler = server.make_handler("secret")

    class Fake:
        path = "/process"

        def __init__(self):
            body = b'{"input":"https://example.com/a"}'
            self.rfile = BytesIO(body)
            self.wfile = BytesIO()
            self.headers = {
                "Authorization": "Bearer secret",
                "Content-Length": str(len(body)),
            }
            self.responses = []

        def send_response(self, s):
            self.responses.append(s)

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

    old = server._job_queue
    import queue

    server._job_queue = queue.Queue(maxsize=2)
    try:
        monkeypatch.setattr(server, "detect_input_type", lambda value: "generic_link")
        monkeypatch.setattr(server, "validate_public_url", lambda value: None)
        f = Fake()
        h = object.__new__(Handler)
        h.__dict__.update(f.__dict__)
        h.path = f.path
        h.send_response = f.send_response
        h.send_header = f.send_header
        h.end_headers = f.end_headers
        h.do_POST()
        assert f.responses == [202]
        assert server._job_queue.get_nowait() == "https://example.com/a"
    finally:
        server._job_queue = old


def test_handler_rejects_local_file_and_ssrf(monkeypatch):
    Handler = server.make_handler("secret")

    class F:
        def __init__(self, value):
            """
            Initialize a fake authenticated request containing the provided input value.

            Parameters:
                value (str): Input value to include in the request body.
            """
            body = ('{"input":"' + value + '"}').encode()
            self.path = "/process"
            self.rfile = BytesIO(body)
            self.wfile = BytesIO()
            self.headers = {
                "Authorization": "Bearer secret",
                "Content-Length": str(len(body)),
            }
            self.responses = []

        def send_response(self, s):
            self.responses.append(s)

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

    monkeypatch.setattr(
        server,
        "detect_input_type",
        lambda value: "local_file" if value == "local" else "generic_link",
    )
    for value, validator in [
        ("local", None),
        ("https://private", ValueError("private")),
    ]:
        if validator:
            monkeypatch.setattr(
                server,
                "validate_public_url",
                lambda value: (_ for _ in ()).throw(validator),
            )
        f = F(value)
        h = object.__new__(Handler)
        h.__dict__.update(f.__dict__)
        h.send_response = f.send_response
        h.send_header = f.send_header
        h.end_headers = f.end_headers
        h.do_POST()
        assert f.responses == [400]


def test_handler_queue_full(monkeypatch):
    import queue

    Handler = server.make_handler("secret")

    class F:
        path = "/process"

        def __init__(self):
            body = b'{"input":"https://example.com/a"}'
            self.rfile = BytesIO(body)
            self.wfile = BytesIO()
            self.headers = {
                "Authorization": "Bearer secret",
                "Content-Length": str(len(body)),
            }
            self.responses = []

        def send_response(self, s):
            self.responses.append(s)

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

    old = server._job_queue
    server._job_queue = queue.Queue(maxsize=1)
    server._job_queue.put("existing")
    try:
        monkeypatch.setattr(server, "detect_input_type", lambda v: "generic_link")
        monkeypatch.setattr(server, "validate_public_url", lambda v: None)
        f = F()
        h = object.__new__(Handler)
        h.__dict__.update(f.__dict__)
        h.path = f.path
        h.send_response = f.send_response
        h.send_header = f.send_header
        h.end_headers = f.end_headers
        h.do_POST()
        assert f.responses == [429]
    finally:
        server._job_queue = old


def test_worker_success_and_failure_branches(monkeypatch, tmp_path):
    class Q:
        def __init__(self, item):
            self.item = item
            self.done = False

        def get(self):
            if self.done:
                raise KeyboardInterrupt
            self.done = True
            return self.item

        def task_done(self):
            pass

    monkeypatch.setattr(
        server, "pipeline_lock", lambda path: __import__("contextlib").nullcontext()
    )
    monkeypatch.setattr(server, "notify", lambda *args: None)
    monkeypatch.setattr(
        server,
        "process_input",
        lambda *args, **kwargs: {"title": "T", "note_path": "n"},
    )
    monkeypatch.setattr(server, "_job_queue", Q("https://example/success"))
    try:
        server._worker({}, "g", "b")
    except KeyboardInterrupt:
        pass
    monkeypatch.setattr(
        server,
        "process_input",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(server, "_job_queue", Q("https://example/failure"))
    try:
        server._worker({}, "g", "b")
    except KeyboardInterrupt:
        pass
    monkeypatch.setattr(
        server,
        "process_input",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            server.NoTranscriptAvailableError("none")
        ),
    )
    monkeypatch.setattr(server, "_job_queue", Q("https://example/no-transcript"))
    try:
        server._worker({}, "g", "b")
    except KeyboardInterrupt:
        pass
