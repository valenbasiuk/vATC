from atc import phrase
from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan, assign_squawk
from atc.llm.client import StubLLM
from atc.main import handle
from atc.models import Airport, Frequency, Runway
from atc.session import Session
from atc.sim.fake import FakeSim

APT = Airport(
    icao="SATS", name="Testa Aerodrome", spoken_name="Testa", lat=-34.0, lon=-58.0, elevation_ft=20, country="AR",
    towered=True, runways=[Runway("13", 130.0, 7000), Runway("31", 310.0, 7000)],
    frequencies=[Frequency("CLD", 129.3), Frequency("GND", 121.9), Frequency("TWR", 118.85), Frequency("APP", 120.6)],
)
PLAN = FlightPlan(
    callsign="MAR4133", rules="I", aircraft_type="F100", origin="SATS", destination="SAAR",
    destination_name="Islas Malvinas", alternate=None, route="ATOVO4B ATOVO W5 PEDRO DCT", sid="ATOVO4B",
    sid_transition="ATOVO", cruise_ft=20000, planned_runway="31",
)
SQ = phrase.digits(PLAN.squawk)


class _Quiet(PrintTTS):
    def say(self, text: str) -> None:
        pass


def _run(lines, freq=129.3):
    sim = FakeSim(APT, callsign=PLAN.callsign)
    sim.update(com1_mhz=freq)
    session = Session(callsign=PLAN.callsign, plan=PLAN, dest_name="Rosario")
    history, replies = [], []
    for line in lines:
        if isinstance(line, float):
            sim.update(com1_mhz=line)
            continue
        replies.append(handle(APT, sim, StubLLM(), _Quiet(), history, line, session=session))
    return replies, session


def test_phrase():
    assert phrase.frequency(121.9) == "one two one decimal niner"
    assert phrase.frequency(118.85, faa=True) == "one one eight point eight five"
    assert phrase.level(20000) == "flight level two zero zero"
    assert phrase.level(3000) == "three thousand feet"
    assert phrase.procedure("ATOVO4B") == "ATOVO four bravo"
    assert phrase.callsign("Martinair", "MAR4133") == "Martinair four one three three"


def test_squawk_is_stable_and_not_special():
    sq = assign_squawk("MAR4133")
    assert sq == assign_squawk("mar4133") and len(sq) == 4 and set(sq) <= set("01234567")
    assert sq not in {"7500", "7600", "7700", "2000", "1200", "7000"}


def test_telephony_learned_from_first_call():
    replies, s = _run(["Testa Delivery, Martinair 4133, request IFR clearance to Rosario"])
    assert s.telephony == "Martinair"
    assert replies[0].startswith("Martinair four one three three, Testa Delivery, cleared to Rosario")
    assert "ATOVO four bravo departure" in replies[0] and f"squawk {SQ}" in replies[0]
    assert "departure frequency one two zero decimal six" in replies[0]


def test_wrong_position_sends_pilot_to_delivery():
    replies, s = _run(["Testa Ground, Martinair 4133, request IFR clearance"], freq=121.9)
    assert replies[0] == "Martinair four one three three, contact Delivery one two niner decimal three."
    assert s.clearance == "none"


def test_correct_readback_then_ground():
    replies, s = _run([
        "Testa Delivery, Martinair 4133, request IFR clearance to Rosario",
        f"Cleared Rosario, ATOVO four bravo departure, climb via SID expect flight level 200, "
        f"departure one two zero decimal six, squawk {PLAN.squawk}, Martinair 4133",
    ])
    assert s.clearance == "confirmed"
    assert "readback correct" in replies[1] and "contact Ground one two one decimal niner" in replies[1]
    assert "taxi" not in replies[1].lower()


def test_wrong_squawk_is_corrected():
    replies, s = _run([
        "Testa Delivery, Martinair 4133, request IFR clearance to Rosario",
        "Cleared Rosario, ATOVO 4B departure, flight level 200, departure 120.6, squawk 1200, Martinair 4133",
    ])
    assert s.clearance == "issued"
    assert replies[1] == f"Martinair four one three three, negative, I say again, squawk {SQ}."


def test_after_correction_only_the_corrected_item_is_read_back():
    replies, s = _run([
        "Testa Delivery, Martinair 4133, request IFR clearance to Rosario",
        "Cleared Rosario, ATOVO 4B departure, flight level 200, departure 120.6, squawk 1200, Martinair 4133",
        f"Squawk {PLAN.squawk}, Martinair 4133",
    ])
    assert s.clearance == "confirmed" and "readback correct" in replies[2]


def test_push_needs_clearance_then_is_approved_and_ack_is_silent():
    replies, s = _run(["Testa Ground, Martinair 4133, at the gate, request push and start"], freq=121.9)
    assert replies[0] == "Martinair four one three three, no clearance received yet. Contact Delivery one two niner decimal three."
    replies, s = _run([
        "Testa Delivery, Martinair 4133, request IFR clearance",
        f"Cleared Rosario, ATOVO four bravo departure, flight level 200, departure 120.6, squawk {PLAN.squawk}, Martinair 4133",
        121.9,
        "Testa Ground, Martinair 4133, request push and start",
        "Push and start approved, Martinair 4133",
    ])
    assert replies[2] == "Martinair four one three three, Testa Ground, push and start approved."
    assert replies[3] is None  # a readback of an approval gets no answer


def test_wrong_runway_readback_is_not_taken_as_acknowledgement():
    sim = FakeSim(APT, callsign=PLAN.callsign)
    sim.update(com1_mhz=121.9)
    session = Session(callsign=PLAN.callsign, telephony="Martinair")
    history = [("Martinair 4133 ready to taxi", "Martinair four one three three, taxi to holding point runway three one.")]
    reply = handle(APT, sim, None, _Quiet(), history, "Taxi to holding point runway 13, Martinair 4133", session=session)
    assert reply == "Martinair four one three three, negative, I say again, taxi to holding point runway three one."


def test_say_again_repeats_clearance():
    replies, _ = _run(["Testa Delivery, Martinair 4133, request IFR clearance", "Say again, Martinair 4133"])
    assert replies[1].startswith("Martinair four one three three, I say again, cleared to Rosario")
