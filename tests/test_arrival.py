"""Directs, climb, descent, vectors, approach, landing and vacating at Rosario: what Valen's first real-sim flight
(2026-10-05) was missing. Route and top of descent as in his SimBrief OFP (MAR4133 SABE-SAAR)."""

from pathlib import Path

from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.flightplan import Fix, FlightPlan
from atc.geo import bearing_deg
from atc.main import _Callbacks, handle
from atc.readback import check_readback
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.taxi import load_network
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]
CS = "Martinair four one three three"
FIXES = [Fix("EZE19", -34.123333, -58.564722, 15300, True), Fix("EZE20", -34.151389, -58.813611, 19000, True),
         Fix("EZE26", -34.171111, -58.991667, 20000, True), Fix("ATOVO", -34.061111, -59.150556, 20000),
         Fix("PEDRO", -33.701111, -59.666944, 20000), Fix("ESKON", -33.231111, -60.328889, 14600)]
PLAN = FlightPlan(
    callsign="MAR4133", rules="I", aircraft_type="F100", origin="SABE", destination="SAAR",
    destination_name="Islas Malvinas", alternate=None, route="ATOVO4B ATOVO W5 PEDRO DCT ESKON DCT", sid="ATOVO4B",
    sid_transition="ATOVO", cruise_ft=20000, planned_runway="31", dest_runway="02", fixes=FIXES,
    tod=(-33.345189, -60.169333), dest_trans_alt_ft=3000,
)


class _Quiet(PrintTTS):
    def say(self, text: str) -> None:
        pass


def _setup():
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    saar = load_airport(ROOT / "airports" / "SAAR.yaml")
    world = World([sabe, saar], taxi={"SABE": load_network(ROOT / "airports", "SABE"),
                                      "SAAR": load_network(ROOT / "airports", "SAAR")})
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(squawk=PLAN.squawk, qnh_hpa=1015.0)
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", dest_name="Rosario", clearance="confirmed",
                departed_from="SABE")
    return world, sim, s, saar


def _fly(sim, lat, lon, alt, toward=None, hdg=None, gs=250):
    hdg = hdg if hdg is not None else bearing_deg(lat, lon, *toward)
    sim.update(lat=lat, lon=lon, alt_msl_ft=alt, alt_agl_ft=alt - 20, heading_deg=hdg, gs_kt=gs, on_ground=False)


def test_directs_only_to_fixes_ahead_on_the_route():
    world, sim, s, _ = _setup()
    sim.update(com1_mhz=120.6)
    _fly(sim, -34.16, -58.90, 17000, toward=(FIXES[2].lat, FIXES[2].lon))  # between EZE20 and EZE26
    s.contacted.add("SABE:approach")
    s.where = "SABE"

    def call(t):
        return handle(world.airports[0], sim, None, _Quiet(), [], t, session=s, world=world)

    assert call("Martinair 4133 requesting direct ATOVO") == f"{CS}, proceed direct ATOVO."
    assert call("Martinair 4133 requesting direct EZE19") == f"{CS}, EZE one niner is behind you, continue as filed."
    assert call("Martinair 4133 requesting direct DORVO") == f"{CS}, unable direct DORVO, continue as filed."


def test_check_in_climbs_to_the_filed_level_and_higher_is_given():
    world, sim, s, _ = _setup()
    sim.update(com1_mhz=120.6)
    _fly(sim, -34.50, -58.45, 2700, hdg=304, gs=200)
    r = handle(world.airports[0], sim, None, _Quiet(), [], "Aeroparque approach, Martinair 4133, climbing 2700",
               session=s, world=world)
    assert r == f"{CS}, Aeroparque Approach, radar contact, climb via SID to flight level two zero zero."
    assert s.cleared_level_ft == 20000


def test_whole_arrival_into_rosario():
    world, sim, s, saar = _setup()
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    sim.update(com1_mhz=120.6)
    s.contacted.add("SABE:approach")
    # past ESKON-ish, 35 NM from Rosario at FL200: Rosario Tower (TWR/APP, no Approach on file) takes over
    _fly(sim, -33.30, -60.25, 20000, toward=(saar.lat, saar.lon))
    assert cb.tick(now=0) == f"{CS}, contact Rosario Tower one one eight decimal seven, good day."
    sim.update(com1_mhz=118.7)
    cb.tick(now=1)
    # check-in past the top of descent: the descent comes with it
    r = handle(saar, sim, None, _Quiet(), cb.history, "Rosario tower good night martinair 4133 flight level 200",
               session=s, world=world)
    assert r == f"{CS}, Rosario Tower, descend to three thousand feet, QNH one zero one five, " \
                f"expect vectors runway zero two."
    assert handle(saar, sim, None, _Quiet(), cb.history, "descend 3000 feet QNH 1015, martinair 4133",
                  session=s, world=world) is None  # correct readback
    # 20 NM out, east of the field: vectors toward the intercept point
    _fly(sim, -33.05, -60.45, 4000, toward=(saar.lat, saar.lon))
    v = cb.tick(now=10)
    assert v.startswith(f"{CS}, fly heading ") and v.endswith("vectors runway zero two, reduce speed to two one zero "
                                                             "knots.")  # 250 kt: slowed for the vectors
    assert check_readback(v, f"heading {v.split('heading ')[1].split(',')[0]}, vectors runway 02, speed 210, "
                             "Martinair 4133").status == "correct"
    # near the intercept point: turn to intercept + approach clearance
    _fly(sim, -33.06, -60.76, 3000, hdg=270)
    i = cb.tick(now=60)
    assert "cleared approach runway zero two" in i and "maintain three thousand feet until established" in i
    assert i.endswith("reduce speed to one eight zero knots.")
    # established on a 5 NM final: Tower clears to land on its own
    sim.update(wind_dir_deg=20.0, wind_kt=8.0)
    sim.place_on_final(saar, 5.0)
    assert cb.tick(now=120) == f"{CS}, wind zero two zero degrees eight knots, runway zero two, cleared to land."
    assert cb.tick(now=121) is None  # once
    # touchdown and slowing on the runway: welcome + vacate
    from atc.sequence import threshold

    lat, lon = threshold(saar, saar.runways[0])
    sim.update(lat=lat + 0.002, lon=lon, on_ground=True, alt_msl_ft=85, alt_agl_ft=0, gs_kt=110, heading_deg=10)
    cb.tick(now=200)
    sim.update(gs_kt=50)
    w = cb.tick(now=210)
    assert w.startswith(f"{CS}, welcome to Rosario, ") and "vacate" in w


def test_request_descent_before_top_of_descent_gets_expect():
    world, sim, s, saar = _setup()
    sim.update(com1_mhz=120.6)
    s.contacted.add("SABE:approach")
    s.where = "SABE"
    _fly(sim, FIXES[4].lat, FIXES[4].lon, 20000, toward=(saar.lat, saar.lon))  # at PEDRO, far before TOD
    r = handle(world.airports[0], sim, None, _Quiet(), [], "Martinair 4133 requesting descent", session=s, world=world)
    assert r.startswith(f"{CS}, expect descent in ") and r.endswith(" miles.")


def test_on_final_report_is_answered_not_taken_as_a_readback():
    from atc.readback import is_acknowledgement

    atc = f"{CS}, continue approach runway zero two, report final."
    assert not is_acknowledgement(atc, "Martinair 4133 is on final for runway zero two", tuple(CS.split()))
    assert is_acknowledgement(atc, "continue approach, wilco report final, Martinair 4133", tuple(CS.split()))


def test_fact_check_no_longer_lets_fl100_pass_as_1000_feet():
    from atc.factcheck import problems

    assert "number 100" in problems("climb flight level one zero zero", ["pattern 1000 ft AGL"])
    assert problems("climb flight level two zero zero", ["cruise 20000"]) == []
    assert problems("contact tower one one eight decimal eight five", ["TWR 118.850"]) == []


def test_intercept_behind_traffic_on_final_slows_to_160_and_names_it():
    from atc.enroute import intercept_text
    from atc.models import Traffic
    from atc.runway import runway_in_use
    from atc.sequence import threshold

    world, sim, s, saar = _setup()
    rwy = runway_in_use(saar, None, None, "02")
    _fly(sim, -33.06, -60.76, 3000, hdg=270, gs=220)  # near the intercept point, ~10 NM from the threshold
    tlat, tlon = threshold(saar, rwy)
    import math

    h = math.radians(rwy.heading_deg)
    ai = Traffic("ARG1234", tlat - 6 * math.cos(h) / 60, tlon - 6 * math.sin(h) / (60 * math.cos(math.radians(tlat))),
                 saar.elevation_ft + 1900, 150, rwy.heading_deg, False, type="A320")
    r = intercept_text(s, saar, rwy, sim.own(), [ai])
    assert r.endswith("reduce speed to one six zero knots, traffic to follow, Airbus three twenty on six mile final.")
    s2 = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair")
    sim.update(ias_kt=175.0)  # already slow (indicated airspeed, ground speed says 220): no speed restriction
    assert "reduce speed" not in intercept_text(s2, saar, rwy, sim.own(), [])
