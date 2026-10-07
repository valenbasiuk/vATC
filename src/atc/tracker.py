"""What each AI aircraft is doing, from its telemetry only (position, speed, on-ground), so ATC can talk to it.

MSFS's AI traffic follows the sim's own internal ATC; we can't command it. Instead the tracker turns its movement
into events at the moment they happen ("taxi_out", "lineup", "takeoff_roll", "departed", "final", "vacated",
"taxi_in", and for the radar positions "dep_checkin", "approach", "app_handoff") and chatter.py says the matching
instruction on the frequency, so the radio sounds like the traffic is being controlled. A new aircraft (first time seen) starts in the phase it is in, without an event: no burst of
chatter at startup.
"""

from __future__ import annotations

from dataclasses import dataclass

from atc.geo import bearing_deg, distance_nm, heading_diff
from atc.models import Airport, Runway, Traffic
from atc.sequence import along_cross, final_distance, on_runway

TAXI_KT = 3.0  # moving on the ground
ROLL_KT = 30.0  # takeoff roll / landing rollout
FINAL_EVENT_NM = 6.0  # "cleared to land" this far out
DEPARTED_AGL_FT = 600.0
DEP_CHECKIN_AGL_FT = 2000.0  # a departure checks in with Departure climbing through this
APPROACH_NM = (7.0, 15.0)  # an arrival this far out (not yet on final) gets its approach clearance
HANDOFF_FINAL_NM = 10.0  # established on final inside this: Approach hands it to Tower
STOPPED_S = 3.0


@dataclass
class Track:
    callsign: str
    phase: str  # parked, pushback, taxi_out, holding, lineup, takeoff, departed, arrival, final, rollout, vacated,
    # taxi_in
    since: float
    last: Traffic
    runway: str | None = None
    stopped_at: float | None = None
    cleared: bool = False  # landing clearance said (set by the caller); until then "final" is offered again
    last_offer: float = 0.0
    # radar chatter, once each (set True for an aircraft already past that point when first seen)
    dep_checked: bool = False
    approach_called: bool = False
    handoff_called: bool = False


@dataclass
class Event:
    kind: str  # pushback, taxi_out, lineup, takeoff_roll, departed, final, vacated, taxi_in, dep_checkin,
    # approach, app_handoff
    traffic: Traffic
    airport: Airport
    runway: Runway | None
    at: float
    repeat: bool = False  # "final" again for an aircraft still waiting for its landing clearance

RECHECK_S = 5.0  # an aircraft on final without landing clearance is looked at again this often
LINEUP_HELD_S = 300.0
AI_FLOW_WINDOW_S = 1200.0  # the AI's runway in use: from their movements in the last 20 minutes


def _aligned_runway(airport: Airport, t: Traffic) -> Runway | None:
    """The runway the aircraft is on and pointing along (either direction counts as on it)."""
    for r in airport.runways:
        if r.heading_deg is not None and on_runway(airport, r, t) and heading_diff(t.heading_deg, r.heading_deg) < 30:
            return r
    return None


def _on_any_runway(airport: Airport, t: Traffic) -> bool:
    return any(on_runway(airport, r, t) for r in airport.runways)


def _final_runway(airport: Airport, t: Traffic) -> tuple[Runway, float] | None:
    best = None
    for r in airport.runways:
        d = final_distance(airport, r, t)
        if d is not None and (best is None or d < best[1]):
            best = (r, d)
    return best


def _near_holding_point(airport: Airport, t: Traffic) -> bool:
    for r in airport.runways:
        along, cross = along_cross(airport, r, t.lat, t.lon)
        if -0.3 <= along <= 0.4 and 0.03 < cross <= 0.15:
            return True
    return False


def _backwards(prev: Traffic, now: Traffic) -> bool:
    """Moved tail first since the last look (a pushback), judged from the positions (> 0.5 m apart)."""
    if distance_nm(prev.lat, prev.lon, now.lat, now.lon) * 1852 < 0.5:
        return False
    return heading_diff(bearing_deg(prev.lat, prev.lon, now.lat, now.lon), now.heading_deg) > 120


class TrafficTracker:
    def __init__(self, airport: Airport) -> None:
        self.airport = airport
        self.tracks: dict[str, Track] = {}
        self.started = False  # after the first update: aircraft seen from then on are new arrivals in range
        self.movements: dict[tuple[str, str], tuple[float, str]] = {}  # (use, callsign) -> (time, runway)

    def _initial_phase(self, t: Traffic) -> str:
        a = self.airport
        if t.on_ground:
            if _on_any_runway(a, t):
                return "rollout" if t.gs_kt >= ROLL_KT else "lineup"
            return "taxi_out" if t.gs_kt >= TAXI_KT else "parked"
        fin = _final_runway(a, t)
        if fin and fin[1] <= FINAL_EVENT_NM:
            return "final"
        inbound = heading_diff(t.heading_deg, bearing_deg(t.lat, t.lon, a.lat, a.lon)) < 90 \
            and distance_nm(t.lat, t.lon, a.lat, a.lon) > 3.0  # pointing at the field from outside: arriving
        return "arrival" if fin or inbound or t.alt_msl_ft - a.elevation_ft < 3000 else "departed"

    def update(self, traffic: list[Traffic], now: float) -> list[Event]:
        """Feed the current traffic list; returns the events that happened since the last call."""
        a = self.airport
        events: list[Event] = []
        seen = set()
        for t in traffic:
            seen.add(t.callsign)
            tr = self.tracks.get(t.callsign)
            if tr is None:
                tr = self.tracks[t.callsign] = Track(t.callsign, self._initial_phase(t), now, t)
                on = _aligned_runway(a, t) if tr.phase in ("lineup", "rollout") else None
                tr.runway = on.ident if on is not None else None
                fin = _final_runway(a, t) if not t.on_ground else None
                d = distance_nm(t.lat, t.lon, a.lat, a.lon)
                tr.dep_checked = tr.phase == "departed"  # already climbing out: it checked in before we looked
                # at startup everyone in range is mid-approach already; later, an arrival is first seen as it
                # comes into range (15 NM) and gets its approach call
                tr.approach_called = tr.phase == "final" or (not self.started and d < APPROACH_NM[1])
                tr.handoff_called = tr.phase == "final" or bool(fin and fin[1] <= HANDOFF_FINAL_NM)
                continue
            radar = self._radar_event(tr, t, now)
            if radar is not None:
                events.append(radar)
            new = self._next_phase(tr, t, now)
            tr.last = t
            if t.on_ground and t.gs_kt < 1:
                tr.stopped_at = tr.stopped_at or now
            else:
                tr.stopped_at = None
            if new is None or new == tr.phase:
                if tr.phase == "final" and not tr.cleared and not t.on_ground and now - tr.last_offer >= RECHECK_S:
                    tr.last_offer = now  # still waiting for "cleared to land": maybe the runway is free now
                    fin = _final_runway(a, t)
                    events.append(Event("final", t, a, fin[0] if fin else None, now, repeat=True))
                continue
            kind = {"pushback": "pushback", "taxi_out": "taxi_out", "lineup": "lineup", "takeoff": "takeoff_roll", "departed": "departed",
                    "final": "final", "vacated": "vacated", "taxi_in": "taxi_in"}.get(new)
            rwy = _aligned_runway(a, t) if new in ("lineup", "takeoff") else \
                (_final_runway(a, t) or (None,))[0] if new == "final" else None
            if rwy is not None:
                tr.runway = rwy.ident
                self.movements[("arrival" if new == "final" else "departure", t.callsign)] = (now, rwy.ident)
            tr.phase, tr.since = new, now
            if new == "final":
                tr.last_offer = now
            if kind:
                events.append(Event(kind, t, a, rwy, now))
        for gone in set(self.tracks) - seen:  # left the area, despawned, or parked out of range
            del self.tracks[gone]
        self.started = True
        return events

    def ai_flow(self, now: float) -> dict[str, str]:
        """{"departure": "31", "arrival": "31"}: the runway most AI lined up / took off on, and came in on final to,
        within the last AI_FLOW_WINDOW_S (a tie goes to the most recent one). Older movements are forgotten."""
        self.movements = {k: v for k, v in self.movements.items() if now - v[0] <= AI_FLOW_WINDOW_S}
        out: dict[str, str] = {}
        for use in ("departure", "arrival"):
            seen: dict[str, tuple[int, float]] = {}  # runway -> (count, latest)
            for (u, _), (at, ident) in self.movements.items():
                if u == use:
                    n, last = seen.get(ident, (0, 0.0))
                    seen[ident] = (n + 1, max(last, at))
            if seen:
                out[use] = max(seen, key=lambda i: seen[i])
        return out

    def runway_users(self, now: float) -> dict[str, str | None]:
        """{callsign: runway} of the AI lined up, rolling for takeoff or rolling out after landing: Tower put them
        there and keeps the runway theirs until they are airborne or off it (a line-up held longer than
        LINEUP_HELD_S is dropped, in case the phase got stuck)."""
        return {cs: tr.runway for cs, tr in self.tracks.items()
                if tr.phase in ("takeoff", "rollout") or (tr.phase == "lineup" and now - tr.since < LINEUP_HELD_S)}

    def _radar_event(self, tr: Track, t: Traffic, now: float) -> Event | None:
        """Departure check-in, approach clearance, handoff to Tower: from position, height and track."""
        a = self.airport
        if t.on_ground:
            return None
        agl = t.alt_msl_ft - a.elevation_ft
        if tr.phase == "departed" and not tr.dep_checked and agl >= DEP_CHECKIN_AGL_FT:
            tr.dep_checked = True
            return Event("dep_checkin", t, a, None, now)
        if tr.phase != "arrival":
            return None
        fin = _final_runway(a, t)
        if fin and FINAL_EVENT_NM < fin[1] <= HANDOFF_FINAL_NM and not tr.handoff_called:
            tr.handoff_called = tr.approach_called = True
            return Event("app_handoff", t, a, fin[0], now)
        d = distance_nm(t.lat, t.lon, a.lat, a.lon)
        inbound = heading_diff(t.heading_deg, bearing_deg(t.lat, t.lon, a.lat, a.lon)) < 100
        if not tr.approach_called and APPROACH_NM[0] <= d <= APPROACH_NM[1] and inbound and not fin:
            tr.approach_called = True
            return Event("approach", t, a, None, now)
        return None

    def _next_phase(self, tr: Track, t: Traffic, now: float) -> str | None:
        a, p = self.airport, tr.phase
        if t.on_ground:
            rwy = _aligned_runway(a, t)
            if p in ("final", "arrival") and _on_any_runway(a, t):
                return "rollout"
            if p == "rollout":
                return "vacated" if not _on_any_runway(a, t) else None
            if p == "vacated":
                return "taxi_in" if t.gs_kt >= TAXI_KT else None
            if p == "parked" and t.gs_kt >= 0.5 and _backwards(tr.last, t):
                return "pushback"  # moving tail first: being pushed back from the stand
            if p == "pushback" and t.gs_kt >= TAXI_KT and not _backwards(tr.last, t) and not _on_any_runway(a, t):
                return "taxi_out"
            if p in ("parked", "taxi_in") and t.gs_kt >= TAXI_KT and not _on_any_runway(a, t):
                return "taxi_out" if p == "parked" else None
            if p in ("taxi_out", "holding", "parked") and rwy is not None:
                return "takeoff" if t.gs_kt >= ROLL_KT / 2 else "lineup"
            if p == "lineup" and rwy is not None and t.gs_kt >= ROLL_KT / 2:
                return "takeoff"
            if p == "taxi_out" and t.gs_kt < 1 and _near_holding_point(a, t):
                return "holding"
            if p == "taxi_in" and t.gs_kt < 1 and tr.stopped_at and now - tr.stopped_at >= STOPPED_S * 10:
                return "parked"
            return None
        # airborne
        if p == "takeoff":
            return "departed" if t.alt_msl_ft - a.elevation_ft >= DEPARTED_AGL_FT else None
        if p in ("arrival", "departed"):
            fin = _final_runway(a, t)
            if fin and fin[1] <= FINAL_EVENT_NM and p == "arrival":
                return "final"
            if p == "departed" and t.alt_msl_ft - a.elevation_ft < 3000 and fin and fin[1] <= FINAL_EVENT_NM:
                return "final"  # a circuit / go-around coming back
        return None
