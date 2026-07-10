"""
Local speech-to-text (Parakeet via MLX - Apple Silicon only). This module
is host-only: it imports parakeet_mlx/mlx, which have no Linux wheels and
cannot run in a container. Used exclusively by host_bridge.py, which
exposes it over HTTP for the containerized pipeline to call.

Transcription itself always runs locally via MLX (Apple Neural Engine/GPU)
- no audio ever leaves the machine. The only network dependency is
Hugging Face Hub, used to download model weights the first time a given
model is used; see get_model() for how that's limited to a one-time
download.
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
    """Cached per model_id so a single process handling multiple items
    doesn't reload the model from disk for every video.

    parakeet-mlx loads weights via huggingface_hub, which by default
    checks Hugging Face Hub for a newer revision on every load, even when
    already cached locally. Since model_id is a fixed config value, that
    check is pure overhead (and an unwanted network call) for anything
    past the first run - so we force offline mode for the load attempt
    and only fall back to an actual download the first time a given
    model_id hasn't been cached yet.

    Note: huggingface_hub reads the HF_HUB_OFFLINE env var into a
    module-level constant once, at import time - setting os.environ at
    runtime has no effect on an already-imported huggingface_hub. We have
    to flip the huggingface_hub.constants.HF_HUB_OFFLINE attribute
    directly, since that's what is_offline_mode() actually reads.
    """
    if model_id not in _MODEL_CACHE:
        hf_constants.HF_HUB_OFFLINE = True
        try:
            _MODEL_CACHE[model_id] = parakeet_mlx.from_pretrained(model_id)
        except Exception:
            log.info("Parakeet model %r not cached yet - downloading once from Hugging Face.", model_id)
            hf_constants.HF_HUB_OFFLINE = False
            _MODEL_CACHE[model_id] = parakeet_mlx.from_pretrained(model_id)
        finally:
            hf_constants.HF_HUB_OFFLINE = False
    return _MODEL_CACHE[model_id]


def _format_timestamp(seconds: float) -> str:
    total_ms = int(timedelta(seconds=seconds).total_seconds() * 1000)
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _sentences_to_srt(sentences: list[AlignedSentence]) -> str:
    lines = []
    for i, sentence in enumerate(sentences, start=1):
        lines.append(str(i))
        lines.append(f"{_format_timestamp(sentence.start)} --> {_format_timestamp(sentence.end)}")
        lines.append(sentence.text.strip())
        lines.append("")
    return "\n".join(lines)


def transcribe_audio(audio_path: Path, model_id: str) -> tuple[str, str]:
    """Transcribes an audio/video file with Parakeet.

    Chunked in 120s windows (15s overlap, matching parakeet-mlx's own CLI
    defaults) - without this, transcribe() tries to run the whole file
    through in one pass, which is fine for short clips but blows up MLX's
    Metal buffer limit on anything more than a few minutes long (podcasts,
    long videos).

    Returns (srt_text, plain_text). Raises on failure (caller catches).
    """
    model = get_model(model_id)
    result = model.transcribe(str(audio_path), chunk_duration=120.0, overlap_duration=15.0)
    srt_text = _sentences_to_srt(result.sentences)
    return srt_text, result.text
