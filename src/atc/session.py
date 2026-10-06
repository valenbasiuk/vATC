"""Per-flight state that code keeps so the LLM never has to remember or decide it (roadmap item 9, first slice)."""

from __future__ import annotations

import csv
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from atc import phrase
from atc.flightplan import FlightPlan
from atc.readback import _normalize, join_digits

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
    contacted: set[str] = field(default_factory=set)  # "<ICAO>:<role>" positions already talked to
    where: str = ""  # ICAO of the facility being talked to this turn (main sets it)
    last_role: str | None = None  # position that made the last ATC transmission (readbacks only count there)
    surface_wind: dict[str, tuple[float, float]] = field(default_factory=dict)  # ICAO -> last low, near wind
    # flight phase, kept by flow.track() from the telemetry watcher
    was_on_ground: bool | None = None
    departed_from: str | None = None
    airborne_at: float | None = None
    landed: bool = False  # taxi requests are then to a stand
    landed_at: str | None = None
    handoffs_done: set[str] = field(default_factory=set)
    pending_handoff: object | None = None  # flow.Pending
    awaiting_squawk: float | None = None  # frequency where "squawk XXXX" was given; "radar contact" follows
    # radar / arrival (enroute.py)
    cleared_level_ft: int | None = None
    direct_to: str | None = None
    descent_given: bool = False
    vectors_given: bool = False
    intercept_given: bool = False
    landing_cleared: bool = False
    vacate_given: bool = False
    # "given" (--telephony), "table" (airline list or remembered from an earlier flight), "learned" (pilot's call)
    telephony_source: str | None = None
    acked_atc: str | None = None  # last ATC instruction already read back / acknowledged: not checked again
    # "<ICAO>:<role>" -> instruction ATC asked to have read back ("read back"): the next call is checked against it
    readback_due: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.telephony is not None:
            self.telephony_source = self.telephony_source or "given"
            return
        prefix = re.match(r"[A-Z]{3}(?=\d)", self.callsign.upper())
        if prefix:
            code = prefix.group(0)
            self.telephony = TELEPHONY.get(code) or remembered_telephony(code) or table_telephony(code)
            self.telephony_source = "table" if self.telephony else None

    @property
    def spoken_callsign(self) -> str:
        return phrase.callsign(self.telephony, self.callsign)

    def learn_telephony(self, pilot_text: str) -> None:
        """'... Martinair 4133 requesting ...' -> telephony 'Martinair' (only if the number matches ours).
        A name from the airline table is only a first guess: what the pilot calls themselves wins, once."""
        num = "".join(c for c in self.callsign if c.isdigit())
        if self.telephony_source in ("given", "learned") or not num or not re.match(r"[A-Z]{3}\d", self.callsign.upper()):
            return
        compact = _compact_call(pilot_text)
        for m in re.finditer(rf"(?:\b([a-z]{{3,}}) )?\b([a-z]{{3,}}) {num}\b", compact):
            word = m.group(2)
            if word in _NOT_TELEPHONY:
                continue
            if word in _SPLIT_TAILS and m.group(1) and m.group(1) not in _NOT_TELEPHONY:
                word = m.group(1) + word
            # "Martiner" heard for a table "Martinair": keep the table's spelling
            if not (self.telephony and difflib.SequenceMatcher(None, word, self.telephony.lower()).ratio() >= 0.75):
                self.telephony = word.title()
            self.telephony_source = "learned"
            remember_telephony(self.callsign[:3], self.telephony)
            return

    def names_other_flight(self, pilot_text: str) -> bool:
        """True if the call names an airline flight that is not ours ("Aerolineas 1234", or "Martinair 4113"
        misheard). Only airline-style callsigns are checked; a registration callsign never triggers this."""
        num = "".join(c for c in self.callsign if c.isdigit())
        if not num or not re.match(r"[A-Z]{3}\d", self.callsign.upper()):
            return False
        compact = _compact_call(pilot_text)
        if re.search(rf"\b{num}\b", compact):
            return False
        names = {t.lower() for t in TELEPHONY.values()} | _KNOWN_TELEPHONY
        own = (self.telephony or "").lower()
        if own:
            names.add(own)
        for m in re.finditer(r"\b([a-z]+) (\d{2,5})\b", compact):
            name, digits = m.groups()
            if name not in names:
                continue
            # In a sim every call is ours: our name with one digit misheard ("4113") is still us.
            if name == own and len(digits) == len(num) and sum(a != b for a, b in zip(digits, num)) <= 1:
                continue
            return True
        return False

    def _key(self, role: str) -> str:
        return f"{self.where}:{role}" if self.where else role

    def is_first_contact(self, role: str) -> bool:
        return self._key(role) not in self.contacted

    def first_contact(self, role: str) -> bool:
        """True the first time we answer on this position; marks it as contacted."""
        first = self.is_first_contact(role)
        self.contacted.add(self._key(role))
        return first


def _compact_call(pilot_text: str) -> str:
    """Normalized, digits joined, letters split from digits: 'LATAM1302' -> 'latam 1302', 'four one' -> '41'."""
    return join_digits(re.sub(r"(?<=[a-z])(?=\d)", " ", _normalize(pilot_text)))


# Telephonies learned from the pilot's own calls, kept across runs (MAR -> Martinair), so ATC knows the name
# before the first call. main sets the path; None (tests) = don't read or write anything.
LEARNED_PATH: Path | None = None


def remembered_telephony(designator: str) -> str | None:
    if LEARNED_PATH is None or not LEARNED_PATH.exists():
        return None
    try:
        return json.loads(LEARNED_PATH.read_text(encoding="utf-8")).get(designator.upper())
    except (OSError, ValueError):
        return None


def remember_telephony(designator: str, name: str | None) -> None:
    if LEARNED_PATH is None or not name or not re.fullmatch(r"[A-Za-z]{3}", designator):
        return
    try:
        known = json.loads(LEARNED_PATH.read_text(encoding="utf-8")) if LEARNED_PATH.exists() else {}
        if known.get(designator.upper()) != name:
            known[designator.upper()] = name
            LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
            LEARNED_PATH.write_text(json.dumps(known, indent=1, sort_keys=True), encoding="utf-8")
    except (OSError, ValueError):
        pass  # remembering is a convenience; never break the radio over it


_AIRLINES: dict[str, str] | None = None


def table_telephony(icao_designator: str, path: Path = Path("data/airlines.dat")) -> str | None:
    """Radio telephony from the OpenFlights airline list (atc-gen --download fetches it), e.g. ARG -> Argentina."""
    global _AIRLINES
    if _AIRLINES is None:
        _AIRLINES = {}
        if path.exists():
            with path.open(encoding="utf-8", errors="replace", newline="") as fh:
                for row in csv.reader(fh):
                    if len(row) >= 8 and row[7] == "Y" and len(row[4]) == 3 and row[5] not in ("", "\\N"):
                        _AIRLINES.setdefault(row[4].upper(), row[5].strip().title())
    return _AIRLINES.get(icao_designator.upper())
