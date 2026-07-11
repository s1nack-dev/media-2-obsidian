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

from core import (
    MAX_MEDIA_BYTES,
    generate_tags_with_claude,
    load_config,
    resolve_secret,
    summarize_with_claude,
)
from transcribe_backend import transcribe_audio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("host_bridge")

MAX_AUDIO_BYTES = MAX_MEDIA_BYTES
MAX_JSON_BYTES = 1_000_000  # transcripts are text, 1MB is already very generous
REQUEST_TIMEOUT_SECONDS = 1800  # Matches the longest bridge client request timeout


def make_handler(cfg: dict, auth_token: str):
    """
    Create an HTTP request handler for the host bridge service.

    Parameters:
        cfg (dict): Configuration containing the Claude command.
        auth_token (str): Bearer token required for POST requests.

    Returns:
        type: A configured HTTP request handler class.
    """
    claude_cmd = cfg["claude"]["command"]

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(REQUEST_TIMEOUT_SECONDS)
            super().setup()

        def log_message(self, fmt, *args):
            """Log an HTTP request message with the client's address."""
            log.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, status: int, payload: dict) -> None:
            """
            Send a JSON response with the specified HTTP status and payload.

            Parameters:
                status (int): HTTP status code for the response.
                payload (dict): JSON-serializable response body.
            """
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            """
            Validate the request's bearer token and send an unauthorized response when authentication fails.

            Returns:
                bool: `True` if the request has the expected bearer token, `False` otherwise.
            """
            provided = self.headers.get("Authorization", "")
            if not secrets.compare_digest(provided, f"Bearer {auth_token}"):
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            """Handle health-check requests and return a not-found response for other paths."""
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            """Authenticate the request and dispatch it to the appropriate POST endpoint."""
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
            """
            Transcribe an uploaded audio file using the requested model.

            Parameters:
                model_id (str): Identifier of the transcription model provided in the request query.

            Returns:
                JSON response containing the SRT-formatted and plain-text transcripts, or an error response for invalid input or transcription failure.
            """
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
            """
            Read and parse the request body as JSON.

            Returns:
                dict | None: The decoded JSON object, or `None` when the body is missing, oversized, or invalid.
            """
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
            """Generate a summary from the transcript in the JSON request body.

            Parameters:
                transcript: The transcript text to summarize.

            Returns:
                A JSON response containing the generated summary.
            """
            payload = self._read_json_body()
            if payload is None:
                return
            transcript = payload.get("transcript", "")
            summary = summarize_with_claude(claude_cmd, transcript)
            self._send_json(200, {"summary": summary})

        def _handle_tags(self):
            """Generate tags for the transcript in the JSON request body and send them as a JSON response."""
            payload = self._read_json_body()
            if payload is None:
                return
            transcript = payload.get("transcript", "")
            tags = generate_tags_with_claude(claude_cmd, transcript)
            self._send_json(200, {"tags": tags})

    return Handler


def main():
    """
    Start the host-native HTTP bridge service using command-line and configuration settings.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. 127.0.0.1 is reachable from "
        "Docker Desktop containers via host.docker.internal without exposing it on the LAN.",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="Overrides bridge.port from config.yaml."
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    bridge_cfg = cfg.get("bridge") or {}

    auth_token_ref = bridge_cfg.get("auth_token_op_ref")
    if not auth_token_ref:
        log.error(
            "config.yaml is missing bridge.auth_token_op_ref - refusing to start unauthenticated."
        )
        sys.exit(1)
    auth_token = resolve_secret("BRIDGE_AUTH_TOKEN", auth_token_ref)

    port = args.port or bridge_cfg.get("port", 8081)

    handler_cls = make_handler(cfg, auth_token)
    httpd = HTTPServer((args.host, port), handler_cls)
    log.info(
        "Listening on %s:%d (transcribe/summarize/tags bridge for the containerized pipeline)",
        args.host,
        port,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
