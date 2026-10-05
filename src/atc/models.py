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


@dataclass
class Frequency:
    kind: str  # TWR, GND, ATIS, APP, DEP, CLD, CTAF, UNICOM, ...
    mhz: float
    description: str = ""


@dataclass
class Runway:
    ident: str  # e.g. "03"
    heading_deg: float | None = None
    length_ft: float | None = None
    surface: str | None = None
    # Pattern rules. None = unknown, needs a human to fill in.
    pattern_alt_agl_ft: int | None = None
    pattern_direction: str | None = None  # "left" | "right"


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


@dataclass
class Facility:
    """Who is answering on the tuned frequency."""

    role: str  # "tower", "ground", "approach", "departure", "clearance", "atis", "advisory"
    freq: Frequency
    can_reply: bool  # False for ATIS and CTAF/UNICOM
