"""A broken SimConnect pipe: the dispatch loop stops (no hot loop), own() keeps the last good state."""

import threading

import atc.sim.simconnect_source as scs
from atc.models import OwnState


class _Dll:
    def __init__(self, err):
        self.err = err
        self.calls = 0

    def CallDispatch(self, *_):
        self.calls += 1
        raise self.err


class _Base:
    def __init__(self, err):
        self.quit = 0
        self.hSimConnect = None
        self.my_dispatch_proc_rd = None
        self.dll = _Dll(err)


def _pipe_error():
    err = OSError("Windows Error 0xc00000b0")
    err.winerror = -1073741648  # what ctypes reports for 0xC00000B0
    return err


def test_pipe_disconnect_stops_the_dispatch_loop(capsys):
    sm = scs._safe_simconnect(_Base)(_pipe_error())
    t = threading.Thread(target=sm._run, daemon=True)
    t.start()
    t.join(2.0)
    assert not t.is_alive() and sm.lost and sm.dll.calls == 1
    err = capsys.readouterr().err
    assert err.count("dispatch error") == 1 and "lost" in err


def test_other_errors_pause_then_give_up(monkeypatch):
    monkeypatch.setattr(scs.time, "sleep", lambda _s: None)
    sm = scs._safe_simconnect(_Base)(OSError("something else"))
    sm._run()
    assert sm.lost and sm.dll.calls == scs.DISPATCH_FAILS_LOST


def test_own_serves_the_last_state_while_the_link_is_down(monkeypatch):
    src = object.__new__(scs.SimConnectSource)
    good = OwnState(lat=-34.56, lon=-58.41, alt_msl_ft=18, alt_agl_ft=0, gs_kt=0, heading_deg=130, on_ground=True,
                    com1_mhz=121.9)
    src._sm = type("Dead", (), {"lost": True})()
    src._last_own = good
    src._reconnect_at = float("inf")  # no reconnect attempt now
    monkeypatch.setattr(src, "_read_own", lambda: (_ for _ in ()).throw(AssertionError("read on a dead link")))
    assert src.own() is good
