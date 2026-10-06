"""What a controller watches on radar and says without being asked, plus the go-around and emergency calls.

Watcher side (main._Callbacks.tick, `radar_event`):
  - level bust: past the cleared level by more than LEVEL_BUST_FT -> "check altitude, maintain flight level 200";
  - transponder 7700 -> "emergency squawk observed, say nature of emergency and intentions";
  - runway still occupied with the pilot on short final -> Tower: "go around, I say again, go around, ...".
Pilot side (`handle_pilot`, from main.handle):
  - "going around" / "missed approach" -> the missed approach instruction, and the arrival starts over;
  - "mayday" / "pan pan" -> "roger mayday", runway and wind if arriving, "say intentions".
"""

from __future__ import annotations

import re

from atc import phrase
from atc.geo import distance_nm
from atc.models import Airport, Facility, OwnState, Traffic
from atc.readback import _normalize
from atc.runway import magnetic, session_runway

LEVEL_BUST_FT = 300.0
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


def _start_over(session, world) -> None:
    """After a go-around the arrival is flown again: vectors, approach clearance, landing clearance, handoff."""
    session.landing_cleared = False
    session.intercept_given = False
    session.vectors_given = False
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
    if not own.on_ground and _GO_AROUND.search(norm) and facility.role in ("tower", "approach"):
        session.go_around_at = now
        _start_over(session, world)
        plan = session.plan
        ifr = plan is not None and plan.is_ifr
        if facility.role == "tower":
            from atc.enroute import _arrival_radar
            from atc.flow import _contact

            if ifr:
                app = None if _arrival_radar(world, session, airport, facility) else _contact(airport, ("APP", "ARR"))
                if app is not None:
                    session.handoffs_done.add(f"{airport.icao}:approach_after_go_around")
                    return f"{cs}, roger, follow the published missed approach procedure, {app[1]}."
                return f"{cs}, roger, follow the published missed approach procedure, expect vectors for another approach."
            rwy = session_runway(airport, own.wind_dir_deg, own.wind_kt, session, "arrival")
            side = (rwy.pattern_direction if rwy and rwy.pattern_direction else "left")
            return f"{cs}, roger, climb to circuit altitude, report {side} downwind" + (
                f" runway {phrase.runway(rwy.ident, airport.faa)}" if rwy else "") + "."
        return f"{cs}, roger, follow the published missed approach procedure, expect vectors for another approach."
    return None


def context_lines(session) -> list[str]:
    """Facts for the model when an emergency is on."""
    if session.emergency is None:
        return []
    what = {"mayday": "MAYDAY (distress)", "pan pan": "PAN PAN (urgency)",
            "squawk": "transponder 7700 (emergency squawk)"}[session.emergency]
    return [f"EMERGENCY declared by the pilot: {what}. Give them priority, keep instructions short, offer the nearest "
            "suitable runway, ask (once) for souls on board and fuel endurance if not given. Never refuse a request "
            "on the grounds of traffic."]
