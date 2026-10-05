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

from atc.geo import distance_nm
from atc.models import OwnState, Traffic


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
        )

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
            for rec in self._ai.read(reach_m):
                if abs(rec.lat - own.lat) < 1e-4 and abs(rec.lon - own.lon) < 1e-4:
                    continue  # confirmed: the user's own aircraft IS in the list (KJFK probe); position match drops it
                    # TODO: while taxiing/flying the two reads are ~ms apart and the match is ~11 m; if you
                    # ever see yourself as traffic, filter by object id/callsign instead.
                t = to_traffic(rec)
                if distance_nm(center_lat, center_lon, t.lat, t.lon) <= radius_nm:
                    out.append(t)
            return out
        except Exception as exc:  # noqa: BLE001 - never crash the radio loop over traffic
            self._ai_failed = True
            print(f"[traffic disabled: {exc}]", file=sys.stderr)
            return []

    def close(self) -> None:
        if self._ai is not None:
            self._ai.close()
        self._sm.exit()
