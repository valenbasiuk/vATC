"""Who sounds like what: a voice per AI pilot (by airline or registration country) and per controller position (by
airport country), from every Piper voice in voices/ (python tools/download_voices.py fetches a good set).

Accent pools come from the multi-speaker models' speaker lists:
  - en_GB-vctk-medium: 109 native speakers tagged by accent (English, Scottish, Irish, American, Canadian,
    Australian, New Zealand, South African, Indian, Welsh...), from the VCTK corpus' speaker-info;
  - en_US-l2arctic-medium: 24 non-native speakers tagged by first language (Spanish, Mandarin, Hindi, Korean,
    Arabic, Vietnamese): an Aerolineas pilot or an Argentine controller sounds like one;
  - en_US-libritts(-r)-high: 904 US speakers, no tags: the generic pool;
  - single-speaker voices count for their locale (en_GB -> English, en_US -> American).
The same aircraft / position always gets the same voice (hashed), and voices that aren't downloaded are simply
not in the pools: with only libritts everything falls back to it, as before.
"""

from __future__ import annotations

import csv
import json
import re
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Voice:
    model: str  # .onnx path
    speaker: int | None  # speaker id in a multi-speaker model
    rate: float = 1.0  # Piper length_scale (< 1 = faster)
    accent: str = ""
    female: bool = False

    @property
    def label(self) -> str:
        return f"{Path(self.model).stem}#{self.speaker} {self.accent}{' F' if self.female else ''}".strip()


# VCTK speaker-info (speaker -> accent, F = female); s5 is a British female reader.
_VCTK = {
    "p225": "English F", "p226": "English", "p227": "English", "p228": "English F", "p229": "English F",
    "p230": "English F", "p231": "English F", "p232": "English", "p233": "English F", "p234": "Scottish F",
    "p236": "English F", "p237": "Scottish", "p238": "NorthernIrish F", "p239": "English F", "p240": "English F",
    "p241": "Scottish", "p243": "English", "p244": "English F", "p245": "Irish", "p246": "Scottish",
    "p247": "Scottish", "p248": "Indian F", "p249": "Scottish F", "p250": "English F", "p251": "Indian",
    "p252": "Scottish", "p253": "Welsh F", "p254": "English", "p255": "Scottish", "p256": "English",
    "p257": "English F", "p258": "English", "p259": "English", "p260": "Scottish", "p261": "NorthernIrish F",
    "p262": "Scottish F", "p263": "Scottish", "p264": "Scottish F", "p265": "Scottish F", "p266": "Irish F",
    "p267": "English F", "p268": "English F", "p269": "English F", "p270": "English", "p271": "Scottish",
    "p272": "Scottish", "p273": "English", "p274": "English", "p275": "Scottish", "p276": "English F",
    "p277": "English F", "p278": "English", "p279": "English", "p281": "Scottish", "p282": "English F",
    "p283": "Irish F", "p284": "Scottish", "p285": "Scottish", "p286": "English", "p287": "English",
    "p288": "Irish F", "p292": "NorthernIrish", "p293": "NorthernIrish F", "p294": "American F", "p295": "Irish F",
    "p297": "American F", "p298": "Irish", "p299": "American F", "p300": "American F", "p301": "American F",
    "p302": "Canadian", "p303": "Canadian F", "p304": "NorthernIrish", "p305": "American F", "p306": "American F",
    "p307": "Canadian F", "p308": "American F", "p310": "American F", "p311": "American", "p312": "Canadian F",
    "p313": "Irish F", "p314": "SouthAfrican F", "p315": "American", "p316": "Canadian", "p317": "Canadian F",
    "p318": "American F", "p323": "SouthAfrican F", "p326": "Australian", "p329": "American F",
    "p330": "American F", "p333": "American F", "p334": "American", "p335": "NewZealand F", "p336": "SouthAfrican F",
    "p339": "American F", "p340": "Irish F", "p341": "American F", "p343": "Canadian F", "p345": "American",
    "p347": "SouthAfrican", "p351": "NorthernIrish F", "p360": "American", "p361": "American F",
    "p362": "American F", "p363": "Canadian", "p364": "Irish", "p374": "Australian", "p376": "Indian",
    "s5": "English F",
}
# L2-ARCTIC speakers by first language (F = female)
_L2ARCTIC = {
    "ABA": "Arabic", "SKA": "Arabic F", "YBAA": "Arabic", "ZHAA": "Arabic F",
    "BWC": "Mandarin", "LXC": "Mandarin F", "NCC": "Mandarin F", "TXHC": "Mandarin",
    "ASI": "Hindi", "RRBI": "Hindi", "SVBI": "Hindi F", "TNI": "Hindi F",
    "HJK": "Korean F", "HKK": "Korean", "YDCK": "Korean F", "YKWK": "Korean",
    "EBVS": "Spanish", "ERMS": "Spanish", "MBMPS": "Spanish F", "NJS": "Spanish F",
    "HQTV": "Vietnamese", "PNV": "Vietnamese F", "THV": "Vietnamese F", "TLV": "Vietnamese",
}
_FEMALE_NAMES = {"amy", "kathleen", "kristin", "ljspeech", "hfc_female", "jenny_dioco", "alba", "cori",
                 "southern_english_female", "lessac"}

_SPANISH = {"AR", "ES", "MX", "CL", "CO", "PE", "UY", "PY", "BO", "EC", "VE", "PA", "CR", "DO", "CU", "GT", "HN",
            "SV", "NI", "PR"}
_ARABIC = {"AE", "SA", "QA", "EG", "JO", "KW", "BH", "OM", "MA", "DZ", "TN", "LB", "LY", "IQ", "SY", "YE", "SD"}
# country -> accents to look for, best first ("generic" = the untagged pool)
_ACCENTS = {"US": ["American"], "CA": ["Canadian", "American"], "GB": ["English", "Scottish", "Welsh", "NorthernIrish"],
            "IE": ["Irish", "NorthernIrish"], "AU": ["Australian", "NewZealand"], "NZ": ["NewZealand", "Australian"],
            "ZA": ["SouthAfrican"], "IN": ["Indian", "Hindi"], "PK": ["Hindi", "Indian"], "CN": ["Mandarin"],
            "TW": ["Mandarin"], "HK": ["Mandarin"], "SG": ["Mandarin"], "KR": ["Korean"], "VN": ["Vietnamese"]}


def accents_for(country: str | None) -> list[str]:
    c = (country or "").upper()
    if c in _SPANISH:
        return ["Spanish"]
    if c in _ARABIC:
        return ["Arabic"]
    return _ACCENTS.get(c, [])


class VoiceBank:
    """All Piper voices in a folder, sorted into accent pools."""

    def __init__(self, voices_dir: Path | str = "voices", default_model: Path | str | None = None) -> None:
        self.default_model = str(default_model) if default_model else None
        self.pools: dict[str, list[Voice]] = {}
        for onnx in sorted(Path(voices_dir).glob("*.onnx")):
            meta = onnx.with_name(onnx.name + ".json")
            if not meta.exists():
                continue
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if info.get("language", {}).get("family") not in (None, "en"):
                continue  # Spanish/Norwegian voices can't read English phraseology
            self._add(str(onnx), info)

    def _add(self, model: str, info: dict) -> None:
        name = Path(model).stem.lower()
        speakers = info.get("speaker_id_map") or {}
        table = _VCTK if "vctk" in name else _L2ARCTIC if "l2arctic" in name else None
        if speakers and table is not None:
            for spk, sid in speakers.items():
                tag = table.get(spk)
                if tag:
                    accent, _, f = tag.partition(" ")
                    self.pools.setdefault(accent, []).append(Voice(model, sid, accent=accent, female=f == "F"))
            return
        if speakers or (info.get("num_speakers") or 1) > 1:  # libritts: hundreds of untagged US speakers
            n = info.get("num_speakers") or len(speakers)
            ids = sorted(speakers.values()) if speakers else list(range(n))
            self.pools.setdefault("generic", []).extend(Voice(model, i, accent="generic") for i in ids)
            return
        locale = (info.get("language") or {}).get("code", "")
        accent = {"en_GB": "English", "en_US": "American"}.get(locale, "generic")
        female = any(n in name for n in _FEMALE_NAMES)
        self.pools.setdefault(accent, []).append(Voice(model, None, accent=accent, female=female))

    def _pick(self, key: str, accents: list[str], female_share: float, rate: tuple[float, float],
              avoid: set[Voice] = frozenset()) -> Voice | None:
        h = zlib.crc32(key.encode())
        want_female = (h % 1000) / 1000.0 < female_share
        for accent in accents + ["generic"]:
            pool = self.pools.get(accent) or []
            if not pool:
                continue
            same = [v for v in pool if v.female == want_female and v not in avoid] or \
                [v for v in pool if v not in avoid] or pool
            v = same[(h // 1000) % len(same)]
            lo, hi = rate
            r = round(lo + ((h >> 8) % 100) / 100.0 * (hi - lo), 2)
            return Voice(v.model, v.speaker, r, v.accent, v.female)
        return None

    def pilot(self, callsign: str, country: str | None) -> Voice | None:
        """Most airline pilots are men; the rate varies a little per person."""
        return self._pick(f"pilot:{callsign}", accents_for(country), 0.1, (0.88, 1.05))

    def controller(self, icao: str, role: str, country: str | None) -> Voice | None:
        """One voice per position: Aeroparque Ground and Aeroparque Tower are different people. Controllers talk
        a bit faster than the pilots."""
        return self._pick(f"atc:{icao}:{role}", accents_for(country), 0.3, (0.85, 0.95))

    def describe(self) -> str:
        return ", ".join(f"{k} {len(v)}" for k, v in sorted(self.pools.items())) or "no voices"


# --- who flies for which country ------------------------------------------------------------------------

_COUNTRY_ISO = {
    "united states": "US", "mexico": "MX", "united kingdom": "GB", "canada": "CA", "spain": "ES", "germany": "DE",
    "france": "FR", "australia": "AU", "italy": "IT", "south africa": "ZA", "china": "CN", "brazil": "BR",
    "netherlands": "NL", "japan": "JP", "egypt": "EG", "portugal": "PT", "colombia": "CO", "chile": "CL",
    "turkey": "TR", "united arab emirates": "AE", "india": "IN", "argentina": "AR", "new zealand": "NZ",
    "ireland": "IE", "venezuela": "VE", "peru": "PE", "ecuador": "EC", "south korea": "KR", "korea": "KR",
    "republic of korea": "KR", "taiwan": "TW", "hong kong": "HK", "singapore": "SG", "vietnam": "VN",
    "saudi arabia": "SA", "qatar": "QA", "kuwait": "KW", "bahrain": "BH", "oman": "OM", "jordan": "JO",
    "morocco": "MA", "algeria": "DZ", "tunisia": "TN", "lebanon": "LB", "uruguay": "UY", "paraguay": "PY",
    "bolivia": "BO", "panama": "PA", "costa rica": "CR", "dominican republic": "DO", "cuba": "CU",
    "guatemala": "GT", "honduras": "HN", "el salvador": "SV", "nicaragua": "NI", "puerto rico": "PR",
    "pakistan": "PK", "iraq": "IQ", "libya": "LY", "sudan": "SD", "yemen": "YE", "syria": "SY",
}


@lru_cache(maxsize=1)
def _airline_countries(path: str = "data/airlines.dat") -> dict[str, str]:
    """airline name / telephony / ICAO code (lower) -> ISO country, from the OpenFlights list."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 8 or row[7] != "Y":
                continue
            iso = _COUNTRY_ISO.get(row[6].strip().lower())
            if not iso:
                continue
            for k in (row[1], row[5], row[4]):
                k = (k or "").strip().lower()
                if k and k not in ("\\n", "n/a"):
                    out.setdefault(k, iso)
    return out


# registration prefix -> country (the common ones; longest prefix wins)
_REG = {"N": "US", "C": "CA", "G": "GB", "EI": "IE", "VH": "AU", "ZK": "NZ", "ZS": "ZA", "VT": "IN", "B": "CN",
        "HL": "KR", "LV": "AR", "LQ": "AR", "CC": "CL", "CX": "UY", "ZP": "PY", "CP": "BO", "HC": "EC", "OB": "PE",
        "HK": "CO", "YV": "VE", "XA": "MX", "XB": "MX", "XC": "MX", "EC": "ES", "PP": "BR", "PR": "BR", "PT": "BR",
        "PS": "BR", "A6": "AE", "HZ": "SA", "A7": "QA", "SU": "EG", "VN": "VN", "9V": "SG", "D": "DE", "F": "FR",
        "I": "IT", "PH": "NL", "CS": "PT", "TC": "TR", "JA": "JP"}


def country_of(airline: str | None, callsign: str | None) -> str | None:
    """Country an aircraft is from: its airline (name, radio telephony or ICAO code), else its registration."""
    table = _airline_countries()
    for k in ((airline or "").strip().lower(), (airline or "").split()[0].lower() if airline else ""):
        if k and k in table:
            return table[k]
    cs = (callsign or "").upper()
    m = re.fullmatch(r"([A-Z]{3})\d{1,4}[A-Z]?", cs)
    if m and m.group(1).lower() in table:
        return table[m.group(1).lower()]
    reg = cs.replace("-", "")
    for n in (2, 1):
        if reg[:n] in _REG and len(reg) > n:
            return _REG[reg[:n]]
    return None
