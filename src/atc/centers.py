"""Area control anywhere in the world: which Center sector (its callsign and frequency) owns a position at a level.

Data: data/centers.json, made on this PC by tools/build_centers.py from VATSIM's sector data (VATGlasses, vNAS,
VATSpy), hand research (data/centers_extra.yaml) and the Navigraph frequencies Little Navmap ships. Each sector
is a polygon with a floor and a ceiling; the smallest one containing the point wins (they are stored smallest
first). A few US ARTCCs come as an outline with radio sites instead: the nearest site's sector owns the point.
Without the file every lookup returns None and world.py falls back to the navdata FIRs, as before.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path

PATH = Path("data/centers.json")
CELL_DEG = 5.0


@dataclass(frozen=True)
class Sector:
    pid: str  # "sg/FAC", "vnas/ZLA/LAX_25_CTR", "vatspy/SVZM"
    callsign: str  # "Asuncion Control" (what the controller says, ASCII)
    mhz: float
    country: str  # ISO: "PY" (phraseology, voices)
    fir: str  # "SGFA" when known
    source: str
    note: str | None = None

    @property
    def name(self) -> str:
        """The callsign without "Control" / "Center" / "Radio" ...: "Asuncion"."""
        words = self.callsign.split()
        return " ".join(words[:-1]) if len(words) > 1 else self.callsign


class _Data:
    def __init__(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.positions: dict[str, dict] = raw.get("positions", {})
        self.volumes = []  # (pid | None, lo_ft, hi_ft, ring [(lat, lon)], bbox, sites)
        self.grid: dict[tuple[int, int], list[int]] = {}
        for pid, lo, hi, flat, sites in raw.get("volumes", []):
            ring = list(zip(flat[0::2], flat[1::2]))
            if len(ring) < 3:
                continue
            bb = (min(p[0] for p in ring), max(p[0] for p in ring), min(p[1] for p in ring), max(p[1] for p in ring))
            i = len(self.volumes)
            self.volumes.append((pid, lo, hi, ring, bb, sites))
            for cy in range(int(math.floor(bb[0] / CELL_DEG)), int(math.floor(bb[1] / CELL_DEG)) + 1):
                for cx in range(int(math.floor(bb[2] / CELL_DEG)), int(math.floor(bb[3] / CELL_DEG)) + 1):
                    cx = ((cx * CELL_DEG + 180.0) % 360.0 - 180.0) / CELL_DEG  # rings unwrapped past 180
                    self.grid.setdefault((cy, int(cx)), []).append(i)
        for cell in self.grid.values():
            cell.sort()  # stored smallest first: keep that order per cell

    def sector(self, pid: str) -> Sector | None:
        p = self.positions.get(pid)
        if p is None:
            return None
        return Sector(pid, p.get("cs", ""), float(p["mhz"]), p.get("cc", ""), p.get("fir", ""), p.get("src", ""),
                      p.get("note"))


_lock = threading.Lock()
_data: _Data | None = None
_tried = False


def _load() -> _Data | None:
    global _data, _tried
    with _lock:
        if not _tried:
            _tried = True
            if PATH.exists():
                try:
                    _data = _Data(PATH)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    print(f"[centers: {PATH} unreadable ({exc}): run python tools/build_centers.py]")
        return _data


def reset(path: Path | None = None) -> None:
    """Tests: load another file (or none) next time."""
    global PATH, _data, _tried
    with _lock:
        if path is not None:
            PATH = path
        _data, _tried = None, False


def available() -> bool:
    return _load() is not None


def _inside(lat: float, lon: float, ring) -> bool:
    hit = False
    for (y1, x1), (y2, x2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def _contains(v, lat: float, lon: float) -> bool:
    _, _, _, ring, bb, _ = v
    if not bb[0] <= lat <= bb[1]:
        return False
    return any(bb[2] <= x <= bb[3] and _inside(lat, x, ring) for x in (lon, lon + 360.0))


def _owner(v, lat: float, lon: float) -> str | None:
    pid, _, _, _, _, sites = v
    if pid is not None or not sites:
        return pid
    c = math.cos(math.radians(lat))
    return min(sites, key=lambda s: (s[0] - lat) ** 2 + ((s[1] - lon) * c) ** 2)[2]


def sector_at(lat: float, lon: float, alt_ft: float | None = None) -> Sector | None:
    """The Center sector that owns this point at this altitude (feet; None = FL200)."""
    d = _load()
    if d is None:
        return None
    alt = 20000.0 if alt_ft is None else max(0.0, alt_ft)
    cell = (int(math.floor(lat / CELL_DEG)), int(math.floor(lon / CELL_DEG)))
    for i in d.grid.get(cell, ()):
        v = d.volumes[i]
        if v[1] <= alt < v[2] and _contains(v, lat, lon):
            pid = _owner(v, lat, lon)
            if pid is not None:
                return d.sector(pid)
    return None


def by_frequency(mhz: float, lat: float, lon: float, max_nm: float = 400.0) -> Sector | None:
    """A Center sector on this frequency near the aircraft (tuned before anyone handed it over), nearest first."""
    d = _load()
    if d is None:
        return None
    best = None
    c = math.cos(math.radians(lat))
    for v in d.volumes:
        pids = {v[0]} if v[0] is not None else {s[2] for s in (v[5] or [])}
        for pid in pids:
            p = d.positions.get(pid)
            if p is None or abs(float(p["mhz"]) - mhz) >= 0.003:
                continue
            if _contains(v, lat, lon):
                nm = 0.0
            else:
                bb = v[4]
                y = min(max(lat, bb[0]), bb[1])
                x = min(max(lon, bb[2]), bb[3])
                nm = math.hypot(y - lat, (x - lon) * c) * 60.0
            if nm <= max_nm and (best is None or nm < best[0]):
                best = (nm, pid)
    return d.sector(best[1]) if best else None
