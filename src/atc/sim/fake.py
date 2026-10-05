"""Fake sim so everything can be developed and tested with MSFS closed."""

from __future__ import annotations

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
