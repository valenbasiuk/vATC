"""Holding, done by code: the pilot asks to hold (practice, a problem to sort out, weather), the radar controller
gives the hold, and releases it on request or at the expected further clearance time.

    "request hold at DOTNE"     -> ICAO "hold at DOTNE as published, maintain flight level one zero zero, expect
                                   further clearance at one four three five"
                                   FAA  "hold west of DOTNE as published, maintain one zero thousand, expect further
                                   clearance one four three five"
    (no published hold there)   -> "hold at DOTNE, inbound track two four seven, right turns, ..."
    "request approach" / "ready to leave the hold" / the EFC time -> "leave the hold, expect vectors ILS ..."

Published holds come from little_navmap_navigraph.sqlite (`holding`: fix, inbound course, turn direction, leg);
while holding, the arrival radar gives no descent or vectors.
"""

from __future__ import annotations

import re
import time

from atc import phrase
from atc.geo import bearing_deg, compass_point
from atc.models import Airport, Facility, OwnState
from atc.readback import _normalize

EFC_MIN = 15  # expect further clearance this long after the hold is given (rounded up to 5 minutes)
_HOLD = re.compile(r"\brequest(?:ing)? (?:a |to )?(?:hold|holding)\b(?: (?:at|over|overhead|on|in))?(?: the)?"
                   r"(?: ([a-z]{2,5}\d{0,2}))?\b")
_LEAVE = re.compile(r"\b(?:request(?:ing)? (?:the )?approach|ready (?:to leave|for (?:the )?approach)|"
                    r"leav(?:e|ing) the hold|request(?:ing)? vectors|ready to continue)\b")
_POINTS = {"N": "north", "NE": "northeast", "E": "east", "SE": "southeast", "S": "south", "SW": "southwest",
           "W": "west", "NW": "northwest"}
_NOT_FIXES = {"pattern", "present", "position", "here", "please", "for", "the", "over"}


def published(ident: str, near: tuple[float, float]) -> tuple[float, str] | None:
    """(inbound course, 'L'/'R') of the published hold at `ident` nearest to `near`, from the Navigraph db."""
    from atc import navdb
    from atc.geo import distance_nm

    path = navdb.navigraph_path()
    con = navdb._connect(str(path)) if path else None
    if con is None:
        return None
    best = None
    for course, turn, lat, lon in con.execute(
            "select course, turn_direction, laty, lonx from holding where nav_ident=?", (ident.upper(),)):
        d = distance_nm(near[0], near[1], lat, lon)
        if d <= 50 and course is not None and (best is None or d < best[0]):
            best = (d, float(course), turn or "R")
    return (best[1], best[2]) if best else None


def _efc(own: OwnState) -> tuple[str, float]:
    """('1435' as said, seconds from now) for the expect-further-clearance time: sim clock, else this PC's UTC."""
    z = own.zulu_s if own.zulu_s is not None else time.time() % 86400
    target = (int(z) // 60 + EFC_MIN + 4) // 5 * 5  # minutes since 00:00, rounded up to 5
    h, m = divmod(target % 1440, 60)
    return f"{h:02d}{m:02d}", target * 60 - z


def _fix(session, ident: str | None, own: OwnState) -> tuple[str, tuple[float, float]] | None:
    plan = session.plan
    if ident:
        f = next((f for f in (plan.fixes if plan else []) if f.ident == ident.upper()), None)
        if f is not None:
            return f.ident, (f.lat, f.lon)
        from atc import navdb

        pos = navdb.find_fix(ident, (own.lat, own.lon), max_nm=150)
        return (ident.upper(), pos) if pos else None
    if plan is not None and plan.fixes:  # no fix named: the next one on the route
        from atc.enroute import next_fix_index

        i = next_fix_index(plan, own)
        if i < len(plan.fixes):
            f = plan.fixes[i]
            return f.ident, (f.lat, f.lon)
    return None


def handle(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str,
           now: float | None = None) -> str | None:
    """The hold (or leaving it) on request, from a radar position (or a Tower doing the approach work, Rosario).
    None = not a holding call."""
    if own.on_ground or not _radar(session, world, airport, facility):
        return None
    norm = _normalize(pilot_text)
    cs = session.spoken_callsign
    now = time.monotonic() if now is None else now
    if session.holding and _LEAVE.search(norm):
        return release(session, world, own)
    m = _HOLD.search(norm)
    if m is None:
        return None
    ident = m.group(1) if m.group(1) and m.group(1) not in _NOT_FIXES else None
    found = _fix(session, ident, own)
    if found is None:
        return f"{cs}, say again the holding fix."
    name, pos = found
    from atc.enroute import _heading_words, spoken_fix

    faa = airport.faa
    lvl = session.cleared_level_ft or int(round(own.alt_msl_ft / 1000.0) * 1000)
    session.assign_level(lvl, own.alt_msl_ft)
    efc, wait_s = _efc(own)
    session.holding = name
    session.holding_until = now + wait_s
    pub = published(name, pos)
    spoken = spoken_fix(name)
    if pub is not None:
        course, turn = pub
        side = _POINTS[compass_point((course + 180) % 360)]  # the hold lies on the far side of the inbound course
        hold = f"hold {side} of {spoken} as published" if faa else f"hold at {spoken} as published"
    else:
        _, words = _heading_words(bearing_deg(own.lat, own.lon, *pos), airport)
        side = _POINTS[compass_point(bearing_deg(*pos, own.lat, own.lon))]
        hold = (f"hold {side} of {spoken}" if faa else f"hold at {spoken}") + f", inbound track {words}, right turns"
    tail = f"expect further clearance {phrase.digits(efc)}" if faa else \
        f"expect further clearance at {phrase.digits(efc)}"
    return f"{cs}, {hold}, maintain {phrase.level(lvl, airport)}, {tail}."


def release(session, world, own: OwnState) -> str:
    """Out of the hold: toward the destination's approach (vectors follow from the arrival radar)."""
    from atc.enroute import _approach, _arrival_runway, _dest

    session.holding = None
    session.holding_until = None
    session.vectors_given = session.intercept_given = False
    cs = session.spoken_callsign
    dest = _dest(world, session)
    rwy = _arrival_runway(dest, own, session) if dest is not None else None
    if rwy is None:
        return f"{cs}, leave the hold, resume own navigation."
    return f"{cs}, leave the hold, expect vectors {_approach(dest, rwy)}."


def watcher_event(session, world, own: OwnState, now: float) -> str | None:
    """At the expect-further-clearance time the controller takes the aircraft out of the hold by itself."""
    if not session.holding or session.holding_until is None or now < session.holding_until or own.on_ground:
        return None
    picked = world.pick(own)
    if picked is None or not _radar(session, world, *picked):
        return None
    return release(session, world, own)


def _radar(session, world, airport: Airport, facility: Facility) -> bool:
    from atc.enroute import _arrival_radar

    return facility.role in ("approach", "departure", "control") or _arrival_radar(world, session, airport, facility)
