"""Check an LLM reply against what the model was told. Anything it states that is not in its input
(a number, an aircraft type, a taxiway when no route was given) is an invention, and the reply is rejected.

Numbers are compared as digit strings: "one two one decimal niner" -> "1219", which must appear inside some
number of the input ("121.900" -> "121900"). Single digits are skipped (clock positions, "number two").
"""

from __future__ import annotations

import re

from atc.readback import _NATO_WORDS, _normalize

_TYPE_WORDS = ("airbus", "boeing", "embraer", "cessna", "piper", "cirrus", "fokker", "atr", "canadair",
               "bombardier", "learjet", "citation", "king air", "dash", "a320", "a321", "b737", "b738", "seven three seven")
_UNITS = {"thousand": "000", "hundred": "00"}


def _numbers(text: str) -> list[str]:
    """Digit groups, with 'four thousand' -> '4000' and spoken digit runs joined (comma ends a number)."""
    out: list[str] = []
    for part in re.split(r"[,;]", text):
        t = _normalize(part)
        t = re.sub(r"(\d) (thousand|hundred)\b", lambda m: m.group(1) + _UNITS[m.group(2)], t)
        t = re.sub(r"(?<=\d) (?:(?:decimal|point) )?(?=\d)", "", t)
        out += re.findall(r"\d+", t)
    return out


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
        if not any(k == n or (k.startswith(n) and not k[len(n):].strip("0")) for k in known):
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


def correction(found: list[str]) -> str:
    return ("Your reply stated " + ", ".join(found) + ", which is not in CONTEXT. Reply again using only facts "
            "from CONTEXT; leave out anything you do not have.")
