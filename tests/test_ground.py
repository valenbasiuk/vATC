"""Ground conflicts while taxiing (ground.py): give way to crossing traffic, hold position for head-on traffic and
"continue taxi" once it has passed. SABE, the user on the apron east of the runway."""

from pathlib import Path

from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.main import _Callbacks
from atc.models import Traffic
from atc.readback import is_acknowledgement
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]
CS = "Martinair four one three three"
NM = 1 / 60


class _Quiet(PrintTTS):
    def say(self, text):
        pass

    def say_as(self, text, who="ATC", voice=None):
        pass


def _setup(freq=121.9):
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    sim = FakeSim(sabe, callsign="MAR4133")
    lat, lon = sabe.lat + 0.004, sabe.lon + 0.004
    sim.update(com1_mhz=freq, lat=lat, lon=lon, heading_deg=0.0, gs_kt=15.0, on_ground=True)
    s = Session(callsign="MAR4133", telephony="Martinair")
    return sim, s, _Callbacks(World([sabe]), sim, _Quiet(), [], s), lat, lon


def _ai(lat, lon, hdg, gs=15.0, cs="ARG1234"):
    return Traffic(cs, lat, lon, 18.0, gs, hdg, True, type="A320")


def test_give_way_to_crossing_traffic_from_the_right():
    sim, s, cb, lat, lon = _setup()
    # 0.06 NM ahead, 0.06 NM to the right, heading west at the same speed: we'd meet in ~15 s
    sim.add_traffic(_ai(lat + 0.06 * NM, lon + 0.06 * NM / 0.82, 270.0))
    assert cb.tick(now=0.0) is None  # predicted once: it may still turn off
    assert cb.tick(now=1.0) == f"{CS}, give way to the Airbus three twenty from the right."
    assert cb.tick(now=2.0) is None  # once
    assert is_acknowledgement("give way to the Airbus three twenty from the right", "Giving way, Martinair 4133",
                              tuple(CS.split()))


def test_head_on_hold_position_then_continue_taxi():
    sim, s, cb, lat, lon = _setup()
    sim.add_traffic(_ai(lat + 0.12 * NM, lon, 180.0))
    assert cb.tick(now=0.0) is None
    assert cb.tick(now=1.0) == f"{CS}, hold position, Airbus three twenty opposite direction."
    sim.update(gs_kt=0.0)
    assert cb.tick(now=5.0) is None  # still in front of us
    sim.clear_traffic()
    sim.add_traffic(_ai(lat - 0.05 * NM, lon + 0.02 * NM, 180.0))  # gone past, behind us
    assert cb.tick(now=30.0) == f"{CS}, continue taxi."


def test_same_direction_and_far_traffic_say_nothing():
    sim, s, cb, lat, lon = _setup()
    sim.add_traffic(_ai(lat + 0.03 * NM, lon, 0.0, gs=10.0))  # slower, ahead, same way: we follow it
    sim.add_traffic(_ai(lat + 0.6 * NM, lon, 180.0, cs="ARG2"))  # far
    assert cb.tick(now=0.0) is None and cb.tick(now=1.0) is None


def test_only_on_the_ground_frequency():
    sim, s, cb, lat, lon = _setup(freq=118.85)  # Tower, and SABE has a Ground
    sim.add_traffic(_ai(lat + 0.06 * NM, lon + 0.06 * NM / 0.82, 270.0))
    assert cb.tick(now=0.0) is None and cb.tick(now=1.0) is None


def test_pushback_moves_tail_first_and_a_turning_ai_is_not_called():
    sim, s, cb, lat, lon = _setup()
    # an AI ahead of us facing us (heading 180) but being pushed back away from us (moving north): no conflict
    ai = _ai(lat + 0.04 * NM, lon, 180.0, gs=4.0)
    sim.add_traffic(ai)
    cb.tick(now=0.0)
    sim.clear_traffic()
    sim.add_traffic(_ai(lat + 0.0408 * NM, lon, 180.0, gs=4.0))  # moved 1.5 m north in 1 s, tail first
    sim.update(lat=lat + 0.0042 * NM)  # we moved ~8 m north (15 kt)
    assert cb.tick(now=1.0) is None and cb.tick(now=2.0) is None
    # a crossing AI predicted once, then turning away before the second look: never called
    sim2, s2, cb2, lat2, lon2 = _setup()
    sim2.add_traffic(_ai(lat2 + 0.06 * NM, lon2 + 0.06 * NM / 0.82, 270.0, cs="ARG9"))
    assert cb2.tick(now=0.0) is None
    sim2.clear_traffic()
    sim2.add_traffic(_ai(lat2 + 0.06 * NM, lon2 + 0.06 * NM / 0.82, 90.0, cs="ARG9"))  # turned away (heading east)
    assert cb2.tick(now=10.0) is None and cb2.tick(now=11.0) is None
