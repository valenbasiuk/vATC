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


@dataclass
class Session:
    callsign: str  # ICAO form, e.g. MAR4133 (or a registration like N123AB)
    plan: FlightPlan | None = None
    telephony: str | None = None  # "Martinair"
    dest_name: str | None = None  # clearance limit as spoken ("Rosario"); falls back to the plan's airport name
    clearance: str = "none"  # "none" -> "issued" -> "confirmed"
    clearance_text: str = ""  # exactly what was said, for "say again"
    pending: list[str] = field(default_factory=list)  # clearance items still to be read back after a correction
    contacted: set[str] = field(default_factory=set)  # facility roles already talked to

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
        for m in re.finditer(rf"\b([a-z]{{3,}}) {num}\b", compact):
            if m.group(1) not in _NOT_TELEPHONY:
                self.telephony = m.group(1).title()
                return

    def first_contact(self, role: str) -> bool:
        """True the first time we answer on this position; marks it as contacted."""
        first = role not in self.contacted
        self.contacted.add(role)
        return first
