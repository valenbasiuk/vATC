"""Every place a flight talks to: the airports in the plan (origin, destination, alternate, --airport) plus
area control files (airspace/*.yaml). One run covers the whole flight; the tuned frequency decides who answers."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from atc.airports.schema import load_airport, save_airport
from atc.facility import resolve_facility
from atc.geo import distance_nm
from atc.models import Airport, Facility, OwnState
from atc.taxi import TaxiNetwork, load_network


@dataclass
class World:
    airports: list[Airport]  # first = the --airport one
    airspaces: list[Airport] = field(default_factory=list)  # area control, frequencies of kind CTR
    taxi: dict[str, TaxiNetwork | None] = field(default_factory=dict)

    def get(self, icao: str | None) -> Airport | None:
        return next((a for a in self.airports if icao and a.icao == icao.upper()), None)

    def pick(self, own: OwnState) -> tuple[Airport, Facility] | None:
        """Who owns the tuned frequency. Two airports on one frequency: the nearer one."""
        matches = [(a, f) for a in self.airports + self.airspaces if (f := resolve_facility(a, own.com1_mhz))]
        if not matches:
            return None
        return min(matches, key=lambda m: distance_nm(own.lat, own.lon, m[0].lat, m[0].lon))

    def nearest(self, own: OwnState) -> Airport:
        return min(self.airports, key=lambda a: distance_nm(own.lat, own.lon, a.lat, a.lon))

    def control(self) -> tuple[Airport, Facility] | None:
        """First area control frequency on file (handoffs after departure go there)."""
        for a in self.airspaces:
            for f in a.frequencies:
                if f.kind == "CTR":
                    return a, resolve_facility(a, f.mhz)
        return None


def load_world(primary: str, airports_dir: Path, plan=None, airspace_dir: Path | None = None,
               data_dir: Path = Path("data")) -> World:
    codes = [primary.upper()]
    if plan is not None:
        codes += [c for c in (plan.origin, plan.destination, plan.alternate) if c]
    airports: list[Airport] = []
    for code in dict.fromkeys(codes):  # unique, keep order
        path = Path(airports_dir) / f"{code}.yaml"
        if not path.exists() and (Path(data_dir) / "airports.csv").exists():
            try:  # first flight to a new airport: generate it (never overwrites an existing file)
                from atc.airports.gen import build_airport

                save_airport(build_airport(code, Path(data_dir)), path)
                print(f"generated {path} (check it: needs_review)")
            except Exception as exc:  # noqa: BLE001
                print(f"[no airport file for {code}: {exc}]", file=sys.stderr)
        if path.exists():
            airports.append(load_airport(path))
        elif code == primary.upper():
            raise FileNotFoundError(path)
    spaces = []
    adir = airspace_dir or Path(airports_dir).parent / "airspace"
    if adir.exists():
        for f in sorted(adir.glob("*.yaml")):
            a = load_airport(f)
            if a.frequencies:
                spaces.append(a)
    return World(airports, spaces, {a.icao: load_network(airports_dir, a.icao) for a in airports})
