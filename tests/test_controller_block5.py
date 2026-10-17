"""Improvement plan block 5 (controller behaviour and emergencies): readback mode (G1), blocked transmissions (G2),
a busy controller's "standby" (G3), emergencies after the first reply (F1), low-visibility procedures (A5)."""

from dataclasses import replace
from pathlib import Path

import pytest

from atc import atis, own, readback, weather
from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS, blocked_by
from atc.flightplan import FlightPlan
from atc.geo import destination
from atc.main import _Callbacks, handle
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


def _ground(mode: str = "strict"):
    readback.MODE = mode
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    saar = load_airport(ROOT / "airports" / "SAAR.yaml")
    world = World([sabe, saar], taxi={"SABE": load_network(ROOT / "airports", "SABE")})
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(com1_mhz=121.9, qnh_hpa=1015.0, wind_dir_deg=300.0, wind_kt=8.0)
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", clearance="confirmed", dest_name="Rosario")
    return sabe, world, sim, s


@pytest.fixture(autouse=True)
def _strict_after():
    yield
    readback.MODE = "strict"


# --- G1 readback mode ---------------------------------------------------------------------------------------------

def test_relaxed_mode_lets_omissions_and_roger_pass_but_corrects_a_wrong_runway():
    sabe, world, sim, s = _ground("relaxed")
    h: list = []

    def call(text):
        return handle(sabe, sim, None, _Q(), h, text, session=s, world=world)

    taxi = call("Aeroparque Ground, Martinair 4133, request taxi")
    route = taxi.split(" via ", 1)[1].split(",")[0]
    assert call("Roger, Martinair 4133") is None  # strict: "read back"
    taxi = call("Martinair 4133, request taxi")
    assert call(f"holding point 31 via {route}, Martinair 4133") is None  # QNH left out: fine when learning
    taxi = call("Martinair 4133, request taxi")
    wrong = call(f"holding point 13 via {route}, QNH 1015, Martinair 4133")
    assert wrong.startswith(f"{CS}, negative, I say again, taxi to holding point runway three one")


def test_strict_mode_is_unchanged():
    sabe, world, sim, s = _ground("strict")
    h: list = []
    taxi = handle(sabe, sim, None, _Q(), h, "Aeroparque Ground, Martinair 4133, request taxi", session=s, world=world)
    assert taxi.endswith("QNH one zero one five.")
    assert handle(sabe, sim, None, _Q(), h, "Roger, Martinair 4133", session=s, world=world) == f"{CS}, read back."


def test_relaxed_clearance_needs_only_sid_level_and_squawk():
    sabe, world, sim, s = _ground("relaxed")
    s.clearance = "none"
    sim.update(com1_mhz=129.3)
    h: list = []
    clr = handle(sabe, sim, None, _Q(), h, "Aeroparque Delivery, Martinair 4133, request IFR clearance", session=s,
                 world=world)
    assert "ATOVO four bravo departure" in clr
    r = handle(sabe, sim, None, _Q(), h, f"ATOVO 4B, climb via SID, flight level 200, squawk {PLAN.squawk}, Martinair 4133",
               session=s, world=world)
    assert r.startswith(f"{CS}, readback correct")


def test_launcher_settings_for_readback_and_busy_controller():
    from atc.config import Config

    argv = Config(readback="relaxed", busy_standby=False).to_argv()
    assert argv[argv.index("--readback") + 1] == "relaxed" and "--no-busy-standby" in argv
    assert "--readback" not in Config().to_argv() and "--no-busy-standby" not in Config().to_argv()


# --- G2 blocked transmissions ----------------------------------------------------------------------------------

def test_blocked_by_another_aircraft_or_not_heard_while_atc_talks():
    log = [(10.0, 13.0, "Argentina one two three four"), (20.0, 24.0, "ATC"), (30.0, 40.0, "ATIS")]
    assert blocked_by(log, None, 12.0, 15.0) == "pilot"  # 1 s on top of an AI pilot
    assert blocked_by(log, None, 22.0, 25.0) == "ATC"
    assert blocked_by(log, None, 31.0, 35.0) is None  # the ATIS is another frequency
    assert blocked_by(log, None, 12.8, 14.0) is None  # 0.2 s: a clipped word, not a blocked call
    assert blocked_by([], ("Argentina one two three four", 50.0), 50.5, 52.0) == "pilot"  # still on the air
    assert blocked_by(log, None, None, None) is None


# --- G3 busy controller -----------------------------------------------------------------------------------------

def test_busy_ground_says_standby_then_gives_the_taxi_clearance(monkeypatch):
    import atc.main as main

    monkeypatch.setattr(main, "BUSY_STANDBY_CHANCE", 1.0)
    sabe, world, sim, s = _ground()
    cb = _Callbacks(world, sim, _Q(), [], s)
    s.busy = lambda role: role == "ground"
    r = handle(sabe, sim, None, _Q(), cb.history, "Aeroparque Ground, Martinair 4133, request taxi", session=s,
               world=world)
    assert r == f"{CS}, Aeroparque Ground, standby."
    assert s.deferred is not None and s.deferred["text"].startswith(f"{CS}, taxi to holding point runway three one")
    s.deferred["at"] = 50.0
    assert cb.tick(now=40.0) is None
    said = cb.tick(now=51.0)
    assert said.startswith(f"{CS}, taxi to holding point runway three one") and s.deferred is None
    assert cb.history[-1][1] == said  # the readback is checked against it


def test_standby_answer_is_lost_on_another_frequency_and_given_at_once_when_asked_again(monkeypatch):
    import atc.main as main

    monkeypatch.setattr(main, "BUSY_STANDBY_CHANCE", 1.0)
    sabe, world, sim, s = _ground()
    cb = _Callbacks(world, sim, _Q(), [], s)
    s.busy = lambda role: True
    handle(sabe, sim, None, _Q(), cb.history, "Aeroparque Ground, Martinair 4133, request taxi", session=s, world=world)
    sim.update(com1_mhz=118.85)
    cb.tick(now=1e9)
    assert s.deferred is None  # left the frequency: gone
    sim.update(com1_mhz=121.9)
    handle(sabe, sim, None, _Q(), cb.history, "Martinair 4133, request taxi", session=s, world=world)
    assert s.deferred is not None
    r = handle(sabe, sim, None, _Q(), cb.history, "Martinair 4133, request taxi", session=s, world=world)
    assert r.startswith(f"{CS}, taxi to holding point") and s.deferred is None  # asked again: answered at once


def test_quiet_frequency_never_says_standby(monkeypatch):
    import atc.main as main

    monkeypatch.setattr(main, "BUSY_STANDBY_CHANCE", 1.0)
    sabe, world, sim, s = _ground()
    s.busy = lambda role: False
    r = handle(sabe, sim, None, _Q(), [], "Aeroparque Ground, Martinair 4133, request taxi", session=s, world=world)
    assert "taxi to holding point" in r and s.deferred is None


def test_radio_bus_is_busy_after_several_exchanges():
    from atc.chatter import Exchange, RadioBus

    bus = RadioBus()
    assert not bus.busy("tower", 100.0)
    bus.mark_said(90.0, "tower")
    bus.mark_said(95.0, "tower")
    assert bus.busy("tower", 100.0) and not bus.busy("ground", 100.0)
    assert not bus.busy("tower", 200.0)  # an hour of quiet later
    bus.offer(Exchange("A", [("ATC", "x", None)], 199.0, "ground"))
    bus.offer(Exchange("B", [("ATC", "y", None)], 199.0, "ground"))
    assert bus.busy("ground", 200.0)


# --- F1 emergencies ---------------------------------------------------------------------------------------------

def _departed(com=120.6):
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    saar = load_airport(ROOT / "airports" / "SAAR.yaml")
    world = World([sabe, saar])
    sim = FakeSim(sabe, callsign="MAR4133")
    lat, lon = destination(sabe.lat, sabe.lon, 300.0, 14.0)
    sim.update(com1_mhz=com, lat=lat, lon=lon, alt_msl_ft=6000, alt_agl_ft=5980, heading_deg=300, gs_kt=230,
               on_ground=False, wind_dir_deg=130.0, wind_kt=10.0, qnh_hpa=1013.0, squawk=PLAN.squawk)
    s = Session(callsign="MAR4133", plan=replace(PLAN), telephony="Martinair", clearance="confirmed",
                departed_from="SABE", dest_name="Rosario")
    s.contacted.add("SABE:approach")
    s.surface_wind["SABE"] = (130.0, 10.0)
    return sabe, world, sim, s


def test_mayday_return_to_the_field_then_persons_on_board_and_cancel():
    sabe, world, sim, s = _departed()
    cb = _Callbacks(world, sim, _Q(), [], s)

    def call(text):
        return handle(sabe, sim, None, _Q(), cb.history, text, session=s, world=world)

    r = call("Mayday mayday mayday, Aeroparque Approach, Martinair 4133, engine failure, request return to the field")
    assert r.startswith(f"{CS}, roger mayday, runway one three available, wind one four zero degrees one zero knots")
    assert "vectors runway one three" in r and r.endswith("when able, report persons on board and fuel endurance.")
    assert s.plan.destination == "SABE" and s.original_destination == "SAAR" and own.EMERGENCY["SABE"]["runway"] == "13"
    r = call("Martinair 4133, 154 persons on board, 2 hours 30 minutes of fuel")
    assert r == f"{CS}, roger, emergency services will be standing by."
    assert s.emergency_info["souls"] == 154 and s.emergency_info["fuel_min"] == 150
    # the pilot's runway of choice, whatever the wind
    r = call("Martinair 4133, request runway 31")
    assert r == f"{CS}, expect runway three one."
    assert s.runway_requests["SABE:arrival"] == "31"
    assert call("Martinair 4133, cancel mayday") == f"{CS}, roger, emergency cancelled."
    assert s.emergency is None and not own.EMERGENCY


def test_pan_pan_then_intentions_in_a_second_call():
    sabe, world, sim, s = _departed()

    def call(text):
        return handle(sabe, sim, None, _Q(), [], text, session=s, world=world)

    r = call("Pan pan pan pan pan pan, Martinair 4133, hydraulic problem")
    assert r == f"{CS}, roger pan pan, runway one three available, wind one four zero degrees one zero knots, " \
                "say intentions."
    r = call("Martinair 4133, we would like to return to Aeroparque")
    assert r.startswith(f"{CS}, roger, runway one three available") and \
        r.endswith("when able, report persons on board and fuel endurance.")
    assert s.plan.destination == "SABE"


def test_mayday_divert_to_a_named_airport():
    sabe, world, sim, s = _departed(com=135.5)
    world.airspaces.append(load_airport(ROOT / "airspace" / "SAEF.yaml"))
    lat, lon = destination(sabe.lat, sabe.lon, 300.0, 120.0)
    sim.update(lat=lat, lon=lon, alt_msl_ft=20000, alt_agl_ft=19900)
    s.contacted.add("SAEF:control")
    r = handle(sabe, sim, None, _Q(), [], "Mayday mayday mayday, Martinair 4133, cabin fire, diverting to Rosario",
               session=s, world=world)
    assert r.startswith(f"{CS}, roger mayday, runway ") and "cleared direct Rosario" in r
    assert s.plan.destination == "SAAR"


def test_our_traffic_gives_way_to_the_emergency():
    from atc.own.injector import FakeInjector
    from atc.own.manager import OwnTraffic
    from atc.own.schedule import Departure
    from atc.sequence import threshold

    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    world = World([sabe], taxi={"SABE": load_network(ROOT / "airports", "SABE")})
    sim = FakeSim(sabe, callsign="MAR4133")
    mgr = OwnTraffic(world, sim, FakeInjector(), wind=lambda a: (130.0, 10.0))
    assert "final runway 13" in mgr.spawn_arrival(sabe, 0.0, dep=Departure("ARG", "1500", "B738"), dist_nm=9.0)
    rwy = next(r for r in sabe.runways if r.ident == "13")
    tlat, tlon = threshold(sabe, rwy)
    import math

    h = math.radians(rwy.heading_deg)
    user_lat = tlat - 5 * math.cos(h) / 60.0
    user_lon = tlon - 5 * math.sin(h) / (60.0 * math.cos(math.radians(tlat)))
    sim.update(lat=user_lat, lon=user_lon, alt_msl_ft=1600, alt_agl_ft=1580, heading_deg=rwy.heading_deg, gs_kt=150,
               on_ground=False)
    own.EMERGENCY["SABE"] = {"runway": "13"}
    mgr.step(0.0, sim.own(), [])
    mgr.step(2.0, sim.own(), [])
    p = mgr.pilots["ARG1500"]
    assert p.state == "go_around"  # behind the emergency on the same final: sent around
    mgr.close()


# --- A5 low-visibility procedures ---------------------------------------------------------------------------------

def _fog():
    weather.put(weather.parse({"icaoId": "SABE", "obsTime": 1791255600, "wdir": 130, "wspd": 5, "visib": 0.19,
                               "temp": 9, "dewp": 9, "altim": 1018, "clouds": [{"cover": "OVX", "base": 100}],
                               "rawOb": "SABE 111200Z 13005KT 0300 R13/0350N FG VV001 09/09 Q1018"}))


def test_atis_in_fog_says_lvp_and_the_rvr():
    _fog()
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    sim = FakeSim(sabe)
    sim.update(wind_dir_deg=130, wind_kt=5, qnh_hpa=1018)
    word, lines = atis.build(atis.AtisState(), sabe, sim.own(), (130.0, 5.0))
    text = " ".join(lines)
    assert "Low visibility procedures in operation. RVR runway one three, three five zero metres." in text
    assert weather.get("SABE").rvr == {"13": (350, "m")}


def test_landing_clearance_in_fog_has_the_rvr_and_crossings_go_to_tower():
    from atc.enroute import arrival_event
    from atc.taxi import crossing_by_tower

    _fog()
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    saar = load_airport(ROOT / "airports" / "SAAR.yaml")
    world = World([saar, sabe])
    plan = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="B738", origin="SAAR", destination="SABE",
                      destination_name="Aeroparque", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=20000, planned_runway="20", dest_runway="13")
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(com1_mhz=118.85, wind_dir_deg=130, wind_kt=5, qnh_hpa=1018)
    sim.place_on_final(sabe, 4.0)
    s = Session(callsign="MAR4133", plan=plan, telephony="Martinair", clearance="confirmed", departed_from="SAAR")
    s.surface_wind["SABE"] = (130.0, 5.0)
    s.contacted.add("SABE:tower")
    r = arrival_event(s, world, sim.own(), [])
    assert r == f"{CS}, wind one four zero degrees five knots, runway one three, cleared to land, RVR three five zero metres."
    ksfo_like = replace(sabe, crossings_by="ground")
    assert crossing_by_tower(ksfo_like, sim.own(), (130.0, 5.0))  # LVP: Tower whatever the file says


def test_lvp_spaces_our_arrivals_wider():
    from atc.own.injector import FakeInjector
    from atc.own.manager import ARRIVAL_SPACING_NM, OwnTraffic
    from atc.own.schedule import Departure

    _fog()
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    world = World([sabe], taxi={"SABE": load_network(ROOT / "airports", "SABE")})
    sim = FakeSim(sabe, callsign="MAR4133")
    mgr = OwnTraffic(world, sim, FakeInjector(), wind=lambda a: (130.0, 5.0))
    mgr.spawn_arrival(sabe, 0.0, dep=Departure("ARG", "1500", "B738"), dist_nm=4.0)
    msg = mgr.spawn_arrival(sabe, 0.0, dep=Departure("ARG", "1501", "B738"))
    nm = float(msg.split(" on a ")[1].split(" NM")[0])
    assert nm >= 4.0 + ARRIVAL_SPACING_NM * 1.5 - 0.5, msg
    mgr.close()
