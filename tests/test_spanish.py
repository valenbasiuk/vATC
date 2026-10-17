"""Spanish radio (improvement plan J2, first slice): Spanish calls understood, every transmission said in Spanish, the
readback checks unchanged underneath (they run on the English form)."""

from pathlib import Path

import pytest

from atc.airports.schema import load_airport
from atc.flightplan import FlightPlan
from atc.main import handle
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.spanish import SpanishSpeaker, to_english, to_spanish
from atc.taxi import _holding_points, load_network
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]
PLAN = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="F100", origin="SABE", destination="SAAR",
                  destination_name="Islas Malvinas", alternate=None, route="ATOVO4B ATOVO W5 PEDRO DCT ESKON DCT",
                  sid="ATOVO4B", sid_transition="ATOVO", cruise_ft=20000, planned_runway="31")


class _Capture:
    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)

    def say_as(self, text, who="ATC", voice=None):
        self.said.append(text)


@pytest.fixture()
def spanish(monkeypatch):
    import atc.main as main

    monkeypatch.setattr(main, "LANGUAGE", ["es"])


def test_phrase_tables_both_ways():
    assert to_spanish("Martinair four one three three, Aeroparque Tower, wind three zero zero degrees one zero knots, "
                      "runway three one, cleared for takeoff.") == \
        "Martinair cuatro uno tres tres, Aeroparque Torre, viento tres cero cero grados uno cero nudos, pista tres " \
        "uno, autorizado a despegar."
    assert to_spanish("Martinair four one three three, descend to two thousand five hundred feet, QNH one zero one "
                      "three.") == "Martinair cuatro uno tres tres, descienda a dos mil quinientos pies, QNH uno cero " \
                                   "uno tres."
    assert to_spanish("Taxi to holding point runway one three via Alfa, Lima Victor Alfa Bravo Charlie.", pilot=True) \
        == "Rodamos al punto de espera pista uno tres vía Alfa, Lima Victor Alfa Bravo Charlie."
    assert to_english("Aeroparque Torre, Aerolíneas uno dos uno seis, en el punto de espera pista tres uno, listo "
                      "para salir") == "aeroparque tower, aerolineas 1 2 1 6, on holding point runway 3 1, ready for " \
                                       "departure"
    assert to_english("ciento cincuenta y cuatro personas a bordo, dos horas y treinta minutos de autonomía") == \
        "154 persons on board, 2 hours and 30 minutes fuel"
    assert to_spanish("Behind the landing Boeing seven thirty-seven, line up and wait runway one three, behind, "
                      "Argentina one two one six.", pilot=True) == \
        "Detrás del Boeing siete treinta y siete que aterriza, alineamos y mantenemos pista uno tres, detrás, " \
        "Argentina uno dos uno seis."


def test_a_departure_from_aeroparque_in_spanish(spanish):
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    net = load_network(ROOT / "airports", "SABE")
    world = World([sabe], taxi={"SABE": net})
    sim = FakeSim(sabe, callsign="MAR4133")
    stand = net.nodes[net.stands[sorted(net.stands)[0]]]
    sim.update(lat=stand[0], lon=stand[1], com1_mhz=129.3, qnh_hpa=1015.0, wind_dir_deg=300.0, wind_kt=10.0)
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", dest_name="Rosario")
    out = _Capture()
    speaker = SpanishSpeaker(out)
    h: list = []

    def call(text):
        return handle(sabe, sim, None, speaker, h, text, session=s, world=world)

    assert call("Aeroparque Autorizaciones, Martinair cuatro uno tres tres, solicito autorización a Rosario")
    assert out.said[-1].startswith("Martinair cuatro uno tres tres, Aeroparque Autorizaciones, autorizado a Rosario, "
                                   "salida ATOVO cuatro bravo, ruta según plan de vuelo")
    sq = " ".join({"0": "cero", "1": "uno", "2": "dos", "3": "tres", "4": "cuatro", "5": "cinco", "6": "seis",
                   "7": "siete"}[c] for c in PLAN.squawk)
    r = call(f"Autorizado a Rosario, salida ATOVO cuatro bravo, ascenso vía SID nivel de vuelo dos cero cero, "
             f"salidas uno dos cero decimal seis, transpondedor {sq}, Martinair cuatro uno tres tres")
    assert r.startswith("Martinair four one three three, readback correct") and "colación correcta" in out.said[-1]
    sim.update(com1_mhz=121.9)
    call("Aeroparque Superficie, Martinair 4133, solicito remolque y puesta en marcha")
    assert "remolque y puesta en marcha aprobados" in out.said[-1]
    assert call("remolque y puesta en marcha aprobados, Martinair 4133") is None
    taxi = call("Martinair 4133, solicito rodaje")
    assert "taxi to holding point runway three one" in taxi and out.said[-1].startswith(
        "Martinair cuatro uno tres tres, ruede al punto de espera pista tres uno vía ")
    via = taxi.split(" via ", 1)[1].split(", QNH")[0]
    assert call(f"rodamos al punto de espera pista tres uno vía {via}, QNH uno cero uno cinco, Martinair 4133") is None
    rwy = next(r for r in sabe.runways if r.ident == "31")
    hp = sorted(_holding_points(net, sabe, rwy))[0]
    sim.update(lat=net.nodes[hp][0], lon=net.nodes[hp][1], com1_mhz=118.85)
    call("Aeroparque Torre, Martinair 4133, en el punto de espera pista tres uno, listo para salir")
    assert out.said[-1] == "Martinair cuatro uno tres tres, Aeroparque Torre, viento tres uno cero grados uno cero " \
                           "nudos, pista tres uno, autorizado a despegar."
    assert call("autorizado a despegar pista tres uno, Martinair 4133") is None


def test_spanish_voice_bank(tmp_path):
    import json

    from atc.voices import VoiceBank

    for name, fam in (("es_AR-daniela-high", "es"), ("es_MX-claude-high", "es"), ("en_US-ryan-high", "en")):
        (tmp_path / f"{name}.onnx").write_bytes(b"")
        (tmp_path / f"{name}.onnx.json").write_text(json.dumps({"language": {"family": fam}}), encoding="utf-8")
    bank = VoiceBank(tmp_path, language="es")
    assert {v.key for v in bank.pools["generic"]} == {"es_AR-daniela-high", "es_MX-claude-high"}
    assert bank.pilot("UAL436", "US").key.startswith("es_")  # everybody speaks Spanish on this radio
    assert "ryan" not in str(bank.pools)


def test_radio_variety_courtesies_are_said_in_spanish():
    from atc.spanish import to_spanish

    assert to_spanish("good morning, Aeroparque Tower, uh, Aerolineas 1234, holding point runway 13, ready",
                      pilot=True) == "Buen día, Aeroparque Torre, eh, Aerolineas 1234, punto de espera pista 13, listo"
    assert to_spanish("Aerolineas one two three four, contact ground, have a good flight").endswith(", buen vuelo")
    assert to_spanish("cleared to land, Aerolineas one two three four, bye bye", pilot=True).endswith(", chau")
