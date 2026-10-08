"""Improvement plan block 1 (2026-10-07): weather the controller gives where there is no ATIS, the sim's own
weather when it doesn't match the METAR, the ATIS on a clock with "all stations" on a change, runway crossings by
Tower in reduced visibility, and backtracking where the taxiways join the runway part way down (SARC)."""

from dataclasses import replace

import pytest

from atc import atis, own, weather
from atc.audio.tts import PrintTTS
from atc.facility import resolve_facility
from atc.main import _Callbacks, handle
from atc.models import Frequency, OwnState, Runway
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.taxi import handle_crossing, handle_taxi
from atc.world import World
from tests.test_crossings import _airport as _crossing_airport
from tests.test_crossings import _net as _crossing_net
from tests.test_flight import ORIGIN

CS = "Martinair four one three three"
METAR = {"icaoId": "SATS", "obsTime": 1791255600, "temp": 24, "dewp": 18, "wdir": 300, "wspd": 12, "visib": "6+",
         "altim": 1015, "cover": "CAVOK", "clouds": [], "rawOb": "METAR SATS 060300Z 30012KT CAVOK 24/18 Q1015"}


class _Quiet(PrintTTS):
    def say(self, text):
        pass


@pytest.fixture
def controller_weather(monkeypatch):
    monkeypatch.setattr("atc.atis.CONTROLLER_WEATHER", True)


def _sim(apt=ORIGIN, **kw):
    sim = FakeSim(apt, callsign="MAR4133")
    base = dict(lat=-34.5638, lon=-58.4072, com1_mhz=121.9, qnh_hpa=1015.0, wind_dir_deg=300.0, wind_kt=12.0,
                temp_c=24.0)
    base.update(kw)
    sim.update(**base)
    return sim


# --- A3: no ATIS -> the controller gives the weather -------------------------------------------------------------

def test_no_atis_departure_gets_runway_wind_temperature_and_qnh_once(controller_weather):
    weather.put(weather.parse(METAR))
    sim, s, h = _sim(), Session(callsign="MAR4133", telephony="Martinair"), []
    r = handle(ORIGIN, sim, None, _Quiet(), h, "Testa Ground, Martinair 4133, request push and start", session=s)
    assert r == (f"{CS}, Testa Ground, push and start approved. Runway three one in use, wind three zero zero "
                 "degrees one two knots, temperature two four, QNH one zero one five.")
    r = handle(ORIGIN, sim, None, _Quiet(), h, "Martinair 4133, request taxi", session=s)
    assert "wind" not in r  # once per airport


def test_no_atis_taxi_reply_with_the_runway_and_qnh_does_not_repeat_them(controller_weather):
    weather.put(weather.parse(METAR))
    sim, s = _sim(), Session(callsign="MAR4133", telephony="Martinair")
    r = handle(ORIGIN, sim, None, _Quiet(), [], "Testa Ground, Martinair 4133, request taxi", session=s)
    assert r.count("QNH") == 1 and r.count("runway three one") == 1 and "wind three zero zero" in r.lower()


def test_airport_with_an_atis_gets_no_controller_weather(controller_weather):
    apt = replace(ORIGIN, frequencies=ORIGIN.frequencies + [Frequency("ATIS", 127.6)])
    sim, s = _sim(apt), Session(callsign="MAR4133", telephony="Martinair")
    r = handle(apt, sim, None, _Quiet(), [], "Testa Ground, Martinair 4133, request push and start", session=s)
    assert r == f"{CS}, Testa Ground, push and start approved."


# --- A4: the sim's own weather -----------------------------------------------------------------------------------

def test_preset_weather_in_the_sim_wins_over_the_metar():
    weather.put(weather.parse(METAR))
    apt = replace(ORIGIN, frequencies=ORIGIN.frequencies + [Frequency("ATIS", 127.6)])
    live = _sim(apt).own()
    _, lines = atis.build(atis.AtisState(), apt, live, (300.0, 12.0), None)
    assert "CAVOK." in " ".join(lines) and not atis.SIM_WX["SATS"]
    preset = _sim(apt, qnh_hpa=1005.0, visibility_m=2000.0).own()  # 10 hPa off the METAR: the sim's own weather
    _, lines = atis.build(atis.AtisState(), apt, preset, (300.0, 12.0), None)
    text = " ".join(lines)
    assert atis.SIM_WX["SATS"] and "CAVOK" not in text and "Visibility two thousand meters." in text
    assert "QNH one zero zero five." in text
    assert atis.conditions(apt, preset, (300.0, 12.0)).reduced


# --- A1/A2: the ATIS on a clock, "all stations" ------------------------------------------------------------------

def test_a_qnh_change_is_broadcast_to_all_stations():
    apt = replace(ORIGIN, frequencies=ORIGIN.frequencies + [Frequency("ATIS", 127.6)])
    sim = _sim(apt, com1_mhz=118.85)
    s = Session(callsign="MAR4133", telephony="Martinair")
    cb = _Callbacks(World([apt]), sim, _Quiet(), [], s)
    assert cb.tick(now=0.0) is None  # first build: the letter starts
    first = s.atis.letters["SATS"][0]
    sim.update(qnh_hpa=1011.0)
    assert cb.tick(now=30.0) is None  # not a minute yet
    said = cb.tick(now=61.0)
    word = atis.letter_word(s.atis.letters["SATS"][0])
    assert s.atis.letters["SATS"][0] == (first + 1) % 26
    assert said == f"All stations, Testa Tower, information {word} now current, QNH one zero one one."


# --- B1: crossings by Tower in reduced visibility ----------------------------------------------------------------

def _crossing(visibility_m: int, rule: str | None = None):
    apt = _crossing_airport()
    if rule:
        apt = replace(apt, crossings_by=rule)
    weather.put(weather.parse({"icaoId": "KXRW", "obsTime": 1791255600, "wdir": 270, "wspd": 10,
                               "visib": visibility_m / 1609.34, "altim": 1013, "clouds": [],
                               "rawOb": "METAR KXRW 060300Z 27010KT"}))
    sim = FakeSim(apt, callsign="N123AB")
    sim.update(lat=-0.005, lon=0.008, com1_mhz=121.9, wind_dir_deg=270, wind_kt=10, qnh_hpa=1013.2)
    s = Session(callsign="N123AB")
    gnd, twr = resolve_facility(apt, 121.9), resolve_facility(apt, 118.3)
    handle_taxi(s, apt, gnd, sim.own(), "Ground, N123AB, request taxi", _crossing_net(), [], apt.runways[2])
    sim.update(lat=-0.002)  # holding short of runway 27
    return apt, sim, s, gnd, twr


def test_good_visibility_ground_clears_the_crossing():
    apt, sim, s, gnd, _ = _crossing(9999)
    assert handle_crossing(s, apt, gnd, sim.own(), "N123AB, holding short runway 27", []) == \
        "November one two three Alfa Bravo, cross runway two seven."


def test_reduced_visibility_ground_sends_to_tower_which_clears_it():
    apt, sim, s, gnd, twr = _crossing(3000)
    assert handle_crossing(s, apt, gnd, sim.own(), "N123AB, holding short runway 27", []) == \
        "November one two three Alfa Bravo, hold short of runway two seven, contact Tower one one eight point three."
    sim.update(com1_mhz=118.3)
    assert handle_crossing(s, apt, twr, sim.own(), "Tower, N123AB, holding short runway 27", []) == \
        "November one two three Alfa Bravo, cross runway two seven, then contact Ground one two one point niner."


def test_crossings_by_tower_set_in_the_airport_file():
    apt, sim, s, gnd, _ = _crossing(9999, rule="tower")
    assert "contact Tower" in handle_crossing(s, apt, gnd, sim.own(), "N123AB, holding short runway 27", [])


# --- B2: backtrack --------------------------------------------------------------------------------------------------

R13 = ORIGIN.runways[0]


def _mid_runway_sim():
    """The user at a holding point 800 m down runway 13, 90 m off the centerline (SARC's layout)."""
    from atc.own.airport import _runway_point

    sim = FakeSim(ORIGIN, callsign="MAR4133")
    lat, lon = _runway_point(ORIGIN, R13, 800.0, 90.0)
    sim.update(lat=lat, lon=lon, com1_mhz=118.85, wind_dir_deg=124.0, wind_kt=8.0, heading_deg=34.0, qnh_hpa=1015.0)
    return sim


def test_backtrack_then_takeoff_once_lined_up_at_the_end():
    from atc.own.airport import _runway_point

    sim = _mid_runway_sim()
    s = Session(callsign="MAR4133", telephony="Martinair")
    cb = _Callbacks(World([ORIGIN]), sim, _Quiet(), [], s)
    r = handle(ORIGIN, sim, None, _Quiet(), cb.history, "Testa Tower, Martinair 4133, ready for departure", session=s)
    assert r == f"{CS}, Testa Tower, backtrack runway one three, line up and wait."
    lat, lon = _runway_point(ORIGIN, R13, 500.0, 0.0)  # backtracking, halfway: nothing yet
    sim.update(lat=lat, lon=lon, heading_deg=304.0, gs_kt=12.0)
    assert cb.tick(now=1.0) is None
    lat, lon = _runway_point(ORIGIN, R13, 80.0, 0.0)  # turned round at the end, stopped
    sim.update(lat=lat, lon=lon, heading_deg=124.0, gs_kt=0.0)
    assert cb.tick(now=2.0) == f"{CS}, wind one two zero degrees eight knots, runway one three, cleared for takeoff."


def test_no_backtrack_with_traffic_on_final_inside_eight_miles():
    sim = _mid_runway_sim()
    sim.add_on_final(6.0, "ARG1234", "13")
    s = Session(callsign="MAR4133", telephony="Martinair")
    r = handle(ORIGIN, sim, None, _Quiet(), [], "Testa Tower, Martinair 4133, ready for departure", session=s)
    assert r == f"{CS}, Testa Tower, hold position, traffic on six mile final."
    assert s.backtrack == "pending"


def test_our_departure_backtracks_and_turns_round_at_the_end():
    from atc.own.airport import backtrack_points
    from atc.own.motion import Path
    from atc.taxi import _signed

    pts = backtrack_points(ORIGIN, R13, 800.0)
    path = Path(pts)
    along, cross = _signed(ORIGIN, R13, *pts[-1])
    assert abs(cross) * 1852 < 0.5 and abs(path.end_heading() - R13.heading_deg) < 0.5
    assert min(_signed(ORIGIN, R13, *p)[0] * 1852 for p in pts) == pytest.approx(43.0, abs=1.0)  # the turn's far side
    assert max(abs(_signed(ORIGIN, R13, *p)[1]) * 1852 for p in pts) < 22.5  # never off a 45 m runway


def test_tower_sends_a_cleared_arrival_of_ours_around_when_the_runway_gets_occupied():
    import random

    from atc.chatter import RadioBus
    from atc.own import catalog, schedule
    from atc.own.injector import FakeInjector
    from atc.own.manager import OwnTraffic

    from tests.test_own_traffic import _world

    bus = RadioBus()
    mgr = OwnTraffic(_world(), None, FakeInjector(), bus=bus, catalog=catalog.ModelCatalog(), community=None,
                     wind=lambda a: (124.0, 8.0), rng=random.Random(1))
    mgr.spawn_arrival(ORIGIN, 0.0, schedule.Departure("ARG", "1234", "B738"))
    p = mgr.pilots["ARG1234"]
    away = OwnState(-34.50, -58.30, 18, 0, 0, 0, True, 118.85)
    t = 0.0
    while p.final_nm > 2.0 and t < 600:
        mgr.step(t, away, [])
        t += 0.1
    assert p.landing_cleared
    on_rwy = OwnState(R13.lat, R13.lon, 18, 0, 5.0, 124.0, True, 118.85)  # the user lines up after all
    while p.state == "final" and t < 700:
        mgr.step(t, on_rwy, [])
        t += 0.1
    assert p.state == "go_around"
    said = [ex.lines[0][1] for ex in bus.queue]
    assert any("go around, I say again, go around, traffic on the runway" in x for x in said)
    own.publish({})
