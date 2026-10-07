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


def is_flight_level(ft: int, airport=None) -> bool:
    """Above the transition altitude levels are flight levels: US from FL180, Argentina above 3000 ft, UK 6000...
    Without a known transition altitude: from 10000 ft (FAA: 18000)."""
    faa = bool(getattr(airport, "faa", False))
    ta = getattr(airport, "trans_alt_ft", None)
    if faa:
        return ft >= (ta or 18000)
    return ft > ta if ta else ft >= 10000


def level(ft: int, airport=None) -> str:
    """20000 -> 'flight level two zero zero'; 3000 -> 'three thousand feet'; 2500 -> 'two thousand five hundred feet'.
    `airport` (its transition altitude, FAA or not) decides FL vs feet: FAA 11000 -> 'one one thousand'
    (FAA altitudes are said without 'feet')."""
    if is_flight_level(ft, airport):
        return "flight level " + digits(str(ft // 100))
    th, hu = divmod(int(ft), 1000)
    out = f"{digits(str(th))} thousand" if th else ""
    if hu:
        out += f" {digits(str(hu // 100))} hundred"
    return out.strip() if getattr(airport, "faa", False) else f"{out.strip()} feet"


def climb(ft: int, airport=None) -> str:
    """ICAO 'climb to flight level two zero zero' / FAA 'climb and maintain one one thousand'."""
    return f"climb and maintain {level(ft, airport)}" if getattr(airport, "faa", False) else \
        f"climb to {level(ft, airport)}"


def descend(ft: int, airport=None) -> str:
    return f"descend and maintain {level(ft, airport)}" if getattr(airport, "faa", False) else \
        f"descend to {level(ft, airport)}"


def wind(direction: float | None, kt: float | None, faa: bool = False) -> str | None:
    """(300, 12) -> ICAO 'wind three zero zero degrees one two knots', FAA 'wind three zero zero at one two';
    under 3 kt -> 'wind calm'; unknown -> None."""
    if direction is None or kt is None:
        return None
    if kt <= 3:
        return "wind calm"
    d = int(round(direction / 10.0) * 10) % 360 or 360  # reported in steps of 10 degrees (ICAO Annex 3)
    if faa:
        return f"wind {digits(f'{d:03d}')} at {digits(str(int(round(kt))))}"
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


def callsign(telephony: str | None, icao_callsign: str, faa: bool = False) -> str:
    """('Martinair', 'MAR4133') -> 'Martinair four one three three'. Without telephony, spell it:
    'LV-ABC' -> 'Lima Victor Alfa Bravo Charlie', 'N123AB' -> 'November one two three Alfa Bravo'.
    FAA (7110.65 2-4-20): airline flight numbers in group form, 'United four thirty-six'."""
    num = "".join(c for c in icao_callsign if c.isdigit())
    if telephony and num:
        return f"{telephony} {group_number(num) if faa else digits(num)}"
    return spell(icao_callsign)


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def _pair(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + (f"-{_ONES[o]}" if o else "")


def group_number(num: str) -> str:
    """Flight number in group form: '5' five, '52' fifty-two, '436' four thirty-six, '4133' forty-one thirty-three,
    '100' one hundred, '1200' twelve hundred, '205' two zero five, '1005' ten zero five."""
    num = num.lstrip("0") or "0"
    n = int(num)
    if len(num) <= 2:
        return _pair(n)
    if len(num) == 3:
        head, tail = int(num[0]), int(num[1:])
        if tail == 0:
            return f"{_ONES[head]} hundred"
        return f"{_ONES[head]} {_pair(tail) if tail >= 10 else 'zero ' + _ONES[tail]}"
    if len(num) == 4:
        head, tail = int(num[:2]), int(num[2:])
        if tail == 0:
            return f"{_pair(head)} hundred" if head % 10 else f"{_ONES[head // 10]} thousand"
        return f"{_pair(head)} {_pair(tail) if tail >= 10 else 'zero ' + _ONES[tail]}"
    return digits(num)


_CARDINAL = ["north", "east", "south", "west"]
PUSH_STYLES_ICAO = ("tail_cardinal", "facing", "tail_side")
PUSH_STYLES_FAA = ("tail_cardinal", "facing")


def push_direction(nose_before: float, nose_after: float, style: str) -> str:
    """Where the tug leaves the aircraft, the way controllers say it:
    "tail east" (where the tail ends up pointing), "facing west" (where the nose does), "tail left" / "tail right"
    (which way the tail swings, seen from the cockpit: nose turning right = tail left). A side is only said for a
    clear turn (45-135 degrees); otherwise the compass form."""
    swing = (nose_after - nose_before + 180.0) % 360.0 - 180.0  # + = the nose ends up to the right
    if style == "tail_side" and 45.0 <= abs(swing) <= 135.0:
        return "tail left" if swing > 0 else "tail right"
    if style == "facing":
        return f"facing {_CARDINAL[round(nose_after / 90.0) % 4]}"
    return f"tail {_CARDINAL[round(((nose_after + 180.0) % 360.0) / 90.0) % 4]}"


def spell(text: str) -> str:
    """Letters in the ICAO alphabet, digits one by one; everything else dropped."""
    out = []
    for c in text.upper():
        if c.isdigit():
            out.append(_DIGIT_WORDS[int(c)])
        elif c in _NATO:
            out.append(_NATO[c].title())
    return " ".join(out)
