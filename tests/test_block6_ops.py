"""Improvement plan block 6, the ATC side: closures by hand (G4), Ground taxiing to an intersection (B6), side-step
to the parallel (C3), US Class Bravo transitions (E1), ride reports (D2)."""

import math
from dataclasses import replace
from pathlib import Path

from atc import atis, closures
from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan
from atc.main import _Callbacks, handle
from atc.models import Traffic
from atc.runway import runway_in_use, session_runway
from atc.sequence import threshold
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.taxi import load_network
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]
CS = "Martinair four one three three"
PLAN = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="B738", origin="SABE", destination="SAAR",
                  destination_name="Islas Malvinas", alternate=None, route="ATOVO4B ATOVO W5 PEDRO DCT ESKON DCT",
                  sid="ATOVO4B", sid_transition="ATOVO", cruise_ft=20000, planned_runway="31", dest_runway="02")


class _Q(PrintTTS):
    def say(self, text):
        pass

    def say_as(self, text, who="ATC", voice=None):
        pass


def _sabe(**changes):
    sabe = replace(load_airport(ROOT / "airports" / "SABE.yaml"), **changes)
    net = load_network(ROOT / "airports", "SABE", use_navdb=False)
    world = World([sabe], taxi={"SABE": net})
    closures.refresh(world)
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(com1_mhz=121.9, qnh_hpa=1015.0, wind_dir_deg=300.0, wind_kt=8.0)
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", clearance="confirmed", dest_name="Rosario")
    return sabe, world, net, sim, s


# --- G4 closures --------------------------------------------------------------------------------------------------

def test_a_closed_runway_is_never_in_use_and_a_request_for_it_is_unable():
    sabe, world, net, sim, s = _sabe(closed=["runway 13/31"])
    assert runway_in_use(sabe, 300.0, 8.0) is None
    sabe, world, net, sim, s = _sabe(closed=["runway 31"])
    assert runway_in_use(sabe, 300.0, 8.0).ident == "13"  # into a tailwind rather than onto a closed runway
    r = handle(sabe, sim, None, _Q(), [], "Aeroparque Ground, Martinair 4133, request runway 31", session=s, world=world)
    assert r == f"{CS}, Aeroparque Ground, unable, runway three one closed."


def test_closures_end_at_their_time_and_are_in_the_atis():
    sabe, world, net, sim, s = _sabe(closed=["runway 13/31 until 1800", "taxiway E"])
    closures.ZULU[0] = 17 * 3600.0
    try:
        assert closures.runway_closed(sabe, "13") and closures.closed_taxiways(sabe) == {"E"}
        words, lines = atis.build(atis.AtisState(), sabe, sim.own(), (300.0, 8.0))
        assert "Runway one three three one closed." in lines and "Taxiway Echo closed." in lines
        closures.ZULU[0] = 18 * 3600.0 + 60
        assert not closures.runway_closed(sabe, "13")
    finally:
        closures.ZULU[0] = None


def test_closed_taxiway_is_left_out_of_the_route():
    sabe, world, net, sim, s = _sabe()
    rwy = next(r for r in sabe.runways if r.ident == "31")
    from atc.taxi import departure_route

    lat, lon = net.nodes[net.stands[sorted(net.stands)[0]]]
    sim.update(lat=lat, lon=lon)
    open_route = departure_route(net, sabe, rwy, sim.own())
    first = open_route.split(", ")[0]
    letter = first[0]
    net.closed = {letter}
    closed_route = departure_route(net, sabe, rwy, sim.own())
    assert closed_route != open_route and not closed_route.startswith(first)


def test_arrival_held_while_the_only_runway_is_closed():
    from atc.flightplan import Fix

    saar = replace(load_airport(ROOT / "airports" / "SAAR.yaml"), closed=["runway 02/20"])
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    plan = replace(PLAN, sid=None, route="DCT PEDRO DCT ESKON DCT",
                   fixes=[Fix("PEDRO", -33.701111, -59.666944, 20000), Fix("ESKON", -33.231111, -60.328889, 14600)])
    world = World([sabe, saar])
    sim = FakeSim(saar, callsign="MAR4133")
    sim.update(com1_mhz=118.7, lat=-33.24, lon=-60.40, alt_msl_ft=5000, alt_agl_ft=4920, heading_deg=320,
               gs_kt=230, on_ground=False, wind_dir_deg=20.0, wind_kt=8.0, zulu_s=14 * 3600 + 20 * 60)
    s = Session(callsign="MAR4133", plan=plan, telephony="Martinair", clearance="confirmed", departed_from="SABE")
    s.contacted.add("SAAR:tower")
    s.descent_given = True
    s.assign_level(5000, 20000)
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = cb.tick(now=0)
    assert r.startswith(f"{CS}, hold at ESKON") and r.endswith("delay due runway closure.") and s.hold_reason == "runway"
    saar.closed = []  # reopened
    assert cb.tick(now=10) == f"{CS}, leave the hold, expect vectors runway zero two."


# --- B6 Ground to an intersection -----------------------------------------------------------------------------

def test_taxi_to_an_intersection_then_takeoff_from_there_without_backtrack():
    sabe, world, net, sim, s = _sabe()
    stand = sorted(net.stands)[0]
    sim.update(lat=net.nodes[net.stands[stand]][0], lon=net.nodes[net.stands[stand]][1])
    h: list = []
    r = handle(sabe, sim, None, _Q(), h, "Aeroparque Ground, Martinair 4133, request taxi, intersection departure "
               "from Echo", session=s, world=world)
    assert r.startswith(f"{CS}, Aeroparque Ground, taxi to holding point Echo runway three one via ")
    assert s.intersection_planned == "Echo"
    from atc.taxi import intersection_hold

    rwy = next(r for r in sabe.runways if r.ident == "31")
    hold = intersection_hold(net, sabe, rwy, "E")
    sim.update(lat=net.nodes[hold][0], lon=net.nodes[hold][1], gs_kt=0.0, com1_mhz=118.85)
    r = handle(sabe, sim, None, _Q(), h, "Aeroparque Tower, Martinair 4133, holding point Echo, ready for departure",
               session=s, world=world)
    assert "runway three one from intersection Echo" in r and "metres available, cleared for takeoff" in r, r


def test_faa_intersection_taxi_words():
    from atc.taxi import _wanted_intersection

    assert _wanted_intersection("request taxi intersection departure from bravo 1") == "B1"
    assert _wanted_intersection("request taxi for departure from delta") == "D"
    assert _wanted_intersection("request taxi full length") is None
    assert _wanted_intersection("request taxi intersection departure present position") is None


# --- C3 side-step ---------------------------------------------------------------------------------------------

KSFO_ENDS = {"28R": (37.613529, -122.357117, 297.9, 11855.0), "28L": (37.611691, -122.358322, 297.9, 11382.0),
             "10L": (37.628742, -122.393394, 117.9, 11855.0), "10R": (37.626305, -122.39315, 117.9, 11382.0)}


def _ksfo():
    from atc.world import load_world

    plan = FlightPlan(callsign="UAL436", rules="I", aircraft_type="B738", origin="KLAX", destination="KSFO",
                      destination_name="San Francisco", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=30000, planned_runway=None)
    world = load_world("KSFO", ROOT / "airports", plan, use_navdb=False)
    ksfo = world.get("KSFO")
    for r in ksfo.runways:
        if r.ident in KSFO_ENDS:
            r.lat, r.lon, r.heading_deg, r.length_ft = KSFO_ENDS[r.ident]
    return world, ksfo, plan


def _on_final(apt, ident, nm):
    rwy = next(r for r in apt.runways if r.ident == ident)
    tlat, tlon = threshold(apt, rwy)
    h = math.radians(rwy.heading_deg)
    return rwy, tlat - nm * math.cos(h) / 60.0, tlon - nm * math.sin(h) / (60.0 * math.cos(math.radians(tlat)))


def test_pilot_side_step_request_and_tower_clears_the_parallel():
    world, ksfo, plan = _ksfo()
    sim = FakeSim(ksfo, callsign="UAL436")
    rwy, lat, lon = _on_final(ksfo, "28L", 5.0)
    sim.update(com1_mhz=120.5, lat=lat, lon=lon, alt_msl_ft=1600, alt_agl_ft=1590, heading_deg=rwy.heading_deg,
               gs_kt=150, on_ground=False, wind_dir_deg=290.0, wind_kt=12.0)
    s = Session(callsign="UAL436", plan=plan, telephony="United")
    s.contacted.add("KSFO:tower")
    s.runway_requests["KSFO:arrival"] = "28L"
    s.intercept_given = True
    r = handle(ksfo, sim, None, _Q(), [], "United 436, request side-step to runway 28R", session=s, world=world)
    assert r == "United four thirty-six, side-step approved, runway two eight right, cleared to land."
    assert session_runway(ksfo, 290.0, 12.0, s, "arrival").ident == "28R"


def test_approach_offers_the_side_step_when_its_runway_has_traffic_close_ahead():
    from atc.enroute import intercept_text

    world, ksfo, plan = _ksfo()
    sim = FakeSim(ksfo, callsign="UAL436")
    rwy, lat, lon = _on_final(ksfo, "28L", 12.0)
    sim.update(lat=lat, lon=lon, alt_msl_ft=4000, alt_agl_ft=3990, heading_deg=rwy.heading_deg, gs_kt=180,
               on_ground=False, wind_dir_deg=290.0, wind_kt=12.0)
    s = Session(callsign="UAL436", plan=plan, telephony="United")
    _, alat, alon = _on_final(ksfo, "28L", 8.0)
    ahead = Traffic("SWA123", alat, alon, ksfo.elevation_ft + 2500, 160.0, rwy.heading_deg, False, type="B738")
    r = intercept_text(s, ksfo, rwy, sim.own(), [ahead])
    assert "side-step to runway two eight right" in r and "traffic to follow" not in r, r
    assert s.runway_requests["KSFO:arrival"] == "28R"
    s2 = Session(callsign="UAL436", plan=plan, telephony="United")
    assert "side-step" not in intercept_text(s2, ksfo, rwy, sim.own(), [])  # nobody ahead: no need


# --- E1 Class Bravo transition ----------------------------------------------------------------------------------

def test_class_bravo_transition_after_radar_contact():
    world, ksfo, _ = _ksfo()
    app = next(f for f in ksfo.frequencies if f.kind == "APP")
    sim = FakeSim(ksfo, callsign="N123AB")
    sim.update(com1_mhz=app.mhz, lat=37.75, lon=-122.25, alt_msl_ft=3000, alt_agl_ft=2990, heading_deg=200, gs_kt=110,
               on_ground=False, qnh_hpa=1013.2)
    s = Session(callsign="N123AB")
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = handle(ksfo, sim, None, _Q(), cb.history, "NorCal Approach, N123AB, request Bravo transition southbound",
               session=s, world=world)
    assert r.startswith("November one two three Alfa Bravo, ") and ", squawk " in r
    code = s.plan.squawk if s.plan else __import__("atc.following", fromlist=["vfr_code"]).vfr_code(s)
    sim.update(squawk=code)
    said = cb.tick(now=1)
    assert said.startswith("November one two three Alfa Bravo, radar contact, ") and said.endswith(
        "cleared through the San Francisco Class Bravo airspace, maintain VFR at or below three thousand five hundred.")


# --- D2 ride reports ---------------------------------------------------------------------------------------------

def test_ride_reports_from_the_winds_on_the_way_up():
    from atc import ride
    from atc.world import World as W

    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    world = W([sabe])
    sim = FakeSim(sabe, callsign="MAR4133")
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", clearance="confirmed", departed_from="SABE")
    s.contacted.add("SABE:approach")
    for alt, wkt in ((19000, 39), (19500, 40), (20000, 40), (20500, 46), (21000, 52), (21500, 57), (22000, 62),
                     (22500, 62), (23000, 63), (23500, 63), (24000, 63)):
        sim.update(on_ground=False, alt_msl_ft=alt, alt_agl_ft=alt, wind_dir_deg=270.0, wind_kt=float(wkt))
        ride.observe(s, sim.own())
    sim.update(com1_mhz=120.6, alt_msl_ft=24000)

    def ask(text):
        return handle(sabe, sim, None, _Q(), [], text, session=s, world=world)

    assert ask("Martinair 4133, any ride reports at flight level 210?") == \
        f"{CS}, moderate chop reported between flight level two zero zero and flight level two two zero."
    assert ask("Martinair 4133, how is the ride at FL240?") == f"{CS}, no turbulence reported at flight level two four zero."
    assert ask("Martinair 4133, any reports of turbulence at flight level 350?") == \
        f"{CS}, no ride reports at flight level three five zero."
