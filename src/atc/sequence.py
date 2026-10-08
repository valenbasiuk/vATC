"""Who is on the runway and who is on final, decided in code (roadmap item 13, first slice).

Tower must not clear the pilot for takeoff with traffic on the runway or on short final, and must not
clear them to land behind someone who is closer to the threshold. The model gets this as facts in
CONTEXT, and `guard()` replaces any takeoff/landing clearance it still gives when code says no.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from atc import phrase
from atc.geo import heading_diff, offset_nm
from atc.models import Airport, OwnState, Runway, Traffic

FINAL_MAX_NM = 10.0
FINAL_MAX_AGL_FT = 3500.0
SHORT_FINAL_NM = 3.0  # an arrival inside this blocks a takeoff in front of it (about 60-80 s out)
FINAL_TRACK_DEG = 30.0  # heading within this of the runway heading
ROLL_KT = 30.0  # faster than this along the runway: a takeoff roll (line up and wait behind it)
RUNWAY_HALF_WIDTH_NM = 0.03  # ~55 m: on the runway, not at the holding point or on a parallel taxiway
FT_PER_NM = 6076.1

# AI that Tower itself put on a runway (line up, takeoff roll, landing roll; tracker phases), per airport, kept by
# the chatter watcher. Tower remembers whom it cleared: they block a takeoff even when a position read misses them
# (real sim, SABE: "Bondi, line up and wait runway three one", then the user was cleared for takeoff on 13).
RUNWAY_USERS: dict[str, dict[str, str | None]] = {}  # ICAO -> {callsign: runway ident, None = unknown}


def same_strip(a: str, b: str) -> bool:
    """'13' and '31', '28L' and '10R': the two directions of one runway (or the same one)."""
    a, b = a.upper(), b.upper()
    if a == b:
        return True
    if not (a[:2].isdigit() and b[:2].isdigit()) or abs(int(a[:2]) - int(b[:2])) != 18:
        return False
    flip = {"L": "R", "R": "L", "C": "C", "": ""}
    return flip.get(a[2:], "?") == b[2:]


def threshold(airport: Airport, rwy: Runway) -> tuple[float, float]:
    """Threshold position. Without one on file, assume the airport reference point is the runway midpoint."""
    if rwy.lat is not None and rwy.lon is not None:
        return rwy.lat, rwy.lon
    half_nm = (rwy.length_ft or 0.0) / 2 / FT_PER_NM
    h = math.radians(rwy.heading_deg or 0.0)
    return (
        airport.lat - half_nm * math.cos(h) / 60.0,
        airport.lon - half_nm * math.sin(h) / (60.0 * math.cos(math.radians(airport.lat))),
    )


def along_cross(airport: Airport, rwy: Runway, lat: float, lon: float) -> tuple[float, float]:
    """(NM past the threshold in the landing direction, negative = on the approach side; NM off the centerline)."""
    tlat, tlon = threshold(airport, rwy)
    e, n = offset_nm(tlat, tlon, lat, lon)
    h = math.radians(rwy.heading_deg or 0.0)
    return e * math.sin(h) + n * math.cos(h), abs(e * math.cos(h) - n * math.sin(h))


def final_distance(airport: Airport, rwy: Runway, a: Traffic | OwnState) -> float | None:
    """NM to the threshold if `a` is airborne on final for `rwy`, else None."""
    if a.on_ground or rwy.heading_deg is None:
        return None
    along, cross = along_cross(airport, rwy, a.lat, a.lon)
    dist = -along
    if not (-0.2 < dist <= FINAL_MAX_NM):
        return None
    if cross > max(0.5, dist * 0.15):  # a wider funnel further out
        return None
    if a.alt_msl_ft - airport.elevation_ft > FINAL_MAX_AGL_FT:
        return None
    if heading_diff(a.heading_deg, rwy.heading_deg) > FINAL_TRACK_DEG:
        return None
    return max(dist, 0.0)


def on_runway(airport: Airport, rwy: Runway, a: Traffic | OwnState) -> bool:
    """On the runway surface (either direction): on the ground, or airborne below 200 ft over it."""
    if rwy.heading_deg is None:
        return False
    if not a.on_ground and a.alt_msl_ft - airport.elevation_ft > 200:
        return False
    along, cross = along_cross(airport, rwy, a.lat, a.lon)
    length = (rwy.length_ft or 0.0) / FT_PER_NM
    return -0.05 <= along <= length + 0.05 and cross <= RUNWAY_HALF_WIDTH_NM


@dataclass
class RunwayStatus:
    runway: Runway
    occupied_by: list[str] = field(default_factory=list)
    finals: list[tuple[str, float]] = field(default_factory=list)  # other traffic, nearest first
    own_final_nm: float | None = None
    types: dict[str, str] = field(default_factory=dict)  # callsign -> spoken type, when the sim gives one
    departing: list[str] = field(default_factory=list)  # of occupied_by: on the takeoff roll in our direction
    # on final for the OTHER end of this runway (head-on): nothing is cleared while one is there. The sim's AI
    # (or FS Traffic's) can land the other way: Tower cleared one onto 31 with ours on final for 13 (2026-10-07).
    opposite: list[tuple[str, float, str]] = field(default_factory=list)  # (callsign, NM, runway ident)

    def _what(self, callsign: str) -> str:
        return self.types.get(callsign) or "traffic"

    def takeoff_blocked(self) -> str | None:
        """Why the pilot may not take off now (spoken traffic info), or None."""
        if self.occupied_by:
            return f"{self._what(self.occupied_by[0])} on the runway"
        if self.opposite:
            return self._head_on()
        short = [(c, d) for c, d in self.finals if d <= SHORT_FINAL_NM]
        if short:
            return f"{self._what(short[0][0])} on {_miles(short[0][1])} final"
        return None

    def ahead_on_final(self) -> list[tuple[str, float]]:
        if self.own_final_nm is None:
            return list(self.finals)
        return [(c, d) for c, d in self.finals if d < self.own_final_nm]

    def landing_blocked(self) -> str | None:
        """Why the pilot may not be cleared to land now, or None."""
        ahead = self.ahead_on_final()
        if ahead:
            cs, nm = ahead[-1]
            typ = self.types.get(cs)
            follow = f"follow the {typ}" if typ else "traffic to follow"
            return f"number {_number(len(ahead) + 1)}, {follow} on {_miles(nm)} final"
        if self.occupied_by:
            return f"{self._what(self.occupied_by[0])} on the runway"
        if self.opposite:
            return self._head_on()
        return None

    def _head_on(self) -> str:
        cs, nm, ident = self.opposite[0]
        return f"{self._what(cs)} landing runway {phrase.runway(ident)}, {_miles(nm)} final"


def wait_for_takeoff(st: RunwayStatus, airport: Airport, own: OwnState) -> str | None:
    """What Tower says instead of a takeoff clearance when the runway isn't available (without the callsign), or
    None when it is. ICAO, landing traffic on short final and the runway otherwise free: a conditional line-up
    ("behind the landing Airbus three twenty on two mile final, line up and wait runway three one, behind").
    FAA has no conditional clearances: "hold short of runway two eight left, traffic on two mile final"."""
    why = st.takeoff_blocked()
    if why is None:
        return None
    faa = airport.faa
    rw = phrase.runway(st.runway.ident, faa)
    lined_up = on_runway(airport, st.runway, own)
    short = [(c, d) for c, d in st.finals if d <= SHORT_FINAL_NM]
    if st.occupied_by and set(st.occupied_by) <= set(st.departing) and not short and not lined_up:
        # only a departure rolling ahead of us: line up behind it, the takeoff clearance follows once it's airborne
        return f"runway {rw}, line up and wait" if faa else f"line up and wait runway {rw}"
    if not faa and short and not st.occupied_by and not lined_up:
        cs, nm = short[0]
        return f"behind the landing {st._what(cs)} on {_miles(nm)} final, line up and wait runway {rw}, behind"
    if faa and not lined_up:
        return f"hold short of runway {rw}, {why}"
    return f"hold position, {why}"


def _miles(nm: float) -> str:
    """Before "final" the distance is an adjective: "on two mile final", never "two miles final"."""
    return f"{phrase.digits(str(max(1, round(nm))))} mile"


def _number(n: int) -> str:
    return phrase.digits(str(n))


def runway_status(airport: Airport, rwy: Runway, own: OwnState, traffic: list[Traffic]) -> RunwayStatus:
    from atc.traffic import spoken_type

    st = RunwayStatus(runway=rwy, own_final_nm=final_distance(airport, rwy, own))
    users = RUNWAY_USERS.get(airport.icao, {})
    reciprocal = [r for r in airport.runways if r is not rwy and r.ident != rwy.ident and same_strip(r.ident, rwy.ident)]
    for t in traffic:
        typ = spoken_type(getattr(t, "type", None))
        if typ:
            st.types[t.callsign] = typ
        cleared_on = t.on_ground and t.callsign in users and \
            (users[t.callsign] is None or same_strip(users[t.callsign], rwy.ident))
        if on_runway(airport, rwy, t) or cleared_on:
            st.occupied_by.append(t.callsign)
            if t.gs_kt >= ROLL_KT and heading_diff(t.heading_deg, rwy.heading_deg) <= 20:
                st.departing.append(t.callsign)
            continue
        d = final_distance(airport, rwy, t)
        if d is not None:
            st.finals.append((t.callsign, d))
            continue
        for other in reciprocal:
            d = final_distance(airport, other, t)
            if d is not None:
                st.opposite.append((t.callsign, d, other.ident))
    st.finals.sort(key=lambda x: x[1])
    st.opposite.sort(key=lambda x: x[1])
    return st


def debug_line(airport: Airport, st: RunwayStatus, traffic: list[Traffic]) -> str:
    """What Tower saw when it decided (printed to the terminal): who is on the runway / on final, and the ground
    AI closest to the centerline with their position along / across it, to tell a geometry miss from a timing one."""
    rwy = st.runway
    near = []
    for t in traffic:
        if t.on_ground:
            along, cross = along_cross(airport, rwy, t.lat, t.lon)
            near.append((cross, f"{t.callsign} along {along:.2f} cross {cross:.3f} hdg {t.heading_deg:.0f} "
                                f"gs {t.gs_kt:.0f}"))
    near.sort()
    users = RUNWAY_USERS.get(airport.icao, {})
    finals = ", ".join(f"{c} {d:.1f} NM" for c, d in st.finals) or "-"
    return (f"[runway {rwy.ident}: on it {', '.join(st.occupied_by) or '-'}; final {finals}; "
            f"Tower's runway users {users or '-'}; {len(traffic)} AI; closest on the ground: "
            + ("; ".join(n for _, n in near[:3]) or "none") + "]")


def context_lines(st: RunwayStatus, own: OwnState) -> list[str]:
    """Facts for the Tower prompt."""
    rid = st.runway.ident
    lines = [
        f"RUNWAY {rid} STATUS (computed by software, treat as fact)",
        "  On the runway: " + (", ".join(st.occupied_by) or "nobody"),
        "  Other traffic on final: " + (", ".join(f"{c} {d:.1f} NM" for c, d in st.finals) or "nobody"),
    ]
    if own.on_ground:
        why = st.takeoff_blocked()
        lines.append("  Takeoff clearance for the pilot: ALLOWED" if why is None else
                     f"  Takeoff clearance for the pilot: NOT ALLOWED ({why}). Never say 'cleared for takeoff'; "
                     "if they are ready, tell them to hold position and give the traffic.")
    else:
        if st.own_final_nm is not None:
            lines.append(f"  The pilot is on {st.own_final_nm:.1f} NM final runway {rid}.")
        why = st.landing_blocked()
        lines.append("  Landing clearance for the pilot: ALLOWED once they are on final" if why is None else
                     f"  Landing clearance for the pilot: NOT ALLOWED ({why}). Never say 'cleared to land'; "
                     "give their number in sequence and the traffic to follow.")
    return lines


_TAKEOFF = re.compile(r"cleared (?:for )?(?:immediate )?take ?off", re.I)
_LAND = re.compile(r"cleared to land", re.I)


def guard(reply: str, st: RunwayStatus | None, spoken_callsign: str, own: OwnState) -> str:
    """Replace a takeoff/landing clearance the model gave when code says the runway is not available."""
    if st is None:
        return reply
    if own.on_ground and _TAKEOFF.search(reply):
        why = st.takeoff_blocked()
        if why:
            return f"{spoken_callsign}, hold position, {why}."
    if not own.on_ground and _LAND.search(reply):
        why = st.landing_blocked()
        if why:
            return f"{spoken_callsign}, {why}, continue approach." if why.startswith("number") else \
                f"{spoken_callsign}, continue approach, {why}, expect late landing clearance."
        if st.own_final_nm is None:  # free runway, but not on final yet (e.g. a VFR call 10 NM out): too early
            return _report_final_instead(reply)
    return reply


def _report_final_instead(reply: str) -> str:
    """'..., runway zero two, cleared to land.' -> '..., runway zero two, report final.' (one 'final' only)."""
    out: list[str] = []
    for c in (c.strip() for c in reply.rstrip(". ").split(",")):
        if _LAND.search(c):
            rwy = re.search(r"runway [a-z ]+", c)
            c = (rwy.group(0).strip() + ", " if rwy and not any("runway" in o for o in out) else "") + "report final"
        if "final" in c and any("final" in o for o in out):
            continue
        out.append(c)
    return ", ".join(out) + "."
