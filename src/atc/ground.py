"""Ground movement conflicts between the user and AI traffic, decided in code from both aircraft's telemetry.

While the user taxis on Ground (or on Tower at a field without Ground), every watcher tick looks LOOKAHEAD_S ahead
in straight lines along each aircraft's actual movement (its heading only when it hasn't moved since the last
tick). If an AI moving on the taxiways would come within CONFLICT_NM of the user on PERSIST_TICKS ticks in a row:
  - it comes from the side   -> "give way to the Airbus three twenty from the left"
  - it comes head-on         -> "hold position, Airbus three twenty opposite direction", and once it has gone past
                                "continue taxi".
Each AI is called once; at most one call every CALL_GAP_S.
"""

from __future__ import annotations

import math

from atc.geo import bearing_deg, distance_nm, heading_diff, offset_nm
from atc.models import OwnState, Traffic
from atc.sequence import on_runway
from atc.traffic import spoken_type

LOOKAHEAD_S = 30.0
CONFLICT_NM = 0.035  # ~65 m: two airliners' wingtips
WATCH_NM = 0.3  # only aircraft this close are looked at
MOVING_KT = 3.0
CALL_GAP_S = 20.0
HEAD_ON_DEG = 35.0  # headings this close to reciprocal: opposite direction
PERSIST_TICKS = 2  # a conflict must be predicted on this many ticks in a row before it is called


def _velocity(session, key: str, lat: float, lon: float, gs_kt: float, heading_deg: float,
              now: float) -> tuple[float, float]:
    """(east, north) NM/s from the actual movement since the last tick (a pushback moves tail first, and a taxiing
    aircraft's nose leads its path in turns), else from heading and ground speed."""
    prev = session.ground_seen.get(key)
    session.ground_seen[key] = (lat, lon, now)
    if prev is not None and 0.2 <= now - prev[2] <= 5.0:
        e, n = offset_nm(prev[0], prev[1], lat, lon)
        if math.hypot(e, n) * 1852 >= 1.0:  # moved at least a metre: trust the track
            dt = now - prev[2]
            return e / dt, n / dt
    h = math.radians(heading_deg)
    return gs_kt / 3600.0 * math.sin(h), gs_kt / 3600.0 * math.cos(h)


def _closest(own: OwnState, t: Traffic, v_own: tuple[float, float],
             v_ai: tuple[float, float]) -> tuple[float, float]:
    """(time of closest approach in s within the lookahead, distance then in NM), straight lines, constant speed."""
    e, n = offset_nm(own.lat, own.lon, t.lat, t.lon)  # AI relative to us
    de, dn = v_ai[0] - v_own[0], v_ai[1] - v_own[1]  # relative velocity (NM/s)
    v2 = de * de + dn * dn
    tc = 0.0 if v2 < 1e-12 else max(0.0, min(LOOKAHEAD_S, -(e * de + n * dn) / v2))
    return tc, math.hypot(e + de * tc, n + dn * tc)


def _track(v: tuple[float, float], fallback: float) -> float:
    return math.degrees(math.atan2(v[0], v[1])) % 360 if math.hypot(*v) > 1e-7 else fallback


def _what(t: Traffic) -> str:
    typ = spoken_type(getattr(t, "type", None))
    return f"the {typ}" if typ else "the traffic"


def ground_event(session, world, own: OwnState, traffic: list[Traffic], now: float) -> str | None:
    """One Ground call about taxiing traffic that is due now, or None."""
    picked = world.pick(own)
    if picked is None or not own.on_ground:
        session.ground_hold = None
        session.ground_seen.clear()
        return None
    airport, facility = picked
    v_own = _velocity(session, "", own.lat, own.lon, own.gs_kt, own.heading_deg, now)
    v_ai = {t.callsign: _velocity(session, t.callsign, t.lat, t.lon, t.gs_kt, t.heading_deg, now)
            for t in traffic if t.on_ground}
    has_ground = any(f.kind in ("GND", "RMP") for f in airport.frequencies)
    if facility.role != "ground" and not (facility.role == "tower" and not has_ground):
        return None
    cs = session.spoken_callsign
    own_track = _track(v_own, own.heading_deg)
    # after "hold position": "continue taxi" once that aircraft has gone past (or gone)
    if session.ground_hold is not None:
        held = next((t for t in traffic if t.callsign == session.ground_hold), None)
        behind = held is not None and heading_diff(bearing_deg(own.lat, own.lon, held.lat, held.lon),
                                                    own.heading_deg) > 100
        if held is None or behind or distance_nm(own.lat, own.lon, held.lat, held.lon) > WATCH_NM:
            session.ground_hold = None
            session.last_ground_call = now
            return f"{cs}, continue taxi."
        return None
    if own.gs_kt < MOVING_KT or now - session.last_ground_call < CALL_GAP_S:
        session.ground_conflict.clear()
        return None
    if any(on_runway(airport, r, own) for r in airport.runways):
        return None  # lining up / vacating: Tower's business
    seen_now = set()
    for t in sorted(traffic, key=lambda t: distance_nm(own.lat, own.lon, t.lat, t.lon)):
        if not t.on_ground or t.gs_kt < MOVING_KT or t.callsign in session.ground_called:
            continue
        if distance_nm(own.lat, own.lon, t.lat, t.lon) > WATCH_NM:
            break
        if any(on_runway(airport, r, t) for r in airport.runways):
            continue
        tc, dmin = _closest(own, t, v_own, v_ai[t.callsign])
        if tc <= 0.0 or dmin > CONFLICT_NM:
            continue
        seen_now.add(t.callsign)
        if session.ground_conflict.get(t.callsign, 0) + 1 < PERSIST_TICKS:
            continue  # seen once: a turn on the taxiway may end it; call it if it is still there next tick
        session.ground_called.add(t.callsign)
        session.last_ground_call = now
        ai_track = _track(v_ai[t.callsign], t.heading_deg)
        if heading_diff(own_track, ai_track) >= 180 - HEAD_ON_DEG:
            session.ground_hold = t.callsign
            typ = spoken_type(getattr(t, "type", None)) or "traffic"
            return f"{cs}, hold position, {typ} opposite direction."
        if heading_diff(own_track, ai_track) <= HEAD_ON_DEG:
            continue  # same direction: one follows the other
        rel = (bearing_deg(own.lat, own.lon, t.lat, t.lon) - own_track) % 360
        side = "right" if rel < 180 else "left"
        return f"{cs}, give way to {_what(t)} from the {side}."
    session.ground_conflict = {c: session.ground_conflict.get(c, 0) + 1 for c in seen_now}
    return None
