"""Airports that load themselves (spawn anywhere) and US (FAA) phraseology: group-form callsigns, altitudes below
FL180 in thousands, "wind 280 at 12", "then as filed"."""

from pathlib import Path

from atc import phrase
from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan
from atc.main import handle
from atc.models import Traffic
from atc.readback import check_readback
from atc.session import Session, callsign_from_sim
from atc.sim.fake import FakeSim
from atc.world import load_world

ROOT = Path(__file__).resolve().parents[1]


class _Quiet(PrintTTS):
    def say(self, text):
        pass


def test_group_form_numbers():
    g = phrase.group_number
    assert [g("5"), g("52"), g("436"), g("286"), g("4133"), g("100"), g("1200"), g("205"), g("1005"), g("2000")] == [
        "five", "fifty-two", "four thirty-six", "two eighty-six", "forty-one thirty-three", "one hundred",
        "twelve hundred", "two zero five", "ten zero five", "two thousand"]
    assert phrase.callsign("United", "UAL436", faa=True) == "United four thirty-six"
    assert phrase.callsign("United", "UAL436") == "United four three six"  # ICAO: digit by digit


def test_levels_follow_the_transition_altitude():
    class Apt:
        def __init__(self, faa, ta):
            self.faa, self.trans_alt_ft = faa, ta

    us, ar = Apt(True, 18000), Apt(False, 3000)
    assert phrase.level(11000, us) == "one one thousand"
    assert phrase.level(18000, us) == "flight level one eight zero"
    assert phrase.climb(11000, us) == "climb and maintain one one thousand"
    assert phrase.level(5000, ar) == "flight level five zero"  # above Argentina's 3000 ft TA
    assert phrase.descend(3000, ar) == "descend to three thousand feet"
    assert phrase.level(20000) == "flight level two zero zero"  # unknown TA: the old 10000 ft rule
    assert phrase.wind(280, 12, faa=True) == "wind two eight zero at one two"


def test_faa_altitude_readback_without_feet():
    atc = "United four thirty-six, climb and maintain one one thousand."
    assert check_readback(atc, "climb and maintain one one thousand, United 436").status == "correct"
    assert check_readback(atc, "climb and maintain one zero thousand, United 436").status == "incomplete"


def _ksfo_world(plan=None):
    world = load_world("KSFO", ROOT / "airports", plan, use_navdb=False)
    return world, world.get("KSFO")


def test_ksfo_ground_uses_group_form_and_faa_taxi():
    world, ksfo = _ksfo_world()
    sim = FakeSim(ksfo, callsign="UAL436")
    sim.update(com1_mhz=121.8, wind_dir_deg=290, wind_kt=12, qnh_hpa=1013.2)
    s = Session(callsign="UAL436")
    r = handle(ksfo, sim, None, _Quiet(), [], "San Francisco Ground, United 436, at the gate, request taxi",
               session=s, world=world)
    assert r.startswith("United four thirty-six, San Francisco Ground, runway two eight")
    assert ", taxi" in r and "altimeter two niner niner two" in r


def test_faa_clearance_says_then_as_filed_and_altitudes():
    plan = FlightPlan(callsign="UAL436", rules="I", aircraft_type="B738", origin="KSFO", destination="KSFO",
                      destination_name="San Francisco", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=11000, planned_runway="28R")
    world, ksfo = _ksfo_world(plan)
    sim = FakeSim(ksfo, callsign="UAL436")
    sim.update(com1_mhz=118.2)
    s = Session(callsign="UAL436", plan=plan)
    r = handle(ksfo, sim, None, _Quiet(), [], "San Francisco Clearance, United 436, request IFR clearance",
               session=s, world=world)
    assert "then as filed" in r and "expect one one thousand" in r and "point" in r
    r2 = handle(ksfo, sim, None, _Quiet(), [(None, r)],
                f"cleared to San Francisco, then as filed, expect one one thousand, departure 120.9, squawk "
                f"{' '.join(plan.squawk)}, United 436", session=s, world=world)
    assert "readback correct" in r2


def test_spawned_at_ksfo_with_an_argentine_plan_loads_ksfo():
    plan = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="F100", origin="SABE", destination="SAAR",
                      destination_name="Rosario", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=20000, planned_runway="31")
    world = load_world("SABE", ROOT / "airports", plan, use_navdb=False)
    sim = FakeSim(world.get("SABE"), callsign="MAR4133")
    sim.update(lat=37.6155, lon=-122.3850, com1_mhz=118.0, on_ground=True)  # a KSFO gate, a frequency nobody has
    assert world.pick(sim.own()) is None  # nobody on 118.0...
    assert world.get("KSFO") is not None  # ...but the airport we are at got loaded
    # and its area control is not Ezeiza's (airspace files only answer near their FIR)
    sim.update(on_ground=False, alt_msl_ft=12000, alt_agl_ft=12000, com1_mhz=135.5)
    assert world.pick(sim.own()) is None
    assert world.control(sim.own(), world.get("KSFO")) is None


def test_callsign_from_the_sims_atc_settings():
    assert callsign_from_sim({"airline": "Speedbird", "flight_number": "286", "atc_id": "G-ABCD"}) == \
        ("BAW286", "Speedbird")
    assert callsign_from_sim({"airline": "", "flight_number": "", "atc_id": "LV-KJQ"}) == ("LV-KJQ", None)
    assert callsign_from_sim(None) == (None, None)


def test_ai_chatter_in_the_us_uses_group_form():
    from atc.chatter import ai_callsign

    t = Traffic("N1", 0, 0, 0, 0, 0, True, airline="United", flight_number="436")
    assert ai_callsign(t, faa=True) == "United four thirty-six"
    assert ai_callsign(t) == "United four three six"
