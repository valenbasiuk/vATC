from atc.readback import check_readback

TAXI = "TANS Peru 1209, taxi to runway 09, hold short."
TKOF = "Runway 27, cleared for takeoff."


def test_correct_readback():
    assert check_readback(TAXI, "Taxi to runway 09 and hold short, TANS Peru 1209").status == "correct"
    assert check_readback(TAXI, "Runway zero niner, holding short, Tans Peru 1209").status == "correct"
    assert check_readback(TKOF, "Cleared for takeoff runway 27").status == "correct"


def test_wrong_runway_is_incomplete():
    r = check_readback(TAXI, "Taxi to runway 27 and hold short")
    assert r.status == "incomplete" and "runway 09" in r.missing


def test_missing_hold_short_is_incomplete():
    r = check_readback(TAXI, "Runway 09, TANS Peru 1209")
    assert r.status == "incomplete" and "hold short" in r.missing


def test_requests_and_unrelated_calls_are_not_readbacks():
    assert check_readback(TAXI, "Holding short runway 09, ready for departure").status == "none"
    assert check_readback(TAXI, "Say again please").status == "none"
    assert check_readback(None, "Taxi to runway 09 and hold short").status == "none"
    assert check_readback("Loud and clear.", "Thanks").status == "none"


def test_runway_sides_distinguished():
    assert check_readback("Runway 09 left, cleared to land.", "Runway 09 right cleared to land").status == "incomplete"
    assert check_readback("Runway 09 left, cleared to land.", "Runway 09 left cleared to land").status == "correct"


def test_stt_spellings_of_compound_words():
    from atc.readback import _normalize, check_readback

    atc = "Martinair four one three three, wind three two zero degrees one zero knots, runway three one, cleared for " \
          "takeoff."
    for said in ("cleared for take off runway 31, Martinair 4133", "cleared for take-off 31, Martinair 4133"):
        assert check_readback(atc, said).status == "correct"
    assert _normalize("Left down wind runway 31") == "left downwind runway 31"
    assert _normalize("cross wind leg") == "crosswind leg"
