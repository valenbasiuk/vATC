"""What each AI aircraft is doing, from its telemetry only (position, speed, on-ground), so ATC can talk to it.

MSFS's AI traffic follows the sim's own internal ATC; we can't command it. Instead the tracker turns its movement
into events at the moment they happen ("taxi_out", "lineup", "takeoff_roll", "departed", "final", "vacated",
"taxi_in") and chatter.py says the matching instruction on the frequency, so the radio sounds like the traffic is
being controlled. A new aircraft (first time seen) starts in the phase it is in, without an event: no burst of
chatter at startup.
"""

from __future__ import annotations

from dataclasses import dataclass

from atc.geo import heading_diff
from atc.models import Airport, Runway, Traffic
from atc.sequence import along_cross, final_distance, on_runway

TAXI_KT = 3.0  # moving on the ground
ROLL_KT = 30.0  # takeoff roll / landing rollout
FINAL_EVENT_NM = 6.0  # "cleared to land" this far out
DEPARTED_AGL_FT = 600.0
STOPPED_S = 3.0


@dataclass
class Track:
    callsign: str
    phase: str  # parked, taxi_out, holding, lineup, takeoff, departed, arrival, final, rollout, vacated, taxi_in
    since: float
    last: Traffic
    runway: str | None = None
    stopped_at: float | None = None
    cleared: bool = False  # landing clearance said (set by the caller); until then "final" is offered again
    last_offer: float = 0.0


@dataclass
class Event:
    kind: str  # taxi_out, lineup, takeoff_roll, departed, final, vacated, taxi_in
    traffic: Traffic
    airport: Airport
    runway: Runway | None
    at: float
    repeat: bool = False  # "final" again for an aircraft still waiting for its landing clearance

RECHECK_S = 5.0  # an aircraft on final without landing clearance is looked at again this often


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


class TrafficTracker:
    def __init__(self, airport: Airport) -> None:
        self.airport = airport
        self.tracks: dict[str, Track] = {}

    def _initial_phase(self, t: Traffic) -> str:
        a = self.airport
        if t.on_ground:
            if _on_any_runway(a, t):
                return "rollout" if t.gs_kt >= ROLL_KT else "lineup"
            return "taxi_out" if t.gs_kt >= TAXI_KT else "parked"
        fin = _final_runway(a, t)
        if fin and fin[1] <= FINAL_EVENT_NM:
            return "final"
        return "arrival" if fin or t.alt_msl_ft - a.elevation_ft < 3000 else "departed"

    def update(self, traffic: list[Traffic], now: float) -> list[Event]:
        """Feed the current traffic list; returns the events that happened since the last call."""
        a = self.airport
        events: list[Event] = []
        seen = set()
        for t in traffic:
            seen.add(t.callsign)
            tr = self.tracks.get(t.callsign)
            if tr is None:
                self.tracks[t.callsign] = Track(t.callsign, self._initial_phase(t), now, t)
                continue
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
            kind = {"taxi_out": "taxi_out", "lineup": "lineup", "takeoff": "takeoff_roll", "departed": "departed",
                    "final": "final", "vacated": "vacated", "taxi_in": "taxi_in"}.get(new)
            rwy = _aligned_runway(a, t) if new in ("lineup", "takeoff") else \
                (_final_runway(a, t) or (None,))[0] if new == "final" else None
            if rwy is not None:
                tr.runway = rwy.ident
            tr.phase, tr.since = new, now
            if new == "final":
                tr.last_offer = now
            if kind:
                events.append(Event(kind, t, a, rwy, now))
        for gone in set(self.tracks) - seen:  # left the area, despawned, or parked out of range
            del self.tracks[gone]
        return events

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
