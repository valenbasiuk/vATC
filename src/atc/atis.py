"""ATIS, made by code: what you hear when COM1 or COM2 is on an airport's ATIS frequency (roadmap item 14).

    "Aeroparque information Bravo, time zero three zero zero. ILS approach, runway in use three one. Wind zero one
     zero degrees eight knots. CAVOK. Temperature one eight, dew point zero niner. QNH one zero one seven.
     Acknowledge receipt of information Bravo and advise aircraft type on first contact."

Wind and QNH: the sim's (the same values Tower gives), from a reading taken at the airport; otherwise the METAR.
Clouds, visibility, temperature, dew point, present weather: the real METAR (weather.py), which is what MSFS live
weather shows. Anything unknown is left out, never guessed. The letter moves on when the observation, the runway
in use or the QNH changes; a pilot who checks in with an old letter is told the current one (`check_letter`).
"""

from __future__ import annotations

import re
import time
import zlib
from dataclasses import dataclass

from atc import phrase, weather
from atc.geo import distance_nm
from atc.models import Airport, OwnState
from atc.runway import magnetic, runway_in_use, runways_in_use

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOCAL_NM = 15.0  # the sim's QNH/temperature are the airport's when the aircraft is this close


def letter_word(i: int) -> str:
    return phrase._NATO[LETTERS[i % 26]].title()


def _height(ft: int, faa: bool) -> str:
    """Cloud base: 800 -> 'eight hundred', 2500 -> 'two thousand five hundred', 12000 -> 'one two thousand'."""
    ft = int(round(ft / 100.0) * 100)
    th, hu = divmod(ft, 1000)
    out = " ".join(w for w in ((f"{phrase.digits(str(th))} thousand" if th else ""),
                               (f"{phrase.digits(str(hu // 100))} hundred" if hu else "")) if w)
    return out if faa else f"{out} feet"


def _temp(c: float, faa: bool = False) -> str:
    """ICAO two digits ('zero niner', 'minus zero five'), FAA as is ('niner')."""
    n = int(round(c))
    return ("minus " if n < 0 else "") + phrase.digits(str(abs(n)) if faa else f"{abs(n):02d}")


_COVER = {"FEW": "few", "SCT": "scattered", "BKN": "broken", "OVC": "overcast", "VV": "vertical visibility"}
_INTENSITY = {"-": "light", "+": "heavy"}
_DESCR = {"SH": "showers", "TS": "thunderstorm", "FZ": "freezing", "BL": "blowing", "DR": "drifting",
          "MI": "shallow", "BC": "patches of", "PR": "partial", "VC": "in the vicinity"}
_PHEN = {"RA": "rain", "DZ": "drizzle", "SN": "snow", "GR": "hail", "GS": "small hail", "PL": "ice pellets",
         "SG": "snow grains", "BR": "mist", "FG": "fog", "HZ": "haze", "FU": "smoke", "DU": "dust", "SA": "sand",
         "SQ": "squalls", "VA": "volcanic ash", "UP": "precipitation", "PO": "dust whirls", "FC": "funnel cloud",
         "SS": "sandstorm", "DS": "duststorm"}


def spoken_weather(group: str) -> str:
    """METAR present weather: '-SHRA' light rain showers, 'TSRA' thunderstorm with rain, 'VCSH' showers in the
    vicinity, 'BR' mist, 'FZFG' freezing fog."""
    inten = _INTENSITY.get(group[:1], "")
    g = group.lstrip("+-")
    codes = [g[i:i + 2] for i in range(0, len(g), 2)]
    vicinity = "VC" in codes
    codes = [c for c in codes if c != "VC"]
    descr = [c for c in codes if c in _DESCR]
    phen = [_PHEN[c] for c in codes if c in _PHEN]
    if "TS" in descr:
        words = ["thunderstorm"] + (["with " + " and ".join(phen)] if phen else [])
    elif "SH" in descr:
        words = [" and ".join(phen) + " showers" if phen else "showers"]
    else:
        words = [_DESCR[d] for d in descr if d != "SH"] + [" and ".join(phen)] if phen else [_DESCR[d] for d in descr]
    text = " ".join(w for w in [inten] + words if w)
    return f"{text} in the vicinity" if vicinity else text


def _visibility(m: int, faa: bool) -> str:
    if faa:
        if m >= 9999:
            return "visibility one zero"
        sm = m / 1609.34
        if sm < 1:
            q = max(1, round(sm * 4))
            return "visibility " + {1: "one quarter", 2: "one half", 3: "three quarters", 4: "one"}[q]
        return f"visibility {phrase.digits(str(int(round(sm))))}"
    if m >= 9999:
        return "visibility one zero kilometers or more"
    if m >= 5000:
        return f"visibility {phrase.digits(str(m // 1000))} kilometers"
    th, hu = divmod(int(round(m / 100.0) * 100), 1000)
    words = " ".join(w for w in ((f"{phrase.digits(str(th))} thousand" if th else ""),
                                 (f"{phrase.digits(str(hu // 100))} hundred" if hu else "")) if w)
    return f"visibility {words} meters"


def _clouds(metar: weather.Metar, faa: bool) -> str | None:
    if not metar.clouds:
        return None
    parts = []
    ceiling_said = False
    for c in metar.clouds:
        if c.base_ft is None:
            continue
        h = _height(c.base_ft, faa)
        kind = {"CB": " cumulonimbus", "TCU": " towering cumulus"}.get(c.kind, "")
        if faa:
            if c.cover in ("BKN", "OVC", "VV") and not ceiling_said:
                parts.append(f"ceiling {h} {_COVER[c.cover]}{kind}")
                ceiling_said = True
            elif c.cover == "FEW":
                parts.append(f"few clouds at {h}{kind}")
            else:
                parts.append(f"{h} {_COVER[c.cover]}{kind}")
        else:
            parts.append(f"{_COVER[c.cover]} {h}{kind}")
    if not parts:
        return None
    return ", ".join(parts) if faa else "clouds " + ", ".join(parts)


class AtisState:
    """Current letter per airport. The letter advances when what the ATIS says changes."""

    def __init__(self) -> None:
        self.letters: dict[str, tuple[int, tuple]] = {}

    def letter(self, icao: str, key: tuple) -> int:
        cur = self.letters.get(icao)
        if cur is None:
            start = zlib.crc32(f"{icao}{time.strftime('%Y%m%d')}".encode()) % 26
            self.letters[icao] = (start, key)
            return start
        if cur[1] != key:
            self.letters[icao] = ((cur[0] + 1) % 26, key)
        return self.letters[icao][0]


def _zulu(own: OwnState | None, metar: weather.Metar | None) -> str:
    """Observation time: the METAR's, else the sim's clock, else this PC's UTC."""
    if metar and metar.obs_time:
        return time.strftime("%H%M", time.gmtime(metar.obs_time))
    z = getattr(own, "zulu_s", None) if own is not None else None
    if z is not None:
        h, m = divmod(int(z) // 60, 60)
        return f"{h % 24:02d}{m:02d}"
    return time.strftime("%H%M", time.gmtime())


# --- which weather: the real METAR, or the sim's own (preset) weather -------------------------------------------
SIM_WX: dict[str, bool] = {}  # ICAO -> the sim flies its own weather there (the METAR doesn't match it)
QNH_TOL_HPA = 3.0
WIND_KT_TOL = 15.0


def sim_weather(airport: Airport, own: OwnState, surface_wind: tuple[float, float] | None) -> bool:
    """True when the sim isn't on live weather at this airport: near it, its QNH or wind disagree with the METAR
    (improvement plan A4: no more ATC_METAR=off by hand). Far from it, the last verdict stands."""
    icao = airport.icao
    metar = weather.get(icao)
    near = distance_nm(own.lat, own.lon, airport.lat, airport.lon) <= LOCAL_NM
    if metar is None or not near:
        return SIM_WX.get(icao, False)
    differ = False
    if own.qnh_hpa is not None and metar.qnh_hpa is not None and abs(own.qnh_hpa - metar.qnh_hpa) > QNH_TOL_HPA:
        differ = True
    if surface_wind is not None and metar.wind_kt is not None:  # (direction varies too much with live weather)
        _, wkt = surface_wind
        if wkt is not None and abs(wkt - metar.wind_kt) > WIND_KT_TOL:
            differ = True
    if differ != SIM_WX.get(icao):
        print(f"[weather at {icao}: " + ("the sim's own weather (it doesn't match the METAR)" if differ
                                         else "live METAR") + "]")
    SIM_WX[icao] = differ
    return differ


def facts(airport: Airport, own: OwnState, surface_wind: tuple[float, float] | None, preferred: str | None):
    """What the ATIS says, as values: (wind_true, wind_kt, qnh, runway, metar, local). metar is None when the sim
    flies its own weather here: then clouds / visibility / present weather come from the sim or are left out."""
    metar = weather.get(airport.icao)
    if metar is not None and sim_weather(airport, own, surface_wind):
        metar = None
    local = distance_nm(own.lat, own.lon, airport.lat, airport.lon) <= LOCAL_NM
    if surface_wind is not None:
        wdir, wkt = surface_wind
    elif metar is not None and metar.wind_kt is not None:
        wdir, wkt = metar.wind_dir_deg, metar.wind_kt
    else:
        wdir = wkt = None
    qnh = own.qnh_hpa if local and own.qnh_hpa is not None else (metar.qnh_hpa if metar else own.qnh_hpa)
    rwy = runway_in_use(airport, wdir, wkt, preferred, use="arrival")
    return wdir, wkt, qnh, rwy, metar, local


@dataclass
class Conditions:
    visibility_m: float | None  # None = unknown
    ceiling_ft: int | None  # lowest broken / overcast / vertical visibility; None = none or unknown
    weather: list[str]  # METAR present weather groups

    @property
    def reduced(self) -> bool:
        """Below VMC-ish: visibility under 5 km or a ceiling under 1500 ft (Tower crosses runways, weather said)."""
        return (self.visibility_m is not None and self.visibility_m < 5000) or \
            (self.ceiling_ft is not None and self.ceiling_ft < 1500)

    @property
    def low_visibility(self) -> bool:
        """Low-visibility procedures: under 550 m or a ceiling under 200 ft."""
        return (self.visibility_m is not None and self.visibility_m < 550) or \
            (self.ceiling_ft is not None and self.ceiling_ft < 200)


def conditions(airport: Airport, own: OwnState, surface_wind: tuple[float, float] | None = None) -> Conditions:
    """Visibility, ceiling and weather at the airport: the METAR's, or the sim's visibility on its own weather."""
    _, _, _, _, metar, local = facts(airport, own, surface_wind, None)
    if metar is not None:
        vis = 9999.0 if metar.cavok else metar.visibility_m
        ceil = min((c.base_ft for c in metar.clouds if c.cover in ("BKN", "OVC", "VV") and c.base_ft is not None),
                   default=None)
        return Conditions(vis, ceil, list(metar.weather))
    return Conditions(own.visibility_m if local else None, None, [])


def controller_weather(airport: Airport, own: OwnState, surface_wind: tuple[float, float] | None,
                       preferred: str | None, departing: bool, reply: str) -> str | None:
    """Where there is no ATIS (SARC), the controller gives the weather on first contact, VATSIM style:
    departing "runway two zero in use, wind ..., visibility ..., temperature two four, QNH ..."; arriving "expect
    ILS approach runway two zero, wind ..., QNH ...", visibility and ceiling only when below VMC. Items the reply
    already has (the runway, the QNH) are not said twice."""
    from atc.navdb import approach_type

    faa = airport.faa
    wdir, wkt, qnh, rwy, metar, local = facts(airport, own, surface_wind, preferred)
    cond = conditions(airport, own, surface_wind)
    low = reply.lower()
    parts: list[str] = []
    rw = phrase.runway(rwy.ident, faa) if rwy else None
    if rw and f"runway {rw}" not in low:
        if departing:
            parts.append(f"runway {rw} in use" if not faa else f"runway {rw}")
        else:
            app = approach_type(airport, rwy.ident)
            parts.append(f"expect {app} approach runway {rw}" if app else f"expect runway {rw}")
    wind = phrase.wind(magnetic(airport, wdir), wkt, faa) if wkt is not None else None
    if wind and "wind" not in low:
        parts.append(wind)
    if cond.visibility_m is not None and (cond.visibility_m < 10000 or not departing and cond.reduced):
        if cond.visibility_m < 9999:
            parts.append(_visibility(int(cond.visibility_m), faa))
    if metar is not None and (cond.reduced or any("TS" in w or "FG" in w for w in cond.weather)):
        wx = ", ".join(spoken_weather(w) for w in metar.weather)
        if wx:
            parts.append(wx)
        sky = _clouds(metar, faa)
        if sky:
            parts.append(sky)
    temp = metar.temp_c if metar and metar.temp_c is not None else (own.temp_c if local else None)
    if departing and temp is not None:
        parts.append(f"temperature {_temp(temp, faa)}")
    if qnh and "qnh" not in low and "altimeter" not in low:
        parts.append(f"altimeter {phrase.digits(f'{qnh * 0.02953:.2f}')}" if faa else f"QNH {phrase.digits(f'{qnh:.0f}')}")
    return ", ".join(parts) if parts else None


def build(state: AtisState, airport: Airport, own: OwnState, surface_wind: tuple[float, float] | None = None,
          preferred: str | None = None) -> tuple[str, list[str]]:
    """(letter word, sentences) of the current ATIS."""
    from atc.navdb import approach_type

    faa = airport.faa
    wdir, wkt, qnh, rwy, metar, local = facts(airport, own, surface_wind, preferred)
    key = (metar.obs_time if metar else time.gmtime().tm_hour, rwy.ident if rwy else None,
           int(round(qnh)) if qnh else None)
    idx = state.letter(airport.icao, key)
    word = letter_word(idx)
    from atc.airports.gen import spoken_name

    name = airport.spoken_name or spoken_name(airport.name) or airport.name
    rw = phrase.runway(rwy.ident, faa) if rwy else None
    # separate landing and departure runways (YAML runway_configs: KSFO lands 28L/28R, departs 1L/1R)
    arr, dep = (runways_in_use(airport, wdir, wkt, use) for use in ("arrival", "departure"))
    split = bool(arr and dep) and {r.ident for r in arr} != {r.ident for r in dep}
    landing = " and ".join(phrase.runway(r.ident, faa) for r in arr) if split else None
    departing = " and ".join(phrase.runway(r.ident, faa) for r in dep) if split else None
    app = approach_type(airport, rwy.ident) if rwy else None
    wind = phrase.wind(magnetic(airport, wdir), wkt, faa) if wkt is not None else None
    if wind and metar and metar.gust_kt and surface_wind is None:
        wind += f" gusting {phrase.digits(str(int(metar.gust_kt)))}" + ("" if faa else " knots")
    temp = metar.temp_c if metar and metar.temp_c is not None else \
        (getattr(own, "temp_c", None) if local else None)
    dew = metar.dew_c if metar else None
    s: list[str] = []
    if faa:
        z = phrase.digits(_zulu(own, metar))
        s.append(f"{name} information {word}. {z[:1].upper()}{z[1:]} Zulu.")
        if wind:
            s.append(f"{wind[:1].upper()}{wind[1:]}.")
        if metar and metar.visibility_m is not None:
            s.append(f"{_visibility(metar.visibility_m, True).capitalize()}.")
        if metar:
            wx = ", ".join(spoken_weather(w) for w in metar.weather)
            if wx:
                s.append(f"{wx[:1].upper()}{wx[1:]}.")
            sky = _clouds(metar, True)
            s.append(f"{sky[:1].upper()}{sky[1:]}." if sky else ("Sky clear." if metar.visibility_m else ""))
        if temp is not None:
            s.append(f"Temperature {_temp(temp, True)}" + (f", dew point {_temp(dew, True)}." if dew is not None else "."))
        if qnh:
            s.append(f"Altimeter {phrase.digits(f'{qnh * 0.02953:.2f}')}.")
        if rw:
            if app:
                s.append(f"{app} runway {rw} approach in use.")
            s.append(f"Landing runway{'s' if len(arr) > 1 else ''} {landing}, departing runway"
                     f"{'s' if len(dep) > 1 else ''} {departing}." if split else f"Landing and departing runway {rw}.")
        s.append(f"Advise on initial contact you have information {word}.")
    else:
        s.append(f"{name} information {word}, time {phrase.digits(_zulu(own, metar))}.")
        if rw:
            if split:
                s.append((f"{app} approach. " if app else "") + f"Landing runway {landing}, departure runway {departing}.")
            else:
                s.append((f"{app} approach, runway in use {rw}." if app else f"Runway in use {rw}."))
        if wind:
            s.append(f"{wind[:1].upper()}{wind[1:]}.")
        if metar is None and local and own.visibility_m is not None:  # the sim's own weather: its visibility
            s.append(f"{_visibility(int(min(own.visibility_m, 9999)), False).capitalize()}.")
        if metar and metar.cavok:
            s.append("CAVOK.")
        elif metar:
            if metar.visibility_m is not None:
                s.append(f"{_visibility(metar.visibility_m, False).capitalize()}.")
            wx = ", ".join(spoken_weather(w) for w in metar.weather)
            if wx:
                s.append(f"{wx[:1].upper()}{wx[1:]}.")
            sky = _clouds(metar, False)
            if sky:
                s.append(f"{sky[:1].upper()}{sky[1:]}.")
            elif metar.visibility_m:
                s.append("No significant clouds.")
        if temp is not None:
            s.append(f"Temperature {_temp(temp)}" + (f", dew point {_temp(dew)}." if dew is not None else "."))
        if qnh:
            s.append(f"QNH {phrase.digits(f'{qnh:.0f}')}.")
        s.append(f"Acknowledge receipt of information {word} and advise aircraft type on first contact.")
    return word, [x for x in s if x]


_INFO = re.compile(r"\b(?:information|info)\s+([a-z]+)\b")
_WITH = re.compile(r"\bwith\s+([a-z]+)\b")


def heard_letter(norm: str) -> str | None:
    """'... with information bravo' / 'information b' -> 'B'. None if no ATIS letter was mentioned."""
    words = {v: k for k, v in phrase._NATO.items()}
    words.update({"alpha": "A", "juliet": "J", "whisky": "W", "xray": "X"})
    for m in _INFO.finditer(norm):  # "information bravo", "info b"
        w = m.group(1)
        if w in words:
            return words[w]
        if len(w) == 1 and w.isalpha():
            return w.upper()
    for m in _WITH.finditer(norm):  # "with bravo"
        if m.group(1) in words:
            return words[m.group(1)]
    return None


def check_letter(state: AtisState, airport: Airport, own: OwnState, norm: str,
                 surface_wind: tuple[float, float] | None, preferred: str | None) -> str | None:
    """On first contact: a pilot with an old ATIS letter gets 'information Charlie is now current, QNH ...'."""
    said = heard_letter(norm)
    if said is None:
        return None
    if airport.icao not in state.letters:  # never played here: take the pilot's letter as the current one
        build(state, airport, own, surface_wind, preferred)
        state.letters[airport.icao] = (LETTERS.index(said), state.letters[airport.icao][1])
        return None
    word, _ = build(state, airport, own, surface_wind, preferred)
    if said == word[0].upper():
        return None
    return current_note(state, airport, own, surface_wind, preferred, now=True)


def current_note(state: AtisState, airport: Airport, own: OwnState, surface_wind: tuple[float, float] | None,
                 preferred: str | None, now: bool = False) -> str:
    """'information Charlie is (now) current, QNH one zero one three'."""
    word, _ = build(state, airport, own, surface_wind, preferred)
    _, _, qnh, _, _, _ = facts(airport, own, surface_wind, preferred)
    alt = (f", altimeter {phrase.digits(f'{qnh * 0.02953:.2f}')}" if airport.faa else
           f", QNH {phrase.digits(f'{qnh:.0f}')}") if qnh else ""
    return f"information {word} is {'now ' if now else ''}current{alt}"


# ask for the ATIS letter when a first call has none (tests turn it off; the launcher: ATC_ATIS_QUESTION=0)
ENFORCE = __import__("os").environ.get("ATC_ATIS_QUESTION", "1") != "0"
CONTROLLER_WEATHER = True  # no ATIS: the controller gives the weather on first contact (tests: off)


def confirm_question(word: str, faa: bool) -> str:
    """Asked when a pilot's first call names no ATIS letter."""
    return f"verify you have information {word}" if faa else f"confirm information {word}"
