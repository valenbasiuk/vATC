"""Scenario replay: scripted situations + checks, runnable against any LLM (roadmap item 14).

    python -m atc.scenarios                       # stub LLM (only proves the harness works)
    ATC_LLM_BASE_URL=... ATC_LLM_API_KEY=... ATC_LLM_MODEL=... python -m atc.scenarios

LLM output varies run to run, so checks are content rules (must contain / must not contain),
never exact strings. Use this to compare models fairly and to catch prompt regressions.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from atc.airports.schema import load_airport
from atc.main import handle
from atc.models import Traffic
from atc.sim.fake import FakeSim


class _Capture:
    def __init__(self) -> None:
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)


@dataclass
class TurnResult:
    say: str
    reply: str | None
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass
class ScenarioResult:
    name: str
    turns: list[TurnResult]

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.turns)


def _check(turn: dict, reply: str | None) -> list[str]:
    fails: list[str] = []
    if turn.get("expect_silence"):
        if reply is not None:
            fails.append(f"expected silence, got: {reply!r}")
        return fails
    if reply is None:
        return [] if turn.get("silence_ok") else ["expected a reply, got silence"]
    low = reply.lower()
    any_of = [s.lower() for s in turn.get("must_contain_any", [])]
    if any_of and not any(s in low for s in any_of):
        fails.append(f"none of {any_of} in {reply!r}")
    for s in turn.get("must_contain_all", []):
        if s.lower() not in low:
            fails.append(f"missing {s!r} in {reply!r}")
    for s in turn.get("must_not_contain", []):
        if s.lower() in low:
            fails.append(f"forbidden {s!r} in {reply!r}")
    max_words = turn.get("max_words")
    if max_words and len(reply.split()) > max_words:
        fails.append(f"{len(reply.split())} words > {max_words}")
    return fails


def run_scenario(spec: dict, airports_dir: Path, llm) -> ScenarioResult:
    airport = load_airport(airports_dir / f"{spec['airport']}.yaml")
    sim = FakeSim(airport, callsign=spec.get("callsign", "N123AB"))
    setup = spec.get("setup", {})
    if "freq" in setup:
        sim.update(com1_mhz=float(setup["freq"]))
    if setup.get("airborne"):
        sim.set_airborne(agl_ft=float(setup.get("agl_ft", 1000)))
    if "wind" in setup:
        w = setup["wind"]
        sim.update(wind_dir_deg=float(w[0]), wind_kt=float(w[1]), qnh_hpa=float(w[2]) if len(w) > 2 else None)
    for t in setup.get("traffic", []):
        sim.add_traffic(
            Traffic(
                callsign=t["callsign"],
                lat=airport.lat + t.get("dlat", 0.0),
                lon=airport.lon + t.get("dlon", 0.0),
                alt_msl_ft=t.get("alt_msl_ft", airport.elevation_ft),
                gs_kt=t.get("gs_kt", 0),
                heading_deg=t.get("heading_deg", 0),
                on_ground=t.get("on_ground", False),
            )
        )

    history: list = []
    results: list[TurnResult] = []
    for turn in spec["turns"]:
        if "freq" in turn:  # retune mid-scenario
            sim.update(com1_mhz=float(turn["freq"]))
        cap = _Capture()
        reply = handle(airport, sim, llm, cap, history, turn["say"])
        results.append(TurnResult(turn["say"], reply, _check(turn, reply)))
    return ScenarioResult(spec.get("name", spec["airport"]), results)


def load_scenarios(directory: Path) -> list[dict]:
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.yaml"))]


def main(argv: list[str] | None = None) -> int:
    from atc.llm.client import make_llm

    p = argparse.ArgumentParser(prog="atc.scenarios")
    p.add_argument("--scenarios-dir", type=Path, default=Path("scenarios"))
    p.add_argument("--airports-dir", type=Path, default=Path("tests/fixtures/airports"))
    args = p.parse_args(argv)

    llm = make_llm()
    total = failed = 0
    for spec in load_scenarios(args.scenarios_dir):
        res = run_scenario(spec, args.airports_dir, llm)
        total += 1
        failed += 0 if res.ok else 1
        print(f"[{'PASS' if res.ok else 'FAIL'}] {res.name}")
        for t in res.turns:
            for f in t.failures:
                print(f"    - {t.say!r}: {f}")
    print(f"{total - failed}/{total} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
