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
from atc.runway import magnetic, runway_in_use
from atc.sequence import along_cross, final_distance, runway_status

LEAVE_ZONE_NM = 8.0
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


def handle(session, airport: Airport, facility: Facility, own: OwnState, pilot_text: str, traffic: list[Traffic],
           preferred: str | None) -> str | None:
    """Tower's reply to a VFR pattern call, or None if this isn't one."""
    if facility.role != "tower" or not is_vfr(session):
        return None
    norm = _normalize(pilot_text)
    cs = session.spoken_callsign
    faa = airport.faa
    rwy = runway_in_use(airport, own.wind_dir_deg, own.wind_kt, preferred)
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
        why = st.takeoff_blocked()
        if why:
            return f"{pre}, hold position, {why}."
        bits = [pre]
        turn = re.search(r"\b(left|right) (?:turn ?out|turn|departure)\b", norm)
        if want == "circuits" or session.circuit_intention in ("circuits", "touch and go"):
            session.in_circuit = True
            if faa:
                bits.append(f"make {_side(rwy)} closed traffic")
        elif turn:
            bits.append(f"{turn.group(1)} turn{'out' if faa else ''} approved")
        if wind:
            bits.append(wind)
        bits.append(f"runway {rw}, cleared for takeoff")
        if session.in_circuit and not faa:
            bits.append("report downwind")
        return ", ".join(bits) + "."

    # airborne
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


def _cleared(session, faa: bool) -> str:
    if session.circuit_intention == "touch and go":
        return "cleared touch and go" if not faa else "cleared for the option"
    return "cleared to land"


def _miles(nm: float) -> str:
    n = max(1, round(nm))
    return f"{phrase.digits(str(n))} mile" if n == 1 else f"{phrase.digits(str(n))} miles"


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
        rwy = runway_in_use(airport, own.wind_dir_deg, own.wind_kt)
        side = _side(rwy) if rwy else "left"
        return f"{cs}, report {side} downwind." if not airport.faa else f"{cs}, make {side} closed traffic."
    if own.on_ground and session.landing_cleared:
        session.touched_down = True
    # a VFR departure leaving the zone
    if not own.on_ground and not session.in_circuit and session.departed_from == airport.icao \
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
