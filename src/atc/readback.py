"""Readback check. CODE decides whether the pilot read back the last instruction correctly;
the LLM is never asked to judge it (small free models get it wrong).

Items checked against the previous ATC instruction: the runway, "hold short", takeoff clearance,
landing clearance. A pilot call that also makes a request ("ready for departure...") is not treated
as a readback and goes to the LLM as usual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "tree": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9",
}
_SIDE = {"left": "l", "right": "r", "center": "c", "centre": "c"}
_REQUEST_WORDS = ("ready", "request", "inbound", "departure", "taxi to the", "go around", "unable", "say again")


@dataclass
class ReadbackResult:
    status: str  # "none" (not a readback), "correct", "incomplete"
    missing: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    t = text.lower().replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return " ".join(_DIGITS.get(w, w) for w in t.split())


def _runway(text: str) -> str | None:
    """Runway ident from normalized text, e.g. 'runway 0 9 left' -> '09l', 'runway 27' -> '27'."""
    m = re.search(r"(?:runway|holding point|hold short(?: of)?) ((?:\d ?){1,2})\s*(left|right|center|centre|l|r|c)?\b",
                  text)
    if not m:
        return None
    digits = m.group(1).replace(" ", "").zfill(2)
    return digits + _SIDE.get(m.group(2) or "", m.group(2) or "")


_NATO_WORDS = {
    "alfa", "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliett", "juliet",
    "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
    "victor", "whiskey", "whisky", "xray", "yankee", "zulu",
}


def _taxi_route(t: str, letters_ok: bool) -> frozenset[str] | None:
    """Taxiway designators after 'via' in normalized text: 'via kilo alfa 1' -> {'k', 'a1'}.
    `letters_ok`: also accept bare letters ('via k a'), which is how speech-to-text writes a pilot's readback."""
    m = re.search(r"\bvia ((?:\w+ ?)+?)(?= qnh| altimeter| hold| runway| cleared|$)", t)
    if not m:
        return None
    out: list[str] = []
    prev_designator = False
    for w in m.group(1).split():
        if w in _NATO_WORDS or (letters_ok and len(w) == 1 and w.isalpha()):
            out.append(w[0])
            prev_designator = True
        elif w.isdigit() and len(w) <= 2 and prev_designator:
            out[-1] += w  # "alfa 1" -> "a1" (but not the callsign digits that follow the route)
            prev_designator = False
        else:
            prev_designator = False
    return frozenset(out) or None


def _items(text: str, letters_ok: bool = False) -> dict[str, str | bool | frozenset]:
    t = _normalize(text)
    items: dict[str, str | bool | frozenset] = {}
    rwy = _runway(t)
    if rwy:
        items["runway"] = rwy
    route = _taxi_route(t, letters_ok)
    if route:
        items["taxi route"] = route
    if "hold short" in t or "holding short" in t or "holding point" in t:  # FAA / ICAO
        items["hold short"] = True
    if "cleared for takeoff" in t or "cleared takeoff" in t:
        items["cleared for takeoff"] = True
    if "cleared to land" in t:
        items["cleared to land"] = True
    return items


_ACK_WORDS = ("roger", "wilco", "copied", "copy", "standing by", "will call", "good day", "thank")


def is_acknowledgement(last_atc: str | None, pilot_text: str, ignore: tuple[str, ...] = ()) -> bool:
    """'Roger', 'wilco', or a readback that only repeats words of the last instruction (no request in it).
    A real controller does not answer these, so code keeps the frequency quiet. `ignore`: callsign words."""
    if not last_atc:
        return False
    t = _normalize(pilot_text)
    if any(w in t for w in _REQUEST_WORDS) or "?" in pilot_text:
        return False
    if any(w in t for w in _ACK_WORDS):
        return True
    said = {_stem(w) for w in _normalize(last_atc).split()}
    skip = {w.lower() for w in ignore}
    words = [_stem(w) for w in t.split() if not w.isdigit() and len(w) > 2 and w not in skip]
    return bool(words) and sum(w in said for w in words) / len(words) >= 0.7


def _stem(w: str) -> str:
    """'holding' ~ 'hold', 'contacting' ~ 'contact': a readback often changes the verb form."""
    return w[:-3] if w.endswith("ing") and len(w) > 5 else w


def check_readback(last_atc: str | None, pilot_text: str) -> ReadbackResult:
    if not last_atc:
        return ReadbackResult("none")
    expected = _items(last_atc)
    if not expected:
        return ReadbackResult("none")
    pilot_norm = _normalize(pilot_text)
    if any(w in pilot_norm for w in _REQUEST_WORDS):
        return ReadbackResult("none")
    got = _items(pilot_text, letters_ok=True)
    if not got:  # nothing of the instruction repeated: not a readback attempt
        return ReadbackResult("none")
    missing = []
    for key, val in expected.items():
        if key == "runway":
            if got.get("runway") != val:
                missing.append(f"runway {val}")
        elif key == "taxi route":
            if not val <= got.get("taxi route", frozenset()):
                missing.append("taxi route")
        elif key not in got:
            missing.append(key)
    return ReadbackResult("incomplete" if missing else "correct", missing)
