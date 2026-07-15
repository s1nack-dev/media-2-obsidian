"""
Container-safe HTTP client for host_bridge.py - the host-native,
transcription-only service running with Parakeet/MLX on the remote Mac.
Summarization and tagging run locally in the pipeline container through
``claude_client.py``. This module intentionally has no MLX/parakeet or
Claude Code dependency; it only sends audio to the private bridge.
"""

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class BridgeRequestError(RuntimeError):
    """Raised when host_bridge.py returns a non-2xx response.

    Wraps urllib's generic "HTTP Error 500: Internal Server Error" with the
    actual `{"error": ...}` detail host_bridge.py sent in its response body,
    which urllib discards by default (it only exposes that body via
    HTTPError.read(), and raises before the normal urlopen()-as-context-
    manager read() path ever runs).
    """


def _validate_bridge_url(bridge_url: str) -> None:
    """Reject non-HTTP bridge endpoints before passing them to urllib."""
    if urlparse(bridge_url).scheme not in ("http", "https"):
        raise ValueError("Bridge URL scheme must be http or https")


def _auth_headers(bridge_token: str) -> dict:
    """Build authorization headers for requests to the bridge service.

    Parameters:
        bridge_token (str): Bearer token used to authorize the request.

    Returns:
        dict: Headers containing the bearer authorization value.
    """
    return {"Authorization": f"Bearer {bridge_token}"}


def _post_json(url: str, data: bytes, headers: dict, timeout: int) -> dict:
    """POST to the bridge and return the parsed JSON response body.

    Raises:
        BridgeRequestError: On a non-2xx response, with host_bridge.py's
            error detail surfaced in the message instead of urllib's
            generic "HTTP Error <code>" text.
    """
    req = Request(url, data=data, method="POST", headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:  # nosec B310 - scheme validated above; nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read()
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                detail = parsed.get("error") or body.decode("utf-8", "ignore")
            else:
                detail = body.decode("utf-8", "ignore")
        except (json.JSONDecodeError, ValueError):
            detail = body.decode("utf-8", "ignore")
        raise BridgeRequestError(
            f"host_bridge request to {url} failed with HTTP {e.code}: {detail}"
        ) from e


def transcribe_audio(
    audio_path: Path, model_id: str, bridge_url: str, bridge_token: str
) -> tuple[str, str]:
    """
    Transcribe an audio file through the bridge service.

    Parameters:
        audio_path (Path): Path to the audio file to transcribe.
        model_id (str): Identifier of the transcription model to use.

    Returns:
        tuple[str, str]: The SRT-formatted transcription and plain-text transcription.
    """
    _validate_bridge_url(bridge_url)
    url = f"{bridge_url.rstrip('/')}/transcribe?model_id={model_id}"
    payload = _post_json(
        url, audio_path.read_bytes(), _auth_headers(bridge_token), timeout=1800
    )
    return payload["srt_text"], payload["plain_text"]
