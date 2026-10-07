"""Fixed-form calls through the flight, decided and phrased in code (roadmap 9c):

- handoffs the controller starts on their own (`next_handoff`, polled by the telemetry watcher in main):
  Tower -> Departure after takeoff, Departure -> Control, -> destination Approach/Tower, Approach -> Tower,
  Tower -> Ground after landing;
- check-ins on a new position ("radar contact ..."), takeoff and landing clearances (`handle_flow`), using
  sequence.py for who may use the runway.

The LLM still answers everything else (VFR pattern work, questions, unusual requests).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from atc import phrase
from atc.clearance import _freq
from atc.facility import ROLE_WORDS, callsign_for, group, resolve_facility
from atc.geo import distance_nm
from atc.models import Airport, Facility, OwnState, Traffic
from atc.pattern import is_transit
from atc.readback import _normalize
from atc.runway import magnetic, session_runway
from atc.sequence import along_cross, final_distance, on_runway, runway_status, wait_for_takeoff

DEP_HANDOFF_AGL_FT = 700.0  # Tower -> Departure once climbing through this
CONTROL_HANDOFF_FT = 10000.0  # Departure -> Control at this altitude or CONTROL_HANDOFF_NM out
CONTROL_HANDOFF_NM = 30.0
TOWER_TO_CONTROL_AGL_FT = 2000.0  # Tower with no Departure on file -> Control at this height (or 5 NM out)
ARRIVAL_APP_NM = 40.0  # -> destination Approach inside this
ARRIVAL_TWR_NM = 12.0  # Approach -> Tower inside this (or -> Tower directly when there is no Approach)
REPEAT_AFTER_S = 20.0  # no frequency change by then: say it again once
GIVE_UP_AFTER_S = 60.0
AIRBORNE_MIN_GS_KT = 30.0  # "not on ground" below this speed and height is a bad read, not a takeoff
AIRBORNE_MIN_AGL_FT = 50.0


@dataclass
class Pending:
    key: str
    text: str  # without the callsign
    from_mhz: float
    at: float
    repeated: bool = False


def _contact(airport: Airport, kinds: tuple[str, ...]) -> tuple[Facility, str] | None:
    f = _freq(airport, *kinds)
    if f is None:
        return None
    fac = resolve_facility(airport, f.mhz)
    if airport.faa and fac.role == "ground" and 121.6 <= f.mhz <= 121.975:
        return fac, f"contact ground {phrase.frequency(f.mhz, True).split(' ', 3)[-1]}"  # "contact ground point eight"
    return fac, f"contact {callsign_for(airport, fac)} {phrase.frequency(f.mhz, airport.country == 'US')}"


def track(session, world, own: OwnState, now: float | None = None) -> None:
    """Takeoff / landing detection from on_ground changes (call every tick)."""
    now = time.monotonic() if now is None else now
    if not own.on_ground and own.gs_kt < AIRBORNE_MIN_GS_KT and own.alt_agl_ft < AIRBORNE_MIN_AGL_FT:
        return  # SIM_ON_GROUND read as 0 while sitting still (sim loading, a failed read): not a takeoff
    prev = session.was_on_ground
    session.was_on_ground = own.on_ground
    if prev is None:
        if not own.on_ground:
            session.airborne_at = now  # started in the air
        return
    if prev == own.on_ground:
        return
    near = world.nearest(own)
    close = distance_nm(own.lat, own.lon, near.lat, near.lon) <= 5.0
    if not own.on_ground and close:
        session.departed_from, session.airborne_at, session.landed = near.icao, now, False
    elif not own.on_ground:
        session.airborne_at = now
    elif close and session.airborne_at is not None \
            and (session.departed_from != near.icao or now - session.airborne_at > 120):
        session.landed, session.landed_at = True, near.icao


def next_handoff(session, world, own: OwnState) -> tuple[str, str] | None:
    """(key, text without callsign) for a handoff that is due now, or None. Each key fires once per flight."""
    picked = world.pick(own)
    if picked is None or not picked[1].can_reply:
        return None
    apt, fac = picked
    done = session.handoffs_done
    plan = session.plan
    dest = world.get(plan.destination) if plan else None
    on_dest = dest is not None and apt.icao == dest.icao

    def due(key: str, target) -> tuple[str, str] | None:
        if key in done or target is None or abs(target[0].freq.mhz - own.com1_mhz) < 0.005:
            return None
        return key, target[1] if own.on_ground else f"{target[1]}, good day"

    # after landing: Tower -> Ground, once off the runway and slow
    if fac.role == "tower" and own.on_ground and session.landed and session.landed_at == apt.icao \
            and own.gs_kt < 40 and not any(on_runway(apt, r, own) for r in apt.runways):
        return due(f"{apt.icao}:ground", _contact(apt, ("GND", "RMP")))
    # before departure: Ground -> Tower once stopped next to the departure end of the runway in use
    if fac.role == "ground" and own.on_ground and not session.landed and own.gs_kt < 5:
        rwy = session_runway(apt, own.wind_dir_deg, own.wind_kt, session, "departure")
        if rwy is not None:
            along, cross = along_cross(apt, rwy, own.lat, own.lon)
            if -0.3 <= along <= 0.4 and 0.03 < cross <= 0.15:
                return due(f"{apt.icao}:tower_from_ground", _contact(apt, ("TWR",)))
    if own.on_ground:
        return None
    d_apt = distance_nm(own.lat, own.lon, apt.lat, apt.lon)
    departing = session.departed_from == apt.icao and not on_dest
    # after takeoff: Tower -> Departure (not for a VFR flight staying in the circuit or leaving on its own)
    tower_out = fac.role == "tower" and departing and not session.in_circuit and not session.vfr_departure
    dep = _contact(apt, ("DEP", "APP", "ARR"))
    if tower_out and dep is not None and own.alt_agl_ft >= DEP_HANDOFF_AGL_FT and d_apt < 15:
        return due(f"{apt.icao}:departure", dep)
    # Departure -> Control; a Tower with no Departure on file (Corrientes) hands over to Control itself
    if (fac.role in ("departure", "approach") and departing and (
            own.alt_msl_ft >= CONTROL_HANDOFF_FT or d_apt >= CONTROL_HANDOFF_NM)) or \
            (tower_out and dep is None and (own.alt_agl_ft >= TOWER_TO_CONTROL_AGL_FT or d_apt >= 5)):
        ctl = world.control(own, apt)
        if ctl is not None:
            a, f = ctl
            return due(f"{apt.icao}:control",
                       (f, f"contact {callsign_for(a, f)} {phrase.frequency(f.freq.mhz, a.country == 'US')}"))
    # Control -> the next Control en route: into another FIR, or (navdata FIRs) up into the upper sector
    if fac.role == "control" and (dest is None or distance_nm(own.lat, own.lon, dest.lat, dest.lon) > ARRIVAL_APP_NM):
        from atc.world import FIR_PREFIX

        ctl = world.control(own, apt)
        if ctl is not None and (ctl[0] is not apt or apt.icao.startswith(FIR_PREFIX)):
            a, f = ctl
            return due(f"control:{a.icao}:{f.freq.mhz:.3f}",
                       (f, f"contact {callsign_for(a, f)} {phrase.frequency(f.freq.mhz, a.country == 'US')}"))
    if dest is None:
        return None
    d_dest = distance_nm(own.lat, own.lon, dest.lat, dest.lon)
    dest_app = _contact(dest, ("APP", "ARR"))
    # anyone else -> destination Approach (or Tower when it has no Approach)
    if not on_dest:
        if dest_app and d_dest <= ARRIVAL_APP_NM:
            return due(f"{dest.icao}:approach", dest_app)
        if not dest_app and d_dest <= ARRIVAL_APP_NM:  # no Approach on file: Tower does the approach work
            return due(f"{dest.icao}:tower", _contact(dest, ("TWR",)))
        return None
    # destination Approach -> Tower, once established on final (or close in and low)
    if fac.role == "approach":
        rwy = session_runway(dest, own.wind_dir_deg, own.wind_kt, session, "arrival")
        established = rwy is not None and final_distance(dest, rwy, own) is not None
        if established or (d_dest <= ARRIVAL_TWR_NM and own.alt_agl_ft <= 5000):
            return due(f"{dest.icao}:tower", _contact(dest, ("TWR",)))
    return None


def takeoff_when_clear(session, world, own: OwnState, traffic: list[Traffic]) -> str | None:
    """The takeoff clearance Tower held back ("hold position, traffic on two mile final" / "behind the landing
    ..., line up and wait"), said as soon as the runway is free: nobody on it, nobody on short final."""
    if session.takeoff_waiting is None:
        return None
    if not own.on_ground:  # took off anyway, or the flight moved on
        session.takeoff_waiting = None
        return None
    picked = world.pick(own)
    if picked is None or picked[1].role != "tower":
        return None
    airport, _ = picked
    ident, clearance = session.takeoff_waiting
    rwy = next((r for r in airport.runways if r.ident == ident), None)
    if rwy is None:
        session.takeoff_waiting = None
        return None
    if runway_status(airport, rwy, own, traffic).takeoff_blocked():
        return None
    session.takeoff_waiting = None
    return f"{session.spoken_callsign}, {clearance}."


def squawk_now_correct(session, own: OwnState, world=None) -> str | None:
    """After "squawk 2235" on check-in: "radar contact" as soon as the transponder shows it (VFR flight following:
    "radar contact, five miles north of San Carlos, altimeter ...")."""
    freq = session.awaiting_squawk
    if freq is None:
        return None
    if abs(own.com1_mhz - freq) >= 0.005:
        session.awaiting_squawk = None  # left the frequency
        return None
    if session.following:
        from atc import following

        if own.squawk == following.vfr_code(session):
            session.awaiting_squawk = None
            return following.identified(session, world, own)
        return None
    if session.plan and own.squawk == session.plan.squawk:
        session.awaiting_squawk = None
        return f"{session.spoken_callsign}, radar contact."
    return None


def repeat_or_clear(session, own: OwnState, now: float) -> str | None:
    """A handoff the pilot hasn't acted on: say it again once, then give up."""
    p = session.pending_handoff
    if p is None:
        return None
    if abs(own.com1_mhz - p.from_mhz) >= 0.005 or now - p.at > GIVE_UP_AFTER_S:
        session.pending_handoff = None
        return None
    if not p.repeated and now - p.at >= REPEAT_AFTER_S:
        p.repeated = True
        return f"{session.spoken_callsign}, I say again, {p.text}."
    return None


_AT_HOLDING_POINT = re.compile(
    r"\b(?:on|at|reaching|approaching|arrived at|established at|holding at|now at) (?:the )?holding point\b")


def is_ready_call(norm: str, departure_runway: str) -> bool:
    """'Ready for departure', or at the departure runway's holding point ('on holding point Alfa for runway 13',
    'holding short runway 13'): a departure telling Tower or Ground it is ready. 'Holding short' of another runway
    is a crossing, not this."""
    from atc.readback import _runway

    if "ready" in norm and ("departure" in norm or "takeoff" in norm or "take off" in norm) and "taxi" not in norm:
        return True  # ("ready to taxi for departure" is a taxi request)
    if not (_AT_HOLDING_POINT.search(norm) or re.search(r"\bholding short\b", norm)):
        return False
    named = _runway(norm)
    return named is None or named.lstrip("0") == departure_runway.lower().lstrip("0")


_SIDE_WORD = r"(left|right|center|centre|l|r|c)"
_REQ_RUNWAY = re.compile(rf"\brequest(?:ing)?\b.*?\brunway (\d{{1,2}}) ?{_SIDE_WORD}?\b")
_REQ_APPROACH = re.compile(rf"\brequest(?:ing)?\b.*?\b(?:ils|rnav|gps|vor|visual|localizer|loc)\b(?: approach)?"
                           rf"(?: (?:for|to))?(?: runway)? (\d{{1,2}}) ?{_SIDE_WORD}?\b")


def runway_request(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    """'Request runway 13 for departure' / 'request ILS 13': approved when the wind allows it (up to
    runway.REQUEST_MAX_TAILWIND_KT of tailwind), then every later decision uses that runway (runway.session_runway);
    else "unable, tailwind one five knots, runway three one in use". With a taxi request in the same call the
    approval is silent: the taxi clearance itself names the runway."""
    from atc.readback import join_digits
    from atc.runway import find_runway, headwind_kt, request_ok
    from atc.taxi import is_taxi_request

    norm = join_digits(_normalize(pilot_text))
    m = _REQ_RUNWAY.search(norm) or _REQ_APPROACH.search(norm)
    if m is None:
        return None
    departing = own.on_ground and not session.landed
    if departing and facility.role not in ("clearance", "ground", "tower"):
        return None
    if not own.on_ground and facility.role not in ("approach", "departure", "control", "tower"):
        return None
    if own.on_ground and session.landed:
        return None
    use = "departure" if departing else "arrival"
    ident = m.group(1).zfill(2) + {"left": "L", "right": "R", "center": "C", "centre": "C"}.get(
        m.group(2) or "", (m.group(2) or "").upper())
    faa = airport.faa
    cs = session.spoken_callsign
    pre = f"{cs}, {callsign_for(airport, facility)}" if session.is_first_contact(facility.role) else cs

    def reply(text: str) -> str:
        session.first_contact(facility.role)
        return f"{pre}, {text}."

    combined = is_taxi_request(norm) or re.search(r"\b(push|pushback|start|clearance)\b", norm) is not None
    rwy = find_runway(airport, ident)
    if rwy is None:
        return None if combined else reply("say again the runway")
    if not request_ok(rwy, own.wind_dir_deg, own.wind_kt):
        if combined:
            return None
        tail = -headwind_kt(own.wind_dir_deg, own.wind_kt, rwy.heading_deg)
        now = session_runway(airport, own.wind_dir_deg, own.wind_kt, session, use)
        in_use = f", runway {phrase.runway(now.ident, faa)} in use" if now is not None else ""
        return reply(f"unable runway {phrase.runway(rwy.ident, faa)}, tailwind {phrase.digits(str(round(tail)))} "
                     f"knots{in_use}")
    key = f"{airport.icao}:{use}"
    changed = session.runway_requests.get(key) != rwy.ident
    session.runway_requests[key] = rwy.ident
    if combined:
        return None
    if use == "arrival" and facility.role != "tower":
        if changed and not session.landing_cleared:  # new runway: vectors and the approach clearance again
            session.vectors_given = session.intercept_given = False
        from atc.enroute import _approach

        return reply(f"expect {_approach(airport, rwy)}")
    return reply(f"runway {phrase.runway(rwy.ident, faa)} approved")


def at_holding_point(session, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    """Pilot tells Ground they are at the holding point: Ground hands them to Tower. (A readback such as
    'taxi to holding point runway 31 via Alfa' doesn't match: it has no 'on/at' before 'holding point'.)"""
    if facility.role != "ground" or not own.on_ground or session.landed:
        return None
    norm = _normalize(pilot_text)
    rwy = session_runway(airport, own.wind_dir_deg, own.wind_kt, session, "departure")
    if rwy is None or not is_ready_call(norm, rwy.ident):
        return None
    target = _contact(airport, ("TWR",))
    if target is None:
        return None
    session.handoffs_done.add(f"{airport.icao}:tower_from_ground")
    return f"{session.spoken_callsign}, {target[1]}."


# --- frequencies and who is being called ----------------------------------------------------------------

_KINDS = {"clearance": ("CLD",), "ground": ("GND", "RMP"), "tower": ("TWR",), "radar": ("APP", "ARR", "DEP")}
_NOT_ADDRESSEE = {"request", "requesting", "say", "confirm", "what", "contact", "roger", "negative", "affirm"}


def _addressee(session, pilot_text: str) -> list[str]:
    """Words before the callsign: who the pilot is calling ('rosario center martinair 4133 ...' -> rosario center)."""
    from atc.readback import _frequencies
    from atc.session import _compact_call

    if _frequencies(_normalize(pilot_text)):  # "Approach 120.6, Martinair 4133" reads back a handoff
        return []
    words = _compact_call(pilot_text).split()
    tele = (session.telephony or "").lower()
    num = "".join(c for c in session.callsign if c.isdigit())
    for i, w in enumerate(words):
        if (tele and w == tele) or (num and w == num):
            # the station comes first ("Aeroparque Ground, good afternoon, Martinair ..."); a long prefix is a
            # readback with the callsign at the end ("... ATOVO four bravo departure ..., Martinair 4133")
            return words[:i] if i <= 5 else []
    if words and words[0] in ROLE_WORDS:
        return words[:1]
    if len(words) > 1 and words[1] in ROLE_WORDS and words[0] not in _NOT_ADDRESSEE:
        return words[:2]
    return []


def _station(world, airport: Airport, grp: str, words: list[str], own: OwnState | None = None,
             ) -> tuple[Airport, Facility] | None:
    """The position of group `grp` at the airport named in `words` (else this one); area control from the world."""
    if grp == "control":
        return world.control(own, airport)
    for a in world.airports:  # a station name the pilot used: "SoCal Approach", "NorCal Departure"
        for f in a.frequencies:
            if f.spoken and f.spoken.split()[0].lower() in words:
                fac = resolve_facility(a, f.mhz)
                if fac is not None and group(fac.role) == grp:
                    return a, fac
    named = next((a for a in world.airports if (a.spoken_name or a.name.split()[0]).lower() in words), airport)
    kinds = ("DEP", "APP", "ARR") if grp == "radar" and "departure" in words else _KINDS[grp]
    f = _freq(named, *kinds)
    return (named, resolve_facility(named, f.mhz)) if f else None


def _contact_text(a: Airport, fac: Facility) -> str:
    return f"{callsign_for(a, fac)} {phrase.frequency(fac.freq.mhz, a.country == 'US')}"


def wrong_station(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    """Calling a position that isn't the one on this frequency: 'this is Aeroparque Delivery, contact ...'.
    Also an aircraft in the air calling Delivery or Ground."""
    cs, station = session.spoken_callsign, callsign_for(airport, facility)
    if not own.on_ground and facility.role in ("clearance", "ground"):
        tgt = _station(world, airport, "radar", [])
        return f"{cs}, this is {station}, " + (f"contact {_contact_text(*tgt)}." if tgt else "check frequency.")
    addr = _addressee(session, pilot_text)
    named = [ROLE_WORDS[w] for w in addr if w in ROLE_WORDS]
    here = group(facility.role)
    if not named or here is None or named[-1] == here:
        return None
    tgt = _station(world, airport, named[-1], addr, own)
    if tgt is not None and abs(tgt[1].freq.mhz - own.com1_mhz) >= 0.005:
        return f"{cs}, this is {station}, contact {_contact_text(*tgt)}."
    return f"{cs}, this is {station}, check frequency."


def frequency_request(session, world, airport: Airport, facility: Facility, own: OwnState,
                      pilot_text: str) -> str | None:
    """'Requesting Delivery frequency' / 'request frequency change (to Center)': answered from the files, never
    by the model (it invented "Buenos Aires Center 125.2" and sent an aircraft in flight to Delivery)."""
    norm = _normalize(pilot_text)
    if "frequency" not in norm or not (re.search(r"\b(request|requesting|say|what|confirm|change|give)\b", norm)
                                       or "?" in pilot_text):
        return None
    from atc.session import _compact_call

    cs = session.spoken_callsign
    addr = _addressee(session, pilot_text)
    rest = _compact_call(pilot_text).split()[len(addr):]
    asked = [ROLE_WORDS[w] for w in rest if w in ROLE_WORDS]
    if asked:
        tgt = _station(world, airport, asked[-1], rest, own)
        if tgt is None:  # e.g. Center with no area control file: nobody to send them to
            return f"{cs}, remain this frequency." if not own.on_ground else f"{cs}, unable, frequency not available."
        if "change" in norm or not own.on_ground:
            return f"{cs}, contact {_contact_text(*tgt)}."
        return f"{cs}, {_contact_text(*tgt)}."
    # "request frequency change" with no position named: the next one along the flight
    tgt = None
    if not own.on_ground:
        if facility.role == "tower":
            tgt = _station(world, airport, "radar", [])
        elif facility.role in ("departure", "approach") and session.departed_from == airport.icao:
            tgt = world.control(own, airport)
        plan = session.plan
        dest = world.get(plan.destination) if plan else None
        if tgt is None and dest is not None and dest.icao != airport.icao \
                and distance_nm(own.lat, own.lon, dest.lat, dest.lon) <= 60:
            tgt = _station(world, dest, "radar", []) or _station(world, dest, "tower", [])
    if tgt is None or abs(tgt[1].freq.mhz - own.com1_mhz) < 0.005:
        return f"{cs}, remain this frequency."
    return f"{cs}, contact {_contact_text(*tgt)}."


# --- check-ins, takeoff and landing --------------------------------------------------------------------

def _qnh(own: OwnState, faa: bool) -> str | None:
    if own.qnh_hpa is None:
        return None
    return f"altimeter {phrase.digits(f'{own.qnh_hpa * 0.02953:.2f}')}" if faa else \
        f"QNH {phrase.digits(f'{own.qnh_hpa:.0f}')}"


def handle_flow(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str,
                traffic: list[Traffic], preferred: str | None) -> str | None:
    """Code-owned reply for check-ins and takeoff/landing calls, or None to let the model answer."""
    norm = _normalize(pilot_text)
    cs = session.spoken_callsign
    plan = session.plan
    faa = airport.country == "US"
    first = session.is_first_contact(facility.role)
    station = callsign_for(airport, facility)
    arriving = plan is not None and plan.destination == airport.icao
    rwy = session_runway(airport, own.wind_dir_deg, own.wind_kt, session,
                         "departure" if own.on_ground and not session.landed else "arrival")

    # a plain check-in on a radar position (a check-in with a request or a question goes to the model)
    from atc.enroute import _arrival_radar, checkin_extra

    radar = facility.role in ("departure", "approach", "control")
    tower_as_app = facility.role == "tower" and arriving and _arrival_radar(world, session, airport, facility) \
        and not own.on_ground and own.alt_agl_ft > 2500  # Rosario TWR/APP: approach work before the final
    if (radar or tower_as_app) and not own.on_ground and first and "request" not in norm and "?" not in pilot_text:
        session.first_contact(facility.role)
        bits = [f"{cs}, {station}, radar contact" if radar else f"{cs}, {station}"]
        if plan and plan.is_ifr and session.clearance == "confirmed" and own.squawk != plan.squawk:
            bits = [f"{cs}, {station}, squawk {phrase.digits(plan.squawk)}"]
            session.awaiting_squawk = own.com1_mhz  # the watcher says "radar contact" once the code is set
        departing = session.departed_from == airport.icao and plan is not None and not arriving
        extra = checkin_extra(session, world, airport, facility, own)
        if extra:  # already past the top of descent: the descent comes with the check-in
            bits += extra
        elif (arriving or tower_as_app) and rwy is not None:
            bits.append(f"expect runway {phrase.runway(rwy.ident, faa)}")
            q = _qnh(own, faa)
            if q:
                bits.append(q)
        elif departing and facility.role in ("departure", "approach") and plan.cruise_ft:
            # ICAO 2018: "climb via SID to <level>"; no Control on file, so Departure clears the filed level
            session.assign_level(plan.cruise_ft, own.alt_msl_ft)
            bits.append(f"climb via SID to {phrase.level(plan.cruise_ft, airport)}" if plan.sid and not faa
                        else phrase.climb(plan.cruise_ft, airport))
        elif departing and plan.sid and facility.role in ("departure", "approach"):
            bits.append("climb via SID")
        return ", ".join(bits) + "."

    if facility.role != "tower" or rwy is None:
        return None
    st = runway_status(airport, rwy, own, traffic)
    pre = f"{cs}, {station}" if first else cs
    rw = phrase.runway(rwy.ident, faa)
    wind = phrase.wind(magnetic(airport, own.wind_dir_deg), own.wind_kt, faa)

    if own.on_ground and not session.landed and is_ready_call(norm, rwy.ident):
        from atc.sequence import debug_line

        print(debug_line(airport, st, traffic))
        session.first_contact(facility.role)
        clearance = (f"{wind}, " if wind else "") + f"runway {rw}, cleared for takeoff"
        wait = wait_for_takeoff(st, airport, own)
        if wait:  # the takeoff clearance follows by itself once the runway is free (takeoff_when_clear)
            session.takeoff_waiting = (rwy.ident, clearance)
            return f"{pre}, {wait}."
        return f"{pre}, {clearance}."

    on_final = st.own_final_nm is not None
    if not own.on_ground and (on_final or (arriving and first)) and not is_transit(norm) and \
            (first or "final" in norm or "landing" in norm or "land" in norm.split()):
        session.first_contact(facility.role)
        why = st.landing_blocked()
        if why:
            return f"{pre}, {why}, continue approach." if why.startswith("number") else \
                f"{pre}, continue approach, {why}."
        if on_final:
            return f"{pre}, " + (f"{wind}, " if wind else "") + f"runway {rw}, cleared to land."
        return f"{pre}, continue approach runway {rw}, report final."
    return None
