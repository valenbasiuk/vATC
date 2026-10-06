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
import time

from atc.geo import distance_nm, heading_diff
from atc.models import OwnState, Traffic

AI_CACHE_S = 0.8  # one AI request per watcher tick (1 s) at most
AI_MIN_REACH_M = int(30 * 1852)  # ask for at least 30 NM so the next callers' smaller radii hit the cache


class SimConnectSource:
    def __init__(self, callsign: str = "N123AB", library_path: str | None = None) -> None:
        try:
            from SimConnect import AircraftRequests, SimConnect  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install the sim extra: pip install -e .[sim] (64-bit Python)") from exc

        # library_path lets you point at the MSFS 2024 SDK's SimConnect.dll if the bundled one fails.
        self._sm = SimConnect(library_path=library_path) if library_path else SimConnect()
        self._ai = None
        self._ai_failed = False
        self._ai_cache: tuple[float, int, list] | None = None  # (time, radius_m, records) of the last AI read
        self._own_object_id: int | None = None
        self._own_record = None  # the user's aircraft as the AI list shows it: ATC ID, airline, flight number
        self._aq = AircraftRequests(self._sm, _time=500)
        self._callsign = callsign

    def _get(self, name: str, default: float = 0.0) -> float:
        v = self._aq.get(name)
        return default if v is None else v

    def _get_opt(self, name: str) -> float | None:
        """Like _get but keeps 'unknown' as None, so a failed read is never spoken as 'calm'."""
        return self._aq.get(name)

    def own(self) -> OwnState:
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
        """AI aircraft near a point. On any failure: log once, return [] (the prompt then says
        'none reported', which is safe: the model is told never to invent traffic)."""
        if self._ai_failed:
            return []
        try:
            if self._ai is None:
                from atc.sim.ai_traffic import AiTrafficReader

                self._ai = AiTrafficReader()
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
            return out
        except Exception as exc:  # noqa: BLE001 - never crash the radio loop over traffic
            self._ai_failed = True
            print(f"[traffic disabled: {exc}]", file=sys.stderr)
            return []

    def _ai_records(self, reach_m: int) -> list:
        """One SimConnect AI request serves every caller for AI_CACHE_S (the watcher asks up to six times per tick:
        takeoff, radar, ground, circuit, arrival, chatter), as long as the cached radius covers the new one."""
        now = time.monotonic()
        c = self._ai_cache
        if c is not None and now - c[0] < AI_CACHE_S and c[1] >= reach_m:
            return c[2]
        reach = max(reach_m, AI_MIN_REACH_M)
        recs = self._ai.read(reach)
        self._ai_cache = (now, reach, recs)
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
