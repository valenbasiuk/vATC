"""How our aircraft move: no flight model, just believable kinematics seen from a cockpit nearby.

- On the ground an aircraft follows a path (a polyline of graph nodes) at a speed it chooses: each segment has its
  own limit (taxiway, apron), it slows for corners, and it can be told to stop at a distance along the path (behind
  traffic, short of the holding point). Speed changes go through a smooth controller (gentle braking, no jerk), so
  it doesn't stop dead behind somebody. Its heading is the direction of a short chord around where it is, so it
  turns smoothly through a node instead of snapping. Pushback runs the same path tail first.
- The takeoff is a roll along the runway heading to Vr, a rotation, liftoff and a straight climb.
- The approach is a 3 degree glide path down the extended centerline (level at the platform height until it meets
  it), slowing to the approach speed by 5 NM, a flare, the touchdown and a rollout.
Positions are in a local flat frame (metres east / north of a reference): fine within an airport and its final.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

M_PER_DEG_LAT = 111_320.0
KT = 0.514444  # m/s per knot
FT = 0.3048
NM = 1852.0

TAXI_KT = 15.0  # on a named taxiway
APRON_KT = 10.0  # apron lanes and lead-in lines (unnamed in the sim's data)
MAX_TAXI_KT = 20.0  # never faster on the ground, whatever a segment says (Valen: "not above 20 kt")
EXIT_KT = 30.0  # after landing, on the runway to the exit (not a taxiway: the 20 kt cap doesn't apply there)
TURN_KT = 7.0  # through a corner of more than TURN_DEG
TURN_DEG = 35.0
PUSH_KT = 2.5
TAXI_ACCEL = 0.35  # m/s2, speeding up
COMFORT_DECEL = 0.45  # m/s2, planned braking (stop points, corners)
MAX_DECEL = 1.4  # m/s2, if something appears close ahead
JERK = 0.3  # m/s3: how fast the acceleration itself may change (speeding up: a gentle start)
BRAKE_JERK = 1.0  # m/s3 braking in
STOP_TAU = 1.2  # s: the last bit of a stop fades out (decel no more than speed / STOP_TAU) instead of a jolt
SPEED_TAU = 1.6  # s: how quickly the speed follows its target
CHORD_M = 9.0  # heading = direction of the path from CHORD_M behind to CHORD_M ahead


@dataclass
class Perf:
    vr_kt: float
    climb_fpm: float
    climb_kt: float  # speed held in the initial climb
    accel: float  # m/s2 on the takeoff roll
    wingspan_m: float
    vapp_kt: float = 140.0
    length_m: float = 38.0


def _p(vr, fpm, ckt, acc, span, vapp, length) -> Perf:
    return Perf(vr, fpm, ckt, acc, span, vapp, length)


_PERF = {  # by the first letters of the ICAO type; the longest match wins
    "A3": _p(145, 2000, 170, 1.9, 36, 137, 38), "A31": _p(140, 2200, 165, 2.0, 34, 132, 34),
    "A32": _p(145, 2000, 170, 1.9, 36, 137, 38), "A321": _p(150, 1900, 175, 1.8, 36, 142, 45),
    "A2": _p(145, 2000, 170, 1.9, 36, 137, 38), "B73": _p(145, 2000, 170, 1.9, 36, 142, 40),
    "B3": _p(145, 2000, 170, 1.9, 36, 142, 40), "B739": _p(150, 1900, 175, 1.8, 36, 148, 42),
    "A33": _p(155, 1800, 180, 1.6, 60, 140, 63), "A34": _p(155, 1600, 180, 1.4, 60, 140, 63),
    "A35": _p(155, 1900, 180, 1.6, 65, 140, 67), "A38": _p(160, 1500, 180, 1.3, 80, 140, 73),
    "B74": _p(160, 1500, 180, 1.4, 65, 150, 70), "B76": _p(150, 1900, 175, 1.7, 48, 140, 55),
    "B77": _p(160, 1700, 180, 1.5, 65, 145, 70), "B78": _p(155, 1900, 180, 1.6, 60, 142, 60),
    "B75": _p(140, 2500, 170, 2.1, 38, 135, 47), "E1": _p(135, 2200, 165, 2.0, 29, 128, 36),
    "E7": _p(130, 2300, 160, 2.0, 26, 125, 30), "CRJ": _p(135, 2200, 165, 2.0, 24, 135, 32),
    "AT": _p(105, 1500, 130, 1.5, 27, 110, 27), "DH8": _p(110, 1700, 140, 1.6, 28, 115, 33),
    "F10": _p(130, 2000, 160, 1.9, 28, 128, 35), "F28": _p(125, 2000, 160, 1.9, 25, 125, 27),
    "B46": _p(115, 1500, 150, 1.6, 26, 120, 26), "C1": _p(55, 700, 75, 1.2, 11, 65, 8),
    "P28": _p(60, 650, 75, 1.1, 11, 70, 8), "DA4": _p(60, 800, 80, 1.3, 12, 70, 8),
    "SR2": _p(70, 900, 95, 1.4, 12, 80, 8), "C208": _p(70, 900, 100, 1.2, 16, 85, 12),
    "C25": _p(110, 2500, 160, 2.0, 16, 115, 16),
}
GA_TYPES = ("C172", "C152", "P28A", "DA40", "C208", "C25C")


def perf(type_icao: str | None) -> Perf:
    t = (type_icao or "").upper()
    best = max((k for k in _PERF if t.startswith(k)), key=len, default=None)
    return _PERF[best] if best else _PERF["A32"]


@dataclass
class Pose:
    """What the sim is told each update. `agl_ft` 0 = on the ground (the injector keeps it on the terrain)."""

    lat: float
    lon: float
    agl_ft: float
    heading_deg: float
    pitch_deg: float = 0.0  # nose up positive (the injector converts to the sim's sign)
    bank_deg: float = 0.0
    gs_kt: float = 0.0
    gear_down: bool = True

    @property
    def on_ground(self) -> bool:
        return self.agl_ft <= 0.0


class Frame:
    """Metres east / north of a reference point, and back."""

    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0, self.lon0 = lat0, lon0
        self.k = math.cos(math.radians(lat0))

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * M_PER_DEG_LAT * self.k, (lat - self.lat0) * M_PER_DEG_LAT

    def latlon(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / M_PER_DEG_LAT, self.lon0 + x / (M_PER_DEG_LAT * self.k)


def moved(lat: float, lon: float, heading_deg: float, metres: float) -> tuple[float, float]:
    h = math.radians(heading_deg)
    return (lat + metres * math.cos(h) / M_PER_DEG_LAT,
            lon + metres * math.sin(h) / (M_PER_DEG_LAT * math.cos(math.radians(lat))))


def _bearing(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dx, dy)) % 360.0


class Path:
    """A polyline on the ground. `s` = metres along it from the first point. `limits_kt`: a speed limit for each
    segment (len(points) - 1), default TAXI_KT."""

    def __init__(self, points: list[tuple[float, float]], frame: Frame | None = None,
                 limits_kt: list[float] | None = None, cap_kt: float = MAX_TAXI_KT) -> None:
        limits = list(limits_kt) if limits_kt is not None else [TAXI_KT] * (len(points) - 1)
        pts, lims = [points[0]], []
        for p, lim in zip(points[1:], limits):
            if p != pts[-1]:
                pts.append(p)
                lims.append(lim)
        if len(pts) < 2:
            raise ValueError("a path needs two different points")
        self.frame = frame or Frame(*pts[0])
        self.xy = [self.frame.xy(*p) for p in pts]
        self.limits = [min(cap_kt, x) for x in lims]
        self.cum = [0.0]
        for (x1, y1), (x2, y2) in zip(self.xy, self.xy[1:]):
            self.cum.append(self.cum[-1] + math.hypot(x2 - x1, y2 - y1))
        self.length = self.cum[-1]
        self.corners = self._corners()

    def _corners(self) -> list[tuple[float, float]]:
        """(s, turn angle) at each inner point that turns more than TURN_DEG."""
        out = []
        for i in range(1, len(self.xy) - 1):
            a = _bearing(self.xy[i][0] - self.xy[i - 1][0], self.xy[i][1] - self.xy[i - 1][1])
            b = _bearing(self.xy[i + 1][0] - self.xy[i][0], self.xy[i + 1][1] - self.xy[i][1])
            turn = abs((b - a + 180.0) % 360.0 - 180.0)
            if turn > TURN_DEG:
                out.append((self.cum[i], turn))
        return out

    def _seg(self, s: float) -> int:
        i = 1
        while i < len(self.cum) - 1 and self.cum[i] < s:
            i += 1
        return i

    def point(self, s: float) -> tuple[float, float]:
        """(x, y) at s; beyond the ends it goes on along the end segments."""
        i = self._seg(s)
        (x1, y1), (x2, y2) = self.xy[i - 1], self.xy[i]
        seg = self.cum[i] - self.cum[i - 1]
        t = (s - self.cum[i - 1]) / seg if seg else 0.0
        return x1 + (x2 - x1) * t, y1 + (y2 - y1) * t

    def latlon(self, s: float) -> tuple[float, float]:
        return self.frame.latlon(*self.point(s))

    def limit_at(self, s: float) -> float:
        return self.limits[min(self._seg(max(0.0, min(s, self.length))), len(self.limits)) - 1]

    def heading(self, s: float) -> float:
        """Direction of travel at s, smoothed over a chord (turns through nodes instead of snapping)."""
        a = self.point(max(0.0, s - CHORD_M))
        b = self.point(min(self.length, s + CHORD_M))
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 0.5:  # a path shorter than a metre
            return self.end_heading()
        return _bearing(b[0] - a[0], b[1] - a[1])

    def end_heading(self) -> float:
        (x1, y1), (x2, y2) = self.xy[-2], self.xy[-1]
        return _bearing(x2 - x1, y2 - y1)


class GroundMover:
    """Moves along a Path: the speed follows a target (segment limit, corners, a stop point) through a smooth
    controller: braking is planned at COMFORT_DECEL, the acceleration itself changes gently (JERK)."""

    def __init__(self, path: Path, backwards: bool = False, v: float = 0.0, run_through: bool = False) -> None:
        self.path, self.backwards, self.run_through = path, backwards, run_through
        self.s, self.v, self.a = 0.0, v, 0.0

    @property
    def done(self) -> bool:
        return self.s >= self.path.length - 0.3 and self.v < 0.3

    def target(self, cap_kt: float, stop: float) -> float:
        """The speed wanted now (m/s): the cap, this segment's limit, corners ahead, the stop point."""
        want = min(cap_kt, self.path.limit_at(self.s + 1.0)) * KT
        for at, _turn in self.path.corners:  # slow for corners ahead (and through them)
            if at >= self.s - 6.0:
                d = max(0.0, at - self.s)
                want = min(want, math.sqrt((TURN_KT * KT) ** 2 + 2 * COMFORT_DECEL * d))
        for i in range(self._next_seg(), len(self.path.limits)):  # a slower segment ahead
            d = self.path.cum[i] - self.s
            if d > 0:
                want = min(want, math.sqrt((self.path.limits[i] * KT) ** 2 + 2 * COMFORT_DECEL * d))
        room = max(0.0, stop - self.s)
        return min(want, math.sqrt(2 * COMFORT_DECEL * max(0.0, room - 0.5)))

    def _next_seg(self) -> int:
        return min(self.path._seg(self.s), len(self.path.limits))

    def step(self, dt: float, target_kt: float, stop_s: float | None = None) -> None:
        if dt <= 0:
            return
        end = self.path.length + (1e6 if self.run_through else 0.0)  # run_through: no stop at the end (rolling takeoff)
        stop = end if stop_s is None else min(stop_s, end)
        want = self.target(target_kt, stop)
        room = stop - self.s
        a_want = (want - self.v) / SPEED_TAU
        if room < self.v * self.v / (2 * COMFORT_DECEL) + 0.5 and self.v > want:  # must brake harder than planned
            a_want = -min(MAX_DECEL, self.v * self.v / (2 * max(0.5, room)))
        elif a_want < 0:
            a_want = max(a_want, -self.v / STOP_TAU)  # ease out: no jolt in the last metre
        a_want = max(-MAX_DECEL, min(TAXI_ACCEL, a_want))
        jerk = (JERK if a_want > self.a else BRAKE_JERK) * dt
        self.a += max(-jerk, min(jerk, a_want - self.a))
        self.v = max(0.0, self.v + self.a * dt)
        if self.v == 0.0 and self.a < 0:
            self.a = 0.0
        self.s = min(stop, self.s + self.v * dt)
        if self.s >= stop - 0.05 and want < 0.3:  # arrived at the stop point
            self.v, self.a = 0.0, 0.0

    def pose(self) -> Pose:
        lat, lon = self.path.latlon(self.s)
        h = self.path.heading(self.s)
        if self.backwards:
            h = (h + 180.0) % 360.0
        return Pose(lat, lon, 0.0, h, gs_kt=self.v / KT)


SPOOL_S = 5.0  # the takeoff thrust is reached this long after the roll starts


class Takeoff:
    """Roll along `heading_deg` from (lat, lon) at speed v0, rotate at Vr, lift off, climb straight ahead."""

    ROTATE_DPS = 2.5
    ROTATE_TO = 8.5
    LIFTOFF_PITCH = 6.5
    GEAR_UP_FT = 150.0

    def __init__(self, lat: float, lon: float, heading_deg: float, p: Perf, v0_kt: float = 0.0) -> None:
        self.frame = Frame(lat, lon)
        self.heading, self.p = heading_deg, p
        self.dist = 0.0  # metres along the heading
        self.v = v0_kt * KT
        self.agl_m = 0.0
        self.vs = 0.0  # m/s
        self.pitch = 0.0
        self.t_air: float | None = None
        self.t_roll = 0.0

    @property
    def airborne(self) -> bool:
        return self.agl_m > 0.0

    def step(self, dt: float) -> None:
        p = self.p
        if not self.airborne:
            self.t_roll += dt
            self.v += p.accel * min(1.0, 0.25 + self.t_roll / SPOOL_S) * dt  # thrust builds up: no jump
            if self.v >= p.vr_kt * KT:
                self.pitch = min(self.ROTATE_TO, self.pitch + self.ROTATE_DPS * dt)
            if self.pitch >= self.LIFTOFF_PITCH:
                self.vs = 1.0
                self.agl_m = 0.01
                self.t_air = 0.0
        else:
            self.t_air = (self.t_air or 0.0) + dt
            target_vs = p.climb_fpm * FT / 60.0
            self.vs = min(target_vs, self.vs + 2.0 * dt)
            target_v = (p.climb_kt if self.agl_m < 3000 * FT else 250.0) * KT
            self.v = min(target_v, self.v + 1.0 * dt) if self.v < target_v else self.v
            self.pitch = max(4.0, min(self.ROTATE_TO + 4.0, math.degrees(math.atan2(self.vs, self.v)) + 3.0))
            self.agl_m += self.vs * dt
        self.dist += self.v * dt

    def pose(self) -> Pose:
        h = math.radians(self.heading)
        lat, lon = self.frame.latlon(self.dist * math.sin(h), self.dist * math.cos(h))
        agl_ft = self.agl_m / FT
        return Pose(lat, lon, agl_ft, self.heading, self.pitch, 0.0, self.v / KT, gear_down=agl_ft < self.GEAR_UP_FT)


class Airborne:
    """In the air without a flight model (improvement plan H2/H3): it turns at a standard rate toward the heading it
    is given (banked into the turn, easing out near the end), climbs or descends toward a height and speeds up or
    slows down gently. What flies a climb-out toward the destination, a downwind and base onto the final, a circuit."""

    TURN_DPS = 3.0  # standard rate
    MAX_BANK = 25.0
    ACCEL = 0.8  # m/s2 (about 1.5 kt/s)
    DESCENT_FPM = 1500.0

    def __init__(self, lat: float, lon: float, agl_ft: float, heading_deg: float, speed_kt: float, p: Perf,
                 vs_fpm: float = 0.0) -> None:
        self.frame = Frame(lat, lon)
        self.x = self.y = 0.0
        self.agl_ft, self.heading, self.v = agl_ft, heading_deg % 360.0, speed_kt * KT
        self.p = p
        self.target_heading, self.target_agl, self.target_kt = self.heading, agl_ft, speed_kt
        self.climb_fpm = p.climb_fpm
        self.bank = 0.0
        self.vs = vs_fpm / 60.0  # ft/s now (eased toward what the target height asks for)
        self.gear_down = False

    def latlon(self) -> tuple[float, float]:
        return self.frame.latlon(self.x, self.y)

    def step(self, dt: float) -> None:
        if dt <= 0:
            return
        diff = (self.target_heading - self.heading + 540.0) % 360.0 - 180.0
        rate = max(-self.TURN_DPS, min(self.TURN_DPS, diff / 2.0))  # eases out over the last 6 degrees
        self.heading = (self.heading + rate * dt) % 360.0
        want_bank = rate / self.TURN_DPS * self.MAX_BANK
        self.bank += max(-5.0 * dt, min(5.0 * dt, want_bank - self.bank))  # rolls in and out at 5 deg/s
        dv = self.target_kt * KT - self.v
        self.v += max(-self.ACCEL * dt, min(self.ACCEL * dt, dv))
        dz = self.target_agl - self.agl_ft
        want_vs = (self.climb_fpm if dz > 0 else self.DESCENT_FPM) / 60.0 * max(-1.0, min(1.0, dz / 200.0))
        self.vs += max(-8.0 * dt, min(8.0 * dt, want_vs - self.vs))  # no step changes in the vertical speed
        self.agl_ft = max(0.0, self.agl_ft + self.vs * dt)
        h = math.radians(self.heading)
        self.x += self.v * math.sin(h) * dt
        self.y += self.v * math.cos(h) * dt

    def pose(self) -> Pose:
        lat, lon = self.latlon()
        pitch = 3.0 + math.degrees(math.atan2(self.vs * FT, max(1.0, self.v)))  # (the takeoff's climb pitch too)
        return Pose(lat, lon, max(0.1, self.agl_ft), self.heading, max(-3.0, min(12.0, pitch)), self.bank,
                    self.v / KT, gear_down=self.gear_down)


class Join:
    """Onto a final from the side (improvement plan H2) or round a circuit (H3), flown by an `Airborne`: legs relative to
    the runway (`threshold`, `heading`, `length_m`), on the `side` of it (+1 right of the landing direction, -1 left):

        upwind -> crosswind -> downwind -> base -> intercept -> done (Approach takes over `dist` NM out)

    Heights: the downwind at `pattern_agl_ft`, then down to where the glide path will be. The base turn and the
    turn onto the final lead by the turn radius, and both lateral holds look ahead, so it rolls out on the centerline
    instead of across it."""

    LOOKAHEAD_S = 8.0
    INTERCEPT_DEG_PER_NM = 150.0  # intercept angle per NM still to go across (at most 30)
    DONE_NM = 0.02  # on the centerline this closely (and aligned): the final approach takes over

    def __init__(self, threshold: tuple[float, float], heading_deg: float, length_m: float, p: Perf, air: Airborne,
                 phase: str, side: float, offset_nm: float, join_nm: float, pattern_agl_ft: float,
                 speeds_kt: tuple[float, float, float]) -> None:
        self.frame = Frame(*threshold)
        self.h, self.length_nm = heading_deg, length_m / NM
        self.p, self.air, self.phase = p, air, phase
        self.side, self.offset_nm, self.join_nm, self.pattern_agl = (1.0 if side >= 0 else -1.0), offset_nm, join_nm, \
            pattern_agl_ft
        self.v_downwind, self.v_base, self.v_final = speeds_kt
        self.upwind_turn_agl = 500.0 if p.vapp_kt < 100 else 1500.0
        self.extend = False  # set by the pilot while someone on the final would be too close at the join: a longer
        # downwind (at most EXTEND_MAX_NM beyond the usual base turn)

    EXTEND_MAX_NM = 8.0

    def along_cross(self) -> tuple[float, float]:
        x, y = self.frame.xy(*self.air.latlon())
        h = math.radians(self.h)
        return (x * math.sin(h) + y * math.cos(h)) / NM, (x * math.cos(h) - y * math.sin(h)) / NM

    def _cross_rate(self) -> float:
        """NM/s the aircraft moves to the right of the landing direction."""
        return self.air.v * math.sin(math.radians(self.air.heading - self.h)) / NM

    def _radius_nm(self) -> float:
        return self.air.v / math.radians(Airborne.TURN_DPS) / NM

    def step(self, dt: float) -> bool:
        """One step; True once it is established on the final (hand over to an Approach at -along)."""
        a = self.air
        along, cross = self.along_cross()
        s, r = self.side, self._radius_nm()
        ahead = cross + self._cross_rate() * self.LOOKAHEAD_S  # where it will be across the runway shortly
        if self.phase == "upwind":
            a.target_heading, a.target_agl, a.target_kt = self.h, self.pattern_agl, self.v_downwind
            if a.agl_ft >= self.upwind_turn_agl and along >= min(self.length_nm, 0.5):
                self.phase = "crosswind"
        if self.phase == "crosswind":
            a.target_heading = (self.h + 90.0 * s) % 360.0
            a.target_agl, a.target_kt = self.pattern_agl, self.v_downwind
            if cross * s >= self.offset_nm - r:
                self.phase = "downwind"
        if self.phase == "downwind":
            err = s * self.offset_nm - ahead  # > 0: needs to move right of the landing direction
            a.target_heading = (self.h + 180.0 - max(-20.0, min(20.0, 30.0 * err))) % 360.0
            a.target_agl, a.target_kt = self.pattern_agl, self.v_downwind
            if along <= -(self.join_nm - r * 0.8) and (not self.extend or along <= -(self.join_nm + self.EXTEND_MAX_NM)):
                self.phase = "base"
        if self.phase == "base":
            a.target_heading = (self.h - 90.0 * s) % 360.0
            a.target_agl = Approach.join_height_ft(self.p, self.join_nm)
            a.target_kt = self.v_base
            if abs(cross) <= r * 1.1 + 0.05:
                self.phase = "intercept"
        if self.phase == "intercept":
            a.target_heading = (self.h - max(-30.0, min(30.0, self.INTERCEPT_DEG_PER_NM * ahead))) % 360.0
            a.target_agl = min(a.target_agl, Approach.join_height_ft(self.p, max(0.5, -along)))
            a.target_kt = self.v_final
        a.step(dt)
        if self.phase == "intercept":
            along, cross = self.along_cross()
            aligned = abs((a.heading - self.h + 540.0) % 360.0 - 180.0) <= 2.0
            if abs(cross) <= self.DONE_NM and aligned and along < -0.3:
                return True
        return False


class Approach:
    """Final approach and landing on a runway: from `dist_nm` out on the extended centerline (threshold at
    (lat, lon), runway heading `heading_deg`) down a 3 degree path to a touchdown AIM_M past the threshold,
    flare, rollout. `slow_to_kt` (set by the pilot each step) holds it behind traffic ahead; `go_around()` turns
    it into a climb straight ahead."""

    GLIDE = math.radians(3.0)
    AIM_M = 250.0
    PLATFORM_FT = 3000.0
    FLARE_FT = 20.0
    GEAR_DOWN_NM = 6.0
    ROLLOUT_DECEL = 2.3  # m/s2 once all wheels are down (autobrake medium)

    def __init__(self, lat: float, lon: float, heading_deg: float, p: Perf, dist_nm: float,
                 agl_ft: float | None = None, speed_kt: float | None = None) -> None:
        """`agl_ft` / `speed_kt`: where a joining aircraft (downwind, base, circuit) meets the final, so the hand-over
        has no jump; else the usual platform height and speed of a straight-in."""
        self.frame = Frame(lat, lon)
        self.heading, self.p = heading_deg, p
        self.x = -dist_nm * NM  # metres along the runway heading from the threshold (negative = on final)
        # (spawned close in, warm start: already slowed to the speed it would have there, not 170 kt at 2 NM)
        self.v = min(max(p.vapp_kt + 30.0, 170.0 if p.vapp_kt > 100 else p.vapp_kt + 20),
                     self.profile_kt(p, dist_nm)) * KT
        self.agl_ft = min(self.PLATFORM_FT, self._glide_ft())
        self.pitch = 2.5
        self.on_ground = False
        self.going_around: Takeoff | None = None
        self.slow_to_kt: float | None = None
        self.touchdown_x: float | None = None
        self.rollout_decel = self.ROLLOUT_DECEL  # the pilot sets it at touchdown to make its planned exit
        if p.vapp_kt < 100:  # light aircraft: a lower, slower final
            self.agl_ft = min(1500.0, self._glide_ft())
        if agl_ft is not None:
            self.agl_ft = agl_ft  # a little above the glide path is fine: step() brings it down to it smoothly
        if speed_kt is not None:
            self.v = speed_kt * KT

    @staticmethod
    def profile_kt(p: Perf, nm: float) -> float:
        """The speed flown `nm` out: Vapp + 40 kt beyond 8 NM, slowing to Vapp by 5 NM."""
        return p.vapp_kt + max(0.0, min(1.0, (nm - 5.0) / 3.0)) * 40.0

    @classmethod
    def join_height_ft(cls, p: Perf, dist_nm: float) -> float:
        """The height an aircraft joining the final `dist_nm` out should be at (on or under the glide path)."""
        glide = max(0.0, (cls.AIM_M + dist_nm * NM) * math.tan(cls.GLIDE)) / FT
        return min(cls.PLATFORM_FT if p.vapp_kt >= 100 else 1500.0, glide)

    def _glide_ft(self) -> float:
        return max(0.0, (self.AIM_M - self.x) * math.tan(self.GLIDE)) / FT

    @property
    def dist_nm(self) -> float:
        """To the threshold (negative once past it)."""
        return -self.x / NM

    def go_around(self) -> None:
        if self.going_around is None and not self.on_ground:
            lat, lon = self._latlon()
            ga = Takeoff(lat, lon, self.heading, self.p, v0_kt=self.v / KT)
            ga.agl_m, ga.vs, ga.pitch, ga.t_air = self.agl_ft * FT, 2.0, 8.0, 0.0
            self.going_around = ga

    def step(self, dt: float) -> None:
        if self.going_around is not None:
            self.going_around.step(dt)
            return
        p = self.p
        if not self.on_ground:
            nm = self.dist_nm
            want = self.profile_kt(p, nm)
            if self.slow_to_kt is not None:
                want = max(p.vapp_kt - 5.0, min(want, self.slow_to_kt))
            dv = (want * KT - self.v)
            self.v += max(-0.6 * dt, min(0.6 * dt, dv))  # ~1 kt/s
            self.x += self.v * dt
            glide = self._glide_ft()
            if self.agl_ft > self.FLARE_FT:
                # level at the platform until the glide path comes down; above it (a joining aircraft a little
                # high) down to it at no more than 1800 ft/min, never a jump
                self.agl_ft = min(self.agl_ft, max(glide, self.agl_ft - 30.0 * dt))
                self.pitch = 2.5
            else:  # flare: sink slows to ~2.5 ft/s, nose up a little
                self.agl_ft = max(0.0, self.agl_ft - max(4.0, min(10.0, self.agl_ft / 2.0)) * dt)
                self.pitch = min(5.0, self.pitch + 1.0 * dt)
                self.v -= 0.4 * dt
            if self.agl_ft <= 0.0:
                self.on_ground, self.touchdown_x = True, self.x
        else:
            self.pitch = max(0.0, self.pitch - 2.0 * dt)  # nose wheel down
            decel = self.rollout_decel if self.pitch <= 0.5 else 1.0
            self.v = max(0.0, self.v - decel * dt)
            self.x += self.v * dt

    def _latlon(self) -> tuple[float, float]:
        h = math.radians(self.heading)
        return self.frame.latlon(self.x * math.sin(h), self.x * math.cos(h))

    def pose(self) -> Pose:
        if self.going_around is not None:
            return self.going_around.pose()
        lat, lon = self._latlon()
        return Pose(lat, lon, 0.0 if self.on_ground else self.agl_ft, self.heading, self.pitch, 0.0, self.v / KT,
                    gear_down=self.on_ground or self.dist_nm <= self.GEAR_DOWN_NM)
