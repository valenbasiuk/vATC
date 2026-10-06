"""Every place a flight talks to: the airports in the plan (origin, destination, alternate, --airport) plus
area control files (airspace/*.yaml). One run covers the whole flight; the tuned frequency decides who answers.

Airports load themselves: spawn somewhere that isn't in the plan (or start without --airport) and the airport
you are at is found from the sim's scenery (Little Navmap db) or OurAirports, its YAML is written the first time
(never over an existing file), and its frequencies, taxi map and magvar come from the sim. Tuning a frequency
that no loaded airport has looks for a nearby airport that has it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from atc.airports.schema import load_airport, save_airport
from atc.facility import resolve_facility
from atc.geo import distance_nm
from atc.models import Airport, Facility, OwnState
from atc.taxi import TaxiNetwork, load_network

AIRSPACE_REACH_NM = 700.0  # an area control file only answers within this of its reference point
SPAWN_NM = 3.0  # on the ground farther than this from every loaded airport: find the one we're at
TUNED_NM = 25.0  # a tuned frequency no loaded airport has: look for an airport this close that has it
# Writing new airport files while flying. Tests switch it off (conftest) so they never add files to airports/.
AUTO_GENERATE = True


@dataclass
class World:
    airports: list[Airport]  # first = the --airport one
    airspaces: list[Airport] = field(default_factory=list)  # area control, frequencies of kind CTR
    taxi: dict[str, TaxiNetwork | None] = field(default_factory=dict)
    airports_dir: Path | None = None  # set by load_world: lets the world load the airport you are at
    data_dir: Path = Path("data")
    use_navdb: bool = True
    _tried: set = field(default_factory=set)

    def get(self, icao: str | None) -> Airport | None:
        return next((a for a in self.airports if icao and a.icao == icao.upper()), None)

    def _airspaces_near(self, own: OwnState) -> list[Airport]:
        return [s for s in self.airspaces if distance_nm(own.lat, own.lon, s.lat, s.lon) <= AIRSPACE_REACH_NM]

    def _matches(self, own: OwnState) -> list[tuple[Airport, Facility]]:
        return [(a, f) for a in self.airports + self._airspaces_near(own)
                if (f := resolve_facility(a, own.com1_mhz))]

    def pick(self, own: OwnState) -> tuple[Airport, Facility] | None:
        """Who owns the tuned frequency. Two airports on one frequency: the nearer one."""
        if own.on_ground and self.airports_dir is not None and not self._loaded_within(own, SPAWN_NM):
            self.discover(own)  # spawned at an airport that isn't loaded yet
        matches = self._matches(own)
        if not matches and self.discover(own, mhz=own.com1_mhz):
            matches = self._matches(own)
        if not matches:
            return None
        return min(matches, key=lambda m: distance_nm(own.lat, own.lon, m[0].lat, m[0].lon))

    def _loaded_within(self, own: OwnState, nm: float) -> bool:
        return any(distance_nm(own.lat, own.lon, a.lat, a.lon) <= nm for a in self.airports)

    # --- finding and loading the airport you are at ---------------------------------------------------

    def discover(self, own: OwnState, mhz: float | None = None) -> Airport | None:
        """Load the airport the aircraft is at (on the ground), or the nearby one that has frequency `mhz`.
        Each place is looked at once (cells of ~6 NM), so the watcher can call this every second."""
        if self.airports_dir is None or (not own.on_ground and own.alt_agl_ft > 5000):
            return None
        cell = (round(own.lat, 1), round(own.lon, 1), None if mhz is None else round(mhz, 2))
        if cell in self._tried:
            return None
        self._tried.add(cell)
        icao = self._find(own, mhz)
        if icao is None or self.get(icao) is not None:
            return None
        apt = self.add_airport(icao)
        if apt is not None:
            print(f"[now at {apt.icao} {apt.name}]" if mhz is None else f"[{apt.icao} {apt.name} loaded: it has "
                                                                          f"{mhz:.3f}]")
        return apt

    def _find(self, own: OwnState, mhz: float | None) -> str | None:
        have = {a.icao for a in self.airports}
        if self.use_navdb:
            from atc import navdb

            if mhz is not None:
                hit = navdb.airport_on_frequency(own.lat, own.lon, mhz, TUNED_NM)
                if hit:
                    return hit
            else:
                near = navdb.airports_near(own.lat, own.lon, SPAWN_NM)
                if near:  # the nearest one is where we are (if it is loaded already: nothing to do)
                    return near[0][1] if near[0][1] not in have else None
        if mhz is not None:  # without the sim's data: only airport files that have the frequency
            best = None
            for apt in self._yaml_airports():
                d = distance_nm(own.lat, own.lon, apt.lat, apt.lon)
                if apt.icao not in have and d <= TUNED_NM and resolve_facility(apt, mhz) and (best is None or d < best[0]):
                    best = (d, apt.icao)
            return best[1] if best else None
        for apt in self._yaml_airports():
            if apt.icao not in have and distance_nm(own.lat, own.lon, apt.lat, apt.lon) <= SPAWN_NM:
                return apt.icao
        hit = self._nearest_in_csv(own, SPAWN_NM, have) if AUTO_GENERATE else None
        return hit[1] if hit else None

    def _yaml_airports(self):
        for path in Path(self.airports_dir).glob("*.yaml"):
            try:
                yield load_airport(path)
            except Exception:  # noqa: BLE001 - a broken hand edit must not stop the radio
                continue

    def add_airport(self, icao: str) -> Airport | None:
        """Load airports/<ICAO>.yaml (writing it first if it doesn't exist) and add it to the world."""
        apt = ensure_airport(icao, Path(self.airports_dir), Path(self.data_dir), self.use_navdb)
        if apt is None:
            return None
        self.airports.append(apt)
        self.taxi[apt.icao] = load_network(Path(self.airports_dir), apt.icao, self.use_navdb)
        return apt

    def _nearest_in_csv(self, own: OwnState, within_nm: float, have: set):
        csv_path = Path(self.data_dir) / "airports.csv"
        if not csv_path.exists():
            return None
        import csv

        best = None
        with open(csv_path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row["type"] not in ("large_airport", "medium_airport", "small_airport"):
                    continue
                code = row["ident"].upper()
                if len(code) != 4 or not code.isalpha() or code in have:
                    continue
                try:
                    lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
                except ValueError:
                    continue
                if abs(lat - own.lat) > 0.5 or abs(lon - own.lon) > 0.7:  # cheap box before the real distance
                    continue
                d = distance_nm(own.lat, own.lon, lat, lon)
                if d <= within_nm and (best is None or d < best[0]):
                    best = (d, code)
        return best

    # --- questions the flow asks ----------------------------------------------------------------------

    def nearest(self, own: OwnState) -> Airport | None:
        return min(self.airports, key=lambda a: distance_nm(own.lat, own.lon, a.lat, a.lon), default=None)

    def stations(self) -> list[tuple[str, str]]:
        """(position group, frequency digits without trailing zeros) for every frequency on file."""
        from atc.facility import group

        out = []
        for a in self.airports + self.airspaces:
            for f in a.frequencies:
                fac = resolve_facility(a, f.mhz)
                if fac is not None and group(fac.role):
                    out.append((group(fac.role), f"{f.mhz:.3f}".replace(".", "").rstrip("0")))
        return out

    def control(self, own: OwnState | None = None, near: Airport | None = None) -> tuple[Airport, Facility] | None:
        """Area control for where the aircraft is: the first CTR frequency of the nearest airspace file in reach,
        else a CTR frequency the sim lists for the departure airport (KSFO: "Oakland Center")."""
        spaces = self.airspaces if own is None else sorted(
            self._airspaces_near(own), key=lambda s: distance_nm(own.lat, own.lon, s.lat, s.lon))
        for a in spaces:
            for f in a.frequencies:
                if f.kind == "CTR":
                    return a, resolve_facility(a, f.mhz)
        for a in ([near] if near is not None else []):
            for f in a.frequencies:
                if f.kind == "CTR":
                    return a, resolve_facility(a, f.mhz)
        return None


def ensure_airport(icao: str, airports_dir: Path, data_dir: Path = Path("data"), use_navdb: bool = True,
                   ) -> Airport | None:
    """airports/<ICAO>.yaml, loaded and filled in from the sim's data. A missing file is written first, from
    OurAirports (data/*.csv) or else from the sim's scenery. An existing file is never overwritten."""
    icao = icao.upper()
    path = Path(airports_dir) / f"{icao}.yaml"
    if not path.exists():
        if not AUTO_GENERATE:
            return None
        apt = None
        if (Path(data_dir) / "airports.csv").exists():
            try:  # first flight to a new airport: generate it
                from atc.airports.gen import build_airport

                apt = build_airport(icao, Path(data_dir))
            except Exception as exc:  # noqa: BLE001
                print(f"[{icao} not in OurAirports: {exc}]", file=sys.stderr)
        if apt is None and use_navdb:
            from atc import navdb

            apt = navdb.build_airport(icao)
        if apt is None:
            print(f"[no airport file for {icao}]", file=sys.stderr)
            return None
        save_airport(apt, path)
        print(f"generated {path} (check it: needs_review)")
    apt = load_airport(path)
    if use_navdb:
        from atc import navdb

        added = navdb.enrich(apt)
        if added:
            print(f"{apt.icao}: from the sim scenery: {', '.join(added)}")
    return apt


def load_world(primary: str | None, airports_dir: Path, plan=None, airspace_dir: Path | None = None,
               data_dir: Path = Path("data"), use_navdb: bool = True) -> World:
    """`primary` None: no --airport (main then finds the one you are at with World.discover)."""
    codes = [primary.upper()] if primary else []
    if plan is not None:
        codes += [c for c in (plan.origin, plan.destination, plan.alternate) if c]
    airports: list[Airport] = []
    for code in dict.fromkeys(codes):  # unique, keep order
        apt = ensure_airport(code, airports_dir, data_dir, use_navdb)
        if apt is not None:
            airports.append(apt)
        elif primary and code == primary.upper():
            raise FileNotFoundError(Path(airports_dir) / f"{code}.yaml")
    spaces = []
    adir = airspace_dir or Path(airports_dir).parent / "airspace"
    if adir.exists():
        for f in sorted(adir.glob("*.yaml")):
            a = load_airport(f)
            if a.frequencies:
                spaces.append(a)
    return World(airports, spaces, {a.icao: load_network(airports_dir, a.icao, use_navdb) for a in airports},
                 airports_dir=Path(airports_dir), data_dir=Path(data_dir), use_navdb=use_navdb)
