"""Check an LLM reply against what the model was told. Anything it states that is not in its input
(a number, an aircraft type, a taxiway when no route was given) is an invention, and the reply is rejected.

Numbers are compared as digit strings: "one two one decimal niner" -> "1219", which must appear inside some
number of the input ("121.900" -> "121900"). Single digits are skipped (clock positions, "number two").
"""

from __future__ import annotations

import re

from atc.readback import _NATO_WORDS, _normalize, join_digits

_TYPE_WORDS = ("airbus", "boeing", "embraer", "cessna", "piper", "cirrus", "fokker", "atr", "canadair",
               "bombardier", "learjet", "citation", "king air", "dash", "a320", "a321", "b737", "b738", "seven three seven")
_UNITS = {"thousand": "000", "hundred": "00"}


def _numbers(text: str) -> list[str]:
    """Digit groups, with 'four thousand' -> '4000' and spoken digit runs joined (comma ends a number)."""
    out: list[str] = []
    for part in re.split(r"[,;]", text):
        t = _normalize(part)
        t = re.sub(r"(\d) (thousand|hundred)\b", lambda m: m.group(1) + _UNITS[m.group(2)], t)
        out += re.findall(r"\d+", join_digits(t))
    return out


def _same_number(n: str, k: str) -> bool:
    """n (said) is k (given), allowing the zeros a controller drops: FL200 for 20000 ft (two zeros) and
    frequencies ('11885' for 118.850). One dropped zero is NOT enough otherwise: FL100 is not 1000 ft AGL."""
    if n == k:
        return True
    if not k.startswith(n) or k[len(n):].strip("0"):
        return False
    return len(k) - len(n) >= 2 or (len(n) >= 4 and re.fullmatch(r"1[1-3]\d+", n) is not None)


def problems(reply: str, sources: list[str]) -> list[str]:
    """What the reply states that none of `sources` (system prompt, context, history, pilot call) contains."""
    corpus = " ".join(sources)
    known = set(_numbers(corpus))
    low_corpus = corpus.lower()
    found = []
    for n in _numbers(reply):
        if len(n) < 2:
            continue
        # whole numbers only, trailing zeros allowed: "1206" = 120.600, "200" = 20000 (FL200), but "300" is NOT in
        # 129.300 (that loose match let "wind three zero zero" through for a 030 wind)
        if not any(_same_number(n, k) for k in known):
            found.append(f"number {n}")
    low = reply.lower()
    for w in _TYPE_WORDS:
        if re.search(rf"\b{w}\b", low) and not re.search(rf"\b{w}\b", low_corpus):
            found.append(f"aircraft type '{w}'")
    if "TAXI ROUTE" not in corpus:
        m = re.search(r"\bvia ((?:\w+[ ,]*)+)", _normalize(reply))
        if m and any(w in _NATO_WORDS for w in m.group(1).split()[:3]):
            found.append("taxiway names")
    return found


def contact_problems(reply: str, stations: list[tuple[str, str]]) -> list[str]:
    """'contact <station> <frequency>' must use a frequency on file that belongs to that kind of station.
    `stations`: (group, frequency digits without trailing zeros), e.g. ('clearance', '1293'). The model once said
    'contact Rosario Center one two niner decimal three': 129.3 is on file, but it is Aeroparque Delivery."""
    from atc.facility import ROLE_WORDS

    m = re.search(r"\bcontact\b(.+)$", _normalize(reply))
    if not m:
        return []
    part = m.group(1)
    named = next((g for w, g in ROLE_WORDS.items() if re.search(rf"\b{w}\b", part)), None)
    for n in (n.rstrip("0") for n in _numbers(part) if len(n) >= 4):
        on_file = [g for g, f in stations if f == n]
        if not on_file:
            return [f"frequency {n} (not on file)"]
        if named and named not in on_file:
            return [f"frequency {n} is not a {named} frequency"]
    return []


def correction(found: list[str]) -> str:
    return ("Your reply stated " + ", ".join(found) + ", which is not in CONTEXT. Reply again using only facts "
            "from CONTEXT; leave out anything you do not have.")
