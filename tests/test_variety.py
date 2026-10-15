"""atc.variety: the AI pilots and controllers say things their own way, never with other values."""

import random
import re

import pytest

from atc import variety

CS = "Argentina one two three four"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(variety, "ENABLED", True)


def _many(text, cs=CS, n=300, **kw):
    rng = random.Random(7)
    return [variety.pilot_line(text, cs, rng=rng, **kw) for _ in range(n)]


def test_landing_readback_keeps_runway_and_callsign_and_varies():
    out = _many("Cleared to land runway one three, Argentina one two three four.", country="AR")
    for o in out:
        assert "cleared to land" in o.lower() and "one three" in o and CS.lower() in o.lower(), o
        assert o[0].isupper() and o.endswith(".")
    assert len(set(out)) >= 2


def test_every_pilot_has_a_style_and_together_they_vary():
    """Across many pilots, readbacks come out several ways (word order, callsign first, 'runway' dropped)."""
    forms = set()
    for n in range(40):
        cs = f"Argentina one two {n}"
        o = variety.pilot_line(f"Cleared for takeoff runway three one, {cs}.", cs, rng=random.Random(n))
        forms.add(re.sub(re.escape(cs), "CS", o))
    assert len(forms) >= 3, forms
    assert variety.style("ARG1234") == variety.style("ARG1234")  # the same pilot all session


def test_handoff_readback_keeps_the_frequency():
    out = _many("Departure one two zero decimal six, good day, Argentina one two three four.", country="AR",
                atc_country="AR")
    for o in out:
        assert "one two zero decimal six" in o.lower() and CS.lower() in o.lower(), o
    assert len(set(out)) >= 3
    assert any("buen día" in o or "chau" in o or "buenas" in o for o in out)  # Argentine pilot, Argentine ATC


def test_no_spanish_courtesy_abroad():
    out = _many("Ground one two one decimal niner, Speedbird two eight six.", cs="Speedbird two eight six",
                country="GB", atc_country="US")
    assert not any("buen" in o or "chau" in o for o in out)
    assert all("one two one decimal niner" in o.lower() for o in out)


def test_taxi_route_untouched():
    text = "Holding point runway one three via Kilo, Alfa, Argentina one two three four."
    for o in _many(text):
        assert "via Kilo, Alfa" in o and "one three" in o, o


def test_first_call_may_greet_or_hesitate_but_keeps_the_request():
    text = "Aeroparque Ground, Argentina one two three four, stand one two, information Bravo, request push and start."
    out = []
    for n in range(40):
        cs = f"Argentina four four {n}"
        out += [variety.pilot_line(text.replace(CS, cs), cs, first_call=True, rng=random.Random(n))]
    for o in out:
        assert o.startswith("Aeroparque Ground") and "information Bravo, request push and start" in o, o
    assert any("good" in o or "hello" in o for o in out)


def test_controller_sign_off_varies_per_position_but_keeps_the_frequency():
    rng = random.Random(3)
    text = "Argentina one two three four, contact departure one two zero decimal six, good day."
    out = {variety.atc_line(text, "SABE:tower", rng=rng) for _ in range(50)}
    assert all(o.startswith("Argentina one two three four, contact departure one two zero decimal six") for o in out)
    assert len(out) >= 2


def test_say_again_repeats_the_controller():
    lines = [("ATC", "Argentina one two three four, line up and wait runway one three.", None),
             (CS, "Line up and wait runway one three, Argentina one two three four.", 5)]
    rng = random.Random(0)
    seen = None
    for _ in range(500):
        out = variety.say_again(lines, rng=rng)
        if len(out) > 2:
            seen = out
            break
    assert seen is not None
    assert "say again" in seen[1][1].lower() and seen[2] == lines[0] and seen[3] == lines[1]


def test_off_means_word_for_word(monkeypatch):
    monkeypatch.setattr(variety, "ENABLED", False)
    t = "Cleared to land runway one three, Argentina one two three four."
    assert variety.pilot_line(t, CS) == t
