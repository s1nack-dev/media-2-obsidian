#!/usr/bin/env python3
"""Poll the private transcription bridge and notify only on state changes."""

import argparse
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from core import load_config, notify

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("bridge_monitor")


def _bridge_health_url(cfg: dict) -> str:
    bridge_url = os.environ.get("BRIDGE_URL") or cfg["bridge"]["url"]
    if urlparse(bridge_url).scheme not in ("http", "https"):
        raise ValueError("bridge.url must use http or https")
    return f"{bridge_url.rstrip('/')}/healthz"


def check_bridge(cfg: dict) -> tuple[bool, str]:
    """Return bridge health without sending credentials or transcript data."""
    monitor_cfg = cfg.get("bridge_monitor") or {}
    timeout = int(monitor_cfg.get("timeout_seconds", 10))
    url = _bridge_health_url(cfg)
    try:
        with urlopen(url, timeout=timeout) as response:  # nosec B310 - scheme validated
            payload = json.loads(response.read())
        if response.status != 200 or payload.get("status") != "ok":
            return False, f"unexpected health response from {url}: {payload!r}"
    except Exception as exc:
        return False, f"bridge health check failed for {url}: {exc}"
    return True, f"bridge healthy at {url}"


def _load_state(path: Path) -> bool | None:
    try:
        value = json.loads(path.read_text())
        return value.get("healthy") if isinstance(value.get("healthy"), bool) else None
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _save_state(path: Path, healthy: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"healthy": healthy}) + "\n")
    temporary.replace(path)


def run_once(cfg: dict, state_file: Path) -> bool:
    healthy, detail = check_bridge(cfg)
    previous = _load_state(state_file)
    _save_state(state_file, healthy)
    log.info(detail)
    if previous is not None and previous != healthy:
        subject = (
            "Pipeline: transcription bridge recovered"
            if healthy
            else "Pipeline: transcription bridge unavailable"
        )
        notify(cfg, subject, detail)
    elif previous is None and not healthy:
        notify(cfg, "Pipeline: transcription bridge unavailable", detail)
    return healthy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--state-file", default="bridge-monitor.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=None)
    args = parser.parse_args()
    cfg = load_config(args.config)
    monitor_cfg = cfg.get("bridge_monitor") or {}
    interval = args.interval or int(monitor_cfg.get("interval_seconds", 60))
    if interval <= 0:
        parser.error("--interval must be positive")
    state_file = Path(args.state_file)

    while True:
        healthy = run_once(cfg, state_file)
        if args.once:
            raise SystemExit(0 if healthy else 1)
        time.sleep(interval)


if __name__ == "__main__":
    main()
