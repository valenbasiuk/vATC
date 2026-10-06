"""Filed flight plan from a SimBrief OFP (json v2, field names checked against a real OFP, 2026-10).

Code turns the plan into the clearance (atc.clearance); the LLM only sees it as a fact.

    python tools/probe_simbrief.py --username NAME     # saves simbrief_last.json
    python -m atc --airport SABE --simbrief simbrief_last.json
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field
from pathlib import Path

_RESERVED_SQUAWKS = {"7500", "7600", "7700", "7000", "2000", "1200", "0000"}


@dataclass
class Fix:
    """A navlog point: 'ATOVO' at its position, planned altitude, part of the SID/STAR or not."""

    ident: str
    lat: float
    lon: float
    alt_ft: int | None = None
    sid_star: bool = False


@dataclass
class FlightPlan:
    callsign: str
    rules: str  # "I" IFR, "V" VFR, "Y"/"Z" mixed
    aircraft_type: str
    origin: str
    destination: str
    destination_name: str
    alternate: str | None
    route: str  # ICAO route, SID/STAR included, e.g. "ATOVO4B ATOVO W5 PEDRO DCT ESKON DCT"
    sid: str | None  # ICAO name, e.g. "ATOVO4B"
    sid_transition: str | None
    cruise_ft: int | None
    planned_runway: str | None
    dest_runway: str | None = None  # SimBrief destination plan_rwy: expected arrival runway in calm wind
    fixes: list[Fix] = field(default_factory=list)  # route waypoints in order (no TOC/TOD/airports)
    tod: tuple[float, float] | None = None  # planned top of descent position
    dest_trans_alt_ft: int | None = None  # destination transition altitude (Argentina: 3000 ft)

    @property
    def squawk(self) -> str:
        return assign_squawk(self.callsign)

    @property
    def is_ifr(self) -> bool:
        return self.rules.upper() in ("I", "Y")


def assign_squawk(callsign: str) -> str:
    """Stable 4-digit octal code per callsign (same flight -> same code), never a special code."""
    n = zlib.crc32(callsign.upper().encode())
    while True:
        code = "".join(str((n >> (3 * i)) & 7) for i in range(4))
        if code[0] != "0" and code not in _RESERVED_SQUAWKS:
            return code
        n = (n >> 1) + 1


def load_simbrief(path: Path) -> FlightPlan:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    gen, atc, org, dst = data.get("general", {}), data.get("atc", {}), data.get("origin", {}), data.get("destination", {})
    alts = data.get("alternate")
    alt = alts[0] if isinstance(alts, list) and alts else alts if isinstance(alts, dict) else None
    route = (gen.get("route_ifps") or gen.get("route") or "").strip()
    sid = (gen.get("sid_ident") or "").strip() or None
    # SimBrief's sid_ident is the 6-char FMS name (ATOV4B); the ICAO name (ATOVO4B) is the first route_ifps token.
    first = route.split()[0] if route else ""
    if sid and first[:4] == sid[:4] and first[-2:] == sid[-2:]:
        sid = first
    cruise = gen.get("initial_altitude")
    nav = data.get("navlog") or []
    nav = nav.get("fix", []) if isinstance(nav, dict) else nav
    nav = [nav] if isinstance(nav, dict) else nav
    fixes, tod = [], None
    for f in nav:
        try:
            lat, lon = float(f["pos_lat"]), float(f["pos_long"])
        except (KeyError, TypeError, ValueError):
            continue
        if f.get("ident") == "TOD":
            tod = (lat, lon)
        if f.get("type") in ("wpt", "vor", "ndb") and f.get("ident"):
            fix_alt = str(f.get("altitude_feet") or "")
            fixes.append(Fix(f["ident"].upper(), lat, lon, int(fix_alt) if fix_alt.isdigit() else None,
                             str(f.get("is_sid_star")) == "1"))
    ta = str(dst.get("trans_alt") or "").lstrip("0")
    return FlightPlan(
        callsign=(atc.get("callsign") or "").strip().upper(),
        rules=(atc.get("flight_rules") or "I").strip().upper(),
        aircraft_type=(data.get("aircraft", {}).get("icaocode") or "").strip(),
        origin=(org.get("icao_code") or "").strip().upper(),
        destination=(dst.get("icao_code") or "").strip().upper(),
        destination_name=(dst.get("name") or "").strip().title(),
        alternate=(alt.get("icao_code") or None) if alt else None,
        route=route,
        sid=sid,
        sid_transition=(gen.get("sid_trans") or "").strip() or None,
        cruise_ft=int(cruise) if str(cruise or "").isdigit() else None,
        planned_runway=(org.get("plan_rwy") or "").strip() or None,
        dest_runway=(dst.get("plan_rwy") or "").strip() or None,
        fixes=fixes,
        tod=tod,
        dest_trans_alt_ft=int(ta) if ta.isdigit() else None,
    )
