"""Runway-in-use selection. Decided in code, then given to the LLM as a fact (roadmap item 10)."""

from __future__ import annotations

import math

from atc.models import Airport, Runway

CALM_KT = 3.0


def headwind_kt(wind_dir_deg: float, wind_kt: float, runway_heading_deg: float) -> float:
    """Positive = headwind, negative = tailwind."""
    return wind_kt * math.cos(math.radians(wind_dir_deg - runway_heading_deg))


def crosswind_kt(wind_dir_deg: float, wind_kt: float, runway_heading_deg: float) -> float:
    return abs(wind_kt * math.sin(math.radians(wind_dir_deg - runway_heading_deg)))


def runway_in_use(airport: Airport, wind_dir_deg: float | None, wind_kt: float | None) -> Runway | None:
    """Best headwind. In calm wind, the longest runway. None if there is nothing to decide on.

    TODO: wind from the sim is TRUE direction and runway headings here are TRUE too, so the maths
    is consistent; controllers announce MAGNETIC wind, so spoken values will differ slightly
    outside areas with near-zero variation (Argentina is about 5-10 degrees west).
    """
    candidates = [r for r in airport.runways if r.heading_deg is not None]
    if not candidates:
        return None
    if wind_dir_deg is None or wind_kt is None or wind_kt <= CALM_KT:
        return max(candidates, key=lambda r: r.length_ft or 0)
    return max(candidates, key=lambda r: headwind_kt(wind_dir_deg, wind_kt, r.heading_deg))
