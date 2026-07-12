import importlib.util
import sys
import types
from pathlib import Path

try:
    import transcribe_backend as tb

    if not hasattr(tb, "_MODEL_CACHE"):
        raise ImportError
except (ModuleNotFoundError, ImportError):
    hf = types.ModuleType("huggingface_hub.constants")
    hf.HF_HUB_OFFLINE = False
    parakeet = types.ModuleType("parakeet_mlx")
    parakeet.from_pretrained = lambda model: object()
    alignment = types.ModuleType("parakeet_mlx.alignment")
    alignment.AlignedSentence = object
    para = types.ModuleType("parakeet_mlx.parakeet")
    para.BaseParakeet = object
    sys.modules.update(
        {
            "huggingface_hub": types.ModuleType("huggingface_hub"),
            "huggingface_hub.constants": hf,
            "parakeet_mlx": parakeet,
            "parakeet_mlx.alignment": alignment,
            "parakeet_mlx.parakeet": para,
        }
    )
    spec = importlib.util.spec_from_file_location(
        "transcribe_backend", Path(__file__).parents[1] / "transcribe_backend.py"
    )
    tb = importlib.util.module_from_spec(spec)
    sys.modules["transcribe_backend"] = tb
    spec.loader.exec_module(tb)


def test_format_timestamp():
    assert tb._format_timestamp(1.234) == "00:00:01,234"


def test_sentences_to_srt():
    class S:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    assert tb._sentences_to_srt([S(0, 1.2, " Hello ")]).startswith(
        "1\n00:00:00,000 --> 00:00:01,200\nHello"
    )


def test_model_cache_and_transcribe(monkeypatch, tmp_path):
    class Model:
        def transcribe(self, path, **kwargs):
            return types.SimpleNamespace(
                sentences=[types.SimpleNamespace(start=0, end=1, text="Hi")], text="Hi"
            )

    model = Model()
    monkeypatch.setattr(tb.parakeet_mlx, "from_pretrained", lambda model_id: model)
    tb._MODEL_CACHE.clear()
    assert tb.get_model("model") is tb.get_model("model")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    assert tb.transcribe_audio(audio, "model")[1] == "Hi"
