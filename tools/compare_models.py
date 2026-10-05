"""Ask every candidate model for the same LLM-owned turn and score the replies.

The IFR clearance itself is issued by code (atc.clearance), so models are compared on what they still own.
Scenario: SABE, IFR clearance already read back, pilot calls Ground ready to taxi. SimBrief plan from
simbrief_last.json, wind/QNH from that day's METAR (030/07, Q1020 -> runway 31 computed by code).

Keys come from env vars; a provider without a key is skipped:
    GEMINI_API_KEY   NVIDIA_API_KEY   OPENROUTER_API_KEY

    python tools/compare_models.py
    python tools/compare_models.py --only openrouter --runs 3
    python tools/compare_models.py --model openrouter=some/model:free
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.clearance import items
from atc.flightplan import load_simbrief
from atc.llm.client import OpenAICompatLLM
from atc.main import handle
from atc.runway import runway_in_use
from atc.session import Session
from atc.sim.fake import FakeSim

PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}

CANDIDATES = [
    ("gemini", "gemini-2.5-flash"),
    ("gemini", "gemini-2.5-flash-lite"),
    ("nvidia", "meta/llama-3.3-70b-instruct"),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
    ("openrouter", "nvidia/nemotron-3.5-lightning:free"),
    ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("openrouter", "google/gemma-4-31b-it:free"),
]

CALL = "Aeroparque Ground, Martinair {num}, at the gate, ready to taxi"
WIND, QNH = (30.0, 7.0), 1020.0


def score(reply: str | None, rwy: str) -> list[str]:
    if not reply:
        return ["no reply"]
    low = reply.lower()
    fails = []
    if not low.startswith("martinair"):
        fails.append("does not start with the spoken callsign")
    if "mar4133" in low.replace(" ", ""):
        fails.append("said the ICAO callsign instead of the telephony")
    spoken_rwy = " ".join({"0": "zero", "1": "one", "2": "two", "3": "three"}.get(c, c) for c in rwy)
    if spoken_rwy not in low and f"runway {rwy}" not in low:
        fails.append(f"no runway {rwy}")
    if "holding point" not in low:
        fails.append("no 'holding point' (ICAO)")
    if "one zero two zero" not in low:
        fails.append("no QNH one zero two zero")
    if re.search(r"\b(alfa|alpha|bravo|charlie|delta|echo|foxtrot|golf|hotel|via)\b", low):
        fails.append("named a taxiway (none on file)")
    if "standby" in low or "stand by" in low:
        fails.append("said standby")
    if "aeroparque" in low:
        fails.append("said station name (not first contact)")
    if re.search(r"\b\d{3}\.\d", reply) or "decimal" in low:
        fails.append("gave a frequency (not asked)")
    if any(x in low for x in ("cleared for takeoff", "line up", "squawk")):
        fails.append("instructions not asked for")
    if len(reply.split()) > 35:
        fails.append(f"too long ({len(reply.split())} words)")
    return fails


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--plan", type=Path, default=Path("simbrief_last.json"))
    p.add_argument("--airports-dir", type=Path, default=Path("airports"))
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--only", choices=sorted(PROVIDERS))
    p.add_argument("--model", action="append", default=[], help="provider=model, replaces the built-in list")
    args = p.parse_args()

    plan = load_simbrief(args.plan)
    airport = load_airport(args.airports_dir / f"{plan.origin}.yaml")
    gnd = next(f for f in airport.frequencies if f.kind == "GND")
    rwy = runway_in_use(airport, *WIND).ident
    candidates = [tuple(m.split("=", 1)) for m in args.model] or CANDIDATES
    if args.only:
        candidates = [c for c in candidates if c[0] == args.only]

    call = CALL.format(num="".join(c for c in plan.callsign if c.isdigit()))
    print(f"plan {plan.callsign} {plan.origin}-{plan.destination}, runway in use {rwy}\nPILOT: {call}\n")
    rows = []
    for provider, model in candidates:
        base, key_var = PROVIDERS[provider]
        key = os.environ.get(key_var, "")
        if not key:
            print(f"--- {provider}/{model}: skipped, {key_var} not set\n")
            continue
        llm = OpenAICompatLLM(base, key, model, timeout_s=20, retries=1)
        passes, times = 0, []
        for _ in range(args.runs):
            sim = FakeSim(airport, callsign=plan.callsign)
            sim.update(com1_mhz=gnd.mhz, wind_dir_deg=WIND[0], wind_kt=WIND[1], qnh_hpa=QNH)
            session = Session(callsign=plan.callsign, plan=plan, telephony="Martinair", dest_name="Rosario")
            session.clearance = "confirmed"  # as if Delivery already did its part
            session.clearance_text = ", ".join(i.spoken for i in items(session, airport, "Rosario"))
            session.contacted.add("ground")  # pilot already called Ground once
            t0 = time.perf_counter()
            reply = handle(airport, sim, llm, _Quiet(), [], call, session=session)
            dt = time.perf_counter() - t0
            fails = score(reply, rwy)
            passes += not fails
            if reply:
                times.append(dt)
            print(f"--- {provider}/{model}  {dt:.1f}s  {'PASS' if not fails else 'FAIL'}")
            print(f"    ATC: {reply}")
            for f in fails:
                print(f"    - {f}")
        avg = sum(times) / len(times) if times else None
        rows.append((f"{provider}/{model}", passes, args.runs, avg))
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
