"""Who sounds like what: a voice per AI pilot (by airline or registration country) and per controller position (by
airport country), from every Piper voice in voices/ (python tools/download_voices.py fetches a good set).

Accent pools come from the multi-speaker models' speaker lists:
  - en_GB-vctk-medium: 109 native speakers tagged by accent (English, Scottish, Irish, American, Canadian,
    Australian, New Zealand, South African, Indian, Welsh...), from the VCTK corpus' speaker-info;
  - en_US-l2arctic-medium: 24 non-native speakers tagged by first language (Spanish, Mandarin, Hindi, Korean,
    Arabic, Vietnamese): an Aerolineas pilot or an Argentine controller sounds like one;
  - en_US-libritts(-r)-high: 904 US speakers, no tags: the generic pool;
  - single-speaker voices count for their locale (en_GB -> English, en_US -> American).
Voices that aren't downloaded are simply not in the pools: with only libritts everything falls back to it.

Kokoro (2026-10-15, `python tools/download_voices.py --kokoro`: voices/kokoro-v1.0.onnx + voices-v1.0.bin, 82M
parameters, Apache-2.0) sounds clearly more natural than Piper (better intonation over a whole clearance). Its
American and British voices go first in those pools, and voice BLENDS (two speakers' style vectors averaged:
"am_michael+am_puck@0.5") make dozens of distinct people out of them: the generic pool is Kokoro blends. Its
Spanish / Brazilian / Hindi / Mandarin / Italian / French / Japanese speakers reading English phonemes keep
some of their timbre and rhythm: they join those accent pools AFTER the L2-ARCTIC speakers (real accented
English) as more people, and are the only voices for Brazil, Italy, France and Japan. ATC_VOICE_ENGINE=piper
leaves Kokoro out even when it is downloaded.

Nobody shares a voice with another speaker heard in the last VOICE_ACTIVE_S (Valen 2026-10-08: at SABE pilots and
controllers kept sounding alike: the Spanish pool has 4 speakers, 2 of them men). A new speaker gets a person nobody
is using, else one of them pitched up or down a little (PITCHES: with the formants moving too, the same speaker
sounds like somebody else, more so through the radio filter), else the other sex, else the next accent and then the
generic pool. A speaker keeps their voice for the whole session.

voices/blacklist.txt (optional, one per line; '#' starts a comment line, ' #' a note after the entry): a voice to
never use, as tools/voice_samples.py names it ("en_US-l2arctic-medium#3", "kokoro-am_michael+am_puck@0.5"), or a whole accent ("accent:Spanish":
those countries then get the next accent on their list, or the generic pool).
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Voice:
    model: str  # .onnx path (Piper voice, or the Kokoro model)
    speaker: int | None  # speaker id in a multi-speaker model
    rate: float = 1.0  # Piper length_scale (< 1 = faster); Kokoro speaks at 1 / rate
    accent: str = ""
    female: bool = False
    pitch: float = 1.0  # > 1 higher (and a smaller voice): tts.py shifts the samples
    style: str = ""  # Kokoro: "am_michael", or a blend "am_michael+am_puck@0.6" (60 % the first)

    @property
    def kokoro(self) -> bool:
        return bool(self.style)

    @property
    def label(self) -> str:
        p = f" p{self.pitch:.2f}" if self.pitch != 1.0 else ""
        who = f"kokoro:{self.style}" if self.kokoro else f"{Path(self.model).stem}#{self.speaker}"
        return f"{who} {self.accent}{' F' if self.female else ''}{p}".strip()

    @property
    def key(self) -> str:
        """How voices/blacklist.txt names this voice: 'en_US-l2arctic-medium#3' (the model alone if single),
        'kokoro:am_michael', 'kokoro:am_michael+am_puck@0.5'."""
        if self.kokoro:
            return f"kokoro:{self.style}"
        return Path(self.model).stem + (f"#{self.speaker}" if self.speaker is not None else "")


VOICE_ACTIVE_S = 900.0  # a voice heard this recently belongs to that speaker: nobody else gets it
PITCHES = (0.92, 1.08)  # the same speaker made into another person when the pool runs out


def read_blacklist(voices_dir: Path | str) -> set[str]:
    path = Path(voices_dir) / "blacklist.txt"
    if not path.exists():
        return set()
    out = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = re.split(r"\s+#", raw.strip(), maxsplit=1)[0].strip()  # "en_US-l2arctic-medium#3  # too fast"
        if line and not line.startswith("#"):
            if line.startswith("kokoro-"):  # copied from a voice_samples file name
                line = "kokoro:" + line[len("kokoro-"):]
            out.add(line.removesuffix(".wav").removesuffix("_F"))
    return out


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
# Kokoro v1.0 speakers worth using on the radio (hexgrad/Kokoro-82M VOICES.md grades; af_nicole whispers, am_adam is
# graded F+): name -> accent pool. F = female from the name's second letter.
KOKORO_FILES = ("kokoro-v1.0.onnx", "kokoro-v1.0.fp16.onnx", "kokoro-v1.0.int8.onnx")
KOKORO_VOICES_BIN = "voices-v1.0.bin"
_KOKORO = {
    "af_heart": "American", "af_bella": "American", "af_sarah": "American", "af_kore": "American",
    "af_aoede": "American", "af_alloy": "American", "af_nova": "American",
    "am_michael": "American", "am_fenrir": "American", "am_puck": "American", "am_echo": "American",
    "am_eric": "American", "am_liam": "American", "am_onyx": "American",
    "bf_emma": "English", "bf_isabella": "English", "bf_alice": "English", "bf_lily": "English",
    "bm_george": "English", "bm_fable": "English", "bm_lewis": "English", "bm_daniel": "English",
    "em_alex": "Spanish", "em_santa": "Spanish", "ef_dora": "Spanish",
    "pm_alex": "Portuguese", "pm_santa": "Portuguese", "pf_dora": "Portuguese",
    "hm_omega": "Hindi", "hm_psi": "Hindi", "hf_alpha": "Hindi", "hf_beta": "Hindi",
    "zm_yunjian": "Mandarin", "zm_yunxi": "Mandarin", "zm_yunyang": "Mandarin", "zf_xiaoxiao": "Mandarin",
    "im_nicola": "Italian", "if_sara": "Italian", "ff_siwis": "French", "jm_kumo": "Japanese", "jf_alpha": "Japanese",
}
# the better-graded speakers each blend leans on: a blend of a weak and a good voice is a new, cleaner person
_KOKORO_BASES = {("American", False): ["am_michael", "am_fenrir", "am_puck"],
                 ("American", True): ["af_heart", "af_bella", "af_sarah"],
                 ("English", False): ["bm_george", "bm_fable", "bm_lewis"],
                 ("English", True): ["bf_emma", "bf_isabella"]}
# graded D: only heard blended with one of the bases above, never on their own
_KOKORO_BLEND_ONLY = {"am_echo", "am_eric", "am_liam", "am_onyx", "bf_alice", "bf_lily", "bm_lewis", "bm_daniel"}
KOKORO_FIRST = {"American", "English", "generic", "Portuguese", "Italian", "French", "Japanese"}


def kokoro_model(voices_dir: Path | str) -> Path | None:
    """voices/kokoro-v1.0(.fp16|.int8).onnx with voices-v1.0.bin next to it, unless ATC_VOICE_ENGINE=piper."""
    if os.environ.get("ATC_VOICE_ENGINE", "auto").lower() == "piper":
        return None
    d = Path(voices_dir)
    if not (d / KOKORO_VOICES_BIN).exists():
        return None
    return next((d / f for f in KOKORO_FILES if (d / f).exists()), None)


_FEMALE_NAMES = {"amy", "kathleen", "kristin", "ljspeech", "hfc_female", "jenny_dioco", "alba", "cori",
                 "southern_english_female", "lessac"}

_SPANISH = {"AR", "ES", "MX", "CL", "CO", "PE", "UY", "PY", "BO", "EC", "VE", "PA", "CR", "DO", "CU", "GT", "HN",
            "SV", "NI", "PR"}
_ARABIC = {"AE", "SA", "QA", "EG", "JO", "KW", "BH", "OM", "MA", "DZ", "TN", "LB", "LY", "IQ", "SY", "YE", "SD"}
# country -> accents to look for, best first ("generic" = the untagged pool)
_ACCENTS = {"US": ["American"], "CA": ["Canadian", "American"], "GB": ["English", "Scottish", "Welsh", "NorthernIrish"],
            "IE": ["Irish", "NorthernIrish"], "AU": ["Australian", "NewZealand"], "NZ": ["NewZealand", "Australian"],
            "ZA": ["SouthAfrican"], "IN": ["Indian", "Hindi"], "PK": ["Hindi", "Indian"], "CN": ["Mandarin"],
            "TW": ["Mandarin"], "HK": ["Mandarin"], "SG": ["Mandarin"], "KR": ["Korean"], "VN": ["Vietnamese"],
            "BR": ["Portuguese", "Spanish"], "PT": ["Portuguese", "Spanish"], "IT": ["Italian"], "FR": ["French"],
            "BE": ["French"], "CH": ["French", "Italian"], "JP": ["Japanese"]}


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
        self.clock = time.monotonic
        self._lock = threading.Lock()  # the chatter thread and the main loop both ask
        self._who: dict[str, Voice] = {}  # speaker key -> their voice for the session
        self._last: dict[str, float] = {}  # speaker key -> when they last spoke
        self.blacklist = read_blacklist(voices_dir)
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
        km = kokoro_model(voices_dir)
        if km is not None:
            self._add_kokoro(str(km))
        for accent in list(self.pools):
            if f"accent:{accent}" in self.blacklist:
                del self.pools[accent]
                continue
            self.pools[accent] = [v for v in self.pools[accent] if v.key not in self.blacklist]
            if not self.pools[accent]:
                del self.pools[accent]

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

    def _add_kokoro(self, model: str) -> None:
        """Kokoro speakers into their accent pools, plus blends: more distinct people for the busy pools."""
        added: dict[str, list[Voice]] = {}
        for name, accent in _KOKORO.items():
            if name in _KOKORO_BLEND_ONLY:
                continue
            v = Voice(model, None, accent=accent, female=name[1] == "f", style=name)
            added.setdefault(accent, []).append(v)
        for (accent, female), bases in _KOKORO_BASES.items():
            others = [n for n, a in _KOKORO.items() if a == accent and (n[1] == "f") == female]
            seen = set()
            for b in bases:
                for o in others:
                    if o == b or (o, b) in seen:
                        continue
                    seen.add((b, o))
                    w = 0.5 if o in bases else 0.65  # lean on the better-graded one
                    pool = "generic" if accent == "American" and len(seen) % 2 == 0 else accent
                    added.setdefault(pool, []).append(Voice(model, None, accent=pool, female=female,
                                                            style=f"{b}+{o}@{w:g}"))
        for accent, voices in added.items():
            have = self.pools.get(accent, [])
            self.pools[accent] = voices + have if accent in KOKORO_FIRST else have + voices

    def _pick(self, key: str, accents: list[str], female_share: float, rate: tuple[float, float]) -> Voice | None:
        with self._lock:
            now = self.clock()
            v = self._who.get(key)
            if v is None:
                v = self._new_voice(key, accents, female_share, rate, now)
                if v is None:
                    return None
                self._who[key] = v
            self._last[key] = now
            return v

    def _new_voice(self, key: str, accents: list[str], female_share: float, rate: tuple[float, float],
                   now: float) -> Voice | None:
        h = zlib.crc32(key.encode())
        want_female = (h % 1000) / 1000.0 < female_share
        lo, hi = rate
        r = round(lo + ((h >> 8) % 100) / 100.0 * (hi - lo), 2)
        active = [self._who[k] for k, t in self._last.items() if now - t < VOICE_ACTIVE_S and k in self._who]
        people = {v.key for v in active}
        taken = {(v.key, v.pitch) for v in active}
        for accent in accents + ["generic"]:
            # this speaker's own order through the pool: two new aircraft don't both start at its first voice
            pool = sorted(self.pools.get(accent) or [],
                          key=lambda v: (self._tier(accent, v), zlib.crc32(f"{key}|{v.key}".encode())))
            if not pool:
                continue
            for sex in (want_female, not want_female):
                same = [v for v in pool if v.female == sex]
                choice = next(((v, 1.0) for v in same if v.key not in people), None) or \
                    next(((v, p) for p in PITCHES for v in same if (v.key, p) not in taken), None)
                if choice is not None:
                    v, pitch = choice
                    return Voice(v.model, v.speaker, r, v.accent, v.female, pitch, v.style)
        for accent in accents + ["generic"]:  # every voice is talking: the old hashed pick
            pool = self.pools.get(accent) or []
            if pool:
                v = pool[(h // 1000) % len(pool)]
                return Voice(v.model, v.speaker, r, v.accent, v.female, style=v.style)
        return None

    @staticmethod
    def _tier(accent: str, v: Voice) -> int:
        """0 = the engine that pool prefers (Kokoro for American / English / generic and the accents only it has;
        L2-ARCTIC's real accented speakers first for Spanish, Hindi, Mandarin...), 1 = the rest."""
        return 0 if v.kokoro == (accent in KOKORO_FIRST) else 1

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
