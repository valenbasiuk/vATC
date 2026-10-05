"""AI traffic through raw SimConnect (ctypes). Windows only.

STATUS: UNTESTED against a running sim. The record parsing is unit-tested with synthetic bytes;
the DLL calls are not. Run `python tools/probe_traffic.py` first and read docs/PRE_IDE_CHECKLIST.md.

Why raw ctypes: the `SimConnect` PyPI package only asks for the USER aircraft (its request_data
hardcodes SIMCONNECT_SIMOBJECT_TYPE_USER), so listing AI aircraft needs our own calls. The C API
used here is the public one from the MSFS SDK:
    SimConnect_Open, SimConnect_Close, SimConnect_AddToDataDefinition,
    SimConnect_RequestDataOnSimObjectType, SimConnect_GetNextDispatch
Numeric constants: CONFIRMED against the SimConnect Python package's Enum.py (checked on the installed
package): SIMOBJECT_TYPE_AIRCRAFT=2, DATATYPE INT32=1, FLOAT64=4, STRING8=5, STRING32=6, and the RECV ids
EXCEPTION=1, OPEN=2, QUIT=3, SIMOBJECT_DATA=8, SIMOBJECT_DATA_BYTYPE=9.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from atc.models import Traffic

# --- SimConnect constants (VERIFY against SDK headers) ---
RECV_ID_EXCEPTION = 1
RECV_ID_OPEN = 2
RECV_ID_QUIT = 3
RECV_ID_SIMOBJECT_DATA = 8
RECV_ID_SIMOBJECT_DATA_BYTYPE = 9
DATATYPE_INT32 = 1
DATATYPE_FLOAT64 = 4
DATATYPE_STRING32 = 6
SIMOBJECT_TYPE_AIRCRAFT = 2
UNUSED = 0xFFFFFFFF

DEFINE_ID = 1001
REQUEST_ID = 2001

# Order matters: the sim returns the values packed in exactly this order.
FIELDS = [
    ("PLANE LATITUDE", "degrees", DATATYPE_FLOAT64),
    ("PLANE LONGITUDE", "degrees", DATATYPE_FLOAT64),
    ("PLANE ALTITUDE", "feet", DATATYPE_FLOAT64),
    ("GROUND VELOCITY", "knots", DATATYPE_FLOAT64),
    ("PLANE HEADING DEGREES TRUE", "degrees", DATATYPE_FLOAT64),
    ("SIM ON GROUND", "bool", DATATYPE_INT32),
    ("ATC ID", None, DATATYPE_STRING32),
]
RECORD_FMT = "<5di32s"  # lat, lon, alt, gs, hdg, on_ground, atc_id
RECORD_SIZE = struct.calcsize(RECORD_FMT)  # 76

# SIMCONNECT_RECV (12 bytes) + 7 DWORDs, then the packed data.
HEADER_FMT = "<3I"
OBJ_HEADER_FMT = "<7I"  # request, object, define, flags, entrynumber, outof, definecount
DATA_OFFSET = struct.calcsize(HEADER_FMT) + struct.calcsize(OBJ_HEADER_FMT)  # 40


@dataclass
class AiRecord:
    object_id: int
    lat: float
    lon: float
    alt_ft: float
    gs_kt: float
    heading_deg: float
    on_ground: bool
    atc_id: str


def unpack_record(object_id: int, data: bytes) -> AiRecord:
    lat, lon, alt, gs, hdg, ground, raw_id = struct.unpack_from(RECORD_FMT, data, 0)
    atc_id = raw_id.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
    return AiRecord(object_id, lat, lon, alt, gs, hdg, bool(ground), atc_id)


def parse_message(msg: bytes) -> tuple[int, dict | None]:
    """Returns (recv_id, info). info is set for SIMOBJECT_DATA(_BYTYPE) messages."""
    _size, _version, recv_id = struct.unpack_from(HEADER_FMT, msg, 0)
    if recv_id not in (RECV_ID_SIMOBJECT_DATA, RECV_ID_SIMOBJECT_DATA_BYTYPE):
        return recv_id, None
    request, obj, _define, _flags, entry, outof, _count = struct.unpack_from(OBJ_HEADER_FMT, msg, 12)
    info = {"request": request, "object_id": obj, "entry": entry, "outof": outof}
    if outof > 0 and len(msg) >= DATA_OFFSET + RECORD_SIZE:
        info["record"] = unpack_record(obj, msg[DATA_OFFSET:])
    return recv_id, info


def default_dll_path() -> Path:
    """ATC_SIMCONNECT_DLL if set, else the SimConnect.dll bundled with the PyPI package."""
    env = os.environ.get("ATC_SIMCONNECT_DLL")
    if env:
        return Path(env)
    spec = importlib.util.find_spec("SimConnect")
    if spec and spec.submodule_search_locations:
        for loc in spec.submodule_search_locations:
            cand = Path(loc) / "SimConnect.dll"
            if cand.exists():
                return cand
    raise FileNotFoundError(
        "SimConnect.dll not found. Install the sim extra or set ATC_SIMCONNECT_DLL to the DLL in the "
        "MSFS 2024 SDK (SimConnect SDK/lib)."
    )


class AiTrafficReader:
    def __init__(self, dll_path: Path | None = None, app_name: str = "atc-ia-traffic") -> None:
        if sys.platform != "win32":
            raise RuntimeError("AiTrafficReader needs Windows and a running MSFS")
        self._dll = ctypes.WinDLL(str(dll_path or default_dll_path()))
        self._set_prototypes()
        self._h = ctypes.c_void_p()
        hr = self._dll.SimConnect_Open(ctypes.byref(self._h), app_name.encode(), None, 0, None, 0)
        if hr != 0:
            raise RuntimeError(
                f"SimConnect_Open failed (0x{hr & 0xFFFFFFFF:08x}). Is MSFS running, past the loading screen?"
            )
        for i, (name, units, dtype) in enumerate(FIELDS):
            hr = self._dll.SimConnect_AddToDataDefinition(
                self._h, DEFINE_ID, name.encode(), units.encode() if units else None, dtype, 0.0, UNUSED
            )
            if hr != 0:
                raise RuntimeError(f"AddToDataDefinition failed for {name!r}: 0x{hr & 0xFFFFFFFF:08x}")

    def _set_prototypes(self) -> None:
        d = self._dll
        d.SimConnect_Open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p, ctypes.c_void_p,
                                      ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        d.SimConnect_AddToDataDefinition.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                                                     ctypes.c_char_p, ctypes.c_uint32, ctypes.c_float,
                                                     ctypes.c_uint32]
        d.SimConnect_RequestDataOnSimObjectType.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                            ctypes.c_uint32, ctypes.c_uint32]
        d.SimConnect_GetNextDispatch.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                                 ctypes.POINTER(ctypes.c_uint32)]
        d.SimConnect_Close.argtypes = [ctypes.c_void_p]
        for fn in ("Open", "AddToDataDefinition", "RequestDataOnSimObjectType", "GetNextDispatch", "Close"):
            getattr(d, f"SimConnect_{fn}").restype = ctypes.c_long  # HRESULT

    def read(self, radius_m: int, timeout_s: float = 1.5) -> list[AiRecord]:
        """AI aircraft within radius_m of the USER aircraft. May include the user's own aircraft."""
        hr = self._dll.SimConnect_RequestDataOnSimObjectType(
            self._h, REQUEST_ID, DEFINE_ID, int(radius_m), SIMOBJECT_TYPE_AIRCRAFT
        )
        if hr != 0:
            raise RuntimeError(f"RequestDataOnSimObjectType failed: 0x{hr & 0xFFFFFFFF:08x}")
        out: list[AiRecord] = []
        expected: int | None = None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            p = ctypes.c_void_p()
            n = ctypes.c_uint32()
            if self._dll.SimConnect_GetNextDispatch(self._h, ctypes.byref(p), ctypes.byref(n)) != 0:
                time.sleep(0.01)  # nothing queued yet
                continue
            msg = ctypes.string_at(p.value, n.value)
            recv_id, info = parse_message(msg)
            if recv_id == RECV_ID_EXCEPTION:
                raise RuntimeError("SimConnect reported an exception (check the request/definition)")
            if info is None or info["request"] != REQUEST_ID:
                continue
            expected = info["outof"]
            if expected == 0:
                return []
            if "record" in info:
                out.append(info["record"])
            if len(out) >= expected:
                break
        return out

    def close(self) -> None:
        if self._h:
            self._dll.SimConnect_Close(self._h)


def to_traffic(rec: AiRecord) -> Traffic:
    return Traffic(
        callsign=rec.atc_id or f"AI{rec.object_id}",
        lat=rec.lat,
        lon=rec.lon,
        alt_msl_ft=rec.alt_ft,
        gs_kt=rec.gs_kt,
        heading_deg=rec.heading_deg,
        on_ground=rec.on_ground,
    )
