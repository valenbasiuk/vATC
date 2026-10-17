"""Soak test for our own traffic: hours of it at an airport, through the real manager and the watcher, on a sim
clock (much faster than real time). Finds what only shows after a while: aircraft stuck in a state, two on a runway
with one running into the other, go-arounds (and who was on the runway), long waits at the holding point.

    python tools/soak_own_traffic.py                          # SABE and SARC, 3 h each, the real data on this PC
    python tools/soak_own_traffic.py --airports SAEZ --hours 1 --factor 1.0
    python tools/soak_own_traffic.py --fake-data              # without the Little Navmap db / Community folder

The real data: the scenery's taxi map and stands (Little Navmap db) and the FSLTL / FS Traffic schedules and
models from the Community folder, as in the sim. The user sits parked 2-3 NM away, listening to Tower.
(2026-10-11: found the push deadlock, starved departures and the go-arounds fixed that day.)
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import io
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WINDS = {"SABE": (130.0, 10.0), "SARC": (20.0, 8.0)}  # else along the first runway, 10 kt


def soak(icao: str, hours: float, seed: int, factor: float, real: bool, log: bool) -> None:
    import atc.world as world_mod
    from atc import departures
    from atc.airports.schema import load_airport
    from atc.audio.tts import PrintTTS
    from atc.clearance import _freq
    from atc.geo import heading_diff
    from atc.main import _Callbacks
    from atc.own import catalog
    from atc.own.injector import FakeInjector
    from atc.own.manager import OwnTraffic
    from atc.sequence import along_cross, on_runway
    from atc.session import Session
    from atc.sim.fake import FakeSim
    from atc.taxi import load_network
    from atc.world import World

    class Quiet(PrintTTS):
        def say(self, text):
            print("SAY>", text)

        def say_as(self, text, who="ATC", voice=None):
            print("SAY>", text)

    world_mod.AUTO_GENERATE = False
    apt = load_airport(ROOT / "airports" / f"{icao}.yaml")
    net = load_network(ROOT / "airports", icao, use_navdb=real)
    if real:
        from atc import navdb

        navdb.enrich(apt)
    wind = WINDS.get(icao) or ((apt.runways[0].heading_deg or 0.0) if apt.runways else 0.0, 10.0)
    twr = _freq(apt, "TWR")
    world = World([apt], taxi={icao: net})
    sim = FakeSim(apt, callsign="LVUSR")
    sim.update(lat=apt.lat + 0.03, lon=apt.lon + 0.03, com1_mhz=twr.mhz if twr else 0.0,
               wind_dir_deg=wind[0], wind_kt=wind[1], qnh_hpa=1013.0)
    cb = _Callbacks(world, sim, Quiet(), [], Session(callsign="LVUSR"))
    comm = catalog.community_dir() if real else None
    mgr = OwnTraffic(world, sim, FakeInjector(), bus=cb.bus,
                     catalog=catalog.load(comm) if comm else catalog.ModelCatalog(), community=comm,
                     wind=lambda a: wind, rng=random.Random(seed), factor=factor, auto=True)
    departures.reset()
    departures.CLOCK = lambda: mgr.clock or 0.0

    t, dt, last_tick, end = 0.0, 0.25, -1.0, hours * 3600.0
    problems, examples, seen = collections.Counter(), {}, collections.Counter()
    since: dict[str, tuple[str, float]] = {}
    go_arounds, waits, holding_at = [], [], {}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        while t < end:
            mgr.step(t, sim.own(), [])
            if t - last_tick >= 1.0:  # the watcher once a second, on the same clock
                last_tick = t
                cb.tick(now=t)
            pilots = list(mgr.pilots.values())
            for p in pilots:
                cs = p.flight.callsign
                if p.state != since.get(cs, (None, 0.0))[0]:
                    if p.state == "holding":
                        holding_at[cs] = t
                    elif cs in holding_at and p.state in ("takeoff_radio", "lining_up"):
                        waits.append((round(t - holding_at.pop(cs)), cs))
                    if p.state == "go_around":
                        on = [f"{q.flight.callsign}:{q.state}" for q in pilots if q is not p and q.pose.agl_ft < 50
                              and any(on_runway(apt, r, q.traffic(apt.elevation_ft)) for r in apt.runways)]
                        go_arounds.append(f"t={t:.0f} {cs}, on the runway: {on or 'nobody'}")
                    since[cs] = (p.state, t)
                    seen[p.state] += 1
                st, at = since[cs]
                if st not in ("parked", "parked_in", "climbout", "joining") and t - at > 900:
                    problems[f"stuck > 15 min in {st}"] += 1
                    chain, q, seen_cs = [], p, set()  # who waits for whom, and what each of them is doing
                    while q is not None and q.flight.callsign not in seen_cs and len(chain) < 5:
                        seen_cs.add(q.flight.callsign)
                        chain.append(f"{q.flight.callsign}:{q.state}(wants={q.wants}, gs={q.pose.gs_kt:.0f}, "
                                     f"ignore={sorted(q.ignore)})")
                        q = mgr.pilots.get(q.blocked_by) if q.blocked_by else None
                    examples.setdefault(f"stuck {st}", f"t={t:.0f} " + " -> ".join(chain)
                                        + (f" -> {p.blocked_by} (not ours)" if p.blocked_by and
                                           p.blocked_by not in mgr.pilots else ""))
                    since[cs] = (st, t)
            for r in apt.runways:  # one fast along the runway with somebody ahead of it on it
                users = [p for p in pilots if p.pose.agl_ft < 50 and on_runway(apt, r, p.traffic(apt.elevation_ft))]
                for f in users:
                    if f.pose.gs_kt <= 40 or heading_diff(f.pose.heading_deg, r.heading_deg or 0.0) > 30:
                        continue
                    fa = along_cross(apt, r, f.pose.lat, f.pose.lon)[0]
                    for o in users:
                        if o is not f and along_cross(apt, r, o.pose.lat, o.pose.lon)[0] > fa:
                            problems[f"running into one on runway {r.ident}"] += 1
                            examples.setdefault("runway", f"{f.flight.callsign} ({f.state}) -> "
                                                          f"{o.flight.callsign} ({o.state}) t={t:.0f}")
            t += dt
    departures.CLOCK = __import__("time").monotonic
    lines = out.getvalue().splitlines()
    ours = [x for x in lines if x.startswith("[own traffic")]
    errors = [x for x in lines if "Traceback" in x or "Error" in x or x.startswith("[watcher:")]
    waits.sort(reverse=True)
    print(f"== {icao}, {hours:g} h, wind {wind[0]:.0f}/{wind[1]:.0f}, factor {factor}: "
          f"{sum(1 for x in ours if ' gone' in x)} flights gone, {seen['rollout']} landings, {seen['takeoff']} takeoffs")
    print("   problems:", dict(problems) or "none", examples or "")
    print("   go-arounds:", go_arounds[:8] or "none")
    print("   deadlock passes:", sum(1 for x in ours if "deadlock" in x or "pushes on past" in x))
    print("   longest holding-point waits (s):", waits[:6] or "none",
          f"median {waits[len(waits) // 2][0]}" if waits else "")
    print("   errors:", errors[:8] or "none")
    if log:
        print("\n".join(x for x in ours if " gone" not in x)[:8000])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--airports", default="SABE,SARC")
    ap.add_argument("--hours", type=float, default=3.0)
    ap.add_argument("--factor", type=float, default=1.5, help="traffic amount, as --own-factor")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fake-data", action="store_true", help="no Little Navmap db, no Community folder")
    ap.add_argument("--log", action="store_true", help="print our traffic's lines too")
    args = ap.parse_args()
    os.environ["ATC_METAR"] = "off"
    if args.fake_data:
        os.environ["ATC_LNM_DB"] = os.environ["ATC_COMMUNITY_DIR"] = str(ROOT / "nonexistent")
    os.chdir(ROOT)
    for icao in args.airports.upper().split(","):
        soak(icao.strip(), args.hours, args.seed, args.factor, not args.fake_data, args.log)


if __name__ == "__main__":
    main()
