from pathlib import Path

from atc.airports.gen import build_airport
from atc.facility import resolve_facility
from atc.geo import bearing_deg, compass_point, distance_nm
from atc.llm.client import StubLLM, clean_reply
from atc.llm.prompt import build_context, build_messages, build_system_prompt
from atc.main import handle
from atc.models import Traffic
from atc.sim.fake import FakeSim

FIX = Path(__file__).parent / "fixtures"


class Capture:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


def test_distance_and_bearing_sanity():
    # One degree of latitude is ~60 NM, due north.
    assert abs(distance_nm(0, 0, 1, 0) - 60.0) < 0.5
    assert abs(bearing_deg(0, 0, 1, 0) - 0.0) < 0.1
    assert compass_point(90) == "E"


def test_facility_resolution():
    a = build_airport("KTST", FIX)
    assert resolve_facility(a, 118.1).role == "tower"
    assert resolve_facility(a, 121.7).role == "ground"
    atis = resolve_facility(a, 127.25)
    assert atis.role == "atis" and atis.can_reply is False
    assert resolve_facility(a, 135.0) is None


def test_system_prompt_contains_only_airport_facts():
    a = build_airport("KTST", FIX)
    f = resolve_facility(a, 118.1)
    s = build_system_prompt(a, f)
    assert "09" in s and "27" in s
    assert "18" not in s.split("Runways:")[1].split("Frequencies")[0]  # closed runway absent
    assert "TWR 118.100" in s
    assert "FAA" in s
    assert "Never invent" in s


def test_context_lists_traffic_nearest_first():
    a = build_airport("KTST", FIX)
    sim = FakeSim(a)
    sim.add_traffic(Traffic("N111", a.lat + 0.05, a.lon, 2000, 100, 90, False))
    sim.add_traffic(Traffic("N222", a.lat + 0.01, a.lon, 1000, 90, 90, False))
    ctx = build_context(sim.own(), sim.traffic(a.lat, a.lon, 15), a)
    assert ctx.index("N222") < ctx.index("N111")


def test_traffic_outside_radius_is_filtered():
    a = build_airport("KTST", FIX)
    sim = FakeSim(a)
    sim.add_traffic(Traffic("FAR", a.lat + 2.0, a.lon, 5000, 200, 90, False))
    assert sim.traffic(a.lat, a.lon, 15) == []


def test_messages_keep_last_turns_only():
    hist = [(f"p{i}", f"a{i}") for i in range(10)]
    msgs = build_messages("sys", "ctx", hist, "now", max_turns=3)
    assert msgs[0]["role"] == "system"
    assert [m["content"] for m in msgs[1:7]] == ["p7", "a7", "p8", "a8", "p9", "a9"]
    assert "PILOT TRANSMISSION: now" in msgs[-1]["content"]


def test_clean_reply_strips_markdown():
    assert clean_reply("**Cleared** for `takeoff`\n\nrunway 09") == "Cleared for takeoff runway 09"


def test_handle_full_turn_with_stub():
    a = build_airport("KTST", FIX)
    sim = FakeSim(a)  # starts on ground frequency
    cap = Capture()
    history = []
    reply = handle(a, sim, StubLLM(), cap, history, "ready for taxi")
    assert reply == "November one two three Alfa Bravo, Testfield Ground, runway niner, taxi."  # FAA, code
    assert cap.said == [reply]
    assert history == [("ready for taxi", reply)]


def test_handle_silent_when_no_station_or_atis():
    a = build_airport("KTST", FIX)
    sim = FakeSim(a)
    cap = Capture()
    sim.update(com1_mhz=135.0)
    assert handle(a, sim, StubLLM(), cap, [], "hello") is None
    sim.update(com1_mhz=127.25)  # ATIS
    assert handle(a, sim, StubLLM(), cap, [], "hello") is None
    assert cap.said == []
