"""The ground side of our own aircraft: free stands, and the paths they push back, taxi, line up, vacate and taxi
in along. Same taxi graph and route as Ground's clearance (taxi.py), so "via Delta, India" is what they really taxi.

Pushback: tail first along the stand's lead-in line onto the taxiway, then on along the taxiway away from where it
will taxi, so it ends up nose pointing the right way (as a tug leaves an aircraft). The taxi starts from there.

Speed limits per segment: TAXI_KT on named taxiways, APRON_KT on apron lanes and lead-ins (unnamed in the sim's
data). After landing: the first exit far enough ahead to make the turn, then the route to the stand.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atc.geo import bearing_deg, distance_nm, heading_diff
from atc.models import Airport, Runway, Traffic
from atc.own.motion import APRON_KT, COMFORT_DECEL, KT, TAXI_KT, TURN_KT, moved
from atc.taxi import TaxiNetwork, _holding_points, _signed, route_path, spoken_route

PUSH_ON_TAXIWAY_M = 38.0  # how far the tug pushes along the taxiway past the lead-in
LINEUP_ALONG_NM = 0.02  # line up this far into the runway past the holding point's abeam (or the threshold)
LINEUP_STRAIGHT_M = 60.0  # straight bit on the centerline, so the takeoff starts aligned
GATE_TYPES = ("GH", "GM", "GS", "RGAL", "RGAM", "RC", "RMCL", "RMCM")  # preference order for airliners
GA_TYPES_FIRST = ("RGAS", "RGA", "RGAM", "RGAL", "GS")
EXIT_ON_RUNWAY_M = 25.0  # a graph node this close to the centerline is where an exit taxiway meets the runway
CLEAR_OF_RUNWAY_M = 85.0  # this far off the centerline the aircraft is clear (past the holding line)
WAIT_CLEAR_M = 20.0  # after vacating it waits for Ground this much further on


@dataclass
class Stand:
    ref: str
    lat: float
    lon: float
    heading: float  # nose of a parked aircraft, true
    kind: str = ""
    radius_m: float = 0.0


@dataclass
class DeparturePaths:
    stand: Stand
    push: list[tuple[float, float]]  # tail first, from the stand
    taxi: list[tuple[float, float]]  # nose first, to the holding point
    lineup: list[tuple[float, float]]  # holding point -> on the centerline, aligned
    via: str  # "Delta, India" (spoken), "" if the route has no named taxiway
    runway: Runway
    taxi_limits: list[float] = field(default_factory=list)  # knots, one per taxi segment


@dataclass
class ArrivalPaths:
    points: list[tuple[float, float]]  # from where the rollout ends, through the exit, to the stand
    limits: list[float]
    clear_s: float  # metres along `points` where it is clear of the runway (it waits there for Ground)
    exit: str  # "Foxtrot" (spoken), "" if unnamed
    via: str  # the route to the stand, spoken
    stand: Stand
    exit_along_nm: float = 0.0  # where the exit leaves the runway, NM from its threshold


def stands(icao: str, net: TaxiNetwork) -> list[Stand]:
    """The sim's parking spots (Little Navmap db), else the taxi map's stands (no heading: nose to the lead-in)."""
    from atc import navdb

    out = [Stand(p["ref"], p["lat"], p["lon"], p["heading"], p["type"], p["radius_m"]) for p in navdb.parkings(icao)
           if p["type"] not in ("FUEL", "V", "H", "MIL_CARGO")]
    if out:
        return out
    for ref, node in net.stands.items():
        lat, lon = net.nodes[node]
        nxt = next((to for to, _, _ in net.edges.get(node, ())), None)
        head = (bearing_deg(lat, lon, *net.nodes[nxt]) + 180.0) % 360.0 if nxt is not None else 0.0
        out.append(Stand(ref, lat, lon, head))
    return out


def free_stand(all_stands: list[Stand], occupied: list[Traffic], wingspan_m: float, airport: Airport,
               rng=None, reserved: list[Stand] = (), ga: bool = False) -> Stand | None:
    """A stand nobody is on or has been given, big enough for the wingspan (the biggest free one if none is), off
    the runways. GA aircraft prefer GA ramps, airliners gates."""
    from atc.sequence import along_cross

    def free(s: Stand) -> bool:
        clear = max(30.0, s.radius_m) / 1852.0
        return not any(t.on_ground and distance_nm(s.lat, s.lon, t.lat, t.lon) < clear for t in occupied) \
            and not any(r.ref == s.ref for r in reserved)

    cands = [s for s in all_stands if free(s)
             and not any(along_cross(airport, r, s.lat, s.lon)[1] < 0.06 for r in airport.runways)]
    fit = [s for s in cands if s.radius_m == 0.0 or s.radius_m >= wingspan_m / 2 - 1.0]
    if fit:
        order = GA_TYPES_FIRST if ga else GATE_TYPES
        rank = {k: i for i, k in enumerate(order)}
        best = min(rank.get(s.kind, len(rank)) for s in fit)
        pool = [s for s in fit if rank.get(s.kind, len(rank)) == best]
        return rng.choice(pool) if rng else pool[0]
    return max(cands, key=lambda s: s.radius_m, default=None)


def _push_extension(net: TaxiNetwork, j: int, k: int, came_from: int | None) -> list[tuple[float, float]]:
    """Points along the taxiway from node j, away from node k (where the taxi goes), PUSH_ON_TAXIWAY_M long.
    `came_from`: the lead-in node before j (the tug doesn't push back up the lead-in)."""
    away = (bearing_deg(*net.nodes[j], *net.nodes[k]) + 180.0) % 360.0
    out: list[tuple[float, float]] = []
    prev, cur, left = came_from, j, PUSH_ON_TAXIWAY_M
    heading = away
    while left > 0:
        options = [(to, m) for to, m, _ in net.edges.get(cur, ()) if to not in (prev, k)]
        if not options:
            break
        to, m = min(options, key=lambda o: heading_diff(bearing_deg(*net.nodes[cur], *net.nodes[o[0]]), heading))
        b = bearing_deg(*net.nodes[cur], *net.nodes[to])
        if heading_diff(b, heading) > 60:  # no taxiway goes on that way
            break
        if m >= left:
            out.append(moved(*net.nodes[cur], b, left))
            break
        out.append(net.nodes[to])
        left -= m
        prev, cur, heading = cur, to, b
    return out


def _centerline(airport: Airport, rwy: Runway, along_nm: float) -> tuple[float, float]:
    from atc.sequence import threshold

    return moved(*threshold(airport, rwy), rwy.heading_deg or 0.0, along_nm * 1852.0)


def _edge_names(net: TaxiNetwork) -> dict[tuple[int, int], str | None]:
    return {(a, to): n for a in net.edges for to, _, n in net.edges[a]}


def _limited(pts: list[tuple[tuple[float, float], float | None]]) -> tuple[list[tuple[float, float]], list[float]]:
    """[(point, limit of the segment that ends at it)] -> (points, limits), close points merged (slowest kept)."""
    out: list[tuple[float, float]] = []
    lims: list[float] = []
    for p, lim in pts:
        if out and distance_nm(*out[-1], *p) * 1852.0 <= 0.5:
            if lims and lim is not None:
                lims[-1] = min(lims[-1], lim)
            continue
        if out:
            lims.append(lim if lim is not None else APRON_KT)
        out.append(p)
    return out, lims


def departure_paths(net: TaxiNetwork, airport: Airport, rwy: Runway, stand: Stand) -> DeparturePaths | None:
    """Push, taxi and line-up paths from `stand` to runway `rwy`, or None if the map has no way there."""
    start = net.nearest(stand.lat, stand.lon)
    if start is None:
        return None
    goals = _holding_points(net, airport, rwy, side=_signed(airport, rwy, *net.nodes[start])[1])
    found = route_path(net, start, goals) if goals else None
    if found is None or len(found[1]) < 2:
        return None
    names, nodes = found
    edge_name = _edge_names(net)
    # the lead-in ends at the first junction (or where a named taxiway starts): apron lanes are often unnamed
    if edge_name.get((nodes[0], nodes[1])):
        j = 0
    else:
        j = next((i for i in range(1, len(nodes) - 1)
                  if len(net.edges.get(nodes[i], ())) >= 3 or edge_name.get((nodes[i], nodes[i + 1]))), len(nodes) - 1)
    lead = [(stand.lat, stand.lon)] + [net.nodes[n] for n in nodes[:j + 1]]
    ext = _push_extension(net, nodes[j], nodes[j + 1], nodes[j - 1] if j else None) if j + 1 < len(nodes) else []
    push, _ = _limited([(p, None) for p in lead + ext])
    seq = [(push[-1], None)] + [(p, APRON_KT) for p in ext[::-1][1:]] + [(net.nodes[nodes[j]], APRON_KT)]
    seq += [(net.nodes[b], TAXI_KT if edge_name.get((a, b)) else APRON_KT) for a, b in zip(nodes[j:], nodes[j + 1:])]
    taxi, limits = _limited(seq)
    if len(push) < 2:  # the stand is on the taxiway itself: no pushback
        push = []
    hold = net.nodes[nodes[-1]]
    along, _ = _signed(airport, rwy, *hold)
    on = max(along, 0.0) + LINEUP_ALONG_NM
    p = _centerline(airport, rwy, on)
    q = _centerline(airport, rwy, on + LINEUP_STRAIGHT_M / 1852.0)
    return DeparturePaths(stand, push, taxi, [hold, p, q], spoken_route(names), rwy, limits)


def arrival_paths(net: TaxiNetwork, airport: Airport, rwy: Runway, lat: float, lon: float, v_kt: float,
                  stand: Stand, decel: float = COMFORT_DECEL, extra_m: float = 0.0) -> ArrivalPaths | None:
    """After landing on `rwy`, rolling at v_kt at (lat, lon): the first exit far enough ahead to slow down for the
    turn (braking at `decel`, plus `extra_m`), then the route to `stand`. Exits that leave on the stand's side of
    the runway come first (no crossing the runway it just landed on to get to the terminal). None if no exit leads
    to the stand."""
    from atc.own.motion import Path

    here, _ = _signed(airport, rwy, lat, lon)
    need_nm = (max(0.0, ((v_kt * KT) ** 2 - (TURN_KT * KT) ** 2) / (2 * decel)) + extra_m) / 1852.0
    length_nm = (rwy.length_ft or 0.0) / 6076.1
    on_rwy = sorted((a, n) for n, (la, lo) in net.nodes.items() for a, c in [_signed(airport, rwy, la, lo)]
                    if abs(c) * 1852.0 <= EXIT_ON_RUNWAY_M and here < a <= length_nm + 0.03)
    ahead = [x for x in on_rwy if x[0] >= here + need_nm] or on_rwy[-1:]  # else the last one, at the far end
    side = _signed(airport, rwy, stand.lat, stand.lon)[1]
    goal = net.nearest(stand.lat, stand.lon)
    edge_name = _edge_names(net)
    best: ArrivalPaths | None = None
    for along, node in ahead:
        found = route_path(net, node, {goal})
        if found is None:
            continue
        names, nodes = found
        seq = [(_centerline(airport, rwy, here), None), (_centerline(airport, rwy, along), TAXI_KT),
               (net.nodes[node], TAXI_KT)]
        seq += [(net.nodes[b], TAXI_KT if edge_name.get((a, b)) else APRON_KT) for a, b in zip(nodes, nodes[1:])]
        seq += [((stand.lat, stand.lon), APRON_KT)]
        pts, lims = _limited(seq)
        if len(pts) < 2:
            continue
        path = Path(pts, limits_kt=lims)
        clear = next((s for s in _samples(path) if abs(_signed(airport, rwy, *path.latlon(s))[1]) * 1852.0
                      > CLEAR_OF_RUNWAY_M), None)
        if clear is None:
            continue
        exit_name = next((n for n in names if n), None)
        out = ArrivalPaths(pts, lims, min(path.length, clear + WAIT_CLEAR_M),
                           spoken_route([exit_name]) if exit_name else "", spoken_route(names[1:] or names), stand,
                           along)
        if _signed(airport, rwy, *path.latlon(clear))[1] * side > 0:  # leaves toward the stand: take it
            return out
        best = best or out
    return best


def _samples(path, step: float = 4.0):
    s = 0.0
    while s <= path.length:
        yield s
        s += step


def push_words(paths: DeparturePaths, faa: bool, key: str) -> str | None:
    """"tail left" / "tail east" / "facing west" for this pushback, or None (no pushback). `key` picks the style
    (stable per airport and flight: one controller doesn't switch between forms)."""
    import zlib

    from atc import phrase
    from atc.own.motion import Path

    if len(paths.push) < 2 or len(paths.taxi) < 2 or Path(paths.push).length < 15.0:
        return None
    styles = phrase.PUSH_STYLES_FAA if faa else phrase.PUSH_STYLES_ICAO
    style = styles[zlib.crc32(key.encode()) % len(styles)]
    return phrase.push_direction(paths.stand.heading, Path(paths.taxi).heading(0.0), style)


def user_push_words(net: TaxiNetwork | None, airport: Airport, rwy: Runway | None, lat: float, lon: float,
                    heading: float, key: str) -> str | None:
    """The same for the user, when they are parked at a stand (within 40 m of one): the pushback that lines them
    up for the taxi to `rwy`."""
    if net is None or rwy is None:
        return None
    if not any(distance_nm(lat, lon, s.lat, s.lon) * 1852.0 < 40.0 for s in stands(airport.icao, net)):
        return None
    paths = departure_paths(net, airport, rwy, Stand("", lat, lon, heading))
    return push_words(paths, airport.faa, key) if paths is not None else None
