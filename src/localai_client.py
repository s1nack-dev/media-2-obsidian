"""Small standard-library client for LocalAI's OpenAI-compatible API."""

import json
import mimetypes
import re
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from core import srt_text_to_plain_text


MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class LocalAIRequestError(RuntimeError):
    """Raised when LocalAI cannot complete an API request."""


class _NoRedirect(HTTPRedirectHandler):
    """Refuse redirects so the bearer key is never replayed to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


# No proxy handler either: the API key should only ever reach LocalAI itself.
_OPENER = build_opener(ProxyHandler({}), _NoRedirect)


def _validate_url(base_url: str) -> None:
    if urlparse(base_url).scheme not in ("http", "https"):
        raise ValueError("LocalAI URL scheme must be http or https")


def _headers(api_key: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return body.decode("utf-8", "replace")[-1000:]


def _post(url: str, data: bytes, headers: dict[str, str], timeout: int) -> bytes:
    request = Request(url, data=data, method="POST", headers=headers)
    path = urlparse(url).path
    try:
        with _OPENER.open(request, timeout=timeout) as response:  # nosec B310 - URL scheme is validated by public callers
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise LocalAIRequestError(
            f"LocalAI {path} failed with HTTP {exc.code}: {_error_detail(exc.read())[:200]}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise LocalAIRequestError(f"LocalAI {path} unreachable: {exc}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise LocalAIRequestError(
            f"LocalAI {path} response exceeds {MAX_RESPONSE_BYTES} bytes"
        )
    return body


def _multipart_audio(audio_path: Path, model: str) -> tuple[bytes, str]:
    """Build the narrow multipart request LocalAI's transcription endpoint needs."""
    boundary = f"----localai-{uuid.uuid4().hex}"
    safe_name = re.sub(r'[\x00-\x1f"\\]', "_", audio_path.name)
    content_type = (
        mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
    )
    fields = [
        ("model", model.encode()),
        ("response_format", b"srt"),
    ]
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value,
                b"\r\n",
            )
        )
    chunks.extend(
        (
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            audio_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return b"".join(chunks), boundary


def transcribe_audio(
    audio_path: Path, model: str, base_url: str, api_key: str | None = None
) -> tuple[str, str]:
    """Transcribe audio with LocalAI and return its SRT plus plain-text form."""
    _validate_url(base_url)
    payload, boundary = _multipart_audio(audio_path, model)
    headers = _headers(api_key)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    srt_text = _post(
        f"{base_url.rstrip('/')}/v1/audio/transcriptions", payload, headers, 1800
    ).decode("utf-8")
    return srt_text, srt_text_to_plain_text(srt_text)


def chat_completion(
    prompt: str, model: str, base_url: str, api_key: str | None = None
) -> str:
    """Submit one non-streaming prompt to LocalAI's chat-completions endpoint."""
    _validate_url(base_url)
    headers = _headers(api_key)
    headers["Content-Type"] = "application/json"
    payload = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}]}
    ).encode()
    try:
        response = json.loads(
            _post(f"{base_url.rstrip('/')}/v1/chat/completions", payload, headers, 600)
        )
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise LocalAIRequestError(
            "LocalAI chat response did not contain choices[0].message.content"
        ) from exc
    if not isinstance(content, str):
        raise LocalAIRequestError("LocalAI chat response content was not text")
    return content.strip()
