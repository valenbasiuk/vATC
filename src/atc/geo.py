"""Small geometry helpers (great circle, no external deps)."""

from __future__ import annotations

import math

EARTH_RADIUS_NM = 3440.065


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing from point 1 to point 2, 0-360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def offset_nm(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """(east, north) of a point from an origin, in NM. Flat-earth: fine within ~20 NM of the airport."""
    return (lon - lon0) * 60.0 * math.cos(math.radians(lat0)), (lat - lat0) * 60.0


def heading_diff(a: float, b: float) -> float:
    """Smallest angle between two headings, 0-180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass_point(bearing: float) -> str:
    return _COMPASS[int((bearing + 22.5) % 360 // 45)]
