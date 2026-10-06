"""Real-world weather (METAR) for airports, from aviationweather.gov (free, no key).

MSFS live weather is built from the same METARs, so clouds, visibility, temperature and dew point (which the sim
doesn't give through SimConnect) can be said in the ATIS. Wind and QNH still come from the sim when the aircraft
is at the airport: those are what the pilot sees, and what Tower already says in clearances.

Fetching never blocks the radio: `get()` returns what is cached (or None) and refreshes in a background thread.
Set ATC_METAR=off to never go online (preset weather in the sim: the METAR wouldn't match it).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field

URL = "https://aviationweather.gov/api/data/metar?ids={ids}&format=json"
MAX_AGE_S = 600.0
RETRY_S = 120.0
TIMEOUT_S = 4.0


@dataclass
class Cloud:
    cover: str  # FEW SCT BKN OVC (VV = vertical visibility)
    base_ft: int | None
    kind: str = ""  # CB / TCU


@dataclass
class Metar:
    icao: str
    raw: str
    obs_time: float | None = None  # unix time
    wind_dir_deg: float | None = None  # None with "VRB"
    wind_kt: float | None = None
    gust_kt: float | None = None
    visibility_m: int | None = None  # 9999 = 10 km or more
    temp_c: float | None = None
    dew_c: float | None = None
    qnh_hpa: float | None = None
    clouds: list[Cloud] = field(default_factory=list)
    cavok: bool = False
    weather: list[str] = field(default_factory=list)  # METAR present weather groups: "-RA", "BR", "TSRA"


_cache: dict[str, tuple[float, Metar | None]] = {}
_lock = threading.Lock()
_inflight: set[str] = set()


def enabled() -> bool:
    return os.environ.get("ATC_METAR", "on").lower() not in ("off", "0", "no", "false")


def get(icao: str, wait_s: float = 0.0) -> Metar | None:
    """Cached METAR for `icao` (None if not known yet). Starts a refresh when it is old; `wait_s` > 0 waits up
    to that long for a first result (startup, tests)."""
    icao = icao.upper()
    if not enabled():  # offline: only what was put in by hand
        with _lock:
            hit = _cache.get(icao)
        return hit[1] if hit else None
    with _lock:
        hit = _cache.get(icao)
        stale = hit is None or time.time() - hit[0] > (MAX_AGE_S if hit[1] else RETRY_S)
        if stale and icao not in _inflight:
            _inflight.add(icao)
            threading.Thread(target=_refresh, args=(icao,), daemon=True, name=f"metar-{icao}").start()
    if hit is None and wait_s > 0:
        end = time.time() + wait_s
        while time.time() < end:
            with _lock:
                if icao in _cache:
                    return _cache[icao][1]
            time.sleep(0.05)
    return hit[1] if hit else None


def _refresh(icao: str) -> None:
    try:
        req = urllib.request.Request(URL.format(ids=icao), headers={"User-Agent": "vATC personal sim ATC"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8") or "[]")
        m = parse(data[0]) if data else None
    except Exception:  # noqa: BLE001 - offline / API down: no METAR, the ATIS uses what the sim gives
        m = None
    with _lock:
        _cache[icao] = (time.time(), m)
        _inflight.discard(icao)


def put(m: Metar) -> None:
    """Seed the cache (tests, or a METAR from elsewhere)."""
    with _lock:
        _cache[m.icao.upper()] = (time.time(), m)


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(d: dict) -> Metar:
    """One aviationweather.gov json record -> Metar."""
    raw = d.get("rawOb") or ""
    vis = d.get("visib")
    vis_m = None
    if isinstance(vis, str) and vis.endswith("+"):
        vis_m = 9999
    elif _num(vis) is not None:
        sm = _num(vis)
        vis_m = 9999 if sm >= 6 else int(round(sm * 1609.34 / 100.0) * 100)
    clouds = []
    for c in d.get("clouds") or []:
        cover = (c.get("cover") or "").upper()
        if cover in ("FEW", "SCT", "BKN", "OVC", "VV", "OVX"):
            base = _num(c.get("base"))
            clouds.append(Cloud("VV" if cover == "OVX" else cover, int(base) if base is not None else None,
                                (c.get("type") or "").upper()))
    tokens = raw.split()
    weather = [t for t in tokens[2:] if _is_weather(t)]
    for c in clouds:  # CB / TCU are on the raw group ("BKN020CB"), not always in the json
        for t in tokens:
            if c.base_ft is not None and t.startswith(f"{c.cover}{c.base_ft // 100:03d}") and t.endswith(("CB", "TCU")):
                c.kind = "CB" if t.endswith("CB") else "TCU"
    wdir = d.get("wdir")
    return Metar(
        icao=(d.get("icaoId") or "").upper(), raw=raw, obs_time=_num(d.get("obsTime")),
        wind_dir_deg=_num(wdir) if wdir not in ("VRB", None) else None,
        wind_kt=_num(d.get("wspd")), gust_kt=_num(d.get("wgst")),
        visibility_m=vis_m, temp_c=_num(d.get("temp")), dew_c=_num(d.get("dewp")), qnh_hpa=_num(d.get("altim")),
        clouds=clouds, cavok="CAVOK" in tokens or (d.get("cover") or "").upper() == "CAVOK", weather=weather,
    )


_WX_CODES = ("DZ", "RA", "SN", "SG", "PL", "GR", "GS", "UP", "BR", "FG", "FU", "VA", "DU", "SA", "HZ", "PO", "SQ",
             "FC", "SS", "DS", "TS", "SH", "FZ", "BC", "MI", "PR", "BL", "DR", "VC")


def _is_weather(tok: str) -> bool:
    t = tok.lstrip("+-")
    if not t or len(t) > 9 or not t.isalpha() or t in ("METAR", "SPECI", "AUTO", "COR", "CAVOK", "NOSIG", "RMK",
                                                         "NSC", "NCD", "SKC", "CLR", "BECMG", "TEMPO"):
        return False
    return all(t[i:i + 2] in _WX_CODES for i in range(0, len(t), 2)) and len(t) % 2 == 0
