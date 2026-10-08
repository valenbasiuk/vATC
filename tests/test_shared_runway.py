"""Improvement plan block 3 (2026-10-07): one runway controller for the user and our own traffic — the departure
queue ("number two for departure"), wake turbulence intervals and cautions, "cleared for immediate takeoff", our
departures waiting for the user, and pushes held back when the apron is busy."""

import random
import time

from atc import departures, own
from atc.audio.tts import PrintTTS
from atc.main import _Callbacks, handle
from atc.models import OwnState
from atc.own.airport import _runway_point
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import World
from tests.test_flight import ORIGIN

CS = "Martinair four one three three"
R13 = ORIGIN.runways[0]


class _Quiet(PrintTTS):
    def say(self, text):
        pass


def _at_hold(com=118.85, **kw):
    sim = FakeSim(ORIGIN, callsign="MAR4133")
    lat, lon = _runway_point(ORIGIN, R13, 0.0, 90.0)  # holding point at the 13 end
    sim.update(lat=lat, lon=lon, com1_mhz=com, wind_dir_deg=124.0, wind_kt=8.0, qnh_hpa=1015.0, heading_deg=34.0,
               aircraft_type="B738", **kw)
    return sim


def _session():
    return Session(callsign="MAR4133", telephony="Martinair")


# --- the module ------------------------------------------------------------------------------------------------

def test_queue_order_wake_intervals_and_categories():
    departures.ready("SATS", "ARG1", "13", now=0.0)
    departures.ready("SATS", departures.USER, "13", now=5.0)
    departures.ready("SATS", "ARG1", "13", now=9.0)  # asking again keeps its place
    assert departures.ahead("SATS", departures.USER, "13", now=10.0) == ["ARG1"]
    departures.airborne("SATS", "13", "ARG1", "B77W", now=20.0)
    assert departures.ahead("SATS", departures.USER, "13", now=21.0) == []
    assert departures.gap_left("SATS", "13", "B738", now=80.0) == 60.0  # 2 minutes behind a heavy
    assert departures.wake_caution("SATS", "13", "B738", now=80.0) == "caution wake turbulence"
    assert departures.wake_caution("SATS", "13", "B77W", now=80.0) is None
    assert [departures.wake(t) for t in ("A388", "B744", "B738", "C172", "C17")] == ["J", "H", "M", "L", "H"]


# --- the user ----------------------------------------------------------------------------------------------------

def test_user_is_number_two_behind_our_departure_then_cleared_after_its_interval():
    departures.ready("SATS", "ARG1216", "13")  # ours reached the holding point first
    sim, s = _at_hold(), _session()
    cb = _Callbacks(World([ORIGIN]), sim, _Quiet(), [], s)
    r = handle(ORIGIN, sim, None, _Quiet(), cb.history, "Testa Tower, Martinair 4133, ready for departure", session=s)
    assert r == f"{CS}, Testa Tower, number two for departure."
    assert cb.tick(now=1.0) is None
    departures.airborne("SATS", "13", "ARG1216", "B738", now=time.monotonic() - 30.0)  # gone 30 s ago
    assert cb.tick(now=2.0) is None  # one minute between departures
    departures.airborne("SATS", "13", "ARG1216", "B738", now=time.monotonic() - 61.0)
    assert cb.tick(now=3.0) == f"{CS}, wind one two zero degrees eight knots, runway one three, cleared for takeoff."


def test_behind_a_heavy_line_up_and_wait_then_caution_wake_turbulence():
    departures.airborne("SATS", "13", "BAW244", "B77W", now=time.monotonic() - 30.0)
    sim, s = _at_hold(), _session()
    cb = _Callbacks(World([ORIGIN]), sim, _Quiet(), [], s)
    r = handle(ORIGIN, sim, None, _Quiet(), cb.history, "Testa Tower, Martinair 4133, ready for departure", session=s)
    assert r == f"{CS}, Testa Tower, line up and wait runway one three."
    departures.airborne("SATS", "13", "BAW244", "B77W", now=time.monotonic() - 125.0)
    assert cb.tick(now=1.0) == (f"{CS}, wind one two zero degrees eight knots, runway one three, cleared for "
                                "takeoff, caution wake turbulence.")


def test_arrival_at_four_miles_cleared_for_immediate_takeoff():
    sim, s = _at_hold(), _session()
    sim.add_on_final(4.0, "ARG1234", "13")
    r = handle(ORIGIN, sim, None, _Quiet(), [], "Testa Tower, Martinair 4133, ready for departure", session=s)
    assert r == f"{CS}, Testa Tower, wind one two zero degrees eight knots, runway one three, cleared for immediate takeoff."


def test_number_three_is_told_the_expected_delay():
    departures.ready("SATS", "ARG1", "13", now=time.monotonic() - 60)
    departures.ready("SATS", "ARG2", "13", now=time.monotonic() - 30)
    sim, s = _at_hold(), _session()
    r = handle(ORIGIN, sim, None, _Quiet(), [], "Testa Tower, Martinair 4133, ready for departure", session=s)
    assert r == f"{CS}, Testa Tower, number three for departure, expect departure in about three minutes."


# --- our departures wait for the user -----------------------------------------------------------------------------

def test_our_departure_waits_while_the_user_is_ahead_at_the_holding_point():
    from atc.own import catalog, schedule
    from atc.own.injector import FakeInjector
    from atc.own.manager import OwnTraffic
    from tests.test_own_traffic import APT, _world

    mgr = OwnTraffic(_world(), None, FakeInjector(), catalog=catalog.ModelCatalog(), community=None,
                     wind=lambda a: (130.0, 8.0), rng=random.Random(4))
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"))
    p = mgr.pilots["ARG1216"]
    from atc.own.motion import moved

    hold = mgr.world.taxi["SATS"].nodes[100]  # ours: 90 m right of the 13 end; the user across, 90 m left
    user = OwnState(*moved(*hold, 34.0, 180.0), 18, 0, 0, 214.0, True, 118.85)
    t = 0.0
    while p.state != "holding" and t < 900:
        mgr.step(t, user, [])
        t += 0.1
    departures.ready("SATS", departures.USER, "13", now=time.monotonic() - 100)  # the user reported ready first
    for _ in range(300):
        mgr.step(t, user, [])
        t += 0.1
    assert p.state == "holding"
    departures.gone("SATS", departures.USER)
    for _ in range(300):
        mgr.step(t, user, [])
        t += 0.1
    assert p.state in ("takeoff_radio", "lining_up", "lined_up", "takeoff")
    own.publish({})


# --- pushes held back at a busy time --------------------------------------------------------------------------------

def test_push_waits_when_the_apron_is_busy_and_ground_calls_back():
    own.APRON["SATS"] = 5
    sim, s = _at_hold(com=121.9), _session()
    lat, lon = _runway_point(ORIGIN, R13, 600.0, 400.0)
    sim.update(lat=lat, lon=lon)
    cb = _Callbacks(World([ORIGIN]), sim, _Quiet(), [], s)
    r = handle(ORIGIN, sim, None, _Quiet(), cb.history, "Testa Ground, Martinair 4133, request push and start",
               session=s)
    assert r == f"{CS}, Testa Ground, hold position, expect push and start in two minutes, number three, I'll call you."
    assert cb.tick(now=1.0) is None
    own.APRON["SATS"] = 2  # the apron got quieter
    assert cb.tick(now=2.0) == f"{CS}, push and start approved."
