"""Runway sequencing (who may take off / land) and the audit fixes from the 2026-10-05 flight test."""

from atc import phrase
from atc.audio.tts import PrintTTS
from atc.flightplan import FlightPlan
from atc.llm.client import StubLLM
from atc.main import handle
from atc.models import Airport, Frequency, Runway, Traffic
from atc.runway import runway_in_use
from atc.sequence import guard, runway_status
from atc.session import Session
from atc.sim.fake import FakeSim

# Threshold positions like SABE: 13 at the NW end, 31 at the SE end.
APT = Airport(
    icao="SATS", name="Testa Aerodrome", spoken_name="Testa", lat=-34.5592, lon=-58.4156, elevation_ft=18,
    country="AR", towered=True,
    runways=[Runway("13", 124.0, 7710, lat=-34.553902, lon=-58.425098),
             Runway("31", 304.0, 7710, lat=-34.564499, lon=-58.406101)],
    frequencies=[Frequency("CLD", 129.3), Frequency("GND", 121.9), Frequency("TWR", 118.85), Frequency("APP", 120.6)],
)
R31 = APT.runways[1]
PLAN = FlightPlan(
    callsign="MAR4133", rules="I", aircraft_type="F100", origin="SATS", destination="SAAR",
    destination_name="Islas Malvinas", alternate=None, route="ATOVO4B ATOVO W5 PEDRO DCT", sid="ATOVO4B",
    sid_transition="ATOVO", cruise_ft=20000, planned_runway="31",
)


class _Quiet(PrintTTS):
    def say(self, text: str) -> None:
        pass


class _Fixed:
    """LLM that always says the same thing (to test what code does with a bad reply)."""

    def __init__(self, text):
        self.text = text

    def complete(self, messages):
        self.last = messages
        return self.text


def _sim(**own):
    sim = FakeSim(APT, callsign="MAR4133")
    sim.update(wind_dir_deg=300.0, wind_kt=10.0, qnh_hpa=1015.0, **own)
    return sim


# --- sequencing -------------------------------------------------------------------------------------

def test_traffic_on_short_final_blocks_takeoff():
    sim = _sim()
    sim.add_on_final(2.0, "ARG1234", "31")
    st = runway_status(APT, R31, sim.own(), sim.traffic(APT.lat, APT.lon, 15))
    assert st.finals and st.finals[0][0] == "ARG1234" and abs(st.finals[0][1] - 2.0) < 0.1
    assert st.takeoff_blocked() == "traffic on two mile final"


def test_traffic_far_out_allows_takeoff():
    sim = _sim()
    sim.add_on_final(7.0, "ARG1234", "31")
    st = runway_status(APT, R31, sim.own(), sim.traffic(APT.lat, APT.lon, 15))
    assert st.finals and st.takeoff_blocked() is None


def test_final_for_the_other_direction_does_not_count():
    sim = _sim()
    sim.add_on_final(2.0, "ARG1234", "13")
    st = runway_status(APT, R31, sim.own(), sim.traffic(APT.lat, APT.lon, 15))
    assert st.finals == [] and st.takeoff_blocked() is None


def test_aircraft_on_runway_blocks_takeoff_and_landing():
    sim = _sim()
    sim.add_on_runway("LAN4521", "31")
    st = runway_status(APT, R31, sim.own(), sim.traffic(APT.lat, APT.lon, 15))
    assert st.occupied_by == ["LAN4521"] and st.takeoff_blocked() == "traffic on the runway"


def test_parked_aircraft_next_to_runway_is_not_on_it():
    sim = _sim()
    sim.add_traffic(Traffic("PARKED", APT.lat + 0.003, APT.lon + 0.003, 18, 0, 0, True))  # ~400 m off
    st = runway_status(APT, R31, sim.own(), sim.traffic(APT.lat, APT.lon, 15))
    assert st.occupied_by == []


def test_landing_behind_closer_traffic_is_number_two():
    sim = _sim()
    sim.add_on_final(2.0, "ARG1234", "31")
    sim.add_on_final(5.0, "OWN", "31")
    me = sim.traffic(APT.lat, APT.lon, 15)[1]
    sim.clear_traffic()
    sim.add_on_final(2.0, "ARG1234", "31")
    sim.update(lat=me.lat, lon=me.lon, alt_msl_ft=me.alt_msl_ft, heading_deg=me.heading_deg, on_ground=False,
               alt_agl_ft=me.alt_msl_ft - 18, gs_kt=140)
    st = runway_status(APT, R31, sim.own(), sim.traffic(APT.lat, APT.lon, 15))
    assert abs(st.own_final_nm - 5.0) < 0.1
    assert st.landing_blocked() == "number two, traffic to follow on two mile final"


def test_landing_ahead_of_traffic_is_allowed():
    sim = _sim()
    sim.add_on_final(2.0, "OWN", "31")
    me = sim.traffic(APT.lat, APT.lon, 15)[0]
    sim.clear_traffic()
    sim.add_on_final(6.0, "ARG1234", "31")
    sim.update(lat=me.lat, lon=me.lon, alt_msl_ft=me.alt_msl_ft, heading_deg=me.heading_deg, on_ground=False,
               alt_agl_ft=me.alt_msl_ft - 18, gs_kt=140)
    st = runway_status(APT, R31, sim.own(), sim.traffic(APT.lat, APT.lon, 15))
    assert st.landing_blocked() is None


def test_guard_replaces_unsafe_takeoff_clearance():
    sim = _sim(com1_mhz=118.85)
    sim.add_on_final(2.0, "ARG1234", "31")
    s = Session(callsign="MAR4133", telephony="Martinair")
    llm = _Fixed("Martinair four one three three, runway three one, cleared for takeoff.")
    reply = handle(APT, sim, llm, _Quiet(), [], "Testa Tower, Martinair 4133, holding point 31, ready", session=s)
    assert reply == "Martinair four one three three, hold position, traffic on two mile final."
    assert "NOT ALLOWED" in llm.last[-1]["content"]


def test_guard_keeps_safe_takeoff_clearance():
    sim = _sim(com1_mhz=118.85)
    s = Session(callsign="MAR4133", telephony="Martinair")
    text = "Martinair four one three three, runway three one, cleared for takeoff."
    reply = handle(APT, sim, _Fixed(text), _Quiet(), [], "Testa Tower, Martinair 4133, ready", session=s)
    assert reply == text


def test_early_landing_clearance_becomes_report_final():
    from atc.sequence import RunwayStatus

    st = RunwayStatus(runway=R31)  # runway free, pilot not on final
    own = _sim().own()
    own.on_ground = False
    cs = "Lima Victor Alfa Bravo Charlie"
    # what the Groq models really said (2026-10-05 compare_models run)
    assert guard(f"{cs}, Corrientes Tower, QNH one zero two zero, runway zero two, cleared to land.", st, cs, own) \
        == f"{cs}, Corrientes Tower, QNH one zero two zero, runway zero two, report final."
    assert guard(f"{cs} Corrientes Tower, cleared to land runway zero two, report final", st, cs, own) \
        == f"{cs} Corrientes Tower, runway zero two, report final."
    assert guard(f"{cs} Corrientes Tower, runway two, QNH one zero two zero, you are cleared to land runway two, "
                 "advise when on final.", st, cs, own) \
        == f"{cs} Corrientes Tower, runway two, QNH one zero two zero, report final."


def test_guard_only_acts_on_clearances():
    from atc.sequence import RunwayStatus

    st = RunwayStatus(runway=R31, occupied_by=["X"])
    own = _sim().own()
    assert guard("Martinair four one three three, hold position.", st, "Martinair four one three three", own) \
        == "Martinair four one three three, hold position."


# --- runway in use follows the flight plan ----------------------------------------------------------

def test_calm_wind_keeps_the_planned_runway():
    assert runway_in_use(APT, None, None, "31").ident == "31"
    assert runway_in_use(APT, 0, 2, "31").ident == "31"
    assert runway_in_use(APT, None, None).ident == "13"  # no plan: longest (first of equals)


def test_wind_is_said_magnetic_when_the_variation_is_known():
    from dataclasses import replace

    from atc.runway import magnetic

    sabe = replace(APT, mag_var_deg=-10.0)  # VAR 10Â° W
    assert magnetic(sabe, 30.0) == 40.0 and magnetic(sabe, 355.0) == 5.0
    assert magnetic(APT, 30.0) == 30.0  # unknown variation: true, unchanged
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(com1_mhz=118.85, wind_dir_deg=300.0, wind_kt=12.0)
    r = handle(sabe, sim, None, _Quiet(), [], "Testa Tower, Martinair 4133, ready for departure",
               session=Session(callsign="MAR4133", telephony="Martinair"))
    assert "wind three one zero degrees one two knots" in r


def test_light_tailwind_keeps_planned_runway_strong_one_does_not():
    assert runway_in_use(APT, 124, 4, "31").ident == "31"  # 4 kt tailwind on 31
    assert runway_in_use(APT, 124, 12, "31").ident == "13"


# --- readbacks, callsigns, frequencies --------------------------------------------------------------

def _delivery(lines, **sess):
    sim = _sim(com1_mhz=129.3)
    s = Session(callsign="MAR4133", plan=PLAN, dest_name="Rosario", **sess)
    out = [handle(APT, sim, StubLLM(), _Quiet(), [], line, session=s) for line in lines]
    return out, s


def test_readback_with_numbers_back_to_back():
    out, s = _delivery(["Testa Delivery, Martinair 4133, request IFR clearance",
                        f"cleared Rosario ATOVO 4 bravo, flight level 200, 120.6, squawk {PLAN.squawk}, Martinair 4133"])
    assert s.clearance == "confirmed", out[1]


def test_readback_as_speech_to_text_writes_it():
    out, s = _delivery(["Testa Delivery, Martinair 4133, request IFR clearance",
                        f"Cleary to Rosario, Atovil for Bravo departure, climb there, Sadie. Expect flight level 200, "
                        f"departure 120 decimal 6, squawk {'-'.join(PLAN.squawk)}, Martin Air 4-1-3-3."])
    assert s.clearance == "confirmed", out[1]


def test_garbled_first_call_on_delivery_still_gets_the_clearance():
    out, s = _delivery(["Air park delivery, Martin Air 4133, to Osario."])
    assert s.telephony == "Martinair"
    assert out[0].startswith("Martinair four one three three, Testa Delivery, cleared to Rosario")


def test_radio_check_on_delivery_is_not_a_clearance_request():
    out, s = _delivery(["Testa Delivery, Martinair 4133, radio check"])
    assert s.clearance == "none"


def test_telephony_split_by_speech_to_text_is_joined():
    s = Session(callsign="MAR4133")
    s.learn_telephony("Arrow, park ground, Martin Air 4133, stand 1-2, request push and start.")
    assert s.telephony == "Martinair"


def test_registration_is_spelled():
    assert phrase.callsign(None, "LVABC") == "Lima Victor Alfa Bravo Charlie"
    assert phrase.callsign(None, "N123AB") == "November one two three Alfa Bravo"
    assert Session(callsign="LV-ABC").spoken_callsign == "Lima Victor Alfa Bravo Charlie"


def test_other_flight_is_asked_for_its_callsign():
    sim = _sim(com1_mhz=121.9)
    s = Session(callsign="MAR4133", telephony="Martinair")
    reply = handle(APT, sim, StubLLM(), _Quiet(), [], "Testa Ground, Aerolineas 1234, request taxi", session=s)
    assert reply == "Station calling Testa Ground, say again your callsign."
    assert s.names_other_flight("Austral 2701 request taxi")
    assert not s.names_other_flight("Martinair 4113 request taxi")  # one digit misheard: still us
    assert s.names_other_flight("Martinair 2701 request taxi")  # our name, a different flight
    assert not s.names_other_flight("Martinair four one three three, passing 2000 feet")
    assert not s.names_other_flight("request taxi")


def test_first_call_on_tower_is_answered_even_if_it_repeats_grounds_instruction():
    sim = _sim(com1_mhz=121.9)
    s = Session(callsign="MAR4133", telephony="Martinair")
    hist = []
    handle(APT, sim, _Fixed("Martinair four one three three, Testa Ground, taxi to holding point runway three one, "
                            "QNH one zero one five."), _Quiet(), hist, "Testa Ground, Martinair 4133, request taxi",
           session=s)
    sim.update(com1_mhz=118.85)
    reply = handle(APT, sim, _Fixed("Martinair four one three three, Testa Tower, hold position."), _Quiet(), hist,
                   "Testa Tower, Martinair 4133, holding point runway 31", session=s)
    assert reply is not None


def test_wrong_readback_correction_leaves_out_the_station_name():
    sim = _sim(com1_mhz=121.9)
    s = Session(callsign="MAR4133", telephony="Martinair")
    hist = []
    first = handle(APT, sim, StubLLM(), _Quiet(), hist, "Testa Ground, Martinair 4133, request taxi", session=s)
    assert first == "Martinair four one three three, Testa Ground, taxi to holding point runway three one, " \
                    "QNH one zero one five."
    reply = handle(APT, sim, StubLLM(), _Quiet(), hist, "holding point runway 13, Martinair 4133", session=s)
    assert reply == "Martinair four one three three, negative, I say again, taxi to holding point runway three one, " \
                    "QNH one zero one five."


def test_winds_aloft_are_not_used_as_airport_wind():
    sim = _sim(com1_mhz=120.6)
    s = Session(callsign="MAR4133", telephony="Martinair")
    llm = _Fixed("Martinair four one three three, roger.")
    handle(APT, sim, llm, _Quiet(), [], "Testa Approach, Martinair 4133, on the ground, request info", session=s)
    assert s.surface_wind == {"SATS": (300.0, 10.0)}
    sim.update(on_ground=False, alt_agl_ft=15000, alt_msl_ft=15018, wind_dir_deg=250.0, wind_kt=60.0)
    handle(APT, sim, llm, _Quiet(), [], "Testa Approach, Martinair 4133, level 150, request info", session=s)
    assert "300 degrees at 10 knots" in llm.last[-1]["content"]


def test_wrong_squawk_on_check_in_gets_the_code():
    sim = _sim(com1_mhz=120.6, squawk="2000")
    sim.set_airborne(2000, 200)
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", clearance="confirmed")
    reply = handle(APT, sim, StubLLM(), _Quiet(), [], "Testa Approach, Martinair 4133, passing 2000", session=s)
    assert reply == f"Martinair four one three three, Testa Approach, squawk {phrase.digits(PLAN.squawk)}."
    sim.update(squawk=PLAN.squawk)
    s2 = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", clearance="confirmed", departed_from="SATS")
    reply = handle(APT, sim, StubLLM(), _Quiet(), [], "Testa Approach, Martinair 4133, passing 2000", session=s2)
    assert reply == "Martinair four one three three, Testa Approach, radar contact, climb via SID to flight level two zero zero."


def test_daily_quota_skips_the_other_free_models():
    from atc.llm.client import DailyQuotaExceeded, OpenAICompatLLM

    llm = OpenAICompatLLM("https://openrouter.ai/api/v1", "k", "x/a:free,x/b:free,paid/model")
    tried = []

    def fake_call(ep, messages):
        tried.append(ep.model)
        if ep.model.endswith(":free"):
            raise DailyQuotaExceeded("free-models-per-day")
        return "ok"

    llm._call = fake_call
    assert llm.complete([]) == "ok" and tried == ["x/a:free", "paid/model"]
    tried.clear()
    assert llm.complete([]) == "ok" and tried == ["paid/model"]  # remembered for the rest of the run


def test_traffic_is_described_from_the_pilots_point_of_view():
    from atc.llm.prompt import build_context

    sim = _sim(heading_deg=0.0)
    sim.add_traffic(Traffic("ARG1234", APT.lat, APT.lon + 0.05, 1018, 140, 304, False))  # ~2.5 NM east
    ctx = build_context(sim.own(), sim.traffic(APT.lat, APT.lon, 15), APT)
    assert "from the pilot: 3 o'clock, 2.5 NM, 1000 ft above" in ctx
    assert "NW bound" in ctx and "type unknown" in ctx
