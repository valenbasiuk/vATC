"""The launcher's settings (improvement plan I1): one JSON file in the user's AppData, turned into the same
command line and environment `python -m atc` has always taken, so the old way keeps working unchanged.

    %APPDATA%\\vATC\\config.json   (ATC_CONFIG to put it elsewhere)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_MODELS = ("gemini:gemini-flash-lite-latest,nvidia:nvidia/nemotron-3-super-120b-a12b,"
                  "openrouter:nvidia/nemotron-3-super-120b-a12b:free,groq:qwen/qwen3.8-27b")


@dataclass
class Config:
    # flight
    sim: bool = True
    airport: str = ""  # empty: the one you are at (with the sim)
    simbrief_user: str = ""
    use_simbrief: bool = False
    callsign: str = ""
    telephony: str = ""
    # radio
    voice: str = "voices/en_US-libritts-high.onnx"  # "" = text only
    ptt: bool = True
    ptt_key: str = "f9"
    ptt_joy: int | None = None
    ptt_joy_device: int | None = None
    mic: str = ""
    audio_out: str = ""
    stt_model: str = "small.en"
    # ATC
    llm_models: str = DEFAULT_MODELS
    metar: str = "auto"  # auto (the sim's own weather is detected), on, off
    standby: float = 0.3
    atis_question: bool = True
    # traffic
    own_traffic: bool = False
    own_factor: float = 1.0
    own_max: int | None = None
    own_models: str = "fstraffic"  # or fsltl
    own_tug: bool = True
    extra: list[str] = field(default_factory=list)  # anything else for the command line

    def to_argv(self, status_file: str | None = None) -> list[str]:
        """The `python -m atc` arguments for these settings."""
        a: list[str] = []
        if self.sim:
            a.append("--sim")
        if self.airport.strip():
            a += ["--airport", self.airport.strip().upper()]
        if self.use_simbrief and Path("simbrief_last.json").exists():
            a += ["--simbrief", "simbrief_last.json"]
        if self.callsign.strip():
            a += ["--callsign", self.callsign.strip().upper()]
        if self.telephony.strip():
            a += ["--telephony", self.telephony.strip()]
        if self.voice.strip():
            a += ["--voice", self.voice.strip()]
        if self.ptt:
            a += ["--ptt", "--ptt-key", self.ptt_key or "none"]
            if self.ptt_joy is not None:
                a += ["--ptt-joy", str(self.ptt_joy)]
                if self.ptt_joy_device is not None:
                    a += ["--ptt-joy-device", str(self.ptt_joy_device)]
            if self.mic.strip():
                a += ["--mic", self.mic.strip()]
            if self.stt_model.strip():
                a += ["--stt-model", self.stt_model.strip()]
        if self.audio_out.strip():
            a += ["--audio-out", self.audio_out.strip()]
        a += ["--standby", f"{self.standby:g}"]
        if self.own_traffic:
            a += ["--own-traffic", "--own-factor", f"{self.own_factor:g}"]
            if self.own_max:
                a += ["--own-max", str(self.own_max)]
        if status_file:
            a += ["--status-file", status_file, "--stdin"]
        return a + list(self.extra)

    def env(self) -> dict[str, str]:
        """Environment variables for the ATC process."""
        e = {"ATC_LLM_MODEL": self.llm_models.strip(), "ATC_OWN_MODELS": self.own_models,
             "ATC_ATIS_QUESTION": "1" if self.atis_question else "0"}
        if self.metar == "off":
            e["ATC_METAR"] = "off"
        if not self.own_tug:
            e["ATC_OWN_TUG"] = "off"
        return {k: v for k, v in e.items() if v}


def path() -> Path:
    env = os.environ.get("ATC_CONFIG")
    if env:
        return Path(env)
    base = Path(os.environ.get("APPDATA") or Path.home())
    return base / "vATC" / "config.json"


def load(p: Path | None = None) -> Config:
    p = p or path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Config()
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})


def save(cfg: Config, p: Path | None = None) -> Path:
    p = p or path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), indent=1), encoding="utf-8")
    return p
