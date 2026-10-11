"""Minimum altitudes near airports (improvement plan C4): the arrival radar never descends or vectors an aircraft
below the minimum sector altitude (MSA), and a pilot who goes below it gets a low altitude warning (monitor.py).

The numbers: the airport's `msa` (airport YAML hand field, else the Navigraph database that ships with Little Navmap,
navdb.msa_records). Each MSA is a centre (the airport or one of its navaids), a radius (25 NM) and sectors given by
the magnetic bearing TO the centre where each one starts, clockwise to the next: SAMR's SRA VOR has 4600 ft to the
north-east (the plains), 6400 south-east, 7600 south-west and 8200 north-west (the Andes). Where several circles
cover a point the highest altitude counts. Outside every circle there is no floor here (area control's descent is
kept off high ground with the grid MORA instead, enroute.py).
"""

from __future__ import annotations

import math

from atc.geo import bearing_deg, distance_nm
from atc.models import Airport


def _sector_alt(msa: dict, lat: float, lon: float) -> int | None:
    """The altitude of the sector of one MSA circle the point is in, None outside the circle."""
    clat, clon = float(msa["lat"]), float(msa["lon"])
    if distance_nm(clat, clon, lat, lon) > float(msa.get("radius_nm") or 25.0):
        return None
    sectors = sorted(((float(b) % 360.0, int(a)) for b, a in msa.get("sectors", [])), key=lambda s: s[0])
    if not sectors:
        return None
    if len(sectors) == 1:
        return sectors[0][1]
    inbound = (bearing_deg(lat, lon, clat, clon) - float(msa.get("mag_var") or 0.0)) % 360.0  # magnetic, TO the centre
    alt = sectors[-1][1]  # before the first start: still in the last sector (it wraps through north)
    for start, a in sectors:
        if inbound >= start:
            alt = a
    return alt


def floor_at(airport: Airport | None, lat: float, lon: float) -> int | None:
    """Minimum altitude (ft) at a point near `airport`: the highest MSA sector over it, None if no MSA covers it."""
    if airport is None or not airport.msa:
        return None
    alts = [a for m in airport.msa if (a := _sector_alt(m, lat, lon)) is not None]
    return max(alts) if alts else None


def floor_along(airport: Airport | None, a: tuple[float, float], b: tuple[float, float],
                step_nm: float = 2.0) -> int | None:
    """The highest floor on the straight line from a to b (both ends included), None if no MSA covers any of it."""
    if airport is None or not airport.msa:
        return None
    n = max(1, int(math.ceil(distance_nm(*a, *b) / step_nm)))
    found = None
    for i in range(n + 1):
        t = i / n
        f = floor_at(airport, a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        if f is not None and (found is None or f > found):
            found = f
    return found


def round_up(ft: float, step: int = 100) -> int:
    return int(math.ceil(ft / step) * step)
