"""Short fixed-form answers any position gives, done by code (no model latency, nothing invented):

    "radio check"                    -> ICAO "read you five" / FAA "loud and clear"
    "time check"                     -> "time one four three five" (the sim's zulu clock, else this PC's UTC)
    "say QNH" / "request altimeter" / "say the wind" -> the values from the sim (only those asked for)

A question about anything else (or with other requests in it) is left to the model.
"""

from __future__ import annotations

import re
import time

from atc import phrase
from atc.facility import callsign_for
from atc.models import Airport, Facility, OwnState
from atc.readback import _normalize
from atc.runway import magnetic

_RADIO_CHECK = re.compile(r"\bradio check\b|\bhow do you read\b")
_TIME_CHECK = re.compile(r"\btime check\b|\bsay (?:the )?time\b")
_ASK = re.compile(r"\b(?:say|request|requesting|confirm|what is|what s|whats|check|current)\b")
_WX = re.compile(r"\b(qnh|altimeter|wind|winds)\b")
_OTHER = re.compile(r"\b(?:taxi|push|start|clearance|takeoff|departure|landing|runway|approach|direct|climb|descend|"
                    r"descent|higher|lower|vectors|traffic|ready|inbound|temperature|visibility|weather|ils)\b")


def handle(session, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    norm = _normalize(pilot_text)
    cs = session.spoken_callsign
    pre = f"{cs}, {callsign_for(airport, facility)}" if session.is_first_contact(facility.role) else cs
    faa = airport.faa
    if _RADIO_CHECK.search(norm):
        session.first_contact(facility.role)
        return f"{pre}, {'loud and clear' if faa else 'read you five'}."
    if _TIME_CHECK.search(norm):
        z = own.zulu_s if own.zulu_s is not None else time.time() % 86400
        h, m = divmod(int(z) // 60 % 1440, 60)
        session.first_contact(facility.role)
        return f"{pre}, time {phrase.digits(f'{h:02d}{m:02d}')}."
    asked = set(_WX.findall(norm))
    if not asked or not _ASK.search(norm) or _OTHER.search(norm):
        return None
    bits = []
    if asked & {"wind", "winds"}:
        if own.wind_kt is None:
            return None  # unknown: the model says "wind not available" from CONTEXT
        bits.append(phrase.wind(magnetic(airport, own.wind_dir_deg), own.wind_kt, faa) or "wind calm")
    if asked & {"qnh", "altimeter"}:
        if own.qnh_hpa is None:
            return None
        bits.append(f"altimeter {phrase.digits(f'{own.qnh_hpa * 0.02953:.2f}')}" if faa else
                    f"QNH {phrase.digits(f'{own.qnh_hpa:.0f}')}")
    session.first_contact(facility.role)
    return f"{pre}, " + ", ".join(bits) + "."
