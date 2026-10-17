"""Our own traffic, all of it: the aircraft alive now, their controller, their radio, and the threads.

Two threads: one reads the sim (the user's aircraft, the traffic list) twice a second, which can block on
SimConnect; the other only moves the aircraft, once per sim frame (`injector.wait`), so a slow read never makes them
stutter (2026-10-07: "va un poco tosco y lageadito"). The aircraft live on a sim clock that stops while the sim is
paused and runs at the sim rate. Once a second `_answer` handles what the pilots are waiting for and puts the
exchange on the radio. Radio timestamps stay on the wall clock (the chatter bus uses it).

How many: `schedule.movements_per_hour` for the airport the user is at or near (FS Traffic's schedule, boosted at
small fields; GA only where there is none), split between departures (from free stands) and arrivals (on a 12 NM
final, spaced behind whoever is ahead). Phase 3 of docs/OWN_TRAFFIC_PLAN.md (one runway controller shared with the
user's clearances) is still to come: here our aircraft keep clear of the user, the user's ATC sees them as traffic.
"""

from __future__ import annotations

import os
import random
import threading
import time
from pathlib import Path

from atc import own, phrase
from atc.clearance import _freq
from atc.facility import callsign_for, resolve_facility
from atc.geo import distance_nm
from atc.models import Airport, OwnState, Traffic
from atc.own import catalog as catalog_mod
from atc.own import schedule
from atc.own.airport import departure_paths, free_stand, push_words, stands
from atc.own.motion import APRON_KT, Perf, perf
from atc.own.pilot import LINEUP_PAUSE_S, REACT_S, ArrivalPilot, DeparturePilot, Flight, _Pilot

USER = "(you)"
SEPARATION_NM = 1.5  # the previous departure this far out (and airborne) before the next takeoff clearance
ARRIVAL_SPAWN_NM = 12.0
FINAL_QUEUE_EXTRA_NM = 10.0  # the last of a line on the final at most this much further out (three in a row)
ARRIVAL_SPACING_NM = 7.0  # the one before has vacated by the time the next is at 1 NM
ARRIVAL_SPACING_DEP_NM = 9.0  # with departures waiting: a gap for one between two arrivals
DEPARTURE_LOOKAHEAD_S = 240.0  # a departure pushing within this long gets its gap from Approach already
FINAL_BUSY_RETRY_S = 60.0  # an arrival with no room on the final yet: tried again this soon
RUNWAY_VACATE_S = 70.0  # a landing one is off the runway this long after touchdown (ours: 60-70 s, at most 80)
LUAW_DEPARTING_S = 60.0  # one rolling away ahead of a "line up and wait": airborne and far enough after ~this
LUAW_WAIT_S = 60.0  # ... and one lined up to wait there is usually gone this much later than if it were cleared
LUAW_BUFFER_S = 10.0  # "line up and wait" only with this much more room than the takeoff itself needs...
LINED_MARGIN_S = 12.0  # ... which, lined up, goes with this margin (immediate takeoff) instead of DEPARTURE_MARGIN_S
DEPARTURE_MARGIN_S = 25.0  # the departure is off the runway this long before the arrival reaches the threshold
MAX_DEPARTURE_QUEUE = 3  # no new departure while this many are taxiing out / holding
CROSS_FINAL_S = 90.0  # no runway crossing with somebody this close to landing on it
CROSS_MARGIN_S = 20.0  # a cleared arrival is not sent around for ours crossing if they are off this long before
CROSS_SPEED_MS = 4.0  # across a runway from a standstill at the hold line (~8 kt on average)
IMMEDIATE_NM = 5.0  # an arrival inside this (but far enough): "cleared for immediate takeoff"
GO_AROUND_ORDER_NM = 2.5  # a cleared arrival of ours is sent around if the runway gets occupied inside this
ATIS_FORGET_SHARE = 0.1  # pilots whose first call has no ATIS letter (ATC asks them to confirm it)
BUBBLE_NM = 25.0  # new traffic only at an airport the user is this close to
ATC_EVERY_S = 1.0
READ_EVERY_S = 0.5
JOIN_SHARE = 0.5  # arrivals onto an empty final: this share join from a downwind or a base (H2)
TURNAROUND_SHARE = 0.6  # airline arrivals that leave again from their stand (H1)...
TURNAROUND_S = (45 * 60.0, 90 * 60.0)  # ... after this long parked (sim time)
CIRCUIT_SHARE = 0.3  # GA departures that stay in the circuit (H3), at airline airports...
CIRCUIT_SHARE_LOCAL = 0.7  # ... and at fields without airlines
CIRCUIT_LAP_S = 300.0  # a GA circuit, takeoff to the threshold again (1000 ft, ~90 kt): about five minutes
LVP_SPACING = 1.5  # low-visibility procedures (A5): arrivals this much farther apart...
LVP_DEPARTURE_EXTRA_S = 60.0  # ... and a departure gone this much earlier before an arrival


PUSH_CLEAR_M = 100.0  # nobody taxiing this close to a pushback's path when it is approved
PUSH_TAIL_CLEAR_M = 70.0  # nobody at all this close to where the tug leaves it on the taxiway
TAXI_PUSH_CLEAR_M = 150.0  # no taxi clearance / second push with a pushback this close
GROUND_WAITING = ("clear_of_runway", "taxi_in_radio", "starting", "ask_taxi", "taxi_radio")
REROUTE_AFTER_S = 40.0  # stopped this long for one that can't move: a new route round it
PUSH_DEADLOCK_S = 45.0  # two of ours pushing into each other this long: the first one goes on
DEPARTURE_WAIT_S = 180.0  # a departure at the holding point this long: no new arrivals until it has gone
DEADLOCK_S = 120.0  # ... and if there is none, after this long the last resort
CROSSING_STUCK_S = 4.0  # ... at once, though, when it is stopped on a runway it is crossing
VACATE_AHEAD_NM = 6.0  # an arrival of ours this close: no departure sent to a holding point on its way off
HEAD_ON_DEG = 135.0  # two taxi routes this far apart in direction where they meet: head-on
HEAD_ON_M = 25.0
NOSE_GEAR_SHARE = 0.38  # the nose gear sits this share of the length ahead of the reference point
TUG_AHEAD_M = 2.0
TUG_YAW = float(os.environ.get("ATC_OWN_TUG_YAW", "180"))  # VERIFY: the tug faces the aircraft
TUG_STAYS_S = 12.0
WARM_ARRIVAL = 0.7  # at the start (times how busy the field is, 20 movements/hour = 1): one already on final
WARM_DEPARTURE = 0.7  # ... and one already taxiing out or at the holding point
WARM_FINAL_NM = (3.5, 8.0)
WARM_CLEAR_M = 70.0  # a warm-started departure not this close to anybody


def _ctaf(a: Airport):
    """The common traffic frequency of a field without a Tower (CTAF, UNICOM, A/D...), or None."""
    return _freq(a, "CTAF", "UNICOM", "ATF", "A/D", "RDO")


def _every(length: float, step: float):
    s = 0.0
    while s < length:
        yield s
        s += step
    yield length


def _final_time_s(p: Perf, nm: float) -> float:
    """Seconds from `nm` out on the final to the threshold, flown at motion.Approach's speeds (Vapp + 40 kt beyond
    8 NM, slowing to Vapp by 5 NM)."""
    from atc.own.motion import Approach

    t, x, step = 0.0, max(0.0, nm), 0.25
    while x > 0.0:
        d = min(step, x)
        t += d / Approach.profile_kt(p, x - d / 2) * 3600.0
        x -= d
    return t


def _user_traffic(u: OwnState) -> Traffic:
    return Traffic(USER, u.lat, u.lon, u.alt_msl_ft, u.gs_kt, u.heading_deg, u.on_ground, type=u.aircraft_type)


class OwnTraffic:
    def __init__(self, world, sim, injector, bus=None, catalog=None, community: Path | None = None,
                 wind=None, rng: random.Random | None = None, factor: float = 1.0, max_alive: int | None = None,
                 auto: bool = False, atis_letter=None) -> None:
        self.world, self.sim, self.injector, self.bus = world, sim, injector, bus
        self.community = community if community is not None else catalog_mod.community_dir()
        self.catalog = catalog if catalog is not None else catalog_mod.load(self.community)
        self.wind = wind or (lambda airport: (None, None))  # airport -> (true direction, knots) at the surface
        self.rng = rng or random.Random()
        self.factor, self.max_alive_fixed, self.auto = factor, max_alive, auto
        self.pilots: dict[str, _Pilot] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last_wall: float | None = None
        self.clock: float | None = None  # sim time: stops while paused, runs at the sim rate
        self.last_atc = -1e9
        self.next_spawn: float | None = None
        self.owe_arrival = False  # the last arrival found no room on the final: the next spawn is an arrival
        self.user: OwnState | None = None
        self.sim_traffic: list[Traffic] = []
        self.tugs: dict[str, dict] = {}
        self._stand_cache: dict[str, list] = {}
        self.atis_letter = atis_letter  # airport -> the current ATIS letter word ("Bravo"), or None (no ATIS)

    # --- spawning ------------------------------------------------------------------------------------------
    def _now(self) -> float:
        return self.clock if self.clock is not None else time.monotonic()

    def _departure_queue(self) -> int:
        """Departures taxiing out or waiting at the holding point."""
        return sum(1 for p in self.pilots.values() if isinstance(p, DeparturePilot)
                   and p.state in ("ask_taxi", "taxi_radio", "taxiing", "holding"))

    def _departures_coming(self, airport: Airport, now: float) -> bool:
        """A departure of ours at the runway by the time a new arrival lands (~5 min): taxiing out, pushing, or about
        to push; or one of ours waiting to cross the runway. Tower asks Approach for gaps ahead of time, not once it
        is already waiting."""
        return any(p.airport.icao == airport.icao and (p.state == "hold_short" or isinstance(p, DeparturePilot) and (
            p.state in ("ask_push", "push_radio", "pushing", "starting", "ask_taxi", "taxi_radio", "taxiing", "holding")
            or (p.state == "parked" and p.push_at - now < DEPARTURE_LOOKAHEAD_S))) for p in self.pilots.values())

    def _reserved(self) -> list:
        return [p.stand for p in self.pilots.values() if isinstance(p, ArrivalPilot)] + \
               [p.paths.stand for p in self.pilots.values() if isinstance(p, DeparturePilot) and p.state == "parked"]

    def _taken(self, airport: Airport) -> list[Traffic]:
        taken = [p.traffic(airport.elevation_ft) for p in self.pilots.values()] + list(self.sim_traffic)
        if self.user is not None:
            taken.append(_user_traffic(self.user))
        return taken

    def _flight(self, airport: Airport, dep: schedule.Departure | None, ga: bool) -> tuple[Flight, str | None] | str:
        if dep is None:
            dep = schedule.ga_flight(airport.country, self.rng) if ga else \
                schedule.pick(airport.icao, airport.country, self.community, self.rng, set(self.pilots))
        if dep.callsign in self.pilots:
            return f"{dep.callsign} is already flying"
        title = self.catalog.pick(dep.airline or None, dep.type_icao, self.rng)
        if title is None and getattr(self.injector, "needs_title", True):
            return f"no model for {dep.airline} {dep.type_icao} (FSLTL / FS Traffic not found: ATC_COMMUNITY_DIR)"
        return Flight(dep.callsign, title or "", dep.type_icao, dep.airline or None, dep.destination), title

    def spawn_departure(self, airport: Airport, now: float | None = None, dep: schedule.Departure | None = None,
                        ga: bool = False, taxiing: float | None = None, push_after: float | None = None,
                        circuits: int | None = None) -> str:
        """`taxiing` (warm start): already taxiing out, that share of the way to the holding point (1.0 = at it).
        `circuits` (GA): touch and goes before the full stop (H3); None = by chance, more at fields without airlines."""
        from atc.runway import runway_in_use

        now = self._now() if now is None else now
        net = self.world.taxi.get(airport.icao)
        if net is None:
            return f"no taxi map for {airport.icao}"
        wdir, wkt = self.wind(airport)
        rwy = runway_in_use(airport, wdir, wkt, use="departure")
        if rwy is None:
            return "no runway"
        made = self._flight(airport, dep, ga)
        if isinstance(made, str):
            return made
        flight, title = made
        p = perf(flight.type_icao)
        stand = free_stand(stands(airport.icao, net), self._taken(airport), p.wingspan_m, airport, self.rng,
                           self._reserved(), ga=p.vapp_kt < 100)
        if stand is None:
            return "no free stand"
        paths = departure_paths(net, airport, rwy, stand)
        if paths is None:
            return f"no taxi route from stand {stand.ref} to runway {rwy.ident}"
        if circuits is None:
            local = not schedule._todays_cached(airport.icao, self.community)
            share = CIRCUIT_SHARE_LOCAL if local else CIRCUIT_SHARE
            circuits = self.rng.randint(2, 4) if p.vapp_kt < 100 and self.rng.random() < share else 0
        pilot = DeparturePilot(flight, airport, paths, p, now,
                               push_after_s=push_after if push_after is not None else self.rng.uniform(20.0, 50.0),
                               circuits=circuits, dest_pos=schedule.airport_position(flight.destination))
        where = f"at stand {stand.ref}"
        if taxiing is not None:
            taken = self._taken(airport)
            for share in (taxiing, 0.8, 0.6, 0.4):
                pilot.start_taxiing(now, share)
                if all(distance_nm(pilot.pose.lat, pilot.pose.lon, t.lat, t.lon) * 1852.0 >= WARM_CLEAR_M
                       for t in taken):
                    break
            else:
                return "no room on the taxi route"
            where = "taxiing" if pilot.mover.s < pilot.mover.path.length - pilot.perf.length_m else "at the holding point"
        with self.lock:
            self.pilots[flight.callsign] = pilot
        self.injector.create(flight.callsign, flight.title, pilot.pose.lat, pilot.pose.lon, pilot.pose.heading_deg,
                             airport.elevation_ft, type_icao=flight.type_icao)
        return self._said(f"{flight.callsign} {flight.type_icao} {where} -> runway {rwy.ident}"
                          + (f" via {paths.via}" if paths.via else "") + (f", {circuits} circuits" if circuits else "")
                          + (f" -> {flight.destination}" if flight.destination else "") + (f" ({title})" if title else ""))

    def spawn_arrival(self, airport: Airport, now: float | None = None, dep: schedule.Departure | None = None,
                      ga: bool = False, dist_nm: float | None = None, join: str | None = "") -> str:
        """`dist_nm` (warm start): this close in on final instead of the usual spawn distance. `join`: "downwind" /
        "base" (H2: onto the final from the side, as radar would bring it), "" = straight in (the default); None = by
        chance when the final is empty (the automatic traffic)."""
        from atc.runway import runway_in_use
        from atc.sequence import final_distance, threshold

        now = self._now() if now is None else now
        wdir, wkt = self.wind(airport)
        rwy = runway_in_use(airport, wdir, wkt, use="arrival")
        if rwy is None or rwy.heading_deg is None:
            return "no runway"
        made = self._flight(airport, dep, ga)
        if isinstance(made, str):
            return made
        flight, title = made
        p = perf(flight.type_icao)
        dist = dist_nm if dist_nm is not None else ARRIVAL_SPAWN_NM if p.vapp_kt >= 100 else 5.0
        spacing = ARRIVAL_SPACING_DEP_NM if self._departures_coming(airport, now) else ARRIVAL_SPACING_NM
        if self._lvp(airport):  # low-visibility procedures: the ILS critical areas need wider gaps (A5)
            spacing *= LVP_SPACING
        # behind everybody already on this final, by time at the threshold: spawned faster further out, the new one
        # would close a gap measured in miles now (9 NM at spawn was ~7 NM at the threshold, no slot for a departure)
        gap_s = spacing / p.vapp_kt * 3600.0
        ahead_s = 0.0
        for t in self._taken(airport):
            d = final_distance(airport, rwy, t) if not t.on_ground else None
            q = self.pilots.get(t.callsign)
            if d is not None and not isinstance(q, ArrivalPilot):  # (ours: by their own speed profile below)
                ahead_s = max(ahead_s, d / max(60.0, t.gs_kt) * 3600.0)
        for q in self.pilots.values():
            if isinstance(q, ArrivalPilot) and q.state == "final" and q.runway.ident == rwy.ident:
                ahead_s = max(ahead_s, _final_time_s(q.perf, q.final_nm))
            elif isinstance(q, ArrivalPilot) and q.state == "joining" and q.join is not None \
                    and q.runway.ident == rwy.ident and not q.circuit:  # will be on the final at its join point
                ahead_s = max(ahead_s, _final_time_s(q.perf, q.join.join_nm))
        if ahead_s > 0.0:
            while _final_time_s(p, dist) < ahead_s + gap_s and dist <= ARRIVAL_SPAWN_NM + FINAL_QUEUE_EXTRA_NM:
                dist += 0.5
        dep_s = max([r for q in self.pilots.values() if isinstance(q, DeparturePilot) and q.airport.icao == airport.icao
                     and q.paths.runway.ident == rwy.ident and (r := self._dep_remaining_s(q, now, True)) is not None],
                    default=0.0)
        while dep_s > 0.0 and _final_time_s(p, dist) < dep_s + DEPARTURE_MARGIN_S \
                and dist <= ARRIVAL_SPAWN_NM + FINAL_QUEUE_EXTRA_NM:
            dist += 0.5  # one of ours cleared for takeoff (a backtrack): airborne before this one is near
        if dist > ARRIVAL_SPAWN_NM + FINAL_QUEUE_EXTRA_NM:
            return "final too busy"
        joining = any(isinstance(q, ArrivalPilot) and q.state == "joining" and q.airport.icao == airport.icao
                      for q in self.pilots.values())
        if join is None:
            empty = dist <= ARRIVAL_SPAWN_NM and dist_nm is None and not joining
            side_ok = empty and p.vapp_kt >= 100 and self.rng.random() < JOIN_SHARE
            join = self.rng.choice(("downwind", "base")) if side_ok else ""
        net = self.world.taxi.get(airport.icao)
        all_stands = stands(airport.icao, net) if net is not None else []
        stand = free_stand(all_stands, self._taken(airport), p.wingspan_m, airport, self.rng, self._reserved(),
                           ga=p.vapp_kt < 100) if all_stands else None
        if stand is None:
            return "no free stand"
        from atc.own.pilot import side_join

        turns = bool(flight.airline) and self.rng.random() < TURNAROUND_SHARE
        turnaround = self.rng.uniform(*TURNAROUND_S) if turns else None
        spec = side_join(rwy, p, join, self.rng.choice((-1.0, 1.0))) if join else None
        with self.lock:
            self.pilots[flight.callsign] = ArrivalPilot(
                flight, airport, rwy, threshold(airport, rwy), stand, p, now, net, dist_nm=dist,
                parked_for_s=float("inf") if turnaround else self.rng.uniform(240.0, 480.0), join=spec,
                turnaround_s=turnaround)
        pose = self.pilots[flight.callsign].pose
        self.injector.create(flight.callsign, flight.title, pose.lat, pose.lon, pose.heading_deg,
                             airport.elevation_ft, agl_ft=pose.agl_ft, type_icao=flight.type_icao)
        where = f"joining a {join} for runway {rwy.ident}" if join else f"on a {dist:.0f} NM final runway {rwy.ident}"
        return self._said(f"{flight.callsign} {flight.type_icao} {where} -> stand {stand.ref}"
                          + (", turnaround" if turnaround else "") + (f" ({title})" if title else ""))

    @staticmethod
    def _said(msg: str) -> str:
        print(f"[own traffic: {msg}]")
        return msg

    # --- each update ---------------------------------------------------------------------------------------
    def step(self, now: float, user: OwnState | None = None, sim_traffic: list[Traffic] | None = None) -> None:
        """`now` = wall clock. Advances the sim clock (not while paused, at the sim rate) and everything on it."""
        if user is not None:
            self.user = user
        if sim_traffic is not None:
            self.sim_traffic = [t for t in sim_traffic if not own.is_own(t.callsign)]
        wall_dt = 0.0 if self.last_wall is None else min(0.5, max(0.0, now - self.last_wall))
        self.last_wall = now
        rate = getattr(self.injector, "sim_rate", 1.0) or 1.0
        dt = 0.0 if getattr(self.injector, "paused", False) else wall_dt * rate
        self.clock = now if self.clock is None else self.clock + dt
        t = self.clock
        with self.lock:
            pilots = list(self.pilots.values())
        others = list(self.sim_traffic) + [p.traffic(p.airport.elevation_ft) for p in pilots]
        if self.user is not None:
            others.append(_user_traffic(self.user))
        parked = self._parked_ids(pilots, others)
        reserved = {(q.airport.icao, q.paths.runway.ident) for q in pilots if isinstance(q, DeparturePilot)
                    and (q.waiting_lined or (q.luaw and q.state in ("takeoff_radio", "lining_up")))}
        for p in pilots:  # who is already stopped for whom: the one waited for goes first (no deadlock)
            p.yielding = {q.flight.callsign for q in pilots if q.blocked_by == p.flight.callsign}
            p.parked_ids = parked
            if isinstance(p, ArrivalPilot):  # a departure lined up to wait there: the downwind is extended
                p.runway_reserved = (p.airport.icao, p.runway.ident) in reserved
        for p in pilots:
            p.step(t, dt, others)
            if p.blocked_by and p.mover is not None and p.mover.v < 0.1:
                p.blocked_since = p.blocked_since if p.blocked_since is not None else t
            else:
                p.blocked_since = None
        swapped = False
        for p in pilots:  # a GA departure staying in the circuit (H3), an arrival leaving again (H1)
            if isinstance(p, DeparturePilot) and p.state == "circuit_ready":
                swapped |= self._to_circuit(p, t)
            elif isinstance(p, ArrivalPilot) and p.state == "parked_in" and p.turnaround_s is not None \
                    and t - p.since >= p.turnaround_s:
                swapped |= self._turnaround(p, t)
        if swapped:
            with self.lock:
                pilots = list(self.pilots.values())
        if t - self.last_atc >= ATC_EVERY_S:
            for p in pilots:
                self._unstick(p, t)
            self.last_atc = t
            for p in pilots:
                if isinstance(p, ArrivalPilot) and self._clear_for_emergency(p, t):
                    continue
                if isinstance(p, ArrivalPilot) and p.state == "final" and p.wind_kt is None:
                    p.wind_kt = self.wind(p.airport)[1]  # (once on the final: go-arounds a little likelier in wind)
                if isinstance(p, ArrivalPilot) and p.tower_should_send_around():  # (it still waits for "cleared")
                    self._send_around_uncleared(p, t, others)
                elif p.wants:
                    self._answer(p, t, others)
                elif isinstance(p, ArrivalPilot):
                    self._check_cleared_arrival(p, t, others)
        own.publish({p.flight.callsign: p.traffic(p.airport.elevation_ft) for p in pilots if not p.done})
        self._shared_runway_state(pilots)
        for p in pilots:
            if p.done:
                self.injector.remove(p.key)
                with self.lock:
                    self.pilots.pop(p.flight.callsign, None)
                print(f"[own traffic: {p.flight.callsign} gone]")
                continue
            self.injector.update(p.key, p.pose)
            self._lights(p)
            if isinstance(p, DeparturePilot):
                self._tug(p, t)
        self.injector.pump()
        if self.auto:
            self._auto_spawn(t)

    def _swap(self, old: _Pilot, new: _Pilot) -> None:
        """The same sim object goes on under a new pilot (and maybe a new flight number)."""
        new.key, new.rng = old.key, old.rng
        with self.lock:
            self.pilots.pop(old.flight.callsign, None)
            self.pilots[new.flight.callsign] = new

    def _to_circuit(self, p: DeparturePilot, now: float) -> bool:
        """A GA departure 400 ft up, staying in the circuit (H3): from now on a circuit flight round `p.circuits`
        touch and goes, then a full stop and a stand (the one it left if no other is free)."""
        from atc.own.motion import Airborne
        from atc.own.pilot import circuit_join
        from atc.sequence import threshold

        a, rwy, pose = p.airport, p.paths.runway, p.pose
        net = self.world.taxi.get(a.icao)
        all_stands = stands(a.icao, net) if net is not None else []
        stand = free_stand(all_stands, self._taken(a), p.perf.wingspan_m, a, self.rng, self._reserved(), ga=True) \
            or p.paths.stand
        air = Airborne(pose.lat, pose.lon, pose.agl_ft, pose.heading_deg, pose.gs_kt, p.perf, vs_fpm=p.perf.climb_fpm)
        c = ArrivalPilot(p.flight, a, rwy, threshold(a, rwy), stand, p.perf, now, net,
                         parked_for_s=self.rng.uniform(240.0, 480.0), join=circuit_join(rwy, p.perf, air),
                         laps=p.circuits - 1)
        c.checked_in = False
        self._swap(p, c)
        print(f"[own traffic: {p.flight.callsign} in the circuit, {p.circuits} to fly]")
        return True

    def _turnaround(self, p: ArrivalPilot, now: float) -> bool:
        """An arrival parked long enough leaves again from the same stand as its airline's next flight (H1)."""
        from atc.runway import runway_in_use

        a = p.airport
        net = self.world.taxi.get(a.icao)
        rwy = runway_in_use(a, *self.wind(a), use="departure")
        paths = departure_paths(net, a, rwy, p.stand) if net is not None and rwy is not None else None
        if paths is None:
            p.turnaround_s, p.parked_for_s = None, 0.0  # nowhere to go: it goes the old way (removed)
            return False
        nxt = schedule.pick(a.icao, a.country, self.community, self.rng, set(self.pilots))
        if nxt.airline != (p.flight.airline or "") or nxt.type_icao[:3] != p.flight.type_icao[:3]:
            nxt = schedule.Departure(p.flight.airline or "", str(self.rng.randint(1000, 4999)), p.flight.type_icao)
        flight = Flight(nxt.callsign, p.flight.title, p.flight.type_icao, p.flight.airline, nxt.destination)
        if flight.callsign in self.pilots:
            return False
        d = DeparturePilot(flight, a, paths, p.perf, now, push_after_s=self.rng.uniform(20.0, 90.0),
                           dest_pos=schedule.airport_position(flight.destination))
        self._swap(p, d)
        print(f"[own traffic: {p.flight.callsign} turns around at stand {p.stand.ref} as {flight.callsign}"
              + (f" -> {flight.destination}" if flight.destination else "") + "]")
        return True

    def _shared_runway_state(self, pilots: list[_Pilot]) -> None:
        """What the user's clearances need to know about ours (departures.py, own.APRON): who is in the departure
        queue, who just got airborne or landed (wake turbulence intervals), how busy the apron is."""
        from atc import departures

        load: dict[str, int] = {}
        for p in pilots:
            icao, cs = p.airport.icao, p.flight.callsign
            if isinstance(p, DeparturePilot):
                rwy = p.paths.runway.ident
                if p.state in ("holding", "takeoff_radio", "lining_up", "lined_up"):
                    departures.ready(icao, cs, rwy)
                if p.state == "takeoff" and p.pose.agl_ft > 0 and not p.reported_airborne:
                    p.reported_airborne = True
                    departures.airborne(icao, rwy, cs, p.flight.type_icao)
                if p.state in ("pushing", "starting", "ask_taxi", "taxi_radio", "taxiing", "hold_short", "cross_radio",
                               "holding", "takeoff_radio", "lining_up", "lined_up"):
                    load[icao] = load.get(icao, 0) + 1
                if p.done:
                    departures.gone(icao, cs)
            elif isinstance(p, ArrivalPilot) and p.state == "rollout" and not getattr(p, "reported_landed", False):
                p.reported_landed = True
                departures.landed(icao, p.runway.ident, cs, p.flight.type_icao)
        own.APRON = load

    def _tug(self, p: DeparturePilot, t: float) -> None:
        """A pushback tug under the nose while it is pushed (GSX's TPX if installed), gone TUG_STAYS_S after."""
        from atc.own.catalog import tug_title
        from atc.own.motion import Pose, moved

        key = f"{p.key}~TUG"
        tug = self.tugs.get(key)
        if p.state == "pushing":
            lat, lon = moved(p.pose.lat, p.pose.lon, p.pose.heading_deg, p.perf.length_m * NOSE_GEAR_SHARE + TUG_AHEAD_M)
            heading = (p.pose.heading_deg + TUG_YAW) % 360.0
            if tug is None:
                title = tug_title(self.community, p.perf.wingspan_m)
                self.tugs[key] = {"title": title, "remove_at": None}
                if title:
                    self.injector.create_vehicle(key, title, lat, lon, heading, p.airport.elevation_ft)
            elif tug["title"]:
                self.injector.update(key, Pose(lat, lon, 0.0, heading, gs_kt=p.pose.gs_kt))
        elif tug is not None:
            if tug["remove_at"] is None:
                tug["remove_at"] = t + TUG_STAYS_S
            elif t >= tug["remove_at"] or p.done:
                if tug["title"]:
                    self.injector.remove(key)
                del self.tugs[key]

    def _lights(self, p: _Pilot) -> None:
        s = p.state
        if isinstance(p, DeparturePilot):
            moving = s not in ("parked", "ask_push", "push_radio")
            self.injector.lights(p.key, nav=True, beacon=moving, engines=moving and s != "pushing",
                                 taxi=s in ("taxiing", "takeoff_radio", "lining_up"),
                                 landing=s in ("lining_up", "takeoff", "climbout") and p.pose.agl_ft < 10000,
                                 strobe=s in ("lining_up", "takeoff", "climbout", "circuit_ready"))
        else:
            airborne = s in ("final", "go_around", "joining", "touch_go")
            self.injector.lights(p.key, nav=True, beacon=s != "parked_in", engines=s != "parked_in",
                                 landing=airborne or s == "rollout", strobe=airborne or s == "rollout",
                                 taxi=s in ("vacating", "taxiing_in"))

    def _active(self) -> int:
        """Aircraft doing something: the ones waiting parked for a departure time don't count toward the cap."""
        return sum(1 for p in self.pilots.values() if not (isinstance(p, DeparturePilot) and p.state == "parked")
                   and not (isinstance(p, ArrivalPilot) and p.state == "parked_in"))

    def _max_alive(self, rate: float) -> int:
        if self.max_alive_fixed is not None:
            return self.max_alive_fixed
        return max(3, min(14, round(rate / 2.5)))

    def _auto_spawn(self, now: float) -> None:
        if self.user is None:
            return
        apt = self.world.nearest(self.user)
        if apt is None or distance_nm(self.user.lat, self.user.lon, apt.lat, apt.lon) > BUBBLE_NM:
            return
        rate = schedule.movements_per_hour(apt, self.community, self.factor)
        if self.next_spawn is None:  # something happening soon after the start
            self.next_spawn = now + 8.0
            if rate > 0:
                self._warm_start(apt, now, rate)
        if now < self.next_spawn or self._active() >= self._max_alive(rate) or rate <= 0:
            return
        self.next_spawn = now + self.rng.expovariate(rate / 3600.0)
        scheduled = schedule._todays_cached(apt.icao, self.community)
        ga = not scheduled or self.rng.random() < schedule.GA_SHARE
        queue_full = self._departure_queue() >= MAX_DEPARTURE_QUEUE
        # a departure waiting long at the holding point (a steady arrival flow leaves no gap): Approach holds new
        # arrivals back until it has gone, as a real Tower asks for a gap
        # (one waiting long to cross the runway too: SABE, a Cessna sat 30 min at a hold-short line between arrivals)
        starved = any(((isinstance(p, DeparturePilot) and p.state == "holding") or p.state == "hold_short")
                      and now - p.since > DEPARTURE_WAIT_S for p in self.pilots.values())
        if apt.icao in own.EMERGENCY or starved:  # the user's emergency lands here: no new arrivals meanwhile
            if not queue_full:
                self.spawn_departure(apt, now, ga=ga)
            return
        if queue_full or self.owe_arrival or self.rng.random() < 0.5:
            msg = self.spawn_arrival(apt, now, ga=ga, join=None)
            self.owe_arrival = msg == "final too busy"
            if self.owe_arrival:  # Approach still has it: it comes once there is room (keeps the mix)
                self.next_spawn = min(self.next_spawn, now + FINAL_BUSY_RETRY_S)
            elif msg == "no runway" and not queue_full:
                self.spawn_departure(apt, now, ga=ga)
        else:
            msg = self.spawn_departure(apt, now, ga=ga)
            if msg == "no free stand":
                self.spawn_arrival(apt, now, ga=ga, join=None)

    APRON_SHARE = 0.25  # of the stands filled at the start (more at busy fields), at most APRON_MAX aircraft
    APRON_MAX = int(__import__("os").environ.get("ATC_OWN_APRON", "6"))  # 0 = nobody parked at the start

    def _populate_apron(self, apt: Airport, now: float, rate: float) -> None:
        """Aircraft already parked when vATC starts (Valen 2026-10-08: the apron was empty): departures waiting at their
        gates, leaving one by one over the next hour (the first within a minute or two), so there are always some
        parked and the next ones keep coming out. They don't count toward the alive cap."""
        net = self.world.taxi.get(apt.icao)
        if net is None:
            return
        n = min(self.APRON_MAX, max(2, round(len(stands(apt.icao, net)) * self.APRON_SHARE * min(1.0, rate / 12.0))))
        scheduled = schedule._todays_cached(apt.icao, self.community)
        gaps, at = [], self.rng.uniform(40.0, 120.0)  # one leaves every 75-330 s, never two together
        for _ in range(n):
            gaps.append(at)
            at += self.rng.uniform(75.0, 330.0)
        for after in gaps:
            ga = not scheduled or self.rng.random() < schedule.GA_SHARE
            self.spawn_departure(apt, now, ga=ga, push_after=after)

    def _warm_start(self, apt: Airport, now: float, rate: float) -> None:
        """Traffic already moving when vATC starts (Valen 2026-10-08: not everybody parked): now and then one on
        final and one taxiing out or at the holding point, more likely the busier the field."""
        busy = min(1.0, rate / 20.0)
        self._populate_apron(apt, now, rate)
        scheduled = schedule._todays_cached(apt.icao, self.community)
        if self.rng.random() < WARM_ARRIVAL * busy:
            ga = not scheduled or self.rng.random() < schedule.GA_SHARE
            self.spawn_arrival(apt, now, ga=ga, dist_nm=self.rng.uniform(*WARM_FINAL_NM))
        if self.rng.random() < WARM_DEPARTURE * busy:
            ga = not scheduled or self.rng.random() < schedule.GA_SHARE
            share = 1.0 if self.rng.random() < 0.5 else self.rng.uniform(0.3, 0.9)
            self.spawn_departure(apt, now, ga=ga, taxiing=share)

    # --- the controller ------------------------------------------------------------------------------------
    def _answer(self, p: _Pilot, now: float, others: list[Traffic]) -> None:
        a = p.airport
        if _freq(a, "TWR") is None and _ctaf(a) is not None:  # no Tower: the pilots tell each other (E3)
            self._answer_ctaf(p, now, others)
        elif isinstance(p, ArrivalPilot):
            self._answer_arrival(p, now, others)
        elif p.wants == "push":
            if self._push_blocked(p, others):
                return  # Ground holds the push until the taxiway behind it is clear
            way = push_words(p.paths, a.faa, f"{a.icao}{p.flight.callsign}")
            ok = ("push back approved" if a.faa else "push and start approved") + (f", {way}" if way else "")
            has, ask, ack = self._atis_words(p)
            self._say(p, now, "push", "ground", [
                ("pilot", f"{{station}}, {{cs}}, stand {phrase.spell(p.paths.stand.ref).lower()}{has}, "
                          "request push and start"),
                ("ATC", "{cs}, " + ok + ask), ("pilot", ok[:1].upper() + ok[1:] + ack + ", {cs}")])
        elif p.wants == "taxi":
            from atc.own.motion import Path

            if self._taxi_blocked(p, Path(p.paths.taxi)):
                return
            rw = phrase.runway(p.paths.runway.ident, a.faa)
            via = f" via {p.paths.via}" if p.paths.via else ""
            clr = f"runway {rw}, taxi{via}" if a.faa else f"taxi to holding point runway {rw}{via}"
            self._say(p, now, "taxi", "ground", [("pilot", "{cs}, request taxi"), ("ATC", "{cs}, " + clr),
                                                 ("pilot", clr[:1].upper() + clr[1:] + ", {cs}")])
        elif p.wants == "takeoff":
            if self._runway_blocked(p, others):
                luaw = self._lineup_wait(p, others) if p.state == "holding" else None
                if luaw is not None:  # onto the runway while the one ahead is still on it (saves the line-up time)
                    words, readback, p.behind = luaw
                    p.luaw = True
                    rw = phrase.runway(p.paths.runway.ident, a.faa)
                    self._say(p, now, "takeoff", "tower", [
                        ("pilot", (f"{{station}}, {{cs}}, holding point runway {rw}, ready for departure" if not a.faa
                                   else f"{{station}}, {{cs}}, holding short runway {rw}, ready for departure")),
                        ("ATC", "{cs}, {station}, " + words), ("pilot", readback + ", {cs}")], priority=0)
                return  # holds at the holding point (or lined up); asked again next second
            from atc import departures
            from atc.sequence import final_distance

            rwy = p.paths.runway
            rw = phrase.runway(rwy.ident, a.faa)
            wind = self._wind_words(a)
            close = [t for t in others if t.callsign != p.flight.callsign
                     and (d := final_distance(a, rwy, t)) is not None and d <= IMMEDIATE_NM]
            p.immediate = bool(close)
            what = "immediate takeoff" if close else "takeoff"
            caution = departures.wake_caution(a.icao, rwy.ident, p.flight.type_icao)
            tail = f", {caution}" if caution else ""
            circuits = getattr(p, "circuits", 0) > 0
            side = "right" if rwy.pattern_direction == "right" else "left"
            closed = f"make {side} closed traffic, " if circuits and a.faa else ""
            report = ", report downwind" if circuits and not a.faa else ""
            call = [] if p.waiting_lined else [  # lined up already: Tower just clears it
                ("pilot", (f"{{station}}, {{cs}}, holding point runway {rw}, ready for departure" if not a.faa
                           else f"{{station}}, {{cs}}, holding short runway {rw}, ready for departure")
                 + (", for circuits" if circuits and not a.faa else ", closed traffic" if circuits else ""))]
            self._say(p, now, "takeoff", "tower", call + [
                ("ATC", ("{cs}, {station}, " if call else "{cs}, ") + closed + (f"{wind}, " if wind else "") +
                 f"runway {rw}, cleared for {what}{tail}{report}"),
                ("pilot", f"Cleared for {what} runway {rw}{', wilco' if report else ''}, {{cs}}")], priority=0)
        elif p.wants == "cross":
            self._answer_cross(p, now, others)
        elif p.wants == "handoff":
            dep = _freq(a, "DEP", "APP", "ARR")
            if dep is None:
                p.clear("handoff", now, said=True)
                return
            f = phrase.frequency(dep.mhz, a.faa)
            self._say(p, now, "handoff", "tower", [("ATC", f"{{cs}}, contact departure {f}, good day"),
                                                   ("pilot", f"Departure {f}, good day, {{cs}}")])

    def _circuit_words(self, p: ArrivalPilot) -> tuple[str, str]:
        """('left' / 'right', 'touch and go' / 'full stop') for a circuit flight's calls."""
        side = "right" if p.join is not None and p.join.side > 0 else "left"
        return side, ("touch and go" if p.laps > 0 else "full stop")

    def _answer_circuit(self, p: ArrivalPilot, now: float, others: list[Traffic]) -> None:
        """Our circuit traffic at a towered field (H3): the downwind report gets the number in sequence (number one:
        the clearance as well), the base call the touch and go or landing clearance."""
        a = p.airport
        rw = phrase.runway(p.runway.ident, a.faa)
        side, intent = self._circuit_words(p)
        cleared = "cleared for the option" if a.faa and p.laps > 0 else \
            "cleared touch and go" if p.laps > 0 else "cleared to land"
        why = self._landing_blocked(p, others)
        wind = self._wind_words(a)
        leg = "downwind" if p.wants == "downwind" else "final"
        call = ("pilot", f"{{station}}, {{cs}}, {side + ' ' if leg == 'downwind' else ''}{leg} runway {rw}, {intent}")
        if leg == "downwind" and (why is not None or not a.faa):
            # ICAO: the number in sequence, the clearance on final; FAA clears number one on the downwind
            number = why if why is not None and why.startswith("number") else "number one"
            self._say(p, now, "downwind", "tower", [call, ("ATC", f"{{cs}}, {number}, report final"),
                                                    ("pilot", f"{number[:1].upper()}{number[1:]}, wilco, {{cs}}")])
            return
        if why is None:
            lines = ([call] if not p.checked_in else []) + [  # (told "continue approach" before: just the clearance)
                ("ATC", "{cs}, " + ("number one, " if leg == "downwind" else "") + (f"{wind}, " if wind else "")
                 + f"runway {rw}, {cleared}"), ("pilot", f"{cleared[:1].upper()}{cleared[1:]} runway {rw}, {{cs}}")]
            p.landing_cleared = p.checked_in = True
            self._say(p, now, p.wants, "tower", lines, priority=0)
            return
        if not p.checked_in:  # the clearance comes once the runway is free: it still wants it (never granted here:
            p.checked_in = True  # _say would have cleared it to land, SARC soak 2026-10-11)
            self._radio(p, now, "tower", [call, ("ATC", f"{{cs}}, continue approach, {why}"),
                                          ("pilot", "Continue approach, {cs}")])

    def _answer_ctaf(self, p: _Pilot, now: float, others: list[Traffic]) -> None:
        """An untowered field (E3): nobody clears anything; the pilot waits for the runway itself (the same checks as
        Tower's) and says what it does on the common frequency: "Corrientes traffic, Lima Victor Alfa Bravo Charlie,
        left downwind runway two zero, touch and go, Corrientes"."""
        what = p.wants
        rw = phrase.runway((p.paths.runway if isinstance(p, DeparturePilot) else p.runway).ident, p.airport.faa)
        if isinstance(p, DeparturePilot):
            if what == "takeoff":
                if self._runway_blocked(p, others):
                    return  # looking out for traffic on final before it lines up
                self._announce(p, now, what, f"departing runway {rw}" + (", remaining in the circuit"
                                                                          if p.circuits else ""))
            elif what == "taxi":
                self._announce(p, now, what, f"taxiing to runway {rw}")
            else:
                p.clear(what, now, said=True)
            return
        side, intent = self._circuit_words(p) if p.circuit else ("", "")
        if what == "downwind":
            self._announce(p, now, what, f"{side} downwind runway {rw}, {intent}")
        elif what == "landing":
            p.landing_cleared = p.checked_in = True  # no clearance at an untowered field
            self._announce(p, now, what, f"{side} base runway {rw}, {intent}" if p.circuit else f"final runway {rw}")
        elif what == "ground":
            self._announce(p, now, what, f"clear of runway {rw}")
        else:
            p.clear(what, now, said=True)

    def _announce(self, p: _Pilot, now: float, what: str, text: str) -> None:
        p.clear(what, now, said=True)
        f = _ctaf(p.airport)
        if self.bus is None or f is None:
            return
        from atc.airports.gen import spoken_name
        from atc.chatter import Exchange, ai_callsign, voice_for
        from atc.voices import country_of

        a = p.airport
        field = a.spoken_name or spoken_name(a.name) or a.name
        cs = ai_callsign(p.traffic(a.elevation_ft), a.faa)
        cap = cs[:1].upper() + cs[1:]
        self.bus.offer(Exchange(p.flight.callsign, [(cap, f"{field} traffic, {cs}, {text}, {field}.",
                                                     voice_for(p.flight.callsign))], time.monotonic(), "advisory",
                                1, country_of(None, p.flight.callsign)))

    def _answer_arrival(self, p: ArrivalPilot, now: float, others: list[Traffic]) -> None:
        a = p.airport
        rw = phrase.runway(p.runway.ident, a.faa)
        if p.circuit and p.wants in ("downwind", "landing"):
            self._answer_circuit(p, now, others)
            return
        if p.wants == "landing":
            has, ask, ack = self._atis_words(p) if not p.checked_in else ("", "", "")
            call = ("pilot", f"{{station}}, {{cs}}, {self._approach_words(p)} runway {rw}{has}")
            why = self._landing_blocked(p, others)
            if why is None:
                wind = self._wind_words(a)
                lines = ([call] if not p.checked_in else []) + [
                    ("ATC", "{cs}" + (", {station}" if not p.checked_in else "") + ", " + (f"{wind}, " if wind else "")
                     + f"runway {rw}, cleared to land" + ask), ("pilot", f"Cleared to land runway {rw}{ack}, {{cs}}")]
                p.checked_in = True
                self._say(p, now, "landing", "tower", lines, priority=0)
            elif not p.checked_in:  # first call: continue, the clearance comes when the runway is free
                p.checked_in = True
                self._radio(p, now, "tower", [call, ("ATC", "{cs}, {station}, continue approach, " + why + ask),
                                              ("pilot", "Continue approach" + ack + ", {cs}")])
        elif p.wants == "go_around" and p.circuit:  # a circuit: round again
            self._say(p, now, "go_around", "tower", [("pilot", "{cs}, going around"),
                                                     ("ATC", "{cs}, roger, report downwind"),
                                                     ("pilot", "Wilco, {cs}")])
        elif p.wants == "go_around":
            self._say(p, now, "go_around", "tower", [
                ("pilot", "{cs}, going around"),
                ("ATC", "{cs}, roger, " + ("fly runway heading, climb and maintain three thousand" if a.faa
                                           else "climb straight ahead to altitude three thousand feet")),
                ("pilot", ("Runway heading, three thousand" if a.faa else "Straight ahead, three thousand feet")
                 + ", {cs}")])
        elif p.wants == "ground":
            gnd = _freq(a, "GND", "RMP")
            if gnd is None:
                p.clear("ground", now, said=True)
                return
            f = phrase.frequency(gnd.mhz, a.faa)
            self._say(p, now, "ground", "tower", [("ATC", f"{{cs}}, contact ground {f}"), ("pilot", f"Ground {f}, {{cs}}")])
        elif p.wants == "cross":
            self._answer_cross(p, now, others)
        elif p.wants == "taxi_in" and p.paths is not None:
            from atc.own.motion import Path

            rest = [p.mover.path.latlon(x) for x in _every(p.mover.path.length, 10.0) if x >= p.mover.s]
            if len(rest) >= 2 and self._taxi_blocked(p, Path(rest)):
                facing = self._head_on(p, Path(rest))
                if facing is None or facing.blocked_by != p.flight.callsign or not self._route_round(p, facing):
                    return  # waits clear of the runway until the one coming the other way has passed
            st = phrase.spell(p.stand.ref).lower()
            via = f" via {p.paths.via}" if p.paths.via else ""
            out = f" via {p.paths.exit}" if p.paths.exit else ""
            self._say(p, now, "taxi_in", "ground", [
                ("pilot", f"{{station}}, {{cs}}, runway {rw} vacated{out}, request taxi to the stand"),
                ("ATC", f"{{cs}}, taxi to stand {st}{via}"), ("pilot", f"Taxi to stand {st}{via}, {{cs}}")])

    def _answer_cross(self, p: _Pilot, now: float, others: list[Traffic]) -> None:
        """Holding short of a runway on the taxi route: cross it when nobody is on it, about to use it or landing
        on it within CROSS_FINAL_S (either end)."""
        from atc.sequence import final_distance, runway_status, same_strip

        c = p.next_crossing
        if c is None:
            p.clear("cross", now, said=True)
            return
        a = p.airport
        rest = [t for t in others if t.callsign != p.flight.callsign]
        user = self.user or OwnState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, 0.0)
        # the crossing takes its time (SABE: a Cessna, 324 m diagonally between the hold lines, ~70 s): the next
        # arrival must be further out than that plus the margin Tower keeps, else it is sent around
        needs = max(CROSS_FINAL_S, (c.s_clear - c.s_hold) / CROSS_SPEED_MS + 10.0 + CROSS_MARGIN_S)
        for r in (x for x in a.runways if same_strip(x.ident, c.ident)):
            st = runway_status(a, r, user, rest)
            if st.occupied_by:
                return
            for t in rest:
                d = final_distance(a, r, t)
                if d is not None and d / max(60.0, t.gs_kt) * 3600.0 < needs:
                    return
            for q in self.pilots.values():  # ours further out than the final funnel, or joining it
                if isinstance(q, ArrivalPilot) and q.runway.ident == r.ident and q.state in ("final", "joining") \
                        and q.airport.icao == a.icao and final_distance(a, r, q.traffic(a.elevation_ft)) is None \
                        and _final_time_s(q.perf, q.final_nm) < needs:
                    return
        for q in self.pilots.values():  # one of ours cleared onto it, or still rolling on it
            if q is p or not isinstance(q, DeparturePilot) or not same_strip(q.paths.runway.ident, c.ident):
                continue
            if q.state in ("takeoff_radio", "lining_up", "lined_up") or (q.state == "takeoff" and q.pose.on_ground):
                return
        for q in self.pilots.values():  # one of ours already crossing it (cleared, not yet clear on the far side)
            if q is p or q.mover is None:
                continue
            if q.state == "cross_radio" and q.next_crossing is not None and same_strip(q.next_crossing.ident, c.ident):
                return  # cleared a moment ago, still on the radio: it goes first
            if any(x.cleared and same_strip(x.ident, c.ident) and q.mover.s < x.s_clear + 5.0
                   and q.mover.s > x.s_hold - 5.0 for x in q.crossings):
                return
        blocker = p.exit_blocker(c, others)
        if blocker is not None:  # somebody where it would come off the runway: it would stop on it
            q = self.pilots.get(blocker)
            facing = q is not None and q.state in ("hold_short", "cross_radio") and q.next_crossing is not None                 and same_strip(q.next_crossing.ident, c.ident) and q.exit_blocker(q.next_crossing, others) ==                 p.flight.callsign
            if not facing or (q.since, q.flight.callsign) < (p.since, p.flight.callsign):
                return  # wait (asked again next second); nose to nose at both hold lines: the one there first goes
            p.ignore.add(blocker)  # ... and passes the other one off the runway (the last resort, as in _unstick)
            print(f"[own traffic: {p.flight.callsign} crosses past {blocker} (deadlock: both holding short)]")
        rw = phrase.runway(c.ident, a.faa)
        from atc.taxi import crossing_by_tower

        if self.user is not None and crossing_by_tower(a, self.user, self.wind(a)):  # reduced visibility: Tower
            gnd = _freq(a, "GND", "RMP")
            back = f", then contact ground {phrase.frequency(gnd.mhz, a.faa)}" if gnd else ""
            self._say(p, now, "cross", "tower", [("pilot", f"{{station}}, {{cs}}, holding short runway {rw}"),
                                                 ("ATC", f"{{cs}}, cross runway {rw}{back}"),
                                                 ("pilot", f"Crossing runway {rw}{back}, {{cs}}")])
            return
        self._say(p, now, "cross", "ground", [("pilot", f"{{station}}, {{cs}}, holding short runway {rw}"),
                                              ("ATC", f"{{cs}}, cross runway {rw}"),
                                              ("pilot", f"Crossing runway {rw}, {{cs}}")])

    def _atis_words(self, p: _Pilot) -> tuple[str, str, str]:
        """For a pilot's first call: (", information Bravo" it says, ATC's ", confirm information Bravo" if it forgot,
        its ", affirm, information Bravo" in the readback). Most say it; ATIS_FORGET_SHARE forget and get asked."""
        letter = self.atis_letter(p.airport) if self.atis_letter is not None else None
        if not letter:
            return "", "", ""
        if p.rng.random() >= ATIS_FORGET_SHARE:
            return f", information {letter}", "", ""
        from atc.atis import confirm_question

        return "", f", {confirm_question(letter, p.airport.faa)}", f", affirm, information {letter}"

    def _check_cleared_arrival(self, p: ArrivalPilot, now: float, others: list[Traffic]) -> None:
        """Cleared to land, and now somebody is on the runway (the user lined up or backtracking, a sim AI) inside
        GO_AROUND_ORDER_NM: Tower sends it around."""
        if not p.landing_cleared or p.state != "final" or p.final_nm > GO_AROUND_ORDER_NM:
            return
        from atc.sequence import runway_status

        rest = [t for t in others if t.callsign != p.flight.callsign]
        user = self.user or OwnState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, 0.0)
        st = runway_status(p.airport, p.runway, user, rest)
        # ours on the takeoff roll, or cleared for takeoff and still lining up (a backtrack can take minutes, a slow
        # arrival is 2.5 NM out two minutes before it lands): gone in time unless it will not be airborne by then
        tta = p.final_nm / max(60.0, p.pose.gs_kt) * 3600.0
        rolling = {q.flight.callsign for q in self.pilots.values() if isinstance(q, DeparturePilot)
                   and (q.state == "takeoff" or (rem := self._dep_remaining_s(q, now)) is not None and rem < tta)}
        airborne = {t.callsign for t in rest if not t.on_ground}  # (lifted off, below 200 ft over the runway)
        # ours crossing it: off in seconds, before the arrival gets there (to the threshold, then down the runway
        # to where they cross: SABE's GA crossing is 0.87 NM in, half a minute more on the ground roll)
        crossing = {q.flight.callsign for q in self.pilots.values()
                    if (left := self._crossing_left_s(q)) is not None
                    and left + CROSS_MARGIN_S < tta + self._roll_to_s(p, q)}
        blocking = [c for c in st.occupied_by if not (c in st.departing and c in airborne) and c not in crossing
                    and (p.final_nm <= 1.0 or (c not in st.departing and c not in rolling))]
        # a departure rolling away ahead of it is fine until 1 NM, one already airborne and climbing away always
        # (ICAO: landing behind a departure that will have crossed the runway end)
        if not blocking and not st.opposite:
            return
        p.order_go_around(now)
        a = p.airport
        climb = "climb and maintain three thousand" if a.faa else "climb straight ahead to altitude three thousand feet"
        self._radio(p, now, "tower", [("ATC", f"{{cs}}, go around, I say again, go around, traffic on the runway, {climb}"),
                                      ("pilot", "Going around, {cs}")], priority=0)

    def _send_around_uncleared(self, p: ArrivalPilot, now: float, others: list[Traffic]) -> None:
        """Tower couldn't clear it to land (the runway still occupied) and it is close in: Tower sends it around
        (pilot.TOWER_GA_NM) before the pilot has to do it himself (Valen: a pilot rarely goes around on his own)."""
        from atc.sequence import runway_status

        rest = [t for t in others if t.callsign != p.flight.callsign]
        user = self.user or OwnState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, 0.0)
        st = runway_status(p.airport, p.runway, user, rest)
        why = ", traffic on the runway" if st.occupied_by else ""
        circuit = p.circuit
        p.order_go_around(now)
        a = p.airport
        if circuit:
            self._radio(p, now, "tower", [("ATC", f"{{cs}}, go around{why}, report downwind"),
                                          ("pilot", "Going around, wilco, {cs}")], priority=0)
            return
        climb = "climb and maintain three thousand" if a.faa else "climb straight ahead to altitude three thousand feet"
        self._radio(p, now, "tower", [("ATC", f"{{cs}}, go around, I say again, go around{why}, {climb}"),
                                      ("pilot", "Going around, {cs}")], priority=0)

    EMERGENCY_GAP_S = 120.0  # one of ours landing this soon before or after the user's emergency: sent around

    def _clear_for_emergency(self, p: ArrivalPilot, now: float) -> bool:
        """The user's emergency is landing at this airport (own.EMERGENCY): one of ours on the same final that would
        land within EMERGENCY_GAP_S of it is sent around (close in: "go around"; farther out: "discontinue
        approach"). True if it was."""
        em = own.EMERGENCY.get(p.airport.icao)
        u = self.user
        if em is None or u is None or u.on_ground or p.state != "final" or p.approach.on_ground:
            return False
        from atc.sequence import final_distance

        mine = final_distance(p.airport, p.runway, _user_traffic(u))
        d_user = mine if mine is not None else distance_nm(u.lat, u.lon, p.airport.lat, p.airport.lon)
        if d_user > 15.0:
            return False
        t_user = d_user / max(120.0, u.gs_kt) * 3600.0
        t_ours = p.final_nm / max(100.0, p.pose.gs_kt) * 3600.0
        if abs(t_user - t_ours) >= self.EMERGENCY_GAP_S and t_ours < t_user:
            return False  # well ahead: lands and vacates first
        p.order_go_around(now)
        a = p.airport
        climb = "climb and maintain three thousand" if a.faa else "climb straight ahead to altitude three thousand feet"
        what = "go around, I say again, go around" if p.final_nm <= 4.0 else "discontinue approach"
        self._radio(p, now, "tower", [("ATC", f"{{cs}}, {what}, emergency traffic, {climb}"),
                                      ("pilot", ("Going around" if p.final_nm <= 4.0 else "Discontinuing approach")
                                       + ", {cs}")], priority=0)
        return True

    def _approach_words(self, p: ArrivalPilot) -> str:
        try:
            from atc.navdb import approach_type

            kind = approach_type(p.airport, p.runway.ident)
        except Exception:  # noqa: BLE001 - no navdata here: a plain final
            kind = None
        if p.perf.vapp_kt < 100 or not kind:
            return "on final" if p.final_nm < 6 else "inbound for final"
        return f"established {kind}" if kind != "VISUAL" else "on a visual approach"

    def _wind_words(self, a: Airport) -> str | None:
        from atc.runway import magnetic

        wdir, wkt = self.wind(a)
        return phrase.wind(magnetic(a, wdir), wkt, a.faa)

    def _push_blocked(self, p: DeparturePilot, others: list[Traffic]) -> bool:
        """Somebody taxiing near the pushback's path, or standing on the taxiway where the tug will leave it."""
        from atc.own.motion import Path

        path = Path(p.paths.push)
        pts = [path.latlon(s) for s in _every(path.length, 8.0)]
        tail = [path.latlon(s) for s in _every(path.length, 5.0) if s >= path.length - 45.0]
        for t in others:
            if t.callsign == p.flight.callsign or not t.on_ground:
                continue
            if t.gs_kt > 1.0 and min(distance_nm(t.lat, t.lon, *q) for q in pts) * 1852.0 < PUSH_CLEAR_M:
                return True
            if min(distance_nm(t.lat, t.lon, *q) for q in tail) * 1852.0 < PUSH_TAIL_CLEAR_M \
                    and not self._parked(p.airport, t):
                return True  # standing on the taxiway (or about to move) where the tug leaves it
        for q in self.pilots.values():  # one of ours pushing close by (or just approved): one push at a time there
            if q is not p and q.state in ("push_radio", "pushing") and \
                    distance_nm(q.pose.lat, q.pose.lon, p.pose.lat, p.pose.lon) * 1852.0 < TAXI_PUSH_CLEAR_M:
                return True
            # an arrival of ours about to taxi in, or taxiing in, through where the tug leaves this one
            if q is not p and isinstance(q, ArrivalPilot) and q.paths is not None and q.state in (
                    "vacating", "clear_of_runway", "taxi_in_radio", "taxiing_in"):
                ahead = [q.mover.path.latlon(x) for x in _every(q.mover.path.length, 10.0) if x >= q.mover.s]
                if any(min(distance_nm(*a, *b) for b in tail) * 1852.0 < PUSH_TAIL_CLEAR_M for a in ahead):
                    return True
        return False

    def _parked(self, airport: Airport, t: Traffic) -> bool:
        """Parked at a stand: ours by their state (small aprons put stands close to the lanes), others by place."""
        q = self.pilots.get(t.callsign)
        if q is not None:
            return q.state in ("parked", "ask_push", "push_radio", "parked_in")
        return t.gs_kt <= 1.0 and self._at_stand(airport, t)

    def _route_round(self, p: "ArrivalPilot", facing: _Pilot) -> bool:
        """An arrival waiting clear of the runway, nose to nose with a departure stopped for it: a route to its stand
        round that one, if the map has one (then its taxi clearance names the new route)."""
        from atc.own.airport import reroute

        net = self.world.taxi.get(p.airport.icao)
        if net is None:
            return False
        found = reroute(net, p.pose.lat, p.pose.lon, {net.nearest(p.stand.lat, p.stand.lon)},
                        [(facing.pose.lat, facing.pose.lon)], p.pose.heading_deg)
        if found is None:
            return False
        pts, lims, via = found
        p.take_route(pts + [(p.stand.lat, p.stand.lon)], lims + [APRON_KT], p.runway)
        p.paths.via = via or p.paths.via
        return True

    def _unstick(self, p: _Pilot, now: float) -> None:
        """Stopped for the same aircraft for REROUTE_AFTER_S while taxiing: Ground finds a route round it ("change
        of routing, taxi via ..."). None after DEADLOCK_S, and the one in the way is stuck too: the last resort,
        this one passes it (rare: a single-lane apron with two aircraft nose to nose)."""
        from atc.own.airport import reroute
        from atc.taxi import _holding_points, _signed

        if p.state == "pushing" and p.blocked_since is not None and now - p.blocked_since >= PUSH_DEADLOCK_S:
            other = self.pilots.get(p.blocked_by)
            # two tugs pushing into each other (both approved in the same second): the one that started first goes on
            if other is not None and other.state == "pushing" and other.blocked_by == p.flight.callsign \
                    and (p.since, p.flight.callsign) < (other.since, other.flight.callsign):
                p.ignore.add(other.flight.callsign)
                print(f"[own traffic: {p.flight.callsign} pushes on past {other.flight.callsign} (both pushing)]")
            return
        if p.blocked_since is None or p.state not in ("taxiing", "taxiing_in", "vacating"):
            return
        stuck_for = now - p.blocked_since
        other = self.pilots.get(p.blocked_by)
        from atc.sequence import on_runway

        on_it = self._crossing_left_s(p) is not None or (p.state == "vacating" and any(
            on_runway(p.airport, r, p.traffic(p.airport.elevation_ft)) for r in p.airport.runways))
        if on_it and stuck_for >= CROSSING_STUCK_S and other is not None \
                and other.state in ("hold_short", "cross_radio", "holding") + GROUND_WAITING and other.pose.gs_kt < 0.5:
            # stopped ON a runway it is crossing or vacating, behind one waiting at a hold line or holding point
            # there (SARC: one taxiway to the runway, the departure at the holding point blocks the arrival's way
            # off, and waits for the runway it blocks): off the runway first (they pass, as the last resort below)
            p.ignore.add(other.flight.callsign)
            print(f"[own traffic: {p.flight.callsign} passes {other.flight.callsign} (deadlock: off the runway first)]")
            return
        if p.state == "vacating":
            return  # (the re-routing below is for taxiing, off the runway)
        if stuck_for < REROUTE_AFTER_S:
            return
        other = self.pilots.get(p.blocked_by)
        other_stuck = other is not None and (other.pose.gs_kt < 0.5)
        if not other_stuck and p.blocked_by != USER:
            return
        blocker = other.pose if other is not None else self.user
        if blocker is None:
            return
        net = self.world.taxi.get(p.airport.icao)
        if net is not None and not getattr(p, "rerouted", False):
            if isinstance(p, DeparturePilot):
                rwy = p.paths.runway
                goals = _holding_points(net, p.airport, rwy, side=_signed(p.airport, rwy, p.pose.lat, p.pose.lon)[1])
            else:
                goals = {net.nearest(p.stand.lat, p.stand.lon)}
                rwy = p.runway
            found = reroute(net, p.pose.lat, p.pose.lon, goals, [(blocker.lat, blocker.lon)], p.pose.heading_deg)
            if found is not None:
                pts, lims, via = found
                if isinstance(p, ArrivalPilot):
                    pts, lims = pts + [(p.stand.lat, p.stand.lon)], lims + [APRON_KT]
                p.take_route(pts, lims, rwy)
                p.rerouted = True
                if via:
                    self._radio(p, now, "ground", [("ATC", f"{{cs}}, change of routing, taxi via {via}"),
                                                   ("pilot", f"Taxi via {via}, {{cs}}")])
                return
        waiting = other is not None and other.state in GROUND_WAITING  # parked on a taxiway for Ground's word
        if stuck_for >= DEADLOCK_S and other is not None and (other.blocked_by == p.flight.callsign or waiting):
            p.ignore.add(other.flight.callsign)  # nose to nose and nowhere else to go
            print(f"[own traffic: {p.flight.callsign} passes {other.flight.callsign} (deadlock)]")

    def _parked_ids(self, pilots: list[_Pilot], others: list[Traffic]) -> set[str]:
        """Who is parked at a stand: ours by their state, the user and the sim's AI by standing still on one."""
        out = {q.flight.callsign for q in pilots if q.state in ("parked", "ask_push", "push_radio", "parked_in")}
        ours = {q.flight.callsign for q in pilots}
        apts = {q.airport.icao: q.airport for q in pilots}
        for t in others:
            if t.callsign in ours or not t.on_ground or t.gs_kt > 1.0:
                continue
            if any(self._at_stand(a, t) for a in apts.values()):
                out.add(t.callsign)
        return out

    def _at_stand(self, airport: Airport, t: Traffic) -> bool:
        """Parked at a stand (not standing on a taxiway)."""
        net = self.world.taxi.get(airport.icao)
        if net is None:
            return False
        if airport.icao not in self._stand_cache:
            self._stand_cache[airport.icao] = stands(airport.icao, net)
        return any(distance_nm(t.lat, t.lon, st.lat, st.lon) * 1852.0 < 30.0 for st in self._stand_cache[airport.icao])

    def _taxi_blocked(self, p: _Pilot, route=None) -> bool:
        """Ground doesn't send one taxiing while another is pushed back right by it, nor head-on into one of ours
        already taxiing the other way along the same taxiway (it waits until that one has passed)."""
        if any(q is not p and q.state == "pushing" and q.blocked_by != p.flight.callsign and
               distance_nm(q.pose.lat, q.pose.lon, p.pose.lat, p.pose.lon) * 1852.0 < TAXI_PUSH_CLEAR_M
               for q in self.pilots.values()):
            return True
        if route is not None and isinstance(p, DeparturePilot) and self._holding_point_on_exit(p, route):
            return True
        return route is not None and self._head_on(p, route) is not None

    def _holding_point_on_exit(self, p: DeparturePilot, route) -> bool:
        """The holding point this departure taxis to lies on the way off the runway of one of ours landing now (on
        final inside VACATE_AHEAD_NM, rolling out or vacating): one taxiway to the runway (SARC), and a departure
        waiting there blocks the arrival on the runway. Ground keeps it at the apron until that one is off."""
        from atc.own.motion import Path

        end = route.latlon(route.length)
        for q in self.pilots.values():
            if not isinstance(q, ArrivalPilot) or q.airport.icao != p.airport.icao or q.paths is None or not (
                    q.state in ("rollout", "vacating") or (q.state == "final" and q.final_nm < VACATE_AHEAD_NM)):
                continue
            way = Path(q.paths.points)
            off = min(way.length, q.paths.clear_s + 60.0)
            if any(distance_nm(*end, *way.latlon(x)) * 1852.0 < 40.0 for x in _every(off, 10.0)):
                return True
        return False

    def _head_on(self, p: _Pilot, route) -> _Pilot | None:
        """`route` (a motion.Path, from where p is) meets the rest of a taxiing aircraft's path going the other way
        (for an arrival: also the route of a departure that has pushed back and is about to taxi out)."""
        from atc.geo import heading_diff
        from atc.own.motion import Path

        mine = [(route.latlon(x), route.heading(x)) for x in _every(route.length, 10.0)]
        for q in self.pilots.values():
            if q is p:
                continue
            if q.mover is not None and q.state in ("taxiing", "taxiing_in", "vacating"):
                path, start = q.mover.path, q.mover.s
            elif isinstance(p, ArrivalPilot) and isinstance(q, DeparturePilot) and \
                    q.state in ("starting", "ask_taxi", "taxi_radio"):
                path, start = Path(q.paths.taxi), 0.0  # pushed back, about to taxi out: it goes first
            else:
                continue
            theirs = [(path.latlon(x), path.heading(x)) for x in _every(path.length, 10.0) if x >= start - 20.0]
            for (a, ha) in mine:
                for (b, hb) in theirs:
                    if heading_diff(ha, hb) > HEAD_ON_DEG and distance_nm(*a, *b) * 1852.0 < HEAD_ON_M:
                        return q
        return None

    def _runway_blocked(self, p: DeparturePilot, others: list[Traffic]) -> bool:
        """Somebody on the runway (either end) or landing the other way, an arrival too close to be gone before it
        lands, or our previous departure still close."""
        from atc.sequence import runway_status

        rwy = p.paths.runway
        rest = [t for t in others if t.callsign != p.flight.callsign]
        user = self.user or OwnState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, 0.0)
        if p.airport.icao in own.EMERGENCY and self.user is not None and not self.user.on_ground:
            return True  # the user's emergency lands here: departures wait at the holding point
        st = runway_status(p.airport, rwy, user, rest)
        if st.occupied_by or st.opposite:  # on it, or landing the other way (the sim's AI can)
            return True
        from atc import departures

        if self._queue_ahead(p):
            return True
        if departures.gap_left(p.airport.icao, rwy.ident, p.flight.type_icao) > 0:  # wake turbulence interval
            return True
        needs_s = self._departure_time_s(p)
        # lined up already: "cleared for immediate takeoff" with a smaller margin (the line-up was given with the
        # full one plus LUAW_BUFFER_S: what is left of it covers one ahead slower off the runway than planned)
        margin = (LINED_MARGIN_S if p.waiting_lined else DEPARTURE_MARGIN_S) + \
            (LVP_DEPARTURE_EXTRA_S if self._lvp(p.airport) else 0.0)
        # (lined up already: only who is on the final holds it; a circuit or a join is told to extend instead, else
        # the two wait for each other: SARC soak, a lined-up departure and a Cessna in the circuit, for hours)
        for _, tta in self._arrival_times(p, rest, finals_only=p.waiting_lined):  # time to the threshold vs needed
            if tta < needs_s + margin:
                return True
        for q in self.pilots.values():
            if q is p:
                continue
            if isinstance(q, DeparturePilot) and q.state in ("takeoff_radio", "lining_up", "lined_up", "takeoff") and \
                    (q.pose.on_ground or distance_nm(q.pose.lat, q.pose.lon, p.pose.lat, p.pose.lon) < SEPARATION_NM):
                return True
            if isinstance(q, ArrivalPilot) and q.runway.ident == rwy.ident and q.state == "rollout":
                return True
        return False

    def _arrival_times(self, p: DeparturePilot, rest: list[Traffic],
                       finals_only: bool = False) -> list[tuple[str, float]]:
        """(callsign, seconds to the threshold) of everybody about to land on this one's runway: on the final (the
        sim's AI, the user, ours), and ours still joining it from a downwind or a base, or further out on the final
        than the funnel looks (a backtrack at SARC takes five minutes: one 14 NM out was missed and went around)."""
        from atc.sequence import final_distance, threshold

        rwy = p.paths.runway
        out = []
        for t in rest:
            d = final_distance(p.airport, rwy, t)
            if d is not None:
                out.append((t.callsign, d / max(60.0, t.gs_kt) * 3600.0))
        on_final = {c for c, _ in out}
        for q in self.pilots.values():
            if isinstance(q, ArrivalPilot) and q.airport.icao == p.airport.icao and q.runway.ident == rwy.ident \
                    and q.state in (("final",) if finals_only else ("final", "joining", "touch_go")) \
                    and q.flight.callsign not in on_final:
                tta = _final_time_s(q.perf, q.final_nm)  # (or on a long final, 15 NM)
                if q.state != "final":  # a join's way left is rough: never later than straight in at its speed
                    lat, lon = threshold(p.airport, rwy)
                    straight = distance_nm(q.pose.lat, q.pose.lon, lat, lon) / max(60.0, q.pose.gs_kt) * 3600.0
                    tta = min(tta, straight)
                out.append((q.flight.callsign, tta))
            elif isinstance(q, DeparturePilot) and q is not p and q.circuits > 0 and q.airport.icao == p.airport.icao \
                    and q.state in ("takeoff", "circuit_ready") and not q.pose.on_ground and not finals_only:
                # a circuit flight just airborne: back on the final in about a lap (Tower knows it stays)
                out.append((q.flight.callsign, max(60.0, CIRCUIT_LAP_S - (self._now() - q.since))))
        return out

    def _queue_ahead(self, p: DeparturePilot) -> list[str]:
        """Who is before this one in the departure queue (the user only while really there, waiting)."""
        from atc import departures
        from atc.sequence import on_runway

        rwy = p.paths.runway
        before = departures.ahead(p.airport.icao, p.flight.callsign, rwy.ident)  # the user (or one of ours) was first
        if departures.USER in before and self.user is not None:
            u = self.user
            near = distance_nm(u.lat, u.lon, p.pose.lat, p.pose.lon) * 1852.0 < 600.0
            if not u.on_ground or not (near or on_runway(p.airport, rwy, _user_traffic(u))):
                before = [c for c in before if c != departures.USER]
        return before

    def _lineup_wait(self, p: DeparturePilot, others: list[Traffic]) -> tuple[str, str, str | None] | None:
        """Tower's "line up and wait" for one of ours at the holding point while the runway is still busy:
        (ATC words, readback, the landing aircraft it lines up behind or None), or None to keep it at the holding
        point. Behind a departure rolling away or during a wake interval: plain "line up and wait runway one three".
        ICAO only, behind a landing aircraft on short final or rolling out beyond where this one lines up: the
        conditional "behind the landing Boeing seven thirty-seven on two mile final, line up and wait runway one
        three, behind" (sequence.wait_for_takeoff's form). Never when the next arrival would find it still there."""
        from atc import departures
        from atc.geo import heading_diff
        from atc.sequence import SHORT_FINAL_NM, _miles, along_cross, runway_status

        a, rwy = p.airport, p.paths.runway
        if a.icao in own.EMERGENCY or self._lvp(a) or self._queue_ahead(p):
            return None
        if any(q is not p and isinstance(q, DeparturePilot) and q.airport.icao == a.icao
               and q.state in ("takeoff_radio", "lining_up", "lined_up") for q in self.pilots.values()):
            return None
        rest = {t.callsign: t for t in others if t.callsign != p.flight.callsign}
        user = self.user or OwnState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, 0.0)
        st = runway_status(a, rwy, user, list(rest.values()))
        if st.opposite:
            return None
        entry = p.entry_along_nm()
        now = self._now()
        behind, busy_s = None, 0.0  # busy_s: until the runway is free for this one's roll
        pass_s = 0.0  # a conditional one: until the landing one has gone by where this one lines up
        for cs in st.occupied_by:
            t = rest.get(cs)
            if t is None or cs == USER:
                return None
            moving_away = along_cross(a, rwy, t.lat, t.lon)[0] > entry + 0.1 and t.gs_kt > 5.0 \
                and heading_diff(t.heading_deg, rwy.heading_deg) < 30.0
            if not moving_away:
                return None
            if cs in st.departing:  # rolling away: gone (and far enough ahead) in about a minute
                busy_s = max(busy_s, LUAW_DEPARTING_S)
                continue
            if a.faa or behind is not None:  # landing, rolling out: a conditional clearance (not FAA)
                return None
            behind = cs
            q = self.pilots.get(cs)
            left = RUNWAY_VACATE_S - (now - q.since) if isinstance(q, ArrivalPilot) and q.state == "rollout" \
                else RUNWAY_VACATE_S
            busy_s = max(busy_s, left, 15.0)
        short = [(c, d) for c, d in st.finals if d <= SHORT_FINAL_NM]
        if short:
            cs, nm = short[0]
            if a.faa or st.occupied_by or cs == USER or cs not in rest:
                return None
            behind = cs
            tta = nm / max(60.0, rest[cs].gs_kt) * 3600.0
            busy_s = tta + RUNWAY_VACATE_S
            pass_s = tta + max(0.0, entry) / 100.0 * 3600.0  # (on its landing roll, ~100 kt)
        for q in self.pilots.values():  # our previous departure not far enough ahead yet
            if isinstance(q, DeparturePilot) and q is not p and q.state == "takeoff" and not q.pose.on_ground:
                d = distance_nm(q.pose.lat, q.pose.lon, p.pose.lat, p.pose.lon)
                if d < SEPARATION_NM:
                    busy_s = max(busy_s, (SEPARATION_NM - d) / max(120.0, q.pose.gs_kt) * 3600.0)
        busy_s = max(busy_s, departures.gap_left(a.icao, rwy.ident, p.flight.type_icao))
        # ready to roll once the runway is free and it is on the centerline (the line-up itself takes a while)
        # (behind a landing one the line-up starts once it has gone by: SARC's 1 km backtrack took 3.5 min after
        # that, and the next arrival found it still lined up)
        ready_s = max(busy_s, max(self._radio_s(), pass_s) + self._lineup_s(p))
        needs = self._departure_time_s(p, lined=True) + DEPARTURE_MARGIN_S + LUAW_BUFFER_S
        for cs, tta in self._arrival_times(p, list(rest.values())):  # the next one must not find it still there
            if cs != behind and tta < ready_s + needs:
                return None
        if any(isinstance(q, ArrivalPilot) and q.landing_cleared and q.flight.callsign != behind
               and q.airport.icao == a.icao and q.state in ("final", "joining") for q in self.pilots.values()):
            return None  # nobody lined up in front of one already cleared to land (a circuit's is given on base)
        rw = phrase.runway(rwy.ident, a.faa)
        if behind is None:
            if a.faa:
                return f"runway {rw}, line up and wait", f"Runway {rw}, line up and wait", None
            return f"line up and wait runway {rw}", f"Line up and wait runway {rw}", None
        typ = st.types.get(behind)
        if not typ:
            return None  # a conditional names the aircraft
        where = f" on {_miles(short[0][1])} final" if short else ""
        return (f"behind the landing {typ}{where}, line up and wait runway {rw}, behind",
                f"Behind the landing {typ}, line up and wait runway {rw}, behind", behind)

    def _lvp(self, airport: Airport) -> bool:
        """Low-visibility procedures at `airport` now (the METAR / the sim's visibility near it)."""
        from atc import atis

        u = self.user or OwnState(airport.lat, airport.lon, airport.elevation_ft, 0.0, 0.0, 0.0, True, 0.0)
        try:
            return atis.low_visibility(airport, u, self.wind(airport) if self.wind(airport)[1] is not None else None)
        except Exception:  # noqa: BLE001 - weather not known: normal spacing
            return False

    def _departure_time_s(self, p: DeparturePilot, lined: bool | None = None) -> float:
        """From the holding point until it is airborne: line up (~5 m/s), spool up, the takeoff roll. `lined`: from
        the centerline (lined up under "line up and wait"; default: where it is now)."""
        from atc.own.motion import KT

        lined = p.waiting_lined if lined is None else lined
        lineup = 0.0 if lined else self._lineup_s(p)
        climb_200ft = 200.0 / (p.perf.climb_fpm / 60.0)  # until then it still counts as on the runway
        radio = self._radio_s()  # said, read back, brakes off
        spool = 3.0  # the thrust builds up (motion.SPOOL_S)
        pause = 0.0 if lined else LINEUP_PAUSE_S[1]  # (lined up: "immediate" when it is tight, no pause)
        return radio + lineup + pause + p.perf.vr_kt * KT / p.perf.accel + 6.0 + spool + climb_200ft

    @staticmethod
    def _roll_to_s(p: ArrivalPilot, q: _Pilot) -> float:
        """Seconds from the threshold to where `q` is on p's runway, on p's landing roll (~100 kt on average over
        the first mile, slower after; nothing beyond 1.5 NM is counted)."""
        from atc.sequence import along_cross

        along = along_cross(p.airport, p.runway, q.pose.lat, q.pose.lon)[0]
        return max(0.0, min(along, 1.5)) / 100.0 * 3600.0

    @staticmethod
    def _crossing_left_s(q: _Pilot) -> float | None:
        """Seconds until one of ours crossing a runway (cleared, between the hold line and clear of the far side) has
        its tail off it, or None if it isn't crossing one."""
        if q.mover is None or q.state not in ("taxiing", "taxiing_in"):
            return None
        for c in q.crossings:
            if c.cleared and c.s_hold - 1.0 <= q.mover.s <= c.s_clear:
                return (c.s_clear - q.mover.s) / max(3.0, q.mover.v)
        return None

    def _dep_remaining_s(self, q: DeparturePilot, now: float, luaw_too: bool = False) -> float | None:
        """Seconds until one of ours cleared for takeoff is airborne (lining up, a backtrack, the pause on the
        centerline, the roll), or None if it holds no takeoff clearance (at the holding point, lined up waiting).
        `luaw_too`: under "line up and wait" as well, as if cleared once lined up (a new arrival's spacing)."""
        from atc.own.motion import KT

        roll = q.perf.vr_kt * KT / q.perf.accel + 6.0 + 3.0 + 200.0 / (q.perf.climb_fpm / 60.0)
        if luaw_too and (q.waiting_lined or (q.luaw and q.state in ("takeoff_radio", "lining_up"))):
            left = max(0.0, q.mover.path.length - q.mover.s) / 5.0 if q.state == "lining_up" and q.mover else \
                0.0 if q.waiting_lined else self._lineup_s(q)
            return left + self._radio_s() + LINEUP_PAUSE_S[1] + roll + LUAW_WAIT_S
        if q.state == "takeoff_radio" and not q.luaw:
            return self._departure_time_s(q) - (now - q.since)
        if q.state == "lining_up" and not q.luaw:
            left = max(0.0, q.mover.path.length - q.mover.s) / 5.0
            return left + (0.0 if q.rolling else LINEUP_PAUSE_S[1]) + roll
        if q.state == "lined_up" and not q.waiting_lined:
            return max(0.0, (q.go_at or now) - now) + roll
        if q.state == "takeoff" and q.pose.on_ground:
            return roll / 2.0
        return None

    def _radio_s(self) -> float:
        """A Tower exchange said and read back, then the pilot reacts."""
        return (12.0 if self.bus is not None else 0.0) + REACT_S["takeoff"][1]

    @staticmethod
    def _lineup_s(p: DeparturePilot) -> float:
        """From the holding point onto the centerline (~5 m/s)."""
        from atc.own.motion import Path

        return Path(p.paths.lineup).length / 5.0 + 6.0

    def _landing_blocked(self, p: ArrivalPilot, others: list[Traffic]) -> str | None:
        """Why this arrival may not be cleared to land now ("traffic on the runway", "number two"), or None."""
        from atc.sequence import final_distance, runway_status

        rest = [t for t in others if t.callsign != p.flight.callsign]
        user = self.user or OwnState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, 0.0)
        st = runway_status(p.airport, p.runway, user, rest)
        airborne = {t.callsign for t in rest if not t.on_ground}
        # (a departure already lifted off, climbing away below 200 ft, is no reason to wait: ICAO anticipated
        # separation; waiting for it sent an arrival around at 1 NM in the SABE soak)
        if [c for c in st.occupied_by if not (c in st.departing and c in airborne)]:
            return "traffic on the runway"
        if st.opposite:
            return st.landing_blocked()
        # (a conditional line-up behind this very arrival waits for it to pass first: not in its way; any other one
        # lining up, "line up and wait" too, will be on the runway: SARC, a Cessna cleared to land met a backtrack)
        if any(isinstance(q, DeparturePilot) and q.state in ("takeoff_radio", "lining_up")
               and q.behind != p.flight.callsign and q.airport.icao == p.airport.icao for q in self.pilots.values()):
            return "traffic departing"
        ahead = [t for t in rest if (d := final_distance(p.airport, p.runway, t)) is not None and d < p.final_nm]
        if ahead:
            return f"number {phrase.digits(str(len(ahead) + 1))}"
        return None

    def _say(self, p: _Pilot, now: float, what: str, role: str, lines: list[tuple[str, str]],
             priority: int = 1) -> None:
        """Grant `what` and put the exchange on the radio (the pilot moves once it has been heard)."""
        if self.bus is None or self._station(p.airport, role) is None:
            p.clear(what, now, said=True)
            return
        p.clear(what, now)
        self._radio(p, now, role, lines, priority, on_said=lambda: p.heard(self._now()))

    def _station(self, a: Airport, role: str):
        f = _freq(a, *(("GND", "RMP") if role == "ground" else ("TWR",)))
        if f is None and role == "ground":  # no Ground on file: Tower does it
            f, role = _freq(a, "TWR"), "tower"
        if f is None:
            return None
        fac = resolve_facility(a, f.mhz)
        return (callsign_for(a, fac) if fac is not None else a.spoken_name or a.name), role

    def _radio(self, p: _Pilot, now: float, role: str, lines: list[tuple[str, str]], priority: int = 1,
               on_said=None) -> None:
        if self.bus is None:
            return
        found = self._station(p.airport, role)
        if found is None:
            return
        station, role = found
        from atc.chatter import Exchange, ai_callsign, voice_for
        from atc.voices import country_of

        a = p.airport
        cs = ai_callsign(p.traffic(a.elevation_ft), a.faa)
        cap = cs[:1].upper() + cs[1:]
        voice = voice_for(p.flight.callsign)
        out = []
        for who, text in lines:
            t = text.format(cs=cs, station=station)
            t = t[:1].upper() + t[1:]
            out.append(("ATC", t + ".", None) if who == "ATC" else (cap, t + ".", voice))
        self.bus.offer(Exchange(p.flight.callsign, out, time.monotonic(), role, priority,
                                country_of(None, p.flight.callsign), on_said=on_said))

    # --- threads -------------------------------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._read_loop, daemon=True, name="own-traffic-read").start()
        threading.Thread(target=self._move_loop, daemon=True, name="own-traffic-move").start()

    def _read_loop(self) -> None:
        """The user's aircraft and the sim's traffic: these reads can block, so never in the move loop."""
        while not self.stop.wait(READ_EVERY_S):
            try:
                user = self.sim.own()
                apt = self.world.nearest(user)
                traffic = self.sim.traffic(apt.lat, apt.lon, 15.0) if apt is not None else []
                self.user = user
                self.sim_traffic = [t for t in traffic if not own.is_own(t.callsign)]
            except Exception as exc:  # noqa: BLE001
                print(f"[own traffic read: {exc}]")

    def _move_loop(self) -> None:
        while not self.stop.is_set():
            self.injector.wait(0.05)  # the next sim frame (SimInjector), or 50 ms
            try:
                self.step(time.monotonic())
            except Exception as exc:  # noqa: BLE001 - never kill the radio over a traffic bug
                print(f"[own traffic: {exc}]")

    def close(self) -> None:
        self.stop.set()
        try:
            self.injector.close()
        except Exception:  # noqa: BLE001
            pass
        own.publish({})
