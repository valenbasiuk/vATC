"""Radio pronunciation done in code, so values are spoken the same way every time (ICAO Doc 9432 style)."""

from __future__ import annotations

_DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "niner"]
_NATO = {
    "A": "alfa", "B": "bravo", "C": "charlie", "D": "delta", "E": "echo", "F": "foxtrot", "G": "golf",
    "H": "hotel", "I": "india", "J": "juliett", "K": "kilo", "L": "lima", "M": "mike", "N": "november",
    "O": "oscar", "P": "papa", "Q": "quebec", "R": "romeo", "S": "sierra", "T": "tango", "U": "uniform",
    "V": "victor", "W": "whiskey", "X": "x-ray", "Y": "yankee", "Z": "zulu",
}


def digits(s: str) -> str:
    """'2235' -> 'two two three five'."""
    return " ".join(_DIGIT_WORDS[int(c)] for c in str(s) if c.isdigit())


def frequency(mhz: float, faa: bool = False) -> str:
    """121.9 -> 'one two one decimal niner'; 118.85 -> 'one one eight decimal eight five'."""
    whole, frac = f"{mhz:.3f}".split(".")
    frac = frac.rstrip("0") or "0"
    return f"{digits(whole)} {'point' if faa else 'decimal'} {digits(frac)}"


def level(ft: int) -> str:
    """20000 -> 'flight level two zero zero'; 3000 -> 'three thousand feet'; 2500 -> 'two thousand five hundred feet'."""
    if ft >= 10000:
        return "flight level " + digits(str(ft // 100))
    th, hu = divmod(ft, 1000)
    out = f"{digits(str(th))} thousand" if th else ""
    if hu:
        out += f" {digits(str(hu // 100))} hundred"
    return f"{out.strip()} feet"


def wind(direction: float | None, kt: float | None) -> str | None:
    """(300, 12) -> 'wind three zero zero degrees one two knots'; under 3 kt -> 'wind calm'; unknown -> None."""
    if direction is None or kt is None:
        return None
    if kt <= 3:
        return "wind calm"
    d = int(round(direction)) % 360 or 360
    return f"wind {digits(f'{d:03d}')} degrees {digits(str(int(round(kt))))} knots"


def runway(ident: str, faa: bool = False) -> str:
    """'31' -> 'three one', '09L' -> 'zero niner left' (ICAO); FAA drops the leading zero: 'niner left'."""
    side = {"L": " left", "R": " right", "C": " center"}.get(ident[-1:].upper(), "")
    num = "".join(c for c in ident if c.isdigit())
    return digits(num.lstrip("0") or "0" if faa else num) + side


def procedure(name: str) -> str:
    """SID/STAR name: 'ATOVO4B' -> 'ATOVO four bravo' (the fix name is said as a word)."""
    head = name.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if not head:  # no digit in it: not a coded procedure name
        return name
    suffix = name[len(head):]
    base = head.rstrip("0123456789")
    number = head[len(base):]
    parts = [base] + ([digits(number)] if number else []) + [_NATO[c] for c in suffix]
    return " ".join(p for p in parts if p)


def callsign(telephony: str | None, icao_callsign: str) -> str:
    """('Martinair', 'MAR4133') -> 'Martinair four one three three'. Without telephony, spell it:
    'LV-ABC' -> 'Lima Victor Alfa Bravo Charlie', 'N123AB' -> 'November one two three Alfa Bravo'."""
    num = "".join(c for c in icao_callsign if c.isdigit())
    if telephony and num:
        return f"{telephony} {digits(num)}"
    return spell(icao_callsign)


def spell(text: str) -> str:
    """Letters in the ICAO alphabet, digits one by one; everything else dropped."""
    out = []
    for c in text.upper():
        if c.isdigit():
            out.append(_DIGIT_WORDS[int(c)])
        elif c in _NATO:
            out.append(_NATO[c].title())
    return " ".join(out)
