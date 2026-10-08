"""Who departs from an airport: FS Traffic's real schedules on Valen's PC (`Data/Schedules/<ICAO>.ini`), else a short
list per country. Phase 1 picks a random flight of today's list; phase 4 (docs/OWN_TRAFFIC_PLAN.md) times them.

FS Traffic line: `0=DT:0405,ICAO:SBBR,CA:ARG,AC:738,FLTNO1216,CS:ARGENTINA,DAYS:25*` under `[<weekday>]`
(VERIFY: weekday 1 = Monday; DT local or UTC). AC is the IATA type: 738 -> B738 via Data/DBs/aircraftCodesDB.csv.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
import re
from dataclasses import dataclass
from pathlib import Path

from atc.own.catalog import FST_DIR

_LINE = re.compile(r"DT:(\d{3,4}),ICAO:(\w{3,4}),CA:(\w{2,3}),AC:(\w{2,4}),FLTNO(\d{1,4})")
FALLBACK = {  # country -> (airline ICAO, type ICAO) when FS Traffic isn't there
    "AR": [("ARG", "B738"), ("ARG", "E190"), ("FBZ", "B738"), ("JES", "A320"), ("ARG", "B38M")],
    "US": [("UAL", "B738"), ("DAL", "A321"), ("AAL", "B738"), ("SWA", "B737"), ("ASA", "B739")],
}


@dataclass
class Departure:
    airline: str  # ICAO
    number: str
    type_icao: str
    destination: str | None = None

    @property
    def callsign(self) -> str:
        return f"{self.airline}{self.number}"


def _iata_types(data_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    db = data_dir / "DBs" / "aircraftCodesDB.csv"
    if db.is_file():
        with db.open(encoding="utf-8", errors="ignore", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("Code") and row.get("Ident"):
                    out.setdefault(row["Code"].strip().upper(), row["Ident"].strip().upper())
    return out


def todays(icao: str, community: Path | None, weekday: int | None = None) -> list[Departure]:
    """Today's departures from `icao` in FS Traffic's schedule ([] without FS Traffic or that airport)."""
    if community is None:
        return []
    data = community / FST_DIR / "Data"
    ini = data / "Schedules" / f"{icao.upper()}.ini"
    if not ini.is_file():
        return []
    day = str(weekday or dt.date.today().isoweekday())
    types = _iata_types(data)
    out, section = [], None
    for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        m = _LINE.search(line)
        if section == day and m and types.get(m.group(4).upper()):
            out.append(Departure(m.group(3).upper(), m.group(5), types[m.group(4).upper()], m.group(2).upper()))
    return out


def pick(icao: str, country: str, community: Path | None, rng: random.Random,
         taken: set[str] = frozenset()) -> Departure:
    """A departure for now: from today's schedule if there is one, else a made-up flight of a local airline."""
    flights = [f for f in _todays_cached(icao, community) if f.callsign not in taken]
    if flights:
        return rng.choice(flights)
    airline, typ = rng.choice(FALLBACK.get(country, FALLBACK["US"]))
    return Departure(airline, str(rng.randint(1000, 4999)), typ)


_CACHE: dict[tuple[str, str], list[Departure]] = {}


def _todays_cached(icao: str, community: Path | None) -> list[Departure]:
    key = (icao.upper(), dt.date.today().isoformat())
    if key not in _CACHE:
        _CACHE[key] = todays(icao, community)
    return _CACHE[key]


# --- how busy an airport is -------------------------------------------------------------------------------
# Movements per hour (departures + arrivals) of our own traffic, from FS Traffic's schedule for today, boosted at
# small airports so something happens (Valen: Corrientes "occasional, every 10 min or so", JFK / Ezeiza "a lot").
MIN_RATE = 6.0  # an airport with scheduled flights: at least one movement every 10 minutes
MAX_RATE = 20.0  # one runway for everything (Valen at SABE: 22 an hour made the queue grow)
MAX_RATE_SPLIT = 36.0  # separate arrival and departure runways (YAML runway_configs: KSFO, JFK)
GA_RATE = {True: 3.0, False: 1.5}  # no airline schedule: GA only (towered field / not)
GA_SHARE = 0.1  # at airline airports, this share of the movements is GA


HUB_RUNWAY_FT = 9800.0  # a runway this long (3000 m): an international hub, twice as busy (Ezeiza)


def movements_per_hour(airport, community: Path | None, factor: float = 1.0) -> float:
    """The airport YAML's `traffic_per_hour` if set, else from today's schedule (GA only without one)."""
    if airport.traffic_per_hour is not None:
        return airport.traffic_per_hour * factor
    deps = len(_todays_cached(airport.icao, community))
    if deps == 0:
        return GA_RATE[bool(airport.towered)] * factor
    base = deps * 2.0 / 16.0  # over a 16-hour day
    boost = 3.0 if deps < 20 else 1.5 if deps < 150 else 1.2
    if max((r.length_ft or 0.0) for r in airport.runways) >= HUB_RUNWAY_FT:
        boost *= 2.0
    cap = MAX_RATE_SPLIT if airport.runway_configs else MAX_RATE
    return max(MIN_RATE, min(cap, base * boost)) * factor


def ga_flight(country: str, rng: random.Random) -> Departure:
    """A GA aircraft with a registration callsign: LV-ABC (Argentina), N123AB (US), else LV."""
    from atc.own.motion import GA_TYPES

    typ = rng.choice(GA_TYPES)
    letters = "".join(rng.choice("ABCDEFGHIJKLMNOPRSTUVWXYZ") for _ in range(3))
    if country == "US":
        return Departure("", f"N{rng.randint(100, 999)}{letters[:2]}", typ)
    return Departure("", f"LV{letters}", typ)
