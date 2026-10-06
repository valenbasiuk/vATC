"""Runway crossings on the taxi route (taxi.py): "hold short of runway X" in the taxi clearance, then "cross runway
X" when the pilot reports holding short and the runway is free. Synthetic airport KXRW: departure runway 36/18 north
of runway 09/27, the stand south of 09/27, taxiway Alfa crossing it."""

from atc.facility import resolve_facility
from atc.models import Airport, Frequency, Runway, Traffic
from atc.readback import check_readback
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.taxi import TaxiNetwork, handle_crossing, handle_taxi, strips

NM_DEG = 1 / 60  # one NM of latitude (and of longitude at the equator)
CS = "November one two three Alfa Bravo"


def _airport() -> Airport:
    rwys = [
        Runway("09", 90.0, 6076.1, lat=0.0, lon=0.0), Runway("27", 270.0, 6076.1, lat=0.0, lon=NM_DEG),
        Runway("36", 0.0, 6076.1, lat=0.03, lon=0.03), Runway("18", 180.0, 6076.1, lat=0.03 + NM_DEG, lon=0.03),
    ]
    return Airport("KXRW", "Crossing Test", 0.01, 0.01, 0.0, country="US", towered=True, runways=rwys,
                   frequencies=[Frequency("GND", 121.9), Frequency("TWR", 118.3)])


def _net() -> TaxiNetwork:
    net = TaxiNetwork()
    net.nodes = {1: (-0.005, 0.008), 2: (0.005, 0.008), 3: (0.025, 0.029), 4: (0.03, 0.03 - 0.05 * NM_DEG)}
    for a, b in ((1, 2), (2, 3), (3, 4)):
        net.edges.setdefault(a, []).append((b, 100.0, "A"))
        net.edges.setdefault(b, []).append((a, 100.0, "A"))
    net.holds = [(4, "runway")]
    net.stands = {"5": 1}
    return net


def _setup(lat=-0.005, lon=0.008):
    apt = _airport()
    sim = FakeSim(apt, callsign="N123AB")
    sim.update(lat=lat, lon=lon, com1_mhz=121.9, wind_dir_deg=270, wind_kt=10, qnh_hpa=1013.2)
    return apt, sim, Session(callsign="N123AB"), resolve_facility(apt, 121.9)


def test_runway_ends_pair_into_strips():
    assert sorted(sorted(r.ident for r in s) for s in strips(_airport())) == [["09", "27"], ["18", "36"]]


def test_taxi_clearance_holds_short_of_the_crossed_runway():
    apt, sim, s, gnd = _setup()
    rwy36 = apt.runways[2]
    r = handle_taxi(s, apt, gnd, sim.own(), "Ground, N123AB, at stand 5, request taxi", _net(), [], rwy36)
    assert r == (f"{CS}, Crossing Test Ground, runway three six, taxi via Alfa, hold short of runway two seven, "
                 "altimeter two niner niner two.")  # 27 has the headwind (wind 270)
    assert [c[0] for c in s.crossings] == ["27"]
    # the readback is a readback, not "holding short" (and the aircraft is still at the stand)
    assert handle_crossing(s, apt, gnd, sim.own(), "runway 36, taxi via A, hold short runway 27, N123AB", []) is None
    assert check_readback(r, "runway 36, taxi via A, hold short runway 27, 2992, N123AB").status == "correct"
    assert check_readback(r, "runway 36, taxi via A, 2992, N123AB").missing == ["hold short"]  # must come back


def test_cross_when_the_runway_is_free():
    apt, sim, s, gnd = _setup()
    handle_taxi(s, apt, gnd, sim.own(), "Ground, N123AB, request taxi", _net(), [], apt.runways[2])
    sim.update(lat=-0.002)  # stopped at the hold short line, 0.12 NM south of runway 27
    assert handle_crossing(s, apt, gnd, sim.own(), "N123AB, holding short runway 27", []) == \
        f"{CS}, cross runway two seven."
    assert s.crossings == []


def test_keep_holding_with_traffic_on_short_final():
    apt, sim, s, gnd = _setup()
    handle_taxi(s, apt, gnd, sim.own(), "Ground, N123AB, request taxi", _net(), [], apt.runways[2])
    sim.update(lat=-0.002)
    # landing runway 27 from the east: 2 NM final, 3 degree path
    sim.add_traffic(Traffic("AAL12", 0.0, 3 * NM_DEG, 700.0, 140.0, 270.0, False, type="B738"))
    r = handle_crossing(s, apt, gnd, sim.own(), "N123AB, holding short runway 27", sim.traffic(0, 0, 50))
    assert r == f"{CS}, hold short of runway two seven, Boeing seven thirty-seven on two mile final."
    assert [c[0] for c in s.crossings] == ["27"]  # still to cross


def test_progressive_taxi_calls_the_turns():
    from atc.taxi import progressive_event, turn_calls
    from atc.world import World

    apt = _airport()
    net = TaxiNetwork()
    # Alfa eastbound, then Bravo north (a left turn), then Charlie east again (a right turn)
    pts = {1: (-0.01, 0.0), 2: (-0.01, 0.002), 3: (-0.01, 0.004), 4: (-0.008, 0.004), 5: (-0.006, 0.004),
           6: (-0.006, 0.006), 7: (-0.006, 0.008)}
    net.nodes = pts
    for a, b, n in ((1, 2, "A"), (2, 3, "A"), (3, 4, "B"), (4, 5, "B"), (5, 6, "C"), (6, 7, "C")):
        net.edges.setdefault(a, []).append((b, 100.0, n))
        net.edges.setdefault(b, []).append((a, 100.0, n))
    calls = turn_calls(net, [1, 2, 3, 4, 5, 6, 7])
    assert [t for _, _, t in calls] == ["turn left on Bravo", "turn right on Charlie"]
    sim = FakeSim(apt, callsign="N123AB")
    s = Session(callsign="N123AB")
    s.progressive = calls
    world = World([apt])
    sim.update(lat=-0.01, lon=0.0, com1_mhz=121.9, gs_kt=15.0, heading_deg=90.0, on_ground=True)
    assert progressive_event(s, world, sim.own()) is None  # 0.24 NM before the first turn
    sim.update(lon=0.0035)
    assert progressive_event(s, world, sim.own()) == f"{CS}, turn left on Bravo."
    sim.update(lat=-0.006, lon=0.0045, heading_deg=0.0)
    assert progressive_event(s, world, sim.own()) == f"{CS}, turn right on Charlie."
    assert s.progressive == []
