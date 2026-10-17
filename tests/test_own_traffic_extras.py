"""Improvement plan block 6, our own traffic: turnarounds (H1), departures turning toward their destination and
arrivals joining from a downwind or a base (H2, D3), GA circuits with touch and goes and CTAF calls (H3, E3)."""

import random
from dataclasses import replace

from atc import own
from atc.chatter import RadioBus
from atc.geo import bearing_deg, distance_nm, heading_diff
from atc.models import Frequency
from atc.own import catalog, schedule
from atc.own.injector import FakeInjector
from atc.own.manager import OwnTraffic
from atc.sequence import along_cross
from atc.world import World

from test_own_traffic import APT, R13, _net, _user


def _mgr(apt=APT, bus=None, seed=4):
    w = World([apt])
    w.taxi[apt.icao] = _net()
    return OwnTraffic(w, None, FakeInjector(), bus=bus, catalog=catalog.ModelCatalog(), community=None,
                      wind=lambda a: (130.0, 8.0), rng=random.Random(seed))


def _drain(bus, said):
    while bus is not None and bus.queue:  # everything said at once (nobody on the frequency to wait for)
        ex = bus.queue.pop(0)
        said.append(" / ".join(t for _, t, _ in ex.lines))
        if ex.on_said:
            ex.on_said()


def _fly(mgr, until, bus=None, watch=None, dt=0.5, t=0.0):
    said = []
    while t < until and mgr.pilots:
        mgr.step(t, _user(), [])
        _drain(bus, said)
        if watch is not None and watch(t):
            break
        t += dt
    return t, said


# --- H2 arrivals from a downwind / base ---------------------------------------------------------------------------

def test_an_arrival_joins_from_a_downwind_and_lands_without_jumps():
    bus = RadioBus()
    mgr = _mgr(bus=bus)
    msg = mgr.spawn_arrival(APT, 0.0, schedule.Departure("ARG", "1234", "B738"), join="downwind")
    assert "joining a downwind for runway 13" in msg
    p = mgr.pilots["ARG1234"]
    states, poses = [], []

    def watch(t):
        if not states or states[-1] != p.state:
            states.append(p.state)
        poses.append((p.pose.lat, p.pose.lon, p.pose.agl_ft, p.state))
        return p.state in ("rollout", "go_around")

    _fly(mgr, 1500, bus, watch)
    assert states[:3] == ["joining", "final", "rollout"], states
    jumps = [distance_nm(a[0], a[1], b[0], b[1]) * 1852 for a, b in zip(poses, poses[1:])]
    assert max(jumps) < 60.0  # 0.5 s at 210 kt is 54 m: no teleport at the hand-over to the final
    heights = [b[2] - a[2] for a, b in zip(poses, poses[1:]) if a[3] != "rollout"]
    assert min(heights) > -16.0  # never more than ~1900 ft/min down
    join_at = next(i for i, x in enumerate(poses) if x[3] == "final")
    along, cross = along_cross(APT, R13, poses[join_at][0], poses[join_at][1])
    assert -9.5 < along < -4.0 and cross < 0.05  # onto the centerline 4-9.5 NM out


def test_an_arrival_joining_from_a_base():
    mgr = _mgr()
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("ARG", "1235", "B738"), join="base")
    p = mgr.pilots["ARG1235"]
    _fly(mgr, 1500, watch=lambda t: p.state in ("rollout", "go_around"))
    assert p.state == "rollout"


# --- H2 / D3 departures toward their destination -------------------------------------------------------------------

def test_a_departure_turns_toward_its_destination_and_stays_until_25_nm():
    mgr = _mgr()
    dep = schedule.Departure("ARG", "1216", "B738", "SAAR")  # Rosario: north-west of Testa
    mgr.spawn_departure(APT, 0.0, dep)
    p = mgr.pilots["ARG1216"]
    far = []

    def watch(t):
        if p.state == "climbout":
            far.append((distance_nm(p.pose.lat, p.pose.lon, APT.lat, APT.lon), p.pose.heading_deg, p.pose.agl_ft))
        return False

    _fly(mgr, 2400, watch=watch)
    assert not mgr.pilots  # gone in the end
    to_rosario = bearing_deg(APT.lat, APT.lon, -32.90, -60.78)
    assert heading_diff(far[-1][1], to_rosario) < 15  # turned toward it
    assert far[-1][0] > 20.0  # still in the sim (traffic for the user) well beyond the old 12 NM


# --- H1 turnarounds --------------------------------------------------------------------------------------------

def test_an_arrival_turns_around_and_departs_from_the_same_stand(monkeypatch):
    import atc.own.manager as manager

    monkeypatch.setattr(manager, "TURNAROUND_SHARE", 1.0)
    monkeypatch.setattr(manager, "TURNAROUND_S", (60.0, 60.0))
    mgr = _mgr()
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("ARG", "1234", "B738"))
    p = mgr.pilots["ARG1234"]
    assert p.turnaround_s == 60.0
    key = p.key
    t, _ = _fly(mgr, 1200, watch=lambda t: "ARG1234" not in mgr.pilots)
    new = next(iter(mgr.pilots.values()))
    assert new.flight.callsign != "ARG1234" and new.flight.airline == "ARG" and new.key == key
    assert new.kind == "departure" and new.paths.stand.ref == p.stand.ref
    assert key in mgr.injector.poses and key not in mgr.injector.removed  # the same object in the sim
    _fly(mgr, t + 900, t=t, watch=lambda t: new.state == "pushing")
    assert new.state == "pushing"


# --- H3 GA circuits, E3 CTAF calls ---------------------------------------------------------------------------------

def test_a_cessna_flies_circuits_with_touch_and_goes_then_parks():
    bus = RadioBus()
    mgr = _mgr(bus=bus)
    mgr.spawn_departure(APT, 0.0, schedule.Departure("", "LVABC", "C172"), circuits=2)
    states = []

    def watch(t):
        p = mgr.pilots.get("LVABC")
        if p is not None and (not states or states[-1] != p.state):
            states.append(p.state)
        return p is not None and p.state == "parked_in"

    t, said = _fly(mgr, 3600, bus, watch)
    assert "takeoff" in states and "joining" in states and states.count("touch_go") == 1 \
        and states[-1] == "parked_in", states
    text = "\n".join(said)
    assert "left downwind runway one three, touch and go" in text and "cleared touch and go" in text, text
    assert "full stop" in text and "cleared to land" in text


def test_ctaf_calls_where_there_is_no_tower():
    apt = replace(APT, icao="SATS", frequencies=[Frequency("CTAF", 122.8)])
    bus = RadioBus()
    mgr = _mgr(apt=apt, bus=bus)
    mgr.spawn_departure(apt, 0.0, schedule.Departure("", "LVABC", "C172"), circuits=1)
    t, said = _fly(mgr, 1800, bus, lambda t: (p := mgr.pilots.get("LVABC")) is not None and p.state == "parked_in")
    calls = [s for s in said]
    assert any(c.startswith("Testa traffic, Lima Victor Alfa Bravo Charlie, departing runway one three, remaining in "
                            "the circuit, Testa.") for c in calls), calls
    assert any("left downwind runway one three, full stop" in c for c in calls)
    assert any("clear of runway one three" in c for c in calls)
    assert not any("Tower" in c or "cleared" in c for c in calls)  # nobody clears anything there


# --- D3 traffic information about ours en route --------------------------------------------------------------------

def test_radar_calls_one_of_our_departures_converging_with_the_user():
    from atc.airports.schema import load_airport
    from atc.audio.tts import PrintTTS
    from atc.flightplan import FlightPlan
    from atc.main import _Callbacks
    from atc.models import Traffic
    from atc.session import Session
    from atc.sim.fake import FakeSim
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sabe = load_airport(root / "airports" / "SABE.yaml")
    world = World([sabe])
    sim = FakeSim(sabe, callsign="MAR4133")
    plan = FlightPlan(callsign="MAR4133", rules="I", aircraft_type="B738", origin="SABE", destination="SAAR",
                      destination_name="Rosario", alternate=None, route="DCT", sid=None, sid_transition=None,
                      cruise_ft=20000, planned_runway="31")
    s = Session(callsign="MAR4133", plan=plan, telephony="Martinair", clearance="confirmed", departed_from="SABE")
    s.contacted.add("SABE:approach")
    sim.update(com1_mhz=120.6, lat=-34.30, lon=-58.80, alt_msl_ft=8000, alt_agl_ft=7980, heading_deg=300,
               gs_kt=250, on_ground=False, squawk=plan.squawk)

    class Q(PrintTTS):
        def say(self, text):
            pass

    cb = _Callbacks(world, sim, Q(), [], s)
    ours = Traffic("ARG1216", -34.27, -58.86, 7500, 250, 120, False, type="B738", flight_number="1216")
    own.publish({"ARG1216": ours})
    assert cb.tick(now=0) is None  # first look
    own.publish({"ARG1216": replace(ours, lat=-34.275, lon=-58.85)})
    r = cb.tick(now=1)
    assert r is not None and "traffic" in r and "Boeing seven thirty-seven" in r and "opposite direction" in r, r


# --- soak test findings (2026-10-11) -------------------------------------------------------------------------------

def test_ground_approves_one_push_at_a_time_near_each_other():
    from types import SimpleNamespace

    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"))
    p = mgr.pilots["ARG1216"]
    assert not mgr._push_blocked(p, [])
    # the neighbour was approved a moment ago (its tug not moving yet): this one waits, or both push into each other
    mgr.pilots["ARG1300"] = SimpleNamespace(state="push_radio", pose=p.pose, flight=SimpleNamespace(callsign="ARG1300"))
    assert mgr._push_blocked(p, [])


def test_two_tugs_pushing_into_each_other_the_first_one_goes_on():
    from types import SimpleNamespace

    import atc.own.manager as manager

    mgr = _mgr()

    def pilot(cs, since, other):
        return SimpleNamespace(state="pushing", since=since, blocked_since=10.0, blocked_by=other, ignore=set(),
                               flight=SimpleNamespace(callsign=cs))

    a, b = pilot("ARG1", 5.0, "ARG2"), pilot("ARG2", 6.0, "ARG1")
    mgr.pilots.update({"ARG1": a, "ARG2": b})
    mgr._unstick(a, 10.0 + manager.PUSH_DEADLOCK_S - 1)
    assert not a.ignore  # not yet
    for p in (a, b):
        mgr._unstick(p, 10.0 + manager.PUSH_DEADLOCK_S)
    assert a.ignore == {"ARG2"} and not b.ignore  # only the one that started first passes


def test_no_new_arrivals_while_a_departure_waits_long_at_the_holding_point(monkeypatch):
    import atc.own.manager as manager

    mgr = _mgr()
    mgr.user = _user()
    monkeypatch.setattr(schedule, "movements_per_hour", lambda *a, **k: 30.0)
    monkeypatch.setattr(mgr, "_warm_start", lambda *a: None)
    spawned = []
    monkeypatch.setattr(mgr, "spawn_arrival", lambda *a, **k: spawned.append("arr") or "ok")
    monkeypatch.setattr(mgr, "spawn_departure", lambda *a, **k: spawned.append("dep") or "ok")
    OwnTraffic.spawn_departure(mgr, APT, 0.0, schedule.Departure("ARG", "1216", "B738"))
    p = mgr.pilots["ARG1216"]
    p.state, p.since = "holding", 0.0
    mgr.rng = random.Random(1)
    for i in range(20):
        mgr.next_spawn = 0.0
        mgr._auto_spawn(manager.DEPARTURE_WAIT_S + 10 + i)
    assert spawned and "arr" not in spawned  # departures only until the waiting one has gone


# --- line up and wait for our departures --------------------------------------------------------------------------

def _lined_up_on_runway(p):
    from atc.sequence import on_runway

    return on_runway(APT, R13, p.traffic(APT.elevation_ft))


def test_behind_the_landing_one_line_up_and_wait_then_takeoff_without_a_new_call():
    bus = RadioBus()
    mgr = _mgr(bus=bus)
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"), taxiing=1.0)
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("AEP", "1890", "B738"), dist_nm=2.4)
    dep, arr = mgr.pilots["ARG1216"], mgr.pilots["AEP1890"]
    early = []

    def watch(t):
        if arr.state == "final" and _lined_up_on_runway(dep):
            early.append(t)  # never onto the runway in front of the landing one
        return dep.state == "takeoff"

    _, said = _fly(mgr, 900, bus, watch, dt=0.25)
    tower = [x for x in said if "ARG1216" in x or "one two one six" in x]
    luaw = next(x for x in tower if "line up and wait" in x)
    assert "behind the landing Boeing seven thirty-seven on two mile final, line up and wait runway one three, " \
           "behind" in luaw and "Behind the landing Boeing seven thirty-seven, line up and wait runway one three, " \
           "behind" in luaw, luaw
    takeoff = next(x for x in tower if "cleared for takeoff" in x)
    assert "ready for departure" not in takeoff  # lined up: Tower just clears it
    assert not early and dep.state == "takeoff" and arr.state != "go_around"


def test_no_line_up_and_wait_when_the_next_arrival_is_close_behind():
    from atc.models import Traffic
    from atc.own.motion import moved

    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"), taxiing=1.0)
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("AEP", "1890", "B738"), dist_nm=2.4)
    lat, lon = moved(R13.lat, R13.lon, 304.0, 4.5 * 1852.0)  # the sim's AI 4.5 NM out behind it
    ai = [Traffic("LAN440", lat, lon, 1450, 140, 124, False, type="A320")]
    dep = mgr.pilots["ARG1216"]
    t = 0.0
    while t < 120 and dep.state == "taxiing":
        mgr.step(t, _user(), ai)
        t += 0.5
    for _ in range(10):  # Tower looks again a few times: still at the holding point
        mgr.step(t, _user(), ai)
        t += 0.5
    assert dep.state == "holding" and not dep.luaw and not dep.behind


def test_faa_line_up_and_wait_behind_a_rolling_departure_but_no_conditional():
    faa = replace(APT, country="US")
    mgr = _mgr(apt=faa)
    mgr.spawn_departure(faa, 0.0, schedule.Departure("AAL", "1216", "B738"), taxiing=1.0)
    mgr.spawn_arrival(faa, 0.0, schedule.Departure("AAL", "1890", "B738"), dist_nm=2.4)
    dep = mgr.pilots["AAL1216"]
    _fly(mgr, 120, watch=lambda t: dep.state != "taxiing")
    assert mgr._lineup_wait(dep, [q.traffic(faa.elevation_ft) for q in mgr.pilots.values()]) is None


def test_tower_counts_our_arrival_on_a_long_final_before_clearing_a_takeoff():
    """SARC soak: a backtrack takes minutes; one of ours 14 NM out (beyond the final funnel) was missed."""
    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"), taxiing=1.0)
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("AEP", "1890", "B738"), dist_nm=14.0)
    times = dict(mgr._arrival_times(mgr.pilots["ARG1216"], []))
    assert 250.0 < times["AEP1890"] < 330.0


def test_a_slow_cleared_arrival_is_not_sent_around_for_a_departure_gone_in_time():
    """Tower's go-around order goes by time: a Cessna 2.4 NM out is two minutes away, our departure lining up under
    its takeoff clearance is airborne long before."""
    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"), taxiing=1.0)
    dep = mgr.pilots["ARG1216"]
    # Tower cleared it (nobody around); now on the runway, still lining up
    t, _ = _fly(mgr, 300, watch=lambda t: dep.state == "lining_up" and _lined_up_on_runway(dep))
    assert dep.state == "lining_up" and _lined_up_on_runway(dep) and not dep.luaw
    mgr.spawn_arrival(APT, t, schedule.Departure("", "LVABC", "C172"), dist_nm=2.4)
    arr = mgr.pilots["LVABC"]
    arr.landing_cleared = arr.checked_in = True
    others = [q.traffic(APT.elevation_ft) for q in mgr.pilots.values()]
    mgr._check_cleared_arrival(arr, t, others)
    assert arr.state == "final"


def test_landing_clearance_waits_for_a_departure_about_to_line_up_unless_it_waits_for_this_one():
    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"), taxiing=1.0)
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("AEP", "1890", "B738"), dist_nm=4.0)
    dep, arr = mgr.pilots["ARG1216"], mgr.pilots["AEP1890"]
    dep.state, dep.luaw, dep.behind = "takeoff_radio", True, None  # "line up and wait": about to be on the runway
    others = [q.traffic(APT.elevation_ft) for q in mgr.pilots.values()]
    assert mgr._landing_blocked(arr, others) == "traffic departing"
    dep.behind = "AEP1890"  # "behind the landing ..., line up and wait ..., behind": it waits for this one
    assert mgr._landing_blocked(arr, others) is None


def test_a_circuit_flight_just_airborne_counts_as_an_arrival_for_the_next_takeoff():
    from types import SimpleNamespace

    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("", "LVABC", "C172"), taxiing=1.0)
    ga = mgr.pilots["LVABC"]
    ga.circuits, ga.state, ga.since = 2, "takeoff", 0.0
    ga.pose.agl_ft = 300.0  # airborne
    mgr.clock = 60.0
    other = SimpleNamespace(airport=APT, paths=SimpleNamespace(runway=R13), flight=SimpleNamespace(callsign="X"))
    assert dict(mgr._arrival_times(other, []))["LVABC"] == 240.0  # back on the final in about a lap


def test_tower_lets_a_cleared_arrival_land_behind_a_departure_already_airborne():
    from atc.models import Traffic
    from atc.own.motion import moved

    mgr = _mgr()
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("AEP", "1890", "B738"), dist_nm=0.8)
    arr = mgr.pilots["AEP1890"]
    arr.landing_cleared = arr.checked_in = True
    lat, lon = moved(R13.lat, R13.lon, 124.0, 1800.0)  # lifted off, 80 ft over the far half of the runway
    gone = Traffic("LAN440", lat, lon, APT.elevation_ft + 80, 150, 124, False, type="A320")
    mgr._check_cleared_arrival(arr, 0.0, [gone])
    assert arr.state == "final"
    rolling = Traffic("LAN440", lat, lon, APT.elevation_ft, 120, 124, True, type="A320")  # still on its wheels
    mgr._check_cleared_arrival(arr, 0.0, [rolling])
    assert arr.state == "go_around"


def test_a_crossing_waits_while_somebody_stands_where_it_comes_off_the_runway():
    """SABE soak: one of ours crossed 13/31 and stopped on it behind an aircraft standing just past the far side."""
    from types import SimpleNamespace

    from atc.models import Traffic
    from atc.own.airport import Crossing
    from atc.own.motion import Path, moved, perf
    from atc.own.pilot import _Pilot

    path = Path([(-0.005, 0.008), (0.010, 0.008)])  # north across an east-west runway at lat 0
    me = SimpleNamespace(mover=SimpleNamespace(path=path, s=395.0), perf=perf("C172"),
                         pose=SimpleNamespace(lat=path.latlon(395.0)[0], lon=0.008),
                         flight=SimpleNamespace(callsign="LVAKD"), parked_ids=set(), ignore=set())
    c = Crossing("27", 400.0, 700.0)
    lat, lon = path.latlon(760.0)  # standing on the taxiway just past the far side
    standing = Traffic("LVBLV", lat, lon, 0.0, 0.0, 180.0, True, type="C172")
    assert _Pilot.exit_blocker(me, c, [standing]) == "LVBLV"
    beside = Traffic("LVBLV", *moved(lat, lon, 90.0, 80.0), 0.0, 0.0, 180.0, True, type="C172")
    assert _Pilot.exit_blocker(me, c, [beside]) is None  # 80 m to the side: not in the way
    me.parked_ids = {"LVBLV"}
    assert _Pilot.exit_blocker(me, c, [standing]) is None  # parked at a stand: never in the way
    me.parked_ids, me.ignore = set(), {"LVBLV"}
    assert _Pilot.exit_blocker(me, c, [standing]) is None  # the last resort after a standoff: passes it


def test_continue_approach_in_the_circuit_is_not_a_landing_clearance():
    """SARC soak: "continue approach, traffic on the runway" granted the landing to a Cessna in the circuit, which
    then flew into a departure lined up there (Tower sent it around at 0.8 NM)."""
    from atc.models import Traffic
    from atc.own.motion import moved

    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("", "LVABC", "C172"), circuits=1)
    t, _ = _fly(mgr, 1800, watch=lambda t: (p := mgr.pilots.get("LVABC")) is not None and p.kind == "arrival")
    p = mgr.pilots["LVABC"]
    p.wants, p.checked_in, p.landing_cleared = "landing", False, False
    on_it = Traffic("LAN440", *moved(R13.lat, R13.lon, 124.0, 300.0), APT.elevation_ft, 0.0, 124.0, True, type="A320")
    mgr._answer_circuit(p, t, [on_it])
    assert not p.landing_cleared and p.wants == "landing" and p.checked_in  # told to continue, still asking
    mgr._answer_circuit(p, t + 1, [])
    assert p.landing_cleared  # the runway is free: cleared, without a new call


def test_a_departure_lined_up_is_held_only_by_who_is_on_the_final():
    """SARC soak: lined up under "line up and wait", it waited for a Cessna in the circuit that could not land
    because it was lined up: both waited for hours. Once on the runway, only traffic on the final holds it."""
    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"), taxiing=1.0)
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("AEP", "1890", "B738"), join="downwind")
    dep = mgr.pilots["ARG1216"]
    assert mgr.pilots["AEP1890"].state == "joining"
    assert "AEP1890" in dict(mgr._arrival_times(dep, []))  # at the holding point: it waits for the joining one
    assert "AEP1890" not in dict(mgr._arrival_times(dep, [], finals_only=True))


def test_ground_keeps_a_departure_off_the_holding_point_an_arrival_vacates_through():
    """SARC: one taxiway to the runway. A departure waiting at its holding point blocks the arrival's way off."""
    from atc.own.airport import ArrivalPaths
    from atc.own.motion import Path, moved

    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"), taxiing=0.5)
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("AEP", "1890", "B738"), dist_nm=3.0)
    dep, arr = mgr.pilots["ARG1216"], mgr.pilots["AEP1890"]
    off_rwy = moved(R13.lat, R13.lon, 124.0, 1500.0)
    hold = moved(*off_rwy, 214.0, 90.0)  # the exit's holding point, 90 m off the centerline
    away = moved(*hold, 214.0, 300.0)
    arr.paths = ArrivalPaths([off_rwy, hold, away], [15.0, 15.0, 15.0], clear_s=120.0, exit="", via="",
                             stand=dep.paths.stand)
    to_hold = Path([moved(*hold, 304.0, 400.0), hold])  # the departure's taxi route ends right there
    elsewhere = Path([moved(*hold, 304.0, 400.0), moved(*hold, 304.0, 200.0)])
    arr.state = "vacating"
    assert mgr._holding_point_on_exit(dep, to_hold) and not mgr._holding_point_on_exit(dep, elsewhere)
    arr.state = "parked_in"
    assert not mgr._holding_point_on_exit(dep, to_hold)  # gone off: Ground sends it
