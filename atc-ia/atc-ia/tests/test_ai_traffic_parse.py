"""The DLL calls can't be tested without MSFS, but the byte parsing can."""

import struct

from atc.sim.ai_traffic import (
    DATA_OFFSET,
    RECORD_FMT,
    RECORD_SIZE,
    RECV_ID_OPEN,
    RECV_ID_SIMOBJECT_DATA_BYTYPE,
    REQUEST_ID,
    parse_message,
    to_traffic,
    unpack_record,
)


def _record_bytes(atc_id=b"N777XY", ground=0):
    return struct.pack(RECORD_FMT, -27.45, -58.76, 1600.0, 80.0, 90.0, ground, atc_id.ljust(32, b"\x00"))


def _message(recv_id, request, obj, entry, outof, payload):
    header = struct.pack("<3I", 40 + len(payload), 4, recv_id)
    obj_header = struct.pack("<7I", request, obj, 1001, 0, entry, outof, 7)
    return header + obj_header + payload


def test_layout_constants():
    assert RECORD_SIZE == 76
    assert DATA_OFFSET == 40


def test_unpack_record():
    r = unpack_record(7, _record_bytes())
    assert (r.lat, r.lon, r.alt_ft, r.gs_kt, r.heading_deg) == (-27.45, -58.76, 1600.0, 80.0, 90.0)
    assert r.atc_id == "N777XY" and r.on_ground is False and r.object_id == 7


def test_parse_message_with_record():
    msg = _message(RECV_ID_SIMOBJECT_DATA_BYTYPE, REQUEST_ID, 42, 1, 2, _record_bytes())
    recv_id, info = parse_message(msg)
    assert recv_id == RECV_ID_SIMOBJECT_DATA_BYTYPE
    assert info["object_id"] == 42 and info["entry"] == 1 and info["outof"] == 2
    assert info["record"].atc_id == "N777XY"


def test_parse_message_zero_objects_has_no_record():
    msg = _message(RECV_ID_SIMOBJECT_DATA_BYTYPE, REQUEST_ID, 0, 0, 0, b"")
    _, info = parse_message(msg)
    assert info["outof"] == 0 and "record" not in info


def test_other_messages_are_ignored():
    header = struct.pack("<3I", 12, 4, RECV_ID_OPEN)
    assert parse_message(header) == (RECV_ID_OPEN, None)


def test_to_traffic_falls_back_to_object_id_when_no_callsign():
    t = to_traffic(unpack_record(9, _record_bytes(atc_id=b"", ground=1)))
    assert t.callsign == "AI9" and t.on_ground is True
