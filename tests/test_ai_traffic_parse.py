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


def test_one_simconnect_ai_request_per_tick(monkeypatch):
    """The watcher asks for traffic several times per tick: only one SimConnect request goes out (cache 0.8 s)."""
    import atc.sim.simconnect_source as scs
    from atc.models import OwnState
    from atc.sim.ai_traffic import AiRecord

    class Reader:
        calls = 0

        def read(self, radius_m):
            Reader.calls += 1
            return [AiRecord(7, -34.56, -58.41, 500.0, 140.0, 310.0, False, "LV-XYZ")]

    src = object.__new__(scs.SimConnectSource)
    src._ai, src._ai_failed, src._ai_cache = Reader(), False, None
    src._own_object_id, src._own_record = None, None
    monkeypatch.setattr(src, "own", lambda: OwnState(-34.6, -58.5, 3000, 2980, 150, 90, False, 118.85))
    clock = [100.0]
    monkeypatch.setattr(scs.time, "monotonic", lambda: clock[0])
    for _ in range(6):
        assert [t.callsign for t in src.traffic(-34.56, -58.41, 15)] == ["LV-XYZ"]
    assert Reader.calls == 1
    clock[0] += 1.0  # next tick
    src.traffic(-34.56, -58.41, 15)
    assert Reader.calls == 2
