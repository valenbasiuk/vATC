"""Our own traffic (docs/OWN_TRAFFIC_PLAN.md): aircraft this app creates in the sim and moves itself, so they do
what this ATC tells them. The sim's AI flies its own aircraft and ignores this ATC.

    catalog.py   which model (title) to create for an airline + type, from FSLTL / FS Traffic on Valen's PC
    motion.py    kinematics: a path on the ground, a takeoff, a climb -> position / heading / pitch each moment
    pilot.py     one aircraft's flight, step by step, each step waiting for its ATC clearance
    injector.py  the sim side: create, release from the sim's AI, set the position many times a second, remove
    manager.py   the aircraft alive now, the update thread, spawning

This module is the registry the rest of the app reads: the traffic list from the sim (or the fake) shows our
aircraft with the state the manager knows (a frozen sim object reports 0 kt ground speed), and the AI tracker
leaves them alone (their radio calls come from their pilot, not from guessing what they do).
"""

from __future__ import annotations

from atc.geo import distance_nm
from atc.models import Traffic

OWN: dict[str, Traffic] = {}  # callsign -> current state; the manager replaces the whole dict each update
APRON: dict[str, int] = {}  # ICAO -> our departures between push and takeoff (the user's push waits if many)


def publish(states: dict[str, Traffic]) -> None:
    global OWN
    OWN = dict(states)


def is_own(callsign: str | None) -> bool:
    return bool(callsign) and callsign in OWN


DUPLICATE_M = 20.0


def merge(traffic: list[Traffic], center_lat: float, center_lon: float, radius_nm: float) -> list[Traffic]:
    """The sim's traffic list with our aircraft as the manager knows them (sim records of them dropped: by callsign,
    and anything sitting right on one of ours, in case the sim reports another ATC id for it. VERIFY)."""
    ours = OWN
    out = [t for t in traffic if t.callsign not in ours
           and not any(distance_nm(t.lat, t.lon, o.lat, o.lon) * 1852.0 < DUPLICATE_M for o in ours.values())]
    out += [t for t in ours.values() if distance_nm(center_lat, center_lon, t.lat, t.lon) <= radius_nm]
    return out
