import pytest


@pytest.fixture(autouse=True)
def _no_little_navmap_db(monkeypatch, request):
    """Tests use the repo's files only, never the Little Navmap database of whoever runs them
    (tests/test_navdb.py opts back in and is skipped where that database doesn't exist)."""
    if "uses_navdb" not in request.keywords:
        monkeypatch.setenv("ATC_LNM_DB", "Z:/no/such/little_navmap.sqlite")
