"""The runway in use follows the sim's AI traffic (MSFS AI picks its runway itself and ignores this ATC).
Real sim, SABE 2026-10-07: the AI departed 31 while this ATC sent the user to 13 (wind 080/20 true)."""

from atc import runway
from atc.models import Airport, Frequency, Runway, Traffic
from atc.runway import runway_in_use, session_runway
from atc.session import Session
from atc.tracker import AI_FLOW_WINDOW_S, TrafficTracker

APT = Airport(
    icao="SATS", name="Testa Aerodrome", spoken_name="Testa", lat=-34.5592, lon=-58.4156, elevation_ft=18,
    country="AR", towered=True,
    runways=[Runway("13", 124.0, 7710, lat=-34.553902, lon=-58.425098),
             Runway("31", 304.0, 7710, lat=-34.564499, lon=-58.406101)],
    frequencies=[Frequency("GND", 121.9), Frequency("TWR", 118.85)],
)
R31 = APT.runways[1]


def _taxiing(cs: str) -> Traffic:
    return Traffic(cs, R31.lat + 0.003, R31.lon, 18, 10.0, 250.0, True)  # on a taxiway beside the 31 end


def _lined_up_31(cs: str) -> Traffic:
    return Traffic(cs, R31.lat, R31.lon, 18, 0.0, 304.0, True)


def test_tracker_learns_the_departure_runway_from_ai_line_ups_and_forgets_it_later():
    tr = TrafficTracker(APT)
    tr.update([_taxiing("ARG5310")], now=0)
    tr.update([_lined_up_31("ARG5310")], now=5)
    assert tr.ai_flow(now=5) == {"departure": "31"}
    assert tr.ai_flow(now=5 + AI_FLOW_WINDOW_S + 1) == {}


def test_runway_in_use_follows_the_ai_up_to_ten_knots_of_tailwind(monkeypatch):
    assert runway_in_use(APT, 80.0, 8.0, use="departure").ident == "13"  # by the wind alone
    runway.AI_FLOW["SATS"] = {"departure": "31"}
    assert runway_in_use(APT, 80.0, 8.0, use="departure").ident == "31"  # ~6 kt tailwind on 31: follow the AI
    assert runway_in_use(APT, 80.0, 8.0, use="arrival").ident == "31"  # no configs: one runway for both
    assert runway_in_use(APT, 80.0, 20.0, use="departure").ident == "13"  # ~14 kt tailwind: the wind decides


def test_taxi_clearance_keeps_the_pilots_runway_when_the_ai_changes_later():
    from atc.sim.fake import FakeSim
    from atc.taxi import handle_taxi
    from atc.facility import resolve_facility

    sim = FakeSim(APT, callsign="LVABC")
    sim.update(com1_mhz=121.9, wind_dir_deg=80.0, wind_kt=8.0)
    s = Session(callsign="LVABC")
    own = sim.own()
    rwy = session_runway(APT, 80.0, 8.0, s, "departure")
    r = handle_taxi(s, APT, resolve_facility(APT, 121.9), own, "Testa Ground, LVABC, request taxi", None, [], rwy)
    assert "runway one three" in r
    runway.AI_FLOW["SATS"] = {"departure": "31"}  # the AI starts using 31 while the user taxis to 13
    assert session_runway(APT, 80.0, 8.0, s, "departure").ident == "13"
    assert session_runway(APT, 80.0, 8.0, Session(callsign="LVXYZ"), "departure").ident == "31"  # a new pilot: 31
