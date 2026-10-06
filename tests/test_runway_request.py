"""Runway requests: "request runway 13 for departure" / "request ILS 13" approved when the wind allows it, then used
by every later decision (taxi, Tower, vectors); "unable, tailwind ..." when it doesn't."""

from pathlib import Path

from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan
from atc.main import handle
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import load_world

ROOT = Path(__file__).resolve().parents[1]
CS = "Lima Victor Alfa Bravo Charlie"


class _Quiet(PrintTTS):
    def say(self, text):
        pass


def _sabe(wind=(310, 8)):
    world = load_world("SABE", ROOT / "airports", None, use_navdb=False)
    sabe = world.get("SABE")
    sim = FakeSim(sabe, callsign="LVABC")
    sim.update(com1_mhz=121.9, wind_dir_deg=wind[0], wind_kt=wind[1], qnh_hpa=1015, lat=sabe.lat + 0.004,
               lon=sabe.lon + 0.004)
    return world, sabe, sim, Session(callsign="LVABC")


def _say(world, apt, sim, s, text, history=None):
    return handle(apt, sim, None, _Quiet(), history if history is not None else [], text, session=s, world=world)


def test_departure_runway_request_approved_then_taxi_uses_it():
    world, sabe, sim, s = _sabe()  # wind 310/8: runway 31 in use, 13 has 8 kt of tailwind (allowed up to 10)
    assert _say(world, sabe, sim, s, "Aeroparque Ground, LV-ABC, request runway 13 for departure") == \
        f"{CS}, Aeroparque Ground, runway one three approved."
    r = _say(world, sabe, sim, s, "LV-ABC, request taxi")
    assert r.startswith(f"{CS}, taxi to holding point runway one three")


def test_request_inside_the_taxi_call_is_silent_and_used():
    world, sabe, sim, s = _sabe()
    r = _say(world, sabe, sim, s, "Aeroparque Ground, LV-ABC, request taxi, runway one three please")
    assert r.startswith(f"{CS}, Aeroparque Ground, taxi to holding point runway one three")  # station said once


def test_unable_with_too_much_tailwind():
    world, sabe, sim, s = _sabe(wind=(310, 15))
    assert _say(world, sabe, sim, s, "Aeroparque Ground, LV-ABC, request runway 13 for departure") == \
        f"{CS}, Aeroparque Ground, unable runway one three, tailwind one five knots, runway three one in use."
    assert _say(world, sabe, sim, s, "LV-ABC, request taxi").startswith(f"{CS}, taxi to holding point runway three one")


def test_arrival_approach_request_resets_the_vectors():
    plan = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="F100", origin="SAAR", destination="SABE",
                      destination_name="Aeroparque", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=20000, planned_runway=None, dest_runway="31")
    world = load_world("SABE", ROOT / "airports", plan, use_navdb=False)
    sabe = world.get("SABE")
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(com1_mhz=120.6, wind_dir_deg=310, wind_kt=5, lat=sabe.lat + 0.3, lon=sabe.lon - 0.2,
               alt_msl_ft=6000, alt_agl_ft=5980, on_ground=False, heading_deg=120, gs_kt=220)
    s = Session(callsign="MAR4133", plan=plan, telephony="Martinair", clearance="confirmed", departed_from="SAAR")
    s.contacted.add("SABE:approach")
    s.vectors_given = True
    r = _say(world, sabe, sim, s, "Martinair 4133, request runway 13")
    assert r == "Martinair four one three three, expect runway one three."  # no approach types in the test YAML
    assert not s.vectors_given and s.runway_requests == {"SABE:arrival": "13"}
    from atc.enroute import _arrival_runway

    assert _arrival_runway(sabe, sim.own(), s).ident == "13"


def test_say_again_repeats_the_last_transmission_word_for_word():
    world, sabe, sim, s = _sabe()
    history = []
    first = _say(world, sabe, sim, s, "Aeroparque Ground, LV-ABC, request taxi", history)
    r = _say(world, sabe, sim, s, "LV-ABC, say again", history)
    body = first.split("Aeroparque Ground, ", 1)[1]
    assert r == f"{CS}, I say again, {body}"
    # and again: no "I say again, I say again"
    assert _say(world, sabe, sim, s, "say again please, LV-ABC", history) == r


def test_radio_check_time_check_and_qnh_wind_questions_are_code():
    world, sabe, sim, s = _sabe()
    sim.update(zulu_s=14 * 3600 + 35 * 60 + 20)
    assert _say(world, sabe, sim, s, "Aeroparque Ground, LV-ABC, radio check") == \
        f"{CS}, Aeroparque Ground, read you five."
    assert _say(world, sabe, sim, s, "LV-ABC, request time check") == f"{CS}, time one four three five."
    assert _say(world, sabe, sim, s, "LV-ABC, say QNH") == f"{CS}, QNH one zero one five."
    assert _say(world, sabe, sim, s, "LV-ABC, say the wind and QNH please") == \
        f"{CS}, wind three two zero degrees eight knots, QNH one zero one five."  # magnetic (VAR 10 W)
    # with another request in it: not this simple answer
    assert "QNH one zero one five." != _say(world, sabe, sim, s, "LV-ABC, request taxi, say QNH")


def test_model_context_knows_what_code_decided():
    world, sabe, sim, s = _setup_with_request()

    class Rec:
        seen = ""

        def complete(self, messages):
            Rec.seen = "\n".join(m["content"] for m in messages)
            return "Lima Victor Alfa Bravo Charlie, standby."

    handle(sabe, sim, Rec(), _Quiet(), [], "LV-ABC, how long is the taxi to the runway?", session=s, world=world)
    assert "Runway 13 for departure at SABE was approved on the pilot's request." in Rec.seen
    assert "Runway in use (computed from wind, treat as fact): 13" in Rec.seen


def _setup_with_request():
    world, sabe, sim, s = _sabe()
    _say(world, sabe, sim, s, "Aeroparque Ground, LV-ABC, request runway 13 for departure")
    return world, sabe, sim, s
