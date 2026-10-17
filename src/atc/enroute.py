"""Radar controller work done in code (roadmap 9c, second slice), from the SimBrief navlog and telemetry:

- pilot requests: "request direct ATOVO" (only fixes of the filed route still ahead), "request higher / FL200",
  "request descent", "request vectors", "request visual approach" / "field in sight" (improvement plan C1),
  "request deviation twenty degrees left" (D1);
- controller-initiated, from the watcher (`arrival_event`): descent at the planned top of descent, vectors to a
  point on the extended centerline (a downwind and a base turn when the aircraft comes from the wrong side, as after
  a missed approach), the turn to intercept + approach clearance, landing clearance on final, "welcome to Rosario,
  vacate via ..." when slowing on the runway.
- never below the minimum sector altitude (terrain.py, C4): the arrival altitude is raised to the MSA over the way to
  the intercept point, which then moves out to where the glide path is at that height.

Headings are said magnetic when the airport's `mag_var_deg` is known (true otherwise, like the sim gives them).
"""

from __future__ import annotations

import math
import re

from atc import phrase, terrain
from atc.geo import bearing_deg, distance_nm, heading_diff, offset_nm
from atc.models import Airport, Facility, OwnState, Runway, Traffic
from atc.readback import _normalize, join_digits
from atc.runway import magnetic, session_runway
from atc.sequence import _miles, final_distance, on_runway, runway_status, threshold

VECTORS_NM = 25.0  # the arrival radar starts vectoring inside this
INTERCEPT_POINT_NM = 10.0  # on the extended centerline...
INTERCEPT_OFFSET_NM = 3.0  # ...and this far to the aircraft's side, for a 30-degree intercept
MAX_INTERCEPT_NM = 25.0  # a high minimum altitude moves the intercept point out, never beyond this
GLIDE_FT_PER_NM = 318.0  # 3 degrees
DOWNWIND_DEG = 120.0  # flying this far off the runway heading: on a downwind, a base turn comes before the intercept
BASE_CENTERLINE_NM = 2.0  # on base: the intercept turn this close to the extended centerline
LANDING_CLEARANCE_NM = 6.0  # Tower clears to land on its own inside this
VACATE_KT = 60.0


def spoken_fix(ident: str) -> str:
    """'ATOVO' -> 'ATOVO', 'EZE19' -> 'EZE one niner'."""
    m = re.fullmatch(r"([A-Z]+)(\d*)", ident.upper())
    if not m:
        return ident
    return m.group(1) + (" " + phrase.digits(m.group(2)) if m.group(2) else "")


def _heading_words(true_deg: float, airport: Airport) -> tuple[int, str]:
    mag = magnetic(airport, true_deg) % 360
    h = int(round(mag / 10.0) * 10) % 360 or 360
    return h, phrase.digits(f"{h:03d}")


def next_fix_index(plan, own: OwnState) -> int:
    """Index of the first route fix still ahead: the nearest fix, or the one after it if it is behind us."""
    fixes = plan.fixes
    if not fixes:
        return 0
    near = min(range(len(fixes)), key=lambda i: distance_nm(own.lat, own.lon, fixes[i].lat, fixes[i].lon))
    f = fixes[near]
    behind = heading_diff(bearing_deg(own.lat, own.lon, f.lat, f.lon), own.heading_deg) > 90 \
        and distance_nm(own.lat, own.lon, f.lat, f.lon) > 1.0
    return near + 1 if behind else near


def _dest(world, session) -> Airport | None:
    plan = session.plan
    return world.get(plan.destination) if plan else None


def _arrival_runway(dest: Airport, own: OwnState, session) -> Runway | None:
    return session_runway(dest, own.wind_dir_deg, own.wind_kt, session, "arrival")


def _qnh(own: OwnState, faa: bool) -> str | None:
    if own.qnh_hpa is None:
        return None
    return f"altimeter {phrase.digits(f'{own.qnh_hpa * 0.02953:.2f}')}" if faa else \
        f"QNH {phrase.digits(f'{own.qnh_hpa:.0f}')}"


CENTER_DESCENT_FT = 10000  # area control descends arrivals to this; the arrival radar gives the final altitude


def _arrival_altitude(dest: Airport, session) -> int:
    """Altitude the arrival radar descends to before vectoring: 3000 ft, or about 2500 ft above a high field
    (never the transition altitude itself: in the US that is 18000 ft). Raised to the MSA by `approach_altitude`."""
    return max(3000, int(math.ceil((dest.elevation_ft + 2500) / 1000.0) * 1000))


def intercept_nm(dest: Airport, alt_ft: float | None) -> float:
    """How far out the final is joined at `alt_ft`: where a 3 degree glide path is at that height (10 NM at 3000 ft
    at a field near sea level, 18 NM at 8200 ft at San Rafael), never closer than INTERCEPT_POINT_NM."""
    if alt_ft is None:
        return INTERCEPT_POINT_NM
    d = math.ceil((alt_ft - dest.elevation_ft - 50.0) / GLIDE_FT_PER_NM)
    return float(min(MAX_INTERCEPT_NM, max(INTERCEPT_POINT_NM, d)))


def _side(dest: Airport, rwy: Runway, own: OwnState) -> float:
    _, cross = _signed_cross(dest, rwy, own.lat, own.lon)
    return INTERCEPT_OFFSET_NM if cross >= 0 else -INTERCEPT_OFFSET_NM


def approach_altitude(dest: Airport, rwy: Runway | None, own: OwnState, session) -> int:
    """The altitude to vector at: `_arrival_altitude`, raised to the minimum sector altitude over the way from the
    aircraft to the intercept point and over the point itself (that point moves out as the altitude goes up)."""
    alt = _arrival_altitude(dest, session)
    if rwy is None or rwy.heading_deg is None or not dest.msa:
        return alt
    for _ in range(3):
        p = _point(dest, rwy, -intercept_nm(dest, alt), _side(dest, rwy, own))
        floors = [f for f in (terrain.floor_along(dest, (own.lat, own.lon), p), terrain.floor_at(dest, *p)) if f]
        if not floors or max(floors) <= alt:
            break
        alt = terrain.round_up(max(floors))
    return alt


def _high_ground(own: OwnState) -> bool:
    """Over terrain that area control's FL100 wouldn't clear (grid MORA above it): the first descent waits."""
    from atc.navdb import mora_ft

    mora = mora_ft(own.lat, own.lon)
    return mora is not None and mora > CENTER_DESCENT_FT


def _center_stage(session, dest: Airport, own: OwnState, role: str | None) -> bool:
    """Area control's part of the descent: high aircraft go to FL100 / 10000 ft first (not over high ground)."""
    return role == "control" and not session.center_descent and own.alt_msl_ft > CENTER_DESCENT_FT + 1000 \
        and _arrival_altitude(dest, session) < CENTER_DESCENT_FT and not _high_ground(own)


def descent_due(session, dest: Airport, own: OwnState, role: str | None) -> bool:
    if session.descent_given or not _past_tod(session, dest, own):
        return False
    if role == "control":
        return _center_stage(session, dest, own, role)
    alt = approach_altitude(dest, _arrival_runway(dest, own, session), own, session)
    return own.alt_msl_ft > alt + 1000


def descent_text(session, dest: Airport, own: OwnState, role: str | None = None) -> str:
    if _center_stage(session, dest, own, role):
        session.center_descent = True
        star = session.plan.star if session.plan else None
        if star and dest.faa:  # FAA "descend via": the altitudes on the chart; the radar can't check a single level
            session.cleared_level_ft = session.cleared_dir = None
            return f"{session.spoken_callsign}, descend via the {phrase.procedure(star)} arrival."
        session.assign_level(CENTER_DESCENT_FT, own.alt_msl_ft)
        if star:  # ICAO "descend via (STAR) to (level)"
            return f"{session.spoken_callsign}, descend via {phrase.procedure(star)} arrival to " \
                f"{phrase.level(CENTER_DESCENT_FT, dest)}."
        return f"{session.spoken_callsign}, {phrase.descend(CENTER_DESCENT_FT, dest)}."
    rwy = _arrival_runway(dest, own, session)
    alt = approach_altitude(dest, rwy, own, session)
    q = _qnh(own, dest.country == "US")
    session.descent_given = True
    session.assign_level(alt, own.alt_msl_ft)
    # (asked for early, below the minimum altitude of the way in: never "descend" up to it)
    verb = phrase.descend(alt, dest) if alt < own.alt_msl_ft - 200 else \
        phrase.climb(alt, dest) if alt > own.alt_msl_ft + 200 else f"maintain {phrase.level(alt, dest)}"
    bits = [f"{session.spoken_callsign}, {verb}"]
    if q:
        bits.append(q)
    if rwy is not None:
        bits.append(f"expect vectors {_approach(dest, rwy)}")
    return ", ".join(bits) + "."


def _approach(dest: Airport, rwy: Runway) -> str:
    """'ILS approach runway two zero' / 'RNAV approach runway zero two' (from the sim's navdata), else
    'runway zero two' when the approach types are unknown."""
    from atc.navdb import approach_type

    rw = f"runway {phrase.runway(rwy.ident, dest.country == 'US')}"
    kind = approach_type(dest, rwy.ident)
    if kind and dest.faa:  # FAA order: "ILS runway two eight left approach"
        return f"{kind} {rw} approach"
    return f"{kind} approach {rw}" if kind else rw


def _cleared(app: str) -> str:
    """'cleared ILS Zulu approach runway one three'; without a known approach type 'cleared approach runway ...'."""
    return f"cleared {app}" if app.startswith(("ILS", "RNAV", "VOR", "NDB", "localizer")) else f"cleared approach {app}"


OWN_NAV_CLEAR_NM = 25.0  # flying the procedure on their own: the approach clearance this far out


def own_nav_request(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    """'Request own navigation' / 'request the full procedure' / 'request RNAV approach via DOKMU' on the arrival
    radar: no vectors; "own navigation approved, expect RNAV approach runway zero two"; the approach clearance
    follows inside OWN_NAV_CLEAR_NM ("cleared RNAV approach runway zero two via DOKMU, report established")."""
    if own.on_ground or not _arrival_radar(world, session, airport, facility) or session.plan is None:
        return None
    norm = _normalize(pilot_text)
    if "request" not in norm or not re.search(r"\b(?:own navigation|full procedure|full approach|"
                                              r"no vectors|via [a-z]{2,5}\d{0,2})\b", norm):
        return None
    via = re.search(r"\bvia ([a-z]{2,5}\d{0,2})\b", norm)
    session.own_nav = via.group(1).upper() if via else ""
    session.vectors_given = session.intercept_given = session.base_given = False
    session.visual = None
    session.holding = session.holding_until = session.hold_reason = None  # "ready to leave the hold, request own navigation"
    dest = _dest(world, session)
    named = re.search(r"\brunway (\d{1,2}) ?(left|right|center|l|r|c)?\b", join_digits(norm))
    if named and dest is not None:  # "... RNAV approach runway 02 via DOKMU": that runway too, if the wind allows
        from atc.runway import find_runway, request_ok

        r = find_runway(dest, named.group(1).zfill(2) + (named.group(2) or "")[:1].upper())
        if r is not None and request_ok(r, own.wind_dir_deg, own.wind_kt):
            session.runway_requests[f"{dest.icao}:arrival"] = r.ident
    rwy = _arrival_runway(dest, own, session) if dest is not None else None
    expect = f", expect {_approach(dest, rwy)}" if rwy is not None else ""
    return f"{session.spoken_callsign}, own navigation approved{expect}."


# --- visual approach (improvement plan C1) ---------------------------------------------------------------------------
_IN_SIGHT = re.compile(r"\b(?:field|airport|aerodrome|airfield|runway|runway lights|approach lights|the lights) "
                       r"(?:is |are )?(?:in sight|insight|in site)\b|\b(?:have|got) the (?:field|airport|runway)\b")
VISUAL_FAA_CEILING_FT = 1000  # FAA 7110.65 7-4-2: a visual approach needs at least this ceiling...
VISUAL_FAA_VIS_M = 4800.0  # ...and 3 statute miles


def visual_request(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str,
                   traffic=()) -> str | None:
    """'Request visual approach' / 'field in sight' to the arrival radar (or a Tower doing the approach work):
    ICAO "cleared visual approach runway one three" (FAA the same), in weather that allows it; "report field in sight"
    when the pilot asks before seeing it; "unable visual approach, expect ILS approach ..." in poor weather. From then
    on no vectors and no level watch; the landing clearance comes from Tower as usual. A VFR flight being followed
    that reports the field in sight is handed to its Tower."""
    if own.on_ground:
        return None
    norm = _normalize(pilot_text)
    in_sight = _IN_SIGHT.search(norm) is not None
    asks = "visual" in norm and ("request" in norm or "like" in norm or "?" in pilot_text)
    if not (in_sight or asks):
        return None
    plan = session.plan
    cs = session.spoken_callsign
    if plan is None or not plan.is_ifr:
        return _vfr_in_sight(session, world, facility, own) if in_sight else None
    dest = _dest(world, session)
    if dest is None or not _arrival_radar(world, session, airport, facility) or session.landing_cleared \
            or session.visual is not None:
        return None
    if facility.role == "control" and distance_nm(own.lat, own.lon, dest.lat, dest.lon) > 40.0:
        return None
    if session.intercept_given and not asks:
        return f"{cs}, roger."  # cleared for the instrument approach already: the field in sight is news, no change
    named = re.search(r"\brunway (\d{1,2}) ?(left|right|center|l|r|c)?\b", join_digits(norm))
    if named:  # "request visual approach runway 31": that runway, if the wind allows it
        from atc.runway import find_runway, request_ok

        r = find_runway(dest, named.group(1).zfill(2) + (named.group(2) or "")[:1].upper())
        if r is not None and request_ok(r, own.wind_dir_deg, own.wind_kt):
            if session.runway_requests.get(f"{dest.icao}:arrival") != r.ident:
                session.vectors_given = session.base_given = False
            session.runway_requests[f"{dest.icao}:arrival"] = r.ident
    rwy = _arrival_runway(dest, own, session)
    if rwy is None:
        return None
    rw = phrase.runway(rwy.ident, dest.faa)
    from atc import atis

    cond = atis.conditions(dest, own, session.surface_wind.get(dest.icao))
    if dest.faa:
        poor = (cond.ceiling_ft is not None and cond.ceiling_ft < VISUAL_FAA_CEILING_FT) or \
            (cond.visibility_m is not None and cond.visibility_m < VISUAL_FAA_VIS_M)
    else:  # ICAO: the pilot seeing the field is enough, unless the visibility is very low
        poor = cond.low_visibility or (not in_sight and cond.reduced)
    if poor:
        app = _approach(dest, rwy)
        known = app.startswith(("ILS", "RNAV", "VOR", "NDB", "localizer"))
        return f"{cs}, unable visual approach, expect {app if known else 'instrument approach ' + app}."
    if not in_sight:
        session.visual_asked = True
        return f"{cs}, expect visual approach runway {rw}, " + \
            ("report the airport in sight." if dest.faa else "report field in sight.")
    session.visual = rwy.ident
    session.intercept_given = session.vectors_given = True
    session.own_nav = session.holding = session.holding_until = None
    session.cleared_level_ft = session.cleared_dir = None  # descends on its own, seeing the field
    bits = [f"{cs}, cleared visual approach runway {rw}"]
    ahead = _traffic_ahead(dest, rwy, own, traffic) if traffic else None
    if ahead:
        bits.append(f"number two, follow the {ahead[0]} on {_miles(ahead[1])} final" if ahead[0] != "traffic"
                    else f"number two, traffic on {_miles(ahead[1])} final")
    if facility.role == "tower":  # Tower doing the approach work: the landing clearance comes from it
        bits.append("report final")
    elif distance_nm(own.lat, own.lon, dest.lat, dest.lon) <= 12.0:  # close in: straight to Tower
        from atc.flow import Pending, _contact

        twr = _contact(dest, ("TWR",))
        if twr is not None:
            key = f"{dest.icao}:tower"
            session.handoffs_done.add(key)
            import time

            session.pending_handoff = Pending(key, twr[1], own.com1_mhz, time.monotonic())
            bits.append(twr[1])
    return ", ".join(bits) + "."


def _vfr_in_sight(session, world, facility: Facility, own: OwnState) -> str | None:
    """A VFR flight with flight following reports the field in sight: Tower takes it (or radar service ends)."""
    if not session.following or facility.role not in ("approach", "departure", "control"):
        return None
    near = world.nearest(own)
    if near is None or distance_nm(own.lat, own.lon, near.lat, near.lon) > 25.0:
        return None
    from atc import following

    return following.terminate(session, near)


# --- weather deviations (improvement plan D1) -----------------------------------------------------------------------
_DEVIATION = re.compile(r"\bdeviat(?:ion|ions|e|ing)\b")
_DEV_AMOUNT = re.compile(r"\b(\d{1,3}) (degrees?|miles?|nm|nautical miles?|kilometers?|kilometres?|km)\b")
_DEV_SIDE = re.compile(r"\b(left|right)\b")
_DEV_HEADING = re.compile(r"\b(?:request(?:ing)?|like) (?:a )?heading (\d{3})\b")
_WEATHER = re.compile(r"\b(?:weather|wx|cb|cbs|cumulonimbus|build ?ups?|cells?|storms?|thunderstorms?|turbulence|"
                      r"clouds?|echoes)\b")
_BACK_ON_TRACK = re.compile(r"\b(?:back on (?:course|track|route)|clear of (?:the )?(?:weather|wx|cells?|storms?|"
                            r"build ?ups?)|(?:re ?)?established (?:back )?on (?:course|track|route)|"
                            r"(?:on|back to) (?:course|track|the route))\b")


def deviation_request(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    """'Request deviation twenty degrees left (of track) due weather' / 'request heading 270 for weather' on a radar
    position: ICAO "deviation up to twenty degrees left of track approved, report back on track"; FAA "deviation
    twenty degrees left of course approved, when able proceed direct ESKON". "Back on track" / "clear of weather"
    afterwards: "roger"."""
    if own.on_ground or not (facility.role in ("approach", "departure", "control")
                             or _arrival_radar(world, session, airport, facility)):
        return None
    norm = join_digits(_normalize(pilot_text))
    cs = session.spoken_callsign
    faa = airport.faa
    asks = "request" in norm or "like" in norm or "?" in pilot_text
    if session.deviating and _BACK_ON_TRACK.search(norm) and not asks:
        session.deviating = False
        return f"{cs}, roger."
    if not asks:
        return None
    heading = _DEV_HEADING.search(norm)
    if heading and _WEATHER.search(norm):
        session.deviating = True
        words = phrase.digits(heading.group(1))
        return f"{cs}, heading {words} approved, " + ("advise clear of weather." if faa else "report clear of weather.")
    if not _DEVIATION.search(norm):
        return None
    side = _DEV_SIDE.search(norm)
    amount = _DEV_AMOUNT.search(norm)
    what = ""
    if amount:
        n, unit = int(amount.group(1)), amount.group(2)
        unit = "degrees" if unit.startswith("degree") else "kilometres" if unit.startswith(("kilo", "km")) else "miles"
        what = f" {phrase.digits(str(n))} {unit}"  # numbers said digit by digit: "two zero degrees"
    session.deviating = True
    if faa:
        where = f"{what} {side.group(1)} of course" if side else what
        plan = session.plan
        nxt = None
        if plan is not None and plan.fixes:
            i = next_fix_index(plan, own)
            nxt = plan.fixes[i].ident if i < len(plan.fixes) else None
        tail = f"when able proceed direct {spoken_fix(nxt)}" if nxt else "advise when able to proceed on course"
        return f"{cs}, deviation{where} approved, {tail}."
    if side:
        where = f" up to{what} {side.group(1)} of track" if what else f" {side.group(1)} of track"
    else:
        where = f" up to{what}" if what else ""
    return f"{cs}, deviation{where} approved, report back on track."


def _past_tod(session, dest: Airport, own: OwnState) -> bool:
    plan = session.plan
    d_dest = distance_nm(own.lat, own.lon, dest.lat, dest.lon)
    if plan and plan.tod:
        return d_dest <= distance_nm(plan.tod[0], plan.tod[1], dest.lat, dest.lon) + 2.0
    above = own.alt_msl_ft - _arrival_altitude(dest, session)
    return d_dest <= above / 1000.0 * 3.0 + 10.0  # 3 NM per 1000 ft, plus room to slow down


def _signed_cross(airport: Airport, rwy: Runway, lat: float, lon: float) -> tuple[float, float]:
    """(along from the threshold, signed cross: + = right of the centerline looking in the landing direction)."""
    tlat, tlon = threshold(airport, rwy)
    e, n = offset_nm(tlat, tlon, lat, lon)
    h = math.radians(rwy.heading_deg or 0.0)
    return e * math.sin(h) + n * math.cos(h), e * math.cos(h) - n * math.sin(h)


def _point(airport: Airport, rwy: Runway, along: float, cross: float) -> tuple[float, float]:
    tlat, tlon = threshold(airport, rwy)
    h = math.radians(rwy.heading_deg or 0.0)
    e = along * math.sin(h) + cross * math.cos(h)
    n = along * math.cos(h) - cross * math.sin(h)
    return tlat + n / 60.0, tlon + e / (60.0 * math.cos(math.radians(tlat)))


BIG_TURN_DEG = 60.0  # a vector this far from the present heading is said with its direction ("turn left heading")


def _vector_level(session, dest: Airport) -> int:
    return int(session.cleared_level_ft or _arrival_altitude(dest, session))


def _turn(dest: Airport, own: OwnState, true_hdg: float, side: float = 0.0) -> tuple[str, str, float]:
    """(heading words, 'left'/'right', degrees to turn) for a new heading. A turn of about 180 degrees goes toward
    `side` (+ = right of the final course, where the vectors are)."""
    hval, words = _heading_words(true_hdg, dest)
    delta = (hval - magnetic(dest, own.heading_deg)) % 360
    turn = "left" if delta > 180 else "right"
    if 150 <= delta <= 210 and side:
        turn = "right" if side > 0 else "left"
    return words, turn, min(delta, 360 - delta)


def vectors_text(session, dest: Airport, rwy: Runway, own: OwnState) -> str:
    """'fly heading 340, vectors ILS approach runway 02' toward a point out on the extended centerline (10 NM, farther
    when the minimum altitude is high), on our side; a big turn says its direction ("turn left heading 130", after a
    missed approach). If the way there crosses higher ground than the level given, the level goes up first."""
    lvl = _vector_level(session, dest)
    side = _side(dest, rwy, own)
    plat, plon = _point(dest, rwy, -intercept_nm(dest, lvl), side)
    words, turn, delta = _turn(dest, own, bearing_deg(own.lat, own.lon, plat, plon), side)
    session.vectors_given = True
    session.base_given = False
    bits = [f"{session.spoken_callsign}, " + (f"turn {turn} heading {words}" if delta > BIG_TURN_DEG
                                              else f"fly heading {words}")]
    need = terrain.floor_along(dest, (own.lat, own.lon), (plat, plon))
    if need is not None and need > lvl:  # never vectored below the minimum sector altitude
        need = terrain.round_up(need)
        session.assign_level(need, own.alt_msl_ft)
        bits.append(phrase.climb(need, dest) if need > own.alt_msl_ft + 200 else f"maintain {phrase.level(need, dest)}")
    bits.append(f"vectors {_approach(dest, rwy)}")
    speed = _speed_text(session, own, VECTORS_KT, dest)
    if speed:
        bits.append(speed)
    return ", ".join(bits) + "."


def base_text(session, dest: Airport, rwy: Runway, own: OwnState) -> str:
    """On a downwind (coming back after a missed approach, or from the far side), abeam the intercept point: the turn
    onto base, square to the final course ('turn left heading 040'); the intercept follows near the centerline."""
    _, cross = _signed_cross(dest, rwy, own.lat, own.lon)
    true_base = (rwy.heading_deg - 90) % 360 if cross > 0 else (rwy.heading_deg + 90) % 360
    words, turn, _ = _turn(dest, own, true_base)
    session.base_given = True
    return f"{session.spoken_callsign}, turn {turn} heading {words}."


def _on_downwind(rwy: Runway, own: OwnState) -> bool:
    return heading_diff(own.heading_deg, rwy.heading_deg or 0.0) > DOWNWIND_DEG


def _due_for_base(session, dest: Airport, rwy: Runway, own: OwnState) -> bool:
    along, _ = _signed_cross(dest, rwy, own.lat, own.lon)
    return _on_downwind(rwy, own) and along <= -(intercept_nm(dest, _vector_level(session, dest)) - 1.0)


VECTORS_KT = 210  # speed on vectors
INTERCEPT_KT = 180  # speed with the approach clearance
SPACING_KT = 160  # with traffic on final ahead closer than SPACING_NM
SPACING_NM = 6.0


def _speed_text(session, own: OwnState, kt: int, airport: Airport) -> str | None:
    """'reduce speed to two one zero knots' (FAA without 'knots') when the aircraft is faster than that (indicated
    airspeed, else ground speed) and hasn't been given this speed or a lower one already."""
    now = own.ias_kt if own.ias_kt is not None else own.gs_kt
    if now <= kt + 10 or (session.speed_assigned is not None and session.speed_assigned <= kt) \
            or session.emergency is not None:  # an aircraft in an emergency flies its own speeds
        return None
    session.speed_assigned = kt
    return f"reduce speed to {phrase.digits(str(kt))}" + ("" if airport.faa else " knots")


def _traffic_ahead(dest: Airport, rwy: Runway, own: OwnState, traffic) -> tuple[str, float] | None:
    """(spoken type or 'traffic', NM final) of an aircraft on final ahead that will be closer than SPACING_NM to us
    once we are on the same final, or None."""
    from atc.traffic import spoken_type

    along, _ = _signed_cross(dest, rwy, own.lat, own.lon)
    ours = max(-along, 0.0)
    best = None
    for t in traffic:
        d = final_distance(dest, rwy, t)
        if d is not None and d < ours and ours - d < SPACING_NM and (best is None or d > best[1]):
            best = (spoken_type(getattr(t, "type", None)) or "traffic", d)
    return best


def _rvr(session, dest: Airport, rwy: Runway, own: OwnState) -> str | None:
    """The runway's RVR when it is low ("RVR five five zero metres", improvement plan A5), else None."""
    from atc import atis

    return atis.rvr_words(dest, rwy.ident, own, session.surface_wind.get(dest.icao))


def intercept_text(session, dest: Airport, rwy: Runway, own: OwnState, traffic=()) -> str:
    """Turn onto a 30-degree intercept (or straight in if already lined up) and the approach clearance, with the
    speed: 180 kt, or 160 kt and the traffic to follow when someone is on final close ahead; the RVR when low."""
    _, cross = _signed_cross(dest, rwy, own.lat, own.lon)
    cleared = _cleared(_approach(dest, rwy))
    alt = phrase.level(int(session.cleared_level_ft or _arrival_altitude(dest, session)), dest)
    session.intercept_given = True
    cs = session.spoken_callsign
    ahead = _traffic_ahead(dest, rwy, own, traffic)
    side = _sidestep_runway(dest, rwy, own, traffic) if ahead and session.emergency is None else None
    if side is not None:  # traffic close ahead, the parallel free: land there (improvement plan C3)
        session.runway_requests[f"{dest.icao}:arrival"] = side.ident
        cleared += f", side-step to runway {phrase.runway(side.ident, dest.faa)}"
        ahead = None
    rvr = _rvr(session, dest, side or rwy, own)
    if rvr:
        cleared += f", {rvr}"
    speed = _speed_text(session, own, SPACING_KT if ahead else INTERCEPT_KT, dest)
    tail = (f", {speed}" if speed else "") + \
        (f", traffic to follow, {ahead[0]} on {_miles(ahead[1])} final" if ahead else "")
    if abs(cross) < 0.5 and heading_diff(own.heading_deg, rwy.heading_deg) <= 20:
        return f"{cs}, {cleared}{tail}, report established."
    true_int = (rwy.heading_deg - 30) % 360 if cross > 0 else (rwy.heading_deg + 30) % 360
    hdg, turn, _ = _turn(dest, own, true_int)
    return f"{cs}, turn {turn} heading {hdg}, maintain {alt} until established, {cleared}{tail}."


def _sidestep_runway(dest: Airport, rwy: Runway, own: OwnState, traffic) -> Runway | None:
    """A parallel landing runway of the configuration in use with nobody close ahead on its final (FAA airports with
    side-step minima: KSFO 28L/28R, JFK...), or None."""
    if not dest.faa:
        return None
    from atc.flow import parallels
    from atc.runway import runways_in_use

    landing = {r.ident for r in runways_in_use(dest, own.wind_dir_deg, own.wind_kt, "arrival")}
    for p in parallels(dest, rwy):
        if p.ident in landing and _traffic_ahead(dest, p, own, traffic) is None:
            return p
    return None


def _due_for_intercept(dest: Airport, rwy: Runway, own: OwnState, session=None) -> bool:
    """Time to turn onto the final: near the intercept point (or lined up with the final anyway); after a base turn,
    once close to the extended centerline. Never from a downwind heading (that gets the base turn first)."""
    if _on_downwind(rwy, own):
        return False
    along, cross = _signed_cross(dest, rwy, own.lat, own.lon)
    d = intercept_nm(dest, _vector_level(session, dest)) if session is not None else INTERCEPT_POINT_NM
    lined_up = -(d + 5.0) <= along <= -3.0 and abs(cross) <= 2.0 and heading_diff(own.heading_deg, rwy.heading_deg) <= 60
    if session is not None and session.base_given:
        return lined_up or (along < -1.0 and abs(cross) <= BASE_CENTERLINE_NM)
    plat, plon = _point(dest, rwy, -d, INTERCEPT_OFFSET_NM if cross >= 0 else -INTERCEPT_OFFSET_NM)
    return distance_nm(own.lat, own.lon, plat, plon) <= 3.0 or lined_up


def _arrival_radar(world, session, airport: Airport, facility: Facility) -> bool:
    """Who does descent and vectors: a radar position, or the destination Tower when it has no Approach."""
    dest = _dest(world, session)
    if facility.role in ("approach", "departure", "control"):
        return True
    return facility.role == "tower" and dest is not None and airport.icao == dest.icao \
        and not any(f.kind in ("APP", "ARR") for f in dest.frequencies)


# --- pilot requests ----------------------------------------------------------------------------------

def handle_request(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    """Directs, levels, descent and vectors asked for by the pilot. None = not one of these."""
    plan = session.plan
    if plan is None or own.on_ground or not _arrival_radar(world, session, airport, facility):
        return None
    norm = _normalize(pilot_text)
    cs = session.spoken_callsign
    dest = _dest(world, session)
    direct = re.search(r"\bdirect (?:to )?([a-z]{2,5}\d{0,2})\b", norm)
    if direct and ("request" in norm or "?" in pilot_text):
        ident = direct.group(1).upper()
        spoken = spoken_fix(ident)
        idx = next((i for i, f in enumerate(plan.fixes) if f.ident == ident), None)
        if idx is None:
            # off the filed route: approve it if the sim's navdata knows the fix and it takes you closer to the
            # destination (a controller shortcutting you), else continue as filed
            from atc.navdb import find_fix

            pos = find_fix(ident, (own.lat, own.lon))
            if pos is not None and dest is not None and distance_nm(*pos, dest.lat, dest.lon) < \
                    distance_nm(own.lat, own.lon, dest.lat, dest.lon) - 5:
                session.direct_to = ident
                return f"{cs}, proceed direct {spoken}."
            return f"{cs}, unable direct {spoken}, continue as filed."
        if idx < next_fix_index(plan, own):
            return f"{cs}, {spoken} is behind you, continue as filed."
        session.direct_to = ident
        return f"{cs}, proceed direct {spoken}."
    if "request" not in norm and "?" not in pilot_text:
        return None
    if re.search(r"\b(descent|descend|lower)\b", norm) and dest is not None:
        if _past_tod(session, dest, own) or (plan.tod and distance_nm(own.lat, own.lon, *plan.tod) <= 10):
            return descent_text(session, dest, own, facility.role)
        n = max(5, int(round(distance_nm(own.lat, own.lon, *plan.tod) / 5.0) * 5)) if plan.tod else None
        return f"{cs}, expect descent in {phrase.digits(str(n))} miles." if n else f"{cs}, expect descent later."
    if re.search(r"\b(higher|climb|level)\b", norm) and plan.cruise_ft and not session.descent_given:
        asked = re.search(r"\b(?:flight level|level|fl) (\d{2,3})\b",
                          re.sub(r"(?<=\d) (?=\d)", "", re.sub(r"(?<=[a-z])(?=\d)", " ", norm)))
        want = min(int(asked.group(1)) * 100, plan.cruise_ft) if asked else plan.cruise_ft
        origin = world.get(session.departed_from) or airport  # its transition altitude: FL or feet
        if own.alt_msl_ft >= want - 300:
            return f"{cs}, maintain {phrase.level(int(round(own.alt_msl_ft / 1000.0) * 1000), origin)}."
        session.assign_level(want, own.alt_msl_ft)
        return f"{cs}, {phrase.climb(want, origin)}."
    if "vector" in norm and dest is not None:
        rwy = _arrival_runway(dest, own, session)
        if rwy is None:
            return None
        session.own_nav = None  # back to vectors after "own navigation"
        if not session.descent_given and own.alt_msl_ft > approach_altitude(dest, rwy, own, session) + 1000:
            return descent_text(session, dest, own, facility.role)
        if session.vectors_given and _due_for_base(session, dest, rwy, own):
            return base_text(session, dest, rwy, own)
        if _due_for_intercept(dest, rwy, own, session):
            return intercept_text(session, dest, rwy, own)
        return vectors_text(session, dest, rwy, own)
    return None


# --- controller-initiated, from the watcher ----------------------------------------------------------

def arrival_event(session, world, own: OwnState, traffic: list[Traffic]) -> str | None:
    """Descent, vectors, intercept, landing clearance, vacate: whichever is due now (one per tick), or None."""
    plan = session.plan
    picked = world.pick(own)
    if picked is None or not picked[1].can_reply:
        return None
    airport, facility = picked
    cs = session.spoken_callsign
    # on the runway after landing: welcome + vacate (IFR and VFR; a touch and go stays on the runway)
    if facility.role == "tower" and own.on_ground and session.landed and session.landed_at == airport.icao \
            and own.gs_kt < VACATE_KT and not session.vacate_given \
            and session.circuit_intention not in ("touch and go", "circuits"):
        rwy = next((r for r in airport.runways if on_runway(airport, r, own)
                    and heading_diff(own.heading_deg, r.heading_deg or 0) < 60), None)
        if rwy is not None:
            session.vacate_given = True
            from atc.taxi import vacate

            from atc.airports.gen import spoken_name

            name = airport.spoken_name or spoken_name(airport.name) or airport.name
            net = world.taxi.get(airport.icao)
            exit_ = vacate(net, airport, rwy, own)
            if airport.faa:  # FAA: no welcome; the exit and Ground together ("turn left at Bravo, contact ground")
                from atc.flow import _contact
                from atc.taxi import exit_side

                gnd = _contact(airport, ("GND", "RMP"))
                if gnd is not None:
                    session.handoffs_done.add(f"{airport.icao}:ground")
                tail = f", {gnd[1]}" if gnd else ""
                if exit_ is None or exit_[1]:
                    return f"{cs}, turn off when able{tail}."
                side = exit_side(net, airport, rwy, exit_[0])
                return f"{cs}, turn {side} at {exit_[0]}{tail}." if side else f"{cs}, exit at {exit_[0]}{tail}."
            if exit_ is None:
                return f"{cs}, welcome to {name}, vacate the runway when able."
            via, backtrack = exit_
            if backtrack:
                return f"{cs}, welcome to {name}, backtrack runway {phrase.runway(rwy.ident)}, vacate via {via}."
            return f"{cs}, welcome to {name}, vacate via {via} when able."
    dest = _dest(world, session) if plan is not None else None
    if own.on_ground or dest is None:
        return None
    rwy = _arrival_runway(dest, own, session)
    # landing clearance on final, from the destination Tower
    if facility.role == "tower" and airport.icao == dest.icao and rwy is not None and not session.landing_cleared:
        st = runway_status(dest, rwy, own, traffic)
        if st.own_final_nm is not None and st.own_final_nm <= LANDING_CLEARANCE_NM and st.landing_blocked() is None:
            session.landing_cleared = True
            wind = phrase.wind(magnetic(dest, own.wind_dir_deg), own.wind_kt, dest.faa)
            rvr = _rvr(session, dest, rwy, own)
            return f"{cs}, " + (f"{wind}, " if wind else "") + \
                f"runway {phrase.runway(rwy.ident, dest.country == 'US')}, cleared to land" + (f", {rvr}." if rvr else ".")
    if not _arrival_radar(world, session, airport, facility):
        return None
    if session.is_first_contact(facility.role) or session.holding:
        return None  # let the pilot check in first; in the hold: nothing until it is left (holding.py)
    # descent at the planned top of descent (area control: to FL100 first; the arrival radar: the final altitude)
    if descent_due(session, dest, own, facility.role):
        return descent_text(session, dest, own, facility.role)
    if rwy is None or final_distance(dest, rwy, own) is not None:
        return None
    d_dest = distance_nm(own.lat, own.lon, dest.lat, dest.lon)
    if session.own_nav is not None:  # flying the procedure on their own: no vectors, the approach clearance at 25 NM
        if session.descent_given and not session.intercept_given and d_dest <= OWN_NAV_CLEAR_NM:
            session.intercept_given = True
            via = f" via {spoken_fix(session.own_nav)}" if session.own_nav else ""
            return f"{session.spoken_callsign}, {_cleared(_approach(dest, rwy))}{via}, report established."
        return None
    if session.descent_given and not session.intercept_given and d_dest <= VECTORS_NM + 10:
        if session.vectors_given and not session.base_given and _due_for_base(session, dest, rwy, own):
            return base_text(session, dest, rwy, own)  # downwind abeam the intercept point: the base turn
        if _due_for_intercept(dest, rwy, own, session):
            return intercept_text(session, dest, rwy, own, traffic)
        if not session.vectors_given and d_dest <= VECTORS_NM:
            return vectors_text(session, dest, rwy, own)
    return None


def checkin_extra(session, world, airport: Airport, facility: Facility, own: OwnState) -> list[str]:
    """What the destination's radar adds to "radar contact" on first contact: the descent if it is due."""
    dest = _dest(world, session)
    if dest is None or own.on_ground or session.descent_given or not _arrival_radar(world, session, airport, facility):
        return []
    if descent_due(session, dest, own, facility.role):
        return [descent_text(session, dest, own, facility.role).split(", ", 1)[1].rstrip(".")]
    return []
