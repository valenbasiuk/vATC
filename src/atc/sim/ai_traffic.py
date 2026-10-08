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

DATATYPE_STRING8 = 5
DATATYPE_STRING64 = 7

DEFINE_ID = 1001  # basic layout (verified on Valen's PC)
DEFINE_FULL = 1002  # + type/airline/flight number. VERIFY: falls back to DEFINE_ID if the sim rejects it
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
EXTRA_FIELDS = [
    ("ATC MODEL", None, DATATYPE_STRING32),  # e.g. "A320" or "TT:ATCCOM.AC_MODEL_A320.0.text"
    ("ATC AIRLINE", None, DATATYPE_STRING64),
    ("ATC FLIGHT NUMBER", None, DATATYPE_STRING8),
]
FULL_FMT = RECORD_FMT + "32s64s8s"
FULL_SIZE = struct.calcsize(FULL_FMT)  # 180

# SIMCONNECT_RECV_EXCEPTION: header, then dwException, dwSendID, dwIndex (SimConnect.h)
EXCEPTION_FMT = "<3I"
EXCEPTION_NAMES = {
    1: "ERROR", 2: "SIZE_MISMATCH", 3: "UNRECOGNIZED_ID", 4: "UNOPENED", 5: "VERSION_MISMATCH",
    6: "TOO_MANY_GROUPS", 7: "NAME_UNRECOGNIZED", 8: "TOO_MANY_EVENT_NAMES", 9: "EVENT_ID_DUPLICATE",
    10: "TOO_MANY_MAPS", 11: "TOO_MANY_OBJECTS", 12: "TOO_MANY_REQUESTS", 15: "INVALID_DATA_TYPE",
    16: "INVALID_DATA_SIZE", 17: "DATA_ERROR", 18: "INVALID_ARRAY", 19: "CREATE_OBJECT_FAILED",
    21: "OPERATION_INVALID_FOR_OBJECT_TYPE", 22: "ILLEGAL_OPERATION",
}  # VERIFY against the SDK header; only used for the log line

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
    model: str = ""
    airline: str = ""
    flight_number: str = ""


def _text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()


def unpack_record(object_id: int, data: bytes, full: bool = False) -> AiRecord:
    if full:
        lat, lon, alt, gs, hdg, ground, raw_id, model, airline, number = struct.unpack_from(FULL_FMT, data, 0)
        return AiRecord(object_id, lat, lon, alt, gs, hdg, bool(ground), _text(raw_id),
                        _text(model), _text(airline), _text(number))
    lat, lon, alt, gs, hdg, ground, raw_id = struct.unpack_from(RECORD_FMT, data, 0)
    return AiRecord(object_id, lat, lon, alt, gs, hdg, bool(ground), _text(raw_id))


def parse_message(msg: bytes) -> tuple[int, dict | None]:
    """Returns (recv_id, info). info is set for SIMOBJECT_DATA(_BYTYPE) messages, and for exceptions
    ({"exception": code, "send_id": ..., "index": ...})."""
    _size, _version, recv_id = struct.unpack_from(HEADER_FMT, msg, 0)
    if recv_id == RECV_ID_EXCEPTION and len(msg) >= 24:
        code, send_id, index = struct.unpack_from(EXCEPTION_FMT, msg, 12)
        return recv_id, {"exception": code, "send_id": send_id, "index": index}
    if recv_id not in (RECV_ID_SIMOBJECT_DATA, RECV_ID_SIMOBJECT_DATA_BYTYPE):
        return recv_id, None
    request, obj, define, _flags, entry, outof, _count = struct.unpack_from(OBJ_HEADER_FMT, msg, 12)
    info = {"request": request, "object_id": obj, "entry": entry, "outof": outof, "define": define}
    full = define == DEFINE_FULL
    if outof > 0 and len(msg) >= DATA_OFFSET + (FULL_SIZE if full else RECORD_SIZE):
        info["record"] = unpack_record(obj, msg[DATA_OFFSET:], full)
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


class ReadError(RuntimeError):
    """A read that got no data: SimConnect answered with an exception (code kept for the log), or nothing at all."""

    def __init__(self, text: str, code: int | None = None) -> None:
        super().__init__(text)
        self.code = code


def exception_text(code: int) -> str:
    return f"SimConnect exception {code} ({EXCEPTION_NAMES.get(code, 'unknown')})"


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
        for name, units, dtype in FIELDS:
            hr = self._dll.SimConnect_AddToDataDefinition(
                self._h, DEFINE_ID, name.encode(), units.encode() if units else None, dtype, 0.0, UNUSED
            )
            if hr != 0:
                raise RuntimeError(f"AddToDataDefinition failed for {name!r}: 0x{hr & 0xFFFFFFFF:08x}")
        self._define = DEFINE_FULL
        self._full_ok = False  # the full layout has returned data once: never drop it over a later hiccup
        self._request = REQUEST_ID
        for name, units, dtype in FIELDS + EXTRA_FIELDS:
            hr = self._dll.SimConnect_AddToDataDefinition(
                self._h, DEFINE_FULL, name.encode(), units.encode() if units else None, dtype, 0.0, UNUSED
            )
            if hr != 0:
                self._define = DEFINE_ID
                break

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
        """AI aircraft within radius_m of the USER aircraft. May include the user's own aircraft.
        If the sim rejects the type/airline fields on the very first reads (a field name it doesn't know),
        drop to the basic (verified) layout and retry once. Once the full layout has worked, a failed read is
        just a failed read (real sim 2026-10-08: one exception dropped the type fields and then the whole reader)."""
        try:
            recs = self._read(radius_m, timeout_s)
        except ReadError as exc:
            if self._define != DEFINE_FULL or self._full_ok or exc.code not in (None, 7, 15, 16):
                raise
            self._define = DEFINE_ID
            print(f"[traffic: sim rejected type/airline fields ({exc}), using basic traffic data]", file=sys.stderr)
            return self._read(radius_m, timeout_s)
        if recs and self._define == DEFINE_FULL:
            self._full_ok = True
        return recs

    def _read(self, radius_m: int, timeout_s: float) -> list[AiRecord]:
        # a fresh request id per read: late records of a read that timed out are not mixed into this one
        self._request = REQUEST_ID + (self._request - REQUEST_ID + 1) % 1000
        hr = self._dll.SimConnect_RequestDataOnSimObjectType(
            self._h, self._request, self._define, int(radius_m), SIMOBJECT_TYPE_AIRCRAFT
        )
        if hr != 0:
            raise ReadError(f"RequestDataOnSimObjectType failed: 0x{hr & 0xFFFFFFFF:08x}")
        return self._collect(self._next_message, self._request, timeout_s)

    def _next_message(self) -> bytes | None:
        p = ctypes.c_void_p()
        n = ctypes.c_uint32()
        if self._dll.SimConnect_GetNextDispatch(self._h, ctypes.byref(p), ctypes.byref(n)) != 0:
            return None
        return ctypes.string_at(p.value, n.value)

    @staticmethod
    def _collect(next_message, request: int, timeout_s: float) -> list[AiRecord]:
        """Records of one request. An exception message doesn't end the read (it may be about one object, or a
        leftover of an earlier request): the records that arrive are kept. Only a read with an exception and no
        data at all fails."""
        out: list[AiRecord] = []
        expected: int | None = None
        seen: set[int] = set()
        error: int | None = None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            msg = next_message()
            if msg is None:
                if error is not None and expected is None and time.monotonic() > deadline - timeout_s + 0.3:
                    break  # an exception and no data after 0.3 s: this request failed
                time.sleep(0.01)  # nothing queued yet
                continue
            recv_id, info = parse_message(msg)
            if recv_id == RECV_ID_EXCEPTION:
                error = info["exception"] if info else 0
                continue
            if info is None or info["request"] != request:
                continue
            expected = info["outof"]
            if expected == 0:
                return []
            seen.add(info["entry"])
            if "record" in info:
                out.append(info["record"])
            if len(seen) >= expected or (error is not None and info["entry"] == expected - 1):
                break
        if not out and error is not None:
            raise ReadError(exception_text(error), error)
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
        type=rec.model or None,
        airline=rec.airline or None,
        flight_number=rec.flight_number or None,
    )
