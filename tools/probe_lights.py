"""Our own traffic's lights, on the PC: which way does the sim switch them on for an aircraft we move ourselves?

    python tools/probe_lights.py                 # a model of a sim AI near you (else the FSLTL Flybondi 738)
    python tools/probe_lights.py --title "FSLTL_FAIB_B738_FBZ-FlyBondi"

Best at dusk or night (set the time in the sim). Stand still on an apron with open space ahead. It creates the
aircraft 100 m ahead of you, facing you, exactly as --own-traffic does (src/atc/own/injector.py: released from the
sim's AI, frozen, battery on), then tries three ways, 12 s each with everything off in between:
  A. key events only (BEACON_LIGHTS_SET, NAV_LIGHTS_SET, STROBES_SET, LANDING_LIGHTS_SET, TAXI_LIGHTS_SET, ...)
  B. the light simvars written on the object (LIGHT BEACON, LIGHT NAV, ...)
  C. both
Watch it and tell the IDE session in which step lights came on, and which: beacon (red, flashing, on top/bottom),
strobes (white flashes at the wingtips/tail), nav (red/green at the wingtips), landing/taxi (bright white ahead),
logo (the tail fin lit). Copy the terminal output too (exceptions matter). Then set ATC_OWN_LIGHTS to the way that
worked (events / data / both; both is the default).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from probe_inject import FSLTL_DEFAULT, OBJECT_ID_USER, REQ_USER, Sim, moved  # noqa: E402

from atc.own.injector import SimInjector  # noqa: E402
from atc.own.motion import Pose  # noqa: E402

ALL = ("beacon", "nav", "strobe", "landing", "taxi", "logo")


def _hold(inj: SimInjector, key: str, pose: Pose, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if inj.wait(0.05):
            inj.update(key, pose)


def _set(inj: SimInjector, key: str, mode: str, on: bool) -> None:
    inj.lights_mode = mode
    inj.objs[key].lights_at = 0.0
    inj.lights(key, **{k: on for k in ALL})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--title", help="model title to spawn (default: the first AI model found near you)")
    p.add_argument("--dll", help="path to SimConnect.dll")
    args = p.parse_args()
    if sys.platform != "win32":
        sys.exit("Windows + MSFS only")
    dll = Path(args.dll) if args.dll else None
    sim = Sim(dll)
    try:
        mine = sim.title_of(OBJECT_ID_USER, REQ_USER)
        title = args.title or next((t for t in sim.titles(20_000) if t != mine), None) or FSLTL_DEFAULT
        user = sim.position(OBJECT_ID_USER, REQ_USER)
    finally:
        sim.close()
    if user is None:
        sys.exit("Could not read your position")
    lat, lon, alt, _, _, hdg = user
    print(f"Using model {title!r}")
    inj = SimInjector(dll)
    key = "LIGHTS1"
    try:
        slat, slon = moved(lat, lon, hdg, 100.0)
        facing = (hdg + 180.0) % 360.0
        print("Creating it 100 m ahead of you, facing you ...")
        inj.create(key, title, slat, slon, facing, alt - 5.0, type_icao="B738")
        pose = Pose(slat, slon, 0.0, facing)
        t0 = time.monotonic()
        while not inj.objs[key].ready and time.monotonic() - t0 < 15.0:
            inj.wait(0.1)
        if not inj.objs[key].ready:
            sys.exit("   not placed after 15 s (see the exceptions above). Try another --title.")
        print("   placed, released and frozen. All lights off for 4 s ...")
        _set(inj, key, "both", False)
        _hold(inj, key, pose, 4.0)
        for step, mode, what in (("A", "events", "key events only"), ("B", "data", "light simvars only"),
                                 ("C", "both", "events and simvars")):
            print(f"{step}. ALL LIGHTS ON through {what} (12 s) ... watch it")
            _set(inj, key, mode, True)
            _hold(inj, key, pose, 12.0)
            print("   off (4 s)")
            _set(inj, key, "both", False)
            _hold(inj, key, pose, 4.0)
        print("Engines (ENGINE_AUTO_START) and C again (12 s): do lights need the engines running?")
        inj.lights(key, engines=True)
        _set(inj, key, "both", True)
        _hold(inj, key, pose, 12.0)
    finally:
        inj.close()
    print("Done. Tell the IDE session in which step (A, B, C, or only with engines) which lights came on, and copy "
          "this output.")


if __name__ == "__main__":
    main()
