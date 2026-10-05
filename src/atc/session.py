"""Per-flight state that code keeps so the LLM never has to remember or decide it (roadmap item 9, first slice)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from atc import phrase
from atc.flightplan import FlightPlan
from atc.readback import _normalize

# Well-known ICAO airline designator -> radio telephony. Anything else is learned from the pilot's first call.
TELEPHONY = {
    "ARG": "Argentina", "AAL": "American", "UAL": "United", "DAL": "Delta", "SWA": "Southwest",
    "JBU": "JetBlue", "BAW": "Speedbird", "AFR": "Airfrance", "DLH": "Lufthansa", "KLM": "KLM",
    "IBE": "Iberia", "AZU": "Azul", "GLO": "Gol",
}
_NOT_TELEPHONY = {"flight", "number", "squawk", "squawking", "runway", "level", "heading", "is", "with", "and",
                  "information", "on", "to", "at", "for", "gate", "stand"}
# Other airline names heard on Argentine frequencies, for spotting a call that is not ours.
_KNOWN_TELEPHONY = {"aerolineas", "austral", "flybondi", "jetsmart", "latam", "lan", "andes", "american", "united"}
# Speech-to-text splits one-word telephonies: "Martinair" -> "Martin Air". These tails get joined back.
_SPLIT_TAILS = {"air", "airlines", "airways", "jet", "express", "link", "wings", "bird"}


@dataclass
class Session:
    callsign: str  # ICAO form, e.g. MAR4133 (or a registration like N123AB)
    plan: FlightPlan | None = None
    telephony: str | None = None  # "Martinair"
    dest_name: str | None = None  # clearance limit as spoken ("Rosario"); falls back to the plan's airport name
    clearance: str = "none"  # "none" -> ("standby" ->) "issued" -> "confirmed"
    standby_chance: float = 0.0  # chance Delivery says "standby" and calls back later (main sets it from --standby)
    clearance_text: str = ""  # exactly what was said, for "say again"
    pending: list[str] = field(default_factory=list)  # clearance items still to be read back after a correction
    contacted: set[str] = field(default_factory=set)  # facility roles already talked to
    last_role: str | None = None  # position that made the last ATC transmission (readbacks only count there)
    surface_wind: tuple[float, float] | None = None  # last wind read low and near the airport (dir, kt)

    def __post_init__(self) -> None:
        if self.telephony is None:
            prefix = re.match(r"[A-Z]{3}(?=\d)", self.callsign.upper())
            self.telephony = TELEPHONY.get(prefix.group(0)) if prefix else None

    @property
    def spoken_callsign(self) -> str:
        return phrase.callsign(self.telephony, self.callsign)

    def learn_telephony(self, pilot_text: str) -> None:
        """'... Martinair 4133 requesting ...' -> telephony 'Martinair' (only if the number matches ours)."""
        num = "".join(c for c in self.callsign if c.isdigit())
        if self.telephony or not num or not re.match(r"[A-Z]{3}\d", self.callsign.upper()):
            return
        compact = re.sub(r"(?<=\d) (?=\d)", "", _normalize(pilot_text))
        for m in re.finditer(rf"(?:\b([a-z]{{3,}}) )?\b([a-z]{{3,}}) {num}\b", compact):
            word = m.group(2)
            if word in _NOT_TELEPHONY:
                continue
            if word in _SPLIT_TAILS and m.group(1) and m.group(1) not in _NOT_TELEPHONY:
                word = m.group(1) + word
            self.telephony = word.title()
            return

    def names_other_flight(self, pilot_text: str) -> bool:
        """True if the call names an airline flight that is not ours ("Aerolineas 1234", or "Martinair 4113"
        misheard). Only airline-style callsigns are checked; a registration callsign never triggers this."""
        num = "".join(c for c in self.callsign if c.isdigit())
        if not num or not re.match(r"[A-Z]{3}\d", self.callsign.upper()):
            return False
        compact = re.sub(r"(?<=\d) (?=\d)", "", _normalize(pilot_text))
        if re.search(rf"\b{num}\b", compact):
            return False
        names = {t.lower() for t in TELEPHONY.values()} | _KNOWN_TELEPHONY
        if self.telephony:
            names.add(self.telephony.lower())
        return any(m.group(1) in names for m in re.finditer(r"\b([a-z]+) \d{2,4}\b", compact))

    def first_contact(self, role: str) -> bool:
        """True the first time we answer on this position; marks it as contacted."""
        first = role not in self.contacted
        self.contacted.add(role)
        return first
