"""ATIS enforcement (Valen, 2026-10-07: "if they don't have it, ATC asks"): a first call at an airport with an ATIS
that names no letter gets "confirm information X"; "affirm" / the letter settles it, "negative" gets the current
one with the QNH; a call with the current letter is not asked; the question is asked once per airport."""

from dataclasses import replace

import pytest

from atc import atis
from atc.audio.tts import PrintTTS
from atc.main import handle
from atc.models import Frequency
from atc.session import Session
from atc.sim.fake import FakeSim
from tests.test_flight import ORIGIN

APT = replace(ORIGIN, frequencies=ORIGIN.frequencies + [Frequency("ATIS", 127.6)])
CS = "Martinair four one three three"


class _Quiet(PrintTTS):
    def say(self, text):
        pass


@pytest.fixture(autouse=True)
def _enforce(monkeypatch):
    monkeypatch.setattr("atc.atis.ENFORCE", True)


def _setup():
    sim = FakeSim(APT, callsign="MAR4133")
    sim.update(lat=-34.5638, lon=-58.4072, com1_mhz=121.9, qnh_hpa=1013.0)
    return sim, Session(callsign="MAR4133", telephony="Martinair"), []


def _letter(session, sim):
    return atis.build(session.atis, APT, sim.own())[0]


def test_first_call_without_the_letter_is_asked_and_affirm_settles_it():
    sim, s, h = _setup()
    r = handle(APT, sim, None, _Quiet(), h, "Testa Ground, Martinair 4133, stand 12, request push and start",
               session=s)
    word = _letter(s, sim)
    assert r == f"{CS}, Testa Ground, push and start approved. Confirm information {word}."
    assert handle(APT, sim, None, _Quiet(), h, "Affirm, Martinair 4133", session=s) is None
    assert APT.icao in s.atis_confirmed
    r = handle(APT, sim, None, _Quiet(), h, "Martinair 4133, request taxi", session=s)
    assert "information" not in r  # asked once


def test_negative_gets_the_current_information_and_qnh():
    sim, s, h = _setup()
    handle(APT, sim, None, _Quiet(), h, "Testa Ground, Martinair 4133, request push and start", session=s)
    word = _letter(s, sim)
    r = handle(APT, sim, None, _Quiet(), h, "Negative, Martinair 4133", session=s)
    assert r == f"{CS}, information {word} is current, QNH one zero one three."


def test_a_call_with_the_current_letter_is_not_asked():
    sim, s, h = _setup()
    word = _letter(s, sim)
    r = handle(APT, sim, None, _Quiet(), h, f"Testa Ground, Martinair 4133, information {word}, request push and start",
               session=s)
    assert r == f"{CS}, Testa Ground, push and start approved."


def test_a_readback_that_ignores_the_question_is_still_a_correct_readback():
    sim, s, h = _setup()
    handle(APT, sim, None, _Quiet(), h, "Testa Ground, Martinair 4133, request push and start", session=s)
    assert handle(APT, sim, None, _Quiet(), h, "Push and start approved, Martinair 4133", session=s) is None


def test_no_question_where_there_is_no_atis():
    sim, s, h = _setup()
    r = handle(ORIGIN, sim, None, _Quiet(), h, "Testa Ground, Martinair 4133, request push and start", session=s)
    assert r == f"{CS}, Testa Ground, push and start approved."
