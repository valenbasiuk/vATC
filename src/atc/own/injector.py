"""The sim side of our own traffic. `SimInjector` talks SimConnect (its own connection, used from the manager's
thread only); `FakeInjector` just remembers the poses (tests, fake sim).

Verified on Valen's PC (tools/probe_inject.py, 2026-10-07): SimConnect_AICreateNonATCAircraft with a model title
(sim AI model and FSLTL title both), AIReleaseControl + FREEZE_LATITUDE_LONGITUDE/ALTITUDE/ATTITUDE_SET, then
SetDataOnSimObject (lat, lon, alt, pitch, bank, heading) at 20 Hz: smooth, 0.3 m from the commanded spot.
Not verified yet (VERIFY): the pitch sign, height above ground while moving (PLANE ALT ABOVE GROUND of a frozen
object), gear / light / engine events on a non-ATC aircraft.

Timing: the connection is opened with a Win32 event the sim signals when it has messages, and subscribed to the
sim's "Frame" event, so `wait()` returns once per rendered frame: the manager sends every pose in step with the
frames (20 Hz from a timer looked jerky in the sim, 2026-10-07). "Pause" stops the clock; the frame's sim rate scales
it.

Creation is asynchronous: `create()` sends the request; the object id arrives with a later message, then the
aircraft is read once (where the sim put it: its height on its gear), released and frozen; poses sent before that
are dropped. Height on the ground: the sim's ground altitude under the aircraft is followed once a second (terrain
under taxiways isn't flat); in the air the height is the ground at liftoff + the pose's height.
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from atc.own.motion import Pose

RECV_ID_EXCEPTION = 1
RECV_ID_EVENT = 4
RECV_ID_EVENT_FRAME = 7
RECV_ID_SIMOBJECT_DATA = 8
RECV_ID_ASSIGNED_OBJECT_ID = 12
DATATYPE_FLOAT64 = 4
UNUSED = 0xFFFFFFFF
PERIOD_ONCE = 1
PERIOD_SECOND = 4
GROUP_PRIORITY_HIGHEST = 1
EVENT_FLAG_GROUPID_IS_PRIORITY = 0x10

DEF_POS = 3101  # lat, lon, alt, pitch, bank, heading
DEF_AGL = 3102  # PLANE ALT ABOVE GROUND, PLANE ALTITUDE
REQ_CREATE0 = 10_000  # + n per aircraft
REQ_PLACED0 = 20_000
REQ_AGL0 = 30_000
REQ_MISC = 40_000
EVENTS = ["FREEZE_LATITUDE_LONGITUDE_SET", "FREEZE_ALTITUDE_SET", "FREEZE_ATTITUDE_SET", "GEAR_UP", "GEAR_DOWN",
          "BEACON_LIGHTS_SET", "NAV_LIGHTS_SET", "LANDING_LIGHTS_SET", "STROBES_SET", "TAXI_LIGHTS_SET",
          "ENGINE_AUTO_START"]
EV0 = 5100
SYS_FRAME, SYS_PAUSE = 5200, 5201
GROUND_FOLLOW_FTPS = 2.0  # the height on the ground moves toward the measured terrain this fast (no bumps)
POS_FMT = "<6d"
HEADER = 12 + 28  # SIMCONNECT_RECV + the 7 DWORDs of SIMCONNECT_RECV_SIMOBJECT_DATA
PITCH_SIGN = float(os.environ.get("ATC_OWN_PITCH_SIGN", "-1"))  # VERIFY: the sim's PLANE PITCH DEGREES is + nose down


class InitPosition(ctypes.Structure):  # SIMCONNECT_DATA_INITPOSITION
    _pack_ = 1
    _fields_ = [("Latitude", ctypes.c_double), ("Longitude", ctypes.c_double), ("Altitude", ctypes.c_double),
                ("Pitch", ctypes.c_double), ("Bank", ctypes.c_double), ("Heading", ctypes.c_double),
                ("OnGround", ctypes.c_uint32), ("Airspeed", ctypes.c_uint32)]


@dataclass
class _Obj:
    key: str
    n: int
    object_id: int | None = None
    ready: bool = False  # placed, released, frozen: poses go to the sim
    created_at: float = 0.0
    gear_agl_ft: float | None = None  # height of the reference point on its gear (as the sim placed it)
    ground_ft: float | None = None  # ground elevation under it as used now (eased toward ground_target_ft)
    ground_target_ft: float | None = None  # last measured
    liftoff_ground_ft: float | None = None
    last_alt_ft: float = 0.0
    asked: bool = False  # "where did you put it" sent
    last_sent: float | None = None
    lights: dict = field(default_factory=dict)
    gear_down: bool = True
    type_icao: str | None = None
    airborne_spawn: bool = False  # created in the air: its gear height comes from GEAR_FILE, not from the sim
    vehicle: bool = False  # a ground vehicle (tug): no AI to release


# Height of the reference point above the ground on the gear, per type: learned from every aircraft created on the
# ground (the sim places those), used for the ones created in the air (arrivals). Until learned, by size.
GEAR_FILE = Path("data/gear_heights.json")


def _gear_guess(type_icao: str | None) -> float:
    from atc.own.motion import perf

    span = perf(type_icao).wingspan_m
    return 15.0 if span >= 55 else 10.0 if span >= 30 else 7.0 if span >= 20 else 4.0


def _load_gear() -> dict[str, float]:
    import json

    try:
        return {k: float(v) for k, v in json.loads(GEAR_FILE.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        return {}


class FakeInjector:
    """No sim: remembers what would have been sent (tests, the fake-sim REPL)."""

    needs_title = False

    def __init__(self) -> None:
        self.poses: dict[str, Pose] = {}
        self.titles: dict[str, str] = {}
        self.removed: list[str] = []

    def create(self, key: str, title: str, lat: float, lon: float, heading: float, ground_ft: float,
               agl_ft: float = 0.0, type_icao: str | None = None) -> None:
        self.titles[key] = title
        self.poses[key] = Pose(lat, lon, agl_ft, heading)

    def create_vehicle(self, key: str, title: str, lat: float, lon: float, heading: float, ground_ft: float) -> None:
        self.create(key, title, lat, lon, heading, ground_ft)

    def update(self, key: str, pose: Pose) -> None:
        if key in self.poses:
            self.poses[key] = pose

    def lights(self, key: str, **on: bool) -> None:
        pass

    def remove(self, key: str) -> None:
        self.poses.pop(key, None)
        self.removed.append(key)

    def pump(self) -> None:
        pass

    paused = False
    sim_rate = 1.0

    def wait(self, timeout_s: float) -> bool:
        """No sim frames here: just the timer."""
        time.sleep(timeout_s)
        return False

    def close(self) -> None:
        for key in list(self.poses):
            self.remove(key)


class SimInjector:
    needs_title = True  # the sim creates nothing without a model title

    def __init__(self, dll: Path | None = None) -> None:
        from atc.sim.ai_traffic import default_dll_path

        self.d = ctypes.WinDLL(str(dll or default_dll_path()))
        self._prototypes()
        k32 = ctypes.windll.kernel32
        k32.CreateEventW.restype = ctypes.c_void_p
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._k32 = k32
        self.event = k32.CreateEventW(None, False, False, None)  # auto-reset: signalled when the sim has messages
        self.h = ctypes.c_void_p()
        self._ok(self.d.SimConnect_Open(ctypes.byref(self.h), b"atc-ia-own-traffic", None, 0, self.event, 0), "Open")
        self.paused = False
        self.sim_rate = 1.0
        self._framed = False
        self._ok(self.d.SimConnect_SubscribeToSystemEvent(self.h, SYS_FRAME, b"Frame"), "Frame event")
        self._ok(self.d.SimConnect_SubscribeToSystemEvent(self.h, SYS_PAUSE, b"Pause"), "Pause event")
        for name, units in (("PLANE LATITUDE", "degrees"), ("PLANE LONGITUDE", "degrees"), ("PLANE ALTITUDE", "feet"),
                            ("PLANE PITCH DEGREES", "degrees"), ("PLANE BANK DEGREES", "degrees"),
                            ("PLANE HEADING DEGREES TRUE", "degrees")):
            self._ok(self.d.SimConnect_AddToDataDefinition(self.h, DEF_POS, name.encode(), units.encode(),
                                                           DATATYPE_FLOAT64, 0.0, UNUSED), name)
        for name in ("PLANE ALT ABOVE GROUND", "PLANE ALTITUDE"):
            self._ok(self.d.SimConnect_AddToDataDefinition(self.h, DEF_AGL, name.encode(), b"feet", DATATYPE_FLOAT64,
                                                           0.0, UNUSED), name)
        for i, ev in enumerate(EVENTS):
            self._ok(self.d.SimConnect_MapClientEventToSimEvent(self.h, EV0 + i, ev.encode()), ev)
        self.objs: dict[str, _Obj] = {}
        self.by_req: dict[int, _Obj] = {}
        self.n = 0
        self.gear = _load_gear()

    def _prototypes(self) -> None:
        d, H, U, P = self.d, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p
        sigs = {
            "Open": [ctypes.POINTER(H), P, H, U, H, U], "Close": [H],
            "AddToDataDefinition": [H, U, P, P, U, ctypes.c_float, U],
            "RequestDataOnSimObject": [H, U, U, U, U, U, U, U, U],
            "GetNextDispatch": [H, ctypes.POINTER(H), ctypes.POINTER(U)],
            "AICreateNonATCAircraft": [H, P, P, InitPosition, U], "AIReleaseControl": [H, U, U],
            "AIRemoveObject": [H, U, U], "SetDataOnSimObject": [H, U, U, U, U, U, H],
            "MapClientEventToSimEvent": [H, U, P], "TransmitClientEvent": [H, U, U, U, U, U],
            "SubscribeToSystemEvent": [H, U, P], "AICreateSimulatedObject": [H, P, InitPosition, U],
        }
        for fn, args in sigs.items():
            f = getattr(d, f"SimConnect_{fn}")
            f.argtypes, f.restype = args, ctypes.c_long

    @staticmethod
    def _ok(hr: int, what: str) -> None:
        if hr != 0:
            raise RuntimeError(f"SimConnect {what} failed: 0x{hr & 0xFFFFFFFF:08x}")

    def _event(self, obj: _Obj, name: str, value: int = 0) -> None:
        if obj.object_id is None:
            return
        self.d.SimConnect_TransmitClientEvent(self.h, obj.object_id, EV0 + EVENTS.index(name), value,
                                              GROUP_PRIORITY_HIGHEST, EVENT_FLAG_GROUPID_IS_PRIORITY)

    # --- the manager's calls ---------------------------------------------------------------------------------
    def create(self, key: str, title: str, lat: float, lon: float, heading: float, ground_ft: float,
               agl_ft: float = 0.0, type_icao: str | None = None) -> None:
        """On the ground (agl_ft 0: the sim places it, its gear height is learned) or in the air at agl_ft above
        `ground_ft` (the airport's elevation: the approach's heights are above the runway)."""
        self.n += 1
        airborne = agl_ft > 0.0
        obj = _Obj(key, self.n, created_at=time.monotonic(), type_icao=type_icao, airborne_spawn=airborne)
        if airborne:
            obj.gear_agl_ft = self.gear.get((type_icao or "").upper(), _gear_guess(type_icao))
            obj.ground_ft = obj.ground_target_ft = ground_ft
        self.objs[key] = obj
        self.by_req[REQ_CREATE0 + obj.n] = obj
        self.by_req[REQ_PLACED0 + obj.n] = obj
        self.by_req[REQ_AGL0 + obj.n] = obj
        alt = ground_ft + agl_ft + (obj.gear_agl_ft or 0.0)
        pos = InitPosition(lat, lon, alt, 0.0, 0.0, heading, 0 if airborne else 1, 150 if airborne else 0)
        tail = key[:12].encode()
        self._ok(self.d.SimConnect_AICreateNonATCAircraft(self.h, title.encode(), tail, pos, REQ_CREATE0 + obj.n),
                 f"AICreateNonATCAircraft {title}")

    def create_vehicle(self, key: str, title: str, lat: float, lon: float, heading: float, ground_ft: float) -> None:
        """A ground vehicle (the pushback tug): a simulated object, moved like the aircraft. VERIFY: GSX's tug
        titles create this way; freeze events on a vehicle."""
        self.n += 1
        obj = _Obj(key, self.n, created_at=time.monotonic(), vehicle=True)
        self.objs[key] = obj
        for base in (REQ_CREATE0, REQ_PLACED0, REQ_AGL0):
            self.by_req[base + obj.n] = obj
        pos = InitPosition(lat, lon, ground_ft, 0.0, 0.0, heading, 1, 0)
        hr = self.d.SimConnect_AICreateSimulatedObject(self.h, title.encode(), pos, REQ_CREATE0 + obj.n)
        if hr != 0:
            print(f"[own traffic: no tug ({title}): 0x{hr & 0xFFFFFFFF:08x}]")
            self.objs.pop(key, None)

    def update(self, key: str, pose: Pose) -> None:
        obj = self.objs.get(key)
        if obj is None or not obj.ready or obj.gear_agl_ft is None or obj.ground_ft is None:
            return
        now = time.monotonic()
        if obj.ground_target_ft is not None:  # ease toward the measured terrain: no step once a second
            step = GROUND_FOLLOW_FTPS * min(0.2, max(0.0, now - (obj.last_sent or now)))
            obj.ground_ft += max(-step, min(step, obj.ground_target_ft - obj.ground_ft))
        obj.last_sent = now
        if pose.on_ground:
            obj.liftoff_ground_ft = None
            alt = obj.ground_ft + obj.gear_agl_ft
        else:
            if obj.liftoff_ground_ft is None:
                obj.liftoff_ground_ft = obj.ground_ft
            alt = obj.liftoff_ground_ft + obj.gear_agl_ft + pose.agl_ft
        obj.last_alt_ft = alt
        data = struct.pack(POS_FMT, pose.lat, pose.lon, alt, PITCH_SIGN * pose.pitch_deg, pose.bank_deg,
                           pose.heading_deg % 360.0)
        buf = ctypes.create_string_buffer(data, len(data))
        self.d.SimConnect_SetDataOnSimObject(self.h, DEF_POS, obj.object_id, 0, 0, len(data), buf)
        if pose.gear_down != obj.gear_down:
            obj.gear_down = pose.gear_down
            self._event(obj, "GEAR_DOWN" if pose.gear_down else "GEAR_UP")

    def lights(self, key: str, **on: bool) -> None:
        """beacon=, nav=, landing=, strobe=, taxi=: sent when they change."""
        obj = self.objs.get(key)
        if obj is None or not obj.ready:
            return
        names = {"beacon": "BEACON_LIGHTS_SET", "nav": "NAV_LIGHTS_SET", "landing": "LANDING_LIGHTS_SET",
                 "strobe": "STROBES_SET", "taxi": "TAXI_LIGHTS_SET"}
        for k, v in on.items():
            if obj.lights.get(k) != v and k in names:
                obj.lights[k] = v
                self._event(obj, names[k], int(v))
        if on.get("engines") and not obj.lights.get("engines"):
            obj.lights["engines"] = True
            self._event(obj, "ENGINE_AUTO_START")

    def remove(self, key: str) -> None:
        obj = self.objs.pop(key, None)
        if obj is not None and obj.object_id is not None:
            self.d.SimConnect_AIRemoveObject(self.h, obj.object_id, REQ_MISC)

    def pump(self) -> None:
        """Read what the sim sent: new object ids, where they were placed, heights above ground, exceptions."""
        now = time.monotonic()
        while True:
            p, n = ctypes.c_void_p(), ctypes.c_uint32()
            if self.d.SimConnect_GetNextDispatch(self.h, ctypes.byref(p), ctypes.byref(n)) != 0:
                break
            msg = ctypes.string_at(p.value, n.value)
            recv_id = struct.unpack_from("<I", msg, 8)[0]
            if recv_id == RECV_ID_EXCEPTION:
                exc, send_id, index = struct.unpack_from("<3I", msg, 12)
                print(f"[own traffic: SimConnect exception {exc} (send {send_id}, parameter {index})]")
            elif recv_id == RECV_ID_ASSIGNED_OBJECT_ID:
                req, oid = struct.unpack_from("<2I", msg, 12)
                obj = self.by_req.get(req)
                if obj is not None and obj.key in self.objs:
                    obj.object_id = oid
                elif oid:  # removed before the sim answered: don't leave it standing there
                    self.d.SimConnect_AIRemoveObject(self.h, oid, REQ_MISC)
            elif recv_id == RECV_ID_EVENT_FRAME:  # SIMCONNECT_RECV_EVENT_FRAME: ..., fFrameRate, fSimSpeed
                self._framed = True
                rate = struct.unpack_from("<f", msg, 28)[0]
                self.sim_rate = rate if 0.0 < rate <= 16.0 else 1.0
            elif recv_id == RECV_ID_EVENT:  # SIMCONNECT_RECV_EVENT: header, uGroupID, uEventID, dwData
                _group, event_id, data = struct.unpack_from("<3I", msg, 12)
                if event_id == SYS_PAUSE:
                    self.paused = bool(data)  # VERIFY: MSFS 2024 sends Pause with 1 / 0
            elif recv_id == RECV_ID_SIMOBJECT_DATA:
                req = struct.unpack_from("<I", msg, 12)[0]
                obj = self.by_req.get(req)
                if obj is None or obj.key not in self.objs:
                    continue
                agl, alt = struct.unpack_from("<2d", msg, HEADER)
                if req == REQ_PLACED0 + obj.n and not obj.ready:
                    self._placed(obj, agl, alt)
                elif obj.ready and obj.liftoff_ground_ft is None:
                    obj.ground_target_ft = alt - agl  # the terrain under it, eased into in update()
        for obj in self.objs.values():  # placed: ask where (once), a second after the object exists
            if obj.object_id is not None and not obj.asked and now - obj.created_at >= 1.5:
                obj.asked = True
                self.d.SimConnect_RequestDataOnSimObject(self.h, REQ_PLACED0 + obj.n, DEF_AGL, obj.object_id,
                                                         PERIOD_ONCE, 0, 0, 0, 0)

    def wait(self, timeout_s: float) -> bool:
        """Until the sim has something for us (a frame, at most `timeout_s`), then read it all. True = a new frame:
        time to send the poses."""
        self._k32.WaitForSingleObject(self.event, max(1, int(timeout_s * 1000)))
        self._framed = False
        self.pump()
        return self._framed

    def _placed(self, obj: _Obj, agl: float, alt: float) -> None:
        if not obj.airborne_spawn and obj.vehicle:
            obj.gear_agl_ft, obj.ground_ft = max(0.0, agl), alt - agl
            obj.ground_target_ft = obj.ground_ft
        elif not obj.airborne_spawn:  # the sim put it on its gear: learn how high that is for this type
            obj.gear_agl_ft = max(0.0, agl)
            obj.ground_ft = obj.ground_target_ft = alt - agl
            t = (obj.type_icao or "").upper()
            if t and 0.5 < agl < 40 and abs(self.gear.get(t, -1.0) - agl) > 0.2:
                self.gear[t] = round(agl, 2)
                self._save_gear()
        if not obj.vehicle:
            self.d.SimConnect_AIReleaseControl(self.h, obj.object_id, REQ_MISC)
        for ev in ("FREEZE_LATITUDE_LONGITUDE_SET", "FREEZE_ALTITUDE_SET", "FREEZE_ATTITUDE_SET"):
            self._event(obj, ev, 1)
        self.d.SimConnect_RequestDataOnSimObject(self.h, REQ_AGL0 + obj.n, DEF_AGL, obj.object_id, PERIOD_SECOND,
                                                 0, 0, 0, 0)
        obj.ready = True

    def _save_gear(self) -> None:
        import json

        try:
            GEAR_FILE.parent.mkdir(parents=True, exist_ok=True)
            GEAR_FILE.write_text(json.dumps(self.gear, indent=1, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

    def close(self) -> None:
        for key in list(self.objs):
            self.remove(key)
        self.d.SimConnect_Close(self.h)
        self._k32.CloseHandle(ctypes.c_void_p(self.event))
