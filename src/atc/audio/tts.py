"""Text-to-speech. Default is PrintTTS (no audio). PiperTTS uses the Piper Python API.

Piper moved to github.com/OHF-Voice/piper1-gpl (GPL-3.0; fine for personal use, but keep it in
mind if this is ever shared). Install: `pip install piper-tts`. Download a voice:
    python -m piper.download_voices en_US-lessac-medium --data-dir voices
which gives voices/en_US-lessac-medium.onnx (+ .json).

STATUS: synthesis verified on Valen's PC (2026-10-05, Piper 1.8); sentence-by-sentence playback through
sd.OutputStream not heard yet. The voice is loaded once and kept in memory (no process spawn per reply).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol


class Speaker(Protocol):
    def say(self, text: str) -> None: ...


class PrintTTS:
    def say(self, text: str) -> None:
        print(f"ATC> {text}")

    def say_as(self, text: str, who: str = "ATC", voice: int | None = None) -> None:
        """Radio traffic: ATC to an AI aircraft, or that aircraft's readback."""
        print(f"{'ATC' if who == 'ATC' else who}> {text}")


class PiperTTS:
    def __init__(
        self,
        model_path: Path,
        radio_fx: bool = True,
        length_scale: float = 1.0,
        lead_pad_s: float = 0.15,
        tail_pad_s: float = 0.5,
    ) -> None:
        from piper import PiperVoice  # type: ignore

        self._voice = PiperVoice.load(str(model_path))
        self.radio_fx = radio_fx
        self.lead_pad_s = lead_pad_s
        self.tail_pad_s = tail_pad_s
        self.last_synth_s: float | None = None
        self.length_scale = length_scale  # < 1.0 speaks faster (controllers talk fast)

    def _chunks(self, text: str, voice: int | None = None):
        """(float32 samples, rate) per sentence, as Piper produces them. `voice`: speaker id (multi-speaker
        models like libritts: 904 voices); None = the model's default, used for ATC."""
        import numpy as np  # type: ignore

        kwargs = {}
        try:
            from piper import SynthesisConfig  # type: ignore

            kwargs["syn_config"] = SynthesisConfig(length_scale=self.length_scale, speaker_id=voice)
        except (ImportError, TypeError):  # older/newer layout: fall back to defaults
            pass
        for chunk in self._voice.synthesize(text, **kwargs):
            yield np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16).astype(np.float32) / 32768.0, chunk.sample_rate

    def synthesize(self, text: str):
        """Returns (float32 mono samples, sample_rate) for the whole text."""
        import numpy as np  # type: ignore

        parts, rate = [], 22050
        for samples, rate in self._chunks(text):
            parts.append(samples)
        return (np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)), rate

    def say(self, text: str) -> None:
        self.say_as(text)

    def say_as(self, text: str, who: str = "ATC", voice: int | None = None) -> None:
        """Stream sentence by sentence: the first sentence plays while the next ones are synthesized."""
        import numpy as np  # type: ignore
        import sounddevice as sd  # type: ignore

        from atc.audio.radio_fx import apply_radio_fx

        print(f"{who}> {text}")
        t0 = time.perf_counter()
        stream = None
        try:
            for samples, rate in self._chunks(text, voice):
                if len(samples) == 0:
                    continue
                if self.radio_fx:
                    samples = apply_radio_fx(samples, rate)
                if stream is None:
                    self.last_synth_s = time.perf_counter() - t0  # delay before audio starts
                    stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32")
                    stream.start()
                    # Lead silence: the output device often clips the first word.
                    stream.write(np.zeros(int(rate * self.lead_pad_s), dtype=np.float32))
                stream.write(samples.astype(np.float32))
            if stream is not None:
                stream.write(np.zeros(int(rate * self.tail_pad_s), dtype=np.float32))  # ...and the last ~300 ms
        finally:
            if stream is not None:
                stream.stop()
                stream.close()
