"""Compare the voices (improvement plan J1): the same ATC phrases in every engine, through the radio filter.

    python tools/compare_voices.py                      # a set per engine: WAVs in voice_samples/compare/
    python tools/compare_voices.py --score              # + how much of it Whisper understands (word error rate)
    python tools/compare_voices.py --voices kokoro:am_michael "en_US-l2arctic-medium#6" --score

For each voice: one WAV with all the phrases (as heard on the radio), the synthesis time per second of speech
(real-time factor: below ~0.5 the audio never waits), and with --score the word error rate of faster-whisper
(the same model as --stt-model) on the filtered audio: a stand-in for "can you understand it through the radio".
Listen too: Whisper forgives a flat, robotic voice that a human would not enjoy.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import wave
from pathlib import Path

PHRASES = [
    "Aerolineas one two three four, wind one four zero degrees one zero knots, runway one three, cleared for takeoff.",
    "LATAM four five six, descend to flight level one zero zero, expect ILS approach runway three one.",
    "Taxi to holding point runway three one via Kilo, Alfa, QNH one zero one three.",
    "Martinair four one three three, contact Ezeiza Control one three five decimal five, good day.",
    "Speedbird two eight six, traffic, two o'clock, five miles, opposite direction, Airbus three twenty, one thousand "
    "feet above.",
]
DEFAULT = ["en_US-libritts-high#0", "en_US-libritts-high#120", "en_US-ryan-high", "en_GB-vctk-medium#5",
           "en_US-l2arctic-medium#6", "en_US-l2arctic-medium#22",
           "kokoro:af_heart", "kokoro:am_michael", "kokoro:am_michael+am_puck@0.5", "kokoro:bm_george",
           "kokoro:bf_emma", "kokoro:em_alex", "kokoro:ef_dora", "kokoro:pm_alex", "kokoro:hm_omega",
           "kokoro:zm_yunjian", "kokoro:im_nicola", "kokoro:ff_siwis"]
_DIGITS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven",
           "8": "eight", "9": "nine"}


def _words(s: str) -> list[str]:
    s = s.lower().replace("niner", "nine").replace("decimal", "point").replace("o'clock", "oclock")
    s = re.sub(r"(\d)\.(\d)", r"\1 point \2", s)
    s = re.sub(r"\d", lambda m: f" {_DIGITS[m.group()]} ", s)
    return re.sub(r"[^a-z ]", " ", s).split()


def wer(ref: str, hyp: str) -> float:
    r, h = _words(ref), _words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return d[len(h)] / max(1, len(r))


def voice_for(spec: str, voices_dir: Path):
    """'kokoro:<style>' or '<piper model stem>[#speaker]' -> atc.voices.Voice (None if not downloaded)."""
    from atc.voices import Voice, kokoro_model

    if spec.startswith("kokoro:"):
        km = kokoro_model(voices_dir)
        return Voice(str(km), None, style=spec.split(":", 1)[1]) if km else None
    stem, _, spk = spec.partition("#")
    path = voices_dir / f"{stem}.onnx"
    return Voice(str(path), int(spk) if spk else None) if path.exists() else None


def _resample(x, sr: int, to: int = 16000):
    import numpy as np  # type: ignore

    if sr == to:
        return x.astype(np.float32)
    return np.interp(np.arange(0, len(x), sr / to), np.arange(len(x)), x).astype(np.float32)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--voices-dir", type=Path, default=Path("voices"))
    p.add_argument("--voices", nargs="*", default=None, help="voice keys (default: a set per engine)")
    p.add_argument("--out", type=Path, default=Path("voice_samples/compare"))
    p.add_argument("--score", action="store_true", help="word error rate with faster-whisper")
    p.add_argument("--stt-model", default="small.en")
    args = p.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import numpy as np  # type: ignore

    from atc.audio.radio_fx import apply_radio_fx
    from atc.audio.tts import PiperTTS

    base = next(iter(sorted(args.voices_dir.glob("*.onnx.json"))), None)
    if base is None:
        print("no Piper voice in", args.voices_dir, "(python tools/download_voices.py)")
        return 1
    tts = PiperTTS(base.with_suffix(""), radio_fx=False)
    stt = None
    if args.score:
        from faster_whisper import WhisperModel  # type: ignore

        stt = WhisperModel(args.stt_model, device="cpu", compute_type="int8")
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"{'voice':38s} {'rtf':>5s} {'first':>6s}" + (f" {'WER':>5s}  worst heard" if stt else ""))
    for spec in args.voices or DEFAULT:
        v = voice_for(spec, args.voices_dir)
        if v is None:
            print(f"{spec:38s} not downloaded")
            continue
        tts.synthesize("Radio check.", v)  # warm up (model load)
        audio, sr, synth_s, speech_s, first, errs = [], 22050, 0.0, 0.0, [], []
        for text in PHRASES:
            t0 = time.perf_counter()
            chunks = tts._chunks(text, v)
            x, sr = next(chunks)
            first.append(time.perf_counter() - t0)
            parts = [x] + [c for c, _ in chunks]
            synth_s += time.perf_counter() - t0
            x = np.concatenate(parts)
            speech_s += len(x) / sr
            y = apply_radio_fx(x, sr)
            audio += [y, np.zeros(int(sr * 0.6), dtype=np.float32)]
            if stt is not None:
                segs, _ = stt.transcribe(_resample(y, sr), language="en", beam_size=1)
                heard = " ".join(s.text for s in segs).strip()
                errs.append((wer(text, heard), heard))
        name = re.sub(r"[^\w+@.#-]", "_", spec)
        with wave.open(str(args.out / f"{name}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((np.clip(np.concatenate(audio), -1, 1) * 32767).astype(np.int16).tobytes())
        line = f"{spec:38s} {synth_s / speech_s:5.2f} {max(first):5.2f}s"
        if errs:
            worst = max(errs)
            line += f" {sum(e for e, _ in errs) / len(errs):5.2f}  {worst[1][:70]}"
        print(line, flush=True)
    print(f"WAVs in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
