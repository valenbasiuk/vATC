"""Taxi routes computed in code from a taxiway network (roadmap item 9b).

Source today: OpenStreetMap (tools/fetch_osm_taxi.py -> airports/osm/<ICAO>.json). MSFS's own taxiway data
(SimConnect facility data, tools/probe_taxi.py) can feed the same graph later. A hand-written
`taxi_routes` entry in the airport YAML always wins over a computed route.

Ground's taxi clearance is then fixed-form and produced here, like the IFR clearance: exact and instant.
"""

from __future__ import annotations

import heapq
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from atc import phrase
from atc.geo import distance_nm
from atc.models import Airport, OwnState, Runway, Traffic
from atc.sequence import along_cross

NAME_CHANGE_PENALTY_M = 250.0  # prefer fewer taxiways in the route, like a controller would
GRAPH_WAYS = ("taxiway", "taxilane", "parking_position")
M_PER_NM = 1852.0


@dataclass
class TaxiNetwork:
    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)  # id -> (lat, lon)
    edges: dict[int, list[tuple[int, float, str | None]]] = field(default_factory=dict)  # id -> [(to, m, name)]
    holds: list[tuple[int, str]] = field(default_factory=list)  # (node id, type) runway/ILS/intermediate
    stands: dict[str, int] = field(default_factory=dict)  # stand ref -> nearest graph node
    source: str = ""  # where the map came from (shown at startup)

    def nearest(self, lat: float, lon: float, among=None) -> int | None:
        ids = among if among is not None else self.edges.keys()
        best = min(ids, key=lambda n: distance_nm(lat, lon, *self.nodes[n]), default=None)
        return best


def _designator(s: str) -> str | None:
    """A taxiway designator is a letter, optionally with letters/digits after it: 'A', 'A1', 'AB'. Long names are
    descriptions and bare numbers are stand lead-in lines (OSM tags one at SABE as taxiway '1'), not taxiways."""
    s = (s or "").strip().upper()
    return s if 1 <= len(s) <= 3 and s[0].isalpha() and s.isalnum() else None


def _name(tags: dict) -> str | None:
    return _designator(tags.get("ref", "")) or _designator(tags.get("name", ""))


def load_osm(path: Path) -> TaxiNetwork:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pos = {e["id"]: (e["lat"], e["lon"]) for e in data["elements"] if e["type"] == "node" and "lat" in e}
    net = TaxiNetwork()
    for e in data["elements"]:
        tags = e.get("tags", {})
        if e["type"] != "way" or tags.get("aeroway") not in GRAPH_WAYS:
            continue
        name = _name(tags) if tags.get("aeroway") == "taxiway" else None
        ids = [n for n in e.get("nodes", []) if n in pos]
        for a, b in zip(ids, ids[1:]):
            m = distance_nm(*pos[a], *pos[b]) * M_PER_NM
            net.edges.setdefault(a, []).append((b, m, name))
            net.edges.setdefault(b, []).append((a, m, name))
    net.nodes = {n: pos[n] for n in net.edges}
    if not net.nodes:
        return net
    for e in data["elements"]:
        tags = e.get("tags", {})
        if tags.get("aeroway") == "holding_position" and e["type"] == "node" and e["id"] in pos:
            node = e["id"] if e["id"] in net.edges else net.nearest(*pos[e["id"]])
            net.holds.append((node, tags.get("holding_position:type", "runway")))
        elif tags.get("aeroway") == "parking_position" and tags.get("ref"):
            if e["type"] == "node" and e["id"] in pos:
                lat, lon = pos[e["id"]]
            elif e["type"] == "way" and e.get("nodes"):
                pts = [pos[n] for n in e["nodes"] if n in pos]
                if not pts:
                    continue
                lat, lon = pts[0]
            else:
                continue
            net.stands[str(tags["ref"]).upper()] = net.nearest(lat, lon)
    return net


def load_msfs(path: Path) -> TaxiNetwork:
    """airports/msfs/<ICAO>.json from tools/probe_taxi.py. VERIFY: point/path type codes are guessed: the most
    common point type is taken as a plain taxi point and every other type as a holding point."""
    from collections import Counter

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    net = TaxiNetwork()
    pts = data["points"]
    names = data.get("names", [])
    for p in data["paths"]:
        a, b = p["start"], p["end"]
        if not (0 <= a < len(pts) and 0 <= b < len(pts)):
            continue
        name = _designator(names[p["name_index"]]) if 0 <= p["name_index"] < len(names) else None
        m = distance_nm(pts[a]["lat"], pts[a]["lon"], pts[b]["lat"], pts[b]["lon"]) * M_PER_NM
        net.edges.setdefault(a, []).append((b, m, name))
        net.edges.setdefault(b, []).append((a, m, name))
    net.nodes = {i: (pts[i]["lat"], pts[i]["lon"]) for i in net.edges}
    common = Counter(p["type"] for p in pts).most_common(1)
    plain = common[0][0] if common else None
    net.holds = [(i, "runway") for i in net.edges if pts[i]["type"] != plain]
    for park in data.get("parkings", []):
        if net.nodes and park.get("number"):
            net.stands[str(park["number"])] = net.nearest(park["lat"], park["lon"])
    return net


def load_network(airports_dir: Path, icao: str, use_navdb: bool = True) -> TaxiNetwork | None:
    """The sim's own taxiways first (probe_taxi json, then Little Navmap's scenery database), else OpenStreetMap
    (OSM can be wrong: it had a taxiway G at Rosario), else None. `source` on the result says which."""
    path = Path(airports_dir) / "msfs" / f"{icao.upper()}.json"
    candidates = [("msfs probe", lambda: load_msfs(path) if path.exists() else None)]
    if use_navdb:
        from atc import navdb

        candidates.append(("sim scenery (Little Navmap db)", lambda: navdb.taxi_network(icao)))
    osm = Path(airports_dir) / "osm" / f"{icao.upper()}.json"
    candidates.append(("OpenStreetMap", lambda: load_osm(osm) if osm.exists() else None))
    for source, load in candidates:
        try:
            net = load()
        except (KeyError, ValueError, TypeError) as exc:
            print(f"[taxi map from {source} unreadable: {exc}]")
            continue
        if net is not None and net.edges:
            net.source = source
            return net
    return None


def route(net: TaxiNetwork, start: int, goals: set[int]) -> list[str | None] | None:
    """Dijkstra over (node, taxiway name) so a change of taxiway costs extra. Returns the names in order."""
    if start in goals:
        return []
    heap: list[tuple[float, int, int, str | None, tuple]] = [(0.0, 0, start, None, ())]
    seen: set[tuple[int, str | None]] = set()
    tie = 0
    while heap:
        cost, _, node, name, names = heapq.heappop(heap)
        if node in goals:
            return list(names)
        if (node, name) in seen:
            continue
        seen.add((node, name))
        for to, m, ename in net.edges.get(node, ()):
            change = ename is not None and name is not None and ename != name
            nxt_name = ename if ename is not None else name
            nxt_names = names if (ename is None or (names and names[-1] == ename)) else names + (ename,)
            tie += 1
            heapq.heappush(heap, (cost + m + (NAME_CHANGE_PENALTY_M if change else 0.0), tie, to, nxt_name, nxt_names))
    return None


def _holding_points(net: TaxiNetwork, airport: Airport, rwy: Runway) -> set[int]:
    """Runway holding point(s) at the departure end of `rwy`; falls back to any holding point near it."""
    length = (rwy.length_ft or 0.0) / 6076.1
    cands = []
    for node, kind in net.holds:
        along, cross = along_cross(airport, rwy, *net.nodes[node])
        if -0.3 <= along <= min(0.5, length / 2) and cross <= 0.15:
            cands.append((kind != "runway", abs(along), node))
    if not cands:  # no holding point mapped: the taxiway node next to the runway closest to the threshold
        near = []
        for node, (lat, lon) in net.nodes.items():
            along, cross = along_cross(airport, rwy, lat, lon)
            if -0.3 <= along <= min(0.5, length / 2) and 0.03 < cross <= 0.15:
                near.append((abs(along) + cross, node))
        return {min(near)[1]} if near else set()
    cands.sort()
    best = cands[0]
    return {n for k, a, n in cands if (k, round(a, 2)) == (best[0], round(best[1], 2))} or {best[2]}


def spoken_route(names: list[str | None]) -> str:
    """['B', 'A1'] -> 'Bravo, Alfa one'."""
    out = []
    for n in names:
        if not n:
            continue
        words = []
        for c in n:
            if c.isdigit():
                words.append(phrase.digits(c))
            elif c.upper() in phrase._NATO:
                words.append(phrase._NATO[c.upper()].title())
        if words:
            out.append(" ".join(words))
    return ", ".join(out)


def departure_route(net: TaxiNetwork | None, airport: Airport, rwy: Runway, own: OwnState) -> str | None:
    """'Bravo, Alfa' from where the aircraft is to the holding point of `rwy`, or None if unknown."""
    hand = airport.taxi_routes.get(rwy.ident)
    if hand:
        return hand.removeprefix("via ").strip()
    if net is None:
        return None
    goals = _holding_points(net, airport, rwy)
    start = net.nearest(own.lat, own.lon)
    if not goals or start is None:
        return None
    names = route(net, start, goals)
    return spoken_route(names) if names else None


def free_stand(net: TaxiNetwork, traffic: list[Traffic], wanted: str | None) -> str | None:
    """The stand the pilot asked for, else one with no aircraft on it (code assigns it, like real Ground)."""
    if wanted and wanted.upper() in net.stands:
        return wanted.upper()
    for ref in sorted(net.stands, key=lambda r: (len(r), r)):
        lat, lon = net.nodes[net.stands[ref]]
        if not any(t.on_ground and distance_nm(lat, lon, t.lat, t.lon) < 0.03 for t in traffic):
            return ref
    return None


def arrival_route(net: TaxiNetwork | None, own: OwnState, stand: str) -> str | None:
    if net is None or stand not in net.stands:
        return None
    start = net.nearest(own.lat, own.lon)
    names = route(net, start, {net.stands[stand]}) if start is not None else None
    return spoken_route(names) if names else None


_STAND = re.compile(r"\b(?:stand|gate|parking|position)\s+([0-9]+[a-z]?)\b")


def vacate(net: TaxiNetwork | None, airport: Airport, rwy: Runway, own: OwnState) -> tuple[str, bool] | None:
    """The taxiway to leave the runway by: the first exit ahead of the aircraft, else the nearest one behind it
    (then it has to backtrack). Returns (spoken name, backtrack) or None if the map has no named exits."""
    if net is None:
        return None
    here, _ = along_cross(airport, rwy, own.lat, own.lon)
    exits: dict[str, float] = {}
    for node, edges in net.edges.items():
        along, cross = along_cross(airport, rwy, *net.nodes[node])
        if cross > 0.03:  # not on the runway edge
            continue
        for _, _, name in edges:
            if name and (name not in exits or abs(along - here) < abs(exits[name] - here)):
                exits[name] = along
    if not exits:
        return None
    ahead = sorted((a, n) for n, a in exits.items() if a > here + 0.05)
    if ahead:
        return spoken_route([ahead[0][1]]), False
    behind = max((a, n) for n, a in exits.items())
    return spoken_route([behind[1]]), True


def is_taxi_request(norm: str) -> bool:
    return "taxi" in norm and ("request" in norm or "ready" in norm)


def handle_taxi(session, airport: Airport, facility, own: OwnState, pilot_text: str, net: TaxiNetwork | None,
                traffic: list[Traffic], rwy: Runway | None) -> str | None:
    """Taxi clearance, decided and phrased in code. None = not a taxi request (or nothing to decide on)."""
    from atc.clearance import _freq, issuing_role
    from atc.facility import callsign_for
    from atc.readback import _normalize

    norm = _normalize(pilot_text)
    has_ground = _freq(airport, "GND", "RMP") is not None
    if not own.on_ground or not is_taxi_request(norm):
        return None
    if facility.role != "ground" and not (facility.role == "tower" and not has_ground):
        return None
    cs = session.spoken_callsign
    faa = airport.country == "US"
    plan = session.plan
    arriving = session.landed or wanted_stand(pilot_text) is not None or "apron" in norm or "parking" in norm
    if not arriving and plan and plan.is_ifr and plan.origin == airport.icao and session.clearance != "confirmed" \
            and issuing_role(airport) == "clearance":
        f = _freq(airport, "CLD")
        return f"{cs}, no clearance received yet. Contact Delivery {phrase.frequency(f.mhz, faa)}."
    station = f", {callsign_for(airport, facility)}" if session.first_contact(facility.role) else ""
    qnh = ""
    if own.qnh_hpa is not None:
        qnh = (f", altimeter {phrase.digits(f'{own.qnh_hpa * 0.02953:.2f}')}" if faa
               else f", QNH {phrase.digits(f'{own.qnh_hpa:.0f}')}")
    if arriving:
        stand = free_stand(net, traffic, wanted_stand(pilot_text)) if net else wanted_stand(pilot_text)
        if stand is None:
            return None  # nothing to assign: let the model answer from CONTEXT
        via = arrival_route(net, own, stand)
        return f"{cs}{station}, taxi to stand {phrase.spell(stand).lower()}" + (f" via {via}" if via else "") + "."
    if rwy is None:
        return None
    via = departure_route(net, airport, rwy, own)
    rw = phrase.runway(rwy.ident, faa)
    if faa:
        return f"{cs}{station}, runway {rw}, taxi" + (f" via {via}" if via else "") + f"{qnh}."
    return f"{cs}{station}, taxi to holding point runway {rw}" + (f" via {via}" if via else "") + f"{qnh}."


def wanted_stand(pilot_text: str) -> str | None:
    """'request taxi to stand one two' -> '12'."""
    from atc.readback import _normalize

    compact = re.sub(r"(?<=\d) (?=\d)", "", _normalize(pilot_text))
    m = _STAND.search(compact)
    return m.group(1).upper() if m else None
