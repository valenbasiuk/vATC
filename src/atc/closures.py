"""Closures by hand (improvement plan G4, a NOTAM-lite): the airport YAML's `closed` list.

    closed:
    - runway 13/31             # both directions
    - runway 13 until 1800     # UTC (the sim's clock), then open again
    - taxiway E
    - twy B1 until 2330

A closed runway is never the runway in use (runway.py) and a request for it is "unable, runway one three closed";
closed taxiways are left out of every taxi route (taxi.route_path reads TaxiNetwork.closed); the ATIS reads them out
("Runway one three three one closed. Taxiway Echo closed."). Real NOTAM feeds only exist for the US (FAA), so these
are typed in by hand.
"""

from __future__ import annotations

import re
import time

from atc import phrase
from atc.models import Airport

ZULU: list[float | None] = [None]  # the sim's clock (seconds since 00:00 UTC), kept by main's watcher

_ITEM = re.compile(r"^(runway|rwy|taxiway|twy)\s+([a-z0-9/ ]+?)(?:\s+until\s+(\d{4})z?)?\s*$", re.I)


def _now_min() -> int:
    z = ZULU[0] if ZULU[0] is not None else time.time() % 86400
    return int(z) // 60 % 1440


def _parse(airport: Airport) -> tuple[set[str], set[str]]:
    runways: set[str] = set()
    taxiways: set[str] = set()
    for raw in airport.closed:
        m = _ITEM.match(str(raw).split("#")[0].strip())
        if not m:
            continue
        until = m.group(3)
        if until and _now_min() >= int(until[:2]) * 60 + int(until[2:]):
            continue  # reopened
        what = m.group(2).upper().replace(" ", "")
        if m.group(1).lower() in ("runway", "rwy"):
            runways.update(i.lstrip("0") for i in what.split("/") if i)
        else:
            taxiways.add(what)
    return runways, taxiways


def runway_closed(airport: Airport, ident: str) -> bool:
    return bool(airport.closed) and ident.upper().lstrip("0") in _parse(airport)[0]


def closed_taxiways(airport: Airport) -> set[str]:
    return _parse(airport)[1] if airport.closed else set()


def atis_lines(airport: Airport, faa: bool) -> list[str]:
    """'Runway one three three one closed.' / 'Taxiway Echo closed.' for the ATIS."""
    if not airport.closed:
        return []
    from atc.taxi import spoken_route

    runways, taxiways = _parse(airport)
    out = []
    done = set()
    for r in airport.runways:  # say a closed strip once, both ends: "runway one three three one"
        ident = r.ident.upper().lstrip("0")
        if ident not in runways or ident in done:
            continue
        twin = next((o for o in airport.runways if o is not r and o.ident.upper().lstrip("0") in runways
                     and o.ident.upper().lstrip("0") not in done and _reciprocal(r.ident, o.ident)), None)
        done.add(ident)
        words = phrase.runway(r.ident, faa)
        if twin is not None:
            done.add(twin.ident.upper().lstrip("0"))
            words += " " + phrase.runway(twin.ident, faa)
        out.append(f"Runway {words} closed.")
    for t in sorted(taxiways):
        out.append(f"Taxiway {spoken_route([t])} closed.")
    return out


def _reciprocal(a: str, b: str) -> bool:
    from atc.sequence import same_strip

    return same_strip(a, b) and a.upper() != b.upper()


def refresh(world) -> None:
    """Keep each taxi map's closed taxiways current (a closure 'until 1800' ends by itself)."""
    for a in world.airports:
        net = world.taxi.get(a.icao)
        if net is not None:
            net.closed = closed_taxiways(a)
