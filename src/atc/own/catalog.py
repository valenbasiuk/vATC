"""Which model to create for an airline + aircraft type: the title of an AI model installed in the sim.

Sources, read from the Community folder on Valen's PC (never copied into this repo):
- FSLTL (`fsltl-traffic-base/FSLTL_Rules.vmr`, vPilot model matching): `CallsignPrefix` (airline ICAO) + `TypeCode`
  (ICAO type) -> `ModelName`, which is the aircraft.cfg title. "A//B" = one of them. A rule without CallsignPrefix
  is the type's fallback, usually a white livery ("..._ZZZZ").
- FS Traffic (`justflight-fstraffic-module/Data/Aircraft/aircraftIniFiles/<IATA type>.ini`): `[airline ICAO]`
  `title=`, `title2=`...; `[fallback]` white. IATA type -> ICAO type from `Data/DBs/aircraftCodesDB.csv`.
Both are legacy packages: the title alone selects model + livery (SimConnect_AICreateNonATCAircraft).

Pick order: a real livery from FSLTL, else a real livery from FS Traffic, else the type's white one.
Parsing 118k VMR lines takes a second or two: the result is cached in data/model_catalog.json.
"""

from __future__ import annotations

import csv
import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

CACHE = Path("data/model_catalog.json")
FSLTL_DIR = "fsltl-traffic-base"
FST_DIR = "justflight-fstraffic-module"
_RULE = re.compile(r'<ModelMatchRule\s+(?:CallsignPrefix\s*=\s*"(\w+)"\s+)?TypeCode\s*=\s*"(\w+)"\s+'
                   r'ModelName\s*=\s*"([^"]+)"')
_USERCFG = [  # MSFS 2024: Microsoft Store, then Steam
    Path(os.environ.get("LOCALAPPDATA", "")) / "Packages/Microsoft.Limitless_8wekyb3d8bbwe/LocalCache/UserCfg.opt",
    Path(os.environ.get("APPDATA", "")) / "Microsoft Flight Simulator 2024/UserCfg.opt",
]


def community_dir() -> Path | None:
    """ATC_COMMUNITY_DIR, else <InstalledPackagesPath>/Community from the sim's UserCfg.opt."""
    env = os.environ.get("ATC_COMMUNITY_DIR")
    if env:
        return Path(env)
    for cfg in _USERCFG:
        if not cfg.is_file():
            continue
        m = re.search(r'InstalledPackagesPath\s+"([^"]+)"', cfg.read_text(encoding="utf-8", errors="ignore"))
        if m and (Path(m.group(1)) / "Community").is_dir():
            return Path(m.group(1)) / "Community"
    return None


def _white(title: str) -> bool:
    t = title.upper()
    return t.endswith("_ZZZZ") or t.endswith("_ZZZ") or "WHITE" in t


@dataclass
class ModelCatalog:
    fsltl: dict[str, list[str]] = field(default_factory=dict)  # "ARG B738" / " B738" (type fallback) -> titles
    fst: dict[str, list[str]] = field(default_factory=dict)  # "ARG B738" / " B738" -> titles

    def pick(self, airline: str | None, type_icao: str, rng: random.Random | None = None,
             prefer: str | None = None) -> str | None:
        """A title for this airline and type: a real livery if any source has one (FS Traffic first, unless
        `prefer` / ATC_OWN_MODELS says "fsltl": Valen found FSLTL's Aerolineas 737 logo stretched), else a white one."""
        rng = rng or random.Random()
        a, t = (airline or "").upper(), type_icao.upper()
        prefer = (prefer or os.environ.get("ATC_OWN_MODELS") or "fstraffic").lower()
        real = {"fsltl": [x for x in self.fsltl.get(f"{a} {t}", []) if not _white(x)] if a else [],
                "fstraffic": [x for x in self.fst.get(f"{a} {t}", []) if not _white(x)] if a else []}
        first = "fsltl" if prefer == "fsltl" else "fstraffic"
        tiers = [
            real[first],
            real["fstraffic" if first == "fsltl" else "fsltl"],
            self.fsltl.get(f" {t}", []),
            self.fst.get(f" {t}", []),
            self.fsltl.get(f"{a} {t}", []) if a else [],
        ]
        for titles in tiers:
            if titles:
                return rng.choice(titles)
        return None

    def __len__(self) -> int:
        return len(self.fsltl) + len(self.fst)


def parse_vmr(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for prefix, typ, names in _RULE.findall(text):
        key = f"{prefix.upper()} {typ.upper()}"
        if key not in out:  # the first rule for a key wins, as in vPilot
            out[key] = [n.strip() for n in names.split("//") if n.strip()]
    return out


def parse_fstraffic(data_dir: Path) -> dict[str, list[str]]:
    """FS Traffic's per-type ini files -> 'ARG B738' -> titles; ' B738' -> the white fallback."""
    codes: dict[str, str] = {}
    db = data_dir / "DBs" / "aircraftCodesDB.csv"
    if db.is_file():
        with db.open(encoding="utf-8", errors="ignore", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("Code") and row.get("Ident"):
                    codes.setdefault(row["Code"].strip().upper(), row["Ident"].strip().upper())
    out: dict[str, list[str]] = {}
    for ini in sorted((data_dir / "Aircraft" / "aircraftIniFiles").glob("*.ini")):
        typ = codes.get(ini.stem.upper())
        if typ is None:
            continue
        section = None
        for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().upper()
                continue
            m = re.match(r"title\d*\s*=\s*(.+)", line, re.I)
            if m and section:
                airline = "" if section == "FALLBACK" else section
                titles = out.setdefault(f"{airline} {typ}", [])
                if m.group(1).strip() not in titles:
                    titles.append(m.group(1).strip())
    return out


def tug_title(community: Path | None, wingspan_m: float) -> str | None:
    """A pushback tug to show under the nose during a push: GSX's (Valen has GSX Pro), else an add-on airport's.
    ATC_OWN_TUG = a title to use, or "off". None = no tug (the push still happens)."""
    env = os.environ.get("ATC_OWN_TUG")
    if env:
        return None if env.lower() == "off" else env
    if community is None:
        return None
    if (community / "fsdreamteam-gsx-pro").is_dir():  # towbarless tractors: TPX-200 narrowbody, TPX-500 widebody
        return "FSDT_TPX_500_MTS" if wingspan_m >= 50 else "FSDT_TPX_200"
    if (community / "aerosoft-airport-engm-oslo").is_dir():
        return "ENGM_GroundVehicle_Tug_001"
    return None


def _stamp(community: Path) -> dict[str, float]:
    files = [community / FSLTL_DIR / "FSLTL_Rules.vmr", community / FST_DIR / "Data" / "DBs" / "aircraftCodesDB.csv"]
    return {str(f): f.stat().st_mtime for f in files if f.is_file()}


def load(community: Path | None = None, cache: Path = CACHE) -> ModelCatalog:
    """The catalog for this Community folder (cached until FSLTL / FS Traffic change). Empty if neither is there."""
    community = community or community_dir()
    if community is None:
        return ModelCatalog()
    stamp = _stamp(community)
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("stamp") == stamp:
                return ModelCatalog(data["fsltl"], data["fst"])
        except (ValueError, KeyError):
            pass
    vmr = community / FSLTL_DIR / "FSLTL_Rules.vmr"
    cat = ModelCatalog(
        parse_vmr(vmr.read_text(encoding="utf-8", errors="ignore")) if vmr.is_file() else {},
        parse_fstraffic(community / FST_DIR / "Data") if (community / FST_DIR).is_dir() else {},
    )
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"stamp": stamp, "fsltl": cat.fsltl, "fst": cat.fst}), encoding="utf-8")
    except OSError:
        pass
    return cat
