"""Read an airport's taxiway network from MSFS itself (SimConnect facility data). Run with MSFS loaded:

    python tools/probe_taxi.py SABE            # prints what the sim sends, saves airports/msfs/SABE.json

STATUS: UNTESTED. The installed SimConnect Python package predates the facility API, so this uses raw ctypes
(like sim/ai_traffic.py). Field names come from the MSFS SDK docs (Facilities: TAXI_POINT, TAXI_PATH, TAXI_NAME,
TAXI_PARKING). Field sizes and the facility RECV ids are not verified: the probe prints every message id and
every record's type and byte length, so its output tells us what to fix. Bring the output back.

When it works, atc.taxi uses airports/msfs/<ICAO>.json before the OpenStreetMap file: names and geometry then
match the scenery you are flying exactly.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atc.sim.ai_traffic import default_dll_path  # noqa: E402

DEFINE_ID = 3001
REQUEST_ID = 3002
# Order matters: records come back with their fields packed in exactly this order.
DEFINITION = [
    "OPEN AIRPORT", "LATITUDE", "LONGITUDE", "N_TAXI_POINTS", "N_TAXI_PATHS", "N_TAXI_NAMES", "N_TAXI_PARKINGS",
    "OPEN TAXI_POINT", "TYPE", "BIAS_X", "BIAS_Z", "CLOSE TAXI_POINT",
    "OPEN TAXI_PATH", "TYPE", "START", "END", "NAME_INDEX", "CLOSE TAXI_PATH",
    "OPEN TAXI_NAME", "NAME", "CLOSE TAXI_NAME",
    "OPEN TAXI_PARKING", "NAME", "NUMBER", "BIAS_X", "BIAS_Z", "CLOSE TAXI_PARKING",
    "CLOSE AIRPORT",
]
# Expected record layouts (VERIFY): airport 2 doubles + 4 int32; point int32 + 2 float32; path 4 int32;
# name char[32]; parking int32 + uint32 + 2 float32.
LAYOUTS = {"airport": ("<2d4i", 32), "point": ("<i2f", 12), "path": ("<4i", 16), "name": ("<32s", 32),
           "parking": ("<iI2f", 16)}
HEADER = "<3I"
FAC_HEADER = "<7I"  # UserRequestId, UniqueRequestId, ParentUniqueRequestId, Type, IsListItem, ItemIndex, ListSize


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("icao")
    p.add_argument("--dll", type=Path)
    p.add_argument("--out", type=Path, default=Path("airports/msfs"))
    p.add_argument("--timeout", type=float, default=15.0)
    args = p.parse_args(argv)
    if sys.platform != "win32":
        print("needs Windows and a running MSFS")
        return 1

    dll = ctypes.WinDLL(str(args.dll or default_dll_path()))
    for fn in ("Open", "Close", "AddToFacilityDefinition", "RequestFacilityData", "GetNextDispatch"):
        if not hasattr(dll, f"SimConnect_{fn}"):
            print(f"this SimConnect.dll has no SimConnect_{fn}: point --dll at the MSFS 2024 SDK's SimConnect.dll")
            return 1
        getattr(dll, f"SimConnect_{fn}").restype = ctypes.c_long
    dll.SimConnect_Open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p, ctypes.c_void_p,
                                    ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
    dll.SimConnect_AddToFacilityDefinition.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p]
    dll.SimConnect_RequestFacilityData.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                   ctypes.c_char_p, ctypes.c_char_p]
    dll.SimConnect_GetNextDispatch.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                               ctypes.POINTER(ctypes.c_uint32)]
    h = ctypes.c_void_p()
    hr = dll.SimConnect_Open(ctypes.byref(h), b"atc-ia-taxi-probe", None, 0, None, 0)
    if hr != 0:
        print(f"SimConnect_Open failed 0x{hr & 0xFFFFFFFF:08x}: is MSFS running, past the loading screen?")
        return 1
    for field in DEFINITION:
        hr = dll.SimConnect_AddToFacilityDefinition(h, DEFINE_ID, field.encode())
        if hr != 0:
            print(f"AddToFacilityDefinition({field!r}) failed 0x{hr & 0xFFFFFFFF:08x}")
    hr = dll.SimConnect_RequestFacilityData(h, DEFINE_ID, REQUEST_ID, args.icao.upper().encode(), b"")
    print(f"RequestFacilityData({args.icao.upper()}) -> 0x{hr & 0xFFFFFFFF:08x}")

    seen_ids: dict[int, int] = {}
    records: list[dict] = []
    deadline = time.monotonic() + args.timeout
    done = False
    while time.monotonic() < deadline and not done:
        pp, n = ctypes.c_void_p(), ctypes.c_uint32()
        if dll.SimConnect_GetNextDispatch(h, ctypes.byref(pp), ctypes.byref(n)) != 0:
            time.sleep(0.01)
            continue
        msg = ctypes.string_at(pp.value, n.value)
        size, _ver, recv_id = struct.unpack_from(HEADER, msg, 0)
        seen_ids[recv_id] = seen_ids.get(recv_id, 0) + 1
        if recv_id == 1:  # EXCEPTION: dwException, dwSendID, dwIndex
            exc, send_id, index = struct.unpack_from("<3I", msg, 12)
            print(f"EXCEPTION {exc} (send id {send_id}, index {index}) - a field name or type is wrong")
            continue
        if len(msg) == 16 and struct.unpack_from("<I", msg, 12)[0] == REQUEST_ID:
            print(f"recv id {recv_id}: looks like FACILITY_DATA_END")
            done = True
            continue
        if len(msg) >= 40 and struct.unpack_from("<I", msg, 12)[0] == REQUEST_ID:
            user, uniq, parent, ftype, is_list, idx, list_size = struct.unpack_from(FAC_HEADER, msg, 12)
            records.append({"recv_id": recv_id, "type": ftype, "index": idx, "list_size": list_size,
                            "data": msg[40:].hex()})
    dll.SimConnect_Close(h)

    print(f"message ids seen: {seen_ids}")
    by_type: dict[int, list[dict]] = {}
    for r in records:
        by_type.setdefault(r["type"], []).append(r)
    for t, rs in sorted(by_type.items()):
        lengths = sorted({len(r["data"]) // 2 for r in rs})
        print(f"record type {t}: {len(rs)} records, data bytes {lengths}, first {rs[0]['data'][:64]}")
    decoded = decode(records)
    if decoded:
        args.out.mkdir(parents=True, exist_ok=True)
        out = args.out / f"{args.icao.upper()}.json"
        out.write_text(json.dumps(decoded, indent=1), encoding="utf-8")
        names = sorted({n for n in decoded["names"] if n})
        print(f"wrote {out}: {len(decoded['points'])} points, {len(decoded['paths'])} paths, names {names[:30]}, "
              f"{len(decoded['parkings'])} parkings")
    else:
        print("could not decode the records with the expected layouts: send this whole output back")
    return 0


def decode(records: list[dict]) -> dict | None:
    """Match records to the expected layouts by byte length (the type ids are not verified either)."""
    out = {"airport": None, "points": [], "paths": [], "names": [], "parkings": []}
    kinds = {}
    for r in records:
        n = len(r["data"]) // 2
        if r["type"] not in kinds:
            if out["airport"] is None and n == LAYOUTS["airport"][1]:
                kinds[r["type"]] = "airport"
            else:
                kinds[r["type"]] = next((k for k in ("point", "path", "name", "parking")
                                         if LAYOUTS[k][1] == n and k not in kinds.values()), None)
        kind = kinds[r["type"]]
        if kind is None:
            continue
        vals = struct.unpack(LAYOUTS[kind][0], bytes.fromhex(r["data"])[:LAYOUTS[kind][1]])
        if kind == "airport":
            out["airport"] = {"lat": vals[0], "lon": vals[1], "counts": vals[2:]}
        elif kind == "point":
            out["points"].append({"type": vals[0], "bias_x": vals[1], "bias_z": vals[2]})
        elif kind == "path":
            out["paths"].append({"type": vals[0], "start": vals[1], "end": vals[2], "name_index": vals[3]})
        elif kind == "name":
            out["names"].append(vals[0].split(b"\0", 1)[0].decode("ascii", "replace").strip())
        else:
            out["parkings"].append({"name": vals[0], "number": vals[1], "bias_x": vals[2], "bias_z": vals[3]})
    if out["airport"] is None or not out["points"] or not out["paths"]:
        return None
    # positions: BIAS_X east / BIAS_Z north, metres from the airport reference point (VERIFY)
    lat0, lon0 = out["airport"]["lat"], out["airport"]["lon"]
    for pt in out["points"] + out["parkings"]:
        pt["lat"] = lat0 + pt["bias_z"] / 111_320.0
        pt["lon"] = lon0 + pt["bias_x"] / (111_320.0 * math.cos(math.radians(lat0)))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
