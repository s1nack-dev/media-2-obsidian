"""
Container-safe HTTP client for host_bridge.py - the host-native service
exposing transcription (Parakeet/MLX) and summarization/tagging (Claude
CLI, subscription auth), neither of which can run inside a Linux
container on this Mac. Used by pipeline.py; has no MLX/parakeet or
claude-CLI dependency itself, just urllib.
"""

import json
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

_JSON_HEADERS = {"Content-Type": "application/json"}


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
    req = Request(
        url,
        data=audio_path.read_bytes(),
        method="POST",
        headers=_auth_headers(bridge_token),
    )
    with urlopen(req, timeout=1800) as resp:  # nosec B310 - scheme validated above; nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        payload = json.loads(resp.read())
    return payload["srt_text"], payload["plain_text"]


def summarize(transcript_text: str, bridge_url: str, bridge_token: str) -> str:
    """
    Generate a summary of the transcript using the bridge service.

    Parameters:
        transcript_text (str): Transcript text to summarize.
        bridge_url (str): Base URL of the bridge service.
        bridge_token (str): Bearer token for authenticating the request.

    Returns:
        str: Generated transcript summary.
    """
    _validate_bridge_url(bridge_url)
    url = f"{bridge_url.rstrip('/')}/summarize"
    body = json.dumps({"transcript": transcript_text}).encode()
    headers = {**_JSON_HEADERS, **_auth_headers(bridge_token)}
    req = Request(url, data=body, method="POST", headers=headers)
    with urlopen(req, timeout=600) as resp:  # nosec B310 - scheme validated above; nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        payload = json.loads(resp.read())
    return payload["summary"]


def generate_tags(
    transcript_text: str, bridge_url: str, bridge_token: str
) -> list[str]:
    """
    Generate tags from transcript text using the bridge service.

    Parameters:
        transcript_text (str): Transcript text to analyze.

    Returns:
        list[str]: Tags generated from the transcript.
    """
    _validate_bridge_url(bridge_url)
    url = f"{bridge_url.rstrip('/')}/tags"
    body = json.dumps({"transcript": transcript_text}).encode()
    headers = {**_JSON_HEADERS, **_auth_headers(bridge_token)}
    req = Request(url, data=body, method="POST", headers=headers)
    with urlopen(req, timeout=600) as resp:  # nosec B310 - scheme validated above; nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        payload = json.loads(resp.read())
    return payload["tags"]
