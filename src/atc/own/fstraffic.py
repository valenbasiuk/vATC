"""FS Traffic's own injection on or off, from the launcher (Valen 2026-10-15).

With our own traffic, FS Traffic's AI must not fly too: its aircraft don't hear this ATC, enter runways unasked
and landed head-on to ours (docs/OWN_TRAFFIC_PLAN.md, 2026-10-07). FS Traffic is two packages in the Community
folder (Just Flight support):
  - justflight-fstraffic-module          the aircraft, liveries and schedules: KEPT, our traffic uses them
                                         (catalog.py, schedule.py);
  - justflight-fstraffic-controlcentre   the Traffic Control Centre, which does the injecting.
"Off" moves the control centre (and any other fstraffic package that isn't the module, e.g. an in-game panel) out
of Community into <Community>/../vATC-parked/; "on" moves it back. MSFS reads Community when it starts: with the
sim running the change counts from its next start (and Windows may refuse to move files the sim has open).
Nothing is deleted, and nothing else in Community is touched.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from atc.own.catalog import FST_DIR, community_dir

PARK_DIR = "vATC-parked"
_SIM_EXES = ("FlightSimulator2024.exe", "FlightSimulator.exe")


def _injectors(folder: Path) -> list[Path]:
    """FS Traffic packages in `folder` that inject (everything fstraffic except the model/schedule module)."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_dir() and "fstraffic" in p.name.lower() and p.name.lower() != FST_DIR)


def _parked_dir(community: Path) -> Path:
    return community.parent / PARK_DIR


def status(community: Path | None = None) -> str:
    """'not installed' / 'on' (the control centre is in Community) / 'off' (parked by vATC) / 'models only'."""
    community = community or community_dir()
    if community is None:
        return "not installed"
    active = _injectors(community)
    if active:
        return "on"
    if _injectors(_parked_dir(community)):
        return "off"
    return "models only" if (community / FST_DIR).is_dir() else "not installed"


def sim_running() -> bool:
    """MSFS is running (Windows only; elsewhere False)."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(f'"{exe.lower()}"' in out for exe in _SIM_EXES)


def _move(src: Path, dst_dir: Path) -> str:
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / src.name
    if target.exists():
        return f"{src.name}: already in {dst_dir} (left as it is)"
    try:
        shutil.move(str(src), str(target))
    except OSError as exc:
        return f"{src.name}: could not move ({exc}); close MSFS and try again"
    return f"{src.name} -> {dst_dir}"


def set_injection(on: bool, community: Path | None = None) -> str:
    """Turn FS Traffic's injection on (packages back into Community) or off (parked). One line saying what happened."""
    community = community or community_dir()
    if community is None:
        return "FS Traffic: Community folder not found (ATC_COMMUNITY_DIR)"
    src, dst = (_parked_dir(community), community) if on else (community, _parked_dir(community))
    pkgs = _injectors(src)
    if not pkgs:
        return f"FS Traffic injection already {'on' if on else 'off'} ({status(community)})"
    done = "; ".join(_move(p, dst) for p in pkgs)
    later = " MSFS is running: restart it for this to count." if sim_running() else ""
    return f"FS Traffic injection {'on' if on else 'off'}: {done}.{later}"
