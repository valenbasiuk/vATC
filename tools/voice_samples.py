"""One WAV per voice in each accent pool, so you can hear them and blacklist the ones you don't like.

    python tools/voice_samples.py                  # every tagged voice + 10 of the generic (libritts) pool
    python tools/voice_samples.py --accent Spanish # one pool only (Spanish, English, Scottish, American, ...)
    python tools/voice_samples.py --generic 0 --no-fx

Files go to voice_samples/<accent>/<voice key>.wav, e.g. voice_samples/Spanish/en_US-l2arctic-medium#16.wav.
To never use a voice again, put its key (the file name without .wav) on a line of voices/blacklist.txt;
"accent:Spanish" there drops the whole pool (those countries then get the generic voices).
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

TEXT = ("Argentina one two three four, descend to three thousand feet, QNH one zero one three, "
        "cleared ILS approach runway one three.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--voices", type=Path, default=Path("voices"))
    p.add_argument("--out", type=Path, default=Path("voice_samples"))
    p.add_argument("--accent", help="only this pool")
    p.add_argument("--generic", type=int, default=10, help="how many of the untagged pool (904 libritts voices)")
    p.add_argument("--text", default=TEXT)
    p.add_argument("--no-fx", action="store_true", help="clean audio, without the radio filter")
    args = p.parse_args()

    import numpy as np  # type: ignore

    from atc.audio.radio_fx import apply_radio_fx
    from atc.audio.tts import PiperTTS
    from atc.voices import VoiceBank

    bank = VoiceBank(args.voices)
    if not bank.pools:
        print(f"no voices in {args.voices} (python tools/download_voices.py)")
        return 1
    print(f"pools: {bank.describe()}" + (f"; blacklisted: {len(bank.blacklist)}" if bank.blacklist else ""))
    first = next(v for pool in bank.pools.values() for v in pool)
    tts = PiperTTS(Path(first.model), radio_fx=False)
    written = 0
    for accent, pool in sorted(bank.pools.items()):
        if args.accent and accent.lower() != args.accent.lower():
            continue
        voices = pool[: args.generic] if accent == "generic" else pool
        folder = args.out / accent
        folder.mkdir(parents=True, exist_ok=True)
        for v in voices:
            samples, rate = tts.synthesize(args.text, v)
            if not args.no_fx:
                samples = apply_radio_fx(samples, rate)
            pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            path = folder / f"{v.key}{'_F' if v.female else ''}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm.tobytes())
            written += 1
            print(f"  {path}")
    print(f"{written} samples in {args.out}/ (blacklist: voices/blacklist.txt, the file name without _F.wav)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
