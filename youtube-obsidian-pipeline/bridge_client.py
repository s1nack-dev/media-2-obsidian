"""
Container-safe HTTP client for host_bridge.py - the host-native service
exposing transcription (Parakeet/MLX) and summarization/tagging (Claude
CLI, subscription auth), neither of which can run inside a Linux
container on this Mac. Used by pipeline.py; has no MLX/parakeet or
claude-CLI dependency itself, just urllib.
"""
import json
from pathlib import Path
from urllib.request import Request, urlopen

_JSON_HEADERS = {"Content-Type": "application/json"}


def _auth_headers(bridge_token: str) -> dict:
    return {"Authorization": f"Bearer {bridge_token}"}


def transcribe_audio(audio_path: Path, model_id: str, bridge_url: str, bridge_token: str) -> tuple[str, str]:
    """Returns (srt_text, plain_text). Raises on failure (caller catches)."""
    url = f"{bridge_url.rstrip('/')}/transcribe?model_id={model_id}"
    req = Request(url, data=audio_path.read_bytes(), method="POST", headers=_auth_headers(bridge_token))
    with urlopen(req, timeout=1800) as resp:
        payload = json.loads(resp.read())
    return payload["srt_text"], payload["plain_text"]


def summarize(transcript_text: str, bridge_url: str, bridge_token: str) -> str:
    url = f"{bridge_url.rstrip('/')}/summarize"
    body = json.dumps({"transcript": transcript_text}).encode()
    headers = {**_JSON_HEADERS, **_auth_headers(bridge_token)}
    req = Request(url, data=body, method="POST", headers=headers)
    with urlopen(req, timeout=600) as resp:
        payload = json.loads(resp.read())
    return payload["summary"]


def generate_tags(transcript_text: str, bridge_url: str, bridge_token: str) -> list[str]:
    url = f"{bridge_url.rstrip('/')}/tags"
    body = json.dumps({"transcript": transcript_text}).encode()
    headers = {**_JSON_HEADERS, **_auth_headers(bridge_token)}
    req = Request(url, data=body, method="POST", headers=headers)
    with urlopen(req, timeout=600) as resp:
        payload = json.loads(resp.read())
    return payload["tags"]
