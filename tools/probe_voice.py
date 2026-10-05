"""STEP 3 on the PC (no sim needed): hear a Piper voice, with and without the radio effect.

    python -m piper.download_voices en_US-lessac-medium --data-dir voices      # once
    python tools/probe_voice.py voices/en_US-lessac-medium.onnx
    python tools/probe_voice.py voices/en_US-lessac-medium.onnx --speed 0.85 --text "Your own line"

Listen for: is it clear? does the radio effect hide the robotic edges or just make it muddy?
Try 3-4 voices and pick the one that sounds most like a controller. Typical controllers talk fast:
--speed 0.8 to 0.9 (length_scale below 1 = faster).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from atc.audio.tts import PiperTTS

DEFAULT_LINE = "Cessna one two three alpha bravo, runway two seven, wind two seven zero at eight, cleared for takeoff."


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("voice", type=Path, help="path to a .onnx Piper voice")
    p.add_argument("--text", default=DEFAULT_LINE)
    p.add_argument("--speed", type=float, default=1.0, help="length_scale; below 1.0 is faster")
    args = p.parse_args()

    for fx in (False, True):
        tts = PiperTTS(args.voice, radio_fx=fx, length_scale=args.speed)
        t0 = time.perf_counter()
        samples, rate = tts.synthesize(args.text)
        dt = time.perf_counter() - t0
        secs = len(samples) / rate if rate else 0
        print(f"radio_fx={fx}: synthesized {secs:.1f}s of audio in {dt:.2f}s (first run includes model warm-up)")
        tts.say(args.text)


if __name__ == "__main__":
    main()
