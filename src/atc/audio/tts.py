"""Text-to-speech. Default is PrintTTS (no audio). PiperTTS uses the Piper Python API.

Piper moved to github.com/OHF-Voice/piper1-gpl (GPL-3.0; fine for personal use, but keep it in
mind if this is ever shared). Install: `pip install piper-tts`. Download a voice:
    python -m piper.download_voices en_US-lessac-medium --data-dir voices
which gives voices/en_US-lessac-medium.onnx (+ .json).

STATUS: written from the project's docs (docs/API_PYTHON.md), NOT run. The voice is loaded once and
kept in memory, which matters for latency (no process spawn per reply).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Speaker(Protocol):
    def say(self, text: str) -> None: ...


class PrintTTS:
    def say(self, text: str) -> None:
        print(f"ATC> {text}")


class PiperTTS:
    def __init__(self, model_path: Path, radio_fx: bool = True, length_scale: float = 1.0) -> None:
        from piper import PiperVoice  # type: ignore

        self._voice = PiperVoice.load(str(model_path))
        self.radio_fx = radio_fx
        self.length_scale = length_scale  # < 1.0 speaks faster (controllers talk fast)

    def synthesize(self, text: str):
        """Returns (float32 mono samples, sample_rate)."""
        import numpy as np  # type: ignore

        kwargs = {}
        try:
            from piper import SynthesisConfig  # type: ignore

            kwargs["syn_config"] = SynthesisConfig(length_scale=self.length_scale)
        except ImportError:  # older/newer layout: fall back to defaults
            pass

        raw = bytearray()
        rate = 22050
        for chunk in self._voice.synthesize(text, **kwargs):
            rate = chunk.sample_rate
            raw += chunk.audio_int16_bytes
        samples = np.frombuffer(bytes(raw), dtype=np.int16).astype(np.float32) / 32768.0
        return samples, rate

    def say(self, text: str) -> None:
        import sounddevice as sd  # type: ignore

        from atc.audio.radio_fx import apply_radio_fx

        print(f"ATC> {text}")
        samples, rate = self.synthesize(text)
        if len(samples) == 0:
            return
        if self.radio_fx:
            samples = apply_radio_fx(samples, rate)
        sd.play(samples, rate)
        sd.wait()
