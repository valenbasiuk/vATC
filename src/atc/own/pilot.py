"""Our own aircraft, one flight each, step by step. Each step that needs ATC waits for it: the pilot sets `wants`,
the controller (manager.OwnAtc) answers with `clear()`, and the pilot moves once that radio exchange has been heard
(or after RADIO_WAIT_S if it never gets said, e.g. the user is on another frequency).

DeparturePilot: parked -> pushback -> engine start -> taxi -> holding point (stopped with its nose short of the
line) -> line up -> takeoff -> climb out (turning toward its destination, improvement plan H2) -> gone; a GA
aircraft staying in the circuit becomes a circuit flight at 400 ft (the manager swaps it for an ArrivalPilot).
ArrivalPilot: joining (a downwind or a base onto the final, H2; or round a circuit, H3) or straight onto a final (from
~12 NM) -> landing clearance (or go-around without one) -> touchdown, braking for the exit it planned (or a touch and
go, then round again) -> vacate -> "contact Ground" -> taxi to its stand -> parked -> gone after a while (or, a
turnaround, H1: it leaves again from that stand as a new flight).

On the ground they stop behind anybody on their path (the user included): what MSFS AI doesn't do.
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass

from atc.geo import bearing_deg, distance_nm
from atc.models import Airport, Traffic
from atc.own.airport import ArrivalPaths, Crossing, DeparturePaths, Stand, arrival_paths, path_crossings
from atc.own.motion import EXIT_KT, KT, MAX_TAXI_KT, PUSH_KT, Airborne, Approach, GroundMover, Join, Path, Perf, Pose, \
    Takeoff

RADIO_WAIT_S = 17.0  # chatter goes stale after 15 s (chatter.STALE_S): then go anyway
ENGINE_START_S = 45.0
LOOK_AHEAD_M = 150.0
CONFLICT_M = 30.0  # somebody this close to a point of our path ahead blocks it (at least; wingspans can add)
WINGTIP_MARGIN_M = 6.0
STANDING_M = 22.0  # an aircraft standing still blocks the path only this close to it (on it, not parked beside)
STOP_BEHIND_M = 60.0  # stop this far before them (two 737s nose to tail: ~20 m between them)
SELF_M = 12.0
LINEUP_KT = 12.0
GONE_NM = 12.0
GONE_AGL_FT = 7000.0
HANDOFF_AGL_FT = 900.0
HOLD_SHORT_M = 6.0  # the nose stops this far before the holding point (half the length is added)
STANDING = {"parked", "ask_push", "push_radio", "starting", "ask_taxi", "taxi_radio", "holding", "takeoff_radio",
            "lined_up", "clear_of_runway", "taxi_in_radio", "parked_in", "hold_short", "cross_radio"}
ASK_LANDING_NM = 9.0  # checks in with Tower from here
GO_AROUND_NM = 1.0  # no landing clearance by now: going around
# Valen 2026-10-17: a pilot rarely goes around on his own because of a blocked runway, Tower sends him around
# first (TOWER_GA_NM, manager._check_uncleared_arrival); only this share calls "going around" itself at GO_AROUND_NM
PILOT_GA_SHARE = 0.05
TOWER_GA_NM = 1.5  # Tower: "go around" to an arrival it couldn't clear to land by here
TOWER_GA_CIRCUIT_NM = 0.6  # the same on a circuit's short final
# Go-arounds with the landing clearance (unstable approach, a gust): rare, a little more often in strong wind
RANDOM_GA = True  # tests switch it off (conftest)
RANDOM_GA_BASE = 0.01  # per approach
RANDOM_GA_PER_KT = 0.002  # more per knot of wind above RANDOM_GA_FROM_KT
RANDOM_GA_FROM_KT = 15.0
RANDOM_GA_MAX = 0.05
RANDOM_GA_NM = (0.2, 1.5)  # where on the final it happens
ROLLOUT_TAXI_KT = 25.0  # the rollout becomes a taxi below this
PARKED_FOR_S = (240.0, 480.0)
SPACING_NM = 3.0  # slows to the speed of the aircraft ahead on final inside this
# A pilot doesn't move the instant a clearance ends: seconds from "heard" to moving (Valen: a small random delay)
REACT_S = {"push": (6.0, 12.0), "taxi": (3.0, 8.0), "takeoff": (2.0, 5.0), "taxi_in": (2.0, 6.0)}
LINEUP_PAUSE_S = (2.0, 5.0)  # lined up and stopped: spooling up before the roll
ROLLING_TAKEOFF_SHARE = 0.5  # the others stop on the centerline first
BEHIND_MAX_S = 240.0  # a conditional line-up ("behind the landing ...") lapses if that one has not passed by then
CLIMBOUT_AGL_FT = 1000.0  # a departure turns toward its destination from here (H2)
CLIMB_GONE_NM = 25.0  # ... and leaves the sim this far out, or this high (traffic for the user meanwhile: D3)
CLIMB_GONE_AGL_FT = 12000.0
CIRCUIT_AGL_FT = 400.0  # a circuit flight hands over from the takeoff to the circuit legs here
GA_GO_AROUND_NM = 0.3  # in the circuit (a short final): going around this close without a landing clearance
TOUCH_GO_ROLL_S = 4.0  # on the runway this long before a touch and go powers up again


@dataclass
class Flight:
    callsign: str  # ICAO form, "ARG1216": also the sim's ATC id of the object
    title: str  # model to create
    type_icao: str
    airline: str | None = None  # ICAO airline code
    destination: str | None = None  # ICAO (FS Traffic's schedule), where a departure turns to after takeoff


class _Pilot:
    kind = ""

    def __init__(self, flight: Flight, airport: Airport, perf: Perf, now: float) -> None:
        self.flight, self.airport, self.perf = flight, airport, perf
        self.key = flight.callsign  # the sim object (a turnaround keeps the object under a new flight number)
        self.state = ""
        self.since = now
        self.wants: str | None = None
        self.radio_done_at: float | None = None
        self.mover: GroundMover | None = None
        self.pose = Pose(airport.lat, airport.lon, 0.0, 0.0)
        self.rng = random.Random(zlib.crc32(flight.callsign.encode()))  # its own habits, the same every run
        self.go_at: float | None = None
        self.blocked_by: str | None = None  # stopped on the ground for this aircraft
        self.crossings: list[Crossing] = []  # runways the current taxi path crosses
        self.resume: str | None = None  # the state to go back to once a crossing is cleared
        self.yielding: set[str] = set()  # ours stopped for this one (the manager fills it): this one goes first
        self.parked_ids: set[str] = set()  # aircraft parked at a stand (the manager fills it): never in the way
        self.blocked_since: float | None = None  # stopped for `blocked_by` since (sim clock)
        self.ignore: set[str] = set()  # last resort after a deadlock: passes these (never set otherwise)

    def clear(self, what: str, now: float, said: bool = False) -> None:
        """ATC gave `what`. `said` = the exchange is already heard (tests); else `heard()` comes from the radio."""
        if self.wants != what:
            return
        self.wants = None
        if self._cleared_in_place(what, now):
            return
        self._go(f"{what}_radio", now)
        self.radio_done_at = now if said else None

    def _cleared_in_place(self, what: str, now: float) -> bool:
        """Clearances that change nothing in how it moves right now (handoffs, landing on final)."""
        return False

    def heard(self, now: float) -> None:
        if self.radio_done_at is None:
            self.radio_done_at = now

    def _go(self, state: str, now: float) -> None:
        self.state, self.since = state, now
        if state in STANDING:
            self.pose.gs_kt = 0.0

    def _radio_over(self, now: float) -> bool:
        return self.radio_done_at is not None or now - self.since >= RADIO_WAIT_S

    def _ready(self, now: float, what: str) -> bool:
        """The clearance was heard and the pilot's reaction time has passed."""
        if not self._radio_over(now):
            return False
        if self.go_at is None:
            self.go_at = now + self.rng.uniform(*REACT_S.get(what, (2.0, 5.0)))
        if now < self.go_at:
            return False
        self.go_at = None
        return True

    @property
    def done(self) -> bool:
        return self.state == "gone"

    def _taxi(self, now: float, dt: float, others: list[Traffic], resume: str, end_stop: float | None = None) -> None:
        """One taxi step: stop for traffic, short of the next runway to cross (and ask), or at `end_stop`."""
        nxt = next((c for c in self.crossings if not c.cleared), None)
        block = self._stop_for_traffic(others)
        ahead = next((c for c in self.crossings if c.cleared and self.mover.s < c.s_hold + 1.0), None)
        if ahead is not None and self.mover.s >= ahead.s_hold - 3.0 and self.exit_blocker(ahead, others):
            # cleared to cross, but somebody stands where it would come off the runway: never stop on the runway
            # (SABE soak: one sat on 13/31 for 90 s, an arrival went around): back to holding short, ask again
            ahead.cleared = False
            nxt = ahead
        stops = [x for x in (block, end_stop, nxt.s_hold if nxt else None) if x is not None]
        self.mover.step(dt, MAX_TAXI_KT, min(stops) if stops else None)
        self.pose = self.mover.pose()
        if nxt and self.mover.s >= nxt.s_hold - 0.5 and self.mover.v < 0.2 and (block is None or block >= nxt.s_hold):
            self.wants, self.resume = "cross", resume  # "holding short of runway one three"
            self._go("hold_short", now)

    def exit_blocker(self, c: Crossing, others: list[Traffic]) -> str | None:
        """Who is on the ground (taxiing or standing, not parked at a stand) on the path from the hold line to a
        length past the far side of runway crossing `c`: crossing now would leave this one stopped on the runway."""
        from atc.own.motion import perf

        if self.mover is None:
            return None
        path = self.mover.path
        end = min(path.length, c.s_clear + STOP_BEHIND_M + 10.0)  # (it stops STOP_BEHIND_M short of anybody)
        pts = []
        x = max(0.0, c.s_hold)
        while x <= end:
            pts.append(path.latlon(x))
            x += 8.0
        for t in others:
            if not t.on_ground or t.gs_kt > 40.0 or t.callsign == self.flight.callsign or t.callsign in self.parked_ids                     or t.callsign in self.ignore:
                continue  # (fast ones are on the runway itself: Tower's business)
            if distance_nm(self.pose.lat, self.pose.lon, t.lat, t.lon) * 1852.0 < SELF_M:
                continue
            radius = max(STANDING_M, (self.perf.wingspan_m + perf(t.type).wingspan_m) / 2 + WINGTIP_MARGIN_M)
            if any(distance_nm(lat, lon, t.lat, t.lon) * 1852.0 < radius for lat, lon in pts):
                return t.callsign
        return None

    def _crossing_step(self, now: float) -> bool:
        """Waiting for or getting a crossing clearance. True while that is what it is doing."""
        if self.state == "hold_short":
            return True
        if self.state == "cross_radio":
            if self._ready(now, "taxi"):
                nxt = next((c for c in self.crossings if not c.cleared), None)
                if nxt is not None:
                    nxt.cleared = True
                self._go(self.resume or "taxiing", now)
            return True
        return False

    def take_route(self, points: list[tuple[float, float]], limits: list[float], runway) -> None:
        """Ground's change of routing: the same taxi state on a new path from where it is."""
        path = Path(points, limits_kt=limits)
        if self.paths is not None and hasattr(self.paths, "clear_s"):  # an arrival: it is clear of the runway now
            self.paths.clear_s = 0.0
        self.mover = GroundMover(path, v=0.0)
        self.crossings = [c for c in path_crossings(self.airport, path, self.perf.length_m, runway)]
        self.blocked_since, self.blocked_by = None, None

    @property
    def next_crossing(self) -> Crossing | None:
        return next((c for c in self.crossings if not c.cleared), None)

    def _stop_for_traffic(self, others: list[Traffic]) -> float | None:
        """Where to stop on the current path: STOP_BEHIND_M before the first point ahead someone is standing or
        moving on (or None). Everyone else on the ground counts: the user, our other aircraft, the sim's AI."""
        m = self.mover

        def gap(t: Traffic) -> float:
            return distance_nm(self.pose.lat, self.pose.lon, t.lat, t.lon) * 1852.0

        from atc.own.motion import perf

        # (within SELF_M: our own sim object under another name, never something to stop for; `yielding`: ours that
        # are already stopped for this one, so it goes first instead of both waiting for each other)
        # wingtip clearance from anybody on a taxiway, moving or standing; aircraft parked at a stand are never in
        # the way (the scenery keeps them clear of the lanes); `yielding` ones (stopped for this one) only when they
        # are on the path itself: passed alongside, never driven into
        def radius(t: Traffic) -> float:
            if t.callsign in self.yielding:
                return STANDING_M
            return max(STANDING_M, (self.perf.wingspan_m + perf(t.type).wingspan_m) / 2 + WINGTIP_MARGIN_M)

        near = [(t, radius(t)) for t in others if t.on_ground and t.callsign != self.flight.callsign
                and t.callsign not in self.parked_ids and t.callsign not in self.ignore
                and SELF_M < gap(t) < LOOK_AHEAD_M + 60.0]
        self.blocked_by = None
        if not near:
            return None
        d = 10.0
        while d <= LOOK_AHEAD_M and m.s + d <= m.path.length + CONFLICT_M:
            lat, lon = m.path.latlon(m.s + d)
            for t, radius in near:  # wingtips: half of each wingspan apart, plus a margin
                if distance_nm(lat, lon, t.lat, t.lon) * 1852.0 < radius:
                    self.blocked_by = t.callsign
                    return m.s + max(0.0, d - STOP_BEHIND_M)
            d += 4.0
        return None

    def traffic(self, elevation_ft: float) -> Traffic:
        """How the rest of the app sees this aircraft."""
        p = self.pose
        digits = "".join(c for c in self.flight.callsign if c.isdigit())
        return Traffic(self.flight.callsign, p.lat, p.lon, elevation_ft + max(0.0, p.agl_ft), p.gs_kt,
                       p.heading_deg % 360.0, p.on_ground, type=self.flight.type_icao, flight_number=digits or None)


class DeparturePilot(_Pilot):
    kind = "departure"

    def __init__(self, flight: Flight, airport: Airport, paths: DeparturePaths, perf: Perf, now: float,
                 push_after_s: float = 20.0, circuits: int = 0, dest_pos: tuple[float, float] | None = None) -> None:
        super().__init__(flight, airport, perf, now)
        self.paths = paths
        st = paths.stand
        self.pose = Pose(st.lat, st.lon, 0.0, st.heading)
        self.state = "parked"
        self.push_at = now + push_after_s
        self.takeoff: Takeoff | None = None
        self.climb: Airborne | None = None
        self.handed_off = False
        self.rolling = False
        self.immediate = False  # "cleared for immediate takeoff": no stop on the centerline
        self.luaw = False  # the clearance is "line up and wait": stops on the centerline, waits for the takeoff one
        self.waiting_lined = False  # lined up under "line up and wait": the takeoff clearance comes next
        self.behind: str | None = None  # conditional line-up ("behind the landing ..."): after that one has passed
        self.reported_airborne = False
        self.circuits = circuits  # GA: touch and goes round the circuit before the full stop (H3)
        self.dest_pos = dest_pos  # where it turns to after takeoff; None: a way off the runway heading of its own

    def start_taxiing(self, now: float, share: float) -> None:
        """Warm start (traffic already moving when vATC starts): taxiing out under a clearance given before we came,
        `share` of the way to the holding point (1.0 = stopped at it, about to call Tower). Never on a runway it
        crosses: then short of that runway instead."""
        path = Path(self.paths.taxi, limits_kt=self.paths.taxi_limits or None)
        hold = path.length - (self.perf.length_m / 2 + HOLD_SHORT_M)
        at = max(0.0, min(hold, share * hold))
        crossings = [c for c in path_crossings(self.airport, path, self.perf.length_m, self.paths.runway)
                     if c.s_hold < hold]
        for c in crossings:
            if c.s_hold - 30.0 < at < c.s_clear + 30.0:
                at = max(0.0, c.s_hold - 30.0)
        self.mover = GroundMover(path)
        self.mover.s = at
        self.crossings = [c for c in crossings if c.s_hold > at]
        self.pose = self.mover.pose()
        self.push_at = now
        self._go("taxiing", now)

    def _cleared_in_place(self, what: str, now: float) -> bool:
        if what == "handoff":  # "contact Departure": nothing changes in how it flies
            self.handed_off = True
            return True
        return False

    def entry_along_nm(self) -> float:
        """Where it lines up: NM past the threshold of its runway (0 at the end, more at an intersection)."""
        from atc.sequence import along_cross

        if not self.paths.lineup:
            return 0.0
        return along_cross(self.airport, self.paths.runway, *self.paths.lineup[-1])[0]

    def _passed(self, callsign: str, others: list[Traffic]) -> bool:
        """The aircraft of a conditional line-up has gone by where this one lines up (or left the runway)."""
        from atc.sequence import along_cross, on_runway

        t = next((o for o in others if o.callsign == callsign), None)
        if t is None:
            return True
        if t.on_ground and not on_runway(self.airport, self.paths.runway, t):
            return True  # vacated
        return along_cross(self.airport, self.paths.runway, t.lat, t.lon)[0] > self.entry_along_nm() + 0.05

    def _roll(self, now: float) -> None:
        heading = self.paths.runway.heading_deg or self.pose.heading_deg
        self.takeoff = Takeoff(self.pose.lat, self.pose.lon, heading, self.perf, v0_kt=self.mover.v / KT)
        self._go("takeoff", now)

    def step(self, now: float, dt: float, others: list[Traffic]) -> None:
        s = self.state
        if s == "parked" and now >= self.push_at:
            if self.paths.push:
                self.wants = "push"
                self._go("ask_push", now)
            else:
                self._go("starting", now)
        elif s == "push_radio" and self._ready(now, "push"):
            self.mover = GroundMover(Path(self.paths.push), backwards=True)
            self._go("pushing", now)
        elif s == "pushing":
            self.mover.step(dt, PUSH_KT, self._stop_for_traffic(others))  # the tug stops for whoever is in the way
            self.pose = self.mover.pose()
            if self.mover.done:
                self._go("starting", now)
        elif s == "starting" and now - self.since >= ENGINE_START_S:
            self.wants = "taxi"
            self._go("ask_taxi", now)
        elif s == "taxi_radio" and self._ready(now, "taxi"):
            path = Path(self.paths.taxi, limits_kt=self.paths.taxi_limits or None)
            self.mover = GroundMover(path)
            hold = path.length - (self.perf.length_m / 2 + HOLD_SHORT_M)
            self.crossings = [c for c in path_crossings(self.airport, path, self.perf.length_m, self.paths.runway)
                              if c.s_hold < hold]
            self._go("taxiing", now)
        elif self._crossing_step(now):
            pass
        elif s == "taxiing":
            hold = self.mover.path.length - (self.perf.length_m / 2 + HOLD_SHORT_M)  # nose short of the line
            self._taxi(now, dt, others, "taxiing", hold)
            if self.state == "taxiing" and self.mover.s >= hold - 0.1 and self.mover.v < 0.2:
                self.wants = "takeoff"
                self._go("holding", now)
        elif s == "takeoff_radio" and self._ready(now, "takeoff"):
            if self.waiting_lined:  # cleared for takeoff where it stands: spool up, roll ("immediate": at once)
                self.waiting_lined = False
                self.go_at = now + (0.0 if self.immediate else self.rng.uniform(*LINEUP_PAUSE_S))
                self._go("lined_up", now)
            elif self.behind is not None and now - self.since > BEHIND_MAX_S:
                # the one it was to line up behind never came by (went around far out, landed elsewhere): the
                # conditional clearance lapses, it waits at the holding point and calls Tower again
                self.behind, self.luaw, self.wants = None, False, "takeoff"
                self._go("holding", now)
            elif self.behind is None or self._passed(self.behind, others):
                self.behind = None
                here = (self.pose.lat, self.pose.lon)
                self.rolling = not self.luaw and (self.immediate or self.rng.random() < ROLLING_TAKEOFF_SHARE)
                self.mover = GroundMover(Path([here] + self.paths.lineup), v=0.0, run_through=self.rolling)
                self._go("lining_up", now)
        elif s == "lining_up":
            self.mover.step(dt, LINEUP_KT)
            self.pose = self.mover.pose()
            if self.rolling and self.mover.s >= self.mover.path.length - 1.0:
                self._roll(now)
            elif not self.rolling and self.mover.done:
                if self.luaw:  # on the centerline: waits there for the takeoff clearance
                    self.luaw, self.waiting_lined, self.go_at = False, True, None
                    self.wants = "takeoff"
                else:
                    self.go_at = now + self.rng.uniform(*LINEUP_PAUSE_S)
                self._go("lined_up", now)
        elif s == "lined_up" and not self.waiting_lined and now >= (self.go_at or now):
            self.go_at = None
            self._roll(now)
        elif s == "takeoff":
            self.takeoff.step(dt)
            self.pose = self.takeoff.pose()
            if self.circuits > 0:
                if self.pose.agl_ft >= CIRCUIT_AGL_FT:
                    self._go("circuit_ready", now)  # the manager turns it into a circuit flight
                return
            if self.pose.agl_ft >= HANDOFF_AGL_FT and not self.handed_off and self.wants is None:
                self.wants = "handoff"
            if self.pose.agl_ft >= CLIMBOUT_AGL_FT:
                self._start_climbout(now)
            far = distance_nm(self.pose.lat, self.pose.lon, self.airport.lat, self.airport.lon) > GONE_NM
            if far or self.pose.agl_ft > GONE_AGL_FT:
                self._go("gone", now)
        elif s == "climbout":
            self.climb.step(dt)
            self.pose = self.climb.pose()
            if self.pose.agl_ft >= HANDOFF_AGL_FT and not self.handed_off and self.wants is None:
                self.wants = "handoff"
            far = distance_nm(self.pose.lat, self.pose.lon, self.airport.lat, self.airport.lon) > CLIMB_GONE_NM
            if far or self.pose.agl_ft > CLIMB_GONE_AGL_FT:
                self._go("gone", now)

    def _start_climbout(self, now: float) -> None:
        """From the straight climb to a turn toward the destination (H2): the way a SID's first legs go, climbing on
        to 10000 ft at 250 kt. Without a known destination a direction of its own, within 90 degrees of the runway's."""
        p = self.pose
        rwy_hdg = self.paths.runway.heading_deg or p.heading_deg
        if self.dest_pos is not None:
            course = bearing_deg(p.lat, p.lon, *self.dest_pos)
        else:
            course = (rwy_hdg + self.rng.uniform(-90.0, 90.0)) % 360.0
        air = Airborne(p.lat, p.lon, p.agl_ft, p.heading_deg, p.gs_kt, self.perf, vs_fpm=self.perf.climb_fpm)
        air.target_heading, air.target_agl, air.target_kt = course, 10000.0, 250.0
        self.climb = air
        self._go("climbout", now)


def random_ga_point(wind_kt: float | None) -> float | None:
    """Where on the final a cleared arrival goes around anyway (NM), or None (most of the time): RANDOM_GA_BASE per
    approach, a little more per knot of wind above RANDOM_GA_FROM_KT, at most RANDOM_GA_MAX."""
    if not RANDOM_GA:
        return None
    chance = RANDOM_GA_BASE + RANDOM_GA_PER_KT * max(0.0, (wind_kt or 0.0) - RANDOM_GA_FROM_KT)
    if random.random() >= min(RANDOM_GA_MAX, chance):
        return None
    return random.uniform(*RANDOM_GA_NM)


class ArrivalPilot(_Pilot):
    kind = "arrival"

    def __init__(self, flight: Flight, airport: Airport, runway, threshold: tuple[float, float], stand: Stand,
                 perf: Perf, now: float, net, dist_nm: float = 12.0, parked_for_s: float = 300.0,
                 join: dict | None = None, laps: int = 0, turnaround_s: float | None = None) -> None:
        """`join` (H2/H3): fly onto the final from the side instead of starting on it: {"phase": "downwind" /
        "base" / "upwind", "along", "cross" (NM from the threshold, + right of the landing direction), "side",
        "offset_nm", "join_nm", "pattern_agl", "speeds" (downwind, base, final kt), "heading", "agl", "kt"} or an
        Airborne already flying ("air"). `laps`: touch and goes still to fly (a circuit, H3). `turnaround_s`: parked
        this long, then it departs again from its stand (H1; the manager does it)."""
        super().__init__(flight, airport, perf, now)
        self.runway, self.stand, self.net = runway, stand, net
        self.threshold = threshold
        self.approach: Approach | None = None
        self.join: Join | None = None
        self.landing_cleared = False
        self.checked_in = False
        self.paths: ArrivalPaths | None = None
        self.parked_for_s = parked_for_s
        self.turnaround_s = turnaround_s
        self.planned_exit_m: float | None = None
        self.laps = laps
        self.circuit = laps > 0 or bool(join and join.get("phase") == "upwind")
        self.runway_reserved = False  # one of ours lined up (or lining up) to wait on its runway: the manager sets it
        self.downwind_called = False
        self.takeoff: Takeoff | None = None
        self.ga_by_pilot = random.random() < PILOT_GA_SHARE  # blocked runway: goes around before Tower says so
        self.wind_kt: float | None = None  # surface wind (the manager sets it): go-arounds a little more likely
        self.random_ga_nm: float | None = None  # cleared to land, but goes around here anyway (rolled at clearance)
        if join is not None:
            self._start_join(join)
            self.state = "joining"
        else:
            self.approach = Approach(*threshold, runway.heading_deg or 0.0, perf, dist_nm)
            self.pose = self.approach.pose()
            self.state = "final"

    def _start_join(self, j: dict) -> None:
        from atc.own.motion import NM, moved

        h = self.runway.heading_deg or 0.0
        air = j.get("air")
        if air is None:
            lat, lon = moved(*self.threshold, h, j["along"] * NM)
            lat, lon = moved(lat, lon, h + 90.0, j["cross"] * NM)
            air = Airborne(lat, lon, j["agl"], j["heading"], j["kt"], self.perf)
        air.gear_down = self.perf.vapp_kt < 100  # light aircraft: fixed gear
        self.join = Join(self.threshold, h, (self.runway.length_ft or 6000.0) * 0.3048, self.perf, air, j["phase"],
                         j["side"], j["offset_nm"], j["join_nm"], j["pattern_agl"], j["speeds"])
        self.pose = air.pose()

    def _cleared_in_place(self, what: str, now: float) -> bool:
        if what == "landing":
            self.landing_cleared = True
            self.random_ga_nm = random_ga_point(self.wind_kt)
            return True
        if what in ("checkin", "go_around", "ground", "downwind", "announce"):
            return True
        return False

    @property
    def final_nm(self) -> float:
        if self.approach is not None and self.state not in ("joining", "touch_go"):
            return self.approach.dist_nm
        along, cross = self.join.along_cross() if self.join is not None else (-99.0, 0.0)
        return abs(along) + abs(cross) + 1.0  # joining: the way still to fly, roughly

    def order_go_around(self, now: float) -> None:
        """Tower: "go around" (the runway is occupied; a circuit flies round again)."""
        if self.state == "final" and not self.approach.on_ground:
            self.landing_cleared = False
            self.random_ga_nm = None
            self.wants = None
            if self.circuit:
                self.checked_in = self.downwind_called = False
                self._resume_circuit(now)
                return
            self.approach.go_around()
            self._go("go_around", now)

    def step(self, now: float, dt: float, others: list[Traffic]) -> None:
        s = self.state
        a = self.approach
        if s == "joining":
            if self.join.phase == "downwind":  # somebody on the final near where it would join: extend the downwind
                # (Tower: "extend downwind") somebody on the final where it would join, or lined up on the runway
                self.join.extend = self._final_busy_at_join(others) or self.runway_reserved
            done = self.join.step(dt)
            self.pose = self.join.air.pose()
            phase = self.join.phase
            if self.circuit and phase == "downwind" and not self.downwind_called and self.wants is None:
                along, _ = self.join.along_cross()
                if along <= (self.runway.length_ft or 6000.0) * 0.3048 / 1852.0 / 2:  # abeam midfield
                    self.downwind_called = True
                    self.wants = "downwind"  # Tower: number in sequence (or the CTAF call where there is none)
            if self.circuit and phase == "intercept" and not self.checked_in and self.wants is None:
                self.wants = "landing"  # turning final: "cleared touch and go" / "cleared to land"
            if done:
                along, _ = self.join.along_cross()
                air = self.join.air
                self.approach = Approach(*self.threshold, self.runway.heading_deg or 0.0, self.perf, -along,
                                         agl_ft=air.agl_ft, speed_kt=air.v / KT)
                self.pose = self.approach.pose()
                self._go("final", now)
        elif s == "final":
            a.slow_to_kt = self._speed_behind(others)
            a.step(dt)
            self.pose = a.pose()
            if not self.checked_in and a.dist_nm <= ASK_LANDING_NM and self.wants is None:
                self.wants = "landing"
            limit = GA_GO_AROUND_NM if self.circuit else GO_AROUND_NM
            unstable = self.landing_cleared and self.random_ga_nm is not None and a.dist_nm <= self.random_ga_nm
            if ((not self.landing_cleared and a.dist_nm <= limit) or unstable) and not a.on_ground:
                self.wants = "go_around"  # the pilot's own "going around" (Tower answers it)
                self.landing_cleared, self.random_ga_nm = False, None
                if self.circuit:  # round the circuit again instead of leaving
                    self.landing_cleared = self.checked_in = self.downwind_called = False
                    self._resume_circuit(now)
                    return
                a.go_around()
                self._go("go_around", now)
            elif a.on_ground:
                if self.laps <= 0:
                    self._plan_exit()
                self._go("rollout", now)
        elif s == "go_around":
            a.step(dt)
            self.pose = a.pose()
            if distance_nm(self.pose.lat, self.pose.lon, self.airport.lat, self.airport.lon) > GONE_NM \
                    or self.pose.agl_ft > GONE_AGL_FT:
                self._go("gone", now)
        elif s == "rollout" and self.laps > 0:  # touch and go: a few seconds on the runway, then full power
            a.step(dt)
            self.pose = a.pose()
            if now - self.since >= TOUCH_GO_ROLL_S:
                self.laps -= 1
                self.landing_cleared = self.checked_in = self.downwind_called = False
                self.takeoff = Takeoff(self.pose.lat, self.pose.lon, self.runway.heading_deg or self.pose.heading_deg,
                                       self.perf, v0_kt=a.v / KT)
                self._go("touch_go", now)
        elif s == "touch_go":
            self.takeoff.step(dt)
            self.pose = self.takeoff.pose()
            if self.pose.agl_ft >= CIRCUIT_AGL_FT:
                self._resume_circuit(now)
        elif s == "rollout":
            a.step(dt)
            self.pose = a.pose()
            if a.v <= ROLLOUT_TAXI_KT * KT:
                self.paths = arrival_paths(self.net, self.airport, self.runway, self.pose.lat, self.pose.lon,
                                           a.v / KT, self.stand) if self.net is not None else None
                if self.paths is None:  # nowhere to go: it vanishes off the runway end (rare: no map)
                    self._go("gone", now)
                    return
                path = Path(self.paths.points, limits_kt=self.paths.limits, cap_kt=EXIT_KT)
                self.mover = GroundMover(path, v=a.v)
                self.crossings = [c for c in path_crossings(self.airport, path, self.perf.length_m, self.runway)
                                  if c.s_hold > self.paths.clear_s]
                self._go("vacating", now)
        elif s == "vacating":
            block = self._stop_for_traffic(others)
            stop = self.paths.clear_s if block is None else min(block, self.paths.clear_s)
            self.mover.step(dt, EXIT_KT, stop)
            self.pose = self.mover.pose()
            if self.mover.s >= self.paths.clear_s - 0.5 and self.mover.v < 0.3:
                self.wants = "ground"  # Tower: "contact Ground"; then the taxi-in request
                self._go("clear_of_runway", now)
        elif s == "clear_of_runway" and self.wants is None:
            self.wants = "taxi_in"
        elif s == "taxi_in_radio" and self._ready(now, "taxi_in"):
            self._go("taxiing_in", now)
        elif self._crossing_step(now):
            pass
        elif s == "taxiing_in":
            self._taxi(now, dt, others, "taxiing_in")
            if self.state == "taxiing_in" and self.mover.done:
                self.pose.heading_deg = self.stand.heading
                self._go("parked_in", now)
        elif s == "parked_in" and now - self.since >= self.parked_for_s:
            self._go("gone", now)

    def _final_busy_at_join(self, others: list[Traffic]) -> bool:
        """Anybody on this final who would be within the spacing of the point where this one joins it."""
        from atc.sequence import final_distance

        j = self.join.join_nm
        gap = 1.5 if self.perf.vapp_kt < 100 else 4.0
        for t in others:
            if t.callsign == self.flight.callsign:
                continue
            d = final_distance(self.airport, self.runway, t)
            if d is not None and j - gap < d < j + gap * 1.5:
                return True
        return False

    def tower_should_send_around(self) -> bool:
        """Not cleared to land this close in: Tower says "go around" (unless this pilot is one of the few who
        goes around first)."""
        if self.state != "final" or self.landing_cleared or self.ga_by_pilot or not self.checked_in:
            return False
        if self.approach is None or self.approach.on_ground:
            return False
        return self.approach.dist_nm <= (TOWER_GA_CIRCUIT_NM if self.circuit else TOWER_GA_NM)

    def _resume_circuit(self, now: float) -> None:
        """Climbing away from a touch and go (or a circuit takeoff): round the circuit again."""
        p = self.pose
        air = Airborne(p.lat, p.lon, p.agl_ft, p.heading_deg, p.gs_kt, self.perf, vs_fpm=self.perf.climb_fpm)
        self._start_join(circuit_join(self.runway, self.perf, air))
        self.approach = None
        self._go("joining", now)

    def _plan_exit(self) -> None:
        """At touchdown: the exit to make (firm braking allowed), and the braking that gets there at taxi speed."""
        if self.net is None:
            return
        a = self.approach
        plan = arrival_paths(self.net, self.airport, self.runway, self.pose.lat, self.pose.lon, a.v / KT,
                             self.stand, decel=2.6, extra_m=150.0)
        if plan is None:
            return
        from atc.taxi import _signed

        here = _signed(self.airport, self.runway, self.pose.lat, self.pose.lon)[0] * 1852.0
        room = plan.exit_along_nm * 1852.0 - here - 150.0 - 40.0  # 40 m of derotation at touchdown speed
        if room > 50:
            need = (a.v ** 2 - (ROLLOUT_TAXI_KT * KT) ** 2) / (2 * room)
            a.rollout_decel = max(1.2, min(3.0, need))
        self.planned_exit_m = plan.exit_along_nm * 1852.0

    def _speed_behind(self, others: list[Traffic]) -> float | None:
        """The speed of the aircraft ahead on the same final if it is inside SPACING_NM (else None)."""
        from atc.sequence import final_distance, on_runway

        mine = self.approach.dist_nm
        ahead = None
        for t in others:
            if t.callsign == self.flight.callsign:
                continue
            d = final_distance(self.airport, self.runway, t)
            if d is not None and d < mine and mine - d < SPACING_NM:
                ahead = t.gs_kt if ahead is None else min(ahead, t.gs_kt)
            elif t.on_ground and on_runway(self.airport, self.runway, t) and mine < SPACING_NM:
                ahead = self.perf.vapp_kt - 5.0
        return ahead


def circuit_join(runway, perf: Perf, air: Airborne | None = None, phase: str = "upwind") -> dict:
    """A light aircraft's circuit of `runway` (H3): on its pattern side (airport YAML `pattern_direction`, left by
    default), at its pattern height (`pattern_alt_agl_ft`, else 1000 ft), 0.7 NM out, onto the final 1.2 NM out."""
    return {"phase": phase, "side": 1.0 if runway.pattern_direction == "right" else -1.0, "offset_nm": 0.7,
            "join_nm": 1.2, "pattern_agl": float(runway.pattern_alt_agl_ft or 1000),
            "speeds": (90.0, 80.0, 75.0) if perf.vapp_kt < 100 else (160.0, 150.0, 140.0), "air": air}


def side_join(runway, perf: Perf, phase: str, side: float) -> dict:
    """An airliner joining the final from the side (H2), as radar vectors would bring it: a downwind 4 NM out from
    abeam the middle of the runway, or a base 6 NM out, at 3000 ft above the field, joining 9 NM out."""
    length_nm = (runway.length_ft or 6000.0) / 6076.1
    h = runway.heading_deg or 0.0
    s = 1.0 if side >= 0 else -1.0
    if phase == "downwind":
        along, cross, heading = length_nm / 2.0, 4.0 * s, (h + 180.0) % 360.0
    else:
        along, cross, heading = -10.0, 6.0 * s, (h - 90.0 * s) % 360.0
    return {"phase": phase, "along": along, "cross": cross, "side": s, "offset_nm": 4.0, "join_nm": 9.0,
            "pattern_agl": Approach.PLATFORM_FT, "speeds": (210.0, 180.0, 170.0), "heading": heading,
            "agl": Approach.PLATFORM_FT, "kt": 210.0}
