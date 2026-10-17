"""Ride reports by code (improvement plan D2): "any ride reports at flight level three five zero?"

There is no live PIREP feed for Argentina, so the radar answers from what is known: the wind the user's own aircraft
met on the way up or down (vertical wind shear, the usual source of clear-air chop) and thunderstorms in the METARs
of the airports in play.

    "light chop reported between flight level two two zero and two six zero"
    "no turbulence reported at flight level three five zero"
    "no ride reports at flight level three niner zero"  (no data at that level)
    "..., moderate turbulence reported in the vicinity of thunderstorms near Rosario"
"""

from __future__ import annotations

import math
import re

from atc import phrase
from atc.models import Airport, Facility, OwnState
from atc.readback import _normalize, join_digits

SAMPLE_FT = 500.0  # a wind sample every this much of climb or descent
LIGHT_KT = 4.0  # wind change per 1000 ft for light chop...
MODERATE_KT = 8.0  # ... and for moderate chop
BAND_FT = 4000  # without a level asked: the band around the aircraft
_ASK = re.compile(r"\b(?:ride reports?|pireps?|reports? of (?:turbulence|chop)|turbulence reports?|"
                  r"how(?: s| is)? the ride|any (?:reported )?(?:chop|turbulence))\b")
_LEVEL = re.compile(r"\b(?:flight level|level|fl) (\d{2,3})\b")


def observe(session, own: OwnState) -> None:
    """Remember the wind at the aircraft every SAMPLE_FT of climb or descent (main's watcher, every tick)."""
    if own.on_ground or own.wind_dir_deg is None or own.wind_kt is None:
        return
    samples = session.ride_samples
    if samples and abs(own.alt_msl_ft - samples[-1][0]) < SAMPLE_FT:
        return
    h = math.radians(own.wind_dir_deg)
    samples.append((own.alt_msl_ft, own.wind_kt * math.sin(h), own.wind_kt * math.cos(h)))
    del samples[:-200]


def bands(session) -> list[tuple[int, int, str]]:
    """(from ft, to ft, 'light chop' / 'moderate chop') where the measured wind changed fast with height."""
    pts = sorted(session.ride_samples)
    out: list[list] = []
    for (a1, e1, n1), (a2, e2, n2) in zip(pts, pts[1:]):
        dz = a2 - a1
        if dz <= 0 or dz > 3000:
            continue
        shear = math.hypot(e2 - e1, n2 - n1) / (dz / 1000.0)
        kind = "moderate chop" if shear >= MODERATE_KT else "light chop" if shear >= LIGHT_KT else None
        if kind is None:
            continue
        lo, hi = int(a1 // 1000 * 1000), int(-(-a2 // 1000) * 1000)
        if out and out[-1][2] == kind and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi, kind])
    return [(lo, hi, k) for lo, hi, k in out]


def _covered(session, lo: float, hi: float) -> bool:
    return any(lo <= a <= hi for a, _, _ in session.ride_samples)


def _storms(world, own: OwnState) -> str | None:
    """'in the vicinity of thunderstorms near Rosario' when an airport in play reports TS or CB."""
    from atc import weather
    from atc.geo import distance_nm

    for a in sorted(world.airports, key=lambda a: distance_nm(own.lat, own.lon, a.lat, a.lon)):
        if distance_nm(own.lat, own.lon, a.lat, a.lon) > 120:
            break
        m = weather.get(a.icao)
        if m is not None and (any("TS" in w for w in m.weather) or any(c.kind == "CB" for c in m.clouds)):
            from atc.airports.gen import spoken_name

            return f"in the vicinity of thunderstorms near {a.spoken_name or spoken_name(a.name) or a.name}"
    return None


def handle(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    """The answer to a ride report question on a radar position, None if it isn't one."""
    if own.on_ground or facility.role not in ("approach", "departure", "control"):
        return None
    norm = join_digits(re.sub(r"(?<=[a-z])(?=\d)", " ", _normalize(pilot_text)))  # "fl240" -> "fl 240"
    if not _ASK.search(norm):
        return None
    cs = session.spoken_callsign
    asked = _LEVEL.search(norm)
    if asked:
        level = int(asked.group(1)) * 100
        lo, hi = level - 1500, level + 1500
        where = phrase.level(level, airport)
    else:
        lo, hi = own.alt_msl_ft - BAND_FT, own.alt_msl_ft + BAND_FT
        where = None
    found = [(a, b, k) for a, b, k in bands(session) if a <= hi and b >= lo]
    bits = []
    for a, b, k in found[:2]:
        bits.append(f"{k} reported between {phrase.level(a, airport)} and {phrase.level(b, airport)}")
    storms = _storms(world, own)
    if storms:
        bits.append(f"moderate turbulence reported {storms}")
    if bits:
        return f"{cs}, " + ", ".join(bits) + "."
    if where is None:
        where = phrase.level(int(round(own.alt_msl_ft / 1000.0) * 1000), airport)
    if _covered(session, lo, hi):
        return f"{cs}, no turbulence reported at {where}."
    return f"{cs}, no ride reports at {where}."
