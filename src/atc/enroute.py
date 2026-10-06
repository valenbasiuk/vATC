"""Radar controller work done in code (roadmap 9c, second slice), from the SimBrief navlog and telemetry:

- pilot requests: "request direct ATOVO" (only fixes of the filed route still ahead), "request higher / FL200",
  "request descent", "request vectors";
- controller-initiated, from the watcher (`arrival_event`): descent at the planned top of descent, vectors to a
  point on the extended centerline, the turn to intercept + approach clearance, landing clearance on final,
  "welcome to Rosario, vacate via ..." when slowing on the runway.

Headings are said magnetic when the airport's `mag_var_deg` is known (true otherwise, like the sim gives them).
"""

from __future__ import annotations

import math
import re

from atc import phrase
from atc.geo import bearing_deg, distance_nm, heading_diff, offset_nm
from atc.models import Airport, Facility, OwnState, Runway, Traffic
from atc.readback import _normalize
from atc.runway import magnetic, runway_in_use
from atc.sequence import _miles, final_distance, on_runway, runway_status, threshold

VECTORS_NM = 25.0  # the arrival radar starts vectoring inside this
INTERCEPT_POINT_NM = 10.0  # on the extended centerline...
INTERCEPT_OFFSET_NM = 3.0  # ...and this far to the aircraft's side, for a 30-degree intercept
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
    plan = session.plan
    return runway_in_use(dest, own.wind_dir_deg, own.wind_kt, plan.dest_runway if plan else None, use="arrival")


def _qnh(own: OwnState, faa: bool) -> str | None:
    if own.qnh_hpa is None:
        return None
    return f"altimeter {phrase.digits(f'{own.qnh_hpa * 0.02953:.2f}')}" if faa else \
        f"QNH {phrase.digits(f'{own.qnh_hpa:.0f}')}"


CENTER_DESCENT_FT = 10000  # area control descends arrivals to this; the arrival radar gives the final altitude


def _arrival_altitude(dest: Airport, session) -> int:
    """Altitude the arrival radar descends to before vectoring: 3000 ft, or about 2500 ft above a high field
    (never the transition altitude itself: in the US that is 18000 ft)."""
    return max(3000, int(math.ceil((dest.elevation_ft + 2500) / 1000.0) * 1000))


def _center_stage(session, dest: Airport, own: OwnState, role: str | None) -> bool:
    """Area control's part of the descent: high aircraft go to FL100 / 10000 ft first."""
    return role == "control" and not session.center_descent and own.alt_msl_ft > CENTER_DESCENT_FT + 1000 \
        and _arrival_altitude(dest, session) < CENTER_DESCENT_FT


def descent_due(session, dest: Airport, own: OwnState, role: str | None) -> bool:
    if session.descent_given or not _past_tod(session, dest, own):
        return False
    if role == "control":
        return _center_stage(session, dest, own, role)
    return own.alt_msl_ft > _arrival_altitude(dest, session) + 1000


def descent_text(session, dest: Airport, own: OwnState, role: str | None = None) -> str:
    if _center_stage(session, dest, own, role):
        session.center_descent = True
        session.assign_level(CENTER_DESCENT_FT, own.alt_msl_ft)
        return f"{session.spoken_callsign}, {phrase.descend(CENTER_DESCENT_FT, dest)}."
    alt = _arrival_altitude(dest, session)
    rwy = _arrival_runway(dest, own, session)
    q = _qnh(own, dest.country == "US")
    session.descent_given = True
    session.assign_level(alt, own.alt_msl_ft)
    bits = [f"{session.spoken_callsign}, {phrase.descend(alt, dest)}"]
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
    return f"{kind} approach {rw}" if kind else rw


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


def vectors_text(session, dest: Airport, rwy: Runway, own: OwnState) -> str:
    """'fly heading 340, vectors runway 02' toward a point 10 NM out on the extended centerline, on our side."""
    _, cross = _signed_cross(dest, rwy, own.lat, own.lon)
    side = INTERCEPT_OFFSET_NM if cross >= 0 else -INTERCEPT_OFFSET_NM
    plat, plon = _point(dest, rwy, -INTERCEPT_POINT_NM, side)
    _, hdg = _heading_words(bearing_deg(own.lat, own.lon, plat, plon), dest)
    session.vectors_given = True
    speed = _speed_text(session, own, VECTORS_KT, dest)
    return f"{session.spoken_callsign}, fly heading {hdg}, vectors {_approach(dest, rwy)}" + \
        (f", {speed}" if speed else "") + "."


VECTORS_KT = 210  # speed on vectors
INTERCEPT_KT = 180  # speed with the approach clearance
SPACING_KT = 160  # with traffic on final ahead closer than SPACING_NM
SPACING_NM = 6.0


def _speed_text(session, own: OwnState, kt: int, airport: Airport) -> str | None:
    """'reduce speed to two one zero knots' (FAA without 'knots') when the aircraft is faster than that (indicated
    airspeed, else ground speed) and hasn't been given this speed or a lower one already."""
    now = own.ias_kt if own.ias_kt is not None else own.gs_kt
    if now <= kt + 10 or (session.speed_assigned is not None and session.speed_assigned <= kt):
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


def intercept_text(session, dest: Airport, rwy: Runway, own: OwnState, traffic=()) -> str:
    """Turn onto a 30-degree intercept (or straight in if already lined up) and the approach clearance, with the
    speed: 180 kt, or 160 kt and the traffic to follow when someone is on final close ahead."""
    _, cross = _signed_cross(dest, rwy, own.lat, own.lon)
    app = _approach(dest, rwy)
    cleared = f"cleared {app}" if app.startswith(("ILS", "RNAV", "VOR", "NDB", "localizer")) else \
        f"cleared approach {app}"
    alt = phrase.level(int(session.cleared_level_ft or _arrival_altitude(dest, session)), dest)
    session.intercept_given = True
    cs = session.spoken_callsign
    ahead = _traffic_ahead(dest, rwy, own, traffic)
    speed = _speed_text(session, own, SPACING_KT if ahead else INTERCEPT_KT, dest)
    tail = (f", {speed}" if speed else "") + \
        (f", traffic to follow, {ahead[0]} on {_miles(ahead[1])} final" if ahead else "")
    if abs(cross) < 0.5 and heading_diff(own.heading_deg, rwy.heading_deg) <= 20:
        return f"{cs}, {cleared}{tail}, report established."
    true_int = (rwy.heading_deg - 30) % 360 if cross > 0 else (rwy.heading_deg + 30) % 360
    hval, hdg = _heading_words(true_int, dest)
    own_mag = magnetic(dest, own.heading_deg) % 360
    turn = "left" if ((hval - own_mag) % 360) > 180 else "right"
    return f"{cs}, turn {turn} heading {hdg}, maintain {alt} until established, {cleared}{tail}."


def _due_for_intercept(dest: Airport, rwy: Runway, own: OwnState) -> bool:
    along, cross = _signed_cross(dest, rwy, own.lat, own.lon)
    plat, plon = _point(dest, rwy, -INTERCEPT_POINT_NM, INTERCEPT_OFFSET_NM if cross >= 0 else -INTERCEPT_OFFSET_NM)
    near_point = distance_nm(own.lat, own.lon, plat, plon) <= 3.0
    lined_up = -15.0 <= along <= -3.0 and abs(cross) <= 2.0 and heading_diff(own.heading_deg, rwy.heading_deg) <= 60
    return near_point or lined_up


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
        asked = re.search(r"\b(?:flight level|level|fl) (\d{2,3})\b", re.sub(r"(?<=\d) (?=\d)", "", norm))
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
        if not session.descent_given and own.alt_msl_ft > _arrival_altitude(dest, session) + 1000:
            return descent_text(session, dest, own, facility.role)
        if _due_for_intercept(dest, rwy, own):
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
            return f"{cs}, " + (f"{wind}, " if wind else "") + \
                f"runway {phrase.runway(rwy.ident, dest.country == 'US')}, cleared to land."
    if not _arrival_radar(world, session, airport, facility):
        return None
    if session.is_first_contact(facility.role):
        return None  # let the pilot check in first
    # descent at the planned top of descent (area control: to FL100 first; the arrival radar: the final altitude)
    if descent_due(session, dest, own, facility.role):
        return descent_text(session, dest, own, facility.role)
    if rwy is None or final_distance(dest, rwy, own) is not None:
        return None
    d_dest = distance_nm(own.lat, own.lon, dest.lat, dest.lon)
    if session.descent_given and not session.intercept_given and d_dest <= VECTORS_NM + 10:
        if _due_for_intercept(dest, rwy, own):
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
