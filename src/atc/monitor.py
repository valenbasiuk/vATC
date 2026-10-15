"""What a controller watches on radar and says without being asked, plus the go-around and emergency calls.

Watcher side (main._Callbacks.tick, `radar_event`):
  - level bust: past the cleared level by more than LEVEL_BUST_FT -> "check altitude, maintain flight level 200";
  - below the minimum sector altitude (terrain.py, improvement plan C4) -> "low altitude warning, check your altitude
    immediately, QNH ..., the minimum flight altitude is four thousand six hundred feet";
  - transponder 7700 -> "emergency squawk observed, say nature of emergency and intentions";
  - runway still occupied with the pilot on short final -> Tower: "go around, I say again, go around, ...".
Pilot side (`handle_pilot`, from main.handle):
  - "going around" / "missed approach" -> the missed approach instruction (C5: the published procedure when the
    runway has one, else "climb straight ahead to three thousand feet"), "contact Approach", and the arrival starts
    over; the first call on Approach then gets the first vector for another approach;
  - "mayday" / "pan pan" -> "roger mayday", runway and wind if arriving, "say intentions".
"""

from __future__ import annotations

import re

from atc import phrase, terrain
from atc.geo import distance_nm
from atc.models import Airport, Facility, OwnState, Traffic
from atc.readback import _normalize
from atc.runway import magnetic, session_runway

LEVEL_BUST_FT = 300.0
LOW_ALT_PAUSE_S = 10.0  # after a low altitude warning: the pilot climbs and reads back before the next vector
LOW_ALT_MARGIN_FT = 200.0  # this far below the minimum sector altitude: "low altitude warning"
LOW_ALT_FIELD_NM = 5.0  # (not this close to the field: that is the final approach)
GO_AROUND_NM = 1.0  # Tower orders a go-around inside this when the runway is not clear
TRAFFIC_CALL_GAP_S = 45.0  # at most one unprompted traffic call this often


def _dest(world, session) -> Airport | None:
    plan = session.plan
    return world.get(plan.destination) if plan else None


def radar_event(session, world, own: OwnState, traffic: list[Traffic], now: float) -> str | None:
    """One unprompted call that is due now, or None."""
    picked = world.pick(own)
    if picked is None or not picked[1].can_reply:
        return None
    airport, facility = picked
    cs = session.spoken_callsign
    if own.squawk == "7700" and session.emergency is None:
        session.emergency = "squawk"
        return f"{cs}, emergency squawk observed, say nature of emergency and intentions."
    if own.on_ground:
        return None
    # level bust (not once an approach is cleared: from then on the pilot descends on the procedure)
    lvl = session.cleared_level_ft
    if lvl is not None and session.level_checked != lvl and not session.intercept_given \
            and facility.role in ("departure", "approach", "control", "tower") \
            and ((session.cleared_dir == "up" and own.alt_msl_ft > lvl + LEVEL_BUST_FT)
                 or (session.cleared_dir == "down" and own.alt_msl_ft < lvl - LEVEL_BUST_FT)):
        session.level_checked = lvl
        ref = world.get(session.departed_from) if session.cleared_dir == "up" else _dest(world, session)
        return f"{cs}, check altitude, maintain {phrase.level(lvl, ref or airport)}."
    low = low_altitude(session, world, airport, facility, own)
    if low is not None:
        session.low_alt_at = now
        return low
    # traffic alert: radar calls converging traffic once per aircraft (not near the field: Tower sequences there)
    if facility.role in ("departure", "approach", "control") and own.alt_agl_ft > 1500 \
            and now - session.last_traffic_call >= TRAFFIC_CALL_GAP_S:
        from atc.traffic import ALERT_FT, ALERT_NM, describe

        for t in sorted(traffic, key=lambda t: distance_nm(own.lat, own.lon, t.lat, t.lon)):
            d = distance_nm(own.lat, own.lon, t.lat, t.lon)
            if t.on_ground or t.callsign in session.traffic_called or d > ALERT_NM \
                    or abs(t.alt_msl_ft - own.alt_msl_ft) > ALERT_FT:
                continue
            closing = session.traffic_seen.get(t.callsign)
            session.traffic_seen[t.callsign] = d
            if closing is None or d >= closing:  # first look, or not getting closer: nothing to say yet
                continue
            session.traffic_called.add(t.callsign)
            session.last_traffic_call = now
            return f"{cs}, {describe(own, t)}."
    # go-around: on short final with the runway still occupied
    dest = _dest(world, session)
    target = dest if dest is not None and airport.icao == dest.icao else airport
    if facility.role == "tower" and (session.go_around_at is None or now - session.go_around_at > 120):
        from atc.sequence import runway_status

        rwy = session_runway(target, own.wind_dir_deg, own.wind_kt, session, "arrival")
        if rwy is not None:
            st = runway_status(target, rwy, own, traffic)
            if st.own_final_nm is not None and st.own_final_nm <= GO_AROUND_NM and st.occupied_by:
                session.go_around_at = now
                _start_over(session, world)
                return f"{cs}, go around, I say again, go around, {st._what(st.occupied_by[0])} on the runway."
    return None


def low_altitude(session, world, airport: Airport, facility: Facility, own: OwnState) -> str | None:
    """An IFR flight under radar control below the minimum sector altitude where it is (not cleared for an approach,
    not climbing out of the field it left, not on the final segment): ICAO "low altitude warning, check your altitude
    immediately, QNH one zero one three, the minimum flight altitude is four thousand six hundred feet" (FAA "low
    altitude alert, ..."). Said once; again only after climbing back above it."""
    from atc.enroute import _arrival_radar

    plan = session.plan
    if plan is None or not plan.is_ifr or own.on_ground:
        return None
    if not (facility.role in ("approach", "departure", "control") or _arrival_radar(world, session, airport, facility)):
        return None
    near = world.nearest(own)
    floor = terrain.floor_at(near, own.lat, own.lon)
    if floor is None or own.alt_msl_ft >= floor:
        session.low_alt_warned = False
        return None
    climbing_out = session.departed_from == near.icao and not session.descent_given
    if own.alt_msl_ft > floor - LOW_ALT_MARGIN_FT or session.low_alt_warned or climbing_out \
            or session.intercept_given or session.visual is not None \
            or distance_nm(own.lat, own.lon, near.lat, near.lon) <= LOW_ALT_FIELD_NM:
        return None
    session.low_alt_warned = True
    cs = session.spoken_callsign
    if near.faa:
        return f"{cs}, low altitude alert, check your altitude immediately, the minimum safe altitude in your area " \
               f"is {phrase.level(terrain.round_up(floor), near)}."
    q = f", QNH {phrase.digits(f'{own.qnh_hpa:.0f}')}" if own.qnh_hpa is not None else ""
    return f"{cs}, low altitude warning, check your altitude immediately{q}, the minimum flight altitude is " \
           f"{phrase.level(terrain.round_up(floor), near)}."


def _start_over(session, world) -> None:
    """After a go-around the arrival is flown again: vectors, approach clearance, landing clearance, handoff."""
    session.landing_cleared = False
    session.intercept_given = False
    session.vectors_given = False
    session.base_given = False
    session.visual = None
    session.speed_assigned = None
    session.missed_approach = True
    dest = _dest(world, session)
    if dest is not None:
        session.handoffs_done.discard(f"{dest.icao}:tower")


_GO_AROUND = re.compile(r"\b(going around|go around|missed approach|executing missed|discontinu\w* approach)\b")
_MAYDAY = re.compile(r"\bmay ?day\b")
_PAN = re.compile(r"\bpan ?pan\b")


def handle_pilot(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str,
                 now: float) -> str | None:
    norm = _normalize(pilot_text)
    cs = session.spoken_callsign
    if _MAYDAY.search(norm) or _PAN.search(norm):
        kind = "mayday" if _MAYDAY.search(norm) else "pan pan"
        first = session.emergency != kind
        session.emergency = kind
        if not first:
            return None  # the follow-up (nature, intentions, souls on board) is free-form: the model answers
        bits = [f"{cs}, roger {kind}"]
        if not own.on_ground and facility.role in ("tower", "approach") and airport.runways:
            rwy = session_runway(airport, own.wind_dir_deg, own.wind_kt, session, "arrival")
            if rwy is not None:
                wind = phrase.wind(magnetic(airport, own.wind_dir_deg), own.wind_kt, airport.faa)
                bits.append(f"runway {phrase.runway(rwy.ident, airport.faa)} available" + (f", {wind}" if wind else ""))
        bits.append("say intentions")
        return ", ".join(bits) + "."
    plan = session.plan
    ifr = plan is not None and plan.is_ifr
    if session.missed_approach and ifr and not own.on_ground and facility.role in ("approach", "departure") \
            and "request" not in norm:
        return missed_checkin(session, world, airport, facility, own)  # back on Approach after the go-around
    if not own.on_ground and _GO_AROUND.search(norm) and facility.role in ("tower", "approach"):
        session.go_around_at = now
        _start_over(session, world)
        if facility.role == "tower" and not ifr:
            rwy = session_runway(airport, own.wind_dir_deg, own.wind_kt, session, "arrival")
            side = (rwy.pattern_direction if rwy and rwy.pattern_direction else "left")
            return f"{cs}, roger, climb to circuit altitude, report {side} downwind" + (
                f" runway {phrase.runway(rwy.ident, airport.faa)}" if rwy else "") + "."
        if facility.role == "approach":  # reported on the radar frequency itself: vectors follow from here
            session.missed_approach = False
            return f"{cs}, roger, {missed_instruction(session, world, airport, own)}, expect vectors for another approach."
        from atc.enroute import _arrival_radar
        from atc.flow import _contact

        what = missed_instruction(session, world, airport, own)
        app = None if _arrival_radar(world, session, airport, facility) else _contact(airport, ("APP", "ARR"))
        if app is not None:
            session.handoffs_done.add(f"{airport.icao}:approach_after_go_around")
            return f"{cs}, roger, {what}, {app[1]}."
        session.missed_approach = False  # Tower does the approach work itself (Rosario): it vectors from here
        return f"{cs}, roger, {what}, expect vectors for another approach."
    return None


def missed_instruction(session, world, airport: Airport, own: OwnState) -> str:
    """After a go-around: "follow the published missed approach procedure" when the runway has an instrument
    approach (and the pilot wasn't on a visual), else ICAO "climb straight ahead to three thousand feet" / FAA "fly
    runway heading, climb and maintain three thousand". The missed approach altitude (sim db, else the arrival
    altitude) is watched for a level bust."""
    from atc.enroute import approach_altitude
    from atc.navdb import approach_type, missed_altitude

    rwy = session_runway(airport, own.wind_dir_deg, own.wind_kt, session, "arrival")
    alt = missed_altitude(airport, rwy.ident) if rwy is not None else None
    published = rwy is not None and approach_type(airport, rwy.ident) is not None
    if alt is None:
        alt = approach_altitude(airport, rwy, own, session)
    session.assign_level(int(alt), own.alt_msl_ft)
    if published:
        return "follow the published missed approach procedure"
    if airport.faa:
        return f"fly runway heading, {phrase.climb(int(alt), airport)}"
    return f"climb straight ahead to {phrase.level(int(alt), airport)}"


def missed_checkin(session, world, airport: Airport, facility: Facility, own: OwnState) -> str:
    """The first call on Approach after a go-around: "roger, climb to (maintain) three thousand feet" and the first
    vector for another approach ("turn left heading three one zero, vectors ILS approach runway one three")."""
    from atc.enroute import _arrival_runway, approach_altitude, vectors_text
    from atc.facility import callsign_for

    session.missed_approach = False
    session.first_contact(facility.role)
    cs = session.spoken_callsign
    dest = _dest(world, session) or airport
    bits = [f"{cs}, {callsign_for(airport, facility)}, roger"]
    rwy = _arrival_runway(dest, own, session)
    lvl = session.cleared_level_ft or (approach_altitude(dest, rwy, own, session) if rwy else None)
    if lvl:
        session.assign_level(int(lvl), own.alt_msl_ft)
        bits.append(phrase.climb(int(lvl), dest) if own.alt_msl_ft < lvl - 300 else f"maintain {phrase.level(int(lvl), dest)}")
    if rwy is not None:
        session.descent_given = True  # the arrival goes on from the vectors
        bits.append(vectors_text(session, dest, rwy, own).split(", ", 1)[1].rstrip("."))
    return ", ".join(bits) + "."


def context_lines(session) -> list[str]:
    """Facts for the model when an emergency is on."""
    if session.emergency is None:
        return []
    what = {"mayday": "MAYDAY (distress)", "pan pan": "PAN PAN (urgency)",
            "squawk": "transponder 7700 (emergency squawk)"}[session.emergency]
    return [f"EMERGENCY declared by the pilot: {what}. Give them priority, keep instructions short, offer the nearest "
            "suitable runway, ask (once) for souls on board and fuel endurance if not given. Never refuse a request "
            "on the grounds of traffic."]
