"""ATIS from the sim's wind/QNH and the real METAR; the letter check on first contact; the broadcast loop."""

from pathlib import Path

import pytest

from atc import atis, weather
from atc.audio.tts import PrintTTS
from atc.main import _Callbacks, handle
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import load_world

ROOT = Path(__file__).resolve().parents[1]
KSFO_JSON = {"icaoId": "KSFO", "obsTime": 1791255360, "temp": 17.2, "dewp": 13.3, "wdir": 300, "wspd": 13,
             "visib": "10+", "altim": 1012.6, "cover": "FEW", "clouds": [{"cover": "FEW", "base": 800},
                                                                       {"cover": "BKN", "base": 2500}],
             "rawOb": "METAR KSFO 060256Z 30013KT 10SM -RA FEW008 BKN025 17/13 A2990"}
SABE_JSON = {"icaoId": "SABE", "obsTime": 1791255600, "temp": 18, "dewp": 9, "wdir": 10, "wspd": 8, "visib": "6+",
             "altim": 1017, "cover": "CAVOK", "clouds": [], "rawOb": "METAR SABE 060300Z 01008KT CAVOK 18/09 Q1017"}


class _Rec(PrintTTS):
    def __init__(self):
        self.lines = []

    def say(self, text):
        self.lines.append(("ATC", text))

    def say_as(self, text, who="ATC", voice=None):
        self.lines.append((who, text))


@pytest.fixture(autouse=True)
def _metars():
    weather._cache.clear()
    weather.put(weather.parse(KSFO_JSON))
    weather.put(weather.parse(SABE_JSON))
    yield
    weather._cache.clear()


def test_metar_parse():
    m = weather.parse(KSFO_JSON)
    assert (m.wind_dir_deg, m.wind_kt, m.visibility_m, m.temp_c, m.dew_c) == (300, 13, 9999, 17.2, 13.3)
    assert [c.cover for c in m.clouds] == ["FEW", "BKN"] and m.weather == ["-RA"] and not m.cavok
    assert weather.parse(SABE_JSON).cavok


def test_present_weather_words():
    w = atis.spoken_weather
    assert [w("-RA"), w("+TSRA"), w("VCSH"), w("BR"), w("FZFG"), w("SHRA")] == [
        "light rain", "heavy thunderstorm with rain", "showers in the vicinity", "mist", "freezing fog", "rain showers"]


def test_faa_atis_from_metar():
    world = load_world("KSFO", ROOT / "airports", use_navdb=False)
    ksfo = world.get("KSFO")
    sim = FakeSim(ksfo)
    letter, lines = atis.build(atis.AtisState(), ksfo, sim.own(), None, None)
    text = " ".join(lines)
    assert text.startswith(f"San Francisco information {letter}. Zero two five six Zulu.")
    assert "Wind three zero zero at one three." in text  # METAR wind (no reading taken at the airport yet)
    assert "Light rain." in text and "Few clouds at eight hundred, ceiling two thousand five hundred broken." in text
    assert "Temperature one seven, dew point one three." in text and "Altimeter two niner niner zero." in text
    assert "Landing runways two eight left and two eight right, departing runways one left and one right." in text
    assert text.endswith(f"Advise on initial contact you have information {letter}.")


def test_icao_atis_uses_the_sims_wind_and_qnh_at_the_airport():
    world = load_world("SABE", ROOT / "airports", use_navdb=False)
    sabe = world.get("SABE")
    sim = FakeSim(sabe)
    sim.update(qnh_hpa=1015.0)
    letter, lines = atis.build(atis.AtisState(), sabe, sim.own(), (300.0, 12.0), None)
    text = " ".join(lines)
    assert "Runway in use three one." in text and "Wind three one zero degrees one two knots." in text  # magnetic (10 W)
    assert "CAVOK." in text and "Temperature one eight, dew point zero niner." in text
    assert "QNH one zero one five." in text  # the sim's, not the METAR's 1017: what Tower will say
    assert text.endswith(f"Acknowledge receipt of information {letter} and advise aircraft type on first contact.")


def test_letter_moves_on_when_the_qnh_changes():
    st = atis.AtisState()
    world = load_world("SABE", ROOT / "airports", use_navdb=False)
    sabe = world.get("SABE")
    sim = FakeSim(sabe)
    sim.update(qnh_hpa=1015.0)
    a, _ = atis.build(st, sabe, sim.own(), (300.0, 12.0), None)
    assert atis.build(st, sabe, sim.own(), (300.0, 12.0), None)[0] == a
    sim.update(qnh_hpa=1012.0)
    b, _ = atis.build(st, sabe, sim.own(), (300.0, 12.0), None)
    assert atis.LETTERS.index(b[0]) == (atis.LETTERS.index(a[0]) + 1) % 26


def test_old_letter_on_first_contact_gets_the_current_one():
    world = load_world("SABE", ROOT / "airports", use_navdb=False)
    sabe = world.get("SABE")
    sim = FakeSim(sabe, callsign="N123AB")
    sim.update(com1_mhz=121.9, qnh_hpa=1015.0, wind_dir_deg=300, wind_kt=12)
    s = Session(callsign="N123AB")
    s.surface_wind["SABE"] = (300.0, 12.0)
    current, _ = atis.build(s.atis, sabe, sim.own(), (300.0, 12.0), None)
    old = atis.letter_word(atis.LETTERS.index(current[0]) - 1)
    r = handle(sabe, sim, None, PrintTTS(), [], f"Aeroparque Ground, N123AB, at stand 12 with information {old}, "
               "request taxi", session=s, world=world)
    assert r.endswith(f"Information {current} is now current, QNH one zero one five.")
    # the right letter: nothing added
    s2 = Session(callsign="N123AB", atis=s.atis)
    r2 = handle(sabe, sim, None, PrintTTS(), [], f"Aeroparque Ground, N123AB, with information {current}, request "
                "taxi", session=s2, world=world)
    assert "now current" not in r2


def test_pilot_letter_adopted_when_the_atis_was_never_played():
    world = load_world("SABE", ROOT / "airports", use_navdb=False)
    sabe = world.get("SABE")
    sim = FakeSim(sabe)
    st = atis.AtisState()
    assert atis.check_letter(st, sabe, sim.own(), "with information kilo", None, None) is None
    assert atis.build(st, sabe, sim.own(), None, None)[0] == "Kilo"


def test_atis_broadcast_on_com1_one_sentence_per_tick():
    world = load_world("SABE", ROOT / "airports", use_navdb=False)
    sabe = world.get("SABE")
    sim = FakeSim(sabe)
    sim.update(com1_mhz=127.6, wind_dir_deg=300, wind_kt=12, qnh_hpa=1015.0)
    rec = _Rec()
    cb = _Callbacks(world, sim, rec, [], Session(callsign="N123AB"))
    for t in range(4):
        cb.tick(now=float(t))
    assert [w for w, _ in rec.lines] == ["ATIS"] * 4
    assert rec.lines[0][1].startswith("Aeroparque information")
    sim.update(com1_mhz=121.9)  # tuned away: it stops
    cb.tick(now=5.0)
    assert len(rec.lines) == 4 and cb.atis_play is None
