"""Ready for departure with landing traffic on short final: ICAO conditional line-up ("behind the landing Airbus
three twenty ..., line up and wait ..., behind"), FAA "hold short of runway ...", and the takeoff clearance Tower
gives by itself once the runway is free."""

import math
from pathlib import Path

from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan
from atc.main import _Callbacks, handle
from atc.models import Traffic
from atc.readback import check_readback
from atc.runway import runway_in_use
from atc.sequence import threshold
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import load_world

ROOT = Path(__file__).resolve().parents[1]
CS = "Martinair four one three three"


class _Quiet(PrintTTS):
    def say(self, text):
        pass

    def say_as(self, text, who="ATC", voice=None):
        pass


def _at(apt, rwy, along, cross):
    tlat, tlon = threshold(apt, rwy)
    h = math.radians(rwy.heading_deg)
    e = along * math.sin(h) + cross * math.cos(h)
    n = along * math.cos(h) - cross * math.sin(h)
    return tlat + n / 60, tlon + e / (60 * math.cos(math.radians(tlat)))


def _setup(icao="SABE", wind=(310, 10), callsign="MAR4133"):
    plan = FlightPlan(callsign=callsign, rules="I", aircraft_type="B738", origin=icao, destination=icao,
                      destination_name="Test", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=20000, planned_runway=None)
    world = load_world(icao, ROOT / "airports", plan, use_navdb=False)
    apt = world.get(icao)
    sim = FakeSim(apt, callsign=callsign)
    twr = next(f.mhz for f in apt.frequencies if f.kind == "TWR")
    rwy = runway_in_use(apt, *wind, use="departure")
    lat, lon = _at(apt, rwy, 0.02, -0.08)  # at the holding point, beside the runway
    sim.update(com1_mhz=twr, wind_dir_deg=wind[0], wind_kt=wind[1], qnh_hpa=1013.2, lat=lat, lon=lon,
               heading_deg=(rwy.heading_deg + 90) % 360, squawk=plan.squawk)
    s = Session(callsign=callsign, plan=plan, telephony="Martinair" if callsign == "MAR4133" else None,
                clearance="confirmed")
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    return apt, world, sim, s, cb, rwy


def _landing(apt, rwy, nm, cs="ARG1234", typ="A320"):
    lat, lon = _at(apt, rwy, -nm, 0.0)
    return Traffic(cs, lat, lon, apt.elevation_ft + 50 + nm * 318, 140.0, rwy.heading_deg, False, type=typ)


def test_icao_conditional_line_up_then_takeoff_when_the_runway_is_free():
    apt, world, sim, s, cb, rwy = _setup()
    sim.add_traffic(_landing(apt, rwy, 2.0))
    r = handle(apt, sim, None, _Quiet(), cb.history, "Aeroparque Tower, Martinair 4133, holding point 31, ready for "
               "departure", session=s, world=world)
    assert r == (f"{CS}, Aeroparque Tower, behind the landing Airbus three twenty on two mile final, line up and "
                 "wait runway three one, behind.")
    assert check_readback(r, "behind the landing Airbus, line up and wait runway 31 behind, Martinair 4133").status \
        == "correct"
    assert check_readback(r, "line up and wait runway 31, Martinair 4133").missing == ["behind"]
    assert cb.tick(now=1.0) is None  # still on final
    sim.clear_traffic()  # landed and vacated
    assert cb.tick(now=30.0) == f"{CS}, wind three two zero degrees one zero knots, runway three one, cleared for takeoff."
    assert s.takeoff_waiting is None
    assert cb.tick(now=31.0) is None  # said once


def test_icao_runway_occupied_holds_position():
    apt, world, sim, s, cb, rwy = _setup()
    sim.add_on_runway("ARG1234", rwy.ident)
    r = handle(apt, sim, None, _Quiet(), cb.history, "Aeroparque Tower, Martinair 4133, ready for departure",
               session=s, world=world)
    assert r == f"{CS}, Aeroparque Tower, hold position, traffic on the runway."


def test_faa_holds_short_without_a_conditional_clearance():
    apt, world, sim, s, cb, rwy = _setup("KSFO", wind=(290, 12), callsign="UAL436")
    assert rwy.ident == "1L"
    sim.add_traffic(_landing(apt, rwy, 2.0, typ="B738"))
    r = handle(apt, sim, None, _Quiet(), cb.history, "San Francisco Tower, United 436, holding short 1L, ready for "
               "departure", session=s, world=world)
    assert r == ("United four thirty-six, San Francisco Tower, hold short of runway one left, Boeing seven "
                 "thirty-seven on two mile final.")
    sim.clear_traffic()
    assert cb.tick(now=30.0) == "United four thirty-six, wind two niner zero at one two, runway one left, cleared " \
                                "for takeoff."


def test_vfr_turnout_is_kept_in_the_delayed_clearance():
    apt, world, sim, _, cb, rwy = _setup(callsign="LVABC")
    s = cb.session = Session(callsign="LVABC")
    sim.add_traffic(_landing(apt, rwy, 1.5))
    r = handle(apt, sim, None, _Quiet(), cb.history, "Aeroparque Tower, LV-ABC, ready for departure, request left "
               "turnout", session=s, world=world)
    assert "line up and wait runway three one, behind" in r
    sim.clear_traffic()
    said = cb.tick(now=30.0)
    assert said.startswith("Lima Victor Alfa Bravo Charlie, left turn approved, wind") and \
        said.endswith("runway three one, cleared for takeoff.")


def test_line_up_and_wait_behind_a_departure_rolling():
    apt, world, sim, s, cb, rwy = _setup()
    lat, lon = _at(apt, rwy, 0.4, 0.0)
    sim.add_traffic(Traffic("ARG1234", lat, lon, apt.elevation_ft, 110.0, rwy.heading_deg, True, type="A320"))
    r = handle(apt, sim, None, _Quiet(), cb.history, "Aeroparque Tower, Martinair 4133, ready for departure",
               session=s, world=world)
    assert r == f"{CS}, Aeroparque Tower, line up and wait runway three one."
    assert check_readback(r, "line up and wait 31, Martinair 4133").status == "correct"
    sim.clear_traffic()  # airborne and gone
    assert cb.tick(now=40.0) == f"{CS}, wind three two zero degrees one zero knots, runway three one, cleared for takeoff."


def test_slow_aircraft_on_the_runway_is_not_a_departure():
    apt, world, sim, s, cb, rwy = _setup()
    lat, lon = _at(apt, rwy, 0.8, 0.0)
    sim.add_traffic(Traffic("ARG1234", lat, lon, apt.elevation_ft, 12.0, rwy.heading_deg, True))  # rolling out
    r = handle(apt, sim, None, _Quiet(), cb.history, "Aeroparque Tower, Martinair 4133, ready for departure",
               session=s, world=world)
    assert r == f"{CS}, Aeroparque Tower, hold position, traffic on the runway."
