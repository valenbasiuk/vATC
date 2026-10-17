"""Voices from engines other than Piper (improvement plan J1), behind the same voice bank: a `voices.Voice` with
engine "kokoro" or "edge" is spoken here, everything else stays Piper.

- Kokoro-82M (local, ONNX, Apache-2.0): `pip install kokoro-onnx`, then the model files in voices/kokoro/
  (kokoro-v1.0.onnx, voices-v1.0.bin: `python tools/voice_compare.py --download-kokoro`). Voices: am_michael,
  am_adam, bm_george, bm_lewis, af_heart, bf_emma ... (a = American, b = British; m / f), ef_dora / em_alex (Spanish).
- Microsoft Edge neural voices (cloud, free, no key, an unofficial endpoint that may stop working): `pip install
  edge-tts` (+ `pip install miniaudio` to decode its mp3). Voices: en-US-GuyNeural, en-GB-RyanNeural,
  en-AU-WilliamNeural, en-IE-ConnorNeural, en-IN-PrabhatNeural, es-AR-TomasNeural ... about 0.5 s more latency.

Nothing here is imported unless such a voice is used; a voice whose engine fails falls back to Piper (tts.py).
"""

from __future__ import annotations

import importlib.util
import threading
from functools import lru_cache
from pathlib import Path

KOKORO_DIR = Path("voices/kokoro")
KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
_lock = threading.Lock()


def available(engine: str) -> str | None:
    """None if `engine` can speak here, else why not (for the startup line)."""
    if engine == "piper":
        return None
    if engine == "kokoro":
        if importlib.util.find_spec("kokoro_onnx") is None:
            return "pip install kokoro-onnx"
        if not (KOKORO_DIR / KOKORO_MODEL).exists() or not (KOKORO_DIR / KOKORO_VOICES).exists():
            return "model files missing: python tools/voice_compare.py --download-kokoro"
        return None
    if engine == "edge":
        if importlib.util.find_spec("edge_tts") is None:
            return "pip install edge-tts"
        if importlib.util.find_spec("miniaudio") is None and importlib.util.find_spec("soundfile") is None:
            return "pip install miniaudio (to decode the mp3)"
        return None
    return f"unknown engine {engine!r}"


@lru_cache(maxsize=1)
def _kokoro():
    from kokoro_onnx import Kokoro  # type: ignore

    return Kokoro(str(KOKORO_DIR / KOKORO_MODEL), str(KOKORO_DIR / KOKORO_VOICES))


def _kokoro_lang(name: str) -> str:
    return {"a": "en-us", "b": "en-gb", "e": "es"}.get(name[:1], "en-us")


def _decode_mp3(data: bytes):
    """mp3 bytes -> (float32 mono samples, rate)."""
    import numpy as np  # type: ignore

    try:
        import miniaudio  # type: ignore

        d = miniaudio.decode(data, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1)
        return np.asarray(d.samples, dtype=np.float32), d.sample_rate
    except ImportError:
        import io

        import soundfile  # type: ignore

        samples, rate = soundfile.read(io.BytesIO(data), dtype="float32")
        if samples.ndim > 1:
            samples = samples.mean(axis=1)
        return samples.astype(np.float32), rate


def _edge(name: str, text: str, rate: float) -> bytes:
    import asyncio

    import edge_tts  # type: ignore

    pct = int(round((1.0 / max(0.5, rate) - 1.0) * 100))  # Piper's length scale -> Edge's "+10%"

    async def run() -> bytes:
        out = bytearray()
        async for chunk in edge_tts.Communicate(text, name, rate=f"{pct:+d}%").stream():
            if chunk.get("type") == "audio":
                out += chunk["data"]
        return bytes(out)

    return asyncio.run(run())


def synthesize(engine: str, name: str, text: str, rate: float = 1.0):
    """(float32 mono samples, sample rate) of `text` in voice `name` of `engine`; `rate` as Piper's length scale
    (< 1 faster). Raises if the engine can't speak (the caller falls back to Piper)."""
    import numpy as np  # type: ignore

    if engine == "kokoro":
        with _lock:  # one ONNX session, used by the chatter thread and the main loop
            samples, sr = _kokoro().create(text, voice=name, speed=1.0 / max(0.5, rate), lang=_kokoro_lang(name))
        return np.asarray(samples, dtype=np.float32), int(sr)
    if engine == "edge":
        return _decode_mp3(_edge(name, text, rate))
    raise ValueError(f"no engine {engine!r}")


def parse_spec(spec: str) -> tuple[str, str]:
    """'edge:en-US-GuyNeural' -> ('edge', 'en-US-GuyNeural'); a bare name is a Piper model."""
    engine, sep, name = spec.partition(":")
    if sep and engine in ("kokoro", "edge", "piper"):
        return engine, name
    return "piper", spec
