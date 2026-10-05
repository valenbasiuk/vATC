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
