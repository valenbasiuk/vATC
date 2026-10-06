"""Runway-in-use selection. Decided in code, then given to the LLM as a fact (roadmap item 10)."""

from __future__ import annotations

import math

from atc.models import Airport, Runway

CALM_KT = 3.0
MAX_TAILWIND_KT = 5.0  # a preferred runway stays in use up to this tailwind (ICAO noise-preferential limit)


def headwind_kt(wind_dir_deg: float, wind_kt: float, runway_heading_deg: float) -> float:
    """Positive = headwind, negative = tailwind."""
    return wind_kt * math.cos(math.radians(wind_dir_deg - runway_heading_deg))


def crosswind_kt(wind_dir_deg: float, wind_kt: float, runway_heading_deg: float) -> float:
    return abs(wind_kt * math.sin(math.radians(wind_dir_deg - runway_heading_deg)))


def magnetic(airport: Airport, true_deg: float | None) -> float | None:
    """True direction (as the sim gives wind) -> magnetic, as a controller says it. Unknown variation: unchanged."""
    if true_deg is None or airport.mag_var_deg is None:
        return true_deg
    return (true_deg - airport.mag_var_deg) % 360


def runway_in_use(
    airport: Airport, wind_dir_deg: float | None, wind_kt: float | None, preferred: str | None = None
) -> Runway | None:
    """Best headwind. In calm wind, the longest runway. None if there is nothing to decide on.

    `preferred` (the flight plan's departure runway, which its SID belongs to) is kept in calm or
    unknown wind and up to MAX_TAILWIND_KT of tailwind, so Ground doesn't send an ATOVO4B (runway 31)
    departure to runway 13 on a calm day.

    TODO: wind from the sim is TRUE direction and runway headings here are TRUE too, so the maths
    is consistent; controllers announce MAGNETIC wind, so spoken values will differ slightly
    outside areas with near-zero variation (Argentina is about 5-10 degrees west).
    """
    candidates = [r for r in airport.runways if r.heading_deg is not None]
    if not candidates:
        return None
    pref = next((r for r in candidates if preferred and r.ident.lstrip("0") == preferred.lstrip("0")), None)
    if pref is not None and (
        wind_dir_deg is None or wind_kt is None or headwind_kt(wind_dir_deg, wind_kt, pref.heading_deg) >= -MAX_TAILWIND_KT
    ):
        return pref
    if wind_dir_deg is None or wind_kt is None or wind_kt <= CALM_KT:
        return max(candidates, key=lambda r: r.length_ft or 0)
    return max(candidates, key=lambda r: headwind_kt(wind_dir_deg, wind_kt, r.heading_deg))
