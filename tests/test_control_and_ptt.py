"""Ezeiza Control (airspace/SAEF.yaml, 135.5 per Valen and VATSIM Argentina) and joystick push-to-talk."""

from pathlib import Path

from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan
from atc.main import _Callbacks, handle
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import load_world

ROOT = Path(__file__).resolve().parents[1]
CS = "Martinair four one three three"
PLAN = FlightPlan(
    callsign="MAR4133", rules="I", aircraft_type="F100", origin="SABE", destination="SAAR",
    destination_name="Islas Malvinas", alternate=None, route="ATOVO4B ATOVO W5 PEDRO DCT ESKON DCT", sid="ATOVO4B",
    sid_transition="ATOVO", cruise_ft=20000, planned_runway="31", dest_runway="02",
)


class _Quiet(PrintTTS):
    def say(self, text):
        pass

    def say_as(self, text, who="ATC", voice=None):
        pass


def _setup():
    world = load_world("SABE", ROOT / "airports", PLAN, use_navdb=False)
    sabe = world.get("SABE")
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(squawk=PLAN.squawk)
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", clearance="confirmed", departed_from="SABE")
    return world, sabe, sim, s


def test_departure_hands_off_to_ezeiza_control():
    world, sabe, sim, s = _setup()
    assert [a.icao for a in world.airspaces] == ["SAEF"]
    sim.update(com1_mhz=120.6, lat=-34.30, lon=-58.80, heading_deg=300)
    sim.set_airborne(11000, 300)
    sim.update(alt_msl_ft=11000)
    s.contacted.add("SABE:approach")
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    assert cb.tick(now=0) == f"{CS}, contact Ezeiza Control one three five decimal five, good day."
    sim.update(com1_mhz=135.5)
    cb.tick(now=1)
    r = handle(sabe, sim, None, _Quiet(), cb.history, "Ezeiza Centro, Martinair 4133, passing flight level 110",
               session=s, world=world)
    assert r == f"{CS}, Ezeiza Control, radar contact."


def test_frequency_change_to_center_now_has_a_frequency():
    world, sabe, sim, s = _setup()
    sim.update(com1_mhz=120.6, lat=-34.30, lon=-58.80)
    sim.set_airborne(15000, 300)
    s.contacted.add("SABE:approach")
    s.where = "SABE"
    r = handle(sabe, sim, None, _Quiet(), [], "Aeroparque approach, Martinair 4133, request frequency change to center",
               session=s, world=world)
    assert r == f"{CS}, contact Ezeiza Control one three five decimal five."


def test_joystick_buttons_from_the_windows_api():
    from atc.audio import joystick

    class FakeWinmm:
        def joyGetPosEx(self, dev, ptr):
            if dev != 0:
                return 167  # JOYERR_UNPLUGGED
            ptr._obj.dwButtons = 0b101
            return 0

    assert joystick.buttons(0, FakeWinmm()) == 5
    assert joystick.buttons(1, FakeWinmm()) is None
    assert joystick.pressed_list(5) == [1, 3]


def test_ptt_listens_to_key_or_button():
    from atc.audio.ptt import PushToTalk

    ptt = PushToTalk(key="none")
    assert ptt.key is None and not ptt._down()

    class Button:
        bit = 1 << 2

        def __init__(self):
            self.down = False

        def pressed(self):
            return self.down

    ptt._joy = Button()
    assert ptt.describe() == "joystick button 3"
    ptt._joy.down = True
    assert ptt._down()


def test_world_loads_the_airport_you_are_parked_at():
    """Started for SABE/SAAR, then repositioned to KSFO: Ground there must answer (it was 'no station')."""
    world, sabe, sim, s = _setup()
    assert world.get("KSFO") is None
    sim.update(lat=37.6245, lon=-122.3752, com1_mhz=121.8, on_ground=True, alt_agl_ft=0.0)
    picked = world.pick(sim.own())
    assert picked and picked[0].icao == "KSFO" and picked[1].role == "ground"
    assert world.get("KSFO") is world.airports[-1] and "KSFO" in world.taxi
    sim.update(lat=-34.5, lon=-58.4, com1_mhz=118.0)  # nowhere near any airport file: still None, no crash
    assert world.pick(sim.own()) is None
