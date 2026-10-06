"""Radio traffic between ATC and the AI aircraft, from the tracker's events, and the rules for when it may be said.

Each exchange is ATC's instruction plus the AI pilot's readback (in its own Piper voice), only on the frequency
the user is tuned to (Tower events on Tower, Ground events on Ground). The radio is one channel:
  - never while the user holds push-to-talk, never on top of another transmission;
  - a gap after every transmission (GAP_S), and a longer quiet window after ATC talks to the user (READBACK_S),
    so the user always gets to answer first;
  - stale chatter is dropped (an instruction for something that already happened sounds wrong), and a newer event
    for the same aircraft replaces an older one still waiting ("cleared for takeoff" replaces "line up").
"""

from __future__ import annotations

import csv
import re
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from atc import phrase
from atc.clearance import _freq
from atc.facility import resolve_facility
from atc.models import Airport, OwnState, Traffic
from atc.runway import magnetic, runway_in_use
from atc.sequence import runway_status
from atc.tracker import Event

GAP_S = 4.0
READBACK_S = 6.0  # after ATC talks to the user, chatter waits this long for their answer (Valen: 6 s)
STALE_S = 15.0
MAX_QUEUE = 4
NUM_VOICES = 904  # en_US-libritts-high speakers


@dataclass
class Exchange:
    key: str  # aircraft callsign: one waiting exchange per aircraft
    lines: list[tuple[str, str, int | None]]  # (who, text, piper speaker id or None for the ATC voice)
    created: float
    role: str  # position that talks: "tower" / "ground"
    priority: int = 1  # 0 = most urgent (landing/takeoff clearances)
    country: str | None = None  # where the AI aircraft is from (its pilot's accent)


@lru_cache(maxsize=1)
def _airline_telephony(path: str = "data/airlines.dat") -> dict[str, str]:
    """Airline name (lower) -> telephony, from the OpenFlights list: 'aerolineas argentinas' -> 'Argentina'."""
    out: dict[str, str] = {}
    p = Path(path)
    if p.exists():
        with p.open(encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) >= 8 and row[5] not in ("", "\\N"):
                    out.setdefault(row[1].strip().lower(), row[5].strip().title())
                    if len(row[4]) == 3:
                        out.setdefault(row[4].upper(), row[5].strip().title())
    return out


def ai_callsign(t: Traffic, faa: bool = False) -> str:
    """How ATC calls an AI aircraft: 'Argentina one two three four' (airline + flight number), else the
    registration spelled ('Lima Victor Golf Tango Uniform')."""
    table = _airline_telephony()
    if t.airline and t.flight_number and t.flight_number.isdigit():
        tel = table.get(t.airline.strip().lower()) or t.airline.split()[0].title()
        return phrase.callsign(tel, t.flight_number, faa)
    m = re.fullmatch(r"([A-Z]{3})(\d{1,4})", (t.callsign or "").upper().replace("-", ""))
    if m and m.group(1) in table:
        return phrase.callsign(table[m.group(1)], m.group(2), faa)
    return phrase.spell(t.callsign or "")


def voice_for(callsign: str) -> int:
    """A stable Piper speaker per AI aircraft (never 0, the ATC voice)."""
    return 1 + zlib.crc32(callsign.encode()) % (NUM_VOICES - 1)


def _contact(airport: Airport, *kinds: str) -> str | None:
    f = _freq(airport, *kinds)
    if f is None:
        return None
    if airport.faa and kinds[0] == "GND" and 121.6 <= f.mhz <= 121.975:
        return phrase.frequency(f.mhz, True).split(" ", 3)[-1]  # FAA: "ground point eight" for 121.8
    return phrase.frequency(f.mhz, airport.faa)


def _departure_name(airport: Airport) -> str:
    """'NorCal Departure' when the frequency has its own name (KSFO), else just 'departure'."""
    f = _freq(airport, "DEP", "APP", "ARR")
    if f is not None and f.spoken:
        return f.spoken.replace("Approach", "Departure")
    return "departure"


def exchange_for(ev: Event, own: OwnState, traffic: list[Traffic], wind: tuple[float | None, float | None],
                 net=None) -> Exchange | None:
    """ATC's instruction + the AI's readback for one event, or None if nothing would be said.
    `net`: the airport's taxi map, so an AI taxiing out gets its real route ("via Kilo, Alfa")."""
    a, t = ev.airport, ev.traffic
    faa = a.faa
    cs = ai_callsign(t, faa)
    if not cs:
        return None
    voice = voice_for(t.callsign)
    w = phrase.wind(magnetic(a, wind[0]), wind[1], faa)
    rwy = ev.runway or runway_in_use(a, wind[0], wind[1])
    rw = phrase.runway(rwy.ident, faa) if rwy is not None else None
    atc = pilot = None
    role, prio = "tower", 1
    if ev.kind == "pushback":
        role = "ground"
        atc = f"{cs}, push back approved" if faa else f"{cs}, push and start approved"
        pilot = f"Push back approved, {cs}" if faa else f"Push and start approved, {cs}"
    elif ev.kind == "taxi_out" and rw:
        from atc.taxi import departure_route

        role = "ground"
        via = departure_route(net, a, rwy, t) if net is not None else None  # Traffic has the lat/lon it needs
        v = f" via {via}" if via else ""
        atc = f"{cs}, taxi to holding point runway {rw}{v}" if not faa else f"{cs}, runway {rw}, taxi{v}"
        pilot = f"Holding point runway {rw}{v}, {cs}" if not faa else f"Runway {rw}, taxi{v}, {cs}"
    elif ev.kind == "lineup" and rw:
        atc, pilot, prio = f"{cs}, runway {rw}, line up and wait", f"Line up and wait runway {rw}, {cs}", 0
    elif ev.kind == "takeoff_roll" and rw:
        atc = f"{cs}, " + (f"{w}, " if w else "") + f"runway {rw}, cleared for takeoff"
        pilot, prio = f"Cleared for takeoff runway {rw}, {cs}", 0
    elif ev.kind == "departed":
        dep = _contact(a, "DEP", "APP", "ARR")
        if dep:
            name = _departure_name(a)
            atc, pilot = f"{cs}, contact {name} {dep}, good day", f"{name[:1].upper()}{name[1:]} {dep}, good day, {cs}"
    elif ev.kind == "final" and rw:
        st = runway_status(a, rwy, own, [x for x in traffic if x.callsign != t.callsign])
        blocked = st.occupied_by or (own.on_ground and _user_on(a, rwy, own))
        if blocked and ev.repeat:
            return None  # already told to continue; the clearance comes once the runway is free
        if blocked:
            atc, pilot = f"{cs}, continue approach, traffic on the runway", f"Continue approach, {cs}"
        else:
            atc = f"{cs}, " + (f"{w}, " if w else "") + f"runway {rw}, cleared to land"
            pilot = f"Cleared to land runway {rw}, {cs}"
        prio = 0
    elif ev.kind == "vacated":
        gnd = _contact(a, "GND", "RMP")
        if gnd:
            atc, pilot = f"{cs}, contact ground {gnd}", f"Ground {gnd}, {cs}"
    elif ev.kind == "taxi_in":
        role = "ground"
        where = "the ramp" if faa else "the apron"
        atc, pilot = f"{cs}, taxi to {where}", f"Taxi to {where}, {cs}"
    if atc is None:
        return None
    pilot = pilot[:1].upper() + pilot[1:]
    from atc.voices import country_of

    return Exchange(t.callsign, [("ATC", atc + ".", None), (cs, pilot + ".", voice)], ev.at, role, prio,
                    country_of(t.airline, t.callsign))


def _user_on(airport: Airport, rwy, own: OwnState) -> bool:
    from atc.sequence import on_runway

    return on_runway(airport, rwy, own)


@dataclass
class RadioBus:
    """One frequency, one voice at a time. `offer` queues chatter; `next_due` hands out what may be said now."""

    queue: list[Exchange] = field(default_factory=list)
    busy_until: float = 0.0  # gap after the last transmission (anyone's)
    quiet_until: float = 0.0  # the user's turn: after ATC talked to them

    def offer(self, ex: Exchange) -> None:
        self.queue = [q for q in self.queue if q.key != ex.key]  # newer event replaces the waiting one
        self.queue.append(ex)
        self.queue.sort(key=lambda q: (q.priority, q.created))
        del self.queue[MAX_QUEUE:]

    def heard(self, now: float, to_user: bool = False) -> None:
        """Something was transmitted (by anyone). After ATC talks to the user, leave them time to answer."""
        self.busy_until = max(self.busy_until, now + GAP_S)
        if to_user:
            self.quiet_until = max(self.quiet_until, now + READBACK_S)

    def next_due(self, now: float, role: str | None) -> Exchange | None:
        self.queue = [q for q in self.queue if now - q.created <= STALE_S]
        if role is None or now < self.busy_until or now < self.quiet_until:
            return None
        for i, q in enumerate(self.queue):
            if q.role == role:
                return self.queue.pop(i)
        return None


def facility_role(airport: Airport, own: OwnState) -> str | None:
    fac = resolve_facility(airport, own.com1_mhz)
    return fac.role if fac and fac.can_reply else None
