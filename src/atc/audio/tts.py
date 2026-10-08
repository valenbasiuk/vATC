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
import zlib
from pathlib import Path
from typing import Protocol


class Speaker(Protocol):
    def say(self, text: str) -> None: ...


def shift_pitch(samples, pitch: float):
    """Pitch (and formants) times `pitch` by resampling: the result is 1/pitch as long, so the voice is synthesized
    `pitch` times slower first (voices.PITCHES: the same Piper speaker sounds like another person)."""
    if pitch == 1.0 or len(samples) < 2:
        return samples
    import numpy as np  # type: ignore

    n = int(len(samples) / pitch)
    return np.interp(np.arange(n) * pitch, np.arange(len(samples)), samples).astype(np.float32)


class PrintTTS:
    def say(self, text: str) -> None:
        print(f"ATC> {text}")

    def say_as(self, text: str, who: str = "ATC", voice: int | None = None) -> None:
        """Radio traffic: ATC to an AI aircraft, or that aircraft's readback."""
        print(f"{'ATC' if who == 'ATC' else who}> {text}")


class PiperTTS:
    audio = True  # real sound: loops (ATIS) may repeat without flooding a console

    def __init__(
        self,
        model_path: Path,
        radio_fx: bool = True,
        length_scale: float = 1.0,
        lead_pad_s: float = 0.15,
        tail_pad_s: float = 0.5,
        device=None,
        bank=None,
    ) -> None:
        from piper import PiperVoice  # type: ignore

        self.device = device  # sounddevice output (index or name part); None = Windows default
        self._voice = PiperVoice.load(str(model_path))
        self._models = {str(Path(model_path)): self._voice}  # every voice model loaded so far
        self.bank = bank  # atc.voices.VoiceBank: accents per pilot / controller (None = one model for all)
        self.radio_fx = radio_fx
        self.lead_pad_s = lead_pad_s
        self.tail_pad_s = tail_pad_s
        self.last_synth_s: float | None = None
        self.length_scale = length_scale  # < 1.0 speaks faster (controllers talk fast)

    def _model(self, path: str | None):
        """A loaded PiperVoice (loaded once, kept: the next transmission in that voice starts right away)."""
        if not path:
            return self._voice
        key = str(Path(path))
        if key not in self._models:
            from piper import PiperVoice  # type: ignore

            try:
                self._models[key] = PiperVoice.load(key)
            except Exception as exc:  # noqa: BLE001 - a broken download must not silence the radio
                print(f"[voice {key} failed to load: {exc}]")
                self._models[key] = self._voice
        return self._models[key]

    def preload(self, paths) -> None:
        for p in paths:
            self._model(p)

    def _chunks(self, text: str, voice=None):
        """(float32 samples, rate) per sentence, as Piper produces them. `voice`: a voices.Voice (model, speaker,
        rate), a speaker id of the main model (libritts: 904 voices), or None = the main model's default."""
        import numpy as np  # type: ignore

        model, speaker, rate = self._voice, voice, self.length_scale
        pitch = 1.0
        if voice is not None and not isinstance(voice, int):
            model, speaker, rate = self._model(voice.model), voice.speaker, voice.rate * self.length_scale
            pitch = getattr(voice, "pitch", 1.0) or 1.0
            rate *= pitch  # synthesized that much slower: the pitch shift below brings the length back
        kwargs = {}
        try:
            from piper import SynthesisConfig  # type: ignore

            kwargs["syn_config"] = SynthesisConfig(length_scale=rate, speaker_id=speaker)
        except (ImportError, TypeError):  # older/newer layout: fall back to defaults
            pass
        for chunk in model.synthesize(text, **kwargs):
            samples = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            yield shift_pitch(samples, pitch), chunk.sample_rate

    def synthesize(self, text: str, voice=None):
        """Returns (float32 mono samples, sample_rate) for the whole text."""
        import numpy as np  # type: ignore

        parts, rate = [], 22050
        for samples, rate in self._chunks(text, voice):
            parts.append(samples)
        return (np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)), rate

    def say(self, text: str) -> None:
        self.say_as(text)

    def say_as(self, text: str, who: str = "ATC", voice=None) -> None:
        """Stream sentence by sentence: the first sentence plays while the next ones are synthesized."""
        import numpy as np  # type: ignore
        import sounddevice as sd  # type: ignore

        from atc.audio.radio_fx import apply_radio_fx, squelch_tail

        print(f"{who}> {text}")
        t0 = time.perf_counter()
        stream = None
        # every station sounds a little different on the radio (its own hiss), the same each time
        hiss = 0.004 + (zlib.crc32(str(getattr(voice, "label", voice)).encode()) % 5) * 0.002
        try:
            for samples, rate in self._chunks(text, voice):
                if len(samples) == 0:
                    continue
                if self.radio_fx:
                    samples = apply_radio_fx(samples, rate, noise=hiss)
                if stream is None:
                    self.last_synth_s = time.perf_counter() - t0  # delay before audio starts
                    stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32", device=self.device)
                    stream.start()
                    # Lead silence: the output device often clips the first word.
                    stream.write(np.zeros(int(rate * self.lead_pad_s), dtype=np.float32))
                stream.write(samples.astype(np.float32))
            if stream is not None:
                if self.radio_fx:
                    stream.write(squelch_tail(rate))  # the "kssh" when the other station unkeys
                stream.write(np.zeros(int(rate * self.tail_pad_s), dtype=np.float32))
        finally:
            if stream is not None:
                stream.stop()
                stream.close()
