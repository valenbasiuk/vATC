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


@pytest.fixture(autouse=True)
def _offline_weather(monkeypatch):
    """No METAR downloads in tests (tests seed atc.weather with weather.put when they need one)."""
    monkeypatch.setenv("ATC_METAR", "off")
