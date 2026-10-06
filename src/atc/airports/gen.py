"""Airport file generator: ICAO -> airports/<ICAO>.yaml, from OurAirports CSVs.

Usage:
    atc-gen --download                  # fetch the three CSVs into ./data (do this on your PC)
    atc-gen KXXX [KYYY ...]             # write airports/KXXX.yaml
    atc-gen SARC --data-dir tests/fixtures

Column names and frequency types were checked against the OurAirports data dictionary
(ourairports.com/help/data-dictionary.html). They have NOT been run against the real CSVs yet
(the build sandbox could not download them); tests use hand-made fixtures with the same columns.
"""

from __future__ import annotations

import argparse
import csv
import sys
import urllib.request
from pathlib import Path

from atc.airports.schema import save_airport
from atc.models import Airport, Frequency, Runway

BASE_URL = "https://davidmegginson.github.io/ourairports-data/"
FILES = ("airports.csv", "runways.csv", "airport-frequencies.csv")

# OurAirports frequency types (from its data dictionary): TWR, GND, RMP, ATIS, ARR, DEP, ATF,
# CTAF, UNICOM, RCO, RDO. facility.py decides which of them can reply.


AIRLINES_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"


def download(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        print(f"downloading {name} ...")
        urllib.request.urlretrieve(BASE_URL + name, data_dir / name)
    print("downloading airlines.dat (ICAO designator -> radio telephony) ...")
    urllib.request.urlretrieve(AIRLINES_URL, data_dir / "airlines.dat")


def _rows(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def _float(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_airport(icao: str, data_dir: Path) -> Airport:
    icao = icao.upper()
    row = next(
        (r for r in _rows(data_dir / "airports.csv") if r["ident"] == icao or r.get("icao_code") == icao),
        None,
    )
    if row is None:
        raise KeyError(f"{icao} not found in airports.csv")

    ident = row["ident"]
    country = row.get("iso_country", "")
    is_us = country == "US"

    runways: list[Runway] = []
    for r in _rows(data_dir / "runways.csv"):
        if r["airport_ident"] != ident or r.get("closed") == "1":
            continue
        for end in ("le", "he"):
            end_ident = r.get(f"{end}_ident")
            if not end_ident:
                continue
            runways.append(
                Runway(
                    ident=end_ident,
                    heading_deg=_float(r.get(f"{end}_heading_degT", "")),
                    length_ft=_float(r.get("length_ft", "")),
                    surface=r.get("surface") or None,
                    # Safe default only for the US. Everywhere else: left empty on purpose.
                    pattern_alt_agl_ft=1000 if is_us else None,
                    pattern_direction="left" if is_us else None,
                    lat=_float(r.get(f"{end}_latitude_deg", "")),
                    lon=_float(r.get(f"{end}_longitude_deg", "")),
                )
            )

    freqs: list[Frequency] = []
    for f in _rows(data_dir / "airport-frequencies.csv"):
        if f["airport_ident"] != ident:
            continue
        mhz = _float(f.get("frequency_mhz", ""))
        if mhz is None:
            continue
        freqs.append(Frequency(kind=f["type"].strip().upper(), mhz=mhz, description=f.get("description", "")))

    towered = any(f.kind == "TWR" for f in freqs)  # heuristic, check by hand

    notes = []
    if not is_us:
        notes.append("Non-US airport: pattern altitude/direction left empty, fill from the local charts.")
    if not towered:
        notes.append("No tower frequency found: treated as non-towered (CTAF/UNICOM self-announce).")

    return Airport(
        icao=icao,
        name=row["name"],
        lat=float(row["latitude_deg"]),
        lon=float(row["longitude_deg"]),
        elevation_ft=_float(row.get("elevation_ft", "")) or 0.0,
        country=country,
        towered=towered,
        language="en" if is_us else "en",  # TODO: es for SABE/SARC once Spanish voice exists
        runways=runways,
        frequencies=freqs,
        notes=notes,
        needs_review=True,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atc-gen")
    p.add_argument("icao", nargs="*")
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--out", type=Path, default=Path("airports"))
    p.add_argument("--download", action="store_true", help="fetch the OurAirports CSVs first")
    p.add_argument("--force", action="store_true", help="overwrite existing files (hand edits are lost!)")
    args = p.parse_args(argv)

    if args.download:
        download(args.data_dir)
    status = 0
    for code in args.icao:
        target = args.out / f"{code.upper()}.yaml"
        if target.exists() and not args.force:
            print(f"{target} exists, skipping (hand edits are safe; use --force to regenerate)")
            continue
        try:
            save_airport(build_airport(code, args.data_dir), target)
            print(f"wrote {target}")
        except (KeyError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
