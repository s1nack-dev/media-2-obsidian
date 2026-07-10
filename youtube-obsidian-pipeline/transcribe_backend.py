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
    """
    Load and cache a Parakeet model for reuse within the process.
    
    Parameters:
        model_id (str): Identifier of the Parakeet model to load.
    
    Returns:
        BaseParakeet: The cached or newly loaded model.
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
        lines.append(f"{_format_timestamp(sentence.start)} --> {_format_timestamp(sentence.end)}")
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
    result = model.transcribe(str(audio_path), chunk_duration=120.0, overlap_duration=15.0)
    srt_text = _sentences_to_srt(result.sentences)
    return srt_text, result.text
