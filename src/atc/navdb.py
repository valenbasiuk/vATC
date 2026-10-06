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
        if not 118.0 <= mhz <= 136.99 or "RAMP" in (name or "").upper():  # airline ramp control isn't ATC
            continue
        same = [f for f in airport.frequencies if abs(f.mhz - mhz) < 0.005]
        if same:
            for f in same:  # KSFO's 128.325 "NORCAL" in the sim: "NorCal Approach", not "San Francisco ..."
                if f.spoken is None:
                    f.spoken = _spoken(airport, f.kind, name, own_name)
            continue
        airport.frequencies.append(Frequency(kind, mhz, f"{name or ''} (sim)".strip(),
                                             _spoken(airport, kind, name, own_name)))
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
    # Runway thresholds the YAML lacks (KSFO had none): without them every runway sits on the airport reference
    # point, and "on final", "on the runway", vacate and taxi crossings can't tell parallel runways apart.
    if any(r.lat is None or r.lon is None for r in airport.runways):
        ends = {str(n).lstrip("0"): (la, lo, ln) for ln, _, _, pair in _runway_rows(con, aid)
                for n, _, la, lo in pair if n and la is not None}
        filled = 0
        for r in airport.runways:
            end = ends.get(r.ident.lstrip("0"))
            if (r.lat is None or r.lon is None) and end is not None:
                r.lat, r.lon = round(end[0], 6), round(end[1], 6)
                r.length_ft = r.length_ft or end[2]
                filled += 1
        if filled:
            added.append(f"{filled} runway thresholds")
    if airport.trans_alt_ft is None:
        row = con.execute("select transition_altitude from airport where airport_id=?", (aid,)).fetchone()
        if row and row[0]:
            airport.trans_alt_ft = int(row[0])
            added.append(f"transition altitude {airport.trans_alt_ft}")
    return added


# --- which airport is here ---------------------------------------------------------------------------

def _box(lat: float, lon: float, nm: float) -> tuple[float, float, float, float]:
    import math

    dlat = nm / 60.0
    dlon = nm / (60.0 * max(0.1, math.cos(math.radians(lat))))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def airports_near(lat: float, lon: float, within_nm: float, con: sqlite3.Connection | None = None,
                  ) -> list[tuple[float, str]]:
    """(distance NM, ident) of the sim's airports with a runway near a point, nearest first (no heliports)."""
    con = con or connect()
    if con is None:
        return []
    s, n, w, e = _box(lat, lon, within_nm)
    rows = con.execute("""select ident, laty, lonx, left_lonx, top_laty, right_lonx, bottom_laty, longest_runway_length
                          from airport where laty between ? and ? and lonx between ? and ?
                          and is_closed = 0 and num_runways > 0""", (s, n, w, e)).fetchall()
    ranked = []
    for ident, la, lo, left, top, right, bottom, longest in rows:
        d = distance_nm(lat, lon, la, lo)
        inside = left is not None and left <= lon <= right and bottom <= lat <= top
        # inside an airport's area beats distance (a Coast Guard strip next to KSFO's terminal); the bigger
        # airport wins when areas overlap
        ranked.append((not inside, -(longest or 0) if inside else d, d, ident))
    ranked.sort()
    return [(d, ident) for _, _, d, ident in ranked if d <= within_nm]


def airport_on_frequency(lat: float, lon: float, mhz: float, within_nm: float,
                         con: sqlite3.Connection | None = None) -> str | None:
    """Ident of the nearest airport within `within_nm` that has a station on `mhz` (the pilot tuned it)."""
    con = con or connect()
    if con is None:
        return None
    s, n, w, e = _box(lat, lon, within_nm)
    hz, khz = int(round(mhz * 1_000_000)), int(round(mhz * 1000))
    rows = con.execute("""select distinct a.ident, a.laty, a.lonx from com c join airport a on a.airport_id = c.airport_id
                          where a.laty between ? and ? and a.lonx between ? and ?
                          and (c.frequency between ? and ? or c.frequency between ? and ?)""",
                       (s, n, w, e, hz - 5000, hz + 5000, khz - 5, khz + 5)).fetchall()
    best = min(((distance_nm(lat, lon, la, lo), ident) for ident, la, lo in rows), default=None)
    return best[1] if best and best[0] <= within_nm else None


def country_from_region(region: str | None) -> str:
    """ICAO region code -> ISO country, only where it changes phraseology (US = FAA)."""
    r = (region or "").upper()
    if r.startswith("K") or r in ("PA", "PH", "PG", "PO", "PP", "TJ"):
        return "US"
    return {"SA": "AR"}.get(r, "")


def build_airport(icao: str, con: sqlite3.Connection | None = None) -> Airport | None:
    """An airport file made only from the sim's own data, for airports OurAirports doesn't have (add-ons,
    fictional fields). Frequencies, magvar and approaches are added by `enrich` when it is loaded."""
    from atc.airports.gen import default_pattern
    from atc.models import Runway

    con = con or connect()
    if con is None:
        return None
    row = con.execute("""select airport_id, ident, name, region, altitude, laty, lonx, has_tower_object, tower_frequency
                         from airport where ident = ?""", (icao.upper(),)).fetchone()
    if row is None:
        return None
    aid, ident, name, region, elev, lat, lon, tower_obj, twr = row
    country = country_from_region(region)
    runways = []
    for length, surface, patt, ends in _runway_rows(con, aid):
        for ename, hdg, elat, elon in ends:
            runways.append(Runway(ident=ename, heading_deg=round(hdg, 1) if hdg is not None else None,
                                  length_ft=length, surface=surface, lat=elat, lon=elon,
                                  pattern_alt_agl_ft=int(patt) if patt else (1000 if country == "US" else None),
                                  pattern_direction=default_pattern(ename, country == "US")))
    return Airport(icao=ident, name=name or ident, lat=lat, lon=lon, elevation_ft=float(elev or 0.0),
                   country=country, towered=bool(twr), runways=runways,
                   notes=["Made from the sim's scenery (Little Navmap db): check names and pattern rules."],
                   needs_review=True)


def _runway_rows(con, aid: int):
    q = """select r.length, r.surface, r.pattern_altitude,
                  pe.name, pe.heading, pe.laty, pe.lonx, se.name, se.heading, se.laty, se.lonx
           from runway r join runway_end pe on pe.runway_end_id = r.primary_end_id
                         join runway_end se on se.runway_end_id = r.secondary_end_id
           where r.airport_id = ?"""
    for length, surface, patt, pn, ph, pla, plo, sn, sh, sla, slo in con.execute(q, (aid,)):
        if surface in ("W",):  # water runways: not for this ATC
            continue
        yield length, surface, patt, [(pn, ph, pla, plo), (sn, sh, sla, slo)]


def _spoken(airport: Airport, kind: str, name: str | None, own_name: str) -> str | None:
    """Station name from the sim's com name when it isn't '<this airport> <role>': 'Resistencia Approach' at SARC,
    'NorCal Departure' at KSFO, 'Oakland Center'. None = callsign_for's '<airport> <role>'."""
    words = [w for w in (name or "").split() if w.upper() not in ("INTL", "INTERNATIONAL", "AIRPORT", "AERO")]
    if not words:
        return None
    if kind == "CTR":  # "OAKLAND SAN FRANCISCO BAY" -> "Oakland Center" (ICAO: "... Control")
        return f"{_title(words[0])} {'Center' if airport.faa else 'Control'}"
    first = words[0].lower()
    if kind in _SUFFIX and first not in (own_name, airport.icao.lower(), airport.name.split()[0].lower()):
        return f"{_title(' '.join(words))} {_SUFFIX[kind]}"
    return None


def _title(s: str) -> str:
    """'NORCAL' -> 'NorCal', other names title-cased ('RESISTENCIA' -> 'Resistencia')."""
    return {"NORCAL": "NorCal", "SOCAL": "SoCal"}.get(s.upper(), s.title())


_APPROACH_WORDS = {"ILS": "ILS", "LOC": "localizer", "RNAV": "RNAV", "GPS": "RNAV", "VORDME": "VOR DME",
                   "VOR": "VOR", "NDBDME": "NDB DME", "NDB": "NDB"}


def approach_type(airport: Airport, runway: str) -> str | None:
    """Best approach to that runway as a controller says it: 'ILS', else 'RNAV', 'VOR DME', ... None = unknown."""
    want_key = runway.upper().lstrip("0")  # OurAirports says "1L", the sim "01L"
    kinds = next((v for k, v in airport.approaches.items() if k.upper().lstrip("0") == want_key), [])
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


# --- FIR boundaries (Navigraph db that ships with Little Navmap; AIRAC 1801 on Valen's PC) -----------------

def navigraph_path() -> Path | None:
    """little_navmap_navigraph.sqlite next to the sim's database (it has the FIR/UIR boundaries; the sim's has none)."""
    main = default_path()
    if main is None:
        return None
    p = Path(main).with_name("little_navmap_navigraph.sqlite")
    return p if p.exists() else None


def _ring(blob: bytes) -> list[tuple[float, float]]:
    """Boundary geometry: big-endian int32 point count, then that many big-endian float32 (lon, lat) pairs."""
    import struct

    n = struct.unpack(">i", blob[:4])[0]
    vals = struct.unpack(f">{2 * n}f", blob[4:4 + 8 * n])
    return list(zip(vals[0::2], vals[1::2]))


def _inside(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Point in polygon (ray casting)."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


@lru_cache(maxsize=256)
def _fir_at(lat_cell: float, lon_cell: float, upper: bool) -> tuple[str, float, tuple[float, float]] | None:
    path = navigraph_path()
    con = _connect(str(path)) if path else None
    if con is None:
        return None
    kinds = ("UIR", "FIR") if upper else ("FIR",)
    rows = con.execute(
        "select type, name, com_frequency, min_laty, max_laty, min_lonx, max_lonx, geometry from boundary "
        f"where type in ({','.join('?' * len(kinds))}) and com_frequency is not null and min_lonx <= max_lonx "
        "and ? between min_laty and max_laty and ? between min_lonx and max_lonx",
        (*kinds, lat_cell, lon_cell)).fetchall()
    rows.sort(key=lambda r: kinds.index(r[0]))  # above the UIR floor the UIR's frequency first
    for _, name, khz, la1, la2, lo1, lo2, geom in rows:
        if geom and _inside(lon_cell, lat_cell, _ring(geom)):
            return str(name), round(khz / 1000.0, 3), ((la1 + la2) / 2, (lo1 + lo2) / 2)
    return None


def fir_at(lat: float, lon: float, alt_ft: float | None = None) -> tuple[str, float, tuple[float, float]] | None:
    """(FIR name, MHz, centre of its box) for a position, e.g. ('OAKLAND', 127.8, ...). Above FL245 the UIR's
    frequency where there is one. Looked up per ~6 NM cell (cached). None without the database."""
    try:
        return _fir_at(round(lat, 1), round(lon, 1), (alt_ft or 0) >= 24500)
    except sqlite3.Error:
        return None
