"""Separate departure and arrival runways (airport YAML `runway_configs`): KSFO's west plan lands 28L/28R and
departs 1L/1R, its southeast plan lands 19L/19R and departs 10L/10R. Airports without configs (SABE) keep one
runway for everything."""

from pathlib import Path

from atc.airports.schema import load_airport
from atc.runway import runway_config, runway_in_use, runways_in_use

ROOT = Path(__file__).resolve().parents[1]


def _ksfo():
    return load_airport(ROOT / "airports" / "KSFO.yaml")


def _ids(rs):
    return [r.ident for r in rs]


def test_west_plan_in_a_westerly():
    k = _ksfo()
    assert runway_in_use(k, 290, 12, use="departure").ident == "1L"
    assert runway_in_use(k, 290, 12, use="arrival").ident == "28L"
    assert _ids(runways_in_use(k, 290, 12, "arrival")) == ["28L", "28R"]
    assert _ids(runways_in_use(k, 290, 12, "departure")) == ["1L", "1R"]


def test_southeast_plan_in_a_southeasterly():
    k = _ksfo()
    assert runway_config(k, 150, 15)["arrival"] == ["19L", "19R"]
    assert runway_in_use(k, 150, 15, use="departure").ident == "10L"
    assert runway_in_use(k, 150, 15, use="arrival").ident == "19L"


def test_calm_wind_takes_the_first_plan():
    k = _ksfo()
    assert runway_in_use(k, None, None, use="departure").ident == "1L"
    assert runway_in_use(k, 0, 2, use="arrival").ident == "28L"


def test_planned_runway_is_kept_when_it_is_in_the_list():
    k = _ksfo()
    assert runway_in_use(k, 290, 12, "1R", use="departure").ident == "1R"
    assert runway_in_use(k, 290, 12, "28R", use="arrival").ident == "28R"
    assert runway_in_use(k, 290, 12, "28L", use="departure").ident == "1L"  # not a departure runway in this plan


def test_departures_move_to_the_arrival_runways_with_a_tailwind():
    k = _ksfo()  # strong south-westerly: the 19s land, the 10s would have a big tailwind, so they depart 19 too
    assert runway_in_use(k, 240, 20, use="arrival").ident == "19L"
    assert runway_in_use(k, 240, 20, use="departure").ident == "19L"


def test_without_use_or_configs_nothing_changes():
    k = _ksfo()
    assert runway_in_use(k, 290, 12).ident == "28R"  # plain best headwind, as before
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    assert runway_config(sabe, 310, 10) is None
    assert runway_in_use(sabe, 310, 10, use="departure").ident == runway_in_use(sabe, 310, 10, use="arrival").ident
