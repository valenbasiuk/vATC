"""Fake sim so everything can be developed and tested with MSFS closed."""

from __future__ import annotations

import math
from dataclasses import replace

from atc.geo import distance_nm
from atc.models import Airport, OwnState, Traffic


class FakeSim:
    def __init__(self, airport: Airport, callsign: str = "N123AB") -> None:
        self._airport = airport
        self._own = OwnState(
            lat=airport.lat,
            lon=airport.lon,
            alt_msl_ft=airport.elevation_ft,
            alt_agl_ft=0.0,
            gs_kt=0.0,
            heading_deg=0.0,
            on_ground=True,
            com1_mhz=_first_freq(airport, "GND") or _first_freq(airport, "TWR") or 122.8,
            callsign=callsign,
        )
        self._traffic: list[Traffic] = []

    # --- SimSource ---
    def own(self) -> OwnState:
        return self._own

    def traffic(self, center_lat: float, center_lon: float, radius_nm: float) -> list[Traffic]:
        return [
            t
            for t in self._traffic
            if distance_nm(center_lat, center_lon, t.lat, t.lon) <= radius_nm
        ]

    def close(self) -> None:
        pass

    # --- helpers for the text REPL and tests ---
    def update(self, **changes) -> None:
        self._own = replace(self._own, **changes)

    def add_traffic(self, t: Traffic) -> None:
        self._traffic.append(t)

    def clear_traffic(self) -> None:
        self._traffic.clear()

    def set_airborne(self, agl_ft: float = 1000.0, gs_kt: float = 90.0) -> None:
        self.update(
            on_ground=False,
            alt_agl_ft=agl_ft,
            alt_msl_ft=self._airport.elevation_ft + agl_ft,
            gs_kt=gs_kt,
        )

    def add_on_final(self, nm: float, callsign: str = "AI001", runway: str | None = None) -> None:
        """AI arrival on a 3-degree final, `nm` from the threshold (runway in use if not given)."""
        from atc.runway import runway_in_use
        from atc.sequence import threshold

        rwy = next((r for r in self._airport.runways if r.ident == runway), None) or runway_in_use(
            self._airport, self._own.wind_dir_deg, self._own.wind_kt)
        tlat, tlon = threshold(self._airport, rwy)
        h = math.radians(rwy.heading_deg)
        lat = tlat - nm * math.cos(h) / 60.0
        lon = tlon - nm * math.sin(h) / (60.0 * math.cos(math.radians(tlat)))
        alt = self._airport.elevation_ft + 50 + nm * 318  # 318 ft per NM = 3 degrees
        self.add_traffic(Traffic(callsign, lat, lon, alt, 140.0, rwy.heading_deg, False))

    def add_on_runway(self, callsign: str = "AI002", runway: str | None = None) -> None:
        """AI aircraft lined up on the runway threshold."""
        from atc.runway import runway_in_use
        from atc.sequence import threshold

        rwy = next((r for r in self._airport.runways if r.ident == runway), None) or runway_in_use(
            self._airport, self._own.wind_dir_deg, self._own.wind_kt)
        lat, lon = threshold(self._airport, rwy)
        self.add_traffic(Traffic(callsign, lat, lon, self._airport.elevation_ft, 0.0, rwy.heading_deg, True))

    def set_on_ground(self) -> None:
        self.update(
            on_ground=True,
            alt_agl_ft=0.0,
            alt_msl_ft=self._airport.elevation_ft,
            gs_kt=0.0,
        )


def _first_freq(airport: Airport, kind: str) -> float | None:
    for f in airport.frequencies:
        if f.kind == kind:
            return f.mhz
    return None
