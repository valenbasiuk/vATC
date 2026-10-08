"""One runway controller for everybody (improvement plan B3/B4): the departure queue at each runway and the last
departure / landing, shared by the user's clearances (flow.py, pattern.py) and our own traffic (own/manager.py).

- Queue: whoever reported ready first at the holding point goes first ("number two for departure").
- Wake turbulence: the next takeoff waits WAKE_GAP_S behind the last departure from that runway (60 s, 2 min
  behind a heavy, 3 min behind an A380) and is told "caution wake turbulence" behind a heavy; a landing behind a
  heavy too.
Times are the wall clock (time.monotonic). The user is USER.
"""

from __future__ import annotations

import time

from atc import phrase

USER = "(you)"
QUEUE: dict[str, dict[str, tuple[float, int, str]]] = {}  # ICAO -> callsign -> (ready since, order, runway)
_order = [0]  # ties on the clock (Windows ticks are ~15 ms): whoever joined first
LAST_DEPARTURE: dict[str, dict[str, tuple[float, str, str | None]]] = {}  # ICAO -> runway -> (airborne at, cs, type)
LAST_LANDING: dict[str, dict[str, tuple[float, str, str | None]]] = {}  # ICAO -> runway -> (touchdown at, cs, type)
STALE_S = 900.0  # somebody "ready" this long ago and still not gone: forgotten

_HEAVY = ("A30", "A33", "A34", "A35", "A38", "B74", "B76", "B77", "B78", "MD11", "DC10", "IL96", "A124", "KC1",
          "KC4", "A400")
_HEAVY_EXACT = ("A310", "C17", "C5", "C5M")
_SUPER = ("A38", "A225")


def wake(type_icao: str | None) -> str:
    """ICAO wake category: "J" (super), "H" (heavy), "M" (medium, the default), "L" (light)."""
    t = (type_icao or "").upper()
    if t.startswith(_SUPER):
        return "J"
    if t.startswith(_HEAVY) or t in _HEAVY_EXACT:
        return "H"
    if t.startswith(("C1", "C2", "P28", "PA", "SR2", "DA4", "BE", "C208", "PC12", "TB", "M20")):
        return "L"
    return "M"


def wake_gap_s(prev_type: str | None, next_type: str | None) -> float:
    """Departure interval behind `prev_type` from the same runway."""
    prev, nxt = wake(prev_type), wake(next_type)
    if prev == "J" and nxt != "J":
        return 180.0
    if prev in ("J", "H") and nxt in ("M", "L"):
        return 120.0
    return 60.0


CLOCK = time.monotonic  # what "now" is; a simulation run faster than real time swaps it for its own clock


def _now(now: float | None) -> float:
    return CLOCK() if now is None else now


def ready(icao: str, callsign: str, runway: str, now: float | None = None) -> None:
    """Reported ready (or arrived at the holding point): joins the queue, keeping its place if already in it."""
    q = QUEUE.setdefault(icao, {})
    if callsign not in q or q[callsign][2] != runway:
        _order[0] += 1
        q[callsign] = (_now(now), _order[0], runway)


def gone(icao: str, callsign: str) -> None:
    QUEUE.get(icao, {}).pop(callsign, None)


def ahead(icao: str, callsign: str, runway: str, now: float | None = None) -> list[str]:
    """Who is before `callsign` in the queue for `runway` (in order)."""
    now = _now(now)
    q = QUEUE.get(icao, {})
    for cs in [c for c, (t, _, _) in q.items() if now - t > STALE_S]:
        q.pop(cs, None)
    mine = q.get(callsign)
    if mine is None:
        return []
    return [c for c, (t, n, r) in sorted(q.items(), key=lambda kv: kv[1][:2]) if r == runway and (t, n) < mine[:2]]


def airborne(icao: str, runway: str, callsign: str, type_icao: str | None, now: float | None = None) -> None:
    LAST_DEPARTURE.setdefault(icao, {})[runway] = (_now(now), callsign, type_icao)
    gone(icao, callsign)


def landed(icao: str, runway: str, callsign: str, type_icao: str | None, now: float | None = None) -> None:
    LAST_LANDING.setdefault(icao, {})[runway] = (_now(now), callsign, type_icao)


def gap_left(icao: str, runway: str, type_icao: str | None, now: float | None = None) -> float:
    """Seconds until a departure of `type_icao` may roll behind the last one from `runway` (0 = now)."""
    last = LAST_DEPARTURE.get(icao, {}).get(runway)
    if last is None:
        return 0.0
    return max(0.0, last[0] + wake_gap_s(last[2], type_icao) - _now(now))


def wake_caution(icao: str, runway: str, type_icao: str | None, now: float | None = None,
                 landing: bool = False) -> str | None:
    """'caution wake turbulence' when a heavier one departed (or, landing, landed) from `runway` in the last 3
    minutes."""
    table = LAST_LANDING if landing else LAST_DEPARTURE
    last = table.get(icao, {}).get(runway)
    if last is None or _now(now) - last[0] > 180.0:
        return None
    if wake(last[2]) in ("J", "H") and wake(type_icao) in ("M", "L") or \
            (wake(last[2]) == "M" and wake(type_icao) == "L"):
        return "caution wake turbulence"
    return None


def minutes_words(seconds: float) -> str:
    m = max(1, round(seconds / 60.0))
    return f"{phrase.digits(str(m))} minute" + ("s" if m > 1 else "")


def reset() -> None:
    QUEUE.clear()
    LAST_DEPARTURE.clear()
    LAST_LANDING.clear()
