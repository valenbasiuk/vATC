"""Our own traffic (atc.own, docs/OWN_TRAFFIC_PLAN.md phase 1): model choice, kinematics, the departure paths, and
a whole departure that obeys ATC and stops for the user. No sim, no Little Navmap db: a made-up airport."""

import random

from atc import own
from atc.chatter import RadioBus
from atc.models import Airport, Frequency, OwnState, Runway, Traffic
from atc.own import catalog, schedule
from atc.own.airport import departure_paths, free_stand, stands
from atc.own.injector import FakeInjector
from atc.own.manager import OwnTraffic
from atc.own.motion import GroundMover, Path, Takeoff, moved, perf
from atc.sequence import runway_status
from atc.taxi import TaxiNetwork
from atc.tracker import TrafficTracker
from atc.world import World

APT = Airport(
    icao="SATS", name="Testa Aerodrome", spoken_name="Testa", lat=-34.5592, lon=-58.4156, elevation_ft=18,
    country="AR", towered=True,
    runways=[Runway("13", 124.0, 7710, lat=-34.553902, lon=-58.425098),
             Runway("31", 304.0, 7710, lat=-34.564499, lon=-58.406101)],
    frequencies=[Frequency("GND", 121.9), Frequency("TWR", 118.85), Frequency("APP", 120.6)],
)
R13 = APT.runways[0]
RIGHT = 214.0  # right of runway 13


def _net() -> TaxiNetwork:
    """Taxiway Alfa parallel to the runway 180 m to its right, holding points 90 m off the centerline at both ends
    (Bravo, Charlie), stand 5 on a lead-in 140 m beyond Alfa, 600 m down the runway."""
    net = TaxiNetwork()
    length_m = 7710 * 0.3048

    def add(a, b, name):
        from atc.geo import distance_nm

        m = distance_nm(*net.nodes[a], *net.nodes[b]) * 1852
        net.edges.setdefault(a, []).append((b, m, name))
        net.edges.setdefault(b, []).append((a, m, name))

    along = [0.0] + [100.0 * i for i in range(1, int(length_m // 100) + 1)] + [length_m]
    for i, d in enumerate(along):
        net.nodes[i] = moved(*moved(R13.lat, R13.lon, R13.heading_deg, d), RIGHT, 180.0)
        if i:
            add(i - 1, i, "A")
    last = len(along) - 1
    net.nodes[100] = moved(*moved(R13.lat, R13.lon, R13.heading_deg, 0.0), RIGHT, 90.0)  # hold 13
    net.nodes[101] = moved(*moved(R13.lat, R13.lon, R13.heading_deg, length_m), RIGHT, 90.0)  # hold 31
    add(0, 100, "B")
    add(last, 101, "C")
    net.holds = [(100, "runway"), (101, "runway")]
    net.nodes[200] = moved(*moved(R13.lat, R13.lon, R13.heading_deg, 600.0), RIGHT, 320.0)  # stand 5
    add(6, 200, None)
    net.stands = {"5": 200}
    for node, d, a_node, name in ((300, 1300.0, 13, "D"), (301, 1800.0, 18, "E")):  # exits Delta, Echo
        net.nodes[node] = moved(R13.lat, R13.lon, R13.heading_deg, d)  # on the centerline
        add(node, a_node, name)
    return net


def _world() -> World:
    w = World([APT])
    w.taxi["SATS"] = _net()
    return w


def _user(**kw) -> OwnState:
    base = dict(lat=-34.50, lon=-58.30, alt_msl_ft=18, alt_agl_ft=0, gs_kt=0, heading_deg=0, on_ground=True,
                com1_mhz=121.9)
    base.update(kw)
    return OwnState(**base)


# --- models -------------------------------------------------------------------------------------------------

VMR = """<?xml version="1.0"?><ModelMatchRuleSet>
<ModelMatchRule TypeCode = "B738" ModelName ="FSLTL_B738_ZZZZ" />
<ModelMatchRule CallsignPrefix="FBZ" TypeCode = "B738" ModelName ="FSLTL_FAIB_B738_FBZ-FlyBondi" />
<ModelMatchRule CallsignPrefix="ARG" TypeCode = "A20N" ModelName ="FSLTL_A20N_ZZZZ" />
<ModelMatchRule CallsignPrefix="LAN" TypeCode = "A320" ModelName ="FSLTL_A320_LAN_1//FSLTL_A320_LAN_2" />
</ModelMatchRuleSet>"""


def test_model_pick_prefers_a_real_livery_then_falls_back_to_white():
    cat = catalog.ModelCatalog(catalog.parse_vmr(VMR), {"ARG A20N": ["JustFlight_AI_A320NEO_ARG"],
                                                       " A20N": ["JustFlight_AI_A320NEO_White"]})
    rng = random.Random(1)
    assert cat.pick("FBZ", "B738", rng) == "FSLTL_FAIB_B738_FBZ-FlyBondi"
    assert cat.pick("ARG", "A20N", rng) == "JustFlight_AI_A320NEO_ARG"  # FSLTL only has it white: FS Traffic's
    assert cat.pick("XYZ", "B738", rng) == "FSLTL_B738_ZZZZ"  # unknown airline: the type's white one
    assert cat.pick("LAN", "A320", rng) in ("FSLTL_A320_LAN_1", "FSLTL_A320_LAN_2")  # "A//B": one of them
    assert cat.pick("XYZ", "C17", rng) is None


def test_fs_traffic_files_parse(tmp_path):
    data = tmp_path / catalog.FST_DIR / "Data"
    (data / "DBs").mkdir(parents=True)
    (data / "Aircraft" / "aircraftIniFiles").mkdir(parents=True)
    (data / "Schedules").mkdir()
    (data / "DBs" / "aircraftCodesDB.csv").write_text("Name,Ident,Code\nBoeing 737-800,B738,738\n", encoding="utf-8")
    (data / "Aircraft" / "aircraftIniFiles" / "738.ini").write_text(
        "[fallback]\ntitle=JF_738_White\n[FBZ]\nName=Flybondi\ntitle=JF_738_FBZ\ntitle2=\n", encoding="utf-8")
    (data / "Schedules" / "SATS.ini").write_text(
        "[3]\n0=DT:0405,ICAO:SBBR,CA:ARG,AC:738,FLTNO1216,CS:ARGENTINA,DAYS:3*\n"
        "[4]\n0=DT:0500,ICAO:SAME,CA:FBZ,AC:738,FLTNO5020,CS:,DAYS:4*\n", encoding="utf-8")
    assert catalog.parse_fstraffic(data) == {" B738": ["JF_738_White"], "FBZ B738": ["JF_738_FBZ"]}
    assert schedule.todays("SATS", tmp_path, weekday=4) == [schedule.Departure("FBZ", "5020", "B738", "SAME")]
    dep = schedule.pick("SXXX", "AR", tmp_path, random.Random(0))  # not in the schedule: a local airline
    assert (dep.airline, dep.type_icao) in schedule.FALLBACK["AR"]


# --- motion ---------------------------------------------------------------------------------------------------

def test_ground_mover_turns_smoothly_and_stops_where_told():
    a = (-34.56, -58.41)
    b = moved(*a, 90.0, 100.0)
    c = moved(*b, 0.0, 100.0)
    m = GroundMover(Path([a, b, c]))
    headings = []
    while not m.done:
        m.step(0.05, 16.0, stop_s=150.0)
        headings.append(m.pose().heading_deg)
        if m.s >= 149.9 and m.v < 0.01:
            break
    assert abs(m.s - 150.0) < 0.5
    steps = [abs((b2 - b1 + 180) % 360 - 180) for b1, b2 in zip(headings, headings[1:])]
    assert max(steps) < 2.0  # never snaps round the corner
    assert any(80 < h < 100 for h in headings) and any(h < 10 or h > 350 for h in headings)


def test_takeoff_rolls_rotates_and_climbs():
    t = Takeoff(R13.lat, R13.lon, 124.0, perf("B738"))
    roll = None
    for _ in range(int(120 / 0.05)):
        t.step(0.05)
        if t.airborne and roll is None:
            roll = t.dist
    assert 1200 < roll < 2100  # metres of runway used: a 737's takeoff roll
    pose = t.pose()
    assert pose.agl_ft > 2000 and not pose.gear_down and pose.pitch_deg > 4


# --- the airport side ------------------------------------------------------------------------------------------

def test_departure_paths_push_onto_the_taxiway_nose_toward_the_runway():
    net = _net()
    st = stands("SATS", net)
    assert [s.ref for s in st] == ["5"] and abs(st[0].heading - RIGHT) < 1.0  # parked nose away from Alfa
    paths = departure_paths(net, APT, R13, st[0])
    assert paths.via == "Alfa, Bravo"
    push, taxi = Path(paths.push), Path(paths.taxi)
    assert 160 < push.length < 200  # lead-in (140 m) plus some taxiway
    nose_after_push = (push.end_heading() + 180) % 360
    assert abs(((nose_after_push - 304.0) + 180) % 360 - 180) < 10  # facing the 13 end along Alfa
    assert abs(((taxi.heading(0) - nose_after_push) + 180) % 360 - 180) < 10
    assert Path(paths.lineup).end_heading() == R13.heading_deg or \
        abs(((Path(paths.lineup).end_heading() - 124.0) + 180) % 360 - 180) < 1


def test_push_direction_phrases():
    from atc.phrase import push_direction

    # parked nose north, pushed so it ends facing west: the nose turned left, so the tail swung right
    assert push_direction(4.0, 299.0, "tail_side") == "tail right"
    assert push_direction(4.0, 299.0, "tail_cardinal") == "tail east"
    assert push_direction(4.0, 299.0, "facing") == "facing west"
    assert push_direction(214.0, 304.0, "tail_side") == "tail left"
    assert push_direction(0.0, 180.0, "tail_side") == "tail north"  # straight back and round: no side, compass


PUSH_WAYS = ("tail left", "tail east", "facing west")  # stand 5 (nose 214) to runway 13 along Alfa (304)


def test_user_push_clearance_says_where_the_tail_goes_and_a_readback_gets_silence():
    from atc.main import handle
    from atc.session import Session
    from atc.sim.fake import FakeSim
    from atc.audio.tts import PrintTTS

    class Quiet(PrintTTS):
        def say(self, text):
            pass

    net = _net()
    sim = FakeSim(APT, callsign="ARG1302")
    sim.update(lat=net.nodes[200][0], lon=net.nodes[200][1], heading_deg=RIGHT, com1_mhz=121.9,
               wind_dir_deg=130.0, wind_kt=8.0)
    s = Session(callsign="ARG1302", telephony="Argentina")
    history = []
    r = handle(APT, sim, None, Quiet(), history, "Testa Ground, Argentina 1302, stand 5, request push and start",
               session=s, world=_world())
    way = next(w for w in PUSH_WAYS if r.endswith(f", {w}."))
    assert r == f"Argentina one three zero two, Testa Ground, push and start approved, {way}."
    assert handle(APT, sim, None, Quiet(), history, f"Push and start approved, {way}, Argentina 1302", session=s,
                  world=_world()) is None
    sim.update(lat=net.nodes[10][0], lon=net.nodes[10][1])  # on Alfa, 400 m from the stand: no direction
    s2 = Session(callsign="ARG1302", telephony="Argentina")
    r = handle(APT, sim, None, Quiet(), [], "Testa Ground, Argentina 1302, request push and start", session=s2,
               world=_world())
    assert r == "Argentina one three zero two, Testa Ground, push and start approved."


def test_no_free_stand_when_somebody_is_on_it():
    st = stands("SATS", _net())
    parked = Traffic("LVABC", st[0].lat, st[0].lon, 18, 0, 0, True)
    assert free_stand(st, [parked], 36.0, APT) is None


# --- a whole departure -------------------------------------------------------------------------------------------

def _run(mgr, user, until, t=0.0, dt=0.1, watch=None):
    while t < until and mgr.pilots:
        mgr.step(t, user, [])
        if watch is not None and watch(t):
            break
        t += dt
    return t


def test_a_departure_pushes_taxis_takes_off_and_leaves():
    inj = FakeInjector()
    mgr = OwnTraffic(_world(), None, inj, catalog=catalog.ModelCatalog(), community=None,
                     wind=lambda a: (130.0, 8.0), rng=random.Random(4))
    assert "at stand 5 -> runway 13 via Alfa, Bravo" in mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"))
    p = mgr.pilots["ARG1216"]
    seen = []
    states = set()

    def watch(t):
        states.add(p.state)
        if "ARG1216" in own.OWN:
            seen.append(own.OWN["ARG1216"])
        return False

    held = []

    def watch_hold(t):
        watch(t)
        if p.state == "holding" and not held:
            from atc.geo import distance_nm

            held.append(distance_nm(p.pose.lat, p.pose.lon, *mgr.world.taxi["SATS"].nodes[100]) * 1852)
        return False

    _run(mgr, _user(), 1500, watch=watch_hold)
    assert not mgr.pilots and inj.removed == ["ARG1216"] and own.OWN == {}
    assert 22 < held[0] < 30  # stopped with its nose short of the holding point (half a 737 + 6 m)
    assert {"pushing", "taxiing", "holding", "lining_up", "takeoff"} <= states
    assert max(t.gs_kt for t in seen) >= 140 and any(not t.on_ground for t in seen)


def test_clearances_come_over_the_radio_and_the_pilot_waits_for_them():
    bus = RadioBus()
    mgr = OwnTraffic(_world(), None, FakeInjector(), bus=bus, catalog=catalog.ModelCatalog(), community=None,
                     wind=lambda a: (130.0, 8.0), rng=random.Random(4))
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"))
    p = mgr.pilots["ARG1216"]
    t = _run(mgr, _user(), 200, watch=lambda t: bool(bus.queue))
    ex = bus.queue[0]
    assert ex.role == "ground" and [w for w, _, _ in ex.lines] == ["Argentina one two one six", "ATC",
                                                                   "Argentina one two one six"]
    assert ex.lines[0][1] == "Testa Ground, Argentina one two one six, stand five, request push and start."
    assert ex.lines[1][1] in {f"Argentina one two one six, push and start approved, {w}." for w in PUSH_WAYS}
    assert ex.lines[2][1] == f"Push and start approved, {ex.lines[1][1].split(', ')[2][:-1]}, Argentina one two one six."
    t = _run(mgr, _user(), t + 5, t=t + 0.1)
    assert p.state == "push_radio"  # not heard yet: still parked
    ex.on_said()
    t = _run(mgr, _user(), t + 5.5, t=t + 0.1)
    assert p.state == "push_radio"  # heard: the tug takes a few seconds (6-12 s) before it pushes
    _run(mgr, _user(), t + 8, t=t)
    assert p.state == "pushing"


def test_it_stops_behind_the_user_on_the_taxiway_and_goes_on_when_they_leave():
    net = _net()
    mgr = OwnTraffic(World([APT]), None, FakeInjector(), catalog=catalog.ModelCatalog(), community=None,
                     wind=lambda a: (130.0, 8.0), rng=random.Random(4))
    mgr.world.taxi["SATS"] = net
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"))
    p = mgr.pilots["ARG1216"]
    lat, lon = net.nodes[3]  # the user stands on Alfa, between the stand and the runway
    blocker = _user(lat=lat, lon=lon, heading_deg=304.0)
    t = _run(mgr, blocker, 400, watch=lambda t: p.state == "taxiing" and p.pose.gs_kt == 0 and p.mover.s > 50)
    t = _run(mgr, blocker, t + 30, t=t)
    from atc.geo import distance_nm

    gap = distance_nm(p.pose.lat, p.pose.lon, lat, lon) * 1852
    assert p.state == "taxiing" and 45 < gap < 100 and p.pose.gs_kt < 0.5
    _run(mgr, _user(), t + 300, t=t, watch=lambda t: p.state == "holding")
    assert p.state == "holding"


def test_holds_short_while_the_user_is_on_the_runway():
    mgr = OwnTraffic(_world(), None, FakeInjector(), catalog=catalog.ModelCatalog(), community=None,
                     wind=lambda a: (130.0, 8.0), rng=random.Random(4))
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"))
    p = mgr.pilots["ARG1216"]
    on_rwy = _user(lat=R13.lat, lon=R13.lon, heading_deg=124.0, com1_mhz=118.85)
    t = _run(mgr, on_rwy, 900, watch=lambda t: p.state == "holding")
    t = _run(mgr, on_rwy, t + 60, t=t)
    assert p.state == "holding"
    _run(mgr, _user(), t + 30, t=t, watch=lambda t: p.state == "lining_up")
    assert p.state == "lining_up"


def _mgr(**kw):
    return OwnTraffic(_world(), None, kw.pop("inj", FakeInjector()), catalog=catalog.ModelCatalog(), community=None,
                      wind=lambda a: (130.0, 8.0), rng=random.Random(4), **kw)


def test_an_arrival_lands_vacates_and_taxis_to_its_stand():
    bus = RadioBus()
    mgr = _mgr(bus=bus)
    assert "on a 12 NM final runway 13 -> stand 5" in mgr.spawn_arrival(APT, 0.0, schedule.Departure("ARG", "1234",
                                                                                                    "B738"))
    p = mgr.pilots["ARG1234"]
    said, states, t = [], [], 0.0
    while t < 1200 and "ARG1234" in mgr.pilots:
        mgr.step(t, _user(), [])
        while bus.queue:  # everything said at once (no user on the frequency to wait for)
            ex = bus.queue.pop(0)
            said.append(ex.lines[1][1] if ex.lines[0][0] != "ATC" else ex.lines[0][1])
            if ex.on_said:
                ex.on_said()
        if not states or states[-1] != p.state:
            states.append(p.state)
        if p.state == "parked_in":
            break
        t += 0.1
    assert states == ["final", "rollout", "vacating", "clear_of_runway", "taxi_in_radio", "taxiing_in", "parked_in"]
    assert said[0].startswith("Argentina one two three four, Testa Tower, wind one three zero degrees eight knots, "
                              "runway one three, cleared to land")
    assert said[1] == "Argentina one two three four, contact ground one two one decimal niner."
    assert said[2].startswith("Argentina one two three four, taxi to stand five via")
    from atc.geo import distance_nm

    st = mgr.world.taxi["SATS"].nodes[200]
    assert distance_nm(p.pose.lat, p.pose.lon, *st) * 1852 < 2.0


def test_no_landing_clearance_with_the_user_on_the_runway_and_a_go_around_at_one_mile():
    mgr = _mgr()
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("ARG", "1234", "B738"))
    p = mgr.pilots["ARG1234"]
    on_rwy = _user(lat=R13.lat, lon=R13.lon, heading_deg=124.0, com1_mhz=118.85)
    _run(mgr, on_rwy, 600, watch=lambda t: p.state != "final")
    assert p.state == "go_around" and not p.landing_cleared and p.checked_in
    _run(mgr, on_rwy, 1200)
    assert "ARG1234" not in mgr.pilots  # climbed away and was removed


def test_our_pilots_report_the_atis_letter_or_get_asked_for_it():
    said = []
    for n in range(1200, 1240):  # enough flights for both: most say it, about one in ten forgets and is asked
        bus = RadioBus()
        mgr = _mgr(bus=bus, atis_letter=lambda a: "Bravo")
        mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", str(n), "B738"))
        _run(mgr, _user(), 200, watch=lambda t: bool(bus.queue))
        said.append([text for _, text, _ in bus.queue[0].lines])
    told = [x for x in said if ", information Bravo, request push and start" in x[0]]
    asked = [x for x in said if x[1].endswith(", confirm information Bravo.")]
    assert told and asked and len(told) + len(asked) == len(said) and len(asked) < len(told)
    assert all(x[2].endswith(", affirm, information Bravo, Argentina " + x[2].split("Argentina ")[-1]) for x in asked)


def test_ground_holds_a_push_while_somebody_taxis_behind_the_stand():
    mgr = _mgr()
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"))
    p = mgr.pilots["ARG1216"]
    lat, lon = mgr.world.taxi["SATS"].nodes[5]
    passing = _user(lat=lat, lon=lon, gs_kt=12.0, heading_deg=304.0)  # the user taxiing along Alfa, behind it
    t = _run(mgr, passing, 120)
    assert p.state == "ask_push"
    _run(mgr, _user(), t + 5, t=t)
    assert p.state in ("push_radio", "pushing")


def test_a_tug_sits_under_the_nose_during_the_push_and_leaves_after(monkeypatch):
    from atc.geo import distance_nm

    monkeypatch.setenv("ATC_OWN_TUG", "FSDT_TPX_200")
    inj = FakeInjector()
    mgr = _mgr(inj=inj)
    mgr.spawn_departure(APT, 0.0, schedule.Departure("ARG", "1216", "B738"))
    p = mgr.pilots["ARG1216"]
    t = _run(mgr, _user(), 300, watch=lambda t: p.state == "pushing" and p.mover.s > 20)
    tug = inj.poses["ARG1216~TUG"]
    assert inj.titles["ARG1216~TUG"] == "FSDT_TPX_200"
    nose = distance_nm(p.pose.lat, p.pose.lon, tug.lat, tug.lon) * 1852
    assert 15 < nose < 20  # under the nose gear of a 737 (0.38 x 40 m + 2 m)
    assert abs(((tug.heading_deg - p.pose.heading_deg) % 360) - 180) < 1  # facing the aircraft
    _run(mgr, _user(), t + 200, t=t, watch=lambda t: p.state == "starting" and "ARG1216~TUG" not in inj.poses)
    assert "ARG1216~TUG" in inj.removed and p.state == "starting"


def test_nothing_moves_while_the_sim_is_paused():
    inj = FakeInjector()
    mgr = _mgr(inj=inj)
    mgr.spawn_arrival(APT, 0.0, schedule.Departure("ARG", "1234", "B738"))
    p = mgr.pilots["ARG1234"]
    mgr.step(0.0, _user(), [])
    mgr.step(1.0, _user(), [])
    before = p.approach.dist_nm
    inj.paused = True
    for i in range(2, 30):
        mgr.step(float(i), _user(), [])
    assert p.approach.dist_nm == before
    inj.paused, inj.sim_rate = False, 2.0
    mgr.step(30.0, _user(), [])
    for i in range(1, 9):  # 2 s of wall clock
        mgr.step(30.0 + i * 0.25, _user(), [])
    moved_nm = before - p.approach.dist_nm
    assert 0.2 < moved_nm < 0.27  # 5 sim seconds (2.5 s of wall clock, the first step included) at ~170 kt


def test_traffic_amount_per_airport():
    from dataclasses import replace

    assert schedule.movements_per_hour(replace(APT, traffic_per_hour=12.0), None) == 12.0
    assert schedule.movements_per_hour(APT, None) == schedule.GA_RATE[True]  # no schedule: GA only
    assert schedule.movements_per_hour(replace(APT, towered=False), None, factor=2.0) == 2 * schedule.GA_RATE[False]


def test_the_rest_of_the_app_sees_our_aircraft_but_the_tracker_leaves_them_alone():
    own.publish({"ARG1216": Traffic("ARG1216", R13.lat, R13.lon, 18, 120.0, 124.0, True, type="B738")})
    from atc.sim.fake import FakeSim

    sim = FakeSim(APT, callsign="LVABC")
    seen = sim.traffic(APT.lat, APT.lon, 15)
    assert [t.callsign for t in seen] == ["ARG1216"]
    st = runway_status(APT, R13, sim.own(), seen)
    assert st.occupied_by == ["ARG1216"] and st.departing == ["ARG1216"]
    tr = TrafficTracker(APT)
    assert tr.update(seen, 0.0) == [] and tr.tracks == {}


def test_change_of_routing_round_somebody_stuck_in_the_way():
    """A square of taxiways: from node 1 to node 3 the short way is past node 2; with somebody stuck at node 2,
    Ground's new route goes round by node 4. A single lane has no way round: None."""
    from atc.geo import distance_nm
    from atc.own.airport import reroute

    net = TaxiNetwork()
    base = (-34.56, -58.41)
    net.nodes = {1: base, 2: moved(*base, 90.0, 300.0), 3: moved(*moved(*base, 90.0, 300.0), 0.0, 300.0),
                 4: moved(*base, 0.0, 300.0)}
    for a, b, name in ((1, 2, "A"), (2, 3, "B"), (1, 4, "C"), (4, 3, "D")):
        m = distance_nm(*net.nodes[a], *net.nodes[b]) * 1852
        net.edges.setdefault(a, []).append((b, m, name))
        net.edges.setdefault(b, []).append((a, m, name))
    pts, _, via = reroute(net, *net.nodes[1], {3}, [net.nodes[2]])
    assert via == "Charlie, Delta" and pts[-1] == net.nodes[3]
    line = TaxiNetwork(nodes={1: base, 2: net.nodes[2]}, edges={1: [(2, 300.0, "A")], 2: [(1, 300.0, "A")]})
    assert reroute(line, *base, {2}, [net.nodes[2]]) is None
