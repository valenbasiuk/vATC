"""Traffic information said by code, not the model, so it can't invent types, directions or levels.

    "traffic, two o'clock, three miles, opposite direction, Airbus three twenty, one thousand feet above"

Only relevant traffic is called: within RELEVANT_NM and RELEVANT_FT of the pilot (both airborne).
"""

from __future__ import annotations

import re

from atc import phrase
from atc.geo import bearing_deg, distance_nm, heading_diff
from atc.models import OwnState, Traffic

RELEVANT_NM = 8.0
RELEVANT_FT = 3000.0

# ICAO type designator -> how a controller says it. Unknown types are left out (never guessed).
_TYPES = {
    "A318": "Airbus three eighteen", "A319": "Airbus three nineteen", "A320": "Airbus three twenty",
    "A20N": "Airbus three twenty", "A321": "Airbus three twenty-one", "A21N": "Airbus three twenty-one",
    "A332": "Airbus three thirty", "A333": "Airbus three thirty", "A339": "Airbus three thirty", "A359": "Airbus three fifty",
    "B737": "Boeing seven thirty-seven", "B738": "Boeing seven thirty-seven", "B739": "Boeing seven thirty-seven",
    "B38M": "Boeing seven thirty-seven", "B39M": "Boeing seven thirty-seven", "B744": "Boeing seven forty-seven",
    "B748": "Boeing seven forty-seven", "B763": "Boeing seven sixty-seven", "B772": "Boeing seven seventy-seven",
    "B77W": "Boeing seven seventy-seven", "B788": "Boeing seven eighty-seven", "B789": "Boeing seven eighty-seven",
    "E190": "Embraer one ninety", "E195": "Embraer one ninety-five", "E170": "Embraer one seventy",
    "E175": "Embraer one seventy-five", "CRJ2": "Canadair regional jet", "CRJ7": "Canadair regional jet",
    "AT72": "ATR seventy-two", "AT76": "ATR seventy-two", "AT45": "ATR forty-two", "F100": "Fokker one hundred",
    "C172": "Cessna one seventy-two", "C152": "Cessna one fifty-two", "C182": "Cessna one eighty-two",
    "C208": "Cessna Caravan", "PA28": "Piper Cherokee", "SR22": "Cirrus", "BE58": "Baron", "DA40": "Diamond",
}


def type_designator(raw: str | None) -> str | None:
    """'TT:ATCCOM.AC_MODEL_A320.0.text' or 'A320' -> 'A320'."""
    if not raw:
        return None
    m = re.search(r"AC_MODEL[_ ]([A-Z0-9]{2,4})", raw.upper()) or re.fullmatch(r"\s*([A-Z0-9]{3,4})\s*", raw.upper())
    return m.group(1) if m else None


def spoken_type(raw: str | None) -> str | None:
    t = type_designator(raw)
    return _TYPES.get(t) if t else None


def _direction(own: OwnState, t: Traffic) -> str:
    d = heading_diff(own.heading_deg, t.heading_deg)
    if d >= 150:
        return "opposite direction"
    if d <= 30:
        return "same direction"
    left_to_right = ((t.heading_deg - own.heading_deg) % 360) < 180
    return "crossing left to right" if left_to_right else "crossing right to left"


def _level(own: OwnState, t: Traffic) -> str:
    diff = round((t.alt_msl_ft - own.alt_msl_ft) / 100) * 100
    if abs(diff) < 300:
        return "same level"
    th, hu = divmod(abs(int(diff)), 1000)
    words = " ".join(w for w in ((f"{phrase.digits(str(th))} thousand" if th else ""),
                                  (f"{phrase.digits(str(hu // 100))} hundred" if hu else "")) if w)
    return f"{words} feet {'above' if diff > 0 else 'below'}"


def relevant(own: OwnState, traffic: list[Traffic]) -> list[Traffic]:
    if own.on_ground:
        return []
    out = [t for t in traffic if not t.on_ground
           and distance_nm(own.lat, own.lon, t.lat, t.lon) <= RELEVANT_NM
           and abs(t.alt_msl_ft - own.alt_msl_ft) <= RELEVANT_FT]
    return sorted(out, key=lambda t: distance_nm(own.lat, own.lon, t.lat, t.lon))


def describe(own: OwnState, t: Traffic) -> str:
    d = distance_nm(own.lat, own.lon, t.lat, t.lon)
    clock = round(((bearing_deg(own.lat, own.lon, t.lat, t.lon) - own.heading_deg) % 360) / 30) % 12 or 12
    miles = max(1, round(d))
    parts = [f"traffic, {phrase.digits(str(clock)) if clock < 10 else {10: 'ten', 11: 'eleven', 12: 'twelve'}[clock]} "
             f"o'clock, {phrase.digits(str(miles))} {'mile' if miles == 1 else 'miles'}", _direction(own, t)]
    typ = spoken_type(getattr(t, "type", None))
    if typ:
        parts.append(typ)
    parts.append(_level(own, t))
    return ", ".join(parts)


def is_traffic_question(norm: str, raw: str) -> bool:
    return "traffic" in norm and ("?" in raw or any(w in norm for w in ("any", "request", "say", "information", "advise")))


def traffic_reply(spoken_callsign: str, own: OwnState, traffic: list[Traffic]) -> str:
    rel = relevant(own, traffic)[:2]
    if not rel:
        return f"{spoken_callsign}, no reported traffic."
    return f"{spoken_callsign}, " + ". ".join(describe(own, t) for t in rel) + "."
