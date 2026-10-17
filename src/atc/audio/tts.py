"""Text-to-speech. Default is PrintTTS (no audio). PiperTTS uses the Piper Python API.

Piper moved to github.com/OHF-Voice/piper1-gpl (GPL-3.0; fine for personal use, but keep it in
mind if this is ever shared). Install: `pip install piper-tts`. Download a voice:
    python -m piper.download_voices en_US-lessac-medium --data-dir voices
which gives voices/en_US-lessac-medium.onnx (+ .json).

STATUS: synthesis verified on Valen's PC (2026-10-05, Piper 1.8); sentence-by-sentence playback through
sd.OutputStream not heard yet. The voice is loaded once and kept in memory (no process spawn per reply).

Kokoro (2026-10-15, `pip install kokoro-onnx`, models from `python tools/download_voices.py --kokoro`): voices.py
hands out Kokoro voices (Voice.style) and this class speaks them with KokoroEngine. Measured on a Ryzen 9 laptop
CPU with 4 threads: ~0.3 s of synthesis per second of speech; the first clause (the callsign) goes out alone so
the audio starts in a few tenths of a second, the rest is synthesized while it plays (say_as runs synthesis in a
thread ahead of playback). ATC_KOKORO_THREADS (default 4) keeps it from taking every core from MSFS.
"""

from __future__ import annotations

import collections
import os
import queue
import re
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Protocol

KOKORO_THREADS = int(os.environ.get("ATC_KOKORO_THREADS", "4"))
KOKORO_SPEED = 1.05  # Kokoro's own pace is a little slow for the radio
KOKORO_FIRST_PART_MAX = 48  # the first clause goes out alone when it is this short (the callsign: quick start)
KOKORO_PART_MIN = 70  # later clauses are grouped up to about this many characters (better intonation)


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


def _espeak_config():
    """espeak-ng for Kokoro's phonemes: ATC_ESPEAK_LIB (+ ATC_ESPEAK_DATA), else on Linux the system library (the
    espeakng-loader wheel there looks for its data in a build path and exits), else the bundled one (Windows)."""
    from kokoro_onnx import EspeakConfig  # type: ignore

    lib = os.environ.get("ATC_ESPEAK_LIB")
    if lib:
        return EspeakConfig(lib_path=lib, data_path=os.environ.get("ATC_ESPEAK_DATA"))
    if sys.platform.startswith("linux"):
        import ctypes.util

        found = ctypes.util.find_library("espeak-ng")
        data = next((d for d in ("/usr/share/espeak-ng-data", "/usr/lib/x86_64-linux-gnu/espeak-ng-data")
                     if Path(d).is_dir()), None)
        if found and data:
            return EspeakConfig(lib_path=found, data_path=data)
    return None


def kokoro_parts(text: str) -> list[tuple[str, float]]:
    """(text, pause after it in seconds) to synthesize one after the other: the first clause alone when short (the
    callsign: audio starts sooner), then clauses grouped up to KOKORO_PART_MIN characters, sentences kept apart."""
    out: list[tuple[str, float]] = []
    for sentence in re.findall(r"[^.!?]+[.!?]*", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        clauses = [c.strip() for c in re.split(r"(?<=,)\s+", sentence) if c.strip()]
        cur = ""
        for i, c in enumerate(clauses):
            if not out and not cur and i == 0 and len(clauses) > 1 and len(c) <= KOKORO_FIRST_PART_MAX:
                out.append((c, 0.08))
                continue
            cur = f"{cur} {c}".strip()
            if len(cur) >= KOKORO_PART_MIN and i < len(clauses) - 1:
                out.append((cur, 0.08))
                cur = ""
        if cur:
            out.append((cur, 0.22))
    return out


class KokoroEngine:
    """Kokoro-82M through onnxruntime (kokoro-onnx), CPU only, a few threads. Voice styles and blends cached."""

    def __init__(self, model_path: Path | str, voices_bin: Path | str | None = None,
                 threads: int = KOKORO_THREADS) -> None:
        import onnxruntime as rt  # type: ignore
        from kokoro_onnx import Kokoro  # type: ignore

        model_path = Path(model_path)
        voices_bin = Path(voices_bin) if voices_bin else model_path.with_name("voices-v1.0.bin")
        so = rt.SessionOptions()
        so.intra_op_num_threads = max(1, threads)
        so.inter_op_num_threads = 1
        sess = rt.InferenceSession(str(model_path), so, providers=["CPUExecutionProvider"])
        self.k = Kokoro.from_session(sess, str(voices_bin), espeak_config=_espeak_config())
        self._styles: dict[str, object] = {}
        self._lock = threading.Lock()  # one synthesis at a time (onnxruntime session + espeak are shared)

    def style(self, spec: str):
        """'am_michael' or a blend 'am_michael+am_puck@0.6' (60 % the first) as the style array Kokoro takes."""
        if spec not in self._styles:
            m = re.fullmatch(r"(\w+)\+(\w+)@([\d.]+)", spec)
            if m:
                a, b, w = self.k.get_voice_style(m.group(1)), self.k.get_voice_style(m.group(2)), float(m.group(3))
                self._styles[spec] = (a * w + b * (1.0 - w)).astype(a.dtype)
            else:
                self._styles[spec] = self.k.get_voice_style(spec)
        return self._styles[spec]

    def chunks(self, text: str, style: str, speed: float = 1.0):
        """(float32 samples, rate) per part of `text` (see kokoro_parts), a short silence after each."""
        import numpy as np  # type: ignore

        st = self.style(style)
        speed = min(2.0, max(0.5, speed * KOKORO_SPEED))
        for part, pause in kokoro_parts(text):
            with self._lock:
                audio, rate = self.k.create(part, st, speed=speed, lang="en-us")
            yield np.concatenate([np.asarray(audio, dtype=np.float32),
                                  np.zeros(int(rate * pause), dtype=np.float32)]), rate


class PrintTTS:
    def say(self, text: str) -> None:
        print(f"ATC> {text}")

    def say_as(self, text: str, who: str = "ATC", voice: int | None = None) -> None:
        """Radio traffic: ATC to an AI aircraft, or that aircraft's readback."""
        print(f"{'ATC' if who == 'ATC' else who}> {text}")


AIR_LOG_S = 60.0


def blocked_by(air_log, on_air, start: float | None, end: float | None, min_s: float = 0.4) -> str | None:
    """Who the user's transmission (from `start` to `end`, time.monotonic()) went out on top of (improvement plan G2):
    "pilot" if another aircraft was transmitting for at least `min_s` of it (two carriers: ATC hears a squeal),
    "ATC" if the controller was (a controller transmitting hears nobody), else None. The ATIS is on another
    frequency and never blocks. `air_log`: (start, end, who) of past transmissions; `on_air`: (who, start) now."""
    if start is None or end is None:
        return None
    spans = list(air_log) + ([(on_air[1], end, on_air[0])] if on_air else [])
    hit = None
    for a, b, who in spans:
        if who == "ATIS" or min(b, end) - max(a, start) < min_s:
            continue
        if who != "ATC":
            return "pilot"
        hit = "ATC"
    return hit


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
        self._kokoro: KokoroEngine | None = None
        self._kokoro_failed = False
        self._broken: set[str] = set()  # voice engines that failed this session (Piper takes over)
        self.on_air: tuple[str, float] | None = None  # (who, since) while a transmission plays
        self.air_log: list[tuple[float, float, str]] = []  # (start, end, who) of the last minute's transmissions

    def kokoro(self, model_path: str) -> KokoroEngine | None:
        """The Kokoro engine (loaded once). None if it can't load: those voices then fall back to Piper's."""
        if self._kokoro is None and not self._kokoro_failed:
            try:
                self._kokoro = KokoroEngine(model_path)
            except Exception as exc:  # noqa: BLE001 - a missing package or broken download must not silence the radio
                print(f"[Kokoro voices off: {exc}  (pip install kokoro-onnx; python tools/download_voices.py --kokoro)]")
                self._kokoro_failed = True
        return self._kokoro

    def _model(self, path: str | None):
        """A loaded PiperVoice (loaded once, kept: the next transmission in that voice starts right away)."""
        if not path:
            return self._voice
        if Path(path).name.startswith("kokoro"):  # the Kokoro model, not a Piper voice
            self.kokoro(path)
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
        if getattr(voice, "style", "") and self.kokoro(voice.model) is not None:
            pitch = getattr(voice, "pitch", 1.0) or 1.0
            speed = 1.0 / max(0.5, voice.rate * self.length_scale * pitch)
            for samples, sr in self._kokoro.chunks(text, voice.style, speed):
                yield shift_pitch(samples, pitch), sr
            return
        if voice is not None and not isinstance(voice, int):
            pitch = getattr(voice, "pitch", 1.0) or 1.0
            engine = getattr(voice, "engine", "piper")
            if engine != "piper" and engine not in self._broken:
                yield from self._engine_chunks(voice, text, pitch)
                return
            if engine != "piper":  # that engine failed before: this voice's place is taken by the main Piper voice
                speaker, pitch = None, 1.0
            else:
                model, speaker, rate = self._model(voice.model), voice.speaker, voice.rate * self.length_scale
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

    def _engine_chunks(self, voice, text: str, pitch: float):
        """A Kokoro / Edge voice (audio/engines.py), sentence by sentence; if the engine fails (not installed,
        offline), the rest is said in the main Piper voice and that engine isn't tried again this session."""
        import re

        from atc.audio import engines

        rate = voice.rate * self.length_scale * pitch
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
        for i, sentence in enumerate(sentences):
            try:
                samples, sr = engines.synthesize(voice.engine, voice.name, sentence, rate)
            except Exception as exc:  # noqa: BLE001 - never lose a transmission over a voice engine
                print(f"[voice {voice.key} failed ({str(exc)[:120]}): Piper from now on]")
                self._broken.add(voice.engine)
                yield from self._chunks(" ".join(sentences[i:]), None)
                return
            yield shift_pitch(samples, pitch), sr

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
        out_rate = None
        feed = _Feed()
        try:
            for samples, rate in _ahead(self._chunks(text, voice)):
                if len(samples) == 0:
                    continue
                if self.radio_fx:
                    samples = apply_radio_fx(samples, rate, noise=hiss)
                if stream is None:
                    self.last_synth_s = time.perf_counter() - t0  # delay before audio starts
                    out_rate = _device_rate(sd, self.device) or rate  # the device's own rate (we resample)
                    # Lead silence: the output device often clips the first word.
                    feed.put(np.zeros(int(out_rate * self.lead_pad_s), dtype=np.float32))
                    feed.put(_resample(samples, rate, out_rate))
                    # Callback mode: the sound card pulls the audio as it plays. Blocking writes returned at once
                    # on Valen's Samsung earphones (DirectSound, PortAudio 19.7) and stop() then cut everything:
                    # every ATC call was silent (2026-10-17)
                    stream = sd.OutputStream(samplerate=out_rate, channels=1, dtype="float32", device=self.device,
                                             callback=feed.callback, finished_callback=feed.done.set)
                    stream.start()
                    self.on_air = (who, time.monotonic())
                    continue
                feed.put(_resample(samples, rate, out_rate))
            if stream is not None:
                if self.radio_fx:
                    feed.put(_resample(squelch_tail(rate), rate, out_rate))  # the "kssh" when the other unkeys
                feed.put(np.zeros(int(out_rate * self.tail_pad_s), dtype=np.float32))
                feed.close()
                feed.done.wait(feed.seconds(out_rate) + 5.0)  # until the card has played it all
        finally:
            if stream is not None:
                feed.close()
                stream.stop()
                stream.close()
            if self.on_air is not None:
                now = time.monotonic()
                self.air_log = [x for x in self.air_log if now - x[1] <= AIR_LOG_S] + \
                    [(self.on_air[1], now, self.on_air[0])]
                self.on_air = None


class _Feed:
    """Audio handed to a callback OutputStream: put() parts as they are synthesized, close() after the last one; the
    callback plays silence while the next part is late and stops the stream once everything has been played."""

    def __init__(self) -> None:
        self._parts: collections.deque = collections.deque()
        self._pos = 0  # samples of the first part already played
        self._closed = False
        self._lock = threading.Lock()
        self._total = 0
        self.done = threading.Event()

    def put(self, samples) -> None:
        with self._lock:
            self._parts.append(samples)
            self._total += len(samples)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def seconds(self, rate: int) -> float:
        return self._total / max(rate, 1)

    def callback(self, outdata, frames, _time, _status) -> None:
        import sounddevice as sd  # type: ignore

        out = outdata[:, 0]
        n = 0
        with self._lock:
            while n < frames and self._parts:
                part = self._parts[0]
                take = min(frames - n, len(part) - self._pos)
                out[n:n + take] = part[self._pos:self._pos + take]
                n += take
                self._pos += take
                if self._pos >= len(part):
                    self._parts.popleft()
                    self._pos = 0
            finished = self._closed and not self._parts
        out[n:] = 0.0
        if finished:
            raise sd.CallbackStop  # this block still plays, then finished_callback


def _device_rate(sd, device) -> int | None:
    """The output device's default sample rate (None if it can't be asked)."""
    try:
        return int(sd.query_devices(device if device is not None else sd.default.device[1])["default_samplerate"])
    except Exception:  # noqa: BLE001 - unknown device: play at the voice's rate as before
        return None


def _resample(samples, rate: int, out_rate: int):
    """float32 mono from `rate` to `out_rate` (linear: fine for a band-passed radio voice)."""
    import numpy as np  # type: ignore

    samples = np.asarray(samples, dtype=np.float32)
    if rate == out_rate or len(samples) == 0:
        return samples
    n = int(round(len(samples) * out_rate / rate))
    return np.interp(np.arange(n) * (rate / out_rate), np.arange(len(samples)), samples).astype(np.float32)


def _ahead(chunks):
    """Run a chunk generator in a thread, ahead of the consumer: the next part is synthesized while the current one
    plays (playback blocks on the sound card; without this, every part would wait for the one before to finish)."""
    q: queue.Queue = queue.Queue(maxsize=8)
    done = object()

    def work():
        try:
            for c in chunks:
                q.put(c)
        except Exception as exc:  # noqa: BLE001 - surfaces in the consumer
            q.put(exc)
        finally:
            q.put(done)

    threading.Thread(target=work, daemon=True, name="tts-synth").start()
    while True:
        item = q.get()
        if item is done:
            return
        if isinstance(item, Exception):
            raise item
        yield item
