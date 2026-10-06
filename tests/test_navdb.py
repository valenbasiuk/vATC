"""Against the real Little Navmap MSFS 2024 database on Valen's PC (skipped elsewhere). Values checked
2026-10-05 against his LIDO chart (SABE) and his report (no taxiway G at Rosario)."""

from pathlib import Path

import pytest

from atc import navdb
from atc.airports.schema import load_airport
from atc.models import OwnState
from atc.taxi import departure_route

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.uses_navdb

if navdb.default_path() is None or navdb.connect() is None:
    pytest.skip("no Little Navmap database on this machine", allow_module_level=True)


def _apt(icao):
    a = load_airport(ROOT / "airports" / f"{icao}.yaml")
    navdb.enrich(a)
    return a


def test_rosario_taxiways_are_the_sims_no_golf():
    net = navdb.taxi_network("SAAR")
    names = {n for edges in net.edges.values() for _, _, n in edges if n}
    assert names == {"A", "B"}
    assert net.holds and net.stands


def test_aeroparque_taxiways_match_the_chart():
    net = navdb.taxi_network("SABE")
    names = {n for edges in net.edges.values() for _, _, n in edges if n}
    assert names == set("ABCDEFHIJKLM")
    sabe = _apt("SABE")
    for rwy in sabe.runways:
        assert departure_route(net, sabe, rwy, OwnState(sabe.lat, sabe.lon, 18, 0, 0, 0, True, 121.9))


def test_frequencies_variation_and_approaches_from_the_sim():
    saar = _apt("SAAR")
    kinds = {(f.kind, round(f.mhz, 2)) for f in saar.frequencies}
    assert ("GND", 121.85) in kinds and ("TWR", 118.7) in kinds
    assert saar.frequencies[0].mhz == 118.7  # the YAML's own frequency stays first
    assert saar.mag_var_deg is not None and -11 < saar.mag_var_deg < -9
    assert navdb.approach_type(saar, "20") == "ILS" and navdb.approach_type(saar, "02") == "RNAV"
    sabe = _apt("SABE")
    assert sabe.mag_var_deg == -10  # the YAML (LIDO chart) wins over the sim's -10.2


def test_corrientes_approach_is_resistencia():
    sarc = _apt("SARC")
    app = [f for f in sarc.frequencies if f.kind == "APP"]
    assert app and app[0].spoken == "Resistencia Approach"


def test_find_fix_off_route():
    pos = navdb.find_fix("ROS", (-32.9, -60.8))  # Rosario VOR
    assert pos is not None and abs(pos[0] + 32.9) < 0.3


def test_ksfo_runway_thresholds_and_crossings_from_the_sim():
    from atc.sim.fake import FakeSim
    from atc.taxi import _departure_path, crossings, strips

    ksfo = _apt("KSFO")  # the YAML has no thresholds: the sim's runway ends fill them in
    assert all(r.lat is not None for r in ksfo.runways)
    assert len(strips(ksfo)) == 4  # 10L/28R, 10R/28L, 1L/19R, 1R/19L
    net = navdb.taxi_network("KSFO")
    sim = FakeSim(ksfo)
    sim.update(lat=net.nodes[net.stands["107"]][0], lon=net.nodes[net.stands["107"]][1])
    rwy1l = next(r for r in ksfo.runways if r.ident == "1L")
    names, nodes = _departure_path(net, ksfo, rwy1l, sim.own())
    # departing 1L from stand 107 (calm): both 28s are crossed, named like the runway in use's parallel direction
    assert [c[0] for c in crossings(net, ksfo, nodes, rwy1l)] in (["28R", "28L"], ["10L", "10R"])


def test_navdata_fir_control_and_center_to_center_handoff():
    from atc import flow
    from atc.session import Session
    from atc.sim.fake import FakeSim
    from atc.world import load_world

    if navdb.navigraph_path() is None:
        pytest.skip("no Navigraph boundary database")
    world = load_world("KSFO", ROOT / "airports", None)
    ksfo = world.get("KSFO")
    sim = FakeSim(ksfo, callsign="UAL436")
    sim.update(lat=36.0, lon=-120.5, alt_msl_ft=15000, on_ground=False, heading_deg=140)
    a, f = world.control(sim.own(), ksfo)
    assert (a.icao, f.freq.mhz, f.freq.spoken) == ("FIR:OAKLAND", 127.8, "Oakland Center")
    sim.update(com1_mhz=127.8)
    s = Session(callsign="UAL436", departed_from="KSFO")
    assert flow.next_handoff(s, world, sim.own()) is None  # still in Oakland's FIR
    sim.update(lat=34.6, lon=-118.6)  # into Los Angeles Center's
    key, text = flow.next_handoff(s, world, sim.own())
    assert text == "contact Los Angeles Center one two five point two seven, good day"
