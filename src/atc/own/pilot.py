"""Our own aircraft, one flight each, step by step. Each step that needs ATC waits for it: the pilot sets `wants`,
the controller (manager.OwnAtc) answers with `clear()`, and the pilot moves once that radio exchange has been heard
(or after RADIO_WAIT_S if it never gets said, e.g. the user is on another frequency).

DeparturePilot: parked -> pushback -> engine start -> taxi -> holding point (stopped with its nose short of the
line) -> line up -> takeoff -> climb out -> gone.
ArrivalPilot: on final (from ~12 NM) -> landing clearance (or go-around at 1 NM without one) -> touchdown, braking
for the exit it planned -> vacate -> "contact Ground" -> taxi to its stand -> parked -> gone after a while.

On the ground they stop behind anybody on their path (the user included): what MSFS AI doesn't do.
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass

from atc.geo import distance_nm
from atc.models import Airport, Traffic
from atc.own.airport import ArrivalPaths, Crossing, DeparturePaths, Stand, arrival_paths, path_crossings
from atc.own.motion import EXIT_KT, KT, MAX_TAXI_KT, PUSH_KT, Approach, GroundMover, Path, Perf, Pose, Takeoff

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
ROLLOUT_TAXI_KT = 25.0  # the rollout becomes a taxi below this
PARKED_FOR_S = (240.0, 480.0)
SPACING_NM = 3.0  # slows to the speed of the aircraft ahead on final inside this
# A pilot doesn't move the instant a clearance ends: seconds from "heard" to moving (Valen: a small random delay)
REACT_S = {"push": (6.0, 12.0), "taxi": (3.0, 8.0), "takeoff": (2.0, 5.0), "taxi_in": (2.0, 6.0)}
LINEUP_PAUSE_S = (2.0, 5.0)  # lined up and stopped: spooling up before the roll
ROLLING_TAKEOFF_SHARE = 0.5  # the others stop on the centerline first


@dataclass
class Flight:
    callsign: str  # ICAO form, "ARG1216": also the sim's ATC id of the object
    title: str  # model to create
    type_icao: str
    airline: str | None = None  # ICAO airline code


class _Pilot:
    kind = ""

    def __init__(self, flight: Flight, airport: Airport, perf: Perf, now: float) -> None:
        self.flight, self.airport, self.perf = flight, airport, perf
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
        stops = [x for x in (block, end_stop, nxt.s_hold if nxt else None) if x is not None]
        self.mover.step(dt, MAX_TAXI_KT, min(stops) if stops else None)
        self.pose = self.mover.pose()
        if nxt and self.mover.s >= nxt.s_hold - 0.5 and self.mover.v < 0.2 and (block is None or block >= nxt.s_hold):
            self.wants, self.resume = "cross", resume  # "holding short of runway one three"
            self._go("hold_short", now)

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
                 push_after_s: float = 20.0) -> None:
        super().__init__(flight, airport, perf, now)
        self.paths = paths
        st = paths.stand
        self.pose = Pose(st.lat, st.lon, 0.0, st.heading)
        self.state = "parked"
        self.push_at = now + push_after_s
        self.takeoff: Takeoff | None = None
        self.handed_off = False
        self.rolling = False
        self.immediate = False  # "cleared for immediate takeoff": no stop on the centerline
        self.reported_airborne = False

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
            here = (self.pose.lat, self.pose.lon)
            self.rolling = self.immediate or self.rng.random() < ROLLING_TAKEOFF_SHARE
            self.mover = GroundMover(Path([here] + self.paths.lineup), v=0.0, run_through=self.rolling)
            self._go("lining_up", now)
        elif s == "lining_up":
            self.mover.step(dt, LINEUP_KT)
            self.pose = self.mover.pose()
            if self.rolling and self.mover.s >= self.mover.path.length - 1.0:
                self._roll(now)
            elif not self.rolling and self.mover.done:
                self.go_at = now + self.rng.uniform(*LINEUP_PAUSE_S)
                self._go("lined_up", now)
        elif s == "lined_up" and now >= (self.go_at or now):
            self.go_at = None
            self._roll(now)
        elif s == "takeoff":
            self.takeoff.step(dt)
            self.pose = self.takeoff.pose()
            if self.pose.agl_ft >= HANDOFF_AGL_FT and not self.handed_off and self.wants is None:
                self.wants = "handoff"
            far = distance_nm(self.pose.lat, self.pose.lon, self.airport.lat, self.airport.lon) > GONE_NM
            if far or self.pose.agl_ft > GONE_AGL_FT:
                self._go("gone", now)


class ArrivalPilot(_Pilot):
    kind = "arrival"

    def __init__(self, flight: Flight, airport: Airport, runway, threshold: tuple[float, float], stand: Stand,
                 perf: Perf, now: float, net, dist_nm: float = 12.0, parked_for_s: float = 300.0) -> None:
        super().__init__(flight, airport, perf, now)
        self.runway, self.stand, self.net = runway, stand, net
        self.approach = Approach(*threshold, runway.heading_deg or 0.0, perf, dist_nm)
        self.pose = self.approach.pose()
        self.state = "final"
        self.landing_cleared = False
        self.checked_in = False
        self.paths: ArrivalPaths | None = None
        self.parked_for_s = parked_for_s
        self.planned_exit_m: float | None = None

    def _cleared_in_place(self, what: str, now: float) -> bool:
        if what == "landing":
            self.landing_cleared = True
            return True
        if what in ("checkin", "go_around", "ground"):
            return True
        return False

    @property
    def final_nm(self) -> float:
        return self.approach.dist_nm

    def order_go_around(self, now: float) -> None:
        """Tower: "go around" (the runway got occupied after the landing clearance)."""
        if self.state == "final" and not self.approach.on_ground:
            self.approach.go_around()
            self.landing_cleared = False
            self.wants = None
            self._go("go_around", now)

    def step(self, now: float, dt: float, others: list[Traffic]) -> None:
        s = self.state
        a = self.approach
        if s == "final":
            a.slow_to_kt = self._speed_behind(others)
            a.step(dt)
            self.pose = a.pose()
            if not self.checked_in and a.dist_nm <= ASK_LANDING_NM and self.wants is None:
                self.wants = "landing"
            if not self.landing_cleared and a.dist_nm <= GO_AROUND_NM and not a.on_ground:
                a.go_around()
                self.wants = "go_around"
                self._go("go_around", now)
            elif a.on_ground:
                self._plan_exit()
                self._go("rollout", now)
        elif s == "go_around":
            a.step(dt)
            self.pose = a.pose()
            if distance_nm(self.pose.lat, self.pose.lon, self.airport.lat, self.airport.lon) > GONE_NM \
                    or self.pose.agl_ft > GONE_AGL_FT:
                self._go("gone", now)
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
