"""Download an airport's taxiways, holding points and stands from OpenStreetMap (Overpass API).

    python tools/fetch_osm_taxi.py SABE            # -> airports/osm/SABE.json
    python tools/fetch_osm_taxi.py SARC --radius-nm 2

Ground then names real taxiways (atc.taxi builds the route in code). The file is raw Overpass JSON, so
it can be checked against the AD chart; delete it to go back to "taxi without naming taxiways".
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atc.airports.schema import load_airport  # noqa: E402

URLS = [  # public Overpass servers, tried in order (the main one often answers 504 when busy)
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def query(lat: float, lon: float, radius_nm: float) -> str:
    dlat = radius_nm / 60.0
    dlon = radius_nm / (60.0 * math.cos(math.radians(lat)))
    bb = f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon}"
    return (
        "[out:json][timeout:60];("
        f'way["aeroway"~"^(taxiway|taxilane|runway|parking_position)$"]({bb});'
        f'node["aeroway"~"^(holding_position|parking_position)$"]({bb});'
        ");out body;>;out skel qt;"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("icao")
    p.add_argument("--airports-dir", type=Path, default=Path("airports"))
    p.add_argument("--radius-nm", type=float, default=2.5)
    args = p.parse_args(argv)
    apt = load_airport(args.airports_dir / f"{args.icao.upper()}.yaml")
    body = urllib.parse.urlencode({"data": query(apt.lat, apt.lon, args.radius_nm)}).encode()
    data = None
    for url in URLS:
        req = urllib.request.Request(url, data=body, headers={"User-Agent": "atc-ia/0.1 (personal flight-sim ATC)",
                                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.load(resp)
            break
        except Exception as exc:  # noqa: BLE001 - busy server, try the next mirror
            print(f"{url}: {exc}", file=sys.stderr)
    if data is None:
        print("all Overpass servers failed; try again in a few minutes", file=sys.stderr)
        return 1
    out = args.airports_dir / "osm" / f"{apt.icao}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data), encoding="utf-8")
    ways = [e for e in data["elements"] if e["type"] == "way"]
    named = sorted({w["tags"].get("ref") or w["tags"].get("name", "") for w in ways
                    if w.get("tags", {}).get("aeroway") == "taxiway"} - {""})
    holds = sum(1 for e in data["elements"] if e.get("tags", {}).get("aeroway") == "holding_position")
    stands = sum(1 for e in data["elements"] if e.get("tags", {}).get("aeroway") == "parking_position")
    print(f"wrote {out}: {len(ways)} ways, taxiways named {named}, {holds} holding points, {stands} stands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
