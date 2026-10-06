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


def _same(ident: str, other: str) -> bool:
    return ident.lstrip("0").upper() == other.lstrip("0").upper()


def runway_config(airport: Airport, wind_dir_deg: float | None, wind_kt: float | None) -> dict | None:
    """The airport's runway configuration for this wind (YAML `runway_configs`: arrival / departure runway lists,
    KSFO west plan: land 28L/28R, depart 1L/1R). The one whose arrival runways have the best headwind; in calm or
    unknown wind the first one listed. None if the airport has none, or the best one still has a tailwind on
    its arrival runways beyond MAX_TAILWIND_KT (then the plain headwind rule decides)."""
    best, best_hw = None, None
    for cfg in airport.runway_configs:
        arr = [r for r in airport.runways if r.heading_deg is not None
               and any(_same(r.ident, i) for i in cfg.get("arrival", []))]
        if not arr:
            continue
        if wind_dir_deg is None or wind_kt is None or wind_kt <= CALM_KT:
            return cfg
        hw = max(headwind_kt(wind_dir_deg, wind_kt, r.heading_deg) for r in arr)
        if best_hw is None or hw > best_hw:
            best, best_hw = cfg, hw
    if best is None or best_hw < -MAX_TAILWIND_KT:
        return None
    return best


def runway_in_use(
    airport: Airport, wind_dir_deg: float | None, wind_kt: float | None, preferred: str | None = None,
    use: str | None = None,
) -> Runway | None:
    """Best headwind. In calm wind, the longest runway. None if there is nothing to decide on.

    `preferred` (the flight plan's departure runway, which its SID belongs to) is kept in calm or
    unknown wind and up to MAX_TAILWIND_KT of tailwind, so Ground doesn't send an ATOVO4B (runway 31)
    departure to runway 13 on a calm day.

    `use` = "departure" / "arrival": at an airport with `runway_configs` (separate departure and arrival runways),
    a runway from that list of the configuration for this wind (the planned runway if it is in the list, else the
    first one listed with the best headwind). Without configs, or with `use` None, both are the same runway.

    TODO: wind from the sim is TRUE direction and runway headings here are TRUE too, so the maths
    is consistent; controllers announce MAGNETIC wind, so spoken values will differ slightly
    outside areas with near-zero variation (Argentina is about 5-10 degrees west).
    """
    candidates = [r for r in airport.runways if r.heading_deg is not None]
    if not candidates:
        return None
    cfg = runway_config(airport, wind_dir_deg, wind_kt) if use in ("departure", "arrival") else None
    if cfg is not None:
        listed = cfg.get(use) or cfg.get("arrival", [])
        subset = [r for i in listed for r in candidates if _same(r.ident, i)]  # in the YAML's order
        windy = wind_dir_deg is not None and wind_kt is not None and wind_kt > CALM_KT
        if subset and windy and max(headwind_kt(wind_dir_deg, wind_kt, r.heading_deg) for r in subset) \
                < -MAX_TAILWIND_KT:  # e.g. departures on the arrival runways when the wind turns
            subset = [r for i in cfg.get("arrival", []) for r in candidates if _same(r.ident, i)]
        if subset:
            pref = next((r for r in subset if preferred and _same(r.ident, preferred)), None)
            if pref is not None:
                return pref
            if not windy:
                return subset[0]
            best = max(headwind_kt(wind_dir_deg, wind_kt, r.heading_deg) for r in subset)
            return next(r for r in subset if headwind_kt(wind_dir_deg, wind_kt, r.heading_deg) >= best - 0.5)
    pref = next((r for r in candidates if preferred and _same(r.ident, preferred)), None)
    if pref is not None and (
        wind_dir_deg is None or wind_kt is None or headwind_kt(wind_dir_deg, wind_kt, pref.heading_deg) >= -MAX_TAILWIND_KT
    ):
        return pref
    if wind_dir_deg is None or wind_kt is None or wind_kt <= CALM_KT:
        return max(candidates, key=lambda r: r.length_ft or 0)
    return max(candidates, key=lambda r: headwind_kt(wind_dir_deg, wind_kt, r.heading_deg))


def runways_in_use(airport: Airport, wind_dir_deg: float | None, wind_kt: float | None, use: str) -> list[Runway]:
    """All runways of the configuration for `use` (the ATIS: "landing runways two eight left and two eight right"),
    or just the one runway in use when the airport has no configurations."""
    cfg = runway_config(airport, wind_dir_deg, wind_kt)
    one = runway_in_use(airport, wind_dir_deg, wind_kt, use=use)
    if cfg is None or one is None:
        return [one] if one else []
    for listed in (cfg.get(use) or [], cfg.get("arrival") or []):
        rs = [r for i in listed for r in airport.runways if _same(r.ident, i) and r.heading_deg is not None]
        if any(r is one for r in rs):
            return rs
    return [one]
