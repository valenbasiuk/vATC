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
    """Runway ident from normalized text, e.g. 'runway 0 9 left' -> '09l', 'runway 27' -> '27',
    'holding point for 31' -> '31' (pilots put 'for'/'to' in between)."""
    m = re.search(r"(?:runway|holding point|hold short(?: of)?)(?: (?:for|to|of|runway))* ((?:\d ?){1,2})\s*"
                  r"(left|right|center|centre|l|r|c)?\b", text)
    if not m:
        return None
    digits = m.group(1).replace(" ", "").zfill(2)
    return digits + _SIDE.get(m.group(2) or "", m.group(2) or "")


def _bare_runway(text: str, want: str) -> bool:
    """The runway said as a bare number: 'three one via alfa' reads back runway 31 (no side letters)."""
    if not want[:2].isdigit() or len(want) != 2:
        return False
    compact = re.sub(r"(?<=\d) (?=\d)", "", text)
    return re.search(rf"\b0?{int(want)}\b" if want.startswith("0") else rf"\b{want}\b", compact) is not None


def _requests(pilot_norm: str, atc_norm: str) -> bool:
    """Does the pilot ask for something? Words the pilot only echoes from the instruction don't count:
    'when ready for push, wilco' repeats Delivery's 'when ready', and 'departure 120.6' is a frequency."""
    t = pilot_norm
    if "when ready" in atc_norm:
        t = t.replace("when ready", " ")
    t = re.sub(r"\bdeparture (?:frequency|on|\d)", " ", t)
    return any(w in t for w in _REQUEST_WORDS)


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


def _joined(t: str) -> str:
    """Normalized text with letters split from digits and digit runs joined: 'to nh1015' -> 'to nh 1015'."""
    return re.sub(r"(?<=\d) (?=\d)", "", re.sub(r"(?<=[a-z])(?=\d)", " ", t))


def _code(t: str, word: str) -> str | None:
    """4-digit value after a keyword in normalized text: 'qnh 1 0 1 5' -> '1015', 'squawk 2 2 3 5' -> '2235'."""
    m = re.search(rf"\b{word} ((?:\d ?){{4}})", t)
    return m.group(1).replace(" ", "") if m else None


# ICAO Doc 4444 4.5.7.5: what must be read back. A "roger" to any of these gets "read back".
_READBACK_REQUIRED = ("taxi", "cleared", "hold short", "line up", "cross", "backtrack", "squawk", "qnh", "altimeter",
                      "climb", "descend", "heading", "maintain")


def needs_readback(atc_text: str | None) -> bool:
    if not atc_text:
        return False
    t = _normalize(atc_text)
    if "readback correct" in t:
        return False
    return any(re.search(rf"\b{w}\b", t) for w in _READBACK_REQUIRED)


def _items(text: str, letters_ok: bool = False) -> dict[str, str | bool | frozenset]:
    t = _normalize(text)
    items: dict[str, str | bool | frozenset] = {}
    rwy = _runway(t)
    if rwy:
        items["runway"] = rwy
    route = _taxi_route(t, letters_ok)
    if route:
        items["taxi route"] = route
    for key, word in (("qnh", "qnh"), ("qnh", "altimeter"), ("squawk", "squawk")):
        val = _code(t, word)
        if val:
            items[key] = val
    # FAA "hold short" must be read back. ICAO "taxi to holding point runway 31" is a clearance limit: the runway
    # read back is enough ("three one via alfa"), so "holding point" is not an item of its own.
    if "hold short" in t or "holding short" in t:
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
    if _requests(t, _normalize(last_atc)) or "?" in pilot_text:
        return False
    if any(w in t for w in _ACK_WORDS):
        return True
    said = {_stem(w) for w in _normalize(last_atc).split()}
    skip = {w.lower() for w in ignore}
    words = [_stem(w) for w in t.split() if not w.isdigit() and len(w) > 2 and w not in skip]
    return bool(words) and sum(w in said for w in words) / len(words) >= 0.7


_FILLER = {"roger", "wilco", "copied", "copy", "that", "thanks", "thank", "you", "good", "day", "afternoon",
            "morning", "evening", "ok", "okay", "will", "do", "affirm", "yes"}


def _pure_ack(pilot_text: str, ignore: tuple[str, ...]) -> bool:
    """Only 'roger' / 'wilco' / greetings and the callsign: nothing of the instruction was said back."""
    skip = {w.lower() for w in ignore}
    words = [w for w in _normalize(pilot_text).split() if not w.isdigit() and w not in skip and w not in _FILLER]
    return not words


def readback_missing(atc_text: str | None, pilot_text: str, ignore: tuple[str, ...] = ()) -> bool:
    """The instruction needed a readback, but the pilot only acknowledged it. Call it when check_readback()
    returned "none" (the pilot repeated none of the checkable items)."""
    if not needs_readback(atc_text) or not is_acknowledgement(atc_text, pilot_text, ignore):
        return False
    if _items(atc_text):  # runway / taxiways / QNH / squawk to read back, and none of them came back
        return True
    return _pure_ack(pilot_text, ignore)  # e.g. "climb via SID": repeating it is fine, a bare "roger" is not


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
    if _requests(pilot_norm, _normalize(last_atc)):
        return ReadbackResult("none")
    got = _items(pilot_text, letters_ok=True)
    if "runway" in expected and "runway" not in got and _bare_runway(pilot_norm, str(expected["runway"])):
        got["runway"] = expected["runway"]
    joined = _joined(pilot_norm)
    for key in ("qnh", "squawk"):  # pilots say the number without the word ("one zero one five", "to NH1015")
        if key in expected and got.get(key) != expected[key] and re.search(rf"\b{expected[key]}\b", joined):
            got[key] = expected[key]
    if not got:  # nothing of the instruction repeated: not a readback attempt
        return ReadbackResult("none")
    missing = []
    for key, val in expected.items():
        if key in ("qnh", "squawk"):
            if got.get(key) != val:
                missing.append(f"{key} {val}")
        elif key == "runway":
            if got.get("runway") != val:
                missing.append(f"runway {val}")
        elif key == "taxi route":
            if not val <= got.get("taxi route", frozenset()):
                missing.append("taxi route")
        elif key not in got:
            missing.append(key)
    return ReadbackResult("incomplete" if missing else "correct", missing)
