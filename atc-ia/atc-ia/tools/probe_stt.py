"""STEP 4 on the PC (no sim needed): hold F9, speak a radio call, release, read the transcript.

    python tools/probe_stt.py                 # model small.en
    python tools/probe_stt.py --model base.en

First run downloads the Whisper model. Say things like:
  "Corrientes Tower, November one two three alpha bravo, ready for departure runway two one."
Check: callsigns and runway numbers transcribed correctly? Time per transcription (printed)?
Target: under about 1 second for a short call, on CPU (the GPU belongs to the sim).
"""

from __future__ import annotations

import argparse
import time

from atc.audio.ptt import PushToTalk
from atc.audio.stt import FasterWhisperSTT


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="small.en")
    p.add_argument("--key", default="f9")
    p.add_argument("--rounds", type=int, default=5)
    args = p.parse_args()

    print(f"loading {args.model} ...")
    stt = FasterWhisperSTT(model_size=args.model)
    ptt = PushToTalk(key=args.key)
    for i in range(args.rounds):
        print(f"[{i + 1}/{args.rounds}] hold {args.key.upper()} and speak ...")
        audio = ptt.record_once()
        if len(audio) < 1600:
            print("  (too short, nothing recorded)")
            continue
        t0 = time.perf_counter()
        text = stt.transcribe(audio)
        print(f"  {len(audio) / 16000:.1f}s of audio -> {time.perf_counter() - t0:.2f}s -> {text!r}")


if __name__ == "__main__":
    main()
