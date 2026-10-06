"""Plain data classes shared by every module. No sim or audio imports here."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OwnState:
    """Own aircraft, as read from the sim (or the fake)."""

    lat: float
    lon: float
    alt_msl_ft: float
    alt_agl_ft: float
    gs_kt: float
    heading_deg: float  # true heading
    on_ground: bool
    com1_mhz: float
    squawk: str = "1200"
    callsign: str = "N123AB"
    aircraft_type: str = "C172"
    # Weather at the aircraft. None = unknown, and then ATC must not state it.
    wind_dir_deg: float | None = None  # direction wind blows FROM, true
    wind_kt: float | None = None
    qnh_hpa: float | None = None
    temp_c: float | None = None  # outside air temperature at the aircraft
    com2_mhz: float | None = None  # only when COM2 is heard (receive on): the ATIS is often listened to there
    zulu_s: float | None = None  # sim clock, seconds since 00:00 UTC


@dataclass
class Traffic:
    """Another aircraft near the airport (AI or injected)."""

    callsign: str
    lat: float
    lon: float
    alt_msl_ft: float
    gs_kt: float
    heading_deg: float
    on_ground: bool
    type: str | None = None  # ICAO type or the sim's "ATC MODEL" string; None = unknown, never guessed
    airline: str | None = None  # sim "ATC AIRLINE"
    flight_number: str | None = None  # sim "ATC FLIGHT NUMBER"


@dataclass
class Frequency:
    kind: str  # TWR, GND, ATIS, APP, DEP, CLD, CTAF, UNICOM, ...
    mhz: float
    description: str = ""
    spoken: str | None = None  # hand field: station name on this frequency if not "<airport> <role>"


@dataclass
class Runway:
    ident: str  # e.g. "03"
    heading_deg: float | None = None
    length_ft: float | None = None
    surface: str | None = None
    # Pattern rules. None = unknown, needs a human to fill in.
    pattern_alt_agl_ft: int | None = None
    pattern_direction: str | None = None  # "left" | "right"
    # Threshold position (OurAirports le/he_latitude_deg). None = unknown: sequence.py then assumes the
    # airport reference point is the runway midpoint, which is only right for single-runway fields.
    lat: float | None = None
    lon: float | None = None


@dataclass
class Airport:
    icao: str
    name: str
    lat: float
    lon: float
    elevation_ft: float
    country: str = ""
    towered: bool = False
    language: str = "en"
    runways: list[Runway] = field(default_factory=list)
    frequencies: list[Frequency] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # local procedures, free text
    needs_review: bool = True  # flip to false once a human checked the file
    spoken_name: str | None = None  # how controllers say it: "Aeroparque" (station names, clearance limits)
    # Hand-written from the charts: runway ident -> taxi route from the main apron, e.g. {"13": "via Alfa, Charlie"}.
    taxi_routes: dict[str, str] = field(default_factory=dict)
    # Magnetic variation from the AD chart, east positive ("VAR 10° W" -> -10). None = unknown: winds are then
    # said in true degrees, as the sim gives them.
    mag_var_deg: float | None = None
    # runway ident -> approach types in the sim's navdata, e.g. {"20": ["ILS", "RNAV"], "02": ["RNAV", "VORDME"]}
    approaches: dict[str, list[str]] = field(default_factory=dict)
    # Transition altitude (feet). Levels above it are flight levels: US 18000, Argentina 3000, UK 6000...
    # None = unknown (then flight levels from 10000 ft, the old rule). Filled from the sim's data (navdb.enrich).
    trans_alt_ft: int | None = None

    @property
    def faa(self) -> bool:
        """US phraseology (FAA 7110.65) instead of ICAO."""
        return self.country == "US"


@dataclass
class Facility:
    """Who is answering on the tuned frequency."""

    role: str  # "tower", "ground", "approach", "departure", "clearance", "atis", "advisory"
    freq: Frequency
    can_reply: bool  # False for ATIS and CTAF/UNICOM
