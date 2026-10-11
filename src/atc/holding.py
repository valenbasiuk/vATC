"""Holding, done by code: the pilot asks to hold (practice, a problem to sort out, weather), or the arrival radar
holds an arrival because the sequence is full (improvement plan C2); the hold ends on request, at the expected time,
or earlier once the sequence thins out.

    "request hold at DOTNE"     -> ICAO "hold at DOTNE as published, maintain flight level one zero zero, expect
                                   further clearance at one four three five"
                                   FAA  "hold west of DOTNE as published, maintain one zero thousand, expect further
                                   clearance one four three five"
    (no published hold there)   -> "hold at DOTNE, inbound track two four seven, right turns, ..."
    (three or more arrivals ahead, 30 NM out) -> ICAO "hold at ESKON as published, maintain four thousand feet, expect
                                   approach clearance at one four three five, delay due traffic"
    "request approach" / "ready to leave the hold" / the expected time -> "leave the hold, expect vectors ILS ..."

Published holds come from little_navmap_navigraph.sqlite (`holding`: fix, inbound course, turn direction, leg);
while holding, the arrival radar gives no descent or vectors.
"""

from __future__ import annotations

import re
import time

from atc import phrase
from atc.geo import bearing_deg, compass_point, distance_nm, heading_diff
from atc.models import Airport, Facility, OwnState, Traffic
from atc.readback import _normalize

EFC_MIN = 15  # expect further clearance this long after the hold is given (rounded up to 5 minutes)
_HOLD = re.compile(r"\brequest(?:ing)? (?:a |to )?(?:hold|holding)\b(?: (?:at|over|overhead|on|in))?(?: the)?"
                   r"(?: ([a-z]{2,5}\d{0,2}))?\b")
_LEAVE = re.compile(r"\b(?:request(?:ing)? (?:the )?approach|ready (?:to leave|for (?:the )?approach)|"
                    r"leav(?:e|ing) the hold|request(?:ing)? vectors|ready to continue)\b")
_POINTS = {"N": "north", "NE": "northeast", "E": "east", "SE": "southeast", "S": "south", "SW": "southwest",
           "W": "west", "NW": "northwest"}
_NOT_FIXES = {"pattern", "present", "position", "here", "please", "for", "the", "over"}
SEQUENCE_FULL = 3  # this many arrivals ahead of an IFR arrival: the arrival radar holds it
SEQUENCE_THIN = 1  # ... and lets it go before the expected time once at most this many are left ahead
HOLD_DECIDE_NM = 30.0  # the decision is taken inside this, before the vectors start
HOLD_MIN_NM = 12.0  # (not this close in: then it is sequenced on the final)
ARRIVAL_SLOT_S = 150.0  # each arrival ahead: about this much delay
HOLD_FIX_NM = 40.0  # a holding fix this close to the destination


def published(ident: str, near: tuple[float, float]) -> tuple[float, str] | None:
    """(inbound course, 'L'/'R') of the published hold at `ident` nearest to `near`, from the Navigraph db."""
    from atc import navdb

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


def _efc(own: OwnState, minutes: float = EFC_MIN) -> tuple[str, float]:
    """('1435' as said, seconds from now) for the expected time `minutes` from now (rounded up to 5 minutes): sim
    clock, else this PC's UTC."""
    z = own.zulu_s if own.zulu_s is not None else time.time() % 86400
    target = (int(z) // 60 + int(round(minutes)) + 4) // 5 * 5  # minutes since 00:00, rounded up to 5
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


def give_hold(session, airport: Airport, own: OwnState, name: str, pos: tuple[float, float], now: float,
              reason: str = "request", minutes: float = EFC_MIN, level_ft: int | None = None) -> str:
    """The holding instruction (and the session state that keeps vectors and descents away meanwhile)."""
    from atc.enroute import _heading_words, spoken_fix

    faa = airport.faa
    lvl = level_ft or session.cleared_level_ft or int(round(own.alt_msl_ft / 1000.0) * 1000)
    session.assign_level(lvl, own.alt_msl_ft)
    efc, wait_s = _efc(own, minutes)
    session.holding = name
    session.holding_until = now + wait_s
    session.hold_reason = reason
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
    cs = session.spoken_callsign
    if reason == "request":
        tail = f"expect further clearance {phrase.digits(efc)}" if faa else \
            f"expect further clearance at {phrase.digits(efc)}"
    elif faa:
        tail = f"expect further clearance {phrase.digits(efc)}, due to " + ("traffic" if reason == "sequence"
                                                                           else "runway closure")
    else:  # an arrival held at the approach: the expected approach time
        tail = f"expect approach clearance at {phrase.digits(efc)}, delay due " + \
            ("traffic" if reason == "sequence" else "runway closure")
    return f"{cs}, {hold}, maintain {phrase.level(lvl, airport)}, {tail}."


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
        if session.hold_reason in ("sequence", "runway") and now < (session.holding_until or 0):
            return f"{cs}, unable, continue holding, " + _expect_words(session, airport, own, now) + "."
        return release(session, world, own)
    m = _HOLD.search(norm)
    if m is None:
        return None
    ident = m.group(1) if m.group(1) and m.group(1) not in _NOT_FIXES else None
    found = _fix(session, ident, own)
    if found is None:
        return f"{cs}, say again the holding fix."
    name, pos = found
    return give_hold(session, airport, own, name, pos, now)


def _expect_words(session, airport: Airport, own: OwnState, now: float) -> str:
    """'expect approach clearance at one four three five' for the time already given."""
    left_min = max(0.0, ((session.holding_until or now) - now) / 60.0)
    z = own.zulu_s if own.zulu_s is not None else time.time() % 86400
    target = int(round((z + left_min * 60) / 60.0))
    h, m = divmod(target % 1440, 60)
    words = phrase.digits(f"{h:02d}{m:02d}")
    return f"expect further clearance {words}" if airport.faa else f"expect approach clearance at {words}"


def release(session, world, own: OwnState) -> str:
    """Out of the hold: toward the destination's approach (vectors follow from the arrival radar)."""
    from atc.enroute import _approach, _arrival_runway, _dest

    session.holding = None
    session.holding_until = None
    session.hold_reason = None
    session.vectors_given = session.intercept_given = session.base_given = False
    cs = session.spoken_callsign
    dest = _dest(world, session)
    rwy = _arrival_runway(dest, own, session) if dest is not None else None
    if rwy is None:
        return f"{cs}, leave the hold, resume own navigation."
    return f"{cs}, leave the hold, expect vectors {_approach(dest, rwy)}."


def arrivals_ahead(dest: Airport, rwy, own: OwnState, traffic: list[Traffic]) -> int:
    """How many aircraft will land on `rwy` before the user: on its final, or airborne nearer the field than the user,
    heading for it, below 6000 ft above it (sim AI and our own traffic alike)."""
    from atc.sequence import final_distance

    d_own = distance_nm(own.lat, own.lon, dest.lat, dest.lon)
    n = 0
    for t in traffic:
        if t.on_ground:
            continue
        if final_distance(dest, rwy, t) is not None:
            n += 1
            continue
        d = distance_nm(t.lat, t.lon, dest.lat, dest.lon)
        inbound = heading_diff(t.heading_deg, bearing_deg(t.lat, t.lon, dest.lat, dest.lon)) < 90
        if d < d_own and d <= 20.0 and t.alt_msl_ft - dest.elevation_ft <= 6000 and inbound:
            n += 1
    return n


def hold_fix(session, dest: Airport, own: OwnState) -> tuple[str, tuple[float, float], int | None] | None:
    """(ident, position, minimum altitude) of where to hold an arrival for `dest`: a published hold within
    HOLD_FIX_NM of it (nearest to the aircraft), else the next fix of the filed route that close to it, else the
    route's last fix. None if there is nowhere sensible."""
    from atc import navdb

    holds = [h for h in navdb.holds_near(dest.lat, dest.lon, HOLD_FIX_NM)
             if distance_nm(own.lat, own.lon, h["lat"], h["lon"]) <= 45.0]
    if holds:
        h = min(holds, key=lambda h: distance_nm(own.lat, own.lon, h["lat"], h["lon"]))
        return h["ident"], (h["lat"], h["lon"]), h["min_alt"]
    plan = session.plan
    if plan is None or not plan.fixes:
        return None
    from atc.enroute import next_fix_index

    ahead = plan.fixes[next_fix_index(plan, own):]
    near = [f for f in ahead if distance_nm(f.lat, f.lon, dest.lat, dest.lon) <= HOLD_FIX_NM]
    f = near[0] if near else (plan.fixes[-1] if distance_nm(plan.fixes[-1].lat, plan.fixes[-1].lon,
                                                             dest.lat, dest.lon) <= HOLD_FIX_NM else None)
    return (f.ident, (f.lat, f.lon), None) if f is not None else None


def sequence_event(session, world, own: OwnState, dest_traffic: list[Traffic], now: float) -> str | None:
    """The arrival radar holds an IFR arrival whose sequence is full (SEQUENCE_FULL or more ahead), once per flight,
    30 NM out and before any vectors: "hold at ESKON as published, ..., expect approach clearance at 1435"."""
    from atc.enroute import _arrival_radar, _arrival_runway, _dest

    plan = session.plan
    if plan is None or not plan.is_ifr or own.on_ground or session.holding or session.sequence_hold_done \
            or not session.descent_given or session.vectors_given or session.intercept_given \
            or session.visual is not None or session.emergency is not None:
        return None
    picked = world.pick(own)
    dest = _dest(world, session)
    if picked is None or dest is None or not picked[1].can_reply:
        return None
    airport, facility = picked
    if facility.role not in ("approach", "control", "tower") or not _arrival_radar(world, session, airport, facility) \
            or session.is_first_contact(facility.role):
        return None
    d = distance_nm(own.lat, own.lon, dest.lat, dest.lon)
    if not HOLD_MIN_NM < d <= HOLD_DECIDE_NM:
        return None
    rwy = _arrival_runway(dest, own, session)
    if rwy is None:
        return None
    n = arrivals_ahead(dest, rwy, own, dest_traffic)
    if n < SEQUENCE_FULL:
        return None
    found = hold_fix(session, dest, own)
    if found is None:
        return None
    session.sequence_hold_done = True
    name, pos, min_alt = found
    from atc import terrain

    floor = terrain.floor_at(dest, *pos)
    lvl = max(x for x in (session.cleared_level_ft or 0, floor or 0, min_alt or 0, 1000))
    return give_hold(session, airport, own, name, pos, now, reason="sequence",
                     minutes=max(5.0, n * ARRIVAL_SLOT_S / 60.0), level_ft=terrain.round_up(lvl))


def watcher_event(session, world, own: OwnState, now: float, dest_traffic: list[Traffic] | None = None) -> str | None:
    """Out of the hold at the expected time, or (held for the sequence) as soon as it has thinned out; the hold
    itself when the sequence is full."""
    if not session.holding:
        return sequence_event(session, world, own, dest_traffic or [], now) if dest_traffic is not None else None
    if session.holding_until is None or own.on_ground:
        return None
    picked = world.pick(own)
    if picked is None or not _radar(session, world, *picked):
        return None
    if now >= session.holding_until:
        return release(session, world, own)
    if session.hold_reason == "sequence" and dest_traffic is not None:
        from atc.enroute import _arrival_runway, _dest

        dest = _dest(world, session)
        rwy = _arrival_runway(dest, own, session) if dest is not None else None
        if rwy is not None and arrivals_ahead(dest, rwy, own, dest_traffic) <= SEQUENCE_THIN:
            return release(session, world, own)
    return None


def _radar(session, world, airport: Airport, facility: Facility) -> bool:
    from atc.enroute import _arrival_radar

    return facility.role in ("approach", "departure", "control") or _arrival_radar(world, session, airport, facility)
