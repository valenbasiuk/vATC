"""IFR clearance at the origin, done entirely in code: issue, check the readback, correct it.

Controllers say clearances the same way every time, so a template is both more realistic and more
reliable than asking a small model to phrase them. The LLM only sees the result (in CONTEXT).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from atc import phrase
from atc.facility import callsign_for
from atc.models import Airport, Facility, Frequency
from atc.readback import _normalize, join_digits
from atc.session import Session


@dataclass
class Item:
    key: str  # "clearance limit", "SID", "level", "departure frequency", "squawk"
    spoken: str  # how it is said in the clearance
    pattern: str  # regex over the compacted pilot readback
    required: bool = True  # must appear in the readback


def _freq(airport: Airport, *kinds: str) -> Frequency | None:
    for kind in kinds:
        for f in airport.frequencies:
            if f.kind == kind:
                return f
    return None


def issuing_role(airport: Airport) -> str:
    """Who gives IFR clearances here: Delivery if the airport has one, else Ground, else Tower."""
    if _freq(airport, "CLD"):
        return "clearance"
    return "ground" if _freq(airport, "GND", "RMP") else "tower"


def _compact(text: str) -> str:
    """Normalized text with digit groups joined: 'one two zero decimal six' -> '1206', '120.600' -> '120600'.
    A comma ends a number, so 'flight level 200, 120.6' stays '200 1206' (not '2001206')."""
    return " ".join(join_digits(_normalize(part)) for part in re.split(r"[,;]", text))


# What speech-to-text writes for a spoken single digit in a SID name ("ATOVO four bravo" -> "Atovil for Bravo").
_DIGIT_SOUNDALIKES = {"1": "won", "2": "to|too", "4": "for", "8": "ate"}


def items(session: Session, airport: Airport, dest_name: str) -> list[Item]:
    plan = session.plan
    faa = airport.country == "US"
    out = [
        Item(
            "clearance limit",
            f"cleared to {dest_name}",
            # ICAO code, or the first word of the name ("Rosario" of "Rosario Islas Malvinas ...")
            "|".join(rf"\b{re.escape(w)}\b" for w in {plan.destination.lower(), dest_name.lower().split()[0]}),
        )
    ]
    if plan.sid:
        base = plan.sid.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ").rstrip("0123456789")
        num = plan.sid[len(base):].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        letter = plan.sid[len(base) + len(num):]
        nato = phrase._NATO.get(letter, letter).lower() if letter else ""
        spoken = f"{phrase.procedure(plan.sid)} departure"
        if plan.sid_transition and plan.sid_transition != base:
            spoken += f", {plan.sid_transition} transition"
        num_pat = f"(?:{num}|{_DIGIT_SOUNDALIKES[num]})" if num in _DIGIT_SOUNDALIKES else num
        pat = rf"{base[:4].lower()}\w*\s*{num_pat}" + (rf"\s*(?:{letter.lower()}|{nato})\b" if letter else "")
        out.append(Item("SID", spoken, pat))
    out.append(Item("route", "then as filed" if faa else "flight planned route", "", required=False))
    if plan.cruise_ft:
        ft = plan.cruise_ft
        # read back as "flight level 200" / "FL200", or (below the transition altitude) "11000" / "one one thousand"
        pat = rf"\b{ft // 100}\b" if phrase.is_flight_level(ft, airport) else \
            rf"\b{ft}\b" + (rf"|\b{ft // 1000} thousand\b" if ft % 1000 == 0 else "")
        how = "climb via SID, " if plan.sid else ""
        out.append(Item("level", f"{how}expect {phrase.level(ft, airport)}, ten minutes after departure", pat))
    dep = _freq(airport, "DEP", "APP", "ARR")
    if dep:
        fd = f"{dep.mhz:.3f}".replace(".", "").rstrip("0")
        out.append(Item("departure frequency", f"departure frequency {phrase.frequency(dep.mhz, faa)}", rf"\b{fd}0*\b"))
    out.append(Item("squawk", f"squawk {phrase.digits(plan.squawk)}", rf"\b{plan.squawk}\b"))
    return out


_ACK = re.compile(r"\b(roger|wilco|copied|copy|thanks|thank you)\b")


def is_clearance_request(pilot_text: str) -> bool:
    t = _normalize(pilot_text)
    return "clearance" in t or ("ifr" in t and "request" in t)


def handle_clearance(
    session: Session, airport: Airport, facility: Facility, pilot_text: str, dest_name: str
) -> str | None:
    """Return the reply when code owns this transmission; None means 'let the LLM answer'."""
    plan = session.plan
    if plan is None or not plan.is_ifr or airport.icao != plan.origin:
        return None
    cs = session.spoken_callsign
    who = issuing_role(airport)
    norm = _normalize(pilot_text)

    # On a dedicated Delivery position with the plan on file, any call (radio checks and questions aside) is
    # the clearance request: real Delivery just reads it out, and a speech-to-text slip mustn't lose it.
    any_call_is_request = (who == "clearance" and facility.role == who and session.clearance == "none"
                           and "radio check" not in norm and "?" not in pilot_text)
    if session.clearance in ("none", "standby") and (is_clearance_request(pilot_text) or any_call_is_request):
        if facility.role != who:  # asked the wrong position: send them to the right one
            f = _freq(airport, {"clearance": "CLD", "ground": "GND", "tower": "TWR"}[who])
            name = {"clearance": "Delivery", "ground": "Ground", "tower": "Tower"}[who]
            return f"{cs}, contact {name} {phrase.frequency(f.mhz, airport.country == 'US')}."
        station = f", {callsign_for(airport, facility)}" if session.first_contact(facility.role) else ""
        if session.clearance == "none" and random.random() < session.standby_chance:
            # Busy controller: "standby" now, the clearance comes 10-25 s later without the pilot asking
            # again (deliver_after_standby, called by a timer in main). Asking again meanwhile gets it now.
            session.clearance = "standby"
            return f"{cs}{station}, standby."
        return f"{cs}{station}, {_issue(session, airport, dest_name)}."

    if session.clearance == "issued" and facility.role == who:
        if "say again" in norm:
            return f"{cs}, I say again, {session.clearance_text}."
        compact = _compact(pilot_text)
        its = [i for i in items(session, airport, dest_name) if i.required]
        if session.pending:  # after a correction only the corrected items need reading back
            its = [i for i in its if i.key in session.pending]
        got = [i for i in its if re.search(i.pattern, compact)]
        if not got and "squawk" not in norm and "cleared" not in norm:
            if is_clearance_request(pilot_text) and not session.pending:
                return f"{cs}, {session.clearance_text}."  # asked again (e.g. came back to Delivery): give it again
            # "Roger", or straight to "request push and start": an IFR clearance must be read back (ICAO)
            if _ACK.search(norm) or re.search(r"\b(push|pushback|start|startup|taxi)\b", norm):
                return f"{cs}, read back the clearance."
            return None  # not a readback attempt: normal conversation (a question)
        missing = [i for i in its if i not in got]
        if missing:
            session.pending = [i.key for i in missing]
            return f"{cs}, negative, I say again, {', '.join(i.spoken for i in missing)}."
        session.clearance = "confirmed"
        session.pending = []
        gnd = _freq(airport, "GND", "RMP") if who == "clearance" else None
        if gnd:
            return (f"{cs}, readback correct. When ready for push and start, contact Ground "
                    f"{phrase.frequency(gnd.mhz, airport.country == 'US')}.")
        return f"{cs}, readback correct. Report ready for push and start."
    return None


def _issue(session: Session, airport: Airport, dest_name: str) -> str:
    session.clearance_text = ", ".join(i.spoken for i in items(session, airport, dest_name))
    session.clearance = "issued"
    return session.clearance_text


def deliver_after_standby(session: Session, airport: Airport, facility: Facility | None, dest_name: str) -> str | None:
    """The controller calls back with the clearance. If the pilot has left the frequency they miss it,
    and the request starts over the next time they call."""
    if session.clearance != "standby":
        return None  # already given (pilot asked again) or nothing pending
    if facility is None or facility.role != issuing_role(airport):
        session.clearance = "none"
        return None
    return f"{session.spoken_callsign}, {_issue(session, airport, dest_name)}."


def handle_push(session: Session, airport: Airport, facility: Facility, pilot_text: str) -> str | None:
    """Push/start request on Ground: approve it, or send an IFR flight without clearance back to Delivery."""
    norm = _normalize(pilot_text)
    if facility.role != "ground" or not re.search(r"\b(push|pushback|start up|startup|start)\b", norm):
        return None
    if "request" not in norm and "ready" not in norm:
        return None
    # "finished start and pushback, ready to taxi" is a taxi request, not a second push request
    if "taxi" in norm or re.search(r"\b(finished|complete|completed|done)\b", norm):
        return None
    cs = session.spoken_callsign
    plan = session.plan
    if plan and plan.is_ifr and airport.icao == plan.origin and session.clearance != "confirmed":
        who = issuing_role(airport)
        f = _freq(airport, "CLD") if who == "clearance" else None
        if f:
            return f"{cs}, no clearance received yet. Contact Delivery {phrase.frequency(f.mhz, airport.country == 'US')}."
    station = f", {callsign_for(airport, facility)}" if session.first_contact(facility.role) else ""
    return f"{cs}{station}, push and start approved."


def context_lines(session: Session, airport: Airport, facility: Facility) -> list[str]:
    """What the LLM is told about the flight plan and the clearance, as facts."""
    plan = session.plan
    if plan is None:
        return []
    lines = [
        "FILED FLIGHT PLAN (treat as fact)",
        f"  {plan.callsign}, {plan.aircraft_type}, {'IFR' if plan.is_ifr else 'VFR'}, {plan.origin} to {plan.destination}",
        f"  Route: {plan.route or 'none filed'}",
    ]
    if not (plan.is_ifr and airport.icao == plan.origin):
        return lines
    if session.clearance == "standby":
        lines.append("  IFR clearance requested; you told the pilot to standby and will call back with it. "
                     "Do not give or invent the clearance yourself.")
    elif session.clearance == "none":
        who = issuing_role(airport)
        if facility.role != who:
            lines.append(f"  IFR clearance NOT yet issued. Only {who} issues it. If the pilot asks for push, start "
                         f"or taxi, tell them to get their clearance from {who} first.")
        else:  # the request reached the model, so software didn't recognise it (often a speech-to-text slip)
            lines.append("  IFR clearance NOT yet issued. Software issues it when the pilot asks for it clearly. "
                         "Never give a clearance, route, level or squawk yourself: if the pilot seems to want "
                         "it, reply '<callsign>, say again'.")
    else:
        lines.append(f"  IFR clearance issued ({'read back correctly' if session.clearance == 'confirmed' else 'readback pending'}): "
                     f"{session.clearance_text}. Never repeat or change it.")
    return lines
