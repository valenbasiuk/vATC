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
        self._scripts: list = []  # moving AI (spawn_departure / spawn_arrival)

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

    def place_on_final(self, airport: Airport, nm: float, alt_ft: float | None = None) -> None:
        """Put the own aircraft `nm` out on final to `airport`'s runway in use (for rehearsing arrivals)."""
        from atc.runway import runway_in_use
        from atc.sequence import threshold

        rwy = runway_in_use(airport, self._own.wind_dir_deg, self._own.wind_kt)
        tlat, tlon = threshold(airport, rwy)
        h = math.radians(rwy.heading_deg)
        alt = alt_ft if alt_ft is not None else airport.elevation_ft + 50 + nm * 318
        self.update(lat=tlat - nm * math.cos(h) / 60.0,
                    lon=tlon - nm * math.sin(h) / (60.0 * math.cos(math.radians(tlat))),
                    alt_msl_ft=alt, alt_agl_ft=alt - airport.elevation_ft, heading_deg=rwy.heading_deg,
                    on_ground=False, gs_kt=140.0 if nm < 15 else 250.0)

    # --- scripted AI traffic that moves (REPL /aidep /aiarr, tests): departs or arrives on its own ---
    def spawn_departure(self, callsign: str, runway: str | None = None, start: float = 0.0, **info) -> None:
        self._scripts.append(("dep", callsign, self._rwy(runway), start, info))

    def spawn_arrival(self, callsign: str, nm: float = 6.0, runway: str | None = None, start: float = 0.0,
                      **info) -> None:
        self._scripts.append(("arr", callsign, self._rwy(runway), start, dict(info, nm=nm)))

    def _rwy(self, ident):
        from atc.runway import runway_in_use

        return next((r for r in self._airport.runways if r.ident == ident), None) or runway_in_use(
            self._airport, self._own.wind_dir_deg, self._own.wind_kt)

    def step(self, now: float) -> None:
        """Move the scripted AI aircraft to where they are at time `now` (seconds, any clock)."""
        from atc.sequence import threshold

        keep = []
        for kind, cs, rwy, start, info in self._scripts:
            t = now - start
            tlat, tlon = threshold(self._airport, rwy)
            h = math.radians(rwy.heading_deg)

            def at(along: float, cross: float = 0.0) -> tuple[float, float]:
                e = along * math.sin(h) + cross * math.cos(h)
                n = along * math.cos(h) - cross * math.sin(h)
                return tlat + n / 60.0, tlon + e / (60.0 * math.cos(math.radians(tlat)))

            elev = self._airport.elevation_ft
            extra = {k: v for k, v in info.items() if k in ("type", "airline", "flight_number")}
            if kind == "dep":
                if t < 6:  # at the holding point, side of the runway, facing it
                    pos, gs, hdg, ground, alt = at(0.05, 0.08), 0.0, (rwy.heading_deg + 90) % 360, True, elev
                elif t < 20:  # lined up, waiting
                    pos, gs, hdg, ground, alt = at(0.02), (5.0 if t < 10 else 0.0), rwy.heading_deg, True, elev
                else:  # takeoff roll (6.4 kt/s), airborne at 140 kt, climb 2000 fpm
                    r = t - 20
                    rot = 140 / 6.4
                    if r < rot:
                        gs = 6.4 * r
                        along = 0.02 + 6.4 * r * r / 2 / 3600
                        ground, alt = True, elev
                    else:
                        gs = 160.0
                        along = 0.02 + 6.4 * rot * rot / 2 / 3600 + 160 * (r - rot) / 3600
                        ground, alt = False, elev + 2000 * (r - rot) / 60
                    pos, hdg = at(along), rwy.heading_deg
                    if r > 200:
                        continue  # gone
            else:
                nm = info.get("nm", 6.0)
                t_land = nm / 140 * 3600
                if t < t_land:  # on a 3-degree final at 140 kt
                    along = -nm + 140 * t / 3600
                    pos, gs, hdg, ground, alt = at(along), 140.0, rwy.heading_deg, False, elev + 50 + -along * 318
                else:
                    r = t - t_land
                    stop = 120 / 3.0  # rollout from 130 kt at 3 kt/s down to 10 kt
                    if r < stop:
                        gs = max(10.0, 130 - 3 * r)
                        along = 0.05 + (130 * r - 1.5 * r * r) / 3600
                        pos, hdg = at(along), rwy.heading_deg
                    else:  # off the runway and taxiing in, then parked
                        q = r - stop
                        along = 0.05 + (130 * stop - 1.5 * stop * stop) / 3600
                        pos = at(along, 0.06 + min(q, 30) * 0.003)
                        gs, hdg = (10.0 if q < 40 else 0.0), (rwy.heading_deg + 90) % 360
                        if q > 160:
                            continue
                    ground, alt = True, elev
            self._traffic = [x for x in self._traffic if x.callsign != cs]
            self._traffic.append(Traffic(cs, pos[0], pos[1], alt, gs, hdg, ground, **extra))
            keep.append((kind, cs, rwy, start, info))
        gone = {s[1] for s in self._scripts} - {s[1] for s in keep}
        self._traffic = [x for x in self._traffic if x.callsign not in gone]
        self._scripts = keep

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
