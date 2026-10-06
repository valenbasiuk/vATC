"""Little Navmap's scenery database (MSFS 2024), read-only: the sim's own airport data.

Little Navmap compiles the installed MSFS scenery into SQLite (%APPDATA%/ABarthel/little_navmap_db/
little_navmap_msfs24.sqlite). That is exactly what is painted in the sim, so it beats OpenStreetMap for taxiways
(OSM had a taxiway G at Rosario that doesn't exist) and fills gaps in OurAirports:
  - taxi network: named taxiway segments, holding points (HSND/IHSND), stands -> atc.taxi.TaxiNetwork
  - frequencies (Rosario Ground 121.85 was missing), magnetic variation, ILS, approach types per runway
  - waypoints / VOR / NDB positions, for "request direct X" off the filed route

Set ATC_LNM_DB to use another file. Nothing here writes to the database or to the airport YAMLs.
"""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path

from atc.geo import distance_nm
from atc.models import Airport, Frequency

M_PER_NM = 1852.0
# Little Navmap com.type -> our frequency kinds (facility.py)
_COM_KIND = {"A": "APP", "D": "DEP", "T": "TWR", "G": "GND", "C": "CLD", "CPT": "CLD", "RCD": "CLD",
             "ATIS": "ATIS", "AWOS": "ATIS", "ASOS": "ATIS", "UNIC": "UNICOM", "CTAF": "CTAF", "TMA": "APP",
             "CTR": "CTR"}
_SUFFIX = {"APP": "Approach", "DEP": "Departure", "TWR": "Tower", "GND": "Ground", "CLD": "Delivery"}
_HOLDS = {"HS": "runway", "HSND": "runway", "IHS": "ILS", "IHSND": "ILS"}


def default_path() -> Path | None:
    env = os.environ.get("ATC_LNM_DB")
    if env:
        return Path(env)
    base = Path(os.environ.get("APPDATA", "")) / "ABarthel" / "little_navmap_db"
    for name in ("little_navmap_msfs24.sqlite", "little_navmap_msfs.sqlite"):
        if (base / name).exists():
            return base / name
    return None


@lru_cache(maxsize=4)
def _connect(path: str) -> sqlite3.Connection | None:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        con.execute("select 1 from airport limit 1")
        return con
    except sqlite3.Error:
        return None


def connect(path: Path | None = None) -> sqlite3.Connection | None:
    path = path or default_path()
    return _connect(str(path)) if path and Path(path).exists() else None


def _airport_id(con, icao: str) -> int | None:
    row = con.execute("select airport_id from airport where ident=?", (icao.upper(),)).fetchone()
    return row[0] if row else None


# --- airport facts -----------------------------------------------------------------------------------

def enrich(airport: Airport, con: sqlite3.Connection | None = None) -> list[str]:
    """Add what the sim knows and the YAML doesn't: missing frequencies (after the YAML's own, so those stay
    first), magnetic variation, approach types per runway. Returns a short list of what was added."""
    con = con or connect()
    if con is None:
        return []
    aid = _airport_id(con, airport.icao)
    if aid is None:
        return []
    added = []
    own_name = (airport.spoken_name or airport.name.split()[0]).lower()
    for ctype, raw, name in con.execute("select type, frequency, name from com where airport_id=?", (aid,)):
        kind = _COM_KIND.get((ctype or "").upper())
        if kind is None or not raw:
            continue
        mhz = round(raw / 1_000_000.0 if raw > 1_000_000 else raw / 1000.0, 3)
        if any(abs(f.mhz - mhz) < 0.005 for f in airport.frequencies):
            continue
        spoken = None
        words = [w for w in (name or "").split() if w.upper() not in ("INTL", "INTERNATIONAL", "AIRPORT", "AERO")]
        if words and kind in _SUFFIX and words[0].lower() not in (own_name, airport.icao.lower()) \
                and words[0].lower() != airport.name.split()[0].lower():
            spoken = f"{' '.join(words).title()} {_SUFFIX[kind]}"  # SARC's approach is "Resistencia Approach"
        airport.frequencies.append(Frequency(kind, mhz, f"{name or ''} (sim)".strip(), spoken))
        added.append(f"{kind} {mhz:.3f}")
    if airport.mag_var_deg is None:
        row = con.execute("select mag_var from airport where airport_id=?", (aid,)).fetchone()
        if row and row[0] is not None:
            airport.mag_var_deg = round(float(row[0]), 1)
            added.append(f"magvar {airport.mag_var_deg}")
    approaches: dict[str, list[str]] = {}
    for rwy, typ in con.execute("select runway_name, type from approach where airport_id=?", (aid,)):
        if rwy and typ:
            approaches.setdefault(str(rwy).zfill(2), [])
            if typ not in approaches[str(rwy).zfill(2)]:
                approaches[str(rwy).zfill(2)].append(typ)
    if approaches and not airport.approaches:
        airport.approaches = approaches
    return added


_APPROACH_WORDS = {"ILS": "ILS", "LOC": "localizer", "RNAV": "RNAV", "GPS": "RNAV", "VORDME": "VOR DME",
                   "VOR": "VOR", "NDBDME": "NDB DME", "NDB": "NDB"}


def approach_type(airport: Airport, runway: str) -> str | None:
    """Best approach to that runway as a controller says it: 'ILS', else 'RNAV', 'VOR DME', ... None = unknown."""
    kinds = airport.approaches.get(runway.zfill(2)) or airport.approaches.get(runway) or []
    for want in ("ILS", "RNAV", "GPS", "LOC", "VORDME", "VOR", "NDBDME", "NDB"):
        if want in kinds:
            return _APPROACH_WORDS[want]
    return None


# --- taxi network ------------------------------------------------------------------------------------

def taxi_network(icao: str, con: sqlite3.Connection | None = None):
    """The sim's taxiway graph for an airport, or None. Segments meet at exactly shared points."""
    from atc.taxi import TaxiNetwork, _designator

    con = con or connect()
    if con is None:
        return None
    aid = _airport_id(con, icao)
    if aid is None:
        return None
    net = TaxiNetwork()
    ids: dict[tuple[float, float], int] = {}

    def node(lon: float, lat: float) -> int:
        key = (round(lat, 7), round(lon, 7))
        if key not in ids:
            ids[key] = len(ids)
            net.nodes[ids[key]] = key
        return ids[key]

    holds: dict[int, str] = {}
    rows = con.execute("""select type, name, start_type, start_lonx, start_laty, end_type, end_lonx, end_laty
                          from taxi_path where airport_id=?""", (aid,)).fetchall()
    for typ, name, stype, slon, slat, etype, elon, elat in rows:
        if typ in ("C", "V", "RD"):  # closed, vehicle lanes, roads
            continue
        a, b = node(slon, slat), node(elon, elat)
        m = distance_nm(slat, slon, elat, elon) * M_PER_NM
        des = _designator(name or "") if typ != "P" else None
        net.edges.setdefault(a, []).append((b, m, des))
        net.edges.setdefault(b, []).append((a, m, des))
        for t, n in ((stype, a), (etype, b)):
            if t in _HOLDS:
                holds[n] = _HOLDS[t]
    if not net.edges:
        return None
    net.holds = list(holds.items())
    for name, number, suffix, lon, lat in con.execute(
            "select name, number, suffix, lonx, laty from parking where airport_id=?", (aid,)):
        if number is None:
            continue
        ref = f"{number}{suffix or ''}".upper()
        net.stands.setdefault(ref, net.nearest(lat, lon))
    return net


# --- navaids and waypoints ---------------------------------------------------------------------------

def find_fix(ident: str, near: tuple[float, float], con: sqlite3.Connection | None = None,
             max_nm: float = 400.0) -> tuple[float, float] | None:
    """Position of a waypoint / VOR / NDB called `ident`, the one nearest to `near` (idents repeat worldwide)."""
    con = con or connect()
    if con is None:
        return None
    best = None
    for table in ("waypoint", "vor", "ndb"):
        for lon, lat in con.execute(f"select lonx, laty from {table} where ident=?", (ident.upper(),)):
            d = distance_nm(near[0], near[1], lat, lon)
            if d <= max_nm and (best is None or d < best[0]):
                best = (d, (lat, lon))
    return best[1] if best else None
