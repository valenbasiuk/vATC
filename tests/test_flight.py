"""Whole-flight pieces: taxi routes, handoffs, code-owned takeoff/landing/traffic, fact check, providers."""

import json
import struct
from pathlib import Path

from atc import phrase
from atc.audio.tts import PrintTTS
from atc.factcheck import problems
from atc.flightplan import FlightPlan
from atc.llm.client import StubLLM, parse_endpoints
from atc.main import _Callbacks, handle
from atc.models import Airport, Frequency, OwnState, Runway, Traffic
from atc.readback import check_readback
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.taxi import departure_route, load_osm, spoken_route
from atc.traffic import describe, spoken_type, traffic_reply
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]

ORIGIN = Airport(
    icao="SATS", name="Testa Aerodrome", spoken_name="Testa", lat=-34.5592, lon=-58.4156, elevation_ft=18,
    country="AR", towered=True,
    runways=[Runway("13", 124.0, 7710, lat=-34.553902, lon=-58.425098),
             Runway("31", 304.0, 7710, lat=-34.564499, lon=-58.406101)],
    frequencies=[Frequency("CLD", 129.3), Frequency("GND", 121.9), Frequency("TWR", 118.85), Frequency("APP", 120.6)],
)
DEST = Airport(
    icao="SADX", name="Destino", spoken_name="Destino", lat=-32.9036, lon=-60.785, elevation_ft=85, country="AR",
    towered=True,
    runways=[Runway("02", 10.0, 9842, lat=-32.9168, lon=-60.7876), Runway("20", 190.0, 9842, lat=-32.8903, lon=-60.7816)],
    frequencies=[Frequency("APP", 119.9), Frequency("TWR", 118.7), Frequency("GND", 121.6)],
)
PLAN = FlightPlan(
    callsign="MAR4133", rules="I", aircraft_type="F100", origin="SATS", destination="SADX",
    destination_name="Destino", alternate=None, route="ATOVO4B ATOVO W5 PEDRO DCT", sid="ATOVO4B",
    sid_transition="ATOVO", cruise_ft=20000, planned_runway="31", dest_runway="20",
)
CS = "Martinair four one three three"


class _Quiet(PrintTTS):
    def say(self, text: str) -> None:
        pass


def _world():
    return World([ORIGIN, DEST])


def _session(**kw):
    return Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", dest_name="Destino", **kw)


# --- taxi --------------------------------------------------------------------------------------------

def _osm(tmp_path):
    """A tiny map: apron node 1 -> Bravo -> 2 -> Alfa -> 3 (holding point, runway 31 end) and Charlie to 13."""
    nodes = {1: (-34.5600, -58.4120), 2: (-34.5620, -58.4100), 3: (-34.5638, -58.4072),
             4: (-34.5560, -58.4200), 5: (-34.5545, -58.4240)}
    els = [{"type": "node", "id": i, "lat": la, "lon": lo} for i, (la, lo) in nodes.items()]
    els[2]["tags"] = {"aeroway": "holding_position", "holding_position:type": "runway"}
    els[4]["tags"] = {"aeroway": "holding_position", "holding_position:type": "runway"}
    els += [
        {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"aeroway": "taxiway", "ref": "B"}},
        {"type": "way", "id": 11, "nodes": [2, 3], "tags": {"aeroway": "taxiway", "ref": "A"}},
        {"type": "way", "id": 12, "nodes": [1, 4, 5], "tags": {"aeroway": "taxiway", "ref": "C"}},
        {"type": "node", "id": 6, "lat": -34.5601, "lon": -58.4121, "tags": {"aeroway": "parking_position", "ref": "12"}},
    ]
    p = tmp_path / "SATS.json"
    p.write_text(json.dumps({"elements": els}))
    return load_osm(p)


def _ground_own(lat=-34.5600, lon=-58.4120):
    return OwnState(lat, lon, 18, 0, 0, 0, True, 121.9, qnh_hpa=1015.0, wind_dir_deg=300.0, wind_kt=10.0)


def test_route_to_each_runway_end(tmp_path):
    net = _osm(tmp_path)
    assert departure_route(net, ORIGIN, ORIGIN.runways[1], _ground_own()) == "Bravo, Alfa"
    assert departure_route(net, ORIGIN, ORIGIN.runways[0], _ground_own()) == "Charlie"


def test_hand_written_route_wins(tmp_path):
    from dataclasses import replace

    apt = replace(ORIGIN, taxi_routes={"31": "via Delta"})
    assert departure_route(_osm(tmp_path), apt, apt.runways[1], _ground_own()) == "Delta"


def test_spoken_route():
    assert spoken_route(["B", "A1", None, "K"]) == "Bravo, Alfa one, Kilo"


def test_code_owned_taxi_clearance_and_route_readback(tmp_path):
    w = World([ORIGIN], taxi={"SATS": _osm(tmp_path)})
    sim = FakeSim(ORIGIN, callsign="MAR4133")
    sim.update(lat=-34.5600, lon=-58.4120, com1_mhz=121.9, qnh_hpa=1015.0, wind_dir_deg=300.0, wind_kt=10.0)
    s = Session(callsign="MAR4133", telephony="Martinair")
    h = []
    r = handle(ORIGIN, sim, StubLLM(), _Quiet(), h, "Testa Ground, Martinair 4133, request taxi", session=s, world=w)
    assert r == f"{CS}, Testa Ground, taxi to holding point runway three one via Bravo, Alfa, QNH one zero one five."
    assert handle(ORIGIN, sim, StubLLM(), _Quiet(), h, "holding point 31 via Bravo Alfa, Martinair 4133",
                  session=s, world=w) is None
    bad = handle(ORIGIN, sim, StubLLM(), _Quiet(), h, "holding point 31 via Bravo, Martinair 4133", session=s, world=w)
    assert bad.startswith(f"{CS}, negative, I say again, taxi to holding point runway three one via Bravo, Alfa")


def test_route_readback_accepts_letters_from_speech_to_text():
    atc = f"{CS}, taxi to holding point runway three one via Bravo, Alfa, QNH one zero one five."
    assert check_readback(atc, "Holding point runway 31 via B, A, Martinair 4133").status == "correct"


def test_arrival_taxi_to_requested_stand(tmp_path):
    w = World([ORIGIN], taxi={"SATS": _osm(tmp_path)})
    sim = FakeSim(ORIGIN, callsign="MAR4133")
    sim.update(lat=-34.5638, lon=-58.4072, com1_mhz=121.9)
    s = Session(callsign="MAR4133", telephony="Martinair", landed=True)
    r = handle(ORIGIN, sim, StubLLM(), _Quiet(), [], "Testa Ground, Martinair 4133, request taxi to stand 12",
               session=s, world=w)
    assert r == f"{CS}, Testa Ground, taxi to stand one two via Alfa, Bravo."


def test_sabe_map_from_openstreetmap_gives_a_route():
    path = ROOT / "airports" / "osm" / "SABE.json"
    if not path.exists():
        return
    from atc.airports.schema import load_airport

    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    net = load_osm(path)
    for rwy in sabe.runways:
        assert departure_route(net, sabe, rwy, _ground_own(sabe.lat, sabe.lon))
    # every taxiway name must exist on the AD chart (Valen's LIDO chart, 2026-10): no "1", no "G"
    chart = set("ABCDEFHIJKLM")
    assert {n for edges in net.edges.values() for _, _, n in edges if n} <= chart


# --- tower: takeoff and landing by code ----------------------------------------------------------------

def _tower_sim():
    sim = FakeSim(ORIGIN, callsign="MAR4133")
    sim.update(com1_mhz=118.85, wind_dir_deg=300.0, wind_kt=12.0, qnh_hpa=1015.0)
    return sim


def test_takeoff_clearance_by_code():
    sim = _tower_sim()
    r = handle(ORIGIN, sim, None, _Quiet(), [], "Testa Tower, Martinair 4133, holding point 31, ready for departure",
               session=_session())
    assert r == f"{CS}, Testa Tower, wind three zero zero degrees one two knots, runway three one, cleared for takeoff."


def test_takeoff_held_for_traffic_on_final_by_code():
    sim = _tower_sim()
    sim.add_on_final(2.0, "ARG1234", "31")
    r = handle(ORIGIN, sim, None, _Quiet(), [], "Testa Tower, Martinair 4133, ready for departure", session=_session())
    assert r == f"{CS}, Testa Tower, hold position, traffic on two miles final."


def test_landing_clearance_on_final_and_number_two_behind_traffic():
    sim = FakeSim(DEST, callsign="MAR4133")
    sim.update(com1_mhz=118.7, wind_dir_deg=190.0, wind_kt=8.0)
    sim.place_on_final(DEST, 5.0)
    w = _world()
    r = handle(DEST, sim, None, _Quiet(), [], "Destino Tower, Martinair 4133, established ILS 20", session=_session(), world=w)
    assert r == f"{CS}, Destino Tower, wind one niner zero degrees eight knots, runway two zero, cleared to land."
    sim.add_on_final(2.0, "LV-ABC", "20")
    r = handle(DEST, sim, None, _Quiet(), [], "Destino Tower, Martinair 4133, established ILS 20", session=_session(), world=w)
    assert r == f"{CS}, Destino Tower, number two, traffic to follow on two miles final, continue approach."


# --- handoffs from telemetry ---------------------------------------------------------------------------

def _watch(sim, session, world=None):
    said = []

    class Cap(PrintTTS):
        def say(self, text):
            said.append(text)

    return _Callbacks(world or _world(), sim, Cap(), [], session), said


def test_tower_hands_off_to_departure_after_takeoff_and_repeats_once():
    sim = _tower_sim()
    s = _session(clearance="confirmed")
    cb, said = _watch(sim, s)
    cb.tick(now=0)  # on the ground
    sim.set_airborne(agl_ft=400, gs_kt=150)
    assert cb.tick(now=1) is None and s.departed_from == "SATS"  # too low still
    sim.set_airborne(agl_ft=900, gs_kt=170)
    assert cb.tick(now=2) == f"{CS}, contact Testa Approach one two zero decimal six, good day."
    assert cb.tick(now=10) is None
    assert cb.tick(now=23) == f"{CS}, I say again, contact Testa Approach one two zero decimal six, good day."
    assert cb.tick(now=40) is None  # said twice, no more
    sim.update(com1_mhz=120.6)
    assert cb.tick(now=41) is None and s.pending_handoff is None


def test_radar_contact_follows_once_the_squawk_is_set():
    sim = FakeSim(ORIGIN, callsign="MAR4133")
    sim.update(com1_mhz=120.6, squawk="1200")
    sim.set_airborne(3000, 220)
    s = _session(clearance="confirmed", departed_from="SATS")
    cb, _ = _watch(sim, s)
    r = handle(ORIGIN, sim, None, _Quiet(), cb.history, "Testa Approach, Martinair 4133, passing 3000", session=s,
               world=cb.world)
    assert r == f"{CS}, Testa Approach, squawk {phrase.digits(PLAN.squawk)}, climb via SID."
    assert cb.tick(now=0) is None
    sim.update(squawk=PLAN.squawk)
    assert cb.tick(now=1) == f"{CS}, radar contact."
    assert cb.tick(now=2) is None


def test_holding_position_is_an_acknowledgement():
    from atc.readback import is_acknowledgement

    assert is_acknowledgement(f"{CS}, hold position, traffic on two miles final.", "Holding position, Martinair 4133",
                              tuple(CS.split()))


def test_readback_of_handoff_is_silent():
    sim = _tower_sim()
    s = _session(clearance="confirmed")
    cb, _ = _watch(sim, s)
    cb.tick(now=0)
    sim.set_airborne(agl_ft=900, gs_kt=170)
    cb.tick(now=1)
    r = handle(ORIGIN, sim, StubLLM(), _Quiet(), cb.history, "Approach 120.6, Martinair 4133", session=s, world=cb.world)
    assert r is None


def test_arrival_handoffs_approach_then_tower_then_ground():
    sim = FakeSim(DEST, callsign="MAR4133")
    sim.update(wind_dir_deg=190.0, wind_kt=8.0, squawk=PLAN.squawk)
    s = _session(clearance="confirmed", departed_from="SATS")
    w = _world()
    cb, said = _watch(sim, s, w)
    sim.update(com1_mhz=120.6)  # still with origin Approach
    sim.place_on_final(DEST, 35.0, 9000)
    assert cb.tick(now=0) == f"{CS}, contact Destino Approach one one niner decimal niner, good day."
    sim.update(com1_mhz=119.9)
    cb.tick(now=1)
    r = handle(DEST, sim, None, _Quiet(), cb.history, "Destino Approach, Martinair 4133, descending FL 90",
               session=s, world=w)
    assert r == f"{CS}, Destino Approach, radar contact, expect runway two zero."
    sim.place_on_final(DEST, 10.0, 3200)
    assert cb.tick(now=2) == f"{CS}, contact Destino Tower one one eight decimal seven, good day."
    sim.update(com1_mhz=118.7)
    cb.tick(now=3)
    # land, roll out, vacate: Tower -> Ground
    from atc.sequence import threshold

    lat, lon = threshold(DEST, DEST.runways[1])
    sim.update(lat=lat, lon=lon, on_ground=True, alt_agl_ft=0, alt_msl_ft=85, gs_kt=120)
    assert cb.tick(now=200) is None and s.landed
    sim.update(lat=lat, lon=lon + 0.01, gs_kt=15)  # off the runway, slow
    assert cb.tick(now=230) == f"{CS}, contact Destino Ground one two one decimal six."


# --- traffic information and fact check -------------------------------------------------------------

def _air_own(**kw):
    base = dict(lat=-34.5, lon=-58.4, alt_msl_ft=5000, alt_agl_ft=4982, gs_kt=250, heading_deg=0.0, on_ground=False,
                com1_mhz=120.6)
    base.update(kw)
    return OwnState(**base)


def test_traffic_is_said_by_code_with_type_when_known():
    own = _air_own()
    t = Traffic("ARG1234", -34.45, -58.4, 6000, 250, 180, False, type="TT:ATCCOM.AC_MODEL_A320.0.text")
    assert spoken_type(t.type) == "Airbus three twenty"
    assert describe(own, t) == "traffic, twelve o'clock, three miles, opposite direction, Airbus three twenty, " \
                               "one thousand feet above"
    t2 = Traffic("X", -34.5, -58.35, 5000, 100, 270, False)
    assert describe(own, t2) == "traffic, three o'clock, two miles, crossing right to left, same level"
    assert traffic_reply("Martinair four one three three", own, []) == "Martinair four one three three, no reported traffic."


def test_pilot_traffic_question_is_answered_by_code():
    sim = FakeSim(ORIGIN, callsign="MAR4133")
    sim.update(com1_mhz=120.6, lat=-34.5, lon=-58.4, heading_deg=0.0)
    sim.set_airborne(5000, 250)
    sim.add_traffic(Traffic("ARG1234", -34.45, -58.4, 5000 + 18 + 1000, 250, 180, False))
    r = handle(ORIGIN, sim, None, _Quiet(), [], "Martinair 4133, any traffic around?", session=_session())
    assert r == f"{CS}, traffic, twelve o'clock, three miles, opposite direction, one thousand feet above."


def test_fact_check():
    ctx = ["QNH: 1015 hectopascals, Runway in use: 31, APP 120.600", "Martinair 4133 request taxi"]
    assert problems("Martinair four one three three, taxi to holding point runway three one, QNH one zero one five.", ctx) == []
    assert problems("Martinair four one three three, contact approach one two zero decimal six.", ctx) == []
    assert "number 270" in problems("Martinair four one three three, wind two seven zero degrees.", ctx)
    assert "number 4000" in problems("Martinair four one three three, climb four thousand feet.", ctx)
    assert "aircraft type 'boeing'" in problems("Traffic, a Boeing on final.", ctx)
    assert "taxiway names" in problems("Martinair four one three three, taxi via Alfa, Bravo.", ctx)


def test_fact_check_retries_then_says_say_again():
    class Liar:
        calls = 0

        def complete(self, messages):
            Liar.calls += 1
            return "Martinair four one three three, climb and maintain eight thousand feet."

    sim = FakeSim(ORIGIN, callsign="MAR4133")
    sim.update(com1_mhz=120.6)
    sim.set_airborne(3000, 200)
    s = _session(clearance="confirmed")
    s.first_contact("approach")
    s.where = "SATS"
    s.contacted.add("SATS:approach")
    r = handle(ORIGIN, sim, Liar(), _Quiet(), [], "Testa Approach, Martinair 4133, request higher", session=s)
    assert r == f"{CS}, say again." and Liar.calls == 2


# --- providers and sim data ----------------------------------------------------------------------------

def test_provider_prefixes(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GEMINI_API_KEY", "m")
    eps = parse_endpoints("groq:llama-3.3-70b-versatile, gemini:gemini-2.5-flash-lite, nvidia/nemotron:free",
                          "https://openrouter.ai/api/v1", "o")
    assert [(e.provider, e.model, e.key) for e in eps] == [
        ("groq", "llama-3.3-70b-versatile", "g"), ("gemini", "gemini-2.5-flash-lite", "m"),
        ("openrouter", "nvidia/nemotron:free", "o")]
    assert eps[0].url == "https://api.groq.com/openai/v1"


def test_full_ai_record_with_type_and_airline():
    from atc.sim.ai_traffic import DEFINE_FULL, FULL_FMT, FULL_SIZE, RECV_ID_SIMOBJECT_DATA_BYTYPE, REQUEST_ID, \
        parse_message, to_traffic

    payload = struct.pack(FULL_FMT, -34.5, -58.4, 3000.0, 180.0, 304.0, 0, b"LV-KCD".ljust(32, b"\0"),
                          b"A320".ljust(32, b"\0"), b"Aerolineas".ljust(64, b"\0"), b"1234".ljust(8, b"\0"))
    assert len(payload) == FULL_SIZE == 180
    msg = struct.pack("<3I", 40 + len(payload), 4, RECV_ID_SIMOBJECT_DATA_BYTYPE) + \
        struct.pack("<7I", REQUEST_ID, 5, DEFINE_FULL, 0, 0, 1, 10) + payload
    _, info = parse_message(msg)
    t = to_traffic(info["record"])
    assert (t.callsign, t.type, t.airline, t.flight_number) == ("LV-KCD", "A320", "Aerolineas", "1234")


def test_telephony_from_the_airline_table_is_overridden_by_the_pilot(tmp_path, monkeypatch):
    import atc.session as sess

    f = tmp_path / "airlines.dat"
    f.write_text('412,"Aerolineas Argentinas",\\N,"AR","ARG","ARGENTINA","Argentina","Y"\n'
                 '9,"Some Air",\\N,"","SOM","SOMEAIR","X","Y"\n')
    monkeypatch.setattr(sess, "_AIRLINES", None)
    monkeypatch.setattr(sess, "table_telephony", lambda code, path=f: sess.__dict__["_load_for_test"](code, f))

    def _load(code, path):
        import csv

        rows = {r[4]: r[5].title() for r in csv.reader(path.open()) if len(r) >= 8}
        return rows.get(code)

    monkeypatch.setitem(sess.__dict__, "_load_for_test", _load)
    s = sess.Session(callsign="SOM123")
    assert s.telephony == "Someair" and s.telephony_source == "table"
    s.learn_telephony("Testa Ground, Skyline 123, request taxi")
    assert s.telephony == "Skyline" and s.telephony_source == "learned"
    s.learn_telephony("Testa Ground, Other 123")
    assert s.telephony == "Skyline"  # learned once


def test_world_picks_airport_by_frequency():
    w = _world()
    own = OwnState(-34.5, -58.4, 18, 0, 0, 0, True, 118.7)
    apt, fac = w.pick(own)
    assert apt.icao == "SADX" and fac.role == "tower"
    assert phrase.runway("09", faa=True) == "niner" and phrase.runway("09") == "zero niner"
