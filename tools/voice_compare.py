"""Compare voice engines on the same ATC lines (improvement plan J1): Piper (local, today's), Kokoro-82M (local,
ONNX) and Microsoft Edge neural voices (cloud). Listen, pick, then put the winners in voices/extra_voices.txt.

    python tools/voice_compare.py                       # every engine that is installed, a few voices each
    python tools/voice_compare.py --engines piper,edge  # only these
    python tools/voice_compare.py --download-kokoro     # fetch Kokoro's model files into voices/kokoro/ (~340 MB)

Installs (each optional): `pip install kokoro-onnx` (+ the model files above), `pip install edge-tts miniaudio`.
Files: voice_samples/compare/<engine>-<voice>/<n>_radio.wav (and <n>_clean.wav); the table printed at the end (and in
voice_samples/compare/README.txt) has the time to first audio of each voice: the 3-4 s budget from the end of the
pilot's call includes it, so a voice that takes 2 s per sentence is too slow even if it sounds best.

voices/extra_voices.txt, one voice per line, '<accent> [F] <engine>:<voice>' (accents as voices.py names them:
American, English, Scottish, Irish, Australian, Indian, Spanish, generic ...):
    American      edge:en-US-GuyNeural
    English       kokoro:bm_george
    Spanish       edge:es-AR-TomasNeural
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

LINES = [
    "Martinair four one three three, cleared to Rosario, ATOVO four bravo departure, flight planned route, climb via "
    "SID, expect flight level two zero zero ten minutes after departure, squawk four seven two one.",
    "Argentina one two three four, taxi to holding point runway three one via Kilo, Alfa, QNH one zero one three.",
    "United four thirty-six, wind two niner zero at one two, runway two eight right, cleared for takeoff.",
    "Speedbird two eight six, turn left heading two seven zero, descend and maintain three thousand, vectors ILS "
    "runway two eight left approach.",
    "Lima Victor Alfa Bravo Charlie, number two, follow the Cessna on two mile final, report final.",
    "Aerolineas one two one six, go around, I say again, go around, traffic on the runway.",
]
LINE_ES = "Aerolíneas uno dos uno seis, autorizado a aterrizar pista tres uno, viento cero cuatro cero grados ocho nudos."

KOKORO = ["am_michael", "am_adam", "bm_george", "bm_lewis", "af_heart", "bf_emma"]
EDGE = ["en-US-GuyNeural", "en-US-ChristopherNeural", "en-GB-RyanNeural", "en-AU-WilliamNeural",
        "en-IE-ConnorNeural", "en-IN-PrabhatNeural", "es-AR-TomasNeural"]
KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"  # VERIFY


def _write(path: Path, samples, rate: int) -> None:
    import numpy as np  # type: ignore

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def _piper_voices(voices_dir: Path) -> list[tuple[str, object]]:
    """(label, voices.Voice) of each single-speaker Piper model, plus a few speakers of the multi-speaker ones."""
    from atc.voices import VoiceBank

    bank = VoiceBank(voices_dir)
    out = []
    for accent, pool in sorted(bank.pools.items()):
        piper = [v for v in pool if v.engine == "piper"]
        for v in piper[: (3 if accent in ("generic", "Spanish", "English", "American") else 1)]:
            out.append((f"piper-{v.key.replace('#', '_')}", v))
    return out


def download_kokoro() -> int:
    from atc.audio import engines

    engines.KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    for name in (engines.KOKORO_MODEL, engines.KOKORO_VOICES):
        target = engines.KOKORO_DIR / name
        if target.exists():
            print(f"{target} is there")
            continue
        print(f"downloading {name} ...")
        tmp = target.with_suffix(target.suffix + ".part")
        urllib.request.urlretrieve(KOKORO_URL + name, tmp)
        tmp.replace(target)
        print(f"  {target} ({target.stat().st_size / 1e6:.0f} MB)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engines", default="piper,kokoro,edge")
    p.add_argument("--voices", type=Path, default=Path("voices"))
    p.add_argument("--out", type=Path, default=Path("voice_samples/compare"))
    p.add_argument("--download-kokoro", action="store_true")
    p.add_argument("--lines", type=int, default=len(LINES), help="how many of the test lines")
    args = p.parse_args()
    if args.download_kokoro:
        return download_kokoro()

    from atc.audio import engines
    from atc.audio.radio_fx import apply_radio_fx

    wanted = [e.strip() for e in args.engines.split(",") if e.strip()]
    lines = LINES[: args.lines]
    jobs: list[tuple[str, str, object]] = []  # (engine, label, voice: a voices.Voice or an engine voice name)
    for engine in wanted:
        why = engines.available(engine)
        if why:
            print(f"{engine}: skipped ({why})")
            continue
        if engine == "piper":
            jobs += [("piper", label, v) for label, v in _piper_voices(args.voices)]
        elif engine == "kokoro":
            jobs += [("kokoro", f"kokoro-{n}", n) for n in KOKORO]
        elif engine == "edge":
            jobs += [("edge", f"edge-{n}", n) for n in EDGE]
    if not jobs:
        print("nothing to compare (python tools/download_voices.py for Piper voices)")
        return 1
    tts = None
    if any(e == "piper" for e, _, _ in jobs):
        from atc.audio.tts import PiperTTS

        first = next(v for e, _, v in jobs if e == "piper")
        tts = PiperTTS(Path(first.model), radio_fx=False)
    rows = []
    for engine, label, voice in jobs:
        folder = args.out / label
        folder.mkdir(parents=True, exist_ok=True)
        spanish = "es-" in label or label.startswith(("kokoro-e", "piper-es"))
        texts = [LINE_ES] if spanish else lines
        firsts, totals, audio_s = [], [], 0.0
        try:
            for i, text in enumerate(texts, 1):
                t0 = time.perf_counter()
                if engine == "piper":
                    samples, rate = tts.synthesize(text, voice)
                    first_sentence = text.split(". ")[0]
                    t1 = time.perf_counter()
                    tts.synthesize(first_sentence, voice)
                    firsts.append(time.perf_counter() - t1)
                else:
                    samples, rate = engines.synthesize(engine, voice, text)
                    firsts.append(time.perf_counter() - t0)  # (one sentence per line: the whole of it)
                totals.append(time.perf_counter() - t0)
                audio_s += len(samples) / rate
                _write(folder / f"{i}_clean.wav", samples, rate)
                _write(folder / f"{i}_radio.wav", apply_radio_fx(samples, rate), rate)
        except Exception as exc:  # noqa: BLE001 - one broken voice doesn't stop the comparison
            print(f"{label}: failed ({str(exc)[:150]})")
            continue
        rows.append((label, max(firsts), sum(totals) / max(1, len(totals)), sum(totals) / max(0.01, audio_s)))
        print(f"{label:44s} first audio {max(firsts):5.2f} s   per line {rows[-1][2]:5.2f} s   x{rows[-1][3]:.2f} RT")
    table = ["voice | worst time to first audio (s) | mean per line (s) | synthesis time / audio time",
             "---|---|---|---"] + [f"{r[0]} | {r[1]:.2f} | {r[2]:.2f} | {r[3]:.2f}" for r in rows]
    (args.out / "README.txt").write_text(
        "Listen to the *_radio.wav files (what you hear in the sim); *_clean.wav without the radio filter.\n"
        "Winners go into voices/extra_voices.txt (see tools/voice_compare.py).\n\n" + "\n".join(table) + "\n",
        encoding="utf-8")
    print(f"\n{len(rows)} voices in {args.out}/ (README.txt has the table)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
