import pytest


@pytest.fixture(autouse=True)
def _no_little_navmap_db(monkeypatch, request):
    """Tests use the repo's files only, never the Little Navmap database of whoever runs them
    (tests/test_navdb.py opts back in and is skipped where that database doesn't exist)."""
    if "uses_navdb" not in request.keywords:
        monkeypatch.setenv("ATC_LNM_DB", "Z:/no/such/little_navmap.sqlite")


@pytest.fixture(autouse=True)
def _no_new_airport_files(monkeypatch):
    """Never write airports/<ICAO>.yaml from a test (the world generates files for airports it finds)."""
    monkeypatch.setattr("atc.world.AUTO_GENERATE", False)


@pytest.fixture(autouse=True)
def _no_runway_users_left_over(monkeypatch):
    """The AI Tower put on a runway (kept by the chatter watcher) never leak from one test into the next."""
    monkeypatch.setattr("atc.sequence.RUNWAY_USERS", {})
    monkeypatch.setattr("atc.runway.AI_FLOW", {})
    monkeypatch.setattr("atc.own.OWN", {})
    monkeypatch.setattr("atc.own.APRON", {})
    monkeypatch.setattr("atc.atis.SIM_WX", {})
    from atc import departures

    departures.reset()


@pytest.fixture(autouse=True)
def _no_community_folder(monkeypatch):
    """Own traffic never reads the sim's Community folder (FSLTL, FS Traffic, GSX) of whoever runs the tests."""
    monkeypatch.setenv("ATC_COMMUNITY_DIR", "Z:/no/such/Community")
    for var in ("ATC_OWN_MODELS", "ATC_OWN_TUG", "ATC_OWN_TUG_YAW"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_atis_question(monkeypatch):
    """Tests written before the ATIS check expect replies without 'confirm information X' (tests of it turn it on)."""
    monkeypatch.setattr("atc.atis.ENFORCE", False)
    monkeypatch.setattr("atc.atis.CONTROLLER_WEATHER", False)


@pytest.fixture(autouse=True)
def _offline_weather(monkeypatch):
    """No METAR downloads in tests (tests seed atc.weather with weather.put when they need one)."""
    monkeypatch.setenv("ATC_METAR", "off")


@pytest.fixture(autouse=True)
def _no_world_centers(request):
    """Tests use the airspace/*.yaml files and the navdata FIRs, never data/centers.json of whoever runs them
    (tests/test_centers.py loads a small made-up one)."""
    from pathlib import Path

    from atc import centers

    centers.reset(Path("Z:/no/such/centers.json"))
    yield
    centers.reset(Path("data/centers.json"))
