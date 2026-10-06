"""VFR flight following (FAA "flight following", ICAO "flight information service"), done by code.

    pilot (VFR, airborne, on Approach/Departure/Center): "... request flight following [to Monterey]"
    ATC:   "November one two three Alfa Bravo, NorCal Approach, squawk four five two one."
    (transponder set)  FAA "November one two three Alfa Bravo, radar contact, five miles north of San Carlos,
                       altimeter two niner niner two." / ICAO "..., identified, ..., QNH one zero one three."

The code is stable per callsign (flightplan.assign_squawk), the position is said from the nearest loaded airport.
Traffic calls then come from monitor.radar_event like for IFR flights (it works on any radar frequency).
"""

from __future__ import annotations

import re

from atc import phrase
from atc.facility import callsign_for
from atc.flightplan import assign_squawk
from atc.geo import bearing_deg, compass_point, distance_nm
from atc.models import Airport, Facility, OwnState
from atc.readback import _normalize

_REQUEST = re.compile(r"\b(?:flight following|following|vfr advisories|traffic advisories|flight information service|"
                      r"traffic information service|radar service)\b")
_POINTS = {"N": "north", "NE": "northeast", "E": "east", "SE": "southeast", "S": "south", "SW": "southwest",
           "W": "west", "NW": "northwest"}


def vfr_code(session) -> str:
    plan = session.plan
    return plan.squawk if plan is not None else assign_squawk(session.callsign + "/VFR")


def handle(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    """Reply to a VFR request for radar service, or None if this isn't one."""
    from atc.pattern import is_vfr

    if facility.role not in ("approach", "departure", "control") or own.on_ground or not is_vfr(session):
        return None
    norm = _normalize(pilot_text)
    if not _REQUEST.search(norm) or "request" not in norm:
        return None
    session.first_contact(facility.role)
    session.following = True
    session.awaiting_squawk = own.com1_mhz
    cs = session.spoken_callsign
    return f"{cs}, {callsign_for(airport, facility)}, squawk {phrase.digits(vfr_code(session))}."


def identified(session, world, own: OwnState) -> str:
    """'radar contact, five miles north of San Carlos, altimeter two niner niner two' (ICAO 'identified, ...')."""
    near = world.nearest(own) if world is not None else None
    faa = near.faa if near is not None else session.faa
    bits = [f"{session.spoken_callsign}, {'radar contact' if faa else 'identified'}"]
    if near is not None:
        from atc.airports.gen import spoken_name

        d = distance_nm(near.lat, near.lon, own.lat, own.lon)
        name = near.spoken_name or spoken_name(near.name) or near.name
        if d < 2.0:
            bits.append(f"over {name}")
        else:
            miles = round(d)
            where = _POINTS.get(compass_point(bearing_deg(near.lat, near.lon, own.lat, own.lon)), "")
            bits.append(f"{phrase.digits(str(miles))} mile{'s' if miles != 1 else ''} {where} of {name}")
    if own.qnh_hpa is not None:
        bits.append(f"altimeter {phrase.digits(f'{own.qnh_hpa * 0.02953:.2f}')}" if faa else
                    f"QNH {phrase.digits(f'{own.qnh_hpa:.0f}')}")
    return ", ".join(bits) + "."


TERMINATE_NM = 8.0
TERMINATE_AGL_FT = 3000.0


def watcher_event(session, world, own: OwnState) -> str | None:
    """Flight following ends near the field the pilot is descending into: "contact San Carlos Tower ..." when it
    has a Tower, else "radar service terminated, squawk VFR, frequency change approved" (ICAO: squawk 7000)."""
    if not session.following or session.awaiting_squawk is not None or own.on_ground:
        return None
    picked = world.pick(own)
    if picked is None or picked[1].role not in ("approach", "departure", "control"):
        return None
    near = world.nearest(own)
    if near is None or distance_nm(own.lat, own.lon, near.lat, near.lon) > TERMINATE_NM \
            or own.alt_agl_ft > TERMINATE_AGL_FT or near.icao == session.departed_from and own.alt_agl_ft > 1500:
        return None
    session.following = False
    cs = session.spoken_callsign
    from atc.flow import _contact

    twr = _contact(near, ("TWR",))
    if twr is not None:
        session.handoffs_done.add(f"{near.icao}:tower")
        return f"{cs}, {twr[1]}, good day."
    vfr = "VFR" if near.faa else phrase.digits("7000")
    return f"{cs}, radar service terminated, squawk {vfr}, frequency change approved."
