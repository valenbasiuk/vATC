"""Taxiway names the sim's default scenery leaves out, filled from OpenStreetMap (2026-10-08 check vs AD charts)."""

from atc.geo import distance_nm
from atc.own.motion import moved
from atc.taxi import TaxiNetwork, _clean_names, fill_names

O = (-25.6, -54.49)


def _net(points: dict, edges: list) -> TaxiNetwork:
    net = TaxiNetwork()
    net.nodes = dict(points)
    for a, b, name in edges:
        m = distance_nm(*net.nodes[a], *net.nodes[b]) * 1852
        net.edges.setdefault(a, []).append((b, m, name))
        net.edges.setdefault(b, []).append((a, m, name))
    return net


def test_unnamed_sim_segments_along_an_osm_taxiway_get_its_name():
    sim = _net({1: moved(*O, 90, 0), 2: moved(*O, 90, 200), 3: moved(*moved(*O, 90, 200), 0, 150),
                4: moved(*O, 0, 80)},
               [(1, 2, None), (2, 3, None), (1, 4, "A")])
    osm = _net({11: moved(*moved(*O, 90, -20), 0, 12), 12: moved(*moved(*O, 90, 260), 0, 12),  # 12 m off, along 1-2
                13: moved(*O, 0, 400), 14: moved(*O, 0, 600)},
               [(11, 12, "E"), (13, 14, "Z")])
    assert fill_names(sim, osm) == 1
    names = {(a, b): n for a, es in sim.edges.items() for b, _, n in es}
    assert names[(1, 2)] == names[(2, 1)] == "E"  # along E
    assert names[(2, 3)] is None  # crosses it: not E
    assert names[(1, 4)] == "A"  # the sim's own name stays


def test_a_few_metres_of_a_crossing_taxiway_are_not_named_in_the_route():
    pts = {i: moved(*O, 90, 100 * i) for i in range(5)}
    pts[2] = moved(*O, 90, 230)
    net = _net(pts, [(0, 1, "Y2"), (1, 2, "Y2"), (2, 3, "Y4"), (3, 4, "Y2")])
    net.nodes[3] = moved(*O, 90, 260)  # Y4 is 30 m long here
    for a in list(net.edges):
        net.edges[a] = [(b, distance_nm(*net.nodes[a], *net.nodes[b]) * 1852, n) for b, _, n in net.edges[a]]
    assert _clean_names(net, [0, 1, 2, 3, 4]) == ["Y2"]
