"""Radar monitoring (level bust, 7700), go-arounds and emergencies (monitor.py)."""

import math
from pathlib import Path

from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan
from atc.main import _Callbacks, handle
from atc.models import Traffic
from atc.runway import runway_in_use
from atc.sequence import threshold
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]
CS = "Martinair four one three three"
PLAN = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="F100", origin="SABE", destination="SAAR",
                  destination_name="Rosario", alternate=None, route="DCT", sid="ATOVO4B", sid_transition="ATOVO",
                  cruise_ft=20000, planned_runway="31", dest_runway="02")


class _Quiet(PrintTTS):
    def say(self, text):
        pass


def _setup():
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    saar = load_airport(ROOT / "airports" / "SAAR.yaml")
    world = World([sabe, saar])
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(squawk=PLAN.squawk, qnh_hpa=1015.0)
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", clearance="confirmed", departed_from="SABE")
    return world, sim, s, saar


def _on_final(sim, apt, nm, rwy_ident="02"):
    rwy = next(r for r in apt.runways if r.ident == rwy_ident)
    tlat, tlon = threshold(apt, rwy)
    h = math.radians(rwy.heading_deg)
    lat = tlat - nm * math.cos(h) / 60.0
    lon = tlon - nm * math.sin(h) / (60.0 * math.cos(math.radians(tlat)))
    alt = apt.elevation_ft + 50 + nm * 318
    sim.update(lat=lat, lon=lon, alt_msl_ft=alt, alt_agl_ft=alt - apt.elevation_ft, heading_deg=rwy.heading_deg,
               gs_kt=140, on_ground=False)
    return rwy


def test_level_bust_after_climb_via_sid():
    world, sim, s, _ = _setup()
    sim.update(com1_mhz=120.6, lat=-34.3, lon=-58.8, heading_deg=300)
    sim.set_airborne(3000, 250)
    s.contacted.add("SABE:approach")
    s.assign_level(20000, 3000)
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    sim.update(alt_msl_ft=19900, alt_agl_ft=19900)
    assert cb.tick(now=0) is None  # still climbing to it: fine
    sim.update(alt_msl_ft=20500, alt_agl_ft=20500)
    assert cb.tick(now=1) == f"{CS}, check altitude, maintain flight level two zero zero."
    assert cb.tick(now=2) is None  # said once


def test_emergency_squawk_is_noticed():
    world, sim, s, _ = _setup()
    sim.update(com1_mhz=120.6, lat=-34.3, lon=-58.8)
    sim.set_airborne(9000, 250)
    s.contacted.add("SABE:approach")
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    sim.update(squawk="7700")
    assert cb.tick(now=0) == f"{CS}, emergency squawk observed, say nature of emergency and intentions."


def test_tower_orders_a_go_around_when_the_runway_is_occupied():
    world, sim, s, saar = _setup()
    sim.update(com1_mhz=118.7, wind_dir_deg=20, wind_kt=10)
    s.surface_wind["SAAR"] = (20.0, 10.0)
    s.contacted.add("SAAR:tower")
    s.descent_given = s.vectors_given = s.intercept_given = True
    s.handoffs_done.add("SAAR:tower")
    rwy = _on_final(sim, saar, 0.8)
    tlat, tlon = threshold(saar, rwy)
    sim.add_traffic(Traffic("LV-ABC", tlat, tlon, saar.elevation_ft, 5.0, rwy.heading_deg, True, type="A320"))
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    r = cb.tick(now=100)
    assert r == f"{CS}, go around, I say again, go around, Airbus three twenty on the runway."
    assert not s.intercept_given and "SAAR:tower" not in s.handoffs_done  # the arrival is flown again
    # the pilot reads it back: the missed approach instruction (Rosario Tower does the approach work itself)
    sim.update(alt_msl_ft=1500, alt_agl_ft=1420)
    r = handle(saar, sim, None, _Quiet(), [(None, r)], "Going around, Martinair 4133", session=s, world=world)
    assert r == f"{CS}, roger, follow the published missed approach procedure, expect vectors for another approach."


def test_mayday_gets_priority_and_a_runway():
    world, sim, s, saar = _setup()
    sim.update(com1_mhz=118.7, wind_dir_deg=20, wind_kt=10)
    _on_final(sim, saar, 12)
    r = handle(saar, sim, None, _Quiet(), [], "Mayday mayday mayday, Martinair 4133, engine failure", session=s,
               world=world)
    rw = runway_in_use(saar, 20, 10, "02")
    assert r.startswith(f"{CS}, roger mayday, runway ") and r.endswith("say intentions.")
    assert s.emergency == "mayday" and rw is not None


def test_vfr_go_around_rejoins_the_circuit():
    world, sim, s, saar = _setup()
    s.plan = None
    s.telephony = None
    s.callsign = "LVABC"
    sim.update(com1_mhz=118.7, wind_dir_deg=20, wind_kt=10)
    _on_final(sim, saar, 0.5)
    r = handle(saar, sim, None, _Quiet(), [], "LVABC going around", session=s, world=world)
    assert r.startswith("Lima Victor Alfa Bravo Charlie, roger, climb to circuit altitude, report ")
    assert "downwind runway" in r


def test_traffic_alert_for_converging_traffic_once():
    world, sim, s, _ = _setup()
    sim.update(com1_mhz=120.6, lat=-34.30, lon=-58.80, heading_deg=270)
    sim.set_airborne(9000, 250)
    sim.update(alt_msl_ft=9000)
    s.contacted.add("SABE:approach")
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    t = Traffic("ARG1234", -34.30, -58.88, 9500, 250, 90, False, type="A320")  # ahead, opposite direction
    sim.add_traffic(t)
    assert cb.tick(now=0) is None  # first look
    t.lon = -58.87  # closing
    r = cb.tick(now=1)
    assert r is not None and "traffic, twelve o'clock" in r and "opposite direction, Airbus three twenty" in r
    assert "five hundred feet above" in r
    t.lon = -58.86
    assert cb.tick(now=100) is None  # called once


def test_wrong_handoff_frequency_is_corrected():
    from atc.main import handle as h

    world, sim, s, _ = _setup()
    sim.update(com1_mhz=120.6, lat=-34.30, lon=-58.80)
    sim.set_airborne(11000, 300)
    s.contacted.add("SABE:approach")
    s.where = "SABE"
    s.last_role = "SABE:approach"
    atc = f"{CS}, contact Ezeiza Control one three five decimal five, good day."
    r = h(world.airports[0], sim, None, _Quiet(), [(None, atc)], "134.5, Martinair 4133", session=s, world=world)
    assert r == f"{CS}, negative, contact Ezeiza Control one three five decimal five."
    s.acked_atc = None
    assert h(world.airports[0], sim, None, _Quiet(), [(None, atc)], "135.5, good day, Martinair 4133", session=s,
             world=world) is None
