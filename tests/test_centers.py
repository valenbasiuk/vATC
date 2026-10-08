"""Area control anywhere (atc.centers + world.control) on a small made-up data/centers.json."""

import json

import pytest

from atc import centers
from atc.models import Airport, Frequency, OwnState, Runway
from atc.world import World


def _box(la1, lo1, la2, lo2):
    return [la1, lo1, la1, lo2, la2, lo2, la2, lo1]


@pytest.fixture
def data(tmp_path):
    raw = {
        "version": 1,
        "positions": {
            "sg/FAC": {"cs": "Asuncion Control", "mhz": 128.4, "cc": "PY", "fir": "SGFA", "src": "VATGlasses"},
            "sa/REC": {"cs": "Resistencia Control", "mhz": 124.3, "cc": "AR", "fir": "SARR", "src": "hand"},
            "sa/REU": {"cs": "Resistencia Control", "mhz": 132.1, "cc": "AR", "fir": "SARR", "src": "hand"},
            "vnas/ZXX/W": {"cs": "Testa Center", "mhz": 125.0, "cc": "US", "fir": "KZXX", "src": "vNAS"},
            "vnas/ZXX/E": {"cs": "Testa Center", "mhz": 133.0, "cc": "US", "fir": "KZXX", "src": "vNAS"},
            "nf/FJ": {"cs": "Nadi Control", "mhz": 120.1, "cc": "FJ", "fir": "NFFF", "src": "VATGlasses"},
        },
        "volumes": [  # smallest first, as tools/build_centers.py writes them
            ["sa/REU", 24500, 99900, _box(-29.0, -60.0, -26.0, -57.0), None],
            ["sg/FAC", 0, 99900, _box(-27.0, -62.0, -19.0, -54.0), None],
            ["sa/REC", 0, 99900, _box(-29.0, -60.0, -26.0, -57.0), None],
            [None, 0, 99900, _box(30.0, -100.0, 35.0, -90.0), [[32.0, -99.0, "vnas/ZXX/W"], [32.0, -91.0, "vnas/ZXX/E"]]],
            ["nf/FJ", 0, 99900, _box(-25.0, 170.0, -10.0, 190.0), None],  # across the antimeridian, unwrapped
        ],
    }
    path = tmp_path / "centers.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    centers.reset(path)
    yield path


def test_the_smallest_sector_at_that_level_wins(data):
    assert centers.sector_at(-25.2, -57.5, 30000).callsign == "Asuncion Control"
    assert centers.sector_at(-27.5, -58.7, 10000).mhz == 124.3  # Corrientes, low
    assert centers.sector_at(-27.5, -58.7, 35000).mhz == 132.1  # the upper sector above FL245
    assert centers.sector_at(-40.0, -58.7, 35000) is None


def test_us_sectors_from_radio_sites_and_the_antimeridian(data):
    assert centers.sector_at(33.0, -98.0, 35000).mhz == 125.0
    assert centers.sector_at(33.0, -92.0, 35000).mhz == 133.0
    assert centers.sector_at(-18.0, -178.0, 35000).callsign == "Nadi Control"  # east of 180
    assert centers.sector_at(-18.0, 178.0, 35000).callsign == "Nadi Control"


def test_a_tuned_center_frequency_answers_nearby(data):
    assert centers.by_frequency(128.4, -27.5, -58.7).pid == "sg/FAC"
    assert centers.by_frequency(128.4, 10.0, -58.7) is None  # far away


def test_world_control_uses_the_sector_under_the_aircraft(data):
    sarc = Airport("SARC", "Corrientes", -27.4455, -58.7619, 202, country="AR", towered=True,
                   runways=[Runway("02", 8.0, 6890)], frequencies=[Frequency("TWR", 118.3)], trans_alt_ft=3000)
    world = World([sarc])
    own = OwnState(-26.0, -57.6, 30000, 29800, 450, 10, False, 124.3)
    a, fac = world.control(own, sarc)
    assert fac.role == "control" and fac.freq.mhz == 128.4 and fac.freq.spoken == "Asuncion Control"
    assert a.country == "PY" and a.trans_alt_ft is None  # not Argentina's 3000 ft
    tuned = OwnState(-26.0, -57.6, 30000, 29800, 450, 10, False, 128.4)
    assert world.pick(tuned)[0] is a  # tuned, it answers
    low = OwnState(-27.6, -58.7, 9000, 8800, 300, 10, False, 118.3)
    assert world.control(low, sarc)[1].freq.mhz == 124.3
