"""Download the Piper voices that give the radio its accents (into voices/, next to the libritts one).

    python tools/download_voices.py            # the two accent sets (~150 MB)
    python tools/download_voices.py --more     # + a few single British/American voices
    python tools/download_voices.py --list     # what is in voices/ and how it is used

  en_US-l2arctic-medium  24 speakers with a first language other than English: Spanish (Aerolineas, LATAM, Iberia,
                         Argentine controllers), Mandarin, Hindi, Korean, Arabic, Vietnamese
  en_GB-vctk-medium      109 speakers: English, Scottish, Irish, Welsh, American, Canadian, Australian, NZ,
                         South African, Indian

atc picks them up by itself (atc.voices): every AI pilot gets a voice from its airline's country, every controller
position one from the airport's country. Nothing to configure. Voices: github.com/rhasspy/piper-voices (MIT/CC
licences per voice, see each MODEL_CARD).
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
ACCENTS = ["en_US-l2arctic-medium", "en_GB-vctk-medium"]
MORE = ["en_GB-alan-medium", "en_GB-northern_english_male-medium", "en_GB-southern_english_female-low",
        "en_US-ryan-high", "en_US-joe-medium", "en_US-kristin-medium"]


def url_for(voice: str, ext: str) -> str:
    locale, name, quality = voice.split("-", 2)
    return f"{BASE}/{locale.split('_')[0]}/{locale}/{name}/{quality}/{voice}{ext}"


def fetch(voice: str, out: Path) -> None:
    for ext in (".onnx.json", ".onnx"):
        target = out / f"{voice}{ext}"
        if target.exists() and target.stat().st_size > 0:
            print(f"  have {target.name}")
            continue
        print(f"  downloading {target.name} ...", flush=True)
        tmp = target.with_suffix(target.suffix + ".part")
        req = urllib.request.Request(url_for(voice, ext), headers={"User-Agent": "vATC voice downloader"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
                got += len(chunk)
                if total > 5 << 20:
                    print(f"\r    {got >> 20} / {total >> 20} MB", end="", flush=True)
        if total > 5 << 20:
            print()
        tmp.replace(target)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("voices"))
    p.add_argument("--more", action="store_true", help="also single-speaker British/American voices")
    p.add_argument("--list", action="store_true", help="show the accent pools made from voices/")
    args = p.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    if not args.list:
        args.out.mkdir(parents=True, exist_ok=True)
        for v in ACCENTS + (MORE if args.more else []):
            print(v)
            try:
                fetch(v, args.out)
            except Exception as exc:  # noqa: BLE001
                print(f"  failed: {exc}")
    from atc.voices import VoiceBank

    print("accent pools:", VoiceBank(args.out).describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
