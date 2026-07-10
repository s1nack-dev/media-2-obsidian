#!/usr/bin/env python3
"""
Host-native bridge service exposing the two things that can't run in a
Linux container on this Mac:

  - Transcription (transcribe_backend.py) - needs direct Metal/Neural
    Engine access via MLX, which Docker Desktop's Linux VM can't provide.
  - Summarization/tagging (core.py's claude -p calls) - needs the Claude
    Code CLI's subscription OAuth session, which is tied to this Mac's
    keychain/session and can't be shared with a container.

The containerized pipeline (pipeline.py, via bridge_client.py) calls this
service over HTTP instead of doing either of these in-process. Unlike
server.py (webhook mode), there's no queue here - each endpoint just does
the work synchronously and returns the result, since the caller
(pipeline.py running inside the container) is already the one blocking on
it as part of its own single-item processing. Single-threaded HTTP
handling on purpose: the Parakeet model and the claude CLI shouldn't be
hit concurrently from a shared process.
"""
import argparse
import json
import logging
import secrets
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from core import generate_tags_with_claude, load_config, resolve_secret, summarize_with_claude
from transcribe_backend import transcribe_audio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("host_bridge")

MAX_AUDIO_BYTES = 500 * 1024 * 1024  # 500MB - generous headroom for long podcast episodes
MAX_JSON_BYTES = 1_000_000  # transcripts are text, 1MB is already very generous


def make_handler(cfg: dict, auth_token: str):
    claude_cmd = cfg["claude"]["command"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            provided = self.headers.get("Authorization", "")
            if not secrets.compare_digest(provided, f"Bearer {auth_token}"):
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self._check_auth():
                return

            if self.path.startswith("/transcribe"):
                self._handle_transcribe()
            elif self.path == "/summarize":
                self._handle_summarize()
            elif self.path == "/tags":
                self._handle_tags()
            else:
                self._send_json(404, {"error": "not found"})

        def _handle_transcribe(self):
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(self.path).query)
            model_id = (query.get("model_id") or [None])[0]
            if not model_id:
                self._send_json(400, {"error": "missing 'model_id' query param"})
                return

            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0 or length > MAX_AUDIO_BYTES:
                self._send_json(400, {"error": "missing or oversized audio body"})
                return

            with tempfile.TemporaryDirectory(prefix="host-bridge-") as tmp:
                audio_path = Path(tmp) / "audio"
                remaining = length
                with open(audio_path, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)

                try:
                    srt_text, plain_text = transcribe_audio(audio_path, model_id)
                except Exception as e:
                    log.error("Transcription failed: %s", e)
                    self._send_json(500, {"error": f"transcription failed: {e}"})
                    return

            self._send_json(200, {"srt_text": srt_text, "plain_text": plain_text})

        def _read_json_body(self) -> dict | None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0 or length > MAX_JSON_BYTES:
                self._send_json(400, {"error": "missing or oversized request body"})
                return None
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return None

        def _handle_summarize(self):
            payload = self._read_json_body()
            if payload is None:
                return
            transcript = payload.get("transcript", "")
            summary = summarize_with_claude(claude_cmd, transcript)
            self._send_json(200, {"summary": summary})

        def _handle_tags(self):
            payload = self._read_json_body()
            if payload is None:
                return
            transcript = payload.get("transcript", "")
            tags = generate_tags_with_claude(claude_cmd, transcript)
            self._send_json(200, {"tags": tags})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address. 127.0.0.1 is reachable from "
                         "Docker Desktop containers via host.docker.internal without exposing it on the LAN.")
    parser.add_argument("--port", type=int, default=None, help="Overrides bridge.port from config.yaml.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    bridge_cfg = cfg.get("bridge") or {}

    auth_token_ref = bridge_cfg.get("auth_token_op_ref")
    if not auth_token_ref:
        log.error("config.yaml is missing bridge.auth_token_op_ref - refusing to start unauthenticated.")
        sys.exit(1)
    auth_token = resolve_secret("BRIDGE_AUTH_TOKEN", auth_token_ref)

    port = args.port or bridge_cfg.get("port", 8081)

    handler_cls = make_handler(cfg, auth_token)
    httpd = HTTPServer((args.host, port), handler_cls)
    log.info("Listening on %s:%d (transcribe/summarize/tags bridge for the containerized pipeline)",
             args.host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
