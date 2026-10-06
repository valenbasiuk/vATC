from pathlib import Path

from atc.airports.gen import build_airport
from atc.facility import resolve_facility
from atc.llm.prompt import build_context, build_system_prompt
from atc.models import Airport, Runway
from atc.runway import crosswind_kt, headwind_kt, runway_in_use
from atc.scenarios import load_scenarios, run_scenario
from atc.sim.fake import FakeSim

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


def _two_runway_airport() -> Airport:
    return Airport(
        icao="TEST", name="Test", lat=0, lon=0, elevation_ft=0,
        runways=[Runway("09", 90, 6000), Runway("27", 270, 5000), Runway("18", 180, 3000), Runway("36", 360, 3000)],
    )


def test_headwind_math():
    assert abs(headwind_kt(270, 10, 270) - 10) < 1e-6
    assert abs(headwind_kt(270, 10, 90) + 10) < 1e-6
    assert abs(crosswind_kt(270, 10, 180) - 10) < 1e-6


def test_runway_in_use_follows_wind():
    a = _two_runway_airport()
    assert runway_in_use(a, 270, 12).ident == "27"
    assert runway_in_use(a, 90, 12).ident == "09"
    assert runway_in_use(a, 355, 12).ident == "36"


def test_calm_or_unknown_wind_picks_longest_runway():
    a = _two_runway_airport()
    assert runway_in_use(a, 270, 2).ident == "09"
    assert runway_in_use(a, None, None).ident == "09"


def test_no_runway_headings_means_no_decision():
    a = Airport(icao="X", name="X", lat=0, lon=0, elevation_ft=0, runways=[Runway("01")])
    assert runway_in_use(a, 10, 10) is None


def test_context_includes_weather_only_when_known():
    a = build_airport("KTST", FIX)
    sim = FakeSim(a)
    unknown = build_context(sim.own(), [], a)
    assert "Wind: NOT AVAILABLE" in unknown and "degrees at" not in unknown  # unknown -> explicit, no value
    assert "Altimeter/QNH: NOT AVAILABLE" in unknown and "inches" not in unknown
    sim.update(wind_dir_deg=270, wind_kt=10, qnh_hpa=1013.25)
    ctx = build_context(sim.own(), [], a)
    assert "Wind: 270 degrees at 10 knots" in ctx
    assert "Altimeter: 29.92 inches" in ctx  # US airport -> inches
    assert "Runway in use (computed from wind, treat as fact): 27" in ctx


def test_non_us_airport_uses_qnh_hpa():
    a = _two_runway_airport()
    a.country = "AR"
    sim = FakeSim(a)
    sim.update(wind_dir_deg=0, wind_kt=1, qnh_hpa=1015.0)
    ctx = build_context(sim.own(), [], a)
    assert "Wind: calm" in ctx and "QNH: 1015 hectopascals" in ctx


def test_system_prompt_forbids_inventing_weather():
    a = build_airport("KTST", FIX)
    s = build_system_prompt(a, resolve_facility(a, 118.1))
    assert "do not state or invent it" in s


class _ScriptedLLM:
    """Pretends to be a model that always answers well for the shipped scenarios."""

    def __init__(self, answer_for):
        self.answer_for = answer_for

    def complete(self, messages):
        return self.answer_for(messages[-1]["content"])


def _good_model(content: str) -> str:
    low = content.lower()
    if "say the wind" in low:
        return "Unable, no wind information available."
    if "runway in use" in low and "27" in content.split("Runway in use")[1][:80]:
        return "Runway 27, cleared for takeoff."
    if "inbound" in low:
        return "Enter left downwind runway 09, report midfield."
    if "taxi" in low:
        return "Taxi to runway 09 via the main taxiway, hold short."
    return "Roger."


def test_shipped_scenarios_pass_with_a_good_model():
    specs = load_scenarios(ROOT / "scenarios")
    assert len(specs) >= 6
    for spec in specs:
        res = run_scenario(spec, FIX / "airports", _ScriptedLLM(_good_model))
        assert res.ok, (res.name, [(t.say, t.failures) for t in res.turns])


def test_bad_model_inventions_never_reach_the_pilot():
    # The model invents a wind; the fact check rejects it (twice) and the pilot hears "say again" instead.
    bad = _ScriptedLLM(lambda c: "Wind 270 at 8 knots, runway 36, cleared for takeoff.")
    specs = {s["name"]: s for s in load_scenarios(ROOT / "scenarios")}
    res = run_scenario(next(s for n, s in specs.items() if "no wind given" in n), FIX / "airports", bad)
    assert res.ok and all("270" not in (t.reply or "") for t in res.turns)
    res = run_scenario(next(s for n, s in specs.items() if "ATIS" in n), FIX / "airports", bad)
    assert res.ok  # silence is enforced by the app before the model is ever called
