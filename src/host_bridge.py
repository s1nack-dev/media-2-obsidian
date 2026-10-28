#!/usr/bin/env python3
"""Private, host-native transcription bridge for an Apple Silicon Mac.

The pipeline container sends audio to this service over a Tailscale Serve
endpoint. The service binds loopback by default; only Parakeet/MLX and the
bridge bearer token exist on this Mac. Claude Code summary/tag generation is
intentionally performed by the pipeline container instead.
"""

import argparse
import json
import logging
import secrets
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core import MAX_MEDIA_BYTES, load_config, resolve_secret
from transcribe_backend import transcribe_audio

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("host_bridge")

MAX_AUDIO_BYTES = MAX_MEDIA_BYTES
REQUEST_TIMEOUT_SECONDS = 1800


def make_handler(cfg: dict, auth_token: str):
    """Create a handler restricted to the configured, preloaded model."""
    # Same default as pipeline.py, so configs that omit the key keep working.
    allowed_model_id = (cfg.get("transcription") or {}).get(
        "model", "mlx-community/parakeet-tdt-0.6b-v3"
    )

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(REQUEST_TIMEOUT_SECONDS)
            super().setup()

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
            log.info(
                "Received %s request (content_length=%s).",
                self.path,
                self.headers.get("Content-Length", "unknown"),
            )
            if not self._check_auth():
                log.warning("Rejected %s request: invalid bridge token.", self.path)
                return
            if urlparse(self.path).path == "/transcribe":
                self._handle_transcribe()
            else:
                # /summarize and /tags are deliberately absent from this host.
                self._send_json(404, {"error": "not found"})

        def _handle_transcribe(self):
            model_id = (parse_qs(urlparse(self.path).query).get("model_id") or [None])[
                0
            ]
            if not model_id:
                self._send_json(400, {"error": "missing 'model_id' query param"})
                return
            if model_id != allowed_model_id:
                self._send_json(400, {"error": "unsupported model_id"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                self._send_json(400, {"error": "invalid content length"})
                return
            if length <= 0 or length > MAX_AUDIO_BYTES:
                self._send_json(400, {"error": "missing or oversized audio body"})
                return

            log.info(
                "Transcription started (model=%s; audio_bytes=%d).", model_id, length
            )
            with tempfile.TemporaryDirectory(prefix="host-bridge-") as tmp:
                audio_path = Path(tmp) / "audio"
                remaining = length
                with audio_path.open("wb") as audio_file:
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            self._send_json(400, {"error": "incomplete audio body"})
                            return
                        audio_file.write(chunk)
                        remaining -= len(chunk)
                try:
                    srt_text, plain_text = transcribe_audio(audio_path, model_id)
                except Exception as exc:
                    log.error("Transcription failed: %s", exc)
                    self._send_json(500, {"error": f"transcription failed: {exc}"})
                    return

            self._send_json(200, {"srt_text": srt_text, "plain_text": plain_text})
            log.info(
                "Transcription succeeded (srt_chars=%d; transcript_chars=%d).",
                len(srt_text),
                len(plain_text),
            )

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Keep the 127.0.0.1 default and expose remotely with "
        "Tailscale Serve; non-loopback values are not blocked.",
    )
    parser.add_argument("--port", type=int, default=None, help="Overrides bridge.port.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    bridge_cfg = cfg.get("bridge") or {}
    auth_token_ref = bridge_cfg.get("auth_token_op_ref")
    if not auth_token_ref:
        log.error(
            "config.yaml is missing bridge.auth_token_op_ref - refusing unauthenticated startup."
        )
        sys.exit(1)
    auth_token = resolve_secret("BRIDGE_AUTH_TOKEN", auth_token_ref)
    port = args.port or bridge_cfg.get("port", 8081)
    httpd = HTTPServer((args.host, port), make_handler(cfg, auth_token))
    log.info("Listening on %s:%d (transcription-only bridge).", args.host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
