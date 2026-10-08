"""Every area control sector in the world -> data/centers.json (atc.centers reads it while flying).

    python tools/build_centers.py              # download (cached in data/cache/centers/) and build
    python tools/build_centers.py --offline    # build from the cache only
    python tools/build_centers.py --report     # also list the FIRs left without a frequency

Sources, best first for each piece of airspace:
  1. data/centers_extra.yaml: hand research (frequencies VATSIM data leaves out: the North Atlantic, the
     FIRs below), committed, wins over everything.
  2. VATGlasses data (github.com/lennycolton/vatglasses-data, CC BY-NC-SA 4.0): the VATSIM sectors of each vACC
     with their polygons, levels and owner order (first owner = the controller of that sector with everything
     staffed), callsign ("Asuncion Control") and frequency. VATSIM's umbrella positions ("South America Control",
     data/fss.json) are left out: they don't exist in the real world.
  3. vNAS data API (data-api.vnas.vatsim.net, VATSIM USA): ARTCCs VATGlasses has only as an outline get their
     staffed ("starred") sectors, each placed at its radio sites: a point belongs to the nearest site.
  4. VATSpy (github.com/vatsimnetwork/vatspy-data-project, CC BY-SA 4.0): FIR outlines and names for whatever is
     still uncovered, with the real-world frequency of the Navigraph data that ships with Little Navmap (AIRAC 1801:
     old, but area control frequencies rarely move) at that spot.
The output is derived from CC BY-NC-SA data: personal use, not committed (.gitignore).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import time
import unicodedata
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CACHE = ROOT / "data" / "cache" / "centers"
OUT = ROOT / "data" / "centers.json"
EXTRA = ROOT / "data" / "centers_extra.yaml"
VATGLASSES_ZIP = "https://codeload.github.com/lennycolton/vatglasses-data/zip/refs/heads/main"
VATSPY_DAT = "https://raw.githubusercontent.com/vatsimnetwork/vatspy-data-project/master/VATSpy.dat"
VATSPY_GEOJSON = "https://github.com/vatsimnetwork/vatspy-data-project/releases/latest/download/Boundaries.geojson"
VNAS = "https://data-api.vnas.vatsim.net/api/artccs/{}"
AREA_TYPES = ("CTR", "FSS")
UMBRELLA_FILES = ("fss",)  # VATSIM-only top-down positions
ENGLISH_SUFFIX = {"control", "center", "centre", "radar", "radio", "information", "oceanic"}


# --- downloads --------------------------------------------------------------------------------------------

def fetch(url: str, name: str, offline: bool) -> Path:
    path = CACHE / name
    if offline or (path.exists() and time.time() - path.stat().st_mtime < 7 * 86400):
        if not path.exists():
            sys.exit(f"{path} missing: run without --offline once")
        return path
    CACHE.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "vATC personal build_centers"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    path.write_bytes(data)
    return path


# --- helpers ----------------------------------------------------------------------------------------------

_TRANSLIT = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D",
                           "ß": "ss", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "œ": "oe"})
US_FILES = {"zab", "zak", "zau", "zhn", "zhu", "zjx", "zlc", "zma", "zny", "zoa", "zob", "zse", "zsu", "zua", "zwy",
            "nodata"}  # VATGlasses folders/files of US ARTCCs (the Canadian ones have CZxx prefixes)


def ascii_name(s: str) -> str:
    return unicodedata.normalize("NFKD", s.translate(_TRANSLIT)).encode("ascii", "ignore").decode().strip()


def spoken(callsign: str) -> str:
    """What the controller says, in English: "Centro Curitiba" -> "Curitiba Center", "Polaris Control (Offshore)"
    -> "Polaris Control", "Mumbai Lower ACC - Mumbai Control" -> "Mumbai Control"."""
    cs = ascii_name(callsign)
    if " - " in cs:
        cs = cs.split(" - ")[-1]
    cs = re.sub(r"\s*\(.*?\)\s*", " ", cs).strip()
    m = re.match(r"^(Centro|Control|Controle|Radar)\s+(.+)$", cs)
    if m:
        cs = f"{m.group(2)} {'Center' if m.group(1) == 'Centro' else 'Control'}"
    return re.sub(r"\s+", " ", cs)


def dms(v: str, deg_digits: int) -> float:
    """'-221156' (DDMMSS) / '-0623904' (DDDMMSS), seconds may have decimals."""
    v = v.strip()
    sign = -1.0 if v.startswith("-") else 1.0
    v = v.lstrip("+-")
    if "." in v and len(v.split(".")[0]) <= 3:  # already decimal degrees
        return sign * float(v)
    d, m, s = v[:deg_digits], v[deg_digits:deg_digits + 2], v[deg_digits + 2:] or "0"
    return sign * (int(d) + int(m) / 60.0 + float(s) / 3600.0)


def ring_area(ring: list[tuple[float, float]]) -> float:
    """Shoelace in degrees^2 (with lon scaled by cos(lat)): only to put small sectors before big ones."""
    if len(ring) < 3:
        return 0.0
    c = math.cos(math.radians(sum(p[0] for p in ring) / len(ring)))
    a = 0.0
    for (y1, x1), (y2, x2) in zip(ring, ring[1:] + ring[:1]):
        a += x1 * c * y2 - x2 * c * y1
    return abs(a) / 2.0


def unwrap(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Across the antimeridian: negative longitudes + 360, so the ring is continuous."""
    lons = [p[1] for p in ring]
    if max(lons) - min(lons) > 180.0:
        return [(la, lo + 360.0 if lo < 0 else lo) for la, lo in ring]
    return ring


def inside(lat: float, lon: float, ring: list[tuple[float, float]]) -> bool:
    hit = False
    for (y1, x1), (y2, x2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def bbox(ring) -> tuple[float, float, float, float]:
    return min(p[0] for p in ring), max(p[0] for p in ring), min(p[1] for p in ring), max(p[1] for p in ring)


def inside_any(lat: float, lon: float, ring, bb=None) -> bool:
    bb = bb or bbox(ring)
    if not bb[0] <= lat <= bb[1]:
        return False
    return any(bb[2] <= x <= bb[3] and inside(lat, x, ring) for x in (lon, lon + 360.0))


def interior_points(ring: list[tuple[float, float]], n: int = 6) -> list[tuple[float, float]]:
    """Up to n*n grid points inside the ring (a coverage test that doesn't hang on one point)."""
    ys, xs = [p[0] for p in ring], [p[1] for p in ring]
    out = []
    for i in range(1, n + 1):
        for j in range(1, n + 1):
            y = min(ys) + (max(ys) - min(ys)) * i / (n + 1)
            x = min(xs) + (max(xs) - min(xs)) * j / (n + 1)
            if inside(y, x, ring):
                out.append((y, x))
    return out


def label_point(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """A point inside the ring: the vertex average if it is inside, else the inside point of a grid nearest it."""
    la = sum(p[0] for p in ring) / len(ring)
    lo = sum(p[1] for p in ring) / len(ring)
    if inside(la, lo, ring):
        return la, lo
    ys, xs = [p[0] for p in ring], [p[1] for p in ring]
    best = None
    for i in range(1, 12):
        for j in range(1, 12):
            y = min(ys) + (max(ys) - min(ys)) * i / 12
            x = min(xs) + (max(xs) - min(xs)) * j / 12
            if inside(y, x, ring):
                d = (y - la) ** 2 + (x - lo) ** 2
                if best is None or d < best[0]:
                    best = (d, (y, x))
    return best[1] if best else (la, lo)


# --- countries --------------------------------------------------------------------------------------------

class Countries:
    """ICAO prefix -> ISO country (majority of OurAirports idents), and the nearest airport's country."""

    def __init__(self) -> None:
        self.by_prefix: dict[str, Counter] = {}
        self.points: list[tuple[float, float, str]] = []
        path = ROOT / "data" / "airports.csv"
        if not path.exists():
            print("  (data/airports.csv missing: countries by ICAO prefix only)")
            return
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                ident, iso = row.get("ident", ""), row.get("iso_country", "")
                if len(ident) == 4 and ident.isalpha() and iso:
                    self.by_prefix.setdefault(ident[:2], Counter())[iso] += 1
                if row.get("type") in ("large_airport", "medium_airport") and iso:
                    self.points.append((float(row["latitude_deg"]), float(row["longitude_deg"]), iso))

    def of(self, prefixes: list[str], lat: float | None = None, lon: float | None = None) -> str:
        for p in prefixes:
            p = p.upper()
            if len(p) >= 4 and p[:4].isalpha():
                if p[0] in "KP" and p[1] == "Z":  # US ARTCC "KZLA", Pacific "PAZA"
                    return "US"
                c = self.by_prefix.get(p[:2])
                if c:
                    return c.most_common(1)[0][0]
        if lat is not None and self.points:
            return min(self.points, key=lambda q: (q[0] - lat) ** 2 + ((q[1] - lon) * math.cos(math.radians(lat))) ** 2)[2]
        return ""


# --- VATGlasses -------------------------------------------------------------------------------------------

def vatglasses(zpath: Path):
    """Yields (file id, positions dict, [(volume id, volume dict, owner list)]) for every data file."""
    z = zipfile.ZipFile(zpath)
    names = z.namelist()
    root = names[0].split("/")[0] + "/data/"

    def load(name):
        return json.loads(z.read(name).decode("utf-8"))

    for n in names:
        rel = n[len(root):] if n.startswith(root) else None
        if not rel or not rel.endswith(".json"):
            continue
        if "/" not in rel:  # one file per region
            d = load(n)
            yield rel[:-5], d.get("positions") or {}, [(a.get("id"), a, a.get("owner") or []) for a in
                                                         d.get("airspace") or []]
        elif rel.endswith("/positions.json"):  # a folder: positions, airspace, ownership/default.json
            fid = rel.split("/")[0]
            pos = load(n).get("positions") or {}
            air = load(root + fid + "/airspace.json").get("airspace") or {}
            own_name = root + fid + "/ownership/default.json"
            own = load(own_name).get("airspace", {}) if own_name in names else {}
            yield fid, pos, [(k, v, own.get(k) or v.get("owner") or []) for k, v in air.items()]


# --- Navigraph (Little Navmap) ----------------------------------------------------------------------------

class Navigraph:
    """Real-world area control frequency at a spot: a centre sector of the Navigraph data, else its FIR/UIR."""

    def __init__(self) -> None:
        self.rows = []
        try:
            from atc import navdb

            path = navdb.navigraph_path()
            if path is None:
                print("  (no Little Navmap Navigraph db: no real-world fallback frequencies)")
                return
            import sqlite3

            con = sqlite3.connect(str(path))
            for typ, name, cname, khz, lo, hi, la1, la2, lo1, lo2, geom in con.execute(
                    "select type, name, com_name, com_frequency, min_altitude, max_altitude, min_laty, max_laty, "
                    "min_lonx, max_lonx, geometry from boundary where type in ('C', 'FIR', 'UIR') "
                    "and com_frequency is not null and geometry is not null"):
                ring = unwrap([(la, lo_) for lo_, la in navdb._ring(geom)])
                self.rows.append((typ, name, cname, khz / 1000.0, lo or 0, hi or 100000, ring, bbox(ring)))
            self.rows.sort(key=lambda r: ({"C": 0, "FIR": 1, "UIR": 2}[r[0]], ring_area(r[6])))
        except Exception as exc:  # noqa: BLE001
            print(f"  (Navigraph db not readable: {exc})")

    def at(self, lat: float, lon: float, alt_ft: float = 20000.0):
        for typ, name, cname, mhz, lo, hi, ring, bb in self.rows:
            if lo <= alt_ft <= hi and inside_any(lat, lon, ring, bb):
                return mhz, name, cname
        return None

    def in_ring(self, ring, alt_ft: float, hint: str = ""):
        """The Navigraph sector for a whole polygon: tried at points across it; one whose name has `hint` ("LIMA")
        wins, else the most common (a vertex average can sit in the neighbouring FIR)."""
        hits = [h for y, x in [label_point(ring)] + interior_points(ring) if (h := self.at(y, x, alt_ft))]
        if not hits:
            return None
        hint = hint.upper()
        named = [h for h in hits if hint and hint in h[1].upper()]
        if named:
            return Counter(named).most_common(1)[0][0]
        return Counter(hits).most_common(1)[0][0]


# --- build ------------------------------------------------------------------------------------------------

def load_extra() -> dict:
    if not EXTRA.exists():
        return {}
    import yaml

    return yaml.safe_load(EXTRA.read_text(encoding="utf-8")) or {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    extra = load_extra()
    pos_extra = {str(k): v for k, v in (extra.get("positions") or {}).items()}
    fir_extra = {str(k).upper(): v for k, v in (extra.get("firs") or {}).items()}

    print("sources:")
    vg = fetch(VATGLASSES_ZIP, "vatglasses.zip", args.offline)
    dat = fetch(VATSPY_DAT, "VATSpy.dat", args.offline)
    geo = fetch(VATSPY_GEOJSON, "Boundaries.geojson", args.offline)
    countries = Countries()
    nav = Navigraph()

    positions: dict[str, dict] = {}
    volumes: list[dict] = []
    raw_pos: dict[str, dict] = {}
    entries = []
    for fid, pos, air in vatglasses(vg):
        for k, v in pos.items():
            raw_pos[f"{fid}/{k}"] = v
        for vid, vol, owners in air:
            entries.append((fid, vid, vol, [o if "/" in o else f"{fid}/{o}" for o in owners]))

    vnas_sites: dict[str, list] = {}  # nodata ARTCC position id -> [(lat, lon, pid)]

    def us_sites(pid: str, v: dict) -> list | None:
        """vNAS starred sectors of an ARTCC VATGlasses has as an outline only (pre 'LAX' -> ZLA ...)."""
        if pid in vnas_sites:
            return vnas_sites[pid]
        icao = pid.split("/")[-1].split("-")[0]  # nodata/KZLA -> KZLA; PAZA-D -> PAZA
        artcc = {"PAZA": "ZAN"}.get(icao, icao[1:] if icao.startswith("KZ") else None)
        sites = None
        if artcc:
            try:
                d = json.loads(fetch(VNAS.format(artcc), f"vnas_{artcc}.json", args.offline).read_text("utf-8"))
                tx = {t["id"]: (t["location"]["lat"], t["location"]["lon"]) for t in d.get("transceivers", [])}
                sites = []
                for p in d["facility"].get("positions", []):
                    if not p.get("starred") or not str(p.get("callsign", "")).endswith("_CTR"):
                        continue
                    sp = f"vnas/{artcc}/{p['callsign']}"
                    positions[sp] = {"cs": spoken(p.get("radioName") or v.get("callsign", "")),
                                     "mhz": round(p["frequency"] / 1e6, 3), "type": "CTR", "fir": icao,
                                     "cc": "US", "src": "vNAS"}
                    n = len(p.get("transceiverIds") or [])
                    for t in p.get("transceiverIds") or []:
                        if t in tx:
                            sites.append((round(tx[t][0], 3), round(tx[t][1], 3), sp, n))
                # a radio site shared by several sectors belongs to the one using the fewest sites (the split)
                best: dict[tuple, tuple] = {}
                for la, lo, sp, n in sites:
                    if (la, lo) not in best or n < best[(la, lo)][1]:
                        best[(la, lo)] = (sp, n)
                sites = [[la, lo, sp] for (la, lo), (sp, _) in best.items()] or None
            except Exception as exc:  # noqa: BLE001
                print(f"  (vNAS {artcc}: {exc})")
                sites = None
        vnas_sites[pid] = sites
        return sites

    def resolve(pid: str, ring) -> str | None:
        """An owner -> an area control position with a frequency (added to `positions`), else None."""
        v = raw_pos.get(pid)
        if v is None or v.get("type") not in AREA_TYPES or pid.split("/")[0] in UMBRELLA_FILES:
            return None
        if pid in positions:
            return pid
        cs = spoken(v.get("callsign") or "")
        mhz = float(v["frequency"]) if v.get("frequency") else None
        src = "VATGlasses"
        ex = pos_extra.get(pid)
        if ex:
            mhz = float(ex.get("mhz", mhz or 0)) or mhz
            cs = ex.get("callsign", cs)
            src = ex.get("source", "hand")
        if mhz is None:
            return None
        la, lo = label_point(ring)
        fid = pid.split("/")[0]
        pres = v.get("pre") or []
        four = [x for x in pres if len(x) >= 4 and x[:4].isalpha()]
        cc = countries.of(four, la, lo) if four else "US" if fid in US_FILES else countries.of(pres, la, lo)
        positions[pid] = {"cs": cs, "mhz": round(mhz, 3), "type": v.get("type"), "fir": (pres or [""])[0],
                          "cc": cc, "src": src}
        if ex and ex.get("note"):
            positions[pid]["note"] = ex["note"]
        return pid

    def navigraph_position(pid: str, ring, alt_ft: float) -> str | None:
        key = f"{pid}@{round(alt_ft, -4):.0f}"
        if key in positions:
            return key
        v = raw_pos[pid]
        la, lo = label_point(ring)
        hint = spoken(v.get("callsign") or "").split(" ")[0] if v.get("callsign") else ""
        hit = nav.in_ring(ring, alt_ft, hint)
        if hit is None:
            return None
        positions[key] = {"cs": spoken(v.get("callsign") or ""), "mhz": round(hit[0], 3), "type": "CTR",
                          "fir": (v.get("pre") or [""])[0], "cc": countries.of(v.get("pre") or [], la, lo),
                          "src": f"Navigraph AIRAC 1801 ({hit[1]}{', ' + hit[2] if hit[2] else ''})"}
        return key

    dropped = Counter()
    for fid, vid, vol, owners in entries:
        for sec in vol.get("sectors") or []:
            pts = sec.get("points") or []
            if len(pts) < 3:
                continue
            ring = [(dms(a, 2), dms(b, 3)) for a, b in pts]
            ring = unwrap(ring)
            lo_ft = int(sec.get("min") or 0) * 100
            hi_ft = int(sec["max"]) * 100 if sec.get("max") is not None else 99900
            pid = next((p for o in owners if (p := resolve(o, ring))), None)
            sites = None
            if pid is None:  # an outline whose controller has no frequency in VATGlasses: vNAS (US) or nothing
                first = next((o for o in owners if raw_pos.get(o, {}).get("type") in AREA_TYPES
                              and o.split("/")[0] not in UMBRELLA_FILES), None)
                if first is not None and first.startswith("nodata/"):
                    sites = us_sites(first, raw_pos[first])
                if not sites and first is not None:  # its real-world frequency (Navigraph) at that spot
                    pid = navigraph_position(first, ring, (lo_ft + min(hi_ft, 45000)) / 2)
                if not sites and pid is None:
                    if first:
                        dropped[first] += 1
                    continue
            volumes.append({"pid": pid, "lo": lo_ft, "hi": hi_ft, "ring": ring, "sites": sites,
                            "area": ring_area(ring), "bb": bbox(ring)})

    # VATSpy FIRs still uncovered: hand frequency, else the Navigraph one at that spot
    fir_rows, suffix = {}, {}
    sec = None
    for line in dat.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            sec = line
            continue
        p = line.split("|")
        if sec == "[Countries]" and len(p) >= 3:
            suffix[p[1]] = p[2]
        elif sec == "[FIRs]" and len(p) >= 4:
            fir_rows.setdefault(p[3] or p[0], p)
    feats = json.loads(geo.read_text(encoding="utf-8")).get("features", [])
    covered_by_vg = list(volumes)
    added_fir = []
    for f in feats:
        props = f.get("properties") or {}
        fid = str(props.get("id", ""))
        geom = f.get("geometry") or {}
        polys = geom.get("coordinates") or []
        if geom.get("type") == "Polygon":
            polys = [polys]
        rings = [unwrap([(float(y), float(x)) for x, y in poly[0]]) for poly in polys if poly and poly[0]]
        if not rings:
            continue
        big = max(rings, key=ring_area)
        la, lo = label_point(big)
        samples = [(la, lo)] + interior_points(big)
        cands = [v for v in covered_by_vg if v["bb"][0] <= max(p[0] for p in big) and v["bb"][1] >= min(p[0] for p in big)]
        if all(any(inside_any(y, x, v["ring"], v["bb"]) for v in cands) for y, x in samples):
            continue  # VATSIM sectors cover it all
        row = fir_rows.get(fid)
        name = ascii_name(row[1]) if row else fid
        icao = (row[0] if row else fid).upper()
        ex = fir_extra.get(icao) or fir_extra.get(fid.upper())
        sfx = suffix.get(icao[:2], "")
        sfx = sfx if sfx.lower() in ENGLISH_SUFFIX else ("Center" if icao[:1] in "KP" else "Control")
        base = spoken(name)
        has_suffix = base.split()[-1].lower() in ENGLISH_SUFFIX if base else False
        cs, mhz, src, note = (base if has_suffix else f"{base} {sfx}").strip(), None, None, None
        if ex:
            cs, mhz, src, note = ex.get("callsign", cs), ex.get("mhz"), ex.get("source", "hand"), ex.get("note")
        if mhz is None:
            hit = nav.in_ring(big, 20000.0, base.split(" ")[0] if base else "")
            if hit:
                mhz, src = hit[0], f"Navigraph AIRAC 1801 ({hit[1]}{', ' + hit[2] if hit[2] else ''})"
        if mhz is None:
            dropped[f"vatspy/{fid}"] += 1
            continue
        pid = f"vatspy/{fid}"
        positions[pid] = {"cs": cs, "mhz": round(float(mhz), 3), "type": "CTR", "fir": icao,
                          "cc": countries.of([icao], la, lo), "src": src}
        if note:
            positions[pid]["note"] = note
        for r in rings:
            volumes.append({"pid": pid, "lo": 0, "hi": 99900, "ring": r, "sites": None, "area": ring_area(r) + 1e6,
                            "bb": bbox(r)})
        added_fir.append(pid)

    volumes.sort(key=lambda v: v["area"])  # the smallest (most specific) sector containing a point wins
    out = {
        "version": 1,
        "built": time.strftime("%Y-%m-%d"),
        "sources": ["data/centers_extra.yaml (hand)", "VATGlasses data (CC BY-NC-SA 4.0)", "vNAS data API",
                    "VATSpy data project (CC BY-SA 4.0)", "Navigraph AIRAC 1801 via Little Navmap"],
        "positions": positions,
        "volumes": [[v["pid"], v["lo"], v["hi"], [c for p in v["ring"] for c in (round(p[0], 3), round(p[1], 3))],
                     v["sites"]] for v in volumes],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    srcs = Counter(p["src"].split(" (")[0] for p in positions.values())
    print(f"{OUT}: {len(positions)} positions, {len(volumes)} sectors ({OUT.stat().st_size / 1e6:.1f} MB); "
          f"by source {dict(srcs)}; {len(added_fir)} FIRs from VATSpy")
    if args.report:
        print("left without a frequency (add them to data/centers_extra.yaml):")
        for k, n in sorted(dropped.items()):
            v = raw_pos.get(k, {})
            print(f"  {k:28s} {ascii_name(v.get('callsign') or ''):35s} sectors {n}")


if __name__ == "__main__":
    main()
