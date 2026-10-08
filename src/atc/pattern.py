"""VFR traffic pattern at a towered airport, done by code (roadmap item 13): what Tower says to a VFR pilot.

  inbound call          -> ICAO "join left downwind runway 31, wind ..., QNH 1015, report downwind"
                           FAA  "enter left downwind runway 28L, altimeter 2992, report midfield downwind"
                           (lined up with the final: "make straight-in approach runway 31, report final")
  "downwind"            -> number in sequence: "number two, follow the Cessna on final, report final"
                           number one: ICAO "report final"; FAA "runway 28L, cleared to land" (FAA clears early)
  "base" / "final"      -> the landing clearance, or "cleared touch and go" when that is the intention
  touch and go          -> back into the circuit: "report downwind"
  ready, request circuits / closed traffic / left turnout -> takeoff clearance with the turn or "report downwind"
  watcher: a VFR departure 8 NM from the field          -> "frequency change approved, good day"

The runway, the pattern side (airport YAML `pattern_direction`, left if unknown, as ICAO's default), the wind, the
QNH and the sequence all come from code; nothing is left to the model.
"""

from __future__ import annotations

import re

from atc import phrase
from atc.facility import callsign_for
from atc.geo import distance_nm, heading_diff
from atc.models import Airport, Facility, OwnState, Runway, Traffic
from atc.readback import _normalize
from atc.runway import magnetic, session_runway
from atc.sequence import _miles, along_cross, final_distance, runway_status, wait_for_takeoff

LEAVE_ZONE_NM = 8.0
AUTO_CLEAR_NM = 2.5  # in the circuit, on final this close and not cleared yet: Tower clears without a call
STRAIGHT_IN_NM = 1.5  # this close to the extended centerline (and pointing along it): straight-in


def is_vfr(session) -> bool:
    return session.plan is None or not session.plan.is_ifr


def _side(rwy: Runway) -> str:
    return rwy.pattern_direction if rwy.pattern_direction in ("left", "right") else "left"


def _qnh(own: OwnState, faa: bool) -> str | None:
    if own.qnh_hpa is None:
        return None
    return f"altimeter {phrase.digits(f'{own.qnh_hpa * 0.02953:.2f}')}" if faa else \
        f"QNH {phrase.digits(f'{own.qnh_hpa:.0f}')}"


def _intention(norm: str) -> str | None:
    if re.search(r"\btouch (?:and|n) go\b|\btouch and goes\b", norm):
        return "touch and go"
    if re.search(r"\b(?:full stop|to land|landing)\b", norm):
        return "full stop"
    if re.search(r"\b(?:circuits?|closed traffic|pattern work|patterns|the pattern)\b", norm):
        return "circuits"
    return None


_INBOUND = re.compile(r"\b(?:inbound|for landing|to land|landing|joining|request(?:ing)? (?:to )?join|"
                      r"(?:miles|nm|mile) (?:north|south|east|west|northeast|northwest|southeast|southwest)|"
                      r"(?:north|south|east|west) of the field)\b")
_POSITION = re.compile(r"\b(?:(left|right) )?(downwind|base|final)\b")
_COMPASS_DEG = {"north": 0, "northeast": 45, "east": 90, "southeast": 135, "south": 180, "southwest": 225,
                "west": 270, "northwest": 315}
_DIRS = "north|northeast|east|southeast|south|southwest|west|northwest"
# "departure to the north" / "northbound departure" / "request north departure"
_COMPASS_DEP = re.compile(rf"\b(?:departure|depart|departing|leave the zone|leaving the zone) (?:to|towards) the "
                          rf"({_DIRS})\b|\b({_DIRS})(?:bound)? (?:departure|departing)\b")
# passing through the zone, not landing: "10 miles south, request to transit the zone northbound" (the model's turn)
_TRANSIT = re.compile(r"\b(?:transit(?:ing)?|cross(?:ing)? (?:the )?(?:field|zone|airport|control zone|ctr)|"
                      r"overfl(?:y|ying|ight)|over ?fly|through (?:the|your) (?:zone|ctr|airspace|class))\b")


def handle(session, airport: Airport, facility: Facility, own: OwnState, pilot_text: str, traffic: list[Traffic],
           preferred: str | None) -> str | None:
    """Tower's reply to a VFR pattern call, or None if this isn't one."""
    if facility.role != "tower" or not is_vfr(session):
        return None
    norm = _normalize(pilot_text)
    cs = session.spoken_callsign
    faa = airport.faa
    rwy = session_runway(airport, own.wind_dir_deg, own.wind_kt, session, "departure" if own.on_ground else "arrival")
    if rwy is None:
        return None
    rw = phrase.runway(rwy.ident, faa)
    wind = phrase.wind(magnetic(airport, own.wind_dir_deg), own.wind_kt, faa)
    first = session.is_first_contact(facility.role)
    pre = f"{cs}, {callsign_for(airport, facility)}" if first else cs
    want = _intention(norm)
    if want:
        session.circuit_intention = want

    if own.on_ground:  # departure: a turn-out, or staying in the circuit
        if "ready" not in norm or not ("departure" in norm or "takeoff" in norm or "take off" in norm):
            return None
        st = runway_status(airport, rwy, own, traffic)
        session.first_contact(facility.role)
        bits = []
        turn = re.search(r"\b(left|right) (?:turn ?out|turn|departure)\b", norm)
        heading = _COMPASS_DEP.search(norm)
        if want == "circuits" or session.circuit_intention in ("circuits", "touch and go"):
            session.in_circuit = True
            if faa:
                bits.append(f"make {_side(rwy)} closed traffic")
        elif turn:
            bits.append(f"{turn.group(1)} turn{'out' if faa else ''} approved")
        elif heading:  # "departure to the north": the turn that gets there from this runway
            word = heading.group(1) or heading.group(2)
            d = (_COMPASS_DEG[word] - magnetic(airport, rwy.heading_deg)) % 360 if airport.mag_var_deg is not None \
                else (_COMPASS_DEG[word] - rwy.heading_deg) % 360
            side = "" if d <= 30 or d >= 330 else "right turn " if d < 180 else "left turn "
            bits.append(f"after departure, {side}{word}bound approved" if side else f"{word}bound departure approved")
        if turn or heading or re.search(r"\bvfr\b", norm):
            session.vfr_departure = True  # leaves the zone on its own: no handoff to Departure
        if wind:
            bits.append(wind)
        from atc.flow import takeoff_words

        bits.append(takeoff_words(session, airport, rwy, own, runway_status(airport, rwy, own, traffic)))
        if session.in_circuit and not faa:
            bits.append("report downwind")
        from atc.flow import _backtrack_blocked, needs_backtrack

        if needs_backtrack(airport, rwy, own, norm):  # SARC: backtrack to the end, the takeoff once lined up
            session.takeoff_waiting = (rwy.ident, ", ".join(bits))
            why = _backtrack_blocked(st, airport)
            session.backtrack = "pending" if why else "lining"
            return f"{pre}, hold position, {why}." if why else f"{pre}, backtrack runway {rw}, line up and wait."
        from atc import departures
        from atc.flow import departure_hold

        departures.ready(airport.icao, departures.USER, rwy.ident)
        wait = wait_for_takeoff(st, airport, own) or departure_hold(session, airport, rwy, own, st)
        if wait:  # the takeoff clearance (with the turn-out / circuit) follows once the runway is free
            session.takeoff_waiting = (rwy.ident, ", ".join(bits))
            return f"{pre}, {wait}."
        return ", ".join([pre] + bits) + "."

    # airborne
    if is_transit(norm):
        return None  # a zone transit: the model answers it (with the facts in CONTEXT)
    if first and (_INBOUND.search(norm) or want in ("full stop", "touch and go")):
        session.first_contact(facility.role)
        session.in_circuit = True
        q = _qnh(own, faa)
        along, cross = along_cross(airport, rwy, own.lat, own.lon)
        lined_up = along < -2.0 and cross <= STRAIGHT_IN_NM and heading_diff(own.heading_deg, rwy.heading_deg) <= 30
        if lined_up:
            bits = [pre, f"make straight-in approach runway {rw}" if not faa else f"make straight-in runway {rw}"]
            tail = "report final" if not faa else f"report {phrase.digits('3')} mile final"
        else:
            side = _side(rwy)
            bits = [pre, f"{'enter' if faa else 'join'} {side} downwind runway {rw}"]
            tail = "report midfield downwind" if faa else "report downwind"
        if wind and not faa:
            bits.append(wind)
        if q:
            bits.append(q)
        bits.append(tail)
        return ", ".join(bits) + "."
    pos = _POSITION.search(norm)
    st = runway_status(airport, rwy, own, traffic)
    on_final = st.own_final_nm is not None
    if pos and pos.group(2) in ("downwind", "base") and ("report" not in norm or "reporting" in norm):
        session.first_contact(facility.role)
        session.in_circuit = True
        ahead = st.finals if not on_final else st.ahead_on_final()
        if pos.group(2) == "downwind":
            session.landing_cleared = session.touched_down = False  # a new lap: the last clearance is used up
            if ahead:
                cs_t, nm = ahead[-1]
                typ = st.types.get(cs_t)
                follow = f"follow the {typ} on {_miles(nm)} final" if typ else f"traffic to follow on {_miles(nm)} final"
                return f"{pre}, number {phrase.digits(str(len(ahead) + 1))}, {follow}, report final."
            if st.occupied_by:
                return f"{pre}, number one, report final."
            if faa:
                session.landing_cleared = True
                return f"{pre}, number one, runway {rw}, {_cleared(session, faa)}."
            return f"{pre}, number one, report final."
        # base
        why = st.landing_blocked()
        if why:
            return f"{pre}, {why}, continue approach." if why.startswith("number") else f"{pre}, continue approach, {why}."
        session.landing_cleared = True
        return f"{pre}, " + (f"{wind}, " if wind else "") + f"runway {rw}, {_cleared(session, faa)}."
    if on_final and (pos is not None or "final" in norm):
        session.first_contact(facility.role)
        why = st.landing_blocked()
        if why:
            return f"{pre}, {why}, continue approach." if why.startswith("number") else f"{pre}, continue approach, {why}."
        session.landing_cleared = True
        return f"{pre}, " + (f"{wind}, " if wind else "") + f"runway {rw}, {_cleared(session, faa)}."
    return None


def is_transit(norm: str) -> bool:
    """A call asking to pass through the zone (normalized text), not to land or join the circuit."""
    return _TRANSIT.search(norm) is not None and _intention(norm) not in ("full stop", "touch and go") \
        and "join" not in norm


def transit_lines(pilot_text: str) -> list[str]:
    """CONTEXT for the model when the pilot asks to transit the zone (even lined up with a runway)."""
    if not is_transit(_normalize(pilot_text)):
        return []
    return ["ZONE TRANSIT: the pilot asks to pass through the control zone, NOT to land. Never clear them to land or "
            "into the circuit. Approve the transit (or give a holding instruction), give the QNH, and ask them to "
            "report a point (e.g. overhead the field or leaving the zone)."]


def _cleared(session, faa: bool) -> str:
    if session.circuit_intention in ("touch and go", "circuits"):
        return "cleared touch and go" if not faa else "cleared for the option"
    return "cleared to land"


def watcher_event(session, world, own: OwnState, traffic: list[Traffic]) -> str | None:
    """Unprompted Tower calls for VFR traffic: after a touch and go, back into the circuit; leaving the zone."""
    if not is_vfr(session):
        return None
    picked = world.pick(own)
    if picked is None or picked[1].role != "tower":
        return None
    airport, facility = picked
    cs = session.spoken_callsign
    # touch and go done (was on the ground, airborne again, still in the circuit): report downwind again
    if session.in_circuit and session.landing_cleared and not own.on_ground and own.alt_agl_ft > 300 \
            and session.circuit_intention in ("touch and go", "circuits") and session.touched_down:
        session.landing_cleared = False
        session.touched_down = False
        rwy = session_runway(airport, own.wind_dir_deg, own.wind_kt, session, "arrival")
        side = _side(rwy) if rwy else "left"
        return f"{cs}, report {side} downwind." if not airport.faa else f"{cs}, make {side} closed traffic."
    if own.on_ground and session.landing_cleared:
        session.touched_down = True
    # in the circuit, on short final, the pilot didn't report final: Tower clears them once the runway is free
    if session.in_circuit and not own.on_ground and not session.landing_cleared:
        wind_dir, wind_kt = session.surface_wind.get(airport.icao) or (own.wind_dir_deg, own.wind_kt)
        rwy = session_runway(airport, wind_dir, wind_kt, session, "arrival")
        if rwy is not None:
            st = runway_status(airport, rwy, own, traffic)
            if st.own_final_nm is not None and st.own_final_nm <= AUTO_CLEAR_NM and st.landing_blocked() is None:
                session.landing_cleared = True
                wind = phrase.wind(magnetic(airport, wind_dir), wind_kt, airport.faa)
                return f"{cs}, " + (f"{wind}, " if wind else "") + \
                    f"runway {phrase.runway(rwy.ident, airport.faa)}, {_cleared(session, airport.faa)}."
    # a VFR departure leaving the zone (unless Tower already handed it to Departure)
    if not own.on_ground and not session.in_circuit and session.departed_from == airport.icao \
            and f"{airport.icao}:departure" not in session.handoffs_done \
            and not session.zone_left and distance_nm(own.lat, own.lon, airport.lat, airport.lon) >= LEAVE_ZONE_NM:
        session.zone_left = True
        return f"{cs}, frequency change approved, good day."
    return None


def context_lines(session) -> list[str]:
    if not is_vfr(session) or not session.in_circuit:
        return []
    return [f"VFR CIRCUIT: the pilot is in the traffic pattern (intention: {session.circuit_intention or 'unknown'}). "
            "Sequencing and clearances are done by software."]


def on_final_for(airport: Airport, rwy: Runway, own: OwnState) -> float | None:
    return final_distance(airport, rwy, own)
