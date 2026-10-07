"""Own traffic, step 0 on the PC: can this app put an aircraft in the sim and move it itself?

    python tools/probe_inject.py                 # spawns a model of a sim AI near you (else the FSLTL Flybondi 738)
    python tools/probe_inject.py --title "FSLTL_FAIB_B738_FBZ-FlyBondi"   # an exact title (FSLTL / FS Traffic)
    python tools/probe_inject.py --list          # only list the model titles of the aircraft around you

Stand still on an apron with some open space ahead (engines off is fine). Run it twice: once with a model copied
from a sim AI (AI traffic on, Low), once with --title of an FSLTL model (that is what our own traffic will use).
What it does:
  1. creates the aircraft 120 m ahead of you, on the ground, facing you;
  2. takes it away from the sim's AI and freezes it (the sim must stop moving it);
  3. slides it 60 m to your right over 10 s (20 updates a second), turning it 90 degrees;
  4. waits 5 s, removes it.
Watch it outside and copy the whole terminal output into the IDE session. What matters:
  - does it appear (and where: ahead of you, on the ground, not sunk or floating)?
  - does it move smoothly, or jump / shake / fall back to where it was?
  - does it disappear at the end?
If step 1 prints an exception, try another --title from the list.

STATUS: UNTESTED. SimConnect calls from the MSFS SDK (public C API): SimConnect_AICreateNonATCAircraft,
SimConnect_AIReleaseControl, SimConnect_AIRemoveObject, SimConnect_SetDataOnSimObject,
SimConnect_MapClientEventToSimEvent, SimConnect_TransmitClientEvent, SimConnect_RequestDataOnSimObject.
This is how VATSIM clients (vPilot) show other pilots: create a non-ATC aircraft, release it from the sim's AI,
freeze it, then set its position every frame.
"""

from __future__ import annotations

import argparse
import ctypes
import math
import struct
import sys
import time
from pathlib import Path

from atc.sim.ai_traffic import (DATATYPE_FLOAT64, HEADER_FMT, OBJ_HEADER_FMT, RECV_ID_EXCEPTION,
                                RECV_ID_SIMOBJECT_DATA, RECV_ID_SIMOBJECT_DATA_BYTYPE, SIMOBJECT_TYPE_AIRCRAFT, UNUSED,
                                default_dll_path)

RECV_ID_ASSIGNED_OBJECT_ID = 12
DATATYPE_STRING256 = 9
OBJECT_ID_USER = 0
PERIOD_ONCE = 1
GROUP_PRIORITY_HIGHEST = 1
EVENT_FLAG_GROUPID_IS_PRIORITY = 0x10

DEF_POS = 3001  # lat, lon, alt, pitch, bank, heading (what we read and what we set)
DEF_TITLE = 3002
REQ_TITLES, REQ_USER, REQ_CREATE, REQ_OBJ, REQ_RELEASE, REQ_REMOVE = 4001, 4002, 4003, 4004, 4005, 4006
EV_FREEZE_LATLON, EV_FREEZE_ALT, EV_FREEZE_ATT = 5001, 5002, 5003

POS_FIELDS = [("PLANE LATITUDE", "degrees"), ("PLANE LONGITUDE", "degrees"), ("PLANE ALTITUDE", "feet"),
              ("PLANE PITCH DEGREES", "degrees"), ("PLANE BANK DEGREES", "degrees"),
              ("PLANE HEADING DEGREES TRUE", "degrees")]
POS_FMT = "<6d"
DATA_OFFSET = struct.calcsize(HEADER_FMT) + struct.calcsize(OBJ_HEADER_FMT)  # 40

M_PER_DEG_LAT = 111_320.0
FSLTL_DEFAULT = "FSLTL_FAIB_B738_FBZ-FlyBondi"  # in Valen's fsltl-traffic-base (used when no sim AI is around)


class InitPosition(ctypes.Structure):  # SIMCONNECT_DATA_INITPOSITION (SimConnect.h packs its structs to 1 byte)
    _pack_ = 1
    _fields_ = [("Latitude", ctypes.c_double), ("Longitude", ctypes.c_double), ("Altitude", ctypes.c_double),
                ("Pitch", ctypes.c_double), ("Bank", ctypes.c_double), ("Heading", ctypes.c_double),
                ("OnGround", ctypes.c_uint32), ("Airspeed", ctypes.c_uint32)]


def _text256(data: bytes) -> str:
    """A STRING256 field: text up to the first NUL."""
    return data[:256].split(bytes(1), 1)[0].decode("utf-8", "ignore").strip()


def moved(lat: float, lon: float, heading_deg: float, metres: float) -> tuple[float, float]:
    h = math.radians(heading_deg)
    return (lat + metres * math.cos(h) / M_PER_DEG_LAT,
            lon + metres * math.sin(h) / (M_PER_DEG_LAT * math.cos(math.radians(lat))))


class Sim:
    def __init__(self, dll: Path | None) -> None:
        self.d = ctypes.WinDLL(str(dll or default_dll_path()))
        self._prototypes()
        self.h = ctypes.c_void_p()
        self._ok(self.d.SimConnect_Open(ctypes.byref(self.h), b"atc-ia-inject", None, 0, None, 0), "Open")
        for name, units in POS_FIELDS:
            self._ok(self.d.SimConnect_AddToDataDefinition(self.h, DEF_POS, name.encode(), units.encode(),
                                                           DATATYPE_FLOAT64, 0.0, UNUSED), name)
        self._ok(self.d.SimConnect_AddToDataDefinition(self.h, DEF_TITLE, b"TITLE", None, DATATYPE_STRING256, 0.0,
                                                       UNUSED), "TITLE")
        for ev, name in ((EV_FREEZE_LATLON, b"FREEZE_LATITUDE_LONGITUDE_SET"), (EV_FREEZE_ALT, b"FREEZE_ALTITUDE_SET"),
                         (EV_FREEZE_ATT, b"FREEZE_ATTITUDE_SET")):
            self._ok(self.d.SimConnect_MapClientEventToSimEvent(self.h, ev, name), name.decode())

    def _prototypes(self) -> None:
        d, H, U, P = self.d, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p
        sigs = {
            "Open": [ctypes.POINTER(H), P, H, U, H, U],
            "Close": [H],
            "AddToDataDefinition": [H, U, P, P, U, ctypes.c_float, U],
            "RequestDataOnSimObjectType": [H, U, U, U, U],
            "RequestDataOnSimObject": [H, U, U, U, U, U, U, U, U],
            "GetNextDispatch": [H, ctypes.POINTER(H), ctypes.POINTER(U)],
            "AICreateNonATCAircraft": [H, P, P, InitPosition, U],
            "AIReleaseControl": [H, U, U],
            "AIRemoveObject": [H, U, U],
            "SetDataOnSimObject": [H, U, U, U, U, U, H],
            "MapClientEventToSimEvent": [H, U, P],
            "TransmitClientEvent": [H, U, U, U, U, U],
        }
        for fn, args in sigs.items():
            f = getattr(d, f"SimConnect_{fn}")
            f.argtypes, f.restype = args, ctypes.c_long

    @staticmethod
    def _ok(hr: int, what: str) -> None:
        if hr != 0:
            raise RuntimeError(f"{what} failed: 0x{hr & 0xFFFFFFFF:08x}")

    def messages(self, seconds: float):
        """Messages from the sim for up to `seconds`: (recv_id, raw bytes)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            p, n = ctypes.c_void_p(), ctypes.c_uint32()
            if self.d.SimConnect_GetNextDispatch(self.h, ctypes.byref(p), ctypes.byref(n)) != 0:
                time.sleep(0.01)
                continue
            msg = ctypes.string_at(p.value, n.value)
            recv_id = struct.unpack_from(HEADER_FMT, msg, 0)[2]
            if recv_id == RECV_ID_EXCEPTION:
                exc, send_id, index = struct.unpack_from("<3I", msg, 12)
                print(f"  ! SimConnect exception {exc} (send id {send_id}, parameter {index})")
            yield recv_id, msg

    def titles(self, radius_m: int) -> list[str]:
        self._ok(self.d.SimConnect_RequestDataOnSimObjectType(self.h, REQ_TITLES, DEF_TITLE, radius_m,
                                                              SIMOBJECT_TYPE_AIRCRAFT), "request titles")
        out: list[str] = []
        for recv_id, msg in self.messages(2.0):
            if recv_id != RECV_ID_SIMOBJECT_DATA_BYTYPE:
                continue
            req, obj, _, _, _, outof, _ = struct.unpack_from(OBJ_HEADER_FMT, msg, 12)
            if req != REQ_TITLES:
                continue
            if outof == 0:
                break
            out.append(_text256(msg[DATA_OFFSET:]))
            if len(out) >= outof:
                break
        return out

    def _once(self, object_id: int, define: int, req: int) -> bytes | None:
        self._ok(self.d.SimConnect_RequestDataOnSimObject(self.h, req, define, object_id, PERIOD_ONCE, 0, 0, 0, 0),
                 "request data")
        for recv_id, msg in self.messages(2.0):
            if recv_id == RECV_ID_SIMOBJECT_DATA and struct.unpack_from("<I", msg, 12)[0] == req:
                return msg[DATA_OFFSET:]
        return None

    def position(self, object_id: int, req: int) -> tuple[float, ...] | None:
        data = self._once(object_id, DEF_POS, req)
        return struct.unpack_from(POS_FMT, data, 0) if data else None

    def title_of(self, object_id: int, req: int) -> str | None:
        data = self._once(object_id, DEF_TITLE, req)
        return _text256(data) if data else None

    def create(self, title: str, lat: float, lon: float, alt: float, heading: float) -> int | None:
        pos = InitPosition(lat, lon, alt, 0.0, 0.0, heading, 1, 0)
        self._ok(self.d.SimConnect_AICreateNonATCAircraft(self.h, title.encode(), b"IA123", pos, REQ_CREATE),
                 "AICreateNonATCAircraft")
        for recv_id, msg in self.messages(10.0):
            if recv_id == RECV_ID_EXCEPTION:
                return None
            if recv_id == RECV_ID_ASSIGNED_OBJECT_ID:
                req, obj = struct.unpack_from("<2I", msg, 12)
                if req == REQ_CREATE:
                    return obj
        return None

    def take_control(self, obj: int) -> None:
        self._ok(self.d.SimConnect_AIReleaseControl(self.h, obj, REQ_RELEASE), "AIReleaseControl")
        for ev in (EV_FREEZE_LATLON, EV_FREEZE_ALT, EV_FREEZE_ATT):
            self._ok(self.d.SimConnect_TransmitClientEvent(self.h, obj, ev, 1, GROUP_PRIORITY_HIGHEST,
                                                           EVENT_FLAG_GROUPID_IS_PRIORITY), f"freeze {ev}")

    def put(self, obj: int, lat: float, lon: float, alt: float, pitch: float, bank: float, heading: float) -> None:
        buf = ctypes.create_string_buffer(struct.pack(POS_FMT, lat, lon, alt, pitch, bank, heading))
        self._ok(self.d.SimConnect_SetDataOnSimObject(self.h, DEF_POS, obj, 0, 0, ctypes.sizeof(buf) - 1, buf),
                 "SetDataOnSimObject")

    def remove(self, obj: int) -> None:
        self._ok(self.d.SimConnect_AIRemoveObject(self.h, obj, REQ_REMOVE), "AIRemoveObject")

    def close(self) -> None:
        self.d.SimConnect_Close(self.h)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--title", help="model title to spawn (default: the first AI model found near you)")
    p.add_argument("--list", action="store_true", help="only list the AI model titles near you")
    p.add_argument("--dll", help="path to SimConnect.dll")
    args = p.parse_args()
    if sys.platform != "win32":
        sys.exit("Windows + MSFS only")

    sim = Sim(Path(args.dll) if args.dll else None)
    try:
        titles = sim.titles(20_000)
        mine = sim.title_of(OBJECT_ID_USER, REQ_USER)
        print(f"Your aircraft: {mine!r}")
        print(f"Models of the {len(titles)} aircraft within 20 km (yours included):")
        for t in sorted(set(titles)):
            print(f"  {t}")
        if args.list:
            return
        title = args.title or next((t for t in titles if t != mine), None) or FSLTL_DEFAULT
        print(f"Using model {title!r}")
        user = sim.position(OBJECT_ID_USER, REQ_USER)
        if user is None:
            sys.exit("Could not read your position")
        lat, lon, alt, _, _, hdg = user
        print(f"You: {lat:.6f}, {lon:.6f}, {alt:.0f} ft, heading {hdg:.0f}")
        slat, slon = moved(lat, lon, hdg, 120.0)
        facing = (hdg + 180.0) % 360.0
        print(f"1. Creating {title!r} 120 m ahead of you, facing you ...")
        obj = sim.create(title, slat, slon, alt, facing)
        if obj is None:
            sys.exit("   not created (see the exception above). Try another --title from the list.")
        print(f"   created, object id {obj}")
        time.sleep(3.0)  # let the sim place it on the ground
        placed = sim.position(obj, REQ_OBJ)
        print(f"   the sim put it at {placed[0]:.6f}, {placed[1]:.6f}, {placed[2]:.0f} ft, pitch {placed[3]:.1f}, "
              f"heading {placed[5]:.0f}" if placed else "   could not read its position")
        print("2. Taking it from the sim's AI and freezing it ...")
        sim.take_control(obj)
        for _ in sim.messages(0.5):
            pass
        olat, olon, oalt, opitch = (placed[0], placed[1], placed[2], placed[3]) if placed else (slat, slon, alt, 0.0)
        print("3. Sliding it 60 m to your right over 10 s, turning 90 degrees ...")
        right = (hdg + 90.0) % 360.0
        t0 = time.monotonic()
        while (k := min(1.0, (time.monotonic() - t0) / 10.0)) < 1.0:
            plat, plon = moved(olat, olon, right, 60.0 * k)
            sim.put(obj, plat, plon, oalt, opitch, 0.0, (facing - 90.0 * k) % 360.0)
            for _ in sim.messages(0.0):
                pass
            time.sleep(0.05)
        end = sim.position(obj, REQ_OBJ)
        want = moved(olat, olon, right, 60.0)
        if end:
            off_m = math.hypot((end[0] - want[0]) * M_PER_DEG_LAT,
                               (end[1] - want[1]) * M_PER_DEG_LAT * math.cos(math.radians(want[0])))
            print(f"   ended {off_m:.1f} m from where it should be, {end[2]:.0f} ft, heading {end[5]:.0f} "
                  f"(wanted {(facing - 90.0) % 360.0:.0f})")
        print("4. Holding 5 s, then removing it ...")
        time.sleep(5.0)
        sim.remove(obj)
        for _ in sim.messages(1.0):
            pass
        print("Done. Copy this whole output (and say what you saw outside) into the IDE session.")
    finally:
        sim.close()


if __name__ == "__main__":
    main()
