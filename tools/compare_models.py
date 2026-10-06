"""Ask every candidate model the same LLM-owned turns and score the replies.

Clearance, taxi, takeoff/landing, check-ins, handoffs and traffic info are produced by code now, so models
are compared on what they still own: a VFR zone transit at Tower (SARC; the circuit itself is code since
2026-10-06) and a free-form question (SABE).

Keys come from env vars (or the Windows user environment); a provider without a key is skipped:
    GROQ_API_KEY  GEMINI_API_KEY  CEREBRAS_API_KEY  NVIDIA_API_KEY  MISTRAL_API_KEY  OPENROUTER_API_KEY

    python tools/compare_models.py
    python tools/compare_models.py --only groq --runs 3
    python tools/compare_models.py --model groq:llama-3.3-70b-versatile
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.llm.client import PROVIDERS, OpenAICompatLLM, env_key
from atc.main import handle
from atc.session import Session
from atc.sim.fake import FakeSim

CANDIDATES = [  # answering on free keys 2026-10-05 (tools/list_models.py shows what exists now)
    "groq:qwen/qwen3.8-27b",
    "groq:openai/gpt-oss-120b",
    "groq:openai/gpt-oss-20b",
    "gemini:gemini-3.5-flash-lite",
    "gemini:gemini-flash-lite-latest",
    "gemini:gemini-3.5-flash",
    "nvidia:nvidia/nemotron-3-super-120b-a12b",
    "openrouter:nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
]
WIND, QNH = (30.0, 7.0), 1020.0


def _vfr_transit(airports_dir: Path):
    apt = load_airport(airports_dir / "SARC.yaml")
    sim = FakeSim(apt, callsign="LVABC")
    twr = next(f.mhz for f in apt.frequencies if f.kind == "TWR")
    sim.update(com1_mhz=twr, wind_dir_deg=WIND[0], wind_kt=WIND[1], qnh_hpa=QNH,
               lat=apt.lat - 0.17, lon=apt.lon, heading_deg=0.0)
    sim.set_airborne(2300, 100)
    return apt, sim, Session(callsign="LVABC"), \
        "Corrientes Tower, LV-ABC, Cessna 172, 10 miles south, 2500 feet, request to transit the zone northbound"


def _score_vfr(reply: str) -> list[str]:
    low = reply.lower()
    fails = []
    if not low.startswith("lima victor alfa bravo charlie"):
        fails.append("does not start with the spelled callsign")
    if re.search(r"cleared to land|cleared for takeoff|downwind|base", low):
        fails.append("treated the transit as a landing")
    if "one zero two zero" not in low:
        fails.append("no QNH one zero two zero")
    if "report" not in low:
        fails.append("no reporting instruction")
    rwys = re.findall(r"runway ((?:zero|one|two|three)(?: (?:zero|one|two|three|four|five|six|seven|eight|niner))?)", low)
    if any(r != "zero two" for r in rwys):
        fails.append("named a runway other than zero two (computed from wind 030)")
    return fails


def _question(airports_dir: Path):
    apt = load_airport(airports_dir / "SABE.yaml")
    sim = FakeSim(apt, callsign="MAR4133")
    sim.update(com1_mhz=121.9, wind_dir_deg=WIND[0], wind_kt=WIND[1], qnh_hpa=QNH)
    s = Session(callsign="MAR4133", telephony="Martinair")
    s.where = "SABE"
    s.contacted.add("SABE:ground")
    # wind/QNH questions are answered by code since 2026-10-06 (info.py); this one still reaches the model
    return apt, sim, s, "Aeroparque Ground, Martinair 4133, say the runway in use and the temperature please"


def _score_question(reply: str) -> list[str]:
    low = reply.lower()
    fails = []
    if not low.startswith("martinair four one three three"):
        fails.append("does not start with the spoken callsign")
    if "three one" not in low and "31" not in low:
        fails.append("no runway three one (computed from wind 030)")
    if re.search(r"temperature[^.,]*\b(?:zero|one|two|three|four|five|six|seven|eight|nine|niner|\d)", low):
        fails.append("stated a temperature it was not given")
    if "aeroparque" in low:
        fails.append("said station name (not first contact)")
    return fails


CASES = [("VFR zone transit SARC", _vfr_transit, _score_vfr), ("runway/temperature question SABE", _question,
                                                                   _score_question)]


def score(reply: str | None, check) -> list[str]:
    if not reply:
        return ["no reply"]
    fails = check(reply)
    if "standby" in reply.lower():
        fails.append("said standby")
    if len(reply.split()) > 35:
        fails.append(f"too long ({len(reply.split())} words)")
    return fails


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--airports-dir", type=Path, default=Path("airports"))
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--only", choices=sorted(PROVIDERS))
    p.add_argument("--model", action="append", default=[], help="provider:model, replaces the built-in list")
    args = p.parse_args()

    candidates = args.model or CANDIDATES
    if args.only:
        candidates = [c for c in candidates if c.startswith(args.only + ":")]
    rows = []
    for spec in candidates:
        provider = spec.split(":", 1)[0]
        if provider not in PROVIDERS or not env_key(PROVIDERS[provider][1]):
            print(f"--- {spec}: skipped, {PROVIDERS.get(provider, ('', '?'))[1]} not set\n")
            continue
        llm = OpenAICompatLLM("", "", spec, timeout_s=20, retries=1, deadline_s=30)
        passes, total, times = 0, 0, []
        for name, setup, check in CASES:
            for _ in range(args.runs):
                apt, sim, session, call = setup(args.airports_dir)
                t0 = time.perf_counter()
                reply = handle(apt, sim, llm, _Quiet(), [], call, session=session)
                dt = time.perf_counter() - t0
                fails = score(reply, check)
                total += 1
                passes += not fails
                if reply:
                    times.append(dt)
                print(f"--- {spec} [{name}]  {dt:.1f}s  {'PASS' if not fails else 'FAIL'}")
                print(f"    ATC: {reply}")
                for f in fails:
                    print(f"    - {f}")
        avg = sum(times) / len(times) if times else None
        rows.append((spec, passes, total, avg))
        print()

    print("=== summary ===")
    for name, ok, n, avg in sorted(rows, key=lambda r: (-r[1], r[3] or 99)):
        print(f"{ok}/{n}  {f'{avg:4.1f}s' if avg else '   - '}  {name}")
    return 0


class _Quiet(PrintTTS):
    def say(self, text: str) -> None:  # the script prints replies itself
        pass


if __name__ == "__main__":
    sys.exit(main())
