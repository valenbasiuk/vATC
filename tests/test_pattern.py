"""VFR traffic pattern at a towered airport, all by code (pattern.py): join / straight-in, sequence on downwind,
landing / touch and go clearances, back into the circuit, turn-outs, leaving the zone. No LLM call anywhere."""

import math
from pathlib import Path

from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan
from atc.main import _Callbacks, handle
from atc.readback import check_readback
from atc.runway import runway_in_use
from atc.sequence import threshold
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import load_world

ROOT = Path(__file__).resolve().parents[1]
CS = "Lima Victor Alfa Bravo Charlie"
SFO_CS = "November one two three Alfa Bravo"


class _Quiet(PrintTTS):
    def say(self, text):
        pass


class _NoLLM:
    """Every turn here is code-owned: any model call is a failure."""

    calls = 0

    def complete(self, messages):
        _NoLLM.calls += 1
        return "LLM"


class Circuit:
    def __init__(self, icao: str, callsign: str = "LVABC", wind=(310, 10), plan=None):
        self.world = load_world(icao, ROOT / "airports", plan, use_navdb=False)
        self.apt = self.world.get(icao)
        self.sim = FakeSim(self.apt, callsign=callsign)
        twr = next(f.mhz for f in self.apt.frequencies if f.kind == "TWR")
        self.sim.update(com1_mhz=twr, wind_dir_deg=wind[0], wind_kt=wind[1], qnh_hpa=1015)
        self.session = Session(callsign=callsign, plan=plan)
        self.history: list = []
        self.cb = _Callbacks(self.world, self.sim, _Quiet(), self.history, self.session)
        self.now = 0.0
        self.rwy = runway_in_use(self.apt, wind[0], wind[1], use="arrival")
        _NoLLM.calls = 0

    def say(self, text: str):
        r = handle(self.apt, self.sim, _NoLLM(), _Quiet(), self.history, text, session=self.session, world=self.world)
        self.cb.after_turn(r is not None)
        return r

    def at(self, along: float, cross: float, hdg_off: float, agl: float, ground: bool = False, gs: float = 90.0):
        """Own aircraft `along` NM past the threshold (negative: on the approach side), `cross` NM to the right of
        the centerline, heading = runway heading + hdg_off."""
        tlat, tlon = threshold(self.apt, self.rwy)
        h = math.radians(self.rwy.heading_deg)
        e = along * math.sin(h) + cross * math.cos(h)
        n = along * math.cos(h) - cross * math.sin(h)
        self.sim.update(lat=tlat + n / 60, lon=tlon + e / (60 * math.cos(math.radians(tlat))),
                        heading_deg=(self.rwy.heading_deg + hdg_off) % 360, alt_agl_ft=agl,
                        alt_msl_ft=self.apt.elevation_ft + agl, on_ground=ground, gs_kt=gs)

    def tick(self, n: int = 1) -> list[str]:
        out = []
        for _ in range(n):
            self.now += 40.0  # slow clock: a circuit takes minutes, the landing detector wants > 120 s airborne
            r = self.cb.tick(self.now)
            if r:
                out.append(r)
        return out

    def downwind(self):
        self.at(1.0, -1.0, 180, 1000)  # left downwind: left of the landing direction, flying the other way


def test_sabe_inbound_join_and_silent_readback():
    c = Circuit("SABE")
    c.at(-2, 8, -90, 1500)  # off to the side, not lined up
    c.tick()
    r = c.say("Aeroparque Tower, LV-ABC, Cessna 172, 10 miles north, 1500 feet, inbound for landing")
    assert r == (f"{CS}, Aeroparque Tower, join left downwind runway three one, wind three two zero degrees one zero "
                 "knots, QNH one zero one five, report downwind.")
    assert c.say("join left downwind runway 31, QNH 1015, report downwind, LV-ABC") is None
    assert c.session.in_circuit and _NoLLM.calls == 0


def test_straight_in_when_lined_up():
    c = Circuit("SABE")
    c.at(-8, 0.3, 0, 1500)
    r = c.say("Aeroparque Tower, LV-ABC, 8 miles southeast, inbound for landing")
    assert r.startswith(f"{CS}, Aeroparque Tower, make straight-in approach runway three one")
    assert r.endswith("report final.")


def test_downwind_number_two_behind_ai_on_final():
    c = Circuit("SABE")
    c.at(-2, 8, -90, 1500)
    c.say("Aeroparque Tower, LV-ABC, inbound for landing")
    c.downwind()
    c.sim.add_on_final(3, "ARG1234")
    r = c.say("LV-ABC left downwind runway 31")
    assert r == f"{CS}, number two, traffic to follow on three mile final, report final."
    assert _NoLLM.calls == 0


def test_base_full_stop_cleared_to_land_then_vacate():
    c = Circuit("SABE")
    c.at(-2, 8, -90, 1500)
    c.say("Aeroparque Tower, LV-ABC, inbound full stop")
    c.downwind()
    c.tick()
    assert c.say("LV-ABC downwind") == f"{CS}, number one, report final."
    c.at(-1.5, -1.0, 90, 700)
    r = c.say("LV-ABC left base")
    assert r == f"{CS}, wind three two zero degrees one zero knots, runway three one, cleared to land."
    assert c.say("cleared to land runway 31, LV-ABC") is None
    c.at(0.3, 0, 0, 0, ground=True, gs=100)
    c.tick()
    c.at(0.5, 0, 0, 0, ground=True, gs=30)
    said = c.tick()
    assert said and said[0].startswith(f"{CS}, welcome to Aeroparque, vacate")
    assert _NoLLM.calls == 0


def test_touch_and_go_then_back_into_the_circuit():
    c = Circuit("SABE")
    c.at(-2, 8, -90, 1500)
    c.say("Aeroparque Tower, LV-ABC, inbound for touch and go")
    c.at(-1.5, -1.0, 90, 700)
    c.tick()
    assert c.say("LV-ABC left base") == (f"{CS}, wind three two zero degrees one zero knots, runway three one, "
                                         "cleared touch and go.")
    c.at(0.3, 0, 0, 0, ground=True, gs=60)  # touch...
    c.tick()
    c.at(1.5, 0, 0, 500, gs=80)  # ...and go
    assert c.tick(2) == [f"{CS}, report left downwind."]
    assert not c.session.landing_cleared  # the next final needs a new clearance
    assert not any("vacate" in a for _, a in c.history)
    assert not any("contact" in a for _, a in c.history)  # no handoff to Departure from the circuit


def test_short_final_in_the_circuit_cleared_without_a_call():
    c = Circuit("SABE")
    c.at(-2, 8, -90, 1500)
    c.say("Aeroparque Tower, LV-ABC, inbound for landing")
    c.at(-2.0, 0, 0, 600)
    assert c.tick() == [f"{CS}, wind three two zero degrees one zero knots, runway three one, cleared to land."]


def test_short_final_not_cleared_while_the_runway_is_occupied():
    c = Circuit("SABE")
    c.at(-2, 8, -90, 1500)
    c.say("Aeroparque Tower, LV-ABC, inbound for landing")
    c.sim.add_on_runway("ARG1234")
    c.at(-2.0, 0, 0, 600)
    assert c.tick() == []
    assert not c.session.landing_cleared


def test_departure_with_turnout_and_leaving_the_zone():
    c = Circuit("SABE")
    c.at(-0.05, 0, 0, 0, ground=True, gs=0)
    c.tick()
    r = c.say("Aeroparque Tower, LV-ABC, holding point runway 31, ready for departure, request left turnout")
    assert r == (f"{CS}, Aeroparque Tower, left turn approved, wind three two zero degrees one zero knots, "
                 "runway three one, cleared for takeoff.")
    c.at(1.0, 0, 0, 400, gs=80)
    c.tick()
    c.at(4.0, -4.0, -60, 1500, gs=100)
    assert c.tick() == []  # still inside the zone
    c.at(6.0, -7.0, -60, 1500, gs=100)
    said = c.tick()
    assert said == [f"{CS}, frequency change approved, good day."]


def test_departure_staying_in_the_circuit():
    c = Circuit("SABE")
    c.at(-0.05, 0, 0, 0, ground=True, gs=0)
    r = c.say("Aeroparque Tower, LV-ABC, ready for departure, request circuits")
    assert r.endswith("runway three one, cleared for takeoff, report downwind.")
    assert c.session.in_circuit


def test_ksfo_faa_enter_downwind_and_cleared_on_downwind():
    c = Circuit("KSFO", callsign="N123AB", wind=(290, 10))
    assert c.rwy.ident == "28L"  # west plan: land 28L/28R (runway_configs)
    c.at(-2, 8, -90, 1500)
    r = c.say("San Francisco Tower, N123AB, Cessna 172, 10 miles south, inbound for landing")
    assert r == (f"{SFO_CS}, San Francisco Tower, enter left downwind runway two eight left, altimeter two niner "
                 "niner seven, report midfield downwind.")
    assert c.say("enter left downwind 28L, 2997, report midfield, N123AB") is None
    c.at(1.0, -1.0, 180, 1000)
    r = c.say("N123AB midfield left downwind 28L")
    assert r == f"{SFO_CS}, number one, runway two eight left, cleared to land."  # FAA clears early
    assert _NoLLM.calls == 0


def test_ksfo_touch_and_go_is_cleared_for_the_option():
    c = Circuit("KSFO", callsign="N123AB", wind=(290, 10))
    c.at(-2, 8, -90, 1500)
    c.say("San Francisco Tower, N123AB, 10 miles south, for touch and go")
    c.at(1.0, -1.0, 180, 1000)
    assert c.say("N123AB midfield left downwind").endswith("cleared for the option.")


def test_us_parallel_runways_default_to_outside_patterns():
    from atc.airports.gen import default_pattern

    assert [default_pattern(i, True) for i in ("28L", "28R", "1C", "31")] == ["left", "right", "left", "left"]
    assert default_pattern("31", False) is None  # outside the US: a hand field


def test_readback_of_a_side_runway():
    atc = f"{SFO_CS}, enter right downwind runway two eight right, altimeter two niner niner seven, report midfield."
    assert check_readback(atc, "right downwind 28R, 2997, N123AB").status == "correct"
    assert check_readback(atc, "right downwind two eight right, 2997, N123AB").status == "correct"
    assert check_readback(atc, "right downwind 28L, 2997, N123AB").status == "incomplete"


def test_ifr_flight_is_not_a_pattern_flight():
    plan = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="B738", origin="SAAR", destination="SABE",
                      destination_name="Aeroparque", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=10000, planned_runway=None)
    c = Circuit("SABE", callsign="MAR4133", plan=plan)
    c.at(-2, 8, -90, 1500)
    r = c.say("Aeroparque Tower, Martinair 4133, inbound for landing")
    assert r is None or "downwind" not in r
    assert not c.session.in_circuit


def test_zone_transit_is_left_to_the_model():
    c = Circuit("SABE")
    c.at(-8, 6, 0, 2000)
    c.say("Aeroparque Tower, LV-ABC, 10 miles south, 2500 feet, request to transit the zone northbound")
    assert _NoLLM.calls == 1 and not c.session.in_circuit


def test_new_lap_needs_a_new_clearance_even_if_the_touchdown_was_missed():
    c = Circuit("SABE")
    c.sim.place_on_leg(c.apt, "out")
    c.say("Aeroparque Tower, LV-ABC, 8 miles west, inbound for touch and go")
    c.sim.place_on_leg(c.apt, "base")
    assert c.say("LV-ABC left base").endswith("cleared touch and go.")
    c.sim.place_on_leg(c.apt, "downwind")  # around again (the sim never reported the wheels on the ground)
    assert c.say("LV-ABC downwind, full stop") == f"{CS}, number one, report final."
    c.sim.place_on_leg(c.apt, "final", 2)
    assert c.tick() == [f"{CS}, wind three two zero degrees one zero knots, runway three one, cleared to land."]


def test_departure_to_a_compass_direction_gets_the_turn():
    c = Circuit("SABE")  # runway 31 (true 310... magnetic 320 with VAR 10 W)
    c.at(-0.05, 0, 0, 0, ground=True, gs=0)
    r = c.say("Aeroparque Tower, LV-ABC, ready for departure, request departure to the north")
    assert r.startswith(f"{CS}, Aeroparque Tower, after departure, right turn northbound approved, wind")
    assert c.session.vfr_departure
    c2 = Circuit("SABE")
    c2.at(-0.05, 0, 0, 0, ground=True, gs=0)
    r = c2.say("Aeroparque Tower, LV-ABC, ready for departure, southbound departure")
    assert ", after departure, left turn southbound approved, " in r
    c3 = Circuit("SABE")
    c3.at(-0.05, 0, 0, 0, ground=True, gs=0)
    assert ", northwest" + "bound departure approved, " in c3.say("Aeroparque Tower, LV-ABC, ready, northwestbound "
                                                                   "departure")
