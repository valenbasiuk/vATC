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
    found = route_path(net, start, goals)
    return found[0] if found else None


def route_path(net: TaxiNetwork, start: int, goals: set[int]) -> tuple[list[str | None], list[int]] | None:
    """route(), plus the graph nodes the aircraft passes, in order (for the runways it crosses)."""
    heap: list = [(0.0, 0, start, None, (), None)]
    parent: dict = {}  # state -> the state it was reached from, on its cheapest path
    tie = 0
    while heap:
        cost, _, node, name, names, prev = heapq.heappop(heap)
        state = (node, name)  # (graph node, taxiway being followed)
        if state in parent:
            continue
        parent[state] = prev
        if node in goals:
            nodes = []
            while state is not None:
                nodes.append(state[0])
                state = parent[state]
            return list(names), nodes[::-1]
        for to, m, ename in net.edges.get(node, ()):
            change = ename is not None and name is not None and ename != name
            nxt_name = ename if ename is not None else name
            nxt_names = names if (ename is None or (names and names[-1] == ename)) else names + (ename,)
            if (to, nxt_name) in parent:
                continue
            tie += 1
            heapq.heappush(heap, (cost + m + (NAME_CHANGE_PENALTY_M if change else 0.0), tie, to, nxt_name, nxt_names,
                                  state))
    return None


def _holding_points(net: TaxiNetwork, airport: Airport, rwy: Runway, side: float | None = None) -> set[int]:
    """Runway holding point(s) at the departure end of `rwy`; falls back to any holding point near it.
    `side` (signed NM off the centerline of where the aircraft starts): holding points on that side of the runway
    first, so the route doesn't cross the departure runway to reach a holding point on the far side."""
    length = (rwy.length_ft or 0.0) / 6076.1
    cands = []
    for node, kind in net.holds:
        along, cross = _signed(airport, rwy, *net.nodes[node])
        if -0.3 <= along <= min(0.5, length / 2) and abs(cross) <= 0.15:
            cands.append((side is not None and cross * side < 0, kind != "runway", abs(along), node))
    if not cands:  # no holding point mapped: the taxiway node next to the runway closest to the threshold
        near = []
        for node, (lat, lon) in net.nodes.items():
            along, cross = _signed(airport, rwy, lat, lon)
            if -0.3 <= along <= min(0.5, length / 2) and 0.03 < abs(cross) <= 0.15:
                near.append((side is not None and cross * side < 0, abs(along) + abs(cross), node))
        return {min(near)[2]} if near else set()
    cands.sort()
    best = cands[0]
    return {n for far, k, a, n in cands if (far, k, round(a, 2)) == (best[0], best[1], round(best[2], 2))} or {best[3]}


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
    found = _departure_path(net, airport, rwy, own)
    return spoken_route(found[0]) if found and found[0] else None


def _departure_path(net: TaxiNetwork | None, airport: Airport, rwy: Runway, own: OwnState):
    if net is None:
        return None
    start = net.nearest(own.lat, own.lon)
    if start is None:
        return None
    goals = _holding_points(net, airport, rwy, side=_signed(airport, rwy, *net.nodes[start])[1])
    return route_path(net, start, goals) if goals else None


ON_RUNWAY_NM = 0.015  # both ends of a segment this close to a centerline: taxiing along the runway, not across


def _signed(airport: Airport, rwy: Runway, lat: float, lon: float) -> tuple[float, float]:
    """(NM along `rwy` from its threshold, NM right of its centerline: negative = left)."""
    import math

    from atc.geo import offset_nm
    from atc.sequence import threshold

    tlat, tlon = threshold(airport, rwy)
    e, n = offset_nm(tlat, tlon, lat, lon)
    h = math.radians(rwy.heading_deg or 0.0)
    return e * math.sin(h) + n * math.cos(h), e * math.cos(h) - n * math.sin(h)


def strips(airport: Airport) -> list[list[Runway]]:
    """Physical runways: each end with its reciprocal (28R with 10L), from the threshold positions. Ends without a
    threshold or length on file are left out (their geometry is a guess)."""
    from atc.geo import heading_diff

    ends = [r for r in airport.runways if r.lat is not None and r.lon is not None and r.heading_deg is not None
            and r.length_ft]
    out: list[list[Runway]] = []
    for r in ends:
        if any(r in s for s in out):
            continue
        twin = next((o for o in ends if o is not r and not any(o in s for s in out)
                     and heading_diff(o.heading_deg, r.heading_deg) > 170
                     and abs(_signed(airport, r, o.lat, o.lon)[1]) < 0.03), None)
        out.append([r, twin] if twin else [r])
    return out


def crossings(net: TaxiNetwork, airport: Airport, nodes: list[int], in_use: Runway | None,
              wind: tuple[float | None, float | None] = (None, None)) -> list[tuple[str, float, float]]:
    """Runways a taxi path crosses, in order: (ident as ATC says it, lat, lon of the crossing). Of the two ends
    of a crossed runway, the one with the best headwind is named, in calm wind the one parallel to the runway in
    use (KSFO departing 1L: "hold short of runway two eight right"). The path is followed side to side of each
    runway: nodes on the runway itself don't count, so a crossing through a node on the centerline is found too."""
    found: list[tuple[int, str, float, float]] = []  # (where in the path, ident, lat, lon)
    pts = [net.nodes[n] for n in nodes]
    for strip in strips(airport):
        r = strip[0]
        length = r.length_ft / 6076.1
        sides = [_signed(airport, r, *p) for p in pts]
        last_side, last_i = 0, None  # the side of the runway the path was last seen on (nodes on it don't count)
        for i, (along, cross) in enumerate(sides):
            side = 0 if abs(cross) < ON_RUNWAY_NM else (1 if cross > 0 else -1)
            if side == 0:
                continue
            if last_side and side != last_side:  # from one side to the other: where it crossed the centerline
                k = min(range(last_i, i + 1), key=lambda j: abs(sides[j][1]))
                if abs(sides[k][1]) < ON_RUNWAY_NM:
                    lat, lon, at = pts[k][0], pts[k][1], sides[k][0]
                else:  # a straight segment across: interpolate
                    k = next(j for j in range(last_i, i) if sides[j][1] * sides[j + 1][1] <= 0)
                    (a1, c1), (a2, c2) = sides[k], sides[k + 1]
                    t = c1 / (c1 - c2) if c1 != c2 else 0.0
                    lat = pts[k][0] + t * (pts[k + 1][0] - pts[k][0])
                    lon = pts[k][1] + t * (pts[k + 1][1] - pts[k][1])
                    at = a1 + t * (a2 - a1)
                if -0.01 <= at <= length + 0.01:
                    found.append((k, _named_end(strip, in_use, wind).ident, lat, lon))
                    break  # each runway is crossed (and cleared) once
            last_side, last_i = side, i
    return [(ident, lat, lon) for _, ident, lat, lon in sorted(found)]


def _named_end(strip: list[Runway], in_use: Runway | None, wind: tuple[float | None, float | None]) -> Runway:
    """Of the two ends of a crossed runway: the best headwind, in calm wind the one parallel to the runway in use."""
    from atc.geo import heading_diff
    from atc.runway import headwind_kt

    wdir, wkt = wind
    if wdir is not None and wkt:
        return max(strip, key=lambda x: headwind_kt(wdir, wkt, x.heading_deg))
    if in_use is not None and in_use.heading_deg is not None:
        return min(strip, key=lambda x: heading_diff(x.heading_deg, in_use.heading_deg))
    return strip[0]


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


# where the pilot wants to go ("taxi to stand 12", "request gate 5"), not where they are ("at stand 12")
_STAND = re.compile(r"\b(?:to|for|request|requesting|via) (?:the )?(?:stand|gate|parking|position)\s+([0-9]+[a-z]?)\b")


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


def exit_side(net: TaxiNetwork | None, airport: Airport, rwy: Runway, spoken_exit: str) -> str | None:
    """'left' / 'right': which side of the runway (looking along `rwy`) the exit taxiway leaves to."""
    import math

    from atc.sequence import threshold

    if net is None:
        return None
    tlat, tlon = threshold(airport, rwy)
    h = math.radians(rwy.heading_deg or 0.0)
    total = 0.0
    for node, edges in net.edges.items():
        for _, _, name in edges:
            if not name or spoken_route([name]) != spoken_exit:
                continue
            lat, lon = net.nodes[node]
            _, cross = along_cross(airport, rwy, lat, lon)
            if 0.03 < cross < 0.3:  # off the runway, close to it
                e = (lon - tlon) * 60.0 * math.cos(math.radians(tlat))
                n = (lat - tlat) * 60.0
                total += e * math.cos(h) - n * math.sin(h)  # + = right of the centerline
    if total == 0.0:
        return None
    return "right" if total > 0 else "left"


def is_taxi_request(norm: str) -> bool:
    return ("taxi" in norm and ("request" in norm or "ready" in norm)) or _is_progressive(norm)


def _is_progressive(norm: str) -> bool:
    """FAA "request progressive (taxi)", ICAO "unfamiliar with the airport" / "detailed taxi instructions"."""
    return "progressive" in norm or "unfamiliar" in norm or "detailed taxi" in norm


def _edge_name(net: TaxiNetwork, a: int, b: int) -> str | None:
    return next((n for to, _, n in net.edges.get(a, ()) if to == b), None)


def turn_calls(net: TaxiNetwork, nodes: list[int]) -> list[tuple[float, float, str]]:
    """Progressive taxi: (lat, lon, "turn left on Bravo") at each node of the path where the taxiway changes.
    The side comes from the path's direction a little before and after the turn (graph nodes can be dense)."""
    from atc.geo import bearing_deg

    names = [_edge_name(net, a, b) for a, b in zip(nodes, nodes[1:])]
    out: list[tuple[float, float, str]] = []
    cur = None
    for i, name in enumerate(names):
        if name is None:
            continue
        if cur is not None and name != cur and 0 < i < len(nodes) - 1:
            before = net.nodes[nodes[max(0, i - 3)]]
            at = net.nodes[nodes[i]]
            after = net.nodes[nodes[min(len(nodes) - 1, i + 3)]]
            d = (bearing_deg(*at, *after) - bearing_deg(*before, *at)) % 360
            words = spoken_route([name])
            side = "right" if 25 <= d <= 180 else "left" if 180 < d <= 335 else None
            out.append((at[0], at[1], f"turn {side} on {words}" if side else f"continue on {words}"))
        cur = name
    return out


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
        tail = ""
        if _is_progressive(norm) and net is not None and stand in net.stands:
            start = net.nearest(own.lat, own.lon)
            found = route_path(net, start, {net.stands[stand]}) if start is not None else None
            session.progressive = turn_calls(net, found[1]) if found else []
            tail = ", I'll call your turns" if session.progressive else ""
        return f"{cs}{station}, taxi to stand {phrase.spell(stand).lower()}" + (f" via {via}" if via else "") + \
            f"{tail}."
    if rwy is None:
        return None
    # the runway given with the taxi clearance stays this pilot's (like an approved request) even if the AI's runway
    # in use changes while they taxi
    session.runway_requests[f"{airport.icao}:departure"] = rwy.ident
    via = departure_route(net, airport, rwy, own)
    rw = phrase.runway(rwy.ident, faa)
    # runways on the way: hold short of the first one; the pilot calls holding short and Ground clears the crossing
    found = None if airport.taxi_routes.get(rwy.ident) else _departure_path(net, airport, rwy, own)
    session.crossings = crossings(net, airport, found[1], rwy, (own.wind_dir_deg, own.wind_kt)) if found else []
    hold = f", hold short of runway {phrase.runway(session.crossings[0][0], faa)}" if session.crossings else ""
    session.progressive = turn_calls(net, found[1]) if found and _is_progressive(norm) else []
    if session.progressive:
        hold += ", I'll call your turns"
    if faa:
        return f"{cs}{station}, runway {rw}, taxi" + (f" via {via}" if via else "") + f"{hold}{qnh}."
    return f"{cs}{station}, taxi to holding point runway {rw}" + (f" via {via}" if via else "") + f"{hold}{qnh}."


CROSSING_NEAR_NM = 0.25  # holding short of a crossing: this close to where the route crosses that runway
_HOLDING_SHORT = re.compile(r"\b(?:holding short|hold short|short of|request(?:ing)? (?:to )?cross|ready to cross)\b")


def handle_crossing(session, airport: Airport, facility, own: OwnState, pilot_text: str,
                    traffic: list[Traffic]) -> str | None:
    """'Holding short of runway two eight right': Ground clears the crossing when that runway is free (nobody on
    it, nobody on short final to either end), else keeps them holding with the reason. Position decides, not words
    alone, so the readback of 'hold short of runway 28R' in the taxi clearance is not taken for it."""
    from atc.readback import _normalize
    from atc.sequence import runway_status

    if facility.role not in ("ground", "tower") or not own.on_ground or not session.crossings:
        return None
    norm = _normalize(pilot_text)
    ident, lat, lon = session.crossings[0]
    if not _HOLDING_SHORT.search(norm) or own.gs_kt > 5 or distance_nm(own.lat, own.lon, lat, lon) > CROSSING_NEAR_NM:
        return None
    faa = airport.faa
    cs = session.spoken_callsign
    whys = []
    for r in next((s for s in strips(airport) if any(e.ident == ident for e in s)), []):  # both directions
        st = runway_status(airport, r, own, traffic)
        why = st.takeoff_blocked()
        if why:  # said as seen from the end the traffic is using ("on two mile final"), not as head-on
            whys.append((bool(st.opposite) and not st.occupied_by and not st.finals, why))
    if whys:
        return f"{cs}, hold short of runway {phrase.runway(ident, faa)}, {min(whys)[1]}."
    session.crossings.pop(0)
    nxt = f", hold short of runway {phrase.runway(session.crossings[0][0], faa)}" if session.crossings else ""
    return f"{cs}, cross runway {phrase.runway(ident, faa)}{nxt}."


def wanted_stand(pilot_text: str) -> str | None:
    """'request taxi to stand one two' -> '12'."""
    from atc.readback import _normalize

    compact = re.sub(r"(?<=\d) (?=\d)", "", _normalize(pilot_text))
    m = _STAND.search(compact)
    return m.group(1).upper() if m else None


TURN_CALL_NM = 0.08  # progressive taxi: the turn is said this far before it (~150 m)


def progressive_event(session, world, own: OwnState) -> str | None:
    """The next turn of a progressive taxi, when the aircraft gets close to it. Turns already passed (a later one
    is nearer) are dropped; the list ends with the aircraft off the ground or off the Ground frequency."""
    if not session.progressive:
        return None
    picked = world.pick(own)
    if not own.on_ground or picked is None or picked[1].role not in ("ground", "tower"):
        session.progressive = []
        return None
    dists = [distance_nm(own.lat, own.lon, lat, lon) for lat, lon, _ in session.progressive]
    nearest = min(range(len(dists)), key=dists.__getitem__)
    if dists[nearest] > TURN_CALL_NM or own.gs_kt < 2:
        return None
    _, _, text = session.progressive[nearest]
    del session.progressive[: nearest + 1]
    return f"{session.spoken_callsign}, {text}."
