"""Check this PC is ready. Standard library only, so it runs before anything is installed.

    python tools/check_env.py

Prints OK / WARN / FAIL per check and exits 1 if anything FAILed. Safe to run any time.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
fails = 0


def report(level: str, msg: str) -> None:
    global fails
    if level == "FAIL":
        fails += 1
    print(f"[{level:4}] {msg}")


def has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


# --- Python ---
v = sys.version_info
report("OK" if v >= (3, 11) else "FAIL", f"Python {v.major}.{v.minor}.{v.micro} (need 3.11+)")
bits = struct.calcsize("P") * 8
report("OK" if bits == 64 else "FAIL", f"{bits}-bit Python (SimConnect.dll needs 64-bit)")

# --- OS ---
system = platform.system()
report("OK" if system == "Windows" else "WARN", f"OS: {system} (the real sim link only works on Windows)")

# --- Packages ---
required = {"yaml": "pyyaml (core)"}
optional = {
    "SimConnect": "SimConnect  -> pip install -e .[sim]",
    "numpy": "numpy  -> pip install -e .[audio]",
    "sounddevice": "sounddevice  -> pip install -e .[audio]",
    "faster_whisper": "faster-whisper  -> pip install -e .[audio]",
    "piper": "piper-tts  -> pip install -e .[audio]",
    "pynput": "pynput  -> pip install -e .[audio]",
    "pytest": "pytest  -> pip install -e .[dev]",
}
for mod, label in required.items():
    report("OK" if has(mod) else "FAIL", f"package {label}")
for mod, label in optional.items():
    report("OK" if has(mod) else "WARN", f"package {label}")
report("OK" if has("atc") else "WARN", "atc package installed (pip install -e .)")

# --- SimConnect.dll ---
dll = os.environ.get("ATC_SIMCONNECT_DLL")
if dll:
    report("OK" if Path(dll).exists() else "FAIL", f"ATC_SIMCONNECT_DLL={dll}")
elif has("SimConnect"):
    spec = importlib.util.find_spec("SimConnect")
    locs = list(spec.submodule_search_locations or [])
    found = [Path(p) / "SimConnect.dll" for p in locs if (Path(p) / "SimConnect.dll").exists()]
    report("OK" if found else "WARN", f"SimConnect.dll bundled with the package: {found[0] if found else 'not found'}")
else:
    report("WARN", "SimConnect.dll: package not installed yet")

# --- MSFS running? (informational) ---
if system == "Windows":
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=15).stdout.lower()
        running = [n for n in ("flightsimulator2024.exe", "flightsimulator.exe") if n in out]
        report("OK" if running else "WARN", f"MSFS process: {running or 'not running (start it for the probe scripts)'}")
    except Exception as exc:  # noqa: BLE001
        report("WARN", f"could not list processes: {exc}")

# --- Audio devices ---
if has("sounddevice"):
    try:
        import sounddevice as sd

        devs = sd.query_devices()
        ins = [d["name"] for d in devs if d["max_input_channels"] > 0]
        outs = [d["name"] for d in devs if d["max_output_channels"] > 0]
        report("OK" if ins else "WARN", f"{len(ins)} input device(s), default: {sd.query_devices(kind='input')['name'] if ins else '-'}")
        report("OK" if outs else "WARN", f"{len(outs)} output device(s), default: {sd.query_devices(kind='output')['name'] if outs else '-'}")
    except Exception as exc:  # noqa: BLE001
        report("WARN", f"sounddevice could not query devices: {exc}")

# --- Project files ---
voices = list((ROOT / "voices").glob("*.onnx"))
report("OK" if voices else "WARN", f"Piper voices in voices/: {[p.name for p in voices] or 'none yet'}")
data_ok = all((ROOT / "data" / f).exists() for f in ("airports.csv", "runways.csv", "airport-frequencies.csv"))
report("OK" if data_ok else "WARN", "OurAirports CSVs in data/" + ("" if data_ok else "  -> atc-gen --download"))

# --- LLM config ---
base, key, model = (os.environ.get(k, "") for k in ("ATC_LLM_BASE_URL", "ATC_LLM_API_KEY", "ATC_LLM_MODEL"))
if not base:
    report("WARN", "ATC_LLM_BASE_URL not set -> stub replies (fine for now)")
else:
    report("OK" if key else "FAIL", "ATC_LLM_API_KEY set" if key else "ATC_LLM_API_KEY missing")
    report("OK" if model else "FAIL", f"ATC_LLM_MODEL={model or 'missing'}")

print()
print("Result:", "ready for the next step" if fails == 0 else f"{fails} blocking problem(s), fix the FAIL lines")
sys.exit(1 if fails else 0)
