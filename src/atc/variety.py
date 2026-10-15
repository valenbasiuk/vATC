"""Natural variety on the radio (Valen 2026-10-15: "priorizar la variancia de comunicaciones"): real pilots don't
read back the same way twice, and no two pilots sound like the same script.

Applied only when the chatter with AI aircraft (sim AI and our own traffic) is SPOKEN (main._Callbacks._chatter):
the exchanges the code builds (chatter.exchange_for, own.manager) stay exact and tested, the radio gets the human
variety. The user's own code-owned replies are never touched (they are checked word for word, and a controller
talking to you should be predictable).

Every value read back stays in the readback (runway, frequency, level, taxi route, callsign): only the order, the
optional words and the courtesies change. Two layers:
  - a STYLE per pilot (from the callsign, the same for the whole session): terse or chatty, callsign first or last,
    says "runway" or not, greets on first contact, a few "uh"s, signs off in their own way (a British "cheers", an
    Argentine "buen día" to an Argentine controller);
  - a little chance per transmission on top (the same pilot doesn't say it identically every time).
Controllers vary their sign-offs ("good day", "bye bye", "have a good flight") per position.
A rare "say again" from a pilot and the controller repeating it (SAY_AGAIN_P).

Timing lives in chatter.RadioBus (gaps between transmissions) and main._chatter (the pilot's reaction time before
a readback): see REPLY_DELAY_S.
"""

from __future__ import annotations

import random
import re
import zlib
from dataclasses import dataclass

ENABLED = True  # tests switch it off (conftest) where they check exact chatter
RNG = random.Random()
SAY_AGAIN_P = 0.03  # a pilot asks "say again" (and the controller repeats) this often
REPLY_DELAY_S = (0.4, 1.6)  # a pilot's reaction time before the readback (audio only)

_SPANISH = {"AR", "ES", "MX", "CL", "CO", "PE", "UY", "PY", "BO", "EC", "VE", "PA", "CR", "DO", "CU", "GT", "HN",
            "SV", "NI", "PR"}
_BRITISH = {"GB", "IE", "AU", "NZ", "ZA"}


@dataclass(frozen=True)
class Style:
    terse: bool  # drops optional words ("runway", "good day")
    callsign_first: float  # chance to put the callsign first in a readback
    drop_runway: float  # "cleared to land one three"
    reorder: float  # "runway one three, cleared to land"
    filler: float  # "uh" in a first call
    greets: bool  # "good morning" on first contact
    signoff: str  # their usual goodbye


def style(callsign: str, country: str | None = None) -> Style:
    """A pilot's habits, the same for the whole session (hashed from the callsign)."""
    h = zlib.crc32(callsign.upper().encode())
    terse = h % 3 == 0
    c = (country or "").upper()
    signoffs = ["good day", "good day", "bye bye", "thank you", "so long"]
    if c in _BRITISH:
        signoffs += ["cheers", "cheers", "ta-ta"]
    if c == "US":
        signoffs += ["have a good one", "see ya"]
    return Style(terse=terse,
                 callsign_first=(0.05, 0.15, 0.3)[(h >> 3) % 3],
                 drop_runway=0.35 if terse else 0.1,
                 reorder=(0.1, 0.25, 0.4)[(h >> 6) % 3],
                 filler=0.0 if terse else (0.05, 0.12, 0.2)[(h >> 9) % 3],
                 greets=not terse and (h >> 12) % 2 == 0,
                 signoff=signoffs[(h >> 14) % len(signoffs)])


def _chance(p: float, rng: random.Random) -> bool:
    return rng.random() < p


def _split_callsign(text: str, cs: str) -> tuple[str, str] | None:
    """('Cleared to land runway one three', '.') when `text` ends with ', <cs>.' (case-insensitive)."""
    m = re.fullmatch(rf"(.+?),\s*{re.escape(cs)}([.?!]?)", text.strip(), flags=re.IGNORECASE)
    return (m.group(1), m.group(2) or ".") if m else None


_CLEARANCE = re.compile(r"^(cleared to land|cleared for (?:immediate )?takeoff|line up and wait) runway ([\w ]+?)$",
                        re.IGNORECASE)
_HANDOFF = re.compile(r"^(?:(?P<name>[A-Za-z][A-Za-z ]*?) )?(?P<freq>(?:one|two|three) (?:\w+ ){1,6}?\w+)"
                      r"(?P<bye>, good day)?$", re.IGNORECASE)
_FREQ_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "niner", "nine", "zero", "decimal",
               "point"}


def _is_frequency(words: str) -> bool:
    w = words.lower().split()
    return len(w) >= 3 and all(x in _FREQ_WORDS for x in w) and ("decimal" in w or "point" in w or len(w) >= 4)


def _clearance_body(body: str, st: Style, rng: random.Random) -> str:
    m = _CLEARANCE.match(body)
    if m is None:
        return body
    what, rw = m.group(1), m.group(2)
    what = what[:1].lower() + what[1:]
    if _chance(st.reorder, rng):
        return f"runway {rw}, {what}"
    if _chance(st.drop_runway, rng):
        return f"{what} {rw}"
    return f"{what} runway {rw}"


def _handoff_body(body: str, st: Style, rng: random.Random, spanish_pair: bool) -> str:
    """'Departure one two zero decimal six, good day' and 'Ground one two one decimal niner' read back other ways."""
    m = _HANDOFF.match(body)
    if m is None or not _is_frequency(m.group("freq")):
        return body
    name, freq = (m.group("name") or "").strip(), m.group("freq")
    bye = st.signoff
    if spanish_pair and _chance(0.3, rng):
        bye = rng.choice(["buen día", "buenas", "chau"])
    r = rng.random()
    if st.terse:
        parts = [freq] if r < 0.6 else [name, freq] if name else [freq]
        return " ".join(parts)
    if r < 0.15 and name:
        out = f"over to {name.lower()} {freq}"
    elif r < 0.35:
        out = freq
    else:
        out = f"{name} {freq}" if name else freq
    if m.group("bye") or _chance(0.5, rng):
        out += f", {bye}"
    return out


def _sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:]


def pilot_line(text: str, cs: str, country: str | None = None, atc_country: str | None = None,
               first_call: bool = False, rng: random.Random | None = None) -> str:
    """One AI pilot transmission, said their way. Values never change; `cs` is the spoken callsign in the text."""
    if not ENABLED:
        return text
    rng = rng or RNG
    st = style(cs, country)
    spanish_pair = (country or "").upper() in _SPANISH and (atc_country or "").upper() in _SPANISH
    parts = _split_callsign(text, cs)
    if parts is None:  # an initial call: "<station>, <callsign>, request taxi"
        return _initial_call(text, cs, st, rng, first_call)
    body, end = parts
    new = _clearance_body(body, st, rng)
    if new == body:
        new = _handoff_body(body, st, rng, spanish_pair)
    if new == body and _chance(0.15 if st.terse else 0.0, rng):
        new = re.sub(r"\bholding point runway\b", "holding point", new, flags=re.IGNORECASE)
    if _chance(st.callsign_first, rng):
        return _sentence(f"{cs}, {new}{end}")
    return _sentence(f"{new}, {cs}{end}")


def _greeting(rng: random.Random) -> str:
    return rng.choice(["good day", "good day", "hello"])  # no morning / evening: the local time is not known here


def _initial_call(text: str, cs: str, st: Style, rng: random.Random, first_call: bool) -> str:
    m = re.fullmatch(rf"(?P<station>[^,]+), (?P<cs>{re.escape(cs)}), (?P<rest>.+?)([.?!]?)", text.strip(),
                     flags=re.IGNORECASE)
    if m is None:
        return text
    station, rest = m.group("station"), m.group("rest")
    if first_call and st.greets and _chance(0.5, rng):
        station = f"{station}, {_greeting(rng)}"
    if _chance(st.filler, rng):
        rest = f"uh, {rest}"
    return _sentence(f"{station}, {cs}, {rest}.")


_ATC_BYE = re.compile(r", good day\.$")


def atc_line(text: str, station_key: str, faa: bool = False, rng: random.Random | None = None) -> str:
    """A controller's transmission to an AI aircraft: the sign-off in that controller's way."""
    if not ENABLED or not _ATC_BYE.search(text):
        return text
    rng = rng or RNG
    h = zlib.crc32(station_key.encode())
    own = (["good day", "bye bye", "have a good flight"] if not faa else
           ["good day", "have a good one", "so long"])[h % 3]
    pick = own if rng.random() < 0.6 else rng.choice(["good day", own, ""])
    return _ATC_BYE.sub(f", {pick}." if pick else ".", text)


def say_again(lines: list[tuple[str, str, object]], rng: random.Random | None = None) \
        -> list[tuple[str, str, object]]:
    """Now and then a pilot misses the controller: "<callsign>, say again" and the controller repeats."""
    if not ENABLED or len(lines) < 2 or lines[0][0] != "ATC" or lines[1][0] == "ATC":
        return lines
    rng = rng or RNG
    if rng.random() >= SAY_AGAIN_P:
        return lines
    who, voice = lines[1][0], lines[1][2]
    ask = rng.choice([f"{who}, say again.", f"Say again for {who}.", f"Sorry, say again, {who}."])
    return [lines[0], (who, ask, voice), ("ATC", lines[0][1], lines[0][2])] + lines[1:]
