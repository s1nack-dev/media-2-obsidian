#!/usr/bin/env python3
"""
Small HTTP server exposing pipeline.process_input() over the network, meant
to sit behind a Cloudflare Tunnel (cloudflared) so a POST to a public URL
can trigger processing of a YouTube URL or other link.

Runs either natively on the Mac host or in a container (see
docker/docker-compose.yml) - unlike host_bridge.py, this has no
MLX/parakeet or claude-CLI dependency itself (process_input() talks to
host_bridge.py over HTTP for both via bridge_client.py), so it isn't
tied to running on the host. If containerized, host_bridge.py still has
to run natively on the Mac and be reachable via host.docker.internal -
see README.md's "Containerized deployment" section.

Requests are queued and handled one at a time by a single background
worker thread: process_input() shares host_bridge.py's cached Parakeet
model and the same on-disk git clones across calls, so concurrent
processing isn't safe. The HTTP server itself stays responsive because
handling a request only means validating it and enqueueing it - the slow
work (download, transcribe, summarize, commit) happens in the worker
thread, so a caller gets an immediate 202 rather than blocking for
however long processing takes (which can exceed typical tunnel/edge
request timeouts).

Only http(s) URLs are accepted from the network - unlike pipeline.py's
CLI, this endpoint refuses local file paths, since a network caller has
no business asking this Mac to read/transcribe an arbitrary local file.
"""
import argparse
import json
import logging
import queue
import secrets
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

from core import load_config, notify, pipeline_lock, resolve_secret, validate_public_url
from pipeline import NoTranscriptAvailableError, detect_input_type, process_input

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("server")

MAX_QUEUE_SIZE = 50
_job_queue: "queue.Queue[str]" = queue.Queue(maxsize=MAX_QUEUE_SIZE)

MAX_BODY_BYTES = 10_000


def _worker(cfg: dict, github_token: str, bridge_token: str) -> None:
    """
    Process queued pipeline jobs sequentially and notify configured recipients of their outcomes.
    
    Parameters:
        cfg (dict): Pipeline and notification configuration.
        github_token (str): Token used for GitHub operations.
        bridge_token (str): Token used for bridge operations.
    """
    lock_path = cfg.get("lock_file", "pipeline.lock")
    while True:
        raw_input = _job_queue.get()
        try:
            log.info("Processing queued job: %s", raw_input)
            with pipeline_lock(lock_path):
                result = process_input(raw_input, cfg, github_token=github_token, bridge_token=bridge_token)
            log.info("Done: %s -> %s", result["title"], result["note_path"])
            notify(
                cfg, "Pipeline: processed via webhook",
                f'"{result["title"]}" ({raw_input}) done -> {result["note_path"]}',
            )
        except NoTranscriptAvailableError as e:
            log.error("No transcript available for %s: %s", raw_input, e)
            notify(cfg, "Pipeline: no transcript available (webhook)", f"{raw_input}\n\n{e}")
        except Exception:
            err = traceback.format_exc()
            log.error("Failed processing %s:\n%s", raw_input, err)
            notify(cfg, "Pipeline: webhook job failed", f"{raw_input}\n\n{err[-2000:]}")
        finally:
            _job_queue.task_done()


def make_handler(auth_token: str):
    """Create an authenticated HTTP request handler for the processing endpoints.
    
    Parameters:
        auth_token (str): Token required in the ``Authorization`` header for
            processing requests.
    
    Returns:
        type: A ``BaseHTTPRequestHandler`` subclass that serves health checks and
            validates and queues processing requests.
    """
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

        def do_GET(self):
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok", "queue_depth": _job_queue.qsize()})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/process":
                self._send_json(404, {"error": "not found"})
                return

            provided = self.headers.get("Authorization", "")
            if not secrets.compare_digest(provided, f"Bearer {auth_token}"):
                self._send_json(401, {"error": "unauthorized"})
                return

            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(400, {"error": "missing or oversized request body"})
                return
            raw_body = self.rfile.read(length)

            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON body"})
                return

            raw_input = (payload.get("input") or "").strip()
            if not raw_input:
                self._send_json(400, {"error": "missing 'input' field"})
                return

            try:
                input_type = detect_input_type(raw_input)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return

            if input_type == "local_file":
                self._send_json(400, {"error": "local file paths are not accepted over the network"})
                return

            # SSRF protection: validate URL before enqueueing
            if input_type in ("youtube", "generic_link"):
                try:
                    validate_public_url(raw_input)
                except ValueError as e:
                    self._send_json(400, {"error": f"URL validation failed: {e}"})
                    return

            try:
                _job_queue.put_nowait(raw_input)
            except queue.Full:
                self._send_json(429, {"error": "job queue is full, try again later"})
                return

            depth = _job_queue.qsize()
            log.info("Queued %s (%s), queue depth now %d", raw_input, input_type, depth)
            self._send_json(202, {"status": "queued", "input": raw_input, "queue_depth": depth})

    return Handler


def main():
    """
    Start the authenticated webhook server and its background pipeline worker.
    
    Command-line options control the configuration file, bind address, and port. The server runs until interrupted.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address. 127.0.0.1 is reachable from "
                         "Docker Desktop containers via host.docker.internal without exposing it on the LAN.")
    parser.add_argument("--port", type=int, default=None, help="Overrides webhook.port from config.yaml.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    webhook_cfg = cfg.get("webhook") or {}

    auth_token_ref = webhook_cfg.get("auth_token_op_ref")
    if not auth_token_ref:
        log.error("config.yaml is missing webhook.auth_token_op_ref - refusing to start unauthenticated.")
        sys.exit(1)
    auth_token = resolve_secret("WEBHOOK_AUTH_TOKEN", auth_token_ref)
    github_token = resolve_secret("GITHUB_TOKEN", cfg["github"]["token_op_ref"])
    bridge_token = resolve_secret("BRIDGE_AUTH_TOKEN", cfg["bridge"]["auth_token_op_ref"])

    port = args.port or webhook_cfg.get("port", 8080)

    worker = threading.Thread(target=_worker, args=(cfg, github_token, bridge_token), daemon=True)
    worker.start()

    handler_cls = make_handler(auth_token)
    httpd = HTTPServer((args.host, port), handler_cls)
    log.info("Listening on %s:%d (single-threaded HTTP; one job processed at a time by the worker thread)",
              args.host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
