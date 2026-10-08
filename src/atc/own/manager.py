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
from atc.own.motion import perf
from atc.own.pilot import LINEUP_PAUSE_S, ArrivalPilot, DeparturePilot, Flight, _Pilot

USER = "(you)"
SEPARATION_NM = 1.5  # the previous departure this far out (and airborne) before the next takeoff clearance
ARRIVAL_SPAWN_NM = 12.0
ARRIVAL_SPACING_NM = 6.0
ARRIVAL_SPACING_DEP_NM = 8.0  # with departures waiting: a gap for one between two arrivals
DEPARTURE_MARGIN_S = 25.0  # the departure is off the runway this long before the arrival reaches the threshold
MAX_DEPARTURE_QUEUE = 3  # no new departure while this many are taxiing out / holding
CROSS_FINAL_S = 90.0  # no runway crossing with somebody this close to landing on it
ATIS_FORGET_SHARE = 0.1  # pilots whose first call has no ATIS letter (ATC asks them to confirm it)
BUBBLE_NM = 25.0  # new traffic only at an airport the user is this close to
ATC_EVERY_S = 1.0
READ_EVERY_S = 0.5


PUSH_CLEAR_M = 100.0  # nobody taxiing this close to a pushback's path when it is approved
NOSE_GEAR_SHARE = 0.38  # the nose gear sits this share of the length ahead of the reference point
TUG_AHEAD_M = 2.0
TUG_YAW = float(os.environ.get("ATC_OWN_TUG_YAW", "180"))  # VERIFY: the tug faces the aircraft
TUG_STAYS_S = 12.0


def _every(length: float, step: float):
    s = 0.0
    while s < length:
        yield s
        s += step
    yield length


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
        self.user: OwnState | None = None
        self.sim_traffic: list[Traffic] = []
        self.tugs: dict[str, dict] = {}
        self.atis_letter = atis_letter  # airport -> the current ATIS letter word ("Bravo"), or None (no ATIS)

    # --- spawning ------------------------------------------------------------------------------------------
    def _now(self) -> float:
        return self.clock if self.clock is not None else time.monotonic()

    def _departure_queue(self) -> int:
        """Departures taxiing out or waiting at the holding point."""
        return sum(1 for p in self.pilots.values() if isinstance(p, DeparturePilot)
                   and p.state in ("ask_taxi", "taxi_radio", "taxiing", "holding"))

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
        return Flight(dep.callsign, title or "", dep.type_icao, dep.airline or None), title

    def spawn_departure(self, airport: Airport, now: float | None = None, dep: schedule.Departure | None = None,
                        ga: bool = False) -> str:
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
        with self.lock:
            self.pilots[flight.callsign] = DeparturePilot(flight, airport, paths, p, now,
                                                          push_after_s=self.rng.uniform(20.0, 50.0))
        self.injector.create(flight.callsign, flight.title, stand.lat, stand.lon, stand.heading, airport.elevation_ft,
                             type_icao=flight.type_icao)
        return self._said(f"{flight.callsign} {flight.type_icao} at stand {stand.ref} -> runway {rwy.ident}"
                          + (f" via {paths.via}" if paths.via else "") + (f" ({title})" if title else ""))

    def spawn_arrival(self, airport: Airport, now: float | None = None, dep: schedule.Departure | None = None,
                      ga: bool = False) -> str:
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
        dist = ARRIVAL_SPAWN_NM if p.vapp_kt >= 100 else 5.0
        spacing = ARRIVAL_SPACING_DEP_NM if self._departure_queue() else ARRIVAL_SPACING_NM
        for t in self._taken(airport):  # behind everybody already on this final
            d = final_distance(airport, rwy, t) if not t.on_ground else None
            if d is not None:
                dist = max(dist, d + spacing)
        for q in self.pilots.values():
            if isinstance(q, ArrivalPilot) and q.state == "final" and q.runway.ident == rwy.ident:
                dist = max(dist, q.final_nm + spacing)
        if dist > ARRIVAL_SPAWN_NM + 8.0:
            return "final too busy"
        net = self.world.taxi.get(airport.icao)
        all_stands = stands(airport.icao, net) if net is not None else []
        stand = free_stand(all_stands, self._taken(airport), p.wingspan_m, airport, self.rng, self._reserved(),
                           ga=p.vapp_kt < 100) if all_stands else None
        if stand is None:
            return "no free stand"
        with self.lock:
            self.pilots[flight.callsign] = ArrivalPilot(
                flight, airport, rwy, threshold(airport, rwy), stand, p, now, net, dist_nm=dist,
                parked_for_s=self.rng.uniform(240.0, 480.0))
        pose = self.pilots[flight.callsign].pose
        self.injector.create(flight.callsign, flight.title, pose.lat, pose.lon, rwy.heading_deg,
                             airport.elevation_ft, agl_ft=pose.agl_ft, type_icao=flight.type_icao)
        return self._said(f"{flight.callsign} {flight.type_icao} on a {dist:.0f} NM final runway {rwy.ident} -> "
                          f"stand {stand.ref}" + (f" ({title})" if title else ""))

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
        for p in pilots:  # who is already stopped for whom: the one waited for goes first (no deadlock)
            p.yielding = {q.flight.callsign for q in pilots if q.blocked_by == p.flight.callsign}
        for p in pilots:
            p.step(t, dt, others)
        if t - self.last_atc >= ATC_EVERY_S:
            self.last_atc = t
            for p in pilots:
                if p.wants:
                    self._answer(p, t, others)
        own.publish({p.flight.callsign: p.traffic(p.airport.elevation_ft) for p in pilots if not p.done})
        for p in pilots:
            if p.done:
                self.injector.remove(p.flight.callsign)
                with self.lock:
                    self.pilots.pop(p.flight.callsign, None)
                print(f"[own traffic: {p.flight.callsign} gone]")
                continue
            self.injector.update(p.flight.callsign, p.pose)
            self._lights(p)
            if isinstance(p, DeparturePilot):
                self._tug(p, t)
        self.injector.pump()
        if self.auto:
            self._auto_spawn(t)

    def _tug(self, p: DeparturePilot, t: float) -> None:
        """A pushback tug under the nose while it is pushed (GSX's TPX if installed), gone TUG_STAYS_S after."""
        from atc.own.catalog import tug_title
        from atc.own.motion import Pose, moved

        key = f"{p.flight.callsign}~TUG"
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
            self.injector.lights(p.flight.callsign, nav=True, beacon=moving, engines=moving and s != "pushing",
                                 taxi=s in ("taxiing", "takeoff_radio", "lining_up"),
                                 landing=s in ("lining_up", "takeoff") and p.pose.agl_ft < 10000,
                                 strobe=s in ("lining_up", "takeoff"))
        else:
            airborne = s in ("final", "go_around")
            self.injector.lights(p.flight.callsign, nav=True, beacon=s != "parked_in", engines=s != "parked_in",
                                 landing=airborne or s == "rollout", strobe=airborne or s == "rollout",
                                 taxi=s in ("vacating", "taxiing_in"))

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
        if now < self.next_spawn or len(self.pilots) >= self._max_alive(rate) or rate <= 0:
            return
        self.next_spawn = now + self.rng.expovariate(rate / 3600.0)
        scheduled = schedule._todays_cached(apt.icao, self.community)
        ga = not scheduled or self.rng.random() < schedule.GA_SHARE
        queue_full = self._departure_queue() >= MAX_DEPARTURE_QUEUE
        if queue_full or self.rng.random() < 0.5:
            msg = self.spawn_arrival(apt, now, ga=ga)
            if msg in ("final too busy", "no runway") and not queue_full:
                self.spawn_departure(apt, now, ga=ga)
        else:
            msg = self.spawn_departure(apt, now, ga=ga)
            if msg == "no free stand":
                self.spawn_arrival(apt, now, ga=ga)

    # --- the controller ------------------------------------------------------------------------------------
    def _answer(self, p: _Pilot, now: float, others: list[Traffic]) -> None:
        a = p.airport
        if isinstance(p, ArrivalPilot):
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
            rw = phrase.runway(p.paths.runway.ident, a.faa)
            via = f" via {p.paths.via}" if p.paths.via else ""
            clr = f"runway {rw}, taxi{via}" if a.faa else f"taxi to holding point runway {rw}{via}"
            self._say(p, now, "taxi", "ground", [("pilot", "{cs}, request taxi"), ("ATC", "{cs}, " + clr),
                                                 ("pilot", clr[:1].upper() + clr[1:] + ", {cs}")])
        elif p.wants == "takeoff":
            if self._runway_blocked(p, others):
                return  # holds at the holding point; asked again next second
            rw = phrase.runway(p.paths.runway.ident, a.faa)
            wind = self._wind_words(a)
            self._say(p, now, "takeoff", "tower", [
                ("pilot", f"{{station}}, {{cs}}, holding point runway {rw}, ready for departure" if not a.faa
                 else f"{{station}}, {{cs}}, holding short runway {rw}, ready for departure"),
                ("ATC", "{cs}, {station}, " + (f"{wind}, " if wind else "") + f"runway {rw}, cleared for takeoff"),
                ("pilot", f"Cleared for takeoff runway {rw}, {{cs}}")], priority=0)
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

    def _answer_arrival(self, p: ArrivalPilot, now: float, others: list[Traffic]) -> None:
        a = p.airport
        rw = phrase.runway(p.runway.ident, a.faa)
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
        for r in (x for x in a.runways if same_strip(x.ident, c.ident)):
            st = runway_status(a, r, user, rest)
            if st.occupied_by:
                return
            for t in rest:
                d = final_distance(a, r, t)
                if d is not None and d / max(60.0, t.gs_kt) * 3600.0 < CROSS_FINAL_S:
                    return
        for q in self.pilots.values():  # one of ours cleared onto it, or still rolling on it
            if q is p or not isinstance(q, DeparturePilot) or not same_strip(q.paths.runway.ident, c.ident):
                continue
            if q.state in ("takeoff_radio", "lining_up", "lined_up") or (q.state == "takeoff" and q.pose.on_ground):
                return
        rw = phrase.runway(c.ident, a.faa)
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
            if min(distance_nm(t.lat, t.lon, *q) for q in tail) * 1852.0 < 35.0:
                return True
        return False

    def _runway_blocked(self, p: DeparturePilot, others: list[Traffic]) -> bool:
        """Somebody on the runway (either end) or landing the other way, an arrival too close to be gone before it
        lands, or our previous departure still close."""
        from atc.sequence import final_distance, runway_status

        rwy = p.paths.runway
        rest = [t for t in others if t.callsign != p.flight.callsign]
        user = self.user or OwnState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, 0.0)
        st = runway_status(p.airport, rwy, user, rest)
        if st.occupied_by or st.opposite:  # on it, or landing the other way (the sim's AI can)
            return True
        needs_s = self._departure_time_s(p)
        for t in rest:  # anybody on final: time to the threshold against the time this one needs to be gone
            d = final_distance(p.airport, rwy, t)
            if d is not None and d / max(60.0, t.gs_kt) * 3600.0 < needs_s + DEPARTURE_MARGIN_S:
                return True
        for q in self.pilots.values():
            if q is p:
                continue
            if isinstance(q, DeparturePilot) and q.state in ("takeoff_radio", "lining_up", "takeoff") and \
                    (q.pose.on_ground or distance_nm(q.pose.lat, q.pose.lon, p.pose.lat, p.pose.lon) < SEPARATION_NM):
                return True
            if isinstance(q, ArrivalPilot) and q.runway.ident == rwy.ident and q.state == "rollout":
                return True
        return False

    @staticmethod
    def _departure_time_s(p: DeparturePilot) -> float:
        """From the holding point until it is airborne: line up (~5 m/s), spool up, the takeoff roll."""
        from atc.own.motion import KT, Path

        lineup = Path(p.paths.lineup).length / 5.0 + 6.0
        return lineup + LINEUP_PAUSE_S[1] + p.perf.vr_kt * KT / p.perf.accel + 6.0

    def _landing_blocked(self, p: ArrivalPilot, others: list[Traffic]) -> str | None:
        """Why this arrival may not be cleared to land now ("traffic on the runway", "number two"), or None."""
        from atc.sequence import final_distance, runway_status

        rest = [t for t in others if t.callsign != p.flight.callsign]
        user = self.user or OwnState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, 0.0)
        st = runway_status(p.airport, p.runway, user, rest)
        if st.occupied_by:
            return "traffic on the runway"
        if st.opposite:
            return st.landing_blocked()
        if any(isinstance(q, DeparturePilot) and q.state in ("takeoff_radio", "lining_up") for q in self.pilots.values()):
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
