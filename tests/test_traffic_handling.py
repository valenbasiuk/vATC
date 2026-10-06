"""AI traffic on the frequency: the tracker sees what the AI does, chatter says the matching ATC instruction and
the AI's readback, and the radio bus keeps it from stepping on the user."""

from pathlib import Path

import pytest

import atc.chatter as chatter
from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.chatter import Exchange, RadioBus, ai_callsign
from atc.main import _Callbacks, handle
from atc.models import Traffic
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.taxi import load_network
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]
ARG = "Argentina one two three four"
INFO = dict(airline="Aerolineas Argentinas", flight_number="1234", type="A320")


@pytest.fixture(autouse=True)
def _telephony(monkeypatch):
    monkeypatch.setattr(chatter, "_airline_telephony", lambda: {"aerolineas argentinas": "Argentina",
                                                                 "ARG": "Argentina"})


class _Quiet(PrintTTS):
    def say(self, text):
        pass

    def say_as(self, text, who="ATC", voice=None):
        pass


def _setup(freq=118.85):
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    world = World([sabe], taxi={"SABE": load_network(ROOT / "airports", "SABE")})
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(com1_mhz=freq, wind_dir_deg=300.0, wind_kt=10.0, qnh_hpa=1015.0)
    s = Session(callsign="MAR4133", telephony="Martinair")
    return sabe, world, sim, s, _Callbacks(world, sim, _Quiet(), [], s)


def _run(cb, until, step=1.0, start=0.0):
    said = []
    t = start
    while t <= until:
        cb.last_chatter = []
        cb.tick(now=t)
        if cb.last_chatter:
            said.append((t, cb.last_chatter))
        t += step
    return said


def test_ai_departure_is_talked_through_on_tower():
    _, _, sim, _, cb = _setup()
    sim.spawn_departure("ARG1234", "31", 0.0, **INFO)
    said = _run(cb, 80)
    atc = [lines[0][1] for _, lines in said]
    assert atc[0] == f"{ARG}, runway three one, line up and wait."
    assert atc[1] == f"{ARG}, wind three one zero degrees one zero knots, runway three one, cleared for takeoff."
    assert atc[2] == f"{ARG}, contact departure one two zero decimal six, good day."
    assert said[1][1][1] == (ARG, f"Cleared for takeoff runway three one, {ARG}.")  # the AI reads back


def test_ai_on_final_with_us_on_the_runway_is_told_to_continue_then_cleared():
    sabe, _, sim, _, cb = _setup()  # the fake sim starts us at the ARP, on the runway
    sim.spawn_arrival("ARG1234", 8.0, "31", 0.0, **INFO)
    said = _run(cb, 80, step=2.0)
    assert [lines[0][1] for _, lines in said] == [f"{ARG}, continue approach, traffic on the runway."]  # once
    sim.update(lat=sabe.lat + 0.004, lon=sabe.lon + 0.004)  # we vacate
    said = _run(cb, 120, step=2.0, start=82.0)
    assert said[0][1][0][1] == f"{ARG}, wind three one zero degrees one zero knots, runway three one, cleared to land."


def test_ai_arrival_cleared_to_land_then_ground():
    sabe, _, sim, _, cb = _setup()
    sim.update(lat=sabe.lat + 0.004, lon=sabe.lon + 0.004)  # us on the apron (the fake sim starts at the ARP,
    sim.spawn_arrival("ARG1234", 8.0, "31", 0.0, **INFO)    # which is on the runway: the AI would go around)
    said = _run(cb, 300, step=2.0)
    atc = [lines[0][1] for _, lines in said]
    assert atc[0] == f"{ARG}, wind three one zero degrees one zero knots, runway three one, cleared to land."
    assert said[0][0] > 40  # said at about 6 NM (from 8 NM at 140 kt), not when first seen
    assert f"{ARG}, contact ground one two one decimal niner." in atc


def test_tower_chatter_is_not_heard_on_ground_and_ground_chatter_is():
    _, _, sim, _, cb = _setup(freq=121.9)
    sim.spawn_departure("ARG1234", "31", 0.0, **INFO)
    assert _run(cb, 80) == []  # line up / takeoff are Tower's
    sim2 = cb.sim
    sim2.clear_traffic()
    sim2._scripts.clear()
    sim2.spawn_arrival("ARG2000", 8.0, "31", 100.0, airline="Aerolineas Argentinas", flight_number="2000")
    said = _run(cb, 420, step=2.0, start=100.0)
    assert any("taxi to the apron" in lines[0][1] for _, lines in said)


def test_user_gets_their_turn_first():
    sabe, world, sim, s, cb = _setup()
    sim.spawn_departure("ARG1234", "31", 0.0, **INFO)
    cb.bus.heard(5.5, to_user=True)  # ATC just talked to the user: their readback comes first
    said = _run(cb, 30)
    assert said and said[0][0] >= 11.5  # nothing during the 6 s readback window


def test_bus_rules():
    bus = RadioBus()
    a = Exchange("ARG1", [("ATC", "line up", None)], 0.0, "tower", 0)
    b = Exchange("ARG1", [("ATC", "cleared for takeoff", None)], 2.0, "tower", 0)
    bus.offer(a)
    bus.offer(b)  # same aircraft: the newer instruction replaces the older one
    assert len(bus.queue) == 1 and bus.queue[0] is b
    assert bus.next_due(3.0, "ground") is None  # wrong frequency
    bus.heard(3.0)
    assert bus.next_due(5.0, "tower") is None  # gap after a transmission
    assert bus.next_due(7.5, "tower") is b
    bus.offer(Exchange("ARG2", [("ATC", "x", None)], 10.0, "tower"))
    assert bus.next_due(40.0, "tower") is None  # stale: dropped


def test_ai_callsigns():
    assert ai_callsign(Traffic("LV-ABC", 0, 0, 0, 0, 0, True, airline="Aerolineas Argentinas",
                               flight_number="1234")) == ARG
    assert ai_callsign(Traffic("ARG1302", 0, 0, 0, 0, 0, True)) == "Argentina one three zero two"
    assert ai_callsign(Traffic("LV-GTU", 0, 0, 0, 0, 0, True)) == "Lima Victor Golf Tango Uniform"


def test_user_sequenced_behind_a_typed_ai():
    sabe, world, sim, s, _ = _setup()
    sim.add_on_final(2.0, "ARG1234", "31")
    sim._traffic[-1].type = "A320"
    r = handle(sabe, sim, None, _Quiet(), [], "Aeroparque Tower, Martinair 4133, holding point 31, ready for departure",
               session=s, world=world)
    assert r == "Martinair four one three three, Aeroparque Tower, hold position, Airbus three twenty on two mile final."


def test_approach_chatter_for_an_ai_arrival():
    sabe, _, sim, _, cb = _setup(freq=120.6)  # tuned to Aeroparque Approach (on the apron, listening)
    sim.update(lat=sabe.lat + 0.004, lon=sabe.lon + 0.004)
    sim.spawn_arrival("ARG1234", 20.0, "31", 0.0, **INFO)  # comes into range (15 NM) a couple of minutes later
    said = _run(cb, 520, step=2.0)
    texts = [[text for _, text in lines] for _, lines in said]
    # no approach types in the test YAML (the sim's db adds them): a visual approach is expected
    assert texts[0] == [f"{ARG}, descend to three thousand feet, expect visual approach runway three one.",
                        f"Descend to three thousand feet, expect visual approach runway three one, {ARG}."]
    assert texts[1] == [f"{ARG}, contact tower one one eight decimal eight five, good day.",
                        f"Tower one one eight decimal eight five, {ARG}."]
    assert not any("cleared to land" in t for x in texts for t in x)  # that is Tower's, not heard here


def test_departure_checks_in_with_departure():
    sabe, _, sim, _, cb = _setup(freq=120.6)
    sim.update(lat=sabe.lat + 0.004, lon=sabe.lon + 0.004)
    sim.spawn_departure("ARG1234", "31", 0.0, **INFO)
    said = _run(cb, 200)
    assert len(said) == 1
    pilot, atc, readback = [text for _, text in said[0][1]]
    assert pilot.startswith(f"Aeroparque Departure, {ARG}, passing two thousand") and pilot.endswith("climbing.")
    assert atc == f"{ARG}, Aeroparque Departure, radar contact, climb to flight level one zero zero."
    assert readback == f"Climb to flight level one zero zero, {ARG}."


def test_tower_handoff_and_departure_check_in_dont_replace_each_other():
    bus = RadioBus()
    bus.offer(Exchange("ARG1", [("ATC", "contact departure", None)], 0.0, "tower"))
    bus.offer(Exchange("ARG1", [("ARG1", "Departure, ARG1, passing 2000", None)], 1.0, "approach"))
    assert {q.role for q in bus.queue} == {"tower", "approach"}
