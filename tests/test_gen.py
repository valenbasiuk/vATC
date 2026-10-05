from pathlib import Path

import pytest

from atc.airports.gen import build_airport, main as gen_main
from atc.airports.schema import load_airport

FIX = Path(__file__).parent / "fixtures"


def test_us_airport_gets_pattern_defaults():
    a = build_airport("KTST", FIX)
    assert a.towered is True
    assert a.country == "US"
    idents = {r.ident for r in a.runways}
    assert idents == {"09", "27"}  # closed runway 18/36 skipped
    assert all(r.pattern_alt_agl_ft == 1000 and r.pattern_direction == "left" for r in a.runways)


def test_non_us_airport_leaves_pattern_empty_and_flags_it():
    a = build_airport("SATS", FIX)
    assert all(r.pattern_alt_agl_ft is None and r.pattern_direction is None for r in a.runways)
    assert any("Non-US" in n for n in a.notes)
    assert a.needs_review is True


def test_non_towered_detected():
    a = build_airport("KNTW", FIX)
    assert a.towered is False
    assert any(f.kind == "CTAF" for f in a.frequencies)


def test_unknown_icao_raises():
    with pytest.raises(KeyError):
        build_airport("ZZZZ", FIX)


def test_generate_then_load_roundtrip(tmp_path):
    assert gen_main(["KTST", "--data-dir", str(FIX), "--out", str(tmp_path)]) == 0
    loaded = load_airport(tmp_path / "KTST.yaml")
    assert loaded.icao == "KTST"
    assert len(loaded.runways) == 2
    assert {f.kind for f in loaded.frequencies} == {"TWR", "GND", "ATIS"}


def test_generator_never_overwrites_hand_edits(tmp_path):
    target = tmp_path / "KTST.yaml"
    target.write_text("hand edited", encoding="utf-8")
    gen_main(["KTST", "--data-dir", str(FIX), "--out", str(tmp_path)])
    assert target.read_text(encoding="utf-8") == "hand edited"


def test_hand_typed_numeric_runway_ident_keeps_leading_zero(tmp_path):
    f = tmp_path / "KXYZ.yaml"
    f.write_text(
        "icao: KXYZ\nname: X\nlat: 1.0\nlon: 2.0\nelevation_ft: 10\n"
        "runways:\n- ident: 07\n- ident: 25\n- ident: 09L\n",
        encoding="utf-8",
    )
    assert [r.ident for r in load_airport(f).runways] == ["07", "25", "09L"]
