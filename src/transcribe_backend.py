"""
Local speech-to-text (Parakeet via MLX - Apple Silicon only). This module
is host-only: it imports parakeet_mlx/mlx, which have no Linux wheels and
cannot run in a container. Used exclusively by host_bridge.py, which
exposes it over HTTP for the containerized pipeline to call.

Transcription itself always runs locally via MLX (Apple Neural Engine/GPU)
- no audio ever leaves the machine. Production loading is offline-only:
models must be fetched in a controlled maintenance step with
``preload_model()`` before the bridge starts.
"""

import logging
from datetime import timedelta
from pathlib import Path

import huggingface_hub.constants as hf_constants
import parakeet_mlx
from parakeet_mlx.alignment import AlignedSentence
from parakeet_mlx.parakeet import BaseParakeet

log = logging.getLogger("host_bridge")

_MODEL_CACHE: dict[str, BaseParakeet] = {}


def get_model(model_id: str) -> BaseParakeet:
    """
    Load and cache a Parakeet model for reuse within the process.

    Parameters:
        model_id (str): Identifier of the Parakeet model to load.

    Returns:
        BaseParakeet: The cached or newly loaded model.
    """
    if model_id not in _MODEL_CACHE:
        previous_offline = hf_constants.HF_HUB_OFFLINE
        hf_constants.HF_HUB_OFFLINE = True
        try:
            _MODEL_CACHE[model_id] = parakeet_mlx.from_pretrained(model_id)
        except Exception as exc:
            raise RuntimeError(
                f"Parakeet model {model_id!r} is not available in the local cache. "
                "Run the controlled preload_transcription_model.py maintenance step first."
            ) from exc
        finally:
            hf_constants.HF_HUB_OFFLINE = previous_offline
    return _MODEL_CACHE[model_id]


def preload_model(model_id: str) -> BaseParakeet:
    """Download and load a model during an explicit maintenance window.

    This is the only code path that permits Hugging Face network access. The
    long-running, sandboxed bridge uses :func:`get_model` and cannot download
    a model opportunistically.
    """
    previous_offline = hf_constants.HF_HUB_OFFLINE
    hf_constants.HF_HUB_OFFLINE = False
    try:
        model = parakeet_mlx.from_pretrained(model_id)
        _MODEL_CACHE[model_id] = model
        return model
    finally:
        hf_constants.HF_HUB_OFFLINE = previous_offline


def _format_timestamp(seconds: float) -> str:
    """
    Format a duration in seconds as an SRT timestamp.

    Parameters:
        seconds (float): Duration in seconds.

    Returns:
        str: Timestamp in `HH:MM:SS,mmm` format.
    """
    total_ms = int(timedelta(seconds=seconds).total_seconds() * 1000)
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _sentences_to_srt(sentences: list[AlignedSentence]) -> str:
    """
    Convert aligned sentences into an SRT-formatted subtitle document.

    Parameters:
        sentences (list[AlignedSentence]): Sentences with start and end timestamps and subtitle text.

    Returns:
        str: The formatted SRT subtitle document.
    """
    lines = []
    for i, sentence in enumerate(sentences, start=1):
        lines.append(str(i))
        lines.append(
            f"{_format_timestamp(sentence.start)} --> {_format_timestamp(sentence.end)}"
        )
        lines.append(sentence.text.strip())
        lines.append("")
    return "\n".join(lines)


def transcribe_audio(audio_path: Path, model_id: str) -> tuple[str, str]:
    """
    Transcribe an audio or video file and produce subtitle and plain-text output.

    Parameters:
        audio_path (Path): Path to the audio or video file.
        model_id (str): Identifier of the Parakeet model to use.

    Returns:
        tuple[str, str]: The SRT-formatted subtitles and plain transcription text.
    """
    model = get_model(model_id)
    result = model.transcribe(
        str(audio_path), chunk_duration=120.0, overlap_duration=15.0
    )
    srt_text = _sentences_to_srt(result.sentences)
    return srt_text, result.text
