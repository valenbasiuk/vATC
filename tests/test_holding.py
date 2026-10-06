"""Holding on request (holding.py): the hold with level and expected further clearance time, no descent or vectors
while holding, out of the hold on request or at the EFC time."""

from pathlib import Path

import atc.holding as holding
from atc.airports.schema import load_airport
from atc.audio.tts import PrintTTS
from atc.flightplan import Fix, FlightPlan
from atc.main import _Callbacks, handle
from atc.readback import check_readback
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import World

ROOT = Path(__file__).resolve().parents[1]
CS = "Martinair four one three three"
FIXES = [Fix("PEDRO", -33.701111, -59.666944, 20000), Fix("ESKON", -33.231111, -60.328889, 14600)]
PLAN = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="F100", origin="SABE", destination="SAAR",
                  destination_name="Rosario", alternate=None, route="DCT PEDRO DCT ESKON DCT", sid=None,
                  sid_transition=None, cruise_ft=20000, planned_runway="31", dest_runway="02", fixes=FIXES)


class _Quiet(PrintTTS):
    def say(self, text):
        pass


def _setup():
    sabe = load_airport(ROOT / "airports" / "SABE.yaml")
    saar = load_airport(ROOT / "airports" / "SAAR.yaml")
    world = World([sabe, saar])
    sim = FakeSim(sabe, callsign="MAR4133")
    # between PEDRO and ESKON at FL200 on Ezeiza... here the SABE approach frequency stands in for a radar position
    sim.update(com1_mhz=120.6, lat=-33.5, lon=-59.95, alt_msl_ft=20000, alt_agl_ft=19900, on_ground=False,
               heading_deg=310, gs_kt=400, squawk=PLAN.squawk, zulu_s=14 * 3600 + 20 * 60 + 10, qnh_hpa=1015)
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", clearance="confirmed", departed_from="SABE")
    s.contacted.add("SABE:approach")
    s.where = "SABE"
    return world, sabe, sim, s


def test_hold_at_a_route_fix_then_leave_on_request():
    world, sabe, sim, s = _setup()
    s.cleared_level_ft = 20000
    r = handle(sabe, sim, None, _Quiet(), [], "Martinair 4133, request holding at ESKON", session=s, world=world)
    assert r.startswith(f"{CS}, hold at ESKON, inbound track ") and r.endswith(
        ", right turns, maintain flight level two zero zero, expect further clearance at one four three five.")
    assert s.holding == "ESKON"
    assert check_readback(r, "hold at ESKON, maintain FL200, Martinair 4133").status == "correct"
    r = handle(sabe, sim, None, _Quiet(), [], "Martinair 4133, ready to leave the hold, request approach",
               session=s, world=world)
    assert r == f"{CS}, leave the hold, expect vectors runway zero two."
    assert s.holding is None and not s.vectors_given


def test_no_fix_named_holds_at_the_next_route_fix_and_the_efc_releases_it():
    world, sabe, sim, s = _setup()
    r = handle(sabe, sim, None, _Quiet(), [], "Martinair 4133, request hold", session=s, world=world, )
    assert r.startswith(f"{CS}, hold at ESKON")
    cb = _Callbacks(world, sim, _Quiet(), [], s)
    s.holding_until = 100.0
    assert cb.tick(now=50.0) is None  # holding: no descent, no vectors
    assert cb.tick(now=101.0) == f"{CS}, leave the hold, expect vectors runway zero two."


def test_faa_published_hold(monkeypatch):
    monkeypatch.setattr(holding, "published", lambda ident, near: (101.0, "R"))
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
               heading_deg=330, gs_kt=250, zulu_s=23 * 3600 + 58 * 60)
    s = Session(callsign="UAL436", plan=plan)
    s.contacted.add("KSFO:approach")
    r = handle(ksfo, sim, None, _Quiet(), [], "United 436, request hold at DOTNE", session=s, world=world)
    assert r == ("United four thirty-six, hold west of DOTNE as published, maintain one zero thousand, "
                 "expect further clearance zero zero one five.")  # past midnight


def test_rosario_tower_doing_approach_work_gives_the_hold_and_own_nav_ends_it():
    world, sabe, sim, s = _setup()
    saar = world.get("SAAR")
    sim.update(com1_mhz=118.7, lat=-33.30, lon=-60.25)  # Rosario TWR/APP, ~35 NM out
    s.contacted.add("SAAR:tower")
    s.cleared_level_ft = 9000
    r = handle(saar, sim, None, _Quiet(), [], "Rosario Tower, Martinair 4133, request hold at ESKON", session=s,
               world=world)
    assert r.startswith(f"{CS}, hold at ESKON") and s.holding == "ESKON"
    r = handle(saar, sim, None, _Quiet(), [], "Martinair 4133, ready to leave the hold, request own navigation",
               session=s, world=world)
    assert r.startswith(f"{CS}, own navigation approved") and s.holding is None
