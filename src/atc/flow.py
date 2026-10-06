"""Fixed-form calls through the flight, decided and phrased in code (roadmap 9c):

- handoffs the controller starts on their own (`next_handoff`, polled by the telemetry watcher in main):
  Tower -> Departure after takeoff, Departure -> Control, -> destination Approach/Tower, Approach -> Tower,
  Tower -> Ground after landing;
- check-ins on a new position ("radar contact ..."), takeoff and landing clearances (`handle_flow`), using
  sequence.py for who may use the runway.

The LLM still answers everything else (VFR pattern work, questions, unusual requests).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from atc import phrase
from atc.clearance import _freq
from atc.facility import callsign_for, resolve_facility
from atc.geo import distance_nm
from atc.models import Airport, Facility, OwnState, Traffic
from atc.readback import _normalize
from atc.runway import magnetic, runway_in_use
from atc.sequence import on_runway, runway_status

DEP_HANDOFF_AGL_FT = 700.0  # Tower -> Departure once climbing through this
CONTROL_HANDOFF_FT = 10000.0  # Departure -> Control at this altitude or CONTROL_HANDOFF_NM out
CONTROL_HANDOFF_NM = 30.0
ARRIVAL_APP_NM = 40.0  # -> destination Approach inside this
ARRIVAL_TWR_NM = 12.0  # Approach -> Tower inside this (or -> Tower directly when there is no Approach)
REPEAT_AFTER_S = 20.0  # no frequency change by then: say it again once
GIVE_UP_AFTER_S = 60.0


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
    return fac, f"contact {callsign_for(airport, fac)} {phrase.frequency(f.mhz, airport.country == 'US')}"


def track(session, world, own: OwnState, now: float | None = None) -> None:
    """Takeoff / landing detection from on_ground changes (call every tick)."""
    now = time.monotonic() if now is None else now
    prev = session.was_on_ground
    session.was_on_ground = own.on_ground
    if prev is None or prev == own.on_ground:
        return
    near = world.nearest(own)
    close = distance_nm(own.lat, own.lon, near.lat, near.lon) <= 5.0
    if not own.on_ground and close:
        session.departed_from, session.airborne_at, session.landed = near.icao, now, False
    elif own.on_ground and close and (session.departed_from != near.icao or now - (session.airborne_at or now) > 120):
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
        return key, f"{target[1]}, good day" if fac.role != "tower" or not own.on_ground else target[1]

    # after landing: Tower -> Ground, once off the runway and slow
    if fac.role == "tower" and own.on_ground and session.landed and session.landed_at == apt.icao \
            and own.gs_kt < 40 and not any(on_runway(apt, r, own) for r in apt.runways):
        return due(f"{apt.icao}:ground", _contact(apt, ("GND", "RMP")))
    if own.on_ground:
        return None
    d_apt = distance_nm(own.lat, own.lon, apt.lat, apt.lon)
    departing = session.departed_from == apt.icao and not on_dest
    # after takeoff: Tower -> Departure
    if fac.role == "tower" and departing and own.alt_agl_ft >= DEP_HANDOFF_AGL_FT and d_apt < 15:
        return due(f"{apt.icao}:departure", _contact(apt, ("DEP", "APP", "ARR")))
    # Departure -> Control
    if fac.role in ("departure", "approach") and departing and (
            own.alt_msl_ft >= CONTROL_HANDOFF_FT or d_apt >= CONTROL_HANDOFF_NM):
        ctl = world.control()
        if ctl is not None:
            a, f = ctl
            return due(f"{apt.icao}:control",
                       (f, f"contact {callsign_for(a, f)} {phrase.frequency(f.freq.mhz, a.country == 'US')}"))
    if dest is None:
        return None
    d_dest = distance_nm(own.lat, own.lon, dest.lat, dest.lon)
    dest_app = _contact(dest, ("APP", "ARR"))
    # anyone else -> destination Approach (or Tower when it has no Approach)
    if not on_dest:
        if dest_app and d_dest <= ARRIVAL_APP_NM:
            return due(f"{dest.icao}:approach", dest_app)
        if not dest_app and d_dest <= ARRIVAL_TWR_NM * 1.5:
            return due(f"{dest.icao}:tower", _contact(dest, ("TWR",)))
        return None
    # destination Approach -> Tower
    if fac.role == "approach" and d_dest <= ARRIVAL_TWR_NM and own.alt_agl_ft <= 5000:
        return due(f"{dest.icao}:tower", _contact(dest, ("TWR",)))
    return None


def squawk_now_correct(session, own: OwnState) -> str | None:
    """After "squawk 2235" on check-in: "radar contact" as soon as the transponder shows it."""
    freq = session.awaiting_squawk
    if freq is None:
        return None
    if abs(own.com1_mhz - freq) >= 0.005:
        session.awaiting_squawk = None  # left the frequency
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
    pref = plan.dest_runway if arriving and plan else preferred
    rwy = runway_in_use(airport, own.wind_dir_deg, own.wind_kt, pref)

    # a plain check-in on a radar position (a check-in with a request or a question goes to the model)
    if facility.role in ("departure", "approach", "control") and not own.on_ground and first \
            and "request" not in norm and "?" not in pilot_text:
        session.first_contact(facility.role)
        bits = [f"{cs}, {station}, radar contact"]
        if plan and plan.is_ifr and session.clearance == "confirmed" and own.squawk != plan.squawk:
            bits = [f"{cs}, {station}, squawk {phrase.digits(plan.squawk)}"]
            session.awaiting_squawk = own.com1_mhz  # the watcher says "radar contact" once the code is set
        if arriving and facility.role == "approach" and rwy is not None:
            bits.append(f"expect runway {phrase.runway(rwy.ident, faa)}")
            q = _qnh(own, faa)
            if q:
                bits.append(q)
        elif session.departed_from == airport.icao and plan and plan.sid and facility.role in ("departure", "approach"):
            bits.append("climb via SID")
        return ", ".join(bits) + "."

    if facility.role != "tower" or rwy is None:
        return None
    st = runway_status(airport, rwy, own, traffic)
    pre = f"{cs}, {station}" if first else cs
    rw = phrase.runway(rwy.ident, faa)
    wind = phrase.wind(magnetic(airport, own.wind_dir_deg), own.wind_kt)

    if own.on_ground and "ready" in norm and ("departure" in norm or "takeoff" in norm or "take off" in norm):
        session.first_contact(facility.role)
        why = st.takeoff_blocked()
        if why:
            return f"{pre}, hold position, {why}."
        return f"{pre}, " + (f"{wind}, " if wind else "") + f"runway {rw}, cleared for takeoff."

    on_final = st.own_final_nm is not None
    if not own.on_ground and (on_final or (arriving and first)) and \
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
