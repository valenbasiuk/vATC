"""Real MSFS link through SimConnect.

STATUS: UNVERIFIED DRAFT. It was written without access to a running sim.
Everything marked VERIFY must be checked on Valen's PC first (Phase 0/1).

Own aircraft uses the `SimConnect` PyPI package (Python-SimConnect, the same one OpenSquawk's
Bridge uses). That package only requests the USER aircraft, so nearby AI traffic goes through
`ai_traffic.AiTrafficReader` (raw ctypes, own connection). See docs/OPEN_QUESTIONS.md, item 1.
"""

from __future__ import annotations

import math
import sys
import threading
import time

from atc.geo import distance_nm, heading_diff
from atc.models import OwnState, Traffic

AI_CACHE_S = 0.8  # one AI request per watcher tick (1 s) at most
AI_MIN_REACH_M = int(30 * 1852)  # ask for at least 30 NM so the next callers' smaller radii hit the cache
AI_RETRY_S = (5.0, 15.0, 30.0, 60.0)  # after a failed AI read: wait, then try again (never off for good)
AI_RECONNECT_AFTER = 3  # failed reads in a row -> a new SimConnect connection for the reader
PIPE_DISCONNECTED = 0xC00000B0  # STATUS_PIPE_DISCONNECTED: the sim closed our connection
DISPATCH_FAILS_LOST = 50  # dispatch errors in a row (0.1 s apart) -> the link counts as lost
RECONNECT_S = 5.0  # a lost own-aircraft link: try a new connection this often


def _safe_simconnect(base):
    """Python-SimConnect with a dispatch loop that survives a broken link. The package's own loop prints 'OS error'
    and calls again with no pause when the pipe breaks: a hot loop that printed millions of lines, starved the radio
    threads and froze the launcher (Valen, 2026-10-17, WinError 0xC00000B0). Here: one message, a pause between
    tries, and `lost` set so SimConnectSource opens a new connection."""

    class SafeSimConnect(base):
        lost = False

        def _run(self):
            fails = 0
            while self.quit == 0:
                try:
                    self.dll.CallDispatch(self.hSimConnect, self.my_dispatch_proc_rd, None)
                    fails = 0
                    time.sleep(0.002)
                except OSError as err:
                    fails += 1
                    if fails == 1:
                        print(f"[sim: SimConnect dispatch error ({err})]", file=sys.stderr)
                    code = (getattr(err, "winerror", None) or 0) & 0xFFFFFFFF
                    if code == PIPE_DISCONNECTED or fails >= DISPATCH_FAILS_LOST:
                        self.lost = True
                        print("[sim: the SimConnect link to the sim is lost; reconnecting]", file=sys.stderr)
                        return
                    time.sleep(0.1)

    return SafeSimConnect


class SimConnectSource:
    _ai_lock = threading.Lock()  # (each instance makes its own; this one serves objects built without __init__)

    def __init__(self, callsign: str = "N123AB", library_path: str | None = None) -> None:
        try:
            from SimConnect import AircraftRequests, SimConnect  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install the sim extra: pip install -e .[sim] (64-bit Python)") from exc

        # library_path lets you point at the MSFS 2024 SDK's SimConnect.dll if the bundled one fails.
        self._library_path = library_path
        self._sm_class = _safe_simconnect(SimConnect)
        self._requests_class = AircraftRequests
        self._sm = self._sm_class(library_path=library_path) if library_path else self._sm_class()
        self._reconnect_at = 0.0
        self._last_own: OwnState | None = None  # the last good read, served while the link is down
        self._ai = None
        self._ai_failed = False  # kept for old callers/tests; a failed read now backs off instead (_ai_retry_at)
        self._ai_errors = 0  # failed AI reads in a row
        self._ai_retry_at = 0.0
        self._ai_cache: tuple[float, int, list] | None = None  # (time, radius_m, records) of the last AI read
        self._own_object_id: int | None = None
        self._own_record = None  # the user's aircraft as the AI list shows it: ATC ID, airline, flight number
        # The watcher and our own traffic's reader thread both ask for traffic: one AI request at a time, or the two
        # reads drain each other's records from the reader's single SimConnect queue (empty lists, exceptions)
        self._ai_lock = threading.Lock()
        self._aq = AircraftRequests(self._sm, _time=500)
        self._callsign = callsign

    def _get(self, name: str, default: float = 0.0) -> float:
        v = self._aq.get(name)
        return default if v is None else v

    def _get_opt(self, name: str) -> float | None:
        """Like _get but keeps 'unknown' as None, so a failed read is never spoken as 'calm'."""
        return self._aq.get(name)

    def _link_ok(self) -> bool:
        """False while the own-aircraft connection is down (a new one is tried every RECONNECT_S)."""
        sm = getattr(self, "_sm", None)
        if not getattr(sm, "lost", False):
            return True
        now = time.monotonic()
        if now < getattr(self, "_reconnect_at", 0.0):
            return False
        self._reconnect_at = now + RECONNECT_S
        try:
            new = self._sm_class(library_path=self._library_path) if self._library_path else self._sm_class()
        except Exception as exc:  # noqa: BLE001 - sim not answering yet: try again later
            print(f"[sim: reconnect failed ({exc}); retrying in {RECONNECT_S:.0f} s]", file=sys.stderr)
            return False
        try:
            sm.dll.Close(sm.hSimConnect)
        except Exception:  # noqa: BLE001 - the old pipe is already gone
            pass
        self._sm = new
        self._aq = self._requests_class(new, _time=500)
        print("[sim: SimConnect link back]", file=sys.stderr)
        return True

    def own(self) -> OwnState:
        if not self._link_ok() and getattr(self, "_last_own", None) is not None:
            return self._last_own  # stale but sane (a dead link reads every simvar as 0: lat/lon 0, COM1 0.000)
        st = self._read_own()
        self._last_own = st
        return st

    def _read_own(self) -> OwnState:
        # VERIFY: simvar names with an index (":1") and the units each one returns.
        # In particular PLANE_HEADING_DEGREES_TRUE may come back in radians.
        heading = self._get("PLANE_HEADING_DEGREES_TRUE")
        if abs(heading) <= 2 * math.pi + 1e-6:  # VERIFY: crude radians guard
            heading = math.degrees(heading)
        return OwnState(
            lat=self._get("PLANE_LATITUDE"),
            lon=self._get("PLANE_LONGITUDE"),
            alt_msl_ft=self._get("PLANE_ALTITUDE"),
            alt_agl_ft=self._get("PLANE_ALT_ABOVE_GROUND"),
            gs_kt=self._get("GROUND_VELOCITY"),
            heading_deg=heading % 360,
            on_ground=bool(self._get("SIM_ON_GROUND")),
            com1_mhz=round(self._get("COM_ACTIVE_FREQUENCY:1", 0.0), 3),  # confirmed; 8.33 channel 118.105 reads as 118.105
            squawk=format(int(self._get("TRANSPONDER_CODE:1", 0x1200)), "04x"),  # BCD16: 13669 -> "3565" (confirmed on MSFS 2024)
            callsign=self._callsign,
            # VERIFY units: AMBIENT_WIND_DIRECTION degrees, AMBIENT_WIND_VELOCITY knots,
            # SEA_LEVEL_PRESSURE millibars. Wind is read at the aircraft, not at the airport.
            wind_dir_deg=self._get_opt("AMBIENT_WIND_DIRECTION"),
            wind_kt=self._get_opt("AMBIENT_WIND_VELOCITY"),
            qnh_hpa=self._get_opt("SEA_LEVEL_PRESSURE") or None,
            temp_c=self._get_opt("AMBIENT_TEMPERATURE"),
            com2_mhz=self._com2(),
            zulu_s=self._get_opt("ZULU_TIME"),
            ias_kt=self._get_opt("AIRSPEED_INDICATED"),  # VERIFY: knots through Python-SimConnect
            visibility_m=self._get_opt("AMBIENT_VISIBILITY"),  # VERIFY: metres through Python-SimConnect
        )

    def _com2(self) -> float | None:
        """COM2's frequency if the pilot hears it (VERIFY: 'COM RECEIVE:2' read through Python-SimConnect)."""
        try:
            if not self._get_opt("COM_RECEIVE:2"):
                return None
            mhz = self._get_opt("COM_ACTIVE_FREQUENCY:2")
            return round(mhz, 3) if mhz else None
        except Exception:  # noqa: BLE001 - simvar not in this package version: no COM2
            return None

    def traffic(self, center_lat: float, center_lon: float, radius_nm: float) -> list[Traffic]:
        """AI aircraft near a point. On a failed read: log, back off a few seconds, try again (a new connection after
        a few failures). Meanwhile only our own traffic is listed: 'none reported' for the sim's AI is safe, the
        model is told never to invent traffic."""
        from atc import own as own_traffic

        if self._ai_failed or time.monotonic() < getattr(self, "_ai_retry_at", 0.0):
            return own_traffic.merge([], center_lat, center_lon, radius_nm)
        try:
            from atc.sim.ai_traffic import to_traffic

            own = self.own()
            # SimConnect measures the radius around the USER aircraft, so ask for a bit more and
            # filter around the airport ourselves.
            reach_m = int((radius_nm + distance_nm(own.lat, own.lon, center_lat, center_lon)) * 1852)
            out = []
            for rec in self._ai_records(reach_m):
                # Confirmed: the user's own aircraft IS in the list (KJFK probe). Drop it by object id once
                # known; learn the id from a position match. The own read can be up to 500 ms old
                # (AircraftRequests cache), so the match allows for 1 s of travel at the current speed.
                if rec.object_id == self._own_object_id:
                    self._own_record = rec
                    continue
                if self._own_object_id is None and _is_own(rec, own):
                    self._own_object_id = rec.object_id
                    self._own_record = rec
                    continue
                t = to_traffic(rec)
                if distance_nm(center_lat, center_lon, t.lat, t.lon) <= radius_nm:
                    out.append(t)
            self._ai_errors = 0
            return own_traffic.merge(out, center_lat, center_lon, radius_nm)  # ours, with their real speed
        except Exception as exc:  # noqa: BLE001 - never crash the radio loop over traffic
            self._ai_read_failed(exc)
            return own_traffic.merge([], center_lat, center_lon, radius_nm)

    def _ai_read_failed(self, exc: Exception) -> None:
        n = getattr(self, "_ai_errors", 0) + 1
        self._ai_errors = n
        wait = AI_RETRY_S[min(n, len(AI_RETRY_S)) - 1]
        self._ai_retry_at = time.monotonic() + wait
        self._ai_cache = None
        print(f"[traffic: AI read failed ({exc}), retrying in {wait:.0f} s]", file=sys.stderr)
        if n % AI_RECONNECT_AFTER == 0:
            with self._ai_lock:  # (not while the other thread reads through it)
                if self._ai is not None:
                    try:
                        self._ai.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._ai = None  # the next try opens a new connection

    def _ai_records(self, reach_m: int) -> list:
        """One SimConnect AI request serves every caller for AI_CACHE_S (the watcher asks up to six times per tick:
        takeoff, radar, ground, circuit, arrival, chatter), as long as the cached radius covers the new one."""
        with self._ai_lock:
            now = time.monotonic()
            c = self._ai_cache
            if c is not None and now - c[0] < AI_CACHE_S and c[1] >= reach_m:
                return c[2]
            if self._ai is None:  # first read, or a new connection after failures
                from atc.sim.ai_traffic import AiTrafficReader

                self._ai = AiTrafficReader()
            reach = max(reach_m, AI_MIN_REACH_M)
            recs = self._ai.read(reach)
            self._ai_cache = (time.monotonic(), reach, recs)
            return recs

    def identity(self) -> dict | None:
        """The user's aircraft as the sim's ATC knows it (from the aircraft/flight settings): ATC ID (tail number),
        airline and flight number, model. None until the traffic list has been read once."""
        if self._own_record is None:
            self.traffic(*_latlon(self.own()), 1.0)
        rec = self._own_record
        if rec is None:
            return None
        return {"atc_id": rec.atc_id, "airline": rec.airline, "flight_number": rec.flight_number, "model": rec.model}

    def close(self) -> None:
        if self._ai is not None:
            self._ai.close()
        self._sm.exit()


def _latlon(own: OwnState) -> tuple[float, float]:
    return own.lat, own.lon


def _is_own(rec, own: OwnState) -> bool:
    """AI record that is really the user's aircraft: same place (within 1 s of travel), level and heading."""
    tol_nm = 0.01 + max(own.gs_kt, 0.0) / 3600.0
    return (
        distance_nm(rec.lat, rec.lon, own.lat, own.lon) <= tol_nm
        and abs(rec.alt_ft - own.alt_msl_ft) < 150
        and heading_diff(rec.heading_deg, own.heading_deg) < 15
    )
