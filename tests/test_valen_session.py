"""Replay of Valen's first full text session at SABE (2026-10-05), with what ATC must answer at each step.

Bugs it caught: 'LATAM1302' slipped past the callsign check; 'when ready for push, wilco' got a 'roger';
'finished start and pushback, ready to taxi' got push approved twice; 'three one via alfa' and
'holding point for 31 via alpha' were rejected; after a correct readback every later call was checked against it
again and the corrections nested ('negative, I say again, negative, I say again, ...'); 'on holding point alpha
for runway 31' never got 'contact Tower'.
"""

from pathlib import Path

from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan
from atc.main import _Callbacks, handle
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.taxi import load_network
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]
CS = "Martinair four one three three"
PLAN = FlightPlan(
    callsign="MAR4133", rules="I", aircraft_type="F100", origin="SABE", destination="SAAR",
    destination_name="Islas Malvinas", alternate=None, route="ATOVO4B ATOVO W5 PEDRO DCT ESKON DCT", sid="ATOVO4B",
    sid_transition="ATOVO", cruise_ft=20000, planned_runway="31",
)


class _Quiet(PrintTTS):
    def say(self, text: str) -> None:
        pass


class _Roger:
    """A model that answers every call with '<callsign>, roger.' (what the LLM did to 'wilco')."""

    calls = 0

    def complete(self, messages):
        _Roger.calls += 1
        return f"{CS}, roger."


def _setup():
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    world = World([sabe], taxi={"SABE": load_network(ROOT / "airports", "SABE")})
    sim = FakeSim(sabe, callsign="MAR4133")
    session = Session(callsign="MAR4133", plan=PLAN, dest_name="Rosario", standby_chance=0.0)
    return sabe, world, sim, session


def test_valens_session_replay():
    sabe, world, sim, s = _setup()
    h: list = []
    llm = _Roger()

    def call(text):
        return handle(sabe, sim, llm, _Quiet(), h, text, session=s, world=world)

    # wrong callsign (Valen's slip), with and without a space between name and number
    for text in ("Aeroparque good afternoon. LATAM 1302 radio checking.", "LATAM 1302.", "Aeroparque ground, LATAM1302.",
                 "LATAM1302"):
        assert call(text) == "Station calling Aeroparque Ground, say again your callsign.", text
    # clearance asked on Ground -> Delivery; readback of that is silent
    assert call("Roger, Martinair 4133 requesting IFR clearance onto Rosario.") == \
        f"{CS}, contact Delivery one two niner decimal three."
    assert call("Delivery on one two nine decimal three, Roger.") is None
    sim.update(com1_mhz=129.3)
    r = call("Aeroparque delivery, martinair 4133 requesting ifr clearance onto rosario.")
    assert r.startswith(f"{CS}, Aeroparque Delivery, cleared to Rosario, ATOVO four bravo departure")
    assert call("Martinair 4133 is cleared to rosario via the atovo four bravo departure, then as filed. climbing via "
                "SID, expecting 200 ten after. Departure will be 120.6, squawking 2235") == \
        f"{CS}, readback correct. When ready for push and start, contact Ground one two one decimal niner."
    assert call("when ready for push wilco ground on 121.9") is None  # was "roger" from the model
    sim.update(com1_mhz=121.9)
    assert call("Aeroparque ground good afternoon, martinair 4133 is ready for start n push with juliet. "
                "IFR to rosario.") == f"{CS}, Aeroparque Ground, push and start approved."
    taxi = call("Aeroparque ground, martinair 4133 finished start and pushback. ready to taxi with juliet ifr.")
    assert taxi.startswith(f"{CS}, taxi to holding point runway three one via ")  # was a second push approval
    # short but complete readbacks are accepted
    route = taxi.split(" via ", 1)[1].split(",")[0].rstrip(".")
    assert call(f"three one via {route} for martinair four one three three.") is None
    # after the correct readback, reporting at the holding point gets Tower, never "negative, I say again"
    assert call("Martinair 4133 on holding point alpha for runway 31") == \
        f"{CS}, contact Aeroparque Tower one one eight decimal eight five."
    assert call("Tower 118.85, Martinair 4133") is None
    assert not any("negative" in a for _, a in h)
    assert _Roger.calls == 0  # every one of these was answered (or not) by code


def test_holding_point_for_readback_and_no_nested_corrections():
    sabe, world, sim, s = _setup()
    s.clearance = "confirmed"
    sim.update(com1_mhz=121.9)
    h: list = []
    taxi = handle(sabe, sim, None, _Quiet(), h, "Aeroparque ground, martinair 4133 ready to taxi", session=s, world=world)
    route = taxi.split(" via ", 1)[1].split(",")[0].rstrip(".")
    bad1 = handle(sabe, sim, None, _Quiet(), h, "holding point 13, Martinair 4133", session=s, world=world)
    bad2 = handle(sabe, sim, None, _Quiet(), h, "holding point 13 again, Martinair 4133", session=s, world=world)
    assert bad1.count("negative, I say again") == 1 and bad2.count("negative, I say again") == 1
    assert handle(sabe, sim, None, _Quiet(), h, f"holding point for 31 via {route}, martinair 4133", session=s,
                  world=world) is None


def test_roger_to_the_clearance_gets_read_back_request():
    # Valen's second session: "Roger that" to the clearance, then push and start straight away on Delivery
    sabe, world, sim, s = _setup()
    s.telephony = "Martinair"
    sim.update(com1_mhz=129.3)
    h: list = []

    def call(text):
        return handle(sabe, sim, None, _Quiet(), h, text, session=s, world=world)

    assert call("Aeroparque delivery good afternoon, martinair 4133 requesting ifr clearance onto rosario.") \
        .startswith(f"{CS}, Aeroparque Delivery, cleared to Rosario")
    assert call("Roger that. martinair 4133.") == f"{CS}, read back the clearance."
    assert call("Martinair 4133 requesting pushback and start") == f"{CS}, read back the clearance."
    again = call("Delivery, martinair 4133, request IFR clearance")  # asked again: given again
    assert again.startswith(f"{CS}, cleared to Rosario, ATOVO four bravo departure")
    assert call("Cleared Rosario, ATOVO 4 bravo, flight level 200, departure 120.6, squawk 2235, Martinair 4133") \
        .startswith(f"{CS}, readback correct")
    # Ground still refuses push to a flight that never read the clearance back (the other session): kept
    s2 = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", clearance="issued")
    sim.update(com1_mhz=121.9)
    assert handle(sabe, sim, None, _Quiet(), [], "Aeroparque ground, martinair 4133 requesting push and start",
                  session=s2, world=world) == f"{CS}, no clearance received yet. Contact Delivery one two niner decimal three."


def test_roger_to_a_taxi_clearance_gets_read_back_then_it_is_checked():
    sabe, world, sim, s = _setup()
    s.clearance, s.telephony = "confirmed", "Martinair"
    sim.update(com1_mhz=121.9, qnh_hpa=1015.0)
    h: list = []

    def call(text):
        return handle(sabe, sim, None, _Quiet(), h, text, session=s, world=world)

    taxi = call("Aeroparque ground, martinair 4133 ready to taxi")
    assert taxi.endswith("QNH one zero one five.")
    route = taxi.split(" via ", 1)[1].split(",")[0]
    assert call("Roger, Martinair 4133") == f"{CS}, read back."
    assert call("Wilco") == f"{CS}, read back."  # still owed
    bad = call(f"holding point 31 via {route}, Martinair 4133")  # QNH left out
    assert bad.startswith(f"{CS}, negative, I say again, taxi to holding point runway three one")
    assert call(f"holding point 31 via {route}, QNH 1015, Martinair 4133") is None


def test_no_read_back_needed_for_handoffs_and_approvals():
    from atc.readback import needs_readback, readback_missing

    cs = tuple(CS.split())
    for atc in (f"{CS}, contact Aeroparque Tower one one eight decimal eight five.",
                f"{CS}, Aeroparque Ground, push and start approved.",
                f"{CS}, hold position, traffic on two mile final.",
                f"{CS}, readback correct. When ready for push and start, contact Ground one two one decimal niner."):
        assert not needs_readback(atc) and not readback_missing(atc, "Roger, Martinair 4133", cs), atc
    takeoff = f"{CS}, wind three zero zero degrees one zero knots, runway three one, cleared for takeoff."
    assert readback_missing(takeoff, "Roger, Martinair 4133", cs)
    sid = f"{CS}, Aeroparque Approach, radar contact, climb via SID."
    assert readback_missing(sid, "Roger, Martinair 4133", cs)
    assert not readback_missing(sid, "Climbing via SID, Martinair 4133", cs)


def test_third_session():
    """Valen's third session (2026-10-05): frequency requests, '200 10 after', 'have a nice day', Center."""
    sabe, world, sim, s = _setup()
    s.telephony = "Martinair"
    h: list = []

    def call(text, llm=None):
        return handle(sabe, sim, llm, _Quiet(), h, text, session=s, world=world)

    sim.update(com1_mhz=121.9)
    assert call("Aeroparque ground, requesting delivery frequency.") == \
        f"{CS}, Aeroparque Delivery one two niner decimal three."  # was the model: "...three zero zero"
    assert call("Roger that thank you martinair 4133.") is None
    sim.update(com1_mhz=129.3)
    assert call("Aeroparque delivery good afternoon, martinair 4133 requesting ifr clearance to rosario with juliet.") \
        .startswith(f"{CS}, Aeroparque Delivery, cleared to Rosario")
    assert call("Roger that, martinair 4133 is cleared to rosario with the atovo 4 bravo departure, then as filed. "
                "climbing via sid, expecting 200 10 after. departure on 120.6 squawking 2235.") \
        .startswith(f"{CS}, readback correct")  # "200 10" was read as 20010
    sim.update(com1_mhz=121.9)
    s.contacted.add("SABE:ground")
    assert call("Aeroparque ground martinair 4133 on holding point alpha for runway 31") == \
        f"{CS}, contact Aeroparque Tower one one eight decimal eight five."
    assert call("Tower on 118.85 have a nice day.") is None  # was "contact Tower" again
    # in the air, with nowhere to send them (no Center frequency on file): remain this frequency, never invent one
    sim.update(com1_mhz=120.6)
    sim.set_airborne(20000, 400)
    s.departed_from = "SABE"
    assert call("aeroparque approach requesting frequency change to center") == f"{CS}, remain this frequency."
    assert call("aeroparque approach, martinair 4133, request frequency change") == f"{CS}, remain this frequency."
    # calling "Rosario Center" while tuned to Aeroparque Delivery, in the air
    sim.update(com1_mhz=129.3)
    assert call("rosario center martinair 4133 flight level 200") == \
        f"{CS}, this is Aeroparque Delivery, contact Aeroparque Approach one two zero decimal six."


def test_calling_the_wrong_position_on_the_ground():
    sabe, world, sim, s = _setup()
    s.telephony = "Martinair"
    sim.update(com1_mhz=121.9)
    r = handle(sabe, sim, None, _Quiet(), [], "Aeroparque delivery, martinair 4133, request IFR clearance",
               session=s, world=world)
    assert r == f"{CS}, this is Aeroparque Ground, contact Aeroparque Delivery one two niner decimal three."
    # Approach and Departure are the same position: "Baires departure" on 120.6 is fine
    from atc.flow import wrong_station

    sim.update(com1_mhz=120.6)
    sim.set_airborne(3000, 220)
    apt, fac = world.pick(sim.own())
    assert wrong_station(s, world, apt, fac, sim.own(), "baires departure good afternoon martinair 4133 climbing") is None


def test_model_cannot_send_you_to_a_frequency_of_another_kind_of_station():
    from atc.factcheck import contact_problems

    sabe, world, _, _ = _setup()
    st = world.stations()
    assert contact_problems(f"{CS}, contact Rosario Center one two niner decimal three.", st) == \
        ["frequency 1293 is not a control frequency"]
    assert contact_problems(f"{CS}, contact Buenos Aires Center one two five decimal two.", st) == \
        ["frequency 1252 (not on file)"]
    assert contact_problems(f"{CS}, contact Aeroparque Tower one one eight decimal eight five.", st) == []


def test_level_readback_and_number_joining():
    from atc.readback import check_readback, join_digits

    atc = f"{CS}, Aeroparque Approach, radar contact, climb flight level two zero zero."
    assert check_readback(atc, "cleared for two zero zero martinair 4133").status == "correct"
    assert check_readback(atc, "climbing flight level 180, martinair 4133").status == "incomplete"
    assert join_digits("expecting 200 10 after") == "expecting 200 10 after"
    assert join_digits("squawk 2 2 3 5 departure 1 2 0 decimal 6") == "squawk 2235 departure 1206"
    assert join_digits("118 decimal 85") == "11885"


def test_learned_telephony_is_remembered_for_the_next_flight(tmp_path, monkeypatch):
    import atc.session as sess

    monkeypatch.setattr(sess, "LEARNED_PATH", tmp_path / "telephony_learned.json")
    first = Session(callsign="MAR4133")
    assert first.telephony is None
    first.learn_telephony("Aeroparque Ground, Martinair 4133, radio check")
    nxt = Session(callsign="MAR2020")  # next flight, same airline designator
    assert nxt.telephony == "Martinair" and nxt.telephony_source == "table"
    assert nxt.spoken_callsign == "Martinair two zero two zero"


def test_watcher_hands_ground_to_tower_when_stopped_at_the_holding_point():
    sabe, world, sim, s = _setup()
    s.clearance = "confirmed"
    s.telephony = "Martinair"
    s.taxi_cleared = True  # Ground gave the taxi clearance (parked at a gate, nothing is said: see the next test)
    sim.update(com1_mhz=121.9, gs_kt=0.0)
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    assert cb.tick(now=0) is None  # at the apron
    # runway 31 holding point area: next to the SE end, ~100 m off the centreline (OSM holding point)
    net = world.taxi["SABE"]
    from atc.sequence import along_cross

    rwy = next(r for r in sabe.runways if r.ident == "31")
    hold = min((n for n, k in net.holds if k == "runway"),
               key=lambda n: abs(along_cross(sabe, rwy, *net.nodes[n])[0]))
    sim.update(lat=net.nodes[hold][0], lon=net.nodes[hold][1])
    assert cb.tick(now=1) == f"{CS}, contact Aeroparque Tower one one eight decimal eight five."


def test_no_handoff_to_tower_while_parked_at_a_gate_next_to_the_runway():
    """Real sim, SABE 2026-10-08: parked at gate 27-29 (that close to the 13 end) on Ground, the watcher said
    "contact Aeroparque Tower" twice before any call."""
    sabe, world, sim, s = _setup()
    s.clearance = "confirmed"
    from atc.own.airport import stands

    gate = next(g for g in stands("SABE", world.taxi["SABE"]) if g.ref == "28")
    sim.update(lat=gate.lat, lon=gate.lon, com1_mhz=121.9, gs_kt=0.0, wind_dir_deg=130.0, wind_kt=8.0)
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    assert cb.tick(now=0) is None and cb.tick(now=5) is None
    s.taxi_cleared = True
    assert cb.tick(now=10) is None  # still at the gate
