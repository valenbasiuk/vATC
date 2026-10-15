"""Improvement plan block 4 (approach): visual approach (C1), ATC holding when the sequence is full (C2), minimum
sector altitudes (C4), the missed approach and a second approach (C5), weather deviations (D1). No LLM anywhere."""

import math
import re
import time
from types import SimpleNamespace
from pathlib import Path

from atc import holding, weather
from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.enroute import _signed_cross, approach_altitude, intercept_nm
from atc.flightplan import Fix, FlightPlan
from atc.geo import destination
from atc.main import _Callbacks, handle
from atc.models import Traffic
from atc.readback import check_readback
from atc.sequence import along_cross, threshold
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]
CS = "Martinair four one three three"
TO_SABE = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="B738", origin="SAAR", destination="SABE",
                     destination_name="Aeroparque", alternate=None, route="DCT", sid=None, sid_transition=None,
                     cruise_ft=20000, planned_runway="20", dest_runway="13")
SAMR_MSA = [{"name": "SRA", "lat": -34.589443, "lon": -68.394722, "radius_nm": 25.0, "mag_var": 1.0,
             "sectors": [[180.0, 4600], [270.0, 6400], [0.0, 7600], [90.0, 8200]]}]  # Navigraph airport_msa


class _Q(PrintTTS):
    def say(self, text):
        pass

    def say_as(self, text, who="ATC", voice=None):
        pass


class _NoLLM:
    calls = 0

    def complete(self, messages):
        _NoLLM.calls += 1
        return "LLM"


def _sabe_arrival(wind=(130.0, 10.0)):
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    saar = load_airport(ROOT / "airports" / "SAAR.yaml")
    world = World([saar, sabe])
    sim = FakeSim(sabe, callsign="MAR4133")
    sim.update(squawk=TO_SABE.squawk, qnh_hpa=1013.0, wind_dir_deg=wind[0], wind_kt=wind[1])
    s = Session(callsign="MAR4133", plan=TO_SABE, telephony="Martinair", clearance="confirmed", departed_from="SAAR")
    s.surface_wind["SABE"] = wind
    return world, sim, s, sabe


def _at(apt, rwy, along, cross):
    """(lat, lon) `along` NM from the threshold in the landing direction, `cross` NM right of the centerline."""
    tlat, tlon = threshold(apt, rwy)
    h = math.radians(rwy.heading_deg)
    e = along * math.sin(h) + cross * math.cos(h)
    n = along * math.cos(h) - cross * math.sin(h)
    return tlat + n / 60.0, tlon + e / (60.0 * math.cos(math.radians(tlat)))


def _say(world, sim, s, cb, text):
    apt = world.pick(sim.own())[0]
    r = handle(apt, sim, _NoLLM(), _Q(), cb.history, text, session=s, world=world)
    cb.after_turn(r is not None)
    return r


# --- C1 visual approach -----------------------------------------------------------------------------------------

def test_visual_approach_at_sabe_in_vmc_without_the_llm():
    _NoLLM.calls = 0
    world, sim, s, sabe = _sabe_arrival()
    rwy = next(r for r in sabe.runways if r.ident == "13")
    cb = _Callbacks(world, sim, _Q(), [], s)
    sim.update(com1_mhz=120.6)
    s.contacted.add("SABE:approach")
    s.descent_given = True
    s.assign_level(3000, 9000)
    lat, lon = _at(sabe, rwy, -14.0, 4.0)
    sim.update(lat=lat, lon=lon, alt_msl_ft=3000, alt_agl_ft=2980, heading_deg=rwy.heading_deg, gs_kt=200,
               on_ground=False)
    r = _say(world, sim, s, cb, "Aeroparque Approach, Martinair 4133, request visual approach")
    assert r == f"{CS}, expect visual approach runway one three, report field in sight."
    r = _say(world, sim, s, cb, "Martinair 4133, field in sight")
    assert r == f"{CS}, cleared visual approach runway one three."
    assert s.visual == "13" and s.intercept_given and s.cleared_level_ft is None
    assert _say(world, sim, s, cb, "cleared visual approach runway 13, Martinair 4133") is None  # read back: quiet
    # inside 12 NM: Approach hands over to Tower; Tower clears to land on final
    lat, lon = _at(sabe, rwy, -9.0, 1.0)
    sim.update(lat=lat, lon=lon, alt_msl_ft=2500, alt_agl_ft=2480)
    assert cb.tick(now=10) == f"{CS}, contact Aeroparque Tower one one eight decimal eight five, good day."
    sim.update(com1_mhz=118.85)
    cb.tick(now=11)
    sim.place_on_final(sabe, 5.0)
    assert cb.tick(now=20) == f"{CS}, wind one four zero degrees one zero knots, runway one three, cleared to land."
    assert _NoLLM.calls == 0


def test_visual_close_in_goes_straight_to_tower_and_imc_is_unable():
    world, sim, s, sabe = _sabe_arrival()
    rwy = next(r for r in sabe.runways if r.ident == "13")
    sim.update(com1_mhz=120.6)
    s.contacted.add("SABE:approach")
    s.descent_given = True
    lat, lon = _at(sabe, rwy, -8.0, 2.0)
    sim.update(lat=lat, lon=lon, alt_msl_ft=2500, alt_agl_ft=2480, heading_deg=rwy.heading_deg, gs_kt=180,
               on_ground=False)
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = _say(world, sim, s, cb, "Martinair 4133, runway in sight, request visual")
    assert r == f"{CS}, cleared visual approach runway one three, contact Aeroparque Tower one one eight decimal " \
                f"eight five."
    assert check_readback(r, "cleared visual runway 13, tower 118.85, Martinair 4133").status == "correct"
    # low clouds: no visual approach
    world, sim, s, sabe = _sabe_arrival()
    weather.put(weather.parse({"icaoId": "SABE", "rawOb": "SABE 111200Z 13010KT 3000 BR BKN006 12/11 Q1013",
                               "wdir": 130, "wspd": 10, "visib": 1.9, "temp": 12, "dewp": 11, "altim": 1013,
                               "clouds": [{"cover": "BKN", "base": 600}]}))
    sim.update(com1_mhz=120.6, lat=lat, lon=lon, alt_msl_ft=2500, alt_agl_ft=2480, heading_deg=rwy.heading_deg,
               gs_kt=180, on_ground=False)
    s.contacted.add("SABE:approach")
    s.descent_given = True
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = _say(world, sim, s, cb, "Martinair 4133, request visual approach")
    assert r == f"{CS}, unable visual approach, expect instrument approach runway one three."


def test_vfr_flight_following_field_in_sight_is_handed_to_tower():
    world, sim, s, sabe = _sabe_arrival()
    s.plan = None
    s.callsign, s.telephony = "LVABC", None
    s.following = True
    s.contacted.add("SABE:approach")
    sim.update(com1_mhz=120.6, lat=-34.40, lon=-58.55, alt_msl_ft=2000, alt_agl_ft=1980, heading_deg=140, gs_kt=110,
               on_ground=False)
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = _say(world, sim, s, cb, "LVABC, field in sight")
    assert r == "Lima Victor Alfa Bravo Charlie, contact Aeroparque Tower one one eight decimal eight five, good day."
    assert not s.following


# --- C5 missed approach, then a second approach -------------------------------------------------------------------

_WORD_DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "niner": "9"}


def _heading(text: str) -> float | None:
    m = re.search(r"heading ((?:\w+ ){2}\w+)", text)
    if not m:
        return None
    return float("".join(_WORD_DIGITS[w] for w in m.group(1).split()))


def test_go_around_at_sabe_ends_in_a_second_approach_without_the_llm():
    _NoLLM.calls = 0
    world, sim, s, sabe = _sabe_arrival()
    rwy = next(r for r in sabe.runways if r.ident == "13")
    cb = _Callbacks(world, sim, _Q(), [], s)
    s.contacted |= {"SABE:approach", "SABE:tower"}
    s.handoffs_done.add("SABE:tower")
    s.descent_given = s.vectors_given = s.intercept_given = s.landing_cleared = True
    sim.update(com1_mhz=118.85)
    sim.place_on_final(sabe, 0.5)
    r = _say(world, sim, s, cb, "Martinair 4133, going around")
    assert r == f"{CS}, roger, climb straight ahead to three thousand feet, contact Aeroparque Approach one two zero " \
                f"decimal six."
    assert s.cleared_level_ft == 3000
    assert _say(world, sim, s, cb, "straight ahead three thousand feet, approach 120.6, Martinair 4133") is None
    lat, lon = _at(sabe, rwy, 2.5, 0.0)  # climbing out past the far end
    sim.update(lat=lat, lon=lon, alt_msl_ft=1800, alt_agl_ft=1780, heading_deg=rwy.heading_deg, gs_kt=180)
    assert cb.tick(now=1) is None  # no Tower handoff while it climbs out
    sim.update(com1_mhz=120.6)
    r = _say(world, sim, s, cb, "Aeroparque Approach, Martinair 4133, missed approach, climbing 2000 for 3000")
    assert r.startswith(f"{CS}, Aeroparque Approach, roger, climb to three thousand feet, turn ")
    assert r.endswith("vectors runway one three.")
    said = [r]
    # fly what is said: headings magnetic -> true (SABE variation 10 W), 180 kt, the cleared altitude, then the final
    hdg = _heading(r) + sabe.mag_var_deg
    cleared_app = False
    now, alt, freq = 2.0, 1800.0, 120.6
    for _ in range(400):
        own = sim.own()
        along, cross = _signed_cross(sabe, rwy, own.lat, own.lon)
        if cleared_app and abs(cross) < 0.25 and along < 0:
            hdg = rwy.heading_deg  # established: down the centerline and the glide path
            alt = min(alt, sabe.elevation_ft + 50 + -along * 318)
        elif s.cleared_level_ft:
            alt += max(-25.0, min(25.0, s.cleared_level_ft - alt))
        lat, lon = destination(own.lat, own.lon, hdg, 180 * 5 / 3600)
        sim.update(lat=lat, lon=lon, heading_deg=hdg % 360, alt_msl_ft=alt, alt_agl_ft=alt - sabe.elevation_ft,
                   com1_mhz=freq)
        now += 5.0
        t = cb.tick(now)
        if not t:
            continue
        said.append(t)
        if _heading(t) is not None:
            hdg = _heading(t) + sabe.mag_var_deg
        if "cleared approach" in t:
            cleared_app = True
        if "contact Aeroparque Tower" in t:
            freq = 118.85
        if "cleared to land" in t:
            break
    order = ["turn ", "cleared approach runway one three", "contact Aeroparque Tower", "cleared to land"]
    i = 0
    for text in said[1:]:
        if i < len(order) and order[i] in text:
            i += 1
    assert i == len(order), "\n".join(said)
    assert any("turn" in t and "maintain" not in t and "cleared" not in t for t in said[1:]), "a base turn first"
    assert _NoLLM.calls == 0


# --- C2 ATC holding when the sequence is full ---------------------------------------------------------------------

ROSARIO_PLAN = FlightPlan(
    callsign="MAR4133", rules="I", aircraft_type="F100", origin="SABE", destination="SAAR",
    destination_name="Islas Malvinas", alternate=None, route="DCT PEDRO DCT ESKON DCT", sid=None,
    sid_transition=None, cruise_ft=20000, planned_runway="31", dest_runway="02",
    fixes=[Fix("PEDRO", -33.701111, -59.666944, 20000), Fix("ESKON", -33.231111, -60.328889, 14600)])


def _rosario(traffic_on_final: int):
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    saar = load_airport(ROOT / "airports" / "SAAR.yaml")
    world = World([sabe, saar])
    sim = FakeSim(saar, callsign="MAR4133")
    sim.update(squawk=ROSARIO_PLAN.squawk, qnh_hpa=1015.0, wind_dir_deg=20.0, wind_kt=8.0, com1_mhz=118.7,
               zulu_s=14 * 3600 + 20 * 60)
    s = Session(callsign="MAR4133", plan=ROSARIO_PLAN, telephony="Martinair", clearance="confirmed",
                departed_from="SABE")
    s.contacted.add("SAAR:tower")
    s.where = "SAAR"  # checked in with Rosario Tower
    s.descent_given = True
    s.assign_level(5000, 20000)
    for i in range(traffic_on_final):
        sim.add_on_final(3.0 + 5.0 * i, f"ARG{1200 + i}", "02")
    # 28 NM south-east of Rosario, inbound, before any vectors
    sim.update(lat=-33.24, lon=-60.40, alt_msl_ft=5000, alt_agl_ft=4920, heading_deg=320, gs_kt=230, on_ground=False)
    return world, sim, s, saar


def test_full_sequence_gets_a_hold_then_vectors_once_it_thins_out(monkeypatch):
    world, sim, s, saar = _rosario(3)
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = cb.tick(now=0)
    assert r.startswith(f"{CS}, hold at ESKON, inbound track ") and r.endswith(
        ", right turns, maintain five thousand feet, expect approach clearance at one four three zero, delay due "
        "traffic.")
    assert s.holding == "ESKON" and s.hold_reason == "sequence"
    assert check_readback(r, "hold at ESKON, maintain 5000 feet, Martinair 4133").status == "correct"
    assert cb.tick(now=5) is None  # holding: no vectors
    # the pilot calls on the watcher's (test) clock
    monkeypatch.setattr(holding, "time", SimpleNamespace(monotonic=lambda: 5.0, time=time.time))
    r = _say(world, sim, s, cb, "Martinair 4133, request approach")
    assert r == f"{CS}, unable, continue holding, expect approach clearance at one four three zero."
    sim.clear_traffic()  # the three have landed
    sim.add_on_final(4.0, "ARG1300", "02")
    assert cb.tick(now=60) == f"{CS}, leave the hold, expect vectors runway zero two."
    assert s.holding is None and s.sequence_hold_done
    assert cb.tick(now=61) is None  # ESKON lies 29 NM out: the vectors start inside enroute.VECTORS_NM (25)
    sim.update(lat=-33.20, lon=-60.62)  # 20 NM south of Rosario
    assert cb.tick(now=120).startswith(f"{CS}, fly heading ")


def test_no_hold_with_two_ahead_and_a_published_hold(monkeypatch):
    world, sim, s, saar = _rosario(2)
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = cb.tick(now=0)
    assert r is None or "hold" not in r
    world, sim, s, saar = _rosario(3)
    monkeypatch.setattr(holding, "published", lambda ident, near: (310.0, "L"))
    cb = _Callbacks(world, sim, _Q(), [], s)
    assert cb.tick(now=0).startswith(f"{CS}, hold at ESKON as published, maintain five thousand feet, expect "
                                     "approach clearance at ")


def test_our_own_traffic_queued_on_final_holds_the_user(monkeypatch):
    """With our own traffic (atc.own) filling the final, the user's arrival is held."""
    from atc.own import manager
    from atc.own.airport import Stand
    from atc.own.injector import FakeInjector
    from atc.own.manager import OwnTraffic

    from atc.taxi import load_network

    world, sim, s, saar = _rosario(0)
    net = world.taxi["SAAR"] = load_network(ROOT / "airports", "SAAR", use_navdb=False)  # OpenStreetMap
    # Rosario's parking spots come from the sim's db (off in tests): three stands on taxi map nodes
    off_runway = [k for k, (la, lo) in net.nodes.items()
                  if all(abs(along_cross(saar, r, la, lo)[1]) > 0.15 for r in saar.runways)]
    spots = [Stand(str(i + 1), *net.nodes[k], 0.0, "GATE_M", 30.0) for i, k in enumerate(off_runway[::20][:3])]
    monkeypatch.setattr(manager, "stands", lambda icao, n: spots)
    mgr = OwnTraffic(world, sim, FakeInjector(), wind=lambda a: (20.0, 8.0))
    from atc.own.schedule import Departure

    for i, nm in enumerate((4.0, None, None)):
        msg = mgr.spawn_arrival(saar, 0.0, dep=Departure(f"ARG{1500 + i}", "1500", "B738"), dist_nm=nm)
        assert "final runway 02" in msg, msg
    mgr.step(0.0, sim.own(), [])
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = cb.tick(now=0)
    assert r is not None and r.startswith(f"{CS}, hold at ESKON") and "delay due traffic" in r
    mgr.close()


# --- C4 minimum sector altitudes ----------------------------------------------------------------------------------

def _samr():
    samr = load_airport(ROOT / "airports" / "SAMR.yaml")
    samr.msa = [dict(m) for m in SAMR_MSA]
    return samr


def test_samr_arrival_never_vectored_below_the_msa():
    from atc import terrain

    samr = _samr()
    rwy = next(r for r in samr.runways if r.ident == "11")  # landing east: the final comes in from the Andes side
    plan = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="B738", origin="SABE", destination="SAMR",
                      destination_name="San Rafael", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=20000, planned_runway="31", dest_runway="11")
    world = World([samr])
    sim = FakeSim(samr, callsign="MAR4133")
    s = Session(callsign="MAR4133", plan=plan, telephony="Martinair", clearance="confirmed", departed_from="SABE")
    s.contacted.add("SAMR:tower")
    s.where = "SAMR"
    # 30 NM north-east (the plains, 4600 ft there), at FL150, inbound
    lat, lon = destination(samr.lat, samr.lon, 45.0, 30.0)
    sim.update(com1_mhz=118.1, lat=lat, lon=lon, alt_msl_ft=15000, alt_agl_ft=12500, heading_deg=225, gs_kt=250,
               on_ground=False, wind_dir_deg=110.0, wind_kt=10.0, qnh_hpa=1016.0)
    own = sim.own()
    alt = approach_altitude(samr, rwy, own, s)
    assert alt >= 8200  # the intercept point lies 18 NM west, under the 8200 ft sector
    assert intercept_nm(samr, alt) >= 17
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = cb.tick(now=0)
    assert r is not None and "descend to eight thousand two hundred feet" in r, r
    # fly the vectors and check every assigned altitude against the floor under the aircraft
    hdg, now = 225.0, 0.0
    for _ in range(300):
        own = sim.own()
        floor = terrain.floor_at(samr, own.lat, own.lon)
        if s.cleared_level_ft and floor and not s.intercept_given:
            assert s.cleared_level_ft >= floor, (s.cleared_level_ft, floor)
        lat, lon = destination(own.lat, own.lon, hdg, 210 * 5 / 3600)
        target = s.cleared_level_ft or own.alt_msl_ft
        alt = own.alt_msl_ft + max(-150.0, min(150.0, target - own.alt_msl_ft))
        sim.update(lat=lat, lon=lon, heading_deg=hdg % 360, alt_msl_ft=alt, alt_agl_ft=alt - samr.elevation_ft)
        now += 5.0
        t = cb.tick(now)
        if t and _heading(t) is not None:
            hdg = _heading(t) + (samr.mag_var_deg or 0.0)
        if t and "cleared approach" in t:
            assert "maintain eight thousand two hundred feet until established" in t, t
            break
    else:
        raise AssertionError("never cleared for the approach")


def test_sarc_flat_msa_changes_nothing():
    sarc = load_airport(ROOT / "airports" / "SARC.yaml")
    sarc.msa = [{"name": "CRR", "lat": -27.437334, "lon": -58.761776, "radius_nm": 25.0, "mag_var": -10.0,
                 "sectors": [[180.0, 3000]]}]
    rwy = next(r for r in sarc.runways if r.ident == "20")
    s = Session(callsign="MAR4133")
    sim = FakeSim(sarc)
    lat, lon = destination(sarc.lat, sarc.lon, 20.0, 25.0)
    sim.update(lat=lat, lon=lon, alt_msl_ft=9000, on_ground=False)
    assert approach_altitude(sarc, rwy, sim.own(), s) == 3000
    assert intercept_nm(sarc, 3000) == 10.0


def test_low_altitude_warning_below_the_msa():
    samr = _samr()
    plan = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="B738", origin="SABE", destination="SAMR",
                      destination_name="San Rafael", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=20000, planned_runway="31", dest_runway="11")
    world = World([samr])
    sim = FakeSim(samr, callsign="MAR4133")
    s = Session(callsign="MAR4133", plan=plan, telephony="Martinair", clearance="confirmed", departed_from="SABE")
    s.contacted.add("SAMR:tower")
    s.where = "SAMR"
    s.descent_given = True
    lat, lon = destination(samr.lat, samr.lon, 315.0, 15.0)  # north-west, toward the Andes: 8200 ft
    sim.update(com1_mhz=118.1, lat=lat, lon=lon, alt_msl_ft=6500, alt_agl_ft=4000, heading_deg=135, gs_kt=200,
               on_ground=False, qnh_hpa=1016.0)
    cb = _Callbacks(world, sim, _Q(), [], s)
    assert cb.tick(now=0) == f"{CS}, low altitude warning, check your altitude immediately, QNH one zero one six, " \
                             "the minimum flight altitude is eight thousand two hundred feet."
    assert cb.tick(now=1) is None  # once
    sim.update(alt_msl_ft=8500)
    cb.tick(now=2)
    assert not s.low_alt_warned


# --- D1 weather deviations ----------------------------------------------------------------------------------------

def test_weather_deviation_icao_and_back_on_track():
    world, sim, s, saar = _rosario(0)
    sim.update(com1_mhz=135.5, lat=-33.9, lon=-59.4, alt_msl_ft=20000, alt_agl_ft=19900, heading_deg=310)
    world.airspaces.append(load_airport(ROOT / "airspace" / "SAEF.yaml"))
    s.contacted.add("SAEF:control")
    cb = _Callbacks(world, sim, _Q(), [], s)
    r = _say(world, sim, s, cb, "Ezeiza Control, Martinair 4133, request deviation 20 degrees left of track due weather")
    assert r == f"{CS}, deviation up to two zero degrees left of track approved, report back on track."
    assert s.deviating
    assert _say(world, sim, s, cb, "Martinair 4133, back on track") == f"{CS}, roger."
    assert not s.deviating


def test_weather_deviation_faa_names_the_next_fix():
    from atc.world import load_world

    plan = FlightPlan(callsign="UAL436", rules="I", aircraft_type="B738", origin="KLAX", destination="KSFO",
                      destination_name="San Francisco", alternate=None, route="DCT DOTNE", sid=None,
                      sid_transition=None, cruise_ft=10000, planned_runway=None,
                      fixes=[Fix("DOTNE", 37.718661, -122.614267, 10000)])
    world = load_world("KSFO", ROOT / "airports", plan, use_navdb=False)
    ksfo = world.get("KSFO")
    app = next(f.mhz for f in ksfo.frequencies if f.kind == "APP")
    sim = FakeSim(ksfo, callsign="UAL436")
    sim.update(com1_mhz=app, lat=37.4, lon=-122.5, alt_msl_ft=10000, alt_agl_ft=9990, on_ground=False,
               heading_deg=330, gs_kt=250)
    s = Session(callsign="UAL436", plan=plan)
    s.contacted.add("KSFO:approach")
    r = handle(ksfo, sim, None, _Q(), [], "United 436, request deviation right for weather", session=s, world=world)
    assert r == "United four thirty-six, deviation right of course approved, when able proceed direct DOTNE."
    r = handle(ksfo, sim, None, _Q(), [], "United 436, request heading 360 due to weather", session=s, world=world)
    assert r == "United four thirty-six, heading three six zero approved, advise clear of weather."
