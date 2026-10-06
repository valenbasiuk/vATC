"""Joystick / yoke buttons for push-to-talk, through Windows' WinMM joystick API (no extra package).

STATUS: UNTESTED with real hardware. Run `python tools/probe_ptt.py` to see your devices and which button
number each physical button is. WinMM sees the first 16 game controllers and their first 32 buttons; a button
beyond 32 (some yokes have more) needs remapping in the device's software, or use a keyboard key instead.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

JOY_RETURNBUTTONS = 0x00000080
JOYERR_NOERROR = 0
MAX_DEVICES = 16


class JOYINFOEX(ctypes.Structure):
    _fields_ = [(n, wintypes.DWORD) for n in (
        "dwSize", "dwFlags", "dwXpos", "dwYpos", "dwZpos", "dwRpos", "dwUpos", "dwVpos", "dwButtons",
        "dwButtonNumber", "dwPOV", "dwReserved1", "dwReserved2")]


class JOYCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("szPname", wintypes.WCHAR * 32)] + \
        [(n, wintypes.UINT) for n in (
            "wXmin", "wXmax", "wYmin", "wYmax", "wZmin", "wZmax", "wNumButtons", "wPeriodMin", "wPeriodMax",
            "wRmin", "wRmax", "wUmin", "wUmax", "wVmin", "wVmax", "wCaps", "wMaxAxes", "wNumAxes",
            "wMaxButtons")] + \
        [("szRegKey", wintypes.WCHAR * 32), ("szOEMVxD", wintypes.WCHAR * 260)]


def _winmm():
    if sys.platform != "win32":
        raise RuntimeError("joystick push-to-talk needs Windows")
    return ctypes.WinDLL("winmm")


def buttons(device: int, dll=None) -> int | None:
    """Bitmask of pressed buttons (bit 0 = button 1) on one device, or None if it isn't connected."""
    dll = dll or _winmm()
    info = JOYINFOEX()
    info.dwSize = ctypes.sizeof(JOYINFOEX)
    info.dwFlags = JOY_RETURNBUTTONS
    if dll.joyGetPosEx(device, ctypes.byref(info)) != JOYERR_NOERROR:
        return None
    return int(info.dwButtons)


def devices(dll=None) -> list[tuple[int, str, int]]:
    """(device id, name, number of buttons) of every connected game controller."""
    dll = dll or _winmm()
    out = []
    for dev in range(MAX_DEVICES):
        if buttons(dev, dll) is None:
            continue
        caps = JOYCAPSW()
        name, n = "?", 0
        if dll.joyGetDevCapsW(dev, ctypes.byref(caps), ctypes.sizeof(JOYCAPSW)) == JOYERR_NOERROR:
            name, n = caps.szPname, caps.wNumButtons
        out.append((dev, name, n))
    return out


def pressed_list(mask: int) -> list[int]:
    """Bitmask -> 1-based button numbers: 0b101 -> [1, 3]."""
    return [i + 1 for i in range(32) if mask >> i & 1]


class JoystickButton:
    """One button (1-based, as tools/probe_ptt.py prints it) on one device, or on any device if None."""

    def __init__(self, button: int, device: int | None = None) -> None:
        self.bit = 1 << (button - 1)
        self.device = device
        self._dll = _winmm()
        self._devices = [device] if device is not None else [d for d, _, _ in devices(self._dll)]

    def pressed(self) -> bool:
        for dev in self._devices:
            mask = buttons(dev, self._dll)
            if mask is not None and mask & self.bit:
                return True
        return False
