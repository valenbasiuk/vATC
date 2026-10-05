"""Which controller answers on the tuned COM1 frequency.

This is what decides the AI's role each turn. If nobody is on that frequency, the AI stays silent.
"""

from __future__ import annotations

from atc.models import Airport, Facility, Frequency

# Keys are the OurAirports frequency types (checked against its data dictionary):
# TWR, GND, RMP, ATIS, ARR, DEP, ATF, CTAF, UNICOM, RCO, RDO.
# APP and CLD are kept as aliases for hand-edited files.
_ROLE = {
    "TWR": ("tower", True),
    "GND": ("ground", True),
    "RMP": ("ground", True),  # ramp control, handled like ground
    "ARR": ("approach", True),
    "APP": ("approach", True),
    "DEP": ("departure", True),
    "CLD": ("clearance", True),
    "ATIS": ("atis", False),
    "ATF": ("advisory", False),
    "CTAF": ("advisory", False),
    "UNICOM": ("advisory", False),
    "RCO": ("advisory", False),
    "RDO": ("advisory", False),
    "A/D": ("advisory", False),
}

_TOLERANCE_MHZ = 0.005  # 25 kHz channels vs 8.33 kHz: compare loosely


def resolve_facility(airport: Airport, com1_mhz: float) -> Facility | None:
    for f in airport.frequencies:
        if abs(f.mhz - com1_mhz) <= _TOLERANCE_MHZ:
            role, can_reply = _ROLE.get(f.kind, ("advisory", False))
            return Facility(role=role, freq=f, can_reply=can_reply)
    return None


def callsign_for(airport: Airport, facility: Facility) -> str:
    """Spoken facility name, e.g. 'Corrientes Tower'. Edit names in the YAML if this sounds off."""
    short = airport.spoken_name or (
        airport.name.replace("International", "").replace("Airport", "").replace("Aeropuerto", "").strip()
    )
    suffix = {
        "tower": "Tower",
        "ground": "Ground",
        "approach": "Approach",
        "departure": "Departure",
        "clearance": "Delivery",
    }.get(facility.role, "")
    return f"{short} {suffix}".strip()


def describe_frequencies(frequencies: list[Frequency]) -> str:
    return ", ".join(f"{f.kind} {f.mhz:.3f}" for f in frequencies) or "none on file"
